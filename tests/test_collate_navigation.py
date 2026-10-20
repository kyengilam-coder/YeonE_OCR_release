"""책갈피 구획의 위치·결과 연결 회귀: 외부 모델·사용자 문서를 쓰지 않는다."""
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from yeone_ocr.collate.sections import Anchor, HwpxBook, canonical_anchors, find_anchor, resolve_anchor_index
from yeone_ocr.collate.pdf_outline import PdfAnchor, fallback_page_bounds, find_title_pages, match_anchors, section_page_range
from yeone_ocr.collate.results import locate_section_report, report_stem, result_binding


class Page:
    def __init__(self, text):
        self.text = text

    def get_text(self, mode):
        return self.text


class Sections(unittest.TestCase):
    def test_bookmark_and_heading_become_one_section_with_heading_level(self):
        anchors = canonical_anchors([
            Anchor("heading", "清熱劑", 0, 1),
            Anchor("bookmark", "瀉白散", 10),
            Anchor("heading", "瀉白散", 10, 3, "제목-처방명"),
            Anchor("bookmark", "葦莖湯", 20),
            Anchor("heading", "葦莖湯", 20, 3, "제목-처방명"),
        ])
        self.assertEqual([(a.name, a.level) for a in anchors], [("清熱劑", 1), ("瀉白散", 3), ("葦莖湯", 3)])
        book = SimpleNamespace(paragraphs=[None] * 30)
        ranges = [HwpxBook.section(book, anchors, i, flat=True) for i in range(len(anchors))]
        self.assertEqual([(s.start, s.end) for s in ranges], [(0, 10), (10, 20), (20, 30)])
        matched = match_anchors(anchors, [PdfAnchor("瀉白散", 3, 623), PdfAnchor("葦莖湯", 3, 627)])
        self.assertEqual([m.pdf.page if m.pdf else None for m in matched], [None, 623, 627])

    def test_generic_bookmark_alias_still_matches_and_uses_body_heading(self):
        anchors = canonical_anchors([Anchor("bookmark", "Bookmark1", 10), Anchor("heading", "瀉白散", 10, 3)])
        self.assertEqual(len(anchors), 1)
        self.assertEqual(anchors[0].content_name, "瀉白散")
        self.assertEqual(find_anchor(anchors, "瀉白散"), anchors)
        self.assertEqual(find_anchor(anchors, "Bookmark1"), anchors)
        self.assertEqual(match_anchors(anchors, [PdfAnchor("瀉白散", 3, 623)])[0].pdf.page, 623)
        from yeone_ocr.collate.run import trim_to_section
        paras = [(622, "前方末尾"), (623, "瀉白散"), (623, "本方正文"), (627, "葦莖湯")]
        cut, note = trim_to_section(paras, anchors[0].content_name, "葦莖湯")
        self.assertEqual(cut, paras[1:3])
        self.assertEqual(note, "")

    def test_selection_resolves_by_position_and_name_after_indices_change(self):
        anchors = [Anchor("heading", "瀉白散", 5271, 3), Anchor("heading", "葦莖湯", 5323, 3)]
        self.assertEqual(resolve_anchor_index(anchors, 180, para=5271, name="瀉白散"), 0)
        with self.assertRaises(ValueError):
            resolve_anchor_index(anchors, 1, para=5271, name="葦莖湯")
        with self.assertRaises(ValueError):
            resolve_anchor_index(anchors, 1, para=5272, name="瀉白散")

    def test_parent_scope_and_flat_scope_share_exact_hwpx_pdf_boundaries(self):
        anchors = [Anchor("heading", "甲章", 0, 1), Anchor("heading", "瀉白散", 10, 3),
                   Anchor("heading", "葦莖湯", 20, 3), Anchor("heading", "乙章", 30, 1)]
        matches = match_anchors(anchors, [PdfAnchor(a.name, a.level, page) for a, page in zip(anchors, (600, 623, 627, 650))])
        book = SimpleNamespace(paragraphs=[None] * 40)
        parent = HwpxBook.section(book, anchors, 0)
        self.assertEqual((parent.start, parent.end), (0, 30))
        self.assertEqual(section_page_range(parent, matches, 700), (600, 650))
        flat = HwpxBook.section(book, anchors, 0, flat=True)
        self.assertEqual((flat.start, flat.end), (0, 10))
        self.assertEqual(section_page_range(flat, matches, 700), (600, 623))
        matches[-1].pdf = None
        self.assertIsNone(section_page_range(parent, matches, 700))

    def test_real_hwpx_duplicate_is_collapsed(self):
        from hwpx.document import HwpxDocument
        from lxml import etree
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "book.hwpx"
            doc = HwpxDocument.new()
            heading = doc.add_heading("瀉白散", level=3)
            run = heading.element.find("{http://www.hancom.co.kr/hwpml/2011/paragraph}run")
            etree.SubElement(run, "{http://www.hancom.co.kr/hwpml/2011/paragraph}bookmark", name="瀉白散")
            doc.add_paragraph("正文", style_id_ref=0, inherit_style=False)
            doc.save_to_path(str(path))
            doc.close()
            book = HwpxBook(str(path))
            try:
                anchors = book.anchors()
                self.assertEqual(len(anchors), 1)
                self.assertEqual(anchors[0].content_name, "瀉白散")
            finally:
                book.doc.close()


