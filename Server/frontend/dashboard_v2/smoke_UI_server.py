# -*- coding: utf-8 -*-
"""
smoke_UI_server.py — 前端頁面後端 API

- GET /ui/config：回「UI_config」並附帶 ETag（支援 If-None-Match，無變更則 304）
  * 參數 cameras=11,33,57 → 只回指定子集（仍用全域 ETag；有帶參數時會加 X-Partial 標頭）
- GET /ui/events：SSE 即時事件流
  * snapshot：初次連線時的快照事件
  * update：後續所有資料變更的通用事件 (config/status/image)
- POST 端點：
  * /config/send_groups                         { workstation_ip, polling_cameras, fixed_cameras, [port] }     
  * /config/send_setting                        { NVR_channel, function_switches{...} }
    ==> 修改設定會觸發【重啟鎖定】流程，API 回應會要求前端鎖定畫面。
  * /status/disconnection_process               { workstation_ip, disconnect_cameras }
  * /status/polling_cams_process                { workstation_ip, current_polling_cameras, [restarted] }
    ==> 工作站重啟後，需帶 restarted:true 標籤來解除鎖定。
  * /image/image_update                         { NVR_channel, base64 | image_path }
"""

from __future__ import annotations
import os, re, json, time, base64, binascii, logging, threading, queue, hashlib, math
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Set, Iterable
from copy import deepcopy
from collections import OrderedDict
from datetime import datetime, timezone, timedelta
import re

from flask import Flask, jsonify, request, render_template, redirect, url_for, make_response, stream_with_context, Response
from werkzeug.utils import secure_filename

# 第三方內部模組
from smoke_config.config_controller import ConfigDistributor
from smoke_config.distributed_config_store import DistributedConfigStore

# =========================
# 路徑與基本設定
# =========================
_HERE = Path(__file__).resolve().parent
_PROJECT_ROOT = _HERE.parents[2]  # base/
_DEFAULT_CFG = _PROJECT_ROOT / "smoke_config" / "smoke_total_camera_config.json"
_DEFAULT_DISTRIBUTED_CFG = _PROJECT_ROOT / "smoke_config" / "distributed_config"

def _is_distributed_config_dir(path: Path) -> bool:
    return (
        path.is_dir()
        and (path / "common_settings.json").is_file()
        and (path / "workstation_assignments.json").is_file()
        and (path / "camera_configs").is_dir()
    )

def _resolve_distributed_config_dir() -> Path:
    """
    解析拆分組態資料夾：
      1) 若設了 DISTRIBUTED_CONFIG_DIR 且資料夾完整 → 直接用
      2) 由當前檔案一路往上尋找 'smoke_config/distributed_config'
    """
    env_p = os.getenv("DISTRIBUTED_CONFIG_DIR")
    if env_p:
        p = Path(env_p).expanduser()
        if _is_distributed_config_dir(p):
            return p

    here = Path(__file__).resolve()
    for parent in [here.parent, *here.parents]:
        candidate = parent / "smoke_config" / "distributed_config"
        if _is_distributed_config_dir(candidate):
            return candidate

    tried = [
        str(parent / "smoke_config" / "distributed_config")
        for parent in [here.parent, *here.parents]
    ]
    raise FileNotFoundError("Cannot find distributed config directory. Tried:\n  " + "\n  ".join(tried))

def _resolve_optional_total_config_path() -> Optional[Path]:
    """
    Legacy total config path is optional at runtime. It is kept only for
    migration/debug compatibility and is not required to start the UI server.
    """
    env_p = os.getenv("TOTAL_CONFIG_PATH")
    if env_p:
        p = Path(env_p).expanduser()
        if p.is_file():
            return p
    if _DEFAULT_CFG.is_file():
        return _DEFAULT_CFG
    return None


DISTRIBUTED_CONFIG_DIR = str(_resolve_distributed_config_dir())
_legacy_total_config_path = _resolve_optional_total_config_path()
TOTAL_CONFIG_PATH = str(_legacy_total_config_path) if _legacy_total_config_path else ""
print(f"[smoke_UI_server] using DISTRIBUTED_CONFIG_DIR = {DISTRIBUTED_CONFIG_DIR}")
if TOTAL_CONFIG_PATH:
    print(f"[smoke_UI_server] optional legacy TOTAL_CONFIG_PATH = {TOTAL_CONFIG_PATH}")
DEFAULT_POLLING_TIME = int(os.environ.get("DEFAULT_POLLING_TIME", "15"))  # 分鐘（UI 刷新建議值）
HOST = os.environ.get("SMOKE_UI_HOST", "0.0.0.0")
# ⚠️ 關閉 reloader 預設值（避免多進程導致 TTL/ENV 不一致）
DEBUG = bool(int(os.environ.get("SMOKE_UI_DEBUG", "0")))
PORT = int(os.environ.get("SMOKE_UI_PORT", "58080"))

# Base64 圖片快取設定（ImageUploader → UI 預覽）
IMAGE_BASE64_TTL = int(os.environ.get("IMAGE_BASE64_TTL", "120"))        # 秒
IMAGE_BASE64_MAX_KB = int(os.environ.get("IMAGE_BASE64_MAX_KB", "300"))  # 單張大小上限 (KB)

# 模板/靜態目錄
TEMPLATE_DIR = str(_HERE / "templates")
STATIC_DIR = str(_HERE / "static")

# logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')

config_store = DistributedConfigStore(TOTAL_CONFIG_PATH or None, distributed_root=DISTRIBUTED_CONFIG_DIR)
distributor = ConfigDistributor(total_config_path=TOTAL_CONFIG_PATH, config_store=config_store)
app = Flask(__name__, template_folder=TEMPLATE_DIR, static_folder=STATIC_DIR)
app.config['JSON_AS_ASCII'] = False
app.config["JSON_SORT_KEYS"] = False
try:
    app.json.sort_keys = False
except Exception:
    pass

# =========================
# 內部狀態
# =========================
_file_lock = threading.Lock()
_change_seq = 1  # 取代舊 _ui_version：內部事件流水，不對外暴露

# 狀態：斷線/輪詢
_disconnection_map: Dict[str, List[int]] = {}
_current_polling_map: Dict[str, List[int]] = {}

# 【新】重啟鎖定機制狀態
_lock_state_lock = threading.Lock()
_pending_restart_ws: Set[str] = set()  # 用於存放等待重啟的工作站 IP
_system_lock_timestamp: Optional[float] = None  # 記錄系統進入鎖定狀態的時間點
LOCK_TIMEOUT_SECONDS = 600  # 鎖定超時時間 (10 分鐘)

def _status_remove_ids(ws_ip: str, ids: Iterable[int]) -> None:
    """從某工作站的即時狀態（current_polling / disconnect）中，移除指定相機 id。"""
    if not ids:
        return
    sids = {int(x) for x in ids if isinstance(x, (int, str)) and str(x).isdigit()}
    if ws_ip in _current_polling_map:
        _current_polling_map[ws_ip] = [c for c in (_current_polling_map.get(ws_ip) or []) if c not in sids]
    if ws_ip in _disconnection_map:
        _disconnection_map[ws_ip] = [c for c in (_disconnection_map.get(ws_ip) or []) if c not in sids]

def _status_clear_ws(ws_ip: str) -> None:
    """清空某工作站的即時狀態（用於『隱式整站搬遷』清來源站）。"""
    _current_polling_map.pop(ws_ip, None)
    _disconnection_map.pop(ws_ip, None)

# SSE Broker（簡易 Pub/Sub）
class SSEBroker:
    def __init__(self):
        self._clients = set()
        self._lock = threading.Lock()
        self._seq = 0

    def register(self):
        q = queue.Queue(maxsize=100)
        with self._lock:
            self._clients.add(q)
        return q

    def unregister(self, q):
        with self._lock:
            self._clients.discard(q)

    def publish(self, event: str, data: Dict[str, Any]):
        with self._lock:
            self._seq += 1
            seq = self._seq
            clients = list(self._clients)
        payload = {"id": seq, **data}
        msg = _format_sse(event=event, data=payload, id=str(seq))
        for q in clients:
            try:
                # 若滿了，丟掉最舊的一個
                if q.full():
                    q.get_nowait()
                q.put_nowait(msg)
            except Exception:
                pass

sse_broker = SSEBroker()

