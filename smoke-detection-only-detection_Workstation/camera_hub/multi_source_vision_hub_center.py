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
from functools import partial 
import argparse
import requests
import random
import os
import result_handler.bot
import logging
import traceback
from .camera_ptz import PTZControl

"""
This script performs simultaneous recording of multiple cameras.
同時對多個攝影機進行錄影與影像上傳的程式。
"""

# =========================== 環境變數設定 ===========================
os.environ["OMP_NUM_THREADS"] = "1"
os.environ["FFMPEG_THREADS"] = "1"

# =========================== 常數定義 ===========================
RETRY_INTERVAL = 10          # 重新嘗試連接的秒數
NOTIFY_INTERVAL = 3600       # 通知間隔秒數 (1小時)
DISCONNECT_THRESHOLD = 1800  # 斷線持續多久需通知 (30分鐘)
WORK_START_HOUR = 7
WORK_END_HOUR = 19
SNAPSHOT_INTERVAL = 1800     # 截圖間隔(30分鐘)
JPEG_QUALITY = 40

# =========================== LOGGING 設定 ===========================
current_date = datetime.now().strftime('%Y-%m-%d')
log_folder = "logs"

# 建立 Logger 函式
def setup_logger(log_type="main"):
    # 取得目前日期
    current_date = datetime.now().strftime('%Y-%m-%d')
    log_file = os.path.join(
        log_folder,
        f"{current_date}_connection.log" if log_type == "main" else f"{current_date}_transmit.log"
    )
    
    # 創建 logger
    logger = logging.getLogger(log_type)
    logger.handlers = []  # 清除所有舊 handler
    logger.propagate = False  # 防止日誌傳遞到 root logger

    # 文件 handler
    file_handler = logging.FileHandler(log_file, mode='a', encoding='utf-8')
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(logging.Formatter('[%(asctime)s] %(levelname)s: %(message)s'))
    logger.addHandler(file_handler)

    # 終端 handler
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(logging.Formatter('[%(asctime)s] %(levelname)s: %(message)s'))
    logger.addHandler(console_handler)

    logger.setLevel(logging.INFO)
    return logger
    
def check_and_update_loggers():
    global main_logger, transmit_logger, current_date
    new_date = datetime.now().strftime('%Y-%m-%d')
    
    if new_date != current_date:
        current_date = new_date  # 更新當前日期
        
        # 更新 main_logger
        log_file_main = os.path.join(log_folder, f"{current_date}_connection.log")
        if main_logger.handlers:
            main_logger.handlers.clear()  # 清除舊 handlers
        file_handler_main = logging.FileHandler(log_file_main, mode='a', encoding='utf-8')
        file_handler_main.setFormatter(logging.Formatter('[%(asctime)s] %(levelname)s: %(message)s'))
        main_logger.addHandler(file_handler_main)

        # 更新 transmit_logger
        log_file_transmit = os.path.join(log_folder, f"{current_date}_transmit.log")
        if transmit_logger.handlers:
            transmit_logger.handlers.clear()  # 清除舊 handlers
        file_handler_transmit = logging.FileHandler(log_file_transmit, mode='a', encoding='utf-8')
        file_handler_transmit.setFormatter(logging.Formatter('[%(asctime)s] %(levelname)s: %(message)s'))
        transmit_logger.addHandler(file_handler_transmit)

        # 控制台輸出仍保持
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(logging.Formatter('[%(asctime)s] %(levelname)s: %(message)s'))
        main_logger.addHandler(console_handler)
        transmit_logger.addHandler(console_handler)

        # 確認更新成功
        main_logger.info(f"日誌已更新至新檔案: {log_file_main}")
        transmit_logger.info(f"日誌已更新至新檔案: {log_file_transmit}")


# 初始化時執行
main_logger = setup_logger("main")
transmit_logger = setup_logger("transmit")




