# -*- coding: utf-8 -*-
"""
config_controller.py — ConfigDistributor

派發規則：
- 不再把設定組成每台工作站各自的切片 JSON。
- 直接把 distributed_config 三個檔案區塊完整派到每台工作站的 /update_config：
  common_settings.json、workstation_assignments.json、camera_configs/*.json。
- 工作站收到後自行落地成相同的 distributed_config 目錄，並依自己的 IP 讀取分派。

傳輸目標：
- 派發 URL 由 (host, port) 組：host 來自 mapping 的 key（或正規化後主機），
  port 優先採用該 key 形如 "ip:port" 的埠；沒有則用該站 value 內的 "port"；
  再沒有則用環境變數 WORKSTATION_DEFAULT_PORT（預設 5001）。
"""

from __future__ import annotations

import os
import json
import time
import logging
import hashlib
import threading
from copy import deepcopy
from typing import Optional, Dict, Any, List, Tuple

import requests

from smoke_config.distributed_config_store import DistributedConfigStore


log = logging.getLogger("ConfigDistributor")
if not log.handlers:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


def _sha(obj: Any) -> str:
    """對 JSON 物件做穩定序列化後 sha1。"""
    try:
        s = json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except Exception:
        s = str(obj)
    return hashlib.sha1(s.encode("utf-8")).hexdigest()


def _normalize_ws_key(ws_ip: str) -> Tuple[str, Optional[int]]:
    """
    將 workstation key 正規化為 (host, port?)：
    - 僅處理單一冒號且右半可轉 int 的情形（避免干擾 IPv6）
    - 其它情況回傳 (原字串, None)
    """
    if not isinstance(ws_ip, str):
        return str(ws_ip), None
    if ":" in ws_ip and ws_ip.count(":") == 1 and not ws_ip.startswith("["):
        host, maybe_port = ws_ip.split(":", 1)
        try:
            return host, int(maybe_port)
        except ValueError:
            return ws_ip, None
    return ws_ip, None


def _as_ws_entry(val: Any) -> Dict[str, Any]:
    """
    將 mapping 的 value 規格化為 dict：
      - dict：直接淺拷貝並保留 port
      - list：視為 polling_cameras
      - 其它：空集合
    """
    if isinstance(val, dict):
        out = dict(val)
        out.setdefault("polling_cameras", [])
        out.setdefault("fixed_cameras", [])
        return out
    elif isinstance(val, list):
        return {"polling_cameras": list(val), "fixed_cameras": []}
    else:
        return {"polling_cameras": [], "fixed_cameras": []}


