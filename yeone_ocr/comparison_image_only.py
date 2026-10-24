"""Image-OCR-only rechecks, separate from proposals and human decisions.

The evidence is the immutable image OCR readings. No native PDF text, reference
document, evaluation label or new AI response is consulted by this module.
"""
from __future__ import annotations

import copy

from . import comparison as core

CONTRACT = "image-only-proposal-check-v1"


def _items(page):
    items = {}
    for review in page.get("refinement_reviews", []):
        for item in review["proposals"]:
            items[("refinement", review["id"], item["id"])] = (review, item)
    for review in page.get("ai_reviews", []):
        if review["status"] == "completed":
            for item in review["proposals"]:
                items[("ai", review["id"], item["id"])] = (review, item)
    return items


def evaluate_incremental_candidate(project, number, kind, review_id, proposal_id, *,
                                   diagnostic_width_consensus=False):
    """Read-only extra review of one new, unconfirmed image-OCR candidate.

    This opt-in API does not alter v1 saved checks, historical selection, raw
    OCR, AI proposals or human decisions. Width changes remain held by default:
    independent OCR agreement is diagnostic evidence, not original Unicode
    encoding proof. ``diagnostic_width_consensus=True`` exposes the frozen
    consensus rule only for explicit development comparisons, never approval.
    Han family consistency is only an extra veto and cannot authorize a glyph.
    Existing UI acceptance paths and stored v1 proof replay remain unchanged.
    """
    from types import SimpleNamespace

    from . import comparison_refinement_guarded as local_guard
    from .comparison_incremental import assess_width_recall
    from .comparison_incremental_observations import (
        frozen_raw,
        minimal_patch,
        observe_han,
        observe_punctuation,
    )
    from .comparison_refinement import _fresh, _pending

    if kind not in ("ai", "refinement"):
        raise core.ComparisonError("지원하지 않는 새 후보 종류입니다.")
    if type(diagnostic_width_consensus) is not bool:
        raise TypeError("diagnostic_width_consensus must be an explicit boolean")
    with project._lock:
        project.validate_source()
        if project.kind != "image":
            raise core.ComparisonError("이미지 OCR 프로젝트의 새 후보만 검사할 수 있습니다.")
        page = project.page(number)
        before_digest = core._digest(page)
        key = (kind, review_id, proposal_id)
        if key not in _items(page):
            raise core.ComparisonError("저장된 원래 후보가 없습니다.")
        review, item = _items(page)[key]
        snapshot, baseline, parents, _stage = _context(page, kind, review)
        if project.text(number) != snapshot or snapshot != baseline["raw"]["text"]:
            raise core.ComparisonError("원래 판독과 다른 사람 교정 문장은 변경하지 않습니다.")
        if kind == "ai":
            if project._ai_decision(page, review_id, proposal_id) != "pending" or project._ai_evidence_stale(page, review):
                raise core.ComparisonError("확정되었거나 오래된 AI 후보는 새 검사 대상이 아닙니다.")
            views = {(p["review_id"], p["id"]): p for p in project.ai_proposals(number, include_decided=True)}
            view = views.get((review_id, proposal_id))
            current = core._source_fidelity_view(page, review)
            verdict = next(p for p in current["proposals"] if p["proposal_id"] == proposal_id)
            original_ok = bool(view and view["applicable"] and not verdict["blocked"])
            prior_guard = {"source_fidelity_sha256": core._digest(current), "source_verdict": verdict}
        else:
            _fresh(project, page, review, item)
            if not _pending(page, review_id, proposal_id):
                raise core.ComparisonError("이미 확정된 지역 후보는 새 검사 대상이 아닙니다.")
            if "guard_evidence" not in item:
                raise core.ComparisonError("원래 독립 부호 보호 근거가 없는 후보입니다.")
            candidate = {**item, "id": item["guard_evidence"]["parent_proposal_id"]}
            verdict = local_guard._evidence(baseline, candidate, local_guard._views(baseline, parents,
                SimpleNamespace(max_input_chars=review["policy"]["max_input_chars"])))
            if verdict != item["guard_evidence"]:
                raise core.ComparisonError("원래 부호 보호 근거를 원시 판독에서 재현할 수 없습니다.")
            original_ok = not verdict["blocked"]
            prior_guard = {"local_guard_sha256": core._digest(verdict)}
        checked = _verdicts(page, [{"kind": kind, "review_id": review_id, "proposal_id": proposal_id}])[0]
        original_ok &= not checked["blocked"]
        prior_guard.update(passed=original_ok, image_guard_sha256=core._digest(checked))
        raw_page = {"runs": list(parents.values()), "baseline_run_id": baseline["id"]}
        batch, evidence, _, _ = frozen_raw(raw_page, snapshot)
        minimal = minimal_patch(item)
        diagnostic_width_verdict = None
        if any(core._han(c) for c in minimal["before"] + minimal["after"]):
            observed = observe_han(batch, item, evidence)
            verdict = observed["family_consistency"]
            category = "Han-family-hold-only"
        else:
            observed = observe_punctuation(batch, item, evidence)
            target = observed["target"] or minimal
            verdict = assess_width_recall(target["before"], target["after"], observed["observations"],
                baseline_engine=baseline["engine"], source_address_valid=observed["address_valid"],
                existing_guards_passed=original_ok)
            diagnostic_width_verdict = copy.deepcopy(verdict)
            if not diagnostic_width_consensus:
                verdict = {**verdict, "action": "hold_review_only",
                    "reason_codes": [*verdict["reason_codes"], "width_consensus_is_diagnostic_not_encoding_proof"],
                    "consensus_admission_disabled": True}
            category = "width-consensus-diagnostic"
        if core._digest(page) != before_digest:
            raise core.ComparisonError("추가 검사 중 원래 기록이 변경되었습니다.")
        return {"contract": "image-only-incremental-review-v3-safe-default", "kind": kind, "review_id": review_id,
                "proposal_id": proposal_id, "proposal_sha256": core._digest(item), "category": category,
                "observation": observed, "existing_guards": prior_guard, "verdict": verdict,
                "diagnostic_width_consensus": diagnostic_width_consensus,
                "diagnostic_width_verdict": diagnostic_width_verdict,
                "raw_AI_human_history_modified": False, "new_approval": False, "human_accepted": False,
                "native_pdf_text_used": False, "new_AI_or_OCR_calls": 0}


