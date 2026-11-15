"""Hardening from the 1.2.17 review: model-written ids, child environments, oversized inputs."""
import io
import json
import sys
from types import SimpleNamespace
import zipfile
from pathlib import Path

import pymupdf
import pytest

from yeone_ocr.schema import PageResponse, region_file_part


def page_with_id(region_id):
    return {"pdf_page": 1, "regions": [{"id": region_id, "k": "삽화", "bbox": [0, 0, 10, 10]}]}


@pytest.mark.parametrize("region_id", ["x\\..\\..\\Pictures\\family", "../../etc/a", "a/b", "C:evil",
                                       "<img src=x onerror=alert(1)>", "a&b", "a\x01b", "가" * 65])
def test_a_new_answer_cannot_put_path_or_markup_in_a_region_id(region_id):
    with pytest.raises(ValueError):
        PageResponse.model_validate(page_with_id(region_id))


@pytest.mark.parametrize("region_id", ["r1", "fig_2", "그림-3", "A.1"])
def test_ordinary_region_ids_are_still_accepted(region_id):
    assert PageResponse.model_validate(page_with_id(region_id)).regions[0].id == region_id


def test_crop_file_names_never_leave_the_crops_folder():
    assert region_file_part("r1") == "r1"
    for hostile in ("x\\..\\..\\Pictures", "../../a", "a:b", "<b>"):
        part = region_file_part(hostile)
        assert all(c.isalnum() or c in "_-" for c in part), part
    assert region_file_part("a/b") != region_file_part("a\\b")        # cleaned names stay distinct


def test_review_page_escapes_model_written_ids(tmp_path):
    from yeone_ocr import review_html
    from yeone_ocr.state import Work
    pdf = tmp_path / "book.pdf"
    with pymupdf.open() as doc:
        doc.new_page(); doc.save(pdf)
    work = Work(str(pdf), str(tmp_path / "work"))
    work.ensure_source(150)
    hostile = "<img src=x onerror=alert(1)>"
    crop = work.crops_dir / "p001_x.jpg"
    crop.parent.mkdir(parents=True, exist_ok=True)
    crop.write_bytes(b"jpg")
    record = {"pdf_page": 1, "merged": {"pdf_page": 1, "regions": [
        {"id": hostile, "k": "삽화", "bbox": [0, 0, 10, 10]}]},
        "crops": {hostile: {"path": str(crop), "touching": ["<script>"]}},
        "pass2": {hostile: {"columns": [{"col_from_right": "<b>", "text": "x"}]}}}
    html = review_html.make_review(work, [record], str(tmp_path / "review.html"))
    html = Path(html).read_text(encoding="utf-8") if Path(str(html)).exists() else str(html)
    assert hostile not in html and "<script>" not in html


def test_child_processes_never_receive_api_keys(monkeypatch):
    from ab_ocr import runtime
    from yeone_ocr import antigravity_subscription, chatgpt_subscription, claude_subscription, gemini_subscription
    for name in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY", "SOMEVENDOR_API_KEY"):
        monkeypatch.setenv(name, "secret-" + name)
    monkeypatch.setenv("KEEP_ME", "1")
    environments = [runtime.subprocess_environment(), chatgpt_subscription.cli_env(), claude_subscription.cli_env(),
                    gemini_subscription.cli_env(), antigravity_subscription.cli_env()]
    from yeone_ocr import engine_manager
    environments.append(engine_manager.installer_environment())
    for env in environments:
        assert not any(value.startswith("secret-") for value in env.values())
        assert env["KEEP_ME"] == "1"
    captured = {}
    from yeone_ocr import plan_setup

    class Done:
        stdout = io.StringIO("")
        def wait(self):
            return 0
    def runner(command, **kwargs):
        captured.update(kwargs["env"])
        return Done()
    plan_setup.run(["npm", "install"], lambda _line: None, runner=runner)
    assert captured and not any(value.startswith("secret-") for value in captured.values())


