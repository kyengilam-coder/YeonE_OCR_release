"""Independent rereading in a new workspace; the source workspace is read-only."""
from __future__ import annotations

import copy
import hashlib
import html
import json
from pathlib import Path
import shutil

from .state import Work, INTERNAL_DIR, work_dir_in, public_root, write_atomic


def folder_for(path):
    path = Path(path).resolve()
    return path if path.name == INTERNAL_DIR else work_dir_in(path)


def read_record(work, number):
    path = work.record_path(number)
    if not path.exists():
        return None
    value = json.loads(path.read_text(encoding='utf-8-sig'))
    if not isinstance(value, dict) or value.get('pdf_page') != number:
        raise ValueError(f'판독 기록 형식 오류: {path}')
    return value


def page_text(record):
    if not record or not record.get('merged'):
        return ''
    from .schema import PageReading
    from .blocks import page_text_of
    return page_text_of(PageReading.model_validate(record['merged']))


def snapshot_records(records, source, baseline):
    """Bind comparison history to the snapshot, never to the live old project."""
    copied = copy.deepcopy(records)
    for rec in copied.values():
        if not rec:
            continue
        local = rec.get('local_first') or {}
        if local.get('project_path'):
            project = Path(local['project_path']).resolve()
            if not project.is_relative_to(source.resolve()):
                raise ValueError('교정 프로젝트가 선택한 작업 폴더 밖에 있습니다. 해당 프로젝트가 포함된 작업 폴더를 선택하십시오.')
            local['project_path'] = str(baseline / project.relative_to(source.resolve()))
    return copied


def reasons(record):
    if not record or not record.get('merged'):
        return ['미완료·판독 기록 없음']
    found = []
    def visit(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key in ('uncertain', 'unc') and item:
                    found.append('판독 확인 항목')
                visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)
    visit(record['merged'])
    if '〔' in page_text(record) or '�' in page_text(record):
        found.append('의심·판독불명 표시')
    if record.get('suspects'):
        found.append('자형 의심 기록')
    if any(item.get('grade') in ('red', 'blue') for item in record.get('local_grades', []) if isinstance(item, dict)):
        found.append('불일치·낮은 신뢰도')
    local = record.get('local_first') or {}
    if local and (local.get('review') or {}).get('status') not in ('reviewed', 'no-api-needed', 'blank-page'):
        found.append('로컬 검수 미완료·확인 필요')
    if local.get('ai_proposals'):
        found.append('AI 교정 제안 확인')
    # Retain pages with low-confidence markers or pending local proposals.
    if any(record.get(key) for key in ('error', 'errors')):
        found.append('판독 오류')
    return list(dict.fromkeys(found))


def inspect(pdf, source, mode='suspect', scope=None):
    from .pdfio import open_pdf, parse_pages
    if mode not in ('suspect', 'all'):
        raise ValueError('지원하지 않는 재판독 범위입니다.')
    work = Work(pdf, folder_for(source), initialize=False)
    if not work.source_matches():
        raise ValueError('선택한 PDF와 기존 OCR의 원본이 일치하지 않습니다.')
    with open_pdf(pdf) as doc:
        pages = parse_pages(scope, len(doc))
    records = {n: read_record(work, n) for n in pages}
    if not any(records.values()):
        raise ValueError('선택 범위에 기존 OCR 기록이 없습니다.')
    selected = {n: (['전체 재판독'] if mode == 'all' else reasons(rec)) for n, rec in records.items()}
    return work, records, {n: why for n, why in selected.items() if why}


def snapshot(source, destination, cancelled=lambda: False):
    """Copy regular files, verify each copy, and reject redirected paths."""
    manifest = {}
    for path in sorted(source.rglob('*')):
        if cancelled():
            raise InterruptedError('재판독 준비를 중지했습니다.')
        if path.is_symlink() or (hasattr(path, 'is_junction') and path.is_junction()):
            raise ValueError('링크가 있는 작업 폴더는 재판독 사본으로 복사할 수 없습니다.')
        if not path.is_file():
            continue
        relative = path.relative_to(source)
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        with path.open('rb') as stream:
            digest = hashlib.file_digest(stream, 'sha256').hexdigest()
        shutil.copy2(path, target)
        with target.open('rb') as stream:
            copied = hashlib.file_digest(stream, 'sha256').hexdigest()
        with path.open('rb') as stream:
            current = hashlib.file_digest(stream, 'sha256').hexdigest()
        if digest != copied or digest != current:
            raise ValueError('복사 중 기존 작업이 변경되었습니다. 다른 작업을 종료하고 다시 시도하십시오.')
        manifest[str(relative)] = digest
    return manifest


