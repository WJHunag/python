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
import result_handler.bot as bot # 確保引用原有的 bot
import logging
import traceback
import sys
import io
import image_metadata
from .camera_ptz import PTZControl
from .drive_retention import DriveRetention, ReceiptStore

"""
multi_source_vision_hub_center.py (Modified)
基於原始版本修改：保留原有 RTSP/Login 邏輯，加入 Server-Driven 架構 (Fixed/Polling/Report)。
"""

# =========================== 環境變數設定 (保留原本) ===========================
os.environ["OMP_NUM_THREADS"] = "1"
os.environ["FFMPEG_THREADS"] = "1"

# =========================== 常數定義 (保留原本 + 新增) ===========================
RETRY_INTERVAL = 10          
NOTIFY_INTERVAL = 3600       
DISCONNECT_THRESHOLD = 3600  
WORK_START_HOUR = 7
WORK_END_HOUR = 19
SNAPSHOT_INTERVAL = 1800     
JPEG_QUALITY = 40

# [NEW] 新架構設定
 # =========================== [NEW] Drive 全域設定/快取 (依 folder_id 共用列檔結果) ===========================
DRIVE_SETTINGS = None              # 由 main() 讀 config 後注入
_DRIVE_CLIENT = None               # lazy init
_DRIVE_INDEXES = {}                # folder_id -> DriveIndexCache
_DRIVE_RUNTIME_LOCK = threading.RLock()

# =========================== LOGGING 設定 (完全保留原本) ===========================
current_date = datetime.now().strftime('%Y-%m-%d')
log_folder = "logs"
_logger_rotation_lock = threading.RLock()


def _replace_logger_handlers(logger, log_file):
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        try:
            handler.close()
        except Exception:
            pass

    file_handler = logging.FileHandler(log_file, mode='a', encoding='utf-8')
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(logging.Formatter('[%(asctime)s] %(levelname)s: %(message)s'))
    logger.addHandler(file_handler)

    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(logging.Formatter('[%(asctime)s] %(levelname)s: %(message)s'))
    logger.addHandler(console_handler)

def setup_logger(log_type="main"):
    logger_date = datetime.now().strftime('%Y-%m-%d')
    os.makedirs(log_folder, exist_ok=True)
    log_file = os.path.join(
        log_folder,
        f"{logger_date}_connection.log" if log_type == "main" else f"{logger_date}_transmit.log"
    )

    logger = logging.getLogger(log_type)
    logger.propagate = False
    logger.setLevel(logging.INFO)
    with _logger_rotation_lock:
        _replace_logger_handlers(logger, log_file)
    return logger
    
def check_and_update_loggers():
    global main_logger, transmit_logger, current_date
    new_date = datetime.now().strftime('%Y-%m-%d')

    if new_date == current_date:
        return

    with _logger_rotation_lock:
        if new_date == current_date:
            return

        os.makedirs(log_folder, exist_ok=True)
        current_date = new_date
        log_file_main = os.path.join(log_folder, f"{current_date}_connection.log")
        log_file_transmit = os.path.join(log_folder, f"{current_date}_transmit.log")
        _replace_logger_handlers(main_logger, log_file_main)
        _replace_logger_handlers(transmit_logger, log_file_transmit)

        main_logger.info(f"日誌已更新至新檔案: {log_file_main}")
        transmit_logger.info(f"日誌已更新至新檔案: {log_file_transmit}")

main_logger = setup_logger("main")
transmit_logger = setup_logger("transmit")


# =========================== [NEW] 通訊輔助函式 ===========================
def report_polling_status(server_ip, server_port, ws_ip, current_list, restarted=False):
    """回報輪詢狀態 (含解鎖訊號)"""
    url = f"http://{server_ip}:{server_port}/polling_report"
    payload = {
        "workstation_ip": ws_ip,
        "current_polling_cameras": list(current_list),
        "restarted": restarted
    }
    def _send():
        try:
            requests.post(url, json=payload, timeout=5)
            if restarted:
                main_logger.info(f"[Report] 發送啟動訊號 (Restarted=True)")
        except Exception as e:
            main_logger.error(f"[Report] Polling report failed: {e}")
    threading.Thread(target=_send, daemon=True).start()

def report_disconnection(server_ip, server_port, ws_ip, disconnect_list):
    """回報斷線清單"""
    url = f"http://{server_ip}:{server_port}/disconnection_report"
    payload = {
        "workstation_ip": ws_ip,
        "disconnect_cameras": list(disconnect_list)
    }
    def _send():
        try:
            requests.post(url, json=payload, timeout=5)
            main_logger.warning(f"[Report] Disconnection report sent: {disconnect_list}")
        except Exception as e:
            main_logger.error(f"[Report] Disconnection report failed: {e}")
    threading.Thread(target=_send, daemon=True).start()


