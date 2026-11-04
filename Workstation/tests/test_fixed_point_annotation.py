"""Offline layout and receiver rendering checks; no Flask startup or notifications."""
import ast
import copy
import hashlib
import math
import os
import shutil
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import Mock, patch
from uuid import uuid4

import cv2
import numpy as np
from PIL import Image

import image_metadata
from result_handler import fixed_point_annotation as annotation


ROOT = Path(__file__).resolve().parents[1]


@contextmanager
def test_directory():
    # TemporaryDirectory's mode 0700 is inaccessible under the Windows sandbox.
    path = ROOT / (".annotation-test-" + uuid4().hex)
    path.mkdir()
    try:
        yield path
    finally:
        if path.resolve().parent != ROOT.resolve():
            raise ValueError("Test cleanup escaped the workspace")
        shutil.rmtree(path)


def result(point=(500, 300), name="測試點", hit=False, radius=50):
    return {"is_inside": hit, "radius": radius, "matched_candidate": "mask_lowest",
            "reference_areas": [{"point_name": name, "coordinates": point, "radius": radius}],
            "candidates": {"mask_lowest": {"coordinates": point, "matched_fixed_point": {
                "point_name": name, "coordinates": point,
                "wgs84_coords": {"x": 120.7, "y": 24.2}}}}}


