"""Real Tk regression checks for raw text preservation and async result safety."""
from __future__ import annotations

import json
import hashlib
from pathlib import Path
import tempfile
import threading
import time
import tkinter as tk
import unittest
from unittest.mock import patch

from ab_ocr.gui import EvaluationWindow, EXAMPLE_REFERENCE, _text_index


class WorkbenchTests(unittest.TestCase):
    def setUp(self):
        try:
            self.app = EvaluationWindow()
        except tk.TclError as exc:
            self.skipTest(f"Tk display unavailable: {exc}")
        self.app.withdraw()
        self.errors = []
        self.error_patch = patch("ab_ocr.gui.messagebox.showerror", side_effect=lambda *a, **k: self.errors.append(a))
        self.error_patch.start()
        self.addCleanup(self.error_patch.stop)
        self.addCleanup(lambda: self.app._close(force=True))

    def wait(self):
        deadline = time.monotonic() + 6
        while self.app._busy and time.monotonic() < deadline:
            self.app.update()
            time.sleep(.01)
        self.app.update()
        self.assertFalse(self.app._busy, "GUI worker did not finish")

    def test_example_is_labelled_synthetic_and_displays_actual_errors(self):
        self.app.load_example()
        self.wait()
        self.assertFalse(self.errors)
        report = self.app._report
        self.assertTrue(report["metadata"]["synthetic_example"])
        self.assertEqual(report["raw"]["substitutions"], 2)
        self.assertEqual(report["raw"]["deletions"], 1)
        self.assertEqual(report["raw"]["reference_length"], len(EXAMPLE_REFERENCE))
        self.assertEqual(self.app.raw_var.get(), "5.00%")
        self.assertEqual(len(self.app.error_tree.get_children()), 3)
        self.assertIn("한자", self.app.scripts_var.get())

    def test_utf8_bom_transport_and_crlf_are_not_silently_normalized(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "reference.txt"
            source.write_bytes(b"\xef\xbb\xbf" + "가\r\n漢\r\n".encode("utf-8"))
            self.app.load_text("reference", source)
            self.wait()
            self.assertEqual(self.app._text("reference"), "가\r\n漢\r\n")
            self.assertEqual(self.app._loaded_text["reference"], "가\r\n漢\r\n")
            self.assertIn(source.resolve(), self.app._protected_paths)
            self.assertEqual(self.app._input_metadata["reference"]["file_sha256"], hashlib.sha256(source.read_bytes()).hexdigest())

    def test_raw_snapshot_survives_edit_and_report_is_invalidated(self):
        self.app.load_example()
        self.wait()
        raw = self.app._loaded_text["hypothesis"]
        self.app.text_widgets["hypothesis"].insert("end-1c", "改")
        self.app.update()
        self.assertEqual(self.app._loaded_text["hypothesis"], raw)
        self.assertTrue(self.app._edited["hypothesis"])
        self.assertIsNone(self.app._report)
        self.assertEqual(str(self.app.export_button.cget("state")), "disabled")
        self.assertEqual(len(self.app.error_tree.get_children()), 0)
        with patch("ab_ocr.gui.messagebox.askyesno", return_value=True):
            self.app.restore_text("hypothesis")
        self.assertEqual(self.app._text("hypothesis"), raw)

    def test_normalization_changes_only_normalized_score(self):
        self.app._set_text("reference", "가 漢", "정답")
        self.app._set_text("hypothesis", "가漢", "OCR")
        self.app.whitespace_var.set("모든 공백 제외")
        self.app.compare()
        self.wait()
        self.assertGreater(self.app._report["raw"]["cer"], 0)
        self.assertEqual(self.app._report["normalized"]["cer"], 0)
        self.assertEqual(self.app._text("reference"), "가 漢")

    def test_error_selection_uses_raw_codepoint_offsets_after_non_bmp_hanja(self):
        reference, result = "𠀀漢\n가", "𠀀宇\n가"
        self.app._set_text("reference", reference, "정답")
        self.app._set_text("hypothesis", result, "OCR")
        self.app.compare()
        self.wait()
        self.app.error_tree.selection_set("0")
        self.app._select_error()
        for key, expected in (("reference", "漢"), ("hypothesis", "宇")):
            widget = self.app.text_widgets[key]
            start, end = widget.tag_ranges("error")
            self.assertEqual(widget.get(start, end), expected)
        widget = self.app.text_widgets["reference"]
        self.assertEqual(widget.get(_text_index(widget, reference, 3), _text_index(widget, reference, 4)), "가")

    def test_worker_runs_off_ui_thread_and_cancelled_result_is_discarded(self):
        release = threading.Event()
        callback_results = []
        worker_thread = []
        main_thread = threading.get_ident()
        def worker(stop, progress):
            worker_thread.append(threading.get_ident())
            release.wait(2)
            return "late result"
        self.app._start("시험", worker, callback_results.append)
        self.assertEqual(str(self.app.text_widgets["reference"].cget("state")), "disabled")
        self.app.cancel()
        release.set()
        self.wait()
        self.assertEqual(callback_results, [])
        self.assertNotEqual(worker_thread[0], main_thread)
        self.assertEqual(str(self.app.text_widgets["reference"].cget("state")), "normal")
        self.assertIn("취소됨", self.app.status_var.get())

    def test_old_job_event_cannot_overwrite_current_result(self):
        release = threading.Event()
        results = []
        self.app._start("첫 작업", lambda _s, _p: "first", results.append)
        self.wait()
        old_id = self.app._job_id
        def worker(_s, _p):
            release.wait(2)
            return "second"
        self.app._start("둘째 작업", worker, results.append)
        self.app._events.put((old_id, "done", "stale"))
        release.set()
        self.wait()
        self.assertEqual(results, ["first", "second"])

    def test_failed_file_load_keeps_existing_text_and_reports_error(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "non-utf8.txt"
            source.write_bytes(b"\xff\xff")
            self.app._set_text("reference", "保全", "existing")
            self.app.load_text("reference", source)
            self.wait()
            self.assertEqual(self.app._text("reference"), "保全")
            self.assertEqual(len(self.errors), 1)
            self.assertIn("실패", self.app.status_var.get())

    def test_report_save_protects_loaded_source(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "source.json"
            source.write_text("保全", encoding="utf-8")
            self.app._protected_paths.add(source.resolve())
            self.app.load_example()
            self.wait()
            self.app.save_report(source)
            self.wait()
            self.assertEqual(source.read_text(encoding="utf-8"), "保全")
            self.assertEqual(len(self.errors), 1)
            destination = Path(folder) / "report.json"
            self.app.save_report(destination)
            self.wait()
            self.assertEqual(json.loads(destination.read_text(encoding="utf-8"))["raw"]["deletions"], 1)

    def test_comparison_completion_is_ignored_if_revision_changes(self):
        self.app._set_text("reference", "漢", "정답")
        self.app._set_text("hypothesis", "宇", "OCR")
        self.app.compare()
        # Programmatic replacement may happen even though normal user controls
        # are locked. A completed worker must still respect the revision guard.
        self.app._set_text("reference", "새 정답", "new")
        self.wait()
        self.assertIsNone(self.app._report)
        self.assertIn("바뀌어", self.app.status_var.get())

    def test_benchmark_completion_replaces_old_error_rows(self):
        self.app.load_example()
        self.wait()
        self.assertEqual(len(self.app.error_tree.get_children()), 3)
        report = {"kind": "benchmark", "engines": {"sample": {"name": "샘플 엔진", "completed_pages": 1,
                  "total_pages": 2, "complete": False, "raw": {"cer": .1}, "normalized": {"cer": .1}}}}
        self.app._show_benchmark(report)
        self.assertEqual(len(self.app.error_tree.get_children()), 0)
        self.assertEqual(len(self.app.benchmark_tree.get_children()), 1)
        self.assertEqual(self.app._report_kind, "benchmark")

    def test_saved_raw_and_current_copies_remain_distinct(self):
        with tempfile.TemporaryDirectory() as folder:
            self.app._set_text("hypothesis", "原本", "engine")
            self.app.text_widgets["hypothesis"].insert("end-1c", "改")
            self.app.update()
            raw_path = Path(folder) / "raw.txt"
            edited_path = Path(folder) / "edited.txt"
            self.app.save_text("hypothesis", raw=True, path=raw_path)
            self.wait()
            self.app.save_text("hypothesis", path=edited_path)
            self.wait()
            self.assertEqual(raw_path.read_text(encoding="utf-8"), "原本")
            self.assertEqual(edited_path.read_text(encoding="utf-8"), "原本改")

    def test_declining_discard_preserves_edits_across_all_replacement_actions(self):
        self.app.text_widgets["hypothesis"].insert("1.0", "손으로 교정한 내용")
        self.app.update()
        with patch("ab_ocr.gui.messagebox.askyesno", return_value=False) as confirm:
            self.app.load_text("hypothesis", Path("unused.txt"))
            self.app.load_example()
            self.app._run_ocr(Path("unused.png"), "kor", 1)
            self.app._close()
        self.assertEqual(confirm.call_count, 4)
        self.assertFalse(self.app._busy)
        self.assertFalse(self.app._closed)
        self.assertEqual(self.app._text("hypothesis"), "손으로 교정한 내용")

    def test_saved_edit_is_still_marked_edited_but_close_does_not_warn(self):
        with tempfile.TemporaryDirectory() as folder:
            self.app._set_text("hypothesis", "원시 결과", "raw")
            self.app.text_widgets["hypothesis"].insert("end-1c", " 교정")
            self.app.update()
            self.app.save_text("hypothesis", path=Path(folder) / "saved.txt")
            self.wait()
            self.assertTrue(self.app._edited["hypothesis"])
            with patch("ab_ocr.gui.messagebox.askyesno") as confirm:
                self.app._close()
            confirm.assert_not_called()
            self.assertTrue(self.app._closed)

    def test_unsaved_actual_ocr_text_is_guarded_even_without_manual_edits(self):
        self.app._set_text("hypothesis", "실제 OCR 원시 결과", "local", saved=False)
        self.app._ocr_unsaved = True
        self.assertFalse(self.app._edited["hypothesis"])
        with patch("ab_ocr.gui.messagebox.askyesno", return_value=False):
            self.app.load_example()
            self.app._close()
        self.assertEqual(self.app._text("hypothesis"), "실제 OCR 원시 결과")
        self.assertFalse(self.app._closed)

    def test_too_large_text_is_rejected_without_replacing_existing_input(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "large.txt"
            source.write_bytes(b"x" * 8_000_001)
            self.app._set_text("reference", "원문 보존", "existing")
            self.app.load_text("reference", source)
            self.wait()
            self.assertEqual(self.app._text("reference"), "원문 보존")
            self.assertEqual(len(self.errors), 1)
            self.assertIn("8 MB", self.errors[0][1])

    def test_benchmark_save_dialog_does_not_offer_csv(self):
        self.app._show_benchmark({"kind": "benchmark", "engines": {}})
        with patch("ab_ocr.gui.filedialog.asksaveasfilename", return_value="") as picker:
            self.app.save_report()
        self.assertEqual([pattern for _label, pattern in picker.call_args.kwargs["filetypes"]], ["*.html", "*.json"])

    def test_engine_doctor_receives_cooperative_cancellation(self):
        with patch("ab_ocr.ocr.doctor", return_value={"available": False, "message": "미설치"}) as doctor, \
                patch("ab_ocr.gui.messagebox.showinfo"):
            self.app.check_engine()
            self.wait()
        callback = doctor.call_args.kwargs["should_stop"]
        self.assertTrue(callable(callback))
        self.app._cancel.set()
        self.assertTrue(callback())

    def test_close_waits_for_uncancellable_write_and_its_callback(self):
        release = threading.Event()
        completed = []
        def worker(_stop, _progress):
            release.wait(2)
            return "saved"
        self.app._start("저장", worker, completed.append, cancellable=False)
        self.app._close()
        self.assertFalse(self.app._closed)
        self.assertTrue(self.app._close_after_write)
        release.set()
        deadline = time.monotonic() + 4
        while not self.app._closed and time.monotonic() < deadline:
            self.app.update()
            time.sleep(.01)
        self.assertTrue(self.app._closed)
        self.assertEqual(completed, ["saved"])


if __name__ == "__main__":
    unittest.main()
