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
    for start, end, kind in marks:                 # AI page reading: kept differences (blue / red)
        paint(start, end, kind)
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


# Review states a page can be left in when nothing more is to be done for it under the same conditions.
RESUME_DONE = {"no-api-needed", "reviewed", "needs-local-review", "blank-page"}


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
    if first.get("resume_key") != key:
        return False
    if status not in RESUME_DONE and not (status == "local-complete" and not review_enabled):
        return False                                    # pending, failed, cancelled, or stopped before the review
    mode = getattr(args, "local_ai_page", "off") or "off"
    return mode == "off" or "ai_page" in first          # the AI page reading finished too


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
        local_grades = _local_grades(project, number, run, current, witness=extras.get("witness"), marks=marks)
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
    """(Trial, --local-ai-accept) apply every applicable, certain AI proposal to the page text.
    Recorded like a manual acceptance (reversible); for measuring what the review changes.
    Proposals the safety checks hold (no engine or image support, uncertain) are counted, not applied."""
    from .comparison import ComparisonError
    accepted, held = 0, 0
    for proposal in project.ai_proposals(number):
        if not proposal.get("applicable") or proposal.get("uncertain"):
            held += 1
            continue
        try:
            project.accept_ai_proposal(number, proposal["review_id"], proposal["id"])
        except ComparisonError:
            held += 1
            continue
        accepted += 1
    if accepted:
        project.save()
    return {"accepted": accepted, "held": held}


