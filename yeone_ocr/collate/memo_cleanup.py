"""Remove only this app's bookmark-correction memos from an existing copy.

The archive writer preserves every member outside changed section XML. The
manual edit journal is checked before and after removal and is never rewritten.
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
from dataclasses import dataclass
import os
from pathlib import Path
from types import SimpleNamespace
import zipfile

from lxml import etree

from .hwpx_edit import HP, MEMO_HEADS, legend_head, paragraph_text, remove_collate_memos
from .section_edit import (
    _Archive, _fingerprint, _path, _sha, _signature, _stage_bytes,
    manual_history_path, manual_protected_paragraphs,
)
from ..grades import COLLATE_LEGEND


class MemoCleanupError(ValueError):
    """Cleanup could not be completed while preserving the current copy."""


@dataclass(frozen=True)
class CleanupResult:
    removed: int
    skipped: int
    backup_path: Path | None
    corrected_path: Path


def _xml(element) -> bytes:
    return etree.tostring(element, method="c14n")


def _memo_inventory(archive: _Archive) -> Counter:
    return Counter(_xml(element)
                   for root in archive.roots.values()
                   for element in root.iter(HP + "fieldBegin")
                   if element.get("type") == "MEMO")


def _owned_memo(element) -> bool:
    author = element.find(HP + "parameters/" + HP + "stringParam[@name='Author']")
    if author is None or author.text != "YeonE_OCR":
        return False
    sub = element.find(HP + "subList")
    text = "".join(paragraph_text(p) for p in sub if p.tag == HP + "p").lstrip() if sub is not None else ""
    return text.startswith((*MEMO_HEADS, legend_head(COLLATE_LEGEND)))


def _validate_removed_only(before: _Archive, after: _Archive, removed: int) -> None:
    """Reconstruct the expected XML by removing the deleted owned pairs only."""
    if len(before.paragraphs) != len(after.paragraphs):
        raise MemoCleanupError("메모 정리 중 문단 수가 달라져 저장하지 않았습니다.")
    for (part_a, a), (part_b, b) in zip(before.locations, after.locations):
        if (part_a != part_b or _signature(a) != _signature(b)
                or paragraph_text(a) != paragraph_text(b)):
            raise MemoCleanupError("메모 정리 중 본문·책갈피 위치가 달라져 저장하지 않았습니다.")
    old_memos, new_memos = _memo_inventory(before), _memo_inventory(after)
    if new_memos - old_memos or sum((old_memos - new_memos).values()) != removed:
        raise MemoCleanupError("메모 정리 결과를 검증하지 못해 저장하지 않았습니다.")
    expected = {name: deepcopy(root) for name, root in before.roots.items()}
    remaining = new_memos.copy()
    removed_ids = set()
    for root in expected.values():
        for begin in list(root.iter(HP + "fieldBegin")):
            if begin.get("type") != "MEMO":
                continue
            key = _xml(begin)
            if remaining[key]:
                remaining[key] -= 1
                continue
            if not _owned_memo(begin):
                raise MemoCleanupError("사용자 메모를 보존할 수 없어 저장하지 않았습니다.")
            control = begin.getparent()
            if control.tag != HP + "ctrl" or len(control) != 1:
                raise MemoCleanupError("메모 구조를 검증하지 못해 저장하지 않았습니다.")
            removed_ids.add((begin.get("id"), begin.get("fieldid")))
            control.getparent().remove(control)
    for root in expected.values():
        for end in list(root.iter(HP + "fieldEnd")):
            if (end.get("beginIDRef"), end.get("fieldid")) in removed_ids:
                control = end.getparent()
                if control.tag != HP + "ctrl" or len(control) != 1:
                    raise MemoCleanupError("메모 끝 구조를 검증하지 못해 저장하지 않았습니다.")
                control.getparent().remove(control)
    if expected.keys() != after.roots.keys() or any(
            _xml(root) != _xml(after.roots[name]) for name, root in expected.items()):
        raise MemoCleanupError("메모 이외의 문서 구조가 달라져 저장하지 않았습니다.")


def clean_corrected_memos(original_hwpx) -> CleanupResult:
    """Clean an existing ``<original>_교정.hwpx`` with backup and atomic publish."""
    from .run import corrected_path

    stages: list[Path] = []
    try:
        source = Path(original_hwpx).resolve()
        if source.suffix.lower() != ".hwpx" or source.stem.endswith("_교정"):
            raise MemoCleanupError("교정본 대신 원본 HWPX를 선택해 주십시오.")
        target = Path(corrected_path(str(source)))
        if not source.is_file():
            raise MemoCleanupError("원본 HWPX를 찾을 수 없습니다.")
        if not target.is_file():
            raise MemoCleanupError("기존 교정본이 없습니다. 먼저 책갈피 OCR로 교정본을 만들어 주십시오.")
        if (target.is_symlink() or _path(source) == _path(target)
                or os.path.samefile(source, target)):
            raise MemoCleanupError("교정본과 원본이 같은 파일이거나 연결된 경로입니다. 정리하지 않았습니다.")
        target = target.resolve()
        history_path = manual_history_path(target)
        source_bytes, target_bytes = source.read_bytes(), target.read_bytes()
        history_bytes = history_path.read_bytes() if history_path.exists() else None
        snapshots = ((source, _sha(source_bytes)), (target, _sha(target_bytes)),
                     (history_path, _sha(history_bytes) if history_bytes is not None else None))

        def check_current():
            for path, digest in snapshots:
                if _fingerprint(path) != digest:
                    raise MemoCleanupError(f"정리 중 파일이 외부에서 변경되었습니다: {path.name}\n교정본을 덮어쓰지 않았습니다.")

        before = _Archive(target_bytes)
        protected = manual_protected_paragraphs(target, before.paragraphs)
        cleaned = _Archive(target_bytes)
        book = SimpleNamespace(paragraphs=[SimpleNamespace(element=p) for p in cleaned.paragraphs])
        result = remove_collate_memos(book)
        _validate_removed_only(before, cleaned, result.removed)
        if manual_protected_paragraphs(target, cleaned.paragraphs) != protected:
            raise MemoCleanupError("수동 교정 문단의 보호를 확인하지 못해 저장하지 않았습니다.")
        check_current()
        if not result.removed:
            return CleanupResult(0, result.skipped, None, target)
        changed = {name for name in before.roots if _xml(before.roots[name]) != _xml(cleaned.roots[name])}
        stage = _stage_bytes(target, b"")
        stages.append(stage)
        cleaned.write(stage, changed)
        with stage.open("r+b") as stream:
            os.fsync(stream.fileno())
        verified = _Archive(stage.read_bytes())
        _validate_removed_only(before, verified, result.removed)
        if (manual_protected_paragraphs(target, verified.paragraphs) != protected
                or any(verified.entries[name] != data for name, data in before.entries.items()
                       if name not in changed)):
            raise MemoCleanupError("저장할 교정본의 보존 검증에 실패했습니다.")
        check_current()
        backup = target.with_suffix(".bak.hwpx")
        backup_stage = _stage_bytes(backup, target_bytes)
        stages.append(backup_stage)
        history_backup = history_path.with_suffix(".bak.json")
        history_backup_stage = None
        if history_bytes is not None:
            history_backup_stage = _stage_bytes(history_backup, history_bytes)
            stages.append(history_backup_stage)
        check_current()
        os.replace(backup_stage, backup)
        if history_backup_stage is not None:
            check_current()
            os.replace(history_backup_stage, history_backup)
        check_current()
        os.replace(stage, target)
        return CleanupResult(result.removed, result.skipped, backup, target)
    except MemoCleanupError:
        raise
    except (OSError, ValueError, KeyError, zipfile.BadZipFile, etree.XMLSyntaxError) as exc:
        raise MemoCleanupError(f"기존 앱 메모를 정리하지 못했습니다: {exc}") from exc
    finally:
        for stage in stages:
            stage.unlink(missing_ok=True)
