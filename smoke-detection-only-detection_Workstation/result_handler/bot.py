import configparser
from datetime import datetime
import requests
import utility
import json
import logging
import threading
import os
from .api_request import upload_monitor_smoke


"""
用於LINE notify通知
"""

# 通知次數記錄檔路徑
NOTIFICATION_LOG_PATH = "daily_notification_log.json"
# 初始化日誌記錄
LOG_FOLDER = './logs'
os.makedirs(LOG_FOLDER, exist_ok=True)  # 確保資料夾存在
file_lock = threading.Lock()

def setup_bot_logger():
    global log_file 
    current_date = datetime.now().strftime('%Y-%m-%d')
    log_file = os.path.join(LOG_FOLDER, f'{current_date}_bot.log')
    logger = logging.getLogger("bot_logger")
    logger.handlers = []  # 清除所有舊 handler
    logger.propagate = False  # 防止日誌傳遞到 root logger

    file_handler = logging.FileHandler(log_file, encoding='utf-8')
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(logging.Formatter('[%(asctime)s] %(levelname)s: %(message)s'))
    logger.addHandler(file_handler)

    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(logging.Formatter('[%(asctime)s] %(levelname)s: %(message)s'))
    logger.addHandler(console_handler)

    logger.setLevel(logging.INFO)
    return logger

bot_logger = setup_bot_logger()

def check_and_rotate_logs():
    """
    檢查日期是否變更，並切換到新日期的日誌檔案
    """
    global log_file, bot_logger
    new_date = datetime.now().strftime('%Y-%m-%d')
    current_log_date = log_file.split('/')[-1].split('_')[0]  # 獲取當前日誌文件日期

    if new_date != current_log_date:
        log_file = os.path.join(LOG_FOLDER, f"{new_date}_bot.log")

        # 創建新的處理器
        file_handler = logging.FileHandler(log_file, encoding='utf-8')
        file_handler.setFormatter(logging.Formatter('[%(asctime)s] %(levelname)s: %(message)s'))

        # 清除舊處理器
        while bot_logger.hasHandlers():
            bot_logger.handlers.clear()

        # 添加新的處理器
        bot_logger.addHandler(file_handler)
        bot_logger.addHandler(logging.StreamHandler())

        bot_logger.info("日期變更，切換到新日誌檔案。")

# 初始化通知計數
def init_notification_log():
    """
    初始化通知記錄。如果記錄檔不存在，或者當天記錄不存在，則新增當天記錄。
    """
    if not os.path.exists(NOTIFICATION_LOG_PATH):
        reset_notification_log()  # 如果檔案不存在，直接重置
        return

    with open(NOTIFICATION_LOG_PATH, "r", encoding="utf-8") as f:
        log_data = json.load(f)

    current_date = datetime.now().strftime("%Y-%m-%d")
    history = log_data.get("history", [])

    # 確認是否已經有當天的記錄
    if history and any(entry["date"] == current_date for entry in history):
        print("當天記錄已存在，不進行重置")
        return

    # 如果當天記錄不存在，新增記錄
    reset_notification_log()

def reset_notification_log():
    """
    新增當天記錄到通知日誌中，不刪除其他歷史記錄。
    """
    current_date = datetime.now().strftime("%Y-%m-%d")
    new_entry = {
        "date": current_date,
        "stage1_count": 0,
        "stage2_count": 0,
        "watersmoke_count": 0
    }

    log_data = {"history": []}

    if os.path.exists(NOTIFICATION_LOG_PATH):
        with open(NOTIFICATION_LOG_PATH, "r", encoding="utf-8") as f:
            log_data = json.load(f)

    # 確保當天記錄唯一
    if not any(entry["date"] == current_date for entry in log_data.get("history", [])):
        log_data["history"].append(new_entry)

    with open(NOTIFICATION_LOG_PATH, "w", encoding="utf-8") as f:
        json.dump(log_data, f, ensure_ascii=False, indent=4)
        