# =========================== 修改後的串流管理器 ===========================
class CameraStreamManager:
    """
    修改說明：
    修正 __init__ 接收參數的方式，正確解析 cam_info 字典。
    """
    def __init__(self, cam_info, server_ip, server_port, ws_ip, shared_disc_set, base_snapshot_dir="images"):
        # [FIX] 修正點 1: 第一個參數是字典 (info)，需從中取出 'ip' 字串
        self.ip = cam_info['ip'] 
        
        # [NEW] 注入參數
        self.server_ip = server_ip
        self.server_port = int(server_port)
        self.ws_ip = ws_ip
        self.shared_disc_set = shared_disc_set
        
        self.is_stream_active = False
        self.stream_capture = None
        self.current_frame = None
        self._capture_lock = threading.Lock()
        self._lifecycle_lock = threading.Lock()
        self._run_generation = 0
        self.last_disconnect_time = datetime.now()
        self.last_notify_time = time.time() - NOTIFY_INTERVAL
        self.last_snapshot_timestamp = time.time()
        self.initial_upload_wait = 60 + random.randint(10,30)
        self.upload_interval_seconds = 0 + random.randint(10,30)
        self.last_upload_timestamp = time.time()
        
        # [FIX] 修正點 2: 現在 self.ip 是字串，可以正常使用 replace
        self.snapshot_dir = os.path.join(base_snapshot_dir, self.ip.replace('.', '_'))

        if not os.path.exists(self.snapshot_dir):
            os.makedirs(self.snapshot_dir)

        self.is_running = threading.Event()
        
        # 載入 distributed_config 供 assign_rtsp_url 使用
        config = utility.load_config()

        self.upload_url = utility.get_upload_url({"server_ip": self.server_ip, "server_port": self.server_port})

        # [NEW] DRIVE 模式不需要 rtsp_ip（而且原本 assign_rtsp_url 會因不支援的 connection_type 而 raise）
        self.connection_type = (cam_info.get('connection_type') or '').upper()
        if self.connection_type == 'DRIVE':
            self.rtsp_ip = None
        else:
            # 這裡的 assign_rtsp_url 依賴 self.ip (字串)，現在已經正確賦值
            self.rtsp_ip = self.assign_rtsp_url_with_40_rule(config)

        # [FIX] 修正點 3: 直接使用傳入的 cam_info 字典讀取資訊，不需再查一次 config
        self.channel_id = int(cam_info.get('NVR_channel', 0))

        function_switches = cam_info.get('function_switches', {})
        self.adjust_camera_zoom_on_detection = function_switches.get('adjust_camera_zoom_on_detection', False)
        self.focus_time = function_switches.get('focus_time', 0)

    # [KEEP] 下面這段 assign_rtsp_url 保持不變，與您舊版一致
    def assign_rtsp_url(self, config):
        camera = next((c for c in config['cameras'] if c['ip'] == self.ip), None)
        if not camera:
            raise ValueError(f"未知的攝影機 IP: {self.ip}")
        conn_type = camera.get('connection_type')
        if conn_type == 'NVR':
            try:
                raw_ch = int(camera.get('NVR_channel', 1))
            except Exception:
                raw_ch = 1
            mapped_ch = ((max(1, raw_ch) - 1) % 20) + 1
            nvr_ip = camera.get('NVR_IP', '')
            return f"rtsp://admin:hs888888@{nvr_ip}:554/cam/realmonitor?channel={mapped_ch}&subtype=0"
        elif conn_type == 'singel':
            return f"rtsp://admin:admin12345@{self.ip}:554/live/0/main"
        elif conn_type == 'multi':
            return f"rtsp://admin:hs888888@{self.ip}:554/cam/realmonitor?channel=1&subtype=0"
        else:
            raise ValueError(f"不支援的攝影機連線類型: {conn_type}")
    
    def _normalize_channel_61_to_100(self, ch):
            """
            1~60 維持原值；
            61~89 → ch-40；
            90~100 → ch-89 (90變1, 91變2...)；
            141~145 → 依照 %20 映射成 1~5；
            其他轉 int 失敗回傳 1，其餘做 1~20 循環映射。
            """
            try:
                ch = int(ch)
            except (ValueError, TypeError):
                return 1

            # 61~89：減 40
            if 41 <= ch <= 90:
                return ch - 40
                
            # 90~100：90->1, 91->2... (減 89)
            if 91 <= ch <= 120:
                return ch - 90

            # 141~145：映射成 1~5
            if 141 <= ch <= 145:
                return ((ch - 1) % 20) + 1

            # 其他走 1~20 循環映射（21->1）
            return ((max(1, ch) - 1) % 20) + 1

    def assign_rtsp_url_with_40_rule(self, config):
        """
        做法B：NVR分支改用 61~100 → (ch-40) 的規則，且不做 %20；其他分支維持原本 assign_rtsp_url 的邏輯。
        """
        camera = next((c for c in config['cameras'] if c['ip'] == self.ip), None)
        if not camera:
            raise ValueError(f"未知的攝影機 IP: {self.ip}")

        conn_type = camera.get('connection_type')
        if conn_type == 'NVR':
            try:
                raw_ch = int(camera.get('NVR_channel', 1))
            except Exception:
                raw_ch = 1
            ch = self._normalize_channel_61_to_100(raw_ch)
            nvr_ip = camera.get('NVR_IP', '')
            return f"rtsp://admin:hs888888@{nvr_ip}:554/cam/realmonitor?channel={ch}&subtype=0"
        else:
            # 非 NVR 走原有流程
            return self.assign_rtsp_url(config)

    def _begin_run(self):
        with self._lifecycle_lock:
            self._run_generation += 1
            return self._run_generation

    def _invalidate_run(self):
        with self._lifecycle_lock:
            self._run_generation += 1

    def _is_run_active(self, run_generation):
        with self._lifecycle_lock:
            is_current = run_generation == self._run_generation
        return self.is_running.is_set() and is_current

    def _wait_while_active(self, seconds, run_generation):
        deadline = time.monotonic() + seconds
        while self._is_run_active(run_generation):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return True
            time.sleep(min(0.5, remaining))
        return False

    def stop(self):
        self.is_running.clear()
        self._invalidate_run()
        self.reset_camera_connection(wait_for_retry=False)

    def stream_controller(self):
        """主要控制器"""
        run_generation = self._begin_run()
        periodic_thread = threading.Thread(
            target=self.schedule_periodic_tasks,
            args=(run_generation,),
            daemon=True,
            name=f"camera-periodic-{self.channel_id}",
        )
        periodic_thread.start()

        try:
            while self._is_run_active(run_generation):
                try:
                    self.connect_to_camera()
                    self.read_camera_stream(run_generation)
                except ConnectionError:
                    if self._is_run_active(run_generation):
                        self.handle_camera_disconnection()
                except Exception as e:
                    main_logger.exception(f"連接攝影機 {self.ip} 失敗: {e}")
                    if self._is_run_active(run_generation):
                        self.reset_camera_connection()
        finally:
            self.reset_camera_connection(wait_for_retry=False)
            periodic_thread.join(timeout=1)

    def connect_to_camera(self):
        check_and_update_loggers()
        stream_capture = cv2.VideoCapture(self.rtsp_ip, cv2.CAP_FFMPEG)
        if not stream_capture.isOpened():
            stream_capture.release()
            raise ConnectionError(f"無法連接至攝影機 {self.ip}")

        with self._capture_lock:
            previous_capture = self.stream_capture
            self.stream_capture = stream_capture
        if previous_capture and previous_capture is not stream_capture:
            previous_capture.release()

        self.is_stream_active = True
        self.last_disconnect_time = None
        main_logger.info(f"成功連接攝影機 {self.ip}")
        # [NEW] Hook: 連線恢復
        self._on_connection_restored()

    def read_camera_stream(self, run_generation):
        """讀取影像"""
        while self._is_run_active(run_generation) and self.is_stream_active:
            with self._capture_lock:
                stream_capture = self.stream_capture
            if stream_capture is None:
                raise ConnectionError(f"攝影機 {self.ip} 串流資源不存在")

            ret, frame = stream_capture.read()
            if not self._is_run_active(run_generation):
                break
            if ret:
                # 這裡保留原版轉 RGB 邏輯，注意: Tkinter 顯示需要 RGB，但 OpenCV 處理通常用 BGR
                # 原版這裡寫 cvtColor(..., BGR2RGB)，所以 current_frame 是 RGB
                self.current_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            else:
                logging.warning(f"攝影機 {self.ip} 串流中斷")
                self.handle_camera_disconnection()
                break

    def schedule_periodic_tasks(self, run_generation):
        """定時任務"""
        if not self._wait_while_active(self.initial_upload_wait, run_generation):
            return
        self.last_upload_timestamp = time.time()
        self.upload_interval_seconds = random.randint(10, 30)

        while self._is_run_active(run_generation):
            if not self.is_stream_active:
                if not self._wait_while_active(1, run_generation):
                    break
                continue

            current_time = time.time()

            if current_time - self.last_snapshot_timestamp >= SNAPSHOT_INTERVAL and self.current_frame is not None:
                self.save_snapshot(self.current_frame)
                self.last_snapshot_timestamp = current_time

            if self.current_frame is not None and current_time - self.last_upload_timestamp >= self.upload_interval_seconds:
                processed_frame = self.preprocessing(self.current_frame)
                if processed_frame:
                    threading.Thread(target=self.transmit, args=(processed_frame,), daemon=True).start()
                self.last_upload_timestamp = current_time
                self.upload_interval_seconds = random.randint(10, 30)

            if not self._wait_while_active(1, run_generation):
                break

    def handle_camera_disconnection(self):
        """保留原版通知邏輯，加入回報 Hook"""
        # [NEW] Hook: 斷線發生 (在進入通知判斷前先標記)
        self.is_stream_active = False # 先標記避免 loop
        self._on_connection_lost()

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
                    # [KEEP] 保留原本的 bot 通知
                    # bot.smoke_notify_camera(self.ip)
                    bot.telegram_smoke_notify_camera(self.ip)
                    bot.discord_smoke_notify_camera(self.ip)
                    main_logger.warning(f"無法連接攝影機 {self.ip} 已超過30分鐘，已發送通知")
                except Exception as e:
                    main_logger.error(f"通知發送失敗: {e}")
                finally:
                    self.last_notify_time = current_time.timestamp()

        self.reset_camera_connection()

    def reset_camera_connection(self, wait_for_retry=True):
        with self._capture_lock:
            stream_capture = self.stream_capture
            self.stream_capture = None
        if stream_capture:
            stream_capture.release()
        self.is_stream_active = False
        self.current_frame = None
        main_logger.info(f"攝影機 {self.ip} 的連線已重置")
        if wait_for_retry and self.is_running.is_set():
            time.sleep(RETRY_INTERVAL)

    def save_snapshot(self, frame):
        filename = f"{datetime.now().strftime('%Y_%m_%d_%H_%M_%S')}.jpg"
        path = os.path.join(self.snapshot_dir, filename)
        # 原版 current_frame 是 RGB，imwrite 需要 BGR
        cv2.imwrite(path, cv2.cvtColor(frame, cv2.COLOR_RGB2BGR))
        logging.info(f"保存截圖: {filename}")

    def preprocessing(self, frame):
        # 原版 current_frame 是 RGB，這裡轉 BGR
        frame_bgr = cv2.cvtColor(frame, cv2.COLOR_RGB2BGR)
        capture_dt = datetime.now().astimezone()
        filename = f"{self.ip.replace('.', '_')}_{capture_dt.strftime('%Y_%m_%d_%H_%M_%S')}.jpg"
        ret, buffer = cv2.imencode('.jpg', frame_bgr, [int(cv2.IMWRITE_JPEG_QUALITY), JPEG_QUALITY])
        if ret:
            metadata = image_metadata.build_source_metadata(
                camera_ip=self.ip,
                filename=filename,
                nvr_channel=self.channel_id,
                capture_time=capture_dt.isoformat(timespec="seconds"),
                source="camera",
                focus_time_min=self.focus_time if self.adjust_camera_zoom_on_detection else None,
                connection_type=self.connection_type,
            )
            jpeg_bytes = image_metadata.maybe_attach_metadata_to_jpeg_bytes(buffer.tobytes(), metadata)
            return {'filename': filename, 'data': jpeg_bytes, 'metadata': metadata}
        else:
            logging.error("影像前處理失敗")
            return None

    def transmit(self, image_info):
        check_and_update_loggers()
        filename = image_info['filename']
        data = image_info['data']
        metadata = dict(image_info.get('metadata') or {})
        if self.adjust_camera_zoom_on_detection:
            filename_base, file_extension = os.path.splitext(filename)
            filename = f"{filename_base}_focus{self.focus_time}min{file_extension}"
            if metadata:
                metadata["focus_time_min"] = self.focus_time
                metadata["legacy_filename"] = filename
                metadata["group_id"] = os.path.splitext(filename)[0]
                data = image_metadata.maybe_attach_metadata_to_jpeg_bytes(data, metadata)

        files = {'file': (filename, data)}
        try:
            # [MOD] upload_url 已經在 init 指向 routing_server
            response = requests.post(self.upload_url, files=files, timeout=10)
            if response.status_code == 200:
                transmit_logger.info(f"成功上傳: {filename}")
            else:
                transmit_logger.warning(f"上傳失敗: {response.status_code}")
        except Exception as e:
            transmit_logger.error(f"上傳錯誤: {e}")

    # [NEW] 子類別 Hook (預設不做事)
    def _on_connection_lost(self): pass
    def _on_connection_restored(self): pass


