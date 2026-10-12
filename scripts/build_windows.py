"""Build the Windows x64 one-file executable with the current interpreter.

No packages are installed and no network access is performed by this script.
Use --prepare-only to inspect the resource/license manifests without building.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import importlib.metadata as metadata
import json
import os
from pathlib import Path
import platform
import re
import shutil
import struct
import subprocess
import sys

from packaging.requirements import Requirement

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ("pymupdf", "pillow", "numpy", "opencv-python-headless", "pydantic", "google-genai",
           "python-hwpx", "opencc-python-reimplemented", "hanja", "python-docx")


def version():
    text = (ROOT / "yeone_ocr" / "__init__.py").read_text(encoding="utf-8")
    found = re.search(r'^__version__\s*=\s*[\'"]([^\'"]+)', text, flags=re.M)
    if not found:
        raise RuntimeError("Cannot read application version")
    return found.group(1)


def write_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def sha256(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def runtime_distributions():
    """Follow active requirements; do not pull in unselected optional extras."""
    pending = list(RUNTIME) + ["pyinstaller", "pyinstaller-hooks-contrib"]
    distributions = {}
    while pending:
        name = pending.pop()
        dist = metadata.distribution(name)
        key = re.sub(r"[-_.]+", "-", dist.metadata["Name"]).lower()
        if key in distributions:
            continue
        distributions[key] = dist
        for raw in dist.requires or ():
            requirement = Requirement(raw)
            if requirement.marker is None or requirement.marker.evaluate({"extra": ""}):
                pending.append(requirement.name)
    return dict(sorted(distributions.items()))


def copy_licenses(destination, distributions):
    destination.mkdir(parents=True, exist_ok=True)
    result = {}
    for name, dist in distributions.items():
        paths = []
        for entry in dist.files or ():
            parts = [part.lower() for part in entry.parts]
            leaf = entry.name.lower()
            is_notice = (leaf.startswith(("license", "licence", "copying", "notice", "copyright"))
                         or "licenses" in parts or "licences" in parts)
            if not is_notice or entry.suffix.lower() in (".py", ".pyc", ".pyd", ".dll"):
                continue
            source = Path(dist.locate_file(entry))
            if not source.is_file():
                continue
            # The dependency wheel controls its relative path. Strip .. and
            # retain distribution subdirectories to preserve duplicate names.
            relative = Path(*[part for part in entry.parts if part not in ("..", ".", "\\", "/")])
            target = destination / name / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            paths.append(target.relative_to(destination).as_posix())
        result[name] = {"version": dist.version, "files": sorted(paths),
                        "license_metadata": dist.metadata.get("License-Expression") or dist.metadata.get("License", ""),
                        "upstream": dist.metadata.get_all("Project-URL") or [dist.metadata.get("Home-page", "")]}
        supplied = ROOT / "resources" / "licenses" / name
        if supplied.is_dir():
            for source in sorted(supplied.rglob("*")):
                if source.is_file():
                    target = destination / name / "upstream" / source.relative_to(supplied)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, target)
                    result[name]["files"].append(target.relative_to(destination).as_posix())
            result[name]["supplement"] = "Upstream license supplied with the source tree; see provenance.txt."
        if not result[name]["files"]:
            result[name]["note"] = "No separate license file supplied by the installed distribution. Review upstream source."
    python_license = Path(sys.base_prefix) / "LICENSE.txt"
    if python_license.is_file():
        shutil.copy2(python_license, destination / "PYTHON-LICENSE.txt")
    tcl_root = Path(sys.base_prefix) / "tcl"
    if tcl_root.exists():
        for source in tcl_root.rglob("license.terms"):
            target = destination / "tcl-tk" / source.relative_to(tcl_root)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
    shutil.copy2(ROOT / "resources" / "THIRD_PARTY_NOTICES.txt", destination / "THIRD_PARTY_NOTICES.txt")
    shutil.copy2(ROOT / "DISTRIBUTION_NOTICE.txt", destination / "DISTRIBUTION_NOTICE.txt")
    project_license = ROOT / "LICENSE"
    if project_license.is_file():
        shutil.copy2(project_license, destination / "YEONE-OCR-LICENSE.txt")
    write_json(destination / "license-manifest.json", result)
    return result


def version_resource(app_version):
    components = tuple(int(x) for x in app_version.split(".")[:3])
    numeric = components + (0,) * (4 - len(components))
    return f'''# UTF-8
VSVersionInfo(
  ffi=FixedFileInfo(filevers={numeric!r}, prodvers={numeric!r}, mask=0x3f,
                    flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[StringFileInfo([StringTable('041204B0', [
    StringStruct('CompanyName', 'YeonE_OCR contributors'),
    StringStruct('FileDescription', '연어OCR · 한자·한글 문헌 OCR'),
    StringStruct('FileVersion', {app_version!r}),
    StringStruct('InternalName', 'YeonE_OCR'),
    StringStruct('OriginalFilename', 'YeonE_OCR.exe'),
    StringStruct('ProductName', 'YeonE_OCR · 연어OCR'),
    StringStruct('ProductVersion', {app_version!r})
  ])]), VarFileInfo([VarStruct('Translation', [1042, 1200])])]
)
'''


def prepare_resources(directory):
    directory.mkdir(parents=True, exist_ok=True)
    distributions = runtime_distributions()
    licenses = copy_licenses(directory / "licenses", distributions)
    app_version = version()
    (directory / "version_info.txt").write_text(version_resource(app_version), encoding="utf-8")
    git = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True, capture_output=True, check=False)
    # Include source hashes, including uncommitted fixes, in the artifact itself.
    files = [ROOT / "windows_entry.py", ROOT / "YeonE_OCR.spec", ROOT / "pyproject.toml"]
    files += sorted((ROOT / "yeone_ocr").rglob("*.py"))
    files += [ROOT / "yeone_ocr" / "지침.md", ROOT / "scripts" / "build_windows.py"]
    manifest = {"application": "YeonE_OCR", "version": app_version,
                "built_at_utc": datetime.now(timezone.utc).isoformat(),
                "python": platform.python_version(), "platform": platform.platform(),
                "architecture_bits": struct.calcsize("P") * 8,
                "base_commit": git.stdout.strip() if git.returncode == 0 else None,
                "dependencies": {name: dist.version for name, dist in distributions.items()},
                "source_sha256": {p.relative_to(ROOT).as_posix(): sha256(p) for p in files},
                "licenses_without_separate_file": [name for name, data in licenses.items() if not data["files"]]}
    write_json(directory / "build-manifest.json", manifest)
    return manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dist-dir", type=Path, default=ROOT / "dist" / "windows")
    parser.add_argument("--work-dir", type=Path, default=ROOT / "build" / "windows")
    parser.add_argument("--prepare-only", action="store_true")
    args = parser.parse_args(argv)
    if sys.platform != "win32" or struct.calcsize("P") != 8:
        parser.error("Build on 64-bit Windows with 64-bit Python; PyInstaller does not cross-compile.")
    dist_dir, work_dir = args.dist_dir.resolve(), args.work_dir.resolve()
    resources = work_dir / "resources"
    manifest = prepare_resources(resources)
    print(json.dumps({"version": manifest["version"], "resources": str(resources),
                      "license_files_missing": manifest["licenses_without_separate_file"]}, ensure_ascii=False))
    if args.prepare_only:
        return 0
    environment = dict(os.environ, YEONE_BUILD_RESOURCES=str(resources))
    # PyInstaller is supplied by the caller's build environment. No pip/network.
    subprocess.run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
                    "--distpath", str(dist_dir), "--workpath", str(work_dir / "pyinstaller"),
                    str(ROOT / "YeonE_OCR.spec")], cwd=ROOT, env=environment, check=True)
    executable = dist_dir / "YeonE_OCR.exe"
    if not executable.is_file():
        raise RuntimeError("PyInstaller did not create YeonE_OCR.exe")
    asset = dist_dir / "YeonE_OCR-Windows-x64.exe"
    shutil.copy2(executable, asset)
    (dist_dir / (asset.name + ".sha256")).write_text(f"{sha256(asset)}  {asset.name}\n", encoding="ascii")
    shutil.copytree(resources / "licenses", dist_dir / "THIRD_PARTY_LICENSES", dirs_exist_ok=True)
    shutil.copy2(ROOT / "resources" / "THIRD_PARTY_NOTICES.txt", dist_dir / "THIRD_PARTY_NOTICES.txt")
    shutil.copy2(ROOT / "DISTRIBUTION_NOTICE.txt", dist_dir / "DISTRIBUTION_NOTICE.txt")
    manifest["release_asset"] = {"filename": asset.name, "sha256": sha256(asset), "bytes": asset.stat().st_size}
    write_json(dist_dir / "build-manifest.json", manifest)
    print(json.dumps({"executable": str(executable), **manifest["release_asset"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
