from flask import Flask, request, jsonify
import os
from bot import smoke_notify, smoke_notify_watersmoke
from werkzeug.utils import secure_filename
import threading
import json
import requests
import utility

app = Flask(__name__)
UPLOAD_FOLDER = './received_results'
STAGE1_FOLDER = './received_results/stage1'
STAGE2_FOLDER = './received_results/stage2'

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
            print(f"伺服器回應: {response_data}")
            return response_data
    return None

@app.route('/receive_result', methods=['POST'])
def receive_result():
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
    channel_number = int(camera_info['NVR_channel']) if camera_info else None

    # 判斷儲存位置：裁剪圖片放入 stage 資料夾，未裁剪圖放入 IP 資料夾
    if 'cropped' in filename:
        target_folder = STAGE2_FOLDER if channel_number in stage2_cams else STAGE1_FOLDER
        save_path = os.path.join(target_folder, filename)
    else:
        # 未裁剪圖儲存在以 IP 命名的資料夾
        save_path = os.path.join(ip_folder_path, filename)

    # 儲存文件
    file.save(save_path)

    if base_name not in image_group_results:
        image_group_results[base_name] = {
            'cropped_images': [], 
            'result': 0,
            'uncropped_image_path': None
        }

    if 'cropped' in filename:
        print(f"收到裁切圖: {filename}")
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
    else:
        # 未裁剪圖處理
        image_group_results[base_name]['uncropped_image_path'] = save_path
        result = image_group_results[base_name]['result']
        uncropped_image_path = image_group_results[base_name]['uncropped_image_path']
        steam_pred = image_group_results[base_name].get('steam_pred')
        smoke_pred = image_group_results[base_name].get('smoke_pred')
        pred_value = image_group_results[base_name].get('smoke_confidence', smoke_pred if result == 1 else steam_pred)

        # Stage 1和Stage 2分別處理
        if channel_number in stage2_cams:
            # 執行伺服器處理並通知
            threading.Thread(target=smoke_notify, args=(uncropped_image_path, ip)).start()
            threading.Thread(target=smoke_notify_watersmoke, args=(uncropped_image_path, str(result), ip, pred_value)).start()
        else:
            # 僅通知
            threading.Thread(target=smoke_notify, args=(uncropped_image_path, ip)).start()

        # 刪除圖片：裁剪圖片
        for cropped_image in image_group_results[base_name]['cropped_images']:
            threading.Thread(target=utility.delayed_remove, args=(cropped_image,)).start()
        
        # 未裁剪圖片已經在IP資料夾中，無需刪除
        del image_group_results[base_name]

    return jsonify({'message': 'Result received successfully', 'path': save_path}), 200

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5001)
