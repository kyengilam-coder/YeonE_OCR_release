"""Image-only candidates for small vertical columns or horizontal note lines.

The detector uses region types and boxes, never recognized text. It does not
change readings or source images. Geometry is evidence for a targeted reread,
not evidence that text is missing or that a proposed transcription is correct.
"""
from __future__ import annotations

from collections import Counter, defaultdict
import hashlib
import json
import math

import cv2
import numpy as np
from PIL import Image

from .schema import IMAGE_KINDS, PageReading, bbox_frac


DETECTOR_POLICY = "local-double-column-v1"
HORIZONTAL_DETECTOR_POLICY = "local-horizontal-small-lines-v1"
MAX_CANDIDATES = 64
_ANALYSIS_EDGE = 2400
_PARENT_KINDS = {"본문", "설명문", "소주"}


def _runs(on: np.ndarray, *, gap: int = 0, minimum: int = 1) -> list[tuple[int, int]]:
    positions = np.flatnonzero(on)
    if not len(positions):
        return []
    cuts = np.flatnonzero(np.diff(positions) > gap + 1) + 1
    return [(int(part[0]), int(part[-1]) + 1)
            for part in np.split(positions, cuts) if part[-1] - part[0] + 1 >= minimum]


def _pixel_box(box, width: int, height: int) -> tuple[int, int, int, int]:
    x0, y0, x1, y1 = bbox_frac(box)
    return (max(0, int(math.floor(x0 * width))), max(0, int(math.floor(y0 * height))),
            min(width, int(math.ceil(x1 * width))), min(height, int(math.ceil(y1 * height))))


def _intersect(a, b) -> int:
    return max(0, min(a[2], b[2]) - max(a[0], b[0])) * max(0, min(a[3], b[3]) - max(a[1], b[1]))


def _area(box) -> int:
    return max(0, box[2] - box[0]) * max(0, box[3] - box[1])


def _body_pitch(ink: np.ndarray) -> tuple[float, dict]:
    """Estimate large text pitch; disconnected strokes are not counted as glyphs."""
    _, _, stats, centers = cv2.connectedComponentsWithStats(ink.astype(np.uint8), connectivity=8)
    components = []
    for stat, center in zip(stats[1:], centers[1:]):
        _, _, width, height, area = (int(value) for value in stat)
        if (height >= 5 and width >= 3 and area >= 6
                and height <= min(140, ink.shape[0] * 0.35)
                and 0.35 * height <= width <= 2.2 * height):
            components.append((width, height, float(center[0]), float(center[1])))
    if len(components) < 4:
        return 0.0, {"reason": "insufficient_large_text_components", "components": len(components)}
    heights = np.asarray([item[1] for item in components], dtype=float)
    upper = float(np.percentile(heights, 70))
    large = [item for item in components if item[1] >= upper and item[0] >= 0.45 * upper]
    if len(large) < 4:
        return 0.0, {"reason": "insufficient_large_text_components", "components": len(large)}
    extent = float(np.median([item[1] for item in large]))
    columns = defaultdict(list)
    for _, _, x, y in large:
        columns[round(x / max(1.0, extent * 0.5))].append(y)
    distances = []
    for values in columns.values():
        distances.extend(float(d) for d in np.diff(sorted(values)) if 0.9 * extent <= d <= 1.9 * extent)
    pitch = float(np.median(distances)) if len(distances) >= 3 else extent * 1.2
    grid = _column_grid(components, pitch)
    return pitch, {"large_component_height_analysis_px": round(extent, 2), "large_components": len(large),
                   "pitch_from_spacing": len(distances) >= 3, "body_column_grid": grid}


