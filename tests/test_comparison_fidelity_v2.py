"""Versioned, exact-literal OCR support with synthetic address noise only."""
import copy
import unittest

from ab_ocr.ocr import RecognitionResult
from yeone_ocr import comparison_fidelity_v2 as current
from yeone_ocr import comparison_fidelity_v1 as legacy


def fixture(original, candidates, *, contract=current.SOURCE_FIDELITY_CONTRACT, baseline="glmocr"):
    runs = {}
    for engine, text in {baseline: original, **candidates}.items():
        raw = RecognitionResult(text=text, words=[], source_path="public-synthetic.png",
            source_sha256="a" * 64, page_number=1, image_width=800, image_height=400,
            engine_version="synthetic", languages="chi_tra", psm=None, duration_seconds=0,
            engine=engine, engine_metadata={"generation_complete": True}).to_dict()
        runs[engine] = {"id": engine, "engine": engine, "raw": raw, "raw_sha256": current._digest(raw),
                        "request": {"profile": "traditional", "direction": "horizontal", "dpi": 300}}
    options = {"traditional": False, "punctuation": True, "preserve_variants": True}
    batch = {"phase": "visual", "snapshot_text": original, "snapshot_sha256": current._text_sha(original),
             "baseline_run_id": baseline, "runs": [{"run_id": engine, "engine": engine,
                "raw_sha256": run["raw_sha256"]} for engine, run in runs.items()],
             "source_fidelity_contract": contract, "source_fidelity_options": options}
    batch["source_fidelity_evidence"] = current.build_source_fidelity_evidence(batch, runs, policy=options)
    batch["source_fidelity_evidence_sha256"] = current._digest(batch["source_fidelity_evidence"])
    return batch, runs, options


def proposal(before="甲", after="乙", start=5):
    return {"id": "public-proposal", "start": start, "end": start + len(before), "before": before,
            "after": after, "kind": "recognition", "uncertain": False, "reason": "synthetic"}


