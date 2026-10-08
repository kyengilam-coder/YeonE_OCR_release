"""명령줄. python -m yeone_ocr <명령> <PDF> [옵션]

  inspect  PDF 구조·텍스트 레이어 상태 보고
  prepare  쪽 이미지(내장 스캔 우선)와 기존 텍스트 레이어를 작업 폴더에 꺼냄
  estimate 판독 전에 호출·토큰·비용 어림
  read     모델(기본 gemini)로 영역 분류·판독 → 확대 재판독 비교 → 텍스트 레이어 대조 → 구조 검사
  build    도판 잘라내기(외곽 검사) → HWPX(+DOCX) 조립 → 파일 구조 검사
  remerge  저장된 1차·2차 판독으로 비교·표시만 다시 계산 (모델 호출 없음)
  adjudicate  3차 글자 판별: 1차·2차가 갈린 〔a/b〕 자리를 글자 하나씩 크게 잘라 모델에 자형만으로 묻는다
  ledger   혼동 장부: 어느 글자가 어느 글자로 읽혔는지 결과 폴더 전체에서 모아 confusion.json 과 표로 (모델 호출 없음)
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
from .layout import upscale, crop_frac, fit_figure_bbox, expand_until_clean, detect_figures, overlap_ratio, uncovered_ink_boxes, attach_uncovered, snap_to_gaps, split_bands
from .merge import merge_passes, compare_textlayer
from .verify import verify_page, check_hwpx_file
from .blocks import assemble_blocks, Block
from .report import make_report
from .refit import refit_regions, describe as refit_describe


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
    n = 0; stale: list[tuple[int, float]] = []
    for rec in work.records(pages):
        if rec.get("version") == __version__ or not rec.get("pass1"):
            continue
        pg = rec["pdf_page"]
        tlp = work.textlayer_path(pg)
        layer = json.loads(tlp.read_text(encoding="utf-8")) if tlp.exists() else {}
        horizontal = (rec.get("mode") or prompts.MODE) == "horizontal_zh"
        p1 = PageReading.model_validate(rec["pass1"])
        fix = refit_regions(None, p1, horizontal, layer)
        if fix["fixed"] and fix["max_shift"] > STALE_MOVE and rec.get("pass2"):
            sd = work.readings_dir / "stale"; sd.mkdir(exist_ok=True)
            work.record_path(pg).replace(sd / work.record_path(pg).name); stale.append((pg, fix["max_shift"]))
            continue
        rec["pass1"] = p1.model_dump(); rec["bbox_fix"] = fix
        rec = remerge_record(rec, layer.get("plain", "")); rec["version"] = __version__
        work.save_record(pg, rec); n += 1
    if n:
        log(f"이전 판의 판독 기록 {n}쪽을 지금 규칙으로 다시 비교했습니다(모델 호출 없음).")
    if stale:
        log(f"이전 기록 {len(stale)}쪽({', '.join(str(p) for p, _ in stale[:8])}{' …' if len(stale) > 8 else ''})은 영역 좌표가 최대 {max(m for _, m in stale):.1%} 밀려 있어"
            f"(2차 조각이 이웃 글을 읽었을 수 있음) 다시 판독합니다 — 이전 기록은 readings/stale/ 에 두었습니다.")
    return n


def make_reader(args, doc, work):
    resolve_mode(args, doc, work)
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
    keep = [u for u in PageReading.model_validate(rec["merged"]).uncertain
            if u.context.startswith(("자동 감지한 큰 도형", "인쇄 쪽수 판독값"))]      # 도판·쪽수 교차 확인 항목은 유지
    merged.uncertain.extend(u for u in keep if u not in merged.uncertain)
    if rec.get("adjudications"):
        from .adjudicate import apply_adjudications
        mstats["adjudicated"] = apply_adjudications(merged, rec["adjudications"])     # 3차 판별로 확정한 글자는 다시 계산해도 남긴다
    rec["merged"] = merged.model_dump(); rec["merge_stats"] = mstats
    rec["textlayer_diff"] = compare_textlayer(merged, layer_plain)
    rec["verify"] = verify_page(merged)
    rec["remerged_at"] = time.strftime("%Y-%m-%d %H:%M:%S"); rec["version"] = __version__
    return rec


def cmd_remerge(args, work: Work | None = None, doc=None):
    doc = doc or open_pdf(args.pdf)
    work = work or Work(args.pdf, args.work)
    resolve_mode(args, doc, work)                      # 가로쓰기 한문이면 간체·부호 차이를 접어 비교
    records = work.records(parse_pages(args.pages, len(doc)))
    if not records:
        log(f"오류: 판독 기록이 없습니다 — 결과 폴더 {work.dir} 의 readings/ 가 비어 있습니다. 기록이 있는 원본 PDF 를 고르십시오."); return 2
    for rec in records:
        n = rec["pdf_page"]
        if not rec.get("pass1"):
            log(f"  {n}쪽: 1차 판독 원본이 없어 건너뜀"); continue
        before = len(rec["merged"].get("uncertain", []))
        tlp = work.textlayer_path(n)
        layer_d = json.loads(tlp.read_text(encoding="utf-8")) if tlp.exists() else {}
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
            + (f" · 1차에만 {ms.get('columns_only_pass1')}열, 2차에만 {ms.get('columns_only_pass2')}열" if ms.get('columns_only_pass1') or ms.get('columns_only_pass2') else ""))
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


def _second_pass(reader2, img, p1: PageReading, n: int, args) -> tuple[dict[str, RegionReread], dict]:
    """문자 영역마다 확대 조각을 만들어 2차 독립 판독. 기본은 조각들을 한 호출에 묶어 보내고(빠르고 쌈), 빠진 영역은 낱개로 다시 묻는다.

    0.6.0: 조각당 글자 수를 줄여 글자당 픽셀을 늘린다.
    - 가로쓰기 모드: 좌우로만 넓히고 위·아래는 글줄 사이 공백에 맞춘다(이웃 줄을 끌어들여 '2차에만 있음' 잡음이 생기던 과확장 방지).
    - 글줄(세로쓰기면 열)이 --chunk-lines 보다 많으면 그만큼씩 나눠 보내고, 돌아온 열을 이어 붙여 영역 하나로 되돌린다."""
    import unicodedata as _ud
    from . import prompts
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
    if args.second_mode == "batch" and len(items) > 1 and hasattr(reader2, "reread_regions"):
        per = max(1, int(getattr(args, "batch_crops", 8) or 8))         # 한 호출에 조각 N장까지 — 많이 넣을수록 모델이 조각과 번호를 섞는다
        for k in range(0, len(items), per):
            batch = items[k:k + per]
            if len(batch) == 1 and k:
                break                                                     # 하나 남은 조각은 아래 낱개 호출로
            try:
                rereads.update(reader2.reread_regions(batch, n))
            except Exception as e:
                from .reader_gemini import ReaderError
                if isinstance(e, ReaderError) and e.fatal:
                    raise
                log(f"    {n}쪽: 묶음 2차 판독 실패({str(e)[:60]}) → 영역별로 다시")
        bad = _misassigned(rereads, p1)
        for rid in bad:
            rereads.pop(rid, None)
        if bad:
            log(f"    {n}쪽: 묶음 응답 {len(bad)}개({', '.join(bad[:5])})가 다른 영역의 글로 보여 낱개로 다시 읽음")
    for rid, kind, im in items:
        if rid in rereads and rereads[rid].columns:
            continue
        rr = reader2.reread_region(im, kind, rid, n)
        if rr.columns:
            rereads[rid] = rr
    # 조각으로 나눠 읽은 영역은 열을 순서대로 이어 붙여 영역 하나로 되돌린다(열 번호는 1부터 다시)
    for base, ids in chunks_of.items():
        cols: list = []; unc: list = []; got = 0
        for cid in ids:
            rr = rereads.pop(cid, None)
            if not rr or not rr.columns:
                continue
            got += 1
            for c in sorted(rr.columns, key=lambda c: c.col_from_right):
                c.col_from_right = len(cols) + 1; cols.append(c)
            for u in rr.uncertain:
                u.region_id = base; unc.append(u)
        if cols:
            rereads[base] = RegionReread(columns=cols, uncertain=unc)
        meta[base]["chunks_read"] = got
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
    from . import prompts
    horizontal = prompts.MODE == "horizontal_zh"
    tlp = work.textlayer_path(n)
    if tlp.exists():
        layer = json.loads(tlp.read_text(encoding="utf-8"))
    else:
        from dataclasses import asdict as _asdict
        with DOC_LOCK:
            tl = text_layer(doc, n)
        layer = {**_asdict(tl), "plain": tl.plain()}
    # 모델의 영역 좌표가 아래로 갈수록 밀리는 일이 있어(실측 최대 7.8%) 글자층 줄·먹 띠에 맞춘 뒤에 조각을 자른다
    bbox_fix = refit_regions(img, p1, horizontal, layer)
    coverage_notes = ([refit_describe(bbox_fix)] if bbox_fix["fixed"] else []) + cover_check(img, p1, horizontal)
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
    rec = {"file": Path(args.pdf).name, "pdf_page": n, "reader": reader.name, "model": getattr(reader, "model", ""), "version": __version__, "mode": getattr(args, "mode", ""),
           "second_model": getattr(reader2, "model", "") if reader2 is not reader else "",
           "image_source": src, "upscale": args.upscale, "read_at": time.strftime("%Y-%m-%d %H:%M:%S"), "seconds": round(time.time() - t0, 1),
           "usage": usage, "coverage_notes": coverage_notes, "bbox_fix": bbox_fix,
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
            mres = getattr(args, "media_resolution", "auto") or "auto"
            mres = "high" if mres == "auto" else (None if mres == "default" else mres)      # auto = 2차 조각은 high(글자당 토큰을 늘린다)
            if (want and want != reader.model) or think2 or mres:
                reader2 = GeminiReader(model=want or reader.model, thinking_budget=args.thinking_budget, use_cache=not args.no_cache, thinking=think2, media_resolution=mres)
    except Exception as e:
        log(f"오류: {e}"); return 1
    todo = [n for n in pages if args.force or not work.load_record(n)]
    skipped = len(pages) - len(todo)
    ceiling = max(1, int(args.workers or 1))
    cur = max(1, min(ceiling, int(args.workers_start or ceiling)))     # 동시 쪽 수: 여기서 시작해 한도(429)가 나면 줄이고, 안정되면 늘린다
    log(f"read: 판독기 {reader.name} {getattr(reader, 'model', '')}"
        + (f" · 2차 {reader2.model}{' (생각 줄임)' if getattr(reader2, 'thinking', None) == 'low' else ''}{' · 조각 해상도 ' + reader2.media_resolution if getattr(reader2, 'media_resolution', None) else ''}" if reader2 is not reader else "")
        + (f" · 조각 {args.chunk_lines}줄 묶음" if getattr(args, "chunk_lines", 0) else "")
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
    u = {"calls": 0, "prompt_tokens": 0, "output_tokens": 0, "thinking_tokens": 0, "cached_tokens": 0}
    for r in {id(reader): reader, id(reader2): reader2}.values():
        for k in u:
            u[k] += (getattr(r, "usage", None) or {}).get(k, 0)
    if u["calls"]:
        log(f"모델 호출 통계(이번 실행, 1차+2차): 호출 {u['calls']}회 · 토큰 입력 {u['prompt_tokens']:,} 출력 {u['output_tokens'] + u['thinking_tokens']:,}"
            + (f" · 캐시 적중 {u['cached_tokens']:,}" if u["cached_tokens"] else "") + f" · {time.time() - t_start:.0f}초  — 쪽별 누적은 ⑥ 보고에 있음")
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
            log(f"build: 판독 기록이 있는 {len(records)}쪽만 문서에 넣음 (PDF 전체 {len(doc)}쪽). 전체를 만들려면 쪽 범위를 비우고 [▶ 한 번에 인식] 을 다시 누르십시오.")
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


# ---------- adjudicate / ledger ----------
def cmd_adjudicate(args, work: Work | None = None, doc=None, standalone: bool = True):
    """3차 글자 판별. 〔a/b〕 자리마다 호출 한 번(flash 로도 충분). 확정한 글자는 merged 와 adjudications 에 남고, remerge 뒤에도 유지된다."""
    from . import prompts
    doc = doc or open_pdf(args.pdf)
    work = work or Work(args.pdf, args.work)
    resolve_mode(args, doc, work)
    max_sites = int(getattr(args, "max_sites", 0) or 0); zoom = int(getattr(args, "zoom", 6) or 6)
    records = work.records(parse_pages(args.pages, len(doc)))
    if not records:
        log(f"오류: 판독 기록이 없습니다 — 결과 폴더 {work.dir} 의 readings/ 가 비어 있습니다. 기록이 있는 원본 PDF 를 고르십시오."); return 2
    from .adjudicate import find_sites, adjudicate_page
    todo = [(rec, len(find_sites(PageReading.model_validate(rec["merged"])))) for rec in records]
    total_sites = sum(n for _, n in todo)
    if not total_sites:
        log("판별할 〔a/b〕 자리가 없습니다."); return 0
    if args.reader == "mock":
        log(f"mock 판독기는 자형을 보지 못합니다. 〔a/b〕 자리 {total_sites}곳만 셌습니다."); return 0
    from .reader_gemini import GeminiReader, ReaderError
    try:
        reader = GeminiReader(model=args.model, thinking_budget=args.thinking_budget, use_cache=not getattr(args, "no_cache", False),
                              media_resolution="high")
    except Exception as e:
        log(f"오류: {e}"); return 1
    log(f"adjudicate: 판독기 {reader.model} · {len(records)}쪽 · 〔a/b〕 {total_sites}곳 · 조각 확대 {zoom}배" + (f" · 쪽당 최대 {max_sites}곳" if max_sites else ""))
    horizontal = prompts.MODE == "horizontal_zh"
    agg = {"sites": 0, "decided": 0, "undecided": 0, "failed": 0, "misplaced": 0}
    for rec, n in todo:
        if not n or CANCEL.is_set():
            continue
        pg = rec["pdf_page"]
        img, _ = _load_page_image(work, doc, pg, args.dpi)
        if hasattr(reader, "usage_begin"): reader.usage_begin()
        try:
            rec = adjudicate_page(rec, img, reader, horizontal, max_sites=max_sites, zoom=zoom)
        except ReaderError as e:
            log(f"오류: {pg}쪽 판별 실패 — {e}")
            if e.fatal: return 1
            continue
        u = reader.usage_end() if hasattr(reader, "usage_end") else {}
        if u.get("calls"):
            usage = rec.get("usage") or {}
            for k, v in u.items():
                usage[k] = usage.get(k, 0) + v
            usage.setdefault("by_model", {}); usage["by_model"][reader.model + " (3차)"] = u; rec["usage"] = usage
        work.save_record(pg, rec)
        st = rec.get("adjudicate_stats") or {}
        for k in agg: agg[k] += st.get(k, 0)
        shown = "; ".join(f"〔{a['a']}/{a['b']}〕→{a['choice'] or '미정'}" for a in (rec.get("adjudications") or [])[-n:])
        log(f"  {pg}쪽: {st.get('sites', 0)}곳 중 확정 {st.get('decided', 0)} · 미정 {st.get('undecided', 0)}" + (f" · 실패 {st['failed']}" if st.get("failed") else "") + f"  {shown[:160]}")
    log(f"adjudicate: 끝. {agg['sites']}곳 중 확정 {agg['decided']} · 미정 {agg['undecided']}" + (f" · 실패 {agg['failed']}" if agg["failed"] else "")
        + (f" · 조각 위치 어긋나 보류 {agg['misplaced']}" if agg.get("misplaced") else "")
        + " — 확정한 글자는 본문에 들어갔고 확인 목록에 '3차 판별' 메모가 남았습니다." + (" ④ 문서 만들기를 다시 실행하십시오." if standalone else ""))
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
                 second_model=getattr(args, "second_model", "") or "", use_cache=not getattr(args, "no_cache", False),
                 prices=prices, fx_krw=getattr(args, "fx", None), records=work.records(pages) if not getattr(args, "force", False) else [])
    log(format_estimate(e))
    return 0


def cmd_run(args):
    """한 번에 인식: 준비 → (읽기 방향·인쇄 쪽수 차이 자동 판정) → 이전 판 기록 갱신 → 판독 → 3차 글자 판별 → 문서 → 검수 화면 → 보고(장부 포함).
    사용자는 PDF 를 고르고 이 명령 하나만 실행하면 된다."""
    doc = open_pdf(args.pdf)
    work = Work(args.pdf, args.work)
    log(f"▶ 한 번에 인식: {Path(args.pdf).name} ({len(doc)}쪽) → {work.dir}")
    cmd_prepare(args, work, doc)
    pages = parse_pages(args.pages, len(doc))
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

    rc = cmd_read(args, work, doc, checkpoint_fn=checkpoint if args.checkpoint else None)
    if rc or CANCEL.is_set():
        if work.records(pages):
            log("판독이 중간에 멈춰 '진행중' HWPX 를 남겼습니다. 다시 실행하면 남은 쪽부터 이어서 합니다.")
        return rc or 130
    if auto_offset and getattr(args, "print_offset", None) is None:
        recs = work.records(pages); off = offset_from_records(recs)
        if off is not None:
            args.print_offset = off; st = work.load_state(); st["print_offset"] = off; work.save_state(st)
            flagged = flag_print_mismatch(work, recs, off)
            log(f"인쇄 쪽수 = PDF 쪽수 + {off} (판독한 쪽번호로 판정)" + (f" · 어긋난 쪽 {flagged}개를 확인 항목으로" if flagged else ""))
    if args.reader == "gemini" and not getattr(args, "no_adjudicate", False):
        try:
            cmd_adjudicate(args, work, doc, standalone=False)
        except Exception as e:
            log(f"3차 글자 판별을 건너뜀({e}). 〔a/b〕 표시는 그대로 남습니다.")
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
    log("끝. 결과: out/ 의 HWPX · review.html(검수 화면) · report.md(상태·다발 오인식 장부) · confusion.json")
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
        p.add_argument("--chunk-lines", type=int, default=8, help="2차 조각 한 장에 넣을 글줄(세로쓰기는 열) 수. 넘으면 나눠 보낸다(0=안 나눔). 글자당 픽셀을 늘려 획 하나 차이를 가린다")
        p.add_argument("--media-resolution", choices=["auto", "default", "low", "medium", "high"], default="auto",
                       help="2차 조각 이미지의 토큰 예산(Gemini media_resolution). auto(기본)=high. 모델이 거부하면 자동으로 기본값")
        p.add_argument("--checkpoint", type=int, default=20, help="N쪽 판독마다 '<이름>_진행중.hwpx' 를 중간 저장(0=안 함). run 에서만")
        p.add_argument("--no-adjudicate", action="store_true", help="run 에서 판독 뒤 3차 글자 판별(〔a/b〕 자리)을 하지 않음")
        p.add_argument("--batch-crops", type=int, default=8, help="2차 묶음 호출 한 번에 넣을 조각 수(기본 8). 많으면 모델이 조각과 영역 번호를 섞는다")
        if not any(a.dest == "mode" for a in p._actions):
            p.add_argument("--mode", choices=["auto", "vertical", "horizontal_zh"], default="auto",
                           help="읽기 모드: auto(기본)=글자층으로 판정(결과 폴더에 적힌 값 우선), vertical=세로쓰기 한자·한글, horizontal_zh=가로쓰기 한문(간체)")

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
    p.add_argument("--mode", choices=["auto", "vertical", "horizontal_zh"], default="auto")
    p.set_defaults(fn=cmd_adjudicate)
    p = sub.add_parser("ledger", help="혼동 장부: 결과 폴더의 판독·대조 기록에서 글자 쌍 빈도를 모아 confusion.json 과 표로"); common(p, pages=False)
    p.add_argument("--top", type=int, default=30); p.set_defaults(fn=cmd_ledger)
    p = sub.add_parser("review"); common(p); p.set_defaults(fn=cmd_review)
    p = sub.add_parser("report"); common(p); p.set_defaults(fn=cmd_report)
    p = sub.add_parser("mark"); common(p); p.add_argument("--status", required=True, choices=STATUSES); p.add_argument("--note")
    p.add_argument("--clear-check", action="store_true", help="'판독 확인 필요' 표시를 지움(사람이 확인한 뒤)"); p.set_defaults(fn=cmd_mark)
    p = sub.add_parser("run"); common(p); read_opts(p); build_opts(p); p.set_defaults(fn=cmd_run)
    p = sub.add_parser("collate", help="책갈피 교정: HWPX 구획을 PDF 판독과 대조해 고친 사본을 저장"); common(p, pages=False); read_opts(p)
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
    p.add_argument("--policy", choices=["all", "sure", "memo"], default="all", help="all=전부 자동(A·B2·누락 삽입, B1·C 는 표시) / sure=확실한 것(A)만 / memo=메모만")
    p.add_argument("--apply", action="store_true", help="대조 결과를 HWPX 에 반영해 사본(<이름>_교정.hwpx)으로 저장. 없으면 보고만")
    p.add_argument("--out", help="저장할 HWPX 경로(기본 <이름>_교정.hwpx)")
    p.add_argument("--no-memo", action="store_true", help="한컴 메모 대신 글 뒤에 〔…〕 로 적음")
    p.add_argument("--all", action="store_true", help="구획 전체를 차례로 대조(--apply 면 교정본 하나에 쌓음). PDF 쪽을 못 찾는 구획은 건너뛰고 요약에 적음")
    p.add_argument("--no-glyph-check", action="store_true", help="대조 뒤 자형 편향·혼동 자리(入/人 등)를 글자 조각으로 모델에 묻는 3차 판별을 하지 않음")
    p.set_defaults(mode="auto")
    from .collate.run import cmd_collate
    p.set_defaults(fn=cmd_collate)
    return ap


def main(argv=None):
    args = build_parser().parse_args(argv)
    if not Path(args.pdf).exists():
        log(f"파일이 없습니다: {args.pdf}"); return 2
    return args.fn(args)
