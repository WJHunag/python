"""Optional JSON metadata helpers for smoke detection JPEG files.

This module keeps the existing filename-based protocol intact. Callers can
attach or read a small JSON object from JPEG EXIF ImageDescription, and every
public helper is safe to disable or fail back to the legacy filename behavior.
"""

from __future__ import annotations

import io
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from PIL import Image


METADATA_SCHEMA = "smoke-image-metadata/v1"
IMAGE_DESCRIPTION_TAG = 270
ENV_FLAG = "SMOKE_IMAGE_METADATA"


def metadata_enabled() -> bool:
    return os.getenv(ENV_FLAG, "1").strip().lower() not in {"0", "false", "no", "off"}


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _clean_dict(data: Dict[str, Any]) -> Dict[str, Any]:
    return {key: value for key, value in data.items() if value is not None}


def normalize_ip_key(camera_ip: Optional[str]) -> Optional[str]:
    if not camera_ip:
        return None
    return str(camera_ip).strip().replace(".", "_")


def normalize_ip_dotted(camera_ip: Optional[str]) -> Optional[str]:
    if not camera_ip:
        return None
    return str(camera_ip).strip().replace("_", ".")


def build_source_metadata(
    *,
    camera_ip: str,
    filename: str,
    nvr_channel: Optional[int] = None,
    capture_time: Optional[str] = None,
    source: str = "camera",
    focus_time_min: Optional[int] = None,
    connection_type: Optional[str] = None,
) -> Dict[str, Any]:
    camera_ip_dotted = normalize_ip_dotted(camera_ip)
    camera_ip_key = normalize_ip_key(camera_ip)
    return _clean_dict(
        {
            "schema": METADATA_SCHEMA,
            "camera_ip": camera_ip_dotted,
            "camera_ip_key": camera_ip_key,
            "nvr_channel": int(nvr_channel) if nvr_channel is not None else None,
            "group_id": Path(filename).stem,
            "frame_role": "source",
            "capture_time": capture_time or _now_iso(),
            "source": source,
            "connection_type": connection_type,
            "focus_time_min": focus_time_min,
            "legacy_filename": filename,
        }
    )


def build_cropped_metadata(
    parent_metadata: Optional[Dict[str, Any]],
    *,
    filename: str,
    group_id: str,
    crop_index: int,
    total_cropped: int,
    bbox_xyxy: Any = None,
) -> Dict[str, Any]:
    base = dict(parent_metadata or {})
    base.update(
        _clean_dict(
            {
                "schema": METADATA_SCHEMA,
                "group_id": group_id,
                "frame_role": "cropped",
                "crop_index": int(crop_index),
                "total_cropped": int(total_cropped),
                "bbox_xyxy": bbox_xyxy,
                "legacy_filename": filename,
                "created_at": _now_iso(),
            }
        )
    )
    return base


def build_with_boxes_metadata(
    parent_metadata: Optional[Dict[str, Any]],
    *,
    filename: str,
    group_id: str,
    detection_time: Optional[str] = None,
    boxes: Any = None,
) -> Dict[str, Any]:
    base = dict(parent_metadata or {})
    base.update(
        _clean_dict(
            {
                "schema": METADATA_SCHEMA,
                "group_id": group_id,
                "frame_role": "with_boxes",
                "detection_time": detection_time or _now_iso(),
                "boxes": boxes,
                "legacy_filename": filename,
            }
        )
    )
    return base

def build_marked_metadata(
    parent_metadata: Optional[Dict[str, Any]],
    *,
    filename: str,
    group_id: str,
    fixed_point_info: Any = None,
    marked_time: Optional[str] = None,
) -> Dict[str, Any]:
    """
    建立「最終標記原圖 (marked)」的 metadata，將包含 AI 檢測點與固定點計算結果寫入。
    """
    base = dict(parent_metadata or {})
    base.update(
        _clean_dict(
            {
                "schema": METADATA_SCHEMA,
                "group_id": group_id,
                "frame_role": "marked",               # 標示此圖片角色為合成標記圖
                "fixed_point_info": fixed_point_info, # 寫入剛剛計算出來的出煙點與固定點關係字典
                "marked_time": marked_time or _now_iso(),
                "legacy_filename": filename,
            }
        )
    )
    return base

