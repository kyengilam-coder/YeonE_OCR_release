"""Compare OCR with independent readings of the same source image."""
from __future__ import annotations

from collections import defaultdict
from contextlib import ExitStack
from copy import deepcopy
from difflib import SequenceMatcher
from html import escape
from pathlib import Path
from tempfile import TemporaryDirectory

from .schema import PageReading, TEXT_KINDS, TITLE_KINDS

# Align common OCR punctuation variants without altering the printed form.
_PUNCTUATION = str.maketrans('，；：。！？（）“”‘’', ',;:.!?()""\'\'')


def _located_lines(raw):
    if 'located_lines' in raw:
        return raw['located_lines']
    from .local_reading import _lines, _normalized, _source_boxes

    width, height = raw['image_width'], raw['image_height']
    lines = _lines(raw['text'])
    boxes, complete = _source_boxes(raw, lines, width, height)
    return [(_normalized(box, width, height), ''.join(line.split()))
            for (_, _, line), box, valid in zip(lines, boxes, complete) if box and valid and line.strip()]


def _in_region(lines, bbox, direction):
    # Word boxes describe ink; allow a little margin for different detectors.
    y0, x0, y1, x1 = bbox
    margin = 10
    values = []
    for (top, left, bottom, right), value in lines:
        if direction == 'vertical':
            cross = x0 - margin <= (left + right) / 2 <= x1 + margin
            overlap = min(y1 + margin, bottom) - max(y0 - margin, top)
            extent = bottom - top
        else:
            cross = y0 - margin <= (top + bottom) / 2 <= y1 + margin
            overlap = min(x1 + margin, right) - max(x0 - margin, left)
            extent = right - left
        if cross and overlap >= .75 * extent:
            values.append(value)
    return ''.join(values)


def _edits(base, witness):
    if not witness or not .8 * len(base) <= len(witness) <= 1.2 * len(base):
        return []
    match = SequenceMatcher(None, base.translate(_PUNCTUATION), witness.translate(_PUNCTUATION), autojunk=False)
    if match.ratio() < .82:
        return []
    ops = match.get_opcodes()
    return [(lo, hi, witness[a:b], prev[0] == after[0] == 'equal'
             and prev[2] - prev[1] >= 3 and after[2] - after[1] >= 3)
            for prev, (op, lo, hi, a, b), after in zip(
                [('edge', 0, 0, 0, 0)] + ops, ops, ops[1:] + [('edge', 0, 0, 0, 0)])
            if op != 'equal']


def _family(engine):
    # Different Paddle models can have correlated errors. Count them as one vote.
    return 'paddle' if engine in ('rapidocr', 'ppocrv6', 'paddle_server') else engine


def compare_page(record, witnesses, *, baseline=None):
    """Return a transient reading and the proposed replacements with evidence.

    Only unambiguous, locally anchored single-letter replacements are applied.
    Keeping character counts also keeps existing review spans aligned.
    """
    number = record['pdf_page']
    direction = 'horizontal' if record.get('mode') == 'horizontal_zh' else 'vertical'
    locations = {engine: _located_lines(raw) for engine, raw in witnesses.items()}
    reading = PageReading.model_validate(record['merged'])
    findings = []
    grades = []
    matched = eligible = 0
    for region in reading.regions:
        if region.kind in TITLE_KINDS:
            fields = [(region, 'title_text', 0, 0)]
        elif region.kind in TEXT_KINDS:
            fields = []
            for para_index, para in enumerate(region.paragraphs):
                offset = 0
                for col in sorted(para.columns, key=lambda c: c.col_from_right):
                    fields.append((col, 'text', para_index, offset))
                    offset += len(col.text)
        else:
            continue
        positions, letters = [], []
        for index, (obj, field, _, _) in enumerate(fields):
            for offset, char in enumerate(getattr(obj, field)):
                if not char.isspace():
                    positions.append((index, offset))
                    letters.append(char)
        base = ''.join(letters)
        if not base:
            continue
        eligible += 1
        if len(base) < 9 or region.bbox == [0, 0, 1000, 1000] and len(reading.regions) != 1:
            continue
        votes = defaultdict(dict)
        exact = False
        for engine, lines in locations.items():
            candidate = _in_region(lines, region.bbox, direction)
            exact |= candidate == base
            edits = _edits(base, candidate)
            for lo, hi, new, anchored in edits:
                votes[lo, hi, new][engine] = anchored
        matched += bool(votes or exact)
        for (lo, hi, new), supporters in sorted(votes.items()):
            old = base[lo:hi]
            agrees = {_family(engine) for engine, anchored in supporters.items()
                      if anchored and _family(engine) != _family(baseline)}
            competing = any(other != new and a <= hi and lo <= b for a, b, other in votes)
            if len(old) == len(new) == 1 and new.isalpha():
                from .collate.zh import _cc
                same_form = old.isalpha() and _cc('s2t').convert(old) == _cc('s2t').convert(new)
            else:
                same_form = False
            applied = (len(agrees) >= 2 and not competing and len(old) == len(new) == 1
                       and new.isalpha() and (old.isalpha() or old in '□�') and not same_form)
            if applied:
                fi, offset = positions[lo]
                obj, field, para, start = fields[fi]
                value = getattr(obj, field)
                setattr(obj, field, value[:offset] + new + value[offset + 1:])
                grades.append(dict(region_id=region.id, para=para, start=start + offset,
                                   end=start + offset + 1, grade='green'))
            findings.append(dict(region=region.id, bbox=region.bbox, offset=lo, before=old, after=new,
                                 context=base[max(0, lo - 10):hi + 10], engines=sorted(supporters), applied=applied))
    if grades:
        record = deepcopy(record)
        record['merged']['regions'] = reading.model_dump()['regions']
        record['local_grades'] = [*(record.get('local_grades') or ()), *grades]
    return record, dict(page=number, engines=sorted(witnesses), eligible_regions=eligible,
                        compared_regions=matched, findings=findings)


