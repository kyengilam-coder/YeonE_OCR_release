"""Local recognition followed by bounded, selective AI review.

The comparison project owns immutable local readings and AI proposals. Standard
Work records are derived views for the existing document/PDF/review builders.
No full-page AI reader, third-pass reader or document context model is invoked.
"""
from __future__ import annotations

import copy
import hashlib
import json
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


def _project(source, work):
    from ab_ocr.ocr import stable_file_sha256
    from .comparison import ComparisonProject
    path = work.dir / "comparison" / ("local-first-" + stable_file_sha256(source)[:24] + ".yeone-compare.json")
    if path.exists():
        return ComparisonProject.load(path, source_path=source)
    project = ComparisonProject.open(source)
    path.parent.mkdir(parents=True, exist_ok=True)
    project.save(path)
    return project


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
        self.project.save()
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


def _local_grades(project, number, run, current, witness=None):
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
    from .comparison import ComparisonError
    from .comparison_ai import LOW_CONFIDENCE_THRESHOLDS, _word_spans
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
        for tag, _a0, _a1, b0, b1 in difflib.SequenceMatcher(None, base, current, autojunk=False).get_opcodes():
            if tag != "equal":
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
    page = project.page(number)
    for suggestion in page.get("suggestions", []):
        if suggestion.get("decision") == "pending" and suggestion.get("kind") != "whitespace":
            span = mapped(suggestion["start"], suggestion["end"])
            if span:
                paint(*span, "red")
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


def _record(project, number, args, work, started, review, extras=None):
    from .local_reading import reading_from_run
    extras = extras or {}
    run = _selected(project, number)
    if run is None:
        raise ValueError("로컬 기준 판독이 없습니다.")
    direction = "horizontal" if args.mode == "horizontal_zh" else "vertical"
    raw = run["raw"]
    first = reading_from_run(raw, number, direction)
    merged = reading_from_run(raw, number, direction, text=project.text(number))
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
    usage = {"calls": 0, "prompt_tokens": 0, "output_tokens": 0, "thinking_tokens": 0}
    for item in reviews:
        # Only numeric public usage metadata; authentication never enters records.
        meta = item.get("metadata", {})
        tokens = meta.get("usage", {})
        usage["calls"] += 1
        for key, aliases in (("prompt_tokens", ("input_tokens", "prompt_token_count")),
                             ("output_tokens", ("output_tokens", "candidates_token_count")),
                             ("thinking_tokens", ("thoughts_token_count",))):
            value = next((tokens[name] for name in aliases if name in tokens), 0) if isinstance(tokens, dict) else 0
            if type(value) in (int, float):
                usage[key] += value
    merged_data = merged.model_dump()
    try:
        local_grades = _local_grades(project, number, run, project.text(number), witness=extras.get("witness"))
    except Exception:
        local_grades = []                                    # grades are display only; never lose the reading for them
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
                            **({"options": copy.deepcopy(extras["options"])} if extras.get("options") else {})}}


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
    from ab_ocr.ocr import render_page
    from . import cli
    # The preview must use the same pixels and frame as local recognition.
    if render:
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


def _settings(args, remaining):
    from .comparison_ai import AISettings
    from .providers import default_model
    return AISettings(args.reader, getattr(args, "model", None) or default_model(args.reader),
                      max_calls=max(1, remaining), max_output_tokens=4096,
                      max_total_output_tokens=max(1, remaining) * 4096)


def command(args, *, work=None, doc=None):
    from ab_ocr.ocr import render_cache
    from . import cli
    from .engine_pool import EnginePool
    # One page is rendered for every engine, the preview and the review crops;
    # engine workers stay loaded across pages.
    from ab_ocr.ocr import input_enhancement
    from ab_ocr.local_engines import easyocr_runtime, line_verification
    enhancement = getattr(args, "local_enhance", "none")
    enhancement = None if enhancement in (None, "none") else enhancement
    with render_cache(), input_enhancement(enhancement), line_verification(getattr(args, "local_line_verification", False)), \
            easyocr_runtime(getattr(args, "local_easyocr_threads", 2) or 2, getattr(args, "local_easyocr_batch", 1) or 1), \
            EnginePool(should_stop=cli.CANCEL.is_set, log=cli.log) as pool:
        return _command(args, work=work, doc=doc, pool=pool)


