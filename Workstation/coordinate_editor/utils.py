from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import time
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import cv2
import numpy as np
from PIL import Image
import requests
import utility
import standalone_package


# =========================
# 基本常數
# =========================
GEO_REFERENCE_KEY = "geo_reference"
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}
EXTERNAL_COORDINATE_EDITOR_DIRNAME = "external_coordinate_editor"
EXTERNAL_COORDINATE_EDITOR_PACKAGE_PREFIX = "external_editor"
COORDINATE_IMPORT_STATE_FILENAME = ".coordinate_editor_import_state.json"
COORDINATE_IMPORT_STATE_VERSION = 1
EXTERNAL_COORDINATE_EDITOR_REQUIRED_FILES = (
    "external_coordinate_editor.exe",
    "_internal/_tkinter.pyd",
    "_internal/tcl86t.dll",
    "_internal/tk86t.dll",
)


class GeoReferenceBatchResponseError(RuntimeError):
    """The server returned HTTP success with an invalid batch response contract."""


class GeoReferenceBatchRemoteError(RuntimeError):
    """Remote batch sync failed after local import completed."""

    def __init__(self, local_updated_count: int, cause: Exception) -> None:
        self.local_updated_count = local_updated_count
        self.cause = cause
        super().__init__(str(cause))


class GeoReferenceSingleRemoteError(RuntimeError):
    """The single-camera remote update failed after local save completed."""

    def __init__(self, cause: Exception) -> None:
        self.cause = cause
        super().__init__(str(cause))

try:
    RESAMPLE_LANCZOS = Image.Resampling.LANCZOS
except AttributeError:
    RESAMPLE_LANCZOS = Image.LANCZOS


# =========================
# 路徑 / 設定
# =========================
@dataclass
class AppPaths:
    app_dir: Path
    project_root: Path
    images_dir: Path
    config_path: Path

    @classmethod
    def from_app_file(cls, app_file: str | Path) -> "AppPaths":
        app_dir = Path(app_file).resolve().parent
        project_root = app_dir.parent
        return cls(
            app_dir=app_dir,
            project_root=project_root,
            images_dir=project_root / "images",
            config_path=project_root / utility.DISTRIBUTED_CONFIG_DIR,
        )


def validate_external_coordinate_editor_package(package_dir: str | Path) -> dict[str, int]:
    """Validate the native runtime files required by the PyInstaller onedir package."""
    package_dir = Path(package_dir)
    required_paths = [package_dir / relative_path for relative_path in EXTERNAL_COORDINATE_EDITOR_REQUIRED_FILES]
    missing_paths = [
        str(path.relative_to(package_dir))
        for path in required_paths
        if not path.is_file()
    ]
    if missing_paths:
        raise FileNotFoundError(
            "外部版坐標編輯系統缺少必要的 PyInstaller 檔案：\n"
            + "\n".join(missing_paths)
        )

    files = [path for path in package_dir.rglob("*") if path.is_file()]
    pyd_files = [path for path in files if path.suffix.lower() == ".pyd"]
    if not pyd_files:
        raise FileNotFoundError(
            "外部版坐標編輯系統沒有找到任何 .pyd 原生擴充模組；"
            "請確認完整保留 _internal 資料夾。"
        )

    return {
        "file_count": len(files),
        "total_bytes": sum(path.stat().st_size for path in files),
        "pyd_count": len(pyd_files),
    }


def load_local_camera_config(config_path: str | Path) -> dict[str, Any]:
    config_path = Path(config_path)
    try:
        # 坐標編輯器需要管理所有工作站的攝影機；檢測端仍使用
        # utility.load_config() 的工作站分派過濾，不在這裡改動共用預設。
        config = utility.load_distributed_config(
            str(config_path),
            filter_for_workstation=False,
        )
    except FileNotFoundError as exc:
        raise ValueError(
            "找不到 distributed_config 設定資料夾。\n\n"
            f"請確認此路徑是否存在：\n{config_path}\n\n"
            "此資料夾需要包含 common_settings.json、workstation_assignments.json "
            "以及攝影機設定檔資料夾。"
        ) from exc
    except json.JSONDecodeError as exc:
        raise ValueError(
            "distributed_config 內的 JSON 格式錯誤。\n\n"
            "請檢查 common_settings.json、workstation_assignments.json "
            "或攝影機設定檔是否有少逗號、括號或引號。"
        ) from exc

    cameras = config.get("cameras")
    if not isinstance(cameras, list):
        raise ValueError("distributed_config 缺少 cameras 陣列")

    return config


def coordinate_import_state_path(config_path: str | Path) -> Path:
    return Path(config_path) / COORDINATE_IMPORT_STATE_FILENAME


def _empty_coordinate_import_state() -> dict[str, Any]:
    return {
        "version": COORDINATE_IMPORT_STATE_VERSION,
        "imported_camera_ids": [],
        "pre_import_point_counts": {},
        "locally_saved_camera_ids": [],
        "checked_camera_ids": [],
        "synced_camera_ids": [],
        "imported_at": "",
        "synced_at": "",
    }


def _sorted_camera_ids(camera_ids: Any) -> list[str]:
    if not isinstance(camera_ids, (list, tuple, set)):
        raise ValueError("camera IDs 必須是陣列或集合")

    normalized_ids = set()
    for camera_id in camera_ids:
        if not isinstance(camera_id, str) or not camera_id.strip():
            raise ValueError("camera ID 必須是非空字串")
        standalone_package.channel_from_camera_id(camera_id)
        normalized_ids.add(camera_id)

    return sorted(
        normalized_ids,
        key=lambda camera_id: standalone_package.channel_from_camera_id(camera_id),
    )


