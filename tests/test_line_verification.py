"""Switching off the per-line verification passes (opt-in, measured before any default change)."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

from ab_ocr import local_engines, ocr


class HostFlag(unittest.TestCase):
    def test_flag_only_for_verifying_engines_and_only_when_off(self):
        self.assertEqual(local_engines._verification_args("ppocrv6"), [])
        with local_engines.line_verification(False):
            self.assertEqual(local_engines._verification_args("ppocrv6"), ["--no-line-verification"])
            self.assertEqual(local_engines._verification_args("easyocr"), ["--no-line-verification"])
            self.assertEqual(local_engines._verification_args("rapidocr"), [])
            self.assertFalse(local_engines.line_verification_enabled())
        self.assertTrue(local_engines.line_verification_enabled())

    def test_unverified_runs_are_never_reused_for_verified_requests(self):
        from yeone_ocr.comparison import ComparisonProject, run_page
        with tempfile.TemporaryDirectory() as td:
            source = Path(td) / "page.png"
            Image.new("RGB", (400, 200), "white").save(source)
            project = ComparisonProject.open(source)
            calls = []

            def recognize(path, *, engine, **kwargs):
                calls.append(engine)
                return ocr.RecognitionResult(text="天地", engine=engine, source_path=str(project.source_path),
                    source_sha256=project.source_sha256, page_number=1, image_width=400, image_height=200,
                    engine_version="synthetic", languages="chi_tra", psm=None, duration_seconds=.01, words=[])
            with local_engines.line_verification(False):
                run_page(project, 1, engines=["ppocrv6", "rapidocr"], recognizer=recognize)
            run_page(project, 1, engines=["ppocrv6", "rapidocr"], recognizer=recognize)
            self.assertEqual(calls, ["ppocrv6", "rapidocr", "ppocrv6"])      # rapidocr has no such pass: reused
            requests = [r["request"] for r in project.page(1)["runs"]]
            self.assertEqual([r.get("line_verification") for r in requests], ["off", None, None])


def test_ppocrv6_skips_recrop_rereading_when_switched_off(tmp_path):
    import pytest
    v6 = pytest.importorskip("ab_ocr.ppocrv6_worker")
    worker = v6.Worker(tmp_path, verify_lines=False)
    result = mock.Mock(boxes=None, txts=("天地",), scores=(0.9,), elapse_list=[])
    image = tmp_path / "in.png"
    Image.new("RGB", (40, 20), "white").save(image)
    with mock.patch.object(worker, "engine", return_value=(mock.Mock(return_value=result), {})), \
            mock.patch.object(v6._shared, "serialize_lines", return_value=("天地", [])), \
            mock.patch.object(worker, "refine_horizontal", side_effect=AssertionError("must be skipped")), \
            mock.patch.object(v6.importlib.metadata, "version", return_value="3.10.0"):
        out = worker.recognize(image, "chi_tra", "horizontal")
    assert out["engine_metadata"]["recognition_policy"] == "ppocrv6-traditional-native-lines-unverified-v1"


def test_easyocr_cli_flag_reaches_the_worker(tmp_path):
    import pytest
    easy = pytest.importorskip("ab_ocr.easyocr_worker")
    seen = []

    class Worker:
        def __init__(self, model_dir, threads, verify_lines=True):
            seen.append(verify_lines)

        def recognize(self, source, languages, direction):
            return {"text": "", "engine_metadata": {}}
    source = tmp_path / "in.png"
    source.write_bytes(b"x")
    with mock.patch.object(easy, "EasyOCRWorker", Worker), mock.patch.object(easy, "write_result"):
        easy.main(["--model-dir", str(tmp_path), "--input", str(source), "--output", str(tmp_path / "o.json"),
                   "--languages", "chi_tra", "--no-line-verification"])
        easy.main(["--model-dir", str(tmp_path), "--input", str(source), "--output", str(tmp_path / "o.json"),
                   "--languages", "chi_tra"])
    assert seen == [False, True]