class FidelityV2Tests(unittest.TestCase):
    SOURCE = "天地，玄黃甲宇宙洪荒"
    AFTER = "天地玄黃乙宇宙 洪荒"

    def assess(self, batch, patch=None, **kwargs):
        return current.assess_source_fidelity(batch, [patch or proposal()], **kwargs)["proposals"][0]

    def test_separator_address_noise_preserves_exact_target_and_all_inputs(self):
        batch, runs, options = fixture(self.SOURCE, {"easyocr": self.AFTER, "surya": self.AFTER})
        patch = proposal();unchanged = copy.deepcopy((batch, runs, patch))
        result = self.assess(batch, patch, runs=runs, policy=options)
        self.assertFalse(result["blocked"])
        self.assertEqual(result["supporting_families"], ["easyocr", "surya"])
        for witness in result["supporting_runs"]:
            self.assertEqual(witness["address_method"], "separator-projection-v1")
            self.assertEqual(runs[witness["run_id"]]["raw"]["text"][witness["start"]:witness["end"]], "乙")
        self.assertEqual((batch, runs, patch), unchanged)

    def test_target_mismatch_and_content_insertions_are_not_separator_noise(self):
        for raw in ("天地玄黃丙宇宙洪荒", "天地玄黃丙乙宇宙洪荒", "天地玄天黃乙宇宙洪荒"):
            with self.subTest(raw=raw):
                batch, _, _ = fixture(self.SOURCE, {"easyocr": raw, "surya": raw})
                self.assertTrue(self.assess(batch)["blocked"])

    def test_repeated_anchor_pair_is_ambiguous_without_looking_at_middle(self):
        repeated = "天地玄黃甲宇宙洪荒；天地，玄黃乙宇宙 洪荒"
        cases = ((repeated, repeated, 4), ("天地玄黃甲宇宙洪荒", repeated, 4))
        for source, raw, start in cases:
            with self.subTest(source_repeated=source == repeated):
                batch, _, _ = fixture(source, {"easyocr": raw, "surya": raw})
                self.assertTrue(self.assess(batch, proposal(start=start))["blocked"])

    def test_anchor_variants_are_never_folded(self):
        source = "天地玄靑甲宇宙洪荒"
        raw = "天地玄青乙宇宙洪荒"
        batch, _, _ = fixture(source, {"easyocr": raw, "surya": raw})
        self.assertTrue(self.assess(batch, proposal(start=4))["blocked"])

    def test_mixed_exact_and_projected_witnesses_still_require_two_other_families(self):
        exact = self.SOURCE.replace("甲", "乙")
        batch, _, _ = fixture(self.SOURCE, {"easyocr": exact, "surya": self.AFTER})
        result = self.assess(batch)
        self.assertFalse(result["blocked"])
        self.assertEqual({row["address_method"] for row in result["supporting_runs"]},
                         {"exact-codepoint-anchors", "separator-projection-v1"})
        batch, _, _ = fixture(self.SOURCE, {"rapidocr": exact, "paddle_server": self.AFTER})
        self.assertTrue(self.assess(batch)["blocked"])

    def test_baseline_related_family_does_not_become_a_second_vote(self):
        batch, _, _ = fixture(self.SOURCE, {"rapidocr": self.AFTER, "surya": self.AFTER}, baseline="paddle_server")
        self.assertEqual(self.assess(batch)["supporting_families"], ["surya"])

    def test_fallback_holds_original_or_third_literal_from_any_addressable_other_engine(self):
        for raw in (self.SOURCE, self.SOURCE.replace("甲", "丙")):
            with self.subTest(original_support=raw == self.SOURCE):
                batch, _, _ = fixture(self.SOURCE, {"easyocr": self.AFTER, "surya": self.AFTER, "tesseract": raw})
                result = self.assess(batch)
                self.assertIn("independent_ocr_conflict", result["reason_codes"])
                self.assertEqual(result["supporting_families"], ["easyocr", "surya"])
                self.assertEqual(result["opposing_families"], ["tesseract"])
                self.assertTrue(all(not row["matches_after"] for row in result["opposing_runs"]))

    def test_existing_exact_path_is_unchanged_by_fallback_conflict_rule(self):
        source = "天地玄黃甲宇宙洪荒"
        raw = source.replace("甲", "乙")
        batch, _, _ = fixture(source, {"easyocr": raw, "surya": raw, "tesseract": source})
        self.assertFalse(self.assess(batch, proposal(start=4))["blocked"])

    def test_unaddressable_other_raw_is_not_an_invented_conflict(self):
        batch, _, _ = fixture(self.SOURCE, {"easyocr": self.AFTER, "surya": self.AFTER,
                                            "tesseract": "不同位置甲無可用的原文錨點"})
        self.assertFalse(self.assess(batch)["blocked"])

    def test_short_or_overlong_address_is_held(self):
        for source, raw, start in (("天地甲宇宙", "天地乙宇宙", 2),
                                   ("天地玄黃" + "，" * 65 + "甲宇宙洪荒",
                                    "天地玄黃" + "，" * 65 + "乙宇宙洪荒", 69)):
            with self.subTest(length=len(source)):
                batch, _, _ = fixture(source, {"easyocr": raw, "surya": raw})
                # Force the noisy counterpart to use projected rather than exact anchors.
                if len(source) > 10:
                    batch, _, _ = fixture(source, {"easyocr": "天地玄黃乙宇宙 洪荒", "surya": "天地玄黃乙宇宙 洪荒"})
                self.assertTrue(self.assess(batch, proposal(start=start))["blocked"])

    def test_target_internal_whitespace_is_not_folded(self):
        source = self.SOURCE.replace("甲", "甲丙")
        raw = "天地玄黃乙 丁宇宙洪荒"
        batch, _, _ = fixture(source, {"easyocr": raw, "surya": raw})
        self.assertTrue(self.assess(batch, proposal("甲丙", "乙丁"))["blocked"])

    def test_selector_attachment_and_form_hold_cannot_be_overridden(self):
        source = self.SOURCE.replace("甲", "甲\ufe00")
        raw = self.AFTER.replace("乙", "乙\ufe00")
        batch, _, _ = fixture(source, {"easyocr": raw, "surya": raw})
        self.assertIn("protected_unicode_change", self.assess(batch)["reason_codes"])
        source = self.SOURCE.replace("甲", "软")
        raw = self.AFTER.replace("乙", "軟")
        batch, _, _ = fixture(source, {"easyocr": raw, "surya": raw})
        self.assertIn("visual_form_family_change", self.assess(batch, proposal("软", "軟"))["reason_codes"])

    def test_v1_stored_proof_replays_while_pending_view_uses_v2(self):
        batch, runs, options = fixture(self.SOURCE, {"easyocr": self.AFTER, "surya": self.AFTER},
                                       contract=legacy.SOURCE_FIDELITY_CONTRACT)
        patch = proposal()
        old = legacy.assess_source_fidelity(batch, [patch], runs=runs, policy=options)
        record = {"request": batch, "policy": options, "status": "completed", "proposals": [patch],
                  "metadata": {"prompt_version": "comparison-ai-v8", "source_fidelity": old}}
        unchanged = copy.deepcopy(record)
        self.assertEqual(current.verify_source_fidelity(record, runs=runs), old)
        self.assertEqual(current.assess_source_fidelity(batch, [patch], runs=runs, policy=options,
                         contract=legacy.SOURCE_FIDELITY_CONTRACT), old)
        self.assertTrue(old["proposals"][0]["blocked"])
        self.assertFalse(self.assess(batch, runs=runs, policy=options)["blocked"])
        self.assertEqual(record, unchanged)
        del record["metadata"]["prompt_version"]
        self.assertEqual(current.verify_source_fidelity(record, runs=runs),
                         legacy.verify_source_fidelity(record, runs=runs))

    def test_v9_proof_and_wrong_version_pairs_are_strict(self):
        batch, runs, options = fixture(self.SOURCE, {"easyocr": self.AFTER, "surya": self.AFTER})
        patch = proposal()
        record = {"request": batch, "policy": options, "status": "completed", "proposals": [patch],
                  "metadata": {"prompt_version": "comparison-ai-v9", "source_fidelity":
                    current.assess_source_fidelity(batch, [patch], runs=runs, policy=options)}}
        current.verify_source_fidelity(record, runs=runs)
        self.assertIs(current.FidelityError, legacy.FidelityError)
        for prompt in ("comparison-ai-v8", "comparison-ai-v7"):
            changed = copy.deepcopy(record);changed["metadata"]["prompt_version"] = prompt
            with self.subTest(prompt=prompt), self.assertRaises(current.FidelityError):
                current.verify_source_fidelity(changed, runs=runs)
        changed = copy.deepcopy(record);changed["metadata"]["source_fidelity"]["policy_sha256"] = "0" * 64
        with self.assertRaises(current.FidelityError):
            current.verify_source_fidelity(changed, runs=runs)

    def test_old_v7_request_without_attestation_uses_current_ephemeral_evidence(self):
        batch, runs, options = fixture(self.SOURCE, {"easyocr": self.AFTER, "surya": self.AFTER})
        for key in tuple(batch):
            if key.startswith("source_fidelity"):
                del batch[key]
        original = copy.deepcopy(batch)
        self.assertFalse(self.assess(batch, runs=runs, policy=options)["blocked"])
        self.assertEqual(batch, original)

    def test_original_evidence_must_be_authenticated_before_reassessment(self):
        batch, runs, options = fixture(self.SOURCE, {"easyocr": self.AFTER, "surya": self.AFTER})
        batch["source_fidelity_evidence"]["runs"][1]["text"] = "forged"
        batch["source_fidelity_evidence_sha256"] = current._digest(batch["source_fidelity_evidence"])
        with self.assertRaises(current.FidelityError):
            self.assess(batch, runs=runs, policy=options)

    def test_punctuation_gap_witness_preserves_exact_repeated_position(self):
        source = "天地玄黃，，宇宙洪荒"
        patch = proposal("，", "。", start=4)
        batch, _, _ = fixture(source, {"easyocr": "天地玄黃。，宇宙洪荒", "surya": "天地玄黃，。宇宙洪荒"})
        evidence = current._punctuation_witnesses(batch, patch, batch["source_fidelity_evidence"])
        self.assertTrue(evidence["address_valid"])
        self.assertEqual(evidence["after_families"], ["easyocr"])
        self.assertEqual(evidence["before_families"], [])
        self.assertEqual([row["stance"] for row in evidence["runs"]], ["after", "neither"])
        source = "天地玄黃，。宇宙洪荒"
        batch, _, _ = fixture(source, {"easyocr": "天地玄黃。宇宙洪荒", "surya": "天地玄黃。宇宙洪荒"})
        evidence = current._punctuation_witnesses(batch, patch, batch["source_fidelity_evidence"])
        self.assertEqual(evidence["after_families"], [])
        self.assertTrue(all(row["stance"] == "neither" for row in evidence["runs"]))

    def test_punctuation_insert_delete_and_source_conflict_are_distinct(self):
        cases = (("天地玄黃宇宙洪荒", "", "，", "天地玄黃，宇宙洪荒"),
                 ("天地玄黃，宇宙洪荒", "，", "", "天地玄黃宇宙洪荒"))
        for source, before, after, raw in cases:
            with self.subTest(operation="insertion" if not before else "deletion"):
                batch, _, _ = fixture(source, {"easyocr": raw, "surya": raw, "tesseract": source})
                evidence = current._punctuation_witnesses(batch, proposal(before, after, start=4),
                                                          batch["source_fidelity_evidence"])
                self.assertEqual(evidence["after_families"], ["easyocr", "surya"])
                self.assertEqual(evidence["before_families"], ["tesseract"])

    def test_punctuation_gap_ignores_only_whitespace_outside_target(self):
        source = "天地玄黃， 宇宙洪荒"
        batch, _, _ = fixture(source, {"easyocr": "天地玄黃, 宇宙洪荒", "surya": "天地玄黃,宇宙洪荒"})
        evidence = current._punctuation_witnesses(batch, proposal("，", ",", start=4),
                                                  batch["source_fidelity_evidence"])
        self.assertEqual(evidence["kind"], "width_style")
        self.assertEqual(evidence["after_families"], ["easyocr", "surya"])
        self.assertEqual([row["stance"] for row in evidence["runs"]], ["after", "after"])
        for row in evidence["runs"]:
            self.assertEqual(row["target_literal_sha256"], current._text_sha(","))
        batch, _, _ = fixture("天地玄黃，，宇宙洪荒", {"easyocr": "天地玄黃。 ，宇宙洪荒", "surya": "天地玄黃。 ，宇宙洪荒"})
        evidence = current._punctuation_witnesses(batch, proposal("，，", "。，", start=4),
                                                  batch["source_fidelity_evidence"])
        self.assertEqual(evidence["after_families"], [])

    def test_punctuation_target_whitespace_requires_entire_gap_exact(self):
        source = "天地玄黃，  宇宙洪荒"
        batch, _, _ = fixture(source, {"easyocr": "天地玄黃,  宇宙洪荒", "surya": "天地玄黃, 宇宙洪荒"})
        evidence = current._punctuation_witnesses(batch, proposal("， ", ", ", start=4),
                                                  batch["source_fidelity_evidence"])
        self.assertEqual(evidence["comparison_mode"], "whole-gap-exact")
        self.assertEqual(evidence["after_families"], ["easyocr"])

    def test_punctuation_gate_requires_two_families_but_does_not_veto_original_support(self):
        source, after = "天地玄黃，宇宙洪荒", "天地玄黃。宇宙洪荒"
        batch, _, _ = fixture(source, {"easyocr": after, "surya": after, "tesseract": source})
        result = self.assess(batch, proposal("，", "。", start=4))
        self.assertFalse(result["blocked"])
        self.assertEqual(result["punctuation_evidence"]["before_families"], ["tesseract"])
        batch, _, _ = fixture(source, {"rapidocr": after, "paddle_server": after})
        result = self.assess(batch, proposal("，", "。", start=4))
        self.assertIn("insufficient_punctuation_ocr_support", result["reason_codes"])

    def test_punctuation_insertion_deletion_and_width_changes_all_use_witness_gate(self):
        for before, after in (("", "，"), ("，", ""), ("，", ",")):
            with self.subTest(before=before, after=after):
                source = "天地玄黃" + before + "宇宙洪荒"
                raw = "天地玄黃" + after + "宇宙洪荒"
                batch, _, _ = fixture(source, {"easyocr": raw, "surya": raw})
                self.assertFalse(self.assess(batch, proposal(before, after, start=4))["blocked"])
                batch, _, _ = fixture(source, {"easyocr": raw, "surya": source})
                self.assertTrue(self.assess(batch, proposal(before, after, start=4))["blocked"])

    def test_punctuation_changes_cannot_hide_in_unchanged_or_changed_han(self):
        for source, before, after, start in (("天地玄黃我,你宇宙洪荒", "我,你", "我，你", 4),
                                             ("天地玄黃甲,你宇宙洪荒", "甲,你", "乙，你", 4)):
            with self.subTest(before=before):
                raw = source[:start] + after + source[start + len(before):]
                batch, _, _ = fixture(source, {"easyocr": raw, "surya": raw})
                result = self.assess(batch, proposal(before, after, start=start))
                self.assertIn("mixed_punctuation_patch", result["reason_codes"])

    def test_punctuation_ambiguous_pair_or_mixed_content_is_not_addressed(self):
        source = "天地玄黃，宇宙洪荒；天地玄黃。宇宙洪荒"
        batch, _, _ = fixture(source, {"easyocr": source, "surya": source})
        result = current._punctuation_witnesses(batch, proposal("，", "。", start=4),
                                                batch["source_fidelity_evidence"])
        self.assertFalse(result["address_valid"])
        batch, _, _ = fixture(self.SOURCE, {"easyocr": self.AFTER, "surya": self.AFTER})
        self.assertFalse(current._punctuation_witnesses(batch, proposal(),
                         batch["source_fidelity_evidence"])["address_valid"])

    def test_punctuation_style_classification_is_properties_only(self):
        cases = (("，", ",", "width_style"), ("　", " ", "width_style"),
                 ("， ", ",", "spacing_style"), ("，", "。", "replacement"),
                 ("", "，", "insertion"), ("，", "", "deletion"),
                 ("...", "…", "replacement"))
        for before, after, kind in cases:
            with self.subTest(before=before, after=after):
                self.assertEqual(current.punctuation_change_kind(before, after), kind)


if __name__ == "__main__":
    unittest.main()
