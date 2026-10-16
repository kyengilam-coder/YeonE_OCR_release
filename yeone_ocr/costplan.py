"""실행 전 비용 한 창: 판독(1차·2차)·3차 판별·합의 의심·문맥 교정의 호출·토큰·금액 어림. count_plan 은 무겁고(작업 스레드), price_plan 은 가볍다(단가를 고칠 때마다).
count_plan 은 읽기만 한다 — 결과 폴더를 만들지 않고(Work initialize=False) 기록을 고치지 않으며(repair=False) 글자층·재사용 기록·문맥 추정 파일을 쓰지 않는다. 연 PDF 는 늘 닫는다.
기록이 있는 쪽은 물을 자리·후보 수가 정확하고(다른 모델이 물은 것 건너뛰기·짜임·짜임 규칙·쪽 잇기를 실행과 같게), 새로 읽을 쪽(다시 판독 포함)은
이 책 기록의 쪽당 평균(없으면 아래 기본값)으로 어림한다. 토큰은 이 책에서 같은 모델이 쓴 호출당 실측 평균이 있으면 그것, 없으면 아래 기본값.
단계마다 따로 어림하고, 한 단계가 실패하면 그 까닭을 errors 에 남긴다 — 창은 그때 [아니요 · 모델 단계 없이](판독 실패면 [취소])를 기본으로 한다(닫힌 쪽으로)."""
from __future__ import annotations
import json, math
from dataclasses import dataclass

from .estimate import CHARS_PER_TOKEN, DEFAULT_FX_KRW, PRICES_DATE, _cost, fmt_krw, fmt_tokens, resolve_price
from .presets import DEFAULT_PRESET

SITES_PER_PAGE = 20          # 실측 없을 때: 새로 읽는 쪽 하나에서 생기는 〔a/b〕 자리
SUSPECTS_PER_PAGE = 3        # 합의 의심(쪽당 상한 8)
CANDIDATES_PER_PAGE = 25     # 문맥 교정 후보(간체 책 기준)
CHOICE_CHARS_PER_ITEM = 260  # 실측 없을 때: 모델에 보내는 문맥 후보 하나(앞뒤 60자·원문 30자·후보)의 JSON 글자 수
GLYPH_IN_PER_SITE = (500, 1300)    # 글자 조각 그림 하나의 입력 토큰(제공사·해상도에 따라)
GLYPH_OUT_PER_SITE = (40, 160)     # 답(w·e·c)과 줄인 생각
CHOICE_OUT_PER_ITEM = (12, 40)
GLYPH_BATCH = 8              # cli --batch-sites 기본(창은 넘기지 않음)
CHOICE_BATCH = 80            # cli.CHOICE_BATCH 와 같게(새로 읽을 쪽의 문맥 교정 호출 어림 — cli 를 늦게 부르려고 따로 둔다)
LABELS = {"read1": "1차 · 쪽 전체 판독", "read2": "2차 · 확대 조각 확인", "glyph": "3차 · 글자 판별", "suspect": "합의 판독 의심", "choice": "문맥 교정(번체)"}


@dataclass
class PlanInput:
    kind: str                                   # run | read | build | adjudicate
    pdf: str
    work_dir: str
    pages: str = ""
    dpi: int = 300
    reader: str = "gemini"
    model: str = ""
    second_model: str = "auto"
    second_pass: bool = True
    upscale: float = 2.0
    use_cache: bool = True
    force: bool = False
    suspects: bool = True
    blind: bool = False
    context_model: bool = True
    normalize: bool = True
    normalize_traditional: bool = True
    normalize_yun: bool = True
    normalize_punctuation: bool = True
    punctuation_rules: str = ""
    layout: str = "page"
    preset: str = DEFAULT_PRESET
    join_pages: bool = True
    skip_any_model: bool = True


class PlanCancelled(Exception):
    """어림 중 [취소] — 단계 사이·기록 사이에서 멈춘다."""


def _why(e: Exception, n: int = 120) -> str:
    return f"{type(e).__name__}: {str(e)[:n]}"


def _batches(n: float) -> int:
    return math.ceil(n / GLYPH_BATCH) if n > 0 else 0


def _row(step: str, scope: str, n_pages: int, model: str, basis: str, calls, tin, cached, tout, detail: str = "", items: int | None = None) -> dict:
    """계획의 한 줄. items 는 물을 자리·후보 수(판독 줄은 None)."""
    label = LABELS[step] + (f" — 기록 있는 {n_pages:,}쪽" if scope == "have" else f" — 새로 읽을 {n_pages:,}쪽(어림)")
    r2 = lambda v: [max(0, round(v[0])), max(0, round(v[1]))]
    return {"step": step, "label": label, "scope": scope, "model": model, "basis": basis, "calls": r2(calls), "in": r2(tin), "cached": r2(cached),
            "out": r2(tout), "detail": detail, "items": items, "model_step": step not in ("read1", "read2")}


