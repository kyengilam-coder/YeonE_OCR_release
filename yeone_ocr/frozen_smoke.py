"""배포 앱의 자체완결성 검사. 실제 API나 사용자의 설정/문서를 사용하지 않는다.

Frozen: YeonE_OCR.exe --smoke-test <report.json>
Source: python -m yeone_ocr.frozen_smoke <report.json>
"""
from __future__ import annotations

from datetime import datetime, timezone
import contextlib
import importlib
import importlib.metadata
import io
import json
from pathlib import Path
import struct
import sys
import tempfile
import time
import traceback


COMPARISON_WORKER_ENGINES = ("rapidocr", "easyocr", "paddle_server", "glmocr", "surya")
COMPARISON_INSTALLER_RESOURCES = (
    "install_tesseract.py", "tesseract-lock.json", "install_rapidocr.py", "rapidocr-lock.json",
    "install_easyocr.py", "easyocr-lock.json", "install_paddle_server.py", "paddle-server-lock.json",
    "install_paddle_server_gpu.py", "paddle-server-gpu-lock.json", "install_glmocr.py", "glmocr-models.lock.json",
    "install_surya.py", "install_surya_cuda.py", "surya-lock.json", "surya-requirements.txt",
)
COMPARISON_DOCUMENTS = ("LOCAL_OCR.md", "INTEGRATION.md", "HANDOFF.md", "AI_REVIEW.md", "PADDLE_SERVER.md",
                        "GLM_OCR.md", "SURYA_LOCAL.md", "LOCAL_ENGINE_COMPARISON.md", "release-notes.md", "AB_OCR_PROVENANCE.json")


def comparison_resources_probe() -> dict:
    from ab_ocr.runtime import resource_root, worker_path
    checked = []
    workers = {}
    for engine in COMPARISON_WORKER_ENGINES:
        worker = worker_path(engine)
        assert worker.is_file(), f"Missing application-owned worker: {worker.name}"
        compile(worker.read_text(encoding="utf-8"), worker.name, "exec")
        workers[engine] = worker.name
        if worker.name not in checked:
            checked.append(worker.name)
    helper = resource_root() / "ab_ocr" / "worker_sessions.py"
    assert helper.is_file(), "Missing persistent-worker protocol helper"
    compile(helper.read_text(encoding="utf-8"), helper.name, "exec")
    checked.append(helper.name)
    import sqlite3
    with contextlib.closing(sqlite3.connect(":memory:")) as database:
        assert database.execute("SELECT 1").fetchone() == (1,)
    for name in COMPARISON_INSTALLER_RESOURCES:
        resource = resource_root() / "scripts" / name
        assert resource.is_file(), f"Missing installer resource: {name}"
        if name.endswith(".json"):
            assert isinstance(json.loads(resource.read_text(encoding="utf-8")), dict)
        elif name.endswith(".txt"):
            requirements = [line.strip() for line in resource.read_text(encoding="utf-8").splitlines()
                            if line.strip() and not line.lstrip().startswith("#")]
            assert requirements and all("==" in requirement for requirement in requirements)
        else:
            compile(resource.read_text(encoding="utf-8"), name, "exec")
        checked.append(name)
    for name in COMPARISON_DOCUMENTS:
        resource = resource_root() / "docs" / name
        assert resource.is_file(), f"Missing comparison documentation: {name}"
        assert resource.read_text(encoding="utf-8").strip(), f"Empty comparison documentation: {name}"
    return {"application_resources": checked, "workers": workers,
            "persistent_worker_helper": helper.name, "gpu_queue_database": "sqlite3 available",
            "documentation": list(COMPARISON_DOCUMENTS),
            "external_models_loaded": False, "network_calls": 0}


def source_preservation_probe(folder: Path) -> dict:
    """Exercise the new record policy and exact evaluation without model calls."""
    from PIL import Image
    from .cli import nfc_reading
    from .evaluate import build_unit, flatten_source_exact, source_exact_metrics
    from .merge import merge_passes
    from .schema import Column, PageReading, Paragraph, Region, RegionReread
    from .state import Work
    from .text_policy import LEGACY_TEXT_POLICY, SOURCE_TEXT_POLICY, record_text_policy

    literal = "靑硏敎眞樂神\U0002f800漢\ufe00字\U000e0100한\u200b"
    first = PageReading(pdf_page=1, regions=[Region(id="body", kind="본문",
        paragraphs=[Paragraph(columns=[Column(n=1, t=literal)])])])
    second = RegionReread(columns=[Column(n=1, t=literal)])
    before = first.model_dump(), second.model_dump()
    assert nfc_reading(first, text_policy=SOURCE_TEXT_POLICY).model_dump() == before[0]
    merged, stats = merge_passes(first, {"body": second}, text_policy=SOURCE_TEXT_POLICY)
    assert merged.regions[0].paragraphs[0].columns[0].text == literal and stats["marks"] == 0
    assert (first.model_dump(), second.model_dump()) == before
    historical = nfc_reading(first.model_copy(deep=True))
    assert "樂" not in historical.regions[0].paragraphs[0].columns[0].text
    assert record_text_policy({}) == LEGACY_TEXT_POLICY
    projected = flatten_source_exact([(1, " " + literal + "\n")], expand=False)
    assert projected.text == literal

    source = folder / "source-policy.png"
    with Image.new("RGB", (64, 48), "white") as image:
        image.save(source)
    work = Work(str(source), str(folder / "source-policy-work"))
    raw = first.model_dump()
    raw["regions"][0]["paragraphs"][0]["columns"][0]["text"] = "樂一"
    record = {"pdf_page": 1, "text_policy": SOURCE_TEXT_POLICY, "pass1": raw,
              "merged": raw, "pass2": {}, "human_edits": [{"text": "kept"}]}
    work.save_record(1, record)
    original = work.record_path(1).read_bytes()
    exact = source_exact_metrics(build_unit(work, None, "synthetic", [1], ["樂一\ufe00"]))
    assert exact["available"] and exact["stages"]["final"]["edits"] == 2
    assert work.record_path(1).read_bytes() == original and work.load_record(1) == record
    return {"text_policy": SOURCE_TEXT_POLICY, "compatibility_and_selectors_preserved": True,
            "legacy_policy_unchanged": True, "source_exact_edits": 2,
            "evaluation_record_unchanged": True, "network_calls": 0}


