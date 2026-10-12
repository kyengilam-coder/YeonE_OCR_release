"""판독 전에 토큰과 비용을 어림한다. 실측(이미 판독한 쪽의 usage)이 있으면 그 평균으로, 없으면 기본값으로.

Gemini 기준 기본값은 768×768 타일마다 258 토큰이다(작은 이미지는 258). 다른 제공사는 이 값을 참고치로만 쓴다.
지침 전문은 호출마다 함께 들어가며, Gemini 지침 캐시를 쓰면 그 부분은 캐시 입력 단가로 과금된다.
단가는 내장 요금표(PRICES, 기준일 PRICES_DATE)에서 모델 이름으로 찾고,
사용자가 설정에 넣은 값이 있으면 그것을 먼저 쓴다. 환율은 무료 공개 API 로 자동 조회하고 실패하면 기본값을 쓴다.
추정일 뿐이며, 생각(thinking) 토큰은 출력으로 과금되어 편차가 크다. 캐시 보관료(시간당 몇 센트)는 넣지 않는다.
"""
from __future__ import annotations
import json, math, re, urllib.request
from pathlib import Path

TILE, TOKENS_PER_TILE = 768, 258
CHARS_PER_TOKEN = 1.3            # 한글·한자 위주 글의 대략치
DEFAULT_REGIONS_PER_PAGE = 2.5   # 쪽당 문자 영역 수(2차 확대 조각 수)
DEFAULT_OUT_PAGE, DEFAULT_OUT_REGION = 2500, 900     # 출력 토큰 기본값(생각 토큰 제외)
PROMPT_TOK = 300                 # 지시문(지침 제외) 토큰
THINK_HIGH = 2.5                 # pro 계열 1차(영역 분류) 생각 토큰까지 넣은 높은 쪽 배수
THINK_HIGH_LOW = 1.5             # 2차(생각 줄임) 높은 쪽 배수

PRICES_DATE = "2026-10"
# USD / 100만 토큰. in=입력(텍스트·이미지, 20만 토큰 이하 구간), out=출력(생각 토큰 포함), cached=캐시 입력. 요금표: https://ai.google.dev/pricing
PRICES: dict[str, dict[str, float]] = {
    "gemini-3.1-pro":        {"in": 2.00, "out": 12.00, "cached": 0.20},
    "gemini-3-pro":          {"in": 2.00, "out": 12.00, "cached": 0.20},
    "gemini-3.1-flash-lite": {"in": 0.25, "out": 1.50, "cached": 0.025},
    "gemini-3-flash":        {"in": 0.50, "out": 3.00, "cached": 0.05},
    "gemini-2.5-pro":        {"in": 1.25, "out": 10.00, "cached": 0.31},
    "gemini-2.5-flash-lite": {"in": 0.10, "out": 0.40, "cached": 0.025},
    "gemini-2.5-flash":      {"in": 0.30, "out": 2.50, "cached": 0.075},
    "gemini-2.0-flash-lite": {"in": 0.075, "out": 0.30, "cached": 0.019},
    "gemini-2.0-flash":      {"in": 0.10, "out": 0.40, "cached": 0.025},
    "gemini-pro-latest":     {"in": 1.25, "out": 10.00, "cached": 0.31},
    "gemini-flash-lite-latest": {"in": 0.10, "out": 0.40, "cached": 0.025},
    "gemini-flash-latest":   {"in": 0.30, "out": 2.50, "cached": 0.075},
}
DEFAULT_FX_KRW = 1400.0          # 환율 조회에 실패했을 때
FX_URLS = ("https://open.er-api.com/v6/latest/USD", "https://api.frankfurter.app/latest?from=USD&to=KRW")
FX_MAX_BYTES = 1 << 20           # 환율 응답은 몇 KB — 1MB 를 넘으면 읽지 않고 버린다(엉뚱한 응답이 메모리를 채우지 않게)


def image_tokens(w: int, h: int) -> int:
    if max(w, h) <= 384:
        return TOKENS_PER_TILE
    return math.ceil(w / TILE) * math.ceil(h / TILE) * TOKENS_PER_TILE


def _norm_model(model: str) -> str:
    return (model or "").strip().lower().removeprefix("models/")