# =========================== [NEW] 控制器子類別 ===========================
class FixedController(CameraStreamManager):
    """固定控制器：啟動即連線，斷線需回報 Server"""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.is_running.set() # 固定相機預設啟動

    def _on_connection_lost(self):
        # 加入共用集合
        self.shared_disc_set.add(self.channel_id)
        # 回報 Server
        report_disconnection(self.server_ip, self.server_port, self.ws_ip, self.shared_disc_set)

    def _on_connection_restored(self):
        if self.channel_id in self.shared_disc_set:
            self.shared_disc_set.discard(self.channel_id)
            report_disconnection(self.server_ip, self.server_port, self.ws_ip, self.shared_disc_set)

class PollingController(CameraStreamManager):
    """輪詢控制器：可開關，斷線需回報 Server"""
    def set_active(self, active: bool):
        if active:
            if not self.is_running.is_set():
                main_logger.info(f"啟動輪詢相機: {self.ip}")
                self.is_running.set()
                threading.Thread(target=self.stream_controller, daemon=True).start()
        else:
            if self.is_running.is_set():
                # main_logger.info(f"停止輪詢相機: {self.ip}")
                self.stop()
                # reset 會在 stream_controller loop 結束時被呼叫，或手動強制 release 也可以
                # 這裡依賴 loop 檢測 is_running 後退出

    def _on_connection_lost(self):
        if self.channel_id not in self.shared_disc_set:
            self.shared_disc_set.add(self.channel_id)
            report_disconnection(self.server_ip, self.server_port, self.ws_ip, self.shared_disc_set)

    def _on_connection_restored(self):
        if self.channel_id in self.shared_disc_set:
            self.shared_disc_set.discard(self.channel_id)
            report_disconnection(self.server_ip, self.server_port, self.ws_ip, self.shared_disc_set)


