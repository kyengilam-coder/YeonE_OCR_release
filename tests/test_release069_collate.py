"""실측 재현: 문단 배정·글자층 증인·수치 보존과 Windows 파일 소유권."""
from argparse import Namespace
from contextlib import ExitStack
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pymupdf
from hwpx import HwpxDocument

from yeone_ocr.collate.collate import Item, collate, hwpx_cells, ocr_cells, witness_filter
from yeone_ocr.collate.hwpx_edit import Editor
from yeone_ocr.collate.sections import HwpxBook
from yeone_ocr.collate.run import cmd_collate
from yeone_ocr.state import Work


def make_book(path, texts, heading=False):
    with HwpxDocument.new() as doc:
        if heading:
            doc.add_heading("章", level=1)
        for text in texts:
            doc.add_paragraph(text)
        doc.save_to_path(path)


class ParagraphAllocation(unittest.TestCase):
    def test_sure_policy_keeps_uncertain_cross_paragraph_boundary_after_save(self):
        with ExitStack() as resources:
            td = Path(resources.enter_context(tempfile.TemporaryDirectory()))
            make_book(td / "source.hwpx", ["天", "地", "玄"])
            book = HwpxBook(str(td / "source.hwpx"))
            resources.callback(book.doc.close)
            items = collate(hwpx_cells(book.texts(), 0), ocr_cells([(1, "宇宙")]), policy="sure")
            self.assertEqual("".join(it.ocr for it in items), "宇宙")
            self.assertTrue(items and all(it.kind == "C" and it.action == "mark" and not it.auto for it in items))
            editor = Editor(book, memo="notice")
            self.assertTrue(all(result.ok for result in editor.apply(items)))
            editor.save_copy(str(td / "corrected.hwpx"))
            saved = HwpxBook(str(td / "corrected.hwpx"))
            resources.callback(saved.doc.close)
            self.assertEqual("".join(saved.texts()), "天地玄")

    def test_equal_length_cross_paragraph_replacement_still_applies(self):
        with ExitStack() as resources:
            td = Path(resources.enter_context(tempfile.TemporaryDirectory()))
            make_book(td / "source.hwpx", ["天地", "玄黃"])
            book = HwpxBook(str(td / "source.hwpx"))
            resources.callback(book.doc.close)
            items = collate(hwpx_cells(book.texts(), 0), ocr_cells([(1, "宇宙洪荒")]), policy="sure")
            self.assertTrue(items and all(it.auto for it in items))
            editor = Editor(book, memo="notice")
            self.assertTrue(all(result.ok for result in editor.apply(items)))
            editor.save_copy(str(td / "corrected.hwpx"))
            saved = HwpxBook(str(td / "corrected.hwpx"))
            resources.callback(saved.doc.close)
            self.assertEqual("".join(saved.texts()), "宇宙洪荒")

    def test_uneven_boundaries_partition_ocr_once(self):
        for paras, text in [(["A", "B", "C"], "XY"), (["AB", "C", "D"], "XYZ"), (["A", "BCD"], "VWXYZ")]:
            with self.subTest(paras=paras, text=text):
                items = collate(hwpx_cells(paras, 0), ocr_cells([(1, text)]), policy="sure")
                self.assertEqual("".join(it.ocr for it in items), text)
                self.assertTrue(all(not it.auto and it.action == "mark" for it in items))


class LayerWitness(unittest.TestCase):
    def item(self, context="ABCD「Y」EFGH"):
        return Item("A", "replace", 0, 4, 5, "X", "Y", "Y", pdf_page=1, auto=True, oc_ctx=context)

    def test_disagreeing_repeated_context_does_not_hide_difference(self):
        item = self.item()
        items = [item]
        stats = witness_filter(items, {1: "ABCDXEFGHABCDYEFGH"})
        self.assertEqual(items, [item])
        self.assertEqual(item.kind, "A")
        self.assertEqual(stats["none"], 1)
        self.assertEqual(item.witness, "")

    def test_overlapping_repeated_context_is_checked(self):
        item = self.item("AAAA「Y」AAAA")
        items = [item]
        witness_filter(items, {1: "AAAAXAAAAYAAAA"})
        self.assertEqual(items, [item])
        self.assertEqual(item.witness, "")

    def test_agreeing_repeated_context_can_still_support_original(self):
        items = [self.item()]
        stats = witness_filter(items, {1: "ABCDXEFGHABCDXEFGH"})
        self.assertEqual(items, [])
        self.assertEqual(stats["hwpx"], 1)


class BodyNumbers(unittest.TestCase):
    def test_body_numbers_are_reviewable_without_automatic_replacement(self):
        for original, ocr in [("水10毫升", "水20毫升"), ("2024年", "2025年"), ("水毫升", "水10毫升"),
                              ("本草", "1本草"), ("10.5mg", "10.6mg"), ("2024. 10. 12.", "2025. 10. 12.")]:
            with self.subTest(original=original, ocr=ocr):
                items = collate(hwpx_cells([original], 0), ocr_cells([(1, ocr)]), policy="sure")
                self.assertTrue(items)
                self.assertTrue(all(it.kind == "C" and it.action == "mark" and not it.auto and it.notice for it in items))

    def test_explicit_numbering_and_page_labels_are_not_body_numbers(self):
        for ocr in ["1. 本草", "２）本草", "[12]本草", "- 12 -\n本草"]:
            with self.subTest(ocr=ocr):
                self.assertEqual(collate(hwpx_cells(["本草"], 0), ocr_cells([(1, ocr)]), policy="sure"), [])


