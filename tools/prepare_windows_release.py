"""Prepare release archives, legacy updater metadata and complete SHA256 sums.

Reads committed source from Git HEAD; never publishes working copies, API keys,
configuration, OCR jobs or documents. No network or GitHub writes are performed.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import re
import stat
import struct
import subprocess
from zipfile import ZipFile, ZIP_DEFLATED


ROOT = Path(__file__).resolve().parents[1]
PORTABLE_NAME = "YeonE_OCR-Windows-x64.exe"
MAX_PORTABLE_BYTES = 512 * 1024 * 1024
VERSION_RE = re.compile(r'(?m)^__version__\s*=\s*[\'"]([0-9]+\.[0-9]+\.[0-9]+)[\'"]')
SOURCE_TOP_FILES = {"README.md", "LICENSE", "LICENSE.txt", "DISTRIBUTION_NOTICE.txt", "pyproject.toml", ".gitignore",
                    ".gitattributes", "requirements.txt", "requirements-build.txt", "requirements-windows.txt",
                    "YeonE_OCR.pyw", "YeonE_OCR.bat", "YeonE_OCR.spec", "windows_entry.py"}
SOURCE_TOP_DIRS = {"yeone_ocr", "tests", "docs", "tools", "scripts", "packaging", "resources", ".github"}
SENSITIVE_NAMES = {"config.json", "credentials.json", "token.json", "secrets.json", "id_rsa", "id_ed25519"}
SENSITIVE_SUFFIXES = {".pem", ".key", ".p12", ".pfx", ".pdf", ".hwpx", ".hwp", ".docx", ".doc", ".msr"}


def read_version(text: str) -> str:
    match = VERSION_RE.search(text)
    if not match:
        raise ValueError("Cannot read a three-component __version__.")
    return match.group(1)


def source_allowed(name: str) -> bool:
    path = PurePosixPath(name)
    if path.is_absolute() or not path.parts or any(p in {"..", ".git", "__pycache__", "work", ".venv", "venv"} for p in path.parts):
        return False
    leaf = path.name.lower()
    if (leaf in SENSITIVE_NAMES or leaf.startswith((".env", "client_secret", "credentials", "private_key"))
            or path.suffix.lower() in SENSITIVE_SUFFIXES):
        return False
    if len(path.parts) == 1:
        return name in SOURCE_TOP_FILES
    if path.parts[0] not in SOURCE_TOP_DIRS:
        return False
    if path.parts[0] == "yeone_ocr":
        return (path.suffix == ".py" or name == "yeone_ocr/지침.md"
                or name == "yeone_ocr/data/normalization_260929.json"
                or (path.parts[1] == "assets" and path.suffix.lower() in {".ico", ".png", ".svg"}))
    if path.parts[0] in {"tools", "scripts", "tests"}:
        return path.suffix.lower() in {".py", ".ps1"}
    if path.parts[0] == "docs":
        return path.suffix.lower() in {".md", ".txt"}
    if path.parts[0] == ".github":
        return len(path.parts) == 3 and path.parts[1] == "workflows" and path.suffix.lower() in {".yml", ".yaml"}
    # Packaging and license resources contain upstream notices without standard
    # extensions. Runtime/user configuration names and documents were excluded above.
    return path.suffix.lower() not in {".exe", ".dll", ".pyd", ".zip", ".7z", ".pyc"}


def _git(repo: Path, *arguments: str) -> bytes:
    result = subprocess.run(["git", *arguments], cwd=repo, capture_output=True, check=False)
    if result.returncode:
        raise RuntimeError(result.stderr.decode("utf-8", errors="replace").strip() or "Git archive command failed.")
    return result.stdout


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def verify_executable(path: Path, *, portable: bool = False) -> None:
    if not path.is_file() or path.is_symlink():
        raise ValueError(f"Missing executable or symbolic link: {path.name}")
    with path.open("rb") as stream:
        header = stream.read(64)
        if len(header) < 64 or header[:2] != b"MZ":
            raise ValueError(f"Not a Windows executable: {path.name}")
        offset = struct.unpack_from("<I", header, 60)[0]
        if offset > min(path.stat().st_size - 6, 1024 * 1024):
            raise ValueError(f"Invalid Windows executable header: {path.name}")
        stream.seek(offset)
        signature = stream.read(6)
        if len(signature) != 6 or signature[:4] != b"PE\0\0":
            raise ValueError(f"Invalid PE signature: {path.name}")
        if portable and struct.unpack_from("<H", signature, 4)[0] != 0x8664:
            raise ValueError("The portable updater asset must be Windows x64.")
    if portable and not 0 < path.stat().st_size <= MAX_PORTABLE_BYTES:
        raise ValueError("Portable asset exceeds the updater's 512 MiB limit.")


def make_update_manifest(directory: Path, version: str) -> dict:
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
        raise ValueError("Invalid release version.")
    asset = Path(directory) / PORTABLE_NAME
    verify_executable(asset, portable=True)
    # Preserve the exact schema consumed by the public 0.7.0 updater.
    manifest = {"schema_version": 1, "version": version, "asset_name": PORTABLE_NAME,
                "sha256": sha256(asset), "size": asset.stat().st_size}
    (directory / "update.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest


def create_source_archive(repo: Path, output: Path, version: str, revision: str = "HEAD") -> dict:
    commit = _git(repo, "rev-parse", "--verify", "--end-of-options", f"{revision}^{{commit}}").decode().strip()
    committed_version = read_version(_git(repo, "show", f"{commit}:yeone_ocr/__init__.py").decode("utf-8"))
    if committed_version != version:
        raise ValueError(f"Committed source version {committed_version} differs from release {version}; commit release source first.")
    prefix = f"YeonE_OCR-{version}/"
    # Filter the in-memory Git archive instead of archiving the working tree.
    # No intermediate unfiltered archive is written to disk.
    archive = _git(repo, "archive", "--format=zip", f"--prefix={prefix}", commit)
    copied = []
    with ZipFile(io.BytesIO(archive)) as original, ZipFile(output, "w", compression=ZIP_DEFLATED) as target:
        for entry in original.infolist():
            if entry.is_dir() or not entry.filename.startswith(prefix):
                continue
            name = entry.filename[len(prefix):]
            if not source_allowed(name) or stat.S_ISLNK(entry.external_attr >> 16):
                continue
            target.writestr(entry, original.read(entry))
            copied.append(name)
        target.writestr(prefix + "SOURCE_REVISION.txt", f"Version: {version}\nGit commit: {commit}\nSource: committed Git tree only.\n")
    if "yeone_ocr/__init__.py" not in copied:
        raise ValueError("Source archive is missing the application package.")
    return {"commit": commit, "files": len(copied)}


def create_licenses_archive(repo: Path, output: Path) -> int:
    licenses = repo / "packaging" / "third_party_licenses"
    if not licenses.is_dir():
        raise ValueError("packaging/third_party_licenses is missing.")
    sources = [(path, f"THIRD_PARTY_LICENSES/{path.relative_to(licenses).as_posix()}")
               for path in sorted(licenses.rglob("*")) if path.is_file()]
    sources += [(repo / "packaging" / "THIRD_PARTY_NOTICES.txt", "THIRD_PARTY_NOTICES.txt"),
                (repo / "DISTRIBUTION_NOTICE.txt", "DISTRIBUTION_NOTICE.txt"),
                (repo / "packaging" / "requirements-windows.lock.txt", "requirements-windows.lock.txt")]
    if (repo / "LICENSE").is_file():
        sources.append((repo / "LICENSE", "YEONE-OCR-LICENSE.txt"))
    with ZipFile(output, "w", compression=ZIP_DEFLATED) as archive:
        for path, name in sources:
            if not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(repo.resolve()):
                raise ValueError(f"Missing or unsafe license resource: {name}")
            archive.write(path, name)
    return len(sources)


def write_checksums(directory: Path, artifacts: list[Path]) -> None:
    directory = directory.resolve()
    for path in artifacts:
        if path.parent.resolve() != directory or not path.is_file() or path.is_symlink():
            raise ValueError(f"Unsafe release artifact: {path.name}")
    lines = [f"{sha256(path)}  {path.name}\n" for path in sorted(artifacts, key=lambda item: item.name)]
    (directory / "SHA256SUMS.txt").write_text("".join(lines), encoding="utf-8")


def prepare(directory: Path, repo: Path = ROOT, revision: str = "HEAD") -> dict:
    directory = directory.resolve(); repo = repo.resolve()
    version = read_version((repo / "yeone_ocr" / "__init__.py").read_text(encoding="utf-8"))
    portable = directory / PORTABLE_NAME
    installer = directory / f"YeonE_OCR-{version}-Setup-x64.exe"
    verify_executable(installer)
    manifest = make_update_manifest(directory, version)
    source_zip = directory / f"YeonE_OCR-{version}-source.zip"
    source = create_source_archive(repo, source_zip, version, revision)
    license_zip = directory / f"YeonE_OCR-{version}-licenses.zip"
    license_count = create_licenses_archive(repo, license_zip)
    artifacts = [portable, installer, directory / "update.json", source_zip, license_zip]
    write_checksums(directory, artifacts)
    return {"version": version, "source": source, "license_files": license_count, "update": manifest,
            "artifacts": [path.name for path in artifacts] + ["SHA256SUMS.txt"]}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path, help="Contains the finished portable and installer executables")
    parser.add_argument("--repo", type=Path, default=ROOT)
    parser.add_argument("--revision", default="HEAD", help="Committed source reference (default HEAD)")
    args = parser.parse_args(argv)
    print(json.dumps(prepare(args.directory, args.repo, args.revision), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