def review_records(work, doc, records, args, *, saved, protected, should_stop, step, log):
    from ab_ocr import ocr
    from .automatic_ocr import GPU_ENGINES, available_engines, gpu_engines_enabled, language_options
    from .engine_pool import EnginePool
    from .local_reading import render_for_record
    from .source_review_pdf import find_scan_copy, positioned_reading, same_scan_page

    output, pages, installed = [], [], {}
    states = work.load_state()['pages']
    dpi = getattr(args, 'dpi', None) or 280
    root = getattr(args, 'local_engine_root', None)
    gpu = getattr(args, 'local_gpu_engines', None)
    gpu = gpu_engines_enabled() if gpu is None else gpu
    excluded = set(getattr(args, 'local_exclude_engines', None) or ())
    gpu_language = None
    with ExitStack() as references, TemporaryDirectory(prefix='source-review-', dir=work.dir) as temp, EnginePool(should_stop=should_stop, log=log) as pool:
        reference = find_scan_copy(work.pdf_path, doc, references)
        if reference:
            log(f'원본 대조 참고 OCR: {reference[0]} · 쪽 이미지가 일치하는 곳에 사용')
        path = Path(temp) / 'page.png'
        step(0, len(records))
        for index, record in enumerate(records, 1):
            if should_stop():
                raise ocr.OCRCancelled('원본 대조 중지')
            number = record['pdf_page']
            if number in protected or '원본 대조 완료(사람)' in states.get(str(number), {}).get('statuses', ()):
                current, report = record, dict(page=number, status='기존 교정 보존', findings=[])
            else:
                info = record.get('local_first') or {}
                baseline = next((run['engine'] for run in saved.get(number, ())
                                 if run['id'] == info.get('baseline_run_id')), record.get('reader'))
                direction = 'horizontal' if record.get('mode') == 'horizontal_zh' else 'vertical'
                profile = info.get('profile') or getattr(args, 'local_profile', None) or (
                    'simplified' if direction == 'horizontal' else 'traditional')
                key = profile, direction, _family(baseline)
                witnesses = {}
                for run in saved.get(number, ()):
                    raw, engine = run['raw'], run['engine']
                    if run.get('active', True) and raw.get('words') and not raw.get('warnings'):
                        witnesses[engine] = raw
                errors = {}
                used_reference = False
                if reference and reference[1] not in {_family(e) for e in witnesses} | {_family(baseline)}:
                    name, family, copy = reference
                    try:
                        if same_scan_page(doc[number - 1], copy[number - 1]):
                            reading = positioned_reading(copy[number - 1], direction)
                            if reading['located_lines']:
                                witnesses[family] = reading
                                used_reference = True
                    except (OSError, ValueError, RuntimeError) as exc:
                        errors['참고 OCR PDF'] = str(exc)[:200]
                def enough():
                    return len({_family(e) for e in witnesses} - {_family(baseline)}) >= 2
                if not enough():
                    if key not in installed:
                        skip = excluded | {e for e in GPU_ENGINES if _family(e) == _family(baseline)}
                        installed[key], _ = available_engines(profile, direction, root, exclude=skip,
                                                              should_stop=should_stop, gpu=gpu)
                    engines = sorted(installed[key], key=lambda e: (e != 'tesseract',))
                    used = {_family(e) for e in witnesses} | {_family(baseline)}
                    engines = [e for e in engines if _family(e) not in used]
                    if engines:
                        try:
                            image, _ = render_for_record(work, doc, number, dpi, record)
                            with image:
                                image.save(path)
                        except (OSError, RuntimeError, ValueError) as exc:
                            errors['원본 이미지'] = str(exc)[:200]
                            engines = []
                    for engine in engines:
                        if _family(engine) in {_family(e) for e in witnesses}:
                            continue
                        languages = language_options(engine, profile, direction)
                        if engine in GPU_ENGINES:
                            if gpu_language is not None and gpu_language != (engine, languages):
                                pool.close()  # A second GPU model/language needs the same exclusive lease.
                            gpu_language = engine, languages
                        try:
                            result = pool.recognize(path, engine=engine, page_number=1, dpi=dpi,
                                                    languages=languages,
                                                    direction=direction, engine_root=root, should_stop=should_stop,
                                                    psm=(5 if direction == 'vertical' else 3) if engine == 'tesseract' else None)
                            if result.words:
                                witnesses[engine] = result.to_dict()
                        except ocr.OCRCancelled:
                            raise
                        except (ocr.OCRError, OSError, ValueError) as exc:
                            errors[engine] = str(exc)[:200]
                        if enough():
                            break
                current, report = compare_page(record, witnesses, baseline=baseline)
                report['status'] = ('대조 근거 부족' if not enough() or not report['compared_regions'] else
                                    '일부 영역 대조' if report['compared_regions'] < report['eligible_regions'] else '대조 결과 기록')
                if errors:
                    report['errors'] = errors
                if used_reference:
                    report['reference_pdf'] = name
            output.append(current)
            pages.append(report)
            step(index, len(records))
    log(f"원본 대조: {len(pages)}쪽 · 자동 교정 {sum(f['applied'] for p in pages for f in p['findings'])}건")
    return output, pages


