import tkinter as tk
from PIL import Image, ImageTk, UnidentifiedImageError
import os
from utility import get_camera_ip, get_json_data

class CameraImageDisplay(tk.Canvas):
    def __init__(self, root, canvas_size, image_dir, camera_ip):
        self.width = canvas_size[0]
        self.height = canvas_size[1]
        super().__init__(root, width=self.width, height=self.height, bg='black')
        self.image_dir = image_dir
        self.camera_ip = camera_ip
        json_path = './smoke_camera_config.json'  # 指定 JSON 配置檔的路徑
        self.camera_name = get_json_data('local_info', json_path).get(camera_ip, '未知位置')
        self.pack()
        self.latest_image = None
        self.current_image_path = None  # 新增此行
        self.update_canvas()
        self.check_for_updates()

    def update_canvas(self):
        latest_image_path = self.get_latest_image()
        if latest_image_path and os.path.exists(latest_image_path):
            if latest_image_path != self.current_image_path:
                try:
                    frame = Image.open(latest_image_path).convert('RGB')
                    resized_frame = frame.resize((self.width, self.height), Image.LANCZOS)
                    self.photo = ImageTk.PhotoImage(image=resized_frame)
                    self.delete("all")
                    self.create_image(0, 0, image=self.photo, anchor="nw")
                    text_x = self.width - 10
                    text_y = self.height - 10
                    self.create_text(text_x - 2, text_y - 2, anchor='se', text=self.camera_name, fill='black', font=('Microsoft YaHei', 14, 'bold'))
                    self.create_text(text_x, text_y, anchor='se', text=self.camera_name, fill='white', font=('Microsoft YaHei', 14, 'bold'))
                    self.current_image_path = latest_image_path  # 更新當前圖片路徑
                except UnidentifiedImageError as e:
                    print(f"更新圖片時發生錯誤，無法識別圖片: {e}")
                except Exception as e:
                    print(f"更新圖片時發生錯誤: {e}")
        elif self.latest_image is None:
            self.delete("all")  # 只有當沒有任何圖片時才清除畫布

    def get_latest_image(self):
        if not os.path.exists(self.image_dir):
            return None
        images = [img for img in os.listdir(self.image_dir) if img.endswith(('.png', '.jpg', '.jpeg'))]
        if not images:
            return None
        latest_image = max(images, key=lambda x: os.path.getctime(os.path.join(self.image_dir, x)))
        latest_image_path = os.path.join(self.image_dir, latest_image)
        if latest_image != self.latest_image:
            self.latest_image = latest_image
            print(f"新圖片已被偵測到: {latest_image}")
        return latest_image_path  # 始終返回最新的圖片路徑

    def check_for_updates(self):
        self.update_canvas()
        self.after(1000, self.check_for_updates)  # 每隔一秒檢查一次

def main():
    window = tk.Tk()
    window.title('多攝像頭最新圖片顯示')
    window.state('zoomed')
    
    win_size = [1920, 1020]
    num_rows = 3
    num_cols = 5
    canvas_size = (win_size[0] // num_cols, win_size[1] // num_rows)
    
    window.geometry(f"{win_size[0]}x{win_size[1]}")
    window.configure(bg='#AFEEEE')
    window.resizable(width=1, height=1)
    
    camera_ips = get_camera_ip()
    camera_ips = camera_ips[:num_rows * num_cols]
    
    base_image_dir = './received_results'
    for idx, ip in enumerate(camera_ips):
        ip_folder = ip.replace('.', '_')
        image_dir = os.path.join(base_image_dir, ip_folder)
        canvas = CameraImageDisplay(window, canvas_size, image_dir, ip)
        canvas.place(x=canvas_size[0] * (idx % num_cols), y=canvas_size[1] * (idx // num_cols))
    
    window.mainloop()

if __name__ == "__main__":
    main()
