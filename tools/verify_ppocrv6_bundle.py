"""Fail a build if the offline engine payload is incomplete or modified."""
import hashlib
import json
from pathlib import Path, PurePosixPath
import sys


def verify(root):
    root = Path(root).resolve()
    manifest = json.loads((root / "bundle-manifest.json").read_text(encoding="utf-8"))
    if manifest.get("engine") != "ppocrv6" or not manifest.get("files"):
        raise ValueError("Invalid PP-OCRv6 bundle manifest")
    expected = manifest["files"]
    actual = {p.relative_to(root).as_posix() for p in root.rglob("*")
              if p.is_file() and p.name != "bundle-manifest.json" and "__pycache__" not in p.parts}
    if set(expected) != actual:
        raise ValueError("Bundle file inventory mismatch")
    for name, record in expected.items():
        rel = PurePosixPath(name)
        if rel.is_absolute() or ".." in rel.parts or "\\" in name or ":" in name:
            raise ValueError("Unsafe bundle path")
        path = root / rel
        path.resolve().relative_to(root)
        if path.is_symlink() or path.stat().st_size != record["bytes"]:
            raise ValueError(f"Bundle file mismatch: {name}")
        with path.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != record["sha256"]:
                raise ValueError(f"Bundle hash mismatch: {name}")
    for name in ("runtime/python.exe", "runtime/python313.dll", "runtime/python313._pth", "installation.json",
                 "runtime/msvcp140.dll", "runtime/msvcp140_1.dll"):
        if name not in expected:
            raise ValueError(f"Missing runtime file: {name}")
    installation = json.loads((root / "installation.json").read_text(encoding="utf-8"))
    if installation.get("runtime_layout") != "standalone-python":
        raise ValueError("Relocatable runtime required")
    print(json.dumps({"ok": True, "files": len(expected)}))


if __name__ == "__main__":
    verify(sys.argv[1])
