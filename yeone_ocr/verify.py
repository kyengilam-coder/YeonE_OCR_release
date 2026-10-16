"""지침이 금지하는 결과가 섞이지 않았는지 기계적으로 검사한다. 글자 정확성 검사가 아니라 구조·형식 검사다."""
from __future__ import annotations
import re
from .schema import PageReading, KINDS, TEXT_KINDS, TITLE_KINDS, IMAGE_KINDS, region_text, region_columns, duplicate_region_ids

ASCII_GUA = re.compile(r"(={3,}|-{3,}|1{3,}|l{3,}|\|{3,})")

# 64괘 이름 → 육획 기호(文王 차례, U+4DC0~4DFF). 이체·약자도 같이 받는다.
HEX_NAMES = ["乾", "坤", "屯", "蒙", "需", "訟", "師", "比", "小畜", "履", "泰", "否", "同人", "大有", "謙", "豫", "隨", "蠱", "臨", "觀",
             "噬嗑", "賁", "剝", "復", "无妄", "大畜", "頤", "大過", "坎", "離", "咸", "恒", "遯", "大壯", "晉", "明夷", "家人", "睽", "蹇", "解",
             "損", "益", "夬", "姤", "萃", "升", "困", "井", "革", "鼎", "震", "艮", "漸", "歸妹", "豐", "旅", "巽", "兌", "渙", "節",
             "中孚", "小過", "既濟", "未濟"]
HEXAGRAM = {name: chr(0x4DC0 + i) for i, name in enumerate(HEX_NAMES)}
HEXAGRAM.update({"無妄": HEXAGRAM["无妄"], "恆": HEXAGRAM["恒"], "遁": HEXAGRAM["遯"], "豊": HEXAGRAM["豐"], "旣濟": HEXAGRAM["既濟"],
                 "剥": HEXAGRAM["剝"], "噬嗑": HEXAGRAM["噬嗑"], "兑": HEXAGRAM["兌"], "巽": HEXAGRAM["巽"]})
TRIGRAM_NAMES = {"乾", "坤", "坎", "離", "震", "巽", "艮", "兌", "兑"}
TRIGRAM_CHAR = {"乾": "☰", "兌": "☱", "兑": "☱", "離": "☲", "震": "☳", "巽": "☴", "坎": "☵", "艮": "☶", "坤": "☷"}
GUA_RE = re.compile("(" + "|".join(sorted(HEXAGRAM, key=len, reverse=True)) + r")卦\s*[(（]\s*([☰-☷䷀-䷿]{1,2})\s*[)）]")


def check_gua_symbols(text: str) -> list[tuple[str, str, str, str]]:
    """'困卦(䷯)' 처럼 괘 이름 옆에 적힌 기호가 이름과 맞는지 본다. (level, 이름, 적힌 기호, 설명) 목록."""
    out = []
    for m in GUA_RE.finditer(text):
        name, sym = m.group(1), m.group(2)
        want = HEXAGRAM[name]
        if sym == want:
            continue
        if all("☰" <= ch <= "☷" for ch in sym):
            if len(sym) == 1 and name in TRIGRAM_NAMES and sym == TRIGRAM_CHAR[name]:
                out.append(("warn", name, sym, f"{name}卦({sym}) 는 삼획 기호. 원본이 육획(重卦)이면 {want} — 획 수 확인(지침 13)"))
            else:
                out.append(("error", name, sym, f"{name}卦 옆에 삼획 기호 '{sym}' — 이름대로면 육획 {want}. 원본 확인(지침 13)"))
        else:
            out.append(("error", name, sym, f"괘상 불일치: {name}卦 는 {want} 인데 '{sym}' 로 적힘 — 원본 확인(지침 13)"))
    return out
JAMO = re.compile(r"[ㄱ-ㆎᄀ-ᇿꥠ-꥿ힰ-퟿]")
LABEL_IN_BODY = re.compile(r"[-–—]\s*\d{1,4}\s*[-–—]")


def verify_page(r: PageReading) -> list[dict]:
    issues: list[dict] = []
    def add(level, rule, where, msg): issues.append({"level": level, "rule": rule, "where": where, "msg": msg})
    body_all = ""
    for rid in sorted(duplicate_region_ids(r)):
        add("error", "영역 id 겹침", rid, "같은 id 의 영역이 여럿이어서 자동 병합·글자 수정을 적용할 수 없음")
    for reg in r.regions:
        if reg.kind not in KINDS:
            add("error", "지원하지 않는 영역 종류", reg.id, f"{reg.kind!r} — 본문 누락을 막기 위해 영역 분류를 확인해야 함")
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
            for level, name, sym, msg in check_gua_symbols(t):
                add(level, "괘상 기호", reg.id, msg)
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
    # 개요·번호 문단 모양이면 한컴이 "1. 가. 나." 자동 번호를 붙여 원본에 없는 글자가 보인다. 번호는 문단 모양(paraPr)의 머리 종류에서 오므로
    # 그것을 본다 — 사용자 양식의 스타일(제목-장·본경 원문 …)은 번호가 없으면 써도 된다. XML 로 읽는다: 속성 차례(id 가 앞인지 뒤인지)에 매이지 않게.
    import xml.etree.ElementTree as ET
    HH = "{http://www.hancom.co.kr/hwpml/2011/head}"; HP = "{http://www.hancom.co.kr/hwpml/2011/paragraph}"
    numbered = set()
    try:
        for ppr in ET.fromstring(z.read("Contents/header.xml")).iter(f"{HH}paraPr"):
            hd = ppr.find(f".//{HH}heading")
            if hd is not None and (hd.get("type") or "NONE").upper() in ("OUTLINE", "NUMBER", "BULLET"):
                numbered.add(ppr.get("id"))
    except (KeyError, ET.ParseError):
        pass
    for n in names:
        if n.startswith("Contents/section") and n.endswith(".xml"):
            try:
                root = ET.fromstring(z.read(n))
            except ET.ParseError as e:
                issues.append({"level": "error", "rule": "XML 해석", "where": n, "msg": f"XML 로 읽을 수 없음: {e}"}); continue
            bad = [p for p in root.iter(f"{HP}p") if p.get("paraPrIDRef") in numbered]
            if bad:
                issues.append({"level": "error", "rule": "개요 문단", "where": n, "msg": f"개요·번호 문단 모양 문단 {len(bad)}개 — 자동 번호가 붙을 수 있음"})
    return issues