class CameraStreamManager:
    """
    攝影機串流管理，提供影像讀取、上傳功能及斷線通知。
    """

    def __init__(self, cam_ip, base_snapshot_dir="images"):
        self.ip = cam_ip
        self.is_stream_active = False
        self.stream_capture = None
        self.current_frame = None
        self.last_disconnect_time = datetime.now()
        self.last_notify_time = time.time() - NOTIFY_INTERVAL
        self.last_snapshot_timestamp = time.time()
        self.initial_upload_wait = 60 + random.randint(10,30)  # 只在啟動時用
        self.upload_interval_seconds = 0 + random.randint(10,30)
        self.last_upload_timestamp = time.time()
        self.snapshot_dir = os.path.join(base_snapshot_dir, cam_ip.replace('.', '_'))

        if not os.path.exists(self.snapshot_dir):
            os.makedirs(self.snapshot_dir)

        self.is_running = threading.Event()
        self.is_running.set()

        # 載入設定
        config = utility.load_config('smoke_camera_config.json')
        self.upload_url = config.get('upload_ip', '')
        self.rtsp_ip = self.assign_rtsp_url(config)

        camera_config = next((c for c in config['cameras'] if c['ip'] == self.ip), {})
        function_switches = camera_config.get('function_switches', {})
        self.adjust_camera_zoom_on_detection = function_switches.get('adjust_camera_zoom_on_detection', False)
        self.focus_time = function_switches.get('focus_time', 0)

    def assign_rtsp_url(self, config):
        camera = next((c for c in config['cameras'] if c['ip'] == self.ip), None)
        if not camera:
            raise ValueError(f"未知的攝影機 IP: {self.ip}")
        conn_type = camera.get('connection_type')
        if conn_type == 'NVR':
            # 讀取並標準化 NVR_channel
            try:
                raw_ch = int(camera.get('NVR_channel', 1))
            except Exception:
                raw_ch = 1
            mapped_ch = ((max(1, raw_ch) - 1) % 20) + 1
            nvr_ip = camera.get('NVR_IP', '')
            # 下行維持你原本的 URL 模板，只把 channel 改成 mapped_ch
            return f"rtsp://admin:hs888888@{nvr_ip}:554/cam/realmonitor?channel={mapped_ch}&subtype=0"
        elif conn_type == 'singel':
            # 保留你原本的其他分支
            return f"rtsp://admin:admin12345@{self.ip}:554/live/0/main"
        elif conn_type == 'multi':
            return f"rtsp://admin:hs888888@{self.ip}:554/cam/realmonitor?channel=1&subtype=0"
        else:
            raise ValueError(f"不支援的攝影機連線類型: {conn_type}")

    def stream_controller(self):
        """
        作為主要控制器，管理影像讀取、快照保存及上傳任務。
        """
        while self.is_running.is_set():
            try:
                # 嘗試連接攝影機
                self.connect_to_camera()

                # 啟動任務分配
                threading.Thread(target=self.read_camera_stream, daemon=True).start()
                threading.Thread(target=self.schedule_periodic_tasks, daemon=True).start()

                # 主控迴圈：維持運行並監控狀態
                while self.is_running.is_set() and self.is_stream_active:
                    time.sleep(1)
            except ConnectionError:
                self.handle_camera_disconnection()
            except Exception as e:
                logging.error(f"連接攝影機 {self.ip} 失敗: {e}")
                self.reset_camera_connection()
                time.sleep(RETRY_INTERVAL)


    def connect_to_camera(self):
        """
        嘗試連接攝影機並初始化串流。
        """
        check_and_update_loggers()  # 檢查日期並更新 log
        self.stream_capture = cv2.VideoCapture(self.rtsp_ip, cv2.CAP_FFMPEG)
        if not self.stream_capture.isOpened():
            raise ConnectionError(f"無法連接至攝影機 {self.ip}")

        self.is_stream_active = True
        self.last_disconnect_time = None
        main_logger.info(f"成功連接攝影機 {self.ip}")  # 使用 main_logger


    def read_camera_stream(self):
        """
        獨立執行緒：負責持續讀取影像並更新 frame。
        """
        while self.is_running.is_set() and self.is_stream_active:
            ret, frame = self.stream_capture.read()
            if ret:
                self.current_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            else:
                logging.warning(f"攝影機 {self.ip} 串流中斷")
                self.handle_camera_disconnection()
                break

    def schedule_periodic_tasks(self):
        """
        獨立執行緒：定期執行快照保存和影像前處理上傳。
        """
        # 啟動時先等待初始連線延遲 (60秒 + 10~30秒)
        time.sleep(self.initial_upload_wait)
        self.last_upload_timestamp = time.time()
        # 設定初始上傳間隔 (僅隨機部分: 10到30秒)
        self.upload_interval_seconds = random.randint(10, 30)

        while self.is_running.is_set() and self.is_stream_active:
            current_time = time.time()

            # 保存快照
            if current_time - self.last_snapshot_timestamp >= SNAPSHOT_INTERVAL and self.current_frame is not None:
                self.save_snapshot(self.current_frame)
                self.last_snapshot_timestamp = current_time

            # 前處理並上傳 (每次上傳間隔 10~30秒)
            if self.current_frame is not None and current_time - self.last_upload_timestamp >= self.upload_interval_seconds:
                processed_frame = self.preprocessing(self.current_frame)
                if processed_frame:
                    threading.Thread(target=self.transmit, args=(processed_frame,), daemon=True).start()
                self.last_upload_timestamp = current_time
                # 重新設定下一次上傳的間隔 (隨機 10~30秒)
                self.upload_interval_seconds = random.randint(10, 30)

            time.sleep(1)

    def handle_camera_disconnection(self):
        current_time = datetime.now()
        work_start = current_time.replace(hour=WORK_START_HOUR, minute=0, second=0, microsecond=0)
        work_end = current_time.replace(hour=WORK_END_HOUR, minute=0, second=0, microsecond=0)

        if not self.last_disconnect_time:
            self.last_disconnect_time = current_time

        if work_start <= current_time < work_end:
            time_since_last_notify = current_time.timestamp() - self.last_notify_time
            disconnect_duration = (current_time - self.last_disconnect_time).total_seconds()

            if time_since_last_notify > NOTIFY_INTERVAL and disconnect_duration >= DISCONNECT_THRESHOLD:
                try:
                    #bot.smoke_notify_camera(self.ip)
                    bot.telegram_smoke_notify_camera(self.ip)
                    bot.discord_smoke_notify_camera(self.ip)
                    main_logger.warning(f"無法連接攝影機 {self.ip} 已超過30分鐘，已發送通知")
                except Exception as e:
                    main_logger.error(f"通知發送失敗: {e}")
                finally:
                    self.last_notify_time = current_time.timestamp()

        self.reset_camera_connection()


    def reset_camera_connection(self):
        if self.stream_capture:
            self.stream_capture.release()
        self.is_stream_active = False
        self.current_frame = None
        main_logger.info(f"攝影機 {self.ip} 的連線已重置")
        time.sleep(RETRY_INTERVAL)

    def save_snapshot(self, frame):
        filename = f"{datetime.now().strftime('%Y_%m_%d_%H_%M_%S')}.jpg"
        path = os.path.join(self.snapshot_dir, filename)
        cv2.imwrite(path, cv2.cvtColor(frame, cv2.COLOR_RGB2BGR))
        logging.info(f"保存截圖: {filename}")

    def preprocessing(self, frame):
        """
        壓縮影像並轉換成上傳格式。
        """
        # 保持影像為 BGR 格式
        frame_bgr = cv2.cvtColor(frame, cv2.COLOR_RGB2BGR)
        filename = f"{self.ip.replace('.', '_')}_{datetime.now().strftime('%Y_%m_%d_%H_%M_%S')}.jpg"
        ret, buffer = cv2.imencode('.jpg', frame_bgr, [int(cv2.IMWRITE_JPEG_QUALITY), JPEG_QUALITY])
        if ret:
            return {'filename': filename, 'data': buffer.tobytes()}
        else:
            logging.error("影像前處理失敗")
            return None

    def transmit(self, image_info):
        """
        上傳圖片，並依設定檔新增檔名標記。
        """
        check_and_update_loggers()  # 檢查日期並更新 log
        filename = image_info['filename']
        if self.adjust_camera_zoom_on_detection:
            filename_base, file_extension = os.path.splitext(filename)
            filename = f"{filename_base}_focus{self.focus_time}min{file_extension}"

        files = {'file': (filename, image_info['data'])}
        try:
            response = requests.post(self.upload_url, files=files, timeout=10)
            if response.status_code == 200:
                transmit_logger.info(f"成功上傳: {filename}")
            else:
                transmit_logger.warning(f"上傳失敗: {response.status_code}")
        except Exception as e:
            transmit_logger.error(f"上傳錯誤: {e}")



