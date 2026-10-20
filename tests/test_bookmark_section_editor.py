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

from yeone_ocr.collate.section_editor import (BookmarkSectionEditor, ChangeReviewDialog,
    CorrectionHistoryDialog, FindReplaceDialog, join_paragraph_lines, render_pdf_page)
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
        self.external_text = "他段皰疹"

    def save(self, changes, *, replacement_plan=None):
        if self.failure:
            raise self.failure
        self.calls.append(dict(changes))
        for paragraph in self.paragraphs:
            if paragraph.index in changes:
                paragraph.corrected_text = changes[paragraph.index]
        return SimpleNamespace(path=self.corrected_path, saved_count=len(changes),
                               backup_path=self.corrected_path.with_suffix(".bak.hwpx"), reports_stale=True)

    def preview_replace(self, find, replacement, *, scope, paragraph_index, changes, previous_plan=None, match=None):
        if not find:
            raise ValueError("찾을 글자를 입력하세요.")
        baseline = {p.index: p.corrected_text for p in self.paragraphs}
        baseline[99] = self.external_text
        wanted = ([paragraph_index] if scope == "paragraph" else
                  list(baseline) if scope == "document" else [p.index for p in self.paragraphs])
        result = dict(changes)
        entries, skipped = [], []
        for index in wanted:
            before = changes.get(index, baseline[index])
            count = before.count(find)
            if not count:
                continue
            if index == 10:
                skipped.append(SimpleNamespace(index=index, reason="제목 읽기 전용", count=count))
                continue
            if match is not None:
                if index != match[0]:
                    continue
                offset = match[1]
                after = before[:offset] + replacement + before[offset + len(find):]
                count = 1
            else:
                after = before.replace(find, replacement)
            entries.append(SimpleNamespace(index=index, before=before, after=after, count=count))
            if after != baseline[index]:
                result[index] = after
            else:
                result.pop(index, None)
        return SimpleNamespace(changes=result, baseline=baseline, entries=tuple(entries), skipped=tuple(skipped),
                               matches=sum(entry.count for entry in entries), paragraph_count=len(entries))


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
        self.yes = self.stack.enter_context(patch("yeone_ocr.collate.section_editor.messagebox.askyesno", return_value=True))
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

    def test_correction_history_shows_saved_records_without_saving_current_draft(self):
        records = [{"time": "2026-10-20 12:00:00", "section": "瀉白散", "paragraph": 11,
                    "before": "사火之劑", "after": "瀉火之劑"}]
        self.session.correction_records = Mock(return_value=records)
        self.edit(11, "아직 저장하지 않은 수정")
        with patch("yeone_ocr.collate.section_editor.CorrectionHistoryDialog") as dialog:
            self.editor.history_button.invoke()
        dialog.assert_called_once_with(self.editor, records)
        self.assertEqual(self.session.calls, [])
        self.assertTrue(self.editor.dirty)
        display = CorrectionHistoryDialog(self.editor, records)
        self.addCleanup(display.destroy)
        text = display.text.get("1.0", "end-1c")
        self.assertIn("문단 12", text)
        self.assertIn("사火之劑", text)
        self.assertIn("瀉火之劑", text)
        self.assertNotIn("아직 저장하지 않은 수정", text)
        self.assertEqual(str(display.text.cget("state")), "disabled")
        selected = display.text.tag_ranges("changed")
        self.assertEqual(display.text.get(selected[0], selected[1]), "瀉")

    def test_legacy_history_explains_that_only_new_saves_record_before_and_after(self):
        display = CorrectionHistoryDialog(self.editor, [])
        self.addCleanup(display.destroy)
        self.assertIn("이 버전부터", display.text.get("1.0", "end-1c"))
        self.assertIn("기존 판독 기록은 유지", display.note.cget("text"))
        self.assertEqual(self.session.calls, [])

    def test_save_status_reports_correction_memory_or_its_warning(self):
        original_save = self.session.save
        def save(changes):
            result = original_save(changes)
            result.learning_path = self.session.corrected_path.with_suffix(".manual.json")
            result.learning_count = 1
            result.learning_warning = ""
            return result
        self.session.save = save
        self.edit(11, "瀉火之劑")
        self.assertTrue(self.editor.save())
        self.assertIn("다음 새 판독 참고 1건", self.editor.v_status.get())
        def warning_save(changes):
            result = save(changes)
            result.learning_warning = "수정 기록 확인 필요"
            return result
        self.session.save = warning_save
        self.edit(11, "瀉火之劑。")
        self.assertTrue(self.editor.save())
        self.assertIn("수정 사례 참고 보류", self.editor.v_status.get())
        self.assertNotIn("다음 새 판독 참고", self.editor.v_status.get())

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

    def test_newline_repair_retains_other_drafts_and_can_be_undone(self):
        self.edit(11, "上藥\r\n入粳米\t𠀀")
        self.edit(12, "氣喘咳嗽。")
        self.yes.return_value = False
        self.assertFalse(self.editor.save())
        self.assertEqual(self.session.calls, [])
        self.assertEqual(self.editor._changes()[11], "上藥\r\n入粳米\t𠀀")
        self.assertEqual(self.editor._active, 11)
        self.yes.return_value = True
        self.confirm.return_value = False
        self.assertFalse(self.editor.save())
        self.assertEqual(self.editor._changes(), {11: "上藥 入粳米 𠀀", 12: "氣喘咳嗽。"})
        self.editor._editors[11].edit_undo()
        self.assertEqual(self.editor._editors[11].get("1.0", "end-1c"), "上藥\r\n入粳米\t𠀀")
        self.assertEqual(join_paragraph_lines(" 甲\r\n乙\t丙\u2028丁\u2029戊 "), " 甲 乙 丙 丁 戊 ")

    def test_unhandled_control_is_located_without_discarding_drafts(self):
        self.edit(11, "甲\x01乙")
        self.edit(12, "修改")
        self.assertFalse(self.editor.save())
        self.assertIn("문단 12", self.error.call_args.args[1])
        self.assertIn("U+0001", self.error.call_args.args[1])
        self.assertEqual(self.editor._changes(), {11: "甲\x01乙", 12: "修改"})
        self.yes.assert_not_called()

    def test_enter_is_blocked_tab_moves_and_paste_has_reversible_space_join(self):
        self.editor.deiconify()
        self.root.update()
        widget = self.editor._editors[11]
        widget.focus_force()
        widget.mark_set("insert", "end-1c")
        before = widget.get("1.0", "end-1c")
        widget.event_generate("<Return>")
        self.root.update()
        self.assertEqual(widget.get("1.0", "end-1c"), before)
        widget.event_generate("<Tab>")
        self.root.update()
        self.assertEqual(self.editor._active, 12)
        self.editor.clipboard_clear()
        self.editor.clipboard_append("皰\r\n液\t𠀀")
        self.editor._paste_paragraph(12)
        self.assertIn("皰 液 𠀀", self.editor._editors[12].get("1.0", "end-1c"))
        self.editor._editors[12].edit_undo()
        self.assertEqual(self.editor._editors[12].get("1.0", "end-1c"), "氣喘咳嗽")

    def replacement_dialog(self):
        self.edit(11, "皰疹，皰液。手改")
        self.edit(12, "原有手改")
        self.editor.open_find_replace()
        dialog = self.editor._find_dialog
        dialog.v_find.set("皰")
        dialog.v_replace.set("疱")
        return dialog

    def test_literal_replace_preview_stages_full_document_and_preserves_drafts(self):
        dialog = self.replacement_dialog()
        dialog.v_scope.set("교정본 전체")
        self.assertTrue(dialog.preview())
        self.assertEqual(dialog.plan.matches, 3)
        self.assertEqual(self.session.calls, [])
        self.assertEqual(self.editor._changes()[11], "皰疹，皰液。手改")
        self.assertTrue(dialog.replace_all())
        self.assertEqual(self.editor._changes(), {11: "疱疹，疱液。手改", 12: "原有手改", 99: "他段疱疹"})
        self.assertIn("현재 구획 밖 1개", self.editor.v_status.get())
        self.assertEqual(len(self.editor._editors), 3)
        self.assertEqual(self.session.calls, [])
        self.assertTrue(self.editor.save())
        self.assertEqual(self.session.calls, [{11: "疱疹，疱液。手改", 12: "原有手改", 99: "他段疱疹"}])
        self.assertFalse(self.editor.dirty)
        self.assertEqual(self.editor._replace_undo, [])

    def test_replace_all_undo_retains_drafts_from_before_replacement(self):
        dialog = self.replacement_dialog()
        dialog.v_scope.set("교정본 전체")
        dialog.preview()
        dialog.replace_all()
        self.assertTrue(self.editor._undo_replacement())
        self.assertEqual(self.editor._changes(), {11: "皰疹，皰液。手改", 12: "原有手改"})
        self.assertEqual(self.editor._external_changes, {})
        self.assertEqual(self.session.calls, [])

    def test_replace_preview_stales_if_drafts_change_and_undo_protects_later_edits(self):
        dialog = self.replacement_dialog()
        dialog.preview()
        self.edit(12, "後來修正")
        self.assertFalse(dialog.replace_all())
        self.assertEqual(self.editor._changes()[11], "皰疹，皰液。手改")
        dialog.preview()
        dialog.replace_all()
        self.edit(12, "另外修正")
        self.assertFalse(self.editor._undo_replacement())
        self.assertEqual(self.editor._changes()[12], "另外修正")

    def test_replace_single_uses_current_match_and_scope_keeps_other_sections(self):
        dialog = self.replacement_dialog()
        self.assertTrue(dialog.preview())
        dialog.find_next()
        dialog.find_next()
        self.assertEqual(dialog._occurrence, 1)
        self.assertTrue(dialog.replace_one())
        self.assertEqual(self.editor._changes()[11], "皰疹，疱液。手改")
        self.assertNotIn(99, self.editor._changes())
        self.assertEqual(self.session.calls, [])

    def test_full_document_drafts_are_reverted_or_retained_on_reload_failure(self):
        dialog = self.replacement_dialog()
        dialog.v_scope.set("교정본 전체")
        dialog.preview()
        dialog.replace_all()
        self.choice.return_value = False
        self.factory.side_effect = ValueError("外部修改")
        self.assertFalse(self.editor.reload())
        self.assertEqual(self.editor._changes()[99], "他段疱疹")
        self.editor.revert_changes()
        self.assertFalse(self.editor.dirty)
        self.assertIsNone(self.editor._replacement_plan)
        self.assertEqual(self.session.calls, [])

    def test_ctrl_h_does_not_delete_a_character_and_toolbar_fits_minimum_width(self):
        self.editor.deiconify()
        self.editor.geometry("900x640")
        self.root.update()
        self.editor._initial_split()
        widget = self.editor._editors[11]
        widget.focus_force()
        widget.mark_set("insert", "end-1c")
        before = widget.get("1.0", "end-1c")
        widget.event_generate("<Control-h>")
        self.root.update()
        self.assertEqual(widget.get("1.0", "end-1c"), before)
        self.assertTrue(self.editor._find_dialog.winfo_exists())
        button = self.editor.find_button
        self.assertGreater(button.winfo_width(), 80)
        self.assertLessEqual(button.winfo_rootx() + button.winfo_width(),
                             self.editor.doc_panel.winfo_rootx() + self.editor.doc_panel.winfo_width())

    def test_existing_line_breaks_outside_section_remain_valid_after_replacement(self):
        self.session.external_text = "他段皰\n疹"
        dialog = self.replacement_dialog()
        dialog.v_scope.set("교정본 전체")
        dialog.preview()
        dialog.replace_all()
        self.assertTrue(self.editor.save())
        self.assertEqual(self.session.calls[0][99], "他段疱\n疹")
        self.yes.assert_not_called()

    def test_repair_preserves_preexisting_separator_while_joining_added_tab(self):
        self.editor._baseline[11] = "甲\n皰"
        self.edit(11, "甲\n疱\t液")
        self.assertTrue(self.editor._prepare_paragraph_input())
        self.assertEqual(self.editor._changes()[11], "甲\n疱 液")

    def test_an_independent_preview_cannot_apply_after_document_reload(self):
        self.edit(11, "皰疹")
        self.session.paragraphs[1].corrected_text = "皰疹"
        self.editor._baseline[11] = "皰疹"
        dialog = FindReplaceDialog(self.editor)
        self.addCleanup(lambda: dialog.destroy() if dialog.winfo_exists() else None)
        dialog.v_find.set("皰")
        dialog.v_replace.set("疱")
        self.assertTrue(dialog.preview())
        self.assertTrue(self.editor.reload())
        self.assertFalse(dialog.replace_all())
        self.assertFalse(self.editor.dirty)


if __name__ == "__main__":
    unittest.main()
