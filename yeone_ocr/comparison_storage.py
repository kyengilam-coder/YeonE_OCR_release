"""Single-file, transactional storage for long comparison projects.

Each row contains one complete page (including all historical evidence). Only
modified rows are encoded in a consistent temporary snapshot. The verified,
flushed snapshot replaces the published file only after all SQLite handles close.
No WAL sidecar or permanent external page directory is required.
"""
from contextlib import contextmanager
import gzip
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import uuid
import zlib

from . import comparison as core


class ProjectStorageError(core.ComparisonError):
    """A persistence failure is fatal to a run, not a page recognition error."""


def storage_failure(path, exc):
    detail={'path':str(path),'exception':type(exc).__name__,'sqlite_errorcode':getattr(exc,'sqlite_errorcode',None),
            'sqlite_errorname':getattr(exc,'sqlite_errorname',None)}
    try:
        with Path(path).open('rb') as stream:header=stream.read(100)
        actual=Path(path).stat().st_size;detail['actual_bytes']=actual
        if header.startswith(MAGIC) and len(header)==100:
            size=int.from_bytes(header[16:18],'big');size=65536 if size==1 else size
            expected=size*int.from_bytes(header[28:32],'big');detail['declared_bytes']=expected
            if actual<expected:detail['missing_bytes']=expected-actual
    except OSError:pass
    code=detail['sqlite_errorname'] or type(exc).__name__
    suffix=f" · 파일 끝 {detail['missing_bytes']:,}바이트 부족" if detail.get('missing_bytes') else ''
    error=ProjectStorageError(f'프로젝트 저장소 오류({code}{suffix}). 추가 판독·AI 호출을 중지합니다. 복구본을 확인하세요.')
    error.storage_detail=detail
    return error

MAGIC = b"SQLite format 3\x00"
# SQLite reads/writes one bounded page at a time; legacy whole-file JSON limits
# must not impose a 1 GB aggregate ceiling on a long book's evidence history.
MAX_DATABASE_BYTES = 2_000_000_000
MAX_DATABASE_RAW_BYTES = 8_000_000_000
MAX_PAGE_RAW_BYTES = 128_000_000
APPLICATION_ID = 0x594F4352
SCHEMA = {
    "metadata": "CREATE TABLE metadata (id INTEGER PRIMARY KEY CHECK (id = 1), payload TEXT NOT NULL, generation TEXT NOT NULL)",
    "pages": "CREATE TABLE pages (number INTEGER PRIMARY KEY, payload BLOB NOT NULL, raw_size INTEGER NOT NULL, digest TEXT NOT NULL)",
}


def _commit(connection, timeout=60.0):
    """Retry the same transaction while readers release their snapshots.

    A busy COMMIT leaves SQLite's transaction live; never re-send an AI request
    or repeat row mutations to recover from this temporary storage condition.
    """
    deadline = time.monotonic() + timeout
    while True:
        try:
            connection.commit()
            return
        except sqlite3.OperationalError as exc:
            if getattr(exc, 'sqlite_errorcode', None) not in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                raise
            if time.monotonic() >= deadline:
                raise
            time.sleep(.05)


def is_database(path):
    try:
        with Path(path).open("rb") as stream:
            return stream.read(16) == MAGIC
    except OSError:
        return False


def _json(data):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise core.ComparisonError("프로젝트에 중복된 JSON 항목이 있습니다.")
            result[key] = value
        return result

    def invalid(_):
        raise core.ComparisonError("프로젝트에 유효하지 않은 숫자가 있습니다.")

    return json.loads(data, object_pairs_hook=pairs, parse_constant=invalid)


@contextmanager
def _connection(path, *, create=False):
    connection = None
    try:
        # rw also permits SQLite to roll back a hot journal left by a killed
        # process. Fall back to ro for genuinely read-only, clean backups.
        uri = Path(path).as_uri()
        try:
            connection = sqlite3.connect(uri + ("?mode=rwc" if create else "?mode=rw"), uri=True, timeout=5)
        except sqlite3.OperationalError:
            if create:
                raise
            connection = sqlite3.connect(uri + "?mode=ro", uri=True, timeout=5)
        connection.execute("PRAGMA trusted_schema=OFF")
        connection.execute("PRAGMA cache_size=-2048")
        connection.execute("PRAGMA synchronous=FULL")
        connection.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, MAX_PAGE_RAW_BYTES * 2)
        if not create:
            if Path(path).stat().st_size > MAX_DATABASE_BYTES:
                raise core.ComparisonError("프로젝트 파일이 허용 크기를 넘습니다.")
            if (connection.execute("PRAGMA application_id").fetchone()[0] != APPLICATION_ID
                    or connection.execute("PRAGMA user_version").fetchone()[0] != 1):
                raise core.ComparisonError("지원하지 않는 증분 프로젝트 형식입니다.")
            schema = connection.execute("SELECT name, sql FROM sqlite_master").fetchall()
            if dict(schema) != SCHEMA or len(schema) != len(SCHEMA):
                raise core.ComparisonError("증분 프로젝트의 저장 구조가 올바르지 않습니다.")
            if connection.execute("PRAGMA journal_mode").fetchone()[0] != "delete":
                raise core.ComparisonError("지원하지 않는 프로젝트 저널 형식입니다.")
        yield connection
    except (sqlite3.Error, OSError, UnicodeError, ValueError, RecursionError) as exc:
        if isinstance(exc, core.ComparisonError):
            raise
        raise storage_failure(path,exc) from exc
    finally:
        if connection is not None:
            connection.close()  # uncommitted changes roll back, including failed saves


