"""명령줄. python -m yeone_ocr <명령> <PDF> [옵션]

  inspect  PDF 구조·텍스트 레이어 상태 보고
  prepare  쪽 이미지(내장 스캔 우선)와 기존 텍스트 레이어를 작업 폴더에 꺼냄
  estimate 판독 전에 호출·토큰·비용 어림
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
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
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
DOC_LOCK = threading.Lock()  # PyMuPDF 문서 객체는 스레드 안전하지 않다. 여러 쪽을 동시에 읽을 때 문서 접근을 직렬화한다.
LOG_LOCK = threading.Lock()


def log(*a):
    with LOG_LOCK:
        print(*a, flush=True)


# ---------- inspect ----------
def cmd_inspect(args):
    doc = open_pdf(args.pdf)
    log(f"파일 {Path(args.pdf).name} · 쪽수 {len(doc)} · 메타 { {k: v for k, v in doc.metadata.items() if v} }")
    log("쪽 | 크기(pt) | 쪽 이미지 | 텍스트 레이어 글자수 | 쪽번호 표기(레이어)")
    many = len(doc) > 60
    for n in range(1, len(doc) + 1):
        if many and not (n <= 20 or n > len(doc) - 5 or n % 50 == 0):
            continue                                   # 쪽이 많으면 앞 20쪽·뒤 5쪽·50쪽마다만
        p = doc[n - 1]
        imgs = p.get_images(full=True)
        if len(imgs) == 1:
            info = doc.extract_image(imgs[0][0]); src = f"내장 스캔 {info['width']}x{info['height']} {info['ext']}"
        else:
            src = f"이미지 {len(imgs)}개(렌더 필요)"
        tl = text_layer(doc, n)
        log(f"{n:>3} | {p.rect.width:.0f}x{p.rect.height:.0f} | {src} | {tl.char_count:>5} | {tl.page_label}")
    if many:
        log(f"(쪽이 많아 일부만 표시. 전체 {len(doc)}쪽)")
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
    return GeminiReader(model=args.model, thinking_budget=args.thinking_budget, use_cache=not getattr(args, "no_cache", False))


def _load_page_image(work: Work, doc, n: int, dpi: int) -> tuple[Image.Image, str]:
    ip = work.page_image_path(n)
    if ip.exists():
        src_p = work.pages_dir / f"p{n:03d}.src.txt"
        return Image.open(ip), (src_p.read_text(encoding="utf-8") if src_p.exists() else "작업 폴더")
    with DOC_LOCK:
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


def orphan_message(b) -> str:
    """영역 밖 글줄 흔적을 사람이 읽는 말로. 좌표는 검수 화면이 상자를 그릴 때 다시 읽는다(가로 a~b%, 세로 c~d%)."""
    hx, hy = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2
    hpos = "왼쪽" if hx < 0.33 else ("가운데" if hx < 0.67 else "오른쪽")
    vpos = "위" if hy < 0.33 else ("중간" if hy < 0.67 else "아래")
    return (f"영역 밖 글줄 흔적 — 쪽 {hpos} {vpos}(가로 {b[0] * 100:.0f}~{b[2] * 100:.0f}%, 세로 {b[1] * 100:.0f}~{b[3] * 100:.0f}%)에 "
            f"모델이 어느 영역에도 넣지 않은 세로 글줄이 있음. 빠진 열·주석·쪽번호인지 원본 확인(검수 화면의 붉은 점선 상자)")


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
        p1.uncertain.append(Uncertain(region_id="", context=orphan_message(b)))
        notes.append(f"영역 밖 글 기둥 x {b[0]:.2f}~{b[2]:.2f} → 확인 항목으로 남김")
    return notes


def nfc_reading(pr: PageReading) -> PageReading:
    """모델이 돌려준 모든 글을 NFC 로 정규화한다(호환 한자 → 표준 한자). 자형은 같고 부호값만 바뀐다."""
    import unicodedata
    from .merge import INVISIBLE
    f = lambda t: INVISIBLE.sub("", unicodedata.normalize("NFC", t or ""))
    for r in pr.regions:
        r.title_text = f(r.title_text); r.note = f(r.note)
        for p in r.paragraphs:
            for c in p.columns:
                c.text = f(c.text)
        for row in r.table_rows:
            row.cells = [f(x) for x in row.cells]
    for u in pr.uncertain:
        u.context = f(u.context)
    pr.page_label = f(pr.page_label); pr.running_header = f(pr.running_header)
    return pr


def _second_pass(reader2, img, p1: PageReading, n: int, args) -> tuple[dict[str, RegionReread], dict]:
    """문자 영역마다 확대 조각을 만들어 2차 독립 판독. 기본은 조각들을 한 호출에 묶어 보내고(빠르고 쌈), 빠진 영역은 낱개로 다시 묻는다."""
    import unicodedata as _ud
    items: list[tuple[str, str, Image.Image]] = []
    meta: dict[str, dict] = {}
    for reg in p1.regions:
        if reg.kind not in TEXT_KINDS:
            continue
        fit = expand_until_clean(img, bbox_frac(reg.bbox), pad=0.012)   # 가장자리 글자가 잘리지 않게 넓힌 뒤 자른다
        crop = crop_frac(img, fit["bbox"], pad=0.012)
        f = max(1.0, min(4.0, 1600 / max(1, crop.height)))              # 열 글자가 또렷하도록 세로 1600px 안팎으로 확대
        items.append((reg.id, reg.kind, upscale(crop, f)))
        meta[reg.id] = {"crop_bbox": list(fit["bbox"]), "crop_steps": fit["steps"], "crop_touching": fit["touching"]}
    rereads: dict[str, RegionReread] = {}
    if not items:
        return rereads, meta
    if args.second_mode == "batch" and len(items) > 1 and hasattr(reader2, "reread_regions"):
        try:
            rereads = reader2.reread_regions(items, n)
        except Exception as e:
            from .reader_gemini import ReaderError
            if isinstance(e, ReaderError) and e.fatal:
                raise
            log(f"    {n}쪽: 묶음 2차 판독 실패({str(e)[:60]}) → 영역별로 다시")
            rereads = {}
    for rid, kind, im in items:
        if rid in rereads and rereads[rid].columns:
            continue
        rr = reader2.reread_region(im, kind, rid, n)
        if rr.columns:
            rereads[rid] = rr
    for rr in rereads.values():
        for c in rr.columns:
            c.text = _ud.normalize("NFC", c.text or "")
    return rereads, meta


def read_one(work: Work, doc, reader, reader2, n: int, args) -> dict:
    total = len(doc)
    img, src = _load_page_image(work, doc, n, args.dpi)
    model_img = upscale(img, args.upscale) if max(img.size) * args.upscale <= 4200 else img
    t0 = time.time()
    for r in {id(reader): reader, id(reader2): reader2}.values():
        if hasattr(r, "usage_begin"): r.usage_begin()
    p1 = nfc_reading(reader.read_page(model_img, Path(args.pdf).name, n, total))
    coverage_notes = cover_check(img, p1)
    rereads: dict[str, RegionReread] = {}
    meta: dict[str, dict] = {}
    if not args.no_second_pass:
        rereads, meta = _second_pass(reader2, img, p1, n, args)
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
    if tlp.exists():
        layer = json.loads(tlp.read_text(encoding="utf-8"))
    else:
        with DOC_LOCK:
            layer = {"plain": text_layer(doc, n).plain()}
    tdiff = compare_textlayer(merged, layer.get("plain", ""))
    issues = verify_page(merged)
    usage = {}; by_model = {}
    for r in {id(reader): reader, id(reader2): reader2}.values():
        if hasattr(r, "usage_end"):
            u = r.usage_end()
            for k, v in u.items():
                usage[k] = usage.get(k, 0) + v
            if u.get("calls"):
                by_model[getattr(r, "model", "?")] = u          # 모델별 실측 — 비용 어림에서 모델마다 제 단가로 계산
    if by_model:
        usage["by_model"] = by_model
    pass2 = {}
    for k, v in rereads.items():
        d = v.model_dump(); d.update(meta.get(k, {})); pass2[k] = d
    rec = {"file": Path(args.pdf).name, "pdf_page": n, "reader": reader.name, "model": getattr(reader, "model", ""),
           "second_model": getattr(reader2, "model", "") if reader2 is not reader else "",
           "image_source": src, "upscale": args.upscale, "read_at": time.strftime("%Y-%m-%d %H:%M:%S"), "seconds": round(time.time() - t0, 1),
           "usage": usage, "coverage_notes": coverage_notes,
           "pass1": p1.model_dump(), "pass2": pass2,
           "merged": merged.model_dump(), "merge_stats": mstats, "textlayer_diff": tdiff, "verify": issues, "crops": {}}
    work.save_record(n, rec)
    work.set_status(n, "영역 분류 완료"); work.set_status(n, "1차 OCR 완료")
    if rereads:
        work.set_status(n, "2차 독립 판독 비교 완료")
    if merged.uncertain or any(i["level"] == "error" for i in issues):
        work.set_status(n, "판독 확인 필요")
    return rec


def cmd_read(args, work: Work | None = None, doc=None, checkpoint_fn=None):
    """쪽을 여러 개 동시에 판독한다(--workers). 치명 오류(키·권한·모델·한도 0)는 전체를 멈추고, 일시 오류는 그 쪽만 건너뛴다.
    checkpoint_fn 이 있으면 --checkpoint 쪽마다, 그리고 멈출 때 호출해 중간 HWPX 를 남긴다."""
    doc = doc or open_pdf(args.pdf)
    work = work or Work(args.pdf, args.work)
    pages = parse_pages(args.pages, len(doc))
    try:
        reader = make_reader(args, doc, work)
        reader2 = reader
        if args.reader == "gemini":
            from .reader_gemini import GeminiReader, list_models, pick_second_model
            want = (args.second_model or "").strip()
            if want.lower() == "auto":
                try:
                    want = pick_second_model(list_models(reader.key, reader.vertexai), reader.model)
                    log(f"  2차 판독 모델 자동 선택: {want}" + ("" if want != reader.model else " (같은 세대 flash 가 없어 1차와 같은 모델)"))
                except Exception as e:
                    want = ""; log(f"  2차 모델 자동 선택 실패({str(e)[:60]}) → 1차와 같은 모델")
            think2 = "low" if args.second_thinking == "low" else None
            if (want and want != reader.model) or think2:
                reader2 = GeminiReader(model=want or reader.model, thinking_budget=args.thinking_budget, use_cache=not args.no_cache, thinking=think2)
    except Exception as e:
        log(f"오류: {e}"); return 1
    todo = [n for n in pages if args.force or not work.load_record(n)]
    skipped = len(pages) - len(todo)
    ceiling = max(1, int(args.workers or 1))
    cur = max(1, min(ceiling, int(args.workers_start or ceiling)))     # 동시 쪽 수: 여기서 시작해 한도(429)가 나면 줄이고, 안정되면 늘린다
    log(f"read: 판독기 {reader.name} {getattr(reader, 'model', '')}"
        + (f" · 2차 {reader2.model}{' (생각 줄임)' if getattr(reader2, 'thinking', None) == 'low' else ''}" if reader2 is not reader else "")
        + f" · {len(pages)}쪽 중 읽을 쪽 {len(todo)}개 · 동시 {cur}쪽(최대 {ceiling}, 자동 조절) · 2차 판독 {'묶음 호출' if args.second_mode == 'batch' else '영역별 호출'}"
        + (f" · {reader.cache_note}" if getattr(reader, "cache_note", "") else ""))
    if skipped:
        log(f"  기록이 있어 건너뛴 쪽 {skipped}개 (다시 읽으려면 '기록 있는 쪽도 다시 판독' 또는 --force)")
    from .reader_gemini import ReaderError
    failed: list[int] = []; fatal: Exception | None = None
    done_now = 0; since_ckpt = 0; t_start = time.time()

    def checkpoint(reason: str):
        nonlocal since_ckpt
        if checkpoint_fn and done_now:
            try:
                checkpoint_fn(reason)
            except Exception as e:
                log(f"  중간 저장 실패(계속 진행): {e}")
        since_ckpt = 0

    it = iter(todo)
    futures: dict = {}
    readers = list({id(reader): reader, id(reader2): reader2}.values())
    def n429() -> int:
        return sum(getattr(r, "n_429", 0) for r in readers)
    seen_429 = n429(); clean_since = 0; pause_until = 0.0; exhausted = False
    with ThreadPoolExecutor(max_workers=ceiling) as ex:
        def submit_next() -> bool:
            nonlocal exhausted
            if fatal is not None or CANCEL.is_set() or exhausted:
                return False
            for n in it:
                futures[ex.submit(read_one, work, doc, reader, reader2, n, args)] = n
                return True
            exhausted = True
            return False
        while True:
            while len(futures) < cur and time.time() >= pause_until and submit_next():
                pass
            if not futures:
                if not exhausted and fatal is None and not CANCEL.is_set() and time.time() < pause_until:
                    time.sleep(1); continue
                break
            done_set, _ = wait(list(futures), return_when=FIRST_COMPLETED, timeout=5)
            for fut in done_set:
                n = futures.pop(fut)
                try:
                    rec = fut.result()
                    done_now += 1; since_ckpt += 1
                    m = rec["merged"]; u = rec.get("usage") or {}
                    log(f"  {n}쪽: 영역 {len(m['regions'])} · 확인 필요 {len(m['uncertain'])} · 구조 검사 {len(rec['verify'])} · 레이어 일치율 {rec['textlayer_diff'].get('ratio')} · {rec['seconds']}s"
                        + (f" · 호출 {u.get('calls')}회 · 토큰 입력 {u.get('prompt_tokens', 0):,} 출력 {u.get('output_tokens', 0) + u.get('thinking_tokens', 0):,}" if u.get("calls") else ""))
                    for line in rec.get("coverage_notes") or []:
                        log("    " + line)
                except ReaderError as e:
                    if e.fatal:
                        fatal = e; log(f"오류: {n}쪽 판독 실패 — {e}")
                    else:
                        failed.append(n); work.set_status(n, "판독 확인 필요", note=f"판독 실패(다시 시도 가능): {e}")
                        log(f"  {n}쪽: 판독 실패 — {str(e)[:120]} → 건너뛰고 계속")
                except Exception as e:
                    failed.append(n); work.set_status(n, "판독 확인 필요", note=f"판독 중 예외: {type(e).__name__}: {e}")
                    log(f"  {n}쪽: 판독 중 예외 {type(e).__name__}: {str(e)[:120]} → 건너뛰고 계속")
                clean_since += 1
            # 동시 쪽 수 자동 조절: 한도 초과가 새로 보이면 하나 줄이고 20초 쉼, 10쪽 연속 깨끗하면 하나 늘림
            now429 = n429()
            if now429 > seen_429:
                seen_429 = now429; clean_since = 0
                if cur > 1:
                    cur -= 1; log(f"  — 한도 초과(429) 감지 → 동시 {cur}쪽으로 줄이고 20초 쉼")
                else:
                    log("  — 한도 초과(429) 감지 → 20초 쉼")
                pause_until = time.time() + 20
            elif clean_since >= 10 and cur < ceiling:
                cur += 1; clean_since = 0; log(f"  — 안정적 → 동시 {cur}쪽으로 늘림")
            if done_set and done_now and done_now % 10 == 0 and len(todo) > 10:
                per = (time.time() - t_start) / done_now; left = len(todo) - done_now - len(failed)
                log(f"  — 진행 {done_now}/{len(todo)}쪽 · 실효 쪽당 {per:.0f}초(동시 {cur}) · 남은 {left}쪽 예상 {left * per / 60:.0f}분")
            if args.checkpoint and since_ckpt >= args.checkpoint:
                checkpoint(f"{done_now}쪽 진행")
    if fatal is not None or CANCEL.is_set():
        checkpoint("멈춤")
    if failed:
        log(f"오류: 판독에 실패한 쪽 {len(failed)}개: {', '.join(map(str, sorted(failed)[:30]))}{'…' if len(failed) > 30 else ''} — ③ 판독을 다시 누르면 이 쪽들만 다시 시도합니다")
    if getattr(reader, "usage", None) and reader.usage.get("calls"):
        u = reader.usage
        log(f"모델 호출 통계(이번 실행): 호출 {u['calls']}회 · 토큰 입력 {u['prompt_tokens']:,} 출력 {u['output_tokens'] + u.get('thinking_tokens', 0):,}"
            + (f" · 캐시 적중 {u['cached_tokens']:,}" if u.get("cached_tokens") else "") + f" · {time.time() - t_start:.0f}초  — 쪽별 누적은 ⑥ 보고에 있음")
    if fatal is not None:
        return 1
    if CANCEL.is_set():
        log(f"중지 요청으로 판독을 멈춤. 지금까지의 쪽은 기록에 남아 있음. (이번에 {done_now}쪽 판독)"); return 130
    return 0


# ---------- build ----------
def make_crops(work: Work, rec: dict, img: Image.Image) -> dict:
    """도판을 원본 이미지에서 잘라 저장한다. 같은 좌표로 이미 잘라 둔 파일이 있으면 다시 자르지 않는다(중간 저장을 자주 해도 빠르게)."""
    crops = dict(rec.get("crops") or {})
    pr = PageReading.model_validate(rec["merged"])
    out: dict = {}
    for reg in pr.regions:
        if reg.kind not in IMAGE_KINDS:
            continue
        old = crops.get(reg.id)
        if old and old.get("path") and Path(old["path"]).exists() and old.get("bbox_model") == list(bbox_frac(reg.bbox)):
            out[reg.id] = old; continue
        fit = fit_figure_bbox(img, bbox_frac(reg.bbox), pad=0.01)
        crop = crop_frac(img, fit["bbox"], pad=0.01)
        path = work.crops_dir / f"p{pr.pdf_page:03d}_{reg.id}.jpg"     # 스캔 도판은 JPEG 가 PNG 의 1/4~1/5 크기. 수백 장이 들어가도 문서가 가볍다
        (crop.convert("L") if crop.mode not in ("L", "RGB") else crop).save(path, "JPEG", quality=90)
        out[reg.id] = {"path": str(path), "kind": reg.kind, "bbox_model": list(bbox_frac(reg.bbox)), "bbox_used": list(fit["bbox"]),
                       "steps": fit["steps"], "touching": fit["touching"], "frac_w": round(fit["bbox"][2] - fit["bbox"][0] + 0.02, 4),
                       "ratio": round(crop.height / max(1, crop.width), 4), "size": list(crop.size)}
    return out


def build_outputs(work: Work, doc, records: list[dict], args, interim: bool = False) -> bool:
    """판독 기록들로 HWPX(+DOCX)를 만든다. interim=True 면 '<이름>_진행중.hwpx' 한 파일만 조용히 만든다(중간 저장)."""
    from .hwpx_builder import build_hwpx
    for rec in records:
        n = rec["pdf_page"]
        img, _ = _load_page_image(work, doc, n, args.dpi)
        new_crops = make_crops(work, rec, img)
        if new_crops != (rec.get("crops") or {}):
            rec["crops"] = new_crops; work.save_record(n, rec)
        if not interim and rec["crops"]:
            touching = [k for k, c in rec["crops"].items() if c["touching"]]
            if touching:
                work.set_status(n, "판독 확인 필요", note=f"도판 외곽에 글자·선이 닿음: {touching}")
            else:
                work.set_status(n, "이미지 보존 확인 완료")
    whole = len(records) == len(doc)
    if interim:
        chunks = [records]
    else:
        if not whole:
            log(f"build: 판독 기록이 있는 {len(records)}쪽만 문서에 넣음 (PDF 전체 {len(doc)}쪽). 전체를 만들려면 쪽 범위를 비우고 ③ 판독을 마치십시오.")
        chunks = [records[i:i + args.split] for i in range(0, len(records), args.split)] if getattr(args, "split", 0) and args.split > 0 else [records]
    ok = True
    for ci, chunk in enumerate(chunks):
        blocks = assemble_blocks(chunk, join_pages=not args.no_join_pages, whole_offset=args.whole_offset,
                                 check_list=getattr(args, "check_list", "none") or "none")
        if not interim and args.append_report and ci == len(chunks) - 1:
            blocks.append(Block(type="report", text=make_report(work, len(doc))))
        if interim:
            stem = f"{Path(args.pdf).stem}_진행중"
        elif args.out:
            stem = args.out + (f"_{ci + 1:02d}" if len(chunks) > 1 else "")
        elif whole and len(chunks) == 1:
            stem = Path(args.pdf).stem
        else:
            stem = f"{Path(args.pdf).stem}_p{chunk[0]['pdf_page']:03d}-{chunk[-1]['pdf_page']:03d}"
        out_h = work.out_dir / f"{stem}.hwpx"
        t0 = time.time()
        st = build_hwpx(blocks, str(out_h), min_mm=args.figure_min_mm, large_min_mm=args.figure_large_min_mm,
                        highlight=not args.no_highlight, page_breaks=not args.no_page_breaks)
        fissues = check_hwpx_file(str(out_h))
        want_imgs = sum(1 for b in blocks if b.type == "image")
        this_ok = not fissues and st.get("images_in_file") == want_imgs
        ok = ok and this_ok
        size_mb = out_h.stat().st_size / 1e6
        if interim:
            log(f"  중간 저장: {out_h.name} ({len(chunk)}쪽, {size_mb:.1f}MB, {time.time() - t0:.0f}초)")
            continue
        log(f"build: HWPX {out_h} ({size_mb:.1f}MB, {time.time() - t0:.0f}초) · 쪽 {len(chunk)} · 문단 {st['paragraphs']} 제목 {st['headings']} 표 {st['tables']} 그림 {st['images']}(파일 안 {st['images_in_file']}) · 의심 표시 {st['marks']}곳 · 쪽 나누기 {st['page_breaks']} · 구조 검사 {'이상 없음' if this_ok else fissues}")
        if args.docx:
            from .docx_builder import build_docx
            out_d = work.out_dir / f"{stem}.docx"
            sd = build_docx(blocks, str(out_d), min_mm=args.figure_min_mm, large_min_mm=args.figure_large_min_mm,
                            highlight=not args.no_highlight, page_breaks=not args.no_page_breaks)
            log(f"build: DOCX {out_d} · {sd}")
    if not interim:
        for rec in records:
            work.set_status(rec["pdf_page"], "문서 반영 완료")
            if ok:
                work.set_status(rec["pdf_page"], "파일 구조 검사 완료")
    return ok


def cmd_build(args, work: Work | None = None, doc=None):
    doc = doc or open_pdf(args.pdf)
    work = work or Work(args.pdf, args.work)
    pages = parse_pages(args.pages, len(doc))
    records = work.records(pages)
    if not records:
        log("build: 판독 기록이 없습니다. 먼저 read 를 실행하십시오."); return 2
    ok = build_outputs(work, doc, records, args, interim=False)
    interim = work.out_dir / f"{Path(args.pdf).stem}_진행중.hwpx"
    if ok and interim.exists():
        try: interim.unlink(); log("  진행중 파일을 지움(최종 문서가 만들어짐)")
        except OSError: pass
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


def cmd_estimate(args, work: Work | None = None, doc=None):
    """판독 전에 호출·토큰·비용을 어림해 보여 준다."""
    from .estimate import estimate, format_estimate
    doc = doc or open_pdf(args.pdf)
    work = work or Work(args.pdf, args.work)
    pages = parse_pages(args.pages, len(doc))
    prices = {}
    if getattr(args, "price_in", None) or getattr(args, "price_out", None):
        prices[args.model or ""] = {"in": args.price_in or 0, "out": args.price_out or 0}
    e = estimate(args.pdf, pages, upscale=args.upscale, second_pass=not args.no_second_pass, model=args.model or "",
                 prices=prices, fx_krw=getattr(args, "fx", None), records=work.records(pages) if not getattr(args, "force", False) else [])
    log(format_estimate(e))
    return 0


def cmd_run(args):
    doc = open_pdf(args.pdf)
    work = Work(args.pdf, args.work)
    cmd_prepare(args, work, doc)
    pages = parse_pages(args.pages, len(doc))

    def checkpoint(reason: str):
        recs = work.records(pages)
        if recs:
            build_outputs(work, doc, recs, args, interim=True)

    rc = cmd_read(args, work, doc, checkpoint_fn=checkpoint if args.checkpoint else None)
    if rc or CANCEL.is_set():
        if work.records(pages):
            log("판독이 중간에 멈춰 '진행중' HWPX 를 남겼습니다. 다시 실행하면 남은 쪽부터 이어서 합니다.")
        return rc or 130
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
        p.add_argument("--workers", type=int, default=6, help="동시에 판독할 쪽 수의 최대(기본 6). 한도(429)가 나면 자동으로 줄인다")
        p.add_argument("--workers-start", type=int, default=4, help="시작 동시 쪽 수(기본 4). 10쪽 연속 문제없으면 하나씩 늘린다")
        p.add_argument("--second-mode", choices=["batch", "each"], default="batch", help="2차 판독을 조각 묶음 한 호출(batch, 기본)로 할지 영역별 호출(each)로 할지")
        p.add_argument("--second-model", default="auto", help="2차 판독 모델. auto(기본)=1차와 같은 세대의 flash 자동 선택, 비우면 1차와 같음")
        p.add_argument("--second-thinking", choices=["low", "full"], default="low", help="2차 판독(전사)의 생각 수준. low(기본)=줄여서 빠르게")
        p.add_argument("--no-cache", action="store_true", help="지침 컨텍스트 캐시를 쓰지 않음")
        p.add_argument("--checkpoint", type=int, default=20, help="N쪽 판독마다 '<이름>_진행중.hwpx' 를 중간 저장(0=안 함). run 에서만")

    def build_opts(p):
        p.add_argument("--docx", action="store_true", help="DOCX 도 함께 생성")
        p.add_argument("--out", help="출력 파일 이름(확장자 제외)")
        p.add_argument("--figure-min-mm", type=float, default=70)
        p.add_argument("--figure-large-min-mm", type=float, default=120, help="나경도·복잡한 표·계통도 최소 폭")
        p.add_argument("--no-join-pages", action="store_true", help="앞 쪽에서 이어지는 문단을 잇지 않음")
        p.add_argument("--whole-offset", type=int, default=None, help="분할 전 전체 PDF 쪽수 = PDF 쪽수 + N 을 기록에 함께 적음")
        p.add_argument("--append-report", action="store_true", help="문서 끝에 〔작업 기록〕 덧붙임")
        p.add_argument("--no-highlight", action="store_true", help="의심 자리를 붉은 글자·노란 형광펜으로 칠하지 않음")
        p.add_argument("--no-page-breaks", action="store_true", help="원본 쪽마다 쪽 나누기를 넣지 않음")
        p.add_argument("--check-list", choices=["none", "extra", "all"], default="none",
                       help="쪽 끝 확인 목록: none=없음(기본, 문서에는 OCR 본문만), extra=본문에 표시 못 한 항목만, all=전부. 전체 목록은 검수 화면에 있음")
        p.add_argument("--split", type=int, default=0, help="N쪽씩 여러 HWPX 로 나눔(0=한 파일). 700쪽짜리를 권별로 나눌 때")

    p = sub.add_parser("inspect"); common(p, pages=False); p.set_defaults(fn=cmd_inspect)
    p = sub.add_parser("prepare"); common(p); p.add_argument("--force", action="store_true"); p.set_defaults(fn=cmd_prepare)
    p = sub.add_parser("read"); common(p); read_opts(p); p.set_defaults(fn=cmd_read)
    p = sub.add_parser("estimate", help="판독 전 호출·토큰·비용 어림"); common(p); read_opts(p)
    p.add_argument("--price-in", type=float, help="입력 토큰 100만 개당 USD"); p.add_argument("--price-out", type=float, help="출력 토큰 100만 개당 USD")
    p.add_argument("--fx", type=float, help="원/달러 환율(원화 표시)"); p.set_defaults(fn=cmd_estimate)
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