def _column_grid(components: list[tuple], pitch: float) -> dict | None:
    """Use repeated large glyphs to reject pairs crossing two body columns.

    The grid is optional: irregular layouts must not be forced onto one. Width
    selects large glyphs because some woodblock notes retain body-height spacing.
    """
    centers = sorted(item[2] for item in components
                     if 0.8 * pitch <= item[0] <= 1.5 * pitch
                     and 0.6 * pitch <= item[1] <= 1.5 * pitch)
    groups = []
    for center in centers:
        if groups and center - float(np.median(groups[-1])) <= 0.3 * pitch:
            groups[-1].append(center)
        else:
            groups.append([center])
    groups = [group for group in groups if len(group) >= 2]
    if len(groups) < 3:
        return None
    xs = np.asarray([np.median(group) for group in groups])
    weights = np.asarray([len(group) for group in groups])
    best = None
    for origin in xs:
        for other in xs:
            for divisor in range(1, 9):
                spacing = abs(other - origin) / divisor
                if not pitch <= spacing <= 2.2 * pitch:
                    continue
                residual = np.abs(xs - origin - np.rint((xs - origin) / spacing) * spacing)
                matching = residual <= 0.22 * pitch
                support = int(weights[matching].sum())
                if matching.sum() < 3 or support < weights.sum() * 0.85:
                    continue
                score = (support, -float(np.sum(residual[matching] * weights[matching])), spacing)
                if best is None or score > best[0]:
                    best = (score, {"origin": float(origin), "spacing": float(spacing)})
    return best[1] if best else None


def _small_runs(sub: np.ndarray, pitch: float, at_top: bool, at_bottom: bool) -> dict | None:
    runs = _runs(sub.mean(axis=1) >= 0.04, minimum=2)
    joined = []
    for start, end in runs:
        if (joined and start - joined[-1][1] < 0.12 * pitch
                and end - joined[-1][0] <= min(0.95 * pitch, 1.9 * sub.shape[1])):
            joined[-1] = (joined[-1][0], end)
        else:
            joined.append((start, end))
    joined = [(start, end) for start, end in joined
              if 0.12 * pitch <= end - start <= 1.05 * pitch
              and (at_top or start > 0) and (at_bottom or end < sub.shape[0])]
    if len(joined) < 2:
        return None
    # Keep a consecutive sequence: widely separated fragments must not become
    # one small-text column merely because each fragment is narrow.
    sequences = [[joined[0]]]
    for item in joined[1:]:
        previous = sequences[-1][-1]
        distance = (item[0] + item[1] - previous[0] - previous[1]) / 2
        if 0.23 * pitch <= distance <= 1.25 * pitch:
            sequences[-1].append(item)
        else:
            sequences.append([item])
    sequence = max(sequences, key=lambda group: (len(group), group[-1][1] - group[0][0]))
    if len(sequence) < 2:
        return None
    centers = [(start + end) / 2 for start, end in sequence]
    small_pitch = float(np.median(np.diff(centers)))
    extent = float(np.median([end - start for start, end in sequence]))
    if (not 0.23 * pitch <= small_pitch <= 1.25 * pitch
            or extent > 0.95 * pitch or extent > 1.9 * sub.shape[1]):
        return None
    return {"start": sequence[0][0], "end": sequence[-1][1], "runs": len(sequence),
            "pitch": small_pitch, "height": extent}


