"""Synthetic cross-engine integration: raw evidence, AI and human history stay separate."""
from argparse import Namespace
import copy
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from PIL import Image

from ab_ocr import ocr
from ab_ocr.engine_registry import ENGINES, LEGACY_ENGINES
from yeone_ocr.comparison import (ComparisonError, ComparisonProject, _eligibility,
                                 _models_unchanged, run_page, usable_runs)
from yeone_ocr.comparison_ai import (AIReviewError, AISettings, _page_scope,
                                    prepare_plan, run_plan)


class EngineIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(prefix="comparison-engines-")
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.source = self.root / "synthetic.png"
        Image.new("RGB", (200, 120), "white").save(self.source)
        self.project = ComparisonProject.open(self.source)

    def add(self, engine, text="甲", *, complete=True, profile="traditional", language="chi_tra", width=200):
        raw = ocr.RecognitionResult(
            text=text, words=[], source_path=str(self.source), source_sha256=self.project.source_sha256,
            page_number=1, image_width=width, image_height=120, engine_version="synthetic",
            languages=language, psm=6 if engine == "tesseract" else None, duration_seconds=0,
            engine=engine, engine_metadata={"generation_complete": complete}).to_dict()
        return self.project.add_result(raw, request={"profile": profile, "direction": "horizontal",
            "dpi": 300, "engine_root": None, "policy": "local-three-engine-v1"})

    def recognize(self, source, **kwargs):
        return ocr.RecognitionResult(text="新神𠮷한글。", words=[], source_path=str(source),
            source_sha256=self.project.source_sha256, page_number=kwargs["page_number"],
            image_width=200, image_height=120, engine_version="synthetic", languages=kwargs["languages"],
            psm=kwargs["psm"], duration_seconds=0, engine=kwargs["engine"],
            engine_metadata={"generation_complete": True})

    def save_load(self):
        path = self.root / "project.json"
        self.project.save(path)
        return ComparisonProject.load(path)

    def test_old_call_still_runs_only_legacy_three(self):
        with patch("yeone_ocr.comparison.ocr.recognize_file", side_effect=self.recognize) as recognize:
            result = run_page(self.project, 1)
        self.assertEqual(result["completed"], list(LEGACY_ENGINES))
        self.assertEqual([call.kwargs["engine"] for call in recognize.call_args_list], list(LEGACY_ENGINES))

    def test_explicit_model_set_and_primary_do_not_replace_human_text(self):
        with patch("yeone_ocr.comparison.ocr.recognize_file", side_effect=self.recognize) as recognize:
            result = run_page(self.project, 1, engines=("glmocr", "tesseract", "surya"), primary_engine="glmocr")
        self.assertEqual(result["completed"], ["glmocr", "tesseract", "surya"])
        self.assertEqual(self.project.page(1)["baseline_engine"], "glmocr")
        self.assertEqual([call.kwargs["languages"] for call in recognize.call_args_list], ["chi_tra"] * 3)
        self.assertEqual(self.project.page(1)["edits"], [])
        self.assertEqual(self.project.page(1)["ai_reviews"], [])
        self.assertIsNone(self.project.page(1)["corrected_text"])

    def test_primary_is_the_first_checkpoint_and_survives_cancel_reopen(self):
        stopped = [False]
        checkpoints = []
        path = self.root / "partial.json"
        def checkpoint(result):
            checkpoints.append((result.engine, self.project.page(1)["baseline_engine"]))
            self.project.save(path)
            stopped[0] = True
        with patch("yeone_ocr.comparison.ocr.recognize_file", side_effect=self.recognize) as recognize:
            result = run_page(self.project, 1, engines=ENGINES, primary_engine="glmocr",
                              should_stop=lambda: stopped[0], on_result=checkpoint)
        self.assertTrue(result["cancelled"])
        self.assertEqual(result["completed"], ["glmocr"])
        self.assertEqual(checkpoints, [("glmocr", "glmocr")])
        self.assertEqual([call.kwargs["engine"] for call in recognize.call_args_list], ["glmocr"])
        first_raw = copy.deepcopy(self.project.page(1)["runs"])
        reopened = ComparisonProject.load(path)
        reopened.compare_page(1)
        self.assertEqual(reopened.page(1)["baseline_engine"], "glmocr")
        with patch("yeone_ocr.comparison.ocr.recognize_file", side_effect=self.recognize):
            run_page(reopened, 1, engines=LEGACY_ENGINES)
        self.assertEqual(reopened.page(1)["baseline_engine"], "glmocr")
        self.assertEqual(reopened.page(1)["runs"][:1], first_raw)
        self.assertEqual(reopened.page(1)["edits"], [])
        self.assertEqual(reopened.page(1)["ai_reviews"], [])

    def test_mixed_language_routes_are_explicit(self):
        with patch("yeone_ocr.comparison.ocr.recognize_file", side_effect=self.recognize) as recognize:
            run_page(self.project, 1, profile="mixed", engines=("paddle_server", "glmocr", "surya"), primary_engine="glmocr")
        self.assertEqual({call.kwargs["engine"]: call.kwargs["languages"] for call in recognize.call_args_list},
                         {"paddle_server": "chi_tra", "glmocr": "kor+chi_tra", "surya": "kor+chi_tra"})

    def test_invalid_engine_scope_has_no_side_effects(self):
        for engines, primary in (((), None), (("glmocr", "glmocr"), None),
                                  (("remote",), None), (("glmocr",), "surya"), ("glmocr", None)):
            with self.subTest(engines=engines), patch("yeone_ocr.comparison.ocr.recognize_file") as recognize:
                with self.assertRaises(ComparisonError):
                    run_page(self.project, 1, engines=engines, primary_engine=primary)
                recognize.assert_not_called()
                self.assertEqual(self.project.page(1)["runs"], [])

    def test_existing_baseline_is_preserved_when_another_engine_is_added(self):
        self.add("tesseract", "原文")
        self.add("easyocr", "異文")
        self.project.choose_baseline(1, "tesseract")
        self.project.set_text(1, "人校神𠮷한글")
        before = self.project.page(1)
        with patch("yeone_ocr.comparison.ocr.recognize_file", side_effect=self.recognize):
            run_page(self.project, 1, engines=("glmocr",))
        after = self.project.page(1)
        self.assertEqual(after["baseline_run_id"], before["baseline_run_id"])
        self.assertEqual(after["edits"], before["edits"])
        self.assertEqual(after["corrected_text"], before["corrected_text"])
        self.assertEqual(after["runs"][:2], before["runs"])

    def test_legacy_v1_reopen_recompare_keeps_stored_baseline(self):
        self.add("tesseract", "原文")
        self.add("easyocr", "異文")
        self.project.compare_page(1, "tesseract")
        before = self.project.page(1)
        reopened = self.save_load()
        reopened.compare_page(1)
        self.assertEqual(reopened.page(1)["baseline_run_id"], before["baseline_run_id"])
        self.assertEqual(reopened.page(1)["runs"], before["runs"])
        self.assertEqual(reopened.page(1)["edits"], [])

    def test_incomplete_only_page_preserves_raw_without_a_false_baseline(self):
        identifier = self.add("glmocr", "未完神𠮷", complete=False)
        self.assertEqual(self.project.compare_page(1), [])
        page = self.project.page(1)
        self.assertEqual(page["runs"][0]["id"], identifier)
        self.assertIsNone(page["baseline_run_id"])
        self.assertEqual(self.project.text(1), "")
        self.assertEqual(usable_runs(page), {})
        self.assertEqual(self.save_load().page(1)["runs"], page["runs"])
        with self.assertRaises(ComparisonError):
            self.project.choose_baseline(1, "glmocr")

    def test_incomplete_recognition_checkpoints_raw_but_reports_failure(self):
        def incomplete(source, **kwargs):
            result = self.recognize(source, **kwargs)
            result.engine_metadata["generation_complete"] = False
            return result
        checkpoint = Mock()
        with patch("yeone_ocr.comparison.ocr.recognize_file", side_effect=incomplete):
            result = run_page(self.project, 1, engines=("glmocr",), primary_engine="glmocr", on_result=checkpoint)
        self.assertEqual(result["completed"], [])
        self.assertIn("glmocr", result["errors"])
        self.assertEqual(len(self.project.page(1)["runs"]), 1)
        self.assertIsNone(self.project.page(1)["baseline_run_id"])
        checkpoint.assert_called_once()
        self.save_load()

    def test_incomplete_addition_is_excluded_from_comparison_and_ai(self):
        for engine in LEGACY_ENGINES:
            self.add(engine, "甲" if engine != "easyocr" else "乙")
        self.project.choose_baseline(1, "tesseract")
        self.add("surya", "丙", complete=False)
        suggestions = self.project.compare_page(1)
        self.assertTrue(suggestions)
        self.assertTrue(all("surya" not in item["candidates"] for item in suggestions))
        excluded = self.project.page(1)["comparisons"][-1]["excluded_runs"]
        self.assertEqual([item["engine"] for item in excluded], ["surya"])
        self.assertEqual(list(usable_runs(self.project.page(1))), list(LEGACY_ENGINES))
        scope = _page_scope(self.project, 1)
        self.assertEqual([item["engine"] for item in scope["runs"]], list(LEGACY_ENGINES))
        self.save_load()

    def test_chinese_paddle_route_abstains_from_hangul_omission(self):
        self.add("tesseract", "한글漢字", profile="mixed", language="kor+chi_tra")
        self.add("paddle_server", "漢字", profile="mixed", language="chi_tra")
        suggestions = self.project.compare_page(1, "tesseract")
        evidence = next(e for item in suggestions for e in item["evidence"] if e["engine"] == "paddle_server")
        self.assertFalse(evidence["eligible"])
        self.assertIn("한글", evidence["eligibility_reason"])
        run = self.project.page(1)["runs"][1]
        self.assertTrue(_eligibility(run, "漢字", "漢字")[0])

    def test_new_models_can_form_the_three_engine_ai_scope(self):
        for engine in ("surya", "glmocr", "paddle_server"):
            self.add(engine)
        self.project.choose_baseline(1, "glmocr")
        self.project.compare_page(1)
        plan = prepare_plan(self.project, [1], settings=AISettings("openai", "synthetic"))
        expected = ["paddle_server", "glmocr", "surya"]
        self.assertEqual([r["engine"] for r in plan.batches[0]["runs"]], expected)
        self.assertEqual([c["engine"] for c in plan.batches[0]["targets"][0]["candidates"]], expected)
        self.assertEqual(self.project.page(1)["ai_reviews"], [])
        self.assertIsNone(self.project.page(1)["corrected_text"])

    def test_new_disagreement_candidates_follow_registry_order(self):
        for engine, text in (("surya", "丙"), ("glmocr", "甲"), ("paddle_server", "乙")):
            self.add(engine, text)
        self.project.compare_page(1, "glmocr")
        plan = prepare_plan(self.project, [1], settings=AISettings("openai", "synthetic"))
        self.assertEqual([c["engine"] for c in plan.batches[0]["targets"][0]["candidates"]],
                         ["paddle_server", "glmocr", "surya"])

    def test_single_completed_local_reading_is_ready_but_incomplete_and_unknown_are_not(self):
        self.add("tesseract")
        self.add("surya", complete=False)
        self.add("imported-ai")
        self.project.choose_baseline(1, "tesseract")
        self.assertEqual(list(usable_runs(self.project.page(1))), ["tesseract"])
        plan = prepare_plan(self.project, [1], settings=AISettings("openai", "synthetic"))
        self.assertEqual([r["engine"] for r in plan.batches[0]["runs"]], ["tesseract"])
        self.project = ComparisonProject.open(self.source)
        self.add("surya", complete=False)
        self.add("imported-ai")
        with self.assertRaises(AIReviewError):
            prepare_plan(self.project, [1], settings=AISettings("openai", "synthetic"))

    def test_each_registered_engine_can_be_the_only_current_ai_scope_reading(self):
        for engine in ENGINES:
            with self.subTest(engine=engine):
                self.project = ComparisonProject.open(self.source)
                identifier = self.add(engine)
                self.project.choose_baseline(1, engine)
                scope = _page_scope(self.project, 1)
                self.assertEqual(scope["baseline_run_id"], identifier)
                self.assertEqual([r["engine"] for r in scope["runs"]], [engine])

    def test_ai_scope_uses_registry_order_for_all_current_compatible_engines(self):
        for engine in reversed(ENGINES):
            self.add(engine)
        self.project.choose_baseline(1, "ppocrv6")
        self.assertEqual([r["engine"] for r in _page_scope(self.project, 1)["runs"]], list(ENGINES))

    def test_incompatible_extra_run_does_not_enter_ai_scope(self):
        for engine in LEGACY_ENGINES:
            self.add(engine)
        self.add("glmocr", width=300)
        self.project.choose_baseline(1, "tesseract")
        scope = _page_scope(self.project, 1)
        self.assertEqual([item["engine"] for item in scope["runs"]], list(LEGACY_ENGINES))

    def test_legacy_ai_page_scope_serialization_is_unchanged(self):
        for engine in LEGACY_ENGINES:
            self.add(engine)
        self.project.choose_baseline(1, "tesseract")
        page = self.project.page(1)
        runs = {run["engine"]: run for run in page["runs"]}
        baseline = runs["tesseract"]
        old = {"page_number": 1, "source_sha256": self.project.source_sha256,
               "snapshot_text": self.project.text(1),
               "snapshot_sha256": hashlib.sha256(self.project.text(1).encode("utf-8")).hexdigest(),
               "baseline_run_id": baseline["id"], "dpi": 300, "source_width": 200, "source_height": 120,
               "runs": [{"run_id": runs[e]["id"], "engine": e, "raw_sha256": runs[e]["raw_sha256"]} for e in LEGACY_ENGINES]}
        self.assertEqual(_page_scope(self.project, 1), old)

    def test_new_model_run_requires_runtime_provenance_to_resume(self):
        self.add("glmocr")
        self.assertFalse(_models_unchanged(self.project.page(1)["runs"][0], self.root))

    def test_new_pins_use_shared_installed_runtime_check(self):
        self.add("glmocr")
        run = self.project.page(1)["runs"][0]
        run["raw"]["engine_metadata"]["worker_sha256"] = "a" * 64
        with patch("ab_ocr.local_engines.installed_run_unchanged", return_value=False) as verify:
            self.assertFalse(_models_unchanged(run, self.root))
        verify.assert_called_once_with("glmocr", self.root, run["raw"]["engine_metadata"])

    def test_ai_proposal_acceptance_and_reopen_remain_separate_after_new_ocr(self):
        original = "天地玄黃甲宇宙洪荒"
        supported = "天地玄黃乙宇宙洪荒"
        for engine in LEGACY_ENGINES:
            self.add(engine, original if engine == "tesseract" else supported)
        self.project.choose_baseline(1, "tesseract")
        self.project.compare_page(1)
        before_raw = copy.deepcopy(self.project.page(1)["runs"])
        plan = prepare_plan(self.project, [1], settings=AISettings("openai", "synthetic"))
        def respond(**kwargs):
            prompt = kwargs["input"][0]["content"][0]["text"]
            data = json.loads(prompt.split("Source DATA (JSON):\n", 1)[1].rsplit("\nReturn JSON", 1)[0])
            items = [{"target_id": target["id"], "uncertain": False, "note": "synthetic",
                      "proposals": [{"start": 4, "end": 5, "before": "甲", "after": "乙",
                                     "kind": "recognition", "reason": "synthetic strokes", "uncertain": False}]}
                     for target in data["targets"]]
            return SimpleNamespace(output_text=json.dumps({"items": items}, ensure_ascii=False), status="completed",
                model="synthetic", id="fixture", usage=SimpleNamespace(input_tokens=1, output_tokens=1))
        client = SimpleNamespace(responses=SimpleNamespace(create=respond), close=Mock())
        outcome = run_plan(self.project, plan, api_key="synthetic-key", client_factory=lambda *_: client)
        self.assertEqual(outcome["failed"], 0)
        page = self.project.page(1)
        self.assertEqual(page["runs"], before_raw)
        self.assertIsNone(page["corrected_text"])
        self.assertEqual(page["edits"], [])
        review = page["ai_reviews"][0]
        proposal = review["proposals"][0]
        self.project.accept_ai_proposal(1, review["id"], proposal["id"])
        accepted = self.project.page(1)
        with patch("yeone_ocr.comparison.ocr.recognize_file", side_effect=self.recognize):
            run_page(self.project, 1, engines=("glmocr",))
        final = self.save_load().page(1)
        self.assertEqual(final["runs"][:3], before_raw)
        self.assertEqual(final["ai_reviews"], accepted["ai_reviews"])
        self.assertEqual(final["ai_decisions"], accepted["ai_decisions"])
        self.assertEqual(final["edits"], accepted["edits"])
        self.assertEqual(final["corrected_text"], supported)
        self.assertEqual(final["baseline_run_id"], accepted["baseline_run_id"])


class EngineCommandTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(prefix="engine-command-")
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.source = self.root / "synthetic.png"
        Image.new("RGB", (100, 100), "white").save(self.source)

    def args(self, **values):
        return Namespace(**dict(pdf=str(self.source), project=None, work=str(self.root / "work"),
            pages="1", profile="traditional", direction="horizontal", dpi=300, engine_root=None,
            force=False, **values))

    def test_old_namespace_without_engine_options_keeps_legacy_scope(self):
        from yeone_ocr.comparison_cli import command
        with patch("yeone_ocr.comparison.run_pages", return_value={"pages": {}, "errors": {}, "cancelled": False}) as run:
            self.assertEqual(command(self.args()), 0)
        self.assertEqual(run.call_args.kwargs["engines"], LEGACY_ENGINES)
        self.assertIsNone(run.call_args.kwargs["primary_engine"])

    def test_primary_cli_selects_measured_registry_engine_for_new_page(self):
        from yeone_ocr.comparison_cli import command
        with patch.dict("ab_ocr.engine_registry.PRIMARY_ENGINES", {"traditional": "glmocr"}):
            with patch("yeone_ocr.comparison.run_pages", return_value={"pages": {}, "errors": {}, "cancelled": False}) as run:
                self.assertEqual(command(self.args(engine_set="primary")), 0)
        self.assertEqual(run.call_args.kwargs["engines"], ("glmocr",))
        self.assertEqual(run.call_args.kwargs["primary_engine"], "glmocr")

    def test_parser_has_explicit_local_model_modes(self):
        from yeone_ocr.cli import build_parser
        args = build_parser().parse_args(["compare-engines", str(self.source), "--engine-set", "all", "--primary-engine", "glmocr"])
        self.assertEqual(args.engine_set, "all")
        self.assertEqual(args.primary_engine, "glmocr")
        old = build_parser().parse_args(["compare-engines", str(self.source)])
        self.assertIsNone(old.engine_set)  # No explicit set uses installed-engine automatic selection.
        self.assertIsNone(old.primary_engine)


if __name__ == "__main__":
    unittest.main()
