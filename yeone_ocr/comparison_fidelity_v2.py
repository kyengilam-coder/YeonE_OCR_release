"""Versioned source-fidelity proofs and conservative positional OCR support.

The frozen v1 implementation validates historical proofs. New assessments keep
all source literals intact and may ignore separators only while finding an
unambiguous address, never while checking the proposed replacement.
"""
from __future__ import annotations

from bisect import bisect_left
import difflib
import unicodedata

from . import comparison_fidelity_v1 as legacy

SOURCE_FIDELITY_CONTRACT = "source-fidelity-v2"
SUPPORTED_SOURCE_FIDELITY_CONTRACTS = (legacy.SOURCE_FIDELITY_CONTRACT, SOURCE_FIDELITY_CONTRACT)
SUPPORTED_CONTRACTS = SUPPORTED_SOURCE_FIDELITY_CONTRACTS
EVIDENCE_VERSION = legacy.EVIDENCE_VERSION
FidelityError = legacy.FidelityError
ANCHOR_CHARS = legacy.ANCHOR_CHARS
MAX_RUN_CHARACTERS = legacy.MAX_RUN_CHARACTERS
MAX_HUMAN_ALIGNMENT_CELLS = legacy.MAX_HUMAN_ALIGNMENT_CELLS
MAX_ADDRESS_FLANK_CODEPOINTS = 64
ENGINE_FAMILIES = legacy.ENGINE_FAMILIES
POLICY = {**legacy.POLICY, "version": SOURCE_FIDELITY_CONTRACT,
          "han_address_fallback": "unique_separator_projection_pair_exact_target_literal",
          "han_fallback_other_addressable_runs": "all_agree_with_after_and_at_least_two_families",
          "address_omitted_characters": "Unicode-P*-or-isspace-outside-target-only",
          "punctuation_support": "two_other_families_same_gap_exact_target_literal",
          "punctuation_outside_target_whitespace": "address_only_if_target_has_no_whitespace",
          "mixed_punctuation_patches": "hold", "punctuation_before_veto": False,
          "max_address_flank_codepoints": MAX_ADDRESS_FLANK_CODEPOINTS}
REASONS = {**legacy.REASONS,
           "independent_ocr_conflict": "정확한 같은 위치를 읽은 다른 OCR 판독이 제안 문자와 달라 보류합니다.",
           "mixed_punctuation_patch": "부호·공백 변경을 다른 문자와 함께 묶은 제안은 최소 범위로 다시 확인해야 합니다.",
           "ambiguous_punctuation_address": "부호 변경의 원래 위치를 고유하게 확인할 수 없어 보류합니다.",
           "insufficient_punctuation_ocr_support": "같은 위치의 부호에 대해 기준 외 두 OCR 계열이 동의하지 않아 보류합니다."}
_digest = legacy._digest
_text_sha = legacy._text_sha
_options = legacy._options
_han = legacy._han
_selector = legacy._selector
_protected_layout = legacy._protected_layout
_protected_unicode_changed = legacy._protected_unicode_changed
_overlaps = legacy._overlaps
_form_family = legacy._form_family
form_related = legacy.form_related
dictionary_manifest = legacy.dictionary_manifest


def source_fidelity_reason(reason_codes):
    return " ".join(REASONS.get(code, "원본 보존 근거를 확인해야 하는 제안입니다.") for code in reason_codes)


def _contract(batch):
    contract = batch.get("source_fidelity_contract")
    if contract not in (None, *SUPPORTED_SOURCE_FIDELITY_CONTRACTS):
        raise FidelityError("지원하지 않는 원본 보존 검증 계약입니다.")
    return contract


def build_source_fidelity_evidence(batch, runs, *, policy=None):
    _contract(batch)
    return legacy.build_source_fidelity_evidence(batch, runs, policy=policy)


