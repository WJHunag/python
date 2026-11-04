import sys
from ..bot import smoke_notify

def trigger_smoke_notify(img_path, ip="127.0.0.1"):
    try:
        smoke_notify(img_path, ip)
        print("通知已發送！")
    except FileNotFoundError:
        print("錯誤：影像檔案不存在，請檢查路徑。")
    except Exception as e:
        print(f"通知失敗：{e}")

if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("使用方法: python linenotify_api_send.py <影像路徑>")
        sys.exit(1)
    
    img_path = sys.argv[1]
    trigger_smoke_notify(img_path)
