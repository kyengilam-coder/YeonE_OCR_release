import json
from unittest.mock import patch
from xml.etree import ElementTree as ET
from zipfile import ZipFile

import pymupdf
import pytest

from yeone_ocr.book_output import preview_document
from yeone_ocr.state import Work
from tests._hwpx_fixtures import HH, HP, make_template, rec, text_region


def test_preview_applies_chosen_style_to_actual_book_content_without_touching_source(tmp_path):
    source = tmp_path / "historical.pdf"
    with pymupdf.open() as doc:
        for _ in range(4):
            doc.new_page(width=160, height=220)
        doc.save(source)
    work = Work(source, tmp_path / "작업자료")
    for number in range(1, 5):
        work.save_record(number, rec(number, [text_region("b", [f"Sample paragraph number {number}"])]))
    template = tmp_path / "styles.hwpx"
    make_template(template)
    rule = tmp_path / "test-rule.json"
    rule.write_text(json.dumps({"schema": 1, "styles": {"paragraph": {"body": ["주석"]}}}), encoding="utf8")
    before = {p: p.read_bytes() for p in work.dir.rglob("*") if p.is_file()}
    with patch("yeone_ocr.cli.create_reader", side_effect=AssertionError("preview must not call an OCR model")):
        output, origin, pages = preview_document(source, work.dir, ["--layout", "flow", "--template", str(template),
                                                      "--preset", str(rule), "--no-normalize"], preferred=(4, 2, 3))
    try:
        assert origin == "저장된 OCR" and pages == [2, 3, 4]
        with ZipFile(output) as archive:
            header = ET.fromstring(archive.read("Contents/header.xml"))
            body_style = next(s.get("id") for s in header.iter(HH + "style") if s.get("name") == "주석")
            section = ET.fromstring(archive.read("Contents/section0.xml"))
            matching = [p for p in section.iter(HP + "p") if "Sample paragraph" in "".join(p.itertext())]
            assert matching and all(p.get("styleIDRef") == body_style for p in matching)
    finally:
        output.unlink()
    assert {p: p.read_bytes() for p in work.dir.rglob("*") if p.is_file()} == before


def test_preview_uses_pdf_layer_or_explains_unread_scan(tmp_path):
    path = tmp_path / "book.pdf"
    with pymupdf.open() as doc:
        doc.new_page().insert_text((30, 40), "Title of the original text")
        doc.save(path)
    output, origin, pages = preview_document(path, tmp_path / "empty", ["--layout", "flow", "--no-normalize"])
    try:
        assert pages == [1] and "PDF 글자층" in origin
        with ZipFile(output) as archive:
            assert b"Title of the original text" in archive.read("Contents/section0.xml")
    finally:
        output.unlink()
    with pymupdf.open() as doc:
        doc.new_page()
        doc.save(path)
    with pytest.raises(ValueError, match="대표 쪽을 먼저 판독"):
        preview_document(path, tmp_path / "empty", ["--layout", "flow"])
    assert not (tmp_path / "empty").exists()


def test_new_default_does_not_change_older_saved_layout(tmp_path):
    from yeone_ocr import gui
    config = tmp_path / "config.json"
    with patch.object(gui, "CONFIG_PATH", config):
        assert gui.load_config()["layout"] == "flow"
        for data, expected in [({"layout": "page"}, "page"), ({"layout": "flow"}, "flow"),
                               ({"reader": "mock"}, "page")]:
            config.write_text(json.dumps(data), encoding="utf-8")
            assert gui.load_config()["layout"] == expected
