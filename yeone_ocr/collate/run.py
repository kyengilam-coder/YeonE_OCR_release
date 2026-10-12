"""책갈피 교정 한 번 돌리기: 색인 → 짝짓기 → 쪽 범위 → 판독(기록 재사용) → 구획 글 추출 → 대조 → 보고 → (반영·저장).

명령줄:  yeone-ocr collate <pdf> --hwpx <hwpx> --list
         yeone-ocr collate <pdf> --hwpx <hwpx> --section "大薊" [--apply] [--policy all|sure|memo] [--out 파일]
"""
from __future__ import annotations
import html, json, os, re, time, unicodedata
from argparse import Namespace
from contextlib import ExitStack
from dataclasses import asdict
from pathlib import Path
from . import zh
from .sections import HwpxBook, Anchor, Section, find_anchor, normalize_name
from .pdf_outline import pdf_bookmarks, match_anchors, page_range, PdfAnchor, find_title_pages
from .collate import hwpx_cells, ocr_cells, collate, summary, Item, witness_filter, book_titles, promote_repeated, REPEAT_MIN
from .hwpx_edit import Editor
from .labels import POLICIES, ANCHOR_SOURCES
from ..grades import COLLATE_LEGEND


def log(msg: str) -> None:
    from ..cli import log as _log
    _log(msg)


# ---------- 색인 ----------

def index_both(pdf_doc, book: HwpxBook, source: str = "auto", styles: list[str] | None = None):
    """구획 기준과 PDF 책갈피를 뽑고 짝짓는다. source: auto | hwpx | pdf.
    - hwpx: HWPX 의 책갈피(hp:bookmark)와 제목 스타일 문단
    - pdf : PDF 책갈피 이름을 HWPX 본문에서 찾아 그 문단을 기준으로(책갈피도 제목 스타일도 없는 HWPX 용)
    - auto: hwpx 로 하나라도 나오면 hwpx, 아니면 pdf"""
    from .pdf_outline import Match
    pdf_anchors = pdf_bookmarks(pdf_doc)
    anchors = book.anchors(styles=styles) if source in ("auto", "hwpx") else []
    if source == "pdf" or (source == "auto" and not anchors):
        found = book.anchors_from_titles([(p.title, p.level) for p in pdf_anchors])
        anchors = [a for _, a in found if a is not None]
        matches = [Match(a, pdf_anchors[j], 1.0, "pdf") for j, a in found if a is not None]
        return anchors, pdf_anchors, matches
    matches = match_anchors(anchors, pdf_anchors)
    return anchors, pdf_anchors, matches


def list_text(anchors, pdf_anchors, matches, limit: int = 0) -> str:
    lines = [f"HWPX 기준 {len(anchors)}개(책갈피 {sum(1 for a in anchors if a.kind == 'bookmark')}, 제목 {sum(1 for a in anchors if a.kind == 'heading')}, "
             f"PDF 책갈피로 찾음 {sum(1 for a in anchors if a.kind == 'pdf')}) · PDF 책갈피 {len(pdf_anchors)}개 · "
             f"짝: 정확 {sum(1 for m in matches if m.how in ('exact', 'pdf'))}, 품음 {sum(1 for m in matches if m.how == 'contains')}, "
             f"비슷 {sum(1 for m in matches if m.how == 'fuzzy')}, 없음 {sum(1 for m in matches if m.how == 'none')}"]
    for i, m in enumerate(matches):
        if limit and i >= limit:
            lines.append(f"… {len(matches) - limit}개 더"); break
        a = m.anchor
        pdf = f"PDF 「{m.pdf.title}」 {m.pdf.page}쪽" if m.pdf else "PDF 짝 없음"
        from . import reading
        lines.append(f"{i + 1:4d}. {'  ' * (a.level - 1)}{a.name[:30]:<32} [{reading.reading(a.name)[:16]}] 문단 {a.para:>5}  {pdf}  ({m.how}{'' if m.how != 'fuzzy' else f' {m.score:.2f}'})")
    return "\n".join(lines)


# ---------- 판독 글 뽑기 ----------
def single_read_regions(rec: dict, pr=None) -> set[str]:
    """2차 판독을 돌린 쪽에서 2차가 (온전히) 없어 1차만으로 들어간 문자 영역 id — 그 글은 1차·2차 대조를 거치지 않았다.
    2차 실패(pass2_failed, 조각 일부 실패·덜 읽은 조각), 2차 답이 비어 pass2 에 없는 영역, '2차 판독 실패(' 확인 항목,
    1차만 들어간 긴 구간(merge.LONG_SEG_NO_P2·LONG_SEG_DIFFER)이 있는 영역. 2차를 끈 기록(pass2 가 비고 실패 표시도 없음)은 넣지 않는다."""
    from ..merge import LONG_SEG_NO_P2, LONG_SEG_DIFFER
    from ..schema import PageReading, TEXT_KINDS
    from ..grades import _pass2_regions
    pr = pr or PageReading.model_validate(rec["merged"])
    p2 = rec.get("pass2") or {}; pf = rec.get("pass2_failed") or []
    out = set(pf)                                                     # [id] 또는 {id: 사유}
    for rid, v in p2.items():
        v = v if isinstance(v, dict) else {}
        if v.get("pass2_failed") or int(v.get("chunks_read", v.get("chunks", 1)) or 0) < int(v.get("chunks", 1) or 1):
            out.add(rid)
    out |= {u.region_id for u in pr.uncertain if u.region_id and (u.context or "").startswith(("2차 판독 실패(", LONG_SEG_NO_P2, LONG_SEG_DIFFER))}
    if p2 or pf:
        got = _pass2_regions(rec)
        out |= {r.id for r in pr.regions if r.kind in TEXT_KINDS and r.id not in got}
    return out


def reading_paras(rec: dict) -> list[tuple[int, str, bool]]:
    """쪽 기록 → [(쪽, 문단 글, 1차만)] 읽기 순서대로. 제목·본문·소주·단순표 글을 모두 넣는다(〔〕 표시 포함).
    1차만: 2차 판독이 빠져 1차만으로 읽은 문자 영역의 글(single_read_regions) — 책갈피 교정은 그 글자의 차이를 표시만 한다."""
    from ..schema import PageReading, TEXT_KINDS, TITLE_KINDS, SKIP_KINDS, paragraph_text
    pr = PageReading.model_validate(rec["merged"])
    once = single_read_regions(rec, pr)
    out = []
    for reg in pr.regions:
        if reg.kind in SKIP_KINDS:
            continue
        if reg.kind in TITLE_KINDS and reg.title_text.strip():
            out.append((pr.pdf_page, reg.title_text.strip(), False))
        elif reg.kind in TEXT_KINDS or reg.kind == "기타":
            for p in reg.paragraphs:
                t = paragraph_text(p).strip()
                if t:
                    out.append((pr.pdf_page, t, reg.kind in TEXT_KINDS and reg.id in once))
        elif reg.kind == "단순표":
            for r in reg.table_rows:
                t = " ".join(c for c in r.cells if c).strip()
                if t:
                    out.append((pr.pdf_page, t, False))
    return out


def reading_text(rec: dict) -> list[tuple[int, str]]:
    """쪽 기록 → [(쪽, 문단 글)] 읽기 순서대로(reading_paras 에서 '1차만' 을 뺀 것)."""
    return [(pg, t) for pg, t, _ in reading_paras(rec)]