def _avg_usage(usages) -> tuple[int, float, float, float] | None:
    """[(calls, prompt, cached, output+thinking), …] 합 → (호출 수, 호출당 입력·캐시 입력·출력). 호출이 없으면 None."""
    c = p = k = o = 0.0
    for uc, up, uk, uo in usages:
        c += uc; p += up; k += uk; o += uo
    return (int(c), p / c, k / c, o / c) if c > 0 else None


def _num(u: dict, *keys) -> float:
    total = 0.0
    for key in keys:
        v = u.get(key) or 0
        total += float(v) if isinstance(v, (int, float)) and math.isfinite(v) and v > 0 else 0.0
    return total


def _glyph_avg(recs: list[dict], model: str):
    """이 책 기록의 3차 사용량(usage.by_model 의 '<모델> (3차…)' — 합의 의심도 같은 판독기라 함께)에서 호출당 평균."""
    out = []
    for r in recs:
        for name, u in ((r.get("usage") or {}).get("by_model") or {}).items():
            if isinstance(u, dict) and str(name).startswith(model + " (3차"):
                out.append((_num(u, "calls"), _num(u, "prompt_tokens"), _num(u, "cached_tokens"), _num(u, "output_tokens", "thinking_tokens")))
    return _avg_usage(out)


def _choice_avg(work, model: str):
    """out/*_normalization.json 의 문맥 교정 판독기 사용량(같은 모델·호출이 있는 보고만)에서 호출당 평균."""
    out = []
    for f in sorted(work.out_dir.glob("*_normalization.json")):
        try:
            r = json.loads(f.read_text(encoding="utf-8")).get("reader") or {}
            u = r.get("usage") or {}
            if r.get("model") == model and _num(u, "calls") > 0:
                out.append((_num(u, "calls"), _num(u, "prompt_tokens"), _num(u, "cached_tokens"), _num(u, "output_tokens", "thinking_tokens")))
        except (OSError, ValueError, AttributeError, TypeError):
            continue
    return _avg_usage(out)


def _glyph_tokens(n: float, calls, blind: bool, sys_tok: float, avg):
    """3차·합의 의심 자리 n 곳(호출 calls=[lo, hi])의 (입력, 캐시 입력, 출력) 토큰 [lo, hi]. 실측 평균이 있으면 호출 수 × 호출당 평균.
    기본값: 낮은 쪽은 자리마다 조각 한 번, 높은 쪽은 묶음 + 낱개 재질문으로 두 번(눈가림 재확인을 켜면 한 번 더)."""
    lo, hi = calls
    if avg:
        _, a_in, a_k, a_out = avg
        return [a_in * lo, a_in * hi], [a_k * lo, a_k * hi], [a_out * lo, a_out * hi]
    k = 1 if blind else 0
    return ([n * (1 + k) * GLYPH_IN_PER_SITE[0] + lo * sys_tok, n * (2 + k) * GLYPH_IN_PER_SITE[1] + hi * sys_tok], [0, 0],
            [n * (1 + k) * GLYPH_OUT_PER_SITE[0], n * (2 + k) * GLYPH_OUT_PER_SITE[1]])


def _layer_plain(work, doc, n: int) -> str:
    """합의 의심에 쓰는 쪽 글자층 평문 — 읽기만: textlayer/pNNN.json 이 읽히면 그 'plain', 아니면 PDF 에서(파일은 쓰지 않음 — cli._load_layer 는 쓴다)."""
    try:
        d = json.loads(work.textlayer_path(n).read_text(encoding="utf-8"))
        if isinstance(d, dict) and isinstance(d.get("plain"), str):
            return d["plain"]
    except (OSError, ValueError):
        pass
    from .pdfio import text_layer
    return text_layer(doc, n).plain()


def _layer_read(work, doc, n: int) -> dict:
    """쪽 글자층 사전 — 읽기만: textlayer/pNNN.json 이 읽히면 그것, 아니면 PDF 에서(cli._load_layer 는 없으면 쓴다)."""
    try:
        d = json.loads(work.textlayer_path(n).read_text(encoding="utf-8"))
        if isinstance(d, dict):
            return d
    except (OSError, ValueError):
        pass
    from .cli import layer_dict
    return layer_dict(doc, n)


