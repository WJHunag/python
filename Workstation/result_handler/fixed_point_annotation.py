"""Draw selected fixed-point labels without changing detection decisions."""
from __future__ import annotations

import math
import os
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont
from .emission_point_policy import apply_reference_policy, configured_reference_threshold


def _font_path():
    fonts = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
    for name in ("msjhbd.ttc", "msjh.ttc", "mingliu.ttc"):
        path = fonts / name
        if path.is_file():
            return path
    raise OSError("No supported Traditional Chinese font found")


def overlaps(a, b, margin=6):
    return (a[0] < b[2] + margin and a[2] + margin > b[0]
            and a[1] < b[3] + margin and a[3] + margin > b[1])


def finite_point(value):
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        return None
    try:
        coords = tuple(float(v) for v in value)
    except (ValueError, TypeError):
        return None
    return tuple(round(v) for v in coords) if all(math.isfinite(v) for v in coords) else None


def numbered_smoke_results(fixed_point_info):
    """Share one crop-result ordering between image, EXIF and notification."""
    number = 0
    for data in (fixed_point_info or {}).values():
        if isinstance(data, dict):
            number += 1
            data["smoke_id"] = number
            yield number, data


def smoke_targets(fixed_point_info):
    targets = []
    for number, data in numbered_smoke_results(fixed_point_info):
        bbox = data.get("marker_bbox_xyxy") or data.get("stage1_bbox_xyxy")
        if not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
            continue
        start, end = finite_point(bbox[:2]), finite_point(bbox[2:])
        if start is None or end is None or end[0] <= start[0] or end[1] <= start[1]:
            continue
        targets.append({"point": start, "name": str(number), "hit": False,
                        "radius": 0, "text": str(number), "smoke_id": number,
                        "bbox": (*start, *end)})
    return targets


def extract_targets(fixed_point_info):
    targets, obstacles, seen = [], [], set()
    for data in (fixed_point_info or {}).values():
        if not isinstance(data, dict):
            continue
        for area in data.get("reference_areas", []):
            p = finite_point(area.get("coordinates"))
            if p:
                radius = float(area.get("radius", 50))
                obstacles.append((p[0] - radius, p[1] - radius,
                                  p[0] + radius, p[1] + radius))
        # Keep the selected detection box and its red/green dots readable.
        bbox = data.get("marker_bbox_xyxy")
        if isinstance(bbox, (list, tuple)) and len(bbox) == 4:
            obstacles.append(tuple(bbox))
        candidates = data.get("candidates") or {}
        for details in candidates.values():
            marker = finite_point(details.get("marker_coordinates"))
            if marker:
                padding = int(details.get("marker_radius") or 8) + 3
                obstacles.append((marker[0] - padding, marker[1] - padding,
                                  marker[0] + padding, marker[1] + padding))
        candidate = candidates.get(data.get("matched_candidate")) or {}
        reference = candidate.get("matched_fixed_point") or {}
        if not (data.get("reference_policy") or {}).get("eligible", True):
            continue
        p = finite_point(reference.get("coordinates"))
        if p is None:
            continue
        name = str(reference.get("point_name") or "未知固定點")
        hit = bool(data.get("is_inside"))
        radius = int(data.get("radius", 50))
        for area in data.get("reference_areas", []):
            if area.get("point_name") == name and finite_point(area.get("coordinates")) == p:
                radius = int(area.get("radius", radius))
                break
        key = (p, name)
        if key not in seen:
            seen.add(key)
            targets.append({"point": p, "name": name, "hit": hit,
                            "radius": radius})
    return targets, list(dict.fromkeys(obstacles))


