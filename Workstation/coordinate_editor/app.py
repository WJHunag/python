from __future__ import annotations

from pathlib import Path
import sys
import threading
import tkinter as tk
import tkinter.font as tkfont
from tkinter import filedialog, messagebox, ttk
from typing import Any, Callable

from PIL import ImageTk

COORDINATE_EDITOR_DIR = Path(__file__).resolve().parent
PROJECT_ROOT_DIR = COORDINATE_EDITOR_DIR.parent
for module_search_path in (PROJECT_ROOT_DIR, COORDINATE_EDITOR_DIR):
    module_search_path_text = str(module_search_path)
    if module_search_path_text not in sys.path:
        sys.path.insert(0, module_search_path_text)

import utils
import standalone_package


def action_button_options(
    font_family: str,
    *,
    primary: bool,
    **extra_options: Any,
) -> dict[str, Any]:
    """Return classic Tk button options whose colors are not theme-rendered."""
    options: dict[str, Any] = {
        "font": (font_family, 10),
        "borderwidth": 1,
        "relief": tk.RAISED,
        "padx": 12,
        "pady": 6,
        "highlightthickness": 1,
        "highlightbackground": "#bdc3c7",
        "highlightcolor": "#2980b9",
    }
    if primary:
        options.update(
            {
                "bg": "#2980b9",
                "fg": "#ffffff",
                "activebackground": "#3498db",
                "activeforeground": "#ffffff",
                "disabledforeground": "#d6eaf8",
            }
        )
    else:
        options.update(
            {
                "bg": "#eeeeee",
                "fg": "#333333",
                "activebackground": "#f5f5f5",
                "activeforeground": "#333333",
                "disabledforeground": "#666666",
            }
        )
    options.update(extra_options)
    return options


def coordinate_import_version_summary(preview_rows: list[dict[str, Any]]) -> str:
    changed_count = sum(bool(row.get("version_alert")) for row in preview_rows)
    unknown_count = sum(
        bool(row.get("exists") and row.get("version_unverified"))
        for row in preview_rows
    )
    if not changed_count and not unknown_count:
        return ""

    return f"版本不同 {changed_count} 支｜無法比對 {unknown_count} 支"


def coordinate_import_version_dates(row: dict[str, Any]) -> dict[str, str]:
    def recorded(value: Any) -> str:
        text = str(value or "").strip()
        return text or "未記錄"

    baseline_time = row.get("baseline_exported_at") or row.get("baseline_save_time")
    return {
        "baseline": recorded(baseline_time),
        "external": recorded(row.get("import_save_time")),
        "local": recorded(row.get("local_save_time")),
        "server": recorded(row.get("server_save_time")),
    }


def route_geo_reference_batch_worker_outcome(
    batch_runner: Callable[[], tuple[int, dict[str, Any]]],
    *,
    on_remote_failure: Callable[[utils.GeoReferenceBatchRemoteError], None],
    on_local_failure: Callable[[Exception], None],
    on_success: Callable[[int, dict[str, Any]], None],
) -> None:
    """Route one batch worker result to exactly one outcome callback."""
    try:
        local_updated_count, response_payload = batch_runner()
    except utils.GeoReferenceBatchRemoteError as exc:
        on_remote_failure(exc)
        return
    except Exception as exc:
        on_local_failure(exc)
        return

    on_success(local_updated_count, response_payload)