class StreamCanvas(tk.Canvas):
    """
    在 Tkinter 窗口中顯示攝影機即時影像的 Canvas。
    """
    def __init__(self, root, canvas_size, camera_manager, location):
        self.width = canvas_size[0]
        self.height = canvas_size[1]
        super().__init__(root, width=self.width, height=self.height, bg='black')
        self.camera_manager = camera_manager
        self.location = location
        self.refresh_rate = 1000 // 10  # 每秒更新10次畫面
        self.photo = None
        self.is_active = True  # 控制畫面更新的開關
        self.update_canvas()

    def toggle_display(self, active: bool):
        """
        開關畫面顯示功能。
        :param active: True 表示啟用畫面顯示，False 表示停止畫面顯示。
        """
        self.is_active = active
        state = "啟用" if active else "停止"
        main_logger.info(f"StreamCanvas ({self.location}) 狀態切換為 {state}")


    def update_canvas(self):
        """
        根據狀態控制畫面更新。
        """
        self.delete("all")
        if self.is_active:
            frame = self.camera_manager.current_frame
            if frame is not None:
                resized_frame = cv2.resize(frame, (self.width, self.height), interpolation=cv2.INTER_LINEAR)
                self.photo = ImageTk.PhotoImage(image=Image.fromarray(resized_frame))
                self.create_image(0, 0, image=self.photo, anchor="nw")
                self.create_text(
                    self.width - 10, self.height - 10, anchor='se', text=self.location,
                    fill='white', font=('Microsoft YaHei', 14, 'bold')
                )
        self.after_idle(self.update_canvas)

