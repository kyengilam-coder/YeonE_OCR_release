"""Synthetic-only regression tests for comparison, provenance, and recovery."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

from ab_ocr import ocr
from yeone_ocr.comparison import ComparisonProject, ComparisonError, SourceChangedError, latest_runs, run_page


class ComparisonTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(prefix="yeone-compare-test-")
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.source = self.root / "synthetic.png"
        Image.new("RGB", (400, 200), "white").save(self.source)
        self.project = ComparisonProject.open(self.source)

    def raw(self, text="原文，한글。", engine="tesseract", languages="kor+chi_tra", **changes):
        value = ocr.RecognitionResult(text=text, words=[{"text": text, "bbox": [10, 10, 390, 190], "confidence": 75.0}],
                                      source_path=str(self.source), source_sha256=self.project.source_sha256,
                                      page_number=1, image_width=400, image_height=200,
                                      engine_version="synthetic-1", languages=languages, psm=6 if engine == "tesseract" else None,
                                      duration_seconds=0.01, engine=engine).to_dict()
        value.update(changes)
        return value

    def add(self, text, engine="tesseract", languages="chi_tra", **changes):
        return self.project.add_result(self.raw(text, engine, languages, **changes))

    def save(self):
        target = self.root / "test.yeone-compare.json"
        self.project.save(target)
        return target

    def test_raw_and_snapshots_are_independent_and_preserve_unicode(self):
        text = "神 神 神\ufe00，,\n 汉 漢"
        raw = self.raw(text)
        self.project.add_result(raw)
        raw["text"] = "mutated"
        snapshot = self.project.page(1)
        snapshot["runs"][0]["raw"]["words"][0]["text"] = "mutated"
        self.assertEqual(self.project.text(1), text)
        self.assertEqual(self.project.page(1)["runs"][0]["raw"]["words"][0]["text"], text)
        target = self.save()
        reopened = ComparisonProject.load(target)
        self.assertEqual(reopened.text(1), text)
        self.assertFalse(reopened.dirty)

    def test_rerecognition_preserves_all_raw_and_corrections(self):
        first = self.add("原文")
        self.project.set_text(1, "人文", reason="image checked")
        self.project.set_reviewed(1)
        second = self.add("原字")
        page = self.project.page(1)
        self.assertEqual([r["id"] for r in page["runs"]], [first, second])
        self.assertEqual([r["raw"]["text"] for r in page["runs"]], ["原文", "原字"])
        self.assertEqual(self.project.text(1), "人文")
        self.assertFalse(page["reviewed"])
        self.assertEqual(len(page["edits"]), 1)

    def test_historical_import_keeps_active_baseline_and_comparison(self):
        current = self.add("新文")
        self.add("新字", "easyocr")
        self.project.compare_page(1, "tesseract")
        self.project.set_text(1, "人校正")
        self.project.set_reviewed(1)
        old = self.project.add_result(self.raw("舊文", "tesseract", "chi_tra"), activate=False)
        page = self.project.page(1)
        self.assertEqual(page["baseline_run_id"], current)
        self.assertEqual(latest_runs(page)["tesseract"]["id"], current)
        self.assertEqual(page["runs"][-1]["id"], old)
        self.assertTrue(page["reviewed"])
        self.project.compare_page(1, "tesseract")
        self.assertEqual(self.project.page(1)["baseline_run_id"], current)
        self.assertEqual(self.project.text(1), "人校正")
        ComparisonProject.load(self.save())

    def test_manual_history_undo_is_append_only_and_roundtrips(self):
        self.add("甲")
        self.project.set_text(1, "乙")
        self.project.set_text(1, "丙")
        self.assertTrue(self.project.undo_edit(1))
        self.assertEqual(self.project.text(1), "乙")
        self.assertTrue(self.project.undo_edit(1))
        self.assertEqual(self.project.text(1), "甲")
        self.assertFalse(self.project.undo_edit(1))
        self.assertEqual([e["kind"] for e in self.project.page(1)["edits"]], ["manual", "manual", "undo", "undo"])
        loaded = ComparisonProject.load(self.save())
        self.assertEqual(loaded.page(1)["edits"], self.project.page(1)["edits"])
        self.assertIsNone(loaded.page(1)["corrected_text"])

    def test_comparison_detects_insert_delete_substitute_and_keeps_raw(self):
        self.add("甲乙丙丁")
        self.add("甲乙丁戊", "rapidocr")
        self.add("甲己丙丁", "easyocr")
        suggestions = self.project.compare_page(1, baseline="tesseract")
        self.assertTrue(any(s["baseline_text"] == "乙丙" for s in suggestions))
        self.assertTrue(any(s["kind"] == "insertion" for s in suggestions))
        self.assertTrue(any(s["candidates"]["rapidocr"] == "戊" for s in suggestions))
        self.assertIsNone(self.project.page(1)["corrected_text"])
        self.assertEqual(self.project.text(1), "甲乙丙丁")
        self.assertTrue(all(s["decision"] == "pending" for s in suggestions))
        self.assertTrue(all(e["bbox"] is not None for s in suggestions for e in s["evidence"]))
        ComparisonProject.load(self.save())

    def test_exact_punctuation_variants_and_whitespace_separately_classified(self):
        self.add("神， A\n文")
        self.add("神, A 文", "easyocr")
        suggestions = self.project.compare_page(1, "tesseract")
        self.assertTrue(any(s["kind"] == "whitespace" for s in suggestions))
        self.assertTrue(any("神" in s["baseline_text"] for s in suggestions))
        self.assertEqual(self.project.text(1), "神， A\n文")

    def test_multiple_independent_candidate_accepts_rebase_offsets(self):
        self.add("甲乙丙丁戊己庚")
        self.add("甲仁丙丁戊義庚", "easyocr")
        suggestions = self.project.compare_page(1, "tesseract")
        self.assertEqual(len(suggestions), 2)
        self.project.accept_candidate(1, suggestions[0]["id"], "easyocr")
        self.project.accept_candidate(1, suggestions[1]["id"], "easyocr")
        self.assertEqual(self.project.text(1), "甲仁丙丁戊義庚")
        self.assertEqual([e["kind"] for e in self.project.page(1)["edits"]], ["candidate", "candidate"])
        self.assertEqual(self.project.page(1)["runs"][0]["raw"]["text"], "甲乙丙丁戊己庚")
        ComparisonProject.load(self.save())

    def test_overlap_with_manual_edit_prevents_stale_candidate(self):
        self.add("甲乙丙")
        self.add("甲己丙", "easyocr")
        suggestion = self.project.compare_page(1, "tesseract")[0]
        self.project.set_text(1, "甲丁丙")
        with self.assertRaises(ComparisonError):
            self.project.accept_candidate(1, suggestion["id"], "easyocr")
        self.assertEqual(self.project.text(1), "甲丁丙")

    def test_insertion_accept_preserves_following_raw_offsets(self):
        self.add("甲乙丙丁")
        self.add("甲仁乙丙義", "easyocr")
        suggestions = self.project.compare_page(1, "tesseract")
        self.assertEqual(len(suggestions), 2)
        for suggestion in suggestions:
            self.project.accept_candidate(1, suggestion["id"], "easyocr")
        self.assertEqual(self.project.text(1), "甲仁乙丙義")

    def test_reject_candidate_preserves_audit_and_no_correction(self):
        self.add("甲")
        self.add("乙", "easyocr")
        suggestion = self.project.compare_page(1, "tesseract")[0]
        self.project.reject_candidate(1, suggestion["id"])
        self.project.compare_page(1, "tesseract")
        self.assertEqual(self.project.page(1)["suggestions"][0]["decision"], "rejected")
        self.assertIsNone(self.project.page(1)["corrected_text"])
        self.assertEqual(len(self.project.page(1)["decisions"]), 1)
        ComparisonProject.load(self.save())

    def test_candidate_undo_restores_pending_proposal_without_erasing_decision(self):
        self.add("甲")
        self.add("乙", "easyocr")
        suggestion = self.project.compare_page(1, "tesseract")[0]
        self.project.accept_candidate(1, suggestion["id"], "easyocr")
        self.project.undo_edit(1)
        self.project.compare_page(1, "tesseract")
        self.assertEqual(self.project.text(1), "甲")
        self.assertEqual(self.project.page(1)["suggestions"][0]["decision"], "pending")
        self.assertEqual([d["decision"] for d in self.project.page(1)["decisions"]], ["accepted", "undone"])
        ComparisonProject.load(self.save())

    def test_rejected_decision_survives_switching_baseline_and_back(self):
        self.add("甲")
        self.add("乙", "easyocr")
        suggestion = self.project.compare_page(1, "tesseract")[0]
        self.project.reject_candidate(1, suggestion["id"])
        self.project.compare_page(1, "easyocr")
        self.project.compare_page(1, "tesseract")
        self.assertEqual(self.project.page(1)["suggestions"][0]["decision"], "rejected")

    def test_korean_models_abstain_on_han_omissions(self):
        self.add("원문漢字입니다", languages="kor+chi_tra")
        self.add("원문입니다", "easyocr", "kor")
        self.add("원문입니다", "rapidocr", "kor")
        with self.project._lock:
            self.project._page(1)["profile"] = "mixed"
        suggestions = self.project.compare_page(1)
        self.assertEqual(self.project.page(1)["baseline_engine"], "tesseract")
        han = next(s for s in suggestions if "漢" in s["baseline_text"])
        eligible = {e["engine"]: e["eligible"] for e in han["evidence"]}
        self.assertEqual(eligible, {"tesseract": True, "easyocr": False, "rapidocr": False})
        with self.assertRaises(ComparisonError):
            self.project.accept_candidate(1, han["id"], "easyocr")
        ComparisonProject.load(self.save())

    def test_other_engines_can_be_retained_but_not_silently_voted(self):
        self.add("甲")
        self.add("乙", "yeone-ai", "ai")
        self.assertEqual(self.project.compare_page(1, "tesseract"), [])
        suggestions = self.project.compare_page(1, "yeone-ai")
        self.assertEqual(len(suggestions), 1)
        self.assertIn("yeone-ai", suggestions[0]["candidates"])
        ComparisonProject.load(self.save())

    def test_different_raster_or_profile_is_excluded(self):
        self.add("甲")
        self.add("乙", "easyocr", image_width=500)
        self.project.compare_page(1, "tesseract")
        comparison = self.project.page(1)["comparisons"][-1]
        self.assertEqual([r["engine"] for r in comparison["excluded_runs"]], ["easyocr"])
        self.assertEqual(comparison["suggestions"], [])

    def test_historical_source_path_is_preserved_when_hash_matches(self):
        raw = self.raw(source_path="Z:/historical/source.png")
        self.project.add_result(raw)
        self.assertEqual(self.project.page(1)["runs"][0]["raw"]["source_path"], "Z:/historical/source.png")

    def test_source_mutation_rejects_results_save_and_export(self):
        self.add("甲")
        self.source.write_bytes(b"changed source")
        with self.assertRaises(SourceChangedError):
            self.project.add_result(self.raw("乙"))
        with self.assertRaises(SourceChangedError):
            self.project.save(self.root / "project.json")
        with self.assertRaises(SourceChangedError):
            self.project.export_text(self.root / "result.txt")
        self.assertEqual(len(self.project.page(1)["runs"]), 1)

    def test_missing_source_recovery_preserves_completed_work(self):
        self.add("原文")
        self.project.set_text(1, "校正文")
        self.source.unlink()
        recovery = self.root / "recovery.json"
        self.project.save_recovery(recovery)
        with self.assertRaises(SourceChangedError):
            ComparisonProject.load(recovery)
        loaded = ComparisonProject.load(recovery, allow_missing_source=True)
        self.assertEqual(loaded.text(1), "校正文")
        with self.assertRaises(SourceChangedError):
            loaded.save()

    def test_relocation_requires_identical_bytes(self):
        self.add("原文")
        target = self.save()
        moved = self.root / "moved.png"
        self.source.rename(moved)
        self.assertEqual(ComparisonProject.load(target, source_path=moved).text(1), "原文")
        Image.new("RGB", (100, 100), "black").save(self.source)
        with self.assertRaises(SourceChangedError):
            ComparisonProject.load(target)

    def test_save_conflict_and_other_project_protection(self):
        self.add("甲")
        target = self.save()
        other = ComparisonProject.load(target)
        other.set_text(1, "乙")
        other.save()
        self.project.set_text(1, "丙")
        with self.assertRaises(ComparisonError):
            self.project.save()
        unrelated = ComparisonProject.open(self.source)
        with self.assertRaises(ComparisonError):
            unrelated.save(target)
        self.assertEqual(ComparisonProject.load(target).text(1), "乙")

    def test_atomic_failure_keeps_previous_project_and_removes_temp(self):
        self.add("甲")
        target = self.save()
        before = target.read_bytes()
        self.project.set_text(1, "乙")
        with patch("yeone_ocr.comparison.os.replace", side_effect=OSError("synthetic failure")):
            with self.assertRaises(OSError):
                self.project.save()
        self.assertEqual(target.read_bytes(), before)
        self.assertFalse(list(self.root.glob("*.tmp")))
        self.assertTrue(self.project.dirty)

    def test_source_and_project_are_protected_from_overwrites(self):
        self.add("甲")
        source_bytes = self.source.read_bytes()
        with self.assertRaises(ComparisonError):
            self.project.save(self.source)
        target = self.save()
        with self.assertRaises(ComparisonError):
            self.project.save_recovery(target)
        with self.assertRaises(ComparisonError):
            self.project.export_text(target)
        self.assertEqual(self.source.read_bytes(), source_bytes)

    def test_export_is_exact_and_unprocessed_pages_are_rejected(self):
        self.add("神，\n漢字\ufe00")
        self.project.set_text(1, "神，\n漢字\ufe00。")
        target = self.project.export_text(self.root / "corrected.txt")
        self.assertEqual(target.read_bytes(), "神，\n漢字\ufe00。".encode())
        fresh = ComparisonProject.open(self.source)
        with self.assertRaises(ComparisonError):
            fresh.export_text(self.root / "empty.txt")

    def test_load_rejects_tampered_raw_history_bounds_and_numbers(self):
        self.add("甲")
        self.project.set_text(1, "乙")
        target = self.save()
        original = json.loads(target.read_text(encoding="utf-8"))
        mutations = [
            lambda v: v["pages"]["1"]["runs"][0]["raw"].update(text="tampered"),
            lambda v: v["pages"]["1"].update(corrected_text="not audited"),
            lambda v: v["pages"]["1"]["edits"][0].update(before="false history"),
            lambda v: v["pages"]["1"]["runs"][0]["raw"]["words"][0].update(bbox=[-1, 0, 20, 20]),
            lambda v: v["source"].update(page_count=True),
            lambda v: v["pages"]["1"].update(number=True),
        ]
        for mutate in mutations:
            value = copy.deepcopy(original)
            mutate(value)
            target.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
            with self.subTest(mutate=mutate), self.assertRaises(ComparisonError):
                ComparisonProject.load(target)

    def test_load_rejects_duplicate_fields_nan_and_future_schema(self):
        target = self.root / "bad.json"
        for content in ('{"format":"yeone-ocr-comparison","format":"other","version":1}',
                        '{"format":"yeone-ocr-comparison","version":NaN}',
                        '{"format":"yeone-ocr-comparison","version":2}'):
            target.write_text(content, encoding="utf-8")
            with self.subTest(content=content), self.assertRaises(ComparisonError):
                ComparisonProject.load(target)

    def test_suggestion_fabrication_is_rejected_on_load(self):
        self.add("甲")
        self.add("乙", "easyocr")
        self.project.compare_page(1, "tesseract")
        target = self.save()
        value = json.loads(target.read_text(encoding="utf-8"))
        value["pages"]["1"]["suggestions"][0]["candidates"]["easyocr"] = "丙"
        target.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        with self.assertRaises(ComparisonError):
            ComparisonProject.load(target)


class RunPageTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(prefix="yeone-run-test-")
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.source = self.root / "synthetic.png"
        Image.new("RGB", (100, 100), "white").save(self.source)
        self.project = ComparisonProject.open(self.source)
        self.calls = []

    def recognize(self, path, **kwargs):
        self.calls.append(kwargs)
        engine = kwargs["engine"]
        return ocr.RecognitionResult(text={"tesseract": "甲乙", "rapidocr": "甲丙", "easyocr": "甲丁"}[engine],
                                      words=[], source_path=str(path), source_sha256=self.project.source_sha256,
                                      page_number=kwargs["page_number"], image_width=100, image_height=100,
                                      engine_version="synthetic", languages=kwargs["languages"], psm=kwargs["psm"],
                                      duration_seconds=0.01, engine=engine)

    def test_three_sequential_engines_checkpoint_and_resume(self):
        checkpoints = []
        with patch("yeone_ocr.comparison.ocr.recognize_file", side_effect=self.recognize):
            result = run_page(self.project, 1, on_result=lambda raw: checkpoints.append((raw.engine, len(self.project.page(1)["runs"]))))
            resumed = run_page(self.project, 1)
        self.assertEqual(result["completed"], ["tesseract", "rapidocr", "easyocr"])
        self.assertEqual(checkpoints, [("tesseract", 1), ("rapidocr", 2), ("easyocr", 3)])
        self.assertEqual(resumed["skipped"], ["tesseract", "rapidocr", "easyocr"])
        self.assertEqual(len(self.calls), 3)
        self.assertIsNone(self.project.page(1)["corrected_text"])

    def test_installer_manifest_change_invalidates_only_changed_engine_cache(self):
        engine_root = self.root / "engine-home"
        for engine in ("tesseract", "rapidocr", "easyocr"):
            folder = engine_root / "tools" / engine
            folder.mkdir(parents=True)
            (folder / "installation.json").write_text('{"revision":1}', encoding="utf-8")
        with patch("yeone_ocr.comparison.ocr.recognize_file", side_effect=self.recognize):
            run_page(self.project, 1, engine_root=engine_root)
            (engine_root / "tools" / "easyocr" / "installation.json").write_text('{"revision":2}', encoding="utf-8")
            result = run_page(self.project, 1, engine_root=engine_root)
        self.assertEqual(result["completed"], ["easyocr"])
        self.assertEqual(result["skipped"], ["tesseract", "rapidocr"])
        self.assertEqual(len(self.project.page(1)["runs"]), 4)

    def test_model_bytes_change_without_manifest_change_invalidates_cache(self):
        engine_root = self.root / "engine-home"
        model = engine_root / "tools" / "tesseract" / "tessdata" / "chi_tra.traineddata"
        model.parent.mkdir(parents=True)
        model.write_bytes(b"synthetic model one")
        def recognize(path, **kwargs):
            result = self.recognize(path, **kwargs)
            if kwargs["engine"] == "tesseract":
                result.engine_metadata["model_files"] = {"chi_tra": {"sha256": ocr.file_sha256(model)}}
            return result
        with patch("yeone_ocr.comparison.ocr.recognize_file", side_effect=recognize):
            run_page(self.project, 1, engine_root=engine_root)
            model.write_bytes(b"synthetic model two")
            result = run_page(self.project, 1, engine_root=engine_root)
        self.assertEqual(result["completed"], ["tesseract"])
        self.assertEqual(result["skipped"], ["rapidocr", "easyocr"])

    def test_cancel_keeps_first_engine_and_resumes_remaining(self):
        stop = [False]
        with patch("yeone_ocr.comparison.ocr.recognize_file", side_effect=self.recognize):
            result = run_page(self.project, 1, should_stop=lambda: stop[0], on_result=lambda _: stop.__setitem__(0, True))
            stop[0] = False
            resumed = run_page(self.project, 1)
        self.assertTrue(result["cancelled"])
        self.assertEqual(result["completed"], ["tesseract"])
        self.assertEqual(resumed["completed"], ["rapidocr", "easyocr"])
        self.assertEqual(len(self.project.page(1)["runs"]), 3)

    def test_failure_keeps_other_results_and_hides_document_stderr(self):
        def recognize(path, **kwargs):
            if kwargs["engine"] == "rapidocr":
                raise ocr.OCRError("PRIVATE OCR TEXT FROM STDERR")
            return self.recognize(path, **kwargs)
        with patch("yeone_ocr.comparison.ocr.recognize_file", side_effect=recognize):
            result = run_page(self.project, 1)
        self.assertEqual(result["completed"], ["tesseract", "easyocr"])
        self.assertIn("rapidocr", result["errors"])
        self.assertNotIn("PRIVATE", json.dumps(self.project.to_dict()))

    def test_checkpoint_failure_stops_without_discarding_completed_raw(self):
        with patch("yeone_ocr.comparison.ocr.recognize_file", side_effect=self.recognize):
            with self.assertRaises(OSError):
                run_page(self.project, 1, on_result=lambda _: (_ for _ in ()).throw(OSError("disk full")))
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.project.completed_pages, [1])

    def test_force_reruns_and_preserves_human_correction(self):
        with patch("yeone_ocr.comparison.ocr.recognize_file", side_effect=self.recognize):
            run_page(self.project, 1)
            self.project.set_text(1, "人校正")
            run_page(self.project, 1, force=True)
        self.assertEqual(len(self.project.page(1)["runs"]), 6)
        self.assertEqual(self.project.text(1), "人校正")

    def test_profile_change_invalidates_cache_and_mixed_models_are_explicit(self):
        with patch("yeone_ocr.comparison.ocr.recognize_file", side_effect=self.recognize):
            run_page(self.project, 1)
            run_page(self.project, 1, profile="mixed")
        self.assertEqual([call["languages"] for call in self.calls[-3:]], ["kor+chi_tra", "kor", "kor"])
        self.assertEqual(self.project.page(1)["profile"], "mixed")
        self.assertEqual(self.project.page(1)["baseline_engine"], "tesseract")

    def test_vertical_models_remain_separate_from_legacy_vertical_reader(self):
        with patch("yeone_ocr.comparison.ocr.recognize_file", side_effect=self.recognize):
            run_page(self.project, 1, direction="vertical")
        self.assertEqual(self.calls[0]["languages"], "chi_tra_vert")
        self.assertEqual(self.calls[0]["psm"], 5)
        self.assertEqual([c["engine"] for c in self.calls], ["tesseract", "rapidocr", "easyocr"])

    def test_mixed_vertical_uses_both_vertical_language_packs(self):
        with patch("yeone_ocr.comparison.ocr.recognize_file", side_effect=self.recognize):
            run_page(self.project, 1, profile="mixed", direction="vertical")
        self.assertEqual(self.calls[0]["languages"], "kor_vert+chi_tra_vert")
        self.assertEqual(self.calls[0]["psm"], 5)

    def test_engine_cancellation_retains_previous_result(self):
        def recognize(path, **kwargs):
            if kwargs["engine"] == "rapidocr":
                raise ocr.OCRCancelled("cancel")
            return self.recognize(path, **kwargs)
        with patch("yeone_ocr.comparison.ocr.recognize_file", side_effect=recognize):
            result = run_page(self.project, 1)
        self.assertTrue(result["cancelled"])
        self.assertEqual(result["completed"], ["tesseract"])
        self.assertEqual(len(self.project.page(1)["runs"]), 1)


if __name__ == "__main__":
    unittest.main()