def prepare_local_batch_sync(
    current_camera_id: str | None,
    imported_camera_ids: set[str],
    save_current_callback: Callable[[], None],
    build_preview_callback: Callable[[], list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    """Save the current imported camera before reading the local batch preview."""
    if current_camera_id and current_camera_id in imported_camera_ids:
        save_current_callback()
    return build_preview_callback()


def record_single_sync_state_after_remote_success(
    config_path: str | Path,
    camera_id: str | None,
    *,
    state_marker: Callable[[str | Path, set[str]], dict[str, Any]] = (
        utils.mark_coordinate_cameras_synced
    ),
) -> Exception | None:
    """Record a single-camera sync only after the remote request succeeded."""
    if not camera_id:
        return None

    try:
        state = utils.load_coordinate_import_state(config_path)
        if camera_id not in set(state["imported_camera_ids"]):
            return None
        state_marker(config_path, {camera_id})
    except Exception as exc:
        return exc
    return None


def persist_batch_sync_state_and_update_preview(
    config_path: str | Path,
    preview_rows: list[dict[str, Any]],
    synced_camera_ids: set[str] | list[str],
    *,
    state_marker: Callable[[str | Path, set[str] | list[str]], dict[str, Any]] = (
        utils.mark_coordinate_cameras_synced
    ),
) -> Exception | None:
    """Update the preview model only after the synced sidecar is durable."""
    try:
        state_marker(config_path, synced_camera_ids)
    except Exception as exc:
        return exc

    synced_ids = {str(camera_id) for camera_id in synced_camera_ids}
    for row in preview_rows:
        if str(row.get("camera_id") or "") in synced_ids:
            row["synced"] = True
    return None


class CameraCoordinateApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("攝影機座標管理系統")
        self.root.geometry("1280x780")
        self.font_family = self._configure_fonts()

        self.paths = utils.AppPaths.from_app_file(__file__)
        self.config = utils.load_local_camera_config(self.paths.config_path)
        self.catalog = utils.CameraCatalog.from_config(self.config)
        if not self.catalog.cameras:
            raise ValueError("distributed_config 內沒有可用的 cameras")

        self.current_camera = None
        self.image_state = utils.CanvasImageState()
        self.tk_image = None
        self.loading_text = ""
        self.capture_request_id = 0
        self.point_index = 0
        self.drag_item_id = None
        self.drag_started = False

        self._build_layout()
        self._show_hint("[ 請先選擇攝影機 ]")
        self.camera_combo.current(0)
        self._on_camera_selected()

    def _configure_fonts(self) -> str:
        available_fonts = set(tkfont.families(self.root))
        font_family = "Microsoft JhengHei UI"
        if font_family not in available_fonts:
            font_family = (
                "Microsoft JhengHei"
                if "Microsoft JhengHei" in available_fonts
                else tkfont.nametofont("TkDefaultFont").cget("family")
            )

        for font_name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont", "TkTooltipFont"):
            try:
                tkfont.nametofont(font_name).configure(family=font_family, size=10)
            except tk.TclError:
                continue

        style = ttk.Style(self.root)
        style.configure("Treeview", font=(font_family, 10), rowheight=26)
        style.configure("Treeview.Heading", font=(font_family, 10, "bold"))
        return font_family

    def _build_layout(self) -> None:
        top_frame = ttk.Frame(self.root)
        top_frame.pack(fill=tk.X, padx=10, pady=(10, 6))

        ttk.Label(top_frame, text="選擇攝影機:").pack(side=tk.LEFT)
        self.camera_var = tk.StringVar()
        self.camera_combo = ttk.Combobox(
            top_frame,
            textvariable=self.camera_var,
            values=self.catalog.labels,
            state="readonly",
            width=55,
        )
        self.camera_combo.pack(side=tk.LEFT, padx=(8, 8))
        self.camera_combo.bind("<<ComboboxSelected>>", lambda _event: self._on_camera_selected())

        ttk.Button(top_frame, text="重新載入圖片", command=self._reload_current_camera).pack(side=tk.LEFT)
        ttk.Button(top_frame, text="即時擷取一張", command=self._capture_current_camera).pack(side=tk.LEFT, padx=(8, 0))

        self.info_var = tk.StringVar(value="尚未載入圖片")
        ttk.Label(top_frame, textvariable=self.info_var).pack(side=tk.LEFT, padx=(12, 0))

        self.paned = ttk.PanedWindow(self.root, orient=tk.HORIZONTAL)
        self.paned.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 10))

        self.canvas_frame = ttk.Frame(self.paned)
        self.paned.add(self.canvas_frame, weight=3)

        self.canvas = tk.Canvas(self.canvas_frame, bg="#000000", cursor="cross")
        self.canvas.pack(fill=tk.BOTH, expand=True)
        self.canvas.bind("<Button-1>", self.on_canvas_press)
        self.canvas.bind("<B1-Motion>", self.on_canvas_drag)
        self.canvas.bind("<ButtonRelease-1>", self.on_canvas_release)
        self.canvas.bind("<Motion>", self.on_canvas_motion)
        self.canvas.bind("<Configure>", lambda _event: self._render_canvas())

        self.right_frame = ttk.Frame(self.paned)
        self.paned.add(self.right_frame, weight=1)

        list_header = ttk.Frame(self.right_frame)
        list_header.pack(fill=tk.X, padx=5, pady=(0, 2))
        ttk.Label(list_header, text="點位清單", font=(self.font_family, 11, "bold")).pack(side=tk.LEFT)
        ttk.Label(list_header, text="右鍵點位可編輯或刪除", foreground="#666666").pack(side=tk.RIGHT)

        columns = ("Point_Name", "WGS84_X", "WGS84_Y", "Image_X", "Image_Y")
        self.tree = ttk.Treeview(self.right_frame, columns=columns, show="headings", selectmode="browse")
        self.tree.heading("Point_Name", text="點位名稱")
        self.tree.heading("WGS84_X", text="座標 X")
        self.tree.heading("WGS84_Y", text="座標 Y")
        self.tree.heading("Image_X", text="Image X")
        self.tree.heading("Image_Y", text="Image Y")
        self.tree.column("Point_Name", width=110, anchor=tk.CENTER)
        self.tree.column("WGS84_X", width=130, anchor=tk.CENTER)
        self.tree.column("WGS84_Y", width=130, anchor=tk.CENTER)
        self.tree.column("Image_X", width=0, stretch=tk.NO)
        self.tree.column("Image_Y", width=0, stretch=tk.NO)
        self.tree.pack(fill=tk.BOTH, expand=True, padx=5, pady=(0, 5))
        self.tree.bind("<Button-3>", self._show_point_context_menu)

        self.point_context_menu = tk.Menu(self.root, tearoff=0)
        self.point_context_menu.add_command(label="編輯此點位", command=self.edit_selected_point)
        self.point_context_menu.add_command(label="刪除此點位", command=self.delete_selected_point)

        self.btn_frame = ttk.Frame(self.right_frame)
        self.btn_frame.pack(fill=tk.X, padx=5, pady=10)

        tk.Button(
            self.btn_frame,
            text="同步目前攝影機點位",
            command=self.sync_current_camera_to_server,
            **action_button_options(self.font_family, primary=True, height=2),
        ).pack(fill=tk.X, pady=2)

        tk.Button(
            self.btn_frame,
            text="重製所有點位",
            command=self.reset_all_points,
            bg="#c0392b",
            fg="white",
            height=2,
        ).pack(fill=tk.X, pady=2)

        ttk.Separator(self.btn_frame).pack(fill=tk.X, pady=8)

        tk.Button(
            self.btn_frame,
            text="產生外部版資料",
            command=self.open_standalone_export_dialog,
            bg="#34495e",
            fg="white",
            height=2,
        ).pack(fill=tk.X, pady=2)

        external_import_row = ttk.Frame(self.btn_frame)
        external_import_row.pack(fill=tk.X, pady=2)
        external_import_row.grid_columnconfigure(
            0,
            weight=1,
            uniform="external_import_actions",
        )
        external_import_row.grid_columnconfigure(
            1,
            weight=1,
            uniform="external_import_actions",
        )
        tk.Button(
            external_import_row,
            text="批次同步本次匯入",
            command=self.open_batch_sync_imported_cameras,
            **action_button_options(self.font_family, primary=True, height=2),
        ).grid(row=0, column=0, sticky=tk.EW, padx=(0, 2))
        tk.Button(
            external_import_row,
            text="匯入外部版結果",
            command=self.open_standalone_import_dialog,
            **action_button_options(
                self.font_family,
                primary=True,
                height=2,
                bg="#16a085",
                fg="#ffffff",
                activebackground="#1abc9c",
                activeforeground="#ffffff",
            ),
        ).grid(row=0, column=1, sticky=tk.EW, padx=(2, 0))

    def _reload_current_camera(self) -> None:
        if self.current_camera is None:
            return
        self._load_camera_image(self.current_camera, force_capture=False)

    def _capture_current_camera(self) -> None:
        if self.current_camera is None:
            return
        self._load_camera_image(self.current_camera, force_capture=True)

    def _place_dialog(
        self,
        dialog: tk.Toplevel,
        width: int,
        height: int,
        canvas_x: int | None = None,
        canvas_y: int | None = None,
    ) -> None:
        self.root.update_idletasks()
        margin = 20

        if canvas_x is not None and canvas_y is not None:
            x = self.canvas.winfo_rootx() + canvas_x + 18
            y = self.canvas.winfo_rooty() + canvas_y + 18
        else:
            root_width = max(1, self.root.winfo_width())
            root_height = max(1, self.root.winfo_height())
            x = self.root.winfo_rootx() + (root_width - width) // 2
            y = self.root.winfo_rooty() + (root_height - height) // 2

        max_x = max(margin, dialog.winfo_screenwidth() - width - margin)
        max_y = max(margin, dialog.winfo_screenheight() - height - margin)
        x = min(max(margin, x), max_x)
        y = min(max(margin, y), max_y)
        dialog.geometry(f"{width}x{height}+{x}+{y}")

    def _on_camera_selected(self) -> None:
        selected_label = self.camera_var.get()
        if not selected_label:
            return

        selected_camera = self.catalog.get_by_label(selected_label)
        if selected_camera is None:
            return

        if self.current_camera and self.current_camera.get("ip") == selected_camera.get("ip"):
            return

        if self.tree.get_children():
            confirmed = messagebox.askyesno("切換攝影機", "切換攝影機會清除目前未儲存的點位，是否繼續？")
            if not confirmed:
                if self.current_camera is not None:
                    current_label = self.catalog.label_for(self.current_camera)
                    self.camera_var.set(current_label)
                return

        self.current_camera = selected_camera
        self._clear_points()
        self._load_saved_points_for_camera(selected_camera)
        self._load_camera_image(selected_camera, force_capture=False)

    def _load_camera_image(self, camera_info: dict, force_capture: bool) -> None:
        self.capture_request_id += 1
        request_id = self.capture_request_id
        self.image_state.clear()
        self.tk_image = None

        camera_label = self.catalog.label_for(camera_info)
        self.info_var.set(f"{camera_label} | 圖片載入中")

        if not force_capture:
            image_path, source_text = utils.resolve_existing_image(camera_info, self.paths.images_dir)
            if image_path is not None and source_text is not None:
                self._display_image_file(image_path, request_id, source_text)
                return

        self.loading_text = "擷取圖片中"
        self._render_canvas()
        threading.Thread(
            target=self._capture_image_in_background,
            args=(camera_info, request_id),
            daemon=True,
        ).start()

    def _capture_image_in_background(self, camera_info: dict, request_id: int) -> None:
        try:
            image_path = utils.capture_and_cache_latest_image(camera_info, self.paths.images_dir)
            self.root.after(
                0,
                lambda: self._display_image_file(
                    image_path,
                    request_id,
                    source_text="即時擷取完成",
                ),
            )
        except Exception as exc:
            self.root.after(0, lambda: self._handle_capture_failure(request_id, str(exc)))

    def _handle_capture_failure(self, request_id: int, error_message: str) -> None:
        if request_id != self.capture_request_id:
            return

        self.loading_text = f"擷取失敗\n{error_message}"
        self.info_var.set(error_message)
        self._render_canvas()

    def _display_image_file(self, image_path: Path, request_id: int, source_text: str) -> None:
        if request_id != self.capture_request_id:
            return

        try:
            image_rgb = utils.read_image_rgb(image_path)
        except Exception as exc:
            self._handle_capture_failure(request_id, f"圖片讀取失敗: {exc}")
            return

        self.image_state.set_image(image_path, image_rgb)
        self.loading_text = ""

        camera_label = self.catalog.label_for(self.current_camera)
        self.info_var.set(f"{camera_label} | {source_text} | {image_path.name}")
        self._render_canvas()

    def _show_hint(self, message: str) -> None:
        self.image_state.clear()
        self.loading_text = message
        self._render_canvas()

    def _load_saved_points_for_camera(self, camera_info: dict) -> None:
        point_rows = utils.load_point_rows_from_camera(camera_info)
        for point_row in point_rows:
            self.tree.insert("", "end", values=point_row)
        self.point_index = len(point_rows)

    def _render_canvas(self) -> None:
        self.canvas.delete("all")

        canvas_width = max(1, self.canvas.winfo_width())
        canvas_height = max(1, self.canvas.winfo_height())

        if not self.image_state.has_image:
            fill = "#dddddd" if "\n" not in self.loading_text else "#ffcc66"
            self.canvas.create_text(
                canvas_width // 2,
                canvas_height // 2,
                text=self.loading_text or "[ 尚無圖片 ]",
                fill=fill,
                font=(self.font_family, 18),
                justify="center",
            )
            return

        pil_image, image_box = utils.resize_image_for_canvas(
            self.image_state.image_rgb,
            canvas_width,
            canvas_height,
        )
        self.tk_image = ImageTk.PhotoImage(pil_image)
        self.canvas.create_image(image_box["offset_x"], image_box["offset_y"], image=self.tk_image, anchor="nw")
        self.image_state.image_box = image_box

        for item_id in self.tree.get_children():
            point_name, _, _, image_x, image_y = self.tree.item(item_id)["values"]
            canvas_x, canvas_y = utils.CanvasGeometry.image_to_canvas_coords(
                self.image_state.image_box,
                float(image_x),
                float(image_y),
            )
            self.canvas.create_oval(canvas_x - 5, canvas_y - 5, canvas_x + 5, canvas_y + 5, outline="cyan", width=2)
            self.canvas.create_text(
                canvas_x,
                canvas_y - 15,
                text=point_name,
                fill="cyan",
                font=(self.font_family, 12, "bold"),
            )

    def _find_point_near_canvas(self, canvas_x: int, canvas_y: int, radius: int = 10) -> str | None:
        if self.image_state.image_box is None:
            return None

        radius_sq = radius * radius
        for item_id in self.tree.get_children():
            _, _, _, image_x, image_y = self.tree.item(item_id)["values"]
            point_x, point_y = utils.CanvasGeometry.image_to_canvas_coords(
                self.image_state.image_box,
                float(image_x),
                float(image_y),
            )
            if (point_x - canvas_x) ** 2 + (point_y - canvas_y) ** 2 <= radius_sq:
                return item_id
        return None

    def on_canvas_motion(self, event: tk.Event) -> None:
        if self._find_point_near_canvas(event.x, event.y):
            self.canvas.configure(cursor="fleur")
        else:
            self.canvas.configure(cursor="cross")

    def on_canvas_press(self, event: tk.Event) -> None:
        if not self.image_state.has_image or self.image_state.image_box is None:
            return

        item_id = self._find_point_near_canvas(event.x, event.y)
        if item_id:
            self.drag_item_id = item_id
            self.drag_started = False
            self.tree.selection_set(item_id)
            self.tree.focus(item_id)
            self.canvas.configure(cursor="fleur")
            return

        image_coords = utils.CanvasGeometry.canvas_to_image_coords(
            self.image_state.image_box,
            event.x,
            event.y,
        )
        if image_coords is None:
            return

        self._show_input_dialog(image_coords[0], image_coords[1], event.x, event.y)

    def on_canvas_drag(self, event: tk.Event) -> None:
        if not self.drag_item_id or self.image_state.image_box is None:
            return

        image_coords = utils.CanvasGeometry.canvas_to_image_coords(
            self.image_state.image_box,
            event.x,
            event.y,
        )
        if image_coords is None:
            return

        point_name, wgs84_x, wgs84_y, _, _ = self.tree.item(self.drag_item_id)["values"]
        self.tree.item(
            self.drag_item_id,
            values=(
                point_name,
                wgs84_x,
                wgs84_y,
                int(round(image_coords[0])),
                int(round(image_coords[1])),
            ),
        )
        self.drag_started = True
        self._render_canvas()

    def on_canvas_release(self, _event: tk.Event) -> None:
        self.drag_item_id = None
        self.drag_started = False
        self.canvas.configure(cursor="cross")

    def _show_input_dialog(
        self,
        image_x: float,
        image_y: float,
        canvas_x: int | None = None,
        canvas_y: int | None = None,
        item_id: str | None = None,
    ) -> None:
        is_editing = item_id is not None
        initial_name = utils.suggest_point_name(self.point_index)
        initial_wgs84_x = "120.654321"
        initial_wgs84_y = "23.456789"
        if is_editing:
            values = self.tree.item(item_id)["values"]
            initial_name = str(values[0])
            initial_wgs84_x = str(values[1])
            initial_wgs84_y = str(values[2])

        dialog = tk.Toplevel(self.root)
        dialog.title("編輯點位" if is_editing else "新增點位")
        dialog_width = 340
        dialog_height = 230 if canvas_x is not None and canvas_y is not None else 210
        self._place_dialog(dialog, dialog_width, dialog_height, canvas_x, canvas_y)
        dialog.resizable(False, False)
        dialog.transient(self.root)
        dialog.grab_set()

        title_text = "編輯既有點位" if is_editing else "新增座標點位"
        tk.Label(dialog, text=title_text, font=(self.font_family, 12, "bold")).pack(pady=(12, 4))

        position_text = f"圖片位置: ({int(round(image_x))}, {int(round(image_y))})"
        if canvas_x is not None and canvas_y is not None:
            position_text = f"畫布位置: ({canvas_x}, {canvas_y})    {position_text}"
        tk.Label(dialog, text=position_text, fg="#555555").pack(pady=(0, 8))

        form = tk.Frame(dialog)
        form.pack(pady=(0, 10))

        tk.Label(form, text="點位名稱:").grid(row=0, column=0, sticky="e")
        entry_name = tk.Entry(form, width=22)
        entry_name.insert(0, initial_name)
        entry_name.grid(row=0, column=1, padx=6, pady=4)

        tk.Label(form, text="WGS84 X:").grid(row=1, column=0, sticky="e")
        entry_x = tk.Entry(form, width=22)
        entry_x.insert(0, initial_wgs84_x)
        entry_x.grid(row=1, column=1, padx=6, pady=4)

        tk.Label(form, text="WGS84 Y:").grid(row=2, column=0, sticky="e")
        entry_y = tk.Entry(form, width=22)
        entry_y.insert(0, initial_wgs84_y)
        entry_y.grid(row=2, column=1, padx=6, pady=4)

        def save() -> None:
            point_name = entry_name.get().strip()
            if not point_name:
                messagebox.showerror("輸入錯誤", "請輸入點位名稱")
                return

            try:
                wgs84_x = float(entry_x.get())
                wgs84_y = float(entry_y.get())
            except ValueError:
                messagebox.showerror("輸入錯誤", "WGS84 座標必須是數字")
                return

            values = (
                point_name,
                wgs84_x,
                wgs84_y,
                int(round(image_x)),
                int(round(image_y)),
            )
            if is_editing:
                self.tree.item(item_id, values=values)
            else:
                self.tree.insert("", "end", values=values)
                self.point_index += 1
            self._render_canvas()
            dialog.destroy()

        button_row = tk.Frame(dialog)
        button_row.pack(pady=(0, 12))
        save_text = "更新" if is_editing else "新增"
        save_color = "#2980b9" if is_editing else "#4CAF50"
        tk.Button(button_row, text=save_text, command=save, bg=save_color, fg="white", width=8).pack(side=tk.LEFT, padx=5)
        tk.Button(button_row, text="取消", command=dialog.destroy, width=8).pack(side=tk.LEFT, padx=5)
        entry_name.focus_set()
        entry_name.select_range(0, tk.END)
        dialog.bind("<Return>", lambda _event: save())

    def _show_point_context_menu(self, event: tk.Event) -> None:
        item_id = self.tree.identify_row(event.y)
        if not item_id:
            return

        self.tree.selection_set(item_id)
        self.tree.focus(item_id)
        try:
            self.point_context_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.point_context_menu.grab_release()

    def _get_selected_point_item(self) -> str | None:
        selected_items = self.tree.selection()
        if not selected_items:
            messagebox.showwarning("尚未選擇點位", "請先在右側清單選擇一個點位")
            return None
        return selected_items[0]

    def edit_selected_point(self) -> None:
        item_id = self._get_selected_point_item()
        if item_id is None:
            return

        _, _, _, image_x, image_y = self.tree.item(item_id)["values"]
        self._show_input_dialog(
            float(image_x),
            float(image_y),
            item_id=item_id,
        )

    def delete_selected_point(self) -> None:
        item_id = self._get_selected_point_item()
        if item_id is None:
            return

        point_name = self.tree.item(item_id)["values"][0]
        confirmed = messagebox.askyesno(
            "刪除點位",
            f"確定要刪除點位「{point_name}」嗎？\n\n"
            "此操作只會影響目前清單，需按「同步目前攝影機點位」才會保存並同步。",
        )
        if not confirmed:
            return

        self.tree.delete(item_id)
        self._render_canvas()

    def _clear_points(self) -> None:
        for item_id in self.tree.get_children():
            self.tree.delete(item_id)
        self.point_index = 0
        self._render_canvas()

    def reset_all_points(self) -> None:
        self._clear_points()

    def _format_capture_camera_label(self, camera_info: dict) -> str:
        location = str(camera_info.get("location") or "未命名攝影機")
        channel = str(camera_info.get("NVR_channel") or "")
        return f"{location} | CH {channel}" if channel else location

    def _capture_missing_images_with_progress(
        self,
        parent: tk.Toplevel,
        missing_cameras: list[dict],
        on_complete: Callable[[list[tuple[dict, str]], bool], None],
    ) -> None:
        progress_dialog = tk.Toplevel(parent)
        progress_dialog.title("擷取參考圖片")
        self._place_dialog(progress_dialog, 560, 240)
        progress_dialog.resizable(False, False)
        progress_dialog.transient(parent)
        progress_dialog.grab_set()

        total_count = len(missing_cameras)
        status_var = tk.StringVar(value="準備擷取缺少的參考圖片")
        count_var = tk.StringVar(value=f"已完成 0 / {total_count}")
        ttk.Label(
            progress_dialog,
            text="目前沒有參考圖片的攝影機將依序擷取一張最新畫面。",
            wraplength=500,
        ).pack(anchor=tk.W, padx=16, pady=(16, 8))
        ttk.Label(progress_dialog, textvariable=status_var).pack(anchor=tk.W, padx=16, pady=(0, 6))
        progress_bar = ttk.Progressbar(
            progress_dialog,
            orient=tk.HORIZONTAL,
            mode="determinate",
            maximum=total_count,
        )
        progress_bar.pack(fill=tk.X, padx=16, pady=(0, 6))
        ttk.Label(progress_dialog, textvariable=count_var, foreground="#555555").pack(anchor=tk.W, padx=16)

        cancel_event = threading.Event()
        cancel_button = ttk.Button(progress_dialog, text="取消後續擷取")
        cancel_button.pack(anchor=tk.E, padx=16, pady=(12, 16))

        def request_cancel() -> None:
            if cancel_event.is_set():
                return
            cancel_event.set()
            cancel_button.configure(state=tk.DISABLED)
            status_var.set("正在停止，等待目前攝影機擷取結束…")

        cancel_button.configure(command=request_cancel)
        progress_dialog.protocol("WM_DELETE_WINDOW", request_cancel)

        def update_progress(index: int, camera_label: str) -> None:
            status_var.set(f"正在擷取：{camera_label}")
            count_var.set(f"已完成 {index - 1} / {total_count}")
            progress_bar.configure(value=index - 1)

        def update_completed(index: int) -> None:
            count_var.set(f"已完成 {index} / {total_count}")
            progress_bar.configure(value=index)

        def finish_progress(
            failed_cameras: list[tuple[dict, str]],
            cancelled: bool,
        ) -> None:
            if progress_dialog.winfo_exists():
                progress_dialog.grab_release()
                progress_dialog.destroy()
            try:
                parent.grab_set()
            except tk.TclError:
                pass
            on_complete(failed_cameras, cancelled)

        def capture_worker() -> None:
            failed_cameras = []
            cancelled = False

            for index, camera_info in enumerate(missing_cameras, start=1):
                if cancel_event.is_set():
                    cancelled = True
                    break

                camera_label = self._format_capture_camera_label(camera_info)
                self.root.after(0, lambda i=index, label=camera_label: update_progress(i, label))
                try:
                    utils.capture_and_cache_latest_image(
                        camera_info,
                        self.paths.images_dir,
                    )
                except Exception as exc:
                    failed_cameras.append((camera_info, str(exc)))

                self.root.after(0, lambda i=index: update_completed(i))

            if cancel_event.is_set():
                cancelled = True

            self.root.after(
                0,
                lambda: finish_progress(failed_cameras, cancelled),
            )

            threading.Thread(target=capture_worker, daemon=True).start()

    def _get_current_camera_id(self) -> str | None:
        if self.current_camera is None:
            return None
        try:
            return standalone_package.camera_id_from_camera(self.current_camera)
        except ValueError:
            return None

    def _build_current_geo_reference_payload(self) -> dict[str, Any]:
        if self.current_camera is None:
            raise ValueError("請先選擇攝影機")
        if not self.image_state.has_image or self.image_state.image_path is None:
            raise ValueError("請先載入目前攝影機圖片")

        point_rows = [
            tuple(self.tree.item(item_id)["values"])
            for item_id in self.tree.get_children()
        ]
        return utils.build_export_payload(self.image_state.image_path, point_rows)

    def _save_current_camera_locally(self) -> dict[str, Any]:
        if self.current_camera is None:
            raise ValueError("請先選擇攝影機")

        geo_reference_payload = self._build_current_geo_reference_payload()
        camera_info = dict(self.current_camera)
        camera_ip = str(camera_info.get("ip") or "").strip()
        if not camera_ip:
            raise ValueError("目前攝影機設定缺少 ip")

        utils.update_camera_geo_reference(
            self.paths.config_path,
            camera_ip,
            geo_reference_payload,
        )
        camera_id = self._get_current_camera_id()
        if camera_id:
            utils.mark_coordinate_cameras_locally_saved(
                self.paths.config_path,
                {camera_id},
                checked=True,
            )
        self.current_camera[utils.GEO_REFERENCE_KEY] = geo_reference_payload
        return geo_reference_payload

    def sync_current_camera_to_server(self) -> None:
        if self.current_camera is None:
            messagebox.showwarning("尚未選擇攝影機", "請先選擇攝影機")
            return

        try:
            geo_reference_payload = self._build_current_geo_reference_payload()
        except Exception as exc:
            messagebox.showerror("無法同步", str(exc), parent=self.root)
            return

        camera_info = dict(self.current_camera)
        camera_id = self._get_current_camera_id()
        camera_label = self.catalog.label_for(self.current_camera)

        dialog = tk.Toplevel(self.root)
        dialog.title("同步攝影機點位")
        self._place_dialog(dialog, 520, 180)
        dialog.resizable(False, False)
        dialog.transient(self.root)
        dialog.grab_set()

        ttk.Label(
            dialog,
            text=f"正在同步目前攝影機：{camera_label}",
            font=(self.font_family, 12, "bold"),
        ).pack(anchor=tk.W, padx=16, pady=(18, 8))
        ttk.Label(dialog, text="同步中，請勿關閉視窗…", wraplength=480).pack(
            anchor=tk.W,
            padx=16,
        )
        progress = ttk.Progressbar(dialog, mode="indeterminate")
        progress.pack(fill=tk.X, padx=16, pady=16)
        progress.start(12)

        sync_running = True

        def close_dialog() -> None:
            if sync_running:
                messagebox.showinfo(
                    "同步處理中",
                    "同步尚未完成，請等待伺服器回應。",
                    parent=dialog,
                )
                return
            dialog.destroy()

        dialog.protocol("WM_DELETE_WINDOW", close_dialog)

        def finish_single_sync(
            error: Exception | None,
            failure_stage: str | None = None,
        ) -> None:
            nonlocal sync_running
            if not dialog.winfo_exists():
                return
            sync_running = False
            progress.stop()
            dialog.destroy()
            if failure_stage != "local":
                self.current_camera[utils.GEO_REFERENCE_KEY] = geo_reference_payload

            if error is not None:
                if failure_stage == "local":
                    messagebox.showerror(
                        "同步前保存失敗",
                        f"同步前的本機設定保存流程失敗，因此未呼叫伺服器。\n\n原因：{error}",
                        parent=self.root,
                    )
                else:
                    failure_label = (
                        "伺服器連線失敗"
                        if isinstance(error, ConnectionError)
                        else "HTTP／伺服器拒絕"
                    )
                    messagebox.showwarning(
                        "本機已保存，伺服器未同步",
                        f"本機設定已保存，但{failure_label}。\n\n"
                        f"原因：{error}\n\n可修正後重新同步。",
                        parent=self.root,
                    )
                return

            state_error = record_single_sync_state_after_remote_success(
                self.paths.config_path,
                camera_id,
            )
            if state_error is not None:
                messagebox.showwarning(
                    "同步成功但狀態記錄失敗",
                    "伺服器已接受目前攝影機更新，但本機同步狀態寫入失敗。\n\n"
                    f"原因：{state_error}\n\n請稍後重新確認同步狀態。",
                    parent=self.root,
                )
                return

            messagebox.showinfo(
                "同步完成",
                "目前攝影機點位已同步到伺服器。",
                parent=self.root,
            )

        def local_saver(
            config_path: str | Path,
            camera: dict[str, Any],
            payload: dict[str, Any],
        ) -> None:
            utils.update_camera_geo_reference(
                config_path,
                str(camera.get("ip") or "").strip(),
                payload,
            )
            if camera_id:
                utils.mark_coordinate_cameras_locally_saved(
                    config_path,
                    {camera_id},
                    checked=True,
                )

        def worker() -> None:
            try:
                utils.save_then_submit_geo_reference_update(
                    self.config,
                    self.paths.config_path,
                    camera_info,
                    geo_reference_payload,
                    local_saver=local_saver,
                )
            except utils.GeoReferenceSingleRemoteError as exc:
                self.root.after(
                    0,
                    lambda error=exc.cause: finish_single_sync(
                        error,
                        "remote",
                    ),
                )
            except Exception as exc:
                self.root.after(
                    0,
                    lambda error=exc: finish_single_sync(
                        error,
                        "local",
                    ),
                )
            else:
                self.root.after(
                    0,
                    lambda: finish_single_sync(None),
                )

        threading.Thread(target=worker, name="coordinate-single-sync", daemon=True).start()

    def open_batch_sync_imported_cameras(self) -> None:
        try:
            state = utils.load_coordinate_import_state(self.paths.config_path)
            imported_ids = set(state["imported_camera_ids"])
            preview_rows = prepare_local_batch_sync(
                self._get_current_camera_id(),
                imported_ids,
                self._save_current_camera_locally,
                lambda: utils.build_local_geo_reference_batch_preview(
                    self.config,
                    self.paths.config_path,
                    utils.load_coordinate_import_state(self.paths.config_path),
                ),
            )
        except Exception as exc:
            messagebox.showerror("批次同步準備失敗", str(exc), parent=self.root)
            return

        if not preview_rows:
            messagebox.showwarning(
                "沒有本次匯入資料",
                "尚未保存任何本次匯入攝影機的 pending state。",
                parent=self.root,
            )
            return

        self._open_local_batch_sync_dialog(preview_rows)

    def _open_local_batch_sync_dialog(self, preview_rows: list[dict[str, Any]]) -> None:
        dialog = tk.Toplevel(self.root)
        dialog.title("批次同步本次匯入攝影機")
        self._place_dialog(dialog, 980, 620)
        dialog.transient(self.root)
        dialog.grab_set()

        ttk.Label(
            dialog,
            text="只從本機最終 camera config 建立批次資料；未選取、不存在或 geo_reference 無效者不會送出",
            font=(self.font_family, 12, "bold"),
            wraplength=940,
        ).pack(anchor=tk.W, padx=12, pady=(12, 6))

        columns = (
            "selected",
            "status",
            "channel",
            "name",
            "pre_import_points",
            "points",
            "synced",
        )
        tree = ttk.Treeview(dialog, columns=columns, show="headings", selectmode="browse")
        for column, heading in (
            ("selected", "選取"),
            ("status", "狀態"),
            ("channel", "CH"),
            ("name", "名稱"),
            ("pre_import_points", "匯入前點位"),
            ("points", "目前點位"),
            ("synced", "同步狀態"),
        ):
            tree.heading(column, text=heading)
        tree.column("selected", width=65, anchor=tk.CENTER, stretch=tk.NO)
        tree.column("status", width=110, anchor=tk.CENTER)
        tree.column("channel", width=70, anchor=tk.CENTER)
        tree.column("name", width=360, anchor=tk.W)
        tree.column("pre_import_points", width=100, anchor=tk.CENTER)
        tree.column("points", width=85, anchor=tk.CENTER)
        tree.column("synced", width=95, anchor=tk.CENTER)
        tree.pack(fill=tk.BOTH, expand=True, padx=12, pady=(0, 8))

        selection_state = {
            row["camera_id"]: False
            for row in preview_rows
            if row["can_sync"]
        }
        item_to_camera_id: dict[str, str] = {}
        row_by_camera_id = {row["camera_id"]: row for row in preview_rows}
        for row in preview_rows:
            item_id = tree.insert("", "end", values=("☐", "", "", "", "", "", ""))
            item_to_camera_id[item_id] = row["camera_id"]

        summary_var = tk.StringVar()
        ttk.Label(dialog, textvariable=summary_var, foreground="#555555").pack(
            anchor=tk.W,
            padx=12,
        )

        running = False

        def selected_ids() -> set[str]:
            return {
                camera_id
                for camera_id, selected in selection_state.items()
                if selected
            }

        def refresh_tree() -> None:
            selected_camera_ids = selected_ids()
            selected_point_count = sum(
                row_by_camera_id[camera_id]["point_count"]
                for camera_id in selected_camera_ids
            )
            eligible_count = sum(1 for row in preview_rows if row["can_sync"])
            synced_count = sum(1 for row in preview_rows if row["synced"])
            summary_var.set(
                f"本次匯入 {len(preview_rows)} 支；可同步 {eligible_count} 支；"
                f"已選 {len(selected_camera_ids)} 支／{selected_point_count} 點；"
                f"已同步 {synced_count} 支。"
            )

            for item_id, camera_id in item_to_camera_id.items():
                row = row_by_camera_id[camera_id]
                selected = selection_state.get(camera_id, False)
                if not row["exists"]:
                    status_text = "本機不存在"
                elif not row["valid_geo_reference"]:
                    status_text = "無有效設定"
                else:
                    status_text = "可同步"
                tree.item(
                    item_id,
                    values=(
                        "☑" if selected else ("☐" if row["can_sync"] else "—"),
                        status_text,
                        row["nvr_channel"],
                        row["display_name"],
                        (
                            row["pre_import_point_count"]
                            if row["pre_import_point_count"] is not None
                            else "未記錄"
                        ),
                        row["point_count"],
                        "已同步" if row["synced"] else "未同步",
                    ),
                )

        def toggle_selected_item(_event=None) -> None:
            selected_items = tree.selection()
            if not selected_items:
                return
            camera_id = item_to_camera_id[selected_items[0]]
            if camera_id not in selection_state:
                return
            selection_state[camera_id] = not selection_state[camera_id]
            refresh_tree()

        def set_all(value: bool) -> None:
            for camera_id in selection_state:
                selection_state[camera_id] = value
            refresh_tree()

        def invert_selection() -> None:
            for camera_id in selection_state:
                selection_state[camera_id] = not selection_state[camera_id]
            refresh_tree()

        def toggle_checkbox_click(event: tk.Event) -> None:
            if tree.identify_column(event.x) != "#1":
                return
            item_id = tree.identify_row(event.y)
            if not item_id:
                return
            tree.selection_set(item_id)
            tree.focus(item_id)
            camera_id = item_to_camera_id[item_id]
            if camera_id in selection_state:
                selection_state[camera_id] = not selection_state[camera_id]
                refresh_tree()
            return "break"

        tree.bind("<Double-1>", toggle_selected_item)
        tree.bind("<space>", toggle_selected_item)
        tree.bind("<Button-1>", toggle_checkbox_click)

        action_buttons: list[tk.Button] = []

        def set_running(value: bool) -> None:
            nonlocal running
            running = value
            state = tk.DISABLED if value else tk.NORMAL
            for button in action_buttons:
                button.configure(state=state)
            tree.state(["disabled"] if value else ["!disabled"])

        def close_dialog() -> None:
            if running:
                messagebox.showinfo(
                    "批次同步處理中",
                    "批次同步尚未完成，請等待結果後再關閉視窗。",
                    parent=dialog,
                )
                return
            dialog.destroy()

        dialog.protocol("WM_DELETE_WINDOW", close_dialog)

        def finish_batch(
            response_payload: dict[str, Any] | None,
            sent_count: int,
            sent_ids: set[str],
            error: Exception | None,
        ) -> None:
            if not dialog.winfo_exists():
                return
            set_running(False)
            if error is not None:
                if isinstance(error, utils.GeoReferenceBatchResponseError):
                    failure_label = "伺服器回應契約異常"
                elif isinstance(error, ConnectionError):
                    failure_label = "伺服器連線失敗"
                else:
                    failure_label = "HTTP／伺服器拒絕"
                summary_var.set(
                    f"伺服器未同步 {sent_count} 支：{failure_label}；可安全重試。"
                )
                messagebox.showwarning(
                    "伺服器未同步",
                    f"這次匯入的 {sent_count} 支設定仍保留在本機，但{failure_label}。\n\n"
                    f"原因：{error}\n\n可修正後重新同步。",
                    parent=dialog,
                )
                return

            remote_count = response_payload["data"]["updated_count"]
            state_error = persist_batch_sync_state_and_update_preview(
                self.paths.config_path,
                preview_rows,
                sent_ids,
            )
            if state_error is not None:
                summary_var.set(
                    f"伺服器已更新 {remote_count} 支，但同步狀態記錄失敗；"
                    "預覽未標記已同步。"
                )
                messagebox.showwarning(
                    "同步完成，但狀態記錄失敗",
                    f"伺服器已更新 {remote_count} 支，但 synced state 寫入失敗："
                    f"{state_error}\n\n預覽未標記已同步，請重新確認狀態。",
                    parent=dialog,
                )
                return

            refresh_tree()
            synced_count = sum(1 for row in preview_rows if row["synced"])
            summary_var.set(
                f"批次同步完成：伺服器更新 {remote_count} 支；"
                f"已同步 {synced_count} 支；"
                "distributed_config 派發已排入背景。"
            )
            messagebox.showinfo(
                "批次同步完成",
                f"伺服器已更新 {remote_count} 支，並只排入一次 distributed_config 派發。\n\n"
                "同步記錄已保留，可在下次開啟時追蹤。",
                parent=dialog,
            )

        def start_batch_sync() -> None:
            if running:
                return
            selected_camera_ids = selected_ids()
            if not selected_camera_ids:
                messagebox.showwarning(
                    "尚未選擇攝影機",
                    "請至少勾選一支可同步攝影機。",
                    parent=dialog,
                )
                return

            updates = utils.build_geo_reference_batch_updates_from_local_config(
                self.config,
                self.paths.config_path,
                selected_camera_ids,
            )
            sent_ids = {
                standalone_package.camera_id_from_channel(update["nvr_channel"])
                for update in updates
            }
            if not updates:
                messagebox.showwarning(
                    "沒有可同步資料",
                    "選取的攝影機必須存在於本機，且具有有效 geo_reference。",
                    parent=dialog,
                )
                return

            point_count = sum(
                len(update["geo_reference"].get("points", []))
                for update in updates
            )
            confirmed = messagebox.askyesno(
                "確認批次同步",
                f"將同步本次匯入的 {len(updates)} 支攝影機、{point_count} 個點位。\n\n"
                "批次資料會從本機最終 camera config 建立，全部驗證通過後才送出。是否繼續？",
                parent=dialog,
            )
            if not confirmed:
                return

            set_running(True)
            summary_var.set(
                f"處理中：正在同步本機最終設定（{len(updates)} 支／{point_count} 點）；"
                "請勿關閉視窗。"
            )

            def batch_runner() -> tuple[int, dict[str, Any]]:
                try:
                    response_payload = utils.submit_geo_reference_batch_update(
                        self.config,
                        updates,
                    )
                except Exception as exc:
                    raise utils.GeoReferenceBatchRemoteError(len(sent_ids), exc) from exc
                return len(sent_ids), response_payload

            def on_remote_failure(exc: utils.GeoReferenceBatchRemoteError) -> None:
                self.root.after(
                    0,
                    lambda error=exc.cause, count=exc.local_updated_count: finish_batch(
                        None,
                        count,
                        sent_ids,
                        error,
                    ),
                )

            def on_local_failure(exc: Exception) -> None:
                self.root.after(
                    0,
                    lambda error=exc: finish_batch(None, len(sent_ids), sent_ids, error),
                )

            def on_success(local_count: int, response_payload: dict[str, Any]) -> None:
                self.root.after(
                    0,
                    lambda payload=response_payload, count=local_count: finish_batch(
                        payload,
                        count,
                        sent_ids,
                        None,
                    ),
                )

            def worker() -> None:
                route_geo_reference_batch_worker_outcome(
                    batch_runner,
                    on_remote_failure=on_remote_failure,
                    on_local_failure=on_local_failure,
                    on_success=on_success,
                )

            threading.Thread(target=worker, name="coordinate-local-batch-sync", daemon=True).start()

        controls = ttk.Frame(dialog)
        controls.pack(fill=tk.X, padx=12, pady=12)
        for text, command in (
            ("全選可同步", lambda: set_all(True)),
            ("清除", lambda: set_all(False)),
            ("反選", invert_selection),
        ):
            button = tk.Button(
                controls,
                text=text,
                command=command,
                **action_button_options(self.font_family, primary=False),
            )
            button.pack(side=tk.LEFT, padx=(0, 8))
            action_buttons.append(button)

        cancel_button = tk.Button(
            controls,
            text="取消",
            command=close_dialog,
            **action_button_options(self.font_family, primary=False),
        )
        cancel_button.pack(side=tk.RIGHT, padx=(8, 0))
        action_buttons.append(cancel_button)
        sync_button = tk.Button(
            controls,
            text="批次同步本次匯入攝影機",
            command=start_batch_sync,
            **action_button_options(self.font_family, primary=True),
        )
        sync_button.pack(side=tk.RIGHT)
        action_buttons.append(sync_button)
        refresh_tree()

    def open_standalone_export_dialog(self) -> None:
        dialog = tk.Toplevel(self.root)
        dialog.title("產生外部版資料")
        self._place_dialog(dialog, 760, 560)
        dialog.transient(self.root)
        dialog.grab_set()

        ttk.Label(
            dialog,
            text="選擇要提供給外部版坐標編輯系統的攝影機",
            font=(self.font_family, 12, "bold"),
        ).pack(anchor=tk.W, padx=12, pady=(12, 6))

        columns = ("selected", "channel", "name", "points", "image")
        tree = ttk.Treeview(dialog, columns=columns, show="headings", selectmode="browse")
        tree.heading("selected", text="選取")
        tree.heading("channel", text="CH")
        tree.heading("name", text="名稱")
        tree.heading("points", text="既有點位")
        tree.heading("image", text="參考圖片")
        tree.column("selected", width=70, anchor=tk.CENTER, stretch=tk.NO)
        tree.column("channel", width=80, anchor=tk.CENTER)
        tree.column("name", width=330, anchor=tk.W)
        tree.column("points", width=90, anchor=tk.CENTER)
        tree.column("image", width=100, anchor=tk.CENTER)
        tree.pack(fill=tk.BOTH, expand=True, padx=12, pady=(0, 8))

        selection_state: dict[str, bool] = {}
        item_to_camera_id: dict[str, str] = {}
        camera_rows: dict[str, dict] = {}

        for camera in self.config.get("cameras", []):
            try:
                camera_id = standalone_package.camera_id_from_camera(camera)
            except ValueError:
                continue

            source_image, _ = utils.resolve_existing_image(camera, self.paths.images_dir)
            point_count = len((camera.get(utils.GEO_REFERENCE_KEY) or {}).get("points") or [])
            channel = str(camera.get("NVR_channel") or "")
            location = str(camera.get("location") or f"CH {channel}")
            has_image_text = "有" if source_image else "缺"
            selection_state[camera_id] = False
            camera_rows[camera_id] = {
                "channel": channel,
                "location": location,
                "point_count": point_count,
                "has_image_text": has_image_text,
            }
            item_id = tree.insert(
                "",
                "end",
                values=("☐", channel, location, point_count, has_image_text),
            )
            item_to_camera_id[item_id] = camera_id

        def refresh_tree() -> None:
            for item_id, camera_id in item_to_camera_id.items():
                row = camera_rows[camera_id]
                tree.item(
                    item_id,
                    values=(
                        "☑" if selection_state[camera_id] else "☐",
                        row["channel"],
                        row["location"],
                        row["point_count"],
                        row["has_image_text"],
                    ),
                )

        def toggle_selected_item(_event=None) -> None:
            selected_items = tree.selection()
            if not selected_items:
                return
            camera_id = item_to_camera_id[selected_items[0]]
            selection_state[camera_id] = not selection_state[camera_id]
            refresh_tree()

        def set_all(value: bool) -> None:
            for camera_id in selection_state:
                selection_state[camera_id] = value
            refresh_tree()

        def invert_selection() -> None:
            for camera_id in selection_state:
                selection_state[camera_id] = not selection_state[camera_id]
            refresh_tree()

        def complete_export(selected_camera_ids: set[str]) -> None:
            export_parent = filedialog.askdirectory(
                title="選擇外部版資料輸出位置",
                parent=dialog,
            )
            if not export_parent:
                return

            try:
                export_dir = utils.export_standalone_config_package(
                    self.config,
                    self.paths.images_dir,
                    export_parent,
                    selected_camera_ids,
                )
            except Exception as exc:
                messagebox.showerror("產生失敗", str(exc), parent=dialog)
                return

            messagebox.showinfo(
                "產生完成",
                "已產生可交付給外部人員的外部版資料夾：\n"
                f"{export_dir}\n\n"
                "外部人員可在資料夾內雙擊 external_coordinate_editor.exe 開啟。",
                parent=dialog,
            )
            dialog.destroy()

        def format_capture_failure_message(
            missing_cameras: list[dict],
            failure_messages: dict[str, str],
        ) -> str:
            lines = []
            for camera_info in missing_cameras:
                camera_id = standalone_package.camera_id_from_camera(camera_info)
                reason = failure_messages.get(camera_id, "擷取後仍找不到圖片")
                lines.append(f"{self._format_capture_camera_label(camera_info)}：{reason}")

            if len(lines) > 12:
                remaining_count = len(lines) - 12
                lines = lines[:12] + [f"…另有 {remaining_count} 支攝影機擷取失敗"]
            return "\n".join(lines)

        def finish_missing_image_capture(
            selected_camera_ids: set[str],
            failed_cameras: list[tuple[dict, str]],
            cancelled: bool,
        ) -> None:
            if cancelled:
                messagebox.showinfo(
                    "已取消擷取",
                    "已停止後續參考圖片擷取，這次不會產生外部版資料。",
                    parent=dialog,
                )
                return

            missing_after_capture = utils.find_cameras_missing_images(
                self.config,
                self.paths.images_dir,
                selected_camera_ids,
            )
            if not missing_after_capture:
                complete_export(selected_camera_ids)
                return

            failure_messages = {}
            for camera_info, error_message in failed_cameras:
                camera_id = standalone_package.camera_id_from_camera(camera_info)
                failure_messages[camera_id] = error_message

            failed_ids = {
                standalone_package.camera_id_from_camera(camera_info)
                for camera_info in missing_after_capture
            }
            exportable_ids = selected_camera_ids - failed_ids
            failure_text = format_capture_failure_message(missing_after_capture, failure_messages)

            if not exportable_ids:
                messagebox.showerror(
                    "擷取失敗",
                    "沒有任何攝影機取得參考圖片，無法產生外部版資料。\n\n"
                    f"失敗清單：\n{failure_text}",
                    parent=dialog,
                )
                return

            confirmed = messagebox.askyesno(
                "部分擷取失敗",
                f"共有 {len(exportable_ids)} 支攝影機已有圖片，{len(failed_ids)} 支仍然缺少圖片。\n\n"
                f"失敗清單：\n{failure_text}\n\n"
                "是否只匯出已有參考圖片的攝影機？",
                parent=dialog,
            )
            if confirmed:
                complete_export(exportable_ids)

        def generate_package() -> None:
            selected_camera_ids = {camera_id for camera_id, selected in selection_state.items() if selected}
            if not selected_camera_ids:
                messagebox.showwarning("尚未選擇攝影機", "請至少勾選一支攝影機", parent=dialog)
                return

            missing_cameras = utils.find_cameras_missing_images(
                self.config,
                self.paths.images_dir,
                selected_camera_ids,
            )
            if not missing_cameras:
                complete_export(selected_camera_ids)
                return

            confirmed = messagebox.askyesno(
                "缺少參考圖片",
                f"目前有 {len(missing_cameras)} 支攝影機沒有參考圖片。\n\n"
                "是否由目前工作站自動擷取一張最新畫面？",
                parent=dialog,
            )
            if not confirmed:
                return

            self._capture_missing_images_with_progress(
                dialog,
                missing_cameras,
                lambda failed, cancelled: finish_missing_image_capture(
                    selected_camera_ids,
                    failed,
                    cancelled,
                ),
            )

        tree.bind("<Double-1>", toggle_selected_item)
        tree.bind("<space>", toggle_selected_item)

        def toggle_checkbox_click(event: tk.Event) -> None:
            if tree.identify_column(event.x) != "#1":
                return
            item_id = tree.identify_row(event.y)
            if not item_id:
                return
            tree.selection_set(item_id)
            tree.focus(item_id)
            camera_id = item_to_camera_id[item_id]
            selection_state[camera_id] = not selection_state[camera_id]
            refresh_tree()
            return "break"

        tree.bind("<Button-1>", toggle_checkbox_click)

        button_row = ttk.Frame(dialog)
        button_row.pack(fill=tk.X, padx=12, pady=(0, 12))
        ttk.Button(button_row, text="全選", command=lambda: set_all(True)).pack(side=tk.LEFT)
        ttk.Button(button_row, text="清除", command=lambda: set_all(False)).pack(side=tk.LEFT, padx=(8, 0))
        ttk.Button(button_row, text="反選", command=invert_selection).pack(side=tk.LEFT, padx=(8, 0))
        tk.Button(
            button_row,
            text="產生外部版",
            command=generate_package,
            bg="#34495e",
            fg="white",
            width=12,
        ).pack(side=tk.RIGHT)
        ttk.Button(button_row, text="取消", command=dialog.destroy).pack(side=tk.RIGHT, padx=(0, 8))

    def open_standalone_import_dialog(self) -> None:
        result_path = filedialog.askopenfilename(
            title="選擇外部版坐標結果",
            filetypes=[
                ("Coordinate result", standalone_package.RESULT_FILENAME),
                ("JSON files", "*.json"),
                ("All files", "*.*"),
            ],
            parent=self.root,
        )
        if not result_path:
            return

        try:
            result_cameras, preview_rows = utils.build_standalone_import_preview(
                self.paths.config_path,
                result_path,
            )
        except Exception as exc:
            messagebox.showerror("讀取失敗", str(exc))
            return

        dialog = tk.Toplevel(self.root)
        dialog.title("匯入外部版結果")
        self._place_dialog(dialog, 1120, 660)
        dialog.transient(self.root)
        dialog.grab_set()

        ttk.Label(
            dialog,
            text="選取要匯入的攝影機",
            font=(self.font_family, 12, "bold"),
            wraplength=1080,
        ).pack(anchor=tk.W, padx=12, pady=(12, 6))

        version_warning_var = tk.StringVar()
        tk.Label(
            dialog,
            textvariable=version_warning_var,
            font=(self.font_family, 10),
            fg="#b03a2e",
            anchor=tk.W,
            justify=tk.LEFT,
            wraplength=1080,
        ).pack(fill=tk.X, padx=12, pady=(0, 6))

        columns = (
            "selected",
            "status",
            "version",
            "channel",
            "name",
            "current_points",
            "import_points",
            "overwrite",
        )
        tree = ttk.Treeview(dialog, columns=columns, show="headings", selectmode="browse")
        for column, heading in (
            ("selected", "選取"),
            ("status", "狀態"),
            ("version", "版本比對"),
            ("channel", "CH"),
            ("name", "名稱"),
            ("current_points", "原點位"),
            ("import_points", "匯入點位"),
            ("overwrite", "覆蓋"),
        ):
            tree.heading(column, text=heading)
        tree.column("selected", width=70, anchor=tk.CENTER, stretch=tk.NO)
        tree.column("status", width=90, anchor=tk.CENTER)
        tree.column("version", width=120, anchor=tk.CENTER)
        tree.column("channel", width=80, anchor=tk.CENTER)
        tree.column("name", width=350, anchor=tk.W)
        tree.column("current_points", width=90, anchor=tk.CENTER)
        tree.column("import_points", width=90, anchor=tk.CENTER)
        tree.column("overwrite", width=90, anchor=tk.CENTER)
        tree.pack(fill=tk.BOTH, expand=True, padx=12, pady=(0, 8))

        selection_state: dict[str, bool] = {}
        item_to_camera_id: dict[str, str] = {}
        row_by_item: dict[str, dict[str, Any]] = {}
        snapshot_targets: list[dict[str, Any]] = []
        for row in preview_rows:
            camera_id = row["camera_id"]
            if row["exists"]:
                selection_state.setdefault(camera_id, False)
                if not row.get("baseline_available"):
                    row["version_check_status"] = "無法比對"
                    row["version_unverified"] = True
                elif not row.get("local_version_hash"):
                    row["version_check_status"] = "無法比對"
                    row["version_unverified"] = True
                    row["version_check_reason"] = row.get("version_check_reason") or "本機座標版本無法讀取"
                elif not row.get("camera_ip"):
                    row["version_check_status"] = "無法比對"
                    row["version_unverified"] = True
                    row["version_check_reason"] = "本機攝影機設定缺少 IP"
                else:
                    row["version_check_status"] = "檢查中"
                    snapshot_targets.append(
                        {
                            "camera_id": camera_id,
                            "camera_ip": row["camera_ip"],
                            "nvr_channel": row["nvr_channel"],
                        }
                    )
            else:
                row["version_check_status"] = "—"

            item_id = tree.insert("", "end", values=("", "", "", "", "", "", "", ""))
            item_to_camera_id[item_id] = camera_id
            row_by_item[item_id] = row

        version_detail = ttk.LabelFrame(dialog, text="選取攝影機版本日期")
        version_detail.pack(fill=tk.X, padx=12, pady=(0, 8))
        version_detail_title_var = tk.StringVar(value="請選取一列查看日期")
        version_detail_title = ttk.Label(
            version_detail,
            textvariable=version_detail_title_var,
            font=(self.font_family, 10, "bold"),
        )
        version_detail_title.pack(anchor=tk.W, padx=8, pady=(6, 4))
        version_date_vars = {
            key: tk.StringVar(value="未記錄")
            for key in ("baseline", "external", "local", "server")
        }
        version_date_grid = ttk.Frame(version_detail)
        for index, (key, label) in enumerate(
            (
                ("baseline", "匯出基準"),
                ("external", "外部版"),
                ("local", "本機"),
                ("server", "A40"),
            )
        ):
            cell = ttk.Frame(version_date_grid)
            cell.grid(
                row=index // 2,
                column=index % 2,
                sticky=tk.W,
                padx=(0, 16),
                pady=3,
            )
            ttk.Label(cell, text=f"{label}　", foreground="#555555").pack(side=tk.LEFT)
            ttk.Label(
                cell,
                textvariable=version_date_vars[key],
                font=(self.font_family, 10, "bold"),
            ).pack(side=tk.LEFT)

        def refresh_version_detail() -> None:
            selected_items = tree.selection()
            row = row_by_item.get(selected_items[0]) if selected_items else None
            if row is None:
                version_detail_title_var.set("請選取一列查看日期")
                version_detail_title.configure(foreground="#333333")
                version_date_grid.pack_forget()
                return

            if not row["exists"]:
                version_detail_title_var.set("本機找不到這支攝影機")
                version_detail_title.configure(foreground="#555555")
                version_date_grid.pack_forget()
                return

            if row["version_check_status"] == "一致":
                version_detail_title_var.set("版本一致")
                version_detail_title.configure(foreground="#277c5d")
                version_date_grid.pack_forget()
                return

            version_detail_title_var.set(
                f"CH {row['nvr_channel']}　{row['display_name']}　｜　"
                f"{row['version_check_status']}"
            )
            version_detail_title.configure(foreground="#333333")
            for key, value in coordinate_import_version_dates(row).items():
                version_date_vars[key].set(value)
            version_date_grid.pack(fill=tk.X, padx=8, pady=(0, 8))

        summary_var = tk.StringVar()
        ttk.Label(dialog, textvariable=summary_var, foreground="#555555").pack(
            anchor=tk.W,
            padx=12,
        )

        import_button: tk.Button | None = None
        version_check_complete = not snapshot_targets

        def get_selected_camera_ids() -> set[str]:
            return {
                camera_id
                for camera_id, selected in selection_state.items()
                if selected
            }

        def refresh_tree() -> None:
            selected_count = 0
            selected_point_count = 0
            for item_id, camera_id in item_to_camera_id.items():
                row = row_by_item[item_id]
                can_import = camera_id in selection_state
                selected = can_import and selection_state[camera_id]
                if selected:
                    selected_count += 1
                    selected_point_count += row["import_points"]
                tree.item(
                    item_id,
                    values=(
                        "☑" if selected else ("☐" if can_import else "—"),
                        "可匯入" if can_import else "找不到",
                        row["version_check_status"],
                        row["nvr_channel"],
                        row["display_name"],
                        row["current_points"],
                        row["import_points"],
                        "是" if row["will_overwrite"] else "否",
                    ),
                )

            missing_count = len(preview_rows) - len(selection_state)
            summary_var.set(
                f"可匯入 {len(selection_state)} 支；找不到 {missing_count} 支；"
                f"已選 {selected_count} 支／{selected_point_count} 點。"
            )
            if import_button is not None:
                import_button.configure(text=f"匯入本機設定（{selected_count} 支）")

        def finish_snapshot_check(
            snapshots: dict[str, dict[str, Any]],
            error_message: str | None,
        ) -> None:
            nonlocal version_check_complete
            if not dialog.winfo_exists():
                return

            for row in preview_rows:
                if row["version_check_status"] != "檢查中":
                    continue
                if error_message:
                    baseline_hash = row.get("baseline_version_hash")
                    local_hash = row.get("local_version_hash")
                    local_changed = bool(
                        baseline_hash and local_hash and local_hash != baseline_hash
                    )
                    row["local_changed_since_export"] = local_changed
                    row["version_alert"] = local_changed
                    row["version_check_status"] = "本機不同" if local_changed else "無法比對"
                    row["version_unverified"] = True
                    row["version_check_reason"] = error_message
                    continue

                snapshot = snapshots.get(row["camera_id"])
                if snapshot is None:
                    row["version_check_status"] = "無法比對"
                    row["version_unverified"] = True
                    row["version_check_reason"] = "伺服端未回傳此攝影機的版本"
                    continue

                server_geo_reference = snapshot.get("geo_reference")
                row["server_save_time"] = str(
                    (server_geo_reference or {}).get("save_time") or ""
                ) if isinstance(server_geo_reference, dict) else ""
                try:
                    server_version_hash = utils.geo_reference_version_hash(
                        server_geo_reference,
                        fallback_image_name=row.get("baseline_image_name"),
                    )
                except (TypeError, ValueError, OverflowError) as exc:
                    row["version_check_status"] = "無法比對"
                    row["version_unverified"] = True
                    row["version_check_reason"] = f"伺服端座標格式無效：{exc}"
                    continue

                baseline_version_hash = row.get("baseline_version_hash")
                local_version_hash = row.get("local_version_hash")
                if not baseline_version_hash or not local_version_hash:
                    row["version_check_status"] = "無法比對"
                    row["version_unverified"] = True
                    row["version_check_reason"] = "匯出基準或本機版本缺少有效座標資料"
                    continue

                local_changed = local_version_hash != baseline_version_hash
                server_changed = server_version_hash != baseline_version_hash
                row["server_version_hash"] = server_version_hash
                row["local_changed_since_export"] = local_changed
                row["server_changed_since_export"] = server_changed
                row["version_alert"] = local_changed or server_changed
                row["version_unverified"] = False
                if local_changed and server_changed:
                    row["version_check_status"] = "兩邊皆不同"
                elif local_changed:
                    row["version_check_status"] = "本機不同"
                elif server_changed:
                    row["version_check_status"] = "伺服端不同"
                else:
                    row["version_check_status"] = "一致"

            version_check_complete = True
            if import_button is not None:
                import_button.configure(state=tk.NORMAL)

            version_warning_var.set(coordinate_import_version_summary(preview_rows))
            refresh_tree()
            refresh_version_detail()

        def start_snapshot_check() -> None:
            if not snapshot_targets:
                finish_snapshot_check({}, None)
                return

            def snapshot_worker() -> None:
                try:
                    snapshots = utils.fetch_geo_reference_snapshots(
                        self.config,
                        snapshot_targets,
                    )
                except Exception as exc:
                    try:
                        self.root.after(
                            0,
                            lambda message=str(exc): finish_snapshot_check({}, message),
                        )
                    except tk.TclError:
                        pass
                    return

                try:
                    self.root.after(
                        0,
                        lambda result=snapshots: finish_snapshot_check(result, None),
                    )
                except tk.TclError:
                    pass

            threading.Thread(
                target=snapshot_worker,
                name="coordinate-import-version-check",
                daemon=True,
            ).start()

        def toggle_selected_item(_event=None) -> None:
            selected_items = tree.selection()
            if not selected_items:
                return
            camera_id = item_to_camera_id[selected_items[0]]
            if camera_id not in selection_state:
                return
            selection_state[camera_id] = not selection_state[camera_id]
            refresh_tree()

        def set_all(value: bool) -> None:
            for camera_id in selection_state:
                selection_state[camera_id] = value
            refresh_tree()

        def invert_selection() -> None:
            for camera_id in selection_state:
                selection_state[camera_id] = not selection_state[camera_id]
            refresh_tree()

        def toggle_checkbox_click(event: tk.Event) -> None:
            if tree.identify_column(event.x) != "#1":
                return
            item_id = tree.identify_row(event.y)
            if not item_id:
                return
            tree.selection_set(item_id)
            tree.focus(item_id)
            camera_id = item_to_camera_id[item_id]
            if camera_id in selection_state:
                selection_state[camera_id] = not selection_state[camera_id]
                refresh_tree()
            return "break"

        tree.bind("<Double-1>", toggle_selected_item)
        tree.bind("<space>", toggle_selected_item)
        tree.bind("<Button-1>", toggle_checkbox_click)
        tree.bind("<<TreeviewSelect>>", lambda _event: refresh_version_detail())
        initial_detail_item = next(
            (
                item_id
                for item_id, camera_id in item_to_camera_id.items()
                if camera_id in selection_state
            ),
            next(iter(item_to_camera_id), None),
        )
        if initial_detail_item:
            tree.selection_set(initial_detail_item)
            tree.focus(initial_detail_item)
        refresh_version_detail()

        def perform_import() -> None:
            if not version_check_complete:
                messagebox.showinfo(
                    "版本檢查中",
                    "正在查詢匯出基準與伺服端座標版本，完成後才能匯入。",
                    parent=dialog,
                )
                return

            selected_camera_ids = get_selected_camera_ids()
            if not selected_camera_ids:
                messagebox.showwarning(
                    "尚未選擇攝影機",
                    "請至少勾選一支要匯入的攝影機",
                    parent=dialog,
                )
                return

            import_point_count = sum(
                row["import_points"]
                for row in preview_rows
                if row["camera_id"] in selected_camera_ids
            )
            selected_rows = [
                row for row in preview_rows
                if row["camera_id"] in selected_camera_ids
            ]
            changed_rows = [row for row in selected_rows if row.get("version_alert")]
            unknown_rows = [
                row for row in selected_rows
                if row.get("version_unverified")
                or row["version_check_status"] in ("無法比對", "未檢查", "檢查中")
            ]
            confirm_message = (
                f"將把 {len(selected_camera_ids)} 支攝影機、{import_point_count} 個點位寫入本機設定。"
            )
            if changed_rows or unknown_rows:
                warning_rows = changed_rows + [
                    row for row in unknown_rows if row not in changed_rows
                ]
                details = []
                for row in warning_rows[:6]:
                    baseline_time = (
                        row.get("baseline_exported_at")
                        or row.get("baseline_save_time")
                        or "未記錄"
                    )
                    import_time = row.get("import_save_time") or "未記錄"
                    local_time = row.get("local_save_time") or "未記錄"
                    server_time = row.get("server_save_time") or "未記錄"
                    detail = (
                        f"CH {row['nvr_channel']} {row['display_name']}："
                        f"{row['version_check_status']}；匯出 {baseline_time}；"
                        f"外部版保存 {import_time}；"
                        f"本機保存 {local_time}；伺服端保存 {server_time}"
                    )
                    if row.get("version_check_reason"):
                        detail += f"；原因：{row['version_check_reason']}"
                    details.append(detail)
                if len(warning_rows) > len(details):
                    details.append(f"另有 {len(warning_rows) - len(details)} 支未列出。")

                warning_summary = []
                if changed_rows:
                    warning_summary.append(f"版本不同 {len(changed_rows)} 支")
                if unknown_rows:
                    warning_summary.append(f"無法比對 {len(unknown_rows)} 支")
                confirm_message += (
                    "\n\n版本檢查提示："
                    + "、".join(warning_summary)
                    + "。匯入將以外部結果覆蓋選定攝影機的本機座標。\n"
                    + "\n".join(details)
                )
            confirm_message += "\n\n不會呼叫 A40，也不會派發 distributed_config。是否繼續？"

            confirmed = messagebox.askyesno(
                "確認匯入本機",
                confirm_message,
                parent=dialog,
            )
            if not confirmed:
                return

            try:
                updated_count, _state = utils.apply_standalone_result_import_and_record_state(
                    self.paths.config_path,
                    result_cameras,
                    selected_camera_ids=selected_camera_ids,
                )
            except Exception as exc:
                messagebox.showerror("本機匯入失敗", str(exc), parent=dialog)
                return

            try:
                self._reload_config_after_local_import()
            except Exception as exc:
                messagebox.showwarning(
                    "匯入完成，但重新載入失敗",
                    f"已匯入 {updated_count} 支、{import_point_count} 個點位；"
                    f"重新載入工作站設定失敗：{exc}",
                    parent=dialog,
                )
                return

            messagebox.showinfo(
                "匯入完成",
                f"已匯入 {updated_count} 支攝影機、{import_point_count} 個點位到本機設定。"
                "\n請檢查點位，完成後按主畫面的「批次同步本次匯入」同步到伺服器。",
                parent=dialog,
            )
            dialog.destroy()

        button_row = ttk.Frame(dialog)
        button_row.pack(fill=tk.X, padx=12, pady=12)
        tk.Button(
            button_row,
            text="全選",
            command=lambda: set_all(True),
            **action_button_options(self.font_family, primary=False),
        ).pack(side=tk.LEFT)
        tk.Button(
            button_row,
            text="清除",
            command=lambda: set_all(False),
            **action_button_options(self.font_family, primary=False),
        ).pack(side=tk.LEFT, padx=(8, 0))
        tk.Button(
            button_row,
            text="反選",
            command=invert_selection,
            **action_button_options(self.font_family, primary=False),
        ).pack(side=tk.LEFT, padx=(8, 0))
        tk.Button(
            button_row,
            text="取消",
            command=dialog.destroy,
            **action_button_options(self.font_family, primary=False),
        ).pack(side=tk.RIGHT, padx=(8, 0))
        import_button = tk.Button(
            button_row,
            text="匯入本機設定（0 支）",
            command=perform_import,
            state=tk.NORMAL if version_check_complete else tk.DISABLED,
            **action_button_options(self.font_family, primary=True),
        )
        import_button.pack(side=tk.RIGHT, padx=(8, 0))
        refresh_tree()
        start_snapshot_check()

    def _reload_config_after_local_import(self) -> None:
        current_camera_id = None
        if self.current_camera is not None:
            try:
                current_camera_id = standalone_package.camera_id_from_camera(self.current_camera)
            except ValueError:
                current_camera_id = None

        self.config = utils.load_local_camera_config(self.paths.config_path)
        self.catalog = utils.CameraCatalog.from_config(self.config)
        self.camera_combo.configure(values=self.catalog.labels)

        if current_camera_id:
            for camera in self.catalog.cameras:
                try:
                    if standalone_package.camera_id_from_camera(camera) == current_camera_id:
                        self.current_camera = camera
                        self.camera_var.set(self.catalog.label_for(camera))
                        self._clear_points()
                        self._load_saved_points_for_camera(camera)
                        self._load_camera_image(camera, force_capture=False)
                        return
                except ValueError:
                    continue

        self.current_camera = None
        if self.catalog.labels:
            self.camera_combo.current(0)
            self._on_camera_selected()

    def save_all_to_json(self) -> None:
        self.sync_current_camera_to_server()


def main() -> None:
    root = tk.Tk()
    try:
        app = CameraCoordinateApp(root)
    except Exception as exc:
        messagebox.showerror("啟動失敗", str(exc))
        root.destroy()
        return

    root.mainloop()


if __name__ == "__main__":
    main()