def _find_pairs(ink: np.ndarray, pitch: float, rejected: Counter, grid: dict | None = None) -> list[dict]:
    height, width = ink.shape
    if pitch < 9 or width < pitch * 0.6 or height < pitch * 1.2:
        return []
    step = max(3, round(pitch * 0.4))
    windows = []
    for factor in (2.8, 4.5):
        window = min(height, max(20, round(pitch * factor)))
        starts = list(range(0, max(1, height - window + 1), step))
        if not starts or starts[-1] != height - window:
            starts.append(height - window)
        windows.extend((y, window) for y in starts)
    found = []
    for y, window in windows:
        stripe = ink[y:y + window]
        bands = _runs(stripe.mean(axis=0) >= 0.035, gap=max(0, round(pitch * 0.045)), minimum=2)
        for left, right in zip(bands, bands[1:]):
            widths = (left[1] - left[0], right[1] - right[0])
            gap = right[0] - left[1]
            total = right[1] - left[0]
            if not (all(0.13 * pitch <= value <= 0.66 * pitch for value in widths)
                    and 0.06 * pitch <= gap <= 0.48 * pitch
                    and 0.5 * pitch <= total <= 1.5 * pitch):
                continue
            if grid:
                center = (left[0] + right[1]) / 2
                nearest = grid["origin"] + round((center - grid["origin"]) / grid["spacing"]) * grid["spacing"]
                if abs(center - nearest) > 0.25 * pitch:
                    rejected["crosses_body_column_grid"] += 1
                    continue
            le = _small_runs(stripe[:, left[0]:left[1]], pitch, y == 0, y + window == height)
            re = _small_runs(stripe[:, right[0]:right[1]], pitch, y == 0, y + window == height)
            if le is None or re is None:
                rejected["no_small_vertical_pitch"] += 1
                continue
            overlap = max(0, min(le["end"], re["end"]) - max(le["start"], re["start"]))
            ratio = overlap / max(1, min(le["end"] - le["start"], re["end"] - re["start"]))
            if ratio < 0.65 or abs(le["start"] - re["start"]) > 0.65 * pitch:
                rejected["subcolumns_not_aligned"] += 1
                continue
            small_pitch = (le["pitch"] + re["pitch"]) / 2
            if max(le["pitch"], re["pitch"]) > 1.5 * min(le["pitch"], re["pitch"]):
                rejected["subcolumn_pitch_mismatch"] += 1
                continue
            found.append({"left": [left[0], y + le["start"], left[1], y + le["end"]],
                          "right": [right[0], y + re["start"], right[1], y + re["end"]],
                          "small_pitch": small_pitch, "runs": [re["runs"], le["runs"]],
                          "overlap": ratio})
    return found


def _merge_pairs(pairs: list[dict], pitch: float) -> list[dict]:
    output = []
    for pair in sorted(pairs, key=lambda item: (min(item["left"][1], item["right"][1]), item["left"][0])):
        target = None
        for old in output:
            if not all(abs((old[side][0] + old[side][2] - pair[side][0] - pair[side][2]) / 2) <= 0.16 * pitch
                       for side in ("left", "right")):
                continue
            old_y = (min(old["left"][1], old["right"][1]), max(old["left"][3], old["right"][3]))
            new_y = (min(pair["left"][1], pair["right"][1]), max(pair["left"][3], pair["right"][3]))
            if max(old_y[0], new_y[0]) - min(old_y[1], new_y[1]) <= min(old["small_pitch"], pair["small_pitch"]) * 0.6:
                target = old
                break
        if target is None:
            output.append({key: list(value) if isinstance(value, list) else value for key, value in pair.items()})
            continue
        for side in ("left", "right"):
            a, b = target[side], pair[side]
            target[side] = [min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3])]
        target["runs"] = [max(a, b) for a, b in zip(target["runs"], pair["runs"])]
        target["overlap"] = max(target["overlap"], pair["overlap"])
    return output


def _extend_pair(pair: dict, ink: np.ndarray, pitch: float) -> dict:
    """Include adjacent small glyphs cut off by a sliding-window boundary.

    Keep a real blank gutter between the two subcolumns. A body glyph spanning
    that gutter stops extension, even when its separate radicals look narrow.
    """
    gutter = ink[:, pair["left"][2]:pair["right"][0]]
    for side in ("right", "left"):
        x0, start, x1, end = pair[side]
        sub = ink[:, x0:x1]
        raw = _runs(sub.mean(axis=1) >= 0.04, minimum=2)
        joined = []
        for a, b in raw:
            if (joined and a - joined[-1][1] < 0.12 * pitch
                    and b - joined[-1][0] <= min(0.95 * pitch, 1.9 * sub.shape[1])):
                joined[-1] = (joined[-1][0], b)
            else:
                joined.append((a, b))
        valid = [(a, b) for a, b in joined
                 if 0.12 * pitch <= b - a <= min(1.05 * pitch, 1.9 * sub.shape[1])]
        for a, b in reversed(valid):
            if a >= start:
                continue
            if start - b > 0.65 * pitch or (gutter.size and gutter[a:b].mean() > 0.12):
                break
            start = a
        for a, b in valid:
            if b <= end:
                continue
            if a - end > 0.65 * pitch or (gutter.size and gutter[a:b].mean() > 0.12):
                break
            end = b
        pair[side] = [x0, start, x1, end]
    return pair


