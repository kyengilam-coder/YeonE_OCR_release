"""Selected-section manual editing with source preservation and OCR protection.

Only text slots in selected body paragraphs are edited. OCR marks on manually
changed text are restored to the source's formatting. Other ZIP members remain
byte-identical except for the style header when a restored style must be added.
Manual history is a durable sibling file so later OCR can protect manual edits.
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
import weakref
import zipfile

from lxml import etree

from .hwpx_edit import HP, HH, Editor, _local, _slots, _tag_of, paragraph_text
from .sections import HwpxBook, normalize_name, resolve_anchor_index
from . import zh


class SectionEditError(ValueError):
    """The requested edit cannot safely preserve document structure."""

    def __init__(self, message: str, *, paragraph_index: int | None = None,
                 offset: int | None = None, character: str | None = None):
        super().__init__(message)
        self.paragraph_index = paragraph_index
        self.offset = offset
        self.character = character


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


@dataclass(frozen=True)
class ReplaceEntry:
    index: int
    before: str
    after: str
    count: int


@dataclass(frozen=True)
class ReplaceSkip:
    index: int
    reason: str
    count: int


@dataclass(frozen=True)
class ReplacePlan:
    """A preview tied to one session snapshot; creating it never writes files."""
    find: str
    replacement: str
    scope: str
    entries: tuple[ReplaceEntry, ...]
    skipped: tuple[ReplaceSkip, ...]
    _drafts: tuple[tuple[int, str, str], ...]
    _generation: int

    @property
    def changes(self) -> dict[int, str]:
        return {index: after for index, _, after in self._drafts}

    @property
    def baseline(self) -> dict[int, str]:
        return {index: before for index, before, _ in self._drafts}

    @property
    def before(self) -> dict[int, str]:
        return {entry.index: entry.before for entry in self.entries}

    @property
    def matches(self) -> int:
        return sum(entry.count for entry in self.entries)

    @property
    def skipped_count(self) -> int:
        return sum(entry.count for entry in self.skipped)

    @property
    def paragraph_count(self) -> int:
        return len(self.entries)


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
    # Existing literal separators are text, unlike hp:tab/hp:lineBreak atoms.
    # Permit retaining/removing these old separators while rejecting new ones.
    existing = iter(c for c in before if _text_control(c))
    for offset, char in enumerate(text):
        if _text_control(char) and not any(old == char for old in existing):
            raise SectionEditError(
                "문단 안에는 줄바꿈·탭·제어 문자를 새로 넣을 수 없습니다. 문단별로 글자만 고쳐 주십시오.",
                offset=offset, character=char)
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


def _text_control(char: str) -> bool:
    return ord(char) < 32 or char in "\u2028\u2029"


def _style_key(element) -> tuple:
    """Compare style meaning, independent of IDs, namespace prefixes or order."""
    return (element.tag, tuple(sorted((k, v) for k, v in element.attrib.items() if k != "id")),
            (element.text or "").strip(), tuple(sorted(_style_key(child) for child in element)))


_GRADE_ATTRIBUTES = ("textColor", "shadeColor")
_GRADE_CHILDREN = ("bold", "underline", "strikeout")
_CHAR_PR_TAIL_ORDER = ("italic", "bold", "underline", "strikeout", "outline",
                       "shadow", "emboss", "engrave", "supscript", "subscript")


def _without_grade(style):
    result = deepcopy(style)
    for name in _GRADE_ATTRIBUTES:
        result.attrib.pop(name, None)
    for child in list(result):
        if _local(child.tag) in _GRADE_CHILDREN:
            result.remove(child)
    return _style_key(result)


def _align_style_choices(before: str, after: str, choices: list[frozenset[str]],
                         empty_choices: frozenset[str]) -> list[frozenset[str]]:
    """Carry source styles through text edits without inventing style boundaries.

    Equal-length replacements retain each character's style. A length-changing
    replacement carries all affected source choices; the surviving run's font
    and other unaffected attributes can disambiguate them later.
    """
    result = []
    for op, a, b, c, d in SequenceMatcher(None, before, after, autojunk=False).get_opcodes():
        if op == "equal" or b - a == d - c:
            result.extend(choices[a:b])
        elif d > c:
            candidates = frozenset().union(*choices[a:b])
            if not candidates:
                candidates = choices[min(a, len(choices) - 1)] if choices else empty_choices
            result.extend([candidates] * (d - c))
    return result


class _ManualGradeStyles:
    """Remove only owned OCR overlays, retaining typography and author marks."""
    def __init__(self, source: _Archive, target: _Archive):
        parser = etree.XMLParser(resolve_entities=False, no_network=True)
        try:
            self.source = etree.fromstring(source.entries["Contents/header.xml"], parser)
            self.header = etree.fromstring(target.entries["Contents/header.xml"], parser)
        except (KeyError, etree.XMLSyntaxError) as exc:
            raise SectionEditError("글자 서식을 확인할 수 없어 교정 표시를 안전하게 지울 수 없습니다.") from exc
        self.source_styles = {s.get("id"): s for s in self.source.iter(HH + "charPr")}
        self.styles = {s.get("id"): s for s in self.header.iter(HH + "charPr")}
        self.target = target
        self.cache = {}

    def clean_style(self, current_id: str, choices: frozenset[str]) -> str:
        key = current_id, choices
        if key in self.cache:
            return self.cache[key]
        current = self.styles.get(current_id)
        if (current is None or _tag_of(current) not in {"#0B7A3B", "#1F4FBF", "#C00000"}
                or _tag_of(current) != (current.get("textColor") or "").upper()):
            return current_id  # Author colors and untagged legacy styles are not ours.
        candidates = [self.source_styles[pid] for pid in sorted(choices) if pid in self.source_styles]
        if len(candidates) > 1:
            matches = [s for s in candidates if _without_grade(s) == _without_grade(current)]
            if matches:
                candidates = matches
        restored = {}
        for base in candidates:
            style = deepcopy(current)
            for name in _GRADE_ATTRIBUTES:
                if name in base.attrib:
                    style.set(name, base.get(name))
                else:
                    style.attrib.pop(name, None)
            for name in _GRADE_CHILDREN:
                old = style.find(HH + name)
                source_child = base.find(HH + name)
                at = (list(style).index(old) if old is not None else next(
                    (i for i, child in enumerate(style)
                     if _local(child.tag) in _CHAR_PR_TAIL_ORDER
                     and _CHAR_PR_TAIL_ORDER.index(_local(child.tag)) > _CHAR_PR_TAIL_ORDER.index(name)),
                    len(style)))
                if old is not None:
                    style.remove(old)
                if source_child is not None:
                    style.insert(at, deepcopy(source_child))
            restored[_style_key(style)] = style
        if len(restored) != 1:
            raise SectionEditError(
                "서로 다른 원본 글자 서식에 걸쳐 교정 표시를 지울 수 없습니다. 서식별로 나누어 고쳐 주십시오.")
        signature, wanted = next(iter(restored.items()))
        pid = next((pid for pid, style in self.styles.items() if _style_key(style) == signature), None)
        if pid is None:
            parent = current.getparent()
            if parent is None or parent.tag != HH + "charProperties":
                raise SectionEditError("글자 서식 목록을 확인할 수 없어 저장하지 않았습니다.")
            pid = str(max((int(value) for value in self.styles if value and value.isdigit()), default=-1) + 1)
            wanted.set("id", pid)
            parent.append(wanted)
            parent.set("itemCnt", str(len(parent)))
            self.styles[pid] = wanted
            self.target.entries["Contents/header.xml"] = etree.tostring(
                self.header, encoding="utf-8", xml_declaration=True)
        self.cache[key] = pid
        return pid

    def clear_changed(self, original, corrected, before: str) -> None:
        source_slots = _slots(original)
        choices = [frozenset({slot.run.get("charPrIDRef", "0")})
                   for slot in source_slots for _ in slot.text]
        empty_choices = frozenset(slot.run.get("charPrIDRef", "0") for slot in source_slots)
        choices = _align_style_choices(paragraph_text(original), before, choices, empty_choices)
        after = paragraph_text(corrected)
        choices = _align_style_choices(before, after, choices, empty_choices)
        changed = [False] * len(after)
        for op, _, _, a, b in SequenceMatcher(None, before, after, autojunk=False).get_opcodes():
            if op != "equal":
                changed[a:b] = [True] * (b - a)
        spans = []
        for slot in _slots(corrected):
            current = slot.run.get("charPrIDRef", "0")
            for offset in range(slot.start, slot.end):
                pid = self.clean_style(current, choices[offset]) if changed[offset] else current
                if pid == current:
                    continue
                if spans and spans[-1][1] == offset and spans[-1][2] == pid:
                    spans[-1] = spans[-1][0], offset + 1, pid
                else:
                    spans.append((offset, offset + 1, pid))
        # Split only changed spans. Existing helper moves, never duplicates,
        # inline markpen nodes, memo/bookmark controls and their text tails.
        for start, end, pid in reversed(spans):
            for slot in reversed(_slots(corrected)):
                lo, hi = max(start, slot.start), min(end, slot.end)
                if lo >= hi:
                    continue
                run_slots = [s for s in _slots(corrected) if s.run is slot.run]
                if hi < max(s.end for s in run_slots):
                    Editor._split_after(None, corrected, slot, hi)
                if lo > min(s.start for s in run_slots):
                    run = Editor._split_after(None, corrected, slot, lo)
                else:
                    run = slot.run
                run.set("charPrIDRef", pid)


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
        self._protected = protected
        self._generation = 0
        self._replace_plans: weakref.WeakValueDictionary[int, ReplacePlan] = weakref.WeakValueDictionary()
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

    def _check_plan(self, plan: ReplacePlan) -> None:
        if (not isinstance(plan, ReplacePlan) or self._replace_plans.get(id(plan)) is not plan
                or plan._generation != self._generation):
            raise ConflictError("바꾸기 미리보기 이후 문서가 달라졌습니다. 다시 찾아 바꿀 내용을 확인하십시오.")

    def _validated_changes(self, changes: dict[int, str],
                           replacement_plan: ReplacePlan | None) -> dict[int, str]:
        if replacement_plan is not None:
            self._check_plan(replacement_plan)
        if not isinstance(changes, dict):
            raise SectionEditError("편집할 문단과 글자를 확인할 수 없습니다.")
        rows = {p.index: p for p in self.paragraphs}
        approved = replacement_plan.changes if replacement_plan is not None else {}
        baseline = replacement_plan.baseline if replacement_plan is not None else {}
        edits = {}
        for index, text in changes.items():
            if type(index) is not int or not isinstance(text, str):
                raise SectionEditError("선택한 구획 안의 문단만 편집할 수 있습니다.")
            if index not in rows:
                if index not in approved or text != approved[index]:
                    raise SectionEditError("다른 구획은 찾아 바꾸기에서 미리 확인한 내용만 저장할 수 있습니다.")
                if text != baseline[index]:
                    edits[index] = text
                continue
            row = rows[index]
            if text == row.corrected_text:
                continue
            if not row.editable:
                raise SectionEditError(f"{index + 1}번 문단: {row.readonly_reason}")
            edits[index] = text
        return edits

    def _alignment_reason(self, index: int, current: str) -> str:
        old = paragraph_text(self._source.paragraphs[index])
        if index not in self._protected and old != current and max(len(old), len(current)) >= 12:
            left, right = zh.compare_key(normalize_name(old)), zh.compare_key(normalize_name(current))
            if SequenceMatcher(None, left, right, autojunk=False).ratio() < 0.45:
                return "원문과 교정본 내용이 크게 달라 위치 대응을 확인할 수 없습니다."
        return ""

    def preview_replace(self, find: str, replacement: str, *, scope: str = "section",
                        paragraph_index: int | None = None, changes: dict[int, str] | None = None,
                        previous_plan: ReplacePlan | None = None,
                        match: tuple[int, int] | None = None) -> ReplacePlan:
        """Preview literal body-text replacement, including current unsaved edits.

        Only a plan created by this snapshot can authorize changes outside its
        displayed section. Callers may discard a preview without invalidating
        the earlier plan that their current draft was based on.
        """
        self._assert_unchanged()
        if not isinstance(find, str) or not find:
            raise SectionEditError("찾을 글자를 입력하십시오.")
        if not isinstance(replacement, str):
            raise SectionEditError("바꿀 글자를 확인하십시오.")
        if any(_text_control(char) for char in find + replacement):
            raise SectionEditError("찾을 글자와 바꿀 글자에는 줄바꿈·탭·제어 문자를 넣을 수 없습니다.")
        if scope not in {"paragraph", "section", "document"}:
            raise SectionEditError("찾아 바꾸기 범위를 확인하십시오.")
        if scope == "paragraph" and (type(paragraph_index) is not int
                                      or not self.start <= paragraph_index < self.end):
            raise SectionEditError("현재 구획에서 바꿀 문단을 선택하십시오.")
        drafts = self._validated_changes(changes or {}, previous_plan)
        target = _Archive(self._target_bytes or self._source_bytes)
        indices = (range(len(target.paragraphs)) if scope == "document" else
                   range(self.start, self.end) if scope == "section" else [paragraph_index])
        if match is not None:
            if (not isinstance(match, tuple) or len(match) != 2
                    or any(type(value) is not int for value in match)
                    or match[0] not in indices or match[1] < 0):
                raise SectionEditError("선택한 찾기 위치가 범위와 다릅니다. 다시 찾으십시오.")
            indices = [match[0]]
        entries, skipped = [], []
        grade_styles = _ManualGradeStyles(self._source, target)
        for index in indices:
            paragraph = target.paragraphs[index]
            saved = paragraph_text(paragraph)
            before = drafts.get(index, saved)
            count = before.count(find)
            if match is not None:
                offset = match[1]
                if before[offset:offset + len(find)] != find:
                    raise ConflictError("찾은 글자가 수정되었습니다. 다시 찾으십시오.")
                count = 1
                after = before[:offset] + replacement + before[offset + len(find):]
            else:
                after = before.replace(find, replacement) if count else before
            if not count:
                continue
            reason = (_readonly_reason(paragraph, index in self._heads)
                      or self._alignment_reason(index, saved))
            if not reason:
                try:
                    trial = deepcopy(paragraph)
                    _replace_text(trial, after)
                    grade_styles.clear_changed(self._source.paragraphs[index], trial, saved)
                except SectionEditError as exc:
                    reason = str(exc)
            if reason:
                skipped.append(ReplaceSkip(index, reason, count))
                continue
            entries.append(ReplaceEntry(index, before, after, count))
            if after == saved:
                drafts.pop(index, None)
            else:
                drafts[index] = after
        plan = ReplacePlan(find, replacement, scope, tuple(entries), tuple(skipped),
                           tuple((index, paragraph_text(target.paragraphs[index]), after)
                                 for index, after in sorted(drafts.items())), self._generation)
        self._replace_plans[id(plan)] = plan
        return plan

    def save(self, changes: dict[int, str], *, replacement_plan: ReplacePlan | None = None) -> SaveResult:
        self._assert_unchanged()
        edits = self._validated_changes(changes, replacement_plan)
        if not edits:
            return SaveResult(self.corrected_path, 0, None, bool(self._history["paragraphs"]))
        target = _Archive(self._target_bytes or self._source_bytes)
        history = deepcopy(self._history)
        outside = any(not self.start <= index < self.end for index in edits)
        history_section = "교정본 전체" if outside else self.section_name
        history_start, history_end = (0, len(target.paragraphs)) if outside else (self.start, self.end)
        history.update(paragraph_count=len(target.paragraphs), source=_path(self.source_path),
                       section=history_section, updated=time.strftime("%Y-%m-%d %H:%M:%S"))
        changed_parts = set()
        correction_edits = []
        grade_styles = _ManualGradeStyles(self._source, target)
        for index, text in edits.items():
            paragraph = target.paragraphs[index]
            before = paragraph_text(paragraph)
            old_hash = _text_hash(before)
            try:
                _replace_text(paragraph, text)
                grade_styles.clear_changed(self._source.paragraphs[index], paragraph, before)
            except SectionEditError as exc:
                raise SectionEditError(f"{index + 1}번 문단: {exc}", paragraph_index=index,
                                       offset=exc.offset, character=exc.character) from exc
            record = history["paragraphs"].setdefault(str(index), {"signature": _signature(paragraph), "text_hashes": []})
            record["text_hashes"] = sorted(set(record["text_hashes"]) | {old_hash, _text_hash(text)})
            correction_edits.append({"paragraph": index, "signature": _signature(paragraph),
                                     "before": before, "after": text})
            changed_parts.add(target.locations[index][0])
        from ..correction_memory import append_corrections, memory_from_snapshot
        learning_warning = append_corrections(
            history, correction_edits, pdf_path=_path(self.pdf_path), pdf_sha256=self._pdf_hash,
            source_path=_path(self.source_path), source_sha256=_sha(self._source_bytes),
            section=history_section, start=history_start, end=history_end,
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
        self._protected.update(edits)
        self._generation += 1
        self._replace_plans.clear()
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
