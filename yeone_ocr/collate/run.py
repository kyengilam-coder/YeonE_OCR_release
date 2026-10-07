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
from .pdf_outline import pdf_bookmarks, match_anchors, page_range, PdfAnchor
from .collate import hwpx_cells, ocr_cells, collate, summary, Item
from .hwpx_edit import Editor

POLICIES = {"memo": "메모만(글자 안 바꿈)", "sure": "확실한 것만(A) 자동", "all": "전부 자동(A·B2·누락 삽입), B1·C 는 표시"}


def log(msg: str) -> None:
    from ..cli import log as _log
    _log(msg)


# ---------- 색인 ----------
def index_both(pdf_doc, book: HwpxBook):
    anchors = book.anchors()
    pdf_anchors = pdf_bookmarks(pdf_doc)
    matches = match_anchors(anchors, pdf_anchors)
    return anchors, pdf_anchors, matches


def list_text(anchors, pdf_anchors, matches, limit: int = 0) -> str:
    lines = [f"HWPX 기준 {len(anchors)}개(책갈피 {sum(1 for a in anchors if a.kind == 'bookmark')}, 제목 {sum(1 for a in anchors if a.kind == 'heading')}) · PDF 책갈피 {len(pdf_anchors)}개 · "
             f"짝: 정확 {sum(1 for m in matches if m.how == 'exact')}, 비슷 {sum(1 for m in matches if m.how == 'fuzzy')}, 없음 {sum(1 for m in matches if m.how == 'none')}"]
    for i, m in enumerate(matches):
        if limit and i >= limit:
            lines.append(f"… {len(matches) - limit}개 더"); break
        a = m.anchor
        pdf = f"PDF 「{m.pdf.title}」 {m.pdf.page}쪽" if m.pdf else "PDF 짝 없음"
        lines.append(f"{i + 1:4d}. {'  ' * (a.level - 1)}{a.name[:30]:<32} 문단 {a.para:>5}  {pdf}  ({m.how}{'' if m.how != 'fuzzy' else f' {m.score:.2f}'})")
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
    return paras[start:end], " · ".join(note)


