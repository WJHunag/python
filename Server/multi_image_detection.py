import argparse
import os
import time
import datetime
import pytz
import json
import threading
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple
import re
import cv2
import requests
from requests.auth import HTTPDigestAuth
from watchdog.observers.polling import PollingObserver
from watchdog.events import FileSystemEventHandler
from concurrent.futures import ThreadPoolExecutor, wait
import torch
import numpy as np
from types import SimpleNamespace
import base64
import image_metadata

from smoke_config.distributed_config_store import DistributedConfigStore

# 偵測後端只保留 YOLOv6 API 與 Ultralytics。
DEFAULT_DETECTION_BACKEND = os.getenv("DETECTION_BACKEND", "yolov6_api").lower()
YOLOV6_API_URL = os.getenv("YOLOV6_API_URL", "http://yolov6:8000/infer")
ULTRALYTICS_WEIGHTS = os.getenv("ULTRALYTICS_WEIGHTS", "./weights/ultralytics/best.pt")
ULTRALYTICS_IMGSZ = int(os.getenv("ULTRALYTICS_IMGSZ", "640"))
NIGHT_SCORE_THR = float(os.getenv("NIGHT_SCORE_THR", "0.9"))

UI_SERVER_UPDATE_URL = os.getenv(
    "UI_SERVER_UPDATE_URL",
    "http://192.168.4.110:58080/image/image_update",
)


# 初始化 logging
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(levelname)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

# =========================
# [ADD] Lightweight in-process profiler (no ptrace needed)
# =========================
from collections import defaultdict, deque
from contextlib import contextmanager

class PerfStats:
    """
    低侵入效能統計：
    - 以 key 累積耗時樣本（ms）
    - 週期性輸出 avg / p50 / p95 / max
    - 用環境變數開關，避免常態開銷
    """
    def __init__(self):
        self.enabled = os.getenv("PERF_ENABLE", "0") == "1"
        self.log_sec = float(os.getenv("PERF_LOG_SEC", "60"))
        self.max_samples = int(os.getenv("PERF_MAX_SAMPLES", "2000"))
        self._data = defaultdict(lambda: deque(maxlen=self.max_samples))
        self._lock = threading.Lock()
        self._last_log_ts = time.time()

    def add_ms(self, key: str, ms: float):
        if not self.enabled:
            return
        with self._lock:
            self._data[key].append(ms)

    @contextmanager
    def timer(self, key: str):
        if not self.enabled:
            yield
            return
        t0 = time.perf_counter()
        try:
            yield
        finally:
            dt_ms = (time.perf_counter() - t0) * 1000.0
            self.add_ms(key, dt_ms)

    def maybe_log(self, topk: int = 12):
        if not self.enabled:
            return
        now = time.time()
        if (now - self._last_log_ts) < self.log_sec:
            return
        self._last_log_ts = now

        with self._lock:
            items = list(self._data.items())

        rows = []
        for k, dq in items:
            if not dq:
                continue
            arr = np.fromiter(dq, dtype=np.float64)  # 你檔案本來就有 import numpy as np :contentReference[oaicite:5]{index=5}
            n = int(arr.size)
            avg = float(arr.mean())
            p50 = float(np.percentile(arr, 50))
            p95 = float(np.percentile(arr, 95))
            mx  = float(arr.max())
            rows.append((p95, k, n, avg, p50, p95, mx))

        rows.sort(reverse=True)  # 依 p95 排序，最能反映尖峰卡頓
        logging.info("[PERF] top by p95 (ms):")
        for _, k, n, avg, p50, p95, mx in rows[:topk]:
            logging.info(f"[PERF] {k:<24} n={n:<5d} avg={avg:>8.2f} p50={p50:>8.2f} p95={p95:>8.2f} max={mx:>8.2f}")

PERF = PerfStats()

def _normalize_ip_variants(ip: Any) -> Set[str]:
    if ip is None:
        return set()
    raw = str(ip).strip()
    if not raw:
        return set()
    dotted = raw.replace('_', '.')
    underscored = raw.replace('.', '_')
    return {raw, dotted, underscored}


def _normalize_ws_key(ws_ip: str) -> Tuple[str, Optional[int]]:
    if not isinstance(ws_ip, str):
        return str(ws_ip), None
    if ":" in ws_ip and ws_ip.count(":") == 1 and not ws_ip.startswith("["):
        host, maybe_port = ws_ip.split(":", 1)
        try:
            return host, int(maybe_port)
        except ValueError:
            return host, None
    return ws_ip, None


