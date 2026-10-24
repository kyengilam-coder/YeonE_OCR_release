"""Synthetic independent-family punctuation evidence and conservative holds."""
import copy
import unittest

from tests import test_comparison_refinement_v2 as fixtures
from yeone_ocr import comparison as core
from yeone_ocr.comparison_refinement_guarded import assessed_candidates, local_candidates


class GuardedPunctuationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.PunctuationAddressTests()
        self.fixture.setUp()
        self.policy = self.fixture.policy

    def readings(self, before=",", after="，", others=()):
        text = "天地玄黃" + before + " 宇宙洪荒"
        donor = "天地玄黃 " + after + "宇宙洪荒"
        base, runs = self.fixture.readings(text, donor)
        for engine, mark in others:
            run = copy.deepcopy(runs["easyocr"])
            run.update(id=engine + "-run", engine=engine)
            run["raw"]["text"] = "天地玄黃" + mark + "宇宙洪荒"
            run["raw"]["words"] = []  # Independent support does not invent geometry.
            run["raw_sha256"] = core._digest(run["raw"])
            runs[engine] = run
        return base, runs

    def assess(self, *args, **kwargs):
        base, runs = self.readings(*args, **kwargs)
        saved = copy.deepcopy((base, runs))
        result = assessed_candidates(base, runs, base["raw"]["text"], self.policy)
        self.assertEqual((base, runs), saved)
        self.assertEqual(len(result), 1)
        return result[0]

    def test_single_easy_after_is_held_and_reasons_are_preserved(self):
        item = self.assess()
        self.assertTrue(item["guard_evidence"]["blocked"])
        self.assertIn("insufficient-independent-glyph-support", item["guard_evidence"]["reason_codes"])
        base, runs = self.readings()
        self.assertEqual(local_candidates(base, runs, base["raw"]["text"], self.policy), [])

    def test_width_one_exact_plus_two_glyph_families_tie_is_allowed(self):
        item = self.assess(others=(("surya", ","),))
        evidence = item["guard_evidence"]
        self.assertFalse(evidence["blocked"])
        self.assertEqual(evidence["before_families"], ["surya"])
        self.assertEqual(evidence["after_families"], ["easyocr"])
        self.assertEqual(evidence["glyph_families"], ["easyocr", "surya"])
        self.assertEqual(evidence["geometry"]["bbox"], [12, 18, 300, 60])
        self.assertFalse(evidence["image_or_AI_proof_created"])

    def test_width_more_before_than_after_families_is_held(self):
        item = self.assess(others=(("surya", ","), ("tesseract", ",")))
        self.assertIn("independent-before-width-majority", item["guard_evidence"]["reason_codes"])

    def test_width_exact_after_two_families_is_allowed(self):
        item = self.assess(others=(("surya", "，"), ("tesseract", ",")))
        self.assertFalse(item["guard_evidence"]["blocked"])

    def test_baseline_is_never_an_independent_vote(self):
        item = self.assess()
        baseline = next(row for row in item["guard_evidence"]["observations"] if row["engine"] == "glmocr")
        self.assertEqual(baseline["exclusion"], "baseline-family")
        self.assertEqual(item["guard_evidence"]["before_families"], [])

    def test_rapid_and_paddle_family_is_counted_once_and_may_oppose_itself(self):
        item = self.assess(others=(("rapidocr", ","), ("paddle_server", ",")))
        self.assertFalse(item["guard_evidence"]["blocked"])
        self.assertEqual(item["guard_evidence"]["before_families"], ["paddle"])
        item = self.assess(others=(("rapidocr", "，"), ("paddle_server", ",")))
        self.assertEqual(item["guard_evidence"]["after_families"], ["easyocr", "paddle"])
        self.assertEqual(item["guard_evidence"]["before_families"], ["paddle"])

    def test_addressed_other_kind_or_count_is_a_veto(self):
        for mark in (";", "", ",,", "。"):
            with self.subTest(mark=mark):
                item = self.assess(others=(("surya", mark),))
                self.assertIn("addressed-punctuation-glyph-or-count-conflict", item["guard_evidence"]["reason_codes"])

    def test_kind_change_needs_two_exact_families_and_no_original_vote(self):
        self.assertTrue(self.assess(".", "。", (("surya", "."),))["guard_evidence"]["blocked"])
        item = self.assess(".", "。", (("surya", "。"),))
        self.assertFalse(item["guard_evidence"]["blocked"])
        self.assertFalse(item["guard_evidence"]["width_only"])

    def test_incomplete_incompatible_and_inactive_votes_are_not_used(self):
        for condition in ("incomplete", "incompatible", "inactive", "bad-hash"):
            with self.subTest(condition=condition):
                base, runs = self.readings(others=(("surya", "，"),))
                run = runs["surya"]
                if condition == "incomplete":
                    run["raw"]["engine_metadata"]["generation_complete"] = False
                elif condition == "incompatible":
                    run["raw"]["image_width"] = 321
                elif condition == "inactive":
                    run["active"] = False
                if condition != "bad-hash":
                    run["raw_sha256"] = core._digest(run["raw"])
                else:
                    run["raw_sha256"] = "0" * 64
                result = assessed_candidates(base, runs, base["raw"]["text"], self.policy)
                self.assertTrue(result[0]["guard_evidence"]["blocked"])

    def test_wrong_occurrence_and_ambiguous_content_never_vote(self):
        base, runs = self.readings(others=(("surya", "，"),))
        text = "天地玄黃，宇宙洪荒天地玄黃，宇宙洪荒"
        runs["surya"]["raw"]["text"] = text
        runs["surya"]["raw_sha256"] = core._digest(runs["surya"]["raw"])
        result = assessed_candidates(base, runs, base["raw"]["text"], self.policy)
        self.assertTrue(result[0]["guard_evidence"]["blocked"])
        observation = next(row for row in result[0]["guard_evidence"]["observations"] if row["engine"] == "surya")
        self.assertFalse(observation["addressable"])

    def test_entire_punctuation_sequence_is_checked(self):
        base, runs = self.readings(",;", "，;", (("surya", ",:"),))
        result = assessed_candidates(base, runs, base["raw"]["text"], self.policy)
        self.assertTrue(result[0]["guard_evidence"]["blocked"])
        self.assertEqual(result[0]["guard_evidence"]["conflict_families"], ["surya"])

    def test_replay_identity_keeps_parent_and_all_literal_evidence(self):
        item = self.assess(others=(("surya", ","),))
        evidence = item["guard_evidence"]
        self.assertNotEqual(item["id"], evidence["parent_proposal_id"])
        self.assertEqual(item["id"], core._digest({k: v for k, v in item.items() if k != "id"}))
        addressed = [row for row in evidence["observations"] if row["addressable"]]
        self.assertEqual(len(addressed), 2)
        self.assertTrue(all(row["raw_sha256"] and row["target_literal"] in (",", "，") for row in addressed))


if __name__ == "__main__":
    unittest.main()
