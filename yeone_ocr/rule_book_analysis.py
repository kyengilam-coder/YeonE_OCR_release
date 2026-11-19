"""Streaming, resumable whole-book rule discovery, with page-level evidence."""
import copy
import hashlib
import json
from pathlib import Path

from .presets import DATA, PAGE_TYPES as TYPES, _compile

VERSION = 4


def page_schema():
    from .rule_generation import SCHEMA
    schema = copy.deepcopy(SCHEMA)
    schema['properties']['types'] = {
        'type': 'array', 'items': {'type': 'string', 'enum': list(TYPES)},
        'description': ('Use 차례 for a contents section explicitly titled 차례 and its identifiable continuation pages. '
                        'Use 목차 for other contents listings, including 目錄 and 總目. '
                        'Use 색인 for lookup entries pointing to locations, and 본문 for the main text. '
                        'Use both types only when separate structures actually coexist on this page.'),
    }
    schema['required'].append('types')
    return schema


def validate_page(text, pdf, template):
    from .rule_generation import validate_response
    if not isinstance(text, str) or len(text) > 500_000:
        raise ValueError('쪽 분석 응답 크기가 올바르지 않습니다.')
    raw = json.loads(text)
    if not isinstance(raw, dict):
        raise ValueError('쪽 분석 응답은 JSON 객체여야 합니다.')
    kinds = raw.pop('types', None)
    if not isinstance(kinds, list) or not kinds or any(k not in TYPES for k in kinds):
        raise ValueError('쪽 유형을 확인하지 못했습니다.')
    rule, summary, warnings = validate_response(json.dumps(raw, ensure_ascii=False), pdf, template)
    return {'rule': rule, 'summary': summary, 'warnings': warnings, 'types': list(dict.fromkeys(kinds))}


def _merge_values(left, right):
    if left == right:
        return copy.deepcopy(left)
    if isinstance(left, dict) and isinstance(right, dict):
        result = copy.deepcopy(left)
        for key, value in right.items():
            result[key] = _merge_values(result[key], value) if key in result else copy.deepcopy(value)
        return result
    if isinstance(left, list) and isinstance(right, list):
        return list(dict.fromkeys(left + right))
    raise ValueError('서로 다른 규칙 값')


def merge_findings(findings, pdf, template):
    """No summary sampling: every page proposal participates, conflicts remain visible."""
    from .rule_generation import validate_response
    data = {'schema': 1}
    warnings, conflicts = [], []
    for feature in ('entry', 'commentary', 'source', 'emphasis', 'styles', 'citation', 'headings'):
        proposals = [(item['pdf_page'], item['rule'][feature]) for item in findings
                     if item['rule'].get(feature) is not None]
        if not proposals:
            continue
        try:
            merged = copy.deepcopy(proposals[0][1])
            for _, value in proposals[1:]:
                # Style fallback order carries meaning; don't union conflicting priorities.
                if feature == 'styles' and value != merged:
                    raise ValueError('스타일 연결 충돌')
                merged = _merge_values(merged, value)
            data[feature] = merged
        except ValueError:
            conflicts.append({'feature': feature, 'pages': [n for n, _ in proposals],
                              'proposals': [{'pdf_page': n, 'value': v} for n, v in proposals]})
            warnings.append(f'{feature}: 쪽별 규칙이 충돌하여 자동 적용에서 제외했습니다. 분석 보고서의 후보를 확인하십시오.')
    headings = data.get('headings', {})
    owners = {}
    for role, titles in headings.items():
        for title in titles:
            owners.setdefault(title, set()).add(role)
    disputed = {title for title, roles in owners.items() if len(roles) > 1}
    if disputed:
        for role in headings:
            headings[role] = [title for title in headings[role] if title not in disputed]
        conflicts.append({'feature': 'headings', 'titles': sorted(disputed)})
        warnings.append('여러 제목 단계로 판별된 제목은 자동 규칙에서 제외했습니다. 전체 후보는 쪽별 기록에 보존했습니다.')
    # Schema limits are explicit review items, never silently truncate the book.
    for role, titles in list(headings.items()):
        if len(titles) > 3000:
            del headings[role]
            conflicts.append({'feature': 'headings.' + role, 'count': len(titles)})
            warnings.append(f'headings.{role}: 제목 {len(titles)}개가 지원 한도 3000개를 넘어 적용을 보류했습니다.')
    rule, _, _ = validate_response(json.dumps({'rule_json': json.dumps(data), 'summary': '', 'warnings': []}), pdf, template)
    _compile(rule, '전체 책 규칙: ', 'generated.json', 'generated')
    return rule, warnings, conflicts