def _normalize_coordinate_import_state(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("coordinate editor import state 必須是 JSON object")

    version = payload.get("version", COORDINATE_IMPORT_STATE_VERSION)
    if version != COORDINATE_IMPORT_STATE_VERSION:
        raise ValueError(f"不支援的 coordinate editor import state 版本：{version}")

    imported_ids = _sorted_camera_ids(payload.get("imported_camera_ids", []))
    imported_id_set = set(imported_ids)
    raw_pre_import_point_counts = payload.get("pre_import_point_counts", {})
    if not isinstance(raw_pre_import_point_counts, dict):
        raise ValueError(
            "coordinate editor import state 匯入前點位欄位必須是 JSON object"
        )
    pre_import_point_counts = {}
    for camera_id in _sorted_camera_ids(list(raw_pre_import_point_counts)):
        point_count = raw_pre_import_point_counts[camera_id]
        if (
            isinstance(point_count, bool)
            or not isinstance(point_count, int)
            or point_count < 0
        ):
            raise ValueError(
                "coordinate editor import state 匯入前點位數必須是非負整數"
            )
        if camera_id in imported_id_set:
            pre_import_point_counts[camera_id] = point_count

    locally_saved_ids = [
        camera_id
        for camera_id in _sorted_camera_ids(payload.get("locally_saved_camera_ids", []))
        if camera_id in imported_id_set
    ]
    checked_ids = [
        camera_id
        for camera_id in _sorted_camera_ids(payload.get("checked_camera_ids", []))
        if camera_id in imported_id_set
    ]
    synced_ids = [
        camera_id
        for camera_id in _sorted_camera_ids(payload.get("synced_camera_ids", []))
        if camera_id in imported_id_set
    ]

    imported_at = payload.get("imported_at") or ""
    synced_at = payload.get("synced_at") or ""
    if not isinstance(imported_at, str) or not isinstance(synced_at, str):
        raise ValueError("coordinate editor import state 時間欄位必須是字串")

    return {
        "version": COORDINATE_IMPORT_STATE_VERSION,
        "imported_camera_ids": imported_ids,
        "pre_import_point_counts": pre_import_point_counts,
        "locally_saved_camera_ids": locally_saved_ids,
        "checked_camera_ids": checked_ids,
        "synced_camera_ids": synced_ids,
        "imported_at": imported_at,
        "synced_at": synced_at,
    }


def _atomic_write_coordinate_import_state(
    config_path: str | Path,
    state: dict[str, Any],
) -> dict[str, Any]:
    state_path = coordinate_import_state_path(config_path)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{state_path.name}.",
        suffix=".tmp",
        dir=str(state_path.parent),
    )
    try:
        with os.fdopen(file_descriptor, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(state, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temporary_name, state_path)
    finally:
        if os.path.exists(temporary_name):
            try:
                os.remove(temporary_name)
            except OSError:
                pass

    return state


def load_coordinate_import_state(config_path: str | Path) -> dict[str, Any]:
    state_path = coordinate_import_state_path(config_path)
    if not state_path.is_file():
        return _empty_coordinate_import_state()

    try:
        with state_path.open("r", encoding="utf-8") as fh:
            payload = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"無法讀取 coordinate editor import state：{state_path}") from exc

    return _normalize_coordinate_import_state(payload)


def record_coordinate_import_success(
    config_path: str | Path,
    imported_camera_ids: set[str] | list[str],
    *,
    pre_import_point_counts: dict[str, int] | None = None,
    imported_at: str | None = None,
) -> dict[str, Any]:
    imported_ids = _sorted_camera_ids(imported_camera_ids)
    if not imported_ids:
        raise ValueError("成功匯入的 camera IDs 不可為空")

    state = _empty_coordinate_import_state()
    state["imported_camera_ids"] = imported_ids
    state["pre_import_point_counts"] = (
        pre_import_point_counts if pre_import_point_counts is not None else {}
    )
    state["locally_saved_camera_ids"] = list(imported_ids)
    state["imported_at"] = imported_at or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    state = _normalize_coordinate_import_state(state)
    return _atomic_write_coordinate_import_state(config_path, state)


def mark_coordinate_cameras_locally_saved(
    config_path: str | Path,
    camera_ids: set[str] | list[str],
    *,
    checked: bool = False,
) -> dict[str, Any]:
    state = load_coordinate_import_state(config_path)
    imported_ids = set(state["imported_camera_ids"])
    target_ids = set(_sorted_camera_ids(camera_ids)) & imported_ids
    if not target_ids:
        return state

    state["locally_saved_camera_ids"] = _sorted_camera_ids(
        set(state["locally_saved_camera_ids"]) | target_ids
    )
    if checked:
        state["checked_camera_ids"] = _sorted_camera_ids(
            set(state["checked_camera_ids"]) | target_ids
        )
    return _atomic_write_coordinate_import_state(config_path, state)


def mark_coordinate_cameras_synced(
    config_path: str | Path,
    camera_ids: set[str] | list[str],
    *,
    synced_at: str | None = None,
) -> dict[str, Any]:
    state = load_coordinate_import_state(config_path)
    imported_ids = set(state["imported_camera_ids"])
    target_ids = set(_sorted_camera_ids(camera_ids)) & imported_ids
    if not target_ids:
        return state

    state["synced_camera_ids"] = _sorted_camera_ids(
        set(state["synced_camera_ids"]) | target_ids
    )
    state["synced_at"] = synced_at or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return _atomic_write_coordinate_import_state(config_path, state)


@dataclass
class CameraCatalog:
    cameras: list[dict[str, Any]]
    labels: list[str]
    by_label: dict[str, dict[str, Any]]

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> "CameraCatalog":
        cameras = [camera for camera in config.get("cameras", []) if camera.get("ip")]
        labels = [format_camera_label(camera) for camera in cameras]
        by_label = {format_camera_label(camera): camera for camera in cameras}
        return cls(cameras=cameras, labels=labels, by_label=by_label)

    def label_for(self, camera_info: dict[str, Any] | None) -> str:
        return format_camera_label(camera_info or {})

    def get_by_label(self, label: str) -> dict[str, Any] | None:
        return self.by_label.get(label)


def format_camera_label(camera_info: dict[str, Any]) -> str:
    location = str(camera_info.get("location") or "未命名攝影機")
    ip = str(camera_info.get("ip") or "")
    channel = str(camera_info.get("NVR_channel") or "")
    suffix = f" | CH {channel}" if channel else ""
    return f"{location} ({ip}){suffix}"


def suggest_point_name(point_index: int) -> str:
    if 0 <= point_index < 26:
        return chr(ord("A") + point_index)
    return "P{0}".format(point_index + 1)


# =========================
# 影像檔案 / 來源
# =========================
@dataclass
class CanvasImageState:
    image_path: Path | None = None
    image_rgb: np.ndarray | None = None
    image_box: dict[str, int] | None = None

    def clear(self) -> None:
        self.image_path = None
        self.image_rgb = None
        self.image_box = None

    def set_image(self, image_path: Path, image_rgb: np.ndarray) -> None:
        self.image_path = image_path
        self.image_rgb = image_rgb
        self.image_box = None

    @property
    def has_image(self) -> bool:
        return self.image_path is not None and self.image_rgb is not None