def validate_source_fidelity_evidence(batch, runs, *, policy=None):
    contract = _contract(batch)
    if contract is None:
        raise FidelityError("원본 보존 검증 계약이 누락됐습니다.")
    if contract == legacy.SOURCE_FIDELITY_CONTRACT:
        return legacy.validate_source_fidelity_evidence(batch, runs, policy=policy)
    return legacy.validate_source_fidelity_evidence(
        {**batch, "source_fidelity_contract": legacy.SOURCE_FIDELITY_CONTRACT}, runs, policy=policy)


def _authenticated_evidence(batch, runs, policy):
    contract = _contract(batch)
    if contract == SOURCE_FIDELITY_CONTRACT:
        if runs is not None:
            validate_source_fidelity_evidence(batch, runs, policy=policy)
        return legacy._frozen_evidence(
            {**batch, "source_fidelity_contract": legacy.SOURCE_FIDELITY_CONTRACT}, runs, policy)
    return legacy._frozen_evidence(batch, runs, policy)


def _frozen_evidence(batch, runs, policy):
    return _authenticated_evidence(batch, runs, policy)


def _separator(char):
    return char.isspace() or unicodedata.category(char).startswith("P")


def _projection(text, cache):
    if text not in cache:
        indices = tuple(index for index, char in enumerate(text) if not _separator(char))
        cache[text] = ("".join(text[index] for index in indices), indices)
    return cache[text]


def _pair_locations(key, left, right, width):
    # Deliberately ignore the middle value when checking uniqueness.
    return [i for i in range(len(key) - len(left) - width - len(right) + 1)
            if key.startswith(left, i) and key.startswith(right, i + len(left) + width)]


def _semantic_support(batch, proposal, evidence, cache):
    snapshot, before, after = batch["snapshot_text"], proposal["before"], proposal["after"]
    start, end = proposal["start"], proposal["end"]
    if (len(snapshot) > MAX_RUN_CHARACTERS or not before or len(before) != len(after)
            or any(_separator(char) for char in before + after)):
        return [], "insufficient_unique_anchors", []
    key, indices = _projection(snapshot, cache)
    a, b = bisect_left(indices, start), bisect_left(indices, end)
    if a < ANCHOR_CHARS or len(key) - b < ANCHOR_CHARS or b - a != len(before):
        return [], "insufficient_unique_anchors", []
    left, right = key[a - ANCHOR_CHARS:a], key[b:b + ANCHOR_CHARS]
    if (_pair_locations(key, left, right, len(before)) != [a - ANCHOR_CHARS]
            or start - indices[a - ANCHOR_CHARS] > MAX_ADDRESS_FLANK_CODEPOINTS
            or indices[b + ANCHOR_CHARS - 1] - end > MAX_ADDRESS_FLANK_CODEPOINTS):
        return [], "insufficient_unique_anchors", []
    baseline_family = ENGINE_FAMILIES[evidence["baseline_engine"]]
    supporting, observations = [], []
    for run in evidence["runs"]:
        text = run.get("text")
        if (not run.get("eligible") or not isinstance(text, str) or run["family"] == baseline_family
                or run["run_id"] == evidence["baseline_run_id"]):
            continue
        candidate_key, candidate_indices = _projection(text, cache)
        locations = _pair_locations(candidate_key, left, right, len(before))
        if len(locations) != 1:
            continue
        location = locations[0]
        middle = location + ANCHOR_CHARS
        c, d = candidate_indices[middle], candidate_indices[middle + len(after) - 1] + 1
        if (c - candidate_indices[location] > MAX_ADDRESS_FLANK_CODEPOINTS
                or candidate_indices[middle + len(after) + ANCHOR_CHARS - 1] - d > MAX_ADDRESS_FLANK_CODEPOINTS):
            continue
        record = {"engine": run["engine"], "family": run["family"], "run_id": run["run_id"],
            "raw_sha256": run["raw_sha256"], "start": c, "end": d,
            "window_sha256": _text_sha(text[candidate_indices[location]:candidate_indices[middle + len(after) + ANCHOR_CHARS - 1] + 1]),
            "address_method": "separator-projection-v1", "source_start": start, "source_end": end,
            "source_anchor_ranges": [[indices[a - ANCHOR_CHARS], start], [end, indices[b + ANCHOR_CHARS - 1] + 1]],
            "anchor_ranges": [[candidate_indices[location], c], [d, candidate_indices[middle + len(after) + ANCHOR_CHARS - 1] + 1]],
            "target_literal_sha256": _text_sha(text[c:d]), "matches_after": text[c:d] == after,
            "matches_before": text[c:d] == before}
        observations.append(record)
        if record["matches_after"]:
            supporting.append(record)
    reason = "" if len({row["family"] for row in supporting}) >= 2 else "insufficient_independent_ocr_support"
    return supporting, reason, observations


