import json
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from xml.etree import ElementTree as ET
from zipfile import ZipFile

import pymupdf
import pytest

from yeone_ocr.rule_generation import (QUICK_SCHEMA, choose_pages, collect_evidence,
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
        if 'sample_types' in kwargs['schema']['properties']:
            pages = json.loads(kwargs['prompt'])['evidence']['pages']
            data['sample_types'] = [{'pdf_page': page['pdf_page'], 'types': ['본문']} for page in pages]
        return json.dumps(data), True, 'stop', {'input': 10}, 'fake'
    return SimpleNamespace(request=Mock(side_effect=answer))


def test_saved_scan_learns_only_repeated_fields_from_this_book():
    from yeone_ocr.rule_layout_scan import saved_ocr_fields
    from yeone_ocr.schema import Column, PageReading, Paragraph, Region

    def record(page, fields):
        regions = [Region(id=str(index), kind=kind, bbox=[top, 100, top + 40, 900],
                          paragraphs=[Paragraph(columns=[Column(col_from_right=1, text=text)])])
                   for index, (kind, top, text) in enumerate(fields)]
        return {'pdf_page': page, 'merged': PageReading(pdf_page=page, regions=regions).model_dump()}

    first = [('본문', 210, '【材料】米'), ('본문', 300, '[주의] 찬물'), ('소주', 810, '〔註釋〕明代'),
             ('본문', 340, '[한 번만] 위')]
    second = [('본문', 190, '[材料]谷'), ('본문', 290, '［주의］ 끓인 물'), ('소주', 830, '[註釋]清代')]
    assert set(saved_ocr_fields([record(1, first), record(2, second)])) == {'材料', '주의'}
    assert saved_ocr_fields([record(1, first)]) == []


def test_sampling_covers_book_and_bookmarks_with_twelve_page_limit():
    with pymupdf.open() as doc:
        for _ in range(100):
            doc.new_page()
        doc.set_toc([[1, 'First', 10], [1, 'Second', 40], [1, 'Third', 70]])
        pages = choose_pages(doc)
    assert len(pages) == 12
    assert {1, 2, 3, 10, 40, 70, 100} <= set(pages)


def test_quick_analysis_scans_all_pages_and_only_requests_one_representative_batch(tmp_path):
    pdf = tmp_path / 'formats.pdf'
    with pymupdf.open() as doc:
        for number in range(1, 49):
            page = doc.new_page()
            if number in (4, 5, 46):
                if number != 5:
                    page.insert_text((40, 40), 'Contents' if number == 4 else 'Index')
                for n in range(9):
                    page.insert_text((40, 70 + n * 20), f'Entry .............. ({n + 1})')
            if 6 <= number <= 45:
                for n in range(7):
                    page.insert_text((40, 40 + n * 20), 'A long line of continuous main text in the book.')
        doc.set_toc([[1, 'Contents', 4], [1, 'Chapter One', 6], [1, 'Index', 46]])
        doc.save(pdf)
    client = client_for()
    result = generate_rule(pdf, client=client)
    report = result['coverage']
    client.request.assert_called_once()
    assert report['local_scanned_pages'] == 48
    assert report['analyzed_pages'] == [4, 5, 6, 26, 46]
    assert len(client.request.call_args.kwargs['images']) == len(report['analyzed_pages'])
    assert result['rule']['page_types']['pages']['목차'] == [4, 5]
    assert result['rule']['page_types']['pages']['색인'] == [46]
    assert not report['whole_book_visually_reviewed']


def test_local_only_does_not_check_subscription_or_claim_unreadable_pages(tmp_path):
    pdf = pdf_file(tmp_path, 20)
    with patch('yeone_ocr.subscriptions.pick') as pick:
        result = generate_rule(pdf, plan='로컬만')
    pick.assert_not_called()
    assert result['coverage']['local_scanned_pages'] == 20
    assert result['coverage']['analyzed_pages'] == []
    assert len(result['coverage']['unresolved_pages']) == 20


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
    assert request['schema'] == QUICK_SCHEMA and len(request['images']) == 1
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
        assert 'PDF 2쪽 로컬 검사 · AI 대표 2쪽 검토' in dialog.preview.get('1.0', 'end')
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


def test_adaptive_pdf_rules_reach_both_outputs_and_do_not_leak_to_other_pdfs(tmp_path):
    from tests._hwpx_fixtures import HP, rec, text_region
    from yeone_ocr import cli

    pdf = tmp_path / 'instructions.pdf'
    sentences = ['Main paragraph. [Materials] Boards and nails. [Safety] Protect your eyes.',
                 'Next page starts here.']
    notes = ['① First note', '② Second note']
    with pymupdf.open() as doc:
        first = doc.new_page(width=420, height=600)
        first.insert_text((35, 85), sentences[0], fontsize=10)
        doc.new_page(width=420, height=600).insert_text((35, 85), sentences[1], fontsize=10)
        doc.save(pdf)

    work = Work(pdf, tmp_path / 'work')
    for number, sentence in enumerate(sentences, 1):
        regions = [text_region('body', [sentence], box=(.08, .12, .90, .15))]
        if number == 1:
            regions.extend(text_region(f'note-{n}', [note], box=(.08, y, .62, y+.018))
                           for n, (note, y) in enumerate(zip(notes, (.84, .88))))
        work.save_record(number, dict(rec(number, regions), mode='horizontal_zh'))
    analyst = client_for(response({'schema': 1, 'field_labels': ['Materials', 'Safety']}))
    result = generate_rule(pdf, work_dir=work.dir, client=analyst)
    rule = save_generated(result, tmp_path / 'rules')
    assert load_preset(rule).field_labels == ('Materials', 'Safety')
    assert cli.main(['build', str(pdf), '--work', str(work.dir), '--layout', 'flow',
                     '--preset', str(rule), '--no-normalize', '--textlayer-pdf', '--out', 'adapted']) == 0
    with ZipFile(work.out_dir / 'adapted.hwpx') as archive:
        section = ET.fromstring(archive.read('Contents/section0.xml'))
    paragraphs = [''.join(t.text or '' for t in row.iter(HP + 't')) for row in section.findall(HP + 'p')]
    assert '[Materials] Boards and nails.' in paragraphs
    assert '[Safety] Protect your eyes.' in paragraphs
    boundary = next(i for i, para in enumerate(paragraphs) if para == '원본 PDF 2쪽 · 본문 시작')
    assert paragraphs[boundary - 1] == notes[1]
    assert 'PDF 2쪽' in paragraphs[boundary] and paragraphs[boundary + 1] == sentences[1]
    assert abs(int(section.find('.//' + HP + 'pagePr').get('width')) - 42000) < 50
    with pymupdf.open(work.out_dir / 'adapted.pdf') as searchable, pymupdf.open(pdf) as original:
        assert searchable[0].get_pixmap().samples == original[0].get_pixmap().samples
        assert 'Materials' in searchable[0].get_text()
    other = pdf_file(tmp_path)
    args = cli.build_parser().parse_args(['build', str(other), '--layout', 'flow', '--preset', str(rule)])
    assert '현재 PDF가 다릅니다' in cli._check_document(args)


def test_adaptive_style_mapping_uses_template_properties_and_exact_names(tmp_path):
    from tests._hwpx_fixtures import HH, HP, make_template
    from yeone_ocr.book_output import preview_document

    pdf = pdf_file(tmp_path)
    template = tmp_path / 'styles.hwpx'
    make_template(template)
    client = client_for(response({'schema': 1, 'headings': {'section': ['Chapter 1', 'Chapter 2']}, 'styles': {
        'paragraph': {'section': ['없는 스타일', '제목-절']},
        'character': {'source': ['주석', '출처']},
    }}))
    result = generate_rule(pdf, template, client=client)
    styles = result['rule']['styles']
    assert styles['paragraph']['section'] == ['제목-절']
    assert styles['character']['source'] == ['출처']
    supplied = json.loads(client.request.call_args.kwargs['prompt'])['evidence']['template']['styles']
    title_style = next(style for style in supplied if style['name'] == '제목-절')
    assert title_style['size_pt'] == 16 and title_style['alignment'] == 'CENTER'
    assert any('없는 스타일' in item for item in result['warnings'])
    output, _, _ = preview_document(pdf, tmp_path / 'empty', ['--no-normalize'], analysis=result)
    try:
        with ZipFile(output) as archive:
            header = ET.fromstring(archive.read('Contents/header.xml'))
            section = ET.fromstring(archive.read('Contents/section0.xml'))
        style_id = next(style.get('id') for style in header.iter(HH + 'style') if style.get('name') == '제목-절')
        title = next(row for row in section.findall(HP + 'p')
                     if ''.join(t.text or '' for t in row.iter(HP + 't')) == 'Chapter 1')
        assert title.get('styleIDRef') == style_id
    finally:
        output.unlink()


def test_analyze_and_build_persists_book_settings_before_starting_normal_ocr(tmp_path):
    import tkinter as tk
    from yeone_ocr.gui import MODE_LABELS
    from yeone_ocr.gui_rule_generation import BookRuleDialog
    from yeone_ocr.hwpx_output_rules import CONVERSION_LABELS

    pdf = pdf_file(tmp_path)
    app = tk.Tk()
    app.withdraw()
    app.cfg = {}
    app.running = False
    app.v_template = tk.StringVar(master=app)
    app.v_bold = tk.BooleanVar(master=app, value=True)
    app._mode_vars = {0: tk.StringVar(master=app, value=MODE_LABELS['vertical'])}
    app._input_vars = {0: {'pdf': tk.StringVar(master=app, value=str(pdf))}}
    calls = Mock()
    app.use_book_rule = calls.apply
    app._save_book = calls.save
    app.do_run = calls.run
    dialog = BookRuleDialog(app, str(pdf), tmp_path / 'work')
    try:
        dialog.conversion_rules['hwpx_convert_punctuation'].set(CONVERSION_LABELS['off'])
        result = generate_rule(pdf, client=client_for())
        dialog.build_after_analysis = True
        dialog.events.put(('done', result))
        with patch('yeone_ocr.gui_rule_generation.rules_dir', return_value=tmp_path / 'rules'):
            dialog.poll()
        assert [call[0] for call in calls.mock_calls] == ['apply', 'save', 'run']
        assert app._book_textlayer_pdf is True
        assert app._book_source_review is True
        assert app._book_hwpx_bookmarks == 'headings'
        assert app._book_conversion_rules['hwpx_convert_punctuation'] == 'off'
        assert app._mode_vars[0].get() == MODE_LABELS['auto']
        assert not app.v_bold.get()
        assert not dialog.winfo_exists()
    finally:
        if dialog.winfo_exists():
            dialog.close()
        app.destroy()
