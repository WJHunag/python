# -*- coding: utf-8 -*-
import cv2
import pytesseract
import numpy as np
import configparser
import os
import re
import time
import argparse
import logging
from datetime import datetime
import subprocess, shlex, pathlib, sys
import shutil
from result_handler.bot import send_discord_notification  # 延用你原本的通知方法

# ========= 你的既有設定，保留 =========
# 若你有不同安裝路徑可改成參數 --tess_path 覆蓋
pytesseract.pytesseract.tesseract_cmd = "C:/Program Files/Tesseract-OCR/tesseract.exe"

config = configparser.ConfigParser()
config.read('./config.ini')
NOTIFY_TOKEN = config['discord']['webhook_test']

COOLDOWN = 180
last_notification_time = 0
TEMP_IMAGE_PATH = "temp_screenshot.jpg"

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("ocr_detection.log", encoding="utf-8"),
        logging.StreamHandler()
    ]
)

# ========= RTSP/FFmpeg 低延遲選項（可視需要調整）=========
# 讓 OpenCV 用 TCP、設定連線逾時與小 buffer（避免延遲累積）
os.environ.setdefault(
    "OPENCV_FFMPEG_CAPTURE_OPTIONS",
    "rtsp_transport;tcp|stimeout;5000000|buffer_size;102400"
)

def calculate_roi(position, frame_width, frame_height):
    """四個角落固定 100x45 的 ROI"""
    roi_width, roi_height = 195, 100
    if position == "left_top":
        return 100, 25, roi_width, roi_height
    elif position == "right_top":
        return frame_width - roi_width, 0, roi_width, roi_height
    elif position == "left_bottom":
        return 0, frame_height - roi_height, roi_width, roi_height
    elif position == "right_bottom":
        return frame_width - roi_width, frame_height - roi_height, roi_width, roi_height
    else:
        logging.error(f"無效的 ROI 位置: {position}")
        raise ValueError("無效的 ROI 位置，必須是 'left_top', 'right_top', 'left_bottom', 或 'right_bottom'")

def open_capture(src):
    """同一函式同時支援 RTSP 與檔案來源，內含簡單重試"""
    is_rtsp = isinstance(src, str) and src.lower().startswith("rtsp://")
    cap = None
    retry = 0
    while True:
        if is_rtsp:
            cap = cv2.VideoCapture(src, cv2.CAP_FFMPEG)
            # 嘗試縮小 buffer，避免高延遲
            try:
                cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            except Exception:
                pass
        else:
            cap = cv2.VideoCapture(src)
        if cap.isOpened():
            return cap
        retry += 1
        if retry <= 5:
            wait = min(2 * retry, 8)
            logging.warning(f"無法開啟來源（第 {retry} 次），{wait}s 後重試：{src}")
            time.sleep(wait)
        else:
            logging.error(f"無法開啟來源（已重試 {retry} 次）：{src}")
            return None

