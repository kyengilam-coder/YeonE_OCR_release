"""책갈피 교정 한 번 돌리기: 색인 → 짝짓기 → 쪽 범위 → 판독(기록 재사용) → 구획 글 추출 → 대조 → 보고 → (반영·저장).

명령줄:  yeone-ocr collate <pdf> --hwpx <hwpx> --list
         yeone-ocr collate <pdf> --hwpx <hwpx> --section "大薊" [--apply] [--policy all|sure|memo] [--out 파일]
"""
from __future__ import annotations
import html, json, os, re, time, unicodedata
from argparse import Namespace
from dataclasses import asdict
from pathlib import Path
from . import zh
from .sections import HwpxBook, Anchor, Section, find_anchor, normalize_name
from .pdf_outline import pdf_bookmarks, match_anchors, page_range, PdfAnchor, find_title_pages
from .collate import hwpx_cells, ocr_cells, collate, summary, Item, witness_filter, book_titles
from .hwpx_edit import Editor

POLICIES = {"memo": "메모만(글자 안 바꿈)", "sure": "확실한 것만(A) 자동", "all": "전부 자동(A·B2·누락 삽입), B1·C 는 표시"}


def log(msg: str) -> None:
    from ..cli import log as _log
    _log(msg)


# ---------- 색인 ----------
ANCHOR_SOURCES = {"auto": "자동(HWPX 책갈피·제목 → 없으면 PDF 책갈피로 본문 검색)", "hwpx": "HWPX 책갈피·제목 스타일", "pdf": "PDF 책갈피 이름으로 HWPX 본문 검색"}


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
def reading_text(rec: dict) -> list[tuple[int, str]]:
    """쪽 기록 → [(쪽, 문단 글)] 읽기 순서대로. 제목·본문·소주·단순표 글을 모두 넣는다(〔〕 표시 포함)."""
    from ..schema import PageReading, TEXT_KINDS, TITLE_KINDS, SKIP_KINDS, paragraph_text
    pr = PageReading.model_validate(rec["merged"])
    out = []
    for reg in pr.regions:
        if reg.kind in SKIP_KINDS:
            continue
        if reg.kind in TITLE_KINDS and reg.title_text.strip():
            out.append((pr.pdf_page, reg.title_text.strip()))
        elif reg.kind in TEXT_KINDS or reg.kind == "기타":
            for p in reg.paragraphs:
                t = paragraph_text(p).strip()
                if t:
                    out.append((pr.pdf_page, t))
        elif reg.kind == "단순표":
            for r in reg.table_rows:
                t = " ".join(c for c in r.cells if c).strip()
                if t:
                    out.append((pr.pdf_page, t))
    return out


def _key(s: str) -> str:
    return zh.compare_key(normalize_name(s))


def trim_to_section(paras: list[tuple[int, str]], title: str, next_title: str | None) -> tuple[list[tuple[int, str]], str]:
    """쪽들의 글에서 구획만 남긴다: 제목 글이 처음 나오는 문단부터, 다음 제목 글이 나오는 문단 직전까지. 못 찾으면 전체(메모 남김)."""
    k, kn = _key(title), _key(next_title) if next_title else ""
    start = 0; note = []
    for i, (_, t) in enumerate(paras):
        if k and _key(t).startswith(k[: max(2, len(k))]):
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
        for i, (pg, t) in enumerate(cut):
            tk = _key(t)
            if i > 0 and tk and (tk == k or (len(tk) >= 2 and len(tk) <= 8 and tk in k)):
                continue
            out.append((pg, t))
        cut = out
    return cut, " · ".join(note)


