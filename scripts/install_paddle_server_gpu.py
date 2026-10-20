"""Install a separate, optional CUDA backend for the same local server weights.

Public NVIDIA runtime wheels are pinned and isolated. No system driver, CUDA
installation, existing CPU environment, document, user config, or key is changed.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import venv

try:
    from . import install_paddle_server as server_installer
except ImportError:
    import install_paddle_server as server_installer


def _bytes_sha(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _exclusive_bytes(path: Path, content: bytes) -> None:
    """Keep the exact original CPU manifest bytes without replacing a backup."""
    with tempfile.NamedTemporaryFile("wb", dir=path.parent, suffix=".tmp", delete=False) as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
        temporary = Path(stream.name)
    try:
        if os.name == "nt":
            os.rename(temporary, path)
        else:
            os.link(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _model_hashes(home: Path, manifest: dict) -> dict[str, str]:
    expected_names = set(json.loads(Path(__file__).with_name("paddle-server-lock.json").read_text(encoding="utf-8"))["models"])
    records = manifest.get("models", [])
    if {item.get("file") for item in records} != expected_names or len(records) != len(expected_names):
        raise ValueError("Exactly the three recorded server models are required")
    result = {}
    for record in records:
        model = home / "models" / record["file"]
        actual = server_installer.runtime_installer.sha256(model)
        if actual != record["sha256"]:
            raise ValueError("Server model changed before activation")
        result[record["file"]] = actual
    return result


def _runtime_inventory(directory: Path) -> dict[str, str]:
    directory = directory.resolve(strict=True)
    inventory = {}
    for path in sorted(directory.rglob("*")):
        if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
            raise ValueError("Runtime links are not copied during activation")
        if not path.resolve().is_relative_to(directory):
            raise ValueError("Runtime file resolves outside its installation")
        if path.is_file():
            relative = path.relative_to(directory)
            if ("__pycache__" in relative.parts or path.suffix.lower() in {".pyc", ".pyo", ".log"}
                    or path.name.startswith(("pip-install-report", "pip-gpu-install-report", "pip-report"))):
                continue
            inventory[relative.as_posix()] = server_installer.runtime_installer.sha256(path)
    return inventory


def _tree_sha(inventory: dict[str, str]) -> str:
    return _bytes_sha(json.dumps(inventory, sort_keys=True, separators=(",", ":")).encode("utf-8"))


def _recover_activation_identity(canonical: Path, current: bytes, cpu_bytes: bytes) -> dict:
    """Recover the interrupted first receipt only from complete activation proof."""
    active = json.loads(current)
    identity = active.get("activation", {})
    if (active.get("engine") != "paddle_server" or active.get("execution_provider") != "cuda"
            or active.get("runtime_root") != "." or identity.get("cpu_manifest_sha256") != _bytes_sha(cpu_bytes)):
        raise ValueError("Interrupted activation lacks a verified original CPU identity")
    expected = {}
    for record in active.get("runtime_files", []):
        filename = record.get("filename", "")
        relative = Path(filename)
        if (not isinstance(filename, str) or not filename.startswith("venv_cuda/") or relative.is_absolute() or ".." in relative.parts
                or not isinstance(record.get("sha256"), str)):
            raise ValueError("Interrupted activation runtime evidence is invalid")
        key = relative.relative_to("venv_cuda").as_posix()
        if key in expected:
            raise ValueError("Interrupted activation runtime evidence has duplicate files")
        expected[key] = record["sha256"]
    if not expected or _runtime_inventory(canonical / "venv_cuda") != expected or _tree_sha(expected) != identity.get("runtime_tree_sha256"):
        raise ValueError("Interrupted activation runtime files changed")
    if _model_hashes(canonical, active) != _model_hashes(canonical, json.loads(cpu_bytes)):
        raise ValueError("Interrupted activation server model identity changed")
    return {"status": "recovered_activation_receipt", "cpu_manifest_sha256": _bytes_sha(cpu_bytes),
            "active_manifest_sha256": _bytes_sha(current), "runtime_tree_sha256": _tree_sha(expected),
            "runtime_files": len(expected),
            "events": [{"action": "recover_activation_receipt", "utc": datetime.now(timezone.utc).isoformat()}]}


def activate_gpu(engine_root: Path, gpu_home: Path) -> dict:
    """Copy the verified CUDA runtime, preserve CPU bytes, then switch atomically."""
    canonical = (engine_root.resolve() / "tools/paddle_server").resolve(strict=True)
    gpu_home = gpu_home.resolve(strict=True)
    manifest_path = canonical / "installation.json"
    original_bytes = manifest_path.read_bytes()
    original = json.loads(original_bytes)
    if original.get("engine") != "paddle_server" or original.get("default_profile") != "server":
        raise ValueError("Existing canonical server installation must be verified before activation")
    source_bytes = (gpu_home / "installation.json").read_bytes()
    source = json.loads(source_bytes)
    pins = json.loads(Path(__file__).with_name("paddle-server-gpu-lock.json").read_text(encoding="utf-8"))["packages"]
    if (source.get("engine") != "paddle_server" or source.get("execution_provider") != "cuda"
            or source.get("onnxruntime_package") != "onnxruntime-gpu" or source.get("gpu_packages") != pins):
        raise ValueError("GPU installation differs from the pinned CUDA profile")
    if _model_hashes(canonical, original) != _model_hashes(gpu_home, source):
        raise ValueError("Canonical CPU and GPU server model hashes must be identical")
    backup = canonical / "installation-cpu.json"
    receipt_path = canonical / "cuda-activation.json"
    previous = json.loads(receipt_path.read_text(encoding="utf-8")) if receipt_path.exists() else None
    if backup.exists():
        cpu_bytes = backup.read_bytes()
        if previous and _bytes_sha(cpu_bytes) != previous["cpu_manifest_sha256"]:
            raise ValueError("Original CPU backup changed; activation will not overwrite it")
        if original.get("execution_provider") == "cuda":
            if not previous or _bytes_sha(original_bytes) != previous["active_manifest_sha256"]:
                raise ValueError("Canonical CUDA manifest changed; it will not be overwritten")
        elif original_bytes != cpu_bytes:
            raise ValueError("Canonical CPU manifest differs from its original backup")
    else:
        if original.get("execution_provider") not in (None, "cpu"):
            raise ValueError("Original canonical CPU manifest is missing")
        cpu_bytes = original_bytes
        _exclusive_bytes(backup, cpu_bytes)
    if json.loads(cpu_bytes).get("execution_provider") not in (None, "cpu"):
        raise ValueError("CPU backup is not a CPU installation")
    cpu_python = canonical / ("venv/Scripts/python.exe" if os.name == "nt" else "venv/bin/python")
    if not cpu_python.is_file():
        raise ValueError("Original canonical CPU Python is missing")
    source_runtime = gpu_home / "venv"
    inventory = _runtime_inventory(source_runtime)
    relative_python = "venv_cuda/Scripts/python.exe" if os.name == "nt" else "venv_cuda/bin/python"
    source_python = "Scripts/python.exe" if os.name == "nt" else "bin/python"
    if source_python not in inventory:
        raise ValueError("Isolated GPU Python executable is missing")
    destination = canonical / "venv_cuda"
    if destination.exists():
        if _runtime_inventory(destination) != inventory:
            raise ValueError("Existing canonical CUDA runtime differs; it will not be overwritten")
    else:
        staging = Path(tempfile.mkdtemp(prefix="venv_cuda.installing-", dir=canonical)).resolve()
        if not staging.is_relative_to(canonical) or not staging.name.startswith("venv_cuda.installing-"):
            raise ValueError("CUDA staging directory resolves outside the canonical engine home")
        try:
            shutil.copytree(source_runtime, staging, dirs_exist_ok=True)
            if _runtime_inventory(staging) != inventory or _runtime_inventory(source_runtime) != inventory:
                raise ValueError("GPU runtime changed during its verified copy")
            staging.rename(destination)
        finally:
            if staging.exists():
                verified = staging.resolve()
                if not verified.is_relative_to(canonical) or not verified.name.startswith("venv_cuda.installing-"):
                    raise ValueError("Refusing cleanup outside the verified CUDA staging directory")
                shutil.rmtree(verified)
    provenance = source["package_provenance"]
    if not isinstance(provenance, str) or Path(provenance).name != provenance or "/" in provenance or "\\" in provenance:
        raise ValueError("GPU provenance must name a file in its installation")
    report = gpu_home / provenance
    destination_report = canonical / "pip-gpu-install-report.json"
    report_bytes = report.read_bytes()
    if destination_report.exists():
        if destination_report.read_bytes() != report_bytes:
            raise ValueError("Existing canonical GPU provenance differs; it will not be overwritten")
    else:
        _exclusive_bytes(destination_report, report_bytes)
    tree_digest = _tree_sha(inventory)
    active = dict(source, runtime_python=str(canonical / relative_python), python_executable=relative_python,
                  package_provenance=destination_report.name,
                  runtime_root=".", runtime_files=[{"filename": "venv_cuda/" + relative, "sha256": digest}
                                                   for relative, digest in inventory.items()],
                  activation={"cpu_manifest_sha256": _bytes_sha(cpu_bytes), "source_gpu_manifest_sha256": _bytes_sha(source_bytes),
                              "runtime_tree_sha256": tree_digest, "runtime_files": len(inventory),
                              "restore_command": "install_paddle_server_gpu.py --restore-cpu --root <engine-home>"})
    if manifest_path.read_bytes() != original_bytes:
        raise ValueError("Canonical installation changed during activation; no manifest was replaced")
    server_installer.runtime_installer.atomic_json(manifest_path, active)
    result = {"status": "cuda_active", "cpu_manifest_sha256": _bytes_sha(cpu_bytes),
              "active_manifest_sha256": _bytes_sha(manifest_path.read_bytes()),
              "source_gpu_manifest_sha256": _bytes_sha(source_bytes), "runtime_tree_sha256": tree_digest,
              "runtime_files": len(inventory), "models": _model_hashes(canonical, active),
              "events": (previous or {}).get("events", []) + [{"action": "activate_cuda", "utc": datetime.now(timezone.utc).isoformat()}]}
    server_installer.runtime_installer.atomic_json(receipt_path, result)
    return result


def restore_cpu(engine_root: Path) -> dict:
    canonical = (engine_root.resolve() / "tools/paddle_server").resolve(strict=True)
    manifest_path, backup = canonical / "installation.json", canonical / "installation-cpu.json"
    receipt_path = canonical / "cuda-activation.json"
    current = manifest_path.read_bytes()
    cpu_bytes = backup.read_bytes()
    receipt = (json.loads(receipt_path.read_text(encoding="utf-8")) if receipt_path.exists() else
               _recover_activation_identity(canonical, current, cpu_bytes))
    if _bytes_sha(current) != receipt["active_manifest_sha256"] or _bytes_sha(cpu_bytes) != receipt["cpu_manifest_sha256"]:
        raise ValueError("Current or original CPU manifest changed; restore will not overwrite it")
    _model_hashes(canonical, json.loads(cpu_bytes))
    # The original byte stream, including its formatting, is restored atomically.
    with tempfile.NamedTemporaryFile("wb", dir=canonical, suffix=".tmp", delete=False) as stream:
        stream.write(cpu_bytes)
        stream.flush()
        os.fsync(stream.fileno())
        temporary = Path(stream.name)
    try:
        if manifest_path.read_bytes() != current:
            raise ValueError("Canonical installation changed during CPU restore")
        temporary.replace(manifest_path)
    finally:
        temporary.unlink(missing_ok=True)
    receipt.update(status="cpu_restored", events=receipt["events"] + [{"action": "restore_cpu", "utc": datetime.now(timezone.utc).isoformat()}])
    server_installer.runtime_installer.atomic_json(receipt_path, receipt)
    return receipt


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    location = parser.add_mutually_exclusive_group()
    location.add_argument("--root", type=Path)
    location.add_argument("--target", type=Path)
    parser.add_argument("--model-cache", type=Path)
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--activate", action="store_true", help="Copy CUDA runtime into canonical server home, preserving the CPU installation")
    action.add_argument("--restore-cpu", action="store_true", help="Restore exact original CPU manifest without downloads")
    args = parser.parse_args(argv)
    engine_root = (args.root or Path(__file__).resolve().parents[1]).expanduser().resolve()
    if args.restore_cpu:
        result = restore_cpu(engine_root)
        print(json.dumps({"status": result["status"], "cpu_manifest_sha256": result["cpu_manifest_sha256"]}))
        return 0
    target = (args.target if args.target else
              (args.root or Path(__file__).resolve().parents[1]) / "tools/paddle_server_gpu").expanduser().resolve()
    if target.name.lower() in ("rapidocr", "easyocr", "tesseract", "paddle_server"):
        raise ValueError("Use a separate paddle_server_gpu directory; existing CPU engines are preserved")
    target.mkdir(parents=True, exist_ok=True)
    runtime = target / "venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not runtime.exists():
        venv.EnvBuilder(with_pip=True).create(target / "venv")
    locks = Path(__file__).parent
    base = json.loads((locks / "rapidocr-lock.json").read_text(encoding="utf-8"))
    gpu = json.loads((locks / "paddle-server-gpu-lock.json").read_text(encoding="utf-8"))
    pins = {key: value for key, value in base["verified_runtime"]["packages"].items() if key != "onnxruntime"}
    pins.update(gpu["packages"])
    constraints = target / "dependency-pins.txt"
    constraints.write_text("\n".join(f"{name}=={version}" for name, version in pins.items()) + "\n", encoding="utf-8")
    report_path = target / "pip-gpu-install-report.json"
    next_report = target / "pip-gpu-install-report.next.json"
    subprocess.run([str(runtime), "-m", "pip", "install", "--disable-pip-version-check", "--no-cache-dir",
                    "--constraint", str(constraints), "--report", str(next_report),
                    f"rapidocr=={base['rapidocr_version']}",
                    *[f"{name}=={version}" for name, version in gpu["packages"].items()]], check=True)
    previous = json.loads(report_path.read_text(encoding="utf-8")) if report_path.exists() else {"install": []}
    current = json.loads(next_report.read_text(encoding="utf-8"))
    by_name = {item["metadata"]["name"].lower().replace("_", "-"): item for item in previous["install"]}
    by_name.update({item["metadata"]["name"].lower().replace("_", "-"): item for item in current["install"]})
    current["install"] = list(by_name.values())
    server_installer.runtime_installer.atomic_json(report_path, current)
    next_report.unlink(missing_ok=True)
    install_args = ["--target", str(target), "--models-only"]
    if args.model_cache:
        install_args.extend(["--model-cache", str(args.model_cache)])
    server_installer.main(install_args)
    manifest_path = target / "installation.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest.update({"onnxruntime_package": "onnxruntime-gpu", "preferred_provider": "cuda", "execution_provider": "cuda",
                     "python_executable": "venv/Scripts/python.exe" if os.name == "nt" else "venv/bin/python",
                     "provider": "CUDAExecutionProvider", "gpu_packages": gpu["packages"],
                     "precision": "float32", "use_tf32": False,
                     "package_provenance": report_path.name,
                     "notes": ["PP-OCRv5 server-size models run locally with the explicitly selected NVIDIA CUDA backend.",
                               *manifest["notes"][1:],
                               "CUDA backend explicitly selected; unavailable CUDA is an error, not a CPU fallback."]})
    server_installer.runtime_installer.atomic_json(manifest_path, manifest)
    if args.activate:
        result = activate_gpu(engine_root, target)
        print(json.dumps({"status": result["status"], "runtime_files": result["runtime_files"],
                          "cpu_manifest_sha256": result["cpu_manifest_sha256"]}))
    print("Optional PaddleOCR server CUDA runtime installed separately.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
