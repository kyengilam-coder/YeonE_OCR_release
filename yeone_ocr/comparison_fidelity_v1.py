"""Versioned, ground-truth-free source-form and OCR-support attestations.

Classification never rewrites OCR or an AI proposal. Dictionaries are used only
to recognise related glyph forms. Unsupported proposals remain inspectable and
must not become human corrections through the ordinary accept action.
"""
from __future__ import annotations

from collections import defaultdict
import difflib
from functools import lru_cache
import hashlib
import importlib.metadata
import json
from pathlib import Path
import unicodedata

SOURCE_FIDELITY_CONTRACT = "source-fidelity-v1"
EVIDENCE_VERSION = "source-fidelity-evidence-v1"
ANCHOR_CHARS = 4
MAX_RUN_CHARACTERS = 20_000
MAX_HUMAN_ALIGNMENT_CELLS = 4_000_000
ENGINE_FAMILIES = {"tesseract": "tesseract", "rapidocr": "paddle", "paddle_server": "paddle",
                   "ppocrv6": "paddle", "easyocr": "easyocr", "glmocr": "glmocr", "surya": "surya"}
# Registry extension only: existing engine classifications, evidence encoding,
# contract versions, and policy digests stay fixed. PP-OCRv6 cannot contribute
# an independent vote alongside RapidOCR/PP-OCRv5 or Paddle server models.
POLICY = {"version": SOURCE_FIDELITY_CONTRACT, "visual_related_forms": "hold",
          "other_han_changes": "two_other_families_exact_unique_anchor_agreement",
          "baseline_family_excluded": True, "minimum_support_families": 2,
          "anchor_characters": ANCHOR_CHARS, "max_run_characters": MAX_RUN_CHARACTERS,
          "max_human_alignment_cells": MAX_HUMAN_ALIGNMENT_CELLS,
          "protected_unicode": "whole_snapshot_identity_and_selector_attachment",
          "han_insertions_deletions_or_ambiguous_alignment": "hold", "human_changed_spans": "hold",
          "source_and_proposal_normalization": "none"}
REASONS = {
    "protected_unicode_change": "호환한자·이체자 선택자의 글자나 위치가 달라 원본 보존을 위해 보류합니다.",
    "visual_form_family_change": "간체·번체·이체자 자형 변경을 이미지 판독 단계에서 보류합니다.",
    "form_dictionary_unavailable": "자형 관계 사전을 검증할 수 없어 한자 변경을 보류합니다.",
    "human_changed_span": "사람이 이미 교정한 범위의 변경을 보류합니다.",
    "ambiguous_han_alignment": "한자 삽입·삭제 또는 위치 정렬이 모호하여 보류합니다.",
    "insufficient_unique_anchors": "수정 위치 양쪽의 고유한 원문 근거가 부족합니다.",
    "insufficient_independent_ocr_support": "기준을 제외한 두 OCR 계열의 정확한 위치 동의가 없어 보류합니다.",
    "editorial_policy_violation": "선택한 번체·문장부호 편집 정책에 맞지 않아 보류합니다.",
    "editorial_visual_overlap": "앞 단계 판독 제안의 일부만 겹치는 편집을 보류합니다.",
    "editorial_carries_blocked_visual": "보류된 앞 단계 판독을 포함하는 편집 제안을 보류합니다.",
    "visual_phase_kind": "이미지 판독 단계의 문자·부호 변환 제안을 보류합니다.",
    "source_alignment_unavailable": "원문과 사람 교정 범위의 안전한 정렬 한도를 넘어 보류합니다.",
    "model_uncertain": "AI가 원본 판독을 확정하지 못한 제안입니다.",
}


class FidelityError(ValueError):
    pass


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
        separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


def _text_sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def source_fidelity_reason(reason_codes):
    return " ".join(REASONS.get(code, "원본 보존 근거를 확인해야 하는 제안입니다.") for code in reason_codes)


def _options(batch, policy=None):
    value = policy if policy is not None else batch.get("source_fidelity_options", {})
    result = {}
    for key, default in (("traditional", False), ("punctuation", False), ("preserve_variants", True)):
        item = value.get(key, default) if isinstance(value, dict) else getattr(value, key, default)
        if type(item) is not bool:
            raise FidelityError("원본 보존 편집 정책이 올바르지 않습니다.")
        result[key] = item
    return result


