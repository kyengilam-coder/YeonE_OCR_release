"""Selected-section manual editing with source preservation and OCR protection.

Only text slots in selected body paragraphs are edited. Existing ZIP members
outside the changed section XML remain byte-identical. Manual history is a
durable sibling file so subsequent OCR runs can protect manually edited text.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from difflib import SequenceMatcher
import hashlib
import io
import json
import os
from pathlib import Path
import re
import tempfile
import time
import zipfile

from lxml import etree

from .hwpx_edit import HP, HH, Editor, _local, _slots, _tag_of, paragraph_text
from .sections import HwpxBook, normalize_name, resolve_anchor_index
from . import zh


class SectionEditError(ValueError):
    """The requested edit cannot safely preserve document structure."""


class ConflictError(SectionEditError):
    """An input changed since this editing session was opened."""


class ManualProtectionError(SectionEditError):
    """Manual edit history cannot safely be applied to the current document."""


@dataclass(frozen=True)
class ParagraphEdit:
    index: int
    original_text: str
    corrected_text: str
    editable: bool
    readonly_reason: str = ""


@dataclass(frozen=True)
class SaveResult:
    path: Path
    saved_count: int
    backup_path: Path | None
    reports_stale: bool
    learning_count: int = 0
    learning_path: Path | None = None
    learning_warning: str = ""

    @property
    def corrected_path(self) -> Path:
        return self.path


def _path(path) -> str:
    return os.path.normcase(str(Path(path).resolve()))


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _fingerprint(path: Path) -> str | None:
    try:
        with path.open("rb") as stream:
            digest = hashlib.sha256()
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
            return digest.hexdigest()
    except FileNotFoundError:
        return None


def _text_hash(text: str) -> str:
    return _sha(text.encode("utf-8"))


def manual_history_path(target_path) -> Path:
    return Path(target_path).with_suffix(".manual.json")


def _element(paragraph):
    return getattr(paragraph, "element", paragraph)


def _signature(paragraph) -> str:
    """Paragraph identity survives OCR run splitting and added memo controls."""
    element = _element(paragraph)
    # Hancom users may rename a bookmark without moving its paragraph. Names
    # are validated separately through canonical bookmark/heading aliases.
    values = [sorted(element.attrib.items()),
              [b.get("id", "") for b in element.iter(HP + "bookmark")]]
    return _sha(json.dumps(values, ensure_ascii=False).encode("utf-8"))


def _load_history(target: Path, data: bytes | None) -> dict:
    if data is None:
        return {"schema_version": 1, "target": _path(target), "paragraph_count": None, "paragraphs": {}}
    try:
        history = json.loads(data.decode("utf-8-sig"))
        if (not isinstance(history, dict) or history.get("schema_version") != 1
                or history.get("target") != _path(target) or type(history.get("paragraph_count")) is not int
                or history["paragraph_count"] < 1 or not isinstance(history.get("paragraphs"), dict)):
            raise ValueError
        for key, record in history["paragraphs"].items():
            if (not key.isdigit() or int(key) >= history["paragraph_count"] or not isinstance(record, dict)
                    or not isinstance(record.get("signature"), str) or not re.fullmatch("[0-9a-f]{64}", record["signature"])
                    or not isinstance(record.get("text_hashes"), list) or not record["text_hashes"]
                    or any(not isinstance(h, str) or not re.fullmatch("[0-9a-f]{64}", h) for h in record["text_hashes"])):
                raise ValueError
        return history
    except (ValueError, UnicodeError, TypeError, AttributeError) as exc:
        raise ManualProtectionError("수동 교정 기록을 확인할 수 없습니다. 기록을 복구하거나 문서를 다시 확인한 뒤 진행하십시오.") from exc


def manual_protected_paragraphs(target_path, paragraphs=None) -> set[int]:
    """Validated manual paragraphs for a loaded ``HwpxBook.paragraphs``.

    Missing history means no manual edits. Malformed history or a changed
    protected paragraph fails closed rather than permitting an OCR overwrite.
    """
    target = Path(target_path)
    try:
        data = manual_history_path(target).read_bytes()
    except FileNotFoundError:
        return set()
    history = _load_history(target, data)
    if paragraphs is None:
        paragraphs = _Archive(target.read_bytes()).paragraphs
    if len(paragraphs) != history["paragraph_count"]:
        raise ManualProtectionError("수동 교정 이후 문단 수가 달라졌습니다. 구획의 위치를 다시 확인하십시오.")
    protected = set()
    for key, record in history["paragraphs"].items():
        index = int(key)
        paragraph = _element(paragraphs[index])
        if (_signature(paragraph) != record["signature"]
                or _text_hash(paragraph_text(paragraph)) not in record["text_hashes"]):
            raise ManualProtectionError(f"수동 교정한 {index + 1}번 문단이 외부에서 변경되었습니다. 자동 교정 전에 해당 문단을 확인하십시오.")
        protected.add(index)
    return protected


class _Archive:
    """Parse the same direct body paragraph sequence as python-hwpx."""
    def __init__(self, data: bytes):
        from hwpx.document import HwpxDocument
        self.data = data
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            self.infos = archive.infolist()
            if len({i.filename for i in self.infos}) != len(self.infos):
                raise SectionEditError("문서 ZIP에 중복된 항목이 있어 편집할 수 없습니다.")
            self.entries = {i.filename: archive.read(i.filename) for i in self.infos}
            self.comment = archive.comment
        doc = HwpxDocument.open(data)
        try:
            names = [section.part_name for section in doc.sections]
            self.style_names = HwpxBook._style_names(type("BookView", (), {"doc": doc})())
        finally:
            doc.close()
        parser = etree.XMLParser(resolve_entities=False, no_network=True, remove_blank_text=False)
        self.roots = {name: etree.fromstring(self.entries[name], parser) for name in names}
        self.locations = [(name, p) for name in names for p in self.roots[name] if p.tag == HP + "p"]
        self.paragraphs = [p for _, p in self.locations]

    def anchors(self, styles=None):
        book = object.__new__(HwpxBook)
        book.style_names = self.style_names
        book.paragraphs = [type("ParagraphView", (), {"element": p, "text": paragraph_text(p)})() for p in self.paragraphs]
        book._texts = None
        return book, book.anchors(styles=styles)

    def write(self, path: Path, changed: set[str]) -> None:
        with zipfile.ZipFile(path, "w") as archive:
            archive.comment = self.comment
            for info in self.infos:
                data = self.entries[info.filename]
                if info.filename in changed:
                    data = etree.tostring(self.roots[info.filename], encoding="utf-8", xml_declaration=True)
                archive.writestr(info, data)
        with zipfile.ZipFile(path) as archive:
            if archive.testzip() is not None:
                raise SectionEditError("저장할 문서의 ZIP 검증에 실패했습니다.")


def _readonly_reason(paragraph, heading: bool) -> str:
    if heading:
        return "구획 위치를 유지하기 위해 제목·책갈피 문단은 읽기 전용입니다."
    if not _slots(paragraph):
        return "편집할 본문 글 조각이 없는 문단입니다."
    memo_ids = {f.get("id") for f in paragraph.iter(HP + "fieldBegin") if f.get("type") == "MEMO"}
    for run in paragraph:
        if _local(run.tag) != "run":
            continue
        for child in run:
            kind = _local(child.tag)
            if kind == "t":
                if any(_local(sub.tag) not in ("markpenBegin", "markpenEnd") for sub in child):
                    return "탭·줄바꿈·특수 글자 요소가 있는 문단은 구조 보존을 위해 읽기 전용입니다."
            elif kind == "ctrl":
                for control in child:
                    ck = _local(control.tag)
                    if ck == "fieldBegin" and control.get("type") == "MEMO":
                        continue
                    if ck == "fieldEnd" and control.get("beginIDRef") in memo_ids:
                        continue
                    if ck == "bookmark":
                        continue
                    return "필드·각주·그림·표가 있는 문단은 구조 보존을 위해 읽기 전용입니다."
            elif kind not in ("bookmark",):
                return "필드·각주·그림·표가 있는 문단은 구조 보존을 위해 읽기 전용입니다."
    return ""


def _replace_text(paragraph, text: str) -> None:
    """Apply diff hunks backwards to existing slots, retaining all XML nodes."""
    before = paragraph_text(paragraph)
    if any(c in text for c in "\r\n\t") or any(ord(c) < 32 for c in text):
        raise SectionEditError("문단 안에는 줄바꿈·탭·제어 문자를 새로 넣을 수 없습니다. 문단별로 글자만 고쳐 주십시오.")
    for op, start, end, j, k in reversed(SequenceMatcher(None, before, text, autojunk=False).get_opcodes()):
        if op == "equal":
            continue
        slots = _slots(paragraph)
        if not slots:
            raise SectionEditError("문단의 글 조각을 찾지 못했습니다.")
        affected = [slot for slot in slots if slot.end > start and slot.start < end]
        if end - start == k - j:
            for slot in affected:
                lo, hi = max(start, slot.start), min(end, slot.end)
                slot.text = slot.text[:lo - slot.start] + text[j + lo - start:j + hi - start] + slot.text[hi - slot.start:]
            continue
        if len({slot.run.get("charPrIDRef") for slot in affected}) > 1 and text[j:k]:
            raise SectionEditError("서로 다른 글자 서식에 걸친 길이 변경은 한 번에 저장할 수 없습니다. 서식별로 나누어 고쳐 주십시오.")
        slot = Editor._locate(slots, start)
        at = max(0, min(len(slot.text), start - slot.start))
        # Existing editor helper deletes text only; controls and style runs stay.
        Editor._delete_range(None, paragraph, start, end)
        slot.text = slot.text[:at] + text[j:k] + slot.text[at:]
    if paragraph_text(paragraph) != text:
        raise SectionEditError("문단의 글자 위치를 안전하게 맞추지 못했습니다.")
    for child in list(paragraph):
        if _local(child.tag) in ("linesegarray", "lineSegArray"):
            paragraph.remove(child)


def _clear_known_grades(original, corrected, source_archive: _Archive, target_archive: _Archive) -> None:
    """Restore tagged OCR run styles only when the source style is unambiguous.

    A uniformly styled source paragraph supplies its existing style ID. Mixed
    source formatting is retained as-is rather than guessing which style to use.
    Memo controls and their historical evidence are always retained.
    """
    source_styles = {s.run.get("charPrIDRef", "0") for s in _slots(original) if s.text}
    if len(source_styles) != 1:
        return
    base = next(iter(source_styles))
    try:
        src = etree.fromstring(source_archive.entries["Contents/header.xml"])
        dst = etree.fromstring(target_archive.entries["Contents/header.xml"])
        original_style = src.find(f".//{HH}charPr[@id='{base}']")
        target_style = dst.find(f".//{HH}charPr[@id='{base}']")
        if original_style is None or target_style is None or etree.tostring(original_style) != etree.tostring(target_style):
            return
        styles = {e.get("id"): e for e in dst.iter(HH + "charPr")}
        for run in corrected:
            if _local(run.tag) != "run":
                continue
            style = styles.get(run.get("charPrIDRef"))
            if style is not None and _tag_of(style) in {"#0B7A3B", "#1F4FBF", "#C00000"} and _tag_of(style) == (style.get("textColor") or "").upper():
                run.set("charPrIDRef", base)
    except (KeyError, etree.XMLSyntaxError):
        return


def _stage_bytes(destination: Path, data: bytes) -> Path:
    descriptor, filename = tempfile.mkstemp(prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent)
    path = Path(filename)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return path


class SectionEditSession:
    """A document snapshot. No HWPX handles remain open while the UI is shown."""
    @classmethod
    def open(cls, pdf, original_hwpx, section_name: str, start: int, end: int, *,
             heading_styles=None, anchor_source: str = "auto"):
        from .run import corrected_path
        self = cls()
        self.pdf_path, self.source_path = Path(pdf).resolve(), Path(original_hwpx).resolve()
        if self.source_path.stem.endswith("_교정"):
            raise SectionEditError("원본 HWPX를 선택해 주십시오. 교정본 자체를 원본으로 지정하면 원본 보존을 확인할 수 없습니다.")
        self.corrected_path = Path(corrected_path(str(self.source_path))).resolve()
        if (_path(self.source_path) == _path(self.corrected_path)
                or self.corrected_path.exists() and os.path.samefile(self.source_path, self.corrected_path)):
            raise SectionEditError("교정본과 원본이 같은 파일입니다. 별도의 원본을 선택해 주십시오.")
        self.section_name, self.start, self.end = section_name, start, end
        self._source_bytes = self.source_path.read_bytes()
        self._target_bytes = self.corrected_path.read_bytes() if self.corrected_path.exists() else None
        self._history_bytes = manual_history_path(self.corrected_path).read_bytes() if manual_history_path(self.corrected_path).exists() else None
        self._pdf_hash = _fingerprint(self.pdf_path)
        if self._pdf_hash is None:
            raise SectionEditError("원본 PDF를 찾을 수 없습니다.")
        source = _Archive(self._source_bytes)
        target = _Archive(self._target_bytes or self._source_bytes)
        source_book, anchors = source.anchors(heading_styles)
        target_book, target_anchors = target.anchors(heading_styles)
        if anchor_source == "pdf" or anchor_source == "auto" and not anchors:
            from ..pdfio import open_pdf
            from .run import index_both
            with open_pdf(str(self.pdf_path)) as doc:
                anchors = index_both(doc, source_book, "pdf", heading_styles)[0]
                target_anchors = index_both(doc, target_book, "pdf", heading_styles)[0]
        index = resolve_anchor_index(anchors, None, para=start, name=section_name)
        section = source_book.section(anchors, index)
        if type(start) is not int or type(end) is not int or section.start != start or section.end != end:
            raise SectionEditError("선택한 구획 범위가 문서와 다릅니다. 구획 목록을 다시 불러오십시오.")
        if len(source.paragraphs) != len(target.paragraphs) or len(anchors) != len(target_anchors):
            raise SectionEditError("원본과 교정본의 문단·구획 수가 다릅니다. 위치를 맞출 수 없어 편집을 중단합니다.")
        for a, b in zip(anchors, target_anchors):
            if a.para != b.para or a.level != b.level or not ({normalize_name(n) for n in a.names} & {normalize_name(n) for n in b.names}):
                raise SectionEditError("원본과 교정본의 제목 위치가 다릅니다. 구획을 다시 확인하십시오.")
        for (src_part, src), (dst_part, dst) in zip(source.locations, target.locations):
            if src_part != dst_part or _signature(src) != _signature(dst):
                raise SectionEditError("원본과 교정본의 문단 구조·위치가 다릅니다. 편집할 문단을 안전하게 대응할 수 없습니다.")
        history = _load_history(self.corrected_path, self._history_bytes)
        protected = manual_protected_paragraphs(self.corrected_path, target.paragraphs) if self._history_bytes else set()
        self._heads = {a.para for a in anchors}
        for i in range(start, end):
            old, current = paragraph_text(source.paragraphs[i]), paragraph_text(target.paragraphs[i])
            # Large unrelated paragraph changes often indicate reordered bodies
            # despite repeated paragraph IDs. Verified manual history is exempt.
            if i not in protected and old != current and max(len(old), len(current)) >= 12:
                left, right = zh.compare_key(normalize_name(old)), zh.compare_key(normalize_name(current))
                if SequenceMatcher(None, left, right, autojunk=False).ratio() < 0.45:
                    raise SectionEditError(f"{i + 1}번 문단의 원문과 교정본 내용이 크게 달라 위치 대응을 확인할 수 없습니다.")
        self._source = source
        self._history = history
        self._refresh_paragraphs(target)
        self._assert_unchanged()
        return self

    def _refresh_paragraphs(self, target: _Archive) -> None:
        self.paragraphs = []
        for i in range(self.start, self.end):
            reason = _readonly_reason(target.paragraphs[i], i in self._heads)
            self.paragraphs.append(ParagraphEdit(i, paragraph_text(self._source.paragraphs[i]),
                                                paragraph_text(target.paragraphs[i]), not reason, reason))

    def _assert_unchanged(self, *, published_history: bytes | None = None) -> None:
        expected_history = self._history_bytes if published_history is None else published_history
        paths = [(self.source_path, _sha(self._source_bytes)),
                 (self.corrected_path, _sha(self._target_bytes) if self._target_bytes is not None else None),
                 (manual_history_path(self.corrected_path), _sha(expected_history) if expected_history is not None else None),
                 (self.pdf_path, self._pdf_hash)]
        for path, expected in paths:
            if _fingerprint(path) != expected:
                raise ConflictError(f"편집 중 파일이 외부에서 변경되었습니다: {path.name}\n저장하지 않았습니다. 다시 불러와 변경 내용을 확인하십시오.")

    def save(self, changes: dict[int, str]) -> SaveResult:
        self._assert_unchanged()
        rows = {p.index: p for p in self.paragraphs}
        edits = {}
        for index, text in changes.items():
            if type(index) is not int or index not in rows or not isinstance(text, str):
                raise SectionEditError("선택한 구획 안의 문단만 편집할 수 있습니다.")
            row = rows[index]
            if text == row.corrected_text:
                continue
            if not row.editable:
                raise SectionEditError(f"{index + 1}번 문단: {row.readonly_reason}")
            edits[index] = text
        if not edits:
            return SaveResult(self.corrected_path, 0, None, bool(self._history["paragraphs"]))
        target = _Archive(self._target_bytes or self._source_bytes)
        history = deepcopy(self._history)
        history.update(paragraph_count=len(target.paragraphs), source=_path(self.source_path),
                       section=self.section_name, updated=time.strftime("%Y-%m-%d %H:%M:%S"))
        changed_parts = set()
        correction_edits = []
        for index, text in edits.items():
            paragraph = target.paragraphs[index]
            before = paragraph_text(paragraph)
            old_hash = _text_hash(before)
            _replace_text(paragraph, text)
            _clear_known_grades(self._source.paragraphs[index], paragraph, self._source, target)
            record = history["paragraphs"].setdefault(str(index), {"signature": _signature(paragraph), "text_hashes": []})
            record["text_hashes"] = sorted(set(record["text_hashes"]) | {old_hash, _text_hash(text)})
            correction_edits.append({"paragraph": index, "signature": _signature(paragraph),
                                     "before": before, "after": text})
            changed_parts.add(target.locations[index][0])
        from ..correction_memory import append_corrections, memory_from_snapshot
        learning_warning = append_corrections(
            history, correction_edits, pdf_path=_path(self.pdf_path), pdf_sha256=self._pdf_hash,
            source_path=_path(self.source_path), source_sha256=_sha(self._source_bytes),
            section=self.section_name, start=self.start, end=self.end,
        )
        self.corrected_path.parent.mkdir(parents=True, exist_ok=True)
        history_path = manual_history_path(self.corrected_path)
        history_bytes = json.dumps(history, ensure_ascii=False, indent=1).encode("utf-8")
        stages: list[Path] = []
        published_history = False
        backup = self.corrected_path.with_suffix(".bak.hwpx") if self._target_bytes is not None else None
        try:
            stage = _stage_bytes(self.corrected_path, b"")
            stages.append(stage)
            target.write(stage, changed_parts)
            with stage.open("r+b") as stream:
                os.fsync(stream.fileno())
            new_bytes = stage.read_bytes()
            verified = _Archive(new_bytes)
            for index, text in edits.items():
                if paragraph_text(verified.paragraphs[index]) != text:
                    raise SectionEditError("저장 전 문단 검증에 실패했습니다.")
            intent_stage = _stage_bytes(history_path, history_bytes)
            stages.append(intent_stage)
            self._assert_unchanged()
            if backup is not None:
                backup_stage = _stage_bytes(backup, self._target_bytes)
                stages.append(backup_stage)
                os.replace(backup_stage, backup)
            if self._history_bytes is not None:
                history_backup = history_path.with_suffix(".bak.json")
                history_backup_stage = _stage_bytes(history_backup, self._history_bytes)
                stages.append(history_backup_stage)
                os.replace(history_backup_stage, history_backup)
            self._assert_unchanged()
            os.replace(intent_stage, history_path)
            published_history = True
            # The durable intent protects both old and new text if publication
            # fails or the process stops between the two atomic replacements.
            self._assert_unchanged(published_history=history_bytes)
            os.replace(stage, self.corrected_path)
        except BaseException:
            if published_history and _fingerprint(history_path) == _sha(history_bytes):
                try:
                    if self._history_bytes is None:
                        history_path.unlink()
                    else:
                        rollback = _stage_bytes(history_path, self._history_bytes)
                        stages.append(rollback)
                        os.replace(rollback, history_path)
                except OSError:
                    pass   # Durable merged intent still protects the old file.
            raise
        finally:
            for path in stages:
                path.unlink(missing_ok=True)
        self._target_bytes, self._history_bytes, self._history = new_bytes, history_bytes, history
        self._refresh_paragraphs(verified)
        memory = memory_from_snapshot(history, verified.paragraphs, self._pdf_hash, history_path)
        return SaveResult(self.corrected_path, len(edits), backup, True,
                          memory.count, history_path, learning_warning or memory.warning)

    def correction_records(self) -> list[dict]:
        """Committed human edits in this section, using this session snapshot."""
        from ..correction_memory import correction_records
        target = _Archive(self._target_bytes or self._source_bytes)
        try:
            return correction_records(self._history, target.paragraphs, self._pdf_hash,
                                      start=self.start, end=self.end)
        except ValueError as exc:
            raise SectionEditError("수동 수정 기록의 내용을 확인할 수 없습니다. 교정본과 자동 덮어쓰기 보호는 유지됩니다.") from exc

    def close(self) -> None:
        """Compatibility with GUI cleanup; snapshots hold no file handles."""
