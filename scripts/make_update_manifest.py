"""Backward-compatible public release command: make_update_manifest.py DIRECTORY."""
from pathlib import Path
import argparse
import json
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.prepare_windows_release import make_update_manifest, read_version, write_checksums


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    version = read_version((ROOT / "yeone_ocr" / "__init__.py").read_text(encoding="utf-8"))
    manifest = make_update_manifest(args.directory, version)
    names = [manifest["asset_name"], "update.json", f"YeonE_OCR-{version}-Setup-x64.exe",
             f"YeonE_OCR-{version}-source.zip", f"YeonE_OCR-{version}-licenses.zip"]
    write_checksums(args.directory, [args.directory / name for name in names if (args.directory / name).is_file()])
    print(json.dumps(manifest))


if __name__ == "__main__":
    main()
