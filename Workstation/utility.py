import os
import cv2
import ctypes
import requests
from requests.auth import HTTPDigestAuth
from urllib.parse import quote
import numpy as np
import time
import copy
import threading
import logging
from datetime import datetime, timedelta
#from bot import smoke_notify
from googleapiclient.http import MediaFileUpload
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from google.auth.transport.requests import Request
import pickle
import tkinter as tk
from tkinter import messagebox
from tkinter import INSERT, filedialog, END
from enum import Enum
import re
import hashlib
import json
import hmac
import socket
import tempfile




regex = '''^(25[0-5]|2[0-4][0-9]|[0-1]?[0-9][0-9]?)\.( 
            25[0-5]|2[0-4][0-9]|[0-1]?[0-9][0-9]?)\.( 
            25[0-5]|2[0-4][0-9]|[0-1]?[0-9][0-9]?)\.( 
            25[0-5]|2[0-4][0-9]|[0-1]?[0-9][0-9]?)'''

"""
函式庫
"""

DISTRIBUTED_CONFIG_DIR = os.path.join("smoke_config", "distributed_config")
COMMON_SETTINGS_FILE = "common_settings.json"
WORKSTATION_ASSIGNMENTS_FILE = "workstation_assignments.json"
CAMERA_CONFIGS_DIR = "camera_configs"

class JsonFormatter(logging.Formatter):
    """
    A custom formatter to output logs in JSON format.
    Each log record will be stored as a JSON object with useful fields.
    """
    def format(self, record):
        log_record = {
            "timestamp": self.formatTime(record, self.datefmt),
            "level": record.levelname,
            "message": record.getMessage(),
            "name": record.name,
            "function": record.funcName,
            "lineno": record.lineno
        }
        if record.exc_info:
            log_record["exception"] = self.formatException(record.exc_info)
        return json.dumps(log_record, ensure_ascii=False)

#========global========
def save_img(img, ip):
    folder_date = datetime.now().strftime("%Y-%m-%d")
    target_path=os.path.join(os.getcwd(),"image",ip)
    if not os.path.exists(target_path):
        os.makedirs(target_path)
    now = datetime.now().strftime("%Y-%m-%d-%H-%M-%S")
    save_path = target_path + "/" + str(now) + ".jpg"
    img = cv2.cvtColor(img, cv2.COLOR_RGB2BGR)
    cv2.imwrite(save_path, img)

def switch_tab(event, detect_page, preview_page, train_page):
    selected_tab = event.widget.index("current")
    if selected_tab == 0:
        detect_page.video_canvas.delete("all")
        detect_page.video_canvas.update()
    if selected_tab != 0:
        preview_page.img_view.delete('1.0', END)


#==========page.py===========
def start_hub_center_process():
    """
    啟動多支攝影機檢測程式，然後結束當前程式。
    可在 page.py 的 start_all_camera 中呼叫此函式。
    """
    import subprocess
    import sys
    subprocess.Popen(["python", "./multi_source_vision_hub_center.py"])
    sys.exit()

def find_free_preset_index(presets_dict):
    """
    尋找一個未使用的預置點編號（除編號5）。
    """
    for i in range(1, 301):
        if i == 5:  # 跳過5
            continue
        if str(i) not in presets_dict:
            return str(i)
    return None

def validate_camera_focus_time(value):
    """驗證時間輸入，限制在 1 到 60 分鐘"""
    if not value:  # 如果輸入為空，允許通過
        return True
    if not value.isdigit():  # 檢查是否為數字
        return False
    num = int(value)
    return 1 <= num <= 60  # 僅接受 1 到 60 的數值

def get_focus_time(self):
    """從 Entry 獲取追焦時間，默認為 1 分鐘"""
    try:
        return int(self.focus_time_entry.get())
    except ValueError:
        print("追焦時間輸入無效，使用默認值 1 分鐘")
        return 1
    
def validate_confidence_input(value):
    """檢查輸入是否為 0 到 100 的數值"""
    if value == "":  # 允許空值，便於刪除輸入
        return True
    if not value.isdigit():
        return False
    num = int(value)
    return 0 <= num <= 100


def load_confidence_settings(filename):
    """從 JSON 文件載入信心值"""
    try:
        with open(filename, 'r', encoding='utf-8') as f:
            data = json.load(f)
        stage1_confidence = data.get('stage1_confidence', 0.50)
        stage2_confidence = data.get('stage2_confidence', 0.50)
        return stage1_confidence, stage2_confidence
    except (FileNotFoundError, json.JSONDecodeError) as e:
        print(f"載入信心值時發生錯誤: {e}")
        return 0.50, 0.50  # 預設值