# =========================== [NEW] 輪詢調度器 ===========================
class PollingScheduler:
    def __init__(self, polling_controllers, polling_minutes, server_ip, server_port, ws_ip, on_update_ui):
        self.controllers = polling_controllers # Dict[channel_id, Controller]
        self.channel_list = list(self.controllers.keys())
        self.interval = max(1, polling_minutes) * 60
        self.server_ip = server_ip
        self.server_port = int(server_port)
        self.ws_ip = ws_ip
        self.on_update_ui = on_update_ui 
        
        # 分組 (每組5支)
        self.groups = [self.channel_list[i:i + 5] for i in range(0, len(self.channel_list), 5)]
        self.current_idx = 0
        self.running = False

    def start(self):
        if not self.groups: return
        self.running = True
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        while self.running:
            current_group_ids = self.groups[self.current_idx]
            
            # 開關控制器
            for ch_id, ctrl in self.controllers.items():
                if ch_id in current_group_ids:
                    ctrl.set_active(True)
                else:
                    ctrl.set_active(False)
            
            # 回報
            report_polling_status(self.server_ip, self.server_port, self.ws_ip, current_group_ids)
            
            # 更新 UI
            if self.on_update_ui:
                self.on_update_ui(current_group_ids)
            
            time.sleep(self.interval)
            self.current_idx = (self.current_idx + 1) % len(self.groups)

# =========================== [NEW] Drive Client / Index Cache ===========================
class DriveClient:
    """
    使用 Service Account 讀取 Drive folder 內檔案。
    依賴: google-auth, google-api-python-client
    """
    def __init__(self, credentials_path):
        try:
            from google.oauth2 import service_account
            from googleapiclient.discovery import build
            from google_auth_httplib2 import AuthorizedHttp
            import httplib2
        except Exception as e:
            raise RuntimeError(
                "缺少 Google Drive API 依賴套件。請安裝: pip install google-api-python-client google-auth"
            ) from e

        scope = 'drive' if _drive_settings().get('cleanup_enabled', False) is True else 'drive.readonly'
        scopes = ['https://www.googleapis.com/auth/' + scope]
        creds = service_account.Credentials.from_service_account_file(credentials_path, scopes=scopes)
        # cache_discovery=False 避免某些環境下的 discovery cache 問題
        self.service = build('drive', 'v3', http=AuthorizedHttp(creds, http=httplib2.Http(timeout=15)),
                             cache_discovery=False)
        self._api_lock = threading.RLock()
        self._max_retries = 3

    def _run_api_call(self, description, func):
        delay = 0.5
        for attempt in range(1, self._max_retries + 1):
            try:
                with self._api_lock:
                    return func()
            except Exception as e:
                if attempt >= self._max_retries:
                    raise
                main_logger.warning(
                    f"[DRIVE] {description} 失敗，準備重試 {attempt}/{self._max_retries}: {e}"
                )
                time.sleep(delay)
                delay *= 2

    def list_recent_files(self, folder_id, newer_than_iso=None, page_size=200):
        # Drive query: parent, optional modifiedTime window.
        query_parts = [f"'{folder_id}' in parents", "trashed=false"]
        if newer_than_iso:
            query_parts.append(f"modifiedTime > '{newer_than_iso}'")
        q = " and ".join(query_parts)
        resp = self._run_api_call(
            f"列檔 folder_id={folder_id}",
            lambda: self.service.files().list(
                q=q,
                orderBy='modifiedTime desc',
                pageSize=page_size,
                fields='files(id,name,modifiedTime,mimeType)'
            ).execute()
        )
        return resp.get('files', []) or []

    def download_file_bytes(self, file_id):
        try:
            from googleapiclient.http import MediaIoBaseDownload
        except Exception as e:
            raise RuntimeError(
                "缺少 google-api-python-client (MediaIoBaseDownload)。請安裝: pip install google-api-python-client"
            ) from e

        def _download():
            request = self.service.files().get_media(fileId=file_id)
            fh = io.BytesIO()
            downloader = MediaIoBaseDownload(fh, request)
            done = False
            while not done:
                status, done = downloader.next_chunk()
            return fh.getvalue()

        return self._run_api_call(f"下載檔案 file_id={file_id}", _download)

    def list_cleanup_page(self, folder_id, cutoff, page_token, page_size):
        folder = folder_id.replace('\\', '\\\\').replace("'", "\\'")
        query = (f"'{folder}' in parents and trashed=false and "
                 f"mimeType contains 'image/' and modifiedTime < '{cutoff}'")
        return self._run_api_call('cleanup list', lambda: self.service.files().list(
            q=query, pageSize=page_size, pageToken=page_token,
            orderBy='modifiedTime', supportsAllDrives=True, includeItemsFromAllDrives=True,
            fields='nextPageToken,incompleteSearch,files(id)').execute())

    def get_file_info(self, file_id):
        return self._run_api_call('cleanup verify', lambda: self.service.files().get(
            fileId=file_id, supportsAllDrives=True,
            fields='id,parents,modifiedTime,mimeType,trashed,md5Checksum,size').execute())

    def delete_source_file(self, file_id):
        def remove():
            try:
                return self.service.files().delete(fileId=file_id, supportsAllDrives=True).execute()
            except Exception as exc:
                if getattr(getattr(exc, 'resp', None), 'status', None) != 404:
                    raise
        return self._run_api_call('cleanup delete', remove)