def default_price(model: str) -> dict | None:
    """내장 요금표에서 모델 이름에 가장 길게 들어맞는 항목. 없으면 None."""
    m = _norm_model(model)
    best = None
    for key in PRICES:
        if m.startswith(key) and (best is None or len(key) > len(best)):
            best = key
    return dict(PRICES[best], key=best) if best else None


def resolve_price(model: str, prices: dict | None) -> dict | None:
    """사용자가 넣은 단가가 있으면 그것(출처 '직접 입력'), 없으면 내장 요금표(출처 '내장 요금표')."""
    pr = (prices or {}).get(model) or {}
    try:
        pin, pout = float(pr.get("in", 0) or 0), float(pr.get("out", 0) or 0)
    except (TypeError, ValueError):
        pin = pout = 0.0
    if math.isfinite(pin) and math.isfinite(pout) and pin >= 0 and pout >= 0 and (pin > 0 or pout > 0):
        cached = pr.get("cached")
        try:
            cached = float(cached) if cached is not None else pin
        except (TypeError, ValueError):
            cached = pin
        if not math.isfinite(cached) or cached < 0:
            cached = pin
        return {"in": pin, "out": pout, "cached": cached, "source": "직접 입력", "key": model}
    d = default_price(model)
    if d:
        d["source"] = f"내장 요금표 {PRICES_DATE}"
    return d


def guess_second_model(primary: str, second: str) -> str:
    """비용 어림용 2차 모델 이름. 'auto' 는 1차와 같은 세대(또는 가장 가까운 아래 세대)의 flash 로 본다."""
    s = (second or "").strip()
    if s and s.lower() != "auto":
        return s
    if not s:
        return primary
    p = _norm_model(primary)
    if "flash" in p:
        return primary
    m = re.search(r"gemini-(\d+(?:\.\d+)?)", p)
    ver = float(m.group(1)) if m else 0.0
    cands = []
    for key in PRICES:
        mm = re.search(r"gemini-(\d+(?:\.\d+)?)-flash$", key)
        if mm and float(mm.group(1)) <= ver:
            cands.append((float(mm.group(1)), key))
    return max(cands)[1] if cands else primary


def fetch_fx_krw(timeout: float = 4.0) -> tuple[float, str] | None:
    """원/달러 환율을 무료 공개 API 에서 가져온다. 실패하면 None. 응답은 FX_MAX_BYTES 까지만 읽는다."""
    for url in FX_URLS:
        try:
            with urllib.request.urlopen(url, timeout=timeout) as r:
                raw = r.read(FX_MAX_BYTES + 1)
            if len(raw) > FX_MAX_BYTES:
                continue
            data = json.loads(raw.decode("utf-8"))
            rate = float((data.get("rates") or {}).get("KRW") or 0)
            if 0 < rate < 1e7:                                       # 엉뚱한 값(0·무한)은 버린다
                return rate, "오늘 자동 조회"
        except Exception:
            continue
    return None


def _cost(tok_in: float, tok_cached: float, tok_out: float, price: dict) -> float:
    """토큰 → USD. tok_in 은 캐시분을 뺀 입력."""
    return tok_in / 1e6 * price["in"] + tok_cached / 1e6 * price.get("cached", price["in"]) + tok_out / 1e6 * price["out"]


def _model_provider(model: str) -> str:
    name = _norm_model(model).split(" (")[0]
    if name.startswith("gemini-"):
        return "gemini"
    if name.startswith("claude-"):
        return "anthropic"
    if name.startswith(("gpt-", "chatgpt-", "o1", "o3", "o4")):
        return "openai"
    return ""


def _compatible_usage(record: dict, reader: str, model: str) -> bool:
    """모델을 바꿔 이어 읽을 때 이전 제공사·이전 1차 모델의 실측을 새 요금에 섞지 않는다."""
    if record.get("reader") and record["reader"] != reader:
        return False
    if record.get("model") and _norm_model(record["model"]) != _norm_model(model):
        return False
    names = list((record.get("usage") or {}).get("by_model") or {})
    if any(_model_provider(name) not in ("", reader) for name in names):
        return False
    # 제공사 기록이 없던 옛 Gemini 기록도 이어 읽는다. 새 제공사에서는 출처 없는 실측을 쓰지 않는다.
    return reader == "gemini" or record.get("reader") == reader or any(_model_provider(name) == reader for name in names)