# How a page's review status reads in the log; the record keeps the status values themselves.
REVIEW_STATUS_LABELS = {"local-complete": "로컬 인식만(AI 검증 안 함)", "blank-page": "빈 쪽",
                        "no-api-needed": "AI 검증 필요 없음", "needs-local-review": "사람 확인 필요(위치를 못 찾은 의심 구간)",
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
    if getattr(args, "local_ai_page", "auto") in (None, "auto") or any(
            getattr(args, name, None) is None for name in SUBSCRIPTION_DEFAULTS):
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
    """comparison_ai.ApiTransport for the API key of the chosen provider, or None (no key, mock,
    --no-local-ai-api-fallback, --local-ai-auth api handles the key itself)."""
    if not getattr(args, "local_ai_api_fallback", True) or args.reader not in ("gemini", "openai", "anthropic"):
        return None
    keys = split_keys(args.reader, environment_key(args.reader))
    if not keys:
        return None
    from .comparison_ai import AIReviewError, ApiTransport
    from .providers import default_model
    try:
        return ApiTransport(args.reader, getattr(args, "model", None) or default_model(args.reader), keys[0], calls)
    except AIReviewError:
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
        session = subscriptions.Session(plan, getattr(args, "local_ai_plan", None), api=api, log=cli.log)
        subscriptions.activate(session)
    plan = session.plan
    client = session                     # moves to the next plan, then the API key, when a plan runs out
    model = subscriptions.model(plan, None)
    direction = "horizontal" if args.mode == "horizontal_zh" else "vertical"
    import os
    # Readings are keyed by page image, model and prompt; a shared folder (measurement sets) reuses them.
    cache = Path(os.environ.get("YEONE_OCR_AI_PAGE_CACHE") or work.dir / "ai-page")
    cli.log(f"  AI 쪽 판독({mode}): {subscriptions.NAMES[plan]} 구독 · 모델 {model} · 쪽을 원래 해상도 띠로 잘라 처음부터 읽고 엔진 판독과 맞춰 봄")

    def read(number):
        from ab_ocr.ocr import render_page
        from .ai_page import read_page
        if not session.available:
            return None
        try:
            with render_page(project.source_path, number, args.dpi, should_stop=cli.CANCEL.is_set) as image:
                result = read_page(image, direction=direction, profile=args.local_profile, client=client,
                                   model=model, cache_dir=cache)
        except (SubscriptionError, TimeoutError, ValueError, OSError) as exc:
            cli.log(f"  {number}쪽: AI 쪽 판독 실패 — {exc} (엔진 판독만 씀)")
            return None
        cli.log(f"  {number}쪽: AI 쪽 판독 {len(result['text'])}자" + (" (저장된 판독)" if result.get("cached") else ""))
        return {**result, "mode": mode}
    return read


def _settings(args, remaining):
    from .comparison_ai import AISettings
    from .providers import default_model
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
    resolved = _resolve_ai_defaults(args, cli)
    cap = getattr(args, "local_ai_max_calls", 20)
    if type(cap) is not int or not 1 <= cap <= 100:
        cli.log("의심 구간 API 호출 상한은 1~100이어야 합니다.")
        return 2
    command_cap = getattr(args, "max_calls", 0)
    cap = min(cap, command_cap) if command_cap else cap
    api_cap = cap                       # paid calls: also the budget when a run falls back from the plans to the key
    if getattr(args, "local_ai_auth", None) == "subscription":
        cap = max(1, int(getattr(args, "local_ai_subscription_calls", 1000) or 1000))   # no per-call charge
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
                key = subscriptions.KEYS[plan]
                session = subscriptions.Session(plan, getattr(args, "local_ai_plan", None), api=api, log=cli.log)
                subscriptions.activate(session)     # the window's [다음 구독으로 넘기기] switches this connection
            elif auth_mode(requested) == "subscription":
                if api is not None:         # no usable plan: straight to the API key, within the paid cap
                    cap = api_cap
                    note = (note + " → " if note else "") + f"API 키({api.provider} · 모델 {api.model})로 검증합니다."
                else:
                    key = ""
            if note:
                cli.log("  " + note + (f" · 모델 {args.model}" if plan else ""))
        calls, failed, incomplete, completed = 0, [], [], []
        cli.LOCAL_API_CALLS = 0
        summaries = {}
        saver = _Checkpoint(project)
        def easyocr_wanted():
            """EasyOCR: off on horizontal pages by default (2026-11 measurements: Han characters more
            accurate without it on 4 documents, 27-35% faster), kept on vertical pages (not measured)."""
            choice = getattr(args, "local_easyocr", None)
            return (args.mode != "horizontal_zh") if choice is None else bool(choice)

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
            exclude = set(getattr(args, "local_exclude_engines", None) or ())
            if not easyocr_wanted():
                exclude.add("easyocr")
            exclude = tuple(sorted(exclude))
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
        fast = FastPlan() if options.get("engine_plan") == "fast" else None

        def witness_text(number):
            from .pdfio import text_layer
            try:
                with cli.DOC_LOCK:
                    return text_layer(doc, number).plain()
            except Exception:
                return None

        page_reader = _ai_page_reader(args, project, work, cli, session, api)
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
                # A page finished under the same conditions (no engine ran again, same key, review done):
                # nothing to crop, plan, read or write again.
                if (old and not getattr(args, "force", False) and not outcome.get("completed")
                        and _resume_done(old, args, _resume_key(args, selected["id"], project.text(number), options),
                                         review_enabled)):
                    done = old["local_first"]["review"]
                    summaries[str(number)] = done
                    if review_enabled and (done.get("status") == "needs-local-review" or done.get("unlocalized_targets")):
                        incomplete.append(number)
                    completed.append(number)
                    cli.page_event(number, "record")
                    cli.log(f"  {number}쪽: 끝난 쪽 — 같은 조건이라 건너뜀 · {REVIEW_STATUS_LABELS.get(done.get('status'), done.get('status'))}")
                    cli.progress("read", index, len(pages))
                    continue
                # Checkpoint local output BEFORE preparing or sending any API call.
                _save_record(project, number, args, work, started, review, extras=extras)
                if page_reader is not None:
                    extras["ai_page"] = page_reader(number)
                settings = _settings(args, cap)
                try:
                    plan = prepare_plan(project, [number], ReviewPolicy(selective=True, batch_size=getattr(args, "local_ai_batch", 1) or 1,
                                                                        detail_tile=getattr(args, "local_ai_detail_tile", 0) or 0),
                                        settings=settings,
                                        should_stop=cli.CANCEL.is_set)
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
                    if plan.summary["calls"] == 0:
                        review["status"] = "needs-local-review" if unresolved else "no-api-needed"
                        if unresolved and review_enabled:
                            incomplete.append(number)
                    elif not review_enabled or not key or plan.summary["calls"] > cap - calls or (
                            session is not None and not session.available):
                        review["status"] = "pending"
                        review["reason"] = ("AI 검증 꺼짐" if not review_enabled else "API 키 없음" if not key else
                                            session.stop_reason() if session is not None and not session.available
                                            else "이번 실행 호출 상한 도달")
                        if review_enabled:
                            cli.log(f"  {number}쪽: AI 검증 보류 — {review['reason']} (이 쪽 {plan.summary['calls']}회 필요)")
                        if review_enabled:
                            incomplete.append(number)
                    else:
                        cli.log(f"  {number}쪽: 의심 구간 {plan.summary['targets']}개 · "
                                + (f"AI 호출 최대 {plan.summary['calls']}회({session.short()})" if session is not None
                                   else f"API 최대 {plan.summary['calls']}회"))
                        review["status"] = "reviewing"
                        _save_record(project, number, args, work, started, review, render=False, extras=extras)
                        checkpoints = [0]
                        def checkpoint(_value):
                            project.save()
                            checkpoints[0] += 1
                            cli.LOCAL_API_CALLS = calls + checkpoints[0]
                        result = run_plan(project, plan, api_key=key, should_stop=cli.CANCEL.is_set, on_result=checkpoint,
                                          client_factory=(lambda _settings, _key: session) if session is not None else None)
                        calls += result["calls"]
                        if getattr(args, "local_ai_accept", False):
                            review["ai_accept"] = _accept_ai(project, number)
                            cli.log(f"  {number}쪽: AI 제안 반영 {review['ai_accept']['accepted']}개 · 보류 {review['ai_accept']['held']}개")
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
                cli.log(f"  {number}쪽: {REVIEW_STATUS_LABELS.get(review['status'], review['status'])} · "
                        f"로컬 엔진 {(_selected(project, number) or {}).get('engine', '')}")
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
        from . import subscriptions
        subscriptions.deactivate()
        if owned_doc:
            doc.close()
