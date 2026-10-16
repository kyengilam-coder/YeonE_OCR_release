"""Hosted editor behavior, tested with synthetic documents and no OCR/API calls."""
from pathlib import Path
import copy
from dataclasses import asdict
import json
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
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

    def test_open_ai_options_reads_host_settings_only_on_ui_thread_and_sends_nothing(self):
        called = []
        main_thread = threading.get_ident()
        def settings():
            called.append(threading.get_ident())
            return {"provider": "openai", "model": "synthetic-model", "api_key": "synthetic-key"}
        self.editor.on_ai_settings = settings
        with mock.patch.object(self.editor, "_prepare_ai") as prepare:
            self.editor.open_ai_verification()
            self.assertEqual(called, [main_thread])
            dialogs = [child for child in self.editor.winfo_children() if child.winfo_class() == "Toplevel"]
            self.assertEqual(len(dialogs), 1)
            widgets = []
            def collect(widget):
                widgets.extend(widget.winfo_children())
                for child in widget.winfo_children():
                    collect(child)
            collect(dialogs[0])
            policy_checks = [widget for widget in widgets if widget.winfo_class() == "TCheckbutton" and ("번체" in widget.cget("text") or "중문부호" in widget.cget("text"))]
            self.assertEqual(len(policy_checks), 2)
            self.assertTrue(all(widget.instate(["selected"]) for widget in policy_checks))
            dialogs[0].destroy()
            prepare.assert_not_called()

    def test_declining_concrete_ai_plan_never_starts_transmission(self):
        plan = SimpleNamespace(summary={"pages": [1], "calls": 2, "targets": 1, "images": 1},
            settings=SimpleNamespace(provider="openai", model="synthetic-model"),
            policy=SimpleNamespace(traditional=True, punctuation=True))
        with mock.patch("yeone_ocr.comparison_editor.messagebox.askyesno", return_value=False) as confirm, mock.patch.object(self.editor, "_run_ai_plan") as run:
            self.editor._confirm_ai_plan(plan, "synthetic-secret", self.project)
            run.assert_not_called()
        displayed = confirm.call_args.args[1]
        self.assertIn("synthetic-model", displayed)
        self.assertIn(str(self.source), displayed)
        self.assertIn("2회", displayed)
        self.assertIn("번체 후보: 포함", displayed)
        self.assertNotIn("synthetic-secret", displayed)

    def test_ai_settings_have_no_implicit_provider_or_model_fallback(self):
        self.editor.on_ai_settings = lambda: {"provider": "openai", "model": None, "api_key": "synthetic-key"}
        self.editor.open_ai_verification()
        self.assertIn("준비되지", self.editor.status.get())
        self.assertFalse(self.editor.busy)

    def ai_plan(self, *, editorial=False):
        from yeone_ocr.comparison_ai import AISettings, ReviewPolicy, prepare_plan
        return prepare_plan(self.project, [1], ReviewPolicy(traditional=editorial, punctuation=editorial),
            settings=AISettings(provider="openai", model="synthetic-model"))

    def ai_response(self, plan, *, uncertain=False):
        batch = plan.batches[0]
        target = batch["targets"][0]
        proposal = {"start": target["start"], "end": target["end"], "before": target["before"],
                    "after": "乙", "reason": "synthetic image evidence", "kind": "recognition", "uncertain": uncertain}
        return json.dumps({"items": [{"target_id": target["id"], "uncertain": uncertain, "note": "synthetic", "proposals": [proposal]}]}, ensure_ascii=False)

    def add_ai_review(self, *, uncertain=False):
        plan = self.ai_plan()
        target = plan.batches[0]["targets"][0]
        record = {"provider": "openai", "model": "synthetic-model", "policy": asdict(plan.policy),
            "status": "completed", "request": copy.deepcopy(plan.batches[0]), "response_text": self.ai_response(plan, uncertain=uncertain),
            "proposals": [{"start": target["start"], "end": target["end"], "before": target["before"], "after": "乙",
                           "reason": "synthetic image evidence", "kind": "recognition", "uncertain": uncertain}]}
        return self.project.record_ai_review(1, record)

    def select_ai_first(self):
        self.editor._populate_page()
        self.editor.proposal_notebook.select(1)
        identifier = self.editor.ai_tree.get_children()[0]
        self.editor.ai_tree.selection_set(identifier)
        self.editor._ai_selected()

    def test_ai_accept_undo_reopen_preserves_raw_response_and_original_ocr(self):
        self.readings()
        self.add_ai_review()
        before = self.project.page(1)
        self.select_ai_first()
        self.assertFalse(self.editor.ai_accept_button.instate(["disabled"]))
        self.editor.accept_ai_proposal()
        self.assertEqual(self.project.text(1), "甲乙丙丁")
        self.assertEqual(self.project.page(1)["runs"], before["runs"])
        self.assertEqual(self.project.page(1)["ai_reviews"], before["ai_reviews"])
        self.assertEqual(self.project.page(1)["ai_decisions"][-1]["decision"], "accepted")
        self.editor.undo_edit()
        self.assertEqual(self.project.text(1), "甲錯丙丁")
        self.assertEqual(self.project.page(1)["ai_decisions"][-1]["decision"], "undone")
        self.assertTrue(self.editor._write_recovery())
        from yeone_ocr.comparison import ComparisonProject
        reopened = ComparisonProject.load(self.editor._recovery_path)
        self.assertEqual(reopened.page(1)["ai_reviews"], before["ai_reviews"])
        self.assertEqual(reopened.page(1)["runs"], before["runs"])

    def test_uncertain_ai_proposal_is_blocked_until_manually_reviewed(self):
        self.readings()
        self.add_ai_review(uncertain=True)
        self.select_ai_first()
        self.assertTrue(self.editor.ai_accept_button.instate(["disabled"]))
        self.editor.accept_ai_proposal()
        self.assertEqual(self.project.text(1), "甲錯丙丁")
        self.assertEqual(self.project.page(1)["ai_decisions"], [])

    def test_ai_cancel_checkpoints_first_real_pipeline_response_without_second_call(self):
        self.readings()
        plan = self.ai_plan(editorial=True)
        self.project.save(self.folder / "ai-review.yeone-compare.json")
        before = self.project.page(1)["runs"]
        response = self.ai_response(plan)
        entered, release = threading.Event(), threading.Event()
        caller_threads = []
        def request(*_args, **_kwargs):
            caller_threads.append(threading.get_ident())
            entered.set()
            release.wait(3)
            return response, True, {}
        with mock.patch("yeone_ocr.comparison_ai.create_client", return_value=SimpleNamespace(close=lambda: None)), mock.patch("yeone_ocr.comparison_ai._request", side_effect=request) as remote:
            self.editor._run_ai_plan(self.project, plan, "synthetic-key")
            self.assertTrue(entered.wait(3))
            self.editor.cancel()
            release.set()
            self.pump(lambda: not self.editor.busy)
            self.assertEqual(remote.call_count, 1)
        self.assertNotEqual(caller_threads, [threading.get_ident()])
        self.assertEqual(self.project.text(1), "甲錯丙丁")
        self.assertIn("중지됨", self.editor.status.get())
        from yeone_ocr.comparison import ComparisonProject
        reopened = ComparisonProject.load(self.folder / "ai-review.yeone-compare.json")
        self.assertEqual(len(reopened.page(1)["ai_reviews"]), 1)
        self.assertEqual(reopened.page(1)["ai_reviews"][0]["response_text"], response)
        self.assertEqual(reopened.page(1)["runs"], before)
        self.assertTrue(self.editor._recovery_path.exists())

    def test_ai_preparation_has_no_remote_calls_and_preserves_selected_budget(self):
        self.readings()
        with mock.patch("yeone_ocr.comparison_ai.create_client") as client, mock.patch("yeone_ocr.comparison_ai._request") as remote, mock.patch.object(self.editor, "_confirm_ai_plan") as confirm:
            self.editor._prepare_ai([1], None, {"traditional": True, "punctuation": True}, "openai", "synthetic-model", "synthetic-secret", max_calls=50)
            self.pump(lambda: not self.editor.busy)
            client.assert_not_called()
            remote.assert_not_called()
            confirm.assert_called_once()
            plan = confirm.call_args.args[0]
            self.assertEqual(plan.settings.max_calls, 50)
            self.assertEqual(plan.settings.max_total_output_tokens, 50 * 4096)
        self.assertNotIn("synthetic-secret", json.dumps(self.project.to_dict(), ensure_ascii=False))

    def test_rejecting_ai_proposal_preserves_text_and_raw_answer(self):
        self.readings()
        self.add_ai_review()
        original = self.project.page(1)["ai_reviews"]
        self.select_ai_first()
        self.editor.reject_ai_proposal()
        self.assertEqual(self.project.text(1), "甲錯丙丁")
        self.assertEqual(self.project.page(1)["ai_reviews"], original)
        self.assertEqual(self.project.page(1)["ai_decisions"][-1]["decision"], "rejected")

    def test_ai_source_crop_highlight_uses_original_render_coordinates(self):
        self.readings()
        self.add_ai_review()
        self.editor._preview = Image.new("RGB", (150, 200), "white")
        self.editor._origin, self.editor._image_scale = (8, 10), 2
        self.select_ai_first()
        rectangle = self.editor.canvas.find_withtag("evidence")
        self.assertEqual(len(rectangle), 1)
        self.assertEqual(self.editor.canvas.coords(rectangle[0]), [18.0, 30.0, 258.0, 65.0])


if __name__ == "__main__":
    unittest.main()
