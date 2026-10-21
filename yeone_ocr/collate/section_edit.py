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

from .hwpx_edit import HP, HH, INLINE_ATOMS, Editor, _local, _slots, _tag_of, paragraph_text
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


class SaveAccessError(PermissionError):
    """A save destination remained inaccessible; no lock/ACL cause is assumed."""
    def __init__(self, path, error: OSError):
        destination = Path(path)
        winerror = getattr(error, "winerror", None)
        error_code = winerror if winerror is not None else error.errno
        code = f" (오류 코드 {error_code})" if error_code is not None else ""
        super().__init__(error.errno, f"파일에 접근할 수 없어 저장하지 못했습니다{code}: {destination}", str(destination))
        self.path, self.winerror, self.error_code = destination, winerror, error_code


_SAVE_ACCESS_ATTEMPTS = 5
_SAVE_ACCESS_SECONDS = 0.75


@dataclass(frozen=True)
class TextStyleSpan:
    """Saved HWPX display style on a half-open range of paragraph text.

    Empty colors mean automatic/transparent. ``kind`` identifies only an owned
    OCR overlay; author colors are displayed without classifying them as OCR.
    """
    start: int
    end: int
    foreground: str = ""
    background: str = ""
    bold: bool = False
    underline: bool = False
    kind: str = ""


@dataclass(frozen=True)
class ParagraphEdit:
    index: int
    original_text: str
    corrected_text: str
    editable: bool
    readonly_reason: str = ""
    corrected_styles: tuple[TextStyleSpan, ...] = ()


@dataclass(frozen=True)
class GeneratedPageScope:
    """A source page and the complete editable paragraphs containing it."""
    page: int
    start: int
    end: int
    pdf_pages: tuple[int, int]
    label: str


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
        if path.is_dir():
            from ..pdfio import image_sources
            digest = hashlib.sha256()
            for source in image_sources(path):
                value = _fingerprint(source)
                if value is None:
                    return None
                digest.update(source.name.encode("utf-8") + b"\0" + bytes.fromhex(value))
            return digest.hexdigest()
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

    Missing history means no manual edits. The exact identity/hash path remains
    authoritative. A validated text event can additionally prove that only
    ASCII spaces changed; that paragraph stays protected without rewriting its
    current text, formatting or history. Learning still requires an exact chain.
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
    space_keys = None
    for key, record in history["paragraphs"].items():
        index = int(key)
        paragraph = _element(paragraphs[index])
        message = f"수동 교정한 {index + 1}번 문단과 수정 기록의 일치를 확인할 수 없습니다. 자동 교정 전에 해당 문단의 글자·구조와 수정 기록을 확인하십시오."
        if _signature(paragraph) != record["signature"]:
            raise ManualProtectionError(message)
        text = paragraph_text(paragraph)
        if _text_hash(text) not in record["text_hashes"]:
            if space_keys is None:
                space_keys = _protected_ascii_space_keys(history)
            if text.replace(" ", "") not in space_keys.get(index, set()):
                raise ManualProtectionError(message)
        protected.add(index)
    return protected


def _protected_ascii_space_keys(history: dict) -> dict[int, set[str]]:
    """Read independently validated event text; malformed/legacy data proves nothing."""
    from ..correction_memory import _events
    try:
        events = _events(history)
    except ValueError:
        return {}
    result: dict[int, set[str]] = {}
    for event in events:
        index = event["paragraph"]
        record = history["paragraphs"].get(str(index))
        if (record is None or event["signature"] != record["signature"]
                or event["before_sha256"] not in record["text_hashes"]
                or event["after_sha256"] not in record["text_hashes"]):
            continue
        result.setdefault(index, set()).update(text.replace(" ", "") for text in (event["before"], event["after"]))
    return result


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