def _normalized(box, width: int, height: int) -> list[int]:
    return [max(0, min(1000, round(box[1] * 1000 / height))),
            max(0, min(1000, round(box[0] * 1000 / width))),
            max(0, min(1000, round(box[3] * 1000 / height))),
            max(0, min(1000, round(box[2] * 1000 / width)))]


def _horizontal_extent(ink: np.ndarray) -> float:
    """Estimate full-sized glyph height without reading the PDF text layer."""
    _, _, stats, _ = cv2.connectedComponentsWithStats(ink.astype(np.uint8), connectivity=8)
    heights = [h for _, _, w, h, area in stats[1:]
               if 5 <= h <= 140 and w >= 3 and area >= 6 and 0.15 * h <= w <= 3 * h]
    if len(heights) < 8:
        return 0.0
    upper = np.percentile(heights, 90)
    return float(np.median([h for h in heights if h >= upper]))


def _horizontal_fragments(ink: np.ndarray, extent: float) -> list[list[int]]:
    # Joining is local to a line. It joins disconnected Korean glyph strokes
    # and normal word gaps without merging the two small lines vertically.
    kernel = np.ones((max(1, round(extent * 0.08)), max(3, round(extent * 0.65))), np.uint8)
    joined = cv2.morphologyEx(ink.astype(np.uint8), cv2.MORPH_CLOSE, kernel)
    _, _, stats, _ = cv2.connectedComponentsWithStats(joined, connectivity=8)
    boxes = []
    for x, y, w, h, area in stats[1:]:
        if w < extent * 0.25 or h < extent * 0.2 or area < extent * 0.25:
            continue
        ys, xs = np.nonzero(ink[y:y+h, x:x+w])
        if len(xs):
            boxes.append([int(x + xs.min()), int(y + ys.min()),
                          int(x + xs.max() + 1), int(y + ys.max() + 1)])
    # Wide word spaces can split a note line after closing. Rejoin only small
    # fragments sharing a baseline, leaving adjacent full-sized body text out.
    lines = []
    for box in sorted(boxes, key=lambda box: (box[0], box[1])):
        target = None
        if box[3]-box[1] <= 0.83 * extent:
            for old in lines:
                overlap = min(box[3],old[3])-max(box[1],old[1])
                if (old[3]-old[1] <= 0.83 * extent and 0 <= box[0]-old[2] <= 2.2 * extent
                        and overlap >= 0.55 * min(box[3]-box[1],old[3]-old[1])
                        and max(box[3],old[3])-min(box[1],old[1]) <= 0.83 * extent):
                    target = old
                    break
        if target is None:
            lines.append(list(box))
        else:
            target[:] = [min(target[0],box[0]), min(target[1],box[1]),max(target[2],box[2]),max(target[3],box[3])]
    return sorted([b for b in lines if b[2]-b[0] >= 1.3 * extent], key=lambda box: (box[1], box[0]))


