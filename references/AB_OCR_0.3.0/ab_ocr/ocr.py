"""Local OCR adapters. No network access or automatic language fallback."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
import csv
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time

LANGUAGE = re.compile(r"[A-Za-z][A-Za-z0-9_/-]*")
MAX_PIXELS = 40_000_000
INSTALL_HELP = ("Tesseract와 필요한 언어 데이터를 설치한 뒤 다시 실행하십시오. "
                "AB_OCR_TESSERACT 환경 변수로 tesseract.exe 경로를 지정할 수 있습니다. "
                "공식 설치 안내: https://tesseract-ocr.github.io/tessdoc/Downloads.html")


class OCRError(RuntimeError):
    pass


class OCRCancelled(OCRError):
    pass


@dataclass(frozen=True)
class RecognitionResult:
    text: str
    words: list[dict]
    source_path: str
    source_sha256: str
    page_number: int
    image_width: int
    image_height: int
    engine_version: str
    languages: str
    psm: int | None
    duration_seconds: float
    preprocessing: dict = field(default_factory=dict)
    engine_metadata: dict = field(default_factory=dict)
    engine: str = "tesseract"

    def to_dict(self) -> dict:
        return {"kind": "recognition", "schema_version": 1,
                "coordinate_system": "rendered-image pixels, xyxy",
                "confidence_note": "Engine confidence is not measured accuracy and is not comparable across engines.",
                **asdict(self)}


def file_sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def find_tesseract(executable=None) -> Path | None:
    explicit = str(executable or os.environ.get("AB_OCR_TESSERACT", "")).strip().strip('"')
    if explicit:
        path = Path(explicit).expanduser()
        if not path.is_file():
            raise OCRError("지정한 Tesseract 실행 파일을 찾을 수 없습니다: " + str(path))
        return path.resolve()
    # Prefer this project's pinned installation over an unrelated PATH engine.
    candidates = [Path(__file__).resolve().parent.parent / "tools" / "tesseract" / "tesseract.exe"]
    found = shutil.which("tesseract")
    if found:
        candidates.append(Path(found))
    for variable in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
        if base := os.environ.get(variable):
            candidates.append(Path(base) / ("Programs/Tesseract-OCR" if variable == "LOCALAPPDATA" else "Tesseract-OCR") / "tesseract.exe")
    return next((p.resolve() for p in candidates if p.is_file()), None)


def _cancelled(should_stop):
    if should_stop is not None and should_stop():
        raise OCRCancelled("인식을 취소했습니다.")


def _run(argv: list[str], *, timeout=30.0, should_stop=None, engine_name="Tesseract", include_stderr=True) -> str:
    _cancelled(should_stop)
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("실행 제한 시간은 양수여야 합니다.")
    kwargs = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    try:
        process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, shell=False, **kwargs)
    except OSError as exc:
        raise OCRError(f"{engine_name}를 실행하지 못했습니다: {exc}") from exc
    deadline = time.monotonic() + timeout
    try:
        while True:
            _cancelled(should_stop)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise OCRError("OCR 실행 제한 시간을 초과했습니다.")
            try:
                stdout, stderr = process.communicate(timeout=min(0.1, remaining))
                break
            except subprocess.TimeoutExpired:
                continue
        if process.returncode:
            detail = stderr.decode("utf-8", errors="replace").strip()[-1500:] if include_stderr else "로컬 엔진 환경과 모델 설치 상태를 확인하세요."
            raise OCRError(f"{engine_name} 실행 실패 ({process.returncode}): {detail}")
        _cancelled(should_stop)
        return stdout.decode("utf-8", errors="replace")
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()


def _data_directory(tessdata_dir=None, executable=None) -> Path | None:
    path = tessdata_dir or os.environ.get("AB_OCR_TESSDATA")
    if path:
        directory = Path(path).expanduser().resolve()
        if not directory.is_dir():
            raise OCRError("언어 데이터 폴더를 찾을 수 없습니다.")
        return directory
    if executable:
        adjacent = Path(executable).parent / "tessdata"
        if adjacent.is_dir():
            return adjacent.resolve()
    return None


def _data_options(tessdata_dir=None, executable=None) -> list[str]:
    directory = _data_directory(tessdata_dir, executable)
    return ["--tessdata-dir", str(directory)] if directory else []


def doctor(*, engine="tesseract", executable=None, tessdata_dir=None, should_stop=None) -> dict:
    if engine != "tesseract":
        from .local_engines import inspect_engine
        _cancelled(should_stop)
        return inspect_engine(engine, should_stop=should_stop)
    try:
        _cancelled(should_stop)
        engine = find_tesseract(executable)
        if engine is None:
            return {"available": False, "engine": "tesseract", "languages": [], "message": INSTALL_HELP}
        version = _run([str(engine), "--version"], should_stop=should_stop).splitlines()[0]
        directory = _data_directory(tessdata_dir, engine)
        output = _run([str(engine), "--list-langs", *_data_options(tessdata_dir, engine)], should_stop=should_stop)
        languages = [s.strip() for s in output.splitlines() if LANGUAGE.fullmatch(s.strip())]
        return {"available": True, "engine": "tesseract", "executable": str(engine),
                "version": version, "languages": sorted(set(languages)),
                "tessdata_dir": str(directory) if directory else None,
                "message": "로컬 엔진 사용 가능. 설치 언어 목록을 확인하십시오."}
    except OCRCancelled:
        raise
    except (OCRError, IndexError) as exc:
        return {"available": False, "engine": "tesseract", "languages": [], "message": str(exc)}


def document_info(path: str | Path, *, should_stop=None) -> dict:
    """Inspect page count without changing source, rejecting concurrent changes."""
    source = Path(path).expanduser().resolve()
    if not source.is_file():
        raise OCRError("원본 파일을 찾을 수 없습니다.")
    _cancelled(should_stop)
    before = file_sha256(source)
    try:
        if source.suffix.lower() == ".pdf":
            import pymupdf
            with pymupdf.open(source) as document:
                if document.needs_pass:
                    raise OCRError("암호가 걸린 PDF입니다. 암호를 해제한 사본을 선택하십시오.")
                count, kind = len(document), "pdf"
        else:
            from PIL import Image
            with Image.open(source) as image:
                count, kind = getattr(image, "n_frames", 1), "image"
    except ImportError as exc:
        raise OCRError("문서 처리를 위해 pip install 'ab-ocr[ocr]'를 실행하십시오.") from exc
    except OCRError:
        raise
    except Exception as exc:
        raise OCRError(f"지원되는 이미지/PDF 문서를 열 수 없습니다: {exc}") from exc
    if count < 1:
        raise OCRError("문서에 쪽이 없습니다.")
    _cancelled(should_stop)
    if file_sha256(source) != before:
        raise OCRError("문서를 여는 동안 원본 파일이 변경되어 다시 열어야 합니다.")
    return {"page_count": count, "source_sha256": before, "source_path": str(source), "kind": kind}


def render_page(path: str | Path, page_number: int = 1, dpi: int = 150, *, should_stop=None):
    """Return a detached RGB Pillow image in the OCR display coordinate frame.

    PDF dimensions scale with DPI; raster images preserve their native pixels.
    Multi-page TIFF/GIF frame selection and EXIF orientation match recognition.
    The caller owns the returned image and should close it when no longer used.
    """
    from PIL import Image
    _cancelled(should_stop)
    source = Path(path).expanduser().resolve()
    if not source.is_file():
        raise OCRError("원본 파일을 찾을 수 없습니다.")
    with tempfile.TemporaryDirectory(prefix="abocr-preview-") as folder:
        temporary = Path(folder) / "page.png"
        details = {}
        _render(source, temporary, page_number, dpi, details=details)
        _cancelled(should_stop)
        with Image.open(temporary) as image:
            result = image.convert("RGB")
            result.info["ab_ocr_preprocessing"] = details
            return result


_HASH_CACHE: dict[tuple[str, int, int], str] = {}


def _cached_file_sha256(path: Path) -> str:
    stat = path.stat()
    key = (str(path), stat.st_size, stat.st_mtime_ns)
    if key not in _HASH_CACHE:
        if len(_HASH_CACHE) > 64:
            _HASH_CACHE.clear()
        _HASH_CACHE[key] = file_sha256(path)
    return _HASH_CACHE[key]


def _engine_metadata(info: dict, selected: list[str]) -> dict:
    """Record actual model bytes so later accuracy comparisons can be reproduced."""
    metadata = {"oem": 1, "model_files": {}}
    executable = Path(info["executable"])
    if executable.is_file():
        metadata["executable_sha256"] = _cached_file_sha256(executable)
    directory = _data_directory(info.get("tessdata_dir"), executable)
    if directory:
        for language in selected:
            model = directory / f"{language}.traineddata"
            if model.is_file():
                metadata["model_files"][language] = {"sha256": _cached_file_sha256(model), "bytes": model.stat().st_size}
    provenance = executable.parent / "installation.json"
    if provenance.is_file() and provenance.stat().st_size < 1_000_000:
        try:
            installation = json.loads(provenance.read_text(encoding="utf-8-sig"))
            metadata["installation"] = {key: installation[key] for key in ("distribution_url", "distribution_sha256", "models_commit", "models_family")
                                        if key in installation}
        except (OSError, ValueError, TypeError):
            pass
    return metadata


def _render(source: Path, destination: Path, page_number: int, dpi: int, *, details=None) -> tuple[int, int]:
    details = {} if details is None else details
    if type(page_number) is not int or page_number < 1:
        raise ValueError("쪽 번호는 1 이상의 정수여야 합니다.")
    if type(dpi) is not int or not 72 <= dpi <= 600:
        raise ValueError("렌더링 DPI는 72~600이어야 합니다.")
    try:
        from PIL import Image, ImageOps
    except ImportError as exc:
        raise OCRError("이미지 처리를 위해 pip install 'ab-ocr[ocr]'를 실행하십시오.") from exc
    if source.suffix.lower() == ".pdf":
        try:
            import pymupdf
        except ImportError as exc:
            raise OCRError("PDF 처리를 위해 pip install 'ab-ocr[ocr]'를 실행하십시오.") from exc
        with pymupdf.open(source) as document:
            if document.needs_pass:
                raise OCRError("암호가 걸린 PDF입니다. 암호를 해제한 사본을 선택하십시오.")
            if page_number > len(document):
                raise OCRError(f"문서는 {len(document)}쪽입니다.")
            page = document[page_number - 1]
            if page.rect.width * page.rect.height * (dpi / 72) ** 2 > MAX_PIXELS:
                raise OCRError("페이지가 너무 큽니다. 낮은 DPI로 다시 실행하십시오.")
            pixmap = page.get_pixmap(dpi=dpi, colorspace=pymupdf.csRGB, alpha=False)
            pixmap.save(destination)
            details.update(input_type="pdf", render_dpi=dpi, colorspace="RGB", annotation_rendering=True)
            return pixmap.width, pixmap.height
    with Image.open(source) as image:
        if page_number > getattr(image, "n_frames", 1):
            raise OCRError("이미지의 쪽 번호가 범위를 벗어났습니다.")
        image.seek(page_number - 1)
        if image.width * image.height > MAX_PIXELS:
            raise OCRError("이미지가 4천만 픽셀을 넘습니다. 영역을 나누어 실행하십시오.")
        details.update(input_type="image", original_mode=image.mode, exif_orientation=image.getexif().get(274, 1))
        image = ImageOps.exif_transpose(image)
        if image.mode in ("I", "F") or image.mode.startswith("I;16"):
            grayscale = image if image.mode in ("I", "F") else image.convert("I")
            low, high = grayscale.getextrema()
            if not math.isfinite(low) or not math.isfinite(high):
                raise OCRError("회색조 이미지에 유효하지 않은 픽셀 값이 있습니다.")
            if high > low:
                scale = 255.0 / (high - low)
                image = grayscale.point(lambda value: (value - low) * scale).convert("L")
            else:
                image = Image.new("L", grayscale.size, 255 if high > 0 else 0)
            details["high_bit_grayscale"] = {"conversion": "linear min-max to 8-bit", "minimum": low, "maximum": high}
        # Flatten alpha onto white; transparent pixels must not become black.
        if "A" in image.getbands() or "transparency" in image.info:
            rgba = image.convert("RGBA")
            background = Image.new("RGBA", rgba.size, "white")
            image = Image.alpha_composite(background, rgba)
            details["alpha_background"] = "white"
        image.convert("RGB").save(destination, dpi=(dpi, dpi))
        return image.width, image.height


def parse_tsv(content: str, width: int, height: int, *, strict=True, warnings=None) -> list[dict]:
    """Read word geometry, optionally preserving OCR when a few boxes are bad.

    Tesseract can emit zero-width placeholder words on actual pages. In tolerant
    mode those boxes are omitted, intersecting boxes are clipped, and every
    change is recorded. The separately produced raw TXT is never rebuilt from
    these boxes. Strict mode remains available for validating stored records.
    """
    words = []
    warnings = [] if warnings is None else warnings
    candidate_count = 0
    reader = csv.DictReader(io.StringIO(content), delimiter="\t", quoting=csv.QUOTE_NONE)
    required = {"level", "left", "top", "width", "height", "conf", "text", "block_num", "par_num", "line_num"}
    if not required.issubset(set(reader.fieldnames or [])):
        raise OCRError("OCR 영역 결과에 필수 TSV 열이 없습니다.")
    for index, row in enumerate(reader):
        if row.get("level") != "5" or not row.get("text"):
            continue
        candidate_count += 1
        try:
            x, y, w, h = (int(row[k]) for k in ("left", "top", "width", "height"))
            confidence = float(row["conf"])
            if not math.isfinite(confidence) or not -1 <= confidence <= 100:
                raise ValueError("신뢰도 범위가 올바르지 않습니다.")
            original = [x, y, x + w, y + h]
            bbox = [max(0, x), max(0, y), min(width, x + w), min(height, y + h)]
            if w <= 0 or h <= 0 or bbox[0] >= bbox[2] or bbox[1] >= bbox[3]:
                if strict:
                    raise ValueError("영역 좌표가 이미지 범위를 벗어났거나 면적이 없습니다.")
                warnings.append({"kind": "omitted_box", "tsv_row": index + 2, "original_bbox": original,
                                 "reason": "zero area or no intersection with image"})
                continue
            if bbox != original:
                if strict:
                    raise ValueError("영역 좌표가 이미지 범위를 벗어났습니다.")
                warnings.append({"kind": "clipped_box", "tsv_row": index + 2, "original_bbox": original,
                                 "bbox": bbox, "reason": "clipped to rendered image"})
            word = {"text": row["text"], "bbox": bbox,
                    "confidence": None if confidence < 0 else confidence,
                    "block": int(row["block_num"]), "paragraph": int(row["par_num"]),
                    "line": int(row["line_num"])}
            if bbox != original:
                word["original_bbox"] = original
            words.append(word)
        except (ValueError, KeyError, TypeError) as exc:
            raise OCRError(f"OCR 영역 결과를 해석하지 못했습니다: {exc}") from exc
    if candidate_count and not words:
        raise OCRError("인식된 모든 단어의 좌표가 유효하지 않아 결과를 적용하지 않습니다.")
    return words


def recognize_file(path: str | Path, *, languages="kor+chi_tra+eng", page_number=1, psm=3,
                   dpi=300, executable=None, tessdata_dir=None, timeout=180.0,
                   should_stop=None, engine="tesseract") -> RecognitionResult:
    """Recognize one selected page without altering the source or correcting characters."""
    if engine != "tesseract":
        from .local_engines import recognize_local_file
        if executable is not None or tessdata_dir is not None:
            raise ValueError("--tesseract/--tessdata는 Tesseract 엔진에서만 사용합니다.")
        if psm not in (None, 3):
            raise ValueError("PSM은 Tesseract 전용입니다. 선택한 엔진은 자체 영역 검출을 사용합니다.")
        return recognize_local_file(path, engine=engine, languages=languages, page_number=page_number,
                                    dpi=dpi, timeout=timeout, should_stop=should_stop)
    start = time.monotonic()
    source = Path(path).expanduser().resolve()
    if not source.is_file():
        raise OCRError("원본 파일을 찾을 수 없습니다.")
    if type(psm) is not int or psm not in (3, 4, 5, 6, 7, 11, 12, 13):
        raise ValueError("지원하는 영역 분할 모드: 3, 4, 5, 6, 7, 11, 12, 13")
    selected = languages.split("+")
    if not selected or any(not LANGUAGE.fullmatch(code) for code in selected):
        raise ValueError("언어 코드는 kor+chi_tra+eng처럼 입력하십시오.")
    _cancelled(should_stop)
    info = doctor(executable=executable, tessdata_dir=tessdata_dir, should_stop=should_stop)
    if not info["available"]:
        raise OCRError(info["message"])
    missing = sorted(set(selected) - set(info["languages"]))
    if missing:
        raise OCRError("설치되지 않은 언어: " + ", ".join(missing) +
                       ". 다른 언어로 자동 대체하지 않습니다. 설치 언어: " + ", ".join(info["languages"]))
    original_hash = file_sha256(source)
    _cancelled(should_stop)
    with tempfile.TemporaryDirectory(prefix="abocr-") as folder:
        temporary = Path(folder)
        image_path, output_base = temporary / "input.png", temporary / "reading"
        preprocessing = {}
        width, height = _render(source, image_path, page_number, dpi, details=preprocessing)
        _cancelled(should_stop)
        argv = [info["executable"], str(image_path), str(output_base), *_data_options(tessdata_dir, info["executable"]),
                "-l", languages, "--oem", "1", "--psm", str(psm),
                "-c", "tessedit_create_txt=1", "-c", "tessedit_create_tsv=1"]
        _run(argv, timeout=timeout, should_stop=should_stop)
        try:
            text = output_base.with_suffix(".txt").read_bytes().decode("utf-8")
            tsv = output_base.with_suffix(".tsv").read_bytes().decode("utf-8")
        except (OSError, UnicodeError) as exc:
            raise OCRError("인식 결과 텍스트/영역 파일이 없거나 UTF-8이 아닙니다.") from exc
        coordinate_warnings = []
        words = parse_tsv(tsv, width, height, strict=False, warnings=coordinate_warnings)
        if coordinate_warnings:
            preprocessing["coordinate_warnings"] = coordinate_warnings
            preprocessing["omitted_word_boxes"] = sum(warning["kind"] == "omitted_box" for warning in coordinate_warnings)
            preprocessing["clipped_word_boxes"] = sum(warning["kind"] == "clipped_box" for warning in coordinate_warnings)
            preprocessing["raw_text_preserved"] = True
    if file_sha256(source) != original_hash:
        raise OCRError("인식 중 원본 파일이 변경되어 결과를 적용하지 않습니다.")
    _cancelled(should_stop)
    return RecognitionResult(text=text, words=words, source_path=str(source), source_sha256=original_hash,
                             page_number=page_number, image_width=width, image_height=height,
                             engine_version=info["version"], languages=languages, psm=psm,
                             duration_seconds=time.monotonic() - start, preprocessing=preprocessing,
                             engine_metadata=_engine_metadata(info, selected))