def _key(target):
    return target["kind"], target["review_id"], target["proposal_id"]


def _context(page, kind, review):
    from .comparison_refinement import _parent_map
    runs = {run["id"]: run for run in page["runs"]}
    if kind == "refinement":
        baseline, parents = _parent_map(review, runs)
        # A review captured active runs. Later replacement of an engine's run
        # does not change the meaning of that historical check.
        parents = {engine: {**run, "active": True} for engine, run in parents.items()}
        return review["snapshot_text"], parents[baseline["engine"]], parents, review["stage"]
    request = review["request"]
    baseline = runs.get(request["baseline_run_id"])
    if baseline is None:
        raise core.ComparisonError("이미지 재검사의 기준 판독이 없습니다.")
    parents = {}
    for parent in request["runs"]:
        run = runs.get(parent["run_id"])
        if (run is None or run["raw_sha256"] != parent["raw_sha256"]
                or run["engine"] != parent["engine"] or run["engine"] in parents):
            raise core.ComparisonError("이미지 재검사의 원시 판독 연결이 다릅니다.")
        parents[run["engine"]] = {**run, "active": True}
    return request["snapshot_text"], parents[baseline["engine"]], parents, "ai"


def _verdicts(page, targets):
    from .comparison_image_safety import assess
    items = _items(page)
    verdicts = []
    for target in targets:
        review, item = items[_key(target)]
        snapshot, baseline, parents, stage = _context(page, target["kind"], review)
        explicit_traditional = (target["kind"] == "ai"
            and review["request"].get("phase") == "editorial"
            and review["policy"].get("traditional") is True and item.get("kind") == "traditional")
        verdicts.append({**assess(snapshot, baseline, parents, item, stage=stage,
                                 allow_explicit_traditional=explicit_traditional), **target})
    return verdicts


