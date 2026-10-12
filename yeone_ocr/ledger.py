"""혼동 장부(confusion ledger). 결과 폴더에 쌓인 판독 기록과 책갈피 OCR 대조 결과에서 '어느 글자가 어느 글자로 읽혔는가'를 모은다.

출처 네 가지
- 1차·2차 판독이 갈린 자리: readings/*.json 의 merged 글에 든 〔a/b〕 표시(= 두 모델이 다르게 읽은 글자)
- 판독 ↔ PDF 글자층(다른 OCR 엔진)의 한 글자 차이: readings/*.json 의 textlayer_diff
- 합의 판독 의심: readings/*.json 의 suspects 가운데 자형 판별이 글자층 쪽인 자리(outcome differs) — 1차·2차 합의 글자 → 자형 판별 글자(출처 '합의↔자형')
- 책갈피 OCR 대조: collate/*.json 의 항목(HWPX 글자 ↔ 판독 글자). A·B1·B2 모두 넣되 갈래를 함께 적는다

장부는 통계일 뿐 어느 쪽이 맞는지 정하지 않는다(지침 21: 자동 치환 금지). 쓰임새는
① 어떤 묶음이 진짜 다발인지 수치로 보기(report.md 의 "다발 오인식" 절), ② collate.CONFUSABLES 에 보탤 묶음 후보,
③ 3차 글자 판별(adjudicate)의 우선순위다.
"""
from __future__ import annotations
import json, re, collections
from pathlib import Path

MARK_AB = re.compile(r"〔([^〔〕/]{1,2})/([^〔〕]{1,2})〕")
CJK = re.compile(r"[一-鿿㐀-䶿豈-﫿]")


def _ctx(s: str, i: int, j: int, k: int = 5) -> str:
    return s[max(0, i - k):i] + "「" + s[i:j] + "」" + s[j:j + k]


def build_ledger(work_dir: str | Path) -> dict:
    """결과 폴더를 훑어 장부를 만든다. 돌려주는 값: {"pairs": [...], "sources": {...}, "pages": n, "sections": n}."""
    work = Path(work_dir)
    pairs: dict[tuple[str, str, str], dict] = {}      # (출처, 왼쪽, 오른쪽) → {count, examples}
    def add(src: str, a: str, b: str, ex: str, page=None, kind: str = ""):
        key = (src, a, b)
        d = pairs.setdefault(key, {"source": src, "a": a, "b": b, "count": 0, "examples": [], "kinds": collections.Counter()})
        d["count"] += 1
        if kind: d["kinds"][kind] += 1
        if len(d["examples"]) < 3:
            d["examples"].append({"page": page, "ctx": ex})
    n_pages = 0
    from .schema import PageReading, TEXT_KINDS, region_text
    from .adjudicate import SUSPECT_SOURCE
    for f in sorted((work / "readings").glob("p*.json")) if (work / "readings").exists() else []:
        try:
            rec = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        n_pages += 1
        pg = rec.get("pdf_page")
        try:
            body = "".join(region_text(r) for r in PageReading.model_validate(rec["merged"]).regions if r.kind in TEXT_KINDS)
        except Exception:
            body = ""
        for m in MARK_AB.finditer(body):
            a, b = m.group(1), m.group(2)
            add("1차↔2차", a, b, _ctx(body, m.start(), m.end()), pg)
        for it in (rec.get("textlayer_diff") or {}).get("items", []):
            if it.get("op") == "replace" and len(it.get("reading", "")) == 1 and len(it.get("layer", "")) == 1 and CJK.match(it["reading"]) and CJK.match(it["layer"]):
                add("판독↔글자층", it["reading"], it["layer"], it.get("ctx", ""), pg)
        for su in rec.get("suspects") or []:
            if isinstance(su, dict) and su.get("outcome") == "differs" and su.get("consensus") and su.get("layer"):
                add(SUSPECT_SOURCE, su["consensus"], su["layer"], (su.get("pre") or "")[-5:] + "「" + su["consensus"] + "」" + (su.get("post") or "")[:5], pg)
    n_sections = 0
    for f in sorted((work / "collate").glob("*.json")) if (work / "collate").exists() else []:
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        n_sections += 1
        for it in d.get("items", []):
            hw, oc = it.get("hwpx", ""), it.get("ocr", "")
            if it.get("action") in ("replace", "mark") and len(hw) == 1 and len(oc) == 1 and CJK.match(hw) and CJK.match(oc):
                add("HWPX↔판독", hw, oc, (it.get("hw_ctx") or "") + " | " + (it.get("oc_ctx") or ""), it.get("pdf_page"), it.get("kind", ""))
    rows = sorted(pairs.values(), key=lambda d: (-d["count"], d["source"], d["a"]))
    for r in rows:
        r["kinds"] = dict(r["kinds"])
    return {"pairs": rows, "pages": n_pages, "sections": n_sections}


def ledger_text(led: dict, top: int = 30) -> list[str]:
    """report.md 에 넣을 줄들."""
    rows = led.get("pairs") or []
    if not rows:
        return ["- (판독 기록·대조 결과가 없어 장부가 비어 있음)"]
    out = [f"- 바탕: 판독 기록 {led.get('pages', 0)}쪽 · 책갈피 OCR 대조 {led.get('sections', 0)}구획 · 글자 쌍 {len(rows)}종 {sum(r['count'] for r in rows)}건",
           "- 장부는 '다르게 읽힌 자리'의 통계이며 어느 쪽이 맞는지는 원본 확인으로 정합니다(지침 21).", ""]
    out.append("| 건수 | 출처 | 왼쪽 → 오른쪽 | 갈래 | 예 |")
    out.append("|---|---|---|---|---|")
    for r in rows[:top]:
        ex = r["examples"][0]["ctx"] if r["examples"] else ""
        kinds = " ".join(f"{k}{v}" for k, v in sorted(r["kinds"].items())) if r.get("kinds") else ""
        out.append(f"| {r['count']} | {r['source']} | {r['a']} → {r['b']} | {kinds} | {ex[:40]} |")
    if len(rows) > top:
        out.append(f"| … | | {len(rows) - top}종 더 | | |")
    return out


def confusable_candidates(led: dict, min_count: int = 2) -> list[tuple[str, str, int]]:
    """collate.CONFUSABLES 에 보탤 후보: 출처를 합쳐 min_count 회 이상 나온 한자 쌍(간체 키 기준은 호출 쪽에서)."""
    agg: collections.Counter = collections.Counter()
    for r in led.get("pairs") or []:
        a, b = r["a"], r["b"]
        agg[tuple(sorted((a, b)))] += r["count"]
    return [(a, b, n) for (a, b), n in agg.most_common() if n >= min_count]


def write_ledger(work_dir: str | Path) -> tuple[Path, dict]:
    """confusion.json 을 결과 폴더에 쓴다."""
    led = build_ledger(work_dir)
    out = Path(work_dir) / "confusion.json"
    out.write_text(json.dumps(led, ensure_ascii=False, indent=1), encoding="utf-8")
    return out, led
