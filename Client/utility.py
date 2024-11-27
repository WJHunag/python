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
from pymongo import MongoClient




regex = '''^(25[0-5]|2[0-4][0-9]|[0-1]?[0-9][0-9]?)\.( 
            25[0-5]|2[0-4][0-9]|[0-1]?[0-9][0-9]?)\.( 
            25[0-5]|2[0-4][0-9]|[0-1]?[0-9][0-9]?)\.( 
            25[0-5]|2[0-4][0-9]|[0-1]?[0-9][0-9]?)'''

"""
函式庫
"""
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

#==========json抓取==========

def get_json_data(key, json_path="./smoke_camera_config.json"):
    """
    從JSON文件中讀取指定的數據。

    :param json_path: JSON文件的路徑，默認為"./config.json"。
    :param key: 要讀取的數據鍵。
    :return: 指定鍵對應的數據。
    """
    with open(json_path, 'r', encoding='utf-8') as file:
        data = json.load(file)
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
    
def save_settings(key, value):
    try:
        with open('./smoke_camera_config.json', 'r', encoding='utf-8') as file:
            settings = json.load(file)
    except (FileNotFoundError, json.JSONDecodeError):
        settings = {}

    if "function_switches" not in settings:
        settings["function_switches"] = {}

    settings["function_switches"][key] = value

    with open('./smoke_camera_config.json', 'w', encoding='utf-8') as file:
        json.dump(settings, file, ensure_ascii=False, indent=4)

def load_settings():
    try:
        with open('./smoke_camera_config.json', 'r', encoding='utf-8') as file:
            return json.load(file)
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
#===========錄影觸發==============

def send_download_request(start_time, end_time, channel):
    """
    Send a POST request to initiate the download of a video from the NVR.

    :param start_time: Start time of the video in 'yyyy-mm-dd hh:mm:ss' format
    :param end_time: End time of the video in 'yyyy-mm-dd hh:mm:ss' format
    :param channel: Channel number
    :return: Response from the server
    """
    data = {
        "StartTime": start_time,
        "EndTime": end_time,
        "Channel": channel
    }

    response = requests.post("http://127.0.0.1:5000/download", json=data) #[需更改]Flask的IP
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

    #==============TkinterLoginwithMongoDB=====================

client = MongoClient(
    host='localhost',
    port=27017,
    )
db = client['user_database']
collection = db['users']


def hash_password(password):
    """回傳 SHA-256 加密後的密碼"""
    return hashlib.sha256(password.encode()).hexdigest()
    

def verify_credentials(username, password, on_success, login_window, context):
    hashed_input_password = hash_password(password)

    # 從 MongoDB 查詢用戶資訊
    user_document = collection.find_one({'username': username})

    if user_document:
        stored_hashed_password = user_document['password']
        can_open = user_document['permissions']['can_open']
        can_close = user_document['permissions']['can_close']
        allowed = (can_open if context == 2 else can_close)
        if hashed_input_password == stored_hashed_password:
            login_window.destroy()
            on_success(can_open, can_close)
            log_action(username, "登錄", "成功", context, allowed)  # 使用 allowed 参数
        else:
            messagebox.showerror("錯誤", "密碼錯誤")
            log_action(username, "登錄", "密碼錯誤", context, False)  # 登錄失敗預設為不允許執行
    else:
        messagebox.showerror("錯誤", "帳號不存在")
        log_action(username, "登錄", "帳號不存在", context, False)  # 帳號不存在也設定為不允許執行

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
        self.detecting = False
        self.frame_interval = 0.033  # 设置为高帧率捕获，大约30FPS
        self.upload_interval = 1  # 每秒上传一次
        self.last_upload_time = time.time()

    def start(self, URL):
        if not self.status:
            self.capture = cv2.VideoCapture(URL)
        print('ipcam started!')
        self.isstart = True
        self.isstop = False
        self.query_frame_thread = threading.Thread(target=self.queryframe, daemon=True)
        self.query_frame_thread.start()

    def queryframe(self):
        while not self.isstop:
            ret, frame = self.capture.read()
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
        url = "http://127.0.0.1:5000/upload"
        files = {'file': (filename, image_data)}
        try:
            response = requests.post(url, files=files)
            print(f"Uploaded {filename} with response {response.status_code}")
        except Exception as e:
            print(f"Failed to upload {filename}: {str(e)}")

    def stop(self):
        self.isstop = True
        if self.query_frame_thread.is_alive():
            self.query_frame_thread.join()
        self.capture.release()
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
    cam_ip = ip_cbb.get()  # 從下拉選擇菜單獲取攝影機的IP地址
    canvas.ip = cam_ip  # 設置畫布的IP地址屬性
    
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