def analyze_book(pdf, template, work_dir, client, model, inputs, *, stop, progress, cache_dir=None, reference_ocr=None):
    import pymupdf
    from .rule_generation import SYSTEM, _check_stop, collect_evidence, verify_inputs
    from .state import write_atomic
    examples = {name: json.loads((DATA / name).read_text(encoding='utf-8'))
                for name in ('preset_basic.json', 'book_rule_kaibao.json', 'book_rule_yaolun.json')}
    for example in examples.values():
        example['template'] = ''
    with pymupdf.open(pdf) as doc:
        count = len(doc)
    if count < 1:
        raise ValueError('PDF에 쪽이 없습니다.')
    findings, usage = [], {}
    cached = calls = 0
    cache = Path(cache_dir) if cache_dir else None
    if cache:
        run_key = hashlib.sha256(json.dumps([VERSION, inputs, model], sort_keys=True).encode()).hexdigest()
        # Leave room for Windows user directories and atomic-save suffixes.
        cache = cache / run_key[:24]
        cache.mkdir(parents=True, exist_ok=True)
    def checkpoint(current, complete=False):
        if cache:
            completed = [item['pdf_page'] for item in findings]
            write_atomic(cache / 'progress.json', json.dumps({
                'page_count': count, 'completed_pages': completed,
                'pending_pages': [n for n in range(1, count + 1) if n not in completed],
                'current_page': current, 'phase': 'analysis_complete' if complete else 'pending_or_interrupted',
                'inputs': inputs}, ensure_ascii=False))
    system = SYSTEM + '\nAnalyze this entire page, including margins and small footnotes. '
    system += ('Return all applicable types (multiple types may coexist). Inspect layout changes, columns, '
               'contents entries, body, footnotes, bibliographies, citations and headings. '
               'Return only rules supported by THIS page; do not repeat generic example rules. '
               'Report every unsupported or uncertain structure in warnings. A blank page still needs classification. '
               'Use 판단 불가 when unreadable. Never classify an unreadable page as a blank page.')
    for number in range(1, count + 1):
        _check_stop(stop)
        progress(f'책 전체 분석: {number}/{count}쪽 · 완료 {len(findings)}쪽 · 새 요청 {calls}회 · 재사용 {cached}쪽')
        checkpoint(number)
        images, evidence = collect_evidence(pdf, template, str(number), work_dir, stop, full_detail=True,
                                           reference_ocr=reference_ocr)
        prompt = json.dumps({'format_examples_only': examples, 'evidence': evidence}, ensure_ascii=False)
        digest = hashlib.sha256(json.dumps([VERSION, inputs, model, system, prompt], sort_keys=True).encode())
        for image in images:
            digest.update(image)
        cache_key = digest.hexdigest()
        path = cache / (cache_key[:32] + '.json') if cache else None
        answer = None
        if path and path.is_file():
            try:
                saved = json.loads(path.read_text(encoding='utf-8'))
                if saved.get('key') != cache_key:
                    raise ValueError('캐시 지문 불일치')
                answer = validate_page(saved['text'], pdf, template)
                actual = saved['model']
                cached += 1
            except (OSError, ValueError, KeyError, TypeError):
                answer = None
        if answer is None:
            for request_system in (system, system + '\nReturn one valid JSON object with no text outside it.'):
                _check_stop(stop)
                text, complete, reason, tokens, actual = client.request(
                    model_name=model, system=request_system,
                    prompt=prompt, images=images, schema=page_schema())
                calls += 1
                for key, value in tokens.items():
                    if isinstance(value, (int, float)):
                        usage[key] = usage.get(key, 0) + value
                if complete or reason != 'invalid_json' or request_system != system:
                    break
                progress(f'{number}/{count}쪽 JSON 응답 오류 · 한 번 다시 요청합니다.')
            if not complete:
                raise ValueError(f'{number}/{count}쪽 AI 분석 미완료({reason}). 완료 쪽은 재사용하며 규칙은 적용하지 않았습니다.')
            try:
                answer = validate_page(text, pdf, template)
            except (ValueError, TypeError) as exc:
                raise ValueError(f'{number}/{count}쪽 분석 형식 오류: {exc}. 다시 실행하면 완료 쪽을 재사용합니다.') from exc
            # Preserve a complete paid response even when cancellation arrives simultaneously.
            if path:
                write_atomic(path, json.dumps({'key': cache_key, 'text': text, 'model': actual}, ensure_ascii=False))
        _check_stop(stop)
        excerpt = evidence['pages'][0]
        if excerpt.get('pdf_text_truncated') or excerpt.get('ocr_reference_truncated'):
            answer['warnings'].append('보조 텍스트가 길어 일부만 전달했습니다. 전체 쪽 이미지와 확대 이미지로 구조를 분석했습니다.')
        if excerpt.get('ocr_reference_unavailable'):
            answer['warnings'].append('기존 OCR 기록을 읽을 수 없어 이미지와 PDF 글자층으로 분석했습니다.')
        findings.append({'pdf_page': number, 'model': actual,
                         'reference_hwpx_status': excerpt.get('reference_hwpx_status', 'not_selected'), **answer})
        checkpoint(number)
    verify_inputs({'inputs': inputs})
    checkpoint(None, complete=True)
    rule, warnings, conflicts = merge_findings(findings, pdf, template)
    inventory = {kind: [item['pdf_page'] for item in findings if kind in item['types']] for kind in TYPES}
    rule['page_types'] = {'source_sha256': inputs['pdf_sha256'],
                          'pages': {kind: numbers for kind, numbers in inventory.items() if numbers}}
    for item in findings:
        warnings.extend(f"PDF {item['pdf_page']}쪽: {warning}" for warning in item['warnings'])
    unresolved = inventory['판단 불가']
    reference_pages = [item['pdf_page'] for item in findings if item['reference_hwpx_status'] == 'aligned']
    unaligned = [item['pdf_page'] for item in findings if item['reference_hwpx_status'] == 'not_aligned']
    if unaligned:
        warnings.append('참고 OCR HWPX와 대응 미확인(참고 본문을 사용하지 않음): PDF ' + ', '.join(map(str, unaligned)))
    if unresolved:
        warnings.append('판단 불가 쪽: ' + ', '.join(map(str, unresolved)) + ' — 해당 쪽의 규칙은 추가 확인이 필요합니다.')
    if inventory['각주']:
        warnings.append('각주가 확인되었습니다. 현재 규칙 스키마는 소주 스타일을 지원하지만 각주 번호·연결의 자동 복원을 지원하지 않습니다.')
    summary = f'전체 {count}쪽을 빠짐없이 AI 구조 분석했습니다. 유형별 분류는 중복될 수 있습니다.\n'
    summary += '\n'.join(f'{kind}: {len(numbers)}쪽' for kind, numbers in inventory.items() if numbers)
    return {'rule': rule, 'summary': summary, 'warnings': warnings, 'inputs': inputs,
            'coverage': {'page_count': count, 'selected_pages': list(range(1, count + 1)),
                         'analyzed_pages': list(range(1, count + 1)), 'selection': 'whole_book',
                         'whole_book_visually_reviewed': True, 'unresolved_pages': unresolved,
                         'reference_aligned_pages': reference_pages, 'reference_unaligned_pages': unaligned,
                         'new_requests': calls, 'cached_pages': cached},
            'type_pages': inventory, 'page_findings': findings, 'conflicts': conflicts,
            'analysis_directory': str(cache) if cache else None,
            'model': model, 'usage': usage}
