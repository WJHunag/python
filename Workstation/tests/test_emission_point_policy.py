"""Feedback policy checks: reference cutoff, test/public messages and display precision."""
import copy
import unittest
from unittest.mock import Mock

import numpy as np

from result_handler.emission_point_policy import apply_reference_policy, format_emission_points, reference_policy
from result_handler.fixed_point_annotation import draw_selected_fixed_points, numbered_smoke_results, extract_targets


def smoke(distance, hit=False, candidate="bbox_bottom_left"):
    return {"is_inside": hit, "radius": 50, "matched_candidate": candidate,
            "marker_bbox_xyxy": (200, 100, 240, 130),
            "reference_areas": [{"point_name": "測試點A", "coordinates": (100, 300), "radius": 50}],
            "candidates": {candidate: {"label": "起煙點" if candidate == "mask_lowest" else "bbox左下",
                "coordinates": (100 + distance, 300), "distance": round(distance, 2),
                "matched_fixed_point": {"point_name": "測試點A", "coordinates": (100, 300),
                    "wgs84_coords": {"x": 120.654321, "y": 23.456789}}}}}


class EmissionPolicyTests(unittest.TestCase):
    def test_camera_override_tightens_or_relaxes_default(self):
        info = {"crop": smoke(300)}
        apply_reference_policy(info, 1920, 200)
        self.assertFalse(info["crop"]["reference_policy"]["eligible"])
        self.assertEqual(format_emission_points(info, numbered_smoke_results), "")
        apply_reference_policy(info, 1920, 1200)
        self.assertTrue(info["crop"]["reference_policy"]["eligible"])
        self.assertEqual(format_emission_points(info, numbered_smoke_results), "")
        self.assertIn("鄰近門檻：1200 px（攝影機設定）", format_emission_points(info, numbered_smoke_results, "test"))
        info = {"crop": smoke(1100)}
        pixels = np.zeros((1080, 1920, 3), dtype=np.uint8)
        draw_selected_fixed_points(pixels, info, Mock(), {"reference_distance_threshold_px": 1200})
        self.assertEqual(info["crop"]["fixed_point_annotation"]["status"], "drawn")
        self.assertEqual(info["crop"]["reference_policy"]["threshold_px"], 1200)

    def test_invalid_and_missing_camera_values_use_dynamic_default(self):
        for value in (None, 0, -1, True, False, "invalid", float("nan"), float("inf")):
            with self.subTest(value=value):
                info = {"crop": smoke(500)}
                apply_reference_policy(info, 1200, value)
                self.assertEqual(info["crop"]["reference_policy"]["threshold_px"], 400)
                self.assertFalse(info["crop"]["reference_policy"]["eligible"])

    def test_inclusive_threshold_and_raw_distance(self):
        self.assertTrue(reference_policy(smoke(640), 1920)["eligible"])
        self.assertFalse(reference_policy(smoke(641), 1920)["eligible"])
        data = smoke(640)
        data["candidates"]["bbox_bottom_left"]["coordinates"] = (740, 301)
        # Stored distance rounds to 640, but actual diagonal distance exceeds it.
        self.assertFalse(reference_policy(data, 1920)["eligible"])
        self.assertFalse(reference_policy(smoke(401), 1200)["eligible"])
        self.assertTrue(reference_policy(smoke(400), 1200)["eligible"])

    def test_hit_ignores_proximity_cutoff(self):
        self.assertTrue(reference_policy(smoke(700, hit=True), 1920)["eligible"])

    def test_public_message_contains_only_hit_coordinates(self):
        info = {"hit": smoke(8.06, True), "near": smoke(215.52), "far": smoke(1081.18),
                "missing": {"is_inside": False}}
        self.assertEqual(format_emission_points(info, numbered_smoke_results, image_width=1920).splitlines(), [
            "煙霧1參考座標(緯度,經度)：23.4568,120.6543",
        ])

    def test_public_message_is_empty_when_all_results_miss(self):
        info = {"near": smoke(215.52), "far": smoke(1081.18), "missing": {"is_inside": False}}
        self.assertEqual(format_emission_points(info, numbered_smoke_results, image_width=1920), "")

    def test_detailed_coordinates_only_apply_to_eligible_smokes(self):
        info = {"first": smoke(1081.18), "second": smoke(8.06, True, "mask_lowest"),
                "third": smoke(966.10)}
        apply_reference_policy(info, 1920)
        text = format_emission_points(info, numbered_smoke_results, "test")
        self.assertIn("煙霧1｜未命中｜候選點：bbox左下", text)
        self.assertIn("超出判定範圍：1031.18 px｜超過鄰近門檻，不提供參考座標", text)
        self.assertIn("煙霧2｜命中測試點A｜候選點：起煙點", text)
        self.assertIn("距離：8.06 px｜位於判定範圍內", text)
        self.assertIn("超出判定範圍：916.10 px", text)
        self.assertEqual(text.count("測試點A經緯度："), 1)
        self.assertIn("測試點A經緯度：經度 120.6543，緯度 23.4568\n座標對應：煙霧2", text)
        self.assertIn("判定半徑：50 px", text)
        self.assertIn("鄰近門檻：640 px（圖片寬度 1920 px 的 1/3）", text)

    def test_all_far_results_do_not_list_coordinates(self):
        info = {"a": smoke(700), "b": smoke(900)}
        text = format_emission_points(info, numbered_smoke_results, "test", 1920)
        self.assertNotIn("120.6543", text)
        apply_reference_policy(info, 1920)
        self.assertEqual(extract_targets(info)[0], [])

    def test_filter_drawing_keeps_numbers_and_shared_eligible_reference(self):
        info = {"far": smoke(700), "hit": smoke(8.06, True)}
        pixels = np.zeros((1080, 1920, 3), dtype=np.uint8)
        draw_selected_fixed_points(pixels, info, Mock())
        self.assertEqual(info["far"]["fixed_point_annotation"]["reason"], "too_far")
        self.assertEqual(info["hit"]["fixed_point_annotation"]["status"], "drawn")
        self.assertEqual(info["far"]["smoke_number_annotation"]["text"], "1")
        self.assertEqual(info["hit"]["smoke_number_annotation"]["text"], "2")

    def test_display_does_not_round_or_replace_raw_reference(self):
        info = {"crop": smoke(200)}
        original = copy.deepcopy(info["crop"]["candidates"])
        apply_reference_policy(info, 1920)
        for style in ("test", "public"):
            format_emission_points(info, numbered_smoke_results, style)
        draw_selected_fixed_points(np.zeros((1080, 1920, 3), dtype=np.uint8), info, Mock())
        self.assertEqual(info["crop"]["candidates"], original)


if __name__ == "__main__":
    unittest.main()
