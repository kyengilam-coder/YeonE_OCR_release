"""Local recognition followed by bounded, selective AI review.

The comparison project owns immutable local readings and AI proposals. Standard
Work records are derived views for the existing document/PDF/review builders.
Signed-in subscriptions can also read whole pages and automatically apply checked
changes. No legacy third-pass reader or document context model is invoked.
"""
from __future__ import annotations

from .local_options import FAST_STRATEGIES

import copy
import hashlib
import json
import math
from pathlib import Path
import time

from . import __version__
from .state import Work, write_atomic

POLICY = "local-first-selective-review-v1"


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def _source(work, doc):
    """Image folders have a stable, private PDF snapshot for local adapters."""
    source = Path(work.pdf_path).expanduser().resolve()
    if source.is_file():
        return source
    manifest = json.loads((work.dir / "source.json").read_text(encoding="utf-8"))
    folder = work.dir / "local-input"
    folder.mkdir(exist_ok=True)
    snapshot = folder / (manifest["sha256"] + ".pdf")
    if not snapshot.exists():
        write_atomic(snapshot, doc.tobytes())
    return snapshot.resolve()


def _project(source, work, *, lazy_pages=False):
    from ab_ocr.ocr import stable_file_sha256
    from .comparison import ComparisonProject
    path = work.dir / "comparison" / ("local-first-" + stable_file_sha256(source)[:24] + ".yeone-compare.json")
    if path.exists():
        return ComparisonProject.load(path, source_path=source, paged=True, lazy_pages=lazy_pages)
    project = ComparisonProject.open(source)
    project._paged_checkpoints = True
    path.parent.mkdir(parents=True, exist_ok=True)
    project.save(path)
    return project


def _preserve_storage_failure(work, project, error, log):
    """Best effort only: a full/unavailable work drive must not hide the fault."""
    import os
    import tempfile
    import uuid
    stamp = time.strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:8]
    detail = {'project': str(project.project_path) if project else None,
              'error': str(error), 'detail': getattr(error, 'storage_detail', {}),
              'stopped_new_requests': True}
    folders = [work.dir / 'storage-diagnostics'] if work is not None else []
    folders.append(Path(os.environ.get('LOCALAPPDATA') or tempfile.gettempdir()) / 'YeonE_OCR' / 'recovery')
    for folder in folders:
        try:
            folder.mkdir(parents=True, exist_ok=True)
            diagnostic = folder / (stamp + '.json')
            write_atomic(diagnostic, json.dumps(detail, ensure_ascii=False, indent=2))
            if project is not None:
                recovery = folder / (stamp + '.recovery.yeone-compare.json')
                project.save_recovery(recovery)
                detail['recovery'] = str(recovery)
            write_atomic(folder / (stamp + '.json'), json.dumps(detail, ensure_ascii=False, indent=2))
            log(f'  저장소 진단·복구 자료: {folder / stamp}')
            return
        except Exception as exc:
            detail['recovery_error_type'] = type(exc).__name__
            detail['recovery_error'] = str(exc)
            try:
                write_atomic(folder / (stamp + '.json'), json.dumps(detail, ensure_ascii=False, indent=2))
            except Exception:
                pass
    log('  복구 자료를 저장할 수 없습니다. 기존 프로젝트와 AI 응답 캐시는 그대로 보존합니다.')


class _Checkpoint:
    """Mid-page project saves, spaced out once a save itself becomes slow.

    The whole project is rewritten on every save, so on a long book a save
    after each engine costs more than the engine result it protects. Cheap
    saves stay immediate; slow ones are at most CHECKPOINT_SECONDS apart.
    The page-end save is unconditional, so at most one page of engine
    output can be lost to a crash.
    """
    SLOW_SAVE_SECONDS, CHECKPOINT_SECONDS = 0.5, 30.0

    def __init__(self, project, clock=time.monotonic):
        self.project, self.clock = project, clock
        self.last_cost, self.last_at = 0.0, None

    def save(self):
        started = self.clock()
        from .comparison import ComparisonError
        from .comparison_storage import ProjectStorageError,storage_failure
        try:self.project.save()
        except ProjectStorageError:
            from . import cli
            cli.CANCEL.set()
            raise
        except (ComparisonError,OSError) as exc:
            from . import cli
            cli.CANCEL.set()
            raise storage_failure(self.project.project_path,exc) from exc
        self.last_at = self.clock()
        self.last_cost = self.last_at - started

    def maybe(self, _value=None):
        if (self.last_at is None or self.last_cost < self.SLOW_SAVE_SECONDS
                or self.clock() - self.last_at >= self.CHECKPOINT_SECONDS):
            self.save()


def _protected_record(work, number, old):
    if not old:
        return False
    if old.get("pipeline") != POLICY:
        return True
    if "원본 대조 완료(사람)" in work.statuses(number):
        return True
    return old.get("derived_reading_sha256") != _digest(old.get("merged"))


def _selected(project, number):
    page = project.page(number)
    return next((r for r in page["runs"] if r["id"] == page["baseline_run_id"]), None)


def _review_summary(project, number):
    page = project.page(number)
    views = project.ai_proposals(number, include_decided=True)
    return [{k: item.get(k) for k in ("id", "review_id", "before", "after", "reason", "uncertain",
                                     "decision", "stale", "blocked_reason")} for item in views], page["ai_reviews"]


GRADE_RANK = {"green": 1, "blue": 2, "red": 3}


def _local_grades(project, number, run, current, witness=None, marks=()):
    """Per-character check grades for a local-first page (text coordinates of ``current``).

    red   = engines disagree on the glyphs (not whitespace only), an unknown
            glyph remains, or a pending AI review proposes a different reading
    blue  = the selected engine's own confidence is below its routing threshold
    green = the text differs from the selected engine's reading (an accepted
            candidate, AI proposal or human edit)
    With ``witness`` (an existing PDF text layer, --local-textlayer-witness),
    glyphs it reads differently are blue too.
    Stronger grades win. Spans that no longer map onto the current text are
    left unpainted rather than guessed.
    """
    import difflib
    from .collate.zh import script_only, script_pair
    from .comparison import ComparisonError
    from .comparison_ai import LOW_CONFIDENCE_THRESHOLDS, _word_spans
    from .ocr_disagreement import unsupported_disagreement
    raw = run["raw"]
    base = raw["text"]
    grade = [None] * len(current)

    def paint(start, end, kind):
        if start == end:                       # an insertion point: paint the neighbouring glyph
            start, end = (start, start + 1) if start < len(current) else (start - 1, start)
        for i in range(max(0, start), min(len(current), end)):
            if not current[i].isspace() and GRADE_RANK[kind] > GRADE_RANK.get(grade[i], 0):
                grade[i] = kind

    def mapped(start, end, origin=base):
        try:
            return project._mapped_span(origin, current, start, end)
        except ComparisonError:
            return None

    if base != current:
        for tag, a0, a1, b0, b1 in difflib.SequenceMatcher(None, base, current, autojunk=False).get_opcodes():
            if tag == "replace" and a1 - a0 == b1 - b0:
                for k in range(b1 - b0):        # the same glyph in the other script (会→會) is not an edit to check
                    if not script_pair(base[a0 + k], current[b0 + k]):
                        paint(b0 + k, b0 + k + 1, "green")
            elif tag != "equal":
                paint(b0, b1, "green")
    threshold = LOW_CONFIDENCE_THRESHOLDS.get(run["engine"])
    if threshold is not None:
        for start, end, word in _word_spans(raw):
            confidence = word.get("confidence")
            if type(confidence) in (int, float) and confidence < threshold:
                for i in range(start, end):            # per glyph: an edited glyph is green, its neighbours stay blue
                    span = mapped(i, i + 1)
                    if span:
                        paint(*span, "blue")
    if witness:
        from .local_options import witness_spans
        for start, end in witness_spans(current, witness):
            paint(start, end, "blue")
    for start, end, kind in marks:                 # AI page reading: kept differences (blue / red)
        paint(start, end, kind)
    page = project.page(number)
    for suggestion in page.get("suggestions", []):
        if (suggestion.get("decision") == "pending" and suggestion.get("kind") != "whitespace"
                and not script_only(suggestion.get("candidates", {}).values())):
            start, end = suggestion["start"], suggestion["end"]
            if unsupported_disagreement(base[start:end], suggestion.get("candidates", {})):
                continue
            values = list(suggestion.get("candidates", {}).values())
            from .punctuation_review import cosmetic_comma_offsets
            cosmetic=cosmetic_comma_offsets(base[start:end],values)
            if values and end > start and all(len(v) == end - start for v in values):
                # Adjacent differences are one suggestion: paint only the glyphs some engine really reads
                # otherwise, not the neighbours that differ only in script (會議爲 / 会议為 → 爲 alone).
                for k in range(end - start):
                    if k in cosmetic:continue
                    glyphs = {v[k] for v in values}
                    first = next(iter(glyphs))
                    if len(glyphs) > 1 and not all(g == first or script_pair(g, first) for g in glyphs):
                        span = mapped(start + k, start + k + 1)
                        if span:
                            paint(*span, "red")
                continue
            span = mapped(start, end)
            if span:
                if cosmetic:
                    for offset in range(start,end):
                        if offset-start not in cosmetic:
                            part=mapped(offset,offset+1)
                            if part:paint(*part,'red')
                else:paint(*span, "red")
    for proposal in project.ai_proposals(number):
        if proposal.get("stale") or proposal.get("superseded"):
            continue
        review = next((r for r in page.get("ai_reviews", []) if r["id"] == proposal["review_id"]), None)
        snapshot = (review or {}).get("request", {}).get("snapshot_text")
        span = mapped(proposal["start"], proposal["end"], snapshot) if isinstance(snapshot, str) else None
        if span:
            paint(*span, "red")
    for i, char in enumerate(current):
        if char in "\ufffd\u25a1":
            paint(i, i + 1, "red")
    out, offset = [], 0
    for index, line in enumerate(current.split("\n")):
        i = 0
        while i < len(line):
            kind = grade[offset + i]
            j = i + 1
            while j < len(line) and grade[offset + j] == kind:
                j += 1
            if kind:
                out.append({"region_id": f"local-line-{index + 1}", "para": 0, "start": i, "end": j, "grade": kind})
            i = j
        offset += len(line) + 1
    return out