# 更新通知計數
def update_notification_log(stage, is_watersmoke=False):
    """
    更新每日通知計數，並保留歷史紀錄。
    """
    with file_lock:  # 確保線性寫入
        with open(NOTIFICATION_LOG_PATH, "r", encoding="utf-8") as f:
            log_data = json.load(f)
        
        current_date = datetime.now().strftime("%Y-%m-%d")
        if not log_data["history"] or log_data["history"][-1]["date"] != current_date:
            reset_notification_log()
            with open(NOTIFICATION_LOG_PATH, "r", encoding="utf-8") as f:
                log_data = json.load(f)

        if stage == 1:
            log_data["history"][-1]["stage1_count"] += 1
        elif stage == 2 and not is_watersmoke:
            log_data["history"][-1]["stage2_count"] += 1
        elif is_watersmoke:
            log_data["history"][-1]["watersmoke_count"] += 1

        with open(NOTIFICATION_LOG_PATH, "w", encoding="utf-8") as f:
            json.dump(log_data, f, ensure_ascii=False, indent=4)

# 修改通知函數
def send_notification(token, message, img_path=None, stage=None, is_watersmoke=False):
    check_and_rotate_logs()

    headers = {"Authorization": "Bearer " + token}
    data = {"message": message}
    files = {"imageFile": open(img_path, "rb")} if img_path else None

    try:
        bot_logger.info(f"正在發送通知，使用的 Token: {token}, 階段: {stage}, 是否水氣: {is_watersmoke}")
        response = requests.post(
            "https://notify-api.line.me/api/notify",
            headers=headers,
            data=data,
            files=files
        )

        update_notification_log(stage, is_watersmoke=is_watersmoke)

        # 僅記錄伺服器回應內容
        bot_logger.info(f"通知回應: 狀態碼 {response.status_code}, 回應: {response.text}")

    except Exception as e:
        bot_logger.error(f"通知發送過程中發生錯誤: {e}")
    finally:
        if files:
            files["imageFile"].close()

def send_telegram_notification(telegram_token, chat_id, message, img_path=None, stage=None, is_watersmoke=False):
    """
    使用 Telegram Bot API 發送通知訊息或圖片
    """
    check_and_rotate_logs()
    try:
        bot_logger.info(f"正在使用 Telegram 發送通知，Token: {telegram_token}, Chat ID: {chat_id}, 階段: {stage}, 是否水氣: {is_watersmoke}")
        if img_path and os.path.exists(img_path):
            url = f"https://api.telegram.org/bot{telegram_token}/sendPhoto"
            data = {"chat_id": chat_id, "caption": message}
            with open(img_path, "rb") as photo_file:
                files = {"photo": photo_file}
                response = requests.post(url, data=data, files=files)
        else:
            url = f"https://api.telegram.org/bot{telegram_token}/sendMessage"
            data = {"chat_id": chat_id, "text": message}
            response = requests.post(url, data=data)
        
        update_notification_log(stage, is_watersmoke=is_watersmoke)
        bot_logger.info(f"Telegram 通知回應: 狀態碼 {response.status_code}, 回應: {response.text}")
    except Exception as e:
        bot_logger.error(f"Telegram 通知發送過程中發生錯誤: {e}")
    
def send_discord_notification(webhook_url, message, img_path=None, stage=None, is_watersmoke=False):
    """
    使用 Discord webhook 傳送通知訊息
    :param webhook_url: Discord webhook 的 URL
    :param message: 傳送的文字訊息
    :param img_path: 圖片檔案路徑 (選用)
    :param stage: 檢測階段 (方便日誌紀錄)
    :param is_watersmoke: 是否為水氣通知 (方便日誌紀錄)
    """
    check_and_rotate_logs()
    try:
        bot_logger.info(f"使用 Discord 發送通知，Webhook URL: {webhook_url}, 階段: {stage}, 是否水氣: {is_watersmoke}")
        
        # 準備要送出的資料，Discord webhook 透過 JSON 格式可傳送 content 內容
        data = {"content": message}
        
        # 如果有圖片檔案，則使用 multipart/form-data 上傳檔案
        if img_path and os.path.exists(img_path):
            with open(img_path, "rb") as fp:
                files = {"file": fp}
                response = requests.post(webhook_url, data=data, files=files)
        else:
            # 沒有圖片時，直接以 JSON 送出訊息
            response = requests.post(webhook_url, json=data)
        
        update_notification_log(stage, is_watersmoke=is_watersmoke)
        bot_logger.info(f"Discord 通知回應: 狀態碼 {response.status_code}, 回應: {response.text}")
    except Exception as e:
        bot_logger.error(f"Discord 通知發送過程中發生錯誤: {e}")


