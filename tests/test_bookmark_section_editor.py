"""Direct bookmark editing: drafts, review, conflicts, closing and PDF scope."""
from concurrent.futures import Future
import contextlib
from pathlib import Path
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import pymupdf
from PIL import Image

from yeone_ocr.collate.section_editor import BookmarkSectionEditor, ChangeReviewDialog, render_pdf_page
from test_ops import _real_tk


class FakeSession:
    def __init__(self, original, corrected=None):
        self.source_path = original
        self.corrected_path = corrected or original.with_name(original.stem + "_교정.hwpx")
        self.paragraphs = [
            SimpleNamespace(index=10, original_text="瀉白散", corrected_text="瀉白散", editable=False, readonly_reason="제목 문단은 보기만 가능합니다."),
            SimpleNamespace(index=11, original_text="上藥人梗米", corrected_text="上藥入粳米", editable=True, readonly_reason=""),
            SimpleNamespace(index=12, original_text="氣喘咳嗽", corrected_text="氣喘咳嗽", editable=True, readonly_reason=""),
        ]
        self.calls = []
        self.failure = None

    def save(self, changes):
        if self.failure:
            raise self.failure
        self.calls.append(dict(changes))
        for paragraph in self.paragraphs:
            if paragraph.index in changes:
                paragraph.corrected_text = changes[paragraph.index]
        return SimpleNamespace(path=self.corrected_path, saved_count=len(changes),
                               backup_path=self.corrected_path.with_suffix(".bak.hwpx"), reports_stale=True)


