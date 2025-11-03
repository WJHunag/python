# api_request.py

import requests
import base64
import json
import traceback
import os
from datetime import datetime, timedelta

# ----------------------------
# 讀取外部 config（從 smoke_camera_config.json 的 "API_info" 字串陣列取值）
# ----------------------------
CONFIG_FILE = "smoke_camera_config.json"
try:
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        _cfg = json.load(f)
    api_info = _cfg.get("API_info", [])
    if len(api_info) >= 3:
        BASE_URL = api_info[0].rstrip("/") 
        USER     = api_info[1]             
        PWD      = api_info[2]              
    else:
        raise ValueError(f"'{CONFIG_FILE}' 中的 \"API_info\" 欄位數量不正確，請確認有 url、user、pwd 三個元素")
except Exception as e:
    raise RuntimeError(f"讀取 {CONFIG_FILE} 失敗：{e}")

COUNTER_FILE = "uid_counters.json"
MONITOR_UPLOAD_ENABLED = True  # 二階上傳開關
EXCLUDED_NVR_CHANNELS = {2, 3, 7, 11, 12, 14, 19, 21, 33, 38, 41, 42, 43, 44, 45, 46, 47, 48, 49, 50, 51, 52, 53, 54, 55, 56}

# ----------------------------
# 1. 讀取圖片並轉成 Base64
# ----------------------------
def image_to_base64(path: str) -> str:
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode("utf-8")

# ----------------------------
# 2. 自動產生 uid（batch_YYYYMMDD_NNN）
# ----------------------------
def generate_uid(prefix: str = "batch") -> str:
    today = datetime.now().strftime("%Y%m%d")
    if os.path.exists(COUNTER_FILE):
        with open(COUNTER_FILE, "r", encoding="utf-8") as f:
            counters = json.load(f)
    else:
        counters = {}
    n = counters.get(today, 0) + 1
    counters[today] = n
    with open(COUNTER_FILE, "w", encoding="utf-8") as f:
        json.dump(counters, f, ensure_ascii=False, indent=2)
    return f"{prefix}_{today}_{n:03d}"

# ----------------------------
# 3. 登入取得短效 JWT
# ----------------------------
def get_jwt_token() -> str:
    login_url = f"{BASE_URL}/api/v1/auth/login"
    resp = requests.post(
        login_url,
        json={"user_name": USER, "password": PWD},
        headers={"Content-Type": "application/json"}
    )
    resp.raise_for_status()
    j = resp.json()
    if j.get("resultcode") != "00":
        raise RuntimeError(f"登入失敗：{j.get('message')}")
    data = j.get("data")
    if isinstance(data, list) and len(data) > 0:
        first = data[0]
        if isinstance(first, dict):
            return first.get("token")
        elif isinstance(first, str):
            return first
    elif isinstance(data, dict):
        return data.get("token")
    elif isinstance(data, str):
        return data
    raise RuntimeError(f"無法解析 JWT token，data={data}")

# ----------------------------
# 4. 呼叫上傳黑煙監控資料 API
# ----------------------------
def upload_smoke_data(payload: dict) -> dict:
    try:
        # 建立 safe_payload，只替換 photo 欄位以免 log 被 Base64 塞爆
        safe_payload = json.loads(json.dumps(payload, ensure_ascii=False))
        if (
            "data" in safe_payload
            and isinstance(safe_payload["data"], list)
            and len(safe_payload["data"]) > 0
            and isinstance(safe_payload["data"][0], dict)
            and "photo" in safe_payload["data"][0]
        ):
            safe_payload["data"][0]["photo"] = "<省略 Base64 字串>"

        print("▶ 要送出的 safe payload（已省略 photo）：")
        print(json.dumps(safe_payload, ensure_ascii=False, indent=2))

        token = get_jwt_token()
        url = f"{BASE_URL}/api/v1/monitor/smoke"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }
        print(f"▶ POST 到：{url}")

        resp = requests.post(url, json=payload, headers=headers)
        print("▶ HTTP 狀態碼：", resp.status_code)

        resp_json = resp.json()
        print("▶ 伺服器回傳 JSON 型態：", type(resp_json))
        print("▶ 伺服器回傳 JSON 內容：", resp_json)

        resp.raise_for_status()
        return resp_json

    except Exception:
        print("❌ 上傳過程發生錯誤：")
        traceback.print_exc()
        raise

# ----------------------------
# 5. 對外呼叫：自動組裝並上傳監控資料
#    sdt = 現在時間 - 100 秒，edt = 現在時間
# ----------------------------
def upload_monitor_smoke(image_path: str,
                         location: str,
                         channel: int,
                         function_switches: dict,
                         gps: str,
                         chanel: str,
                         area: str) -> dict:
    """
    :param image_path:      圖片檔案路徑
    :param location:        camera_info['location']
    :param channel:         camera_info['NVR_channel']
    :param function_switches: camera_info['function_switches']
    :param gps:             camera_info['gps']，格式如 "24.214277,120.492440"
    :param chanel:          camera_info['chanel']，如 "Toward:west"
    :param area:            camera_info['Area']，如 "臺中市后里區"（長度 ≤ 7）
    """
    try:
        ch = int(channel)
    except Exception:
        ch = None

    if ch is not None and ch in EXCLUDED_NVR_CHANNELS:
        print(f"[INFO] channel={ch} 在排除清單，跳過對外 API 上傳 (location={location})")
        return {
            "ok": True,
            "skipped": True,
            "reason": "excluded_channel",
            "channel": ch,
            "location": location
        }

    if not MONITOR_UPLOAD_ENABLED:
        print(f"[INFO] MONITOR_UPLOAD_ENABLED=False，跳過上傳 (location={location})")
        return None

    # 1. 處理 gps → 拆逗號、去空白、轉 float 再格式化到 6 位小數
    gps_formatted = ""
    try:
        lat_str, lon_str = [s.strip() for s in gps.split(",")]
        lat = float(lat_str)
        lon = float(lon_str)
        gps_formatted = f"{lat:.6f},{lon:.6f}"
    except Exception:
        gps_formatted = ""  # 若解析失敗就留空

    # 2. station_area 直接用 area，截到最長 7 個字
    station_area = area[:7]

    # 3. station_name / devs_name 必填、且長度 ≤ 20
    station_name = location[:20]
    devs_name    = location[:20]

    uid = generate_uid()

    # 計算 sdt = 現在時間 - 100 秒，edt = 現在時間
    now = datetime.now()
    start_time = now - timedelta(seconds=100)
    sdt_str = start_time.strftime("%Y/%m/%d %H:%M:%S")
    edt_str = now.strftime("%Y/%m/%d %H:%M:%S")

    payload = {
        "uid": uid,
        "udt": now.strftime("%Y/%m/%d %H:%M:%S"),
        "data": [
            {
                "station_no":   "UNKNOWN",
                "station_name": station_name,
                "station_area": station_area,
                "devs_no":      str(channel),
                "devs_name":    devs_name,
                "devs_type":    "M" if function_switches.get("enable_camera_ptz", False) else "S",
                "devs_place":   "",
                "gps":          gps_formatted,
                "image_type":   "01",
                "photo":        image_to_base64(image_path),
                "url":          "",
                "chanel":       chanel[:50],
                "sdt":          sdt_str,
                "edt":          edt_str,
                "notes":        ""
            }
        ]
    }

    return upload_smoke_data(payload)