# Review states a page can be left in when nothing more is to be done for it under the same conditions.
RESUME_DONE = {"no-api-needed", "reviewed", "needs-local-review", "blank-page"}


def _human_review_only(review):
    """No automatic request remains; unresolved positions also require human review.

    New engine results or changed reading conditions still invalidate resume reuse.
    """
    review = review or {}
    outcome = review.get('outcome') or {}
    return (review.get('status') == 'needs-local-review'
            and not review.get('engine_errors') and not review.get('deferred_calls')
            and not outcome.get('failed') and not outcome.get('cancelled'))


def _resume_key(args, run_id, text, options):
    """What a finished page depends on besides the engine readings (those are compared engine by engine
    in run_automatic_page): reading conditions, the baseline reading, the page text (human and accepted
    AI corrections) and the AI settings. Same key and a finished review: the page needs no work."""
    fields = {"policy": POLICY, "mode": args.mode, "profile": getattr(args, "local_profile", None),
              "dpi": getattr(args, "dpi", None), "run": run_id, "text": _digest(text),
              "review": bool(getattr(args, "local_ai_review", True)), "ai_page": getattr(args, "local_ai_page", "off") or "off",
              "accept": bool(getattr(args, "local_ai_accept", False)), "tile": getattr(args, "local_ai_detail_tile", 0) or 0,
              "batch": getattr(args, "local_ai_batch", 1) or 1, "columns": bool(getattr(args, "local_columns", True)),
              "forms": bool(getattr(args, "local_korean_forms", True)), "offset": getattr(args, "print_offset", None),
              "enhance": getattr(args, "local_enhance", "none"), "options": options or None}
    return _digest(fields)


def _resume_done(old, args, key, review_enabled):
    """The record of an earlier run says this page is finished under the same conditions (same key)."""
    first = (old or {}).get("local_first") or {}
    status = (first.get("review") or {}).get("status")
    if review_enabled and not _human_review_only(first.get('review')) and (status == "needs-local-review" or
                           (first.get("review") or {}).get("unlocalized_targets")):
        return False  # Re-plan unresolved geometry using saved readings, without re-recognition.
    if first.get("resume_key") != key:
        return False
    if status not in RESUME_DONE and not (status == "local-complete" and not review_enabled):
        return False                                    # pending, failed, cancelled, or stopped before the review
    mode = getattr(args, "local_ai_page", "off") or "off"
    return mode == "off" or ("ai_page" in first and not (first.get("ai_page") or {}).get("empty"))


def _response_usage(tokens):
    """Public usage of one stored response; never retain arbitrary provider fields."""
    result = {"calls": 1, "prompt_tokens": 0, "output_tokens": 0, "thinking_tokens": 0}
    if not isinstance(tokens, dict):
        return result
    for key, aliases in (("prompt_tokens", ("input_tokens", "prompt_token_count", "prompt_tokens")),
                         ("output_tokens", ("output_tokens", "candidates_token_count")),
                         ("thinking_tokens", ("thoughts_token_count", "thinking_tokens"))):
        for name in aliases:
            value = tokens.get(name)
            if type(value) in (int, float) and value >= 0 and (type(value) is int or math.isfinite(value)):
                result[key] = value
                break
    return result


def _record(project, number, args, work, started, review, extras=None):
    from .local_reading import reading_from_run
    extras = extras or {}
    run = _selected(project, number)
    if run is None:
        raise ValueError("로컬 기준 판독이 없습니다.")
    direction = "horizontal" if args.mode == "horizontal_zh" else "vertical"
    raw = run["raw"]
    first = reading_from_run(raw, number, direction)
    current = project.text(number)
    marks, ai_page_summary = [], None
    page_ai = extras.get("ai_page")
    if page_ai and page_ai.get("text"):
        from .ai_page import merge
        text, red, blue, ai_page_summary = merge(current, page_ai["text"], page_ai.get("mode", "prefer"),
                                                 uncertain=page_ai.get("uncertain") or ())
        ai_page_summary.update({k: page_ai[k] for k in ("mode", "model", "uncertain", "cached") if k in page_ai})
        if ai_page_summary["used"]:
            current = text
            marks = [(a, b, "red") for a, b in red] + [(a, b, "blue") for a, b in blue]
        elif page_ai.get('mode','prefer')=='prefer':
            page=project.page(number)
            if page.get('corrected_text') is None and not any(page.get(k) for k in ('edits','decisions','ai_decisions')):
                from .catalog_recovery import recover_catalog
                recovery=recover_catalog(page_ai,[r['raw']['text'] for r in page['runs']])
                if recovery:
                    current,red,evidence=recovery
                    marks=[(a,b,'red') for a,b in red]
                    ai_page_summary.update(used=True,catalog_recovery=evidence)
    merged = reading_from_run(raw, number, direction, text=current)
    from .local_options import mark_folios
    mark_folios(merged)                                     # a printed page number is not body text
    if getattr(args, "local_profile", None) == "mixed" and getattr(args, "local_korean_forms", True):
        from .korean_forms import apply_to_reading
        apply_to_reading(merged)                            # 説→說, 真→眞 … (KS X 1001); document only, same offsets
    if extras.get("columns") and direction == "horizontal":
        from .local_options import column_order
        merged.regions = column_order(merged.regions)       # document order only; the project text keeps engine order
    offset = getattr(args, "print_offset", None)
    if offset is not None:
        merged.print_page = first.print_page = str(number + offset)
    proposals, reviews = _review_summary(project, number)
    if review.get("targets") or review.get("unlocalized_targets"):
        from .schema import Uncertain
        merged.uncertain.append(Uncertain(context="로컬 인식 의심 구간의 AI 검증·수정 제안을 비교·교정 창에서 확인하세요."))
    # Usage belongs to the stored responses, not to this execution's bill. Rebuild
    # totals each time: a reused page cache contributes its original response once.
    usage = {"calls": 0, "prompt_tokens": 0, "output_tokens": 0, "thinking_tokens": 0}
    for item in reviews:
        meta = item.get("metadata", {})
        for key, value in _response_usage(meta.get("usage", {})).items():
            usage[key] += value
    if page_ai is not None:
        # A completed response still consumed a call when empty or rejected by
        # the merge's similarity guard. Cache hits are identified separately.
        page_usage = _response_usage(page_ai.get("usage", {}))
        if ai_page_summary is None:
            # Accounting for an empty response must not change the old resume
            # decision: an empty page transcription was not considered done.
            ai_page_summary = {"used": False, "empty": True,
                               **{k: page_ai[k] for k in ("mode", "model", "cached") if k in page_ai}}
        ai_page_summary["usage"] = page_usage
        for key, value in page_usage.items():
            usage[key] += value
    merged_data = merged.model_dump()
    try:
        local_grades = _local_grades(project, number, run, current, witness=extras.get("witness"), marks=marks)
    except Exception:
        local_grades = []                                    # grades are display only; never lose the reading for them
    if page_ai and ai_page_summary and ai_page_summary.get('used') and page_ai.get('mode','prefer')=='prefer':
        page=project.page(number)
        if page.get('corrected_text') is None and not any(page.get(k) for k in ('edits','decisions','ai_decisions')):
            from .ai_priority import confirmed_positions,supported_grades
            confirmed=confirmed_positions(current,page_ai['text'],red=red)
            # Explicit selective review proposals remain red even when a page
            # reader otherwise agrees with the selected engine.
            for proposal in project.ai_proposals(number):
                proposal_review=next((r for r in page.get('ai_reviews',[]) if r['id']==proposal['review_id']),{})
                snapshot=(proposal_review.get('request') or {}).get('snapshot_text')
                if isinstance(snapshot,str):
                    try:a,b=project._mapped_span(snapshot,current,proposal['start'],proposal['end'])
                    except Exception:continue
                    confirmed.difference_update(range(a,b))
            local_grades=supported_grades(local_grades,current,confirmed)
    return {"file": Path(args.pdf).name, "pdf_page": number, "version": __version__,
            "pipeline": POLICY, "reader": "local", "model": run["engine"], "mode": args.mode,
            "image_frame": "full-page", "image_source": "로컬 OCR 전체 쪽 렌더링", "upscale": 1,
            "read_at": time.strftime("%Y-%m-%d %H:%M:%S"), "seconds": round(time.perf_counter() - started, 2),
            "pass1": first.model_dump(), "pass2": {}, "merged": merged_data,
            "derived_reading_sha256": _digest(merged_data), "usage": usage, "verify": [],
            "coverage_notes": ["로컬 줄 좌표를 사용합니다. 제목·도판·다단 구조는 원본과 대조하세요."],
            "textlayer_diff": {}, "merge_stats": {}, "timings": {}, "crops": {}, "local_grades": local_grades,
            "local_first": {"project_path": str(project.project_path), "source_sha256": project.source_sha256,
                            "baseline_run_id": run["id"], "profile": args.local_profile,
                            "review": copy.deepcopy(review), "ai_proposals": proposals,
                            **({"ai_page": ai_page_summary} if ai_page_summary else {}),
                            **({"options": copy.deepcopy(extras["options"])} if extras.get("options") else {}),
                            "resume_key": _resume_key(args, run["id"], project.text(number), extras.get("options"))}}