class PdfRendering(unittest.TestCase):
    def test_page_numbers_are_one_based_and_renders_do_not_modify_source(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "source.pdf"
            with pymupdf.open() as document:
                page = document.new_page(width=200, height=300)
                page.draw_rect(page.rect, color=(1, 0, 0), fill=(1, 0, 0))
                page = document.new_page(width=200, height=300)
                page.draw_rect(page.rect, color=(0, 0, 1), fill=(0, 0, 1))
                document.save(path)
            before = path.read_bytes()
            first = render_pdf_page(str(path), 1, 432, None)
            second = render_pdf_page(str(path), 2, 432, .5)
            self.assertEqual(first.size, (400, 600))
            self.assertEqual(second.size, (100, 150))
            self.assertEqual(first.getpixel((20, 20)), (255, 0, 0))
            self.assertEqual(second.getpixel((20, 20)), (0, 0, 255))
            first.close(); second.close()
            with self.assertRaises(ValueError):
                render_pdf_page(str(path), 0, 432, None)
            self.assertEqual(path.read_bytes(), before)


class SectionEditorTk(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = _real_tk()
        cls.root.withdraw()

    @classmethod
    def tearDownClass(cls):
        cls.root.destroy()

    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.folder = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.pdf = self.folder / "book.pdf"
        with pymupdf.open() as document:
            for _ in range(4):
                document.new_page(width=300, height=400)
            document.save(self.pdf)
        self.hwpx = self.folder / "book.hwpx"
        self.hwpx.write_bytes(b"original fixture; GUI must never overwrite")
        self.session = FakeSession(self.hwpx)
        backend = ModuleType("yeone_ocr.collate.section_edit")
        self.factory = Mock(return_value=self.session)
        backend.SectionEditSession = SimpleNamespace(open=self.factory)
        self.stack.enter_context(patch.dict(sys.modules, {"yeone_ocr.collate.section_edit": backend}))
        self.error = self.stack.enter_context(patch("yeone_ocr.collate.section_editor.messagebox.showerror"))
        self.info = self.stack.enter_context(patch("yeone_ocr.collate.section_editor.messagebox.showinfo"))
        self.choice = self.stack.enter_context(patch("yeone_ocr.collate.section_editor.messagebox.askyesnocancel"))
        self.confirm = self.stack.enter_context(patch.object(BookmarkSectionEditor, "_confirm_changes", return_value=True))
        self.can_write = Mock(return_value=True)
        self.saved = Mock()
        self.editor = BookmarkSectionEditor(self.root, pdf_path=self.pdf, hwpx_path=self.hwpx, section_name="瀉白散",
                                             section_start=10, section_end=13, pdf_pages=(2, 3), can_write=self.can_write,
                                             on_saved=self.saved, heading_styles=["제목-처방명"], anchor_source="hwpx")
        self.editor.withdraw()
        self.addCleanup(lambda: self.editor.destroy() if not self.editor._closed else None)
        self.root.update()

    def edit(self, index, value):
        widget = self.editor._editors[index]
        widget.delete("1.0", "end")
        widget.insert("1.0", value)
        widget.edit_modified(False)
        self.editor._update_status()

    def test_section_scope_and_custom_index_settings_are_passed_to_backend(self):
        self.factory.assert_called_once_with(str(self.pdf), str(self.hwpx), "瀉白散", 10, 13,
                                             heading_styles=["제목-처방명"], anchor_source="hwpx")
        self.assertEqual(str(self.editor._editors[10].cget("state")), "disabled")

    def test_pdf_paging_is_bounded_and_does_not_change_selected_paragraph(self):
        self.editor.select_paragraph(12)
        self.editor.set_page(99)
        self.assertEqual(self.editor._pdf_page, 3)
        self.assertEqual(self.editor._active, 12)
        self.assertEqual(str(self.editor.next_button.cget("state")), "disabled")
        self.editor.change_page(-20)
        self.assertEqual(self.editor._pdf_page, 2)
        self.assertEqual(str(self.editor.previous_button.cget("state")), "disabled")

    def test_switching_paragraph_preserves_drafts_and_save_reviews_only_changes(self):
        self.edit(11, "上藥入粳米一撮")
        self.editor.select_paragraph(12)
        self.assertEqual(self.editor._editors[11].get("1.0", "end-1c"), "上藥入粳米一撮")
        self.assertTrue(self.editor.dirty)
        self.assertTrue(self.editor.save())
        self.confirm.assert_called_once_with({11: "上藥入粳米一撮"})
        self.assertEqual(self.session.calls, [{11: "上藥入粳米一撮"}])
        self.assertFalse(self.editor.dirty)
        self.assertIn("수정 전 사본", self.editor.v_status.get())
        self.saved.assert_called_once_with(self.session.corrected_path)
        self.assertEqual(self.hwpx.read_bytes(), b"original fixture; GUI must never overwrite")

    def test_cancel_review_never_saves_or_discards_drafts(self):
        self.edit(12, "氣喘咳嗽。")
        self.confirm.return_value = False
        self.assertFalse(self.editor.save())
        self.assertEqual(self.session.calls, [])
        self.assertTrue(self.editor.dirty)
        self.assertFalse(self.editor.busy)

    def test_save_conflict_keeps_drafts_and_does_not_notify_success(self):
        self.edit(12, "修改文字")
        self.session.failure = ValueError("교정본이 외부에서 변경되었습니다. 다시 불러오십시오.")
        self.assertFalse(self.editor.save())
        self.assertTrue(self.editor.dirty)
        self.assertFalse(self.editor.busy)
        self.assertIn("외부에서 변경", self.error.call_args.args[1])
        self.saved.assert_not_called()

    def test_busy_parent_blocks_before_review_and_rechecks_after_review(self):
        self.edit(11, "修改")
        self.can_write.return_value = False
        self.assertFalse(self.editor.save())
        self.confirm.assert_not_called()
        self.can_write.side_effect = [True, False]
        self.assertFalse(self.editor.save())
        self.assertEqual(self.session.calls, [])
        self.assertTrue(self.editor.dirty)

    def test_close_cancel_or_failed_save_keeps_window_and_discard_closes(self):
        self.edit(11, "修改")
        self.choice.return_value = None
        self.assertFalse(self.editor.close())
        self.assertTrue(self.editor.winfo_exists())
        self.choice.return_value = True
        self.confirm.return_value = False
        self.assertFalse(self.editor.close())
        self.assertTrue(self.editor.dirty)
        self.choice.return_value = False
        self.assertTrue(self.editor.close())
        self.assertTrue(self.editor._closed)
        self.assertEqual(self.session.calls, [])

    def test_reload_cancel_or_failure_preserves_draft_and_discard_loads_new_snapshot(self):
        self.edit(11, "입력한 수정")
        self.choice.return_value = None
        self.assertFalse(self.editor.reload())
        self.assertEqual(self.factory.call_count, 1)
        self.choice.return_value = False
        self.factory.side_effect = ValueError("문서 구조가 바뀌었습니다")
        self.assertFalse(self.editor.reload())
        self.assertEqual(self.editor._editors[11].get("1.0", "end-1c"), "입력한 수정")
        replacement = FakeSession(self.hwpx)
        replacement.paragraphs[1].corrected_text = "外部修改"
        self.factory.side_effect = None; self.factory.return_value = replacement
        self.assertTrue(self.editor.reload())
        self.assertEqual(self.editor._editors[11].get("1.0", "end-1c"), "外部修改")
        self.assertFalse(self.editor.dirty)

    def test_difference_highlights_use_original_and_support_supplementary_characters(self):
        self.editor._paragraphs[11].original_text = "甲𠀀乙"
        self.edit(11, "甲𠀀丙")
        self.editor._highlight(11)
        widget = self.editor._editors[11]
        ranges = widget.tag_ranges("changed")
        self.assertEqual(widget.get(ranges[0], ranges[1]), "丙")
        self.editor.toggle_marks()
        self.assertEqual(widget.tag_ranges("changed"), ())
        self.assertEqual(widget.get("1.0", "end-1c"), "甲𠀀丙")

    def test_stale_pdf_render_cannot_replace_new_page(self):
        if self.editor._poll_job:
            self.editor.after_cancel(self.editor._poll_job)
            self.editor._poll_job = None
        future = Future()
        picture = Image.new("RGB", (3, 3), "white")
        future.set_result(picture)
        self.editor._render_generation = 5
        self.editor._render_future = (4, future)
        with patch.object(self.editor, "_display_pdf") as display:
            self.editor._poll_render()
        display.assert_not_called()
        picture.close()

    def test_ctrl_s_runs_review_and_review_dialog_is_read_only(self):
        self.edit(12, "修改")
        self.assertEqual(self.editor._save_shortcut(), "break")
        self.confirm.assert_called_once()
        dialog = ChangeReviewDialog(self.editor, {11: "修改後"}, {11: "修改前"}, self.session.corrected_path)
        try:
            texts = []
            def inspect(widget):
                for child in widget.winfo_children():
                    if child.winfo_class() == "Text":
                        texts.append(child)
                    inspect(child)
            inspect(dialog)
            self.assertEqual(len(texts), 1)
            self.assertEqual(str(texts[0].cget("state")), "disabled")
            self.assertIn("修改前", texts[0].get("1.0", "end"))
            self.assertIn("修改後", texts[0].get("1.0", "end"))
            self.assertFalse(dialog.accepted)
            dialog._accept()
            self.assertTrue(dialog.accepted)
        finally:
            if dialog.winfo_exists():
                dialog.destroy()


if __name__ == "__main__":
    unittest.main()
