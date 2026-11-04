"""Test hit-only Discord routing without calling live webhooks."""
import ast
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock


ROOT = Path(__file__).resolve().parents[1]


class ConfigParserStub:
    def read(self, *_args, **_kwargs):
        return None


def load_functions(path, names, namespace):
    tree = ast.parse(path.read_text(encoding="utf-8-sig"))
    selected = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
    if {node.name for node in selected} != set(names):
        raise AssertionError("Requested source functions are missing")
    exec(compile(ast.Module(body=selected, type_ignores=[]), str(path), "exec"), namespace)


class PublicEmissionRoutingTests(unittest.TestCase):
    def setUp(self):
        self.camera = {
            "NVR_channel": 42,
            "location": "測試地點 A",
            "stage2_confidence": 0.6,
        }
        self.send = Mock()
        self.ns = {
            "_get_config_value": lambda key, default=None: {
                "cameras": [self.camera],
                "stage2_cams": [42],
            }.get(key, default),
            "_find_camera_info": lambda _ip, _cameras: self.camera,
            "configparser": SimpleNamespace(ConfigParser=ConfigParserStub),
            "datetime": datetime,
            "_get_discord_webhook": lambda _parser, name: f"test://{name}",
            "_get_notification_channel": Mock(return_value="default"),
            "_is_drive_camera": Mock(return_value=False),
            "bot_logger": Mock(),
            "send_discord_notification": self.send,
            "upload_monitor_smoke": Mock(return_value=None),
        }
        self.bot_path = ROOT / "result_handler/bot.py"

    def test_first_stage_public_message_requires_a_coordinate_hit(self):
        load_functions(self.bot_path, ["discord_smoke_notify"], self.ns)

        self.ns["discord_smoke_notify"]("marked.jpg", "camera-A", 1, "")
        self.send.assert_not_called()

        hit_text = "煙霧1參考座標(緯度,經度)：23.0000,120.0000"
        self.ns["discord_smoke_notify"]("marked.jpg", "camera-A", 1, hit_text)

        self.assertEqual(self.send.call_count, 1)
        args, kwargs = self.send.call_args
        self.assertEqual(args[0], "test://webhook_notify")
        self.assertIn(hit_text, args[1])
        self.assertEqual(kwargs["stage"], 1)

    def test_first_stage_only_camera_does_not_route_to_second_stage_group(self):
        load_functions(self.bot_path, ["discord_smoke_notify"], self.ns)
        self.camera["NVR_channel"] = 41
        hit_text = "煙霧1參考座標(緯度,經度)：23.0000,120.0000"

        self.ns["discord_smoke_notify"]("marked.jpg", "camera-A", 1, hit_text)

        self.assertEqual(self.send.call_count, 1)
        self.assertEqual(self.send.call_args.args[0], "test://webhook_notify")

    def test_stage_two_smoke_requires_both_classifier_approval_and_coordinate_hit(self):
        load_functions(self.bot_path, ["discord_smoke_notify_watersmoke"], self.ns)

        self.ns["discord_smoke_notify_watersmoke"]("marked.jpg", "1", "camera-A", 90, "")
        self.send.assert_not_called()

        hit_text = "煙霧1參考座標(緯度,經度)：23.0000,120.0000"
        self.ns["discord_smoke_notify_watersmoke"]("marked.jpg", "1", "camera-A", 90, hit_text)

        self.assertEqual(self.send.call_count, 1)
        args, kwargs = self.send.call_args
        self.assertEqual(args[0], "test://webhook_smoke")
        self.assertIn(hit_text, args[1])
        self.assertEqual(kwargs["stage"], 2)

    def test_stage_two_water_does_not_go_to_smoke_group(self):
        load_functions(self.bot_path, ["discord_smoke_notify_watersmoke"], self.ns)
        hit_text = "煙霧1參考座標(緯度,經度)：23.0000,120.0000"

        self.ns["discord_smoke_notify_watersmoke"]("marked.jpg", "0", "camera-A", 90, hit_text)

        args, kwargs = self.send.call_args
        self.assertEqual(args[0], "test://webhook_watersmoke")
        self.assertNotIn("test://webhook_smoke", [call.args[0] for call in self.send.call_args_list])
        self.assertNotIn(hit_text, args[1])
        self.assertTrue(kwargs["is_watersmoke"])

    def test_stage_two_smoke_below_confidence_threshold_is_not_published(self):
        load_functions(self.bot_path, ["discord_smoke_notify_watersmoke"], self.ns)
        hit_text = "煙霧1參考座標(緯度,經度)：23.0000,120.0000"

        self.ns["discord_smoke_notify_watersmoke"]("marked.jpg", "1", "camera-A", 50, hit_text)

        self.send.assert_not_called()


if __name__ == "__main__":
    unittest.main()
