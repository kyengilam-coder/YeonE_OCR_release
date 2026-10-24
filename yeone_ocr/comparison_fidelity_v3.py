"""Versioned immutable OCR evidence; current policy starts from frozen v2.

Historical v1/v2 requests and acceptance proofs are replayed by their frozen
implementations. New punctuation rules may add evidence without changing the
v2 Han/source-variant decisions or rewriting any original proposal.
"""
from __future__ import annotations

import difflib
import unicodedata

from . import comparison_fidelity_v1 as legacy_v1
from . import comparison_fidelity_v2 as legacy_v2

SOURCE_FIDELITY_CONTRACT = "source-fidelity-v3"
SUPPORTED_SOURCE_FIDELITY_CONTRACTS = (legacy_v1.SOURCE_FIDELITY_CONTRACT,
                                      legacy_v2.SOURCE_FIDELITY_CONTRACT,
                                      SOURCE_FIDELITY_CONTRACT)
SUPPORTED_CONTRACTS = SUPPORTED_SOURCE_FIDELITY_CONTRACTS
EVIDENCE_VERSION = legacy_v2.EVIDENCE_VERSION
FidelityError = legacy_v2.FidelityError
ANCHOR_CHARS = legacy_v2.ANCHOR_CHARS
MAX_RUN_CHARACTERS = legacy_v2.MAX_RUN_CHARACTERS
MAX_HUMAN_ALIGNMENT_CELLS = legacy_v2.MAX_HUMAN_ALIGNMENT_CELLS
MAX_ADDRESS_FLANK_CODEPOINTS = legacy_v2.MAX_ADDRESS_FLANK_CODEPOINTS
ENGINE_FAMILIES = legacy_v2.ENGINE_FAMILIES
POLICY = {**legacy_v2.POLICY, "version": SOURCE_FIDELITY_CONTRACT,
          "han_policy": "frozen-source-fidelity-v2-exact",
          "punctuation_width_shape": "P-only-direct-wide-narrow-one-to-one",
          "width_relaxation": "one-exact-target-two-other-glyph-families-image-bound-no-glyph-conflict",
          "nonwidth_punctuation": "two-exact-target-families-no-addressable-glyph-gap-conflict",
          "mixed_punctuation": "unique-single-minimal-separator-splice-only",
          "editorial_punctuation_basis": "independent-final-evidence-soft-punctuation-holds-only"}
REASONS = {**legacy_v2.REASONS,
           "punctuation_glyph_conflict": "같은 위치의 다른 OCR이 서로 다른 부호나 반복 수를 읽어 보류합니다.",
           "punctuation_image_provenance_missing": "폭 표기 완화에 필요한 원본 이미지 연결이 없어 보류합니다.",
           "ambiguous_punctuation_splice": "최소 부호 교정 위치가 여러 곳이어서 보류합니다."}
_digest = legacy_v2._digest
_text_sha = legacy_v2._text_sha
_options = legacy_v2._options
_han = legacy_v2._han
_selector = legacy_v2._selector
_separator = legacy_v2._separator
_projection = legacy_v2._projection
_pair_locations = legacy_v2._pair_locations
_protected_layout = legacy_v2._protected_layout
_protected_unicode_changed = legacy_v2._protected_unicode_changed
_overlaps = legacy_v2._overlaps
_form_family = legacy_v2._form_family
form_related = legacy_v2.form_related
dictionary_manifest = legacy_v2.dictionary_manifest
punctuation_change_kind = legacy_v2.punctuation_change_kind
_punctuation_witnesses = legacy_v2._punctuation_witnesses
_ocr_support = legacy_v2._ocr_support
_semantic_support = legacy_v2._semantic_support
_exact_observations = legacy_v2._exact_observations


def source_fidelity_reason(reason_codes):
    return " ".join(REASONS.get(code, "원본 보존 근거를 확인해야 하는 제안입니다.") for code in reason_codes)


def _punctuation_shape(text):
    """A comparison key for direct Unicode width forms of punctuation only."""
    output = []
    for char in text:
        parts = unicodedata.decomposition(char).split()
        if (unicodedata.category(char).startswith("P") and len(parts) == 2
                and parts[0] in ("<wide>", "<narrow>")):
            counterpart = chr(int(parts[1], 16))
            if unicodedata.category(counterpart).startswith("P"):
                output.append(counterpart)
                continue
        output.append(char)
    return "".join(output)


