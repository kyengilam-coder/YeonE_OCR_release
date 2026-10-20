"""Cursor/PDF following uses the current caret and discards stale worker results."""
from concurrent.futures import Future
import contextlib
from pathlib import Path
import sys
import tempfile
from tkinter import ttk
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import pymupdf
from PIL import Image

from yeone_ocr.collate.section_editor import BookmarkSectionEditor, validate_pdf_range
import test_bookmark_section_editor as editor_fixtures
from test_ops import _real_tk


class ControlledExecutor:
    """Run no background work until the test explicitly completes a Future."""
    def __init__(self, *, immediate=(), **_kwargs):
        self.calls = []
        self.immediate_calls = []
        self.immediate = immediate
        self.shutdown_calls = []

    def submit(self, function, *args, **kwargs):
        future = Future()
        if any(function is item for item in self.immediate):
            self.immediate_calls.append((function, args, kwargs))
            try:
                future.set_result(function(*args, **kwargs))
            except Exception as exc:
                future.set_exception(exc)
            return future
        self.calls.append((function, args, kwargs, future))
        return future

    def finish(self, call=-1):
        function, args, kwargs, future = self.calls[call]
        if not future.cancelled():
            try:
                future.set_result(function(*args, **kwargs))
            except Exception as exc:
                future.set_exception(exc)
        return future

    def shutdown(self, **kwargs):
        self.shutdown_calls.append(kwargs)
        if kwargs.get("cancel_futures"):
            for _, _, _, future in self.calls:
                future.cancel()


def location(page=2, *, precise=True):
    return SimpleNamespace(
        page=page, boxes=((.1, .2, .8, .3),), caret_box=(.4, .2, .45, .3),
        precise=precise, message=f"커서 위치 · PDF {page}쪽" if precise else f"주변 문맥 · PDF {page}쪽",
    )