def _metadata_to_json(metadata: Dict[str, Any]) -> str:
    payload = dict(metadata or {})
    payload.setdefault("schema", METADATA_SCHEMA)
    return json.dumps(payload, ensure_ascii=True, separators=(",", ":"))


def attach_metadata_to_jpeg_bytes(jpeg_bytes: bytes, metadata: Dict[str, Any]) -> bytes:
    with Image.open(io.BytesIO(jpeg_bytes)) as image:
        exif = image.getexif()
        exif[IMAGE_DESCRIPTION_TAG] = _metadata_to_json(metadata)
        out = io.BytesIO()
        try:
            image.save(out, format="JPEG", exif=exif.tobytes(), quality="keep")
        except Exception:
            image.save(out, format="JPEG", exif=exif.tobytes(), quality=90)
        return out.getvalue()


def maybe_attach_metadata_to_jpeg_bytes(jpeg_bytes: bytes, metadata: Dict[str, Any]) -> bytes:
    if not metadata_enabled():
        return jpeg_bytes
    try:
        return attach_metadata_to_jpeg_bytes(jpeg_bytes, metadata)
    except Exception:
        return jpeg_bytes


def attach_metadata_to_jpeg_file(image_path: str | os.PathLike[str], metadata: Dict[str, Any]) -> bool:
    path = Path(image_path)
    updated = attach_metadata_to_jpeg_bytes(path.read_bytes(), metadata)
    path.write_bytes(updated)
    return True


def maybe_attach_metadata_to_jpeg_file(image_path: str | os.PathLike[str], metadata: Dict[str, Any]) -> bool:
    if not metadata_enabled():
        return False
    try:
        return attach_metadata_to_jpeg_file(image_path, metadata)
    except Exception:
        return False


def read_metadata_from_jpeg_bytes(jpeg_bytes: bytes) -> Optional[Dict[str, Any]]:
    with Image.open(io.BytesIO(jpeg_bytes)) as image:
        raw = image.getexif().get(IMAGE_DESCRIPTION_TAG)
    if not raw:
        return None
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    data = json.loads(str(raw))
    if not isinstance(data, dict) or data.get("schema") != METADATA_SCHEMA:
        return None
    return data


def safe_read_metadata_from_jpeg_bytes(jpeg_bytes: bytes) -> Optional[Dict[str, Any]]:
    if not metadata_enabled():
        return None
    try:
        return read_metadata_from_jpeg_bytes(jpeg_bytes)
    except Exception:
        return None


def read_metadata_from_jpeg_file(image_path: str | os.PathLike[str]) -> Optional[Dict[str, Any]]:
    return read_metadata_from_jpeg_bytes(Path(image_path).read_bytes())


def safe_read_metadata_from_jpeg_file(image_path: str | os.PathLike[str]) -> Optional[Dict[str, Any]]:
    if not metadata_enabled():
        return None
    try:
        return read_metadata_from_jpeg_file(image_path)
    except Exception:
        return None


def parse_legacy_filename(filename: str) -> Dict[str, Any]:
    stem = Path(filename).stem
    parts = stem.split("_")
    camera_ip_key = "_".join(parts[:4]) if len(parts) >= 4 else None
    camera_ip = normalize_ip_dotted(camera_ip_key)
    frame_role = "cropped" if "cropped" in parts else "with_boxes" if "with" in parts and "boxes" in parts else "source"
    group_id = "_".join(parts[:5]) if len(parts) >= 5 else stem
    focus_time_min = None
    match = re.search(r"_focus(\d+)min", stem)
    if match:
        focus_time_min = int(match.group(1))
    return _clean_dict(
        {
            "camera_ip": camera_ip,
            "camera_ip_key": camera_ip_key,
            "group_id": group_id,
            "frame_role": frame_role,
            "focus_time_min": focus_time_min,
            "legacy_filename": filename,
        }
    )


def resolve_image_context(filename: str, metadata: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    legacy = parse_legacy_filename(filename)
    context = dict(legacy)
    if metadata:
        context.update(_clean_dict(metadata))
        if context.get("camera_ip") and not context.get("camera_ip_key"):
            context["camera_ip_key"] = normalize_ip_key(context["camera_ip"])
        if context.get("camera_ip_key") and not context.get("camera_ip"):
            context["camera_ip"] = normalize_ip_dotted(context["camera_ip_key"])
        context.setdefault("legacy_filename", filename)
    return context