def _key(s: str) -> str:
    return zh.compare_key(normalize_name(s))


def trim_to_section(paras: list[tuple], title: str, next_title: str | None) -> tuple[list[tuple], str]:
    """쪽들의 글에서 구획만 남긴다: 제목 글이 처음 나오는 문단부터, 다음 제목 글이 나오는 문단 직전까지. 못 찾으면 전체(메모 남김).
    paras 의 항목은 (쪽, 글, …) — 둘째 값만 보고 항목은 그대로 돌려준다(reading_paras 의 '1차만' 도 따라간다)."""
    k, kn = _key(title), _key(next_title) if next_title else ""
    start = 0; note = []
    for i, x in enumerate(paras):
        if k and _key(x[1]).startswith(k[: max(2, len(k))]):
            start = i; break
    else:
        note.append("구획 제목을 판독 글에서 찾지 못해 첫 쪽 처음부터 넣음")
    end = len(paras)
    if kn:
        for i in range(start + 1, len(paras)):
            if _key(paras[i][1]).startswith(kn[: max(2, len(kn))]):
                end = i; break
        else:
            note.append("다음 구획 제목을 판독 글에서 찾지 못해 마지막 쪽 끝까지 넣음")
    cut = paras[start:end]
    # 구획 제목이 되풀이된 줄(쪽마다 다시 찍힌 머리 제목, 제목 영역과 첫 문단의 중복)과, 제목의 일부만으로 된 짧은 줄
    # (「車前子·車前草」 구획 안의 소제목 车前子·车前草, 쪽 머리의 车前子)은 첫 줄 뒤로는 거른다 — 「车前子车前子」 중복 삽입 방지
    if k:
        out = []
        for i, x in enumerate(cut):
            tk = _key(x[1])
            if i > 0 and tk and (tk == k or (len(tk) >= 2 and len(tk) <= 8 and tk in k)):
                continue
            out.append(x)
        cut = out
    return cut, " · ".join(note)


# ---------- 보고 ----------
def memo_mode(args) -> str:
    """책갈피 교정의 메모 방식: --memo 가 있으면 그것, --no-memo 면 inline, 아니면 notice(고지 사항만)."""
    return getattr(args, "memo", None) or ("inline" if getattr(args, "no_memo", False) else "notice")


def _item_json(it: Item, mode: str) -> dict:
    from ..grades import collate_grade, needs_memo
    d = asdict(it); d["grade"] = collate_grade(it); d["memo"] = needs_memo(it, mode) if it.kind != "D" else False
    return d


def report_html(section: Section, pdf_anchor: PdfAnchor | None, pages: tuple[int, int], hw_paras: list[str], oc_paras: list[tuple],
                items: list[Item], policy: str, applied: list | None, out_path: Path, note: str = "", hwpx_file: str = "", mode: str = "notice") -> str:
    """mode: 실제 메모 방식(memo_mode) — 항목마다 '메모를 다는지' 를 그 방식으로 보인다(json 의 memo 와 같게)."""
    sm = summary(items)
    def esc(s): return html.escape(s or "")
    from ..grades import collate_grade, needs_memo
    gname = {"green": "<span style='color:#0B7A3B;text-decoration:underline dotted'>초록</span>", "blue": "<span style='color:#1F4FBF;background:#E1EBFF;text-decoration:underline dashed'>파랑</span>",
             "red": "<span style='color:#C00000;background:#FFFF00'><b>빨강</b></span>"}
    mlabel = {"notice": "고지 메모", "all": "메모", "inline": "글 속 〔〕 표기"}.get(mode, "메모")
    rows = []
    for it in items:
        cls = {"A": "a", "B1": "b1", "B2": "b2", "C": "c"}.get(it.kind, "")
        g = collate_grade(it)
        grade = "—" if g is None else gname[g] + (f" · {mlabel}" if needs_memo(it, mode) else "")
        rows.append(f"<tr class='{cls}'><td>{it.kind}</td><td>{'자동 반영' if it.auto else '표시만'}<br><small>{grade}</small></td><td>{it.para}</td>"
                    f"<td class=ctx>{esc(it.hw_ctx) or esc(it.hwpx) or '—'}</td><td class=ctx>{esc(it.oc_ctx) or esc(it.ocr) or '—'}</td>"
                    f"<td><b>{esc(it.new) or '—'}</b></td><td>{esc('/'.join(it.candidates))}</td><td>{it.pdf_page}</td><td>{esc(it.note)}</td></tr>")
    appl = ""
    if applied is not None:
        bad = [a for a in applied if not a.ok]
        appl = f"<p>반영 {sum(1 for a in applied if a.ok)}건" + (f", 실패 {len(bad)}건: " + "; ".join(esc(a.note) for a in bad[:5]) if bad else "") + "</p>"
    hw = "".join(f"<p>{esc(t)}</p>" for t in hw_paras)
    oc = "".join(f"<p><small>{pg}쪽{' · 1차만' if once and once[0] else ''}</small> {esc(t)}</p>" for pg, t, *once in oc_paras)
    css = """body{font-family:'Noto Serif KR',serif;margin:0;background:#f4efe4;color:#231c18}header{padding:12px 20px;background:#3b1b14;color:#f1e7cf}
    h1{font-size:18px;margin:0}main{padding:16px 20px}table{border-collapse:collapse;width:100%;font-size:13px;background:#fff}td,th{border:1px solid #d9d2c3;padding:4px 6px;vertical-align:top}
    tr.a td:first-child{background:#d8f5c8}tr.b2 td:first-child{background:#e6f0ff}tr.b1 td:first-child,tr.c td:first-child{background:#ffe9a8}
    .cols{display:grid;grid-template-columns:1fr 1fr;gap:16px}.cols>div{background:#fff;padding:10px;border:1px solid #d9d2c3;max-height:520px;overflow:auto;font-size:14px;line-height:1.7}
    .sum span{display:inline-block;margin-right:12px;padding:2px 8px;border-radius:10px;background:#e4d7b4}
    td.ctx{font-size:15px;letter-spacing:.02em}td.ctx b{color:#b3261e}"""
    doc = (f"<!doctype html><html lang=ko><head><meta charset=utf-8><title>교정 · {esc(section.anchor.name)}</title><style>{css}</style></head><body>"
           f"<header><h1>책갈피 교정 — {esc(section.anchor.name)}</h1><small>HWPX 문단 {section.start}~{section.end - 1} · PDF {pages[0]}~{pages[1]}쪽"
           + (f" · PDF 책갈피 「{esc(pdf_anchor.title)}」" if pdf_anchor else " · PDF 책갈피 짝 없음(쪽 범위 직접 지정)") + f" · 정책: {esc(POLICIES.get(policy, policy))}"
           + (f"<br>기준 HWPX: {esc(hwpx_file)}" if hwpx_file else "") + f"<br>{esc(COLLATE_LEGEND)}</small></header><main>"
           f"<p class=sum><span>자동 교정 {sm['auto']}</span><span>표시만 {sm['mark']}</span><span>A {sm['A']}</span><span>B1 {sm['B1']}</span><span>B2 {sm['B2']}</span><span>C {sm['C']}</span></p>"
           + (f"<p><b>참고:</b> {esc(note)}</p>" if note else "") + appl +
           f"<table><tr><th>갈래</th><th>처리</th><th>문단</th><th>HWPX (「」 안이 해당 자리)</th><th>판독(간체)</th><th>반영</th><th>다른 후보</th><th>PDF 쪽</th><th>설명</th></tr>{''.join(rows) or '<tr><td colspan=9>차이 없음</td></tr>'}</table>"
           f"<h3>원문 대조</h3><div class=cols><div><b>HWPX 구획(교정 전)</b>{hw}</div><div><b>PDF 판독</b>{oc}</div></div></main></body></html>")
    out_path.write_text(doc, encoding="utf-8")
    return str(out_path)


