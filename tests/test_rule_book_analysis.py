import json
import threading
from functools import partial
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from tests.test_rule_generation import pdf_file
from yeone_ocr.rule_book_analysis import _review_contents_type, merge_findings
from yeone_ocr.rule_generation import generate_rule as _generate_rule

generate_rule = partial(_generate_rule, deep_analysis=True)


def page_client(stop=None, fail_page=None):
    seen = []
    def request(**kwargs):
        assert len(kwargs['images']) == 5  # whole page + four detailed quadrants
        evidence = json.loads(kwargs['prompt'])['evidence']
        n = evidence['pages'][0]['pdf_page']
        seen.append(n)
        if n == fail_page:
            return '{}', False, 'failed', {}, 'fake'
        if stop is not None and n == 3:
            stop.set()
        types = ['본문']
        if n == 13:
            types = ['본문', '각주']
        if n == 16:
            types = ['색인']
        if n == 17:
            types = ['문헌 목록']
        if n == 18:
            types = ['목차']
        rule = {'schema': 1, 'headings': {'section': [f'제목 {n}']}}
        return json.dumps({'rule_json': json.dumps(rule), 'types': types,
                           'summary': f'{n}쪽 구조', 'warnings': []}), True, 'stop', {'input': 1}, 'fake'
    return SimpleNamespace(request=Mock(side_effect=request)), seen


def test_every_page_and_rare_types_beyond_twelve_reach_final_rule(tmp_path):
    pdf = pdf_file(tmp_path, 19)
    client, seen = page_client()
    result = generate_rule(pdf, client=client, cache_dir=tmp_path / 'cache')
    assert seen == list(range(1, 20))
    assert result['type_pages']['각주'] == [13]
    assert result['type_pages']['문헌 목록'] == [17]
    profile = result['rule']['page_types']
    assert profile['source_sha256'] == result['inputs']['pdf_sha256']
    assert profile['pages']['색인'] == [16] and profile['pages']['목차'] == [18]
    assert len(result['page_findings']) == 19
    assert len(result['rule']['headings']['section']) == 19
    assert result['coverage']['whole_book_visually_reviewed']
    assert any('각주 번호' in warning for warning in result['warnings'])
    cached_client, seen = page_client()
    result2 = generate_rule(pdf, client=cached_client, cache_dir=tmp_path / 'cache')
    assert seen == [] and result2['coverage']['cached_pages'] == 19


def test_cancellation_keeps_completed_response_and_resumes_without_skipping(tmp_path):
    pdf = pdf_file(tmp_path, 5)
    stop = threading.Event()
    client, seen = page_client(stop)
    with pytest.raises(InterruptedError):
        generate_rule(pdf, client=client, stop=stop.is_set, cache_dir=tmp_path / 'cache')
    assert seen == [1, 2, 3]
    client, seen = page_client()
    result = generate_rule(pdf, client=client, cache_dir=tmp_path / 'cache')
    assert seen == [4, 5]
    assert len(result['page_findings']) == 5


def test_failure_never_produces_a_complete_rule_and_retry_fills_gap(tmp_path):
    pdf = pdf_file(tmp_path, 4)
    client, seen = page_client(fail_page=2)
    with pytest.raises(ValueError, match='2/4'):
        generate_rule(pdf, client=client, cache_dir=tmp_path / 'cache')
    assert seen == [1, 2]
    progress = json.loads(next((tmp_path / 'cache').rglob('progress.json')).read_text())
    assert progress['completed_pages'] == [1]
    assert progress['pending_pages'] == [2, 3, 4]
    client, seen = page_client()
    generate_rule(pdf, client=client, cache_dir=tmp_path / 'cache')
    assert seen == [2, 3, 4]


def test_unsupported_other_paragraph_mapping_does_not_interrupt_book_analysis(tmp_path):
    pdf = pdf_file(tmp_path, 3)
    client, seen = page_client(fail_page=2)
    cache = tmp_path / 'cache'
    with pytest.raises(ValueError, match='2/3'):
        generate_rule(pdf, client=client, cache_dir=cache)

    client, seen = page_client()
    request = client.request.side_effect

    def with_other(**kwargs):
        text, *metadata = request(**kwargs)
        payload = json.loads(text)
        rule = json.loads(payload['rule_json'])
        paragraph = {'other': ['기타']}
        if seen[-1] == 2:
            paragraph['body'] = ['본문']
        rule['styles'] = {'paragraph': paragraph}
        payload['rule_json'] = json.dumps(rule)
        return json.dumps(payload), *metadata

    client.request.side_effect = with_other
    result = generate_rule(pdf, client=client, cache_dir=cache)
    assert seen == [2, 3]
    assert result['coverage']['cached_pages'] == 1
    assert result['rule']['styles'] == {'paragraph': {'body': ['본문']}}
    assert any('PDF 2쪽:' in warning and 'other' in warning for warning in result['warnings'])


