"""Local reading-direction evidence from raster text lines (no OCR/API calls).

This deliberately leaves photographs, blank pages and ambiguous square grids
undecided. A page's shape alone is never evidence for its reading direction.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from PIL import Image


DETECTION_VERSION = 1


@dataclass(frozen=True)
class DirectionEvidence:
    mode: str | None = None
    horizontal: float = 0.0
    vertical: float = 0.0
    lines: int = 0
    confidence: float = 0.0


def _line_score(mask: np.ndarray, ink_pixels: np.ndarray, scale: float,
                horizontal: bool, bridge: float) -> tuple[float, int]:
    import cv2

    # Bridge letter/radical gaps but not the larger gaps between text lines.
    along = max(2, round(scale * bridge))
    across = max(1, round(scale * 0.12))
    kernel = (along, across) if horizontal else (across, along)
    joined = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones(kernel[::-1], np.uint8))
    _, labels, stats, _ = cv2.connectedComponentsWithStats(joined, 8)
    ink = np.bincount(labels.ravel()[ink_pixels], minlength=len(stats))
    score = 0.0
    lines = 0
    for i, (_, _, width, height, _) in enumerate(stats[1:], 1):
        length, thickness = (width, height) if horizontal else (height, width)
        if not (0.65 * scale <= thickness <= 1.6 * scale and length >= 2.5 * scale
                and length >= 3 * thickness):
            continue
        density = ink[i] / (length * thickness)
        if not 0.10 <= density <= 0.72:
            continue
        score += float(ink[i]) / (1 + bridge)
        lines += 1
    return score, lines


def image_direction(image: Image.Image) -> DirectionEvidence:
    """Find repeated text-like rows/columns; return ``mode=None`` when unclear.

    Analysis is bounded to 1,800 pixels on the longer side. Both foreground
    polarities work, so a dark screenshot does not need a separate setting.
    """
    import cv2

    gray_image = image.convert("L")
    gray_image.thumbnail((1800, 1800), Image.Resampling.LANCZOS)
    gray = np.asarray(gray_image)
    if min(gray.shape) < 12 or int(gray.max()) - int(gray.min()) < 25:
        return DirectionEvidence()
    _, binary = cv2.threshold(gray, 0, 1, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    if binary.mean() > 0.5:
        binary = 1 - binary
    count, labels, stats, _ = cv2.connectedComponentsWithStats(binary, 8)
    area = gray.size
    height, width = gray.shape
    keep = np.zeros(count, np.uint8)
    sizes, weights = [], []
    for i, (_, _, cw, ch, ink) in enumerate(stats[1:], 1):
        # Discard large pictures, rules, borders and tiny isolated noise.
        if (ink < 3 or ink > area * 0.025 or max(cw, ch) > max(width, height) * 0.25
                or max(cw, ch) > 16 * min(cw, ch)):
            continue
        keep[i] = 1
        if ink >= 8 and max(cw, ch) >= 5:
            sizes.append(max(cw, ch))
            weights.append(ink)
    if len(sizes) < 8:
        return DirectionEvidence()
    mask = keep[labels]
    total = int(mask.sum())
    if total < 60:
        return DirectionEvidence()
    sizes = np.asarray(sizes)
    weights = np.asarray(weights)
    order = np.argsort(sizes)
    cumulative = np.cumsum(weights[order]) / weights.sum()
    # Area-weighted sizes emphasize whole characters over their tiny radicals.
    scales = {float(sizes[order[np.searchsorted(cumulative, q)]]) * factor
              for q in (0.35, 0.65, 0.85) for factor in (1.0, 1.25)}
    ys, xs = np.nonzero(mask)
    # Empty margins contribute no directional evidence. Keep padding so closing
    # near the crop edge has the same behavior as on the original page.
    padding = int(max(scales)) + 2
    mask = np.pad(mask[ys.min():ys.max() + 1, xs.min():xs.max() + 1], padding)
    ink_pixels = np.flatnonzero(mask)
    best = []
    for horizontal in (True, False):
        best.append(max((_line_score(mask, ink_pixels, scale, horizontal, bridge)
                         for scale in scales for bridge in (0.20, 0.40, 0.65, 0.90)),
                        key=lambda item: item[0]))
    (hs, hn), (vs, vn) = best
    winning, losing = max(hs, vs), min(hs, vs)
    confidence = (winning - losing) / max(winning, 1)
    lines = hn if hs > vs else vn
    mode = None
    if winning >= total * 0.30 and confidence >= 0.25 and lines:
        mode = "horizontal_zh" if hs > vs else "vertical"
    return DirectionEvidence(mode, hs / total, vs / total, lines, confidence)
