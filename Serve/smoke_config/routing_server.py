# routing_server.py
# -*- coding: utf-8 -*-
"""
routing_server.py — 工作站入口 API（統一回傳格式版）

角色：
  - 僅接受「工作站」上報，並把狀態轉送到 smoke_UI_server（APIServer）。
  - 接收工作站/上傳器傳來的影像檔，直接落地到 input 目錄，供偵測管線讀取。

端點：
  - GET  /healthz
  - POST /disconnection_report     -> 轉送至 {UI_SERVER_BASE}/status/disconnection_process
  - POST /polling_report           -> 轉送至 {UI_SERVER_BASE}/status/polling_cams_process
  - POST /coordinate/geo_reference -> 接收工作站座標校正結果，更新中央設定並重新派發
  - POST /coordinate/geo_reference/snapshot -> 批次唯讀回傳目前中央座標設定
  - POST /coordinate/geo_reference/batch -> 驗證後批次更新座標並只重新派發一次
  - POST /upload                   -> 僅保存圖片檔到 UPLOAD_FOLDER（不更名、不讀 NVR_channel）

環境變數：
  - UI_SERVER_BASE       : UI 後端（smoke_UI_server）Base URL（預設 http://127.0.0.1:58080）
  - UI_FORWARD_TOKEN     : （可選）若 UI 端驗證，需要 Authorization: Bearer <token>
  - FORWARD_TIMEOUT_SEC  : 轉送 UI 的逾時（秒，預設 5）
  - ROUTING_HOST         : 本服務綁定 host（預設 0.0.0.0）
  - ROUTING_PORT         : 本服務綁定 port（預設 5020）
  - UPLOAD_FOLDER        : 影像落地資料夾（預設 ./input）
  - UPLOAD_MAX_MB        : 接受的上傳大小上限（MB）（預設 50）
  - SOURCE_IP_ALLOWLIST  : （可選）允許的來源 IP（逗號分隔）。留空=不限制。
  - SMOKE_TOTAL_CONFIG_PATH : （可選）ConfigDistributor 相容參數，預設 smoke_camera_config.json。

回傳格式（已統一）：
  - 成功：{ "code": "OK", "message": "<文字>", "data": { ... } }
  - 失敗：{ "code": "<ERR_CODE>", "message": "<原因>", "data": { ...（如需） } }
"""

from __future__ import annotations
import os
import sys
import time
import logging
import threading
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from ipaddress import ip_address

import requests
from flask import Flask, request, jsonify
from werkzeug.utils import secure_filename

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from smoke_config.config_controller import ConfigDistributor
from smoke_config.distributed_config_store import DistributedConfigStore

# =========================
# 設定
# =========================
UI_SERVER_BASE       = os.getenv("UI_SERVER_BASE", "http://127.0.0.1:58080").rstrip("/")  # 與新版 APIServer 對齊
UI_FORWARD_TOKEN     = os.getenv("UI_FORWARD_TOKEN", "").strip()
FORWARD_TIMEOUT_SEC  = float(os.getenv("FORWARD_TIMEOUT_SEC", "5"))

ROUTING_HOST         = os.getenv("ROUTING_HOST", "0.0.0.0")
ROUTING_PORT         = int(os.getenv("ROUTING_PORT", "5020"))

UPLOAD_FOLDER        = os.getenv("UPLOAD_FOLDER", str(PROJECT_ROOT / "input"))
UPLOAD_MAX_MB        = int(os.getenv("UPLOAD_MAX_MB", "50"))

# 允許的來源 IP（逗號分隔）；空字串代表不限制
SOURCE_IP_ALLOWLIST  = [x.strip() for x in os.getenv("SOURCE_IP_ALLOWLIST", "").split(",") if x.strip()]

# 新版 APIServer 的狀態路徑（皆加上 status/）
UI_ENDPOINT_DISCONN  = "/status/disconnection_process"
UI_ENDPOINT_POLLING  = "/status/polling_cams_process"

# =========================
# App / Logger
# =========================
app = Flask(__name__)
app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER
CONFIG_UPDATE_LOCK = threading.Lock()
CONFIG_DISPATCH_LOCK = threading.Lock()


