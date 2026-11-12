"""Selective local-first review uses synthetic pixels and fake API clients only."""
import copy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from PIL import Image, ImageDraw
from ab_ocr.ocr import RecognitionResult
from yeone_ocr.comparison import ComparisonProject
from yeone_ocr.comparison_ai import AIReviewError, AISettings, ReviewPolicy, prepare_plan, run_plan


class SelectiveReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "synthetic.png"
        self.make_image()
        self.project = ComparisonProject.open(self.source)
        self.settings = AISettings("openai", "synthetic-model")

    def make_image(self, ink=False):
        with Image.new("RGB", (400, 200), "white") as image:
            if ink:
                ImageDraw.Draw(image).rectangle((40, 20, 90, 45), fill="black")
            image.save(self.source)

    def add(self, engine="ppocrv6", text="甲乙丙", confidence=99.0, *, words=None, dpi=300, active=True):
        words = [{"text": text, "bbox": [40, 20, 90, 45], "confidence": confidence}] if words is None else words
        raw = RecognitionResult(text, words, str(self.source), self.project.source_sha256,
                                1, 400, 200, "synthetic", "chi_tra", 6 if engine == "tesseract" else None,
                                0, engine=engine)
        return self.project.add_result(raw, activate=active, request={"profile": "traditional", "direction": "horizontal",
              "dpi": dpi, "engine_root": None, "policy": "local-three-engine-v1"})

    def plan(self, **kwargs):
        policy = kwargs.pop("policy", ReviewPolicy(selective=True))
        return prepare_plan(self.project, [1], policy, settings=self.settings, **kwargs)

    def fake_run(self, plan, patches=()):
        calls = []
        def request(**kwargs):
            calls.append(kwargs)
            prompt = kwargs["input"][0]["content"][0]["text"]
            data = json.loads(prompt.split("Source DATA (JSON):\n")[1].rsplit("\nReturn JSON", 1)[0])
            items = [{"target_id": target["id"], "uncertain": False, "note": "synthetic",
                      "proposals": list(patches)} for target in data["targets"]]
            return SimpleNamespace(output_text=json.dumps({"items": items}), status="completed", usage=None)
        client = SimpleNamespace(responses=SimpleNamespace(create=request), close=Mock())
        result = run_plan(self.project, plan, api_key="synthetic-key", client_factory=lambda *_: client)
        return result, calls

    def test_clean_single_v6_has_zero_calls_even_with_editorial_policy(self):
        self.add()
        plan = self.plan(policy=ReviewPolicy(selective=True, traditional=True, punctuation=True))
        self.assertEqual(plan.summary["calls"], 0)
        self.assertEqual(plan.summary["targets"], 0)
        forbidden = Mock(side_effect=AssertionError("No API for a clean reading"))
        outcome = run_plan(self.project, plan, api_key="", client_factory=forbidden)
        self.assertEqual(outcome["calls"], 0)
        forbidden.assert_not_called()

    def test_single_v6_low_confidence_crop_and_context(self):
        words = [{"text": "甲", "bbox": [40, 20, 55, 45], "confidence": 99.0},
                 {"text": "乙", "bbox": [55, 20, 70, 45], "confidence": 40.0},
                 {"text": "丙", "bbox": [70, 20, 90, 45], "confidence": 99.0}]
        self.add(words=words)
        plan = self.plan()
        target = plan.batches[0]["targets"][0]
        self.assertEqual((target["before"], target["context_before"], target["context_after"]), ("乙", "甲", "丙"))
        self.assertEqual(plan.summary["difficulty_reasons"], {"low-confidence-ppocrv6": 1})
        self.assertEqual(plan.summary["full_page_images"], 0)
        self.assertEqual(plan.batches[0]["images"][0]["source_bbox"], [43, 8, 82, 57])

    def test_clean_agreement_does_not_trigger_whole_page_review(self):
        self.add()
        self.add("tesseract", confidence=95)
        self.project.compare_page(1, baseline="ppocrv6")
        plan = self.plan()
        self.assertEqual(plan.summary["calls"], 0)
        self.assertEqual(plan.summary["dense_pages"], [])

    def test_engine_specific_confidence_thresholds(self):
        self.add("tesseract", confidence=60)
        self.assertEqual(self.plan().summary["difficulty_reasons"], {"low-confidence-tesseract": 1})
        self.add("tesseract", confidence=90)
        self.assertEqual(self.plan().summary["calls"], 0)

    def test_worker_scale_confidence_reaches_low_confidence_routing(self):
        """Adapters store word confidence on 0–100. A model score of 0.40 (0.30 for
        EasyOCR) as the real worker serializers emit it must be routed, and a
        confident line must not."""
        from ab_ocr.easyocr_worker import convert_results
        from ab_ocr.rapidocr_worker import serialize_lines
        from yeone_ocr.comparison_ai import LOW_CONFIDENCE_BELOW
        box = [[40, 20], [90, 20], [90, 45], [40, 45]]
        for engine, low, high in (("ppocrv6", .40, .99), ("rapidocr", .40, .99), ("easyocr", .30, .99)):
            for score, expected in ((low, {"low-confidence-" + engine: 1}), (high, {})):
                with self.subTest(engine=engine, score=score):
                    if engine == "easyocr":
                        _text, words = convert_results([(box, "甲乙丙", score)], (400, 200))
                    else:
                        _text, words = serialize_lines([box], ["甲乙丙"], [score])
                    self.assertEqual(words[0]["confidence"], score * 100)
                    self.project = ComparisonProject.open(self.source)          # one engine alone is the baseline
                    self.add(engine, words=[{"text": "甲乙丙", "bbox": [40, 20, 90, 45], "confidence": words[0]["confidence"]}])
                    self.assertEqual(self.plan().summary["difficulty_reasons"], expected)
        for engine, threshold in LOW_CONFIDENCE_BELOW.items():                  # one scale for every adapter
            self.assertTrue(threshold is None or 1 < threshold <= 100, engine)

    def test_whitespace_only_disagreement_is_not_sent_automatically(self):
        """Two engines that agree on every character and differ only in line
        breaks are not a paid target; a person can still select that item."""
        self.add(text="甲乙\n丙丁", words=[{"text": "甲乙", "bbox": [40, 20, 90, 45], "confidence": 99.0},
                                           {"text": "丙丁", "bbox": [40, 60, 90, 85], "confidence": 99.0}])
        self.add("rapidocr", text="甲乙丙丁", words=[{"text": "甲乙丙丁", "bbox": [40, 20, 140, 45], "confidence": 99.0}])
        self.project.compare_page(1, baseline="ppocrv6")
        suggestions = self.project.page(1)["suggestions"]
        self.assertTrue(suggestions)
        self.assertEqual({item["kind"] for item in suggestions}, {"whitespace"})
        plan = self.plan()
        self.assertEqual((plan.summary["targets"], plan.summary["calls"]), (0, 0))
        self.assertEqual(plan.summary["unlocalized_targets"], {})               # not an unresolved page either
        chosen = self.plan(suggestion_ids=[suggestions[0]["id"]])
        self.assertEqual(chosen.summary["difficulty_reasons"], {"engine-disagreement": 1})

    def test_unknown_character_with_high_confidence_is_localized(self):
        self.add(text="甲\ufffd乙")
        self.assertEqual(self.plan().summary["difficulty_reasons"], {"unknown-glyph": 1})

    def test_null_score_routes_to_review(self):
        self.add(confidence=None)
        self.assertEqual(self.plan().summary["difficulty_reasons"], {"confidence-unavailable": 1})

    def test_explicit_unknown_markers_route_to_local_review(self):
        for text in ("甲□乙", "甲〔판독불명〕乙"):
            with self.subTest(text=text):
                self.add(text=text)
                self.assertEqual(self.plan().summary["difficulty_reasons"], {"unknown-glyph": 1})

    def test_changed_scheduling_caps_reuse_completed_selective_review(self):
        self.add(confidence=40.0)
        plan = self.plan()
        outcome, calls = self.fake_run(plan)
        self.assertEqual((outcome["completed"], len(calls)), (1, 1))
        other = AISettings("openai", "synthetic-model", max_calls=7,
                           max_total_output_tokens=7 * 4096)
        resumed = prepare_plan(self.project, [1], ReviewPolicy(selective=True), settings=other)
        self.assertEqual(resumed.fingerprint, plan.fingerprint)
        self.assertEqual(resumed.summary["resumed_batches"], 1)
        self.assertEqual(resumed.summary["calls"], 0)
        self.assertNotEqual(resumed._integrity, plan._integrity)

    def test_compatible_subset_excludes_other_dpi_and_inactive_history(self):
        self.add(confidence=40.0)
        self.add("tesseract", confidence=90, dpi=150)
        self.add("easyocr", active=False)
        plan = self.plan()
        self.assertEqual([r["engine"] for r in plan.batches[0]["runs"]], ["ppocrv6"])
        outcome, calls = self.fake_run(plan, [{"start": 1, "end": 2, "before": "乙", "after": "仁",
                "kind": "recognition", "reason": "synthetic pixels", "uncertain": False}])
        self.assertEqual((outcome["completed"], len(calls)), (1, 1))
        proposal = self.project.ai_proposals(1)[0]
        self.assertFalse(proposal["stale"])
        self.assertFalse(proposal["applicable"])
        self.assertTrue(proposal["source_fidelity_blocked"])  # One family cannot authorize a Han substitution.
        path = self.root / "review.json"
        self.project.save(path)
        loaded = ComparisonProject.load(path)
        self.assertFalse(loaded.ai_proposals(1)[0]["stale"])
        self.assertTrue(loaded.ai_proposals(1)[0]["source_fidelity_blocked"])
        self.assertEqual(loaded.text(1), "甲乙丙")
        self.add("tesseract", confidence=95, dpi=300)
        self.assertTrue(self.project.ai_proposals(1)[0]["stale"])

    def test_new_current_reading_invalidates_frozen_plan(self):
        self.add(confidence=40.0)
        plan = self.plan()
        self.add(confidence=30.0)
        with self.assertRaises(AIReviewError):
            self.fake_run(plan)

    def test_stale_zero_call_plan_is_checked(self):
        self.add()
        plan = self.plan()
        self.add(text="甲乙丁")
        with self.assertRaises(AIReviewError):
            run_plan(self.project, plan, api_key="")

    def test_blank_empty_ocr_has_no_call(self):
        self.add(text="", words=[])
        self.assertEqual(self.plan().summary["calls"], 0)

    def test_missed_nonblank_ocr_is_localized_and_auditable(self):
        self.make_image(ink=True)
        self.project = ComparisonProject.open(self.source)
        self.add(text="", words=[])
        plan = self.plan()
        self.assertEqual(plan.summary["difficulty_reasons"], {"empty-ocr-with-ink": 1})
        self.assertEqual(plan.batches[0]["targets"][0]["bbox"], [40, 20, 91, 46])
        self.assertEqual(plan.summary["full_page_images"], 0)
        outcome, _ = self.fake_run(plan, [{"start": 0, "end": 0, "before": "", "after": "原文",
               "kind": "recognition", "reason": "synthetic pixels", "uncertain": False}])
        self.assertEqual(outcome["completed"], 1)
        self.assertEqual(self.project.text(1), "")
        self.assertFalse(self.project.ai_proposals(1)[0]["applicable"])
        self.assertTrue(self.project.ai_proposals(1)[0]["source_fidelity_blocked"])

    def test_missing_geometry_uses_only_unambiguous_ink_band(self):
        self.make_image(ink=True)
        self.project = ComparisonProject.open(self.source)
        self.add(words=[])
        plan = self.plan()
        self.assertEqual(plan.summary["difficulty_reasons"], {"missing-ocr-geometry": 1})
        self.assertIsNotNone(plan.batches[0]["targets"][0]["bbox"])

    def test_high_confidence_line_does_not_hide_a_missed_ink_line(self):
        with Image.new("RGB", (400, 200), "white") as image:
            draw = ImageDraw.Draw(image)
            draw.rectangle((40, 20, 90, 45), fill="black")
            draw.rectangle((40, 90, 90, 115), fill="black")
            image.save(self.source)
        self.project = ComparisonProject.open(self.source)
        self.add()
        plan = self.plan()
        self.assertEqual(plan.summary["difficulty_reasons"], {"unrecognized-ink-region": 1})
        target = plan.batches[0]["targets"][0]
        self.assertEqual((target["start"], target["end"], target["bbox"]), (3, 3, [40, 90, 91, 116]))

    def test_editorial_phase_stays_in_difficult_span(self):
        words = [{"text": "甲", "bbox": [40, 20, 55, 45], "confidence": 99.0},
                 {"text": "汉", "bbox": [55, 20, 70, 45], "confidence": 40.0},
                 {"text": "丙", "bbox": [70, 20, 90, 45], "confidence": 99.0}]
        self.add(text="甲汉丙", words=words)
        plan = self.plan(policy=ReviewPolicy(selective=True, traditional=True))
        self.assertEqual(plan.summary["editorial_calls"], 1)
        target = next(b for b in plan.batches if b["phase"] == "editorial")["targets"][0]
        self.assertEqual((target["start"], target["end"], target["before"]), (1, 2, "汉"))

    def test_explicit_disagreement_still_reviewed_at_high_confidence(self):
        self.add()
        self.add("rapidocr", text="甲仁丙")
        self.project.compare_page(1, baseline="ppocrv6")
        plan = self.plan()
        self.assertEqual(plan.summary["difficulty_reasons"], {"engine-disagreement": 1})
        self.assertEqual(plan.summary["full_page_images"], 0)

    def test_selective_policy_requires_boolean(self):
        with self.assertRaises(AIReviewError):
            ReviewPolicy(selective=1)

    def test_dense_disagreements_never_escalate_to_full_page(self):
        text = "\n".join(chr(0x4e00 + i) + "ABCDE" for i in range(30))
        other = "\n".join(chr(0x4f00 + i) + "ABCDE" for i in range(30))
        def words(value):
            return [{"text": line, "bbox": [40, 5 + i * 5, 110, 9 + i * 5], "confidence": 99.0}
                    for i, line in enumerate(value.splitlines())]
        self.add(text=text, words=words(text))
        self.add("rapidocr", text=other, words=words(other))
        self.project.compare_page(1, baseline="ppocrv6")
        plan = self.plan(policy=ReviewPolicy(selective=True, batch_size=4))
        self.assertGreater(plan.summary["targets"], 24)
        self.assertEqual(plan.summary["dense_pages"], [])
        self.assertEqual(plan.summary["full_page_images"], 0)
        self.assertTrue(all(t["bbox"] is not None for b in plan.batches for t in b["targets"]))


if __name__ == "__main__":
    unittest.main()
