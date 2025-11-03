import argparse
import os
import time
import datetime
import pytz
import json
import threading
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional
import re
import cv2
import mmcv
import requests
from requests.auth import HTTPDigestAuth
from mmcv.transforms import Compose
from mmdet.apis import inference_detector, init_detector
from mmyolo.registry import VISUALIZERS
from watchdog.observers.polling import PollingObserver
from watchdog.events import FileSystemEventHandler
import torch
import numpy as np
from types import SimpleNamespace

# [ADD] 強制走 YOLOv5
FORCE_ULTRALYTIC = True
YOLOV5_WEIGHTS = "./mmyolo/weights/yolov5/best.pt"

# 初始化 logging
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(levelname)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

class ConfigManager:
    """
    配置管理，統一處理 JSON 設定檔。
    """
    def __init__(self, json_path: str):
        self.json_path = json_path
        self.config_data = self._load_config()

    def _load_config(self) -> Dict[str, Any]:
        if not os.path.exists(self.json_path):
            raise FileNotFoundError(f"Configuration file not found: {self.json_path}")
        with open(self.json_path, 'r', encoding='utf-8') as f:
            return json.load(f)

    def get(self, key: str) -> Any:
        return self.config_data.get(key)


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

        acc_pwd = self.config_manager.get('Cameras_account_password')
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
    def __init__(self, config_manager: ConfigManager):
        self.config_manager = config_manager
        self.upload_lock = threading.Lock()
        self.uploaded_images = set()

    def determine_workstation_url(self, camera_ip: str) -> str:
        """
        根據 camera IP 從配置中取得對應工作站的上傳 URL。
        如未找到則記錄錯誤。
        """
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
        logging.error(f"IP {camera_ip} 無法匹配到任何工作站，請檢查 JSON 配置。")
        return None

    def upload_image(self, image_path: Path, camera_ip: str, total_cropped: int):
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
                data = {'total_cropped': total_cropped}
                response = requests.post(workstation_url, files=files, data=data, timeout=10)
                if response.status_code == 200:
                    logging.info(f"上傳成功: {image_path} from {camera_ip}")
                    try:
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
    def __init__(self, model, test_pipeline, uploader: ImageUploader, score_thr: float, out_dir: Path):
        self.model = model
        self.test_pipeline = test_pipeline
        self.uploader = uploader
        self.base_score_thr = score_thr   # 原本的 score_thr 改存成「白天的基準值」
        self.out_dir = out_dir

    def is_night_window(self, tz_name: str = 'Asia/Taipei') -> bool:
        """每天 18:00 ～ 隔日 06:00 視為夜間。"""
        tz = pytz.timezone(tz_name)
        now = datetime.datetime.now(tz).time()
        return (now >= datetime.time(18, 0)) or (now <= datetime.time(6, 0))

    def current_score_thr(self) -> float:
        """夜間用 0.9，其他時段用 base_score_thr。"""
        return 0.9 if self.is_night_window() else self.base_score_thr

    def run_inference(self, image_path: Path, camera_ip: str, base_filename: str):
        """
        執行偵測，回傳偵測到的框數量、偵測結果以及影像資料。
        不再於此進行裁切，裁切將統一以最佳影像的結果進行。
        """
        frame = mmcv.imread(str(image_path))

        if self.backend == 'mmdet':
            result = inference_detector(self.model, frame, self.test_pipeline)
            num_boxes = 0
            if hasattr(result, 'pred_instances'):
                valid = result.pred_instances['scores'] > self.current_score_thr()
                num_boxes = valid.sum().item()
            return num_boxes, result, frame

        # Ultralytic YOLOv5
        with torch.no_grad():
            yres = self.model(frame, size=getattr(self, 'yolo_imgsz', 640))
            pred = yres.xyxy[0]  # [N, 6] = x1,y1,x2,y2,conf,cls

        if pred is None or len(pred) == 0:
            empty_boxes = torch.empty((0, 4), dtype=torch.int32)
            empty_scores = torch.empty((0,), dtype=torch.float32)
            empty_labels = torch.empty((0,), dtype=torch.int64)
            result = SimpleNamespace(pred_instances={
                'bboxes': empty_boxes,
                'scores': empty_scores,
                'labels': empty_labels
            })
            return 0, result, frame

        pred = pred.detach().cpu()
        bboxes = pred[:, 0:4].round().to(torch.int32)
        scores = pred[:, 4]
        labels = pred[:, 5].to(torch.int64)
        result = SimpleNamespace(pred_instances={'bboxes': bboxes, 'scores': scores, 'labels': labels})
        num_boxes = int((scores > self.current_score_thr()).sum().item())
        return num_boxes, result, frame

    def crop_best_frame(self, frame, result, camera_ip: str, base_filename: str):
            """
            以最佳影像的偵測框來裁切目標圖像，並上傳裁切後的圖片。
            改為回傳啟動的上傳 thread 列表。
            """
            threads = []
            if hasattr(result, 'pred_instances'):
                valid_boxes = result.pred_instances['scores'] > self.current_score_thr()
                bboxes = result.pred_instances['bboxes'][valid_boxes].cpu().numpy().tolist()
                for idx, bbox in enumerate(bboxes):
                    x1, y1, x2, y2 = map(int, bbox)
                    cropped_image = frame[y1:y2, x1:x2]
                    cropped_filename = f"{base_filename}_cropped_{idx+1}.jpg"
                    cropped_save_path = self.out_dir / cropped_filename
                    cv2.imwrite(str(cropped_save_path), cropped_image)
                    # 建立上傳 thread 並加入列表
                    t = threading.Thread(
                        target=self.uploader.upload_image,
                        args=(cropped_save_path, camera_ip, len(bboxes))
                    )
                    t.start()
                    threads.append(t)
            return threads

    def save_best_frame_with_boxes(self, frame, result, camera_ip: str, base_filename: str):
        """
        在最佳框數的影像上標註框並保存，接著上傳到工作站。
        """
        if hasattr(result, 'pred_instances'):
            valid_boxes = result.pred_instances['scores'] > self.current_score_thr()
            bboxes = result.pred_instances['bboxes'][valid_boxes].cpu().numpy()
            for bbox in bboxes:
                x1, y1, x2, y2 = map(int, bbox)
                cv2.rectangle(frame, (x1, y1), (x2, y2), (0, 255, 0), 2)

        frame_filename = f"{base_filename}_with_boxes.jpg"
        frame_path = self.out_dir / camera_ip / frame_filename
        frame_path.parent.mkdir(parents=True, exist_ok=True)
        cv2.imwrite(str(frame_path), frame)

        # 上傳結果 (不帶 total_cropped 資訊)
        workstation_url = self.uploader.determine_workstation_url(camera_ip)
        try:
            with frame_path.open('rb') as f:
                response = requests.post(workstation_url, files={'file': f}, timeout=10)
                if response.status_code == 200:
                    logging.info(f"結果已成功上傳至工作站: {camera_ip}")
                else:
                    logging.error(f"結果上傳失敗: {camera_ip}: {response.text}")
        except Exception as e:
            logging.error(f"上傳結果時發生錯誤: {camera_ip}: {e}")


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
        self.input_dir = Path(args.folder)
        self.out_dir = Path(args.out_dir)
        self.group_size = args.group_size

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

    def clear_input_folder_if_needed(self, threshold=800):
        """
        若資料夾圖片數量超過 threshold，則清空資料夾。
        """
        image_files = list(self.input_dir.glob('*.[jp][pn]g')) + list(self.input_dir.glob('*.jpeg'))
        if len(image_files) > threshold:
            logging.info(f"檔案數量超過 {threshold}，清空資料夾 {self.input_dir}")
            for file in image_files:
                try:
                    file.unlink()
                    logging.info(f"刪除檔案: {file}")
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
        if not image_path.exists():
            logging.warning(f"新檔案 {image_path} 不存在，跳過處理。")
            return

        camera_ip = self.extract_camera_ip_from_filename(image_path.name)
        if camera_ip is None:
            logging.warning(f"無法從檔名中提取攝影機 IP：{image_path.name}")
            return

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
        if len(self.frames_by_camera[camera_ip]) >= self.group_size or current_wait >= max_wait_time:
            if current_wait >= max_wait_time:
                logging.info(f"等待時間 {current_wait:.0f} 秒超過理論上限 {max_wait_time} 秒，強制進行偵測。")
            self.process_detected_images_group(camera_ip)

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

    def process_detected_images_group(self, camera_ip: str):
        """
        處理該攝影機的一組影像 (group)：
        - 針對每張圖片執行偵測（不進行裁切）
        - 挑選出偵測框數最多的最佳影像
        - 以最佳影像的框進行裁切並上傳，同時保存標註框的完整影像
        - 若檔名包含 `_focus5min`，則執行 CameraController 縮放
        - 清空該組影像並重置狀態
        """
        try:
            # 清空過多的圖片
            self.clear_input_folder_if_needed()

            # 獲取存在的圖片
            existing_images = [img for img in self.frames_by_camera.get(camera_ip, []) if img.exists()]
            if not existing_images:
                logging.info(f"資料夾無可用圖片，重置 {camera_ip}")
                self.reset_camera_state(camera_ip)
                return

            # 若未達 group_size，則暫存影像等待更多
            if len(existing_images) < self.group_size:
                self.frames_by_camera[camera_ip] = existing_images
                return

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
            for image_path in existing_images:
                try:
                    num_boxes, result, frame = self.detector.run_inference(image_path, camera_ip, base_filename)
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
            if best_num_boxes > 0 and best_frame is not None and best_result is not None:
                try:
                    # 以最佳影像的偵測框進行裁切並上傳，並取得上傳 thread 列表
                    threads = self.detector.crop_best_frame(best_frame, best_result, camera_ip, base_filename)
                    # 等待所有裁切圖上傳完成
                    for t in threads:
                        t.join()

                    # 上傳最佳影像（加上標註框）的結果
                    self.detector.save_best_frame_with_boxes(best_frame, best_result, camera_ip, base_filename)

                    # 檢查是否需要執行焦點控制
                    focus_match = re.search(r'_focus(\d+)min', best_image_path.name)
                    if focus_match:
                        focus_time = int(focus_match.group(1))
                        logging.info(f"檔名包含焦點控制標記，進行縮放處理：{best_image_path.name}")
                        if hasattr(best_result, 'pred_instances'):
                            valid_mask = best_result.pred_instances['scores'] > self.detector.current_score_thr()
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
            for img_path in self.processed_images[camera_ip]:
                if img_path.exists():
                    try:
                        img_path.unlink()
                    except OSError as e:
                        logging.error(f"刪除影像失敗 {img_path}: {e}")

            # 重置攝影機狀態
            self.reset_camera_state(camera_ip)
            if camera_ip in self.first_frame_timestamp:
                del self.first_frame_timestamp[camera_ip]
        except Exception as e:
            logging.error(f"處理影像組 (camera_ip={camera_ip}) 時發生未預期的錯誤: {e}")

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
    parser = argparse.ArgumentParser(description='MMYOLO image folder demo')
    parser.add_argument('folder', help='監測的資料夾')
    parser.add_argument('config', help='Config檔案')
    parser.add_argument('checkpoint', help='權重檔案')
    parser.add_argument('--device', default='cuda:0', help='使用的裝置')
    parser.add_argument('--score-thr', type=float, default=0.6, help='Bbox score threshold')
    parser.add_argument('--out-dir', type=str, required=True, help='輸出資料夾')
    parser.add_argument('--group-size', type=int, default=10, help='每組影像數量')
    parser.add_argument('--json-path', type=str, default='./mmyolo/smoke_camera_config.json', help='JSON 配置檔路徑')
    return parser.parse_args()