# ---------- 한 구획 처리 ----------
class Cancelled(Exception):
    """[중지]로 쪽 판독이 멈춤 — 오류가 아니라 사용자가 멈춘 것(창에 '중지됨'으로 보이게 따로 다룬다)."""


class ReadFailed(RuntimeError):
    """쪽 판독이 오류로 끝남(키·한도·모델 이름 등). 다음 구획도 같은 오류가 날 것이므로 전체 교정을 멈춘다."""


class SaveFailed(RuntimeError):
    """교정본을 저장하지 못함(한컴에 열려 있음 등). 다음 구획도 같은 파일에 저장하므로 전체 교정을 멈춘다."""


class PartialRead(RuntimeError):
    """구획 쪽 일부의 판독이 실패해(일시 오류) 기록이 없음. 빠진 쪽과 대조하면 그 쪽 글이 모두 '판독에 없는 긴 구간'(C)으로 교정본에 남고
    다음에 판독해도 지워지지 않으므로 이 구획은 대조하지 않는다. 다른 구획과는 무관하므로 전체 교정은 계속한다(ReadFailed 와 달리)."""


def ensure_pages_read(args, work, doc, pages: tuple[int, int]) -> bool:
    """범위 쪽 중 기록이 없는 쪽과 2차가 일부 영역에서 실패한 채 저장된 쪽(pass2_failed — cmd_read 가 그 영역의 2차만 다시 묻는다)을
    준비·판독한다. 일부 쪽 판독이 실패했으면(cmd_read 3) True — 기록이 없는 쪽이 있으면 PartialRead. 2차가 여전히 덜 된 영역은
    run_section 이 표시만 하고(1차만 읽은 글), 다시 판독만 실패한 쪽은 있던 기록으로 대조한다."""
    from ..cli import cmd_prepare, cmd_read
    from ..pdfio import parse_pages
    rng = range(pages[0], pages[1] + 1); force = getattr(args, "force", False); redo2 = not getattr(args, "no_second_pass", False)
    before = {n: work.load_record(n) for n in rng}
    need = [n for n in rng if before[n] is None or force or (redo2 and before[n].get("pass2_failed"))]
    if not need:
        log(f"쪽 {pages[0]}~{pages[1]} 판독 기록이 모두 있어 모델을 부르지 않습니다."); return False
    sub = Namespace(**vars(args)); sub.pages = ",".join(str(n) for n in need)
    log(f"쪽 {sub.pages} 준비·판독 …")
    cmd_prepare(sub, work, doc)
    rc = cmd_read(sub, work, doc)
    if rc == 130:
        raise Cancelled()
    if rc == 3:                                       # 일부 쪽만 실패(일시 오류) — 다음 구획과는 무관
        after = {n: work.load_record(n) for n in rng}
        missing = [n for n in rng if after[n] is None]
        if missing:
            raise PartialRead(f"쪽 {', '.join(map(str, missing[:10]))}{' …' if len(missing) > 10 else ''} 판독 실패")
        # 다시 판독하지 못해 이전 기록 그대로인 쪽(2차 덜 된 쪽은 run_section 이 '1차만' 으로 알린다)
        kept = [n for n in need if before[n] is not None and (after[n] or {}).get("read_at") == before[n].get("read_at") and not after[n].get("pass2_failed")]
        if kept:
            log(f"주의: 쪽 {', '.join(map(str, kept[:10]))}{' …' if len(kept) > 10 else ''} 을(를) 다시 판독하지 못했습니다(위 오류). 이전 판독 기록으로 대조합니다.")
        return True
    if rc not in (0, None):
        raise ReadFailed(f"판독이 끝나지 않음(rc={rc})")
    return False


_READER_CACHE: dict = {}


def _reader_cache_key(args) -> tuple:
    """(모델, 설정, 키 목록의 sha1) — 창에서 API 키를 바꾸면 새 판독기를 만든다(옛 키로 계속 부르지 않게). 키 자체는 들고 있지 않는다."""
    import hashlib
    from ..readers import split_keys, environment_key, default_model
    provider = getattr(args, "reader", "gemini")
    keys = split_keys(provider, environment_key(provider))
    return (provider, getattr(args, "model", None) or default_model(provider), getattr(args, "adjudicate_thinking", "low"),
            hashlib.sha1("\n".join(keys).encode("utf-8")).hexdigest())


def glyph_reader(args):
    """항목 3차 판별용 판독기(같은 제공사·모델·설정·키면 한 번만 만들어 재사용)."""
    if getattr(args, "no_glyph_check", False) or getattr(args, "reader", "gemini") == "mock":
        return None
    key = _reader_cache_key(args)
    if key in _READER_CACHE:
        return _READER_CACHE[key]
    try:
        from ..readers import create_reader
        from ..prompts import glyph_system_prompt
        think = None if getattr(args, "adjudicate_thinking", "low") == "full" else "low"
        r = create_reader(getattr(args, "reader", "gemini"), model=getattr(args, "model", None), use_cache=False, media_resolution="high", thinking=think, system=glyph_system_prompt())
    except Exception as e:
        log(f"자형 3차 판별은 건너뜁니다({str(e)[:80]}).")
        r = None
    _READER_CACHE[key] = r
    return r


GLYPH_WORDS = ("자형 편향", "혼동", "헷갈리기")   # 3차 판별(글자 조각)로 가릴 표시 항목의 note 표지(collate 의 자형 편향 쌍·干/于 혼동·CONFUSABLES 쌍)


def glyph_candidate(it: Item) -> bool:
    """3차 판별(글자 조각)로 가릴 한 글자 표시 항목인지."""
    return it.kind == "B1" and it.action == "mark" and len(it.hwpx) == 1 and len(it.ocr) == 1 and any(w in it.note for w in GLYPH_WORDS)


