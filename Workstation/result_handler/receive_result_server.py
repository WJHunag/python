from flask import Flask, request, jsonify
import os
from .bot import smoke_notify, smoke_notify_watersmoke, init_notification_log,telegram_smoke_notify, telegram_smoke_notify_watersmoke,discord_smoke_notify,discord_smoke_notify_watersmoke,discord_heartbeat_notify,discord_emission_point_test_notify,validate_notification_channels
from . import api_request
from .fixed_point_annotation import draw_selected_fixed_points, numbered_smoke_results
from .emission_point_policy import format_emission_points
from werkzeug.utils import secure_filename
import threading
import json
import requests
import logging
from datetime import datetime
import utility
import time
import configparser
import math
from datetime import timedelta
import cv2
import image_metadata
from camera_hub.drive_retention import ReceiptStore, record_detection

app = Flask(__name__)
UPLOAD_FOLDER = './received_results'
STAGE1_FOLDER = './received_results/stage1'
STAGE2_FOLDER = './received_results/stage2'
LOG_FOLDER = './logs'
MARKED_OUTPUT_FOLDER = './received_results/marked_results'

# 工作站本機保存中央派發的 distributed_config；不再讀 smoke_local_camera_config.json。
DISTRIBUTED_CONFIG_DIR = os.path.join('smoke_config', 'distributed_config')

os.makedirs(LOG_FOLDER, exist_ok=True)  # 確保資料夾存在
os.makedirs(MARKED_OUTPUT_FOLDER, exist_ok=True)

# init_notification_log()

# 初始化日誌記錄
def setup_server_logger():
    global log_file
    current_date = datetime.now().strftime('%Y-%m-%d')
    log_file = os.path.join(LOG_FOLDER, f'{current_date}_server.log')
    logger = logging.getLogger("server_logger")
    logger.handlers = []
    logger.propagate = False

    file_handler = logging.FileHandler(log_file, encoding='utf-8')
    file_handler.setFormatter(logging.Formatter('[%(asctime)s] %(levelname)s: %(message)s'))
    logger.addHandler(file_handler)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(logging.Formatter('[%(asctime)s] %(levelname)s: %(message)s'))
    logger.addHandler(console_handler)

    logger.setLevel(logging.INFO)
    return logger

receive_logger = setup_server_logger()
_last_heartbeat_date = None  # 當日去重用（date）

def _parse_hhmm(hhmm: str):
    """
    解析 'HH:MM'，回傳 (hour, minute)。解析失敗則回傳預設 07:00。
    """
    try:
        hhmm = (hhmm or "").strip()
        parts = hhmm.split(":")
        if len(parts) != 2:
            raise ValueError("invalid time format")
        h = int(parts[0])
        m = int(parts[1])
        if not (0 <= h <= 23 and 0 <= m <= 59):
            raise ValueError("time out of range")
        return h, m
    except Exception:
        return 7, 0

def _next_run_datetime(target_h: int, target_m: int) -> datetime:
    """
    計算下一次排程觸發的 datetime（本機時間）
    """
    now = datetime.now()
    run_at = now.replace(hour=target_h, minute=target_m, second=0, microsecond=0)
    if run_at <= now:
        run_at = run_at + timedelta(days=1)
    return run_at

def _seconds_until(run_at: datetime) -> int:
    """
    計算距離 run_at 的秒數（向上取整避免提早醒來造成 06:59/07:00 雙發）。
    """
    now = datetime.now()
    delta_s = (run_at - now).total_seconds()
    # 使用 ceil 避免 int() 向下截斷導致提早在 06:59:59.x 醒來
    return max(1, int(math.ceil(delta_s)))

def start_daily_heartbeat_thread():
    """
    每天固定時間送 Discord 心跳訊息：'<workstation_name> 運行中'
    設定來源：config.ini 的 [heartbeat] enabled/time/workstation_name
    """
    config_parser = configparser.ConfigParser()
    config_parser.read("./config.ini")

    enabled = config_parser.get("heartbeat", "enabled", fallback="false").strip().lower() in ("1", "true", "yes", "y", "on")
    if not enabled:
        receive_logger.info("Heartbeat 已停用（[heartbeat] enabled=false）。")
        return

    workstation_name = config_parser.get("heartbeat", "workstation_name", fallback="工作站").strip() or "工作站"
    hhmm = config_parser.get("heartbeat", "time", fallback="07:00")
    target_h, target_m = _parse_hhmm(hhmm)

    def _loop():
        global _last_heartbeat_date
        receive_logger.info(f"Heartbeat 排程啟動：每日 {target_h:02d}:{target_m:02d}，名稱='{workstation_name}'")
        while True:
            try:
                run_at = _next_run_datetime(target_h, target_m)
                time.sleep(_seconds_until(run_at))

                # 當日去重：避免邊界情況或意外重入導致同一天發兩次
                today = datetime.now().date()
                if _last_heartbeat_date == today:
                    receive_logger.warning("Heartbeat duplicated in same day; skip.")
                    continue

                now_ts = datetime.now().strftime("%Y-%m-%d %H:%M")
                msg = f"{workstation_name} 運行中（{now_ts}）"
                discord_heartbeat_notify(msg)
                _last_heartbeat_date = today
                receive_logger.info(f"Heartbeat 已發送：{msg}")
            except Exception as e:
                receive_logger.error(f"Heartbeat 執行失敗: {e}")
                # 避免錯誤迴圈狂刷
                time.sleep(60)

    t = threading.Thread(target=_loop, daemon=True, name="daily_heartbeat_thread")
    t.start()

def check_and_rotate_logs():
    global log_file, receive_logger
    new_date = datetime.now().strftime('%Y-%m-%d')
    current_log_date = log_file.split('/')[-1].split('_')[0]

    if new_date != current_log_date:
        log_file = os.path.join(LOG_FOLDER, f'{new_date}_receive.log')
        file_handler = logging.FileHandler(log_file, encoding='utf-8')
        file_handler.setFormatter(logging.Formatter('[%(asctime)s] %(levelname)s: %(message)s'))
        
        while receive_logger.hasHandlers():
            receive_logger.handlers.clear()
            
        receive_logger.addHandler(file_handler)
        receive_logger.addHandler(logging.StreamHandler())
        receive_logger.info("日期變更，切換到新日誌檔案。")

