"""Measure local-first option sets on test documents: seconds per page and CER.

Run from a source checkout on the PC that has the local engines installed.
Give one PDF, or a folder of test files:

    python tools/compare_local_options.py "C:\\Projects\\YeonE_OCR\\TEST용 파일들" ^
        --out "C:\\Projects\\YeonE_OCR\\측정" --engine-root "<엔진 설치 폴더>"

Folder mode pairs every PDF with the answer file of the same name
(.hwpx > .hwp > .docx > .txt; .BAK is ignored). A PDF without an answer is
measured for speed only. On the first run the folder's guesses are written to
<out>/manifest.json (document type from the answer's script, printed-from-
answer detection); edit it and run again to correct them.

Each option set reads the same pages in its own work folder. Sets that only
change post-processing (vote, columns, text-layer witness) start from a copy
of the "default" folder, so engines are not run again. --resume skips
(document, set) pairs that already finished.

Error rate: document-level character error rate against the answer,
whitespace removed, aligned with difflib (a close upper bound of edit
distance, the same for every set, so fit for comparing sets). Running heads
and page numbers the answer omits count equally in every set. With --pages or
--max-pages the answer is cut to the stretch the reading aligns with.

A PDF whose own text layer already matches its answer was printed from the
answer: its pixels are cleaner than a scan, so its error rates flatter real
scans, and the text-layer witness set is skipped for it.

No API is called unless --ai.
"""
from __future__ import annotations

import argparse
import csv
import difflib
import json
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import sys
import time
import zipfile
import zlib

ROOT = Path(__file__).resolve().parents[1]

# name: (extra CLI arguments, extra environment, start from the "default" folder)
SETS = {
    "sequential": (["--local-engine-parallel", "1"],
                   {"YEONE_OCR_RESIDENT_WORKERS": "0", "YEONE_OCR_GPU_ENGINES": "0"}, False),
    "default": ([], {}, False),
    "vote": (["--local-vote"], {}, True),
    "columns": (["--local-columns"], {}, True),          # the default since two-column order became automatic
    "no-columns": (["--no-local-columns"], {}, True),
    "witness": (["--local-textlayer-witness"], {}, True),
    "vote+columns": (["--local-vote", "--local-columns"], {}, True),
    "enhance": (["--local-enhance", "contrast"], {}, False),
    "fast": (["--local-engine-plan", "fast"], {}, False),
    "no-verify": (["--no-local-line-verification"], {}, False),   # the default since 2026-11 measurements
    "verify": (["--local-line-verification"], {}, False),
    "no-easyocr": (["--local-exclude-engines", "easyocr"], {}, False),   # the horizontal default since 2026-11
    "with-easyocr": (["--local-easyocr"], {}, False),
    "gpu-vertical": ([], {"YEONE_OCR_GPU_VERTICAL": "1"}, False),   # the vertical default since 2026-11 measurements
    "no-gpu-vertical": ([], {"YEONE_OCR_GPU_VERTICAL": "0"}, False),   # Tesseract-only vertical (the old default)
    "glmocr-vertical": (["--local-exclude-engines", "surya"], {"YEONE_OCR_GPU_VERTICAL": "1"}, False),  # GLM-OCR instead of Surya
    "korean-models": ([], {"YEONE_OCR_MIXED_KOREAN_MODELS": "1"}, False),   # trial: Korean-only CPU models on mixed pages
    "all-vertical": ([], {"YEONE_OCR_GPU_VERTICAL": "1", "YEONE_OCR_MIXED_KOREAN_MODELS": "1"}, False),
    # Claude subscription review (claude -p) of the suspicious spans, its certain proposals applied.
    "ai-subscription": (["--local-ai-auth", "subscription", "--local-ai-accept", "--local-ai-max-calls", "100",
                         "--local-ai-batch", "4"], {}, False),
    # ... with full-resolution detail tiles of large crops (models shrink a page-size image).
    "ai-subscription-tiles": (["--local-ai-auth", "subscription", "--local-ai-accept", "--local-ai-max-calls", "100",
                               "--local-ai-batch", "4", "--local-ai-detail-tile", "1024"], {}, False),
    # Claude (subscription) reads each whole page from full-resolution strips as a second reading.
    "ai-page": (["--local-ai-page", "prefer"], {}, False),
    "ai-page-witness": (["--local-ai-page", "witness"], {}, False),
    # Both: the suspicious-span review with detail tiles corrects the engine reading, then the page reading is merged.
    "ai-page+review": (["--local-ai-page", "prefer", "--local-ai-auth", "subscription", "--local-ai-accept",
                        "--local-ai-max-calls", "100", "--local-ai-batch", "4", "--local-ai-detail-tile", "1024"], {}, False),
    "no-korean-forms": (["--no-local-korean-forms"], {}, False),   # engine Hanja forms as read (before 2026-11)
    "easyocr-fast": (["--local-easyocr-threads", "6", "--local-easyocr-batch", "8"], {}, False),
}
DEFAULT_SETS = ("sequential", "default", "vote", "no-columns")
ANSWER_SUFFIXES = (".hwpx", ".hwp", ".docx", ".txt")
PRINTED_SIMILARITY = 0.9