def _blank_record(project, number, args, work, started):
    """A page with (almost) no ink, read by no engine (--local-skip-blank)."""
    from .schema import PageReading
    reading = PageReading(pdf_page=number, page_note="빈 쪽으로 판정해 로컬 OCR을 건너뛰었습니다(--local-skip-blank). 글자가 있으면 이 옵션 없이 다시 인식하세요.")
    offset = getattr(args, "print_offset", None)
    if offset is not None:
        reading.print_page = str(number + offset)
    data = reading.model_dump()
    return {"file": Path(args.pdf).name, "pdf_page": number, "version": __version__,
            "pipeline": POLICY, "reader": "local", "model": "blank-page", "mode": args.mode,
            "image_frame": "full-page", "image_source": "로컬 OCR 전체 쪽 렌더링", "upscale": 1,
            "read_at": time.strftime("%Y-%m-%d %H:%M:%S"), "seconds": round(time.perf_counter() - started, 2),
            "pass1": copy.deepcopy(data), "pass2": {}, "merged": data, "derived_reading_sha256": _digest(data),
            "usage": {"calls": 0, "prompt_tokens": 0, "output_tokens": 0, "thinking_tokens": 0}, "verify": [],
            "coverage_notes": ["빈 쪽 판정: 어두운 화소가 거의 없어 엔진을 실행하지 않았습니다."],
            "textlayer_diff": {}, "merge_stats": {}, "timings": {}, "crops": {}, "local_grades": [],
            "local_first": {"project_path": str(project.project_path), "source_sha256": project.source_sha256,
                            "baseline_run_id": None, "profile": args.local_profile, "blank_page": True,
                            "review": {"status": "blank-page", "targets": 0, "calls": 0}, "ai_proposals": []}}


def _save_record(project, number, args, work, started, review, *, render=True, extras=None):
    if getattr(args, 'local_review_only', False):
        record = work.load_record(number)
        if not record or not record.get('local_first'):
            raise ValueError('미완료 검증에는 저장된 로컬 OCR 기록이 필요합니다.')
        record = copy.deepcopy(record)
        record['local_first']['review'] = copy.deepcopy(review)
        proposals, reviews = _review_summary(project, number)
        record['local_first']['ai_proposals'] = proposals
        usage = {'calls': 0, 'prompt_tokens': 0, 'output_tokens': 0, 'thinking_tokens': 0}
        for item in reviews:
            for key, value in _response_usage(item.get('metadata', {}).get('usage', {})).items():
                usage[key] += value
        for key, value in (record['local_first'].get('ai_page') or {}).get('usage', {}).items():
            if key in usage:
                usage[key] += value
        record['usage'] = usage
        record['local_first']['revalidated_at'] = time.strftime('%Y-%m-%d %H:%M:%S')
        work.backup_record(number)
        work.save_record(number, record)
        return
    from ab_ocr.ocr import render_page
    from . import cli
    # The preview must use the same pixels and frame as local recognition.
    if render and (not work.page_image_path(number).is_file() or getattr(args, "local_enhance", "none") not in (None, "none")
                   or not (work.load_record(number) or {}).get("local_first")):
        # The page image of an earlier local-first run is the same pixels (same source, same DPI): keep it.
        with render_page(project.source_path, number, args.dpi, should_stop=cli.CANCEL.is_set) as image:
            cli._save_png_atomic(image, work.page_image_path(number))
        write_atomic(work.pages_dir / f"p{number:03d}.src.txt", "로컬 OCR 전체 쪽 렌더링")
    record = _record(project, number, args, work, started, review, extras)
    if work.load_record(number):
        work.backup_record(number)
    work.save_record(number, record)
    work.set_status(number, "1차 OCR 완료")
    if record["merged"]["uncertain"] or record["local_first"]["ai_proposals"]:
        work.set_status(number, "판독 확인 필요")
    return record


def _accept_ai(project, number) -> dict:
    """(--local-ai-accept, also a subscription default) apply applicable, certain proposals.
    Recorded like a manual acceptance (reversible); for measuring what the review changes.
    Proposals the safety checks hold (no engine or image support, uncertain) are counted, not applied."""
    from .comparison import ComparisonError
    accepted, held = 0, 0
    for proposal in project.ai_proposals(number):
        if not proposal.get("applicable") or proposal.get("uncertain"):
            held += 1
            continue
        try:
            project.accept_ai_proposal(number, proposal["review_id"], proposal["id"], actor="auto")
        except ComparisonError:
            held += 1
            continue
        accepted += 1
    if accepted:
        project.save()
    return {"accepted": accepted, "held": held}


# How a page's review status reads in the log; the record keeps the status values themselves.
REVIEW_STATUS_LABELS = {"local-complete": "로컬 인식만(AI 검증 안 함)", "blank-page": "빈 쪽",
                        "no-api-needed": "AI 검증 필요 없음", "needs-local-review": "사람 확인 필요(미확정 제안·위치 미확인)",
                        "pending": "AI 검증 보류", "reviewing": "AI 검증 중", "reviewed": "AI 검증 끝",
                        "failed": "AI 검증 일부 실패", "cancelled": "중지"}


SUBSCRIPTION_DEFAULTS = {"local_ai_page": "prefer", "local_ai_accept": True, "local_ai_detail_tile": 1024, "local_ai_batch": 4}
PLAIN_DEFAULTS = {"local_ai_page": "off", "local_ai_accept": False, "local_ai_detail_tile": 0, "local_ai_batch": 1}


def _resolve_ai_defaults(args, cli):
    """Default (2026-11, chosen after the vertical measurements): when a signed-in plan (Claude Code,
    Codex CLI for ChatGPT or Gemini CLI: subscriptions.pick) is usable, read each page with Claude as well (--local-ai-page prefer), review the
    suspicious spans through it with detail tiles, four spans a call, and apply its certain, checked
    proposals. Without it, the earlier defaults. Options given explicitly always win.
    Returns the values the options were resolved to, for the option record."""
    from . import subscriptions
    from .claude_subscription import auth_mode
    plan = None
    page_ai = getattr(args, 'local_ai_page', 'auto')
    needs_ai = getattr(args, 'local_ai_review', True) or page_ai != 'off'
    if needs_ai and (page_ai in (None, 'auto') or any(
            getattr(args, name, None) is None for name in SUBSCRIPTION_DEFAULTS)):
        if auth_mode(getattr(args, "local_ai_auth", None)) != "api":
            plan = subscriptions.pick(getattr(args, "local_ai_plan", None))[0]
    use = plan is not None
    chosen = SUBSCRIPTION_DEFAULTS if use else PLAIN_DEFAULTS
    for name, value in chosen.items():
        current = getattr(args, name, None)
        if current is None or current == "auto":
            setattr(args, name, value)
    if use and getattr(args, "local_ai_auth", None) in (None, "auto"):
        args.local_ai_auth = "subscription"
    if use:
        cli.log(f"  {subscriptions.NAMES[plan]} 구독에 로그인되어 있어 AI 판독을 함께 합니다(쪽 판독 + 의심 구간 검증, "
                "구독 사용량에서 차감). 끄려면 --local-ai-page off --local-ai-auth api")
    return {name.removeprefix("local_"): value for name, value in chosen.items()}


def _api_fallback(args, calls, environment_key, split_keys):
    """Compatibility entry point. Old settings cannot enable paid subscription fallback.

    Explicit --local-ai-auth api is handled separately by the caller.
    """
    return None


