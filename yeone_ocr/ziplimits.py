"""Size limits for ZIP-based documents (HWPX, DOCX) the person picks.

A small crafted file can declare gigabytes inside. zipfile stops every entry at its declared
size, so checking the declared sizes before reading bounds the memory a document can take.
Every path that opens a user's HWPX goes through check_archive (Codex review, 1.2.17)."""
from __future__ import annotations

import zipfile

MAX_ARCHIVE_BYTES = 1_000_000_000      # all entries together, uncompressed
MAX_MEMBER_BYTES = 256_000_000         # one XML part read on its own


class ArchiveTooLarge(ValueError):
    pass


def check_archive(source) -> None:
    """Refuse a ZIP whose entries expand beyond the limit. `source` is a path or a ZipFile."""
    if isinstance(source, zipfile.ZipFile):
        infos = source.infolist()
    else:
        with zipfile.ZipFile(source) as archive:
            infos = archive.infolist()
    if sum(info.file_size for info in infos) > MAX_ARCHIVE_BYTES:
        raise ArchiveTooLarge("문서를 풀면 너무 커서(1 GB 초과) 열지 않습니다.")


def read_member(archive: zipfile.ZipFile, name: str) -> bytes:
    """One entry, refused when it declares more than MAX_MEMBER_BYTES."""
    if archive.getinfo(name).file_size > MAX_MEMBER_BYTES:
        raise ArchiveTooLarge(f"문서 안의 {name} 가 너무 커서 읽지 않습니다.")
    return archive.read(name)
