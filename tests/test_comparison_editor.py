"""Hosted editor behavior, tested with synthetic documents and no OCR/API calls."""
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest import mock

from PIL import Image

from yeone_ocr.comparison_editor import ComparisonEditor, mapped_span, parse_page_range


class PageRanges(unittest.TestCase):
    def test_ranges_are_bounded_sorted_and_deduplicated(self):
        self.assertEqual(parse_page_range("3-5,1,4", 6), [1, 3, 4, 5])
        self.assertEqual(parse_page_range("", 3), [1, 2, 3])
        for value in ("0", "1-7", "5-2", "1,,2", "1-2-3", "١", "1.5", "all", "-1"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_page_range(value, 6)

    def test_span_mapping_does_not_guess_across_a_changed_glyph(self):
        self.assertEqual(mapped_span("甲乙丙丁", "前甲乙丙丁", 2, 3), (3, 4))
        self.assertIsNone(mapped_span("甲乙丙丁", "甲改丙丁", 1, 2))
        self.assertEqual(mapped_span("甲𠀀乙", "甲𠀀乙", 1, 2), (1, 2))


class HostedComparisonEditor(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            import tkinter as tk
            cls.root = tk.Tk()
            cls.root.withdraw()
        except Exception as exc:
            raise unittest.SkipTest(f"Tk display unavailable: {exc}")

    @classmethod
    def tearDownClass(cls):
        cls.root.destroy()

    def setUp(self):
        from yeone_ocr.comparison import ComparisonProject
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.source = self.folder / "source.png"
        with Image.new("RGB", (300, 400), "white") as image:
            image.save(self.source)
        self.project = ComparisonProject.open(self.source)
        self.preview_patch = mock.patch.object(ComparisonEditor, "_request_render")
        self.preview_patch.start()
        self.addCleanup(self.preview_patch.stop)
        self.editor = ComparisonEditor(self.root, project=self.project, work_dir=self.folder)
        self.addCleanup(self.editor._dispose)

    def raw(self, engine, text, languages="chi_tra", page_number=1):
        from ab_ocr.ocr import RecognitionResult
        return RecognitionResult(text=text,
            words=[{"text": text, "bbox": [10, 20, 250, 55], "confidence": 90}] if text else [],
            source_path=str(self.source), source_sha256=self.project.source_sha256,
            page_number=page_number, image_width=300, image_height=400,
            engine_version="synthetic-test", languages=languages, psm=6 if engine == "tesseract" else None,
            duration_seconds=0.1, engine=engine)

    def readings(self):
        self.project.add_result(self.raw("tesseract", "甲乙丙丁"))
        self.project.add_result(self.raw("rapidocr", "甲乙丙丁"))
        self.project.add_result(self.raw("easyocr", "甲錯丙丁"))
        self.project.compare_page(1)
        self.editor._populate_page()

    def pump(self, predicate=lambda: True, timeout=5):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            self.root.update()
            if predicate():
                return
            time.sleep(.01)
        self.fail("editor operation did not finish")

    def select_first(self):
        identifier = self.editor.diff_tree.get_children()[0]
        self.editor.diff_tree.selection_set(identifier)
        self.editor._difference_selected()
        return identifier

    def test_is_child_window_and_raw_tabs_remain_read_only_after_manual_edit_and_undo(self):
        import tkinter as tk
        self.assertIsInstance(self.editor, tk.Toplevel)
        self.assertIs(self.editor.master, self.root)
        self.readings()
        before = self.project.page(1)["runs"]
        self.editor.text.delete("1.0", "end")
        self.editor.text.insert("1.0", "甲乙丙丁𠀀，。")
        self.editor._text_changed()
        self.editor._commit_text()
        self.assertEqual(self.project.text(1), "甲乙丙丁𠀀，。")
        self.assertEqual(self.project.page(1)["runs"], before)
        self.assertEqual(self.project.page(1)["edits"][-1]["reason"], "manual")
        for widget in self.editor.raw_widgets.values():
            self.assertEqual(str(widget.cget("state")), "disabled")
        self.editor.undo_edit()
        self.assertEqual(self.project.text(1), "甲錯丙丁")
        self.assertEqual(self.project.page(1)["runs"], before)

    def test_explicit_candidate_selection_keeps_evidence_and_raw_records(self):
        self.readings()
        before = self.project.page(1)["runs"]
        identifier = self.select_first()
        self.editor.candidate.set("tesseract")
        self.editor._candidate_changed()
        self.editor.accept_candidate()
        self.assertEqual(self.project.text(1), "甲乙丙丁")
        self.assertEqual(self.project.page(1)["runs"], before)
        self.assertEqual(self.project.page(1)["edits"][-1]["kind"], "candidate")
        self.assertEqual(self.project.page(1)["decisions"][-1]["suggestion_id"], identifier)
        self.assertTrue(self.editor._write_recovery())
        from yeone_ocr.comparison import ComparisonProject
        reopened = ComparisonProject.load(self.editor._recovery_path)
        self.assertEqual(reopened.text(1), "甲乙丙丁")
        self.assertEqual(reopened.page(1)["runs"], before)

    def test_hanja_unsupported_korean_candidate_is_disabled(self):
        self.project.add_result(self.raw("tesseract", "한글甲", "kor+chi_tra"))
        self.project.add_result(self.raw("easyocr", "한글", "kor"))
        self.project.compare_page(1, baseline="tesseract")
        self.editor._populate_page()
        self.select_first()
        self.editor.candidate.set("easyocr")
        self.editor._candidate_changed()
        self.editor._set_enabled()
        self.assertTrue(self.editor.accept_button.instate(["disabled"]))
        self.assertIn("지원 범위", self.editor.candidate_detail.get())
        self.assertEqual(self.project.text(1), "한글甲")

    def test_image_evidence_scales_from_ocr_pixels_to_original_preview(self):
        self.readings()
        self.editor._preview = Image.new("RGB", (150, 200), "white")
        self.editor._origin = (8, 10)
        self.editor._image_scale = 2
        self.select_first()
        rectangle = self.editor.canvas.find_withtag("evidence")
        self.assertEqual(len(rectangle), 1)
        self.assertEqual(self.editor.canvas.coords(rectangle[0]), [18.0, 30.0, 258.0, 65.0])

    def test_late_render_is_disposed_without_replacing_current_page(self):
        old = Image.new("RGB", (10, 10), "red")
        self.editor._dispatch(("render", self.editor._generation - 1, self.editor._render_id, old))
        self.assertIsNone(self.editor._preview)
        with self.assertRaises(ValueError):
            old.getpixel((0, 0))

    def test_invalid_scope_never_starts_recognition(self):
        self.editor.range_var.set("1-999")
        with mock.patch("yeone_ocr.comparison.run_page") as run:
            self.editor.recognize(True)
        run.assert_not_called()
        self.assertFalse(self.editor.busy)

    def test_cancel_checkpoints_completed_engine_and_closes_only_after_worker_finishes(self):
        self.project.save(self.folder / "review.yeone-compare.json")
        entered = threading.Event()
        release = threading.Event()
        ui_thread = threading.get_ident()
        callback_threads = []
        def run(project, number, *, on_result, should_stop, **kwargs):
            callback_threads.append(threading.get_ident())
            result = self.raw("tesseract", "甲乙")
            project.add_result(result)
            on_result(result)
            entered.set()
            release.wait(3)
            return {"completed": ["tesseract"], "errors": {}, "cancelled": should_stop()}
        with mock.patch("yeone_ocr.comparison.run_page", side_effect=run):
            self.editor.recognize()
            self.assertTrue(entered.wait(3))
            self.assertTrue(self.editor.busy)
            self.assertEqual(str(self.editor.text.cget("state")), "disabled")
            self.assertFalse(self.editor.request_close())
            self.assertTrue(self.editor._stop.is_set())
            self.assertTrue(self.editor.winfo_exists())
            release.set()
            self.pump(lambda: self.editor._closed)
        self.assertNotEqual(callback_threads, [ui_thread])
        from yeone_ocr.comparison import ComparisonProject
        reopened = ComparisonProject.load(self.folder / "review.yeone-compare.json")
        self.assertEqual(reopened.text(1), "甲乙")
        self.assertEqual(reopened.page(1)["runs"][0]["engine"], "tesseract")
        self.assertTrue(self.editor._recovery_path.exists())

    def test_close_saves_known_project_and_preserves_host(self):
        self.project.save(self.folder / "review.yeone-compare.json")
        self.readings()
        self.editor.text.insert("end-1c", "。")
        self.editor._text_changed()
        self.editor.close()
        self.assertTrue(self.editor._closed)
        self.assertTrue(self.root.winfo_exists())
        from yeone_ocr.comparison import ComparisonProject
        reopened = ComparisonProject.load(self.folder / "review.yeone-compare.json")
        self.assertEqual(reopened.text(1), "甲錯丙丁。")

    def test_stale_job_completion_cannot_replace_open_project(self):
        self.editor._busy = True
        self.editor._dispatch(("done", self.editor._generation - 1, self.editor._job_id, ("project", object())))
        self.assertIs(self.editor.project, self.project)
        self.assertTrue(self.editor.busy)
        self.editor._busy = False

    def test_install_check_uses_selected_root_and_never_runs_on_ui_thread(self):
        ui_thread = threading.get_ident()
        threads = []
        selected = self.folder / "local-engines"
        self.editor.engine_root = selected
        def doctor(*, engine, engine_root, should_stop):
            threads.append(threading.get_ident())
            self.assertEqual(engine_root, selected)
            return {"engine": engine, "available": True, "languages": ["chi_tra"], "message": "verified"}
        with mock.patch("ab_ocr.ocr.doctor", side_effect=doctor) as check, mock.patch.object(self.editor, "_show_engine_info") as show:
            self.editor.inspect_engines()
            self.pump(lambda: not self.editor.busy)
            self.assertEqual(check.call_count, 3)
            show.assert_called_once()
            self.assertEqual(len(show.call_args.args[0][1]), 3)
        self.assertTrue(all(identifier != ui_thread for identifier in threads))

    def test_save_conflict_never_overwrites_external_project_and_can_close_with_recovery(self):
        from yeone_ocr.comparison import ComparisonError, ComparisonProject
        project_path = self.folder / "review.yeone-compare.json"
        self.project.save(project_path)
        previous = project_path.read_bytes()
        self.readings()
        with mock.patch.object(self.project, "save", side_effect=ComparisonError("changed externally")):
            self.editor.close()
        self.assertFalse(self.editor._closed)
        self.assertTrue(self.editor._recovery_path.exists())
        self.assertEqual(project_path.read_bytes(), previous)
        self.editor._close_from_recovery()
        self.assertTrue(self.editor._closed)
        self.assertEqual(ComparisonProject.load(self.editor._recovery_path).text(1), "甲錯丙丁")

    def test_historical_import_never_replaces_current_raw_display_or_bbox_evidence(self):
        self.readings()
        active = self.project.page(1)["runs"][0]
        self.project.add_result(self.raw("tesseract", "이전 판독", "kor"), activate=False)
        self.editor._populate_page()
        self.assertEqual(self.editor.raw_widgets["tesseract"].get("1.0", "end-1c"), "甲乙丙丁")
        self.assertEqual(self.editor._latest_runs(self.project.page(1))["tesseract"]["id"], active["id"])
        self.assertEqual(len(self.project.page(1)["runs"]), 4)

    def test_old_tk_variable_cycles_are_finalized_before_background_work(self):
        import gc
        import tkinter as tk
        import weakref
        main_thread = threading.get_ident()
        finalized = []
        class OwnedVariable(tk.StringVar):
            def __del__(variable):
                finalized.append(threading.get_ident())
                super().__del__()
        previously_enabled = gc.isenabled()
        gc.disable()
        try:
            variable = OwnedVariable(self.root, value="synthetic")
            variable.cycle = variable
            reference = weakref.ref(variable)
            del variable
            with mock.patch("ab_ocr.ocr.doctor", return_value={"available": False}), mock.patch.object(self.editor, "_show_engine_info"):
                self.editor.inspect_engines()
                self.pump(lambda: not self.editor.busy)
            self.assertIsNone(reference())
            self.assertEqual(finalized, [main_thread])
        finally:
            if previously_enabled:
                gc.enable()


if __name__ == "__main__":
    unittest.main()