def _exact_observations(batch, proposal, evidence):
    snapshot = batch["snapshot_text"]
    start, end, before, after = (proposal[k] for k in ("start", "end", "before", "after"))
    if len(snapshot) > MAX_RUN_CHARACTERS:
        return []
    left, right = snapshot[max(0, start - ANCHOR_CHARS):start], snapshot[end:end + ANCHOR_CHARS]
    if len(left) != ANCHOR_CHARS or len(right) != ANCHOR_CHARS:
        return []
    if _pair_locations(snapshot, left, right, len(before)) != [start - ANCHOR_CHARS]:
        return []
    baseline_family = ENGINE_FAMILIES[evidence["baseline_engine"]]
    observations = []
    for run in evidence["runs"]:
        text = run.get("text")
        if (not run.get("eligible") or not isinstance(text, str) or run["family"] == baseline_family
                or run["run_id"] == evidence["baseline_run_id"]):
            continue
        locations = _pair_locations(text, left, right, len(before))
        if len(locations) != 1:
            continue
        c = locations[0] + ANCHOR_CHARS
        d = c + len(before)
        observations.append({"engine": run["engine"], "family": run["family"], "run_id": run["run_id"],
            "raw_sha256": run["raw_sha256"], "start": c, "end": d,
            "target_literal_sha256": _text_sha(text[c:d]), "matches_after": text[c:d] == after,
            "matches_before": text[c:d] == before, "address_method": "exact-codepoint-anchors"})
    return observations


def _ocr_support(batch, proposal, evidence):
    exact, exact_reason = legacy._ocr_support(batch, proposal, evidence)
    if not exact_reason:
        return exact, "", []
    projected, projected_reason, observations = _semantic_support(batch, proposal, evidence, {})
    observations += _exact_observations(batch, proposal, evidence)
    combined = {row["run_id"]: {**row, "address_method": "exact-codepoint-anchors"} for row in exact}
    for row in projected:
        combined.setdefault(row["run_id"], row)
    supporting = list(combined.values())
    conflicts = [row for row in observations if not row["matches_after"]]
    by_run = {}
    for row in observations:
        previous = by_run.setdefault(row["run_id"], row)
        if (previous["start"], previous["end"]) != (row["start"], row["end"]):
            conflicts.extend([{**previous, "address_conflict": True}, {**row, "address_conflict": True}])
    if len({row["family"] for row in supporting}) >= 2:
        return supporting, "independent_ocr_conflict" if conflicts else "", conflicts
    return supporting, projected_reason or exact_reason, conflicts


def punctuation_change_kind(before, after):
    """Classify formatting relationships without producing replacement text."""
    def width_key(text):
        result = []
        for char in text:
            decomposition = unicodedata.decomposition(char).split()
            if (len(decomposition) == 2 and decomposition[0] in ("<wide>", "<narrow>")
                    and _separator(chr(int(decomposition[1], 16)))):
                result.append(chr(int(decomposition[1], 16)))
            else:
                result.append(char)
        return "".join(result)
    left, right = width_key(before), width_key(after)
    if left == right:
        return "width_style" if before != after else "unchanged"
    if "".join(c for c in left if not c.isspace()) == "".join(c for c in right if not c.isspace()):
        return "spacing_style"
    if not before:
        return "insertion"
    if not after:
        return "deletion"
    return "replacement"