def _stale_moves(w, doc, recs: list[dict], reader: str, stop, tell, notes: list) -> set[int]:
    """run 이 판독 전에 하는 refresh_stale_records 를 읽기만으로: 다른 판의 기록 가운데 영역 좌표가 크게 밀려 readings/stale/ 로 옮겨 다시 판독할 쪽.
    다른 제공사로 이어 읽는 쪽은 cmd_run 처럼 보지 않는다. 나머지 이전 판 기록은 실행 때 다시 비교되어 '정확' 수가 조금 달라질 수 있음을 notes 에."""
    from .cli import _record_provider, is_stale_version, stale_check
    old = [r for r in recs if is_stale_version(r) and not (_record_provider(r) and _record_provider(r) != reader)]
    if not old:
        return set()
    state_mode = w.load_state().get("mode")
    moved = set()
    for i, r in enumerate(old):
        stop(); tell(i, len(old), f"이전 판 기록 다시 비교해 보는 중 {i + 1:,}/{len(old):,}쪽")
        if stale_check(r, _layer_read(w, doc, r["pdf_page"]), (r.get("mode") or state_mode) == "horizontal_zh")[2]:
            moved.add(r["pdf_page"])
    if moved:
        notes.append(f"이전 판 기록 {len(moved):,}쪽({', '.join(map(str, sorted(moved)[:8]))}{' …' if len(moved) > 8 else ''})은 영역 좌표가 밀려 있어 실행할 때 "
                     "readings/stale/ 로 옮기고 다시 판독합니다 — 판독(1차·2차)과 새로 읽을 쪽 어림에 넣었습니다.")
    if len(old) > len(moved):
        notes.append(f"이전 판 기록 {len(old) - len(moved):,}쪽은 실행할 때 지금 규칙으로 다시 비교합니다(모델 호출 없음) — 그 쪽의 '정확' 자리·후보 수는 다시 비교한 뒤 조금 달라질 수 있습니다.")
    return moved


def _mean(vals: list[float]) -> float | None:
    return sum(vals) / len(vals) if vals else None


def _pp(x: float) -> str:
    """쪽당 평균 — 10 아래는 소수 한 자리(0.3건이 '0건' 으로 보이지 않게)."""
    return f"{x:.1f}" if x < 10 else f"{x:,.0f}"


