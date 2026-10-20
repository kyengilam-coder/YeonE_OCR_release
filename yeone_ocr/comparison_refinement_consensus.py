"""Literal multi-family Han/Hangul proposals; no reference text or normalization."""
from __future__ import annotations

import difflib
import unicodedata

from . import comparison as core
from . import comparison_fidelity_v2 as fidelity


def _projection(text):
    indexes = tuple(i for i, c in enumerate(text)
                    if not c.isspace() and not unicodedata.category(c).startswith("P"))
    return "".join(text[i] for i in indexes), indexes


def _script(text):
    if text and all(core._han(c) for c in text):
        return "han"
    if text and all(core._hangul(c) for c in text):
        return "hangul"
    return None


def local_candidates(baseline, runs, snapshot, policy):
    from .comparison_refinement import _box, _current_span, _locate

    text = baseline["raw"]["text"]
    if (core._run_profile(baseline)[1] != "horizontal"
            or max(len(text), len(snapshot)) > policy.max_input_chars):
        return []
    n = policy.anchor_chars
    key, indexes = _projection(text)
    sources = {}
    for engine, run in runs.items():
        other = run["raw"]["text"]
        if (run["id"] != baseline["id"] and core._compatible(baseline, run)
                and core._run_complete(run) and len(other) <= policy.max_input_chars):
            sources[engine] = (run, other, *_projection(other))
    targets = set()
    for run, other, projected, positions in sources.values():
        for tag, a, b, c, d in difflib.SequenceMatcher(None, key, projected, autojunk=False).get_opcodes():
            if tag != "replace" or not 0 < b-a == d-c <= policy.han_max_chars:
                continue
            start, end = indexes[a], indexes[b-1]+1
            before, after = text[start:end], other[positions[c]:positions[d-1]+1]
            if _script(before) and _script(before) == _script(after) and len(before) == len(after):
                targets.add((a, b, before, after))
    result = []
    baseline_family = fidelity.ENGINE_FAMILIES[baseline["engine"]]
    dictionary = fidelity.dictionary_manifest()
    for a, b, before, after in sorted(targets):
        start, end = indexes[a], indexes[b-1]+1
        if a < n or len(key)-b < n or start < n or len(text)-end < n:
            continue
        changes = difflib.SequenceMatcher(None, before, after, autojunk=False).get_opcodes()
        han_change = _script(before) == "han"
        if (han_change and (not dictionary["available"] or any(
                fidelity.form_related(old, new)
                for tag, ca, cb, da, db in changes if tag != "equal"
                for old in before[ca:cb] for new in after[da:db]))
                or fidelity._protected_unicode_changed(text, start, end, after, changes)):
            continue
        left, right = key[a-n:a], key[b:b+n]
        if fidelity._pair_locations(key, left, right, b-a) != [a-n]:
            continue
        if start-indexes[a-n] > 64 or indexes[b+n-1]-end > 64:
            continue
        anchors = {"left": text[start-n:start], "right": text[end:end+n]}
        if _locate(text, **anchors, maximum=policy.han_max_chars) != (start, end):
            continue
        try:
            current_start, current_end = _current_span(text, snapshot, start, end, anchors)
        except core.ComparisonError:
            continue
        supporting, conflict, witness = [], False, None
        for engine, (run, other, projected, positions) in sorted(sources.items()):
            family = fidelity.ENGINE_FAMILIES[engine]
            if family == baseline_family or not core._eligibility(run, after, before)[0]:
                continue
            locations = fidelity._pair_locations(projected, left, right, len(before))
            if len(locations) != 1:
                continue
            location = locations[0]
            middle = location+n
            c, d = positions[middle], positions[middle+len(before)-1]+1
            ca, cb = positions[location], positions[middle+len(before)+n-1]+1
            if c-ca > 64 or cb-d > 64:
                continue
            if other[c:d] != after:
                conflict = True
                continue
            supporting.append({"engine": engine, "family": family, "run_id": run["id"],
                "raw_sha256": run["raw_sha256"], "start": c, "end": d,
                "context_start": ca, "context_end": cb,
                "context_sha256": core._text_sha(other[ca:cb])})
            box = _box(run["raw"], ca, cb) if engine != "glmocr" else None
            if witness is None and box is not None:
                witness = {"run_id": run["id"], "engine": engine, "raw_sha256": run["raw_sha256"],
                    "start": c, "end": d, "context_start": ca, "context_end": cb, "bbox": box,
                    "granularity": run["raw"].get("engine_metadata", {}).get("bbox_granularity", "engine-region")}
        if conflict or len({row["family"] for row in supporting}) < 2 or witness is None:
            continue
        source = supporting[0]
        item = {"kind": _script(before), "engine": source["engine"],
            "start": current_start, "end": current_end, "before": before, "after": after,
            "baseline_start": start, "baseline_end": end, "source_run_id": source["run_id"],
            "source_raw_sha256": source["raw_sha256"], "source_start": source["start"],
            "source_end": source["end"], "anchors": anchors, "evidence": witness,
            "consensus_evidence": {"version": "literal-content-consensus-v1",
                "supporting": supporting, "families": sorted({row["family"] for row in supporting}),
                "baseline_family": baseline_family, "left": left, "right": right,
                "variant_protection": "source-fidelity-v2", "normalization": False,
                "dictionary_manifest": dictionary if han_change else None}}
        item["id"] = core._digest(item)
        result.append(item)
    return sorted(result, key=lambda p: (p["start"], p["end"], p["engine"], p["id"]))[:policy.max_candidates]
