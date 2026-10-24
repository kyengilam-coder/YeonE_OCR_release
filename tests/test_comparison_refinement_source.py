"""Visible source metadata holds flow through draft, UI-facing state and history."""
import copy
import json
from pathlib import Path
import tempfile
import unittest

import pymupdf

from ab_ocr import ocr
from yeone_ocr import comparison as core
from yeone_ocr import comparison_refinement as refinement
from yeone_ocr.comparison_refinement_source import record_pdf_check


class SourceCheckLifecycleTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="pdf-punctuation-lifecycle-")
        self.addCleanup(temp.cleanup)
        self.folder = Path(temp.name)
        self.source = self.folder / "source.pdf"
        with pymupdf.open() as doc:
            page = doc.new_page(width=240, height=120)
            page.insert_text((20, 45), "ABCD:EFGH", fontsize=16)
            self.box = [float(v) * 300 / 72 for v in page.get_text("blocks")[0][:4]]
            pix = page.get_pixmap(dpi=300)
            self.width, self.height = pix.width, pix.height
            doc.save(self.source)
        self.project = core.ComparisonProject.open(self.source)

    def readings(self, before=":", after="："):
        self.before, self.after = "ABCD" + before + "EFGH", "ABCD" + after + "EFGH"
        for engine, text in (("glmocr", self.before), ("easyocr", self.after), ("surya", self.before)):
            result = ocr.RecognitionResult(text=text,
                words=[] if engine == "glmocr" else [{"text": text, "bbox": self.box, "confidence": None}],
                source_path=str(self.source), source_sha256=self.project.source_sha256, page_number=1,
                image_width=self.width, image_height=self.height, engine_version="synthetic",
                languages="chi_tra", psm=None, duration_seconds=0, engine=engine,
                engine_metadata={"generation_complete": True, "bbox_granularity": "line"})
            self.project.add_result(result, request={"profile": "traditional", "direction": "horizontal", "dpi": 300,
                                                    "policy": "local-three-engine-v1"})
        self.project.choose_baseline(1, "glmocr")
        return self.project.page(1)

    def build(self):
        items = refinement.build_candidates(self.project, 1,
            policy=refinement.RefinementPolicy(version="anchored-refinement-v3"))
        self.assertEqual(len(items), 1)
        return items[0]

    def reopen(self):
        path = self.folder / "project.json"
        self.project.save(path)
        return core.ComparisonProject.load(path)

    def test_pdf_conflict_holds_candidate_without_creating_any_correction(self):
        old = self.readings()
        item = self.build()
        page = self.project.page(1)
        self.assertEqual(len(page["refinement_source_checks"]), 1)
        view = refinement.proposals(self.project, 1)[0]
        self.assertFalse(view["eligible"])
        self.assertTrue(view["source_check"]["blocked"])
        self.assertEqual(refinement.evaluate_proposals(self.project, 1)["text"], self.before)
        with self.assertRaises(core.ComparisonError):
            refinement.accept_proposal(self.project, 1, item["review_id"], item["id"])
        for key in ("runs", "ai_reviews", "ai_decisions", "edits", "corrected_text"):
            self.assertEqual(page[key], old[key])
        self.assertEqual(page["refinement_decisions"], [])
        reopened = self.reopen()
        self.assertEqual(reopened.page(1), self.project.page(1))
        self.assertFalse(refinement.proposals(reopened, 1)[0]["eligible"])

    def test_native_support_keeps_correct_candidate_and_separate_accept_undo(self):
        old = self.readings("：", ":")
        item = self.build()
        view = refinement.proposals(self.project, 1)[0]
        self.assertTrue(view["eligible"])
        self.assertFalse(view["source_check"]["blocked"])
        self.assertEqual(refinement.evaluate_proposals(self.project, 1)["text"], self.after)
        before_checks = self.project.page(1)["refinement_source_checks"]
        refinement.accept_proposal(self.project, 1, item["review_id"], item["id"])
        self.assertEqual(self.project.text(1), self.after)
        self.assertEqual(self.project.page(1)["runs"], old["runs"])
        self.assertEqual(self.project.page(1)["ai_reviews"], old["ai_reviews"])
        self.assertEqual(self.project.page(1)["refinement_source_checks"], before_checks)
        reopened = self.reopen()
        self.assertEqual(reopened.text(1), self.after)
        reopened.undo_edit(1)
        self.assertEqual(reopened.text(1), self.before)

    def test_repeating_check_is_idempotent_and_old_proposal_is_immutable(self):
        self.readings()
        self.build()
        old = copy.deepcopy(self.project.page(1))
        record_pdf_check(self.project, 1)
        self.build()
        self.assertEqual(self.project.page(1), old)

    def test_recomputed_outer_hash_does_not_hide_tampered_verdict(self):
        self.readings()
        self.build()
        path = self.folder / "project.json"
        self.project.save(path)
        data = json.loads(path.read_text(encoding="utf-8"))
        record = data["pages"]["1"]["refinement_source_checks"][0]
        record["verdicts"][0]["blocked"] = False
        record["id"] = core._digest({k: v for k, v in record.items() if k not in ("id", "created_at")})
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        with self.assertRaises(core.ComparisonError):
            core.ComparisonProject.load(path)

    def test_source_check_does_not_invent_a_human_rejection(self):
        self.readings()
        item = self.build()
        self.assertEqual(self.project.page(1)["refinement_decisions"], [])
        refinement.reject_proposal(self.project, 1, item["review_id"], item["id"])
        page = self.reopen().page(1)
        self.assertEqual([d["decision"] for d in page["refinement_decisions"]], ["rejected"])
        self.assertEqual(page["edits"], [])


if __name__ == "__main__":
    unittest.main()
