"""Corpus evaluation checks use controlled transcripts, not OCR accuracy claims."""
from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from ab_ocr.benchmark import BenchmarkCancelled, ManifestError, evaluate_manifest, load_manifest


class BenchmarkTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="ab-ocr-benchmark-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.data = {"schema_version": 1, "name": "Evaluator smoke fixture", "synthetic": True,
                     "engines": [{"id": "a", "version": "fixture-1"}, {"id": "b", "version": "fixture-1"}], "pages": []}

    def add_page(self, page_id, reference, a, b, *, split="dev", tags=None):
        reference_path = self.root / f"{page_id}.reference.txt"
        reference_path.write_bytes(reference if isinstance(reference, bytes) else reference.encode("utf-8"))
        outputs = {}
        for engine_id, text in (("a", a), ("b", b)):
            if text is None:
                continue
            path = self.root / f"{page_id}.{engine_id}.txt"
            path.write_bytes(text if isinstance(text, bytes) else text.encode("utf-8"))
            outputs[engine_id] = path.name
        self.data["pages"].append({"id": page_id, "reference": reference_path.name, "outputs": outputs,
                                   "split": split, "tags": tags or []})

    def manifest(self):
        path = self.root / "manifest.json"
        path.write_text(json.dumps(self.data, ensure_ascii=False), encoding="utf-8")
        return path

    def test_micro_cer_includes_blank_insertions_and_weighted_lengths(self):
        self.add_page("long", "가" * 10, "가" * 10, "가" * 9, tags=["hangul"])
        self.add_page("short", "天", "地", "天", split="test", tags=["hanja"])
        self.add_page("blank", "", "字", "")
        report = evaluate_manifest(self.manifest())
        summary = report["engines"]["a"]["raw"]
        self.assertEqual((summary["distance"], summary["reference_length"]), (2, 11))
        self.assertAlmostEqual(summary["cer"], 2 / 11)
        self.assertEqual(summary["blank_reference_insertions"], 1)
        self.assertIsNone(report["pages"][2]["engines"]["a"]["comparison"]["raw"]["cer"])
        self.assertTrue(report["ranking"]["available"])
        self.assertEqual(report["ranking"]["raw"][0]["engine_id"], "b")
        self.assertEqual(report["engines"]["a"]["by_tag"]["hanja"]["raw"]["cer"], 1)
        self.assertEqual(report["engines"]["a"]["by_split"]["test"]["raw"]["reference_length"], 1)

    def test_missing_engine_page_cannot_disappear_or_win_ranking(self):
        self.add_page("one", "天地", "天地", "天地")
        self.add_page("two", "玄黃", "玄黄", None)
        report = evaluate_manifest(self.manifest())
        engine = report["engines"]["b"]
        self.assertFalse(engine["complete"])
        self.assertEqual(engine["completed_pages"], 1)
        self.assertEqual(engine["missing_output_pages"], ["two"])
        self.assertEqual(engine["raw"]["unscored_reference_length"], 2)
        self.assertEqual(engine["raw"]["expected_reference_length"], 4)
        self.assertFalse(report["ranking"]["available"])
        self.assertEqual(report["ranking"]["raw"], [])
        self.assertEqual(report["completeness"]["common_completed_pages"], ["one"])

    def test_missing_file_differs_from_empty_completed_output(self):
        self.add_page("page", "天地", "", "天地")
        (self.root / "page.b.txt").unlink()
        report = evaluate_manifest(self.manifest())
        self.assertEqual(report["engines"]["a"]["raw"]["deletions"], 2)
        self.assertTrue(report["engines"]["a"]["complete"])
        self.assertEqual(report["pages"][0]["engines"]["b"]["status"], "missing_output")

    def test_invalid_utf8_truth_and_output_are_explicit_and_hashed(self):
        self.add_page("bad-truth", b"\xff", "天地", "天地")
        self.add_page("bad-output", "天地", b"\xff", "天地")
        report = evaluate_manifest(self.manifest())
        self.assertEqual(report["completeness"]["missing_reference_pages"], ["bad-truth"])
        self.assertEqual(report["engines"]["a"]["unknown_reference_pages"], 1)
        bad = report["pages"][1]["engines"]["a"]
        self.assertEqual(bad["status"], "output_error")
        self.assertEqual(bad["input_status"], "invalid_utf8")
        self.assertEqual(bad["sha256"], hashlib.sha256(b"\xff").hexdigest())
        self.assertFalse(report["ranking"]["available"])

    def test_raw_newlines_preserved_and_transport_bom_removal_recorded(self):
        self.add_page("page", b"\xef\xbb\xbfA\r\n", b"A\n", b"A\r\n")
        report = evaluate_manifest(self.manifest(), policy={"whitespace": "remove"})
        self.assertEqual(report["engines"]["a"]["raw"]["reference_length"], 3)
        self.assertEqual(report["engines"]["a"]["raw"]["deletions"], 1)
        self.assertEqual(report["engines"]["a"]["normalized"]["reference_length"], 1)
        self.assertTrue(report["pages"][0]["reference"]["input_decoding"]["initial_bom_removed"])

    def test_blank_only_cohort_has_no_cer_or_rank(self):
        self.add_page("blank", "", "가", "")
        report = evaluate_manifest(self.manifest())
        self.assertTrue(report["completeness"]["complete"])
        self.assertIsNone(report["engines"]["a"]["raw"]["cer"])
        self.assertEqual(report["engines"]["a"]["raw"]["insertions"], 1)
        self.assertFalse(report["ranking"]["available"])

    def test_split_selection_and_hash_reproducibility(self):
        self.add_page("dev-page", "한글", "한글", "한굴", split="dev")
        self.add_page("test-page", "漢字", "漢字", "汉字", split="test")
        path = self.manifest()
        first = evaluate_manifest(path, split="test")
        second = evaluate_manifest(path, split="test")
        self.assertEqual(first["reproducibility"]["evaluation_id"], second["reproducibility"]["evaluation_id"])
        self.assertEqual(first["reproducibility"]["selected_page_ids"], ["test-page"])
        self.assertIn(str((self.root / "dev-page.reference.txt").resolve()), first["protected_input_paths"])
        (self.root / "test-page.a.txt").write_text("漢学", encoding="utf-8")
        changed = evaluate_manifest(path, split="test")
        self.assertNotEqual(first["reproducibility"]["corpus_sha256"], changed["reproducibility"]["corpus_sha256"])
        self.assertEqual(first["reproducibility"]["reference_corpus_sha256"], changed["reproducibility"]["reference_corpus_sha256"])
        normalized = evaluate_manifest(path, split="test", policy={"whitespace": "remove"})
        self.assertNotEqual(changed["reproducibility"]["evaluation_id"], normalized["reproducibility"]["evaluation_id"])

    def test_unsafe_paths_unknown_engines_and_duplicate_pages_rejected(self):
        self.add_page("page", "文", "文", "文")
        for invalid in ("../outside.txt", "C:\\outside.txt", "C:outside.txt", "\\\\server\\share\\file.txt"):
            with self.subTest(invalid=invalid):
                self.data["pages"][0]["reference"] = invalid
                with self.assertRaises(ManifestError):
                    load_manifest(self.manifest())
        self.data["pages"][0]["reference"] = "page.reference.txt"
        self.data["pages"][0]["outputs"]["typo"] = "page.a.txt"
        with self.assertRaises(ManifestError):
            load_manifest(self.manifest())
        del self.data["pages"][0]["outputs"]["typo"]
        self.data["pages"].append(dict(self.data["pages"][0]))
        with self.assertRaises(ManifestError):
            load_manifest(self.manifest())

    def test_duplicate_json_keys_rejected(self):
        path = self.root / "duplicate.json"
        path.write_text('{"schema_version":1,"schema_version":1}', encoding="utf-8")
        with self.assertRaisesRegex(ManifestError, "중복"):
            load_manifest(path)

    def test_exact_comparison_limit_is_failure_not_fabricated_score(self):
        self.add_page("page", "天地玄黃", "天地玄黄", "天地玄黃")
        report = evaluate_manifest(self.manifest(), max_cells=1)
        self.assertEqual(report["pages"][0]["engines"]["a"]["status"], "comparison_error")
        self.assertIsNone(report["engines"]["a"]["raw"]["cer"])
        self.assertFalse(report["ranking"]["available"])

    def test_cancellation_and_progress_do_not_mutate_inputs(self):
        self.add_page("one", "한글", "한굴", "한글")
        self.add_page("two", "漢字", "漢宇", "漢字")
        path = self.manifest()
        before = {p.name: p.read_bytes() for p in self.root.iterdir()}
        updates = []
        with self.assertRaises(BenchmarkCancelled):
            evaluate_manifest(path, progress=lambda done, total: updates.append((done, total)),
                              should_stop=lambda: bool(updates and updates[-1][0] >= 1))
        self.assertEqual(updates, [(0, 4), (1, 4)])
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.root.iterdir()})

    def test_example_is_labelled_synthetic_and_fully_evaluates(self):
        path = Path(__file__).resolve().parents[1] / "examples" / "benchmark" / "manifest.json"
        report = evaluate_manifest(path)
        self.assertTrue(report["synthetic"])
        self.assertTrue(report["completeness"]["complete"])
        self.assertIn("합성", report["warning"])
        json.dumps(report, ensure_ascii=False, allow_nan=False)


if __name__ == "__main__":
    unittest.main()