def adjudicate_items(items: list[Item], *, work, doc, reader, horizontal: bool, dpi: int = 300, policy: str = "all",
                     min_px: float | None = None, blind: bool = False) -> dict:
    """자형 편향·혼동으로 표시만 한 한 글자 항목(入/人·曰/日·于/干 …)을 글자 조각으로 모델에 묻는다.
    인쇄가 HWPX 글자면 차이에서 빼고(D), 판독 글자면 교정 후보(A/B2)로 올리며, 못 가리면 표시 그대로 둔다.
    묻는 방식은 일반 OCR 의 3차 판별과 같다(눈가림: 앞뒤 글자 없이 조각 안 순번으로 가리키고 후보를 섞는다, 저해상 자리는 묻지 않는다,
    blind 면 후보 없이 한 번 더 읽어 같을 때만 따른다)."""
    from ..schema import PageReading, GlyphAsk
    from ..adjudicate import locate_in_reading, locate_glyph, glyph_crop, column_plain, page_ink, make_ask, blind_agrees, MIN_GLYPH_PX
    min_px = MIN_GLYPH_PX if min_px is None else min_px
    from ..cli import _load_page_image, DOC_LOCK
    from ..pdfio import layer_chars, page_frame
    from . import zh
    from .collate import _s2t_from_ctx, _split_ctx
    st = {"asked": 0, "hwpx": 0, "ocr": 0, "undecided": 0, "unlocated": 0, "lowres": 0, "blind_rejected": 0}   # 멈추면 stopped(사유)·skipped(못 물은 수)도
    cache: dict[int, tuple] = {}
    for it in items:
        if not glyph_candidate(it):
            continue
        if st.get("stopped"):                               # 호출 상한·키·한도(치명)로 멈춘 뒤의 자리는 묻지 않고 표시 그대로(B1)
            it.note += " · 3차 판별: 멈춤으로 묻지 못함"; st["skipped"] = st.get("skipped", 0) + 1; continue
        pg = it.pdf_page
        if pg not in cache:
            rec = work.load_record(pg)
            if not rec:
                cache[pg] = None
            else:
                merged = PageReading.model_validate(rec["merged"]); p1 = PageReading.model_validate(rec["pass1"]) if rec.get("pass1") else merged
                img, _ = _load_page_image(work, doc, pg, dpi)
                lines, frame = [], None
                if doc is not None:
                    with DOC_LOCK:
                        try:
                            lines = layer_chars(doc, pg); frame = page_frame(doc, pg)
                        except Exception:
                            lines, frame = [], None
                cache[pg] = (merged, {r.id: r.bbox for r in p1.regions}, img, lines, frame, page_ink(img))
        if not cache[pg]:
            st["unlocated"] += 1; continue
        merged, bboxes, img, lines, frame, ink = cache[pg]
        pre, post = _split_ctx(it.oc_ctx)
        site = locate_in_reading(merged, pre, it.ocr, post)
        if site is None:
            st["unlocated"] += 1; continue
        hk, ok = zh.compare_key(it.hwpx), zh.compare_key(it.ocr)
        hc = hk if (horizontal and ok == it.ocr) else it.hwpx       # 후보는 인쇄 꼴대로: 간체 인쇄(가로쓰기)면 간체, 번체 인쇄(세로쓰기·번체로 읽은 판독)면 HWPX 글자
        cands = [hc, it.ocr] if hk != ok else [it.ocr]
        win = locate_glyph(ink, bboxes.get(site.region_id, [0, 0, 1000, 1000]), site, horizontal, column_plain(merged, site), lines, frame)
        if min_px and win.em_px and win.em_px < min_px:
            it.note += f" · 3차 판별: 저해상(원본 글자 약 {win.em_px:.0f}px)이라 묻지 않음"; st["lowres"] += 1; st["undecided"] += 1; continue
        ask = make_ask(site, win, cands, pg, tick=bool(win.measured and win.target))
        crop = glyph_crop(img, win.box, target=win.target if ask.tick else None, horizontal=horizontal)
        try:
            res = reader.adjudicate(crop, ask, horizontal, pg)
        except Exception as e:
            if getattr(e, "fatal", False):                  # 다음 자리·구획도 같은 이유로 못 부른다 — 멈추고 알린다(run_section → 오류: 줄, 전체 교정 멈춤)
                st["stopped"] = str(e)[:200]; it.note += " · 3차 판별: 멈춤으로 묻지 못함"; st["skipped"] = st.get("skipped", 0) + 1; continue
            st["asked"] += 1; it.note += f" · 3차 판별 실패({str(e)[:40]})"; st["undecided"] += 1; continue
        st["asked"] += 1
        choice = (res.choice or "").strip(); ev = (res.evidence or "")[:60]
        from ..adjudicate import window_ok
        if choice and not window_ok(getattr(res, "window", "") or "", site.pre, choice, site.post):
            it.note += f" · 3차 판별: 조각 위치가 어긋나 보류(보인 글: {(res.window or '')[:10]})"; st["undecided"] += 1; continue
        if blind and choice in cands and hasattr(reader, "identify"):
            try:
                rb = reader.identify(crop, GlyphAsk(candidates=(), pos=ask.pos, k=ask.k or max(1, len(choice)), count=ask.count, measured=ask.measured, tick=ask.tick), horizontal, pg)
                got = (rb.choice or "").strip()
            except Exception as e:
                if getattr(e, "fatal", False):
                    st["stopped"] = str(e)[:200]
                it.note += f" · 3차 판별: 눈가림 재확인 실패({str(e)[:30]})라 보류"; st["undecided"] += 1; continue
            if not blind_agrees(got, choice, cands):
                it.note += f" · 3차 판별: 눈가림 답 「{got or '?'}」 ≠ 객관식 「{choice}」라 보류"; st["undecided"] += 1; st["blind_rejected"] += 1; continue
        ck = zh.compare_key(choice) if choice else ""             # 답은 접어서 맞춘다(번체 인쇄를 보고 問 으로 답해도 HWPX 편)
        if ck and ck == hk and hk != ok:
            it.kind, it.witness = "D", (it.witness + "+" if it.witness else "") + "glyph=hwpx"
            it.note = f"3차 판별: 인쇄 자형이 HWPX 글자 「{it.hwpx}」({ev}) — 차이에서 뺌"; st["hwpx"] += 1
        elif ck and ck == ok:
            new = _s2t_from_ctx(it.oc_ctx, it.ocr); c2 = zh.candidates(it.ocr)
            it.kind = "B2" if len(c2) > 1 else "A"; it.action = "replace"; it.new = new; it.candidates = c2 if len(c2) > 1 else []
            it.auto = (policy == "all") if it.kind == "B2" else (policy in ("sure", "all"))
            it.witness = (it.witness + "+" if it.witness else "") + "glyph=ocr"
            it.note = f"3차 판별: 인쇄 자형이 판독 글자 「{it.ocr}」({ev}) — HWPX 「{it.hwpx}」 가 오탈로 보임"; st["ocr"] += 1
        else:
            it.note += f" · 3차 판별: 자형으로 가리지 못함({ev or '근거 없음'})"; st["undecided"] += 1
    items[:] = [it for it in items if it.kind != "D"]
    return st


def backup_record(work, n: int) -> Path | None:
    """쪽 기록을 다시 계산하기 전에 readings/prev/ 에 사본을 남긴다(Work.backup_record — merged 에 손으로 고친 글을 되찾을 수 있게).
    남긴 사본의 경로, 남기지 않았으면(기록 없음·사본이 이미 있음) None. Work.backup_record 가 경로를 돌려주든 참·거짓(pNNN.json 첫 사본만)을 돌려주든 받는다."""
    r = work.backup_record(n)
    if isinstance(r, (str, os.PathLike)):
        return Path(r)
    return work.readings_dir / "prev" / work.record_path(n).name if r else None


