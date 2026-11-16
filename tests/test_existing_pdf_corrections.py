import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from yeone_ocr import cli
from yeone_ocr.comparison import ComparisonProject
from yeone_ocr.local_first import command

REAL_TEXTLAYER = cli.make_textlayer


class ExistingPDFTests(unittest.TestCase):
    def test_hwpx_output_uses_current_correction_and_preserves_project(self):
        from tests.test_local_first import LocalFirstTests
        from yeone_ocr.local_reading import corrected_output_records
        fixture=LocalFirstTests();fixture.setUp();self.addCleanup(fixture.doCleanups)
        with patch.dict(os.environ, {'YEONE_OCR_CLAUDE_AUTH':'api',
             'YEONE_OCR_CONFUSIONS_PATH':str(fixture.root/'isolated-ledger.json')}):
            self.assertEqual(command(fixture.args()),0)
            project=ComparisonProject.load(fixture.record()['local_first']['project_path'])
            project.set_text(1,'天地玄黃校',reason='source image checked');project.save()
            path=Path(fixture.record()['local_first']['project_path'])
            before=path.read_bytes();record=fixture.record()
            view=corrected_output_records([record],str(project.source_path))
            self.assertIn('校',json.dumps(view[0]['merged'],ensure_ascii=False))
            self.assertNotIn('校',json.dumps(record['merged'],ensure_ascii=False))
            self.assertEqual(path.read_bytes(),before)
            self.assertEqual(fixture.calls,[])

    def test_existing_sqlite_book_uses_current_correction_without_ocr_or_record_mutation(self):
        from tests.test_local_first import LocalFirstTests
        fixture=LocalFirstTests();fixture.setUp();self.addCleanup(fixture.doCleanups)
        with patch.dict(os.environ, {'YEONE_OCR_CLAUDE_AUTH':'api',
             'YEONE_OCR_CONFUSIONS_PATH':str(fixture.root/'isolated-ledger.json')}), \
             patch('yeone_ocr.comparison.INCREMENTAL_PAGE_COUNT',1):
            self.assertEqual(command(fixture.args()),0)
            project=ComparisonProject.load(fixture.record()['local_first']['project_path'])
            project.set_text(1,'天地玄黃校',reason='source image checked');project.save()
            record_path=fixture.work/'readings/p001.json';before=record_path.read_bytes()
            original_ocr_calls=len(fixture.local_calls)
            def build(doc,pages,page_data,out,**kwargs):
                reading,image,frame,vertical=page_data(1)
                try:
                    self.assertIn('校',json.dumps(reading.model_dump(),ensure_ascii=False))
                finally:image.close()
                return {'how':{},'chars':5,'pages':1,'stripped':0,'skipped':[],'mismatch':[]}
            with patch.object(cli,'make_textlayer',side_effect=REAL_TEXTLAYER), \
                 patch('yeone_ocr.textlayer.searchable_pdf',side_effect=build):
                self.assertEqual(cli.cmd_run(fixture.args()),0)
            self.assertEqual(record_path.read_bytes(),before)
            self.assertEqual(len(fixture.local_calls),original_ocr_calls)
            self.assertEqual(fixture.calls,[])