def ip_to_folder_name(ip_address: str) -> str:
    return ip_address.replace(".", "_")


def get_camera_image_dir(images_dir: str | Path, ip_address: str) -> Path:
    return Path(images_dir) / ip_to_folder_name(ip_address)


def find_latest_image_for_camera(images_dir: str | Path, ip_address: str) -> Path | None:
    camera_dir = get_camera_image_dir(images_dir, ip_address)
    if not camera_dir.exists():
        return None

    candidates = [
        path
        for path in camera_dir.iterdir()
        if path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS
    ]
    if not candidates:
        return None

    return max(candidates, key=lambda path: path.stat().st_mtime)


def resolve_existing_image(camera_info: dict[str, Any], images_dir: str | Path) -> tuple[Path | None, str | None]:
    latest_image = find_latest_image_for_camera(images_dir, str(camera_info["ip"]))
    if latest_image is None:
        return None, None
    return latest_image, "來自 images 最新圖片"


def find_cameras_missing_images(
    config: dict[str, Any],
    images_dir: str | Path,
    selected_camera_ids: set[str],
) -> list[dict[str, Any]]:
    missing_cameras = []
    for camera in config.get("cameras", []):
        try:
            camera_id = standalone_package.camera_id_from_camera(camera)
        except ValueError:
            continue

        if camera_id not in selected_camera_ids:
            continue

        image_path, _source_text = resolve_existing_image(camera, images_dir)
        if image_path is None:
            missing_cameras.append(camera)

    return missing_cameras


def read_image_rgb(image_path: str | Path) -> np.ndarray:
    file_bytes = np.fromfile(str(image_path), dtype=np.uint8)
    if file_bytes.size == 0:
        raise ValueError(f"無法讀取圖片: {image_path}")

    frame_bgr = cv2.imdecode(file_bytes, cv2.IMREAD_COLOR)
    if frame_bgr is None:
        raise ValueError(f"無法解析圖片: {image_path}")

    return cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)


def write_image_bgr(image_path: str | Path, frame_bgr: np.ndarray) -> None:
    image_path = Path(image_path)
    image_path.parent.mkdir(parents=True, exist_ok=True)

    success, encoded = cv2.imencode(".jpg", frame_bgr)
    if not success:
        raise ValueError("圖片編碼失敗，無法儲存擷取結果")

    encoded.tofile(str(image_path))


def resize_image_for_canvas(
    image_rgb: np.ndarray,
    canvas_width: int,
    canvas_height: int,
) -> tuple[Image.Image, dict[str, int]]:
    image_height, image_width = image_rgb.shape[:2]
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
    pil_image = Image.fromarray(image_rgb).resize((display_width, display_height), RESAMPLE_LANCZOS)
    return pil_image, image_box


# =========================
# 座標換算 / 匯出
# =========================
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


def build_export_payload(
    image_path: Path,
    point_rows: list[tuple[Any, Any, Any, Any, Any]],
) -> dict[str, Any]:
    points = []
    for point_name, wgs84_x, wgs84_y, image_x, image_y in point_rows:
        points.append(
            {
                "point_name": str(point_name),
                "image_coord": {
                    "x": int(round(float(image_x))),
                    "y": int(round(float(image_y))),
                },
                "wgs84_coords": {"x": float(wgs84_x), "y": float(wgs84_y)},
            }
        )

    return {
        "image_name": image_path.name,
        "save_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "points": points,
    }


def build_geo_reference_update_url(config: dict[str, Any]) -> str:
    configured_url = str(config.get("coordinate_geo_reference_url") or "").strip()
    if configured_url:
        return configured_url

    server_ip = str(config.get("server_ip") or "").strip()
    if not server_ip:
        raise ValueError("distributed_config 缺少 server_ip，無法回傳座標到伺服端")

    return "http://{0}:{1}/coordinate/geo_reference".format(
        server_ip,
        utility.get_server_port(config),
    )


def build_geo_reference_batch_update_url(config: dict[str, Any]) -> str:
    configured_url = str(config.get("coordinate_geo_reference_batch_url") or "").strip()
    if configured_url:
        return configured_url

    single_url = build_geo_reference_update_url(config).rstrip("/")
    if single_url.endswith("/batch"):
        return single_url
    return f"{single_url}/batch"


def build_geo_reference_snapshot_url(config: dict[str, Any]) -> str:
    configured_url = str(config.get("coordinate_geo_reference_snapshot_url") or "").strip()
    if configured_url:
        return configured_url

    single_url = build_geo_reference_update_url(config).rstrip("/")
    if single_url.endswith("/batch"):
        single_url = single_url[:-len("/batch")]
    if single_url.endswith("/snapshot"):
        return single_url
    return f"{single_url}/snapshot"


