from __future__ import annotations

import sys
from pathlib import Path
import tkinter as tk
import tkinter.font as tkfont
from tkinter import filedialog, messagebox, ttk

from PIL import ImageTk

SOURCE_APP_DIR = Path(__file__).resolve().parent
if getattr(sys, "frozen", False):
    APP_DIR = Path(sys.executable).resolve().parent
    PROJECT_ROOT_DIR = APP_DIR
    MODULE_DIR = Path(getattr(sys, "_MEIPASS", SOURCE_APP_DIR))
else:
    APP_DIR = SOURCE_APP_DIR
    PROJECT_ROOT_DIR = APP_DIR.parent
    MODULE_DIR = APP_DIR

for module_search_path in (MODULE_DIR, APP_DIR):
    module_search_path_text = str(module_search_path)
    if module_search_path_text not in sys.path:
        sys.path.insert(0, module_search_path_text)

import standalone_data
import standalone_utils as utils


class StandaloneCoordinateApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("座標編輯系統獨立版")
        self.root.geometry("1280x780")
        self.font_family = self._configure_fonts()

        self.config_path = self._resolve_config_path()
        self.package_dir = self.config_path.parent
        self.result_path = self.package_dir / standalone_data.RESULT_FILENAME
        self.images_dir = self.package_dir / standalone_data.IMAGES_DIRNAME

        self.cameras = self._load_cameras()
        if not self.cameras:
            raise ValueError("coordinate_config.json 內沒有可用的 cameras")

        self.labels = [self._format_camera_label(camera) for camera in self.cameras]
        self.camera_by_label = {self._format_camera_label(camera): camera for camera in self.cameras}
        self.current_camera = None
        self.image_state = utils.CanvasImageState()
        self.tk_image = None
        self.loading_text = ""
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

    def _resolve_config_path(self) -> Path:
        candidates = []

        direct_paths = [
            APP_DIR / standalone_data.CONFIG_FILENAME,
            PROJECT_ROOT_DIR / standalone_data.CONFIG_FILENAME,
        ]
        candidates.extend(path for path in direct_paths if utils.is_file(path))
        candidates.extend(
            path
            for path in sorted(PROJECT_ROOT_DIR.glob("coordinate_editor_standalone_export_*/" + standalone_data.CONFIG_FILENAME))
            if utils.is_file(path)
        )

        unique_candidates = []
        seen = set()
        for path in candidates:
            resolved = path.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            unique_candidates.append(path)

        if len(unique_candidates) == 1:
            return unique_candidates[0]
        if len(unique_candidates) > 1:
            selected_path = filedialog.askopenfilename(
                title="選擇座標編輯系統獨立版設定檔",
                initialdir=str(PROJECT_ROOT_DIR),
                filetypes=[("Coordinate config", standalone_data.CONFIG_FILENAME), ("JSON files", "*.json"), ("All files", "*.*")],
            )
            if selected_path:
                return Path(selected_path)

        raise ValueError(
            "找不到 coordinate_config.json。\n\n"
            "請確認設定檔位於以下其中一個位置：\n"
            "- coordinate_editor_standalone 資料夾內\n"
            "- 啟動檔同層資料夾內\n"
            "- coordinate_editor_standalone_export_* 資料夾內"
        )

    def _load_cameras(self) -> list[dict]:
        config_cameras = standalone_data.load_config(self.config_path)
        if not self.result_path.exists():
            return config_cameras

        result_cameras = standalone_data.load_result(self.result_path)
        result_by_id = {camera["camera_id"]: camera for camera in result_cameras}
        for camera in config_cameras:
            result_camera = result_by_id.get(camera["camera_id"])
            if result_camera:
                camera[standalone_data.GEO_REFERENCE_KEY] = result_camera[standalone_data.GEO_REFERENCE_KEY]
        return config_cameras

    def _format_camera_label(self, camera: dict) -> str:
        return f"{camera.get('display_name', '未命名攝影機')} | CH {camera.get('nvr_channel', '')}"

    def _build_layout(self) -> None:
        top_frame = ttk.Frame(self.root)
        top_frame.pack(fill=tk.X, padx=10, pady=(10, 6))

        ttk.Label(top_frame, text="選擇攝影機:").pack(side=tk.LEFT)
        self.camera_var = tk.StringVar()
        self.camera_combo = ttk.Combobox(
            top_frame,
            textvariable=self.camera_var,
            values=self.labels,
            state="readonly",
            width=55,
        )
        self.camera_combo.pack(side=tk.LEFT, padx=(8, 8))
        self.camera_combo.bind("<<ComboboxSelected>>", lambda _event: self._on_camera_selected())

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
            text="儲存座標結果",
            command=self.save_result,
            bg="#2c3e50",
            fg="white",
            height=2,
        ).pack(fill=tk.X, pady=2)

        tk.Button(
            self.btn_frame,
            text="重製目前點位",
            command=self.reset_all_points,
            bg="#c0392b",
            fg="white",
            height=2,
        ).pack(fill=tk.X, pady=2)

    def _on_camera_selected(self) -> None:
        selected_label = self.camera_var.get()
        if not selected_label:
            return

        selected_camera = self.camera_by_label.get(selected_label)
        if selected_camera is None:
            return

        if self.current_camera is selected_camera:
            return

        self._store_current_camera_points()
        self.current_camera = selected_camera
        self._clear_points(render=False)
        self._load_saved_points_for_camera(selected_camera)
        self._load_camera_image(selected_camera)

    def _load_camera_image(self, camera: dict) -> None:
        self.image_state.clear()
        self.tk_image = None

        image_name = str(camera.get("image_name") or "")
        image_path = self._resolve_image_path(image_name)
        if not image_name or not utils.is_file(image_path):
            self.info_var.set(f"{self._format_camera_label(camera)} | 找不到圖片：{image_name}")
            self._show_hint(f"找不到圖片\n{image_path}")
            return

        try:
            image_rgb = utils.read_image_rgb(image_path)
        except Exception as exc:
            self.info_var.set(f"圖片讀取失敗: {exc}")
            self._show_hint(f"圖片讀取失敗\n{exc}")
            return

        self.image_state.set_image(image_path, image_rgb)
        self.loading_text = ""
        self.info_var.set(f"{self._format_camera_label(camera)} | {image_path.name}")
        self._render_canvas()

    def _resolve_image_path(self, image_name: str) -> Path:
        candidates = [
            self.images_dir / image_name,
            self.package_dir / image_name,
            PROJECT_ROOT_DIR / standalone_data.IMAGES_DIRNAME / image_name,
            PROJECT_ROOT_DIR / image_name,
        ]
        candidates.extend(
            path
            for export_dir in sorted(PROJECT_ROOT_DIR.glob("coordinate_editor_standalone_export_*"))
            for path in (
                export_dir / standalone_data.IMAGES_DIRNAME / image_name,
                export_dir / image_name,
            )
        )

        seen = set()
        for path in candidates:
            resolved = path.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            if utils.is_file(path):
                return path

        return candidates[0]

    def _show_hint(self, message: str) -> None:
        self.image_state.clear()
        self.loading_text = message
        self._render_canvas()

    def _load_saved_points_for_camera(self, camera: dict) -> None:
        point_rows = utils.point_rows_from_geo_reference(camera.get(standalone_data.GEO_REFERENCE_KEY) or {})
        for point_row in point_rows:
            self.tree.insert("", "end", values=point_row)
        self.point_index = len(point_rows)

    def _store_current_camera_points(self) -> None:
        if self.current_camera is None:
            return
        image_name = str(self.current_camera.get("image_name") or "")
        point_rows = [tuple(self.tree.item(item_id)["values"]) for item_id in self.tree.get_children()]
        self.current_camera[standalone_data.GEO_REFERENCE_KEY] = utils.build_geo_reference(
            image_name,
            point_rows,
            standalone_data.now_text(),
        )

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
            values=(point_name, wgs84_x, wgs84_y, int(round(image_coords[0])), int(round(image_coords[1]))),
        )
        self.drag_started = True
        self._render_canvas()

    def on_canvas_release(self, _event: tk.Event) -> None:
        self.drag_item_id = None
        self.drag_started = False
        self.canvas.configure(cursor="cross")

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
        self._show_input_dialog(float(image_x), float(image_y), item_id=item_id)

    def delete_selected_point(self) -> None:
        item_id = self._get_selected_point_item()
        if item_id is None:
            return

        point_name = self.tree.item(item_id)["values"][0]
        confirmed = messagebox.askyesno("刪除點位", f"確定要刪除點位「{point_name}」嗎？")
        if not confirmed:
            return

        self.tree.delete(item_id)
        self._render_canvas()

    def _clear_points(self, render: bool = True) -> None:
        for item_id in self.tree.get_children():
            self.tree.delete(item_id)
        self.point_index = 0
        if render:
            self._render_canvas()

    def reset_all_points(self) -> None:
        self._clear_points()

    def save_result(self) -> None:
        self._store_current_camera_points()
        result_payload = standalone_data.build_result_payload(self.cameras)
        try:
            standalone_data.write_json(self.result_path, result_payload)
        except Exception as exc:
            messagebox.showerror("儲存失敗", str(exc))
            return
        messagebox.showinfo("儲存完成", f"已儲存座標結果：\n{self.result_path}")


def main() -> None:
    root = tk.Tk()
    try:
        StandaloneCoordinateApp(root)
    except Exception as exc:
        messagebox.showerror("啟動失敗", str(exc))
        root.destroy()
        return

    root.mainloop()


if __name__ == "__main__":
    main()
