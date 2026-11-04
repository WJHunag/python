# -*- coding: utf-8 -*-
"""
煙霧偵測分散式設定檔的讀寫工具。

分散式設定會把原本龐大的總設定檔拆成幾個邏輯檔案：

distributed_config/
  common_settings.json
  workstation_assignments.json
  camera_configs/
    camera_channel_001.json
    ...

外部呼叫者仍可取得舊版 total config 形狀的資料；寫入時則只更新
實際擁有該資料的檔案，避免每次修改都碰到整份大型 JSON。
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple, Union


COMMON_SETTINGS_FILE = "common_settings.json"
WORKSTATION_ASSIGNMENTS_FILE = "workstation_assignments.json"
CAMERA_CONFIGS_DIR = "camera_configs"
CAMERA_FILE_RE = re.compile(r"^camera_channel_(\d+)\.json$")


class DistributedConfigError(RuntimeError):
    """分散式設定檔無法讀取或驗證失敗時拋出的例外。"""


def _compact_flat_int_lists_pretty(s: str) -> str:
    """把純整數陣列壓成單行，讓頻道清單更容易閱讀。"""
    pattern = re.compile(r"\[\s*(?:\d+\s*,\s*)*\d+\s*\]", flags=re.DOTALL)

    def repl(m):
        nums = re.findall(r"\d+", m.group(0))
        return "[ " + ", ".join(nums) + " ]"

    return pattern.sub(repl, s)


def _load_json(path: Path) -> Dict[str, Any]:
    """讀取 JSON 檔，並確認根節點是物件。"""
    if not path.is_file():
        raise FileNotFoundError(f"Config file not found: {path}")
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise DistributedConfigError(f"JSON root must be an object: {path}")
    return data


def _atomic_write_json(path: Path, data: Dict[str, Any]) -> None:
    """以暫存檔加替換的方式寫入 JSON，避免寫到一半留下壞檔。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=".__tmp__", suffix=".json", dir=str(path.parent))
    tmp = Path(tmp_path)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            s = json.dumps(data, ensure_ascii=False, indent=2)
            f.write(_compact_flat_int_lists_pretty(s))
        os.replace(str(tmp), str(path))
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except Exception:
            pass