def test_codex_review_calls_turn_off_command_and_browser_tools():
    from yeone_ocr import chatgpt_subscription
    assert {"shell_tool", "unified_exec", "browser_use"} <= set(chatgpt_subscription.NO_TOOLS)


def test_system_programs_are_started_by_full_path_on_windows(monkeypatch):
    from yeone_ocr.runtime import system_program
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("SystemRoot", "C:\\Windows")
    assert system_program("taskkill.exe").replace("\\", "/").endswith("Windows/System32/taskkill.exe")
    assert system_program("powershell.exe").replace("\\", "/").endswith("System32/WindowsPowerShell/v1.0/powershell.exe")
    monkeypatch.setattr(sys, "platform", "linux")
    assert system_program("taskkill.exe") == "taskkill.exe"


def test_an_oversized_pdf_page_is_rendered_at_a_lower_resolution():
    from yeone_ocr.pdfio import MAX_RENDER_PIXELS, page_image
    with pymupdf.open() as doc:
        doc.new_page(width=14400, height=14400)
        doc.new_page(width=595, height=842)
        big, note = page_image(doc, 1, 300, prefer_embedded=False)
        assert big.width * big.height <= MAX_RENDER_PIXELS and "dpi" in note
        normal, note = page_image(doc, 2, 300, prefer_embedded=False)
        assert note.startswith("렌더 300dpi")


def test_an_hwpx_that_expands_beyond_the_limit_is_refused(monkeypatch):
    from yeone_ocr.collate import section_edit
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("Contents/big.xml", b"\0" * 2_000_000)
    monkeypatch.setattr(section_edit, "MAX_ARCHIVE_BYTES", 1_000_000)
    with pytest.raises(section_edit.SectionEditError, match="너무 커서"):
        section_edit._Archive(stream.getvalue())


def test_control_characters_do_not_stop_a_docx(tmp_path):
    from docx import Document
    from yeone_ocr.blocks import Block
    from yeone_ocr.docx_builder import build_docx
    from yeone_ocr.text_policy import xml_text
    assert xml_text("가\x01나\t다\n\ufffe") == "가나\t다\n"
    out = tmp_path / "out.docx"
    build_docx([Block(type="para", text="본문\x01글"), Block(type="table", rows=[["칸\x02", "b"]])], str(out))
    text = "\n".join(p.text for p in Document(str(out)).paragraphs)
    assert "본문글" in text


def test_an_ai_page_answer_of_the_wrong_shape_falls_back_to_the_engine(tmp_path):
    from PIL import Image
    from yeone_ocr import ai_page
    client = SimpleNamespace(request=lambda **_: ("[]", True, "", {}, "model"))
    with Image.new("RGB", (200, 300), "white") as image:
        with pytest.raises(ValueError):
            ai_page.read_page(image, direction="vertical", profile="traditional", client=client, model="m",
                              cache_dir=tmp_path)
        (tmp_path / "junk.json").write_text("[1, 2]", encoding="utf-8")
        good = SimpleNamespace(request=lambda **_: (json.dumps({"lines": ["甲"], "uncertain": []}), True, "", {}, "m"))
        assert ai_page.read_page(image, direction="vertical", profile="traditional", client=good, model="m",
                                 cache_dir=tmp_path)["text"] == "甲"


def test_startup_log_hides_keys_set_after_start(tmp_path, monkeypatch):
    from yeone_ocr.windows_app import SafeLog, redact_log
    assert "AQ.Ab8RN6" not in redact_log("key=AQ.Ab8RN6Lr0abcdefghijklmnopqrstu")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    log = SafeLog(tmp_path / "startup.log")
    monkeypatch.setenv("OPENAI_API_KEY", "plain-secret-value-123")
    log.write("using plain-secret-value-123 now\n")
    log.finish()
    assert "plain-secret-value-123" not in (tmp_path / "startup.log").read_text(encoding="utf-8")