def _metadata(connection):
    rows = connection.execute("SELECT payload, generation FROM metadata WHERE id=1").fetchall()
    if len(rows) != 1:
        raise core.ComparisonError("프로젝트 식별 정보가 없습니다.")
    payload, generation = rows[0]
    value = _json(payload)
    if (not isinstance(value, dict) or value.get("format") != core.FORMAT
            or type(value.get("version")) is not int or value["version"] != core.VERSION
            or not isinstance(generation, str) or len(generation) != 32):
        raise core.ComparisonError("지원하지 않는 프로젝트 형식입니다.")
    total = value.get("storage_raw_bytes", 0)
    if type(total) is not int or not 0 <= total <= MAX_DATABASE_RAW_BYTES:
        raise core.ComparisonError("프로젝트 파일이 허용 크기를 넘습니다.")
    return value, generation


def _token(path, generation):
    stat = Path(path).stat()
    # Generation detects other writers; stat catches replacement and external
    # edits without hashing the entire book at every checkpoint.
    return "sqlite:" + generation + ":" + ":".join(str(v) for v in (
        stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns))


def identity(path):
    with _connection(path) as connection:
        connection.execute("BEGIN")
        value, generation = _metadata(connection)
        return value.get("document_id"), _token(path, generation)


def _decode_page(payload, size, digest):
    if type(size) is not int or not 0 < size <= MAX_PAGE_RAW_BYTES:
        raise core.ComparisonError("쪽별 저장 크기가 허용 범위를 넘습니다.")
    inflater = zlib.decompressobj(16 + zlib.MAX_WBITS)
    try:
        content = inflater.decompress(payload, size + 1)
    except (zlib.error, TypeError) as exc:
        raise core.ComparisonError("쪽별 압축 기록이 손상되었습니다.") from exc
    if len(content) != size or not inflater.eof or inflater.unused_data or hashlib.sha256(content).hexdigest() != digest:
        raise core.ComparisonError("쪽별 저장 기록의 크기·해시가 일치하지 않습니다.")
    return _json(content)


class StreamPages(dict):
    """Validated one-page-at-a-time input to ComparisonProject.load."""
    def __init__(self, path, token, numbers):
        self.path, self.token, self.numbers = path, token, numbers

    def __len__(self):
        return len(self.numbers)

    def items(self):
        with _connection(self.path) as connection:
            connection.execute('BEGIN')
            _, generation = _metadata(connection)
            if _token(self.path, generation) != self.token:
                raise core.ComparisonError('다른 창에서 프로젝트가 변경되었습니다.')
            for n, payload, size, digest in connection.execute('SELECT number,payload,raw_size,digest FROM pages ORDER BY number'):
                yield str(n), _decode_page(payload, size, digest)


def paged_read(path):
    with _connection(path) as connection:
        connection.execute('BEGIN')
        value, generation = _metadata(connection)
        sizes = connection.execute('SELECT number,raw_size FROM pages ORDER BY number').fetchall()
        total = 0
        for n, size in sizes:
            if type(size) is not int or not 0 < size <= MAX_PAGE_RAW_BYTES:
                raise core.ComparisonError('쪽별 저장 크기가 허용 범위를 넘습니다.')
            total += size
        if total > MAX_DATABASE_RAW_BYTES or value.get('storage_raw_bytes', total) != total:
            raise core.ComparisonError('쪽별 저장 기록의 전체 크기가 일치하지 않습니다.')
        token = _token(path, generation)
        value['pages'] = StreamPages(path, token, [n for n, _ in sizes])
        return value, token


from collections.abc import MutableMapping


