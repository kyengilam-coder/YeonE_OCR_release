"""Installed engines must work without the build PC's Python or venv."""
import hashlib
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from ab_ocr.local_engines import inspect_engine
from ab_ocr import runtime
from yeone_ocr.comparison_editor import default_selected_engines
from yeone_ocr.comparison import DEFAULT_ENGINES
from tools.verify_ppocrv6_bundle import verify


def engine_home(root, layout="standalone-python"):
    folder = root / "tools/ppocrv6"
    (folder / "runtime").mkdir(parents=True)
    (folder / "runtime/python.exe").write_bytes(b"synthetic executable")
    (folder / "models").mkdir()
    (folder / "models/model.onnx").write_bytes(b"synthetic model")
    data = {"engine": "ppocrv6", "runtime_layout": layout,
            "models": [{"file": "model.onnx", "sha256": hashlib.sha256(b"synthetic model").hexdigest()}],
            "language_models": {"chi_tra": ["model.onnx"]}}
    (folder / "installation.json").write_text(json.dumps(data), encoding="utf-8")
    return folder


def test_standalone_detection_uses_relocated_python_and_preserves_hash_verification(tmp_path):
    root = tmp_path / "한글 공백 설치 위치"
    folder = engine_home(root)
    info = inspect_engine("ppocrv6", engine_root=root)
    assert info["available"], info
    assert Path(info["executable"]) == folder / "runtime/python.exe"
    assert info["languages"] == ["chi_tra"]
    (folder / "models/model.onnx").write_bytes(b"modified")
    assert not inspect_engine("ppocrv6", engine_root=root)["available"]


def test_packaged_default_selects_included_engine_without_loading_it(tmp_path):
    assert default_selected_engines(tmp_path) == DEFAULT_ENGINES
    engine_home(tmp_path)
    assert default_selected_engines(tmp_path) == ("ppocrv6",)
    (tmp_path / "tools/ppocrv6/installation.json").write_text("null")
    assert default_selected_engines(tmp_path) == DEFAULT_ENGINES


def test_standalone_does_not_fall_back_to_unrelated_venv(tmp_path):
    folder = engine_home(tmp_path)
    (folder / "runtime/python.exe").unlink()
    venv = folder / "venv/Scripts/python.exe"
    venv.parent.mkdir(parents=True)
    venv.write_bytes(b"unrelated")
    assert not inspect_engine("ppocrv6", engine_root=tmp_path)["available"]


def test_saved_legacy_home_does_not_hide_bundled_v6_or_replace_other_engine_roots(tmp_path):
    bundled, old = tmp_path / "app", tmp_path / "legacy saved folder"
    folder = engine_home(bundled)
    old.mkdir()
    with patch.object(runtime, "application_root", return_value=bundled):
        assert runtime.engine_root_for("ppocrv6", old) == bundled
        assert runtime.engine_root_for("easyocr", old) == old
        assert inspect_engine("ppocrv6", engine_root=old)["available"]
        assert default_selected_engines(old) == ("ppocrv6",)
        (folder / "models/model.onnx").write_bytes(b"changed bundled bytes")
        engine_home(old)
        assert not inspect_engine("ppocrv6", engine_root=old)["available"]


def test_frozen_invalid_manifest_does_not_silently_use_saved_copy(tmp_path):
    bundled, old = tmp_path / "app", tmp_path / "old"
    folder = engine_home(bundled)
    engine_home(old)
    (folder / "installation.json").write_text("null")
    with patch.object(runtime, "application_root", return_value=bundled), \
            patch.object(runtime.sys, "frozen", True, create=True):
        assert not inspect_engine("ppocrv6", engine_root=old)["available"]


def test_bundle_inventory_rejects_tampering_and_path_escape(tmp_path):
    folder = engine_home(tmp_path)
    for name in ("runtime/python313.dll", "runtime/python313._pth", "runtime/msvcp140.dll", "runtime/msvcp140_1.dll"):
        (folder / name).write_bytes(b"synthetic")
    files = {p.relative_to(folder).as_posix(): {"bytes": p.stat().st_size,
              "sha256": hashlib.sha256(p.read_bytes()).hexdigest()}
             for p in folder.rglob("*") if p.is_file()}
    manifest = {"engine": "ppocrv6", "files": files}
    path = folder / "bundle-manifest.json"
    path.write_text(json.dumps(manifest))
    verify(folder)
    (folder / "runtime/python.exe").write_bytes(b"changed")
    with pytest.raises(ValueError, match="mismatch"):
        verify(folder)
    manifest["files"]["../escaped"] = manifest["files"].pop("runtime/python.exe")
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError):
        verify(folder)