def test_conflicting_rules_and_title_levels_are_retained_for_review():
    findings = [{'pdf_page': 1, 'rule': {'citation': {'mode': 'separate', 'max_title': 40},
                                      'headings': {'chapter': ['같은 제목']}}},
                {'pdf_page': 2, 'rule': {'citation': {'mode': 'separate', 'max_title': 80},
                                      'headings': {'section': ['같은 제목', '다른 제목']}}}]
    rule, warnings, conflicts = merge_findings(findings, 'book.pdf', '')
    assert 'citation' not in rule
    assert rule['headings']['chapter'] == []
    assert rule['headings']['section'] == ['다른 제목']
    assert len(conflicts) == 2 and len(warnings) == 2
    assert conflicts[0]['proposals'][1]['value']['max_title'] == 80


def test_unreadable_page_is_visible_in_final_coverage(tmp_path):
    pdf = pdf_file(tmp_path, 1)
    payload = json.dumps({'rule_json': '{"schema":1}', 'types': ['판단 불가'],
                          'summary': '해독 불가', 'warnings': ['글씨가 너무 흐립니다.']})
    client = SimpleNamespace(request=Mock(return_value=(payload, True, 'stop', {}, 'fake')))
    result = generate_rule(pdf, client=client)
    assert result['coverage']['unresolved_pages'] == [1]
    assert any('판단 불가 쪽' in message for message in result['warnings'])


def test_invalid_json_retries_same_page_and_keeps_distinct_page_types(tmp_path):
    pdf = pdf_file(tmp_path, 4)
    types = ('차례', '목차', '색인', '본문')
    seen = []

    def request(**kwargs):
        assert set(types) <= set(kwargs['schema']['properties']['types']['items']['enum'])
        number = json.loads(kwargs['prompt'])['evidence']['pages'][0]['pdf_page']
        seen.append(number)
        if seen == [1]:
            return '', False, 'invalid_json', {'input': 1}, 'fake'
        payload = json.dumps({'rule_json': '{}' if number == 2 else '{"schema":1}', 'summary': '', 'warnings': [],
                              'types': [types[number - 1]]})
        return payload, True, 'stop', {'input': 1}, 'fake'

    result = generate_rule(pdf, client=SimpleNamespace(request=request))
    assert seen == [1, 1, 2, 3, 4]
    assert result['rule']['page_types']['pages'] == {kind: [n] for n, kind in enumerate(types, 1)}
    assert result['coverage']['new_requests'] == result['usage']['input'] == 5
    assert any('PDF 2쪽:' in warning and '스키마' in warning for warning in result['warnings'])


def test_contents_with_footnotes_are_not_counted_as_body():
    listing = '\n'.join(f'항목 ................. ({n})' for n in range(1, 10))
    listing += '\n〔1］ 이 목록 항목의 원문 표기에 관한 주석입니다。'
    for tail, expected in (('', ['목차', '각주']), ('\n여기부터 본문이 시작된다。', ['목차', '본문', '각주'])):
        answer = {'types': ['목차', '본문', '각주'], 'warnings': []}
        _review_contents_type(answer, {'pdf_text': listing + tail})
        assert answer['types'] == expected


def test_corrupt_cache_is_reanalyzed_without_losing_other_pages(tmp_path):
    pdf = pdf_file(tmp_path, 2)
    client, _ = page_client()
    generate_rule(pdf, client=client, cache_dir=tmp_path / 'cache')
    caches = [p for p in (tmp_path / 'cache').rglob('*.json') if p.name != 'progress.json']
    caches[0].write_text('{broken')
    client, seen = page_client()
    result = generate_rule(pdf, client=client, cache_dir=tmp_path / 'cache')
    assert len(seen) == 1 and len(result['page_findings']) == 2


def test_changed_ocr_invalidates_only_affected_page_cache(tmp_path):
    from yeone_ocr.state import Work
    pdf = pdf_file(tmp_path, 2)
    work = Work(pdf, tmp_path / 'work')
    work.save_record(1, {'merged': {'text': 'first'}})
    client, _ = page_client()
    generate_rule(pdf, work_dir=work.dir, client=client, cache_dir=tmp_path / 'cache')
    work.save_record(1, {'merged': {'text': 'corrected'}})
    client, seen = page_client()
    generate_rule(pdf, work_dir=work.dir, client=client, cache_dir=tmp_path / 'cache')
    assert seen == [1]