class CheckpointPages(MutableMapping):
    """Keep unsaved pages in RAM; evict saved pages only at explicit checkpoints."""
    def __init__(self, project, path, token, numbers, cached=None, *, validate_on_access=False):
        self.project, self.path, self.token = project, path, token
        self.numbers = set(numbers)
        self.cached = dict(cached or {})
        self.unvalidated = set(self.numbers) if validate_on_access else set()
        self.validated_run_ids = set()

    def __iter__(self):
        return iter(sorted(self.numbers))

    def __len__(self):
        return len(self.numbers)

    def __getitem__(self, number):
        if number not in self.numbers:
            raise KeyError(number)
        if number not in self.cached:
            with _connection(self.path) as connection:
                connection.execute('BEGIN')
                _, generation = _metadata(connection)
                if _token(self.path, generation) != self.token:
                    raise core.ComparisonError('다른 창에서 프로젝트가 변경되었습니다.')
                row = connection.execute('SELECT payload,raw_size,digest FROM pages WHERE number=?',(number,)).fetchone()
                if row is None:
                    raise core.ComparisonError('저장된 쪽 기록이 없습니다.')
                page = _decode_page(*row)
                if number in self.unvalidated:
                    if isinstance(page, dict):
                        page.setdefault("ai_reviews", [])
                        page.setdefault("ai_decisions", [])
                    run_ids = set()
                    self.project._validate_page(page, number, run_ids)
                    if run_ids & self.validated_run_ids:
                        raise core.ComparisonError("쪽 사이에 중복된 판독 실행 식별자가 있습니다.")
                    self.validated_run_ids.update(run_ids)
                    if page.get("baseline_run_id") is not None:
                        self.project._preserve_baselines.add(number)
                    self.unvalidated.remove(number)
                self.cached[number] = page
        return self.cached[number]

    def __setitem__(self, number, value):
        self.numbers.add(number)
        self.cached[number] = value

    def __delitem__(self, number):
        raise core.ComparisonError('저장된 쪽 기록은 삭제할 수 없습니다.')

    def trim(self):
        for n in list(self.cached):
            if self.project._page_versions.get(n,0) == self.project._saved_page_versions.get(n,0):
                del self.cached[n]


def read(path):
    with _connection(path) as connection:
        connection.execute("BEGIN")
        value, generation = _metadata(connection)
        pages = {}
        total = 0
        for number, payload, size, digest in connection.execute(
                "SELECT number, payload, raw_size, digest FROM pages ORDER BY number"):
            if type(size) is not int or not 0 < size <= min(MAX_PAGE_RAW_BYTES, MAX_DATABASE_RAW_BYTES - total):
                raise core.ComparisonError("프로젝트 파일이 허용 크기를 넘습니다.")
            total += size
            inflater = zlib.decompressobj(16 + zlib.MAX_WBITS)
            try:
                content = inflater.decompress(payload, size + 1)
            except (zlib.error, TypeError) as exc:
                raise core.ComparisonError("쪽별 압축 기록이 손상되었습니다.") from exc
            if len(content) > size or not inflater.eof or inflater.unused_data:
                raise core.ComparisonError("쪽별 압축 기록의 크기가 올바르지 않습니다.")
            if len(content) != size or hashlib.sha256(content).hexdigest() != digest:
                raise core.ComparisonError("쪽별 저장 기록의 크기·해시가 일치하지 않습니다.")
            pages[str(number)] = _json(content)
        if "storage_raw_bytes" in value and value["storage_raw_bytes"] != total:
            raise core.ComparisonError("쪽별 저장 기록의 전체 크기가 일치하지 않습니다.")
        value["pages"] = pages
        return value, _token(path, generation)


def _guard(project, target, recovery):
    journal = Path(str(target) + "-journal")
    # SQLite opens this deterministic sidecar during a write. A hard link or
    # symlink there must not let journal creation truncate a protected file.
    for original in (project.source_path, project.project_path, target):
        if original is not None and core._same_file(journal, original):
            raise core.ComparisonError("복구 저널 경로가 원본이나 프로젝트 파일을 가리킵니다.")
    for original in (project.source_path, project.project_path if recovery else None):
        if original is not None and core._same_file(target, original):
            raise core.ComparisonError("원본이나 프로젝트 파일을 덮어쓸 수 없습니다.")
    if not recovery:
        project.validate_source()


def _project_metadata(project, recovery):
    metadata = {"format": core.FORMAT, "version": core.VERSION, "document_id": project._id,
                "source": {"path": str(project.source_path), "sha256": project.source_sha256,
                           "page_count": project.page_count, "kind": project.kind}}
    if recovery:
        metadata["recovery"] = {"source_validation": "required_before_ocr_or_export", "at": core._now()}
    return metadata


