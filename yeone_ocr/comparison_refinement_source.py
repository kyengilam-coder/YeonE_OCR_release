"""Append-only source evidence holds, separate from OCR, AI and human edits.

This layer never creates replacement text. It can only hold an existing local
punctuation proposal whose original glyph metadata supports the baseline.
"""
from __future__ import annotations

import copy

from . import comparison as core

CONTRACT = "refinement-pdf-punctuation-check-v1"


def _items(page):
    from .comparison_refinement import _flatten
    return {(review["id"], item["id"]): _flatten(review, item)
            for review in page.get("refinement_reviews", [])
            if review["stage"] == "local" and review["policy"]["version"] == "anchored-refinement-v3"
            for item in review["proposals"] if item["kind"] == "punctuation"}


def _verdicts(capture, targets, items):
    from .comparison_pdf_punctuation import assess
    return [{**assess(capture, items[(target["review_id"], target["proposal_id"])]),
             "review_id": target["review_id"], "proposal_id": target["proposal_id"]}
            for target in targets]


def record_pdf_check(project, number, *, pdf_path=None, pdf_page=None, dpi=None,
                     crop=None, review_ids=None):
    """Verify an original PDF (or its pixel-identical crop); record no decision.

    Explicit crop provenance is required for an image project. The native PDF
    reader validates it against the source pixels; filenames are never guessed.
    """
    from .comparison_pdf_punctuation import capture
    from .comparison_refinement import _fresh, _pending
    with project._lock:
        project.validate_source()
        page = project._page(number)
        selected = set(review_ids) if review_ids is not None else None
        items = _items(page)
        reviews = {review["id"]: review for review in page.get("refinement_reviews", [])}
        targets = []
        for review_id, proposal_id in items:
            if (selected is not None and review_id not in selected) or not _pending(page, review_id, proposal_id):
                continue
            try:
                _fresh(project, page, reviews[review_id], items[(review_id, proposal_id)])
            except core.ComparisonError:
                continue
            targets.append({"review_id": review_id, "proposal_id": proposal_id})
        if not targets:
            return None
        captured = capture(project, number, pdf_path=pdf_path, pdf_page=pdf_page, dpi=dpi, crop=crop)
        project.validate_source()
        record = {"contract": CONTRACT, "source_sha256": project.source_sha256,
                  "page_number": number, "capture": captured, "targets": targets,
                  "verdicts": _verdicts(captured, targets, items)}
        record["id"] = core._digest(record)
        record["created_at"] = core._now()
        existing = page.get("refinement_source_checks", [])
        previous = next((item for item in existing if item["id"] == record["id"]), None)
        if previous is not None:
            return copy.deepcopy(previous)
        page.setdefault("refinement_source_checks", []).append(record)
        project._revision += 1
        return copy.deepcopy(record)


def evidence_for(page, review_id, proposal_id):
    """Holds are monotonic: an unavailable later reading cannot erase one."""
    results = [{**copy.deepcopy(verdict), "source_check_id": record["id"]}
               for record in page.get("refinement_source_checks", [])
               for verdict in record["verdicts"]
               if verdict["review_id"] == review_id and verdict["proposal_id"] == proposal_id]
    return next((item for item in reversed(results) if item.get("blocked")), results[-1] if results else None)


def hold_reason(page, review_id, proposal_id):
    evidence = evidence_for(page, review_id, proposal_id)
    if evidence and evidence.get("blocked"):
        return "원본 PDF의 표시 문자 정보가 변경 전 부호를 지지하여 보류했습니다. 원본을 확인하세요."
    return ""


def validate_records(project, page):
    """Replay stored evidence without reopening an external PDF on load."""
    from .comparison_pdf_punctuation import _word_regions, validate_capture
    records = page.get("refinement_source_checks", [])
    if not isinstance(records, list):
        raise core.ComparisonError("PDF 부호 확인 이력은 목록이어야 합니다.")
    items, seen = _items(page), set()
    for record in records:
        if (not isinstance(record, dict) or record.get("contract") != CONTRACT
                or record.get("source_sha256") != project.source_sha256
                or record.get("page_number") != page["number"]
                or not isinstance(record.get("created_at"), str)
                or record.get("id") in seen
                or record.get("id") != core._digest({k: v for k, v in record.items()
                                                    if k not in ("id", "created_at")})):
            raise core.ComparisonError("PDF 부호 확인 이력의 형식·원본 지문이 일치하지 않습니다.")
        core._reject_ai_secrets(record)
        validate_capture(record["capture"], project.source_sha256, page["number"])
        captured = record["capture"]
        if captured["status"] == "available":
            runs = {run["id"]: run for run in page["runs"]}
            baseline = runs.get(captured["baseline_run_id"])
            if baseline is None or baseline["raw_sha256"] != captured["baseline_raw_sha256"]:
                raise core.ComparisonError("PDF 부호 확인의 기준 원시 판독이 일치하지 않습니다.")
            states = [baseline["raw"]["text"]] + [edit["after"] for edit in page["edits"]]
            if captured["snapshot_text"] not in states:
                raise core.ComparisonError("PDF 부호 확인 당시의 교정문이 이력에 없습니다.")
            region_ids = set()
            for region in captured["ocr_regions"]:
                run = runs.get(region["run_id"])
                if (run is None or run["id"] in region_ids or _word_regions(run) != region
                        or run["raw"]["image_width"] != captured["image_width"]
                        or run["raw"]["image_height"] != captured["image_height"]):
                    raise core.ComparisonError("PDF 부호 확인의 실제 OCR 영역이 원시 기록과 다릅니다.")
                region_ids.add(run["id"])
        targets = record.get("targets")
        if not isinstance(targets, list) or not targets:
            raise core.ComparisonError("PDF 부호 확인 대상이 없습니다.")
        keys = []
        for target in targets:
            if not isinstance(target, dict) or set(target) != {"review_id", "proposal_id"}:
                raise core.ComparisonError("PDF 부호 확인 대상 연결이 올바르지 않습니다.")
            key = (target["review_id"], target["proposal_id"])
            if key not in items or key in keys:
                raise core.ComparisonError("PDF 부호 확인 대상이 없거나 중복되었습니다.")
            keys.append(key)
        if record.get("verdicts") != _verdicts(record["capture"], targets, items):
            raise core.ComparisonError("보존한 원본 PDF 정보에서 부호 보류 근거를 재현할 수 없습니다.")
        seen.add(record["id"])
