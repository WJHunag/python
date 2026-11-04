"""Configuration access for the workstation settings UI.

The workstation runtime is backed by ``distributed_config``.  Keeping the
load/save boundary here prevents the Tkinter page from accidentally writing
the derived runtime object or a legacy monolithic JSON file.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

import utility


class CameraConfigError(RuntimeError):
    """Raised when the workstation configuration cannot serve the UI."""


def format_camera_label(camera: dict[str, Any]) -> str:
    """Return a stable, human-readable label for the camera combobox."""

    location = str(camera.get("location") or "未命名攝影機")
    ip = str(camera.get("ip") or "")
    channel = camera.get("NVR_channel")
    channel_text = f"  |  CH {channel}" if channel not in (None, "") else ""
    return f"{location}  ({ip}){channel_text}"


class CameraConfigStore:
    """Load assigned cameras and persist one camera file at a time."""

    def __init__(self) -> None:
        self.config: dict[str, Any] = {}
        self.cameras: list[dict[str, Any]] = []
        self.labels: list[str] = []
        self._by_ip: dict[str, dict[str, Any]] = {}
        self._by_label: dict[str, dict[str, Any]] = {}

    def reload(self) -> dict[str, Any]:
        """Reload the cameras assigned to this workstation."""

        try:
            config = utility.load_config()
        except FileNotFoundError as exc:
            raise CameraConfigError(
                "找不到 workstation 的 distributed_config。\n\n"
                "請確認 smoke_config/distributed_config 已放在工作站執行目錄。"
            ) from exc
        except (OSError, ValueError) as exc:
            raise CameraConfigError(f"無法讀取 distributed_config：{exc}") from exc

        if not isinstance(config, dict):
            raise CameraConfigError("distributed_config 格式錯誤：根節點不是物件。")

        raw_cameras = config.get("cameras")
        if not isinstance(raw_cameras, list):
            raise CameraConfigError("distributed_config 缺少 cameras 陣列。")

        cameras = [
            deepcopy(camera)
            for camera in raw_cameras
            if isinstance(camera, dict) and str(camera.get("ip") or "").strip()
        ]

        self.config = deepcopy(config)
        self.cameras = cameras
        self.labels = [format_camera_label(camera) for camera in cameras]
        self._by_ip = {str(camera["ip"]): camera for camera in cameras}
        self._by_label = {
            label: camera for label, camera in zip(self.labels, cameras)
        }
        return deepcopy(self.config)

    def camera_by_ip(self, ip_address: str | None) -> dict[str, Any] | None:
        """Return a detached camera copy for editing."""

        if not ip_address:
            return None
        camera = self._by_ip.get(str(ip_address))
        return deepcopy(camera) if camera else None

    def camera_by_label(self, label: str | None) -> dict[str, Any] | None:
        """Return a detached camera copy selected by its display label."""

        if not label:
            return None
        camera = self._by_label.get(label)
        return deepcopy(camera) if camera else None

    def save_camera(self, camera: dict[str, Any]) -> None:
        """Persist one camera through the current distributed-config writer."""

        if not isinstance(camera, dict) or not str(camera.get("ip") or "").strip():
            raise CameraConfigError("攝影機設定缺少有效的 ip。")
        if camera.get("NVR_channel") in (None, ""):
            raise CameraConfigError("攝影機設定缺少 NVR_channel，無法定位設定檔。")

        try:
            utility.save_distributed_camera_config(deepcopy(camera))
        except (OSError, TypeError, ValueError) as exc:
            raise CameraConfigError(f"攝影機設定儲存失敗：{exc}") from exc

        self.reload()
