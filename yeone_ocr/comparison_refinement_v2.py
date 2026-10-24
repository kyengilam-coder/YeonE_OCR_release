"""Source-only punctuation addresses; raw text and whitespace stay unchanged.

The content projection locates a gap, never supplies replacement characters.
Each suggestion copies exactly one original EasyOCR punctuation codepoint and
requires its actual word/line geometry. This is a machine hypothesis, not a
claim that typography identifies the source's Unicode encoding.
"""
from __future__ import annotations

import unicodedata

from . import comparison as core

ADDRESS_CONTRACT = "punctuation-content-gap-address-v1"


def _punctuation(char):
    return unicodedata.category(char).startswith("P")


def _selector_follows(text, index):
    if index + 1 == len(text):
        return False
    value = ord(text[index + 1])
    return 0xFE00 <= value <= 0xFE0F or 0xE0100 <= value <= 0xE01EF


def _content_projection(text):
    indices = [index for index, char in enumerate(text)
               if not char.isspace() and not _punctuation(char)]
    return "".join(text[index] for index in indices), indices


def _unique_gap(spine, left, right):
    if spine.count(left) != 1 or spine.count(right) != 1:
        return None
    start = spine.index(left) + len(left)
    return start if spine.index(right) == start else None


def local_candidates(baseline, runs, snapshot, policy):
    """Return raw-indexed EasyOCR P→P candidates with immutable provenance.

    ``policy`` has the same bounds/anchor attributes as RefinementPolicy.
    Version dispatch and combining independent Han candidates belong to the
    caller. Historical v1 generation is intentionally not changed here.
    """
    # Import at call time: the legacy module dispatches to this implementation.
    from .comparison_refinement import _box, _current_span, _locate

    if core._run_profile(baseline)[1] != "horizontal":
        return []
    source = runs.get("easyocr")
    if (source is None or source["id"] == baseline["id"]
            or not core._run_complete(baseline) or not core._run_complete(source)
            or source["raw"].get("engine_metadata", {}).get("generation_complete") is False
            or not core._compatible(baseline, source)):
        return []
    text, other = baseline["raw"]["text"], source["raw"]["text"]
    if max(len(text), len(other), len(snapshot)) > policy.max_input_chars:
        return []
    n = policy.anchor_chars
    spine, offsets = _content_projection(text)
    other_spine, other_offsets = _content_projection(other)
    if min(len(spine), len(other_spine)) < n * 2:
        return []
    result = []
    for gap in range(n, len(spine) - n + 1):
        gap_start, gap_end = offsets[gap - 1] + 1, offsets[gap]
        punctuation = [i for i in range(gap_start, gap_end) if _punctuation(text[i])]
        if not punctuation:
            continue
        left, right = spine[gap - n:gap], spine[gap:gap + n]
        if _unique_gap(spine, left, right) != gap:
            continue
        other_gap = _unique_gap(other_spine, left, right)
        if other_gap is None:
            continue
        other_start, other_end = other_offsets[other_gap - 1] + 1, other_offsets[other_gap]
        other_punctuation = [i for i in range(other_start, other_end) if _punctuation(other[i])]
        # Never infer insertions/deletions or align a target by its proposed value.
        if len(punctuation) != len(other_punctuation):
            continue
        context_start = other_offsets[other_gap - n]
        context_end = other_offsets[other_gap + n - 1] + 1
        box = _box(source["raw"], context_start, context_end)
        if box is None:
            continue
        for ordinal, (raw_start, source_start) in enumerate(zip(punctuation, other_punctuation)):
            before, after = text[raw_start], other[source_start]
            if before == after or not core._eligibility(source, after, before)[0]:
                continue
            if _selector_follows(text, raw_start) or _selector_follows(other, source_start):
                continue  # A selector's unchanged codepoint does not preserve a changed base.
            raw_end, source_end = raw_start + 1, source_start + 1
            if raw_start < n or len(text) - raw_end < n:
                continue
            anchors = {"left": text[raw_start - n:raw_start], "right": text[raw_end:raw_end + n]}
            if (not anchors["left"].strip() or not anchors["right"].strip()
                    or _locate(text, **anchors, maximum=policy.punctuation_max_chars) != (raw_start, raw_end)):
                continue
            try:
                start, end = _current_span(text, snapshot, raw_start, raw_end, anchors)
            except core.ComparisonError:
                continue
            address = {
                "contract": ADDRESS_CONTRACT,
                "projection": "exclude-P-star-and-isspace-for-address-only; no normalization",
                "content_left": left, "content_right": right,
                "baseline_content_gap": gap, "source_content_gap": other_gap,
                "baseline_gap_start": gap_start, "baseline_gap_end": gap_end,
                "source_gap_start": other_start, "source_gap_end": other_end,
                "baseline_gap_sha256": core._text_sha(text[gap_start:gap_end]),
                "source_gap_sha256": core._text_sha(other[other_start:other_end]),
                "baseline_punctuation_offsets": punctuation,
                "source_punctuation_offsets": other_punctuation,
                "punctuation_ordinal": ordinal, "punctuation_count": len(punctuation),
                "whitespace_policy": "preserve-baseline-whitespace; replace-one-P-codepoint-only",
            }
            witness = {"run_id": source["id"], "engine": source["engine"],
                "raw_sha256": source["raw_sha256"], "start": source_start, "end": source_end,
                "context_start": context_start, "context_end": context_end, "bbox": box,
                "granularity": source["raw"].get("engine_metadata", {}).get("bbox_granularity", "engine-region")}
            item = {"kind": "punctuation", "engine": source["engine"], "start": start, "end": end,
                "before": before, "after": after, "baseline_start": raw_start, "baseline_end": raw_end,
                "source_run_id": source["id"], "source_raw_sha256": source["raw_sha256"],
                "source_start": source_start, "source_end": source_end, "anchors": anchors,
                "evidence": witness, "address_evidence": address}
            item["id"] = core._digest(item)
            result.append(item)
    return sorted(result, key=lambda item: (item["start"], item["end"], item["engine"], item["id"]))[:policy.max_candidates]