def save_camera_settings(camera_ip, settings, filename, patrol_points=None, patrol_time=None):
    """保存攝影機設定到 distributed_config 的單支 camera 檔。"""
    try:
        data = load_config()
    except (FileNotFoundError, json.JSONDecodeError, ValueError):
        data = {"cameras": []}

    # 更新對應攝影機的設定
    for camera in data.get('cameras', []):
        if camera['ip'] == camera_ip:
            # 從 settings 中移除信心值，並保存到根部
            stage1_confidence = settings.pop('stage1_confidence', None)
            stage2_confidence = settings.pop('stage2_confidence', None)

            if stage1_confidence is not None:
                camera['stage1_confidence'] = stage1_confidence
            if stage2_confidence is not None:
                camera['stage2_confidence'] = stage2_confidence

            # 更新 function_switches 設定
            camera['function_switches'] = settings

            # 移除 function_switches 中的 patrol_points 和 patrol_time
            camera['function_switches'].pop('patrol_points', None)
            camera['function_switches'].pop('patrol_time', None)

            # 更新根層的巡弋設定
            if patrol_points is not None:
                camera['patrol_points'] = patrol_points
            if patrol_time is not None:
                camera['patrol_time'] = patrol_time
            break
    else:
        print(f"未找到攝影機 IP: {camera_ip}，無法保存設定。")
        return

    try:
        save_distributed_camera_config(camera)
        print(f"攝影機 {camera_ip} 的設定已成功保存。")
    except IOError as e:
        print(f"保存攝影機設定時發生錯誤: {e}")

def toggle_stage1_detection(stage1_var, stage2_var):
    """控制一階檢測的啟用狀態，停用二階檢測如果一階未啟用"""
    if stage1_var.get() == 1:  # 啟用一階檢測
        stage2_var.set(0)  # 保證二階檢測預設未選中
    else:  # 停用一階檢測
        stage2_var.set(0)  # 停用二階檢測

def toggle_stage2_detection(stage1_var, stage2_var):
    """控制二階檢測的啟用條件，必須啟用一階"""
    if stage1_var.get() != 1:  # 如果一階未啟用
        print("請先啟用一階檢測。")
        stage2_var.set(0)  # 停用二階檢測

#==========json相關==========

def _read_json_file(path):
    with open(path, 'r', encoding='utf-8') as f:
        return json.load(f)


def _atomic_write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=".__tmp__", suffix=".json", dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, path)
    finally:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass


def _camera_channel(camera):
    try:
        return int(camera.get("NVR_channel"))
    except (TypeError, ValueError):
        return 10 ** 9


def _derive_stage2_cams(cameras):
    stage2_cams = []
    seen = set()
    for camera in cameras or []:
        switches = camera.get("function_switches", {}) if isinstance(camera, dict) else {}
        if not isinstance(switches, dict) or not switches.get("stage2_detection", False):
            continue
        try:
            channel = int(camera.get("NVR_channel"))
        except (TypeError, ValueError):
            continue
        if channel not in seen:
            seen.add(channel)
            stage2_cams.append(channel)
    return sorted(stage2_cams)


def get_server_port(config):
    try:
        return int(config.get("server_port", 5020))
    except (TypeError, ValueError):
        return 5020


def get_upload_url(config):
    """由 server_ip/server_port 統一組出 routing server 的圖片上傳 URL。"""
    server_ip = str(config.get("server_ip", "127.0.0.1")).strip() or "127.0.0.1"
    return f"http://{server_ip}:{get_server_port(config)}/upload"


def _assignment_camera_ids(entry):
    if isinstance(entry, dict):
        raw = (entry.get("polling_cameras") or []) + (entry.get("fixed_cameras") or [])
    elif isinstance(entry, list):
        raw = entry
    else:
        raw = []
    ids = set()
    for value in raw:
        try:
            ids.add(int(value))
        except (TypeError, ValueError):
            pass
    return ids


def get_workstation_identity_candidates():
    candidates = []
    for env_name in ("WORKSTATION_IP", "WORKSTATION_KEY", "COMPUTERNAME", "HOSTNAME"):
        value = os.getenv(env_name)
        if value:
            candidates.append(value.strip())
    try:
        hostname = socket.gethostname()
        candidates.append(hostname)
        candidates.append(socket.getfqdn())
        candidates.append(socket.gethostbyname(hostname))
        for value in socket.gethostbyname_ex(hostname)[2]:
            candidates.append(value)
    except Exception:
        pass

    normalized = []
    seen = set()
    for value in candidates:
        if not value:
            continue
        for candidate in (value, value.split(":", 1)[0]):
            if candidate and candidate not in seen:
                seen.add(candidate)
                normalized.append(candidate)
    return normalized