# ---------- 보고 ----------
def report_html(section: Section, pdf_anchor: PdfAnchor | None, pages: tuple[int, int], hw_paras: list[str], oc_paras: list[tuple[int, str]],
                items: list[Item], policy: str, applied: list | None, out_path: Path, note: str = "", hwpx_file: str = "") -> str:
    sm = summary(items)
    def esc(s): return html.escape(s or "")
    rows = []
    for it in items:
        cls = {"A": "a", "B1": "b1", "B2": "b2", "C": "c"}.get(it.kind, "")
        rows.append(f"<tr class='{cls}'><td>{it.kind}</td><td>{'자동 반영' if it.auto else '표시만'}</td><td>{it.para}</td>"
                    f"<td class=ctx>{esc(it.hw_ctx) or esc(it.hwpx) or '—'}</td><td class=ctx>{esc(it.oc_ctx) or esc(it.ocr) or '—'}</td>"
                    f"<td><b>{esc(it.new) or '—'}</b></td><td>{esc('/'.join(it.candidates))}</td><td>{it.pdf_page}</td><td>{esc(it.note)}</td></tr>")
    appl = ""
    if applied is not None:
        bad = [a for a in applied if not a.ok]
        appl = f"<p>반영 {sum(1 for a in applied if a.ok)}건" + (f", 실패 {len(bad)}건: " + "; ".join(esc(a.note) for a in bad[:5]) if bad else "") + "</p>"
    hw = "".join(f"<p>{esc(t)}</p>" for t in hw_paras)
    oc = "".join(f"<p><small>{pg}쪽</small> {esc(t)}</p>" for pg, t in oc_paras)
    css = """body{font-family:'Noto Serif KR',serif;margin:0;background:#f4efe4;color:#231c18}header{padding:12px 20px;background:#3b1b14;color:#f1e7cf}
    h1{font-size:18px;margin:0}main{padding:16px 20px}table{border-collapse:collapse;width:100%;font-size:13px;background:#fff}td,th{border:1px solid #d9d2c3;padding:4px 6px;vertical-align:top}
    tr.a td:first-child{background:#d8f5c8}tr.b2 td:first-child{background:#e6f0ff}tr.b1 td:first-child,tr.c td:first-child{background:#ffe9a8}
    .cols{display:grid;grid-template-columns:1fr 1fr;gap:16px}.cols>div{background:#fff;padding:10px;border:1px solid #d9d2c3;max-height:520px;overflow:auto;font-size:14px;line-height:1.7}
    .sum span{display:inline-block;margin-right:12px;padding:2px 8px;border-radius:10px;background:#e4d7b4}
    td.ctx{font-size:15px;letter-spacing:.02em}td.ctx b{color:#b3261e}"""
    doc = (f"<!doctype html><html lang=ko><head><meta charset=utf-8><title>교정 · {esc(section.anchor.name)}</title><style>{css}</style></head><body>"
           f"<header><h1>책갈피 교정 — {esc(section.anchor.name)}</h1><small>HWPX 문단 {section.start}~{section.end - 1} · PDF {pages[0]}~{pages[1]}쪽"
           + (f" · PDF 책갈피 「{esc(pdf_anchor.title)}」" if pdf_anchor else " · PDF 책갈피 짝 없음(쪽 범위 직접 지정)") + f" · 정책: {esc(POLICIES.get(policy, policy))}"
           + (f"<br>기준 HWPX: {esc(hwpx_file)}" if hwpx_file else "") + "</small></header><main>"
           f"<p class=sum><span>자동 교정 {sm['auto']}</span><span>표시만 {sm['mark']}</span><span>A {sm['A']}</span><span>B1 {sm['B1']}</span><span>B2 {sm['B2']}</span><span>C {sm['C']}</span></p>"
           + (f"<p><b>참고:</b> {esc(note)}</p>" if note else "") + appl +
           f"<table><tr><th>갈래</th><th>처리</th><th>문단</th><th>HWPX (「」 안이 해당 자리)</th><th>판독(간체)</th><th>반영</th><th>다른 후보</th><th>PDF 쪽</th><th>설명</th></tr>{''.join(rows) or '<tr><td colspan=9>차이 없음</td></tr>'}</table>"
           f"<h3>원문 대조</h3><div class=cols><div><b>HWPX 구획(교정 전)</b>{hw}</div><div><b>PDF 판독</b>{oc}</div></div></main></body></html>")
    out_path.write_text(doc, encoding="utf-8")
    return str(out_path)


