"""명령줄. python -m yeone_ocr <명령> <PDF> [옵션]

  inspect  PDF 구조·텍스트 레이어 상태 보고
  prepare  쪽 이미지(내장 스캔 우선)와 기존 텍스트 레이어를 작업 폴더에 꺼냄
  read     모델(기본 gemini)로 영역 분류·판독 → 확대 재판독 비교 → 텍스트 레이어 대조 → 구조 검사
  build    도판 잘라내기(외곽 검사) → HWPX(+DOCX) 조립 → 파일 구조 검사
  remerge  저장된 1차·2차 판독으로 비교·표시만 다시 계산 (모델 호출 없음)
  review   검수용 review.html
  report   작업 상태 보고 report.md
  mark     사람이 확인한 상태 기록 (예: --status "원본 대조 완료(사람)")
  run      prepare → read → build → review → report
"""
from __future__ import annotations
import argparse, json, sys, time, threading
from pathlib import Path
from PIL import Image

from . import __version__
from .state import Work, HUMAN_ONLY, STATUSES
from .pdfio import open_pdf, parse_pages, page_image, text_layer
from .schema import PageReading, RegionReread, Uncertain, TEXT_KINDS, IMAGE_KINDS, bbox_frac
from .layout import upscale, crop_frac, fit_figure_bbox, expand_until_clean, detect_figures, overlap_ratio, uncovered_ink_boxes, attach_uncovered
from .merge import merge_passes, compare_textlayer
from .verify import verify_page, check_hwpx_file
from .blocks import assemble_blocks, Block
from .report import make_report


CANCEL = threading.Event()   # 창 프로그램의 [중지] 단추가 켠다. 쪽 사이에서 확인한다.


def log(*a):
    print(*a, flush=True)


# ---------- inspect ----------
def cmd_inspect(args):
    doc = open_pdf(args.pdf)
    log(f"파일 {Path(args.pdf).name} · 쪽수 {len(doc)} · 메타 { {k: v for k, v in doc.metadata.items() if v} }")
    log("쪽 | 크기(pt) | 쪽 이미지 | 텍스트 레이어 글자수 | 쪽번호 표기(레이어)")
    for n in range(1, len(doc) + 1):
        img, src = page_image(doc, n, dpi=args.dpi)
        tl = text_layer(doc, n)
        p = doc[n - 1]
        log(f"{n:>3} | {p.rect.width:.0f}x{p.rect.height:.0f} | {src} | {tl.char_count:>5} | {tl.page_label}")
    return 0


# ---------- prepare ----------
def cmd_prepare(args, work: Work | None = None, doc=None):
    doc = doc or open_pdf(args.pdf)
    work = work or Work(args.pdf, args.work)
    pages = parse_pages(args.pages, len(doc))
    for n in pages:
        ip = work.page_image_path(n)
        if not ip.exists() or args.force:
            img, src = page_image(doc, n, dpi=args.dpi)
            img.save(ip)
            (work.pages_dir / f"p{n:03d}.src.txt").write_text(src, encoding="utf-8")
        tp = work.textlayer_path(n)
        if not tp.exists() or args.force:
            tl = text_layer(doc, n)
            tp.write_text(json.dumps({"char_count": tl.char_count, "page_label": tl.page_label, "plain": tl.plain(),
                                      "vertical_columns": tl.vertical_columns, "horizontal_lines": tl.horizontal_lines,
                                      "page_width": tl.page_width, "page_height": tl.page_height}, ensure_ascii=False, indent=1), encoding="utf-8")
        st = work.load_state()
        st["pages"].setdefault(str(n), {"statuses": ["미처리"], "notes": []})
        st["total_pages"] = len(doc)
        work.save_state(st)
    log(f"prepare: {len(pages)}쪽 → {work.dir}")
    return 0


# ---------- read ----------
def make_reader(args, doc, work):
    if args.reader == "mock":
        from .reader_mock import MockReader
        tls = {}
        for n in range(1, len(doc) + 1):
            tls[n] = text_layer(doc, n)
        return MockReader(tls)
    from .reader_gemini import GeminiReader
    return GeminiReader(model=args.model, thinking_budget=args.thinking_budget)


