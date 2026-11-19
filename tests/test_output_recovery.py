import copy

import pytest

from yeone_ocr.blocks import Block
from yeone_ocr.document_editing import format_blocks
from yeone_ocr.local_reading import reading_from_run
from yeone_ocr.output_recovery import apply_repairs, digest, quality_report, unexpected_latin_lines


def fixture():
    merged = reading_from_run({'text': 'RE AN\n原文保留', 'image_width': 1000,
                              'image_height': 1000, 'words': []}, 128, 'horizontal').model_dump()
    record = {'pdf_page': 128, 'merged': merged, 'pass1': copy.deepcopy(merged),
              'local_grades': [{'region_id': 'local-line-1', 'start': 0, 'end': 2, 'grade': 'red'},
                               {'region_id': 'local-line-2', 'start': 0, 'end': 2, 'grade': 'blue'}]}
    replacement = copy.deepcopy(merged['regions'][0])
    replacement['bbox'] = [530, 100, 570, 920]
    replacement['paragraphs'][0]['columns'][0]['text'] = '2.《本草汇言》：甘遂，行水气。'
    manifest = {'schema': 1, 'source_sha256': 'a' * 64, 'pages': [
        {'pdf_page': 128, 'merged_sha256': digest(merged), 'changes': [
            {'region_ids': ['local-line-1'], 'regions': [replacement],
             'evidence': {'method': 'source-image-checked', 'image_sha256': 'b' * 64,
                          'note': 'PDF 128쪽 원본 행 대조'}}]}]}
    return record, manifest


def test_repairs_preserve_raw_history_and_unaffected_evidence():
    record, manifest = fixture()
    original = copy.deepcopy(record)
    result = apply_repairs([record], manifest, 'a' * 64)[0]
    assert record == original
    assert result['pass1'] == original['pass1']
    assert result['merged']['regions'][1] == original['merged']['regions'][1]
    assert result['local_grades'] == [original['local_grades'][1]]
    assert result['_output_repairs'] == manifest['pages'][0]


def test_unchanged_tail_keeps_shifted_review_colors():
    record, manifest = fixture()
    record['merged']['regions'][0]['paragraphs'][0]['columns'][0]['text'] = '甘遂原文保留'
    record['local_grades'][0].update(start=3, end=5)
    manifest['pages'][0]['merged_sha256'] = digest(record['merged'])
    region = manifest['pages'][0]['changes'][0]['regions'][0]
    region['paragraphs'][0]['columns'][0]['text'] = '原文保留'
    out = apply_repairs([record], manifest, 'a' * 64)[0]
    grade = next(g for g in out['local_grades'] if g['region_id'] == 'local-line-1')
    assert (grade['start'], grade['end']) == (1, 3)


@pytest.mark.parametrize('failure', ['source', 'edited', 'image', 'order', 'overlap', 'box'])
def test_stale_or_unverified_repairs_fail_without_mutating_history(failure):
    record, manifest = fixture()
    patch = manifest['pages'][0]
    change = patch['changes'][0]
    if failure == 'source': manifest['source_sha256'] = 'c' * 64
    if failure == 'edited': record['merged']['regions'][0]['paragraphs'][0]['columns'][0]['text'] = '人手校正'
    if failure == 'image': change['evidence'] = {}
    if failure == 'order': change['region_ids'] = ['missing']
    if failure == 'overlap': patch['changes'].append(copy.deepcopy(change))
    if failure == 'box': change['regions'][0]['bbox'] = [0, 0, 0, 1000]
    original = copy.deepcopy(record)
    with pytest.raises(ValueError): apply_repairs([record], manifest, 'a' * 64)
    assert record == original


def test_fused_herb_title_is_separate_and_marks_stay_on_body():
    text = '甘遂1.『綱目』: 腎主水。'
    source = Block(type='para', role='quotation', text=text, spans=[(10, 11, 'blue')])
    result, _ = format_blocks([source], titles={'甘遂': ('section', '')})
    assert result[0].text == '甘遂' and result[0].type == 'heading'
    assert result[0].role == 'section' and result[0].layout['alignment'] == 'CENTER'
    assert result[1].text.startswith('1.『綱目』')
    assert result[1].spans == [(8, 9, 'blue')]
    assert source.text == text
    for other in ['甘遂能行水氣。', '甘遂2.『綱目』: 腎主水。', '此方用甘遂1.『綱目』: 腎主水。']:
        out, _ = format_blocks([Block(type='para', text=other)], titles={'甘遂': ('section', '')})
        assert len(out) == 1 and out[0].type == 'para'


def test_quality_detects_latin_citation_gaps_and_pending_despite_reviewed():
    blocks = [Block(type='para', page=128, text='1.『綱目』' + '天地玄黃' * 30),
              Block(type='para', page=128, text='RE AN EN NARSN'),
              Block(type='para', page=128, text='4.『本草崇原』')]
    records = [{'pdf_page': 128, 'local_first': {'review': {'status': 'reviewed'},
                'ai_proposals': [{'decision': 'pending', 'stale': False}]}}]
    report = quality_report(blocks, records)
    assert report['status'] == 'needs-review'
    assert {f['kind'] for f in report['findings']} == {'unexpected-latin', 'citation-gap'}
    assert report['findings'][1]['missing'] == [2, 3]
    assert report['unresolved_history'][0]['execution_status'] == 'reviewed'
    assert unexpected_latin_lines('This is an English page.\nRE AN') == []
    assert unexpected_latin_lines('한글 본문입니다.\nISBN example') == []
