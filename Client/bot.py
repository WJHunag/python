import configparser
from datetime import datetime
import requests
import utility 


"""
用於LINE notify通知
"""


def smoke_notify(img_path, ip):
    exclude_list = utility.get_json_data('exclude_list')
    cameras_info = utility.get_json_data('cameras')  # 從新JSON中獲取攝影機列表

    config = configparser.ConfigParser()
    config.read("./config.ini")
    notify_token = config['token']['notify']
    river_notify_token = config['token']['notify']

    # 使用新的排除判斷函數
    if utility.is_camera_excluded(ip, exclude_list, cameras_info):
        notify_token = river_notify_token

    now_timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    
    headers = {
        "Authorization": "Bearer " + notify_token
    }

    # 獲取對應IP的 location
    camera = next((camera for camera in cameras_info if camera['ip'] == ip), None)
    location = camera['location'] if camera else '未知地點'

    data = {
        "message": f"檢測到煙霧\n時間：{now_timestamp}\n位置：{location}"
    }
    
    img_file = {"imageFile": open(img_path, "rb")}
    
    r = requests.post("https://notify-api.line.me/api/notify",
                      headers=headers, data=data, files=img_file)
    
    print(f"Response: {r.status_code}, {r.text}")

def smoke_notify_camera(ip):
    cameras_info = utility.get_json_data('cameras')  # 從新JSON中獲取攝影機列表
    config = configparser.ConfigParser()
    config.read("./config.ini")
    notify_token = config['token']['notifyerror']

    headers = {
        "Authorization": "Bearer " + notify_token
    }

    # 獲取對應IP的 notify (通知區域)
    camera = next((camera for camera in cameras_info if camera['ip'] == ip), None)
    notify_info = camera['notify'] if camera else '未知位置'

    data = {
        "message": f"\n位置：{notify_info}\n攝影機已斷線三十分鐘"
    }

    r = requests.post("https://notify-api.line.me/api/notify", headers=headers, data=data)

def smoke_notify_watersmoke(img_path, result, ip, confidence):
    cameras_info = utility.get_json_data('cameras')  # 從新JSON中獲取攝影機列表
    exclude_list = utility.get_json_data('exclude_list')

    # 使用新的排除判斷函數
    if utility.is_camera_excluded(ip, exclude_list, cameras_info):
        return

    config = configparser.ConfigParser()
    config.read("./config.ini")
    notify_token_water = config['token']['notifywatersmoke']
    notify_token_smoke = config['token']['notifysmoke']
    now_timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")

    # 獲取對應IP的 location
    camera = next((camera for camera in cameras_info if camera['ip'] == ip), None)
    location = camera['location'] if camera else '未知地點'

    if result == "0":
        headers = {
            "Authorization": "Bearer " + notify_token_water
        }
        data = {
            "message": f"檢測到水氣\n時間：{now_timestamp}\n位置：{location}\n信心值：{confidence}%"
        }
    elif result == "1":
        headers = {
            "Authorization": "Bearer " + notify_token_smoke
        }
        data = {
            "message": f"檢測到煙霧\n時間：{now_timestamp}\n位置：{location}\n信心值：{confidence}%"
        }

    img_file = {"imageFile": open(img_path, "rb")}
    r = requests.post("https://notify-api.line.me/api/notify", headers=headers, data=data, files=img_file)

    if r.status_code != 200:
        print(f"Error: {r.status_code}, {r.text}")
    else:
        print("LINE通知成功")

if __name__ == "__main__":
    smoke_notify("./1025_0.jpg", "127.0.0.1")


