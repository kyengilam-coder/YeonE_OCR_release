"""Exactness, Unicode preservation, and honest error reporting regressions."""
from __future__ import annotations

import json
import random
import unicodedata
import unittest

from ab_ocr.evaluation import (ComparisonLimitError, NormalizationPolicy,
                               classify_character, compare_texts, normalize_text)


def oracle(reference, hypothesis):
    """Independent full integer matrix, deliberately without production optimizations."""
    matrix = [[0] * (len(hypothesis) + 1) for _ in range(len(reference) + 1)]
    for i in range(len(reference) + 1):
        matrix[i][0] = i
    for j in range(len(hypothesis) + 1):
        matrix[0][j] = j
    for i, rchar in enumerate(reference, 1):
        for j, hchar in enumerate(hypothesis, 1):
            matrix[i][j] = min(matrix[i - 1][j] + 1, matrix[i][j - 1] + 1,
                               matrix[i - 1][j - 1] + int(rchar != hchar))
    return matrix[-1][-1]


class ExactCERTests(unittest.TestCase):
    def assert_alignment(self, reference, hypothesis, report):
        self.assertFalse(report["errors_truncated"])
        rpos = hpos = 0
        for error in report["errors"]:
            ri, hi = error["reference_position"], error["hypothesis_position"]
            self.assertEqual(reference[rpos:ri], hypothesis[hpos:hi])
            self.assertEqual(reference[ri:ri + len(error["reference"])], error["reference"])
            self.assertEqual(hypothesis[hi:hi + len(error["hypothesis"])], error["hypothesis"])
            self.assertEqual(error["operation"], "insertion" if not error["reference"]
                             else "deletion" if not error["hypothesis"] else "substitution")
            rpos, hpos = ri + len(error["reference"]), hi + len(error["hypothesis"])
        self.assertEqual(reference[rpos:], hypothesis[hpos:])

    def test_random_short_inputs_match_independent_oracle_and_valid_alignment(self):
        randomizer = random.Random(20261016)
        alphabet = "가나天地未末 ab\n𠮷\ufe00"
        for _ in range(600):
            reference = "".join(randomizer.choices(alphabet, k=randomizer.randrange(14)))
            hypothesis = "".join(randomizer.choices(alphabet, k=randomizer.randrange(14)))
            with self.subTest(reference=reference, hypothesis=hypothesis):
                report = compare_texts(reference, hypothesis)["raw"]
                self.assertEqual(report["distance"], oracle(reference, hypothesis))
                self.assertEqual(report["distance"], sum(report[k] for k in ("substitutions", "deletions", "insertions")))
                self.assertEqual(report["distance"], sum(item["count"] for item in report["confusions"]))
                self.assert_alignment(reference, hypothesis, report)

    def test_repeats_transpositions_and_ties_have_deterministic_optimal_alignment(self):
        examples = [("aaaaa", "aaa"), ("ababab", "bababa"), ("未末未末", "末未末未"),
                    ("abc", "abXYZc"), ("가가나다다", "가나다"), ("ab", "ba")]
        for reference, hypothesis in examples:
            report = compare_texts(reference, hypothesis)
            self.assertEqual(report, compare_texts(reference, hypothesis))
            self.assertEqual(report["raw"]["distance"], oracle(reference, hypothesis))
            self.assert_alignment(reference, hypothesis, report["raw"])

    def test_cer_can_exceed_one_and_blank_reference_is_undefined(self):
        report = compare_texts("가", "나라다마")["raw"]
        self.assertEqual((report["distance"], report["cer"], report["cer_percent"]), (4, 4.0, 400.0))
        for hypothesis in ("", "天地"):
            report = compare_texts("", hypothesis)["raw"]
            self.assertIsNone(report["cer"])
            self.assertIsNone(report["cer_percent"])
            self.assertEqual(report["insertions"], len(hypothesis))
        self.assertEqual(compare_texts("天地", "")["raw"]["cer"], 1)

    def test_errors_are_zero_based_codepoint_offsets_with_local_context(self):
        reference, hypothesis = "서문𠮷天地끝", "서문𠮷天池끝"
        report = compare_texts(reference, hypothesis)["raw"]
        error = report["errors"][0]
        self.assertEqual((error["reference_position"], error["hypothesis_position"]), (4, 4))
        self.assertEqual((error["reference"], error["hypothesis"]), ("地", "池"))
        self.assertEqual(error["reference_context"], reference)
        self.assertEqual(error["hypothesis_context"], hypothesis)

    def test_detail_limit_does_not_change_counts_confusions_or_first_error_positions(self):
        complete = compare_texts("天地山川", "abcd")["raw"]
        limited = compare_texts("天地山川", "abcd", max_errors=2)["raw"]
        none = compare_texts("天地山川", "abcd", max_errors=0)["raw"]
        self.assertEqual(limited["errors"], complete["errors"][:2])
        self.assertEqual(none["errors"], [])
        for report in (limited, none):
            self.assertTrue(report["errors_truncated"])
            self.assertEqual(report["distance"], 4)
            self.assertEqual(report["confusions"], complete["confusions"])
            self.assertEqual(report["by_script"], complete["by_script"])

    def test_cell_bound_rejects_without_an_approximate_result(self):
        with self.assertRaises(ComparisonLimitError):
            compare_texts("가" * 20, "나" * 20, max_cells=440)
        report = compare_texts("가" * 20, "나" * 20, max_cells=441)["raw"]
        self.assertEqual(report["distance"], 20)
        self.assertEqual(report["alignment"]["computed_cells"], 441)

    def test_exact_equal_edges_and_empty_middles_do_not_need_a_large_grid(self):
        edge = "天地" * 1000
        report = compare_texts(edge + "가" + edge, edge + "나" + edge, max_cells=4)["raw"]
        self.assertEqual(report["distance"], 1)
        self.assertEqual(report["errors"][0]["reference_position"], len(edge))
        self.assertEqual(compare_texts(edge, edge, max_cells=1)["raw"]["distance"], 0)
        self.assertEqual(compare_texts(edge, "", max_cells=1, max_errors=0)["raw"]["deletions"], len(edge))

    def test_report_is_json_serializable_and_raw_normalized_are_independent(self):
        result = compare_texts("𠮷가", "𠮷나")
        self.assertEqual(json.loads(json.dumps(result, ensure_ascii=False)), result)
        result["normalized"]["errors"][0]["reference"] = "changed"
        self.assertEqual(result["raw"]["errors"][0]["reference"], "가")


