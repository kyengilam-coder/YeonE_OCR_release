"""Synthetic punctuation-address tests; no source documents or OCR calls."""
import copy
from types import SimpleNamespace
import unittest

from yeone_ocr import comparison as core
from yeone_ocr.comparison_refinement_v2 import local_candidates


class PunctuationAddressTests(unittest.TestCase):
    def setUp(self):
        self.policy = SimpleNamespace(anchor_chars=4, punctuation_max_chars=4,
                                      max_input_chars=20000, max_candidates=2000)

    def readings(self, before, after, *, boxes=True, complete=True):
        def reading(engine, text):
            raw = {"text": text, "words": ([{"text": text, "bbox": [12, 18, 300, 60]}]
                if engine == "easyocr" and boxes else []), "source_sha256": "s" * 64,
                "page_number": 1, "image_width": 320, "image_height": 160, "languages": "chi_tra",
                "engine_metadata": {"generation_complete": complete, "bbox_granularity": "line"}}
            return {"id": engine + "-run", "engine": engine, "raw": raw, "raw_sha256": core._digest(raw),
                "request": {"profile": "traditional", "direction": "horizontal", "dpi": 300}}
        base, donor = reading("glmocr", before), reading("easyocr", after)
        return base, {"glmocr": base, "easyocr": donor}

    def candidates(self, before, after, **kwargs):
        base, runs = self.readings(before, after, **kwargs)
        original = copy.deepcopy((base, runs))
        result = local_candidates(base, runs, before, self.policy)
        self.assertEqual((base, runs), original)
        return result

    def test_spacing_changes_only_locate_and_never_change_original_whitespace(self):
        before, after = "天地玄黃, 宇宙洪荒", "天 地玄 黃，宇 宙洪荒"
        items = self.candidates(before, after)
        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual((item["start"], item["end"], item["before"], item["after"]), (4, 5, ",", "，"))
        self.assertEqual(before[:item["start"]] + item["after"] + before[item["end"]:], "天地玄黃， 宇宙洪荒")
        self.assertEqual(item["evidence"]["bbox"], [12, 18, 300, 60])
        self.assertEqual(item["address_evidence"]["punctuation_count"], 1)

    def test_different_punctuation_type_is_copied_literally(self):
        item = self.candidates("天地玄黃.宇宙洪荒", "天地玄黃 。宇宙洪荒")[0]
        self.assertEqual((item["before"], item["after"]), (".", "。"))

    def test_multi_mark_gap_is_mapped_by_unchanged_count_and_order(self):
        items = self.candidates("天地玄黃,;宇宙洪荒", "天地玄黃 ，；宇宙洪荒")
        self.assertEqual([(p["baseline_start"], p["after"]) for p in items], [(4, "，"), (5, "；")])
        self.assertEqual([p["address_evidence"]["punctuation_ordinal"] for p in items], [0, 1])

    def test_no_insert_delete_or_content_substitution(self):
        for after in ("天地玄黃宇宙洪荒", "天地玄黃，；宇宙洪荒", "天地玄黃，字宇宙洪荒", "天地玄黃，宇宙洪星"):
            with self.subTest(after=after):
                self.assertEqual(self.candidates("天地玄黃,宇宙洪荒", after), [])

    def test_repeated_content_anchors_abstain_without_using_after_as_a_locator(self):
        before = "天地玄黃,宇宙洪荒天地玄黃;日月盈昃"
        after = "天地玄黃，宇宙洪荒天地玄黃;日月盈昃"
        self.assertEqual(self.candidates(before, after), [])

    def test_geometry_incomplete_and_incompatible_inputs_abstain(self):
        before, after = "天地玄黃,宇宙洪荒", "天地玄黃，宇宙洪荒"
        self.assertEqual(self.candidates(before, after, boxes=False), [])
        self.assertEqual(self.candidates(before, after, complete=False), [])
        base, runs = self.readings(before, after)
        runs["easyocr"]["raw"]["image_width"] = 321
        self.assertEqual(local_candidates(base, runs, before, self.policy), [])

    def test_human_target_or_anchor_edits_are_protected(self):
        before, after = "天地玄黃,宇宙洪荒", "天地玄黃，宇宙洪荒"
        base, runs = self.readings(before, after)
        for current in ("天地玄黃;宇宙洪荒", "天地玄黃,宇宙洪星"):
            self.assertEqual(local_candidates(base, runs, current, self.policy), [])

    def test_non_BMP_and_variation_selector_indices_remain_codepoint_exact(self):
        before, after = "𠀀地玄黃,宇宙洪荒", "𠀀地玄黃，宇宙洪荒"
        item = self.candidates(before, after)[0]
        self.assertEqual((item["start"], item["end"]), (4, 5))
        before, after = "天地玄黃\U000E0100,宇宙洪荒", "天地玄黃\U000E0100，宇宙洪荒"
        item = self.candidates(before, after)[0]
        self.assertEqual((item["start"], item["end"]), (5, 6))
        self.assertEqual(item["before"], ",")

    def test_no_candidate_replaces_Han_Hangul_VS_or_just_whitespace(self):
        for before, after in (("天地玄黃甲宇宙洪荒", "天地玄黃乙宇宙洪荒"),
                              ("天地玄黃한宇宙洪荒", "天地玄黃글宇宙洪荒"),
                              ("天地玄黃 宇宙洪荒", "天地玄黃宇宙洪荒")):
            self.assertEqual(self.candidates(before, after), [])

    def test_punctuation_with_attached_variation_selector_is_not_rebased(self):
        for selector in ("\ufe0f", "\U000E0100"):
            with self.subTest(selector=ord(selector)):
                self.assertEqual(self.candidates("天地玄黃," + selector + "宇宙洪荒",
                                                 "天地玄黃，" + selector + "宇宙洪荒"), [])

    def test_replay_is_deterministic_and_source_change_changes_provenance(self):
        before, after = "天地玄黃, 宇宙洪荒", "天地玄黃，宇宙洪荒"
        one = self.candidates(before, after)
        self.assertEqual(one, self.candidates(before, after))
        two = self.candidates(before, "天地玄黃 ，宇宙洪荒")
        self.assertEqual(one[0]["after"], two[0]["after"])
        self.assertNotEqual(one[0]["id"], two[0]["id"])


if __name__ == "__main__":
    unittest.main()
