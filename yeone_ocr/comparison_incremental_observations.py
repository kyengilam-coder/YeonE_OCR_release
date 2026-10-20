"""Exact raw observation extraction for optional incremental proposal review.

No files, AI, native PDF text or references are inputs. Separator projection
locates addresses only; candidate literals and target whitespace stay exact.
"""
from __future__ import annotations

from bisect import bisect_left

from . import comparison as core
from . import comparison_fidelity_v1 as identity
from . import comparison_fidelity_v2 as address
from . import comparison_fidelity_v3 as derive
from .comparison_incremental import assess_family_consistency, digest


def minimal_patch(item):
    before, after = item["before"], item["after"]
    left = 0
    while left < min(len(before), len(after)) and before[left] == after[left]:
        left += 1
    right = 0
    while right < min(len(before), len(after)) - left and before[-1-right] == after[-1-right]:
        right += 1
    return {"start": item["start"] + left, "end": item["end"] - right,
            "before": before[left:len(before)-right if right else None],
            "after": after[left:len(after)-right if right else None]}


def frozen_raw(page, snapshot):
    runs = {r["id"]: r for r in page["runs"]}
    if len(runs) != len(page["runs"]):
        raise ValueError("Duplicate raw engine run identity")
    baseline = runs[page["baseline_run_id"]]
    if snapshot != baseline["raw"]["text"]:
        raise ValueError("Fresh snapshot must remain the actual baseline raw text")
    batch = {"snapshot_text": snapshot, "snapshot_sha256": core._text_sha(snapshot),
             "baseline_run_id": baseline["id"], "runs": [{"run_id": r["id"], "engine": r["engine"],
                 "raw_sha256": r["raw_sha256"]} for r in runs.values()]}
    evidence = identity.build_source_fidelity_evidence(batch, runs)
    for row in evidence["runs"]:
        run = runs[row["run_id"]]
        row["eligible"] &= run.get("active", True)
        row["generation_complete"] &= run["raw"].get("engine_metadata", {}).get("generation_complete") is not False
        row["eligible"] &= row["generation_complete"]
    return batch, evidence, runs, baseline


def observe_punctuation(batch, original, evidence):
    target, derivation, error = derive._punctuation_target(original)
    if target is None:
        return {"target": None, "address_valid": False, "error": error, "observations": []}
    witness = address._punctuation_witnesses(batch, target, evidence)
    found = {r["run_id"]: r for r in witness["runs"]}
    snapshot = batch["snapshot_text"]
    key, indices = address._projection(snapshot, {})
    column = bisect_left(indices, target["start"])
    left, right = key[max(0, column-4):column], key[column:column+4]
    observations = []
    for run in evidence["runs"]:
        row = {"run_id": run["run_id"], "engine": run["engine"], "raw_sha256": run["raw_sha256"],
               "text_sha256": run["text_sha256"], "generation_complete": run["generation_complete"],
               "status": "missing", "literal": None}
        if not run["generation_complete"]:
            row["status"] = "incomplete"
        elif run["run_id"] == evidence["baseline_run_id"]:
            row.update(status="unique", literal=target["before"], baseline_countervote_only=True,
                       start=target["start"], end=target["end"])
        elif run["eligible"] and witness["address_valid"]:
            candidate_key, _ = address._projection(run["text"], {})
            locations = address._pair_locations(candidate_key, left, right, 0)
            row["anchor_matches"] = len(locations)
            if len(locations) > 1:
                row["status"] = "ambiguous"
            elif run["run_id"] in found:
                hit = found[run["run_id"]]
                a, b = hit["target_start"], hit["target_end"]
                if a is None:
                    a, b = hit["gap_start"], hit["gap_end"]
                literal = run["text"][a:b]
                row.update(status="unique", literal=literal, start=a, end=b, gap_start=hit["gap_start"],
                           gap_end=hit["gap_end"], gap_sha256=hit["gap_sha256"], inherited_stance=hit["stance"])
                if hit["stance"] == "neither" and literal in (target["before"], target["after"]):
                    row.update(status="ambiguous", reason="target-inside-incompatible-gap")
            elif len(locations) == 1:
                row.update(status="ambiguous", reason="address-flank-or-gap-exceeds-bound")
        observations.append(row)
    return {"target": {k: target[k] for k in ("start", "end", "before", "after")},
            "derivation": derivation, "address_valid": witness["address_valid"],
            "comparison_mode": witness.get("comparison_mode"), "observations": observations,
            "recomputed_full_witness_sha256": digest(witness), "literal_normalized": False}


def _han_locations(snapshot, target, text):
    """Address all families; baseline-family exclusion belongs only to voting."""
    start, end, width = target["start"], target["end"], len(target["before"])
    candidates, ambiguous = set(), False
    if width != len(target["after"]) or not width:
        return candidates, ambiguous
    left, right = snapshot[max(0, start-4):start], snapshot[end:end+4]
    if (len(left) == len(right) == 4
            and address._pair_locations(snapshot, left, right, width) == [start-4]):
        hits = address._pair_locations(text, left, right, width)
        ambiguous |= len(hits) > 1
        if len(hits) == 1:
            candidates.add((hits[0]+4, hits[0]+4+width))
    key, indices = address._projection(snapshot, {})
    a, b = bisect_left(indices, start), bisect_left(indices, end)
    if (a >= 4 and len(key)-b >= 4 and b-a == width
            and start-indices[a-4] <= 64 and indices[b+3]-end <= 64):
        left, right = key[a-4:a], key[b:b+4]
        if address._pair_locations(key, left, right, width) == [a-4]:
            other_key, other_indices = address._projection(text, {})
            hits = address._pair_locations(other_key, left, right, width)
            ambiguous |= len(hits) > 1
            if len(hits) == 1:
                location, middle = hits[0], hits[0]+4
                c, d = other_indices[middle], other_indices[middle+width-1]+1
                if c-other_indices[location] <= 64 and other_indices[middle+width+3]-d <= 64:
                    candidates.add((c, d))
                else:
                    ambiguous = True
    return candidates, ambiguous


def observe_han(batch, original, evidence):
    target = {**original, **minimal_patch(original)}
    observations = []
    for run in evidence["runs"]:
        row = {"engine": run["engine"], "run_id": run["run_id"], "raw_sha256": run["raw_sha256"],
               "generation_complete": run["generation_complete"], "status": "missing", "literal": None}
        if not run["generation_complete"]:
            row["status"] = "incomplete"
        elif run["run_id"] == evidence["baseline_run_id"]:
            row.update(status="unique", literal=target["before"], baseline_countervote_only=True,
                       start=target["start"], end=target["end"])
        elif run["eligible"]:
            locations, ambiguous = _han_locations(batch["snapshot_text"], target, run["text"])
            if ambiguous or len(locations) > 1:
                row["status"] = "ambiguous"
            elif len(locations) == 1:
                start, end = next(iter(locations))
                row.update(status="unique", literal=run["text"][start:end], start=start, end=end,
                           address_method="unique-exact-or-content-four-flanks-all-families")
        observations.append(row)
    return {"target": {k: target[k] for k in ("start", "end", "before", "after")},
            "observations": observations, "family_consistency": assess_family_consistency(observations),
            "new_Han_admission_created": False}