class ConfigDistributor:
    def __init__(
        self,
        total_config_path: str,
        *,
        default_port: Optional[int] = None,
        debounce_seconds: float = 0.5,
        http_timeout: float = 3.0,
        status_provider: Optional[object] = None,
        session: Optional[requests.Session] = None,
        config_store: Optional[DistributedConfigStore] = None,
    ) -> None:
        """
        :param total_config_path: 總組態檔路徑（JSON）
        :param default_port: 預設工作站埠；未提供時取環境 WORKSTATION_DEFAULT_PORT（預設 5001）
        :param debounce_seconds: 去抖時間（秒）
        :param http_timeout: 單次派發 HTTP 逾時（秒）
        :param status_provider: 可選，若提供，應有
                                .get_current_polling_map() -> Dict[str, List[int]]
                                .get_disconnection_map()   -> Dict[str, List[int]]
                                （注意：依需求，我們不會把這些欄位新增到 payload，只用來決策或日誌）
        :param session: 注入 requests.Session 以複用連線；不提供則由類別自行建立
        :param config_store: 可選，使用拆分後的 distributed_config 派發 payload
        """
        self.total_config_path = total_config_path
        self.config_store = config_store or DistributedConfigStore(total_config_path)
        self.default_port = int(
            default_port
            if default_port is not None
            else os.environ.get("WORKSTATION_DEFAULT_PORT", "5001")
        )
        self.debounce_seconds = float(debounce_seconds)
        self.http_timeout = float(http_timeout)
        self.status_provider = status_provider

        self._lock = threading.Lock()
        self._pending: Optional[Tuple[int, str]] = None  # (version, reason)
        self._stop_evt = threading.Event()
        self._wakeup_evt = threading.Event()
        self._thread: Optional[threading.Thread] = None

        # 去重指紋
        self._last_whole_hash = ""
        self._last_slice_hash: Dict[str, str] = {}

        # 最近一次派發結果
        self._last_results: Dict[str, Any] = {}
        self._last_dispatch_ts: float = 0.0

        self._session = session or requests.Session()

    # ---------- 生命週期 ----------
    def start(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop_evt.clear()
            self._wakeup_evt.clear()
            self._thread = threading.Thread(target=self._worker_loop, name="config-distributor", daemon=True)
            self._thread.start()
            log.info("[dist] worker started (default_port=%s, debounce=%.2fs, timeout=%.1fs)",
                     self.default_port, self.debounce_seconds, self.http_timeout)

    def stop(self, timeout: float = 2.0) -> None:
        with self._lock:
            if not self._thread:
                return
            self._stop_evt.set()
            self._wakeup_evt.set()
            th = self._thread
            self._thread = None
        th.join(timeout=timeout)
        log.info("[dist] worker stopped")

    def is_alive(self) -> bool:
        t = self._thread
        return bool(t and t.is_alive())

    # ---------- 對外 API ----------
    def enqueue(self, *, version: int, reason: str = "unknown") -> None:
        """
        由上層在「成功寫入總組態檔」後呼叫。
        會「去抖」：短時間多次 enqueue 只會派發一次，採用最新 (version, reason)。
        """
        with self._lock:
            self._pending = (int(version), str(reason))
            self._wakeup_evt.set()

    def distribute_now(self) -> Dict[str, Any]:
        """
        立即派發一次（同步），不經過 worker 的去抖。
        不會更新去重指紋（避免影響 worker 決策）。
        """
        try:
            payload, ws_items = self._load_distributed_payload()
        except Exception as e:
            log.error("[dist] distribute_now: failed to load distributed config: %s", e, exc_info=True)
            return {"ok": False, "error": str(e)}

        results = self._send_all(payload, ws_items, update_hash=False)
        with self._lock:
            self._last_results = results
            self._last_dispatch_ts = time.time()
        return results

    def status(self) -> Dict[str, Any]:
        """回傳 worker 與最近一次派發的狀態摘要。"""
        with self._lock:
            p = self._pending
            t = self._thread
            return {
                "alive": bool(t and t.is_alive()),
                "pending": {"version": p[0], "reason": p[1]} if p else None,
                "last_results": deepcopy(self._last_results),
                "last_dispatch_ts": self._last_dispatch_ts,
                "last_whole_hash": self._last_whole_hash,
            }

    def get_last_results(self) -> Dict[str, Any]:
        with self._lock:
            return deepcopy(self._last_results)

    # ---------- 內部 ----------
    def _worker_loop(self) -> None:
        while not self._stop_evt.is_set():
            self._wakeup_evt.wait(timeout=0.5)
            if self._stop_evt.is_set():
                break

            with self._lock:
                pending = self._pending
                self._pending = None
                self._wakeup_evt.clear()

            if not pending:
                continue

            version, reason = pending
            # 去抖：等待 debounce_seconds，看有沒有新 enqueue
            t_end = time.time() + self.debounce_seconds
            while time.time() < t_end and not self._stop_evt.is_set():
                time.sleep(0.05)
                with self._lock:
                    if self._pending:
                        version, reason = self._pending
                        self._pending = None
                        t_end = time.time() + self.debounce_seconds

            try:
                payload, ws_items = self._load_distributed_payload()
            except Exception as e:
                log.error("[dist] worker: failed to load distributed config: %s", e, exc_info=True)
                continue

            log.info("[dist] dispatch begin → version=%s reason=%s", version, reason)
            results = self._send_all(payload, ws_items, update_hash=True)
            with self._lock:
                self._last_results = results
                self._last_dispatch_ts = time.time()
            log.info("[dist] dispatch end   → success=%s fail=%s skipped=%s",
                     sum(1 for r in results.values() if r.get("ok")),
                     sum(1 for r in results.values() if r.get("ok") is False),
                     sum(1 for r in results.values() if r.get("skipped")))

    def _load_distributed_payload(self) -> Tuple[Dict[str, Any], List[Tuple[str, str, Dict[str, Any], Optional[int]]]]:
        """讀取 distributed_config 原始檔案區塊，並產生所有工作站派發目標。"""
        if not self.config_store or not self.config_store.is_available():
            raise FileNotFoundError("Distributed config store is not available.")

        common_settings = self.config_store.load_common_settings()
        common_settings.pop("stage2_cams", None)
        common_settings.pop("upload_ip", None)
        workstation_assignments = self.config_store.load_workstation_assignments()
        camera_configs = {
            path.name: self.config_store.load_camera_config(int(path.stem.rsplit("_", 1)[-1]))
            for path in self.config_store.list_camera_paths()
        }

        ws_map_raw = workstation_assignments or {}
        # 保留原始 key 以便 payload 也用「原鍵名」
        ws_items: List[Tuple[str, str, Dict[str, Any], Optional[int]]] = []  # (orig_key, host, entry, port_in_key)

        for ws_key, val in ws_map_raw.items():
            host, port_in_key = _normalize_ws_key(ws_key)
            entry = _as_ws_entry(val)
            ws_items.append((ws_key, host, entry, port_in_key))

        payload = {
            "format": "distributed_config",
            "generated_at": int(time.time()),
            "common_settings": common_settings,
            "workstation_assignments": workstation_assignments,
            "camera_configs": camera_configs,
        }
        return payload, ws_items

    def _send_all(
        self,
        payload: Dict[str, Any],
        ws_items: List[Tuple[str, str, Dict[str, Any], Optional[int]]],
        *,
        update_hash: bool,
    ) -> Dict[str, Any]:
        """對所有工作站送出完整 distributed_config payload。"""
        payload_for_hash = deepcopy(payload)
        payload_for_hash.pop("generated_at", None)
        whole_hash = _sha(payload_for_hash)

        results: Dict[str, Any] = {}

        for orig_key, host, entry, port_in_key in ws_items:
            try:
                polling = entry.get("polling_cameras", []) or []
                fixed = entry.get("fixed_cameras", []) or []

                # 目標 port（只用於 HTTP 發送；不會寫回 payload）
                port = entry.get("port")
                if port is None:
                    port = port_in_key if port_in_key is not None else self.default_port
                try:
                    port = int(port)
                except Exception:
                    port = self.default_port

                # per-WS 去重保留，用完整 payload hash 即可；各站收到同一份設定。
                slice_hash = whole_hash

                if self._last_slice_hash.get(orig_key) == slice_hash:
                    results[orig_key] = {"skipped": True}
                    continue

                # log 摘要
                camera_count = len(payload.get("camera_configs", {}) or {})
                log.info("[dist] → POST http://%s:%s/update_config files=%d polling=%d fixed=%d",
                         host, port, camera_count, len(polling), len(fixed))

                r = self._post_ws(host, port, payload)
                results[orig_key] = {"ok": True, "status": r.status_code}
                if update_hash:
                    self._last_slice_hash[orig_key] = slice_hash

            except Exception as e:
                log.error("[dist] dispatch to %s failed: %s", orig_key, e, exc_info=True)
                results[orig_key] = {"ok": False, "error": str(e)}

        if update_hash:
            self._last_whole_hash = whole_hash

        return results

    def _post_ws(self, host: str, port: int, payload: Dict[str, Any]) -> requests.Response:
        """送到單一工作站的 /update_config。"""
        url = f"http://{host}:{int(port)}/update_config"
        r = self._session.post(url, json=payload, timeout=self.http_timeout)
        return r