def report(root, pdf, before, work, selected, status):
    from .local_reading import corrected_output_records
    from .recheck_review import spans
    warnings = []
    def views(records):
        records = [r for r in records if r and r.get('merged')]
        try:
            return {r['pdf_page']: r for r in corrected_output_records(records, pdf)}
        except Exception:
            result = {}
            for rec in records:
                try:
                    result[rec['pdf_page']] = corrected_output_records([rec], pdf)[0]
                except Exception as exc:
                    warnings.append(f"PDF {rec['pdf_page']}쪽 교정문을 불러오지 못해 저장 판독문 표시: {exc}")
                    result[rec['pdf_page']] = rec
            return result
    def marked(text, intervals):
        rendered = []
        at = 0
        for left, right in intervals:
            rendered.append(html.escape(text[at:left]))
            if right > left:
                rendered.append('<mark>' + html.escape(text[left:right]) + '</mark>')
            at = right
        return ''.join(rendered) + html.escape(text[at:])
    old_views = views(before.get(n) for n in selected)
    new_views = views(read_record(work, n) for n in selected)
    changes, sections = [], []
    for n, why in selected.items():
        after = new_views.get(n)
        old, new = page_text(old_views.get(n)), page_text(after)
        edits = spans(old, new)
        state = '미완료' if not after or not after.get('merged') else ('변경 후보' if old != new else '본문 동일')
        changes.append({'pdf_page': n, 'reasons': why, 'status': state, 'before': old, 'after': new})
        link = html.escape(Path(pdf).resolve().as_uri() + f'#page={n}', quote=True)
        image = work.page_image_path(n)
        preview = ('<details><summary>원본 쪽 이미지</summary><img style="max-width:100%" src="'
                   + html.escape(image.resolve().as_uri(), quote=True) + '"></details>') if image.is_file() else ''
        sections.append(f'<h2>PDF {n}쪽 · {state}</h2><a href="{link}">원본 PDF 보기</a>'
                        f'{preview}<p>{html.escape(" · ".join(why))}</p><div class="pair"><pre>{marked(old, [e[:2] for e in edits])}</pre>'
                        f'<pre>{marked(new, [e[2:] for e in edits])}</pre></div>')
    write_atomic(root / '재판독_비교.json', json.dumps({'status': status, 'warnings': warnings, 'pages': changes}, ensure_ascii=False, indent=2))
    write_atomic(root / '재판독_비교.html', '<!doctype html><meta charset="utf-8"><title>재판독 비교</title>'
                 '<style>body{font:18px sans-serif;margin:30px}.pair{display:flex;gap:20px}pre{white-space:pre-wrap;'
                 'overflow-wrap:anywhere;width:50%;padding:15px;background:#f4f5f8}</style>'
                 '<h1>기존 OCR / 새 판독 비교</h1><p>왼쪽은 기존 본문, 오른쪽은 새 판독입니다. '
                 '차이는 교정 후보이며 정확도 향상을 보증하지 않습니다. 기존 OCR·교정 이력은 보존됩니다.</p>'
                 f'<p>실행 상태: {html.escape(status)}</p><p>{html.escape(chr(10).join(warnings))}</p>' + ''.join(sections))


def command(args):
    from . import cli
    source, before, selected = inspect(args.pdf, args.recheck_from, args.recheck_mode, args.pages)
    if not selected:
        cli.log('의심·미완료 쪽이 없습니다. 전체 재판독을 선택하면 모든 쪽을 다시 읽습니다.')
        return 0
    if not args.work:
        raise ValueError('재판독은 새로운 --work 결과 폴더가 필요합니다.')
    destination = Path(args.work).resolve()
    root = public_root(destination)
    original = public_root(source.dir).resolve()
    if root == original or root.is_relative_to(original) or original.is_relative_to(root) or root.exists():
        raise ValueError('기존 결과와 겹치지 않는 새 결과 폴더를 선택하십시오.')
    root.mkdir(parents=True)
    audit = {'source': str(source.dir), 'mode': args.recheck_mode, 'scope_pages': list(before),
             'selected_pages': list(selected), 'status': '준비 중'}
    def save():
        write_atomic(root / '재판독_실행.json', json.dumps(audit, ensure_ascii=False, indent=2))
    save()
    fresh = None
    try:
        cli.log('기존 OCR·교정 이력의 보존 사본을 준비합니다…')
        audit['snapshot_sha256'] = snapshot(source.dir, root / '기존작업_보존', cli.CANCEL.is_set)
        # Read the immutable snapshot after copying, not a potentially changing source.
        baseline = Work(args.pdf, root / '기존작업_보존', initialize=False)
        copied = {n: read_record(baseline, n) for n in before}
        if copied != before:
            raise ValueError('대상 확인 후 기존 OCR이 변경되었습니다. 작업을 멈추고 다시 시도하십시오.')
        before = snapshot_records(copied, source.dir, baseline.dir)
        if not baseline.source_matches():
            raise ValueError('보존 사본의 원본 정보가 일치하지 않습니다.')
        fresh = Work(args.pdf, destination)
        fresh.ensure_source(args.dpi)
        for n, rec in before.items():
            if n not in selected and rec:
                fresh.save_record(n, rec)
        trial = copy.copy(args)
        trial.recheck_from = None
        trial.pages = ','.join(map(str, selected))
        # The whole-scope build below includes the reread and untouched pages.
        trial.defer_recheck_hwpx = True
        trial.force = True
        trial.no_cache = True
        trial.local_review_only = False
        trial.local_resume = 'all'
        audit['status'] = '실행 중'; save()
        cli.log(f'기존 OCR 보존: {source.dir}\n새 재판독: {destination}\n재판독 대상: {len(selected)}쪽')
        rc = cli.cmd_run(trial, work=fresh)
        if rc not in (0, None) or cli.CANCEL.is_set():
            audit['status'] = '중지·미완료'
            return rc or 130
        if any(not (read_record(fresh, n) or {}).get('merged') for n in selected):
            audit['status'] = '미완료 쪽 있음'
            return 2
        # Assemble all pages in the requested scope, retaining untouched readings.
        output = copy.copy(trial)
        output.pages = ','.join(map(str, before))
        output.reuse_records = True
        output.no_context_model = True
        rc = cli.cmd_build(output, work=fresh)
        audit['status'] = '완료·원본 대조 필요' if rc in (0, None) else '문서 생성 실패'
        return rc
    except BaseException:
        audit['status'] = '중지·실패'
        raise
    finally:
        save()
        if fresh is not None:
            report(root, args.pdf, before, fresh, selected, audit['status'])