def estimate(pdf_path: str, pages: list[int], *, upscale: float = 2.0, second_pass: bool = True, model: str = "",
             second_model: str = "", use_cache: bool = True, prices: dict | None = None, fx_krw: float | None = None,
             fx_source: str = "", records: list[dict] | None = None, page_size_px: tuple[int, int] | None = None,
             guide_chars: int | None = None, reader: str = "gemini") -> dict:
    """쪽 범위에 대한 호출·토큰·비용 어림. records 에 usage 가 있는 쪽이 있으면 그 평균으로 보정한다."""
    from .pdfio import open_pdf, page_image
    from .prompts import system_prompt
    n = len(pages)
    reader = (reader or "gemini").strip().lower()
    use_cache = use_cache and reader == "gemini"
    out = {"pages": n, "reader": reader, "model": model, "second_model": "", "basis": "기본값", "calls": 0, "in_low": 0, "in_high": 0, "out_low": 0, "out_high": 0,
           "usd_low": None, "usd_high": None, "krw_low": None, "krw_high": None, "fx": None, "fx_source": "",
           "already_read": 0, "use_cache": use_cache, "price_source": "", "page_px": page_size_px, "upscale": upscale,
           "rows": [], "price_lines": [], "notes": []}
    if n == 0:
        return out
    done = {r["pdf_page"] for r in (records or []) if "pass1" in r and not r.get("pass2_failed")}
    todo = [p for p in pages if p not in done]
    out["already_read"] = n - len(todo)
    redo = sorted(r["pdf_page"] for r in (records or []) if r.get("pass2_failed") and r["pdf_page"] in pages)
    if redo:                                               # 2차가 덜 된 쪽은 다시 읽는다 — 보통 덜 된 영역의 2차만이라 실제는 이보다 적다(1차 설정·모델이 바뀌었으면 1차도)
        out["notes"].append(f"2차가 덜 된 쪽 {len(redo)}개는 다 읽는 셈으로 잡았습니다(실제로는 덜 된 영역의 2차만 다시 묻는 일이 많아 더 적음).")
    m2 = (guess_second_model(model, second_model) if reader == "gemini" else
          model if (second_model or "").strip().lower() in ("", "auto") else second_model.strip()) if second_pass else model
    out["second_model"] = m2
    p1 = resolve_price(model, prices)
    p2 = resolve_price(m2, prices)
    usd_low = usd_high = 0.0
    priced = p1 is not None and (not second_pass or p2 is not None)
    missing_prices = set()
    if p1 is None:
        missing_prices.add(model)
    if second_pass and p2 is None:
        missing_prices.add(m2)

    measured_records = [r for r in (records or []) if r.get("usage", {}).get("calls")]
    measured = [r["usage"] for r in measured_records if _compatible_usage(r, reader, model)]
    if len(measured) < len(measured_records):
        out["notes"].append("이전 제공사·모델의 판독 기록은 완료 쪽 계산에만 쓰고, 현재 모델의 비용 실측에서는 제외했습니다.")
    if measured:
        k = len(measured)
        avg_calls = sum(u["calls"] for u in measured) / k
        avg_in = sum(u.get("prompt_tokens", 0) for u in measured) / k
        avg_out = sum(u.get("output_tokens", 0) + u.get("thinking_tokens", 0) for u in measured) / k
        out["basis"] = f"실측 {k}쪽 평균"
        out["calls"] = round(avg_calls * len(todo))
        out["in_low"], out["in_high"] = round(avg_in * len(todo) * 0.85), round(avg_in * len(todo) * 1.15)
        out["out_low"], out["out_high"] = round(avg_out * len(todo) * 0.7), round(avg_out * len(todo) * 1.3)
        # 모델별 실측은 각각의 단가만 적용한다. 모르는 모델을 1차 단가로 대신 계산하지 않는다.
        buckets: dict[str, dict] = {}
        for u in measured:
            bm = u.get("by_model") or {model or "?": u}
            for name, uu in bm.items():
                b = buckets.setdefault(name, {"prompt_tokens": 0, "cached_tokens": 0, "out": 0, "calls": 0})
                b["prompt_tokens"] += uu.get("prompt_tokens", 0); b["cached_tokens"] += uu.get("cached_tokens", 0); b["calls"] += uu.get("calls", 0)
                b["out"] += uu.get("output_tokens", 0) + uu.get("thinking_tokens", 0)
        for name, b in buckets.items():
            base_name = name.split(" (")[0]                                   # "gemini-2.5-pro (3차)" → 단가는 모델 이름으로
            pr = resolve_price(base_name, prices)
            scale = len(todo) / k
            c = None
            if pr is not None:
                c = _cost(max(0, b["prompt_tokens"] - b["cached_tokens"]) * scale, b["cached_tokens"] * scale, b["out"] * scale, pr)
                usd_low += c * 0.8; usd_high += c * 1.25
            else:
                priced = False
                missing_prices.add(base_name)
            label = ("3차 · 글자 판별" if "(3차" in name else
                     ("1차 · 쪽 전체 판독" if _norm_model(base_name) == _norm_model(model) or len(buckets) == 1 else "2차 · 확대 조각 확인"))
            out["rows"].append({"label": label,
                                "model": name, "calls": round(b.get("calls", 0) * scale),
                                "in_low": round(b["prompt_tokens"] * scale * 0.85), "in_high": round(b["prompt_tokens"] * scale * 1.15),
                                "out_low": round(b["out"] * scale * 0.7), "out_high": round(b["out"] * scale * 1.3),
                                "usd_low": c * 0.8 if c is not None else None, "usd_high": c * 1.25 if c is not None else None})
        if not any(u.get("by_model") for u in measured) and m2 != model:
            out["notes"].append("이전 판 기록이라 모델별 실측이 없어 전부 1차 단가로 계산했습니다.")
    else:
        if page_size_px is None:
            doc = open_pdf(pdf_path)
            try:
                img, _ = page_image(doc, pages[0]); page_size_px = img.size
            finally:
                doc.close()
        w, h = page_size_px
        page_img_tok = image_tokens(round(w * upscale), round(h * upscale))
        region_img_tok = image_tokens(round(w * 0.6 * 2.5), 1600)          # 영역 조각을 세로 1600px 안팎으로 확대
        guide_tok = round((guide_chars if guide_chars is not None else len(system_prompt())) / CHARS_PER_TOKEN)
        regions = DEFAULT_REGIONS_PER_PAGE if second_pass else 0.0
        calls_pp = 1 + (1 if second_pass else 0)                              # 2차는 조각들을 한 호출로 묶어 보냄
        in1, out1 = guide_tok + PROMPT_TOK + page_img_tok, DEFAULT_OUT_PAGE
        in2, out2 = (guide_tok + PROMPT_TOK + regions * region_img_tok, regions * DEFAULT_OUT_REGION) if second_pass else (0, 0)
        t = len(todo)
        out["calls"] = round(calls_pp * t)
        out["in_low"], out["in_high"] = round((in1 + in2) * t), round((in1 + in2) * t * 1.4)
        out["out_low"], out["out_high"] = round((out1 + out2) * t), round((out1 * THINK_HIGH + out2 * THINK_HIGH_LOW) * t)
        out["notes"].append(f"쪽 이미지 {w}×{h} → 확대 {upscale}배 {page_img_tok:,} 토큰/장 · 지침 약 {guide_tok:,} 토큰/호출"
                            + ("(캐시 단가 적용)" if use_cache else "(호출마다 전액)") + f" · 쪽당 호출 {calls_pp}회")
        if reader != "gemini":
            out["notes"].append("이 제공사의 실측이 아직 없어 이미지·출력 토큰은 Gemini 기준의 참고치입니다. 실제 토큰 계산과 차이가 날 수 있으며 캐시 할인은 가정하지 않았습니다.")
        def pass_cost(tin, tout, price, hi_in, hi_out):
            if price is None:
                return None, None
            g = guide_tok if use_cache else 0
            return (_cost(tin - g, g, tout, price) * t, _cost((tin - g) * hi_in, g, tout * hi_out, price) * t)
        lo1, hi1 = pass_cost(in1, out1, p1, 1.4, THINK_HIGH)
        lo2, hi2 = pass_cost(in2, out2, p2, 1.4, THINK_HIGH_LOW) if second_pass else (0.0, 0.0)
        if priced:
            usd_low, usd_high = lo1 + lo2, hi1 + hi2
        out["rows"].append({"label": "1차 · 쪽 전체 판독", "model": model, "calls": t, "in_low": round(in1 * t), "in_high": round(in1 * t * 1.4),
                            "out_low": round(out1 * t), "out_high": round(out1 * THINK_HIGH * t), "usd_low": lo1, "usd_high": hi1})
        if second_pass:
            out["rows"].append({"label": "2차 · 확대 조각 확인", "model": m2 + (" (auto)" if (second_model or "").strip().lower() == "auto" else ""),
                                "calls": t, "in_low": round(in2 * t), "in_high": round(in2 * t * 1.4),
                                "out_low": round(out2 * t), "out_high": round(out2 * THINK_HIGH_LOW * t), "usd_low": lo2, "usd_high": hi2})

    if priced:
        out["usd_low"], out["usd_high"] = usd_low, usd_high
        out["price_source"] = p1.get("source", "")
        fx = fx_krw or DEFAULT_FX_KRW
        out["fx"] = fx; out["fx_source"] = fx_source or ("직접 입력" if fx_krw else f"기본값(조회 실패 시 {DEFAULT_FX_KRW:,.0f}원)")
        out["krw_low"], out["krw_high"] = usd_low * fx, usd_high * fx
        def pl(name, pr, role):
            return f"{role} {name}: 입력 {pr['in']:.2f} · 출력 {pr['out']:.2f} · 캐시 입력 {pr.get('cached', pr['in']):.3f} ({pr.get('source', '')})"
        out["price_lines"].append(pl(model, p1, "1차"))
        if second_pass and (m2 != model):
            out["price_lines"].append(pl(m2 + (" (auto 추정)" if (second_model or "").strip().lower() == "auto" else ""), p2, "2차"))
    else:
        names = ", ".join(sorted(missing_prices))
        out["notes"].append(f"단가를 모르는 모델({names})이 있어 총금액을 내지 못했습니다. 해당 모델의 입력·출력 단가(USD/100만 토큰)를 직접 넣으면 계산합니다.")
    out["notes"].append("추정치입니다. 제공사별 이미지 처리·추론 토큰·캐시 정책에 따라 실제 청구액은 달라지며, 캐시 보관료는 넣지 않았습니다.")
    out["notes"].append("최종 HWPX·DOCX의 한자 문맥 교정에는 추가 모델 호출이 생길 수 있으며, 그 비용은 이 OCR 어림에 포함되지 않습니다.")
    return out