def record_checks(project, number):
    """Append a reproducible check of pending proposals; never accept an edit."""
    from .comparison_refinement import _fresh, _pending
    with project._lock:
        project.validate_source()
        page = project._page(number)
        targets = []
        for (kind, review_id, proposal_id), (review, item) in _items(page).items():
            if kind == "refinement":
                if not _pending(page, review_id, proposal_id):
                    continue
                try:
                    _fresh(project, page, review, item)
                except core.ComparisonError:
                    continue
            else:
                if (project._ai_decision(page, review_id, proposal_id) != "pending"
                        or project._ai_evidence_stale(page, review)):
                    continue
                try:
                    a, b = project._mapped_span(review["request"]["snapshot_text"],
                                                project.text(number), item["start"], item["end"])
                    if project.text(number)[a:b] != item["before"]:
                        continue
                except core.ComparisonError:
                    continue
            targets.append({"kind": kind, "review_id": review_id, "proposal_id": proposal_id})
        if not targets:
            return None
        record = {"contract": CONTRACT, "source_sha256": project.source_sha256,
                  "page_number": number, "targets": targets,
                  "verdicts": _verdicts(page, targets), "native_pdf_text_used": False,
                  "new_AI_or_OCR_calls": 0}
        record["id"] = core._digest(record)
        record["created_at"] = core._now()
        previous = next((old for old in page.get("image_only_checks", []) if old["id"] == record["id"]), None)
        if previous is not None:
            return copy.deepcopy(previous)
        page.setdefault("image_only_checks", []).append(record)
        project._revision += 1
        return copy.deepcopy(record)


def evidence_for(page, kind, review_id, proposal_id):
    results = [{**copy.deepcopy(verdict), "image_check_id": record["id"]}
               for record in page.get("image_only_checks", []) for verdict in record["verdicts"]
               if _key(verdict) == (kind, review_id, proposal_id)]
    return next((value for value in reversed(results) if value["blocked"]), results[-1] if results else None)


def current_verdict(page, kind, review_id, proposal_id):
    """Read-only current protection for older files not explicitly rechecked."""
    proof = evidence_for(page, kind, review_id, proposal_id)
    if proof is not None:
        return proof
    target = {"kind": kind, "review_id": review_id, "proposal_id": proposal_id}
    if _key(target) not in _items(page):
        raise core.ComparisonError("이미지 OCR 재검사의 원래 제안이 없습니다.")
    return _verdicts(page, [target])[0]


def hold_reason(page, kind, review_id, proposal_id):
    proof = current_verdict(page, kind, review_id, proposal_id)
    if not proof or not proof["blocked"]:
        return ""
    from .comparison_image_safety import reason_text
    return "이미지 OCR 근거 보류: " + reason_text(proof["reason_codes"])


def captured_proof(page, kind, review_id, proposal_id, proof):
    record = next((r for r in page.get("image_only_checks", [])
                   if isinstance(proof, dict) and r["id"] == proof.get("image_check_id")), None)
    verdict = next((v for v in record["verdicts"] if _key(v) == (kind, review_id, proposal_id)), None) if record else None
    expected = {**copy.deepcopy(verdict), "image_check_id": record["id"]} if verdict else None
    if expected is None or proof != expected or expected["blocked"]:
        raise core.ComparisonError("당시 이미지 OCR 재검사 근거가 없거나 보류 상태입니다.")
    return expected