def _load_all_camera_configs(distributed_root):
    camera_dir = os.path.join(distributed_root, CAMERA_CONFIGS_DIR)
    cameras = []
    for filename in sorted(os.listdir(camera_dir)):
        if not (filename.startswith("camera_channel_") and filename.endswith(".json")):
            continue
        camera = _read_json_file(os.path.join(camera_dir, filename))
        if "NVR_channel" in camera:
            camera["NVR_channel"] = str(int(camera["NVR_channel"]))
        cameras.append(camera)
    return sorted(cameras, key=lambda cam: (_camera_channel(cam), str(cam.get("ip", ""))))


def load_distributed_config(distributed_root=DISTRIBUTED_CONFIG_DIR, filter_for_workstation=True):
    """
    從 distributed_config 目錄組出執行期設定。
    磁碟上的 common_settings.json 不保存 stage2_cams；此函式會由 camera 設定動態產生。
    """
    common_path = os.path.join(distributed_root, COMMON_SETTINGS_FILE)
    assignments_path = os.path.join(distributed_root, WORKSTATION_ASSIGNMENTS_FILE)
    camera_dir = os.path.join(distributed_root, CAMERA_CONFIGS_DIR)
    if not (os.path.isfile(common_path) and os.path.isfile(assignments_path) and os.path.isdir(camera_dir)):
        raise FileNotFoundError(f"Distributed config is not initialized: {distributed_root}")

    config = _read_json_file(common_path)
    assignments = _read_json_file(assignments_path)
    cameras = _load_all_camera_configs(distributed_root)

    if filter_for_workstation and assignments:
        candidates = set(get_workstation_identity_candidates())
        matched_keys = [key for key in assignments if key in candidates or key.split(":", 1)[0] in candidates]
        if matched_keys:
            keep_ids = set()
            filtered_assignments = {}
            for key in matched_keys:
                entry = assignments[key]
                filtered_assignments[key] = entry
                keep_ids |= _assignment_camera_ids(entry)
            cameras = [cam for cam in cameras if _camera_channel(cam) in keep_ids]
            assignments = filtered_assignments
        else:
            logging.warning(
                "No workstation assignment matched local identity %s; using all distributed cameras.",
                sorted(candidates),
            )

    config["workstation_ip_mapping"] = assignments
    config["cameras"] = cameras
    config["stage2_cams"] = _derive_stage2_cams(cameras)
    return config


def save_distributed_camera_config(camera):
    channel = int(camera["NVR_channel"])
    path = os.path.join(DISTRIBUTED_CONFIG_DIR, CAMERA_CONFIGS_DIR, f"camera_channel_{channel:03d}.json")
    camera = copy.deepcopy(camera)
    camera["NVR_channel"] = str(channel)
    _atomic_write_json(path, camera)


def load_config(config_path='smoke_camera_config.json'):
    """
    載入程式所需的設定。正式部署僅讀 distributed_config，不再 fallback 舊版 smoke_local_camera_config.json。
    """
    try:
        return load_distributed_config()
    except FileNotFoundError:
        logging.error(f"Distributed config not found: {DISTRIBUTED_CONFIG_DIR}")
        raise
    except json.JSONDecodeError as e:
        logging.error(f"Failed to parse distributed JSON config: {e}")
        raise ValueError("Invalid distributed JSON config format")


def get_json_data(key, json_path="./smoke_camera_config.json"):
    """
    從JSON文件中讀取指定的數據。

    :param json_path: JSON文件的路徑，默認為"./config.json"。
    :param key: 要讀取的數據鍵。
    :return: 指定鍵對應的數據。
    """
    data = load_config(json_path)
    return data[key]

def get_connection_mode(json_path="./smoke_camera_config.json"):
    """
    讀取全域的連接模式。
    
    :param json_path: JSON文件的路徑，默認為"./smoke_camera_config.json"。
    :return: 'camera' 或 'NVR'
    """
    data = get_json_data('connection_mode', json_path)
    return data

def get_camera_ip(json_path="./smoke_camera_config.json"):
    """
    從JSON文件中讀取所有攝影機IP地址。
    
    :param json_path: JSON文件的路徑，默認為"./smoke_camera_config.json"。
    :return: 包含所有IP地址的列表。
    """
    cameras = get_json_data('cameras', json_path)
    ip_list = [camera['ip'] for camera in cameras]
    return ip_list
    
def save_json_data(key, data, filename):
    """
    將 data 儲存至指定的 JSON 檔案中，並更新 key 對應的欄位。
    """
    try:
        with open(filename, 'r', encoding='utf-8') as f:
            json_data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        json_data = {}

    json_data[key] = data

    with open(filename, 'w', encoding='utf-8') as f:
        json.dump(json_data, f, ensure_ascii=False, indent=4)

