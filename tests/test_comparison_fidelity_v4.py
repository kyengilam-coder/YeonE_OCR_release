"""Public synthetic preservation and punctuation structure checks."""
import copy
import unittest

from yeone_ocr import comparison_fidelity as current
from yeone_ocr import comparison_fidelity_v3 as v3
from test_comparison_fidelity_v3 import fixture


class FidelityV4Tests(unittest.TestCase):
    def patch(self, original, before, after):
        start = original.index(before)
        return {"id": "width-proposal", "start": start, "end": start + len(before),
                "before": before, "after": after, "kind": "recognition", "uncertain": False,
                "reason": "Synthetic source-width observation"}

    def evidence(self, original, patch):
        updated = original[:patch["start"]] + patch["after"] + original[patch["end"]:]
        return fixture(original, {"easyocr": updated, "surya": original}, image=True)

    def test_single_end_width_change_cannot_break_an_originally_matching_pair(self):
        for left, right, changed in (("(", ")", "）"), ("（", "）", ")"),
                                     ("[", "]", "］"), ("｢", "｣", "」")):
            with self.subTest(pair=left + right):
                original = "天地玄黃" + left + "宇宙洪荒" + right + "寒來暑往"
                patch = self.patch(original, right, changed)
                batch, runs, policy = self.evidence(original, patch)
                frozen = copy.deepcopy((batch, runs, patch))
                old = v3.assess_source_fidelity(batch, [patch], runs=runs, policy=policy)
                self.assertFalse(old["proposals"][0]["blocked"])
                new = current.assess_source_fidelity(batch, [patch], runs=runs, policy=policy)
                verdict = new["proposals"][0]
                self.assertEqual(verdict["reason_codes"], ["paired_punctuation_width_conflict"])
                self.assertEqual(verdict["paired_punctuation_evidence"]["counterpart"], left)
                self.assertEqual((batch, runs, patch), frozen)

    def test_opener_and_nested_pairs_use_the_actual_matching_partner(self):
        original = "天地玄黃(宇宙[洪荒]寒來)暑往秋收"
        for before, after in (("(", "（"), ("]", "］")):
            patch = self.patch(original, before, after)
            result = current._paired_width_conflict(original, patch)
            self.assertIsNotNone(result)
            self.assertEqual(result["source_pair"], "()" if before == "(" else "[]")

    def test_malformed_or_already_mixed_pairs_do_not_invent_a_partner(self):
        for original in ("天地玄黃(宇宙]洪荒)寒來暑往", "天地玄黃（宇宙洪荒)寒來暑往",
                         "天地玄黃宇宙洪荒)寒來暑往"):
            patch = self.patch(original, ")", "）")
            self.assertIsNone(current._paired_width_conflict(original, patch))

    def test_unchanged_punctuation_or_content_cannot_hide_the_changed_endpoint(self):
        source = "天地玄黃(宇宙洪荒[寒來暑往]) 秋收冬藏"
        for before, after in (("])", "]）"), ("]) ", "]） "),
                              ("寒來暑往])", "寒來暑往]）")):
            patch = self.patch(source, before, after)
            batch, runs, policy = self.evidence(source, patch)
            frozen = copy.deepcopy(patch)
            result = current.assess_source_fidelity(batch, [patch], runs=runs, policy=policy)
            verdict = result["proposals"][0]
            with self.subTest(before=before):
                self.assertTrue(verdict["blocked"])
                # The minimal one-codepoint geometry is independent of packaging.
                pair = current._paired_width_conflict(source, patch)
                self.assertEqual(pair["source_pair"], "()")
                self.assertEqual(pair["counterpart_start"], source.index("("))
                self.assertEqual(patch, frozen)

    def test_pair_evidence_requires_exact_snapshot_and_preserves_jointly_changed_pairs(self):
        source = "天地玄黃(宇宙洪荒)寒來暑往"
        patch = self.patch(source, ")", "）")
        self.assertIsNone(current._paired_width_conflict(source.replace(")", "]"), patch))
        patch = self.patch(source, "(宇宙洪荒)", "（宇宙洪荒）")
        self.assertIsNone(current._paired_width_conflict(source, patch))

    def test_other_width_changes_do_not_hide_a_broken_pair(self):
        source = "天地玄黃(宇宙洪荒),寒來暑往"
        patch = self.patch(source, "),", "），")
        self.assertEqual(current._paired_width_conflict(source, patch)["proposed_pair"], "(）")
        source = "天地玄黃(宇宙洪荒),[寒來暑往]秋收冬藏"
        patch = self.patch(source, "(宇宙洪荒),[寒來暑往]", "（宇宙洪荒），[寒來暑往］")
        self.assertEqual(current._paired_width_conflict(source, patch)["source_pair"], "[]")

    def test_good_punctuation_and_han_verdicts_stay_equal_to_frozen_v3(self):
        for before, after in (("，", ","), ("甲", "乙")):
            source = "天地玄黃" + before + "宇宙洪荒"
            patch = self.patch(source, before, after)
            batch, runs, policy = self.evidence(source, patch)
            old = v3.assess_source_fidelity(batch, [patch], runs=runs, policy=policy)
            new = current.assess_source_fidelity(batch, [patch], runs=runs, policy=policy)
            self.assertEqual(new["proposals"], old["proposals"])

    def test_explicit_v3_history_replays_without_new_pair_hold(self):
        source = "天地玄黃(宇宙洪荒)寒來暑往"
        patch = self.patch(source, ")", "）")
        batch, runs, policy = self.evidence(source, patch)
        batch["source_fidelity_contract"] = "source-fidelity-v3"
        old = v3.assess_source_fidelity(batch, [patch], runs=runs, policy=policy)
        record = {"request": batch, "status": "completed", "policy": policy,
                  "proposals": [patch], "metadata": {"prompt_version": "comparison-ai-v10", "source_fidelity": old}}
        frozen = copy.deepcopy(record)
        self.assertEqual(current.verify_source_fidelity(record, runs=runs), old)
        self.assertEqual(current.assess_source_fidelity(batch, [patch], runs=runs, policy=policy,
            contract="source-fidelity-v3"), old)
        self.assertEqual(record, frozen)

    def test_v4_prompt_and_proof_cannot_use_v3_contract(self):
        source = "天地玄黃，宇宙洪荒"
        patch = self.patch(source, "，", ",")
        batch, runs, policy = self.evidence(source, patch)
        batch["source_fidelity_contract"] = "source-fidelity-v4"
        proof = current.assess_source_fidelity(batch, [patch], runs=runs, policy=policy)
        record = {"request": batch, "status": "completed", "policy": policy, "proposals": [patch],
                  "metadata": {"prompt_version": "comparison-ai-v11", "source_fidelity": proof}}
        for version in ("comparison-ai-v11", "comparison-ai-v12"):
            record["metadata"]["prompt_version"] = version
            frozen = copy.deepcopy(record)
            self.assertEqual(current.verify_source_fidelity(record, runs=runs), proof)
            self.assertEqual(record, frozen)
            for field in ("prompt", "contract"):
                bad = copy.deepcopy(record)
                if field == "prompt":
                    bad["metadata"]["prompt_version"] = "comparison-ai-v10"
                else:
                    bad["request"]["source_fidelity_contract"] = "source-fidelity-v3"
                with self.subTest(version=version, field=field), self.assertRaises(current.FidelityError):
                    current.verify_source_fidelity(bad, runs=runs)


if __name__ == "__main__":
    unittest.main()
