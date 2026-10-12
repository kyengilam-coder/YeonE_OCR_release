"""Write the public updater manifest for the exact executable being released."""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import re


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parent.parent
    version = re.search(r'__version__\s*=\s*"([0-9]+\.[0-9]+\.[0-9]+)"',
                        (root / "yeone_ocr/__init__.py").read_text(encoding="utf-8")).group(1)
    asset = args.directory / "YeonE_OCR-Windows-x64.exe"
    digest = hashlib.sha256()
    with asset.open("rb") as f:
        if f.read(2) != b"MZ":
            raise ValueError("The release asset is not a Windows executable")
        f.seek(0)
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    manifest = {"schema_version": 1, "version": version, "asset_name": asset.name,
                "sha256": digest.hexdigest(), "size": asset.stat().st_size}
    (args.directory / "update.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (args.directory / "SHA256SUMS.txt").write_text(f"{digest.hexdigest()}  {asset.name}\n", encoding="utf-8")
    print(json.dumps(manifest))


if __name__ == "__main__":
    main()