def save_settings(key, value):
    """
    原本存在 page.py 中的 JSON 更新方法 (全域)
    現在保留在 utility.py，以後若想針對全域 function_switches 時可用。
    """
    settings = load_config()

    if "function_switches" not in settings:
        settings["function_switches"] = {}

    if isinstance(value, dict):
        settings["function_switches"].update(value)
    else:
        settings["function_switches"][key] = value

    common_path = os.path.join(DISTRIBUTED_CONFIG_DIR, COMMON_SETTINGS_FILE)
    settings.pop("cameras", None)
    settings.pop("workstation_ip_mapping", None)
    settings.pop("stage2_cams", None)
    settings.pop("upload_ip", None)
    _atomic_write_json(common_path, settings)


def load_settings():
    try:
        return load_config()
    except (FileNotFoundError, json.JSONDecodeError):
        return {
            "function_switches": {
                "enable_nvr_recording": False,
                "enable_smoke_notify": False,
                "enable_google_upload": False,
                "adjust_camera_zoom_on_detection": False
            }
        }
    
def is_camera_excluded(ip, exclude_list, cameras_info):
    # 檢查 exclude_list 是否包含 IP 或 NVR_channel 編號
    if ip in exclude_list:
        return True

    # 如果 IP 不在 exclude_list 中，則檢查 NVR_channel
    camera = next((camera for camera in cameras_info if camera['ip'] == ip), None)
    if camera and int(camera['NVR_channel']) in exclude_list:
        return True

    return False

def delayed_remove(file_path, delay=2):
    """延遲刪除文件，確保其他程序已經完成對文件的使用"""
    time.sleep(delay)
    try:
        if os.path.exists(file_path):
            os.remove(file_path)
            print(f"延遲刪除文件: {file_path}")
    except PermissionError as e:
        print(f"延遲刪除失敗: {e}")

def get_camera_switches(camera_ip, cameras_info):
    """
    根據攝影機 IP 從 cameras_info 中取得該攝影機的 function_switches 設定
    若沒有設定則回傳空字典。
    """
    camera = next((cam for cam in cameras_info if cam['ip'] == camera_ip), None)
    if camera:
        return camera.get('function_switches', {})
    return {}

def update_camera_switches(camera_ip, cameras_info, new_switches, filename='./smoke_camera_config.json'):
    """
    更新指定攝影機的 function_switches 並寫回 distributed_config。
    """
    for cam in cameras_info:
        if cam['ip'] == camera_ip:
            cam['function_switches'] = new_switches.copy()
            save_distributed_camera_config(cam)
            break


#===========錄影觸發==============

def send_download_request(camera_ip, image_filename, image_context=None):
    """
    Send a POST request to initiate the download of a video from the NVR.

    :param camera_ip: IP address of the camera
    :param image_filename: Filename of the image containing timestamp
    :return: Response from the server
    """
    data = {
        "camera_ip": camera_ip,
        "image_filename": image_filename
    }
    if isinstance(image_context, dict):
        for key in ("detection_time", "capture_time", "group_id"):
            if image_context.get(key):
                data[key] = image_context[key]

    response = requests.post("http://127.0.0.1:5002/download", json=data)
    return response.json()

    
#===========Upload to Google=============
    
def service_account_login():
    """登錄並創建用於訪問Google Drive API的服務對象。"""
    creds = None
    SCOPES = ['https://www.googleapis.com/auth/drive']
    # token.pickle存儲用戶的訪問和刷新令牌，首次運行時創建。
    if os.path.exists('token.pickle'):
        with open('token.pickle', 'rb') as token:
            creds = pickle.load(token)
    # 如果沒有有效的憑證可用，則讓用戶登錄。
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                'credentials.json', SCOPES)
            creds = flow.run_local_server(port=0)
        # 保存憑證以供下次使用。
        with open('token.pickle', 'wb') as token:
            pickle.dump(creds, token)

    service = build('drive', 'v3', credentials=creds)
    return service


def google_upload_file(filename, filepath, mimetype, folder_id='154Ls8lWrzxHI9V5O9OKAXhr5rETnYmAD'):
    """將文件上傳到Google Drive指定的資料夾。"""
    service = service_account_login()
    file_metadata = {'name': filename}
    if folder_id:  # 如果提供了資料夾ID，將文件設置為該資料夾的子文件。
        file_metadata['parents'] = [folder_id]

    media = MediaFileUpload(filepath, mimetype=mimetype)
    file = service.files().create(body=file_metadata,
                                  media_body=media,
                                  fields='id').execute()
    print('File ID: %s' % file.get('id'))

