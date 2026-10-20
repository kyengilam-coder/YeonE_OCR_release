"""Independent raw punctuation evidence for separately versioned local drafts.

No ground truth, AI response, fabricated image claim or source normalization is
used. Width shape is an address/evidence comparison only; the replacement is
still the exact original EasyOCR codepoint supplied by the v2 generator.
"""
from __future__ import annotations

import copy

from ab_ocr.engine_registry import ENGINES
from . import comparison as core
from . import comparison_fidelity_v3 as fidelity
from . import comparison_refinement_v2 as candidate_source

GUARD_CONTRACT = "independent-local-punctuation-v1"
POLICY = {
    "version": GUARD_CONTRACT,
    "baseline_family_excluded": True,
    "address": "unique-exact-content-flanks-and-zero-content-gap",
    "punctuation_count_and_order": "whole-addressed-gap",
    "width": "one-exact-after-family-two-glyph-families-no-glyph-conflict",
    "width_before_majority": "hold-if-before-families-exceed-after-families",
    "kind": "two-exact-after-families-no-addressed-glyph-or-count-conflict",
    "family_overlap": "one-family-may-appear-on-both-sides-when-its-models-disagree",
    "replacement_and_whitespace": "original-v2-one-P-codepoint-patch-only",
    "image_evidence": "actual-EasyOCR-word-or-line-box; not-Unicode-width-proof",
}


def _views(baseline, runs, policy):
    output = []
    baseline_family = fidelity.ENGINE_FAMILIES[baseline["engine"]]
    for engine in ENGINES:
        run = runs.get(engine)
        if run is None:
            continue
        raw = run["raw"]
        row = {"engine": engine, "family": fidelity.ENGINE_FAMILIES[engine],
            "run_id": run["id"], "raw_sha256": run["raw_sha256"]}
        reason = None
        if row["family"] == baseline_family:
            reason = "baseline-family"
        elif run.get("engine") != engine or core._digest(raw) != run["raw_sha256"]:
            reason = "raw-identity-mismatch"
        elif not run.get("active", True):
            reason = "inactive-reading"
        elif not core._run_complete(run) or raw.get("engine_metadata", {}).get("generation_complete") is False:
            reason = "incomplete-reading"
        elif not core._compatible(baseline, run):
            reason = "incompatible-reading"
        elif len(raw["text"]) > policy.max_input_chars:
            reason = "reading-exceeds-bound"
        row.update(usable=reason is None, exclusion=reason)
        projected = candidate_source._content_projection(raw["text"]) if reason is None else None
        output.append((run, row, projected))
    return output


def _evidence(baseline, item, views):
    address = item["address_evidence"]
    text = baseline["raw"]["text"]
    punctuation_offsets = address["baseline_punctuation_offsets"]
    ordinal, count = address["punctuation_ordinal"], address["punctuation_count"]
    before_gap = "".join(text[index] for index in punctuation_offsets)
    after_gap = before_gap[:ordinal] + item["after"] + before_gap[ordinal + 1:]
    expected_shape = fidelity._punctuation_shape(after_gap)
    before_families, after_families, glyph_families, conflict_families = set(), set(), set(), set()
    observations = []
    for run, summary, projected in views:
        row = copy.deepcopy(summary)
        row.update(addressable=False, target_literal=None, exact_before=False,
                   exact_after=False, glyph_agreement=False, glyph_conflict=False)
        if not row["usable"]:
            observations.append(row)
            continue
        eligible, note = core._eligibility(run, item["after"], item["before"])
        if not eligible:
            row.update(usable=False, exclusion="unsupported-language", support_note=note)
            observations.append(row)
            continue
        spine, indices = projected
        gap = candidate_source._unique_gap(spine, address["content_left"], address["content_right"])
        if gap is None:
            row["address_reason"] = "nonunique-or-missing-content-flanks"
            observations.append(row)
            continue
        start, end = indices[gap - 1] + 1, indices[gap]
        raw = run["raw"]["text"]
        offsets = [i for i in range(start, end) if candidate_source._punctuation(raw[i])]
        observed = "".join(raw[i] for i in offsets)
        count_equal = len(offsets) == count
        glyph_agreement = count_equal and fidelity._punctuation_shape(observed) == expected_shape
        row.update(addressable=True, gap_start=start, gap_end=end, gap_text=raw[start:end],
                   gap_sha256=core._text_sha(raw[start:end]), punctuation_offsets=offsets,
                   punctuation_literal=observed, punctuation_count=len(offsets),
                   expected_count_matches=count_equal, glyph_agreement=glyph_agreement,
                   glyph_conflict=not glyph_agreement)
        if count_equal:
            target_start = offsets[ordinal]
            literal = raw[target_start]
            row.update(target_start=target_start, target_end=target_start + 1, target_literal=literal,
                       exact_before=literal == item["before"], exact_after=literal == item["after"])
            if row["exact_before"]:
                before_families.add(row["family"])
            if row["exact_after"] and glyph_agreement:
                after_families.add(row["family"])
        if glyph_agreement:
            glyph_families.add(row["family"])
        else:
            conflict_families.add(row["family"])
        observations.append(row)
    width_only = fidelity._width_only(item["before"], item["after"])
    reasons = []
    if not any(row["run_id"] == item["source_run_id"] and row["usable"] and row["exact_after"]
               and row["glyph_agreement"] for row in observations):
        reasons.append("candidate-source-not-usable-exact-after")
    if conflict_families:
        reasons.append("addressed-punctuation-glyph-or-count-conflict")
    if width_only:
        if not after_families:
            reasons.append("insufficient-exact-after-support")
        if len(glyph_families) < 2:
            reasons.append("insufficient-independent-glyph-support")
        if len(before_families) > len(after_families):
            reasons.append("independent-before-width-majority")
    elif len(after_families) < 2:
        reasons.append("insufficient-independent-exact-after-support")
    return {"contract": GUARD_CONTRACT, "policy": copy.deepcopy(POLICY),
        "policy_sha256": core._digest(POLICY), "parent_proposal_id": item["id"],
        "baseline_run_id": baseline["id"], "baseline_raw_sha256": baseline["raw_sha256"],
        "baseline_family": fidelity.ENGINE_FAMILIES[baseline["engine"]],
        "content_left": address["content_left"], "content_right": address["content_right"],
        "punctuation_ordinal": ordinal, "expected_punctuation_count": count,
        "baseline_punctuation_literal": before_gap, "expected_punctuation_literal": after_gap,
        "width_only": width_only, "before_families": sorted(before_families),
        "after_families": sorted(after_families), "glyph_families": sorted(glyph_families),
        "conflict_families": sorted(conflict_families), "observations": observations,
        "blocked": bool(reasons), "reason_codes": reasons,
        "geometry": copy.deepcopy(item["evidence"]),
        "image_or_AI_proof_created": False, "source_normalization": False}


def assessed_candidates(baseline, runs, snapshot, policy):
    """Retain both held and allowed v2 hypotheses with all raw run evidence."""
    originals = candidate_source.local_candidates(baseline, runs, snapshot, policy)
    if not originals:
        return []
    views = _views(baseline, runs, policy)
    output = []
    for original in originals:
        item = copy.deepcopy(original)
        item["guard_evidence"] = _evidence(baseline, original, views)
        item["id"] = core._digest({key: value for key, value in item.items() if key != "id"})
        output.append(item)
    return output


def local_candidates(baseline, runs, snapshot, policy):
    """Only admitted hypotheses; callers may persist assessed_candidates too."""
    return [item for item in assessed_candidates(baseline, runs, snapshot, policy)
            if not item["guard_evidence"]["blocked"]]