class DriveIndexCache:
    """
    單一 Drive folder 的列檔快取。
    每台 DRIVE camera 使用自己的 folder_id。
    """
    def __init__(self, client, folder_id, recent_window_minutes=0, refresh_min_interval=1.0):
        self.client = client
        self.folder_id = folder_id
        self.recent_window_minutes = int(recent_window_minutes)
        self.refresh_min_interval = max(0.5, float(refresh_min_interval))

        self._lock = threading.Lock()
        self._last_refresh_ts = 0.0
        self._files = []  # raw file list

    @staticmethod
    def _iso_utc_minus_minutes(minutes):
        # 以 UTC 表示（Drive modifiedTime 走 RFC3339 / ISO）
        import datetime as _dt
        dt = _dt.datetime.utcnow() - _dt.timedelta(minutes=minutes)
        min_dt = _dt.datetime(1970, 1, 1)
        if dt < min_dt:
            dt = min_dt
        return dt.replace(microsecond=0).isoformat() + 'Z'

    def refresh_if_needed(self):
        now = time.time()
        if now - self._last_refresh_ts < self.refresh_min_interval:
            return
        with self._lock:
            now2 = time.time()
            if now2 - self._last_refresh_ts < self.refresh_min_interval:
                return
            newer_than = None
            if self.recent_window_minutes > 0:
                newer_than = self._iso_utc_minus_minutes(self.recent_window_minutes)
            try:
                self._files = self.client.list_recent_files(self.folder_id, newer_than_iso=newer_than)
                self._last_refresh_ts = time.time()
            except Exception as e:
                main_logger.error(f"[DRIVE] 列檔失敗: {e}")
                # 不更新 last_refresh_ts，讓下次還會再試

    def pick_latest_file(self):
        """
        回傳 folder 內最新檔案；每台相機一個 folder 時使用。
        """
        self.refresh_if_needed()
        if not self._files:
            return None
        return self._files[0]

def _drive_settings():
    return DRIVE_SETTINGS or {}


def _init_drive_runtime_if_needed(credentials_path=None):
    global _DRIVE_CLIENT
    if _DRIVE_CLIENT is not None:
        return

    ds = _drive_settings()
    cred_path = credentials_path or ds.get('credentials_path') or os.environ.get('GOOGLE_APPLICATION_CREDENTIALS')
    if not cred_path:
        raise RuntimeError(
            "[DRIVE] 缺少 Google Drive credentials_path。"
            "請在 config 最上層 drive_settings.credentials_path、"
            "DRIVE camera.credentials_path，或環境變數 GOOGLE_APPLICATION_CREDENTIALS 設定 service account json。"
        )

    with _DRIVE_RUNTIME_LOCK:
        if _DRIVE_CLIENT is None:
            _DRIVE_CLIENT = DriveClient(cred_path)


def _get_drive_index_for_folder(folder_id, credentials_path=None):
    global _DRIVE_INDEXES
    _init_drive_runtime_if_needed(credentials_path)

    folder_key = str(folder_id or '').strip()
    if not folder_key:
        raise RuntimeError("[DRIVE] 缺少 folder_id。")

    ds = _drive_settings()
    recent_window = ds.get('recent_window_minutes', 0)
    refresh_min_interval = ds.get('refresh_min_interval', 1.0)

    with _DRIVE_RUNTIME_LOCK:
        index = _DRIVE_INDEXES.get(folder_key)
        if index is None:
            index = DriveIndexCache(
                client=_DRIVE_CLIENT,
                folder_id=folder_key,
                recent_window_minutes=recent_window,
                refresh_min_interval=refresh_min_interval
            )
            _DRIVE_INDEXES[folder_key] = index
        return index