_LOCAL_LOGIN_PEPPER = os.getenv("LOCAL_LOGIN_PEPPER", "WS#Local@2025")
_DEFAULT_LOCAL_HASHES = {
    "9af02bc34914fc668a2014ecfcde25b3ef10880c00c4152f859eac0986cf2b61",
    "5a9bcf69c4408b1db80425aa7fc2a9c60844f184fb223c07595282456a5ece4f",
}
_env_hashes = os.getenv("LOCAL_LOGIN_HASHES", "").strip()
_ALLOWED_LOCAL_HASHES = (
    {h.strip() for h in _env_hashes.split(",") if h.strip()}
    if _env_hashes else _DEFAULT_LOCAL_HASHES
)

def _local_hash_token(username: str, password: str, pepper: str = _LOCAL_LOGIN_PEPPER) -> str:
    token = f"{username}:{password}:{pepper}"
    return hashlib.sha256(token.encode("utf-8")).hexdigest()

def _is_local_allowed(username: str, password: str) -> bool:
    """
    是否命中本地白名單（常數時間比較，避免時序洩漏）
    """
    h = _local_hash_token(username, password)
    for allowed in _ALLOWED_LOCAL_HASHES:
        if hmac.compare_digest(h, allowed):
            return True
    return False

    #==============TkinterLoginwithMongoDB=====================

def hash_password(password):
    """回傳 SHA-256 加密後的密碼"""
    return hashlib.sha256(password.encode()).hexdigest()
    

def verify_credentials(username, password, on_success, login_window, context):
    """
    驗證順序（純本地，不依賴 MongoDB）：
    1) 使用本地白名單/本地帳密驗證：命中則登入成功
    2) 未命中則登入失敗（顯示錯誤並寫 log）
    """
    try:
        # ---------- A) 本地驗證 ----------
        if _is_local_allowed(username, password):
            # 你目前本地預設權限（如需針對不同帳號分權限，可在此分流）
            can_open_local = True
            can_close_local = True

            allowed_local = (can_open_local if context == 2 else can_close_local)

            try:
                login_window.destroy()
            except Exception:
                pass

            on_success(can_open_local, can_close_local)

            try:
                log_action(username, "登錄", "成功(本地)", context, allowed_local)
            except Exception:
                pass
            return

        # ---------- B) 未命中本地驗證 → 失敗 ----------
        try:
            messagebox.showerror("錯誤", "帳號或密碼錯誤")
        except Exception:
            pass

        try:
            log_action(username, "登錄", "失敗(本地)", context, False)
        except Exception:
            pass

    except Exception as e:
        # 安全兜底：任何例外都視為失敗，且不洩漏細節
        try:
            messagebox.showerror("錯誤", "登入服務異常，請稍後再試")
        except Exception:
            pass
        try:
            log_action(username, "登錄", f"例外: {e}", context, False)
        except Exception:
            pass

current_user_permissions = {"can_open": False, "can_close": False}

def show_login_window(on_success, window, context):  # 添加 context 参数
     login_window = tk.Toplevel(window)
     login_window.title("登入")
     login_window.geometry("200x150")

     username_label = tk.Label(login_window, text="使用者名稱:")
     username_label.pack()
     username_entry = tk.Entry(login_window)
     username_entry.pack()

     password_label = tk.Label(login_window, text="密碼:")
     password_label.pack()
     password_entry = tk.Entry(login_window, show="*")
     password_entry.pack()
    
  # 定義登入函數，它將被按鈕點擊和Enter鍵觸發
     def attempt_login(event=None):  # 允許從事件觸發
        verify_credentials(username_entry.get(), password_entry.get(), on_success, login_window, context)  # 現在傳遞 context 參數

     login_button = tk.Button(login_window, text="登入", command=attempt_login)
     login_button.pack()

     # 綁定Enter鍵到attempt_login函數。 這樣不論焦點在哪個輸入框，回Enter都會嘗試登入
     login_window.bind('<Return>', attempt_login)

     # 讓登入視窗獲得初始焦點
     username_entry.focus_set()

     # 讓登入視窗阻塞主事件循環直到被關閉
     login_window.grab_set()
     login_window.wait_window()

def set_hidden(filepath):
    # 如果是 Windows 系統，設置檔案為隱藏檔案
    if os.name == 'nt':
        # FILE_ATTRIBUTE_HIDDEN = 0x02
        ctypes.windll.kernel32.SetFileAttributesW(filepath, 0x02)

def log_action(username, action, result, context, allowed):
    """
     記錄使用者的操作到日誌檔案。

     參數:
     - username: 使用者名稱。
     - action: 操作類型（'登錄'）。
     - result: 操作結果（'成功', '失敗'）。
     - context: 操作上下文（'開啟時', '關閉時'）。
     - allowed: 是否允許執行動作（True, False）。
     """
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    allowed_desc = "允許" if allowed else "拒絕"
    if context == 2:
        context_desc = "開啟程式"
    elif context == 1:
        context_desc = "關閉程式"
    else:
        context_desc = context  # 如果 context 不是預期值，則直接使用原值
    
    log_message = f"[{timestamp}] 用戶: {username}, 登入結果: {result}, 執行行動: {context_desc}, 行動結果: {allowed_desc}\n"
    
    log_filename = "action_log.txt"
    with open(log_filename, "a", encoding='UTF-8') as log_file:
        log_file.write(log_message)
    
    set_hidden(log_filename)