def _ai_page_reader(args, project, work, cli, session=None, api=None):
    """(Trial, --local-ai-page) a function number -> AI page reading for extras, or None.
    Through a signed-in plan's CLI (subscriptions) only; failures leave the engine reading alone."""
    mode = getattr(args, "local_ai_page", "off") or "off"
    if mode == "off":
        return None
    from . import subscriptions
    from .claude_subscription import SubscriptionError
    if session is None:
        plan, reason = subscriptions.pick(getattr(args, "local_ai_plan", None))
        if not plan:
            cli.log("  AI 쪽 판독을 하지 않습니다: " + (reason or "구독 계정 연결을 확인하세요."))
            return None
        session = subscriptions.Session(plan, getattr(args, "local_ai_plan", None), api=api, gemini_model=getattr(args, "local_gemini_model", None), log=cli.log,
                                        should_stop=cli.CANCEL.is_set)
        subscriptions.activate(session)
    plan = session.plan
    client = session                     # moves to the next plan, then the API key, when a plan runs out
    model = subscriptions.model(plan, None)
    if plan == "gemini" and session.gemini_model:
        model = session.gemini_model
    direction = "horizontal" if args.mode == "horizontal_zh" else "vertical"
    import os
    # Readings are keyed by page image, model and prompt; a shared folder (measurement sets) reuses them.
    cache = Path(os.environ.get("YEONE_OCR_AI_PAGE_CACHE") or work.dir / "ai-page")
    cli.log(f"  AI 쪽 판독({mode}): {subscriptions.NAMES[plan]} 구독 · 모델 {model} · 같은 조건의 저장 판독 우선 사용 · 없을 때 새 요청")

    def read(number):
        from ab_ocr.ocr import render_page
        from .ai_page import read_page
        from .comparison_ai import AIReviewCancelled
        if not session.available:
            return None
        def count_request():
            with cli.LOG_LOCK:
                cli.LOCAL_PAGE_CALLS += 1
            cli.log(f'  {number}쪽: 저장 판독 없음 — AI 쪽 판독 새 요청({session.short()})')
        def retry_invalid():
            if session.plan is None or cli.CANCEL.is_set():
                return False
            cli.log(f'  {number}쪽: AI 쪽 판독 응답 형식 오류 — 구독으로 1회 다시 요청')
            return True
        try:
            with render_page(project.source_path, number, args.dpi, should_stop=cli.CANCEL.is_set) as image:
                result = read_page(image, direction=direction, profile=args.local_profile, client=client,
                                   model=model, cache_dir=cache, on_request=count_request, retry_invalid=retry_invalid)
        except AIReviewCancelled:
            return None
        except (SubscriptionError, TimeoutError, ValueError, OSError) as exc:
            cli.log(f"  {number}쪽: AI 쪽 판독 실패 — {exc} (엔진 판독만 씀)")
            return None
        cli.log(f"  {number}쪽: AI 쪽 판독 {len(result['text'])}자"
                + (" (저장된 판독)" if result.get("cached") else f"({session.short()})")
                + (f" · {result['cache_warning']}" if result.get("cache_warning") else ""))
        return {**result, "mode": mode}
    return read


class _Side:
    """A call on its own thread (the AI page reading beside the review); result() waits for it."""

    def __init__(self, function, *args):
        import threading
        self._value = self._error = None
        self._thread = threading.Thread(target=self._run, args=(function, args), daemon=True, name="ai-page")
        self._thread.start()

    def _run(self, function, args):
        try:
            self._value = function(*args)
        except BaseException as exc:                 # handed to the caller of result()
            self._error = exc

    def result(self):
        self._thread.join()
        if self._error is not None:
            raise self._error
        return self._value


class _AiWorkers:
    """The AI part of pages (ai_stage) on worker threads, so the engines read on without waiting for an
    answer: pages wait in one queue, in page order, and every connection takes --local-ai-pages pages
    at a time. With --local-ai-split every signed-in plan has its own connection (session); otherwise
    the run's one connection. A page is answered by one connection only (its review stays consistent).
    Records keep the run's provider and model settings, so a page sent to another plan next time
    resumes the same plan of batches; the actual plan and model are in each review's metadata."""

    def __init__(self, crews, stage, failed_page, log, *, rotate=True, should_stop=lambda: False, on_start=None,
                 on_done=None, watch_chatgpt=False):
        import collections
        import threading
        self.crews, self.stage, self.failed_page, self.log = crews, stage, failed_page, log
        self.should_stop, self.on_start, self.on_done = should_stop, on_start, on_done
        self.stopped = False
        self._queue, self._closed = collections.deque(), False
        self._cond = threading.Condition()
        self._threads = []
        self._quota_stop = threading.Event()
        self._quota_thread = None
        self._rotate = rotate
        # First, second and third get three, two and one turns per
        # cycle. Interleave turns so all available plans start early.
        self._schedule = [index for size in range(len(crews), 0, -1) for index in range(size)] if rotate else list(range(len(crews)))
        self._next_slot = 0
        for index, crew in enumerate(crews):
            crew["index"] = index
            crew["idle"] = max(1, int(crew.get("pages") or 1))          # workers of this connection without a page
        for crew in crews:
            for index in range(crew["idle"]):
                thread = threading.Thread(target=self._work, args=(crew,), daemon=True, name=f"ai-{crew['plan']}-{index + 1}")
                thread.start()
                self._threads.append(thread)
        if watch_chatgpt:
            from . import subscriptions
            self._quota_thread = threading.Thread(
                target=subscriptions.watch_chatgpt_quota,
                args=(self._quota_stop, self._chatgpt_recovered, self.should_stop),
                daemon=True, name="chatgpt-quota",
            )
            self._quota_thread.start()

    def _chatgpt_recovered(self):
        self.log("  ChatGPT 사용량 한도 회복 확인 — 다음 쪽 배정에 다시 참여합니다.")
        with self._cond:
            self._cond.notify_all()

    @classmethod
    def start(cls, args, session, api, project, work, cli, stage, failed_page, review_enabled, *, api_parallel=False, **hooks):
        if not review_enabled:
            return None
        per = min(4, max(1, int(getattr(args, "local_ai_pages", 2) or 1)))
        if session is None:
            if not api_parallel:
                return None
            cli.log(f"  로컬 OCR과 선택 구간 API 검증을 병행합니다. API 검증은 {per}쪽씩 진행합니다.")
            return cls([{"plan": "api", "session": None, "reader": None, "pages": per}], stage,
                       failed_page, cli.log, should_stop=cli.CANCEL.is_set, **hooks)
        from . import subscriptions
        plans = [session.plan]
        if getattr(args, "local_ai_split", False):
            plans += [plan for plan in subscriptions.order(getattr(args, "local_ai_plan", None))
                      if plan != session.plan and plan not in subscriptions.exhausted()
                      and subscriptions.status(plan)["usable"]]
        crews = []
        for plan in plans:
            plan_session = session if plan == session.plan else subscriptions.Session(
                plan, plan, api=api, gemini_model=getattr(args, "local_gemini_model", None), log=cli.log, record_provider=session.record_provider, should_stop=cli.CANCEL.is_set)
            reader = None if getattr(args, 'local_review_only', False) else _ai_page_reader(args, project, work, cli, plan_session, api)
            crews.append({"plan": plan, "session": plan_session, "reader": reader, "pages": per})
        subscriptions.activate(session, [crew["session"] for crew in crews])     # the window shows every connection
        names = " · ".join(subscriptions.NAMES[p] for p in plans)
        rotate = subscriptions.plan_choice(getattr(args, "local_ai_plan", None)) == "auto"
        if len(plans) > 1:
            distribution = ("빈자리 있는 계정끼리 우선순위 비중 " + ":".join(map(str, range(len(plans),0,-1)))
                            if rotate else "직접 고른 계정 우선")
            cli.log(f"  쪽 나눠 보내기: {names} 구독 · {distribution} · 계정마다 {per}쪽씩 함께")
        else:
            if getattr(args, "local_ai_split", False):
                cli.log("  쪽 나눠 보내기: 로그인된 구독 계정이 하나뿐이라 한 계정으로 진행합니다.")
            cli.log(f"  AI 판독·검증: {names} 구독이 {per}쪽씩 함께 맡습니다.")
        cli.log("  엔진은 AI의 답을 기다리지 않고 다음 쪽을 계속 읽고, 엔진을 마친 쪽은 차례대로 AI 판독·검증을 받습니다.")
        return cls(crews, stage, failed_page, cli.log, rotate=rotate, should_stop=cli.CANCEL.is_set,
                   watch_chatgpt=True, **hooks)

    def waiting(self) -> int:
        with self._cond:
            return len(self._queue)

    def submit(self, number, started, review, extras, saved=False):
        with self._cond:
            self._queue.append((number, started, review, extras, saved))
            self._cond.notify_all()

    def _take_slot(self, crew):
        """Return the next usable slot for this worker, under the queue lock."""
        usable = [other["session"] is None or other["session"].ready_for_page(split=len(self.crews) > 1)
                  for other in self.crews]
        if not any(usable):
            return self._next_slot                  # record pending work when no account can answer
        for step in range(len(self._schedule)):
            slot = (self._next_slot + step) % len(self._schedule)
            index = self._schedule[slot]
            if usable[index] and self.crews[index]["idle"] > 0:
                return slot if index == crew["index"] else None
        return None

    def _work(self, crew):
        while True:
            with self._cond:
                while True:
                    if self.stopped or self.should_stop():
                        self.stopped = True
                        return
                    if self._queue:
                        slot = self._take_slot(crew)
                        if slot is not None:
                            break
                    elif self._closed:
                        return
                    self._cond.wait(2.0)
                job = self._queue.popleft()
                crew["idle"] -= 1
                if self._rotate:
                    self._next_slot = (slot + 1) % len(self._schedule)
                self._cond.notify_all()              # the next connection in order may take the page after this one
            try:
                if self.stopped or self.should_stop():
                    self.stopped = True              # [중지]: pages still waiting keep their engine reading
                else:
                    self._run(crew, job)
            finally:
                with self._cond:
                    crew["idle"] += 1
                    self._cond.notify_all()

    def _run(self, crew, job):
        number = job[0]
        from ab_ocr.ocr import OCRCancelled
        from .comparison_ai import AIReviewCancelled
        if self.on_start is not None:
            self.on_start(number)
        try:
            if crew["session"] is not None:
                crew["session"].prepare_page(split=len(self.crews) > 1)
            if len(self.crews) > 1 and crew["session"].available:
                self.log(f"  {number}쪽: AI 담당 계정 · {crew['session'].short()}")
            number, started, review, extras, saved = job
            if self.stage(number, started, review, extras, crew["session"], crew["reader"], saved) == 130:
                self.stopped = True
            elif self.on_done is not None:
                self.on_done(number)
        except (OCRCancelled, AIReviewCancelled, KeyboardInterrupt):
            self.stopped = True
        except Exception as exc:                     # the page's own failure, as the inline loop reports it
            self.failed_page(number, exc)

    def finish(self):
        """Wait for every page handed in; 130 when the run was stopped."""
        if self.should_stop():
            self._quota_stop.set()
        with self._cond:
            self._closed = True
            self._cond.notify_all()
        for thread in self._threads:
            thread.join()
        self._quota_stop.set()
        if self._quota_thread is not None:
            self._quota_thread.join()
        return 130 if self.stopped else None


