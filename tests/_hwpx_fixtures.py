"""HWPX 시험 공용 도우미(시험 모음이 아님 — 이름에 test_ 를 붙이지 않는다): 판독 기록·양식 흉내 만들기와, 만든 HWPX 를 id 없이 견줄 수 있는
특징(hwpx_features)으로 줄이기. test_flow·test_hwpx_golden 이 같이 쓴다."""
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

from yeone_ocr.schema import PageReading, frac_bbox

HH = "{http://www.hancom.co.kr/hwpml/2011/head}"
HP = "{http://www.hancom.co.kr/hwpml/2011/paragraph}"


def rec(page: int, regions: list[dict], print_page: str = "") -> dict:
    pr = PageReading.model_validate({"pdf_page": page, "print": print_page, "regions": regions}).model_dump()
    return {"file": "책.pdf", "pdf_page": page, "merged": pr, "pass1": pr, "pass2": {}, "crops": {}, "verify": []}


def text_region(rid: str, lines: list[str], kind: str = "본문", cont: bool = False, box=(0.1, 0.1, 0.9, 0.9)) -> dict:
    return {"id": rid, "k": kind, "bbox": frac_bbox(box), "dir": "가로(좌→우)",
            "paras": [{"cols": [{"n": i + 1, "t": t} for i, t in enumerate(lines)], "cont": cont}]}


def tiny_png(path) -> str:
    """도판 블록용 40×30 PNG(가운데 검은 네모)."""
    from PIL import Image, ImageDraw
    im = Image.new("RGB", (40, 30), "white"); ImageDraw.Draw(im).rectangle([10, 8, 30, 22], fill="black")
    im.save(path, "PNG")
    return str(path)


def make_template(path: Path) -> None:
    """양식 흉내: '제목-절'(가운데)·'주석' 스타일과 본문 글·그림이 든 HWPX. '본문'은 줄 나눔이 어절 단위(KEEP_WORD)."""
    import pymupdf
    from hwpx import HwpxDocument
    doc = HwpxDocument.new()
    h = doc.oxml.headers[0]
    center = h.ensure_paragraph_format(base_para_pr_id=1, alignment="CENTER")
    big = doc.ensure_run_style(size=16, bold=True)
    h.ensure_style("제목-절", style_type="PARA", para_pr_id_ref=center, char_pr_id_ref=big)
    h.ensure_style("주석", style_type="PARA", para_pr_id_ref=1, char_pr_id_ref=doc.ensure_run_style(size=9))
    h.ensure_style("출처", style_type="CHAR", para_pr_id_ref=0, char_pr_id_ref=doc.ensure_run_style(size=8, italic=True))
    doc.add_paragraph("양식 본문 — 쓰지 않는다")
    d = pymupdf.open(); pg = d.new_page(width=40, height=40); pg.draw_rect(pymupdf.Rect(5, 5, 35, 35), fill=(0, 0, 0))
    doc.add_picture(pg.get_pixmap().tobytes("png"), "png", width_mm=10, height_mm=10); d.close()
    doc.save_to_path(str(path)); doc.close()


def make_template2(path: Path, page_number: bool = False) -> None:
    """양식 흉내 2: 구역이 둘, 본문에 그림, 글이 든 머리말·꼬리말(page_number 면 꼬리말은 쪽 번호), '제목-절'(가운데·줄 간격 130%)과
    굵은 글자의 '본경 원문' 스타일이 든 HWPX."""
    import pymupdf
    from hwpx import HwpxDocument
    doc = HwpxDocument.new()
    h = doc.oxml.headers[0]
    center = h.ensure_paragraph_format(base_para_pr_id=1, alignment="CENTER", line_spacing_percent=130)
    h.ensure_style("제목-절", style_type="PARA", para_pr_id_ref=center, char_pr_id_ref=doc.ensure_run_style(size=16, bold=True))
    h.ensure_style("본경 원문", style_type="PARA", para_pr_id_ref=1, char_pr_id_ref=doc.ensure_run_style(size=11, bold=True))
    doc.add_paragraph("양식 본문 — 쓰지 않는다")
    d = pymupdf.open(); pg = d.new_page(width=40, height=40); pg.draw_rect(pymupdf.Rect(5, 5, 35, 35), fill=(0, 0, 0))
    doc.add_picture(pg.get_pixmap().tobytes("png"), "png", width_mm=10, height_mm=10); d.close()
    doc.page.set_header(text="본초학 양식")
    if page_number:
        doc.set_page_number(position="BOTTOM_CENTER")
    else:
        doc.page.set_footer(text="— 양식 꼬리말 —")
    doc.add_section(); doc.add_paragraph("둘째 구역 — 쓰지 않는다", section=doc.sections[1])
    doc.save_to_path(str(path)); doc.close()