def count_plan(inp: PlanInput, progress=None, should_stop=None) -> dict:
    """호출·토큰 어림(단가 없음 — price_plan 이 붙인다). progress(한 것, 전체, 단계 글), should_stop() 이 참이면 PlanCancelled."""
    from .pdfio import open_pdf, parse_pages
    from .providers import default_model
    from .state import Work
    kind = inp.kind
    model = (inp.model or "").strip() or default_model(inp.reader)
    force = bool(inp.force) and kind in ("run", "read")                   # 다시 판독은 판독하는 명령에만(문서 만들기·3차 판별은 기록을 그대로 씀)
    plan = {"kind": kind, "model": model, "context_model": bool(inp.context_model), "skip_any_model": bool(inp.skip_any_model), "force": force,
            "pages": 0, "records": 0, "to_read": 0, "new_pages": 0, "same_source": False, "rows": [], "errors": [], "notes": [],
            "skipped": dict.fromkeys(("glyph", "glyph_other", "choice_reused", "choice_reused_other", "choice_remembered", "choice_remembered_other"), 0)}
    rows, errors, notes, skipped = plan["rows"], plan["errors"], plan["notes"], plan["skipped"]

    def stop():
        if should_stop is not None and should_stop():
            raise PlanCancelled()

    def tell(done, total, stage):
        if progress is not None:
            progress(done, total, stage)

    def stage(step, fn):
        stop()
        try:
            fn()
        except PlanCancelled:
            raise
        except Exception as e:
            errors.append({"step": step, "reason": _why(e)})

    try:
        with open_pdf(inp.pdf) as doc:
            tell(0, 0, "판독 기록 읽는 중")
            pages = parse_pages(inp.pages.strip() or None, len(doc))
            w = Work(inp.pdf, inp.work_dir, initialize=False)
            same = w.source_matches(inp.dpi if kind in ("run", "read") else None)    # 문서 만들기·3차 판별은 DPI 를 보지 않고 원본만 견준다(ensure_source() 와 같게)
            recs = w.records(pages, repair=False) if same else []
            stop()
            moved: set[int] = set()                                      # run 이 판독 전에 readings/stale/ 로 옮겨 다시 판독할 이전 판 기록(refresh_stale_records)
            if kind == "run" and not force and recs:
                stage("read", lambda: moved.update(_stale_moves(w, doc, recs, inp.reader, stop, tell, notes)))
            have = [] if force or not same else [r for r in recs if r.get("pdf_page") not in moved]
            have_pages = {r.get("pdf_page") for r in have}
            done = set() if force else {r["pdf_page"] for r in recs if "pass1" in r and not r.get("pass2_failed") and r["pdf_page"] not in moved}
            plan.update(pages=len(pages), records=len(recs), same_source=same,
                        to_read=sum(1 for p in pages if p not in done) if kind in ("run", "read") else 0,
                        new_pages=len(pages) - len(have) if kind == "run" else 0)
            new_n = plan["new_pages"]
            if not same and kind in ("run", "read") and w.readings_dir.is_dir() and any(w.readings_dir.glob("p*.json")):
                notes.append("원본·영상 설정이 바뀌었거나 기존 기록의 원본을 확인할 수 없어 모두 새로 판독합니다 — 이전 결과는 실행할 때 보관됩니다.")
            if force:
                notes.append(f"다시 판독: 고른 {len(pages):,}쪽을 모두 새로 읽는 셈입니다. 3차 판별·합의 의심·문맥 교정도 새로 읽을 쪽의 어림(이 책 기록의 쪽당 평균)으로 셉니다.")

            def reading():                                               # (2) 1차·2차 판독
                from .estimate import estimate
                if not plan["to_read"]:
                    return
                def est(rs):
                    return estimate(inp.pdf, pages, upscale=inp.upscale, second_pass=inp.second_pass, model=model, second_model=inp.second_model,
                                    use_cache=inp.use_cache, records=rs, reader=inp.reader, force=force)
                rs = [{k: v for k, v in r.items() if k != "pass1"} if r.get("pdf_page") in moved else r for r in recs]   # 옮길 쪽은 읽을 쪽으로(사용량 실측은 평균에 그대로)
                e = est(rs)
                if e["rows"] and all("(3차" in r["model"] for r in e["rows"]):    # 실측이 3차뿐(판독 사용량이 없는 옛 기록) — 판독은 기본값으로
                    e = est([{k: v for k, v in r.items() if k != "usage"} for r in rs])
                basis = "토큰 기본값(실측 없음)" if e["basis"] == "기본값" else "토큰 이 책 " + e["basis"]
                for r in e["rows"]:
                    if "(3차" in r["model"]:                              # 앞 실행의 3차 실측은 아래 3차 줄이 따로 센다
                        continue
                    rows.append(_row("read1" if r["label"].startswith("1차") else "read2", "new", plan["to_read"], r["model"], basis,
                                     (r["calls"], r["calls"]), (r["in_low"], r["in_high"]), (r.get("cached_low", 0), r.get("cached_high", 0)),
                                     (r["out_low"], r["out_high"])))
                notes.extend(n for n in e["notes"] if not n.startswith(("단가를 모르는", "추정치입니다")) and "어림에 포함되지 않습니다" not in n)
            if kind in ("run", "read"):
                stage("read", reading)

            from .prompts import glyph_system_prompt
            sys_glyph = len(glyph_system_prompt()) / CHARS_PER_TOKEN
            g_avg = None

            def g_basis(prefix):
                return prefix + (f" · 토큰 이 책 {g_avg[0]:,}회 호출 평균" if g_avg else " · 토큰 기본값")

            def glyph():                                                 # (4) 3차 글자 판별
                nonlocal g_avg
                from .adjudicate import MIN_GLYPH_PX, find_sites, pending_sites
                from .schema import PageReading
                g_avg = _glyph_avg(recs, model)
                models = {model}; per = []; per_page = []
                for i, r in enumerate(recs):
                    stop(); tell(i, len(recs), f"3차 자리 세는 중 {i + 1:,}/{len(recs):,}쪽")
                    decided = sum(1 for e in r.get("adjudications") or [] if isinstance(e, dict) and e.get("choice"))
                    if r.get("pdf_page") in have_pages:
                        sites, k = pending_sites(r, models, MIN_GLYPH_PX, inp.skip_any_model)
                        per.append(len(sites)); skipped["glyph"] += k
                        if inp.skip_any_model and k:
                            skipped["glyph_other"] += k - pending_sites(r, models, MIN_GLYPH_PX, False)[1]
                        per_page.append(len(sites) + k + decided)
                    else:
                        per_page.append(len(find_sites(PageReading.model_validate(r["merged"]))) + decided)
                if have:
                    n = sum(per); lo = sum(_batches(x) for x in per)
                    calls = [lo * (2 if inp.blind else 1), lo * (2 if inp.blind else 1) + n]
                    tin, tk, tout = _glyph_tokens(n, calls, inp.blind, sys_glyph, g_avg)
                    rows.append(_row("glyph", "have", len(have), model, g_basis("자리 정확"), calls, tin, tk, tout,
                                     f"〔a/b〕 {n:,}곳 — {GLYPH_BATCH}자리씩 묶어 {lo:,}회, 묶음 답이 조각과 맞지 않으면 낱개로 최대 {n:,}회 더"
                                     + (f", 눈가림 재확인 최대 {lo:,}회 더" if inp.blind else ""), n))
                if new_n:
                    x = _mean(per_page)
                    basis = f"어림: 쪽당 {_pp(x)}곳(이 책 {len(per_page):,}쪽 평균)" if x is not None else f"어림: 쪽당 {SITES_PER_PAGE}곳(기본값)"
                    x = SITES_PER_PAGE if x is None else x
                    n = round(x * new_n); lo = new_n * _batches(x)
                    calls = [lo * (2 if inp.blind else 1), lo * (2 if inp.blind else 1) + n]
                    tin, tk, tout = _glyph_tokens(n, calls, inp.blind, sys_glyph, g_avg)
                    rows.append(_row("glyph", "new", new_n, model, g_basis(basis), calls, tin, tk, tout, f"〔a/b〕 약 {n:,}곳", n))
            if kind in ("run", "adjudicate"):
                stage("glyph", glyph)

            def suspects():                                              # (5) 합의 판독 의심
                from .adjudicate import SUSPECT_MAX, suspect_freq, suspects_pending
                freq = suspect_freq(w.dir); ks = []
                for i, r in enumerate(have):
                    stop(); tell(i, len(have), f"합의 의심 세는 중 {i + 1:,}/{len(have):,}쪽")
                    ks.append(suspects_pending(r, _layer_plain(w, doc, r["pdf_page"]), SUSPECT_MAX, False, freq))
                if have:
                    k = sum(ks); lo = sum(_batches(x) for x in ks)
                    calls = [lo * (2 if inp.blind else 1), lo * (2 if inp.blind else 1) + k]
                    tin, tk, tout = _glyph_tokens(k, calls, inp.blind, sys_glyph, g_avg)
                    rows.append(_row("suspect", "have", len(have), model, g_basis("자리 정확"), calls, tin, tk, tout,
                                     f"1·2차가 같게 읽었지만 글자층과 자형이 헷갈리는 자리 {k:,}곳(쪽당 최대 {SUSPECT_MAX}곳) — 본문은 그대로, 확인 항목만", k))
                if new_n:
                    x = _mean([len(r.get("suspects") or []) for r in recs if "suspect_stats" in r])
                    basis = f"어림: 쪽당 {_pp(x)}곳(이 책 {sum(1 for r in recs if 'suspect_stats' in r):,}쪽 평균)" if x is not None else f"어림: 쪽당 {SUSPECTS_PER_PAGE}곳(기본값)"
                    x = SUSPECTS_PER_PAGE if x is None else x
                    k = round(x * new_n); lo = new_n * _batches(x)
                    calls = [lo * (2 if inp.blind else 1), lo * (2 if inp.blind else 1) + k]
                    tin, tk, tout = _glyph_tokens(k, calls, inp.blind, sys_glyph, g_avg)
                    rows.append(_row("suspect", "new", new_n, model, g_basis(basis), calls, tin, tk, tout, f"합의 의심 약 {k:,}곳", k))
            if kind in ("run", "adjudicate") and inp.suspects:
                stage("suspect", suspects)

            def choice():                                                # (6) 문맥 교정(번체) — 문서 조립·규칙 변환까지 해 보므로 수백 쪽이면 몇 초
                from . import cli
                from .normalization import NormalizationOptions, load_punctuation_rules
                from .readers import TEXT_CHOICE_SYSTEM
                options = NormalizationOptions(traditional=inp.normalize_traditional, yun=inp.normalize_yun,
                    punctuation=inp.normalize_punctuation,
                    punctuation_rules=load_punctuation_rules(inp.punctuation_rules) if inp.normalize_punctuation and inp.punctuation_rules else None)
                base = have or recs
                np_ = None
                if base:
                    tell(0, 0, "문맥 교정 후보 세는 중(수백 쪽이면 몇 초)")
                    np_ = cli.normalization_plan(w, base, inp.reader, model, layout=inp.layout, join_pages=inp.join_pages, preset=inp.preset,
                                                 any_model=inp.skip_any_model, options=options)
                    stop()
                avg = _choice_avg(w, model)
                sys_tok = len(TEXT_CHOICE_SYSTEM) / CHARS_PER_TOKEN
                per_item = (np_["ask_chars"] / np_["ask"]) if np_ and np_.get("ask") else CHOICE_CHARS_PER_ITEM

                def tokens(ask, chars, calls):
                    if avg:
                        _, a_in, a_k, a_out = avg
                        return ([a_in * calls * 0.85, a_in * calls * 1.15], [a_k * calls * 0.85, a_k * calls * 1.15],
                                [a_out * calls * 0.7, a_out * calls * 1.3])
                    return ([chars / CHARS_PER_TOKEN * 0.8 + calls * sys_tok, chars / CHARS_PER_TOKEN * 1.3 + calls * sys_tok], [0, 0],
                            [ask * CHOICE_OUT_PER_ITEM[0], ask * CHOICE_OUT_PER_ITEM[1]])
                t_basis = f" · 토큰 이 책 {avg[0]:,}회 호출 평균" if avg else " · 토큰 기본값"
                if have and np_:
                    skipped.update(choice_reused=np_["reused"], choice_reused_other=np_.get("other_reused", 0),
                                   choice_remembered=np_["remembered"], choice_remembered_other=np_.get("other_remembered", 0))
                    c = np_["calls"]; ask = np_["ask"]; chars = np_.get("ask_chars", ask * CHOICE_CHARS_PER_ITEM)
                    tin, tk, tout = tokens(ask, chars, c)
                    # run 은 3차 판별 뒤에 문서를 만든다 — 3차가 〔a/b〕 를 글자로 바꾸면 그 곁 후보는 앞뒤 글(재사용 열쇠)이 달라져 다시 묻는다
                    near = np_.get("near_marks", 0) if kind == "run" and any(r["step"] == "glyph" and r["scope"] == "have" and r["items"] for r in rows) else 0
                    basis, detail = "후보 정확", f"후보 {np_['total']:,}건 가운데 새로 물을 {ask:,}건 — {CHOICE_BATCH}건씩"
                    if near:
                        c_hi = -(-(ask + near) // CHOICE_BATCH)
                        hin, hk, hout = tokens(ask + near, chars + near * per_item, c_hi)
                        c, tin, tk, tout = (c, c_hi), [tin[0], hin[1]], [tk[0], hk[1]], [tout[0], hout[1]]
                        basis = f"후보 정확(3차가 바꿀 〔a/b〕 곁 {near:,}건은 다시 물을 수 있음)"
                        detail += f", 이번 3차 판별이 앞뒤 글의 〔a/b〕 를 정하면 앞서 답을 얻은 후보 가운데 최대 {near:,}건을 더"
                    else:
                        c = (c, c)
                    rows.append(_row("choice", "have", len(have), model, basis + t_basis, c, tin, tk, tout, detail, ask))
                if new_n:
                    x = np_["total"] / len(base) if np_ and base else None
                    basis = f"어림: 쪽당 후보 {_pp(x)}건(이 책 {len(base):,}쪽 평균)" if x is not None else f"어림: 쪽당 후보 {CANDIDATES_PER_PAGE}건(기본값)"
                    ask = round((CANDIDATES_PER_PAGE if x is None else x) * new_n); c = -(-ask // CHOICE_BATCH)
                    tin, tk, tout = tokens(ask, ask * per_item, c)
                    rows.append(_row("choice", "new", new_n, model, basis + t_basis, (c, c), tin, tk, tout, f"새 후보 약 {ask:,}건(모두 새로 묻는 셈)", ask))
            if kind in ("run", "build") and inp.normalize and inp.context_model and (inp.normalize_traditional or inp.normalize_yun):
                stage("choice", choice)
    except PlanCancelled:
        raise
    except Exception as e:                                               # (1) PDF·쪽 범위·결과 폴더를 읽지 못함 — 아무것도 셀 수 없다
        errors.append({"step": "plan", "reason": _why(e)})
    # (7) 실제가 어림보다 적거나 많을 수 있는 까닭
    steps = {r["step"] for r in rows}
    if steps & {"glyph", "suspect"}:
        notes.append("3차 판별·합의 의심: 글자가 작아(저해상) 묻지 않는 자리는 빠지므로 실제 호출은 이보다 적을 수 있습니다.")
    if kind == "adjudicate":
        notes.append("앞뒤 글자는 글로 주지 않고(조각에는 보임) 후보 차례를 섞어 자형만 묻습니다(눈가림)."
                     + (" 고른 자리는 후보 없이 한 번 더 읽어 같을 때만 확정합니다." if inp.blind else "")
                     + " 획이 보이면 확정해 본문에 넣고, 보이지 않으면 표시를 그대로 둡니다.")
    if "choice" in steps:
        notes.append("문맥 교정: 앞뒤 글이 같은 후보는 한 번만 묻습니다. 새로 읽을 쪽의 후보는 모두 새로 묻는 셈이라 실제는 더 적기 쉽습니다.")
    if any(r["scope"] == "new" and r["model_step"] for r in rows):
        notes.append("'새로 읽을 쪽(어림)' 줄은 판독한 뒤에야 정확히 셀 수 있어 이 책 기록의 쪽당 평균(없으면 기본값)으로 어림했습니다.")
    notes.append("추정치입니다. 생각 토큰·재질문·제공사별 이미지 토큰 계산에 따라 실제 청구액은 달라집니다(캐시 보관료는 넣지 않음).")
    return plan


def price_plan(plan: dict, prices: dict | None, fx: float | None, fx_source: str = "") -> dict:
    """count_plan 의 결과에 줄마다 금액(usd [낮은, 높은] 또는 단가가 없으면 None)과 합계·원화·단가 출처를 붙인 새 사전. 다시 세지 않는다."""
    out = dict(plan); rows = []
    seen: dict[str, dict | None] = {}
    tot = {"calls": [0, 0], "in": [0, 0], "out": [0, 0], "usd": [0.0, 0.0], "usd_known": [0.0, 0.0], "missing_prices": []}
    all_priced = True
    for r in plan.get("rows") or []:
        r = dict(r); name = (r.get("model") or "").split(" (")[0].strip()
        if name not in seen:
            seen[name] = resolve_price(name, prices) if name else None
        pr = seen[name]
        if not r["calls"][1] and not r["in"][1] and not r["out"][1]:
            r["usd"] = [0.0, 0.0]                                        # 할 일이 없는 줄 — 단가가 없어도 0
        elif pr is None:
            r["usd"] = None; all_priced = False
            if name not in tot["missing_prices"]:
                tot["missing_prices"].append(name or "(모델 이름 없음)")
        else:
            r["usd"] = [_cost(max(0, r["in"][i] - r["cached"][i]), min(r["cached"][i], r["in"][i]), r["out"][i], pr) for i in (0, 1)]
        for k in ("calls", "in", "out"):
            tot[k] = [tot[k][0] + r[k][0], tot[k][1] + r[k][1]]
        if r["usd"] is not None:
            tot["usd_known"] = [tot["usd_known"][0] + r["usd"][0], tot["usd_known"][1] + r["usd"][1]]
        rows.append(r)
    tot["usd"] = list(tot["usd_known"]) if all_priced else None
    rate = fx or DEFAULT_FX_KRW
    out.update(rows=rows, totals=tot, fx=rate, fx_source=fx_source or ("직접 입력" if fx else f"기본값(조회 실패 시 {DEFAULT_FX_KRW:,.0f}원)"),
               krw=[v * rate for v in tot["usd"]] if tot["usd"] is not None else None, krw_known=[v * rate for v in tot["usd_known"]],
               price_lines=[f"{n}: 입력 {p['in']:.2f} · 출력 {p['out']:.2f} · 캐시 입력 {p.get('cached', p['in']):.3f} ({p.get('source', '')})" if p
                            else f"{n or '(모델 이름 없음)'}: 단가 없음" for n, p in seen.items()],
               models=list(seen), prices_date=PRICES_DATE)
    return out


def needs_confirm(priced: dict) -> bool:
    """창을 띄울지: 어림이 하나라도 실패했거나(닫힌 쪽으로) 호출이 생길 수 있으면."""
    return bool(priced.get("errors")) or (priced.get("totals") or {}).get("calls", [0, 0])[1] > 0


def default_choice(priced: dict) -> str:
    """기본(Enter) 단추: 'cancel'(판독 어림 실패 — 읽을 쪽이 있을 때 — 또는 전체 실패) | 'without'(모델 단계 어림 실패) | 'all'.
    금액을 낼 수 없는 줄(호출이 생기는데 단가 없음 — OpenAI 처럼 요금표에 없는 모델)도 어림 실패처럼 닫힌 쪽으로: 판독 줄이면 'cancel',
    모델 단계 줄뿐이면 run·build 는 'without'(판독·문서 만들기만), read·adjudicate 는 'cancel' — 금액을 보지 않고 Enter 로 돈이 나가지 않게."""
    steps = {e.get("step") for e in priced.get("errors") or []}
    if "plan" in steps or ("read" in steps and priced.get("to_read", 1)):
        return "cancel"
    if steps:
        return "without"
    unpriced = {r.get("step") for r in priced.get("rows") or [] if r.get("usd") is None and (r.get("calls") or [0, 0])[1] > 0}
    if unpriced & {"read1", "read2"}:
        return "cancel"
    if unpriced:
        return "without" if priced.get("kind") in ("run", "build") else "cancel"
    return "all"


def without_flags(kind: str, context_model: bool) -> list[str]:
    """[아니요 · 모델 단계 없이]: run 은 3차 판별(합의 의심 포함)을 빼고 문맥 교정 모델을 끈다(판독·문서 만들기는 함) · build 는 문맥 교정 모델만 · 그 밖에는 없음."""
    if kind == "run":
        return ["--no-adjudicate"] + (["--no-context-model"] if context_model else [])
    if kind == "build":
        return ["--no-context-model"] if context_model else []
    return []


def failed_plan(kind: str, reason: str) -> dict:
    """어림이 통째로 실패했을 때(작업 스레드의 예외)의 계획 — 창은 까닭을 보이고 [취소]를 기본으로. 문맥 교정은 켜진 셈으로(아니요 = 모델 없이 쪽이 늘 안전)."""
    return {"kind": kind, "model": "", "context_model": True, "skip_any_model": True, "force": False, "pages": 0, "records": 0, "to_read": 1, "new_pages": 0,
            "same_source": False, "rows": [], "notes": [], "skipped": {},
            "errors": [{"step": "read" if kind in ("run", "read") else "plan", "reason": reason}]}


def _span(v, f=lambda x: f"{x:,}") -> str:
    return f(v[0]) if v[0] == v[1] else f"{f(v[0])}~{f(v[1])}"


def money(priced: dict) -> str:
    """'약 12,300~25,600원 ($8.79~18.30)' 또는 '금액을 다 낼 수 없음 — 단가 없는 모델: …(아는 부분 …)'."""
    t = priced["totals"]
    if t["usd"] is not None:
        return f"약 {fmt_krw(priced['krw'][0])}~{fmt_krw(priced['krw'][1])} (${t['usd'][0]:.2f}~{t['usd'][1]:.2f})"
    known = t["usd_known"]
    return ("금액을 다 낼 수 없음 — 단가 없는 모델: " + ", ".join(t["missing_prices"])
            + (f" (아는 부분만 약 {fmt_krw(priced['krw_known'][0])}~{fmt_krw(priced['krw_known'][1])}, ${known[0]:.2f}~{known[1]:.2f})" if known[1] else ""))


def skipped_lines(plan: dict) -> list[str]:
    """다시 사지 않는 것: 3차 미정 자리 · 문맥 교정 재사용·건너뜀(다른 모델 몫을 따로)."""
    s = plan.get("skipped") or {}; out = []
    if s.get("glyph"):
        out.append(f"3차: 앞서 물어 결론 난 미정 자리 {s['glyph']:,}곳" + (f"(다른 모델 {s['glyph_other']:,}곳 포함)" if s.get("glyph_other") else "")
                   + ("" if plan.get("skip_any_model", True) else f"(같은 모델 {plan.get('model', '')} 이 물은 것만)") + "은 묻지 않음")
    if s.get("choice_reused") or s.get("choice_remembered"):
        out.append(f"문맥 교정: 고른 답 다시 씀 {s.get('choice_reused', 0):,}건" + (f"(다른 모델 {s['choice_reused_other']:,}건)" if s.get("choice_reused_other") else "")
                   + f" · 못 고른 후보 건너뜀 {s.get('choice_remembered', 0):,}건"
                   + (f"(다른 모델 {s['choice_remembered_other']:,}건 — 원문대로 둠)" if s.get("choice_remembered_other") else ""))
    if plan.get("skip_any_model", True) and any(s.get(k) for k in ("glyph_other", "choice_reused_other", "choice_remembered_other")):
        out.append("이번 모델로 다시 물으려면 [설정] 판독 칸 [세부 설정 ▾] 의 '다른 모델이 이미 물은 자리·후보는 다시 묻지 않기' 를 끄십시오(호출 듦).")
    return out


def format_plan(priced: dict) -> str:
    """진행 기록·시험용 글. 첫 줄은 합계 한 줄."""
    t = priced["totals"]
    lines = [f"비용 어림: 호출 {_span(t['calls'])}회 · 입력 토큰 {_span(t['in'], fmt_tokens)} · 출력 토큰 {_span(t['out'], fmt_tokens)} · {money(priced)}"
             + (f" · 어림하지 못한 단계 {len(priced['errors'])}곳" if priced.get("errors") else "")]
    for r in priced["rows"]:
        cost = f"${r['usd'][0]:.2f}~{r['usd'][1]:.2f}" if r["usd"] is not None else "단가 미입력"
        lines.append(f"· {r['label']} [{r['model']}] {r['basis']} · 호출 {_span(r['calls'])}회 · 입력 {_span(r['in'], fmt_tokens)} · 출력 {_span(r['out'], fmt_tokens)} · {cost}"
                     + (f" — {r['detail']}" if r.get("detail") else ""))
    lines += ["· " + x for x in skipped_lines(priced)]
    lines += ["· 어림하지 못함: " + {"read": "판독", "glyph": "3차 판별", "suspect": "합의 의심", "choice": "문맥 교정", "plan": "전체"}.get(e["step"], e["step"])
              + f" — {e['reason']}" for e in priced.get("errors") or []]
    lines += ["· " + n for n in priced.get("notes") or []]
    lines.append(f"· 환율 {priced['fx']:,.0f}원/달러({priced['fx_source']}) · 단가(USD/100만 토큰) — " + " | ".join(priced.get("price_lines") or ["-"]))
    return "\n".join(lines)