class SafePdfFallback(unittest.TestCase):
    def test_narrative_mention_and_toc_are_not_section_headings(self):
        doc = [Page("目錄\n瀉白散 …… 623\n葦莖湯 …… 627"), Page("如六味地黄丸、泻白散、导赤散、泻黄散等"),
               Page("前方末尾\n泻\n白\n散\n【组成】地骨皮"), Page("后文"), Page("苇\n茎\n汤\n【组成】")]
        self.assertEqual(find_title_pages(doc, "瀉白散", "葦莖湯"), (3, 5))

    def test_repeated_headings_require_neighbor_bounds(self):
        doc = [Page("瀉白散"), Page("葦莖湯"), Page("瀉白散"), Page("葦莖湯")]
        self.assertIsNone(find_title_pages(doc, "瀉白散", "葦莖湯"))
        self.assertEqual(find_title_pages(doc, "瀉白散", "葦莖湯", start_page=3, end_page=4), (3, 4))

    def test_missing_or_reversed_boundaries_fail_safely(self):
        doc = [Page("瀉白散"), Page("本文中提到葦莖湯的名字")]
        self.assertIsNone(find_title_pages(doc, "瀉白散", "葦莖湯"))
        self.assertIsNone(find_title_pages(doc, "瀉白散", start_page=2, end_page=1))
        anchors = [Anchor("heading", name, i) for i, name in enumerate(("左金丸", "瀉白散", "葦莖湯"))]
        matches = match_anchors(anchors, [PdfAnchor("左金丸", 3, 619), PdfAnchor("葦莖湯", 3, 627)])
        self.assertEqual(fallback_page_bounds(matches, 1, 2091), (619, 627))