# ------------------------------------------------------------------ answers
def hwp5_text(path: Path) -> str:
    """Body text of an HWP 5 file (paragraph text records only)."""
    try:
        import olefile
    except ImportError as error:
        raise SystemExit("HWP 정답을 읽으려면 olefile 이 필요합니다: pip install olefile "
                         "(또는 한글에서 HWPX 로 저장해 주세요)") from error
    with olefile.OleFileIO(str(path)) as ole:
        flags = struct.unpack("<I", ole.openstream("FileHeader").read()[36:40])[0]
        if flags & 2:
            raise SystemExit(f"암호가 걸린 HWP 입니다: {path.name}")
        sections = sorted((e for e in ole.listdir() if e[0] == "BodyText"), key=lambda e: int(e[1][7:]))
        paragraphs = []
        for entry in sections:
            data = ole.openstream(entry).read()
            if flags & 1:
                data = zlib.decompress(data, -15)
            i = 0
            while i + 4 <= len(data):
                head = struct.unpack("<I", data[i:i + 4])[0]
                i += 4
                tag, size = head & 0x3FF, (head >> 20) & 0xFFF
                if size == 0xFFF:
                    size = struct.unpack("<I", data[i:i + 4])[0]
                    i += 4
                record, i = data[i:i + size], i + size
                if tag != 67:                                    # HWPTAG_PARA_TEXT
                    continue
                units, j = bytearray(), 0
                while j + 2 <= len(record):
                    code = struct.unpack("<H", record[j:j + 2])[0]
                    if code < 32:                                # controls; inline/extended ones span 8 units
                        j += 16 if code in (1, 2, 3, 4, 5, 6, 7, 8, 9, 11, 12, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23) else 2
                        continue
                    units += record[j:j + 2]
                    j += 2
                paragraphs.append(bytes(units).decode("utf-16-le", "replace"))
    return "\n".join(paragraphs)


def hwpx_text(path: Path) -> str:
    """Body text of an HWPX file; memo contents (inside field controls) are left out."""
    from html import unescape
    with zipfile.ZipFile(path) as package:
        names = sorted((n for n in package.namelist() if re.fullmatch(r"Contents/section\d+\.xml", n)),
                       key=lambda n: int(re.search(r"\d+", n.rsplit("/", 1)[1]).group()))
        parts = []
        for name in names:
            xml = package.read(name).decode("utf-8")
            xml = re.sub(r"<hp:fieldBegin\b.*?</hp:fieldBegin>", "", xml, flags=re.S)
            xml = xml.replace("</hp:p>", "\n")
            pieces = re.findall(r"<hp:t\b[^>]*>(.*?)</hp:t>|(\n)", xml, flags=re.S)
            parts.append("".join(text or newline for text, newline in pieces))
    return unescape(re.sub(r"<[^>]+>", "", "".join(parts)))