def preprocess_for_ocr(img_bgr):
    """提升 OCR 穩定度：灰階→放大→中值濾波→Otsu 正/反二值化擇優"""
    gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
    scaled = cv2.resize(gray, None, fx=2.0, fy=2.0, interpolation=cv2.INTER_CUBIC)
    scaled = cv2.medianBlur(scaled, 3)
    _, th1 = cv2.threshold(scaled, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    _, th2 = cv2.threshold(scaled, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    cfg = r'--oem 3 --psm 7 -c tessedit_char_whitelist=0123456789.-'
    t1 = pytesseract.image_to_string(th1, config=cfg)
    t2 = pytesseract.image_to_string(th2, config=cfg)
    # 選包含較多「數字樣式」的結果
    c1 = len(re.findall(r'[-+]?\d*\.?\d+', t1))
    c2 = len(re.findall(r'[-+]?\d*\.?\d+', t2))
    if c2 > c1:
        return th2, t2
    return th1, t1

def parse_temperature(text):
    """取第一個浮點數；若沒有就回 None"""
    m = re.search(r'[-+]?\d*\.?\d+', text)
    if not m:
        return None
    try:
        return float(m.group(0))
    except Exception:
        return None
    
def start_ffmpeg_recorder(rtsp_url, out_dir="recordings", segment_secs=600, use_mkv=True, transport="tcp",
                          use_date_dir=True, log_path="ffmpeg_record.log"):
    """
    用 FFmpeg 子程序從 RTSP 直接分段錄影（不重編碼）。
    - out_dir：輸出根資料夾
    - segment_secs：切檔秒數（例如 10 或 600）
    - use_mkv：True → .mkv（耐斷線）；False → .mp4
    - transport：'tcp' 或 'udp'
    - use_date_dir：是否使用 recordings/YYYYMMDD 子資料夾
    - log_path：FFmpeg 訊息輸出到這個檔案，方便除錯
    """
    # 1) 檢查 ffmpeg 是否在 PATH
    if shutil.which("ffmpeg") is None:
        raise RuntimeError("找不到 ffmpeg，請確認已安裝並加入 PATH")

    # 2) 準備輸出路徑（真的把資料夾建出來）
    base = pathlib.Path(out_dir)
    if use_date_dir:
        base = base / datetime.now().strftime("%Y%m%d")
    base.mkdir(parents=True, exist_ok=True)

    ext = "mkv" if use_mkv else "mp4"
    # 不把日期夾放在 pattern 裡，避免跨日時因資料夾不存在而失敗
    pattern = str(base / f"flir_%Y%m%d_%H%M%S.{ext}")

    cmd = (
        f'ffmpeg -hide_banner -loglevel info '
        f'-rtsp_transport {transport} -fflags nobuffer -flags low_delay '
        f'-i "{rtsp_url}" -map 0 -c copy '
        f'-f segment -segment_time {segment_secs} -segment_atclocktime 1 '
        f'-reset_timestamps 1 -strftime 1 "{pattern}"'
    )

    # 3) 把 FFmpeg 日誌寫進檔案，出問題好追
    log_f = open(log_path, "a", encoding="utf-8", errors="ignore")

    proc = subprocess.Popen(
        cmd if sys.platform.startswith("win") else shlex.split(cmd),
        shell=sys.platform.startswith("win"),
        stdout=log_f, stderr=log_f
    )
    return proc

def stop_ffmpeg_recorder(proc):
    if proc and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()

def main(source, threshold, location, roi_position, roi_box=None, tess_path=None, show=False, process_every_sec=1.0, record=False):
    global last_notification_time

    if tess_path:
        pytesseract.pytesseract.tesseract_cmd = tess_path

    cap = open_capture(source)
    if cap is None:
        return

    # 讀取畫面尺寸：RTSP 有時候 CAP_PROP 會是 0，先試讀一幀回推尺寸
    frame_width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)) or 0
    frame_height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)) or 0

    if frame_width == 0 or frame_height == 0:
        ok, probe = cap.read()
        if not ok:
            logging.error("來源可開啟但取不到任何畫面")
            cap.release()
            return
        frame_height, frame_width = probe.shape[:2]
    logging.info(f"來源影像大小：{frame_width}x{frame_height}")

    # 若啟用錄影且來源為 RTSP，啟動 FFmpeg 分段錄影
    rec_proc = None
    if record and isinstance(source, str) and source.lower().startswith("rtsp://"):
        rec_proc = start_ffmpeg_recorder(
            source,                 # 直接錄同一路 RTSP
            out_dir="temperature_recordings",   # 影片輸出根資料夾
            segment_secs=600,       # 每 10 分鐘切一段
            use_mkv=True,           # MKV 較耐斷（要 MP4 可改 False）
            transport="tcp"         # 同網段要更低延遲可改 'udp'
        )
        logging.info("FFmpeg 錄影子程序已啟動（10 分鐘分段）")

    # 計算或覆蓋 ROI
    if roi_box is not None:
        x, y, w, h = roi_box  # 直接使用使用者指定
    else:
        try:
            x, y, w, h = calculate_roi(roi_position, frame_width, frame_height)
        except ValueError as e:
            logging.error(str(e))
            cap.release()
            return

    try:
        # === 你原本的主迴圈（保留） ===
        if show:
            cv2.namedWindow("OCR View", cv2.WINDOW_NORMAL)

        last_proc_ts = 0.0
        val_i = None  # 供疊字顯示

        while True:
            grabbed = cap.grab()
            if not grabbed:
                time.sleep(0.03)
                cap.release()
                cap = open_capture(source)
                if cap is None:
                    break
                continue

            now = time.time()
            if (now - last_proc_ts) < max(0.0, process_every_sec):
                if show and (cv2.waitKey(1) & 0xFF == ord('q')):
                    break
                continue

            ok, frame = cap.retrieve()
            if not ok:
                continue
            last_proc_ts = now

            roi = frame[y:y+h, x:x+w]
            bin_img, text = preprocess_for_ocr(roi)
            val = parse_temperature(text)
            if val is not None:
                val = round(val, 1)
                logging.info(f"檢測值: {val}")

                current_time = time.time()
                if (val >= threshold) and (current_time - last_notification_time > COOLDOWN):
                    cv2.imwrite(TEMP_IMAGE_PATH, frame)
                    current_time_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    message = (
                        f"\n 溫度檢測超過閥值\n"
                        f"時間：{current_time_str}\n"
                        f"位置：{location}\n"
                        f"檢測值：{val}\n"
                        f"閥值：{threshold}\n"
                    )
                    send_discord_notification(NOTIFY_TOKEN, message, img_path=TEMP_IMAGE_PATH)
                    last_notification_time = current_time
            else:
                logging.warning("無法辨識有效數字")

            if show:
                vis = frame.copy()
                cv2.rectangle(vis, (x, y), (x+w, y+h), (0, 255, 0), 2)
                txt = f"T={val}" if val is not None else "T=--"
                cv2.putText(vis, txt, (x+5, y+h+25), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (50,220,50), 2, cv2.LINE_AA)
                ph, pw = min(120, bin_img.shape[0]), min(240, bin_img.shape[1])
                thumb = cv2.resize(bin_img, (pw, ph))
                if len(thumb.shape) == 2:
                    thumb = cv2.cvtColor(thumb, cv2.COLOR_GRAY2BGR)
                vis[10:10+ph, vis.shape[1]-10-pw:vis.shape[1]-10] = thumb
                cv2.imshow("OCR View", vis)
                if cv2.waitKey(1) & 0xFF == ord('q'):
                    break

    finally:
        # 停止錄影子程序
        stop_ffmpeg_recorder(rec_proc)
        logging.info("FFmpeg 錄影子程序已關閉" if rec_proc else "未啟用錄影，略過關閉")

        # 清理暫存與視窗
        if os.path.exists(TEMP_IMAGE_PATH):
            try:
                os.remove(TEMP_IMAGE_PATH)
            except Exception:
                pass
        cap.release()
        if show:
            cv2.destroyAllWindows()

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="RTSP/影片 OCR 檢測與 LINE 通知")
    # 同時支援 --source 與 --video（向下相容）
    parser.add_argument("--source", "--video", dest="source", required=True,
                        help="影片來源：檔案路徑或 RTSP（rtsp://...）")
    parser.add_argument("--threshold", type=float, required=True, help="設定檢測的閥值（浮點數可）")
    parser.add_argument("--location", required=True, help="設定通知中的位置")
    parser.add_argument("--roi_position",
                        choices=["left_top", "right_top", "left_bottom", "right_bottom"],
                        default="right_top",
                        help="四角 ROI（若有 --roi_box 會覆蓋此設定）")
    parser.add_argument("--roi_box", type=str, default=None,
                        help="自訂 ROI，格式 x,y,w,h（例如 1520,40,100,45），指定後會覆蓋 --roi_position")
    parser.add_argument("--tess_path", type=str, default=None, help="tesseract.exe 的安裝路徑")
    parser.add_argument("--show", action="store_true", help="顯示即時畫面與 OCR 結果")
    parser.add_argument("--process_every_sec", type=float, default=1.0,help="每隔幾秒處理一張影格（預設 1.0；設 0 表示每幀處理）")
    parser.add_argument("--record", action="store_true",help="啟用同程式錄影（FFmpeg 分段，預設關閉）")
    args = parser.parse_args()

    roi_box = None
    if args.roi_box:
        try:
            x, y, w, h = map(int, args.roi_box.split(","))
            roi_box = (x, y, w, h)
        except Exception:
            logging.error("解析 --roi_box 失敗，請用 x,y,w,h 格式（例如 1520,40,100,45）")
            raise SystemExit(2)

    main(args.source, args.threshold, args.location, args.roi_position,
        roi_box=roi_box, tess_path=args.tess_path, show=args.show,
        process_every_sec=args.process_every_sec, record=args.record)
