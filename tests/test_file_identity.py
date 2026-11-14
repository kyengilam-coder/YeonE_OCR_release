"""같은 파일 판정: 네트워크 드라이브(SMB·NAS)는 모든 파일의 st_ino 를 0 으로 주어 os.path.samefile 이 서로 다른 파일을 같다고 했다.
그래서 Z: 드라이브의 책에서 [한 번에 인식]이 '프로젝트와 원본은 다른 파일이어야 합니다'로 멈췄다."""
import os
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ab_ocr import fileid
from ab_ocr.fileid import same_file


def without_file_numbers():
    """fileid 안에서만: 네트워크 드라이브처럼 st_ino 가 늘 0 (다른 코드의 os.stat 은 그대로)."""
    def stat(path):
        value = os.stat(path)
        return SimpleNamespace(st_dev=value.st_dev, st_ino=0, st_size=value.st_size, st_mtime_ns=value.st_mtime_ns)
    return patch.object(fileid, "os", SimpleNamespace(stat=stat))


def test_ordinary_file_system_uses_file_numbers(tmp_path):
    a, b = tmp_path / "a.pdf", tmp_path / "b.json"
    a.write_bytes(b"pdf")
    b.write_bytes(b"json")
    assert same_file(a, a) and same_file(a, tmp_path / "." / "a.pdf")
    assert not same_file(a, b)
    assert not same_file(a, tmp_path / "missing.pdf")


@pytest.mark.skipif(not hasattr(os, "link"), reason="hard links")
def test_hard_link_is_the_same_file(tmp_path):
    a = tmp_path / "a.pdf"
    a.write_bytes(b"pdf")
    try:
        os.link(a, tmp_path / "alias.pdf")
    except OSError:
        pytest.skip("this file system has no hard links")
    assert same_file(a, tmp_path / "alias.pdf")


def test_network_drive_without_file_numbers_tells_different_files_apart(tmp_path):
    source, project = tmp_path / "책.pdf", tmp_path / "책.yeone-compare.json"
    source.write_bytes(b"%PDF-1.7 synthetic")
    project.write_text("{}", encoding="utf-8")
    with without_file_numbers():
        assert not same_file(source, project)
        assert same_file(source, source)
        # the same file reached through another path keeps size and modification time, so it still matches
        copy = tmp_path / "copy.pdf"
        copy.write_bytes(source.read_bytes())
        os.utime(copy, ns=(os.stat(source).st_atime_ns, os.stat(source).st_mtime_ns))
        assert same_file(source, copy)


def test_comparison_project_saves_next_to_its_source_on_such_a_drive(tmp_path):
    import pymupdf
    from yeone_ocr.comparison import ComparisonProject
    source = tmp_path / "개보본초.ocr.pdf"
    with pymupdf.open() as document:
        document.new_page(width=200, height=200)
        document.save(source)
    with without_file_numbers():
        project = ComparisonProject.open(source)
        target = tmp_path / "work" / "comparison" / "local-first.yeone-compare.json"
        target.parent.mkdir(parents=True)
        project.save(target)
        assert ComparisonProject.load(target, source_path=source).source_path == source.resolve()
