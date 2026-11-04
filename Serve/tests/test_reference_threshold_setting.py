"""Isolated settings adapters and temporary distributed storage; no live server import."""
import ast
import copy
import math
import re
import tempfile
import time
import unittest
from collections import OrderedDict
from pathlib import Path

from smoke_config.distributed_config_store import DistributedConfigStore


class ReferenceThresholdSettingTests(unittest.TestCase):
    def setUp(self):
        source = Path(__file__).resolve().parents[1] / "frontend/dashboard_v2/smoke_UI_server.py"
        tree = ast.parse(source.read_text(encoding="utf-8-sig"))
        names = {"_to_int_id", "_index_cameras_by_id", "_validate_single_setting",
                 "_apply_single_setting", "build_ui_config_from_total_config", "_camera_signature"}
        nodes = [ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)]
        nodes += [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
        self.ns = {"deepcopy": copy.deepcopy, "math": math, "re": re, "time": time,
                   "OrderedDict": OrderedDict, "_camera_b64": {}, "IMAGE_BASE64_TTL": 300,
                   "_disconnection_map": {}, "_current_polling_map": {}, "DEFAULT_POLLING_TIME": 30}
        exec(compile(ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[])), str(source), "exec"), self.ns)
        self.config = {"cameras": [{"NVR_channel": "1", "ip": "test-camera", "location": "test",
                                   "function_switches": {"enable_smoke_notify": True},
                                   "geo_reference": {"points": []}}],
                       "workstation_ip_mapping": {"127.0.0.1": {"fixed_cameras": [1], "polling_cameras": []}}}

    def item(self, value):
        return {"NVR_channel": 1, "function_switches": {"reference_distance_threshold_px": value}}

    def test_validation_rejects_invalid_and_accepts_auto(self):
        for value in (None, 200, 1200.5):
            self.assertTrue(self.ns["_validate_single_setting"](self.item(value))[0])
        for value in (0, -1, True, False, float("nan"), float("inf"), "bad", ""):
            self.assertFalse(self.ns["_validate_single_setting"](self.item(value))[0])

    def test_save_ui_roundtrip_partial_update_and_clear(self):
        edited = self.ns["_apply_single_setting"](self.config, self.item(200))
        self.assertNotIn("reference_distance_threshold_px", self.config["cameras"][0])
        camera = edited["cameras"][0]
        self.assertEqual(camera["reference_distance_threshold_px"], 200)
        self.assertNotIn("reference_distance_threshold_px", camera["function_switches"])
        ui = self.ns["build_ui_config_from_total_config"](edited)
        self.assertEqual(ui["cameras"][0]["function_switches"]["reference_distance_threshold_px"], 200)
        self.assertNotEqual(self.ns["_camera_signature"](camera), self.ns["_camera_signature"](self.config["cameras"][0]))
        partial = self.ns["_apply_single_setting"](edited, {"NVR_channel": 1, "function_switches": {}})
        self.assertEqual(partial["cameras"][0]["reference_distance_threshold_px"], 200)
        cleared = self.ns["_apply_single_setting"](edited, self.item(None))
        self.assertNotIn("reference_distance_threshold_px", cleared["cameras"][0])
        self.assertIsNone(self.ns["build_ui_config_from_total_config"](cleared)["cameras"][0]["function_switches"]["reference_distance_threshold_px"])

    def test_distributed_storage_preserves_camera_threshold(self):
        edited = self.ns["_apply_single_setting"](self.config, self.item(1200))
        with tempfile.TemporaryDirectory() as folder:
            store = DistributedConfigStore(distributed_root=Path(folder) / "distributed_config")
            store.save_total_config(edited)
            dispatched = store.build_workstation_config("127.0.0.1")
            self.assertEqual(dispatched["cameras"][0]["reference_distance_threshold_px"], 1200)
            self.assertEqual(dispatched["cameras"][0]["geo_reference"], {"points": []})


if __name__ == "__main__":
    unittest.main()