@lru_cache(maxsize=1)
def _dictionary_manifest():
    from .collate.zh import VARIANTS, available
    variants_sha = _digest(VARIANTS)
    if not available():
        return {"available": False, "variants_sha256": variants_sha, "opencc_version": None,
                "opencc_files_sha256": None}
    try:
        import opencc
        folder = Path(opencc.__file__).parent
        inventory = {}
        for directory, suffix in (("config", ".json"), ("dictionary", ".txt")):
            for path in sorted((folder / directory).glob("*" + suffix)):
                inventory[path.relative_to(folder).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
        if not inventory:
            raise OSError("Missing dictionary inventory")
        version = importlib.metadata.version("opencc-python-reimplemented")
        return {"available": True, "variants_sha256": variants_sha, "opencc_version": version,
                "opencc_files_sha256": _digest(inventory)}
    except (OSError, ImportError, importlib.metadata.PackageNotFoundError):
        return {"available": False, "variants_sha256": variants_sha, "opencc_version": None,
                "opencc_files_sha256": None}


def dictionary_manifest():
    return json.loads(json.dumps(_dictionary_manifest()))


def _han(char):
    code = ord(char)
    return (0x3400 <= code <= 0x4DBF or 0x4E00 <= code <= 0x9FFF
            or 0xF900 <= code <= 0xFAFF or 0x20000 <= code <= 0x323AF
            or unicodedata.name(char, "").startswith(("CJK UNIFIED IDEOGRAPH-", "CJK COMPATIBILITY IDEOGRAPH-")))


def _selector(char):
    code = ord(char)
    return (0xFE00 <= code <= 0xFE0F or 0xE0100 <= code <= 0xE01EF
            or code in (0x180B, 0x180C, 0x180D, 0x180F))


def _protected_layout(text):
    return [(index, char, text[index - 1] if index and _selector(char) else None)
            for index, char in enumerate(text)
            if _selector(char) or 0xF900 <= ord(char) <= 0xFAFF or 0x2F800 <= ord(char) <= 0x2FA1F]


def _protected_unicode_changed(snapshot, start, end, after, opcodes):
    """Preserve protected identities and VS attachment across edit boundaries.

    Equal spans retain source identities. Unrelated insertions may move an intact
    sequence, but cannot replace its base, split it, or attach an orphan VS.
    """
    changed = snapshot[:start] + after + snapshot[end:]
    delta = len(after) - (end - start)

    def mapped(index):
        if index < start:
            return index
        if index >= end:
            return index + delta
        for tag, a, b, c, _ in opcodes:
            if tag == "equal" and a <= index - start < b:
                return start + c + index - start - a
        return None

    retained = set()
    for index, char, _ in _protected_layout(snapshot):
        destination = mapped(index)
        if destination is None or changed[destination] != char:
            return True
        retained.add(destination)
        if _selector(char):
            if index == 0:
                if destination != 0:
                    return True
            else:
                base = mapped(index - 1)
                if base is None or base != destination - 1 or changed[base] != snapshot[index - 1]:
                    return True
    return retained != {index for index, _, _ in _protected_layout(changed)}


@lru_cache(maxsize=4096)
def _form_family(char):
    from .collate.zh import VARIANTS, candidates, to_simplified, to_traditional
    graph = defaultdict(set)
    for left, right in VARIANTS.items():
        graph[left].add(right);graph[right].add(left)
    seen, pending = set(), [char]
    while pending and len(seen) <= 128:
        current = pending.pop()
        if current in seen:
            continue
        seen.add(current)
        related = graph[current] | {to_simplified(current), to_traditional(current), *candidates(current)}
        pending.extend(value for value in related if len(value) == 1 and _han(value) and value not in seen)
    return frozenset(seen)


def form_related(left, right):
    return left != right and _han(left) and _han(right) and bool(_form_family(left) & _form_family(right))


def _overlaps(a, b, c, d):
    if a == b:
        return c <= a <= d
    if c == d:
        return a <= c <= b
    return a < d and c < b


def build_source_fidelity_evidence(batch, runs, *, policy=None):
    """Freeze trusted referenced raw text once per batch, without AI input use."""
    from .comparison import _compatible, _eligibility, _run_complete
    snapshot = batch.get("snapshot_text")
    refs = batch.get("runs")
    if (not isinstance(snapshot, str) or not isinstance(refs, list)
            or _text_sha(snapshot) != batch.get("snapshot_sha256")):
        raise FidelityError("원본 보존 검증의 문장 지문이 올바르지 않습니다.")
    baseline = runs.get(batch.get("baseline_run_id"))
    if baseline is None or baseline.get("engine") not in ENGINE_FAMILIES:
        raise FidelityError("원본 보존 검증의 기준 판독이 없습니다.")
    output, seen = [], set()
    for ref in refs:
        run_id = ref.get("run_id")
        run = runs.get(run_id)
        if (not isinstance(run_id, str) or run_id in seen or run is None
                or run.get("engine") != ref.get("engine") or run.get("raw_sha256") != ref.get("raw_sha256")
                or _digest(run.get("raw")) != run.get("raw_sha256") or run["engine"] not in ENGINE_FAMILIES):
            raise FidelityError("원본 보존 검증의 원시 판독 연결이 일치하지 않습니다.")
        seen.add(run_id)
        raw_text = run["raw"].get("text")
        if not isinstance(raw_text, str):
            raise FidelityError("원본 보존 근거의 원시 문장이 올바르지 않습니다.")
        eligible, note = _eligibility(run, raw_text, snapshot, run_id == baseline["id"])
        complete = _run_complete(run)
        compatible = _compatible(baseline, run)
        bounded = len(raw_text) <= MAX_RUN_CHARACTERS
        output.append({"run_id": run_id, "engine": run["engine"], "family": ENGINE_FAMILIES[run["engine"]],
                       "raw_sha256": run["raw_sha256"], "text_sha256": _text_sha(raw_text),
                       "text": raw_text if bounded else None, "characters": len(raw_text),
                       "eligible": bool(eligible and complete and compatible and bounded),
                       "eligibility_reason": note, "generation_complete": complete,
                       "compatible": compatible, "bounded": bounded})
    if baseline["id"] not in seen:
        raise FidelityError("원본 보존 검증 범위에 기준 판독이 없습니다.")
    original = baseline["raw"]["text"]
    alignment_bounded = (original == snapshot or (max(len(original), len(snapshot)) <= MAX_RUN_CHARACTERS
                         and len(original) * len(snapshot) <= MAX_HUMAN_ALIGNMENT_CELLS))
    protected = ([] if original == snapshot or not alignment_bounded else
                 [[c, d] for tag, _, _, c, d in difflib.SequenceMatcher(None, original, snapshot, autojunk=False).get_opcodes() if tag != "equal"])
    return {"version": EVIDENCE_VERSION, "baseline_run_id": baseline["id"],
            "baseline_engine": baseline["engine"], "baseline_raw_sha256": baseline["raw_sha256"],
            "snapshot_sha256": batch["snapshot_sha256"], "protected_spans": protected,
            "snapshot_alignment_bounded": alignment_bounded,
            "options": _options(batch, policy), "runs": output}


def validate_source_fidelity_evidence(batch, runs, *, policy=None):
    if batch.get("source_fidelity_contract") != SOURCE_FIDELITY_CONTRACT:
        raise FidelityError("지원하지 않는 원본 보존 검증 계약입니다.")
    expected = build_source_fidelity_evidence(batch, runs, policy=policy)
    if (batch.get("source_fidelity_options") != _options(batch, policy)
            or batch.get("source_fidelity_evidence") != expected
            or batch.get("source_fidelity_evidence_sha256") != _digest(expected)):
        raise FidelityError("원본 보존 검증 근거가 보존된 원시 판독과 다릅니다.")


def _frozen_evidence(batch, runs, policy):
    if batch.get("source_fidelity_contract") not in (None, SOURCE_FIDELITY_CONTRACT):
        raise FidelityError("지원하지 않는 원본 보존 검증 계약입니다.")
    if runs is not None:
        if batch.get("source_fidelity_contract") == SOURCE_FIDELITY_CONTRACT:
            validate_source_fidelity_evidence(batch, runs, policy=policy)
        return build_source_fidelity_evidence(batch, runs, policy=policy)
    evidence = batch.get("source_fidelity_evidence")
    if (batch.get("source_fidelity_contract") != SOURCE_FIDELITY_CONTRACT or not isinstance(evidence, dict)
            or evidence.get("version") != EVIDENCE_VERSION
            or batch.get("source_fidelity_evidence_sha256") != _digest(evidence)
            or evidence.get("snapshot_sha256") != batch.get("snapshot_sha256")
            or evidence.get("options") != _options(batch, policy)):
        raise FidelityError("원본 보존 판정에 필요한 고정 근거가 없습니다.")
    return evidence


def _ocr_support(batch, proposal, evidence):
    snapshot = batch["snapshot_text"]
    if len(snapshot) > MAX_RUN_CHARACTERS:
        return [], "source_alignment_unavailable"
    start, end = proposal["start"], proposal["end"]
    left, right = snapshot[max(0, start - ANCHOR_CHARS):start], snapshot[end:end + ANCHOR_CHARS]
    if len(left) != ANCHOR_CHARS or len(right) != ANCHOR_CHARS:
        return [], "insufficient_unique_anchors"
    def locations(text):
        width = len(left) + len(proposal["before"]) + len(right)
        return [i for i in range(len(text) - width + 1)
                if text.startswith(left, i) and text.startswith(right, i + len(left) + len(proposal["before"]))]
    source_locations = locations(snapshot)
    if source_locations != [start - len(left)]:
        return [], "insufficient_unique_anchors"
    needle = left + proposal["after"] + right
    baseline_family = ENGINE_FAMILIES[evidence["baseline_engine"]]
    supporting = []
    for run in evidence["runs"]:
        text = run.get("text")
        if (not run.get("eligible") or not isinstance(text, str) or run["family"] == baseline_family
                or run["run_id"] == evidence["baseline_run_id"]):
            continue
        candidates = locations(text)
        if len(candidates) == 1 and text.startswith(needle, candidates[0]):
            a = candidates[0] + len(left)
            supporting.append({"engine": run["engine"], "family": run["family"], "run_id": run["run_id"],
                               "raw_sha256": run["raw_sha256"], "start": a, "end": a + len(proposal["after"]),
                               "window_sha256": _text_sha(needle)})
    return supporting, "" if len({row["family"] for row in supporting}) >= 2 else "insufficient_independent_ocr_support"


def assess_source_fidelity(batch, proposals, visual=(), *, runs=None, policy=None):
    if batch.get("phase") not in ("visual", "editorial"):
        raise FidelityError("원본 보존 검증 단계가 올바르지 않습니다.")
    evidence = _frozen_evidence(batch, runs, policy)
    options = evidence["options"]
    dictionaries = dictionary_manifest()
    verdicts = []
    for proposal in proposals:
        reasons, supporting = [], []
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
                    supporting, support_reason = _ocr_support(batch, proposal, evidence)
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
                         "supporting_runs": supporting})
    return {"contract": SOURCE_FIDELITY_CONTRACT, "policy_sha256": _digest(POLICY),
            "dictionary": dictionaries, "evidence_sha256": _digest(evidence), "proposals": verdicts}


def verify_source_fidelity(review, previous_visual=(), *, runs=None):
    batch = review["request"]
    new_prompt = review.get("metadata", {}).get("prompt_version") == "comparison-ai-v8"
    if batch.get("source_fidelity_contract") not in (None, SOURCE_FIDELITY_CONTRACT):
        raise FidelityError("지원하지 않는 원본 보존 검증 계약입니다.")
    if batch.get("source_fidelity_contract") != SOURCE_FIDELITY_CONTRACT:
        if new_prompt:
            raise FidelityError("새 AI 검증에서 원본 보존 계약이 누락됐습니다.")
        return None
    if runs is not None:
        validate_source_fidelity_evidence(batch, runs, policy=review["policy"])
    if review["status"] != "completed":
        return None
    expected = assess_source_fidelity(batch, review["proposals"], previous_visual, runs=runs, policy=review["policy"])
    if review.get("metadata", {}).get("source_fidelity") != expected:
        raise FidelityError("AI 원본 보존 판정이 보존된 제안·독립 OCR 근거와 다릅니다.")
    return expected
