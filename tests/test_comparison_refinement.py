"""Synthetic exact-anchor, history and image-coordinate refinement tests."""
import copy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock
from unittest import mock

from PIL import Image

from ab_ocr import ocr
from yeone_ocr.comparison import ComparisonError, ComparisonProject, SourceChangedError, _digest, _validate_anchor_evidence
from yeone_ocr.comparison_refinement import (
    accept_proposal, build_candidates, build_region_candidates, evaluate_proposals,
    plan_region, proposals, refinement_context, region_words, reject_proposal,
    region_targets, run_region, validate_ai_refinement_context,
)


class RefinementTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="refinement-synthetic-")
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        self.source = self.folder / "source.png"
        Image.new("RGB", (320, 160), "white").save(self.source)
        self.project = ComparisonProject.open(self.source)
        self.text = "甲乙丙丁戊己庚辛壬癸子丑寅卯"
        patcher = mock.patch("yeone_ocr.comparison_refinement._condition_snapshot", side_effect=self.conditions)
        patcher.start();self.addCleanup(patcher.stop)

    def conditions(self, engine="glmocr", languages="chi_tra", engine_root=None, device=None, should_stop=None):
        from ab_ocr.runtime import default_engine_root
        device = device or ("cuda" if engine in ("glmocr", "surya", "paddle_server") else "cpu")
        pin = {"installation": "a" * 64, "worker": "b" * 64, "protocol": "c" * 64,
               "runtime": {"count": 1, "sha256": "d" * 64},
               "models": {"synthetic": {"sha256": "e" * 64, "bytes": 1}},
               "device": device, "languages": languages}
        if engine == "tesseract":
            pin = {"executable_sha256": "f" * 64, "models": pin["models"], "device": "cpu", "languages": languages}
        return {"engine": engine, "languages": languages, "engine_root": str(default_engine_root(engine_root).resolve()),
                "device": device, "pin": pin}

    def add(self, engine, text, *, boxes=None, complete=True, language="chi_tra"):
        if boxes is None:
            boxes = [] if engine == "glmocr" else [{"text": text, "bbox": [20, 30, 220, 80], "confidence": None}]
        raw = ocr.RecognitionResult(text=text, words=boxes, source_path=str(self.source),
            source_sha256=self.project.source_sha256, page_number=1, image_width=320, image_height=160,
            engine_version="synthetic", languages=language, psm=6 if engine == "tesseract" else None,
            duration_seconds=0, engine=engine,
            engine_metadata={"generation_complete": complete, "bbox_granularity": "line"}).to_dict()
        return self.project.add_result(raw, request={"profile": "traditional", "direction": "horizontal",
            "dpi": 300, "engine_root": None, "policy": "local-three-engine-v1"})

    def han(self, replacement="神"):
        self.add("easyocr", self.text)
        self.add("glmocr", self.text[:4] + replacement + self.text[5:])
        self.project.choose_baseline(1, "easyocr")
        return build_candidates(self.project, 1)[0]

    def reopen(self):
        path = self.folder / "project.json"
        self.project.save(path)
        return ComparisonProject.load(path)

    def session(self, engine="glmocr", text=None, *, complete=True, words=False, during=None):
        def recognize(path, *, page_number, dpi, psm=None):
            if during:
                during()
            with Image.open(path) as image:
                width, height = image.size
            value = text or (self.text[:4] + "神" + self.text[5:])
            boxes = [{"text": value, "bbox": [0, 0, width, height], "confidence": None}] if words else []
            conditions = self.conditions(engine)
            metadata = {"generation_complete": complete, "session_conditions": conditions["pin"],
                        "engine_root": conditions["engine_root"]}
            if engine == "glmocr":
                metadata.update(device_requested=conditions["device"], actual_size_vram=1024 if conditions["device"] == "cuda" else 0)
            if engine == "surya":
                metadata["provider"] = conditions["device"].upper()
            return ocr.RecognitionResult(text=value, words=boxes, source_path=str(path),
                source_sha256=ocr.file_sha256(path), page_number=page_number, image_width=width,
                image_height=height, engine_version="synthetic", languages="chi_tra", psm=None,
                duration_seconds=0, engine=engine, engine_metadata=metadata)
        return SimpleNamespace(engine=engine, languages="chi_tra", recognize_file=Mock(side_effect=recognize),
                               condition_snapshot=Mock(return_value=self.conditions(engine)))

    def test_han_proposal_preserves_compatibility_glyph_and_does_not_edit(self):
        item = self.han("神");page = self.project.page(1)
        self.assertEqual((item["kind"], item["before"], item["after"]), ("han", "戊", "神"))
        self.assertFalse(proposals(self.project, 1)[0]["eligible"])
        self.assertEqual(page["edits"], [])
        self.assertIsNone(page["corrected_text"])
        self.assertEqual(item["evidence"]["engine"], "easyocr")
        self.assertEqual(item["evidence"]["bbox"], [20, 30, 220, 80])
        self.assertEqual(self.project.text(1), self.text)
        self.assertEqual(self.reopen().page(1)["refinement_reviews"], page["refinement_reviews"])

    def test_easy_punctuation_uses_exact_anchors_and_real_line_box(self):
        before, after = "甲乙丙丁,戊己庚辛", "甲乙丙丁，戊己庚辛"
        self.add("glmocr", before);self.add("easyocr", after)
        self.project.choose_baseline(1, "glmocr")
        item = build_candidates(self.project, 1)[0]
        self.assertEqual((item["kind"], item["before"], item["after"]), ("punctuation", ",", "，"))
        self.assertEqual(self.project.text(1), before)
        self.assertEqual(evaluate_proposals(self.project, 1)["text"], after)

    def test_no_glm_coordinate_invention_even_if_invalid_box_is_supplied(self):
        self.add("glmocr", self.text, boxes=[{"text": self.text, "bbox": [1, 1, 100, 50], "confidence": None}])
        with self.assertRaises(ComparisonError):
            plan_region(self.project, 1, 4, 5, "glmocr")

    def test_no_coordinate_witness_no_automatic_candidate(self):
        self.add("easyocr", self.text, boxes=[])
        self.add("glmocr", self.text[:4] + "神" + self.text[5:])
        self.project.choose_baseline(1, "easyocr")
        self.assertEqual(build_candidates(self.project, 1), [])

    def test_repeated_anchors_and_mixed_script_replacements_abstain(self):
        before = "甲乙丙丁戊己庚辛壬甲乙丙丁戊己庚辛壬"
        self.add("easyocr", before);self.add("glmocr", before.replace("戊", "神", 1))
        self.project.choose_baseline(1, "easyocr")
        self.assertEqual(build_candidates(self.project, 1), [])
        self.add("glmocr", before.replace("戊", "한", 1))
        self.assertEqual(build_candidates(self.project, 1), [])

    def test_prior_human_edit_inside_span_is_protected(self):
        self.han()
        self.project.set_text(1, self.text[:4] + "人" + self.text[5:])
        self.assertEqual(build_candidates(self.project, 1), [])
        self.assertTrue(all(not p["eligible"] for p in proposals(self.project, 1)))

    def test_human_edit_elsewhere_can_be_mapped_without_overwrite(self):
        item = self.han()
        self.project.set_text(1, "序" + self.text)
        result = accept_proposal(self.project, 1, item["review_id"], item["id"])
        self.assertEqual(result, "序" + self.text[:4] + "神" + self.text[5:])
        self.assertEqual(self.reopen().text(1), result)

    def test_accept_undo_reject_and_reopen_preserve_separate_histories(self):
        item = self.han();raw = self.project.page(1)["runs"]
        result = accept_proposal(self.project, 1, item["review_id"], item["id"])
        self.assertEqual(self.reopen().text(1), result)
        self.assertTrue(self.project.undo_edit(1))
        self.assertEqual(self.project.text(1), self.text)
        reject_proposal(self.project, 1, item["review_id"], item["id"])
        page = self.reopen().page(1)
        self.assertEqual([d["decision"] for d in page["refinement_decisions"]], ["accepted", "undone", "rejected"])
        self.assertEqual(page["runs"], raw)
        self.assertEqual(page["ai_reviews"], [])
        self.assertEqual(page["ai_decisions"], [])

    def test_changed_parent_run_and_changed_anchor_block_acceptance(self):
        item = self.han()
        self.add("glmocr", self.text[:4] + "神" + self.text[5:])
        with self.assertRaises(ComparisonError):
            accept_proposal(self.project, 1, item["review_id"], item["id"])
        self.assertFalse(proposals(self.project, 1)[0]["eligible"])

    def test_pure_simulation_has_no_project_mutation(self):
        self.han();before = self.project.to_dict()
        result = evaluate_proposals(self.project, 1)
        self.assertEqual(result["text"], self.text[:4] + "神" + self.text[5:])
        self.assertEqual(len(result["applied"]), 1)
        self.assertEqual(self.project.to_dict(), before)

    def test_proposal_semantics_replayed_even_if_all_hashes_recomputed(self):
        self.han();path = self.folder / "project.json";self.project.save(path)
        value = json.loads(path.read_text(encoding="utf-8"));review = value["pages"]["1"]["refinement_reviews"][0]
        proposal = review["proposals"][0];proposal["after"] = "錯"
        proposal["id"] = _digest({k: v for k, v in proposal.items() if k != "id"})
        review["id"] = _digest({k: v for k, v in review.items() if k not in ("id", "created_at")})
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        with self.assertRaises(ComparisonError):
            ComparisonProject.load(path)

    def test_old_project_missing_optional_fields_stays_without_them(self):
        self.add("easyocr", self.text)
        page = self.reopen().page(1)
        self.assertNotIn("refinement_reviews", page)
        self.assertNotIn("refinement_decisions", page)
        self.assertNotIn("region_readings", page)

    def test_region_retains_crop_fingerprints_without_overwriting_full_runs(self):
        self.han();before = self.project.page(1)["runs"]
        plan = plan_region(self.project, 1, 4, 5, "glmocr")
        session = self.session()
        record = run_region(self.project, plan, session=session)
        self.assertEqual(record["status"], "completed")
        self.assertEqual(plan["bbox"], [8, 18, 232, 92])
        self.assertEqual(record["raw"]["source_sha256"], plan["crop_png_sha256"])
        self.assertEqual(record["raw"]["words"], [])
        self.assertEqual(region_words(record), [])
        self.assertEqual(self.project.page(1)["runs"], before)
        self.assertEqual(self.reopen().page(1)["region_readings"][0], record)

    def test_region_actual_boxes_translate_only_by_crop_origin(self):
        self.han();plan = plan_region(self.project, 1, 4, 5, "easyocr")
        record = run_region(self.project, plan, session=self.session("easyocr", words=True))
        self.assertEqual(region_words(record)[0]["bbox"], plan["bbox"])
        self.assertEqual(record["raw"]["words"][0]["bbox"][:2], [0, 0])

    def test_region_generates_a_pending_anchored_proposal(self):
        self.han();plan = plan_region(self.project, 1, 4, 5, "glmocr")
        record = run_region(self.project, plan, session=self.session())
        items = build_region_candidates(self.project, 1)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["region_id"], record["id"])
        self.assertEqual(items[0]["after"], "神")
        self.assertEqual(self.project.page(1)["edits"], [])
        self.reopen()

    def test_cancel_and_incomplete_raw_are_saved_but_cannot_propose(self):
        self.han();plan = plan_region(self.project, 1, 4, 5, "glmocr")
        cancelled = run_region(self.project, plan, should_stop=lambda: True)
        incomplete = run_region(self.project, plan, session=self.session(complete=False))
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertEqual(incomplete["status"], "incomplete")
        self.assertIsNotNone(incomplete["raw"])
        self.assertEqual(build_region_candidates(self.project, 1), [])
        self.reopen()

    def test_unmet_glm_cuda_preserves_incomplete_raw_but_excludes_region_evidence(self):
        self.han();before = self.project.page(1)["runs"]
        plan = plan_region(self.project, 1, 4, 5, "glmocr")
        session = self.session(complete=False);recognize = session.recognize_file.side_effect
        def fallback(*args, **kwargs):
            raw = recognize(*args, **kwargs)
            raw.engine_metadata.update(actual_size_vram=0, device_requirement_satisfied=False)
            return raw
        session.recognize_file.side_effect = fallback
        record = run_region(self.project, plan, session=session)
        self.assertEqual(record["status"], "incomplete")
        self.assertIsNotNone(record["raw"])
        self.assertEqual(record["raw"]["engine_metadata"]["actual_size_vram"], 0)
        self.assertEqual(self.reopen().page(1)["region_readings"][0], record)
        self.assertEqual(build_region_candidates(self.project, 1), [])
        self.assertEqual(refinement_context(self.project, 1)["region_readings"], [])
        page = self.reopen().page(1)
        self.assertEqual(page["runs"], before)
        self.assertEqual(page["edits"], [])
        self.assertEqual(page["ai_reviews"], [])

    def test_region_device_exception_requires_explicit_incomplete_failure_and_matching_pins(self):
        self.han();plan = plan_region(self.project, 1, 4, 5, "glmocr")
        cases = ({"generation_complete": True}, {"device_requirement_satisfied": True},
                 {"device_requested": "cpu"}, {"actual_size_vram": None},
                 {"engine_root": str(self.folder / "different-home")},
                 {"session_conditions": {"worker": "f" * 64}})
        for changed in cases:
            with self.subTest(field=next(iter(changed))):
                session = self.session(complete=False);recognize = session.recognize_file.side_effect
                def invalid(*args, **kwargs):
                    raw = recognize(*args, **kwargs)
                    raw.engine_metadata.update(actual_size_vram=0, device_requirement_satisfied=False)
                    raw.engine_metadata.update(changed)
                    return raw
                session.recognize_file.side_effect = invalid
                record = run_region(self.project, plan, session=session)
                self.assertEqual(record["status"], "failed")
                self.assertIsNone(record["raw"])
                self.assertEqual(record["error_type"], "ComparisonError")
        self.reopen()

    def test_prompt_v7_and_v6_both_require_strict_completed_response_evidence(self):
        for version in ("comparison-ai-v6", "comparison-ai-v7"):
            with self.subTest(version=version), self.assertRaises(ComparisonError):
                _validate_anchor_evidence({"request": {}, "status": "completed",
                    "metadata": {"prompt_version": version, "response_complete": False}}, self.text)

    def test_edit_during_region_recognition_retains_raw_and_blocks_it(self):
        self.han();plan = plan_region(self.project, 1, 4, 5, "glmocr")
        session = self.session(during=lambda: self.project.set_text(1, self.text[:4] + "人" + self.text[5:]))
        record = run_region(self.project, plan, session=session)
        self.assertEqual(record["status"], "completed")
        self.assertTrue(record["evidence_stale_on_record"])
        self.assertEqual(build_region_candidates(self.project, 1), [])
        self.reopen()

    def test_region_coordinates_replayed_when_tampered_hashes_are_recomputed(self):
        self.han();plan = plan_region(self.project, 1, 4, 5, "glmocr")
        run_region(self.project, plan, session=self.session())
        path = self.folder / "project.json";self.project.save(path)
        value = json.loads(path.read_text(encoding="utf-8"));record = value["pages"]["1"]["region_readings"][0]
        plan = record["plan"];plan["bbox"][0] += 1;plan["transform"]["offset_x"] += 1
        plan["id"] = _digest({k: v for k, v in plan.items() if k != "id"});record["plan_sha256"] = _digest(plan)
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        with self.assertRaises(ComparisonError):
            ComparisonProject.load(path)

    def test_historical_ai_context_survives_later_human_acceptance(self):
        item = self.han();plan = plan_region(self.project, 1, 4, 5, "glmocr")
        run_region(self.project, plan, session=self.session())
        context = refinement_context(self.project, 1)
        request = {"snapshot_text": self.project.text(1), "snapshot_sha256": context["snapshot_sha256"],
                   "refinement_evidence": context, "refinement_evidence_sha256": _digest(context),
                   "baseline_run_id": self.project.page(1)["baseline_run_id"],
                   "source_sha256": self.project.source_sha256, "page_number": 1,
                   "runs": [{"run_id": r["id"], "raw_sha256": r["raw_sha256"]} for r in self.project.page(1)["runs"]]}
        accept_proposal(self.project, 1, item["review_id"], item["id"])
        validate_ai_refinement_context(self.project.page(1), request)
        altered = copy.deepcopy(request);altered["refinement_evidence"]["proposals"][0]["after"] = "錯"
        altered["refinement_evidence_sha256"] = _digest(altered["refinement_evidence"])
        with self.assertRaises(ComparisonError):
            validate_ai_refinement_context(self.project.page(1), altered)

    def test_shifted_roi_context_reports_current_offsets(self):
        self.han();plan = plan_region(self.project, 1, 4, 5, "glmocr")
        run_region(self.project, plan, session=self.session())
        self.project.set_text(1, "序" + self.text)
        context = refinement_context(self.project, 1)
        self.assertEqual(context["region_readings"][0]["current_start"], 5)
        self.assertEqual(context["region_readings"][0]["baseline_start"], 4)

    def test_difficult_region_selection_is_pure_bounded_and_anchored(self):
        self.han();before = self.project.to_dict()
        targets = region_targets(self.project, 1)
        self.assertEqual(len(targets), 1)
        self.assertEqual((targets[0]["start"], targets[0]["end"]), (4, 5))
        self.assertEqual(targets[0]["engines"], ["glmocr"])
        self.assertEqual(targets[0]["evidence"]["engine"], "easyocr")
        self.assertEqual(self.project.to_dict(), before)

    def test_region_selector_excludes_punctuation_only_and_human_changed_span(self):
        self.add("glmocr", "甲乙丙丁,戊己庚辛")
        self.add("easyocr", "甲乙丙丁，戊己庚辛")
        self.project.choose_baseline(1, "glmocr")
        self.assertEqual(region_targets(self.project, 1), [])
        self.add("easyocr", self.text)
        self.add("glmocr", self.text[:4] + "神" + self.text[5:])
        self.project.choose_baseline(1, "easyocr")
        self.project.set_text(1, self.text[:4] + "人" + self.text[5:])
        self.assertEqual(region_targets(self.project, 1), [])

    def test_region_selector_abstains_when_coordinate_anchors_repeat(self):
        text = "甲乙丙丁戊己庚辛壬甲乙丙丁戊己庚辛壬"
        self.add("easyocr", text);self.add("glmocr", text.replace("戊", "神", 1))
        self.project.choose_baseline(1, "easyocr")
        self.assertEqual(region_targets(self.project, 1), [])

    def test_region_selector_zero_limit_has_no_side_effect(self):
        self.han();before = self.project.to_dict()
        self.assertEqual(region_targets(self.project, 1, limit=0), [])
        self.assertEqual(self.project.to_dict(), before)

    def test_repeated_proposal_build_is_idempotent(self):
        self.han();before = self.project.to_dict();revision = self.project._revision
        build_candidates(self.project, 1)
        self.assertEqual(self.project.to_dict(), before)
        self.assertEqual(self.project._revision, revision)

    def test_overlapping_local_and_region_drafts_are_all_blocked(self):
        self.han();plan = plan_region(self.project, 1, 4, 5, "glmocr")
        run_region(self.project, plan, session=self.session())
        items = build_region_candidates(self.project, 1)
        before = self.project.to_dict()
        result = evaluate_proposals(self.project, 1)
        self.assertEqual(result["text"], self.text)
        self.assertEqual(result["applied"], [])
        self.assertEqual(len(result["blocked"]), 2)
        self.assertEqual(self.project.to_dict(), before)
        selected = evaluate_proposals(self.project, 1, proposal_ids=[items[0]["id"]])
        self.assertEqual(selected["applied"], [items[0]["id"]])

    def test_source_change_blocks_region_before_inference_or_project_mutation(self):
        self.han();plan = plan_region(self.project, 1, 4, 5, "glmocr")
        before = self.project.to_dict();session = self.session()
        Image.new("RGB", (320, 160), "black").save(self.source)
        with self.assertRaises(SourceChangedError):
            run_region(self.project, plan, session=session)
        session.recognize_file.assert_not_called()
        self.assertEqual(self.project.to_dict(), before)

    def test_roi_checkpoint_failure_retains_completed_raw(self):
        self.han();plan = plan_region(self.project, 1, 4, 5, "glmocr")
        def checkpoint(_record):
            raise OSError("synthetic disk failure")
        with self.assertRaises(OSError):
            run_region(self.project, plan, session=self.session(), on_result=checkpoint)
        self.assertEqual(self.project.page(1)["region_readings"][0]["status"], "completed")
        self.assertEqual(self.project.page(1)["edits"], [])

    def test_incomplete_source_cannot_make_a_han_proposal_or_roi_target(self):
        self.add("easyocr", self.text)
        self.add("glmocr", self.text[:4] + "神" + self.text[5:], complete=False)
        self.project.choose_baseline(1, "easyocr")
        self.assertEqual(build_candidates(self.project, 1), [])
        self.assertEqual(region_targets(self.project, 1), [])

    def test_region_rejects_supplied_session_from_different_engine_home(self):
        self.han();plan = plan_region(self.project, 1, 4, 5, "glmocr")
        session = self.session()
        session.condition_snapshot.return_value = self.conditions(engine_root=self.folder / "different-home")
        record = run_region(self.project, plan, session=session)
        self.assertEqual(record["status"], "failed")
        session.recognize_file.assert_not_called()
        self.assertIsNone(record["raw"])
        self.reopen()

    def test_region_rejects_same_root_but_different_model_runtime_or_device(self):
        self.han();plan = plan_region(self.project, 1, 4, 5, "glmocr")
        for field in ("worker", "installation", "protocol", "models", "runtime", "device"):
            with self.subTest(field=field):
                session = self.session();conditions = copy.deepcopy(session.condition_snapshot.return_value)
                if field == "device":
                    conditions["device"] = conditions["pin"]["device"] = "cpu"
                elif field in ("models", "runtime"):
                    conditions["pin"][field] = {"changed": "f" * 64}
                else:
                    conditions["pin"][field] = "f" * 64
                session.condition_snapshot.return_value = conditions
                record = run_region(self.project, plan, session=session)
                self.assertEqual(record["status"], "failed")
                session.recognize_file.assert_not_called()

    def test_region_checks_actual_output_pins_after_a_matching_snapshot(self):
        self.han();plan = plan_region(self.project, 1, 4, 5, "glmocr")
        session = self.session();recognize = session.recognize_file.side_effect
        def changed(*args, **kwargs):
            raw = recognize(*args, **kwargs)
            raw.engine_metadata["session_conditions"]["worker"] = "f" * 64
            return raw
        session.recognize_file.side_effect = changed
        record = run_region(self.project, plan, session=session)
        self.assertEqual(record["status"], "failed")
        self.assertIsNone(record["raw"])

    def test_region_reuses_one_verified_snapshot_but_binds_every_raw_output(self):
        self.han();conditions = self.conditions()
        plan = plan_region(self.project, 1, 4, 5, "glmocr", conditions=conditions)
        session = self.session()
        first = run_region(self.project, plan, session=session)
        second = run_region(self.project, plan, session=session)
        self.assertEqual([first["status"], second["status"]], ["completed", "completed"])
        self.assertEqual(session.condition_snapshot.call_count, 1)
        self.assertEqual(session.recognize_file.call_count, 2)


if __name__ == "__main__":
    unittest.main()
