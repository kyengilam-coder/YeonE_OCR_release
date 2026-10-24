"""Installed-output verifier rejects unrecognized, API-backed, or corrupt output."""
import json
from pathlib import Path
import tempfile
import unittest

import pymupdf

from tools.verify_installed_local_first import inspect_outputs, POLICY, REFERENCE


class InstalledLocalFirstTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.app, self.work = self.root / "app", self.root / "work"
        (self.work / "readings").mkdir(parents=True)
        (self.work / "out").mkdir()
        self.project_path = self.work / "project.json"
        self.record = {"pipeline": POLICY, "model": "ppocrv6", "image_frame": "full-page",
                       "usage": {"calls": 0}, "local_first": {"project_path": str(self.project_path)}}
        self.state = {"local_first_last_run": {"calls": 0}}
        self.project = {"pages": {"1": {"baseline_run_id": "synthetic", "ai_reviews": [], "runs": [
            {"id": "synthetic", "engine": "ppocrv6", "request": {"engine_root": str(self.app)},
             "raw": {"text": REFERENCE}}]}}}
        self.write_json()
        self.pdf(REFERENCE)

    def write_json(self):
        for path, value in ((self.work / "readings/p001.json", self.record),
                            (self.work / "state.json", self.state), (self.project_path, self.project)):
            path.write_text(json.dumps(value), encoding="utf-8")

    def pdf(self, text):
        path = self.work / "out" / "synthetic_글자층.pdf"
        with pymupdf.open() as doc:
            page = doc.new_page(width=400, height=200)
            page.insert_text((20, 60), text, fontname="china-s", fontsize=18)
            doc.save(path)

    def test_exact_own_engine_pdf_and_zero_api_are_verified(self):
        result = inspect_outputs(self.work, self.app)
        self.assertEqual(result["api_calls"], 0)
        self.assertTrue(result["pdf_text_matches"])
        self.assertEqual(result["character_errors"], 0)

    def test_any_unexpected_api_call_is_rejected(self):
        self.state["local_first_last_run"]["calls"] = 1
        self.write_json()
        with self.assertRaisesRegex(ValueError, "API"):
            inspect_outputs(self.work, self.app)

    def test_wrong_pipeline_or_external_engine_is_rejected(self):
        self.record["pipeline"] = "legacy-ai"
        self.write_json()
        with self.assertRaisesRegex(ValueError, "reading"):
            inspect_outputs(self.work, self.app)
        self.record["pipeline"] = POLICY
        self.project["pages"]["1"]["runs"][0]["request"]["engine_root"] = str(self.root / "other")
        self.write_json()
        with self.assertRaisesRegex(ValueError, "own"):
            inspect_outputs(self.work, self.app)

    def test_empty_ocr_or_wrong_searchable_text_is_rejected(self):
        self.project["pages"]["1"]["runs"][0]["raw"]["text"] = ""
        self.write_json()
        with self.assertRaisesRegex(ValueError, "reference"):
            inspect_outputs(self.work, self.app)
        self.project["pages"]["1"]["runs"][0]["raw"]["text"] = REFERENCE
        self.write_json()
        self.pdf("天地")
        with self.assertRaisesRegex(ValueError, "PDF text"):
            inspect_outputs(self.work, self.app)


if __name__ == "__main__":
    unittest.main()