class DisplayController:
    """
    控制畫布顯示與按鈕文字狀態的類別。
    """
    def __init__(self, canvases, button):
        self.canvases = canvases
        self.button = button
        self.is_active = True  # 初始狀態：啟用畫面
        self.update_button()

    def toggle_display(self):
        """
        切換所有畫布的顯示狀態。
        """
        self.is_active = not self.is_active
        state = "啟用" if self.is_active else "停止"
        main_logger.info(f"所有畫布狀態切換為 {state}")
        for canvas in self.canvases:
            canvas.toggle_display(self.is_active)
        self.update_button()

    def update_button(self):
        """
        更新按鈕文字與背景顏色。
        """
        if self.is_active:
            self.button.config(
                text="畫面展示",
                bg="green",
                fg="white"  # 字體顏色
            )
        else:
            self.button.config(
                text="畫面展示",
                bg="red",
                fg="white"
            )


def notify_server_on_startup(config_path='smoke_camera_config.json'):
    """
    啟動時通知 Server 重置相關資料夾與影格。
    """
    try:
        config = utility.load_config(config_path)
        server_url = config.get("server_notify_url", None)

        if not server_url:
            main_logger.warning("Server notify URL not found in configuration.")
            return

        response = requests.post(server_url, json={"message": "workstation_started"}, timeout=10)
        if response.status_code == 200:
            main_logger.info("成功通知 Server 清除input資料夾。")
        else:
            main_logger.warning(f"無法通知 Server，狀態碼: {response.status_code}, 訊息: {response.text}")
    except requests.exceptions.RequestException as e:
        main_logger.warning(f"無法連接到 Server 清除input資料夾")
    except Exception as e:
        main_logger.warning(f"通知 Server 發生未預期的錯誤: {e}")


