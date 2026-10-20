"""Current protection; frozen v1-v3 proofs keep their original meaning.

Additional punctuation checks can only withhold a v3 proposal. Raw OCR, AI
proposals, and historical acceptance records are never rewritten.
"""
from __future__ import annotations

from . import comparison_fidelity_v3 as legacy_v3

SOURCE_FIDELITY_CONTRACT = "source-fidelity-v4"
SUPPORTED_SOURCE_FIDELITY_CONTRACTS = (*legacy_v3.SUPPORTED_CONTRACTS, SOURCE_FIDELITY_CONTRACT)
SUPPORTED_CONTRACTS = SUPPORTED_SOURCE_FIDELITY_CONTRACTS
EVIDENCE_VERSION = legacy_v3.EVIDENCE_VERSION
FidelityError = legacy_v3.FidelityError
ENGINE_FAMILIES = legacy_v3.ENGINE_FAMILIES
MAX_RUN_CHARACTERS = legacy_v3.MAX_RUN_CHARACTERS
POLICY = {**legacy_v3.POLICY, "version": SOURCE_FIDELITY_CONTRACT,
          "base_policy": "frozen-source-fidelity-v3",
          "matched_brackets": "preserve-original-matched-width-pair-for-single-end-changes"}
REASONS = {**legacy_v3.REASONS,
           "paired_punctuation_width_conflict": "기준 OCR에서 짝이 맞던 괄호의 한쪽 폭만 바뀌므로 원본 대조를 위해 보류합니다."}
_digest = legacy_v3._digest
_text_sha = legacy_v3._text_sha
dictionary_manifest = legacy_v3.dictionary_manifest
form_related = legacy_v3.form_related
punctuation_change_kind = legacy_v3.punctuation_change_kind

# Unicode width counterparts, not a document-, character-error-, or page list.
_PAIRS = {"(": ")", "（": "）", "[": "]", "［": "］", "{": "}", "｛": "｝",
          "「": "」", "｢": "｣"}
_CLOSES = {right: left for left, right in _PAIRS.items()}


def source_fidelity_reason(reason_codes):
    return " ".join(REASONS.get(code, "원본 보존 근거를 확인해야 하는 제안입니다.") for code in reason_codes)


def _contract(batch):
    value = batch.get("source_fidelity_contract")
    if value not in (None, *SUPPORTED_CONTRACTS):
        raise FidelityError("지원하지 않는 원본 보존 검증 계약입니다.")
    return value


def _as_v3(batch):
    return {**batch, "source_fidelity_contract": legacy_v3.SOURCE_FIDELITY_CONTRACT}


def build_source_fidelity_evidence(batch, runs, *, policy=None):
    _contract(batch)
    return legacy_v3.build_source_fidelity_evidence(_as_v3(batch), runs, policy=policy)


def validate_source_fidelity_evidence(batch, runs, *, policy=None):
    value = _contract(batch)
    return legacy_v3.validate_source_fidelity_evidence(
        _as_v3(batch) if value == SOURCE_FIDELITY_CONTRACT else batch, runs, policy=policy)


def _authenticated_evidence(batch, runs, policy):
    value = _contract(batch)
    return legacy_v3._authenticated_evidence(
        _as_v3(batch) if value == SOURCE_FIDELITY_CONTRACT else batch, runs, policy)


def _paired_width_conflict(snapshot, proposal):
    before, after = proposal["before"], proposal["after"]
    if (len(before) != len(after)
            or snapshot[proposal["start"]:proposal["end"]] != before):
        return None
    changed = {proposal["start"] + index: new for index, (old, new) in enumerate(zip(before, after))
               if old != new}
    if not changed or any(not legacy_v3._width_only(snapshot[index], new)
                          for index, new in changed.items()):
        return None
    if not any(snapshot[index] in _PAIRS or snapshot[index] in _CLOSES for index in changed):
        return None
    stack = []
    for index, char in enumerate(snapshot):
        if char in _PAIRS:
            stack.append((index, char))
        elif char in _CLOSES:
            if not stack or _PAIRS[stack[-1][1]] != char:
                # Do not invent a partner across malformed or mixed-width input.
                stack.clear()
                continue
            left, opener = stack.pop()
            if left not in changed and index not in changed:
                continue
            other = index if left in changed else left
            new_open = changed.get(left, opener)
            new_close = changed.get(index, char)
            if _PAIRS.get(new_open) == new_close:
                continue
            return {"method": "original-exact-width-bracket-stack-v1", "start": left, "end": index + 1,
                    "counterpart_start": other, "counterpart": snapshot[other],
                    "source_pair": opener + char, "proposed_pair": new_open + new_close,
                    "snapshot_sha256": _text_sha(snapshot)}
    return None


def assess_source_fidelity(batch, proposals, visual=(), *, runs=None, policy=None, contract=None):
    selected = SOURCE_FIDELITY_CONTRACT if contract is None else contract
    if selected not in SUPPORTED_CONTRACTS:
        raise FidelityError("지원하지 않는 원본 보존 판정 버전입니다.")
    evidence = _authenticated_evidence(batch, runs, policy)
    frozen = {**batch, "source_fidelity_contract": selected if selected != SOURCE_FIDELITY_CONTRACT
              else legacy_v3.SOURCE_FIDELITY_CONTRACT,
              "source_fidelity_options": evidence["options"], "source_fidelity_evidence": evidence,
              "source_fidelity_evidence_sha256": _digest(evidence)}
    proposals = list(proposals)
    result = legacy_v3.assess_source_fidelity(frozen, proposals, visual, policy=policy,
        contract=selected if selected != SOURCE_FIDELITY_CONTRACT else legacy_v3.SOURCE_FIDELITY_CONTRACT)
    if selected != SOURCE_FIDELITY_CONTRACT:
        return result
    for proposal, verdict in zip(proposals, result["proposals"]):
        if verdict["blocked"]:
            continue
        paired = _paired_width_conflict(batch["snapshot_text"], proposal)
        if paired is not None:
            verdict.update(blocked=True, reason_codes=["paired_punctuation_width_conflict"],
                           paired_punctuation_evidence=paired)
    result["contract"] = SOURCE_FIDELITY_CONTRACT
    result["policy_sha256"] = _digest(POLICY)
    return result


def verify_source_fidelity(review, previous_visual=(), *, runs=None):
    contract = _contract(review["request"])
    prompt = review.get("metadata", {}).get("prompt_version")
    current_prompts = ("comparison-ai-v11", "comparison-ai-v12")
    if ((prompt in current_prompts and contract != SOURCE_FIDELITY_CONTRACT)
            or (contract == SOURCE_FIDELITY_CONTRACT and prompt not in current_prompts)):
        raise FidelityError("AI 요청 버전과 원본 보존 검증 계약이 일치하지 않습니다.")
    if contract != SOURCE_FIDELITY_CONTRACT:
        return legacy_v3.verify_source_fidelity(review, previous_visual, runs=runs)
    if runs is not None:
        validate_source_fidelity_evidence(review["request"], runs, policy=review["policy"])
    if review["status"] != "completed":
        return None
    expected = assess_source_fidelity(review["request"], review["proposals"], previous_visual,
        runs=runs, policy=review["policy"], contract=SOURCE_FIDELITY_CONTRACT)
    if review.get("metadata", {}).get("source_fidelity") != expected:
        raise FidelityError("AI 원본 보존 판정이 보존된 제안·독립 OCR 근거와 다릅니다.")
    return expected
