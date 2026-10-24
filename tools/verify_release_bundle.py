"""Seal and verify the exact release bytes handed from build to publication.

The receipt hash is passed separately as a CI job output. Verification is
read-only and uses only the standard library, so publication needs no build
environment or second build.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.prepare_windows_release import PORTABLE_NAME, read_version, sha256

RECEIPT = "release-bundle.json"


def artifact_names(version):
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
        raise ValueError("Invalid release version.")
    return {PORTABLE_NAME, f"YeonE_OCR-{version}-Setup-x64.exe", "update.json",
            "SHA256SUMS.txt", f"YeonE_OCR-{version}-source.zip", f"YeonE_OCR-{version}-licenses.zip"}


def _snapshot(directory, version, commit, *, sealed):
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("Expected source commit must be a full Git SHA.")
    names = artifact_names(version)
    entries = list(directory.iterdir())
    expected = names | ({RECEIPT} if sealed else set())
    if {entry.name for entry in entries} != expected:
        raise ValueError("Release bundle must contain the exact artifact inventory.")
    if any(entry.is_symlink() or not entry.is_file() for entry in entries):
        raise ValueError("Release bundle may contain only regular files.")
    artifacts = {name: {"sha256": sha256(directory / name), "size": (directory / name).stat().st_size}
                 for name in sorted(names)}
    manifest = json.loads((directory / "update.json").read_text(encoding="utf-8"))
    wanted = {"schema_version": 1, "version": version, "asset_name": PORTABLE_NAME,
              **artifacts[PORTABLE_NAME]}
    if manifest != wanted:
        raise ValueError("Update manifest does not match the exact portable bytes and version.")
    checksums = {}
    for line in (directory / "SHA256SUMS.txt").read_text(encoding="utf-8").splitlines():
        match = re.fullmatch(r"([0-9a-f]{64})  ([^/\\]+)", line)
        if match is None or match[2] in checksums:
            raise ValueError("Invalid or duplicate release checksum entry.")
        checksums[match[2]] = match[1]
    if checksums != {name: item["sha256"] for name, item in artifacts.items() if name != "SHA256SUMS.txt"}:
        raise ValueError("Release checksum inventory or bytes differ.")
    source_name = f"YeonE_OCR-{version}-source.zip"
    marker = f"YeonE_OCR-{version}/SOURCE_REVISION.txt"
    with ZipFile(directory / source_name) as archive:
        if archive.namelist().count(marker) != 1:
            raise ValueError("Source archive must have one revision marker.")
        revision = archive.read(marker).decode("utf-8")
    wanted_revision = f"Version: {version}\nGit commit: {commit}\nSource: committed Git tree only.\n"
    if revision != wanted_revision:
        raise ValueError("Source archive version or commit does not match this workflow.")
    return {"schema_version": 1, "version": version, "commit": commit, "artifacts": artifacts}


def seal(directory: Path, version: str, commit: str):
    payload = _snapshot(directory, version, commit, sealed=False)
    receipt = directory / RECEIPT
    receipt.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"version": version, "commit": commit, "receipt_sha256": sha256(receipt)}


def verify(directory: Path, version: str, commit: str, receipt_sha256: str):
    receipt = directory / RECEIPT
    if (not re.fullmatch(r"[0-9a-f]{64}", receipt_sha256)
            or not receipt.is_file() or receipt.is_symlink()
            or sha256(receipt) != receipt_sha256):
        raise ValueError("Release receipt differs from the build job output.")
    payload = json.loads(receipt.read_text(encoding="utf-8"))
    if payload != _snapshot(directory, version, commit, sealed=True):
        raise ValueError("Release artifact bytes differ from the verified build.")
    return {"ok": True, "version": version, "commit": commit, "artifacts": sorted(payload["artifacts"])}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("seal", "verify"))
    parser.add_argument("directory", type=Path)
    parser.add_argument("--commit", required=True, help="The workflow's full github.sha.")
    parser.add_argument("--receipt-sha256", help="Required for verify; trusted build job output.")
    args = parser.parse_args()
    version = read_version((ROOT / "yeone_ocr/__init__.py").read_text(encoding="utf-8"))
    if args.action == "seal":
        if args.receipt_sha256:
            parser.error("--receipt-sha256 applies only to verify")
        result = seal(args.directory, version, args.commit)
    else:
        if not args.receipt_sha256:
            parser.error("verify requires --receipt-sha256")
        result = verify(args.directory, version, args.commit, args.receipt_sha256)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