def place_labels(size, targets, obstacles, font_path, font_size=28):
    """Choose the nearest collision-free box; crowded labels may move farther away."""
    width, height = size
    placed = []
    draw = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    marker_boxes = [(t["point"][0] - 15, t["point"][1] - 15,
                     t["point"][0] + 15, t["point"][1] + 15)
                    for t in targets if "smoke_id" not in t]
    for target in targets:
        x, y = target["point"]
        if not (0 <= x < width and 0 <= y < height):
            continue
        text = target.get("text", target["name"])
        chosen = None
        # Smaller text is a last resort, after trying placements at the preferred size.
        for fs in sorted({font_size, max(18, font_size - 4), 18}, reverse=True):
            font = ImageFont.truetype(str(font_path), fs)
            ink = draw.textbbox((0, 0), text, font=font)
            bw, bh = ink[2] - ink[0] + 24, ink[3] - ink[1] + 20
            if "smoke_id" in target:
                bw, bh = ink[2] - ink[0] + 20, ink[3] - ink[1] + 16
            if bw > width - 16 or bh > height - 16:
                continue
            gap = target["radius"] + 18
            candidates = [
                (x + gap, y - bh - 12), (x - gap - bw, y - bh - 12),
                (x + gap, y + 12), (x - gap - bw, y + 12),
                (x - bw // 2, y - gap - bh), (x - bw // 2, y + gap),
            ]
            if "smoke_id" in target:
                left, top, right, bottom = target["bbox"]
                candidates = [(left, top - bh - 8), (right + 8, top),
                              (left - bw - 8, top), (left, bottom + 8)]
            # Full-image fallback prevents edge clipping and finds space in dense scenes.
            step = max(12, fs)
            grid = [(gx, gy) for gy in range(8, height - bh - 7, step)
                    for gx in range(8, width - bw - 7, step)]
            grid.sort(key=lambda pos: (math.hypot(pos[0] + bw / 2 - x,
                                                pos[1] + bh / 2 - y), pos[1], pos[0]))
            for index, (left, top) in enumerate(candidates + grid):
                box = (left, top, left + bw, top + bh)
                if left < 8 or top < 8 or box[2] > width - 8 or box[3] > height - 8:
                    continue
                if any(overlaps(box, obstacle) for obstacle in obstacles + marker_boxes
                       + [label["box"] for label in placed]):
                    continue
                crowded = any(other is not target and math.hypot(
                    other["point"][0] - x, other["point"][1] - y
                ) < target["radius"] + other["radius"] + 30 for other in targets)
                chosen = {**target, "text": text, "box": box, "font_size": fs,
                          "ink": ink, "connector": index != 0 or crowded}
                break
            if chosen:
                break
        if chosen is None:
            continue
        placed.append(chosen)
    return placed


def annotate(image, targets, obstacles, font_path, numbers=()):
    font_size = max(18, min(40, round(image.width * 28 / 1920)))
    marker_boxes = [(t["point"][0] - 15, t["point"][1] - 15,
                     t["point"][0] + 15, t["point"][1] + 15) for t in targets]
    number_labels = place_labels(image.size, numbers, obstacles + marker_boxes, font_path, font_size)
    labels = place_labels(image.size, targets, obstacles + [n["box"] for n in number_labels], font_path, font_size)
    result = image.convert("RGB").copy()
    draw = ImageDraw.Draw(result)
    for label in number_labels + labels:
        x, y = label["point"]
        left, top, right, bottom = label["box"]
        if label["connector"]:
            # A displaced label needs a short leader to identify its point unambiguously.
            end = (max(left, min(x, right)), max(top, min(y, bottom)))
            draw.line(((x, y), end), fill="black", width=5)
            draw.line(((x, y), end), fill="#ffdf00", width=2)
    # Draw boxes after leaders so that lines cannot run through the text.
    for label in number_labels + labels:
        left, top, right, bottom = label["box"]
        draw.rounded_rectangle(label["box"], radius=5, fill="#202020", outline="#eeeeee", width=1)
        font = ImageFont.truetype(str(font_path), label["font_size"])
        ink = label["ink"]
        pad_x = (right - left - (ink[2] - ink[0])) // 2
        pad_y = (bottom - top - (ink[3] - ink[1])) // 2
        draw.text((left + pad_x - ink[0], top + pad_y - ink[1]), label["text"], font=font, fill="white")
    for label in labels:
        x, y = label["point"]
        for fill, width in (("black", 7), ("#ffdf00", 3)):
            draw.line(((x - 8, y), (x + 8, y)), fill=fill, width=width)
            draw.line(((x, y - 8), (x, y + 8)), fill=fill, width=width)
    return result, labels, number_labels


def draw_selected_fixed_points(image_bgr, fixed_point_info, logger, camera_info=None):
    """Return an annotated copy; font/layout failures preserve the existing image."""
    try:
        threshold = (camera_info or {}).get("reference_distance_threshold_px")
        if threshold is not None and configured_reference_threshold(threshold) is None:
            logger.warning("攝影機參考距離門檻無效，改用圖片寬度 1/3：%r", threshold)
        apply_reference_policy(fixed_point_info, image_bgr.shape[1], threshold)
        numbers = smoke_targets(fixed_point_info)
        targets, obstacles = extract_targets(fixed_point_info)
        if not targets and not numbers:
            return image_bgr
        font_path = _font_path()
        pil_image = Image.fromarray(image_bgr[:, :, ::-1])
        result, labels, number_labels = annotate(pil_image, targets, obstacles, font_path, numbers)
    except Exception as exc:
        logger.warning("固定點標籤繪圖失敗，保留原成圖：%s", exc)
        return image_bgr
    for data in fixed_point_info.values():
        if not isinstance(data, dict):
            continue
        number_label = next((item for item in number_labels if item["smoke_id"] == data["smoke_id"]), None)
        if number_label:
            data["smoke_number_annotation"] = {
                "status": "drawn", "text": number_label["text"],
                "label_bbox_xyxy": number_label["box"],
            }
        elif any(item["smoke_id"] == data["smoke_id"] for item in numbers):
            data["smoke_number_annotation"] = {"status": "skipped", "reason": "no_readable_position"}
            logger.warning("煙霧 %s 無可用編號位置，略過編號", data["smoke_id"])
        candidate = (data.get("candidates") or {}).get(data.get("matched_candidate")) or {}
        reference = candidate.get("matched_fixed_point") or {}
        if not (data.get("reference_policy") or {}).get("eligible", True):
            if reference:
                data["fixed_point_annotation"] = {"status": "skipped", "reason": data["reference_policy"]["reason"]}
            continue
        point = finite_point(reference.get("coordinates"))
        if point is None:
            continue
        name = str(reference.get("point_name") or "未知固定點")
        label = next((item for item in labels if item["point"] == point
                      and item["name"] == name), None)
        if label is None:
            data["fixed_point_annotation"] = {"status": "skipped", "reason": "no_readable_position"}
            logger.warning("固定點 %s 無可用標籤位置或超出圖片，略過標籤", name)
            continue
        left, top, right, bottom = label["box"]
        endpoint = (max(left, min(point[0], right)), max(top, min(point[1], bottom)))
        data["fixed_point_annotation"] = {
            "status": "drawn", "point_name": name, "coordinates": point,
            "text": label["text"], "label_bbox_xyxy": label["box"],
            "font_size": label["font_size"], "marker_half_length_px": 8,
            "leader_line": [point, endpoint] if label["connector"] else None,
        }
    return np.asarray(result)[:, :, ::-1].copy()
