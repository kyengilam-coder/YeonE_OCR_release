import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

from yeone_ocr.state import Work
from yeone_ocr.cli import build_parser, cmd_build, cmd_textlayer


class ReuseRecordsTests(unittest.TestCase):
    def test_textlayer_rejects_original_as_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / 'book.pdf'
            source.write_bytes(b'original')
            work = Work(source, Path(tmp) / 'work')
            work.save_record(1, {'pdf_page': 1})
            args = build_parser().parse_args(['textlayer', str(source), '--work', str(work.dir), '--out', str(source)])
            with patch('yeone_ocr.cli.open_pdf') as opened:
                self.assertEqual(cmd_textlayer(args, work=work, doc=[object()]), 2)
                opened.assert_not_called()
            self.assertEqual(source.read_bytes(), b'original')

    def test_pdf_button_uses_selected_folder_without_hwpx_or_ai_settings(self):
        from yeone_ocr import gui
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            folder = root / 'previous' / '작업자료'
            (folder / 'readings').mkdir(parents=True)
            (folder / 'source.json').write_text('{}')
            (folder / 'readings' / 'p001.json').write_text('{}')
            app = MagicMock()
            app.running = False
            app.pdf.return_value = str(root / 'renamed.pdf')
            app._pages.return_value = ['--pages', '1-3']
            with patch.object(gui.filedialog, 'askdirectory', return_value=str(folder.parent)), \
                 patch.object(gui.App, '_result_editors_ready', return_value=True), \
                 patch.object(gui, 'template_ok') as template, patch.object(gui, 'preset_ok') as preset:
                gui.App.do_reuse_textlayer(app)
            argv = app.start.call_args.args[0]
            self.assertEqual(argv[:4], ['textlayer', app.pdf(), '--work', str(folder)])
            self.assertEqual(argv[-2:], ['--pages', '1-3'])
            out = app.start.call_args.kwargs['open_after']
            self.assertEqual(out.parent, folder.parent / 'out')
            self.assertEqual(out.suffix, '.pdf')
            template.assert_not_called()
            preset.assert_not_called()
            app._prepare_env.assert_not_called()
            app._build_opts.assert_not_called()

    def test_pdf_reuse_accepts_rename_and_rejects_changed_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / 'old.pdf'
            source.write_bytes(b'original')
            work = Work(source, root / 'work')
            work.save_record(1, {'pdf_page': 1})
            renamed = root / 'new.pdf'
            source.rename(renamed)
            args = build_parser().parse_args(['textlayer', str(renamed), '--work', str(work.dir)])
            before = {p.relative_to(work.dir): p.read_bytes() for p in work.dir.rglob('*') if p.is_file()}
            with patch('yeone_ocr.cli.make_textlayer', return_value=0) as make:
                self.assertEqual(cmd_textlayer(args, doc=[object()]), 0)
                make.assert_called_once()
                renamed.write_bytes(b'changed')
                make.reset_mock()
                self.assertEqual(cmd_textlayer(args, doc=[object()]), 2)
                make.assert_not_called()
            after = {p.relative_to(work.dir): p.read_bytes() for p in work.dir.rglob('*') if p.is_file()}
            self.assertEqual(before, after)

    def test_rename_preserves_records_and_dpi_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old = root / 'original.pdf'
            old.write_bytes(b'original PDF contents')
            work = Work(old, root / 'work')
            work.ensure_source(300)
            work.save_record(1, {'pdf_page': 1})
            renamed = root / 'renamed.pdf'
            old.rename(renamed)
            reopened = Work(renamed, work.dir, initialize=False)
            self.assertTrue(reopened.source_matches())
            self.assertFalse(reopened.source_matches(72))
            reopened.ensure_source()
            self.assertEqual(reopened.load_record(1), {'pdf_page': 1})
            self.assertFalse((work.dir / 'archives').exists())
            self.assertEqual(json.loads((work.dir / 'source.json').read_text(encoding='utf-8'))['path'], str(renamed.resolve()))

    def test_changed_bytes_abort_without_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / 'book.pdf'
            source.write_bytes(b'original')
            work = Work(source, root / 'work')
            work.save_record(1, {'pdf_page': 1})
            before = {p.relative_to(work.dir): p.read_bytes() for p in work.dir.rglob('*') if p.is_file()}
            source.write_bytes(b'different')
            args = build_parser().parse_args(['build', str(source), '--work', str(work.dir), '--reuse-records'])
            with patch('yeone_ocr.cli.open_pdf') as opened:
                self.assertEqual(cmd_build(args, doc=object()), 2)
                opened.assert_not_called()
            after = {p.relative_to(work.dir): p.read_bytes() for p in work.dir.rglob('*') if p.is_file()}
            self.assertEqual(before, after)

    def test_reuse_disables_context_model(self):
        args = build_parser().parse_args(['build', 'book.pdf', '--reuse-records'])
        with patch('yeone_ocr.cli.Work') as work, patch('yeone_ocr.cli._check_document', return_value='stop before output'):
            work.return_value.source_matches.return_value = True
            self.assertEqual(cmd_build(args, doc=object()), 2)
            self.assertTrue(args.no_context_model)
