from pathlib import Path
from unittest.mock import patch
import os
from zipfile import ZipFile
from xml.etree import ElementTree as ET

import pytest

from tests.test_recheck import setup, args_for, files, record
from yeone_ocr import cli, recheck
from yeone_ocr.recheck_review import Review
from yeone_ocr.state import Work


def reviewed_book(tmp_path):
    pdf, old = setup(tmp_path)
    old.save_record(2, record(2, '天地校書甲正文乙'))
    current = tmp_path / 'reread' / '작업자료'
    def run(args, work):
        for n, text in [(1, '정상 본문'), (2, '天地校書新正文改'), (3, '추가된 본문')]:
            work.save_record(n, record(n, text))
    with patch.object(cli, 'cmd_run', side_effect=run), patch.object(cli, 'cmd_build', return_value=0):
        assert recheck.command(args_for(pdf, old.dir, current, 'all')) == 0
    return pdf, old, current


def test_reviewed_export_mixes_two_changes_and_preserves_sources(tmp_path):
    from tests.test_template_headings import template
    pdf, old, current = reviewed_book(tmp_path)
    previous = files(old.dir), files(current), files(current.parent / '기존작업_보존')
    style = tmp_path / 'book-style.hwpx'
    template(style)
    session = Review(pdf, current)
    assert [c.page for c in session.changes] == [2, 2, 3]
    session.decisions = {0: True, 1: False, 2: True}
    session.save()
    reopened = Review(pdf, current)
    assert reopened.decisions == session.decisions
    with patch.object(cli, 'create_reader', side_effect=AssertionError('no model calls')):
        work, hwpx = reopened.export(['--layout', 'flow', '--template', str(style), '--preset', 'basic',
                                      '--no-context', '--no-traditional', '--no-punctuation'])
    assert work != current and hwpx.is_file()
    with ZipFile(hwpx) as archive:
        xml = ET.fromstring(archive.read('Contents/section0.xml'))
        text = ''.join(xml.itertext())
        assert '天地校書新正文乙' in text and '추가된 본문' in text
        assert '天地校書新正文改' not in text
    final = Work(pdf, work, initialize=False)
    assert final.load_state()['last_build']['template'] == str(style)
    assert recheck.page_text(final.load_record(2)) == '天地校書新正文乙'
    assert files(old.dir) == previous[0] and files(current) == previous[1]
    assert files(current.parent / '기존작업_보존') == previous[2]
    assert (work.parent / '확정_내역.json').is_file()


def test_requires_complete_decisions_and_refuses_stale_or_conflicting_reviews(tmp_path):
    pdf, _, current = reviewed_book(tmp_path)
    a, b = Review(pdf, current), Review(pdf, current)
    with pytest.raises(ValueError, match='검토하지'):
        a.export([])
    a.decisions = dict.fromkeys(range(3), False)
    a.save()
    with pytest.raises(ValueError, match='다른 창'):
        b.save()
    fresh = Work(pdf, current, initialize=False)
    fresh.save_record(2, record(2, '새로운 교정'))
    with pytest.raises(ValueError, match='바뀌'):
        a.export([])
    reopened = Review(pdf, current)
    assert reopened.stale and not reopened.decisions
    reopened.save()
    assert list(current.parent.glob('이전_검토-*.json'))
    assert not list(tmp_path.glob('확정-*'))


def test_structural_change_is_an_explicit_single_choice(tmp_path):
    pdf, _, current = reviewed_book(tmp_path)
    fresh = Work(pdf, current, initialize=False)
    changed = fresh.load_record(2)
    changed['merged']['regions'][0].update(kind='제목', title_text='新題', title_level=1, paragraphs=[])
    fresh.save_record(2, changed)
    session = Review(pdf, current)
    assert len(session.changes) == 2 and session.changes[0].path is None
    session.decisions = {0: True, 1: False}
    approved = session.approved_records()
    assert approved[1]['merged']['regions'][0]['kind'] == '제목'
    assert recheck.page_text(approved[2]) == ''  # kept prior missing reading
    with patch.object(cli, 'build_outputs', return_value=False):
        with pytest.raises(ValueError, match='검사'):
            session.export([])
    assert not list(tmp_path.glob('확정-*'))


def test_preserved_local_human_correction_participates_in_review(tmp_path):
    from tests.test_local_first import LocalFirstTests
    from yeone_ocr.local_first import command
    from yeone_ocr.comparison import ComparisonProject
    fixture = LocalFirstTests()
    fixture.setUp()
    try:
        with patch.dict(os.environ, {'YEONE_OCR_CLAUDE_AUTH':'api',
                                      'YEONE_OCR_CONFUSIONS_PATH': str(fixture.root/'ledger.json')}):
            assert command(fixture.args()) == 0
            project_path = Path(fixture.record()['local_first']['project_path'])
            project = ComparisonProject.load(project_path)
            project.set_text(1, '天地玄黃校', reason='원본 대조')
            project.save()
            snapshot = project_path.read_bytes()
            current = tmp_path/'새판'/'작업자료'
            pdf = project.source_path
            def run(args, work):
                work.save_record(1, record(1, '天地玄黃效'))
            with patch.object(cli, 'cmd_run', side_effect=run), patch.object(cli, 'cmd_build', return_value=0):
                assert recheck.command(args_for(pdf, fixture.work, current, 'all')) == 0
            review = Review(pdf, current)
            assert '校' in review.changes[0].before
            review.decisions = dict.fromkeys(range(len(review.changes)), False)
            assert '校' in recheck.page_text(review.approved_records()[0])
            assert project_path.read_bytes() == snapshot
    finally:
        fixture.doCleanups()


def test_review_window_accept_preserve_and_select_final_version(tmp_path):
    import time
    import tkinter as tk
    from unittest.mock import Mock
    from yeone_ocr.gui_recheck_review import RecheckReviewWindow
    pdf, _, current = reviewed_book(tmp_path)
    app = tk.Tk()
    app.withdraw()
    app.running = False
    app.pdf = lambda: str(pdf)
    app._build_opts = lambda: ['--layout', 'flow', '--preset', 'basic', '--no-context']
    app.select_output_file = Mock()
    def until(test):
        end = time.monotonic() + 20
        while not test() and time.monotonic() < end:
            app.update()
            time.sleep(.02)
        assert test()
    try:
        with patch('yeone_ocr.gui_recheck_review.messagebox.showinfo'), \
             patch('yeone_ocr.gui_recheck_review.messagebox.showerror', side_effect=AssertionError), \
             patch('yeone_ocr.gui_recheck_review.messagebox.askyesno', return_value=True):
            view = RecheckReviewWindow(app, app.pdf(), current)
            until(lambda: view.session is not None)
            assert view.blocks[0].tag_ranges('change')
            view.choose(True)
            view.keep_remaining()
            view.export()
            until(lambda: app.select_output_file.called)
            directory, output = app.select_output_file.call_args.args
            assert directory.name == '작업자료' and output.is_file()
            assert not view.winfo_exists()
    finally:
        app.destroy()
