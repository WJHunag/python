import tkinter as tk
from tkinter import messagebox
from PIL import Image, ImageTk
import threading
import cv2
import time
from datetime import datetime
import json
import numpy as np
import utility 
import argparse
import requests
import random
import os
import bot



"""
進行n個攝影機同時錄影的程式
"""
os.environ["OMP_NUM_THREADS"] = "1"
os.environ["FFMPEG_THREADS"] = "1"

class StreamCapture:
    def __init__(self, cam_ip, base_snapshot_dir="images"):
        self.ip = cam_ip
        
        # 讀取配置文件
        with open('smoke_camera_config.json', 'r', encoding='utf-8') as f:
            config = json.load(f)

        self.connection_mode = config['connection_mode']  # 全域變數
        self.cameras = config['cameras']
        self.current_camera = next((camera for camera in self.cameras if camera['ip'] == cam_ip), None)

        if not self.current_camera:
            raise ValueError(f"未知的攝影機 IP: {cam_ip}")
        
        # 手動檢查該攝影機是否為 NVR
        if self.current_camera['connection_type'] == 'NVR':
            nvr_ip = self.current_camera['NVR_IP']
            nvr_channel = self.current_camera['NVR_channel']
            rtsp_ip = f"rtsp://admin:hs888888@{nvr_ip}:554/cam/realmonitor?channel={nvr_channel}&subtype=0"
        else:
            # 根據全域 connection_mode 設置連接方式
            if self.connection_mode == 'NVR':
                nvr_ip = self.current_camera['NVR_IP']
                nvr_channel = self.current_camera['NVR_channel']
                rtsp_ip = f"rtsp://admin:hs888888@{nvr_ip}:554/cam/realmonitor?channel={nvr_channel}&subtype=0"
            elif self.current_camera['connection_type'] == 'singel':
                rtsp_ip = "rtsp://admin:admin12345@" + cam_ip + ":554/live/0/main"
            elif self.current_camera['connection_type'] == 'multi':
                rtsp_ip = "rtsp://admin:hs888888@" + cam_ip + ":554/cam/realmonitor?channel=1&subtype=0"

        self.rtsp_ip = rtsp_ip  # 保存 RTSP 連接字串
        self.upload_url = config.get('upload_ip', '')  # 上傳的 URL
        self.online = False  # 初始設置為False，表示假設初始狀態為斷線
        self.capture = None
        self.frame = None
        self.last_disconnect_time = datetime.now()  # 初始化為當前時間
        self.last_notify_time = time.time() - 3600  # 確保首次通知可以發送
        self.last_snapshot_time = time.time()  # 初始化截圖時間戳記
        self.upload_interval = 60 + random.randint(10, 30)  # 每個攝影機的上傳間隔略微不同
        self.last_upload_time = time.time()
        self.snapshot_dir = os.path.join(base_snapshot_dir, cam_ip.replace('.', '_'))

        if not os.path.exists(self.snapshot_dir):
            os.makedirs(self.snapshot_dir)
        
        self.update_frame_thread = None
        self.update_frame_running = threading.Event()
        self.load_network_stream()  # 啟動攝影機連接
        
    def load_network_stream(self):    
        def load_network_stream_thread():
            first_attempt = True  # 标记是否为第一次尝试
            while True:
                if self.verify_network_stream(self.rtsp_ip):
                    if not self.online:
                        self.capture = cv2.VideoCapture(self.rtsp_ip, cv2.CAP_FFMPEG)
                        self.online = True
                        print(f"重新連接至攝影機 {self.ip}")
                        self.last_disconnect_time = None

                        # 启动 update_frame 线程
                        self.update_frame_running.set()  # 设置事件标志
                        if self.update_frame_thread is None or not self.update_frame_thread.is_alive():
                            self.update_frame_thread = threading.Thread(target=self.update_frame, daemon=True)
                            self.update_frame_thread.start()
                else:
                    current_time = datetime.now()
                    
                    # 定義工作時間範圍：早上7點到晚上7點
                    work_start = current_time.replace(hour=7, minute=0, second=0, microsecond=0)
                    work_end = current_time.replace(hour=19, minute=0, second=0, microsecond=0)

                    # 更新斷線時間和狀態
                    if first_attempt or (self.online and self.last_disconnect_time is None):
                        self.last_disconnect_time = current_time  # 記錄第一次嘗試連接失敗的時間
                        first_attempt = False  # 更新標記
                    self.online = False

                    # 檢查是否在工作時間範圍內，跨日時間處理
                    if (work_start <= current_time < work_end):  # 如果在當天的7點到19點之間
                        # 如果已經記錄了斷線時間，並且已經斷線超過30分鐘
                        if (self.last_disconnect_time and 
                            (current_time - self.last_disconnect_time).total_seconds() >= 1800):
                            # 檢查自上次通知以來是否已經過了一小時（3600秒）
                            if current_time.timestamp() - self.last_notify_time > 3600:
                                bot.smoke_notify_camera(self.ip)  # 發送通知
                                self.last_notify_time = current_time.timestamp()  # 更新通知時間戳
                                print(f"無法連接至攝影機: {self.ip}，已持續30分鐘發送通知")
                    
                    print(f"無法連接至攝影機: {self.ip}，10秒後重新嘗試連接")
                    time.sleep(10)  # 減少重試頻率
                
        threading.Thread(target=load_network_stream_thread, daemon=True).start()

    def verify_network_stream(self, link):
        """確認是否可以連接攝影機"""
        cap = cv2.VideoCapture(link)
        if not cap.isOpened():
            cap.release()
            return False
        cap.release()
        return True

    def update_frame(self):
        """接收最新影像"""
        while True:
            try:
                if self.capture.isOpened() and self.online:
                    ret, frame = self.capture.read()
                    if ret:
                        frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                        self.frame = frame
                        current_time = time.time()
                        
                        # 每1800秒（30分鐘）保存截圖一次
                        if current_time - self.last_snapshot_time >= 1800:
                            self.save_snapshot(self.frame)
                            self.last_snapshot_time = current_time
                        
                        # 每個攝影機根據自己的間隔時間上傳一次圖片
                        if current_time - self.last_upload_time >= self.upload_interval:
                            self.handle_frame(self.frame)
                            self.last_upload_time = current_time
                    else:
                        print(f"攝影機 {self.ip} 斷線，嘗試重新連接")
                        self.capture.release()
                        self.online = False
                        self.update_frame_running.clear()  # 清除事件标志，停止循环
                else:
                    self.update_frame_running.clear()  # 清除事件标志，停止循环
            except Exception as e:
                print(f"更新畫面時發生錯誤: {str(e)}")
            time.sleep(0.1)

    def save_snapshot(self, frame):
        filename = f"{datetime.now().strftime('%Y_%m_%d_%H_%M_%S')}.jpg"
        path = os.path.join(self.snapshot_dir, filename)
        cv2.imwrite(path, cv2.cvtColor(frame, cv2.COLOR_RGB2BGR))
        print(f"Saved snapshot {filename} in {self.snapshot_dir}")

    def handle_frame(self, frame):
        # 使用 IP 地址創建文件名，將點替換為底線
        ip_formatted = self.ip.replace('.', '_')
        filename = f"{ip_formatted}_{datetime.now().strftime('%Y_%m_%d_%H_%M_%S')}.jpg"
        bgr_frame = cv2.cvtColor(frame, cv2.COLOR_RGB2BGR)
        ret, buffer = cv2.imencode('.jpg', bgr_frame, [int(cv2.IMWRITE_JPEG_QUALITY), 40])
        if ret:
            threading.Thread(target=self.upload_frame, args=(buffer.tobytes(), filename)).start()

    def upload_frame(self, image_data, filename):
        files = {'file': (filename, image_data)}
        try:
            response = requests.post(self.upload_url, files=files)
            print(f"Uploaded {filename} with response {response.status_code}")
        except Exception as e:
            print(f"Failed to upload {filename}: {str(e)}")

    def get_frame(self):
        """返回最新的影像"""
        return self.frame if hasattr(self, "frame") else None