def geo_reference_version_hash(
    geo_reference: Any,
    *,
    fallback_image_name: str | None = None,
) -> str:
    normalized = standalone_package.normalize_geo_reference(
        geo_reference,
        image_name=fallback_image_name,
    )
    points = sorted(
        normalized["points"],
        key=lambda point: (
            point["point_name"],
            json.dumps(point, sort_keys=True, separators=(",", ":"), ensure_ascii=False),
        ),
    )
    version_payload = {
        "image_name": normalized["image_name"],
        "points": points,
    }
    encoded = json.dumps(
        version_payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def fetch_geo_reference_snapshots(
    config: dict[str, Any],
    cameras: list[dict[str, Any]],
    timeout_seconds: float = 12.0,
) -> dict[str, dict[str, Any]]:
    if not cameras:
        return {}

    expected_by_camera_id: dict[str, dict[str, str]] = {}
    request_cameras = []
    for camera in cameras:
        camera_ip = str(camera.get("camera_ip") or "").strip()
        nvr_channel = str(camera.get("nvr_channel") or "").strip()
        if not camera_ip or not nvr_channel:
            raise ValueError("版本檢查缺少攝影機 IP 或 NVR channel")
        camera_id = standalone_package.camera_id_from_channel(nvr_channel)
        if camera_id in expected_by_camera_id:
            raise ValueError(f"版本檢查包含重複攝影機：{camera_id}")
        expected_by_camera_id[camera_id] = {
            "camera_ip": camera_ip,
            "nvr_channel": str(int(nvr_channel)),
        }
        request_cameras.append(
            {
                "camera_ip": camera_ip,
                "nvr_channel": str(int(nvr_channel)),
            }
        )

    snapshot_url = build_geo_reference_snapshot_url(config)
    try:
        response = requests.post(
            snapshot_url,
            json={"cameras": request_cameras},
            timeout=timeout_seconds,
        )
    except requests.RequestException as exc:
        raise ConnectionError(
            "無法查詢伺服端座標版本。\n\n"
            f"目標：{snapshot_url}\n"
            f"原因：{exc}"
        ) from exc

    try:
        response_payload = response.json()
    except (TypeError, ValueError):
        response_payload = None

    status_code = int(response.status_code)
    if not 200 <= status_code < 300:
        message = (
            response_payload.get("message")
            if isinstance(response_payload, dict)
            else None
        ) or response.text or "伺服端拒絕座標版本查詢"
        raise RuntimeError(
            "伺服端座標版本查詢失敗。\n\n"
            f"目標：{snapshot_url}\n"
            f"HTTP 狀態碼：{status_code}\n"
            f"原因：{message}"
        )

    if not isinstance(response_payload, dict) or response_payload.get("code") != "OK":
        raise RuntimeError("伺服端座標版本查詢回應格式錯誤，無法確認版本")
    data = response_payload.get("data")
    snapshots = data.get("cameras") if isinstance(data, dict) else None
    if not isinstance(snapshots, list) or len(snapshots) != len(expected_by_camera_id):
        raise RuntimeError("伺服端座標版本查詢回應數量不符，無法確認版本")

    result: dict[str, dict[str, Any]] = {}
    for index, snapshot in enumerate(snapshots):
        if not isinstance(snapshot, dict) or "geo_reference" not in snapshot:
            raise RuntimeError(f"伺服端版本回應第 {index + 1} 筆格式錯誤")
        try:
            camera_id = standalone_package.camera_id_from_channel(
                snapshot.get("nvr_channel")
            )
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"伺服端版本回應第 {index + 1} 筆 channel 無效") from exc
        expected = expected_by_camera_id.get(camera_id)
        if expected is None or camera_id in result:
            raise RuntimeError("伺服端座標版本查詢回傳了非預期或重複攝影機")
        if str(snapshot.get("camera_ip") or "").strip() != expected["camera_ip"]:
            raise RuntimeError(f"伺服端版本回應的攝影機 IP 與 CH 不符：{camera_id}")
        result[camera_id] = {
            "camera_ip": expected["camera_ip"],
            "nvr_channel": expected["nvr_channel"],
            "geo_reference": snapshot.get("geo_reference"),
        }

    if len(result) != len(expected_by_camera_id):
        raise RuntimeError("伺服端座標版本查詢缺少攝影機資料")
    return result


def submit_geo_reference_update(
    config: dict[str, Any],
    camera_info: dict[str, Any],
    geo_reference_payload: dict[str, Any],
    timeout_seconds: float = 15.0,
) -> dict[str, Any]:
    camera_ip = str(camera_info.get("ip") or "").strip()
    if not camera_ip:
        raise ValueError("攝影機設定缺少 ip，無法回傳座標到伺服端")

    nvr_channel = str(camera_info.get("NVR_channel") or "").strip()
    if not nvr_channel:
        raise ValueError("攝影機設定缺少 NVR_channel，無法回傳座標到伺服端")

    update_url = build_geo_reference_update_url(config)
    request_payload = {
        "camera_ip": camera_ip,
        "nvr_channel": nvr_channel,
        "geo_reference": geo_reference_payload,
    }

    try:
        response = requests.post(update_url, json=request_payload, timeout=timeout_seconds)
    except requests.RequestException as exc:
        raise ConnectionError(
            "無法連線到座標更新伺服端。\n\n"
            f"目標：{update_url}\n"
            f"原因：{exc}"
        ) from exc

    try:
        response_payload = response.json()
    except ValueError:
        response_payload = {}

    if not response.ok:
        message = response_payload.get("message") or response.text or "伺服端拒絕座標更新"
        raise RuntimeError(
            "座標更新失敗。\n\n"
            f"目標：{update_url}\n"
            f"HTTP 狀態碼：{response.status_code}\n"
            f"原因：{message}"
        )

    if response_payload.get("code") not in (None, "OK"):
        raise RuntimeError(response_payload.get("message") or "伺服端回傳未知錯誤")

    return response_payload