def validate_acceptance(page, kind, review_id, proposal_id, decision, edit):
    proof = decision.get("image_only_check")
    fingerprint = edit.get("image_only_check_sha256")
    if proof is None and fingerprint is None:
        return  # Historical user decisions retain their original meaning.
    if proof is None or fingerprint != core._digest(proof):
        raise core.ComparisonError("이미지 OCR 재검사와 사용자 확정 이력의 연결이 다릅니다.")
    captured_proof(page, kind, review_id, proposal_id, proof)


def validate_records(project, page):
    records = page.get("image_only_checks", [])
    if not isinstance(records, list):
        raise core.ComparisonError("이미지 OCR 재검사 이력은 목록이어야 합니다.")
    items, seen = _items(page), set()
    for record in records:
        if (not isinstance(record, dict) or record.get("contract") != CONTRACT
                or record.get("source_sha256") != project.source_sha256
                or record.get("page_number") != page["number"]
                or record.get("native_pdf_text_used") is not False
                or record.get("new_AI_or_OCR_calls") != 0
                or not isinstance(record.get("created_at"), str)
                or record.get("id") in seen
                or record.get("id") != core._digest({k: v for k, v in record.items() if k not in ("id", "created_at")})):
            raise core.ComparisonError("이미지 OCR 재검사 이력의 형식·지문이 다릅니다.")
        core._reject_ai_secrets(record)
        targets = record.get("targets")
        if not isinstance(targets, list) or not targets:
            raise core.ComparisonError("이미지 OCR 재검사 대상이 없습니다.")
        keys = set()
        for target in targets:
            if not isinstance(target, dict) or set(target) != {"kind", "review_id", "proposal_id"}:
                raise core.ComparisonError("이미지 OCR 재검사 대상 연결이 다릅니다.")
            key = _key(target)
            if key in keys or key not in items:
                raise core.ComparisonError("이미지 OCR 재검사 대상이 없거나 중복되었습니다.")
            keys.add(key)
        if record.get("verdicts") != _verdicts(page, targets):
            raise core.ComparisonError("원시 판독에서 이미지 OCR 재검사 근거를 재현할 수 없습니다.")
        seen.add(record["id"])


def _touching(first, second):
    return not (first["end"] < second["start"] or first["start"] > second["end"])


def _overlaps(first, second):
    a, b, c, d = first["start"], first["end"], second["start"], second["end"]
    if a == b:
        return c <= a <= d
    if c == d:
        return a <= c <= b
    return max(a, c) < min(b, d)


