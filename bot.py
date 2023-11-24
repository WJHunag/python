import configparser
from datetime import datetime
import requests
import utility 


"""
用於LINE notify通知
"""


def smoke_notify(img_path, ip):
    ip_path = "./local_info.txt"
    local_info = utility.get_local_info(ip_path)
    config = configparser.ConfigParser()
    config.read("./config.ini")
    notify_token = config['token']['notify']

    now_timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    print(str(now_timestamp))
    #Bearer後面一定要一個空格
    headers = {
        "Authorization": "Bearer " + notify_token
    }
    data = {
        "message": f"檢測到煙霧\n時間：{now_timestamp}\n位置：{local_info[ip]}"
    }
    img_file = {"imageFile": open(img_path, "rb")}
    r = requests.post("https://notify-api.line.me/api/notify",
                    headers=headers, data=data, files=img_file)


if __name__ == "__main__":
    smoke_notify("./1025_0.jpg", "127.0.0.1")