# ---------- 한 구획 처리 ----------
def ensure_pages_read(args, work, doc, pages: tuple[int, int]) -> None:
    """범위 쪽 중 기록이 없는 쪽만 준비·판독한다(기존 ②·③ 그대로)."""
    from ..cli import cmd_prepare, cmd_read
    from ..pdfio import parse_pages
    need = [n for n in range(pages[0], pages[1] + 1) if work.load_record(n) is None or getattr(args, "force", False)]
    if not need:
        log(f"쪽 {pages[0]}~{pages[1]} 판독 기록이 모두 있어 모델을 부르지 않습니다."); return
    sub = Namespace(**vars(args)); sub.pages = ",".join(str(n) for n in need)
    log(f"쪽 {sub.pages} 준비·판독 …")
    cmd_prepare(sub, work, doc)
    rc = cmd_read(sub, work, doc)
    if rc not in (0, None):
        raise RuntimeError(f"판독이 끝나지 않음(rc={rc})")


_READER_CACHE: dict = {}


def glyph_reader(args):
    """항목 3차 판별용 Gemini 판독기(한 번만 만들어 재사용). 키가 없거나 만들지 못하면 None."""
    if getattr(args, "no_glyph_check", False) or getattr(args, "reader", "gemini") != "gemini":
        return None
    key = (getattr(args, "model", None) or "", bool(getattr(args, "no_cache", False)))
    if key in _READER_CACHE:
        return _READER_CACHE[key]
    try:
        from ..reader_gemini import GeminiReader
        r = GeminiReader(model=getattr(args, "model", None), use_cache=not getattr(args, "no_cache", False), media_resolution="high")
    except Exception as e:
        log(f"자형 3차 판별은 건너뜁니다({str(e)[:80]}).")
        r = None
    _READER_CACHE[key] = r
    return r


