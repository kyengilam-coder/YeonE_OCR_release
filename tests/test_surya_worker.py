from __future__ import annotations

import json
from pathlib import Path
import socket
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from ab_ocr.surya_worker import (ENGINE_VERSION, Worker, allow_only_loopback,
                                 atomic_output, digest_file, html_to_text,
                                 verify_installation)


class SuryaWorkerTests(unittest.TestCase):
    def test_html_preserves_hangul_hanja_variants_and_punctuation(self):
        source = "<p>한글 漢字 神 神。，“简体”</p><p>第二行<br>끝</p>"
        self.assertEqual(html_to_text(source), "한글 漢字 神 神。，“简体”\n第二行\n끝\n")

    def test_html_entities_and_table_structure_are_explicit_derivation(self):
        self.assertEqual(html_to_text("<table><tr><td>甲&amp;乙</td><td>丙</td></tr></table>"),
                         "甲&乙\t丙\t\n\n")

    def test_atomic_output_never_overwrites_raw_record(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "raw.json"
            atomic_output(path, {"text": "神 神"})
            original = path.read_bytes()
            with self.assertRaises(FileExistsError):
                atomic_output(path, {"text": "神 神"})
            self.assertEqual(path.read_bytes(), original)

    def _installation(self, root):
        models = root / "models"
        models.mkdir()
        model = models / "surya-2.gguf"
        model.write_bytes(b"synthetic model fixture")
        backend = root / "llama.cpp"
        backend.mkdir()
        binary = backend / "llama-server.exe"
        binary.write_bytes(b"synthetic backend fixture")
        manifest = {"engine": "surya", "engine_version": ENGINE_VERSION,
                    "models": [{"file": model.name, "sha256": digest_file(model)}],
                    "runtime_files": [{"file": "llama.cpp/llama-server.exe",
                                       "sha256": digest_file(binary)}]}
        (root / "installation.json").write_text(json.dumps(manifest), encoding="utf-8")
        return models, binary

    @patch("ab_ocr.surya_worker.importlib.metadata.version", return_value=ENGINE_VERSION)
    def test_verified_model_and_backend_return_exact_binary(self, _version):
        with tempfile.TemporaryDirectory() as temporary:
            models, binary = self._installation(Path(temporary))
            manifest, actual = verify_installation(models)
            self.assertEqual(actual, binary)
            self.assertEqual(manifest["engine"], "surya")

    @patch("ab_ocr.surya_worker.importlib.metadata.version", return_value=ENGINE_VERSION)
    def test_backend_modification_is_rejected(self, _version):
        with tempfile.TemporaryDirectory() as temporary:
            models, binary = self._installation(Path(temporary))
            binary.write_bytes(b"changed")
            with self.assertRaisesRegex(ValueError, "backend SHA-256"):
                verify_installation(models)

    @patch("ab_ocr.surya_worker.importlib.metadata.version", return_value=ENGINE_VERSION)
    def test_unrecorded_runtime_dll_is_rejected(self, _version):
        with tempfile.TemporaryDirectory() as temporary:
            models, binary = self._installation(Path(temporary))
            (binary.parent / "unrecorded.dll").write_bytes(b"extra")
            with self.assertRaisesRegex(ValueError, "Unrecorded"):
                verify_installation(models)

    def test_network_policy_allows_only_owned_loopback_port(self):
        with patch("socket.socket.connect"), patch("socket.socket.connect_ex"), \
                patch("socket.getaddrinfo", return_value=[]) as original_dns, \
                patch("socket.socket.sendto"):
            allow_only_loopback(34567)
            socket.getaddrinfo("127.0.0.1", 34567)
            original_dns.assert_called_once()
            with self.assertRaises(RuntimeError):
                socket.getaddrinfo("example.com", 34567)
            with self.assertRaises(RuntimeError):
                socket.getaddrinfo("127.0.0.1", 34568)

    def _mock_worker(self, finish_reason="stop"):
        worker = object.__new__(Worker)
        worker.manifest = {"model_repository": "synthetic/model", "model_revision": "synthetic",
                           "models": [], "runtime_files": [], "backend": "synthetic CPU",
                           "llama_cpp_release": "synthetic"}
        worker.manager = SimpleNamespace(responses=[])
        worker.completions = []
        worker.start = lambda: None
        raw = {"blocks": [{"html": "<p>한글 神 神。</p>", "reading_order": 0,
                           "bbox": [2, 3, 100, 24], "confidence": 1.0,
                           "skipped": False, "error": False}],
               "image_bbox": [0, 0, 120, 40]}

        def predict(_images):
            worker.manager.responses.append({"raw": "<div>한글 神 神。</div>"})
            worker.completions.append({"choices": [{"finish_reason": finish_reason,
                                                    "message": {"content": "<div>한글 神 神。</div>"}}]})
            return [SimpleNamespace(model_dump=lambda **kwargs: raw)]
        worker.predictor = predict
        return worker, raw

    def test_derived_view_preserves_raw_html_and_marks_confidence_unknown(self):
        from PIL import Image
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "synthetic.png"
            Image.new("RGB", (120, 40), "white").save(path)
            worker, raw = self._mock_worker()
            result = worker.recognize(path, "kor+chi_tra")
            self.assertEqual(result["text"], "한글 神 神。\n")
            self.assertIsNone(result["words"][0]["confidence"])
            self.assertEqual(result["words"][0]["bbox"], [2.0, 3.0, 100.0, 24.0])
            self.assertEqual(result["engine_metadata"]["raw_prediction"], raw)
            self.assertEqual(result["engine_metadata"]["input_file_sha256"], digest_file(path))
            self.assertIs(result["engine_metadata"]["generation_complete"], True)
            self.assertEqual(result["engine_metadata"]["generation_settings"]["max_tokens_full_page"], 12288)
            self.assertIs(result["engine_metadata"]["generation_settings"]["token_logprobs_requested"], False)

    def test_length_limited_generation_is_not_accepted_as_ocr(self):
        from PIL import Image
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "synthetic.png"
            Image.new("RGB", (120, 40), "white").save(path)
            worker, _raw = self._mock_worker(finish_reason="length")
            with self.assertRaisesRegex(ValueError, "not completed"):
                worker.recognize(path, "kor+chi_tra")
            self.assertEqual(worker.completions[0]["choices"][0]["finish_reason"], "length")


if __name__ == "__main__":
    unittest.main()