class BookmarkCursorTk(unittest.TestCase):
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
            for _ in range(3):
                document.new_page(width=300, height=400)
            document.save(self.pdf)
        self.hwpx = self.folder / "book.hwpx"
        self.hwpx.write_bytes(b"unchanged source")
        self.session = editor_fixtures.FakeSession(self.hwpx)
        self.factory = Mock(return_value=self.session)
        backend = ModuleType("yeone_ocr.collate.section_edit")
        backend.SectionEditSession = SimpleNamespace(open=self.factory)
        self.mapping = SimpleNamespace(
            available=True, locate=Mock(return_value=location()),
            page_font_width=Mock(return_value=None), page_font_height=Mock(return_value=None))
        mapper = ModuleType("yeone_ocr.collate.cursor_map")
        self.snapshot = object()
        self.read_pages = Mock(return_value=self.snapshot)
        self.build_map = Mock(return_value=self.mapping)
        mapper.read_cursor_pages = self.read_pages
        mapper.align_cursor_map = self.build_map
        self.stack.enter_context(patch.dict(sys.modules, {
            "yeone_ocr.collate.section_edit": backend,
            "yeone_ocr.collate.cursor_map": mapper,
        }))
        self.executors = [ControlledExecutor(immediate=(validate_pdf_range,))]
        self.stack.enter_context(patch(
            "yeone_ocr.collate.section_editor.shared_pdf_worker", return_value=self.executors[0]))

        def executor(**kwargs):
            result = ControlledExecutor(**kwargs)
            self.executors.append(result)
            return result

        self.stack.enter_context(patch(
            "yeone_ocr.collate.section_editor.ThreadPoolExecutor", side_effect=executor))
        self.error = self.stack.enter_context(patch(
            "yeone_ocr.collate.section_editor.messagebox.showerror"))
        self.editor = BookmarkSectionEditor(
            self.root, pdf_path=self.pdf, hwpx_path=self.hwpx, section_name="瀉白散",
            section_start=10, section_end=13, pdf_pages=(2, 3))
        self.editor.attributes("-alpha", 0)
        self.editor.withdraw()
        self.addCleanup(lambda: self.editor.destroy() if not self.editor._closed else None)
        self.root.update()
        # All test completion is explicit, independent of worker or timer speed.
        if self.editor._poll_job is not None:
            self.editor.after_cancel(self.editor._poll_job)
            self.editor._poll_job = None

    def finish_index(self):
        future = self.editor._index_future[1]
        for i, call in enumerate(self.executors[0].calls):
            if call[3] is future:
                self.executors[0].finish(i)
                break
        self.editor._poll_cursor()

    def finish_alignment(self):
        future = self.editor._alignment_future[1]
        for i, call in enumerate(self.executors[1].calls):
            if call[3] is future:
                self.executors[1].finish(i)
                break
        self.editor._poll_cursor()

    def ready(self):
        self.finish_index()
        self.finish_alignment()
        self.assertIs(self.editor._cursor_map, self.mapping)
        self.editor._cancel_cursor_request()

    def submit(self, index=11, offset=0):
        editor = self.editor
        editor.select_paragraph(index)
        editor._editors[index].mark_set("insert", f"1.0+{offset}c")
        if editor._cursor_job is not None:
            editor.after_cancel(editor._cursor_job)
        editor._submit_cursor()
        return editor._cursor_future[1]

    def complete_cursor(self):
        self.executors[1].finish()
        self.editor._poll_cursor()

    def test_build_uses_only_the_selected_pages_and_snapshot_paragraphs(self):
        self.ready()
        self.read_pages.assert_called_once_with(str(self.pdf), (2, 3))
        self.build_map.assert_called_once_with(self.snapshot, tuple(self.session.paragraphs))
        self.assertTrue(self.editor.v_follow_cursor.get())
        self.assertEqual(self.hwpx.read_bytes(), b"unchanged source")
        self.assertFalse(self.editor.dirty)

    def test_mouse_release_tracks_the_caret_after_click_within_same_paragraph(self):
        self.ready()
        self.editor.deiconify()
        self.root.update()
        self.editor.select_paragraph(11)
        widget = self.editor._editors[11]
        widget.mark_set("insert", "1.0+3c")
        # The class Button-1 binding has already moved insert before release.
        widget.event_generate("<ButtonRelease-1>", x=20, y=8)
        self.root.update()
        self.assertIsNotNone(self.editor._cursor_job)
        self.editor.after_cancel(self.editor._cursor_job)
        self.editor._submit_cursor()
        self.complete_cursor()
        self.mapping.locate.assert_called_once_with(11, "上藥入粳米", 3)

    def test_keyboard_move_uses_new_caret_without_changing_active_paragraph(self):
        self.ready()
        self.editor.deiconify()
        self.root.update()
        widget = self.editor._editors[11]
        widget.focus_force()
        self.root.update()
        widget.mark_set("insert", "1.0+1c")
        widget.event_generate("<KeyPress-Right>")
        widget.event_generate("<KeyRelease-Right>")
        self.root.update()
        self.assertEqual(widget.get("1.0", "insert"), "上藥")
        self.assertEqual(self.editor._active, 11)
        self.assertIsNotNone(self.editor._cursor_job)
        self.editor.after_cancel(self.editor._cursor_job)
        self.editor._submit_cursor()
        self.complete_cursor()
        self.mapping.locate.assert_called_once_with(11, "上藥入粳米", 2)

    def test_python_offset_counts_supplementary_character_once_and_current_draft(self):
        self.ready()
        widget = self.editor._editors[11]
        widget.delete("1.0", "end")
        widget.insert("1.0", "甲𠀀乙丙")
        self.submit(offset=2)
        self.complete_cursor()
        self.mapping.locate.assert_called_once_with(11, "甲𠀀乙丙", 2)
        self.assertTrue(self.editor.dirty)
        self.assertEqual(self.session.calls, [])

    def test_precise_match_changes_page_and_draws_normalized_line_and_glyph_after_render(self):
        self.ready()
        self.editor._apply_cursor_location(location(3))
        self.assertEqual(self.editor._pdf_page, 3)
        self.assertEqual(self.editor.v_page.get(), "3")
        self.assertEqual(self.editor.pdf_canvas.find_withtag("cursor-location"), ())
        self.editor._display_pdf(Image.new("RGB", (200, 300), "white"))
        canvas = self.editor.pdf_canvas
        line, = canvas.find_withtag("cursor-line")
        glyph, = canvas.find_withtag("cursor-glyph")
        self.assertEqual(canvas.coords(line), [36, 76, 176, 106])
        self.assertEqual(canvas.coords(glyph), [96, 76, 106, 106])
        self.assertEqual(canvas.itemcget(line, "fill"), "#ffe28a")
        self.assertEqual(canvas.itemcget(glyph, "outline"), "#ba3e28")
        self.assertIn("PDF 3쪽", self.editor.pdf_note.cget("text"))

    def test_zoom_rebuilds_markers_at_new_image_scale_without_changing_location(self):
        self.ready()
        self.editor._apply_cursor_location(location())
        self.editor._display_pdf(Image.new("RGB", (200, 300), "white"))
        self.editor.set_zoom(2.0)
        self.assertTrue(self.editor._cursor_scroll_pending)
        self.editor._display_pdf(Image.new("RGB", (400, 600), "white"))
        canvas = self.editor.pdf_canvas
        self.assertEqual(len(canvas.find_withtag("cursor-location")), 2)
        glyph, = canvas.find_withtag("cursor-glyph")
        self.assertEqual(canvas.coords(glyph), [176, 136, 196, 196])
        self.assertEqual(self.editor._cursor_location.page, 2)

    def test_approximate_match_has_line_only_even_if_caret_box_is_present(self):
        self.ready()
        self.editor._display_pdf(Image.new("RGB", (200, 300), "white"))
        self.editor._apply_cursor_location(location(precise=False))
        self.assertEqual(len(self.editor.pdf_canvas.find_withtag("cursor-line")), 1)
        self.assertEqual(self.editor.pdf_canvas.find_withtag("cursor-glyph"), ())

    def test_unknown_or_out_of_scope_match_clears_existing_marker_without_page_jump(self):
        self.ready()
        self.editor._display_pdf(Image.new("RGB", (200, 300), "white"))
        for unavailable in (None, location(1), location(4)):
            self.editor._apply_cursor_location(location())
            self.assertTrue(self.editor.pdf_canvas.find_withtag("cursor-location"))
            self.editor._apply_cursor_location(unavailable)
            self.assertIsNone(self.editor._cursor_location)
            self.assertEqual(self.editor.pdf_canvas.find_withtag("cursor-location"), ())
            self.assertEqual(self.editor._pdf_page, 2)
            self.assertIn("확실히", self.editor.pdf_note.cget("text"))

    def test_running_old_cursor_result_is_ignored_after_caret_move(self):
        self.ready()
        old = self.submit(offset=1)
        old_generation = self.editor._cursor_generation
        old.set_running_or_notify_cancel()
        self.editor._editors[11].mark_set("insert", "1.0+4c")
        self.editor._schedule_cursor(11)
        old.set_result(location(3))
        # Simulate completion delivery from the abandoned running worker.
        self.editor._cursor_future = old_generation, old
        with patch.object(self.editor, "_apply_cursor_location") as apply:
            self.editor._poll_cursor()
        apply.assert_not_called()
        self.assertEqual(self.editor._pdf_page, 2)

    def test_manual_page_navigation_invalidates_inflight_lookup_and_pending_index(self):
        self.ready()
        old = self.submit(offset=1)
        generation = self.editor._cursor_generation
        old.set_running_or_notify_cancel()
        self.editor.set_page(3)
        old.set_result(location(2))
        self.editor._cursor_future = generation, old
        self.editor._poll_cursor()
        self.assertEqual(self.editor._pdf_page, 3)
        self.assertIsNone(self.editor._cursor_location)
        self.assertFalse(self.editor._cursor_requested)
        self.editor._start_cursor_map()
        self.editor.set_page(2)
        self.finish_index()
        self.finish_alignment()
        self.assertIsNone(self.editor._cursor_job)
        self.assertFalse(self.editor._cursor_requested)

    def test_turning_follow_off_clears_and_cancels_even_running_result_without_render(self):
        self.ready()
        self.editor._display_pdf(Image.new("RGB", (200, 300), "white"))
        self.editor._apply_cursor_location(location())
        old = self.submit(offset=1)
        generation = self.editor._cursor_generation
        old.set_running_or_notify_cancel()
        with patch.object(self.editor, "_request_render") as render:
            self.editor.v_follow_cursor.set(False)
            self.editor._toggle_cursor_follow()
            old.set_result(location(3))
            self.editor._cursor_future = generation, old
            self.editor._poll_cursor()
            self.editor._schedule_cursor(11)
            render.assert_not_called()
        self.assertIsNone(self.editor._cursor_job)
        self.assertIsNone(self.editor._cursor_location)
        self.assertEqual(self.editor._pdf_page, 2)
        self.assertEqual(self.editor.pdf_canvas.find_withtag("cursor-location"), ())
        self.assertIn("꺼짐", self.editor.pdf_note.cget("text"))

    def test_unavailable_pdf_text_does_not_submit_a_lookup(self):
        self.mapping.available = False
        self.ready()
        self.editor.select_paragraph(11)
        self.editor.after_cancel(self.editor._cursor_job)
        self.editor._submit_cursor()
        self.mapping.locate.assert_not_called()
        self.assertIsNone(self.editor._cursor_future)
        self.assertIn("위치 정보가 없습니다", self.editor.pdf_note.cget("text"))

    def test_reload_rebuilds_map_and_rejects_old_alignment_completion(self):
        self.ready()
        self.editor._start_cursor_map()
        self.finish_index()
        old_generation, old = self.editor._alignment_future
        old.set_running_or_notify_cancel()
        replacement = editor_fixtures.FakeSession(self.hwpx)
        replacement.paragraphs[1].corrected_text = "外部修改"
        self.factory.return_value = replacement
        self.assertTrue(self.editor.reload())
        self.assertGreater(self.editor._index_future[0], old_generation)
        old.set_result(SimpleNamespace(available=True, locate=Mock()))
        self.editor._alignment_future = old_generation, old
        self.editor._poll_cursor()
        self.assertIsNone(self.editor._cursor_map)
        self.finish_index()
        self.finish_alignment()
        self.assertEqual(self.build_map.call_args.args[1], tuple(replacement.paragraphs))
        self.assertIs(self.editor._cursor_map, self.mapping)

    def test_mapping_failure_can_be_retried_by_toggling_follow(self):
        self.editor._index_future[1].set_exception(ValueError("fixture unavailable"))
        self.editor._poll_cursor()
        self.assertIsNone(self.editor._cursor_map)
        self.assertIn("재시도", self.editor.pdf_note.cget("text"))
        self.editor.v_follow_cursor.set(False)
        self.editor._toggle_cursor_follow()
        self.editor.v_follow_cursor.set(True)
        self.editor._toggle_cursor_follow()
        self.assertIsNotNone(self.editor._index_future)
        self.finish_index()
        self.finish_alignment()
        self.assertIs(self.editor._cursor_map, self.mapping)

    def test_turning_follow_back_on_during_indexing_finds_current_caret_when_ready(self):
        self.editor.select_paragraph(11)
        self.editor._editors[11].mark_set("insert", "1.0+3c")
        self.editor.v_follow_cursor.set(False)
        self.editor._toggle_cursor_follow()
        self.editor.v_follow_cursor.set(True)
        self.editor._toggle_cursor_follow()
        self.finish_index()
        self.finish_alignment()
        self.assertIsNotNone(self.editor._cursor_job)
        self.editor.after_cancel(self.editor._cursor_job)
        self.editor._submit_cursor()
        self.complete_cursor()
        self.mapping.locate.assert_called_once_with(11, "上藥入粳米", 3)

    def test_close_cancels_jobs_and_both_workers_without_waiting_for_running_work(self):
        self.ready()
        pending = self.submit(offset=1)
        pending.set_running_or_notify_cancel()
        self.editor._schedule_cursor(11)
        jobs = [job for job in (
            self.editor._initial_job, self.editor._render_job, self.editor._poll_job,
            self.editor._layout_job, self.editor._cursor_job, self.editor._position_job) if job is not None]
        self.assertTrue(jobs)
        self.assertTrue(self.editor.close())
        self.assertTrue(pending.running())
        remaining = self.root.tk.call("after", "info")
        for job in jobs:
            self.assertNotIn(job, remaining)
        self.assertEqual(self.executors[0].shutdown_calls, [])
        self.assertEqual(self.executors[1].shutdown_calls, [{"wait": False, "cancel_futures": True}])
        pending.set_result(location(3))
        self.root.update()
        self.assertEqual(self.hwpx.read_bytes(), b"unchanged source")

    def test_no_image_or_different_rendered_page_never_draws_wrong_overlay(self):
        self.ready()
        self.editor._apply_cursor_location(location())
        self.assertEqual(self.editor.pdf_canvas.find_withtag("cursor-location"), ())
        self.editor._image_size = (200, 300)
        self.editor._displayed_page = 3
        self.editor._draw_cursor_marker()
        self.assertEqual(self.editor.pdf_canvas.find_withtag("cursor-location"), ())

    def test_pdf_text_read_and_render_share_executor_but_alignment_uses_other_worker(self):
        self.assertEqual([call[0] for call in self.executors[0].immediate_calls],
                         [validate_pdf_range])
        self.assertEqual(self.executors[0].calls[0][0], self.read_pages)
        self.editor._submit_render()
        self.assertEqual(self.executors[0].calls[-1][0].__name__, "render_pdf_page")
        self.assertEqual(self.executors[1].calls, [])
        self.finish_index()
        self.assertEqual(self.executors[1].calls[0][0], self.build_map)
        self.finish_alignment()
        self.submit()
        self.assertEqual(self.executors[1].calls[-1][0], self.mapping.locate)

    def test_two_windows_share_pdf_queue_and_closing_one_keeps_other_requests_alive(self):
        first_index = self.editor._index_future[1]
        second = BookmarkSectionEditor(
            self.root, pdf_path=self.pdf, hwpx_path=self.hwpx, section_name="다른 구획",
            section_start=10, section_end=13, pdf_pages=(2, 3))
        second.attributes("-alpha", 0)
        second.withdraw()
        self.addCleanup(lambda: second.destroy() if not second._closed else None)
        self.assertIs(second._executor, self.editor._executor)
        second_index = second._index_future[1]
        self.assertIsNot(second._alignment_executor, self.editor._alignment_executor)
        self.editor.close()
        self.assertTrue(first_index.cancelled())
        self.assertFalse(second_index.cancelled())
        self.assertEqual(self.executors[0].shutdown_calls, [])
        for i, call in enumerate(self.executors[0].calls):
            if call[3] is second_index:
                self.executors[0].finish(i)
        second._poll_cursor()
        self.assertIsNotNone(second._alignment_future)
        self.executors[2].finish()
        second._poll_cursor()
        self.assertIs(second._cursor_map, self.mapping)

    def test_pdf_font_matches_measured_cjk_width_and_zoom_without_touching_text(self):
        self.ready()
        self.mapping.page_font_width.return_value = .04
        self.mapping.page_font_height.return_value = .05
        before = {index: widget.get("1.0", "end-1c") for index, widget in self.editor._editors.items()}
        self.editor._editors[11].mark_set("insert", "1.0+2c")
        self.assertTrue(self.editor.v_match_pdf_font.get())
        self.editor._display_pdf(Image.new("RGB", (400, 600), "white"))
        self.assertLessEqual(abs(self.editor._editor_font.measure("漢") - 16), 1)
        self.assertLess(int(self.editor._editor_font.cget("size")), 0)
        self.editor.set_zoom(2.0)
        self.editor._display_pdf(Image.new("RGB", (800, 1200), "white"))
        self.assertLessEqual(abs(self.editor._editor_font.measure("漢") - 32), 1)
        for index, widget in self.editor._editors.items():
            self.assertEqual(widget.get("1.0", "end-1c"), before[index])
            self.assertEqual(str(widget.cget("font")), str(self.editor._editor_font))
        self.assertEqual(self.editor._editors[11].get("1.0", "insert"), "上藥")
        self.assertFalse(self.editor.dirty)
        self.assertEqual(self.session.calls, [])

    def test_manual_font_adjustment_suspends_matching_until_enabled_again(self):
        self.ready()
        self.mapping.page_font_width.return_value = .04
        self.editor._display_pdf(Image.new("RGB", (400, 600), "white"))
        matching_width = self.editor._editor_font.measure("漢")
        self.editor.change_text_size(1)
        self.assertFalse(self.editor.v_match_pdf_font.get())
        manual_size = int(self.editor._editor_font.cget("size"))
        self.assertGreater(self.editor._editor_font.measure("漢"), matching_width)
        self.editor._display_pdf(Image.new("RGB", (800, 1200), "white"))
        self.assertEqual(int(self.editor._editor_font.cget("size")), manual_size)
        self.editor.v_match_pdf_font.set(True)
        self.editor._sync_text_size()
        self.assertLessEqual(abs(self.editor._editor_font.measure("漢") - 32), 1)
        self.editor.change_text_size(-1)
        self.assertFalse(self.editor.v_match_pdf_font.get())
        self.assertLess(self.editor._editor_font.measure("漢"), 32)
        self.assertFalse(self.editor.dirty)

    def test_font_height_is_fallback_and_missing_metrics_preserve_current_font(self):
        self.ready()
        original = int(self.editor._editor_font.cget("size"))
        self.editor._display_pdf(Image.new("RGB", (400, 600), "white"))
        self.assertEqual(int(self.editor._editor_font.cget("size")), original)
        self.mapping.page_font_height.return_value = .04
        self.editor._sync_text_size()
        self.assertLessEqual(abs(self.editor._editor_font.metrics("linespace") - 24), 1)
        matched = int(self.editor._editor_font.cget("size"))
        self.mapping.page_font_height.return_value = None
        self.editor._display_pdf(Image.new("RGB", (800, 1200), "white"))
        self.assertEqual(int(self.editor._editor_font.cget("size")), matched)
        self.assertFalse(self.editor.dirty)

    def show_for_positioning(self):
        self.editor.deiconify()
        self.root.update()
        self.editor.select_paragraph(11)
        self.editor._editors[11].mark_set("insert", "1.0+2c")
        self.editor._layout_paragraphs()
        self.root.update_idletasks()
        self.editor._display_pdf(Image.new("RGB", (600, 1200), "white"))

    def assert_same_screen_height(self):
        canvas = self.editor.pdf_canvas
        glyph, = canvas.find_withtag("cursor-glyph")
        _, y0, _, y1 = canvas.coords(glyph)
        pdf_screen_y = canvas.winfo_rooty() + (y0 + y1) / 2 - canvas.canvasy(0)
        editor_screen_y = self.editor._editor_cursor_y(ensure_visible=False)
        self.assertIsNotNone(editor_screen_y)
        self.assertLessEqual(abs(pdf_screen_y - editor_screen_y), 2)

    def test_pdf_marker_and_right_caret_share_screen_height_even_at_page_edges(self):
        self.ready()
        self.show_for_positioning()
        for y0, y1 in ((.01, .03), (.5, .52), (.97, .99)):
            point = location()
            point.boxes = ((.1, y0, .8, y1),)
            point.caret_box = (.4, y0, .45, y1)
            self.editor._apply_cursor_location(point)
            self.root.update_idletasks()
            self.assert_same_screen_height()
        self.assertFalse(self.editor.dirty)

    def test_document_scroll_repositions_marker_without_moving_right_document_back(self):
        self.ready()
        self.show_for_positioning()
        point = location()
        point.boxes = ((.1, .6, .8, .62),)
        point.caret_box = (.4, .6, .45, .62)
        self.editor._apply_cursor_location(point)
        # Extra document space makes a small user scroll possible while the
        # same right-hand caret remains inside the viewport.
        bounds = self.editor.doc_canvas.bbox("all")
        self.editor.doc_canvas.configure(scrollregion=(0, 0, bounds[2], 1600))
        self.editor.doc_canvas.yview_moveto(.015)
        self.root.update_idletasks()
        before = self.editor.doc_canvas.yview()
        self.assertIsNotNone(self.editor._position_job)
        self.editor.after_cancel(self.editor._position_job)
        self.editor._position_cursor()
        self.assert_same_screen_height()
        self.assertEqual(self.editor.doc_canvas.yview(), before)
        self.assertFalse(self.editor.dirty)

    def test_scrolling_caret_out_of_view_does_not_jump_either_pane_back(self):
        self.ready()
        self.show_for_positioning()
        point = location()
        point.caret_box = (.4, .6, .45, .62)
        point.boxes = ((.1, .6, .8, .62),)
        self.editor._apply_cursor_location(point)
        bounds = self.editor.doc_canvas.bbox("all")
        self.editor.doc_canvas.configure(scrollregion=(0, 0, bounds[2], 2000))
        self.editor.doc_canvas.yview_moveto(.5)
        self.root.update_idletasks()
        before_right = self.editor.doc_canvas.yview()
        before_left = self.editor.pdf_canvas.yview()
        self.assertIsNone(self.editor._editor_cursor_y(ensure_visible=False))
        self.editor._position_cursor()
        self.assertEqual(self.editor.doc_canvas.yview(), before_right)
        self.assertEqual(self.editor.pdf_canvas.yview(), before_left)

    def test_new_cursor_request_brings_unmapped_right_paragraph_back_and_aligns_pdf(self):
        self.ready()
        self.show_for_positioning()
        bounds = self.editor.doc_canvas.bbox("all")
        self.editor.doc_canvas.configure(scrollregion=(0, 0, bounds[2], 2000))
        self.editor.doc_canvas.yview_moveto(.5)
        self.root.update_idletasks()
        self.assertFalse(self.editor._editors[11].winfo_ismapped())
        point = location()
        point.caret_box = (.4, .6, .45, .62)
        point.boxes = ((.1, .6, .8, .62),)
        self.editor._apply_cursor_location(point)
        self.root.update_idletasks()
        self.assertTrue(self.editor._editors[11].winfo_ismapped())
        self.assert_same_screen_height()
        self.assertFalse(self.editor.dirty)

    def finish_layout_and_position(self):
        self.root.update_idletasks()
        if self.editor._layout_job is not None:
            self.editor.after_cancel(self.editor._layout_job)
        self.editor._layout_paragraphs()
        self.root.update_idletasks()
        if self.editor._position_job is not None:
            self.editor.after_cancel(self.editor._position_job)
        self.editor._position_cursor()
        self.root.update_idletasks()

    def show_long_paragraph_caret(self):
        self.ready()
        self.editor.deiconify()
        self.root.update()
        text = "天地玄黃宇宙洪荒日月盈昃辰宿列張寒來暑往秋收冬藏" * 20
        widget = self.editor._editors[11]
        widget.delete("1.0", "end")
        widget.insert("1.0", text)
        widget.edit_modified(False)
        self.editor._baseline[11] = text
        self.editor._paragraphs[11].original_text = text
        self.editor._paragraphs[11].corrected_text = text
        self.editor.select_paragraph(11)
        widget.mark_set("insert", "end-1c")
        self.editor._cancel_cursor_request()
        self.mapping.page_font_width.return_value = .02
        self.editor._display_pdf(Image.new("RGB", (600, 1200), "white"))
        self.finish_layout_and_position()
        point = location()
        point.caret_box = (.4, .6, .45, .62)
        point.boxes = ((.1, .6, .8, .62),)
        self.editor._apply_cursor_location(point)
        self.finish_layout_and_position()
        self.assert_same_screen_height()
        return text, widget

    def test_font_reflow_keeps_long_paragraph_caret_visible_and_pdf_at_same_height(self):
        text, widget = self.show_long_paragraph_caret()
        old_height = widget.winfo_height()
        self.mapping.page_font_width.return_value = .065
        self.editor._sync_text_size()
        self.finish_layout_and_position()
        self.assertGreater(widget.winfo_height(), old_height * 2)
        self.assertIsNotNone(self.editor._editor_cursor_y(ensure_visible=False))
        self.assert_same_screen_height()
        self.assertEqual(widget.get("1.0", "insert"), text)
        self.assertFalse(self.editor.dirty)
        self.assertEqual(self.session.calls, [])

    def test_pane_width_reflow_keeps_long_paragraph_caret_visible_and_aligned(self):
        text, widget = self.show_long_paragraph_caret()
        old_height = widget.winfo_height()
        self.editor.panes.sashpos(0, self.editor.panes.winfo_width() - 310)
        self.root.update_idletasks()
        self.finish_layout_and_position()
        self.assertGreater(widget.winfo_height(), old_height)
        self.assertIsNotNone(self.editor._editor_cursor_y(ensure_visible=False))
        self.assert_same_screen_height()
        self.assertEqual(widget.get("1.0", "insert"), text)
        self.assertFalse(self.editor.dirty)

    def test_equal_panes_are_not_pushed_apart_by_text_font_requested_width(self):
        self.ready()
        self.editor.deiconify()
        self.root.update()
        if self.editor._initial_job is not None:
            self.editor.after_cancel(self.editor._initial_job)
        self.editor._initial_split()
        self.root.update_idletasks()
        left = self.editor.pdf_panel.winfo_width()
        right = self.editor.doc_panel.winfo_width()
        self.assertLessEqual(abs(left - right), 4)
        self.mapping.page_font_width.return_value = .08
        self.editor._display_pdf(Image.new("RGB", (600, 1200), "white"))
        self.finish_layout_and_position()
        self.assertEqual(self.editor.pdf_panel.winfo_width(), left)
        self.assertEqual(self.editor.doc_panel.winfo_width(), right)

    def test_initial_split_balances_actual_panes_across_theme_separator_widths(self):
        style = ttk.Style(self.root)
        previous = style.theme_use()
        self.addCleanup(lambda: style.theme_use(previous))
        self.editor.deiconify()
        self.editor.geometry("1200x900")
        self.root.update()
        if self.editor._initial_job is not None:
            self.editor.after_cancel(self.editor._initial_job)
        for theme in style.theme_names():
            for width in (1200, 1201):
                with self.subTest(theme=theme, width=width):
                    style.theme_use(theme)
                    self.editor.geometry(f"{width}x900")
                    self.root.update_idletasks()
                    # Start off-center so the test requires the initial callback
                    # to run; it cannot pass on ttk's automatic weight allocation.
                    self.editor.panes.sashpos(0, self.editor.panes.winfo_width() // 3)
                    self.root.update_idletasks()
                    self.assertGreater(self.editor.doc_panel.winfo_width() - self.editor.pdf_panel.winfo_width(), 100)
                    self.editor._initial_split()
                    self.root.update_idletasks()
                    left = self.editor.pdf_panel.winfo_width()
                    right = self.editor.doc_panel.winfo_width()
                    self.assertLessEqual(abs(left - right), 1)
                    self.assertIsNone(self.editor._initial_job)

    def test_font_reflow_preserves_user_split_after_initial_callback(self):
        self.ready()
        self.editor.deiconify()
        self.root.update()
        if self.editor._initial_job is not None:
            self.editor.after_cancel(self.editor._initial_job)
        self.editor._initial_split()
        self.root.update_idletasks()
        self.editor.panes.sashpos(0, self.editor.panes.winfo_width() // 3)
        self.root.update_idletasks()
        left = self.editor.pdf_panel.winfo_width()
        right = self.editor.doc_panel.winfo_width()
        self.assertGreater(right - left, 100)
        self.mapping.page_font_width.return_value = .08
        self.editor._display_pdf(Image.new("RGB", (600, 1200), "white"))
        self.finish_layout_and_position()
        self.assertIsNone(self.editor._initial_job)
        self.assertEqual(self.editor.pdf_panel.winfo_width(), left)
        self.assertEqual(self.editor.doc_panel.winfo_width(), right)


if __name__ == "__main__":
    unittest.main()
