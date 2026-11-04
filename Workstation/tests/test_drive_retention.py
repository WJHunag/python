import ast
import hashlib
import logging
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock

from camera_hub.drive_retention import DriveRetention, ReceiptStore, month_ago, record_detection


NOW = datetime(2026, 10, 21, 12, tzinfo=timezone.utc)
ROOT = Path(__file__).resolve().parents[1]


class RetentionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=ROOT)
        self.addCleanup(self.tmp.cleanup)
        self.store = ReceiptStore(Path(self.tmp.name) / 'state.sqlite3')
        self.client = Mock()
        self.data = b'original smoke image bytes'
        self.digest = hashlib.md5(self.data).hexdigest()
        self.item = dict(id='source', parents=['camera'], mimeType='image/jpeg',
                         modifiedTime=(NOW - timedelta(days=40)).isoformat(), md5Checksum=self.digest)
        self.client.get_file_info.side_effect = lambda fid: dict(self.item)
        self.client.download_file_bytes.return_value = self.data
        self.client.list_cleanup_page.return_value = {'files': [{'id': 'source'}]}

    def cleaner(self, **settings):
        settings.setdefault('cleanup_enabled', True)
        settings['backup_directory'] = str(Path(self.tmp.name) / 'archive')
        return DriveRetention(self.client, 'camera', settings, logging.getLogger('test'), self.store)

    def register(self, smoke=None):
        self.store.register('camera', 'source', self.item['modifiedTime'], 'group')
        if smoke is not None:
            self.store.complete('group', smoke)

    def regular(self):
        self.store.finish_history('camera')

    def test_legacy_month_cleanup_and_persisted_completion(self):
        cleaner = self.cleaner()
        cleaner.tick(NOW)
        self.client.delete_source_file.assert_called_once_with('source')
        self.assertFalse(self.cleaner().historical)
        self.assertEqual(self.client.list_cleanup_page.call_args.args[1], month_ago(NOW).isoformat())

    def test_month_clamps_calendar_day(self):
        self.assertEqual(month_ago(datetime(2024, 3, 31)).day, 29)
        self.assertEqual(month_ago(datetime(2026, 1, 15)).year, 2025)

    def test_newer_than_month_is_not_deleted_during_history(self):
        self.item['modifiedTime'] = (NOW - timedelta(days=10)).isoformat()
        self.cleaner().tick(NOW)
        self.client.delete_source_file.assert_not_called()

    def test_pending_receipt_deleted_in_history(self):
        self.register()
        self.cleaner().tick(NOW)
        self.client.delete_source_file.assert_called_once_with('source')
        self.client.download_file_bytes.assert_not_called()

    def test_one_day_cutoff_and_hourly_schedule(self):
        self.regular()
        self.register(False)
        cleaner = self.cleaner()
        cleaner.tick(NOW)
        cleaner.tick(NOW + timedelta(minutes=59))
        self.assertEqual(self.client.list_cleanup_page.call_count, 1)
        self.assertEqual(self.client.list_cleanup_page.call_args.args[1], (NOW - timedelta(hours=24)).isoformat())
        cleaner.tick(NOW + timedelta(hours=1))
        self.assertEqual(self.client.list_cleanup_page.call_count, 2)

    def test_recent_image_protected(self):
        self.regular()
        self.item['modifiedTime'] = (NOW - timedelta(hours=23)).isoformat()
        self.register(False)
        self.cleaner().tick(NOW)
        self.client.delete_source_file.assert_not_called()

    def test_regular_without_receipt_deleted_after_one_day(self):
        self.regular()
        self.item['modifiedTime'] = (NOW - timedelta(hours=25)).isoformat()
        self.cleaner().tick(NOW)
        self.client.delete_source_file.assert_called_once_with('source')
        self.client.download_file_bytes.assert_not_called()

    def test_pending_or_unknown_detection_does_not_block_expired_image(self):
        self.regular()
        self.item['modifiedTime'] = (NOW - timedelta(hours=25)).isoformat()
        self.register()
        record_detection(self.store, 'group', {
            'crop': {'stage2_enabled': True, 'retention_result': 'unknown'}})
        self.cleaner().tick(NOW)
        self.client.delete_source_file.assert_called_once_with('source')
        self.client.download_file_bytes.assert_not_called()
        self.assertEqual(self.store.get('camera', 'source')['deleted'], 1)

    def test_unprocessed_image_within_one_day_is_retained(self):
        self.regular()
        self.item['modifiedTime'] = (NOW - timedelta(hours=23)).isoformat()
        self.cleaner().tick(NOW)
        self.client.delete_source_file.assert_not_called()

    def test_exactly_one_day_not_yet_expired(self):
        self.regular()
        self.item['modifiedTime'] = (NOW - timedelta(hours=24)).isoformat()
        self.cleaner().tick(NOW)
        self.client.delete_source_file.assert_not_called()

    def test_smoke_download_failure_retained(self):
        self.register(True)
        self.client.download_file_bytes.side_effect = RuntimeError('download failed')
        self.cleaner().tick(NOW)
        self.client.delete_source_file.assert_not_called()

    def test_smoke_backup_verified_before_deletion_and_persisted(self):
        self.register(True)
        self.cleaner().tick(NOW)
        self.client.download_file_bytes.assert_called_once_with('source')
        self.client.delete_source_file.assert_called_once_with('source')
        receipt = self.store.get('camera', 'source')
        self.assertEqual(Path(receipt['backup_id']).read_bytes(), self.data)
        self.assertEqual(receipt['deleted'], 1)

    def test_failed_backup_retained_and_historical_scan_retries(self):
        self.register(True)
        self.client.download_file_bytes.side_effect = RuntimeError('failed')
        self.cleaner().tick(NOW)
        self.client.delete_source_file.assert_not_called()
        self.assertFalse(self.store.history_done('camera'))

    def test_checksum_mismatch_retained(self):
        self.register(True)
        self.client.download_file_bytes.return_value = b'different version'
        self.cleaner().tick(NOW)
        self.client.delete_source_file.assert_not_called()

    def test_protected_folder_never_scanned_as_source(self):
        self.cleaner(protected_folder_ids=['camera']).tick(NOW)
        self.client.list_cleanup_page.assert_not_called()

    def test_disabled_never_scans(self):
        self.cleaner(cleanup_enabled=False).tick(NOW)
        self.client.list_cleanup_page.assert_not_called()
        self.client.delete_source_file.assert_not_called()

    def test_cleanup_disabled_by_default(self):
        cleaner = DriveRetention(self.client, 'camera', {}, logging.getLogger('test'), self.store)
        cleaner.tick(NOW)
        self.assertFalse(cleaner.enabled)
        self.client.list_cleanup_page.assert_not_called()
        self.client.delete_source_file.assert_not_called()
        self.assertFalse(self.store.history_done('camera'))

    def test_disabled_cleanup_preserves_local_smoke_backup(self):
        self.register(True)
        self.cleaner(cleanup_enabled=False).tick(NOW)
        receipt = self.store.get('camera', 'source')
        self.assertEqual(Path(receipt['backup_id']).read_bytes(), self.data)
        self.client.list_cleanup_page.assert_not_called()
        self.client.delete_source_file.assert_not_called()

    def test_nonimage_or_other_parent_retained(self):
        for change in ({'mimeType': 'application/pdf'}, {'parents': ['other']}, {'trashed': True}):
            original = dict(self.item)
            self.item.update(change)
            self.cleaner().tick(NOW)
            self.client.delete_source_file.assert_not_called()
            self.item = original

    def test_changed_expired_version_does_not_require_previous_smoke_backup(self):
        self.register(True)
        self.item['modifiedTime'] = (NOW - timedelta(days=35)).isoformat()
        self.cleaner().tick(NOW)
        self.client.delete_source_file.assert_called_once_with('source')
        self.client.download_file_bytes.assert_not_called()

    def test_changed_between_verifications_retained(self):
        self.client.get_file_info.side_effect = [dict(self.item), dict(self.item, modifiedTime=NOW.isoformat())]
        self.cleaner().tick(NOW)
        self.client.delete_source_file.assert_not_called()

    def test_pagination_bounded_and_history_only_finished_at_end(self):
        self.client.list_cleanup_page.side_effect = [
            {'files': [], 'nextPageToken': 'page2'}, {'files': []}]
        cleaner = self.cleaner(cleanup_batch_size=30)
        cleaner.tick(NOW)
        self.assertFalse(self.store.history_done('camera'))
        cleaner.tick(NOW)
        self.assertEqual(self.client.list_cleanup_page.call_args.args[2:], ('page2', 30))
        self.assertTrue(self.store.history_done('camera'))

    def test_permission_failure_does_not_finish_history(self):
        self.client.delete_source_file.side_effect = PermissionError()
        self.cleaner().tick(NOW)
        self.assertFalse(self.store.history_done('camera'))

    def test_incomplete_search_retains_history_for_retry(self):
        self.client.list_cleanup_page.return_value = {'incompleteSearch': True, 'files': [{'id': 'source'}]}
        self.cleaner().tick(NOW)
        self.client.delete_source_file.assert_not_called()
        self.assertFalse(self.store.history_done('camera'))

    def test_stop_before_delete(self):
        active = Mock(side_effect=[True, False])
        self.cleaner().tick(NOW, active=active)
        self.client.delete_source_file.assert_not_called()

    def test_receipts_survive_reopen_and_new_version_resets_completion(self):
        self.register(True)
        self.store.backup('camera', 'source', 'backup')
        reopened = ReceiptStore(self.store.path)
        self.assertTrue(reopened.get('camera', 'source')['completed'])
        reopened.register('camera', 'source', NOW.isoformat(), 'group2')
        receipt = reopened.get('camera', 'source')
        self.assertFalse(receipt['completed'])
        self.assertIsNone(receipt['backup_id'])

    def test_detection_aggregates_smoke_and_unknown_conservatively(self):
        for crops, expected in [
            ({'a': {'stage2_enabled': False}}, (1, 1)),
            ({'a': {'stage2_enabled': True, 'retention_result': 'steam', 'retention_total': 1}}, (1, 0)),
            ({'a': {'stage2_enabled': True, 'retention_result': 'steam', 'retention_total': 2}}, (0, 1)),
            ({'a': {'stage2_enabled': True, 'retention_result': 'steam'}}, (0, 1)),
            ({'a': {'stage2_enabled': True, 'retention_result': 'unknown'}}, (0, 1)),
            ({'a': {'stage2_enabled': True, 'retention_result': 'steam'},
              'b': {'stage2_enabled': True, 'retention_result': 'smoke'}}, (1, 1)),
        ]:
            with self.store.connect() as db:
                db.execute('DELETE FROM receipts')
            self.register()
            record_detection(self.store, 'group', crops)
            receipt = self.store.get('camera', 'source')
            self.assertEqual((receipt['completed'], receipt['smoke']), expected)

    def test_smoke_backed_up_before_retention_age(self):
        self.item['modifiedTime'] = (NOW - timedelta(minutes=5)).isoformat()
        self.register(True)
        self.cleaner().tick(NOW)
        receipt = self.store.get('camera', 'source')
        self.assertEqual(Path(receipt['backup_id']).read_bytes(), self.data)
        self.client.delete_source_file.assert_not_called()

    def test_corrupted_backup_is_repaired_before_deleting(self):
        self.regular()
        self.item['modifiedTime'] = (NOW - timedelta(hours=2)).isoformat()
        self.register(True)
        cleaner = self.cleaner()
        cleaner.tick(NOW)
        receipt = self.store.get('camera', 'source')
        path = Path(receipt['backup_id'])
        path.write_bytes(b'corrupted')
        cleaner.tick(NOW + timedelta(days=2))
        self.assertEqual(path.read_bytes(), self.data)
        self.client.delete_source_file.assert_called_once_with('source')

    def test_local_disk_failure_never_deletes_source(self):
        self.register(True)
        root = Path(self.tmp.name) / 'archive'
        root.write_bytes(b'not a directory')
        self.cleaner().tick(NOW)
        self.client.delete_source_file.assert_not_called()


class ClientContractTests(unittest.TestCase):
    def test_google_cleanup_query_and_mutations_without_network(self):
        path = ROOT / 'camera_hub/multi_source_vision_hub_center.py'
        tree = ast.parse(path.read_text(encoding='utf-8-sig'))
        client = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'DriveClient')
        names = {'list_cleanup_page', 'get_file_info', 'delete_source_file'}
        methods = [n for n in client.body if isinstance(n, ast.FunctionDef) and n.name in names]
        ns = {}
        exec(compile(ast.Module(body=methods, type_ignores=[]), str(path), 'exec'), ns)
        fake = Mock()
        fake._run_api_call.side_effect = lambda _desc, func: func()
        ns['list_cleanup_page'](fake, "folder'id", NOW.isoformat(), 'page2', 100)
        kwargs = fake.service.files.return_value.list.call_args.kwargs
        self.assertIn("folder\\'id", kwargs['q'])
        self.assertEqual(kwargs['pageToken'], 'page2')
        self.assertIn('nextPageToken', kwargs['fields'])
        ns['delete_source_file'](fake, 'source')
        fake.service.files.return_value.delete.assert_called_once_with(fileId='source', supportsAllDrives=True)


if __name__ == '__main__':
    unittest.main()
