"""Source protection through project decisions and persistence; synthetic only."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
import uuid

from PIL import Image

from ab_ocr.ocr import RecognitionResult
from yeone_ocr.comparison import (ComparisonError, ComparisonProject, _digest,
                                 _source_fidelity_acceptance, latest_runs)


class FidelityIntegration(unittest.TestCase):
    TEXT = "天地玄黄宇宙洪荒日月盈昃辰宿列張寒來暑往"
    START = 9

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="fidelity-project-")
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.source = self.folder / "synthetic.png"
        Image.new("RGB", (300, 200), "white").save(self.source)
        self.project = ComparisonProject.open(self.source)
        self.add_raw("tesseract", self.TEXT)
        self.project.compare_page(1, baseline="tesseract")

    def add_raw(self, engine, text):
        return self.project.add_result(RecognitionResult(text,
            [{"text": text, "bbox": [0, 0, 300, 200], "confidence": 80.0}],
            str(self.source), self.project.source_sha256, 1, 300, 200,
            "synthetic", "chi_tra", 6 if engine == "tesseract" else None, 0,
            engine=engine))

    def support(self, after="星", engines=("easyocr", "glmocr")):
        text = self.TEXT[:self.START] + after + self.TEXT[self.START + 1:]
        for engine in engines:
            self.add_raw(engine, text)
        self.project.compare_page(1, baseline="tesseract")

    def record(self, *, new=False, after="星", phase="visual", lineage=None, supersedes=None,
               contract="source-fidelity-v1", kind="recognition"):
        from dataclasses import asdict
        from yeone_ocr.comparison_ai import ReviewPolicy
        policy = asdict(ReviewPolicy(traditional=False, punctuation=True))
        page = self.project.page(1)
        proposal = {"id": uuid.uuid4().hex, "start": self.START, "end": self.START + 1,
                    "before": self.TEXT[self.START:self.START + 1], "after": after,
                    "kind": kind, "reason": "synthetic image evidence", "uncertain": False}
        batch = {"source_sha256": self.project.source_sha256, "page_number": 1,
                 "snapshot_text": self.TEXT, "snapshot_sha256": hashlib.sha256(self.TEXT.encode()).hexdigest(),
                 "baseline_run_id": page["baseline_run_id"],
                 "runs": [{"run_id": r["id"], "engine": r["engine"], "raw_sha256": r["raw_sha256"]}
                          for r in latest_runs(page).values()],
                 "targets": [{"id": "whole", "start": 0, "end": len(self.TEXT), "before": self.TEXT,
                              "bbox": [0, 0, 300, 200], "image_id": "synthetic-image"}],
                 "images": [{"id": "synthetic-image", "sha256": "a" * 64, "width": 300, "height": 200,
                             "source_width": 300, "source_height": 200, "source_bbox": [0, 0, 300, 200]}],
                 "phase": phase, "scope_fingerprint": "c" * 64,
                 "batch_id": ("d" if phase == "visual" else "e") * 32}
        body = {key: value for key, value in proposal.items() if key != "id"}
        response = json.dumps({"items": [{"target_id": "whole", "uncertain": False,
                                          "note": "synthetic", "proposals": [body]}]}, ensure_ascii=False)
        record = {"provider": "openai", "model": "synthetic-model", "policy": policy,
                  "status": "completed", "request": batch, "response_text": response,
                  "metadata": {"phase": phase}, "proposals": [proposal]}
        if lineage is not None:
            record["metadata"]["provisional_visual_proposal_ids"] = lineage
        if supersedes is not None:
            record["metadata"]["superseded_proposal_ids"] = supersedes
        if new:
            from yeone_ocr.comparison_fidelity import assess_source_fidelity, build_source_fidelity_evidence
            batch.update(source_fidelity_contract=contract,
                         response_contract="comparison-ai-anchored-v1",
                         source_fidelity_options={key: policy[key] for key in
                                                  ("traditional", "punctuation", "preserve_variants")})
            runs = {run["id"]: run for run in page["runs"]}
            batch["source_fidelity_evidence"] = build_source_fidelity_evidence(batch, runs, policy=policy)
            batch["source_fidelity_evidence_sha256"] = _digest(batch["source_fidelity_evidence"])
            previous = [p for old in page.get("ai_reviews", []) for p in old["proposals"]
                        if p["id"] in (lineage or [])]
            record["metadata"].update(prompt_version={"source-fidelity-v1": "comparison-ai-v8",
                                                      "source-fidelity-v2": "comparison-ai-v9",
                                                      "source-fidelity-v3": "comparison-ai-v10",
                                                      "source-fidelity-v4": "comparison-ai-v11"}[contract], response_complete=True,
                                     finish_reason="completed",
                                     source_fidelity=assess_source_fidelity(batch, [proposal], previous,
                                                                          runs=runs, policy=policy,
                                                                          contract=contract))
        identifier = self.project.record_ai_review(1, record)
        return identifier, proposal["id"]

    def reopen(self):
        target = self.folder / "project.json"
        self.project.save(target)
        return ComparisonProject.load(target)

    def test_legacy_pending_is_held_without_rewriting_AI_or_OCR(self):
        review, proposal = self.record()
        before = self.project.to_dict()
        item = self.project.ai_proposals(1)[0]
        self.assertTrue(item["source_fidelity_blocked"])
        self.assertFalse(item["applicable"])
        self.assertIn("원본보존 보류", item["blocked_reason"])
        with self.assertRaises(ComparisonError):
            self.project.accept_ai_proposal(1, review, proposal)
        self.assertEqual(self.project.to_dict(), before)
        self.assertEqual(self.reopen().to_dict(), before)

    def test_two_independent_witnesses_allow_accept_undo_and_reopen(self):
        self.support()
        review, proposal = self.record()
        raw = self.project.page(1)
        self.assertTrue(self.project.ai_proposals(1)[0]["applicable"])
        self.project.accept_ai_proposal(1, review, proposal)
        self.assertTrue(self.project.undo_edit(1))
        reopened = self.reopen()
        self.assertEqual(reopened.text(1), self.TEXT)
        self.assertEqual(reopened.page(1)["runs"], raw["runs"])
        self.assertEqual(reopened.page(1)["ai_reviews"], raw["ai_reviews"])
        self.assertEqual([d["decision"] for d in reopened.page(1)["ai_decisions"]], ["accepted", "undone"])

    def test_shared_Paddle_family_is_not_two_independent_witnesses(self):
        self.support(engines=("rapidocr", "paddle_server"))
        self.record()
        self.assertFalse(self.project.ai_proposals(1)[0]["applicable"])

    def test_blocked_proposal_can_be_rejected_without_changing_text_or_raw(self):
        review, proposal = self.record()
        raw = self.project.page(1)
        self.project.reject_ai_proposal(1, review, proposal)
        reopened = self.reopen()
        self.assertEqual(reopened.text(1), self.TEXT)
        self.assertEqual(reopened.page(1)["ai_reviews"], raw["ai_reviews"])
        self.assertEqual(reopened.page(1)["runs"], raw["runs"])
        self.assertEqual(reopened.page(1)["ai_decisions"][-1]["decision"], "rejected")

    def test_legacy_accepted_history_is_not_retroactively_invalidated(self):
        review, proposal = self.record()
        self.project.set_text(1, self.TEXT[:self.START] + "星" + self.TEXT[self.START + 1:])
        payload = self.project.to_dict()
        page = payload["pages"]["1"]
        edit = page["edits"][-1]
        edit.update(kind="ai_accept", ai_review_id=review, ai_proposal_id=proposal,
                    applied_start=self.START, applied_end=self.START + 1)
        page["ai_decisions"].append({"id": uuid.uuid4().hex, "at": edit["at"], "review_id": review,
                                     "proposal_id": proposal, "decision": "accepted", "edit_id": edit["id"]})
        target = self.folder / "legacy-accepted.json"
        target.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        loaded = ComparisonProject.load(target)
        self.assertEqual(loaded.to_dict(), payload)
        self.assertEqual(loaded.ai_proposals(1, True)[0]["decision"], "accepted")
        self.assertFalse(loaded.ai_proposals(1, True)[0]["source_fidelity_blocked"])
        self.assertFalse(loaded.dirty)

    def test_new_attestation_roundtrips_without_modifying_raw_proposal(self):
        self.support()
        self.record(new=True)
        before = self.project.to_dict()
        self.assertTrue(self.project.ai_proposals(1)[0]["applicable"])
        self.assertEqual(self.reopen().to_dict(), before)

    def test_new_attestation_tamper_is_rejected_even_after_record_rehash(self):
        self.record(new=True)
        payload = self.project.to_dict()
        review = payload["pages"]["1"]["ai_reviews"][0]
        review["metadata"]["source_fidelity"]["proposals"][0]["blocked"] = False
        review["record_sha256"] = _digest({key: value for key, value in review.items() if key != "record_sha256"})
        target = self.folder / "tampered.json"
        target.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        with self.assertRaises(ComparisonError):
            ComparisonProject.load(target)

    def test_new_witness_text_is_rechecked_against_original_raw_even_after_rehash(self):
        self.support()
        self.record(new=True)
        payload = self.project.to_dict()
        review = payload["pages"]["1"]["ai_reviews"][0]
        evidence = review["request"]["source_fidelity_evidence"]
        evidence["runs"][1]["text"] = "invented witness"
        review["request"]["source_fidelity_evidence_sha256"] = _digest(evidence)
        review["record_sha256"] = _digest({key: value for key, value in review.items() if key != "record_sha256"})
        target = self.folder / "invented-witness.json"
        target.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        with self.assertRaises(ComparisonError):
            ComparisonProject.load(target)

    def test_new_attestation_removal_is_rejected_even_after_record_rehash(self):
        self.record(new=True)
        payload = self.project.to_dict()
        review = payload["pages"]["1"]["ai_reviews"][0]
        del review["metadata"]["source_fidelity"]
        review["record_sha256"] = _digest({key: value for key, value in review.items() if key != "record_sha256"})
        target = self.folder / "missing-attestation.json"
        target.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        with self.assertRaises(ComparisonError):
            ComparisonProject.load(target)

    def test_new_failed_response_is_preserved_without_a_success_attestation(self):
        self.record(new=True)
        prior = self.project.page(1)["ai_reviews"][0]
        failed = {key: value for key, value in prior.items()
                  if key not in ("id", "created_at", "source_state_on_record", "record_sha256")}
        failed.update(status="failed", proposals=[], response_text="synthetic incomplete {", error="synthetic failure")
        failed["metadata"] = {key: value for key, value in failed["metadata"].items() if key != "source_fidelity"}
        self.project.record_ai_review(1, failed)
        reviews = self.reopen().page(1)["ai_reviews"]
        self.assertEqual(reviews[0], prior)
        self.assertEqual(reviews[1]["status"], "failed")
        self.assertEqual(reviews[1]["response_text"], "synthetic incomplete {")

    def test_legacy_editorial_lineage_keeps_original_held_visual_history(self):
        visual, proposal = self.record()
        editorial, _ = self.record(phase="editorial", lineage=[proposal], supersedes=[proposal])
        original = self.project.page(1)["ai_reviews"]
        item = next(p for p in self.project.ai_proposals(1, True) if p["review_id"] == editorial)
        self.assertFalse(item["lineage_stale"])
        self.assertFalse(item["applicable"])
        self.assertEqual(self.reopen().page(1)["ai_reviews"], original)

    def test_form_substitution_is_held_even_with_two_witnesses(self):
        self.TEXT = self.TEXT[:self.START] + "擧" + self.TEXT[self.START + 1:]
        self.add_raw("tesseract", self.TEXT)
        self.support(after="舉")
        self.record(after="舉")
        item = self.project.ai_proposals(1)[0]
        self.assertTrue(item["source_fidelity_blocked"])
        self.assertFalse(item["applicable"])

    def test_held_new_editorial_does_not_supersede_supported_visual(self):
        self.support()
        visual, proposal = self.record(new=True)
        editorial, _ = self.record(new=True, phase="editorial", after="曜", lineage=[proposal])
        items = self.project.ai_proposals(1, True)
        self.assertTrue(next(p for p in items if p["review_id"] == visual)["applicable"])
        self.assertTrue(next(p for p in items if p["review_id"] == editorial)["source_fidelity_blocked"])
        self.reopen()

    def test_new_held_editorial_cannot_forge_supersession(self):
        self.support()
        _, proposal = self.record(new=True)
        with self.assertRaisesRegex(ComparisonError, "대체"):
            self.record(new=True, phase="editorial", after="曜", lineage=[proposal], supersedes=[proposal])

    def test_new_editorial_lineage_excludes_legacy_held_visual(self):
        self.record()
        editorial, _ = self.record(new=True, phase="editorial", after="曜", lineage=[])
        item = next(p for p in self.project.ai_proposals(1, True) if p["review_id"] == editorial)
        self.assertFalse(item["lineage_stale"])
        self.assertTrue(item["source_fidelity_blocked"])
        self.reopen()

    def version_one_overheld_candidate(self):
        self.TEXT = "天地玄黃，甲。宇宙洪荒"
        self.START = 5
        self.add_raw("tesseract", self.TEXT)
        witness = "天地玄黃,乙.宇宙洪荒"
        for engine in ("easyocr", "glmocr"):
            self.add_raw(engine, witness)
        self.project.compare_page(1, baseline="tesseract")
        review_id, proposal_id = self.record(new=True, after="乙")
        stored = self.project.page(1)["ai_reviews"][-1]
        self.assertEqual(stored["metadata"]["source_fidelity"]["contract"], "source-fidelity-v1")
        self.assertTrue(stored["metadata"]["source_fidelity"]["proposals"][0]["blocked"])
        return review_id, proposal_id, stored

    def test_version_one_overheld_candidate_accepts_with_separate_version_two_proof(self):
        review, proposal, stored = self.version_one_overheld_candidate()
        before = self.project.page(1)
        view = self.project.ai_proposals(1)[0]
        from yeone_ocr.comparison_fidelity import SOURCE_FIDELITY_CONTRACT
        self.assertEqual(view["source_fidelity_contract"], SOURCE_FIDELITY_CONTRACT)
        self.assertTrue(view["applicable"])
        self.project.accept_ai_proposal(1, review, proposal)
        reopened = self.reopen()
        page = reopened.page(1)
        self.assertEqual(page["ai_reviews"], before["ai_reviews"])
        self.assertEqual(page["runs"], before["runs"])
        self.assertEqual(page["ai_reviews"][-1], stored)
        proof = page["ai_decisions"][-1]["source_fidelity_acceptance"]
        self.assertEqual(proof["attestation"]["contract"], SOURCE_FIDELITY_CONTRACT)
        self.assertEqual(proof["review_record_sha256"], stored["record_sha256"])
        self.assertEqual(page["edits"][-1]["source_fidelity_acceptance_sha256"], _digest(proof))
        self.assertTrue(reopened.undo_edit(1))
        target = self.folder / "undo-version-two-acceptance.json"
        reopened.save(target)
        self.assertEqual(ComparisonProject.load(target).text(1), self.TEXT)

    def test_version_two_acceptance_proof_cannot_be_removed_or_rehashed(self):
        review, proposal, _ = self.version_one_overheld_candidate()
        self.project.accept_ai_proposal(1, review, proposal)
        original = self.project.to_dict()
        for mutation in ("proof-only", "proof-and-edit-link", "changed-proof-with-new-link"):
            payload = copy.deepcopy(original)
            page = payload["pages"]["1"]
            decision, edit = page["ai_decisions"][-1], page["edits"][-1]
            if mutation == "changed-proof-with-new-link":
                proof = decision["source_fidelity_acceptance"]
                proof["before_text_sha256"] = "0" * 64
                edit["source_fidelity_acceptance_sha256"] = _digest(proof)
            else:
                del decision["source_fidelity_acceptance"]
                if mutation == "proof-and-edit-link":
                    del edit["source_fidelity_acceptance_sha256"]
            target = self.folder / (mutation + ".json")
            target.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            with self.subTest(mutation=mutation), self.assertRaises(ComparisonError):
                ComparisonProject.load(target)

    def test_version_one_editorial_lineage_uses_stored_guard_instead_of_current_guard(self):
        self.version_one_overheld_candidate()
        editorial, _ = self.record(new=True, phase="editorial", after="曜", lineage=[])
        before = self.project.page(1)["ai_reviews"]
        item = next(p for p in self.project.ai_proposals(1, True) if p["review_id"] == editorial)
        self.assertFalse(item["lineage_stale"])
        self.assertEqual(self.reopen().page(1)["ai_reviews"], before)

    def test_version_two_review_requires_its_separate_acceptance_proof(self):
        self.support()
        review, proposal = self.record(new=True, contract="source-fidelity-v2")
        self.project.accept_ai_proposal(1, review, proposal)
        self.reopen()
        payload = self.project.to_dict()
        page = payload["pages"]["1"]
        del page["ai_decisions"][-1]["source_fidelity_acceptance"]
        del page["edits"][-1]["source_fidelity_acceptance_sha256"]
        target = self.folder / "version-two-missing-proof.json"
        target.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        with self.assertRaises(ComparisonError):
            ComparisonProject.load(target)

    def test_original_version_one_accepted_without_new_proof_remains_unchanged(self):
        self.support()
        review, proposal = self.record(new=True)
        self.project.set_text(1, self.TEXT[:self.START] + "星" + self.TEXT[self.START + 1:])
        payload = self.project.to_dict()
        page = payload["pages"]["1"]
        edit = page["edits"][-1]
        edit.update(kind="ai_accept", ai_review_id=review, ai_proposal_id=proposal,
                    applied_start=self.START, applied_end=self.START + 1)
        page["ai_decisions"].append({"id": uuid.uuid4().hex, "at": edit["at"], "review_id": review,
                                     "proposal_id": proposal, "decision": "accepted", "edit_id": edit["id"]})
        target = self.folder / "original-version-one-accepted.json"
        target.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        reopened = ComparisonProject.load(target)
        self.assertEqual(reopened.to_dict(), payload)
        self.assertFalse(reopened.dirty)

    def test_version_one_raw_held_compatibility_proposal_stays_readable(self):
        self.TEXT = self.TEXT[:self.START] + "神" + self.TEXT[self.START + 1:]
        self.add_raw("tesseract", self.TEXT)
        self.support(after="神")
        self.record(new=True, after="神")
        before = self.project.to_dict()
        stored = before["pages"]["1"]["ai_reviews"][-1]
        self.assertTrue(stored["metadata"]["source_fidelity"]["proposals"][0]["blocked"])
        self.assertFalse(stored["proposals"][0]["uncertain"])
        self.assertEqual(self.reopen().to_dict(), before)

    def test_acceptance_proof_cannot_move_to_another_identical_character(self):
        self.TEXT += "月春夏秋冬"
        self.add_raw("tesseract", self.TEXT)
        self.support()
        review, proposal = self.record(new=True, contract="source-fidelity-v2")
        self.project.accept_ai_proposal(1, review, proposal)
        payload = self.project.to_dict()
        page = payload["pages"]["1"]
        edit, decision = page["edits"][-1], page["ai_decisions"][-1]
        wrong = self.TEXT.rfind("月")
        proof = decision["source_fidelity_acceptance"]
        proof.update(applied_start=wrong, applied_end=wrong + 1)
        edit.update(applied_start=wrong, applied_end=wrong + 1,
                    after=self.TEXT[:wrong] + "星" + self.TEXT[wrong + 1:],
                    source_fidelity_acceptance_sha256=_digest(proof))
        page["corrected_text"] = edit["after"]
        target = self.folder / "rebound-wrong-position.json"
        target.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        with self.assertRaisesRegex(ComparisonError, "검증 위치"):
            ComparisonProject.load(target)

    def test_original_version_one_punctuation_accept_stays_when_current_policy_holds(self):
        self.TEXT, self.START = "天地玄黃，宇宙洪荒", 4
        for engine in ("tesseract", "easyocr", "glmocr"):
            self.add_raw(engine, self.TEXT)
        self.project.compare_page(1, baseline="tesseract")
        review, proposal = self.record(new=True, after="。", phase="editorial", kind="punctuation")
        stored = self.project.page(1)["ai_reviews"][-1]
        self.assertFalse(stored["metadata"]["source_fidelity"]["proposals"][0]["blocked"])
        self.project.set_text(1, "天地玄黃。宇宙洪荒")
        payload = self.project.to_dict()
        page = payload["pages"]["1"]
        edit = page["edits"][-1]
        edit.update(kind="ai_accept", ai_review_id=review, ai_proposal_id=proposal,
                    applied_start=self.START, applied_end=self.START + 1)
        page["ai_decisions"].append({"id": uuid.uuid4().hex, "at": edit["at"], "review_id": review,
                                     "proposal_id": proposal, "decision": "accepted", "edit_id": edit["id"]})
        target = self.folder / "version-one-punctuation-accepted.json"
        target.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        reopened = ComparisonProject.load(target)
        self.assertEqual(reopened.to_dict(), payload)
        self.assertEqual(reopened.text(1), "天地玄黃。宇宙洪荒")
        item = reopened.ai_proposals(1, True)[0]
        self.assertEqual(item["decision"], "accepted")
        self.assertTrue(item["source_fidelity"]["blocked"])
        self.assertFalse(item["source_fidelity_blocked"])
        self.assertFalse(reopened.dirty)
        self.assertTrue(reopened.undo_edit(1))
        item = reopened.ai_proposals(1)[0]
        self.assertTrue(item["source_fidelity_blocked"])
        self.assertFalse(item["applicable"])
        with self.assertRaises(ComparisonError):
            reopened.accept_ai_proposal(1, review, proposal)
        undone = self.folder / "version-one-punctuation-undone.json"
        reopened.save(undone)
        loaded = ComparisonProject.load(undone)
        self.assertEqual(loaded.text(1), self.TEXT)
        self.assertEqual(loaded.page(1)["ai_reviews"][-1], stored)

    def test_historical_version_two_acceptance_proof_is_replayed_with_version_two(self):
        self.support()
        review_id, proposal_id = self.record(new=True, contract="source-fidelity-v2")
        self.project.accept_ai_proposal(1, review_id, proposal_id)
        payload = self.project.to_dict()
        page = payload["pages"]["1"]
        review = next(r for r in page["ai_reviews"] if r["id"] == review_id)
        proof = _source_fidelity_acceptance(page, review, proposal_id, self.project.source_sha256,
                                           self.TEXT, self.START, self.START + 1,
                                           contract="source-fidelity-v2")
        self.assertEqual(proof["attestation"]["contract"], "source-fidelity-v2")
        page["ai_decisions"][-1]["source_fidelity_acceptance"] = proof
        page["edits"][-1]["source_fidelity_acceptance_sha256"] = _digest(proof)
        target = self.folder / "historical-version-two-accepted-proof.json"
        target.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        reopened = ComparisonProject.load(target)
        self.assertEqual(reopened.to_dict(), payload)
        self.assertFalse(reopened.dirty)

    def test_version_three_new_review_acceptance_proof_is_required(self):
        from yeone_ocr.comparison_fidelity import SOURCE_FIDELITY_CONTRACT
        self.support()
        review, proposal = self.record(new=True, contract="source-fidelity-v3")
        before = self.project.page(1)
        self.project.accept_ai_proposal(1, review, proposal)
        reopened = self.reopen()
        page = reopened.page(1)
        self.assertEqual(page["ai_reviews"], before["ai_reviews"])
        self.assertEqual(page["runs"], before["runs"])
        self.assertEqual(page["ai_decisions"][-1]["source_fidelity_acceptance"]["attestation"]["contract"],
                         SOURCE_FIDELITY_CONTRACT)
        payload = reopened.to_dict()
        del payload["pages"]["1"]["ai_decisions"][-1]["source_fidelity_acceptance"]
        del payload["pages"]["1"]["edits"][-1]["source_fidelity_acceptance_sha256"]
        target = self.folder / "version-three-missing-acceptance-proof.json"
        target.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        with self.assertRaises(ComparisonError):
            ComparisonProject.load(target)

    def test_version_three_review_cannot_downgrade_its_acceptance_proof(self):
        self.support()
        review_id, proposal_id = self.record(new=True, contract="source-fidelity-v3")
        self.project.accept_ai_proposal(1, review_id, proposal_id)
        payload = self.project.to_dict()
        page = payload["pages"]["1"]
        review = next(r for r in page["ai_reviews"] if r["id"] == review_id)
        old_policy_proof = _source_fidelity_acceptance(page, review, proposal_id, self.project.source_sha256,
                                                      self.TEXT, self.START, self.START + 1,
                                                      contract="source-fidelity-v2")
        page["ai_decisions"][-1]["source_fidelity_acceptance"] = old_policy_proof
        page["edits"][-1]["source_fidelity_acceptance_sha256"] = _digest(old_policy_proof)
        target = self.folder / "version-three-proof-downgrade.json"
        target.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        with self.assertRaises(ComparisonError):
            ComparisonProject.load(target)

    def test_historical_version_three_acceptance_proof_is_replayed_with_version_three(self):
        self.support()
        review_id, proposal_id = self.record(new=True, contract="source-fidelity-v3")
        self.project.accept_ai_proposal(1, review_id, proposal_id)
        payload = self.project.to_dict()
        page = payload["pages"]["1"]
        review = next(r for r in page["ai_reviews"] if r["id"] == review_id)
        proof = _source_fidelity_acceptance(page, review, proposal_id, self.project.source_sha256,
                                           self.TEXT, self.START, self.START + 1,
                                           contract="source-fidelity-v3")
        self.assertEqual(proof["attestation"]["contract"], "source-fidelity-v3")
        page["ai_decisions"][-1]["source_fidelity_acceptance"] = proof
        page["edits"][-1]["source_fidelity_acceptance_sha256"] = _digest(proof)
        target = self.folder / "historical-version-three-accepted-proof.json"
        target.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        reopened = ComparisonProject.load(target)
        self.assertEqual(reopened.to_dict(), payload)
        self.assertFalse(reopened.dirty)

    def test_historical_version_three_bracket_acceptance_survives_current_hold_and_undo(self):
        self.TEXT = "天地玄黃(宇宙洪荒)寒來暑往"
        self.START = self.TEXT.index(")")
        self.add_raw("tesseract", self.TEXT)
        self.support(after="）")
        review_id, proposal_id = self.record(new=True, contract="source-fidelity-v3", after="）")
        stored = self.project.page(1)["ai_reviews"][-1]
        self.assertFalse(stored["metadata"]["source_fidelity"]["proposals"][0]["blocked"])
        self.assertTrue(self.project.ai_proposals(1)[0]["source_fidelity_blocked"])
        corrected = self.TEXT[:self.START] + "）" + self.TEXT[self.START + 1:]
        self.project.set_text(1, corrected)
        payload = self.project.to_dict()
        page = payload["pages"]["1"]
        review = next(r for r in page["ai_reviews"] if r["id"] == review_id)
        proof = _source_fidelity_acceptance(page, review, proposal_id, self.project.source_sha256,
                                           self.TEXT, self.START, self.START + 1,
                                           contract="source-fidelity-v3")
        edit = page["edits"][-1]
        edit.update(kind="ai_accept", ai_review_id=review_id, ai_proposal_id=proposal_id,
                    applied_start=self.START, applied_end=self.START + 1,
                    source_fidelity_acceptance_sha256=_digest(proof))
        page["ai_decisions"].append({"id": uuid.uuid4().hex, "at": edit["at"], "review_id": review_id,
            "proposal_id": proposal_id, "decision": "accepted", "edit_id": edit["id"],
            "source_fidelity_acceptance": proof})
        target = self.folder / "historical-version-three-bracket.json"
        target.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        reopened = ComparisonProject.load(target)
        self.assertEqual(reopened.to_dict(), payload)
        self.assertEqual(reopened.text(1), corrected)
        self.assertFalse(reopened.dirty)
        accepted = reopened.ai_proposals(1, True)[0]
        self.assertEqual(accepted["decision"], "accepted")
        self.assertTrue(accepted["source_fidelity"]["blocked"])
        self.assertFalse(accepted["source_fidelity_blocked"])
        self.assertTrue(reopened.undo_edit(1))
        pending = reopened.ai_proposals(1)[0]
        self.assertTrue(pending["source_fidelity_blocked"])
        self.assertFalse(pending["applicable"])
        with self.assertRaises(ComparisonError):
            reopened.accept_ai_proposal(1, review_id, proposal_id)
        undone = self.folder / "historical-version-three-bracket-undone.json"
        reopened.save(undone)
        loaded = ComparisonProject.load(undone)
        self.assertEqual(loaded.text(1), self.TEXT)
        self.assertEqual(loaded.page(1)["ai_reviews"][-1], stored)
        self.assertEqual(loaded.page(1)["ai_decisions"][0]["source_fidelity_acceptance"], proof)

    def test_version_four_review_requires_its_separate_acceptance_proof(self):
        self.support()
        review, proposal = self.record(new=True, contract="source-fidelity-v4")
        original = self.project.page(1)
        self.project.accept_ai_proposal(1, review, proposal)
        reopened = self.reopen()
        page = reopened.page(1)
        self.assertEqual(page["ai_reviews"], original["ai_reviews"])
        self.assertEqual(page["runs"], original["runs"])
        self.assertEqual(page["ai_decisions"][-1]["source_fidelity_acceptance"]["attestation"]["contract"],
                         "source-fidelity-v4")
        payload = reopened.to_dict()
        del payload["pages"]["1"]["ai_decisions"][-1]["source_fidelity_acceptance"]
        del payload["pages"]["1"]["edits"][-1]["source_fidelity_acceptance_sha256"]
        target = self.folder / "version-four-missing-acceptance-proof.json"
        target.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        with self.assertRaises(ComparisonError):
            ComparisonProject.load(target)

    def test_version_four_review_rejects_rehashed_version_two_and_three_proofs(self):
        self.support()
        review_id, proposal_id = self.record(new=True, contract="source-fidelity-v4")
        self.project.accept_ai_proposal(1, review_id, proposal_id)
        accepted = self.project.to_dict()
        for contract in ("source-fidelity-v2", "source-fidelity-v3"):
            with self.subTest(contract=contract):
                payload = copy.deepcopy(accepted)
                page = payload["pages"]["1"]
                review = next(r for r in page["ai_reviews"] if r["id"] == review_id)
                proof = _source_fidelity_acceptance(page, review, proposal_id, self.project.source_sha256,
                                                   self.TEXT, self.START, self.START + 1, contract=contract)
                page["ai_decisions"][-1]["source_fidelity_acceptance"] = proof
                page["edits"][-1]["source_fidelity_acceptance_sha256"] = _digest(proof)
                target = self.folder / (contract + "-downgraded-version-four.json")
                target.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
                with self.assertRaises(ComparisonError):
                    ComparisonProject.load(target)


if __name__ == "__main__":
    unittest.main()
