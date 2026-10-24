"""AI receives unconfirmed evidence while old and human histories stay separate."""
import copy
import json
import unittest

from tests import test_comparison_refinement as fixture
from tests import test_comparison_ai as ai_fixture
from yeone_ocr.comparison import ComparisonProject, ComparisonError
from yeone_ocr.comparison_ai import AISettings, ReviewPolicy, prepare_plan, run_plan, AIReviewError
from yeone_ocr.comparison_refinement import (build_candidates, plan_region, run_region,
                                           build_region_candidates, accept_proposal)


class AIRefinementTests(unittest.TestCase):
    setUp = fixture.RefinementTests.setUp
    add = fixture.RefinementTests.add
    session = fixture.RefinementTests.session
    conditions = fixture.RefinementTests.conditions
    response = staticmethod(ai_fixture.PipelineTests.response)
    prompt_data = staticmethod(ai_fixture.PipelineTests.prompt_data)
    client = ai_fixture.PipelineTests.client

    def prepare(self, *, regional=False, include_refinement=True):
        baseline = self.text[:4] + "," + self.text[4:]
        self.add("glmocr", baseline)
        self.add("easyocr", baseline.replace(",", "，"))
        self.add("surya", baseline)
        self.project.choose_baseline(1, "glmocr")
        build_candidates(self.project, 1)
        if regional:
            region = plan_region(self.project, 1, 4, 5, "glmocr")
            run_region(self.project, region, session=self.session(text=baseline.replace(",", "，")))
            build_region_candidates(self.project, 1)
        policy = ReviewPolicy(visual_scope="page", punctuation=True)
        settings = AISettings("openai", "synthetic-only", max_calls=20)
        return prepare_plan(self.project, [1], policy, settings=settings,
                            include_refinement=include_refinement)

    def test_pending_local_evidence_is_transmitted_without_human_acceptance(self):
        plan = self.prepare()
        before = copy.deepcopy(self.project.page(1))
        def answer(data, _kwargs):
            context = data["local_refinement_unconfirmed"]
            self.assertFalse(context["human_accepted"])
            self.assertTrue(context["proposals"])
            self.assertEqual(context["proposals"][0]["before"], ",")
            self.assertEqual(context["proposals"][0]["after"], "，")
            self.assertEqual(context["proposals"][0]["bbox"], [20, 30, 220, 80])
            return self.response(data)
        client, calls = self.client(answer)
        result = run_plan(self.project, plan, api_key="synthetic", client_factory=lambda *_: client)
        self.assertEqual((result["failed"], len(calls)), (0, 2))
        self.assertEqual(self.project.ai_proposals(1), [])
        page = self.project.page(1)
        for field in ("runs", "corrected_text", "edits", "refinement_reviews", "refinement_decisions", "ai_decisions"):
            self.assertEqual(page[field], before[field])
        path = self.folder / "ai-refinement.json"
        self.project.save(path)
        self.assertEqual(ComparisonProject.load(path).page(1)["ai_reviews"], page["ai_reviews"])

    def test_region_image_is_frozen_and_rerendered_from_original(self):
        plan = self.prepare(regional=True)
        self.assertTrue(any(target.get("supplemental_images") for batch in plan.batches for target in batch["targets"]))
        def answer(data, kwargs):
            regions = data["local_refinement_unconfirmed"]["region_readings"]
            self.assertTrue(regions)
            self.assertTrue(regions[0]["image_ids"])
            image_count = sum(part.get("type") == "input_image" for part in kwargs["input"][0]["content"])
            self.assertGreaterEqual(image_count, 2)
            return self.response(data)
        client, _calls = self.client(answer)
        result = run_plan(self.project, plan, api_key="synthetic", client_factory=lambda *_: client)
        self.assertEqual(result["failed"], 0)

    def test_changed_pending_context_stops_before_provider_creation(self):
        plan = self.prepare()
        item = self.project.page(1)["refinement_reviews"][0]["proposals"][0]
        review = self.project.page(1)["refinement_reviews"][0]
        accept_proposal(self.project, 1, review["id"], item["id"])
        def forbidden(*_args):
            self.fail("provider created for a changed snapshot")
        with self.assertRaises((AIReviewError, ComparisonError)):
            run_plan(self.project, plan, api_key="synthetic", client_factory=forbidden)

    def test_explicit_page_scope_does_not_silently_shrink_to_disagreements(self):
        plan = self.prepare(include_refinement=False)
        visual = [batch for batch in plan.batches if batch["phase"] == "visual"]
        self.assertEqual(visual[0]["targets"][0]["before"], self.project.text(1))
        self.assertNotIn("refinement_evidence", visual[0])
        self.assertEqual(plan.summary["visual_scope"], "page")

    def test_tampered_evidence_is_not_accepted_even_with_saved_project(self):
        plan = self.prepare()
        client, _ = self.client()
        run_plan(self.project, plan, api_key="synthetic", client_factory=lambda *_: client)
        path = self.folder / "tampered.json"
        self.project.save(path)
        value = json.loads(path.read_text(encoding="utf-8"))
        request = value["pages"]["1"]["ai_reviews"][0]["request"]
        request["refinement_evidence"]["proposals"][0]["after"] = "錯"
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        with self.assertRaises(ComparisonError):
            ComparisonProject.load(path)


if __name__ == "__main__":
    unittest.main()