def _load_page_image(work: Work, doc, n: int, dpi: int) -> tuple[Image.Image, str]:
    ip = work.page_image_path(n)
    if ip.exists():
        src_p = work.pages_dir / f"p{n:03d}.src.txt"
        return Image.open(ip), (src_p.read_text(encoding="utf-8") if src_p.exists() else "작업 폴더")
    img, src = page_image(doc, n, dpi=dpi)
    img.save(ip)
    return img, src


def fill_page_marks(pr: PageReading) -> None:
    """모델이 쪽번호·머리말 영역은 만들고 page_label/running_header 칸을 비워 두면 영역 글로 채운다."""
    from .schema import region_text
    for reg in pr.regions:
        t = (reg.title_text or region_text(reg)).strip()
        if not t:
            continue
        if reg.kind == "쪽번호" and not pr.page_label:
            pr.page_label = t
            m = __import__("re").search(r"\d{1,4}", t)
            if m and not pr.print_page:
                pr.print_page = m.group(0)
        elif reg.kind == "머리말" and not pr.running_header:
            pr.running_header = t


def remerge_record(rec: dict, layer_plain: str) -> dict:
    """저장된 1차·2차 판독으로 비교·표시를 다시 계산한다(모델 호출 없음). merged 에 손으로 고친 내용은 사라진다."""
    p1 = PageReading.model_validate(rec["pass1"])
    rereads = {k: RegionReread.model_validate(v) for k, v in (rec.get("pass2") or {}).items()}
    merged, mstats = merge_passes(p1, rereads)
    fill_page_marks(merged)
    keep = [u for u in PageReading.model_validate(rec["merged"]).uncertain
            if u.context.startswith(("자동 감지한 큰 도형", "인쇄 쪽수 판독값"))]      # 도판·쪽수 교차 확인 항목은 유지
    merged.uncertain.extend(u for u in keep if u not in merged.uncertain)
    rec["merged"] = merged.model_dump(); rec["merge_stats"] = mstats
    rec["textlayer_diff"] = compare_textlayer(merged, layer_plain)
    rec["verify"] = verify_page(merged)
    rec["remerged_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    return rec


def cmd_remerge(args, work: Work | None = None, doc=None):
    doc = doc or open_pdf(args.pdf)
    work = work or Work(args.pdf, args.work)
    records = work.records(parse_pages(args.pages, len(doc)))
    if not records:
        log("오류: 판독 기록이 없습니다."); return 2
    for rec in records:
        n = rec["pdf_page"]
        if not rec.get("pass1"):
            log(f"  {n}쪽: 1차 판독 원본이 없어 건너뜀"); continue
        before = len(rec["merged"].get("uncertain", []))
        tlp = work.textlayer_path(n)
        layer = json.loads(tlp.read_text(encoding="utf-8")).get("plain", "") if tlp.exists() else ""
        rec = remerge_record(rec, layer)
        work.save_record(n, rec)
        m = rec["merged"]; ms = rec["merge_stats"]
        if m["uncertain"] or any(i["level"] == "error" for i in rec["verify"]):
            work.set_status(n, "판독 확인 필요")
        else:
            st = work.load_state(); p = st["pages"].get(str(n))
            if p and "판독 확인 필요" in p["statuses"]:
                p["statuses"].remove("판독 확인 필요"); work.save_state(st)
        log(f"  {n}쪽: 확인 필요 {before} → {len(m['uncertain'])}건 · 열 비교 {ms.get('columns_compared')}열 중 {ms.get('columns_differ')}열 불일치"
            + (f" · 1차에만 {ms.get('columns_only_pass1')}열, 2차에만 {ms.get('columns_only_pass2')}열" if ms.get('columns_only_pass1') or ms.get('columns_only_pass2') else ""))
    log("remerge: 끝. ④ 문서 만들기를 다시 실행하십시오.")
    return 0


def cover_check(img: Image.Image, p1: PageReading) -> list[str]:
    """영역 밖에 남은 글 기둥을 찾아 가까운 문자 영역의 좌표를 넓히거나(2차 조각에 포함되게), 붙일 곳이 없으면 확인 항목으로 남긴다."""
    from .schema import bbox_frac as _bf, frac_bbox as _fb
    all_fr = [_bf(r.bbox) for r in p1.regions]
    boxes = uncovered_ink_boxes(img, all_fr)
    if not boxes:
        return []
    # 도판이 아닌 모든 영역(본문·제목·소주·쪽번호…)에 붙일 수 있다. 제목 글자가 크면 모델 좌표 밖으로 획이 삐져나오기 쉽다.
    targets = [(r.id, _bf(r.bbox)) for r in p1.regions if r.kind not in IMAGE_KINDS]
    grown, orphans = attach_uncovered(boxes, targets)
    notes = []
    for r in p1.regions:
        if r.id in grown:
            r.bbox = _fb(grown[r.id])
            notes.append(f"{r.id}: 영역 밖 글 기둥을 포함하도록 좌표를 넓힘 → {r.bbox}")
    for b in orphans:
        p1.uncertain.append(Uncertain(region_id="", context=f"어느 영역에도 들지 않은 글 기둥 (x {b[0]:.2f}~{b[2]:.2f}, y {b[1]:.2f}~{b[3]:.2f}) — 누락된 열·주석·쪽번호인지 원본 확인"))
        notes.append(f"영역 밖 글 기둥 x {b[0]:.2f}~{b[2]:.2f} → 확인 항목으로 남김")
    return notes


def read_one(work: Work, doc, reader, n: int, args) -> dict:
    total = len(doc)
    img, src = _load_page_image(work, doc, n, args.dpi)
    model_img = upscale(img, args.upscale) if max(img.size) * args.upscale <= 4200 else img
    t0 = time.time()
    u0 = dict(getattr(reader, "usage", {}) or {})
    p1 = reader.read_page(model_img, Path(args.pdf).name, n, total)
    coverage_notes = cover_check(img, p1)
    for line in coverage_notes:
        log("    " + line)
    rereads: dict[str, RegionReread] = {}
    if not args.no_second_pass:
        for reg in p1.regions:
            if reg.kind not in TEXT_KINDS:
                continue
            fit = expand_until_clean(img, bbox_frac(reg.bbox), pad=0.012)   # 가장자리 글자가 잘리지 않게 넓힌 뒤 자른다
            crop = crop_frac(img, fit["bbox"], pad=0.012)
            if fit["steps"]:
                log(f"    {reg.id}: 2차 조각 가장자리 {fit['steps']}단계 넓힘" + (f" (여전히 닿음: {fit['touching']})" if fit["touching"] else ""))
            f = max(1.0, min(4.0, 1600 / max(1, crop.height)))      # 열 글자가 또렷하도록 세로 1600px 안팎으로 확대
            rr = reader.reread_region(upscale(crop, f), reg.kind, reg.id, n)
            if rr.columns:
                rereads[reg.id] = rr
                rec_fit = rr.model_dump(); rec_fit["crop_bbox"] = list(fit["bbox"]); rec_fit["crop_steps"] = fit["steps"]
                rereads_meta = locals().setdefault("rereads_meta", {}); rereads_meta[reg.id] = rec_fit
    merged, mstats = merge_passes(p1, rereads)
    fill_page_marks(merged)
    # 도판 교차 확인(자동 감지)
    cv = detect_figures(img)
    model_figs = [bbox_frac(r.bbox) for r in merged.regions if r.kind in IMAGE_KINDS]
    for f in cv:
        if not any(overlap_ratio(f, m) > 0.5 for m in model_figs):
            merged.uncertain.append(Uncertain(region_id="", context=f"자동 감지한 큰 도형 {tuple(round(v, 2) for v in f)} 가 도판 영역으로 분류되지 않음 — 영역 분류 확인"))
    # 인쇄 쪽수 일관성(추정으로 확정하지 않고 어긋남만 표시)
    if args.print_offset is not None and merged.print_page.isdigit() and int(merged.print_page) != n + args.print_offset:
        merged.uncertain.append(Uncertain(region_id="", context=f"인쇄 쪽수 판독값 {merged.print_page} 이 PDF 쪽수+{args.print_offset}={n + args.print_offset} 과 다름 — 원본 확인"))
    # 기존 텍스트 레이어 대조
    tlp = work.textlayer_path(n)
    layer = json.loads(tlp.read_text(encoding="utf-8")) if tlp.exists() else {"plain": text_layer(doc, n).plain()}
    tdiff = compare_textlayer(merged, layer.get("plain", ""))
    issues = verify_page(merged)
    u1 = dict(getattr(reader, "usage", {}) or {})
    usage = {k: int(u1.get(k, 0)) - int(u0.get(k, 0)) for k in ("calls", "prompt_tokens", "output_tokens") if k in u1}
    rec = {"file": Path(args.pdf).name, "pdf_page": n, "reader": reader.name, "model": getattr(reader, "model", ""),
           "image_source": src, "upscale": args.upscale, "read_at": time.strftime("%Y-%m-%d %H:%M:%S"), "seconds": round(time.time() - t0, 1),
           "usage": usage,
           "pass1": p1.model_dump(), "pass2": {k: (locals().get("rereads_meta") or {}).get(k) or v.model_dump() for k, v in rereads.items()},
           "merged": merged.model_dump(), "merge_stats": mstats, "textlayer_diff": tdiff, "verify": issues, "crops": {}}
    work.save_record(n, rec)
    work.set_status(n, "영역 분류 완료"); work.set_status(n, "1차 OCR 완료")
    if rereads:
        work.set_status(n, "2차 독립 판독 비교 완료")
    if merged.uncertain or any(i["level"] == "error" for i in issues):
        work.set_status(n, "판독 확인 필요")
    return rec


def cmd_read(args, work: Work | None = None, doc=None):
    doc = doc or open_pdf(args.pdf)
    work = work or Work(args.pdf, args.work)
    pages = parse_pages(args.pages, len(doc))
    try:
        reader = make_reader(args, doc, work)
    except Exception as e:
        log(f"오류: {e}"); return 1
    log(f"read: 판독기 {reader.name} {getattr(reader, 'model', '')} · {len(pages)}쪽 · 묶음 {args.batch}쪽")
    for i in range(0, len(pages), args.batch):
        batch = pages[i:i + args.batch]
        for n in batch:
            if CANCEL.is_set():
                log("중지 요청으로 판독을 멈춤. 지금까지의 쪽은 기록에 남아 있음."); return 130
            if work.load_record(n) and not args.force:
                log(f"  {n}쪽: 기록 있음(건너뜀, --force 로 다시)"); continue
            try:
                rec = read_one(work, doc, reader, n, args)
            except Exception as e:
                from .reader_gemini import ReaderError
                if isinstance(e, ReaderError):
                    log(f"오류: {n}쪽 판독 실패 — {e}"); return 1
                raise
            m = rec["merged"]
            u = rec.get("usage") or {}
            log(f"  {n}쪽: 영역 {len(m['regions'])} · 확인 필요 {len(m['uncertain'])} · 구조 검사 {len(rec['verify'])} · 레이어 일치율 {rec['textlayer_diff'].get('ratio')} · {rec['seconds']}s"
                + (f" · 호출 {u.get('calls')}회 · 토큰 입력 {u.get('prompt_tokens'):,} 출력 {u.get('output_tokens'):,}" if u.get("calls") else ""))
        log(f"  — 묶음 {batch[0]}~{batch[-1]}쪽 끝. 앞 묶음 마지막 문장과 이음새를 검수에서 확인하십시오(지침 25).")
    if getattr(reader, "usage", None) and reader.usage.get("calls"):
        u = reader.usage
        log(f"모델 호출 통계(이번 실행): 호출 {u['calls']}회 · 토큰 입력 {u['prompt_tokens']:,} 출력 {u['output_tokens']:,}  — 쪽별 누적은 ⑥ 보고에 있음")
    return 0


# ---------- build ----------
def make_crops(work: Work, rec: dict, img: Image.Image) -> dict:
    crops = {}
    pr = PageReading.model_validate(rec["merged"])
    for reg in pr.regions:
        if reg.kind not in IMAGE_KINDS:
            continue
        fit = fit_figure_bbox(img, bbox_frac(reg.bbox), pad=0.01)
        crop = crop_frac(img, fit["bbox"], pad=0.01)
        path = work.crops_dir / f"p{pr.pdf_page:03d}_{reg.id}.png"
        crop.save(path)
        crops[reg.id] = {"path": str(path), "kind": reg.kind, "bbox_model": list(bbox_frac(reg.bbox)), "bbox_used": list(fit["bbox"]),
                         "steps": fit["steps"], "touching": fit["touching"], "frac_w": round(fit["bbox"][2] - fit["bbox"][0] + 0.02, 4),
                         "ratio": round(crop.height / max(1, crop.width), 4), "size": list(crop.size)}
    return crops


def cmd_build(args, work: Work | None = None, doc=None):
    doc = doc or open_pdf(args.pdf)
    work = work or Work(args.pdf, args.work)
    pages = parse_pages(args.pages, len(doc))
    records = work.records(pages)
    if not records:
        log("build: 판독 기록이 없습니다. 먼저 read 를 실행하십시오."); return 2
    for rec in records:
        if CANCEL.is_set():
            log("중지 요청으로 문서 만들기를 멈춤."); return 130
        n = rec["pdf_page"]
        img, _ = _load_page_image(work, doc, n, args.dpi)
        rec["crops"] = make_crops(work, rec, img)
        work.save_record(n, rec)
        touching = [k for k, c in rec["crops"].items() if c["touching"]]
        if rec["crops"]:
            if touching:
                work.set_status(n, "판독 확인 필요", note=f"도판 외곽에 글자·선이 닿음: {touching}")
            else:
                work.set_status(n, "이미지 보존 확인 완료")
    blocks = assemble_blocks(records, join_pages=not args.no_join_pages, whole_offset=args.whole_offset)
    if args.append_report:
        blocks.append(Block(type="report", text=make_report(work, len(doc))))
    stem = args.out or f"{Path(args.pdf).stem}_p{records[0]['pdf_page']:03d}-{records[-1]['pdf_page']:03d}"
    out_h = work.out_dir / f"{stem}.hwpx"
    from .hwpx_builder import build_hwpx
    st = build_hwpx(blocks, str(out_h), min_mm=args.figure_min_mm, large_min_mm=args.figure_large_min_mm)
    fissues = check_hwpx_file(str(out_h))
    want_imgs = sum(1 for b in blocks if b.type == "image")
    ok = not fissues and st.get("images_in_file") == want_imgs
    log(f"build: HWPX {out_h} · 문단 {st['paragraphs']} 제목 {st['headings']} 표 {st['tables']} 그림 {st['images']}(파일 안 {st['images_in_file']}) · 구조 검사 {'이상 없음' if ok else fissues}")
    if args.docx:
        from .docx_builder import build_docx
        out_d = work.out_dir / f"{stem}.docx"
        sd = build_docx(blocks, str(out_d), min_mm=args.figure_min_mm, large_min_mm=args.figure_large_min_mm)
        log(f"build: DOCX {out_d} · {sd}")
    for rec in records:
        work.set_status(rec["pdf_page"], "문서 반영 완료")
        if ok:
            work.set_status(rec["pdf_page"], "파일 구조 검사 완료")
    return 0 if ok else 1


# ---------- review / report / mark ----------
def cmd_review(args, work: Work | None = None, doc=None):
    work = work or Work(args.pdf, args.work)
    doc = doc or open_pdf(args.pdf)
    from .review_html import make_review
    recs = work.records(parse_pages(args.pages, len(doc)))
    if not recs:
        log("오류: 판독 기록이 없어 검수 화면을 만들지 않았습니다. 먼저 ③ 판독(read)을 실행하십시오."); return 2
    out = make_review(work, recs)
    log(f"review: {out} ({len(recs)}쪽)")
    return 0


def cmd_report(args, work: Work | None = None, doc=None):
    work = work or Work(args.pdf, args.work)
    doc = doc or open_pdf(args.pdf)
    txt = make_report(work, len(doc))
    (work.dir / "report.md").write_text(txt, encoding="utf-8")
    log(txt)
    return 0


def cmd_mark(args):
    work = Work(args.pdf, args.work)
    doc = open_pdf(args.pdf)
    for n in parse_pages(args.pages, len(doc)):
        work.set_status(n, args.status, note=args.note, remove=["판독 확인 필요"] if args.status in HUMAN_ONLY and args.clear_check else None)
    log(f"mark: {args.pages} → {args.status}")
    return 0


def cmd_run(args):
    doc = open_pdf(args.pdf)
    work = Work(args.pdf, args.work)
    cmd_prepare(args, work, doc)
    rc = cmd_read(args, work, doc)
    if rc or CANCEL.is_set(): return rc or 130
    rc = cmd_build(args, work, doc)
    if CANCEL.is_set(): return 130
    cmd_review(args, work, doc)
    cmd_report(args, work, doc)
    return rc


def build_parser():
    ap = argparse.ArgumentParser(prog="yeone-ocr", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", action="version", version=f"YeonE_OCR {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p, pages=True):
        p.add_argument("pdf")
        p.add_argument("--work", help="작업 폴더(기본 work/<PDF 이름>)")
        if pages:
            p.add_argument("--pages", help="예 1-5,8 (기본 전체)")
        p.add_argument("--dpi", type=int, default=300, help="내장 스캔이 없을 때 렌더 해상도")

    def read_opts(p):
        p.add_argument("--reader", choices=["gemini", "mock"], default="gemini")
        p.add_argument("--model", help="Gemini 모델 이름(기본 GEMINI_MODEL 환경 변수 또는 gemini-2.5-pro)")
        p.add_argument("--thinking-budget", type=int, default=None, help="Gemini 생각 토큰 예산(모델이 지원할 때)")
        p.add_argument("--upscale", type=float, default=2.0, help="모델에 보낼 쪽 이미지 확대 배율")
        p.add_argument("--no-second-pass", action="store_true", help="영역 확대 재판독·비교를 생략")
        p.add_argument("--batch", type=int, default=5, help="묶음 쪽수(지침 25: 본문 3~5, 복잡한 판면 1~3)")
        p.add_argument("--force", action="store_true", help="기존 기록이 있어도 다시 판독")
        p.add_argument("--print-offset", type=int, default=None, help="인쇄 쪽수 = PDF 쪽수 + N 인지 어긋남만 표시")

    def build_opts(p):
        p.add_argument("--docx", action="store_true", help="DOCX 도 함께 생성")
        p.add_argument("--out", help="출력 파일 이름(확장자 제외)")
        p.add_argument("--figure-min-mm", type=float, default=70)
        p.add_argument("--figure-large-min-mm", type=float, default=120, help="나경도·복잡한 표·계통도 최소 폭")
        p.add_argument("--no-join-pages", action="store_true", help="앞 쪽에서 이어지는 문단을 잇지 않음")
        p.add_argument("--whole-offset", type=int, default=None, help="분할 전 전체 PDF 쪽수 = PDF 쪽수 + N 을 기록에 함께 적음")
        p.add_argument("--append-report", action="store_true", help="문서 끝에 〔작업 기록〕 덧붙임")

    p = sub.add_parser("inspect"); common(p, pages=False); p.set_defaults(fn=cmd_inspect)
    p = sub.add_parser("prepare"); common(p); p.add_argument("--force", action="store_true"); p.set_defaults(fn=cmd_prepare)
    p = sub.add_parser("read"); common(p); read_opts(p); p.set_defaults(fn=cmd_read)
    p = sub.add_parser("build"); common(p); build_opts(p); p.set_defaults(fn=cmd_build)
    p = sub.add_parser("remerge", help="저장된 1차·2차 판독으로 비교·표시를 다시 계산(모델 호출 없음)"); common(p); p.set_defaults(fn=cmd_remerge)
    p = sub.add_parser("review"); common(p); p.set_defaults(fn=cmd_review)
    p = sub.add_parser("report"); common(p); p.set_defaults(fn=cmd_report)
    p = sub.add_parser("mark"); common(p); p.add_argument("--status", required=True, choices=STATUSES); p.add_argument("--note")
    p.add_argument("--clear-check", action="store_true", help="'판독 확인 필요' 표시를 지움(사람이 확인한 뒤)"); p.set_defaults(fn=cmd_mark)
    p = sub.add_parser("run"); common(p); read_opts(p); build_opts(p); p.set_defaults(fn=cmd_run)
    return ap


def main(argv=None):
    args = build_parser().parse_args(argv)
    if not Path(args.pdf).exists():
        log(f"파일이 없습니다: {args.pdf}"); return 2
    return args.fn(args)
