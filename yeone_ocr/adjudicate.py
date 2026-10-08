"""3차 글자 판별(adjudication). 1차·2차 판독이 갈린 자리(〔a/b〕)마다 그 글자 하나를 원본에서 크게 잘라 모델에 객관식으로 묻는다.

왜 따로 하는가
- 1차(쪽 전체)·2차(영역 조각)에서는 글자 하나에 토큰이 거의 배정되지 않아 획 하나 차이(入/人·曰/日·于/干)가 운에 맡겨진다.
- 글자 ±2자 창을 4~6배로 키우면 모델이 갈고리·점 하나를 볼 수 있다(지침 24장: 전체 → 영역 → 열 → 글자 순 확대를 코드로 옮긴 것).
- 자형이 보이면 확정하고, 보이지 않으면 표시를 그대로 둔다. 문맥으로 고르게 하지 않는다(지침 2·21).

위치 추정은 1차 판독의 영역 좌표·줄(열) 번호·글자 번호로 한다. 중의역대약론선 300dpi 쪽 네 자리에서 모두 맞았다.
"""
from __future__ import annotations
import re
from dataclasses import dataclass, asdict
from PIL import Image
from .schema import PageReading, Region, Column, TEXT_KINDS, bbox_frac, GlyphChoice
from .merge import columns_in_reading_order, MARK_RE

MARK_AB = re.compile(r"〔([^〔〕/]{1,2})/([^〔〕]{1,2})〕")


@dataclass
class Site:
    region_id: str
    col_index: int          # 영역 안 읽기 순서 열(가로쓰기면 줄) 번호, 0부터
    n_cols: int
    char_index: int         # 표시를 뺀 글에서 그 글자의 위치, 0부터
    n_chars: int
    a: str                  # 1차 글자
    b: str                  # 2차 글자
    pre: str
    post: str
    mark: str               # 본문에 있는 표시 글 그대로 (〔a/b〕)


def plain(text: str) -> str:
    """표시를 1차 글자로 접은 글(길이·위치 계산용)."""
    def rep(m):
        inner = m.group(0)[1:-1]
        if inner.startswith("疑:"):
            return inner[2:].strip()
        if "/" in inner:
            return inner.split("/")[0]
        return ""
    return MARK_RE.sub(rep, text)


def find_sites(merged: PageReading) -> list[Site]:
    """병합 판독문의 〔a/b〕 자리를 모두 찾는다(한두 글자짜리만)."""
    out: list[Site] = []
    for reg in merged.regions:
        if reg.kind not in TEXT_KINDS:
            continue
        cols = columns_in_reading_order(reg)
        for ci, col in enumerate(cols):
            text = col.text or ""
            for m in MARK_AB.finditer(text):
                before = plain(text[:m.start()])
                after = plain(text[m.end():])
                out.append(Site(reg.id, ci, len(cols), len(before), len(before) + len(m.group(1)) + len(after),
                                m.group(1), m.group(2), before[-4:], after[:4], m.group(0)))
    return out


def glyph_box(bbox: list[int], site: Site, horizontal: bool, span: int = 2) -> tuple[float, float, float, float]:
    """영역 좌표와 줄·글자 번호로 글자 ±span 자 창의 비율 좌표를 추정한다."""
    x0, y0, x1, y1 = bbox_frac(bbox)
    n_cols, L = max(1, site.n_cols), max(1, site.n_chars)
    k = len(site.a)
    if horizontal:                                  # 줄은 위→아래, 글자는 왼→오른쪽
        lh = (y1 - y0) / n_cols; cw = (x1 - x0) / L
        ya, yb = y0 + site.col_index * lh - lh * 0.35, y0 + (site.col_index + 1) * lh + lh * 0.35
        xa, xb = x0 + max(0, site.char_index - span) * cw, x0 + min(L, site.char_index + k + span) * cw
    else:                                           # 열은 오른→왼쪽(col_from_right 1 이 맨 오른쪽), 글자는 위→아래
        cw = (x1 - x0) / n_cols; ch = (y1 - y0) / L
        xb = x1 - site.col_index * cw + cw * 0.35; xa = x1 - (site.col_index + 1) * cw - cw * 0.35
        ya, yb = y0 + max(0, site.char_index - span) * ch, y0 + min(L, site.char_index + k + span) * ch
    return (max(0.0, xa), max(0.0, ya), min(1.0, xb), min(1.0, yb))


def glyph_crop(img: Image.Image, box: tuple[float, float, float, float], zoom: int = 6, max_side: int = 1800) -> Image.Image:
    W, H = img.size
    crop = img.crop((int(box[0] * W), int(box[1] * H), max(int(box[0] * W) + 4, int(box[2] * W)), max(int(box[1] * H) + 4, int(box[3] * H))))
    f = min(zoom, max_side / max(1, crop.width, crop.height))
    if f > 1:
        crop = crop.resize((int(crop.width * f), int(crop.height * f)), Image.LANCZOS)
    return crop


def apply_choice(merged: PageReading, site: Site, choice: str) -> bool:
    """표시 자리를 고른 글자로 바꾼다. 같은 표시가 그 열에 여럿이면 첫 번째."""
    for reg in merged.regions:
        if reg.id != site.region_id:
            continue
        cols = columns_in_reading_order(reg)
        if site.col_index >= len(cols):
            return False
        col = cols[site.col_index]
        if site.mark in (col.text or ""):
            col.text = col.text.replace(site.mark, choice, 1)
            return True
    return False


