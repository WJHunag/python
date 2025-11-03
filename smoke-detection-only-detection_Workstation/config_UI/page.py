import tkinter as tk
from tkinter import ttk,messagebox 
from tkinter.scrolledtext import ScrolledText
import json
from .element import Button, Label, Entry, StreamCanvas, CamButton, SettingButton, SettingRadiobutton,SettingCheckbutton
import utility

"""
頁面排版
"""

class SmokeDetectPage(): #煙霧檢測頁
    def __init__(self):
        self.threshold = [1.0, 0.9, 0.8, 0.7, 0.6, 0.5, 0.4 ,0.3 ,0.2 ,0.1]
        self.win_size = [1.0, 0.5]
        self.labelimg = utility.LabelImage()
        self.streamer = utility.CamCapture()
        self.camera_ip = None
        self.cameras_info = utility.get_json_data('cameras', './smoke_camera_config.json')
        self.cam_ip = [camera['ip'] for camera in self.cameras_info]

        # 目前攝影機設定暫存
        self.current_camera_settings = {}

        # Radiobutton 變數
        self.line_notify_status_var = tk.IntVar()
        self.google_upload_status = tk.IntVar()
        self.nvr_recording_status = tk.IntVar()
        self.camera_zoom_status = tk.IntVar()
        self.camera_ptz_status = tk.IntVar()
        self.night_detection_status = tk.IntVar()
        self.stage1_detection_var = tk.IntVar(value=0)
        self.stage2_detection_var = tk.IntVar(value=0)

        self.night_start_hour = None
        self.night_start_minute = None
        self.night_end_hour = None
        self.night_end_minute = None
        
        self.presets_dict = {}
        

    def load_camera_settings(self):
        """依據目前選擇的攝影機 IP，載入該攝影機的 function_switches 到介面中"""
        if not self.camera_ip:
            print("未選擇攝影機 IP，無法載入設定。")
            return

        # 找到對應攝影機的設定
        camera = next((cam for cam in self.cameras_info if cam['ip'] == self.camera_ip), None)
        if not camera:
            print(f"找不到攝影機設定，IP: {self.camera_ip}")
            return
        

        camera_switches = camera.get('function_switches', {})
        self.current_camera_settings = camera_switches.copy()

        # 驗證並載入錄影時間
        self.validate_and_load_time('record_time', 10, camera_switches, self.record_time_entry, 'record_time')
        self.validate_and_load_time('focus_time', 5, camera_switches, self.focus_time_entry, 'focus_time')
        self.validate_and_load_time('patrol_time', 15, camera, self.patrol_time_entry, 'patrol_time')

        # 載入夜間檢測時間
        night_start = camera_switches.get('night_detection_start', "00:00")
        night_end = camera_switches.get('night_detection_end', "00:00")
        start_h, start_m = night_start.split(":")
        end_h, end_m = night_end.split(":")
        self.night_start_hour.set(start_h)
        self.night_start_minute.set(start_m)
        self.night_end_hour.set(end_h)
        self.night_end_minute.set(end_m)

        # 載入攝影機功能開關
        self.line_notify_status_var.set(1 if camera_switches.get('enable_smoke_notify', False) else 2)
        self.google_upload_status.set(1 if camera_switches.get('enable_google_upload', False) else 2)
        self.nvr_recording_status.set(1 if camera_switches.get('enable_nvr_recording', False) else 2)
        self.camera_zoom_status.set(1 if camera_switches.get('adjust_camera_zoom_on_detection', False) else 2)
        self.camera_ptz_status.set(1 if camera_switches.get('enable_camera_ptz', False) else 2)
        self.night_detection_status.set(1 if camera_switches.get('enable_night_detection', False) else 2)

        # 載入信心值
        self.load_confidence_settings()

        # 新增：載入一階與二階檢測狀態
        self.stage1_detection_var.set(1 if camera_switches.get('stage1_detection', False) else 0)
        self.stage2_detection_var.set(1 if camera_switches.get('stage2_detection', False) else 0)

        # 確保邏輯一致性
        if not self.stage1_detection_var.get():
            self.stage2_detection_var.set(0)

    def validate_and_load_time(self, name, default_value, source, entry_widget, setting_key):
        """
        通用的時間驗證與載入函數
        :param name: 參數名稱 (如 '錄影時間')
        :param default_value: 默認值
        :param source: 數據源字典
        :param entry_widget: 對應的輸入框
        :param setting_key: 要更新的內部設定鍵
        """
        value = source.get(name, default_value)
        try:
            value = int(value)
            if value <= 0:
                raise ValueError(f"{name} 必須為正數")
        except (ValueError, TypeError):
            print(f"{name} 無效，使用默認值 {default_value} 分鐘 (原始值: {value})")
            value = default_value

        self.current_camera_settings[setting_key] = value
        entry_widget.delete(0, tk.END)
        entry_widget.insert(0, str(value))

    def load_confidence_settings(self):
        """從 JSON 文件載入信心值"""
        with open('./smoke_camera_config.json', 'r', encoding='utf-8') as f:
            config = json.load(f)

        current_camera = next((cam for cam in config['cameras'] if cam['ip'] == self.camera_ip), None)
        if not current_camera:
            print(f"找不到當前攝影機，IP: {self.camera_ip}")
            return

        stage1_confidence = current_camera.get('stage1_confidence', 0.6)  # 默認值
        stage2_confidence = current_camera.get('stage2_confidence', 0.6)  # 默認值

        # 暫時禁用 validatecommand
        self.confidence_stage1_entry.config(validate="none")
        self.confidence_stage2_entry.config(validate="none")

        # 更新輸入框的值
        self.confidence_stage1_entry.delete(0, tk.END)
        self.confidence_stage1_entry.insert(0, f"{int(stage1_confidence * 100)}")
        self.confidence_stage2_entry.delete(0, tk.END)
        self.confidence_stage2_entry.insert(0, f"{int(stage2_confidence * 100)}")

        # 恢復 validatecommand
        self.confidence_stage1_entry.config(validate="key")
        self.confidence_stage2_entry.config(validate="key")

        print(f"加載信心值: stage1_confidence={stage1_confidence}, stage2_confidence={stage2_confidence}")

    def update_status(self, setting_key, status_var, description):
        current_status = (status_var.get() == 1)
        self.current_camera_settings[setting_key] = current_status
        print(f"當前{description}狀態暫存:", "啟用" if current_status else "關閉")
    
    def save_changes(self):
        """保存所有變更到 JSON 文件並更新內部資料"""
        if not self.camera_ip:
            print("尚未選擇攝影機，無法儲存設定。")
            return

        try:
            #確保狀態同步到內部緩存
            self._save_detection_stages()  # 同步 stage1 與 stage2 檢測
            self._save_night_detection_time()
            self._save_confidence_values()

            #保存其他設定（巡弋點、時間等）
            patrol_points = None
            if self.camera_ptz_status.get() == 1:  # PTZ 啟用時
                patrol_points = [self.presets_listbox.get(idx) for idx in range(self.presets_listbox.size())]
                print(f"已保存巡弋點: {patrol_points}")

            patrol_time = int(self.patrol_time_entry.get())
            if patrol_time <= 0:
                raise ValueError("巡弋時間必須為正整數。")
            print(f"已保存巡弋時間: {patrol_time} 分鐘")

            record_time = self._save_record_time()
            focus_time = self._save_focus_time()

            settings = self.current_camera_settings.copy()
            settings.pop('stage1_confidence', None)
            settings.pop('stage2_confidence', None)

            utility.save_camera_settings(
                self.camera_ip,
                settings,
                './smoke_camera_config.json',
                patrol_points=patrol_points,
                patrol_time=patrol_time
            )

            # 4. 同步本地攝影機設定
            self._update_local_camera_settings()

            print(f"所有變更已成功儲存")
            messagebox.showinfo("提示", "設定已儲存成功。\n請重啟 multi_source_vision_hub_center 程式以啟用更改。")

        except ValueError as e:
            print(f"儲存時發生錯誤（無效數值）: {e}")
            messagebox.showerror("錯誤", f"儲存失敗：{e}")
        except Exception as e:
            print(f"儲存時發生其他錯誤: {e}")
            messagebox.showerror("錯誤", f"發生未知錯誤：{e}")

    def _save_confidence_values(self):
        """保存每個攝影機的信心值到 JSON 文件"""
        try:
            # 從輸入框獲取信心值
            stage1_confidence = float(self.confidence_stage1_entry.get()) / 100.0
            stage2_confidence = float(self.confidence_stage2_entry.get()) / 100.0

            # 驗證範圍
            if not (0 <= stage1_confidence <= 1 and 0 <= stage2_confidence <= 1):
                raise ValueError("信心值必須在 0 到 100 之間。")

            # 讀取 JSON 文件
            with open('./smoke_camera_config.json', 'r', encoding='utf-8') as f:
                config = json.load(f)

            # 找到當前攝影機
            current_camera = next((cam for cam in config['cameras'] if cam['ip'] == self.camera_ip), None)
            if not current_camera:
                raise ValueError(f"找不到當前攝影機，IP: {self.camera_ip}")

            # 更新信心值，只保留根部信心值
            current_camera['stage1_confidence'] = stage1_confidence
            current_camera['stage2_confidence'] = stage2_confidence
            if 'function_switches' in current_camera:
                current_camera['function_switches'].pop('stage1_confidence', None)
                current_camera['function_switches'].pop('stage2_confidence', None)

            # 保存更新
            with open('./smoke_camera_config.json', 'w', encoding='utf-8') as f:
                json.dump(config, f, ensure_ascii=False, indent=4)

            print(f"信心值保存成功: stage1_confidence={stage1_confidence}, stage2_confidence={stage2_confidence}")
        except ValueError as e:
            print(f"信心值保存錯誤: {e}")
        except Exception as e:
            print(f"保存信心值時發生錯誤: {e}")

    def _save_record_time(self):
        """保存錄影時間"""
        try:
            record_time = int(self.record_time_entry.get())
            if record_time <= 0:
                raise ValueError("錄影時間必須為正數")
            self.current_camera_settings['record_time'] = record_time
            print(f"錄影時間已儲存：{record_time} 分鐘")
            return record_time
        except ValueError:
            raise ValueError("輸入的錄影時間無效，請輸入正整數值。")
        
    def _update_stage2_detection(self):
        """更新 JSON 中的 stage2_detection 和 stage2_cams"""
        try:
            # 讀取 JSON 配置檔
            config_path = './smoke_camera_config.json'
            with open(config_path, 'r', encoding='utf-8') as f:
                config = json.load(f)

            # 找到當前攝影機
            current_camera = next((cam for cam in config['cameras'] if cam['ip'] == self.camera_ip), None)
            if not current_camera:
                print(f"找不到當前攝影機，IP: {self.camera_ip}")
                return

            # 更新 stage2_detection 狀態
            stage2_enabled = self.stage2_detection_var.get() == 1
            current_camera['function_switches']['stage2_detection'] = stage2_enabled

            # 更新 stage2_cams
            nvr_channel = current_camera.get("NVR_channel")
            if nvr_channel is not None:
                nvr_channel = int(nvr_channel)
                if stage2_enabled:
                    if nvr_channel not in config.get("stage2_cams", []):
                        config["stage2_cams"].append(nvr_channel)
                        print(f"NVR_channel {nvr_channel} 已添加到 stage2_cams。")
                else:
                    if nvr_channel in config.get("stage2_cams", []):
                        config["stage2_cams"].remove(nvr_channel)
                        print(f"NVR_channel {nvr_channel} 已從 stage2_cams 移除。")

            # 清理無效的 NVR_channel
            valid_channels = [
                int(cam["NVR_channel"]) for cam in config['cameras']
                if cam.get("function_switches", {}).get("stage2_detection", False)
            ]
            config["stage2_cams"] = [ch for ch in config.get("stage2_cams", []) if ch in valid_channels]

            # 保存更新的配置檔
            with open(config_path, 'w', encoding='utf-8') as f:
                json.dump(config, f, ensure_ascii=False, indent=4)

        except Exception as e:
            print(f"更新 stage2_detection 時發生錯誤: {e}")

    def _save_night_detection_time(self):
        """保存夜間檢測時間"""
        start_h = self.night_start_hour.get()
        start_m = self.night_start_minute.get()
        end_h = self.night_end_hour.get()
        end_m = self.night_end_minute.get()

        self.current_camera_settings['night_detection_start'] = f"{start_h}:{start_m}"
        self.current_camera_settings['night_detection_end'] = f"{end_h}:{end_m}"
        print(f"夜間檢測時間已儲存：{start_h}:{start_m} 至 {end_h}:{end_m}")

    def enable_camera_ptz(self):
        """啟用 PTZ，並更新界面狀態"""
        self.camera_ptz_status.set(1)  # 啟用 PTZ
        print("已啟用 PTZ 功能，可編輯巡弋點。")

    def _save_detection_stages(self):
        """保存一階與二階檢測狀態"""
        stage1_enabled = bool(self.stage1_detection_var.get())
        stage2_enabled = bool(self.stage2_detection_var.get())

        if not stage1_enabled and stage2_enabled:
            raise ValueError("無法啟用二階檢測而未啟用一階檢測。")

        self.current_camera_settings['stage1_detection'] = stage1_enabled
        self.current_camera_settings['stage2_detection'] = stage2_enabled
        print(f"一階檢測={stage1_enabled}, 二階檢測={stage2_enabled}")

    def _save_focus_time(self):
        """保存追焦時間"""
        try:
            focus_time = int(self.focus_time_entry.get())
            if focus_time <= 0:
                raise ValueError("追焦時間必須為正數")
            self.current_camera_settings['focus_time'] = focus_time
            print(f"追焦時間已儲存：{focus_time} 分鐘")
            return focus_time
        except ValueError:
            raise ValueError("輸入的追焦時間無效，請輸入正整數值。")

    def _update_local_camera_settings(self):
        """更新本地的攝影機設定資料"""
        for camera in self.cameras_info:
            if camera['ip'] == self.camera_ip:
                camera['function_switches'] = self.current_camera_settings
                break
        print("本地攝影機設定已更新。")

    def create_radiobutton(self, tab, text, variable, value, status_type, status_label, x, y):
        radio_button = SettingRadiobutton(
            root=tab,
            text=text,
            variable=variable,
            value=value,
            command=lambda: self.update_status(status_type, variable, status_label)
        )
        radio_button.place(x=x, y=y)
        return radio_button  # 返回創建的按鈕實例
    
    def create_checkbutton(self, tab, text, variable, status_type, x, y):
        check_button = SettingCheckbutton(
            root=tab,
            text=text,
            variable=variable,
            command=lambda: self.handle_checkbutton_logic(variable, status_type)
        )
        check_button.place(x=x, y=y)
        return check_button
    
    def handle_checkbutton_logic(self, variable, status_type):
        """處理一階檢測與二階檢測之間的依賴邏輯並即時更新 JSON"""
        try:
            if status_type == "stage1_detection":
                if variable.get() == 0:  # 如果一階檢測被關閉
                    self.stage2_detection_var.set(0)  # 強制關閉二階檢測
                    print("一階檢測已關閉")
                else:
                    print("一階檢測已啟用")

            elif status_type == "stage2_detection":
                if self.stage1_detection_var.get() == 0:  # 如果一階檢測未啟用
                    variable.set(0)  # 強制關閉二階檢測
                    print("請先啟用一階檢測")
                else:
                    print("二階檢測已啟用" if variable.get() == 1 else "二階檢測已關閉")

                # 即時更新 stage2_detection 和 stage2_cams
                self._update_stage2_detection()

        except Exception as e:
            print(f"處理檢測邏輯時發生錯誤: {e}")

    def update_cam_ip_combobox(self):
        """更新 cam_ip_cbb 下拉選單，預設顯示為空"""
        self.ip_location_map = {
            camera['ip']: camera.get('location', '未知')
            for camera in self.cameras_info
            if camera['ip'] != '127.0.0.1'  # 過濾掉 127.0.0.1
        }

        # 將選單內容加入 "請選擇攝影機" 提示
        values = ["請選擇攝影機"] + [f"{ip} - {location}" for ip, location in self.ip_location_map.items()]
        self.cam_ip_cbb['values'] = values

        # 預設選中第一個選項 (空值)
        self.cam_ip_cbb.current(0)

    def update_location(self, event):
        """處理下拉選單選擇事件"""
        selected = self.cam_ip_cbb.get()  # 取得選單顯示值
        if selected == "請選擇攝影機":
            # 清空設定
            self.camera_ip = None
            self.cam_ip_info_lbl.config(text="位置: 未選擇")
            self.preset_info_lbl.config(text="請選擇攝影機 IP")
            return

        # 提取 IP 部分
        ip = selected.split(" - ")[0]
        self.camera_ip = ip

        # 更新顯示
        location = self.ip_location_map.get(ip, '未知')
        self.cam_ip_info_lbl.config(text=f"位置: {location}")
        self.preset_info_lbl.config(text="")

        # 載入該攝影機的設定
        self.load_camera_settings()


    def update_preset_info(self):
        if self.camera_ip:
            self.presets_dict = utility.get_presets_info(self.camera_ip, 1)
            self.presets_listbox.delete(0, tk.END)
            if self.presets_dict and all(v is not None for v in self.presets_dict.values()):
                for index, name in self.presets_dict.items():
                    preset_name = "攝影機原點" if name == "預設點5" else name
                    self.presets_listbox.insert(tk.END, preset_name)
            else:
                self.preset_info_lbl.config(text="無可用預置點")
        else:
            self.presets_listbox.delete(0, tk.END)
            self.preset_info_lbl.config(text="請選擇攝影機 IP")

    def go_to_selected_preset(self):
        selection = self.presets_listbox.curselection()
        if selection:
            selected_index = selection[0]
            selected_preset_name = self.presets_listbox.get(selected_index)
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
        selection = self.presets_listbox.curselection()
        if selection:
            selected_index = selection[0]
            selected_preset_name = self.presets_listbox.get(selected_index)
            for index, name in self.presets_dict.items():
                if name == selected_preset_name or (selected_preset_name == "攝影機原點" and name == "預設點5"):
                    print(f"選擇刪除的預置點: {name}")
                    print(f"對應的預置點索引: {index}")
                    response = utility.clear_preset(self.camera_ip, 1, index)
                    print(f"刪除結果: {response}")
                    self.update_preset_info()
                    break
        else:
            print("未選擇任何預置點。")

    def start_streaming_and_clear_presets(self):
        """清除預置點並開始串流"""
        self.presets_listbox.delete(0, tk.END)
        self.preset_info_lbl.config(text="")

        # 從 cam_ip_cbb 中取得選擇的 IP
        selected = self.cam_ip_cbb.get()  # 取得選單顯示值，例如 "111.70.5.77 - 龍井區A"
        ip = selected.split(" - ")[0]  # 提取 IP，例如 "111.70.5.77"

        # 傳遞純 IP 給 utility.start_canvas
        utility.start_canvas(ip, self.video_canvas, self.streamer)

        self.update_preset_info()


    def enable_preset_creation(self):
        self.preset_name_entry.config(state=tk.NORMAL)
        self.confirm_btn.config(state=tk.NORMAL)
        self.preset_name_label.place(x=530, y=360)
        self.preset_name_entry.place(x=530, y=390)
        self.confirm_btn.place(x=700, y=390)
        self.preset_name_entry.delete(0, tk.END)
        self.preset_name_entry.focus()
        
    def confirm_preset(self):
        preset_name = self.preset_name_entry.get().strip()
        if not preset_name:
            print("錯誤：請輸入預置點名稱。")
            return

        # 尋找一個未使用的預置點編號
        for i in range(1, 301):
            if i == 5:  
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


    def start_hub_center(self):
        # 使用utility的函式啟動多支攝影機檢測並結束本程式
        utility.start_hub_center_process()

    def build(self, tab):
        btn_bg=tk.Canvas(tab,bg='#FEEAD2',width=250,height=655)
        setting_bg=tk.Canvas(tab,bg='#FEEAD2',width=500 ,height=655)
        cam_move_bg=tk.Canvas(tab,bg='#D2E9FE',width=462 ,height=328)
        preset_frame = tk.Frame(tab, bg='#FEEAD2', width=465, height=100)
        preset_frame.pack_propagate(False)

        self.video_canvas=StreamCanvas(tab, 462, 327, self.streamer)

        cam_ip_lbl = Label(tab, text="IP:")
        self.cam_ip_info_lbl = Label(tab, text="請選擇攝影機開始設定")

        self.preset_info_lbl = tk.Label(tab, text="預置點資料", bg='#FEEAD2')
        self.preset_name_label = Label(tab, text="請輸入預置點名稱：(僅限英數)")
        self.preset_name_entry = tk.Entry(tab, width=10, font=('微軟正黑體', 18))
        self.preset_name_entry.config(state=tk.DISABLED)
        self.presets_listbox = tk.Listbox(tab, height=10, width=51, font=('微軟正黑體', 12))

        self.cam_ip_cbb = ttk.Combobox(tab, values=[""], state="readonly", width=22)
        self.cam_ip_cbb.bind('<<ComboboxSelected>>', self.update_location)
        self.update_cam_ip_combobox()  # 更新選單內容
        self.cam_ip_cbb.current(0)

        stream_btn = Button(tab, '開始串流', command=self.start_streaming_and_clear_presets)
        start_all_camera_btn = Button(tab, '多支攝影機檢測', command=self.start_hub_center)
        save_btn = Button(tab, "儲存更改", command=self.save_changes)

        # PTZ
        ptz_up_btn = CamButton(tab, '↑', lambda: utility.ptz_up(self.camera_ip))
        ptz_left_btn = CamButton(tab, '←', lambda: utility.ptz_left(self.camera_ip))
        ptz_right_btn = CamButton(tab, '→', lambda: utility.ptz_right(self.camera_ip))
        ptz_down_btn = CamButton(tab, '↓', lambda: utility.ptz_down(self.camera_ip))

        # Radiobuttons for current camera settings
        self.create_radiobutton(tab, "啟用Line通知", self.line_notify_status_var, 1, 'enable_smoke_notify', 'Line通知', 780, 40)
        self.create_radiobutton(tab, "關閉Line通知", self.line_notify_status_var, 2, 'enable_smoke_notify', 'Line通知', 1020, 40)
        self.create_radiobutton(tab, "啟用Google雲端上傳", self.google_upload_status, 1, 'enable_google_upload', 'Google Drive上傳', 780, 80)
        self.create_radiobutton(tab, "關閉Google雲端上傳", self.google_upload_status, 2, 'enable_google_upload', 'Google Drive上傳', 1020, 80)
        self.create_radiobutton(tab, "啟用鏡頭巡弋", self.camera_ptz_status, 1, 'enable_camera_ptz', '鏡頭巡弋', 780, 140)
        self.create_radiobutton(tab, "關閉鏡頭巡弋", self.camera_ptz_status, 2, 'enable_camera_ptz', '鏡頭巡弋', 1020, 140)
        self.create_radiobutton(tab, "啟用偵煙追焦", self.camera_zoom_status, 1, 'adjust_camera_zoom_on_detection', '偵煙畫面縮放', 780, 200)
        self.create_radiobutton(tab, "關閉偵煙追焦", self.camera_zoom_status, 2, 'adjust_camera_zoom_on_detection', '偵煙畫面縮放', 1020, 200)
        self.create_radiobutton(tab, "啟用夜間檢測", self.night_detection_status, 1, 'enable_night_detection', '夜間檢測', 780, 260)
        self.create_radiobutton(tab, "關閉夜間檢測", self.night_detection_status, 2, 'enable_night_detection', '夜間檢測', 1020, 260)

        self.create_checkbutton(tab,text="啟用一階檢測",variable=self.stage1_detection_var,status_type="stage1_detection",x=780,y=350)
        self.create_checkbutton(tab,text="啟用二階檢測",variable=self.stage2_detection_var,status_type="stage2_detection",x=1020,y=350)

        # 錄影時間設定標籤與輸入框
        record_time_label = Label(tab, text="錄影時間設定(分鐘)：")
        record_time_label.place(x=805, y=115)  # 調整位置
        vcmd_record_time = tab.register(utility.validate_camera_focus_time)
        self.record_time_entry = tk.Entry(tab, validate="key", validatecommand=(vcmd_record_time, "%P"), width=3)
        self.record_time_entry.place(x=975, y=120)  # 調整位置
        self.record_time_entry.insert(0, "5")  # 預設值為 5 分鐘

        focus_time_label = Label(tab, text="追焦時間設定(分鐘)：")
        focus_time_label.place(x=805, y=235)
        vcmd_focus_time = tab.register(utility.validate_camera_focus_time)
        self.focus_time_entry = tk.Entry(tab, validate="key", validatecommand=(vcmd_focus_time, "%P"), width=3)
        self.focus_time_entry.place(x=975, y=240)
        self.focus_time_entry.insert(0, "5")  # 預設值

        # 巡弋時間設定
        patrol_time_label = Label(tab, text="巡弋時間 (分鐘):")
        patrol_time_label.place(x=815, y=175)

        vcmd_patrol_time = tab.register(utility.validate_camera_focus_time)
        self.patrol_time_entry = tk.Entry(tab, validate="key", validatecommand=(vcmd_patrol_time, "%P"), width=3)
        self.patrol_time_entry.place(x=975, y=180)
        self.patrol_time_entry.insert(0, "15")  # 預設 15 分鐘

        # 夜間檢測時間設定標籤
        night_time_label = Label(tab, text="夜間檢測時間設定：")
        night_time_label.place(x=805, y=295)

        # 開始時間
        start_time_label = Label(tab, text="開始時間：")
        start_time_label.place(x=805, y=320)

        self.night_start_hour = ttk.Combobox(tab, state="readonly", width=3, values=[f"{i:02}" for i in range(24)])
        self.night_start_hour.place(x=895, y=323)
        self.night_start_hour.current(0)  # 預設選擇第一個值

        self.night_start_minute = ttk.Combobox(tab, state="readonly", width=3, values=[f"{i:02}" for i in range(60)])
        self.night_start_minute.place(x=940, y=323)
        self.night_start_minute.current(0)  # 預設選擇第一個值

        # 結束時間
        end_time_label = Label(tab, text="結束時間：")
        end_time_label.place(x=1045, y=320)

        self.night_end_hour = ttk.Combobox(tab, state="readonly", width=3, values=[f"{i:02}" for i in range(24)])
        self.night_end_hour.place(x=1135, y=323)
        self.night_end_hour.current(0)  # 預設選擇第一個值

        self.night_end_minute = ttk.Combobox(tab, state="readonly", width=3, values=[f"{i:02}" for i in range(60)])
        self.night_end_minute.place(x=1180, y=323)
        self.night_end_minute.current(0)  # 預設選擇第一個值


        go_to_preset_btn = SettingButton(tab, '前往預置點', command=self.go_to_selected_preset)
        clear_preset_btn = SettingButton(tab, '刪除預置點', command=self.clear_selected_preset)
        add_preset_btn = Button(tab, '建立預置點', command=self.enable_preset_creation)

        self.confirm_btn = tk.Button(tab, text='確認', width=4, bg='#C24C62', fg='#FFFFFF', activebackground='#191970', relief='groove', font=('微軟正黑體', 12, 'bold'), command=self.confirm_preset)
        self.confirm_btn.config(state=tk.DISABLED)

        # 添加一階與二階檢測信心值輸入框
        confidence_label_stage1 = Label(tab, text="一階檢測信心值 (0-100)：")
        confidence_label_stage1.place(x=40, y=245)

        vcmd_confidence_stage1 = tab.register(lambda v: utility.validate_confidence_input(v))
        self.confidence_stage1_entry = tk.Entry(tab, validate="key", validatecommand=(vcmd_confidence_stage1, "%P"), width=5)
        self.confidence_stage1_entry.place(x=230, y=250)
        

        confidence_label_stage2 = Label(tab, text="二階檢測信心值 (0-100)：")
        confidence_label_stage2.place(x=40, y=275)

        vcmd_confidence_stage2 = tab.register(lambda v: utility.validate_confidence_input(v))
        self.confidence_stage2_entry = tk.Entry(tab, validate="key", validatecommand=(vcmd_confidence_stage2, "%P"), width=5)
        self.confidence_stage2_entry.place(x=230, y=280)
        



        # place
        btn_bg.place(x=20,y=20)
        setting_bg.place(x=760,y=20)
        cam_move_bg.place(x=290,y=347)
        self.video_canvas.place(x=290,y=20)
        preset_frame.place(x=290, y=347)

        cam_ip_lbl.place(x=40,y=38)
        self.cam_ip_info_lbl.place(x=40,y=68)
        self.preset_info_lbl.place(x=780, y=160)  
        self.cam_ip_cbb.place(x=80, y=40)  
        self.presets_listbox.place(x=780, y=400)

        start_all_camera_btn.place(x=40,y=180)
        stream_btn.place(x=40,y=100)
        save_btn.place(x=40, y=600)

        ptz_up_btn.place(x=475,y=480)
        ptz_down_btn.place(x=475,y=580)
        ptz_left_btn.place(x=375,y=530)
        ptz_right_btn.place(x=575,y=530)

        go_to_preset_btn.place(x=820, y=620)
        clear_preset_btn.place(x=1060,y=620)
        add_preset_btn.place(x=300,y=367) 

        self.preset_name_label.place(x=530, y=360)  
        self.preset_name_entry.place(x=530, y=390)
        self.confirm_btn.place(x=700, y=390)
        
