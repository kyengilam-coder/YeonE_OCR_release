"""Review rereadings against preserved, corrected readings and publish a separate HWPX."""
from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import uuid
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path

from .file_lock import exclusive_file_lock
from .local_reading import corrected_output_records
from .recheck import read_record, snapshot_records
from .schema import PageReading
from .state import INTERNAL_DIR, Work, write_atomic


def _digest(obj):
    data = json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()
    return hashlib.sha256(data).hexdigest()


def spans(before, after):
    matcher = SequenceMatcher(None, before, after, autojunk=max(len(before), len(after)) > 500)
    return [(a, b, c, d) for op, a, b, c, d in matcher.get_opcodes() if op != 'equal']


@dataclass(frozen=True)
class Change:
    page: int
    path: tuple | None   # None means a whole page with incompatible structure.
    location: str
    before: str
    after: str
    left: tuple
    right: tuple


def _fields(reading):
    """Compare native fields. Keep paragraphs, columns, title levels and table cells."""
    shape = []
    values = {}
    for i, region in enumerate(reading['regions']):
        base = ('regions', i)
        label = f'{i+1}번 영역 · {region["kind"]}'
        shape.append((region['kind'], region['title_level'], region['reading_direction'],
                      [(p['continues_from_previous'], [c['col_from_right'] for c in p['columns']])
                       for p in region['paragraphs']], [len(r['cells']) for r in region['table_rows']]))
        values[base + ('title_text',)] = (label + ' · 제목', region['title_text'])
        for j, paragraph in enumerate(region['paragraphs']):
            for k, column in enumerate(paragraph['columns']):
                path = base + ('paragraphs', j, 'columns', k, 'text')
                values[path] = (f'{label} · {j+1}문단 {k+1}열', column['text'])
        for j, row in enumerate(region['table_rows']):
            for k, cell in enumerate(row['cells']):
                values[base + ('table_rows', j, 'cells', k)] = (f'{label} · 표 {j+1}행 {k+1}열', cell)
    for key, label in [('print_page', '인쇄 쪽수'), ('running_header', '머리말'), ('page_label', '쪽번호 표기')]:
        values[(key,)] = (label, reading[key])
    return shape, values


def differences(page, old, new):
    shape_a, fields_a = _fields(old) if old else (None, {})
    shape_b, fields_b = _fields(new)
    if shape_a != shape_b:
        # There is no safe mapping across e.g. an old table and a new paragraph.
        def describe(fields):
            return '\n'.join(f'{label}\n{text}' for label, text in fields.values() if text)
        a, b = describe(fields_a), describe(fields_b)
        return [Change(page, None, '쪽 구조 변경 · 쪽 단위로 선택' if old else '신규 판독 쪽', a, b,
                       (0, len(a)), (0, len(b)))]
    result = []
    for path, (label, a) in fields_a.items():
        b = fields_b[path][1]
        result.extend(Change(page, path, label, a, b, (i, j), (k, l)) for i, j, k, l in spans(a, b))
    return result


def _output_copy(record):
    record = copy.deepcopy(record)
    record['merged'] = PageReading.model_validate(record['merged']).model_dump()
    # Image crops are regenerated from this version's PDF and geometry at export.
    record.pop('crops', None)
    # This independent output view already includes the saved human corrections.
    # A later output must not overlay the comparison project's text a second time.
    if record.get('local_first'):
        record['local_first'].pop('project_path', None)
    return record


def _views(records, pdf, directory):
    views = corrected_output_records([rec for rec in records if rec and rec.get('merged')], pdf)
    repairs = Path(directory) / 'output_repairs.json'
    if repairs.is_file():
        from ab_ocr.ocr import stable_file_sha256
        from .output_recovery import apply_repairs
        views = apply_repairs(views, json.loads(repairs.read_text(encoding='utf-8-sig')), stable_file_sha256(pdf))
    return {r['pdf_page']: _output_copy(r) for r in views}