def _punctuation_witnesses(batch, proposal, evidence):
    """Classify exact entire-gap evidence; this helper makes no accept decision."""
    snapshot, before, after = batch["snapshot_text"], proposal["before"], proposal["after"]
    start, end = proposal["start"], proposal["end"]
    result = {"kind": punctuation_change_kind(before, after), "address_valid": False,
              "after_families": [], "before_families": [], "runs": []}
    if (len(snapshot) > MAX_RUN_CHARACTERS or any(not _separator(c) for c in before + after)):
        return result
    cache = {}
    key, indices = _projection(snapshot, cache)
    a, b = bisect_left(indices, start), bisect_left(indices, end)
    if a != b or a < ANCHOR_CHARS or len(key) - b < ANCHOR_CHARS:
        return result
    left, right = key[a - ANCHOR_CHARS:a], key[b:b + ANCHOR_CHARS]
    if _pair_locations(key, left, right, 0) != [a - ANCHOR_CHARS]:
        return result
    gap_start, gap_end = indices[a - 1] + 1, indices[a]
    if (gap_end - gap_start > MAX_ADDRESS_FLANK_CODEPOINTS
            or start - indices[a - ANCHOR_CHARS] > MAX_ADDRESS_FLANK_CODEPOINTS
            or indices[b + ANCHOR_CHARS - 1] - end > MAX_ADDRESS_FLANK_CODEPOINTS):
        return result
    original_gap = snapshot[gap_start:gap_end]
    prefix, suffix = snapshot[gap_start:start], snapshot[end:gap_end]
    expected_gap = prefix + after + suffix
    whitespace_address_only = not any(char.isspace() for char in before + after)
    result.update(address_valid=True, source_gap=[gap_start, gap_end],
                  before_gap_sha256=_text_sha(original_gap), after_gap_sha256=_text_sha(expected_gap),
                  comparison_mode="outside-target-whitespace-address-only" if whitespace_address_only else "whole-gap-exact")

    def literal_at_gap(gap, expected, literal):
        if not whitespace_address_only:
            return (len(prefix), len(prefix) + len(literal)) if gap == expected else None
        indices = [i for i, char in enumerate(gap) if not char.isspace()]
        if "".join(gap[i] for i in indices) != "".join(char for char in expected if not char.isspace()):
            return None
        column = sum(not char.isspace() for char in prefix)
        if not literal:
            boundary = indices[column] if column < len(indices) else len(gap)
            return boundary, boundary
        if column + len(literal) > len(indices):
            return None
        a, b = indices[column], indices[column + len(literal) - 1] + 1
        return (a, b) if gap[a:b] == literal else None

    baseline_family = ENGINE_FAMILIES[evidence["baseline_engine"]]
    for run in evidence["runs"]:
        text = run.get("text")
        if (not run.get("eligible") or not isinstance(text, str) or run["family"] == baseline_family
                or run["run_id"] == evidence["baseline_run_id"]):
            continue
        candidate_key, candidate_indices = _projection(text, cache)
        locations = _pair_locations(candidate_key, left, right, 0)
        if len(locations) != 1:
            continue
        middle = locations[0] + ANCHOR_CHARS
        c, d = candidate_indices[middle - 1] + 1, candidate_indices[middle]
        if (d - c > MAX_ADDRESS_FLANK_CODEPOINTS
                or c - candidate_indices[locations[0]] > MAX_ADDRESS_FLANK_CODEPOINTS
                or candidate_indices[middle + ANCHOR_CHARS - 1] - d > MAX_ADDRESS_FLANK_CODEPOINTS):
            continue
        gap = text[c:d]
        target = literal_at_gap(gap, expected_gap, after)
        stance = "after" if target is not None else "neither"
        if target is None:
            target = literal_at_gap(gap, original_gap, before)
            if target is not None:
                stance = "before"
        result["runs"].append({"engine": run["engine"], "family": run["family"], "run_id": run["run_id"],
            "raw_sha256": run["raw_sha256"], "gap_start": c, "gap_end": d, "gap_sha256": _text_sha(gap),
            "stance": stance, "address_method": "whole-separator-gap-v1",
            "target_start": c + target[0] if target is not None else None,
            "target_end": c + target[1] if target is not None else None,
            "target_literal_sha256": _text_sha(text[c + target[0]:c + target[1]]) if target is not None else None})
    for stance in ("after", "before"):
        result[stance + "_families"] = sorted({row["family"] for row in result["runs"] if row["stance"] == stance})
    return result


