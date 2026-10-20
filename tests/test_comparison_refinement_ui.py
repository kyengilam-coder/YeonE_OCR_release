"""Hosted refinement UI exercises real core evidence with synthetic images."""
from dataclasses import replace
import threading
import unittest
from unittest import mock

from PIL import Image

from ab_ocr import ocr
from tests import test_comparison_editor as hosted
from tests import test_comparison_refinement as fixtures
from yeone_ocr import comparison_refinement as refinement
from yeone_ocr.comparison import ComparisonProject


class RefinementPanelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        hosted.HostedComparisonEditor.setUpClass.__func__(cls)

    @classmethod
    def tearDownClass(cls):
        hosted.HostedComparisonEditor.tearDownClass.__func__(cls)

    def setUp(self):
        hosted.HostedComparisonEditor.setUp(self)
        self.panel = self.editor.refinement_panel

    pump = hosted.HostedComparisonEditor.pump
    raw = hosted.HostedComparisonEditor.raw
    conditions = fixtures.RefinementTests.conditions

    def readings(self):
        self.before, self.after = "甲乙丙丁,戊己庚辛", "甲乙丙丁，戊己庚辛"
        self.project.add_result(replace(self.raw("glmocr", self.before), words=[],
            engine_metadata={"generation_complete": True, "bbox_granularity": "none"}))
        self.project.add_result(self.raw("easyocr", self.after))
        self.project.add_result(self.raw("tesseract", self.before))
        self.project.choose_baseline(1, "glmocr")
        self.editor._populate_page()

    def generate(self):
        self.readings()
        self.panel.generate()
        self.pump(lambda: not self.editor.busy)
        self.assertEqual(len(self.panel.tree.get_children()), 1)
        self.select_first()

    def select_first(self):
        key = self.panel.tree.get_children()[0]
        self.panel.tree.selection_set(key)
        self.panel.selected()
        return key

    def test_generation_runs_off_ui_thread_and_does_not_apply_or_call_ai(self):
        self.readings();before = self.project.page(1)["runs"]
        caller_threads = []
        implementation = refinement.build_candidates
        def build(*args, **kwargs):
            caller_threads.append(threading.get_ident())
            return implementation(*args, **kwargs)
        with mock.patch.object(refinement, "build_candidates", side_effect=build), \
                mock.patch("yeone_ocr.comparison_ai.create_client") as client:
            self.panel.generate()
            self.pump(lambda: not self.editor.busy)
        self.assertTrue(caller_threads)
        self.assertTrue(all(caller != threading.get_ident() for caller in caller_threads))
        client.assert_not_called()
        self.assertEqual(self.project.text(1), self.before)
        self.assertEqual(self.project.page(1)["edits"], [])
        self.assertEqual(self.project.page(1)["runs"], before)
        self.assertEqual(len(self.panel.tree.get_children()), 1)
        reopened = ComparisonProject.load(self.editor._recovery_path)
        self.assertEqual(reopened.page(1)["refinement_reviews"], self.project.page(1)["refinement_reviews"])

    def test_selected_glm_baseline_candidate_highlights_actual_easy_line_box(self):
        self.generate()
        self.editor._preview = Image.new("RGB", (150, 200), "white")
        self.editor._origin, self.editor._image_scale = (8, 10), 2
        self.panel.selected()
        rectangles = self.editor.canvas.find_withtag("evidence")
        self.assertEqual(len(rectangles), 1)
        self.assertEqual(self.editor.canvas.coords(rectangles[0]), [18.0, 30.0, 258.0, 65.0])
        self.assertEqual(self.panel.current()["evidence"]["engine"], "easyocr")
        self.assertEqual(self.project._baseline(self.project.page(1))["raw"]["words"], [])
        self.assertEqual(self.editor.text.get("selection-evidence.first", "selection-evidence.last"), ",")

    def test_accept_undo_and_reopen_preserve_raw_and_ai_separation(self):
        self.generate();before = self.project.page(1)
        self.assertFalse(self.panel.accept_button.instate(["disabled"]))
        self.panel.decide(True)
        self.assertEqual(self.project.text(1), self.after)
        self.assertEqual(self.project.page(1)["edits"][-1]["kind"], "refinement_accept")
        self.assertEqual(self.project.page(1)["runs"], before["runs"])
        self.assertEqual(self.project.page(1)["ai_reviews"], before["ai_reviews"])
        self.assertEqual(self.project.page(1)["refinement_reviews"], before["refinement_reviews"])
        self.editor.undo_edit()
        self.assertEqual(self.project.text(1), self.before)
        self.assertTrue(self.editor._write_recovery())
        reopened = ComparisonProject.load(self.editor._recovery_path)
        self.assertEqual([d["decision"] for d in reopened.page(1)["refinement_decisions"]], ["accepted", "undone"])

    def test_manual_change_blocks_stale_candidate_in_ui_and_core(self):
        self.generate()
        self.project.set_text(1, self.before[:4] + "。" + self.before[5:])
        self.editor._populate_page();self.select_first()
        self.assertTrue(self.panel.accept_button.instate(["disabled"]))
        previous = self.project.to_dict()
        self.panel.decide(True)
        self.assertEqual(self.project.to_dict(), previous)
        self.assertTrue(self.panel.current()["blocked_reason"])

    def fake_session(self, *, entered=None, release=None, cancelled=False):
        owner = self
        closed = []
        class Session:
            def __init__(self, engine, languages, **kwargs):
                self.engine, self.languages = engine, languages
                self.stop = kwargs.get("should_stop")
                self.root = kwargs.get("engine_root")
            def __enter__(self):
                return self
            def __exit__(self, *_args):
                closed.append(True)
            def condition_snapshot(self):
                return owner.conditions(self.engine, self.languages, self.root)
            def recognize_file(self, path, *, page_number, dpi, psm=None):
                if entered:
                    entered.set()
                if release:
                    release.wait(3)
                if cancelled and self.stop and self.stop():
                    raise ocr.OCRCancelled("synthetic cancellation")
                with Image.open(path) as image:
                    width, height = image.size
                conditions = self.condition_snapshot()
                metadata = {"generation_complete": True, "engine_root": conditions["engine_root"],
                            "session_conditions": conditions["pin"], "device_requested": conditions["device"],
                            "actual_size_vram": 1024 if conditions["device"] == "cuda" else 0}
                return ocr.RecognitionResult(text=owner.after, words=[], source_path=str(path),
                    source_sha256=ocr.file_sha256(path), page_number=page_number, image_width=width,
                    image_height=height, engine_version="synthetic", languages=self.languages,
                    psm=psm, duration_seconds=0, engine=self.engine,
                    engine_metadata=metadata)
        return Session, closed

    def test_selected_roi_planning_and_recognition_run_off_ui_thread(self):
        self.generate();before = self.project.page(1)["runs"]
        self.panel.engine.set("glmocr")
        threads = []
        implementation = refinement.plan_region
        def plan(*args, **kwargs):
            threads.append(threading.get_ident())
            return implementation(*args, **kwargs)
        session, closed = self.fake_session()
        with mock.patch.object(refinement, "plan_region", side_effect=plan), \
                mock.patch("ab_ocr.local_engines.RecognitionSession", session):
            self.panel.reread()
            self.pump(lambda: not self.editor.busy)
        self.assertEqual(closed, [True])
        self.assertNotEqual(threads, [threading.get_ident()])
        record = self.project.page(1)["region_readings"][0]
        self.assertEqual(record["status"], "completed")
        self.assertEqual(record["plan"]["start"], 4)
        self.assertEqual(record["plan"]["bbox"], [0, 8, 262, 67])
        self.assertEqual(self.project.page(1)["runs"], before)
        self.assertEqual(self.project.page(1)["edits"], [])
        self.assertEqual(self.project.text(1), self.before)
        self.assertEqual(ComparisonProject.load(self.editor._recovery_path).page(1)["region_readings"], [record])

    def test_cancel_roi_retains_cancelled_record_closes_session_and_preserves_document(self):
        self.generate();self.panel.engine.set("glmocr")
        entered, release = threading.Event(), threading.Event()
        session, closed = self.fake_session(entered=entered, release=release, cancelled=True)
        with mock.patch("ab_ocr.local_engines.RecognitionSession", session):
            self.panel.reread()
            self.assertTrue(entered.wait(3))
            self.assertTrue(self.editor.busy)
            self.assertTrue(self.panel.region_button.instate(["disabled"]))
            self.editor.cancel();release.set()
            self.pump(lambda: not self.editor.busy)
        self.assertEqual(closed, [True])
        self.assertEqual(self.project.page(1)["region_readings"][0]["status"], "cancelled")
        self.assertEqual(self.project.text(1), self.before)
        self.assertEqual(self.project.page(1)["edits"], [])
        self.assertEqual(ComparisonProject.load(self.editor._recovery_path).page(1)["region_readings"][0]["status"], "cancelled")

    def test_invalid_short_selection_starts_no_recognition_and_closes_lazy_session(self):
        self.readings()
        self.editor.text.tag_add("sel", "1.0", "1.0+1c")
        with mock.patch("ab_ocr.local_engines.RecognitionSession") as session:
            self.panel.reread()
            self.pump(lambda: not self.editor.busy)
        session.return_value.__enter__.return_value.recognize_file.assert_not_called()
        session.return_value.__exit__.assert_called_once()
        self.assertNotIn("region_readings", self.project.page(1))
        self.assertEqual(self.project.page(1)["edits"], [])

    def test_automatic_difficult_regions_share_one_session_and_checkpoint_two_raws(self):
        self.before = "甲乙丙丁戊己庚辛壬癸子丑寅卯辰巳午未申酉戌亥"
        self.after = self.before[:4] + "神" + self.before[5:16] + "𠮷" + self.before[17:]
        self.project.add_result(replace(self.raw("glmocr", self.before), words=[],
            engine_metadata={"generation_complete": True, "bbox_granularity": "none"}))
        self.project.add_result(self.raw("easyocr", self.after))
        self.project.add_result(self.raw("tesseract", self.before))
        self.project.choose_baseline(1, "glmocr");self.editor._populate_page()
        self.panel.engine.set("glmocr")
        original = self.project.page(1)["runs"]
        session, closed = self.fake_session()
        with mock.patch("ab_ocr.local_engines.RecognitionSession", session):
            self.panel.reread(difficult=True)
            self.pump(lambda: not self.editor.busy)
        self.assertEqual(closed, [True])
        records = self.project.page(1)["region_readings"]
        self.assertEqual(len(records), 2)
        self.assertTrue(all(r["status"] == "completed" for r in records))
        self.assertEqual([r["plan"]["start"] for r in records], [4, 16])
        self.assertEqual(self.project.page(1)["runs"], original)
        self.assertEqual(self.project.page(1)["edits"], [])
        reopened = ComparisonProject.load(self.editor._recovery_path)
        self.assertEqual(reopened.page(1)["region_readings"], records)

    def test_automatic_selector_with_no_han_disagreement_opens_no_session(self):
        self.readings()
        with mock.patch("ab_ocr.local_engines.RecognitionSession") as session:
            self.panel.reread(difficult=True)
            self.pump(lambda: not self.editor.busy)
        session.assert_not_called()
        self.assertNotIn("region_readings", self.project.page(1))
        self.assertEqual(self.project.text(1), self.before)


if __name__ == "__main__":
    unittest.main()
