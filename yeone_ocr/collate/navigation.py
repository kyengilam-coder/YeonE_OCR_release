"""Local bookmark previews and location-specific HWPX viewing copies."""
from __future__ import annotations

import os
from pathlib import Path
import re
import tempfile
import xml.etree.ElementTree as ET
import zipfile

from .sections import normalize_name

HP = "{http://www.hancom.co.kr/hwpml/2011/paragraph}"
HA = "{http://www.hancom.co.kr/hwpml/2011/app}"


def index_signature(pdf: str, hwpx: str, source: str, styles: list[str]) -> tuple:
    """Bind an index to its inputs, including edits made outside this app."""
    def stamp(value):
        if not value:
            return ("", None, None)
        path = Path(value).resolve()
        try:
            stat = path.stat()
            return (os.path.normcase(str(path)), stat.st_size, stat.st_mtime_ns)
        except OSError:
            return (os.path.normcase(str(path)), None, None)
    return stamp(pdf), stamp(hwpx), source, tuple(styles)


def create_section_view_copy(hwpx: str | Path, para: int, name: str, *,
                             output_dir: str | Path | None = None) -> Path:
    """Change only the saved caret in a fresh copy after checking its title.

    A single body section has the documented main paragraph list 0. Multiple
    sections need a separate list mapping, so they are rejected instead of
    opening at a guessed location. Never use an XML paragraph ``id`` as the
    caret ordinal: real Hancom documents often repeat the same id.
    """
    source = Path(hwpx)
    with zipfile.ZipFile(source) as archive:
        sections = [n for n in archive.namelist() if re.fullmatch(r"Contents/section\d+\.xml", n)]
        if sections != ["Contents/section0.xml"]:
            raise ValueError("이 문서는 본문 섹션이 여러 개여서 한글의 이동 위치를 확인할 수 없습니다. 앱의 구획 미리 보기를 이용하십시오.")
        root = ET.fromstring(archive.read(sections[0]))
        paragraphs = root.findall(HP + "p")
        if not 0 <= para < len(paragraphs):
            raise ValueError("선택한 문단이 교정본에 없습니다. 구획을 다시 색인하십시오.")
        paragraph = paragraphs[para]
        text = "".join(t.text or "" for t in paragraph.iter(HP + "t"))
        titles = [text] + [b.get("name", "") for b in paragraph.iter(HP + "bookmark")]
        if not normalize_name(name) or not any(normalize_name(t) == normalize_name(name) for t in titles):
            raise ValueError("교정본의 제목과 선택한 구획이 다릅니다. 구획을 다시 색인하십시오.")
        if "settings.xml" not in archive.namelist():
            raise ValueError("이 문서에는 한글의 저장된 커서 위치가 없습니다. 앱의 구획 미리 보기를 이용하십시오.")
        settings = archive.read("settings.xml")
        settings_root = ET.fromstring(settings)
        caret = settings_root.find(HA + "CaretPosition")
        if caret is None:
            raise ValueError("한글의 커서 위치 형식을 확인할 수 없습니다. 앱의 구획 미리 보기를 이용하십시오.")
        # Preserve every other settings byte, including namespace prefixes.
        tags = list(re.finditer(rb"<(?:[A-Za-z_][\w.-]*:)?CaretPosition\b[^>]*>", settings))
        if len(tags) != 1:
            raise ValueError("한글의 커서 위치가 하나가 아닙니다. 앱의 구획 미리 보기를 이용하십시오.")
        tag = tags[0].group()
        for attribute, value in ((b"listIDRef", 0), (b"paraIDRef", para), (b"pos", 0)):
            pattern = rb"\b" + attribute + rb"\s*=\s*([\"']).*?\1"
            tag, count = re.subn(pattern, attribute + b'="' + str(value).encode("ascii") + b'"', tag)
            if count != 1:
                raise ValueError("한글의 커서 위치 속성을 확인할 수 없습니다.")
        settings = settings[:tags[0].start()] + tag + settings[tags[0].end():]
        actual = ET.fromstring(settings).find(HA + "CaretPosition")
        if actual is None or any(actual.get(k) != v for k, v in
                                 (("listIDRef", "0"), ("paraIDRef", str(para)), ("pos", "0"))):
            raise ValueError("구획 이동 위치를 검증하지 못했습니다.")
        directory = Path(output_dir) if output_dir else Path(tempfile.gettempdir()) / "YeonE_OCR" / "구획보기"
        directory.mkdir(parents=True, exist_ok=True)
        safe_name = re.sub(r'[\\/:*?"<>|\s]+', "_", name)[:32] or "구획"
        descriptor, filename = tempfile.mkstemp(prefix=f"{source.stem}_{safe_name}_보기_", suffix=".hwpx", dir=directory)
        os.close(descriptor)
        result = Path(filename)
        try:
            with zipfile.ZipFile(result, "w") as output:
                for entry in archive.infolist():
                    output.writestr(entry, settings if entry.filename == "settings.xml" else archive.read(entry.filename))
            with zipfile.ZipFile(result) as check:
                if check.testzip() is not None or check.read("settings.xml") != settings:
                    raise ValueError("구획 보기 사본 검증에 실패했습니다.")
        except Exception:
            result.unlink(missing_ok=True)
            raise
    return result