class NormalizationTests(unittest.TestCase):
    def test_default_does_not_normalize_hangul_jamo_or_whitespace(self):
        reference, hypothesis = "각 한", "\u1100\u1161\u11a8\t한"
        result = compare_texts(reference, hypothesis)
        self.assertGreater(result["raw"]["distance"], 0)
        self.assertEqual(result["raw"], result["normalized"])
        self.assertEqual(result["policy"]["unicode_form"], "none")

    def test_explicit_nfc_composes_hangul_while_raw_score_is_retained(self):
        result = compare_texts("각", "\u1100\u1161\u11a8", NormalizationPolicy(unicode_form="NFC"))
        self.assertEqual(result["raw"]["distance"], 3)
        self.assertEqual(result["normalized"]["distance"], 0)
        self.assertEqual(result["normalized"]["hypothesis_length"], 1)

    def test_all_cjk_compatibility_block_codepoints_are_preserved_under_nfc(self):
        characters = "".join(chr(code) for lo, hi in ((0xF900, 0xFAFF), (0x2F800, 0x2FA1F))
                             for code in range(lo, hi + 1))
        self.assertEqual(normalize_text(characters, {"unicode_form": "NFC"}), characters)
        self.assertNotEqual(unicodedata.normalize("NFC", "\uf900"), "\uf900")
        result = compare_texts("\uf900", "豈", {"unicode_form": "NFC"})
        self.assertEqual(result["normalized"]["substitutions"], 1)

    def test_variation_selectors_and_simplified_traditional_variants_are_not_folded(self):
        selectors = "".join(chr(code) for code in [*range(0xFE00, 0xFE10), *range(0xE0100, 0xE01F0),
                                                   0x180B, 0x180C, 0x180D, 0x180F])
        self.assertEqual(normalize_text("邊" + selectors, {"unicode_form": "NFC"}), "邊" + selectors)
        self.assertEqual(compare_texts("邊\U000e0100", "边", {"unicode_form": "NFC"})["normalized"]["distance"], 2)
        self.assertEqual(normalize_text("\u3131\u314f", {"unicode_form": "NFC"}), "\u3131\u314f")

    def test_whitespace_modes_have_recorded_edge_behavior(self):
        text = "\t가\u3000\n地  "
        self.assertEqual(normalize_text(text), text)
        self.assertEqual(normalize_text(text, {"whitespace": "collapse"}), " 가 地 ")
        self.assertEqual(normalize_text(text, {"whitespace": "remove"}), "가地")
        result = compare_texts("가地", text, {"whitespace": "remove"})
        self.assertEqual(result["normalized"]["distance"], 0)
        self.assertGreater(result["raw"]["distance"], 0)
        self.assertIn("string edges", result["policy"]["collapse_rule"])
        self.assertEqual(normalize_text("\ufeff가", {"whitespace": "remove"}), "\ufeff가")

    def test_serialized_policy_round_trip_and_tampering_rejection(self):
        policy = NormalizationPolicy("NFC", "collapse")
        self.assertEqual(compare_texts("가", "가", policy), compare_texts("가", "가", policy.as_dict()))
        for invalid in ({"unicode_form": "NFKC"}, {"whitespace": "ignore"},
                        {"silent_hanja_fold": True}, {**policy.as_dict(), "cjk_compatibility_ideographs": "fold"}):
            with self.assertRaises(ValueError):
                compare_texts("가", "가", invalid)

    def test_argument_validation_rejects_implicit_conversion_and_invalid_limits(self):
        for reference, hypothesis in ((None, ""), (b"x", ""), ("", 1)):
            with self.assertRaises(TypeError):
                compare_texts(reference, hypothesis)
        for options in ({"max_cells": 0}, {"max_cells": True}, {"max_cells": 1.5},
                        {"max_errors": -1}, {"max_errors": True}):
            with self.assertRaises(ValueError):
                compare_texts("가", "나", **options)


