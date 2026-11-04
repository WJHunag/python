from __future__ import annotations

import copy
import json
import tempfile
import tkinter as tk
import unittest
from pathlib import Path

from coordinate_editor import app, standalone_package, utils


class CoordinateEditorWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.config_path = Path(self.temp_dir.name)
        (self.config_path / "camera_configs").mkdir(parents=True)
        (self.config_path / "common_settings.json").write_text(
            json.dumps({"server_ip": "192.168.1.200", "server_port": 5020}),
            encoding="utf-8",
        )
        (self.config_path / "workstation_assignments.json").write_text(
            json.dumps({"127.0.0.1": {"polling_cameras": [], "fixed_cameras": []}}),
            encoding="utf-8",
        )
        self.config = {
            "cameras": [
                {
                    "ip": "192.168.1.1",
                    "NVR_channel": "1",
                    "location": "Camera 1",
                },
                {
                    "ip": "192.168.1.2",
                    "NVR_channel": "2",
                    "location": "Camera 2",
                },
                {
                    "ip": "192.168.1.3",
                    "NVR_channel": "3",
                    "location": "Camera 3",
                },
            ]
        }
        self._write_camera(1, self._geo("old-1.jpg", 1), preserve_me="keep-1")
        self._write_camera(2, self._geo("old-2.jpg", 2), preserve_me="keep-2")
        self._write_camera(3, self._geo("old-3.jpg", 3), preserve_me="keep-3")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    @staticmethod
    def _geo(image_name: str, point_count: int) -> dict:
        return {
            "image_name": image_name,
            "save_time": "2026-08-25 12:00:00",
            "points": [
                {
                    "point_name": f"P{index}",
                    "image_coord": {"x": index, "y": index + 1},
                    "wgs84_coords": {"x": 120.1 + index, "y": 23.2 + index},
                }
                for index in range(point_count)
            ],
        }

    def _camera_path(self, channel: int) -> Path:
        return self.config_path / "camera_configs" / f"camera_channel_{channel:03d}.json"

    def _write_camera(self, channel: int, geo_reference: dict, *, preserve_me: str) -> None:
        camera = {
            "ip": f"192.168.1.{channel}",
            "NVR_channel": str(channel),
            "location": f"Camera {channel}",
            "function_switches": {"stage2_detection": channel % 2 == 0},
            "preserve_me": preserve_me,
            "geo_reference": geo_reference,
        }
        self._camera_path(channel).write_text(
            json.dumps(camera, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _read_camera(self, channel: int) -> dict:
        return json.loads(self._camera_path(channel).read_text(encoding="utf-8"))

    def test_local_save_changes_only_geo_reference(self) -> None:
        before = self._read_camera(1)
        final_geo_reference = self._geo("checked-1.jpg", 3)

        utils.update_camera_geo_reference(
            self.config_path,
            "192.168.1.1",
            final_geo_reference,
        )

        expected = copy.deepcopy(before)
        expected[utils.GEO_REFERENCE_KEY] = final_geo_reference
        self.assertEqual(self._read_camera(1), expected)

    def test_single_save_is_before_remote_and_local_failure_skips_remote(self) -> None:
        events: list[str] = []

        def local_saver(config_path, camera_info, geo_reference):
            events.append("local")

        def remote_submitter(config, camera_info, geo_reference):
            events.append("remote")
            return {"code": "OK"}

        response = utils.save_then_submit_geo_reference_update(
            self.config,
            self.config_path,
            self.config["cameras"][0],
            self._geo("final-1.jpg", 1),
            local_saver=local_saver,
            remote_submitter=remote_submitter,
        )

        self.assertEqual(events, ["local", "remote"])
        self.assertEqual(response["code"], "OK")

        remote_submitter_called = False

        def failing_local_saver(config_path, camera_info, geo_reference):
            raise OSError("local write failed")

        def should_not_submit(config, camera_info, geo_reference):
            nonlocal remote_submitter_called
            remote_submitter_called = True

        with self.assertRaises(OSError):
            utils.save_then_submit_geo_reference_update(
                self.config,
                self.config_path,
                self.config["cameras"][0],
                self._geo("failed.jpg", 1),
                local_saver=failing_local_saver,
                remote_submitter=should_not_submit,
            )

        self.assertFalse(remote_submitter_called)

    def test_single_remote_success_records_state_only_for_imported_camera(self) -> None:
        imported_id = standalone_package.camera_id_from_channel(1)
        non_imported_id = standalone_package.camera_id_from_channel(2)
        utils.record_coordinate_import_success(
            self.config_path,
            {imported_id},
            imported_at="2026-08-25 13:00:00",
        )

        self.assertIsNone(
            app.record_single_sync_state_after_remote_success(
                self.config_path,
                imported_id,
            )
        )
        self.assertIsNone(
            app.record_single_sync_state_after_remote_success(
                self.config_path,
                non_imported_id,
            )
        )

        state = utils.load_coordinate_import_state(self.config_path)
        self.assertEqual(state["synced_camera_ids"], [imported_id])
        self.assertTrue(state["synced_at"])

    def test_single_remote_failure_does_not_mark_synced(self) -> None:
        camera_id = standalone_package.camera_id_from_channel(1)
        utils.record_coordinate_import_success(
            self.config_path,
            {camera_id},
            imported_at="2026-08-25 13:00:00",
        )

        def failing_remote(config, camera_info, geo_reference):
            raise ConnectionError("server unavailable")

        with self.assertRaises(utils.GeoReferenceSingleRemoteError):
            utils.save_then_submit_geo_reference_update(
                self.config,
                self.config_path,
                self.config["cameras"][0],
                self._geo("remote-failed.jpg", 1),
                local_saver=lambda *_args: None,
                remote_submitter=failing_remote,
            )

        self.assertEqual(
            utils.load_coordinate_import_state(self.config_path)["synced_camera_ids"],
            [],
        )

    def test_single_state_write_failure_is_reported_after_remote_success(self) -> None:
        camera_id = standalone_package.camera_id_from_channel(1)
        utils.record_coordinate_import_success(
            self.config_path,
            {camera_id},
            imported_at="2026-08-25 13:00:00",
        )

        def failing_state_writer(config_path, camera_ids):
            raise OSError("state sidecar is read-only")

        state_error = app.record_single_sync_state_after_remote_success(
            self.config_path,
            camera_id,
            state_marker=failing_state_writer,
        )

        self.assertIsInstance(state_error, OSError)
        self.assertEqual(
            utils.load_coordinate_import_state(self.config_path)["synced_camera_ids"],
            [],
        )

    def test_batch_success_updates_preview_model_after_state_write(self) -> None:
        camera_id = standalone_package.camera_id_from_channel(1)
        utils.record_coordinate_import_success(
            self.config_path,
            {camera_id},
            imported_at="2026-08-25 13:00:00",
        )
        rows = [{"camera_id": camera_id, "synced": False}]

        state_error = app.persist_batch_sync_state_and_update_preview(
            self.config_path,
            rows,
            {camera_id},
        )

        self.assertIsNone(state_error)
        self.assertTrue(rows[0]["synced"])
        self.assertEqual(sum(1 for row in rows if row["synced"]), 1)

    def test_batch_state_write_failure_does_not_mark_preview_model(self) -> None:
        camera_id = standalone_package.camera_id_from_channel(1)
        rows = [{"camera_id": camera_id, "synced": False}]

        def failing_state_writer(config_path, camera_ids):
            raise OSError("state sidecar is read-only")

        state_error = app.persist_batch_sync_state_and_update_preview(
            self.config_path,
            rows,
            {camera_id},
            state_marker=failing_state_writer,
        )

        self.assertIsInstance(state_error, OSError)
        self.assertFalse(rows[0]["synced"])
        self.assertEqual(sum(1 for row in rows if row["synced"]), 0)

    def test_import_state_round_trips_saved_checked_and_synced_ids(self) -> None:
        imported_ids = {
            standalone_package.camera_id_from_channel(1),
            standalone_package.camera_id_from_channel(2),
        }
        state = utils.record_coordinate_import_success(
            self.config_path,
            imported_ids,
            imported_at="2026-08-25 13:00:00",
        )
        self.assertEqual(state["imported_camera_ids"], sorted(imported_ids))
        self.assertEqual(state["locally_saved_camera_ids"], sorted(imported_ids))
        self.assertEqual(state["checked_camera_ids"], [])
        self.assertEqual(state["synced_camera_ids"], [])

        utils.mark_coordinate_cameras_locally_saved(
            self.config_path,
            {standalone_package.camera_id_from_channel(1)},
            checked=True,
        )
        utils.mark_coordinate_cameras_synced(
            self.config_path,
            {standalone_package.camera_id_from_channel(2)},
            synced_at="2026-08-25 14:00:00",
        )

        loaded = utils.load_coordinate_import_state(self.config_path)
        self.assertEqual(
            loaded["checked_camera_ids"],
            [standalone_package.camera_id_from_channel(1)],
        )
        self.assertEqual(
            loaded["synced_camera_ids"],
            [standalone_package.camera_id_from_channel(2)],
        )
        self.assertEqual(loaded["synced_at"], "2026-08-25 14:00:00")
        self.assertTrue(utils.coordinate_import_state_path(self.config_path).is_file())

    def test_successful_external_import_records_pending_ids_after_file_write(self) -> None:
        camera_id = standalone_package.camera_id_from_channel(1)
        result_cameras = [
            {
                "camera_id": camera_id,
                "geo_reference": self._geo("imported-final.jpg", 2),
            }
        ]

        updated_count, state = utils.apply_standalone_result_import_and_record_state(
            self.config_path,
            result_cameras,
            {camera_id},
            imported_at="2026-08-25 15:00:00",
        )

        self.assertEqual(updated_count, 1)
        self.assertEqual(state["imported_camera_ids"], [camera_id])
        self.assertEqual(self._read_camera(1)["geo_reference"]["image_name"], "imported-final.jpg")
        self.assertEqual(
            utils.load_coordinate_import_state(self.config_path)["imported_at"],
            "2026-08-25 15:00:00",
        )

    def test_batch_updates_use_final_local_geo_reference_and_filter_targets(self) -> None:
        final_geo_reference = self._geo("checked-final.jpg", 4)
        self._write_camera(1, final_geo_reference, preserve_me="keep-1")
        self._write_camera(2, {"points": "invalid"}, preserve_me="keep-2")

        updates = utils.build_geo_reference_batch_updates_from_local_config(
            self.config,
            self.config_path,
            {
                standalone_package.camera_id_from_channel(1),
                standalone_package.camera_id_from_channel(2),
                standalone_package.camera_id_from_channel(3),
                standalone_package.camera_id_from_channel(99),
            },
        )

        self.assertEqual(len(updates), 2)
        self.assertEqual(
            {update["nvr_channel"] for update in updates},
            {"1", "3"},
        )
        channel_1_update = next(update for update in updates if update["nvr_channel"] == "1")
        self.assertEqual(channel_1_update["geo_reference"], final_geo_reference)

    def test_batch_preview_exposes_missing_invalid_and_checked_status(self) -> None:
        self._write_camera(2, {"points": "invalid"}, preserve_me="keep-2")
        imported_ids = {
            standalone_package.camera_id_from_channel(1),
            standalone_package.camera_id_from_channel(2),
            standalone_package.camera_id_from_channel(99),
        }
        utils.record_coordinate_import_success(
            self.config_path,
            imported_ids,
            imported_at="2026-08-25 13:00:00",
        )
        utils.mark_coordinate_cameras_locally_saved(
            self.config_path,
            {standalone_package.camera_id_from_channel(1)},
            checked=True,
        )

        rows = utils.build_local_geo_reference_batch_preview(
            self.config,
            self.config_path,
            utils.load_coordinate_import_state(self.config_path),
        )
        rows_by_id = {row["camera_id"]: row for row in rows}

        self.assertTrue(rows_by_id[standalone_package.camera_id_from_channel(1)]["can_sync"])
        self.assertTrue(rows_by_id[standalone_package.camera_id_from_channel(1)]["locally_saved"])
        self.assertTrue(rows_by_id[standalone_package.camera_id_from_channel(1)]["checked"])
        self.assertFalse(rows_by_id[standalone_package.camera_id_from_channel(2)]["can_sync"])
        self.assertEqual(rows_by_id[standalone_package.camera_id_from_channel(2)]["reason"], "沒有有效 geo_reference")
        self.assertFalse(rows_by_id[standalone_package.camera_id_from_channel(99)]["can_sync"])
        self.assertEqual(rows_by_id[standalone_package.camera_id_from_channel(99)]["reason"], "本機不存在")

    def test_batch_preparation_saves_current_camera_before_building_preview(self) -> None:
        events: list[str] = []

        def save_current() -> None:
            events.append("save-current")

        def build_preview() -> list[dict]:
            events.append("build-preview")
            return [{"camera_id": "camera_channel_001"}]

        rows = app.prepare_local_batch_sync(
            "camera_channel_001",
            {"camera_channel_001"},
            save_current,
            build_preview,
        )

        self.assertEqual(events, ["save-current", "build-preview"])
        self.assertEqual(rows, [{"camera_id": "camera_channel_001"}])

    def test_coordinate_import_version_summary_only_shows_nonmatching_rows(self) -> None:
        self.assertEqual(
            app.coordinate_import_version_summary(
                [{"exists": True, "version_alert": False, "version_unverified": False}]
            ),
            "",
        )
        self.assertEqual(
            app.coordinate_import_version_summary(
                [
                    {"exists": True, "version_alert": True, "version_unverified": False},
                    {"exists": True, "version_alert": False, "version_unverified": True},
                    {"exists": False, "version_alert": False, "version_unverified": True},
                ]
            ),
            "版本不同 1 支｜無法比對 1 支",
        )

    def test_coordinate_import_version_dates_handle_legacy_missing_values(self) -> None:
        self.assertEqual(
            app.coordinate_import_version_dates(
                {
                    "baseline_exported_at": "",
                    "baseline_save_time": "2026-10-13 09:20:00",
                    "import_save_time": None,
                    "local_save_time": "2026-10-14 11:05:00",
                    "server_save_time": "",
                }
            ),
            {
                "baseline": "2026-10-13 09:20:00",
                "external": "未記錄",
                "local": "2026-10-14 11:05:00",
                "server": "未記錄",
            },
        )

    def test_ui_styles_and_sync_actions_are_declared(self) -> None:
        source = Path(app.__file__).read_text(encoding="utf-8")

        for expected in (
            "同步目前攝影機點位",
            "批次同步本次匯入攝影機",
            "匯入外部版結果",
            "選取攝影機版本日期",
            "同步成功但狀態記錄失敗",
        ):
            self.assertIn(expected, source)

        self.assertNotIn("儲存／同步點位", source)
        self.assertNotIn("只儲存目前攝影機到本機", source)
        self.assertNotIn("PRIMARY_DIALOG_BUTTON_STYLE", source)
        self.assertNotIn("NEUTRAL_DIALOG_BUTTON_STYLE", source)
        self.assertIn("tk.Button(", source)

    def test_action_buttons_use_tk_colors_that_survive_vista_theme(self) -> None:
        root = tk.Tk()
        root.withdraw()
        try:
            primary = tk.Button(
                root,
                text="主要",
                command=lambda: None,
                **app.action_button_options("Arial", primary=True),
            )
            neutral = tk.Button(
                root,
                text="中性",
                command=lambda: None,
                **app.action_button_options("Arial", primary=False),
            )

            self.assertIsInstance(primary, tk.Button)
            self.assertIsInstance(neutral, tk.Button)
            self.assertEqual(primary.cget("bg").lower(), "#2980b9")
            self.assertEqual(primary.cget("fg").lower(), "#ffffff")
            self.assertEqual(primary.cget("activebackground").lower(), "#3498db")
            self.assertEqual(primary.cget("activeforeground").lower(), "#ffffff")
            self.assertEqual(primary.cget("disabledforeground").lower(), "#d6eaf8")
            self.assertEqual(neutral.cget("bg").lower(), "#eeeeee")
            self.assertEqual(neutral.cget("fg").lower(), "#333333")
            self.assertEqual(neutral.cget("activebackground").lower(), "#f5f5f5")
            self.assertEqual(neutral.cget("activeforeground").lower(), "#333333")
            self.assertEqual(neutral.cget("disabledforeground").lower(), "#666666")
            for option in ("font", "borderwidth", "relief", "padx", "pady"):
                self.assertEqual(primary.cget(option), neutral.cget(option), option)
        finally:
            root.destroy()


if __name__ == "__main__":
    unittest.main()
