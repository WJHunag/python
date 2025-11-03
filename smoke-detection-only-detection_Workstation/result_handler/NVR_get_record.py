import json
import os
from threading import Thread
from flask import Flask, request
from requests.auth import HTTPDigestAuth
import requests
from tqdm import tqdm
from datetime import datetime, timedelta
import re

# 初始化 Flask 應用
app = Flask(__name__)

# 讀取 JSON 配置檔案
def load_camera_config(config_path):
    with open(config_path, 'r', encoding='utf-8') as file:
        return json.load(file)

# 動態選擇 NVR IP 和 Channel
def select_nvr_and_channel(camera):
    nvr_channel = int(camera['NVR_channel'])
    if nvr_channel <= 20:
        return camera['NVR_IP'], nvr_channel
    else:
        return camera['NVR2_IP'], nvr_channel - 20

# 從檔名中提取時間並計算範圍
def calculate_time_from_filename(filename):
    match = re.search(r'_(\d{8})_(\d{6})_with_boxes', filename)
    if not match:
        raise ValueError("Filename format is incorrect. Expected format: *_YYYYMMDD_HHMMSS_with_boxes.jpg")

    # 解析時間
    date_part = match.group(1)  # YYYYMMDD
    time_part = match.group(2)  # HHMMSS
    datetime_str = f"{date_part} {time_part}"
    timestamp = datetime.strptime(datetime_str, "%Y%m%d %H%M%S")

    # 計算時間範圍
    start_time = (timestamp - timedelta(minutes=5)).strftime('%Y-%m-%d %H:%M:%S')
    end_time = timestamp.strftime('%Y-%m-%d %H:%M:%S')
    return start_time, end_time

# 下載 NVR 視訊
def download_nvr_video(nvr_ip, channel, start_time, end_time, username, password, file_type='mp4', max_retries=3, google_drive_folder_id="1IqKWbRqyCNIFSGNNWirl-lhSqvGyH2fx"):
    save_folder = "NVR_record"
    os.makedirs(save_folder, exist_ok=True)

    safe_start_time = start_time.replace(' ', '_').replace(':', '-')
    save_path = os.path.join(save_folder, f"channel_{channel}_{safe_start_time}.{file_type}")

    url = f"http://{nvr_ip}/cgi-bin/loadfile.cgi?action=startLoad&channel={channel}&startTime={start_time.replace(' ', '%20')}&endTime={end_time.replace(' ', '%20')}&Types={file_type}"

    attempts = 0
    while attempts < max_retries:
        try:
            response = requests.get(url, auth=HTTPDigestAuth(username, password), stream=True, timeout=60)
            if response.status_code == 200:
                total_size = int(response.headers.get('content-length', 0))
                progress_bar = tqdm(total=total_size, unit='iB', unit_scale=True)
                with open(save_path, 'wb') as file:
                    for data in response.iter_content(8192):
                        progress_bar.update(len(data))
                        file.write(data)
                progress_bar.close()
                print(f"Download complete: {save_path}")

                # 如果啟用了 Google Drive 上傳，執行上傳
                if google_drive_folder_id:
                    file_name = os.path.basename(save_path)
                    upload_to_google_drive(file_name, save_path, "video/mp4", google_drive_folder_id)

                return
            else:
                print(f"Failed to download, status code: {response.status_code}")
                break
        except Exception as e:
            attempts += 1
            print(f"Download attempt {attempts}/{max_retries} failed: {e}")

    print("Failed to download after maximum retries.")

def upload_to_google_drive(file_name, save_path, file_type, folder_id):
    """
    上傳文件至 Google 雲端
    """
    try:
        from utility import google_upload_file  # 確保 utility 中有此函式
        upload_result = google_upload_file(file_name, save_path, file_type, folder_id=folder_id)
        print(f"Upload complete. Result: {upload_result}")
    except Exception as e:
        print(f"An error occurred during upload: {e}")

# Flask API 接口
@app.route('/download', methods=['POST'])
def handle_download():
    data = request.json
    camera_ip = data.get('camera_ip')
    image_filename = data.get('image_filename')

    # 從檔名中提取時間
    try:
        start_time, end_time = calculate_time_from_filename(image_filename)
    except ValueError as e:
        return {"error": str(e)}, 400

    # 加載配置
    config = load_camera_config('smoke_camera_config.json')
    camera_list = config['cameras']

    # 根據 IP 找到對應攝影機
    camera = next((cam for cam in camera_list if cam['ip'] == camera_ip), None)
    if not camera:
        return {"error": "Camera not found"}, 404

    # 動態選擇 NVR 和 Channel
    nvr_ip, adjusted_channel = select_nvr_and_channel(camera)

    # 開始下載
    Thread(target=download_nvr_video, args=(
        nvr_ip,
        adjusted_channel,
        start_time,
        end_time,
        config['NVR_account_password'][0],
        config['NVR_account_password'][1],
    )).start()

    return {"message": f"Download started for {camera_ip} from {start_time} to {end_time}"}, 200

if __name__ == '__main__':
    app.run(host='0.0.0.0',port=5002)
