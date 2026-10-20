"""Synthetic version-dispatch and preservation checks for current fidelity."""
import copy
import unittest

from yeone_ocr import comparison_fidelity_v3 as current
from yeone_ocr import comparison_fidelity_v1 as v1
from yeone_ocr import comparison_fidelity_v2 as v2
from test_comparison_fidelity_v2 import fixture as v2_fixture, proposal


def fixture(original, candidates, *, contract=v2.SOURCE_FIDELITY_CONTRACT, baseline="glmocr", image=False, phase="visual"):
    batch, runs, policy = v2_fixture(original, candidates, baseline=baseline)
    batch["phase"] = phase
    batch["source_fidelity_contract"] = contract
    batch["source_fidelity_evidence"] = current.build_source_fidelity_evidence(batch, runs, policy=policy)
    batch["source_fidelity_evidence_sha256"] = current._digest(batch["source_fidelity_evidence"])
    if image:
        batch["images"] = [{"id": "public-image", "sha256": "b" * 64,
                            "source_bbox": [0, 0, 800, 400], "width": 800, "height": 400}]
        batch["targets"] = [{"id": "public-target", "start": 0, "end": len(original),
                              "before": original, "image_id": "public-image"}]
    return batch, runs, policy


class FidelityV3VersionTests(unittest.TestCase):
    SOURCE = "天地，玄黃甲宇宙洪荒"
    WITNESS = "天地玄黃乙宇宙 洪荒"

    def test_han_verdicts_and_evidence_are_exactly_the_frozen_v2_results(self):
        cases = (("easyocr", self.WITNESS, "surya", self.WITNESS),
                 ("rapidocr", self.WITNESS, "paddle_server", self.WITNESS),
                 ("easyocr", self.SOURCE, "surya", self.SOURCE))
        for first, a, second, b in cases:
            with self.subTest(families=(first, second)):
                batch, runs, policy = fixture(self.SOURCE, {first: a, second: b})
                old = v2.assess_source_fidelity(batch, [proposal()], runs=runs, policy=policy)
                new = current.assess_source_fidelity(batch, [proposal()], runs=runs, policy=policy)
                self.assertEqual(new["proposals"], old["proposals"])
                self.assertEqual(new["evidence_sha256"], old["evidence_sha256"])
                self.assertEqual(new["dictionary"], old["dictionary"])
                self.assertEqual(new["contract"], "source-fidelity-v3")

    def test_explicit_older_contracts_reproduce_their_exact_proofs_without_mutation(self):
        for module, prompt in ((v1, "comparison-ai-v8"), (v2, "comparison-ai-v9")):
            with self.subTest(contract=module.SOURCE_FIDELITY_CONTRACT):
                batch, runs, policy = fixture(self.SOURCE, {"easyocr": self.WITNESS, "surya": self.WITNESS},
                                               contract=module.SOURCE_FIDELITY_CONTRACT)
                old = module.assess_source_fidelity(batch, [proposal()], runs=runs, policy=policy)
                review = {"request": batch, "status": "completed", "policy": policy,
                          "proposals": [proposal()], "metadata": {"prompt_version": prompt, "source_fidelity": old}}
                original = copy.deepcopy(review)
                self.assertEqual(current.verify_source_fidelity(review, runs=runs), old)
                self.assertEqual(current.assess_source_fidelity(batch, [proposal()], runs=runs, policy=policy,
                    contract=module.SOURCE_FIDELITY_CONTRACT), old)
                self.assertEqual(review, original)

    def test_new_prompt_contract_pair_and_attestation_are_required(self):
        batch, runs, policy = fixture(self.SOURCE, {"easyocr": self.WITNESS, "surya": self.WITNESS},
                                       contract=current.SOURCE_FIDELITY_CONTRACT)
        proof = current.assess_source_fidelity(batch, [proposal()], runs=runs, policy=policy)
        review = {"request": batch, "status": "completed", "policy": policy, "proposals": [proposal()],
                  "metadata": {"prompt_version": "comparison-ai-v10", "source_fidelity": proof}}
        self.assertEqual(current.verify_source_fidelity(review, runs=runs), proof)
        for prompt in ("comparison-ai-v8", "comparison-ai-v9", None):
            bad = copy.deepcopy(review);bad["metadata"]["prompt_version"] = prompt
            with self.subTest(prompt=prompt), self.assertRaises(current.FidelityError):
                current.verify_source_fidelity(bad, runs=runs)
        bad = copy.deepcopy(review);bad["metadata"]["source_fidelity"]["contract"] = v2.SOURCE_FIDELITY_CONTRACT
        with self.assertRaises(current.FidelityError):
            current.verify_source_fidelity(bad, runs=runs)

    def test_missing_new_attestation_is_permitted_only_for_failed_checkpoint(self):
        batch, runs, policy = fixture(self.SOURCE, {"easyocr": self.WITNESS, "surya": self.WITNESS},
                                       contract=current.SOURCE_FIDELITY_CONTRACT)
        review = {"request": batch, "status": "failed", "policy": policy, "proposals": [],
                  "metadata": {"prompt_version": "comparison-ai-v10"}}
        self.assertIsNone(current.verify_source_fidelity(review, runs=runs))
        review["status"] = "completed"
        with self.assertRaises(current.FidelityError):
            current.verify_source_fidelity(review, runs=runs)
        self.assertIs(current.FidelityError, v1.FidelityError)


