"""Public synthetic images and fake transports; no real provider calls or keys."""
import copy
import io
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from PIL import Image

from yeone_ocr.comparison import ComparisonProject, SourceChangedError
from yeone_ocr.comparison_ai import (AIReviewError, AISettings, ReviewPolicy,
    prepare_plan, run_plan, _validate_response)


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ai-pipeline-")
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.source = self.folder / "public-synthetic.png"
        with Image.new("RGB", (400, 200), "white") as image:
            image.putpixel((28, 8), (255, 0, 0))
            image.save(self.source)
        self.source_bytes = self.source.read_bytes()
        self.original = "误汉,樂一\ufe00한글"
        self.project = self.new_project()
        self.settings = AISettings("openai", "synthetic-vision", max_calls=20)

    def new_project(self, texts=None, languages=None):
        project = ComparisonProject.open(self.source)
        texts = texts or {"tesseract": self.original, "rapidocr": "论汉,樂一\ufe00한글", "easyocr": "论汉,樂一\ufe00한글"}
        for engine, text in texts.items():
            raw = {"kind": "recognition", "schema_version": 1, "engine": engine,
                "coordinate_system": "rendered-image pixels, xyxy", "text": text,
                "words": [{"text": text, "bbox": [40, 20, 90, 80], "confidence": 80,
                           "block": 1, "paragraph": 1, "line": 1}],
                "source_path": str(self.source), "source_sha256": project.source_sha256,
                "page_number": 1, "image_width": 400, "image_height": 200,
                "engine_version": "synthetic", "languages": (languages or {}).get(engine, "chi_tra"),
                "psm": 6 if engine == "tesseract" else None, "duration_seconds": 0,
                "preprocessing": {}, "engine_metadata": {}}
            project.add_result(raw, request={"profile": "traditional", "direction": "horizontal", "dpi": 300,
                                             "engine_root": None, "policy": "local-three-engine-v1"})
        project.compare_page(1, baseline="tesseract")
        return project

    def plan(self, policy=None, **kwargs):
        return prepare_plan(self.project, [1], policy, settings=self.settings, **kwargs)

    @staticmethod
    def prompt_data(kwargs):
        prompt = kwargs["input"][0]["content"][0]["text"]
        return json.loads(prompt.split("Source DATA (JSON):\n", 1)[1].rsplit("\nReturn JSON", 1)[0])

    @staticmethod
    def response(data, patches=(), *, uncertain=False):
        items = []
        for target in data["targets"]:
            proposals = [{"start": start, "end": end, "before": before, "after": after,
                          "kind": kind, "reason": "synthetic evidence", "uncertain": uncertain}
                         for start, end, before, after, kind in patches
                         if target["start"] <= start <= end <= target["end"]]
            items.append({"target_id": target["id"], "uncertain": uncertain,
                          "note": "public synthetic fixture", "proposals": proposals})
        return json.dumps({"items": items}, ensure_ascii=False)

    def client(self, operation=None):
        calls = []
        def request(**kwargs):
            calls.append(kwargs)
            data = self.prompt_data(kwargs)
            value = operation(data, kwargs) if operation else self.response(data)
            if not isinstance(value, str):
                return value
            return SimpleNamespace(output_text=value, status="completed", model="returned-synthetic-model", id="synthetic-response",
                                   usage=SimpleNamespace(input_tokens=12, output_tokens=8))
        return SimpleNamespace(responses=SimpleNamespace(create=request), close=Mock()), calls

    def run_fake(self, plan, operation=None, **kwargs):
        client, calls = self.client(operation)
        result = run_plan(self.project, plan, api_key="synthetic-key", client_factory=lambda *_: client, **kwargs)
        return result, calls

    def test_offline_plan_crop_geometry_and_explicit_region_scope(self):
        plan = self.plan()
        self.assertEqual(plan.summary["calls"], 1)
        self.assertEqual(plan.summary["editorial_calls"], 0)
        image = plan.batches[0]["images"][0]
        self.assertEqual(image["source_bbox"], [28, 8, 102, 92])
        self.assertEqual((image["width"], image["height"]), (74, 84))
        self.assertEqual(self.project.page(1)["ai_reviews"], [])
        def inspect(data, kwargs):
            import base64
            raw = base64.b64decode(kwargs["input"][0]["content"][1]["image_url"].split(",", 1)[1])
            with Image.open(io.BytesIO(raw)) as picture:
                self.assertEqual(picture.getpixel((0, 0)), (255, 0, 0))
            self.assertEqual(data["targets"][0]["before"], "误")
            self.assertNotIn("snapshot_text", data)
            return self.response(data)
        outcome, _ = self.run_fake(plan, inspect)
        self.assertEqual(outcome["completed"], 1)

    def test_two_phase_combined_proposals_preserve_raw_and_human_text(self):
        raw = self.project.page(1)["runs"]
        policy = ReviewPolicy(traditional=True, punctuation=True)
        plan = self.plan(policy)
        self.assertEqual(plan.summary["calls"], 2)
        self.assertEqual(plan.batches[1]["targets"][0]["before"], self.original)
        def answer(data, kwargs):
            self.assertEqual(kwargs["model"], self.settings.model)
            self.assertFalse(kwargs["store"])
            self.assertEqual(kwargs["max_output_tokens"], 4096)
            self.assertTrue(kwargs["text"]["format"]["strict"])
            if data["phase"] == "visual":
                return self.response(data, [(0, 1, "误", "论", "recognition")])
            self.assertEqual(data["visual_proposals_provisional"][0]["after"], "论")
            return self.response(data, [(0, 1, "误", "論", "traditional"),
                                         (1, 2, "汉", "漢", "traditional"), (2, 3, ",", "，", "punctuation")])
        checkpoints = []
        outcome, calls = self.run_fake(plan, answer, on_result=checkpoints.append)
        self.assertEqual((outcome["completed"], outcome["failed"], len(calls), len(checkpoints)), (2, 0, 2, 2))
        self.assertEqual(self.project.text(1), self.original)
        self.assertEqual(self.project.page(1)["runs"], raw)
        self.assertEqual(self.project.page(1)["edits"], [])
        reviews = self.project.page(1)["ai_reviews"]
        self.assertEqual(reviews[0]["metadata"]["actual_model"], "returned-synthetic-model")
        self.assertEqual(reviews[0]["metadata"]["usage"]["input_tokens"], 12)
        proposals = self.project.ai_proposals(1)
        self.assertEqual({p["after"] for p in proposals}, {"論", "漢", "，"})
        self.assertTrue(all(p["applicable"] for p in proposals))

    def test_model_uncertainty_is_readonly_and_raw_response_is_preserved(self):
        plan = self.plan()
        outcome, _ = self.run_fake(plan, lambda d, _: self.response(d, [(0, 1, "误", "论", "recognition")], uncertain=True))
        self.assertEqual(outcome["completed"], 1)
        proposal = self.project.ai_proposals(1)[0]
        self.assertTrue(proposal["uncertain"])
        self.assertFalse(proposal["applicable"])
        self.assertEqual(self.project.text(1), self.original)

    def test_malformed_or_ambiguous_response_is_audited_without_proposals(self):
        factories = [lambda d: '{"items":[],"items":[]}',
                     lambda d: '{"items":[NaN]}', lambda d: '{"items":[]}',
                     lambda d: json.dumps({"items": [{"target_id": d["targets"][0]["id"], "uncertain": False, "note": "fixture",
                        "proposals": [{"start": 0, "end": 99, "before": self.original, "after": "错", "kind": "recognition", "reason": "fixture", "uncertain": False}]}]}),
                     lambda d: self.response(d, [(0, 1, "wrong-before", "论", "recognition")])]
        for response in factories:
            with self.subTest(response=response):
                self.project = self.new_project()
                plan = self.plan()
                text = response({"targets": plan.batches[0]["targets"]})
                outcome, _ = self.run_fake(plan, lambda *_: text)
                self.assertEqual(outcome["failed"], 1)
                review = self.project.page(1)["ai_reviews"][-1]
                self.assertEqual(review["response_text"], text)
                self.assertEqual(review["proposals"], [])
                self.assertEqual(self.project.text(1), self.original)

    def test_duplicate_target_or_overlapping_patches_fail_closed(self):
        plan = self.plan()
        batch = plan.batches[0]
        value = json.loads(self.response({"targets": batch["targets"]}, [(0, 1, "误", "论", "recognition")]))
        value["items"].append(copy.deepcopy(value["items"][0]))
        with self.assertRaises(AIReviewError):
            _validate_response(json.dumps(value), batch, plan.policy)
        value["items"].pop()
        value["items"][0]["proposals"] *= 2
        with self.assertRaises(AIReviewError):
            _validate_response(json.dumps(value), batch, plan.policy)

    def test_incomplete_response_keeps_raw_answer_without_applicable_edits(self):
        raw = '{"items":[{"truncated":'
        outcome, _ = self.run_fake(self.plan(), lambda *_: SimpleNamespace(output_text=raw, status="incomplete", usage=None))
        self.assertEqual(outcome["failed"], 1)
        review = self.project.page(1)["ai_reviews"][0]
        self.assertEqual(review["response_text"], raw)
        self.assertEqual(review["proposals"], [])

    def test_completed_openai_refusal_is_preserved_and_never_accepted_as_verification(self):
        refusal = "Synthetic refusal answer retained exactly."
        response = SimpleNamespace(output_text="", status="completed", usage=None,
            output=[SimpleNamespace(content=[SimpleNamespace(type="refusal", refusal=refusal)])])
        outcome, calls = self.run_fake(self.plan(), lambda *_: response)
        self.assertEqual((outcome["failed"], len(calls)), (1, 1))
        review = self.project.page(1)["ai_reviews"][0]
        self.assertEqual(review["response_text"], refusal)
        self.assertTrue(review["metadata"]["refusal"])
        self.assertEqual(review["proposals"], [])
        self.assertEqual(self.project.text(1), self.original)

    def test_resume_uses_exact_policy_model_and_frozen_scope(self):
        plan = self.plan()
        self.run_fake(plan)
        resumed = self.plan()
        self.assertEqual(resumed.summary["calls"], 0)
        self.assertEqual(resumed.fingerprint, plan.fingerprint)
        self.assertEqual(self.plan(force=True).summary["calls"], 1)
        changed = prepare_plan(self.project, [1], settings=AISettings("openai", "another-selected-model"))
        self.assertNotEqual(changed.fingerprint, plan.fingerprint)
        self.assertEqual(changed.summary["calls"], 1)
        self.assertEqual(self.plan(ReviewPolicy(punctuation=True)).summary["calls"], 2)

    def test_rejected_visual_proposal_is_not_reused_as_editorial_resume_basis(self):
        policy = ReviewPolicy(traditional=True)
        plan = self.plan(policy)
        self.run_fake(plan, lambda data, _: self.response(data, [(0, 1, "误", "论", "recognition")]
                                                        if data["phase"] == "visual" else []))
        self.assertEqual(self.plan(policy).summary["calls"], 0)
        review = self.project.page(1)["ai_reviews"][0]
        proposal = review["proposals"][0]
        self.project.reject_ai_proposal(1, review["id"], proposal["id"])
        resumed = self.plan(policy)
        self.assertEqual(resumed.summary["calls"], 1)
        self.assertEqual(resumed.summary["editorial_lineage_refreshes"], 1)
        self.assertEqual(resumed.batches[0]["phase"], "editorial")
        def inspect(data, _):
            self.assertEqual(data["visual_proposals_provisional"], [])
            return self.response(data)
        outcome, calls = self.run_fake(resumed, inspect)
        self.assertEqual((outcome["completed"], len(calls)), (1, 1))
        self.assertEqual(self.project.page(1)["ai_reviews"][0], review)

    def test_forced_new_visual_then_cancel_requires_new_editorial_lineage(self):
        policy = ReviewPolicy(traditional=True)
        initial = self.plan(policy)
        def first_generation(data, _):
            return self.response(data, [(0, 1, "误", "论", "recognition")] if data["phase"] == "visual"
                                 else [(0, 1, "误", "論", "traditional")])
        self.run_fake(initial, first_generation)
        original_reviews = self.project.page(1)["ai_reviews"]
        self.assertEqual(self.plan(policy).summary["calls"], 0)
        forced = self.plan(policy, force=True)
        stop = threading.Event()
        outcome, calls = self.run_fake(forced, lambda d, _: self.response(d, [(0, 1, "误", "汉", "recognition")]),
                                       should_stop=stop.is_set, on_result=lambda _: stop.set())
        self.assertTrue(outcome["cancelled"])
        self.assertEqual(len(calls), 1)
        latest_visual = self.project.page(1)["ai_reviews"][-1]["proposals"][0]
        resumed = self.plan(policy)
        self.assertEqual(resumed.summary["calls"], 1)
        self.assertEqual(resumed.summary["editorial_lineage_refreshes"], 1)
        self.assertEqual(resumed.batches[0]["phase"], "editorial")
        def final_editorial(data, _):
            self.assertEqual([p["id"] for p in data["visual_proposals_provisional"]], [latest_visual["id"]])
            return self.response(data, [(0, 1, "误", "漢", "traditional")])
        result, _ = self.run_fake(resumed, final_editorial)
        self.assertEqual(result["completed"], 1)
        self.assertEqual(self.plan(policy).summary["calls"], 0)
        self.assertEqual(self.project.page(1)["ai_reviews"][:2], original_reviews)
        self.assertEqual(self.project.text(1), self.original)

    def test_latest_failed_force_attempt_is_retried_without_falling_back_to_old_completion(self):
        policy = ReviewPolicy(traditional=True)
        def completed(data, _):
            return self.response(data, [(0, 1, "误", "论", "recognition")] if data["phase"] == "visual" else [])
        self.run_fake(self.plan(policy), completed)
        class Forbidden(Exception):
            status_code = 403
        def failure(*_):
            raise Forbidden("synthetic-denied")
        outcome, _ = self.run_fake(self.plan(policy, force=True), failure)
        self.assertEqual(outcome["failed"], 1)
        resumed = self.plan(policy)
        self.assertEqual([b["phase"] for b in resumed.batches], ["visual", "editorial"])
        self.assertEqual(resumed.summary["calls"], 2)

    def test_scope_change_before_call_sends_nothing(self):
        plan = self.plan()
        self.project.set_text(1, self.original + "人工")
        factory = Mock()
        with self.assertRaises(AIReviewError):
            run_plan(self.project, plan, api_key="synthetic-key", client_factory=factory)
        factory.assert_not_called()

    def test_source_change_after_paid_answer_preserves_completed_response_and_stops(self):
        plan = self.plan(ReviewPolicy(punctuation=True))
        def answer(data, _):
            self.source.write_bytes(self.source_bytes + b"changed-source")
            return self.response(data)
        client, calls = self.client(answer)
        checkpoints = []
        with self.assertRaises(SourceChangedError):
            run_plan(self.project, plan, api_key="synthetic-key", client_factory=lambda *_: client, on_result=checkpoints.append)
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(checkpoints), 1)
        review = self.project.page(1)["ai_reviews"][0]
        self.assertEqual(review["status"], "completed")
        self.assertEqual(review["source_state_on_record"], "changed")
        self.project.save_recovery(self.folder / "changed-source-recovery.json")

    def test_manual_edit_during_answer_is_preserved_and_next_request_aborted(self):
        plan = self.plan(ReviewPolicy(punctuation=True))
        def answer(data, _):
            self.project.set_text(1, "사람이 새로 고친 문장")
            return self.response(data)
        client, calls = self.client(answer)
        with self.assertRaises(AIReviewError):
            run_plan(self.project, plan, api_key="synthetic-key", client_factory=lambda *_: client)
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.project.text(1), "사람이 새로 고친 문장")
        self.assertEqual(self.project.page(1)["ai_reviews"][0]["status"], "completed")

    def test_cancel_after_checkpoint_keeps_answer_and_avoids_next_call(self):
        plan = self.plan(ReviewPolicy(punctuation=True))
        stop = threading.Event()
        outcome, calls = self.run_fake(plan, should_stop=stop.is_set, on_result=lambda _: stop.set())
        self.assertTrue(outcome["cancelled"])
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.project.page(1)["ai_reviews"][0]["status"], "completed")

    def test_cancel_before_first_request_never_constructs_provider(self):
        plan = self.plan()
        factory = Mock()
        outcome = run_plan(self.project, plan, api_key="synthetic-key", should_stop=lambda: True, client_factory=factory)
        self.assertTrue(outcome["cancelled"])
        self.assertEqual(outcome["calls"], 0)
        factory.assert_not_called()

    def test_fatal_provider_failure_is_sanitized_and_not_retried(self):
        plan = self.plan(ReviewPolicy(punctuation=True))
        class Forbidden(Exception):
            status_code = 403
        def failure(*_):
            raise Forbidden("secret-key public-source-body must never reach an error log")
        outcome, calls = self.run_fake(plan, failure)
        self.assertEqual((outcome["calls"], outcome["failed"], len(calls)), (1, 1, 1))
        record = self.project.page(1)["ai_reviews"][0]
        serialized = json.dumps(record)
        self.assertNotIn("secret-key", serialized)
        self.assertNotIn("public-source-body", serialized)
        self.assertEqual(record["proposals"], [])

    def test_checkpoint_failure_keeps_answer_and_stops_remaining_requests(self):
        plan = self.plan(ReviewPolicy(punctuation=True))
        client, calls = self.client()
        def broken_checkpoint(_):
            raise OSError("synthetic disk failure")
        with self.assertRaises(OSError):
            run_plan(self.project, plan, api_key="synthetic-key", client_factory=lambda *_: client, on_result=broken_checkpoint)
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(self.project.page(1)["ai_reviews"]), 1)

    def test_budget_and_target_caps_reject_whole_plan_without_truncation(self):
        with self.assertRaises(AIReviewError):
            prepare_plan(self.project, [1], ReviewPolicy(punctuation=True), settings=AISettings("openai", "synthetic", max_calls=1))
        with self.assertRaises(AIReviewError):
            self.plan(ReviewPolicy(punctuation=True, max_targets=1))
        self.assertEqual(self.project.page(1)["ai_reviews"], [])

    def test_dense_265_differences_cover_entire_selected_page_with_bounded_chunks(self):
        spaced, compact = "甲 " * 265, "甲" * 265
        self.project = self.new_project({"tesseract": spaced, "rapidocr": compact, "easyocr": compact})
        self.assertEqual(len(self.project.page(1)["suggestions"]), 265)
        plan = self.plan(ReviewPolicy(traditional=True, punctuation=True))
        self.assertEqual(plan.summary["dense_pages"], [1])
        self.assertLessEqual(plan.summary["calls"], 20)
        visual = [t for b in plan.batches if b["phase"] == "visual" for t in b["targets"]]
        self.assertEqual("".join(t["before"] for t in visual), spaced)
        self.assertEqual({c["engine"] for t in visual for c in t["candidates"]}, {"tesseract", "rapidocr", "easyocr"})
        self.assertTrue(all(len(c["text"]) <= 800 for t in visual for c in t["candidates"]))
        self.assertEqual(self.project.page(1)["ai_reviews"], [])

    def test_human_insertions_force_safe_full_page_editorial_image(self):
        self.project.set_text(1, "新增" + self.original)
        plan = self.plan(ReviewPolicy(traditional=True))
        editorial = next(b for b in plan.batches if b["phase"] == "editorial")
        self.assertIsNone(editorial["targets"][0]["bbox"])
        self.assertEqual(editorial["images"][0]["source_bbox"], [0, 0, 400, 200])
        self.assertEqual(plan.batches[0]["targets"][0]["start"], 2)
        self.assertEqual(plan.summary["full_page_image_pages"], [1])
        self.assertEqual(plan.summary["full_page_images"], 1)

    def test_human_edit_inside_disagreement_uses_current_page_text_but_selected_scope_fails(self):
        identifier = self.project.page(1)["suggestions"][0]["id"]
        corrected = "人工确认" + self.original[1:]
        self.project.set_text(1, corrected)
        plan = self.plan(ReviewPolicy(traditional=True))
        visual = [t for b in plan.batches if b["phase"] == "visual" for t in b["targets"]]
        self.assertEqual("".join(t["before"] for t in visual), corrected)
        self.assertEqual(plan.summary["edited_text_fallback_pages"], [1])
        self.assertEqual(plan.summary["full_page_image_pages"], [1])
        self.assertTrue(plan.summary["fallback_reason"])
        with self.assertRaises(AIReviewError):
            self.plan(suggestion_ids=[identifier])

    def test_mutating_future_nested_plan_during_first_response_stops_further_transmission(self):
        plan = self.plan(ReviewPolicy(punctuation=True))
        def answer(data, _):
            plan.batches[1]["targets"][0]["before"] = "unapproved-future-context"
            return self.response(data)
        client, calls = self.client(answer)
        checkpoints = []
        with self.assertRaises(AIReviewError):
            run_plan(self.project, plan, api_key="synthetic-key", client_factory=lambda *_: client, on_result=checkpoints.append)
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(checkpoints), 1)
        self.assertEqual(self.project.page(1)["ai_reviews"][0]["status"], "completed")

    def test_provider_close_failure_cannot_hide_completed_answer(self):
        plan = self.plan()
        client, calls = self.client()
        client.close.side_effect = OSError("credential and body must not surface")
        outcome = run_plan(self.project, plan, api_key="synthetic-key", client_factory=lambda *_: client)
        self.assertEqual(outcome["completed"], 1)
        self.assertEqual(len(calls), 1)

    def test_explicit_selected_disagreement_never_expands_editorial_text_scope(self):
        suggestion = self.project.page(1)["suggestions"][0]
        plan = self.plan(ReviewPolicy(traditional=True), suggestion_ids=[suggestion["id"]])
        self.assertTrue(all(t["before"] == "误" for b in plan.batches for t in b["targets"]))
        with self.assertRaises(AIReviewError):
            self.plan(suggestion_ids=["nonexistent"])

    def test_tampered_plan_or_missing_explicit_key_is_rejected_before_provider(self):
        plan = self.plan()
        factory = Mock()
        with self.assertRaises(AIReviewError):
            run_plan(self.project, plan, api_key="", client_factory=factory)
        plan.batches[0]["targets"][0]["before"] = "changed-plan"
        with self.assertRaises(AIReviewError):
            run_plan(self.project, plan, api_key="synthetic-key", client_factory=factory)
        factory.assert_not_called()

    def test_editorial_rejects_general_rewrite_and_variant_folding(self):
        plan = self.plan(ReviewPolicy(traditional=True, punctuation=True))
        batch = next(b for b in plan.batches if b["phase"] == "editorial")
        for start, end, before, after, kind in ((0, 1, "误", "文章", "traditional"),
                (3, 4, "樂", "樂", "traditional"), (6, 8, "한글", "韓文", "traditional"),
                (1, 2, "汉", "", "punctuation")):
            with self.subTest(after=after), self.assertRaises(AIReviewError):
                _validate_response(self.response({"targets": batch["targets"]}, [(start, end, before, after, kind)]), batch, plan.policy)


if __name__ == "__main__":
    unittest.main()
