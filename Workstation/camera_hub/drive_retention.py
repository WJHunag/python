"""Persistent receipts and bounded Drive source retention (no API calls on import)."""
from __future__ import annotations

import calendar
import hashlib
import os
import sqlite3
import tempfile
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path


DEFAULT_STATE_PATH = Path(__file__).resolve().parents[1] / "runtime" / "drive_retention.sqlite3"


def parse_time(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def month_ago(now):
    year, month = (now.year - 1, 12) if now.month == 1 else (now.year, now.month - 1)
    return now.replace(year=year, month=month, day=min(now.day, calendar.monthrange(year, month)[1]))


def record_detection(store, group_id, crops):
    """Only explicit classifications prove a negative; stage-one detections are smoke candidates."""
    if not crops:
        return
    values = list(crops.values())
    if any(not crop.get('stage2_enabled') or crop.get('retention_result') == 'smoke' for crop in values):
        store.complete(group_id, smoke=True)
    elif (all(crop.get('retention_result') == 'steam' for crop in values)
          and all(crop.get('retention_total') == len(values) for crop in values)):
        store.complete(group_id, smoke=False)


class ReceiptStore:
    def __init__(self, path=DEFAULT_STATE_PATH):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS receipts (
                folder TEXT, file_id TEXT, modified TEXT, group_id TEXT,
                completed INTEGER DEFAULT 0, smoke INTEGER DEFAULT 1,
                backup_id TEXT, deleted INTEGER DEFAULT 0,
                PRIMARY KEY(folder, file_id))""")
            db.execute("CREATE INDEX IF NOT EXISTS receipt_group ON receipts(group_id)")
            db.execute("CREATE TABLE IF NOT EXISTS history (folder TEXT PRIMARY KEY, done INTEGER)")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(str(self.path), timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def register(self, folder, file_id, modified, group_id):
        with self.connect() as db:
            db.execute("""INSERT INTO receipts(folder,file_id,modified,group_id) VALUES(?,?,?,?)
                ON CONFLICT(folder,file_id) DO UPDATE SET group_id=excluded.group_id,
                completed=CASE WHEN modified=excluded.modified THEN completed ELSE 0 END,
                backup_id=CASE WHEN modified=excluded.modified THEN backup_id ELSE NULL END,
                deleted=0, modified=excluded.modified""", (folder, file_id, modified, group_id))

    def complete(self, group_id, smoke):
        with self.connect() as db:
            db.execute("UPDATE receipts SET completed=1, smoke=? WHERE group_id=? AND deleted=0",
                       (int(smoke), group_id))

    def get(self, folder, file_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM receipts WHERE folder=? AND file_id=?", (folder, file_id)).fetchone()
            return dict(row) if row else None

    def backup(self, folder, file_id, backup_id):
        with self.connect() as db:
            db.execute("UPDATE receipts SET backup_id=? WHERE folder=? AND file_id=?", (str(backup_id), folder, file_id))

    def needing_backup(self, folder, limit):
        with self.connect() as db:
            return [dict(row) for row in db.execute(
                "SELECT * FROM receipts WHERE folder=? AND completed=1 AND smoke=1 "
                "AND backup_id IS NULL AND deleted=0 LIMIT ?", (folder, limit))]

    def deleted(self, folder, file_id):
        with self.connect() as db:
            db.execute("UPDATE receipts SET deleted=1 WHERE folder=? AND file_id=?", (folder, file_id))

    def history_done(self, folder):
        with self.connect() as db:
            return bool(db.execute("SELECT 1 FROM history WHERE folder=? AND done=1", (folder,)).fetchone())

    def finish_history(self, folder):
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO history VALUES(?,1)", (folder,))


class DriveRetention:
    """One page per tick, on the snapshot thread rather than the capture thread."""
    def __init__(self, client, folder, settings, logger, store=None):
        self.client, self.folder, self.logger = client, folder, logger
        self.store = store or ReceiptStore()
        self.enabled = settings.get("cleanup_enabled", False) is True
        root = Path(settings.get("backup_directory") or "drive_smoke_backup")
        self.backup_root = root if root.is_absolute() else DEFAULT_STATE_PATH.parents[1] / root
        self.protected_folders = set(settings.get("protected_folder_ids") or [])
        self.batch_size = min(200, max(1, int(settings.get("cleanup_batch_size", 100))))
        self.hours = max(1, float(settings.get("retention_hours", 24)))
        self.interval = max(60, float(settings.get("cleanup_interval_seconds", 3600)))
        self.historical = not self.store.history_done(folder)
        self.token, self.cutoff, self.next_check = None, None, 0
        self.pass_failed = False
        self.next_backup_check = 0

    def tick(self, now=None, active=lambda: True):
        now = now or datetime.now(timezone.utc)
        if self.folder in self.protected_folders:
            return
        if now.timestamp() >= self.next_backup_check:
            try:
                self._backup_pending(active)
            except Exception as exc:
                self.logger.warning('[DRIVE backup] state unavailable: %s', type(exc).__name__)
            self.next_backup_check = now.timestamp() + 60
        if not self.enabled or now.timestamp() < self.next_check:
            return
        if self.cutoff is None:
            self.cutoff = month_ago(now) if self.historical else now - timedelta(hours=self.hours)
        try:
            page = self.client.list_cleanup_page(self.folder, self.cutoff.isoformat(), self.token, self.batch_size)
            if page.get("incompleteSearch"):
                raise RuntimeError("Drive returned incomplete search")
            for item in page.get("files", []):
                if not active():
                    return
                try:
                    self._process(item, active)
                except Exception as exc:
                    self.pass_failed = True
                    self.logger.warning("[DRIVE cleanup] failed file_id=%s error=%s", item.get("id"), type(exc).__name__)
            self.token = page.get("nextPageToken")
            if not self.token:
                if self.historical and not self.pass_failed:
                    self.store.finish_history(self.folder)
                    self.historical = False
                else:
                    self.next_check = now.timestamp() + self.interval
                self.cutoff = None
                self.pass_failed = False
        except Exception as exc:
            self.logger.warning("[DRIVE cleanup] scan failed: %s", type(exc).__name__)
            self.token = None
            self.next_check = now.timestamp() + self.interval

    def _process(self, item, active):
        file_id = item["id"]
        # Recheck the parent/version immediately before acting on a list result.
        current = self.client.get_file_info(file_id)
        if (current.get("trashed") or self.folder not in current.get("parents", [])
                or not str(current.get("mimeType", "")).startswith("image/")
                or parse_time(current["modifiedTime"]) >= self.cutoff):
            return
        receipt = self.store.get(self.folder, file_id)
        # Retention is based on age even when detection never completed. Only a
        # confirmed smoke result for this exact source version requires backup.
        confirmed_smoke = bool(receipt and receipt["completed"] and receipt["smoke"]
                               and receipt["modified"] == current["modifiedTime"])
        if confirmed_smoke:
            if not active():
                return
            self._ensure_local_backup(current)
        latest = self.client.get_file_info(file_id)
        if latest.get("modifiedTime") != current["modifiedTime"] or latest.get("parents") != current.get("parents"):
            return
        if not active():
            return
        self.client.delete_source_file(file_id)
        self.store.deleted(self.folder, file_id)
        self.logger.info("[DRIVE cleanup] deleted file_id=%s reason=%s modified=%s", file_id,
                         "smoke_backed_up" if confirmed_smoke else
                         "historical" if self.historical else "retention_expired", current["modifiedTime"])

    def _ensure_local_backup(self, current):
        expected = current.get('md5Checksum')
        if not expected:
            raise ValueError('source has no checksum')
        folder_key = hashlib.sha256(self.folder.encode()).hexdigest()[:20]
        file_key = hashlib.sha256((current['id'] + current['modifiedTime']).encode()).hexdigest()
        extension = {'image/jpeg': '.jpg', 'image/png': '.png', 'image/bmp': '.bmp',
                     'image/webp': '.webp', 'image/tiff': '.tiff', 'image/gif': '.gif'}.get(current['mimeType'], '.image')
        path = self.backup_root / folder_key / (file_key + extension)
        if not path.is_file() or hashlib.md5(path.read_bytes()).hexdigest() != expected:
            data = self.client.download_file_bytes(current['id'])
            if hashlib.md5(data).hexdigest() != expected:
                raise ValueError('source changed while downloading backup')
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = None
            try:
                with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as out:
                    tmp_path = Path(out.name)
                    out.write(data)
                    out.flush()
                    os.fsync(out.fileno())
                os.replace(tmp_path, path)
            finally:
                if tmp_path is not None:
                    tmp_path.unlink(missing_ok=True)
        if hashlib.md5(path.read_bytes()).hexdigest() != expected:
            raise ValueError('local backup checksum mismatch')
        self.store.backup(self.folder, current['id'], path)
        self.logger.info('[DRIVE backup] verified local image file_id=%s path=%s', current['id'], path)

    def _backup_pending(self, active):
        for receipt in self.store.needing_backup(self.folder, self.batch_size):
            if not active():
                return
            try:
                current = self.client.get_file_info(receipt['file_id'])
                if (not current.get('trashed') and self.folder in current.get('parents', [])
                        and str(current.get('mimeType', '')).startswith('image/')
                        and current.get('modifiedTime') == receipt['modified']):
                    self._ensure_local_backup(current)
            except Exception as exc:
                self.logger.warning('[DRIVE backup] retained file_id=%s error=%s', receipt['file_id'], type(exc).__name__)