def _format_sse(event: str, data: Dict[str, Any], id: str = None) -> str:
    lines = []
    if id is not None:
        lines.append(f"id: {id}")
    if event:
        lines.append(f"event: {event}")
    # 單行 JSON
    js = json.dumps(data, ensure_ascii=False, separators=(',', ':'))
    for line in js.splitlines():
        lines.append(f"data: {line}")
    lines.append("")
    return "\n".join(lines) + "\n"

def _stable_dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(',', ':'), sort_keys=True)

def _compute_etag(ui_config: Dict[str, Any]) -> str:
    s = _stable_dumps(ui_config)
    return hashlib.sha256(s.encode('utf-8')).hexdigest()

def _image_cache_sig(now: float | None = None) -> str:
    now = now or time.time()
    live = []
    for cid, entry in _camera_b64.items():
        ts = float(entry.get("ts", 0) or 0.0)
        if now - ts <= IMAGE_BASE64_TTL:
            # len(data) 可避免同一秒重覆上傳但內容不同的情況
            live.append((int(cid), int(ts), len(str(entry.get("data") or ""))))
    live.sort()
    s = _stable_dumps(live)
    return hashlib.sha1(s.encode("utf-8")).hexdigest()

def _current_ui_and_etag() -> Tuple[Dict[str, Any], str]:
    with _file_lock:
        total_config = _load_total_config()
    ui = build_ui_config_from_total_config(total_config)
    # 把事件序號與圖片快取簽章一起納入 ETag 材料
    material = {
        "ui": ui,
        "_seq": int(_change_seq),
        "_img": _image_cache_sig()
    }
    etag = hashlib.sha256(_stable_dumps(material).encode("utf-8")).hexdigest()
    return ui, etag

# 每台相機最新的 Base64 預覽圖（只存最近一張）
# 結構：{ camera_id(int): {"data": data_url(str), "ts": epoch_seconds(float)} }
_camera_b64: Dict[int, Dict[str, Any]] = {}

# =========================
# 共用工具
# =========================
def _now_iso() -> str:
    return datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds")

def _load_json(path: str) -> Dict[str, Any]:
    if not os.path.exists(path):
        raise FileNotFoundError(f"Config file not found: {path}")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def _load_total_config() -> Dict[str, Any]:
    """Load a legacy-shaped total config from the distributed store."""
    return config_store.build_total_config(fallback_to_legacy=False)

def _compact_flat_int_lists_pretty(s: str) -> str:
    """
    把多行的「純數字 1D 陣列」壓成單行，括號內與逗點後保留空白。
    僅針對像 [1, 2, 3] 這類 1D 整數清單；不處理物件陣列或巢狀。
    """
    pattern = re.compile(r'\[\s*(?:\d+\s*,\s*)*\d+\s*\]', flags=re.DOTALL)
    def repl(m):
        nums = re.findall(r'\d+', m.group(0))
        return "[ " + ", ".join(nums) + " ]"
    return pattern.sub(repl, s)

