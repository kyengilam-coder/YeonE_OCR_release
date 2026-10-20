"""Independent PDF-hold lifecycle/AI-history/UI checks using synthetic PDFs."""
import copy
import json
import unittest

import pymupdf

from tests import test_comparison_ai as fake_ai
from tests import test_comparison_editor as hosted
from tests import test_comparison_refinement_source as source_fixture
from yeone_ocr import comparison as core
from yeone_ocr import comparison_refinement as refinement
from yeone_ocr import comparison_refinement_source as source
from yeone_ocr.comparison_ai import AISettings, ReviewPolicy, _page_scope, prepare_plan, run_plan


class SourceCheckIndependentTests(unittest.TestCase):
    response = staticmethod(fake_ai.PipelineTests.response)
    prompt_data = staticmethod(fake_ai.PipelineTests.prompt_data)
    client = fake_ai.PipelineTests.client

    def setUp(self):
        self.fixture = source_fixture.SourceCheckLifecycleTests(methodName="runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.project = self.fixture.project

    def build(self, before=":", after="："):
        self.fixture.readings(before, after)
        return self.fixture.build()

    def test_real_pdf_check_has_scope_and_excludes_held_local_from_AI_context(self):
        item = self.build()
        page = self.project.page(1)
        verdict = source.evidence_for(page, item["review_id"], item["id"])
        self.assertTrue(verdict["blocked"])
        self.assertEqual(verdict["reason_codes"], ["visible-original-pdf-punctuation-supports-before"])
        self.assertEqual(refinement.refinement_context(self.project, 1)["proposals"], [])
        self.assertNotIn("refinement_evidence", _page_scope(self.project, 1))
        self.assertEqual(page["refinement_reviews"][0]["proposals"][0]["id"], item["id"])
        self.assertEqual(page["ai_decisions"], [])
        self.assertEqual(page["refinement_decisions"], [])

    def test_later_unavailable_capture_never_erases_an_earlier_hold(self):
        item = self.build()
        old = self.project.page(1)
        source.record_pdf_check(self.project, 1, dpi=72)
        page = self.project.page(1)
        self.assertEqual(len(page["refinement_source_checks"]), 2)
        self.assertEqual(page["refinement_source_checks"][-1]["capture"]["status"], "unavailable")
        self.assertEqual(source.evidence_for(page, item["review_id"], item["id"])["source_check_id"],
                         old["refinement_source_checks"][0]["id"])
        self.assertFalse(refinement.proposals(self.project, 1)[0]["eligible"])
        self.assertEqual(refinement.evaluate_proposals(self.project, 1)["text"], self.fixture.before)
        for key in ("runs", "ai_reviews", "ai_decisions", "edits", "refinement_reviews", "refinement_decisions"):
            self.assertEqual(page[key], old[key])
        self.assertEqual(self.fixture.reopen().page(1), page)

    def test_returned_check_and_view_mutation_do_not_modify_stored_proofs(self):
        item = self.build()
        old = self.project.page(1)
        returned = source.record_pdf_check(self.project, 1)
        returned["verdicts"][0]["blocked"] = False
        returned["capture"]["chars"][0]["char"] = "Z"
        view = refinement.proposals(self.project, 1)[0]
        view["source_check"]["blocked"] = False
        self.assertEqual(self.project.page(1), old)
        with self.assertRaises(core.ComparisonError):
            refinement.accept_proposal(self.project, 1, item["review_id"], item["id"])

    def test_AI_historical_context_keeps_captured_check_not_latest_check(self):
        self.build("：", ":")
        first = self.project.page(1)["refinement_source_checks"][0]["id"]
        plan = prepare_plan(self.project, [1], ReviewPolicy(visual_scope="page", punctuation=True),
                            settings=AISettings("openai", "synthetic-only", max_calls=20))
        self.assertTrue(all(batch["refinement_evidence"]["proposals"][0]["source_check"]["source_check_id"] == first
                            for batch in plan.batches))
        client, calls = self.client()
        outcome = run_plan(self.project, plan, api_key="public-synthetic-only", client_factory=lambda *_: client)
        self.assertEqual(outcome["failed"], 0)
        self.assertEqual(len(calls), 2)
        original_reviews = copy.deepcopy(self.project.page(1)["ai_reviews"])
        source.record_pdf_check(self.project, 1, dpi=72)
        latest = refinement.proposals(self.project, 1)[0]["source_check"]["source_check_id"]
        self.assertNotEqual(first, latest)
        reopened = self.fixture.reopen()
        self.assertEqual(reopened.page(1)["ai_reviews"], original_reviews)
        for review in original_reviews:
            refinement.validate_ai_refinement_context(reopened.page(1), review["request"])

    def test_image_history_accepted_before_PDF_check_remains_unchanged_after_undo_and_hold(self):
        original_pdf = self.fixture.source
        png = self.fixture.folder / "from-visible-source.png"
        with pymupdf.open(original_pdf) as doc:
            doc[0].get_pixmap(dpi=300).save(png)
        self.fixture.source = png
        self.fixture.project = self.project = core.ComparisonProject.open(png)
        item = self.build()
        self.assertNotIn("refinement_source_checks", self.project.page(1))
        refinement.accept_proposal(self.project, 1, item["review_id"], item["id"])
        old = copy.deepcopy(self.project.page(1))
        self.assertIsNone(source.record_pdf_check(self.project, 1, pdf_path=original_pdf, pdf_page=1, dpi=300))
        reopened = self.fixture.reopen()
        self.assertEqual(reopened.page(1), old)
        reopened.undo_edit(1)
        history = copy.deepcopy(reopened.page(1))
        record = source.record_pdf_check(reopened, 1, pdf_path=original_pdf, pdf_page=1, dpi=300)
        self.assertTrue(record["verdicts"][0]["blocked"])
        for key in ("runs", "refinement_reviews", "edits", "ai_reviews", "ai_decisions", "refinement_decisions"):
            self.assertEqual(reopened.page(1)[key], history[key])
        reopened.save()
        self.assertEqual(core.ComparisonProject.load(reopened.project_path).page(1), reopened.page(1))

    def test_rehashed_capture_region_cannot_replace_actual_raw_geometry(self):
        self.build()
        path = self.fixture.folder / "tampered-regions.json"
        self.project.save(path)
        data = json.loads(path.read_text(encoding="utf-8"))
        record = data["pages"]["1"]["refinement_source_checks"][0]
        capture = record["capture"]
        capture["ocr_regions"][0]["regions"][0]["bbox"][0] += 1
        capture["sha256"] = core._digest({key: value for key, value in capture.items() if key != "sha256"})
        record["id"] = core._digest({key: value for key, value in record.items() if key not in ("id", "created_at")})
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        with self.assertRaises(core.ComparisonError):
            core.ComparisonProject.load(path)

    def test_invisible_content_anchors_cannot_supply_a_source_address(self):
        hidden = self.fixture.folder / "hidden-address.pdf"
        with pymupdf.open() as doc:
            page = doc.new_page(width=240, height=120)
            page.insert_text((20, 45), "ABCD", fontsize=16, render_mode=3)
            page.insert_text((70, 45), ":", fontsize=16)
            page.insert_text((90, 45), "EFGH", fontsize=16, render_mode=3)
            boxes = [block[:4] for block in page.get_text("blocks")]
            self.fixture.box = [min(b[0] for b in boxes) * 300 / 72,
                                min(b[1] for b in boxes) * 300 / 72,
                                max(b[2] for b in boxes) * 300 / 72,
                                max(b[3] for b in boxes) * 300 / 72]
            pixmap = page.get_pixmap(dpi=300)
            self.fixture.width, self.fixture.height = pixmap.width, pixmap.height
            doc.save(hidden)
        self.fixture.source = hidden
        self.fixture.project = self.project = core.ComparisonProject.open(hidden)
        self.build()
        view = refinement.proposals(self.project, 1)[0]
        self.assertFalse(view["source_check"]["blocked"])
        self.assertEqual(view["source_check"]["status"], "unavailable")
        self.assertTrue(view["eligible"])

    def test_opaque_text_without_rendered_contrast_is_not_a_visible_source_witness(self):
        hidden = self.fixture.folder / "no-rendered-contrast.pdf"
        with pymupdf.open() as doc:
            page = doc.new_page(width=240, height=120)
            page.draw_rect((10, 20, 150, 60), color=(0, 0, 0), fill=(0, 0, 0))
            page.insert_text((20, 45), "ABCD:EFGH", fontsize=16, color=(0, 0, 0))
            self.fixture.box = [float(value) * 300 / 72 for value in page.get_text("blocks")[0][:4]]
            pixmap = page.get_pixmap(dpi=300)
            self.fixture.width, self.fixture.height = pixmap.width, pixmap.height
            doc.save(hidden)
        self.fixture.source = hidden
        self.fixture.project = self.project = core.ComparisonProject.open(hidden)
        self.build()
        view = refinement.proposals(self.project, 1)[0]
        self.assertFalse(view["source_check"]["blocked"])
        self.assertEqual(view["source_check"]["status"], "unavailable")
        self.assertTrue(view["eligible"])


class SourceCheckUIIndependentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        hosted.HostedComparisonEditor.setUpClass.__func__(cls)

    @classmethod
    def tearDownClass(cls):
        hosted.HostedComparisonEditor.tearDownClass.__func__(cls)

    def setUp(self):
        hosted.HostedComparisonEditor.setUp(self)
        self.fixture = source_fixture.SourceCheckLifecycleTests(methodName="runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.readings()
        self.item = self.fixture.build()
        self.project = self.editor.project = self.fixture.project
        self.editor._populate_page()

    def test_source_hold_badge_disabled_accept_and_original_details(self):
        import tkinter as tk
        panel = self.editor.refinement_panel
        key = panel.tree.get_children()[0]
        panel.tree.selection_set(key)
        panel.selected()
        self.assertEqual(panel.tree.item(key, "values")[-1], "PDF 근거 보류")
        self.assertTrue(panel.accept_button.instate(["disabled"]))
        self.assertIn("PDF 보류 1곳", panel.count.get())
        before = self.project.to_dict()
        panel.decide(True)
        self.assertEqual(self.project.to_dict(), before)
        existing = set(self.editor.winfo_children())
        panel.details()
        window = next(w for w in self.editor.winfo_children() if w not in existing and isinstance(w, tk.Toplevel))
        self.addCleanup(window.destroy)
        text = next(w for w in window.winfo_children() if isinstance(w, tk.Text))
        details = json.loads(text.get("1.0", "end"))
        self.assertEqual(details["source_checks"], self.project.page(1)["refinement_source_checks"])
        self.assertEqual(details["selected_proposal"]["id"], self.item["id"])
        self.assertTrue(details["selected_proposal"]["source_check"]["blocked"])
        self.assertEqual(str(text.cget("state")), "disabled")


if __name__ == "__main__":
    unittest.main()
