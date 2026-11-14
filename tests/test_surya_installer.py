"""Surya 설치기: pip 는 판 번호·해시가 고정된 휠로 설치한다(get-pip.py 는 pip 새 판마다 바이트가 바뀌어 고정 해시와 어긋났다)."""
import hashlib
import json
from pathlib import Path
import sys
import zipfile

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import install_surya  # noqa: E402

LOCK = json.loads((Path(__file__).resolve().parents[1] / "scripts" / "surya-lock.json").read_text(encoding="utf-8"))


def test_lock_pins_a_versioned_pip_wheel_and_no_unversioned_get_pip():
    pip = LOCK["pip_wheel"]
    assert "get_pip_sha256" not in LOCK
    assert pip["url"].startswith("https://files.pythonhosted.org/") and pip["url"].endswith("/" + pip["file"])
    assert pip["file"].startswith("pip-") and pip["file"].endswith("-py3-none-any.whl")
    assert len(pip["sha256"]) == 64 and int(pip["sha256"], 16) >= 0 and pip["size"] > 0
    source = Path(install_surya.__file__).read_text(encoding="utf-8")
    assert 'download("https://bootstrap.pypa.io' not in source and 'lock["get_pip_sha256"]' not in source


def wheel(path, members):
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    return path


def test_pip_wheel_is_unpacked_into_site_packages_once(tmp_path):
    python_root = tmp_path / "python"
    python_root.mkdir()
    source = wheel(tmp_path / "pip-1.0-py3-none-any.whl", {
        "pip/__init__.py": "", "pip/__main__.py": "print('pip')",
        "pip-1.0.dist-info/WHEEL": "Root-Is-Purelib: true\n"})
    site = install_surya.install_pip(python_root, source)
    assert site == python_root / "Lib" / "site-packages"
    assert (site / "pip" / "__main__.py").read_text() == "print('pip')"
    (site / "pip" / "__main__.py").write_text("kept")
    install_surya.install_pip(python_root, source)                   # 이미 있으면 다시 풀지 않는다
    assert (site / "pip" / "__main__.py").read_text() == "kept"


def test_pip_wheel_without_pip_or_with_escaping_paths_is_refused(tmp_path):
    for name, members in (("empty", {"other/__init__.py": ""}), ("escape", {"../evil.py": "x"})):
        root = tmp_path / name / "python"
        root.mkdir(parents=True)
        with pytest.raises(ValueError):
            install_surya.install_pip(root, wheel(tmp_path / f"{name}.whl", members))
    assert not (tmp_path / "evil.py").exists()


def test_download_rejects_bytes_that_differ_from_the_pinned_hash(tmp_path):
    existing = tmp_path / "pip.whl"
    existing.write_bytes(b"changed")
    with pytest.raises(ValueError, match="SHA-256"):
        install_surya.download("https://example.invalid/pip.whl", existing, hashlib.sha256(b"original").hexdigest())
