"""Legacy import is read-only; exported correction text is checked before publish."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

from yeone_ocr import comparison_bridge as bridge


class ComparisonBridgeTests(unittest.TestCase):
    def setUp(self):
        from yeone_ocr.comparison import ComparisonProject
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.source = self.folder / "source.png"
        Image.new("RGB", (200, 300), "white").save(self.source)
        self.project = ComparisonProject.open(self.source)

    def result(self, text="漢樂一\ufe00，한글。", engine="tesseract"):
        return {"kind": "recognition", "schema_version": 1, "engine": engine,
            "coordinate_system": "rendered-image pixels, xyxy",
            "text": text, "words": [], "source_path": str(self.source),
            "source_sha256": self.project.source_sha256, "page_number": 1,
            "image_width": 200, "image_height": 300, "engine_version": "fixture",
            "languages": "kor+chi_tra", "psm": 6, "duration_seconds": 0,
            "engine_metadata": {}, "preprocessing": {}}

    def legacy(self):
        from yeone_ocr.state import Work
        work = Work(str(self.source), str(self.folder / "old-work"))
        def reading(text):
            return {"pdf_page": 1, "regions": [{"id": "r1", "kind": "본문",
                "bbox": [100, 200, 900, 800], "reading_direction": "세로(우→좌)",
                "paragraphs": [{"columns": [{"col_from_right": 1, "text": text}]}]}]}
        record = {"pdf_page": 1, "version": "0.7.5", "pass1": reading("漢樂一\ufe00，"),
                  "merged": reading("漢樂一\ufe00。"), "adjudications": [{"fixture": True}]}
        work.save_record(1, record)
        return work, record

    def test_legacy_import_preserves_snapshot_unicode_and_source(self):
        work, record = self.legacy()
        before = work.record_path(1).read_bytes()
        self.assertEqual(bridge.import_yeone_records(self.project, work.dir), 1)
        page = self.project.page(1)
        self.assertEqual(len(page["runs"]), 2)
        self.assertEqual(self.project.text(1), "漢樂一\ufe00。")
        self.assertEqual(page["runs"][0]["raw"]["engine_metadata"]["import"]["record"], record)
        self.assertEqual(work.record_path(1).read_bytes(), before)
        self.assertEqual(bridge.import_yeone_records(self.project, work.dir), 0)

    def test_changed_legacy_source_is_rejected_without_rewriting(self):
        work, record = self.legacy()
        path = work.dir / "source.json"
        source = json.loads(path.read_text(encoding="utf-8")); source["sha256"] = "0" * 64
        path.write_text(json.dumps(source), encoding="utf-8")
        with self.assertRaises(ValueError):
            bridge.import_yeone_records(self.project, work.dir)
        self.assertEqual(self.project.page(1)["runs"], [])

    def test_open_reuses_comparison_and_preserves_correction(self):
        work, _ = self.legacy()
        project = bridge.open_project(self.source, work.dir)
        project.set_text(1, "人工校正")
        project.save()
        reopened = bridge.open_project(self.source, work.dir)
        self.assertEqual(reopened.text(1), "人工校正")
        self.assertEqual(len(reopened.page(1)["runs"]), 2)

    def test_ab_import_preserves_raw_and_human_correction(self):
        from ab_ocr.document import DocumentProject
        original = DocumentProject.open(self.source)
        raw = self.result()
        original.apply_recognition(raw)
        original.set_text(1, "人手校正，樂一\ufe00")
        path = self.folder / "old.abocr.json"
        original.save(path)
        before = path.read_bytes()
        self.assertEqual(bridge.import_ab_project(self.project, path), 1)
        self.assertEqual(self.project.text(1), "人手校正，樂一\ufe00")
        self.assertEqual(self.project.page(1)["runs"][0]["raw"]["text"], raw["text"])
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(bridge.import_ab_project(self.project, path), 0)

    def test_import_does_not_replace_a_new_correction(self):
        work, _ = self.legacy()
        self.project.add_result(self.result())
        self.project.set_text(1, "새 교정문")
        bridge.import_yeone_records(self.project, work.dir)
        self.assertEqual(self.project.text(1), "새 교정문")

    def test_ab_historical_import_preserves_current_baseline_and_all_previous_raw(self):
        from ab_ocr.document import DocumentProject
        source = DocumentProject.open(self.source)
        source.apply_recognition(self.result("이전 원시"))
        source.apply_recognition(self.result("마지막 원시"))
        source.set_text(1, "이전 사용자 교정")
        path = self.folder / "old.abocr.json"; source.save(path)
        self.project.add_result(self.result("현재 판독"))
        baseline = self.project.page(1)["baseline_run_id"]
        bridge.import_ab_project(self.project, path)
        self.assertEqual(self.project.page(1)["baseline_run_id"], baseline)
        self.assertEqual(self.project.text(1), "현재 판독")
        snapshot = self.project.page(1)["runs"][-1]["raw"]["engine_metadata"]["import"]["page_snapshot"]
        self.assertEqual(snapshot["previous_recognitions"][0]["text"], "이전 원시")
        self.assertEqual(snapshot["corrected_text"], "이전 사용자 교정")
        self.project.compare_page(1)
        self.assertEqual(self.project.text(1), "현재 판독")

    def test_txt_export_and_source_protection(self):
        self.project.add_result(self.result())
        self.project.set_text(1, "樂\t一\ufe00，\n한글。")
        path = self.folder / "result.txt"
        bridge.export_project(self.project, path, "txt")
        self.assertEqual(path.read_text(encoding="utf-8"), self.project.text(1))
        with self.assertRaises(ValueError):
            bridge.export_project(self.project, self.source, "png")

    def test_hwpx_docx_roundtrip_retains_characters_and_raw(self):
        self.project.add_result(self.result())
        raw = copy.deepcopy(self.project.page(1)["runs"])
        self.project.set_text(1, "樂一\ufe00，한글。\n發髮云雲，")
        for kind in ("hwpx", "docx"):
            with self.subTest(kind=kind):
                path = self.folder / ("result." + kind)
                bridge.export_project(self.project, path, kind)
                self.assertEqual("\n".join(bridge._read_export(path, kind)).strip("\n"), self.project.text(1))
        self.assertEqual(self.project.page(1)["runs"], raw)

    def test_failed_roundtrip_preserves_existing_export(self):
        self.project.add_result(self.result())
        path = self.folder / "result.docx"; path.write_bytes(b"old export")
        with patch.object(bridge, "_read_export", return_value=["different"]):
            with self.assertRaises(ValueError):
                bridge.export_project(self.project, path, "docx")
        self.assertEqual(path.read_bytes(), b"old export")


if __name__ == "__main__":
    unittest.main()