def _review_allowance(planned, settings, cap, used, reserved):
    """Called under the tally lock. Zero cap is subscription-only and unbounded."""
    return min(planned, settings.max_calls,
               settings.max_total_output_tokens // settings.max_output_tokens,
               max(0, cap - used - reserved) if cap else settings.max_calls)


def _memory_pressure():
    from .automatic_ocr import memory_status
    status = memory_status() or {}
    return (status.get('available_commit_mb', 4096) < 1024
            or status.get('available_physical_mb', 4096) < 256)


def _settings(args, remaining):
    from .comparison_ai import AISettings
    from .providers import default_model
    remaining = remaining or 100  # zero means no run-wide subscription cap; windows stay bounded
    return AISettings(args.reader, getattr(args, "model", None) or default_model(args.reader),
                      max_calls=max(1, min(100, remaining)), max_output_tokens=4096,     # per page plan
                      max_total_output_tokens=max(1, min(100, remaining)) * 4096)


def command(args, *, work=None, doc=None):
    from ab_ocr.ocr import render_cache
    from . import cli
    from .engine_pool import EnginePool
    # One page is rendered for every engine, the preview and the review crops;
    # engine workers stay loaded across pages.
    from ab_ocr.ocr import input_enhancement
    from ab_ocr.local_engines import easyocr_runtime, fast_horizontal_ocr, line_verification
    enhancement = getattr(args, "local_enhance", "none")
    enhancement = None if enhancement in (None, "none") else enhancement
    verify = getattr(args, "local_line_verification", False)
    fast = getattr(args, "ocr_strategy", None) in FAST_STRATEGIES and not getattr(args, "local_review_only", False)
    with render_cache(), input_enhancement(enhancement), line_verification(verify), fast_horizontal_ocr(fast and not verify), \
            easyocr_runtime(getattr(args, "local_easyocr_threads", 2) or 2, getattr(args, "local_easyocr_batch", 1) or 1), \
            EnginePool(should_stop=cli.CANCEL.is_set, log=cli.log) as pool:
        return _command(args, work=work, doc=doc, pool=pool)


