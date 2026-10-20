"""구획 결과의 문서·위치 연결. 조회는 기존 파일을 읽기만 한다."""
from __future__ import annotations

import hashlib
import json
import os
import re
from functools import lru_cache
from pathlib import Path

from .sections import normalize_name
from . import zh


def normalized_path(path) -> str:
    return os.path.normcase(str(Path(path).resolve()))


@lru_cache(maxsize=32)
def _digest(path: str, size: int, modified: int) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def file_identity(path) -> dict:
    """같은 경로에서 파일을 교체한 경우도 구분한다. 큰 PDF의 해시는 상태별로 재사용."""
    p = Path(path)
    result = {"path": normalized_path(p)}
    try:
        stat = p.stat()
        result.update(size=stat.st_size, sha256=_digest(result["path"], stat.st_size, stat.st_mtime_ns))
    except OSError:
        pass
    return result


def _name_key(name: str) -> str:
    return zh.compare_key(normalize_name(name))


def result_binding(source_pdf, original_hwpx, section_name: str, section_start: int,
                   section_end: int | None = None) -> dict:
    pdf, hwpx = file_identity(source_pdf), file_identity(original_hwpx)
    stable = json.dumps([pdf, hwpx, section_start, section_end, _name_key(section_name)], ensure_ascii=False, sort_keys=True)
    return {"schema_version": 2, "source_pdf": pdf, "source_hwpx": hwpx,
            "section_id": hashlib.sha256(stable.encode("utf-8")).hexdigest()[:20]}


def report_stem(binding: dict, section_name: str, section_start: int) -> str:
    name = re.sub(r'[\\/:*?"<>|\s]+', "_", section_name)[:40] or "구획"
    return f"{name}__p{section_start}_{binding['section_id']}"


def _legacy_source_matches(work_dir: Path, source_pdf) -> bool:
    """옛 보고는 작업 폴더의 source.json 지문으로 PDF를 검증한다."""
    try:
        manifest = json.loads((work_dir / "source.json").read_text(encoding="utf-8-sig"))
        identity = file_identity(source_pdf)
        if normalized_path(manifest["path"]) != identity["path"] or not identity.get("sha256"):
            return False
        digest = hashlib.sha256()
        digest.update(Path(source_pdf).name.encode("utf-8") + b"\0")
        digest.update(bytes.fromhex(identity["sha256"]))
        return manifest.get("sha256") == digest.hexdigest()
    except (OSError, ValueError, KeyError, TypeError):
        return False


def locate_section_report(work_dir, source_pdf, original_hwpx, section_name: str,
                          section_start: int, section_end: int | None = None) -> Path | None:
    """선택한 문서·구획과 검증된 HTML만 반환한다. 최신 시각은 일치 후보끼리만 비교."""
    folder = Path(work_dir) / "collate"
    if not folder.is_dir():
        return None
    expected = result_binding(source_pdf, original_hwpx, section_name, section_start, section_end)
    if not expected["source_pdf"].get("sha256") or not expected["source_hwpx"].get("sha256"):
        return None
    original = Path(original_hwpx)
    from .run import corrected_path
    allowed_hwpx = {normalized_path(original), normalized_path(corrected_path(str(original)))}
    legacy_pdf_ok = None
    found = []
    for meta in folder.glob("*.json"):
        report = meta.with_suffix(".html")
        if not report.is_file():
            continue
        try:
            data = json.loads(meta.read_text(encoding="utf-8-sig"))
            if (_name_key(data.get("section", "")) != _name_key(section_name)
                    or data.get("hwpx_paras", [None])[0] != section_start):
                continue
            if section_end is not None and data.get("hwpx_paras", [None, None])[1] != section_end:
                continue
            if data.get("schema_version") == 2:
                if data.get("source_pdf") != expected["source_pdf"] or data.get("source_hwpx") != expected["source_hwpx"]:
                    continue
            else:
                if not data.get("hwpx_file") or normalized_path(data["hwpx_file"]) not in allowed_hwpx:
                    continue
                if legacy_pdf_ok is None:
                    legacy_pdf_ok = _legacy_source_matches(Path(work_dir), source_pdf)
                if not legacy_pdf_ok:
                    continue
            found.append(report)
        except (OSError, ValueError, TypeError, IndexError, AttributeError):
            continue
    return max(found, key=lambda p: p.stat().st_mtime_ns) if found else None