class BoundResults(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.pdf = self.root / "source.pdf"
        self.hwpx = self.root / "book.hwpx"
        self.pdf.write_bytes(b"source pdf")
        self.hwpx.write_bytes(b"source hwpx")
        self.folder = self.root / "work" / "collate"
        self.folder.mkdir(parents=True)

    def report(self, name="瀉白散", start=5271, original=None, legacy=False):
        source = original or self.hwpx
        binding = {} if legacy else result_binding(self.pdf, source, name, start)
        stem = name if legacy else report_stem(binding, name, start)
        html = self.folder / (stem + ".html")
        html.write_text("report", encoding="utf-8")
        data = {**binding, "section": name, "hwpx_paras": [start, start + 52], "hwpx_file": str(source)}
        html.with_suffix(".json").write_text(json.dumps(data), encoding="utf-8")
        return html

    def locate(self, name="瀉白散", start=5271):
        return locate_section_report(self.folder.parent, self.pdf, self.hwpx, name, start)

    def test_selects_matching_report_not_newest_other_section_or_document(self):
        expected = self.report()
        self.report("葦莖湯", 5323)
        self.report("瀉白散", 10000)
        other = self.root / "another.hwpx"
        other.write_bytes(b"other book")
        self.report(original=other)
        self.assertEqual(self.locate(), expected)

    def test_replaced_input_does_not_open_old_report(self):
        self.report()
        self.hwpx.write_bytes(b"replacement hwpx")
        self.assertIsNone(self.locate())

    def test_legacy_report_requires_verified_source_manifest(self):
        expected = self.report(legacy=True)
        self.assertIsNone(self.locate())
        digest = hashlib.sha256(self.pdf.name.encode() + b"\0" + hashlib.sha256(self.pdf.read_bytes()).digest()).hexdigest()
        manifest = {"path": str(self.pdf), "sha256": digest}
        (self.folder.parent / "source.json").write_text(json.dumps(manifest), encoding="utf-8")
        self.assertEqual(self.locate(), expected)
        self.pdf.write_bytes(b"another pdf")
        self.assertIsNone(self.locate())

    def test_report_names_distinguish_same_title_and_multiple_documents(self):
        a = self.report()
        b = self.report(start=5300)
        self.assertNotEqual(a.name, b.name)
        self.assertTrue(a.exists() and b.exists())

    def test_parent_and_flat_reports_have_distinct_names_and_lookup_scopes(self):
        reports = {}
        for end in (10, 30):
            binding = result_binding(self.pdf, self.hwpx, "甲章", 0, end)
            path = self.folder / (report_stem(binding, "甲章", 0) + ".html")
            path.write_text("report", encoding="utf-8")
            data = {**binding, "section": "甲章", "hwpx_paras": [0, end]}
            path.with_suffix(".json").write_text(json.dumps(data), encoding="utf-8")
            reports[end] = path
        self.assertNotEqual(reports[10], reports[30])
        self.assertEqual(locate_section_report(self.folder.parent, self.pdf, self.hwpx, "甲章", 0, 30), reports[30])
        self.assertIsNone(locate_section_report(self.folder.parent, self.pdf, self.hwpx, "甲章", 0, 99))

    def test_corrected_file_used_as_input_rebinds_after_save(self):
        from argparse import Namespace
        from unittest.mock import patch
        from tests.test_collate import make_book
        from yeone_ocr.collate import run
        from yeone_ocr.collate.collate import Item
        from yeone_ocr.schema import PageReading
        source = self.root / "input_교정.hwpx"
        make_book(str(source))
        before = source.read_bytes()
        book = HwpxBook(str(source))
        try:
            anchors = book.anchors()
            section = book.section(anchors, 1)
            work = SimpleNamespace(dir=self.folder.parent, pdf_path=self.pdf,
                                   load_record=lambda _: {"merged": PageReading(pdf_page=1).model_dump()},
                                   textlayer_path=lambda _: self.root / "absent.json")
            args = Namespace(pdf=str(self.pdf), hwpx=str(source), pdf_pages="1", apply=True,
                             no_glyph_check=True, policy="sure", memo="inline")
            item = Item("A", "replace", section.start + 1, 0, 1, "1", "2", "2", auto=True)
            with patch.object(run, "ensure_pages_read", return_value=False), patch("yeone_ocr.cli.resolve_mode"), \
                    patch.object(run, "collate", return_value=[item]), patch.object(run, "log"):
                result = run.run_section(args, pdf_doc=[None], work=work, book=book, anchors=anchors,
                                         pdf_anchors=[], matches=[SimpleNamespace(pdf=None, how="none")] * len(anchors), idx=1)
            self.assertNotEqual(source.read_bytes(), before)
            self.assertEqual(locate_section_report(self.folder.parent, self.pdf, source,
                                                   section.anchor.name, section.start, section.end), Path(result["report"]))
        finally:
            book.doc.close()


if __name__ == "__main__":
    unittest.main()