class FidelityV3PunctuationTests(unittest.TestCase):
    SOURCE = "天地玄黃,宇宙洪荒"
    AFTER = "天地玄黃，宇宙洪荒"

    def punctuation(self, before=",", after="，", start=4, kind="recognition", identifier="punctuation"):
        return {**proposal(before, after, start=start), "kind": kind, "id": identifier}

    def verdict(self, batch, patch=None, visual=(), **kwargs):
        return current.assess_source_fidelity(batch, [patch or self.punctuation()], visual, **kwargs)["proposals"][0]

    def test_width_support_keeps_literals_and_requires_image_bound_two_families(self):
        batch, runs, policy = fixture(self.SOURCE, {"easyocr": self.AFTER, "surya": self.SOURCE}, image=True)
        patch = self.punctuation();original = copy.deepcopy((batch, runs, patch))
        old = v2.assess_source_fidelity(batch, [patch], runs=runs, policy=policy)["proposals"][0]
        self.assertTrue(old["blocked"])
        new = self.verdict(batch, patch, runs=runs, policy=policy)
        self.assertFalse(new["blocked"])
        evidence = new["punctuation_evidence"]
        self.assertEqual(evidence["after_families"], ["easyocr"])
        self.assertEqual(evidence["glyph_after_families"], ["easyocr", "surya"])
        self.assertEqual(evidence["route"], "one-exact-target-two-glyph-families-image-bound")
        self.assertTrue(evidence["image_request_provenance"])
        self.assertEqual((batch, runs, patch), original)

    def test_missing_image_or_exact_codepoint_or_independent_family_cannot_relax(self):
        cases = (({"easyocr": self.AFTER, "surya": self.SOURCE}, False),
                 ({"easyocr": self.SOURCE, "surya": self.SOURCE}, True),
                 ({"rapidocr": self.AFTER, "paddle_server": self.SOURCE}, True))
        for candidates, image in cases:
            with self.subTest(engines=tuple(candidates), image=image):
                batch, _, _ = fixture(self.SOURCE, candidates, image=image)
                self.assertTrue(self.verdict(batch)["blocked"])
        batch, _, _ = fixture(self.SOURCE, {"easyocr": self.AFTER, "surya": self.AFTER})
        self.assertFalse(self.verdict(batch)["blocked"])

    def test_width_only_classification_never_folds_han_space_or_other_shapes(self):
        for before, after in (("，", ","), ("；：", ";:"), ("｡", "。")):
            with self.subTest(pair=(before, after)):
                self.assertTrue(current._width_only(before, after))
        for before, after in (("。", "."), ("…", "..."), ("“", '"'), ("　", " "),
                              ("神", "神"), ("靑", "青"), ("，；", ";,"), ("，", ",,")):
            with self.subTest(pair=(before, after)):
                self.assertFalse(current._width_only(before, after))
        self.assertEqual(current._punctuation_shape("靑神一\ufe00。…“　"), "靑神一\ufe00。…“　")

    def test_width_and_semantic_changes_hold_on_other_glyph_count_or_order(self):
        for before, after, third in ((",", "，", ":"), (",", ";", ","),
                                     (",", "", ","), ("", ",", ";"), (",;", "，；", ";,"),
                                     (",", "，", ",,")):
            with self.subTest(before=before, after=after, third=third):
                source = "天地玄黃" + before + "宇宙洪荒"
                raw = "天地玄黃" + after + "宇宙洪荒"
                alternate = "天地玄黃" + third + "宇宙洪荒"
                batch, _, _ = fixture(source, {"easyocr": raw, "surya": raw, "tesseract": alternate}, image=True)
                verdict = self.verdict(batch, self.punctuation(before, after))
                self.assertIn("punctuation_glyph_conflict", verdict["reason_codes"])
                self.assertEqual(verdict["punctuation_evidence"]["glyph_conflict_families"], ["tesseract"])

    def test_nonwidth_does_not_use_one_literal_plus_similar_shape_support(self):
        source, after = "天地玄黃,宇宙洪荒", "天地玄黃;宇宙洪荒"
        batch, _, _ = fixture(source, {"easyocr": after, "surya": "天地玄黃；宇宙洪荒"}, image=True)
        self.assertIn("insufficient_punctuation_ocr_support", self.verdict(batch, self.punctuation(",", ";"))["reason_codes"])

    def test_target_internal_spaces_and_repeated_address_remain_unproven(self):
        source = "天地玄黃,;宇宙洪荒"
        batch, _, _ = fixture(source, {"easyocr": "天地玄黃，；宇宙洪荒", "surya": "天地玄黃, ;宇宙洪荒"}, image=True)
        result = self.verdict(batch, self.punctuation(",;", "，；"))
        self.assertTrue(result["blocked"])
        self.assertEqual(result["punctuation_evidence"]["glyph_after_families"], ["easyocr"])
        source += ";天地玄黃,;宇宙洪荒"
        batch, _, _ = fixture(source, {"easyocr": source, "surya": source}, image=True)
        self.assertIn("ambiguous_punctuation_address", self.verdict(batch, self.punctuation(",;", "，；"))["reason_codes"])

    def test_unique_mixed_context_uses_derived_target_without_changing_parent(self):
        source = "天地玄黃我,你宇宙洪荒"
        raw = source.replace(",", "，")
        batch, runs, policy = fixture(source, {"easyocr": raw, "surya": source}, image=True)
        parent = self.punctuation("我,你", "我，你");unchanged = copy.deepcopy(parent)
        result = self.verdict(batch, parent, runs=runs, policy=policy)
        self.assertFalse(result["blocked"])
        self.assertEqual(result["derived_punctuation_target"]["start"], 5)
        self.assertEqual(result["derived_punctuation_target"]["end"], 6)
        self.assertEqual(parent, unchanged)

    def test_multiple_splices_repeated_position_and_actual_han_change_never_bypass(self):
        cases = (("我,,你", "我,你", "ambiguous_punctuation_splice"),
                 ("我,你;他", "我，你；他", "mixed_punctuation_patch"),
                 ("甲,你", "乙，你", "mixed_punctuation_patch"))
        for before, after, reason in cases:
            with self.subTest(before=before):
                source = "天地玄黃" + before + "宇宙洪荒"
                raw = "天地玄黃" + after + "宇宙洪荒"
                batch, _, _ = fixture(source, {"easyocr": raw, "surya": raw}, image=True)
                result = self.verdict(batch, self.punctuation(before, after))
                self.assertIn(reason, result["reason_codes"])
                if "甲" in before:
                    self.assertEqual(result, v2.assess_source_fidelity(batch, [self.punctuation(before, after)])["proposals"][0])

    def test_independent_final_punctuation_can_replace_only_soft_punctuation_basis(self):
        batch, runs, policy = fixture(self.SOURCE, {"easyocr": self.AFTER, "surya": self.SOURCE},
                                       image=True, phase="editorial")
        parent = self.punctuation(kind="punctuation")
        basis = self.punctuation(",", ";", identifier="visual-basis")
        original = copy.deepcopy((parent, basis))
        old = v2.assess_source_fidelity(batch, [parent], [basis], runs=runs, policy=policy)["proposals"][0]
        self.assertIn("editorial_carries_blocked_visual", old["reason_codes"])
        result = self.verdict(batch, parent, visual=[basis], runs=runs, policy=policy)
        self.assertFalse(result["blocked"])
        self.assertEqual(result["independently_verified_basis"][0]["proposal_id"], "visual-basis")
        self.assertEqual((parent, basis), original)

    def test_han_or_uncertain_basis_and_selector_attachment_cannot_be_bypassed(self):
        batch, runs, policy = fixture(self.SOURCE, {"easyocr": self.AFTER, "surya": self.SOURCE},
                                       image=True, phase="editorial")
        parent = self.punctuation(kind="punctuation")
        for basis in (self.punctuation(",", "甲"), {**self.punctuation(",", ";"), "uncertain": True}):
            with self.subTest(uncertain=basis["uncertain"]):
                result = self.verdict(batch, parent, visual=[basis], runs=runs, policy=policy)
                self.assertIn("editorial_carries_blocked_visual", result["reason_codes"])
        source = "天地玄黃甲\ufe00宇宙洪荒"
        raw = source.replace("甲\ufe00", "甲,\ufe00")
        batch, _, _ = fixture(source, {"easyocr": raw, "surya": raw}, image=True)
        result = self.verdict(batch, self.punctuation("甲\ufe00", "甲,\ufe00"))
        self.assertIn("protected_unicode_change", result["reason_codes"])
        self.assertNotIn("mixed_punctuation_patch", result["reason_codes"])

    def test_wide_parent_retains_human_and_model_uncertainty_protection(self):
        source = "天地玄黃我,你宇宙洪荒"
        raw = source.replace(",", "，")
        batch, _, _ = fixture(source, {"easyocr": raw, "surya": raw}, image=True)
        parent = {**self.punctuation("我,你", "我，你"), "uncertain": True}
        result = self.verdict(batch, parent)
        self.assertIn("model_uncertain", result["reason_codes"])
        self.assertNotIn("mixed_punctuation_patch", result["reason_codes"])
        snapshot = source.replace(",", ":")
        raw = snapshot.replace(":", "：")
        batch, runs, policy = fixture(source, {"easyocr": raw, "surya": raw}, image=True)
        batch.update(snapshot_text=snapshot, snapshot_sha256=current._text_sha(snapshot))
        batch["targets"][0]["before"] = snapshot
        batch["source_fidelity_evidence"] = current.build_source_fidelity_evidence(batch, runs, policy=policy)
        batch["source_fidelity_evidence_sha256"] = current._digest(batch["source_fidelity_evidence"])
        parent = self.punctuation("我:你", "我：你")
        result = self.verdict(batch, parent, runs=runs, policy=policy)
        self.assertIn("human_changed_span", result["reason_codes"])
        self.assertNotIn("mixed_punctuation_patch", result["reason_codes"])


if __name__ == "__main__":
    unittest.main()