def is_within_night_detection(camera_info):
    function_switches = camera_info.get('function_switches', {})
    night_start_str = function_switches.get('night_detection_start', "00:00")
    night_end_str = function_switches.get('night_detection_end', "00:00")
    night_start = datetime.strptime(night_start_str, "%H:%M").time()
    night_end = datetime.strptime(night_end_str, "%H:%M").time()
    current_time = datetime.now().time()
    if night_start <= night_end:
        return night_start <= current_time <= night_end
    else:
        return current_time >= night_start or current_time <= night_end

for folder in [UPLOAD_FOLDER, STAGE1_FOLDER, STAGE2_FOLDER]:
    if not os.path.exists(folder):
        os.makedirs(folder)
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER

try:
    config = utility.load_config()
    stage2_cams = config.get('stage2_cams', [])
    validate_notification_channels(config.get('cameras', []), send_error_notification=True)
except FileNotFoundError:
    config = {'cameras': []}
    stage2_cams = []

image_group_results = {}

# ==========================
# BCAF (Dual) 參數與常數
# ==========================
IMG_EXTS = ('.png', '.jpg', '.jpeg', '.bmp', '.webp')
LABEL_STEAM = "steam"
LABEL_SMOKE = "black_smoke"
LABEL_NONE = "none"

# 使用者指定的最佳信心閾值
T_STEAM = 0.5
T_SMOKE = 0.9

def bcaf_dual_fuse(l_cls: str, conf_cls: float, l_ratio: str, t_steam: float = T_STEAM, t_smoke: float = T_SMOKE) -> str:
    """BCAF (Dual): Bidirectional Confidence-Adaptive Fusion
    - l_cls: deep classifier label (steam/black_smoke/none)
    - conf_cls: deep classifier confidence for its predicted label (0~1)
    - l_ratio: ratio classifier label (steam/black_smoke/none)
    """
    if l_cls == LABEL_STEAM:
        if conf_cls < t_steam:
            return LABEL_SMOKE if l_ratio == LABEL_SMOKE else LABEL_STEAM
        return LABEL_STEAM
    elif l_cls == LABEL_SMOKE:
        if conf_cls < t_smoke:
            if l_ratio == LABEL_STEAM:
                return LABEL_STEAM
            elif l_ratio == LABEL_SMOKE:
                return LABEL_SMOKE
            else:
                return LABEL_SMOKE
        return LABEL_SMOKE
    else:
        return l_ratio if l_ratio != LABEL_NONE else LABEL_NONE



def send_image_to_server(image_path, timeout=180):
    """等價於 verify_models.py 的 infer_classifier。
    回傳: (label, confidence, raw_json)
      - label: steam / black_smoke / none
      - confidence: deep model 對「自己預測類別」的信心值 (0~1)
    """
    url = config.get('smoke_classifier_ip', "http://127.0.0.1:5000/infer")
    if not url:
        return LABEL_NONE, 0.0, {}

    if not image_path.lower().endswith(IMG_EXTS):
        return LABEL_NONE, 0.0, {}

    try:
        with open(image_path, 'rb') as image_file:
            files = {'file': image_file}
            resp = requests.post(url, files=files, timeout=timeout)
            resp.raise_for_status()
            js = resp.json()

        pred_val = None
        conf = 0.0

        # S2_Server_1 常見回傳格式: [pred, prob0, prob1]
        if isinstance(js, list):
            if len(js) >= 3:
                pred_val = str(js[0])
                try:
                    prob0 = float(js[1])
                    prob1 = float(js[2])
                    conf = prob1 if pred_val == '1' else prob0
                except Exception:
                    conf = 0.0
            elif len(js) >= 1:
                pred_val = str(js[0])
                conf = 0.0
        # 兼容 dict 格式
        elif isinstance(js, dict):
            pred_val = js.get("label", js.get("result"))
            try:
                conf = float(js.get("confidence", 0.0) or 0.0)
            except Exception:
                conf = 0.0
        else:
            return LABEL_NONE, 0.0, js

        # pred_val: "1"=smoke, "0"=steam
        if str(pred_val) == "1":
            return LABEL_SMOKE, float(conf), js
        elif str(pred_val) == "0":
            return LABEL_STEAM, float(conf), js
        else:
            return LABEL_NONE, 0.0, js

    except Exception as e:
        receive_logger.error(f"S2_Server 連線/解析失敗: {e}")
        return LABEL_NONE, 0.0, {}



def send_to_ratio_classifier(image_path, timeout=10):
    """等價於 verify_models.py 的 infer_ratio。
    回傳: (label, raw_json)
      - label: steam / black_smoke / none
    """
    url = config.get('ratio_classifier_ip')
    if not url:
        return LABEL_NONE, {}

    if not image_path.lower().endswith(IMG_EXTS):
        return LABEL_NONE, {}

    try:
        with open(image_path, 'rb') as image_file:
            files = {'file': image_file}
            resp = requests.post(url, files=files, timeout=timeout)
            resp.raise_for_status()
            js = resp.json()

        label = js.get("image_final_label")
        if label is None:
            dets = js.get("detections", []) or []
            labels = [(d.get("final_label") or "") for d in dets]
            if any(l == "black_smoke" for l in labels):
                label = "black_smoke"
            elif any(l == "steam" for l in labels):
                label = "steam"
            else:
                label = "none"

        if label == "black_smoke":
            return LABEL_SMOKE, js
        elif label == "steam":
            return LABEL_STEAM, js
        else:
            return LABEL_NONE, js

    except Exception as e:
        receive_logger.error(f"傳送/解析 Ratio Classifier 失敗: {e}")
        return LABEL_NONE, {}

def _safe_int(value, default=0):
    try:
        if value is None:
            return default
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _extract_crop_origin(context, form):
    bbox_xyxy = context.get("bbox_xyxy") if isinstance(context, dict) else None
    if isinstance(bbox_xyxy, (list, tuple)) and len(bbox_xyxy) >= 2:
        return _safe_int(bbox_xyxy[0]), _safe_int(bbox_xyxy[1])

    return _safe_int(form.get("crop_x", 0)), _safe_int(form.get("crop_y", 0))


def _get_ratio_detections(raw_ratio):
    if not isinstance(raw_ratio, dict):
        return []
    detections = raw_ratio.get("detections", [])
    return detections if isinstance(detections, list) else []