class CharacterClassificationTests(unittest.TestCase):
    def test_hangul_hanja_extensions_and_nonideographic_lookalikes(self):
        for character in "가\u1100\u1161\u11a8ㄱ\ua960\ud7b0\uffa1":
            self.assertEqual(classify_character(character), "hangul")
        for character in "天㐀𠮷\uf900\U0002f800":
            self.assertEqual(classify_character(character), "hanja")
        # Include later extensions whenever this Python's database assigns them.
        for code in (0x2A700, 0x2B740, 0x2B820, 0x2CEB0, 0x30000, 0x31350, 0x2EBF0):
            character = chr(code)
            if unicodedata.name(character, "").startswith("CJK UNIFIED IDEOGRAPH-"):
                self.assertEqual(classify_character(character), "hanja")
        for character in ("\u2f00", "〇", "\U000f0000", "\u0378"):
            self.assertNotEqual(classify_character(character), "hanja")
        self.assertEqual(classify_character("\U000e0100"), "variation_selector")

    def test_cross_script_errors_are_attributed_conservatively(self):
        result = compare_texts("가天", "a나!")["raw"]
        self.assertEqual(result["by_script"]["hangul"]["substitutions"], 1)
        self.assertEqual(result["by_script"]["hanja"]["substitutions"], 1)
        self.assertEqual(sum(row["insertions"] for row in result["by_script"].values()), 1)
        self.assertEqual(sum(row["substitutions_to"] for row in result["by_script"].values()), 2)
        self.assertNotIn("cer", result["by_script"]["hanja"])


if __name__ == "__main__":
    unittest.main()