def save_then_submit_geo_reference_update(
    config: dict[str, Any],
    config_path: str | Path,
    camera_info: dict[str, Any],
    geo_reference_payload: dict[str, Any],
    *,
    local_saver: Callable[[str | Path, dict[str, Any], dict[str, Any]], None] | None = None,
    remote_submitter: Callable[[dict[str, Any], dict[str, Any], dict[str, Any]], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Persist one camera locally before attempting its remote update."""
    if local_saver is None:
        local_saver = lambda path, camera, payload: update_camera_geo_reference(
            path,
            str(camera.get("ip") or "").strip(),
            payload,
        )
    if remote_submitter is None:
        remote_submitter = submit_geo_reference_update

    local_saver(config_path, camera_info, geo_reference_payload)
    try:
        return remote_submitter(config, camera_info, geo_reference_payload)
    except Exception as exc:
        raise GeoReferenceSingleRemoteError(exc) from exc


def build_geo_reference_batch_updates(
    config: dict[str, Any],
    result_cameras: list[dict[str, Any]],
    *,
    selected_camera_ids: set[str],
) -> list[dict[str, Any]]:
    """Build only selected, locally-known camera updates for the batch API."""
    selected_ids = {str(camera_id) for camera_id in selected_camera_ids}
    local_cameras_by_id: dict[str, dict[str, Any]] = {}
    for camera in config.get("cameras", []):
        try:
            camera_id = standalone_package.camera_id_from_camera(camera)
        except ValueError:
            continue
        local_cameras_by_id[camera_id] = camera

    return _build_geo_reference_batch_updates_from_cameras(
        result_cameras,
        selected_ids=selected_ids,
        local_cameras_by_id=local_cameras_by_id,
    )


def _build_geo_reference_batch_updates_from_cameras(
    result_cameras: list[dict[str, Any]],
    *,
    selected_ids: set[str],
    local_cameras_by_id: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Internal payload builder; the caller supplies the local camera index."""
    updates = []
    for result_camera in result_cameras:
        camera_id = str(result_camera.get("camera_id") or "").strip()
        if camera_id not in selected_ids:
            continue

        local_camera = local_cameras_by_id.get(camera_id)
        if local_camera is None:
            continue

        updates.append(
            {
                "camera_ip": str(local_camera.get("ip") or "").strip(),
                "nvr_channel": str(local_camera.get("NVR_channel") or "").strip(),
                "geo_reference": result_camera[GEO_REFERENCE_KEY],
            }
        )
    return updates


def has_valid_geo_reference(geo_reference: Any) -> bool:
    if not isinstance(geo_reference, dict):
        return False
    if not str(geo_reference.get("image_name") or "").strip():
        return False
    if not isinstance(geo_reference.get("points"), list):
        return False
    try:
        standalone_package.normalize_geo_reference(geo_reference)
    except (TypeError, ValueError):
        return False
    return True


def _read_local_camera_for_batch(
    config_path: str | Path,
    camera_id: str,
) -> dict[str, Any] | None:
    try:
        camera_config_path = get_camera_config_path_by_camera_id(config_path, camera_id)
    except ValueError:
        return None
    if not camera_config_path.is_file():
        return None
    try:
        camera = standalone_package.read_json(camera_config_path)
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    try:
        if standalone_package.camera_id_from_camera(camera) != camera_id:
            return None
    except ValueError:
        return None
    return camera


def build_geo_reference_batch_updates_from_local_config(
    config: dict[str, Any],
    config_path: str | Path,
    selected_camera_ids: set[str] | list[str],
) -> list[dict[str, Any]]:
    """Build updates from the final geo_reference stored in local camera files."""
    del config  # The camera file is authoritative for this post-inspection workflow.
    updates = []
    for camera_id in _sorted_camera_ids(selected_camera_ids):
        camera = _read_local_camera_for_batch(config_path, camera_id)
        if camera is None:
            continue

        geo_reference = camera.get(GEO_REFERENCE_KEY)
        if not has_valid_geo_reference(geo_reference):
            continue

        camera_ip = str(camera.get("ip") or "").strip()
        nvr_channel = str(camera.get("NVR_channel") or "").strip()
        if not camera_ip or not nvr_channel:
            continue

        updates.append(
            {
                "camera_ip": camera_ip,
                "nvr_channel": nvr_channel,
                "geo_reference": deepcopy(geo_reference),
            }
        )
    return updates


def build_local_geo_reference_batch_preview(
    config: dict[str, Any],
    config_path: str | Path,
    state: dict[str, Any],
) -> list[dict[str, Any]]:
    """Describe only the current imported cameras using local config files."""
    del config  # Local camera files are authoritative for this preview.
    normalized_state = _normalize_coordinate_import_state(state)
    pre_import_point_counts = normalized_state["pre_import_point_counts"]
    locally_saved_ids = set(normalized_state["locally_saved_camera_ids"])
    checked_ids = set(normalized_state["checked_camera_ids"])
    synced_ids = set(normalized_state["synced_camera_ids"])
    rows = []

    for camera_id in normalized_state["imported_camera_ids"]:
        camera = _read_local_camera_for_batch(config_path, camera_id)
        try:
            channel = str(standalone_package.channel_from_camera_id(camera_id))
        except ValueError:
            channel = ""

        if camera is None:
            rows.append(
                {
                    "camera_id": camera_id,
                    "camera_ip": "",
                    "nvr_channel": channel,
                    "display_name": f"CH {channel}" if channel else camera_id,
                    "pre_import_point_count": pre_import_point_counts.get(camera_id),
                    "point_count": 0,
                    "exists": False,
                    "valid_geo_reference": False,
                    "can_sync": False,
                    "checked": camera_id in checked_ids,
                    "locally_saved": camera_id in locally_saved_ids,
                    "synced": camera_id in synced_ids,
                    "reason": "本機不存在",
                }
            )
            continue

        geo_reference = camera.get(GEO_REFERENCE_KEY)
        valid_geo_reference = has_valid_geo_reference(geo_reference)
        camera_ip = str(camera.get("ip") or "").strip()
        nvr_channel = str(camera.get("NVR_channel") or channel).strip()
        has_valid_target = bool(camera_ip and nvr_channel)
        points = geo_reference.get("points") if isinstance(geo_reference, dict) else []
        rows.append(
            {
                "camera_id": camera_id,
                "camera_ip": camera_ip,
                "nvr_channel": nvr_channel,
                "display_name": str(camera.get("location") or f"CH {channel}"),
                "pre_import_point_count": pre_import_point_counts.get(camera_id),
                "point_count": len(points) if isinstance(points, list) else 0,
                "exists": True,
                "valid_geo_reference": valid_geo_reference,
                "can_sync": valid_geo_reference and has_valid_target,
                "checked": camera_id in checked_ids,
                "locally_saved": camera_id in locally_saved_ids,
                "synced": camera_id in synced_ids,
                "reason": (
                    ""
                    if valid_geo_reference and has_valid_target
                    else (
                        "沒有有效 geo_reference"
                        if not valid_geo_reference
                        else "缺少有效攝影機識別欄位"
                    )
                ),
            }
        )
    return rows


def count_geo_reference_batch_points(
    result_cameras: list[dict[str, Any]],
    selected_camera_ids: set[str],
) -> int:
    selected_ids = {str(camera_id) for camera_id in selected_camera_ids}
    total_points = 0
    for result_camera in result_cameras:
        camera_id = str(result_camera.get("camera_id") or "").strip()
        if camera_id not in selected_ids:
            continue
        geo_reference = result_camera.get(GEO_REFERENCE_KEY) or {}
        points = geo_reference.get("points") if isinstance(geo_reference, dict) else []
        if isinstance(points, list):
            total_points += len(points)
    return total_points


def validate_geo_reference_batch_response(
    response_payload: Any,
    expected_count: int,
) -> dict[str, Any]:
    """Validate the success contract returned by the A40 batch endpoint."""
    contract_prefix = "伺服器回應契約異常"
    if not isinstance(response_payload, dict):
        raise GeoReferenceBatchResponseError(
            f"{contract_prefix}：response body 必須是 object"
        )

    if response_payload.get("code") != "OK":
        raise GeoReferenceBatchResponseError(
            f"{contract_prefix}：code 必須等於 OK"
        )

    data = response_payload.get("data")
    if not isinstance(data, dict):
        raise GeoReferenceBatchResponseError(
            f"{contract_prefix}：data 必須是 object"
        )

    updated_count = data.get("updated_count")
    if isinstance(updated_count, bool) or not isinstance(updated_count, int):
        raise GeoReferenceBatchResponseError(
            f"{contract_prefix}：data.updated_count 必須是非 bool 的整數"
        )
    if updated_count != expected_count:
        raise GeoReferenceBatchResponseError(
            f"{contract_prefix}：updated_count={updated_count}，預期 {expected_count}"
        )

    updated_targets = data.get("updated_targets")
    if not isinstance(updated_targets, list):
        raise GeoReferenceBatchResponseError(
            f"{contract_prefix}：data.updated_targets 必須是陣列"
        )
    if len(updated_targets) != updated_count:
        raise GeoReferenceBatchResponseError(
            f"{contract_prefix}：updated_targets 數量與 updated_count 不一致"
        )

    for index, target in enumerate(updated_targets):
        if not isinstance(target, dict):
            raise GeoReferenceBatchResponseError(
                f"{contract_prefix}：updated_targets[{index}] 必須是 object"
            )
        if not str(target.get("camera_ip") or "").strip():
            raise GeoReferenceBatchResponseError(
                f"{contract_prefix}：updated_targets[{index}].camera_ip 缺少必要值"
            )
        if target.get("nvr_channel") in (None, ""):
            raise GeoReferenceBatchResponseError(
                f"{contract_prefix}：updated_targets[{index}].nvr_channel 缺少必要值"
            )
        point_count = target.get("point_count")
        if isinstance(point_count, bool) or not isinstance(point_count, int) or point_count < 0:
            raise GeoReferenceBatchResponseError(
                f"{contract_prefix}：updated_targets[{index}].point_count 必須是非負整數"
            )

    if data.get("dispatch_status") != "queued":
        raise GeoReferenceBatchResponseError(
            f"{contract_prefix}：dispatch_status 必須等於 queued"
        )

    return response_payload


def submit_geo_reference_batch_update(
    config: dict[str, Any],
    updates: list[dict[str, Any]],
    timeout_seconds: float = 30.0,
) -> dict[str, Any]:
    if not updates:
        raise ValueError("尚未選擇任何可同步的攝影機")

    update_url = build_geo_reference_batch_update_url(config)
    request_payload = {"updates": updates}

    try:
        response = requests.post(update_url, json=request_payload, timeout=timeout_seconds)
    except requests.RequestException as exc:
        raise ConnectionError(
            "無法連線到座標批次更新伺服端。\n\n"
            f"目標：{update_url}\n"
            f"原因：{exc}"
        ) from exc

    try:
        response_payload = response.json()
    except (TypeError, ValueError):
        response_payload = None

    status_code = int(response.status_code)
    if not 200 <= status_code < 300:
        message = (
            response_payload.get("message")
            if isinstance(response_payload, dict)
            else None
        ) or response.text or "伺服端拒絕座標批次更新"
        raise RuntimeError(
            "座標批次更新失敗。\n\n"
            f"目標：{update_url}\n"
            f"HTTP 狀態碼：{status_code}\n"
            f"原因：{message}"
        )

    return validate_geo_reference_batch_response(response_payload, len(updates))


def load_point_rows_from_camera(camera_info: dict[str, Any]) -> list[tuple[str, float, float, int, int]]:
    geo_reference = camera_info.get(GEO_REFERENCE_KEY) or {}
    points = geo_reference.get("points") or []
    point_rows = []

    for point in points:
        point_name = str(point.get("point_name") or "")
        image_coord = point.get("image_coord") or {}
        wgs84_coords = point.get("wgs84_coords") or {}
        if not point_name:
            continue

        point_rows.append(
            (
                point_name,
                float(wgs84_coords.get("x", 0)),
                float(wgs84_coords.get("y", 0)),
                int(round(float(image_coord.get("x", 0)))),
                int(round(float(image_coord.get("y", 0)))),
            )
        )

    return point_rows


# =========================
# 設定檔寫回
# =========================
def update_camera_geo_reference(
    config_path: str | Path,
    camera_ip: str,
    geo_reference_payload: dict[str, Any],
) -> None:
    config_path = Path(config_path)
    config = load_local_camera_config(config_path)
    cameras = config.get("cameras", [])
    target_camera = next((camera for camera in cameras if camera.get("ip") == camera_ip), None)
    if target_camera is None:
        raise ValueError("在設定檔中找不到攝影機 IP: {0}".format(camera_ip))

    target_camera[GEO_REFERENCE_KEY] = geo_reference_payload

    camera_config_path = _get_camera_config_path(config_path, target_camera)
    with camera_config_path.open("w", encoding="utf-8") as fh:
        json.dump(target_camera, fh, indent=4, ensure_ascii=False)


def _get_camera_config_path(config_path: Path, camera_info: dict[str, Any]) -> Path:
    try:
        channel = int(camera_info["NVR_channel"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("攝影機設定缺少有效的 NVR_channel，無法寫回設定檔") from exc

    return config_path / utility.CAMERA_CONFIGS_DIR / "camera_channel_{0:03d}.json".format(channel)


def get_camera_config_path_by_camera_id(config_path: str | Path, camera_id: str) -> Path:
    channel = standalone_package.channel_from_camera_id(camera_id)
    return Path(config_path) / utility.CAMERA_CONFIGS_DIR / f"camera_channel_{channel:03d}.json"


def export_standalone_config_package(
    config: dict[str, Any],
    images_dir: str | Path,
    export_parent_dir: str | Path,
    selected_camera_ids: set[str],
) -> Path:
    images_dir = Path(images_dir)
    export_parent_dir = Path(export_parent_dir)
    timestamp = datetime.now().strftime("%y%m%d_%H%M%S")
    delivery_dir = export_parent_dir / f"{EXTERNAL_COORDINATE_EDITOR_PACKAGE_PREFIX}_{timestamp}"
    export_dir = delivery_dir / f"coordinate_editor_standalone_export_{timestamp}"
    external_editor_source = Path(__file__).resolve().parent / EXTERNAL_COORDINATE_EDITOR_DIRNAME

    if not external_editor_source.is_dir():
        raise FileNotFoundError(f"找不到外部版坐標編輯系統程式資料夾：\n{external_editor_source}")
    validate_external_coordinate_editor_package(external_editor_source)
    if delivery_dir.exists():
        raise FileExistsError(f"輸出資料夾已存在，請稍後再試或選擇其他位置：\n{delivery_dir}")

    selected_cameras = []
    missing_images = []
    export_images = []
    for camera in config.get("cameras", []):
        camera_id = standalone_package.camera_id_from_camera(camera)
        if camera_id not in selected_camera_ids:
            continue

        source_image, _source_text = resolve_existing_image(camera, images_dir)
        if source_image is None:
            missing_images.append(f"{camera_id} {camera.get('location') or ''}".strip())
            continue

        image_name = f"{camera_id}{source_image.suffix.lower()}"
        export_images.append((source_image, image_name))
        selected_cameras.append(standalone_package.build_standalone_camera(camera, image_name))

    if missing_images:
        raise FileNotFoundError(
            "以下攝影機找不到參考圖片，無法產生外部版資料：\n"
            + "\n".join(missing_images)
        )
    if not selected_cameras:
        raise ValueError("尚未選擇可匯出的攝影機")

    shutil.copytree(external_editor_source, delivery_dir)
    export_images_dir = export_dir / standalone_package.IMAGES_DIRNAME
    for source_image, image_name in export_images:
        standalone_package.copy_image_to_export(source_image, export_images_dir, image_name)

    standalone_package.write_json(
        export_dir / standalone_package.CONFIG_FILENAME,
        standalone_package.build_config_payload(selected_cameras),
    )
    return delivery_dir


def build_standalone_import_preview(
    config_path: str | Path,
    result_path: str | Path,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    result_cameras = standalone_package.load_result(result_path)
    package_config_path = Path(result_path).parent / standalone_package.CONFIG_FILENAME
    baseline_by_camera_id: dict[str, dict[str, Any]] = {}
    baseline_exported_at = ""
    baseline_error = ""
    if package_config_path.is_file():
        try:
            baseline_payload = standalone_package.read_json(package_config_path)
            baseline_exported_at = str(baseline_payload.get("created_at") or "")
            baseline_by_camera_id = {
                camera["camera_id"]: camera
                for camera in standalone_package.load_config(package_config_path)
            }
        except Exception as exc:
            baseline_error = str(exc)
    else:
        baseline_error = "找不到匯入包內的 coordinate_config.json"

    preview_rows = []
    for result_camera in result_cameras:
        camera_id = result_camera["camera_id"]
        camera_config_path = get_camera_config_path_by_camera_id(config_path, camera_id)
        exists = camera_config_path.is_file()
        local_camera = {}
        if exists:
            local_camera = standalone_package.read_json(camera_config_path)

        raw_local_geo_reference = local_camera.get(GEO_REFERENCE_KEY)
        local_geo_reference = raw_local_geo_reference
        if not isinstance(local_geo_reference, dict):
            local_geo_reference = {}
        current_points = local_geo_reference.get("points") or []
        if not isinstance(current_points, list):
            current_points = []
        import_points = result_camera.get(GEO_REFERENCE_KEY, {}).get("points") or []
        channel = standalone_package.channel_from_camera_id(camera_id)
        baseline_camera = baseline_by_camera_id.get(camera_id)
        baseline_geo_reference = (
            baseline_camera.get(GEO_REFERENCE_KEY) if baseline_camera else None
        )
        baseline_image_name = (
            baseline_camera.get("image_name") if baseline_camera else None
        )
        baseline_version_hash = None
        local_version_hash = None
        version_check_reason = baseline_error
        if baseline_camera is not None:
            try:
                if raw_local_geo_reference is not None and not isinstance(
                    raw_local_geo_reference,
                    dict,
                ):
                    raise ValueError("本機 geo_reference 必須是 object")
                baseline_version_hash = geo_reference_version_hash(
                    baseline_geo_reference,
                    fallback_image_name=baseline_image_name,
                )
                local_version_hash = geo_reference_version_hash(
                    raw_local_geo_reference,
                    fallback_image_name=baseline_image_name,
                )
                version_check_reason = ""
            except (TypeError, ValueError, OverflowError) as exc:
                version_check_reason = f"本機或匯出基準座標格式無效：{exc}"
        preview_rows.append(
            {
                "camera_id": camera_id,
                "nvr_channel": str(channel),
                "display_name": result_camera.get("display_name") or local_camera.get("location") or f"CH {channel:03d}",
                "camera_ip": str(local_camera.get("ip") or "").strip(),
                "exists": exists,
                "current_points": len(current_points),
                "import_points": len(import_points),
                "will_overwrite": bool(current_points),
                "baseline_available": baseline_camera is not None,
                "baseline_exported_at": baseline_exported_at,
                "baseline_image_name": baseline_image_name,
                "baseline_save_time": str(
                    (baseline_geo_reference or {}).get("save_time") or ""
                ),
                "import_save_time": str(
                    (result_camera.get(GEO_REFERENCE_KEY) or {}).get("save_time") or ""
                ),
                "local_save_time": str(local_geo_reference.get("save_time") or ""),
                "baseline_version_hash": baseline_version_hash,
                "local_version_hash": local_version_hash,
                "version_check_status": "未檢查",
                "version_check_reason": version_check_reason,
            }
        )
    return result_cameras, preview_rows


def apply_standalone_result_import(
    config_path: str | Path,
    result_cameras: list[dict[str, Any]],
    selected_camera_ids: set[str] | None = None,
) -> int:
    updated_count = 0
    for result_camera in result_cameras:
        camera_id = result_camera["camera_id"]
        if selected_camera_ids is not None and camera_id not in selected_camera_ids:
            continue

        camera_config_path = get_camera_config_path_by_camera_id(config_path, camera_id)
        if not camera_config_path.is_file():
            continue

        local_camera = standalone_package.read_json(camera_config_path)
        local_camera[GEO_REFERENCE_KEY] = result_camera[GEO_REFERENCE_KEY]
        with camera_config_path.open("w", encoding="utf-8") as fh:
            json.dump(local_camera, fh, indent=4, ensure_ascii=False)
        updated_count += 1
    return updated_count


def apply_standalone_result_import_and_record_state(
    config_path: str | Path,
    result_cameras: list[dict[str, Any]],
    selected_camera_ids: set[str],
    *,
    imported_at: str | None = None,
) -> tuple[int, dict[str, Any]]:
    """Persist an external import, then record only the successfully importable IDs."""
    selected_ids = set(_sorted_camera_ids(selected_camera_ids))
    importable_ids = {
        str(result_camera.get("camera_id") or "").strip()
        for result_camera in result_cameras
        if str(result_camera.get("camera_id") or "").strip() in selected_ids
        and get_camera_config_path_by_camera_id(
            config_path,
            str(result_camera.get("camera_id") or "").strip(),
        ).is_file()
    }
    pre_import_point_counts = {}
    for camera_id in importable_ids:
        local_camera = standalone_package.read_json(
            get_camera_config_path_by_camera_id(config_path, camera_id)
        )
        geo_reference = local_camera.get(GEO_REFERENCE_KEY) or {}
        points = geo_reference.get("points") if isinstance(geo_reference, dict) else []
        pre_import_point_counts[camera_id] = (
            len(points) if isinstance(points, list) else 0
        )

    updated_count = apply_standalone_result_import(
        config_path,
        result_cameras,
        selected_camera_ids=selected_ids,
    )
    if updated_count != len(importable_ids):
        raise RuntimeError(
            "本機匯入數量與可寫入攝影機數量不一致，未更新 pending import state"
        )

    state = record_coordinate_import_success(
        config_path,
        importable_ids,
        pre_import_point_counts=pre_import_point_counts,
        imported_at=imported_at,
    )
    return updated_count, state


def import_then_submit_geo_reference_batch(
    config: dict[str, Any],
    config_path: str | Path,
    result_cameras: list[dict[str, Any]],
    selected_camera_ids: set[str],
    updates: list[dict[str, Any]],
    *,
    local_importer: Callable[..., int] | None = None,
    remote_submitter: Callable[[dict[str, Any], list[dict[str, Any]]], dict[str, Any]] | None = None,
    progress_callback: Callable[[int], None] | None = None,
) -> tuple[int, dict[str, Any]]:
    """Import locally first, then submit the same batch to the remote server."""
    local_importer = local_importer or apply_standalone_result_import
    remote_submitter = remote_submitter or submit_geo_reference_batch_update

    local_updated_count = local_importer(
        config_path,
        result_cameras,
        selected_camera_ids=selected_camera_ids,
    )
    if progress_callback is not None:
        progress_callback(local_updated_count)

    try:
        response_payload = remote_submitter(config, updates)
    except Exception as exc:
        raise GeoReferenceBatchRemoteError(local_updated_count, exc) from exc

    return local_updated_count, response_payload


# =========================
# 攝影機單張擷取
# =========================
def _normalize_channel_61_to_100(channel: Any) -> int:
    try:
        channel = int(channel)
    except (TypeError, ValueError):
        return 1

    if 41 <= channel <= 90:
        return channel - 40
    if 91 <= channel <= 115:
        return channel - 90
    if 141 <= channel <= 145:
        return ((channel - 1) % 20) + 1
    return ((max(1, channel) - 1) % 20) + 1


def build_rtsp_url(camera_info: dict[str, Any]) -> str:
    ip_address = camera_info.get("ip")
    connection_type = str(camera_info.get("connection_type") or "").upper()

    if not ip_address:
        raise ValueError("camera_info 缺少 ip")

    if connection_type == "NVR":
        channel = _normalize_channel_61_to_100(camera_info.get("NVR_channel", 1))
        nvr_ip = camera_info.get("NVR_IP")
        if not nvr_ip:
            raise ValueError("{0} 缺少 NVR_IP，無法建立 RTSP".format(ip_address))
        return "rtsp://admin:hs888888@{0}:554/cam/realmonitor?channel={1}&subtype=0".format(nvr_ip, channel)

    if connection_type == "SINGEL":
        return "rtsp://admin:admin12345@{0}:554/live/0/main".format(ip_address)

    if connection_type == "MULTI":
        return "rtsp://admin:hs888888@{0}:554/cam/realmonitor?channel=1&subtype=0".format(ip_address)

    if connection_type == "DRIVE":
        raise ValueError("{0} 為 DRIVE 類型，沒有本地圖片時無法直接用 RTSP 擷取".format(ip_address))

    raise ValueError("不支援的 connection_type: {0}".format(camera_info.get("connection_type")))


def capture_single_frame(
    camera_info: dict[str, Any],
    timeout_seconds: float = 12.0,
    warmup_frames: int = 3,
) -> np.ndarray:
    rtsp_url = build_rtsp_url(camera_info)
    capture = cv2.VideoCapture(rtsp_url, cv2.CAP_FFMPEG)
    if not capture.isOpened():
        capture.release()
        raise ConnectionError("無法連線到攝影機 {0}".format(camera_info.get("ip")))

    deadline = time.time() + timeout_seconds
    last_frame = None

    try:
        while time.time() < deadline:
            ok, frame = capture.read()
            if not ok or frame is None:
                time.sleep(0.2)
                continue

            last_frame = frame
            for _ in range(max(0, warmup_frames - 1)):
                ok, newer_frame = capture.read()
                if ok and newer_frame is not None:
                    last_frame = newer_frame
            if last_frame is not None:
                return last_frame

        raise TimeoutError("攝影機 {0} 在 {1} 秒內沒有回傳畫面".format(camera_info.get("ip"), timeout_seconds))
    finally:
        capture.release()


def capture_and_cache_latest_image(
    camera_info: dict[str, Any],
    images_dir: str | Path,
    timeout_seconds: float = 12.0,
) -> Path:
    frame_bgr = capture_single_frame(camera_info, timeout_seconds=timeout_seconds)
    camera_dir = get_camera_image_dir(images_dir, str(camera_info["ip"]))
    timestamp = datetime.now().strftime("%Y_%m_%d_%H_%M_%S")
    image_path = camera_dir / "{0}.jpg".format(timestamp)
    write_image_bgr(image_path, frame_bgr)
    return image_path