def main():
    main_logger.info("主程式啟動")
    try:
        # 嘗試通知 Server，但即使失敗也能繼續執行
        notify_server_on_startup()

        # 初始化 Tkinter 視窗
        window = tk.Tk()
        window.withdraw()  # 隱藏主視窗，直到驗證完成
        window.protocol("WM_DELETE_WINDOW", lambda: utility.on_closing(window))

        # 定義登入結果變數
        login_successful = {"can_open": False, "can_close": False}

        def on_success(can_open, can_close):
            login_successful["can_open"] = can_open
            login_successful["can_close"] = can_close
            window.deiconify()  # 驗證通過後顯示主視窗

        # 顯示登入對話框
        utility.show_login_window(on_success, window, 2)

        if not login_successful["can_open"]:
            messagebox.showerror("權限不足", "您沒有權限開啟此視窗。")
            return

        # 設定 Tkinter 主視窗
        window.title('煙霧檢測總畫面')
        window.state('zoomed')

        # 視窗大小與布局參數
        win_size = [1920, 1020]
        num_rows = 3
        num_cols = 5
        canvas_size = (win_size[0] // num_cols, win_size[1] // num_rows)
        window.geometry(f"{win_size[0]}x{win_size[1]}")
        window.configure(bg='#AFEEEE')
        window.resizable(width=1, height=1)

        # 載入設定與攝影機資訊
        config = utility.load_config('smoke_camera_config.json')
        display_channels = config.get('display_cameras', [])
        cameras_info = config['cameras']

        # 過濾出要顯示的攝影機列表
        selected_cameras = [cam for cam in cameras_info if int(cam['NVR_channel']) in display_channels]

        # 創建攝影機物件並啟動串流執行緒
        cameras = {}
        canvases = []
        for idx, camera in enumerate(selected_cameras):
            ip = camera['ip']
            cameras[ip] = CameraStreamManager(ip)
            threading.Thread(target=cameras[ip].stream_controller, daemon=True).start()

            # 創建 StreamCanvas
            location = camera.get('location', '未知位置')
            canvas = StreamCanvas(window, canvas_size, cameras[camera['ip']], location)
            canvas.place(x=canvas_size[0] * (idx % num_cols), y=canvas_size[1] * (idx // num_cols))
            canvases.append(canvas)

        # 啟動 PTZ 功能（若已啟用）
        for camera in cameras_info:
            ip = camera["ip"]
            function_switches = camera.get("function_switches", {})
            if function_switches.get("enable_camera_ptz", False):
                ptz_username, ptz_password = config.get("PTZ_config", ["admin", "hs888888"])
                ptz_control = PTZControl(ip, ptz_username, ptz_password)
                threading.Thread(target=ptz_control.move, daemon=True).start()
                main_logger.info(f"已啟動巡弋功能: 攝影機 {ip}")

        # 創建控制按鈕
        control_button = tk.Button(window, text="")
        display_controller = DisplayController(canvases, control_button)

        control_button.config(command=display_controller.toggle_display)
        control_button.place(x=win_size[0] / 2 - 50, y=win_size[1] - 50)  # 居中放置按鈕

        window.mainloop()
    except Exception as e:
        main_logger.exception(f"主程式執行期間發生錯誤: {e}")
    finally:
        # 清理資源
        for ip, camera in cameras.items():
            camera.is_running.clear()
            main_logger.info(f"已停止攝影機串流: {ip}")
        if 'window' in locals():
            window.destroy()
        main_logger.info("程式結束")


if __name__ == "__main__":
    main()
