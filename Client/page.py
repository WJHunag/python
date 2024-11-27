import tkinter as tk
from tkinter import ttk
from tkinter.scrolledtext import ScrolledText

from element import Button, Label, Entry, StreamCanvas, CamButton, SettingButton, SettingRadiobutton
import subprocess
import sys
import utility

"""
頁面排版
"""

class SmokeDetectPage(): #煙霧檢測頁
    def __init__(self):
        settings = utility.load_settings().get('function_switches', {})
        self.threshold = [1.0, 0.9, 0.8, 0.7, 0.6, 0.5, 0.4 ,0.3 ,0.2 ,0.1]
        self.win_size = [1.0, 0.5]
        self.labelimg = utility.LabelImage()
        self.streamer = utility.CamCapture()
        self.camera_ip = None
        self.cam_ip = [camera['ip'] for camera in utility.get_json_data('cameras', './smoke_camera_config.json')]
        self.cameras_info = utility.get_json_data('cameras', './smoke_camera_config.json')
        self.upload_enabled = tk.BooleanVar(value=True)  
        self.presets_dict = {}
        self.line_notify_status_var = tk.IntVar(value=1 if settings.get('enable_smoke_notify', False) else 2)
        self.google_upload_status = tk.IntVar(value=1 if settings.get('enable_google_upload', False) else 2)
        self.nvr_recording_status = tk.IntVar(value=1 if settings.get('enable_nvr_recording', False) else 2)
        self.camera_zoom_status = tk.IntVar(value=1 if settings.get('adjust_camera_zoom_on_detection', False) else 2)

    def update_status(self, setting_key, status_var, description):
        current_status = status_var.get() == 1
        utility.save_settings(setting_key, current_status)
        print(f"當前{description}狀態:", "啟用" if current_status else "關閉")

    def create_radiobutton(self, tab, text, variable, value, status_type, status_label, x, y):
        SettingRadiobutton(
            root=tab,
            text=text,
            variable=variable,
            value=value,
            command=lambda: self.update_status(status_type, variable, status_label)
        ).place(x=x, y=y)

    def update_location(self, event):
        if isinstance(event, dict) and 'widget' in event:
            ip = event['widget'].get()
        else:
            ip = event.widget.get()
        
        # 從 cameras_info 中查找對應的攝影機
        camera = next((camera for camera in self.cameras_info if camera['ip'] == ip), None)
        location = camera['location'] if camera else '未知'
        
        self.cam_ip_info_lbl.config(text=f"位置: {location}")
        self.camera_ip = ip
        self.preset_info_lbl.config(text="")

    def update_preset_info(self):
         if self.camera_ip: # 確保有有效的 IP 位址
             self.presets_dict = utility.get_presets_info(self.camera_ip, 1) # 取得預置點字典
             # 清除 Listbox 內容
             self.presets_listbox.delete(0, tk.END)
             # 將預置點索引轉換為更易讀的名稱，並更新至 Listbox
             for index, name in self.presets_dict.items():
                 preset_name = "攝影機原點" if name == "預設點5" else name
                 self.presets_listbox.insert(tk.END, preset_name)
             if not self.presets_dict: # 檢查是否有可用預置點
                 self.preset_info_lbl.config(text="無可用預置點")
         else:
             self.presets_listbox.delete(0, tk.END) # 清空 Listbox
             self.preset_info_lbl.config(text="請選擇攝影機 IP")


    def go_to_selected_preset(self):
         selection = self.presets_listbox.curselection() # 取得 Listbox 中的目前選擇
         if selection: # 確保有選擇
             selected_index = selection[0] # 取得選取的索引
             selected_preset_name = self.presets_listbox.get(selected_index) # 取得預置點名稱
             # 從預置點名稱找到對應的索引
             for index, name in self.presets_dict.items():
                 if name == selected_preset_name or (selected_preset_name == "攝影機原點" and name == "預設點5"):
                     print(f"選擇的預置點: {name}")
                     print(f"對應的預置點索引: {index}")
                     response = utility.go_to_preset(self.camera_ip, 1, index)
                     print(f"跳到結果: {response}")
                     break
         else:
             print("未選擇任何預置點。")

    def clear_selected_preset(self):
         selection = self.presets_listbox.curselection() # 取得 Listbox 中的目前選擇
         if selection: # 確保有選擇
             selected_index = selection[0] # 取得選取的索引
             selected_preset_name = self.presets_listbox.get(selected_index) # 取得預置點名稱
             # 從預置點名稱找到對應的索引
             for index, name in self.presets_dict.items():
                 if name == selected_preset_name or (selected_preset_name == "攝影機原點" and name == "預設點5"):
                     print(f"選擇刪除的預置點: {name}")
                     print(f"對應的預置點索引: {index}")
                     response = utility.clear_preset(self.camera_ip, 1, index)
                     print(f"刪除結果: {response}")
                     # 刪除後更新預置點訊息
                     self.update_preset_info()
                     break
         else:
             print("未選擇任何預置點。")

    def start_streaming_and_clear_presets(self):
        # 清空 Listbox
        self.presets_listbox.delete(0, tk.END)
        # 清空預置點信息顯示標籤
        self.preset_info_lbl.config(text="")
        # 調用實際開始串流的函數，這裡調用已經修改的 start_canvas
        utility.start_canvas(self.cam_ip_cbb, self.video_canvas, self.streamer)
        # 自動更新預置點信息
        self.update_preset_info()  # 在開始串流後自動調用



    def enable_preset_creation(self):
        # 启用输入框和按钮
        self.preset_name_entry.config(state=tk.NORMAL)
        self.confirm_btn.config(state=tk.NORMAL)
        # 显示输入框和标签
        self.preset_name_label.place(x=530, y=360)
        self.preset_name_entry.place(x=530, y=390)
        self.confirm_btn.place(x=700, y=390)
        # 清空输入框以准备新输入
        self.preset_name_entry.delete(0, tk.END)
        # 给输入框焦点
        self.preset_name_entry.focus()
        


    def confirm_preset(self):
        preset_name = self.preset_name_entry.get().strip()
        if not preset_name:
            print("錯誤：請輸入預置點名稱。")
            return

        # 查找一个未使用的预置点编号
        for i in range(1, 301):
            if i == 5:  # 跳过编号5
                continue
            if str(i) not in self.presets_dict:
                free_index = str(i)
                break
        else:
            print("錯誤：沒有可用的預置點編號。")
            return

        
        response = utility.set_preset(self.camera_ip, 1, free_index, preset_name)
        if response.strip() == 'OK':
            print(f"成功設定預置點: {preset_name}")
            self.update_preset_info()
            self.preset_name_entry.delete(0, tk.END)
            self.preset_name_entry.config(state=tk.DISABLED)
            self.confirm_btn.config(state=tk.DISABLED)
            
        else:
            print(f"設定預置點失敗: {response}")
            
    def start_all_camera(self):
        # 啟動另一個 Python 程式
        subprocess.Popen(["python", "./all_camera.py"])
        # 關閉當前的 Python 程式
        sys.exit()




    def build(self, tab):
        #create
        btn_bg=tk.Canvas(tab,bg='#FEEAD2',width=250,height=655)
        setting_bg=tk.Canvas(tab,bg='#FEEAD2',width=500 ,height=655)
        cam_move_bg=tk.Canvas(tab,bg='#D2E9FE',width=462 ,height=328)

        preset_frame = tk.Frame(tab, bg='#FEEAD2', width=465, height=100)
        preset_frame.pack_propagate(False)

        self.video_canvas=StreamCanvas(tab, 462, 327, self.streamer)

        #window_size_lbl = Label(tab, text = "頁面大小:")
        #window_size_cbb = ttk.Combobox(tab, values=self.win_size, state="readonly", width=14)             
        #window_size_cbb.current(0)


        cam_ip_lbl = Label(tab, text = "IP:")
        self.cam_ip_info_lbl = Label(tab, text="位置:")

        self.preset_info_lbl = tk.Label(tab, text="預置點資料", bg='#FEEAD2')

        self.preset_name_label = Label(tab, text = "請輸入預置點名稱：(僅限英數)")

        self.preset_name_entry = tk.Entry(tab, width=10, font=('微軟正黑體', 20))
        self.preset_name_entry.config(state=tk.DISABLED)
        #self.preset_name_entry.place_forget()

        #self.presets_cbb = ttk.Combobox(tab, state="readonly", width=14)
        self.presets_listbox = tk.Listbox(tab, height=10, width=51, font=('微軟正黑體', 12))

        self.cam_ip_cbb = ttk.Combobox(tab, values=[camera['ip'] for camera in self.cameras_info], state="readonly", width=14)
        self.cam_ip_cbb.bind('<<ComboboxSelected>>', self.update_location)  # 綁定事件
        self.cam_ip_cbb.current(0)  # 自動選擇第一個 IP，觸發更新位置和 camera_ip
        self.update_location({'widget': self.cam_ip_cbb})  # 初始更新，確保 camera_ip 有值




        stream_btn = Button(tab, '開始串流', command=self.start_streaming_and_clear_presets)

        start_all_camera_btn = Button(tab, '多支攝影機檢測',command=self.start_all_camera)
        #PTZ
        ptz_up_btn = CamButton(tab, '↑', lambda: utility.ptz_up(self.camera_ip))
        ptz_left_btn = CamButton(tab, '←', lambda: utility.ptz_left(self.camera_ip))
        ptz_right_btn = CamButton(tab, '→', lambda: utility.ptz_right(self.camera_ip))
        ptz_down_btn = CamButton(tab, '↓', lambda: utility.ptz_down(self.camera_ip))

        self.create_radiobutton(tab, "啟用Line通知", self.line_notify_status_var, 1, 'enable_smoke_notify', 'Line通知', 780, 40)
        self.create_radiobutton(tab, "關閉Line通知", self.line_notify_status_var, 2, 'enable_smoke_notify', 'Line通知', 960, 40)
        self.create_radiobutton(tab, "啟用Google雲端上傳", self.google_upload_status, 1, 'enable_google_upload', 'Google Drive上傳', 780, 80)
        self.create_radiobutton(tab, "關閉Google雲端上傳", self.google_upload_status, 2, 'enable_google_upload', 'Google Drive上傳', 1020, 80)
        self.create_radiobutton(tab, "啟用偵煙錄影", self.nvr_recording_status, 1, 'enable_nvr_recording', '偵煙錄影', 780, 120)
        self.create_radiobutton(tab, "關閉偵煙錄影", self.nvr_recording_status, 2, 'enable_nvr_recording', '偵煙錄影', 1020, 120)
        self.create_radiobutton(tab, "啟用偵煙畫面縮放", self.camera_zoom_status, 1, 'adjust_camera_zoom_on_detection', '偵煙畫面縮放', 780, 160)
        self.create_radiobutton(tab, "關閉偵煙畫面縮放", self.camera_zoom_status, 2, 'adjust_camera_zoom_on_detection', '偵煙畫面縮放', 1020, 160)

        #get_preset_btn = SettingButton(tab, text='獲取預置點', command=self.update_preset_info)
        go_to_preset_btn = SettingButton(tab, '前往預置點', command=self.go_to_selected_preset)
        clear_preset_btn = SettingButton(tab, '刪除預置點', command=self.clear_selected_preset)


        add_preset_btn = Button(tab, '建立預置點', command=self.enable_preset_creation)
        self.confirm_btn = tk.Button(tab, text='確認', width=4, bg='#C24C62', fg='#FFFFFF', activebackground='#191970', relief='groove', font=('微軟正黑體', 12, 'bold'), command=self.confirm_preset)
        self.confirm_btn.config(state=tk.DISABLED)
        #self.confirm_btn.place_forget()


        #place    
        btn_bg.place(x=20,y=20)
        setting_bg.place(x=760,y=20)
        cam_move_bg.place(x=290,y=347)
        self.video_canvas.place(x=290,y=20)
        preset_frame.place(x=290, y=347)
        

        #window_size_lbl.place(x=40,y=108)
        #window_size_cbb.place(x=130, y=108)
        #upload_chk.place(x=780, y=160)  
        cam_ip_lbl.place(x=40,y=38)
        self.cam_ip_info_lbl.place(x=40,y=68)

        self.preset_info_lbl.place(x=780, y=160)  

        self.cam_ip_cbb.place(x=80, y=40)  

        self.presets_listbox.place(x=780, y=400)
        #self.presets_cbb.place(x=780, y=80)


        start_all_camera_btn.place(x=40,y=180)
        stream_btn.place(x=40,y=100)

        ptz_up_btn.place(x=475,y=480)
        ptz_down_btn.place(x=475,y=580)
        ptz_left_btn.place(x=375,y=530)
        ptz_right_btn.place(x=575,y=530)

        #get_preset_btn.place(x=780, y=620)
        go_to_preset_btn.place(x=820, y=620)
        clear_preset_btn.place(x=1060,y=620)

        add_preset_btn.place(x=300,y=367) 
        self.preset_name_label.place(x=530, y=360)  # 位置可以根据需要调整
        self.preset_name_entry.place(x=530, y=390)
        self.confirm_btn.place(x=700, y=390)
        #self.preset_entry.place(x=530, y=380)
        #self.preset_index_label.place(x=530, y=360)
        #self.preset_name_label.place(x=530, y=360)