def smoke_notify(img_path, ip, stage=1):
    """
    發送煙霧檢測通知，並記錄通知時間。
    :param img_path: 圖片路徑
    :param ip: 攝影機 IP
    :param stage: 檢測階段（1 或 2）
    """
    exclude_list = utility.get_json_data('exclude_list')
    cameras_info = utility.get_json_data('cameras')

    # 加載二階攝影機列表
    with open('smoke_camera_config.json', 'r', encoding='utf-8') as f:
        config = json.load(f)
    stage2_cams = config.get('stage2_cams', [])

    config_parser = configparser.ConfigParser()
    config_parser.read("./config.ini")
    notify_token = config_parser['token']['notify']
    notify_token_smoke = config_parser['token']['notifysmoke']
    river_notify_token = config_parser['token']['notifyriver']

    # 檢查攝影機資訊
    camera_info = next((cam for cam in cameras_info if cam['ip'] == ip), None)
    if not camera_info:
        bot_logger.warning(f"無法找到 IP: {ip} 的攝影機資訊")
        return

    # 若攝影機在 exclude_list 中，僅發送到 river_notify_token
    if utility.is_camera_excluded(ip, exclude_list, cameras_info):
        bot_logger.info(f"攝影機 {ip} 屬於 exclude_list，僅發送到 river_notify_token")
        now_timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        location = camera_info['location']
        message = f"檢測到煙霧\n時間：{now_timestamp}\n位置：{location}"
        send_notification(river_notify_token, message, img_path)
        return

    # 若是二階攝影機，僅發送到 notify，避免重複發送到 notifysmoke
    if int(camera_info['NVR_channel']) in stage2_cams:
        bot_logger.info(f"二階攝影機 {ip} (頻道 {camera_info['NVR_channel']})，發送通知至一階群組")
        now_timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        location = camera_info['location']
        message = f"檢測到煙霧\n時間：{now_timestamp}\n位置：{location}"
        send_notification(notify_token, message, img_path,1)
        return

    # 非二階攝影機的情況，發送到一階群組和煙霧專用群組
    now_timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    location = camera_info['location']
    message = f"檢測到煙霧\n時間：{now_timestamp}\n位置：{location}"

    bot_logger.info(f"準備發送煙霧通知: 時間={now_timestamp}, 位置={location}, IP={ip}, Stage={stage}")
    send_notification(notify_token, message, img_path,1)  # 發送到一階群組
    send_notification(notify_token_smoke, message, img_path,2)  # 發送到煙霧專用群組

def smoke_notify_camera(ip):
    cameras_info = utility.get_json_data('cameras')
    config = configparser.ConfigParser()
    config.read("./config.ini")
    notify_token = config['token']['notifyerror']

    camera = next((camera for camera in cameras_info if camera['ip'] == ip), None)
    notify_info = camera['notify'] if camera else '未知位置'
    message = f"\n位置：{notify_info}\n攝影機已斷線三十分鐘"

    send_notification(notify_token, message)

def smoke_notify_watersmoke(img_path, result, ip, confidence):
    cameras_info = utility.get_json_data('cameras')
    exclude_list = utility.get_json_data('exclude_list')

    if utility.is_camera_excluded(ip, exclude_list, cameras_info):
        bot_logger.info(f"攝影機 {ip} 在排除清單中，跳過通知處理。")
        return

    camera = next((camera for camera in cameras_info if camera['ip'] == ip), None)
    if not camera:
        bot_logger.info(f"找不到 IP 為 {ip} 的攝影機配置，無法處理通知。")
        return

    stage2_confidence = camera.get("stage2_confidence", 0.6)
    config_parser = configparser.ConfigParser()
    config_parser.read("./config.ini")
    notify_token_water = config_parser['token']['notifywatersmoke']
    notify_token_smoke = config_parser['token']['notifysmoke']
    now_timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    location = camera['location'] if camera else '未知地點'

    bot_logger.info(f"開始處理通知，結果: {result}, 信心值: {confidence}, 攝影機位置: {location}")

    if result == "0":
        message = f"檢測到水氣\n時間：{now_timestamp}\n位置：{location}"
        bot_logger.info(f"發送水氣通知: {message}")
        send_notification(notify_token_water, message, img_path, stage=2, is_watersmoke=True)
    elif result == "1":
        if confidence > (stage2_confidence*100):
            message = f"檢測到煙霧.\n時間：{now_timestamp}\n位置：{location}"
            bot_logger.info(f"二階檢測信心值 (信心值: {confidence}, 閾值: {stage2_confidence})，發送煙霧通知: {message}")
            send_notification(notify_token_smoke, message, img_path, stage=2)
        else:
            bot_logger.info(f"二階檢測信心值過低 (信心值: {confidence}, 閾值: {stage2_confidence})，不發送通知。")

