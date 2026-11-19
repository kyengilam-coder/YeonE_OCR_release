from pathlib import Path
from zipfile import ZipFile
from xml.sax.saxutils import escape
import hashlib

from yeone_ocr.reference_ocr import ReferenceOCR
from yeone_ocr.comparison_ai import ReviewPolicy, prepare_plan, review_window


def make_reference(path, text):
    with ZipFile(path, 'w') as package:
        package.writestr('Contents/section0.xml', '<s xmlns:hp="http://www.hancom.co.kr/hwpml/2011/paragraph"><hp:p><hp:run><hp:t>'+escape(text)+'</hp:t></hp:run></hp:p></s>')
    return ReferenceOCR(path)


def test_reference_alignment_preserves_original_and_ignores_unmatched(tmp_path):
    text = ''.join(chr(0x4e00+i) for i in range(400))
    path = tmp_path/'previous.hwpx'
    ref = make_reference(path, '原文'+text+'末尾')
    before = path.read_bytes()
    hint = ref.hint({'snapshot_text':text, 'targets':[{'id':'a', 'start':100}]})
    assert hint['normalized_alignment_coverage']==1
    assert hint['image_checks_skipped']==0 and hint['excerpts'][0]['target_id']=='a'
    assert hint['document_sha256']==hashlib.sha256(before).hexdigest()
    assert ref.hint({'snapshot_text':'unrelated text', 'targets':[]}) is None
    assert path.read_bytes()==before


def test_adding_reference_keeps_finished_image_reviews_and_batch_identity(tmp_path):
    import test_comparison_ai
    fixture = test_comparison_ai.PipelineTests()
    fixture.setUp()
    try:
        text = ''.join(chr(0x4e00+i) for i in range(400))
        fixture.project = fixture.new_project({'tesseract':text, 'glmocr':text[:100]+'錯'+text[101:]})
        ref = make_reference(tmp_path/'previous.hwpx', text)
        policy = ReviewPolicy(chunk_chars=50, batch_size=1)
        original = prepare_plan(fixture.project,[1],policy,settings=fixture.settings)
        contextual = prepare_plan(fixture.project,[1],policy,settings=fixture.settings,reference_ocr=ref)
        assert original.fingerprint==contextual.fingerprint
        assert [b['batch_id'] for b in original.batches]==[b['batch_id'] for b in contextual.batches]
        assert contextual.summary['reference_ocr']['matched_batches']>0
        assert original.summary['calls']==contextual.summary['calls']
        fixture.run_fake(review_window(original,1))
        resumed = prepare_plan(fixture.project,[1],policy,settings=fixture.settings,reference_ocr=ref)
        assert resumed.summary['resumed_batches']>=1
        assert original.batches[0]['batch_id'] not in [b['batch_id'] for b in resumed.batches]
    finally:
        fixture.doCleanups()


def test_reference_choice_is_book_scoped_and_used_in_local_strategy():
    import test_book_settings
    from test_gui_settings_merge import Var
    fixture = test_book_settings.Window()
    fixture.setUp()
    try:
        app = fixture.app()
        app.v_reference_hwpx = Var('')
        app._use_pdf(fixture.a)
        app.v_reference_hwpx.set('book-a.hwpx')
        app._save_book(create=True)
        app._use_pdf(fixture.b)
        assert app.v_reference_hwpx.get()==''
        app._use_pdf(fixture.a)
        assert app.v_reference_hwpx.get()=='book-a.hwpx'
        app.cfg['ocr_strategy']='local-first'
        app.v_ocr_strategy = Var(fixture.gui.OCR_STRATEGY_LABELS['local-first'])
        opts = fixture.gui.App._strategy_opts(app)
        assert opts[opts.index('--local-reference-hwpx')+1]=='book-a.hwpx'
    finally:
        fixture.doCleanups()
