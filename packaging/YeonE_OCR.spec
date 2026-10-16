# -*- mode: python ; coding: utf-8 -*-
"""Windows x64, self-contained onedir GUI. Run with tools/build_windows.ps1.

Only the application package and explicitly listed resources are collected.
Never add the repository root as a data tree: it can contain private OCR jobs.
"""
import ast
from pathlib import Path
import struct
import sys

from PyInstaller.utils.hooks import collect_all, collect_data_files, collect_dynamic_libs, copy_metadata
from PyInstaller.utils.win32.versioninfo import FixedFileInfo, StringFileInfo, StringStruct, StringTable, VarFileInfo, VarStruct, VSVersionInfo

ROOT = Path(SPECPATH).resolve().parent
if sys.platform != "win32" or struct.calcsize("P") != 8:
    raise SystemExit("Build YeonE_OCR with 64-bit Python on Windows.")

module = ast.parse((ROOT / "yeone_ocr" / "__init__.py").read_text(encoding="utf-8"))
VERSION = next(ast.literal_eval(node.value) for node in module.body if isinstance(node, ast.Assign)
               and any(isinstance(target, ast.Name) and target.id == "__version__" for target in node.targets))
VERSION_PARTS = tuple(int(part) for part in VERSION.split(".")) + (0,)

datas = collect_data_files("yeone_ocr", includes=["지침.md", "data/*.json", "assets/*.ico", "assets/*.png", "assets/*.svg"])
datas += [(str(ROOT / "README.md"), ".")]
# Isolated engine Python processes need real script files rather than modules
# inside the executable's PYZ. Keep this explicit resource allowlist; never
# collect engine environments, models, work records or private source documents.
for name in ("rapidocr_worker.py", "easyocr_worker.py"):
    datas.append((str(ROOT / "ab_ocr" / name), "ab_ocr"))
for name in ("install_tesseract.py", "tesseract-lock.json", "install_rapidocr.py", "rapidocr-lock.json",
             "install_easyocr.py", "easyocr-lock.json"):
    datas.append((str(ROOT / "scripts" / name), "scripts"))
for name in ("LOCAL_OCR.md", "INTEGRATION.md", "HANDOFF.md", "AI_REVIEW.md", "release-notes.md", "AB_OCR_PROVENANCE.json"):
    datas.append((str(ROOT / "docs" / name), "docs"))
for name in ("THIRD_PARTY_NOTICES.txt", "requirements-windows.lock.txt"):
    resource = ROOT / "packaging" / name
    if resource.exists():
        datas.append((str(resource), "."))
for resource in (ROOT / "LICENSE", ROOT / "DISTRIBUTION_NOTICE.txt"):
    if resource.is_file():
        datas.append((str(resource), "."))
for directory, destination in ((ROOT / "packaging" / "licenses", "licenses/bundled"),
                               (ROOT / "packaging" / "third_party_licenses", "third_party_licenses"),
                               (ROOT / "resources" / "licenses", "licenses/upstream")):
    if directory.is_dir():
        datas.append((str(directory), destination))
python_license = Path(sys.base_prefix) / "LICENSE.txt"
if python_license.is_file():
    datas.append((str(python_license), "licenses/python"))

binaries = []
hiddenimports = ["tkinter", "tkinter.ttk", "tkinter.filedialog", "tkinter.messagebox", "tkinter.simpledialog",
                 "PIL.ImageTk", "PIL.ImageGrab", "PIL.IcoImagePlugin", "fitz", "pymupdf", "cv2", "numpy", "lxml.etree",
                 "pydantic_core", "jiter", "yeone_ocr.launcher", "yeone_ocr.frozen_smoke"]

def runtime_module(name):
    return name != "google.genai._test_api_client" and not any(
        part in {"tests", "testing", "test", "examples", "benchmarks"} for part in name.split("."))

