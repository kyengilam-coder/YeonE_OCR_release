"""Read existing jobs without rewriting them; export explicitly selected text.

Legacy readings may already contain normalization performed by older versions.
Their exact JSON snapshot is retained, never presented as an untouched API reply.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import zipfile
from xml.etree import ElementTree as ET


def default_project_path(source, work_dir=None):
    from ab_ocr.ocr import file_sha256
    from .state import default_work_dir
    source = Path(source).expanduser().resolve()
    root = Path(work_dir) if work_dir else default_work_dir(str(source))
    return root / "comparison" / (file_sha256(source)[:24] + ".yeone-compare.json")


def open_project(source, work_dir=None):
    from .comparison import ComparisonProject
    from .state import default_work_dir
    path = default_project_path(source, work_dir)
    if path.exists():
        return ComparisonProject.load(path, source_path=source)
    project = ComparisonProject.open(source)
    folder = Path(work_dir) if work_dir else default_work_dir(str(source))
    project.import_notices = []
    if (folder / "readings").is_dir():
        try:
            import_yeone_records(project, folder)
        except (OSError, ValueError) as exc:
            project.import_notices.append(str(exc))
    path.parent.mkdir(parents=True, exist_ok=True)
    project.save(path)
    return project


def _already_imported(project, number, digest, stage):
    return any(run["raw"].get("engine_metadata", {}).get("import", {}).get("record_sha256") == digest
               and run["raw"].get("engine_metadata", {}).get("import", {}).get("stage") == stage
               for run in project.page(number)["runs"])


def _legacy_reading(reading):
    """Derive display text without Unicode folding; original structure is kept."""
    from .schema import PageReading, IMAGE_KINDS, SKIP_KINDS, TITLE_KINDS, region_text
    page = PageReading.model_validate(reading)
    parts, regions = [], []
    for index, region in enumerate(page.regions):
        if region.kind in IMAGE_KINDS or region.kind in SKIP_KINDS:
            continue
        if region.table_rows:
            text = "\n".join("\t".join(row.cells) for row in region.table_rows)
        else:
            text = region.title_text if region.kind in TITLE_KINDS else region_text(region)
        if text:
            parts.append(text)
            regions.append((index, text, region.bbox))
    return "\n".join(parts), regions


def import_yeone_records(project, work_dir):
    """Import an authenticated legacy work directory. Existing files stay intact."""
    from ab_ocr.ocr import render_page
    from .state import Work
    project.validate_source()
    work = Work(str(project.source_path), str(work_dir), initialize=False)
    if not work.source_matches():
        raise ValueError("기존 판독 기록의 원본 지문을 확인할 수 없습니다. 기존 기록은 보존하고 새 비교 프로젝트를 사용합니다.")
    count = 0
    for path in sorted((Path(work_dir) / "readings").glob("p*.json")):
        if path.stat().st_size > 16_000_000:
            raise ValueError("기존 쪽 기록이 너무 큽니다. 쪽별로 가져오세요.")
        content = path.read_bytes()
        record = json.loads(content.decode("utf-8-sig"))
        number = record.get("pdf_page")
        if type(number) is not int or not 1 <= number <= project.page_count:
            raise ValueError("기존 판독 기록의 쪽 번호가 올바르지 않습니다.")
        digest = hashlib.sha256(content).hexdigest()
        added = False
        was_empty = not project.page(number)["runs"]
        size = None
        for stage in ("pass1", "merged"):
            reading = record.get(stage)
            if not isinstance(reading, dict) or _already_imported(project, number, digest, stage):
                continue
            text, regions = _legacy_reading(reading)
            if size is None:
                image = render_page(project.source_path, number, dpi=72)
                size = image.size
                image.close()
            width, height = size
            words = []
            for index, region_text_value, bbox in regions:
                y0, x0, y1, x1 = bbox
                words.append({"text": region_text_value,
                    "bbox": [x0 * width / 1000, y0 * height / 1000, x1 * width / 1000, y1 * height / 1000],
                    "confidence": None, "block": index, "paragraph": 0, "line": 0})
            raw = {"kind": "recognition", "schema_version": 1, "engine": "yeone-" + stage,
                "coordinate_system": "rendered-image pixels, xyxy",
                "text": text, "words": words, "source_path": str(project.source_path),
                "source_sha256": project.source_sha256, "page_number": number,
                "image_width": width, "image_height": height,
                "engine_version": str(record.get("version", "legacy")),
                "languages": "legacy", "psm": None, "duration_seconds": 0,
                "preprocessing": {"input_type": "legacy-display-derivation", "render_dpi": 72},
                "engine_metadata": {"coordinate_granularity": "region; not glyph",
                    "legacy_normalization_may_already_be_applied": True,
                    "import": {"kind": "yeone", "stage": stage, "record_sha256": digest,
                               "record": copy.deepcopy(record), "original_json": content.decode("utf-8-sig")}}}
            project.add_result(raw, activate=was_empty)
            added = True
        if added:
            # Initial opening uses the previous document's final text. Later
            # imports do not replace already edited text or the selected run.
            page = project.page(number)
            if was_empty and page["corrected_text"] is None and not page["edits"]:
                project.choose_baseline(number, "yeone-merged" if record.get("merged") else "yeone-pass1")
            count += 1
    return count


def import_ab_project(project, path):
    from ab_ocr.document import DocumentProject
    project.validate_source()
    source = DocumentProject.load(path, source_path=project.source_path)
    digest = hashlib.sha256(Path(path).read_bytes()).hexdigest()
    count = 0
    for number in source.completed_pages:
        old = source.page(number)
        if _already_imported(project, number, digest, "ab-raw"):
            continue
        was_empty = not project.page(number)["runs"]
        raw = copy.deepcopy(old.raw)
        raw.setdefault("coordinate_system", "rendered-image pixels, xyxy")
        metadata = raw.setdefault("engine_metadata", {})
        metadata["import"] = {"kind": "ab-project", "stage": "ab-raw", "record_sha256": digest,
                              "raw_snapshot": copy.deepcopy(old.raw),
                              "page_snapshot": {"raw": copy.deepcopy(old.raw),
                                  "previous_recognitions": copy.deepcopy(old.previous_recognitions),
                                  "corrected_text": old.corrected_text, "reviewed": old.reviewed,
                                  "last_error": old.last_error}}
        project.add_result(raw, activate=was_empty)
        if was_empty:
            project.choose_baseline(number, raw.get("engine", "tesseract"))
        if was_empty and old.corrected_text is not None and project.page(number)["corrected_text"] is None:
            project.set_text(number, old.corrected_text, reason="imported AB user correction")
            project.set_reviewed(number, old.reviewed)
        count += 1
    return count


def _export_blocks(project, numbers):
    from .blocks import Block
    blocks, expected = [], []
    for number in numbers:
        page = project.page(number)
        if not page["runs"] and page["corrected_text"] is None:
            raise ValueError("선택한 쪽에 미처리 쪽이 있습니다. 완료된 쪽을 골라 내보내세요.")
        text = project.text(number)
        # No inferred headings, word replacements, or simplified/traditional
        # conversion. Paragraph boundaries follow explicit line breaks only.
        for line in text.split("\n"):
            blocks.append(Block(type="para", text=line, page=number))
            expected.append(line)
    return blocks, expected


def _read_export(path, format):
    def paragraph_text(paragraph):
        chunks = []
        for node in paragraph.iter():
            tag = node.tag.rsplit("}", 1)[-1]
            if tag == "t":
                chunks.append(node.text or "")
            elif tag == "tab":
                chunks.append("\t")
            elif tag == "br":
                chunks.append("\n")
        return "".join(chunks)
    with zipfile.ZipFile(path) as archive:
        if format == "docx":
            root = ET.fromstring(archive.read("word/document.xml"))
            ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
            return [paragraph_text(p) for p in root.findall(".//w:body/w:p", ns)]
        names = sorted(n for n in archive.namelist() if n.startswith("Contents/section") and n.endswith(".xml"))
        ns = {"hp": "http://www.hancom.co.kr/hwpml/2011/paragraph"}
        return [paragraph_text(p) for name in names
                for p in ET.fromstring(archive.read(name)).findall("hp:p", ns)]


def export_project(project, path, format, page_numbers=None):
    """Atomically publish only an export that reopens with the same characters."""
    format = format.lower().lstrip(".")
    if format not in ("txt", "hwpx", "docx"):
        raise ValueError("TXT, HWPX, DOCX를 선택하세요.")
    target = Path(path).expanduser().resolve()
    if target.suffix.lower() != "." + format:
        raise ValueError("출력 형식과 확장자가 일치하지 않습니다.")
    protected = [project.source_path, project.project_path]
    for original in protected:
        if original and (target == Path(original).resolve() or
                (target.exists() and Path(original).exists() and os.path.samefile(target, original))):
            raise ValueError("원본 또는 비교 프로젝트를 덮어쓸 수 없습니다.")
    numbers = list(page_numbers) if page_numbers is not None else list(project.completed_pages)
    if not numbers or len(numbers) != len(set(numbers)):
        raise ValueError("내보낼 쪽을 중복 없이 선택하세요.")
    project.validate_source()
    if format == "txt":
        return project.export_text(target, page_numbers=numbers)
    blocks, expected = _export_blocks(project, numbers)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".yeone-export-", suffix="." + format, dir=target.parent)
    os.close(fd)
    temporary = Path(temporary)
    try:
        if format == "hwpx":
            from .hwpx_builder import build_hwpx
            build_hwpx(blocks, str(temporary), highlight=False, page_breaks=False, memos=False)
        else:
            from .docx_builder import build_docx
            build_docx(blocks, str(temporary), highlight=False, page_breaks=False, memos=False)
        got = _read_export(temporary, format)
        # HWPX can contain a writer-created leading/trailing empty paragraph.
        trim = lambda values: "\n".join(values).strip("\n")
        if trim(got) != trim(expected):
            raise ValueError("내보낸 문서를 다시 읽은 글자가 달라 최종 파일을 교체하지 않았습니다.")
        project.validate_source()
        with temporary.open("r+b") as stream:
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return target