def assess_source_fidelity(batch, proposals, visual=(), *, runs=None, policy=None, contract=None):
    """Authenticate original evidence, then assess without rewriting its record.

    An explicit contract reproduces a stored proof. Default assessment always
    uses the current policy, including for pending older requests.
    """
    selected = SOURCE_FIDELITY_CONTRACT if contract is None else contract
    if selected not in SUPPORTED_SOURCE_FIDELITY_CONTRACTS:
        raise FidelityError("지원하지 않는 원본 보존 판정 버전입니다.")
    evidence = _authenticated_evidence(batch, runs, policy)
    frozen = {**batch, "source_fidelity_contract": selected,
              "source_fidelity_options": evidence["options"],
              "source_fidelity_evidence": evidence,
              "source_fidelity_evidence_sha256": _digest(evidence)}
    if selected == legacy.SOURCE_FIDELITY_CONTRACT:
        return legacy.assess_source_fidelity(frozen, proposals, visual, policy=policy)
    return _assess_current(frozen, proposals, visual, policy=policy)


def _assess_current(batch, proposals, visual=(), *, runs=None, policy=None):
    if batch.get("phase") not in ("visual", "editorial"):
        raise FidelityError("원본 보존 검증 단계가 올바르지 않습니다.")
    evidence = _frozen_evidence(batch, runs, policy)
    options = evidence["options"]
    dictionaries = dictionary_manifest()
    verdicts = []
    for proposal in proposals:
        reasons, supporting, opposing, punctuation_evidence = [], [], [], None
        start, end, before, after = (proposal[k] for k in ("start", "end", "before", "after"))
        if (type(start) is not int or type(end) is not int or not 0 <= start <= end <= len(batch["snapshot_text"])
                or batch["snapshot_text"][start:end] != before):
            raise FidelityError("AI 제안의 원래 문자 위치가 보존 근거와 다릅니다.")
        if proposal.get("uncertain"):
            reasons.append("model_uncertain")
        if not evidence["snapshot_alignment_bounded"]:
            reasons.append("source_alignment_unavailable")
        opcodes = difflib.SequenceMatcher(None, before, after, autojunk=False).get_opcodes()
        if _protected_unicode_changed(batch["snapshot_text"], start, end, after, opcodes):
            reasons.append("protected_unicode_change")
        if any(_overlaps(start, end, a, b) for a, b in evidence["protected_spans"]):
            reasons.append("human_changed_span")
        changed = [(tag, a, b, c, d) for tag, a, b, c, d in opcodes if tag != "equal"]
        han_changed = any(any(_han(char) for char in before[a:b] + after[c:d]) for _, a, b, c, d in changed)
        punctuation_changed = any(any(_separator(char) for char in before[a:b] + after[c:d])
                                  for _, a, b, c, d in changed)
        if punctuation_changed:
            if any(not _separator(char) for char in before + after):
                reasons.append("mixed_punctuation_patch")
            else:
                punctuation_evidence = _punctuation_witnesses(batch, proposal, evidence)
                if not punctuation_evidence["address_valid"]:
                    reasons.append("ambiguous_punctuation_address")
                elif len(punctuation_evidence["after_families"]) < 2:
                    reasons.append("insufficient_punctuation_ocr_support")
        related = False
        ambiguous = False
        for tag, a, b, c, d in changed:
            old, new = before[a:b], after[c:d]
            if not any(_han(char) for char in old + new):
                continue
            if tag != "replace" or len(old) != len(new):
                ambiguous = True
            if dictionaries["available"]:
                related |= any(form_related(left, right) for left in old for right in new if _han(left) and _han(right))
        if han_changed and not dictionaries["available"]:
            reasons.append("form_dictionary_unavailable")
        if batch["phase"] == "visual":
            if proposal["kind"] != "recognition":
                reasons.append("visual_phase_kind")
            if related:
                reasons.append("visual_form_family_change")
            if han_changed:
                if ambiguous or len(before) != len(after):
                    reasons.append("ambiguous_han_alignment")
                else:
                    supporting, support_reason, opposing = _ocr_support(batch, proposal, evidence)
                    if support_reason:
                        reasons.append(support_reason)
        else:
            from .comparison_ai import ReviewPolicy, AIReviewError, _editorial_allowed, _provisional_before
            try:
                provisional, _ = _provisional_before(batch, start, end, visual)
            except AIReviewError:
                provisional = before
                reasons.append("editorial_visual_overlap")
            if not _editorial_allowed(provisional, after, proposal["kind"], ReviewPolicy(**options)):
                reasons.append("editorial_policy_violation")
            for basis in visual:
                a, b = basis["start"], basis["end"]
                if not start <= a <= b <= end:
                    continue
                original_slice = before[a - start:b - start]
                if len(before) == len(after) and after[a - start:b - start] == original_slice:
                    continue  # The editorial result does not carry this visual change.
                basis_verdict = assess_source_fidelity({**batch, "phase": "visual"}, [basis],
                    runs=runs, policy=policy)["proposals"][0]
                if basis_verdict["blocked"]:
                    reasons.append("editorial_carries_blocked_visual")
        reasons = list(dict.fromkeys(reasons))
        verdicts.append({"proposal_id": proposal["id"],
                         "proposal_sha256": _digest({k: v for k, v in proposal.items() if k != "id"}),
                         "blocked": bool(reasons), "reason_codes": reasons,
                         "supporting_families": sorted({row["family"] for row in supporting}),
                         "supporting_runs": supporting,
                         "opposing_families": sorted({row["family"] for row in opposing}),
                         "opposing_runs": opposing, "punctuation_evidence": punctuation_evidence})
    return {"contract": SOURCE_FIDELITY_CONTRACT, "policy_sha256": _digest(POLICY),
            "dictionary": dictionaries, "evidence_sha256": _digest(evidence), "proposals": verdicts}



