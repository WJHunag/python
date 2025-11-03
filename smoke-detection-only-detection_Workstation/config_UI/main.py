import tkinter as tk
from tkinter import ttk
from utility import switch_tab
from .page import SmokeDetectPage
import utility

"""
主程式
"""
class UI:
    def __init__(self, window):
        #設定視窗大小、視窗背景顏色
        self.window = window
        self.window.geometry('1280x720')
        self.window.configure(bg='#AFEEEE')
        self.window.resizable(width=0, height=0)
        self.pages_root = ttk.Notebook(root)
        
        #創建分頁欄
        self.detect_frame = tk.Frame(self.pages_root, bg='#FFFFFF')
        self.pages_root.add(self.detect_frame, text='煙霧檢測')
        self.pages_root.pack(expand=1, fill="both")

        #建置版面
        self.smoke_check = SmokeDetectPage()
        self.smoke_check.build(self.detect_frame)



if __name__ == '__main__':
    root = tk.Tk()
    root.withdraw()
    root.protocol("WM_DELETE_WINDOW", lambda: utility.on_closing(root))
    utility.show_login_window(lambda can_open, can_close: utility.initialize_main_window(root, can_open, can_close), root, 2)
    root.title("煙霧檢測")
    app = UI(root)
    app.window.mainloop()
    app.smoke_check.streamer.close_stream()     