def glyph_location_hold_probe() -> dict:
    """A synthetic answer cannot confirm a position on a blank source image."""
    from dataclasses import asdict
    from PIL import Image
    from . import adjudicate
    from .schema import Column, GlyphChoice, PageReading, Paragraph, Region

    reading = PageReading(pdf_page=1, regions=[Region(id="body", kind="본문",
        bbox=[100, 100, 900, 900],
        paragraphs=[Paragraph(columns=[Column(n=1, t="甲〔入/人〕乙")])])])
    record = {"pdf_page": 1, "pass1": reading.model_dump(), "merged": reading.model_dump(),
              "human_edits": [{"text": "historical correction remains"}]}

    class FixtureReader:
        model = "offline-location-fixture"
        calls = 0

        def adjudicate(self, *_args):
            self.calls += 1
            return GlyphChoice(choice="人", window="甲人乙", evidence="synthetic fixture")

    reader = FixtureReader()
    with Image.new("L", (120, 120), 255) as image:
        adjudicate.adjudicate_page(record, image, reader, True, batch=1)
    entry = record["adjudications"][0]
    assert reader.calls == 1 and entry["choice"] == "" and entry["unconfirmed_choice"] == "人"
    assert entry["location_check"]["reason"] == "unmeasured-location"
    assert not entry["location_check"]["verified"] and record["merged"] == reading.model_dump()
    assert record["human_edits"] == [{"text": "historical correction remains"}]
    legacy = reading.model_copy(deep=True)
    old = {**asdict(adjudicate.find_sites(legacy)[0]), "choice": "人",
           "loc": "uniform", "window": "", "evidence": "legacy"}
    assert adjudicate.apply_adjudications(legacy, [old]) == 1
    assert legacy.regions[0].paragraphs[0].columns[0].text == "甲人乙"
    return {"new_unmeasured_choice": "held", "legacy_decision_replay": "preserved",
            "mock_answers": reader.calls, "network_calls": 0}


