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


def committed_tree(monkeypatch, version="0.7.1", extra_files=None):
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
    files.update(extra_files or {})
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


def test_source_allowlist_preserves_all_current_local_engine_resources():
    root = Path(__file__).resolve().parents[1]
    required = set(release.SOURCE_DATA | release.SOURCE_ENGINE_LOCKS | release.SOURCE_JSON_DOCS | release.SOURCE_PACKAGING)
    required.update(path.relative_to(root).as_posix() for path in (root / "ab_ocr").glob("*.py"))
    required.update(f"scripts/install_{engine}.py" for engine in ("tesseract", "rapidocr", "easyocr"))
    assert required
    for name in required:
        assert (root / name).is_file(), name
        assert release.source_allowed(name), name
    # Presets must stay synchronized with the real built-in registry/data files.
    from yeone_ocr.presets import BUILTIN
    assert {f"yeone_ocr/data/preset_{key}.json" for key in BUILTIN} <= release.SOURCE_DATA


def test_source_allowlist_rejects_private_inputs_models_and_runtime_homes():
    rejected = [
        "work/book/result.py", "ab_ocr/work/result.py", "ab_ocr/Work/result.py",
        "ab_ocr/source.png", "ab_ocr/source.pdf", "ab_ocr/result.docx", "ab_ocr/raw.json",
        "ab_ocr/config.json", "ab_ocr/credentials.json", "ab_ocr/.env.production", "ab_ocr/private_key.pem",
        "ab_ocr/model.onnx", "ab_ocr/model.pth", "ab_ocr/model.traineddata", "ab_ocr/model.safetensors",
        "resources/model.onnx", "resources/licenses/model.pth", "packaging/third_party_licenses/model.traineddata",
        "resources/source.png", "packaging/source.png", "packaging/private-record.json",
        "scripts/private-settings.json", "scripts/unknown-lock.json", "docs/private-record.json",
        "yeone_ocr/data/private-record.json", "yeone_ocr/data/preset_private.json",
        "tools/easyocr/credentials.py", "tools/rapidocr/cache.py", "tools/tesseract/installation.py",
        "scripts/downloads/private.py", "tests/private.hwpx", "docs/private.pdf", "packaging/.env.production",
        "ab_ocr/../private.py", "/ab_ocr/ocr.py", "C:/Users/private/credentials.json",
    ]
    for name in rejected:
        assert not release.source_allowed(name), name


def test_source_archive_keeps_integration_resources_as_committed_bytes(tmp_path, monkeypatch):
    required = {
        "ab_ocr/ocr.py": "# vendored local engine interface\n",
        "ab_ocr/runtime.py": "# independent runtime resource selection\n",
        "ab_ocr/rapidocr_worker.py": "# isolated RapidOCR worker\n",
        "ab_ocr/easyocr_worker.py": "# isolated EasyOCR worker\n",
        "scripts/install_tesseract.py": "# pinned installer\n",
        "scripts/install_rapidocr.py": "# pinned installer\n",
        "scripts/install_easyocr.py": "# pinned installer\n",
        "scripts/tesseract-lock.json": '{"models":[{"sha256":"pinned"}]}\n',
        "scripts/rapidocr-lock.json": '{"models":[{"sha256":"pinned"}]}\n',
        "scripts/easyocr-lock.json": '{"models":[{"sha256":"pinned"}]}\n',
        "docs/AB_OCR_PROVENANCE.json": '{"source_project":"AB_OCR"}\n',
        "yeone_ocr/data/preset_basic.json": '{"id":"basic"}\n',
        "yeone_ocr/data/preset_kaibao.json": '{"id":"kaibao"}\n',
        "yeone_ocr/data/preset_regions.json": '{"id":"regions"}\n',
    }
    private = {"ab_ocr/source.png": "private synthetic image", "ab_ocr/model.pth": "private synthetic weights",
               "scripts/private-settings.json": "private synthetic configuration",
               "resources/licenses/model.onnx": "private synthetic weights"}
    committed_tree(monkeypatch, extra_files={**required, **private})
    output = tmp_path / "source.zip"
    result = release.create_source_archive(tmp_path, output, "0.7.1")
    assert result["files"] == 7 + len(required)
    prefix = "YeonE_OCR-0.7.1/"
    with zipfile.ZipFile(output) as archive:
        for name, content in required.items():
            assert archive.read(prefix + name) == content.encode("utf-8")
        for name in private:
            assert prefix + name not in archive.namelist()


def test_source_allowlist_retains_current_upstream_license_inventory():
    root = Path(__file__).resolve().parents[1]
    licenses = root / "packaging" / "third_party_licenses"
    files = [path for path in licenses.rglob("*") if path.is_file()]
    assert files
    assert all(release.source_allowed(path.relative_to(root).as_posix()) for path in files)


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


import unittest  # noqa: E402  (unittest discover 도 거두게 — 위의 시험은 pytest 꼴)


class PresetPackaging(unittest.TestCase):
    """짜임 규칙 내장 파일(yeone_ocr/data/preset_*.json)이 pip 꾸러미(pyproject package-data)와 Windows 실행 파일(PyInstaller spec)에 다 든다
    — 빠지면 실행 파일에서 本草 조문(kaibao)을 찾지 못해 내용별 문서가 막힌다."""
    def test_every_builtin_preset_is_packaged(self):
        import ast, fnmatch, re, tomllib
        from yeone_ocr.presets import BUILTIN, DATA
        root = Path(__file__).resolve().parents[1]
        files = sorted(p.relative_to(root / "yeone_ocr").as_posix() for p in DATA.glob("preset_*.json"))
        self.assertEqual(files, sorted(f"data/preset_{i}.json" for i in BUILTIN))
        package_data = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["tool"]["setuptools"]["package-data"]["yeone_ocr"]
        m = re.search(r'collect_data_files\("yeone_ocr",\s*includes=(\[[^\]]*\])\)', (root / "packaging" / "YeonE_OCR.spec").read_text(encoding="utf-8"))
        self.assertIsNotNone(m)
        spec_includes = ast.literal_eval(m.group(1))
        for f in files:
            self.assertTrue(any(fnmatch.fnmatch(f, g) for g in package_data), (f, package_data))
            self.assertTrue(any(fnmatch.fnmatch(f, g) for g in spec_includes), (f, spec_includes))