def adjudicate_items(items: list[Item], *, work, doc, reader, horizontal: bool, dpi: int = 300, policy: str = "all") -> dict:
    """자형 편향·혼동으로 표시만 한 한 글자 항목(入/人·曰/日·于/干 …)을 글자 조각으로 모델에 묻는다.
    인쇄가 HWPX 글자면 차이에서 빼고(D), 판독 글자면 교정 후보(A/B2)로 올리며, 못 가리면 표시 그대로 둔다."""
    from ..schema import PageReading
    from ..adjudicate import locate_in_reading, glyph_box, glyph_crop
    from ..cli import _load_page_image
    from . import zh
    from .collate import _s2t_from_ctx, _split_ctx
    st = {"asked": 0, "hwpx": 0, "ocr": 0, "undecided": 0, "unlocated": 0}
    cache: dict[int, tuple] = {}
    for it in items:
        if not (it.kind == "B1" and it.action == "mark" and len(it.hwpx) == 1 and len(it.ocr) == 1):
            continue
        if not ("자형 편향" in it.note or "혼동" in it.note or "헷갈리기" in it.note):
            continue
        pg = it.pdf_page
        if pg not in cache:
            rec = work.load_record(pg)
            if not rec:
                cache[pg] = None
            else:
                merged = PageReading.model_validate(rec["merged"]); p1 = PageReading.model_validate(rec["pass1"]) if rec.get("pass1") else merged
                img, _ = _load_page_image(work, doc, pg, dpi)
                cache[pg] = (merged, {r.id: r.bbox for r in p1.regions}, img)
        if not cache[pg]:
            st["unlocated"] += 1; continue
        merged, bboxes, img = cache[pg]
        pre, post = _split_ctx(it.oc_ctx)
        site = locate_in_reading(merged, pre, it.ocr, post)
        if site is None:
            st["unlocated"] += 1; continue
        hk = zh.compare_key(it.hwpx)
        cands = [hk, it.ocr] if hk != it.ocr else [it.ocr]
        crop = glyph_crop(img, glyph_box(bboxes.get(site.region_id, [0, 0, 1000, 1000]), site, horizontal))
        st["asked"] += 1
        try:
            res = reader.adjudicate(crop, cands, site.pre, site.post, horizontal, pg)
        except Exception as e:
            it.note += f" · 3차 판별 실패({str(e)[:40]})"; st["undecided"] += 1; continue
        choice = (res.choice or "").strip(); ev = (res.evidence or "")[:60]
        if choice == hk and hk != it.ocr:
            it.kind, it.witness = "D", (it.witness + "+" if it.witness else "") + "glyph=hwpx"
            it.note = f"3차 판별: 인쇄 자형이 HWPX 글자 「{it.hwpx}」({ev}) — 차이에서 뺌"; st["hwpx"] += 1
        elif choice == it.ocr:
            new = _s2t_from_ctx(it.oc_ctx, it.ocr); c2 = zh.candidates(it.ocr)
            it.kind = "B2" if len(c2) > 1 else "A"; it.action = "replace"; it.new = new; it.candidates = c2 if len(c2) > 1 else []
            it.auto = (policy == "all") if it.kind == "B2" else (policy in ("sure", "all"))
            it.witness = (it.witness + "+" if it.witness else "") + "glyph=ocr"
            it.note = f"3차 판별: 인쇄 자형이 판독 글자 「{it.ocr}」({ev}) — HWPX 「{it.hwpx}」 가 오탈로 보임"; st["ocr"] += 1
        else:
            it.note += f" · 3차 판별: 자형으로 가리지 못함({ev or '근거 없음'})"; st["undecided"] += 1
    items[:] = [it for it in items if it.kind != "D"]
    return st


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
    ensure_pages_read(args, work, pdf_doc, pages)
    oc_all = []; layers: dict[int, str] = {}
    from ..cli import remerge_record
    from ..pdfio import text_layer
    for n in range(pages[0], pages[1] + 1):
        rec = work.load_record(n)
        if rec:
            try:
                tlp = work.textlayer_path(n)
                layer = json.loads(tlp.read_text(encoding="utf-8")).get("plain", "") if tlp.exists() else text_layer(pdf_doc, n).plain()
            except Exception:
                layer = ""
            layers[n] = layer
            if rec.get("pass1") and rec.get("pass2"):
                # 저장된 1차·2차로 비교 표시를 현재 모드 규칙(부호·간체 접기)으로 다시 계산한다. 모델 호출 없음
                try:
                    rec = remerge_record(rec, layer); work.save_record(n, rec)
                except Exception as e:
                    log(f"  {n}쪽 비교 다시 계산 실패(기존 기록 사용): {e}")
            oc_all.extend(reading_text(rec))
    oc_paras, note = trim_to_section(oc_all, a.name, sec.next_anchor.name if sec.next_anchor else (m.pdf and None))
    policy = getattr(args, "policy", "all") or "all"
    log(f"기준 HWPX: {Path(book.path).name} (문단 {len(book.paragraphs):,}개)")
    items = collate(hwpx_cells(hw_paras, sec.start), ocr_cells(oc_paras), policy=policy, titles=book_titles(book.texts()))
    wst = witness_filter(items, layers, policy)
    if wst["checked"] or wst.get("weak"):
        log(f"PDF 글자층 대조: 한 글자 차이 {wst['checked']}건 중 글자층이 HWPX 편 {wst['hwpx']}건(차이에서 뺌) · 판독 편 {wst['ocr']}건 · 못 찾음 {wst['none']}건"
            + (f" · 자형 편향 쌍이라 증인으로 안 씀 {wst['weak']}건" if wst.get("weak") else ""))
    gst = {}
    if any(it.kind == "B1" and len(it.hwpx) == 1 and len(it.ocr) == 1 and ("자형 편향" in it.note or "혼동" in it.note or "헷갈리기" in it.note) for it in items):
        reader = glyph_reader(args)
        if reader is not None:
            from .. import prompts
            gst = adjudicate_items(items, work=work, doc=pdf_doc, reader=reader, horizontal=(prompts.MODE == "horizontal_zh"), dpi=getattr(args, "dpi", 300), policy=policy)
            log(f"자형 3차 판별: {gst['asked']}곳 물음 → 인쇄가 HWPX 글자 {gst['hwpx']}(차이에서 뺌) · 판독 글자 {gst['ocr']}(교정 후보로) · 못 가림 {gst['undecided']}" + (f" · 자리 못 찾음 {gst['unlocated']}" if gst.get("unlocated") else ""))
    sm = summary(items)
    log(f"대조: 자동 교정 {sm['auto']}건 · 표시만 {sm['mark']}건 (A {sm['A']} · B1 {sm['B1']} · B2 {sm['B2']} · C {sm['C']})" + (f" · {note}" if note else ""))
    out_dir = work.dir / "collate"; out_dir.mkdir(exist_ok=True)
    stem = re.sub(r"[\\/:*?\"<>|\s]+", "_", a.name)[:40] or f"sec{idx}"
    applied = None; saved = None
    if getattr(args, "apply", False):
        if not items:
            log("차이가 없어 고칠 것이 없습니다.")
        else:
            ed = Editor(book, memo=not getattr(args, "no_memo", False))
            applied = ed.apply(items)
            out = getattr(args, "out", None) or corrected_path(book.path)
            saved = ed.save_copy(out)
            ok = sum(1 for x in applied if x.ok); bad = [x for x in applied if not x.ok]
            log(f"반영 {ok}건" + (f", 실패 {len(bad)}건" if bad else "") + f" → 저장: {saved}")
            for x in bad[:5]:
                log(f"  실패: {x.item.kind} 문단 {x.item.para} [{x.item.start}:{x.item.end}] — {x.note}")
    rep = report_html(sec, m.pdf, pages, hw_paras, oc_paras, items, policy, applied, out_dir / f"{stem}.html", note, hwpx_file=str(book.path))
    (out_dir / f"{stem}.json").write_text(json.dumps({"section": a.name, "hwpx_paras": [sec.start, sec.end], "pdf_pages": list(pages), "policy": policy,
                                                      "hwpx_file": str(book.path), "hwpx_paragraphs": len(book.paragraphs), "witness_stats": wst, "glyph_stats": gst,
                                                      "summary": sm, "items": [asdict(i) for i in items], "saved": saved, "at": time.strftime("%Y-%m-%d %H:%M:%S")},
                                                     ensure_ascii=False, indent=1), encoding="utf-8")
    log(f"보고: {rep}")
    return {"section": a.name, "pages": pages, "summary": sm, "report": rep, "saved": saved, "items": items}


