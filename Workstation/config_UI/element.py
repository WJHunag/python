"""Reusable widgets for the workstation configuration UI."""

from __future__ import annotations

import tkinter as tk

import cv2
import numpy as np
from PIL import Image, ImageTk


COLORS = {
    "page": "#eef2f6",
    "panel": "#ffffff",
    "panel_muted": "#f6f8fa",
    "border": "#d6dde5",
    "text": "#17212b",
    "muted": "#5d6b78",
    "accent": "#1769aa",
    "accent_active": "#0d4f82",
    "danger": "#b23a48",
    "preview": "#17212b",
}


def configure_styles(root: tk.Misc) -> None:
    """Apply a compact, readable ttk theme without changing system settings."""

    from tkinter import ttk

    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass

    style.configure("App.TFrame", background=COLORS["page"])
    style.configure("Panel.TFrame", background=COLORS["panel"])
    style.configure(
        "Panel.TLabelframe",
        background=COLORS["panel"],
        bordercolor=COLORS["border"],
        relief="solid",
        borderwidth=1,
    )
    style.configure(
        "Panel.TLabelframe.Label",
        background=COLORS["panel"],
        foreground=COLORS["text"],
        font=("Microsoft JhengHei UI", 11, "bold"),
    )
    style.configure(
        "App.TLabel",
        background=COLORS["panel"],
        foreground=COLORS["text"],
        font=("Microsoft JhengHei UI", 10),
    )
    style.configure(
        "Muted.TLabel",
        background=COLORS["panel"],
        foreground=COLORS["muted"],
        font=("Microsoft JhengHei UI", 9),
    )
    style.configure(
        "Header.TLabel",
        background=COLORS["page"],
        foreground=COLORS["text"],
        font=("Microsoft JhengHei UI", 16, "bold"),
    )
    style.configure(
        "App.TButton",
        padding=(12, 7),
        font=("Microsoft JhengHei UI", 10, "bold"),
    )
    style.configure(
        "Accent.TButton",
        background=COLORS["accent"],
        foreground="#ffffff",
        padding=(14, 8),
        font=("Microsoft JhengHei UI", 10, "bold"),
    )
    style.map(
        "Accent.TButton",
        background=[("active", COLORS["accent_active"]), ("disabled", "#aebdca")],
        foreground=[("disabled", "#edf1f4")],
    )
    style.configure(
        "Danger.TButton",
        background=COLORS["danger"],
        foreground="#ffffff",
        padding=(10, 7),
        font=("Microsoft JhengHei UI", 10, "bold"),
    )
    style.map("Danger.TButton", background=[("active", "#8f2e3a")])
    style.configure(
        "App.TCheckbutton",
        background=COLORS["panel"],
        foreground=COLORS["text"],
        font=("Microsoft JhengHei UI", 10),
    )
    style.map("App.TCheckbutton", background=[("active", COLORS["panel"])])
    style.configure("App.TEntry", padding=5)
    style.configure("App.TCombobox", padding=4)
    style.configure(
        "Status.TLabel",
        background=COLORS["panel_muted"],
        foreground=COLORS["muted"],
        padding=(8, 6),
        font=("Microsoft JhengHei UI", 9),
    )


class StreamCanvas(tk.Canvas):
    """Display the latest camera frame and stop its timer safely on teardown."""

    def __init__(
        self,
        parent: tk.Misc,
        streamer,
        *,
        refresh_ms: int = 100,
    ) -> None:
        super().__init__(
            parent,
            background=COLORS["preview"],
            highlightthickness=0,
            bd=0,
        )
        self.streamer = streamer
        self.refresh_ms = refresh_ms
        self._after_id: str | None = None
        self._closed = False
        self._image: ImageTk.PhotoImage | None = None
        self._message_id: int | None = None
        self._render_message("尚未開始串流")
        self.start_refresh()

    def start_refresh(self) -> None:
        if self._closed or self._after_id is not None:
            return
        try:
            self._after_id = self.after(self.refresh_ms, self._refresh)
        except tk.TclError:
            self._after_id = None

    def stop_refresh(self) -> None:
        after_id = self._after_id
        self._after_id = None
        if after_id is None:
            return
        try:
            self.after_cancel(after_id)
        except tk.TclError:
            pass

    def _refresh(self) -> None:
        self._after_id = None
        if self._closed:
            return

        try:
            frame = self.streamer.getframe()
        except Exception:
            frame = None

        if isinstance(frame, np.ndarray) and frame.size:
            self._render_frame(frame)
        elif self._image is None:
            self._render_message("尚未收到影像，請先開始串流")

        self.start_refresh()

    def _render_frame(self, frame: np.ndarray) -> None:
        try:
            rgb_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            canvas_width = max(self.winfo_width(), 320)
            canvas_height = max(self.winfo_height(), 180)
            height, width = rgb_frame.shape[:2]
            scale = min(canvas_width / width, canvas_height / height)
            target_size = (
                max(1, int(width * scale)),
                max(1, int(height * scale)),
            )
            resized = cv2.resize(rgb_frame, target_size)
            self._image = ImageTk.PhotoImage(Image.fromarray(resized))
            self.delete("all")
            self.create_image(
                canvas_width // 2,
                canvas_height // 2,
                image=self._image,
                anchor="center",
            )
        except (cv2.error, tk.TclError, ValueError):
            self._render_message("影像顯示失敗")

    def _render_message(self, message: str) -> None:
        try:
            self.delete("all")
            self._message_id = self.create_text(
                max(self.winfo_width(), 320) // 2,
                max(self.winfo_height(), 180) // 2,
                text=message,
                fill="#c7d2dc",
                font=("Microsoft JhengHei UI", 12),
            )
        except tk.TclError:
            pass

    def destroy(self) -> None:
        self._closed = True
        self.stop_refresh()
        try:
            super().destroy()
        except tk.TclError:
            pass
