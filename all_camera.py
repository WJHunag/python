import tkinter as tk
from PIL import Image, ImageTk
import threading
import cv2
import time
import json
import numpy as np
from utility import call_model_api, save_img, get_local_info
import argparse

"""
進行6個攝影機同時錄影的程式
"""

class streamCapture():
    """
    用於串流影像，具有斷線重連功能
    """
    #cam_ip由get_camera_ip取得
    def __init__(self, cam_ip):
        self.ip = cam_ip
        #self.rtsp_ip = "rtsp://admin:admin12345@"+cam_ip+":554/live/0/main"
        #self.rtsp_ip = "rtsp://admin:hs888888@"+cam_ip+":554/cam/realmonitor?channel=1&subtype=0"
        self.rtsp_ip = "./"+cam_ip

        # 用來確認攝影機狀態的屬性
        self.online = False
        self.capture = None
        
        self.load_network_stream()
    
        self.get_frame_thread = threading.Thread(target=self.update_frame, args=())
        self.get_frame_thread.daemon = True
        self.get_frame_thread.start()

    def load_network_stream(self):
        """連線攝影機"""
        def load_network_stream_thread():
            while True:
                if self.verify_network_stream(self.rtsp_ip):
                    self.capture = cv2.VideoCapture(self.rtsp_ip, cv2.CAP_FFMPEG)
                    self.online = True
                time.sleep(5)
                if self.online:
                    break
                    
        self.load_stream_thread = threading.Thread(target=load_network_stream_thread, args=())
        self.load_stream_thread.daemon = True
        self.load_stream_thread.start()

    def verify_network_stream(self, link):
        """確認是否可以連結攝影機"""
        cap = cv2.VideoCapture(link)
        if not cap.isOpened():
            return False
        cap.release()
        return True

    def update_frame(self):
        """接收最新影像"""
        while True:
            try:
                if self.capture.isOpened() and self.online:
                    # 取得影像
                    status, frame = self.capture.read()
                    if status:
                        #frame=cv2.imread(照片絕對路徑)
                        frame = cv2.resize(frame, (1024, 576))
                        frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                        self.frame = frame
                    if not status:
                        self.capture.release()
                        self.online = False
                else:
                    # 重新連線
                    if not self.load_stream_thread.is_alive():
                        self.load_network_stream()
            except AttributeError:
                pass
            time.sleep(.03)
    
    def get_frame(self):
        if hasattr(self, "frame"):
            return self.frame
        else:
            return None

class StreamCanvas(tk.Canvas):
    """
    用於顯示串流影像
    """
    def __init__(self, root, canvas_size, streamer, roi, args):
        self.width = canvas_size[0]
        self.height = canvas_size[1]
        super().__init__(root, width=self.width, height=self.height)
        self.fps = 20
        self.streamer = streamer
        self.api = call_model_api

        self.roi = roi[self.streamer.ip]
        self.notify_time = 0
        self.save_time = time.time()
        self.detect_time = time.time()

        self.notify = args.notify
        self.save_period = args.save_time
        self.detect_period = args.detect_period
        self.roi_check = args.roi_check

        self.refresh()

    def refresh(self):
        self.frame = self.streamer.get_frame()
        if type(self.frame) == np.ndarray:
            now = time.time()
            if now-self.save_time >= self.save_period:
                save_img(self.frame, self.streamer.ip)
                self.save_time = time.time()

            if now-self.detect_time >= self.detect_period:
                
                self.notify_time = self.api(self.frame, self.roi, 0.4, self.streamer.ip, self.notify_time, self.notify,self.roi_check)
                
                self.detect_time = time.time()

            self.frame = cv2.resize(self.frame, (self.width, self.height))
            frame_array = ImageTk.PhotoImage(image = Image.fromarray(self.frame))

            self.delete("all")
            self.image = frame_array
            self.create_image(0, 0, image=frame_array, anchor="nw")
            if self.info:
                #畫面大小改變 這邊也要改 x-140
                self.create_text(244, 310, text=self.info[self.streamer.ip], anchor='nw', fill='#000000', font=('microsoft yahei', 14, 'bold'))
                self.create_text(246, 312, text=self.info[self.streamer.ip], anchor='nw', fill='#ffffff', font=('microsoft yahei', 14, 'bold'))

        self.after(int(3000), self.refresh)

    def add_info(self, info):
        self.info = info

def get_camera_ip(path):
    """
    載入ipcam address
    """
    cam_ip = []
    with open(path) as f:
        for line in f.readlines():
            cam_ip.append(line.rstrip('\n'))

    return cam_ip