def _command(args, *, work=None, doc=None, pool=None):
    from . import cli
    from .automatic_ocr import run_automatic_page
    from .comparison import ComparisonError
    from .comparison_ai import AIReviewError, AIReviewCancelled, ReviewPolicy, prepare_plan, run_plan
    from .pdfio import open_pdf, parse_pages
    from .readers import environment_key, split_keys
    from ab_ocr.ocr import OCRError, OCRCancelled

    pdf_only = getattr(args, "pdf_only", False)
    bad = None if pdf_only else cli._check_document(args)
    if bad:
        cli.log(bad)
        return 2
    cap = getattr(args, "local_ai_max_calls", 20)
    if type(cap) is not int or not 1 <= cap <= 100:
        cli.log("의심 구간 API 호출 상한은 1~100이어야 합니다.")
        return 2
    command_cap = getattr(args, "max_calls", 0)
    cap = min(cap, command_cap) if command_cap else cap
    owned_doc = doc is None
    doc = doc or open_pdf(args.pdf)
    try:
        work = work or Work(args.pdf, getattr(args, "work", None))
        work.ensure_source(args.dpi)
        pages = parse_pages(getattr(args, "pages", None), len(doc))
        work.ensure_pages(pages, len(doc))
        cli.resolve_mode(args, doc, work)
        if getattr(args, "print_offset", None) is None:
            state = work.load_state()
            args.print_offset = state.get("print_offset")
            if args.print_offset is None:
                args.print_offset = cli.detect_print_offset(doc)
        source = _source(work, doc)
        project = _project(source, work)
        state = work.load_state()
        state["local_first_project"] = str(project.project_path)
        work.save_state(state)
        cli.log(f"▶ 로컬 우선 인식: {len(pages)}쪽 · 의심 구간만 API 검증(이번 실행 최대 {cap}회)")
        review_enabled = bool(getattr(args, "local_ai_review", True))
        keys = split_keys(args.reader, environment_key(args.reader)) if review_enabled else []
        key = keys[0] if keys else ""
        calls, failed, incomplete, completed = 0, [], [], []
        cli.LOCAL_API_CALLS = 0
        summaries = {}
        saver = _Checkpoint(project)
        def engine_options():
            """Per page: workers resident so far, and how many engines may run at once."""
            if pool is None:
                return {}
            from .automatic_ocr import memory_status
            from .engine_pool import engine_parallelism
            requested = getattr(args, "local_engine_parallel", 0) or 0
            resident = pool.resident()
            from .automatic_ocr import gpu_engines_enabled
            mode = getattr(args, "local_enhance", "none")
            exclude = tuple(getattr(args, "local_exclude_engines", None) or ())
            return {"recognizer": pool.recognize, "resident": resident, "gpu_engines": gpu_engines_enabled(),
                    **({"exclude": exclude} if exclude else {}),
                    **({"input_enhancement": mode} if mode not in (None, "none") else {}),
                    "parallel": lambda engines: engine_parallelism(engines, requested, resident=resident,
                                                                   memory=memory_status())}
        from .local_options import FastPlan, apply_votes, is_blank
        defaults = {"engine_plan": ("local_engine_plan", "all"), "vote": ("local_vote", False),
                    "columns": ("local_columns", True), "textlayer_witness": ("local_textlayer_witness", False),
                    "skip_blank": ("local_skip_blank", False), "enhance": ("local_enhance", "none"),
                    "ai_batch": ("local_ai_batch", 1), "line_verification": ("local_line_verification", False),
                    "exclude_engines": ("local_exclude_engines", []), "easyocr_threads": ("local_easyocr_threads", 2),
                    "easyocr_batch": ("local_easyocr_batch", 1)}
        options = {}
        for option, (name, default) in defaults.items():
            value = getattr(args, name, default)
            if value is not None and (type(value), value) != (type(default), default):
                options[option] = value
        if options:
            cli.log("  선택 옵션(측정 전 시험 기능): " + ", ".join(f"{k}={v}" for k, v in options.items()))
        fast = FastPlan() if options.get("engine_plan") == "fast" else None

        def witness_text(number):
            from .pdfio import text_layer
            try:
                with cli.DOC_LOCK:
                    return text_layer(doc, number).plain()
            except Exception:
                return None

        for index, number in enumerate(pages, 1):
            if cli.CANCEL.is_set():
                return 130
            started = time.perf_counter()
            extras = {"columns": bool(getattr(args, "local_columns", True)), "options": options or None,
                      "witness": witness_text(number) if options.get("textlayer_witness") else None}
            old = work.load_record(number)
            if _protected_record(work, number, old) and not getattr(args, "force", False):
                cli.log(f"  {number}쪽: 기존 판독·사람 교정 보존(다시 인식하려면 강제 재판독)")
                cli.page_event(number, "record")
                cli.progress("read", index, len(pages))
                completed.append(number)
                continue
            cli.page_event(number, "now")
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
                        cli.progress("read", index, len(pages))
                        continue

                def read(only=None):
                    return run_automatic_page(project, number, profile=args.local_profile,
                        direction="horizontal" if args.mode == "horizontal_zh" else "vertical", dpi=args.dpi,
                        engine_root=getattr(args, "local_engine_root", None), force=getattr(args, "force", False),
                        should_stop=cli.CANCEL.is_set, on_result=saver.maybe, **engine_options(),
                        **({"only": only} if only else {}))
                only = fast.engines() if fast is not None else None
                outcome = read(only)
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
                from .comparison import _run_profile
                selected = _selected(project, number)
                successful = set(outcome.get("completed", [])) | set(outcome.get("skipped", []))
                expected = (args.local_profile, "horizontal" if args.mode == "horizontal_zh" else "vertical")
                if (selected is None or selected["engine"] not in successful
                        or _run_profile(selected) != expected or selected.get("request", {}).get("dpi") != args.dpi):
                    raise ComparisonError("이번 조건에서 성공한 로컬 기준 판독이 없습니다. 이전 판독은 보존했습니다.")
                # Checkpoint local output BEFORE preparing or sending any API call.
                _save_record(project, number, args, work, started, review, extras=extras)
                settings = _settings(args, cap)
                try:
                    plan = prepare_plan(project, [number], ReviewPolicy(selective=True, batch_size=getattr(args, "local_ai_batch", 1) or 1),
                                        settings=settings,
                                        should_stop=cli.CANCEL.is_set)
                except AIReviewError as exc:
                    review.update(status="pending", error=str(exc))
                    if review_enabled:
                        incomplete.append(number)
                else:
                    review.update(copy.deepcopy(plan.summary))
                    plan_path = work.dir / "local-first-review" / f"p{number:03d}-{plan.fingerprint[:24]}.json"
                    plan_path.parent.mkdir(exist_ok=True)
                    write_atomic(plan_path, json.dumps(plan.to_dict(), ensure_ascii=False, indent=2))
                    review["plan_path"] = str(plan_path)
                    unresolved = bool(plan.summary.get("unlocalized_targets"))
                    if plan.summary["calls"] == 0:
                        review["status"] = "needs-local-review" if unresolved else "no-api-needed"
                        if unresolved and review_enabled:
                            incomplete.append(number)
                    elif not review_enabled or not key or plan.summary["calls"] > cap - calls:
                        review["status"] = "pending"
                        review["reason"] = ("AI 검증 꺼짐" if not review_enabled else "API 키 없음" if not key else "이번 실행 호출 상한 도달")
                        if review_enabled:
                            incomplete.append(number)
                    else:
                        cli.log(f"  {number}쪽: 의심 구간 {plan.summary['targets']}개 · API 최대 {plan.summary['calls']}회")
                        review["status"] = "reviewing"
                        _save_record(project, number, args, work, started, review, render=False, extras=extras)
                        checkpoints = [0]
                        def checkpoint(_value):
                            project.save()
                            checkpoints[0] += 1
                            cli.LOCAL_API_CALLS = calls + checkpoints[0]
                        result = run_plan(project, plan, api_key=key, should_stop=cli.CANCEL.is_set,
                                          on_result=checkpoint)
                        calls += result["calls"]
                        cli.LOCAL_API_CALLS = calls
                        review.update(status="failed" if result["failed"] else "reviewed", outcome=result)
                        if result["failed"] or unresolved:
                            incomplete.append(number)
                        project.save()
                        if result["cancelled"]:
                            review["status"] = "cancelled"
                            _save_record(project, number, args, work, started, review, render=False, extras=extras)
                            return 130
                if not work.source_matches(args.dpi):
                    raise ComparisonError("처리 중 원본이 변경되어 출력 기록을 갱신하지 않았습니다.")
                _save_record(project, number, args, work, started, review, render=False, extras=extras)
                summaries[str(number)] = review
                completed.append(number)
                cli.page_event(number, "done")
                cli.log(f"  {number}쪽: {review['status']} · 로컬 {(_selected(project, number) or {}).get('engine', '')}")
            except (OCRCancelled, AIReviewCancelled, KeyboardInterrupt):
                return 130
            except (OCRError, ComparisonError, AIReviewError, OSError, ValueError) as exc:
                failed.append(number)
                cli.page_event(number, "fail")
                cli.log(f"  {number}쪽 오류: {exc}")
            cli.progress("read", index, len(pages))
        state = work.load_state()
        state["local_first_last_run"] = {"calls": calls, "cap": cap, "failed_pages": failed,
                                         "incomplete_review_pages": incomplete, "pages": summaries}
        work.save_state(state)
        completed = [n for n in pages if work.load_record(n)]
        if not work.source_matches(args.dpi):
            cli.log("원본이 변경되어 문서 출력을 중단했습니다. 완료 기록은 보존했습니다.")
            return 2
        if not completed:
            return 3 if failed else 2
        # Local review is the only model path; document assembly must stay offline.
        build_args = copy.copy(args)
        build_args.no_context_model = True
        if pdf_only:
            rc = cli.make_textlayer(build_args, work, completed)
        else:
            rc = cli.cmd_build(build_args, work, doc)
        cli.cmd_review(build_args, work, doc)
        cli.cmd_report(build_args, work, doc)
        cli.log(f"끝: 로컬 우선 인식 · 이번 실행 API {calls}/{cap}회 · 비교 프로젝트 {project.project_path}")
        cli.log("AI 수정 제안은 비교·교정 창에서 채택·기각합니다. 원시 판독과 사람 교정은 보존됩니다.")
        return rc or (3 if failed else 4 if incomplete else 0)
    finally:
        if owned_doc:
            doc.close()