def _width_only(before, after):
    return (before != after and len(before) == len(after) and bool(before)
            and all(unicodedata.category(char).startswith("P") for char in before + after)
            and _punctuation_shape(before) == _punctuation_shape(after))


def _punctuation_target(proposal):
    """Derive a unique minimal target without modifying its parent proposal."""
    before, after = proposal["before"], proposal["after"]
    if all(_separator(char) for char in before + after):
        return dict(proposal), {"derivation": "original-separator-target"}, ""
    if before == after:
        return None, None, "mixed_punctuation_patch"
    prefix = 0
    while prefix < min(len(before), len(after)) and before[prefix] == after[prefix]:
        prefix += 1
    suffix = 0
    while suffix < min(len(before), len(after)) and before[-1 - suffix] == after[-1 - suffix]:
        suffix += 1
    nonoverlap_suffix = min(suffix, len(before) - prefix, len(after) - prefix)
    old_width = len(before) - prefix - nonoverlap_suffix
    new_width = len(after) - prefix - nonoverlap_suffix
    low = max(0, len(before) - old_width - suffix)
    high = min(prefix, len(before) - old_width, len(after) - new_width)
    candidates = []
    for index in range(low, high + 1):
        old, new = before[index:index + old_width], after[index:index + new_width]
        if (old != new and all(_separator(char) for char in old + new)
                and before[:index] == after[:index]
                and before[index + old_width:] == after[index + new_width:]):
            candidates.append((index, old, new))
    if len(candidates) != 1:
        return None, None, "ambiguous_punctuation_splice" if candidates else "mixed_punctuation_patch"
    index, old, new = candidates[0]
    target = {**proposal, "start": proposal["start"] + index,
              "end": proposal["start"] + index + len(old), "before": old, "after": new}
    return target, {"derivation": "unique-single-minimal-separator-splice-v1",
                    "parent_proposal_sha256": _digest({k: v for k, v in proposal.items() if k != "id"}),
                    "start": target["start"], "end": target["end"],
                    "before_sha256": _text_sha(old), "after_sha256": _text_sha(new)}, ""


def _image_request_provenance(batch, target):
    """Attest image-bound inputs, never claim that a model saw the right pixels."""
    images = {image.get("id"): image for image in batch.get("images", []) if isinstance(image, dict)}
    output = []
    for candidate in batch.get("targets", []):
        if not isinstance(candidate, dict):
            continue
        a, b = candidate.get("start"), candidate.get("end")
        image = images.get(candidate.get("image_id"))
        if (type(a) is not int or type(b) is not int or not a <= target["start"] <= target["end"] <= b
                or image is None or not isinstance(image.get("sha256"), str) or len(image["sha256"]) != 64
                or not isinstance(image.get("source_bbox"), list) or len(image["source_bbox"]) != 4):
            continue
        output.append({"target_id": candidate.get("id"), "target_start": a, "target_end": b,
                       "image_id": image["id"], "image_sha256": image["sha256"],
                       "source_bbox": list(image["source_bbox"])})
    return output