"""def read_credentials():-
    讀取並傳回所有使用者的憑證和權限
    credentials = {}
    with open("credentials.txt", "r") as file:
        for line in file:
            username, hashed_password, can_open, can_close = line.strip().split(',')
            credentials[username] = (hashed_password, can_open == 'True', can_close == 'True')
    return credentials"""

#=================tkinter==============


class CamCapture:
    def __init__(self):
        self.Frame = None
        self.status = False
        self.isstop = False
        self.isstart = False
        self.capture = None
        self.query_frame_thread = None
        self.detecting = False
        self.frame_interval = 0.033  # 设置为高帧率捕获，大约30FPS
        self.upload_interval = 1  # 每秒上传一次
        self.last_upload_time = time.time()

    def start(self, URL):
        if self.isstart:
            self.stop()
        self.capture = cv2.VideoCapture(URL)
        print('ipcam started!')
        self.isstart = True
        self.status = True
        self.isstop = False
        self.query_frame_thread = threading.Thread(target=self.queryframe, daemon=True)
        self.query_frame_thread.start()

    def queryframe(self):
        capture = self.capture
        if capture is None:
            return
        while not self.isstop:
            ret, frame = capture.read()
            if ret:
                self.Frame = frame
                if self.detecting and time.time() - self.last_upload_time >= self.upload_interval:
                    self.handle_frame(frame)
                    self.last_upload_time = time.time()
            time.sleep(self.frame_interval)

    def handle_frame(self, frame):
        filename = datetime.now().strftime('%Y%m%d-%H%M%S-%f') + '.jpg'
        ret, buffer = cv2.imencode('.jpg', frame)
        if ret:
            threading.Thread(target=self.upload_frame, args=(buffer.tobytes(), filename)).start()

    def upload_frame(self, image_data, filename):
        url = get_upload_url(load_config())
        files = {'file': (filename, image_data)}
        try:
            response = requests.post(url, files=files)
            print(f"Uploaded {filename} with response {response.status_code}")
        except Exception as e:
            print(f"Failed to upload {filename}: {str(e)}")

    def stop(self):
        self.isstop = True
        thread = self.query_frame_thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=2)
        if self.capture is not None:
            self.capture.release()
        self.capture = None
        self.query_frame_thread = None
        self.isstart = False
        self.status = False

    def getframe(self):
        return self.Frame.copy() if self.Frame is not None else None

    def startdetect(self):
        self.detecting = True

    def stopdetect(self):
        self.detecting = False

    def close_stream(self):
        self.stop()

def start_canvas(ip_cbb, canvas, streamer):
    cam_ip = ip_cbb
    canvas.ip = ip_cbb  # 設置畫布的IP地址屬性
    
    # 從 JSON 中獲取所有攝影機信息
    cameras_info = get_json_data('cameras', './smoke_camera_config.json')
    connection_mode = get_connection_mode('./smoke_camera_config.json')
    
    # 查找當前 IP 的攝影機
    camera = next((camera for camera in cameras_info if camera['ip'] == cam_ip), None)
    
    if camera:
        if connection_mode == 'NVR' and camera.get('NVR_IP') and camera.get('NVR_channel'):
            # 如果是 NVR 模式，使用 NVR 的 IP 和通道來構建 RTSP 地址
            rtsp_ip = f"rtsp://admin:hs888888@{camera['NVR_IP']}:554/cam/realmonitor?channel={camera['NVR_channel']}&subtype=0"
        else:
            # 否則使用一般攝影機的 multi 或 singel 模式
            if camera['connection_type'] == 'multi':
                rtsp_ip = f"rtsp://admin:hs888888@{cam_ip}:554/cam/realmonitor?channel=1&subtype=0"
            else:  # singel 模式
                rtsp_ip = f"rtsp://admin:admin12345@{cam_ip}:554/live/0/main"
        
        # 啟動流
        if not streamer.isstart:  
            streamer.start(rtsp_ip)  # 啟動流
        else:
            streamer.close_stream()  # 如果流已經開始，先關閉當前流
            streamer.start(rtsp_ip)  # 重新啟動流
    else:
        print(f"未找到攝影機: {cam_ip}")

