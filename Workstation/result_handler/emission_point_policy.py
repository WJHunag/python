"""Shared reference eligibility and display formats; raw detection data stays intact."""
import math


def _number(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def selected_reference(data):
    candidate = (data.get("candidates") or {}).get(data.get("matched_candidate")) or {}
    return candidate, candidate.get("matched_fixed_point") or {}


def configured_reference_threshold(value):
    """Missing/invalid settings use the dynamic default, never a fixed 640px."""
    number = _number(value)
    return number if not isinstance(value, bool) and number is not None and number > 0 else None


def reference_policy(data, image_width=None):
    candidate, reference = selected_reference(data)
    distance = None
    a, b = candidate.get("coordinates"), reference.get("coordinates")
    if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)) and len(a) == len(b) == 2:
        values = [_number(v) for v in (*a, *b)]
        if all(v is not None for v in values):
            distance = math.hypot(values[0] - values[2], values[1] - values[3])
    if distance is None:
        distance = _number(candidate.get("distance"))
    width = _number(image_width)
    if width is None:
        width = _number((data.get("reference_policy") or {}).get("image_width"))
    configured = configured_reference_threshold(data.get(
        "reference_distance_threshold_px",
        (data.get("reference_policy") or {}).get("configured_threshold_px"),
    ))
    threshold = configured if configured is not None else width / 3 if width is not None and width > 0 else None
    if not reference:
        eligible, reason = False, "no_reference"
    elif data.get("is_inside"):
        eligible, reason = True, "hit"
    elif distance is None or threshold is None:
        eligible, reason = False, "missing_distance_or_width"
    elif distance <= threshold:
        eligible, reason = True, "nearby"
    else:
        eligible, reason = False, "too_far"
    return {"image_width": width, "threshold_px": threshold,
            "configured_threshold_px": configured,
            "threshold_source": "camera" if configured is not None else "image_width_third",
            "distance_px": distance, "eligible": eligible, "reason": reason}


def apply_reference_policy(fixed_point_info, image_width, reference_distance_threshold_px=None):
    for data in (fixed_point_info or {}).values():
        if isinstance(data, dict):
            data["reference_distance_threshold_px"] = configured_reference_threshold(reference_distance_threshold_px)
            data["reference_policy"] = reference_policy(data, image_width)


def _coordinates(reference):
    coords = reference.get("wgs84_coords") or {}
    longitude, latitude = _number(coords.get("x")), _number(coords.get("y"))
    if longitude is None or latitude is None:
        return None
    return longitude, latitude


def format_emission_points(fixed_point_info, numbered_results, style="public", image_width=None):
    if style not in ("test", "public"):
        raise ValueError("Unknown emission notification style")
    blocks, references, radii, thresholds = [], {}, [], []
    has_miss = False
    for number, data in numbered_results(fixed_point_info):
        candidate, reference = selected_reference(data)
        policy = reference_policy(data, image_width)
        coords = _coordinates(reference)
        # WGS84 stays x=longitude/y=latitude internally; public text is latitude,longitude for Google Maps.
        coordinate_text = f"{coords[1]:.4f},{coords[0]:.4f}" if coords else "固定點經緯度未設定或資料無效"
        hit = bool(data.get("is_inside"))
        has_miss = has_miss or not hit
        if style == "public":
            if not hit:
                continue
            blocks.append(f"煙霧{number}參考座標(緯度,經度)：" + coordinate_text)
            continue

        name = str(reference.get("point_name") or "未知固定點")
        label = candidate.get("label") or data.get("matched_candidate") or "未知候選點"
        block = [f"煙霧{number}｜{'命中' + name if hit else '未命中'}｜候選點：{label}"]
        distance, radius = policy["distance_px"], _number(data.get("radius"))
        if radius is not None and radius not in radii:
            radii.append(radius)
        threshold_description = (policy["threshold_px"], policy["image_width"], policy["threshold_source"])
        if policy["threshold_px"] is not None and threshold_description not in thresholds:
            thresholds.append(threshold_description)
        if not reference:
            block.append("無有效固定點，不提供參考座標")
        else:
            distance_text = f"{distance:.2f} px" if distance is not None else "未知"
            if hit:
                block.append(f"距離：{distance_text}｜位於判定範圍內")
            else:
                block.append(f"最近固定點：{name}｜距離：{distance_text}")
                beyond = f"{max(0, distance - radius):.2f} px" if distance is not None and radius is not None else "未知"
                note = ("超過鄰近門檻，不提供參考座標" if policy["reason"] == "too_far"
                        else "位於鄰近門檻內" if policy["eligible"]
                        else "缺少距離或圖片寬度，不提供參考座標")
                block.append(f"超出判定範圍：{beyond}｜{note}")
            if policy["eligible"]:
                if coords:
                    # Never merge distinct fixed points just because four-decimal text matches.
                    key = (name, tuple(reference.get("coordinates") or ()), *coords)
                    references.setdefault(key, []).append(number)
                else:
                    block.append(f"{name}經緯度：未設定或資料無效")
        blocks.append("\n".join(block))
    if style == "public" or not blocks:
        return "\n".join(blocks)
    summary = []
    for (name, point, longitude, latitude), numbers in references.items():
        summary.extend([f"{name}經緯度：經度 {longitude:.4f}，緯度 {latitude:.4f}",
                        "座標對應：" + "、".join(f"煙霧{n}" for n in numbers)])
    if radii:
        summary.append("判定半徑：" + "、".join(f"{r:g} px" for r in radii))
    for threshold, width, source in thresholds:
        description = "攝影機設定" if source == "camera" else f"圖片寬度 {width:g} px 的 1/3"
        summary.append(f"鄰近門檻：{threshold:g} px（{description}）")
    if has_miss:
        summary.append("未命中項目的實際地理位置無法確認。")
    return "\n\n".join(blocks + (["\n".join(summary)] if summary else []))
