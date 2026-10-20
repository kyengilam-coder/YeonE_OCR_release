"""Bookmark results stay attached to their selected source and section; no OCR."""
import contextlib
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import xml.etree.ElementTree as ET
import zipfile

from yeone_ocr import gui
from yeone_ocr.collate.navigation import create_section_view_copy, index_signature, HA


class Var:
    def __init__(self, value=""):
        self.value = value
    def get(self):
        return self.value
    def set(self, value):
        self.value = value


class SectionViewCopy(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.source = self.root / "교정본.hwpx"

    def package(self, *, multiple=False, settings=True):
        # Repeated p/@id is normal in actual Hancom files. The caret uses ordinal.
        section = ('<hs:sec xmlns:hs="http://www.hancom.co.kr/hwpml/2011/section" '
                   'xmlns:hp="http://www.hancom.co.kr/hwpml/2011/paragraph">'
                   '<hp:p id="2147483648"><hp:run><hp:t>좌금환</hp:t></hp:run></hp:p>'
                   '<hp:p id="2147483648"><hp:run><hp:bookmark name="瀉白散"/><hp:t>瀉白散</hp:t></hp:run></hp:p>'
                   '</hs:sec>')
        with zipfile.ZipFile(self.source, "w") as out:
            out.writestr("mimetype", "application/hwp+zip")
            out.writestr("Contents/section0.xml", section)
            out.writestr("Contents/header.xml", "<header/>")
            out.writestr("BinData/image1.png", b"unchanged image bytes")
            if multiple:
                out.writestr("Contents/section1.xml", section)
            if settings:
                out.writestr("settings.xml", '<ha:HWPApplicationSetting xmlns:ha="http://www.hancom.co.kr/hwpml/2011/app">'
                             '<ha:CaretPosition listIDRef="0" paraIDRef="0" pos="4"/><other keep="yes"/>'
                             '</ha:HWPApplicationSetting>')

    def test_only_view_copy_caret_changes_and_uses_ordinal(self):
        self.package()
        original = self.source.read_bytes()
        copy = create_section_view_copy(self.source, 1, "瀉白散", output_dir=self.root / "views")
        self.assertEqual(self.source.read_bytes(), original)
        self.assertNotEqual(copy, self.source)
        with zipfile.ZipFile(self.source) as before, zipfile.ZipFile(copy) as after:
            changed = [name for name in before.namelist() if before.read(name) != after.read(name)]
            self.assertEqual(changed, ["settings.xml"])
            caret = ET.fromstring(after.read("settings.xml")).find(HA + "CaretPosition")
            self.assertEqual(caret.attrib, {"listIDRef": "0", "paraIDRef": "1", "pos": "0"})
        again = create_section_view_copy(self.source, 1, "瀉白散", output_dir=self.root / "views")
        self.assertNotEqual(copy, again)

    def test_wrong_section_and_unsupported_layout_never_create_a_view(self):
        for kwargs, para, name in (({}, 0, "瀉白散"), ({}, 5, "瀉白散"),
                                   ({"multiple": True}, 1, "瀉白散"), ({"settings": False}, 1, "瀉白散")):
            with self.subTest(kwargs=kwargs, para=para):
                self.package(**kwargs)
                original = self.source.read_bytes()
                with self.assertRaises(ValueError):
                    create_section_view_copy(self.source, para, name, output_dir=self.root / "views")
                self.assertEqual(self.source.read_bytes(), original)
                self.assertFalse((self.root / "views").exists())

    def test_index_identity_includes_external_edits_and_settings(self):
        self.package()
        pdf = self.root / "book.pdf"
        pdf.write_bytes(b"pdf")
        initial = index_signature(str(pdf), str(self.source), "auto", ["제목"])
        self.assertNotEqual(initial, index_signature(str(pdf), str(self.source), "pdf", ["제목"]))
        self.assertNotEqual(initial, index_signature(str(pdf), str(self.source), "auto", ["장", "절"]))
        pdf.write_bytes(b"changed pdf")
        self.assertNotEqual(initial, index_signature(str(pdf), str(self.source), "auto", ["제목"]))


class BookmarkGui(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.pdf = self.root / "book.pdf"; self.pdf.write_bytes(b"pdf")
        self.hwpx = self.root / "book.hwpx"; self.hwpx.write_bytes(b"hwpx")
        self.row = {"no": 93, "name": "瀉白散", "read": "사백산", "para": 5271, "end": 5323,
                    "lv": 3, "pdf": "사백산", "page": 623, "pages": (623, 627), "how": "정확"}
        self.selected = ["93"]
        self.tree = SimpleNamespace(selection=lambda: tuple(self.selected), exists=lambda value: value == "93",
                                    selection_set=lambda value: self.selected.__setitem__(slice(None), [value]), see=Mock())
        self.app = SimpleNamespace(ctable=self.tree, collate_rows=[self.row], v_hwpx=Var(str(self.hwpx)),
                                   pdf2=lambda: str(self.pdf), work2=lambda: self.root / "work",
                                   anchor_source=lambda: "auto", heading_styles_list=lambda: ["제목"],
                                   v_cstat=Var(), refresh_collate_table=Mock(), _collate_generation=4,
                                   _collate_index_signature=None, running=False)
        self.opened = self.stack.enter_context(patch.object(gui, "open_path"))
        self.info = self.stack.enter_context(patch.object(gui.messagebox, "showinfo"))

    def test_report_opens_selected_identity_and_labels_legacy_history(self):
        report = self.root / "old_selected.html"; report.write_text("selected", encoding="utf-8")
        report.with_suffix(".json").write_text(json.dumps({"at": "2026-10-20 09:00:00"}), encoding="utf-8")
        with patch("yeone_ocr.collate.results.locate_section_report", return_value=report) as lookup, \
             patch.object(gui.App, "_latest", side_effect=AssertionError("must not select by folder mtime")):
            gui.App.open_collate_report(self.app)
        lookup.assert_called_once_with(self.root / "work", str(self.pdf), str(self.hwpx), "瀉白散", 5271, 5323)
        self.opened.assert_called_once_with(report)
        self.assertIn("기존 기록: 瀉白散", self.app.v_cstat.get())
        self.assertIn("2026-10-20", self.app.v_cstat.get())

    def test_double_click_opens_editor_without_executing(self):
        with patch.object(gui.App, "open_collate_editor") as editor, \
             patch.object(gui.App, "do_collate", side_effect=AssertionError("view must not execute OCR")):
            gui.App.open_collate_section(self.app)
        editor.assert_called_once_with(self.app)
        self.opened.assert_not_called()

    def test_editor_receives_selected_scope_and_save_gate(self):
        with patch.object(gui.App, "_collate_editor_pages", return_value=(623, 627)), \
             patch("yeone_ocr.collate.section_editor.BookmarkSectionEditor") as constructor:
            gui.App.open_collate_editor(self.app)
        arguments = constructor.call_args.kwargs
        self.assertEqual(arguments["section_name"], "瀉白散")
        self.assertEqual((arguments["section_start"], arguments["section_end"]), (5271, 5323))
        self.assertEqual(arguments["pdf_pages"], (623, 627))
        self.assertEqual(arguments["heading_styles"], ["제목"])
        self.assertTrue(arguments["can_write"]())
        self.app.running = True
        self.assertFalse(arguments["can_write"]())
        self.assertEqual(len(self.app._collate_edit_windows), 1)

    def test_dirty_editor_blocks_ocr_for_its_document(self):
        window = SimpleNamespace(
            winfo_exists=lambda: True, dirty=True, busy=False, deiconify=Mock(), lift=Mock(),
            _collate_key=("", os.path.normcase(str(self.hwpx.resolve())), 5271, 5323))
        self.app._collate_edit_windows = [window]
        self.assertFalse(gui.App._collate_editors_ready(self.app))
        window.lift.assert_called_once()
        window.dirty = False
        self.assertTrue(gui.App._collate_editors_ready(self.app))

    def test_editing_cannot_open_during_ocr(self):
        self.app.running = True
        with patch("yeone_ocr.collate.section_editor.BookmarkSectionEditor") as constructor:
            gui.App.open_collate_editor(self.app)
        constructor.assert_not_called()

    def test_memo_cleanup_blocks_active_work_and_unsaved_editor(self):
        window = SimpleNamespace(
            winfo_exists=lambda: True, dirty=True, busy=False, deiconify=Mock(), lift=Mock(), reload=Mock(),
            _collate_key=("", os.path.normcase(str(self.hwpx.resolve())), 5271, 5323))
        self.app._collate_edit_windows = [window]
        with patch("yeone_ocr.collate.memo_cleanup.clean_corrected_memos") as cleanup:
            self.app.running = True
            gui.App.clean_collate_memos(self.app)
            self.app.running = False
            gui.App.clean_collate_memos(self.app)
        cleanup.assert_not_called()
        window.lift.assert_called_once()
        window.reload.assert_not_called()
        self.assertIn("저장하지 않은 수정", self.info.call_args.args[1])

    def test_memo_cleanup_uses_existing_corrected_file_and_reloads_clean_editor(self):
        window = SimpleNamespace(
            winfo_exists=lambda: True, dirty=False, busy=False, reload=Mock(return_value=True),
            _collate_key=("", os.path.normcase(str(self.hwpx.resolve())), 5271, 5323))
        self.app._collate_edit_windows = [window]
        self.app._append = Mock()
        result = SimpleNamespace(removed=4, skipped=1, corrected_path=self.root / "book_교정.hwpx",
                                 backup_path=self.root / "book_교정.메모정리.bak.hwpx")
        with patch("yeone_ocr.collate.memo_cleanup.clean_corrected_memos", return_value=result) as cleanup, \
             patch.object(gui.App, "do_collate", side_effect=AssertionError("cleanup must not run OCR")):
            gui.App.clean_collate_memos(self.app)
        cleanup.assert_called_once_with(str(self.hwpx))
        window.reload.assert_called_once()
        self.assertIn("4개 정리", self.app.v_cstat.get())
        self.assertIn("1개는 남겼습니다", self.info.call_args.args[1])
        self.assertIn(str(result.backup_path), self.info.call_args.args[1])
        self.assertEqual(self.app.collate_rows, [self.row])  # Body and source index do not change.

    def test_editor_without_confirmed_pdf_range_is_not_opened(self):
        with patch.object(gui.App, "_collate_editor_pages", return_value=None), \
             patch("yeone_ocr.collate.section_editor.BookmarkSectionEditor") as constructor:
            gui.App.open_collate_editor(self.app)
        constructor.assert_not_called()

    def test_missing_result_does_not_open_an_unrelated_report(self):
        with patch("yeone_ocr.collate.results.locate_section_report", return_value=None):
            gui.App.open_collate_report(self.app)
        self.opened.assert_not_called()
        self.assertIn("瀉白散", self.info.call_args.args[1])

    def test_invalidation_removes_rows_and_discards_late_worker_result(self):
        gui.App._invalidate_collate_index(self.app)
        self.assertEqual(self.app.collate_rows, [])
        self.app.refresh_collate_table.reset_mock()
        gui.App._accept_collate_index(self.app, ("sections", [self.row], "old", None, 4))
        self.assertEqual(self.app.collate_rows, [])
        self.app.refresh_collate_table.assert_not_called()

    def test_external_source_change_blocks_action_and_invalidates_index(self):
        self.app._collate_index_signature = gui.App._collate_input_signature(self.app)
        self.pdf.write_bytes(b"replacement pdf")
        with patch("yeone_ocr.collate.results.locate_section_report") as lookup:
            gui.App.open_collate_report(self.app)
        lookup.assert_not_called()
        self.opened.assert_not_called()
        self.assertEqual(self.app.collate_rows, [])

    def test_execute_passes_section_identity_not_only_display_number(self):
        app = self.app
        app._need_hwpx = lambda: True
        app._prepare_env = lambda: True
        app._selected_anchor = lambda: 93
        app.collate_policy = lambda: "sure"
        app.v_cforce = Var(False)
        app._read_opts = lambda **kw: []
        app.collate_memo_mode = lambda: "notice"
        app._glyph_opts = lambda: []
        app.cfg = {}; app.v_pdf2 = Var(str(self.pdf))
        app.nb = SimpleNamespace(select=Mock()); app.start = Mock()
        with patch.object(gui, "write_config"):
            gui.App.do_collate(app, True)
        argv = app.start.call_args.args[0]
        self.assertEqual(argv[argv.index("--anchor-index") + 1], "93")
        self.assertEqual(argv[argv.index("--anchor-para") + 1], "5271")
        self.assertEqual(argv[argv.index("--anchor-name") + 1], "瀉白散")
        self.assertIn("--apply", argv)
        self.assertEqual(app._collate_run_selection["row"], self.row)

    def test_completion_restores_exact_finished_section_then_result_uses_it(self):
        self.app._collate_run_selection = {"signature": gui.App._collate_input_signature(self.app), "row": dict(self.row)}
        self.selected[:] = []
        gui.App._complete_collate_selection(self.app)
        self.assertEqual(self.selected, ["93"])
        self.assertIn("완료: 瀉白散", self.app.v_cstat.get())
        report = self.root / "selected.html"
        with patch("yeone_ocr.collate.results.locate_section_report", return_value=report):
            gui.App.open_collate_report(self.app)
        self.opened.assert_called_once_with(report)

    def test_view_buttons_stay_available_during_run_but_need_selection(self):
        self.app._csec_btns = [Mock(), Mock()]
        self.app._cview_btns = [Mock(), Mock()]
        self.app._cedit_btns = [Mock()]
        self.app.running = True
        gui.App._sync_collate_pick(self.app)
        for button in self.app._csec_btns:
            button.configure.assert_called_with(state="disabled")
        for button in self.app._cview_btns:
            button.configure.assert_called_with(state="normal")
        self.app._cedit_btns[0].configure.assert_called_with(state="disabled")
        self.selected[:] = []
        gui.App._sync_collate_pick(self.app)
        for button in self.app._cview_btns:
            button.configure.assert_called_with(state="disabled")


if __name__ == "__main__":
    unittest.main()
