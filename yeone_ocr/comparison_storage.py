"""Single-file, transactional storage for long comparison projects.

Each row contains one complete page (including all historical evidence). Only
modified rows are encoded. DELETE journaling leaves a portable single file after
commit and recovers an interrupted transaction on the next open. No WAL sidecar
or permanent external page directory is required.
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

MAGIC = b"SQLite format 3\x00"
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
        connection.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, core.MAX_BYTES)
        if not create:
            if Path(path).stat().st_size > core.MAX_BYTES:
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
        raise core.ComparisonError("증분 프로젝트를 읽거나 저장할 수 없습니다. 정상 저장본이나 복구본을 확인하세요.") from exc
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
    if type(total) is not int or not 0 <= total <= core.MAX_JSON_BYTES:
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


def read(path):
    with _connection(path) as connection:
        connection.execute("BEGIN")
        value, generation = _metadata(connection)
        pages = {}
        total = 0
        for number, payload, size, digest in connection.execute(
                "SELECT number, payload, raw_size, digest FROM pages ORDER BY number"):
            if type(size) is not int or not 0 < size <= core.MAX_JSON_BYTES - total:
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
        if total > core.MAX_JSON_BYTES:
            raise core.ComparisonError("프로젝트가 허용 크기를 넘습니다.")
        packed = gzip.compress(content, compresslevel=1, mtime=0)
        connection.execute("INSERT OR REPLACE INTO pages VALUES (?, ?, ?, ?)",
                           (number, packed, len(content), hashlib.sha256(content).hexdigest()))
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
    if incremental:
        writing = False
        try:
            with _connection(target) as connection:
                connection.execute("BEGIN IMMEDIATE")
                value, generation = _metadata(connection)
                if value.get("document_id") != project._id or _token(target, generation) != known:
                    raise core.ComparisonError("다른 창에서 프로젝트가 변경되었습니다. 다른 이름으로 저장하세요.")
                before = target.stat()
                if numbers or value.get("source") != _project_metadata(project, False)["source"]:
                    page_size = connection.execute("PRAGMA page_size").fetchone()[0]
                    connection.execute(f"PRAGMA max_page_count={max(1, core.MAX_BYTES // page_size)}")
                    writing = True
                    generation = _write_rows(connection, project, numbers, recovery)
                _guard(project, target, recovery)
                after = target.stat()
                if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
                    raise core.ComparisonError("저장 중 프로젝트 파일이 교체되었습니다.")
                _commit(connection)
                return _token(target, generation)
        except BaseException:
            # A rolled-back cache spill may change file timestamps even though
            # SQLite restored every row. Retain our pending edits and allow a
            # retry, but never adopt a different committed generation.
            if writing:
                try:
                    identifier, current = identity(target)
                    if identifier == project._id and current.split(":")[1] == known.split(":")[1]:
                        if recovery:
                            project._recovery_sha256[target] = current
                        else:
                            project._disk_sha256 = current
                except (core.ComparisonError, OSError):
                    pass
            raise

    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix="." + target.name + ".", suffix=".tmp", dir=target.parent)
    os.close(descriptor)
    temporary = Path(name)
    try:
        with _connection(temporary, create=True) as connection:
            connection.execute("PRAGMA page_size=4096")
            connection.execute("PRAGMA journal_mode=DELETE")
            connection.execute(f"PRAGMA application_id={APPLICATION_ID}")
            connection.execute("PRAGMA user_version=1")
            connection.execute(f"PRAGMA max_page_count={max(1, core.MAX_BYTES // 4096)}")
            for sql in SCHEMA.values():
                connection.execute(sql)
            connection.execute("BEGIN IMMEDIATE")
            generation = _write_rows(connection, project, numbers, recovery)
            connection.commit()
        if temporary.stat().st_size > core.MAX_BYTES:
            raise core.ComparisonError("프로젝트가 허용 크기를 넘습니다.")
        with temporary.open("r+b") as stream:
            os.fsync(stream.fileno())

        def verify():
            _guard(project, target, recovery)
            project._check_existing(target, check_current=not recovery)

        core._replace_file(target, temporary, protected=(project.source_path,
                           project.project_path if recovery else None), before_replace=verify)
        return _token(target, generation)
    finally:
        temporary.unlink(missing_ok=True)
