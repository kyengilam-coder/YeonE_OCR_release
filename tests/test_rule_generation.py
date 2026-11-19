import json
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pymupdf
import pytest

from yeone_ocr.rule_generation import (SCHEMA, choose_pages, collect_evidence,
                                       generate_rule, save_generated, validate_response)
from yeone_ocr.presets import load_preset
from yeone_ocr.state import Work


def pdf_file(tmp_path, count=2):
    path = tmp_path / 'book.pdf'
    with pymupdf.open() as doc:
        for i in range(count):
            page = doc.new_page()
            page.insert_text((50, 50), f'Chapter {i + 1}')
        doc.save(path)
    return path


def response(rule=None):
    return json.dumps({'rule_json': json.dumps(rule or {'schema': 1}),
                       'summary': '대표 쪽의 제목과 본문을 분석했습니다.',
                       'warnings': ['각주 규칙은 확인이 필요합니다.']})


def client_for(text=None):
    def answer(**kwargs):
        data = json.loads(text or response())
        if 'types' in kwargs['schema']['properties']:
            data['types'] = ['본문']
        return json.dumps(data), True, 'stop', {'input': 10}, 'fake'
    return SimpleNamespace(request=Mock(side_effect=answer))


def test_sampling_covers_book_and_bookmarks_with_twelve_page_limit():
    with pymupdf.open() as doc:
        for _ in range(100):
            doc.new_page()
        doc.set_toc([[1, 'First', 10], [1, 'Second', 40], [1, 'Third', 70]])
        pages = choose_pages(doc)
    assert len(pages) == 12
    assert {1, 2, 3, 10, 40, 70, 100} <= set(pages)


def test_matching_ocr_is_read_without_changes_and_mismatched_ocr_is_excluded(tmp_path):
    pdf = pdf_file(tmp_path)
    work = Work(pdf, tmp_path / 'old')
    work.save_record(1, {'merged': {'text': 'old OCR'}})
    before = {p.name: p.read_bytes() for p in work.dir.rglob('*') if p.is_file()}
    images, evidence = collect_evidence(pdf, work_dir=work.dir)
    assert len(images) == 2 and images[0].startswith(b'\x89PNG')
    assert 'old OCR' in evidence['pages'][0]['ocr_reference']
    assert evidence['coverage']['selected_pages'] == [1, 2]
    assert before == {p.name: p.read_bytes() for p in work.dir.rglob('*') if p.is_file()}
    manifest = json.loads((work.dir / 'source.json').read_text())
    manifest['sha256'] = '0' * 64
    (work.dir / 'source.json').write_text(json.dumps(manifest))
    _, evidence = collect_evidence(pdf, work_dir=work.dir)
    assert not evidence['coverage']['ocr_source_matches']
    assert 'ocr_reference' not in evidence['pages'][0]
    assert not (work.dir / 'archives').exists()


def test_generation_validates_and_preserves_original_and_existing_rule(tmp_path):
    pdf = pdf_file(tmp_path)
    before = pdf.read_bytes()
    client = client_for(response({'schema': 1, 'template': 'untrusted.hwpx', 'headings': {'chapter': ['Chapter 1']}}))
    result = generate_rule(pdf, pages='1', client=client)
    assert pdf.read_bytes() == before
    assert result['rule']['template'] == ''
    assert result['coverage']['selected_pages'] == [1]
    assert not result['coverage']['whole_book_visually_reviewed']
    request = client.request.call_args.kwargs
    assert request['schema'] == SCHEMA and len(request['images']) == 1
    assert 'untrusted evidence' in request['system']
    first = save_generated(result, tmp_path / 'rules')
    content = first.read_bytes()
    second = save_generated(result, tmp_path / 'rules')
    assert first != second and first.read_bytes() == content
    assert list(load_preset(second).headings['chapter']) == ['Chapter 1']
    assert (second.parent / 'generation_reports' / second.name).is_file()


@pytest.mark.parametrize('rule', [{'schema': 1, 'script': 'run code'}, {'schema': 1, 'headings': {'section': 'bad'}},
                                 {'schema': 1, 'citation': {'mode': 'invented'}}])
def test_invalid_ai_rule_is_not_saved(rule):
    with pytest.raises(ValueError):
        validate_response(response(rule), 'book.pdf', '')


def test_cancel_and_bad_inputs_do_not_call_ai(tmp_path):
    pdf = pdf_file(tmp_path)
    client = client_for()
    with pytest.raises(InterruptedError):
        generate_rule(pdf, client=client, stop=lambda: True)
    with pytest.raises(ValueError):
        generate_rule(pdf, pages='1-20', client=client)
    with pytest.raises((OSError, ValueError)):
        generate_rule(pdf, template=tmp_path / 'missing.hwpx', client=client)
    client.request.assert_not_called()


def test_cancel_after_response_prevents_result(tmp_path):
    pdf = pdf_file(tmp_path)
    stop = threading.Event()
    client = client_for()
    def answer(**kwargs):
        stop.set()
        data = json.loads(response()); data['types'] = ['본문']
        return json.dumps(data), True, 'stop', {}, 'fake'
    client.request.side_effect = answer
    with pytest.raises(InterruptedError):
        generate_rule(pdf, client=client, stop=stop.is_set)


def test_subscription_has_no_paid_api_fallback(tmp_path):
    pdf = pdf_file(tmp_path)
    client = client_for()
    with patch('yeone_ocr.subscriptions.pick', return_value=('chatgpt', '')), \
         patch('yeone_ocr.subscriptions.Session', return_value=client) as session:
        generate_rule(pdf, plan='chatgpt')
    assert session.call_args.kwargs['api'] is None


def test_dialog_previews_without_applying_and_requires_current_book(tmp_path):
    import tkinter as tk
    from yeone_ocr.gui_rule_generation import BookRuleDialog
    pdf = pdf_file(tmp_path)
    app = tk.Tk(); app.withdraw()
    app.v_template = tk.StringVar(master=app)
    app._input_vars = {0: {'pdf': tk.StringVar(master=app, value=str(pdf))}}
    app.cfg = {}; app.running = False
    app.use_book_rule = Mock(); app.make_book_rule_manual = Mock()
    dialog = BookRuleDialog(app, str(pdf), tmp_path / 'work')
    try:
        result = generate_rule(pdf, client=client_for())
        dialog.events.put(('done', result)); dialog.poll()
        assert '전체 2쪽' in dialog.preview.get('1.0', 'end')
        app.use_book_rule.assert_not_called()
        with patch('yeone_ocr.gui_rule_generation.rules_dir', return_value=tmp_path / 'rules'):
            dialog.apply_result()
        app.use_book_rule.assert_called_once()
        assert load_preset(app.use_book_rule.call_args.args[0]).label.endswith('AI 규칙')
        assert dialog.result is None
    finally:
        dialog.close(); app.destroy()


def test_source_modified_after_preview_cannot_be_saved(tmp_path):
    pdf = pdf_file(tmp_path)
    result = generate_rule(pdf, client=client_for())
    pdf.write_bytes(b'changed PDF')
    with pytest.raises(ValueError, match='변경'):
        save_generated(result, tmp_path / 'rules')
    assert not (tmp_path / 'rules').exists()