def docx_text(path: Path) -> str:
    import docx
    document = docx.Document(str(path))
    lines = [p.text for p in document.paragraphs]
    for table in document.tables:
        lines.extend(cell.text for row in table.rows for cell in row.cells)
    return "\n".join(lines)


def reference_text(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".hwp":
        return hwp5_text(path)
    if suffix == ".hwpx":
        return hwpx_text(path)
    if suffix == ".docx":
        return docx_text(path)
    return path.read_text(encoding="utf-8-sig")


def guess_profile(text: str) -> str:
    """mixed if Hangul is a real share of the letters, else simplified/traditional by OpenCC."""
    letters = [c for c in text if c.isalpha()]
    hangul = sum(1 for c in letters if "\uac00" <= c <= "\ud7a3")
    if letters and hangul / len(letters) > 0.1:
        return "mixed"
    try:
        import opencc
    except ImportError:
        return "traditional"
    han = "".join(c for c in letters if "\u3400" <= c <= "\u9fff")[:20000]
    simplified = sum(a != b for a, b in zip(han, opencc.OpenCC("s2t").convert(han)))
    traditional = sum(a != b for a, b in zip(han, opencc.OpenCC("t2s").convert(han)))
    return "simplified" if simplified > traditional else "traditional"


def pick_answer(pdf: Path) -> Path | None:
    for suffix in ANSWER_SUFFIXES:
        candidate = pdf.with_suffix(suffix)
        if candidate.is_file():
            return candidate
    return None


# ------------------------------------------------------------------- measure
def compact(text: str) -> str:
    return re.sub(r"\s+", "", text)


def content(text: str) -> str:
    """Letters and digits only: punctuation width/kind (，/, ．/.) and symbols are not counted."""
    import unicodedata
    return "".join(c for c in text if not unicodedata.category(c).startswith(("P", "S")))


def char_errors(reference: str, hypothesis: str) -> int:
    matcher = difflib.SequenceMatcher(None, reference, hypothesis, autojunk=False)
    return sum(max(a1 - a0, b1 - b0) for tag, a0, a1, b0, b1 in matcher.get_opcodes() if tag != "equal")


def crop_reference(reference: str, hypothesis: str, anchor: int = 8) -> str:
    """The stretch of the answer that a partial reading aligns with (outermost long matches)."""
    blocks = [b for b in difflib.SequenceMatcher(None, reference, hypothesis, autojunk=False).get_matching_blocks()
              if b.size >= anchor]
    if not blocks:
        return reference
    return reference[blocks[0].a:blocks[-1].a + blocks[-1].size]


def text_layer(pdf: Path, pages: list[int]) -> str:
    import pymupdf
    with pymupdf.open(pdf) as document:
        return "".join(document[n - 1].get_text() for n in pages)


def printed_from_answer(layer: str, reference: str) -> bool:
    """Most of the PDF's own text (for the measured pages) is found in the answer, in order.

    Measured against the text layer's length, so a few pages of a long answer still count.
    """
    a = compact(layer)[:20000]
    if len(a) < 20 or not reference:
        return False
    matcher = difflib.SequenceMatcher(None, a, reference, autojunk=False)
    matched = sum(block.size for block in matcher.get_matching_blocks())
    return matched / len(a) >= PRINTED_SIMILARITY


def recognized(source: Path, work: Path, pages: list[int]) -> tuple[str, dict]:
    sys.path.insert(0, str(ROOT))
    from yeone_ocr.blocks import page_text_of
    from yeone_ocr.schema import PageReading
    from yeone_ocr.state import Work
    w = Work(str(source), str(work), initialize=False)
    texts, engines = [], {}
    for number in pages:
        record = w.load_record(number)
        if not record:
            continue
        texts.append(page_text_of(PageReading.model_validate(record["merged"])))
        engines[record.get("model", "?")] = engines.get(record.get("model", "?"), 0) + 1
    return "\n".join(texts), engines


def ai_review(source: Path, work: Path, pages: list[int]) -> tuple[dict, list[str]]:
    """AI review totals (calls, applied, held) and one line per AI proposal, from the page records."""
    sys.path.insert(0, str(ROOT))
    from yeone_ocr.state import Work
    w = Work(str(source), str(work), initialize=False)
    totals, lines = {"calls": 0, "accepted": 0, "held": 0}, []
    for number in pages:
        record = w.load_record(number) or {}
        local = record.get("local_first") or {}
        totals["calls"] += (record.get("usage") or {}).get("calls", 0)
        accept = (local.get("review") or {}).get("ai_accept") or {}
        totals["accepted"] += accept.get("accepted", 0)
        totals["held"] += accept.get("held", 0)
        page = local.get("ai_page")
        if page:
            totals["calls"] += 0 if page.get("cached") else 1          # one AI page reading per page
            for key in ("taken", "witness", "kept_long"):
                totals[f"page_{key}"] = totals.get(f"page_{key}", 0) + page.get(key, 0)
            lines.append(f"{number}쪽\tAI 쪽 판독\t유사도 {page.get('ratio')} · 채택 {page.get('taken', 0)} · "
                         f"파랑 {page.get('witness', 0)} · 빨강(긴 차이) {page.get('kept_long', 0)}\t"
                         + ("쓰지 않음(엔진 판독과 너무 다름)" if not page.get("used") else ", ".join(page.get("uncertain") or [])))
        for item in local.get("ai_proposals") or []:
            state = item.get("decision") or "?"
            why = item.get("blocked_reason") or ("AI 불확실" if item.get("uncertain") else "")
            lines.append(f"{number}쪽\t{state}\t{item.get('before')!s} → {item.get('after')!s}\t{why}")
    return totals, lines


def error_lines(reference: str, hypothesis: str, context: int = 6) -> list[str]:
    """Every difference against the answer, with a little context: 정답 → 판독."""
    matcher = difflib.SequenceMatcher(None, reference, hypothesis, autojunk=False)
    out = []
    for tag, a0, a1, b0, b1 in matcher.get_opcodes():
        if tag != "equal":
            out.append(f"{reference[max(0, a0 - context):a0]}[{reference[a0:a1]}→{hypothesis[b0:b1]}]{reference[a1:a1 + context]}")
    return out


def write_lists(doc, work: Path, folder: Path) -> None:
    """errors.txt / ai_proposals.txt from an existing work folder, without reading anything again."""
    if not work.is_dir():
        return
    if doc.get("_reference") and not (folder / "errors.txt").is_file():
        hyp = compact(recognized(Path(doc["pdf"]), work, doc["pages"])[0])
        reference = crop_reference(doc["_reference"], hyp) if doc.get("_partial") else doc["_reference"]
        (folder / "errors.txt").write_text("\ufeff정답 → 판독 차이 (공백 무시)\n" + "\n".join(error_lines(reference, hyp)) + "\n",
                                           encoding="utf-8")
    if not (folder / "ai_proposals.txt").is_file():
        totals, lines = ai_review(Path(doc["pdf"]), work, doc["pages"])
        if totals["calls"] or lines:
            (folder / "ai_proposals.txt").write_text("\ufeff쪽\t결정\t변경 전 → 변경 후\t보류 이유\n" + "\n".join(lines) + "\n",
                                                     encoding="utf-8")
            print(f"  {doc['name']} · {folder.name} AI 검증: 호출 {totals['calls']}회 · 반영 {totals['accepted']}개 · "
                  f"보류 {totals['held']}개", flush=True)


def engine_seconds(work: Path, pages: list[int]) -> dict:
    """Seconds each engine spent recognizing the measured pages (from the comparison project).

    Engines run in parallel in the new mode, so these add up to more than the wall time;
    the largest one is the page's bottleneck.
    """
    totals: dict[str, float] = {}
    for path in (work / "comparison").glob("*.yeone-compare.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for number, page in (data.get("pages") or {}).items():
            if int(number) not in pages:
                continue
            for run in page.get("runs", []):
                seconds = (run.get("raw") or {}).get("duration_seconds")
                if isinstance(seconds, (int, float)):
                    totals[run.get("engine", "?")] = totals.get(run.get("engine", "?"), 0.0) + seconds
    return {engine: round(value, 1) for engine, value in sorted(totals.items(), key=lambda item: -item[1])}


def page_list(spec: str | None, count: int, limit: int = 0) -> list[int]:
    if not spec:
        pages = list(range(1, count + 1))
    else:
        pages = []
        for part in spec.split(","):
            a, _, b = part.partition("-")
            pages.extend(range(int(a), int(b or a) + 1))
        pages = [p for p in pages if 1 <= p <= count]
    return pages[:limit] if limit else pages


def spec_of(pages: list[int]) -> str:
    out, i = [], 0
    while i < len(pages):
        j = i
        while j + 1 < len(pages) and pages[j + 1] == pages[j] + 1:
            j += 1
        out.append(str(pages[i]) if i == j else f"{pages[i]}-{pages[j]}")
        i = j + 1
    return ",".join(out)


class FolderBusy(RuntimeError):
    pass


def clear_folder(work: Path, *, attempts=5, sleep=time.sleep) -> None:
    """Remove a previous set folder. On Windows another program (an open file, an Explorer or
    PowerShell window inside it, an indexer or antivirus scan) can hold it for a moment:
    retry, then move it aside, and only then give up with the reason in Korean."""
    def writable(function, path, _info):
        os.chmod(path, 0o700)                       # read-only files block deletion on Windows
        function(path)
    for attempt in range(attempts):
        try:
            shutil.rmtree(work, **({"onexc": writable} if sys.version_info >= (3, 12) else {"onerror": writable}))
            return
        except FileNotFoundError:
            return
        except OSError:
            if attempt + 1 < attempts:
                sleep(1 + attempt)
    aside = work.with_name(f"{work.name}.이전-{time.strftime('%Y%m%d-%H%M%S')}")
    try:
        work.rename(aside)
        print(f"  이전 결과 폴더를 지우지 못해 옆으로 옮겼습니다: {aside.name} (나중에 지워도 됩니다)", flush=True)
    except OSError as error:
        raise FolderBusy(f"이전 결과 폴더를 지우지 못했습니다: {work}\n"
                         "  그 폴더 안의 파일을 연 프로그램(메모장 등), 그 폴더를 연 탐색기·PowerShell 창을 닫고 다시 실행하세요. "
                         f"({error})") from error


def run_set(name, doc, args, out_dir):
    extra, env_extra, from_default = SETS[name]
    work = out_dir / name
    if work.exists():
        clear_folder(work)
    if from_default:
        shutil.copytree(out_dir / "default", work, ignore=shutil.ignore_patterns("result.json"))
    argv = [sys.executable, "-m", "yeone_ocr", "run", str(doc["pdf"]), "--work", str(work), "--pdf-only",
            "--local-profile", doc["profile"], "--mode", doc.get("mode", "auto"), "--pages", spec_of(doc["pages"]), *extra]
    if args.engine_root:
        argv += ["--local-engine-root", str(args.engine_root)]
    if not args.ai and "--local-ai-auth" not in extra:
        argv.append("--no-local-ai-review")
    env = {**os.environ, "PYTHONUTF8": "1", **env_extra}
    started = time.perf_counter()
    total = len(doc["pages"])
    # The recognizer writes straight into the log file and this process only reads it back.
    # If the console stops printing (a click puts Windows PowerShell into selection mode),
    # only the on-screen progress waits; recognition itself keeps running.
    log_path = out_dir / f"{name}.log"
    state = {"offset": 3, "pending": b"", "done": 0}

    def show(text):
        text = text.strip()
        if re.match(r"\d+쪽: \S+ · 로컬 ", text):                       # the one line local-first prints per finished page
            state["done"] += 1
            print(f"    [{state['done']}/{total}쪽 · {time.perf_counter() - started:.0f}초] {text}", flush=True)
        elif re.search(r"오류|실패|GPU|제외|Traceback|Error", text) and "MuPDF" not in text:
            print(f"    {text}", flush=True)

    def drain(final=False):
        with log_path.open("rb") as reader:
            reader.seek(state["offset"])
            chunk = reader.read()
        state["offset"] += len(chunk)
        lines = (state["pending"] + chunk).split(b"\n")
        state["pending"] = b"" if final else lines.pop()
        for raw in lines:
            show(raw.decode("utf-8", "replace"))

    with log_path.open("wb") as sink:
        sink.write(b"\xef\xbb\xbf")                                # BOM: PowerShell's Get-Content then shows Korean
        sink.flush()
        process = subprocess.Popen(argv, cwd=ROOT, env=env, stdout=sink, stderr=subprocess.STDOUT)
        try:
            while process.poll() is None:
                drain()
                time.sleep(0.5)
        finally:
            code = process.wait()
    drain(final=True)
    return work, code, time.perf_counter() - started


# --------------------------------------------------------------- documents
def discover(args) -> list[dict]:
    import pymupdf
    source = args.source
    pdfs = sorted(source.glob("*.pdf")) if source.is_dir() else [source]
    manifest_path = args.out / "manifest.json"
    saved = {}
    if manifest_path.is_file():
        saved = {d["pdf"]: d for d in json.loads(manifest_path.read_text(encoding="utf-8"))["documents"]}
    docs = []
    for pdf in pdfs:
        key = str(pdf)
        entry = dict(saved.get(key, {}))
        if entry.get("skip"):
            docs.append(entry)
            continue
        answer = Path(entry["answer"]) if entry.get("answer") else (args.reference if not source.is_dir() else pick_answer(pdf))
        with pymupdf.open(pdf) as document:
            count = len(document)
        # The command decides the pages each run; "pages_override" in manifest.json pins one document.
        pages = page_list(entry.get("pages_override") or args.pages, count, args.max_pages)
        reference = compact(reference_text(answer)) if answer else None
        if args.profile:
            entry["profile"], entry["profile_source"] = args.profile, "option"
        elif "profile" not in entry:
            entry["profile"] = guess_profile(reference) if reference else "traditional"
            entry["profile_source"] = "answer" if reference else "default"
        elif entry.get("profile_confirmed"):
            entry["profile_source"] = "manifest"
        if "printed_override" in entry:                         # set by hand in manifest.json
            entry["printed_from_answer"] = bool(entry["printed_override"])
        else:
            entry["printed_from_answer"] = bool(reference) and printed_from_answer(text_layer(pdf, pages), reference)
        entry.update(pdf=key, name=pdf.stem, answer=str(answer) if answer else None, page_count=count,
                     pages_spec=spec_of(pages), mode=entry.get("mode", args.mode), skip=entry.get("skip", False))
        entry["pages"], entry["_reference"], entry["_partial"] = pages, reference, len(pages) < count
        docs.append(entry)
    args.out.mkdir(parents=True, exist_ok=True)
    public = [{k: v for k, v in d.items() if not k.startswith("_") and k != "pages"} for d in docs]
    manifest_path.write_text(json.dumps({"note": "profile(+profile_confirmed)·mode(auto/vertical/horizontal_zh)·answer·skip·"
                                                 "pages_override(예 \"2-8\")·printed_override(true/false) 를 고쳐 다시 실행하면 그대로 씁니다. "
                                                 "pages_spec 은 마지막 실행의 쪽(참고용).",
                                         "documents": public}, ensure_ascii=False, indent=2), encoding="utf-8")
    return docs


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("source", type=Path, help="원본 PDF 하나, 또는 시험 파일 폴더")
    parser.add_argument("--reference", type=Path, help="(PDF 하나일 때) 정답 .hwpx/.hwp/.docx/.txt")
    parser.add_argument("--out", type=Path, required=True, help="측정 폴더(문서·세트마다 하위 폴더)")
    parser.add_argument("--pages", help="예: 2-12 (기본 전체)")
    parser.add_argument("--max-pages", type=int, default=0, help="문서마다 앞에서부터 이 쪽수까지만(0=전부)")
    parser.add_argument("--sets", default=",".join(DEFAULT_SETS), help="쉼표로: " + ", ".join(SETS))
    parser.add_argument("--profile", choices=("traditional", "simplified", "mixed"), help="모든 문서에 강제(기본: 정답 글로 추정)")
    parser.add_argument("--mode", choices=("auto", "vertical", "horizontal_zh"), default="auto")
    parser.add_argument("--engine-root", type=Path, help="tools 폴더가 있는 엔진 설치 위치")
    parser.add_argument("--ai", action="store_true", help="의심 구간 AI 검증도 실행(API 비용 발생)")
    parser.add_argument("--resume", action="store_true", help="이미 끝난 (문서, 세트)는 건너뜀")
    parser.add_argument("--redo", default="", help="--resume 이어도 다시 계산할 세트(쉼표로). 예: columns,vote+columns — "
                                                    "코드를 고친 뒤 후처리 세트만 새로 잴 때(default 의 엔진 결과는 재사용)")
    parser.add_argument("--only", help="이 글자가 이름에 든 문서만(쉼표로 여러 개)")
    args = parser.parse_args(argv)

    names = [n.strip() for n in args.sets.split(",") if n.strip()]
    unknown = [n for n in names if n not in SETS]
    if unknown:
        raise SystemExit("알 수 없는 세트: " + ", ".join(unknown))
    if any(SETS[n][2] for n in names) and "default" not in names:
        names.insert(0, "default")
    names.sort(key=lambda n: (SETS[n][2], n != "sequential", n != "default"))   # fresh runs first, default before copies

    docs = [d for d in discover(args) if not d.get("skip")]
    if args.only:
        wanted = [w.strip() for w in args.only.split(",") if w.strip()]
        docs = [d for d in docs if any(w in d["name"] for w in wanted)]
    print(f"문서 {len(docs)}개 · 세트 {', '.join(names)} · 측정 폴더 {args.out}", flush=True)
    for d in docs:
        print(f"  {d['name']}: {len(d['pages'])}쪽 · {d['profile']} · 정답 {Path(d['answer']).name if d['answer'] else '없음(속도만)'}"
              + (" · 정답에서 인쇄한 PDF(스캔보다 쉬움)" if d["printed_from_answer"] else ""), flush=True)
        if d.get("profile_source") == "default":
            print(f"    ⚠ 정답이 없어 문서 종류를 추정하지 못해 traditional 로 둠 — {args.out / 'manifest.json'} 에서 "
                  f"profile 을 고치고 \"profile_confirmed\": true 를 넣은 뒤 다시 실행하세요", flush=True)

    rows = []
    for d in docs:
        doc_dir = args.out / re.sub(r'[<>:"/\\|?*]', "_", d["name"])
        doc_dir.mkdir(parents=True, exist_ok=True)
        for name in names:
            if name == "witness" and d["printed_from_answer"]:
                continue                                         # its text layer is the answer itself
            result_path = doc_dir / name / "result.json"
            redo = {n.strip() for n in args.redo.split(",") if n.strip()}
            if args.resume and result_path.is_file() and name not in redo:
                previous = json.loads(result_path.read_text(encoding="utf-8"))
                if previous.get("pages_spec") == d["pages_spec"]:          # same pages only
                    write_lists(d, doc_dir / name, result_path.parent)    # lists for runs made before they existed
                    rows.append(previous)
                    continue
            print(f"▶ {d['name']} · {name} …", flush=True)
            try:
                work, code, seconds = run_set(name, d, args, doc_dir)
            except FolderBusy as error:
                print(f"  {error}\n  이 세트는 건너뜁니다.", flush=True)
                continue
            text, engines = recognized(Path(d["pdf"]), work, d["pages"])
            hyp = compact(text)
            row = {"document": d["name"], "set": name, "exit": code, "pages": len(d["pages"]), "pages_spec": d["pages_spec"],
                   "seconds": round(seconds, 1),
                   "seconds_per_page": round(seconds / max(1, len(d["pages"])), 1), "pages_read": sum(engines.values()),
                   "engines": engines, "characters": len(hyp), "profile": d["profile"],
                   "printed_from_answer": d["printed_from_answer"],
                   "engine_seconds": {} if SETS[name][2] else engine_seconds(work, d["pages"])}
            if d["_reference"]:
                reference = crop_reference(d["_reference"], hyp) if d["_partial"] else d["_reference"]
                errors = char_errors(reference, hyp)
                letters = content(reference)
                content_errors = char_errors(letters, content(hyp))
                (result_path.parent / "errors.txt").parent.mkdir(parents=True, exist_ok=True)
                (result_path.parent / "errors.txt").write_text(
                    "\ufeff정답 → 판독 차이 (공백 무시)\n" + "\n".join(error_lines(reference, hyp)) + "\n", encoding="utf-8")
                row.update(reference_characters=len(reference), errors=errors,
                           cer_percent=round(100 * errors / max(1, len(reference)), 2),
                           content_cer_percent=round(100 * content_errors / max(1, len(letters)), 2))
            totals, proposal_lines = ai_review(Path(d["pdf"]), work, d["pages"])
            if totals["calls"] or proposal_lines:
                row["ai"] = totals
                (result_path.parent / "ai_proposals.txt").write_text(
                    "\ufeff쪽\t결정\t변경 전 → 변경 후\t보류 이유\n" + "\n".join(proposal_lines) + "\n", encoding="utf-8")
                print(f"  AI 검증: 호출 {totals['calls']}회 · 반영 {totals['accepted']}개 · 보류 {totals['held']}개", flush=True)
            result_path.parent.mkdir(parents=True, exist_ok=True)
            result_path.write_text(json.dumps(row, ensure_ascii=False, indent=2), encoding="utf-8")
            rows.append(row)
            print(f"  쪽당 {row['seconds_per_page']}초 · CER {row.get('cer_percent', '-')}% "
                  f"(부호 제외 {row.get('content_cer_percent', '-')}%) · {engines}", flush=True)
            if row["engine_seconds"]:
                print("  엔진별 판독 시간(합): " + ", ".join(f"{k} {v}초" for k, v in row["engine_seconds"].items()), flush=True)

    (args.out / "summary.json").write_text(json.dumps({"sets": names, "rows": rows}, ensure_ascii=False, indent=2), encoding="utf-8")
    with (args.out / "summary.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["문서", "세트", "종료 코드", "쪽", "초", "쪽당 초", "읽은 쪽", "글자 수", "정답 글자", "오류", "CER %", "부호 제외 CER %",
                         "문서 종류", "정답에서 인쇄", "선택 엔진", "엔진별 판독 초"])
        for r in rows:
            writer.writerow([r["document"], r["set"], r["exit"], r["pages"], r["seconds"], r["seconds_per_page"], r["pages_read"],
                             r["characters"], r.get("reference_characters", ""), r.get("errors", ""), r.get("cer_percent", ""),
                             r.get("content_cer_percent", ""),
                             r["profile"], "예" if r["printed_from_answer"] else "", " ".join(f"{k}:{v}" for k, v in r["engines"].items()),
                             " ".join(f"{k}:{v}" for k, v in (r.get("engine_seconds") or {}).items())])
    lines = ["| 문서 | " + " | ".join(names) + " |", "|---|" + "---|" * len(names)]
    for d in docs:
        cells = []
        for name in names:
            r = next((r for r in rows if r["document"] == d["name"] and r["set"] == name), None)
            cells.append("—" if r is None else
                         f"{r.get('cer_percent', '-')}% (부호 제외 {r.get('content_cer_percent', '-')}%) · {r['seconds_per_page']}초")
        lines.append(f"| {d['name']}{' (인쇄본)' if d['printed_from_answer'] else ''} | " + " | ".join(cells) + " |")
    table = "\n".join(lines)
    (args.out / "summary.md").write_text("# 로컬 우선 옵션 비교 (CER · 부호 제외 CER · 쪽당 초)\n\n" + table + "\n", encoding="utf-8")
    print("\n" + table + f"\n\n결과: {args.out / 'summary.csv'} · {args.out / 'summary.md'}")
    return 0 if all(r["exit"] in (0, 4) for r in rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