class StreamCanvas(tk.Canvas):
    def __init__(self, root, canvas_size, stream_capture, location):
        self.width = canvas_size[0]
        self.height = canvas_size[1]
        super().__init__(root, width=self.width, height=self.height, bg='black')
        self.stream_capture = stream_capture
        self.location = location
        self.refresh_rate = 1000 // 10 
        self.image = None
        self.update_canvas()

    def update_canvas(self):
        frame = self.stream_capture.get_frame()
        if frame is not None:
            resized_frame = cv2.resize(frame, (self.width, self.height), interpolation=cv2.INTER_LINEAR)
            self.photo = ImageTk.PhotoImage(image=Image.fromarray(resized_frame))
            self.create_image(0, 0, image=self.photo, anchor="nw")
            text_x = self.width - 10
            text_y = self.height - 10
            self.create_text(text_x - 2, text_y - 2, anchor='se', text=self.location, fill='black', font=('Microsoft YaHei', 14, 'bold'))
            self.create_text(text_x, text_y, anchor='se', text=self.location, fill='white', font=('Microsoft YaHei', 14, 'bold'))
        else:
            self.delete("all")
        self.after(self.refresh_rate, self.update_canvas)

def main():
    window = tk.Tk()
    window.withdraw()
    window.protocol("WM_DELETE_WINDOW", lambda: utility.on_closing(window))
    utility.show_login_window(lambda can_open, can_close: utility.initialize_main_window(window, can_open, can_close), window, 2)
    window.title('煙霧檢測總畫面')
    window.title('多攝像頭監控系統')
    window.state('zoomed')
    
    # 設定視窗和畫布大小
    win_size = [1920, 1020]
    num_rows = 3
    num_cols = 5
    canvas_size = (win_size[0] // num_cols, win_size[1] // num_rows)
    
    window.geometry(f"{win_size[0]}x{win_size[1]}")
    window.configure(bg='#AFEEEE')
    window.resizable(width=1, height=1)
    
    ip_path = "./smoke_camera_config.json"
    
    # 直接讀取整個JSON文件
    with open(ip_path, 'r', encoding='utf-8') as f:
        config = json.load(f)

    # 根據JSON的 `display_cameras` 設定來選擇要顯示的攝影機
    display_channels = config.get('display_cameras', [])
    cameras_info = config['cameras']

    # 過濾出指定的攝影機列表
    selected_cameras = [camera for camera in cameras_info if int(camera['NVR_channel']) in display_channels]

    cameras = {camera['ip']: StreamCapture(camera['ip']) for camera in selected_cameras}

    for idx, camera in enumerate(selected_cameras):
        location = camera.get('location', '未知位置')
        canvas = StreamCanvas(window, canvas_size, cameras[camera['ip']], location)
        canvas.place(x=canvas_size[0] * (idx % num_cols), y=canvas_size[1] * (idx // num_cols))
    
    window.mainloop()

if __name__ == "__main__":
    main()