class BatchCoordinateWriteError(RuntimeError):
    """A batch write failed after the server had started touching camera files."""

    def __init__(self, message: str, rollback_errors: list[str]) -> None:
        super().__init__(message)
        self.rollback_errors = rollback_errors

logging.basicConfig(
    level=logging.INFO,
    format="[routing_server] %(asctime)s %(levelname)s: %(message)s"
)
log = logging.getLogger("routing_server")


# =========================
# [MOD] 統一回傳：{ code, message, data }
# =========================
def _resp(status: int, code: str, message: str, data: Optional[Dict[str, Any]] = None) -> Tuple[Any, int]:
    payload = {"code": code, "message": message, "data": data or {}}
    return jsonify(payload), status

def _ok(message: str = "success", **data) -> Tuple[Any, int]:
    """成功回傳統一格式。"""
    return _resp(200, "OK", message, data)

def _err(status: int, code: str, message: str, **data) -> Tuple[Any, int]:
    """錯誤回傳統一格式。"""
    return _resp(status, code, message, data or None)

def _now_ts() -> int:
    return int(time.time())

def _fwd_headers() -> Dict[str, str]:
    h = {"Content-Type": "application/json"}
    if UI_FORWARD_TOKEN:
        h["Authorization"] = f"Bearer {UI_FORWARD_TOKEN}"
    return h

def _require_json() -> Dict[str, Any]:
    if not request.is_json:
        raise ValueError("content-type must be application/json")
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise ValueError("invalid JSON body")
    return data

def _client_ip() -> str:
    # 若有反代，可改讀 X-Forwarded-For
    return request.headers.get("X-Forwarded-For", request.remote_addr or "")

def _check_allowlist() -> Optional[Tuple[Any, int]]:
    """若設定了 SOURCE_IP_ALLOWLIST，檢查來源 IP 是否允許。"""
    if not SOURCE_IP_ALLOWLIST:
        return None  # 無限制
    ip = _client_ip()
    try:
        ip_address(ip)  # 合法性檢查
    except Exception:
        return _err(403, "FORBIDDEN", f"invalid client ip: {ip}")
    if ip not in SOURCE_IP_ALLOWLIST:
        return _err(403, "FORBIDDEN", f"ip not allowed: {ip}")
    return None

def _reject_oversize() -> Optional[Tuple[Any, int]]:
    """使用 Content-Length 粗略檢查上傳大小。"""
    if request.content_length is None:
        return None  # 不知道大小，放行
    max_bytes = UPLOAD_MAX_MB * 1024 * 1024
    if request.content_length > max_bytes:
        return _err(413, "PAYLOAD_TOO_LARGE",
                    f"upload exceeds {UPLOAD_MAX_MB}MB limit")
    return None


def _normalize_camera_ip(camera_ip: Any) -> str:
    return str(camera_ip or "").strip().replace("_", ".")


def _parse_channel_id(value: Any) -> Optional[int]:
    if value in (None, ""):
        return None
    try:
        channel_id = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("nvr_channel must be an integer") from exc
    if channel_id <= 0:
        raise ValueError("nvr_channel must be a positive integer")
    return channel_id