# =========================== [NEW] Drive Stream Manager (繼承，僅換來源) ===========================
class DriveStreamManager(CameraStreamManager):
    """
    connection_type == 'DRIVE' 時使用：
    - connect_to_camera(): 初始化 Drive client/index，視為連線成功
    - read_camera_stream(): 以固定頻率抓最新圖片並更新 current_frame (RGB)
    """
    def __init__(self, cam_info, *args, **kwargs):
        super().__init__(cam_info, *args, **kwargs)
        # 取圖/斷線參數（只從 drive_settings 讀；不影響原本環境變數設計）
        ds = _drive_settings()
        self.drive_poll_interval = max(1, int(ds.get('poll_interval_seconds', 5)))
        raw_stale_timeout = ds.get('stale_timeout_seconds', 0)
        self.drive_stale_timeout = max(0, int(raw_stale_timeout or 0))
        self.drive_folder_id = str(cam_info.get('folder_id') or '').strip()
        self.drive_credentials_path = str(
            cam_info.get('credentials_path') or ds.get('credentials_path') or os.environ.get('GOOGLE_APPLICATION_CREDENTIALS') or ''
        ).strip()

        self._drive_index = None
        self._drive_connect_ts = 0.0
        self._drive_last_seen_ts = 0.0
        self._drive_last_file_id = None
        self._drive_last_transmitted_file_id = None
        self._drive_reconnect_not_before = 0.0
        self._drive_retention = None

    def connect_to_camera(self):
        check_and_update_loggers()
        wait_seconds = self._drive_reconnect_not_before - time.time()
        if wait_seconds > 0:
            time.sleep(wait_seconds)

        if not self.drive_folder_id:
            raise RuntimeError(f"[DRIVE] {self.ip} 缺少 folder_id。每台 DRIVE 攝影機都需要設定自己的 folder_id。")
        self._drive_index = _get_drive_index_for_folder(self.drive_folder_id, self.drive_credentials_path)

        # Drive 模式視為「來源可用」即連線成功；實際是否有新圖由 read loop 判斷
        self.is_stream_active = True
        self.last_disconnect_time = None
        self._drive_connect_ts = time.time()
        self._drive_last_seen_ts = 0.0
        self._drive_last_file_id = None
        main_logger.info(f"[DRIVE] 成功初始化來源: {self.ip} folder_id={self.drive_folder_id}")
        self._on_connection_restored()

    def read_camera_stream(self, run_generation):
        while self._is_run_active(run_generation) and self.is_stream_active:
            try:
                drive_index = self._drive_index or _get_drive_index_for_folder(
                    self.drive_folder_id,
                    self.drive_credentials_path
                )

                f = drive_index.pick_latest_file()
                if f:
                    file_id = f.get('id')
                    # 若是同一張就不重複下載
                    if file_id and file_id != self._drive_last_file_id:
                        data = _DRIVE_CLIENT.download_file_bytes(file_id)
                        arr = np.frombuffer(data, dtype=np.uint8)
                        frame_bgr = cv2.imdecode(arr, cv2.IMREAD_COLOR)
                        if frame_bgr is not None:
                            self.current_frame = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
                            self._drive_last_file_id = file_id
                            self._drive_last_seen_ts = time.time()
                            self._transmit_drive_frame_once(file_id, self.current_frame, f.get('modifiedTime'))
                            if self._drive_retention is None:
                                self._drive_retention = DriveRetention(
                                    _DRIVE_CLIENT, self.drive_folder_id, _drive_settings(), main_logger)

                # Drive 不是連續串流；預設不因「沒有新圖」判斷斷線。
                # 若現場確定上游週期，可設定 stale_timeout_seconds > 0 啟用停更監控。
                if self.drive_stale_timeout > 0:
                    if self._drive_last_seen_ts > 0:
                        if time.time() - self._drive_last_seen_ts > self.drive_stale_timeout:
                            main_logger.warning(f"[DRIVE] {self.ip} 超過 {self.drive_stale_timeout}s 無新圖，視為斷線")
                            self.handle_camera_disconnection()
                            break
                    elif time.time() - self._drive_connect_ts > self.drive_stale_timeout:
                        main_logger.warning(f"[DRIVE] {self.ip} 啟動後超過 {self.drive_stale_timeout}s 仍未取得任何圖")
                        self.handle_camera_disconnection()
                        break

            except Exception as e:
                main_logger.error(f"[DRIVE] 取圖失敗 ({self.ip}): {e}")
                # 不中斷，讓逾時計時處理

            if not self._wait_while_active(self.drive_poll_interval, run_generation):
                break

    def schedule_periodic_tasks(self, run_generation):
        """
        Drive 來源只在新 file_id 出現時上傳偵測。
        這裡保留定期截圖，不沿用 RTSP 的 10~30 秒定時上傳。
        """
        while self._is_run_active(run_generation):
            if not self.is_stream_active:
                if not self._wait_while_active(1, run_generation):
                    break
                continue

            current_time = time.time()

            if self._drive_retention is not None:
                self._drive_retention.tick(active=lambda: self._is_run_active(run_generation))

            if current_time - self.last_snapshot_timestamp >= SNAPSHOT_INTERVAL and self.current_frame is not None:
                self.save_snapshot(self.current_frame)
                self.last_snapshot_timestamp = current_time

            if not self._wait_while_active(1, run_generation):
                break

    def _transmit_drive_frame_once(self, file_id, frame, modified_time=None):
        if not file_id or file_id == self._drive_last_transmitted_file_id:
            return

        processed_frame = self.preprocessing(frame)
        if not processed_frame:
            return

        # Persist the source association before /upload can return any results.
        group_id = processed_frame['metadata']['group_id']
        if self.adjust_camera_zoom_on_detection:
            group_id += f'_focus{self.focus_time}min'
        ReceiptStore().register(self.drive_folder_id, file_id, modified_time,
                                group_id)

        self._drive_last_transmitted_file_id = file_id
        main_logger.info(f"[DRIVE] 偵測到新圖片，送出一次偵測: {self.ip} file_id={file_id}")
        threading.Thread(target=self.transmit, args=(processed_frame,), daemon=True).start()

    def reset_camera_connection(self, wait_for_retry=True):
        # Drive 模式不需要 release VideoCapture
        self.is_stream_active = False
        self.current_frame = None
        self._drive_reconnect_not_before = time.time() + RETRY_INTERVAL
        main_logger.info(f"[DRIVE] {self.ip} 來源狀態已重置")
        if wait_for_retry and self.is_running.is_set():
            time.sleep(RETRY_INTERVAL)


class DriveFixedController(DriveStreamManager):
    """固定控制器：Drive 來源版本"""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.is_running.set()

    def _on_connection_lost(self):
        if self.channel_id not in self.shared_disc_set:
            self.shared_disc_set.add(self.channel_id)
            report_disconnection(self.server_ip, self.server_port, self.ws_ip, self.shared_disc_set)

    def _on_connection_restored(self):
        if self.channel_id in self.shared_disc_set:
            self.shared_disc_set.discard(self.channel_id)
            report_disconnection(self.server_ip, self.server_port, self.ws_ip, self.shared_disc_set)