def run_all(args, *, pdf_doc, work, book: HwpxBook, anchors, pdf_anchors, matches) -> dict:
    """구획 전체를 차례로 대조(--apply 면 교정본 하나에 쌓는다). 구획은 급과 무관하게 바로 다음 기준 직전까지(겹침·빠짐 없음).
    PDF 쪽을 못 찾는 구획은 건너뛰고, 끝에 요약(collate/_전체_요약.html·json)을 남긴다."""
    from ..cli import CANCEL
    results = []; skipped = []; t0 = time.time()
    total = {"A": 0, "B1": 0, "B2": 0, "C": 0, "auto": 0, "mark": 0}
    log(f"▶ 전체 교정: 구획 {len(anchors)}개 · 정책 {POLICIES.get(getattr(args, 'policy', 'all'), '')} · {'교정본에 반영' if getattr(args, 'apply', False) else '대조만'}")
    for i, a in enumerate(anchors):
        if CANCEL.is_set():
            log("중지 요청으로 멈춤."); break
        sec = book.section(anchors, i, flat=True)
        if sec.n_paras <= 0 or not any(t.strip() for t in book.section_texts(sec)[1:]):
            continue                                            # 제목뿐인 구획(장 제목 등)
        m = matches[i]
        if not m.pdf and not find_title_pages(pdf_doc, a.name, sec.next_anchor.name if sec.next_anchor else None):
            skipped.append(a.name); continue
        try:
            r = run_section(args, pdf_doc=pdf_doc, work=work, book=book, anchors=anchors, pdf_anchors=pdf_anchors, matches=matches, idx=i, flat=True)
            results.append(r)
            for k in total:
                total[k] += r["summary"].get(k, 0)
            log(f"  [{i + 1}/{len(anchors)}] {a.name[:24]}: 자동 {r['summary']['auto']} · 표시 {r['summary']['mark']} · 누적 자동 {total['auto']} 표시 {total['mark']} · {time.time() - t0:.0f}초")
        except Exception as e:
            skipped.append(f"{a.name} (오류: {str(e)[:60]})"); log(f"  {a.name}: 건너뜀 — {str(e)[:100]}")
    out_dir = work.dir / "collate"; out_dir.mkdir(exist_ok=True)
    esc = html.escape
    rows = "".join(f"<tr><td>{esc(r['section'])}</td><td>{r['pages'][0]}~{r['pages'][1]}</td><td>{r['summary']['auto']}</td><td>{r['summary']['mark']}</td>"
                   f"<td>{r['summary']['A']}</td><td>{r['summary']['B1']}</td><td>{r['summary']['B2']}</td><td>{r['summary']['C']}</td><td><a href='{esc(Path(r['report']).name)}'>보고</a></td></tr>" for r in results)
    doc_html = (f"<!doctype html><html lang=ko><head><meta charset=utf-8><title>전체 교정 요약</title><style>body{{font-family:'Noto Serif KR',serif;background:#f4efe4;color:#231c18;margin:0}}"
                f"header{{padding:12px 20px;background:#3b1b14;color:#f1e7cf}}main{{padding:16px 20px}}table{{border-collapse:collapse;background:#fff;font-size:13px}}td,th{{border:1px solid #d9d2c3;padding:4px 8px}}</style></head><body>"
                f"<header><h1 style='font-size:18px;margin:0'>전체 교정 요약 — {esc(Path(book.path).name)}</h1><small>구획 {len(results)}개 대조 · 건너뜀 {len(skipped)}개 · 자동 교정 {total['auto']} · 표시만 {total['mark']}"
                f" · {time.strftime('%Y-%m-%d %H:%M')}</small></header><main><table><tr><th>구획</th><th>PDF 쪽</th><th>자동</th><th>표시</th><th>A</th><th>B1</th><th>B2</th><th>C</th><th></th></tr>{rows}</table>"
                + (f"<h3>건너뛴 구획(PDF 쪽을 못 찾음·오류)</h3><p>{esc(' · '.join(skipped))}</p>" if skipped else "") + "</main></body></html>")
    (out_dir / "_전체_요약.html").write_text(doc_html, encoding="utf-8")
    (out_dir / "_전체_요약.json").write_text(json.dumps({"sections": [{"section": r["section"], "pages": r["pages"], "summary": r["summary"], "saved": r["saved"]} for r in results],
                                                        "skipped": skipped, "total": total, "at": time.strftime("%Y-%m-%d %H:%M:%S")}, ensure_ascii=False, indent=1), encoding="utf-8")
    log(f"전체 교정 끝: 구획 {len(results)}개 · 자동 교정 {total['auto']}건 · 표시만 {total['mark']}건 · 건너뜀 {len(skipped)}개 · {time.time() - t0:.0f}초"
        + (f" · 교정본 {results[-1]['saved']}" if results and results[-1].get("saved") else "") + f" · 요약 {out_dir / '_전체_요약.html'}")
    return {"results": results, "skipped": skipped, "total": total}


