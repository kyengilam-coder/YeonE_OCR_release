"""Synthetic coverage for bounded image OCR consensus and variant retention."""
import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from ab_ocr.ocr import RecognitionResult
from yeone_ocr import comparison as core
from yeone_ocr import comparison_unihan as unihan
from yeone_ocr.comparison_refinement import RefinementPolicy
from yeone_ocr.comparison_refinement_windows import _mappings, local_candidates


class WindowConsensusTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="window-consensus-synthetic-")
        self.addCleanup(temporary.cleanup)
        self.source = Path(temporary.name) / "source.png"
        Image.new("RGB", (320, 160), "white").save(self.source)
        self.project = core.ComparisonProject.open(self.source)
        self.before = "甲乙丙丁錯假庚辛壬癸"
        self.after = "甲乙丙丁戊己庚辛壬癸"
        self.policy = RefinementPolicy(version="anchored-refinement-v3")

    def add(self, engine, text, *, complete=True, language="chi_tra", boxes=True):
        raw = RecognitionResult(text=text,
            words=[] if engine == "glmocr" or not boxes else [
                {"text": text, "bbox": [5, 5, 300, 140], "confidence": None}],
            source_path=str(self.source), source_sha256=self.project.source_sha256,
            page_number=1, image_width=320, image_height=160, engine=engine,
            engine_version="synthetic", languages=language, psm=6 if engine == "tesseract" else None,
            duration_seconds=0, engine_metadata={"generation_complete": complete}).to_dict()
        self.project.add_result(raw, request={"profile": "traditional", "direction": "horizontal",
            "dpi": 300, "engine_root": None, "policy": "local-three-engine-v1"})

    def setup_three(self, before=None, after=None):
        self.add("glmocr", self.before if before is None else before)
        for engine in ("easyocr", "surya", "tesseract"):
            self.add(engine, self.after if after is None else after)
        self.project.choose_baseline(1, "glmocr")

    def build(self, snapshot=None):
        page = self.project.page(1)
        return local_candidates(self.project._baseline(page), core.usable_runs(page),
                                self.project.text(1) if snapshot is None else snapshot, self.policy)

    def test_adjacent_errors_split_without_mutating_any_history(self):
        self.setup_three()
        old = copy.deepcopy(self.project.page(1))
        result = self.build()
        self.assertEqual([(x["before"], x["after"]) for x in result], [("錯", "戊"), ("假", "己")])
        self.assertEqual(self.project.page(1), old)
        for item in result:
            self.assertEqual(len(item["consensus_evidence"]["families"]), 3)
            self.assertTrue(all(x["bbox"] for x in item["consensus_evidence"]["supporting"]))

    def test_newline_is_not_copied_into_single_character_patch(self):
        self.setup_three(after=self.after[:5]+"\n"+self.after[5:])
        self.assertEqual([p["after"] for p in self.build()], ["戊", "己"])

    def test_two_families_or_correlated_paddle_models_are_insufficient(self):
        self.add("glmocr", self.before)
        for engine in ("rapidocr", "paddle_server", "surya"):
            self.add(engine, self.after)
        self.project.choose_baseline(1, "glmocr")
        self.assertEqual(self.build(), [])

    def test_addressed_conflicting_reader_vetoes(self):
        self.setup_three()
        self.add("rapidocr", self.before)
        self.assertEqual(self.build(), [])

    def test_each_supporting_family_needs_real_box_coverage(self):
        self.setup_three()
        self.add("tesseract", self.after, boxes=False)
        self.assertEqual(self.build(), [])

    def test_incomplete_and_unsupported_readers_do_not_vote(self):
        self.setup_three()
        self.add("surya", self.after, complete=False)
        self.assertEqual(self.build(), [])
        self.add("surya", self.after)
        self.add("easyocr", self.after, language="kor")
        self.assertEqual(self.build(), [])

    def test_human_target_and_context_changes_are_not_overwritten(self):
        self.setup_three()
        self.assertEqual(self.build(self.before.replace("錯", "人")), [])
        self.assertEqual(self.build(self.before.replace("丁", "人")), [])

    def test_unavailable_or_corrupt_official_dictionary_fails_closed(self):
        self.setup_three()
        with patch("yeone_ocr.comparison_refinement_windows.unihan.manifest", return_value={"available": False}):
            self.assertEqual(self.build(), [])
        with patch.object(unihan, "DATA", self.source):
            unihan._load.cache_clear()
            self.assertFalse(unihan.manifest()["available"])
        unihan._load.cache_clear()
        self.assertTrue(unihan.manifest()["available"])

    def test_variants_compatibility_and_variation_selectors_remain_protected(self):
        for before, after in (("清", "淸"), ("汉", "漢"), ("神", "神"), ("錯\ufe00", "戊\ufe00")):
            with self.subTest(before=before, after=after):
                self.setup_three("甲乙丙丁"+before+"己庚辛壬癸", "甲乙丙丁"+after+"己庚辛壬癸")
                self.assertEqual(self.build(), [])

    def test_shifted_form_relationship_cannot_be_split_around_protection(self):
        self.setup_three("甲乙丙丁吾清己庚辛壬癸", "甲乙丙丁淸人己庚辛壬癸")
        self.assertEqual(self.build(), [])

    def test_shared_reading_is_caution_not_an_equivalence_conversion(self):
        self.assertTrue(unihan.shared_reading("師", "獅"))
        self.setup_three("甲乙丙丁師己庚辛壬癸", "甲乙丙丁獅己庚辛壬癸")
        self.assertEqual(self.build(), [])

    def test_eight_character_bound_excludes_wide_rewrites(self):
        before = "甲乙丙丁"+"錯"*9+"庚辛壬癸"
        after = "甲乙丙丁"+"戊"*9+"庚辛壬癸"
        self.assertFalse(any(x["method"] != "equal-veto-only" for x in _mappings(before, after, 4).values()))

    def test_repeated_outer_address_is_not_a_new_vote(self):
        a, b = "甲乙丙丁錯庚辛壬癸", "甲乙丙丁戊庚辛壬癸"
        self.assertFalse(any(x["method"] != "equal-veto-only" for x in _mappings(a+a, b+b, 4).values()))

    def test_official_data_is_versioned_and_spoof_similarity_is_not_equivalence(self):
        self.assertTrue(unihan.manifest()["available"])
        self.assertEqual(unihan.manifest()["unicode_version"], "17.0.0")
        self.assertNotIn("kSpoofingVariant", unihan.manifest()["properties"])
        self.assertTrue(unihan.relationship("兎", "兔"))
        self.assertFalse(unihan.relationship("未", "末"))


if __name__ == "__main__":
    unittest.main()