class Review:
    def __init__(self, pdf, directory):
        self.pdf = Path(pdf).resolve()
        self.work = Work(self.pdf, Path(directory).resolve(), initialize=False)
        self.root = self.work.public
        audit = json.loads((self.root / '재판독_실행.json').read_text(encoding='utf-8'))
        if audit.get('status') != '완료·원본 대조 필요':
            raise ValueError('완료된 재판독 결과에서 확정본을 만들 수 있습니다.')
        preserved = Work(self.pdf, self.root / '기존작업_보존', initialize=False)
        if not self.work.source_matches() or not preserved.source_matches():
            raise ValueError('현재 PDF와 재판독·보존 기록의 원본이 일치하지 않습니다.')
        selected = set(audit['selected_pages'])
        if not selected or any(type(n) is not int or n < 1 for n in selected):
            raise ValueError('재판독 대상 쪽 기록을 확인하십시오.')
        current = self.work.records(repair=False)
        self.pages = {r['pdf_page'] for r in current}
        if self.pages != set(audit.get('scope_pages', self.pages)):
            raise ValueError('재판독 범위와 저장된 쪽이 달라졌습니다. 원래 판독 기록을 확인하십시오.')
        original = {n: read_record(preserved, n) for n in self.pages}
        original = snapshot_records(original, Path(audit['source']), preserved.dir)
        self.old = _views(original.values(), self.pdf, preserved.dir)
        self.new = _views([r for r in current if r['pdf_page'] in selected], self.pdf, self.work.dir)
        if selected != self.new.keys() or not self.pages - selected <= self.old.keys():
            raise ValueError('완료된 쪽 판독 기록이 누락되었습니다.')
        self.changes = []
        for n in sorted(selected):
            self.changes.extend(differences(n, self.old.get(n, {}).get('merged'), self.new[n]['merged']))
        self.selected = selected
        source_hash = json.loads((self.work.dir / 'source.json').read_text(encoding='utf-8'))['sha256']
        self.signature = _digest((source_hash, sorted(selected), self.old, self.new))
        self.decisions_path = self.root / '재판독_검토.json'
        self.decisions = {}
        self.revision = None
        self.stale = False
        if self.decisions_path.exists():
            saved = json.loads(self.decisions_path.read_text(encoding='utf-8'))
            self.revision = _digest(saved)
            if saved.get('inputs') == self.signature:
                self.decisions = {int(k): v for k, v in saved['decisions'].items()
                                  if k.isdecimal() and int(k) < len(self.changes) and type(v) is bool}
            else:
                self.stale = True

    def save(self):
        with exclusive_file_lock(self.decisions_path.with_suffix('.lock')):
            existing = (json.loads(self.decisions_path.read_text(encoding='utf-8'))
                        if self.decisions_path.exists() else None)
            if (_digest(existing) if existing is not None else None) != self.revision:
                raise ValueError('다른 창에서 검토 결과를 바꿨습니다. 창을 다시 여십시오.')
            data = {'inputs': self.signature, 'decisions': {str(k): v for k, v in self.decisions.items()}}
            if self.stale and existing is not None:
                backup = self.root / ('이전_검토-' + uuid.uuid4().hex[:10] + '.json')
                write_atomic(backup, json.dumps(existing, ensure_ascii=False))
            write_atomic(self.decisions_path, json.dumps(data, ensure_ascii=False))
            self.revision = _digest(data)
            self.stale = False

    def approved_records(self):
        if set(self.decisions) != set(range(len(self.changes))):
            raise ValueError('아직 검토하지 않은 변경이 있습니다.')
        result = {n: copy.deepcopy(self.old.get(n)) for n in self.pages}
        for i in reversed(range(len(self.changes))):
            if not self.decisions[i]:
                continue
            change = self.changes[i]
            n = change.page
            if change.path is None:
                result[n] = copy.deepcopy(self.new[n])
                continue
            record = result[n]
            parent = record['merged']
            for key in change.path[:-1]:
                parent = parent[key]
            field = change.path[-1]
            a, b = change.left
            c, d = change.right
            parent[field] = parent[field][:a] + change.after[c:d] + parent[field][b:]
            # OCR/correction evidence remains in the source versions. Its character
            # offsets cannot be interpreted against mixed, length-changing text.
            for key in ('adjudications', 'suspects', 'local_grades', 'derived_reading_sha256'):
                record.pop(key, None)
        for n, record in result.items():
            if record is None:  # User explicitly declined to insert a previously missing page.
                result[n] = {'pdf_page': n, 'merged': PageReading(pdf_page=n, page_note='기존 판독 기록 없음').model_dump()}
            result[n].setdefault('file', self.pdf.name)
        return [result[n] for n in sorted(result)]

    def export(self, options):
        from . import cli
        from .pdfio import open_pdf
        records = self.approved_records()
        # Re-open immediately before export to detect OCR, human or file changes.
        if Review(self.pdf, self.work.dir).signature != self.signature:
            raise ValueError('검토 중 PDF 또는 판독·교정문이 바뀌었습니다. 새 내용으로 다시 검토하십시오.')
        self.save()
        name = '확정-' + uuid.uuid4().hex[:12]
        destination = self.root.parent / name
        with tempfile.TemporaryDirectory(dir=self.root.parent, prefix='.yeone-review-') as container:
            staged = Path(container) / name
            work = Work(self.pdf, staged / INTERNAL_DIR)
            for record in records:
                work.save_record(record['pdf_page'], record)
            args = cli.build_parser().parse_args(['build', str(self.pdf), *options, '--no-context-model'])
            args.docx = False
            args.out = None
            args.split = 0
            with open_pdf(self.pdf) as doc:
                if not cli.build_outputs(work, doc, records, args):
                    raise ValueError('출력 양식 또는 HWPX 구조 검사를 통과하지 못했습니다.')
                if cli.cmd_review(args, work=work, doc=doc):
                    raise ValueError('확정 문서의 원본 대조 화면을 만들지 못했습니다.')
            filename, = work.load_state()['last_build']['outputs']
            if not (work.out_dir / filename).is_file():
                raise ValueError('확정 HWPX를 찾을 수 없습니다.')
            decisions = [{'page': change.page, 'field': change.location, 'accepted': self.decisions[i],
                          'before': change.before[slice(*change.left)], 'after': change.after[slice(*change.right)]}
                         for i, change in enumerate(self.changes)]
            write_atomic(staged / '확정_내역.json', json.dumps({'reviewed_version': str(self.root),
                         'inputs': self.signature, 'changes': decisions}, ensure_ascii=False, indent=1))
            staged.rename(destination)
        return destination / INTERNAL_DIR, destination / 'out' / filename
