"""Exercise notification data flow without importing the live Flask/bot modules."""
import ast
import math
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock
from result_handler.fixed_point_annotation import numbered_smoke_results
from result_handler.emission_point_policy import format_emission_points


ROOT = Path(__file__).resolve().parents[1]


def load_functions(path, names, namespace):
    tree = ast.parse(path.read_text(encoding="utf-8-sig"))
    selected = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
    if {node.name for node in selected} != set(names):
        raise AssertionError("Requested source functions are missing")
    exec(compile(ast.Module(body=selected, type_ignores=[]), str(path), "exec"), namespace)


class EmissionPointNotificationTests(unittest.TestCase):
    def setUp(self):
        self.ns = {"math": math, "numbered_smoke_results": numbered_smoke_results, "format_emission_points": format_emission_points}
        path = ROOT / "result_handler/receive_result_server.py"
        tree = ast.parse(path.read_text(encoding="utf-8-sig"))
        constants = [node for node in tree.body if isinstance(node, ast.Assign)
                     and any(isinstance(target, ast.Name) and target.id.startswith((
                         "_CANDIDATE_POINT_", "_DEFAULT_EMISSION_POINT_")) for target in node.targets)]
        exec(compile(ast.Module(body=constants, type_ignores=[]), str(path), "exec"), self.ns)
        load_functions(path, ["_safe_int", "_finite_float", "_candidate_priority",
                              "_get_fixed_reference_points", "_empty_candidate_match",
                              "_check_candidates_in_fixed_areas", "_format_emission_point_text"], self.ns)

    @staticmethod
    def point(name, x, longitude, latitude):
        return {"point_name": name, "image_coord": {"x": x, "y": 0},
                "wgs84_coords": {"x": longitude, "y": latitude}}

    def match(self, candidates, points):
        return self.ns["_check_candidates_in_fixed_areas"](
            candidates, {"geo_reference": {"points": points}}, radius=50)

    def text(self, result):
        return self.ns["_format_emission_point_text"]({"crop.jpg": result}, image_width=1920)

    def test_hit_uses_nearest_selected_point_coordinates(self):
        points = [self.point("測試點 A", 0, 120.1, 23.1),
                  self.point("測試點 B", 40, 121.2, 24.2)]
        result = self.match({"bbox_bottom_left": (35, 0)}, points)
        self.assertEqual(result["point_name"], "測試點 B")
        text = self.text(result)
        self.assertEqual(text, "煙霧1參考座標(緯度,經度)：24.2000,121.2000")
        self.assertNotIn("120.1000", text)

    def test_miss_labels_reference_without_claiming_estimated_location(self):
        result = self.match({"bbox_bottom_left": (1098, 20)},
                            [self.point("測試點 A", 0, 120.654321, 23.456789)])
        text = self.text(result)
        self.assertEqual(text, "")
        detailed_text = self.ns["_format_emission_point_text"]({"crop.jpg": result}, style="test")
        self.assertIn("煙霧1｜未命中", detailed_text)
        self.assertNotIn("120.6543", detailed_text)

    def test_no_reference_is_not_reported_as_a_location(self):
        for geo_reference in (None, {}, {"points": []}):
            with self.subTest(geo_reference=geo_reference):
                result = self.ns["_check_candidates_in_fixed_areas"](
                    {"mask_lowest": (10, 10)}, {"geo_reference": geo_reference})
                text = self.text(result)
                self.assertEqual(text, "")
                detailed_text = self.ns["_format_emission_point_text"]({"crop.jpg": result}, style="test")
                self.assertIn("無有效固定點，不提供參考座標", detailed_text)

    def test_invalid_geographic_coordinates_keep_pixel_match(self):
        for coords in (None, {}, {"x": float("nan"), "y": 23}, "invalid"):
            with self.subTest(coords=coords):
                point = self.point("測試點 A", 0, 120, 23)
                point["wgs84_coords"] = coords
                result = self.match({"mask_lowest": (0, 0)}, [point])
                self.assertTrue(result["is_inside"])
                self.assertIn("固定點經緯度未設定或資料無效", self.text(result))

    def test_formatted_marked_metadata_preserves_reference_coordinates(self):
        result = self.match({"mask_lowest": (1, 0)}, [self.point("A", 0, 120, 23)])
        # The renderer serializes candidate details, not the helper's min_distance.
        data = {key: result[key] for key in ("point_name", "is_inside", "matched_candidate", "candidates", "radius")}
        text = self.text(data)
        self.assertEqual(text, "煙霧1參考座標(緯度,經度)：23.0000,120.0000")

    def test_multiple_crops_are_separate_numbered_blocks(self):
        result = self.match({"mask_lowest": (0, 0)}, [self.point("A", 0, 120, 23)])
        text = self.ns["_format_emission_point_text"]({"a.jpg": result, "b.jpg": result})
        self.assertTrue(text.startswith("煙霧1參考座標(緯度,經度)："))
        self.assertIn("\n煙霧2參考座標(緯度,經度)：", text)

    def test_empty_result_does_not_create_notification_text(self):
        self.assertEqual(self.ns["_format_emission_point_text"]({}), "")

    def test_discord_message_has_camera_label_and_compact_lines(self):
        send = Mock()
        ns = {"_get_config_value": Mock(return_value=[{}]),
              "_find_camera_info": Mock(return_value={"location": "后里區 B"}),
              "configparser": Mock(), "datetime": datetime,
              "_get_discord_webhook": Mock(return_value="unused-test-webhook"),
              "bot_logger": Mock(), "send_discord_notification": send}
        load_functions(ROOT / "result_handler/bot.py", ["discord_emission_point_test_notify"], ns)
        result = self.match({"mask_lowest": (0, 0)}, [self.point("A", 0, 120, 23)])
        text = self.ns["_format_emission_point_text"]({"crop.jpg": result}, style="test")
        ns["discord_emission_point_test_notify"]("test.jpg", "test-camera", text)
        message = send.call_args.args[1]
        self.assertTrue(message.startswith("檢測到煙霧\n時間："))
        self.assertIn("\n攝影機位置：后里區 B\n" + text, message)
        self.assertIn("煙霧1｜命中A", message)
        self.assertIn("距離：0.00 px｜位於判定範圍內", message)

    def test_three_smokes_use_their_own_reference_in_shared_order(self):
        points = [self.point("B", 0, 120.1, 23.1), self.point("A", 1000, 120.2, 23.2)]
        results = {"crop3": self.match({"mask_lowest": (120, 0)}, points),
                   "crop1": self.match({"mask_lowest": (0, 0)}, points),
                   "crop2": self.match({"mask_lowest": (1100, 0)}, points)}
        text = self.ns["_format_emission_point_text"](results, image_width=1920)
        self.assertEqual(text.splitlines(), ["煙霧2參考座標(緯度,經度)：23.1000,120.1000"])
        self.assertEqual([data["smoke_id"] for data in results.values()], [1, 2, 3])


if __name__ == "__main__":
    unittest.main()