def _punctuation_evidence(batch, target, evidence):
    result = legacy_v2._punctuation_witnesses(batch, target, evidence)
    result.update(width_only=_width_only(target["before"], target["after"]),
                  glyph_after_families=[], glyph_conflict_families=[], glyph_conflicts=[],
                  image_request_provenance=_image_request_provenance(batch, target), route=None)
    if not result["address_valid"]:
        return result, "ambiguous_punctuation_address"
    start, end = target["start"], target["end"]
    a, b = result["source_gap"]
    prefix, suffix = batch["snapshot_text"][a:start], batch["snapshot_text"][end:b]
    expected = prefix + target["after"] + suffix
    omit_spaces = result["comparison_mode"] == "outside-target-whitespace-address-only"

    def gap_key(text):
        return _punctuation_shape("".join(char for char in text if not (omit_spaces and char.isspace())))

    expected_key = gap_key(expected)
    runs = {run["run_id"]: run for run in evidence["runs"]}
    column = sum(not char.isspace() for char in prefix)
    for row in result["runs"]:
        text = runs[row["run_id"]]["text"]
        gap = text[row["gap_start"]:row["gap_end"]]
        agrees = gap_key(gap) == expected_key
        row.update(glyph_gap_sha256=_text_sha(gap_key(gap)), glyph_conflict=not agrees,
                   target_shape_contiguous=False, glyph_agreement=False)
        if agrees:
            if omit_spaces:
                indices = [i for i, char in enumerate(gap) if not char.isspace()]
                width = len(target["after"])
                if not width:
                    row["target_shape_contiguous"] = True
                elif column + width <= len(indices):
                    local = gap[indices[column]:indices[column + width - 1] + 1]
                    row["target_shape_contiguous"] = (len(local) == width
                        and _punctuation_shape(local) == _punctuation_shape(target["after"]))
            else:
                row["target_shape_contiguous"] = True
            row["glyph_agreement"] = row["target_shape_contiguous"]
    result["glyph_after_families"] = sorted({row["family"] for row in result["runs"] if row["glyph_agreement"]})
    result["glyph_conflicts"] = [dict(row) for row in result["runs"] if row["glyph_conflict"]]
    result["glyph_conflict_families"] = sorted({row["family"] for row in result["glyph_conflicts"]})
    result["expected_glyph_gap_sha256"] = _text_sha(expected_key)
    if result["glyph_conflicts"]:
        return result, "punctuation_glyph_conflict"
    if len(result["after_families"]) >= 2:
        result["route"] = "two-exact-target-families"
        return result, ""
    if (result["width_only"] and result["after_families"] and len(result["glyph_after_families"]) >= 2):
        if not result["image_request_provenance"]:
            return result, "punctuation_image_provenance_missing"
        result["route"] = "one-exact-target-two-glyph-families-image-bound"
        return result, ""
    return result, "insufficient_punctuation_ocr_support"


def _independent_punctuation_basis(batch, proposal, visual, policy, final_evidence):
    soft = {"insufficient_punctuation_ocr_support", "ambiguous_punctuation_address", "mixed_punctuation_patch"}
    proofs = []
    for basis in visual:
        a, b = basis["start"], basis["end"]
        if not proposal["start"] <= a <= b <= proposal["end"]:
            continue
        relative_a, relative_b = a - proposal["start"], b - proposal["start"]
        if (len(proposal["before"]) == len(proposal["after"])
                and proposal["after"][relative_a:relative_b] == proposal["before"][relative_a:relative_b]):
            continue
        verdict = legacy_v2.assess_source_fidelity({**_as_v2(batch), "phase": "visual"}, [basis],
            policy=policy, contract=legacy_v2.SOURCE_FIDELITY_CONTRACT)["proposals"][0]
        if not verdict["blocked"]:
            continue
        derived, _, error = _punctuation_target(basis)
        if error or derived is None or not set(verdict["reason_codes"]) <= soft:
            return None
        proofs.append({"proposal_id": basis["id"], "proposal_sha256": verdict["proposal_sha256"],
                       "previous_reason_codes": list(verdict["reason_codes"]),
                       "independent_final_evidence_sha256": _digest(final_evidence)})
    return proofs or None


def _contract(batch):
    contract = batch.get("source_fidelity_contract")
    if contract not in (None, *SUPPORTED_SOURCE_FIDELITY_CONTRACTS):
        raise FidelityError("지원하지 않는 원본 보존 검증 계약입니다.")
    return contract


def _as_v2(batch):
    return {**batch, "source_fidelity_contract": legacy_v2.SOURCE_FIDELITY_CONTRACT}


def build_source_fidelity_evidence(batch, runs, *, policy=None):
    _contract(batch)
    return legacy_v2.build_source_fidelity_evidence(_as_v2(batch), runs, policy=policy)


def validate_source_fidelity_evidence(batch, runs, *, policy=None):
    contract = _contract(batch)
    if contract == SOURCE_FIDELITY_CONTRACT:
        return legacy_v2.validate_source_fidelity_evidence(_as_v2(batch), runs, policy=policy)
    return legacy_v2.validate_source_fidelity_evidence(batch, runs, policy=policy)


def _authenticated_evidence(batch, runs, policy):
    contract = _contract(batch)
    return legacy_v2._authenticated_evidence(_as_v2(batch) if contract == SOURCE_FIDELITY_CONTRACT else batch,
                                            runs, policy)


def _frozen_evidence(batch, runs, policy):
    return _authenticated_evidence(batch, runs, policy)