def make_template3(path: Path) -> None:
    """양식 흉내 3: 본초학 양식의 스타일 이름을 거의 다 갖춘 HWPX(제목-장·제목-절·제목-본초명+생약명·본경 원문(굵게)·별록원문·주석·그림캡션,
    글자 스타일 소주·출처·본경 조문). '본문'·'제목-본초명(소)' 은 일부러 없다(다음 후보·바탕글로)."""
    from hwpx import HwpxDocument
    doc = HwpxDocument.new()
    h = doc.oxml.headers[0]
    center = h.ensure_paragraph_format(base_para_pr_id=1, alignment="CENTER")
    rs = doc.ensure_run_style
    for name, ppr, cpr in (("제목-장", center, rs(size=18, bold=True)), ("제목-절", center, rs(size=15, bold=True)),
                           ("제목-본초명+생약명", 1, rs(size=13, bold=True)), ("본경 원문", 1, rs(size=11, bold=True)),
                           ("별록원문", 1, rs(size=11)), ("주석", 1, rs(size=9.5)), ("그림캡션", center, rs(size=9, italic=True))):
        h.ensure_style(name, style_type="PARA", para_pr_id_ref=ppr, char_pr_id_ref=cpr)
    for name, cpr in (("소주", rs(size=8.5)), ("출처", rs(size=8, italic=True)), ("본경 조문", rs(size=11, bold=True, color="#202020"))):
        h.ensure_style(name, style_type="CHAR", para_pr_id_ref=0, char_pr_id_ref=cpr)
    doc.add_paragraph("양식 본문 — 쓰지 않는다")
    doc.save_to_path(str(path)); doc.close()


# ---------- HWPX 특징(골든 견주기) ----------
def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _texts(el) -> str:
    return "".join(t.text or "" for t in el.iter(f"{HP}t"))


def hwpx_features(path) -> dict:
    """HWPX 를 id 없이 견줄 수 있는 꼴로: 맨 위 문단마다 스타일 이름·문단 모양·쪽 나누기와 런마다 글자 모양·든 것(글·책갈피·메모·쪽 번호·
    머리말·꼬리말·구역 설정·단·그림 크기·표). 글자 모양·문단 모양은 id 를 빼고 글꼴 번호를 글꼴 이름으로 바꾼 속성·자식 나무(canon)로 적는다."""
    with zipfile.ZipFile(path) as z:
        header = ET.fromstring(z.read("Contents/header.xml")); sec = ET.fromstring(z.read("Contents/section0.xml"))
        bindata = sum(1 for n in z.namelist() if n.startswith("BinData/"))
    fonts = {(ff.get("lang"), f.get("id")): f.get("face") for ff in header.iter(f"{HH}fontface") for f in ff.iter(f"{HH}font")}

    def canon(el) -> list:
        fref = _local(el.tag) == "fontRef"
        attrs = sorted((k, fonts.get((k.upper(), v), v) if fref else v) for k, v in el.attrib.items() if k not in ("id", "itemCnt"))
        return [_local(el.tag), [list(a) for a in attrs], [canon(c) for c in el]]
    charpr = {c.get("id"): canon(c) for c in header.iter(f"{HH}charPr")}
    parapr = {p.get("id"): canon(p) for p in header.iter(f"{HH}paraPr")}
    styles = {s.get("id"): s.get("name") for s in header.iter(f"{HH}style")}

    def walk(el, items: list) -> None:
        for ch in el:
            tag = _local(ch.tag)
            if tag == "t":
                items.append(["t", ch.text or ""])
                walk(ch, items)                                       # 글 안 탭·줄바꿈 꼬리(드묾)
            elif tag == "bookmark":
                items.append(["bm", ch.get("name")])
            elif tag == "fieldBegin" and ch.get("type") == "MEMO":
                items.append(["memo", "".join(_texts(s) for s in ch.iter(f"{HP}subList"))])
            elif tag in ("pageNum", "autoNum"):
                items.append(["pagenum", ch.get("pos") or ch.get("numType") or ""])
            elif tag in ("header", "footer"):
                inner = [["pagenum", n.get("pos") or n.get("numType") or ""] for n in ch.iter() if _local(n.tag) in ("pageNum", "autoNum")]
                items.append([tag, _texts(ch), inner])
            elif tag == "secPr":
                pp = ch.find(f"{HP}pagePr")
                items.append(["secPr", canon(pp) if pp is not None else None])
            elif tag == "colPr":
                items.append(["colPr"])
            elif tag == "pic":
                sz = ch.find(f"{HP}sz")
                if sz is None:
                    sz = ch.find(f"{HP}curSz")
                items.append(["pic", sz.get("width") if sz is not None else None, sz.get("height") if sz is not None else None])
            elif tag == "tbl":
                cells = [_texts(tc) for tc in ch.iter(f"{HP}tc")]
                items.append(["tbl", ch.get("rowCnt"), ch.get("colCnt"), cells])
            else:
                walk(ch, items)
    shapes: dict = {"char": [], "para": []}                           # 처음 나온 차례로 번호(같은 모양은 같은 번호 — 파일이 짧아진다)

    def ref(kind: str, c) -> int:
        tab = shapes[kind]
        if c not in tab:
            tab.append(c)
        return tab.index(c)
    paras = []
    for p in sec.findall(f"{HP}p"):
        runs = []
        for run in p.findall(f"{HP}run"):
            items: list = []
            walk(run, items)
            runs.append([ref("char", charpr.get(run.get("charPrIDRef"))), items])
        paras.append({"style": styles.get(p.get("styleIDRef")), "para": ref("para", parapr.get(p.get("paraPrIDRef"))),
                      "pageBreak": p.get("pageBreak"), "runs": runs})
    return {"paras": paras, "char_shapes": shapes["char"], "para_shapes": shapes["para"], "bindata": bindata}
