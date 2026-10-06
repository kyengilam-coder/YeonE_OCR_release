"""지침이 금지하는 결과가 섞이지 않았는지 기계적으로 검사한다. 글자 정확성 검사가 아니라 구조·형식 검사다."""
from __future__ import annotations
import re
from .schema import PageReading, TEXT_KINDS, TITLE_KINDS, IMAGE_KINDS, region_text, region_columns

ASCII_GUA = re.compile(r"(={3,}|-{3,}|1{3,}|l{3,}|\|{3,})")
JAMO = re.compile(r"[ㄱ-ㆎᄀ-ᇿꥠ-꥿ힰ-퟿]")
LABEL_IN_BODY = re.compile(r"[-–—]\s*\d{1,4}\s*[-–—]")


def verify_page(r: PageReading) -> list[dict]:
    issues: list[dict] = []
    def add(level, rule, where, msg): issues.append({"level": level, "rule": rule, "where": where, "msg": msg})
    body_all = ""
    for reg in r.regions:
        if reg.kind in TEXT_KINDS:
            t = region_text(reg); body_all += t
            if not t.strip():
                add("warn", "빈 문자 영역", reg.id, f"{reg.kind} 영역에 글자가 없음")
            for m in ASCII_GUA.finditer(t):
                add("error", "괘상 ASCII 대체", reg.id, f"'{m.group(0)}' — 음양효를 =, -, 1, l 로 적은 OCR 문자열 의심(지침 13)")
            for m in JAMO.finditer(t):
                add("warn", "한글 자모 분리", reg.id, f"'{m.group(0)}' — 음절이 자모로 분리된 오인식 의심(지침 8). 옛한글이면 note 에 기록")
            for m in LABEL_IN_BODY.finditer(t):
                add("error", "쪽번호 혼입", reg.id, f"'{m.group(0)}' — 쪽번호 표기가 본문에 섞임(지침 15)")
            for c in region_columns(reg):
                if c.col_from_right <= 0:
                    add("warn", "열 번호 없음", reg.id, "col_from_right 가 0 이하")
        elif reg.kind in TITLE_KINDS and not reg.title_text.strip():
            add("warn", "빈 제목", reg.id, f"{reg.kind} 영역에 글자가 없음")
        elif reg.kind in IMAGE_KINDS and (reg.paragraphs or reg.table_rows):
            add("error", "그림 안 글자 전사", reg.id, f"{reg.kind} 영역에 글자가 들어 있음 — 그림 안 글자는 본문에 섞지 않음(지침 9)")
        if reg.kind == "단순표":
            widths = {len(row.cells) for row in reg.table_rows}
            if len(widths) > 1:
                add("warn", "표 열 수 불일치", reg.id, f"행마다 셀 수가 다름 {sorted(widths)} — 병합 관계 확인(지침 12)")
    if body_all.count("〔") != body_all.count("〕"):
        add("error", "표시 괄호 불균형", "page", "〔 와 〕 개수가 다름")
    if r.print_page and not r.print_page.isdigit():
        add("warn", "인쇄 쪽수 형식", "page", f"print_page='{r.print_page}' 는 숫자가 아님")
    return issues


def check_hwpx_file(path: str) -> list[dict]:
    """HWPX 파일 구조 검사: 확장자만 바꾼 파일이 아닌지(지침 23)."""
    import zipfile
    issues = []
    try:
        z = zipfile.ZipFile(path)
    except Exception as e:
        return [{"level": "error", "rule": "zip", "where": path, "msg": f"zip 으로 열리지 않음: {e}"}]
    names = z.namelist()
    if not names or names[0] != "mimetype":
        issues.append({"level": "error", "rule": "mimetype 순서", "where": path, "msg": "첫 항목이 mimetype 이 아님"})
    try:
        mt = z.read("mimetype").decode().strip()
        if mt != "application/hwp+zip":
            issues.append({"level": "error", "rule": "mimetype 값", "where": path, "msg": f"'{mt}'"})
    except KeyError:
        issues.append({"level": "error", "rule": "mimetype 없음", "where": path, "msg": "mimetype 항목 없음"})
    for need in ("Contents/content.hpf", "Contents/header.xml", "Contents/section0.xml"):
        if need not in names:
            issues.append({"level": "error", "rule": "필수 항목", "where": path, "msg": f"{need} 없음"})
    if any(n.startswith("word/") for n in names):
        issues.append({"level": "error", "rule": "DOCX 구조", "where": path, "msg": "word/ 폴더가 있음 — DOCX 를 이름만 바꾼 파일"})
    return issues