# ---------- 보고 ----------
def report_html(section: Section, pdf_anchor: PdfAnchor | None, pages: tuple[int, int], hw_paras: list[str], oc_paras: list[tuple[int, str]],
                items: list[Item], policy: str, applied: list | None, out_path: Path, note: str = "") -> str:
    sm = summary(items)
    def esc(s): return html.escape(s or "")
    rows = []
    for it in items:
        cls = {"A": "a", "B1": "b1", "B2": "b2", "C": "c"}.get(it.kind, "")
        rows.append(f"<tr class='{cls}'><td>{it.kind}</td><td>{'자동 반영' if it.auto else '표시만'}</td><td>{it.para}</td><td>{esc(it.hwpx) or '—'}</td>"
                    f"<td>{esc(it.ocr) or '—'}</td><td>{esc(it.new) or '—'}</td><td>{esc('/'.join(it.candidates))}</td><td>{it.pdf_page}</td><td>{esc(it.note)}</td></tr>")
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
    .sum span{display:inline-block;margin-right:12px;padding:2px 8px;border-radius:10px;background:#e4d7b4}"""
    doc = (f"<!doctype html><html lang=ko><head><meta charset=utf-8><title>교정 · {esc(section.anchor.name)}</title><style>{css}</style></head><body>"
           f"<header><h1>책갈피 교정 — {esc(section.anchor.name)}</h1><small>HWPX 문단 {section.start}~{section.end - 1} · PDF {pages[0]}~{pages[1]}쪽"
           + (f" · PDF 책갈피 「{esc(pdf_anchor.title)}」" if pdf_anchor else " · PDF 책갈피 짝 없음(쪽 범위 직접 지정)") + f" · 정책: {esc(POLICIES.get(policy, policy))}</small></header><main>"
           f"<p class=sum><span>자동 교정 {sm['auto']}</span><span>표시만 {sm['mark']}</span><span>A {sm['A']}</span><span>B1 {sm['B1']}</span><span>B2 {sm['B2']}</span><span>C {sm['C']}</span></p>"
           + (f"<p><b>참고:</b> {esc(note)}</p>" if note else "") + appl +
           f"<table><tr><th>갈래</th><th>처리</th><th>문단</th><th>HWPX</th><th>판독(간체)</th><th>반영</th><th>다른 후보</th><th>PDF 쪽</th><th>설명</th></tr>{''.join(rows) or '<tr><td colspan=9>차이 없음</td></tr>'}</table>"
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


def run_section(args, *, pdf_doc, work, book: HwpxBook, anchors, pdf_anchors, matches, idx: int) -> dict:
    from .. import prompts
    prompts.set_mode(getattr(args, "mode", None) or "horizontal_zh")
    a = anchors[idx]; m = matches[idx]
    sec = book.section(anchors, idx)
    hw_paras = book.section_texts(sec)
    # 쪽 범위
    if getattr(args, "pdf_pages", None):
        from ..pdfio import parse_pages
        ps = parse_pages(args.pdf_pages, len(pdf_doc)); pages = (min(ps), max(ps))
    elif m.pdf:
        j = pdf_anchors.index(m.pdf); pages = page_range(pdf_anchors, j, len(pdf_doc))
    else:
        raise RuntimeError(f"「{a.name}」 에 맞는 PDF 책갈피가 없습니다. --pdf-pages 로 쪽 범위를 주십시오.")
    log(f"구획 「{a.name}」: HWPX 문단 {sec.start}~{sec.end - 1}({sec.n_paras}개) · PDF {pages[0]}~{pages[1]}쪽" + (f" · PDF 책갈피 「{m.pdf.title}」({m.how})" if m.pdf else ""))
    ensure_pages_read(args, work, pdf_doc, pages)
    oc_all = []
    for n in range(pages[0], pages[1] + 1):
        rec = work.load_record(n)
        if rec:
            oc_all.extend(reading_text(rec))
    oc_paras, note = trim_to_section(oc_all, a.name, sec.next_anchor.name if sec.next_anchor else (m.pdf and None))
    policy = getattr(args, "policy", "all") or "all"
    items = collate(hwpx_cells(hw_paras, sec.start), ocr_cells(oc_paras), policy=policy)
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
            out = getattr(args, "out", None) or str(Path(book.path).with_name(Path(book.path).stem.replace("_교정", "") + "_교정.hwpx"))
            if os.path.abspath(out) == os.path.abspath(book.path) and "_교정" not in Path(book.path).stem:
                out = str(Path(book.path).with_name(Path(book.path).stem + "_교정.hwpx"))
            saved = ed.save_copy(out)
            ok = sum(1 for x in applied if x.ok); bad = [x for x in applied if not x.ok]
            log(f"반영 {ok}건" + (f", 실패 {len(bad)}건" if bad else "") + f" → 저장: {saved}")
            for x in bad[:5]:
                log(f"  실패: {x.item.kind} 문단 {x.item.para} [{x.item.start}:{x.item.end}] — {x.note}")
    rep = report_html(sec, m.pdf, pages, hw_paras, oc_paras, items, policy, applied, out_dir / f"{stem}.html", note)
    (out_dir / f"{stem}.json").write_text(json.dumps({"section": a.name, "hwpx_paras": [sec.start, sec.end], "pdf_pages": list(pages), "policy": policy,
                                                      "summary": sm, "items": [asdict(i) for i in items], "saved": saved, "at": time.strftime("%Y-%m-%d %H:%M:%S")},
                                                     ensure_ascii=False, indent=1), encoding="utf-8")
    log(f"보고: {rep}")
    return {"section": a.name, "pages": pages, "summary": sm, "report": rep, "saved": saved, "items": items}


def cmd_collate(args):
    """명령줄 진입점. --list 면 색인만, --section 이면 그 구획을 대조(--apply 면 반영·저장)."""
    from ..pdfio import open_pdf
    from ..state import Work
    if not getattr(args, "hwpx", None) or not Path(args.hwpx).exists():
        log("--hwpx 파일이 필요합니다."); return 2
    pdf_doc = open_pdf(args.pdf); work = Work(args.pdf, args.work)
    book = HwpxBook(args.hwpx)
    anchors, pdf_anchors, matches = index_both(pdf_doc, book)
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