# =========================== [MOD] StreamCanvas (加入邊框顏色) ===========================
class StreamCanvas(tk.Canvas):
    """
    在 Tkinter 窗口中顯示攝影機即時影像的 Canvas。
    修改：加入 role 與 check_disc_func 用於繪製狀態邊框。
    """
    def __init__(self, root, canvas_size, camera_manager, location, role='legacy', check_disc_func=None):
        self.width = canvas_size[0]
        self.height = canvas_size[1]
        super().__init__(root, width=self.width, height=self.height, bg='black')
        self.camera_manager = camera_manager
        self.location = location
        self.refresh_rate = 1000 // 10  
        self.photo = None
        self.is_active = True  
        # [NEW]
        self.role = role
        self.check_disc_func = check_disc_func
        self.update_canvas()

    def toggle_display(self, active: bool):
        self.is_active = active
        # state = "啟用" if active else "停止"
        # main_logger.info(f"StreamCanvas ({self.location}) 狀態切換為 {state}")

    def destroy_safe(self):
        self.is_active = False
        self.destroy()

    def update_canvas(self):
        try:
            self.delete("all")
            if self.is_active:
                frame = self.camera_manager.current_frame
                if frame is not None:
                    # 原本的 manager.current_frame 已經是 RGB (在 read_stream 轉過)
                    resized_frame = cv2.resize(frame, (self.width, self.height), interpolation=cv2.INTER_LINEAR)
                    self.photo = ImageTk.PhotoImage(image=Image.fromarray(resized_frame))
                    self.create_image(0, 0, image=self.photo, anchor="nw")
                    self.create_text(
                        self.width - 10, self.height - 10, anchor='se', text=self.location,
                        fill='white', font=('Microsoft YaHei', 14, 'bold')
                    )
                    
                    # [NEW] 繪製狀態邊框
                    color = ""
                    if self.check_disc_func and self.check_disc_func():
                        color = "red"
                    elif self.role == 'fixed':
                        color = "green"
                    elif self.role == 'polling':
                        color = "orange"
                    
                    if color:
                        self.create_rectangle(2, 2, self.width - 2, self.height - 2, outline=color, width=4)

            self.after_idle(self.update_canvas)
        except Exception:
             pass # 防止銷毀後呼叫報錯

# =========================== DisplayController (保留原本) ===========================
class DisplayController:
    def __init__(self, canvases, button):
        self.canvases = canvases
        self.button = button
        self.is_active = True  
        self.update_button()

    def toggle_display(self):
        self.is_active = not self.is_active
        state = "啟用" if self.is_active else "停止"
        main_logger.info(f"所有畫布狀態切換為 {state}")
        for canvas in self.canvases:
            canvas.toggle_display(self.is_active)
        self.update_button()

    def update_button(self):
        if self.is_active:
            self.button.config(
                text="畫面展示",
                bg="green",
                fg="white"
            )
        else:
            self.button.config(
                text="畫面展示",
                bg="red",
                fg="white"
            )

# =========================== 輔助 (保留原本) ===========================
def notify_server_on_startup(config_path='smoke_camera_config.json'):
    try:
        config = utility.load_config()
            
        server_url = config.get("server_notify_url", None)
        if not server_url:
            main_logger.warning("Server notify URL not found in configuration.")
            return

        response = requests.post(server_url, json={"message": "workstation_started"}, timeout=10)
        if response.status_code == 200:
            main_logger.info("成功通知 Server 清除input資料夾。")
        else:
            main_logger.warning(f"無法通知 Server，狀態碼: {response.status_code}")
    except Exception as e:
        main_logger.warning(f"通知 Server 發生錯誤: {e}")


