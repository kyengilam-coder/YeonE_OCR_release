"""Manual HWPX edits survive later automatic collation and conflicts."""
from argparse import Namespace
from contextlib import ExitStack
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile

from lxml import etree

from yeone_ocr.collate import run
from yeone_ocr.collate.collate import Item
from yeone_ocr.collate.hwpx_edit import paragraph_text
from yeone_ocr.collate.section_edit import (
    SectionEditSession, _Archive, manual_history_path, manual_protected_paragraphs,
)
from yeone_ocr.collate.sections import HwpxBook
from yeone_ocr.schema import PageReading


class ManualCollationReplay(unittest.TestCase):
    def setUp(self):
        import pymupdf
        from hwpx.document import HwpxDocument

        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "book.hwpx"
        self.target = self.root / "book_교정.hwpx"
        self.pdf = self.root / "book.pdf"
        document = HwpxDocument.new()
        try:
            document.add_heading("瀉白散", level=1)
            style = document.styles.ensure_run(size=11)
            for text in ("地骨皮一錢。", "甘草二錢。"):
                document.add_paragraph(text, style_id_ref=0, char_pr_id_ref=style, inherit_style=False)
            document.add_heading("葦莖湯", level=1)
            document.add_paragraph("葦根三錢。", style_id_ref=0, char_pr_id_ref=style, inherit_style=False)
            document.save_to_path(str(self.source))
        finally:
            document.close()
        with pymupdf.open() as pdf:
            pdf.new_page()
            pdf.new_page()
            pdf.set_toc([[1, "瀉白散", 1], [1, "葦莖湯", 2]])
            pdf.save(self.pdf)
        book = HwpxBook(str(self.source))
        try:
            anchors = book.anchors()
            self.scope = book.section(anchors, 0)
            self.manual_index = book.texts().index("地骨皮一錢。")
            self.other_index = book.texts().index("甘草二錢。")
        finally:
            book.doc.close()
        self.original_bytes = self.source.read_bytes()
        self.session = SectionEditSession.open(
            self.pdf, self.source, "瀉白散", self.scope.start, self.scope.end,
        )
        self.session.save({self.manual_index: "天骨皮一錢。"})
        self.work = SimpleNamespace(
            dir=self.root,
            pdf_path=self.pdf,
            load_record=lambda _: {"merged": PageReading(pdf_page=1).model_dump()},
            textlayer_path=lambda _: self.root / "absent.json",
        )
        self.args = Namespace(
            pdf=str(self.pdf), hwpx=str(self.source), out=str(self.target),
            pdf_pages="1", apply=True, no_glyph_check=True, policy="sure", memo="notice",
        )

    def _replay(self, items, during_read=None):
        book = HwpxBook(str(self.target))
        try:
            anchors = book.anchors()
            with ExitStack() as stack:
                stack.enter_context(patch("yeone_ocr.cli.resolve_mode"))
                stack.enter_context(patch.object(run, "log"))
                stack.enter_context(patch.object(run, "ensure_pages_read",
                                                side_effect=during_read, return_value=False))
                stack.enter_context(patch.object(run, "collate", return_value=items))
                stack.enter_context(patch.object(
                    run, "witness_filter", return_value={"checked": 0, "hwpx": 0, "ocr": 0, "none": 0},
                ))
                stack.enter_context(patch.object(run, "promote_repeated", return_value=0))
                stack.enter_context(patch.object(run, "glyph_reader", return_value=None))
                result = run.run_section(
                    self.args, pdf_doc=[None, None], work=self.work, book=book, anchors=anchors,
                    pdf_anchors=[], matches=[SimpleNamespace(pdf=None, how="none")] * len(anchors), idx=0,
                )
                return result
        finally:
            book.doc.close()

    def _proposal(self, index, old, new, *, kind="A", auto=True):
        return Item(kind, "replace", index, 0, 1, old, new, new, pdf_page=1, auto=auto)

    def _paragraph_xml(self, index):
        return etree.tostring(
            _Archive(self.target.read_bytes()).paragraphs[index], method="c14n", exclusive=True,
        )

    def test_manual_save_then_ocr_replay_preserves_manual_text_styles_and_history(self):
        protected_xml = self._paragraph_xml(self.manual_index)
        history_bytes = manual_history_path(self.target).read_bytes()
        held = self._proposal(self.manual_index, "天", "地")
        eligible = self._proposal(self.other_index, "甘", "炙")
        result = self._replay([held, eligible])

        self.assertEqual(self.source.read_bytes(), self.original_bytes)
        self.assertEqual(self._paragraph_xml(self.manual_index), protected_xml)
        self.assertEqual(manual_history_path(self.target).read_bytes(), history_bytes)
        self.assertEqual(manual_protected_paragraphs(self.target), {self.manual_index})
        saved = _Archive(self.target.read_bytes())
        self.assertEqual(paragraph_text(saved.paragraphs[self.manual_index]), "天骨皮一錢。")
        self.assertEqual(paragraph_text(saved.paragraphs[self.other_index]), "炙草二錢。")
        self.assertFalse(held.auto)
        self.assertIn("보고에만 제안", held.note)
        report = json.loads(Path(result["report"]).with_suffix(".json").read_text("utf-8"))
        self.assertEqual(report["manual_held_items"], 1)
        self.assertEqual(report["manual_protected_paragraphs"], [self.manual_index])
        reopened = SectionEditSession.open(
            self.pdf, self.source, "瀉白散", self.scope.start, self.scope.end,
        )
        self.assertEqual(next(p.corrected_text for p in reopened.paragraphs
                              if p.index == self.manual_index), "天骨皮一錢。")

        before = self.target.read_bytes()
        self._replay([self._proposal(self.manual_index, "天", "地")])
        self.assertEqual(self.target.read_bytes(), before)
        self.assertEqual(manual_protected_paragraphs(self.target), {self.manual_index})

    def test_protected_proposals_are_not_sent_to_glyph_repeat_witness_or_editor(self):
        observed = {"repeat": [], "witness": []}

        def repeat(items, **_):
            observed["repeat"] = [item.para for item in items]
            return 0

        def witness(items, *_args, **_kwargs):
            observed["witness"] = [item.para for item in items]
            return {"checked": 0, "hwpx": 0, "ocr": 0, "none": 0}

        before = self.target.read_bytes()
        book = HwpxBook(str(self.target))
        try:
            anchors = book.anchors()
            item = self._proposal(self.manual_index, "天", "夫", kind="B1", auto=False)
            with patch("yeone_ocr.cli.resolve_mode"), patch.object(run, "log"), \
                    patch.object(run, "ensure_pages_read", return_value=False), \
                    patch.object(run, "collate", return_value=[item]), \
                    patch.object(run, "promote_repeated", side_effect=repeat), \
                    patch.object(run, "witness_filter", side_effect=witness), \
                    patch.object(run, "glyph_reader") as glyph, patch.object(run, "Editor") as editor:
                run.run_section(
                    self.args, pdf_doc=[None, None], work=self.work, book=book, anchors=anchors,
                    pdf_anchors=[], matches=[SimpleNamespace(pdf=None, how="none")] * len(anchors), idx=0,
                )
            glyph.assert_not_called()
            editor.assert_not_called()
        finally:
            book.doc.close()
        self.assertEqual(observed, {"repeat": [], "witness": []})
        self.assertEqual(self.target.read_bytes(), before)

    def test_outline_mismatch_keeps_manual_corrected_file_and_history_in_place(self):
        archive = _Archive(self.target.read_bytes())
        from yeone_ocr.collate.hwpx_edit import _slots
        _slots(archive.paragraphs[self.scope.start])[0].text = "別方"
        staged = self.root / "changed.hwpx"
        archive.write(staged, {archive.locations[self.scope.start][0]})
        staged.replace(self.target)
        before = self.target.read_bytes()
        history = manual_history_path(self.target).read_bytes()
        args = Namespace(**vars(self.args), anchor_index=1, work=str(self.root / "work"),
                         anchor_source="auto", heading_styles="", section=None)
        with patch.object(run, "run_section") as replay, patch.object(run, "set_aside") as move, \
                patch.object(run, "log"):
            code = run.cmd_collate(args)
        self.assertEqual(code, 1)
        replay.assert_not_called()
        move.assert_not_called()
        self.assertEqual(self.target.read_bytes(), before)
        self.assertEqual(manual_history_path(self.target).read_bytes(), history)
        self.assertEqual(self.source.read_bytes(), self.original_bytes)
        self.assertFalse(list(self.root.glob("*_이전_*.hwpx")))

    def test_external_target_change_during_ocr_is_not_overwritten(self):
        external = []

        def modify(*_):
            with zipfile.ZipFile(self.target, "a") as archive:
                archive.comment = b"external writer changed this file"
            external.append(self.target.read_bytes())
            return False

        with self.assertRaises(run.SaveFailed):
            self._replay([self._proposal(self.other_index, "甘", "炙")], during_read=modify)
        self.assertEqual(self.target.read_bytes(), external[0])
        self.assertEqual(self.source.read_bytes(), self.original_bytes)
        self.assertEqual(manual_protected_paragraphs(self.target), {self.manual_index})

    def test_external_original_change_during_ocr_prevents_publication(self):
        before = self.target.read_bytes()
        external = []

        def modify(*_):
            with zipfile.ZipFile(self.source, "a") as archive:
                archive.comment = b"original changed while corrected copy was loaded"
            external.append(self.source.read_bytes())
            return False

        with self.assertRaises(run.SaveFailed):
            self._replay([self._proposal(self.other_index, "甘", "炙")], during_read=modify)
        self.assertEqual(self.source.read_bytes(), external[0])
        self.assertEqual(self.target.read_bytes(), before)

    def test_external_target_change_during_serialization_is_not_overwritten(self):
        from hwpx.document import HwpxDocument

        real_save = HwpxDocument.save_to_path
        external = []
        history = manual_history_path(self.target).read_bytes()

        def save_then_external_change(document, *args, **kwargs):
            result = real_save(document, *args, **kwargs)
            with zipfile.ZipFile(self.target, "a") as archive:
                archive.comment = b"external writer during slow serialization"
            external.append(self.target.read_bytes())
            return result

        with patch.object(HwpxDocument, "save_to_path", new=save_then_external_change), \
                self.assertRaises(run.SaveFailed):
            self._replay([self._proposal(self.other_index, "甘", "炙")])
        self.assertEqual(len(external), 1)
        self.assertEqual(self.target.read_bytes(), external[0])
        self.assertEqual(manual_history_path(self.target).read_bytes(), history)
        self.assertEqual(self.source.read_bytes(), self.original_bytes)
        self.assertFalse(self.target.with_suffix(".tmp.hwpx").exists())

    def test_invalid_manual_record_blocks_before_ocr_or_output_mutation(self):
        history = manual_history_path(self.target)
        history.write_text('{"schema_version": 999}', encoding="utf-8")
        before = self.target.read_bytes()
        with patch.object(run, "ensure_pages_read") as read:
            book = HwpxBook(str(self.target))
            try:
                anchors = book.anchors()
                with self.assertRaises(run.SaveFailed):
                    run.run_section(
                        self.args, pdf_doc=[None, None], work=self.work, book=book, anchors=anchors,
                        pdf_anchors=[], matches=[SimpleNamespace(pdf=None, how="none")] * len(anchors), idx=0,
                    )
            finally:
                book.doc.close()
        read.assert_not_called()
        self.assertEqual(self.target.read_bytes(), before)
        self.assertEqual(history.read_text("utf-8"), '{"schema_version": 999}')


if __name__ == "__main__":
    unittest.main()
