import tkinter as tk
import cv2
from PIL import Image, ImageTk
import numpy as np
import json
import os
import copy

"""
Tkinter物件
"""
class Button(tk.Button):
    """
    自訂義tkinter按鈕
    """
    def __init__(self, root, text, command=None):
        super().__init__(root)
        self.configure(text=text)
        self.configure(width=12)
        self.configure(bg='#C24C62', fg='#FFFFFF', activebackground='#191970')
        self.configure(relief= 'groove', font=('微軟正黑體',20,'bold'))
        self.configure(command=command)

class CamButton(tk.Button):
    """
    自訂義tkinter按鈕
    """
    def __init__(self, root, text, command=None):
        super().__init__(root)
        self.configure(text=text)
        self.configure(width=3)
        self.configure(bg='#C24C62', fg='#FFFFFF', activebackground='#191970')
        self.configure(relief= 'groove', font=('微軟正黑體',24,'bold'))
        self.configure(command=command)

class SettingButton(tk.Button):
    """
    自定义tkinter按钮
    """
    def __init__(self, root, text, command=None):
        super().__init__(root)
        self.configure(text=text)
        estimated_width = len(text) + 4
        self.configure(width=estimated_width)
        self.configure(bg='#C24C62', fg='#FFFFFF', activebackground='#191970')
        self.configure(relief='groove', font=('微軟正黑體', 18, 'bold'))
        self.configure(command=command)

class SettingCheckbutton(tk.Checkbutton):
    """
    自定義tkinter複選按鈕，內建背景顏色、文字顏色和字體設定
    """
    def __init__(self, root, text, variable, command=None):
        super().__init__(root)
        self.configure(text=text, bg='#FEEAD2', fg='#FF4600', font=('微軟正黑體', 16, 'bold'), variable=variable, command=command)
        self.configure(activebackground='#D2E9FE', activeforeground='#FF4600')

class SettingRadiobutton(tk.Radiobutton):
    """
    自定義tkinter單選按鈕，內建背景顏色、文字顏色和字體設定
    """
    def __init__(self, root, text, variable, value, command):
        super().__init__(root)
        self.configure(text=text, bg='#FEEAD2', fg='#FF4600', font=('微軟正黑體', 16, 'bold'), variable=variable, value=value, command=command)


class Label(tk.Label):
    """
    自訂義tkinter標籤
    """
    def __init__(self, root, text):
        super().__init__(root)
        self.configure(text=text)
        self.configure(bg='#FEEAD2', fg='#FF4600')
        self.configure(font=('微軟正黑體',12,'bold'))

class Entry(tk.Entry):
    """
    自訂義tkinter輸入格
    """
    def __init__(self, root, width, tag):
        super().__init__(root)
        self.configure(width=width)
        self.tag = tag

class StreamCanvas(tk.Canvas):
    """
    自訂義tkinter畫布
    用於顯示串流影像
    """
    def __init__(self, root, width, height, streamer):
        super().__init__(root, width=width, height=height)
        self.width = width
        self.height = height
        self.fps = 10
        self.streamer = streamer
        self.api = None
        self.roi_path = os.getcwd() + "/roi_coord.json"
        self.roi = []
        self.notify_time = 0
        
        self.refresh()


    def refresh(self):
        frame = self.streamer.getframe()
        if type(frame) == np.ndarray:
            frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            if self.api:
                self.notify_time = self.api(frame, self.roi, self.threshold_cbb.get(), self.ip, self.notify_time)
            frame = cv2.resize(frame, (self.width, self.height))
            self.roi_image = copy.deepcopy(frame)
            frame_array = ImageTk.PhotoImage(image = Image.fromarray(frame))

            self.delete("all")
            self.image = frame_array
            self.create_image(0, 0, image=frame_array, anchor="nw")
        """
        if self.roi != []:
            for i in range(len(self.roi)):
                x1, y1, x2, y2 = self.roi[i]
                self.create_rectangle(x1, y1, x2, y2, width=0.01,outline="red")
        """    

        self.after(int(1000/self.fps), self.refresh)



    def add_api(self, api):
        self.api = api

    def remove_api(self):
        self.api = None

    def add_threshold_cbb(self, cbb):
        self.threshold_cbb = cbb

    def add_ip(self, ip):
        self.ip = ip










