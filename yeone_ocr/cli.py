"""명령줄. python -m yeone_ocr <명령> <PDF·이미지> [옵션]   (명령마다 -h 로 자세히)

  inspect  PDF 구조·텍스트 레이어 상태 보고
  prepare  쪽 이미지(내장 스캔 우선)와 기존 텍스트 레이어를 작업 폴더에 꺼냄
  estimate 판독 전에 호출·토큰·비용 어림
  read     모델(기본 gemini)로 영역 분류·판독 → 확대 재판독 비교 → 텍스트 레이어 대조 → 구조 검사
  build    도판 잘라내기(외곽 검사) → HWPX(+DOCX) 조립 → 파일 구조 검사
  remerge  저장된 1차·2차 판독으로 비교·표시만 다시 계산 (모델 호출 없음)
  adjudicate  3차 글자 판별: 1차·2차가 갈린 〔a/b〕 자리를 글자 하나씩 크게 잘라 모델에 자형만으로 묻는다. 이어서 합의 판독 의심
           (1차·2차가 같은데 글자층과 자형 혼동 쌍 한 글자가 다른 자리)도 묻고, 본문은 두고 확인 항목만 단다(--no-suspects 면 안 함)
  ledger   혼동 장부: 어느 글자가 어느 글자로 읽혔는지 결과 폴더 전체에서 모아 confusion.json 과 표로 (모델 호출 없음)
  review   검수용 review.html
  report   작업 상태 보고 report.md
  mark     사람이 확인한 상태 기록 (예: --status "원본 대조 완료(사람)")
  run      한 번에: prepare → read → adjudicate(3차 판별·합의 의심, gemini 일 때 · --no-adjudicate 면 뺌) → build → review → ledger → report
           (읽기 모드·인쇄 쪽수 차이 자동 판정, 이전 판 기록 갱신, --checkpoint 쪽마다 '진행중' HWPX 중간 저장)
  collate  책갈피 교정: --hwpx 의 구획(책갈피·제목)마다 PDF 판독(없는 쪽은 판독)과 대조해 보고, --apply 면 고친 사본(<이름>_교정.hwpx)에 저장.
           자형 혼동 자리는 3차 판별로 가림(--no-glyph-check 면 안 함). --list 는 구획 짝 목록만
  eval     평가: 저장된 판독 기록을 사람이 입력한 원본 HWPX(--hwpx, 교정본은 안 됨)와 맞춰 단계별 CER·표시 밖 오류율·3차 정밀도 (모델 호출 없음)
"""
from __future__ import annotations
import argparse, functools, inspect, json, re, sys, time, threading
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from pathlib import Path
from PIL import Image

from . import __version__
from .state import Work, HUMAN_ONLY, STATUSES, legacy_work_dir, write_atomic, write_atomic_with
from .pdfio import open_pdf, parse_pages, page_image, text_layer
from .reader_gemini import ReaderError
from .schema import PageReading, RegionReread, Uncertain, TEXT_KINDS, IMAGE_KINDS, bbox_frac
from .layout import upscale, crop_frac, fit_figure_bbox, grow_to_components, expand_until_clean, detect_figures, overlap_ratio, uncovered_ink_boxes, attach_uncovered, snap_to_gaps, split_bands
from .merge import merge_passes, compare_textlayer
from .verify import verify_page, check_hwpx_file
from .blocks import assemble_blocks, Block
from .report import make_report
from .refit import refit_regions, describe as refit_describe
from .adjudicate import MIN_GLYPH_PX


CANCEL = threading.Event()   # 창 프로그램의 [중지] 단추가 켠다. 쪽 사이에서 확인한다.
PROGRESS = None              # 창 프로그램이 넣는 진행 알림 함수 f(단계, 끝난 수, 전체 수). 명령줄에서는 None.
DOC_LOCK = threading.Lock()  # PyMuPDF 문서 객체는 스레드 안전하지 않다. 여러 쪽을 동시에 읽을 때 문서 접근을 직렬화한다.
LOG_LOCK = threading.Lock()


def _document_command(fn):
    """명령이 연 PDF만 명령 끝에 닫는다. 호출자가 전달한 문서는 빌려 쓴다."""
    signature = inspect.signature(fn)
    @functools.wraps(fn)
    def wrapped(*args, **kwargs):
        bound = signature.bind(*args, **kwargs)
        if bound.arguments.get("doc") is not None:
            return fn(*args, **kwargs)
        with open_pdf(bound.arguments["args"].pdf) as doc:
            bound.arguments["doc"] = doc
            return fn(*bound.args, **bound.kwargs)
    return wrapped


def log(*a):
    with LOG_LOCK:
        print(*a, flush=True)


def progress(stage: str, done: int, total: int) -> None:
    """진행 막대에 알린다(창 프로그램에서만). 알림이 실패해도 작업은 계속한다."""
    cb = PROGRESS
    if cb is not None:
        try:
            cb(stage, int(done), int(total))
        except Exception:
            pass


# ---------- inspect ----------
@_document_command
def cmd_inspect(args, doc=None):
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
def _page_cache_io(fn):
    @functools.wraps(fn)
    def wrapped(work, doc, n, *args, **kwargs):
        with work.page_lock(n):
            return fn(work, doc, n, *args, **kwargs)
    return wrapped


def _save_png_atomic(img: Image.Image, path: Path) -> None:
    """쪽 이미지를 임시 이름으로 다 쓴 뒤 바꿔 넣는다 — 다른 스레드(판독·미리 준비)가 반쯤 쓴 PNG 를 여는 일이 없게."""
    write_atomic_with(path, lambda t: img.save(t, "PNG"))


@_page_cache_io
def _write_layer(work: Work, doc, n: int) -> dict:
    """쪽 이미지의 좌표 틀에 맞춘 글자층을 textlayer/pNNN.json 에 원자적으로 쓴다. 평문은 원래 글자층 그대로다."""
    from .pdfio import page_frame
    with DOC_LOCK:
        tl = text_layer(doc, n)
        x0, y0, width, height = page_frame(doc, n)
    # 내장 스캔은 PDF 쪽 가장자리보다 안쪽에 놓일 수 있다. 판독 bbox 는 이 스캔 기준이므로
    # 2차 영역 보정에 쓰는 줄·열도 같은 원점과 크기로 옮긴다(3차 글자 상자는 별도로 page_frame 을 쓴다).
    vertical = [{**c, "x": c["x"] - x0, "y0": c["y0"] - y0, "y1": c["y1"] - y0} for c in tl.vertical_columns]
    horizontal = [{**line, "bbox": [v - (x0 if i % 2 == 0 else y0) for i, v in enumerate(line["bbox"])]}
                  for line in tl.horizontal_lines]
    d = {"char_count": tl.char_count, "page_label": tl.page_label, "plain": tl.plain(),
         "vertical_columns": vertical, "horizontal_lines": horizontal,
         "page_width": width, "page_height": height, "page_frame": [x0, y0, width, height]}
    write_atomic(work.textlayer_path(n), json.dumps(d, ensure_ascii=False, indent=1))
    return d


@_page_cache_io
def _load_layer(work: Work, doc, n: int) -> dict:
    """쪽 글자층 사전. 파일이 없거나 깨졌으면(쓰다 만 JSON — 0.6.7 이하 결과 폴더·정전) PDF 에서 다시 만들어 둔다. doc 이 없으면 빈 사전."""
    tp = work.textlayer_path(n)
    if tp.exists():
        try:
            return json.loads(tp.read_text(encoding="utf-8"))
        except ValueError:
            if doc is None:
                return {}
            log(f"주의: 글자층 파일 textlayer/{tp.name} 이(가) 깨져 있어(쓰다 만 JSON) PDF 에서 다시 만듭니다.")
    return _write_layer(work, doc, n) if doc is not None else {}


@_page_cache_io
def _prepare_page(work: Work, doc, n: int, dpi: int, force: bool = False) -> None:
    """쪽 이미지·출처·글자층 파일(없을 때만, force 면 다시). PyMuPDF 접근은 DOC_LOCK 안에서(판독 스레드와 같은 문서 객체).
    있는 파일이 깨졌는지는 여기서 보지 않는다 — 쓰는 쪽(_load_page_image·_load_layer)이 알아보고 다시 만든다."""
    ip = work.page_image_path(n)
    if force or not ip.exists():
        with DOC_LOCK:
            img, src = page_image(doc, n, dpi=dpi)
        _save_png_atomic(img, ip)
        write_atomic(work.pages_dir / f"p{n:03d}.src.txt", src)
    if force or not work.textlayer_path(n).exists():
        _write_layer(work, doc, n)


@_document_command
def cmd_prepare(args, work: Work | None = None, doc=None, pages: list[int] | None = None, quiet: bool = False, stop: threading.Event | None = None,
                force: bool | None = None):
    """쪽 이미지·글자층을 작업 폴더에 꺼낸다. quiet=True 는 한 번에 인식의 미리 준비 스레드용: 진행·요약을 알리지 않고 CANCEL(또는 stop)이 켜지면 멈춘다.
    판독 쪽(read_one)도 없는 파일은 스스로 만든다. 같은 쪽 캐시는 잠금으로 조정하고 원자적으로 저장한다. force=None 이면 args.force 를 따른다."""
    doc = doc or open_pdf(args.pdf)
    work = work or Work(args.pdf, args.work)
    work.ensure_source(getattr(args, "dpi", 300))
    pages = parse_pages(args.pages, len(doc)) if pages is None else list(pages)
    force = bool(getattr(args, "force", False)) if force is None else bool(force)
    work.ensure_pages(pages, len(doc))
    if not quiet:
        progress("prepare", 0, len(pages))
    for i, n in enumerate(pages, 1):
        if quiet and (CANCEL.is_set() or (stop is not None and stop.is_set())):
            return 0
        _prepare_page(work, doc, n, args.dpi, force)
        if not quiet:
            progress("prepare", i, len(pages))
    if not quiet:
        log(f"prepare: {len(pages)}쪽 → {work.dir}")
    return 0


# ---------- read ----------
def apply_mode(args) -> None:
    """읽기 모드를 지시문(prompts)과 비교 규칙(merge)에 함께 적용한다."""
    from . import prompts, merge
    mode = getattr(args, "mode", None)
    prompts.set_mode(mode if mode in ("vertical", "horizontal_zh") else "vertical")
    merge.FOLD_ZH = prompts.MODE == "horizontal_zh"