def _atomic_write_json(path: str, data: Dict[str, Any]) -> None:
    # 正確：只用 mkstemp 建立臨時檔，避免先前同時使用 mkdtemp/mkstemp 的殘留
    import tempfile, os
    fd, tmp_path = tempfile.mkstemp(prefix=".__tmp__", suffix=".json", dir=str(Path(path).parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            s = json.dumps(data, ensure_ascii=False, indent=2)
            s = _compact_flat_int_lists_pretty(s)
            f.write(s)
        os.replace(tmp_path, path)  # 原子替換
    finally:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass

def _save_common_settings_from_total(total_config: Dict[str, Any]) -> None:
    common = deepcopy(total_config)
    common.pop("cameras", None)
    common.pop("workstation_ip_mapping", None)
    config_store.save_common_settings(common)

def _save_workstation_assignments_from_total(total_config: Dict[str, Any]) -> None:
    config_store.save_workstation_assignments(deepcopy(total_config.get("workstation_ip_mapping", {}) or {}))

def _save_camera_from_total(total_config: Dict[str, Any], channel_id: int) -> None:
    idx = _index_cameras_by_id(total_config.get("cameras", []) or [])
    cam = idx.get(int(channel_id))
    if not cam:
        raise FileNotFoundError(f"Camera not found: NVR_channel={channel_id}")
    config_store.save_camera_config(int(channel_id), deepcopy(cam))

def _response_ok(message: str = "操作成功", data: Any = None, http_status: int = 200, headers: Dict[str,str]=None, code: str = "OK"):
    """成功回應統一格式。"""
    payload = {"code": code, "message": message}
    if data is not None:
        payload["data"] = data
    resp = make_response(jsonify(payload), http_status)
    if headers:
        for k,v in headers.items():
            resp.headers[k] = v
    return resp

def _response_err(code: str, message: str, http_status=400, **detail):
    """錯誤回應統一格式。"""
    error_payload = {"code": code, "message": message, **detail}
    return jsonify(error_payload), http_status

def _to_int_id(x) -> Optional[int]:
    try:
        return int(x)
    except (ValueError, TypeError):
        return None
    
def _form_get_bool(name: str) -> Optional[bool]:
    if name not in request.form:
        return None
    v = request.form.get(name).strip().lower()
    if v in ("1","true","yes","y","on"): return True
    if v in ("0","false","no","n","off"): return False
    return None

def _form_get_int(name: str) -> Optional[int]:
    if name not in request.form:
        return None
    try:
        return int(request.form.get(name).strip())
    except Exception:
        return None

def _form_get_float(name: str) -> Optional[float]:
    if name not in request.form:
        return None
    try:
        return float(request.form.get(name).strip())
    except Exception:
        return None

def _payload_from_form_receive_groups() -> Dict[str, Any]:
    """
    支援 form-data/x-www-form-urlencoded 的 receive_groups 格式：
    workstation_ip=192.168.1.10:58090
    polling_cameras=1,2,3
    fixed_cameras=4,5
    port=58090
    """
    ws = request.form.get("workstation_ip", "").strip()
    payload: Dict[str, Any] = {}
    if ws:
        payload["workstation_ip"] = ws

    def parse_id_list(s: str) -> List[int]:
        out = []
        for t in re.split(r"[,\s]+", s.strip()):
            if not t: continue
            try:
                out.append(int(t))
            except Exception:
                pass
        return out

    if "polling_cameras" in request.form:
        payload["polling_cameras"] = parse_id_list(request.form.get("polling_cameras"))
    if "fixed_cameras" in request.form:
        payload["fixed_cameras"] = parse_id_list(request.form.get("fixed_cameras"))
    if "port" in request.form:
        try:
            payload["port"] = int(request.form.get("port"))
        except Exception:
            pass
    return payload

def _payload_from_form_receive_setting() -> Dict[str, Any]:
    """
    支援 form-data/x-www-form-urlencoded 的 receive_setting 格式：
    NVR_channel=1
    function_switches.stage1_confidence=0.8
    function_switches.stage2_confidence=0.9
    function_switches.focus_time=10
    function_switches.record_time=10
    function_switches.patrol_time=5
    ...
    也支援頂層 patrol_time（若仍沿用）
    """
    nvr = _form_get_int("NVR_channel")
    if nvr is None:
        return {}
    fs: Dict[str, Any] = {}

    # 解析 function_switches.*
    for k in list(request.form.keys()):
        if not k.startswith("function_switches."):
            continue
        nk = k.split(".", 1)[1]
        v = request.form.get(k)
        if nk in ("enable_upload_images","enable_google_upload","enable_smoke_notify",
                    "enable_nvr_recording","enable_camera_ptz","enable_night_detection",
                    "adjust_camera_zoom_on_detection","stage1_detection","stage2_detection"):
            b = _form_get_bool(k)
            if b is not None: fs[nk] = b
        elif nk in ("focus_time","record_time","patrol_time"):
            iv = _form_get_int(k)
            if iv is not None: fs[nk] = iv
        elif nk == "reference_distance_threshold_px":
            fs[nk] = v.strip() if v and v.strip() else None
        elif nk in ("stage1_confidence","stage2_confidence"):
            fv = _form_get_float(k)
            if fv is not None: fs[nk] = fv
        else:
            if v is not None and v.strip():
                fs[nk] = v.strip()

    # 夜間時段（字串）
    for k in ("night_detection_start","night_detection_end"):
        if k in request.form and request.form.get(k).strip():
            fs[k] = request.form.get(k).strip()
    payload: Dict[str, Any] = {"NVR_channel": nvr}
    if fs:
        payload["function_switches"] = fs
    # 頂層 patrol_time（若你仍想沿用）
    pt = _form_get_int("patrol_time")
    if pt is not None:
        payload["patrol_time"] = pt
    return payload

def _index_cameras_by_id(cameras: List[Dict[str, Any]]) -> Dict[int, Dict[str, Any]]:
    idx = {}
    for cam in cameras:
        cid = _to_int_id(cam.get("NVR_channel"))
        if cid is not None:
            idx[cid] = cam
    return idx

def _ensure_cameras_nvr_channel_str(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """將 cfg['cameras'][i]['NVR_channel'] 強制轉為字串，確保存檔格式一致。"""
    cams = cfg.get("cameras", []) or []
    for cam in cams:
        if "NVR_channel" in cam:
            try:
                cam["NVR_channel"] = str(int(cam["NVR_channel"]))
            except Exception:
                cam["NVR_channel"] = str(cam["NVR_channel"])
    return cfg

def _next_change_seq() -> int:
    global _change_seq
    _change_seq += 1
    return _change_seq

# -- 工作站 IP 正規化：支援 "127.0.0.1:58090" 輸入 --------------------------
def _normalize_ws_ip_port(ws: str) -> Tuple[str, Optional[int]]:
    """
    將 workstation_ip 正規化為 (host, port)：
    - 僅處理「單一冒號且 port 為整數」的常見情況（避免與 IPv6 混淆）。
    - 其餘狀況回傳 (原字串, None)。
    """
    if not isinstance(ws, str):
        return str(ws), None
    if ":" in ws and ws.count(":") == 1 and not ws.startswith("["):
        host, maybe_port = ws.split(":", 1)
        try:
            return host, int(maybe_port)
        except Exception:
            return host, None
    return ws, None

def _ws_entry_to_sets(entry):
    """
    將工作站 entry（舊版 list / 新版 dict 皆可）轉為 (polling_set, fixed_set, union_set)。
    舊版 list 視為 polling。
    """
    if isinstance(entry, dict):
        p = set(entry.get("polling_cameras") or [])
        f = set(entry.get("fixed_cameras") or [])
    else:  # 舊格式 list
        p, f = set(entry or []), set()
    return p, f, (p | f)


def _infer_implicit_transfer_superset(target_ip, target_polling, target_fixed, ws_map):
    """
    嘗試推論『隱式整站搬遷（超集合）』來源：
    moving = 這次要掛到 target_ip 的全集（polling ∪ fixed）。
    成立條件：
      1) 所有「造成跨站重複」的相機，都只出現在『同一台』來源 A；
      2) A 的全部相機集合 A_union ⊆ moving（moving 可以比 A_union 大，也就是可同時新增相機）；
      3) moving - A_union（新增的相機）不可被 A 與 target 以外的其他站持有。

    回傳：
      (from_ip, extras_conflicts_set)
        - from_ip：若能推論出來源 A，否則 None；
        - extras_conflicts_set：moving 的新增相機若被其他站佔用，回傳衝突集合（非空 → 不放行）。
    """
    moving = set(target_polling or []) | set(target_fixed or [])
    if not moving:
        return None, set()

    # 蒐集所有『造成重複』的 owner
    owners = {}
    for ip, entry in (ws_map or {}).items():
        if ip == target_ip:
            continue
        _, _, u = _ws_entry_to_sets(entry)
        inter = moving & u
        if inter:
            owners[ip] = inter

    # 重複只能來自同一台 A
    if len(owners) != 1:
        return None, set()

    from_ip = next(iter(owners.keys()))
    _, _, a_union = _ws_entry_to_sets(ws_map[from_ip])

    # A_union 必須被 moving 完整包含（超集合）
    if not a_union.issubset(moving):
        return None, set()

    # 檢查新增相機（extras）是否被其他站持有
    extras = moving - a_union
    extras_conflicts = set()
    if extras:
        for ip, entry in (ws_map or {}).items():
            if ip in (target_ip, from_ip):
                continue
            _, _, u = _ws_entry_to_sets(entry)
            conflict = extras & u
            if conflict:
                extras_conflicts |= conflict

    return from_ip, extras_conflicts


# =========================
# UI_config 產生（Total → UI）
# =========================
def build_ui_config_from_total_config(total_config: Dict[str, Any]) -> Dict[str, Any]:
    """
    生成最終 UI_config（固定鍵序、僅三鍵）：
      1) workstation_ip_mapping
      2) polling_time
      3) cameras（含 base64）
    ※ 前端輸出統一：NVR_channel 為 int；function_switches 內的時間單位為「分鐘」。
    """
    # 1) cameras
    cameras_out = []
    now_ts = time.time()
    for cam in total_config.get("cameras", []):
        c = deepcopy(cam)
        cid = _to_int_id(c.get("NVR_channel"))
        if cid is None:
            continue

        # 取最近一張預覽圖（TTL 內）
        b64 = ""
        entry = _camera_b64.get(cid)
        if entry and (now_ts - float(entry.get("ts", 0))) <= IMAGE_BASE64_TTL:
            b64 = str(entry.get("data") or "")

        out = {
            "picture": b64,
            "location": c.get("location"),
            "NVR_channel": cid,
            "function_switches": {}
        }

        fs_in = c.get("function_switches", {}) or {}
        fs_out = deepcopy(fs_in)

        # 相機層 → function_switches（給前端好編輯）
        if "stage1_confidence" in c:
            fs_out["stage1_confidence"] = c["stage1_confidence"]
        if "stage2_confidence" in c:
            fs_out["stage2_confidence"] = c["stage2_confidence"]
        fs_out["reference_distance_threshold_px"] = c.get("reference_distance_threshold_px")
        if "patrol_time" in c:
            fs_out["patrol_time"] = c["patrol_time"]

        out["function_switches"] = fs_out
        cameras_out.append(out)

    # 2) workstation_ip_mapping：容忍舊格式 list，一律轉成 dict 形態
    ws_map_raw = deepcopy(total_config.get("workstation_ip_mapping", {}) or {})
    ws_map = {}
    for ws_ip, val in ws_map_raw.items():
        if isinstance(val, dict):
            ws_map[ws_ip] = val
        elif isinstance(val, list):
            ws_map[ws_ip] = {"polling_cameras": val, "fixed_cameras": []}
        else:
            ws_map[ws_ip] = {"polling_cameras": [], "fixed_cameras": []}

    # 貼上狀態欄位
    for ws_ip in list(ws_map.keys()):
        if ws_ip in _disconnection_map:
            ws_map[ws_ip]["disconnect_cameras"] = _disconnection_map[ws_ip]
        if ws_ip in _current_polling_map:
            ws_map[ws_ip]["current_polling_cameras"] = _current_polling_map[ws_ip]

    # 3) Ordered 固定鍵序
    ui = OrderedDict()
    ui["workstation_ip_mapping"] = ws_map
    try:
        ui["polling_time"] = int((total_config or {}).get("polling_time", DEFAULT_POLLING_TIME))
    except Exception:
        ui["polling_time"] = DEFAULT_POLLING_TIME
    ui["cameras"] = cameras_out
    return ui

# =========================
# 驗證/套用：單筆寫入（沿用）
# =========================
def _validate_single_setting(item: Dict[str, Any]) -> Tuple[bool, str]:
    """
    驗證單台相機設定。
    """
    if not isinstance(item, dict):
        return False, "setting item must be an object."
    if "NVR_channel" not in item:
        return False, "item.NVR_channel is required."

    cid = _to_int_id(item.get("NVR_channel"))
    if cid is None:
        return False, "NVR_channel must be an integer."

    fs = item.get("function_switches", {})
    if fs is not None and not isinstance(fs, dict):
        return False, "function_switches must be an object."

    def _validate_nonneg_int(x, name):
        if x is None: return True, ""
        try:
            iv = int(x)
            if iv < 0: return False, f"{name} must be >= 0."
            return True, ""
        except Exception:
            return False, f"{name} must be integer."

    def _validate_01_float(x, name):
        if x is None: return True, ""
        try:
            fv = float(x)
            if fv < 0 or fv > 1: return False, f"{name} must be in [0,1]."
            return True, ""
        except Exception:
            return False, f"{name} must be float."

    if isinstance(fs, dict):
        threshold = fs.get("reference_distance_threshold_px")
        if threshold is not None:
            try:
                value = float(threshold)
                if isinstance(threshold, bool) or not math.isfinite(value) or value <= 0:
                    return False, "reference_distance_threshold_px must be a positive finite number or null."
            except (TypeError, ValueError):
                return False, "reference_distance_threshold_px must be a positive finite number or null."
        ok, msg = _validate_nonneg_int(fs.get("focus_time"), "focus_time");  
        if not ok: return False, msg
        ok, msg = _validate_nonneg_int(fs.get("record_time"), "record_time"); 
        if not ok: return False, msg
        ok, msg = _validate_nonneg_int(fs.get("patrol_time"), "patrol_time");
        if not ok: return False, msg
        ok, msg = _validate_01_float(fs.get("stage1_confidence"), "stage1_confidence"); 
        if not ok: return False, msg
        ok, msg = _validate_01_float(fs.get("stage2_confidence"), "stage2_confidence");
        if not ok: return False, msg
        for sk in ("night_detection_start", "night_detection_end"):
            v = fs.get(sk)
            if v is not None:
                if not (isinstance(v, str) and re.match(r"^\d{2}:\d{2}$", v)):
                    return False, f"{sk} must be 'HH:MM'"

    return True, ""

def _apply_single_setting(total_config: Dict[str, Any], item: Dict[str, Any]) -> Dict[str, Any]:
    """
    實際寫入單台相機設定（部分更新）。
    - function_switches.* 會寫在 function_switches
    - stage1/2_confidence 與 patrol_time 會同步在相機層（方便 UI 顯示與後端使用）
    - 【新】根據 stage2_detection 的布林值，更新頂層的 stage2_cams 列表。
    """
    cfg = deepcopy(total_config)
    cams: List[Dict[str, Any]] = cfg.get("cameras", []) or []

    cid = _to_int_id(item.get("NVR_channel"))
    idx_map = _index_cameras_by_id(cams)
    cam_obj = idx_map.get(cid)
    if not cam_obj:
        raise FileNotFoundError(f"Camera not found: NVR_channel={cid}")

    in_fs = item.get("function_switches", {}) or {}
    fs = cam_obj.get("function_switches", {}) or {}

    # 布林旗標
    for bkey in ["enable_upload_images","enable_google_upload","enable_smoke_notify",
                "enable_nvr_recording","enable_camera_ptz","enable_night_detection",
                "adjust_camera_zoom_on_detection","stage1_detection","stage2_detection"]:
        if bkey in in_fs:
            fs[bkey] = bool(in_fs[bkey])

    # 分鐘類型
    for nkey in ["focus_time", "record_time"]:
        if nkey in in_fs:
            fs[nkey] = int(in_fs[nkey])

    for sk in ["night_detection_start", "night_detection_end"]:
        if sk in in_fs and isinstance(in_fs[sk], str):
            v = in_fs[sk].strip()
            if re.match(r"^\d{2}:\d{2}$", v):  # 簡單 HH:MM 格式
                fs[sk] = v

    # 信心值寫回攝影機層
    if "stage1_confidence" in in_fs:
        cam_obj["stage1_confidence"] = float(in_fs["stage1_confidence"])
    if "stage2_confidence" in in_fs:
        cam_obj["stage2_confidence"] = float(in_fs["stage2_confidence"])
    if "reference_distance_threshold_px" in in_fs:
        threshold = in_fs["reference_distance_threshold_px"]
        if threshold is None:
            cam_obj.pop("reference_distance_threshold_px", None)
        else:
            cam_obj["reference_distance_threshold_px"] = float(threshold)
        fs.pop("reference_distance_threshold_px", None)

    # 巡弋時間（分鐘）：僅接受從 function_switches 傳入；實際儲存在相機層
    if "patrol_time" in in_fs:
        cam_obj["patrol_time"] = int(in_fs["patrol_time"])

    cam_obj["function_switches"] = fs

    # 【新】根據 stage2_detection 的值，更新頂層的 stage2_cams 列表
    if "stage2_detection" in in_fs:
        # 使用 set 處理，可避免重複且易於增刪
        stage2_cams = set(cfg.get("stage2_cams", []) or [])
        
        if in_fs["stage2_detection"] is True:
            stage2_cams.add(cid)
        elif in_fs["stage2_detection"] is False:
            stage2_cams.discard(cid) # discard 不會在元素不存在時報錯
        
        # 寫回設定檔前，轉為排序後的 list，確保存檔格式穩定
        cfg["stage2_cams"] = sorted(list(stage2_cams))

    return cfg

def _camera_signature(cam: Dict[str, Any]) -> tuple:
    """
    擷取相機設定的「可比較簽章」，用來判斷是否有實質變更。
    僅取會被 send_setting 影響的欄位：
      - function_switches（排序後的 (k, v) tuple）
      - stage1_confidence, stage2_confidence, patrol_time, reference_distance_threshold_px
    """
    if not isinstance(cam, dict):
        return ()
    fs = cam.get("function_switches", {}) or {}
    fs_norm = tuple(sorted((str(k), fs[k]) for k in fs.keys()))
    s1 = cam.get("stage1_confidence")
    s2 = cam.get("stage2_confidence")
    pt = cam.get("patrol_time")
    return ("fs", fs_norm, "s1", s1, "s2", s2, "pt", pt,
            "reference_distance_threshold_px", cam.get("reference_distance_threshold_px"))

def _validate_groups_single(payload: Dict[str, Any], total_config: Dict[str, Any]) -> Tuple[bool, str]:
    """
    驗證單站分組：
      • 同時帶 polling + fixed → 完整覆蓋（原行為）
      • 只帶 polling → 移動/追加到 polling，並從 fixed 移除該等相機（不清空原 polling）
      • 只帶 fixed   → 移動/追加到 fixed，並從 polling 移除該等相機（不清空原 fixed）
      • 保留隱式整站搬遷（超集合）與單相機搬遷（per-camera reassign）
    """
    if not isinstance(payload, dict):
        return False, "payload must be an object."
    if "workstation_ip" not in payload:
        return False, "payload.workstation_ip is required."

    ws_host, _ = _normalize_ws_ip_port(str(payload.get("workstation_ip")))
    _raw_p = payload.get("polling_cameras", None)
    _raw_f = payload.get("fixed_cameras", None)
    allow_clear = bool(payload.get("_allow_clear", False))  # 預設不允許空陣列當「清空」指令

    has_polling = (_raw_p is not None) and (allow_clear or len(_raw_p) > 0)
    has_fixed   = (_raw_f is not None) and (allow_clear or len(_raw_f) > 0)

    polling_in = payload.get("polling_cameras", []) or []
    fixed_in   = payload.get("fixed_cameras", []) or []

    # 驗相機 id 並轉成 int（去除無效）
    cams_idx = set(_index_cameras_by_id(total_config.get("cameras", [])).keys())
    def _to_int_list_safe(arr):
        out = []
        for x in arr or []:
            cid = _to_int_id(x)
            if cid is None or cid not in cams_idx:
                raise ValueError(f"Unknown or invalid camera id: {x}")
            out.append(cid)
        return out
    try:
        polling_ids_in = _to_int_list_safe(polling_in)
        fixed_ids_in   = _to_int_list_safe(fixed_in)
    except ValueError as e:
        return False, str(e)

    # 讀現況
    ws_map_cur = deepcopy(total_config.get("workstation_ip_mapping", {}) or {})
    cur_entry  = ws_map_cur.get(ws_host, {})
    if isinstance(cur_entry, list):
        cur_p, cur_f = [c for c in [_to_int_id(x) for x in cur_entry] if c is not None], []
    elif isinstance(cur_entry, dict):
        cur_p = [c for c in [_to_int_id(x) for x in (cur_entry.get("polling_cameras") or [])] if c is not None]
        cur_f = [c for c in [_to_int_id(x) for x in (cur_entry.get("fixed_cameras")   or [])] if c is not None]
    else:
        cur_p, cur_f = [], []

    # 計算「此次生效」之目標清單（覆蓋 or PATCH＝移動/追加）
    if has_polling and has_fixed:
        # 現況
        oldP = list(dict.fromkeys(cur_p))
        oldF = list(dict.fromkeys(cur_f))
        oldU = set(oldP) | set(oldF)

        # 本次輸入
        P_in = list(dict.fromkeys(polling_ids_in))
        F_in = list(dict.fromkeys(fixed_ids_in))
        U_in = set(P_in) | set(F_in)

        if not P_in and not F_in:
            # 兩欄同時為空 → 覆蓋為空（清空）
            tgt_p, tgt_f = [], []
        elif U_in.issuperset(oldU):
            # 給了完整集合 → 覆蓋（fixed 優先）
            tgt_f = F_in[:]
            tgt_p = [c for c in P_in if c not in set(tgt_f)]
        else:
            # 雙向 PATCH（以現況為基礎移動/追加）
            # 1) newF = oldF ∪ F_in
            newF = oldF[:]
            for c in F_in:
                if c not in newF:
                    newF.append(c)
            # 2) 若某 id 僅在 P_in（不在 F_in），表示 fixed→polling，需自 newF 移除
            for c in P_in:
                if c not in F_in and c in newF:
                    newF = [x for x in newF if x != c]
            # 3) newP = (oldP ∪ P_in) - newF（fixed 優先）
            newP = oldP[:]
            for c in P_in:
                if c not in newP:
                    newP.append(c)
            newP = [x for x in newP if x not in set(newF)]

            tgt_p, tgt_f = newP, newF
    elif has_polling and not has_fixed:
        # 移動/追加到 polling；從 fixed 移除相同 id
        tgt_p = list(dict.fromkeys(cur_p + [c for c in polling_ids_in if c not in cur_p]))
        tgt_f = [c for c in cur_f if c not in set(polling_ids_in)]
    elif has_fixed and not has_polling:
        # 移動/追加到 fixed；從 polling 移除相同 id
        tgt_f = list(dict.fromkeys(cur_f + [c for c in fixed_ids_in if c not in cur_f]))
        tgt_p = [c for c in cur_p if c not in set(fixed_ids_in)]
    else:
        return False, "At least one of polling_cameras or fixed_cameras must be provided."

    # 構建暫存 map 供重複檢查
    def _entry_union_ids(entry: Any) -> set[int]:
        if isinstance(entry, dict):
            arr = (entry.get("polling_cameras") or []) + (entry.get("fixed_cameras") or [])
        elif isinstance(entry, list):
            arr = entry
        else:
            arr = []
        s = set()
        for z in arr:
            cid = _to_int_id(z)
            if cid is not None:
                s.add(cid)
        return s

    ws_map_new = deepcopy(ws_map_cur)
    new_entry = ws_map_new.get(ws_host, {})
    if isinstance(new_entry, list):
        new_entry = {"polling_cameras": new_entry, "fixed_cameras": []}
    elif not isinstance(new_entry, dict):
        new_entry = {"polling_cameras": [], "fixed_cameras": []}
    new_entry["polling_cameras"] = tgt_p
    new_entry["fixed_cameras"]   = tgt_f
    ws_map_new[ws_host] = new_entry

    # 掃描跨站重複
    seen: dict[int, str] = {}
    dup: set[int] = set()
    for ip, entry in ws_map_new.items():
        for cid in _entry_union_ids(entry):
            if cid in seen and seen[cid] != ip:
                dup.add(cid)
            else:
                seen[cid] = ip

    if dup:
        moving_set = set(tgt_p) | set(tgt_f)

        # 先嘗試：隱式整站搬遷（超集合）
        from_ip, extras_conflicts = _infer_implicit_transfer_superset(
            ws_host, tgt_p, tgt_f, total_config.get("workstation_ip_mapping") or {}
        )
        if from_ip and not extras_conflicts:
            payload["_implicit_transfer_from"] = from_ip
            payload["_implicit_transfer_mode"] = "superset"
            return True, ""
        elif from_ip and extras_conflicts:
            return False, f"extras owned by other workstations: {sorted(list(extras_conflicts))}"

        # 再嘗試：隱式單相機/少量相機搬遷（per-camera）
        owners: dict[str, set[int]] = {}
        for ip, entry in (total_config.get("workstation_ip_mapping") or {}).items():
            if ip == ws_host:
                continue
            inter = moving_set & _entry_union_ids(entry)
            if inter:
                owners[ip] = inter
        if len(owners) == 1:
            from_ip2, dup_set = next(iter(owners.items()))
            dup_set = set(dup_set)
            extras  = moving_set - dup_set
            extras_conflicts2: set[int] = set()
            if extras:
                for ip, entry in (total_config.get("workstation_ip_mapping") or {}).items():
                    if ip in (from_ip2, ws_host):
                        continue
                    conflict = extras & _entry_union_ids(entry)
                    if conflict:
                        extras_conflicts2 |= conflict
            if not extras_conflicts2:
                payload["_implicit_reassign_from"]    = from_ip2
                payload["_implicit_reassign_cameras"] = sorted(list(dup_set))
                return True, ""
            else:
                return False, f"extras owned by other workstations: {sorted(list(extras_conflicts2))}"

        return False, f"Duplicated camera ids across workstations: {sorted(list(dup))}"

    return True, ""


def _apply_groups_single(total_config: Dict[str, Any], payload: Dict[str, Any]) -> Dict[str, Any]:
    """實際寫入單站分組；支援：
       • 完整覆蓋（同時帶兩清單）
       • 單欄位 PATCH＝移動/追加語意
       • 隱式整站搬遷（超集合）與隱式單相機搬遷（per-camera）
    """
    ws_host, ws_port = _normalize_ws_ip_port(str(payload.get("workstation_ip")))
    _raw_p = payload.get("polling_cameras", None)
    _raw_f = payload.get("fixed_cameras", None)
    allow_clear = bool(payload.get("_allow_clear", False))  # 預設不允許空陣列當「清空」指令

    has_polling = (_raw_p is not None) and (allow_clear or len(_raw_p) > 0)
    has_fixed   = (_raw_f is not None) and (allow_clear or len(_raw_f) > 0)

    polling_in = payload.get("polling_cameras", []) or []
    fixed_in   = payload.get("fixed_cameras", []) or []

    # 讀現況
    cfg    = deepcopy(total_config)
    ws_map = deepcopy(cfg.get("workstation_ip_mapping", {}) or {})
    cur_entry = ws_map.get(ws_host, {})
    if isinstance(cur_entry, list):
        cur_p, cur_f, cur_port = [c for c in [_to_int_id(x) for x in cur_entry] if c is not None], [], None
    elif isinstance(cur_entry, dict):
        cur_p   = [c for c in [_to_int_id(x) for x in (cur_entry.get("polling_cameras") or [])] if c is not None]
        cur_f   = [c for c in [_to_int_id(x) for x in (cur_entry.get("fixed_cameras")   or [])] if c is not None]
        cur_port = cur_entry.get("port")
    else:
        cur_p, cur_f, cur_port = [], [], None

    def _uniq_int_list(arr): return list(dict.fromkeys([_to_int_id(x) for x in (arr or []) if _to_int_id(x) is not None]))
    polling_ids_in = _uniq_int_list(polling_in)
    fixed_ids_in   = _uniq_int_list(fixed_in)

    # 決定目標清單
    if has_polling and has_fixed:
        oldP = list(dict.fromkeys(cur_p))
        oldF = list(dict.fromkeys(cur_f))
        oldU = set(oldP) | set(oldF)

        P_in = list(dict.fromkeys(polling_ids_in))
        F_in = list(dict.fromkeys(fixed_ids_in))
        U_in = set(P_in) | set(F_in)

        if not P_in and not F_in:
            # 覆蓋為空
            tgt_p, tgt_f = [], []
        elif U_in.issuperset(oldU):
            # 覆蓋（fixed 優先）
            tgt_f = F_in[:]
            tgt_p = [c for c in P_in if c not in set(tgt_f)]
        else:
            # 雙向 PATCH
            newF = oldF[:]
            for c in F_in:
                if c not in newF:
                    newF.append(c)
            for c in P_in:
                if c not in F_in and c in newF:
                    newF = [x for x in newF if x != c]
            newP = oldP[:]
            for c in P_in:
                if c not in newP:
                    newP.append(c)
            newP = [x for x in newP if x not in set(newF)]
            tgt_p, tgt_f = newP, newF
    elif has_polling and not has_fixed:
        tgt_p = list(dict.fromkeys(cur_p + [c for c in polling_ids_in if c not in cur_p]))
        tgt_f = [c for c in cur_f if c not in set(polling_ids_in)]
    elif has_fixed and not has_polling:
        tgt_f = list(dict.fromkeys(cur_f + [c for c in fixed_ids_in if c not in cur_f]))
        tgt_p = [c for c in cur_p if c not in set(fixed_ids_in)]
    else:
        tgt_p, tgt_f = cur_p, cur_f  # 理論上不會到這（validate 已擋）
        
    # 清除：凡這次被「移出 polling」的 id，都把該站的即時狀態一併移除
    _removed_from_polling = set(cur_p) - set(tgt_p)
    if _removed_from_polling:
        _status_remove_ids(ws_host, _removed_from_polling)


    # ---- 路徑一：隱式整站搬遷（超集合）----
    from_ip = payload.get("_implicit_transfer_from")
    if from_ip and from_ip != ws_host:
        target_existing = ws_map.get(ws_host, {})
        source_existing = ws_map.get(from_ip,  {})
        def _safe_int(x):
            try: return int(x)
            except Exception: return None

        # port 優先序：IP:port > body.port > 目標既有 > 來源
        if ws_port:
            final_port = ws_port
        else:
            body_port = _safe_int(payload.get("port")) if ("port" in payload) else None
            tgt_port  = target_existing.get("port") if isinstance(target_existing, dict) else None
            src_port  = source_existing.get("port")  if isinstance(source_existing, dict)  else None
            final_port = body_port if body_port is not None else (tgt_port if tgt_port is not None else src_port)

        src_entry = ws_map.get(from_ip, {})
        kept_port = src_entry.get("port") if isinstance(src_entry, dict) else None
        _status_clear_ws(from_ip)  
        ws_map[from_ip] = {
            "polling_cameras": [],
            "fixed_cameras": [],
            "current_polling_cameras": [],
            "disconnect_cameras": []
        }
        if kept_port is not None:
            ws_map[from_ip]["port"] = kept_port

        entry = {"polling_cameras": tgt_p, "fixed_cameras": tgt_f}
        if final_port is not None:
            entry["port"] = int(final_port)
        ws_map[ws_host] = entry
        cfg["workstation_ip_mapping"] = ws_map
        return cfg

    # ---- 路徑二：隱式單相機/少量相機搬遷（per-camera）----
    from_ip2     = payload.get("_implicit_reassign_from")
    reassign_ids = set(payload.get("_implicit_reassign_cameras") or [])
    if from_ip2 and reassign_ids and from_ip2 != ws_host:
        src_entry = ws_map.get(from_ip2, {})
        if isinstance(src_entry, list):
            src_entry = {"polling_cameras": src_entry, "fixed_cameras": []}
        elif not isinstance(src_entry, dict):
            src_entry = {"polling_cameras": [], "fixed_cameras": []}
        src_entry["polling_cameras"] = [c for c in (src_entry.get("polling_cameras") or []) if _to_int_id(c) not in reassign_ids]
        src_entry["fixed_cameras"]   = [c for c in (src_entry.get("fixed_cameras")   or []) if _to_int_id(c) not in reassign_ids]
        src_entry["current_polling_cameras"] = [c for c in (src_entry.get("current_polling_cameras") or []) if _to_int_id(c) not in reassign_ids]
        src_entry["disconnect_cameras"]      = [c for c in (src_entry.get("disconnect_cameras")      or []) if _to_int_id(c) not in reassign_ids]
        ws_map[from_ip2] = src_entry
        _status_remove_ids(from_ip2, reassign_ids)  # ★ 清 in-memory

    # ---- 路徑三：一般寫回（覆蓋或 PATCH 移動/追加）----
    new_entry = ws_map.get(ws_host, {})
    if isinstance(new_entry, list):
        new_entry = {"polling_cameras": new_entry, "fixed_cameras": []}
    elif not isinstance(new_entry, dict):
        new_entry = {"polling_cameras": [], "fixed_cameras": []}
    if _removed_from_polling:
        def _flt(lst):
            return [ _to_int_id(x) for x in (lst or []) if (_to_int_id(x) is not None and _to_int_id(x) not in _removed_from_polling) ]
        new_entry["current_polling_cameras"] = _flt(new_entry.get("current_polling_cameras"))
        new_entry["disconnect_cameras"]      = _flt(new_entry.get("disconnect_cameras"))
        _status_remove_ids(ws_host, _removed_from_polling)  # ★ 清 in-memory（同站）
    new_entry["polling_cameras"] = tgt_p
    new_entry["fixed_cameras"]   = tgt_f

    # port：IP:port > body.port > 既有
    if ws_port:
        new_entry["port"] = ws_port
    elif "port" in payload:
        try: new_entry["port"] = int(payload["port"])
        except Exception: pass
    elif cur_port is not None:
        new_entry.setdefault("port", cur_port)

    ws_map[ws_host] = new_entry
    cfg["workstation_ip_mapping"] = ws_map
    return cfg


# =========================
# 【新】背景鎖定超時監控
# =========================
def _lock_timeout_monitor():
    """
    背景執行緒，定期檢查 UI 鎖定是否超時。
    """
    # 【修正】使用 global 關鍵字宣告我們要修改的是全域變數
    global _pending_restart_ws, _system_lock_timestamp

    while True:
        time.sleep(30)  # 每 30 秒檢查一次
        with _lock_state_lock:
            if not _pending_restart_ws or _system_lock_timestamp is None:
                continue

            elapsed = time.time() - _system_lock_timestamp
            if elapsed > LOCK_TIMEOUT_SECONDS:
                pending_ws_list = sorted(list(_pending_restart_ws))
                logging.warning(
                    f"[Lock Timeout] System lock timed out after {LOCK_TIMEOUT_SECONDS}s. "
                    f"Workstations that did not report back: {pending_ws_list}"
                )
                
                # 強制解鎖
                _pending_restart_ws.clear()
                _system_lock_timestamp = None
                
                # 推送 SSE 通知前端解鎖（附帶原因和未回報列表）
                sse_broker.publish(
                    "update", 
                    {
                        "unlock_reason": "timeout",
                        "pending_ws": pending_ws_list
                    }
                )

# =========================
# 路由
# =========================
@app.get("/ui/config")
def get_ui_config():
    try:
        ui, etag = _current_ui_and_etag()

        # If-None-Match / 304（維持全域 ETag）
        inm = request.headers.get("If-None-Match", "").strip()
        inm_clean = inm.strip('W/').strip('"') if inm else ""
        raw_ids = (request.args.get("cameras") or "").strip()
        if inm_clean and inm_clean == etag:
            resp = make_response("", 304)
            resp.headers["ETag"] = f'W/"{etag}"'
            resp.headers["Cache-Control"] = "no-cache"
            if raw_ids:
                resp.headers["X-Partial"] = "1"
                resp.headers["X-Requested-Cameras"] = raw_ids
            return resp

        # 子集回傳（?cameras=11,33,57）
        headers = {"ETag": f'W/"{etag}"', "Cache-Control": "no-cache"}
        if raw_ids:
            ids = []
            for t in re.split(r"[,\s]+", raw_ids):
                if not t: continue
                cid = _to_int_id(t)
                if cid is not None:
                    ids.append(cid)
            if ids:
                ui = deepcopy(ui)
                ui["cameras"] = [c for c in ui.get("cameras", []) if _to_int_id(c.get("NVR_channel")) in set(ids)]
                headers["X-Partial"] = "1"
                headers["X-Requested-Cameras"] = ",".join(str(i) for i in ids)

        return _response_ok("UI 設定讀取成功", data=ui, headers=headers)
    except FileNotFoundError as e:
        return _response_err("TOTAL_CONFIG_NOT_FOUND", str(e), http_status=404)
    except Exception as e:
        logging.error(f"Error in get_ui_config: {e}", exc_info=True)
        return _response_err("INTERNAL_SERVER_ERROR", "An unexpected error occurred while building UI config.", http_status=500)

@app.post("/config/send_groups")
def receive_groups(workstation_ip=None):
    """
    【修改】單站分組寫入，並觸發 UI 鎖定流程。
    """
    # 【新】前置鎖定檢查
    with _lock_state_lock:
        if _pending_restart_ws:
            return _response_err("SYSTEM_LOCKED", 
                                 f"系統已被鎖定並等待工作站重啟。", 
                                 http_status=409)

    if request.mimetype and request.mimetype.startswith(("application/x-www-form-urlencoded", "multipart/form-data")):
        payload = _payload_from_form_receive_groups()
    else:
        payload = request.get_json(silent=True) or {}

    if workstation_ip:
        payload["workstation_ip"] = workstation_ip

    if not payload:
        return _response_err("INVALID_PAYLOAD", "Request body must be a valid JSON.", http_status=400)

    if "workstation_ip" not in payload:
        return _response_err("INVALID_PAYLOAD", "payload must contain 'workstation_ip'.", http_status=400)

    ws_host, _ = _normalize_ws_ip_port(str(payload["workstation_ip"]))
    payload["workstation_ip"] = ws_host

    # ... (驗證 payload 格式)
    polling = payload.get("polling_cameras")
    fixed = payload.get("fixed_cameras")
    if polling is not None and not isinstance(polling, list):
        return _response_err("INVALID_PAYLOAD", "polling_cameras must be an array.", http_status=400)
    if fixed is not None and not isinstance(fixed, list):
        return _response_err("INVALID_PAYLOAD", "fixed_cameras must be an array.", http_status=400)
    
    affected_ws = {ws_host}

    try:
        with _file_lock:
            total_config = _load_total_config()

            ok, msg = _validate_groups_single(payload, total_config)
            if not ok:
                return _response_err("INVALID_GROUPS", msg, http_status=409)

            # 在套用前，先確定是否有來源工作站，一併加入影響列表
            from_ip1 = payload.get("_implicit_transfer_from")
            from_ip2 = payload.get("_implicit_reassign_from")
            if from_ip1: affected_ws.add(from_ip1)
            if from_ip2: affected_ws.add(from_ip2)

            new_total_config = _apply_groups_single(total_config, payload)
            _save_workstation_assignments_from_total(_ensure_cameras_nvr_channel_str(new_total_config))
            
    except FileNotFoundError as e:
        return _response_err("TOTAL_CONFIG_NOT_FOUND", str(e), http_status=404)
    except Exception as e:
        logging.error(f"Error in receive_groups (write stage): {e}", exc_info=True)
        return _response_err("INTERNAL_SERVER_ERROR", "An unexpected error occurred while updating groups.", http_status=500)

    # 檔案寫入成功後
    try:
        # 觸發系統鎖定
        with _lock_state_lock:
            _pending_restart_ws.update(affected_ws)
            _system_lock_timestamp = time.time()
        
        logging.info(f"[Lock Acquired] System locked. Waiting for workstations to restart: {list(affected_ws)}")
        
        # 觸發派送
        _next_change_seq()
        distributor.enqueue(version=_change_seq, reason="groups")

        # 回傳鎖定指令給前端（已移除 affected_workstations）
        return _response_ok(
            message="設定已儲存，正等待工作站重啟。",
            data={
                "lock_ui": True,
                # "affected_workstations": sorted(list(affected_ws)), #【移除】根據前端要求，不再回傳此欄位
                "timeout_seconds": LOCK_TIMEOUT_SECONDS
            },
            code="PENDING_RESTART"
        )
    except Exception as e:
        logging.error(f"Post-update lock/dispatch failed in receive_groups: {e}", exc_info=True)
        # 即使鎖定或派送失敗，設定檔也已寫入，這是一個需要手動介入的嚴重錯誤
        return _response_err("INTERNAL_SERVER_ERROR", "設定已寫入，但觸發鎖定或派送時發生錯誤。", http_status=500)

@app.post("/config/send_setting")
def receive_setting(NVR_channel=None):
    """
    【修改】攝影機設定寫入，並觸發 UI 鎖定流程。
    """
    # 【新】前置鎖定檢查
    with _lock_state_lock:
        if _pending_restart_ws:
            return _response_err("SYSTEM_LOCKED", 
                                 f"系統已被鎖定並等待工作站重啟。", 
                                 http_status=409)

    is_form = bool(request.mimetype and request.mimetype.startswith(("application/x-www-form-urlencoded", "multipart/form-data")))
    if is_form:
        payload = _payload_from_form_receive_setting()
    else:
        payload = request.get_json(silent=True) or {}

    if NVR_channel is not None:
        try: payload["NVR_channel"] = int(NVR_channel)
        except Exception: pass

    if isinstance(payload, dict) and "items" in payload:
        return _response_err("INVALID_PAYLOAD", "Batch update via 'items' is not supported.", http_status=400)

    if not payload or "NVR_channel" not in payload:
        return _response_err("INVALID_PAYLOAD", "Request body must contain NVR_channel.", http_status=400)
    
    item = payload
    cid = _to_int_id(item.get("NVR_channel"))
    if cid is None:
        return _response_err("INVALID_PAYLOAD", "NVR_channel must be an integer.", http_status=400)
        
    affected_ws = set()

    try:
        with _file_lock:
            total_config = _load_total_config()
            
            # 找出此攝影機所屬的工作站
            ws_map = total_config.get("workstation_ip_mapping", {})
            for ip, entry in ws_map.items():
                _, _, cam_set = _ws_entry_to_sets(entry)
                if cid in cam_set:
                    affected_ws.add(ip)
                    break # 假定一台相機只屬於一台工作站
            
            if not affected_ws:
                return _response_err("CAMERA_NOT_ASSIGNED", f"攝影機 {cid} 未被分配至任何工作站。", http_status=404)

            ok, msg = _validate_single_setting(item)
            if not ok:
                return _response_err("VALIDATION_ERROR", msg, http_status=400)

            new_cfg = _apply_single_setting(total_config, item)
            new_cfg = _ensure_cameras_nvr_channel_str(new_cfg)
            _save_common_settings_from_total(new_cfg)
            _save_camera_from_total(new_cfg, cid)

    except FileNotFoundError as e:
        return _response_err("TOTAL_CONFIG_NOT_FOUND", str(e), http_status=404)
    except Exception as e:
        logging.error(f"Error in receive_setting (write stage): {e}", exc_info=True)
        return _response_err("INTERNAL_SERVER_ERROR", "An unexpected error occurred while updating setting.", http_status=500)

    # 檔案寫入成功後
    try:
        # 觸發系統鎖定
        with _lock_state_lock:
            _pending_restart_ws.update(affected_ws)
            _system_lock_timestamp = time.time()
        
        logging.info(f"[Lock Acquired] System locked. Waiting for workstations to restart: {list(affected_ws)}")
        
        # 觸發派送
        _next_change_seq()
        distributor.enqueue(version=_change_seq, reason="settings")

        # 回傳鎖定指令給前端（已移除 affected_workstations）
        return _response_ok(
            message="設定已儲存，正等待工作站重啟。",
            data={
                "lock_ui": True,
                # "affected_workstations": sorted(list(affected_ws)), #【移除】根據前端要求，不再回傳此欄位
                "timeout_seconds": LOCK_TIMEOUT_SECONDS
            },
            code="PENDING_RESTART"
        )
    except Exception as e:
        logging.error(f"Post-update lock/dispatch failed in receive_setting: {e}", exc_info=True)
        return _response_err("INTERNAL_SERVER_ERROR", "設定已寫入，但觸發鎖定或派送時發生錯誤。", http_status=500)

@app.post("/status/disconnection_process")
def disconnection_process():
    """
    回報工作站的斷線清單（內部使用）。
    """
    payload = request.get_json(silent=True)
    if not payload:
        return _response_err("INVALID_PAYLOAD", "Request body must be a valid JSON.", http_status=400)

    ip_raw = payload.get("workstation_ip")
    host, _ = _normalize_ws_ip_port(str(ip_raw))
    arr = payload.get("disconnect_cameras", [])
    if not isinstance(arr, list):
        return _response_err("INVALID_PAYLOAD", "disconnect_cameras must be an array.", http_status=400)

    try:
        _disconnection_map[host] = [_to_int_id(x) for x in arr if _to_int_id(x) is not None]
    except Exception as e:
        return _response_err("INVALID_PAYLOAD", f"invalid disconnect_cameras: {e}", http_status=400)

    try:
        with _file_lock:
            cfg = _load_total_config()
            ws_map = cfg.get("workstation_ip_mapping") or {}
            entry = ws_map.get(host)
            if isinstance(entry, list): entry = {"polling_cameras": entry, "fixed_cameras": []}
            elif not isinstance(entry, dict): entry = {"polling_cameras": [], "fixed_cameras": []}
            entry["disconnect_cameras"] = list(dict.fromkeys(_disconnection_map[host]))
            ws_map[host] = entry
            cfg["workstation_ip_mapping"] = ws_map
            _save_workstation_assignments_from_total(_ensure_cameras_nvr_channel_str(cfg))
    except FileNotFoundError as e:
        return _response_err("TOTAL_CONFIG_NOT_FOUND", str(e), http_status=404)
    except Exception as e:
        logging.error(f"Write total-config failed in disconnection_process: {e}", exc_info=True)
        return _response_err("INTERNAL_SERVER_ERROR", "Failed to persist disconnection status.", http_status=500)

    # 【修改】發送通用更新事件
    _next_change_seq()
    _, etag = _current_ui_and_etag()
    sse_broker.publish("update", {"etag": etag})
    return _response_ok("已更新斷線狀態")

@app.post("/status/polling_cams_process")
def polling_cams_process():
    """
    【修改】回報工作站目前的輪詢清單，並處理重啟解鎖訊號。
    """
    payload = request.get_json(silent=True)
    if not payload:
        return _response_err("INVALID_PAYLOAD", "Request body must be a valid JSON.", http_status=400)

    ip_raw = payload.get("workstation_ip")
    host, _ = _normalize_ws_ip_port(str(ip_raw))
    arr = payload.get("current_polling_cameras", [])
    if not isinstance(arr, list):
        return _response_err("INVALID_PAYLOAD", "current_polling_cameras must be an array.", http_status=400)

    # 【新】處理重啟解鎖訊號
    if payload.get("restarted") is True:
        with _lock_state_lock:
            if host in _pending_restart_ws:
                logging.info(f"[Lock Release] Workstation '{host}' reported back as restarted.")
                _pending_restart_ws.discard(host)
                if not _pending_restart_ws:
                    logging.info("[Lock Release] All workstations have reported back. System unlocked.")
                    _system_lock_timestamp = None
                    sse_broker.publish("update", {"unlock_reason": "completed"})
            else:
                logging.warning(f"Received a restart signal from an unexpected workstation: {host}")

    # 更新記憶體 map
    try:
        _current_polling_map[host] = [_to_int_id(x) for x in arr if _to_int_id(x) is not None]
    except Exception as e:
        return _response_err("INVALID_PAYLOAD", f"invalid current_polling_cameras: {e}", http_status=400)

    # 同步寫入 total-config（持久化）
    try:
        with _file_lock:
            cfg = _load_total_config()
            ws_map = cfg.get("workstation_ip_mapping") or {}
            entry = ws_map.get(host)
            if isinstance(entry, list): entry = {"polling_cameras": entry, "fixed_cameras": []}
            elif not isinstance(entry, dict): entry = {"polling_cameras": [], "fixed_cameras": []}
            entry["current_polling_cameras"] = list(dict.fromkeys(_current_polling_map[host]))
            ws_map[host] = entry
            cfg["workstation_ip_mapping"] = ws_map
            _save_workstation_assignments_from_total(_ensure_cameras_nvr_channel_str(cfg))
    except FileNotFoundError as e:
        return _response_err("TOTAL_CONFIG_NOT_FOUND", str(e), http_status=404)
    except Exception as e:
        logging.error(f"Write total-config failed in polling_cams_process: {e}", exc_info=True)
        return _response_err("INTERNAL_SERVER_ERROR", "Failed to persist current polling status.", http_status=500)

    # 【修改】發送通用更新事件
    _next_change_seq()
    _, etag = _current_ui_and_etag()
    sse_broker.publish("update", {"etag": etag})
    return _response_ok("已更新當前輪詢清單")

@app.post("/image/image_update")
def image_update():
    """
    上傳一階預覽圖。
    """
    payload = request.get_json(silent=True)
    if not payload:
        return _response_err("INVALID_PAYLOAD", "Request body must be a valid JSON.", http_status=400)

    cam_id = _to_int_id(payload.get("NVR_channel"))
    if cam_id is None:
        return _response_err("INVALID_PAYLOAD", "NVR_channel must be integer.", http_status=400)

    try:
        with _file_lock:
            total_config = _load_total_config()
        idx = _index_cameras_by_id(total_config.get("cameras", []) or [])
        if cam_id not in idx:
            return _response_err("CAMERA_NOT_FOUND", f"camera not found: {cam_id}", http_status=404)
    except FileNotFoundError as e:
        return _response_err("TOTAL_CONFIG_NOT_FOUND", str(e), http_status=404)
    
    data_url = None
    b64_part = None

    if isinstance(payload.get("picture"), str):
        data_url = payload["picture"]
        if "," not in data_url: return _response_err("INVALID_IMAGE", "picture must be a data URL", http_status=400)
        try:
            header, b64_part = data_url.split(",", 1)
            base64.b64decode(b64_part, validate=True)
        except (binascii.Error, ValueError):
            return _response_err("INVALID_IMAGE", "Invalid base64.", http_status=400)
        h = header.lower()
        if not (h.startswith("data:image/jpeg;base64") or h.startswith("data:image/png;base64")):
            return _response_err("INVALID_IMAGE", "only jpeg or png base64 is allowed.", http_status=400)

    elif isinstance(payload.get("image_path"), str):
        path_str = payload["image_path"].strip()
        p = Path(path_str)
        if not p.is_file(): return _response_err("FILE_NOT_FOUND", f"file not found: {path_str}", http_status=404)
        raw = p.read_bytes()
        head = raw[:16]
        if head.startswith(b"\xff\xd8"): mime = "image/jpeg"
        elif head.startswith(b"\x89PNG\r\n\x1a\n"): mime = "image/png"
        else: return _response_err("INVALID_IMAGE", "only JPEG or PNG file is allowed.", http_status=400)
        if len(raw) > IMAGE_BASE64_MAX_KB * 1024: return _response_err("IMAGE_TOO_LARGE", f"image exceeds {IMAGE_BASE64_MAX_KB}KB limit.", http_status=413)
        b64_part = base64.b64encode(raw).decode("ascii")
        data_url = f"data:{mime};base64,{b64_part}"
    else:
        return _response_err("INVALID_IMAGE", "Provide either 'picture' (data URL) or 'image_path'.", http_status=400)

    est_bytes = (len(b64_part) * 3) // 4
    if est_bytes > IMAGE_BASE64_MAX_KB * 1024:
        return _response_err("IMAGE_TOO_LARGE", f"image exceeds {IMAGE_BASE64_MAX_KB}KB limit.", http_status=413)

    _camera_b64[cam_id] = {"data": data_url, "ts": time.time()}
    _next_change_seq()
    
    _, etag = _current_ui_and_etag()
    sse_broker.publish("update", {"etag": etag, "touched_cameras": [cam_id]})
    
    return _response_ok("預覽圖已儲存", data={"stored": True})

@app.get("/ui/events")
def ui_events():
    """
    【修改】SSE 事件流：推送 snapshot / update
    """
    def gen():
        q = sse_broker.register()
        try:
            # 初次連線即推送一個快照事件，方便前端同步
            _, etag = _current_ui_and_etag()
            sse_broker.publish("snapshot", {"etag": etag, "at": _now_iso()})
            while True:
                msg = q.get()
                yield msg
        except GeneratorExit:
            pass
        finally:
            sse_broker.unregister(q)
    headers = {"Cache-Control": "no-cache", "Connection": "keep-alive"}
    return Response(stream_with_context(gen()), mimetype="text/event-stream", headers=headers)

# -------- HTML 頁面 --------
@app.get("/")
def home():
    return redirect(url_for("dashboard_page"))

@app.get("/dashboard")
def dashboard_page():
    ui, etag = _current_ui_and_etag()
    return render_template("dashboard_ui.html", boot_etag=etag, seq=_change_seq)

if __name__ == "__main__":
    logging.info(f"[smoke_UI_server] starting @ {HOST}:{PORT}, TOTAL_CONFIG={TOTAL_CONFIG_PATH}")
    logging.info(f"[smoke_UI_server] IMAGE_BASE64_TTL = {IMAGE_BASE64_TTL}s, DEBUG={DEBUG}")
    
    # 【新】啟動背景鎖定超時監控執行緒
    timeout_thread = threading.Thread(target=_lock_timeout_monitor, daemon=True)
    timeout_thread.start()
    logging.info("[smoke_UI_server] Lock timeout monitor thread started.")
    
    distributor.start()
    app.run(host=HOST, port=PORT, debug=DEBUG)
