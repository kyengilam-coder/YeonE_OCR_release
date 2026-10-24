"""Existing PDF/image OCR results can be edited without recognizing again."""
from pathlib import Path
import tempfile
import unittest
from zipfile import ZipFile

from lxml import etree
from PIL import Image
import pymupdf

from yeone_ocr.blocks import Block
from yeone_ocr.collate.hwpx_edit import HP, HH
from yeone_ocr.collate.section_edit import (
    ConflictError, SectionEditError, SectionEditSession, _Archive,
    _generated_bookmarks, manual_history_path, manual_protected_paragraphs,
)
from yeone_ocr.hwpx_builder import build_hwpx
from yeone_ocr.hwpx_template import build_hwpx_template
from yeone_ocr.presets import resolve_preset
from yeone_ocr.state import Work


class GeneratedSectionEditing(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.pdf = self.root / "source.pdf"
        with pymupdf.open() as document:
            for _ in range(5):
                document.new_page(width=200, height=300)
            document.save(self.pdf)
        self.output = self.root / "result.hwpx"

    def build(self, blocks=None, **kwargs):
        build_hwpx(blocks or [
            Block(type="meta", text="[source.pdf / 전체 PDF 102쪽 / PDF 2쪽 / 인쇄 12쪽]"),
            Block(type="para", text="甲乙丙丁皰", spans=[(0, 1, "green"), (1, 2, "blue"), (2, 3, "red")]),
            Block(type="meta", text="[source.pdf / 전체 PDF 105쪽 / PDF 5쪽 / 인쇄 15쪽]"),
            Block(type="para", text="末行皰。"),
        ], str(self.output), memos=False, **kwargs)
        return self.output

    def open(self, page=None, **kwargs):
        return SectionEditSession.open_generated(self.pdf, self.output, page, **kwargs)

    def strip_provenance(self):
        with ZipFile(self.output) as archive:
            entries = [(info, archive.read(info.filename)) for info in archive.infolist()
                       if info.filename != "META-INF/yeone-ocr-edit.json"]
        with ZipFile(self.output, "w") as archive:
            for info, data in entries:
                archive.writestr(info, data)

    def test_partial_pages_use_local_number_and_bound_widgets(self):
        self.build()
        session = self.open(5)
        self.assertEqual(session.generated_pages, (2, 5))
        self.assertEqual(session.page, 5)
        self.assertEqual(session.pdf_pages, (5, 5))
        self.assertEqual(len(session.paragraphs), 2)
        self.assertFalse(session.paragraphs[0].editable)
        self.assertTrue(session.paragraphs[1].editable)
        self.assertEqual([scope.page for scope in session.page_scopes], [2, 5])
        for invalid in (1, 3, True, 6):
            with self.subTest(invalid=invalid), self.assertRaises(SectionEditError):
                self.open(invalid)
        self.assertFalse(session.corrected_path.exists())
        self.assertFalse(manual_history_path(session.corrected_path).exists())

    def test_save_clears_only_changed_grade_and_accumulates_history_across_pages(self):
        self.build()
        source_bytes, pdf_bytes = self.output.read_bytes(), self.pdf.read_bytes()
        session = self.open(2)
        row = next(row for row in session.paragraphs if row.editable)
        self.assertEqual([span.kind for span in row.corrected_styles[:3]], ["green", "blue", "red"])
        first = session.save({row.index: "甲辛丙丁皰"})
        spans = next(p for p in session.paragraphs if p.index == row.index).corrected_styles
        at = lambda offset: next(s for s in spans if s.start <= offset < s.end)
        self.assertEqual(at(0).foreground, "#0B7A3B")
        self.assertEqual(at(1).foreground, "#000000")
        self.assertEqual(at(1).background, "")
        self.assertEqual(at(2).foreground, "#C00000")
        session = self.open(5)
        last = next(p for p in session.paragraphs if p.editable)
        second = session.save({last.index: "末行疱。"})
        self.assertIsNotNone(second.backup_path)
        self.assertEqual(manual_protected_paragraphs(first.path), {row.index, last.index})
        self.assertEqual(next(p for p in self.open(2).paragraphs if p.index == row.index).corrected_text, "甲辛丙丁皰")
        self.assertEqual(self.output.read_bytes(), source_bytes)
        self.assertEqual(self.pdf.read_bytes(), pdf_bytes)
        original, saved = _Archive(source_bytes), _Archive(first.path.read_bytes())
        for name, data in original.entries.items():
            if name not in {"Contents/header.xml", "Contents/section0.xml"}:
                self.assertEqual(saved.entries[name], data, name)

    def test_whole_document_replace_and_export_include_other_output_page(self):
        self.build()
        session = self.open(2)
        plan = session.preview_replace("皰", "疱", scope="document")
        self.assertEqual(plan.matches, 2)
        self.assertNotIn("皰", session.export_text(plan.changes, scope="document"))
        self.assertIn("末行疱", session.export_text(plan.changes, scope="document"))
        self.assertNotIn("末行", session.export_text(plan.changes, scope="section"))
        session.save(plan.changes, replacement_plan=plan)
        self.assertIn("末行疱", self.open(5).export_text({}))

    def test_inline_flow_bookmarks_expand_shared_paragraph_range_and_survive_save(self):
        self.build([
            Block(type="para", text="甲乙丙丁戊己", anchors=[(0, "전체 PDF 102쪽 · PDF 2쪽 · 인쇄 12쪽"), (3, "PDF 5쪽 · 인쇄 15쪽")]),
            Block(type="para", text="末行。"),
        ])
        session = self.open(2)
        row = next(p for p in session.paragraphs if p.editable)
        self.assertEqual(session.generated_pages, (2, 5))
        self.assertEqual(session.pdf_pages, (2, 5))
        self.assertEqual(self.open(5).start, row.index)
        original = _generated_bookmarks(_Archive(self.output.read_bytes()).paragraphs[row.index])
        session.save({row.index: "甲乙新丙丁戊己"})
        saved = _generated_bookmarks(_Archive(session.corrected_path.read_bytes()).paragraphs[row.index])
        self.assertEqual([(p, label) for _, p, label in saved], [(p, label) for _, p, label in original])
        self.assertEqual(self.open(5).pdf_pages, (2, 5))

    def test_page_layout_continuation_is_shared_and_marker_cannot_be_edited(self):
        self.build([
            Block(type="meta", text="[source.pdf / PDF 2쪽 / 인쇄 12쪽]"),
            Block(type="para", text="甲乙〔→PDF 5쪽/인쇄 15쪽〕丙丁"),
            Block(type="meta", text="[source.pdf / PDF 5쪽 / 인쇄 15쪽]"),
            Block(type="para", text="末行。"),
        ])
        session = self.open(5)
        self.assertEqual(session.pdf_pages, (2, 5))
        row = next(p for p in session.paragraphs if "〔→" in p.corrected_text)
        with self.assertRaisesRegex(SectionEditError, "쪽 연결"):
            session.save({row.index: row.corrected_text.replace("PDF 5쪽", "PDF 4쪽")})
        plan = session.preview_replace("PDF 5쪽", "PDF 4쪽", scope="document")
        self.assertEqual(plan.matches, 0)
        self.assertEqual(plan.skipped_count, 2)
        session.save({row.index: row.corrected_text.replace("甲", "新")})
        self.open(2)

    def test_inline_notices_after_page_metadata_do_not_hide_first_page(self):
        self.build([
            Block(type="meta", text="[source.pdf / PDF 2쪽 / 인쇄 12쪽]", memos=[(0, "범례와\n쪽 고지")]),
            Block(type="para", text="첫 본문"),
            Block(type="meta", text="[source.pdf / PDF 5쪽 / 인쇄 15쪽]"),
            Block(type="para", text="뒤 본문"),
        ])
        session = self.open(2)
        self.assertEqual(session.generated_pages, (2, 5))
        self.assertIn("첫 본문", session.export_text({}))
        self.assertFalse(session.paragraphs[0].editable)

    def test_single_image_and_multiframe_input_use_actual_page_count(self):
        self.pdf = self.root / "scan.png"
        Image.new("RGB", (30, 40), "white").save(self.pdf)
        self.build([Block(type="para", text="單張皰")])
        session = self.open()
        self.assertEqual(session.generated_pages, (1,))
        row = next(p for p in session.paragraphs if p.editable)
        session.save({row.index: "單張疱"})
        self.pdf = self.root / "frames.tif"
        Image.new("RGB", (30, 40), "white").save(self.pdf, save_all=True,
                                                    append_images=[Image.new("RGB", (20, 30), "black")])
        self.output = self.root / "multi.hwpx"
        self.build([Block(type="meta", text="[frames.tif / PDF 2쪽 / 인쇄 쪽수 확인 안 됨]"), Block(type="para", text="第二張")])
        self.assertEqual(self.open(2).pdf_pages, (2, 2))

    def test_folder_source_fingerprint_ignores_nonimages_and_detects_membership(self):
        self.pdf = self.root / "images"
        self.pdf.mkdir()
        for name in ("scan2.png", "scan10.png"):
            Image.new("RGB", (20, 30), "white").save(self.pdf / name)
        self.build([Block(type="meta", text="[images / PDF 2쪽 / 인쇄 쪽수 확인 안 됨]"), Block(type="para", text="第二張")])
        session = self.open(2)
        (self.pdf / "notes.txt").write_text("not an image", encoding="utf-8")
        session._assert_unchanged()
        (self.pdf / "scan2.png").rename(self.pdf / "scan20.png")
        with self.assertRaises(ConflictError):
            session._assert_unchanged()
        self.assertFalse(session.corrected_path.exists())

    def test_mismatched_work_manifest_and_page_bounds_fail_without_writes(self):
        self.build()
        work = Work(str(self.pdf), str(self.root / "work"))
        different = self.root / "other.pdf"
        with pymupdf.open() as document:
            document.new_page()
            document.save(different)
        with self.assertRaisesRegex(SectionEditError, "작업 기록"):
            SectionEditSession.open_generated(different, self.output, work_dir=work.dir)
        with self.assertRaisesRegex(SectionEditError, "범위"):
            SectionEditSession.open_generated(different, self.output)
        self.assertFalse(self.output.with_stem("result_교정").exists())

    def test_legacy_work_result_removes_exact_grades_but_preserves_unclaimed_colors(self):
        work = Work(str(self.pdf), str(self.root / "work"))
        self.output = work.out_dir / "result.hwpx"
        self.build()
        self.strip_provenance()
        untrusted = self.open(2)
        self.assertEqual(untrusted._generated_bases, {})
        trusted = self.open(2, work_dir=work.dir)
        self.assertTrue(trusted._generated_bases)
        row = next(p for p in trusted.paragraphs if p.editable)
        trusted.save({row.index: "甲辛丙丁皰"})
        spans = next(p for p in trusted.paragraphs if p.index == row.index).corrected_styles
        self.assertEqual(next(s.foreground for s in spans if s.start <= 1 < s.end), "#000000")

    def test_legacy_named_author_style_identical_to_grade_is_never_claimed(self):
        work = Work(str(self.pdf), str(self.root / "work"))
        self.output = work.out_dir / "result.hwpx"
        self.build()
        self.strip_provenance()
        archive = _Archive(self.output.read_bytes())
        header = etree.fromstring(archive.entries["Contents/header.xml"])
        blue = next(style.get("id") for style in header.iter(HH + "charPr")
                    if style.get("textColor") == "#1F4FBF")
        named_styles = next(header.iter(HH + "styles"))
        etree.SubElement(named_styles, HH + "style", id=str(len(named_styles)),
                                  type="CHAR", name="작성자 파랑", engName="AuthorBlue",
                                  paraPrIDRef="0", charPrIDRef=blue, nextStyleIDRef="0", langID="1042", lockForm="0")
        named_styles.set("itemCnt", str(len(named_styles)))
        archive.entries["Contents/header.xml"] = etree.tostring(header, encoding="utf-8", xml_declaration=True)
        archive.write(self.output, set())
        session = self.open(2, work_dir=work.dir)
        self.assertNotIn(blue, session._generated_bases)
        row = next(p for p in session.paragraphs if p.editable)
        self.assertEqual(next(s.kind for s in row.corrected_styles if s.start <= 1 < s.end), "")
        session.save({row.index: "甲辛丙丁皰"})
        spans = next(p for p in session.paragraphs if p.index == row.index).corrected_styles
        self.assertEqual(next(s.foreground for s in spans if s.start <= 1 < s.end), "#1F4FBF")

    def test_template_manual_edit_restores_author_color_and_bold(self):
        from hwpx import HwpxDocument
        template = self.root / "template.hwpx"
        document = HwpxDocument.new()
        try:
            author = document.oxml.ensure_run_style(size=19, bold=True, color="#603080")
            document.oxml.headers[0].ensure_style("문단-1", style_type="PARA", para_pr_id_ref=0, char_pr_id_ref=author)
            document.save_to_path(str(template))
        finally:
            document.close()
        build_hwpx_template([
            Block(type="para", text="甲乙丙丁", role="body", anchors=[(0, "PDF 2쪽")],
                  spans=[(0, 1, "red"), (1, 2, "blue"), (2, 3, "green")])
        ], str(self.output), str(template), preset=resolve_preset("basic"), memos=False)
        before = template.read_bytes()
        session = self.open(2)
        row = next(p for p in session.paragraphs if p.editable)
        original = _Archive(self.output.read_bytes()).paragraphs[row.index]
        controls = [etree.tostring(child) for run in original.findall(HP + "run")
                    for child in run if child.tag in {HP + "secPr", HP + "ctrl"}]
        session.save({row.index: "子丑寅丁"})
        spans = next(p for p in session.paragraphs if p.index == row.index).corrected_styles
        self.assertTrue(all(span.foreground == "#603080" and span.bold for span in spans))
        saved = _Archive(session.corrected_path.read_bytes()).paragraphs[row.index]
        self.assertEqual([etree.tostring(child) for run in saved.findall(HP + "run")
                          for child in run if child.tag in {HP + "secPr", HP + "ctrl"}], controls)
        self.assertEqual(template.read_bytes(), before)

    def test_source_or_generated_file_change_blocks_save(self):
        self.build()
        session = self.open(2)
        row = next(p for p in session.paragraphs if p.editable)
        self.output.write_bytes(self.output.read_bytes() + b"changed")
        with self.assertRaises(ConflictError):
            session.save({row.index: "改字"})
        self.assertFalse(session.corrected_path.exists())

    def test_modified_page_marker_and_paragraph_identity_in_existing_copy_are_rejected(self):
        self.build()
        session = self.open(2)
        archive = _Archive(self.output.read_bytes())
        metadata = archive.paragraphs[session.start]
        next(metadata.iter(HP + "t")).text = "[source.pdf / PDF 1쪽 / 인쇄 12쪽]"
        archive.write(session.corrected_path, {archive.locations[session.start][0]})
        with self.assertRaisesRegex(SectionEditError, "구조"):
            self.open(2)


if __name__ == "__main__":
    unittest.main()
