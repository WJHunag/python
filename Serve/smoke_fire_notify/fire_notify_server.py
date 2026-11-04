import re
import requests
from datetime import datetime
from flask import Flask, request

app = Flask(__name__)

# Discord Webhook URL
DISCORD_WEBHOOK_URL = "https://discord.com/api/webhooks/1373739943758794973/HgK206W4yxSkKZLY_5JaExQI4-0zkqu2B0wcM-7Pyf8bna6esAg0KeZPiWBrwm_hfVVh"

# 固定提醒文案
WARNING_TEXT = (
    "臺中市環保局提醒您\n"
    "位處事故發生附近地區的民眾請關閉門窗，環保局將持續監控空氣品質，"
    "並掌握附近空氣品質測站及空氣盒子之監測數據，請民眾不必恐慌。"
)

STATUS_REPORT = (
    "📌現況報告：市府消防局目前已抵達現場滅火；"
    "環保局應變小組亦已抵達現場，並持續掌握空品變化情形。"
)

def send_discord(message):
    data = {"content": message}
    resp = requests.post(DISCORD_WEBHOOK_URL, json=data)
    return resp.status_code

def clean_location(raw_loc):
    """
    智慧清洗地點函式 (含規則順序)：
    Rule 1 (最高): 國道/快速道路 -> 保留K數
    Rule 2 (次高): 交叉路口 (含 "跟", "與") -> 保留兩條路名
    Rule 3 (一般): 一般道路 -> 只保留到路/街/段/巷/弄，切除後方雜訊
    """
    # 0. 基礎清理
    loc = raw_loc.replace("左右", "").replace("一帶", "").replace("約", "")
    loc = re.sub(r"\d+(?:-\d+)*號", "", loc) 

    # --- Rule 1: 國道/快速道路 ---
    is_highway = "國道" in loc or "快速道路" in loc
    if is_highway:
        k_match = re.search(r"(\d+(?:\.\d+)?[kK])", loc)
        k_marker = k_match.group(1) if k_match else ""
        bridge = "橋下" if "橋下" in loc else ""
        base_road = re.split(r"[，,（(]", loc)[0].strip()
        cleaned = f"{base_road}{k_marker}{bridge}"
        
        if "附近" not in cleaned: cleaned += "附近"
        return cleaned

    # --- Rule 2: 交叉路口 (跟/與) ---
    if "跟" in loc or "與" in loc:
        if loc.endswith("口"): loc = loc[:-1]
        splitter = "跟" if "跟" in loc else "與"
        parts = loc.split(splitter)
        part1 = parts[0].strip()
        part2 = parts[1].strip()
        cleaned = f"{part1}{splitter}{part2}"
        
        if "附近" not in cleaned: cleaned += "附近"
        return cleaned

    # --- Rule 3: 一般道路 ---
    first_part = re.split(r"[，,（(]", loc)[0].strip()
    road_pattern = r"(.*?(?:路|街|大道|(?:[0-9一二三四五六七八九十]+段)|巷|弄))"
    match = re.search(road_pattern, first_part)
    
    if match:
        cleaned = match.group(1)
    else:
        cleaned = first_part

    if "附近" not in cleaned: cleaned += "附近"
    return cleaned

def get_time_description(text):
    """
    從內文中抓取 (一)報案：HH:MM
    並轉換成新的時段規則 (例如: 晚間7時許)
    """
    # 抓取 "報案：" 後面的小時 (group 1)
    time_match = re.search(r"\(一\)報案：\s*(\d{1,2}):", text)
    
    if time_match:
        hour = int(time_match.group(1))
        
        period = ""
        display_hour = hour

        # 依照您的新規則進行判斷
        # 00：00~05：59：清晨 (0點轉為12)
        if 0 <= hour < 6:
            period = "清晨"
            display_hour = 12 if hour == 0 else hour
        
        # 06：00~11：59：上午
        elif 6 <= hour < 12:
            period = "上午"
            display_hour = hour
            
        # 12:00-13:59：中午 (13點轉為1)
        elif 12 <= hour < 14:
            period = "中午"
            display_hour = hour if hour == 12 else (hour - 12)
            
        # 14:00：00~17：59：下午 (轉12小時制)
        elif 14 <= hour < 18:
            period = "下午"
            display_hour = hour - 12
            
        # 18：00~23：59：晚間 (轉12小時制)
        elif 18 <= hour <= 23:
            period = "晚間"
            display_hour = hour - 12
            
        return f"{period}{display_hour}時許"
    
    return "" # 抓不到時間就回傳空字串

def parse_message(text):
    # 1. 火警類型
    case_reason_match = re.search(r"([\u4e00-\u9fa5]{2}火警)", text)
    case_reason = case_reason_match.group(1) if case_reason_match else "火警"

    # 2. 地點提取 & 清洗
    loc_match = re.search(r"地點：([^\n\r]+)", text)
    raw_location = loc_match.group(1).strip() if loc_match else "未知地點"
    location = clean_location(raw_location)

    # 3. 日期提取
    date_match = re.search(r"日期：([^\n\r]+)", text)
    raw_date = date_match.group(1).strip() if date_match else ""
    mm_dd_match = re.search(r"(\d{1,2}/\d{1,2})", raw_date)
    formatted_date = mm_dd_match.group(1) if mm_dd_match else datetime.now().strftime("%m/%d")

    # 4. 🌟 時間提取 (使用新規則)
    time_desc = get_time_description(text)

    return case_reason, location, formatted_date, time_desc

@app.route("/webhook", methods=["POST"])
def webhook():
    payload = request.get_json()
    print("接收到的 payload：", payload)

    if "events" in payload:
        for event in payload["events"]:
            if event.get("type") == "message" and event["source"].get("type") == "group":
                if event["message"].get("type") == "text":
                    txt = event["message"]["text"]
                    
                    if "事件通報" in txt:
                        case, loc, date_str, time_str = parse_message(txt)
                        
                        # 處理時間與標點的連接
                        # 注意：這裡保留 " 於"，後面接 time_str (例如: 清晨1時許)
                        time_part = f" 於{time_str}" if time_str else ""
                        
                        new_text = (
                            "📢空污突發事件通知📢\n\n"
                            f"今日({date_str}）{time_part}，於本市{loc}發生火警，🧯消防單位目前已抵達現場滅火。\n\n"
                            "目前環境風場偏○○風，該址下風處及附近區域皆可能會有異味情形發生。\n\n"
                            f"{WARNING_TEXT}\n\n"
                            f"{STATUS_REPORT}"
                        )
                        
                        status = send_discord(new_text)
                        print("發送訊息到 Discord 狀態：", status)
                    else:
                        print("訊息不符合「事件通報」格式，跳過")
    return "OK", 200

if __name__ == "__main__":
    app.run(debug=True, port=5180)