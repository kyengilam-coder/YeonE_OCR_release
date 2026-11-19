"""Capture the GUI command destination and reopen the exact finished document."""
import json
from pathlib import Path
from time import monotonic, sleep
from types import SimpleNamespace
from unittest.mock import patch

import pymupdf

from yeone_ocr import gui
from yeone_ocr.gui_result_versions import ResultNavigation
from yeone_ocr.result_versions import OutputRun, ResultVersion, remember, versions
from yeone_ocr.schema import PageReading
from yeone_ocr.state import Work


def test_recheck_start_finish_and_restart_keep_previous_output(tmp_path, monkeypatch):
    config = tmp_path / 'user-config.json'
    config.write_text(json.dumps({'auto_update': False, 'reader': 'mock', 'input_tab': 0}))
    monkeypatch.setattr(gui, 'CONFIG_PATH', config)
    monkeypatch.setattr(gui.App, '_fetch_fx', lambda self: None)
    monkeypatch.setattr(gui, 'missing_packages', lambda: [])
    source = tmp_path / '책.pdf'
    with pymupdf.open() as pdf:
        pdf.new_page()
        pdf.save(source)
    original = Work(source, gui.work_dir_for(str(source)))
    original.save_record(1, {'pdf_page': 1, 'merged': PageReading(pdf_page=1).model_dump()})
    previous = original.out_dir / '책.hwpx'
    previous.write_bytes(b'original document')
    before = original.record_path(1).read_bytes()
    new = tmp_path / '재판독' / '작업자료'
    app = gui.App()
    try:
        app.withdraw()
        app._use_pdf(str(source))
        with patch.object(gui, 'open_path') as shell_open:
            app.start(['run', str(source), '--reader', 'mock', '--recheck-from', str(original.dir),
                       '--recheck-mode', 'all', '--work', str(new), '--no-context-model', '--no-adjudicate'],
                      '기존 OCR 재검수·재판독', open_after=new.parent)
            deadline = monotonic() + 45
            while app.running and monotonic() < deadline:
                app.update()
                sleep(.02)
            assert not app.running
            assert app.selected_result_work() == new and app.work() == original.dir
            assert Path(app.v_output_dir.get()) == new.parent / 'out'
            app.open_hwpx()
            document = shell_open.call_args.args[0]
            assert document.parent == new.parent / 'out' and document.suffix == '.hwpx'
            assert previous.read_bytes() == b'original document' and original.record_path(1).read_bytes() == before
            app.open_work()
            shell_open.assert_called_with(new.parent)
            with patch('yeone_ocr.collate.section_editor.BookmarkSectionEditor', return_value=SimpleNamespace()) as editor:
                app.open_result_editor()
                assert editor.call_args.kwargs['hwpx_path'] == document
                assert editor.call_args.kwargs['work_dir'] == new
            other = tmp_path / '다른책.pdf'
            other.write_bytes(source.read_bytes())
            app._use_pdf(str(other))
            assert app.selected_result() is None
            reopened = SimpleNamespace(pdf=lambda: str(source), cfg=gui.load_config(), work=lambda: original.dir)
            assert ResultNavigation._result_file(reopened, '.hwpx') == document
    finally:
        app.destroy()


def test_pdf_reuse_selects_exact_new_version_and_ignores_noop(tmp_path):
    source = tmp_path / 'book.pdf'
    source.write_bytes(b'original')
    work = Work(source, tmp_path / 'older' / '작업자료')
    out = work.out_dir / 'book - 261118_012345.pdf'
    argv = ['textlayer', str(source), '--work', str(work.dir), '--out', str(out)]
    run = OutputRun.capture(argv)
    out.write_bytes(b'searchable document')
    result = run.completed()
    assert result.file('.pdf') == out and result.file('.hwpx') is None
    assert OutputRun.capture(argv).completed() is None
    cfg = {}
    remember(cfg, source, result)
    assert versions(json.loads(json.dumps(cfg)), source)[1] == result


def test_existing_output_history_rejects_invalid_source_and_retains_original_entry(tmp_path):
    source = tmp_path / 'book.pdf'
    source.write_bytes(b'original')
    first = Work(source, tmp_path / 'old' / '작업자료')
    second = Work(source, tmp_path / 'new' / '작업자료')
    cfg = {}
    for work in (first, second):
        path = work.out_dir / 'book.hwpx'
        path.write_bytes(b'output')
        remember(cfg, source, ResultVersion(str(work.dir), '출력', path.name))
    app = SimpleNamespace(pdf=lambda: str(source), cfg=cfg, work=lambda: first.dir)
    entries, active = versions(cfg, source)
    assert len(entries) == 2
    assert ResultNavigation._result_file(app, '.hwpx') == second.out_dir / 'book.hwpx'
    remember(cfg, source, entries[1])
    assert ResultNavigation._result_file(app, '.hwpx') == first.out_dir / 'book.hwpx'
    source.write_bytes(b'wrong pdf')
    assert ResultNavigation._result_file(app, '.hwpx') is None
