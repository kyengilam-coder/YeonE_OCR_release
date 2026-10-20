"""Conservative local suggestions and separately retained region recognitions.

No truth/reference text is read here. A suggestion is not a corrected document:
exact Unicode anchors and real OCR boxes are evidence for human review only.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import copy
import difflib
import hashlib
import io
import math
from pathlib import Path
import tempfile
import unicodedata
import uuid

from ab_ocr import ocr
from ab_ocr.engine_registry import ENGINES, language_for
from . import comparison as core


@dataclass(frozen=True)
class RefinementPolicy:
    version: str = "anchored-refinement-v1"
    anchor_chars: int = 4
    han_max_chars: int = 4
    punctuation_max_chars: int = 4
    region_max_chars: int = 256
    max_candidates: int = 2000
    max_input_chars: int = 20000

    def __post_init__(self):
        if self.version not in ("anchored-refinement-v1", "anchored-refinement-v2", "anchored-refinement-v3", "anchored-refinement-v4"):
            raise core.ComparisonError("지원하지 않는 교정 후보 정책입니다.")
        for name, lower, upper in (("anchor_chars", 4, 32), ("han_max_chars", 1, 16),
                                   ("punctuation_max_chars", 1, 16), ("region_max_chars", 1, 1024),
                                   ("max_candidates", 1, 10000), ("max_input_chars", 100, 50000)):
            if type(getattr(self, name)) is not int or not lower <= getattr(self, name) <= upper:
                raise core.ComparisonError("교정 후보 정책의 범위를 확인하세요.")


def _policy(value=None):
    if value is None:
        return RefinementPolicy()
    if not isinstance(value, RefinementPolicy):
        raise core.ComparisonError("교정 후보 정책 객체가 필요합니다.")
    return value


def _locate(text, left, right, maximum):
    """Both adjacent anchors must occur exactly once, without normalization."""
    if not left or not right or text.count(left) != 1 or text.count(right) != 1:
        return None
    start, end = text.index(left) + len(left), text.index(right)
    return (start, end) if 0 <= end - start <= maximum else None


def _box(raw, start, end):
    """Use whole real word/line/block boxes, never interpolated glyph boxes."""
    boxes, covered = [], set()
    for word in raw.get("words", []):
        text = word.get("text", "")
        if not text or raw["text"].count(text) != 1:
            continue
        left = raw["text"].index(text)
        right = left + len(text)
        if left < end and right > start:
            boxes.append(word["bbox"])
            covered.update(range(max(start, left), min(end, right)))
    required = {i for i in range(start, end) if not raw["text"][i].isspace()}
    if not boxes or not required.issubset(covered):
        return None
    return [min(b[0] for b in boxes), min(b[1] for b in boxes),
            max(b[2] for b in boxes), max(b[3] for b in boxes)]


def _witness(runs, left, right, maximum, *, preferred=(), only=None):
    order = tuple(dict.fromkeys((*preferred, *ENGINES))) if only is None else (only,)
    for engine in order:
        if engine == "glmocr":
            continue
        run = runs.get(engine)
        if run is None:
            continue
        raw = run["raw"]
        span = _locate(raw["text"], left, right, maximum)
        if span is None:
            continue
        start, end = span
        box = _box(raw, start - len(left), end + len(right))
        if box is not None:
            return {"run_id": run["id"], "engine": engine, "raw_sha256": run["raw_sha256"],
                    "start": start, "end": end, "context_start": start - len(left),
                    "context_end": end + len(right), "bbox": box,
                    "granularity": raw.get("engine_metadata", {}).get("bbox_granularity", "engine-region")}
    return None


def _anchors(baseline, other, start, end, source_start, source_end, policy):
    n = policy.anchor_chars
    if min(start, source_start) < n or min(len(baseline) - end, len(other) - source_end) < n:
        return None
    left, right = baseline[start - n:start], baseline[end:end + n]
    if not left.strip() or not right.strip():
        return None
    if other[source_start - n:source_start] != left or other[source_end:source_end + n] != right:
        return None
    if _locate(baseline, left, right, max(policy.han_max_chars, policy.punctuation_max_chars)) != (start, end):
        return None
    if _locate(other, left, right, max(policy.han_max_chars, policy.punctuation_max_chars)) != (source_start, source_end):
        return None
    return {"left": left, "right": right}


def _current_span(snapshot, current, start, end, anchors):
    left, right = core.ComparisonProject._mapped_span(snapshot, current, start, end)
    a, b = anchors["left"], anchors["right"]
    if (current[left - len(a):left] != a or current[right:right + len(b)] != b
            or _locate(current, a, b, max(end - start, 1)) != (left, right)):
        raise core.ComparisonError("교정 위치 또는 주변 원문이 변경되었습니다.")
    return left, right


def _local_candidates_v1(baseline, runs, snapshot, policy):
    if core._run_profile(baseline)[1] != "horizontal":
        return []
    text = baseline["raw"]["text"]
    if max(len(text), len(snapshot)) > policy.max_input_chars:
        return []
    result = []
    for engine, kind in (("glmocr", "han"), ("easyocr", "punctuation")):
        source = runs.get(engine)
        if source is None or source["id"] == baseline["id"] or not core._compatible(baseline, source):
            continue
        other = source["raw"]["text"]
        if len(other) > policy.max_input_chars:
            continue
        for tag, i, j, a, b in difflib.SequenceMatcher(None, text, other, autojunk=False).get_opcodes():
            if tag == "equal":
                continue
            before, after = text[i:j], other[a:b]
            if kind == "han":
                eligible = (tag == "replace" and 0 < len(before) == len(after) <= policy.han_max_chars
                            and all(core._han(c) for c in before + after))
            else:
                eligible = (0 < max(len(before), len(after)) <= policy.punctuation_max_chars
                            and all(unicodedata.category(c).startswith("P") for c in before + after))
            if not eligible or not core._eligibility(source, after, before)[0]:
                continue
            anchors = _anchors(text, other, i, j, a, b, policy)
            if anchors is None:
                continue
            try:
                start, end = _current_span(text, snapshot, i, j, anchors)
            except core.ComparisonError:
                continue
            witness = _witness(runs, **anchors, maximum=max(policy.han_max_chars, policy.punctuation_max_chars),
                               preferred=(engine, baseline["engine"]))
            if witness is None:
                continue
            item = {"kind": kind, "engine": engine, "start": start, "end": end,
                    "before": before, "after": after, "baseline_start": i, "baseline_end": j,
                    "source_run_id": source["id"], "source_raw_sha256": source["raw_sha256"],
                    "source_start": a, "source_end": b, "anchors": anchors, "evidence": witness}
            item["id"] = core._digest(item)
            result.append(item)
    return sorted(result, key=lambda p: (p["start"], p["end"], p["engine"], p["id"]))[:policy.max_candidates]


def _local_candidates(baseline, runs, snapshot, policy):
    if policy.version == "anchored-refinement-v1":
        return _local_candidates_v1(baseline, runs, snapshot, policy)
    if policy.version == "anchored-refinement-v4":
        from .comparison_refinement_windows import local_candidates as window_candidates
        established = _local_candidates(baseline, runs, snapshot,
            RefinementPolicy(**{**asdict(policy), "version": "anchored-refinement-v3"}))
        spans = {(p["start"], p["end"], p["before"], p["after"]) for p in established}
        return [p for p in window_candidates(baseline, runs, snapshot, policy)
                if (p["start"], p["end"], p["before"], p["after"]) not in spans]
    from .comparison_refinement_v2 import local_candidates as punctuation_candidates
    from .comparison_refinement_consensus import local_candidates as consensus_candidates
    from .comparison_fidelity import _paired_width_conflict
    if policy.version == "anchored-refinement-v3":
        from .comparison_refinement_guarded import local_candidates as punctuation_candidates
    items = punctuation_candidates(baseline, runs, snapshot, policy)
    items = [item for item in items if _paired_width_conflict(snapshot, item) is None]
    content = consensus_candidates(baseline, runs, snapshot, policy)
    if policy.version == "anchored-refinement-v3":
        retained = []
        for item in content:
            minimum = 3 if item["kind"] == "han" else 2
            if len(item["consensus_evidence"]["families"]) < minimum:
                continue
            item = copy.deepcopy(item)
            item["consensus_evidence"]["minimum_independent_families"] = minimum
            item["id"] = core._digest({key: value for key, value in item.items() if key != "id"})
            retained.append(item)
        content = retained
    items += content
    return sorted(items, key=lambda p: (p["start"], p["end"], p["engine"], p["id"]))[:policy.max_candidates]


def _parents(runs):
    return [{"id": run["id"], "engine": engine, "raw_sha256": run["raw_sha256"],
             "request_sha256": core._digest(run.get("request", {}))}
            for engine in ENGINES if (run := runs.get(engine)) is not None]


def _make_review(project, number, policy, stage, items, runs, *, region_ids=()):
    page = project._page(number)
    baseline = project._baseline(page)
    snapshot = project.text(number)
    review = {"stage": stage, "source_sha256": project.source_sha256, "page_number": number,
              "baseline_run_id": baseline["id"], "baseline_raw_sha256": baseline["raw_sha256"],
              "snapshot_text": snapshot, "snapshot_sha256": core._text_sha(snapshot),
              "snapshot_edit_id": page["edits"][-1]["id"] if page["edits"] else None,
              "policy": asdict(policy), "policy_sha256": core._digest(asdict(policy)),
              "parent_runs": _parents(runs), "region_ids": list(region_ids), "proposals": items}
    review["id"] = core._digest(review)
    review["created_at"] = core._now()
    existing = page.setdefault("refinement_reviews", [])
    previous = next((r for r in existing if r["id"] == review["id"]), None)
    if previous is None:
        existing.append(review)
        page.setdefault("refinement_decisions", [])
        project._revision += 1
    return previous or review


def build_candidates(project, number, *, policy=None, use_pdf_text=True):
    """Record proposals only; never splice text, run models, or consult labels."""
    policy = _policy(policy)
    with project._lock:
        project.validate_source()
        page = project._page(number)
        runs = core.usable_runs(page)
        baseline = project._baseline(page)
        if baseline is None or baseline["engine"] not in runs:
            return []
        items = _local_candidates(baseline, runs, project.text(number), policy)
        review = _make_review(project, number, policy, "local", items, runs)
        if use_pdf_text and policy.version == "anchored-refinement-v3" and project.source_path.suffix.lower() == ".pdf":
            from .comparison_refinement_source import record_pdf_check
            record_pdf_check(project, number, review_ids=[review["id"]])
        return [_flatten(review, p) for p in review["proposals"]]


def _flatten(review, item):
    return {**copy.deepcopy(item), "review_id": review["id"], "stage": review["stage"],
            "source_sha256": review["source_sha256"], "page_number": review["page_number"],
            "baseline_run_id": review["baseline_run_id"], "baseline_raw_sha256": review["baseline_raw_sha256"],
            "snapshot_sha256": review["snapshot_sha256"], "policy_sha256": review["policy_sha256"]}


def _pending(page, review_id, proposal_id):
    decision = next((d for d in reversed(page.get("refinement_decisions", []))
                     if d["review_id"] == review_id and d["proposal_id"] == proposal_id), None)
    return decision is None or decision["decision"] == "undone"


def _fresh(project, page, review, item):
    if page.get("baseline_run_id") != review["baseline_run_id"]:
        raise core.ComparisonError("기준 판독이 바뀌어 교정 후보를 다시 만들어야 합니다.")
    latest = core.usable_runs(page)
    for parent in review["parent_runs"]:
        run = latest.get(parent["engine"])
        if run is None or run["id"] != parent["id"] or run["raw_sha256"] != parent["raw_sha256"]:
            raise core.ComparisonError("교정 후보의 부모 판독이 변경되었습니다.")
    start, end = _current_span(review["snapshot_text"], project.text(page["number"]),
                               item["start"], item["end"], item["anchors"])
    if project.text(page["number"])[start:end] != item["before"]:
        raise core.ComparisonError("이미 수정한 교정 위치는 덮어쓸 수 없습니다.")
    return start, end


def proposals(project, number, *, include_decided=False):
    from .comparison_refinement_source import evidence_for, hold_reason
    from .comparison_image_only import evidence_for as image_evidence, hold_reason as image_hold
    with project._lock:
        page = project.page(number)
        result = []
        for review in page.get("refinement_reviews", []):
            for item in review["proposals"]:
                pending = _pending(page, review["id"], item["id"])
                if not pending and not include_decided:
                    continue
                entry = _flatten(review, item)
                entry.update(pending=pending, eligible=False, blocked_reason="")
                try:
                    start, end = _fresh(project, page, review, item)
                    entry.update(eligible=pending, current_start=start, current_end=end)
                except core.ComparisonError as exc:
                    entry["blocked_reason"] = str(exc)
                proof = evidence_for(page, review["id"], item["id"])
                if proof is not None:
                    entry["source_check"] = proof
                reason = hold_reason(page, review["id"], item["id"])
                if reason:
                    entry.update(eligible=False, blocked_reason=reason)
                image_proof = image_evidence(page, "refinement", review["id"], item["id"])
                if image_proof is not None:
                    entry["image_only_check"] = image_proof
                reason = image_hold(page, "refinement", review["id"], item["id"])
                if pending and reason:
                    entry.update(eligible=False, blocked_reason=reason)
                    if image_proof is None:
                        from .comparison_image_only import current_verdict
                        entry["image_only_current"] = current_verdict(page, "refinement", review["id"], item["id"])
                result.append(entry)
        return result


def evaluate_proposals(project, number, proposal_ids=None):
    """Pure draft simulation. It creates no human/AI decisions or edits."""
    with project._lock:
        project.validate_source()
        current = project.text(number)
        selected = set(proposal_ids) if proposal_ids is not None else None
        candidates = [p for p in proposals(project, number) if selected is None or p["id"] in selected]
        blocked, eligible, seen = [], [], set()
        for item in sorted(candidates, key=lambda p: (p["current_start"] if p["eligible"] else p["start"], p["id"])):
            if item["id"] in seen:
                continue
            seen.add(item["id"])
            if not item["eligible"]:
                blocked.append({"id": item["id"], "reason": item["blocked_reason"] or "not-pending"})
                continue
            a, b = item["current_start"], item["current_end"]
            if current[a:b] != item["before"]:
                blocked.append({"id": item["id"], "reason": "before-mismatch"})
                continue
            eligible.append(item)
        conflicts = set()
        for index, item in enumerate(eligible):
            for other in eligible[index + 1:]:
                if not (item["current_end"] < other["current_start"] or item["current_start"] > other["current_end"]):
                    conflicts.update((item["id"], other["id"]))
        blocked.extend({"id": p["id"], "reason": "overlapping-proposals"} for p in eligible if p["id"] in conflicts)
        accepted = [p for p in eligible if p["id"] not in conflicts]
        draft = current
        for item in sorted(accepted, key=lambda p: p["current_start"], reverse=True):
            a, b = item["current_start"], item["current_end"]
            draft = draft[:a] + item["after"] + draft[b:]
        if selected is not None:
            blocked.extend({"id": p, "reason": "unknown-or-decided-proposal"} for p in sorted(selected - seen))
        return {"text": draft, "snapshot_sha256": core._text_sha(current),
                "applied": [p["id"] for p in accepted], "blocked": blocked}


def _find(page, review_id, proposal_id):
    review = next((r for r in page.get("refinement_reviews", []) if r["id"] == review_id), None)
    item = next((p for p in review["proposals"] if p["id"] == proposal_id), None) if review else None
    if item is None or not _pending(page, review_id, proposal_id):
        raise core.ComparisonError("처리할 수 있는 재검증 교정 후보가 없습니다.")
    return review, item


def accept_proposal(project, number, review_id, proposal_id):
    from .comparison_refinement_source import hold_reason
    from .comparison_image_only import evidence_for as image_evidence, hold_reason as image_hold
    with project._lock:
        project.validate_source()
        page = project._page(number)
        review, item = _find(page, review_id, proposal_id)
        start, end = _fresh(project, page, review, item)
        reason = hold_reason(page, review_id, proposal_id) or image_hold(page, "refinement", review_id, proposal_id)
        if reason:
            raise core.ComparisonError(reason)
        from .comparison_image_only import record_checks
        record_checks(project, number)
        current = project.text(number)
        replacement = current[:start] + item["after"] + current[end:]
        project._set_text(number, replacement, "anchored-local-refinement", kind="refinement_accept",
                          extra={"refinement_review_id": review_id, "refinement_proposal_id": proposal_id,
                                 "applied_start": start, "applied_end": end})
        page.setdefault("refinement_decisions", []).append({"id": uuid.uuid4().hex, "at": core._now(),
            "review_id": review_id, "proposal_id": proposal_id, "decision": "accepted",
            "edit_id": page["edits"][-1]["id"]})
        image_proof = image_evidence(page, "refinement", review_id, proposal_id)
        if image_proof is not None:
            page["refinement_decisions"][-1]["image_only_check"] = image_proof
            page["edits"][-1]["image_only_check_sha256"] = core._digest(image_proof)
        return replacement


def reject_proposal(project, number, review_id, proposal_id):
    with project._lock:
        project.validate_source()
        page = project._page(number)
        _find(page, review_id, proposal_id)
        page.setdefault("refinement_decisions", []).append({"id": uuid.uuid4().hex, "at": core._now(),
            "review_id": review_id, "proposal_id": proposal_id, "decision": "rejected"})
        project._revision += 1


def record_undo(page, edit, undo):
    page.setdefault("refinement_decisions", []).append({"id": uuid.uuid4().hex, "at": core._now(),
        "review_id": edit["refinement_review_id"], "proposal_id": edit["refinement_proposal_id"],
        "decision": "undone", "edit_id": edit["id"], "undo_edit_id": undo["id"]})


def _image_proof(project, number, dpi, bbox=None, should_stop=None):
    image = ocr.render_page(project.source_path, number, dpi=dpi, should_stop=should_stop)
    try:
        rgb = image.convert("RGB")
        try:
            proof = {"image_width": rgb.width, "image_height": rgb.height,
                     "page_pixel_sha256": hashlib.sha256(rgb.tobytes()).hexdigest()}
            if bbox is not None:
                crop = rgb.crop(tuple(bbox))
                try:
                    output = io.BytesIO();crop.save(output, format="PNG")
                    data = output.getvalue()
                    proof.update(crop_pixel_sha256=hashlib.sha256(crop.tobytes()).hexdigest(),
                                 crop_png_sha256=hashlib.sha256(data).hexdigest())
                finally:
                    crop.close()
            else:
                data = None
            return proof, data
        finally:
            rgb.close()
    finally:
        image.close()


def region_targets(project, number, *, limit=2, policy=None):
    """Recommend bounded difficult spans, without rendering or running OCR.

    Fixed priority: supported Han/Hangul omission/insertion disagreements, then
    more disagreeing eligible engines, then original text order. Votes schedule
    a review only; they never determine the replacement text.
    """
    policy = _policy(policy)
    if type(limit) is not int or not 0 <= limit <= 8:
        raise core.ComparisonError("자동 영역 개수는 0~8개로 선택하세요.")
    with project._lock:
        project.validate_source();page = project.page(number)
        runs = core.usable_runs(page);baseline = project._baseline(page)
        snapshot = project.text(number)
        if (limit == 0 or baseline is None or baseline["engine"] not in runs
                or core._run_profile(baseline)[1] != "horizontal"):
            return []
        text = baseline["raw"]["text"]
        if max(len(text), len(snapshot)) > policy.max_input_chars:
            return []
        differences = {}
        for engine, run in runs.items():
            if run["id"] == baseline["id"] or len(run["raw"]["text"]) > policy.max_input_chars:
                continue
            other = run["raw"]["text"]
            for tag, i, j, a, b in difflib.SequenceMatcher(None, text, other, autojunk=False).get_opcodes():
                if tag == "equal" or j - i > policy.region_max_chars or b - a > policy.region_max_chars:
                    continue
                before, after = text[i:j], other[a:b]
                if not any(core._han(c) or core._hangul(c) for c in before + after):
                    continue
                if not core._eligibility(run, after, before)[0]:
                    continue
                n = policy.anchor_chars
                if i < n or len(text) - j < n:
                    continue
                anchors = {"left": text[i - n:i], "right": text[j:j + n]}
                if _locate(text, **anchors, maximum=policy.region_max_chars) != (i, j):
                    continue
                try:
                    start, end = _current_span(text, snapshot, i, j, anchors)
                except core.ComparisonError:
                    continue
                witness = _witness(runs, **anchors, maximum=policy.region_max_chars,
                                   preferred=(baseline["engine"], engine))
                if witness is None:
                    continue
                key = (i, j)
                if key not in differences:
                    differences[key] = {"version": "han-hangul-disagreement-anchors-v1",
                        "source_sha256": project.source_sha256, "page_number": number,
                        "baseline_run_id": baseline["id"], "baseline_raw_sha256": baseline["raw_sha256"],
                        "snapshot_sha256": core._text_sha(snapshot), "start": start, "end": end,
                        "baseline_start": i, "baseline_end": j, "before": before, "anchors": anchors,
                        "evidence": witness, "engines": [], "omission_or_insertion": False}
                target = differences[key]
                target["engines"].append(engine)
                target["omission_or_insertion"] = target["omission_or_insertion"] or not before or not after
        selected = []
        for target in sorted(differences.values(), key=lambda p: (not p["omission_or_insertion"],
                             -len(p["engines"]), p["start"], p["end"])):
            if any(not (target["end"] < old["start"] or target["start"] > old["end"]) for old in selected):
                continue
            target["id"] = core._digest(target)
            selected.append(target)
            if len(selected) == limit:
                break
        return copy.deepcopy(selected)


def _condition_snapshot(engine, languages, engine_root, device, should_stop):
    from ab_ocr.local_engines import RecognitionSession
    with RecognitionSession(engine, languages, engine_root=engine_root, device=device,
                            should_stop=should_stop) as session:
        return session.condition_snapshot()


def _validate_conditions(snapshot, engine, languages, engine_root, device=None):
    from ab_ocr.runtime import default_engine_root
    expected_root = str(default_engine_root(engine_root).resolve())
    if (not isinstance(snapshot, dict) or set(snapshot) != {"engine", "languages", "engine_root", "device", "pin"}
            or snapshot.get("engine") != engine or snapshot.get("languages") != languages
            or snapshot.get("engine_root") != expected_root or snapshot.get("device") not in ("cpu", "cuda")
            or (device is not None and snapshot["device"] != device)):
        raise core.ComparisonError("영역 계획과 실제 세션의 엔진 폴더·언어·장치가 다릅니다.")
    pin = snapshot.get("pin")
    if (not isinstance(pin, dict) or pin.get("device") != snapshot["device"]
            or pin.get("languages") != languages or not isinstance(pin.get("models"), dict) or not pin["models"]):
        raise core.ComparisonError("영역 판독 모델·실행 조건 지문이 없습니다.")
    hashes = ("executable_sha256",) if engine == "tesseract" else ("installation", "worker", "protocol")
    if any(not isinstance(pin.get(k), str) or not core._HEX.fullmatch(pin[k]) for k in hashes):
        raise core.ComparisonError("영역 실행 파일·설치·프로토콜 지문이 올바르지 않습니다.")
    if engine != "tesseract" and (not isinstance(pin.get("runtime"), dict) or not pin["runtime"]):
        raise core.ComparisonError("영역 실행 환경의 고정 지문이 없습니다.")
    core._reject_ai_secrets(snapshot)
    return core._json_copy(snapshot)


def plan_region(project, number, start, end, engine, *, anchor_engine=None, padding=12,
                dpi=None, engine_root=None, policy=None, should_stop=None, conditions=None, device=None):
    """Freeze an image crop justified by real neighbouring OCR boxes.

    Offsets refer to the current corrected text. A GLM-only page has no usable
    box and is rejected instead of estimating glyph locations.
    """
    policy = _policy(policy)
    if engine not in ENGINES or (anchor_engine is not None and anchor_engine not in ENGINES):
        raise core.ComparisonError("지원하는 영역 판독 엔진을 선택하세요.")
    if type(padding) is not int or not 0 <= padding <= 256:
        raise core.ComparisonError("영역 여백은 0~256 pixel이어야 합니다.")
    if device not in (None, "cpu", "cuda"):
        raise core.ComparisonError("영역 판독 장치는 cpu 또는 cuda로 명시하세요.")
    with project._lock:
        project.validate_source()
        page = project._page(number);runs = core.usable_runs(page);baseline = project._baseline(page)
        snapshot = project.text(number)
        if (baseline is None or baseline["engine"] not in runs or type(start) is not int or type(end) is not int
                or not 0 <= start <= end <= len(snapshot) or end - start > policy.region_max_chars):
            raise core.ComparisonError("같은 조건의 기준 판독과 짧은 영역을 선택하세요.")
        if core._run_profile(baseline)[1] != "horizontal":
            raise core.ComparisonError("세로쓰기 영역 재판독은 아직 검증하지 않았습니다.")
        try:
            raw_start, raw_end = core.ComparisonProject._mapped_span(snapshot, baseline["raw"]["text"], start, end)
        except core.ComparisonError as exc:
            raise core.ComparisonError("수동 수정한 영역 대신 아직 바꾸지 않은 원문을 선택하세요.") from exc
        n = policy.anchor_chars
        text = baseline["raw"]["text"]
        if raw_start < n or len(text) - raw_end < n:
            raise core.ComparisonError("영역 양쪽의 확인 가능한 원문 anchor가 부족합니다.")
        anchors = {"left": text[raw_start - n:raw_start], "right": text[raw_end:raw_end + n]}
        if (_locate(text, **anchors, maximum=policy.region_max_chars) != (raw_start, raw_end)
                or _locate(snapshot, **anchors, maximum=policy.region_max_chars) != (start, end)):
            raise core.ComparisonError("반복되거나 수정된 원문 anchor로 영역을 결정할 수 없습니다.")
        witness = _witness(runs, **anchors, maximum=policy.region_max_chars,
                           preferred=(baseline["engine"],), only=anchor_engine)
        if witness is None:
            raise core.ComparisonError("확인된 다른 판독의 실제 영역 좌표가 필요합니다.")
        raw = baseline["raw"];width, height = raw["image_width"], raw["image_height"]
        box = witness["bbox"]
        bbox = [max(0, math.floor(box[0]) - padding), max(0, math.floor(box[1]) - padding),
                min(width, math.ceil(box[2]) + padding), min(height, math.ceil(box[3]) + padding)]
        recorded_dpi = baseline.get("request", {}).get("dpi") or raw.get("preprocessing", {}).get("render_dpi") or 300
        if dpi is None:
            dpi = recorded_dpi
        if type(dpi) is not int or not 72 <= dpi <= 600 or dpi != recorded_dpi:
            raise core.ComparisonError("부모 판독과 같은 렌더링 DPI를 사용하세요.")
        profile, direction = core._run_profile(baseline)
        languages = language_for(engine, profile, direction)
        execution = _validate_conditions(conditions if conditions is not None else
            _condition_snapshot(engine, languages, engine_root, device, should_stop), engine, languages, engine_root, device)
        proof, _ = _image_proof(project, number, dpi, bbox, should_stop)
        project.validate_source()
        if (proof["image_width"], proof["image_height"]) != (width, height):
            raise core.ComparisonError("원본 렌더링 크기가 부모 판독과 다릅니다.")
        conditions = {"engine": engine, "languages": languages,
                      "profile": profile, "direction": direction, "dpi": dpi,
                      "psm": 6 if engine == "tesseract" else None,
                      "engine_root": execution["engine_root"], "device": execution["device"], "pin": execution["pin"]}
        plan = {"version": "anchored-region-v1", "source_sha256": project.source_sha256, "page_number": number,
                "baseline_run_id": baseline["id"], "baseline_raw_sha256": baseline["raw_sha256"],
                "snapshot_text": snapshot, "snapshot_sha256": core._text_sha(snapshot),
                "snapshot_edit_id": page["edits"][-1]["id"] if page["edits"] else None,
                "start": start, "end": end, "before": snapshot[start:end],
                "baseline_start": raw_start, "baseline_end": raw_end, "anchors": anchors,
                "evidence": witness, "parent_runs": _parents(runs), "bbox": bbox, "padding": padding,
                "render_dpi": dpi, "render_policy": "ocr.render_page-RGB-v1", **proof,
                "engine_conditions": conditions, "policy": asdict(policy),
                "transform": {"scale_x": 1, "scale_y": 1, "offset_x": bbox[0], "offset_y": bbox[1]}}
        plan["id"] = core._digest(plan)
        return plan


def _plan_fresh(project, page, plan):
    proxy = {**plan, "proposals": []}
    return _fresh(project, page, proxy, plan)


def run_region(project, plan, *, session=None, should_stop=None, on_result=None):
    """Preserve a completed/incomplete/failed/cancelled crop in its own array.

    The caller checkpoints via on_result. A supplied recognition session owns
    the GPU lease and server; this function never acquires another lease.
    """
    plan = core._json_copy(plan)
    number = plan.get("page_number")
    project._check_page(number)
    with project._lock:
        project.validate_source();page = project._page(number)
        _validate_plan(project, page, plan, {r["id"]: r for r in page["runs"]})
        _plan_fresh(project, page, plan)
    if session is None:
        from ab_ocr.local_engines import RecognitionSession
        conditions = plan["engine_conditions"]
        with RecognitionSession(conditions["engine"], conditions["languages"],
                engine_root=conditions["engine_root"], device=conditions["device"],
                should_stop=should_stop, psm=conditions["psm"]) as owned:
            return run_region(project, plan, session=owned, should_stop=should_stop, on_result=on_result)
    record = {"id": uuid.uuid4().hex, "created_at": core._now(), "plan": plan,
              "plan_sha256": core._digest(plan), "status": "failed", "raw": None, "raw_sha256": None,
              "evidence_stale_on_record": False}
    try:
        if should_stop and should_stop():
            raise ocr.OCRCancelled("영역 판독을 중지했습니다.")
        proof, png = _image_proof(project, number, plan["render_dpi"], plan["bbox"], should_stop)
        if any(proof[k] != plan[k] for k in proof):
            raise core.ComparisonError("영역 이미지 지문이 계획과 다릅니다.")
        conditions = plan["engine_conditions"]
        with tempfile.TemporaryDirectory(prefix="yeone-region-") as temporary:
            input_path = Path(temporary) / "region.png";input_path.write_bytes(png)
            if getattr(session, "engine", None) != conditions["engine"] or getattr(session, "languages", None) != conditions["languages"]:
                raise core.ComparisonError("영역 요청과 재사용 세션의 엔진·언어가 다릅니다.")
            frozen = getattr(session, "_refinement_verified_conditions", None)
            if frozen is None:
                frozen = _validate_conditions(session.condition_snapshot(), conditions["engine"], conditions["languages"],
                                                conditions["engine_root"], conditions["device"])
                session._refinement_verified_conditions = frozen
            expected = {k: conditions[k] for k in ("engine", "languages", "engine_root", "device", "pin")}
            if frozen != expected:
                raise core.ComparisonError("영역 계획의 모델·런타임과 실제 재사용 세션의 고정 조건이 다릅니다.")
            result = session.recognize_file(input_path, page_number=1, dpi=plan["render_dpi"], psm=conditions["psm"])
            raw = result.to_dict() if hasattr(result, "to_dict") else core._json_copy(result)
        _validate_region_raw(project, plan, raw)
        record.update(raw=raw, raw_sha256=core._digest(raw),
                      status="incomplete" if raw.get("engine_metadata", {}).get("generation_complete") is False else "completed")
    except ocr.OCRCancelled:
        record.update(status="cancelled", error_type="OCRCancelled")
    except core.SourceChangedError:
        raise
    except Exception as exc:
        # Local exception messages can contain private filenames or OCR output.
        record.update(status="failed", error_type=type(exc).__name__)
    with project._lock:
        project.validate_source()
        try:
            _plan_fresh(project, project._page(number), plan)
        except core.ComparisonError:
            record["evidence_stale_on_record"] = True
        project._page(number).setdefault("region_readings", []).append(record)
        project._revision += 1
    if on_result:
        on_result(copy.deepcopy(record))
    return copy.deepcopy(record)


def region_words(record):
    """Translate actual crop word/line/block boxes back to full-render pixels."""
    if record.get("raw") is None:
        return []
    x, y = record["plan"]["bbox"][:2]
    return [{**copy.deepcopy(word), "bbox": [word["bbox"][0] + x, word["bbox"][1] + y,
                                             word["bbox"][2] + x, word["bbox"][3] + y]}
            for word in record["raw"]["words"]]


def _region_candidates(baseline, runs, snapshot, policy, readings):
    result = []
    for record in readings:
        if record["status"] != "completed" or record.get("evidence_stale_on_record"):
            continue
        plan = record["plan"]
        if plan["baseline_run_id"] != baseline["id"] or _parents(runs) != plan["parent_runs"]:
            continue
        anchors = plan["anchors"];raw = record["raw"]
        span = _locate(raw["text"], **anchors, maximum=policy.region_max_chars)
        if span is None:
            continue
        a, b = span;after = raw["text"][a:b];before = plan["before"]
        if after == before or not core._eligibility({"engine": raw["engine"], "raw": raw}, after, before)[0]:
            continue
        try:
            start, end = _current_span(plan["snapshot_text"], snapshot, plan["start"], plan["end"], anchors)
        except core.ComparisonError:
            continue
        item = {"kind": "region", "engine": raw["engine"], "start": start, "end": end,
                "before": before, "after": after, "baseline_start": plan["baseline_start"],
                "baseline_end": plan["baseline_end"], "source_run_id": record["id"],
                "source_raw_sha256": record["raw_sha256"], "source_start": a, "source_end": b,
                "anchors": anchors, "evidence": plan["evidence"], "region_id": record["id"],
                "region_plan_sha256": record["plan_sha256"]}
        item["id"] = core._digest(item);result.append(item)
    return sorted(result, key=lambda p: (p["start"], p["end"], p["id"]))[:policy.max_candidates]


def build_region_candidates(project, number, *, policy=None, region_ids=None):
    policy = _policy(policy)
    with project._lock:
        project.validate_source();page = project._page(number)
        runs = core.usable_runs(page);baseline = project._baseline(page)
        if baseline is None or baseline["engine"] not in runs:
            return []
        selected = set(region_ids) if region_ids is not None else None
        readings = [r for r in page.get("region_readings", []) if selected is None or r["id"] in selected]
        items = _region_candidates(baseline, runs, project.text(number), policy, readings)
        review = _make_review(project, number, policy, "region", items, runs, region_ids=[r["id"] for r in readings])
        return [_flatten(review, p) for p in review["proposals"]]


def _snapshot(page, baseline, identifier):
    if identifier is None:
        return baseline["raw"]["text"]
    edit = next((e for e in page["edits"] if e.get("id") == identifier), None)
    if edit is None:
        raise core.ComparisonError("후보 생성 시점의 교정 이력이 없습니다.")
    return edit["after"] if edit["after"] is not None else baseline["raw"]["text"]


def _parent_map(record, runs):
    parents = record.get("parent_runs")
    if not isinstance(parents, list) or not 1 <= len(parents) <= len(ENGINES):
        raise core.ComparisonError("재검증 부모 판독 목록이 올바르지 않습니다.")
    selected = {}
    for reference in parents:
        if not isinstance(reference, dict) or reference.get("id") not in runs:
            raise core.ComparisonError("재검증 부모 판독을 찾을 수 없습니다.")
        run = runs[reference["id"]]
        if run["engine"] in selected or run["engine"] not in ENGINES or not core._run_complete(run):
            raise core.ComparisonError("재검증에는 정상 완료된 엔진별 부모 판독이 필요합니다.")
        selected[run["engine"]] = run
    if parents != _parents(selected):
        raise core.ComparisonError("재검증 부모 원시·요청 해시가 일치하지 않습니다.")
    baseline = runs.get(record.get("baseline_run_id"))
    if (baseline is None or baseline["engine"] not in selected or selected[baseline["engine"]]["id"] != baseline["id"]
            or baseline["raw_sha256"] != record.get("baseline_raw_sha256")
            or any(not core._compatible(baseline, run) for run in selected.values())):
        raise core.ComparisonError("재검증 기준과 부모 판독 조건이 다릅니다.")
    return baseline, selected


def _validate_plan(project, page, plan, runs):
    if (not isinstance(plan, dict) or plan.get("version") != "anchored-region-v1"
            or plan.get("source_sha256") != project.source_sha256 or plan.get("page_number") != page["number"]
            or plan.get("id") != core._digest({k: v for k, v in plan.items() if k != "id"})):
        raise core.ComparisonError("영역 재판독 계획 지문이 올바르지 않습니다.")
    core._reject_ai_secrets(plan)
    policy = RefinementPolicy(**plan["policy"])
    baseline, selected = _parent_map(plan, runs)
    snapshot = plan.get("snapshot_text");core._text(snapshot)
    if (core._text_sha(snapshot) != plan.get("snapshot_sha256")
            or snapshot != _snapshot(page, baseline, plan.get("snapshot_edit_id"))):
        raise core.ComparisonError("영역 계획의 교정문 시점이 일치하지 않습니다.")
    start, end = plan.get("start"), plan.get("end")
    if (type(start) is not int or type(end) is not int or not 0 <= start <= end <= len(snapshot)
            or end - start > policy.region_max_chars or snapshot[start:end] != plan.get("before")):
        raise core.ComparisonError("영역 계획의 문자 범위가 올바르지 않습니다.")
    a, b = core.ComparisonProject._mapped_span(snapshot, baseline["raw"]["text"], start, end)
    if (a, b) != (plan.get("baseline_start"), plan.get("baseline_end")):
        raise core.ComparisonError("영역 계획의 부모 문자 위치가 일치하지 않습니다.")
    n = policy.anchor_chars;text = baseline["raw"]["text"]
    anchors = {"left": text[max(0, a - n):a], "right": text[b:b + n]}
    if (any(len(v) != n for v in anchors.values()) or anchors != plan.get("anchors")
            or _locate(text, **anchors, maximum=policy.region_max_chars) != (a, b)
            or _locate(snapshot, **anchors, maximum=policy.region_max_chars) != (start, end)):
        raise core.ComparisonError("영역 계획의 유일한 주변 원문이 일치하지 않습니다.")
    evidence = plan.get("evidence")
    if not isinstance(evidence, dict):
        raise core.ComparisonError("영역 좌표의 원시 근거가 없습니다.")
    expected = _witness(selected, **anchors, maximum=policy.region_max_chars, only=evidence.get("engine"))
    if expected is None or expected != evidence:
        raise core.ComparisonError("영역 좌표가 실제 부모 판독의 좌표와 다릅니다.")
    raw = baseline["raw"];width, height = raw["image_width"], raw["image_height"]
    padding = plan.get("padding")
    if type(padding) is not int or not 0 <= padding <= 256:
        raise core.ComparisonError("영역 여백 기록이 올바르지 않습니다.")
    box = evidence["bbox"]
    expected_box = [max(0, math.floor(box[0]) - padding), max(0, math.floor(box[1]) - padding),
                    min(width, math.ceil(box[2]) + padding), min(height, math.ceil(box[3]) + padding)]
    recorded_dpi = baseline.get("request", {}).get("dpi") or raw.get("preprocessing", {}).get("render_dpi") or 300
    if (plan.get("bbox") != expected_box or plan.get("image_width") != width or plan.get("image_height") != height
            or plan.get("render_dpi") != recorded_dpi or plan.get("render_policy") != "ocr.render_page-RGB-v1"
            or plan.get("transform") != {"scale_x": 1, "scale_y": 1, "offset_x": expected_box[0], "offset_y": expected_box[1]}):
        raise core.ComparisonError("영역 좌표·렌더링·원본 변환 기록이 일치하지 않습니다.")
    for key in ("page_pixel_sha256", "crop_pixel_sha256", "crop_png_sha256"):
        if not isinstance(plan.get(key), str) or not core._HEX.fullmatch(plan[key]):
            raise core.ComparisonError("영역 이미지 지문이 올바르지 않습니다.")
    conditions = plan.get("engine_conditions");profile, direction = core._run_profile(baseline)
    if (not isinstance(conditions, dict) or set(conditions) != {"engine", "languages", "profile", "direction", "dpi", "psm", "engine_root", "device", "pin"}
            or conditions.get("engine") not in ENGINES or direction != "horizontal"
            or conditions.get("profile") != profile or conditions.get("direction") != direction
            or conditions.get("languages") != language_for(conditions["engine"], profile, direction)
            or conditions.get("dpi") != recorded_dpi
            or conditions.get("psm") != (6 if conditions["engine"] == "tesseract" else None)
            or not isinstance(conditions.get("engine_root"), str)):
        raise core.ComparisonError("영역 엔진의 고정 요청 조건이 일치하지 않습니다.")
    _validate_conditions({k: conditions[k] for k in ("engine", "languages", "engine_root", "device", "pin")},
                         conditions["engine"], conditions["languages"], conditions["engine_root"], conditions["device"])


def _validate_region_raw(project, plan, raw):
    if not isinstance(raw, dict):
        raise core.ComparisonError("영역 원시 응답이 올바르지 않습니다.")
    # Reuse the original raw schema without pretending its image is the PDF.
    project._validate_raw({**raw, "source_sha256": project.source_sha256, "page_number": plan["page_number"]}, plan["page_number"])
    width = plan["bbox"][2] - plan["bbox"][0];height = plan["bbox"][3] - plan["bbox"][1]
    conditions = plan["engine_conditions"]
    if (raw["source_sha256"] != plan["crop_png_sha256"] or raw["page_number"] != 1
            or raw["image_width"] != width or raw["image_height"] != height
            or raw["engine"] != conditions["engine"] or raw["languages"] != conditions["languages"]
            or raw.get("psm") != conditions["psm"]):
        raise core.ComparisonError("영역 원시 응답의 입력 지문·크기·엔진 조건이 다릅니다.")
    if (raw["engine"] in ("glmocr", "surya")
            and type(raw.get("engine_metadata", {}).get("generation_complete")) is not bool):
        raise core.ComparisonError("영역 생성 결과의 완료 근거가 없습니다.")
    metadata = raw.get("engine_metadata", {})
    effective_device = core._effective_device({"engine": raw["engine"], "raw": raw})
    # Preserve an explicitly unsuccessful CUDA attempt as incomplete evidence.
    # Root/model/runtime pins still have to match; this cannot become a proposal.
    unmet_cuda = (raw["engine"] == "glmocr" and conditions["device"] == "cuda"
                  and effective_device == "cpu" and metadata.get("device_requested") == "cuda"
                  and metadata.get("generation_complete") is False
                  and metadata.get("device_requirement_satisfied") is False)
    if (metadata.get("engine_root") != conditions["engine_root"]
            or metadata.get("session_conditions") != conditions["pin"]
            or (effective_device != conditions["device"] and not unmet_cuda)):
        raise core.ComparisonError("영역 원시 응답의 실제 엔진 폴더·장치·모델·런타임 지문이 계획과 다릅니다.")


def validate_page(project, page, runs):
    """Strict replay of new optional records. Missing fields stay missing."""
    try:
        readings = page.get("region_readings", [])
        reviews = page.get("refinement_reviews", [])
        decisions = page.get("refinement_decisions", [])
        if not all(isinstance(value, list) for value in (readings, reviews, decisions)):
            raise core.ComparisonError("영역·교정 제안 이력은 목록이어야 합니다.")
        regions, review_map = {}, {}
        for record in readings:
            if (not isinstance(record, dict) or not isinstance(record.get("id"), str)
                    or not core.re.fullmatch(r"[a-f0-9]{32}", record["id"]) or record["id"] in regions
                    or not isinstance(record.get("created_at"), str)
                    or type(record.get("evidence_stale_on_record")) is not bool
                    or record.get("status") not in ("completed", "incomplete", "failed", "cancelled")
                    or record.get("plan_sha256") != core._digest(record.get("plan"))):
                raise core.ComparisonError("영역 판독 이력의 형식·지문이 올바르지 않습니다.")
            _validate_plan(project, page, record["plan"], runs)
            if record["status"] in ("completed", "incomplete"):
                _validate_region_raw(project, record["plan"], record.get("raw"))
                complete = record["raw"].get("engine_metadata", {}).get("generation_complete") is not False
                if record["raw_sha256"] != core._digest(record["raw"]) or complete != (record["status"] == "completed"):
                    raise core.ComparisonError("영역 원시 결과의 해시·완료 상태가 일치하지 않습니다.")
            elif record.get("raw") is not None or record.get("raw_sha256") is not None:
                raise core.ComparisonError("실패 영역을 완료된 판독으로 저장할 수 없습니다.")
            regions[record["id"]] = record
        for review in reviews:
            if (not isinstance(review, dict) or review.get("stage") not in ("local", "region")
                    or not isinstance(review.get("created_at"), str) or review.get("id") in review_map
                    or review.get("source_sha256") != project.source_sha256 or review.get("page_number") != page["number"]
                    or review.get("id") != core._digest({k: v for k, v in review.items() if k not in ("id", "created_at")})):
                raise core.ComparisonError("재검증 후보 배치의 형식·지문이 올바르지 않습니다.")
            core._reject_ai_secrets(review)
            policy = RefinementPolicy(**review["policy"])
            if review.get("policy_sha256") != core._digest(asdict(policy)):
                raise core.ComparisonError("재검증 후보 정책 지문이 일치하지 않습니다.")
            baseline, selected = _parent_map(review, runs)
            snapshot = review.get("snapshot_text");core._text(snapshot)
            if (core._text_sha(snapshot) != review.get("snapshot_sha256")
                    or snapshot != _snapshot(page, baseline, review.get("snapshot_edit_id"))):
                raise core.ComparisonError("재검증 후보의 교정문 스냅샷이 일치하지 않습니다.")
            if review["stage"] == "local":
                if review.get("region_ids") != []:
                    raise core.ComparisonError("일반 후보에 영역 판독을 혼합할 수 없습니다.")
                expected = _local_candidates(baseline, selected, snapshot, policy)
            else:
                ids = review.get("region_ids")
                if not isinstance(ids, list) or len(ids) != len(set(ids)) or any(i not in regions for i in ids):
                    raise core.ComparisonError("영역 후보의 원시 판독 연결이 올바르지 않습니다.")
                expected = _region_candidates(baseline, selected, snapshot, policy, [regions[i] for i in ids])
            if expected != review.get("proposals"):
                raise core.ComparisonError("보존된 원시·anchor·좌표에서 교정 후보를 재현할 수 없습니다.")
            review_map[review["id"]] = review
        if "refinement_source_checks" in page:
            from .comparison_refinement_source import validate_records
            validate_records(project, page)
        if "image_only_checks" in page:
            from .comparison_image_only import validate_records
            validate_records(project, page)
        edits = {e["id"]: e for e in page["edits"]};states = {};decision_ids = set();accepted = set()
        for decision in decisions:
            if (not isinstance(decision, dict) or not isinstance(decision.get("id"), str)
                    or decision["id"] in decision_ids or not core.re.fullmatch(r"[a-f0-9]{32}", decision["id"])
                    or not isinstance(decision.get("at"), str) or decision.get("decision") not in ("accepted", "rejected", "undone")):
                raise core.ComparisonError("재검증 후보의 사람 결정 이력이 올바르지 않습니다.")
            review = review_map.get(decision.get("review_id"))
            item = next((p for p in review["proposals"] if p["id"] == decision.get("proposal_id")), None) if review else None
            if item is None:
                raise core.ComparisonError("사람 결정의 원래 교정 후보가 없습니다.")
            key = (review["id"], item["id"]);old = states.get(key)
            if decision["decision"] == "undone":
                edit = edits.get(decision.get("edit_id"));undo = edits.get(decision.get("undo_edit_id"))
                if (old is None or old["decision"] != "accepted" or old.get("edit_id") != decision.get("edit_id")
                        or edit is None or undo is None or undo.get("kind") != "undo" or undo.get("undo_of") != edit["id"]):
                    raise core.ComparisonError("재검증 후보의 되돌리기 연결이 일치하지 않습니다.")
            elif old is not None and old["decision"] != "undone":
                raise core.ComparisonError("이미 확정한 재검증 후보를 중복 처리할 수 없습니다.")
            elif decision["decision"] == "accepted":
                edit = edits.get(decision.get("edit_id"))
                if (edit is None or edit.get("kind") != "refinement_accept" or edit["id"] in accepted
                        or edit.get("refinement_review_id") != review["id"] or edit.get("refinement_proposal_id") != item["id"]):
                    raise core.ComparisonError("교정 후보 채택과 사람 교정 이력이 일치하지 않습니다.")
                baseline = runs[review["baseline_run_id"]]
                previous = edit["before"] if edit["before"] is not None else baseline["raw"]["text"]
                a, b = _current_span(review["snapshot_text"], previous, item["start"], item["end"], item["anchors"])
                if (edit.get("applied_start"), edit.get("applied_end")) != (a, b) or edit["after"] != previous[:a] + item["after"] + previous[b:]:
                    raise core.ComparisonError("사람이 채택한 후보의 정확한 문자 위치를 재현할 수 없습니다.")
                from .comparison_image_only import validate_acceptance
                validate_acceptance(page, "refinement", review["id"], item["id"], decision, edit)
                accepted.add(edit["id"])
            states[key] = decision;decision_ids.add(decision["id"])
        if any(e.get("kind") == "refinement_accept" and e["id"] not in accepted for e in page["edits"]):
            raise core.ComparisonError("확정 결정 없이 재검증 교정문을 저장할 수 없습니다.")
    except (KeyError, TypeError, AttributeError, ValueError) as exc:
        if isinstance(exc, core.ComparisonError):
            raise
        raise core.ComparisonError("영역·재검증 교정 이력을 확인할 수 없습니다.") from exc


def _region_is_fresh(project, page, record):
    if record.get("evidence_stale_on_record"):
        return False
    try:
        _plan_fresh(project, page, record["plan"])
        return True
    except core.ComparisonError:
        return False


def _region_context(record, snapshot):
    plan = record["plan"]
    a, b = _current_span(plan["snapshot_text"], snapshot, plan["start"], plan["end"], plan["anchors"])
    return {"id": record["id"], "status": record["status"], "plan_sha256": record["plan_sha256"],
            "raw_sha256": record["raw_sha256"], "bbox": plan["bbox"], "text": record["raw"]["text"],
            "parent_runs": plan["parent_runs"], "baseline_start": plan["baseline_start"],
            "baseline_end": plan["baseline_end"], "current_start": a, "current_end": b, "eligible": True}


def validate_ai_refinement_context(page, request):
    """Verify immutable captured evidence, regardless of later user decisions."""
    evidence = request.get("refinement_evidence")
    if evidence is None:
        if "refinement_evidence_sha256" in request:
            raise core.ComparisonError("AI 재검증 근거 본문이 없습니다.")
        return
    if (not isinstance(evidence, dict) or set(evidence) != {"version", "snapshot_sha256", "proposals", "region_readings"}
            or evidence["version"] != "anchored-refinement-v1"
            or evidence["snapshot_sha256"] != request["snapshot_sha256"]
            or request.get("refinement_evidence_sha256") != core._digest(evidence)
            or not isinstance(evidence["proposals"], list) or not isinstance(evidence["region_readings"], list)):
        raise core.ComparisonError("AI에 전달한 미확정 교정 근거의 지문이 일치하지 않습니다.")
    reviews = {r["id"]: r for r in page.get("refinement_reviews", [])}
    regions = {r["id"]: r for r in page.get("region_readings", [])}
    references = {r["run_id"]: r["raw_sha256"] for r in request.get("runs", [])}
    def check_scope(record):
        if (record["baseline_run_id"] != request.get("baseline_run_id")
                or record["source_sha256"] != request.get("source_sha256")
                or record["page_number"] != request.get("page_number")
                or any(references.get(p["id"]) != p["raw_sha256"] for p in record["parent_runs"])):
            raise core.ComparisonError("AI 요청과 재검증 근거의 원본·기준·부모 판독이 다릅니다.")
    seen = set()
    for entry in evidence["proposals"]:
        review = reviews.get(entry.get("review_id"))
        item = next((p for p in review["proposals"] if p["id"] == entry.get("id")), None) if review else None
        if item is None or (review["id"], item["id"]) in seen:
            raise core.ComparisonError("AI에 전달한 원래 재검증 후보가 없습니다.")
        check_scope(review)
        expected = _flatten(review, item)
        a, b = _current_span(review["snapshot_text"], request["snapshot_text"], item["start"], item["end"], item["anchors"])
        expected.update(pending=True, eligible=True, blocked_reason="", current_start=a, current_end=b)
        if "source_check" in entry:
            captured = entry["source_check"]
            record = next((r for r in page.get("refinement_source_checks", [])
                           if isinstance(captured, dict) and r["id"] == captured.get("source_check_id")), None)
            verdict = next((v for v in record["verdicts"]
                            if v["review_id"] == review["id"] and v["proposal_id"] == item["id"]), None) if record else None
            if verdict is None or verdict.get("blocked"):
                raise core.ComparisonError("AI에 전달한 PDF 부호 확인 근거가 없거나 보류 상태입니다.")
            expected["source_check"] = {**copy.deepcopy(verdict), "source_check_id": record["id"]}
        if "image_only_check" in entry:
            from .comparison_image_only import captured_proof
            expected["image_only_check"] = captured_proof(page, "refinement", review["id"], item["id"], entry["image_only_check"])
        if entry != expected:
            raise core.ComparisonError("AI에 전달한 후보 문자·위치·원시 근거가 다릅니다.")
        seen.add((review["id"], item["id"]))
    seen_regions = set()
    for entry in evidence["region_readings"]:
        record = regions.get(entry.get("id"))
        if (record is None or record["id"] in seen_regions or record["status"] != "completed"
                or record.get("evidence_stale_on_record") or entry != _region_context(record, request["snapshot_text"])):
            raise core.ComparisonError("AI에 전달한 영역 원시 판독이 일치하지 않습니다.")
        check_scope(record["plan"])
        seen_regions.add(record["id"])


def refinement_context(project, number):
    """Small pending evidence view; full snapshots remain in their review batch."""
    page = project.page(number)
    return {"version": "anchored-refinement-v1", "snapshot_sha256": core._text_sha(project.text(number)),
            "proposals": [p for p in proposals(project, number) if p["eligible"]],
            "region_readings": [_region_context(r, project.text(number)) for r in page.get("region_readings", [])
                                if r["status"] == "completed" and _region_is_fresh(project, page, r)]}
