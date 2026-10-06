"""판독 전에 토큰과 비용을 어림한다. 실측(이미 판독한 쪽의 usage)이 있으면 그 평균으로, 없으면 기본값으로.

Gemini 는 이미지를 768×768 타일로 나눠 타일마다 258 토큰으로 센다(작은 이미지는 258). 지침 전문은 호출마다 함께 들어간다.
단가는 Google AI Studio 요금표(https://ai.google.dev/pricing)에서 모델별 입력·출력 100만 토큰당 USD 를 넣어 쓴다.
추정일 뿐이며, 생각(thinking) 토큰은 출력으로 과금되어 편차가 크다. 캐시 할인은 계산에 넣지 않는다.
"""
from __future__ import annotations
import math
from pathlib import Path

TILE, TOKENS_PER_TILE = 768, 258
CHARS_PER_TOKEN = 1.3            # 한글·한자 위주 글의 대략치
DEFAULT_REGIONS_PER_PAGE = 2.5   # 2차 판독 호출 수(문자 영역 수)
DEFAULT_OUT_PAGE, DEFAULT_OUT_REGION = 2500, 900     # 출력 토큰 기본값(생각 토큰 제외)
THINK_HIGH = 2.5                 # pro 계열 생각 토큰까지 넣은 높은 쪽 배수


def image_tokens(w: int, h: int) -> int:
    if max(w, h) <= 384:
        return TOKENS_PER_TILE
    return math.ceil(w / TILE) * math.ceil(h / TILE) * TOKENS_PER_TILE


def estimate(pdf_path: str, pages: list[int], *, upscale: float = 2.0, second_pass: bool = True, model: str = "",
             prices: dict | None = None, fx_krw: float | None = None, records: list[dict] | None = None,
             page_size_px: tuple[int, int] | None = None, guide_chars: int | None = None) -> dict:
    """쪽 범위에 대한 호출·토큰·비용 어림. records 에 usage 가 있는 쪽이 있으면 그 평균으로 보정한다."""
    from .pdfio import open_pdf, page_image
    from .prompts import system_prompt
    n = len(pages)
    out = {"pages": n, "model": model, "basis": "기본값", "calls": 0, "in_low": 0, "in_high": 0, "out_low": 0, "out_high": 0,
           "usd_low": None, "usd_high": None, "krw_low": None, "krw_high": None, "already_read": 0, "notes": []}
    if n == 0:
        return out
    # 이미 기록이 있는 쪽은 다시 읽지 않는다(--force 아니면)
    done = {r["pdf_page"] for r in (records or []) if "pass1" in r}
    todo = [p for p in pages if p not in done]
    out["already_read"] = n - len(todo)
    measured = [r["usage"] for r in (records or []) if r.get("usage", {}).get("calls")]
    if measured:
        k = len(measured)
        avg_calls = sum(u["calls"] for u in measured) / k
        avg_in = sum(u.get("prompt_tokens", 0) for u in measured) / k
        avg_out = sum(u.get("output_tokens", 0) + u.get("thinking_tokens", 0) for u in measured) / k
        out["basis"] = f"실측 {k}쪽 평균"
        out["calls"] = round(avg_calls * len(todo))
        out["in_low"], out["in_high"] = round(avg_in * len(todo) * 0.85), round(avg_in * len(todo) * 1.15)
        out["out_low"], out["out_high"] = round(avg_out * len(todo) * 0.7), round(avg_out * len(todo) * 1.3)
    else:
        if page_size_px is None:
            doc = open_pdf(pdf_path); img, _ = page_image(doc, pages[0]); page_size_px = img.size
        w, h = page_size_px
        page_img_tok = image_tokens(round(w * upscale), round(h * upscale))
        region_img_tok = image_tokens(round(w * 0.6 * 2.5), 1600)          # 영역 조각을 세로 1600px 안팎으로 확대
        guide_tok = round((guide_chars if guide_chars is not None else len(system_prompt())) / CHARS_PER_TOKEN) + 400
        regions = DEFAULT_REGIONS_PER_PAGE if second_pass else 0.0
        calls_pp = 1 + regions
        in_pp = (guide_tok + page_img_tok) + regions * (guide_tok + region_img_tok)
        out_pp = DEFAULT_OUT_PAGE + regions * DEFAULT_OUT_REGION
        out["calls"] = round(calls_pp * len(todo))
        out["in_low"], out["in_high"] = round(in_pp * len(todo)), round(in_pp * len(todo) * 1.4)
        out["out_low"], out["out_high"] = round(out_pp * len(todo)), round(out_pp * len(todo) * THINK_HIGH)
        out["notes"].append(f"쪽 이미지 {w}×{h} → 확대 {upscale}배 {page_img_tok:,} 토큰/장, 지침 약 {guide_tok:,} 토큰/호출(호출마다 들어감)")
    pr = (prices or {}).get(model) or {}
    try:
        pin, pout = float(pr.get("in", 0) or 0), float(pr.get("out", 0) or 0)
    except (TypeError, ValueError):
        pin = pout = 0.0
    if pin > 0 or pout > 0:
        out["usd_low"] = out["in_low"] / 1e6 * pin + out["out_low"] / 1e6 * pout
        out["usd_high"] = out["in_high"] / 1e6 * pin + out["out_high"] / 1e6 * pout
        if fx_krw:
            out["krw_low"], out["krw_high"] = out["usd_low"] * fx_krw, out["usd_high"] * fx_krw
    else:
        out["notes"].append("단가(입력·출력 USD/100만 토큰)를 넣으면 금액도 보여 줍니다. 요금표: https://ai.google.dev/pricing")
    out["notes"].append("추정치입니다. 생각(thinking) 토큰은 출력으로 과금되어 편차가 크고, 캐시 할인은 넣지 않았습니다.")
    return out


def format_estimate(e: dict) -> str:
    if e["pages"] == 0:
        return "쪽 범위가 비어 있습니다."
    lines = [f"대상 {e['pages']}쪽" + (f" (이미 판독한 {e['already_read']}쪽은 건너뜀)" if e["already_read"] else "") + f" · 모델 {e['model'] or '-'} · 기준: {e['basis']}",
             f"예상 호출 {e['calls']:,}회 · 입력 토큰 {e['in_low']:,}~{e['in_high']:,} · 출력 토큰 {e['out_low']:,}~{e['out_high']:,}"]
    if e["usd_low"] is not None:
        money = f"예상 비용 ${e['usd_low']:.2f}~${e['usd_high']:.2f}"
        if e["krw_low"] is not None:
            money += f" (약 {e['krw_low']:,.0f}~{e['krw_high']:,.0f}원)"
        lines.append(money)
    lines.extend("· " + n for n in e["notes"])
    return "\n".join(lines)
