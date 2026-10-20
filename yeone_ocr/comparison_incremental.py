"""Pure additional review policy for newly withheld punctuation-width proposals.

This does not recognize pixels, normalize output, edit proposals or accept a
human correction. The caller authenticates raw readings, unique addresses and
the existing proposal guards before supplying literal observations.
"""
from __future__ import annotations

import hashlib
import json
import unicodedata

CONTRACT = "incremental-exact-width-majority-v1"
ENGINE_FAMILIES = {"tesseract": "tesseract", "rapidocr": "paddle", "paddle_server": "paddle",
                   "easyocr": "easyocr", "glmocr": "glmocr", "surya": "surya"}
POLICY = {
    "contract": CONTRACT,
    "scope": "newly-withheld-width-only-proposals",
    "minimum_independent_exact_after_families": 2,
    "majority": "after > other_before + one_conservative_baseline_vote",
    "baseline": "countervote-only-never-original-encoding-proof",
    "literal": "exact-codepoints-including-target-whitespace",
    "third_split_ambiguous_incomplete": "hold",
    "missing_address": "abstain-never-support",
    "whitespace_only": "hold",
    "image_shape_proves_encoding": False,
    "existing_proposal_guards": "required",
    "historical_selection": "caller-must-preserve-exact-objects-and-order",
    "human_accepted": False,
}


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":"), allow_nan=False).encode("utf8")).hexdigest()


def _width(char):
    parts = unicodedata.decomposition(char).split()
    if (unicodedata.category(char).startswith("P") and len(parts) == 2
            and parts[0] in ("<wide>", "<narrow>")):
        other = chr(int(parts[1], 16))
        if unicodedata.category(other).startswith("P"):
            return other
    return char


def assess_family_consistency(observations):
    """Hold-only proof when one family has distinct exact addressed literals.

    There is no preferred glyph, normalization or minimum positive vote count.
    A passing result cannot authorize a proposal or replace source guards.
    """
    observations = list(observations)
    families, seen = {}, set()
    for row in observations:
        engine, run_id, status = row.get("engine"), row.get("run_id"), row.get("status")
        if (engine not in ENGINE_FAMILIES or not isinstance(run_id, str) or not run_id
                or run_id in seen or status not in ("unique", "missing", "ambiguous", "incomplete")
                or type(row.get("generation_complete")) is not bool):
            raise ValueError("Malformed or duplicate raw observation")
        seen.add(run_id)
        if status != "unique" or not row["generation_complete"]:
            continue
        if not isinstance(row.get("literal"), str):
            raise TypeError("A unique observation must contain the exact literal")
        families.setdefault(ENGINE_FAMILIES[engine], {}).setdefault(row["literal"], []).append(run_id)
    conflicts = {family: [{"literal": literal, "run_ids": ids} for literal, ids in sorted(values.items())]
                 for family, values in sorted(families.items()) if len(values) > 1}
    return {"contract": "incremental-exact-family-consistency-v1", "blocked": bool(conflicts),
            "action": "hold_review_only" if conflicts else "no_additional_hold",
            "conflicts": conflicts, "observations_sha256": digest(observations),
            "source_normalization": False, "new_approval": False, "human_accepted": False}


def assess_width_recall(before, after, observations, *, baseline_engine,
                        source_address_valid, existing_guards_passed):
    """Assess one minimal separator target without trusting precomputed votes.

    Every observation is a distinct engine run with ``engine``, ``run_id``,
    ``status`` (unique/missing/ambiguous/incomplete), ``generation_complete``,
    and a literal for a unique addressed target. The baseline family is
    excluded from support and gets one conservative countervote. Missing
    flanks abstain; ambiguous flanks veto. The caller proves addresses and raw
    hashes, including the exact whitespace inside the proposed target.
    """
    if not isinstance(before, str) or not isinstance(after, str) or baseline_engine not in ENGINE_FAMILIES:
        raise ValueError("Exact target strings and a known baseline engine are required")
    if type(source_address_valid) is not bool or type(existing_guards_passed) is not bool:
        raise ValueError("Address and existing-guard results must be explicit booleans")
    observations = list(observations)
    initial = digest(observations)
    consistency = assess_family_consistency(observations)
    reasons = []
    if not existing_guards_passed:
        reasons.append("existing_guard_not_passed")
    if not source_address_valid:
        reasons.append("source_address_not_unique")
    if before == after or any(not (c.isspace() or unicodedata.category(c).startswith("P")) for c in before + after):
        reasons.append("not_minimal_separator_width_change")
    old = "".join(c for c in before if not c.isspace())
    new = "".join(c for c in after if not c.isspace())
    if old == new:
        reasons.append("whitespace_only_remains_held")
    elif "".join(map(_width, old)) != "".join(map(_width, new)):
        reasons.append("not_width_equivalent")
    states, seen, before_families, after_families, third_families = {}, set(), set(), set(), set()
    baseline_family = ENGINE_FAMILIES[baseline_engine]
    missing, ambiguous, incomplete = [], [], []
    for row in observations:
        engine, run_id = row.get("engine"), row.get("run_id")
        status = row.get("status")
        if (engine not in ENGINE_FAMILIES or not isinstance(run_id, str) or not run_id
                or run_id in seen or status not in ("unique", "missing", "ambiguous", "incomplete")
                or type(row.get("generation_complete")) is not bool):
            raise ValueError("Malformed or duplicate raw observation")
        seen.add(run_id)
        family = ENGINE_FAMILIES[engine]
        if row["generation_complete"] is not True or status == "incomplete":
            incomplete.append(run_id)
        if status == "ambiguous":
            ambiguous.append(run_id)
        if status == "missing":
            missing.append(run_id)
        if family == baseline_family or status != "unique" or not row["generation_complete"]:
            continue
        literal = row.get("literal")
        if not isinstance(literal, str):
            raise TypeError("A unique observation must contain the exact literal")
        stance = "after" if literal == after else "before" if literal == before else "third"
        states.setdefault(family, set()).add(stance)
        {"after": after_families, "before": before_families, "third": third_families}[stance].add(family)
    split = sorted(consistency["conflicts"])
    clean_after = sorted(after_families - before_families - third_families)
    if incomplete:
        reasons.append("incomplete_reading")
    if ambiguous:
        reasons.append("ambiguous_reading_address")
    if third_families:
        reasons.append("third_literal_at_same_address")
    if split:
        reasons.append("within_family_disagreement")
    if len(clean_after) < POLICY["minimum_independent_exact_after_families"]:
        reasons.append("insufficient_independent_exact_after")
    if len(clean_after) <= len(before_families) + 1:
        reasons.append("no_strict_majority_over_baseline_and_before")
    if digest(observations) != initial:
        raise ValueError("Pure policy mutated an observation")
    return {"contract": CONTRACT, "policy_sha256": digest(POLICY),
            "action": "retain_unconfirmed" if not reasons else "hold_review_only",
            "reason_codes": reasons, "before": before, "after": after,
            "after_families": sorted(after_families), "before_families": sorted(before_families),
            "clean_after_families": clean_after, "third_families": sorted(third_families),
            "split_families": split, "missing_run_ids": missing, "ambiguous_run_ids": ambiguous,
            "incomplete_run_ids": incomplete, "baseline_conservative_countervotes": 1,
            "baseline_original_encoding_proved": False, "image_encoding_proved": False,
            "observations_sha256": initial, "human_accepted": False,
            "raw_or_proposals_modified": False, "family_consistency": consistency}
