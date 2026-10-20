from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tools import measure_small_annotations as measure


class SmallAnnotationMeasurementTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parents[1] / "work" / "small-annotation-evaluation-tests"
        root.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=root)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def save(self, name, value):
        path = self.root / name
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        return str(path)

    def fixture(self, hypothesis="甲乙", baseline="本文", box=None):
        image = self.root / "page.png"
        image.write_bytes(b"frozen image fixture")
        item = {"id": "page", "image_path": str(image), "image_sha256": measure.digest(image)}
        note = {"id": "note", "text": "甲乙", "raw_text": "甲。乙", "bbox_px": [10, 10, 20, 30],
                "subcolumns": [{"side": "right", "text": "甲"}, {"side": "left", "text": "乙"}]}
        gold = self.save("gold.json", {"status": "provisional", "scope": "selected only", "text_policy": {},
                                       "items": [{**item, "notes": [note]}]})
        record = {"merged": {"regions": [{"paragraphs": [{"columns": [{"text": baseline}]}]}]}}
        rec = self.save("record.json", record)
        candidate = {"id": "c1", "bbox_px": box or [10, 10, 20, 30], "combined_text": hypothesis,
                     "status": "read", "subcolumns": [{"side": "right"}, {"side": "left"}]}
        output = self.save("output.json", {"pages": [{"candidates": [candidate]}]})
        run = self.save("run.json", {"items": [{**item, "baseline_path": rec, "record_path": rec,
                          "baseline_sha256": measure.digest(rec), "reread_path": output,
                          "reread_sha256": measure.digest(output)}]})
        return gold, run, rec

    def test_exact_han_projection_preserves_compatibility_and_selectors(self):
        text = "樂\ufe00樂\U000e0100。 甲"
        self.assertEqual(measure.han_text(text), "樂\ufe00樂\U000e0100甲")
        self.assertNotEqual(measure.han_text("樂"), measure.han_text("樂"))

    def test_gold_cannot_enter_inference_manifest(self):
        gold, _run, _rec = self.fixture()
        with patch("yeone_ocr.readers.create_reader") as factory:
            with self.assertRaisesRegex(ValueError, "never gold"):
                measure.baseline(gold, self.root / "new-run", provider="mock", model="mock")
        factory.assert_not_called()

    def test_spatial_match_before_text_and_read_only_scoring(self):
        gold, run, rec = self.fixture()
        before = Path(rec).read_bytes()
        with patch("yeone_ocr.readers.create_reader", side_effect=AssertionError("evaluation called reader")):
            scored = measure.evaluate(gold, run)
        self.assertEqual(scored["baseline_exact_pairs"], 0)
        self.assertEqual(scored["baseline_or_proposal_exact_pairs"], 1)
        self.assertEqual(scored["proposal_edit_distance"], 0)
        self.assertEqual(scored["rows"][0]["proposal_raw_codepoint"]["deletions"], 1)
        self.assertIsNone(scored["baseline_cer"])
        self.assertIsNone(scored["precision"])
        self.assertEqual(Path(rec).read_bytes(), before)

    def test_missing_roi_counts_as_deletions_even_if_wrong_place_text_matches(self):
        gold, run, _rec = self.fixture(box=[100, 100, 110, 120])
        scored = measure.evaluate(gold, run)
        self.assertEqual(scored["detected_selected_rois"], 0)
        self.assertEqual(scored["proposal_edit_distance"], 2)
        self.assertEqual(scored["baseline_or_proposal_exact_pairs"], 0)

    def test_reversed_text_does_not_count_as_recovery(self):
        gold, run, _rec = self.fixture(hypothesis="乙甲")
        scored = measure.evaluate(gold, run)
        self.assertTrue(scored["rows"][0]["proposal_reversed_exact"])
        self.assertEqual(scored["baseline_or_proposal_exact_pairs"], 0)

    def test_changed_baseline_is_rejected(self):
        gold, run, rec = self.fixture()
        Path(rec).write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "hash mismatch"):
            measure.evaluate(gold, run)

    def test_matching_is_one_to_one_maximum_cardinality(self):
        a = {"bbox_px": [0, 0, 10, 10]}
        b = {"bbox_px": [5, 0, 15, 10]}
        # First note can match either; second can only match the wide box.
        candidates = [{"bbox_px": [0, 0, 15, 10]}, deepcopy(a)]
        got = measure.match_boxes([a, b], candidates, 0.5)
        self.assertEqual(got, {1: 0, 0: 1})
        self.assertEqual(len(measure.match_boxes([a, deepcopy(a)], [deepcopy(a)])), 1)

    def test_ordered_presence_can_find_later_nonoverlapping_pair(self):
        self.assertTrue(measure.ordered_presence("乙甲文乙", "甲", "乙"))
        self.assertFalse(measure.ordered_presence("乙甲", "甲", "乙"))

    def horizontal_fixture(self, baseline="甲乙", repeated=3, lines=2):
        gold, run, rec = self.fixture(baseline=baseline)
        previous = measure.read_json(gold)["items"][0]
        source_pdf = self.root / "source.pdf"
        source_hwpx = self.root / "source.hwpx"
        source_pdf.write_bytes(b"frozen PDF")
        source_hwpx.write_bytes(b"frozen original HWPX")
        notes = [{"id": f"note-{i}", "kind": "horizontal_note", "text": "甲乙",
                  "bbox_px": [10, 10+i*100, 30, 40+i*100],
                  "lines": [{"text": "甲乙" if j == 0 else "", "order": j+1} for j in range(lines)]}
                 for i in range(repeated)]
        data = {"schema_version": "horizontal-hwpx-source-gold-v1", "gold_status": "source-HWPX",
                "scope": "fixture", "text_policy": {}, "image_path": previous["image_path"],
                "image_sha256": previous["image_sha256"], "source_pdf": str(source_pdf),
                "source_pdf_sha256": measure.digest(source_pdf), "source_hwpx": str(source_hwpx),
                "source_hwpx_sha256": measure.digest(source_hwpx), "notes": notes,
                "reference_page_text": "甲乙"*repeated}
        Path(gold).write_text(json.dumps(data, ensure_ascii=False), encoding="utf8")
        r = measure.read_json(run)
        r["items"][0]["source_sha256"] = measure.digest(source_pdf)
        r["items"][0]["source_path"] = str(source_pdf)
        output = {"pages": [{"candidates": [{"id": "c1", "bbox_px": notes[0]["bbox_px"],
                    "combined_text": "甲乙", "status": "read", "direction": "horizontal",
                    "subcolumns": [{"side": f"line-{j+1}", "bbox_px": [10,10+j*3,30,12+j*3]}
                                   for j in range(lines)]}]}]}
        out_path = r["items"][0]["reread_path"]
        Path(out_path).write_text(json.dumps(output), encoding="utf8")
        r["items"][0]["reread_sha256"] = measure.digest(out_path)
        Path(run).write_text(json.dumps(r), encoding="utf8")
        return gold, run, rec

    def test_horizontal_repeated_note_occurrence_is_consumed_only_once(self):
        gold, run, rec = self.horizontal_fixture()
        before = Path(rec).read_bytes()
        with patch("yeone_ocr.readers.create_reader", side_effect=AssertionError("gold reached inference")):
            result = measure.evaluate(gold, run)
        self.assertEqual(result["baseline_distinct_exact_occurrences"]["raw_codepoint"], 1)
        self.assertEqual(result["detected_selected_rois"], 1)
        self.assertIsNone(result["baseline_or_proposal_exact_notes"])
        self.assertEqual(result["proposal_metrics_including_missing_as_deletions"]["proposal_source_exact"]["edit_distance"], 4)
        self.assertEqual(Path(rec).read_bytes(), before)

    def test_horizontal_three_independent_occurrences_count_three(self):
        gold, run, _rec = self.horizontal_fixture(baseline="甲乙正文甲乙正文甲乙")
        result = measure.evaluate(gold, run)
        self.assertEqual(result["baseline_distinct_exact_occurrences"]["raw_codepoint"], 3)
        self.assertEqual([r["baseline_occurrence_spans"]["raw_codepoint"] for r in result["rows"]],
                         [[0,2], [4,6], [8,10]])

    def test_horizontal_multiline_count_and_source_codepoints_are_not_folded(self):
        gold, run, _rec = self.horizontal_fixture(repeated=1, lines=5)
        data = measure.read_json(gold)
        data["notes"][0]["text"] = "樂\ufe00樂\U000e0100 甲。乙"
        Path(gold).write_text(json.dumps(data, ensure_ascii=False), encoding="utf8")
        result = measure.evaluate(gold, run)
        row = result["rows"][0]
        self.assertEqual(row["proposal_source_exact"]["reference_length"], 8)
        self.assertEqual(row["proposal_without_whitespace"]["reference_length"], 7)
        self.assertEqual(row["proposal_han_diagnostic"]["reference_length"], 6)
        self.assertTrue(row["line_count_matches_reference"])
        self.assertTrue(row["candidate_line_order_top_bottom"])
        self.assertEqual(result["subsets"]["multiline"]["roi_count"], 1)

    def test_horizontal_source_hash_is_checked(self):
        gold, run, _rec = self.horizontal_fixture()
        Path(measure.read_json(gold)["source_hwpx"]).write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "hash mismatch"):
            measure.evaluate(gold, run)

    def test_baseline_region_score_uses_geometry_and_all_region_text(self):
        gold, run, rec = self.horizontal_fixture(repeated=1)
        g = measure.read_json(gold)
        g["image_size"] = [100, 100]
        Path(gold).write_text(json.dumps(g), encoding="utf8")
        record = {"merged": {"regions": [
            {"id": "wrong-place", "bbox": [700,700,900,900], "paragraphs": [{"columns": [{"text": "甲乙"}]}]},
            {"id": "right-place", "bbox": [100,100,400,300], "paragraphs": [{"columns": [{"text": "甲乙外"}]}]}]}}
        Path(rec).write_text(json.dumps(record), encoding="utf8")
        r = measure.read_json(run)
        r["items"][0]["baseline_sha256"] = measure.digest(rec)
        Path(run).write_text(json.dumps(r), encoding="utf8")
        result = measure.evaluate(gold, run)
        self.assertEqual(result["rows"][0]["baseline_region_id"], "right-place")
        self.assertEqual(result["rows"][0]["baseline_region_source_exact"]["insertions"], 1)
        self.assertEqual(result["paired_region_metrics"]["matched_roi_count"], 1)
        self.assertEqual(result["paired_region_metrics"]["source_exact"]["baseline"]["edit_distance"], 1)
        self.assertEqual(result["paired_region_metrics"]["source_exact"]["proposal"]["edit_distance"], 0)

    def test_mixed_full_page_region_is_not_scored_as_roi_substring(self):
        gold, run, rec = self.horizontal_fixture(repeated=1)
        g = measure.read_json(gold); g["image_size"] = [100,100]
        Path(gold).write_text(json.dumps(g), encoding="utf8")
        record = measure.read_json(rec)
        record["merged"]["regions"][0]["bbox"] = [0,0,1000,1000]
        Path(rec).write_text(json.dumps(record), encoding="utf8")
        r = measure.read_json(run); r["items"][0]["baseline_sha256"] = measure.digest(rec)
        Path(run).write_text(json.dumps(r), encoding="utf8")
        result = measure.evaluate(gold, run)
        self.assertEqual(result["baseline_region_matched_count"], 0)
        self.assertIsNone(result["rows"][0]["baseline_region_source_exact"])
        self.assertEqual(result["paired_region_metrics"]["matched_roi_count"], 0)

    def test_source_text_includes_table_cells_and_excludes_model_notes(self):
        record = {"merged": {"page_note": "not original", "regions": [{"title_text": "題",
                  "paragraphs": [{"columns": [{"text": "原 文"}]}], "note": "not original",
                  "table_rows": [{"cells": ["樂\ufe00", "樂\U000e0100"]}]}]}}
        self.assertEqual(measure.source_text(record), "題原 文樂\ufe00樂\U000e0100")

    def test_horizontal_image_manifest_is_allowed_but_source_gold_is_rejected(self):
        gold, run, _rec = self.horizontal_fixture()
        acquired = measure.read_json(run)["items"][0]
        item = {k: acquired[k] for k in ("id", "image_path", "image_sha256", "source_path", "source_sha256")}
        item.update(mode="horizontal_zh", orientation="horizontal", render_dpi=300, pdf_page=1)
        path = self.save("inputs.json", {"schema_version": 1, "items": [item]})
        self.assertEqual(measure.input_mode(measure.inference_inputs(path)[0]), "horizontal_zh")
        self.save("inputs.json", {"frozen_date": "2026-10-19", "development_canvases": [12],
                                  "holdout_canvases": [20, 40], "selection_policy": "image only", "items": [item]})
        self.assertEqual(len(measure.inference_inputs(path)), 1)
        item["source_path"] = measure.read_json(gold)["source_hwpx"]
        item["source_sha256"] = measure.digest(item["source_path"])
        self.save("inputs.json", {"schema_version": 1, "items": [item]})
        with self.assertRaisesRegex(ValueError, "evaluation only"):
            measure.inference_inputs(path)
        with self.assertRaisesRegex(ValueError, "disagree"):
            measure.input_mode({"mode": "vertical", "orientation": "horizontal"})

    def test_reread_budget_counts_actual_number_of_lines(self):
        _gold, run, rec = self.horizontal_fixture(repeated=1, lines=5)
        data = measure.read_json(run)
        data.update(baseline_complete=True, baseline_calls=2, max_calls=32, provider="mock", model="mock")
        data["items"][0]["work_dir"] = str(self.root / "isolated-work")
        Path(run).write_text(json.dumps(data), encoding="utf8")
        bundle = measure.read_json(data["items"][0]["reread_path"])
        reread = deepcopy(bundle)
        reread["run"] = {"attempted_calls": 5}
        with patch("yeone_ocr.small_annotations.scan_work", return_value=bundle), \
             patch("yeone_ocr.small_annotations.reread_work", return_value=reread) as call:
            result = measure.reread(run)
        self.assertEqual(call.call_args.kwargs["max_calls"], 5)
        self.assertEqual(result["reread_calls"], 5)
        self.assertEqual(measure.digest(rec), data["items"][0]["baseline_sha256"])

    def test_baseline_passes_horizontal_mode_and_raster_to_existing_reader(self):
        _gold, run, _rec = self.horizontal_fixture(repeated=1)
        acquired = measure.read_json(run)["items"][0]
        item = {k: acquired[k] for k in ("id", "image_path", "image_sha256", "source_path", "source_sha256")}
        item.update(mode="horizontal_zh", orientation="horizontal", render_dpi=300)
        manifest = self.save("inputs.json", {"schema_version": 1, "items": [item]})

        class FakeWork:
            def __init__(self, image_path, directory):
                self.dir = Path(directory)
                self.dir.mkdir(parents=True)
                self.image_path = image_path
            def ensure_source(self, dpi):
                pass
            def record_path(self, page):
                return self.dir / "record.json"

        def read(work, doc, first, second, page, args):
            self.assertEqual(args.mode, "horizontal_zh")
            self.assertEqual(work.image_path, item["image_path"])
            self.assertFalse(args.no_second_pass)
            work.record_path(page).write_text(json.dumps({"merged": {"regions": []}, "pass2": {}}))

        with patch("yeone_ocr.state.Work", FakeWork), patch("yeone_ocr.pdfio.open_pdf"), \
             patch("yeone_ocr.cli.read_one", side_effect=read), \
             patch("yeone_ocr.cli.apply_mode") as apply_mode, patch("yeone_ocr.readers.create_reader") as factory:
            result = measure.baseline(manifest, self.root / "fresh", provider="mock", model="mock")
        self.assertEqual(apply_mode.call_args.args[0].mode, "horizontal_zh")
        self.assertFalse(factory.call_args.kwargs["use_cache"])
        self.assertEqual(result["mode"], "horizontal_zh")
        self.assertFalse(result["gold_loaded_for_inference"])


if __name__ == "__main__":
    unittest.main()
