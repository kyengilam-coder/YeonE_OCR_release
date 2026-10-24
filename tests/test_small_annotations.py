from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

from yeone_ocr import small_annotations as notes
from yeone_ocr.schema import PageReading, Region, Paragraph, Column, RegionReread
from yeone_ocr.state import Work


def detection(*args, **kwargs):
    return {"schema_version": 1, "detector": "test-v1", "image_size": [160, 240],
            "diagnostics": {}, "candidates": [{"id": "note-1", "parent_region_id": "r1",
            "bbox": [200, 300, 800, 700], "bbox_px": [48, 48, 112, 192],
            "subcolumns": [{"side": "right", "bbox_px": [82, 48, 112, 192]},
                           {"side": "left", "bbox_px": [48, 48, 78, 192]}]}]}


class Reader:
    def __init__(self, answers=None):
        self.answers = list(answers or ["靑樂\ufe00", "硏\U000e0100"])
        self.calls = []

    def usage_begin(self):
        pass

    def usage_end(self):
        return {"calls": 1}

    def reread_region(self, image, kind, rid, page):
        self.calls.append((image.size, kind, rid, page))
        result = self.answers.pop(0)
        if isinstance(result, Exception):
            raise result
        return RegionReread(columns=[Column(n=1, t=result)])


class SmallAnnotationFlow(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "book.png"
        Image.new("RGB", (160, 240), "white").save(self.source)
        self.work = Work(str(self.source), str(self.root / "work"))
        reading = PageReading(pdf_page=1, regions=[Region(id="r1", kind="본문",
            paragraphs=[Paragraph(columns=[Column(n=1, t="人手樂\ufe00校正")])])])
        self.rec = {"pass1": reading.model_dump(), "pass2": {"r1": {"columns": []}},
                    "merged": reading.model_dump(), "adjudications": [{"choice": "曰"}],
                    "mode": "vertical", "unknown_old_metadata": {"keep": "unchanged"}}
        self.work.save_record(1, self.rec)
        self.original = self.work.record_path(1).read_bytes()
        self.detector = patch("yeone_ocr.small_annotation_detection.detect_small_annotations", side_effect=detection)
        self.detector.start()
        self.addCleanup(self.detector.stop)
        self.addCleanup(self.temp.cleanup)

    def scan(self):
        return notes.scan_work(self.source, self.work.dir, pages=[1])

    def reread(self, reader=None, **kwargs):
        reader = reader or Reader()
        with patch("yeone_ocr.readers.create_reader", return_value=reader) as factory:
            result = notes.reread_work(self.source, self.work.dir, model="test-model", **kwargs)
        return result, reader, factory

    def candidate(self):
        return notes.load_bundle(self.work.dir)["pages"][0]["candidates"][0]

    def apply(self, **kwargs):
        options = {"region_id": "r1", "paragraph_index": 0, "column_index": 0,
                   "offset": 2, "expected_text": "人手樂\ufe00校正"}
        options.update(kwargs)
        return notes.apply_candidate(self.source, self.work.dir, 1, "note-1", **options)

    def test_scan_and_reread_preserve_old_bytes_and_exact_source_codes(self):
        self.scan()
        result, reader, factory = self.reread()
        self.assertEqual(self.work.record_path(1).read_bytes(), self.original)
        candidate = result["pages"][0]["candidates"][0]
        self.assertEqual(candidate["combined_text"], "靑樂\ufe00硏\U000e0100")
        self.assertEqual(candidate["status"], "read")
        self.assertEqual([c[2] for c in reader.calls], ["note-1-right", "note-1-left"])
        self.assertFalse(factory.call_args.kwargs["use_cache"])
        self.assertTrue(Path(candidate["crop_path"]).is_file())
        self.assertNotIn("api_key", json.dumps(result, ensure_ascii=False))

    def test_explicit_insertion_only_adds_text_and_creates_byte_exact_backup(self):
        self.scan(); self.reread()
        result = self.apply()
        record = self.work.load_record(1)
        self.assertEqual(Path(result["backup_path"]).read_bytes(), self.original)
        self.assertEqual(record["merged"]["regions"][0]["paragraphs"][0]["columns"][0]["text"],
                         "人手靑樂\ufe00硏\U000e0100樂\ufe00校正")
        for key in ("pass1", "pass2", "adjudications", "unknown_old_metadata"):
            self.assertEqual(record[key], self.rec[key])
        self.assertEqual(self.candidate()["status"], "applied")
        with self.assertRaisesRegex(ValueError, "이미 반영"):
            self.apply()

    def test_only_missing_side_is_paid_again_after_fatal_failure(self):
        from yeone_ocr.readers import ReaderError
        self.scan()
        result, _, _ = self.reread(Reader(["靑", ReaderError("temporary", fatal=True)]))
        self.assertTrue(result["run"]["stopped"])
        self.assertEqual(self.candidate()["status"], "failed")
        result, reader, _ = self.reread(Reader(["硏"]))
        self.assertEqual(len(reader.calls), 1)
        self.assertEqual(self.candidate()["combined_text"], "靑硏")
        self.assertEqual(len(self.candidate()["subcolumns"][1]["runs"]), 2)
        self.assertEqual(self.work.record_path(1).read_bytes(), self.original)

    def test_call_cap_and_cancel_resume_without_repaying_success(self):
        self.scan()
        _, reader, _ = self.reread(Reader(["右"]), max_calls=1)
        self.assertEqual(len(reader.calls), 1)
        self.assertEqual(self.candidate()["status"], "detected")
        with self.assertRaises(ValueError):
            self.apply()
        _, reader, _ = self.reread(Reader(["左"]))
        self.assertEqual(len(reader.calls), 1)
        _, reader, factory = self.reread(Reader(["unused"]), should_stop=lambda: True)
        self.assertEqual(reader.calls, [])
        factory.assert_not_called()

    def test_model_change_keeps_raw_history_but_does_not_mix_models(self):
        self.scan(); self.reread()
        with patch("yeone_ocr.readers.create_reader", return_value=Reader(["新右"])):
            notes.reread_work(self.source, self.work.dir, model="another-model", max_calls=1)
        c = self.candidate()
        self.assertEqual(c["combined_text"], "新右")
        self.assertEqual(c["status"], "detected")
        self.assertEqual(len(c["subcolumns"][0]["runs"]), 2)
        self.assertEqual(len(c["subcolumns"][1]["runs"]), 1)

    def test_stale_human_edit_blocks_paid_reread_and_insertion(self):
        self.scan(); self.reread()
        edited = deepcopy(self.rec)
        edited["merged"]["regions"][0]["paragraphs"][0]["columns"][0]["text"] = "새 교정"
        self.work.save_record(1, edited)
        self.assertTrue(notes.load_bundle(self.work.dir)["pages"][0]["stale"])
        with self.assertRaisesRegex(ValueError, "기록이 바뀌"):
            self.reread()
        with self.assertRaisesRegex(ValueError, "기록이 바뀌"):
            self.apply()
        self.scan()
        self.assertFalse(notes.load_bundle(self.work.dir)["pages"][0]["stale"])
        self.assertEqual(self.candidate()["combined_text"], "靑樂\ufe00硏\U000e0100")
        self.assertEqual(self.work.load_record(1), edited)

    def test_wrong_source_does_not_archive_or_modify_any_reading(self):
        self.scan()
        Image.new("RGB", (160, 240), "black").save(self.source)
        for action in (self.scan, self.reread, self.apply):
            with self.assertRaisesRegex(ValueError, "다른 파일"):
                action()
        self.assertEqual(self.work.record_path(1).read_bytes(), self.original)
        self.assertFalse((self.work.dir / "archives").exists())

    def test_invalid_caret_or_selection_never_modifies_record(self):
        self.scan(); self.reread()
        for args in ({"offset": 3}, {"offset": -1}, {"offset": 999},
                     {"paragraph_index": 1}, {"column_index": -1},
                     {"expected_text": "wrong"}, {"actor": "machine"}):
            with self.subTest(args=args), self.assertRaises(ValueError):
                self.apply(**args)
        self.assertEqual(self.work.record_path(1).read_bytes(), self.original)

    def test_already_present_and_partial_present_cannot_be_inserted_again(self):
        self.scan(); self.reread(Reader(["人", "手"]))
        self.assertEqual(self.candidate()["status"], "already_present")
        with self.assertRaises(ValueError):
            self.apply()

    def test_partial_present_cannot_be_inserted_again(self):
        self.scan(); self.reread(Reader(["人手", "未收"]))
        self.assertEqual(self.candidate()["status"], "partial_present")
        with self.assertRaises(ValueError):
            self.apply()

    def test_partial_note_only_inserts_explicitly_selected_missing_subcolumn(self):
        self.scan(); self.reread(Reader(["人手", "未收\U000e0100"]))
        with self.assertRaisesRegex(ValueError, "이미 있"):
            self.apply(side="right")
        self.assertEqual(self.work.record_path(1).read_bytes(), self.original)
        result = self.apply(side="left")
        self.assertEqual(result["inserted_text"], "未收\U000e0100")
        text = self.work.load_record(1)["merged"]["regions"][0]["paragraphs"][0]["columns"][0]["text"]
        self.assertEqual(text, "人手未收\U000e0100樂\ufe00校正")
        self.assertEqual(result["application"]["selected_side"], "left")
        self.assertEqual(Path(result["backup_path"]).read_bytes(), self.original)

    def test_missing_baseline_allows_review_but_no_implicit_insertion(self):
        self.work.record_path(1).unlink()
        self.scan(); self.reread()
        self.assertIsNone(notes.load_bundle(self.work.dir)["pages"][0]["record"])
        with self.assertRaisesRegex(ValueError, "먼저"):
            self.apply()
        self.assertFalse(self.work.record_path(1).exists())

    def test_scan_reuses_rereads_and_archives_previous_sidecar(self):
        self.scan(); self.reread()
        self.scan()
        _, _, factory = self.reread()
        factory.assert_not_called()
        self.assertEqual(len(self.candidate()["subcolumns"][0]["runs"]), 1)
        self.assertTrue(list((self.work.dir / "annotations" / "history").glob("p001*.json")))

    def test_crash_after_record_commit_does_not_allow_second_insertion(self):
        self.scan(); self.reread()
        old_sidecar = (self.work.dir / "annotations" / "p001.json").read_bytes()
        self.apply()
        (self.work.dir / "annotations" / "p001.json").write_bytes(old_sidecar)
        self.assertEqual(self.candidate()["status"], "applied")
        with self.assertRaises(ValueError):
            self.apply()

    def test_report_escapes_ocr_markup_and_never_modifies_record(self):
        self.scan(); self.reread(Reader(["<script>", "&本文"]))
        report = notes.write_report(self.work.dir).read_text(encoding="utf-8")
        self.assertNotIn("<script>", report)
        self.assertIn("&lt;script&gt;", report)
        self.assertEqual(self.work.record_path(1).read_bytes(), self.original)

    def test_prompts_keep_generic_behavior_and_force_small_column_order(self):
        from yeone_ocr import prompts
        old = prompts.MODE
        try:
            prompts.set_mode("horizontal_zh")
            self.assertIn("왼쪽→오른쪽", prompts.region_prompt("본문", "r", 1))
            self.assertIn("위에서 아래", prompts.region_prompt(notes.KIND, "r", 1))
            self.assertIn("이체자", prompts.region_prompt(notes.KIND, "r", 1))
        finally:
            prompts.MODE = old

    def test_cli_is_local_by_default_and_reread_has_finite_cap(self):
        from yeone_ocr.cli import build_parser
        args = build_parser().parse_args(["annotations", str(self.source), "--work", str(self.work.dir)])
        self.assertFalse(args.reread)
        self.assertEqual(args.max_calls, 12)
        with patch("yeone_ocr.readers.create_reader") as factory:
            self.assertEqual(notes.command(args), 0)
        factory.assert_not_called()

    def test_cancel_event_is_scoped_and_previous_stopped_job_does_not_poison_new_job(self):
        import threading
        from yeone_ocr import reader_gemini
        old = reader_gemini._CANCEL_EVENT
        previous = threading.Event(); previous.set()
        reader_gemini.set_cancel_event(previous)
        self.addCleanup(reader_gemini.set_cancel_event, old)
        self.scan()
        reader = Reader()
        original = reader.reread_region
        def read(*args):
            reader_gemini._check_cancelled()
            return original(*args)
        reader.reread_region = read
        self.reread(reader)
        self.assertEqual(len(reader.calls), 2)
        self.assertIs(reader_gemini._CANCEL_EVENT, previous)
        event = notes._StopEvent(lambda: True)
        self.assertTrue(event.wait(10))

    def test_inserted_note_and_human_correction_survive_remerge(self):
        from yeone_ocr.cli import remerge_record
        self.scan(); self.reread(); self.apply()
        record = self.work.load_record(1)
        before = record["merged"]["regions"][0]["paragraphs"][0]["columns"][0]["text"]
        remerge_record(record, "")
        self.assertEqual(record["merged"]["regions"][0]["paragraphs"][0]["columns"][0]["text"], before)

    def test_unknown_selection_never_creates_a_reader(self):
        self.scan()
        with patch("yeone_ocr.readers.create_reader") as factory:
            with self.assertRaisesRegex(ValueError, "후보 ID"):
                notes.reread_work(self.source, self.work.dir, candidate_ids=["missing"])
        factory.assert_not_called()

    def test_insertion_rechecks_bytes_after_backup_before_replacing_record(self):
        self.scan(); self.reread()
        real_atomic = notes._atomic_write
        newer = self.original + b"\n"
        def concurrent_edit(path, content, **kwargs):
            if path == self.work.record_path(1):
                path.write_bytes(newer)
            return real_atomic(path, content, **kwargs)
        with patch.object(notes, "_atomic_write", side_effect=concurrent_edit):
            with self.assertRaisesRegex(ValueError, "다른 작업"):
                self.apply()
        self.assertEqual(self.work.record_path(1).read_bytes(), newer)
        self.assertEqual((self.work.readings_dir / "prev" / "p001.json").read_bytes(), self.original)

    def test_malformed_sidecar_and_source_identity_are_never_silently_repaired(self):
        self.scan()
        path = self.work.dir / "annotations" / "p001.json"
        path.write_text("{damaged", encoding="utf-8")
        with self.assertRaises(ValueError):
            self.scan()
        self.assertEqual(path.read_text(encoding="utf-8"), "{damaged")
        (self.work.dir / "source.json").write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "지문"):
            self.scan()
        self.assertEqual(self.work.record_path(1).read_bytes(), self.original)

    def test_anomalous_multi_column_response_is_recorded_but_not_insertable(self):
        self.scan()
        reader = Reader()
        reader.reread_region = lambda *args: RegionReread(columns=[Column(n=1, t="甲"), Column(n=2, t="乙")])
        self.reread(reader)
        c = self.candidate()
        self.assertEqual(c["status"], "failed")
        self.assertEqual(len(c["subcolumns"][0]["runs"][0]["response"]["columns"]), 2)
        with self.assertRaises(ValueError):
            self.apply()

    def test_work_lock_rejects_competing_scan_without_touching_reading(self):
        with notes._work_lock(self.work.dir):
            with self.assertRaisesRegex(ValueError, "다른 창"):
                self.scan()
        self.assertEqual(self.work.record_path(1).read_bytes(), self.original)

    def horizontal_scan(self, count=5):
        found = detection()
        candidate = found["candidates"][0]
        candidate["direction"] = "horizontal"
        candidate["subcolumns"] = [{"side": f"line-{i + 1}",
            "bbox_px": [48, 48 + 20 * i, 112, 65 + 20 * i]} for i in range(count)]
        with patch("yeone_ocr.small_annotation_detection.detect_small_annotations", return_value=found):
            return notes.scan_work(self.source, self.work.dir, horizontal=True)

    def test_horizontal_five_rows_resume_in_order_and_preserve_human_record(self):
        self.horizontal_scan()
        result, reader, factory = self.reread(Reader(["甲", "別樂\ufe00", "硏\U000e0100"]), max_calls=3)
        self.assertTrue(result["run"]["stopped"])
        self.assertEqual(self.candidate()["status"], "detected")
        self.assertTrue(all(call[1] == notes.HORIZONTAL_KIND for call in reader.calls))
        self.assertIn("왼쪽→오른쪽", factory.call_args.kwargs["system"])
        with self.assertRaises(ValueError):
            self.apply()
        result, reader, _ = self.reread(Reader(["丁", "戊"]))
        self.assertEqual([call[2] for call in reader.calls], ["note-1-line-4", "note-1-line-5"])
        self.assertEqual(self.candidate()["combined_text"], "甲別樂\ufe00硏\U000e0100丁戊")
        self.assertEqual(self.work.record_path(1).read_bytes(), self.original)
        report = notes.write_report(self.work.dir).read_text(encoding="utf-8")
        self.assertIn("행 5", report)
        inserted = self.apply()
        self.assertEqual(inserted["inserted_text"], "甲別樂\ufe00硏\U000e0100丁戊")
        self.assertEqual(Path(inserted["backup_path"]).read_bytes(), self.original)

    def test_horizontal_only_missing_row_can_be_inserted(self):
        self.rec["merged"]["regions"][0]["paragraphs"][0]["columns"][0]["text"] = "甲乙丙丁人手樂\ufe00校正"
        self.work.save_record(1, self.rec)
        original = self.work.record_path(1).read_bytes()
        self.horizontal_scan()
        self.reread(Reader(["甲", "乙", "丙", "丁", "戊"]))
        self.assertEqual(self.candidate()["status"], "partial_present")
        with self.assertRaises(ValueError):
            self.apply(side="line-1", expected_text="甲乙丙丁人手樂\ufe00校正")
        result = self.apply(side="line-5", offset=4, expected_text="甲乙丙丁人手樂\ufe00校正")
        self.assertEqual(result["inserted_text"], "戊")
        self.assertEqual(Path(result["backup_path"]).read_bytes(), original)
        self.assertEqual(self.work.load_record(1)["adjudications"], self.rec["adjudications"])

    def test_horizontal_multiple_missing_rows_do_not_finish_partial_candidate(self):
        self.horizontal_scan()
        self.reread(Reader(["人", "手", "甲", "乙", "丙"]))
        with self.assertRaisesRegex(ValueError, "나머지 모든"):
            self.apply(side="line-5")
        self.assertEqual(self.work.record_path(1).read_bytes(), self.original)

    def test_partial_rows_cannot_reuse_one_occurrence_or_overlapping_text(self):
        for rows in (["人", "人", "未收"], ["人", "人手", "未收"]):
            with self.subTest(rows=rows):
                self.horizontal_scan(count=3)
                self.reread(Reader(rows))
                with self.assertRaisesRegex(ValueError, "기존 판독 위치|나머지 모든"):
                    self.apply(side="line-3")
                self.assertEqual(self.work.record_path(1).read_bytes(), self.original)
                # Isolate requests between the two distinct mocked model replies.
                (self.work.dir / "annotations" / "p001.json").unlink()

    def test_duplicate_phrase_cannot_certify_several_unknown_positions(self):
        candidates = []
        for index in range(3):
            candidate = deepcopy(detection()["candidates"][0])
            candidate["id"] = f"note-{index}"
            candidate["subcolumns"][0]["text"] = "人"
            candidate["subcolumns"][1]["text"] = "手"
            candidates.append(candidate)
        notes._refresh_candidates(candidates, self.rec)
        self.assertEqual([c["status"] for c in candidates], ["position_unverified"] * 3)
        complete = deepcopy(self.rec)
        complete["merged"]["regions"][0]["paragraphs"][0]["columns"][0]["text"] = "人手人手人手"
        notes._refresh_candidates(candidates, complete)
        self.assertEqual([c["status"] for c in candidates], ["already_present"] * 3)

    def test_matching_text_elsewhere_on_page_does_not_suppress_reread(self):
        record = deepcopy(self.rec)
        record["merged"]["regions"][0]["bbox"] = [0, 0, 100, 1000]
        self.work.save_record(1, record)
        self.scan(); self.reread(Reader(["人", "手"]))
        self.assertEqual(self.candidate()["status"], "read")

    def test_extra_nonmatching_region_cannot_certify_shared_occurrence_twice(self):
        record = deepcopy(self.rec)
        extra = deepcopy(record["merged"]["regions"][0])
        extra.update(id="extra", bbox=[500, 0, 1000, 1000])
        extra["paragraphs"][0]["columns"][0]["text"] = "別字"
        record["merged"]["regions"].append(extra)
        candidates = []
        for index, box in enumerate(([100, 200, 200, 800], [600, 200, 700, 800])):
            c = deepcopy(detection()["candidates"][0])
            c.update(id=f"note-{index}", bbox=box)
            c["subcolumns"][0]["text"] = "人"
            c["subcolumns"][1]["text"] = "手"
            candidates.append(c)
        notes._refresh_candidates(candidates, record)
        self.assertEqual([c["status"] for c in candidates], ["position_unverified"] * 2)

    def test_duplicate_position_uncertainty_blocks_insertion(self):
        self.scan(); self.reread(Reader(["人手", "未收"]))
        path = self.work.dir / "annotations" / "p001.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        duplicate = deepcopy(data["candidates"][0])
        duplicate["id"] = "note-2"
        data["candidates"].append(duplicate)
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        self.assertEqual(self.candidate()["status"], "position_unverified")
        with self.assertRaisesRegex(ValueError, "기존 판독 위치"):
            self.apply(side="left")
        self.assertEqual(self.work.record_path(1).read_bytes(), self.original)

    def test_inserting_one_note_does_not_certify_another_occurrence(self):
        self.scan(); self.reread(Reader(["新右", "新左"]))
        path = self.work.dir / "annotations" / "p001.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        duplicate = deepcopy(data["candidates"][0])
        duplicate["id"] = "note-2"
        data["candidates"].append(duplicate)
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        self.apply()
        statuses = [c["status"] for c in notes.load_bundle(self.work.dir)["pages"][0]["candidates"]]
        self.assertEqual(statuses, ["applied", "position_unverified"])

    def test_repeated_rows_reserve_every_occurrence_before_other_candidate_inserts(self):
        record = deepcopy(self.rec)
        record["merged"]["regions"][0]["paragraphs"][0]["columns"][0]["text"] = "AAX"
        self.work.save_record(1, record)
        original = self.work.record_path(1).read_bytes()
        self.horizontal_scan(count=3)
        self.reread(Reader(["A", "A", "X"]))
        path = self.work.dir / "annotations" / "p001.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        duplicate = deepcopy(data["candidates"][0])
        duplicate["id"] = "note-2"
        duplicate["subcolumns"] = duplicate["subcolumns"][:2]
        duplicate["subcolumns"][1]["text"] = "Y"
        data["candidates"].append(duplicate)
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "기존 판독 위치"):
            notes.apply_candidate(self.source, self.work.dir, 1, "note-2", "r1", 0, 0, 3, "AAX", side="line-2")
        self.assertEqual(self.work.record_path(1).read_bytes(), original)

    def test_scan_direction_can_be_explicit_or_from_saved_region(self):
        detector_path = "yeone_ocr.small_annotation_detection.detect_small_annotations"
        with patch(detector_path, side_effect=detection) as detector:
            notes.scan_work(self.source, self.work.dir, horizontal=True)
            self.assertTrue(detector.call_args.kwargs["horizontal"])
            self.rec["mode"] = "horizontal_zh"
            self.work.save_record(1, self.rec)
            notes.scan_work(self.source, self.work.dir, horizontal=False)
            self.assertFalse(detector.call_args.kwargs["horizontal"])
            self.scan()
            self.assertTrue(detector.call_args.kwargs["horizontal"])

    def test_horizontal_prompt_does_not_depend_on_global_mode(self):
        from yeone_ocr import prompts
        old = prompts.MODE
        try:
            prompts.set_mode("vertical")
            self.assertIn("왼쪽→오른쪽", prompts.region_prompt(notes.HORIZONTAL_KIND, "r", 1))
            self.assertIn("변환을 하지 않는다", prompts.small_annotation_system_prompt(horizontal=True))
            self.assertIn("위→아래", prompts.small_annotation_system_prompt())
            self.assertEqual(prompts.MODE, "vertical")
        finally:
            prompts.MODE = old


if __name__ == "__main__":
    unittest.main()
