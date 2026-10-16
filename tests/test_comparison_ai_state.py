"""AI record/audit tests use synthetic text/images and make no provider calls."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from PIL import Image
from ab_ocr.ocr import RecognitionResult
from yeone_ocr.comparison import ComparisonProject, ComparisonError, SourceChangedError, latest_runs


def review_record(project, text=None, proposals=None, status="completed"):
    """Minimal valid synthetic review; useful for hosted editor regression tests."""
    page = project.page(1)
    snapshot = project.text(1) if text is None else text
    runs = latest_runs(page)
    return {"provider": "openai", "model": "synthetic-model", "policy": {"traditional": "propose", "punctuation": True},
        "status": status, "response_text": '{"result":"原文，한글。"}\n', "metadata": {"phase": "visual"},
        "request": {"source_sha256": project.source_sha256, "page_number": 1, "snapshot_text": snapshot,
            "snapshot_sha256": hashlib.sha256(snapshot.encode("utf-8")).hexdigest(), "baseline_run_id": page["baseline_run_id"],
            "runs": [{"run_id": r["id"], "engine": r["engine"], "raw_sha256": r["raw_sha256"]} for r in runs.values()],
            "targets": [{"id": "whole-page", "start": 0, "end": len(snapshot), "before": snapshot,
                         "bbox": [0, 0, 200, 100], "image_id": "page-image"}],
            "images": [{"id": "page-image", "sha256": "a" * 64, "width": 200, "height": 100,
                        "source_width": 200, "source_height": 100, "source_bbox": [0, 0, 200, 100]}]},
        "proposals": copy.deepcopy(proposals or [])}


def proposal(start=1, end=2, before="乙", after="仁", kind="recognition", uncertain=False):
    return {"start": start, "end": end, "before": before, "after": after,
            "kind": kind, "uncertain": uncertain, "reason": "synthetic image evidence"}


class AIState(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(prefix="ai-state-")
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.source = self.root / "synthetic.png"
        Image.new("RGB", (200, 100), "white").save(self.source)
        self.original_bytes = self.source.read_bytes()
        self.project = ComparisonProject.open(self.source)
        self.add_raw("甲乙丙丁戊己庚")

    def add_raw(self, text, engine="tesseract", activate=True):
        raw = RecognitionResult(text, [{"text": text, "bbox": [0, 0, 200, 100], "confidence": 80.0}],
            str(self.source), self.project.source_sha256, 1, 200, 100, "synthetic", "chi_tra", 6, 0, engine=engine)
        return self.project.add_result(raw, activate=activate)

    def add_review(self, proposals=None, **changes):
        record = review_record(self.project, proposals=proposals if proposals is not None else [proposal()])
        record.update(changes)
        identifier = self.project.record_ai_review(1, record)
        stored = self.project.page(1)["ai_reviews"][-1]
        return identifier, stored

    def save_load(self):
        path = self.root / "ai-project.json"
        self.project.save(path)
        return ComparisonProject.load(path)

    def scoped_review(self, phase="visual", proposals=None, status="completed", lineage=None, supersedes=None):
        record = review_record(self.project, proposals=proposals, status=status)
        record["request"].update(phase=phase, scope_fingerprint="c" * 64,
                                  batch_id=("d" if phase == "visual" else "e") * 32)
        record["metadata"]["phase"] = phase
        if lineage is not None:
            record["metadata"]["provisional_visual_proposal_ids"] = lineage
        if supersedes is not None:
            record["metadata"]["superseded_proposal_ids"] = supersedes
        identifier = self.project.record_ai_review(1, record)
        return identifier, self.project.page(1)["ai_reviews"][-1]

    def test_legacy_version_one_projects_open_without_invented_ai_state(self):
        payload = self.project.to_dict()
        del payload["pages"]["1"]["ai_reviews"]
        del payload["pages"]["1"]["ai_decisions"]
        path = self.root / "old.json"
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        loaded = ComparisonProject.load(path)
        self.assertEqual(loaded.page(1)["ai_reviews"], [])
        self.assertEqual(loaded.page(1)["ai_decisions"], [])
        self.assertEqual(loaded.text(1), "甲乙丙丁戊己庚")
        self.assertFalse(loaded.dirty)

    def test_response_request_and_proposals_are_immutable_snapshots(self):
        record = review_record(self.project, proposals=[proposal()])
        exact = record["response_text"]
        identifier = self.project.record_ai_review(1, record)
        record["response_text"] = "caller mutation"
        record["proposals"][0]["after"] = "caller mutation"
        snapshot = self.project.page(1)
        snapshot["ai_reviews"][0]["request"]["snapshot_text"] = "caller mutation"
        stored = self.project.page(1)["ai_reviews"][0]
        self.assertEqual(stored["id"], identifier)
        self.assertEqual(stored["response_text"], exact)
        self.assertEqual(stored["proposals"][0]["after"], "仁")
        self.assertIsNone(self.project.page(1)["corrected_text"])
        self.assertEqual(self.save_load().page(1)["ai_reviews"], [stored])

    def test_accept_keeps_raw_ai_raw_ocr_and_human_edits_separate(self):
        identifier, review = self.add_review()
        before_runs = self.project.page(1)["runs"]
        self.project.set_reviewed(1)
        text = self.project.accept_ai_proposal(1, identifier, review["proposals"][0]["id"])
        self.assertEqual(text, "甲仁丙丁戊己庚")
        page = self.project.page(1)
        self.assertEqual(page["runs"], before_runs)
        self.assertEqual(page["ai_reviews"], [review])
        self.assertEqual(page["edits"][-1]["kind"], "ai_accept")
        self.assertEqual(page["ai_decisions"][-1]["decision"], "accepted")
        self.assertFalse(page["reviewed"])
        self.assertEqual(self.save_load().text(1), text)

    def test_multiple_candidates_map_through_independent_manual_changes(self):
        identifier, review = self.add_review([proposal(), proposal(5, 6, "己", "義")])
        self.project.set_text(1, self.project.text(1) + "。")
        for candidate in review["proposals"]:
            self.project.accept_ai_proposal(1, identifier, candidate["id"])
        self.assertEqual(self.project.text(1), "甲仁丙丁戊義庚。")
        self.save_load()

    def test_overlapping_manual_edit_blocks_stale_candidate(self):
        identifier, review = self.add_review()
        self.project.set_text(1, "甲人丙丁戊己庚")
        with self.assertRaises(ComparisonError):
            self.project.accept_ai_proposal(1, identifier, review["proposals"][0]["id"])
        self.assertEqual(self.project.text(1), "甲人丙丁戊己庚")
        self.assertFalse(self.project.ai_proposals(1)[0]["applicable"])
        self.assertEqual(self.project.page(1)["ai_decisions"], [])

    def test_rerecognition_invalidates_evidence_without_deleting_ai_history(self):
        identifier, review = self.add_review()
        self.add_raw("甲仁丙丁戊己庚")
        self.assertTrue(self.project.ai_proposals(1)[0]["stale"])
        with self.assertRaises(ComparisonError):
            self.project.accept_ai_proposal(1, identifier, review["proposals"][0]["id"])
        self.assertEqual(self.project.page(1)["ai_reviews"], [review])
        self.save_load()

    def test_inflight_old_evidence_response_is_preserved_as_stale_history(self):
        record = review_record(self.project, proposals=[proposal()])
        self.add_raw("甲仁丙丁戊己庚")
        identifier = self.project.record_ai_review(1, record)
        stored = self.project.page(1)["ai_reviews"][-1]
        self.assertEqual(stored["status"], "completed")
        self.assertEqual(stored["response_text"], record["response_text"])
        self.assertTrue(self.project.ai_proposals(1)[0]["stale"])
        with self.assertRaises(ComparisonError):
            self.project.accept_ai_proposal(1, identifier, stored["proposals"][0]["id"])
        self.save_load()

    def test_historical_import_does_not_invalidate_active_evidence(self):
        identifier, review = self.add_review()
        self.add_raw("old historical text", activate=False)
        self.assertTrue(self.project.ai_proposals(1)[0]["applicable"])
        self.project.accept_ai_proposal(1, identifier, review["proposals"][0]["id"])
        self.save_load()

    def test_uncertain_candidates_require_manual_correction(self):
        identifier, review = self.add_review([proposal(uncertain=True)])
        candidate = review["proposals"][0]
        self.assertFalse(self.project.ai_proposals(1)[0]["applicable"])
        with self.assertRaises(ComparisonError):
            self.project.accept_ai_proposal(1, identifier, candidate["id"])
        self.project.reject_ai_proposal(1, identifier, candidate["id"])
        self.assertEqual(self.project.ai_proposals(1), [])
        self.assertEqual(self.project.ai_proposals(1, True)[0]["decision"], "rejected")
        self.assertIsNone(self.project.page(1)["corrected_text"])
        self.save_load()

    def test_undo_retains_all_ai_evidence_and_all_decisions(self):
        identifier, review = self.add_review()
        candidate = review["proposals"][0]
        self.project.accept_ai_proposal(1, identifier, candidate["id"])
        self.project.undo_edit(1)
        self.assertEqual(self.project.text(1), "甲乙丙丁戊己庚")
        self.assertEqual(self.project.page(1)["ai_reviews"], [review])
        self.assertEqual([d["decision"] for d in self.project.page(1)["ai_decisions"]], ["accepted", "undone"])
        self.assertTrue(self.project.ai_proposals(1)[0]["applicable"])
        self.project.accept_ai_proposal(1, identifier, candidate["id"])
        self.save_load()

    def test_failed_and_cancelled_raw_responses_are_preserved_read_only(self):
        for status in ("failed", "cancelled"):
            record = review_record(self.project, status=status)
            record["response_text"] = "malformed {AI response\n神 漢，한글"
            record["error"] = "invalid_response"
            self.project.record_ai_review(1, record)
        self.assertEqual(len(self.project.page(1)["ai_reviews"]), 2)
        self.assertEqual(self.project.ai_proposals(1, True), [])
        self.assertIn("malformed {AI", self.save_load().page(1)["ai_reviews"][0]["response_text"])
        record["proposals"] = [proposal()]
        with self.assertRaises(ComparisonError):
            self.project.record_ai_review(1, record)
        self.assertEqual(len(self.project.page(1)["ai_reviews"]), 2)

    def test_inflight_response_is_recoverable_after_original_changes(self):
        record = review_record(self.project, proposals=[proposal()])
        self.source.write_bytes(b"externally changed synthetic source")
        identifier = self.project.record_ai_review(1, record)
        stored = self.project.page(1)["ai_reviews"][-1]
        self.assertEqual(stored["source_state_on_record"], "changed")
        self.assertTrue(self.project.ai_proposals(1)[0]["stale"])
        self.assertFalse(self.project.ai_proposals(1)[0]["applicable"])
        with self.assertRaises(SourceChangedError):
            self.project.accept_ai_proposal(1, identifier, stored["proposals"][0]["id"])
        recovery = self.root / "recovery.json"
        self.project.save_recovery(recovery)
        self.source.write_bytes(self.original_bytes)
        self.assertEqual(ComparisonProject.load(recovery).page(1)["ai_reviews"], [stored])
        self.assertTrue(self.project.ai_proposals(1)[0]["applicable"])

    def test_inflight_response_is_recoverable_when_original_disconnects(self):
        record = review_record(self.project, proposals=[proposal()])
        self.source.unlink()
        self.project.record_ai_review(1, record)
        self.assertEqual(self.project.page(1)["ai_reviews"][0]["source_state_on_record"], "unavailable")
        path = self.root / "missing-source-recovery.json"
        self.project.save_recovery(path)
        loaded = ComparisonProject.load(path, allow_missing_source=True)
        self.assertEqual(len(loaded.page(1)["ai_reviews"]), 1)

    def test_unicode_variants_and_punctuation_are_not_normalized(self):
        self.add_raw("神 神 漢 汉,。")
        before = self.project.text(1)
        offset = before.index(",")
        identifier, review = self.add_review([proposal(offset, offset + 1, ",", "，", "punctuation")])
        self.project.accept_ai_proposal(1, identifier, review["proposals"][0]["id"])
        self.assertEqual(self.project.text(1), "神 神 漢 汉，。")
        self.assertEqual(self.project.page(1)["runs"][-1]["raw"]["text"], before)
        self.assertEqual(self.save_load().page(1)["ai_reviews"][0]["request"]["snapshot_text"], before)

    def test_tampered_raw_response_hash_is_rejected_on_reopen(self):
        self.add_review()
        path = self.root / "tampered.json"
        payload = self.project.to_dict()
        payload["pages"]["1"]["ai_reviews"][0]["response_text"] = "tampered"
        path.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaises(ComparisonError):
            ComparisonProject.load(path)

    def test_wrong_source_snapshot_evidence_and_scope_are_rejected(self):
        mutations = [
            lambda r: r["request"].update(source_sha256="b" * 64),
            lambda r: r["request"].update(snapshot_sha256="b" * 64),
            lambda r: r["request"]["runs"][0].update(raw_sha256="b" * 64),
            lambda r: r["proposals"][0].update(before="wrong"),
            lambda r: r["request"]["targets"][0].update(end=1, before="甲"),
            lambda r: r["request"]["images"][0].update(source_bbox=[-1, 0, 200, 100]),
            lambda r: r["request"]["targets"][0].update(bbox=[0, 0, 500, 100]),
        ]
        for mutate in mutations:
            record = review_record(self.project, proposals=[proposal()])
            mutate(record)
            with self.subTest(mutation=mutate), self.assertRaises(ComparisonError):
                self.project.record_ai_review(1, record)
        self.assertEqual(self.project.page(1)["ai_reviews"], [])

    def test_credentials_and_headers_cannot_enter_ai_metadata(self):
        for key in ("api_key", "Authorization", "client-secret", "request_headers"):
            record = review_record(self.project)
            record["metadata"]["nested"] = [{key: "synthetic credential must not persist"}]
            with self.subTest(field=key), self.assertRaises(ComparisonError):
                self.project.record_ai_review(1, record)
        self.assertEqual(self.project.page(1)["ai_reviews"], [])

    def test_raw_image_payloads_cannot_enter_persisted_ai_metadata(self):
        for mutate in (lambda r: r["request"]["images"][0].update(data="synthetic base64 payload"),
                       lambda r: r["metadata"].update(image_base64="synthetic base64 payload")):
            record = review_record(self.project)
            mutate(record)
            with self.assertRaises(ComparisonError):
                self.project.record_ai_review(1, record)
        self.assertEqual(self.project.page(1)["ai_reviews"], [])

    def test_invalid_record_has_no_hidden_mutation_on_an_unprocessed_page(self):
        fresh = ComparisonProject.open(self.source)
        before = fresh.to_dict()
        record = review_record(fresh)
        record["provider"] = ""
        with self.assertRaises(ComparisonError):
            fresh.record_ai_review(1, record)
        self.assertEqual(fresh.to_dict(), before)
        self.assertFalse(fresh.dirty)

    def test_decision_must_link_to_real_ai_edit_and_exact_patch(self):
        identifier, review = self.add_review()
        self.project.accept_ai_proposal(1, identifier, review["proposals"][0]["id"])
        for mutate in (lambda p: p["ai_decisions"][0].update(edit_id="invented"),
                       lambda p: p["edits"][0].update(applied_start=0),
                       lambda p: p.update(ai_decisions=[])):
            payload = self.project.to_dict()
            mutate(payload["pages"]["1"])
            path = self.root / "bad-audit.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.subTest(mutation=mutate), self.assertRaises(ComparisonError):
                ComparisonProject.load(path)

    def test_editorial_supersedes_visual_without_mutating_either_record(self):
        visual_id, visual = self.add_review()
        record = review_record(self.project, proposals=[proposal(after="義", kind="traditional")])
        record["metadata"] = {"phase": "editorial", "superseded_proposal_ids": [visual["proposals"][0]["id"]]}
        editorial_id = self.project.record_ai_review(1, record)
        self.assertEqual(len(self.project.ai_proposals(1)), 1)
        old = next(p for p in self.project.ai_proposals(1, True) if p["review_id"] == visual_id)
        self.assertTrue(old["superseded"])
        self.assertFalse(old["applicable"])
        with self.assertRaises(ComparisonError):
            self.project.accept_ai_proposal(1, visual_id, visual["proposals"][0]["id"])
        final = self.project.ai_proposals(1)[0]
        self.project.accept_ai_proposal(1, editorial_id, final["id"])
        self.assertEqual(self.project.text(1), "甲義丙丁戊己庚")
        self.assertEqual(self.project.page(1)["ai_reviews"][0], visual)
        self.save_load()

    def test_superseding_unrelated_scope_is_rejected(self):
        _, visual = self.add_review()
        record = review_record(self.project, proposals=[proposal(5, 6, "己", "義", "traditional")])
        record["metadata"]["superseded_proposal_ids"] = [visual["proposals"][0]["id"]]
        with self.assertRaises(ComparisonError):
            self.project.record_ai_review(1, record)
        self.assertEqual(len(self.project.page(1)["ai_reviews"]), 1)

    def test_failed_new_generation_keeps_previous_proposals_read_only(self):
        identifier, old = self.scoped_review(proposals=[proposal()])
        self.scoped_review(status="failed")
        self.assertEqual(self.project.ai_proposals(1), [])
        candidate = self.project.ai_proposals(1, True)[0]
        self.assertTrue(candidate["generation_stale"])
        self.assertTrue(candidate["stale"])
        self.assertFalse(candidate["applicable"])
        with self.assertRaises(ComparisonError):
            self.project.accept_ai_proposal(1, identifier, old["proposals"][0]["id"])
        self.assertEqual(self.project.page(1)["ai_reviews"][0], old)
        self.save_load()

    def test_rejected_visual_basis_invalidates_existing_editorial_proposal(self):
        visual_id, visual = self.scoped_review(proposals=[proposal()])
        basis = [visual["proposals"][0]["id"]]
        editorial_id, editorial = self.scoped_review("editorial", [proposal(after="義", kind="traditional")],
                                                    lineage=basis, supersedes=basis)
        self.assertTrue(next(p for p in self.project.ai_proposals(1, True) if p["review_id"] == editorial_id)["applicable"])
        self.project.reject_ai_proposal(1, visual_id, basis[0])
        candidate = next(p for p in self.project.ai_proposals(1, True) if p["review_id"] == editorial_id)
        self.assertTrue(candidate["lineage_stale"])
        self.assertFalse(candidate["applicable"])
        with self.assertRaises(ComparisonError):
            self.project.accept_ai_proposal(1, editorial_id, editorial["proposals"][0]["id"])
        self.assertEqual(self.project.page(1)["ai_reviews"], [visual, editorial])
        self.save_load()

    def test_new_visual_generation_invalidates_old_editorial_basis(self):
        _, visual = self.scoped_review(proposals=[proposal()])
        basis = [visual["proposals"][0]["id"]]
        editorial_id, _ = self.scoped_review("editorial", [proposal(after="義", kind="traditional")],
                                           lineage=basis, supersedes=basis)
        latest_id, _ = self.scoped_review(proposals=[proposal(after="人")])
        views = self.project.ai_proposals(1, True)
        self.assertTrue(next(p for p in views if p["review_id"] == editorial_id)["lineage_stale"])
        self.assertTrue(next(p for p in views if p["review_id"] == latest_id)["applicable"])
        self.assertEqual(self.project.text(1), "甲乙丙丁戊己庚")
        self.save_load()

    def test_generation_changes_never_rewrite_human_accepted_decisions(self):
        identifier, review = self.scoped_review(proposals=[proposal()])
        inflight = review_record(self.project, status="failed")
        inflight["request"].update(phase="visual", scope_fingerprint="c" * 64, batch_id="d" * 32)
        self.project.accept_ai_proposal(1, identifier, review["proposals"][0]["id"])
        edits, decisions = self.project.page(1)["edits"], self.project.page(1)["ai_decisions"]
        self.project.record_ai_review(1, inflight)
        self.assertEqual(self.project.text(1), "甲仁丙丁戊己庚")
        self.assertEqual(self.project.page(1)["edits"], edits)
        self.assertEqual(self.project.page(1)["ai_decisions"], decisions)
        self.save_load()

    def test_new_editorial_agreement_does_not_keep_old_supersession_active(self):
        visual_id, visual = self.scoped_review(proposals=[proposal()])
        basis = [visual["proposals"][0]["id"]]
        old_id, _ = self.scoped_review("editorial", [proposal(after="義", kind="traditional")],
                                     lineage=basis, supersedes=basis)
        self.scoped_review("editorial", [], lineage=basis)
        view = next(p for p in self.project.ai_proposals(1, True) if p["review_id"] == visual_id)
        self.assertFalse(view["superseded"])
        self.assertTrue(view["applicable"])
        self.assertTrue(next(p for p in self.project.ai_proposals(1, True) if p["review_id"] == old_id)["generation_stale"])
        self.save_load()


if __name__ == "__main__":
    unittest.main()
