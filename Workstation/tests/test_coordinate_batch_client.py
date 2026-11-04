from __future__ import annotations

import ast
import unittest
import sys
from pathlib import Path
from unittest.mock import Mock, patch

# coordinate_editor/utils.py is also launched as a script and imports its
# sibling standalone_package by the module name used by that launch mode.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "coordinate_editor"))

from coordinate_editor import app, standalone_package, utils


def _geo(channel: int) -> dict:
    return {
        "image_name": f"camera_{channel}.jpg",
        "save_time": "2026-08-25 12:00:00",
        "points": [],
    }


class CoordinateBatchClientTests(unittest.TestCase):
    @staticmethod
    def _config(camera_count: int) -> dict:
        return {
            "server_ip": "192.168.1.200",
            "server_port": 5020,
            "cameras": [
                {
                    "ip": f"192.168.1.{channel}",
                    "NVR_channel": str(channel),
                    "location": f"Camera {channel}",
                }
                for channel in range(1, camera_count + 1)
            ],
        }

    @staticmethod
    def _results(channels: list[int]) -> list[dict]:
        return [
            {
                "camera_id": standalone_package.camera_id_from_channel(channel),
                "nvr_channel": str(channel),
                "display_name": f"Camera {channel}",
                "image_name": f"camera_{channel}.jpg",
                "geo_reference": _geo(channel),
            }
            for channel in channels
        ]

    @staticmethod
    def _valid_response(channel: int = 1) -> dict:
        return {
            "code": "OK",
            "data": {
                "updated_count": 1,
                "updated_targets": [
                    {
                        "camera_ip": f"192.168.1.{channel}",
                        "nvr_channel": str(channel),
                        "point_count": 0,
                    }
                ],
                "dispatch_status": "queued",
            },
        }

    def test_100_camera_batch_contains_only_selected_local_cameras(self) -> None:
        config = self._config(100)
        result_cameras = self._results(list(range(1, 101)) + [101])

        updates = utils.build_geo_reference_batch_updates(
            config,
            result_cameras,
            selected_camera_ids={standalone_package.camera_id_from_channel(channel) for channel in range(1, 101)},
        )

        self.assertEqual(len(updates), 100)
        self.assertEqual(
            {update["nvr_channel"] for update in updates},
            {str(channel) for channel in range(1, 101)},
        )
        self.assertNotIn("192.168.1.101", {update["camera_ip"] for update in updates})
        self.assertEqual(set(updates[0]), {"camera_ip", "nvr_channel", "geo_reference"})

    def test_unselected_camera_is_not_sent(self) -> None:
        config = self._config(3)
        result_cameras = self._results([1, 2, 3])
        selected_id = standalone_package.camera_id_from_channel(2)

        updates = utils.build_geo_reference_batch_updates(
            config,
            result_cameras,
            selected_camera_ids={selected_id},
        )

        self.assertEqual(len(updates), 1)
        self.assertEqual(updates[0]["nvr_channel"], "2")
        self.assertEqual(updates[0]["camera_ip"], "192.168.1.2")

    def test_batch_client_posts_batch_endpoint_and_rejects_empty_batch(self) -> None:
        config = {
            **self._config(1),
            "coordinate_geo_reference_url": "http://a40.test/coordinate/geo_reference",
        }
        updates = utils.build_geo_reference_batch_updates(
            config,
            self._results([1]),
            selected_camera_ids={standalone_package.camera_id_from_channel(1)},
        )
        fake_response = Mock(
            ok=True,
            status_code=200,
            text="",
        )
        fake_response.json.return_value = {
            **self._valid_response(),
        }

        with patch.object(utils.requests, "post", return_value=fake_response) as post:
            response_payload = utils.submit_geo_reference_batch_update(config, updates)

        self.assertEqual(response_payload["data"]["updated_count"], 1)
        post.assert_called_once_with(
            "http://a40.test/coordinate/geo_reference/batch",
            json={"updates": updates},
            timeout=30.0,
        )

        with patch.object(utils.requests, "post") as post:
            with self.assertRaises(ValueError):
                utils.submit_geo_reference_batch_update(config, [])
        post.assert_not_called()

    def test_batch_response_contract_rejects_invalid_2xx_payloads(self) -> None:
        config = self._config(1)
        updates = utils.build_geo_reference_batch_updates(
            config,
            self._results([1]),
            selected_camera_ids={standalone_package.camera_id_from_channel(1)},
        )
        valid_data = self._valid_response()["data"]
        invalid_payloads = [
            None,
            [],
            {},
            {"code": "ERROR", "data": valid_data},
            {"code": "OK"},
            {"code": "OK", "data": {}},
            {
                "code": "OK",
                "data": {**valid_data, "updated_count": True},
            },
            {
                "code": "OK",
                "data": {**valid_data, "updated_count": 2},
            },
            {
                "code": "OK",
                "data": {**valid_data, "updated_targets": []},
            },
            {
                "code": "OK",
                "data": {**valid_data, "updated_targets": [{"camera_ip": "192.168.1.1"}]},
            },
            {
                "code": "OK",
                "data": {**valid_data, "updated_targets": ["not-an-object"]},
            },
            {
                "code": "OK",
                "data": {**valid_data, "dispatch_status": "not_queued"},
            },
        ]

        for payload in invalid_payloads:
            with self.subTest(payload=payload):
                fake_response = Mock(ok=True, status_code=200, text="")
                fake_response.json.return_value = payload
                with patch.object(utils.requests, "post", return_value=fake_response):
                    with self.assertRaises(utils.GeoReferenceBatchResponseError):
                        utils.submit_geo_reference_batch_update(config, updates)

    def test_valid_batch_response_contract_is_accepted(self) -> None:
        config = self._config(1)
        updates = utils.build_geo_reference_batch_updates(
            config,
            self._results([1]),
            selected_camera_ids={standalone_package.camera_id_from_channel(1)},
        )
        fake_response = Mock(ok=True, status_code=200, text="")
        fake_response.json.return_value = self._valid_response()

        with patch.object(utils.requests, "post", return_value=fake_response):
            response_payload = utils.submit_geo_reference_batch_update(config, updates)

        self.assertEqual(response_payload["code"], "OK")
        self.assertEqual(response_payload["data"]["dispatch_status"], "queued")

    def test_total_import_point_count_uses_selected_cameras(self) -> None:
        result_cameras = self._results([1, 2, 3])
        result_cameras[0]["geo_reference"]["points"] = [{}, {}]
        result_cameras[1]["geo_reference"]["points"] = [{}, {}, {}, {}]
        result_cameras[2]["geo_reference"]["points"] = [{}]

        total = utils.count_geo_reference_batch_points(
            result_cameras,
            {
                standalone_package.camera_id_from_channel(1),
                standalone_package.camera_id_from_channel(3),
            },
        )

        self.assertEqual(total, 3)

    def test_local_import_precedes_remote_submission(self) -> None:
        config = self._config(1)
        result_cameras = self._results([1])
        selected_ids = {standalone_package.camera_id_from_channel(1)}
        updates = utils.build_geo_reference_batch_updates(
            config,
            result_cameras,
            selected_camera_ids=selected_ids,
        )
        events = []

        def local_importer(config_path, cameras, *, selected_camera_ids):
            events.append("local")
            return 1

        def remote_submitter(batch_config, batch_updates):
            events.append("remote")
            return self._valid_response()

        local_count, response_payload = utils.import_then_submit_geo_reference_batch(
            config,
            "unused-config-path",
            result_cameras,
            selected_ids,
            updates,
            local_importer=local_importer,
            remote_submitter=remote_submitter,
            progress_callback=lambda count: events.append(f"progress:{count}"),
        )

        self.assertEqual(events, ["local", "progress:1", "remote"])
        self.assertEqual(local_count, 1)
        self.assertEqual(response_payload["code"], "OK")

    def test_local_import_failure_does_not_call_remote(self) -> None:
        config = self._config(1)
        result_cameras = self._results([1])
        selected_ids = {standalone_package.camera_id_from_channel(1)}
        updates = utils.build_geo_reference_batch_updates(
            config,
            result_cameras,
            selected_camera_ids=selected_ids,
        )
        remote_submitter = Mock()

        def local_importer(config_path, cameras, *, selected_camera_ids):
            raise OSError("local write failed")

        with self.assertRaises(OSError):
            utils.import_then_submit_geo_reference_batch(
                config,
                "unused-config-path",
                result_cameras,
                selected_ids,
                updates,
                local_importer=local_importer,
                remote_submitter=remote_submitter,
            )

        remote_submitter.assert_not_called()

    def test_remote_failure_reports_local_count_for_safe_retry(self) -> None:
        config = self._config(1)
        result_cameras = self._results([1])
        selected_ids = {standalone_package.camera_id_from_channel(1)}
        updates = utils.build_geo_reference_batch_updates(
            config,
            result_cameras,
            selected_camera_ids=selected_ids,
        )

        def remote_submitter(batch_config, batch_updates):
            raise ConnectionError("server offline")

        with self.assertRaises(utils.GeoReferenceBatchRemoteError) as caught:
            utils.import_then_submit_geo_reference_batch(
                config,
                "unused-config-path",
                result_cameras,
                selected_ids,
                updates,
                local_importer=lambda *args, **kwargs: 1,
                remote_submitter=remote_submitter,
            )

        self.assertEqual(caught.exception.local_updated_count, 1)
        self.assertIsInstance(caught.exception.cause, ConnectionError)

    def test_remote_failure_worker_branch_has_explicit_return(self) -> None:
        source = Path(app.__file__).read_text(encoding="utf-8")
        module = ast.parse(source)
        worker = next(
            node
            for node in ast.walk(module)
            if isinstance(node, ast.FunctionDef)
            and node.name == "route_geo_reference_batch_worker_outcome"
        )
        remote_handler = next(
            handler
            for node in ast.walk(worker)
            if isinstance(node, ast.Try)
            for handler in node.handlers
            if isinstance(handler.type, ast.Attribute)
            and handler.type.attr == "GeoReferenceBatchRemoteError"
        )

        self.assertTrue(
            any(isinstance(statement, ast.Return) for statement in remote_handler.body),
            "remote failure branch must return after scheduling its failure callback",
        )

    def test_worker_outcome_routes_remote_failure_without_success_callback(self) -> None:
        events = []
        remote_error = app.utils.GeoReferenceBatchRemoteError(
            1,
            ConnectionError("server offline"),
        )

        def batch_runner():
            raise remote_error

        app.route_geo_reference_batch_worker_outcome(
            batch_runner,
            on_remote_failure=lambda error: events.append(("remote", error)),
            on_local_failure=lambda error: events.append(("local", error)),
            on_success=lambda count, payload: events.append(("success", count, payload)),
        )

        self.assertEqual([event[0] for event in events], ["remote"])
        self.assertIs(events[0][1], remote_error)


if __name__ == "__main__":
    unittest.main()
