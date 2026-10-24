from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from ab_ocr.rapidocr_worker import (
    CHINESE, CHINESE_MOBILE, CLASSIFIER, DETECTOR, KOREAN,
    preload_nvrtc, recognition_route, serialize_lines, verify_models, verify_providers, Worker,
)
from scripts import install_paddle_server


class PaddleServerWorkerTests(unittest.TestCase):
    def test_server_does_not_fall_back_to_korean_mobile(self):
        for language in ("kor", "kor+eng"):
            with self.subTest(language=language), self.assertRaisesRegex(ValueError, "no mobile fallback"):
                recognition_route(language, "server")
        self.assertEqual(recognition_route("kor", "mobile"), "korean")

    def test_server_supports_only_explicit_chinese_routes(self):
        for language in ("chi_sim", "chi_tra", "chi_sim+eng", "chi_tra+eng"):
            self.assertEqual(recognition_route(language, "server"), "ch")
        for language in ("kor+chi_tra", "eng", "auto", "chi_tra+kor"):
            with self.subTest(language=language), self.assertRaises(ValueError):
                recognition_route(language, "server")

    def _installation(self, root: Path, engine="paddle_server"):
        models = root / "models"
        models.mkdir()
        records = []
        for name in (DETECTOR, CLASSIFIER, CHINESE, CHINESE_MOBILE, KOREAN):
            content = name.encode("ascii")
            (models / name).write_bytes(content)
            records.append({"file": name, "sha256": hashlib.sha256(content).hexdigest()})
        (root / "installation.json").write_text(json.dumps({
            "engine": engine, "rapidocr_version": "3.9.2", "models": records,
        }), encoding="utf-8")
        return models

    def test_server_checks_and_records_server_model_bytes(self):
        with tempfile.TemporaryDirectory() as temporary:
            models = self._installation(Path(temporary))
            _, selected = verify_models(models, "chi_tra", "server")
            self.assertEqual([item["file"] for item in selected], [DETECTOR, CLASSIFIER, CHINESE])
            (models / CHINESE).write_bytes(b"changed")
            with self.assertRaisesRegex(ValueError, "SHA-256 mismatch"):
                verify_models(models, "chi_tra", "server")

    def test_legacy_mobile_manifest_and_route_remain_valid(self):
        with tempfile.TemporaryDirectory() as temporary:
            models = self._installation(Path(temporary), "rapidocr")
            _, selected = verify_models(models, "kor", "mobile")
            self.assertEqual(selected[-1]["file"], KOREAN)
            _, selected = verify_models(models, "chi_sim", "mobile")
            self.assertEqual(selected[-1]["file"], CHINESE_MOBILE)

    def test_independent_server_manifest_cannot_select_mobile(self):
        with tempfile.TemporaryDirectory() as temporary:
            models = self._installation(Path(temporary))
            with self.assertRaisesRegex(ValueError, "requires the server profile"):
                verify_models(models, "chi_sim", "mobile")

    def test_serialization_keeps_variants_and_punctuation(self):
        lines = ["한글 漢字 神 神", "简体，繁體。𠀀"]
        text, words = serialize_lines(
            [[[0, 0], [80, 0], [80, 20], [0, 20]], [[0, 30], [90, 30], [90, 50], [0, 50]]],
            lines, [0.9, 0.8],
        )
        self.assertEqual(text, "\n".join(lines))
        self.assertEqual([word["text"] for word in words], lines)
        self.assertEqual(words[1]["bbox"], [0.0, 30.0, 90.0, 50.0])

    def test_installer_refuses_existing_other_engine_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "rapidocr"
            with self.assertRaisesRegex(ValueError, "separate paddle_server"):
                install_paddle_server.main(["--target", str(target), "--models-only"])
            self.assertFalse(target.exists())

    def test_cuda_provider_cannot_silently_fall_back_to_cpu(self):
        class Session:
            def __init__(self, providers):
                self.providers = providers

            def get_providers(self):
                return self.providers

        def fake(providers):
            return SimpleNamespace(session=SimpleNamespace(session=Session(providers)))

        engine = SimpleNamespace(**{name: fake(["CUDAExecutionProvider", "CPUExecutionProvider"])
                                    for name in ("text_det", "text_cls", "text_rec")})
        actual = verify_providers(engine, "cuda")
        self.assertEqual(actual["text_rec"][0], "CUDAExecutionProvider")
        engine.text_rec = fake(["CPUExecutionProvider"])
        with self.assertRaisesRegex(ValueError, "no automatic backend fallback"):
            verify_providers(engine, "cuda")

    def test_explicit_cuda_does_not_change_existing_mobile_default(self):
        default = Worker(Path("models"))
        self.assertEqual(default.provider, "cpu")
        self.assertEqual(default.profile, "mobile")
        with self.assertRaisesRegex(ValueError, "explicit server profile"):
            Worker(Path("models"), "mobile", "cuda")

    def test_nvrtc_both_dlls_are_checked_before_gpu_inference(self):
        distribution = SimpleNamespace(version="12.8.93", locate_file=lambda _: Path("runtime/bin"))
        search = mock.Mock()
        with mock.patch("ab_ocr.rapidocr_worker.importlib.metadata.distribution", return_value=distribution), \
             mock.patch("ab_ocr.rapidocr_worker.os.add_dll_directory", return_value=search, create=True), \
             mock.patch("ctypes.WinDLL", create=True) as load:
            handles, searches = preload_nvrtc({"gpu_packages": {"nvidia-cuda-nvrtc-cu12": "12.8.93"}})
        self.assertEqual(len(handles), 2)
        self.assertEqual(searches, [search])
        self.assertEqual([Path(call.args[0]).name for call in load.call_args_list],
                         ["nvrtc-builtins64_128.dll", "nvrtc64_120_0.dll"])

    def test_nvrtc_missing_dll_closes_added_search_directory(self):
        distribution = SimpleNamespace(version="12.8.93", locate_file=lambda _: Path("runtime/bin"))
        search = mock.Mock()
        with mock.patch("ab_ocr.rapidocr_worker.importlib.metadata.distribution", return_value=distribution), \
             mock.patch("ab_ocr.rapidocr_worker.os.add_dll_directory", return_value=search, create=True), \
             mock.patch("ctypes.WinDLL", side_effect=OSError("missing DLL"), create=True):
            with self.assertRaises(OSError):
                preload_nvrtc({"gpu_packages": {"nvidia-cuda-nvrtc-cu12": "12.8.93"}})
        search.close.assert_called_once_with()

    def test_nvrtc_version_mismatch_fails_before_loading_any_dll(self):
        distribution = SimpleNamespace(version="12.9.86")
        with mock.patch("ab_ocr.rapidocr_worker.importlib.metadata.distribution", return_value=distribution), \
             mock.patch("ctypes.WinDLL", create=True) as load:
            with self.assertRaisesRegex(ValueError, "Pinned NVIDIA NVRTC"):
                preload_nvrtc({"gpu_packages": {"nvidia-cuda-nvrtc-cu12": "12.8.93"}})
        load.assert_not_called()


if __name__ == "__main__":
    unittest.main()