class CommandResources(unittest.TestCase):
    def test_owned_documents_close_after_return_and_exception(self):
        with tempfile.TemporaryDirectory() as folder:
            td = Path(folder)
            pdf = td / "source.pdf"
            hwpx = td / "source.hwpx"
            with pymupdf.open() as doc:
                doc.new_page()
                doc.save(pdf)
            make_book(hwpx, ["天地"], heading=True)
            for fail in (False, True):
                with self.subTest(fail=fail), ExitStack() as patches:
                    docs, books = [], []

                    def open_pdf(path):
                        doc = pymupdf.open(path)
                        docs.append(doc)
                        return doc

                    def open_book(path):
                        book = HwpxBook(path)
                        books.append(book)
                        return book

                    patches.enter_context(patch("yeone_ocr.pdfio.open_pdf", side_effect=open_pdf))
                    patches.enter_context(patch("yeone_ocr.collate.run.HwpxBook", side_effect=open_book))
                    patches.enter_context(patch("yeone_ocr.collate.run.log"))
                    if fail:
                        patches.enter_context(patch("yeone_ocr.collate.run.index_both", side_effect=RuntimeError("index failed")))
                    args = Namespace(pdf=str(pdf), hwpx=str(hwpx), work=str(td / "work"), list=True)
                    if fail:
                        with self.assertRaisesRegex(RuntimeError, "index failed"):
                            cmd_collate(args)
                    else:
                        self.assertEqual(cmd_collate(args), 0)
                    self.assertTrue(docs and all(doc.is_closed for doc in docs))
                    self.assertTrue(books and all(book.doc._closed for book in books))


class CollateSourceIdentity(unittest.TestCase):
    @staticmethod
    def make_pdf(path, text):
        with pymupdf.open() as doc:
            doc.new_page().insert_text((30, 30), text)
            doc.save(path)

    def test_processing_invalidates_cached_records_when_source_or_dpi_changes(self):
        for change in ("source", "dpi"):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as folder:
                td = Path(folder)
                pdf, hwpx, wd = td / "source.pdf", td / "source.hwpx", td / "work"
                self.make_pdf(pdf, "old")
                make_book(hwpx, ["天地"], heading=True)
                work = Work(str(pdf), str(wd))
                work.ensure_source(72)
                work.save_record(1, {"pdf_page": 1, "marker": "old"})
                if change == "source":
                    self.make_pdf(pdf, "new")
                dpi = 144 if change == "dpi" else 72
                checked = []

                def section(args, **kwargs):
                    checked.append(kwargs["work"].load_record(1))
                    return {}

                args = Namespace(pdf=str(pdf), hwpx=str(hwpx), work=str(wd), anchor_index=1, dpi=dpi)
                with patch("yeone_ocr.collate.run.run_section", side_effect=section), patch("yeone_ocr.collate.run.log"), patch("yeone_ocr.state.WARN"):
                    self.assertEqual(cmd_collate(args), 0)
                self.assertEqual(checked, [None])
                self.assertTrue(list((wd / "archives").glob("*/readings/p001.json")))

    def test_list_is_read_only_with_changed_source_and_apply_flag(self):
        from yeone_ocr.collate.run import corrected_path
        with tempfile.TemporaryDirectory() as folder:
            td = Path(folder)
            pdf, hwpx, wd = td / "source.pdf", td / "source.hwpx", td / "work"
            self.make_pdf(pdf, "old")
            make_book(hwpx, ["天地"], heading=True)
            corrected = Path(corrected_path(str(hwpx)))
            make_book(corrected, ["already corrected"], heading=False)  # mismatched anchors
            work = Work(str(pdf), str(wd))
            work.ensure_source(72)
            work.save_record(1, {"pdf_page": 1, "marker": "old"})
            self.make_pdf(pdf, "new")
            before = {p.relative_to(td): p.read_bytes() for p in td.rglob("*") if p.is_file()}
            args = Namespace(pdf=str(pdf), hwpx=str(hwpx), work=str(wd), list=True, apply=True, dpi=144)
            with patch("yeone_ocr.collate.run.log"):
                self.assertEqual(cmd_collate(args), 0)
            after = {p.relative_to(td): p.read_bytes() for p in td.rglob("*") if p.is_file()}
            self.assertEqual(before, after)
            self.assertFalse((wd / "archives").exists())
            empty = td / "new_work"
            args.work = str(empty)
            with patch("yeone_ocr.collate.run.log"):
                self.assertEqual(cmd_collate(args), 0)
            self.assertFalse(empty.exists())


if __name__ == "__main__":
    unittest.main()
