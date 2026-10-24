"""Additional image-OCR vetoes without source-PDF text or reference answers.

This layer can only hold an existing proposal. It does not generate, approve,
rewrite or replace a proposal and does not relax its original safety policy.
The existing v3 generator still requires three independent Han families.
"""
from __future__ import annotations

import copy
import difflib

from . import comparison as core
from . import comparison_fidelity_v1 as identity
from . import comparison_fidelity_v3 as fidelity

CONTRACT = "image-only-proposal-safety-v1"
POLICY = {
    "version": CONTRACT,
    "action": "additional-hold-only-no-new-approval",
    "source": "authenticated-compatible-complete-active-image-OCR-runs",
    "protected_han": "source-fidelity-variant-and-Unicode-identity-all-pending-stages",
    "explicit_traditional_editorial": "caller-authenticated-user-option-and-traditional-kind-only; skip-forms-veto-only",
    "punctuation": "nonwidth-change-two-independent-exact-original-gap-families-veto",
    "punctuation_address": "unique-bounded-four-content-flanks-whole-gap",
    "width": "existing-policy-unchanged-no-width-proof-invented",
    "baseline_family": "excluded",
    "rapid_and_paddle": "one-family",
    "existing_candidate_gates": "remain-required",
    "PDF_text_reference_AI_content": "not-used",
}
REASONS = {
    "protected_source_variant": "원본의 이체자·간번체 관계를 바꾸는 제안이어서 보류합니다.",
    "protected_unicode_identity": "호환 한자·변형 선택자의 원래 문자 관계를 바꾸어 보류합니다.",
    "variant_dictionary_unavailable": "원본 한자 형태를 보호할 사전 근거가 없어 보류합니다.",
    "independent_original_punctuation": "기준 외 두 OCR 계열이 같은 위치의 원래 부호에 동의하여 종류 변경·삭제를 보류합니다.",
}


def reason_text(codes):
    return " ".join(REASONS.get(code, "이미지 OCR의 원본 보존 근거를 확인해야 합니다.") for code in codes)


def _inputs(snapshot, baseline, runs, proposal, stage):
    if stage not in ("local", "region", "roi", "ai"):
        raise core.ComparisonError("이미지 원본 보호 검사의 제안 단계가 올바르지 않습니다.")
    if not isinstance(snapshot, str) or not isinstance(baseline, dict) or not isinstance(runs, dict):
        raise core.ComparisonError("이미지 원본 보호 검사의 원시 입력이 올바르지 않습니다.")
    by_id = {}
    for run in runs.values():
        if (not isinstance(run, dict) or not isinstance(run.get("id"), str)
                or not isinstance(run.get("raw"), dict) or not isinstance(run["raw"].get("text"), str)
                or run.get("engine") not in fidelity.ENGINE_FAMILIES
                or core._digest(run.get("raw")) != run.get("raw_sha256")):
            raise core.ComparisonError("이미지 원본 보호 검사의 원시 판독 지문이 일치하지 않습니다.")
        if run["id"] in by_id and by_id[run["id"]] != run:
            raise core.ComparisonError("같은 원시 판독 ID에 서로 다른 내용이 있습니다.")
        by_id[run["id"]] = run
    if by_id.get(baseline.get("id")) != baseline:
        raise core.ComparisonError("이미지 원본 보호 검사의 기준 판독이 일치하지 않습니다.")
    if not core._run_complete(baseline) or not baseline.get("active", True):
        raise core.ComparisonError("완료된 활성 기준 판독이 필요합니다.")
    if not isinstance(proposal, dict):
        raise core.ComparisonError("이미지 원본 보호 검사의 제안이 올바르지 않습니다.")
    start, end = proposal.get("start"), proposal.get("end")
    if (type(start) is not int or type(end) is not int or not 0 <= start <= end <= len(snapshot)
            or not isinstance(proposal.get("before"), str) or not isinstance(proposal.get("after"), str)
            or snapshot[start:end] != proposal["before"] or proposal["before"] == proposal["after"]):
        raise core.ComparisonError("이미지 원본 보호 검사의 정확한 제안 위치가 일치하지 않습니다.")
    for field, expected in (("baseline_run_id", baseline["id"]),
                            ("baseline_raw_sha256", baseline["raw_sha256"]),
                            ("snapshot_sha256", core._text_sha(snapshot))):
        if field in proposal and proposal[field] != expected:
            raise core.ComparisonError("이미지 원본 보호 검사의 제안과 기준 지문이 다릅니다.")
    return by_id


