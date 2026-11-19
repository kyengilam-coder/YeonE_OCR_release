"""Start choices preserve saved data and reach the existing resume policy."""
import contextlib
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase, mock
import tempfile

from test_ops import gui_module
from test_gui_settings_merge import Var
from yeone_ocr.gui_run_start import recorded_pages


class RunStartTests(TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.gui = self.stack.enter_context(gui_module())
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))

    def app(self):
        return SimpleNamespace(work=lambda: self.root, v_pages=Var('2-8'),
                               v_local_resume=Var(self.gui.RESUME_LABELS['continue']),
                               v_force=Var(True), wait_window=mock.Mock())

    def record(self):
        folder=self.root/'readings';folder.mkdir()
        (folder/'p003.json').write_text('preserved OCR',encoding='utf8')

    def test_filename_scan_is_read_only_and_numeric(self):
        self.assertEqual(recorded_pages(self.root), [])
        self.assertFalse((self.root/'readings').exists())
        self.record()
        for name in ('p012.json','p001.json','pabc.json','p003.backup.json'):
            (self.root/'readings'/name).write_text('data')
        self.assertEqual(recorded_pages(self.root), [1,3,12])

    def test_selected_policy_is_not_overwritten_or_prompted_again(self):
        self.record();path=self.root/'readings/p003.json';before=path.read_bytes()
        with mock.patch.object(self.gui.App,'_local_first',return_value=True):
            for choice in ('continue','unfinished','all'):
                app=self.app();app.v_local_resume.set(self.gui.RESUME_LABELS[choice])
                self.assertTrue(self.gui.App._choose_run_start(app))
                app.wait_window.assert_not_called()
                self.assertEqual(app.v_local_resume.get(),self.gui.RESUME_LABELS[choice])
                self.assertFalse(app.v_force.get())
                self.assertEqual(path.read_bytes(),before)

    def test_invalid_selection_stops_instead_of_silently_using_default(self):
        self.record();app=self.app();app.v_local_resume.set('invalid')
        with mock.patch.object(self.gui.App,'_local_first',return_value=True), mock.patch.object(
                self.gui.messagebox,'showerror') as error:
            self.assertFalse(self.gui.App._choose_run_start(app))
        error.assert_called_once()
        self.assertEqual(app.v_local_resume.get(),'invalid')
        self.assertTrue(app.v_force.get())

    def test_hwpx_launch_passes_selected_continue_to_cli(self):
        from yeone_ocr.cli import build_parser
        self.record();app=self.app()
        app.cfg=dict(self.gui.DEFAULTS)
        app.v_ocr_strategy=Var(self.gui.OCR_STRATEGY_LABELS['local-first'])
        app.v_reader=Var('openai')
        app.pdf=lambda: 'book.pdf'
        app._need_pdf=lambda: True
        app._prepare_env=lambda: True
        app.nb=SimpleNamespace(select=mock.Mock())
        app._save_book=mock.Mock()
        app._pages=lambda: []
        app._read_opts=lambda: []
        app._glyph_opts=lambda **kw: []
        app._answered_opts=lambda: []
        app._build_opts=lambda: []
        app._with_plan=lambda command,go: go([])
        app.start=mock.Mock()
        with mock.patch.object(self.gui.App,'_result_editors_ready',return_value=True), mock.patch.object(
                self.gui.App,'_corrections_ok',return_value=True), mock.patch.object(
                self.gui.App,'_save_ledger'), mock.patch.object(self.gui,'template_ok',return_value=True), mock.patch.object(
                self.gui,'preset_ok',return_value=True):
            self.gui.App.do_run(app)
        app.wait_window.assert_not_called()
        argv=app.start.call_args.args[0]
        parsed=build_parser().parse_args(argv)
        self.assertEqual(parsed.local_resume,'continue')
        self.assertFalse(parsed.force)
        self.assertEqual(app.v_local_resume.get(),self.gui.RESUME_LABELS['continue'])

    def test_new_book_and_other_strategies_do_not_prompt(self):
        for local in (False,True):
            app = self.app()
            with mock.patch.object(self.gui.App, '_local_first', return_value=local):
                self.assertTrue(self.gui.App._choose_run_start(app))
            app.wait_window.assert_not_called()

    def test_launch_cancel_blocks_preparation_plan_and_execution(self):
        app=SimpleNamespace(_need_pdf=lambda:True,pdf=lambda:'book.pdf',
                            _prepare_env=mock.Mock(),start=mock.Mock(),_with_plan=mock.Mock())
        with mock.patch.object(self.gui.App,'_strategy_opts',return_value=[]), mock.patch.object(
                self.gui.App,'_result_editors_ready',return_value=True), mock.patch.object(
                self.gui.App,'_corrections_ok',return_value=True), mock.patch.object(
                self.gui.App,'_choose_run_start',return_value=False):
            self.gui.App.do_run(app)
        app._prepare_env.assert_not_called();app._with_plan.assert_not_called();app.start.assert_not_called()