def _readonly_reason(paragraph, heading: bool, *, allow_page_furniture: bool = False) -> str:
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
                    if allow_page_furniture and ck in ("colPr", "pageNum", "header", "footer"):
                        continue
                    return "필드·각주·그림·표가 있는 문단은 구조 보존을 위해 읽기 전용입니다."
            elif allow_page_furniture and kind == "secPr":
                continue
            elif kind not in ("bookmark",):
                return "필드·각주·그림·표가 있는 문단은 구조 보존을 위해 읽기 전용입니다."
    return ""


def _display_color(value: str | None) -> str:
    """Return a safe literal HWPX RGB color, retaining explicit black/white."""
    value = (value or "").strip()
    return value.upper() if re.fullmatch(r"#[0-9a-fA-F]{6}", value) else ""


def _display_char_styles(target: _Archive) -> dict[str, TextStyleSpan]:
    parser = etree.XMLParser(resolve_entities=False, no_network=True)
    header = etree.fromstring(target.entries["Contents/header.xml"], parser)
    grades = {"#0B7A3B": "green", "#1F4FBF": "blue", "#C00000": "red"}
    styles = {}
    for style in header.iter(HH + "charPr"):
        foreground = _display_color(style.get("textColor"))
        underline = style.find(HH + "underline")
        kind = grades.get(foreground, "") if _tag_of(style) == foreground else ""
        styles[style.get("id", "")] = TextStyleSpan(
            0, 0, foreground, _display_color(style.get("shadeColor")),
            style.find(HH + "bold") is not None,
            underline is not None and underline.get("type", "NONE").upper() != "NONE", kind)
    return styles


def _paragraph_display_styles(paragraph, styles: dict[str, TextStyleSpan]) -> tuple[TextStyleSpan, ...]:
    """Use exactly paragraph_text's direct body offsets, never nested memos.

    Inline markpen colors override run shading until their matching end marker,
    including text tails and ranges crossing run boundaries. No XML is changed.
    """
    result = []
    offset = 0
    markpens = []
    default = TextStyleSpan(0, 0)

    def markpen(node):
        if _local(node.tag) == "markpenBegin":
            markpens.append(_display_color(node.get("color")))
        elif _local(node.tag) == "markpenEnd" and markpens:
            markpens.pop()

    def emit(text, base):
        nonlocal offset
        if not text:
            return
        background = markpens[-1] if markpens and markpens[-1] else base.background
        values = (base.foreground, background, base.bold, base.underline, base.kind)
        if result and (result[-1].foreground, result[-1].background, result[-1].bold,
                       result[-1].underline, result[-1].kind) == values:
            result[-1] = TextStyleSpan(result[-1].start, offset + len(text), *values)
        else:
            result.append(TextStyleSpan(offset, offset + len(text), *values))
        offset += len(text)

    for run in paragraph:
        if _local(run.tag) != "run":
            continue
        base = styles.get(run.get("charPrIDRef", ""), default)
        for child in run:
            name = _local(child.tag)
            if name == "t":
                emit(child.text or "", base)
                for sub in child:
                    kind = _local(sub.tag)
                    markpen(sub)
                    emit(INLINE_ATOMS.get(kind, "" if kind in (
                        "markpenBegin", "markpenEnd", "insertBegin", "insertEnd",
                        "deleteBegin", "deleteEnd") else "".join(sub.itertext())), base)
                    emit(sub.tail or "", base)
            elif name == "tab" or name == "ctrl" and (child.get("id") or "").lower() == "tab":
                emit("\t", base)
            else:
                markpen(child)
    return tuple(result)