def assess(snapshot, baseline, runs, proposal, *, stage, policy=None, allow_explicit_traditional=False):
    """Return a deterministic extra hold; all inputs remain untouched.

    ``runs`` may be keyed by engine or run ID. The caller remains responsible
    for original proposal eligibility, staleness, geometry and human edits.
    ``allow_explicit_traditional`` is accepted only for an AI traditional edit;
    the caller must authenticate the user's original editorial option. It
    bypasses only this extra form-related veto, never Unicode identity or the
    existing AI source-fidelity checks. Recognition/local/region edits cannot
    use it to normalize a source variant.
    No file, PDF, image, network service or reference answer is read here.
    """
    if policy is not None and policy != POLICY:
        raise core.ComparisonError("지원하지 않는 이미지 원본 보호 정책입니다.")
    by_id = _inputs(snapshot, baseline, runs, proposal, stage)
    if (type(allow_explicit_traditional) is not bool
            or allow_explicit_traditional and (stage != "ai" or proposal.get("kind") != "traditional")):
        raise core.ComparisonError("사용자가 선택한 AI 번체 편집 근거가 올바르지 않습니다.")
    batch = {"snapshot_text": snapshot, "snapshot_sha256": core._text_sha(snapshot),
             "baseline_run_id": baseline["id"],
             "runs": [{"run_id": run["id"], "engine": run["engine"], "raw_sha256": run["raw_sha256"]}
                      for run in sorted(by_id.values(), key=lambda row: (row["engine"], row["id"]))]}
    frozen = identity.build_source_fidelity_evidence(batch, by_id)
    for reading in frozen["runs"]:
        run = by_id[reading["run_id"]]
        reading["active"] = run.get("active", True)
        if not reading["active"] or run["raw"].get("engine_metadata", {}).get("generation_complete") is False:
            reading["eligible"] = False
    old, new = proposal["before"], proposal["after"]
    changes = difflib.SequenceMatcher(None, old, new, autojunk=False).get_opcodes()
    changed_pairs = [(old[a:b], new[c:d]) for tag, a, b, c, d in changes if tag != "equal"]
    changes_han = any(identity._han(char) for pair in changed_pairs for text in pair for char in text)
    dictionary = identity.dictionary_manifest() if changes_han else None
    forms = sorted({(left, right) for before, after in changed_pairs for left in before for right in after
                    if identity.form_related(left, right)}) if dictionary and dictionary["available"] else []
    protected = identity._protected_unicode_changed(snapshot, proposal["start"], proposal["end"], new, changes)
    reasons = []
    if protected:
        reasons.append("protected_unicode_identity")
    if changes_han and not dictionary["available"]:
        reasons.append("variant_dictionary_unavailable")
    if forms and not allow_explicit_traditional:
        reasons.append("protected_source_variant")
    target, derivation, target_reason = fidelity._punctuation_target(proposal)
    punctuation = None
    if target is not None:
        punctuation = fidelity._punctuation_witnesses(batch, target, frozen)
        punctuation["width_only"] = fidelity._width_only(target["before"], target["after"])
        # Width codes are not independently established by OCR or glyph shape.
        # Holding all majority/tie width disagreements would erase good edits.
        if (punctuation["address_valid"] and not punctuation["width_only"]
                and len(punctuation["before_families"]) >= 2):
            reasons.append("independent_original_punctuation")
    result = {
        "contract": CONTRACT, "policy_sha256": core._digest(POLICY),
        "stage": stage, "proposal_id": proposal.get("id"), "proposal_sha256": core._digest(proposal),
        "snapshot_sha256": batch["snapshot_sha256"], "baseline_run_id": baseline["id"],
        "baseline_raw_sha256": baseline["raw_sha256"], "blocked": bool(reasons), "reason_codes": reasons,
        "source_evidence": {"runs": [{key: value for key, value in run.items() if key != "text"}
                                     for run in frozen["runs"]],
                            "dictionary_manifest": dictionary, "variant_pairs": [list(pair) for pair in forms],
                            "explicit_traditional_editorial": allow_explicit_traditional,
                            "form_related_veto_applies": not allow_explicit_traditional,
                            "protected_unicode_identity": protected, "punctuation": punctuation,
                            "punctuation_target": ({key: target[key] for key in ("start", "end", "before", "after")}
                                                   if target is not None else None),
                            "target_derivation": derivation, "target_unavailable_reason": target_reason},
        "new_approval": False, "reference_or_pdf_text_used": False,
    }
    result["verdict_sha256"] = core._digest(result)
    return copy.deepcopy(result)
