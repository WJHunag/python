from flask import Flask, request, jsonify
import os
from .bot import smoke_notify, smoke_notify_watersmoke, init_notification_log,telegram_smoke_notify, telegram_smoke_notify_watersmoke,discord_smoke_notify,discord_smoke_notify_watersmoke
from werkzeug.utils import secure_filename
import threading
import json
import requests
import logging
from datetime import datetime
import utility

app = Flask(__name__)
UPLOAD_FOLDER = './received_results'
STAGE1_FOLDER = './received_results/stage1'
STAGE2_FOLDER = './received_results/stage2'
LOG_FOLDER = './logs'
os.makedirs(LOG_FOLDER, exist_ok=True)  # 確保資料夾存在

init_notification_log()

# 初始化日誌記錄

def setup_server_logger():
    """
    初始化伺服器日誌記錄
    """
    global log_file  # 宣告 log_file 為全局變數
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

def check_and_rotate_logs():
    """
    檢查日期是否變更，並切換到新日期的日誌檔案。
    """
    global log_file, receive_logger
    new_date = datetime.now().strftime('%Y-%m-%d')
    current_log_date = log_file.split('/')[-1].split('_')[0]  # 獲取當前日誌日期

    if new_date != current_log_date:
        log_file = os.path.join(LOG_FOLDER, f'{new_date}_receive.log')

        # 創建新的處理器
        file_handler = logging.FileHandler(log_file, encoding='utf-8')
        file_handler.setFormatter(logging.Formatter('[%(asctime)s] %(levelname)s: %(message)s'))

        # 清除舊處理器
        while receive_logger.hasHandlers():
            receive_logger.handlers.clear()

        # 添加新的處理器
        receive_logger.addHandler(file_handler)
        receive_logger.addHandler(logging.StreamHandler())

        receive_logger.info("日期變更，切換到新日誌檔案。")

def is_within_night_detection(camera_info):
    """
    檢查當前時間是否在夜間檢測時間範圍內。
    """
    night_start_str = camera_info['function_switches'].get('night_detection_start', "00:00")
    night_end_str = camera_info['function_switches'].get('night_detection_end', "00:00")
    
    # 解析時間字串為 datetime.time 對象
    night_start = datetime.strptime(night_start_str, "%H:%M").time()
    night_end = datetime.strptime(night_end_str, "%H:%M").time()
    current_time = datetime.now().time()
    
    # 判斷是否在夜間檢測時間範圍內
    if night_start <= night_end:
        # 同一天的時間範圍
        return night_start <= current_time <= night_end
    else:
        # 跨越午夜的時間範圍
        return current_time >= night_start or current_time <= night_end

# 創建基本文件夾
for folder in [UPLOAD_FOLDER, STAGE1_FOLDER, STAGE2_FOLDER]:
    if not os.path.exists(folder):
        os.makedirs(folder)
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER

# 載入 JSON 配置
with open('smoke_camera_config.json', 'r', encoding='utf-8') as f:
    config = json.load(f)
stage2_cams = config.get('stage2_cams', [])

# 存储每组图片的结果
image_group_results = {}

def send_image_to_server(image_path):
    url = config.get('smoke_classifier_ip', "http://127.0.0.1:5000/infer")
    if image_path.lower().endswith(('.png', '.jpg', '.jpeg', '.bmp')):
        with open(image_path, 'rb') as image_file:
            files = {'file': image_file}
            response = requests.post(url, files=files)
            response_data = response.json()
            receive_logger.info(f"二階檢測伺服器回應: {response_data}")
            print(f"伺服器回應: {response_data}")
            return response_data
    return None