def telegram_smoke_notify(img_path, ip, stage=1):
    """
    使用 Telegram Bot 發送煙霧檢測通知，並記錄通知時間。
    :param img_path: 圖片路徑
    :param ip: 攝影機 IP
    :param stage: 檢測階段（1 或 2）
    """
    exclude_list = utility.get_json_data('exclude_list')
    cameras_info = utility.get_json_data('cameras')

    # 載入二階攝影機列表
    with open('smoke_camera_config.json', 'r', encoding='utf-8') as f:
        config_smoke = json.load(f)
    stage2_cams = config_smoke.get('stage2_cams', [])

    config_parser = configparser.ConfigParser()
    config_parser.read("./config.ini")
    telegram_token = config_parser['telegram']['token']
    telegram_notify_chat_id = config_parser['telegram']['notify_chat_id']
    telegram_smoke_chat_id = config_parser['telegram']['smoke_chat_id']
    telegram_river_chat_id = config_parser['telegram']['river_chat_id']

    # 檢查攝影機資訊
    camera_info = next((cam for cam in cameras_info if cam['ip'] == ip), None)
    if not camera_info:
        bot_logger.warning(f"無法找到 IP: {ip} 的攝影機資訊")
        return

    now_timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    location = camera_info.get('location', '未知位置')
    message = f"檢測到煙霧\n時間：{now_timestamp}\n位置：{location}"

    # 若攝影機在排除清單中，僅發送到河川通知群組
    if utility.is_camera_excluded(ip, exclude_list, cameras_info):
        bot_logger.info(f"攝影機 {ip} 屬於排除清單，僅發送到 Telegram 河川通知群組")
        send_telegram_notification(telegram_token, telegram_river_chat_id, message, img_path)
        return

    # 若為二階攝影機，僅發送到一階通知群組
    if int(camera_info['NVR_channel']) in stage2_cams:
        bot_logger.info(f"二階攝影機 {ip} (頻道 {camera_info['NVR_channel']})，發送通知至 Telegram 一階群組")
        send_telegram_notification(telegram_token, telegram_notify_chat_id, message, img_path, stage=1)
        return

    # 非二階攝影機的情況，發送到一階群組與煙霧專用群組
    bot_logger.info(f"準備發送 Telegram 煙霧通知: 時間={now_timestamp}, 位置={location}, IP={ip}, Stage={stage}")
    send_telegram_notification(telegram_token, telegram_notify_chat_id, message, img_path, stage=1)
    send_telegram_notification(telegram_token, telegram_smoke_chat_id, message, img_path, stage=2)

def telegram_smoke_notify_camera(ip):
    """
    使用 Telegram Bot 發送攝影機斷線通知
    :param ip: 攝影機 IP
    """
    cameras_info = utility.get_json_data('cameras')
    config_parser = configparser.ConfigParser()
    config_parser.read("./config.ini")
    telegram_token = config_parser['telegram']['token']
    error_chat_id = config_parser['telegram']['error_chat_id']

    camera = next((camera for camera in cameras_info if camera['ip'] == ip), None)
    notify_info = camera['notify'] if camera else '未知位置'
    message = f"\n位置：{notify_info}\n攝影機已斷線三十分鐘"

    bot_logger.info(f"使用 Telegram 發送攝影機斷線通知，IP: {ip}, 位置: {notify_info}")
    send_telegram_notification(telegram_token, error_chat_id, message)