def _horizontal_groups(boxes: list[list[int]], extent: float, rejected: Counter,
                       reference_boxes: list[list[int]] | None = None) -> list[list[list[int]]]:
    small = [box for box in boxes if 0.25 * extent <= box[3] - box[1] <= 0.83 * extent]
    groups = []
    for box in small:
        target = None
        for group in groups:
            last = group[-1]
            gap = box[1] - last[3]
            overlap = max(0, min(box[2], last[2]) - max(box[0], last[0]))
            if (0.1 * extent <= gap <= 0.9 * extent
                    and abs(box[0] - last[0]) <= 0.65 * extent
                    and overlap >= 0.6 * min(box[2]-box[0], last[2]-last[0])
                    and max(box[3]-box[1], last[3]-last[1]) <= 1.6 * min(box[3]-box[1], last[3]-last[1])):
                target = group
                break
        if target is None:
            groups.append([box])
        else:
            target.append(box)
    output = []
    large = [box for box in (boxes if reference_boxes is None else reference_boxes)
             if 0.9 * extent <= box[3]-box[1] <= 1.6 * extent]
    for group in groups:
        if len(group) < 2:
            rejected["single_small_line"] += 1
            continue
        x0, y0 = min(b[0] for b in group), group[0][1]
        x1, y1 = max(b[2] for b in group), group[-1][3]
        # Ordinary small-font paragraphs elsewhere on the page are insufficient:
        # require larger body text beside the group in the same vertical range.
        neighbors = [b for b in large if min(y1, b[3]) > max(y0, b[1])
                     and (0 <= x0-b[2] <= 2.5*extent or 0 <= b[0]-x1 <= 2.5*extent)]
        if not neighbors:
            rejected["no_adjacent_larger_body"] += 1
            continue
        output.append(group)
    return output


def _horizontal_ruled_boxes(ink: np.ndarray, extent: float) -> list[list[int]]:
    """Hold ruled tables even when no layout region identified them yet."""
    binary = ink.astype(np.uint8)
    horizontal = cv2.morphologyEx(binary, cv2.MORPH_OPEN, np.ones((1, max(20, round(4*extent))), np.uint8))
    vertical = cv2.morphologyEx(binary, cv2.MORPH_OPEN, np.ones((max(12, round(1.5*extent)), 1), np.uint8))
    _, _, stats, _ = cv2.connectedComponentsWithStats(horizontal | vertical, connectivity=8)
    return [[int(x),int(y),int(x+w),int(y+h)] for x,y,w,h,area in stats[1:]
            if w >= 4*extent and h >= 1.5*extent and area >= w+h]