def note_remerged(work, pages: list[int], backups: list | None = None) -> None:
    """비교 표시를 다시 계산한 쪽을 알린다. 한 실행에 처음 한 번은 '주의:' 로(창의 알림창에 보이게) — merged 에 손으로 고친 글이 사라지므로.
    backups: 이번에 남긴 사본 경로(backup_record) — 어디서 되찾는지 그 경로를 적는다."""
    from .. import __version__, prompts
    ps = ", ".join(str(p) for p in pages[:8]) + (" …" if len(pages) > 8 else "")
    head = "  " if getattr(work, "_remerge_noted", False) else "주의: "
    saved = [Path(b) for b in (backups or []) if b]
    def rel(b: Path) -> str:
        try:
            return b.relative_to(work.dir).as_posix()
        except (AttributeError, ValueError):
            return b.as_posix()
    where = (", ".join(rel(b) for b in saved[:4]) + (f" 외 {len(saved) - 4}개" if len(saved) > 4 else "")) if saved else "readings/prev/"
    log(f"{head}쪽 {ps}({len(pages)}쪽)의 판독 기록이 다른 판·모드로 비교한 것이라 지금 규칙(판 {__version__}·{prompts.MODE})으로 비교 표시를 다시 계산했습니다 — "
        f"판독 기록(merged)에 손으로 고친 글은 사라지고, 계산 전 기록은 {where} 에 남김" + ("" if len(saved) >= len(pages) else "(사본이 이미 있던 쪽은 그 사본)"))
    try:
        work._remerge_noted = True
    except Exception:
        pass


def run_section(args, *, pdf_doc, work, book: HwpxBook, anchors, pdf_anchors, matches, idx: int, flat: bool = False) -> dict:
    from ..cli import resolve_mode
    resolve_mode(args, pdf_doc, work)
    a = anchors[idx]; m = matches[idx]
    sec = book.section(anchors, idx, flat=flat)
    hw_paras = book.section_texts(sec)
    # 쪽 범위
    if getattr(args, "pdf_pages", None):
        from ..pdfio import parse_pages
        ps = parse_pages(args.pdf_pages, len(pdf_doc)); pages = (min(ps), max(ps))
    elif m.pdf:
        j = pdf_anchors.index(m.pdf); pages = page_range(pdf_anchors, j, len(pdf_doc))
    else:
        guess = find_title_pages(pdf_doc, a.name, sec.next_anchor.name if sec.next_anchor else None)
        if not guess:
            raise RuntimeError(f"「{a.name}」 에 맞는 PDF 책갈피가 없고 PDF 글자층에서도 제목을 찾지 못했습니다. --pdf-pages 로 쪽 범위를 주십시오.")
        pages = guess
        log(f"PDF 책갈피에 짝이 없어 PDF 글자층에서 「{a.name}」 을 찾음 → {pages[0]}~{pages[1]}쪽")
    log(f"구획 「{a.name}」: HWPX 문단 {sec.start}~{sec.end - 1}({sec.n_paras}개) · PDF {pages[0]}~{pages[1]}쪽" + (f" · PDF 책갈피 「{m.pdf.title}」({m.how})" if m.pdf else ""))
    read_partial = ensure_pages_read(args, work, pdf_doc, pages)          # 범위 쪽 기록이 빠졌으면 PartialRead — 이 구획은 대조하지 않는다
    oc_all = []; layers: dict[int, str] = {}; remerged: list[int] = []; backups: list[Path] = []
    from .. import __version__, prompts
    from ..cli import remerge_record
    from ..pdfio import text_layer
    for n in range(pages[0], pages[1] + 1):
        rec = work.load_record(n)
        if rec:
            layer_d: dict = {}
            try:
                tlp = work.textlayer_path(n)
                layer_d = json.loads(tlp.read_text(encoding="utf-8")) if tlp.exists() else {"plain": text_layer(pdf_doc, n).plain()}
                layer = layer_d.get("plain", "")
            except Exception:
                layer = ""
            layers[n] = layer
            if rec.get("pass1") and rec.get("pass2") and not (rec.get("version") == __version__ and rec.get("mode") == prompts.MODE):
                # 다른 판·다른 모드로 비교한 기록만 저장된 1차·2차로 비교 표시를 지금 규칙(부호·간체 접기)으로 다시 계산한다(영역 좌표도 글자층에 맞춤, 모델 호출 없음).
                # 같은 판·같은 모드면 결과가 같으니 건너뛴다 — merged 에 손으로 고친 글이 실행마다 지워지지 않게. 계산 전 기록은 readings/prev/ 에 남기고 그 경로를 알린다
                try:
                    bk = backup_record(work, n)
                    rec = remerge_record(rec, layer, layer_d); rec["mode"] = prompts.MODE; work.save_record(n, rec); remerged.append(n)
                    if bk: backups.append(bk)
                except Exception as e:
                    log(f"  {n}쪽 비교 다시 계산 실패(기존 기록 사용): {e}")
            oc_all.extend(reading_paras(rec))
    if remerged:
        note_remerged(work, remerged, backups)
    oc_paras, note = trim_to_section(oc_all, a.name, sec.next_anchor.name if sec.next_anchor else (m.pdf and None))
    once = sorted({pg for pg, _, o in oc_paras if o})
    if once:
        log(f"주의: 쪽 {', '.join(map(str, once[:10]))}{' …' if len(once) > 10 else ''} 의 일부 영역은 2차 판독이 빠져 1차만으로 읽은 글이라 그 자리의 차이는 고치지 않고 표시만 합니다(B1)"
            + (" — 2차 판독을 켜고 다시 누르면 그 영역의 2차만 다시 묻습니다." if getattr(args, "no_second_pass", False) else " — 다시 누르면 그 영역의 2차만 다시 묻습니다."))
    policy = getattr(args, "policy", "all") or "all"
    log(f"기준 HWPX: {Path(book.path).name} (문단 {len(book.paragraphs):,}개)")
    items = collate(hwpx_cells(hw_paras, sec.start), ocr_cells(oc_paras), policy=policy, titles=book_titles(book.texts()))
    # 3차 판별(글자 조각)을 글자층 대조보다 먼저 — 글자층은 같은 화소를 본 다른 OCR 이라 자형 혼동을 가리지 못하고, 먼저 올려 버리면(B1→A·B2) 3차가 묻지 않게 된다
    gst = {}; reader = glyph_reader(args) if any(glyph_candidate(it) for it in items) else None
    if reader is not None:
        gst = adjudicate_items(items, work=work, doc=pdf_doc, reader=reader, horizontal=(prompts.MODE == "horizontal_zh"), dpi=getattr(args, "dpi", 300), policy=policy,
                               min_px=getattr(args, "adjudicate_min_px", None), blind=bool(getattr(args, "adjudicate_blind", False)))
        log(f"자형 3차 판별: {gst['asked']}곳 물음 → 인쇄가 HWPX 글자 {gst['hwpx']}(차이에서 뺌) · 판독 글자 {gst['ocr']}(교정 후보로) · 못 가림 {gst['undecided']}"
            + (f" · 자리 못 찾음 {gst['unlocated']}" if gst.get("unlocated") else "") + (f" · 저해상이라 묻지 않음 {gst['lowres']}" if gst.get("lowres") else "")
            + (f" · 눈가림 재확인 불일치 {gst['blind_rejected']}" if gst.get("blind_rejected") else "") + (f" · 멈춤으로 못 물음 {gst['skipped']}" if gst.get("skipped") else ""))
        if gst.get("stopped"):
            log(f"오류: 자형 3차 판별을 멈췄습니다 — {gst['stopped']} · 묻지 못한 자리는 표시만(B1) 두고 이 구획은 마저 대조합니다(모델을 더 부르지 않음).")
    else:
        # 같은 干/于 쌍이 되풀이되면 책 전체의 변환 오류로 보고 교정 후보로 — 글자층과 같이 3차 판별을 못 할 때만(3차가 가릴 자리를 반복 수로 먼저 올리지 않게)
        n_rep = promote_repeated(items, policy=policy)
        if n_rep:
            log(f"같은 쌍이 구획에서 {REPEAT_MIN}회 이상 되풀이된 干/于 자리 {n_rep}건을 교정 후보로 올림(3차 판별을 못 해 반복 근거로)")
    # 글자층 대조: 3차가 가린 자리는 건너뛰고, 글자층만으로 교정 후보로 올리는 것은 3차 판별을 못 할 때(키 없음·--no-glyph-check)만
    wst = witness_filter(items, layers, policy, promote=reader is None)
    if wst["checked"] or wst.get("weak") or wst.get("glyph"):
        log(f"PDF 글자층 대조: 한 글자 차이 {wst['checked']}건 중 글자층이 HWPX 편 {wst['hwpx']}건(차이에서 뺌) · 판독 편 {wst['ocr']}건 · 못 찾음 {wst['none']}건"
            + (f" · 자형 편향 쌍이라 증인으로 안 씀 {wst['weak']}건" if wst.get("weak") else "")
            + (f" · 3차 판별이 가린 자리라 건너뜀 {wst['glyph']}건" if wst.get("glyph") else "")
            + (" · 글자층만으로는 교정 후보로 올리지 않음(3차 판별이 가림)" if reader is not None else ""))
    sm = summary(items)
    log(f"대조: 자동 교정 {sm['auto']}건 · 표시만 {sm['mark']}건 (A {sm['A']} · B1 {sm['B1']} · B2 {sm['B2']} · C {sm['C']})" + (f" · {note}" if note else ""))
    out_dir = work.dir / "collate"; out_dir.mkdir(exist_ok=True)
    stem = re.sub(r"[\\/:*?\"<>|\s]+", "_", a.name)[:40] or f"sec{idx}"
    applied = None; saved = None
    if getattr(args, "apply", False):
        if not items:
            log("차이가 없어 고칠 것이 없습니다.")
        else:
            mode = memo_mode(args)
            ed = Editor(book, memo=mode, heads={x.para for x in anchors})   # 이번 실행의 구획 기준(구획 시작 포함, PDF 책갈피 제목·고른 스타일)
            applied = ed.apply(items)
            if mode != "inline":
                ed.add_legend_memo()                                   # 범례 메모 하나(이미 있으면 그대로)
            out = getattr(args, "out", None) or corrected_path(book.path)
            try:
                Path(out).parent.mkdir(parents=True, exist_ok=True)
                saved = ed.save_copy(out)
            except OSError as e:                                       # 한컴에 열린 교정본 등 — 고친 것이 저장되지 않았다
                raise SaveFailed(f"교정본 {Path(out).name} 을(를) 저장하지 못했습니다({str(e)[:80]}) — 한컴 등에서 그 파일을 열어 두었으면 닫고 다시 누르십시오") from e
            ok = sum(1 for x in applied if x.ok); bad = [x for x in applied if not x.ok]
            from ..grades import collate_grade
            gc = {"green": 0, "blue": 0, "red": 0}
            for x in applied:
                g = collate_grade(x.item) if x.ok else None
                if g: gc[g] += 1
            n_memo = sum(1 for x in applied if x.ok and x.memo)   # 실제로 단 메모·글 속 표기만('이미 표시된 자리'·제목 문단·표기 자리 없음은 세지 않음)
            log(f"반영 {ok}건" + (f", 실패 {len(bad)}건" if bad else "") + f" → 저장: {saved}")
            log(f"표시: 초록 {gc['green']} · 파랑 {gc['blue']} · 빨강 {gc['red']} · 메모 {n_memo}건({ {'notice': '고지 사항', 'all': '모든 표시', 'inline': '글 속 〔〕'}[mode] })"
                + (" — 원래 글자·근거는 보고 화면(html·json)에" if mode != "all" else ""))
            n_hidden = sum(1 for x in applied if x.ok and x.hidden)
            if n_hidden:
                log(f"제목 문단의 표시 {n_hidden}건(지운 글자·폭 없는 표시)은 문서에 보이지 않고 보고(html)에만 있습니다 — 구획 이름이 바뀌지 않게 글 속 표기를 넣지 않음")
            for x in bad[:5]:
                log(f"  실패: {x.item.kind} 문단 {x.item.para} [{x.item.start}:{x.item.end}] — {x.note}")
    rep = report_html(sec, m.pdf, pages, hw_paras, oc_paras, items, policy, applied, out_dir / f"{stem}.html", note, hwpx_file=str(book.path), mode=memo_mode(args))
    (out_dir / f"{stem}.json").write_text(json.dumps({"section": a.name, "hwpx_paras": [sec.start, sec.end], "pdf_pages": list(pages), "policy": policy,
                                                      "hwpx_file": str(book.path), "hwpx_paragraphs": len(book.paragraphs), "witness_stats": wst, "glyph_stats": gst,
                                                      "summary": sm, "items": [_item_json(i, memo_mode(args)) for i in items], "saved": saved, "at": time.strftime("%Y-%m-%d %H:%M:%S")},
                                                     ensure_ascii=False, indent=1), encoding="utf-8")
    log(f"보고: {rep}")
    return {"section": a.name, "pages": pages, "summary": sm, "report": rep, "saved": saved, "items": items, "read_partial": bool(read_partial), "remerged": remerged,
            "single_read": once, "halt": gst.get("stopped") or ""}


