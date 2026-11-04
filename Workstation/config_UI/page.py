"""Main workstation configuration page."""

from __future__ import annotations

import copy
import tkinter as tk
from tkinter import messagebox, ttk
from typing import Any

import utility

from .config_store import CameraConfigError, CameraConfigStore
from .element import COLORS, StreamCanvas


class SmokeDetectPage(ttk.Frame):
    """Camera preview, PTZ controls, presets, and per-camera settings."""

    SWITCHES = (
        ("enable_smoke_notify", "煙霧通知"),
        ("enable_google_upload", "Google Drive 上傳"),
        ("enable_nvr_recording", "NVR 錄影"),
        ("enable_camera_ptz", "鏡頭巡弋"),
        ("adjust_camera_zoom_on_detection", "偵煙追焦"),
        ("enable_night_detection", "夜間檢測"),
    )

    def __init__(self, parent: tk.Misc) -> None:
        super().__init__(parent, style="App.TFrame")
        self.store = CameraConfigStore()
        self.streamer = utility.CamCapture()
        self.video_canvas: StreamCanvas | None = None
        self.camera_ip: str | None = None
        self.current_camera: dict[str, Any] | None = None
        self.current_camera_settings: dict[str, Any] = {}
        self.cameras_info: list[dict[str, Any]] = []
        self.presets_dict: dict[str, str | None] = {}
        self._preset_entries: list[tuple[str, str, str]] = []
        self._closed = False

        self._create_variables()
        self._build_ui()
        self.reload_cameras()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------
    def _create_variables(self) -> None:
        self.camera_label_var = tk.StringVar(self, value="請選擇攝影機")
        self.camera_location_var = tk.StringVar(self, value="位置：尚未選擇")
        self.camera_channel_var = tk.StringVar(self, value="通道：-")
        self.status_var = tk.StringVar(self, value="正在載入工作站設定…")
        self.switch_vars = {
            key: tk.BooleanVar(self, value=False) for key, _label in self.SWITCHES
        }
        self.stage1_detection_var = tk.BooleanVar(self, value=False)
        self.stage2_detection_var = tk.BooleanVar(self, value=False)

    def _build_ui(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        header = ttk.Frame(self, style="App.TFrame")
        header.grid(row=0, column=0, sticky="ew", padx=4, pady=(0, 10))
        header.columnconfigure(1, weight=1)
        ttk.Label(
            header,
            text="煙霧檢測工作站",
            style="Header.TLabel",
        ).grid(row=0, column=0, sticky="w")
        ttk.Label(
            header,
            text="每個工作站只顯示目前分派的攝影機，設定直接寫回 distributed_config。",
            style="Muted.TLabel",
        ).grid(row=0, column=1, sticky="w", padx=(16, 0))

        self.body = ttk.PanedWindow(self, orient=tk.HORIZONTAL)
        self.body.grid(row=1, column=0, sticky="nsew")

        self.camera_panel = ttk.Frame(self.body, style="Panel.TFrame", padding=12)
        self.preview_panel = ttk.Frame(self.body, style="Panel.TFrame", padding=10)
        self.settings_panel = ttk.Frame(self.body, style="Panel.TFrame", padding=12)
        self.body.add(self.camera_panel, weight=1)
        self.body.add(self.preview_panel, weight=3)
        self.body.add(self.settings_panel, weight=2)

        self._build_camera_panel()
        self._build_preview_panel()
        self._build_settings_panel()

    def _build_camera_panel(self) -> None:
        self.camera_panel.columnconfigure(0, weight=1)
        self.camera_panel.rowconfigure(3, weight=1)

        camera_frame = ttk.LabelFrame(
            self.camera_panel,
            text="攝影機",
            style="Panel.TLabelframe",
            padding=10,
        )
        camera_frame.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        camera_frame.columnconfigure(0, weight=1)

        ttk.Label(camera_frame, text="選擇來源", style="App.TLabel").grid(
            row=0, column=0, sticky="w"
        )
        self.cam_ip_cbb = ttk.Combobox(
            camera_frame,
            state="readonly",
            textvariable=self.camera_label_var,
            style="App.TCombobox",
        )
        self.cam_ip_cbb.grid(row=1, column=0, sticky="ew", pady=(4, 8))
        self.cam_ip_cbb.bind("<<ComboboxSelected>>", self.update_location)

        ttk.Label(
            camera_frame,
            textvariable=self.camera_location_var,
            style="App.TLabel",
        ).grid(row=2, column=0, sticky="w")
        ttk.Label(
            camera_frame,
            textvariable=self.camera_channel_var,
            style="Muted.TLabel",
        ).grid(row=3, column=0, sticky="w", pady=(2, 0))

        button_row = ttk.Frame(camera_frame, style="Panel.TFrame")
        button_row.grid(row=4, column=0, sticky="ew", pady=(10, 0))
        button_row.columnconfigure(0, weight=1)
        button_row.columnconfigure(1, weight=1)
        ttk.Button(
            button_row,
            text="重新載入",
            command=self.reload_cameras,
            style="App.TButton",
        ).grid(row=0, column=0, sticky="ew", padx=(0, 4))
        ttk.Button(
            button_row,
            text="開始串流",
            command=self.start_streaming_and_clear_presets,
            style="Accent.TButton",
        ).grid(row=0, column=1, sticky="ew", padx=(4, 0))

        ttk.Button(
            self.camera_panel,
            text="啟動多支攝影機檢測",
            command=self.start_hub_center,
            style="Accent.TButton",
        ).grid(row=1, column=0, sticky="ew", pady=(0, 10))

        hint_frame = ttk.LabelFrame(
            self.camera_panel,
            text="操作提示",
            style="Panel.TLabelframe",
            padding=10,
        )
        hint_frame.grid(row=2, column=0, sticky="ew", pady=(0, 10))
        ttk.Label(
            hint_frame,
            text=(
                "1. 選擇攝影機\n"
                "2. 開始串流後可操作 PTZ 與預置點\n"
                "3. 調整右側設定後按儲存\n"
                "4. 設定變更通常需要重啟檢測程式"
            ),
            style="Muted.TLabel",
            justify="left",
        ).pack(anchor="w")

        self.status_label = ttk.Label(
            self.camera_panel,
            textvariable=self.status_var,
            style="Status.TLabel",
            anchor="w",
            justify="left",
            wraplength=260,
        )
        self.status_label.grid(row=4, column=0, sticky="ew", pady=(10, 0))

    def _build_preview_panel(self) -> None:
        self.preview_panel.columnconfigure(0, weight=1)
        self.preview_panel.rowconfigure(0, weight=4)
        self.preview_panel.rowconfigure(2, weight=2)

        preview_frame = ttk.LabelFrame(
            self.preview_panel,
            text="即時畫面",
            style="Panel.TLabelframe",
            padding=6,
        )
        preview_frame.grid(row=0, column=0, sticky="nsew", pady=(0, 10))
        preview_frame.columnconfigure(0, weight=1)
        preview_frame.rowconfigure(0, weight=1)
        self.video_canvas = StreamCanvas(preview_frame, self.streamer)
        self.video_canvas.grid(row=0, column=0, sticky="nsew")

        ptz_frame = ttk.LabelFrame(
            self.preview_panel,
            text="鏡頭控制",
            style="Panel.TLabelframe",
            padding=8,
        )
        ptz_frame.grid(row=1, column=0, sticky="ew", pady=(0, 10))
        for index in range(3):
            ptz_frame.columnconfigure(index, weight=1)
        ttk.Button(
            ptz_frame,
            text="上",
            command=lambda: self._run_ptz(utility.ptz_up),
            style="App.TButton",
            width=5,
        ).grid(row=0, column=1, padx=4, pady=2)
        ttk.Button(
            ptz_frame,
            text="左",
            command=lambda: self._run_ptz(utility.ptz_left),
            style="App.TButton",
            width=5,
        ).grid(row=1, column=0, padx=4, pady=2)
        ttk.Button(
            ptz_frame,
            text="停止",
            command=lambda: self._set_status("已送出鏡頭停止指令"),
            style="App.TButton",
            width=5,
        ).grid(row=1, column=1, padx=4, pady=2)
        ttk.Button(
            ptz_frame,
            text="右",
            command=lambda: self._run_ptz(utility.ptz_right),
            style="App.TButton",
            width=5,
        ).grid(row=1, column=2, padx=4, pady=2)
        ttk.Button(
            ptz_frame,
            text="下",
            command=lambda: self._run_ptz(utility.ptz_down),
            style="App.TButton",
            width=5,
        ).grid(row=2, column=1, padx=4, pady=2)

        preset_frame = ttk.LabelFrame(
            self.preview_panel,
            text="預置點",
            style="Panel.TLabelframe",
            padding=8,
        )
        preset_frame.grid(row=2, column=0, sticky="nsew")
        preset_frame.columnconfigure(0, weight=1)
        preset_frame.rowconfigure(0, weight=1)

        list_frame = ttk.Frame(preset_frame, style="Panel.TFrame")
        list_frame.grid(row=0, column=0, sticky="nsew")
        list_frame.columnconfigure(0, weight=1)
        list_frame.rowconfigure(0, weight=1)
        self.presets_listbox = tk.Listbox(
            list_frame,
            height=5,
            selectmode=tk.SINGLE,
            activestyle="none",
            background="#ffffff",
            foreground=COLORS["text"],
            selectbackground=COLORS["accent"],
            selectforeground="#ffffff",
            relief="solid",
            borderwidth=1,
            highlightthickness=0,
            font=("Microsoft JhengHei UI", 10),
        )
        self.presets_listbox.grid(row=0, column=0, sticky="nsew")
        preset_scrollbar = ttk.Scrollbar(
            list_frame,
            orient=tk.VERTICAL,
            command=self.presets_listbox.yview,
        )
        preset_scrollbar.grid(row=0, column=1, sticky="ns")
        self.presets_listbox.configure(yscrollcommand=preset_scrollbar.set)

        preset_actions = ttk.Frame(preset_frame, style="Panel.TFrame")
        preset_actions.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        preset_actions.columnconfigure(0, weight=1)
        preset_actions.columnconfigure(1, weight=1)
        preset_actions.columnconfigure(2, weight=1)
        ttk.Button(
            preset_actions,
            text="前往",
            command=self.go_to_selected_preset,
            style="App.TButton",
        ).grid(row=0, column=0, sticky="ew", padx=(0, 4))
        ttk.Button(
            preset_actions,
            text="刪除",
            command=self.clear_selected_preset,
            style="Danger.TButton",
        ).grid(row=0, column=1, sticky="ew", padx=4)
        ttk.Button(
            preset_actions,
            text="重新讀取",
            command=self.update_preset_info,
            style="App.TButton",
        ).grid(row=0, column=2, sticky="ew", padx=(4, 0))

        create_frame = ttk.Frame(preset_frame, style="Panel.TFrame")
        create_frame.grid(row=2, column=0, sticky="ew", pady=(8, 0))
        create_frame.columnconfigure(1, weight=1)
        ttk.Button(
            create_frame,
            text="建立預置點",
            command=self.enable_preset_creation,
            style="App.TButton",
        ).grid(row=0, column=0, padx=(0, 8))
        self.preset_name_entry = ttk.Entry(create_frame, state="disabled")
        self.preset_name_entry.grid(row=0, column=1, sticky="ew")
        self.confirm_btn = ttk.Button(
            create_frame,
            text="確認",
            command=self.confirm_preset,
            style="Accent.TButton",
            state="disabled",
        )
        self.confirm_btn.grid(row=0, column=2, padx=(8, 0))

    def _build_settings_panel(self) -> None:
        self.settings_panel.columnconfigure(0, weight=1)
        self.settings_panel.rowconfigure(3, weight=1)

        feature_frame = ttk.LabelFrame(
            self.settings_panel,
            text="功能開關",
            style="Panel.TLabelframe",
            padding=10,
        )
        feature_frame.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        feature_frame.columnconfigure(0, weight=1)
        for row, (key, label) in enumerate(self.SWITCHES):
            ttk.Checkbutton(
                feature_frame,
                text=label,
                variable=self.switch_vars[key],
                style="App.TCheckbutton",
            ).grid(row=row, column=0, sticky="w", pady=2)

        detection_frame = ttk.LabelFrame(
            self.settings_panel,
            text="檢測階段",
            style="Panel.TLabelframe",
            padding=10,
        )
        detection_frame.grid(row=1, column=0, sticky="ew", pady=(0, 10))
        detection_frame.columnconfigure(0, weight=1)
        ttk.Checkbutton(
            detection_frame,
            text="啟用一階檢測",
            variable=self.stage1_detection_var,
            command=self._on_stage1_changed,
            style="App.TCheckbutton",
        ).grid(row=0, column=0, sticky="w", pady=2)
        self.stage2_check = ttk.Checkbutton(
            detection_frame,
            text="啟用二階檢測",
            variable=self.stage2_detection_var,
            command=self._on_stage2_changed,
            style="App.TCheckbutton",
        )
        self.stage2_check.grid(row=1, column=0, sticky="w", pady=2)

        timing_frame = ttk.LabelFrame(
            self.settings_panel,
            text="時間與信心值",
            style="Panel.TLabelframe",
            padding=10,
        )
        timing_frame.grid(row=2, column=0, sticky="ew", pady=(0, 10))
        timing_frame.columnconfigure(1, weight=1)
        timing_frame.columnconfigure(3, weight=1)

        self.record_time_entry = self._add_numeric_setting(
            timing_frame, 0, 0, "錄影時間（分鐘）"
        )
        self.focus_time_entry = self._add_numeric_setting(
            timing_frame, 1, 0, "追焦時間（分鐘）"
        )
        self.patrol_time_entry = self._add_numeric_setting(
            timing_frame, 2, 0, "巡弋時間（分鐘）"
        )
        self.confidence_stage1_entry = self._add_numeric_setting(
            timing_frame, 0, 2, "一階信心值（0–100）"
        )
        self.confidence_stage2_entry = self._add_numeric_setting(
            timing_frame, 1, 2, "二階信心值（0–100）"
        )

        night_frame = ttk.Frame(timing_frame, style="Panel.TFrame")
        night_frame.grid(row=3, column=0, columnspan=4, sticky="ew", pady=(10, 0))
        night_frame.columnconfigure(1, weight=1)
        night_frame.columnconfigure(4, weight=1)
        ttk.Label(night_frame, text="夜間開始", style="App.TLabel").grid(
            row=0, column=0, sticky="w"
        )
        self.night_start_hour = self._time_combobox(night_frame)
        self.night_start_hour.grid(row=0, column=1, sticky="w", padx=(6, 2))
        self.night_start_minute = self._minute_combobox(night_frame)
        self.night_start_minute.grid(row=0, column=2, sticky="w")
        ttk.Label(night_frame, text="夜間結束", style="App.TLabel").grid(
            row=0, column=3, sticky="w", padx=(14, 0)
        )
        self.night_end_hour = self._time_combobox(night_frame)
        self.night_end_hour.grid(row=0, column=4, sticky="w", padx=(6, 2))
        self.night_end_minute = self._minute_combobox(night_frame)
        self.night_end_minute.grid(row=0, column=5, sticky="w")

        save_frame = ttk.Frame(self.settings_panel, style="Panel.TFrame")
        save_frame.grid(row=3, column=0, sticky="sew")
        save_frame.columnconfigure(0, weight=1)
        ttk.Separator(save_frame, orient=tk.HORIZONTAL).grid(
            row=0, column=0, sticky="ew", pady=(0, 10)
        )
        ttk.Button(
            save_frame,
            text="儲存目前攝影機設定",
            command=self.save_changes,
            style="Accent.TButton",
        ).grid(row=1, column=0, sticky="ew")

    def _add_numeric_setting(
        self,
        parent: tk.Misc,
        row: int,
        column: int,
        label: str,
    ) -> ttk.Entry:
        ttk.Label(parent, text=label, style="App.TLabel").grid(
            row=row, column=column, sticky="w", pady=3
        )
        entry = ttk.Entry(parent, width=7, style="App.TEntry")
        entry.configure(
            validate="key",
            validatecommand=(self.register(self._validate_integer), "%P"),
        )
        entry.grid(row=row, column=column + 1, sticky="ew", padx=(8, 10), pady=3)
        return entry

    @staticmethod
    def _validate_integer(value: str) -> bool:
        return value == "" or value.isdigit()

    @staticmethod
    def _time_combobox(parent: tk.Misc) -> ttk.Combobox:
        return ttk.Combobox(
            parent,
            state="readonly",
            width=3,
            values=[f"{hour:02d}" for hour in range(24)],
            style="App.TCombobox",
        )

    @staticmethod
    def _minute_combobox(parent: tk.Misc) -> ttk.Combobox:
        return ttk.Combobox(
            parent,
            state="readonly",
            width=3,
            values=[f"{minute:02d}" for minute in range(60)],
            style="App.TCombobox",
        )

    # ------------------------------------------------------------------
    # Configuration loading and saving
    # ------------------------------------------------------------------
    def reload_cameras(self) -> None:
        previous_ip = self.camera_ip
        try:
            self.store.reload()
        except CameraConfigError as exc:
            self.cameras_info = []
            self.cam_ip_cbb["values"] = ["請選擇攝影機"]
            self.cam_ip_cbb.set("請選擇攝影機")
            self._clear_camera_selection()
            self._set_status(str(exc))
            return

        self.cameras_info = copy.deepcopy(self.store.cameras)
        values = ["請選擇攝影機", *self.store.labels]
        self.cam_ip_cbb["values"] = values

        camera = self.store.camera_by_ip(previous_ip)
        if camera:
            label = next(
                (
                    label
                    for label, candidate in zip(self.store.labels, self.store.cameras)
                    if str(candidate.get("ip")) == str(previous_ip)
                ),
                None,
            )
            if label:
                self.cam_ip_cbb.set(label)
                self._select_camera(camera)
                return

        self.cam_ip_cbb.set("請選擇攝影機")
        self._clear_camera_selection()
        self._set_status(f"已載入 {len(self.store.cameras)} 支分派攝影機，請選擇來源。")

    def update_cam_ip_combobox(self) -> None:
        """Compatibility wrapper for the previous page API."""

        self.reload_cameras()

    def update_location(self, _event: tk.Event | None = None) -> None:
        camera = self.store.camera_by_label(self.cam_ip_cbb.get())
        if not camera:
            self._clear_camera_selection()
            self._set_status("請選擇有效的攝影機來源。")
            return
        self._select_camera(camera)

    def _select_camera(self, camera: dict[str, Any]) -> None:
        self.current_camera = copy.deepcopy(camera)
        self.camera_ip = str(camera.get("ip"))
        switches = camera.get("function_switches")
        self.current_camera_settings = (
            copy.deepcopy(switches) if isinstance(switches, dict) else {}
        )
        self.camera_location_var.set(f"位置：{camera.get('location') or '未命名攝影機'}")
        self.camera_channel_var.set(f"通道：{camera.get('NVR_channel') or '-'}")
        self.load_camera_settings()
        self._clear_presets("開始串流後載入預置點")
        self._set_status(f"已選擇 {camera.get('location') or self.camera_ip}")

    def _clear_camera_selection(self) -> None:
        self.current_camera = None
        self.camera_ip = None
        self.current_camera_settings = {}
        self.camera_location_var.set("位置：尚未選擇")
        self.camera_channel_var.set("通道：-")
        for variable in self.switch_vars.values():
            variable.set(False)
        self.stage1_detection_var.set(False)
        self.stage2_detection_var.set(False)
        self._on_stage1_changed()
        self._clear_presets()

    def load_camera_settings(self) -> None:
        """Load the selected camera into the editable controls."""

        camera = self.current_camera
        if not camera:
            return

        switches = camera.get("function_switches")
        switches = switches if isinstance(switches, dict) else {}
        for key, _label in self.SWITCHES:
            self.switch_vars[key].set(bool(switches.get(key, False)))

        self._set_entry(self.record_time_entry, switches.get("record_time", 10))
        self._set_entry(self.focus_time_entry, switches.get("focus_time", 5))
        self._set_entry(self.patrol_time_entry, camera.get("patrol_time", 15))
        self._set_entry(
            self.confidence_stage1_entry,
            self._confidence_percent(camera.get("stage1_confidence", 0.6)),
        )
        self._set_entry(
            self.confidence_stage2_entry,
            self._confidence_percent(camera.get("stage2_confidence", 0.6)),
        )

        start_hour, start_minute = self._parse_time(
            switches.get("night_detection_start", "00:00")
        )
        end_hour, end_minute = self._parse_time(
            switches.get("night_detection_end", "00:00")
        )
        self.night_start_hour.set(start_hour)
        self.night_start_minute.set(start_minute)
        self.night_end_hour.set(end_hour)
        self.night_end_minute.set(end_minute)

        self.stage1_detection_var.set(bool(switches.get("stage1_detection", False)))
        self.stage2_detection_var.set(
            bool(switches.get("stage2_detection", False))
            and self.stage1_detection_var.get()
        )
        self._on_stage1_changed()

    @staticmethod
    def _confidence_percent(value: Any) -> int:
        try:
            number = float(value)
        except (TypeError, ValueError):
            number = 0.6
        return max(0, min(100, round(number * 100)))

    @staticmethod
    def _parse_time(value: Any) -> tuple[str, str]:
        try:
            hour_text, minute_text = str(value).split(":", 1)
            hour = int(hour_text)
            minute = int(minute_text)
            if 0 <= hour < 24 and 0 <= minute < 60:
                return f"{hour:02d}", f"{minute:02d}"
        except (TypeError, ValueError):
            pass
        return "00", "00"

    @staticmethod
    def _set_entry(entry: ttk.Entry, value: Any) -> None:
        entry.configure(validate="none")
        entry.delete(0, tk.END)
        entry.insert(0, str(value))
        entry.configure(validate="key")

    def _read_positive_int(self, entry: ttk.Entry, label: str) -> int:
        raw = entry.get().strip()
        try:
            value = int(raw)
        except ValueError as exc:
            raise ValueError(f"{label} 必須是正整數。") from exc
        if value <= 0:
            raise ValueError(f"{label} 必須是正整數。")
        return value

    def _read_confidence(self, entry: ttk.Entry, label: str) -> float:
        raw = entry.get().strip()
        try:
            value = int(raw)
        except ValueError as exc:
            raise ValueError(f"{label} 必須是 0 到 100 的整數。") from exc
        if not 0 <= value <= 100:
            raise ValueError(f"{label} 必須介於 0 到 100。")
        return value / 100.0

    def _build_camera_update(self) -> dict[str, Any]:
        if not self.current_camera:
            raise ValueError("請先選擇攝影機。")

        camera = copy.deepcopy(self.current_camera)
        switches = camera.get("function_switches")
        switches = copy.deepcopy(switches) if isinstance(switches, dict) else {}

        for key, _label in self.SWITCHES:
            switches[key] = bool(self.switch_vars[key].get())

        stage1_enabled = bool(self.stage1_detection_var.get())
        stage2_enabled = bool(self.stage2_detection_var.get())
        if stage2_enabled and not stage1_enabled:
            raise ValueError("二階檢測必須在一階檢測啟用時才能使用。")
        switches["stage1_detection"] = stage1_enabled
        switches["stage2_detection"] = stage2_enabled
        switches["record_time"] = self._read_positive_int(
            self.record_time_entry, "錄影時間"
        )
        switches["focus_time"] = self._read_positive_int(
            self.focus_time_entry, "追焦時間"
        )
        switches["night_detection_start"] = (
            f"{self.night_start_hour.get()}:{self.night_start_minute.get()}"
        )
        switches["night_detection_end"] = (
            f"{self.night_end_hour.get()}:{self.night_end_minute.get()}"
        )
        switches.pop("stage1_confidence", None)
        switches.pop("stage2_confidence", None)

        camera["function_switches"] = switches
        camera["patrol_time"] = self._read_positive_int(
            self.patrol_time_entry, "巡弋時間"
        )
        camera["stage1_confidence"] = self._read_confidence(
            self.confidence_stage1_entry, "一階信心值"
        )
        camera["stage2_confidence"] = self._read_confidence(
            self.confidence_stage2_entry, "二階信心值"
        )

        if self.switch_vars["enable_camera_ptz"].get():
            camera["patrol_points"] = [
                actual_name for _index, actual_name, _display_name in self._preset_entries
            ]

        return camera

    def save_changes(self) -> None:
        try:
            camera = self._build_camera_update()
            self.store.save_camera(camera)
            self.current_camera = self.store.camera_by_ip(self.camera_ip)
            self.cameras_info = copy.deepcopy(self.store.cameras)
            self._set_status("設定已儲存；若要套用到檢測端，請重啟檢測程式。")
            messagebox.showinfo(
                "儲存完成",
                "目前攝影機設定已寫入 distributed_config。\n"
                "請重啟 multi_source_vision_hub_center 以套用變更。",
            )
        except (CameraConfigError, ValueError) as exc:
            self._set_status(f"儲存失敗：{exc}")
            messagebox.showerror("儲存失敗", str(exc))
        except Exception as exc:
            self._set_status(f"儲存發生未預期錯誤：{exc}")
            messagebox.showerror("儲存失敗", f"發生未預期錯誤：{exc}")

    # ------------------------------------------------------------------
    # Detection, stream, PTZ, and presets
    # ------------------------------------------------------------------
    def _on_stage1_changed(self) -> None:
        if not self.stage1_detection_var.get():
            self.stage2_detection_var.set(False)
            self.stage2_check.state(["disabled"])
        else:
            self.stage2_check.state(["!disabled"])

    def _on_stage2_changed(self) -> None:
        if self.stage2_detection_var.get() and not self.stage1_detection_var.get():
            self.stage2_detection_var.set(False)
            self._set_status("請先啟用一階檢測，再啟用二階檢測。")

    def _require_camera(self) -> bool:
        if self.camera_ip:
            return True
        self._set_status("請先選擇攝影機。")
        return False

    def start_streaming_and_clear_presets(self) -> None:
        if not self._require_camera() or self.video_canvas is None:
            return
        self._clear_presets()
        try:
            utility.start_canvas(self.camera_ip, self.video_canvas, self.streamer)
            self.update_preset_info()
            self._set_status("已送出串流連線，正在等待最新畫面。")
        except Exception as exc:
            self._set_status(f"串流啟動失敗：{exc}")
            messagebox.showerror("串流啟動失敗", str(exc))

    def start_hub_center(self) -> None:
        try:
            utility.start_hub_center_process()
            self._set_status("已啟動多支攝影機檢測程式。")
        except Exception as exc:
            self._set_status(f"多機檢測啟動失敗：{exc}")
            messagebox.showerror("啟動失敗", str(exc))

    def _run_ptz(self, action) -> None:
        if not self._require_camera():
            return
        try:
            response = action(self.camera_ip)
            self._set_status(f"鏡頭指令完成：{str(response).strip() or 'OK'}")
        except Exception as exc:
            self._set_status(f"鏡頭控制失敗：{exc}")
            messagebox.showerror("鏡頭控制失敗", str(exc))

    def _clear_presets(self, message: str = "") -> None:
        self.presets_dict = {}
        self._preset_entries = []
        self.presets_listbox.delete(0, tk.END)
        if message:
            self._set_status(message)

    def update_preset_info(self) -> None:
        if not self._require_camera():
            return
        self._clear_presets()
        try:
            presets = utility.get_presets_info(self.camera_ip, 1)
        except Exception as exc:
            self._set_status(f"讀取預置點失敗：{exc}")
            return

        self.presets_dict = {
            str(index): name for index, name in (presets or {}).items()
        }
        for index, name in self.presets_dict.items():
            if name is None:
                continue
            display_name = "攝影機原點" if name == "預設點5" else str(name)
            self._preset_entries.append((str(index), str(name), display_name))
            self.presets_listbox.insert(tk.END, f"{index}  ·  {display_name}")

        if self._preset_entries:
            self._set_status(f"已讀取 {len(self._preset_entries)} 個預置點。")
        else:
            self._set_status("目前攝影機沒有可用預置點。")

    def _selected_preset(self) -> tuple[str, str, str] | None:
        selection = self.presets_listbox.curselection()
        if not selection:
            self._set_status("請先選擇預置點。")
            return None
        index = selection[0]
        if index >= len(self._preset_entries):
            return None
        return self._preset_entries[index]

    def go_to_selected_preset(self) -> None:
        if not self._require_camera():
            return
        selected = self._selected_preset()
        if not selected:
            return
        preset_index, _actual_name, display_name = selected
        try:
            response = utility.go_to_preset(self.camera_ip, 1, preset_index)
            self._set_status(f"已前往預置點：{display_name}（{response}）")
        except Exception as exc:
            self._set_status(f"前往預置點失敗：{exc}")
            messagebox.showerror("預置點操作失敗", str(exc))

    def clear_selected_preset(self) -> None:
        if not self._require_camera():
            return
        selected = self._selected_preset()
        if not selected:
            return
        preset_index, _actual_name, display_name = selected
        if not messagebox.askyesno("刪除預置點", f"確定刪除「{display_name}」嗎？"):
            return
        try:
            response = utility.clear_preset(self.camera_ip, 1, preset_index)
            self._set_status(f"已刪除預置點：{display_name}（{response}）")
            self.update_preset_info()
        except Exception as exc:
            self._set_status(f"刪除預置點失敗：{exc}")
            messagebox.showerror("預置點操作失敗", str(exc))

    def enable_preset_creation(self) -> None:
        self.preset_name_entry.configure(state="normal")
        self.confirm_btn.configure(state="normal")
        self.preset_name_entry.delete(0, tk.END)
        self.preset_name_entry.focus_set()

    def confirm_preset(self) -> None:
        if not self._require_camera():
            return
        name = self.preset_name_entry.get().strip()
        if not name or not name.isascii() or not name.isalnum():
            messagebox.showerror("建立失敗", "預置點名稱只能使用英文字母與數字。")
            return

        used_indices = {str(index) for index, _name, _display in self._preset_entries}
        free_index = next(
            (
                str(index)
                for index in range(1, 301)
                if index != 5 and str(index) not in used_indices
            ),
            None,
        )
        if free_index is None:
            messagebox.showerror("建立失敗", "沒有可用的預置點編號。")
            return

        try:
            response = utility.set_preset(self.camera_ip, 1, free_index, name)
            if str(response).strip().upper() != "OK":
                raise RuntimeError(str(response).strip() or "攝影機拒絕建立預置點")
            self.preset_name_entry.configure(state="disabled")
            self.confirm_btn.configure(state="disabled")
            self.update_preset_info()
            self._set_status(f"已建立預置點：{name}")
        except Exception as exc:
            self._set_status(f"建立預置點失敗：{exc}")
            messagebox.showerror("建立失敗", str(exc))

    # ------------------------------------------------------------------
    # Lifecycle and compatibility helpers
    # ------------------------------------------------------------------
    def _set_status(self, message: str) -> None:
        self.status_var.set(str(message))

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self.video_canvas is not None:
            self.video_canvas.stop_refresh()
        self.streamer.close_stream()