def _detect_horizontal(image: Image.Image, reading: PageReading | None, max_candidates: int, result: dict) -> dict:
    """Detect two or more small horizontal lines; all crops stay unrotated."""
    result["detector"] = HORIZONTAL_DETECTOR_POLICY
    diagnostics = result["diagnostics"]
    diagnostics["limitations"].append("horizontal_notes_require_adjacent_larger_body")
    width, height = image.size
    scale = min(1.0, _ANALYSIS_EDGE / max(width, height))
    aw, ah = max(1, round(width*scale)), max(1, round(height*scale))
    gray = image.convert("L")
    if gray.size != (aw, ah):
        gray = gray.resize((aw, ah), Image.Resampling.BILINEAR)
    pixels = np.asarray(gray)
    if int(pixels.max()) - int(pixels.min()) < 8:
        diagnostics["reason"] = "no_ink_contrast"
        return result
    _, binary = cv2.threshold(pixels, 0, 1, cv2.THRESH_BINARY_INV | cv2.THRESH_OTSU)
    ink = binary.astype(bool)
    regions = list(reading.regions) if reading is not None else []
    parents, excluded = [], []
    for region in regions:
        box = _pixel_box(region.bbox, aw, ah)
        if "세로" in region.reading_direction or "vertical" in region.reading_direction.lower():
            excluded.append(box)
            diagnostics["skipped_regions"].append({"region_id": region.id, "reason": "vertical_region_in_horizontal_mode"})
        elif region.kind in IMAGE_KINDS or region.kind == "단순표":
            excluded.append(box)
            diagnostics["skipped_regions"].append({"region_id": region.id, "reason": "region_type_not_parent"})
        elif region.kind in _PARENT_KINDS:
            parents.append((region.id, box))
        else:
            diagnostics["skipped_regions"].append({"region_id": region.id, "reason": "region_type_not_parent"})
    if not regions:
        parents = [("__page__", (0, 0, aw, ah))]
        diagnostics["virtual_page_parent"] = True
        diagnostics["limitations"].remove("outside_recorded_text_regions_not_examined")
    for x0, y0, x1, y1 in excluded:
        ink[y0:y1, x0:x1] = False
    extent = _horizontal_extent(ink)
    if extent < 8:
        diagnostics["reason"] = "body_height_unconfirmed"
        return result
    ruled = _horizontal_ruled_boxes(ink, extent)
    for x0,y0,x1,y1 in ruled:
        ink[y0:y1,x0:x1] = False
    excluded.extend(ruled)
    diagnostics["ruled_areas_excluded"] = len(ruled)
    context_boxes = _horizontal_fragments(ink, extent)
    rejected, candidates = Counter(), []
    for parent_id, parent in sorted(parents, key=lambda item: (_area(item[1]), item[1][1], item[1][0], item[0])):
        x0, y0, x1, y1 = parent
        if x1-x0 < 12 or y1-y0 < 20:
            diagnostics["skipped_regions"].append({"region_id": parent_id, "reason": "parent_too_small"})
            continue
        diagnostics["regions_examined"] += 1
        context = [[b[0]-x0,b[1]-y0,b[2]-x0,b[3]-y0] for b in context_boxes]
        groups = _horizontal_groups(_horizontal_fragments(ink[y0:y1, x0:x1], extent), extent, rejected, context)
        for group in groups:
            boxes = [[x0+max(0,b[0]-1), y0+max(0,b[1]-1),
                      x0+min(x1-x0,b[2]+1), y0+min(y1-y0,b[3]+1)] for b in group]
            box = [min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes)]
            if any(_intersect(box, blocked) for blocked in excluded):
                rejected["overlap_excluded_region"] += 1
                continue
            if any(_intersect(box, old["analysis_box"]) / max(1, min(_area(box), _area(old["analysis_box"]))) >= 0.75 for old in candidates):
                rejected["overlapping_parent_duplicate"] += 1
                continue
            def convert(b):
                return [max(0, math.floor(b[0]*width/aw)), max(0, math.floor(b[1]*height/ah)),
                        min(width, math.ceil(b[2]*width/aw)), min(height, math.ceil(b[3]*height/ah))]
            pixel_box = convert(box)
            lines = [{"side": f"line-{i}", "bbox_px": convert(b), "bbox": _normalized(convert(b), width, height)}
                     for i, b in enumerate(boxes, 1)]
            geometry = {"policy": HORIZONTAL_DETECTOR_POLICY, "bbox": _normalized(pixel_box,width,height),
                        "subcolumns": [line["bbox"] for line in lines]}
            candidate_id = "sa-" + hashlib.sha256(json.dumps(geometry,sort_keys=True,separators=(",",":")).encode()).hexdigest()[:16]
            candidates.append({"id": candidate_id, "parent_region_id": parent_id, "direction": "horizontal",
                               "bbox": geometry["bbox"], "bbox_px": pixel_box, "subcolumns": lines,
                               "body_pitch_px": round(extent/scale,2),
                               "small_pitch_px": round(float(np.median(np.diff([(b[1]+b[3])/2 for b in group])))/scale,2),
                               "reasons": ["multiple_small_horizontal_lines", "adjacent_larger_body", "aligned_line_starts"],
                               "evidence": {"analysis_scale": round(scale,6), "line_count": len(lines),
                                            "body_height_analysis_px": extent}, "analysis_box": box})
    candidates.sort(key=lambda c: (c["bbox_px"][1],c["bbox_px"][0],c["id"]))
    limit = min(max_candidates, MAX_CANDIDATES)
    diagnostics.update(capped=len(candidates)>limit, candidates_before_cap=len(candidates),
                       rejected_pairs=sum(rejected.values()), rejection_reasons=dict(sorted(rejected.items())))
    for candidate in candidates[:limit]:
        candidate.pop("analysis_box")
        result["candidates"].append(candidate)
    return result


