from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from smoke_config import routing_server
from smoke_config.distributed_config_store import DistributedConfigStore


class CoordinateGeoReferenceBatchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(
            dir=Path(__file__).resolve().parents[1],
        )
        self.distributed_root = Path(self.temp_dir.name) / "distributed_config"
        self._write_fixture(camera_count=100)
        self.store = DistributedConfigStore(distributed_root=self.distributed_root)
        self.client = routing_server.app.test_client()
        self.dispatch_patch = patch.object(
            routing_server,
            "_dispatch_distributed_config_in_background",
        )
        self.dispatch = self.dispatch_patch.start()
        self.store_patch = patch.object(
            routing_server,
            "DistributedConfigStore",
            return_value=self.store,
        )
        self.store_patch.start()

    def tearDown(self) -> None:
        self.store_patch.stop()
        self.dispatch_patch.stop()
        self.temp_dir.cleanup()

    def _write_fixture(self, camera_count: int) -> None:
        camera_dir = self.distributed_root / "camera_configs"
        camera_dir.mkdir(parents=True, exist_ok=True)
        (self.distributed_root / "common_settings.json").write_text(
            json.dumps({"server_ip": "127.0.0.1", "server_port": 5020}, ensure_ascii=False),
            encoding="utf-8",
        )
        (self.distributed_root / "workstation_assignments.json").write_text(
            json.dumps({"127.0.0.1": {"polling_cameras": [1], "fixed_cameras": []}}),
            encoding="utf-8",
        )
        for channel in range(1, camera_count + 1):
            camera = {
                "ip": f"192.168.1.{channel}",
                "NVR_channel": str(channel),
                "location": f"Camera {channel}",
                "function_switches": {"stage2_detection": channel % 2 == 0},
                "preserve_me": {"value": channel},
                "geo_reference": {
                    "image_name": f"old_{channel}.jpg",
                    "save_time": "2026-01-01 00:00:00",
                    "points": [],
                },
            }
            (camera_dir / f"camera_channel_{channel:03d}.json").write_text(
                json.dumps(camera, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

    @staticmethod
    def _geo(channel: int) -> dict:
        return {
            "image_name": f"new_{channel}.jpg",
            "save_time": "2026-08-25 12:00:00",
            "points": [
                {
                    "point_name": "A",
                    "image_coord": {"x": channel, "y": 20},
                    "wgs84_coords": {"x": 120.1, "y": 23.2},
                }
            ],
        }

    def _update(self, channel: int, *, camera_ip: str | None = None) -> dict:
        return {
            "camera_ip": camera_ip or f"192.168.1.{channel}",
            "nvr_channel": str(channel),
            "geo_reference": self._geo(channel),
        }

    def _snapshot(self, channels: list[int]) -> dict[int, dict]:
        return {channel: self.store.load_camera_config(channel) for channel in channels}

    def test_batch_updates_100_cameras_and_dispatches_once(self) -> None:
        updates = [self._update(channel) for channel in range(1, 101)]
        before = self._snapshot([1, 50, 100])

        response = self.client.post(
            "/coordinate/geo_reference/batch",
            json={"updates": updates},
        )

        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(body["code"], "OK")
        self.assertEqual(body["data"]["updated_count"], 100)
        self.assertEqual(len(body["data"]["updated_targets"]), 100)
        self.assertEqual(body["data"]["dispatch_status"], "queued")
        self.dispatch.assert_called_once()

        for channel in (1, 50, 100):
            after = self.store.load_camera_config(channel)
            expected = copy.deepcopy(before[channel])
            expected["geo_reference"] = self._geo(channel)
            self.assertEqual(after, expected)

    def test_validation_error_writes_nothing_and_does_not_dispatch(self) -> None:
        before = self._snapshot([1, 2])
        response = self.client.post(
            "/coordinate/geo_reference/batch",
            json={
                "updates": [
                    self._update(1),
                    self._update(2, camera_ip="192.168.99.2"),
                ]
            },
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("does not match", response.get_json()["message"])
        self.assertEqual(self._snapshot([1, 2]), before)
        self.dispatch.assert_not_called()

    def test_empty_duplicate_and_missing_targets_are_rejected_without_writes(self) -> None:
        cases = [
            {"updates": []},
            {"updates": [self._update(1), self._update(1)]},
            {"updates": [self._update(999, camera_ip="192.168.1.999")]},
        ]
        for payload in cases:
            with self.subTest(payload=payload):
                before = self._snapshot([1])
                response = self.client.post(
                    "/coordinate/geo_reference/batch",
                    json=payload,
                )
                self.assertEqual(response.status_code, 400)
                self.assertEqual(self._snapshot([1]), before)
                self.dispatch.assert_not_called()

    def test_write_failure_rolls_back_and_does_not_dispatch(self) -> None:
        channels = [1, 2, 3, 4]
        before = self._snapshot(channels)
        original_save = self.store.save_camera_config
        call_count = 0

        def fail_on_third(channel: int, camera: dict) -> None:
            nonlocal call_count
            call_count += 1
            if call_count == 3:
                raise OSError("simulated camera file failure")
            original_save(channel, camera)

        self.store.save_camera_config = fail_on_third  # type: ignore[method-assign]
        response = self.client.post(
            "/coordinate/geo_reference/batch",
            json={"updates": [self._update(channel) for channel in channels]},
        )

        self.assertEqual(response.status_code, 500)
        body = response.get_json()
        self.assertEqual(body["code"], "BATCH_WRITE_FAILED")
        self.assertEqual(body["data"]["rollback_status"], "completed")
        self.assertEqual(self._snapshot(channels), before)
        self.dispatch.assert_not_called()

    def test_existing_single_camera_flow_remains_compatible(self) -> None:
        before = self.store.load_camera_config(1)
        response = self.client.post(
            "/coordinate/geo_reference",
            json=self._update(1),
        )

        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(body["code"], "OK")
        self.assertEqual(body["data"]["dispatch_status"], "queued")
        self.dispatch.assert_called_once()

        after = self.store.load_camera_config(1)
        expected = copy.deepcopy(before)
        expected["geo_reference"] = self._geo(1)
        self.assertEqual(after, expected)


if __name__ == "__main__":
    unittest.main()