def load_json(path):
    """
    載入roi座標
    """
    with open(path, 'r') as f:
        json_data = json.load(f)
        
    return json_data

def get_parser():
    """
    設定是否要進行通知
    """
    parser = argparse.ArgumentParser(description='ip')
    parser.add_argument('--notify', default=True, type=bool)
    parser.add_argument('--save_time', default=1800, type=int)
    parser.add_argument('--detect_period', default=7, type=int)
    parser.add_argument('--roi_check', default=False, type=bool)
    return parser

if __name__ == "__main__":
    parser = get_parser()
    args = parser.parse_args()
    window = tk.Tk()
    window.title('煙霧檢測總畫面')
    #攝影機數量 後續修改成讀txt檔的方式
    local_info_path = "./local_info.txt"
    local_info = get_local_info(local_info_path)
    """local_info = {
        "111.70.5.77":"中火、中龍",
        "111.70.19.2":"台中工業區",
        "111.70.19.3":"中科后里基地",
        "111.70.19.4":"大甲幼獅工業",
        "111.70.19.5":"大里工業區",
        "111.70.16.169":"中部科學園區",
        "111.70.19.158":"龍井區二"
    }"""

    canvases = [] # 創建canvas儲存列表
    num_canvases = 1 #設定canvas的數量
    num_canvases_x = 1 #設定x軸的canvas數量
    if num_canvases % num_canvases_x == 0:
        num_canvases_y = num_canvases // num_canvases_x
    else:
        num_canvases_y = (num_canvases // num_canvases_x) + 1
    
    win_conter = 1
    win_size = [1920,1020]

    window.geometry("1920x1020")#設定視窗大小
    window.configure(bg='#AFEEEE')#設定背景顏色
    window.resizable(width=1, height=1)#是否可以改變視窗大小

    #攝影機的ip位置 可以跟上面的info合併
    ip_path = "./camera_ip.txt"
    cam_ip = get_camera_ip(ip_path)

    cam = {}
    #攝影機數量 可以用len方式自動判斷
    for i in range(1):
        cam[i] = streamCapture(cam_ip[i])

    #顯示攝影機的畫面大小
    canvas_size = (640, 340)
    ip_data = load_json("./roi_coord.json")

    canvas_size = (win_size[0] // num_canvases_x, win_size[1] // num_canvases_y)
    #ip_data = 0
    x, y = 0, 0

    for n in range(1, num_canvases + 1):
        if x == num_canvases_x:
            y += 1
            x = 0
        canvas = StreamCanvas(window, canvas_size, cam[n-1], ip_data, args)
        canvas.add_info(local_info)
        canvas.place(x = canvas_size[0] * x, y = canvas_size[1] * y)
        canvases.append(canvas)
        x += 1

    #攝影機畫位置 可能可以改成相對位置 較為方便
    """cam1_canvas = StreamCanvas(window, canvas_size, cam[0], ip_data, args)
    cam1_canvas.add_info(local_info)
    cam1_canvas.place(x=0,y=0)
    cam2_canvas = StreamCanvas(window, canvas_size, cam[1], ip_data, args)
    cam2_canvas.add_info(local_info)
    cam2_canvas.place(x=641,y=0)
    
    cam3_canvas = StreamCanvas(window, canvas_size, cam[2], ip_data, args)
    cam3_canvas.add_info(local_info)
    cam3_canvas.place(x=1281,y=0)
    cam4_canvas = StreamCanvas(window, canvas_size, cam[3], ip_data, args)
    cam4_canvas.add_info(local_info)
    cam4_canvas.place(x=0,y=341)
    cam5_canvas = StreamCanvas(window, canvas_size, cam[4], ip_data, args)
    cam5_canvas.add_info(local_info)
    cam5_canvas.place(x=641,y=341)
    
    cam6_canvas = StreamCanvas(window, canvas_size, cam[5], ip_data, args)
    cam6_canvas.add_info(local_info)
    cam6_canvas.place(x=1281,y=341)
    cam7_canvas = StreamCanvas(window, canvas_size, cam[6], ip_data, args)
    cam7_canvas.add_info(local_info)
    cam7_canvas.place(x=0,y=682)

    cam8_canvas = StreamCanvas(window, canvas_size, cam[7], ip_data, args)
    cam8_canvas.add_info(local_info)
    cam8_canvas.place(x=641,y=682)
    cam9_canvas = StreamCanvas(window, canvas_size, cam[8], ip_data, args)
    cam9_canvas.add_info(local_info)
    cam9_canvas.place(x=1281,y=682)"""

    window.mainloop()
    

