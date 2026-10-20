"""Fake SDK end-to-end source fidelity; no real API/GPU/private fixtures."""
import copy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from PIL import Image

from ab_ocr.ocr import RecognitionResult
from yeone_ocr.comparison import ComparisonError, ComparisonProject
from yeone_ocr.comparison_ai import AIReviewError, AISettings, ReviewPolicy, prepare_plan, run_plan, _validate_response


class SourceFidelityPipelineTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="source-fidelity-public-")
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        self.source = self.folder / "public.png"
        Image.new("RGB", (400, 200), "white").save(self.source)
        self.settings = AISettings("openai", "synthetic-source-fidelity")

    def project(self, original="天地玄黃甲宇宙洪荒，寒來暑往", replacement="乙", *, punctuation_witness=False,
                punctuation_witness_engines=None):
        project = ComparisonProject.open(self.source)
        for engine in ("glmocr", "easyocr", "surya"):
            text = original if engine == "glmocr" else original.replace("甲", replacement)
            if (punctuation_witness and engine != "glmocr"
                    and (punctuation_witness_engines is None or engine in punctuation_witness_engines)):
                text = text.replace("，", ",")
            raw = RecognitionResult(text=text, words=[] if engine == "glmocr" else [
                {"text": text, "bbox": [20, 20, 300, 90], "confidence": None}], source_path=str(self.source),
                source_sha256=project.source_sha256, page_number=1, image_width=400, image_height=200,
                engine_version="public synthetic", languages="chi_tra", psm=None, duration_seconds=0,
                engine=engine, engine_metadata={"generation_complete": True}).to_dict()
            project.add_result(raw, request={"profile": "traditional", "direction": "horizontal", "dpi": 300,
                "engine_root": None, "policy": "local-three-engine-v1"})
        project.compare_page(1, baseline="glmocr")
        return project

    @staticmethod
    def response(data, patches):
        return json.dumps({"items": [{"target_id": target["id"], "uncertain": False, "note": "public synthetic",
            "proposals": [{"start": start, "end": start + len(before), "before": before, "after": after,
                "kind": kind, "reason": "synthetic source comparison", "uncertain": False}
                for start, before, after, kind in patches if target["start"] <= start <= start + len(before) <= target["end"]]}
            for target in data["targets"]]}, ensure_ascii=False)

    def run_fake(self, project, answer, *, traditional=True, punctuation=True):
        policy = ReviewPolicy(traditional=traditional, punctuation=punctuation, visual_scope="page")
        plan = prepare_plan(project, [1], policy, settings=self.settings)
        responses, requests = [], []
        def request(**kwargs):
            prompt = kwargs["input"][0]["content"][0]["text"]
            data = json.loads(prompt.split("Source DATA (JSON):\n", 1)[1].rsplit("\nReturn JSON", 1)[0])
            self.assertNotIn("source_fidelity_evidence", data)
            self.assertEqual(data["source_fidelity_rules"]["minimum_other_families"], 2)
            patches = answer(data)
            text = self.response(data, patches)
            responses.append(text);requests.append(data)
            return SimpleNamespace(output_text=text, status="completed", model="public-synthetic-model")
        client = SimpleNamespace(responses=SimpleNamespace(create=request), close=Mock())
        result = run_plan(project, plan, api_key="synthetic-not-a-key", client_factory=lambda *_: client)
        return plan, result, responses, requests

    def test_compatibility_and_selector_risks_do_not_discard_good_punctuation(self):
        for glyph, changed in (("神", "神"), ("一\ufe00二", "一二\ufe00")):
            with self.subTest(glyph=glyph):
                original = "天地玄黃" + glyph + "宇宙洪荒，寒來暑往"
                project = self.project(original, punctuation_witness=True)
                raw = project.page(1)["runs"]
                def answer(data):
                    return [] if data["phase"] == "visual" else [
                        (4, glyph, changed, "traditional"), (original.index("，"), "，", ",", "punctuation")]
                _, outcome, responses, _ = self.run_fake(project, answer)
                self.assertEqual((outcome["completed"], outcome["failed"]), (2, 0))
                page = project.page(1)
                self.assertEqual([r["response_text"] for r in page["ai_reviews"]], responses)
                self.assertTrue(all(not p["uncertain"] for r in page["ai_reviews"] for p in r["proposals"]))
                views = project.ai_proposals(1)
                bad = next(p for p in views if p["after"] == changed)
                good = next(p for p in views if p["after"] == ",")
                self.assertFalse(bad["applicable"])
                self.assertTrue(good["applicable"])
                self.assertIn("protected_unicode_change", bad["source_fidelity"]["reason_codes"])
                with self.assertRaises(ComparisonError):
                    project.accept_ai_proposal(1, bad["review_id"], bad["id"])
                self.assertEqual(project.text(1), original)
                project.accept_ai_proposal(1, good["review_id"], good["id"])
                path = self.folder / ("project-" + str(len(glyph)) + ".json")
                project.save(path)
                reopened = ComparisonProject.load(path)
                self.assertEqual(reopened.text(1), original.replace("，", ","))
                self.assertEqual(reopened.page(1)["runs"], raw)
                self.assertEqual(len(reopened.page(1)["edits"]), 1)
                self.assertEqual([r["response_text"] for r in reopened.page(1)["ai_reviews"]], responses)

    def test_held_editorial_does_not_supersede_good_visual(self):
        project = self.project()
        def answer(data):
            if data["phase"] == "visual":
                return [(4, "甲", "乙", "recognition")]
            self.assertEqual(data["visual_proposals_provisional"][0]["after"], "乙")
            return [(4, "甲", "丙", "traditional")]
        _, outcome, _, _ = self.run_fake(project, answer)
        self.assertEqual(outcome["completed"], 2)
        views = project.ai_proposals(1)
        safe = next(p for p in views if p["after"] == "乙")
        held = next(p for p in views if p["after"] == "丙")
        self.assertTrue(safe["applicable"])
        self.assertFalse(safe["superseded"])
        self.assertFalse(held["applicable"])
        self.assertEqual(project.page(1)["ai_reviews"][1]["metadata"].get("superseded_proposal_ids", []), [])
        project.accept_ai_proposal(1, safe["review_id"], safe["id"])
        self.assertTrue(project.undo_edit(1))
        path = self.folder / "visual-and-held.json";project.save(path)
        self.assertEqual(ComparisonProject.load(path).text(1), "天地玄黃甲宇宙洪荒，寒來暑往")

    def test_held_visual_is_not_sent_as_editorial_provisional_text(self):
        original = "天地玄黃软宇宙洪荒，寒來暑往"
        project = self.project(original)
        def answer(data):
            if data["phase"] == "visual":
                return [(4, "软", "軟", "recognition")]
            self.assertEqual(data["visual_proposals_provisional"], [])
            return []
        _, outcome, _, _ = self.run_fake(project, answer)
        self.assertEqual(outcome["completed"], 2)
        self.assertFalse(project.ai_proposals(1)[0]["applicable"])

    def test_legacy_response_validation_remains_strict_and_unknown_new_contract_data_fails(self):
        original = "天地玄黃神宇宙洪荒，寒來暑往"
        project = self.project(original)
        plan = prepare_plan(project, [1], ReviewPolicy(visual_scope="page"), settings=self.settings)
        new_batch = plan.batches[0]
        text = self.response(new_batch, [(4, "神", "神", "recognition")])
        self.assertEqual(len(_validate_response(text, new_batch, plan.policy)), 1)
        old_batch = {key: value for key, value in new_batch.items() if not key.startswith("source_fidelity")}
        with self.assertRaises(AIReviewError):
            _validate_response(text, old_batch, plan.policy)
        for phase, kind in (("unknown", "recognition"), ("visual", "unknown")):
            with self.subTest(phase=phase, kind=kind), self.assertRaises(AIReviewError):
                batch = copy.deepcopy(new_batch);batch["phase"] = phase
                _validate_response(self.response(batch, [(4, "神", "神", kind)]), batch, plan.policy)
        bad = json.loads(text);bad["items"][0]["proposals"][0]["before"] = "wrong exact quote"
        with self.assertRaises(AIReviewError):
            _validate_response(json.dumps(bad), new_batch, plan.policy)

    def test_width_spelling_keeps_one_literal_and_two_glyph_witnesses_separate(self):
        original = "天地玄黃甲宇宙洪荒，寒來暑往"
        project = self.project(original, punctuation_witness=True, punctuation_witness_engines={"easyocr"})
        raw = project.page(1)["runs"]
        def answer(data):
            return [] if data["phase"] == "visual" else [(original.index("，"), "，", ",", "punctuation")]
        _, outcome, responses, _ = self.run_fake(project, answer, traditional=False)
        self.assertEqual((outcome["completed"], outcome["failed"]), (2, 0))
        proposal = project.ai_proposals(1)[0]
        self.assertTrue(proposal["applicable"])
        self.assertEqual(proposal["source_fidelity_contract"], "source-fidelity-v4")
        reviews = project.page(1)["ai_reviews"]
        self.assertEqual([review["response_text"] for review in reviews], responses)
        self.assertTrue(all(review["metadata"]["prompt_version"] == "comparison-ai-v12" for review in reviews))
        self.assertEqual(project.text(1), original)
        project.accept_ai_proposal(1, proposal["review_id"], proposal["id"])
        target = self.folder / "width-evidence-v3.json"
        project.save(target)
        loaded = ComparisonProject.load(target)
        self.assertEqual(loaded.text(1), original.replace("，", ","))
        self.assertEqual(loaded.page(1)["ai_reviews"], reviews)
        self.assertEqual(loaded.page(1)["runs"], raw)
        self.assertEqual(loaded.page(1)["ai_decisions"][-1]["source_fidelity_acceptance"]["attestation"]["contract"],
                         "source-fidelity-v4")
        self.assertTrue(loaded.undo_edit(1))
        loaded.save(target)
        self.assertEqual(ComparisonProject.load(target).text(1), original)


if __name__ == "__main__":
    unittest.main()