@app.route('/receive_result', methods=['POST'])
def receive_result():
    check_and_rotate_logs()  # 檢查日期變更
    receive_logger.info("收到新的結果處理請求")
    if 'file' not in request.files:
        return jsonify({'error': 'No file part'}), 400
    file = request.files['file']
    if file.filename == '':
        return jsonify({'error': 'No selected file'}), 400

    filename = secure_filename(file.filename)
    base_name = "_".join(filename.split('_')[:5])
    ip_part = filename.split('_')[0:4]
    ip = ".".join(ip_part)
    ip_folder_name = ip.replace('.', '_')
    ip_folder_path = os.path.join(UPLOAD_FOLDER, ip_folder_name)

    # 確保IP資料夾存在
    if not os.path.exists(ip_folder_path):
        os.makedirs(ip_folder_path)

    # 獲取攝影機的 NVR_channel 編號
    camera_info = next((cam for cam in config['cameras'] if cam['ip'] == ip), None)
    if not camera_info and ip == "127.0.0.1":
        camera_info = next((cam for cam in config['cameras'] if cam['ip'] == "127.0.0.1"), None)
    channel_number = int(camera_info['NVR_channel']) if camera_info else None

    # 判斷儲存位置：裁剪圖片放入 stage 資料夾，未裁剪圖放入 IP 資料夾
    if 'cropped' in filename:
        target_folder = STAGE2_FOLDER if channel_number in stage2_cams else STAGE1_FOLDER
        save_path = os.path.join(target_folder, filename)
    else:
        save_path = os.path.join(ip_folder_path, filename)

    # 儲存文件
    file.save(save_path)

    # 若該組資料尚未建立，先建立一個新的記錄（增加 completed flag）
    if base_name not in image_group_results:
        image_group_results[base_name] = {
            'cropped_images': [],
            'result': 0,
            'uncropped_image_path': None,
            'completed': False
        }

    # 處理裁切圖
    if 'cropped' in filename:
        # 若該組已完成處理，則忽略後續裁切圖
        if image_group_results[base_name].get('completed'):
            receive_logger.info(f"Group {base_name}已完成處理，忽略裁切圖: {filename}")
            return jsonify({'message': 'Group already processed'}), 200

        receive_logger.info(f"收到裁切圖: {filename}")
        image_group_results[base_name]['cropped_images'].append(save_path)
        # 僅在 stage2_cams 中執行伺服器處理
        if channel_number in stage2_cams:
            response_text = send_image_to_server(save_path)
            if response_text and len(response_text) >= 3:
                steam_pred = round(float(response_text[1]) * 100, 2)
                smoke_pred = round(float(response_text[2]) * 100, 2)
                image_group_results[base_name]['steam_pred'] = steam_pred
                image_group_results[base_name]['smoke_pred'] = smoke_pred
                if response_text[0] == "1":
                    image_group_results[base_name]['result'] = 1
                    image_group_results[base_name]['smoke_confidence'] = smoke_pred
        return jsonify({'message': 'Cropped result received successfully', 'path': save_path}), 200
    else:
        # 處理未裁切圖，必須先有裁切圖資料
        if not image_group_results[base_name].get('cropped_images'):
            receive_logger.error(f"未找到對應的裁切圖資料，無法處理未裁切圖: {filename}")
            return jsonify({'error': 'Cropped image not received yet'}), 400

        image_group_results[base_name]['uncropped_image_path'] = save_path
        result_value = image_group_results[base_name]['result']
        uncropped_image_path = image_group_results[base_name]['uncropped_image_path']
        steam_pred = image_group_results[base_name].get('steam_pred')
        smoke_pred = image_group_results[base_name].get('smoke_pred')
        pred_value = image_group_results[base_name].get('smoke_confidence', smoke_pred if result_value == 1 else steam_pred)

        # 處理通知
        if camera_info and camera_info['function_switches'].get('enable_smoke_notify', True):
            enable_night_detection = camera_info['function_switches'].get('enable_night_detection', True)
            if not enable_night_detection:
                if is_within_night_detection(camera_info):
                    receive_logger.info(
                        f"通知跳過：夜間檢測關閉，當前時間在夜間範圍內 ({camera_info['function_switches'].get('night_detection_start')} ~ {camera_info['function_switches'].get('night_detection_end')})"
                    )
                    return jsonify({'message': 'Notification skipped due to time range'}), 200
            if channel_number in stage2_cams:
                #threading.Thread(target=smoke_notify, args=(uncropped_image_path, ip)).start()
                #threading.Thread(target=smoke_notify_watersmoke, args=(uncropped_image_path, str(result_value), ip, pred_value)).start()
                threading.Thread(target=telegram_smoke_notify, args=(uncropped_image_path, ip)).start()
                threading.Thread(target=telegram_smoke_notify_watersmoke, args=(uncropped_image_path, str(result_value), ip, pred_value)).start()
                threading.Thread(target=discord_smoke_notify, args=(uncropped_image_path, ip)).start()
                threading.Thread(target=discord_smoke_notify_watersmoke, args=(uncropped_image_path, str(result_value), ip, pred_value)).start()
            else:
                #threading.Thread(target=smoke_notify, args=(uncropped_image_path, ip)).start()
                threading.Thread(target=telegram_smoke_notify, args=(uncropped_image_path, ip)).start()
                threading.Thread(target=discord_smoke_notify, args=(uncropped_image_path, ip)).start()
        else:
            receive_logger.info(f"攝影機 {ip} 的通知功能已關閉，跳過通知發送。")

        if camera_info and camera_info['function_switches'].get('enable_google_upload', False):
            receive_logger.info(f"啟用了 enable_google_upload，發送下載請求。")
            try:
                download_response = utility.send_download_request(ip, filename)
                receive_logger.info(f"下載請求完成: {download_response}")
            except Exception as e:
                receive_logger.error(f"發送下載請求時出錯: {e}")

        # 標記該組已完成處理，並延遲刪除該組資料
        image_group_results[base_name]['completed'] = True

        def delayed_delete(group_key):
            if group_key in image_group_results:
                del image_group_results[group_key]
                receive_logger.info(f"已刪除 group: {group_key}")

        threading.Timer(5, delayed_delete, args=(base_name,)).start()

        return jsonify({'message': 'Uncropped result received successfully', 'path': save_path}), 200


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5001)
