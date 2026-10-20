"""설치판의 출력 경로·실행 분기·업데이트 경계를 네트워크/사용자 설정 없이 검사한다."""
import contextlib
import ctypes
import io
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from yeone_ocr import gui, launcher, runtime, state, updater, windows_app


class FrozenRuntime(unittest.TestCase):
    def test_windows_redirected_cli_logs_preserve_unicode_in_utf8(self):
        text = "판독 — 樂𠀀漢\U000e0100"
        output = io.BytesIO()
        errors = io.BytesIO()
        stdout = io.TextIOWrapper(output, encoding="cp949")
        stderr = io.TextIOWrapper(errors, encoding="cp949")
        stdin = io.StringIO("unchanged input")
        try:
            with mock.patch.object(sys, "platform", "win32"), \
                 mock.patch.object(sys, "stdout", stdout), \
                 mock.patch.object(sys, "stderr", stderr), \
                 mock.patch.object(sys, "stdin", stdin):
                runtime.ensure_standard_streams()
                print(text, flush=True)
                print(text, file=sys.stderr, flush=True)
                self.assertIs(sys.stdin, stdin)
            self.assertEqual(output.getvalue().decode("utf-8").strip(), text)
            self.assertEqual(errors.getvalue().decode("utf-8").strip(), text)
        finally:
            stdout.close()
            stderr.close()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.install = self.root / "installed"
        self.install.mkdir()
        self.exe = self.install / "YeonE_OCR.exe"
        self.exe.write_bytes(b"test")

    def frozen(self):
        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch.object(sys, "frozen", True, create=True))
        stack.enter_context(mock.patch.object(sys, "platform", "win32"))
        stack.enter_context(mock.patch.object(sys, "executable", str(self.exe)))
        stack.enter_context(mock.patch.object(sys, "_MEIPASS", str(self.install / "_internal"), create=True))
        stack.enter_context(mock.patch.object(runtime, "user_data_dir", return_value=self.root / "user-data"))
        return stack

    def test_source_paths_remain_compatible(self):
        with mock.patch.object(sys, "frozen", False, create=True):
            self.assertEqual(gui.work_dir_for("book.pdf"), Path("book_YeonE_OCR"))
            self.assertEqual(state.default_work_dir("book.pdf"), Path("work/book"))

    def test_installed_defaults_use_user_folder_and_do_not_collide(self):
        source = self.install / "한문 책.pdf"
        with self.frozen():
            gui_work = gui.work_dir_for(str(source))
            cli_work = state.default_work_dir(str(source))
            for output in (gui_work, cli_work):
                self.assertTrue(output.is_relative_to(self.root / "user-data"))
                self.assertFalse(output.is_relative_to(self.install))
            self.assertEqual(gui_work, gui.work_dir_for(str(source)))
            self.assertEqual(cli_work, state.default_work_dir(str(source)))
            self.assertNotEqual(cli_work, state.default_work_dir(str(self.root / "other" / source.name)))
            explicit = self.root / "existing-result"
            self.assertEqual(state.Work(str(source), str(explicit)).dir, explicit)

    def test_frozen_gui_retains_existing_adjacent_work_for_normal_documents(self):
        source = self.root / "books" / "book.pdf"
        with self.frozen():
            self.assertEqual(gui.work_dir_for(str(source)), source.with_name("book_YeonE_OCR"))
            # A sibling with a shared prefix is not inside the install directory.
            adjacent = self.root / "installed-other" / "book.pdf"
            self.assertEqual(gui.work_dir_for(str(adjacent)), adjacent.with_name("book_YeonE_OCR"))

    def test_bundled_images_and_corrected_hwpx_use_safe_output(self):
        from yeone_ocr.collate.run import corrected_path
        image = self.install / "page.png"
        image.write_bytes(b"image contents only used for a fingerprint")
        with self.frozen():
            self.assertTrue(gui.App._bundle_path([image]).is_relative_to(self.root / "user-data"))
            corrected = Path(corrected_path(str(self.install / "book.hwpx")))
            self.assertTrue(corrected.is_relative_to(self.root / "user-data"))
            self.assertEqual(Path(corrected_path(str(corrected))), corrected)

    def test_package_check_never_executes_frozen_executable_as_python(self):
        app = SimpleNamespace(running=False, check_packages=mock.Mock())
        with self.frozen(), mock.patch.object(gui.subprocess, "Popen") as popen:
            gui.App.install_packages(app)
        app.check_packages.assert_called_once_with()
        popen.assert_not_called()

    def test_missing_bundled_component_requests_reinstallation_without_pip(self):
        app = SimpleNamespace(running=False, v_pkg=mock.Mock(), _append=mock.Mock())
        with self.frozen(), mock.patch.object(gui.messagebox, "showerror") as error, \
             mock.patch.object(gui.messagebox, "askyesno") as ask:
            gui.App._pkg_result(app, ["opencc-python-reimplemented"])
        self.assertIn("다시 설치", error.call_args.args[1])
        ask.assert_not_called()

    def test_installed_updater_rejects_exe_replacement_before_reading_download(self):
        (self.install / "_internal").mkdir()
        with self.frozen(), mock.patch.object(updater.subprocess, "Popen") as popen:
            self.assertTrue(runtime.is_installed_bundle())
            self.assertFalse(runtime.can_replace_executable())
            with self.assertRaisesRegex(updater.UpdateError, "Setup"):
                updater.install_update(self.root / "not-downloaded.exe", self.exe, os.getpid())
        popen.assert_not_called()
        self.assertEqual(self.exe.read_bytes(), b"test")

    def test_installed_gui_update_guides_to_setup_without_download(self):
        (self.install / "_internal").mkdir()
        app = SimpleNamespace(running=False, _update_installing=False)
        info = SimpleNamespace(url="https://github.com/kyengilam-coder/YeonE_OCR_release/releases/tag/v9.0.0")
        with self.frozen(), mock.patch.object(gui.messagebox, "showinfo") as show, \
             mock.patch.object(updater, "download_update") as download:
            gui.App._start_update(app, info)
        self.assertIn("Setup", show.call_args.args[1])
        download.assert_not_called()
        with self.frozen():
            (self.install / "_internal").rmdir()
            self.assertTrue(runtime.can_replace_executable())

    def test_external_dll_directory_is_restored_even_if_open_fails(self):
        kernel = mock.Mock()
        def directory(length, buf):
            if buf is not None:
                buf.value = "C:\\bundled"
            return len("C:\\bundled")
        kernel.GetDllDirectoryW.side_effect = directory
        with self.frozen(), mock.patch.object(ctypes, "windll", SimpleNamespace(kernel32=kernel), create=True):
            with self.assertRaisesRegex(OSError, "cannot open"):
                with runtime.external_dll_search():
                    raise OSError("cannot open")
        self.assertEqual(kernel.SetDllDirectoryW.call_args_list, [mock.call(None), mock.call("C:\\bundled")])

    def test_launcher_dispatches_smoke_and_cli_without_gui(self):
        report = self.root / "smoke.json"
        fake_smoke = SimpleNamespace(run=mock.Mock(return_value=0))
        fake_cli = SimpleNamespace(main=mock.Mock(return_value=3))
        with mock.patch("multiprocessing.freeze_support"), \
             mock.patch.dict(sys.modules, {"yeone_ocr.frozen_smoke": fake_smoke, "yeone_ocr.cli": fake_cli}), \
             mock.patch.object(windows_app, "_normal_gui") as normal:
            self.assertEqual(launcher.main(["--smoke-test", str(report)]), 0)
            self.assertEqual(launcher.main(["--cli", "report", "book.pdf"]), 3)
        fake_smoke.run.assert_called_once_with(report)
        fake_cli.main.assert_called_once_with(["report", "book.pdf"])
        normal.assert_not_called()

    def test_windowless_streams_can_print_and_existing_streams_are_preserved(self):
        captured = io.StringIO()
        with mock.patch.object(sys, "stdin", None), mock.patch.object(sys, "stdout", captured), mock.patch.object(sys, "stderr", None):
            runtime.ensure_standard_streams()
            try:
                self.assertIs(sys.stdout, captured)
                self.assertEqual(sys.stdin.read(), "")
                print("diagnostic", file=sys.stderr, flush=True)
            finally:
                sys.stdin.close()
                sys.stderr.close()

    def test_logs_redact_all_provider_key_formats(self):
        keys = ["AIza" + "a" * 35, "sk-proj-" + "b" * 48, "sk-ant-api03-" + "c" * 48]
        text = windows_app.redact_log(" ".join(keys))
        self.assertTrue(all(key not in text for key in keys))
        self.assertEqual(text.count("[REDACTED]"), 3)

    def test_all_six_engine_worker_and_installer_resources_are_self_contained(self):
        from yeone_ocr.frozen_smoke import comparison_resources_probe
        from ab_ocr import runtime as engine_runtime
        result = comparison_resources_probe()
        self.assertEqual(set(result["workers"]), {"rapidocr", "easyocr", "paddle_server", "glmocr", "surya"})
        self.assertEqual(len(set(result["workers"].values())), 4)
        self.assertEqual(result["workers"]["paddle_server"], "rapidocr_worker.py")
        self.assertFalse(result["external_models_loaded"])
        self.assertEqual(result["network_calls"], 0)
        resources = set(result["application_resources"])
        self.assertIn("worker_sessions.py", resources)
        self.assertEqual(result["gpu_queue_database"], "sqlite3 available")
        for name in ("install_paddle_server.py", "paddle-server-lock.json", "install_glmocr.py",
                     "glmocr-models.lock.json", "install_surya.py", "surya-lock.json", "surya-requirements.txt"):
            self.assertIn(name, resources)
        self.assertTrue({"GLM_OCR.md", "PADDLE_SERVER.md", "SURYA_LOCAL.md", "LOCAL_ENGINE_COMPARISON.md"}
                        <= set(result["documentation"]))
        # A frozen installation must resolve its own workers, never the source
        # checkout or another project's runtime if one bundled file is missing.
        with self.frozen():
            self.assertTrue(engine_runtime.worker_path("glmocr").is_relative_to(self.install))
            with self.assertRaisesRegex(AssertionError, "Missing application-owned worker"):
                comparison_resources_probe()

    def test_release_smoke_preserves_source_codes_and_measures_literal_errors(self):
        from yeone_ocr.frozen_smoke import source_preservation_probe
        result = source_preservation_probe(self.root)
        self.assertEqual(result["source_exact_edits"], 2)
        self.assertTrue(result["evaluation_record_unchanged"])
        self.assertTrue(result["compatibility_and_selectors_preserved"])
        self.assertEqual(result["network_calls"], 0)

    def test_release_smoke_holds_new_unlocated_answers_and_replays_legacy(self):
        from yeone_ocr.frozen_smoke import glyph_location_hold_probe
        result = glyph_location_hold_probe()
        self.assertEqual(result["new_unmeasured_choice"], "held")
        self.assertEqual(result["legacy_decision_replay"], "preserved")
        self.assertEqual(result["mock_answers"], 1)
        self.assertEqual(result["network_calls"], 0)

    def test_release_smoke_uses_unique_sections_and_rejects_earlier_mentions(self):
        from yeone_ocr.frozen_smoke import bookmark_section_probe
        result = bookmark_section_probe(self.root)
        self.assertEqual(result["canonical_sections"], 2)
        self.assertEqual(result["body_heading_pages"], [2, 3])
        self.assertTrue(result["earlier_mention_rejected"])
        self.assertTrue(result["input_unchanged"])
        self.assertEqual(result["network_calls"], 0)

    def test_release_smoke_scans_both_annotation_layouts_without_editing_records(self):
        from yeone_ocr.frozen_smoke import small_annotations_probe
        result = small_annotations_probe(self.root)
        self.assertEqual(result["layouts"], {"vertical": {"candidates": 1, "lines": 2},
                                            "horizontal": {"candidates": 2, "lines": 7}})
        self.assertTrue(result["ui_imported"])
        self.assertTrue(result["original_and_records_unchanged"])
        self.assertEqual(result["automatic_insertions"], 0)
        self.assertEqual(result["network_calls"], 0)

    def test_release_smoke_edits_existing_bookmark_copy_with_backup(self):
        from yeone_ocr.frozen_smoke import bookmark_edit_probe
        result = bookmark_edit_probe(self.root)
        self.assertTrue(result["saved_into_existing_copy"])
        self.assertTrue(result["backup_matches_previous"])
        self.assertTrue(result["source_and_other_section_preserved"])
        self.assertTrue(result["manual_protection"])
        self.assertTrue(result["ui_save_and_reopen"])
        self.assertEqual(result["network_calls"], 0)

    def test_release_smoke_follows_bookmark_caret_and_matches_pdf_text_size(self):
        from yeone_ocr.frozen_smoke import bookmark_cursor_probe
        from yeone_ocr.collate.section_editor import shared_pdf_worker
        with mock.patch("socket.create_connection", side_effect=AssertionError("Unexpected network access")), \
             mock.patch("socket.getaddrinfo", side_effect=AssertionError("Unexpected DNS access")):
            result = bookmark_cursor_probe(self.root)
        self.assertNotEqual(shared_pdf_worker().submit(os.getpid).result(timeout=8), os.getpid())
        self.assertTrue(result["native_pdf_separate_process"])
        self.assertEqual(result["cursor_pdf_page"], 2)
        self.assertTrue(result["line_and_glyph_overlays"])
        self.assertTrue(result["same_paragraph_caret_move"])
        self.assertTrue(result["font_tracks_pdf_zoom"])
        self.assertGreater(result["font_sizes"][1], result["font_sizes"][0])
        self.assertTrue(result["cursor_screen_height_aligned"])
        self.assertLessEqual(result["cursor_height_error_px"], 2)
        self.assertTrue(result["source_pdf_and_hwpx_unchanged"])
        self.assertTrue(result["corrected_file_not_created"])
        self.assertEqual(result["ocr_calls"], 0)
        self.assertEqual(result["network_calls"], 0)

    def test_release_smoke_color_only_and_owned_memo_cleanup(self):
        from yeone_ocr.frozen_smoke import color_only_collation_probe
        result = color_only_collation_probe(self.root)
        self.assertEqual(result["owned_memos_removed"], 2)
        for field in ("color_only_no_memos_or_inline_text", "human_memo_preserved",
                      "text_and_original_preserved", "backup_matches_previous",
                      "repeated_cleanup_no_write"):
            self.assertTrue(result[field], field)
        self.assertEqual(result["ocr_calls"], 0)
        self.assertEqual(result["network_calls"], 0)

    def test_release_smoke_remembers_human_correction_without_cross_document_or_undo_reuse(self):
        from yeone_ocr.frozen_smoke import correction_memory_probe
        with mock.patch("socket.create_connection", side_effect=AssertionError("Unexpected network access")), \
             mock.patch("socket.getaddrinfo", side_effect=AssertionError("Unexpected DNS access")):
            result = correction_memory_probe(self.root)
        self.assertEqual(result["same_pdf_hint_count"], 1)
        for field in ("before_after_journal_saved", "hangul_to_hanja_hint", "other_pdf_excluded",
                      "read_only_hint_loading", "undo_excluded_history_retained",
                      "existing_copy_and_backup_preserved", "manual_protection_retained",
                      "source_pdf_and_hwpx_unchanged"):
            self.assertTrue(result[field], field)
        self.assertEqual(result["ocr_calls"], 0)
        self.assertEqual(result["network_calls"], 0)

    def test_release_smoke_clears_only_manually_edited_ocr_marks(self):
        from yeone_ocr.frozen_smoke import manual_style_cleanup_probe
        with mock.patch("socket.create_connection", side_effect=AssertionError("Unexpected network access")), \
             mock.patch("socket.getaddrinfo", side_effect=AssertionError("Unexpected DNS access")):
            result = manual_style_cleanup_probe(self.root)
        for field in ("edited_marks_cleared", "mixed_author_styles_restored",
                      "untouched_marks_and_paragraphs_preserved", "existing_copy_and_backup_preserved",
                      "manual_history_and_protection_retained", "saved_file_reopened",
                      "source_pdf_and_hwpx_unchanged"):
            self.assertTrue(result[field], field)
        self.assertEqual(result["ocr_calls"], 0)
        self.assertEqual(result["network_calls"], 0)

    def test_release_smoke_replaces_across_sections_and_recovers_paragraph_input(self):
        from yeone_ocr.frozen_smoke import bookmark_replace_probe
        with mock.patch("socket.create_connection", side_effect=AssertionError("Unexpected network access")), \
             mock.patch("socket.getaddrinfo", side_effect=AssertionError("Unexpected DNS access")):
            result = bookmark_replace_probe(self.root)
        self.assertEqual(result["editable_occurrences"], 3)
        self.assertEqual(result["readonly_skipped"], 2)
        for field in ("input_line_join_preserved_text", "section_and_document_scopes",
                      "pending_edits_preserved", "edited_marks_cleared",
                      "edited_text_copy_shortcuts", "copy_context_menu",
                      "unicode_copy_and_drafts_preserved", "user_clipboard_untouched",
                      "source_and_other_content_preserved", "existing_copy_and_backup_preserved",
                      "manual_history_and_protection_retained", "saved_file_reopened"):
            self.assertTrue(result[field], field)
        self.assertEqual(result["ocr_calls"], 0)
        self.assertEqual(result["network_calls"], 0)

    def test_release_smoke_keeps_comparison_visible_after_delayed_show_and_reload(self):
        from yeone_ocr.frozen_smoke import bookmark_layout_probe
        with mock.patch("socket.create_connection", side_effect=AssertionError("Unexpected network access")), \
             mock.patch("socket.getaddrinfo", side_effect=AssertionError("Unexpected DNS access")):
            result = bookmark_layout_probe(self.root)
        for field in ("delayed_first_show_balanced", "collapsed_pane_restored",
                      "reload_preserves_comparison", "pdf_and_font_follow_available_width",
                      "manual_resize_preserved", "source_pdf_and_hwpx_unchanged",
                      "corrected_file_not_created"):
            self.assertTrue(result[field], field)
        self.assertEqual(result["ocr_calls"], 0)
        self.assertEqual(result["network_calls"], 0)


if __name__ == "__main__":
    unittest.main()