def to_check(r: dict | None) -> bool:
    """구획 결과에 '완료'로만 보이면 안 될 일이 있는지(판독 일부 실패·1차만 읽은 영역·손 고침 다시 계산) — cmd_collate 가 3 을 돌려준다."""
    return bool(r and (r.get("read_partial") or r.get("remerged") or r.get("single_read")))


def run_all(args, *, pdf_doc, work, book: HwpxBook, anchors, pdf_anchors, matches) -> dict:
    """구획 전체를 차례로 대조(--apply 면 교정본 하나에 쌓는다). 구획은 급과 무관하게 바로 다음 기준 직전까지(겹침·빠짐 없음).
    PDF 쪽을 못 찾는 구획은 건너뛰고, 끝에 요약(collate/_전체_요약.html·json)을 남긴다."""
    from ..cli import CANCEL, progress
    results = []; skipped = []; t0 = time.time(); stopped = False; failed = ""; partial = False
    total = {"A": 0, "B1": 0, "B2": 0, "C": 0, "auto": 0, "mark": 0}
    log(f"▶ 전체 교정: 구획 {len(anchors)}개 · 정책 {POLICIES.get(getattr(args, 'policy', 'all'), '')} · {'교정본에 반영' if getattr(args, 'apply', False) else '대조만'}")
    for i, a in enumerate(anchors):
        progress("collate", i, len(anchors))
        if CANCEL.is_set():
            stopped = True; log("중지 요청으로 멈춤."); break
        sec = book.section(anchors, i, flat=True)
        if sec.n_paras <= 0 or not any(t.strip() for t in book.section_texts(sec)[1:]):
            continue                                            # 제목뿐인 구획(장 제목 등)
        m = matches[i]
        if not m.pdf and not find_title_pages(pdf_doc, a.name, sec.next_anchor.name if sec.next_anchor else None):
            skipped.append(a.name); continue
        try:
            r = run_section(args, pdf_doc=pdf_doc, work=work, book=book, anchors=anchors, pdf_anchors=pdf_anchors, matches=matches, idx=i, flat=True)
            results.append(r); partial = partial or to_check(r)
            for k in total:
                total[k] += r["summary"].get(k, 0)
            log(f"  [{i + 1}/{len(anchors)}] {a.name[:24]}: 자동 {r['summary']['auto']} · 표시 {r['summary']['mark']} · 누적 자동 {total['auto']} 표시 {total['mark']} · {time.time() - t0:.0f}초")
            if r.get("halt"):                                   # 3차 판별이 호출 상한·키·한도로 멈춤(위 오류: 줄) — 다음 구획도 같으므로 멈춘다
                failed = f"자형 3차 판별 멈춤 — {r['halt']}"; break
        except Cancelled:
            stopped = True; log(f"중지 요청으로 멈춤 — 「{a.name[:24]}」 구획은 판독 도중이라 대조하지 않았습니다(끝난 쪽 판독은 남음)."); break
        except PartialRead as e:
            partial = True; skipped.append(f"{a.name} (판독 일부 실패)")
            log(f"주의: 「{a.name[:24]}」 구획은 {e}로 대조하지 않았습니다(빠진 쪽과 대조하면 거짓 '판독에 없는 긴 구간' 표시가 남음) — 다시 누르면 그 쪽만 다시 판독합니다.")
        except ReadFailed as e:
            failed = str(e); skipped.append(f"{a.name} (판독 오류)")
            log(f"오류: 「{a.name[:24]}」 구획의 쪽 판독이 오류로 끝나 전체 교정을 멈춥니다 — 위 오류(키·한도·모델 이름)를 고친 뒤 다시 누르면 이어서 합니다."); break
        except SaveFailed as e:                                 # 다음 구획도 같은 교정본에 저장하므로 멈춘다
            failed = str(e); skipped.append(f"{a.name} (저장 실패)")
            log(f"오류: 「{a.name[:24]}」 구획에서 전체 교정을 멈춥니다 — {e}"); break
        except Exception as e:                                  # 이 구획만의 오류 — 끝까지 가되 '완료' 로만 보이지 않게(3)
            partial = True; skipped.append(f"{a.name} (오류: {str(e)[:60]})")
            log(f"오류: 「{a.name[:24]}」 구획은 오류로 대조하지 못하고 건너뜀 — {type(e).__name__}: {str(e)[:100]}")
    if not stopped and not failed:
        progress("collate", len(anchors), len(anchors))
    out_dir = work.dir / "collate"; out_dir.mkdir(exist_ok=True)
    esc = html.escape
    from ..grades import collate_grade
    gsum = {"green": 0, "blue": 0, "red": 0}
    for r in results:
        for it in r.get("items") or []:
            g = collate_grade(it)
            if g: gsum[g] += 1
    rows = "".join(f"<tr><td>{esc(r['section'])}</td><td>{r['pages'][0]}~{r['pages'][1]}</td><td>{r['summary']['auto']}</td><td>{r['summary']['mark']}</td>"
                   f"<td>{r['summary']['A']}</td><td>{r['summary']['B1']}</td><td>{r['summary']['B2']}</td><td>{r['summary']['C']}</td><td><a href='{esc(Path(r['report']).name)}'>보고</a></td></tr>" for r in results)
    doc_html = (f"<!doctype html><html lang=ko><head><meta charset=utf-8><title>전체 교정 요약</title><style>body{{font-family:'Noto Serif KR',serif;background:#f4efe4;color:#231c18;margin:0}}"
                f"header{{padding:12px 20px;background:#3b1b14;color:#f1e7cf}}main{{padding:16px 20px}}table{{border-collapse:collapse;background:#fff;font-size:13px}}td,th{{border:1px solid #d9d2c3;padding:4px 8px}}</style></head><body>"
                f"<header><h1 style='font-size:18px;margin:0'>전체 교정 요약 — {esc(Path(book.path).name)}</h1><small>{'중지로 멈춤 · ' if stopped else ('오류로 멈춤 · ' if failed else '')}표시 초록 {gsum['green']} · 파랑 {gsum['blue']} · 빨강 {gsum['red']} · 구획 {len(results)}개 대조 · 건너뜀 {len(skipped)}개 · 자동 교정 {total['auto']} · 표시만 {total['mark']}"
                f" · {time.strftime('%Y-%m-%d %H:%M')}</small></header><main><table><tr><th>구획</th><th>PDF 쪽</th><th>자동</th><th>표시</th><th>A</th><th>B1</th><th>B2</th><th>C</th><th></th></tr>{rows}</table>"
                + (f"<h3>건너뛴 구획(PDF 쪽을 못 찾음·판독 일부 실패·오류)</h3><p>{esc(' · '.join(skipped))}</p>" if skipped else "") + "</main></body></html>")
    (out_dir / "_전체_요약.html").write_text(doc_html, encoding="utf-8")
    (out_dir / "_전체_요약.json").write_text(json.dumps({"sections": [{"section": r["section"], "pages": r["pages"], "summary": r["summary"], "saved": r["saved"]} for r in results],
                                                        "skipped": skipped, "total": total, "grades": gsum, "stopped": stopped, "failed": failed, "partial": partial,
                                                        "at": time.strftime("%Y-%m-%d %H:%M:%S")}, ensure_ascii=False, indent=1), encoding="utf-8")
    log(f"전체 교정 {'중지' if stopped else ('오류로 멈춤' if failed else ('끝(위 주의 확인)' if partial else '끝'))}: 구획 {len(results)}개 · 자동 교정 {total['auto']}건 · 표시만 {total['mark']}건 · 건너뜀 {len(skipped)}개 · {time.time() - t0:.0f}초"
        + (f" · 교정본 {results[-1]['saved']}" if results and results[-1].get("saved") else "") + f" · 요약 {out_dir / '_전체_요약.html'}")
    return {"results": results, "skipped": skipped, "total": total, "stopped": stopped, "failed": failed, "partial": partial}