def telegram_smoke_notify_watersmoke(img_path, result, ip, confidence):
    """
    使用 Telegram Bot 發送水氣或煙霧檢測通知
    :param img_path: 圖片路徑
    :param result: 檢測結果 ("0" 表示水氣, "1" 表示煙霧)
    :param ip: 攝影機 IP
    :param confidence: 檢測信心值
    """
    cameras_info = utility.get_json_data('cameras')
    exclude_list = utility.get_json_data('exclude_list')

    if utility.is_camera_excluded(ip, exclude_list, cameras_info):
        bot_logger.info(f"攝影機 {ip} 在排除清單中，跳過 Telegram 通知處理。")
        return

    camera = next((camera for camera in cameras_info if camera['ip'] == ip), None)
    if not camera:
        bot_logger.info(f"找不到 IP 為 {ip} 的攝影機配置，無法處理 Telegram 通知。")
        return

    stage2_confidence = camera.get("stage2_confidence", 0.6)
    config_parser = configparser.ConfigParser()
    config_parser.read("./config.ini")
    telegram_token = config_parser['telegram']['token']
    telegram_watersmoke_chat_id = config_parser['telegram']['watersmoke_chat_id']
    telegram_smoke_chat_id = config_parser['telegram']['smoke_chat_id']

    now_timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    location = camera.get('location', '未知地點')

    bot_logger.info(f"開始處理 Telegram 通知，結果: {result}, 信心值: {confidence}, 攝影機位置: {location}")

    if result == "0":
        message = f"檢測到水氣\n時間：{now_timestamp}\n位置：{location}"
        bot_logger.info(f"發送 Telegram 水氣通知: {message}")
        send_telegram_notification(telegram_token, telegram_watersmoke_chat_id, message, img_path, stage=2, is_watersmoke=True)
    elif result == "1":
        if confidence > (stage2_confidence * 100):
            message = f"檢測到煙霧.\n時間：{now_timestamp}\n位置：{location}"
            bot_logger.info(f"二階檢測信心值 (信心值: {confidence}, 閾值: {stage2_confidence})，發送 Telegram 煙霧通知: {message}")
            send_telegram_notification(telegram_token, telegram_smoke_chat_id, message, img_path, stage=2)
        else:
            bot_logger.info(f"二階檢測信心值過低 (信心值: {confidence}, 閾值: {stage2_confidence})，不發送 Telegram 通知。")

def discord_smoke_notify(img_path, ip, stage=1):
    """
    使用 Discord webhook 發送煙霧檢測通知，並記錄通知時間。
    :param img_path: 圖片檔案路徑
    :param ip: 攝影機 IP
    :param stage: 檢測階段（預設為 1）
    """
    exclude_list = utility.get_json_data('exclude_list')
    cameras_info = utility.get_json_data('cameras')
    
    # 載入二階攝影機列表
    with open('smoke_camera_config.json', 'r', encoding='utf-8') as f:
        config_smoke = json.load(f)
    stage2_cams = config_smoke.get('stage2_cams', [])
    
    config_parser = configparser.ConfigParser()
    config_parser.read("./config.ini")
    # 請在 config.ini 的 [discord] 區段設定以下鍵值：
    # webhook_notify : 一階群組通知 Webhook URL
    # webhook_smoke  : 煙霧專用群組 Webhook URL
    # webhook_river  : 河川（或排除清單）通知 Webhook URL
    discord_notify = config_parser['discord']['webhook_notify']
    discord_smoke = config_parser['discord']['webhook_smoke']
    discord_river = config_parser['discord']['webhook_river']
    
    # 取得攝影機資訊
    camera_info = next((cam for cam in cameras_info if cam['ip'] == ip), None)
    if not camera_info:
        bot_logger.warning(f"無法找到 IP: {ip} 的攝影機資訊")
        return
    
    now_timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    location = camera_info.get('location', '未知位置')
    message = f"檢測到煙霧\n時間：{now_timestamp}\n位置：{location}"
    
    # 若攝影機在排除清單中，僅發送到河川通知
    if utility.is_camera_excluded(ip, exclude_list, cameras_info):
        bot_logger.info(f"攝影機 {ip} 屬於排除清單，僅發送到 Discord 河川通知群組")
        send_discord_notification(discord_river, message, img_path)
        return
    
    # 若為二階攝影機，僅發送到一階群組
    if int(camera_info.get('NVR_channel', 0)) in stage2_cams:
        bot_logger.info(f"二階攝影機 {ip} (頻道 {camera_info['NVR_channel']})，發送通知至 Discord 一階群組")
        send_discord_notification(discord_notify, message, img_path, stage=1)
        return
    
    # 非二階攝影機的情況，發送到一階群組與煙霧專用群組
    bot_logger.info(f"準備發送 Discord 煙霧通知: 時間={now_timestamp}, 位置={location}, IP={ip}, Stage={stage}")
    send_discord_notification(discord_notify, message, img_path, stage=1)
    send_discord_notification(discord_smoke, message, img_path, stage=2)
    #[MOD]
    gps_value = camera_info.get('gps', '')         
    chanel_value = camera_info.get('chanel', '')   
    area_value  = camera_info.get('Area', '')  
    try:
        api_result = upload_monitor_smoke(
            image_path=img_path,
            location=camera_info['location'],
            channel=int(camera_info['NVR_channel']),
            function_switches=camera_info.get('function_switches', {}),
            gps=gps_value,       
            chanel=chanel_value,
            area=area_value  
        )
        if api_result is None:
            bot_logger.info("MONITOR_UPLOAD_ENABLED=False，已跳過上傳。")
        else:
            bot_logger.info(f"API 上傳回傳：{api_result}")
    except Exception as e:
        bot_logger.error(f"API 上傳發生錯誤：{e}")

