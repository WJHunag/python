#===================================
# 此程式用於讀取 Video 來源
#====================================

import cv2
import time
import argparse
import requests
import logging
import os
from datetime import datetime
import utility  # 使用原始程式的 utility 模組來載入設定

# =========================== 常數定義 ===========================
JPEG_QUALITY = 40
UPLOAD_INTERVAL_SECONDS = 10  # 每隔10秒上傳一次圖片

# =========================== LOGGING 設定 ===========================
logger = logging.getLogger()
logger.setLevel(logging.INFO)
console_handler = logging.StreamHandler()
console_handler.setFormatter(logging.Formatter('[%(asctime)s] %(levelname)s: %(message)s'))
logger.addHandler(console_handler)

class VideoProcessor:
    def __init__(self, video_path, config_path='smoke_camera_config.json'):
        self.video_path = video_path
        self.upload_url = None
        self.cap = None
        self.last_upload_timestamp = 0

        # 載入設定檔
        config = utility.load_config(config_path)
        self.upload_url = config.get('upload_ip', '')
        if not self.upload_url:
            raise ValueError("無法從設定檔中找到有效的上傳 URL")
        logger.info(f"成功載入上傳 URL: {self.upload_url}")

    def initialize_video(self):
        """初始化影片讀取"""
        self.cap = cv2.VideoCapture(self.video_path)
        if not self.cap.isOpened():
            raise ValueError(f"無法讀取影片: {self.video_path}")
        logger.info(f"成功讀取影片: {self.video_path}")

    def process_video(self):
        """處理影片並定時上傳圖片"""
        while True:
            ret, frame = self.cap.read()
            if not ret:
                logger.info("影片已播放完畢")
                break

            current_time = time.time()
            if current_time - self.last_upload_timestamp >= UPLOAD_INTERVAL_SECONDS:
                self.last_upload_timestamp = current_time

                # 圖像處理與上傳
                processed_frame = self.preprocessing(frame)
                if processed_frame:
                    self.transmit(processed_frame)
            time.sleep(1 / self.cap.get(cv2.CAP_PROP_FPS))  # 控制讀取速率

    def preprocessing(self, frame):
        """壓縮影像並轉換成上傳格式"""
        # 模擬攝影機 IP
        simulated_ip = "127_0_0_1"  # 改成符合 Server 偵測需求的 IP 標識
        filename = f"{simulated_ip}_video_{datetime.now().strftime('%Y_%m_%d_%H_%M_%S')}.jpg"
        ret, buffer = cv2.imencode('.jpg', frame, [int(cv2.IMWRITE_JPEG_QUALITY), JPEG_QUALITY])
        if ret:
            return {'filename': filename, 'data': buffer.tobytes()}
        else:
            logger.error("影像前處理失敗")
            return None

    def transmit(self, image_info):
        """上傳圖片"""
        files = {'file': (image_info['filename'], image_info['data'])}
        try:
            response = requests.post(self.upload_url, files=files, timeout=10)
            if response.status_code == 200:
                logger.info(f"成功上傳: {image_info['filename']}")
            else:
                logger.warning(f"上傳失敗: {response.status_code}")
        except Exception as e:
            logger.error(f"上傳錯誤: {e}")

    def release_resources(self):
        """釋放資源"""
        if self.cap:
            self.cap.release()
        logger.info("已釋放影片資源")

def main():
    # 命令列參數解析
    parser = argparse.ArgumentParser(description="從影片擷取影像並上傳到伺服器")
    parser.add_argument('--video', required=True, help="影片路徑")
    parser.add_argument('--config', default='smoke_camera_config.json', help="設定檔路徑")
    args = parser.parse_args()

    processor = VideoProcessor(video_path=args.video, config_path=args.config)

    try:
        processor.initialize_video()
        processor.process_video()
    except Exception as e:
        logger.error(f"發生錯誤: {e}")
    finally:
        processor.release_resources()

if __name__ == "__main__":
    main()