_DEFAULT_EMISSION_POINT_RADIUS = 50
_EMISSION_POINT_MARKER_RADIUS = 8
_FIXED_REFERENCE_AREA_COLOR_BGR = (225, 105, 65)  # RoyalBlue #4169E1
_FIXED_REFERENCE_AREA_OPACITY = 0.4
_EMISSION_POINT_DECISION_MODE = "three_point_any"
_STAGE2_PARTIAL_DECISION_MODE = "stage2_partial_candidates"
_STAGE1_FALLBACK_DECISION_MODE = "stage1_bbox_fallback"
_EMISSION_POINT_METHOD_CODE = "three_point_emission_point"
_EMISSION_POINT_METHOD_NAME = "三點推論起煙點"
_CANDIDATE_POINT_LABELS = {
    "mask_lowest": "起煙點",
    "bbox_bottom_left": "bbox左下",
    "bbox_bottom_right": "bbox右下",
    "stage1_bbox_bottom_left": "Stage1 bbox左下",
    "stage1_bbox_bottom_right": "Stage1 bbox右下",
}
_CANDIDATE_POINT_ORDER = (
    "mask_lowest",
    "bbox_bottom_left",
    "bbox_bottom_right",
    "stage1_bbox_bottom_left",
    "stage1_bbox_bottom_right",
)