def main():
    args = parse_args()
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    input_dir = Path(args.folder)
    input_dir.mkdir(parents=True, exist_ok=True)

    # 初始化
    config_manager = ConfigManager(args.json_path)
    camera_ctrl = CameraController(config_manager=config_manager)

    backend = 'mmdet'
    test_pipeline = None

    if FORCE_ULTRALYTIC:
        backend = 'yolov5'
        logging.info(f'Backend: YOLOv5 (forced), weights={YOLOV5_WEIGHTS}')
        # 以 Ultralytic YOLOv5 載入
        yolo = torch.hub.load('ultralytics/yolov5', 'custom', path=YOLOV5_WEIGHTS, verbose=False)
        dev = args.device if args.device else 'cpu'
        if 'cuda' in dev and torch.cuda.is_available():
            yolo.to(dev)
        else:
            yolo.to('cpu')
        model = yolo
    else:
        # 原 MMDet/MMYOLO 路徑
        model = init_detector(args.config, args.checkpoint, device=args.device)
        model.cfg.test_dataloader.dataset.pipeline[0].type = 'mmdet.LoadImageFromNDArray'
        test_pipeline = Compose(model.cfg.test_dataloader.dataset.pipeline)
        visualizer = VISUALIZERS.build(model.cfg.visualizer)
        visualizer.dataset_meta = model.dataset_meta

    uploader = ImageUploader(config_manager=config_manager)
    detector = DetectionProcessor(model, test_pipeline, uploader, args.score_thr, out_dir)
    detector.backend = backend  # 'yolov5' 或 'mmdet'
    detector.yolo_imgsz = 640

    event_handler = ImageHandler(args, config_manager, camera_ctrl, detector)

    observer = PollingObserver()
    observer.schedule(event_handler, str(input_dir), recursive=True)
    logging.info(f"開始監控資料夾: {input_dir}")
    observer.start()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        observer.stop()
    observer.join()


if __name__ == '__main__':
    main()