def _as_number(value: Any, field_name: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be a number") from exc


def _as_image_coordinate(value: Any, field_name: str) -> int:
    return int(round(_as_number(value, field_name)))


def _validate_geo_reference(value: Any) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("geo_reference must be an object")

    image_name = str(value.get("image_name") or "").strip()
    if not image_name:
        raise ValueError("geo_reference.image_name is required")

    save_time = str(value.get("save_time") or "").strip() or time.strftime("%Y-%m-%d %H:%M:%S")
    points = value.get("points")
    if not isinstance(points, list):
        raise ValueError("geo_reference.points must be an array")

    normalized_points = []
    for index, point in enumerate(points):
        if not isinstance(point, dict):
            raise ValueError(f"geo_reference.points[{index}] must be an object")

        point_name = str(point.get("point_name") or "").strip()
        if not point_name:
            raise ValueError(f"geo_reference.points[{index}].point_name is required")

        image_coord = point.get("image_coord")
        wgs84_coords = point.get("wgs84_coords")
        if not isinstance(image_coord, dict):
            raise ValueError(f"geo_reference.points[{index}].image_coord must be an object")
        if not isinstance(wgs84_coords, dict):
            raise ValueError(f"geo_reference.points[{index}].wgs84_coords must be an object")

        normalized_points.append(
            {
                "point_name": point_name,
                "image_coord": {
                    "x": _as_image_coordinate(image_coord.get("x"), f"geo_reference.points[{index}].image_coord.x"),
                    "y": _as_image_coordinate(image_coord.get("y"), f"geo_reference.points[{index}].image_coord.y"),
                },
                "wgs84_coords": {
                    "x": _as_number(wgs84_coords.get("x"), f"geo_reference.points[{index}].wgs84_coords.x"),
                    "y": _as_number(wgs84_coords.get("y"), f"geo_reference.points[{index}].wgs84_coords.y"),
                },
            }
        )

    return {
        "image_name": image_name,
        "save_time": save_time,
        "points": normalized_points,
    }


def _resolve_camera(store: DistributedConfigStore, camera_ip: str, channel_id: Optional[int]) -> Tuple[int, Dict[str, Any]]:
    normalized_ip = _normalize_camera_ip(camera_ip)

    if channel_id is not None:
        camera = store.load_camera_config(channel_id)
        if normalized_ip and _normalize_camera_ip(camera.get("ip")) != normalized_ip:
            raise ValueError(
                f"camera_ip does not match nvr_channel: {camera_ip} != {camera.get('ip')}"
            )
        return channel_id, camera

    if not normalized_ip:
        raise ValueError("camera_ip or nvr_channel is required")

    for camera in store.load_all_camera_configs():
        if _normalize_camera_ip(camera.get("ip")) == normalized_ip:
            return int(camera["NVR_channel"]), camera

    raise ValueError(f"camera not found: {camera_ip}")


def _validate_geo_reference_batch(payload: Dict[str, Any]) -> list[Dict[str, Any]]:
    updates = payload.get("updates")
    if not isinstance(updates, list):
        raise ValueError("updates must be an array")
    if not updates:
        raise ValueError("updates must not be empty")

    normalized_updates: list[Dict[str, Any]] = []
    seen_raw_targets: set[Tuple[str, int]] = set()
    for index, update in enumerate(updates):
        if not isinstance(update, dict):
            raise ValueError(f"updates[{index}] must be an object")

        camera_ip = _normalize_camera_ip(update.get("camera_ip"))
        if not camera_ip:
            raise ValueError(f"updates[{index}].camera_ip is required")

        channel_id = _parse_channel_id(update.get("nvr_channel"))
        if channel_id is None:
            raise ValueError(f"updates[{index}].nvr_channel is required")

        raw_target = (camera_ip, channel_id)
        if raw_target in seen_raw_targets:
            raise ValueError(
                f"updates[{index}] is a duplicate target: camera_ip={camera_ip}, nvr_channel={channel_id}"
            )
        seen_raw_targets.add(raw_target)

        normalized_updates.append(
            {
                "camera_ip": camera_ip,
                "nvr_channel": channel_id,
                "geo_reference": _validate_geo_reference(update.get("geo_reference")),
            }
        )

    return normalized_updates


def _validate_geo_reference_snapshot_request(payload: Dict[str, Any]) -> list[Dict[str, Any]]:
    cameras = payload.get("cameras")
    if not isinstance(cameras, list):
        raise ValueError("cameras must be an array")
    if not cameras:
        raise ValueError("cameras must not be empty")

    normalized_cameras: list[Dict[str, Any]] = []
    seen_channels: set[int] = set()
    for index, camera in enumerate(cameras):
        if not isinstance(camera, dict):
            raise ValueError(f"cameras[{index}] must be an object")

        camera_ip = _normalize_camera_ip(camera.get("camera_ip"))
        if not camera_ip:
            raise ValueError(f"cameras[{index}].camera_ip is required")

        channel_id = _parse_channel_id(camera.get("nvr_channel"))
        if channel_id is None:
            raise ValueError(f"cameras[{index}].nvr_channel is required")
        if channel_id in seen_channels:
            raise ValueError(f"cameras[{index}] is a duplicate channel: {channel_id}")
        seen_channels.add(channel_id)

        normalized_cameras.append(
            {
                "camera_ip": camera_ip,
                "nvr_channel": channel_id,
            }
        )

    return normalized_cameras


def _apply_geo_reference_batch(
    store: DistributedConfigStore,
    updates: list[Dict[str, Any]],
) -> list[Dict[str, Any]]:
    """Resolve and snapshot every target before writing any camera file."""
    prepared: list[Dict[str, Any]] = []
    seen_channels: set[int] = set()

    for index, update in enumerate(updates):
        try:
            resolved_channel_id, camera = _resolve_camera(
                store,
                update["camera_ip"],
                update["nvr_channel"],
            )
        except FileNotFoundError as exc:
            raise ValueError(
                f"updates[{index}] camera not found: nvr_channel={update['nvr_channel']}"
            ) from exc

        if resolved_channel_id in seen_channels:
            raise ValueError(
                f"updates[{index}] is a duplicate target: nvr_channel={resolved_channel_id}"
            )
        seen_channels.add(resolved_channel_id)

        prepared.append(
            {
                "channel_id": resolved_channel_id,
                "snapshot": deepcopy(camera),
                "geo_reference": update["geo_reference"],
            }
        )

    written_channels: list[int] = []
    try:
        for item in prepared:
            channel_id = item["channel_id"]
            updated_camera = deepcopy(item["snapshot"])
            updated_camera["geo_reference"] = deepcopy(item["geo_reference"])
            # Record before calling save so a custom/underlying writer that fails
            # after replacing a file is also given a rollback attempt.
            written_channels.append(channel_id)
            store.save_camera_config(channel_id, updated_camera)
    except Exception as exc:
        rollback_errors: list[str] = []
        snapshots_by_channel = {item["channel_id"]: item["snapshot"] for item in prepared}
        for channel_id in reversed(written_channels):
            try:
                store.save_camera_config(channel_id, snapshots_by_channel[channel_id])
            except Exception as rollback_exc:
                rollback_errors.append(f"nvr_channel={channel_id}: {rollback_exc}")

        rollback_status = "completed" if not rollback_errors else "incomplete"
        raise BatchCoordinateWriteError(
            "批次座標寫入失敗；已嘗試回復已寫入攝影機 "
            f"（rollback={rollback_status}）：{exc}",
            rollback_errors,
        ) from exc

    return [
        {
            "camera_ip": _normalize_camera_ip(item["snapshot"].get("ip")),
            "nvr_channel": str(item["channel_id"]),
            "point_count": len(item["geo_reference"].get("points", [])),
        }
        for item in prepared
    ]


def _dispatch_distributed_config(store: DistributedConfigStore) -> Dict[str, Any]:
    distributor = ConfigDistributor(
        os.getenv("SMOKE_TOTAL_CONFIG_PATH", "smoke_camera_config.json"),
        config_store=store,
    )
    return distributor.distribute_now()


def _dispatch_distributed_config_in_background(reason: str) -> None:
    def _run() -> None:
        try:
            with CONFIG_DISPATCH_LOCK:
                dispatch_result = _dispatch_distributed_config(DistributedConfigStore())
            dispatch_summary = _summarize_dispatch_result(dispatch_result)
            log.info(
                "background distributed_config dispatch finished: reason=%s summary=%s",
                reason,
                dispatch_summary,
            )
        except Exception:
            log.exception("background distributed_config dispatch failed: reason=%s", reason)

    thread = threading.Thread(
        target=_run,
        name="distributed-config-dispatch",
        daemon=True,
    )
    thread.start()


def _summarize_dispatch_result(dispatch_result: Dict[str, Any]) -> Dict[str, int]:
    summary = {"success": 0, "failed": 0, "skipped": 0}
    if not isinstance(dispatch_result, dict):
        summary["failed"] = 1
        return summary

    if dispatch_result.get("ok") is False and "error" in dispatch_result:
        summary["failed"] = 1
        return summary

    for result in dispatch_result.values():
        if not isinstance(result, dict):
            continue
        if result.get("skipped"):
            summary["skipped"] += 1
        elif result.get("ok") is False:
            summary["failed"] += 1
        elif result.get("ok"):
            summary["success"] += 1

    return summary


# =========================
# 健康檢查
# =========================
@app.get("/healthz")
def healthz():
    return _ok(
        "service is healthy",
        service="routing_server",
        time=_now_ts(),
        ui_server=UI_SERVER_BASE,
        upload_folder=app.config["UPLOAD_FOLDER"],
        forward_timeout_sec=FORWARD_TIMEOUT_SEC,
        allowlist_enabled=bool(SOURCE_IP_ALLOWLIST),
        ui_endpoints={
            "disconnection": UI_ENDPOINT_DISCONN,
            "polling": UI_ENDPOINT_POLLING,
        }
    )


# =========================
# 工作站：斷線回報 → 轉送到 UI
# =========================
@app.post("/disconnection_report")
def disconnection_report():
    """
    期待 payload：
      {
        "workstation_ip": "192.168.1.10",
        "disconnect_cameras": [7, 8]
      }
    """
    # 來源 IP 控制（如啟用）
    deny = _check_allowlist()
    if deny:
        return deny

    try:
        data = _require_json()
        ws   = data.get("workstation_ip")
        cams = data.get("disconnect_cameras")

        if not ws or not isinstance(cams, list):
            return _err(400, "INVALID_PAYLOAD",
                        "missing workstation_ip or disconnect_cameras")

        url = f"{UI_SERVER_BASE}{UI_ENDPOINT_DISCONN}"
        log.info("forwarding disconnection_report -> %s", url)
        resp = requests.post(
            url, json={"workstation_ip": ws, "disconnect_cameras": cams},
            headers=_fwd_headers(), timeout=FORWARD_TIMEOUT_SEC
        )

        # 整理 UI 回應
        body: Any = {}
        try:
            body = resp.json()
        except Exception:
            body = {"text": resp.text}

        if resp.ok:
            return _ok(
                "forwarded to UI successfully",
                forward_status=resp.status_code,
                forward_url=url,
                ui_body=body
            )
        else:
            return _err(resp.status_code, "FORWARD_FAILED", "UI responded with error",
                        forward_status=resp.status_code,
                        forward_url=url,
                        ui_body=body)

    except ValueError as e:
        return _err(400, "INVALID_PAYLOAD", str(e))
    except requests.RequestException as e:
        log.exception("disconnection_report forward error")
        return _err(502, "BAD_GATEWAY", f"forward failed: {e}")
    except Exception as e:
        log.exception("disconnection_report error")
        return _err(500, "SERVER_ERROR", str(e))


# =========================
# 工作站：目前輪詢回報 → 轉送到 UI
# =========================
@app.post("/polling_report")
def polling_report():
    """
    期待 payload：
      {
        "workstation_ip": "192.168.1.10",
        "current_polling_cameras": [1, 2, 3]
      }
    """
    # 來源 IP 控制（如啟用）
    deny = _check_allowlist()
    if deny:
        return deny

    try:
        data = _require_json()
        ws   = data.get("workstation_ip")
        cams = data.get("current_polling_cameras")
        # [MOD] 1. 讀取 restarted 欄位 (預設為 False)
        is_restarted = data.get("restarted", False) 

        if not ws or not isinstance(cams, list):
            return _err(400, "INVALID_PAYLOAD",
                        "missing workstation_ip or current_polling_cameras")

        url = f"{UI_SERVER_BASE}{UI_ENDPOINT_POLLING}"
        log.info("forwarding polling_report -> %s (restarted=%s)", url, is_restarted)
        
        # [MOD] 2. 在轉發的 json 中加入 restarted
        resp = requests.post(
            url, 
            json={
                "workstation_ip": ws, 
                "current_polling_cameras": cams,
                "restarted": is_restarted  # <--- 關鍵修改
            },
            headers=_fwd_headers(), timeout=FORWARD_TIMEOUT_SEC
        )
        body: Any = {}
        try:
            body = resp.json()
        except Exception:
            body = {"text": resp.text}

        if resp.ok:
            return _ok(
                "forwarded to UI successfully",
                forward_status=resp.status_code,
                forward_url=url,
                ui_body=body
            )
        else:
            return _err(resp.status_code, "FORWARD_FAILED", "UI responded with error",
                        forward_status=resp.status_code,
                        forward_url=url,
                        ui_body=body)

    except ValueError as e:
        return _err(400, "INVALID_PAYLOAD", str(e))
    except requests.RequestException as e:
        log.exception("polling_report forward error")
        return _err(502, "BAD_GATEWAY", f"forward failed: {e}")
    except Exception as e:
        log.exception("polling_report error")
        return _err(500, "SERVER_ERROR", str(e))


# =========================
# 工作站：座標校正更新
# =========================
@app.post("/coordinate/geo_reference")
def update_camera_geo_reference():
    """
    JSON:
      {
        "camera_ip": "192.168.1.10",
        "nvr_channel": "12",
        "geo_reference": {
          "image_name": "...jpg",
          "save_time": "YYYY-MM-DD HH:MM:SS",
          "points": [...]
        }
      }
    動作：
      - 更新 A40 中央 distributed_config 的單支 camera 設定。
      - 寫入成功後立即回應，並在背景把完整 distributed_config 重新派發到所有工作站。
    """
    deny = _check_allowlist()
    if deny:
        return deny

    if not request.is_json:
        return _err(400, "INVALID_PAYLOAD", "content-type must be application/json")

    try:
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return _err(400, "INVALID_PAYLOAD", "invalid JSON body")

        camera_ip = _normalize_camera_ip(payload.get("camera_ip"))
        channel_id = _parse_channel_id(payload.get("nvr_channel"))
        geo_reference = _validate_geo_reference(payload.get("geo_reference"))

        with CONFIG_UPDATE_LOCK:
            store = DistributedConfigStore()
            resolved_channel_id, camera = _resolve_camera(store, camera_ip, channel_id)
            camera["geo_reference"] = geo_reference
            store.save_camera_config(resolved_channel_id, camera)

        dispatch_reason = f"geo_reference channel={resolved_channel_id}"
        _dispatch_distributed_config_in_background(dispatch_reason)

        log.info(
            "updated geo_reference: channel=%s ip=%s points=%d dispatch=queued",
            resolved_channel_id,
            camera.get("ip"),
            len(geo_reference.get("points", [])),
        )
        return _ok(
            "Geo reference updated; distribution was queued",
            camera_ip=camera.get("ip"),
            nvr_channel=str(resolved_channel_id),
            dispatch_status="queued",
        )

    except (FileNotFoundError, ValueError) as e:
        log.warning("geo_reference update rejected: %s", e)
        return _err(400, "INVALID_PAYLOAD", str(e))
    except Exception as e:
        log.exception("geo_reference update failed")
        return _err(500, "SERVER_ERROR", str(e))


@app.post("/coordinate/geo_reference/snapshot")
def get_camera_geo_reference_snapshot():
    """Return current A40 coordinate data for multiple cameras without writing."""
    deny = _check_allowlist()
    if deny:
        return deny

    if not request.is_json:
        return _err(400, "INVALID_PAYLOAD", "content-type must be application/json")

    try:
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return _err(400, "INVALID_PAYLOAD", "invalid JSON body")

        normalized_cameras = _validate_geo_reference_snapshot_request(payload)
        snapshots: list[Dict[str, Any]] = []
        with CONFIG_UPDATE_LOCK:
            store = DistributedConfigStore()
            for target in normalized_cameras:
                channel_id, camera = _resolve_camera(
                    store,
                    target["camera_ip"],
                    target["nvr_channel"],
                )
                snapshots.append(
                    {
                        "camera_ip": camera.get("ip"),
                        "nvr_channel": str(channel_id),
                        "geo_reference": deepcopy(camera.get("geo_reference")),
                    }
                )

        return _ok("Geo reference snapshot loaded", cameras=snapshots)

    except (FileNotFoundError, ValueError) as exc:
        log.warning("geo_reference snapshot rejected: %s", exc)
        return _err(400, "INVALID_PAYLOAD", str(exc))
    except Exception as exc:
        log.exception("geo_reference snapshot failed")
        return _err(500, "SERVER_ERROR", str(exc))


@app.post("/coordinate/geo_reference/batch")
def update_camera_geo_reference_batch():
    """
    JSON:
      {
        "updates": [
          {
            "camera_ip": "192.168.1.10",
            "nvr_channel": "12",
            "geo_reference": {"image_name": "...jpg", "save_time": "...", "points": [...]}
          }
        ]
      }

    The whole request is resolved and snapshotted while CONFIG_UPDATE_LOCK is
    held.  No camera file is written until every update passes validation.
    """
    deny = _check_allowlist()
    if deny:
        return deny

    if not request.is_json:
        return _err(400, "INVALID_PAYLOAD", "content-type must be application/json")

    try:
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return _err(400, "INVALID_PAYLOAD", "invalid JSON body")

        with CONFIG_UPDATE_LOCK:
            normalized_updates = _validate_geo_reference_batch(payload)
            store = DistributedConfigStore()
            updated_targets = _apply_geo_reference_batch(store, normalized_updates)

        _dispatch_distributed_config_in_background(
            f"geo_reference batch count={len(updated_targets)}"
        )

        log.info(
            "updated geo_reference batch: count=%s dispatch=queued",
            len(updated_targets),
        )
        return _ok(
            "Geo reference batch updated; distribution was queued",
            updated_count=len(updated_targets),
            updated_targets=updated_targets,
            dispatch_status="queued",
        )

    except (FileNotFoundError, ValueError) as exc:
        log.warning("geo_reference batch rejected: %s", exc)
        return _err(400, "INVALID_PAYLOAD", str(exc))
    except BatchCoordinateWriteError as exc:
        log.exception("geo_reference batch write failed")
        return _err(
            500,
            "BATCH_WRITE_FAILED",
            str(exc),
            updated_count=0,
            rollback_status="completed" if not exc.rollback_errors else "incomplete",
            rollback_errors=exc.rollback_errors,
            dispatch_status="not_queued",
        )
    except Exception as exc:
        log.exception("geo_reference batch update failed")
        return _err(500, "SERVER_ERROR", str(exc), dispatch_status="not_queued")


# =========================
# 工作站：影像上傳（只存檔，不更名）
# =========================
@app.post("/upload")
def upload_file():
    """
    multipart/form-data:
      - file: 二進位影像 (必填)
    動作：
      - 僅以上傳的「原始檔名」（經 secure_filename 安全化）存入 UPLOAD_FOLDER。
      - 不讀也不改任何通道資訊；模型會依『檔名』自行辨識。

    回應（成功）：
      { "code": "OK", "message": "File uploaded successfully", "data": { "path": "<實際儲存路徑>" } }
    """
    # 來源 IP 控制（如啟用）
    deny = _check_allowlist()
    if deny:
        return deny

    # 大小限制（以 Content-Length 為準）
    oversize = _reject_oversize()
    if oversize:
        return oversize

    try:
        if "file" not in request.files:
            return _err(400, "NO_FILE_PART", "No file part")

        file = request.files["file"]
        if file.filename == "":
            return _err(400, "NO_SELECTED_FILE", "No selected file")

        # 僅安全化，不改名，保留語意
        filename = secure_filename(file.filename)
        if not filename:
            return _err(400, "INVALID_FILENAME", "invalid filename after sanitization")

        # 確保目錄存在
        os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)

        save_path = os.path.join(app.config["UPLOAD_FOLDER"], filename)
        file.save(save_path)
        log.info("saved upload -> %s", save_path)

        return _ok("File uploaded successfully", path=save_path)

    except Exception as e:
        log.exception("upload error")
        return _err(500, "SERVER_ERROR", str(e))


# =========================
# 啟動
# =========================
if __name__ == "__main__":
    log.info(
        "starting routing_server @ %s:%s  -> UI=%s  UPLOAD=%s  MAX=%dMB  allowlist=%s",
        ROUTING_HOST, ROUTING_PORT, UI_SERVER_BASE, UPLOAD_FOLDER, UPLOAD_MAX_MB,
        ",".join(SOURCE_IP_ALLOWLIST) if SOURCE_IP_ALLOWLIST else "<disabled>",
    )
    app.run(host=ROUTING_HOST, port=ROUTING_PORT)
