from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any


CONFIG_FORMAT = "coordinate_editor_standalone_config"
RESULT_FORMAT = "coordinate_editor_standalone_result"
FORMAT_VERSION = 1

CONFIG_FILENAME = "coordinate_config.json"
RESULT_FILENAME = "coordinate_result.json"
IMAGES_DIRNAME = "images"
GEO_REFERENCE_KEY = "geo_reference"


def now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def read_json(path: str | Path) -> dict[str, Any]:
    path = Path(path)
    with path.open("r", encoding="utf-8") as fh:
        payload = json.load(fh)
    if not isinstance(payload, dict):
        raise ValueError(f"{path.name} 必須是 JSON object")
    return payload


def write_json(path: str | Path, payload: dict[str, Any]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8", newline="\n") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    tmp_path.replace(path)


def channel_from_camera_id(camera_id: str) -> int:
    prefix = "camera_channel_"
    if not isinstance(camera_id, str) or not camera_id.startswith(prefix):
        raise ValueError(f"無效的 camera_id: {camera_id}")
    try:
        return int(camera_id[len(prefix):])
    except ValueError as exc:
        raise ValueError(f"無效的 camera_id: {camera_id}") from exc


def normalize_geo_reference(payload: Any, image_name: str | None = None) -> dict[str, Any]:
    if not isinstance(payload, dict):
        payload = {}

    resolved_image_name = str(payload.get("image_name") or image_name or "").strip()
    save_time = str(payload.get("save_time") or "").strip()
    raw_points = payload.get("points") or []
    if not isinstance(raw_points, list):
        raise ValueError("座標資料的點位清單必須是陣列")

    points = []
    for index, point in enumerate(raw_points):
        if not isinstance(point, dict):
            raise ValueError(f"座標資料第 {index + 1} 筆點位必須是 object")
        point_name = str(point.get("point_name") or "").strip()
        if not point_name:
            continue

        image_coord = point.get("image_coord") or {}
        wgs84_coords = point.get("wgs84_coords") or {}
        if not isinstance(image_coord, dict) or not isinstance(wgs84_coords, dict):
            raise ValueError(f"座標資料第 {index + 1} 筆點位座標格式錯誤")

        points.append(
            {
                "point_name": point_name,
                "image_coord": {
                    "x": int(round(float(image_coord.get("x", 0)))),
                    "y": int(round(float(image_coord.get("y", 0)))),
                },
                "wgs84_coords": {
                    "x": float(wgs84_coords.get("x", 0)),
                    "y": float(wgs84_coords.get("y", 0)),
                },
            }
        )

    return {
        "image_name": resolved_image_name,
        "save_time": save_time,
        "points": points,
    }


def validate_standalone_payload(payload: dict[str, Any], expected_format: str) -> list[dict[str, Any]]:
    if payload.get("format") != expected_format:
        raise ValueError(f"格式錯誤，預期 {expected_format}")
    if int(payload.get("version", 0)) != FORMAT_VERSION:
        raise ValueError(f"版本錯誤，預期 version={FORMAT_VERSION}")

    cameras = payload.get("cameras")
    if not isinstance(cameras, list):
        raise ValueError("cameras 必須是陣列")

    normalized = []
    for index, camera in enumerate(cameras):
        if not isinstance(camera, dict):
            raise ValueError(f"cameras[{index}] 必須是 object")

        camera_id = str(camera.get("camera_id") or "").strip()
        channel = channel_from_camera_id(camera_id)
        image_name = str(camera.get("image_name") or "").strip()
        if not image_name:
            raise ValueError(f"{camera_id} 缺少 image_name")

        normalized.append(
            {
                "camera_id": camera_id,
                "nvr_channel": str(camera.get("nvr_channel") or channel),
                "display_name": str(camera.get("display_name") or f"CH {channel:03d}"),
                "image_name": image_name,
                GEO_REFERENCE_KEY: normalize_geo_reference(camera.get(GEO_REFERENCE_KEY), image_name=image_name),
            }
        )
    return normalized


def load_config(path: str | Path) -> list[dict[str, Any]]:
    return validate_standalone_payload(read_json(path), CONFIG_FORMAT)


def load_result(path: str | Path) -> list[dict[str, Any]]:
    return validate_standalone_payload(read_json(path), RESULT_FORMAT)


def build_result_payload(cameras: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "format": RESULT_FORMAT,
        "version": FORMAT_VERSION,
        "saved_at": now_text(),
        "cameras": cameras,
    }