def evaluate_draft(project, number, *, existing_selected=()):
    """Build an unconfirmed image-only draft from authenticated proposals.

    Existing selection is an explicit review selection, not ground truth. Its
    members must match saved proposals exactly. Native PDF checks are excluded
    here even when the project also preserves a separate native PDF check.
    """
    from .comparison_refinement import (
        RefinementPolicy,
        _flatten,
        _fresh,
        _pending,
        build_candidates,
    )
    with project._lock:
        project.validate_source()
        page = project._page(number)
        baseline = project._baseline(page)
        if baseline is None or project.text(number) != baseline["raw"]["text"]:
            raise core.ComparisonError("초안 비교는 사람 교정 전의 동일한 기준 판독에서 실행하세요.")
        snapshot = project.text(number)
        locals_v3 = build_candidates(project, number,
            policy=RefinementPolicy(version="anchored-refinement-v3"), use_pdf_text=False)
        # New content candidates are in a separate review; old candidate IDs and
        # exact proposal objects remain available for provenance comparisons.
        windows = build_candidates(project, number,
            policy=RefinementPolicy(version="anchored-refinement-v4"), use_pdf_text=False)
        record = record_checks(project, number)
        page = project._page(number)
        saved = _items(page)
        selected, blocked, seen = [], [], set()
        ai_views = None
        for original in existing_selected:
            kind = "ai" if original.get("stage") == "ai" else "refinement"
            source_id = original.get("source_proposal_id", original.get("id")) if kind == "ai" else original.get("id")
            key = (kind, original.get("review_id"), source_id)
            if source_id != original.get("id") and original.get("id") != core._digest(
                    {"review_id": original.get("review_id"), "proposal_id": source_id}):
                raise core.ComparisonError("AI 초안 후보의 복합 ID와 원래 제안 연결이 다릅니다.")
            if key not in saved or original["id"] in seen:
                raise core.ComparisonError("기존 초안 후보가 저장된 제안과 연결되지 않습니다.")
            review, item = saved[key]
            reference_snapshot, reference_baseline, _, _ = _context(page, kind, review)
            if (reference_snapshot != snapshot or reference_baseline["id"] != baseline["id"]
                    or any(original.get(k) != item[k] for k in ("start", "end", "before", "after"))
                    or any(original[k] != value for k, value in item.items() if k in original and k != "id")
                    or any(k in original and original[k] != value for k, value in (
                        ("baseline_run_id", baseline["id"]), ("baseline_raw_sha256", baseline["raw_sha256"]),
                        ("snapshot_sha256", core._text_sha(snapshot)), ("source_sha256", project.source_sha256)))):
                raise core.ComparisonError("기존 초안 후보의 원문·수정 위치가 일치하지 않습니다.")
            reason = hold_reason(page, *key)
            if kind == "refinement":
                try:
                    _fresh(project, page, review, item)
                    if not _pending(page, review["id"], item["id"]):
                        reason = reason or "already-decided"
                except core.ComparisonError as exc:
                    reason = reason or str(exc)
            else:
                if ai_views is None:
                    ai_views = {(p["review_id"], p["id"]): p for p in project.ai_proposals(number, include_decided=True)}
                view = ai_views.get((review["id"], item["id"]))
                if view is None or not view["applicable"]:
                    reason = reason or (view["blocked_reason"] if view else "AI-proposal-unavailable")
            seen.add(original["id"])
            if reason:
                blocked.append({"id": original["id"], "reason": reason, "source": "existing"})
            else:
                selected.append(copy.deepcopy(original))
        # Preserve adjacent legacy substitutions; reject ambiguous insertion
        # boundaries as well as actual overlap, just as the old selector did.
        if any(_overlaps(p, q) for index, p in enumerate(selected) for q in selected[index + 1:]):
            raise core.ComparisonError("기존 초안의 교정 범위가 겹칩니다.")
        extras = []
        for entry in locals_v3 + windows:
            if entry["id"] in seen:
                continue
            seen.add(entry["id"])
            key = ("refinement", entry["review_id"], entry["id"])
            review, item = saved[key]
            reason = hold_reason(page, *key)
            try:
                _fresh(project, page, review, item)
                if not _pending(page, review["id"], item["id"]):
                    reason = reason or "already-decided"
            except core.ComparisonError as exc:
                reason = reason or str(exc)
            if any(_touching(entry, prior) for prior in selected):
                reason = reason or "touches-retained-existing-proposal"
            if reason:
                blocked.append({"id": entry["id"], "reason": reason, "source": "new-local"})
            else:
                extras.append(_flatten(review, item))
        # Prefer already established v3 candidates over equivalent window
        # suggestions. No label determines this deterministic precedence.
        retained = []
        for item in extras:
            if any(_touching(item, prior) for prior in retained):
                blocked.append({"id": item["id"], "reason": "touches-earlier-local-proposal", "source": "new-local"})
            else:
                retained.append(item)
        selected += retained
        text = snapshot
        for item in sorted(selected, key=lambda p: (p["start"], p["end"], p["id"]), reverse=True):
            if snapshot[item["start"]:item["end"]] != item["before"]:
                raise core.ComparisonError("초안의 변경 전 문자가 기준 판독과 다릅니다.")
            text = text[:item["start"]] + item["after"] + text[item["end"]:]
        return {"text": text, "selected": selected, "blocked": blocked,
                "new_local_selected_ids": [p["id"] for p in retained],
                "new_local_AI_approved": False, "image_only_check": record,
                "native_pdf_text_used": False, "human_accepted": 0,
                "generated_proposals": locals_v3 + windows}
