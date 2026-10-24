"""결과 위치와 문서 열기를 가짜 파일·OS 열기 함수로 검증한다."""
import contextlib
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from test_ops import gui_module


class Var:
    def __init__(self, value=""):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


class ResultFilesGui(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.gui = self.stack.enter_context(gui_module())
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.source = self.root / "캡처 [1].png"
        self.source.write_bytes(b"synthetic source")
        self.work = self.gui.work_dir_for(str(self.source))
        self.open_path = self.stack.enter_context(mock.patch.object(self.gui, "open_path"))
        self.info = self.stack.enter_context(mock.patch.object(self.gui.messagebox, "showinfo"))
        self.error = self.stack.enter_context(mock.patch.object(self.gui.messagebox, "showerror"))

    def app(self):
        app = SimpleNamespace(v_pdf=Var(str(self.source)), v_output_dir=Var())
        for name in ("pdf", "work", "_need_pdf", "_sync_output_location"):
            setattr(app, name, getattr(self.gui.App, name).__get__(app))
        return app

    def result(self, stem, *, work=None, tick=1):
        path = (work or self.work) / "out" / (stem + ".hwpx")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"synthetic result")
        timestamp = 1_700_000_000_000_000_000 + tick * 1_000_000_000
        os.utime(path, ns=(timestamp, timestamp))
        return path

    def test_source_selection_ignores_newer_interim_foreign_and_corrected_files(self):
        expected = self.result(self.source.stem)
        for name in (self.source.stem + "_진행중", self.source.stem + "_교정",
                     "다른 캡처", "캡처 1_p001-003", self.source.stem + "_p001-003_교정"):
            self.result(name, tick=10)
        # A directory with a plausible HWPX name is not a result document.
        (self.work / "out" / (self.source.stem + "_p001-005.hwpx")).mkdir()
        self.assertEqual(self.gui.latest_result_hwpx(str(self.source), self.work), expected)

    def test_latest_partial_range_wins_until_full_document_is_rebuilt(self):
        self.result(self.source.stem, tick=1)
        self.result(self.source.stem + "_p001-003", tick=2)
        latest_partial = self.result(self.source.stem + "_p007-010", tick=3)
        self.assertEqual(self.gui.latest_result_hwpx(str(self.source), self.work), latest_partial)
        rebuilt = self.result(self.source.stem, tick=4)
        self.assertEqual(self.gui.latest_result_hwpx(str(self.source), self.work), rebuilt)

    def test_search_for_missing_results_does_not_create_work_directory(self):
        self.assertIsNone(self.gui.latest_result_hwpx(str(self.source), self.work))
        self.assertFalse(self.work.exists())

    def test_one_click_opens_selected_source_result_without_a_dialog(self):
        expected = self.result(self.source.stem)
        self.result("다른 캡처", tick=10)
        self.gui.App.open_hwpx(self.app())
        self.open_path.assert_called_once_with(expected)
        self.info.assert_not_called()
        self.error.assert_not_called()

    def test_switching_capture_updates_output_location_and_opens_new_capture(self):
        app = self.app()
        self.result(self.source.stem, tick=10)
        app._sync_output_location()
        first_location = app.v_output_dir.get()
        self.assertEqual(Path(first_location), (self.work / "out").resolve())

        next_source = self.root / "다음 캡처.png"
        next_source.write_bytes(b"another synthetic source")
        next_work = self.gui.work_dir_for(str(next_source))
        expected = self.result(next_source.stem, work=next_work)
        app.v_pdf.set(str(next_source))
        app._sync_output_location()
        self.assertEqual(Path(app.v_output_dir.get()), (next_work / "out").resolve())
        self.assertNotEqual(first_location, app.v_output_dir.get())
        self.gui.App.open_hwpx(app)
        self.open_path.assert_called_once_with(expected)

    def test_missing_result_reports_intended_location_without_opening_or_creating_it(self):
        app = self.app()
        app._sync_output_location()
        self.assertEqual(Path(app.v_output_dir.get()), (self.work / "out").resolve())
        self.gui.App.open_hwpx(app)
        self.open_path.assert_not_called()
        self.assertIn(str(self.work / "out"), self.info.call_args.args[1])
        self.assertFalse(self.work.exists())

    def test_interim_only_is_not_opened_as_completed_result(self):
        self.result(self.source.stem + "_진행중")
        self.gui.App.open_hwpx(self.app())
        self.open_path.assert_not_called()
        self.info.assert_called_once()

    def test_result_editor_pins_active_input_output_and_work_for_pdf_image_and_folder(self):
        for name, directory in (("원본.pdf", False), ("캡처.png", False), ("이미지 모음", True)):
            with self.subTest(source=name):
                source = self.root / name
                source.mkdir() if directory else source.write_bytes(b"synthetic source")
                work = self.gui.work_dir_for(str(source))
                result = self.result(source.stem + "_p002-005", work=work, tick=4)
                self.result(source.stem + "_교정", work=work, tick=9)
                app = self.app()
                app.v_pdf.set(str(source))
                app.v_status = Var()
                window = SimpleNamespace()
                with mock.patch("yeone_ocr.collate.section_editor.BookmarkSectionEditor", return_value=window) as constructor:
                    self.gui.App.open_result_editor(app)
                arguments = constructor.call_args.kwargs
                self.assertEqual(arguments["pdf_path"], str(source.resolve()))
                self.assertEqual(arguments["hwpx_path"], result.resolve())
                self.assertEqual(arguments["work_dir"], work.resolve())
                self.assertTrue(arguments["generated"])
                self.assertTrue(arguments["can_write"]())
                app.running = True
                self.assertFalse(arguments["can_write"]())
                app.v_pdf.set(str(self.source))
                self.assertEqual(window._generated_key[0], os.path.normcase(str(source.resolve())))
                self.assertEqual(app._collate_edit_windows, [window])

    def test_result_editor_reuses_exact_result_without_discarding_unsaved_edits(self):
        result = self.result(self.source.stem)
        app = self.app()
        key = tuple(os.path.normcase(str(path.resolve())) for path in (self.source, result, self.work))
        window = SimpleNamespace(_generated_key=key, winfo_exists=lambda: True, dirty=True, busy=False,
                                 reload=mock.Mock(), deiconify=mock.Mock(), lift=mock.Mock(), focus_set=mock.Mock())
        app._collate_edit_windows = [window]
        with mock.patch("yeone_ocr.collate.section_editor.BookmarkSectionEditor") as constructor:
            self.gui.App.open_result_editor(app)
            window.reload.assert_not_called()
            window.dirty = False
            self.gui.App.open_result_editor(app)
        constructor.assert_not_called()
        window.reload.assert_called_once()
        self.assertEqual(window.lift.call_count, 2)

    def test_result_editor_missing_or_running_never_opens_or_creates_output(self):
        app = self.app()
        with mock.patch("yeone_ocr.collate.section_editor.BookmarkSectionEditor") as constructor:
            self.gui.App.open_result_editor(app)
            self.assertIn(str(self.work / "out"), self.info.call_args.args[1])
            self.assertFalse(self.work.exists())
            self.result(self.source.stem)
            app.running = True
            self.gui.App.open_result_editor(app)
        constructor.assert_not_called()
        self.assertIn("OCR 작업이 끝난 뒤", self.info.call_args.args[1])

    def test_result_editor_drafts_block_regeneration_before_worker_or_cost_dialog(self):
        app = self.app()
        app.running = False
        app._prepare_env = mock.Mock(return_value=True)
        window = SimpleNamespace(_generated_key=(os.path.normcase(str(self.source.resolve())), "result", "work"),
                                 winfo_exists=lambda: True, dirty=True, busy=False, deiconify=mock.Mock(), lift=mock.Mock())
        app._collate_edit_windows = [window]
        with mock.patch.object(self.gui.threading, "Thread") as worker:
            self.gui.App.start(app, ["run", str(self.source)], "인식")
            self.gui.App.start(app, ["build", str(self.source)], "문서 만들기")
            self.gui.App.do_run(app)
        worker.assert_not_called()
        app._prepare_env.assert_not_called()
        self.assertEqual(window.lift.call_count, 3)
        self.assertTrue(self.gui.App._result_editors_ready(app, self.root / "other.pdf"))
        window.dirty = False
        self.assertTrue(self.gui.App._result_editors_ready(app, self.source))

    def test_result_editor_participates_in_main_window_close_draft_guard(self):
        app = self.app()
        app.v_status = Var()
        window = SimpleNamespace(_generated_key=(str(self.source),), winfo_exists=lambda: True,
                                 close=mock.Mock(return_value=False), lift=mock.Mock())
        app._collate_edit_windows = [window]
        self.gui.App.on_close(app)
        window.close.assert_called_once()
        window.lift.assert_called_once()
        self.assertIn("저장 상태", app.v_status.get())

    def test_save_folder_button_opens_out_instead_of_work_root(self):
        expected = self.result(self.source.stem).parent
        self.gui.App.open_output_folder(self.app())
        self.open_path.assert_called_once_with(expected)
        self.info.assert_not_called()

    def test_missing_save_folder_is_reported_without_creating_it(self):
        self.gui.App.open_output_folder(self.app())
        self.open_path.assert_not_called()
        self.assertIn(str(self.work / "out"), self.info.call_args.args[1])
        self.assertFalse(self.work.exists())

    def test_shell_open_failures_are_reported_for_document_and_folder(self):
        result = self.result(self.source.stem)
        self.open_path.side_effect = OSError("synthetic shell error")
        for method, expected in (("open_hwpx", result), ("open_output_folder", result.parent)):
            with self.subTest(method=method):
                self.open_path.reset_mock()
                self.error.reset_mock()
                getattr(self.gui.App, method)(self.app())
                self.open_path.assert_called_once_with(expected)
                self.error.assert_called_once()
                self.assertIn("synthetic shell error", self.error.call_args.args[1])
                self.assertTrue(result.is_file())

    def test_collated_copy_opens_a_read_only_copy_while_collating(self):
        """[교정본 열기]는 실행 중에도 눌리지만, 그때는 [▶ HWPX 전체 교정]이 구획마다 다시 쓰는 <이름>_교정.hwpx 대신 읽기용 사본을 연다
        (한컴이 교정본을 쥐면 다음 구획 저장이 막혀 전체 교정이 멈추고, 그 구획의 교정·모델 호출이 헛된다)."""
        original = self.root / "본초학 양식.hwpx"; original.write_bytes(b"original")
        collated = self.root / "본초학 양식_교정.hwpx"
        logs = []
        app = SimpleNamespace(v_hwpx=Var(str(original)), running=False, _append=logs.append)
        self.gui.App.open_collated(app)
        self.open_path.assert_not_called(); self.info.assert_called_once()                         # 아직 없음
        collated.write_bytes(b"collated")
        self.gui.App.open_collated(app)
        self.open_path.assert_called_once_with(collated)
        self.open_path.reset_mock(); app.running = True
        self.gui.App.open_collated(app)
        copy = self.open_path.call_args.args[0]
        self.assertNotEqual(copy, collated); self.assertIn("_보기_", copy.name); self.assertEqual(copy.read_bytes(), b"collated")
        self.assertTrue(logs and "읽기용 사본" in logs[-1])
        copy.unlink()
        self.open_path.side_effect = OSError("synthetic shell error"); app.running = False
        self.gui.App.open_collated(app)
        self.error.assert_called_once(); self.assertIn("synthetic shell error", self.error.call_args.args[1])

    def test_clearing_source_does_not_keep_old_location_or_open_old_result(self):
        app = self.app()
        self.result(self.source.stem)
        app._sync_output_location()
        old_location = app.v_output_dir.get()
        app.v_pdf.set("")
        app._sync_output_location()
        self.assertNotEqual(app.v_output_dir.get(), old_location)
        self.assertNotIn(str(self.work), app.v_output_dir.get())
        self.gui.App.open_hwpx(app)
        self.gui.App.open_output_folder(app)
        self.open_path.assert_not_called()


if __name__ == "__main__":
    unittest.main()