def _command(args, *, work=None, doc=None, pool=None):
    from . import cli
    from .automatic_ocr import run_automatic_page
    from .comparison import ComparisonError
    from .comparison_storage import ProjectStorageError
    from .comparison_ai import AIReviewError, AIReviewCancelled, ReviewPolicy, prepare_plan, run_plan
    from .pdfio import open_pdf, parse_pages
    from .readers import environment_key, split_keys
    from ab_ocr.ocr import OCRError, OCRCancelled

    pdf_only = getattr(args, "pdf_only", False)
    bad = None if pdf_only else cli._check_document(args)
    if bad:
        cli.log(bad)
        return 2
    resolved = _resolve_ai_defaults(args, cli)
    cap = getattr(args, "local_ai_max_calls", 20)
    if type(cap) is not int or not 1 <= cap <= 100:
        cli.log("의심 구간 API 호출 상한은 1~100이어야 합니다.")
        return 2
    command_cap = getattr(args, "max_calls", 0)
    cap = min(cap, command_cap) if command_cap else cap
    api_cap = cap                       # paid calls: also the budget when a run falls back from the plans to the key
    if getattr(args, "local_ai_auth", None) == "subscription":
        cap = getattr(args, "local_ai_subscription_calls", 0)
        if type(cap) is not int or cap < 0:
            cli.log("구독 구간 검증 상한은 0(자체 상한 없음) 이상의 정수여야 합니다.")
            return 2
    owned_doc = doc is None
    doc = doc or open_pdf(args.pdf)
    split = None
    project = None
    storage_fault=[]
    try:
        work = work or Work(args.pdf, getattr(args, "work", None))
        work.ensure_source(args.dpi)
        pages = parse_pages(getattr(args, "pages", None), len(doc))
        work.ensure_pages(pages, len(doc))
        cli.resolve_mode(args, doc, work)
        fast_mode = getattr(args, "ocr_strategy", None) in FAST_STRATEGIES and not getattr(args, "local_review_only", False)
        quick = fast_mode and args.mode == "horizontal_zh" and args.local_profile != "mixed"
        collect_layout = fast_mode and not pdf_only and not getattr(args, "no_document_editing", False) and Path(args.pdf).suffix.lower() == ".pdf"
        layout_lines = {}
        if collect_layout:
            from .source_typography import source_line_blocks
            cli.log("  판독과 함께 원본 줄 위치·글자 크기를 수집합니다. HWPX는 전체 판독과 배치 정리가 끝난 뒤 만듭니다.")
        if quick and pool is not None:
            pool.max_requests = 64
        if getattr(args, "print_offset", None) is None:
            state = work.load_state()
            args.print_offset = state.get("print_offset")
            if args.print_offset is None:
                args.print_offset = cli.detect_print_offset(doc)
        source = _source(work, doc)
        resume = getattr(args, "local_resume", "unfinished") or "unfinished"
        project = _project(source, work, lazy_pages=resume == "continue" and not getattr(args, "local_review_only", False))
        state = work.load_state()
        state["local_first_project"] = str(project.project_path)
        work.save_state(state)
        cli.log(f"▶ 로컬 우선 인식: {len(pages)}쪽 · 의심 구간 검증 상한 {str(cap) + '회' if cap else '없음(서비스 한도 적용)'} · "
                f"쪽 AI 판독 {getattr(args, 'local_ai_page', 'off')} · "
                f"검증 제안 자동 반영 {'켬' if getattr(args, 'local_ai_accept', False) else '끔'} · 유료 API 상한 {api_cap}회")
        review_enabled = bool(getattr(args, "local_ai_review", True))
        # One snapshot of the confusion ledger for the whole run: every page's plan sees the same
        # learned characters, and this run's corrections join the ledger only when it ends.
        from .confusion_memory import load_for_review
        learned = load_for_review(cli.log) if getattr(args, "local_learned_confusions", True) else None
        if learned is not None:
            cli.log(f"  엔진 혼동 장부: 이 엔진들이 자주 틀린 글자 {sum(r.risky for r in learned.rows)}개를 신뢰도와 상관없이 검증에 보탭니다")
        from . import subscriptions
        subscriptions.reset_exhausted()     # plan limits may have reset since the last run
        api = _api_fallback(args, api_cap, environment_key, split_keys)
        keys = split_keys(args.reader, environment_key(args.reader)) if review_enabled else []
        key = keys[0] if keys else ""
        session = None
        if review_enabled:
            # A signed-in plan's own CLI (personal plan, no API charge): Claude Code, Codex CLI or Gemini CLI.
            from .claude_subscription import auth_mode
            requested = getattr(args, "local_ai_auth", None)
            plan, note = subscriptions.choose(requested, args.reader, key, getattr(args, "local_ai_plan", None))
            if plan:
                chosen = getattr(args, "model", None) or ""
                args.reader = subscriptions.PROVIDERS[plan]
                args.model = subscriptions.model(plan, chosen if subscriptions.accepts_model(plan, chosen) else None)
                if plan == "gemini" and getattr(args, "local_gemini_model", None):
                    args.model = args.local_gemini_model
                key = subscriptions.KEYS[plan]
                session = subscriptions.Session(plan, getattr(args, "local_ai_plan", None), api=api, gemini_model=getattr(args, "local_gemini_model", None), log=cli.log,
                                                should_stop=cli.CANCEL.is_set)
                subscriptions.activate(session)     # the window's [다음 구독으로 넘기기] switches this connection
            elif auth_mode(requested) == "subscription":
                if api is not None:         # no usable plan: straight to the API key, within the paid cap
                    cap = api_cap
                    note = (note + " → " if note else "") + f"API 키({api.provider} · 모델 {api.model})로 검증합니다."
                else:
                    key = ""
            if note:
                cli.log("  " + note + (f" · 모델 {args.model}" if plan else ""))
        failed, incomplete, completed = [], [], []
        import threading
        tally, tally_lock = {"calls": 0}, threading.Lock()                # AI calls this run (split runs add from workers)
        cli.LOCAL_API_CALLS = 0
        cli.LOCAL_PAGE_CALLS = 0
        summaries = {}
        saver = _Checkpoint(project)
        def easyocr_wanted():
            """EasyOCR: off on horizontal pages by default (2026-11 measurements: Han characters more
            accurate without it on 4 documents, 27-35% faster), kept on vertical pages (not measured)."""
            choice = getattr(args, "local_easyocr", None)
            return (args.mode != "horizontal_zh") if choice is None else bool(choice)

        def engine_options(skip=()):
            """Per page: workers resident so far, and how many engines may run at once."""
            if pool is None:
                return {}
            from .automatic_ocr import memory_status
            from .engine_pool import engine_parallelism
            requested = getattr(args, "local_engine_parallel", 0) or 0
            resident = pool.resident()
            from .automatic_ocr import gpu_engines_enabled
            mode = getattr(args, "local_enhance", "none")
            exclude = set(getattr(args, "local_exclude_engines", None) or ()) | set(skip)
            if quick:
                exclude.update(pool.failed_gpu_engines())
            if not easyocr_wanted():
                exclude.add("easyocr")
            exclude = tuple(sorted(exclude))
            choice = getattr(args, "local_gpu_engines", None)
            gpu = gpu_engines_enabled() if choice is None else bool(choice)
            return {"recognizer": pool.recognize, "resident": resident, "gpu_engines": gpu,
                    **({"exclude": exclude} if exclude else {}),
                    **({"input_enhancement": mode} if mode not in (None, "none") else {}),
                    "parallel": lambda engines: engine_parallelism(engines, requested, resident=resident,
                                                                   memory=memory_status())}
        from .local_options import FastPlan, apply_votes, is_blank
        defaults = {"engine_plan": ("local_engine_plan", "all"), "vote": ("local_vote", False),
                    "columns": ("local_columns", True), "textlayer_witness": ("local_textlayer_witness", False),
                    "skip_blank": ("local_skip_blank", False), "enhance": ("local_enhance", "none"),
                    "ai_batch": ("local_ai_batch", 1), "line_verification": ("local_line_verification", False),
                    "exclude_engines": ("local_exclude_engines", []), "easyocr": ("local_easyocr", None),
                    "easyocr_threads": ("local_easyocr_threads", 2),
                    "easyocr_batch": ("local_easyocr_batch", 1), "ai_accept": ("local_ai_accept", False),
                    "ai_detail_tile": ("local_ai_detail_tile", 0), "korean_forms": ("local_korean_forms", True),
                    "ai_page": ("local_ai_page", "off")}
        options = {}
        for option, (name, default) in defaults.items():
            default = resolved.get(option, default)                 # automatic choices are not trial options
            value = getattr(args, name, default)
            if value is not None and (type(value), value) != (type(default), default):
                options[option] = value
        if options:
            cli.log("  선택 옵션(측정 전 시험 기능): " + ", ".join(f"{k}={v}" for k, v in options.items()))
        fast = FastPlan() if not quick and options.get("engine_plan") == "fast" else None

        def witness_text(number):
            from .pdfio import text_layer
            try:
                with cli.DOC_LOCK:
                    return text_layer(doc, number).plain()
            except Exception:
                return None

        reference_ocr = None
        if getattr(args, 'local_reference_hwpx', None):
            from .reference_ocr import ReferenceOCR
            reference_ocr = ReferenceOCR(args.local_reference_hwpx)
            cli.log(f'  참고 OCR: {reference_ocr.path.name} · 문단 대응 후 읽기 문맥으로만 사용 · 원본 이미지 검증 유지')
        page_reader = None if getattr(args, 'local_review_only', False) else _ai_page_reader(args, project, work, cli, session, api)
        def ai_stage(number, started, review, extras, stage_session, stage_reader, saved=False):
            """The AI part of one page after the engines: checkpoint, AI page reading beside the review
            (review plan, review, accepted proposals), record. Inline, or on a worker of a signed-in plan
            (_AiWorkers) while the engines read on. Returns 130 when stopped."""
            cli.page_event(number, 'ai-review')
            cli.log(f"  {number}쪽: AI 판독·미완료 검증 점검(완료 응답은 재사용)")
            # Checkpoint local output BEFORE preparing or sending any API call.
            if not saved:
                _save_record(project, number, args, work, started, review, extras=extras)
            # The page reading and the review do not depend on each other (they meet in the record): both go out now.
            reading = _Side(stage_reader, number) if stage_reader is not None else None
            try:
                stopped = review_stage(number, started, review, extras, stage_session)
            finally:
                if reading is not None:
                    extras["ai_page"] = reading.result()
            if stopped or cli.CANCEL.is_set():
                return 130
            if not work.source_matches(args.dpi):
                raise ComparisonError("처리 중 원본이 변경되어 출력 기록을 갱신하지 않았습니다.")
            _save_record(project, number, args, work, started, review, render=False, extras=extras)
            summaries[str(number)] = review
            completed.append(number)
            from .state import review_page_mark
            cli.page_event(number, review_page_mark(review))
            cli.log(f"  {number}쪽: {REVIEW_STATUS_LABELS.get(review['status'], review['status'])} · "
                    f"로컬 엔진 {(_selected(project, number) or {}).get('engine', '')}")
            return None

        def memory_stop():
            if not _memory_pressure():
                return False
            cli.log('메모리 여유 부족으로 안전 중지: 저장된 체크포인트를 보존하고 새 OCR·AI 요청을 멈춥니다.')
            cli.CANCEL.set()
            return True

        def review_stage(number, started, review, extras, stage_session):
            """The review of one page's suspicious spans. True when stopped."""
            if memory_stop():
                return True
            settings = _settings(args, cap)
            try:
                plan = prepare_plan(project, [number], ReviewPolicy(selective=True, batch_size=getattr(args, "local_ai_batch", 1) or 1,
                                                                    detail_tile=getattr(args, "local_ai_detail_tile", 0) or 0),
                                    settings=settings,
                                    should_stop=cli.CANCEL.is_set, confusions=learned,
                                    page_context_fallback=bool(getattr(args, 'local_review_only', False)),
                                    target_limit=2000 if getattr(args, 'local_review_only', False) else None,
                                    auto_split_targets=True, reference_ocr=reference_ocr)
            except AIReviewError as exc:
                review.update(status="pending", error=str(exc))
                if review_enabled:
                    cli.log(f"  {number}쪽: AI 검증 보류 — {exc}")
                if review_enabled:
                    incomplete.append(number)
            else:
                review.update(copy.deepcopy(plan.summary))
                plan_path = work.dir / "local-first-review" / f"p{number:03d}-{plan.fingerprint[:24]}.json"
                plan_path.parent.mkdir(exist_ok=True)
                write_atomic(plan_path, json.dumps(plan.to_dict(), ensure_ascii=False, indent=2))
                review["plan_path"] = str(plan_path)
                unresolved = bool(plan.summary.get("unlocalized_targets"))
                # Engines that failed on this page leave fewer readings to compare: a page that then has
                # nothing to send is not "checked", it is unchecked evidence (Codex review, 1.2.17).
                weakened = bool(review.get("engine_errors")) and review.get("single_reading")
                if plan.summary["calls"] == 0:
                    review["status"] = "needs-local-review" if unresolved or weakened else "no-api-needed"
                    if weakened:
                        review["reason"] = "엔진 실패로 남은 판독이 하나뿐이고 의심 구간도 찾지 못함"
                    if (unresolved or weakened) and review_enabled:
                        incomplete.append(number)
                elif not review_enabled or not key or (
                        stage_session is not None and not stage_session.available):
                    review["status"] = "pending"
                    review["reason"] = ("AI 검증 꺼짐" if not review_enabled else "API 키 없음" if not key else
                                        stage_session.stop_reason() if stage_session is not None and not stage_session.available
                                        else "이번 실행 호출 상한 도달")
                    if review_enabled:
                        cli.log(f"  {number}쪽: AI 검증 보류 — {review['reason']} (이 쪽 {plan.summary['calls']}회 필요)")
                    if review_enabled:
                        incomplete.append(number)
                else:
                    from .comparison_ai import review_window
                    with tally_lock:
                        allowance = _review_allowance(plan.summary['calls'], settings, cap,
                                                      tally['calls'], tally.get('reserved', 0))
                        tally['reserved'] = tally.get('reserved', 0) + allowance
                    if not allowance:
                        reason = (f"구간 검증 호출 상한 {cap}회 도달 "
                                  f"(완료 {tally['calls']}회 · 진행 예약 {tally.get('reserved', 0)}회; 쪽 판독 별도)")
                        review.update(status='pending', reason=reason)
                        incomplete.append(number)
                        cli.log(f'  {number}쪽: AI 검증 보류 — {reason} · 남은 묶음은 보존합니다.')
                        return False
                    plan = review_window(plan, allowance)
                    deferred = plan.summary.get('deferred_calls', 0)
                    if plan.summary.get('automatic_partitions'):
                        cli.log(f"  {number}쪽: 검증 대상 {plan.summary['targets']}개를 {plan.summary['automatic_partitions']}개 구간으로 자동 분할 · 요청당 최대 {plan.policy.batch_size}개")
                    cli.log(f"  {number}쪽: 의심 구간 {plan.summary['targets']}개 · "
                            + (f"AI 호출 최대 {plan.summary['calls']}회({stage_session.short()})" if stage_session is not None
                               else f"API 최대 {plan.summary['calls']}회"))
                    review["status"] = "reviewing"
                    _save_record(project, number, args, work, started, review, render=False, extras=extras)
                    checkpoints = [0]
                    def checkpoint(_value):
                        checkpoints[0] += 1
                        cli.LOCAL_API_CALLS = tally["calls"] + checkpoints[0]
                        saver.save()
                    result = None
                    try:
                        result = run_plan(project, plan, api_key=key, should_stop=cli.CANCEL.is_set, on_result=checkpoint,
                                          client_factory=(lambda _settings, _key: stage_session) if stage_session is not None else None,
                                          parallel=(getattr(args, "local_ai_parallel", 3) or 1) if stage_session is not None else 1,
                                          cancel_requests=True)
                    finally:
                        with tally_lock:
                            tally['reserved'] -= allowance
                            tally['calls'] += result['calls'] if result is not None else checkpoints[0]
                    if getattr(args, "local_ai_accept", False) and not getattr(args, 'local_review_only', False) and not deferred:
                        review["ai_accept"] = _accept_ai(project, number)
                        cli.log(f"  {number}쪽: AI 제안 반영 {review['ai_accept']['accepted']}개 · 보류 {review['ai_accept']['held']}개")
                    cli.LOCAL_API_CALLS = tally["calls"]
                    pending_proposals = sum(p.get('decision') == 'pending' and not p.get('stale')
                                            for p in project.ai_proposals(number))
                    review.update(status="failed" if result["failed"] else "needs-local-review" if unresolved or pending_proposals else "reviewed",
                                  pending_proposals=pending_proposals, outcome=result)
                    review.pop('deferred_calls', None)
                    if deferred:
                        review.update(status='pending', reason='다음 구간 검증 묶음 대기', deferred_calls=deferred)
                    if result["failed"] or unresolved or pending_proposals:
                        incomplete.append(number)
                    saver.save()
                    if result["cancelled"]:
                        review["status"] = "cancelled"
                        _save_record(project, number, args, work, started, review, render=False, extras=extras)
                        return True
                    if deferred and not result['failed']:
                        _save_record(project, number, args, work, started, review, render=False, extras=extras)
                        return review_stage(number, started, review, extras, stage_session)
            return False

        finished = [0]
        def page_finished(_number=None):
            """The progress bar counts pages that are finished (their AI part too), not pages the engines have read."""
            with tally_lock:
                finished[0] += 1
                count = finished[0]
            cli.progress("read", count, len(pages))

        def page_failed(number, exc):
            from .comparison_storage import ProjectStorageError
            cause=exc;seen=set()
            while cause is not None and id(cause) not in seen:
                seen.add(id(cause))
                if isinstance(cause,ProjectStorageError):
                    if not storage_fault:storage_fault.append(cause)
                    cli.CANCEL.set()
                    cli.log('  저장소 오류로 안전 중지: 다음 쪽 판독·새 AI 호출을 보내지 않습니다.')
                    break
                cause=cause.__cause__ or cause.__context__
            failed.append(number)
            cli.page_event(number, "fail")
            cli.log(f"  {number}쪽 오류: {exc}")
            page_finished()

        split = _AiWorkers.start(args, session, api, project, work, cli, ai_stage, page_failed, review_enabled,
                                 api_parallel=fast_mode and bool(key), on_done=page_finished)
        if resume != "unfinished":
            cli.log("  이어서 할 때: " + {"continue": "앞선 쪽은 점검하지 않고 바로 이어갑니다",
                                         "all": "앞선 쪽을 모두 다시 점검한 뒤 이어갑니다"}.get(resume, resume))
        engine_read = 0
        for index, number in enumerate(pages, 1):
            memory_stop()
            if cli.CANCEL.is_set():
                return 3 if storage_fault else 130
            if collect_layout:
                with cli.DOC_LOCK:
                    layout_lines[number] = source_line_blocks(doc[number - 1])
            started = time.perf_counter()
            extras = {"columns": bool(getattr(args, "local_columns", True)), "options": options or None,
                      "witness": witness_text(number) if options.get("textlayer_witness") else None}
            old = work.load_record(number)
            if resume != 'continue' and ((old or {}).get('local_first', {}).get('review') or {}).get('status') == 'completed':
                cli.log(f'  {number}쪽: 이전 버전의 검증 상태 재확인 — 저장된 응답 재사용 · 교정 이력 보존')
            if getattr(args, 'local_review_only', False):
                # This mode never calls recognition, page transcription or
                # auto-accept; even derived AI-merged output remains untouched.
                first = (old or {}).get('local_first') or {}
                done = first.get('review') or {}
                if not first or _human_review_only(done) or (done.get('status') in {'reviewed', 'no-api-needed', 'blank-page'}
                                 and not done.get('unlocalized_targets')):
                    if _human_review_only(done):
                        incomplete.append(number)
                        cli.page_event(number, 'review_pending')
                        cli.log(f'  {number}쪽: 사람 검수 대기 — 자동 검증 재실행 안 함 · 기존 기록 보존')
                    if not first:
                        incomplete.append(number)
                    summaries[str(number)] = done
                    if first:
                        completed.append(number)
                    page_finished()
                    continue
                if first.get('source_sha256') != project.source_sha256:
                    raise ComparisonError('저장된 판독과 원본이 달라 재검증하지 않았습니다.')
                review = copy.deepcopy(done)
                review['status'] = 'pending'
                if split is not None:
                    cli.page_event(number, 'queued')
                    split.submit(number, started, review, extras, False)
                else:
                    if ai_stage(number, started, review, extras, session, None) == 130:
                        return 130
                    page_finished()
                continue
            if _protected_record(work, number, old) and not getattr(args, "force", False):
                cli.log(f"  {number}쪽: 기존 판독·사람 교정 보존(다시 인식하려면 강제 재판독)")
                cli.page_event(number, "record")
                page_finished()
                completed.append(number)
                continue
            if (resume == "continue" and old and old.get("pipeline") == POLICY and old.get("local_first")
                    and not getattr(args, "force", False)):
                # 바로 이어가기: an earlier page is left as recorded, unchecked (its pending review too).
                done = old["local_first"].get("review") or {}
                summaries[str(number)] = done
                if review_enabled and (done.get("status") not in RESUME_DONE or done.get('status') == 'needs-local-review'):
                    incomplete.append(number)
                completed.append(number)
                from .state import review_page_mark
                cli.page_event(number, review_page_mark(done, success='record'))
                cli.log(f"  {number}쪽: 앞선 기록 그대로 — 바로 이어가기(점검 안 함) · "
                        f"{REVIEW_STATUS_LABELS.get(done.get('status'), done.get('status') or '')}")
                page_finished()
                continue
            cli.page_event(number, "checking" if old else "ocr")
            cli.log(f"  {number}쪽: " + ("저장 기록·처리 조건 확인" if old else "새 쪽 OCR 시작"))
            review = {"status": "local-complete", "targets": 0, "calls": 0}
            try:
                if not work.source_matches(args.dpi):
                    raise ComparisonError("처리 중 원본이 변경되었습니다. 다시 열어주세요.")
                if options.get("skip_blank"):
                    from ab_ocr.ocr import render_page
                    with render_page(project.source_path, number, args.dpi, should_stop=cli.CANCEL.is_set) as image:
                        blank = is_blank(image)
                        if blank:
                            cli._save_png_atomic(image, work.page_image_path(number))
                    if blank:
                        write_atomic(work.pages_dir / f"p{number:03d}.src.txt", "로컬 OCR 전체 쪽 렌더링")
                        if work.load_record(number):
                            work.backup_record(number)
                        work.save_record(number, _blank_record(project, number, args, work, started))
                        work.set_status(number, "1차 OCR 완료")
                        summaries[str(number)] = {"status": "blank-page", "targets": 0, "calls": 0}
                        completed.append(number)
                        cli.page_event(number, "done")
                        cli.log(f"  {number}쪽: 빈 쪽 — 엔진 건너뜀")
                        page_finished()
                        continue

                def read(only=None, skip=()):
                    return run_automatic_page(project, number, profile=args.local_profile,
                        direction="horizontal" if args.mode == "horizontal_zh" else "vertical", dpi=args.dpi,
                        engine_root=getattr(args, "local_engine_root", None), force=getattr(args, "force", False),
                        should_stop=cli.CANCEL.is_set, on_result=saver.maybe,
                        on_recognition=lambda: cli.page_event(number, 'reocr' if old else 'ocr'),
                        **engine_options(skip), **({"quick": True} if quick else {}),
                        **({"only": only} if only else {}))
                only = fast.engines() if fast is not None else None
                outcome = read(only)
                if quick and not outcome.get("cancelled") and not (outcome.get("selection") or {}).get("selected_engine"):
                    tried = set(outcome.get("errors", ())) | set(outcome.get("completed", ())) | set(outcome.get("skipped", ()))
                    tried.discard("automatic")
                    if tried:
                        cli.log(f"  {number}쪽: {', '.join(sorted(tried))} 판독이 비어 다른 로컬 엔진으로 다시 읽습니다.")
                        outcome = read(skip=tried)
                if only and not outcome.get("cancelled") and FastPlan.needs_fallback(outcome.get("selection")):
                    cli.log(f"  {number}쪽: 줄인 엔진({', '.join(only)})의 점수가 낮아 모든 엔진으로 다시 읽음")
                    only = None
                    outcome = read()
                if fast is not None and not outcome.get("cancelled"):
                    fast.observe(outcome.get("selection"), narrowed=bool(only))
                    if fast.seen == fast.sample and not only and fast.engines():
                        cli.log(f"  표본 {fast.sample}쪽 뒤로는 {', '.join(fast.engines())} 엔진만 먼저 읽습니다(--local-engine-plan fast)")
                if options.get("vote") and not outcome.get("cancelled"):
                    voted = apply_votes(project, number)
                    if voted:
                        cli.log(f"  {number}쪽: 엔진 다수결로 {voted}곳 채택(초록, 비교·교정 창에서 되돌릴 수 있음)")
                saver.save()
                if outcome.get("cancelled"):
                    return 130
                errors = {engine: str(message) for engine, message in (outcome.get("errors") or {}).items()}
                if errors:
                    # Reported, not hidden: the page goes on with the engines that did read it.
                    review["engine_errors"] = errors
                    review["single_reading"] = len([r for r in (outcome.get("selection") or {}).get("scores", [])
                                                    if r.get("characters") and r.get("score", 0) > 0]) <= 1
                    cli.log(f"  {number}쪽: 엔진 실패 — " + " · ".join(f"{engine}: {message[:120]}" for engine, message in errors.items())
                            + " (남은 엔진의 판독으로 계속)")
                from .comparison import _run_complete, _run_profile
                selected = _selected(project, number)
                successful = set(outcome.get("completed", [])) | set(outcome.get("skipped", []))
                expected = (args.local_profile, "horizontal" if args.mode == "horizontal_zh" else "vertical")
                # A baseline kept to protect human or accepted AI work stays even when its engine did not run
                # this time (e.g. GLM-OCR read the page, then the GPU engine was switched off): it is a complete
                # reading stored under the same conditions, checked below.
                page = project.page(number)
                kept = (selected is not None and selected["engine"] not in successful and _run_complete(selected)
                        and (page["corrected_text"] is not None or bool(page["edits"]) or page["reviewed"]
                             or page.get("baseline_choice") == "manual"))
                why = ("기준 판독이 없음" if selected is None
                       else f"기준 판독 엔진 {selected['engine']}이 이번에 돌지 않았고 고친 내용으로 고정된 판독도 아님"
                       if selected["engine"] not in successful and not kept
                       else f"기준 판독({selected['engine']})의 문서 종류·방향 {'/'.join(map(str, _run_profile(selected)))} ≠ "
                            f"이번 {'/'.join(expected)}" if _run_profile(selected) != expected
                       else f"기준 판독({selected['engine']})의 해상도 {selected.get('request', {}).get('dpi')} ≠ 이번 {args.dpi}"
                       if selected.get("request", {}).get("dpi") != args.dpi else "")
                if why:
                    ran = ", ".join(sorted(successful)) or "없음"
                    raise ComparisonError(f"이번 조건에서 성공한 로컬 기준 판독이 없습니다({why} · 이번에 읽은 엔진: {ran}). "
                                          "이전 판독은 보존했습니다.")
                if kept:
                    cli.log(f"  {number}쪽: 기준 판독은 고친 내용을 지키려고 고정한 {selected['engine']} 판독"
                            "(이번에 돌지 않은 엔진 — 저장된 판독을 그대로 씀)")
                # A page finished under the same conditions (no engine ran again, same key, review done):
                # nothing to crop, plan, read or write again.
                if (resume == "unfinished" and old and not getattr(args, "force", False) and not outcome.get("completed")
                        and not review.get("engine_errors")      # a failure this time is judged again, not copied
                        and _resume_done(old, args, _resume_key(args, selected["id"], project.text(number), options),
                                         review_enabled)):
                    done = old["local_first"]["review"]
                    summaries[str(number)] = done
                    if review_enabled and (done.get("status") == "needs-local-review" or done.get("unlocalized_targets")):
                        incomplete.append(number)
                    completed.append(number)
                    from .state import review_page_mark
                    cli.page_event(number, review_page_mark(done, success='record'))
                    cli.log(f"  {number}쪽: " + ("사람 검수 대기 — 자동 검증 재실행 안 함 · 기존 기록 보존" if _human_review_only(done)
                            else f"끝난 쪽 — 같은 조건이라 건너뜀 · {REVIEW_STATUS_LABELS.get(done.get('status'), done.get('status'))}"))
                    page_finished()
                    continue
                if split is not None:
                    # The engines' reading is on disk before the page waits for its AI turn.
                    _save_record(project, number, args, work, started, review, extras=extras)
                    cli.page_event(number, "queued")
                    split.submit(number, started, review, extras, True)
                    engine_read += 1
                    if engine_read % 10 == 0:
                        cli.log(f"  엔진 판독 {index}/{len(pages)}쪽째 · AI 차례를 기다리는 쪽 {split.waiting()}")
                elif ai_stage(number, started, review, extras, session, page_reader) == 130:
                    return 130
                else:
                    page_finished()
            except (OCRCancelled, AIReviewCancelled, KeyboardInterrupt):
                return 130
            except (OCRError, ComparisonError, AIReviewError, OSError, ValueError) as exc:
                page_failed(number, exc)
        if split is not None:
            if fast_mode and pool is not None:
                pool.close()
            if split.waiting():
                cli.log(f"  엔진 판독을 모두 마쳤습니다. AI 판독·검증을 기다리는 쪽 {split.waiting()} — 끝날 때까지 이어갑니다"
                        "([중지]를 누르면 끝난 쪽까지 저장하고, 남은 쪽은 다음 실행에서 이어 검증).")
            if split.finish() == 130:
                return 3 if storage_fault else 130
        state = work.load_state()                    # after the workers: their pages' statuses are in it
        calls = tally["calls"]
        state["local_first_last_run"] = {"calls": calls, "cap": cap, "failed_pages": failed,
                                         "incomplete_review_pages": incomplete, "pages": summaries}
        work.save_state(state)
        completed = [n for n in pages if work.load_record(n)]
        if not work.source_matches(args.dpi):
            cli.log("원본이 변경되어 문서 출력을 중단했습니다. 완료 기록은 보존했습니다.")
            return 2
        if not completed:
            return 3 if failed else 2
        if getattr(args, 'local_review_only', False):
            cli.log(f'미완료 검증 끝: 구간 검증 {calls}회 · 상한 {cap or "없음"} · 기존 OCR·교정문·출력 문서 보존')
            return 3 if failed else 4 if incomplete else 0
        # Local review is the only model path; document assembly must stay offline.
        build_args = copy.copy(args)
        build_args.no_context_model = True
        if layout_lines:
            build_args._source_layout_lines = layout_lines
        if pdf_only:
            rc = cli.make_textlayer(build_args, work, completed)
        elif getattr(args, 'defer_recheck_hwpx', False):
            rc = 0
        else:
            if getattr(args, 'source_review', False) and pool is not None:
                pool.close()
            rc = cli.cmd_build(build_args, work, doc)
        cli.cmd_review(build_args, work, doc)
        cli.cmd_report(build_args, work, doc)
        cli.log(f"끝: 로컬 우선 인식 · 이번 실행 구간 검증 {calls}회 · 상한 {cap or '없음'} · 비교 프로젝트 {project.project_path}")
        cli.log("AI 수정 제안은 비교·교정 창에서 채택·기각합니다. 원시 판독과 사람 교정은 보존됩니다.")
        return rc or (3 if failed else 4 if incomplete else 0)
    except ProjectStorageError as exc:
        storage_fault.append(exc)
        cli.CANCEL.set()
        cli.log(str(exc))
        return 3
    finally:
        if split is not None:
            split.finish()                                   # pages in flight on the plans' workers (they stop at [중지])
        if storage_fault:
            _preserve_storage_failure(work, project, storage_fault[0], cli.log)
        if project is not None and not storage_fault and not cli.CANCEL.is_set():
            from .confusion_memory import harvest_quietly
            harvested = harvest_quietly(project, cli.log)
            if harvested and harvested["pairs"]:
                cli.log(f"  엔진 혼동 장부 갱신: 확인한 {harvested['pages']}쪽 · 엔진 판독과 확정 글이 다른 자리 {harvested['pairs']}곳")
            # Line images with their confirmed text, for fine-tuning a local engine later (kept on this PC).
            from .glyph_samples import collect_quietly
            collect_quietly(project, cli.log, should_stop=cli.CANCEL.is_set)
        from . import subscriptions
        subscriptions.deactivate()
        if owned_doc:
            doc.close()
