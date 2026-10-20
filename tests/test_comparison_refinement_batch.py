"""Session batching retains durable primary/raw results across interruption."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import fitz

from ab_ocr import ocr
from yeone_ocr.comparison import ComparisonError, ComparisonProject, run_pages


class RefinementBatchTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix="refinement-batch-")
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        self.source = self.folder / "synthetic.pdf"
        with fitz.open() as document:
            document.new_page(width=80, height=40)
            document.new_page(width=80, height=40)
            document.save(self.source)
        self.project = ComparisonProject.open(self.source)
        self.calls, self.opened, self.closed = [], [], []
        self.failure = None
        owner = self

        class Session:
            def __init__(self, engine, languages, **kwargs):
                self.engine, self.languages, self.kwargs = engine, languages, kwargs

            def __enter__(self):
                owner.opened.append((self.engine, self.kwargs["max_requests"]))
                return self

            def __exit__(self, *_args):
                owner.closed.append(self.engine)

            def recognize_file(self, path, *, page_number, dpi, psm):
                owner.calls.append((self.engine, page_number, dpi, psm))
                if owner.failure and owner.failure(self.engine, page_number):
                    raise ocr.OCRError("synthetic failure")
                device = self.kwargs.get("device") or "cuda"
                metadata = {"generation_complete": True}
                if self.engine == "glmocr":
                    metadata.update(device_requested=device, actual_size_vram=1024 if device == "cuda" else 0)
                elif self.engine == "surya":
                    metadata["provider"] = device.upper()
                return ocr.RecognitionResult(text="甲乙丙丁", words=[], source_path=str(path),
                    source_sha256=owner.project.source_sha256, page_number=page_number,
                    image_width=320, image_height=160, engine_version="synthetic", languages=self.languages,
                    psm=psm, duration_seconds=0, engine=self.engine, engine_metadata=metadata)
        self.Session = Session

    def test_one_session_per_engine_and_primary_first_on_every_new_page(self):
        result = run_pages(self.project, (1, 2), engines=("easyocr", "glmocr", "surya"),
                           primary_engine="glmocr", session_factory=self.Session, max_requests=2)
        self.assertEqual([(e, n) for e, n, _, _ in self.calls],
                         [("glmocr", 1), ("glmocr", 2), ("easyocr", 1), ("easyocr", 2), ("surya", 1), ("surya", 2)])
        self.assertEqual(self.opened, [("glmocr", 2), ("easyocr", 2), ("surya", 2)])
        self.assertEqual(self.closed, ["glmocr", "easyocr", "surya"])
        self.assertFalse(result["cancelled"])
        self.assertTrue(all(self.project.page(n)["baseline_engine"] == "glmocr" for n in (1, 2)))

    def test_existing_baseline_and_human_edit_survive_batched_models(self):
        run_pages(self.project, (1,), engines=("easyocr",), session_factory=self.Session)
        self.project.set_text(1, "人校神𠮷")
        original = self.project.page(1)
        run_pages(self.project, (1, 2), engines=("glmocr", "surya"), primary_engine="glmocr", session_factory=self.Session)
        updated = self.project.page(1)
        self.assertEqual(updated["baseline_run_id"], original["baseline_run_id"])
        self.assertEqual(updated["edits"], original["edits"])
        self.assertEqual(updated["runs"][:1], original["runs"])
        self.assertEqual(self.project.page(2)["baseline_engine"], "glmocr")

    def test_cancel_checkpoint_reopen_keeps_primary_and_closes_session(self):
        stopped = [False];path = self.folder / "partial.json"
        def checkpoint(_result):
            self.project.save(path);stopped[0] = True
        result = run_pages(self.project, (1, 2), engines=("easyocr", "glmocr"), primary_engine="glmocr",
                           should_stop=lambda: stopped[0], on_result=checkpoint, session_factory=self.Session)
        self.assertTrue(result["cancelled"])
        self.assertEqual(self.closed, ["glmocr"])
        reopened = ComparisonProject.load(path)
        self.assertEqual(reopened.page(1)["baseline_engine"], "glmocr")
        self.assertEqual(len(reopened.page(1)["runs"]), 1)
        self.assertEqual(reopened.page(2)["runs"], [])

    def test_completed_cache_does_not_send_any_session_request(self):
        run_pages(self.project, (1, 2), engines=("glmocr",), primary_engine="glmocr", session_factory=self.Session)
        self.calls.clear()
        with patch("yeone_ocr.comparison._models_unchanged", return_value=True):
            result = run_pages(self.project, (1, 2), engines=("glmocr",), session_factory=self.Session)
        self.assertEqual(self.calls, [])
        self.assertTrue(all(result["pages"][n]["skipped"] == ["glmocr"] for n in (1, 2)))

    def test_checkpoint_failure_propagates_but_owns_cleanup_and_retains_raw(self):
        def checkpoint(_result):
            raise RuntimeError("synthetic disk failure")
        with self.assertRaises(RuntimeError):
            run_pages(self.project, (1, 2), engines=("glmocr",), primary_engine="glmocr",
                      on_result=checkpoint, session_factory=self.Session)
        self.assertEqual(self.closed, ["glmocr"])
        self.assertEqual(len(self.project.page(1)["runs"]), 1)
        self.assertEqual(self.project.page(1)["edits"], [])

    def test_engine_failure_does_not_remove_other_pages_or_other_engines(self):
        self.failure = lambda e, n: e == "glmocr" and n == 1
        result = run_pages(self.project, (1, 2), engines=("glmocr", "easyocr"), primary_engine="glmocr", session_factory=self.Session)
        self.assertIn("glmocr", result["pages"][1]["errors"])
        self.assertEqual(result["pages"][2]["completed"], ["glmocr", "easyocr"])
        self.assertEqual(self.closed, ["glmocr", "easyocr"])

    def test_invalid_scope_fails_before_session_or_page_creation(self):
        for numbers, engines in (((), ("glmocr",)), ((1, 1), ("glmocr",)), (([],), ("glmocr",)),
                                  ((1,), ("glmocr", "glmocr")), ((1,), ([],))):
            with self.subTest(numbers=numbers, engines=engines), self.assertRaises(ComparisonError):
                run_pages(self.project, numbers, engines=engines, session_factory=self.Session)
        self.assertEqual(self.opened, [])
        self.assertEqual(self.project.to_dict()["pages"], {})

    def test_explicit_device_change_never_reuses_other_device_reading(self):
        for initial, following in (("cuda", "cpu"), ("cpu", "cuda")):
            with self.subTest(initial=initial):
                self.project = ComparisonProject.open(self.source)
                run_pages(self.project, (1,), engines=("glmocr",), device=initial, session_factory=self.Session)
                self.calls.clear()
                with patch("yeone_ocr.comparison._models_unchanged", return_value=True):
                    outcome = run_pages(self.project, (1,), engines=("glmocr",), device=following, session_factory=self.Session)
                self.assertEqual(len(self.calls), 1)
                self.assertEqual(outcome["pages"][1]["skipped"], [])
                self.assertEqual(self.project.page(1)["runs"][-1]["request"]["device"], following)

    def test_request_label_alone_cannot_reuse_mismatched_effective_device(self):
        run_pages(self.project, (1,), engines=("glmocr",), device="cpu", session_factory=self.Session)
        run = self.project._pages[1]["runs"][-1]
        run["raw"]["engine_metadata"]["actual_size_vram"] = 1024
        from yeone_ocr.comparison import _digest
        run["raw_sha256"] = _digest(run["raw"])
        self.calls.clear()
        with patch("yeone_ocr.comparison._models_unchanged", return_value=True):
            outcome = run_pages(self.project, (1,), engines=("glmocr",), device="cpu", session_factory=self.Session)
        self.assertEqual(outcome["pages"][1]["skipped"], [])
        self.assertEqual(len(self.calls), 1)

    def test_explicit_device_with_matching_execution_evidence_reuses_cache(self):
        run_pages(self.project, (1,), engines=("glmocr",), device="cpu", session_factory=self.Session)
        self.calls.clear()
        with patch("yeone_ocr.comparison._models_unchanged", return_value=True):
            outcome = run_pages(self.project, (1,), engines=("glmocr",), device="cpu", session_factory=self.Session)
        self.assertEqual(outcome["pages"][1]["skipped"], ["glmocr"])
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