def detect_small_annotations(image: Image.Image, reading: PageReading | None,
                             *, horizontal: bool = False, max_candidates: int = 64) -> dict:
    """Return bounded, deterministic geometry candidates for targeted rereading.

    The whole image is a virtual parent only when no regions were recorded.
    Existing image regions are excluded; recorded small-note regions are also
    checked because their text can still omit one of the two subcolumns. IDs
    depend on detector policy and geometry only; callers bind this evidence to
    the full source-image hash. No text is inserted into ``reading``.
    """
    if isinstance(max_candidates, bool) or not isinstance(max_candidates, int) or max_candidates < 0:
        raise ValueError("max_candidates는 0 이상의 정수여야 합니다.")
    width, height = image.size
    diagnostics = {"regions_examined": 0, "skipped_regions": [], "rejected_pairs": 0,
                   "rejection_reasons": {}, "capped": False,
                   "limitations": ["geometry_candidates_not_verified_omissions", "very_short_or_blurred_notes_may_be_missed",
                                   "candidate_bounds_may_cover_only_part_of_note", "outside_recorded_text_regions_not_examined"]}
    result = {"schema_version": 1, "detector": HORIZONTAL_DETECTOR_POLICY if horizontal else DETECTOR_POLICY,
              "image_size": [width, height],
              "candidates": [], "diagnostics": diagnostics}
    if not max_candidates:
        diagnostics["reason"] = "disabled_by_candidate_limit"
        return result
    if width < 20 or height < 20:
        diagnostics["reason"] = "image_too_small"
        return result
    if horizontal:
        return _detect_horizontal(image, reading, max_candidates, result)
    scale = min(1.0, _ANALYSIS_EDGE / max(width, height))
    analysis_width, analysis_height = max(1, round(width * scale)), max(1, round(height * scale))
    gray = image.convert("L")
    if gray.size != (analysis_width, analysis_height):
        gray = gray.resize((analysis_width, analysis_height), Image.Resampling.BILINEAR)
    pixels = np.asarray(gray)
    if int(pixels.max()) - int(pixels.min()) < 8:
        diagnostics["reason"] = "no_ink_contrast"
        return result
    _, binary = cv2.threshold(pixels, 0, 1, cv2.THRESH_BINARY_INV | cv2.THRESH_OTSU)
    ink = binary.astype(bool)
    regions = list(reading.regions) if reading is not None else []
    excluded = []
    parents = []
    for region in regions:
        box = _pixel_box(region.bbox, analysis_width, analysis_height)
        if "가로" in region.reading_direction or "horizontal" in region.reading_direction.lower():
            excluded.append(box)
            diagnostics["skipped_regions"].append({"region_id": region.id, "reason": "horizontal_region_not_supported"})
            continue
        if region.kind in IMAGE_KINDS:
            excluded.append(box)
        if region.kind in _PARENT_KINDS:
            parents.append((region.id, box, region.kind))
        else:
            diagnostics["skipped_regions"].append({"region_id": region.id, "reason": "region_type_not_parent"})
    if not regions:
        parents = [("__page__", (0, 0, analysis_width, analysis_height), "본문")]
        diagnostics["virtual_page_parent"] = True
        diagnostics["limitations"].remove("outside_recorded_text_regions_not_examined")
    for x0, y0, x1, y1 in excluded:
        ink[y0:y1, x0:x1] = False
    rejected = Counter()
    candidates = []
    page_pitch = None
    for parent_id, parent_box, kind in sorted(parents, key=lambda item: (_area(item[1]), item[1][1], -item[1][2], item[0])):
        x0, y0, x1, y1 = parent_box
        if x1 - x0 < 12 or y1 - y0 < 20:
            diagnostics["skipped_regions"].append({"region_id": parent_id, "reason": "parent_too_small"})
            continue
        diagnostics["regions_examined"] += 1
        sub = ink[y0:y1, x0:x1]
        pitch, pitch_evidence = _body_pitch(sub)
        if kind == "소주":
            if page_pitch is None:
                page_pitch = _body_pitch(ink)
            pitch, pitch_evidence = page_pitch[0], dict(page_pitch[1])
            if pitch_evidence.get("body_column_grid"):
                pitch_evidence["body_column_grid"] = dict(pitch_evidence["body_column_grid"])
                pitch_evidence["body_column_grid"]["origin"] -= x0
            pitch_evidence["pitch_reference"] = "whole_page_for_existing_small_note"
        if pitch < 9:
            diagnostics["skipped_regions"].append({"region_id": parent_id, "reason": "body_pitch_unconfirmed", **pitch_evidence})
            continue
        pairs = _merge_pairs(_find_pairs(sub, pitch, rejected, pitch_evidence["body_column_grid"]), pitch)
        pairs = _merge_pairs([_extend_pair(pair, sub, pitch) for pair in pairs], pitch)
        for pair in pairs:
            source_boxes = []
            for side in ("right", "left"):
                a, b, c, d = pair[side]
                # One analysis pixel of padding retains antialiased edge strokes
                # lost during thresholding. Keep the original gutter midpoint.
                middle = (pair["left"][2] + pair["right"][0]) // 2
                left_limit, right_limit = (middle, x1 - x0) if side == "right" else (0, middle)
                box = [x0 + max(left_limit, a - 1), y0 + max(0, b - 1),
                       x0 + min(right_limit, c + 1), y0 + min(y1 - y0, d + 1)]
                source_boxes.append(box)
            box = [min(b[0] for b in source_boxes), min(b[1] for b in source_boxes),
                   max(b[2] for b in source_boxes), max(b[3] for b in source_boxes)]
            if any(_intersect(box, blocked) > 0 for blocked in excluded):
                rejected["overlap_excluded_region"] += 1
                continue
            if any(_intersect(box, old["analysis_box"]) / max(1, min(_area(box), _area(old["analysis_box"]))) >= 0.75
                   for old in candidates):
                rejected["overlapping_parent_duplicate"] += 1
                continue
            convert = lambda b: [max(0, math.floor(b[0] * width / analysis_width)),
                                 max(0, math.floor(b[1] * height / analysis_height)),
                                 min(width, math.ceil(b[2] * width / analysis_width)),
                                 min(height, math.ceil(b[3] * height / analysis_height))]
            pixel_box = convert(box)
            subcolumns = [{"side": side, "bbox": _normalized(convert(b), width, height), "bbox_px": convert(b)}
                          for side, b in zip(("right", "left"), source_boxes)]
            geometry = {"policy": DETECTOR_POLICY, "bbox": _normalized(pixel_box, width, height),
                        "subcolumns": [item["bbox"] for item in subcolumns]}
            candidate_id = "sa-" + hashlib.sha256(json.dumps(geometry, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:16]
            candidates.append({"id": candidate_id, "parent_region_id": parent_id,
                               "bbox": geometry["bbox"], "bbox_px": pixel_box, "subcolumns": subcolumns,
                               "body_pitch_px": round(pitch / scale, 2), "small_pitch_px": round(pair["small_pitch"] / scale, 2),
                               "reasons": ["two_narrow_vertical_bands", "narrow_glyphs_relative_to_body", "overlapping_subcolumn_height"],
                               "evidence": {"subcolumn_runs": pair["runs"], "vertical_overlap": round(pair["overlap"], 3),
                                            "analysis_scale": round(scale, 6), **pitch_evidence},
                               "analysis_box": box})
    candidates.sort(key=lambda item: (-item["bbox_px"][2], item["bbox_px"][1], item["id"]))
    limit = min(max_candidates, MAX_CANDIDATES)
    diagnostics["capped"] = len(candidates) > limit
    diagnostics["candidates_before_cap"] = len(candidates)
    diagnostics["rejected_pairs"] = sum(rejected.values())
    diagnostics["rejection_reasons"] = dict(sorted(rejected.items()))
    for candidate in candidates[:limit]:
        candidate.pop("analysis_box")
        result["candidates"].append(candidate)
    return result