def _export_body_paragraph(paragraph) -> str:
    """Flatten visible body text, including tables/notes, but not memo content.

    Table cells, notes and text boxes follow their XML order, separated by blank
    lines. Headers/footers are page furniture, not body paragraphs. This export
    view does not change the direct paragraph numbering used by HWPX editing.
    """
    def nested(node):
        kind = _local(node.tag)
        if (kind in ("header", "footer")
                or kind == "fieldBegin" and node.get("type") == "MEMO"):
            return
        if node.tag == HP + "p":
            yield _export_body_paragraph(node)
        else:
            for child in node:
                yield from nested(child)

    blocks, inline = [], []
    for run in paragraph:
        if _local(run.tag) != "run":
            continue
        for child in run:
            kind = _local(child.tag)
            if kind == "t":
                inline.append(child.text or "")
                for sub in child:
                    name = _local(sub.tag)
                    inline.append(INLINE_ATOMS.get(name, "" if name in (
                        "markpenBegin", "markpenEnd", "insertBegin", "insertEnd",
                        "deleteBegin", "deleteEnd") else "".join(sub.itertext())))
                    inline.append(sub.tail or "")
            elif kind == "tab" or kind == "ctrl" and (child.get("id") or "").lower() == "tab":
                inline.append("\t")
            else:
                embedded = list(nested(child))
                if embedded:
                    if inline:
                        blocks.append("".join(inline))
                        inline = []
                    blocks.extend(embedded)
    if inline:
        blocks.append("".join(inline))
    return "\n\n".join(blocks)