def _sample_pages(n: int, k: int = 8) -> list[int]:
    """앞 표지·목차를 피해 고르게 뽑은 표본 쪽."""
    if n <= k:
        return list(range(1, n + 1))
    start = min(4, n // 10 + 1)
    return sorted({start + int(i * (n - start - 1) / (k - 1)) for i in range(k)})


def detect_mode(doc) -> tuple[str, str]:
    """글자층으로 읽기 방향을 판정한다. 세로 열 글자가 가로 줄 글자 이상이면 세로쓰기, 아니면 가로쓰기 한문. 글자층이 없으면 세로쓰기(지침 기본)."""
    v = h = 0; sample = []
    for n in _sample_pages(len(doc)):
        try:
            tl = text_layer(doc, n)
        except Exception:
            continue
        v += sum(len(c["text"]) for c in tl.vertical_columns)
        hs = "".join(l["text"] for l in tl.horizontal_lines)
        h += len(hs); sample.append(hs[:400])
    if v + h < 40:
        return "vertical", "글자층이 거의 없어 기본값(세로쓰기 한자·한글)"
    if v >= h:
        return "vertical", f"글자층 표본에서 세로 열 글자 {v:,} ≥ 가로 줄 글자 {h:,}"
    note = f"글자층 표본에서 가로 줄 글자 {h:,} > 세로 열 글자 {v:,}"
    try:
        from .collate import zh
        txt = "".join(sample); cjk = "".join(ch for ch in txt if zh.is_cjk(ch))
        if cjk and zh.available():
            changed = sum(1 for a, b in zip(cjk, zh.to_traditional(cjk)) if a != b)
            note += f" · 한자 {len(cjk):,}자 중 번체로 바뀌는 글자 {changed / len(cjk):.0%}" + ("(간체 인쇄)" if changed / len(cjk) > 0.08 else "(번체·한글 혼용으로 보이나 가로쓰기라 같은 모드로 읽음)")
    except Exception:
        pass
    return "horizontal_zh", note


def detect_print_offset(doc) -> int | None:
    """글자층의 쪽번호 표기(- 30 -)로 '인쇄 쪽수 = PDF 쪽수 + N' 의 N 을 짐작한다. 표본 3쪽 이상이 같은 값이면 그 값, 아니면 None."""
    import re
    from collections import Counter
    offs: Counter = Counter()
    for n in _sample_pages(len(doc), 12):
        try:
            tl = text_layer(doc, n)
        except Exception:
            continue
        cands = [tl.page_label] if tl.page_label else []
        H = tl.page_height or 1
        for l in tl.horizontal_lines:                                   # 머리글·꼬리글 자리의 '114' 같은 숫자만인 줄
            t = (l["text"] or "").strip(); bb = l["bbox"]
            if re.fullmatch(r"[-–—ー一·\s]*\d{1,4}[-–—ー一·\s]*", t) and (bb[3] < 0.15 * H or bb[1] > 0.85 * H):
                cands.append(t)
        if cands:
            m = re.search(r"\d{1,4}", cands[0])
            if m:
                offs[int(m.group(0)) - n] += 1
    if offs:
        off, cnt = offs.most_common(1)[0]
        if cnt >= 3 and cnt >= 0.6 * sum(offs.values()):
            return off
    return None


def offset_from_records(records: list[dict]) -> int | None:
    """판독한 쪽번호(merged.print_page)로 '인쇄 쪽수 = PDF 쪽수 + N' 을 판정한다. 2쪽 이상이 같은 값이고 과반이면 그 값."""
    from collections import Counter
    offs: Counter = Counter()
    for rec in records:
        pp = (rec.get("merged") or {}).get("print_page") or ""
        if pp.isdigit():
            offs[int(pp) - rec["pdf_page"]] += 1
    if not offs:
        return None
    off, cnt = offs.most_common(1)[0]
    return off if cnt >= 2 and cnt >= 0.6 * sum(offs.values()) else None


def flag_print_mismatch(work: "Work", records: list[dict], off: int) -> int:
    """판정한 차이와 어긋나는 쪽에 확인 항목을 붙인다(이미 있으면 그대로). 돌려주는 값은 새로 표시한 쪽 수."""
    n = 0
    for rec in records:
        m = rec.get("merged") or {}; pp = m.get("print_page") or ""
        if not pp.isdigit() or int(pp) == rec["pdf_page"] + off:
            continue
        if any((u.get("context") or "").startswith("인쇄 쪽수 판독값") for u in m.get("uncertain", [])):
            continue
        m.setdefault("uncertain", []).append(Uncertain(region_id="", context=f"인쇄 쪽수 판독값 {pp} 이 PDF 쪽수+{off}={rec['pdf_page'] + off} 과 다름 — 원본 확인").model_dump())
        work.save_record(rec["pdf_page"], rec); work.set_status(rec["pdf_page"], "판독 확인 필요"); n += 1
    return n


def resolve_mode(args, doc, work: "Work | None" = None) -> str:
    """--mode auto 면 결과 폴더에 적힌 모드를 쓰고, 없으면 글자층으로 판정해 적어 둔다. 명시한 모드는 그대로 쓰되 역시 적어 둔다.
    이렇게 해 두면 다시 계산·3차 판별·책갈피 OCR 이 같은 모드를 저절로 따른다."""
    mode = getattr(args, "mode", None)
    st = work.load_state() if work else {}
    if mode not in ("vertical", "horizontal_zh"):
        if st.get("mode") in ("vertical", "horizontal_zh"):
            mode = st["mode"]; why = "결과 폴더에 적힌 값"
        else:
            mode, why = detect_mode(doc)
        log(f"읽기 방향: {'가로쓰기 한문' if mode == 'horizontal_zh' else '세로쓰기 한자·한글'} (자동 — {why})")
    args.mode = mode
    if work and st.get("mode") != mode:
        st["mode"] = mode; work.save_state(st)
    apply_mode(args)
    return mode


STALE_MOVE = 0.02        # 이전 기록의 영역 좌표가 이보다 밀려 있으면 2차 조각이 이웃 글을 읽었을 수 있어 다시 판독한다


def refresh_stale_records(work: "Work", doc, pages: list[int]) -> int:
    """다른 판에서 만든 판독 기록을 지금 규칙으로 다시 비교·표시한다(모델 호출 없음). '프로그램을 바꾼 뒤 비교 다시 계산' 을 저절로.
    영역 좌표를 글자층 줄 위치에 맞춰 보고(0.6.1 좌표 보정), 크게 밀려 있던 기록은 readings/stale/ 로 옮겨 다시 판독되게 한다."""
    from . import prompts
    n = 0; backed: list[Path] = []; stale: list[tuple[int, float]] = []
    for rec in work.records(pages):
        if rec.get("version") == __version__ or not rec.get("pass1"):
            continue
        pg = rec["pdf_page"]
        layer = _load_layer(work, doc, pg)
        horizontal = (rec.get("mode") or prompts.MODE) == "horizontal_zh"
        p1 = PageReading.model_validate(rec["pass1"])
        fix = refit_regions(None, p1, horizontal, layer)
        if fix["fixed"] and fix["max_shift"] > STALE_MOVE and rec.get("pass2"):
            sd = work.readings_dir / "stale"; sd.mkdir(exist_ok=True)
            work.record_path(pg).replace(sd / work.record_path(pg).name); stale.append((pg, fix["max_shift"]))
            continue
        bk = work.backup_record(pg)                        # 다시 비교하면 merged 의 손 고침이 사라진다 — 덮어쓰기 전 prev/ 에 남긴다(앞의 사본은 덮지 않음)
        if bk:
            backed.append(bk)
        rec["pass1"] = p1.model_dump(); rec["bbox_fix"] = fix
        rec = remerge_record(rec, layer.get("plain", "")); rec["version"] = __version__
        work.save_record(pg, rec); n += 1
    if n:
        log(f"이전 판의 판독 기록 {n}쪽을 지금 규칙으로 다시 비교했습니다(모델 호출 없음). 손으로 고친 merged 가 있었다면 readings/prev/ 에 남아 있습니다"
            + (f"(이번에 {len(backed)}쪽 복사: {', '.join(b.name for b in backed[:4])}{' …' if len(backed) > 4 else ''})." if backed
               else "(같은 내용의 사본이 이미 있음)."))
    if stale:
        log(f"이전 기록 {len(stale)}쪽({', '.join(str(p) for p, _ in stale[:8])}{' …' if len(stale) > 8 else ''})은 영역 좌표가 최대 {max(m for _, m in stale):.1%} 밀려 있어"
            f"(2차 조각이 이웃 글을 읽었을 수 있음) 다시 판독합니다 — 이전 기록은 readings/stale/ 에 두었습니다.")
    return n


def make_reader(args, doc, work):
    resolve_mode(args, doc, work)
    if args.reader == "mock":
        from .reader_mock import MockReader
        tls = {}
        with DOC_LOCK:                                     # 미리 준비 스레드가 같은 문서를 쓰고 있을 수 있다
            for n in range(1, len(doc) + 1):
                tls[n] = text_layer(doc, n)
        return MockReader(tls)
    from .reader_gemini import GeminiReader
    think = getattr(args, "page_thinking", "auto") or "auto"; mres = getattr(args, "page_media_resolution", "auto") or "auto"
    # 1차(쪽 전체) 측정용 손잡이: auto 는 지금까지와 같다(모델 기본 생각·해상도 설정 없음). ultra_high 는 조각(Part)마다 붙는다(image_part)
    return GeminiReader(model=args.model, thinking_budget=(args.thinking_budget if think == "auto" else None), use_cache=not getattr(args, "no_cache", False),
                        thinking=None if think == "auto" else think, media_resolution=None if mres == "auto" else mres)


@_page_cache_io
def _load_page_image(work: Work, doc, n: int, dpi: int) -> tuple[Image.Image, str]:
    """쪽 이미지(없으면 만들어 두고). 바로 다 읽어 파일을 닫는다 — 미리 준비 스레드가 뒤에 같은 파일을 바꿔 넣어도(Windows) 걸리지 않게."""
    ip = work.page_image_path(n)
    if ip.exists():
        src_p = work.pages_dir / f"p{n:03d}.src.txt"
        try:
            img = Image.open(ip)
            try:
                img.load()
            except BaseException:
                img.close(); raise                     # 열린 파일을 닫아야 아래에서 바꿔 넣을 수 있다(Windows)
        except (OSError, ValueError) as e:             # 쓰다 만 PNG(0.6.7 이하 결과 폴더·정전) — '기록 있는 쪽도 다시 판독' 은 쪽 파일을 다시 꺼내지 않으므로 여기서 고친다
            log(f"주의: 쪽 이미지 pages/{ip.name} 이(가) 깨져 있어({str(e)[:60]}) PDF 에서 다시 꺼냅니다.")
        else:
            return img, (src_p.read_text(encoding="utf-8") if src_p.exists() else "작업 폴더")
    with DOC_LOCK:
        img, src = page_image(doc, n, dpi=dpi)
    _save_png_atomic(img, ip)
    write_atomic(work.pages_dir / f"p{n:03d}.src.txt", src)
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


def remerge_record(rec: dict, layer_plain: str, layer: dict | None = None) -> dict:
    """저장된 1차·2차 판독으로 비교·표시를 다시 계산한다(모델 호출 없음). merged 에 손으로 고친 내용은 사라진다.
    layer(글자층 사전)를 주면 아직 좌표 보정을 안 한 기록의 영역 좌표를 글자층 줄 위치에 맞춘다."""
    p1 = PageReading.model_validate(rec["pass1"])
    if layer and not rec.get("bbox_fix"):
        from . import prompts
        fix = refit_regions(None, p1, (rec.get("mode") or prompts.MODE) == "horizontal_zh", layer)
        if fix["fixed"]:
            rec["pass1"] = p1.model_dump(); rec["bbox_fix"] = fix
    rereads = {k: RegionReread.model_validate(v) for k, v in (rec.get("pass2") or {}).items()}
    merged, mstats = merge_passes(p1, rereads)
    fill_page_marks(merged)
    from .adjudicate import SUSPECT_NOTE, suspect_notices
    keep = [u for u in PageReading.model_validate(rec["merged"]).uncertain
            if u.context.startswith(("자동 감지한 큰 도형", "인쇄 쪽수 판독값", "2차 판독 실패(", SUSPECT_NOTE))]      # 도판·쪽수 교차 확인, 2차 실패 항목(pass2 에 그 영역이 여전히 없다), 합의 의심(본문을 바꾸지 않은 자형 판별)은 유지
    p2 = rec.get("pass2") or {}; pf = rec.get("pass2_failed") or []     # 2차 실패 영역: [id] 또는 {id: 사유}. 조각 일부만 실패한 영역은 pass2[id]["pass2_failed"]
    pf = pf if isinstance(pf, dict) else dict.fromkeys(pf, "")
    for rid in dict.fromkeys([*pf, *(k for k, v in p2.items() if isinstance(v, dict) and v.get("pass2_failed"))]):
        if not any(u.region_id == rid and u.context.startswith("2차 판독 실패(") for u in keep):      # 항목이 사라진 기록도 다시 만든다 — '판독 확인 필요' 가 남게
            why = pf.get(rid) or (p2.get(rid) or {}).get("pass2_failed") or "사유 기록 없음"
            keep.append(Uncertain(region_id=rid, context=f"2차 판독 실패({why[:60]}) — 이 영역은 1차 판독만으로 들어감, 원본 확인"))
    keep.extend(u for u in suspect_notices(rec) if u not in keep)       # 합의 의심(자형이 글자층 쪽)은 기록(rec['suspects'])에서 다시 만든다 — 항목이 사라진 기록도
    merged.uncertain.extend(u for u in keep if u not in merged.uncertain)
    if rec.get("adjudications"):
        from .adjudicate import apply_adjudications
        mstats["adjudicated"] = apply_adjudications(merged, rec["adjudications"], stats=mstats)     # 3차 판별로 확정한 글자는 다시 계산해도 남긴다(자리를 못 찾으면 unmapped)
    rec["merged"] = merged.model_dump(); rec["merge_stats"] = mstats
    rec["textlayer_diff"] = compare_textlayer(merged, layer_plain)
    rec["verify"] = verify_page(merged)
    rec["remerged_at"] = time.strftime("%Y-%m-%d %H:%M:%S"); rec["version"] = __version__
    return rec


@_document_command
def cmd_remerge(args, work: Work | None = None, doc=None):
    doc = doc or open_pdf(args.pdf)
    work = work or Work(args.pdf, args.work)
    work.ensure_source()
    resolve_mode(args, doc, work)                      # 가로쓰기 한문이면 간체·부호 차이를 접어 비교
    records = work.records(parse_pages(args.pages, len(doc)))
    if not records:
        log(f"오류: 판독 기록이 없습니다 — 결과 폴더 {work.dir} 의 readings/ 가 비어 있습니다. 기록이 있는 원본 PDF 를 고르십시오."); return 2
    for k, rec in enumerate(records, 1):
        n = rec["pdf_page"]
        progress("remerge", k - 1, len(records))
        if not rec.get("pass1"):
            log(f"  {n}쪽: 1차 판독 원본이 없어 건너뜀"); continue
        before = len(rec["merged"].get("uncertain", []))
        layer_d = _load_layer(work, doc, n)
        rec = remerge_record(rec, layer_d.get("plain", ""), layer_d)
        work.save_record(n, rec)
        m = rec["merged"]; ms = rec["merge_stats"]
        if m["uncertain"] or any(i["level"] == "error" for i in rec["verify"]):
            work.set_status(n, "판독 확인 필요")
        else:
            st = work.load_state(); p = st["pages"].get(str(n))
            if p and "판독 확인 필요" in p["statuses"]:
                p["statuses"].remove("판독 확인 필요"); work.save_state(st)
        log(f"  {n}쪽: 확인 필요 {before} → {len(m['uncertain'])}건 · 열 비교 {ms.get('columns_compared')}열 중 {ms.get('columns_differ')}열 불일치"
            + (f" · 1차에만 {ms.get('columns_only_pass1')}열, 2차에만 {ms.get('columns_only_pass2')}열" if ms.get('columns_only_pass1') or ms.get('columns_only_pass2') else "")
            + (f" · 3차 확정 위치 못 찾음 {ms['unmapped']}(표시 그대로)" if ms.get("unmapped") else ""))
    progress("remerge", len(records), len(records))
    log("remerge: 끝. 문서에 넣으려면 ④ 문서 만들기 또는 [▶ 한 번에 인식] 을 실행하십시오.")
    return 0


def orphan_message(b, horizontal: bool = False) -> str:
    """영역 밖 글줄 흔적을 사람이 읽는 말로. 좌표는 검수 화면이 상자를 그릴 때 다시 읽는다(가로 a~b%, 세로 c~d%)."""
    hx, hy = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2
    hpos = "왼쪽" if hx < 0.33 else ("가운데" if hx < 0.67 else "오른쪽")
    vpos = "위" if hy < 0.33 else ("중간" if hy < 0.67 else "아래")
    edge = " 쪽 가장자리라 머리글·꼬리글·쪽번호일 가능성이 큼." if horizontal and (b[3] < 0.13 or b[1] > 0.9) else ""
    return (f"영역 밖 글줄 흔적 — 쪽 {hpos} {vpos}(가로 {b[0] * 100:.0f}~{b[2] * 100:.0f}%, 세로 {b[1] * 100:.0f}~{b[3] * 100:.0f}%)에 "
            f"모델이 어느 영역에도 넣지 않은 {'가로' if horizontal else '세로'} 글줄이 있음.{edge} 빠진 {'줄' if horizontal else '열'}·주석·쪽번호인지 원본 확인(검수 화면의 붉은 점선 상자)")


_EDGE_RE = __import__("re").compile(r"세로 (\d+)~(\d+)%")


def absorb_edge_lines(p1: PageReading, layer: dict | None) -> list[str]:
    """가로쓰기에서 쪽 가장자리의 '영역 밖 글줄'(머리글·꼬리글·쪽번호)을 글자층 글로 확인해 확인 항목 대신 쪽 표지(page_label·running_header)로 삼는다.
    책 전체를 돌릴 때 쪽마다 2건씩 쌓이던 잡음을 없앤다. 글자층에 그 자리 글이 없으면(선·그림) 항목은 그대로 둔다."""
    if not layer or not layer.get("horizontal_lines"):
        return []
    H = float(layer.get("page_height") or 0)
    if not H:
        return []
    notes = []; keep = []
    for u in p1.uncertain:
        ctx = u.context or ""
        m = _EDGE_RE.search(ctx)
        if not (ctx.startswith("영역 밖 글줄 흔적") and "가장자리" in ctx and m):
            keep.append(u); continue
        y0, y1 = int(m.group(1)) / 100 - 0.01, int(m.group(2)) / 100 + 0.01
        texts = [(l["bbox"][0], (l.get("text") or "").strip()) for l in layer["horizontal_lines"] if y0 <= (l["bbox"][1] + l["bbox"][3]) / 2 / H <= y1]
        text = " ".join(t for _, t in sorted(texts) if t)
        if not text or len(text) > 40:
            keep.append(u); continue
        digits = __import__("re").search(r"(?<!\d)\d{1,4}(?!\d)", text)
        if digits and not p1.print_page:
            p1.print_page = digits.group(0); p1.page_label = p1.page_label or text
        rest = __import__("re").sub(r"[-–—ー一·\s]*\d{1,4}[-–—ー一·\s]*", " ", text).strip() if digits else text
        if rest and not p1.running_header:
            p1.running_header = rest
        notes.append(f"쪽 가장자리 글줄을 머리글·쪽번호로 봄(글자층): 「{text}」")
    p1.uncertain = keep
    return notes


def cover_check(img: Image.Image, p1: PageReading, horizontal: bool = False) -> list[str]:
    """영역 밖에 남은 글 기둥(가로쓰기면 글줄)을 찾아 가까운 문자 영역의 좌표를 넓히거나(2차 조각에 포함되게), 붙일 곳이 없으면 확인 항목으로 남긴다.
    가로쓰기는 줄 단위로 찾아야 머리글 한 줄이 글자 수만큼의 항목으로 쪼개지지 않는다(실측: 머리글 하나가 확인 항목 20건)."""
    from .schema import bbox_frac as _bf, frac_bbox as _fb
    axis = "y" if horizontal else "x"
    all_fr = [_bf(r.bbox) for r in p1.regions]
    boxes = uncovered_ink_boxes(img, all_fr, axis=axis)
    if not boxes:
        return []
    # 도판이 아닌 모든 영역(본문·제목·소주·쪽번호…)에 붙일 수 있다. 제목 글자가 크면 모델 좌표 밖으로 획이 삐져나오기 쉽다.
    targets = [(r.id, _bf(r.bbox)) for r in p1.regions if r.kind not in IMAGE_KINDS]
    if horizontal:
        # 가로쓰기: 쪽 위·아래 가장자리의 줄(머리글·꼬리글·쪽번호)은 본문 영역에 붙이지 않는다 — 붙이면 2차 조각이 머리글을 본문 줄로 읽는다
        edge = [b for b in boxes if b[3] < 0.12 or b[1] > 0.9]
        grown, orphans = attach_uncovered([b for b in boxes if b not in edge], targets, max_gap=0.03, axis=axis)
        orphans = edge + orphans
    else:
        grown, orphans = attach_uncovered(boxes, targets, axis=axis)
    notes = []
    word = "글줄" if horizontal else "글 기둥"
    for r in p1.regions:
        if r.id in grown:
            r.bbox = _fb(grown[r.id])
            notes.append(f"{r.id}: 영역 밖 {word}을 포함하도록 좌표를 넓힘 → {r.bbox}")
    for b in orphans:
        p1.uncertain.append(Uncertain(region_id="", context=orphan_message(b, horizontal)))
        notes.append(f"영역 밖 {word} {'y' if horizontal else 'x'} {(b[1] if horizontal else b[0]):.2f}~{(b[3] if horizontal else b[2]):.2f} → 확인 항목으로 남김")
    return notes


def nfc_reading(pr: PageReading) -> PageReading:
    """모델이 돌려준 모든 글을 NFC 로 정규화한다(호환 한자 → 표준 한자). 자형은 같고 부호값만 바뀐다.
    자형이 다른 호환 한자(塚 U+FA10 등 U+FA0E~FAFF)는 merge.nfc_text 가 그대로 둔다(지침 21: 이체자를 바꾸지 않는다)."""
    from .merge import INVISIBLE, nfc_text
    f = lambda t: INVISIBLE.sub("", nfc_text(t or ""))
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


def _zoom(w: int, h: int, target_h: int = 1600, longest: int = 3000) -> float:
    """조각 확대 배율. 세로 target_h 안팎으로 키우되 긴 변이 longest 를 넘지 않게(모델은 3072px 안으로 줄이거나 토큰 예산이 고정이라 그 이상은 낭비)."""
    return max(1.0, min(4.0, target_h / max(1, h), longest / max(1, w, h)))


def _contained(part: str, whole: str) -> float:
    """part 의 글자 가운데 whole 에 순서대로 들어 있는 비율(0~1)."""
    if not part or not whole:
        return 0.0
    from difflib import SequenceMatcher
    sm = SequenceMatcher(None, part, whole, autojunk=False)
    return sum(b.size for b in sm.get_matching_blocks() if b.size >= 2) / len(part)      # 한 글자짜리 우연한 일치(之·故·阳…)는 세지 않는다


def _misassigned(rereads: dict[str, RegionReread], p1: PageReading) -> list[str]:
    """묶음 2차 응답 가운데 제 영역의 1차 글과는 거의 안 맞고 다른 영역의 1차 글과는 잘 맞는 것(모델이 조각 번호를 섞음)을 찾는다."""
    from .merge import _fold
    from .schema import region_text
    refs = {r.id: _fold(region_text(r)) for r in p1.regions if r.kind in TEXT_KINDS}
    bad = []
    for rid, rr in rereads.items():
        base = rid.split("#")[0]
        got = _fold("".join(c.text or "" for c in rr.columns))[:80]
        if len(got) < 6 or base not in refs:
            continue
        own = _contained(got, refs[base])
        if own >= 0.35:
            continue
        other = max((_contained(got, t) for k, t in refs.items() if k != base), default=0.0)
        if other >= 0.6:
            bad.append(rid)
    return bad


CHUNK_OWN = 0.35       # 조각 응답이 제 영역 1차 글과 이만큼은 맞아야 1차 안 위치를 잰다(_misassigned 와 같은 문턱). 겹친 두 응답의 글이 서로 닮았다고 볼 문턱도 이것
CHUNK_OVERLAP = 0.3    # 두 조각 응답이 1차의 같은 자리를 짧은 쪽 길이의 이만큼 넘게 겹쳐 가리키고 글도 닮으면 한 조각을 두 번 읽은 응답으로 본다
CHUNK_ORDER_NOTE = "1차 판독의 글 차례가 2차 조각(그림) 차례와 다름"   # 낱개로 읽은 조각끼리도 차례가 1차 글과 다를 때의 확인 항목 머리


def _flat(s: str) -> str:
    """조각 위치를 잴 때 쓰는 글: 표시는 알맹이로(〔a/b〕→a), 공백 없이, 부호·이체자 접기(글자 수는 접기 전과 같다)."""
    from .merge import _fold, strip_marks_for_compare
    return _fold(re.sub(r"\s+", "", strip_marks_for_compare(s or "")))


def _locate(part: str, whole: str) -> tuple[float, int]:
    """part 가 whole 안 어디에 있는지 — (들어 있는 비율(_contained 와 같은 셈), part 첫 글자가 놓일 whole 안 위치).
    위치는 두 글자 이상 맞는 묶음마다의 어긋남(whole 위치 − part 위치)을 글자 수로 무게 둔 가운데값 — 끝의 우연한 일치에 끌려가지 않는다."""
    from difflib import SequenceMatcher
    bl = [b for b in SequenceMatcher(None, part, whole, autojunk=False).get_matching_blocks() if b.size >= 2] if part and whole else []
    offs = sorted((b.b - b.a, b.size) for b in bl); tot = sum(k for _, k in offs); acc = 0
    for off, k in offs:
        acc += k
        if 2 * acc >= tot:
            return tot / len(part), off
    return 0.0, 0


def _chunk_check(ids: list[str], texts: dict[str, str], ref: str, trusted: set) -> tuple[list[str], str, list[str]]:
    """한 영역 조각 응답들을 1차 글(ref) 안 위치로 검사한다. 묶음 응답은 같은 영역 안에서 조각 번호를 바꾸거나 한 조각을 두 번 돌려줄 수 있는데,
    _misassigned 는 다른 영역의 글만 잡는다. texts: 응답이 있는 조각 → _flat 글. trusted: 낱개로 읽은 조각(제 그림의 답이 확실 — 다시 읽지 않는다).
    조각은 그림에서 차례대로 잘랐으니 차례를 1차 위치로 옮기지 않는다 — 1차의 열 차례가 틀렸을 수도 있어서, 갈린 조각은 낱개로 다시 읽어 가린다.
    돌려주는 것: (낱개로 다시 읽을 조각, 사유 겹침|순서|"", 걸린 조각). 다시 읽을 조각 없이 순서면 낱개로 읽은 조각끼리 차례가 1차와 다르다(1차 차례가 다름).
    1차 글은 기준으로만 쓰고 바꾸지 않는다."""
    loc: dict[str, tuple[int, int]] = {}; floor = 0
    for c in ids:
        t = texts.get(c, "")
        if len(t) >= 6:
            own, at = _locate(t, ref[floor:]); at += floor           # 앞 조각 뒤에서 먼저 찾는다 — 되풀이되는 글이 앞의 같은 글에 붙지 않게
            if own < CHUNK_OWN:
                own, at = _locate(t, ref)
            if own >= CHUNK_OWN:
                loc[c] = (at, at + len(t)); floor = max(0, at + 1)
    seq = [c for c in ids if c in loc]                        # 조각 번호 순
    if len(seq) < 2:
        return [], "", []
    dup: set[str] = set()
    for i, a in enumerate(seq):
        for b in seq[i + 1:]:
            (s1, e1), (s2, e2) = loc[a], loc[b]
            if (min(e1, e2) - max(s1, s2) > CHUNK_OVERLAP * min(e1 - s1, e2 - s2)
                    and max(_contained(texts[a], texts[b]), _contained(texts[b], texts[a])) >= CHUNK_OWN):   # 글도 닮아야 — 1차가 빠뜨린 글로 자리만 겹쳐 보이는 것은 아니다
                dup |= {a, b}
    if dup - trusted:                                          # 같은 자리를 두 조각이 — 어느 쪽이 맞는지 모르니 둘 다(낱개로 읽은 쪽은 빼고)
        return [c for c in seq if c in dup - trusted], "겹침", [c for c in seq if c in dup]
    odd = [c for i, c in enumerate(seq) if any(loc[a][0] > loc[c][0] for a in seq[:i]) or any(loc[c][0] > loc[b][0] for b in seq[i + 1:])]
    if not odd:
        return [], "", []
    return [c for c in odd if c not in trusted], "순서", odd     # 차례가 갈린 쌍은 두 조각 모두 — 묶음이 번호를 섞었는지 1차 차례가 다른지는 낱개 판독으로만 안다


def _batches(items: list, per: int) -> list[list]:
    """⌈n/per⌉ 묶음으로 거의 고르게 나눈다(9장·8장씩 → 5+4, 17장 → 6+6+5). 8장 묶음 뒤에 1장이 남는 식의 치우침을 없앤다."""
    n = len(items); per = max(1, int(per))
    if not n:
        return []
    k = -(-n // per); base, extra = divmod(n, k)
    out = []; i = 0
    for j in range(k):
        size = base + (1 if j < extra else 0)
        out.append(items[i:i + size]); i += size
    return out


def _reread_batch(reader2, batch: list, n: int, rereads: dict) -> None:
    """묶음 2차 호출. 일시 오류면 조각이 셋 이상일 때 두 반으로 한 번 더 묻고, 그래도 빠진 조각만 낱개 호출로 간다(8장 × 5번 재시도로 번지지 않게)."""
    try:
        rereads.update(reader2.reread_regions(batch, n)); return
    except Exception as e:
        if isinstance(e, ReaderError) and (e.fatal or e.kind == "infrastructure"):
            raise
        if len(batch) <= 2:
            log(f"    {n}쪽: 묶음 2차 판독 실패({str(e)[:60]}) → 영역별로 다시"); return
        log(f"    {n}쪽: 묶음 2차 판독 실패({str(e)[:60]}) → 반으로 나눠 다시")
    h = (len(batch) + 1) // 2
    for half in (batch[:h], batch[h:]):
        if len(half) < 2:
            continue                                                      # 한 장은 낱개 호출로
        try:
            rereads.update(reader2.reread_regions(half, n))
        except Exception as e:
            if isinstance(e, ReaderError) and (e.fatal or e.kind == "infrastructure"):
                raise
            log(f"    {n}쪽: 반 묶음({len(half)}장) 2차 판독 실패({str(e)[:60]}) → 빠진 영역은 낱개로")


def _second_pass(reader2, img, p1: PageReading, n: int, args) -> tuple[dict[str, RegionReread], dict]:
    """문자 영역마다 확대 조각을 만들어 2차 독립 판독. 기본은 조각들을 한 호출에 묶어 보내고(빠르고 쌈), 빠진 영역은 낱개로 다시 묻는다.

    0.6.0: 조각당 글자 수를 줄여 글자당 픽셀을 늘린다.
    - 가로쓰기 모드: 좌우로만 넓히고 위·아래는 글줄 사이 공백에 맞춘다(이웃 줄을 끌어들여 '2차에만 있음' 잡음이 생기던 과확장 방지).
    - 글줄(세로쓰기면 열)이 --chunk-lines 보다 많으면 그만큼씩 나눠 보내고, 돌아온 열을 이어 붙여 영역 하나로 되돌린다.
    0.6.8: 이어 붙이기 전에 조각 응답을 1차 글 안 위치로 검사한다(_chunk_check — 겹쳤거나 차례가 갈린 묶음 응답은 낱개로 다시 읽어 가리고,
    낱개로 읽어도 차례가 다르면 1차 차례를 확인 항목으로). 조각은 늘 그림 차례대로 — 1차 위치로 옮기지 않는다."""
    from . import prompts
    from .merge import nfc_text
    horizontal = prompts.MODE == "horizontal_zh"
    max_bands = max(0, int(getattr(args, "chunk_lines", 8) or 0))
    items: list[tuple[str, str, Image.Image]] = []
    meta: dict[str, dict] = {}
    chunks_of: dict[str, list[str]] = {}
    for reg in p1.regions:
        if reg.kind not in TEXT_KINDS:
            continue
        if horizontal:
            fit = expand_until_clean(img, bbox_frac(reg.bbox), pad=0.0, sides=("left", "right"))
            fr = snap_to_gaps(img, fit["bbox"], axis="y")
            crop = crop_frac(img, fr, pad=0.002); axis = "y"; reverse = False
        else:
            fit = expand_until_clean(img, bbox_frac(reg.bbox), pad=0.012)   # 가장자리 글자가 잘리지 않게 넓힌 뒤 자른다
            fr = fit["bbox"]
            crop = crop_frac(img, fr, pad=0.012); axis = "x"; reverse = True   # 세로쓰기: 오른쪽 열부터 묶는다
        bands = split_bands(crop, axis=axis, max_bands=max_bands, reverse=reverse) if max_bands else [(0, crop.height if axis == "y" else crop.width)]
        meta[reg.id] = {"crop_bbox": list(fr), "crop_steps": fit["steps"], "crop_touching": fit["touching"], "chunks": len(bands)}
        if len(bands) == 1:
            items.append((reg.id, reg.kind, upscale(crop, _zoom(crop.width, crop.height))))
            continue
        if reverse:
            bands = bands[::-1]                                       # 세로쓰기는 오른쪽 구간부터 읽기 순서
        ids = []
        for k, (a, b) in enumerate(bands, 1):
            sub = crop.crop((0, a, crop.width, b)) if axis == "y" else crop.crop((a, 0, b, crop.height))
            cid = f"{reg.id}#{k}"; ids.append(cid)
            items.append((cid, reg.kind, upscale(sub, _zoom(sub.width, sub.height))))
        chunks_of[reg.id] = ids
    rereads: dict[str, RegionReread] = {}
    if not items:
        return rereads, meta
    batched = args.second_mode == "batch" and len(items) > 1 and hasattr(reader2, "reread_regions")
    if batched:
        per = max(1, int(getattr(args, "batch_crops", 8) or 8))         # 한 호출에 조각 N장까지 — 많이 넣을수록 모델이 조각과 번호를 섞는다
        for batch in _batches(items, per):
            if len(batch) == 1:
                continue                                                  # 한 장짜리 묶음은 아래 낱개 호출로
            _reread_batch(reader2, batch, n, rereads)
        bad = _misassigned(rereads, p1)
        for rid in bad:
            rereads.pop(rid, None)
        if bad:
            log(f"    {n}쪽: 묶음 응답 {len(bad)}개({', '.join(bad[:5])})가 다른 영역의 글로 보여 낱개로 다시 읽음")
    single: set[str] = set()                                          # 낱개로 읽은 조각 — 제 그림의 답이 확실하다
    asked: set[str] = set()                                           # 낱개로 물은 조각 — 쪽마다 조각당 한 번까지

    def one(rid: str, kind: str, im) -> str:
        """낱개 2차 호출. 일시 오류면 그 사유를 돌려준다(기록은 부르는 쪽이 — failed)."""
        if CANCEL.is_set():
            raise ReaderError("중지 요청으로 2차 판독을 멈춥니다", kind="cancelled")
        asked.add(rid)
        try:
            rr = reader2.reread_region(im, kind, rid, n)
        except ReaderError as e:
            if e.fatal or e.kind == "infrastructure":
                raise
            return str(e)[:80] or type(e).__name__
        if rr.columns and any(c.text.strip() for c in rr.columns):
            rereads[rid] = rr; single.add(rid)
            return ""
        if getattr(reader2, "allow_empty_reread", False):
            return ""                         # 모의 판독의 생략은 일치한 2차 증거로 저장하지 않는다
        return "빈 2차 판독 응답 — 문자 영역을 확인하지 못함"

    def failed(rid: str, why: str) -> None:
        # 조각 하나의 일시 오류로 쪽 전체(1차 pro 결과까지)를 버리지 않는다 — 그 영역만 2차 없이 두고 기록에 남긴다
        meta.setdefault(rid.split("#")[0], {})["pass2_failed"] = why
        log(f"    {n}쪽: {rid} 2차 판독 실패({why[:60]}) → 이 영역은 1차만으로")

    for rid, kind, im in items:
        if rid in rereads and any(c.text.strip() for c in rereads[rid].columns):
            continue
        rereads.pop(rid, None)                 # 빈 묶음 응답은 낱개로 한 번 더 확인한다
        if err := one(rid, kind, im):
            failed(rid, err)
    odd_of: dict[str, list[str]] = {}
    if chunks_of:
        # 묶음 응답은 같은 영역 안에서 조각 번호를 바꾸거나 한 조각을 두 번 돌려줄 수 있다 — 이어 붙이기 전에 1차 글 안 위치로 검사한다.
        # 조각은 그림 차례 그대로 둔다(1차 위치 순으로 옮기면 1차의 열 차례 오류가 2차에 옮아 가려진다). 겹쳐 읽었거나 차례가 1차 글과 갈린 조각은
        # 낱개로 다시 읽어 가리고(조각마다 한 번까지), 낱개로 읽어도 차례가 다르면 1차 차례를 확인 항목으로 남긴다. 1차 글은 바꾸지 않는다.
        from .schema import region_text
        refs = {r.id: _flat(region_text(r)) for r in p1.regions if r.id in chunks_of}
        by_id = {rid: (kind, im) for rid, kind, im in items}
        flat = lambda ids: {c: _flat("".join(x.text or "" for x in sorted(rereads[c].columns, key=lambda x: x.col_from_right)))
                            for c in ids if c in rereads and rereads[c].columns}
        held: dict[str, tuple[RegionReread, str]] = {}               # 낱개 재판독이 실패한(빈 응답 포함) 조각의 묶음 응답 — 끝에 다시 검사해 통과하면 쓴다
        by_order: dict[str, list[str]] = {}                           # 차례 때문에 낱개로 다시 물은 조각
        for _ in range(len(items) + 1):
            redo: list[str] = []
            for base, ids in chunks_of.items():
                again, why, _ = _chunk_check(ids, flat(ids), refs.get(base, ""), single)
                again = [c for c in again if c not in asked]
                if again:
                    nums = "·".join(c.rsplit("#", 1)[-1] for c in again)
                    meta[base].setdefault("chunk_fix", []).append(f"{why} {nums} 낱개로")
                    log(f"    {n}쪽: {base} 조각 {nums} " + ("응답이 1차의 같은 자리를 겹쳐 읽음 → 낱개로 다시 읽음" if why == "겹침" else "응답 차례가 1차 글과 다름 → 낱개로 다시 읽어 가림"))
                    redo += again
                    if why == "순서":
                        by_order.setdefault(base, []).extend(again)
            if not redo:
                break
            for cid in redo:
                prev = rereads.pop(cid); err = one(cid, *by_id[cid])
                if cid not in single:
                    held[cid] = (prev, err)                          # 갈아 넣을 낱개 응답이 없다 — 묶음 응답을 바로 버리지 않는다
        for base, ids in chunks_of.items():
            back = [c for c in ids if c in held]
            while True:                                               # 되살린 묶음 응답이 다시 걸리면 버린다(짝의 낱개 판독으로 겹침이 풀린 것만 남는다)
                rereads.update({c: held[c][0] for c in back})
                again, why, odd = _chunk_check(ids, flat(ids), refs.get(base, ""), single)
                bad = [c for c in back if c in again]
                if not bad:
                    break
                for c in bad:
                    rereads.pop(c); back.remove(c)
            for c in (c for c in ids if c in held):
                if c in back:
                    log(f"    {n}쪽: {c} 낱개 2차 판독 실패({held[c][1][:60] or '빈 응답'}) → 다시 검사한 묶음 응답을 그대로 씀")
                elif held[c][1]:
                    failed(c, held[c][1])
            if why == "순서" and not again:                          # 낱개로 읽은 조각끼리도 차례가 1차 글과 다르다 — 묶음 탓이 아니라 1차의 차례가 원본과 다를 수 있다
                odd_of[base] = odd; nums = "·".join(c.rsplit("#", 1)[-1] for c in odd)
                meta[base].setdefault("chunk_fix", []).append(f"1차 차례 다름 {nums}")
                log(f"    {n}쪽: {base} 조각 {nums} 낱개로 읽어도 차례가 1차 글과 다름 → 1차 판독의 글 차례를 의심(조각은 그림 차례대로 비교, 확인 목록에)")
            elif base in by_order and all(c in single for c in by_order[base]):
                log(f"    {n}쪽: {base} 조각 차례 — 낱개 판독은 1차 글 차례와 맞음(묶음 응답이 조각을 섞었음)")
    # 조각으로 나눠 읽은 영역은 열을 순서대로 이어 붙여 영역 하나로 되돌린다(열 번호는 1부터 다시)
    for base, ids in chunks_of.items():
        cols: list = []; unc: list = []; got = 0; span: dict[str, str] = {}
        for cid in ids:
            rr = rereads.pop(cid, None)
            if not rr or not rr.columns:
                continue
            got += 1; k0 = len(cols) + 1
            for c in sorted(rr.columns, key=lambda c: c.col_from_right):
                c.col_from_right = len(cols) + 1; cols.append(c)
            for u in rr.uncertain:
                u.region_id = base; unc.append(u)
            span[cid] = f"{k0}~{len(cols)}" if len(cols) > k0 else str(k0)
        if base in odd_of:                                            # 2차 판독 기록(pass2)에 남아 [비교 다시 계산] 뒤에도 확인 목록에 있다
            odd = odd_of[base]
            unc.append(Uncertain(region_id=base, context=f"{CHUNK_ORDER_NOTE}(조각 {'·'.join(c.rsplit('#', 1)[-1] for c in odd)} = 2차 "
                                 f"{'·'.join(span[c] for c in odd if c in span)}{'줄' if horizontal else '열'}) — 본문은 1차 차례대로 들어감, 원본에서 차례 확인"))
        if cols:
            rereads[base] = RegionReread(columns=cols, uncertain=unc)
        meta[base]["chunks_read"] = got
    for rr in rereads.values():
        for c in rr.columns:
            c.text = nfc_text(c.text or "")            # 이체자 호환 한자는 그대로(merge.nfc_text)
    return rereads, meta


def page_upscale(reader, factor: float, size: tuple[int, int]) -> float:
    """쪽 전체를 모델에 보낼 때의 확대 배율. Gemini 3 계열은 이미지 토큰 예산이 media_resolution 으로 고정이라 확대해도 얻는 것 없이
    CPU·전송만 든다 → 1. 2.5 세대·mock 은 그대로(긴 변이 4200px 을 넘지 않을 때만)."""
    if re.search(r"gemini-[3-9]", getattr(reader, "model", "") or ""):
        return 1.0
    return float(factor) if max(size) * factor <= 4200 else 1.0


def partial_path(work: Work, n: int) -> Path:
    """2차 판독 도중 실패한 쪽의 1차 결과를 두는 곳(readings/ 바로 아래가 아니라 기록으로 세지 않는다)."""
    return work.readings_dir / "partial" / f"p{n:03d}.json"


PASS1_CFG0 = ["auto", "auto", None]   # 1차 설정을 적지 않은 기록·부분 기록(0.6.8 시험판)은 기본 설정으로 읽은 것으로 본다


def _pass1_cfg(args) -> list:
    """1차(쪽 전체) 판독 설정 [생각 수준, 쪽 해상도, 생각 예산(auto 일 때)] — 요청한 값(모델이 거부해 판독기가 실행 중에 끈 것과 상관없이).
    다시 쓸 1차가 지금 설정으로 읽은 것인지 가르고, 기록·부분 기록에 남겨 측정(eval)이 설정이 다른 판독을 섞지 않게 한다."""
    think = getattr(args, "page_thinking", "auto") or "auto"
    return [think, getattr(args, "page_media_resolution", "auto") or "auto", getattr(args, "thinking_budget", None) if think == "auto" else None]


def _same_pass1(d: dict, reader, args) -> bool:
    """기록·부분 기록 d 의 1차가 지금과 같은 모델·읽기 방향·1차 설정(_pass1_cfg)으로 읽은 것인지."""
    return ((d.get("model"), d.get("mode")) == (getattr(reader, "model", ""), getattr(args, "mode", ""))
            and list(d.get("pass1_cfg") or PASS1_CFG0) == _pass1_cfg(args))


def _p2_failed_ids(rec: dict) -> list[str]:
    """2차가 덜 된 영역 id: pass2_failed([id] 또는 {id: 사유})와 조각 일부만 읽힌 영역(pass2[id]['pass2_failed'])."""
    return list(dict.fromkeys([*(rec.get("pass2_failed") or []), *(k for k, v in (rec.get("pass2") or {}).items() if isinstance(v, dict) and v.get("pass2_failed"))]))


def _reusable_pass1(work: Work, n: int, reader, args, old: dict | None = None) -> dict | None:
    """다시 사지 않아도 되는 1차 판독(부분 기록 모양의 사전) 또는 None. 모델·읽기 방향·1차 설정이 같을 때만(_same_pass1).
    ① readings/partial/ — 2차 도중 실패한 쪽(같은 판일 때만). ② 2차가 일부 영역에서 실패한 채 저장된 기록 old(pass2_failed·pass1_raw)
    — 판이 달라도 쓰고, 'record' 에 그 기록을 실어 read_one 이 덜 된 영역의 2차만 다시 묻게 한다. ①이 ②를 다시 묻다 남은 것이면(같은 1차)
    ②로 하되 사용량은 ①의 것(②의 사용량에 실패한 시도까지 더한 것). --force 는 기록 있는 쪽을 처음부터 다시 읽으라는 뜻이라 ②는 쓰지 않는다."""
    pp = partial_path(work, n); part = None
    if pp.exists():
        try:
            part = json.loads(pp.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            part = None
        if not (part and part.get("version") == __version__ and _same_pass1(part, reader, args) and part.get("pass1")):
            part = None
    if getattr(args, "force", False):
        return part
    old = old if old is not None else work.load_record(n)
    if old and old.get("pass2_failed") and old.get("pass1_raw") and _same_pass1(old, reader, args) and (part is None or part["pass1"] == old["pass1_raw"]):
        return {"pass1": old["pass1_raw"], "image_source": old.get("image_source"), "upscale": old.get("upscale"),
                "usage": (part or old).get("usage") or {}, "saved_at": old.get("read_at", ""), "record": old}   # 그 기록의 사용량(1차·실패한 2차·3차)은 새 기록이 이어받는다
    return part


def _keep_finished(old: dict, merged: PageReading, p1: PageReading, redo: set) -> tuple[PageReading, list[str]]:
    """덜 된 영역(redo)의 2차만 다시 물은 쪽의 merged: 이전 기록의 merged(손 고침·3차 확정·합의 의심 항목 포함)에서 다시 물은 영역만 새 비교로 바꾼다.
    돌려주는 값은 (merged, 새 비교로 바뀐 글에 손 고침·3차 확정이 있던 영역 id). 영역 차례·id 가 1차와 달라졌으면(영역을 손으로 고침) 새 비교 전체에
    이전 3차 확정·합의 의심 항목을 다시 얹고 모든 영역을 바뀐 것으로 센다."""
    from .adjudicate import apply_adjudications, suspect_notices
    from .schema import region_text
    om = PageReading.model_validate(old["merged"]); adj = old.get("adjudications") or []
    base, _ = merge_passes(p1, {k: RegionReread.model_validate(v) for k, v in (old.get("pass2") or {}).items()})     # 이전 기록의 손대기 전 비교
    ids = [r.id for r in om.regions]
    if ids != [r.id for r in merged.regions] or ids != [r.id for r in base.regions]:
        apply_adjudications(merged, adj)
        merged.uncertain.extend(u for u in suspect_notices(old) if u not in merged.uncertain)
        return merged, sorted(set(ids))
    text = lambda r: (r.title_text, region_text(r))
    changed = sorted({o.id for o, b in zip(om.regions, base.regions) if o.id in redo and text(o) != text(b)})
    om.regions = [nr if nr.id in redo else r for r, nr in zip(om.regions, merged.regions)]
    om.uncertain = [u for u in om.uncertain if u.region_id not in redo] + [u for u in merged.uncertain if u.region_id in redo]
    apply_adjudications(om, [a for a in adj if a.get("region_id") in redo])      # 다시 물은 영역에 같은 〔a/b〕 가 다시 나왔으면 이전 3차 확정을 그대로
    return om, changed


def _usage_add(usage: dict, extra: dict) -> None:
    for k, v in (extra or {}).items():
        if k == "by_model":
            bm = usage.setdefault("by_model", {})
            for m, u in (v or {}).items():
                old = bm.get(m) or {}
                bm[m] = {kk: old.get(kk, 0) + u.get(kk, 0) for kk in set(old) | set(u)}
        elif isinstance(v, (int, float)):
            usage[k] = usage.get(k, 0) + v


def _take_usage(reader, reader2) -> dict:
    """이 쪽에서 쓴 호출·토큰(판독기마다 usage_end)을 합치고 모델별(by_model)로도 나눠 둔다 — 비용 어림에서 모델마다 제 단가로 계산.
    부분 기록에 남길 때도 같은 모양이어야 재사용한 쪽의 1차(pro) 토큰이 모델별 값에서 빠지지 않는다."""
    usage: dict = {}
    for r in {id(reader): reader, id(reader2): reader2}.values():
        if hasattr(r, "usage_end"):
            u = r.usage_end()
            _usage_add(usage, u)
            if u.get("calls"):
                _usage_add(usage, {"by_model": {getattr(r, "model", "?"): u}})      # 1차·2차가 같은 모델 이름이면 더한다(덮지 않음)
    return usage


def read_one(work: Work, doc, reader, reader2, n: int, args) -> dict:
    # 초 단위 경과 시간: 최종 기록 저장 직전까지(저장·상태 파일 쓰기는 제외). 벽시계 변경에도 흔들리지 않는다.
    t0 = time.perf_counter()
    timings = {k: 0.0 for k in ("image_load", "layer_load", "pass1", "layout", "pass2", "postprocess", "total")}
    total = len(doc)
    img, src = _load_page_image(work, doc, n, args.dpi)
    timings["image_load"] = time.perf_counter() - t0
    up = page_upscale(reader, args.upscale, img.size)
    for r in {id(reader): reader, id(reader2): reader2}.values():
        if hasattr(r, "usage_begin"): r.usage_begin()
    stage_start = time.perf_counter()
    layer = _load_layer(work, doc, n)                       # 1차 호출 전에 — 깨진 글자층 파일 때문에 산 1차를 버리지 않게
    timings["layer_load"] = time.perf_counter() - stage_start
    # 이전 실행이 2차 도중(또는 일부 영역에서) 실패해 남긴 1차 결과는 다시 사지 않는다(1차 pro 호출이 쪽 비용의 대부분)
    old = None if getattr(args, "force", False) else work.load_record(n)       # 덮어쓸 기록(2차가 덜 된 채 저장된 쪽) — 바꾸기 전에 prev/ 에 남긴다
    pp = partial_path(work, n); partial = _reusable_pass1(work, n, reader, args, old)
    keep = None if args.no_second_pass else (partial or {}).get("record")      # ② 그 기록의 1차: 2차가 된 영역은 그 기록의 2차·글을 그대로, 덜 된 영역만 다시 묻는다
    redo = set(_p2_failed_ids(keep)) if keep else set()
    if partial:
        p1 = PageReading.model_validate(partial["pass1"]); src = partial.get("image_source") or src; up = partial.get("upscale") or up
        log(f"  {n}쪽: 이전 실행의 1차 판독을 재사용(2차만 다시)")
    else:
        stage_start = time.perf_counter()
        model_img = upscale(img, up) if up != 1 else img
        p1 = nfc_reading(reader.read_page(model_img, Path(args.pdf).name, n, total))
        timings["pass1"] = time.perf_counter() - stage_start
    stage_start = time.perf_counter()
    p1_raw = p1.model_dump()                                  # 좌표 보정·덮개 검사 전의 모델 답 — 부분 기록은 이것을 남긴다(재사용 때 보정을 다시 하므로 중복되지 않게)
    from . import prompts
    horizontal = prompts.MODE == "horizontal_zh"
    # 모델의 영역 좌표가 아래로 갈수록 밀리는 일이 있어(실측 최대 7.8%) 글자층 줄·먹 띠에 맞춘 뒤에 조각을 자른다
    bbox_fix = refit_regions(img, p1, horizontal, layer)
    coverage_notes = ([refit_describe(bbox_fix)] if bbox_fix["fixed"] else []) + cover_check(img, p1, horizontal)
    if horizontal:
        coverage_notes += absorb_edge_lines(p1, layer)
    timings["layout"] = time.perf_counter() - stage_start
    rereads: dict[str, RegionReread] = {}
    meta: dict[str, dict] = {}
    if not args.no_second_pass:
        stage_start = time.perf_counter()
        try:
            sub = p1.model_copy(update={"regions": [r for r in p1.regions if r.id in redo]}) if keep else p1
            rereads, meta = _second_pass(reader2, img, sub, n, args)
            texts = [r.id for r in p1.regions if r.kind in TEXT_KINDS]
            bad = [rid for rid in texts if meta.get(rid, {}).get("pass2_failed") and rid not in rereads]
            if texts and len(bad) == len(texts):           # 문자 영역 모두 2차 실패(서버 오류 등) — 1차만인 쪽을 '완료'로 저장하지 않고 실패로 센다(1차는 아래에서 partial/ 에)
                raise ReaderError(f"2차 판독이 문자 영역 {len(texts)}개 모두에서 실패 — {meta[bad[0]]['pass2_failed'][:80]}", fatal=False)
        except Exception as e:
            timings["pass2"] = time.perf_counter() - stage_start
            stage_start = time.perf_counter()
            # 2차에서 빠져나온 오류(치명 포함): 이미 산 1차 결과를 부분 기록으로 남겨 다음 실행이 2차만 다시 하게
            usage0 = _take_usage(reader, reader2)
            if partial:
                _usage_add(usage0, partial.get("usage") or {})
            pp.parent.mkdir(exist_ok=True)
            timings["postprocess"] = time.perf_counter() - stage_start
            timings["total"] = time.perf_counter() - t0
            write_atomic(pp, json.dumps({"file": Path(args.pdf).name, "pdf_page": n, "model": getattr(reader, "model", ""), "version": __version__,
                                         "mode": getattr(args, "mode", ""), "pass1_cfg": _pass1_cfg(args), "image_source": src, "upscale": up, "pass1": p1_raw, "bbox_fix": bbox_fix,
                                         "coverage_notes": coverage_notes, "usage": usage0, "timings": timings, "pass1_reused": bool(partial),
                                         "error": f"{type(e).__name__}: {str(e)[:200]}",
                                         "saved_at": time.strftime("%Y-%m-%d %H:%M:%S")}, ensure_ascii=False, indent=1))
            log(f"  {n}쪽: 2차 판독 중 오류 — 1차 결과는 readings/partial/ 에 남겨 다음 실행에서 재사용합니다")
            raise
        timings["pass2"] = time.perf_counter() - stage_start
    stage_start = time.perf_counter()
    kept2 = {k: v for k, v in ((keep or {}).get("pass2") or {}).items() if k not in redo}       # 다시 묻지 않은 영역의 이전 2차(조각 정보 포함 그대로)
    for k, v in kept2.items():
        rereads.setdefault(k, RegionReread.model_validate(v))
    merged, mstats = merge_passes(p1, rereads)
    fill_page_marks(merged)
    for rid, m in meta.items():
        if m.get("pass2_failed"):
            merged.uncertain.append(Uncertain(region_id=rid, context=f"2차 판독 실패({m['pass2_failed'][:60]}) — 이 영역은 1차 판독만으로 들어감, 원본 확인"))
    for f in ("page_label", "print_page", "running_header"):                 # 가장자리 글줄에서 얻은 쪽 표지를 넘긴다
        if not getattr(merged, f) and getattr(p1, f):
            setattr(merged, f, getattr(p1, f))
    # 도판 교차 확인(자동 감지)
    cv = detect_figures(img)
    model_figs = [bbox_frac(r.bbox) for r in merged.regions if r.kind in IMAGE_KINDS]
    for f in cv:
        if not any(overlap_ratio(f, m) > 0.5 for m in model_figs):
            merged.uncertain.append(Uncertain(region_id="", context=f"자동 감지한 큰 도형 {tuple(round(v, 2) for v in f)} 가 도판 영역으로 분류되지 않음 — 영역 분류 확인"))
    # 인쇄 쪽수 일관성(추정으로 확정하지 않고 어긋남만 표시)
    if args.print_offset is not None and merged.print_page.isdigit() and int(merged.print_page) != n + args.print_offset:
        merged.uncertain.append(Uncertain(region_id="", context=f"인쇄 쪽수 판독값 {merged.print_page} 이 PDF 쪽수+{args.print_offset}={n + args.print_offset} 과 다름 — 원본 확인"))
    changed: list[str] = []
    if keep:                                                  # 이전 기록의 글(손 고침·3차 확정)은 다시 묻지 않은 영역에 그대로
        merged, changed = _keep_finished(keep, merged, p1, redo)
    # 기존 텍스트 레이어 대조
    tdiff = compare_textlayer(merged, layer.get("plain", ""))
    issues = verify_page(merged)
    usage = _take_usage(reader, reader2)
    if partial:
        _usage_add(usage, partial.get("usage") or {})          # 이전 실행에서 산 1차 호출도 이 쪽의 비용이다
    failed2 = [rid for rid, m in meta.items() if m.get("pass2_failed")]
    pass2 = {}
    for k, v in rereads.items():
        if k in kept2:
            pass2[k] = kept2[k]; continue
        d = v.model_dump(); d.update(meta.get(k, {})); pass2[k] = d
    rec = {"file": Path(args.pdf).name, "pdf_page": n, "reader": reader.name, "model": getattr(reader, "model", ""), "version": __version__, "mode": getattr(args, "mode", ""),
           "pass1_cfg": _pass1_cfg(args),
           "second_model": getattr(reader2, "model", "") if reader2 is not reader else "",
           "image_source": src, "upscale": up, "read_at": time.strftime("%Y-%m-%d %H:%M:%S"), "seconds": 0.0,
           "timings": timings, "pass1_reused": bool(partial),
           "usage": usage, "coverage_notes": coverage_notes, "bbox_fix": bbox_fix,
           "pass1": p1.model_dump(), "pass2": pass2,
           "merged": merged.model_dump(), "merge_stats": mstats, "textlayer_diff": tdiff, "verify": issues, "crops": {}}
    if partial:
        rec["pass1_reused_from"] = partial.get("saved_at", "")
    if keep:                                          # 3차 판별·합의 의심 기록은 그 글(이전 merged)과 함께 이어받는다 — 다시 묻지 않게
        for k in ("adjudications", "adjudicate_stats", "suspects", "suspect_stats"):
            if keep.get(k) is not None:
                rec[k] = keep[k]
    if failed2:                                       # 2차가 일부 영역에서 실패 — 쪽은 1차로 살리되 끝나지 않은 쪽으로 세고, 다음 실행은 이 1차로 2차만 다시 묻는다
        rec["pass2_failed"] = failed2; rec["pass1_raw"] = p1_raw
    if old is not None:                               # 덮어쓰기 전에 prev/ 에 — 손으로 고친 merged 를 되찾을 수 있게
        bk = work.backup_record(n)
        try:
            where = bk.relative_to(work.dir).as_posix() if bk else "readings/prev/(같은 사본이 이미 있음)"
        except ValueError:
            where = str(bk)
        if keep:
            log(f"  {n}쪽: 2차가 덜 된 영역 {', '.join(sorted(redo))} 만 다시 물음 — 다른 영역의 글(손 고침·3차 확정)은 이전 기록 그대로")
            if changed:
                log(f"주의: {n}쪽 영역 {', '.join(changed)} 의 이전 글(손 고침·3차 확정)은 2차를 다시 물어 새 비교로 바뀌었습니다 — 이전 기록은 {where}")
        else:
            log(f"주의: {n}쪽의 이전 기록을 1차부터 새로 읽은 기록으로 바꿨습니다(손 고침·3차 확정은 이어지지 않음) — 이전 기록은 {where}")
    timings["postprocess"] = time.perf_counter() - stage_start
    timings["total"] = time.perf_counter() - t0
    rec["seconds"] = round(timings["total"], 1)
    work.save_record(n, rec)
    if pp.exists():                                   # 새 기록이 부분 기록을 대신한다(2차 일부 실패면 1차는 기록의 pass1_raw 에)
        try: pp.unlink()
        except OSError: pass
    work.set_status(n, "영역 분류 완료"); work.set_status(n, "1차 OCR 완료")
    if rereads and not failed2:
        work.set_status(n, "2차 독립 판독 비교 완료")
    if merged.uncertain or any(i["level"] == "error" for i in issues):
        work.set_status(n, "판독 확인 필요", remove=["2차 독립 판독 비교 완료"] if failed2 else None)     # 2차가 덜 된 쪽에 '완료' 를 남기지 않는다(지침 33)
    return rec


@_document_command
def cmd_read(args, work: Work | None = None, doc=None, checkpoint_fn=None):
    """쪽을 여러 개 동시에 판독한다(--workers). 치명 오류(키·권한·모델·한도 0)는 전체를 멈추고, 일시 오류는 그 쪽만 건너뛴다.
    checkpoint_fn 이 있으면 --checkpoint 쪽마다, 그리고 멈출 때 호출해 중간 HWPX 를 남긴다."""
    doc = doc or open_pdf(args.pdf)
    work = work or Work(args.pdf, args.work)
    work.ensure_source(getattr(args, "dpi", 300))
    pages = parse_pages(args.pages, len(doc))
    existing = {n: work.load_record(n) for n in pages}
    if not args.force and all(existing[n] is not None and
                              (args.no_second_pass or not existing[n].get("pass2_failed")) for n in pages):
        log(f"read: {len(pages)}쪽은 이미 판독되어 모델 호출 없이 건너뜁니다.")
        progress("read", len(pages), len(pages))
        return 130 if CANCEL.is_set() else 0
    try:
        reader = make_reader(args, doc, work)
        reader2 = reader
        if args.reader == "gemini" and not args.no_second_pass:
            from .reader_gemini import GeminiReader, list_models, pick_second_model
            want = (args.second_model or "").strip()
            if want.lower() == "auto":
                try:
                    want = pick_second_model(list_models(reader.key, reader.vertexai), reader.model)
                    log(f"  2차 판독 모델 자동 선택: {want}" + ("" if want != reader.model else " (같은 세대 flash 가 없어 1차와 같은 모델)"))
                except Exception as e:
                    want = ""; log(f"  2차 모델 자동 선택 실패({str(e)[:60]}) → 1차와 같은 모델")
            think2 = "low" if args.second_thinking == "low" else None
            mres = getattr(args, "media_resolution", "auto") or "auto"
            mres = "high" if mres == "auto" else (None if mres == "default" else mres)      # auto = 2차 조각은 high(글자당 토큰을 늘린다)
            if (want and want != reader.model) or think2 or mres or getattr(reader, "thinking", None) or getattr(reader, "media_resolution", None):     # 1차 생각·해상도 설정이 2차로 새지 않게
                reader2 = GeminiReader(model=want or reader.model, thinking_budget=args.thinking_budget, use_cache=not args.no_cache, thinking=think2, media_resolution=mres)
    except Exception as e:
        log(f"오류: {e}"); return 1
    todo: list[int] = []; redo2: list[int] = []; redo1: list[int] = []
    for n in pages:
        if args.force:
            work.backup_record(n)                                       # '기록 있는 쪽도 다시 판독' — 덮어쓰기 전 readings/prev/ 에 사본(손으로 고친 merged 보존)
        old = None if args.force else work.load_record(n)
        if old is None:
            todo.append(n)
        elif old.get("pass2_failed") and not args.no_second_pass:      # 2차가 일부 영역에서 실패한 채 저장된 쪽 — 1차는 기록의 것을 쓰고 덜 된 영역의 2차만 다시
            todo.append(n)
            (redo2 if old.get("pass1_raw") and _same_pass1(old, reader, args) else redo1).append(n)     # 모델·읽기 방향·1차 설정이 바뀌었으면 1차부터(_reusable_pass1)
    skipped = len(pages) - len(todo)
    ceiling = max(1, int(args.workers or 1))
    cur = max(1, min(ceiling, int(args.workers_start or ceiling)))     # 동시 쪽 수: 여기서 시작해 한도(429)가 나면 줄이고, 안정되면 늘린다
    log(f"read: 판독기 {reader.name} {getattr(reader, 'model', '')}"
        + (f" (1차 {reader.settings_note()})" if hasattr(reader, "settings_note") else "")              # 측정 실행이 스스로 설명되게(생각 수준·쪽 해상도)
        + (f" · 2차 {reader2.model}{' (생각 줄임)' if getattr(reader2, 'thinking', None) == 'low' else ''}{' · 조각 해상도 ' + reader2.media_resolution if getattr(reader2, 'media_resolution', None) else ''}" if reader2 is not reader else "")
        + (f" · 조각 {args.chunk_lines}줄 묶음" if getattr(args, "chunk_lines", 0) else "")
        + f" · {len(pages)}쪽 중 읽을 쪽 {len(todo)}개 · 동시 {cur}쪽(최대 {ceiling}, 자동 조절) · 2차 판독 {'묶음 호출' if args.second_mode == 'batch' else '영역별 호출'}"
        + (f" · {reader.cache_note}" if getattr(reader, "cache_note", "") else ""))
    if skipped:
        log(f"  기록이 있어 건너뛴 쪽 {skipped}개 (다시 읽으려면 '기록 있는 쪽도 다시 판독' 또는 --force)")
    if redo2:
        log(f"  2차 판독이 일부 영역에서 실패했던 쪽 {len(redo2)}개({', '.join(map(str, redo2[:10]))}{' …' if len(redo2) > 10 else ''})는 1차를 다시 사지 않고 덜 된 영역의 2차만 다시 묻습니다")
    if redo1:
        log(f"  2차 판독이 일부 영역에서 실패했던 쪽 {len(redo1)}개({', '.join(map(str, redo1[:10]))}{' …' if len(redo1) > 10 else ''})는 1차의 모델·읽기 방향·설정이 지금과 달라 1차부터 다시 읽습니다")
    progress("read", 0, len(todo))
    failed: list[int] = []; incomplete: list[int] = []; fatal: Exception | None = None
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
                    progress("read", done_now + len(failed), len(todo))
                    m = rec["merged"]; u = rec.get("usage") or {}
                    if rec.get("pass2_failed"):
                        incomplete.append(n)
                    log(f"  {n}쪽: 영역 {len(m['regions'])} · 확인 필요 {len(m['uncertain'])} · 구조 검사 {len(rec['verify'])} · 레이어 일치율 {rec['textlayer_diff'].get('ratio')} · {rec['seconds']}s"
                        + (f" · 호출 {u.get('calls')}회 · 토큰 입력 {u.get('prompt_tokens', 0):,} 출력 {u.get('output_tokens', 0) + u.get('thinking_tokens', 0):,}" if u.get("calls") else "")
                        + (f" · 2차 실패 영역 {', '.join(rec['pass2_failed'])}" if rec.get("pass2_failed") else ""))
                    for line in rec.get("coverage_notes") or []:
                        log("    " + line)
                    clean_since += 1                                   # 성공한 쪽만 '깨끗한' 것으로 센다(실패를 세면 실패 중에도 동시 쪽 수가 는다)
                except ReaderError as e:
                    if e.fatal:
                        fatal = e; log(f"오류: {n}쪽 판독 실패 — {e}")
                    else:
                        failed.append(n); work.set_status(n, "판독 확인 필요", note=f"판독 실패(다시 시도 가능): {e}")
                        log(f"  {n}쪽: 판독 실패 — {str(e)[:120]} → 건너뛰고 계속")
                        progress("read", done_now + len(failed), len(todo))
                except Exception as e:
                    failed.append(n); work.set_status(n, "판독 확인 필요", note=f"판독 중 예외: {type(e).__name__}: {e}")
                    log(f"  {n}쪽: 판독 중 예외 {type(e).__name__}: {str(e)[:120]} → 건너뛰고 계속")
                    progress("read", done_now + len(failed), len(todo))
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
    if incomplete:
        log(f"오류: 2차 판독이 일부 영역에서 실패한 쪽 {len(incomplete)}개: {', '.join(map(str, sorted(incomplete)[:30]))}{'…' if len(incomplete) > 30 else ''}"
            " — 그 영역은 1차만으로 들어갔습니다(확인 항목). 다시 실행하면 이 쪽들의 2차만 다시 묻습니다(1차는 다시 사지 않음)")
    u = {"calls": 0, "prompt_tokens": 0, "output_tokens": 0, "thinking_tokens": 0, "cached_tokens": 0}
    for r in {id(reader): reader, id(reader2): reader2}.values():
        for k in u:
            u[k] += (getattr(r, "usage", None) or {}).get(k, 0)
    if u["calls"]:
        log(f"모델 호출 통계(이번 실행, 1차+2차): 호출 {u['calls']}회 · 토큰 입력 {u['prompt_tokens']:,} 출력 {u['output_tokens'] + u['thinking_tokens']:,}"
            + (f" · 캐시 적중 {u['cached_tokens']:,}" if u["cached_tokens"] else "") + f" · {time.time() - t_start:.0f}초  — 쪽별 누적은 ⑥ 보고에 있음")
    if fatal is not None:
        return 1
    if CANCEL.is_set() and done_now + len(failed) < len(todo):        # 남은 쪽이 있을 때만 '중지'(마지막 쪽들이 도는 중에 눌렸으면 다 끝난 것)
        log(f"중지 요청으로 판독을 멈춤. 지금까지의 쪽은 기록에 남아 있음. (이번에 {done_now}쪽 판독)"); return 130
    return 3 if failed or incomplete else 0                            # 3 = 끝까지 갔지만 일부 쪽 실패·2차 덜 됨(성공으로 보이지 않게)


# ---------- build ----------
CROP_FIT = 2        # 도판 자르기 방식이 바뀌면 올린다 — 예전 판으로 잘라 둔 도판을 [④ 문서 만들기] 때 다시 자른다(0.6.6: 연결 성분으로 넓힘)


def make_crops(work: Work, rec: dict, img: Image.Image) -> dict:
    """도판을 원본 이미지에서 잘라 저장한다. 같은 좌표로 이미 잘라 둔 파일이 있으면 다시 자르지 않는다(중간 저장을 자주 해도 빠르게)."""
    crops = dict(rec.get("crops") or {})
    pr = PageReading.model_validate(rec["merged"])
    out: dict = {}
    for reg in pr.regions:
        if reg.kind not in IMAGE_KINDS:
            continue
        old = crops.get(reg.id)
        if old and old.get("path") and Path(old["path"]).exists() and old.get("bbox_model") == list(bbox_frac(reg.bbox)) and old.get("fit") == CROP_FIT:
            out[reg.id] = old; continue
        grown = grow_to_components(img, bbox_frac(reg.bbox))           # 모델 좌표가 도판을 크게 자른 경우(원형 도판 반쪽 등) 도판 전체로
        fit = fit_figure_bbox(img, grown, pad=0.01)
        crop = crop_frac(img, fit["bbox"], pad=0.01)
        path = work.crops_dir / f"p{pr.pdf_page:03d}_{reg.id}.jpg"     # 스캔 도판은 JPEG 가 PNG 의 1/4~1/5 크기. 수백 장이 들어가도 문서가 가볍다
        (crop.convert("L") if crop.mode not in ("L", "RGB") else crop).save(path, "JPEG", quality=90)
        out[reg.id] = {"path": str(path), "kind": reg.kind, "bbox_model": list(bbox_frac(reg.bbox)), "bbox_used": list(fit["bbox"]),
                       "steps": fit["steps"], "touching": fit["touching"], "frac_w": round(fit["bbox"][2] - fit["bbox"][0] + 0.02, 4),
                       "ratio": round(crop.height / max(1, crop.width), 4), "size": list(crop.size),
                       "grown": grown != bbox_frac(reg.bbox), "fit": CROP_FIT}
    return out


def context_snapshots(work: Work, records: list[dict], write: bool = True) -> dict[int, dict]:
    """문맥 추정(context.analyze): 결과 폴더의 모든 판독 기록을 바탕으로 이번에 문서에 넣을 쪽들의 추정을 낸다.
    글도 판독 기록도 바꾸지 않고, write 면 <결과 폴더>/context.json 에만 남긴다. 실패해도 문서 만들기는 계속한다(밑줄만 빠짐)."""
    try:
        from .context import analyze, fold_zh_for, load_ledger_pairs, write_snapshot
        allrec = []
        for f in sorted(work.readings_dir.glob("p*.json")):
            try:
                allrec.append(json.loads(f.read_text(encoding="utf-8-sig")))      # 손으로 고치다 BOM 이 붙은 기록도
            except Exception:
                continue
        res = analyze(allrec or records, records, fold_zh=fold_zh_for(records), ledger_pairs=load_ledger_pairs(work.dir))
        if write:
            write_snapshot(work.dir, res)
        return res
    except Exception as e:
        log(f"문맥 추정을 건너뜀: {e}")
        return {}


def build_outputs(work: Work, doc, records: list[dict], args, interim: bool = False) -> bool:
    """판독 기록들로 HWPX(+DOCX)를 만든다. interim=True 면 '<이름>_진행중.hwpx' 한 파일만 조용히 만든다(중간 저장)."""
    from .hwpx_builder import build_hwpx
    steps = len(records) + 1                                    # 쪽마다 도판 자르기 + 문서 조립 한 번
    if not interim:
        progress("build", 0, steps)
    for k, rec in enumerate(records, 1):
        n = rec["pdf_page"]
        if not interim:
            progress("build", k - 1, steps)
        img, _ = _load_page_image(work, doc, n, args.dpi)
        new_crops = make_crops(work, rec, img)
        if new_crops != (rec.get("crops") or {}):
            rec["crops"] = new_crops
            if not interim:                                  # 중간 저장은 기록을 쓰지 않는다 — 다시 읽는 중인 쪽의 새 기록을 옛 기록으로 덮을 수 있다(도판 파일만 남기고 최종 문서 때 적는다)
                work.save_record(n, rec)
        if not interim and rec["crops"]:
            touching = [k for k, c in rec["crops"].items() if c["touching"]]
            if touching:
                work.set_status(n, "판독 확인 필요", note=f"도판 외곽에 글자·선이 닿음: {touching}")
            else:
                work.set_status(n, "이미지 보존 확인 완료")
    if not interim:
        progress("build", len(records), steps)                  # 쪽 다 함 → 이제 문서 조립
    whole = len(records) == len(doc)
    ctx = None
    gstats: dict = {}
    if not interim and not getattr(args, "no_context", False):
        ctx = context_snapshots(work, records)
    if interim:
        chunks = [records]
    else:
        if not whole:
            log(f"build: 판독 기록이 있는 {len(records)}쪽만 문서에 넣음 (PDF 전체 {len(doc)}쪽). 전체를 만들려면 쪽 범위를 비우고 [▶ 한 번에 인식] 을 다시 누르십시오.")
        chunks = [records[i:i + args.split] for i in range(0, len(records), args.split)] if getattr(args, "split", 0) and args.split > 0 else [records]
    ok = True
    for ci, chunk in enumerate(chunks):
        blocks = assemble_blocks(chunk, join_pages=not args.no_join_pages, whole_offset=args.whole_offset,
                                 check_list=getattr(args, "check_list", "none") or "none", grades=not interim,
                                 context=ctx, memo_mode=getattr(args, "memo", "notice") or "notice",
                                 legend=not args.no_highlight, stats=gstats)
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
                        highlight=not args.no_highlight, page_breaks=not args.no_page_breaks, memos=getattr(args, "memo", "notice") != "inline")
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
                            highlight=not args.no_highlight, page_breaks=not args.no_page_breaks, memos=getattr(args, "memo", "notice") != "inline")
            log(f"build: DOCX {out_d} · {sd}")
    if not interim and gstats:
        anyc = next(iter((ctx or {}).values()), {})
        log(f"등급: 초록 {gstats.get('green', 0)} · 파랑 {gstats.get('blue', 0)} · 문맥 밑줄 {gstats.get('red_ctx', 0)} · 쪽 고지 {gstats.get('notices', 0)}"
            + (f" · 3차 확정 위치 못 찾음 {gstats['unmapped']}" if gstats.get("unmapped") else "")
            + (f" · 3차와 다른 대목 어긋남 {gstats['conflict']}" if gstats.get("conflict") else "")
            + (f" · 문맥 바탕 {anyc.get('corpus_chars', 0):,}자/{anyc.get('corpus_pages', 0)}쪽(1차·2차 일치 글)" if ctx else " · 문맥 추정 끔")
            + " — 글은 바꾸지 않음(색·밑줄·메모만)")
    if not interim:
        for rec in records:
            work.set_status(rec["pdf_page"], "문서 반영 완료")
            if ok:
                work.set_status(rec["pdf_page"], "파일 구조 검사 완료")
        progress("build", steps, steps)
    return ok


@_document_command
def cmd_build(args, work: Work | None = None, doc=None):
    doc = doc or open_pdf(args.pdf)
    work = work or Work(args.pdf, args.work)
    work.ensure_source()
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


# ---------- adjudicate / ledger ----------
def run_adaptive(items: list, fn, readers: list, ceiling: int, start: int, on_result, on_error, pause: float = 20.0) -> bool:
    """items 를 동시 start→ceiling 스레드로 fn(item) 실행(판독·판별 공통). 한도 초과(429)가 새로 보이면 하나 줄이고 20초 쉬며,
    10개 연속 깨끗하면 하나 늘린다. CANCEL 이 켜지거나 on_error 가 True(치명)를 돌려주면 새 작업을 내지 않는다. 돌려주는 값은 멈췄는지."""
    cur = max(1, min(ceiling, start)); it = iter(items); futures: dict = {}
    def n429() -> int:
        return sum(getattr(r, "n_429", 0) for r in readers if r is not None)
    seen = n429(); clean = 0; pause_until = 0.0; exhausted = False; stop = False
    with ThreadPoolExecutor(max_workers=max(1, ceiling)) as ex:
        def submit_next() -> bool:
            nonlocal exhausted
            if stop or CANCEL.is_set() or exhausted:
                return False
            for item in it:
                futures[ex.submit(fn, item)] = item
                return True
            exhausted = True
            return False
        while True:
            while len(futures) < cur and time.time() >= pause_until and submit_next():
                pass
            if not futures:
                if not exhausted and not stop and not CANCEL.is_set() and time.time() < pause_until:
                    time.sleep(1); continue
                break
            done_set, _ = wait(list(futures), return_when=FIRST_COMPLETED, timeout=5)
            for fut in done_set:
                item = futures.pop(fut)
                try:
                    on_result(item, fut.result()); clean += 1          # 성공만 '깨끗한' 것으로 센다
                except Exception as e:
                    if on_error(item, e):
                        stop = True
            now = n429()
            if now > seen:
                seen = now; clean = 0
                if cur > 1:
                    cur -= 1; log(f"  — 한도 초과(429) 감지 → 동시 {cur}쪽으로 줄이고 {pause:.0f}초 쉼")
                else:
                    log(f"  — 한도 초과(429) 감지 → {pause:.0f}초 쉼")
                pause_until = time.time() + pause
            elif clean >= 10 and cur < ceiling:
                cur += 1; clean = 0; log(f"  — 안정적 → 동시 {cur}쪽으로 늘림")
    return stop or CANCEL.is_set()


def make_glyph_readers(args) -> tuple:
    """3차 글자 판별 판독기 (주 판독기, 보완 판독기 또는 None).
    --adjudicate-model: 비우면 1차 모델, 이름을 주면 그 모델, auto 면 같은 세대 flash 로 먼저 묻고 못 가린 자리만 1차 모델로.
    생각 수준은 기본 LOW(객관식 자형 질문이라 깊은 생각이 필요 없음), 조각 해상도 high."""
    from .reader_gemini import GeminiReader, list_models, pick_second_model
    from .prompts import glyph_system_prompt
    think = None if getattr(args, "adjudicate_thinking", "low") == "full" else "low"
    system = glyph_system_prompt()                      # 눈가림: 지침 전문(문맥 단서 포함) 대신 자형 전용 지시. 짧아서 캐시는 쓰지 않는다
    def make(model):
        return GeminiReader(model=model, thinking_budget=(getattr(args, "thinking_budget", None) if think is None else None), use_cache=False,
                            media_resolution="high", thinking=think, system=system)
    want = (getattr(args, "adjudicate_model", "") or "").strip()
    if not want:
        return make(args.model), None
    if want.lower() == "auto":
        primary = make(args.model)
        try:
            flash = pick_second_model(list_models(primary.key, primary.vertexai), primary.model)
        except Exception as e:
            log(f"  3차 판별 모델 자동 선택 실패({str(e)[:60]}) → 1차 모델만"); return primary, None
        if flash == primary.model:
            return primary, None
        return make(flash), primary
    return make(want), None


@_document_command
def cmd_adjudicate(args, work: Work | None = None, doc=None, standalone: bool = True):
    """3차 글자 판별. 〔a/b〕 자리를 조각 묶음(--batch-sites)으로 묻고, 쪽은 동시(--workers)에 돌린다. 확정한 글자는 merged 와
    adjudications 에 남고, remerge 뒤에도 유지된다. 자리 창은 글자층 글자 상자 → 먹 실측 → 균등 분할 순으로 잡는다.
    이어서 같은 쪽의 합의 판독 의심 자리(1차·2차가 같게 읽었는데 글자층과 자형 혼동 쌍 한 글자가 다름 — 〔a/b〕 가 없는 쪽도)를 같은 방식으로 묻는다
    (--no-suspects 면 안 함, 쪽당 --suspect-max 곳, 이미 물은 자리는 --force-suspects 가 아니면 다시 묻지 않음). 본문은 바꾸지 않고 확인 항목만 단다."""
    from . import prompts
    doc = doc or open_pdf(args.pdf)
    work = work or Work(args.pdf, args.work)
    work.ensure_source()
    resolve_mode(args, doc, work)
    max_sites = int(getattr(args, "max_sites", 0) or 0); zoom = int(getattr(args, "zoom", 6) or 6)
    batch = int(getattr(args, "batch_sites", 8) or 0)
    min_px = float(getattr(args, "adjudicate_min_px", MIN_GLYPH_PX) or 0); blind = bool(getattr(args, "adjudicate_blind", False))
    records = work.records(parse_pages(args.pages, len(doc)))
    if not records:
        log(f"오류: 판독 기록이 없습니다 — 결과 폴더 {work.dir} 의 readings/ 가 비어 있습니다. 기록이 있는 원본 PDF 를 고르십시오."); return 2
    from .adjudicate import find_sites, adjudicate_page, suspect_page, suspects_pending, suspect_freq, SUSPECT_MAX
    sus = not getattr(args, "no_suspects", False)
    smax = max(0, int(SUSPECT_MAX if getattr(args, "suspect_max", None) is None else args.suspect_max)) if sus else 0
    sforce = bool(getattr(args, "force_suspects", False)); freq = suspect_freq(work.dir) if smax else None
    todo = []                                                            # (기록, 〔a/b〕 자리 수, 물을 합의 의심 수(어림), 글자층 글)
    for rec in records:
        n = len(find_sites(PageReading.model_validate(rec["merged"])))
        layer = _load_layer(work, doc, rec["pdf_page"]).get("plain", "") if smax else ""
        k = suspects_pending(rec, layer, smax, sforce, freq) if layer else 0
        if n or k:
            todo.append((rec, n, k, layer))
    total_sites = sum(t[1] for t in todo); total_sus = sum(t[2] for t in todo)
    if not total_sites and not total_sus:
        log("판별할 〔a/b〕 자리가 없습니다." + (" 합의 판독 의심 자리(1차·2차가 같게 읽었는데 글자층과 자형 혼동 쌍 한 글자가 다름)도 새로 물을 곳이 없습니다." if smax else "")); return 0
    if args.reader == "mock":
        log(f"mock 판독기는 자형을 보지 못합니다. 〔a/b〕 자리 {total_sites}곳" + (f" · 합의 판독 의심 {total_sus}곳" if smax else "") + "만 셌습니다."); return 0
    from .reader_gemini import ReaderError
    from .pdfio import layer_chars, page_frame
    try:
        reader, fallback = make_glyph_readers(args)
    except Exception as e:
        log(f"오류: {e}"); return 1
    readers = [r for r in (reader, fallback) if r is not None]
    ceiling = max(1, int(getattr(args, "workers", 6) or 1)); start = max(1, min(ceiling, int(getattr(args, "workers_start", 4) or ceiling)))
    log(f"adjudicate: 판독기 {reader.model}" + (f" → 못 가리면 {fallback.model}" if fallback else "") + (" (생각 줄임)" if getattr(reader, "thinking", None) == "low" else "")
        + f" · {len(todo)}쪽 · 〔a/b〕 {total_sites}곳" + (f" · 합의 의심 {total_sus}곳(쪽당 최대 {smax})" if total_sus else "")
        + f" · 조각 확대 {zoom}배 · 한 호출에 {batch}자리" + (f" · 쪽당 최대 {max_sites}곳" if max_sites else "")
        + f" · 동시 {start}쪽(최대 {ceiling}, 자동 조절)" + " · 눈가림(앞뒤 글자를 글로 주지 않음·후보 섞음)" + (" + 후보 없이 재확인" if blind else "")
        + (f" · 글자 {min_px:g}px 미만은 묻지 않음" if min_px else ""))
    horizontal = prompts.MODE == "horizontal_zh"
    progress("adjudicate", 0, len(todo))
    pages_done = [0]
    agg = {"sites": 0, "decided": 0, "undecided": 0, "failed": 0, "misplaced": 0, "batched": 0, "fallback_decided": 0, "batch_rechecked": 0, "skipped": 0,
           "lowres": 0, "blind_checked": 0, "blind_rejected": 0, "blind_failed": 0}
    sagg = {"asked": 0, "differs": 0, "confirmed": 0, "undecided": 0, "failed": 0, "lowres": 0, "skipped": 0, "already": 0, "low_ratio": 0}
    loc_agg: dict[str, int] = {}
    fatal: list = []; t0 = time.time(); ran: set[int] = set()            # ran: 이번에 합의 의심을 본 쪽(지난 실행의 suspect_stats 를 세지 않게)
    sus_cut: list[int] = []                                              # 중지로 합의 의심을 건너뛴 쪽

    def one(item):
        rec, n, _, layer = item; pg = rec["pdf_page"]
        img, _ = _load_page_image(work, doc, pg, args.dpi)
        with DOC_LOCK:                                   # PyMuPDF 문서는 스레드 안전하지 않다
            try:
                lines = layer_chars(doc, pg); frame = page_frame(doc, pg)
            except Exception:
                lines, frame = [], None
        for r in readers:
            if hasattr(r, "usage_begin"): r.usage_begin()
        def account(rec, more: bool = False):           # 이 쪽에서 지금까지 쓴 호출·토큰을 기록에 더한다(more 면 이어서 새로 센다)
            usage = rec.get("usage") or {}; used = False
            for i, r in enumerate(readers):
                u = r.usage_end() if hasattr(r, "usage_end") else {}
                if more and hasattr(r, "usage_begin"): r.usage_begin()
                if u.get("calls"):
                    used = True
                    for k, v in u.items():
                        usage[k] = usage.get(k, 0) + v
                    key = r.model + (" (3차)" if i == 0 else " (3차 보완)")
                    bm = usage.setdefault("by_model", {}); old = bm.get(key) or {}
                    bm[key] = {k: old.get(k, 0) + u.get(k, 0) for k in set(old) | set(u)}      # 다시 실행해도 누적(합계와 같은 셈법)
            if used:
                rec["usage"] = usage
        try:
            if n:
                rec = adjudicate_page(rec, img, reader, horizontal, max_sites=max_sites, zoom=zoom, batch=batch, fallback_reader=fallback,
                                      layer_lines=lines, frame=frame, should_stop=CANCEL.is_set, min_px=min_px, blind=blind)
            if smax and layer and not CANCEL.is_set():                  # 〔a/b〕 를 정한 뒤의 글로(3차가 정한 글자와 그 곁은 묻지 않는다)
                if n:                                                    # 값을 치른 〔a/b〕 판정은 합의 의심 전에 남긴다(그 단계가 호출 상한·키 오류로 멈춰도 다시 사지 않게)
                    account(rec, more=True); work.save_record(pg, rec)
                rec = suspect_page(rec, img, reader, horizontal, layer, max_n=smax, force=sforce, freq=freq, zoom=zoom, batch=batch, fallback_reader=fallback,
                                   layer_lines=lines, frame=frame, should_stop=CANCEL.is_set, min_px=min_px, blind=blind)
                ran.add(pg)
            elif smax and layer:
                sus_cut.append(pg)
        finally:                                         # 오류로 멈춰도 끝난 판정과 쓴 호출은 기록에 남긴다(두 단계 모두 rec 을 끝에서 한 번에 바꾼다)
            account(rec); work.save_record(pg, rec)
        if pg in ran and rec["suspect_stats"].get("differs"):
            work.set_status(pg, "판독 확인 필요")                       # 자형 판별이 1차·2차 합의와 다름 — 사람이 원본 확인
        return rec

    def on_result(item, rec):
        rec0, n, _, _ = item; pg = rec["pdf_page"]
        pages_done[0] += 1; progress("adjudicate", pages_done[0], len(todo))
        st = (rec.get("adjudicate_stats") or {}) if n else {}
        ss = (rec.get("suspect_stats") or {}) if pg in ran else {}
        for k in agg: agg[k] += st.get(k, 0)
        for k in sagg: sagg[k] += ss.get(k, 0)
        for k, v in (st.get("loc") or {}).items(): loc_agg[k] = loc_agg.get(k, 0) + v
        sus_txt = (f"합의 의심 {ss['asked']}곳 → 글자층 쪽 {ss.get('differs', 0)} · 판독 쪽 {ss.get('confirmed', 0)} · 못 가림 {ss.get('undecided', 0)}"
                   + (f" · 실패 {ss['failed']}" if ss.get("failed") else "")) if ss.get("asked") else ""
        if not n:
            if sus_txt:
                log(f"  {pg}쪽: {sus_txt}")
            return
        shown = "; ".join(f"〔{a['a']}/{a['b']}〕→{a['choice'] or '미정'}" for a in (rec.get("adjudications") or [])[-n:])
        log(f"  {pg}쪽: {st.get('sites', 0)}곳 중 확정 {st.get('decided', 0)} · 미정 {st.get('undecided', 0)}" + (f" · 실패 {st['failed']}" if st.get("failed") else "") + (f" · 중지로 못 물은 {st['skipped']}" if st.get("skipped") else "")
            + (f" · 보완 확정 {st['fallback_decided']}" if st.get("fallback_decided") else "") + (f" · {sus_txt}" if sus_txt else "") + f"  {shown[:160]}")

    def on_error(item, e) -> bool:
        rec, n = item[0], item[1]; pg = rec["pdf_page"]
        pages_done[0] += 1; progress("adjudicate", pages_done[0], len(todo))
        if isinstance(e, ReaderError) and e.fatal:
            fatal.append(e); log(f"오류: {pg}쪽 판별 실패 — {e}"); return True
        log(f"  {pg}쪽: 판별 중 예외 {type(e).__name__}: {str(e)[:120]} → 건너뜀"); return False

    run_adaptive(todo, one, readers, ceiling, start, on_result, on_error)
    if fatal:
        return 1
    names = {"layer": "글자층", "ink": "먹 실측", "uniform": "균등 분할"}
    if total_sites:
        log(f"adjudicate: 끝. {agg['sites']}곳 중 확정 {agg['decided']} · 미정 {agg['undecided']}" + (f" · 실패 {agg['failed']}" if agg["failed"] else "") + (f" · 중지로 못 물은 {agg['skipped']}" if agg.get("skipped") else "")
            + (f" · 조각 위치 어긋나 보류 {agg['misplaced']}" if agg.get("misplaced") else "") + (f" · 묶음으로 답한 자리 {agg['batched']}" if agg.get("batched") else "")
            + (f" · 보완 확정 {agg['fallback_decided']}" if agg.get("fallback_decided") else "") + (f" · 묶음 창 불일치로 낱개 재질문 {agg['batch_rechecked']}" if agg.get("batch_rechecked") else "")
            + (f" · 저해상이라 묻지 않음 {agg['lowres']}" if agg.get("lowres") else "")
            + (f" · 눈가림 재확인 {agg['blind_checked']}곳 중 불일치 보류 {agg['blind_rejected']}" if agg.get("blind_checked") else "") + (f" · 재확인 실패 보류 {agg['blind_failed']}" if agg.get("blind_failed") else "")
            + (" · 자리: " + ", ".join(f"{names.get(k, k)} {v}" for k, v in sorted(loc_agg.items(), key=lambda kv: -kv[1])) if loc_agg else "")
            + f" · {time.time() - t0:.0f}초 — 확정한 글자는 본문에 들어갔고 확인 목록에 '3차 판별' 메모가 남았습니다." + (" ④ 문서 만들기를 다시 실행하십시오." if standalone and not ran else ""))
    if ran:                                                              # 글자층이 있어 합의 의심을 본 쪽이 있을 때만
        log(f"합의 의심: {sagg['asked']}곳 물음 → 자형이 글자층 쪽 {sagg['differs']}(확인 항목) · 판독 쪽 {sagg['confirmed']} · 못 가림 {sagg['undecided']}"
            + (f" · 실패 {sagg['failed']}(다음 실행에서 다시)" if sagg["failed"] else "") + (f" · 저해상이라 묻지 않음 {sagg['lowres']}" if sagg["lowres"] else "")
            + (f" · 중지로 못 물은 {sagg['skipped']}" if sagg["skipped"] else "") + (f" · 이미 물은 자리 {sagg['already']}" if sagg["already"] else "")
            + (f" · 글자층 일치율이 낮아 뺀 쪽 {sagg['low_ratio']}" if sagg["low_ratio"] else "")
            + " — 본문은 판독대로 두고 확인 항목만 더함(지침 20)" + (" ④ 문서 만들기를 다시 실행하십시오." if standalone else ""))
    if CANCEL.is_set() and (pages_done[0] < len(todo) or agg.get("skipped") or sagg.get("skipped") or sus_cut):
        log("중지 요청으로 판별을 멈춤. 끝난 쪽은 기록에 남아 있음."); return 130
    return 0


def cmd_ledger(args, work: Work | None = None, doc=None):
    work = work or Work(args.pdf, args.work)
    from .ledger import write_ledger, ledger_text, confusable_candidates
    out, led = write_ledger(work.dir)
    log("\n".join(["# 혼동 장부 — " + work.pdf_path.name] + ledger_text(led, top=args.top)))
    cands = confusable_candidates(led, min_count=2)
    if cands:
        log("혼동 묶음 후보(두 번 이상): " + ", ".join(f"{a}{b}({n})" for a, b, n in cands[:30]))
    log(f"ledger: {out}")
    return 0


# ---------- review / report / mark ----------
@_document_command
def cmd_review(args, work: Work | None = None, doc=None):
    work = work or Work(args.pdf, args.work)
    doc = doc or open_pdf(args.pdf)
    from .review_html import make_review
    recs = work.records(parse_pages(args.pages, len(doc)))
    if not recs:
        log("오류: 판독 기록이 없어 검수 화면을 만들지 않았습니다. 먼저 ③ 판독(read)을 실행하십시오."); return 2
    progress("review", 0, 1)
    ctx = None if getattr(args, "no_context", False) else context_snapshots(work, recs, write=False)
    out = make_review(work, recs, context=ctx)
    progress("review", 1, 1)
    log(f"review: {out} ({len(recs)}쪽)")
    return 0


@_document_command
def cmd_report(args, work: Work | None = None, doc=None):
    work = work or Work(args.pdf, args.work)
    doc = doc or open_pdf(args.pdf)
    progress("report", 0, 1)
    txt = make_report(work, len(doc))
    (work.dir / "report.md").write_text(txt, encoding="utf-8")
    progress("report", 1, 1)
    log(txt)
    return 0


@_document_command
def cmd_mark(args, doc=None):
    work = Work(args.pdf, args.work)
    for n in parse_pages(args.pages, len(doc)):
        work.set_status(n, args.status, note=args.note, remove=["판독 확인 필요"] if args.status in HUMAN_ONLY and args.clear_check else None)
    log(f"mark: {args.pages} → {args.status}")
    return 0


@_document_command
def cmd_estimate(args, work: Work | None = None, doc=None):
    """판독 전에 호출·토큰·비용을 어림해 보여 준다."""
    from .estimate import estimate, format_estimate
    doc = doc or open_pdf(args.pdf)
    work = work or Work(args.pdf, args.work, initialize=False)
    pages = parse_pages(args.pages, len(doc))
    same_source = work.source_matches(getattr(args, "dpi", 300))
    prices = {}
    if getattr(args, "price_in", None) or getattr(args, "price_out", None):
        prices[args.model or ""] = {"in": args.price_in or 0, "out": args.price_out or 0}
    e = estimate(args.pdf, pages, upscale=args.upscale, second_pass=not args.no_second_pass, model=args.model or "",
                 second_model=getattr(args, "second_model", "") or "", use_cache=not getattr(args, "no_cache", False),
                 prices=prices, fx_krw=getattr(args, "fx", None),
                 records=work.records(pages, repair=False) if same_source and not getattr(args, "force", False) else [])
    if not same_source and any(work.readings_dir.glob("p*.json")):
        e["notes"].append("원본·영상 설정이 바뀌었거나 기존 기록의 원본을 확인할 수 없어 선택한 쪽을 모두 새로 판독하는 비용입니다. 실행할 때 이전 결과는 보관됩니다.")
    log(format_estimate(e))
    return 0


@_document_command
def cmd_run(args, doc=None):
    """한 번에 인식: 준비 → (읽기 방향·인쇄 쪽수 차이 자동 판정) → 이전 판 기록 갱신 → 판독 → 3차 글자 판별 → 문서 → 검수 화면 → 보고(장부 포함).
    사용자는 PDF 를 고르고 이 명령 하나만 실행하면 된다."""
    work = Work(args.pdf, args.work)
    work.ensure_source(args.dpi)
    log(f"▶ 한 번에 인식: {Path(args.pdf).name} ({len(doc)}쪽) → {work.dir}")
    pages = parse_pages(args.pages, len(doc))
    work.ensure_pages(pages, len(doc))
    # 준비는 기록이 있는 쪽(이전 판 기록 갱신에 글자층 파일이 필요)만 먼저 하고, 나머지는 판독이 읽을 차례대로 뒤에서 미리 만든다
    # — 300쪽을 다 꺼내는 7~15분 동안 모델이 노는 일이 없게. 판독 쪽이 먼저 닿은 쪽은 스스로 만든다(원자적 쓰기라 겹쳐도 됨).
    # run 의 --force 는 '다시 판독' 이지 쪽 파일을 다시 꺼내라는 뜻이 아니다(그러면 전체를 다시 꺼내느라 다시 판독 때마다 모델이 논다) — 다시 꺼내려면 prepare --force
    have = {n for n in pages if work.record_path(n).exists()}
    cmd_prepare(args, work, doc, pages=[n for n in pages if n in have], force=False)
    ahead = [n for n in pages if n not in have]
    ahead_stop = threading.Event()

    def prepare_ahead():
        try:
            cmd_prepare(args, work, doc, pages=ahead, quiet=True, stop=ahead_stop, force=False)
        except Exception as e:
            log(f"  미리 준비 중 예외(판독 쪽에서 직접 준비합니다): {type(e).__name__}: {str(e)[:80]}")
    ahead_thread = threading.Thread(target=prepare_ahead, daemon=True, name="prepare-ahead")

    def ahead_done(timeout: float | None = None):
        ahead_stop.set()
        if ahead_thread.is_alive():
            ahead_thread.join(timeout)
    resolve_mode(args, doc, work)
    auto_offset = getattr(args, "print_offset", None) is None
    if auto_offset:
        st = work.load_state()
        if isinstance(st.get("print_offset"), int):
            args.print_offset = st["print_offset"]; log(f"인쇄 쪽수 = PDF 쪽수 + {args.print_offset} (결과 폴더에 적힌 값)")
        else:
            off = detect_print_offset(doc)
            if off is not None:
                args.print_offset = off; st["print_offset"] = off; work.save_state(st)
                log(f"인쇄 쪽수 = PDF 쪽수 + {off} (글자층의 쪽번호 표기로 자동 판정)")
            else:
                log("인쇄 쪽수 차이는 글자층으로 알 수 없어, 판독한 쪽번호가 모이면 판정합니다.")
    refresh_stale_records(work, doc, pages)

    def checkpoint(reason: str):
        recs = work.records(pages)
        if recs:
            build_outputs(work, doc, recs, args, interim=True)

    ahead_thread.start()                                      # 문서 객체를 혼자 쓰는 판정(읽기 방향·쪽수)이 끝난 뒤에
    try:
        rc = cmd_read(args, work, doc, checkpoint_fn=checkpoint if args.checkpoint else None)
    finally:
        ahead_done()
    partial = rc == 3                                         # 끝까지 갔지만 실패한 쪽이 있음 — 문서는 만들되 성공으로 돌려주지 않는다
    adj_failed = False                                        # 3차 판별을 못 마침(〔a/b〕 남음) — 실패한 쪽과는 다른 말·끝 코드(4)로
    if rc not in (0, 3) or CANCEL.is_set():
        if work.records(pages):
            log("판독이 중간에 멈춰 '진행중' HWPX 를 남겼습니다. 다시 실행하면 남은 쪽부터 이어서 합니다.")
        return rc if rc not in (0, 3) else 130
    if auto_offset and getattr(args, "print_offset", None) is None:
        recs = work.records(pages); off = offset_from_records(recs)
        if off is not None:
            args.print_offset = off; st = work.load_state(); st["print_offset"] = off; work.save_state(st)
            flagged = flag_print_mismatch(work, recs, off)
            log(f"인쇄 쪽수 = PDF 쪽수 + {off} (판독한 쪽번호로 판정)" + (f" · 어긋난 쪽 {flagged}개를 확인 항목으로" if flagged else ""))
    if args.reader == "gemini" and not getattr(args, "no_adjudicate", False):
        try:
            rc_a = cmd_adjudicate(args, work, doc, standalone=False)
            if rc_a not in (0, None, 130):
                log("3차 글자 판별이 중단되었습니다(위 오류). 가리지 못한 〔a/b〕 표시는 그대로 남고, 문서는 계속 만듭니다."); adj_failed = True
        except Exception as e:
            log(f"오류: 3차 글자 판별을 건너뜀({e}). 〔a/b〕 표시는 그대로 남습니다."); adj_failed = True
        if CANCEL.is_set(): return 130
    rc = cmd_build(args, work, doc)
    if CANCEL.is_set(): return 130
    cmd_review(args, work, doc)
    try:
        from .ledger import write_ledger
        write_ledger(work.dir)
    except Exception:
        pass
    cmd_report(args, work, doc)
    log("끝. 결과: out/ 의 HWPX · review.html(검수 화면) · report.md(상태·다발 오인식 장부) · confusion.json"
        + (" — 실패한 쪽이 있습니다(위 '오류:' 줄). 다시 [▶] 를 누르면 그 쪽만 다시 시도합니다." if partial else "")
        + ((" —" if not partial else "") + " 3차 글자 판별을 마치지 못해 〔a/b〕 표시가 남았습니다(위 '오류:' 줄). 다시 [▶] 를 누르면 3차 판별을 이어서 합니다." if adj_failed else ""))
    return rc or (3 if partial else (4 if adj_failed else 0))         # 3 = 실패한 쪽 있음, 4 = 쪽은 다 읽었지만 3차 판별을 못 마침


def build_parser():
    ap = argparse.ArgumentParser(prog="yeone-ocr", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", action="version", version=f"YeonE_OCR {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p, pages=True):
        p.add_argument("pdf", help="스캔 PDF, 또는 이미지 파일(PNG·JPG·TIFF…, 여러 쪽 TIFF 는 쪽마다)·이미지 폴더(이름의 숫자 순서로 한 권)")
        p.add_argument("--work", help="작업 폴더(기본 work/<PDF 이름>, 이미지는 work/<이름>_<확장자>, 이미지 폴더는 work/<폴더>_이미지)")
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
        p.add_argument("--workers", type=int, default=8, help="동시에 판독할 쪽 수의 최대(기본 8). 한도(429)가 나면 자동으로 줄인다")
        p.add_argument("--workers-start", type=int, default=4, help="시작 동시 쪽 수(기본 4). 10쪽 연속 문제없으면 하나씩 늘린다")
        p.add_argument("--second-mode", choices=["batch", "each"], default="batch", help="2차 판독을 조각 묶음 한 호출(batch, 기본)로 할지 영역별 호출(each)로 할지")
        p.add_argument("--second-model", default="auto", help="2차 판독 모델. auto(기본)=1차와 같은 세대의 flash 자동 선택, 비우면 1차와 같음")
        p.add_argument("--second-thinking", choices=["low", "full"], default="low", help="2차 판독(전사)의 생각 수준. low(기본)=줄여서 빠르게")
        p.add_argument("--no-cache", action="store_true", help="지침 컨텍스트 캐시를 쓰지 않음")
        p.add_argument("--chunk-lines", type=int, default=8, help="2차 조각 한 장에 넣을 글줄(세로쓰기는 열) 수. 넘으면 나눠 보낸다(0=안 나눔). 글자당 픽셀을 늘려 획 하나 차이를 가린다")
        p.add_argument("--media-resolution", choices=["auto", "default", "low", "medium", "high", "ultra_high"], default="auto",
                       help="2차 조각 이미지의 토큰 예산(Gemini media_resolution). auto(기본)=high. ultra_high 는 Gemini 3 에서 조각마다 2,240 토큰(두 배). 모델이 거부하면 자동으로 한 단계 아래로")
        p.add_argument("--checkpoint", type=int, default=20, help="N쪽 판독마다 '<이름>_진행중.hwpx' 를 중간 저장(0=안 함). run 에서만")
        p.add_argument("--no-adjudicate", action="store_true", help="run 에서 판독 뒤 3차 글자 판별(〔a/b〕 자리)과 합의 판독 의심 확인을 하지 않음(--no-suspects 는 합의 의심만 뺌)")
        p.add_argument("--batch-crops", type=int, default=8, help="2차 묶음 호출 한 번에 넣을 조각 수(기본 8). 많으면 모델이 조각과 영역 번호를 섞는다")
        p.add_argument("--page-thinking", choices=["auto", "low", "medium", "high"], default="auto",
                       help="1차(쪽 전체) 판독의 생각 수준. auto(기본)=모델 기본. Gemini 3 은 thinking_level, 2.5 는 예산(low 512 · medium 4096 · high 24576, 2.5 Pro 32768). 측정(eval) 뒤에 바꾸십시오")
        p.add_argument("--page-media-resolution", choices=["auto", "high", "ultra_high"], default="auto",
                       help="1차 쪽 이미지의 토큰 예산. auto(기본)=설정 없음. ultra_high 는 Gemini 3 에서 쪽 이미지에 조각별로 붙인다. 거부되면 자동으로 한 단계 아래로")
        max_calls_opt(p)
        if not any(a.dest == "mode" for a in p._actions):
            p.add_argument("--mode", choices=["auto", "vertical", "horizontal_zh"], default="auto",
                           help="읽기 모드: auto(기본)=글자층으로 판정(결과 폴더에 적힌 값 우선), vertical=세로쓰기 한자·한글, horizontal_zh=가로쓰기 한문(간체)")

    def max_calls_opt(p):
        def count(s):
            n = int(s)
            if n < 0:
                raise argparse.ArgumentTypeError("0 이상의 정수(0 = 상한 없음)")
            return n
        p.add_argument("--max-calls", type=count, default=0, help="이 명령 전체의 모델 호출 상한(재시도 포함, 0=없음, 기본 0). 닿으면 멈추고 끝난 쪽은 기록에 남음")

    def thinking_opt(p):
        p.add_argument("--adjudicate-thinking", choices=["low", "full"], default="low", help="3차(자형) 판별의 생각 수준. low(기본)=줄여서 빠르게(객관식 자형 질문)")
        p.add_argument("--adjudicate-blind", action="store_true",
                       help="3차 판별에서 고른 자리를 후보 없이 한 번 더 읽게 해 같을 때만 확정. 호출이 더 든다: adjudicate·run 은 쪽마다 ⌈확정 자리/--batch-sites⌉회, 책갈피 교정(collate)은 확정 항목마다 1회")
        p.add_argument("--adjudicate-min-px", type=float, default=MIN_GLYPH_PX,
                       help=f"원본 글자 크기가 이 픽셀보다 작은 자리는 3차로 묻지 않고 미정으로 둠(기본 {MIN_GLYPH_PX:g}, 0=끔. 지침 24)")

    def adjudicate_opts(p):
        p.add_argument("--adjudicate-model", default="", help="3차 글자 판별 모델. 비우면 1차 모델(--model), auto=같은 세대 flash 로 먼저 묻고 못 가린 자리만 1차 모델로")
        thinking_opt(p)
        p.add_argument("--batch-sites", type=int, default=8, help="3차 판별 한 호출에 넣을 글자 조각 수(기본 8, 1=낱개 호출)")
        p.add_argument("--no-suspects", action="store_true",
                       help="합의 판독 의심을 묻지 않음: 1차·2차가 같게 읽었는데 PDF 글자층과 자형 혼동 쌍(入/人 등) 한 글자가 다른 자리를 3차처럼 자형으로 묻고, "
                            "자형이 글자층 쪽이면 본문은 그대로 두고 확인 항목만 다는 일(기본은 함)")
        p.add_argument("--suspect-max", type=int, default=8, help="쪽당 묻는 합의 의심 자리 수 상한(기본 8, 이미 물은 자리도 셈. 혼동 장부가 있으면 잦은 쌍부터)")
        p.add_argument("--force-suspects", action="store_true", help="이미 물은 합의 의심 자리도 다시 물음(앞 결과를 바꿈)")

    def build_opts(p):
        p.add_argument("--docx", action="store_true", help="DOCX 도 함께 생성")
        p.add_argument("--out", help="출력 파일 이름(확장자 제외)")
        p.add_argument("--figure-min-mm", type=float, default=70)
        p.add_argument("--figure-large-min-mm", type=float, default=120, help="나경도·복잡한 표·계통도 최소 폭")
        p.add_argument("--no-join-pages", action="store_true", help="앞 쪽에서 이어지는 문단을 잇지 않음")
        p.add_argument("--whole-offset", type=int, default=None, help="분할 전 전체 PDF 쪽수 = PDF 쪽수 + N 을 기록에 함께 적음")
        p.add_argument("--append-report", action="store_true", help="문서 끝에 〔작업 기록〕 덧붙임")
        p.add_argument("--no-highlight", action="store_true", help="확인 등급 색·밑줄·범례를 넣지 않음(〔〕 표기와 고지 메모는 그대로)")
        p.add_argument("--memo", choices=["notice", "all", "inline"], default="notice",
                       help="한컴 메모: notice=고지 사항만(기본) / all=등급 근거까지 / inline=메모 없이 고지를 글 속 회색 〔고지: …〕 로")
        p.add_argument("--no-context", action="store_true", help="문맥 추정(이 판독의 다른 대목이 가리키는 후보에 밑줄)을 하지 않음")
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
    p = sub.add_parser("remerge", help="저장된 1차·2차 판독으로 비교·표시를 다시 계산(모델 호출 없음)"); common(p)
    p.add_argument("--mode", choices=["auto", "vertical", "horizontal_zh"], default="auto", help="읽기 모드(auto=결과 폴더에 적힌 값·글자층 판정)")
    p.set_defaults(fn=cmd_remerge)
    p = sub.add_parser("adjudicate", help="3차 글자 판별: 1차·2차가 갈린 〔a/b〕 자리를 글자 하나씩 크게 잘라 모델에 자형만으로 묻는다"); common(p)
    p.add_argument("--reader", choices=["gemini", "mock"], default="gemini")
    p.add_argument("--model", help="판별에 쓸 모델(기본 GEMINI_MODEL 또는 gemini-2.5-pro). 1차와 같은 pro 계열을 권함")
    p.add_argument("--thinking-budget", type=int, default=None)
    p.add_argument("--no-cache", action="store_true")
    p.add_argument("--max-sites", type=int, default=0, help="쪽당 판별할 자리 수 상한(0=전부)")
    p.add_argument("--zoom", type=int, default=6, help="글자 조각 확대 배율(기본 6)")
    p.add_argument("--workers", type=int, default=8, help="동시에 판별할 쪽 수의 최대(기본 8). 한도(429)가 나면 자동으로 줄인다")
    p.add_argument("--workers-start", type=int, default=4, help="시작 동시 쪽 수(기본 4)")
    max_calls_opt(p)
    adjudicate_opts(p)
    p.add_argument("--mode", choices=["auto", "vertical", "horizontal_zh"], default="auto")
    p.set_defaults(fn=cmd_adjudicate)
    p = sub.add_parser("ledger", help="혼동 장부: 결과 폴더의 판독·대조 기록에서 글자 쌍 빈도를 모아 confusion.json 과 표로"); common(p, pages=False)
    p.add_argument("--top", type=int, default=30); p.set_defaults(fn=cmd_ledger)
    p = sub.add_parser("review"); common(p); p.set_defaults(fn=cmd_review)
    p = sub.add_parser("eval", help="평가: 저장된 판독 기록을 사람이 입력한 원본 HWPX 와 맞춰 단계별 CER·표시 밖 오류율·3차 정밀도를 잼(모델 호출 없음)"); common(p)
    from .evaluate import add_arguments as _eval_args, cmd_eval
    _eval_args(p); p.set_defaults(fn=cmd_eval)
    p = sub.add_parser("report"); common(p); p.set_defaults(fn=cmd_report)
    p = sub.add_parser("mark"); common(p); p.add_argument("--status", required=True, choices=STATUSES); p.add_argument("--note")
    p.add_argument("--clear-check", action="store_true", help="'판독 확인 필요' 표시를 지움(사람이 확인한 뒤)"); p.set_defaults(fn=cmd_mark)
    p = sub.add_parser("run"); common(p); read_opts(p); adjudicate_opts(p); build_opts(p); p.set_defaults(fn=cmd_run)
    p = sub.add_parser("collate", help="책갈피 교정: HWPX 구획을 PDF 판독과 대조해 고친 사본을 저장"); common(p, pages=False); read_opts(p); thinking_opt(p)
    p.add_argument("--hwpx", required=True, help="교정할 HWPX")
    p.add_argument("--list", action="store_true", help="HWPX 책갈피·제목과 PDF 책갈피의 짝 목록만 보여 줌")
    p.add_argument("--limit", type=int, default=0, help="--list 에서 보여 줄 줄 수(0=전부)")
    p.add_argument("--section", help="교정할 책갈피·제목 이름(정확히, 또는 일부)")
    p.add_argument("--nth", type=int, help="같은 이름이 여럿일 때 몇 번째인지(1부터)")
    p.add_argument("--anchor-index", type=int, help="--list 번호로 구획을 바로 지정(1부터). 화면에서 쓴다")
    p.add_argument("--heading-styles", help="책갈피처럼 쓸 HWPX 스타일 이름(쉼표로 여러 개, 앞이 큰 단위). 예 \"제목-본초명,제목-본초명(소)\". 비우면 이름에 '제목'이 든 스타일 전부")
    p.add_argument("--anchor-source", choices=["auto", "hwpx", "pdf"], default="auto",
                   help="구획 기준: auto(기본)=HWPX 책갈피·제목 스타일, 없으면 PDF 책갈피 이름으로 본문 검색 / hwpx / pdf")
    p.add_argument("--pdf-pages", help="PDF 책갈피 짝이 없을 때 쪽 범위를 직접(예 120-123)")
    p.add_argument("--policy", choices=["all", "sure", "memo"], default="sure", help="all=A·B2 자동, C 는 표시만 / sure=A 만(기본) / memo=메모만")
    p.add_argument("--apply", action="store_true", help="대조 결과를 HWPX 에 반영해 사본(<이름>_교정.hwpx)으로 저장. 없으면 보고만")
    p.add_argument("--out", help="저장할 HWPX 경로(기본 <이름>_교정.hwpx)")
    p.add_argument("--memo", choices=["notice", "all", "inline"], default=None,
                   help="한컴 메모: notice=고지 사항만(지운 글자·구조 차이·반영하지 않은 제안 등, 기본) / all=모든 항목에 근거(0.6.3 방식) / inline=메모 없이 고지만 글 뒤 〔…〕")
    p.add_argument("--no-memo", action="store_true", help="(예전 이름) --memo inline 과 같음")
    p.add_argument("--all", action="store_true", help="구획 전체를 차례로 대조(--apply 면 교정본 하나에 쌓음). PDF 쪽을 못 찾는 구획은 건너뛰고 요약에 적음")
    p.add_argument("--no-glyph-check", action="store_true", help="대조 뒤 자형 편향·혼동 자리(入/人 등)를 글자 조각으로 모델에 묻는 3차 판별을 하지 않음")
    p.set_defaults(mode="auto")
    from .collate.run import cmd_collate
    p.set_defaults(fn=cmd_collate)
    return ap


def main(argv=None):
    args = build_parser().parse_args(argv)
    from .reader_gemini import set_call_cap, set_cancel_event
    set_call_cap(getattr(args, "max_calls", 0) or 0)        # 호출 상한은 명령마다 새로 센다(창 프로그램은 한 프로세스에서 명령을 여럿 돌린다)
    if getattr(args, "max_calls", 0):
        log(f"모델 호출 상한: 이번 명령에서 {args.max_calls}회(재시도 포함)")
    if not Path(args.pdf).exists() and not (args.cmd == "eval" and getattr(args, "pages", None)):   # eval 은 --pages 가 있으면 PDF 없이 기록만으로 잰다
        log(f"파일이 없습니다: {args.pdf}"); return 2
    if not getattr(args, "work", None) and (old := legacy_work_dir(args.pdf)):     # 예전 판의 이미지 결과 폴더(work/<이름>)는 저절로 옮겨 쓰지 않고 알린다
        log(f"참고: 예전 판의 결과 폴더가 있습니다: {old} — 그 판독 기록을 이어 쓰려면 --work \"{old}\" 을(를) 붙이십시오.")
    set_cancel_event(CANCEL)
    try:
        rc = args.fn(args)
        return 130 if CANCEL.is_set() and rc not in (0, None) else rc
    except ReaderError as e:
        if e.kind == "cancelled":
            log(str(e)); return 130
        raise
    finally:
        set_cancel_event(None)