def corrected_path(hwpx_path: str) -> str:
    """원본 → <이름>_교정.hwpx. 이미 교정본이면 그 자리(같은 이름)."""
    p = Path(hwpx_path)
    stem = p.stem if p.stem.endswith("_교정") else p.stem + "_교정"
    return str(p.with_name(stem + ".hwpx"))


def cmd_collate(args):
    """명령줄 진입점. --list 면 색인만, --section 이면 그 구획을 대조(--apply 면 반영·저장)."""
    from ..pdfio import open_pdf
    from ..state import Work
    if not getattr(args, "hwpx", None) or not Path(args.hwpx).exists():
        log("--hwpx 파일이 필요합니다."); return 2
    pdf_doc = open_pdf(args.pdf); work = Work(args.pdf, args.work)
    styles = [s for s in (getattr(args, "heading_styles", "") or "").split(",") if s.strip()] or None
    src = getattr(args, "anchor_source", "auto") or "auto"
    book = HwpxBook(args.hwpx)
    anchors, pdf_anchors, matches = index_both(pdf_doc, book, src, styles)
    # 교정본(<이름>_교정.hwpx)이 이미 있으면 거기에 이어서 고친다 — 앞서 고친 구획이 지워지지 않게.
    # 구획 번호가 원본 기준으로 매겨지므로, 교정본의 기준 목록이 원본과 같을 때만 바꿔 탄다.
    if True:   # 대조만 할 때도 교정본이 있으면 그 상태를 기준으로 비교한다(이미 고친 자리는 차이로 나오지 않게)
        target = Path(getattr(args, "out", None) or corrected_path(args.hwpx))
        if target.exists() and os.path.abspath(target) != os.path.abspath(args.hwpx):
            book2 = HwpxBook(str(target))
            a2, _, m2 = index_both(pdf_doc, book2, src, styles)
            if len(a2) == len(anchors) and all(x.para == y.para for x, y in zip(a2, anchors)):
                book, anchors, matches = book2, a2, m2
                log(f"기존 교정본에 이어서 작업합니다(앞서 고친 구획 유지): {target.name}")
            else:
                log(f"⚠ 교정본 {target.name} 의 구획 목록이 원본과 달라 원본에서 다시 시작합니다. 저장하면 교정본이 덮어써집니다.")
    if not anchors:
        log("구획 기준을 찾지 못했습니다. HWPX 에 책갈피·제목 스타일이 없고 PDF 책갈피 이름도 본문에서 찾지 못했습니다. "
            "--anchor-source pdf 로 다시 하거나, 한컴에서 제목에 '제목' 스타일 또는 책갈피를 넣어 주십시오."); return 2
    if getattr(args, "all", False):
        run_all(args, pdf_doc=pdf_doc, work=work, book=book, anchors=anchors, pdf_anchors=pdf_anchors, matches=matches)
        return 0
    ai = getattr(args, "anchor_index", None)
    if ai:
        if not 1 <= ai <= len(anchors):
            log(f"구획 번호 {ai} 가 범위(1~{len(anchors)}) 밖입니다."); return 2
        run_section(args, pdf_doc=pdf_doc, work=work, book=book, anchors=anchors, pdf_anchors=pdf_anchors, matches=matches, idx=ai - 1)
        return 0
    if getattr(args, "list", False) or not getattr(args, "section", None):
        log(list_text(anchors, pdf_anchors, matches, limit=getattr(args, "limit", 0) or 0)); return 0
    hits = find_anchor(anchors, args.section)
    if not hits:
        log(f"「{args.section}」 이라는 책갈피·제목이 HWPX 에 없습니다. --list 로 목록을 보십시오."); return 2
    if len(hits) > 1 and not getattr(args, "nth", None):
        log(f"「{args.section}」 에 맞는 기준이 {len(hits)}개입니다. --nth 로 고르십시오:")
        for i, h in enumerate(hits, 1):
            log(f"  {i}. {h.name} (문단 {h.para}, 급 {h.level})")
        return 2
    a = hits[(getattr(args, "nth", 1) or 1) - 1]
    run_section(args, pdf_doc=pdf_doc, work=work, book=book, anchors=anchors, pdf_anchors=pdf_anchors, matches=matches, idx=anchors.index(a))
    return 0
