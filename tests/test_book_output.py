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


def test_preview_matches_source_heading_geometry_and_chooses_closest_template_style(tmp_path):
    from hwpx import HwpxDocument

    source = tmp_path / 'layout.pdf'
    with pymupdf.open() as pdf:
        page = pdf.new_page(width=400, height=600)
        page.insert_text((90, 125), 'Section title', fontsize=19)
        for n in range(5):
            page.insert_text((40, 170 + n * 14), 'Source body text fills most of the printable width here.', fontsize=10)
        pdf.save(source)
    template = tmp_path / 'layout.hwpx'
    doc = HwpxDocument.new()
    header = doc.oxml.headers[0]
    for name, align, size in [('Centered title', 'CENTER', 24), ('Left title', 'LEFT', 18), ('Body', 'JUSTIFY', 12)]:
        ppr = header.ensure_paragraph_format(base_para_pr_id=1, alignment=align)
        cpr = doc.styles.ensure_run(size=size)
        header.ensure_style(name, style_type='PARA', para_pr_id_ref=ppr, char_pr_id_ref=cpr)
    doc.add_paragraph('template fixture')
    doc.save_to_path(str(template))
    doc.close()
    rule = tmp_path / 'layout-rule.json'
    rule.write_text(json.dumps({'schema': 1, 'headings': {'section': ['Section title']},
                                'styles': {'paragraph': {'section': ['Centered title', 'Left title'], 'body': ['Body']}}}),
                    encoding='utf-8')
    output, _, _ = preview_document(source, tmp_path / 'empty', ['--layout', 'flow', '--template', str(template),
                                                                 '--preset', str(rule), '--no-normalize'])
    try:
        with ZipFile(output) as archive:
            header = ET.fromstring(archive.read('Contents/header.xml'))
            section = ET.fromstring(archive.read('Contents/section0.xml'))
        styles = {node.get('id'): node.get('name') for node in header.iter(HH + 'style')}
        chars = {node.get('id'): node.get('height') for node in header.iter(HH + 'charPr')}
        paras = {node.get('id'): node for node in header.iter(HH + 'paraPr')}
        title = next(p for p in section.findall(HP + 'p') if 'Section title' in ''.join(p.itertext()))
        body = next(p for p in section.findall(HP + 'p') if 'Source body text' in ''.join(p.itertext()))
        assert styles[title.get('styleIDRef')] == 'Left title'
        fmt = paras[title.get('paraPrIDRef')]
        assert fmt.find(HH + 'align').get('horizontal') == 'LEFT'
        assert any(node.tag.endswith('}left') and int(node.get('value', 0)) > 0 for node in fmt.iter())
        for node, size in [(title, '1900'), (body, '1000')]:
            runs = [run for run in node.findall(HP + 'run') if ''.join(run.itertext()).strip()]
            assert runs and all(chars[run.get('charPrIDRef')] == size for run in runs)
        assert styles[body.get('styleIDRef')] == 'Body'
    finally:
        output.unlink()


def test_new_default_does_not_change_older_saved_layout(tmp_path):
    from yeone_ocr import gui
    config = tmp_path / "config.json"
    with patch.object(gui, "CONFIG_PATH", config):
        assert gui.load_config()["layout"] == "flow"
        for data, expected in [({"layout": "page"}, "page"), ({"layout": "flow"}, "flow"),
                               ({"reader": "mock"}, "page")]:
            config.write_text(json.dumps(data), encoding="utf-8")
            assert gui.load_config()["layout"] == expected


@pytest.mark.parametrize("disabled_rule", [
    None, "front-pages", "source-typography", "entry-paragraphs", "footnote-groups",
])
def test_preview_applies_each_book_hwpx_rule(tmp_path, disabled_rule):
    source = tmp_path / "book.pdf"
    body = "This line fills most of the printable width in the original book."
    entry = "露水[氣味] 甘, 平, 無毒. [主治] 未晞[1]時以盤收取。"
    notes = ["[1] First note.", "[2] Second note."]
    with pymupdf.open() as doc:
        doc.new_page(width=400, height=600).insert_text((40, 120), "Publication information")
        page = doc.new_page(width=400, height=600)
        page.insert_text((183, 115), "前言", fontsize=17, fontname="china-t")
        for n in range(4):
            page.insert_text((40, 150 + n * 15), body, fontsize=10)
        for n, note in enumerate(notes):
            page.insert_text((45, 520 + n * 14), note, fontsize=8)
        doc.save(source)

    work = Work(source, tmp_path / "작업자료")
    work.save_record(1, rec(1, [text_region("p", ["Publication information"])]))
    work.save_record(2, rec(2, [text_region("heading", ["前言"]), text_region("body", [body]),
                                text_region("entry", [entry]),
                                *(text_region(f"note-{n}", [note]) for n, note in enumerate(notes))]))
    template = tmp_path / "styles.hwpx"
    make_template(template)
    flags = [f"--hwpx-no-{disabled_rule}"] if disabled_rule else []
    output, _, _ = preview_document(source, work.dir, ["--layout", "flow", "--template", str(template),
                                                        "--preset", "regions", "--no-normalize", *flags])
    try:
        with ZipFile(output) as archive:
            header = ET.fromstring(archive.read("Contents/header.xml"))
            section = ET.fromstring(archive.read("Contents/section0.xml"))
        paragraphs = {"".join(t.text or "" for t in p.iter(HP + "t")): p for p in section.findall(HP + "p")}
        title = paragraphs["前言"]
        styles = {s.get("id"): s.get("name") for s in header.iter(HH + "style")}
        chars = {c.get("id"): c.get("height") for c in header.iter(HH + "charPr")}
        assert styles[title.get("styleIDRef")] == "제목-절"
        assert (title.get("pageBreak") == "1") == (disabled_rule != "front-pages")
        run = next(run for run in title.findall(HP + "run") if run.find(HP + "t") is not None)
        assert chars[run.get("charPrIDRef")] == ("1600" if disabled_rule == "source-typography" else "1700")
        if disabled_rule == "entry-paragraphs":
            assert entry in paragraphs
        else:
            assert "露水" in paragraphs
            assert "[氣味] 甘, 平, 無毒." in paragraphs
            assert "[主治] 未晞[1]時以盤收取。" in paragraphs
        formats = {p.get("id"): p for p in header.iter(HH + "paraPr")}
        fmt = formats[paragraphs[notes[0]].get("paraPrIDRef")]
        assert (fmt.find(HH + "breakSetting").get("keepWithNext") == "1") == (disabled_rule != "footnote-groups")
    finally:
        output.unlink()