def _finite_float(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _get_emission_point_radius(camera_info):
    """讀取單支攝影機的固定點判定半徑，設定無效時回退預設值。"""
    raw_radius = (
        camera_info.get("fixed_area_radius_px")
        if isinstance(camera_info, dict)
        else None
    )
    radius = _finite_float(raw_radius)
    if (
        isinstance(raw_radius, bool)
        or radius is None
        or radius <= 0
        or not radius.is_integer()
    ):
        if raw_radius not in (None, ""):
            receive_logger.warning(
                "固定點範圍設定無效：fixed_area_radius_px=%r，使用預設 %d px",
                raw_radius,
                _DEFAULT_EMISSION_POINT_RADIUS,
            )
        return _DEFAULT_EMISSION_POINT_RADIUS
    return int(radius)


def _normalize_candidate_point(value):
    if not isinstance(value, (list, tuple)) or len(value) < 2:
        return None

    x = _finite_float(value[0])
    y = _finite_float(value[1])
    if x is None or y is None:
        return None
    return _safe_int(x), _safe_int(y)


def _normalize_bbox_xyxy(value):
    if not isinstance(value, (list, tuple)) or len(value) < 4:
        return None

    bbox_values = [_finite_float(item) for item in value[:4]]
    if any(item is None for item in bbox_values):
        return None

    x1, y1, x2, y2 = bbox_values
    left, right = sorted((_safe_int(x1), _safe_int(x2)))
    top, bottom = sorted((_safe_int(y1), _safe_int(y2)))
    if right <= left or bottom <= top:
        return None
    return left, top, right, bottom


def _resolve_stage1_bbox(data):
    """取得原圖座標系的 Stage1 bbox；metadata 缺漏時由裁切圖尺寸還原。"""
    bbox = _normalize_bbox_xyxy(data.get("stage1_bbox_xyxy"))
    if bbox is not None:
        return bbox

    crop_image_path = data.get("crop_image_path")
    if not crop_image_path:
        return None

    crop_image = cv2.imread(crop_image_path)
    if crop_image is None:
        return None

    crop_height, crop_width = crop_image.shape[:2]
    if crop_width <= 0 or crop_height <= 0:
        return None

    crop_x = _safe_int(data.get("crop_x", 0))
    crop_y = _safe_int(data.get("crop_y", 0))
    return crop_x, crop_y, crop_x + crop_width, crop_y + crop_height


def _get_stage1_bbox_candidate_points(bbox_xyxy, image_width, image_height):
    """以 Stage1 原圖 bbox 的實際底部像素建立 fallback 候選點。"""
    bbox = _normalize_bbox_xyxy(bbox_xyxy)
    if bbox is None or image_width <= 0 or image_height <= 0:
        return {}

    left, top, right, bottom = bbox
    if right <= 0 or bottom <= 0 or left >= image_width or top >= image_height:
        return {}

    left = max(0, left)
    right = min(image_width - 1, right - 1)
    bottom = min(image_height - 1, bottom - 1)
    top = max(0, top)
    if right < left or bottom < top:
        return {}

    return {
        "stage1_bbox_bottom_left": (left, bottom),
        "stage1_bbox_bottom_right": (right, bottom),
    }


def _get_detection_candidate_points(detection, crop_width=None, crop_height=None):
    """取得 Ratio Classifier detection 在裁切圖座標系中的三個候選點。"""
    if not isinstance(detection, dict):
        return {}

    candidates = {}
    lowest_point = _normalize_candidate_point(detection.get("lowest_point"))
    if lowest_point is not None:
        if crop_width and crop_height:
            lowest_point = (
                min(max(0, lowest_point[0]), crop_width - 1),
                min(max(0, lowest_point[1]), crop_height - 1),
            )
        candidates["mask_lowest"] = lowest_point

    bbox = _normalize_bbox_xyxy(detection.get("box"))
    if bbox is not None:
        left, top, right, bottom = bbox
        if crop_width and crop_height:
            left = max(0, left)
            top = max(0, top)
            right = min(crop_width, right)
            bottom = min(crop_height, bottom)
        if right > left and bottom > top:
            # box 採 xyxy exclusive，使用實際底部與右側像素。
            candidates["bbox_bottom_left"] = (left, bottom - 1)
            candidates["bbox_bottom_right"] = (right - 1, bottom - 1)

    return candidates


def _offset_candidate_points(candidates, offset_x, offset_y):
    return {
        name: (_safe_int(point[0]) + offset_x, _safe_int(point[1]) + offset_y)
        for name, point in candidates.items()
    }


def _offset_bbox_xyxy(bbox_xyxy, offset_x, offset_y):
    bbox = _normalize_bbox_xyxy(bbox_xyxy)
    if bbox is None:
        return None

    left, top, right, bottom = bbox
    return (
        left + _safe_int(offset_x),
        top + _safe_int(offset_y),
        right + _safe_int(offset_x),
        bottom + _safe_int(offset_y),
    )


def _candidate_priority(name):
    try:
        return _CANDIDATE_POINT_ORDER.index(name)
    except ValueError:
        return len(_CANDIDATE_POINT_ORDER)


def _get_fixed_reference_points(camera_info):
    if not isinstance(camera_info, dict):
        return []

    geo_reference = camera_info.get("geo_reference") or {}
    if not isinstance(geo_reference, dict):
        return []
    raw_points = geo_reference.get("points", [])
    if not isinstance(raw_points, list):
        return []

    fixed_points = []
    for index, point in enumerate(raw_points):
        if not isinstance(point, dict):
            continue
        image_coord = point.get("image_coord", {})
        if not isinstance(image_coord, dict):
            continue

        fixed_x = _finite_float(image_coord.get("x"))
        fixed_y = _finite_float(image_coord.get("y"))
        if fixed_x is None or fixed_y is None:
            continue

        wgs84_coords = point.get("wgs84_coords") or {}
        if not isinstance(wgs84_coords, dict):
            wgs84_coords = {}
        fixed_points.append({
            "index": index,
            "point_name": point.get("point_name", "未知固定點"),
            "coordinates": (_safe_int(fixed_x), _safe_int(fixed_y)),
            "wgs84_coords": {
                "x": _finite_float(wgs84_coords.get("x")),
                "y": _finite_float(wgs84_coords.get("y")),
            },
        })
    return fixed_points


def _empty_candidate_match(candidates, radius):
    preferred_name = min(candidates, key=_candidate_priority) if candidates else None
    preferred_coord = candidates.get(preferred_name) if preferred_name else None
    return {
        "is_inside": False,
        "point_name": None,
        "offset_distance": None,
        "fixed_coordinates": None,
        "matched_candidate": preferred_name,
        "candidate_coordinates": preferred_coord,
        "hit_count": 0,
        "min_distance": None,
        "radius": radius,
        "hit_reference_points": [],
        "candidates": {
            name: {
                "label": _CANDIDATE_POINT_LABELS.get(name, name),
                "coordinates": point,
                "distance": None,
                "is_inside": False,
                "marker_color": "red",
                "matched_fixed_point": None,
                "hit_fixed_points": [],
            }
            for name, point in candidates.items()
        },
    }


def _check_candidates_in_fixed_areas(
    candidates,
    camera_info,
    radius=_DEFAULT_EMISSION_POINT_RADIUS,
    min_hits=1,
):
    """讓每個候選點獨立比對所有固定參考點，再彙整整體命中結果。"""
    if not candidates:
        return _empty_candidate_match({}, radius)

    fixed_points = _get_fixed_reference_points(camera_info)
    if not fixed_points:
        return _empty_candidate_match(candidates, radius)

    candidate_details = {}
    hit_references = {}
    for name, candidate_coord in candidates.items():
        reference_distances = []
        for fixed_point in fixed_points:
            fixed_coord = fixed_point["coordinates"]
            distance = math.hypot(
                candidate_coord[0] - fixed_coord[0],
                candidate_coord[1] - fixed_coord[1],
            )
            reference_distances.append({
                "index": fixed_point["index"],
                "point_name": fixed_point["point_name"],
                "coordinates": fixed_coord,
                "wgs84_coords": fixed_point["wgs84_coords"],
                "distance": distance,
                "is_inside": distance <= radius,
            })

        nearest_reference = min(
            reference_distances,
            key=lambda item: (item["distance"], item["index"]),
        )
        point_hits = [item for item in reference_distances if item["is_inside"]]
        matched_reference = min(
            point_hits or reference_distances,
            key=lambda item: (item["distance"], item["index"]),
        )
        for hit in point_hits:
            hit_references[hit["index"]] = {
                "point_name": hit["point_name"],
                "coordinates": hit["coordinates"],
                "wgs84_coords": hit["wgs84_coords"],
            }

        candidate_details[name] = {
            "label": _CANDIDATE_POINT_LABELS.get(name, name),
            "coordinates": candidate_coord,
            "distance": round(matched_reference["distance"], 2),
            "is_inside": bool(point_hits),
            "marker_color": "green" if point_hits else "red",
            "matched_fixed_point": {
                "point_name": matched_reference["point_name"],
                "coordinates": matched_reference["coordinates"],
                "wgs84_coords": matched_reference["wgs84_coords"],
            },
            "hit_fixed_points": [
                {
                    "point_name": item["point_name"],
                    "coordinates": item["coordinates"],
                    "wgs84_coords": item["wgs84_coords"],
                    "distance": round(item["distance"], 2),
                }
                for item in sorted(
                    point_hits,
                    key=lambda item: (item["distance"], item["index"]),
                )
            ],
            "_distance": nearest_reference["distance"],
        }

    hit_count = sum(1 for details in candidate_details.values() if details["is_inside"])
    overall_hit = hit_count >= min_hits
    matched_candidate = min(
        candidate_details,
        key=lambda name: (
            0 if candidate_details[name]["is_inside"] else 1,
            candidate_details[name]["_distance"],
            _candidate_priority(name),
        ),
    )
    matched_details = candidate_details[matched_candidate]
    matched_reference = matched_details.get("matched_fixed_point") or {}
    min_distance = matched_details["_distance"]

    cleaned_candidate_details = {
        name: {key: value for key, value in details.items() if key != "_distance"}
        for name, details in candidate_details.items()
    }
    return {
        "is_inside": overall_hit,
        "point_name": matched_reference.get("point_name"),
        "offset_distance": (
            0.0
            if overall_hit
            else round(max(0.0, min_distance - radius), 2)
        ),
        "fixed_coordinates": (
            matched_reference.get("coordinates") if overall_hit else None
        ),
        "matched_candidate": matched_candidate,
        "candidate_coordinates": matched_details["coordinates"],
        "hit_count": hit_count,
        "min_distance": round(min_distance, 2),
        "radius": radius,
        "hit_reference_points": [
            hit_references[index]
            for index in sorted(hit_references)
        ],
        "candidates": cleaned_candidate_details,
    }

def _detection_match_sort_key(result):
    min_distance = result.get("min_distance")
    if min_distance is None:
        min_distance = float("inf")
    confidence = result.get("detection_conf")
    if confidence is None:
        confidence = 0.0
    return (
        0 if result.get("is_inside") else 1,
        -result.get("hit_count", 0),
        min_distance,
        -confidence,
    )

def _create_marked_uncropped_image(image_path, filename, crops_data, camera_info=None):
    image = cv2.imread(image_path)
    fixed_point_info = {}  # 建立用來儲存所有點位資訊的 Dict
    
    if image is None:
        receive_logger.warning(f"警告：cv2 讀不到原圖 (img is None)，路徑：{image_path}")
        return image_path, fixed_point_info

    image_height, image_width = image.shape[:2]
    marked_count = 0
    has_stage2_crop = any(
        isinstance(data, dict) and data.get("stage2_enabled", False)
        for data in crops_data.values()
    )
    fixed_reference_points = (
        _get_fixed_reference_points(camera_info) if has_stage2_crop else []
    )
    emission_point_radius = _get_emission_point_radius(camera_info)
    reference_areas = [
        {
            "point_name": point["point_name"],
            "coordinates": point["coordinates"],
            "radius": emission_point_radius,
            "color": "royal_blue",
            "color_bgr": _FIXED_REFERENCE_AREA_COLOR_BGR,
            "opacity": _FIXED_REFERENCE_AREA_OPACITY,
            "filled": True,
        }
        for point in fixed_reference_points
    ]

    # 所有固定座標範圍先畫在同一張 overlay，再一次混合成 40% 不透明度；
    # 圓面填滿寶藍色；選中固定點的圓心與名稱在所有結果彙整後標示。
    fixed_area_overlay = image.copy()
    for point in fixed_reference_points:
        cv2.circle(
            fixed_area_overlay,
            point["coordinates"],
            emission_point_radius,
            _FIXED_REFERENCE_AREA_COLOR_BGR,
            -1,
            lineType=cv2.LINE_AA,
        )

    if fixed_reference_points:
        image = cv2.addWeighted(
            fixed_area_overlay,
            _FIXED_REFERENCE_AREA_OPACITY,
            image,
            1.0 - _FIXED_REFERENCE_AREA_OPACITY,
            0,
        )
        receive_logger.info(
            f"[{_EMISSION_POINT_METHOD_NAME}] 已畫出 {len(fixed_reference_points)} 個"
        )

    # 改用 items() 取得 crop_filename，用來作為 fixed_point_info 的 Key
    for crop_filename, data in crops_data.items():
        if not data.get("stage2_enabled", False):
            continue

        raw_ratio = data.get("raw_ratio", {})
        offset_x = _safe_int(data.get("crop_x", 0))
        offset_y = _safe_int(data.get("crop_y", 0))
        stage1_bbox = _resolve_stage1_bbox(data)
        crop_width = None
        crop_height = None
        crop_image_path = data.get("crop_image_path")
        if crop_image_path:
            crop_image = cv2.imread(crop_image_path)
            if crop_image is not None:
                crop_height, crop_width = crop_image.shape[:2]
        detections = _get_ratio_detections(raw_ratio)
        has_stage2_lowest_point = any(
            isinstance(detection, dict)
            and _normalize_candidate_point(detection.get("lowest_point")) is not None
            for detection in detections
        )
        detection_results = []

        if has_stage2_lowest_point:
            for detection_index, detection in enumerate(detections):
                local_candidates = _get_detection_candidate_points(
                    detection,
                    crop_width=crop_width,
                    crop_height=crop_height,
                )
                if not local_candidates:
                    continue

                marker_bbox = _offset_bbox_xyxy(
                    detection.get("box"),
                    offset_x,
                    offset_y,
                )
                marker_bbox_source = "stage2_detection_bbox"
                if marker_bbox is None:
                    marker_bbox = stage1_bbox
                    marker_bbox_source = "stage1_bbox"

                original_candidates = _offset_candidate_points(
                    local_candidates,
                    offset_x,
                    offset_y,
                )
                result = _check_candidates_in_fixed_areas(
                    original_candidates,
                    camera_info,
                    radius=emission_point_radius,
                    min_hits=1,
                )
                confidence = _finite_float(detection.get("conf"))
                has_complete_three_points = all(
                    name in original_candidates
                    for name in (
                        "mask_lowest",
                        "bbox_bottom_left",
                        "bbox_bottom_right",
                    )
                )
                result.update({
                    "detection_index": detection_index,
                    "detection_id": detection.get("id", detection_index),
                    "detection_conf": confidence if confidence is not None else 0.0,
                    "decision_mode": (
                        _EMISSION_POINT_DECISION_MODE
                        if has_complete_three_points
                        else _STAGE2_PARTIAL_DECISION_MODE
                    ),
                    "candidate_source": "stage2",
                    "fallback_reason": None,
                    "stage1_bbox_xyxy": None,
                    "marker_bbox_xyxy": marker_bbox,
                    "marker_bbox_source": marker_bbox_source,
                })
                detection_results.append(result)
        else:
            stage1_candidates = _get_stage1_bbox_candidate_points(
                stage1_bbox,
                image_width,
                image_height,
            )
            if stage1_candidates:
                if not raw_ratio:
                    fallback_reason = "stage2_no_response"
                elif not detections:
                    fallback_reason = "stage2_no_detection"
                else:
                    fallback_reason = "stage2_no_lowest_point"

                result = _check_candidates_in_fixed_areas(
                    stage1_candidates,
                    camera_info,
                    radius=emission_point_radius,
                    min_hits=1,
                )
                result.update({
                    "detection_index": None,
                    "detection_id": None,
                    "detection_conf": 0.0,
                    "decision_mode": _STAGE1_FALLBACK_DECISION_MODE,
                    "candidate_source": "stage1",
                    "fallback_reason": fallback_reason,
                    "stage1_bbox_xyxy": stage1_bbox,
                    "marker_bbox_xyxy": stage1_bbox,
                    "marker_bbox_source": "stage1_bbox",
                })
                detection_results.append(result)

        if not detection_results:
            receive_logger.info(
                f"檢測點 [{crop_filename}] 沒有有效的 Stage2 起煙點，"
                f"Stage1 bbox fallback 也無法建立候選點。"
            )
            continue

        selected = min(detection_results, key=_detection_match_sort_key)
        is_fixed = selected["is_inside"]
        point_name = selected["point_name"]
        offset_dist = selected["offset_distance"]
        fixed_coord = selected["fixed_coordinates"]
        candidate_coord = selected["candidate_coordinates"]
        matched_candidate = selected["matched_candidate"]
        candidate_source = selected["candidate_source"]
        candidate_results = {}
        marker_bbox_source = selected.get("marker_bbox_source")
        selected_marker_bbox = selected.get("marker_bbox_xyxy")

        for candidate_name, candidate_data in selected["candidates"].items():
            candidate_data = dict(candidate_data)
            point = _normalize_candidate_point(candidate_data.get("coordinates"))
            point_hit = bool(candidate_data.get("is_inside"))
            marker_color_name = "green" if point_hit else "red"
            marker_color = (0, 255, 0) if point_hit else (0, 0, 255)
            marker_coord = None
            marker_radius = _EMISSION_POINT_MARKER_RADIUS
            marker_bbox = selected_marker_bbox

            if point is None:
                receive_logger.warning(
                    f"[{_EMISSION_POINT_METHOD_NAME}] [{crop_filename}] "
                    f"候選點 [{candidate_name}] 座標無效，略過繪圖。"
                )
            elif 0 <= point[0] < image_width and 0 <= point[1] < image_height:
                # 紅／綠點都直接畫在實際候選座標；bbox 底角不得因圓形半徑向內移動。
                marker_coord = point
            else:
                receive_logger.warning(
                    f"[{_EMISSION_POINT_METHOD_NAME}] [{crop_filename}] "
                    f"候選點 [{candidate_name}] 座標 {point} 超出原圖，略過 {marker_color_name} 標記。"
                )

            candidate_data.update({
                "marker_coordinates": marker_coord,
                "marker_radius": marker_radius if marker_coord is not None else None,
                "marker_bbox_xyxy": marker_bbox,
                "marker_bbox_source": marker_bbox_source,
                "marker_color": marker_color_name,
            })
            candidate_results[candidate_name] = candidate_data

            if marker_coord is not None:
                cv2.circle(
                    image,
                    marker_coord,
                    marker_radius,
                    marker_color,
                    -1,
                    lineType=cv2.LINE_AA,
                )
                marked_count += 1

            matched_fixed_point = candidate_data.get("matched_fixed_point") or {}
            receive_logger.info(
                f"[{_EMISSION_POINT_METHOD_NAME}] [{crop_filename}] "
                f"{candidate_data.get('label', candidate_name)}={point} "
                f"marker={marker_coord} fixed={matched_fixed_point.get('point_name')} "
                f"distance={candidate_data.get('distance')} hit={point_hit} "
                f"color={marker_color_name}"
            )

        matched_candidate_data = candidate_results.get(matched_candidate, {})
        marker_coord = matched_candidate_data.get("marker_coordinates")
        marker_radius = matched_candidate_data.get("marker_radius")
        marker_bbox = matched_candidate_data.get("marker_bbox_xyxy")
        mask_coordinates = candidate_results.get("mask_lowest", {}).get("coordinates")
        legacy_coordinates = mask_coordinates or candidate_coord
        fixed_point_info[crop_filename] = {
            "inference_method": {
                "code": _EMISSION_POINT_METHOD_CODE,
                "name": _EMISSION_POINT_METHOD_NAME,
            },
            "coordinates": legacy_coordinates,
            "matched_candidate_coordinates": candidate_coord,
            "fixed_coordinates": fixed_coord,
            "marker_coordinates": marker_coord,
            "marker_radius": marker_radius,
            "marker_bbox_xyxy": marker_bbox,
            "marker_bbox_source": selected.get("marker_bbox_source"),
            "is_inside": is_fixed,
            "point_name": point_name,
            "offset_distance": offset_dist,
            "decision_mode": selected["decision_mode"],
            "candidate_source": candidate_source,
            "fallback_reason": selected["fallback_reason"],
            "stage1_bbox_xyxy": selected["stage1_bbox_xyxy"],
            "matched_candidate": matched_candidate,
            "hit_count": selected["hit_count"],
            "valid_point_count": len(candidate_results),
            "radius": selected["radius"],
            "hit_reference_points": selected.get("hit_reference_points", []),
            "reference_areas": reference_areas,
            "detection_id": selected["detection_id"],
            "detection_conf": selected["detection_conf"],
            "candidates": candidate_results,
        }

        summary_message = (
            f"[{_EMISSION_POINT_METHOD_NAME}] crop={crop_filename} "
            f"source={candidate_source} decision_mode={selected['decision_mode']} "
            f"detection_id={selected['detection_id']} radius={selected['radius']} "
            f"valid_points={len(candidate_results)} hit_count={selected['hit_count']} "
            f"result={'hit' if is_fixed else 'miss'} point_name={point_name} "
            f"offset_distance={offset_dist}"
        )
        if is_fixed:
            receive_logger.warning(summary_message)
        else:
            receive_logger.info(summary_message)

    if marked_count == 0 and not fixed_reference_points:
        receive_logger.info(
            f"原圖未找到可還原的 lowest_point 或 bbox 底部候選點，"
            f"通知沿用乾淨原圖：{image_path}"
        )
        return image_path, fixed_point_info

    image = draw_selected_fixed_points(image, fixed_point_info, receive_logger, camera_info)
    os.makedirs(MARKED_OUTPUT_FOLDER, exist_ok=True)
    marked_save_path = os.path.join(MARKED_OUTPUT_FOLDER, filename)
    cv2.imwrite(marked_save_path, image)
    receive_logger.info(f"成圖已另存至新資料夾：{marked_save_path}")
    
    # 回傳圖片路徑與整理好的字典
    return marked_save_path, fixed_point_info


def _format_emission_point_text(fixed_point_data, style="public", image_width=None):
    return format_emission_points(fixed_point_data, numbered_smoke_results, style, image_width)


def _write_distributed_config_payload(payload):
    if payload.get("format") != "distributed_config":
        raise ValueError("format must be distributed_config")

    common_settings = payload.get("common_settings")
    workstation_assignments = payload.get("workstation_assignments")
    camera_configs = payload.get("camera_configs")
    if not isinstance(common_settings, dict):
        raise ValueError("common_settings must be an object")
    if not isinstance(workstation_assignments, dict):
        raise ValueError("workstation_assignments must be an object")
    if not isinstance(camera_configs, dict):
        raise ValueError("camera_configs must be an object")

    common_settings = dict(common_settings)
    common_settings.pop("stage2_cams", None)
    common_settings.pop("upload_ip", None)

    camera_dir = os.path.join(DISTRIBUTED_CONFIG_DIR, "camera_configs")
    os.makedirs(camera_dir, exist_ok=True)

    utility._atomic_write_json(
        os.path.join(DISTRIBUTED_CONFIG_DIR, "common_settings.json"),
        common_settings,
    )
    utility._atomic_write_json(
        os.path.join(DISTRIBUTED_CONFIG_DIR, "workstation_assignments.json"),
        workstation_assignments,
    )

    expected_files = set()
    for filename, camera_config in camera_configs.items():
        if not isinstance(filename, str) or not filename.startswith("camera_channel_") or not filename.endswith(".json"):
            raise ValueError(f"invalid camera config filename: {filename}")
        if not isinstance(camera_config, dict):
            raise ValueError(f"camera config must be an object: {filename}")
        expected_files.add(filename)
        utility._atomic_write_json(os.path.join(camera_dir, filename), camera_config)

    for filename in os.listdir(camera_dir):
        if filename.startswith("camera_channel_") and filename.endswith(".json") and filename not in expected_files:
            os.remove(os.path.join(camera_dir, filename))


@app.route('/update_config', methods=['POST'])
# ---------------------------------------------------------
# 狀況 A：收到 JSON -> 更新 distributed_config
# ---------------------------------------------------------
def update_config():
    check_and_rotate_logs()
    if not request.is_json:
        return jsonify({'code': 'INVALID_PAYLOAD', 'message': 'Payload must be JSON'}), 400

    receive_logger.info("收到 /update_config 請求：處理設定檔更新...")
    try:
        new_config = request.get_json()
        _write_distributed_config_payload(new_config)
        
        # [新增] 同步更新記憶體中的全域變數，實現立即生效 (Hot Reload)
        global config, stage2_cams
        config = utility.load_config()
        stage2_cams = config.get('stage2_cams', [])
        api_request.reset_api_settings_cache()
        validate_notification_channels(config.get('cameras', []), send_error_notification=True)
        
        receive_logger.info(f"distributed_config 更新成功，攝影機數={len(config.get('cameras', []))}，二階數={len(stage2_cams)}。")
        return jsonify({'code': 'OK', 'message': 'Distributed configuration updated successfully.'}), 200
        
    except ValueError as e:
        receive_logger.error(f"設定檔格式錯誤: {e}")
        return jsonify({'code': 'INVALID_PAYLOAD', 'message': str(e)}), 400
    except Exception as e:
        receive_logger.error(f"更新設定檔失敗: {e}")
        return jsonify({'code': 'INTERNAL_ERROR', 'message': str(e)}), 500


@app.route('/receive_result', methods=['POST'])
# ---------------------------------------------------------
# 狀況 B：收到檔案 -> 處理圖片 (Image Upload) - 維持原邏輯
# ---------------------------------------------------------
def receive_result():
    check_and_rotate_logs()

    if 'file' in request.files:
        # receive_logger.info("收到檔案請求：處理圖片...") # 可依需求開啟 Log
        file = request.files['file']
        if file.filename == '':
            return jsonify({'error': 'No selected file'}), 400

        filename = secure_filename(file.filename)
        uploaded_bytes = file.read()
        metadata = image_metadata.safe_read_metadata_from_jpeg_bytes(uploaded_bytes)
        context = image_metadata.resolve_image_context(filename, metadata)
        base_name = context.get("group_id") or "_".join(filename.split('_')[:5])
        ip = context.get("camera_ip") or ".".join(filename.split('_')[0:4])
        ip_folder_name = ip.replace('.', '_')
        ip_folder_path = os.path.join(UPLOAD_FOLDER, ip_folder_name)
        frame_role = context.get("frame_role")
        is_cropped = frame_role == "cropped" or 'cropped' in filename

        if not os.path.exists(ip_folder_path):
            os.makedirs(ip_folder_path)

        try:
            # 這裡使用的 config 就是記憶體中最新熱更新的設定
            camera_info = next((cam for cam in config.get('cameras', []) if cam.get('ip') == ip), None)
            if not camera_info and ip == "127.0.0.1":
                camera_info = next((cam for cam in config.get('cameras', []) if cam.get('ip') == "127.0.0.1"), None)
            channel_number = int(camera_info['NVR_channel']) if camera_info else None
        except Exception:
            channel_number = None

        if is_cropped:
            target_folder = STAGE2_FOLDER if (channel_number and channel_number in stage2_cams) else STAGE1_FOLDER
            save_path = os.path.join(target_folder, filename)
        else:
            save_path = os.path.join(ip_folder_path, filename)

        os.makedirs(os.path.dirname(save_path), exist_ok=True)
        with open(save_path, 'wb') as out_file:
            out_file.write(uploaded_bytes)

        if base_name not in image_group_results:
            image_group_results[base_name] = {
                'cropped_images': [],
                'crops_data': {},
                'result': 0,
                'uncropped_image_path': None,
                'completed': False
            }

        # 處理裁切圖
        if is_cropped:
            if image_group_results[base_name].get('completed'):
                receive_logger.info(f"Group {base_name}已完成處理，忽略裁切圖: {filename}")
                return jsonify({'message': 'Group already processed'}), 200

            receive_logger.info(f"收到裁切圖: {filename}")
            crop_x, crop_y = _extract_crop_origin(context, request.form)
            stage2_enabled = channel_number in stage2_cams
            image_group_results[base_name].setdefault('crops_data', {})
            image_group_results[base_name]['crops_data'][filename] = {
                'crop_x': crop_x,
                'crop_y': crop_y,
                'stage1_bbox_xyxy': context.get('bbox_xyxy') if isinstance(context, dict) else None,
                'crop_image_path': save_path,
                'stage2_enabled': stage2_enabled
            }
            image_group_results[base_name]['cropped_images'].append(save_path)
            
            if stage2_enabled:
                receive_logger.info(f"開始傳送圖片至 亮度比例分類器 : {save_path}")
                l_ratio, raw_ratio = send_to_ratio_classifier(save_path)
                image_group_results[base_name]['crops_data'][filename]['raw_ratio'] = raw_ratio
                receive_logger.info(f"API 回傳的 raw_ratio 內容: {raw_ratio}")

                receive_logger.info(f"開始傳送圖片至 煙霧分類器 : {save_path}")
                l_cls, conf_cls, raw_cls = send_image_to_server(save_path)

                steam_pred = None
                smoke_pred = None
                if isinstance(raw_cls, list) and len(raw_cls) >= 3:
                    try:
                        steam_pred = round(float(raw_cls[1]) * 100, 2)
                        smoke_pred = round(float(raw_cls[2]) * 100, 2)
                        image_group_results[base_name]['steam_pred'] = steam_pred
                        image_group_results[base_name]['smoke_pred'] = smoke_pred
                    except Exception:
                        pass

                final_label = bcaf_dual_fuse(l_cls, float(conf_cls or 0.0), l_ratio, t_steam=T_STEAM, t_smoke=T_SMOKE)
                image_group_results[base_name]['crops_data'][filename]['retention_result'] = (
                    'smoke' if final_label == LABEL_SMOKE else
                    'steam' if final_label == LABEL_STEAM else 'unknown')
                image_group_results[base_name]['crops_data'][filename]['retention_total'] = context.get('total_cropped')

                image_group_results[base_name]['l_cls'] = l_cls
                image_group_results[base_name]['conf_cls'] = float(conf_cls or 0.0)
                image_group_results[base_name]['l_ratio'] = l_ratio
                image_group_results[base_name]['final_label'] = final_label

                if final_label == LABEL_SMOKE:
                    image_group_results[base_name]['result'] = 1
                    image_group_results[base_name]['smoke_confidence'] = smoke_pred if smoke_pred is not None else round(float(conf_cls or 0.0) * 100, 2)
                elif final_label == LABEL_STEAM:
                    image_group_results[base_name]['result'] = 0
                    image_group_results[base_name]['steam_confidence'] = steam_pred if steam_pred is not None else round(float(conf_cls or 0.0) * 100, 2)
                else:
                    image_group_results[base_name]['result'] = -1
                    receive_logger.info(f"BCAF 最終判定為 none/unknown，將跳過 WaterSmoke 詳細通知: {save_path}")

            return jsonify({'message': 'Cropped result received successfully', 'path': save_path}), 200
        
        else:
            if not image_group_results[base_name].get('cropped_images'):
                receive_logger.error(f"未找到對應的裁切圖資料，無法處理未裁切圖: {filename}")
                return jsonify({'error': 'Cropped image not received yet'}), 400

            final_notify_path = save_path
            fixed_point_data = {}  # 初始化空字典以防錯誤
            try:
                final_notify_path, fixed_point_data = _create_marked_uncropped_image(
                    save_path,
                    filename,
                    image_group_results[base_name].get('crops_data', {}),
                    camera_info
                )
                # 利用一開始解開的 context 作為 parent，保留 camera_ip 等來源資訊
                marked_metadata = image_metadata.build_marked_metadata(
                    parent_metadata=context,
                    filename=filename,
                    group_id=base_name,
                    fixed_point_info=fixed_point_data
                )
                image_metadata.maybe_attach_metadata_to_jpeg_file(final_notify_path, marked_metadata)
                receive_logger.info(f"成功將固定點資料 (Metadata) 寫入圖片 EXIF: {final_notify_path}")
            except Exception as e:
                receive_logger.error(f"還原座標畫圖或寫入 Metadata 失敗: {e}")

            image_group_results[base_name]['uncropped_image_path'] = final_notify_path
            image_group_results[base_name]['fixed_point_info'] = fixed_point_data
            result_value = image_group_results[base_name]['result']
            uncropped_image_path = image_group_results[base_name]['uncropped_image_path']
            steam_pred = image_group_results[base_name].get('steam_pred')
            smoke_pred = image_group_results[base_name].get('smoke_pred')
            pred_value = image_group_results[base_name].get('smoke_confidence', smoke_pred if result_value == 1 else steam_pred)
            function_switches = camera_info.get('function_switches', {}) if camera_info else {}
            emission_point_text = _format_emission_point_text(fixed_point_data, style="test")
            public_emission_point_text = _format_emission_point_text(fixed_point_data, style="public")

            if camera_info and function_switches.get('enable_smoke_notify', True) and emission_point_text:
                receive_logger.info(f"起煙點測試通知觸發：{emission_point_text}")
                threading.Thread(
                    target=discord_emission_point_test_notify,
                    args=(uncropped_image_path, ip, emission_point_text),
                ).start()

            if camera_info and function_switches.get('enable_smoke_notify', True):
                if channel_number in stage2_cams:
                    threading.Thread(target=telegram_smoke_notify, args=(uncropped_image_path, ip)).start()
                    threading.Thread(
                        target=discord_smoke_notify,
                        args=(uncropped_image_path, ip, 1, public_emission_point_text),
                    ).start()

                    enable_night_detection = function_switches.get('enable_night_detection', True)
                    allow_watersmoke = True
                    
                    if not enable_night_detection:
                        if is_within_night_detection(camera_info):
                            allow_watersmoke = False
                            receive_logger.info(f"二階通知略過：夜間檢測限制 ({function_switches.get('night_detection_start')} ~ {function_switches.get('night_detection_end')})")

                    if allow_watersmoke and result_value in (0, 1):
                        threading.Thread(target=telegram_smoke_notify_watersmoke, args=(uncropped_image_path, str(result_value), ip, pred_value)).start()
                        threading.Thread(
                            target=discord_smoke_notify_watersmoke,
                            args=(uncropped_image_path, str(result_value), ip, pred_value, public_emission_point_text),
                        ).start()
                else:
                    threading.Thread(target=telegram_smoke_notify, args=(uncropped_image_path, ip)).start()
                    threading.Thread(
                        target=discord_smoke_notify,
                        args=(uncropped_image_path, ip, 1, public_emission_point_text),
                    ).start()
            else:
                receive_logger.info(f"攝影機 {ip} 的通知功能已關閉，跳過通知發送。")

            if camera_info and function_switches.get('enable_google_upload', False):
                receive_logger.info(f"啟用了 enable_google_upload，發送下載請求。")
                try:
                    download_response = utility.send_download_request(ip, filename, image_context=context)
                    receive_logger.info(f"下載請求完成: {download_response}")
                except Exception as e:
                    receive_logger.error(f"發送下載請求時出錯: {e}")

            if camera_info and (camera_info.get('connection_type') or '').upper() == 'DRIVE':
                try:
                    record_detection(ReceiptStore(), base_name,
                                     image_group_results[base_name].get('crops_data', {}))
                except Exception:
                    receive_logger.exception('[DRIVE cleanup] 無法保存檢測完成紀錄，保留來源圖片')
            image_group_results[base_name]['completed'] = True

            def delayed_delete(group_key):
                if group_key in image_group_results:
                    del image_group_results[group_key]
                    receive_logger.info(f"已刪除 group: {group_key}")

            threading.Timer(5, delayed_delete, args=(base_name,)).start()

            return jsonify({'message': 'Uncropped result received successfully', 'path': save_path}), 200
            
    else:
        # 當不是傳送檔案時，直接回傳錯誤
        return jsonify({'error': 'Unknown request format or missing file part'}), 400


if __name__ == '__main__':
    start_daily_heartbeat_thread()
    app.run(host='0.0.0.0', port=5001)
