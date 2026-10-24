"""Literal Han suggestions from bounded, uniquely anchored disagreement windows.

Eight content characters is twice the ordinary four-character target bound:
it can contain adjacent OCR errors without allowing sentence rewrites. A
suggestion still changes one literal character, needs three independent image
OCR families, and preserves every known form/compatibility relationship.
"""
from __future__ import annotations

import difflib

from . import comparison as core
from . import comparison_fidelity_v2 as fidelity
from . import comparison_unihan as unihan
from .comparison_refinement_consensus import _projection

MAX_WINDOW_CONTENT_CHARACTERS = 8


def _mappings(key, other, n):
    mapped = {}
    for tag, a, b, c, d in difflib.SequenceMatcher(None, key, other, autojunk=False).get_opcodes():
        if tag == "equal":
            # Equal spans can only veto a change; they never supply a new vote.
            for i, j in zip(range(a, b), range(c, d)):
                mapped[i] = {"position": j, "method": "equal-veto-only"}
            continue
        if (tag != "replace" or not 0 < b-a == d-c <= MAX_WINDOW_CONTENT_CHARACTERS
                or min(a, c) < n or min(len(key)-b, len(other)-d) < n):
            continue
        left, right = key[a-n:a], key[b:b+n]
        if (other[c-n:c] != left or other[d:d+n] != right
                or fidelity._pair_locations(key, left, right, b-a) != [a-n]
                or fidelity._pair_locations(other, left, right, d-c) != [c-n]):
            continue
        for i, j in zip(range(a, b), range(c, d)):
            mapped[i] = {"position": j, "method": "unique-equal-width-content-window",
                         "baseline_window": [a, b], "source_window": [c, d],
                         "left": left, "right": right}
    return mapped


def _related(left, right):
    return (fidelity.form_related(left, right) or bool(unihan.relationship(left, right))
            or unihan.shared_reading(left, right))


def _cross_form_change(key, projected, mapping, before, after):
    a, b = mapping["baseline_window"]
    c, d = mapping["source_window"]
    # Do not split a shifted variant into two apparently unrelated changes.
    return (any(_related(old, after) for old in key[a:b])
            or any(_related(before, new) for new in projected[c:d]))


def local_candidates(baseline, runs, snapshot, policy):
    from .comparison_refinement import _box, _current_span, _locate

    text = baseline["raw"]["text"]
    dictionary, official = fidelity.dictionary_manifest(), unihan.manifest()
    if (core._run_profile(baseline)[1] != "horizontal"
            or max(len(text), len(snapshot)) > policy.max_input_chars
            or not dictionary["available"] or not official["available"]):
        return []
    n = policy.anchor_chars
    key, indexes = _projection(text)
    sources, targets = {}, set()
    for engine, run in sorted(runs.items()):
        other = run["raw"]["text"]
        if (run["id"] == baseline["id"] or not core._compatible(baseline, run)
                or not core._run_complete(run) or len(other) > policy.max_input_chars):
            continue
        projected, positions = _projection(other)
        mapped = _mappings(key, projected, n)
        sources[engine] = (run, other, projected, positions, mapped)
        for i, mapping in mapped.items():
            after = projected[mapping["position"]]
            if (mapping["method"] != "equal-veto-only" and key[i] != after
                    and core._han(key[i]) and core._han(after)):
                targets.add((i, after))
    results = []
    baseline_family = fidelity.ENGINE_FAMILIES[baseline["engine"]]
    for i, after in sorted(targets):
        start, end = indexes[i], indexes[i]+1
        before = text[start:end]
        if (min(start, len(text)-end) < n or _related(before, after)
                or fidelity._protected_unicode_changed(text, start, end, after,
                                                       [("replace", 0, 1, 0, 1)])):
            continue
        anchors = {"left": text[start-n:start], "right": text[end:end+n]}
        if _locate(text, **anchors, maximum=1) != (start, end):
            continue
        try:
            current_start, current_end = _current_span(text, snapshot, start, end, anchors)
        except core.ComparisonError:
            continue
        if snapshot[current_start:current_end] != before:
            continue
        supporting, conflict = [], False
        for engine, (run, other, projected, positions, mapped) in sources.items():
            family = fidelity.ENGINE_FAMILIES[engine]
            if (family == baseline_family or i not in mapped
                    or not core._eligibility(run, after, before)[0]):
                continue
            mapping = mapped[i]
            j = mapping["position"]
            if projected[j] != after:
                conflict = True
                continue
            if mapping["method"] == "equal-veto-only":
                continue
            if _cross_form_change(key, projected, mapping, before, after):
                conflict = True
                continue
            a, b = mapping["baseline_window"]
            c, d = mapping["source_window"]
            ca, cb = positions[c-n], positions[d+n-1]+1
            s, e = positions[j], positions[j]+1
            if (start-indexes[a-n] > 64 or indexes[b+n-1]+1-end > 64
                    or s-ca > 64 or cb-e > 64):
                continue
            box = _box(run["raw"], ca, cb) if engine != "glmocr" else None
            if box is None:
                continue
            supporting.append({"engine": engine, "family": family, "run_id": run["id"],
                "raw_sha256": run["raw_sha256"], "start": s, "end": e,
                "context_start": ca, "context_end": cb,
                "context_sha256": core._text_sha(other[ca:cb]), "bbox": box,
                "granularity": run["raw"].get("engine_metadata", {}).get("bbox_granularity", "engine-region"),
                "address": mapping})
        families = sorted({row["family"] for row in supporting})
        if conflict or len(families) < 3:
            continue
        source = supporting[0]
        evidence = {k: source[k] for k in ("run_id", "engine", "raw_sha256", "start", "end",
                                          "context_start", "context_end", "bbox", "granularity")}
        item = {"kind": "han", "engine": source["engine"],
            "start": current_start, "end": current_end, "before": before, "after": after,
            "baseline_start": start, "baseline_end": end, "source_run_id": source["run_id"],
            "source_raw_sha256": source["raw_sha256"], "source_start": source["start"],
            "source_end": source["end"], "anchors": anchors, "evidence": evidence,
            "consensus_evidence": {"version": "literal-window-consensus-v1",
                "supporting": supporting, "families": families, "baseline_family": baseline_family,
                "minimum_independent_families": 3, "maximum_content_window": MAX_WINDOW_CONTENT_CHARACTERS,
                "variant_protection": "source-fidelity-v2-plus-unihan-v1", "normalization": False,
                "dictionary_manifest": dictionary, "unihan_manifest": official}}
        item["id"] = core._digest(item)
        results.append(item)
    return sorted(results, key=lambda p: (p["start"], p["end"], p["engine"], p["id"]))[:policy.max_candidates]
