"""Hosted editor behavior, tested with synthetic documents and no OCR/API calls."""
from pathlib import Path
import copy
from dataclasses import asdict, replace
import hashlib
import json
import tempfile
import threading
import time
import unittest
import uuid
from types import SimpleNamespace
from unittest import mock

from PIL import Image

from ab_ocr.engine_registry import ENGINES, engine_set
from yeone_ocr.comparison_editor import ComparisonEditor, _anchor_recovery_notice, mapped_span, parse_page_range


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

    def test_recovery_notice_identifies_only_the_relocated_proposal(self):
        recovery = {"policy": "unique-literal-v1", "relocations": [
            {"original_start": 10, "original_end": 11, "start": 1, "end": 2,
             "before_sha256": hashlib.sha256("錯".encode()).hexdigest()}]}
        record = {"metadata": {"anchor_recovery": recovery}}
        notice = _anchor_recovery_notice(record, {"start": 1, "end": 2, "before": "錯"})
        self.assertIn("10–11", notice)
        self.assertIn("1–2", notice)
        self.assertIn("한 곳에만 일치", notice)
        self.assertEqual(_anchor_recovery_notice(record, {"start": 2, "end": 3, "before": "錯"}), "")
        self.assertEqual(_anchor_recovery_notice(record, {"start": 1, "end": 2, "before": "甲"}), "")


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
        with mock.patch("yeone_ocr.comparison.run_pages") as run:
            self.editor.recognize(True)
        run.assert_not_called()
        self.assertFalse(self.editor.busy)

    def test_cancel_checkpoints_completed_engine_and_closes_only_after_worker_finishes(self):
        self.project.save(self.folder / "review.yeone-compare.json")
        entered = threading.Event()
        release = threading.Event()
        ui_thread = threading.get_ident()
        callback_threads = []
        def run(project, numbers, *, on_result, should_stop, on_page, **kwargs):
            callback_threads.append(threading.get_ident())
            result = self.raw("tesseract", "甲乙")
            project.add_result(result)
            on_result(result)
            on_page(numbers[0], "tesseract", {"completed": ["tesseract"], "errors": {}})
            entered.set()
            release.wait(3)
            return {"pages": {numbers[0]: {"completed": ["tesseract"], "errors": {}}},
                    "errors": {}, "cancelled": should_stop()}
        with mock.patch("yeone_ocr.comparison.run_pages", side_effect=run):
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
            self.assertEqual(check.call_count, len(ENGINES))
            show.assert_called_once()
            self.assertEqual(len(show.call_args.args[0][1]), len(ENGINES))
        self.assertTrue(all(identifier != ui_thread for identifier in threads))

    def test_six_raw_tabs_are_read_only_and_mode_changes_preserve_saved_history(self):
        self.readings()
        self.add_ai_review()
        self.project.set_text(1, "사람이 확정한 神 神。")
        self.editor._populate_page()
        before = self.project.to_dict()
        self.assertEqual(set(self.editor.raw_widgets), set(ENGINES))
        for mode in ("권장 비교", "기존 3엔진", "전체 6엔진", "주력만"):
            self.editor.engine_mode.set(mode)
            self.editor._settings_changed()
            self.assertEqual(self.project.to_dict(), before)
            for widget in self.editor.raw_widgets.values():
                self.assertEqual(str(widget.cget("state")), "disabled")
        self.assertIn("단독 판독도", self.editor.hint.get())

    def test_selected_models_control_runner_scope_and_progress_for_new_page(self):
        routes = {"mixed": "glmocr", "traditional": "glmocr", "simplified": "glmocr"}
        with mock.patch.dict("ab_ocr.engine_registry.PRIMARY_ENGINES", routes):
            for label, mode in (("권장 비교", "recommended"), ("기존 3엔진", "legacy"),
                                ("전체 6엔진", "all"), ("주력만", "primary")):
                with self.subTest(mode=mode):
                    self.editor.engine_mode.set(label)
                    self.editor._settings_changed()
                    wanted = engine_set(mode, "traditional")
                    def batch(_project, numbers, *, engines, on_page, **_kwargs):
                        for number in numbers:
                            for engine in engines:
                                on_page(number, engine, {"completed": [], "errors": {}})
                        return {"pages": {}, "errors": {}, "cancelled": False}
                    with mock.patch("yeone_ocr.comparison.run_pages", side_effect=batch) as run:
                        self.editor.recognize()
                        self.pump(lambda: not self.editor.busy)
                        self.assertEqual(run.call_args.kwargs["engines"], wanted)
                        self.assertEqual(run.call_args.kwargs["primary_engine"], "glmocr" if "glmocr" in wanted else None)
                    self.assertEqual(float(self.editor.progress.cget("maximum")), len(wanted))
                    self.assertEqual(float(self.editor.progress.cget("value")), len(wanted))

    def test_existing_page_runner_does_not_replace_baseline_with_new_recommendation(self):
        self.readings()
        self.add_ai_review()
        self.project.set_text(1, "사람의 교정문")
        self.editor._populate_page()
        before = self.project.page(1)
        self.editor.engine_mode.set("전체 6엔진")
        self.editor._settings_changed()
        with mock.patch.dict("ab_ocr.engine_registry.PRIMARY_ENGINES", {"traditional": "glmocr"}), \
                mock.patch("yeone_ocr.comparison.run_pages", return_value={"pages": {}, "errors": {}, "cancelled": False}) as run:
            self.editor.recognize()
            self.pump(lambda: not self.editor.busy)
        # Batch orchestration applies the preference only to previously empty
        # pages; one range can contain both old and new pages.
        self.assertEqual(run.call_args.kwargs["primary_engine"], "glmocr")
        self.assertEqual(self.project.page(1), before)

    def test_one_model_can_start_selective_ai_without_changing_mode(self):
        self.editor.on_ai_settings = lambda: {"provider": "openai", "model": "synthetic", "api_key": "synthetic"}
        self.project.add_result(replace(self.raw("glmocr", "甲乙"), words=[],
                                       engine_metadata={"generation_complete": True, "bbox_granularity": "none"}))
        self.project.compare_page(1, baseline="glmocr")
        self.editor.engine_mode.set("주력만")
        self.editor._populate_page()
        self.assertFalse(self.editor.ai_start_button.instate(["disabled"]))
        self.assertIn("정상 판독 1개", self.editor.ai_count.get())
        self.project.add_result(self.raw("tesseract", "甲乙"))
        self.project.add_result(self.raw("easyocr", "甲乙"))
        self.project.compare_page(1, baseline="glmocr")
        self.editor._populate_page()
        self.assertFalse(self.editor.ai_start_button.instate(["disabled"]))
        self.assertEqual(self.editor.engine_mode.get(), "주력만")

    def test_incomplete_new_model_is_not_counted_for_ai_readiness(self):
        self.editor.on_ai_settings = lambda: {"provider": "openai", "model": "synthetic", "api_key": "synthetic"}
        self.project.add_result(self.raw("tesseract", "甲乙"))
        self.project.add_result(self.raw("easyocr", "甲乙"))
        self.project.add_result(replace(self.raw("glmocr", "甲"), words=[],
                                       engine_metadata={"generation_complete": False}))
        self.project.compare_page(1, baseline="easyocr")
        self.editor.engine_mode.set("전체 6엔진")
        self.editor._populate_page()
        self.editor._update_page(1)
        self.assertEqual(self.editor._ai_ready_count(), 2)
        self.assertFalse(self.editor.ai_start_button.instate(["disabled"]))
        self.assertTrue(self.editor.page_tree.item("1", "values")[1].startswith(f"2/{len(ENGINES)}"))

    def test_v6_single_mode_preserves_new_model_modes_and_allows_selective_ai(self):
        self.assertIn("ppocrv6", self.editor.raw_widgets)
        self.editor.on_ai_settings = lambda: {"provider": "openai", "model": "synthetic", "api_key": "synthetic"}
        self.editor.engine_mode.set("PP-OCRv6만")
        self.editor._settings_changed()
        self.assertEqual(self.editor._selected_engines(), ("ppocrv6",))
        self.project.add_result(self.raw("ppocrv6", "甲乙"))
        self.project.compare_page(1, baseline="ppocrv6")
        self.editor._populate_page()
        self.assertFalse(self.editor.ai_start_button.instate(["disabled"]))
        self.assertEqual(self.editor._ai_ready_count(), 1)

    def test_glm_without_word_coordinates_highlights_whole_original_only(self):
        self.readings()
        self.project.add_result(replace(self.raw("glmocr", "甲乙丙丁"), words=[],
                                       engine_metadata={"generation_complete": True, "bbox_granularity": "none"}))
        self.project.compare_page(1, baseline="easyocr")
        self.editor._populate_page()
        self.editor._preview = Image.new("RGB", (150, 200), "white")
        self.editor._origin, self.editor._image_scale = (8, 10), 2
        self.select_first()
        self.editor.candidate.set("glmocr")
        self.editor._candidate_changed()
        self.editor._highlight_image()
        rectangle = self.editor.canvas.find_withtag("evidence")
        self.assertEqual(len(rectangle), 1)
        self.assertEqual(self.editor.canvas.coords(rectangle[0]), [8.0, 10.0, 308.0, 410.0])
        self.assertIn("원본 전체", self.editor.evidence_label.get())
        raw = self.editor._latest_runs(self.project.page(1))["glmocr"]["raw"]
        self.assertEqual(raw["words"], [])

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
            whole_page = [widget for widget in widgets if widget.winfo_class() == "TCheckbutton" and "쪽 전체 이미지 검증" in widget.cget("text")]
            self.assertEqual(len(whole_page), 1)
            self.assertFalse(whole_page[0].instate(["selected"]))
            dialogs[0].destroy()
            prepare.assert_not_called()

    def test_declining_concrete_ai_plan_never_starts_transmission(self):
        plan = SimpleNamespace(summary={"pages": [1], "calls": 2, "targets": 1, "images": 1,
            "visual_calls": 1, "editorial_calls": 1, "max_output_tokens": 8192, "output_token_cap": 16384,
            "target_char_cap": 480, "batch_target_cap": 1, "anchor_policy": "unique-literal-v1", "codepoint_ledger_entries": 21},
            settings=SimpleNamespace(provider="openai", model="synthetic-model", max_output_tokens=8192),
            policy=SimpleNamespace(traditional=True, punctuation=True))
        with mock.patch.object(self.editor, "_ask_ai_plan_confirmation", return_value=False) as confirm, mock.patch.object(self.editor, "_run_ai_plan") as run:
            self.editor._confirm_ai_plan(plan, "synthetic-secret", self.project, key_count=3)
            run.assert_not_called()
        displayed = confirm.call_args.args[0]
        self.assertIn("synthetic-model", displayed)
        self.assertIn(str(self.source), displayed)
        self.assertIn("2회", displayed)
        self.assertIn("번체 후보: 포함", displayed)
        self.assertIn("8,192토큰", displayed)
        self.assertIn("16,384토큰", displayed)
        self.assertIn("최대 480자", displayed)
        self.assertIn("호출당 최대 1묶음", displayed)
        self.assertIn("문자 위치 대조표: 21항목", displayed)
        self.assertIn("이미지 판독 1회", displayed)
        self.assertIn("문자·부호 검토 1회", displayed)
        self.assertIn("키 3개 중 첫 번째", displayed)
        self.assertIn("한 곳에만 일치", displayed)
        self.assertNotIn("synthetic-secret", displayed)

    def test_ai_settings_have_no_implicit_provider_or_model_fallback(self):
        self.editor.on_ai_settings = lambda: {"provider": "openai", "model": None, "api_key": "synthetic-key"}
        self.editor.open_ai_verification()
        self.assertIn("준비되지", self.editor.status.get())
        self.assertFalse(self.editor.busy)

    def test_long_ai_confirmation_keeps_cancel_visible_and_scope_scrollable(self):
        self.editor.geometry("1080x720")
        self.root.update()
        errors = []
        def inspect_and_cancel():
            dialog = next(child for child in self.editor.winfo_children() if child.winfo_class() == "Toplevel")
            widgets = []
            def collect(widget):
                for child in widget.winfo_children():
                    widgets.append(child)
                    collect(child)
            collect(dialog)
            cancel = next(widget for widget in widgets if widget.winfo_class() == "TButton" and widget.cget("text") == "취소")
            try:
                text = next(widget for widget in widgets if widget.winfo_class() == "Text")
                self.assertEqual(str(text.cget("state")), "disabled")
                self.assertLess(text.yview()[1], 1)
                text.yview_moveto(1)
                self.assertAlmostEqual(text.yview()[1], 1)
                self.assertLessEqual(dialog.winfo_height(), 660)
                self.assertLessEqual(cancel.winfo_rooty() + cancel.winfo_height(), dialog.winfo_rooty() + dialog.winfo_height())
            except AssertionError as exc:
                errors.append(exc)
            finally:
                cancel.invoke()
        self.root.after(100, inspect_and_cancel)
        self.assertFalse(self.editor._ask_ai_plan_confirmation("synthetic approved scope\n" * 160))
        self.assertEqual(errors, [])

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

    def add_ai_review(self, *, uncertain=False, metadata=None):
        plan = self.ai_plan()
        target = plan.batches[0]["targets"][0]
        record = {"provider": "openai", "model": "synthetic-model", "policy": asdict(plan.policy),
            "status": "completed", "request": copy.deepcopy(plan.batches[0]), "response_text": self.ai_response(plan, uncertain=uncertain),
            "metadata": {"response_complete": True, "finish_reason": "completed"},
            "proposals": [{"id": uuid.uuid4().hex, "start": target["start"], "end": target["end"], "before": target["before"], "after": "乙",
                           "reason": "synthetic image evidence", "kind": "recognition", "uncertain": uncertain}]}
        if metadata is not None:
            record["metadata"].update(metadata)
        if record["request"].get("source_fidelity_contract"):
            from yeone_ocr.comparison_ai import PROMPT_VERSION
            from yeone_ocr.comparison_fidelity import assess_source_fidelity
            record["metadata"]["prompt_version"] = PROMPT_VERSION
            record["metadata"]["source_fidelity"] = assess_source_fidelity(
                record["request"], record["proposals"], policy=record["policy"])
        return self.project.record_ai_review(1, record)

    def supported_ai_readings(self):
        self.project.add_result(self.raw("tesseract", "天地玄黄甲乙丙丁宇宙洪荒"))
        self.project.add_result(self.raw("rapidocr", "天地玄黄甲乙丙丁宇宙洪荒"))
        self.project.add_result(self.raw("easyocr", "天地玄黄甲錯丙丁宇宙洪荒"))
        self.project.compare_page(1, baseline="easyocr")
        self.editor._populate_page()

    def select_ai_first(self):
        self.editor._populate_page()
        self.editor.proposal_notebook.select(1)
        identifier = self.editor.ai_tree.get_children()[0]
        self.editor.ai_tree.selection_set(identifier)
        self.editor._ai_selected()

    def test_ai_accept_undo_reopen_preserves_raw_response_and_original_ocr(self):
        self.supported_ai_readings()
        self.add_ai_review()
        before = self.project.page(1)
        self.select_ai_first()
        self.assertFalse(self.editor.ai_accept_button.instate(["disabled"]))
        self.editor.accept_ai_proposal()
        self.assertEqual(self.project.text(1), "天地玄黄甲乙丙丁宇宙洪荒")
        self.assertEqual(self.project.page(1)["runs"], before["runs"])
        self.assertEqual(self.project.page(1)["ai_reviews"], before["ai_reviews"])
        self.assertEqual(self.project.page(1)["ai_decisions"][-1]["decision"], "accepted")
        self.editor.undo_edit()
        self.assertEqual(self.project.text(1), "天地玄黄甲錯丙丁宇宙洪荒")
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

    def test_source_fidelity_hold_is_visible_and_rejectable_without_raw_changes(self):
        self.readings()
        self.add_ai_review()
        before = self.project.page(1)
        self.select_ai_first()
        selected = self.editor.ai_tree.get_children()[0]
        self.assertEqual(self.editor.ai_tree.item(selected, "values")[-1], "원본보존 보류")
        self.assertIn("원본보존 보류", self.editor.ai_detail.get())
        self.assertTrue(self.editor.ai_accept_button.instate(["disabled"]))
        self.editor.accept_ai_proposal()
        self.assertEqual(self.project.page(1), before)
        self.editor.reject_ai_proposal()
        after = self.project.page(1)
        self.assertEqual(after["runs"], before["runs"])
        self.assertEqual(after["ai_reviews"], before["ai_reviews"])
        self.assertEqual(after["ai_decisions"][-1]["decision"], "rejected")

    def test_details_separate_current_width_evidence_from_historical_hold(self):
        from yeone_ocr.comparison import _digest, latest_runs
        from yeone_ocr.comparison_ai import ReviewPolicy
        from yeone_ocr.comparison_fidelity import (SOURCE_FIDELITY_CONTRACT, assess_source_fidelity,
                                                  build_source_fidelity_evidence)
        snapshot = "天地玄黃,宇宙洪荒"
        for engine, value in (("glmocr", snapshot), ("easyocr", snapshot.replace(",", "，")),
                              ("surya", snapshot)):
            self.project.add_result(self.raw(engine, value))
        self.project.compare_page(1, baseline="glmocr")
        page = self.project.page(1)
        policy = asdict(ReviewPolicy(traditional=False, punctuation=True))
        proposal = {"id": uuid.uuid4().hex, "start": 4, "end": 5, "before": ",", "after": "，",
                    "reason": "synthetic punctuation evidence", "kind": "punctuation", "uncertain": False}
        batch = {"source_sha256": self.project.source_sha256, "page_number": 1,
                 "snapshot_text": snapshot, "snapshot_sha256": hashlib.sha256(snapshot.encode()).hexdigest(),
                 "baseline_run_id": page["baseline_run_id"],
                 "runs": [{"run_id": run["id"], "engine": run["engine"], "raw_sha256": run["raw_sha256"]}
                          for run in latest_runs(page).values()],
                 "targets": [{"id": "whole", "start": 0, "end": len(snapshot), "before": snapshot,
                              "bbox": [0, 0, 300, 400], "image_id": "synthetic-image"}],
                 "images": [{"id": "synthetic-image", "sha256": "a" * 64, "width": 300, "height": 400,
                             "source_width": 300, "source_height": 400, "source_bbox": [0, 0, 300, 400]}],
                 "phase": "editorial", "scope_fingerprint": "c" * 64, "batch_id": "d" * 32,
                 "source_fidelity_contract": "source-fidelity-v2", "response_contract": "comparison-ai-anchored-v1",
                 "source_fidelity_options": {key: policy[key] for key in
                                             ("traditional", "punctuation", "preserve_variants")}}
        runs = {run["id"]: run for run in page["runs"]}
        batch["source_fidelity_evidence"] = build_source_fidelity_evidence(batch, runs, policy=policy)
        batch["source_fidelity_evidence_sha256"] = _digest(batch["source_fidelity_evidence"])
        stored = assess_source_fidelity(batch, [proposal], runs=runs, policy=policy, contract="source-fidelity-v2")
        response = json.dumps({"items": [{"target_id": "whole", "uncertain": False, "note": "synthetic",
                                         "proposals": [{k: v for k, v in proposal.items() if k != "id"}]}]},
                              ensure_ascii=False)
        self.project.record_ai_review(1, {"provider": "openai", "model": "synthetic-model", "policy": policy,
            "status": "completed", "request": batch, "response_text": response, "proposals": [proposal],
            "metadata": {"phase": "editorial", "prompt_version": "comparison-ai-v9", "response_complete": True,
                         "finish_reason": "completed", "provisional_visual_proposal_ids": [], "source_fidelity": stored}})
        before = self.project.page(1)
        self.select_ai_first()
        item = self.editor._selected_ai_proposal
        self.assertTrue(stored["proposals"][0]["blocked"])
        self.assertEqual(item["source_fidelity_current"]["contract"], SOURCE_FIDELITY_CONTRACT)
        self.assertTrue(item["applicable"])
        evidence = item["source_fidelity"]["punctuation_evidence"]
        self.assertEqual(evidence["after_families"], ["easyocr"])
        self.assertEqual(evidence["glyph_after_families"], ["easyocr", "surya"])
        self.editor.show_ai_details()
        dialogs = [child for child in self.editor.winfo_children() if child.winfo_class() == "Toplevel"]
        displayed = "\n".join(child.get("1.0", "end-1c") for dialog in dialogs
                              for child in dialog.winfo_children() if child.winfo_class() == "Text")
        self.assertIn("현재 프로그램 검증 근거", displayed)
        self.assertIn("당시 저장된 원본보존 검증", displayed)
        self.assertIn(json.dumps(item["source_fidelity_current"], ensure_ascii=False, indent=2), displayed)
        self.assertIn(json.dumps(before["ai_reviews"][-1]["metadata"]["source_fidelity"],
                                 ensure_ascii=False, indent=2), displayed)
        self.assertFalse(self.editor.ai_accept_button.instate(["disabled"]))
        self.assertEqual(self.project.page(1), before)

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
            self.assertTrue(plan.policy.selective)
            self.assertEqual(plan.settings.max_calls, 50)
            from yeone_ocr.comparison_ai import AISettings
            self.assertEqual(plan.settings.max_output_tokens, AISettings("openai", "synthetic-model").max_output_tokens)
            self.assertEqual(plan.settings.max_total_output_tokens, 50 * plan.settings.max_output_tokens)
        self.assertNotIn("synthetic-secret", json.dumps(self.project.to_dict(), ensure_ascii=False))

    def test_recovered_proposal_is_reviewable_without_confirming_it(self):
        self.readings()
        plan = self.ai_plan()
        response = json.loads(self.ai_response(plan))
        response["items"][0]["proposals"][0].update(start=10, end=11)
        response_text = json.dumps(response, ensure_ascii=False)
        from yeone_ocr.comparison_ai import run_plan
        with mock.patch("yeone_ocr.comparison_ai.create_client", return_value=SimpleNamespace(close=lambda: None)), mock.patch("yeone_ocr.comparison_ai._request", return_value=(response_text, True, {"response_complete": True, "finish_reason": "completed"})):
            outcome = run_plan(self.project, plan, api_key="synthetic-key")
        self.assertEqual(outcome["completed"], 1)
        before = self.project.page(1)
        self.assertEqual(before["ai_reviews"][0]["response_text"], response_text)
        self.select_ai_first()
        self.assertIn("위치 복원", self.editor.ai_detail.get())
        self.assertIn("10–11", self.editor.ai_detail.get())
        self.editor.show_ai_details()
        self.editor.show_ai_history()
        dialogs = [child for child in self.editor.winfo_children() if child.winfo_class() == "Toplevel"]
        displayed = "\n".join(child.get("1.0", "end-1c") for dialog in dialogs
                              for child in dialog.winfo_children() if child.winfo_class() == "Text")
        self.assertIn("원시 응답:", displayed)
        self.assertIn(before["ai_reviews"][0]["response_text"], displayed)
        self.assertIn("위치를 복원한 검증용 응답", displayed)
        self.assertIn(before["ai_reviews"][0]["metadata"]["anchor_recovery"]["derived_response_text"], displayed)
        self.assertIn("종료 사유: completed", displayed)
        self.assertEqual(self.project.page(1), before)
        self.assertEqual(self.project.page(1)["ai_decisions"], [])

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
