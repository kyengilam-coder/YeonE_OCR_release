"""Book-specific result selection. OCR destinations and browsing destinations are independent."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import re

from .state import Work, public_root


def _book_key(source):
    return os.path.normcase(str(Path(source).resolve()))


def _source_stems(source, work):
    stems = {Path(source).stem}
    # A copied/renamed PDF may still have completed documents under its old name.
    try:
        manifest = json.loads((work / 'source.json').read_text(encoding='utf-8-sig'))
        old_path = manifest.get('path')
        if old_path and Path(old_path).stem not in stems and Work(source, work, initialize=False).source_matches():
            stems.add(Path(old_path).stem)
    except (AttributeError, ValueError, OSError, TypeError):
        pass
    return stems


def documents(source, work):
    """Recognize completed current, dated, split and legacy output names only."""
    stems = '|'.join(re.escape(stem) for stem in _source_stems(source, work))
    dated = r' - (?:\d{6}(?:_\d{6})?|\d{8}(?:_\d{6})?)(?: - v[1-9]\d*)?(?:_\d{2,})?'
    name = re.compile(rf'(?:{stems})(?:{dated}|_p\d+-\d+)?\.hwpx|(?:{stems})(?:{dated}|_글자층)\.pdf', re.I)
    folder = public_root(work) / 'out'
    result = {}
    if folder.is_dir():
        for path in folder.iterdir():
            if name.fullmatch(path.name) and path.is_file():
                try:
                    stat = path.stat()
                except FileNotFoundError:
                    continue
                result[path] = (stat.st_mtime_ns, stat.st_size)
    return result


def latest_document(source, work, suffix):
    files = documents(source, work)
    return max((p for p in files if p.suffix.lower() == suffix),
               key=lambda path: (files[path][0], path.name), default=None)


@dataclass(frozen=True)
class ResultVersion:
    work: str
    label: str
    hwpx: str = ''
    pdf: str = ''
    editor_source: str = ''

    def file(self, extension):
        filename = self.hwpx if extension == '.hwpx' else self.pdf
        if not filename or Path(filename).name != filename or Path(filename).suffix.lower() != extension:
            return None
        path = public_root(Path(self.work)) / 'out' / filename
        return path if path.is_file() else None

    def valid_source(self, source):
        try:
            return Work(source, self.work, initialize=False).source_matches()
        except (OSError, ValueError, TypeError):
            return False


def versions(cfg, source):
    by_book = cfg.get('result_versions')
    book = by_book.get(_book_key(source)) if isinstance(by_book, dict) else None
    if not isinstance(book, dict):
        return [], None
    entries = []
    rows = book.get('versions')
    for row in rows if isinstance(rows, list) else []:
        try:
            version = ResultVersion(**row)
            if all(isinstance(value, str) for value in asdict(version).values()) and Path(version.work).is_absolute():
                entries.append(version)
        except (TypeError, ValueError):
            continue
    current = next((v for v in entries if asdict(v) == book.get('current')), None)
    return entries, current


def remember(cfg, source, version):
    entries, _ = versions(cfg, source)
    if version:
        entries = [version, *(v for v in entries if v != version)]
    if not isinstance(cfg.get('result_versions'), dict):
        cfg['result_versions'] = {}
    cfg['result_versions'][_book_key(source)] = {
        'versions': [asdict(v) for v in entries], 'current': asdict(version) if version else None}


def build_details(work):
    try:
        state = json.loads((work / 'state.json').read_text(encoding='utf-8-sig'))
        record = state.get('last_build')
        return record if isinstance(record, dict) else {}
    except (AttributeError, OSError, ValueError):
        return {}


def review_summary(result):
    details = build_details(Path(result.work))
    if result.hwpx not in [Path(name).name for name in details.get('outputs', []) if isinstance(name, str)]:
        return ''
    counts = details.get('review')
    if not isinstance(counts, dict):
        return ''
    labels = [('marks', '의심 표시'), ('findings', '검사 항목'), ('pending_proposals', '미확정 제안'), ('missing_styles', '누락 스타일')]
    return ' · '.join(f'{text} {counts[key]}개' for key, text in labels if type(counts.get(key)) is int)


@dataclass
class OutputRun:
    source: str
    work: Path
    label: str
    suffix: str
    before: dict

    @classmethod
    def capture(cls, argv):
        if len(argv) < 4 or argv[0] not in ('run', 'build', 'textlayer') or '--work' not in argv or '--local-review-only' in argv:
            return None
        work = Path(argv[argv.index('--work') + 1]).resolve()
        source = argv[1]
        suffix = '.pdf' if argv[0] == 'textlayer' or '--pdf-only' in argv else '.hwpx'
        label = '재판독본' if '--recheck-from' in argv else ('기존 OCR 출력' if '--reuse-records' in argv else '출력')
        return cls(source, work, label, suffix, documents(source, work))

    def completed(self):
        """Only a newly written document can replace the user's current result."""
        after = documents(self.source, self.work)
        created = [p for p in after if self.before.get(p) != after[p]]
        # For a split volume, choose the first HWPX from the final build.
        written = build_details(self.work).get('outputs') if self.suffix == '.hwpx' else None
        primary = None
        if isinstance(written, list):
            for name in written:
                if isinstance(name, str):
                    candidate = public_root(self.work) / 'out' / Path(name).name
                    if candidate in created and candidate.suffix.lower() == '.hwpx':
                        primary = candidate
                        break
        if primary is None:
            primary = max((p for p in created if p.suffix.lower() == self.suffix),
                          key=lambda path: (after[path][0], path.name), default=None)
        if primary is None:
            return None
        newest = lambda suffix: max((p for p in created if p.suffix.lower() == suffix),
                                    key=lambda path: (after[path][0], path.name), default=None)
        hwpx = primary if self.suffix == '.hwpx' else newest('.hwpx')
        pdf = primary if self.suffix == '.pdf' else newest('.pdf')
        return ResultVersion(str(self.work), self.label, hwpx.name if hwpx else '', pdf.name if pdf else '')