# SDKs resolve resource/model classes dynamically. These explicit packages also
# include hwpx/data/Skeleton.hwpx, OpenCC dictionaries, hanja/table.yml and DOCX templates.
for package in ("yeone_ocr", "ab_ocr", "google.genai", "openai", "anthropic", "hwpx", "opencc", "hanja", "docx"):
    package_datas, package_binaries, package_imports = collect_all(
        package, include_py_files=False, filter_submodules=runtime_module,
        exclude_datas=["**/tests/**", "**/test/**", "**/examples/**", "**/__pycache__/**"], on_error="warn once")
    # Local resources were allowlisted above; never broaden that set implicitly.
    if package not in ("yeone_ocr", "ab_ocr"):
        datas += package_datas
    binaries += package_binaries
    hiddenimports += package_imports

# Standard PyInstaller/contrib hooks collect native extensions and Tcl/Tk. Add
# package-owned DLLs explicitly for MuPDF/OpenCV and the token/schema runtimes.
for package in ("pymupdf", "cv2", "numpy", "PIL", "lxml", "pydantic_core", "jiter"):
    binaries += collect_dynamic_libs(package)

for distribution in ("pymupdf", "pillow", "numpy", "opencv-python-headless", "pydantic", "pydantic-core",
                     "google-genai", "google-auth", "openai", "anthropic", "python-hwpx", "python-docx",
                     "opencc-python-reimplemented", "hanja", "PyYAML", "lxml", "certifi", "httpx", "httpcore",
                     "anyio", "jiter", "typing-extensions", "requests", "websockets", "tenacity"):
    datas += copy_metadata(distribution)

# Newer OpenAI/Anthropic SDKs use httpx2, while older supported versions use httpx.
try:
    datas += copy_metadata("httpx2")
except Exception as exc:
    from importlib.metadata import PackageNotFoundError
    if not isinstance(exc, PackageNotFoundError):
        raise

version_resource = VSVersionInfo(
    ffi=FixedFileInfo(filevers=VERSION_PARTS, prodvers=VERSION_PARTS, mask=0x3F, flags=0x0, OS=0x40004,
                      fileType=0x1, subtype=0x0, date=(0, 0)),
    kids=[StringFileInfo([StringTable("040904B0", [
        StringStruct("CompanyName", "YeonE_OCR"), StringStruct("FileDescription", "YeonE_OCR · 연어OCR"),
        StringStruct("FileVersion", VERSION), StringStruct("InternalName", "YeonE_OCR"),
        StringStruct("OriginalFilename", "YeonE_OCR.exe"), StringStruct("ProductName", "YeonE_OCR · 연어OCR"),
        StringStruct("ProductVersion", VERSION),
    ])]), VarFileInfo([VarStruct("Translation", [0x0409, 1200])])],
)

analysis = Analysis(
    [str(ROOT / "YeonE_OCR.pyw")], pathex=[str(ROOT)],
    binaries=sorted(set(binaries)), datas=sorted(set(datas)), hiddenimports=sorted(set(hiddenimports)),
    hookspath=[], hooksconfig={}, runtime_hooks=[],
    excludes=["pytest", "_pytest", "coverage", "coveralls", "pip", "IPython", "notebook", "torch", "tensorflow"],
    noarchive=False, optimize=0,
)
pyz = PYZ(analysis.pure)
executable = EXE(
    pyz, analysis.scripts, [], exclude_binaries=True, name="YeonE_OCR", debug=False,
    bootloader_ignore_signals=False, strip=False, upx=False, console=False,
    disable_windowed_traceback=False, argv_emulation=False, target_arch=None, codesign_identity=None,
    entitlements_file=None, icon=str(ROOT / "yeone_ocr" / "assets" / "YeonE_OCR.ico"),
    version=version_resource, contents_directory="_internal",
)
bundle = COLLECT(executable, analysis.binaries, analysis.datas, strip=False, upx=False, name="YeonE_OCR")

# 0.7.0's updater replaces a single executable. Keep its existing portable asset
# contract alongside the installer; an onedir executable alone is not portable.
portable = EXE(
    pyz, analysis.scripts, analysis.binaries, analysis.datas, [], name="YeonE_OCR-Windows-x64",
    debug=False, bootloader_ignore_signals=False, strip=False, upx=False, console=False,
    disable_windowed_traceback=False, runtime_tmpdir=None,
    icon=str(ROOT / "yeone_ocr" / "assets" / "YeonE_OCR.ico"), version=version_resource,
)