def _write_rows(connection, project, numbers, recovery):
    previous_metadata = connection.execute("SELECT payload FROM metadata WHERE id=1").fetchone()
    total = _json(previous_metadata[0]).get("storage_raw_bytes") if previous_metadata else 0
    if total is None:  # early development snapshots did not cache the aggregate
        total = connection.execute("SELECT coalesce(sum(raw_size), 0) FROM pages").fetchone()[0]
    for number in numbers:
        content = json.dumps(project._pages[number], ensure_ascii=False,
                             separators=(",", ":"), allow_nan=False).encode("utf-8")
        previous = connection.execute("SELECT raw_size FROM pages WHERE number=?", (number,)).fetchone()
        total += len(content) - (previous[0] if previous else 0)
        if len(content) > MAX_PAGE_RAW_BYTES or total > MAX_DATABASE_RAW_BYTES:
            raise core.ComparisonError("프로젝트가 허용 크기를 넘습니다.")
        packed = gzip.compress(content, compresslevel=1, mtime=0)
        connection.execute("INSERT OR REPLACE INTO pages VALUES (?, ?, ?, ?)",
                           (number, packed, len(content), hashlib.sha256(content).hexdigest()))
        if isinstance(project._pages, CheckpointPages):
            project._pages.trim()
    metadata = _project_metadata(project, recovery)
    metadata["storage_raw_bytes"] = total
    generation = uuid.uuid4().hex
    connection.execute("INSERT OR REPLACE INTO metadata VALUES (1, ?, ?)",
                       (json.dumps(metadata, ensure_ascii=False, allow_nan=False), generation))
    return generation


def save(project, target, *, recovery=False):
    _guard(project, target, recovery)
    known = (project._recovery_sha256.get(target) if recovery else
             project._disk_sha256 if project.project_path == target else None)
    saved = project._recovery_versions.get(target, {}) if recovery else project._saved_page_versions
    incremental = isinstance(known, str) and known.startswith("sqlite:") and target.exists()
    numbers = [n for n in sorted(project._pages)
               if not incremental or project._page_versions.get(n, 0) != saved.get(n, -1)]
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix="." + target.name + ".", suffix=".tmp", dir=target.parent)
    os.close(descriptor)
    temporary = Path(name)
    try:
        with _connection(temporary, create=True) as connection:
            if incremental:
                # Clone a consistent SQLite snapshot; never modify the published
                # project in place. Only changed page rows are encoded below.
                with _connection(target) as original:
                    original.execute("BEGIN")
                    value, generation = _metadata(original)
                    if value.get("document_id") != project._id or _token(target, generation) != known:
                        raise core.ComparisonError("다른 창에서 프로젝트가 변경되었습니다. 다른 이름으로 저장하세요.")
                    if not numbers and value.get("source") == _project_metadata(project, False)["source"]:
                        return known
                    original.backup(connection)
            else:
                connection.execute("PRAGMA page_size=4096")
                connection.execute("PRAGMA journal_mode=DELETE")
                connection.execute(f"PRAGMA application_id={APPLICATION_ID}")
                connection.execute("PRAGMA user_version=1")
                for sql in SCHEMA.values():
                    connection.execute(sql)
            page_size=connection.execute("PRAGMA page_size").fetchone()[0]
            connection.execute(f"PRAGMA max_page_count={max(1, MAX_DATABASE_BYTES // page_size)}")
            connection.execute("BEGIN IMMEDIATE")
            generation = _write_rows(connection, project, numbers, recovery)
            _commit(connection)
            if connection.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
                raise ProjectStorageError("저장본 무결성 검사 실패. 기존 프로젝트는 교체하지 않았습니다.")
        if temporary.stat().st_size > MAX_DATABASE_BYTES:
            raise core.ComparisonError("프로젝트가 허용 크기를 넘습니다.")
        with temporary.open("r+b") as stream:
            os.fsync(stream.fileno())
        # Reopen the bytes that will be published. The writer's schema cache
        # cannot detect every form of on-disk corruption in its own connection.
        with _connection(temporary) as verification:
            if verification.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
                raise ProjectStorageError("저장본 무결성 검사 실패. 기존 프로젝트는 교체하지 않았습니다.")

        def verify():
            _guard(project, target, recovery)
            project._check_existing(target, check_current=not recovery)

        core._replace_file(target, temporary, protected=(project.source_path,
                           project.project_path if recovery else None), before_replace=verify)
        return _token(target, generation)
    finally:
        temporary.unlink(missing_ok=True)