def fmt_tokens(n: float) -> str:
    """토큰 수를 읽기 쉽게: 1,511,500 → '151만'."""
    n = float(n or 0)
    if n >= 1e8:
        return f"{n / 1e8:.1f}억"
    if n >= 1e4:
        return f"{n / 1e4:,.0f}만"
    return f"{n:,.0f}"


def fmt_range(lo, hi, f) -> str:
    return f"{f(lo)}~{f(hi)}"


def fmt_krw(v: float) -> str:
    """원화는 100원 단위로 끊어 보여 준다(추정치라는 뜻)."""
    return f"{round(v / 100) * 100:,.0f}원"


def format_estimate(e: dict) -> str:
    if e["pages"] == 0:
        return "쪽 범위가 비어 있습니다."
    lines = [f"대상 {e['pages']}쪽" + (f" (이미 판독한 {e['already_read']}쪽은 건너뜀)" if e["already_read"] else "") + f" · 모델 {e['model'] or '-'} · 기준: {e['basis']}",
             f"예상 호출 {e['calls']:,}회 · 입력 토큰 {e['in_low']:,}~{e['in_high']:,} · 출력 토큰 {e['out_low']:,}~{e['out_high']:,}"]
    if e["usd_low"] is not None:
        money = f"예상 비용 ${e['usd_low']:.2f}~${e['usd_high']:.2f}"
        if e["krw_low"] is not None:
            money += f" (약 {e['krw_low']:,.0f}~{e['krw_high']:,.0f}원 · 환율 {e['fx']:,.0f}원/달러, {e['fx_source']})"
        lines.append(money)
        lines.append("단가(USD/100만 토큰) — " + " | ".join(e["price_lines"]))
    lines.extend("· " + n for n in e["notes"])
    return "\n".join(lines)