def _replace_text(paragraph, text: str) -> None:
    """Apply diff hunks backwards, retaining nodes and ordered style boundaries.

    Within a length-changing replacement, distribute the new text over the
    affected slots in proportion to their old lengths. This also preserves the
    positions of memo/markpen boundaries relative to the replacement. Unchanged
    characters retain their exact slots; a pure insertion uses its adjacent slot.
    """
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
        if end > start:
            old_length, new_length = end - start, k - j
            for slot in affected:
                lo, hi = max(start, slot.start), min(end, slot.end)
                # ceil maps an old slot boundary to the first replacement
                # character belonging to the following slot. The inverse
                # mapping is shared by _align_style_choices below.
                left = ((lo - start) * new_length + old_length - 1) // old_length
                right = ((hi - start) * new_length + old_length - 1) // old_length
                slot.text = slot.text[:lo - slot.start] + text[j + left:j + right] + slot.text[hi - slot.start:]
            continue
        # Deletion leaves empty runs in place. At the end of nonempty text,
        # inherit its last visible character rather than a trailing empty run's
        # style, matching the adjacent source choice used for grade cleanup.
        slot = Editor._locate([slot for slot in slots if slot.text] or slots, start)
        at = max(0, min(len(slot.text), start - slot.start))
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
    """Carry source styles using the same ordered mapping as _replace_text."""
    result = []
    for op, a, b, c, d in SequenceMatcher(None, before, after, autojunk=False).get_opcodes():
        if op == "equal" or b - a == d - c:
            result.extend(choices[a:b])
        elif b > a and d > c:
            # Never merge all source styles into an ambiguous set: OCR red
            # overlays can hide real bold/normal differences. Each replacement
            # character instead inherits one position's original style.
            result.extend(choices[a + offset * (b - a) // (d - c)]
                          for offset in range(d - c))
        elif d > c:
            candidates = choices[min(a, len(choices) - 1)] if choices else empty_choices
            result.extend([candidates] * (d - c))
    return result


_FLOW_PAGE = re.compile(r"^(?:전체 PDF [1-9][0-9]*쪽 · )?PDF ([1-9][0-9]*)쪽(?: · 인쇄 .+쪽)?(?: \([0-9]+\))?$")
_CONTINUED_PAGE = re.compile(r"〔→PDF ([1-9][0-9]*)쪽(?:/인쇄 [^〕]*)?〕")


def _generated_meta_page(text: str) -> int | None:
    match = re.fullmatch(r"\[.+ / (?:전체 PDF [1-9][0-9]*쪽 / )?PDF ([1-9][0-9]*)쪽 / 인쇄 [^\]]+\](?:\s*〔고지: [\s\S]*〕)*", text)
    return int(match[1]) if match else None


def _generated_bookmarks(paragraph) -> list[tuple[int, int, str]]:
    """Read only direct body page bookmarks; memo paragraphs never contribute."""
    result, offset = [], 0
    for run in paragraph.findall(HP + "run"):
        for child in run:
            candidates = [child] if child.tag == HP + "bookmark" else list(child) if child.tag == HP + "ctrl" else []
            for bookmark in candidates:
                if bookmark.tag != HP + "bookmark":
                    continue
                label = bookmark.get("name", "")
                match = _FLOW_PAGE.fullmatch(label)
                if match:
                    result.append((offset, int(match[1]), label))
            if child.tag == HP + "t":
                holder = etree.Element(HP + "p")
                holder_run = etree.SubElement(holder, HP + "run")
                holder_run.append(deepcopy(child))
                offset += len(paragraph_text(holder))
            elif _local(child.tag) == "tab" or child.tag == HP + "ctrl" and child.get("id", "").lower() == "tab":
                offset += 1
    return result


def _generated_scopes(source: _Archive, page_count: int) -> tuple[tuple[GeneratedPageScope, ...], set[int]]:
    memberships: list[set[int]] = []
    labels, heads = {}, set()
    current = None
    for index, paragraph in enumerate(source.paragraphs):
        text = paragraph_text(paragraph)
        meta = _generated_meta_page(text)
        bookmarks = _generated_bookmarks(paragraph)
        if meta is not None:
            current = meta
            heads.add(index)
            labels.setdefault(meta, f"원본 {meta}쪽")
        pages = {current} if current is not None else set()
        if bookmarks:
            if bookmarks[0][0] == 0:
                pages.clear()
            for _, number, label in bookmarks:
                pages.add(number)
                labels.setdefault(number, label)
                current = number
        # In page layout, continuation text is placed before the later page's
        # metadata paragraph. Include that shared paragraph in both scopes.
        for match in _CONTINUED_PAGE.finditer(text):
            number = int(match[1])
            pages.add(number)
            labels.setdefault(number, f"원본 {number}쪽")
        memberships.append(pages)
    pages = sorted(set().union(*memberships)) if memberships else []
    if not pages:
        if page_count != 1:
            raise SectionEditError("OCR 결과에서 원본 쪽 표시를 찾을 수 없습니다. 쪽 정보가 있는 결과를 선택하십시오.")
        pages = [1]
        memberships = [{1} for _ in source.paragraphs]
    if any(number > page_count for number in pages):
        raise SectionEditError("OCR 결과의 원본 쪽 표시가 선택한 원본 범위를 벗어납니다.")
    scopes = []
    for number in pages:
        indices = [index for index, values in enumerate(memberships) if number in values]
        start, end = min(indices), max(indices) + 1
        represented = set().union(*memberships[start:end])
        scopes.append(GeneratedPageScope(number, start, end, (min(represented), max(represented)),
                                          labels.get(number, f"원본 {number}쪽")))
    return tuple(scopes), heads


def _generated_grade_kind(style, base=None) -> str:
    """Recognize the writer's full overlay, never a color alone."""
    color = (style.get("textColor") or "").upper()
    shade = (style.get("shadeColor") or "none").upper()
    underline = style.find(HH + "underline")
    shape = (underline.get("shape", "") if underline is not None and underline.get("type", "NONE") != "NONE" else "")
    if base is None:
        strikeout = style.find(HH + "strikeout")
        if (strikeout is not None and strikeout.get("shape", "NONE") != "NONE"
                or shape and (underline.get("color", "").upper() != color
                              or underline.get("type") != "BOTTOM")):
            return ""
    base_shade = (base.get("shadeColor") or "none").upper() if base is not None else "NONE"
    base_underline = base.find(HH + "underline") if base is not None else None
    base_shape = (base_underline.get("shape", "") if base_underline is not None
                  and base_underline.get("type", "NONE") != "NONE" else "")
    if color == "#0B7A3B" and shade in {"NONE", "#FFFFFF", base_shade} and shape == "DOT":
        return "green"
    if color == "#1F4FBF" and shade == "#E1EBFF" and shape == "DASH":
        return "blue"
    if color == "#C00000" and shade == "#FFFF00" and style.find(HH + "bold") is not None and shape in {"", "SOLID", base_shape}:
        return "red"
    return ""


def _generated_grade_bases(source: _Archive, *, legacy_trusted: bool) -> dict[str, str]:
    """Restore only declared writer overlays or unambiguous trusted legacy ones."""
    header = etree.fromstring(source.entries["Contents/header.xml"], etree.XMLParser(resolve_entities=False, no_network=True))
    styles = {style.get("id"): style for style in header.iter(HH + "charPr")}
    raw = source.entries.get("META-INF/yeone-ocr-edit.json")
    if raw is not None:
        try:
            manifest = json.loads(raw)
            declared = manifest["grade_styles"]
            if (manifest.get("schema_version") != 1 or manifest.get("producer") != "YeonE_OCR"
                    or not isinstance(declared, dict)):
                raise ValueError
            result = {}
            for current, base in declared.items():
                if (not isinstance(current, str) or not isinstance(base, str)
                        or current not in styles or base not in styles or current == base
                        or not _generated_grade_kind(styles[current], styles[base])
                        or _without_grade(styles[current]) != _without_grade(styles[base])):
                    raise ValueError
                result[current] = base
            return result
        except (ValueError, TypeError, KeyError) as exc:
            raise SectionEditError("OCR 결과의 교정 표시 서식 기록을 확인할 수 없습니다.") from exc
    if not legacy_trusted:
        return {}
    used = {slot.run.get("charPrIDRef", "0") for paragraph in source.paragraphs for slot in _slots(paragraph)}
    # A named paragraph/character style is an author's reusable formatting,
    # even if it happens to look exactly like one of our old grade overlays.
    author_styles = {style.get("charPrIDRef") for style in header.iter(HH + "style")}
    result = {}
    for current in used:
        style = styles.get(current)
        if current in author_styles or style is None or not _generated_grade_kind(style):
            continue
        candidates = {}
        for pid in styles:
            base = styles.get(pid)
            if (base is None or _generated_grade_kind(base)
                    or (base.get("textColor") or "#000000").upper() != "#000000"
                    or (base.get("shadeColor") or "none").upper() != "NONE"
                    or _without_grade(base) != _without_grade(style)):
                continue
            underline = base.find(HH + "underline")
            if underline is not None and underline.get("type", "NONE") != "NONE":
                continue
            candidates[_style_key(base)] = pid
        if len(candidates) == 1:
            result[current] = next(iter(candidates.values()))
    return result


class _ManualGradeStyles:
    """Remove only owned OCR overlays, retaining typography and author marks."""
    def __init__(self, source: _Archive, target: _Archive, generated_bases: dict[str, str] | None = None):
        parser = etree.XMLParser(resolve_entities=False, no_network=True)
        try:
            self.source = etree.fromstring(source.entries["Contents/header.xml"], parser)
            self.header = etree.fromstring(target.entries["Contents/header.xml"], parser)
        except (KeyError, etree.XMLSyntaxError) as exc:
            raise SectionEditError("글자 서식을 확인할 수 없어 교정 표시를 안전하게 지울 수 없습니다.") from exc
        self.source_styles = {s.get("id"): s for s in self.source.iter(HH + "charPr")}
        self.styles = {s.get("id"): s for s in self.header.iter(HH + "charPr")}
        self.target = target
        self.generated_bases = generated_bases or {}
        self.cache = {}

    def clean_style(self, current_id: str, choices: frozenset[str]) -> str:
        key = current_id, choices
        if key in self.cache:
            return self.cache[key]
        current = self.styles.get(current_id)
        generated = (current_id in self.generated_bases and current is not None
                     and _style_key(current) == _style_key(self.source_styles[current_id]))
        owned = (current is not None and _tag_of(current) in {"#0B7A3B", "#1F4FBF", "#C00000"}
                 and _tag_of(current) == (current.get("textColor") or "").upper())
        if not generated and not owned:
            return current_id  # Author colors and untagged legacy styles are not ours.
        candidates = [self.source_styles[self.generated_bases.get(pid, pid)]
                      for pid in sorted(choices) if pid in self.source_styles]
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
        # Editor._locate inserts into the last empty slot when no text remains.
        empty_choices = frozenset({source_slots[-1].run.get("charPrIDRef", "0")}) if source_slots else frozenset()
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
    def open_generated(cls, input_path, output_hwpx, page: int | None = None, *, work_dir=None):
        """Open an existing OCR result without recognizing or rewriting it.

        Source page markers bound the widgets, including shared flow paragraphs.
        A matching read-only Work manifest enables conservative legacy overlay
        recognition; newer writer documents declare the exact base styles.
        """
        from ..pdfio import open_pdf
        from ..state import Work
        from .run import corrected_path
        self = cls()
        self.generated = True
        self.pdf_path, self.source_path = Path(input_path).resolve(), Path(output_hwpx).resolve()
        if self.source_path.stem.endswith("_교정"):
            raise SectionEditError("처음 생성한 OCR 결과 HWPX를 선택하십시오. 기존 교정본은 자동으로 이어서 엽니다.")
        self.corrected_path = Path(corrected_path(str(self.source_path))).resolve()
        if (_path(self.source_path) == _path(self.corrected_path)
                or self.corrected_path.exists() and os.path.samefile(self.source_path, self.corrected_path)):
            raise SectionEditError("교정본과 OCR 결과가 같은 파일입니다.")
        self._source_bytes = self.source_path.read_bytes()
        self._target_bytes = self.corrected_path.read_bytes() if self.corrected_path.exists() else None
        history_path = manual_history_path(self.corrected_path)
        self._history_bytes = history_path.read_bytes() if history_path.exists() else None
        self._pdf_hash = _fingerprint(self.pdf_path)
        if self._pdf_hash is None:
            raise SectionEditError("원본 PDF·이미지를 찾을 수 없습니다.")
        legacy_trusted = False
        self.work_dir = Path(work_dir).resolve() if work_dir is not None else None
        if self.work_dir is not None:
            work = Work(str(self.pdf_path), str(self.work_dir), initialize=False)
            if not work.source_matches():
                raise SectionEditError("작업 기록의 원본과 선택한 PDF·이미지가 다릅니다. 같은 원본의 OCR 결과를 선택하십시오.")
            legacy_trusted = self.source_path.is_relative_to(work.out_dir.resolve())
        with open_pdf(str(self.pdf_path)) as document:
            page_count = len(document)
        source = _Archive(self._source_bytes)
        target = _Archive(self._target_bytes or self._source_bytes)
        self.page_scopes, self._heads = _generated_scopes(source, page_count)
        self.generated_pages = tuple(scope.page for scope in self.page_scopes)
        self.page = self.generated_pages[0] if page is None else page
        if type(self.page) is not int or self.page not in self.generated_pages:
            raise SectionEditError("선택한 쪽은 현재 OCR 결과에 없습니다.")
        selected = next(scope for scope in self.page_scopes if scope.page == self.page)
        self.start, self.end, self.pdf_pages = selected.start, selected.end, selected.pdf_pages
        self.section_name = selected.label
        if len(source.paragraphs) != len(target.paragraphs):
            raise SectionEditError("OCR 결과와 교정본의 문단 수가 달라 위치를 대응할 수 없습니다.")
        for index, ((src_part, src), (dst_part, dst)) in enumerate(zip(source.locations, target.locations)):
            source_marks = [(number, label) for _, number, label in _generated_bookmarks(src)]
            target_marks = [(number, label) for _, number, label in _generated_bookmarks(dst)]
            if (src_part != dst_part or _signature(src) != _signature(dst) or source_marks != target_marks
                    or index in self._heads and paragraph_text(src) != paragraph_text(dst)
                    or _CONTINUED_PAGE.findall(paragraph_text(src)) != _CONTINUED_PAGE.findall(paragraph_text(dst))):
                raise SectionEditError("OCR 결과와 교정본의 원본 쪽 표시·문단 구조가 달라 편집할 수 없습니다.")
        self._source = source
        self._history = _load_history(self.corrected_path, self._history_bytes)
        self._protected = manual_protected_paragraphs(self.corrected_path, target.paragraphs) if self._history_bytes else set()
        for index in range(self.start, self.end):
            reason = self._alignment_reason(index, paragraph_text(target.paragraphs[index]))
            if reason:
                raise SectionEditError(f"{index + 1}번 문단: {reason}")
        self._generated_bases = _generated_grade_bases(source, legacy_trusted=legacy_trusted)
        self._generation = 0
        self._replace_plans = weakref.WeakValueDictionary()
        self._refresh_paragraphs(target)
        self._assert_unchanged()
        return self

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
        styles = _display_char_styles(target)
        generated_bases = getattr(self, "_generated_bases", {})
        if generated_bases:
            parser = etree.XMLParser(resolve_entities=False, no_network=True)
            source_header = etree.fromstring(self._source.entries["Contents/header.xml"], parser)
            target_header = etree.fromstring(target.entries["Contents/header.xml"], parser)
            source_keys = {s.get("id"): _style_key(s) for s in source_header.iter(HH + "charPr")}
            target_keys = {s.get("id"): _style_key(s) for s in target_header.iter(HH + "charPr")}
        for pid in generated_bases:
            if pid in styles and source_keys.get(pid) == target_keys.get(pid):
                style = styles[pid]
                kind = {"#0B7A3B": "green", "#1F4FBF": "blue", "#C00000": "red"}.get(style.foreground, "")
                styles[pid] = TextStyleSpan(style.start, style.end, style.foreground, style.background,
                                            style.bold, style.underline, kind)
        for i in range(self.start, self.end):
            reason = _readonly_reason(target.paragraphs[i], i in self._heads,
                                      allow_page_furniture=getattr(self, "generated", False))
            self.paragraphs.append(ParagraphEdit(i, paragraph_text(self._source.paragraphs[i]),
                                                paragraph_text(target.paragraphs[i]), not reason, reason,
                                                _paragraph_display_styles(target.paragraphs[i], styles)))

    def export_text(self, changes: dict[int, str], *, scope: str = "section") -> str:
        """Return current body text, including unsaved edits, without saving.

        The session snapshot supplies unchanged paragraphs and headings. Export
        remains available after an external file change or a failed HWPX save;
        it does not re-read those files, authorize edits, or update history.
        """
        if scope not in ("section", "document"):
            raise SectionEditError("내보낼 범위는 현재 구획 전체 또는 교정 문서 전체로 선택하십시오.")
        if not isinstance(changes, dict):
            raise SectionEditError("내보낼 문단과 글자를 확인할 수 없습니다.")
        for index, text in changes.items():
            if (type(index) is not int or not 0 <= index < len(self._source.paragraphs)
                    or not isinstance(text, str)):
                raise SectionEditError("내보낼 문단 번호와 글자를 확인할 수 없습니다.")
        target = _Archive(self._target_bytes or self._source_bytes)
        indices = range(self.start, self.end) if scope == "section" else range(len(target.paragraphs))
        return "\n\n".join(changes[index] if index in changes else _export_body_paragraph(target.paragraphs[index])
                          for index in indices)

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

    def _page_marker_reason(self, index: int, text: str) -> str:
        if getattr(self, "generated", False):
            before = paragraph_text(self._source.paragraphs[index])
            if ([m.group() for m in _CONTINUED_PAGE.finditer(before)]
                    != [m.group() for m in _CONTINUED_PAGE.finditer(text)]):
                return "원본 쪽 연결 표시는 그대로 두고 본문만 수정하십시오."
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
        grade_styles = _ManualGradeStyles(self._source, target, getattr(self, "_generated_bases", None))
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
            reason = (_readonly_reason(paragraph, index in self._heads,
                                       allow_page_furniture=getattr(self, "generated", False))
                      or self._alignment_reason(index, saved) or self._page_marker_reason(index, after))
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
        grade_styles = _ManualGradeStyles(self._source, target, getattr(self, "_generated_bases", None))
        for index, text in edits.items():
            paragraph = target.paragraphs[index]
            before = paragraph_text(paragraph)
            old_hash = _text_hash(before)
            try:
                marker_reason = self._page_marker_reason(index, text)
                if marker_reason:
                    raise SectionEditError(marker_reason)
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
                self._retry_save_access(lambda: os.replace(backup_stage, backup), backup)
            if self._history_bytes is not None:
                history_backup = history_path.with_suffix(".bak.json")
                history_backup_stage = _stage_bytes(history_backup, self._history_bytes)
                stages.append(history_backup_stage)
                self._retry_save_access(lambda: os.replace(history_backup_stage, history_backup), history_backup)
            self._assert_unchanged()
            self._retry_save_access(lambda: os.replace(intent_stage, history_path), history_path)
            published_history = True
            # The durable intent protects both old and new text if publication
            # fails or the process stops between the two atomic replacements.
            self._assert_unchanged(published_history=history_bytes)
            self._retry_save_access(lambda: os.replace(stage, self.corrected_path), self.corrected_path,
                                    published_history=history_bytes)
        except BaseException:
            try:
                own_intent = published_history and _fingerprint(history_path) == _sha(history_bytes)
            except OSError:
                own_intent = False
            if own_intent:
                try:
                    if self._history_bytes is None:
                        self._retry_save_access(history_path.unlink, history_path,
                                                published_history=history_bytes, rollback=True)
                    else:
                        rollback = _stage_bytes(history_path, self._history_bytes)
                        stages.append(rollback)
                        self._retry_save_access(lambda: os.replace(rollback, history_path), history_path,
                                                published_history=history_bytes, rollback=True)
                except (OSError, ConflictError):
                    # If only our own intent remains, remember that durable
                    # snapshot so releasing the lock permits the same session
                    # to save again. Competing writes must still fail closed.
                    try:
                        self._assert_unchanged(published_history=history_bytes)
                    except (OSError, ConflictError):
                        pass
                    else:
                        self._history_bytes, self._history = history_bytes, history
                        self._protected.update(edits)
            raise
        finally:
            for path in stages:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    # A temporary-file lock must not hide the actual save
                    # failure, or report failure after publication succeeded.
                    pass
        self._target_bytes, self._history_bytes, self._history = new_bytes, history_bytes, history
        self._protected.update(edits)
        self._generation += 1
        self._replace_plans.clear()
        self._refresh_paragraphs(verified)
        memory = memory_from_snapshot(history, verified.paragraphs, self._pdf_hash, history_path)
        return SaveResult(self.corrected_path, len(edits), backup, True,
                          memory.count, history_path, learning_warning or memory.warning)

    def _retry_save_access(self, operation, path: Path, *, published_history: bytes | None = None,
                           rollback: bool = False) -> None:
        """Retry only brief Windows access failures, rechecking before each try.

        The initial transaction checks remain at the call sites. A rollback
        compares only its own durable history intent, so a competing document
        save does not prevent restoring our previous history. Another writer's
        history is never replaced or removed by that rollback.
        """
        deadline = time.monotonic() + _SAVE_ACCESS_SECONDS
        last_error = None
        for attempt in range(_SAVE_ACCESS_ATTEMPTS):
            if rollback:
                if published_history is None or _fingerprint(path) != _sha(published_history):
                    raise ConflictError("저장 복구 중 수정 기록이 외부에서 변경되었습니다. 외부 기록은 덮어쓰지 않았습니다.")
            elif attempt:
                self._assert_unchanged(published_history=published_history)
            if attempt and time.monotonic() >= deadline:
                raise SaveAccessError(path, last_error) from last_error
            try:
                operation()
                return
            except OSError as exc:
                code = getattr(exc, "winerror", None)
                if not isinstance(exc, PermissionError) and code not in {5, 32, 33}:
                    raise
                remaining = deadline - time.monotonic()
                if (code not in {5, 32, 33} or attempt + 1 >= _SAVE_ACCESS_ATTEMPTS
                        or remaining <= 0):
                    raise SaveAccessError(path, exc) from exc
                last_error = exc
                time.sleep(min(0.05 * 2 ** attempt, 0.25, remaining))

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