def small_annotations_probe(folder: Path) -> dict:
    """Detect both layouts and persist proposals without changing old records."""
    import tkinter as tk
    from PIL import Image, ImageDraw
    from .cli import build_parser
    from .schema import Column, PageReading, Paragraph, Region
    from .small_annotation_detection import detect_small_annotations
    from .small_annotation_ui import SmallAnnotationDialog
    from .small_annotations import load_bundle, scan_work
    from .state import Work

    assert issubclass(SmallAnnotationDialog, tk.Toplevel)

    def glyph(draw, x, y, width, height):
        draw.rectangle((x, y, x + width - 1, y + height - 1), outline=0, width=2)
        draw.line((x, y + height // 2, x + width - 1, y + height // 2), fill=0, width=2)

    counts = {}
    for horizontal in (False, True):
        label = "horizontal" if horizontal else "vertical"
        source = folder / f"small-{label}.png"
        with Image.new("L", (900, 650) if horizontal else (420, 400), 255) as image:
            draw = ImageDraw.Draw(image)
            if horizontal:
                for y in (30, 80, 130):
                    for x in range(25, 810, 34):
                        glyph(draw, x, y, 26, 30)
                for y in (240, 390, 440, 490):
                    for x in range(25, 230, 34):
                        glyph(draw, x, y, 26, 30)
                for row in range(2):
                    for x in range(280, 800 if row == 0 else 390, 18):
                        glyph(draw, x, 235 + row * 24, 12, 14)
                for row in range(5):
                    for x in range(280, 800 if row < 4 else 600, 26):
                        glyph(draw, x, 380 + row * 32, 18, 20)
            else:
                for x in (255, 320):
                    for y in range(25, 365, 34):
                        glyph(draw, x, y, 24, 26)
                for y in (25, 59, 263, 297, 331):
                    glyph(draw, 160, y, 24, 26)
                for x in (160, 176):
                    for y in range(110, 207, 16):
                        glyph(draw, x, y, 10, 12)
            reading = PageReading(pdf_page=1, regions=[Region(id="body", kind="본문",
                bbox=[0, 0, 1000, 1000], reading_direction="가로" if horizontal else "세로",
                paragraphs=[Paragraph(columns=[Column(n=1, t="人手樂\ufe00校正")])])])
            detected = detect_small_annotations(image, reading, horizontal=horizontal)
            expected = [2, 5] if horizontal else [2]
            assert [len(c["subcolumns"]) for c in detected["candidates"]] == expected
            image.save(source)
        work = Work(str(source), str(folder / f"small-{label}-work"))
        record = {"pdf_page": 1, "pass1": reading.model_dump(), "pass2": {},
                  "merged": reading.model_dump(), "adjudications": [{"choice": "曰"}],
                  "mode": "horizontal_zh" if horizontal else "vertical",
                  "unknown_old_metadata": {"keep": "unchanged"}}
        work.save_record(1, record)
        originals = {path: path.read_bytes() for path in (source, work.record_path(1))}
        bundle = scan_work(source, work.dir, pages=[1], horizontal=horizontal)
        candidates = bundle["pages"][0]["candidates"]
        assert [len(c["subcolumns"]) for c in candidates] == expected
        for candidate in candidates:
            sides = [side["side"] for side in candidate["subcolumns"]]
            assert sides == ([f"line-{n}" for n in range(1, len(sides) + 1)]
                             if horizontal else ["right", "left"])
            assert candidate["status"] == "detected" and Path(candidate["crop_path"]).is_file()
        assert load_bundle(work.dir)["pages"][0]["stale"] is False
        assert all(path.read_bytes() == content for path, content in originals.items())
        counts[label] = {"candidates": len(candidates), "lines": sum(expected)}
    args = build_parser().parse_args(["annotations", str(source), "--direction", "horizontal"])
    assert callable(args.fn) and args.direction == "horizontal"
    return {"layouts": counts, "ui_imported": True, "original_and_records_unchanged": True,
            "automatic_insertions": 0, "network_calls": 0}


def bookmark_section_probe(folder: Path) -> dict:
    """Exercise the shipped bookmark matcher with an earlier narrative mention."""
    from hwpx.document import HwpxDocument
    from lxml import etree
    import pymupdf
    from .collate.sections import HwpxBook
    from .collate.pdf_outline import find_title_pages
    from .collate.run import index_both

    path = folder / "bookmark-navigation.hwpx"
    hp = "{http://www.hancom.co.kr/hwpml/2011/paragraph}"
    document = HwpxDocument.new()
    try:
        document.add_heading("瀉白散", level=1)
        heading = document.paragraphs[-1].element
        run = etree.SubElement(heading, hp + "run", charPrIDRef="0")
        ctrl = etree.SubElement(run, hp + "ctrl")
        etree.SubElement(ctrl, hp + "bookmark", name="    瀉白散")
        document.add_paragraph("本方正文。", style_id_ref=0, inherit_style=False)
        document.add_heading("葦莖湯", level=1)
        document.add_paragraph("下一方正文。", style_id_ref=0, inherit_style=False)
        document.save_to_path(str(path))
    finally:
        document.close()
    before = path.read_bytes()
    with pymupdf.open() as pdf:
        for lines in (["方書載有瀉白散等多種方劑。"],
                      ["瀉白散", "本方正文。"], ["葦莖湯", "下一方正文。"]):
            page = pdf.new_page()
            for row, text in enumerate(lines):
                page.insert_text((40, 70 + row * 40), text, fontname="china-t", fontsize=18)
        assert find_title_pages(pdf, "瀉白散", "葦莖湯") == (2, 3)
        pdf.set_toc([[1, "사백산", 2], [1, "위경탕", 3]])
        book = HwpxBook(str(path))
        try:
            anchors, _, matches = index_both(pdf, book)
            assert [a.name.strip() for a in anchors] == ["瀉白散", "葦莖湯"]
            assert [m.pdf.page if m.pdf else None for m in matches] == [2, 3]
            assert len({a.para for a in anchors}) == len(anchors)
        finally:
            book.doc.close()
    assert path.read_bytes() == before
    return {"canonical_sections": 2, "body_heading_pages": [2, 3],
            "earlier_mention_rejected": True, "input_unchanged": True, "network_calls": 0}


def bookmark_edit_probe(folder: Path) -> dict:
    """Save a manual edit through the shipped UI, preserving source and other text."""
    import tkinter as tk
    import pymupdf
    from hwpx.document import HwpxDocument
    from .collate.sections import HwpxBook
    from .collate.section_edit import SectionEditSession, manual_protected_paragraphs
    from .collate.section_editor import BookmarkSectionEditor

    source = folder / "bookmark-edit.hwpx"
    pdf_path = folder / "bookmark-edit.pdf"
    document = HwpxDocument.new()
    try:
        document.add_heading("瀉白散", level=1)
        document.add_paragraph("本方正文。", style_id_ref=0, inherit_style=False)
        document.add_heading("葦莖湯", level=1)
        document.add_paragraph("下一方正文。", style_id_ref=0, inherit_style=False)
        document.save_to_path(str(source))
    finally:
        document.close()
    with pymupdf.open() as pdf:
        pdf.new_page().insert_text((40, 60), "瀉白散", fontname="china-t", fontsize=18)
        pdf.save(pdf_path)
    original = source.read_bytes()
    book = HwpxBook(str(source))
    try:
        anchors = book.anchors()
        section = book.section(anchors, 0)
    finally:
        book.doc.close()
    session = SectionEditSession.open(pdf_path, source, "瀉白散", section.start, section.end)
    editable = next(p.index for p in session.paragraphs if p.editable)
    first = session.save({editable: "本方正文甲。"})
    previous = first.path.read_bytes()
    root = tk.Tk()
    root.withdraw()
    window = None
    try:
        window = BookmarkSectionEditor(root, pdf_path=pdf_path, hwpx_path=source,
            section_name="瀉白散", section_start=section.start, section_end=section.end,
            pdf_pages=(1, 1))
        window.withdraw()
        field = window._editors[editable]
        field.delete("1.0", "end")
        field.insert("1.0", "本方正文乙。")
        window._confirm_changes = lambda changes: changes == {editable: "本方正文乙。"}
        assert window.dirty
        assert window.save()
        assert not window.dirty
        assert first.path.with_suffix(".bak.hwpx").read_bytes() == previous
        assert manual_protected_paragraphs(first.path) == {editable}
        reopened = SectionEditSession.open(pdf_path, source, "瀉白散", section.start, section.end)
        assert next(p.corrected_text for p in reopened.paragraphs if p.index == editable) == "本方正文乙。"
        check = HwpxBook(str(first.path))
        try:
            assert "下一方正文。" in check.texts()
        finally:
            check.doc.close()
        assert source.read_bytes() == original
    finally:
        if window is not None:
            window.destroy()
            # Finish this window's native reads before the next fixture creates a PDF.
            window._executor.submit(time.time).result(timeout=8)
            window._alignment_executor.shutdown(wait=True, cancel_futures=True)
        root.destroy()
    return {"saved_into_existing_copy": True, "backup_matches_previous": True,
            "source_and_other_section_preserved": True, "manual_protection": True,
            "ui_save_and_reopen": True, "network_calls": 0}


def bookmark_cursor_probe(folder: Path) -> dict:
    """Exercise real cursor mapping, PDF overlays and matching text size."""
    import os
    import tkinter as tk
    from tkinter import font as tkfont
    import pymupdf
    from hwpx.document import HwpxDocument
    from .collate.sections import HwpxBook
    from .collate.section_editor import BookmarkSectionEditor

    source = folder / "bookmark-cursor.hwpx"
    pdf_path = folder / "bookmark-cursor.pdf"
    body = "天地玄黃宇宙洪荒日月盈昃辰宿列張寒來暑往秋收冬藏"
    document = HwpxDocument.new()
    try:
        document.add_heading("瀉白散", level=1)
        document.add_paragraph(body, style_id_ref=0, inherit_style=False)
        document.add_heading("葦莖湯", level=1)
        document.add_paragraph("下一方正文。", style_id_ref=0, inherit_style=False)
        document.save_to_path(str(source))
    finally:
        document.close()
    with pymupdf.open() as pdf:
        pdf.new_page(width=760, height=500).insert_text(
            (40, 60), "瀉白散", fontname="china-t", fontsize=22)
        pdf.new_page(width=760, height=500).insert_text(
            (40, 130), body, fontname="china-t", fontsize=22)
        pdf.save(pdf_path)
    original, original_pdf = source.read_bytes(), pdf_path.read_bytes()
    book = HwpxBook(str(source))
    try:
        section = book.section(book.anchors(), 0)
        body_index = book.texts().index(body)
    finally:
        book.doc.close()
    root = tk.Tk()
    root.withdraw()
    window = None
    try:
        window = BookmarkSectionEditor(
            root, pdf_path=pdf_path, hwpx_path=source, section_name="瀉白散",
            section_start=section.start, section_end=section.end, pdf_pages=(1, 2))
        assert window._executor.submit(os.getpid).result(timeout=8) != os.getpid()
        # Map an invisible window so Text.bbox() and viewport geometry are real.
        window.attributes("-alpha", 0.0)
        deadline = time.monotonic() + 8

        def pump_until(predicate, message):
            while time.monotonic() < deadline:
                root.update()
                if predicate():
                    return
                time.sleep(.01)
            raise AssertionError(f"{message}: {window.pdf_note.cget('text')}")

        def has_marker():
            return (window._pdf_page == 2 and window._displayed_page == 2
                    and bool(window.pdf_canvas.find_withtag("cursor-line"))
                    and bool(window.pdf_canvas.find_withtag("cursor-glyph")))

        def glyph_coordinates():
            return tuple(window.pdf_canvas.coords(window.pdf_canvas.find_withtag("cursor-glyph")[0]))

        def cursor_height_error():
            caret = field.bbox("insert")
            if not has_marker() or caret is None:
                return float("inf")
            glyph = glyph_coordinates()
            pdf_y = (window.pdf_canvas.winfo_rooty() + (glyph[1] + glyph[3]) / 2
                     - window.pdf_canvas.canvasy(0))
            text_y = field.winfo_rooty() + caret[1] + caret[3] / 2
            return abs(pdf_y - text_y)

        field = window._editors[body_index]
        assert str(field.cget("state")) == "normal"
        assert window._pdf_page == 1
        window.set_zoom(.7)
        field.mark_set("insert", "1.0+9c")
        window.select_paragraph(body_index)
        pump_until(has_marker, "Cursor did not locate the body on PDF page 2")
        assert window._cursor_location.precise
        first_box = glyph_coordinates()
        assert first_box[0] < first_box[2] and first_box[1] < first_box[3]
        small_font = abs(int(tkfont.Font(root=root, font=field.cget("font")).actual("size")))
        field.mark_set("insert", "1.0+15c")
        window.select_paragraph(body_index)
        pump_until(lambda: has_marker() and glyph_coordinates() != first_box,
                   "Moving the caret did not update its glyph marker")
        previous_width = window._image_size[0]
        window.set_zoom(1.4)
        pump_until(lambda: has_marker() and window._image_size[0] > previous_width
                   and abs(int(tkfont.Font(root=root, font=field.cget("font")).actual("size"))) > small_font,
                   "Document text did not follow the PDF's enlarged character size")
        pump_until(lambda: cursor_height_error() <= 2,
                   "The PDF marker and editing caret did not align vertically")
        height_error = round(cursor_height_error(), 3)
        large_font = abs(int(tkfont.Font(root=root, font=field.cget("font")).actual("size")))
        assert not window.dirty
        assert not window.session.corrected_path.exists()
        assert source.read_bytes() == original and pdf_path.read_bytes() == original_pdf
    finally:
        if window is not None:
            window.destroy()
            # Drain prior native work without closing the worker shared by later windows.
            window._executor.submit(time.time).result(timeout=8)
            window._alignment_executor.shutdown(wait=True, cancel_futures=True)
        root.destroy()
    return {"cursor_pdf_page": 2, "line_and_glyph_overlays": True,
            "native_pdf_separate_process": True,
            "same_paragraph_caret_move": True, "font_tracks_pdf_zoom": True,
            "font_sizes": [small_font, large_font],
            "cursor_screen_height_aligned": True, "cursor_height_error_px": height_error,
            "source_pdf_and_hwpx_unchanged": True, "corrected_file_not_created": True,
            "ocr_calls": 0, "network_calls": 0}


def run(report_path: Path) -> int:
    from . import __version__
    report_path = Path(report_path).resolve()
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report = {"version": __version__, "frozen": bool(getattr(sys, "frozen", False)),
              "python": sys.version.split()[0], "bits": struct.calcsize("P") * 8,
              "platform": sys.platform, "checked_at": datetime.now(timezone.utc).isoformat(), "checks": []}

    def check(name, operation):
        try:
            detail = operation()
            report["checks"].append({"name": name, "ok": True, "detail": detail})
        except Exception as exc:
            report["checks"].append({"name": name, "ok": False, "error": f"{type(exc).__name__}: {exc}",
                                     "traceback": traceback.format_exc()})

    packages = {"pymupdf": "pymupdf", "PIL": "pillow", "numpy": "numpy", "cv2": "opencv-python-headless",
                "pydantic": "pydantic", "google.genai": "google-genai", "openai": "openai", "anthropic": "anthropic",
                "hwpx": "python-hwpx", "docx": "python-docx", "opencc": "opencc-python-reimplemented",
                "hanja": "hanja", "lxml.etree": "lxml", "certifi": "certifi"}
    for module, distribution in packages.items():
        def package_probe(module=module, distribution=distribution):
            importlib.import_module(module)
            return {"distribution": distribution, "version": importlib.metadata.version(distribution)}
        check(f"import:{module}", package_probe)

    def data_probe():
        from .app_icon import ASSETS
        from .normalization import rule_data
        from .prompts import load_guide
        from .collate.reading import reading
        from .presets import resolve_preset, BUILTIN
        from opencc import OpenCC
        import certifi
        guide = load_guide()
        assert len(guide) > 1000 and "지침 파일 yeone_ocr/지침.md 이 없습니다" not in guide
        assert (ASSETS / "YeonE_OCR.ico").is_file()
        assert (ASSETS / "app-icon-256.png").is_file()
        assert Path(certifi.where()).is_file()
        assert OpenCC("s2t").convert("汉书") == "漢書"
        korean = reading("大薊")
        assert korean == "대계", korean
        rules = rule_data()
        assert len(rules["rules"]) >= 100
        assert all(resolve_preset(i).label for i in BUILTIN)                # 짜임 규칙 내장 파일(data/preset_*.json)이 꾸러미에 — 없으면 本草 짜임을 잃는다
        assert resolve_preset("kaibao").source_re.search("所在有之。　唐附")
        return {"guide_chars": len(guide), "msr_rules": len(rules["rules"]), "hanja": korean, "opencc": "漢書", "presets": list(BUILTIN)}
    check("bundled-data", data_probe)

    def sdk_probe():
        # Construct clients and access the APIs used by OCR without making a request.
        from google import genai
        from openai import OpenAI
        from anthropic import Anthropic
        clients = []
        try:
            clients.append(genai.Client(api_key="AIza" + "x" * 35, vertexai=False))
            clients.append(OpenAI(api_key="smoke-test-not-a-key", max_retries=0))
            clients.append(Anthropic(api_key="smoke-test-not-a-key", max_retries=0))
            assert callable(clients[0].models.generate_content)
            assert callable(clients[1].responses.create)
            assert callable(clients[2].messages.create)
        finally:
            for client in clients:
                client.close()
        return ["Gemini generate_content", "OpenAI Responses", "Anthropic Messages"]
    check("provider-clients-offline", sdk_probe)

    def tkinter_probe():
        import tkinter as tk
        from tkinter import ttk
        from .app_icon import ASSETS, apply_window_icon
        window = tk.Tk()
        window.withdraw()
        try:
            ttk.Style(window).theme_use("clam")
            image = tk.PhotoImage(master=window, file=str(ASSETS / "app-icon-256.png"))
            assert image.width() == 256 and image.height() == 256
            apply_window_icon(window)
            return {"tcl": window.tk.call("info", "patchlevel"), "tk": tk.TkVersion, "icon": "256x256"}
        finally:
            window.destroy()
    check("tkinter-and-icon", tkinter_probe)

    with tempfile.TemporaryDirectory(prefix="yeone-smoke-", dir=report_path.parent) as temporary:
        folder = Path(temporary).resolve()
        assert folder.is_relative_to(report_path.parent)

        def native_probe():
            import cv2
            import numpy as np
            import pymupdf
            from PIL import Image
            from lxml import etree
            image_path = folder / "image.png"
            Image.new("RGB", (64, 48), "white").save(image_path)
            with Image.open(image_path) as image:
                gray = cv2.cvtColor(np.asarray(image), cv2.COLOR_RGB2GRAY)
            assert gray.shape == (48, 64) and int(gray.min()) == 255
            assert etree.fromstring(b"<root><text>ok</text></root>")[0].text == "ok"
            pdf = folder / "fixture.pdf"
            with pymupdf.open() as doc:
                page = doc.new_page(width=300, height=300)
                page.insert_text((36, 60), "OCR smoke test.", fontsize=14)
                page.insert_text((250, 60), "天地玄黃", fontname="china-t", fontsize=14, rotate=270)
                doc.save(pdf)
            with pymupdf.open(pdf) as doc:
                pixmap = doc[0].get_pixmap()
                assert pixmap.width == 300
                assert "OCR smoke test." in doc[0].get_text()
            return {"image": "Pillow/NumPy/OpenCV", "pdf": "MuPDF create/read/render", "xml": "lxml"}
        check("native-rendering", native_probe)

        def screenshot_probe():
            from PIL import Image, ImageGrab
            from .screenshots import save_screenshot
            from .pdfio import open_pdf, page_image
            assert callable(ImageGrab.grabclipboard)
            if sys.platform == "win32":
                assert callable(Image.core.grabclipboard_win32)
            with Image.new("RGBA", (320, 180), (0, 0, 0, 0)) as screenshot:
                screenshot.putpixel((20, 30), (10, 20, 30, 255))
                saved = save_screenshot(screenshot, folder / "screenshots")
                assert save_screenshot(screenshot, folder / "screenshots") == saved
            with open_pdf(str(saved)) as document:
                assert len(document) == 1
                extracted, _ = page_image(document, 1)
                try:
                    assert extracted.size == (320, 180)
                    assert extracted.getpixel((20, 30)) == (10, 20, 30)
                    assert extracted.getpixel((0, 0)) == (255, 255, 255)
                finally:
                    extracted.close()
            return {"clipboard_backend": "available (not read)", "input": "PNG → one-page OCR input",
                    "pixels": "original resolution preserved", "repeat_paste": "same source", "network_calls": 0}
        check("screenshot-input", screenshot_probe)

        def direction_probe():
            import pymupdf
            from .cli import detect_mode
            from .pdfio import open_pdf
            checked = []
            text = "天地玄黃宇宙洪荒日月盈昃辰宿列張"
            for mode in ("horizontal_zh", "vertical"):
                path = folder / f"direction-{mode}.png"
                with pymupdf.open() as source:
                    page = source.new_page(width=600, height=700)
                    if mode == "horizontal_zh":
                        for row in range(8):
                            page.insert_text((40, 60 + row * 42), text, fontname="china-t", fontsize=20)
                    else:
                        for col in range(8):
                            for row, char in enumerate(text):
                                page.insert_text((540 - col * 42, 60 + row * 24), char, fontname="china-t", fontsize=20)
                    page.get_pixmap(matrix=pymupdf.Matrix(1.5, 1.5)).save(path)
                # Opening the raster loses the fixture's text layer deliberately.
                with open_pdf(str(path)) as document:
                    assert not document[0].get_text().strip()
                    detected, reason = detect_mode(document)
                    assert detected == mode, f"{mode}: {detected} ({reason})"
                    checked.append({"expected": mode, "detected": detected, "reason": reason})
            return {"raster_text_directions": checked, "network_calls": 0}
        check("image-reading-direction", direction_probe)

        def pipeline_probe():
            from .cli import CANCEL, main
            from .verify import check_hwpx_file
            from .schema import PageReading
            from zipfile import ZipFile
            assert PageReading.model_validate({"pdf_page": 1}).pdf_page == 1
            CANCEL.clear()
            work = folder / "ocr"
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = main(["run", str(folder / "fixture.pdf"), "--work", str(work), "--reader", "mock",
                             "--no-adjudicate", "--no-second-pass", "--no-context", "--checkpoint", "0", "--docx"])
            assert code == 0, f"mock pipeline exit {code}: {output.getvalue()[-3000:]}"
            hwpx = work / "out" / "fixture.hwpx"
            docx = work / "out" / "fixture.docx"
            assert hwpx.is_file() and docx.is_file()
            assert check_hwpx_file(str(hwpx)) == []
            for path, member in ((hwpx, "Contents/section0.xml"), (docx, "word/document.xml")):
                with ZipFile(path) as archive:
                    assert "天地玄黃" in archive.read(member).decode("utf-8")
            assert (work / "review.html").is_file()
            assert (work / "out" / "fixture_normalization.html").is_file()
            return {"pipeline": "mock PDF → OCR records → HWPX/DOCX → normalization/review", "network_calls": 0}
        check("offline-ocr-pipeline", pipeline_probe)
        check("source-preservation-and-exact-evaluation", lambda: source_preservation_probe(folder))
        check("bookmark-selection-and-body-boundaries", lambda: bookmark_section_probe(folder))
        check("bookmark-side-by-side-edit-and-save", lambda: bookmark_edit_probe(folder))
        check("bookmark-cursor-follow", lambda: bookmark_cursor_probe(folder))
        check("glyph-location-hold-and-legacy-replay", glyph_location_hold_probe)
        check("small-annotations-layouts-and-record-preservation", lambda: small_annotations_probe(folder))

        def comparison_project_probe():
            from .comparison import ComparisonProject
            from ab_ocr.ocr import file_sha256
            source = folder / "image.png"
            project = ComparisonProject.open(source)
            source_hash = file_sha256(source)
            texts = {"tesseract": "漢樂一\ufe00，", "rapidocr": "漢樂一\ufe00，", "easyocr": "漢樂一\ufe00。",
                     "paddle_server": "漢樂一\ufe00，", "glmocr": "漢樂一\ufe00，", "surya": "漢樂一\ufe00。"}
            for engine, text in texts.items():
                raw = {"kind": "recognition", "schema_version": 1, "engine": engine,
                       "coordinate_system": "rendered-image pixels, xyxy",
                       "text": text, "words": [{"text": text, "bbox": [1, 1, 63, 47],
                           "confidence": None, "block": 1, "paragraph": 1, "line": 1}],
                       "source_path": str(source), "source_sha256": source_hash,
                       "page_number": 1, "image_width": 64, "image_height": 48,
                       "engine_version": "offline-smoke-fixture", "languages": "chi_tra",
                       "psm": 6 if engine == "tesseract" else None, "duration_seconds": 0,
                       "preprocessing": {}, "engine_metadata": {"synthetic": True}}
                project.add_result(raw, request={"profile": "traditional", "direction": "horizontal", "dpi": 300,
                                                 "engine_root": None, "policy": "local-three-engine-v1"})
            original_runs = project.page(1)["runs"]
            suggestions = project.compare_page(1, baseline="tesseract")
            candidate = next(item for item in suggestions if item["candidates"]["rapidocr"] != item["baseline_text"])
            project.accept_candidate(1, candidate["id"], "rapidocr")
            assert project.text(1) != texts["tesseract"]
            assert project.undo_edit(1)
            assert project.text(1) == texts["tesseract"]
            corrected = "人手校正，樂一\ufe00。\n한글 원문"
            project.set_text(1, corrected, reason="offline-smoke-human-fixture")
            target = folder / "comparison.yeone-compare.json"
            project.save(target)
            reopened = ComparisonProject.load(target)
            assert reopened.text(1) == corrected
            assert reopened.page(1)["runs"] == original_runs
            assert [edit["kind"] for edit in reopened.page(1)["edits"]] == ["candidate", "undo", "manual"]
            recovery = folder / "comparison-recovery.yeone-compare.json"
            reopened.save_recovery(recovery)
            assert ComparisonProject.load(recovery).text(1) == corrected
            assert file_sha256(source) == source_hash
            return {"raw_runs": 6, "engines": list(texts), "unicode": "compatibility ideograph and variation selector preserved",
                    "audit": "candidate → undo → manual", "storage": "project and recovery reopen", "network_calls": 0}
        check("local-comparison-project", comparison_project_probe)

        def refinement_probe():
            from .comparison import ComparisonProject
            from .comparison_refinement import (build_candidates, evaluate_proposals,
                                                 accept_proposal, reject_proposal, proposals)
            from .comparison_ai import AISettings, ReviewPolicy, prepare_plan, _prompt
            from ab_ocr.ocr import RecognitionResult
            source = folder / "image.png"
            project = ComparisonProject.open(source)
            original = "甲乙丙丁,戊己庚辛"
            expected = original.replace(",", "，")
            for engine, value in (("glmocr", original), ("easyocr", expected), ("surya", original)):
                raw = RecognitionResult(text=value, words=[] if engine == "glmocr" else [
                    {"text": value, "bbox": [1, 1, 63, 47], "confidence": None}],
                    source_path=str(source), source_sha256=project.source_sha256, page_number=1,
                    image_width=64, image_height=48, engine_version="offline-refinement",
                    languages="chi_tra", psm=None, duration_seconds=0, engine=engine,
                    engine_metadata={"generation_complete": True})
                project.add_result(raw, request={"profile": "traditional", "direction": "horizontal",
                                                 "dpi": 300, "engine_root": None, "policy": "local-three-engine-v1"})
            project.choose_baseline(1, "glmocr")
            raw_runs = project.page(1)["runs"]
            items = build_candidates(project, 1)
            assert len(items) == 1 and evaluate_proposals(project, 1)["text"] == expected
            assert project.text(1) == original and not project.page(1)["edits"]
            plan = prepare_plan(project, [1], ReviewPolicy(punctuation=True, visual_scope="page"),
                                settings=AISettings("openai", "offline-refinement-fixture"))
            assert plan.summary["refinement_proposals"] == 1
            assert "local_refinement_unconfirmed" in _prompt(plan.batches[0], plan.policy, [])
            item = proposals(project, 1)[0]
            accept_proposal(project, 1, item["review_id"], item["id"])
            assert project.text(1) == expected
            assert project.undo_edit(1) and project.text(1) == original
            reject_proposal(project, 1, item["review_id"], item["id"])
            destination = folder / "local-refinement.json"
            project.save(destination)
            page = ComparisonProject.load(destination).page(1)
            assert page["runs"] == raw_runs and not page["ai_reviews"] and not page["ai_decisions"]
            assert [entry["decision"] for entry in page["refinement_decisions"]] == ["accepted", "undone", "rejected"]
            return {"unconfirmed_candidates": 1, "AI_context": "frozen pending evidence",
                    "raw_unchanged": True, "human_decisions": "accept/undo/reject/reopen", "network_calls": 0}
        check("local-refinement-history-and-ai-context", refinement_probe)

        def comparison_ai_probe():
            from types import SimpleNamespace
            from .comparison import ComparisonProject
            from .comparison_ai import AISettings, ReviewPolicy, prepare_plan, run_plan
            from ab_ocr.ocr import file_sha256
            source = folder / "image.png"
            project = ComparisonProject.open(source)
            # Exact positional context and two eligible mixed-script witnesses
            # exercise the same source-fidelity gate used by the editor.
            original = "ABCD误汉天地玄黃,樂一\ufe00한글"
            for engine, text in (("tesseract", original), ("glmocr", "ABCD论汉天地玄黃，樂一\ufe00한글"),
                                 ("surya", "ABCD论汉天地玄黃，樂一\ufe00한글")):
                raw = {"kind": "recognition", "schema_version": 1, "engine": engine,
                       "coordinate_system": "rendered-image pixels, xyxy", "text": text,
                       "words": [] if engine == "glmocr" else [{"text": text, "bbox": [1, 1, 63, 47], "confidence": None,
                                  "block": 1, "paragraph": 1, "line": 1}],
                       "source_path": str(source), "source_sha256": file_sha256(source), "page_number": 1,
                       "image_width": 64, "image_height": 48, "engine_version": "offline-ai-fixture",
                       "languages": "chi_tra", "psm": 6 if engine == "tesseract" else None,
                       "duration_seconds": 0, "preprocessing": {}, "engine_metadata": {"synthetic": True}}
                project.add_result(raw, request={"profile": "traditional", "direction": "horizontal", "dpi": 300,
                                                 "engine_root": None, "policy": "local-three-engine-v1"})
            project.compare_page(1, baseline="tesseract")
            raw_runs = project.page(1)["runs"]
            settings = AISettings(provider="openai", model="offline-vision-fixture", max_calls=2)
            policy = ReviewPolicy(traditional=True, punctuation=True, visual_scope="page")
            plan = prepare_plan(project, [1], policy, settings=settings)
            assert plan.summary["calls"] == 2
            requests = []
            def request(**kwargs):
                requests.append(kwargs)
                assert kwargs["model"] == settings.model and kwargs["store"] is False
                parts = kwargs["input"][0]["content"]
                assert any(item["type"] == "input_image" for item in parts)
                prompt = parts[0]["text"]
                data = json.loads(prompt.split("Source DATA (JSON):\n", 1)[1].rsplit("\nReturn JSON", 1)[0])
                items = []
                for target in data["targets"]:
                    patches = [(4, 5, "误", "论", "recognition")] if data["phase"] == "visual" else [
                        (4, 5, "误", "論", "traditional"), (5, 6, "汉", "漢", "traditional"),
                        (10, 11, ",", "，", "punctuation")]
                    proposals = [{"start": start, "end": end, "before": before, "after": after,
                                  "kind": kind, "reason": "offline synthetic protocol fixture", "uncertain": False}
                                 for start, end, before, after, kind in patches
                                 if target["start"] <= start <= end <= target["end"]]
                    items.append({"target_id": target["id"], "uncertain": False,
                                  "note": "offline fixture", "proposals": proposals})
                return SimpleNamespace(output_text=json.dumps({"items": items}, ensure_ascii=False),
                                       status="completed", usage=None, model=settings.model, id="offline-response")
            client = SimpleNamespace(responses=SimpleNamespace(create=request), close=lambda: None)
            checkpoints = []
            outcome = run_plan(project, plan, api_key="offline-fixture-key",
                               client_factory=lambda *_: client, on_result=checkpoints.append)
            assert outcome["completed"] == 2 and outcome["failed"] == 0 and len(requests) == 2
            assert len(checkpoints) == 2 and project.text(1) == original
            reviews = project.page(1)["ai_reviews"]
            assert len(reviews) == 2 and project.page(1)["runs"] == raw_runs
            from .comparison_fidelity import SOURCE_FIDELITY_CONTRACT
            assert all(r["metadata"]["source_fidelity"]["contract"] == SOURCE_FIDELITY_CONTRACT for r in reviews)
            assert reviews[0]["metadata"]["source_fidelity"]["proposals"][0]["supporting_families"] == ["glmocr", "surya"]
            for ending in ("論", "漢", "，"):
                proposal = next(item for item in project.ai_proposals(1) if item["after"] == ending)
                project.accept_ai_proposal(1, proposal["review_id"], proposal["id"])
            assert project.text(1) == "ABCD論漢天地玄黃，樂一\ufe00한글"
            for _ in range(3):
                assert project.undo_edit(1)
            assert project.text(1) == original and project.page(1)["ai_reviews"] == reviews
            target = folder / "ai-comparison.yeone-compare.json"
            project.save(target)
            reopened = ComparisonProject.load(target)
            assert reopened.page(1)["runs"] == raw_runs and reopened.page(1)["ai_reviews"] == reviews
            assert "offline-fixture-key" not in target.read_text(encoding="utf-8")
            resumed = prepare_plan(reopened, [1], policy, settings=settings)
            assert resumed.summary["calls"] == 0
            return {"stages": "image disagreement → whole-page traditional/punctuation proposals",
                    "mock_requests": 2, "human_accepts_and_undos": 3, "raw_and_ai_records": "unchanged",
                    "storage": "reopened; credential excluded; completed requests reused", "network_calls": 0}
        check("comparison-ai-proposals-offline", comparison_ai_probe)

        check("local-comparison-worker-resources", comparison_resources_probe)

        def comparison_cli_probe():
            from .comparison_cli import parse_pages
            from .cli import build_parser
            assert parse_pages("1-2,4", 4) == [1, 2, 4]
            rejected = []
            for invalid in ("0", "5", "3-1", "1-", "wrong"):
                try:
                    parse_pages(invalid, 4)
                except ValueError:
                    rejected.append(invalid)
                else:
                    raise AssertionError("Invalid page scope was accepted")
            args = build_parser().parse_args(["compare-engines", str(folder / "image.png"),
                                              "--pages", "1", "--profile", "mixed", "--direction", "vertical"])
            assert callable(args.fn) and args.profile == "mixed" and args.direction == "vertical"
            return {"command": "compare-engines", "strict_invalid_ranges_rejected": len(rejected),
                    "ocr_executed": False, "network_calls": 0}
        check("local-comparison-cli-scope", comparison_cli_probe)

        def comparison_editor_probe():
            import tkinter as tk
            from .comparison import ComparisonProject
            from .comparison_editor import ComparisonEditor
            project = ComparisonProject.load(folder / "comparison.yeone-compare.json")
            original = project.text(1)
            raw_runs = project.page(1)["runs"]
            root = tk.Tk()
            root.withdraw()
            editor = None
            try:
                editor = ComparisonEditor(root, project=project, work_dir=folder / "editor-work",
                                          engine_root=folder / "no-installed-models")
                editor.withdraw()
                deadline = time.monotonic() + 5
                while editor._preview is None and time.monotonic() < deadline:
                    root.update()
                    time.sleep(.02)
                assert editor._preview is not None, "Original image preview did not render"
                assert editor.master is root and isinstance(editor, tk.Toplevel)
                editor.text.insert("end-1c", "，確認")
                editor.text.edit_modified(True)
                editor._commit_text()
                assert project.text(1) == original + "，確認"
                editor.undo_edit()
                assert project.text(1) == original
                assert project.page(1)["runs"] == raw_runs
                assert editor.request_close()
                return {"window": "hosted Toplevel", "preview": "rendered original",
                        "editing": "manual change and undo; raw runs unchanged", "network_calls": 0}
            finally:
                if editor is not None and editor.winfo_exists():
                    editor.destroy()
                root.destroy()
        check("local-comparison-editor-offline", comparison_editor_probe)

    report["ok"] = all(item["ok"] for item in report["checks"])
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("Usage: python -m yeone_ocr.frozen_smoke report.json")
    raise SystemExit(run(Path(sys.argv[1])))
