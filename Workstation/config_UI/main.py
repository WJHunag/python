"""Entry point for the workstation configuration UI."""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk

import utility

from .element import configure_styles
from .page import SmokeDetectPage


class UI:
    """Compose the application window and its configuration page."""

    def __init__(self, window: tk.Tk) -> None:
        self.window = window
        self.window.title("煙霧檢測工作站")
        self.window.geometry("1440x900")
        self.window.minsize(1120, 720)
        self.window.configure(bg="#eef2f6")

        configure_styles(self.window)

        root_frame = ttk.Frame(self.window, style="App.TFrame", padding=14)
        root_frame.pack(fill="both", expand=True)
        root_frame.columnconfigure(0, weight=1)
        root_frame.rowconfigure(0, weight=1)

        self.pages_root = ttk.Notebook(root_frame)
        self.pages_root.grid(row=0, column=0, sticky="nsew")

        self.detect_frame = ttk.Frame(self.pages_root, style="App.TFrame", padding=10)
        self.pages_root.add(self.detect_frame, text="煙霧檢測")
        self.detect_frame.columnconfigure(0, weight=1)
        self.detect_frame.rowconfigure(0, weight=1)

        self.smoke_check = SmokeDetectPage(self.detect_frame)
        self.smoke_check.grid(row=0, column=0, sticky="nsew")

    def close(self) -> None:
        self.smoke_check.close()


def main() -> None:
    root = tk.Tk()
    root.withdraw()
    root.protocol("WM_DELETE_WINDOW", lambda: utility.on_closing(root))

    login_state = {"can_open": False}

    def on_login(can_open: bool, can_close: bool) -> None:
        login_state["can_open"] = bool(can_open)
        utility.initialize_main_window(root, can_open, can_close)

    utility.show_login_window(on_login, root, 2)
    if not login_state["can_open"]:
        return
    try:
        if not root.winfo_exists():
            return
    except tk.TclError:
        return

    app = UI(root)
    try:
        root.mainloop()
    finally:
        app.close()


if __name__ == "__main__":
    main()
