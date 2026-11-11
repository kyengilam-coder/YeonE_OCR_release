"""Measure local-first option sets on a small sample: seconds per page and CER.

Run from a source checkout on the PC that has the local engines installed:

    python tools/compare_local_options.py 원본.pdf --reference 정답.hwp ^
        --out D:\\측정\\의방집해 --engine-root "C:\\Program Files\\YeonE_OCR"

Each option set reads the same pages in its own work folder. Sets that only
change post-processing (vote, columns, text-layer witness) start from a copy
of the "default" folder, so the engines are not run again and they finish in
seconds. Error rate = document-level character error rate of the recognized
text against the reference, whitespace removed, aligned with difflib (an upper
bound close to edit distance; identical for every set, so fit for comparing
sets). Running heads and page numbers that the reference omits count as
errors equally in every set.

The reference may be .txt, .hwpx or .hwp (HWP 5, needs `pip install olefile`).
It should cover the same pages as --pages. No API is called unless --ai.
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
    "columns": (["--local-columns"], {}, True),
    "witness": (["--local-textlayer-witness"], {}, True),
    "vote+columns": (["--local-vote", "--local-columns"], {}, True),
    "enhance": (["--local-enhance", "contrast"], {}, False),
    "fast": (["--local-engine-plan", "fast"], {}, False),
}
DEFAULT_SETS = ("sequential", "default", "vote", "columns", "vote+columns")


# ------------------------------------------------------------------ reference
def hwp5_text(path: Path) -> str:
    """Body text of an HWP 5 file (paragraph text records only)."""
    try:
        import olefile
    except ImportError as error:
        raise SystemExit("HWP 정답을 읽으려면 olefile 이 필요합니다: pip install olefile "
                         "(또는 한글에서 HWPX·TXT 로 저장해 주세요)") from error
    with olefile.OleFileIO(str(path)) as ole:
        header = ole.openstream("FileHeader").read()
        compressed = bool(struct.unpack("<I", header[36:40])[0] & 1)
        if struct.unpack("<I", header[36:40])[0] & 2:
            raise SystemExit("암호가 걸린 HWP 입니다. 암호를 푼 사본을 주세요.")
        sections = sorted((e for e in ole.listdir() if e[0] == "BodyText"), key=lambda e: int(e[1][7:]))
        paragraphs = []
        for entry in sections:
            data = ole.openstream(entry).read()
            if compressed:
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
                    if code < 32:
                        # extended/inline controls occupy 8 code units
                        j += 16 if code in (1, 2, 3, 4, 5, 6, 7, 8, 9, 11, 12, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23) else 2
                        if code == 9:
                            units += "\t".encode("utf-16-le")
                        continue
                    units += record[j:j + 2]
                    j += 2
                paragraphs.append(bytes(units).decode("utf-16-le", "replace"))
    return "\n".join(paragraphs)


def hwpx_text(path: Path) -> str:
    with zipfile.ZipFile(path) as package:
        names = sorted((n for n in package.namelist() if re.fullmatch(r"Contents/section\d+\.xml", n)),
                       key=lambda n: int(re.search(r"\d+", n.rsplit("/", 1)[1]).group()))
        text = []
        for name in names:
            xml = package.read(name).decode("utf-8")
            for paragraph in re.findall(r"<hp:p\b.*?</hp:p>", xml, flags=re.S):
                text.append("".join(re.findall(r"<hp:t\b[^>]*>(.*?)</hp:t>", paragraph, flags=re.S)))
    from html import unescape
    return unescape(re.sub(r"<[^>]+>", "", "\n".join(text)))


def reference_text(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".hwp":
        return hwp5_text(path)
    if suffix == ".hwpx":
        return hwpx_text(path)
    return path.read_text(encoding="utf-8-sig")


# ------------------------------------------------------------------- measure
def compact(text: str) -> str:
    return re.sub(r"\s+", "", text)


def char_errors(reference: str, hypothesis: str) -> int:
    matcher = difflib.SequenceMatcher(None, reference, hypothesis, autojunk=False)
    return sum(max(a1 - a0, b1 - b0) for tag, a0, a1, b0, b1 in matcher.get_opcodes() if tag != "equal")


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


def page_list(spec: str | None, count: int) -> list[int]:
    if not spec:
        return list(range(1, count + 1))
    out = []
    for part in spec.split(","):
        a, _, b = part.partition("-")
        out.extend(range(int(a), int(b or a) + 1))
    return [p for p in out if 1 <= p <= count]


def run_set(name, args, source, pages, out_dir):
    extra, env_extra, from_default = SETS[name]
    work = out_dir / name
    if work.exists():
        shutil.rmtree(work)
    if from_default:
        base = out_dir / "default"
        if not (base / "readings").exists():
            raise SystemExit(f"{name}: default 를 먼저 실행해야 합니다(--sets 에 default 포함)")
        shutil.copytree(base, work)
    argv = [sys.executable, "-m", "yeone_ocr", "run", str(source), "--work", str(work), "--pdf-only",
            "--local-profile", args.profile, *extra]
    if args.pages:
        argv += ["--pages", args.pages]
    if args.mode:
        argv += ["--mode", args.mode]
    if args.engine_root:
        argv += ["--local-engine-root", str(args.engine_root)]
    if not args.ai:
        argv.append("--no-local-ai-review")
    env = {**os.environ, "PYTHONUTF8": "1", **env_extra}
    started = time.perf_counter()
    log = (out_dir / f"{name}.log").open("w", encoding="utf-8")
    try:
        code = subprocess.run(argv, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT).returncode
    finally:
        log.close()
    return work, code, time.perf_counter() - started


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("source", type=Path, help="원본 PDF")
    parser.add_argument("--reference", type=Path, help="정답 .hwp/.hwpx/.txt (없으면 속도만)")
    parser.add_argument("--out", type=Path, required=True, help="측정 폴더(세트마다 하위 폴더)")
    parser.add_argument("--pages", help="예: 2-12 (기본 전체)")
    parser.add_argument("--sets", default=",".join(DEFAULT_SETS), help="쉼표로: " + ", ".join(SETS))
    parser.add_argument("--profile", choices=("traditional", "simplified", "mixed"), default="traditional")
    parser.add_argument("--mode", choices=("auto", "vertical", "horizontal_zh"), default="horizontal_zh")
    parser.add_argument("--engine-root", type=Path, help="tools 폴더가 있는 엔진 설치 위치(예: 설치한 연어OCR 폴더)")
    parser.add_argument("--ai", action="store_true", help="의심 구간 AI 검증도 실행(API 비용 발생)")
    args = parser.parse_args(argv)

    import pymupdf
    with pymupdf.open(args.source) as document:
        pages = page_list(args.pages, len(document))
    names = [n.strip() for n in args.sets.split(",") if n.strip()]
    unknown = [n for n in names if n not in SETS]
    if unknown:
        raise SystemExit("알 수 없는 세트: " + ", ".join(unknown))
    if any(SETS[n][2] for n in names) and "default" not in names:
        names.insert(0, "default")
    names.sort(key=lambda n: (SETS[n][2], n != "sequential", n != "default"))   # fresh runs first, default before copies
    args.out.mkdir(parents=True, exist_ok=True)
    reference = compact(reference_text(args.reference)) if args.reference else None

    rows = []
    for name in names:
        print(f"▶ {name} …", flush=True)
        work, code, seconds = run_set(name, args, args.source, pages, args.out)
        text, engines = recognized(args.source, work, pages)
        row = {"set": name, "exit": code, "seconds": round(seconds, 1),
               "seconds_per_page": round(seconds / max(1, len(pages)), 1),
               "pages_read": sum(engines.values()), "engines": engines, "characters": len(compact(text))}
        if reference is not None:
            errors = char_errors(reference, compact(text))
            row.update(errors=errors, cer_percent=round(100 * errors / max(1, len(reference)), 2))
        rows.append(row)
        print(f"  {row}", flush=True)

    (args.out / "summary.json").write_text(json.dumps({"source": str(args.source), "pages": pages,
        "reference": str(args.reference) if args.reference else None,
        "reference_characters": len(reference) if reference is not None else None,
        "rows": rows}, ensure_ascii=False, indent=2), encoding="utf-8")
    with (args.out / "summary.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["세트", "종료 코드", "초", "쪽당 초", "읽은 쪽", "글자 수", "오류", "CER %", "선택 엔진"])
        for r in rows:
            writer.writerow([r["set"], r["exit"], r["seconds"], r["seconds_per_page"], r["pages_read"], r["characters"],
                             r.get("errors", ""), r.get("cer_percent", ""),
                             " ".join(f"{k}:{v}" for k, v in r["engines"].items())])
    print("\n세트          쪽당 초   CER %   선택 엔진")
    for r in rows:
        print(f"{r['set']:<13} {r['seconds_per_page']:>7}  {str(r.get('cer_percent', '-')):>6}   "
              + " ".join(f"{k}:{v}" for k, v in r["engines"].items()))
    print(f"\n결과: {args.out / 'summary.csv'}")
    return 0 if all(r["exit"] in (0, 4) for r in rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