def corrected_path(hwpx_path: str) -> str:
    """원본 → <이름>_교정.hwpx. 이미 교정본이면 그 자리(같은 이름)."""
    p = Path(hwpx_path)
    stem = p.stem if p.stem.endswith("_교정") else p.stem + "_교정"
    from ..runtime import safe_output_path
    return str(safe_output_path(p.with_name(stem + ".hwpx"), p))


def set_aside(path: Path) -> Path:
    """교정본을 같은 폴더의 <이름>_이전_<n>.hwpx(비어 있는 첫 n ≥ 1)로 옮긴다(지우지 않음). 옮긴 경로를 돌려준다."""
    path = Path(path); n = 1
    while (dst := path.with_name(f"{path.stem}_이전_{n}{path.suffix}")).exists():
        n += 1
    os.replace(path, dst)
    return dst


def cmd_collate(args):
    """명령줄 진입점. --list 면 색인만, --section 이면 그 구획을 대조(--apply 면 반영·저장)."""
    from contextlib import ExitStack
    with ExitStack() as resources:
        return _cmd_collate(args, resources)


def _cmd_collate(args, resources):
    from ..pdfio import open_pdf
    from ..state import Work
    if not getattr(args, "hwpx", None) or not Path(args.hwpx).exists():
        log("--hwpx 파일이 필요합니다."); return 2
    pdf_doc = open_pdf(args.pdf)
    resources.callback(pdf_doc.close)
    preview = bool(getattr(args, "list", False) or not (getattr(args, "all", False) or
                   getattr(args, "anchor_index", None) or getattr(args, "section", None)))
    styles = [s for s in (getattr(args, "heading_styles", "") or "").split(",") if s.strip()] or None
    src = getattr(args, "anchor_source", "auto") or "auto"
    book = HwpxBook(args.hwpx)
    resources.callback(book.doc.close)
    anchors, pdf_anchors, matches = index_both(pdf_doc, book, src, styles)
    moved = None                                                 # 교정본을 _이전_n 으로 옮겼으면 그 경로 — 끝나도 3(확인할 것 있음)으로 알린다
    # 교정본(<이름>_교정.hwpx)이 이미 있으면 거기에 이어서 고친다 — 앞서 고친 구획이 지워지지 않게.
    # 구획 번호가 원본 기준으로 매겨지므로, 교정본의 기준 목록이 원본과 같을 때만 바꿔 탄다.
    if True:   # 대조만 할 때도 교정본이 있으면 그 상태를 기준으로 비교한다(이미 고친 자리는 차이로 나오지 않게)
        target = Path(getattr(args, "out", None) or corrected_path(args.hwpx))
        if target.exists() and os.path.abspath(target) != os.path.abspath(args.hwpx):
            book2 = HwpxBook(str(target))
            resources.callback(book2.doc.close)
            a2, _, m2 = index_both(pdf_doc, book2, src, styles)
            if len(a2) == len(anchors) and all(x.para == y.para for x, y in zip(a2, anchors)):
                book.doc.close()
                book, anchors, matches = book2, a2, m2
                log(f"기존 교정본에 이어서 작업합니다(앞서 고친 구획 유지): {target.name}")
            elif getattr(args, "apply", False) and not preview:
                # 저장하면 덮어써질 교정본 — 앞서 고친 것이 사라지지 않게 옆으로 옮겨 두고 원본에서 다시 시작한다
                book2.doc.close()                           # Windows 에서 읽던 ZIP 핸들을 닫아야 파일을 옮길 수 있다
                moved = set_aside(target)
                log(f"주의: 교정본 {target.name} 의 구획 목록이 원본과 달라(한컴에서 제목·책갈피를 넣거나 뺀 듯) 원본에서 다시 시작합니다 — "
                    f"앞서 고친 교정본은 지우지 않고 {moved.name} 으로 옮겨 두었습니다. 필요한 교정은 그 파일에서 옮겨 적으십시오.")
            else:
                book2.doc.close()
                log(f"주의: 교정본 {target.name} 의 구획 목록이 원본과 달라 원본을 기준으로 대조합니다(반영하면 교정본을 _이전_n 으로 옮겨 두고 원본에서 다시 시작).")
    if not anchors:
        log("구획 기준을 찾지 못했습니다. HWPX 에 책갈피·제목 스타일이 없고 PDF 책갈피 이름도 본문에서 찾지 못했습니다. "
            "--anchor-source pdf 로 다시 하거나, 한컴에서 제목에 '제목' 스타일 또는 책갈피를 넣어 주십시오."); return 2
    if preview:
        log(list_text(anchors, pdf_anchors, matches, limit=getattr(args, "limit", 0) or 0)); return 0
    # 조회는 결과 폴더를 만들거나 옮기지 않는다. 실제 대조를 시작할 때 한 번 검증해야
    # '모든 쪽의 기록이 있음' 경로도 바뀐 PDF·DPI의 옛 판독을 재사용하지 않는다.
    work = Work(args.pdf, args.work)
    work.ensure_source(getattr(args, "dpi", 300))
    from ..cli import progress
    if getattr(args, "all", False):
        r = run_all(args, pdf_doc=pdf_doc, work=work, book=book, anchors=anchors, pdf_anchors=pdf_anchors, matches=matches) or {}
        # 중지·판독 오류로 멈추면 '완료'로 보이지 않게. 판독 일부 실패·손 고침 다시 계산·교정본 옮김은 끝났어도 3(창이 '주의:' 를 알림창에)
        return 130 if r.get("stopped") else (1 if r.get("failed") else (3 if r.get("partial") or moved else 0))
    ai = getattr(args, "anchor_index", None)
    if ai:
        if not 1 <= ai <= len(anchors):
            log(f"구획 번호 {ai} 가 범위(1~{len(anchors)}) 밖입니다."); return 2
        progress("collate", 0, 1)
        try:
            r = run_section(args, pdf_doc=pdf_doc, work=work, book=book, anchors=anchors, pdf_anchors=pdf_anchors, matches=matches, idx=ai - 1)
        except Cancelled:
            log("중지 요청으로 멈춤 — 쪽 판독 도중이라 이 구획은 대조하지 않았습니다(끝난 쪽 판독은 남음)."); return 130
        except PartialRead as e:
            log(f"주의: {e}로 이 구획은 대조하지 않았습니다(빠진 쪽과 대조하면 거짓 '판독에 없는 긴 구간' 표시가 남음) — 다시 누르면 그 쪽만 다시 판독합니다."); return 3
        except ReadFailed as e:
            log(f"오류: 쪽 판독이 끝나지 않아 이 구획을 대조하지 못했습니다 — {e}"); return 1
        except SaveFailed as e:
            log(f"오류: {e}"); return 1
        progress("collate", 1, 1)
        if (r or {}).get("halt"):
            return 1                                            # 3차 판별이 호출 상한·키·한도로 멈춤(오류: 줄은 run_section 이)
        return 3 if moved or to_check(r) else 0                 # 판독이 끝난 뒤 눌린 [중지]는 이 구획을 마저 끝내고 저장했으므로 완료
    hits = find_anchor(anchors, args.section)
    if not hits:
        log(f"「{args.section}」 이라는 책갈피·제목이 HWPX 에 없습니다. --list 로 목록을 보십시오."); return 2
    if len(hits) > 1 and not getattr(args, "nth", None):
        log(f"「{args.section}」 에 맞는 기준이 {len(hits)}개입니다. --nth 로 고르십시오:")
        for i, h in enumerate(hits, 1):
            log(f"  {i}. {h.name} (문단 {h.para}, 급 {h.level})")
        return 2
    a = hits[(getattr(args, "nth", 1) or 1) - 1]
    try:
        r = run_section(args, pdf_doc=pdf_doc, work=work, book=book, anchors=anchors, pdf_anchors=pdf_anchors, matches=matches, idx=anchors.index(a))
    except Cancelled:
        log("중지 요청으로 멈춤 — 쪽 판독 도중이라 이 구획은 대조하지 않았습니다(끝난 쪽 판독은 남음)."); return 130
    except PartialRead as e:
        log(f"주의: {e}로 이 구획은 대조하지 않았습니다(빠진 쪽과 대조하면 거짓 '판독에 없는 긴 구간' 표시가 남음) — 다시 누르면 그 쪽만 다시 판독합니다."); return 3
    except ReadFailed as e:
        log(f"오류: 쪽 판독이 끝나지 않아 이 구획을 대조하지 못했습니다 — {e}"); return 1
    except SaveFailed as e:
        log(f"오류: {e}"); return 1
    if (r or {}).get("halt"):
        return 1
    return 3 if moved or to_check(r) else 0