def _as_ws_entry(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        entry = dict(value)
        entry.setdefault("polling_cameras", [])
        entry.setdefault("fixed_cameras", [])
        return entry
    if isinstance(value, list):
        return {"polling_cameras": list(value), "fixed_cameras": []}
    return {"polling_cameras": [], "fixed_cameras": []}


def _entry_camera_ids(entry: Any) -> List[int]:
    if isinstance(entry, dict):
        raw = (entry.get("polling_cameras") or []) + (entry.get("fixed_cameras") or [])
    elif isinstance(entry, list):
        raw = entry
    else:
        raw = []

    ids: List[int] = []
    for value in raw:
        try:
            ids.append(int(value))
        except (TypeError, ValueError):
            logging.warning(f"忽略無效 camera id: {value!r}")
    return list(dict.fromkeys(ids))


def _resolve_distributed_config_dir(distributed_config_dir: Optional[str], json_path: str) -> Optional[Path]:
    candidates: List[Path] = []

    for raw in (distributed_config_dir, os.getenv("DISTRIBUTED_CONFIG_DIR")):
        if raw:
            candidates.append(Path(raw).expanduser())

    cwd = Path.cwd()
    script_dir = Path(__file__).resolve().parent
    if json_path:
        json_parent = Path(json_path).expanduser().parent
        candidates.append(json_parent / "smoke_config" / "distributed_config")

    candidates.extend([
        cwd / "smoke_config" / "distributed_config",
        script_dir / "smoke_config" / "distributed_config",
    ])

    seen = set()
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        if (
            resolved.is_dir()
            and (resolved / "common_settings.json").is_file()
            and (resolved / "workstation_assignments.json").is_file()
            and (resolved / "camera_configs").is_dir()
        ):
            return resolved
    return None


class ConfigManager:
    """
    配置管理，統一處理 JSON 設定檔。
    """
    def __init__(self, json_path: str, distributed_config_dir: Optional[str] = None):
        self.json_path = json_path
        self.distributed_config_dir = _resolve_distributed_config_dir(distributed_config_dir, json_path)
        self.config_source = "legacy_json"
        self.camera_ip_to_channel: Dict[str, int] = {}
        self.camera_ip_to_camera_config: Dict[str, Dict[str, Any]] = {}
        self.camera_ip_to_workstation_url: Dict[str, str] = {}
        self.config_data = self._load_config()

    def _load_config(self) -> Dict[str, Any]:
        if self.distributed_config_dir:
            store = DistributedConfigStore(distributed_root=self.distributed_config_dir)
            config = store.build_total_config(fallback_to_legacy=False)
            self.config_source = f"distributed_config:{self.distributed_config_dir}"
            self._augment_distributed_config(config)
            logging.info(f"使用拆分設定檔: {self.distributed_config_dir}")
            return config

        if not os.path.exists(self.json_path):
            raise FileNotFoundError(
                f"Configuration file not found and distributed config is unavailable: {self.json_path}"
            )
        with open(self.json_path, 'r', encoding='utf-8') as f:
            config = json.load(f)
        self.config_source = f"legacy_json:{self.json_path}"
        self._build_ip_to_channel(config.get("cameras") or [])
        logging.warning(f"使用舊版設定檔: {self.json_path}")
        return config

    def get(self, key: str) -> Any:
        return self.config_data.get(key)

    def _build_ip_to_channel(self, cameras: List[Dict[str, Any]]) -> None:
        self.camera_ip_to_channel = {}
        self.camera_ip_to_camera_config = {}
        seen_ips: Dict[str, int] = {}
        for cam in cameras:
            if not isinstance(cam, dict):
                continue
            try:
                channel_id = int(cam.get("NVR_channel"))
            except (TypeError, ValueError):
                logging.error(f"Camera 缺少有效 NVR_channel: {cam}")
                continue

            ip = cam.get("ip")
            if not ip:
                logging.error(f"Camera channel {channel_id} 缺少 ip。")
                continue

            for variant in _normalize_ip_variants(ip):
                if variant in seen_ips and seen_ips[variant] != channel_id:
                    logging.error(
                        f"Camera IP 重複: {variant} 同時對應 channel {seen_ips[variant]} 與 {channel_id}"
                    )
                    continue
                seen_ips[variant] = channel_id
                self.camera_ip_to_channel[variant] = channel_id
                self.camera_ip_to_camera_config[variant] = cam

    def _augment_distributed_config(self, config: Dict[str, Any]) -> None:
        cameras = config.get("cameras") or []
        assignments = config.get("workstation_ip_mapping") or {}
        self._build_ip_to_channel(cameras)

        cameras_by_channel: Dict[int, Dict[str, Any]] = {}
        for cam in cameras:
            if not isinstance(cam, dict):
                continue
            try:
                cameras_by_channel[int(cam.get("NVR_channel"))] = cam
            except (TypeError, ValueError):
                pass

        workstation_camera_mapping: Dict[str, List[str]] = {}
        workstation_url_mapping: Dict[str, str] = {}
        camera_ip_to_workstation_url: Dict[str, str] = {}
        assigned_channels: Set[int] = set()
        channel_owner: Dict[int, str] = {}

        default_port = int(os.getenv("WORKSTATION_DEFAULT_PORT", "5001"))
        common_url_mapping = config.get("workstation_url_mapping") or {}

        for ws_key, raw_entry in assignments.items():
            host, port_in_key = _normalize_ws_key(str(ws_key))
            entry = _as_ws_entry(raw_entry)
            port = entry.get("port")
            if port is None:
                port = port_in_key if port_in_key is not None else default_port
            try:
                port = int(port)
            except (TypeError, ValueError):
                port = default_port

            workstation_url = (
                entry.get("url")
                or common_url_mapping.get(ws_key)
                or common_url_mapping.get(host)
                or f"http://{host}:{port}/receive_result"
            )
            workstation_url_mapping[ws_key] = workstation_url
            workstation_url_mapping[host] = workstation_url
            workstation_camera_mapping.setdefault(ws_key, [])

            for channel_id in _entry_camera_ids(entry):
                if channel_id in channel_owner and channel_owner[channel_id] != ws_key:
                    raise ValueError(
                        f"Camera channel {channel_id} 被重複分派到 {channel_owner[channel_id]} 與 {ws_key}"
                    )
                channel_owner[channel_id] = ws_key
                assigned_channels.add(channel_id)

                cam = cameras_by_channel.get(channel_id)
                if cam is None:
                    raise ValueError(f"工作站 {ws_key} 指向不存在的 camera channel {channel_id}")

                ip = cam.get("ip")
                if not ip:
                    raise ValueError(f"Camera channel {channel_id} 缺少 ip，無法推導上傳路由。")

                underscored_ip = str(ip).replace('.', '_')
                workstation_camera_mapping[ws_key].append(underscored_ip)
                for variant in _normalize_ip_variants(ip):
                    camera_ip_to_workstation_url[variant] = workstation_url

        unassigned = sorted(set(cameras_by_channel.keys()) - assigned_channels)
        if unassigned:
            logging.warning(f"有 {len(unassigned)} 台 camera 未分派到工作站: {unassigned}")

        config["workstation_camera_mapping"] = workstation_camera_mapping
        config["workstation_url_mapping"] = workstation_url_mapping
        config["camera_ip_to_workstation_url"] = camera_ip_to_workstation_url
        self.camera_ip_to_workstation_url = camera_ip_to_workstation_url

        missing_routes = []
        for ws_key, ips in workstation_camera_mapping.items():
            for ip in ips:
                if ip not in camera_ip_to_workstation_url:
                    missing_routes.append((ws_key, ip))
        if missing_routes:
            raise ValueError(f"部分 workstation_camera_mapping 無法對應上傳 URL: {missing_routes[:10]}")

        logging.info(
            "拆分設定路由建立完成: cameras=%d assigned=%d routes=%d workstations=%d",
            len(cameras_by_channel),
            len(assigned_channels),
            len(camera_ip_to_workstation_url),
            len(assignments),
        )

    def get_camera_account_password(self) -> Optional[List[str]]:
        for key in ("Cameras_account_password", "camera_account_password", "NVR_account_password"):
            value = self.config_data.get(key)
            if isinstance(value, list) and len(value) >= 2:
                return value
        return None

    def get_workstation_url(self, camera_ip: str) -> Optional[str]:
        for variant in _normalize_ip_variants(camera_ip):
            url = self.camera_ip_to_workstation_url.get(variant)
            if url:
                return url
        return None

    def get_camera_config(self, camera_ip: str) -> Optional[Dict[str, Any]]:
        for variant in _normalize_ip_variants(camera_ip):
            camera_config = self.camera_ip_to_camera_config.get(variant)
            if camera_config:
                return camera_config
        return None

    def get_camera_stage1_confidence(self, camera_ip: str) -> Optional[float]:
        camera_config = self.get_camera_config(camera_ip)
        if not camera_config:
            return None

        raw_value = camera_config.get("stage1_confidence")
        try:
            value = float(raw_value)
        except (TypeError, ValueError):
            logging.warning(f"Camera {camera_ip} 的 stage1_confidence 無效: {raw_value!r}，改用全域門檻。")
            return None

        if 0.0 <= value <= 1.0:
            return value

        logging.warning(f"Camera {camera_ip} 的 stage1_confidence 超出 0~1: {value}，改用全域門檻。")
        return None

    def get_ip_to_channel(self) -> Dict[str, int]:
        return dict(self.camera_ip_to_channel)


class CameraController:
    """
    負責攝影機鏡頭控制的類別。
    """
    def __init__(self, config_manager: ConfigManager, cool_down_seconds: int = 60):
        self.config_manager = config_manager
        self.cool_down_seconds = cool_down_seconds
        self.camera_cooling_down = {}  # 每台攝影機的冷卻狀態

    def adjust_camera_zoom_on_detection(self, ip: str, boxes: List[float], focus_time: int):
        """
        控制攝影機縮放，避免重複執行。
        """
        high_t, width_t = 7, 4
        ip = ip.replace('_', '.')

        # 檢查攝影機是否在冷卻
        if self.camera_cooling_down.get(ip, False):
            logging.info(f"攝影機 {ip} 在冷卻狀態，跳過縮放。")
            return

        if len(boxes) == 0:
            return

        acc_pwd = self.config_manager.get_camera_account_password()
        if not acc_pwd or len(acc_pwd) < 2:
            logging.warning("Camera account or password not found in config.")
            return

        acc, pwd = acc_pwd[0], acc_pwd[1]

        # 解包座標並放大比例
        xmin, ymin, xmax, ymax = boxes
        xmin, ymin, xmax, ymax = int(xmin) * width_t, int(ymin) * high_t, int(xmax) * width_t, int(ymax) * high_t

        # 設置攝影機為冷卻狀態
        self.camera_cooling_down[ip] = True

        # 鏡頭縮放
        url_zoom = f"http://{ip}/cgi-bin/ptzBase.cgi?action=moveDirectly&channel=1&startPoint[0]={xmin}&startPoint[1]={ymin}&endPoint[0]={xmax}&endPoint[1]={ymax}"
        try:
            requests.get(url_zoom, auth=HTTPDigestAuth(acc, pwd), timeout=10)
            time.sleep(focus_time * 60)  # 根據 focus_time 設定放大時間
        except requests.RequestException as e:
            logging.error(f"Error adjusting camera zoom: {e}")

        # 鏡頭回預置點
        url_preset = f"http://{ip}/cgi-bin/ptz.cgi?action=start&channel=1&code=GotoPreset&arg1=0&arg2=1&arg3=0"
        try:
            requests.get(url_preset, auth=HTTPDigestAuth(acc, pwd), timeout=10)
        except requests.RequestException as e:
            logging.error(f"Error returning camera to preset: {e}")

        # 設置攝影機為非冷卻狀態
        threading.Timer(self.cool_down_seconds, self.reset_cooling_down, [ip]).start()

    def reset_cooling_down(self, ip: str):
        self.camera_cooling_down[ip] = False
        logging.info(f"攝影機 {ip} 冷卻完成，允許縮放。")


class ImageUploader:
    """
    負責將圖片上傳到對應工作站的類別。
    """
    def __init__(self, config_manager: ConfigManager, max_workers: int = 8):
        self.config_manager = config_manager
        self.upload_lock = threading.Lock()
        self.uploaded_images = set()

        # [MOD] 用固定大小的 ThreadPool，避免每個 bbox 都開新 thread 造成 CPU 飆高
        self.executor = ThreadPoolExecutor(max_workers=max_workers)

    def determine_workstation_url(self, camera_ip: str) -> str:
        """
        根據 camera IP 從配置中取得對應工作站的上傳 URL。
        如未找到則記錄錯誤。
        """
        workstation_url = self.config_manager.get_workstation_url(camera_ip)
        if workstation_url:
            return workstation_url

        # 從配置中獲取映射
        workstation_mapping = self.config_manager.get('workstation_camera_mapping')
        url_mapping = self.config_manager.get('workstation_url_mapping')

        # 驗證資料是否完整
        if not workstation_mapping or not url_mapping:
            logging.error("JSON 配置中缺少 workstation_mapping 或 url_mapping.")
            return None

        # 查找工作站名稱
        for workstation, ips in workstation_mapping.items():
            if camera_ip in ips:
                # 查找工作站 URL
                if workstation in url_mapping:
                    return url_mapping[workstation]
                else:
                    logging.error(f"工作站 {workstation} 缺少 URL 對應。")
                    return None

        # 若未找到對應工作站
        logging.error(f"IP {camera_ip} 無法匹配到任何工作站，請檢查拆分設定或舊版 JSON 配置。")
        return None

    def upload_image(self, image_path: Path, camera_ip: str, total_cropped: int,crop_x: int = 0, crop_y: int = 0):
        """
        將裁剪後的圖片上傳到對應工作站，並於上傳成功後刪除本地檔案。
        """
        workstation_url = self.determine_workstation_url(camera_ip)
        with self.upload_lock:
            if str(image_path) in self.uploaded_images:
                logging.info(f"{image_path} 已上傳過，跳過。")
                return
            self.uploaded_images.add(str(image_path))

        if not image_path.exists():
            logging.warning(f"{image_path} 不存在，無法上傳。")
            return

        try:
            with image_path.open('rb') as f:
                files = {'file': f}
                data = {
                    'total_cropped': total_cropped,
                    'crop_x': crop_x,
                    'crop_y': crop_y
                }
                with PERF.timer("upload.request"):
                    response = requests.post(workstation_url, files=files, data=data, timeout=10)
            if response.status_code == 200:
                logging.info(f"上傳成功: {image_path} from {camera_ip}")
                try:
                    with PERF.timer("upload.unlink"):
                        image_path.unlink(missing_ok=True)
                    logging.info(f"成功刪除已上傳的圖片: {image_path}")
                except Exception as e:
                    logging.error(f"刪除圖片失敗 {image_path}: {e}")
            else:
                logging.error(f"上傳失敗 {image_path} from {camera_ip}: {response.text}")
        except Exception as e:
            logging.error(f"上傳圖片 {image_path} 時發生錯誤: {e}")


class DetectionProcessor:
    """
    負責影像偵測與後處理的類別。
    """
    # [修改] __init__，使其能彈性接收 API URL 或本地模型
    def __init__(self, uploader: ImageUploader, score_thr: float, out_dir: Path, 
                 backend: str, # <-- [修改] 明確傳入 backend
                 night_score_thr: float = NIGHT_SCORE_THR,
                 api_url: Optional[str] = None, 
                 model: Optional[Any] = None, 
                 test_pipeline: Optional[Any] = None):
        
        self.api_url = api_url
        self.model = model
        self.test_pipeline = test_pipeline
        self.uploader = uploader
        self.base_score_thr = score_thr
        self.night_score_thr = night_score_thr
        self.out_dir = out_dir
        self.backend = backend # <-- [修改] 直接使用傳入的 backend
        self._stage1_threshold_log_cache: Set[str] = set()
        
        # [修改] 移除自動判斷邏輯，改為 Log 輸出
        if self.backend == 'api_v6':
            logging.info("DetectionProcessor: 後端設定為 YOLOv6 API。")
        elif self.backend == 'ultralytics':
            logging.info("DetectionProcessor: 後端設定為 Ultralytics。")
        else:
            # 增加錯誤處理
            raise ValueError(f"DetectionProcessor: 未知的 backend: {self.backend}")

    def is_night_window(self, tz_name: str = 'Asia/Taipei') -> bool:
        """每天 18:00 ～ 隔日 06:00 視為夜間。"""
        tz = pytz.timezone(tz_name)
        now = datetime.datetime.now(tz).time()
        return (now >= datetime.time(18, 0)) or (now <= datetime.time(6, 0))

    def current_score_thr(self, camera_ip: Optional[str] = None) -> float:
        """優先使用 camera.stage1_confidence；未設定時沿用全域門檻。"""
        global_thr = self.night_score_thr if self.is_night_window() else self.base_score_thr
        if not camera_ip:
            return global_thr

        camera_thr = self.uploader.config_manager.get_camera_stage1_confidence(camera_ip)
        if camera_thr is None:
            return global_thr

        if camera_thr != global_thr and camera_ip not in self._stage1_threshold_log_cache:
            logging.info(
                f"Camera {camera_ip} 使用 stage1_confidence={camera_thr} "
                f"(全域門檻={global_thr})"
            )
            self._stage1_threshold_log_cache.add(camera_ip)
        return camera_thr

    # 輔助函式：建立一個空的偵測結果物件
    def _create_empty_result(self) -> SimpleNamespace:
        return SimpleNamespace(pred_instances={
            'bboxes': torch.empty((0, 4), dtype=torch.int32),
            'scores': torch.empty((0,), dtype=torch.float32),
            'labels': torch.empty((0,), dtype=torch.int64)
        })

    # API 推論函式 (邏輯獨立)
    def run_inference_api(self, image_path: Path, camera_ip: str):
        """
        [MOD] API 模式延後解碼：
        - 這裡只讀 bytes + 呼叫 API + 做 threshold 過濾
        - 不做 cv2.imdecode，避免每張圖都解碼吃 CPU
        - frame 統一回傳 None，等挑到最佳影格後才解碼一次
        """
        try:
            with PERF.timer("api.read_bytes"):
                image_bytes = image_path.read_bytes()
        except Exception as e:
            logging.error(f"[API] 讀取影像失敗 {image_path}: {e}")
            return 0, self._create_empty_result(), None

        try:
            files = {'file': (image_path.name, image_bytes, 'image/jpeg')}
            with PERF.timer("api.request"):
                response = requests.post(self.api_url, files=files, timeout=10)
                response.raise_for_status()
                api_data = response.json()
            if isinstance(api_data, dict):
                detections = api_data.get("boxes", []) or api_data.get("detections", [])
            elif isinstance(api_data, list):
                detections = api_data
            else:
                detections = []
        except requests.exceptions.RequestException as e:
            logging.error(f"呼叫 API 失敗 ({self.api_url}) ({image_path}): {e}")
            return 0, self._create_empty_result(), None

        if not detections:
            return 0, self._create_empty_result(), None

        with PERF.timer("api.postprocess"):
            bboxes_list, scores_list, labels_list = [], [], []
            for det in detections:
                if isinstance(det, dict):
                    bbox = det.get('bbox') or det.get('bbox_xyxy') or det.get('xyxy')
                    if bbox is None and all(k in det for k in ('x1', 'y1', 'x2', 'y2')):
                        bbox = [det['x1'], det['y1'], det['x2'], det['y2']]
                    score = det.get('conf', det.get('score', det.get('confidence', 0.0)))
                    label = det.get('cls', det.get('label', det.get('class_id', 0)))
                elif isinstance(det, (list, tuple)) and len(det) >= 4:
                    bbox = det[:4]
                    score = det[4] if len(det) >= 5 else 1.0
                    label = det[5] if len(det) >= 6 else 0
                else:
                    continue
                if bbox is None:
                    continue
                bboxes_list.append(bbox)
                scores_list.append(score)
                labels_list.append(label)

            if not bboxes_list:
                return 0, self._create_empty_result(), None

            bboxes_tensor = torch.tensor(bboxes_list, dtype=torch.int32)
            scores_tensor = torch.tensor(scores_list, dtype=torch.float32)
            labels_tensor = torch.tensor(labels_list, dtype=torch.int64)

            # 依該攝影機 stage1_confidence 做本機過濾；未設定時用全域門檻。
            current_thr = self.current_score_thr(camera_ip)
            keep_mask = scores_tensor >= current_thr
            bboxes_tensor = bboxes_tensor[keep_mask]
            scores_tensor = scores_tensor[keep_mask]
            labels_tensor = labels_tensor[keep_mask]

        result = SimpleNamespace(pred_instances={
            'bboxes': bboxes_tensor,
            'scores': scores_tensor,
            'labels': labels_tensor
        })

        # 以「過濾後」的盒數作為該張影像的有效偵測量，供挑最佳影像用
        num_boxes = int(scores_tensor.numel())

        # [MOD] frame 在 API 模式固定回傳 None（延後到最佳影格才解碼）
        return num_boxes, result, None

    # 本地模型推論函式 (Ultralytics)
    def run_inference_local(self, image_path: Path, camera_ip: str):
        """
        (這是你原本的 run_inference 邏輯，只處理本地模型)
        """
        try:
            frame = cv2.imread(str(image_path))
            if frame is None:
                logging.warning(f"[Local] 無法讀取影像 (cv2): {image_path}")
                return 0, self._create_empty_result(), None
        except Exception as e:
            logging.error(f"[Local] 讀取影像失敗 {image_path}: {e}")
            return 0, self._create_empty_result(), None

        # Ultralytics local model
        if self.backend == 'ultralytics':
            with torch.no_grad():
                yres = self.model.predict(
                    source=frame,
                    imgsz=getattr(self, 'yolo_imgsz', 640),
                    conf=0.0,
                    device=getattr(self, 'device', None),
                    verbose=False,
                )
            boxes_obj = yres[0].boxes if yres else None
            if boxes_obj is None:
                return 0, self._create_empty_result(), frame
            bboxes = boxes_obj.xyxy.detach().cpu().round().to(torch.int32)
            scores = boxes_obj.conf.detach().cpu()
            labels = boxes_obj.cls.detach().cpu().to(torch.int64)
            result = SimpleNamespace(pred_instances={'bboxes': bboxes, 'scores': scores, 'labels': labels})
            num_boxes = int((scores > self.current_score_thr(camera_ip)).sum().item())
            return num_boxes, result, frame
        
        logging.error(f"[Local] 未知的 backend: {self.backend}")
        return 0, self._create_empty_result(), frame

    # run_inference 現在變成一個「分派器」(dispatcher)
    def run_inference(self, image_path: Path, camera_ip: str, base_filename: str):
        """
        執行偵測，自動分派給 API 或本地模型。
        """
        if self.backend == 'api_v6':
            return self.run_inference_api(image_path, camera_ip)
        else:
            return self.run_inference_local(image_path, camera_ip)


    def crop_best_frame(self, frame, result, camera_ip: str, base_filename: str, source_metadata: Optional[Dict[str, Any]] = None):
            """
            以最佳影像的偵測框來裁切目標圖像，並上傳裁切後的圖片。
            [MOD] 不再每個 bbox 開一條 thread，改用 ThreadPoolExecutor 限制併發。
            回傳 futures 供外部 wait。
            """
            futures = []
            if hasattr(result, 'pred_instances'):
                valid_boxes = result.pred_instances['scores'] > self.current_score_thr(camera_ip)
                bboxes = result.pred_instances['bboxes'][valid_boxes].cpu().numpy().tolist()
                for idx, bbox in enumerate(bboxes):
                    x1, y1, x2, y2 = map(int, bbox)
                    cropped_image = frame[y1:y2, x1:x2]
                    cropped_filename = f"{base_filename}_cropped_{idx+1}.jpg"
                    cropped_save_path = self.out_dir / cropped_filename
                    with PERF.timer("crop.imwrite"):
                        cv2.imwrite(str(cropped_save_path), cropped_image)
                    cropped_metadata = image_metadata.build_cropped_metadata(
                        source_metadata,
                        filename=cropped_filename,
                        group_id=base_filename,
                        crop_index=idx + 1,
                        total_cropped=len(bboxes),
                        bbox_xyxy=bbox,
                    )
                    image_metadata.maybe_attach_metadata_to_jpeg_file(cropped_save_path, cropped_metadata)

                    fut = self.uploader.executor.submit(
                        self.uploader.upload_image,
                        cropped_save_path, camera_ip, len(bboxes), x1, y1
                    )
                    futures.append(fut)

            return futures


# [MOD] 增加 channel_id 參數
    def save_best_frame_with_boxes(
        self,
        frame,
        result,
        camera_ip: str,
        base_filename: str,
        channel_id: int = None,
        source_metadata: Optional[Dict[str, Any]] = None,
    ):
        """
        在最佳框數的影像上標註框並保存，接著上傳到工作站，並推送 Base64 給 UI Server。
        """
        # [DEBUG] 強力除錯 Log：確認函式被呼叫時到底收到了什麼
        logging.info(f"======== [DEBUG] save_best_frame_with_boxes START ========")
        logging.info(f"IP: {camera_ip}, Filename: {base_filename}")
        logging.info(f"收到 Channel ID: {channel_id} (型態: {type(channel_id)})")
        logging.info(f"UI Server URL: {UI_SERVER_UPDATE_URL}")

        # 1. 畫框 (保持不變)
        if hasattr(result, 'pred_instances'):
            valid_boxes = result.pred_instances['scores'] > self.current_score_thr(camera_ip)
            bboxes = result.pred_instances['bboxes'][valid_boxes].cpu().numpy()
            for bbox in bboxes:
                x1, y1, x2, y2 = map(int, bbox)
                cv2.rectangle(frame, (x1, y1), (x2, y2), (0, 255, 0), 2)
        
        # 2. 存檔 (保持不變)
        frame_filename = f"{base_filename}_with_boxes.jpg"
        frame_path = self.out_dir / camera_ip / frame_filename
        frame_path.parent.mkdir(parents=True, exist_ok=True)
        with PERF.timer("save.imwrite"):
            cv2.imwrite(str(frame_path), frame)
        boxes = []
        if hasattr(result, 'pred_instances'):
            valid_boxes = result.pred_instances['scores'] > self.current_score_thr(camera_ip)
            bboxes = result.pred_instances['bboxes'][valid_boxes].cpu().numpy().tolist()
            scores = result.pred_instances['scores'][valid_boxes].cpu().numpy().tolist()
            labels = result.pred_instances['labels'][valid_boxes].cpu().numpy().tolist()
            boxes = [
                {"bbox_xyxy": bbox, "score": float(score), "label": int(label)}
                for bbox, score, label in zip(bboxes, scores, labels)
            ]
        frame_metadata = dict(source_metadata or {})
        if channel_id is not None:
            frame_metadata["nvr_channel"] = int(channel_id)
        frame_metadata = image_metadata.build_with_boxes_metadata(
            frame_metadata,
            filename=frame_filename,
            group_id=base_filename,
            boxes=boxes,
        )
        image_metadata.maybe_attach_metadata_to_jpeg_file(frame_path, frame_metadata)

        # 3. [ADD] 推送 Base64 給 smoke_UI_server
        if channel_id is not None:
            logging.info(f"[DEBUG] Channel ID 存在，準備轉換 Base64...")
            try:
                # ==================== [MOD] 新增：縮圖與壓縮 ====================
                # 為了符合 UI Server 的 300KB 限制，我們先把圖縮小
                # 假設縮放到寬度 800px (高度等比例)，這對網頁預覽已經足夠清晰
                target_width = 800
                height, width = frame.shape[:2]
                scale = target_width / width
                
                if scale < 1: # 只有當原圖比 800 大才縮小
                    new_height = int(height * scale)
                    preview_frame = cv2.resize(frame, (target_width, new_height))
                else:
                    preview_frame = frame

                # 轉成 JPEG 時設定品質參數 (例如 70%)
                # 這樣通常能將檔案壓在 100KB 以內
                with PERF.timer("ui.encode"):
                    _, buffer = cv2.imencode('.jpg', preview_frame, [int(cv2.IMWRITE_JPEG_QUALITY), 70])
                # ===============================================================

                # 轉成 Base64 字串
                b64_str = base64.b64encode(buffer).decode('utf-8')
                logging.info(f"[DEBUG] Base64 轉換成功，大小約: {len(b64_str)/1024:.2f} KB")
                
                # 拼裝 Data URL
                img_data_url = f"data:image/jpeg;base64,{b64_str}"
                
                # 發送 POST 請求
                payload = {
                    "NVR_channel": channel_id,
                    "picture": img_data_url
                }
                
                # 發送請求
                with PERF.timer("ui.request"):
                    response = requests.post(UI_SERVER_UPDATE_URL, json=payload, timeout=2)
                
                if response.status_code == 200:
                    logging.info(f"已推送預覽圖至 UI Server: Channel {channel_id}")
                else:
                    # 這樣可以看到具體的錯誤原因 (例如還是 413)
                    logging.warning(f"推送預覽圖失敗 (Code {response.status_code}): {response.text}")
                
            except Exception as e:
                logging.error(f"[DEBUG] 推送預覽圖發生未預期錯誤: {e}")

        # 4. 上傳到工作站 (保持不變)
        workstation_url = self.uploader.determine_workstation_url(camera_ip)
        try:
            with frame_path.open('rb') as f:
                with PERF.timer("workstation.request"):
                    response = requests.post(workstation_url, files={'file': (frame_path.name, f, 'image/jpeg')}, timeout=10)
                if response.status_code == 200:
                    logging.info(f"結果已成功上傳至工作站: {camera_ip}")
                else:
                    logging.error(f"結果上傳失敗: {camera_ip}: {response.text}")
        except Exception as e:
            logging.error(f"上傳結果時發生錯誤: {camera_ip}: {e}")
            
        logging.info(f"======== [DEBUG] save_best_frame_with_boxes END ========")

class ImageHandler(FileSystemEventHandler):
    """
    檔案事件處理類別，監控資料夾中新產生的影像檔並處理。
    """
    def __init__(self, args, config_manager: ConfigManager, camera_ctrl: CameraController, detector: DetectionProcessor):
        self.args = args
        self.config_manager = config_manager
        self.camera_ctrl = camera_ctrl
        self.detector = detector

        self.frames_by_camera: Dict[str, List[Path]] = {}
        self.processed_images: Dict[str, List[Path]] = {}
        self.max_boxes_by_camera: Dict[str, int] = {}
        self.best_frame_by_camera: Dict[str, Any] = {}
        self.best_result_by_camera: Dict[str, Any] = {}
        self.first_frame_timestamp = {}
        self.metadata_by_image: Dict[str, Dict[str, Any]] = {}
        self.input_dir = Path(args.folder)
        self.out_dir = Path(args.out_dir)
        self.group_size = args.group_size
        self.ip_to_channel = self.config_manager.get_ip_to_channel()
        self.state_lock = threading.RLock()

        self.initialize_existing_images()

    def initialize_existing_images(self):
        """
        初始化階段，將資料夾中已存在的圖片加入處理序列。
        """
        image_paths = list(self.input_dir.glob("*.jpg")) + \
                      list(self.input_dir.glob("*.jpeg")) + \
                      list(self.input_dir.glob("*.png"))
        for image_path in image_paths:
            self.handle_new_image(image_path)

    def clear_input_folder_if_needed(self, threshold=1500):
        """
        若資料夾圖片數量超過 threshold，則清空資料夾。
        """
        image_files = list(self.input_dir.glob('*.[jp][pn]g')) + list(self.input_dir.glob('*.jpeg'))
        if len(image_files) > threshold:
            logging.info(f"檔案數量超過 {threshold}，清空資料夾 {self.input_dir}")
            for file in image_files:
                try:
                    file.unlink()
                    #logging.info(f"刪除檔案: {file}")
                except Exception as e:
                    logging.error(f"無法刪除檔案 {file}: {e}")
            # 清空 frames_by_camera
            for camera_ip in self.frames_by_camera:
                self.frames_by_camera[camera_ip] = []
            logging.info("已清空 frames_by_camera 中的等待處理圖片。")

    def on_created(self, event):
        if not event.is_directory and event.src_path.lower().endswith(('.png', '.jpg', '.jpeg')):
            self.handle_new_image(Path(event.src_path))

    def on_moved(self, event):
        if not event.is_directory and event.dest_path.lower().endswith(('.png', '.jpg', '.jpeg')):
            self.handle_new_image(Path(event.dest_path))

    def handle_new_image(self, image_path: Path):
        """
        將新檔案依照攝影機 IP 分組，並在達到 group_size 時觸發後續處理。
        """
        with self.state_lock:
            if not image_path.exists():
                logging.warning(f"新檔案 {image_path} 不存在，跳過處理。")
                return

            metadata = image_metadata.safe_read_metadata_from_jpeg_file(image_path)
            context = image_metadata.resolve_image_context(image_path.name, metadata)
            camera_ip = context.get("camera_ip_key") or self.extract_camera_ip_from_filename(image_path.name)
            if camera_ip is None:
                logging.warning(f"無法從檔名中提取攝影機 IP：{image_path.name}")
                return
            self.metadata_by_image[str(image_path)] = context

            if camera_ip not in self.frames_by_camera:
                self.frames_by_camera[camera_ip] = []
                self.processed_images[camera_ip] = []
                self.max_boxes_by_camera[camera_ip] = -1
                self.best_frame_by_camera[camera_ip] = None
                self.best_result_by_camera[camera_ip] = None
                self.first_frame_timestamp[camera_ip] = time.time()
            else:
                # 若目前組中沒有影像，也表示可以重新記錄時間
                if len(self.frames_by_camera[camera_ip]) == 0:
                    self.first_frame_timestamp[camera_ip] = time.time()


            try:
                self.frames_by_camera[camera_ip].append(image_path)
                self.processed_images[camera_ip].append(image_path)
            except Exception as e:
                logging.error(f"處理檔案 {image_path} 時發生錯誤: {e}")

            # 檢查是否已滿組或等待時間過長
            current_wait = time.time() - self.first_frame_timestamp[camera_ip]
            max_wait_time = self.group_size * 60  # 例如：10*90 = 900 秒
            force_detect = current_wait >= max_wait_time
            if len(self.frames_by_camera[camera_ip]) >= self.group_size or force_detect:
                if force_detect:
                    logging.info(f"等待時間 {current_wait:.0f} 秒超過理論上限 {max_wait_time} 秒，強制進行偵測。")
                self.process_detected_images_group(camera_ip, force_detect=force_detect)

    def process_expired_groups(self):
        """
        定期檢查等待中的攝影機分組；即使沒有新的檔案事件，超時也會強制進行偵測。
        """
        with self.state_lock:
            now = time.time()
            max_wait_time = self.group_size * 60
            expired_groups = []
            for camera_ip, frames in list(self.frames_by_camera.items()):
                if not frames:
                    continue
                first_ts = self.first_frame_timestamp.get(camera_ip)
                if first_ts is None:
                    continue
                current_wait = now - first_ts
                if len(frames) < self.group_size and current_wait >= max_wait_time:
                    expired_groups.append((camera_ip, current_wait, len(frames)))

            for camera_ip, current_wait, frame_count in expired_groups:
                logging.info(
                    f"{camera_ip} 等待 {current_wait:.0f} 秒仍未滿組 "
                    f"({frame_count}/{self.group_size})，強制進行偵測。"
                )
                self.process_detected_images_group(camera_ip, force_detect=True)

    @staticmethod
    def extract_camera_ip_from_filename(filename: str) -> Optional[str]:
        """
        根據檔名模式(如 IP_..._..._...)解析出 camera_ip。
        假設前四段為 IP。
        """
        parts = filename.split('_')
        if len(parts) >= 4:
            return "_".join(parts[:4])
        return None

    def process_detected_images_group(self, camera_ip: str, force_detect: bool = False):
        """
        處理該攝影機的一組影像 (group)：
        - 針對每張圖片執行偵測（不進行裁切）
        - 挑選出偵測框數最多的最佳影像
        - 以最佳影像的框進行裁切並上傳，同時保存標註框的完整影像
        - 若檔名包含 `_focus5min`，則執行 CameraController 縮放
        - 清空該組影像並重置狀態
        """
        try:
            with PERF.timer("group.total"):
                # 清空過多的圖片
                with PERF.timer("group.clear_folder"):
                    self.clear_input_folder_if_needed()

                ip_dot_format = camera_ip.replace('_', '.')

                # 先嘗試用點號格式查，查不到再試試原始格式
                channel_id = self.ip_to_channel.get(ip_dot_format)
                if channel_id is None:
                    # [FIX] 嘗試將底線格式 IP (111_70_19_4) 轉為點號格式 (111.70.19.4)
                    ip_dot_format = camera_ip.replace('_', '.')
                    channel_id = self.ip_to_channel.get(ip_dot_format)

                    # 如果還是找不到，再試試原始格式 (Fallback)
                    if channel_id is None:
                        # [FIX] 1. 將底線格式轉為點號格式
                        ip_dot_format = camera_ip.replace('_', '.')

                        # [FIX] 2. 使用點號格式查詢
                        channel_id = self.ip_to_channel.get(ip_dot_format)

                        # 如果還是沒有，嘗試原始格式 (Fallback)
                        if channel_id is None:
                            channel_id = self.ip_to_channel.get(camera_ip)

                if channel_id is None:
                    logging.warning(f"[UI Push] 找不到 IP {camera_ip} (或 {ip_dot_format}) 對應的 Channel ID，將跳過 UI 推送。")
                else:
                    logging.info(f"[UI Push] 找到 Channel ID: {channel_id} (IP: {ip_dot_format})")

                # (debug log) 可選：印出來確認是否有抓到
                if channel_id is None:
                    logging.warning(f"無法找到 IP {camera_ip} (或 {ip_dot_format}) 對應的 Channel ID，UI 預覽將不會更新。")

                # 獲取存在的圖片
                existing_images = [img for img in self.frames_by_camera.get(camera_ip, []) if img.exists()]
                if not existing_images:
                    logging.info(f"資料夾無可用圖片，重置 {camera_ip}")
                    self.reset_camera_state(camera_ip)
                    return
                source_metadata = None
                for img in existing_images:
                    source_metadata = self.metadata_by_image.get(str(img))
                    if source_metadata:
                        break

                # 若未達 group_size，則暫存影像等待更多；超時時改用現有影像強制偵測
                if len(existing_images) < self.group_size and not force_detect:
                    self.frames_by_camera[camera_ip] = existing_images
                    return
                if len(existing_images) < self.group_size and force_detect:
                    logging.info(
                        f"{camera_ip} 目前僅有 {len(existing_images)}/{self.group_size} 張影像，"
                        "因等待超時仍強制進行偵測。"
                    )

                # 生成標準化檔名
                tz = pytz.timezone('Asia/Taipei')
                timestamp = datetime.datetime.now(tz).strftime("%Y%m%d_%H%M%S")
                formatted_ip = camera_ip.replace('.', '_')
                base_filename = f"{formatted_ip}_{timestamp}"

                # 初始化最佳影像的記錄
                best_num_boxes = -1
                best_frame = None
                best_result = None
                best_image_path = None

                # 針對每張影像進行偵測 (不做裁切)
                with PERF.timer("group.detect_loop"):
                    for image_path in existing_images:
                        try:
                            num_boxes, result, frame = self.detector.run_inference(image_path, camera_ip, base_filename)

                            # [MOD] 只有「非 API 模式」才要求 frame 一定要有
                            if self.detector.backend != 'api_v6' and frame is None:
                                logging.warning(f"偵測時 frame 為 None，跳過: {image_path}")
                                continue
                            if num_boxes > best_num_boxes:
                                best_num_boxes = num_boxes
                                best_frame = frame
                                best_result = result
                                best_image_path = image_path
                        except FileNotFoundError:
                            logging.warning(f"檔案 {image_path} 不存在，跳過處理。")
                        except Exception as e:
                            logging.error(f"處理檔案 {image_path} 時發生錯誤: {e}")

                # 若找到最佳影像
                if best_num_boxes > 0 and best_result is not None and best_image_path is not None:
                    try:
                        # [MOD] API 模式下 best_frame 會是 None：此時才解碼一次
                        if best_frame is None:
                            with PERF.timer("group.best_decode"):
                                best_frame = cv2.imread(str(best_image_path))
                            if best_frame is None:
                                logging.warning(f"最佳影像解碼失敗，跳過: {best_image_path}")
                                self.reset_camera_state(camera_ip)
                                return
                        best_metadata = dict(self.metadata_by_image.get(str(best_image_path)) or source_metadata or {})
                        best_metadata.setdefault("camera_ip_key", camera_ip)
                        best_metadata.setdefault("camera_ip", camera_ip.replace('_', '.'))
                        if channel_id is not None:
                            best_metadata.setdefault("nvr_channel", int(channel_id))

                        # 以最佳影像的偵測框進行裁切並上傳（回傳 futures）
                        with PERF.timer("group.crop_submit"):
                            futures = self.detector.crop_best_frame(
                                best_frame, best_result, camera_ip, base_filename, best_metadata
                            )

                        # [MOD] 等待固定併發池完成（取代 thread.join）
                        if futures:
                            with PERF.timer("group.wait_upload"):
                                wait(futures)

                        # 上傳最佳影像（加上標註框）的結果
                        with PERF.timer("group.save_best"):
                            self.detector.save_best_frame_with_boxes(
                                best_frame, best_result, camera_ip, base_filename, channel_id, best_metadata
                            )

                        # 下面你原本的 focus 判斷與後續流程維持不變


                        # 檢查是否需要執行焦點控制
                        focus_match = re.search(r'_focus(\d+)min', best_image_path.name)
                        metadata_focus_time = best_metadata.get("focus_time_min")
                        if focus_match or metadata_focus_time:
                            focus_time = int(focus_match.group(1)) if focus_match else int(metadata_focus_time)
                            logging.info(f"檔名包含焦點控制標記，進行縮放處理：{best_image_path.name}")
                            if hasattr(best_result, 'pred_instances'):
                                valid_mask = best_result.pred_instances['scores'] > self.detector.current_score_thr(camera_ip)
                                valid_boxes = best_result.pred_instances['bboxes'][valid_mask]
                                if len(valid_boxes) > 0:
                                    best_bbox = valid_boxes[0].cpu().numpy().tolist()
                                    self.camera_ctrl.adjust_camera_zoom_on_detection(camera_ip, best_bbox, focus_time)
                        else:
                            logging.info(f"檔名未包含焦點控制標記，跳過縮放處理：{best_image_path.name}")
                    except Exception as e:
                        logging.error(f"保存最佳影像或進行縮放處理時發生錯誤: {e}")
                else:
                    logging.info(f"無有效影像可儲存: {camera_ip}")

                # 移除已處理的影像
                with PERF.timer("group.cleanup_delete"):
                    for img_path in self.processed_images[camera_ip]:
                        if img_path.exists():
                            try:
                                img_path.unlink()
                            except OSError as e:
                                logging.error(f"刪除影像失敗 {img_path}: {e}")
                        self.metadata_by_image.pop(str(img_path), None)

                # 重置攝影機狀態
                self.reset_camera_state(camera_ip)
                if camera_ip in self.first_frame_timestamp:
                    del self.first_frame_timestamp[camera_ip]

        except Exception as e:
            logging.error(f"處理影像組 (camera_ip={camera_ip}) 時發生未預期的錯誤: {e}")
        finally:
            # 週期性輸出統計（避免每次都刷屏）
            PERF.maybe_log()


    def reset_camera_state(self, camera_ip: str):
        """
        重置某攝影機的處理狀態。
        """
        self.frames_by_camera[camera_ip] = []
        self.processed_images[camera_ip] = []
        self.max_boxes_by_camera[camera_ip] = -1
        self.best_frame_by_camera[camera_ip] = None
        self.best_result_by_camera[camera_ip] = None


def parse_args():
    parser = argparse.ArgumentParser(description='A40 smoke detection folder worker')
    parser.add_argument('folder', help='監測的資料夾')
    parser.add_argument('config', nargs='?', help='舊版 config 參數，保留相容但不使用')
    parser.add_argument('checkpoint', nargs='?', help='舊版 checkpoint 參數，保留相容但不使用')
    parser.add_argument('--backend', choices=['yolov6_api', 'ultralytics'], default=DEFAULT_DETECTION_BACKEND,
                        help='偵測後端：yolov6_api 或 ultralytics')
    parser.add_argument('--api-url', default=YOLOV6_API_URL, help='YOLOv6 API URL')
    parser.add_argument('--ultralytics-weights', default=ULTRALYTICS_WEIGHTS, help='Ultralytics 權重檔路徑')
    parser.add_argument('--imgsz', type=int, default=ULTRALYTICS_IMGSZ, help='Ultralytics image size')
    parser.add_argument('--ui-server-update-url', default=UI_SERVER_UPDATE_URL, help='UI 預覽圖更新 URL')
    parser.add_argument('--device', default='cuda:0', help='使用的裝置')
    parser.add_argument('--score-thr', type=float, default=0.7, help='Bbox score threshold')
    parser.add_argument('--night-score-thr', type=float, default=NIGHT_SCORE_THR, help='夜間 Bbox score threshold')
    parser.add_argument('--out-dir', type=str, required=True, help='輸出資料夾')
    parser.add_argument('--group-size', type=int, default=8, help='每組影像數量')
    parser.add_argument('--json-path', type=str, default='./smoke_camera_config.json', help='舊版 JSON 配置檔路徑（僅作為 fallback）')
    parser.add_argument('--distributed-config-dir', type=str, default=None, help='拆分設定資料夾路徑')
    return parser.parse_args()


def main():
    args = parse_args()
    global UI_SERVER_UPDATE_URL
    UI_SERVER_UPDATE_URL = args.ui_server_update_url

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    input_dir = Path(args.folder)
    input_dir.mkdir(parents=True, exist_ok=True)

    # 初始化
    config_manager = ConfigManager(args.json_path, distributed_config_dir=args.distributed_config_dir)
    camera_ctrl = CameraController(config_manager=config_manager)
    uploader = ImageUploader(config_manager=config_manager)

    if args.backend == 'yolov6_api':
        logging.info(f"[Main] 後端模式: YOLOv6 API (URL: {args.api_url})")
        detector = DetectionProcessor(
            uploader=uploader, 
            score_thr=args.score_thr, 
            out_dir=out_dir,
            backend='api_v6',
            night_score_thr=args.night_score_thr,
            api_url=args.api_url
        )

    elif args.backend == 'ultralytics':
        logging.info(f'[Main] 後端模式: Ultralytics, weights={args.ultralytics_weights}')
        try:
            from ultralytics import YOLO

            yolo = YOLO(args.ultralytics_weights)
            logging.info("[Main] 使用 ultralytics 套件載入模型。")
        except Exception as e:
            raise RuntimeError(f"Ultralytics 後端載入失敗，請確認 ultralytics 套件與權重路徑: {e}") from e
        
        detector = DetectionProcessor(
            uploader=uploader, 
            score_thr=args.score_thr, 
            out_dir=out_dir,
            backend='ultralytics',
            night_score_thr=args.night_score_thr,
            model=yolo
        )
        detector.yolo_imgsz = args.imgsz
        detector.device = args.device

    else:
        raise ValueError(f"未知的 detection backend: {args.backend}")

    event_handler = ImageHandler(args, config_manager, camera_ctrl, detector)

    observer = PollingObserver()
    observer.schedule(event_handler, str(input_dir), recursive=True)
    logging.info(f"開始監控資料夾: {input_dir}")
    observer.start()

    try:
        while True:
            event_handler.process_expired_groups()
            time.sleep(1)
    except KeyboardInterrupt:
        observer.stop()
    observer.join()


if __name__ == '__main__':
    main()
