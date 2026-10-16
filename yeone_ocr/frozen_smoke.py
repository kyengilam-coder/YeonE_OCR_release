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
import time
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
        from .presets import resolve_preset, BUILTIN
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
        assert all(resolve_preset(i).label for i in BUILTIN)                # 짜임 규칙 내장 파일(data/preset_*.json)이 꾸러미에 — 없으면 本草 짜임을 잃는다
        assert resolve_preset("kaibao").source_re.search("所在有之。　唐附")
        return {"guide_chars": len(guide), "msr_rules": len(rules["rules"]), "hanja": korean, "opencc": "漢書", "presets": list(BUILTIN)}
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

        def comparison_project_probe():
            from .comparison import ComparisonProject
            from ab_ocr.ocr import file_sha256
            source = folder / "image.png"
            project = ComparisonProject.open(source)
            source_hash = file_sha256(source)
            texts = {"tesseract": "漢樂一\ufe00，", "rapidocr": "漢樂一\ufe00，", "easyocr": "漢樂一\ufe00。"}
            for engine, text in texts.items():
                raw = {"kind": "recognition", "schema_version": 1, "engine": engine,
                       "coordinate_system": "rendered-image pixels, xyxy",
                       "text": text, "words": [{"text": text, "bbox": [1, 1, 63, 47],
                           "confidence": None, "block": 1, "paragraph": 1, "line": 1}],
                       "source_path": str(source), "source_sha256": source_hash,
                       "page_number": 1, "image_width": 64, "image_height": 48,
                       "engine_version": "offline-smoke-fixture", "languages": "chi_tra",
                       "psm": 6 if engine == "tesseract" else None, "duration_seconds": 0,
                       "preprocessing": {}, "engine_metadata": {"synthetic": True}}
                project.add_result(raw, request={"profile": "traditional", "direction": "horizontal", "dpi": 300,
                                                 "engine_root": None, "policy": "local-three-engine-v1"})
            original_runs = project.page(1)["runs"]
            suggestions = project.compare_page(1, baseline="tesseract")
            candidate = next(item for item in suggestions if item["candidates"]["rapidocr"] != item["baseline_text"])
            project.accept_candidate(1, candidate["id"], "rapidocr")
            assert project.text(1) != texts["tesseract"]
            assert project.undo_edit(1)
            assert project.text(1) == texts["tesseract"]
            corrected = "人手校正，樂一\ufe00。\n한글 원문"
            project.set_text(1, corrected, reason="offline-smoke-human-fixture")
            target = folder / "comparison.yeone-compare.json"
            project.save(target)
            reopened = ComparisonProject.load(target)
            assert reopened.text(1) == corrected
            assert reopened.page(1)["runs"] == original_runs
            assert [edit["kind"] for edit in reopened.page(1)["edits"]] == ["candidate", "undo", "manual"]
            recovery = folder / "comparison-recovery.yeone-compare.json"
            reopened.save_recovery(recovery)
            assert ComparisonProject.load(recovery).text(1) == corrected
            assert file_sha256(source) == source_hash
            return {"raw_runs": 3, "unicode": "compatibility ideograph and variation selector preserved",
                    "audit": "candidate → undo → manual", "storage": "project and recovery reopen", "network_calls": 0}
        check("local-comparison-project", comparison_project_probe)

        def comparison_ai_probe():
            from types import SimpleNamespace
            from .comparison import ComparisonProject
            from .comparison_ai import AISettings, ReviewPolicy, prepare_plan, run_plan
            from ab_ocr.ocr import file_sha256
            source = folder / "image.png"
            project = ComparisonProject.open(source)
            original = "误汉,樂一\ufe00한글"
            for engine, text in (("tesseract", original), ("rapidocr", "论汉,樂一\ufe00한글"),
                                 ("easyocr", "论汉,樂一\ufe00한글")):
                raw = {"kind": "recognition", "schema_version": 1, "engine": engine,
                       "coordinate_system": "rendered-image pixels, xyxy", "text": text,
                       "words": [{"text": text, "bbox": [1, 1, 63, 47], "confidence": None,
                                  "block": 1, "paragraph": 1, "line": 1}],
                       "source_path": str(source), "source_sha256": file_sha256(source), "page_number": 1,
                       "image_width": 64, "image_height": 48, "engine_version": "offline-ai-fixture",
                       "languages": "chi_tra", "psm": 6 if engine == "tesseract" else None,
                       "duration_seconds": 0, "preprocessing": {}, "engine_metadata": {"synthetic": True}}
                project.add_result(raw, request={"profile": "traditional", "direction": "horizontal", "dpi": 300,
                                                 "engine_root": None, "policy": "local-three-engine-v1"})
            project.compare_page(1, baseline="tesseract")
            raw_runs = project.page(1)["runs"]
            settings = AISettings(provider="openai", model="offline-vision-fixture", max_calls=2)
            policy = ReviewPolicy(traditional=True, punctuation=True)
            plan = prepare_plan(project, [1], policy, settings=settings)
            assert plan.summary["calls"] == 2
            requests = []
            def request(**kwargs):
                requests.append(kwargs)
                assert kwargs["model"] == settings.model and kwargs["store"] is False
                parts = kwargs["input"][0]["content"]
                assert any(item["type"] == "input_image" for item in parts)
                prompt = parts[0]["text"]
                data = json.loads(prompt.split("Source DATA (JSON):\n", 1)[1].rsplit("\nReturn JSON", 1)[0])
                items = []
                for target in data["targets"]:
                    patches = [(0, 1, "误", "论", "recognition")] if data["phase"] == "visual" else [
                        (0, 1, "误", "論", "traditional"), (1, 2, "汉", "漢", "traditional"),
                        (2, 3, ",", "，", "punctuation")]
                    proposals = [{"start": start, "end": end, "before": before, "after": after,
                                  "kind": kind, "reason": "offline synthetic protocol fixture", "uncertain": False}
                                 for start, end, before, after, kind in patches
                                 if target["start"] <= start <= end <= target["end"]]
                    items.append({"target_id": target["id"], "uncertain": False,
                                  "note": "offline fixture", "proposals": proposals})
                return SimpleNamespace(output_text=json.dumps({"items": items}, ensure_ascii=False),
                                       status="completed", usage=None, model=settings.model, id="offline-response")
            client = SimpleNamespace(responses=SimpleNamespace(create=request), close=lambda: None)
            checkpoints = []
            outcome = run_plan(project, plan, api_key="offline-fixture-key",
                               client_factory=lambda *_: client, on_result=checkpoints.append)
            assert outcome["completed"] == 2 and outcome["failed"] == 0 and len(requests) == 2
            assert len(checkpoints) == 2 and project.text(1) == original
            reviews = project.page(1)["ai_reviews"]
            assert len(reviews) == 2 and project.page(1)["runs"] == raw_runs
            for ending in ("論", "漢", "，"):
                proposal = next(item for item in project.ai_proposals(1) if item["after"] == ending)
                project.accept_ai_proposal(1, proposal["review_id"], proposal["id"])
            assert project.text(1) == "論漢，樂一\ufe00한글"
            for _ in range(3):
                assert project.undo_edit(1)
            assert project.text(1) == original and project.page(1)["ai_reviews"] == reviews
            target = folder / "ai-comparison.yeone-compare.json"
            project.save(target)
            reopened = ComparisonProject.load(target)
            assert reopened.page(1)["runs"] == raw_runs and reopened.page(1)["ai_reviews"] == reviews
            assert "offline-fixture-key" not in target.read_text(encoding="utf-8")
            resumed = prepare_plan(reopened, [1], policy, settings=settings)
            assert resumed.summary["calls"] == 0
            return {"stages": "image disagreement → whole-page traditional/punctuation proposals",
                    "mock_requests": 2, "human_accepts_and_undos": 3, "raw_and_ai_records": "unchanged",
                    "storage": "reopened; credential excluded; completed requests reused", "network_calls": 0}
        check("comparison-ai-proposals-offline", comparison_ai_probe)

        def comparison_resources_probe():
            from ab_ocr.runtime import resource_root, worker_path
            checked = []
            for engine in ("rapidocr", "easyocr"):
                worker = worker_path(engine)
                assert worker.is_file(), f"Missing application-owned worker: {worker.name}"
                compile(worker.read_text(encoding="utf-8"), worker.name, "exec")
                checked.append(worker.name)
            for name in ("install_tesseract.py", "tesseract-lock.json", "install_rapidocr.py",
                         "rapidocr-lock.json", "install_easyocr.py", "easyocr-lock.json"):
                resource = resource_root() / "scripts" / name
                assert resource.is_file(), f"Missing installer resource: {name}"
                if name.endswith(".json"):
                    assert isinstance(json.loads(resource.read_text(encoding="utf-8")), dict)
                else:
                    compile(resource.read_text(encoding="utf-8"), name, "exec")
                checked.append(name)
            return {"application_resources": checked, "external_models_loaded": False, "network_calls": 0}
        check("local-comparison-worker-resources", comparison_resources_probe)

        def comparison_cli_probe():
            from .comparison_cli import parse_pages
            from .cli import build_parser
            assert parse_pages("1-2,4", 4) == [1, 2, 4]
            rejected = []
            for invalid in ("0", "5", "3-1", "1-", "wrong"):
                try:
                    parse_pages(invalid, 4)
                except ValueError:
                    rejected.append(invalid)
                else:
                    raise AssertionError("Invalid page scope was accepted")
            args = build_parser().parse_args(["compare-engines", str(folder / "image.png"),
                                              "--pages", "1", "--profile", "mixed", "--direction", "vertical"])
            assert callable(args.fn) and args.profile == "mixed" and args.direction == "vertical"
            return {"command": "compare-engines", "strict_invalid_ranges_rejected": len(rejected),
                    "ocr_executed": False, "network_calls": 0}
        check("local-comparison-cli-scope", comparison_cli_probe)

        def comparison_editor_probe():
            import tkinter as tk
            from .comparison import ComparisonProject
            from .comparison_editor import ComparisonEditor
            project = ComparisonProject.load(folder / "comparison.yeone-compare.json")
            original = project.text(1)
            raw_runs = project.page(1)["runs"]
            root = tk.Tk()
            root.withdraw()
            editor = None
            try:
                editor = ComparisonEditor(root, project=project, work_dir=folder / "editor-work",
                                          engine_root=folder / "no-installed-models")
                editor.withdraw()
                deadline = time.monotonic() + 5
                while editor._preview is None and time.monotonic() < deadline:
                    root.update()
                    time.sleep(.02)
                assert editor._preview is not None, "Original image preview did not render"
                assert editor.master is root and isinstance(editor, tk.Toplevel)
                editor.text.insert("end-1c", "，確認")
                editor.text.edit_modified(True)
                editor._commit_text()
                assert project.text(1) == original + "，確認"
                editor.undo_edit()
                assert project.text(1) == original
                assert project.page(1)["runs"] == raw_runs
                assert editor.request_close()
                return {"window": "hosted Toplevel", "preview": "rendered original",
                        "editing": "manual change and undo; raw runs unchanged", "network_calls": 0}
            finally:
                if editor is not None and editor.winfo_exists():
                    editor.destroy()
                root.destroy()
        check("local-comparison-editor-offline", comparison_editor_probe)

    report["ok"] = all(item["ok"] for item in report["checks"])
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("Usage: python -m yeone_ocr.frozen_smoke report.json")
    raise SystemExit(run(Path(sys.argv[1])))
