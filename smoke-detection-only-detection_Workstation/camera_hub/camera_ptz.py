import requests
import time
from requests.auth import HTTPDigestAuth
import threading
import json
from utility import get_json_data

class PTZControl:
    def __init__(self, ip, usr, pas):
        self.ip = ip
        self.usr = usr
        self.pas = pas
        # 從 JSON 加載巡弋點和巡弋時間
        config = get_json_data("cameras", "./smoke_camera_config.json")
        for camera in config:
            if camera["ip"] == ip:
                self.patrol_points = camera.get("patrol_points", [])
                self.patrol_time = camera.get("patrol_time", 15)  # 預設巡弋時間為 15 分鐘
                break
        else:
            self.patrol_points = []
            self.patrol_time = 15  # 預設值

    def control_ptz(self, action, channel, code, arg1, arg2, arg3):
        url = f"http://{self.ip}/cgi-bin/ptz.cgi?action={action}&channel={channel}&code={code}&arg1={arg1}&arg2={arg2}&arg3={arg3}"
        print(url)
        try:
            response = requests.get(url, auth=HTTPDigestAuth(self.usr, self.pas))
            response.raise_for_status()
        except requests.exceptions.RequestException as e:
            print(f"PTZ ERROR at {time.strftime('%Y-%m-%d - %H:%M')}: {e}")

    def move(self):
        """巡弋邏輯，根據配置中的停留時間巡弋到下一個預置點"""
        stop_time = self.patrol_time * 60  # 將分鐘轉換為秒
        while True:
            try:
                for index, preset_name in enumerate(self.patrol_points):
                    print(f"巡弋到預置點: {preset_name} (索引 {index})")
                    # 移動到預置點
                    self.control_ptz("start", 1, "GotoPreset", 0, index + 1, 0)
                    print(f"停留 {self.patrol_time} 分鐘在預置點: {preset_name}")
                    # 停留指定時間
                    time.sleep(stop_time)
            except Exception as e:
                print(f"PTZ ERROR: {e}")

def load_camera_config(config_path):
    with open(config_path, 'r', encoding='utf-8') as f:
        return json.load(f)

def main():
    config = load_camera_config('./smoke_camera_config.json')
    cameras = config.get("cameras", [])
    threads = []

    for camera in cameras:
        function_switches = camera.get("function_switches", {})
        enable_ptz = function_switches.get("enable_camera_ptz", False)

        if enable_ptz:
            ip = camera.get("ip")
            username = camera.get("username", "admin")
            password = camera.get("password", "hs888888")

            print(f"啟用巡弋功能: 攝影機 {ip} ({camera.get('location')})")

            ptz_controller = PTZControl(ip, username, password)
            thread = threading.Thread(target=ptz_controller.move)
            threads.append(thread)
        else:
            print(f"攝影機 {camera.get('ip')} ({camera.get('location')})巡弋功能已關閉")

    for thread in threads:
        thread.start()

if __name__ == "__main__":
    main()
