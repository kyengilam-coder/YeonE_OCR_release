"""Release metadata must describe actual binaries and committed, filtered source."""
import hashlib
import io
import json
from pathlib import Path
import struct
import zipfile

import pytest

from tools import prepare_windows_release as release


def pe_bytes(machine=0x8664):
    data = bytearray(128)
    data[:2] = b"MZ"
    struct.pack_into("<I", data, 60, 64)
    data[64:68] = b"PE\0\0"
    struct.pack_into("<H", data, 68, machine)
    return bytes(data)


def committed_tree(monkeypatch, version="0.7.1"):
    prefix = f"YeonE_OCR-{version}/"
    files = {
        "yeone_ocr/__init__.py": f'__version__ = "{version}"\n',
        "yeone_ocr/지침.md": "instructions",
        "yeone_ocr/data/normalization_260929.json": "{}",
        "yeone_ocr/assets/YeonE_OCR.ico": "icon",
        "tools/build_windows.ps1": "script",
        "packaging/third_party_licenses/dep/LICENSE": "license",
        ".github/workflows/windows-release.yml": "on: workflow_dispatch",
        "config.json": "private config",
        "yeone_ocr/config.json": "private config",
        "packaging/.env.production": "private key",
        "resources/private_key.pem": "private key",
        "work/book/readings.json": "private OCR",
        "docs/book.pdf": "private original",
        "tests/result.hwpx": "private result",
    }
    raw = io.BytesIO()
    with zipfile.ZipFile(raw, "w") as archive:
        for name, content in files.items():
            archive.writestr(prefix + name, content)
        symlink = zipfile.ZipInfo(prefix + "yeone_ocr/link.py")
        symlink.external_attr = (0o120777 << 16)
        archive.writestr(symlink, "../../private")

    def git(repo, *args):
        if args[0] == "rev-parse":
            assert args[-1] == "HEAD^{commit}"
            return b"a" * 40 + b"\n"
        if args[0] == "show":
            return files["yeone_ocr/__init__.py"].encode()
        assert args[0] == "archive"
        return raw.getvalue()

    monkeypatch.setattr(release, "_git", git)


def license_tree(repo):
    base = repo / "packaging"
    licenses = base / "third_party_licenses" / "dependency"
    licenses.mkdir(parents=True)
    (licenses / "LICENSE").write_text("upstream license", encoding="utf-8")
    (base / "THIRD_PARTY_NOTICES.txt").write_text("notices", encoding="utf-8")
    (base / "requirements-windows.lock.txt").write_text("dependency==1.0\n", encoding="utf-8")
    (repo / "DISTRIBUTION_NOTICE.txt").write_text("distribution notice", encoding="utf-8")


def test_legacy_manifest_matches_portable_bytes(tmp_path):
    payload = pe_bytes()
    (tmp_path / release.PORTABLE_NAME).write_bytes(payload)
    result = release.make_update_manifest(tmp_path, "0.7.1")
    assert result == {"schema_version": 1, "version": "0.7.1", "asset_name": release.PORTABLE_NAME,
                      "sha256": hashlib.sha256(payload).hexdigest(), "size": len(payload)}
    assert json.loads((tmp_path / "update.json").read_text(encoding="utf-8")) == result


@pytest.mark.parametrize("payload", [b"not an executable", pe_bytes(0x014C)])
def test_updater_asset_rejects_invalid_or_32bit_binary(tmp_path, payload):
    (tmp_path / release.PORTABLE_NAME).write_bytes(payload)
    with pytest.raises(ValueError):
        release.make_update_manifest(tmp_path, "0.7.1")
    assert not (tmp_path / "update.json").exists()


def test_source_archive_uses_committed_tree_and_excludes_private_files(tmp_path, monkeypatch):
    committed_tree(monkeypatch)
    output = tmp_path / "source.zip"
    result = release.create_source_archive(tmp_path, output, "0.7.1")
    assert result == {"commit": "a" * 40, "files": 7}
    with zipfile.ZipFile(output) as archive:
        names = archive.namelist()
        assert len(names) == 8
        assert all("private" not in archive.read(name).decode() for name in names)
        assert not any(name.endswith("link.py") for name in names)
        assert "Git commit: " + "a" * 40 in archive.read("YeonE_OCR-0.7.1/SOURCE_REVISION.txt").decode()


def test_source_archive_refuses_version_mismatch(tmp_path, monkeypatch):
    committed_tree(monkeypatch, version="0.7.0")
    with pytest.raises(ValueError, match="commit release source first"):
        release.create_source_archive(tmp_path, tmp_path / "source.zip", "0.7.1")
    assert not (tmp_path / "source.zip").exists()


def test_prepare_covers_every_published_payload_with_checksums(tmp_path, monkeypatch):
    committed_tree(monkeypatch)
    repo = tmp_path / "repo"
    (repo / "yeone_ocr").mkdir(parents=True)
    (repo / "yeone_ocr" / "__init__.py").write_text('__version__ = "0.7.1"\n', encoding="utf-8")
    license_tree(repo)
    output = tmp_path / "dist"
    output.mkdir()
    (output / release.PORTABLE_NAME).write_bytes(pe_bytes())
    # Inno Setup's bootstrap may be x86 even for an x64-only installed application.
    (output / "YeonE_OCR-0.7.1-Setup-x64.exe").write_bytes(pe_bytes(0x014C))
    result = release.prepare(output, repo)
    assert len(result["artifacts"]) == 6
    assert result["license_files"] == 4
    checksums = {}
    for line in (output / "SHA256SUMS.txt").read_text(encoding="utf-8").splitlines():
        digest, name = line.split("  ", 1)
        checksums[name] = digest
        assert digest == hashlib.sha256((output / name).read_bytes()).hexdigest()
    assert set(checksums) == set(result["artifacts"]) - {"SHA256SUMS.txt"}
    with zipfile.ZipFile(output / "YeonE_OCR-0.7.1-licenses.zip") as archive:
        assert "THIRD_PARTY_LICENSES/dependency/LICENSE" in archive.namelist()
        assert archive.read("DISTRIBUTION_NOTICE.txt") == b"distribution notice"
