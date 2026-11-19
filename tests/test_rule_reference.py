import json
from unittest.mock import Mock, patch

import pymupdf
import pytest

from tests.test_reference_ocr import make_reference
from tests.test_rule_generation import client_for
from yeone_ocr.rule_generation import collect_evidence, generate_rule, save_generated


def fixture(tmp_path):
    import random
    rng = random.Random(123)
    text = ' '.join(''.join(rng.choices('abcdefghijklmnopqrstuvwxyz', k=12)) for _ in range(40))
    pdf = tmp_path / 'book.pdf'
    with pymupdf.open() as doc:
        page = doc.new_page()
        page.insert_textbox(pymupdf.Rect(30, 30, 550, 700), text, fontname='helv', fontsize=10)
        doc.save(pdf)
    ref = make_reference(tmp_path / 'reference.hwpx', '原文' + text + '末尾')
    return pdf, ref


def test_only_aligned_reference_passage_is_sent_with_image_evidence(tmp_path):
    pdf, ref = fixture(tmp_path)
    before = ref.path.read_bytes()
    images, evidence = collect_evidence(pdf, pages='1', reference_ocr=ref)
    passage = evidence['pages'][0]['reference_hwpx']
    assert passage['normalized_alignment_coverage'] >= .8
    assert passage['document_sha256'] == ref.sha256
    assert 'advisory' in passage['role'] and images
    assert ref.path.read_bytes() == before
    other = make_reference(tmp_path / 'other.hwpx', 'Completely unrelated text.')
    _, evidence = collect_evidence(pdf, pages='1', reference_ocr=other)
    assert evidence['pages'][0]['reference_hwpx_status'] == 'not_aligned'
    assert 'reference_hwpx' not in evidence['pages'][0]


def test_reference_changes_invalidate_cache_and_prevent_stale_apply(tmp_path):
    pdf, ref = fixture(tmp_path)
    client = client_for()
    result = generate_rule(pdf, client=client, reference_hwpx=ref.path, cache_dir=tmp_path / 'cache')
    assert result['coverage']['reference_aligned_pages'] == [1]
    prompt = json.loads(client.request.call_args.kwargs['prompt'])
    assert 'reference_hwpx' in prompt['evidence']['pages'][0]
    client.request.reset_mock()
    generate_rule(pdf, client=client, reference_hwpx=ref.path, cache_dir=tmp_path / 'cache')
    client.request.assert_not_called()
    make_reference(ref.path, ref.text + '新增')
    with pytest.raises(ValueError, match='변경'):
        save_generated(result, tmp_path / 'rules')
    generate_rule(pdf, client=client, reference_hwpx=ref.path, cache_dir=tmp_path / 'cache')
    client.request.assert_called_once()


def test_missing_reference_fails_before_ai_call(tmp_path):
    pdf, _ = fixture(tmp_path)
    client = client_for()
    with pytest.raises(OSError):
        generate_rule(pdf, reference_hwpx=tmp_path / 'missing.hwpx', client=client)
    client.request.assert_not_called()


def test_book_setup_saves_reference_and_template_together_and_rejects_switched_book(tmp_path):
    import tkinter as tk
    from yeone_ocr.gui_rule_generation import BookRuleDialog
    pdf, ref = fixture(tmp_path)
    app = tk.Tk(); app.withdraw()
    app.cfg = {}; app.running = False
    app.v_template = tk.StringVar(master=app)
    app.v_reference_hwpx = tk.StringVar(master=app, value=str(ref.path))
    app._input_vars = {0: {'pdf': tk.StringVar(master=app, value=str(pdf))}}
    app._save_book = Mock(); app.use_book_rule = Mock(); app.make_book_rule_manual = Mock()
    dialog = BookRuleDialog(app, str(pdf), tmp_path / 'work')
    try:
        assert dialog.reference.get() == str(ref.path)
        assert dialog.template is app.v_template
        assert dialog.reference is app.v_reference_hwpx
        dialog.template.set('output-template.hwpx')
        with patch('yeone_ocr.rule_request.template_summary', return_value={}):
            assert dialog.save_selection()
        assert app.v_template.get() == 'output-template.hwpx'
        assert app.v_reference_hwpx.get() == str(ref.path)
        app._save_book.assert_called_once_with(create=True)
        app._save_book.return_value = False
        with patch('yeone_ocr.rule_request.template_summary', return_value={}):
            assert not dialog.save_selection()
        app._save_book.return_value = True
        dialog.result = {'preview': 'old'}
        app.v_reference_hwpx.set('')
        assert dialog.result is None
        assert dialog.reference.get() == ''
        dialog.analysis_selection = ('old-template.hwpx', '', '')
        dialog.set_busy(True)
        dialog.events.put(('done', {'stale': True}))
        dialog.poll()
        assert dialog.result is None and not dialog.busy
        assert '변경' in dialog.status.get()
        app._input_vars[0]['pdf'].set('another.pdf')
        assert not dialog.winfo_exists()
        app.v_reference_hwpx.set(str(ref.path))  # closed window has no dangling trace
        app._input_vars[0]['pdf'].set(str(pdf))
        app.open_book_settings = Mock()
        dialog = BookRuleDialog(app, str(pdf), tmp_path / 'work')
        with patch('yeone_ocr.rule_request.template_summary', return_value={}):
            dialog.open_settings()
        app.open_book_settings.assert_called_once()
        assert not dialog.winfo_exists()
    finally:
        if dialog.winfo_exists():
            dialog.close()
        app.destroy()