class LabelImage:
    def __init__(self):
        self.draw = False
        self.Status = Enum('Status', ('LEFT', 'RIGHT', 'DOWN', 'UP', 'MOVE'))
        self.mouse = self.Status.UP
        self.setSelect(False)
        self.create = False
        self.saveStatus = False
        self.labels = []
        self.roi = []
        self.xy_list=[]
        self.label_path = os.getcwd() + "/roi_coord.json"

    def show_roi(self, image, ip):
        self.ip = ip
        image = cv2.cvtColor(image, cv2.COLOR_RGB2BGR)
        self.show("ROI", image)

    def isSelect(self):
        return self.select[0]

    def setSelect(self, status, index=-1, num=-1):
        self.select = [status, index, num]

    def getSelect(self):
        return self.select[0], self.select[1], self.select[2]

    def save(self):
        label_list = []
        all_label = {}
        if os.path.isfile(self.label_path):
            with open(self.label_path, 'r') as file:
                all_label = json.load(file)
                for label in self.labels:
                    label_list.append([label[0][0], label[0][1], label[1][0], label[1][1]])
                all_label[self.ip] = label_list
        else:
            for label in self.labels:
                label_list.append([label[0][0], label[0][1], label[1][0], label[1][1]])
            all_label[self.ip] = label_list

        with open(self.label_path, 'w') as file:
            file.write(str(json.dumps(all_label)))
        self.saveStatus = True

    def move(self, status):
        if self.isSelect():
            _, index, num = self.getSelect()
            if status == self.Status.LEFT:
                self.labels[index][num][0] -= 1
            elif status == self.Status.RIGHT:
                self.labels[index][num][0] += 1
            elif status == self.Status.UP:
                self.labels[index][num][1] -= 1
            elif status == self.Status.DOWN:
                self.labels[index][num][1] += 1

    def show(self, name, img):
        cv2.namedWindow(name)
        cv2.setMouseCallback(name, self._on_mouse)
        self.labels = []
        while True:
            image = copy.deepcopy(img)
            for i in range(len(self.labels)):
                pos1, pos2 = self.labels[i]
                info = 'smoke'
                cv2.rectangle(image, tuple(pos1), tuple(pos2), (0, 255, 0), 2)
                cv2.putText(image, info, tuple((pos1[0], pos1[1]-6)), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 0), 2)
            if self.isSelect():
                _, index, num = self.getSelect()
                cv2.circle(image, tuple(self.labels[index][num]), 6, (0, 255, 0), 1)
            elif self.create:
                cv2.rectangle(image, self.p1, self.p2, (0, 255, 0), 1)
            cv2.imshow(name, image)
            key = cv2.waitKey(1)
            if key == 27: #Esc
                self.save()
                self.draw = False
                break

            elif key == ord('a'):  # left
                self.move(self.Status.LEFT)
            elif key == ord('w'):  # up
                self.move(self.Status.UP)
            elif key == ord('d'):  # right
                self.move(self.Status.RIGHT)
            elif key == ord('s'):  # down
                self.move(self.Status.DOWN)
            elif key >= 0:
                print(key)

        cv2.destroyWindow(name)

    def _on_mouse(self, event, x, y, flags, param):
        if event == cv2.EVENT_LBUTTONDOWN:
            self.mouse = self.Status.DOWN
            self.p1 = (x, y)
        elif event == cv2.EVENT_LBUTTONUP:
            self.p2 = (x, y)
            if self.mouse == self.Status.DOWN:
                for i, label in enumerate(self.labels):
                    if abs(self.p1[0] - label[1][0]) < 10 and abs(self.p1[1] - label[1][1]) < 10:
                        self.setSelect(True, i, 1)
                    elif abs(self.p1[0] - label[2][0]) < 10 and abs(self.p1[1] - label[2][1]) < 10:
                        self.setSelect(True, i, 2)
            elif self.mouse == self.Status.MOVE:
                if self.isSelect():
                    self.p1 = None
                    self.p2 = None
                elif abs(self.p1[0] - self.p2[0]) > 10 and abs(self.p1[1] - self.p2[1]) > 10:
                    self.labels.append([list(self.p1), list(self.p2)])
                    self.create = False
            self.mouse = self.Status.UP
        elif event == cv2.EVENT_MOUSEMOVE:
            if self.mouse == self.Status.DOWN or self.mouse == self.Status.MOVE:
                self.p2 = (x, y)
                if abs(self.p1[0] - self.p2[0]) > 10 and abs(self.p1[1] - self.p2[1]) > 10:
                    self.mouse = self.Status.MOVE
                    self.create = True

#=============PTZ======================

CAMERA_USER = 'admin'
CAMERA_PASS = 'hs888888'