def _channel_id(value: Any) -> int:
    """將 NVR_channel 正規化成 int，失敗時拋出設定錯誤。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        raise DistributedConfigError(f"Invalid NVR_channel: {value!r}")


def _camera_filename(channel_id: int) -> str:
    """依頻道號產生固定格式的攝影機設定檔名。"""
    return f"camera_channel_{int(channel_id):03d}.json"


def _sort_camera_key(cam: Dict[str, Any]) -> Tuple[int, str]:
    """攝影機排序鍵：優先依 NVR_channel，其次依 IP。"""
    try:
        return _channel_id(cam.get("NVR_channel")), str(cam.get("ip", ""))
    except DistributedConfigError:
        return 10**9, str(cam.get("ip", ""))


def _normalize_camera_channel(cam: Dict[str, Any]) -> Dict[str, Any]:
    """複製攝影機設定，並把 NVR_channel 統一存成字串。"""
    out = deepcopy(cam)
    if "NVR_channel" in out:
        out["NVR_channel"] = str(_channel_id(out["NVR_channel"]))
    return out


def derive_stage2_cams(cameras: Iterable[Dict[str, Any]]) -> List[int]:
    """由單支攝影機的 stage2_detection 開關動態產生二階頻道清單。"""
    stage2_channels: List[int] = []
    seen: Set[int] = set()
    for cam in cameras:
        if not isinstance(cam, dict):
            continue
        switches = cam.get("function_switches") or {}
        if not isinstance(switches, dict) or not switches.get("stage2_detection", False):
            continue
        try:
            channel_id = _channel_id(cam.get("NVR_channel"))
        except DistributedConfigError:
            continue
        if channel_id not in seen:
            seen.add(channel_id)
            stage2_channels.append(channel_id)
    return sorted(stage2_channels)


def resolve_distributed_root(total_config_path: Optional[Union[str, Path]] = None) -> Path:
    """解析 distributed_config 根目錄，優先使用環境變數覆寫。"""
    env_root = os.getenv("DISTRIBUTED_CONFIG_DIR")
    if env_root:
        return Path(env_root).expanduser().resolve()
    if total_config_path is None:
        return Path(__file__).resolve().parent / "distributed_config"
    total_path = Path(total_config_path).expanduser().resolve()
    return total_path.parent / "distributed_config"


class DistributedConfigStore:
    """集中管理分散式設定檔的讀取、寫入與組裝。"""

    def __init__(
        self,
        total_config_path: Optional[Union[str, Path]] = None,
        *,
        distributed_root: Optional[Union[str, Path]] = None,
    ) -> None:
        self.total_config_path = Path(total_config_path).expanduser().resolve() if total_config_path else None
        self.root = Path(distributed_root).expanduser().resolve() if distributed_root else resolve_distributed_root(self.total_config_path)
        self.common_settings_path = self.root / COMMON_SETTINGS_FILE
        self.workstation_assignments_path = self.root / WORKSTATION_ASSIGNMENTS_FILE
        self.camera_configs_dir = self.root / CAMERA_CONFIGS_DIR

    def is_available(self) -> bool:
        """檢查分散式設定檔三個必要區塊是否都存在。"""
        return (
            self.common_settings_path.is_file()
            and self.workstation_assignments_path.is_file()
            and self.camera_configs_dir.is_dir()
        )

    def load_legacy_total_config(self) -> Dict[str, Any]:
        """讀取舊版單一總設定檔。"""
        if self.total_config_path is None:
            raise FileNotFoundError("Legacy total config path is not configured.")
        return _load_json(self.total_config_path)

    def load_common_settings(self) -> Dict[str, Any]:
        """讀取共用設定 common_settings.json。"""
        return _load_json(self.common_settings_path)

    def save_common_settings(self, data: Dict[str, Any]) -> None:
        """儲存共用設定，並避免把 cameras 或 workstation_ip_mapping 寫進此檔。"""
        data = deepcopy(data)
        data.pop("cameras", None)
        data.pop("workstation_ip_mapping", None)
        data.pop("stage2_cams", None)
        data.pop("upload_ip", None)
        _atomic_write_json(self.common_settings_path, data)

    def load_workstation_assignments(self) -> Dict[str, Any]:
        """讀取工作站分派設定 workstation_assignments.json。"""
        return _load_json(self.workstation_assignments_path)

    def save_workstation_assignments(self, data: Dict[str, Any]) -> None:
        """儲存工作站分派設定。"""
        _atomic_write_json(self.workstation_assignments_path, deepcopy(data))

    def camera_path(self, channel_id: int) -> Path:
        """取得指定頻道的攝影機設定檔路徑。"""
        return self.camera_configs_dir / _camera_filename(channel_id)

    def list_camera_paths(self) -> List[Path]:
        """列出所有符合 camera_channel_XXX.json 命名規則的攝影機設定檔。"""
        if not self.camera_configs_dir.is_dir():
            return []

        def sort_key(path: Path) -> Tuple[int, str]:
            m = CAMERA_FILE_RE.match(path.name)
            if not m:
                return 10**9, path.name
            return int(m.group(1)), path.name

        return sorted(
            [p for p in self.camera_configs_dir.iterdir() if p.is_file() and CAMERA_FILE_RE.match(p.name)],
            key=sort_key,
        )

    def load_camera_config(self, channel_id: int) -> Dict[str, Any]:
        """讀取單一頻道的攝影機設定。"""
        cam = _load_json(self.camera_path(channel_id))
        return _normalize_camera_channel(cam)

    def save_camera_config(self, channel_id: int, data: Dict[str, Any]) -> None:
        """儲存單一攝影機設定，並檢查檔名頻道與內容頻道一致。"""
        cam = _normalize_camera_channel(data)
        actual = _channel_id(cam.get("NVR_channel"))
        if actual != int(channel_id):
            raise DistributedConfigError(
                f"Camera file channel mismatch: path={channel_id}, payload={actual}"
            )
        _atomic_write_json(self.camera_path(channel_id), cam)

    def load_all_camera_configs(self) -> List[Dict[str, Any]]:
        """讀取全部攝影機設定，檢查頻道不可重複，最後依頻道排序。"""
        cameras = [_normalize_camera_channel(_load_json(path)) for path in self.list_camera_paths()]
        seen: Dict[int, Path] = {}
        for path, cam in zip(self.list_camera_paths(), cameras):
            cid = _channel_id(cam.get("NVR_channel"))
            if cid in seen:
                raise DistributedConfigError(f"Duplicate camera NVR_channel {cid}: {seen[cid]} and {path}")
            seen[cid] = path
        return sorted(cameras, key=_sort_camera_key)

    def build_total_config(self, *, fallback_to_legacy: bool = True) -> Dict[str, Any]:
        """組回舊版 total config 形狀，供仍依賴單一設定物件的流程使用。"""
        if not self.is_available():
            if fallback_to_legacy:
                return self.load_legacy_total_config()
            raise FileNotFoundError(f"Distributed config is not initialized: {self.root}")

        cfg = self.load_common_settings()
        cfg["workstation_ip_mapping"] = self.load_workstation_assignments()
        cfg["cameras"] = self.load_all_camera_configs()
        cfg["stage2_cams"] = derive_stage2_cams(cfg["cameras"])
        return cfg

    def build_workstation_config(self, workstation_key: str, *, status_provider: Optional[object] = None) -> Dict[str, Any]:
        """組出單一工作站需要的設定，只包含該站分派到的攝影機。"""
        cfg = self.load_common_settings()
        assignments = self.load_workstation_assignments()
        if workstation_key not in assignments:
            raise KeyError(f"Unknown workstation: {workstation_key}")

        entry = deepcopy(assignments.get(workstation_key))
        if isinstance(entry, list):
            entry = {"polling_cameras": entry, "fixed_cameras": []}
        elif not isinstance(entry, dict):
            entry = {"polling_cameras": [], "fixed_cameras": []}

        host = workstation_key.split(":", 1)[0] if workstation_key.count(":") == 1 else workstation_key
        if status_provider:
            curr_map = getattr(status_provider, "get_current_polling_map", lambda: {})() or {}
            disc_map = getattr(status_provider, "get_disconnection_map", lambda: {})() or {}
            entry.setdefault("current_polling_cameras", list(curr_map.get(host) or curr_map.get(workstation_key) or []))
            entry.setdefault("disconnect_cameras", list(disc_map.get(host) or disc_map.get(workstation_key) or []))
        else:
            entry.setdefault("current_polling_cameras", [])
            entry.setdefault("disconnect_cameras", [])

        keep_ids = _ids_from_assignment(entry)
        cameras = []
        for cid in sorted(keep_ids):
            path = self.camera_path(cid)
            if path.is_file():
                cameras.append(self.load_camera_config(cid))

        cfg["workstation_ip_mapping"] = {workstation_key: entry}
        cfg["cameras"] = cameras
        cfg["stage2_cams"] = derive_stage2_cams(cameras)
        return cfg

    def save_total_config(self, total_config: Dict[str, Any]) -> None:
        """將舊版 total config 形狀的資料寫回分散式設定檔結構。"""
        write_distributed_config(total_config, self.root, overwrite=True)


def _ids_from_assignment(entry: Any) -> Set[int]:
    """從工作站分派 entry 中取出 polling 與 fixed 的頻道號集合。"""
    if isinstance(entry, dict):
        raw = (entry.get("polling_cameras") or []) + (entry.get("fixed_cameras") or [])
    elif isinstance(entry, list):
        raw = entry
    else:
        raw = []
    out: Set[int] = set()
    for value in raw:
        try:
            out.add(int(value))
        except (TypeError, ValueError):
            pass
    return out


def split_total_config(total_config: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Any], List[Dict[str, Any]]]:
    """將舊版 total config 拆成 common、workstation assignments、camera configs 三部分。"""
    common_settings = deepcopy(total_config)
    workstation_assignments = deepcopy(common_settings.pop("workstation_ip_mapping", {}))
    cameras = deepcopy(common_settings.pop("cameras", []))
    common_settings.pop("stage2_cams", None)
    common_settings.pop("upload_ip", None)
    if not isinstance(workstation_assignments, dict):
        raise DistributedConfigError("workstation_ip_mapping must be an object")
    if not isinstance(cameras, list):
        raise DistributedConfigError("cameras must be an array")
    normalized_cameras = [_normalize_camera_channel(cam) for cam in cameras if isinstance(cam, dict)]
    return common_settings, workstation_assignments, normalized_cameras


def write_distributed_config(
    total_config: Dict[str, Any],
    distributed_root: Union[str, Path],
    *,
    overwrite: bool = False,
) -> Dict[str, Any]:
    """把舊版 total config 寫成 distributed_config 目錄結構。"""
    root = Path(distributed_root).expanduser().resolve()
    camera_dir = root / CAMERA_CONFIGS_DIR
    common_settings, workstation_assignments, cameras = split_total_config(total_config)

    seen: Dict[int, Dict[str, Any]] = {}
    for cam in cameras:
        cid = _channel_id(cam.get("NVR_channel"))
        if cid in seen:
            raise DistributedConfigError(f"Duplicate NVR_channel: {cid}")
        seen[cid] = cam

    if root.exists() and not overwrite:
        existing = [
            root / COMMON_SETTINGS_FILE,
            root / WORKSTATION_ASSIGNMENTS_FILE,
            camera_dir,
        ]
        if any(p.exists() for p in existing):
            raise FileExistsError(f"Distributed config already exists: {root}")

    root.mkdir(parents=True, exist_ok=True)
    camera_dir.mkdir(parents=True, exist_ok=True)

    _atomic_write_json(root / COMMON_SETTINGS_FILE, common_settings)
    _atomic_write_json(root / WORKSTATION_ASSIGNMENTS_FILE, workstation_assignments)

    if overwrite:
        for path in camera_dir.glob("camera_channel_*.json"):
            path.unlink()

    for cid, cam in sorted(seen.items()):
        _atomic_write_json(camera_dir / _camera_filename(cid), cam)

    return {
        "root": str(root),
        "common_settings": str(root / COMMON_SETTINGS_FILE),
        "workstation_assignments": str(root / WORKSTATION_ASSIGNMENTS_FILE),
        "camera_configs_dir": str(camera_dir),
        "camera_count": len(seen),
        "channels": sorted(seen),
    }


def compare_total_configs(expected: Dict[str, Any], actual: Dict[str, Any]) -> Dict[str, Any]:
    """比較兩份 total config，先正規化 cameras 排序以降低格式差異干擾。"""
    expected_norm = deepcopy(expected)
    actual_norm = deepcopy(actual)
    if isinstance(expected_norm.get("cameras"), list):
        expected_norm["cameras"] = sorted(
            [_normalize_camera_channel(cam) for cam in expected_norm["cameras"]],
            key=_sort_camera_key,
        )
    if isinstance(actual_norm.get("cameras"), list):
        actual_norm["cameras"] = sorted(
            [_normalize_camera_channel(cam) for cam in actual_norm["cameras"]],
            key=_sort_camera_key,
        )

    ok = expected_norm == actual_norm
    details: Dict[str, Any] = {"ok": ok}
    if not ok:
        details["expected_keys"] = list(expected_norm.keys())
        details["actual_keys"] = list(actual_norm.keys())
        details["camera_count_expected"] = len(expected_norm.get("cameras", []) or [])
        details["camera_count_actual"] = len(actual_norm.get("cameras", []) or [])
    return details


def validate_distributed_config(store: DistributedConfigStore) -> Dict[str, Any]:
    """檢查分散式設定是否可組裝，並確認工作站分派的頻道都有對應 camera 檔。"""
    cfg = store.build_total_config(fallback_to_legacy=False)
    assignments = cfg.get("workstation_ip_mapping", {}) or {}
    cameras = cfg.get("cameras", []) or []
    camera_ids = {_channel_id(cam.get("NVR_channel")) for cam in cameras}
    assignment_ids = set()
    missing_ids = set()
    for entry in assignments.values():
        ids = _ids_from_assignment(entry)
        assignment_ids |= ids
        missing_ids |= {cid for cid in ids if cid not in camera_ids}

    return {
        "ok": not missing_ids,
        "camera_count": len(cameras),
        "workstation_count": len(assignments),
        "assigned_camera_count": len(assignment_ids),
        "missing_camera_ids": sorted(missing_ids),
        "common_has_cameras": "cameras" in store.load_common_settings(),
        "common_has_workstation_ip_mapping": "workstation_ip_mapping" in store.load_common_settings(),
    }
