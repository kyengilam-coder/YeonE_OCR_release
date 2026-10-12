# -*- mode: python ; coding: utf-8 -*-
"""Windows x64 one-file GUI. Invoke through scripts/build_windows.py."""
from pathlib import Path
import os

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, collect_submodules, copy_metadata

root = Path(SPECPATH)
resources = Path(os.environ["YEONE_BUILD_RESOURCES"])

datas = [(str(root / "yeone_ocr" / "지침.md"), "yeone_ocr"),
         (str(resources / "licenses"), "licenses"),
         (str(resources / "build-manifest.json"), "."),
         (str(root / "DISTRIBUTION_NOTICE.txt"), "."),
         (str(root / "resources" / "THIRD_PARTY_NOTICES.txt"), ".")]
for package in ("hwpx", "hanja", "opencc", "docx"):
    datas += collect_data_files(package)

# The SDK and distribution-version queries need metadata even inside a PYZ.
# Recursive metadata includes auth/http/pydantic requirements without importing
# their optional extras (torch/aiohttp/local tokenizers are not used).
for distribution in ("pymupdf", "pillow", "numpy", "opencv-python-headless", "pydantic", "google-genai",
                     "python-hwpx", "opencc-python-reimplemented", "hanja", "python-docx"):
    datas += copy_metadata(distribution, recursive=True)

hiddenimports = collect_submodules("yeone_ocr") + collect_submodules(
    "google.genai", filter=lambda name: not name.startswith("google.genai.tests")
    and name != "google.genai._test_api_client")
# hanja.__init__ resolves impl by __import__(a string) on first translation;
# static analysis cannot see it. impl then imports hangul, table, and PyYAML.
hiddenimports += collect_submodules("hanja")
hiddenimports += ["tkinter", "tkinter.ttk", "tkinter.filedialog", "tkinter.messagebox", "PIL.ImageTk",
                  "unittest.mock", "email.mime.text", "email.mime.multipart", "xml.etree.ElementTree"]

# Standard hooks supply NumPy/OpenCV/Tk/lxml/Pydantic binaries; explicit MuPDF
# collection protects its sibling DLL lookup without bundling mupdf-devel.
binaries = collect_dynamic_libs("pymupdf")
a = Analysis([str(root / "windows_entry.py")], pathex=[str(root)], binaries=binaries, datas=datas,
             hiddenimports=hiddenimports, hookspath=[], hooksconfig={}, runtime_hooks=[],
             # MuPDF's optional Table.to_pandas() would otherwise pull every
             # globally installed data-science dependency into a system-site
             # build. The app never uses DataFrames, Excel, or these SDK extras.
             excludes=["IPython", "jupyter", "matplotlib", "torch", "tensorflow", "pytest", "_pytest", "google.genai.tests",
                       "google.genai._test_api_client",
                       "pandas", "scipy", "pyarrow", "numba", "llvmlite", "sklearn", "sympy",
                       "polars", "xarray", "dask", "openpyxl"],
             noarchive=False, optimize=0)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, a.binaries, a.datas, [], name="YeonE_OCR", debug=False,
          bootloader_ignore_signals=False, strip=False, upx=False, console=False,
          disable_windowed_traceback=False, version=str(resources / "version_info.txt"),
          runtime_tmpdir=None, contents_directory="_internal")