def discord_smoke_notify_camera(ip):
    """
    使用 Discord webhook 發送攝影機斷線通知。
    :param ip: 攝影機 IP
    """
    cameras_info = utility.get_json_data('cameras')
    config_parser = configparser.ConfigParser()
    config_parser.read("./config.ini")
    # 請在 config.ini 的 [discord] 區段設定 webhook_error，供斷線通知使用
    discord_error = config_parser['discord']['webhook_error']
    
    camera = next((cam for cam in cameras_info if cam['ip'] == ip), None)
    notify_info = camera.get('notify', '未知位置') if camera else '未知位置'
    message = f"\n位置：{notify_info}\n攝影機已斷線三十分鐘"
    
    bot_logger.info(f"使用 Discord 發送攝影機斷線通知，IP: {ip}, 位置: {notify_info}")
    send_discord_notification(discord_error, message)

def discord_smoke_notify_watersmoke(img_path, result, ip, confidence):
    """
    使用 Discord webhook 發送水氣或煙霧檢測通知。
    :param img_path: 圖片檔案路徑
    :param result: 檢測結果 ("0" 表示水氣, "1" 表示煙霧)
    :param ip: 攝影機 IP
    :param confidence: 檢測信心值
    """
    cameras_info = utility.get_json_data('cameras')
    exclude_list = utility.get_json_data('exclude_list')
    
    if utility.is_camera_excluded(ip, exclude_list, cameras_info):
        bot_logger.info(f"攝影機 {ip} 在排除清單中，跳過 Discord 通知處理。")
        return
    
    camera = next((cam for cam in cameras_info if cam['ip'] == ip), None)
    if not camera:
        bot_logger.info(f"找不到 IP 為 {ip} 的攝影機配置，無法處理 Discord 通知。")
        return
    
    stage2_confidence = camera.get("stage2_confidence", 0.6)
    config_parser = configparser.ConfigParser()
    config_parser.read("./config.ini")
    # 請在 config.ini 的 [discord] 區段設定下列鍵值：
    # webhook_watersmoke : 水氣專用通知 Webhook URL
    # webhook_smoke       : 煙霧專用通知 Webhook URL
    discord_watersmoke = config_parser['discord']['webhook_watersmoke']
    discord_smoke = config_parser['discord']['webhook_smoke']
    
    now_timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    location = camera.get('location', '未知地點')
    
    bot_logger.info(f"開始處理 Discord 通知，結果: {result}, 信心值: {confidence}, 攝影機位置: {location}")
    
    if result == "0":
        message = f"檢測到水氣\n時間：{now_timestamp}\n位置：{location}"
        bot_logger.info(f"發送 Discord 水氣通知: {message}")
        send_discord_notification(discord_watersmoke, message, img_path, stage=2, is_watersmoke=True)
    elif result == "1":
        if confidence > (stage2_confidence * 100):
            message = f"檢測到煙霧.\n時間：{now_timestamp}\n位置：{location}"
            bot_logger.info(f"二階檢測信心值 (信心值: {confidence}, 閾值: {stage2_confidence})，發送 Discord 煙霧通知: {message}")
            send_discord_notification(discord_smoke, message, img_path, stage=2)
            #[MOD]
            try:
                api_result = upload_monitor_smoke(
                    image_path=img_path,
                    location=cameras_info['location'],
                    channel=int(cameras_info['NVR_channel']),
                     function_switches=cameras_info.get('function_switches', {})
                )
                if api_result is None:
                    bot_logger.info("MONITOR_UPLOAD_ENABLED=False，已跳過上傳。")
                else:
                    # 這裡就可以看到真正的回傳內容
                    bot_logger.info(f"API 上傳回傳：{api_result}")
            except Exception as e:
                bot_logger.error(f"API 上傳發生錯誤：{e}")
            else:
                bot_logger.info(
                    f"二階檢測信心值過低 (信心值: {confidence}, 閾值: {stage2_confidence})，不發送 Discord 通知。"
                    )



if __name__ == "__main__":
    smoke_notify("./1025_0.jpg", "127.0.0.1")
