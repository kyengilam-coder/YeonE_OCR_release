"""Build third-party notices from this build interpreter's installed metadata.

Run with the same virtual-environment Python used by PyInstaller. This reads only
requirements, installed package metadata/license files, and the Python runtime's
license files. It does not inspect application settings or OCR work folders.
"""
from __future__ import annotations

import argparse
from importlib import metadata
from pathlib import Path, PurePosixPath
import re
import shutil
import sys

from packaging.requirements import Requirement


ROOT = Path(__file__).resolve().parents[1]


def normalized(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def runtime_distributions(requirements: Path) -> list[metadata.Distribution]:
    """Resolve active runtime requirements, including requested dependency extras."""
    pending = [Requirement(line.split("#", 1)[0].strip())
               for line in requirements.read_text(encoding="utf-8").splitlines()
               if line.split("#", 1)[0].strip()]
    found: dict[str, metadata.Distribution] = {}
    processed: set[tuple[str, tuple[str, ...]]] = set()
    while pending:
        requirement = pending.pop()
        if requirement.marker and not requirement.marker.evaluate():
            continue
        key = normalized(requirement.name)
        extras = tuple(sorted(requirement.extras))
        if (key, extras) in processed:
            continue
        processed.add((key, extras))
        dist = metadata.distribution(requirement.name)
        if requirement.specifier and dist.version not in requirement.specifier:
            raise RuntimeError(f"{requirement}: installed version is {dist.version}")
        found[key] = dist
        for item in dist.requires or []:
            child = Requirement(item)
            if child.marker and not any(child.marker.evaluate({"extra": extra})
                                        for extra in ("", *extras)):
                continue
            # The parent's requested extras have already been evaluated. They
            # must not be evaluated again using a different dependency's extras.
            child.marker = None
            pending.append(child)
    return [found[key] for key in sorted(found)]


def license_description(dist: metadata.Distribution) -> str:
    meta = dist.metadata
    expression = meta.get("License-Expression", "").strip()
    if expression:
        return expression
    declared = meta.get("License", "").strip()
    if declared and declared.upper() != "UNKNOWN":
        if len(declared) <= 250 and len(declared.splitlines()) <= 3:
            return " ".join(declared.split())
        return "License metadata contains full text; see METADATA.txt."
    classifiers = [value.removeprefix("License :: ") for value in meta.get_all("Classifier", [])
                   if value.startswith("License :: ")]
    return "; ".join(classifiers) or "Not specified in installed package metadata."


def license_files(dist: metadata.Distribution) -> list[metadata.PackagePath]:
    matches = []
    for entry in dist.files or []:
        parts = [part.lower() for part in PurePosixPath(str(entry).replace("\\", "/")).parts]
        leaf = parts[-1] if parts else ""
        if "__pycache__" in parts or leaf.endswith((".pyc", ".pyo")):
            continue
        if (any(part in {"license", "licenses", "licence", "licences"} for part in parts[:-1])
                or leaf.startswith(("license", "licence", "copying", "notice", "third-party", "third_party"))):
            if Path(dist.locate_file(entry)).is_file():
                matches.append(entry)
    return sorted(matches, key=str)


def copy_component(dist: metadata.Distribution, licenses_dir: Path, display_root: str,
                   supplements: Path) -> list[str]:
    name = dist.metadata.get("Name", "unknown")
    component = normalized(name) + "-" + dist.version
    folder = licenses_dir / component
    folder.mkdir(parents=True, exist_ok=True)
    metadata_text = dist.read_text("METADATA") or dist.read_text("PKG-INFO")
    if metadata_text is None:
        raise RuntimeError(f"Raw package metadata was not found for {name}")
    (folder / "METADATA.txt").write_text(metadata_text, encoding="utf-8")
    copied = []
    for entry in license_files(dist):
        parts = PurePosixPath(str(entry).replace("\\", "/")).parts
        # Distribution records may use ../ for sibling data folders. Preserve
        # the readable source name without letting it escape the destination.
        safe = [re.sub(r'[<>:"|?*]', "_", part) for part in parts if part not in {"/", ".", ".."}]
        target = folder.joinpath(*safe)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(dist.locate_file(entry), target)
        copied.append(f"{display_root}/{component}/" + "/".join(safe))
    supplement = supplements / normalized(name)
    if supplement.is_dir():
        for source in sorted(supplement.rglob("*")):
            if not source.is_file():
                continue
            relative = source.relative_to(supplement)
            target = folder / "upstream-supplement" / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            copied.append(f"{display_root}/{component}/upstream-supplement/{relative.as_posix()}")
    urls = list(dict.fromkeys([value for value in dist.metadata.get_all("Project-URL", [])
                             if value.strip()] +
                            ([dist.metadata["Home-page"]] if dist.metadata.get("Home-page") else [])))
    lines = [f"{name} {dist.version}", f"  Declared license: {license_description(dist)}"]
    lines.extend(f"  URL: {url}" for url in urls)
    if not urls:
        lines.append("  URL: Not specified in installed package metadata.")
    lines.append(f"  Metadata: {display_root}/{component}/METADATA.txt")
    lines.extend(f"  License/notice file: {path}" for path in copied)
    if not copied:
        lines.append("  License/notice file: No separate license file found in the installed distribution.")
    return lines


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--requirements", type=Path, default=ROOT / "requirements.txt")
    parser.add_argument("--output", type=Path, default=ROOT / "packaging" / "THIRD_PARTY_NOTICES.txt")
    parser.add_argument("--licenses-dir", type=Path, default=ROOT / "packaging" / "third_party_licenses")
    parser.add_argument("--supplements", type=Path, default=ROOT / "packaging" / "license_supplements")
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.licenses_dir.mkdir(parents=True, exist_ok=True)
    display_root = args.licenses_dir.name
    runtime = runtime_distributions(args.requirements)
    lines = [
        "YeonE_OCR - Third-party component notices", "",
        "Generated from the installed runtime dependency metadata of the build environment.",
        "This includes declared transitive dependencies that the executable builder may exclude.",
        "License names and URLs below reproduce package declarations; they are not a legal assessment.",
        "The referenced license texts and METADATA.txt files are included alongside this notice.",
        "Package-provided notices can cover additional native libraries within a wheel.",
        "The PyInstaller bootloader is listed separately from the application's Python dependencies.", "",
        f"Python {sys.version.split()[0]}",
        "  URL: https://www.python.org/",
    ]
    python_license = Path(sys.base_prefix) / "LICENSE.txt"
    if not python_license.is_file():
        python_license = Path(sys.base_prefix) / "LICENSE"
    if not python_license.is_file():
        raise FileNotFoundError("Python runtime LICENSE.txt/LICENSE was not found in sys.base_prefix")
    python_folder = args.licenses_dir / "python"
    python_folder.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(python_license, python_folder / "LICENSE.txt")
    lines.extend(["  License: See the Python runtime's bundled LICENSE.txt.",
                  f"  License/notice file: {display_root}/python/LICENSE.txt", ""])
    import _tkinter
    lines.extend([f"Tcl {_tkinter.TCL_VERSION} / Tk {_tkinter.TK_VERSION}",
                  "  URL: https://www.tcl-lang.org/",
                  "  License: See bundled Tcl/Tk license text and Python LICENSE.txt."])
    tcl_root = Path(sys.base_prefix) / "tcl"
    tcl_copied = []
    for source in sorted(tcl_root.rglob("license.terms")):
        relative = source.relative_to(tcl_root)
        destination = args.licenses_dir / "tcl-tk" / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        tcl_copied.append(f"{display_root}/tcl-tk/{relative.as_posix()}")
    lines.extend(f"  License/notice file: {item}" for item in tcl_copied)
    if not tcl_copied:
        lines.append("  No separate license.terms file was found in the build Python's tcl folder.")
    lines.extend(["", "Runtime Python distributions", "============================"])
    for dist in runtime:
        lines.extend(["", *copy_component(dist, args.licenses_dir, display_root, args.supplements)])
    bootloader = metadata.distribution("pyinstaller")
    lines.extend(["", "Executable bootloader", "=====================", "",
                  *copy_component(bootloader, args.licenses_dir, display_root, args.supplements), ""])
    args.output.write_text("\n".join(lines), encoding="utf-8")
    print(f"Wrote {args.output} ({len(runtime)} runtime distributions, Python, Tcl/Tk, PyInstaller bootloader)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
