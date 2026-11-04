from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image


IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}

try:
    RESAMPLE_LANCZOS = Image.Resampling.LANCZOS
except AttributeError:
    RESAMPLE_LANCZOS = Image.LANCZOS


@dataclass
class CanvasImageState:
    image_path: Path | None = None
    image_rgb: Image.Image | None = None
    image_box: dict[str, int] | None = None

    def clear(self) -> None:
        self.image_path = None
        self.image_rgb = None
        self.image_box = None

    def set_image(self, image_path: Path, image_rgb: Image.Image) -> None:
        self.image_path = image_path
        self.image_rgb = image_rgb
        self.image_box = None

    @property
    def has_image(self) -> bool:
        return self.image_path is not None and self.image_rgb is not None


def read_image_rgb(image_path: str | Path) -> Image.Image:
    image_path = Path(image_path)
    try:
        with Image.open(windows_read_path(image_path)) as image:
            return image.convert("RGB").copy()
    except Exception as exc:
        raise ValueError(f"無法解析圖片: {image_path}") from exc


def windows_read_path(path: str | Path) -> str:
    path = Path(path)
    if sys.platform != "win32":
        return str(path)

    path_text = str(path.resolve())
    if path_text.startswith("\\\\?\\"):
        return path_text
    if path_text.startswith("\\\\"):
        return "\\\\?\\UNC\\" + path_text[2:]
    return "\\\\?\\" + path_text


def is_file(path: str | Path) -> bool:
    return Path(windows_read_path(path)).is_file()


def resize_image_for_canvas(
    image_rgb: Image.Image,
    canvas_width: int,
    canvas_height: int,
) -> tuple[Image.Image, dict[str, int]]:
    image_width, image_height = image_rgb.size
    scale = min(canvas_width / image_width, canvas_height / image_height)
    display_width = max(1, int(image_width * scale))
    display_height = max(1, int(image_height * scale))
    offset_x = (canvas_width - display_width) // 2
    offset_y = (canvas_height - display_height) // 2

    image_box = {
        "offset_x": offset_x,
        "offset_y": offset_y,
        "display_width": display_width,
        "display_height": display_height,
        "image_width": image_width,
        "image_height": image_height,
    }
    pil_image = image_rgb.resize((display_width, display_height), RESAMPLE_LANCZOS)
    return pil_image, image_box


class CanvasGeometry:
    @staticmethod
    def image_to_canvas_coords(image_box: dict[str, int] | None, image_x: float, image_y: float) -> tuple[int, int]:
        if not image_box:
            return int(round(image_x)), int(round(image_y))

        scale_x = image_box["display_width"] / image_box["image_width"]
        scale_y = image_box["display_height"] / image_box["image_height"]
        canvas_x = image_box["offset_x"] + (image_x * scale_x)
        canvas_y = image_box["offset_y"] + (image_y * scale_y)
        return int(round(canvas_x)), int(round(canvas_y))

    @staticmethod
    def canvas_to_image_coords(
        image_box: dict[str, int] | None,
        canvas_x: int,
        canvas_y: int,
    ) -> tuple[float, float] | None:
        if not image_box:
            return None

        x0 = image_box["offset_x"]
        y0 = image_box["offset_y"]
        x1 = x0 + image_box["display_width"]
        y1 = y0 + image_box["display_height"]

        if not (x0 <= canvas_x <= x1 and y0 <= canvas_y <= y1):
            return None

        rel_x = (canvas_x - x0) / image_box["display_width"]
        rel_y = (canvas_y - y0) / image_box["display_height"]
        image_x = rel_x * image_box["image_width"]
        image_y = rel_y * image_box["image_height"]
        return image_x, image_y


def suggest_point_name(point_index: int) -> str:
    if 0 <= point_index < 26:
        return chr(ord("A") + point_index)
    return "P{0}".format(point_index + 1)


def point_rows_from_geo_reference(geo_reference: dict[str, Any]) -> list[tuple[str, float, float, int, int]]:
    rows = []
    for point in geo_reference.get("points") or []:
        point_name = str(point.get("point_name") or "")
        if not point_name:
            continue
        image_coord = point.get("image_coord") or {}
        wgs84_coords = point.get("wgs84_coords") or {}
        rows.append(
            (
                point_name,
                float(wgs84_coords.get("x", 0)),
                float(wgs84_coords.get("y", 0)),
                int(round(float(image_coord.get("x", 0)))),
                int(round(float(image_coord.get("y", 0)))),
            )
        )
    return rows


def build_geo_reference(image_name: str, point_rows: list[tuple[Any, Any, Any, Any, Any]], save_time: str) -> dict[str, Any]:
    points = []
    for point_name, wgs84_x, wgs84_y, image_x, image_y in point_rows:
        points.append(
            {
                "point_name": str(point_name),
                "image_coord": {
                    "x": int(round(float(image_x))),
                    "y": int(round(float(image_y))),
                },
                "wgs84_coords": {
                    "x": float(wgs84_x),
                    "y": float(wgs84_y),
                },
            }
        )

    return {
        "image_name": image_name,
        "save_time": save_time,
        "points": points,
    }
