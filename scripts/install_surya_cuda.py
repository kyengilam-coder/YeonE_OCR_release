"""Add the official CUDA 12.4 llama.cpp backend to an existing Surya install.

This downloads public archives only. It does not run OCR or read documents.
Use --activate only when selecting this installed CUDA backend for inference.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import urllib.request

from install_surya import ROOT, LOCK, download, extract, sha256


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", type=Path, default=ROOT / "tools" / "surya")
    parser.add_argument("--activate", action="store_true")
    args = parser.parse_args()
    target = args.target.resolve()
    manifest_path = target / "installation.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    lock = json.loads(LOCK.read_text(encoding="utf-8"))
    if manifest["engine"] != "surya" or manifest["engine_version"] != lock["surya_version"]:
        raise ValueError("Install the locked Surya base environment first")
    url = "https://api.github.com/repos/ggml-org/llama.cpp/releases/tags/" + lock["llama_cpp_release"]
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "YeonE-OCR-local-installer"}),
                                timeout=60) as source:
        release = json.load(source)
    records = []
    runtime_root = target / "llama.cpp-cuda"
    for record in lock["llama_cpp_cuda_assets"]:
        asset = next(item for item in release["assets"] if item["name"] == record["file"])
        if asset.get("digest") != "sha256:" + record["sha256"]:
            raise ValueError("Official CUDA asset digest differs from lock")
        archive = target / record["file"]
        records.append(download(asset["browser_download_url"], archive, record["sha256"]))
        extract(archive, runtime_root)
    cuda_manifest = dict(manifest, backend="llama.cpp CUDA 12.4 Windows x64",
                         backend_device="cuda", runtime_directory="llama.cpp-cuda",
                         runtime_files=[{"file": path.relative_to(target).as_posix(),
                                         "sha256": sha256(path), "size": path.stat().st_size}
                                        for path in sorted(runtime_root.rglob("*")) if path.is_file()],
                         cuda_public_downloads=records)
    (target / "installation-cuda.json").write_text(json.dumps(cuda_manifest, ensure_ascii=False, indent=2),
                                                  encoding="utf-8")
    if args.activate:
        if manifest.get("backend_device", "cpu") == "cpu" and not (target / "installation-cpu.json").exists():
            (target / "installation-cpu.json").write_bytes(manifest_path.read_bytes())
        temporary = target / "installation.json.tmp"
        temporary.write_text(json.dumps(cuda_manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, manifest_path)
    print("Surya official CUDA backend installed; " + ("activated." if args.activate else "not activated."))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