def write_report(path, pages, boundaries, missing, *, ok):
    from .state import write_atomic

    applied = sum(f['applied'] for p in pages for f in p['findings'])
    pending = sum(not f['applied'] for p in pages for f in p['findings'])
    def row(*values):
        return '<tr>' + ''.join(f'<td>{escape(str(value))}</td>' for value in values) + '</tr>'

    out = ['<!doctype html><html lang="ko"><meta charset="utf-8"><title>원본 대조·각주 경계 검수</title>',
           '<style>body{font:16px/1.6 system-ui,sans-serif;margin:32px auto;max-width:1080px;padding:0 16px}'
           'table{border-collapse:collapse;width:100%;margin:12px 0}td,th{border-bottom:1px solid #ddd;'
           'padding:5px 10px;text-align:left;vertical-align:top}summary{cursor:pointer}</style>',
           '<body><h1>원본 대조·각주 경계 검수</h1>',
           f'<p>{len(pages)}쪽 · 자동 반영 {applied}건 · 추가 확인 후보 {pending}건</p>',
           '<p>자동 교정은 같은 원본 위치에서 독립 판독 두 계열 이상이 일치한 경우에 한합니다. '
           '각주 경계는 HWPX의 각주·제목·쪽 안내를 기준으로 검사했습니다.</p>']
    if not ok:
        out.append('<p>출력 파일 생성 또는 검증에 오류가 발생했습니다. 진행 기록을 확인하십시오.</p>')
    if missing:
        out.append(f'<p>판독 기록이 없는 PDF 쪽: {", ".join(map(str, missing))}</p>')
    out.append('<h2>각주 뒤 쪽 경계</h2>')
    if boundaries:
        out.append('<table><tr><th>각주 쪽</th><th>다음 쪽</th><th>확인 결과</th><th>각주 끝</th><th>다음 쪽 첫머리</th></tr>')
        out.extend(row(b['page'], b['next_page'] or '—', b['status'], b['tail'], b['head']) for b in boundaries)
        out.append('</table>')
    else:
        out.append('<p>이번 출력에서 구조 분석이 각주로 분류한 경계가 없습니다.</p>')
    out.append('<h2>쪽별 원본 대조</h2>')
    for page in pages:
        findings = page['findings']
        done = sum(f['applied'] for f in findings)
        out.append(f'<details><summary>PDF {page["page"]}쪽 · {escape(page["status"])} · '
                   f'반영 {done} · 후보 {len(findings) - done}</summary>')
        if 'engines' in page:
            engines = ', '.join(page['engines']) or '없음'
            out.append(f'<p>판독 엔진 {escape(engines)} · 대응 영역 {page["compared_regions"]}/{page["eligible_regions"]}</p>')
        if 'reference_pdf' in page:
            out.append(f'<p>원본 이미지가 일치한 참고 OCR PDF · {escape(page["reference_pdf"])}</p>')
        for engine, error in page.get('errors', {}).items():
            out.append(f'<p>{escape(engine)} · {escape(error)}</p>')
        if findings:
            out.append('<table><tr><th>처리</th><th>원본 위치</th><th>기존</th><th>후보</th><th>주변 글자</th><th>판독 엔진</th></tr>')
            out.extend(row('반영' if f['applied'] else '확인', f['bbox'], f['before'], f['after'], f['context'],
                           ', '.join(f['engines'])) for f in findings)
            out.append('</table>')
        out.append('</details>')
    write_atomic(path, '\n'.join(out + ['</body></html>']))