# =========================== Main (重構流程) ===========================
def main():
    main_logger.info("主程式啟動 (Server-Driven Mode)")
    controllers = {}
    window = None
    try:
        # 0. 讀取中央派發的 distributed_config
        main_logger.info("讀取 distributed_config")
        config = utility.load_config()

        # [NEW] 注入 Drive 設定（只給 DRIVE 模式使用；不影響原本環境變數設計）
        global DRIVE_SETTINGS
        DRIVE_SETTINGS = config.get('drive_settings', None)

        notify_server_on_startup()

        # 1. 啟動模式（預設 interactive；排程用 service）
        try:
            parser = argparse.ArgumentParser(add_help=False)
            parser.add_argument(
                "--mode",
                default="interactive",
                choices=["interactive", "service"],
            )
            args, _ = parser.parse_known_args()
            startup_mode = args.mode
        except Exception as e:
            startup_mode = "interactive"
            main_logger.warning(f"啟動模式解析失敗，使用預設 interactive: {e}")

        main_logger.info(f"啟動模式: {startup_mode}")

        win_size = [1920, 1020]
        num_rows = 3
        num_cols = 5
        canvas_size = (win_size[0] // num_cols, win_size[1] // num_rows)

        # 2. interactive 才建立 Tk；service 必須維持純背景執行。
        if startup_mode == "interactive":
            window = tk.Tk()
            window.withdraw()
            window.protocol("WM_DELETE_WINDOW", lambda: utility.on_closing(window))

            login_successful = {"can_open": False, "can_close": False}

            def on_success(can_open, can_close):
                login_successful["can_open"] = can_open
                login_successful["can_close"] = can_close
                window.deiconify()

            utility.show_login_window(on_success, window, 2)
            window.title('煙霧檢測總畫面')
            window.state('zoomed')
            window.geometry(f"{win_size[0]}x{win_size[1]}")
            window.configure(bg='#AFEEEE')
            window.resizable(width=1, height=1)
        else:
            main_logger.info("Service 模式已啟用：不建立 Tk 視窗與攝影機畫布")

        # 3. 解析設定與分類相機
        server_ip = config.get('server_ip', '127.0.0.1')
        server_port = utility.get_server_port(config)
        polling_time = int(config.get('polling_time', 15))
        
        ws_map = config.get('workstation_ip_mapping', {})
        # 簡單取自己：假設 config 內這台機器的 IP 有對應到 mapping
        # 這裡為了穩健，若找不到就全開 (Fall back to legacy behavior) 或報錯
        # 這裡示範：取第一個 mapping 或 legacy display_cameras
        my_ws_ip = "127.0.0.1"
        fixed_ids = []
        polling_ids = []
        
        # 嘗試從 mapping 找
        if ws_map:
            my_ws_ip = list(ws_map.keys())[0] # 簡化：取第一個 key
            slot = ws_map[my_ws_ip]
            fixed_ids = [int(x) for x in slot.get('fixed_cameras', [])]
            polling_ids = [int(x) for x in slot.get('polling_cameras', [])]
        else:
            # Fallback: 使用舊版的 display_cameras 作為 fixed
            main_logger.warning("Mapping 不存在，使用舊版 display_cameras 模式")
            display_channels = config.get('display_cameras', [])
            fixed_ids = [int(x) for x in display_channels]
            my_ws_ip = "127.0.0.1"
        if ":" not in my_ws_ip:
            my_ws_ip = f"{my_ws_ip}:5001"

        all_cameras_info = {int(c['NVR_channel']): c for c in config.get('cameras', [])}
        
        # 4. 建立控制器
        polling_ctrls = {} # channel_id -> manager
        shared_disc_set = set()
        
        fixed_canvases = []
        polling_canvases = [] # 動態列表
        
        # =========================== [FIX] DRIVE 一律視為 Fixed（不輪巡） ===========================
        # 說明：
        # - 使用者要求 DRIVE 不用 Polling
        # - 若有人誤把 DRIVE 放進 polling_cameras，這裡會強制移到 fixed
        drive_ids = []
        for ch_id in list(fixed_ids) + list(polling_ids):
            info = all_cameras_info.get(ch_id)
            if not info:
                continue
            conn_type = (info.get('connection_type') or '').upper()
            if conn_type == 'DRIVE' and ch_id not in drive_ids:
                drive_ids.append(ch_id)

        if drive_ids:
            # 固定區：追加未包含的 drive_ids（保留原本 fixed_ids 的順序）
            for ch_id in drive_ids:
                if ch_id not in fixed_ids:
                    fixed_ids.append(ch_id)
            # 輪巡區：移除 drive_ids
            polling_ids = [x for x in polling_ids if x not in set(drive_ids)]
            main_logger.info(f"[DRIVE] 強制將 DRIVE 相機視為 Fixed（從 polling 移除）: {drive_ids}")

        # 建立 Fixed
        for idx, ch_id in enumerate(fixed_ids):
            info = all_cameras_info.get(ch_id)
            if not info: continue
            conn_type = (info.get('connection_type') or '').upper()
            # [FIX] Fixed 區塊必須使用 FixedController / DriveFixedController
            if conn_type == 'DRIVE':
                ctrl = DriveFixedController(info, server_ip, server_port, my_ws_ip, shared_disc_set)
            else:
                ctrl = FixedController(info, server_ip, server_port, my_ws_ip, shared_disc_set)
            controllers[ch_id] = ctrl
            threading.Thread(
                target=ctrl.stream_controller,
                daemon=True,
                name=f"camera-stream-{ch_id}",
            ).start()
            
            if window is not None:
                loc = info.get('location', '')
                cv = StreamCanvas(window, canvas_size, ctrl, loc, 'fixed', lambda cid=ch_id: cid in shared_disc_set)
                r, c = divmod(idx, num_cols)
                cv.place(x=c*canvas_size[0], y=r*canvas_size[1])
                fixed_canvases.append(cv)

        # 建立 Polling (不啟動)
        for ch_id in polling_ids:
            info = all_cameras_info.get(ch_id)
            if not info: continue
            
            conn_type = (info.get('connection_type') or '').upper()
            # [FIX] DRIVE 不用 Polling：保險起見，若仍出現就略過
            if conn_type == 'DRIVE':
                main_logger.warning(f"[DRIVE] 發現 DRIVE 相機出現在 polling_ids(ch={ch_id})，已略過（DRIVE 只走 Fixed）")
                continue

            ctrl = PollingController(info, server_ip, server_port, my_ws_ip, shared_disc_set)
            controllers[ch_id] = ctrl
            polling_ctrls[ch_id] = ctrl
            # 注意：這裡不 start thread，等 set_active(True)

        # 定義 UI 更新回呼
        def update_polling_ui(current_group_ids):
            if window is None:
                return

            def _task():
                for cv in polling_canvases: cv.destroy_safe()
                polling_canvases.clear()
                
                start_idx = len(fixed_ids)
                for i, ch_id in enumerate(current_group_ids):
                    ctrl = polling_ctrls.get(ch_id)
                    if not ctrl: continue
                    info = all_cameras_info.get(ch_id)
                    loc = info.get('location', '')
                    
                    cv = StreamCanvas(window, canvas_size, ctrl, loc, 'polling', lambda cid=ch_id: cid in shared_disc_set)
                    
                    total_idx = start_idx + i
                    r, c = divmod(total_idx, num_cols)
                    if r < num_rows:
                        cv.place(x=c*canvas_size[0], y=r*canvas_size[1])
                        polling_canvases.append(cv)
            window.after(0, _task)

        # 5. [CRITICAL] 強制發送啟動回報 (解鎖 Server)
        initial_polling = polling_ids[:5] if polling_ids else []
        report_polling_status(server_ip, server_port, my_ws_ip, initial_polling, restarted=True)

        # 6. 啟動調度器
        if polling_ids:
            ui_callback = update_polling_ui if window is not None else None
            scheduler = PollingScheduler(polling_ctrls, polling_time, server_ip, server_port, my_ws_ip, ui_callback)
            scheduler.start()

        # 7. 按鈕與 PTZ (保留原本)
        if window is not None:
            control_button = tk.Button(window, text="")
            display_controller = DisplayController(fixed_canvases, control_button)

            def dynamic_toggle():
                display_controller.canvases = fixed_canvases + polling_canvases
                display_controller.toggle_display()

            control_button.config(command=dynamic_toggle)
            control_button.place(x=win_size[0] / 2 - 50, y=win_size[1] - 50)

        # PTZ
        for camera in config.get('cameras', []):
            function_switches = camera.get("function_switches", {})
            if function_switches.get("enable_camera_ptz", False):
                ptz_username, ptz_password = config.get("PTZ_config", ["admin", "hs888888"])
                ptz_control = PTZControl(camera['ip'], ptz_username, ptz_password)
                threading.Thread(target=ptz_control.move, daemon=True).start()
                main_logger.info(f"已啟動巡弋功能: 攝影機 {camera['ip']}")

        if window is not None:
            window.mainloop()
        else:
            while any(camera.is_running.is_set() for camera in controllers.values()):
                time.sleep(1)
    except Exception as e:
        main_logger.exception(f"主程式執行期間發生錯誤: {e}")
    finally:
        for camera in controllers.values():
            camera.stop()
            main_logger.info(f"已停止攝影機串流: {camera.ip}")
        if window is not None:
            try:
                window.destroy()
            except tk.TclError:
                pass
        main_logger.info("程式結束")

if __name__ == "__main__":
    main()