def apply_adjudications(merged: PageReading, adjudications: list[dict] | None) -> int:
    """저장된 판별 결과를 병합 판독문에 다시 적용한다(비교 다시 계산 뒤에도 판별이 남게). 적용한 수를 돌려준다."""
    n = 0
    for a in adjudications or []:
        if not a.get("choice"):
            continue
        site = Site(**{k: a[k] for k in Site.__dataclass_fields__ if k in a})
        if apply_choice(merged, site, a["choice"]):
            n += 1
    return n


def adjudicate_page(rec: dict, img: Image.Image, reader, horizontal: bool, max_sites: int = 0, zoom: int = 6) -> dict:
    """한 쪽의 〔a/b〕 자리를 판별한다. rec 을 고쳐 돌려준다(merged 글, adjudications, uncertain 메모)."""
    merged = PageReading.model_validate(rec["merged"])
    p1 = PageReading.model_validate(rec["pass1"]) if rec.get("pass1") else merged
    bboxes = {r.id: r.bbox for r in p1.regions}
    sites = find_sites(merged)
    if max_sites:
        sites = sites[:max_sites]
    done = list(rec.get("adjudications") or [])
    stats = {"sites": len(sites), "decided": 0, "undecided": 0, "a": 0, "b": 0, "failed": 0}
    for site in sites:
        box = glyph_box(bboxes.get(site.region_id, [0, 0, 1000, 1000]), site, horizontal)
        crop = glyph_crop(img, box, zoom=zoom)
        try:
            res = reader.adjudicate(crop, [site.a, site.b], site.pre, site.post, horizontal, rec.get("pdf_page", 0))
        except Exception as e:                       # 한 자리 실패가 쪽 전체를 멈추지 않게
            stats["failed"] += 1
            done.append({**asdict(site), "box": [round(v, 4) for v in box], "choice": "", "evidence": f"판별 실패: {str(e)[:80]}"})
            continue
        choice = (res.choice or "").strip()
        entry = {**asdict(site), "box": [round(v, 4) for v in box], "choice": choice if choice in (site.a, site.b) else "", "evidence": (res.evidence or "")[:200]}
        if entry["choice"]:
            apply_choice(merged, site, entry["choice"]); stats["decided"] += 1; stats["a" if entry["choice"] == site.a else "b"] += 1
            for u in merged.uncertain:
                if site.mark in (u.context or "") and "3차 판별" not in u.context:
                    u.context = u.context + f" — 3차 판별: 「{entry['choice']}」 확정({entry['evidence'][:40]})"
        else:
            stats["undecided"] += 1
            for u in merged.uncertain:
                if site.mark in (u.context or "") and "3차 판별" not in u.context:
                    u.context = u.context + " — 3차 판별: 자형으로 가리지 못함(원본 확인)"
        done.append(entry)
    rec["merged"] = merged.model_dump()
    rec["adjudications"] = done
    rec["adjudicate_stats"] = stats
    return rec


_PUNCT = re.compile(r"[\s　·ㆍ・•\.。,，、:：;；!！?？()（）\[\]【】『』「」《》〈〉“”\"'‘’—─―－\-_/／\\|*＊~～…‥0-9０-９]")


def locate_in_reading(merged: PageReading, pre: str, ch: str, post: str, max_ctx: int = 4) -> Site | None:
    """책갈피 OCR 대조 항목(판독 앞뒤 글자 pre「ch」post)의 자리를 병합 판독문에서 찾는다. 부호·숫자는 빼고 맞춘다.
    찾으면 글자 창 좌표 추정에 쓸 Site(표시 글은 빈 글)를 돌려준다."""
    pre_k, post_k = _PUNCT.sub("", pre)[-max_ctx:], _PUNCT.sub("", post)[:max_ctx]
    for a, b in ((4, 4), (3, 3), (2, 2), (4, 1), (1, 4), (4, 0), (0, 4), (2, 1), (1, 2), (3, 0), (0, 3), (1, 1), (2, 0), (0, 2), (1, 0), (0, 1)):   # 문단 첫·끝 글자는 한쪽 문맥만으로
        pk, nk = pre_k[-a:] if a else "", post_k[:b] if b else ""
        if (a and len(pk) < a) or (b and len(nk) < b):
            continue
        needle = pk + ch + nk
        hits: list[Site] = []
        for reg in merged.regions:
            if reg.kind not in TEXT_KINDS:
                continue
            cols = columns_in_reading_order(reg)
            for ci, col in enumerate(cols):
                text = plain(col.text or "")
                stripped = []; idx = []
                for i, c in enumerate(text):
                    if not _PUNCT.match(c):
                        stripped.append(c); idx.append(i)
                st = "".join(stripped)
                j = st.find(needle)
                while j >= 0:
                    pos = j + len(pk)
                    hits.append(Site(reg.id, ci, len(cols), idx[pos], len(text), ch, "", text[max(0, idx[pos] - 4):idx[pos]], text[idx[pos] + 1:idx[pos] + 5], ""))
                    j = st.find(needle, j + 1)
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1 and a + b >= 4:
            return hits[0]
    return None