def assess_source_fidelity(batch, proposals, visual=(), *, runs=None, policy=None, contract=None):
    selected = SOURCE_FIDELITY_CONTRACT if contract is None else contract
    if selected not in SUPPORTED_SOURCE_FIDELITY_CONTRACTS:
        raise FidelityError("지원하지 않는 원본 보존 판정 버전입니다.")
    evidence = _authenticated_evidence(batch, runs, policy)
    frozen = {**batch, "source_fidelity_contract": selected,
              "source_fidelity_options": evidence["options"],
              "source_fidelity_evidence": evidence,
              "source_fidelity_evidence_sha256": _digest(evidence)}
    if selected in (legacy_v1.SOURCE_FIDELITY_CONTRACT, legacy_v2.SOURCE_FIDELITY_CONTRACT):
        return legacy_v2.assess_source_fidelity(frozen, proposals, visual, policy=policy, contract=selected)
    return _assess_current(frozen, proposals, visual, policy=policy)


def _assess_current(batch, proposals, visual=(), *, runs=None, policy=None):
    proposals = list(proposals)
    result = legacy_v2.assess_source_fidelity(_as_v2(batch), proposals, visual,
        runs=runs, policy=policy, contract=legacy_v2.SOURCE_FIDELITY_CONTRACT)
    evidence = _authenticated_evidence(batch, runs, policy)
    for proposal, verdict in zip(proposals, result["proposals"]):
        before, after = proposal["before"], proposal["after"]
        changes = [row for row in difflib.SequenceMatcher(None, before, after, autojunk=False).get_opcodes()
                   if row[0] != "equal"]
        changed = "".join(before[a:b] + after[c:d] for _, a, b, c, d in changes)
        if not changed or not all(_separator(char) for char in changed):
            continue  # All actual Han/other content changes remain exactly frozen-v2 decisions.
        target, derivation, error = _punctuation_target(proposal)
        if error:
            verdict["reason_codes"] = list(dict.fromkeys(verdict["reason_codes"] + [error]))
            verdict["blocked"] = True
            continue
        punctuation, error = _punctuation_evidence(batch, target, evidence)
        reasons = [code for code in verdict["reason_codes"] if code not in
                   {"mixed_punctuation_patch", "ambiguous_punctuation_address", "insufficient_punctuation_ocr_support"}]
        if (derivation["derivation"] != "original-separator-target" and batch["phase"] == "editorial"
                and proposal["kind"] == "punctuation" and evidence["options"]["punctuation"]):
            reasons = [code for code in reasons if code != "editorial_policy_violation"]
        if error:
            reasons.append(error)
        independently_verified = []
        if not error and "editorial_carries_blocked_visual" in reasons:
            proofs = _independent_punctuation_basis(batch, proposal, visual, policy, punctuation)
            if proofs is not None:
                reasons.remove("editorial_carries_blocked_visual")
                independently_verified = proofs
        verdict.update(reason_codes=list(dict.fromkeys(reasons)), blocked=bool(reasons),
                       punctuation_evidence=punctuation, derived_punctuation_target=derivation,
                       independently_verified_basis=independently_verified)
    result["contract"] = SOURCE_FIDELITY_CONTRACT
    result["policy_sha256"] = _digest(POLICY)
    return result


def verify_source_fidelity(review, previous_visual=(), *, runs=None):
    contract = _contract(review["request"])
    prompt = review.get("metadata", {}).get("prompt_version")
    if ((prompt == "comparison-ai-v10" and contract != SOURCE_FIDELITY_CONTRACT)
            or (contract == SOURCE_FIDELITY_CONTRACT and prompt != "comparison-ai-v10")):
        raise FidelityError("AI 요청 버전과 원본 보존 검증 계약이 일치하지 않습니다.")
    if contract != SOURCE_FIDELITY_CONTRACT:
        return legacy_v2.verify_source_fidelity(review, previous_visual, runs=runs)
    if runs is not None:
        validate_source_fidelity_evidence(review["request"], runs, policy=review["policy"])
    if review["status"] != "completed":
        return None
    expected = assess_source_fidelity(review["request"], review["proposals"], previous_visual,
        runs=runs, policy=review["policy"], contract=SOURCE_FIDELITY_CONTRACT)
    if review.get("metadata", {}).get("source_fidelity") != expected:
        raise FidelityError("AI 원본 보존 판정이 보존된 제안·독립 OCR 근거와 다릅니다.")
    return expected