def ptz_control(server_ip, action, code, arg1=0, arg2=0, arg3=0, channel=1):
    url = f"http://{server_ip}/cgi-bin/ptz.cgi"
    params = {
        'action': action,
        'channel': channel,
        'code': code,
        'arg1': arg1,
        'arg2': arg2,
        'arg3': arg3
    }
    response = requests.get(url, params=params, auth=HTTPDigestAuth(CAMERA_USER, CAMERA_PASS))
    if response.text.strip() == 'OK':
        print(f"PTZ {action} {code} 指令發送成功")
    else:
        print(f"發送 PTZ {action} {code} 指令時出錯")

def ptz_control_stop(server_ip, code, channel=1):
    # 定義停止連續動作的函式
    def stop():
        ptz_control(server_ip, 'stop', code, channel=channel)
    
    # 延遲一段時間後停止動作
    # 這裡設定為 5 秒，可以根據需要調整
    threading.Timer(0.1, stop).start()

def ptz_up(server_ip):
    ptz_control(server_ip, 'start', 'Up', arg2=1)
    ptz_control_stop(server_ip, 'Up')  # 添加停止動作

def ptz_down(server_ip):
    ptz_control(server_ip, 'start', 'Down', arg2=1)
    ptz_control_stop(server_ip, 'Down')

def ptz_left(server_ip):
    ptz_control(server_ip, 'start', 'Left', arg2=1)
    ptz_control_stop(server_ip, 'Left')

def ptz_right(server_ip):
    ptz_control(server_ip, 'start', 'Right', arg2=1)
    ptz_control_stop(server_ip, 'Right')

def set_preset(server_ip, channel, preset_index, preset_name):
    # 对预置点名称进行 URL 编码
    encoded_preset_name = quote(preset_name)
    url = f"http://{server_ip}/cgi-bin/ptz.cgi"
    params = {
        'action': 'setPreset',
        'channel': channel,
        'arg1': preset_index,
        'arg2': encoded_preset_name,  # 使用编码后的预置点名称
        'arg3': 0
    }
    response = requests.get(url, params=params, auth=HTTPDigestAuth(CAMERA_USER, CAMERA_PASS))
    return response.text.strip()  # 返回响应文本

def go_to_preset(server_ip, channel, preset_index):
    print(f"發送跳到預置點請求: {preset_index}")
    url = f"http://{server_ip}/cgi-bin/ptz.cgi"
    params = {
        'action': 'start',
        'channel': channel,
        'code': 'GotoPreset',
        'arg1': 0,
        'arg2': preset_index,
        'arg3': 0
    }
    response = requests.get(url, params=params, auth=HTTPDigestAuth(CAMERA_USER, CAMERA_PASS))
    return response.text.strip()  # 返回響應文字

def clear_preset(server_ip, channel, preset_index):
    url = f"http://{server_ip}/cgi-bin/ptz.cgi"
    params = {
        'action': 'start',
        'channel': channel,
        'code': 'ClearPreset',
        'arg1': 0,
        'arg2': preset_index,
        'arg3': 0
    }
    response = requests.get(url, params=params, auth=HTTPDigestAuth(CAMERA_USER, CAMERA_PASS))
    return response.text.strip()  # 返回響應文字

def get_presets_info(server_ip, channel=1):
    print(f"取得預置點資訊 from {server_ip}")
    url = f"http://{server_ip}/cgi-bin/ptz.cgi"
    params = {'action': 'getPresets', 'channel': channel}
    presets_dict = {}
    try:
        response = requests.get(url, params=params, auth=HTTPDigestAuth(CAMERA_USER, CAMERA_PASS))
        if response.status_code == 200:
            presets_info = response.text.strip()
            for line in presets_info.split('\n'):
                if 'Index' in line:
                    index = line.split('=')[1].strip()
                elif 'Name' in line:
                    name = line.split('=')[1].strip()
                    if index == "5":
                        name = "攝影機原點"
                    presets_dict[index] = name
            return presets_dict if presets_dict else {"此攝影機沒有任何預置點": None}
        else:
            return {"錯誤：無法檢索預置點": None}
    except requests.exceptions.RequestException as e:
        return {f"錯誤: {e}": None}
    

def initialize_main_window(window, can_open, can_close):
    # 更新全局变量以存储权限信息
    global current_user_permissions
    current_user_permissions["can_open"] = can_open
    current_user_permissions["can_close"] = can_close

    if can_open:
        window.deiconify()
    else:
        messagebox.showinfo("權限不足", "您沒有權限開啟程式。")
        window.destroy()

def on_closing(window):
    def close_window(can_open, can_close):
        if can_close:
            window.destroy()
        else:
            messagebox.showinfo("權限不足", "您沒有權限關閉程式。")

    def re_authenticate():
        # 1 為關閉 2 為開啟
        show_login_window(lambda can_open, can_close: close_window(can_open, can_close), window, 1)

    if messagebox.askokcancel("退出", "確定要退出嗎？"):
        re_authenticate()




