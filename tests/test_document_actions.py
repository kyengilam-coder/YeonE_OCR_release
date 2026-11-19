"""The regular GUI buttons must format the active reading with this book's settings."""
import json
from pathlib import Path
from unittest.mock import Mock, patch
from xml.etree import ElementTree as ET
from zipfile import ZipFile

import pymupdf

from tests._hwpx_fixtures import HH, HP, make_template, rec, text_region
from yeone_ocr import cli, gui, gui_document_actions
from yeone_ocr.book_output import select_template
from yeone_ocr.result_versions import ResultVersion, remember
from yeone_ocr.state import Work


def test_primary_ocr_and_saved_export_are_separate_and_export_uses_book_style(tmp_path, monkeypatch):
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"reader": "mock", "auto_update": False, "input_tab": 0}))
    monkeypatch.setattr(gui, "CONFIG_PATH", config)
    monkeypatch.setattr(gui.App, "_fetch_fx", lambda self: None)
    monkeypatch.setattr(gui, "missing_packages", lambda: [])
    source = tmp_path / "책.pdf"
    with pymupdf.open() as doc:
        doc.new_page()
        doc.new_page()
        doc.save(source)
    original = Work(source, gui.work_dir_for(str(source)))
    active = Work(source, tmp_path / "재판독" / "작업자료")
    for work, content in [(original, "Previous reading"), (active, "Reviewed reading")]:
        work.save_record(1, rec(1, [text_region("body", [content])]))
    before = [work.record_path(1).read_bytes() for work in (original, active)]
    template = tmp_path / "양식.hwpx"
    make_template(template)
    rule = tmp_path / "책규칙.json"
    rule.write_text(json.dumps({"schema": 1, "styles": {"paragraph": {"body": ["주석"]}}}))
    app = gui.App()
    try:
        app.withdraw()
        app._use_pdf(str(source))
        remember(app.cfg, source, ResultVersion(str(active.dir), "재판독"))
        app.use_book_rule(str(rule))
        select_template(app.v_template, app.v_layout, str(template))
        app.update_idletasks()
        app._counts = {'target': 2, 'records': 1, 'to_read': 1}
        app._sync_run_card()
        assert app.b_run.cget('text') == gui.RESUME_TEXT
        app._ocr_coverage = (str(source), 2, {1})
        app._set_running(True, 'build')
        app._on_progress('build', 2, 2)
        assert app.l_ocr_coverage.cget('text') == '전체 OCR 기록 1/2쪽 · 남은 1쪽'
        assert app.l_output_coverage.cget('text') == '이번 문서 출력 · HWPX 1/1쪽 완료'
        app._on_progress('textlayer', 1, 2)
        assert app.l_output_coverage.cget('text') == '이번 문서 출력 · HWPX 1/1쪽 완료 · 문자 PDF 50%'
        app._set_running(False)
        monkeypatch.setattr(app, "_prepare_env", lambda: True)
        launch = Mock()
        monkeypatch.setattr(app, "start", launch)
        with patch.object(gui_document_actions, "_show") as popup:
            with patch.object(app, 'do_run') as run:
                app.b_run.invoke()
                run.assert_called_once_with()
                popup.assert_not_called()
            app.b_export.invoke()
            menu = popup.call_args.args[1]
            menu.invoke(0)
            args = launch.call_args.args[0]
            assert args[0] == 'build' and '--reuse-records' in args
            parsed = cli.build_parser().parse_args(args)
            assert Path(parsed.work) == active.dir
            assert Path(parsed.template) == template and Path(parsed.preset) == rule
            assert parsed.layout == 'flow'
            menu.invoke(1)
            pdf_args = launch.call_args.args[0]
            assert pdf_args[0] == "textlayer"
            parsed = cli.build_parser().parse_args(pdf_args)
            assert Path(parsed.work) == active.dir and Path(parsed.out).parent == active.out_dir
            menu.invoke(2)
            assert Path(cli.build_parser().parse_args(launch.call_args.args[0]).work) == original.dir
        assert cli.main(args) == 0
        output, = active.out_dir.glob("*.hwpx")
        with ZipFile(output) as package:
            header = ET.fromstring(package.read("Contents/header.xml"))
            style = next(s.get("id") for s in header.iter(HH + "style") if s.get("name") == "주석")
            section = ET.fromstring(package.read("Contents/section0.xml"))
            assert "Previous reading" not in "".join(section.itertext())
            assert any(p.get("styleIDRef") == style and "Reviewed reading" in "".join(p.itertext())
                       for p in section.iter(HP + "p"))
        assert [work.record_path(1).read_bytes() for work in (original, active)] == before
    finally:
        app.destroy()