class FixedPointAnnotationTests(unittest.TestCase):
    def setUp(self):
        self.image = np.full((620, 1000, 3), (30, 70, 120), dtype=np.uint8)
        self.logger = Mock()
        self.font = annotation._font_path()

    def test_hit_and_miss_use_the_selected_reference(self):
        for hit, text in ((True, "測試點"), (False, "測試點")):
            info = {"crop": result(hit=hit)}
            before = copy.deepcopy(info)
            before["crop"]["smoke_id"] = 1
            annotation.apply_reference_policy(before, self.image.shape[1])
            rendered = annotation.draw_selected_fixed_points(self.image, info, self.logger)
            label = info["crop"].pop("fixed_point_annotation")
            self.assertEqual(info, before)
            self.assertEqual(label["text"], text)
            self.assertEqual(label["coordinates"], (500, 300))
            self.assertEqual(label["marker_half_length_px"], 8)
            self.assertEqual(tuple(rendered[300, 500]), (0, 223, 255))
            self.assertEqual(tuple(self.image[300, 500]), (30, 70, 120))

    def test_no_fixed_point_does_not_load_font_or_modify_pixels(self):
        info = {"crop": {"matched_candidate": "mask_lowest", "candidates": {
            "mask_lowest": {"matched_fixed_point": None}}}}
        before = copy.deepcopy(info)
        before["crop"]["smoke_id"] = 1
        annotation.apply_reference_policy(before, self.image.shape[1])
        with patch.object(annotation, "_font_path", side_effect=AssertionError("No font needed")):
            self.assertIs(annotation.draw_selected_fixed_points(self.image, info, self.logger), self.image)
        self.assertEqual(info, before)

    def test_missing_font_preserves_image_and_decision(self):
        info = {"crop": result()}
        before = copy.deepcopy(info)
        before["crop"]["smoke_id"] = 1
        annotation.apply_reference_policy(before, self.image.shape[1])
        with patch.object(annotation, "_font_path", side_effect=OSError("missing font")):
            self.assertIs(annotation.draw_selected_fixed_points(self.image, info, self.logger), self.image)
        self.assertEqual(info, before)
        self.logger.warning.assert_called_once()

    def test_dense_references_have_nonoverlapping_labels_and_leaders(self):
        info = {str(i): result(point=(490 + i * 20, 310 + i * 12), name=name, hit=i == 1)
                for i, name in enumerate(("A", "B", "C"))}
        annotation.draw_selected_fixed_points(self.image, info, self.logger)
        labels = [data["fixed_point_annotation"] for data in info.values()]
        for i, label in enumerate(labels):
            self.assertEqual(label["status"], "drawn")
            self.assertIsNotNone(label["leader_line"])
            self.assertFalse(any(annotation.overlaps(label["label_bbox_xyxy"], other["label_bbox_xyxy"])
                                 for other in labels[i + 1:]))

    def test_dynamic_radii_drive_crowding_threshold(self):
        targets = [{"point": (300, 250), "name": "A", "hit": False, "radius": 80},
                   {"point": (300, 460), "name": "B", "hit": False, "radius": 100}]
        at_threshold = annotation.place_labels((1000, 620), targets, [], self.font)
        self.assertFalse(at_threshold[0]["connector"])
        targets[1]["point"] = (300, 459)
        below_threshold = annotation.place_labels((1000, 620), targets, [], self.font)
        self.assertTrue(all(label["connector"] for label in below_threshold))
        info = {"crop": result(radius=50)}
        info["crop"]["reference_areas"][0]["radius"] = 80
        extracted, _ = annotation.extract_targets(info)
        self.assertEqual(extracted[0]["radius"], 80)

    def test_four_corners_stay_inside_image(self):
        targets = [{"point": p, "name": str(i), "hit": False, "radius": 50}
                   for i, p in enumerate(((0, 0), (999, 0), (0, 619), (999, 619)))]
        labels = annotation.place_labels((1000, 620), targets, [], self.font)
        self.assertEqual(len(labels), 4)
        for label in labels:
            left, top, right, bottom = label["box"]
            self.assertTrue(8 <= left < right <= 992 and 8 <= top < bottom <= 612)

    def test_duplicate_events_share_one_label(self):
        info = {"a": result(), "b": result()}
        targets, _ = annotation.extract_targets(info)
        self.assertEqual(len(targets), 1)
        annotation.draw_selected_fixed_points(self.image, info, self.logger)
        self.assertEqual(info["a"]["fixed_point_annotation"], info["b"]["fixed_point_annotation"])

    def test_mixed_hits_share_one_reference_label_but_have_three_numbers(self):
        info = {"crop3": result(hit=False), "crop1": result(hit=True), "crop2": result(hit=False)}
        for data, bbox in zip(info.values(), ((100, 100, 130, 140), (480, 280, 520, 300),
                                             (800, 100, 840, 140))):
            data["marker_bbox_xyxy"] = bbox
        targets, _ = annotation.extract_targets(info)
        self.assertEqual(len(targets), 1)
        annotation.draw_selected_fixed_points(self.image, info, self.logger)
        reference_labels = [data["fixed_point_annotation"] for data in info.values()]
        self.assertTrue(all(label == reference_labels[0] for label in reference_labels))
        self.assertEqual(reference_labels[0]["text"], "測試點")
        self.assertEqual([data["smoke_number_annotation"]["text"] for data in info.values()], ["1", "2", "3"])
        number_boxes = [data["smoke_number_annotation"]["label_bbox_xyxy"] for data in info.values()]
        for i, box in enumerate(number_boxes):
            self.assertFalse(any(annotation.overlaps(box, other) for other in number_boxes[i + 1:]))
            self.assertFalse(annotation.overlaps(box, reference_labels[0]["label_bbox_xyxy"]))

    def test_smoke_without_reference_still_has_number(self):
        info = {"crop": {"marker_bbox_xyxy": (300, 200, 340, 240)}}
        annotation.draw_selected_fixed_points(self.image, info, self.logger)
        self.assertEqual(info["crop"]["smoke_number_annotation"]["text"], "1")
        self.assertNotIn("fixed_point_annotation", info["crop"])

    def test_unplaceable_label_does_not_remove_other_labels(self):
        info = {"bad": result(name="很長的名稱" * 100), "good": result(point=(700, 400), name="B")}
        annotation.draw_selected_fixed_points(self.image, info, self.logger)
        self.assertEqual(info["bad"]["fixed_point_annotation"]["status"], "skipped")
        self.assertEqual(info["good"]["fixed_point_annotation"]["status"], "drawn")
        self.logger.warning.assert_called_once()

    def test_detection_and_marker_boxes_are_not_covered(self):
        info = {"crop": result()}
        info["crop"]["marker_bbox_xyxy"] = (550, 200, 820, 320)
        info["crop"]["candidates"]["mask_lowest"].update(
            {"marker_coordinates": (830, 300), "marker_radius": 8})
        targets, obstacles = annotation.extract_targets(info)
        labels = annotation.place_labels((1000, 620), targets, obstacles, self.font)
        self.assertEqual(len(labels), 1)
        self.assertTrue(labels[0]["connector"])
        self.assertFalse(any(annotation.overlaps(labels[0]["box"], box) for box in obstacles))

    def test_receiver_render_and_exif_round_trip_preserve_source(self):
        # Load only pure receiver helpers, avoiding live module initialization.
        path = ROOT / "result_handler/receive_result_server.py"
        tree = ast.parse(path.read_text(encoding="utf-8-sig"))
        helper_names = {"_safe_int", "_get_ratio_detections", "_finite_float",
                        "_get_emission_point_radius", "_normalize_candidate_point",
                        "_normalize_bbox_xyxy", "_resolve_stage1_bbox",
                        "_get_stage1_bbox_candidate_points", "_get_detection_candidate_points",
                        "_offset_candidate_points", "_offset_bbox_xyxy", "_candidate_priority",
                        "_get_fixed_reference_points", "_empty_candidate_match",
                        "_check_candidates_in_fixed_areas", "_detection_match_sort_key",
                        "_create_marked_uncropped_image"}
        functions = [node for node in tree.body if isinstance(node, ast.FunctionDef)
                     and node.name in helper_names]
        constants = [node for node in tree.body if isinstance(node, ast.Assign)
                     and any(isinstance(target, ast.Name) and target.id.startswith((
                         "_DEFAULT_EMISSION_", "_EMISSION_POINT_", "_FIXED_REFERENCE_",
                         "_STAGE1_FALLBACK_", "_STAGE2_PARTIAL_", "_CANDIDATE_POINT_"))
                         for target in node.targets)]
        ns = {"math": math, "cv2": cv2, "os": os, "receive_logger": self.logger,
              "draw_selected_fixed_points": annotation.draw_selected_fixed_points}
        exec(compile(ast.Module(body=constants + functions, type_ignores=[]), str(path), "exec"), ns)
        with test_directory() as tmp:
            source = Path(tmp) / "source.jpg"
            Image.fromarray(self.image[:, :, ::-1]).save(source)
            digest = hashlib.sha256(source.read_bytes()).hexdigest()
            ns["MARKED_OUTPUT_FOLDER"] = os.path.relpath(Path(tmp) / "marked")
            camera = {"fixed_area_radius_px": 80, "reference_distance_threshold_px": 200, "geo_reference": {"points": [
                {"point_name": "測試點", "image_coord": {"x": 500, "y": 300},
                 "wgs84_coords": {"x": 120.7, "y": 24.2}}]}}
            crops = {"crop": {"stage2_enabled": True, "stage1_bbox_xyxy": (400, 200, 480, 301)}}
            source_path = os.path.relpath(source)
            output, info = ns["_create_marked_uncropped_image"](source_path, "result.jpg", crops, camera)
            self.assertNotEqual(output, source_path)
            self.assertEqual(info["crop"]["fixed_point_annotation"]["status"], "drawn")
            self.assertEqual(info["crop"]["radius"], 80)
            metadata = image_metadata.build_marked_metadata({}, filename="result.jpg", group_id="test", fixed_point_info=info)
            with patch.dict(os.environ, {"SMOKE_IMAGE_METADATA": "1"}):
                self.assertTrue(image_metadata.maybe_attach_metadata_to_jpeg_file(output, metadata))
            loaded = image_metadata.read_metadata_from_jpeg_bytes(Path(output).read_bytes())
            saved = loaded["fixed_point_info"]["crop"]
            self.assertEqual(saved["fixed_point_annotation"]["text"], "測試點")
            self.assertEqual(saved["smoke_id"], 1)
            self.assertEqual(saved["smoke_number_annotation"]["text"], "1")
            self.assertEqual(saved["reference_policy"]["threshold_px"], 200)
            self.assertEqual(saved["reference_policy"]["threshold_source"], "camera")
            selected = saved["candidates"][saved["matched_candidate"]]["matched_fixed_point"]
            self.assertEqual(selected["wgs84_coords"], {"x": 120.7, "y": 24.2})
            self.assertEqual(digest, hashlib.sha256(source.read_bytes()).hexdigest())


if __name__ == "__main__":
    unittest.main()
