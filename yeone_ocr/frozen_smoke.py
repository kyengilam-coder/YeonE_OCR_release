"""배포 앱의 자체완결성 검사. 실제 API나 사용자의 설정/문서를 사용하지 않는다.

Frozen: YeonE_OCR.exe --smoke-test <report.json>
Source: python -m yeone_ocr.frozen_smoke <report.json>
"""
from __future__ import annotations

from datetime import datetime, timezone
import contextlib
import importlib
import importlib.metadata
import io
import json
from pathlib import Path
import struct
import sys
import tempfile
import traceback


def run(report_path: Path) -> int:
    from . import __version__
    report_path = Path(report_path).resolve()
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report = {"version": __version__, "frozen": bool(getattr(sys, "frozen", False)),
              "python": sys.version.split()[0], "bits": struct.calcsize("P") * 8,
              "platform": sys.platform, "checked_at": datetime.now(timezone.utc).isoformat(), "checks": []}

    def check(name, operation):
        try:
            detail = operation()
            report["checks"].append({"name": name, "ok": True, "detail": detail})
        except Exception as exc:
            report["checks"].append({"name": name, "ok": False, "error": f"{type(exc).__name__}: {exc}",
                                     "traceback": traceback.format_exc()})

    packages = {"pymupdf": "pymupdf", "PIL": "pillow", "numpy": "numpy", "cv2": "opencv-python-headless",
                "pydantic": "pydantic", "google.genai": "google-genai", "openai": "openai", "anthropic": "anthropic",
                "hwpx": "python-hwpx", "docx": "python-docx", "opencc": "opencc-python-reimplemented",
                "hanja": "hanja", "lxml.etree": "lxml", "certifi": "certifi"}
    for module, distribution in packages.items():
        def package_probe(module=module, distribution=distribution):
            importlib.import_module(module)
            return {"distribution": distribution, "version": importlib.metadata.version(distribution)}
        check(f"import:{module}", package_probe)

    def data_probe():
        from .app_icon import ASSETS
        from .normalization import rule_data
        from .prompts import load_guide
        from .collate.reading import reading
        from opencc import OpenCC
        import certifi
        guide = load_guide()
        assert len(guide) > 1000 and "지침 파일 yeone_ocr/지침.md 이 없습니다" not in guide
        assert (ASSETS / "YeonE_OCR.ico").is_file()
        assert (ASSETS / "app-icon-256.png").is_file()
        assert Path(certifi.where()).is_file()
        assert OpenCC("s2t").convert("汉书") == "漢書"
        korean = reading("大薊")
        assert korean == "대계", korean
        rules = rule_data()
        assert len(rules["rules"]) >= 100
        return {"guide_chars": len(guide), "msr_rules": len(rules["rules"]), "hanja": korean, "opencc": "漢書"}
    check("bundled-data", data_probe)

    def sdk_probe():
        # Construct clients and access the APIs used by OCR without making a request.
        from google import genai
        from openai import OpenAI
        from anthropic import Anthropic
        clients = []
        try:
            clients.append(genai.Client(api_key="AIza" + "x" * 35, vertexai=False))
            clients.append(OpenAI(api_key="smoke-test-not-a-key", max_retries=0))
            clients.append(Anthropic(api_key="smoke-test-not-a-key", max_retries=0))
            assert callable(clients[0].models.generate_content)
            assert callable(clients[1].responses.create)
            assert callable(clients[2].messages.create)
        finally:
            for client in clients:
                client.close()
        return ["Gemini generate_content", "OpenAI Responses", "Anthropic Messages"]
    check("provider-clients-offline", sdk_probe)

    def tkinter_probe():
        import tkinter as tk
        from tkinter import ttk
        from .app_icon import ASSETS, apply_window_icon
        window = tk.Tk()
        window.withdraw()
        try:
            ttk.Style(window).theme_use("clam")
            image = tk.PhotoImage(master=window, file=str(ASSETS / "app-icon-256.png"))
            assert image.width() == 256 and image.height() == 256
            apply_window_icon(window)
            return {"tcl": window.tk.call("info", "patchlevel"), "tk": tk.TkVersion, "icon": "256x256"}
        finally:
            window.destroy()
    check("tkinter-and-icon", tkinter_probe)

    with tempfile.TemporaryDirectory(prefix="yeone-smoke-", dir=report_path.parent) as temporary:
        folder = Path(temporary).resolve()
        assert folder.is_relative_to(report_path.parent)

        def native_probe():
            import cv2
            import numpy as np
            import pymupdf
            from PIL import Image
            from lxml import etree
            image_path = folder / "image.png"
            Image.new("RGB", (64, 48), "white").save(image_path)
            with Image.open(image_path) as image:
                gray = cv2.cvtColor(np.asarray(image), cv2.COLOR_RGB2GRAY)
            assert gray.shape == (48, 64) and int(gray.min()) == 255
            assert etree.fromstring(b"<root><text>ok</text></root>")[0].text == "ok"
            pdf = folder / "fixture.pdf"
            with pymupdf.open() as doc:
                page = doc.new_page(width=300, height=300)
                page.insert_text((36, 60), "OCR smoke test.", fontsize=14)
                page.insert_text((250, 60), "天地玄黃", fontname="china-t", fontsize=14, rotate=270)
                doc.save(pdf)
            with pymupdf.open(pdf) as doc:
                pixmap = doc[0].get_pixmap()
                assert pixmap.width == 300
                assert "OCR smoke test." in doc[0].get_text()
            return {"image": "Pillow/NumPy/OpenCV", "pdf": "MuPDF create/read/render", "xml": "lxml"}
        check("native-rendering", native_probe)

        def screenshot_probe():
            from PIL import Image, ImageGrab
            from .screenshots import save_screenshot
            from .pdfio import open_pdf, page_image
            assert callable(ImageGrab.grabclipboard)
            if sys.platform == "win32":
                assert callable(Image.core.grabclipboard_win32)
            with Image.new("RGBA", (320, 180), (0, 0, 0, 0)) as screenshot:
                screenshot.putpixel((20, 30), (10, 20, 30, 255))
                saved = save_screenshot(screenshot, folder / "screenshots")
                assert save_screenshot(screenshot, folder / "screenshots") == saved
            with open_pdf(str(saved)) as document:
                assert len(document) == 1
                extracted, _ = page_image(document, 1)
                try:
                    assert extracted.size == (320, 180)
                    assert extracted.getpixel((20, 30)) == (10, 20, 30)
                    assert extracted.getpixel((0, 0)) == (255, 255, 255)
                finally:
                    extracted.close()
            return {"clipboard_backend": "available (not read)", "input": "PNG → one-page OCR input",
                    "pixels": "original resolution preserved", "repeat_paste": "same source", "network_calls": 0}
        check("screenshot-input", screenshot_probe)

        def direction_probe():
            import pymupdf
            from .cli import detect_mode
            from .pdfio import open_pdf
            checked = []
            text = "天地玄黃宇宙洪荒日月盈昃辰宿列張"
            for mode in ("horizontal_zh", "vertical"):
                path = folder / f"direction-{mode}.png"
                with pymupdf.open() as source:
                    page = source.new_page(width=600, height=700)
                    if mode == "horizontal_zh":
                        for row in range(8):
                            page.insert_text((40, 60 + row * 42), text, fontname="china-t", fontsize=20)
                    else:
                        for col in range(8):
                            for row, char in enumerate(text):
                                page.insert_text((540 - col * 42, 60 + row * 24), char, fontname="china-t", fontsize=20)
                    page.get_pixmap(matrix=pymupdf.Matrix(1.5, 1.5)).save(path)
                # Opening the raster loses the fixture's text layer deliberately.
                with open_pdf(str(path)) as document:
                    assert not document[0].get_text().strip()
                    detected, reason = detect_mode(document)
                    assert detected == mode, f"{mode}: {detected} ({reason})"
                    checked.append({"expected": mode, "detected": detected, "reason": reason})
            return {"raster_text_directions": checked, "network_calls": 0}
        check("image-reading-direction", direction_probe)

        def pipeline_probe():
            from .cli import CANCEL, main
            from .verify import check_hwpx_file
            from .schema import PageReading
            from zipfile import ZipFile
            assert PageReading.model_validate({"pdf_page": 1}).pdf_page == 1
            CANCEL.clear()
            work = folder / "ocr"
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = main(["run", str(folder / "fixture.pdf"), "--work", str(work), "--reader", "mock",
                             "--no-adjudicate", "--no-second-pass", "--no-context", "--checkpoint", "0", "--docx"])
            assert code == 0, f"mock pipeline exit {code}: {output.getvalue()[-3000:]}"
            hwpx = work / "out" / "fixture.hwpx"
            docx = work / "out" / "fixture.docx"
            assert hwpx.is_file() and docx.is_file()
            assert check_hwpx_file(str(hwpx)) == []
            for path, member in ((hwpx, "Contents/section0.xml"), (docx, "word/document.xml")):
                with ZipFile(path) as archive:
                    assert "天地玄黃" in archive.read(member).decode("utf-8")
            assert (work / "review.html").is_file()
            assert (work / "out" / "fixture_normalization.html").is_file()
            return {"pipeline": "mock PDF → OCR records → HWPX/DOCX → normalization/review", "network_calls": 0}
        check("offline-ocr-pipeline", pipeline_probe)

    report["ok"] = all(item["ok"] for item in report["checks"])
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("Usage: python -m yeone_ocr.frozen_smoke report.json")
    raise SystemExit(run(Path(sys.argv[1])))