def verify_source_fidelity(review, previous_visual=(), *, runs=None):
    batch = review["request"]
    contract = _contract(batch)
    prompt = review.get("metadata", {}).get("prompt_version")
    if ((prompt == "comparison-ai-v8" and contract != legacy.SOURCE_FIDELITY_CONTRACT)
            or (prompt == "comparison-ai-v9" and contract != SOURCE_FIDELITY_CONTRACT)
            or (contract == SOURCE_FIDELITY_CONTRACT and prompt != "comparison-ai-v9")):
        raise FidelityError("AI 요청 버전과 원본 보존 검증 계약이 일치하지 않습니다.")
    if contract != SOURCE_FIDELITY_CONTRACT:
        return legacy.verify_source_fidelity(review, previous_visual, runs=runs)
    if runs is not None:
        validate_source_fidelity_evidence(batch, runs, policy=review["policy"])
    if review["status"] != "completed":
        return None
    expected = assess_source_fidelity(batch, review["proposals"], previous_visual,
        runs=runs, policy=review["policy"], contract=SOURCE_FIDELITY_CONTRACT)
    if review.get("metadata", {}).get("source_fidelity") != expected:
        raise FidelityError("AI 원본 보존 판정이 보존된 제안·독립 OCR 근거와 다릅니다.")
    return expected
