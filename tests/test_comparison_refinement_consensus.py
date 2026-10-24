"""Independent-family literal agreement and unchanged human history."""
import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from ab_ocr.ocr import RecognitionResult
from yeone_ocr import comparison as core
from yeone_ocr.comparison_refinement import (
    RefinementPolicy, accept_proposal, build_candidates, evaluate_proposals,
)


class ConsensusTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="consensus-synthetic-")
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        self.source = self.folder / "source.png"
        Image.new("RGB", (320, 160), "white").save(self.source)
        self.project = core.ComparisonProject.open(self.source)
        self.before = "甲乙丙丁錯己庚辛壬癸"
        self.after = "甲乙丙丁戊己庚辛壬癸"
        self.policy = RefinementPolicy(version="anchored-refinement-v2")

    def add(self, engine, text, *, complete=True, language="chi_tra"):
        raw = RecognitionResult(text=text,
            words=[] if engine == "glmocr" else [{"text": text, "bbox": [5, 5, 300, 140], "confidence": None}],
            source_path=str(self.source), source_sha256=self.project.source_sha256,
            page_number=1, image_width=320, image_height=160, engine=engine,
            engine_version="synthetic", languages=language, psm=6 if engine == "tesseract" else None,
            duration_seconds=0, engine_metadata={"generation_complete": complete}).to_dict()
        self.project.add_result(raw, request={"profile": "traditional", "direction": "horizontal",
            "dpi": 300, "engine_root": None, "policy": "local-three-engine-v1"})

    def build(self):
        self.project.choose_baseline(1, "glmocr")
        return build_candidates(self.project, 1, policy=self.policy)

    def test_glm_baseline_can_receive_two_family_han_proposal(self):
        self.add("glmocr", self.before)
        self.add("easyocr", self.after[:4] + " " + self.after[4:])
        self.add("surya", self.after)
        raw = copy.deepcopy(self.project.page(1)["runs"])
        items = self.build()
        self.assertEqual([(p["before"], p["after"]) for p in items], [("錯", "戊")])
        self.assertEqual(items[0]["consensus_evidence"]["families"], ["easyocr", "surya"])
        self.assertEqual(evaluate_proposals(self.project, 1)["text"], self.after)
        self.assertEqual(self.project.text(1), self.before)
        self.assertEqual(self.project.page(1)["runs"], raw)
        self.assertEqual(self.project.page(1)["edits"], [])
        item = items[0]
        accept_proposal(self.project, 1, item["review_id"], item["id"])
        path = self.folder / "project.json"
        self.project.save(path)
        reopened = core.ComparisonProject.load(path)
        self.assertEqual(reopened.text(1), self.after)
        reopened.undo_edit(1)
        self.assertEqual(reopened.text(1), self.before)
        reopened.save()
        self.assertEqual(core.ComparisonProject.load(path).page(1)["runs"], raw)

    def test_paddle_mobile_and_server_are_one_family(self):
        self.add("glmocr", self.before)
        self.add("rapidocr", self.after)
        self.add("paddle_server", self.after)
        self.assertEqual(self.build(), [])

    def test_addressed_conflicting_reader_vetoes(self):
        self.add("glmocr", self.before)
        self.add("easyocr", self.after)
        self.add("surya", self.after)
        self.add("tesseract", self.before)
        self.assertEqual(self.build(), [])

    def test_variant_compatibility_and_variation_selector_protected(self):
        for before, after in [("清", "淸"), ("汉", "漢"), ("神", "神"), ("神", "神"), ("錯\ufe00", "戊\ufe00")]:
            with self.subTest(before=before, after=after):
                self.add("glmocr", "甲乙丙丁" + before + "己庚辛壬癸")
                self.add("easyocr", "甲乙丙丁" + after + "己庚辛壬癸")
                self.add("surya", "甲乙丙丁" + after + "己庚辛壬癸")
                self.assertEqual(self.build(), [])

    def test_incomplete_and_unsupported_models_do_not_vote(self):
        self.add("glmocr", self.before)
        self.add("easyocr", self.after, language="kor")
        self.add("surya", self.after)
        self.assertEqual(self.build(), [])
        self.add("easyocr", self.after)
        self.add("surya", self.after, complete=False)
        self.assertEqual(self.build(), [])

    def test_unavailable_variant_dictionary_blocks_han(self):
        self.add("glmocr", self.before)
        self.add("easyocr", self.after)
        self.add("surya", self.after)
        with patch("yeone_ocr.comparison_refinement_consensus.fidelity.dictionary_manifest",
                   return_value={"available": False}):
            self.assertEqual(self.build(), [])

    def test_shifted_variant_inside_multicharacter_replacement_is_protected(self):
        self.add("glmocr", "甲乙丙丁吾清己庚辛壬癸")
        self.add("easyocr", "甲乙丙丁淸人己庚辛壬癸")
        self.add("surya", "甲乙丙丁淸人己庚辛壬癸")
        self.assertEqual(self.build(), [])

    def test_human_corrected_target_is_not_overwritten(self):
        self.add("glmocr", self.before)
        self.add("easyocr", self.after)
        self.add("surya", self.after)
        self.build()
        self.project.set_text(1, self.before.replace("錯", "人"))
        self.assertEqual(build_candidates(self.project, 1, policy=self.policy), [])

    def test_hangul_literal_consensus(self):
        before, after = "가나다라틀바사아자차", "가나다라마바사아자차"
        self.add("glmocr", before, language="kor")
        self.add("surya", after, language="kor")
        self.add("tesseract", after, language="kor")
        self.assertEqual([(p["kind"], p["before"], p["after"]) for p in self.build()], [("hangul", "틀", "마")])

    def test_local_width_proposal_cannot_break_existing_bracket_pair(self):
        before = "甲乙丙丁(戊己庚辛)壬癸子丑"
        self.add("glmocr", before)
        self.add("easyocr", before.replace(")", "）"))
        self.assertEqual(self.build(), [])

    def test_v3_requires_three_han_families_and_retains_separate_history(self):
        self.policy = RefinementPolicy(version="anchored-refinement-v3")
        self.add("glmocr", self.before)
        self.add("easyocr", self.after)
        self.add("surya", self.after)
        self.assertEqual(self.build(), [])
        self.add("tesseract", self.after)
        original_runs = copy.deepcopy(self.project.page(1)["runs"])
        items = self.build()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["consensus_evidence"]["minimum_independent_families"], 3)
        self.assertEqual(self.project.text(1), self.before)
        self.assertEqual(self.project.page(1)["edits"], [])
        accept_proposal(self.project, 1, items[0]["review_id"], items[0]["id"])
        path = self.folder / "v3-project.json"
        self.project.save(path)
        reopened = core.ComparisonProject.load(path)
        self.assertEqual(reopened.text(1), self.after)
        reopened.undo_edit(1)
        reopened.save()
        restored = core.ComparisonProject.load(path)
        self.assertEqual(restored.text(1), self.before)
        self.assertEqual(restored.page(1)["runs"], original_runs)

    def test_v3_three_engines_in_two_families_do_not_suffice_for_han(self):
        self.policy = RefinementPolicy(version="anchored-refinement-v3")
        self.add("glmocr", self.before)
        self.add("rapidocr", self.after)
        self.add("paddle_server", self.after)
        self.add("surya", self.after)
        self.assertEqual(self.build(), [])

    def test_v3_keeps_two_family_hangul_candidate(self):
        self.policy = RefinementPolicy(version="anchored-refinement-v3")
        self.add("glmocr", "가나다라틀바사아자차", language="kor")
        self.add("surya", "가나다라마바사아자차", language="kor")
        self.add("tesseract", "가나다라마바사아자차", language="kor")
        items = self.build()
        self.assertEqual([(p["before"], p["after"]) for p in items], [("틀", "마")])
        self.assertEqual(items[0]["consensus_evidence"]["minimum_independent_families"], 2)

    def test_v3_three_family_agreement_does_not_override_variant_protection(self):
        self.policy = RefinementPolicy(version="anchored-refinement-v3")
        for before, after in (("清", "淸"), ("漢", "汉"), ("神", "神"), ("錯\ufe00", "戊\ufe00")):
            with self.subTest(before=before, after=after):
                self.add("glmocr", "甲乙丙丁" + before + "己庚辛壬癸")
                for engine in ("easyocr", "surya", "tesseract"):
                    self.add(engine, "甲乙丙丁" + after + "己庚辛壬癸")
                self.assertEqual(self.build(), [])


if __name__ == "__main__":
    unittest.main()
