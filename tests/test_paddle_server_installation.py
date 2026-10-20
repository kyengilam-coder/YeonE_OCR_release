from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from scripts import install_paddle_server_gpu as installer


class PaddleServerActivationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.cpu = self.root / "tools/paddle_server"
        self.gpu = self.root / "tools/paddle_server_gpu"
        models = []
        lock = json.loads(Path(installer.__file__).with_name("paddle-server-lock.json").read_text(encoding="utf-8"))
        for name in lock["models"]:
            content = ("synthetic public model fixture " + name).encode()
            models.append({"file": name, "sha256": hashlib.sha256(content).hexdigest()})
            for home in (self.cpu, self.gpu):
                (home / "models").mkdir(parents=True, exist_ok=True)
                (home / "models" / name).write_bytes(content)
        executable = "Scripts/python.exe" if os.name == "nt" else "bin/python"
        for home in (self.cpu, self.gpu):
            path = home / "venv" / executable
            path.parent.mkdir(parents=True)
            path.write_bytes(b"synthetic Python fixture")
        (self.gpu / "venv/library.dll").write_bytes(b"synthetic CUDA DLL fixture")
        self.cpu_manifest = {"engine": "paddle_server", "default_profile": "server", "execution_provider": "cpu", "models": models}
        self.original = (json.dumps(self.cpu_manifest, ensure_ascii=False, indent=4) + "\n").encode()
        (self.cpu / "installation.json").write_bytes(self.original)
        pins = json.loads(Path(installer.__file__).with_name("paddle-server-gpu-lock.json").read_text(encoding="utf-8"))["packages"]
        self.gpu_manifest = dict(self.cpu_manifest, execution_provider="cuda", onnxruntime_package="onnxruntime-gpu",
                                 gpu_packages=pins, package_provenance="pip-gpu-install-report.json")
        (self.gpu / "installation.json").write_text(json.dumps(self.gpu_manifest), encoding="utf-8")
        (self.gpu / "pip-gpu-install-report.json").write_text('{"install":[],"synthetic":true}', encoding="utf-8")

    def test_activation_copies_runtime_and_preserves_exact_cpu_bytes(self):
        source = installer._runtime_inventory(self.gpu / "venv")
        result = installer.activate_gpu(self.root, self.gpu)
        self.assertEqual(result["status"], "cuda_active")
        self.assertEqual((self.cpu / "installation-cpu.json").read_bytes(), self.original)
        self.assertEqual(installer._runtime_inventory(self.cpu / "venv_cuda"), source)
        self.assertEqual(installer._runtime_inventory(self.gpu / "venv"), source)
        self.assertEqual((self.cpu / "venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")).read_bytes(), b"synthetic Python fixture")
        active = json.loads((self.cpu / "installation.json").read_text(encoding="utf-8"))
        self.assertEqual(active["execution_provider"], "cuda")
        self.assertTrue(active["python_executable"].startswith("venv_cuda/"))
        self.assertEqual(active["runtime_root"], ".")
        self.assertEqual({item["filename"] for item in active["runtime_files"]},
                         {"venv_cuda/" + relative for relative in source})

    def test_restore_returns_exact_original_manifest_without_removing_gpu(self):
        installer.activate_gpu(self.root, self.gpu)
        result = installer.restore_cpu(self.root)
        self.assertEqual(result["status"], "cpu_restored")
        self.assertEqual((self.cpu / "installation.json").read_bytes(), self.original)
        self.assertEqual([event["action"] for event in result["events"]], ["activate_cuda", "restore_cpu"])
        self.assertTrue((self.cpu / "venv_cuda/library.dll").is_file())

    def test_changed_canonical_manifest_is_not_overwritten(self):
        installer.activate_gpu(self.root, self.gpu)
        path = self.cpu / "installation.json"
        changed = path.read_bytes() + b" \n"
        path.write_bytes(changed)
        with self.assertRaisesRegex(ValueError, "manifest changed"):
            installer.activate_gpu(self.root, self.gpu)
        with self.assertRaisesRegex(ValueError, "manifest changed"):
            installer.restore_cpu(self.root)
        self.assertEqual(path.read_bytes(), changed)
        self.assertEqual((self.cpu / "installation-cpu.json").read_bytes(), self.original)

    def test_changed_cpu_backup_is_not_adopted(self):
        installer.activate_gpu(self.root, self.gpu)
        backup = self.cpu / "installation-cpu.json"
        backup.write_bytes(self.original + b" ")
        with self.assertRaisesRegex(ValueError, "CPU backup changed"):
            installer.activate_gpu(self.root, self.gpu)
        self.assertEqual(backup.read_bytes(), self.original + b" ")

    def test_changed_model_stops_before_copy_and_activation(self):
        name = self.gpu_manifest["models"][0]["file"]
        (self.gpu / "models" / name).write_bytes(b"changed model")
        with self.assertRaisesRegex(ValueError, "model changed"):
            installer.activate_gpu(self.root, self.gpu)
        self.assertEqual((self.cpu / "installation.json").read_bytes(), self.original)
        self.assertFalse((self.cpu / "venv_cuda").exists())

    def test_existing_unrecognized_cuda_runtime_is_preserved(self):
        destination = self.cpu / "venv_cuda"
        destination.mkdir()
        sentinel = destination / "user-note.txt"
        sentinel.write_bytes(b"preserve this file")
        with self.assertRaisesRegex(ValueError, "runtime differs"):
            installer.activate_gpu(self.root, self.gpu)
        self.assertEqual(sentinel.read_bytes(), b"preserve this file")
        self.assertEqual((self.cpu / "installation.json").read_bytes(), self.original)

    def test_missing_pinned_gpu_dependency_stops_activation(self):
        self.gpu_manifest["gpu_packages"].pop("nvidia-cuda-nvrtc-cu12")
        (self.gpu / "installation.json").write_text(json.dumps(self.gpu_manifest), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "pinned CUDA profile"):
            installer.activate_gpu(self.root, self.gpu)
        self.assertEqual((self.cpu / "installation.json").read_bytes(), self.original)
        self.assertFalse((self.cpu / "installation-cpu.json").exists())

    def test_repeat_activation_reuses_verified_copy_and_appends_history(self):
        first = installer.activate_gpu(self.root, self.gpu)
        second = installer.activate_gpu(self.root, self.gpu)
        self.assertEqual(first["runtime_tree_sha256"], second["runtime_tree_sha256"])
        self.assertEqual(first["active_manifest_sha256"], second["active_manifest_sha256"])
        self.assertEqual(len(second["events"]), 2)

    def test_runtime_evidence_excludes_mutable_cache_and_keeps_code_and_dll(self):
        cache = self.gpu / "venv/__pycache__"
        cache.mkdir()
        (cache / "cached.pyc").write_bytes(b"mutable cache")
        (self.gpu / "venv/runtime.log").write_bytes(b"mutable log")
        (self.gpu / "venv/package.py").write_bytes(b"# immutable package code\n")
        installer.activate_gpu(self.root, self.gpu)
        active = json.loads((self.cpu / "installation.json").read_text(encoding="utf-8"))
        names = {item["filename"] for item in active["runtime_files"]}
        self.assertIn("venv_cuda/package.py", names)
        self.assertIn("venv_cuda/library.dll", names)
        self.assertFalse(any(name.endswith((".pyc", ".log")) for name in names))
        (self.cpu / "venv_cuda/__pycache__/cached.pyc").write_bytes(b"updated cache")
        installer.activate_gpu(self.root, self.gpu)

    def test_receipt_write_failure_can_restore_original_cpu_from_proven_identity(self):
        atomic = installer.server_installer.runtime_installer.atomic_json

        def fail_receipt(path, data):
            if path.name == "cuda-activation.json":
                raise OSError("synthetic receipt write failure")
            return atomic(path, data)

        with mock.patch.object(installer.server_installer.runtime_installer, "atomic_json", side_effect=fail_receipt):
            with self.assertRaisesRegex(OSError, "receipt write failure"):
                installer.activate_gpu(self.root, self.gpu)
        self.assertFalse((self.cpu / "cuda-activation.json").exists())
        self.assertEqual(json.loads((self.cpu / "installation.json").read_text())["execution_provider"], "cuda")
        receipt = installer.restore_cpu(self.root)
        self.assertEqual((self.cpu / "installation.json").read_bytes(), self.original)
        self.assertEqual([event["action"] for event in receipt["events"]], ["recover_activation_receipt", "restore_cpu"])

    def test_interrupted_activation_with_changed_dll_cannot_forge_restore_identity(self):
        installer.activate_gpu(self.root, self.gpu)
        (self.cpu / "cuda-activation.json").unlink()
        (self.cpu / "venv_cuda/library.dll").write_bytes(b"changed runtime")
        before = (self.cpu / "installation.json").read_bytes()
        with self.assertRaisesRegex(ValueError, "runtime files changed"):
            installer.restore_cpu(self.root)
        self.assertEqual((self.cpu / "installation.json").read_bytes(), before)
        self.assertEqual((self.cpu / "installation-cpu.json").read_bytes(), self.original)


if __name__ == "__main__":
    unittest.main()
