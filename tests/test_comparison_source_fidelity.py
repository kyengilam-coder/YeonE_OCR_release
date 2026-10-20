"""Public synthetic glyphs and immutable evidence, without OCR/API calls."""
import copy
import unittest
from unittest.mock import patch

from ab_ocr.ocr import RecognitionResult
from yeone_ocr.comparison_fidelity_v2 import (
    FidelityError, SOURCE_FIDELITY_CONTRACT, _digest, _text_sha,
    assess_source_fidelity, build_source_fidelity_evidence, dictionary_manifest,
    validate_source_fidelity_evidence, verify_source_fidelity,
)


class SourceFidelityTests(unittest.TestCase):
    original = "天地玄黃甲宇宙洪荒，寒來暑往"

    def fixture(self, *, original=None, snapshot=None, engines=None, baseline="glmocr", phase="visual", traditional=False):
        original = original or self.original
        snapshot = snapshot or original
        engines = engines or {"glmocr": original, "easyocr": original.replace("甲", "乙"), "surya": original.replace("甲", "乙")}
        runs = {}
        for engine, text in engines.items():
            raw = RecognitionResult(text=text, words=[], source_path="public-synthetic.png", source_sha256="a" * 64,
                page_number=1, image_width=800, image_height=400, engine_version="synthetic", languages="chi_tra",
                psm=6 if engine == "tesseract" else None, duration_seconds=0, engine=engine,
                engine_metadata={"generation_complete": True}).to_dict()
            runs["run-" + engine] = {"id": "run-" + engine, "engine": engine, "raw": raw, "raw_sha256": _digest(raw),
                "request": {"profile": "traditional", "direction": "horizontal", "dpi": 300, "engine_root": None}}
        options = {"traditional": traditional, "punctuation": True, "preserve_variants": True}
        batch = {"phase": phase, "snapshot_text": snapshot, "snapshot_sha256": _text_sha(snapshot),
            "baseline_run_id": "run-" + baseline,
            "runs": [{"run_id": run["id"], "engine": run["engine"], "raw_sha256": run["raw_sha256"]} for run in runs.values()],
            "source_fidelity_contract": SOURCE_FIDELITY_CONTRACT, "source_fidelity_options": options}
        batch["source_fidelity_evidence"] = build_source_fidelity_evidence(batch, runs, policy=options)
        batch["source_fidelity_evidence_sha256"] = _digest(batch["source_fidelity_evidence"])
        return batch, runs, options

    def proposal(self, before="甲", after="乙", *, start=4, kind="recognition", identifier="proposal-1"):
        return {"id": identifier, "start": start, "end": start + len(before), "before": before, "after": after,
                "kind": kind, "reason": "public synthetic glyph evidence", "uncertain": False}

    def verdict(self, batch, proposal, **kwargs):
        return assess_source_fidelity(batch, [proposal], **kwargs)["proposals"][0]

    def test_two_other_families_support_exact_original_position_without_mutation(self):
        batch, runs, policy = self.fixture();proposal = self.proposal();before = copy.deepcopy((batch, runs, proposal))
        result = self.verdict(batch, proposal, runs=runs, policy=policy)
        self.assertFalse(result["blocked"])
        self.assertEqual(result["supporting_families"], ["easyocr", "surya"])
        self.assertEqual((batch, runs, proposal), before)

    def test_rapid_and_paddle_count_as_one_family(self):
        text = self.original.replace("甲", "乙")
        batch, _, _ = self.fixture(engines={"glmocr": self.original, "rapidocr": text, "paddle_server": text})
        result = self.verdict(batch, self.proposal())
        self.assertTrue(result["blocked"])
        self.assertEqual(result["supporting_families"], ["paddle"])

    def test_baseline_family_is_excluded_even_with_another_model(self):
        text = self.original.replace("甲", "乙")
        batch, _, _ = self.fixture(baseline="paddle_server", engines={"paddle_server": self.original, "rapidocr": text, "glmocr": text})
        self.assertEqual(self.verdict(batch, self.proposal())["supporting_families"], ["glmocr"])

    def test_different_location_with_same_surrounding_anchors_is_not_support(self):
        original = "abcd甲efgh / abcd乙efgh"
        batch, _, _ = self.fixture(original=original, engines={name: original for name in ("glmocr", "easyocr", "surya")})
        result = self.verdict(batch, self.proposal())
        self.assertIn("insufficient_unique_anchors", result["reason_codes"])
        self.assertEqual(result["supporting_runs"], [])

    def test_candidate_anchor_pair_must_also_be_unique(self):
        repeated = self.original.replace("甲", "乙") + " / 天地玄黃甲宇宙洪荒"
        batch, _, _ = self.fixture(engines={"glmocr": self.original, "easyocr": repeated, "surya": repeated})
        self.assertEqual(self.verdict(batch, self.proposal())["supporting_families"], [])

    def test_form_family_change_is_held_even_with_agreement_and_enabled_conversion(self):
        for old, new in (("蔘", "參"), ("软", "軟")):
            with self.subTest(pair=(old, new)):
                original = self.original.replace("甲", old)
                batch, _, _ = self.fixture(original=original, traditional=True,
                    engines={"glmocr": original, "easyocr": original.replace(old, new), "surya": original.replace(old, new)})
                self.assertIn("visual_form_family_change", self.verdict(batch, self.proposal(old, new))["reason_codes"])

    def test_compatibility_glyph_and_selector_move_are_held(self):
        for old, new in (("神", "神"), ("一\ufe00二", "一二\ufe00"), ("一\U000e0100", "一")):
            with self.subTest(pair=(old, new)):
                original = self.original.replace("甲", old)
                batch, _, _ = self.fixture(original=original, engines={name: original for name in ("glmocr", "easyocr", "surya")})
                self.assertIn("protected_unicode_change", self.verdict(batch, self.proposal(old, new))["reason_codes"])

    def test_selector_outside_proposal_still_protects_its_base(self):
        for selector in ("\ufe00", "\U000e0100", "\u180b"):
            with self.subTest(selector=ord(selector)):
                original = self.original.replace("甲", "甲" + selector)
                batch, runs, policy = self.fixture(original=original)
                proposal = self.proposal()
                unchanged = copy.deepcopy((batch, runs, proposal))
                result = self.verdict(batch, proposal, runs=runs, policy=policy)
                self.assertIn("protected_unicode_change", result["reason_codes"])
                self.assertEqual(result["supporting_families"], ["easyocr", "surya"])
                self.assertEqual((batch, runs, proposal), unchanged)

    def test_insertion_between_base_and_outside_selector_is_held(self):
        original = self.original.replace("甲", "甲\ufe00")
        batch, _, _ = self.fixture(original=original)
        result = self.verdict(batch, self.proposal("", ",", start=5))
        self.assertIn("protected_unicode_change", result["reason_codes"])

    def test_deletion_before_outside_selector_cannot_remove_its_base(self):
        original = self.original.replace("甲", "甲\ufe00")
        batch, _, _ = self.fixture(original=original)
        result = self.verdict(batch, self.proposal("黃甲", "黃", start=3))
        self.assertIn("protected_unicode_change", result["reason_codes"])

    def test_unrelated_punctuation_may_shift_an_intact_selector_sequence(self):
        original = self.original.replace("甲", "甲\ufe00")
        corrected = original[:4] + "," + original[4:]
        batch, _, _ = self.fixture(original=original, engines={"glmocr": original, "easyocr": corrected, "surya": corrected})
        self.assertFalse(self.verdict(batch, self.proposal("", ",", start=4))["blocked"])
        broad = self.verdict(batch, self.proposal(original, "," + original, start=0))
        self.assertNotIn("protected_unicode_change", broad["reason_codes"])
        self.assertIn("mixed_punctuation_patch", broad["reason_codes"])
        original = original[:4] + "," + original[4:]
        corrected = original[:4] + original[5:]
        batch, _, _ = self.fixture(original=original, engines={"glmocr": original, "easyocr": corrected, "surya": corrected})
        self.assertFalse(self.verdict(batch, self.proposal(",", "", start=4))["blocked"])

    def test_extension_han_including_unassigned_runtime_names_requires_support(self):
        for old in ("𠮷", "\U00031350"):
            with self.subTest(codepoint=ord(old)):
                original = self.original.replace("甲", old)
                batch, _, _ = self.fixture(original=original, engines={name: original for name in ("glmocr", "easyocr", "surya")})
                self.assertTrue(self.verdict(batch, self.proposal(old, "乙"))["blocked"])

    def test_han_insertion_and_deletion_are_held(self):
        batch, _, _ = self.fixture()
        for proposal in (self.proposal("", "乙"), self.proposal("甲", "")):
            self.assertIn("ambiguous_han_alignment", self.verdict(batch, proposal)["reason_codes"])

    def test_manual_snapshot_change_is_protected(self):
        batch, _, _ = self.fixture(snapshot=self.original.replace("甲", "丙"))
        self.assertIn("human_changed_span", self.verdict(batch, self.proposal("丙", "乙"))["reason_codes"])

    def test_opencc_unavailable_holds_han_but_not_good_punctuation(self):
        batch, _, _ = self.fixture()
        missing = {**dictionary_manifest(), "available": False}
        with patch("yeone_ocr.comparison_fidelity_v2.dictionary_manifest", return_value=missing):
            self.assertIn("form_dictionary_unavailable", self.verdict(batch, self.proposal())["reason_codes"])
            corrected = self.original.replace("，", ",")
            editorial, _, _ = self.fixture(phase="editorial", engines={"glmocr": self.original,
                "easyocr": corrected, "surya": corrected})
            self.assertFalse(self.verdict(editorial, self.proposal("，", ",", start=9, kind="punctuation"))["blocked"])

    def test_large_changed_snapshot_does_not_attempt_unbounded_alignment(self):
        original = "甲" * 20_001
        batch, _, _ = self.fixture(original=original, snapshot="乙" + original[1:], engines={name: original for name in ("glmocr", "easyocr", "surya")})
        self.assertFalse(batch["source_fidelity_evidence"]["snapshot_alignment_bounded"])
        self.assertIn("source_alignment_unavailable", self.verdict(batch, self.proposal("乙", "丙", start=0))["reason_codes"])

    def test_explicit_editorial_conversion_is_separate_and_policy_gated(self):
        original = self.original.replace("甲", "汉")
        batch, _, _ = self.fixture(original=original, phase="editorial", traditional=True)
        self.assertFalse(self.verdict(batch, self.proposal("汉", "漢", kind="traditional"))["blocked"])
        batch["source_fidelity_options"]["traditional"] = False
        batch["source_fidelity_evidence"]["options"]["traditional"] = False
        batch["source_fidelity_evidence_sha256"] = _digest(batch["source_fidelity_evidence"])
        self.assertIn("editorial_policy_violation", self.verdict(batch, self.proposal("汉", "漢", kind="traditional"))["reason_codes"])

    def test_legacy_bad_visual_cannot_bypass_guard_through_editorial_conversion(self):
        batch, runs, policy = self.fixture(phase="editorial", traditional=True, engines={name: self.original for name in ("glmocr", "easyocr", "surya")})
        bad_visual = self.proposal("甲", "汉")
        result = self.verdict(batch, self.proposal("甲", "漢", kind="traditional"), visual=[bad_visual], runs=runs, policy=policy)
        self.assertIn("editorial_carries_blocked_visual", result["reason_codes"])

    def test_tampered_evidence_and_attestation_are_rejected(self):
        batch, runs, policy = self.fixture();proposal = self.proposal()
        record = {"status": "completed", "request": batch, "policy": policy, "proposals": [proposal],
            "metadata": {"prompt_version": "comparison-ai-v9", "source_fidelity": assess_source_fidelity(batch, [proposal], policy=policy)}}
        verify_source_fidelity(record, runs=runs)
        changed = copy.deepcopy(batch);changed["source_fidelity_evidence"]["runs"][1]["text"] = "forged"
        changed["source_fidelity_evidence_sha256"] = _digest(changed["source_fidelity_evidence"])
        with self.assertRaises(FidelityError):
            validate_source_fidelity_evidence(changed, runs, policy=policy)
        record["metadata"]["source_fidelity"]["proposals"][0]["blocked"] = True
        with self.assertRaises(FidelityError):
            verify_source_fidelity(record, runs=runs)

    def test_failed_new_review_keeps_raw_without_completed_attestation(self):
        batch, runs, policy = self.fixture()
        record = {"status": "failed", "request": batch, "policy": policy, "proposals": [], "metadata": {"prompt_version": "comparison-ai-v9"}}
        self.assertIsNone(verify_source_fidelity(record, runs=runs))
        old = {"status": "completed", "request": {}, "metadata": {"prompt_version": "comparison-ai-v7"}}
        self.assertIsNone(verify_source_fidelity(old))


if __name__ == "__main__":
    unittest.main()
