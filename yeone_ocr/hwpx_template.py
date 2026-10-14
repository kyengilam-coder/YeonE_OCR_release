"""사용자 양식(HWPX)의 스타일로 내용별 문서를 채운다. 양식의 쪽 설정(용지·여백)은 그대로 두고 본문은 비운 뒤, 블록의 쓰임(structure.ROLES)마다
양식 스타일을 골라 문단을 넣는다. 원본 PDF 쪽은 그 쪽이 시작되는 글자 자리의 책갈피('PDF 6쪽 · 인쇄 394쪽')로 남기고, 확인 등급 색·고지 메모는
양식 글자 모양 위에 덧입힌다(바탕은 양식 그대로, 색·굵기·형광펜만 바꾼 글자 모양을 따로 만든다).

한문 문단은 줄 나눔을 '글자 단위'로 바꾼 문단 모양을 쓴다: 양식 스타일 대부분이 '어절 단위'(KEEP_WORD)라 띄어쓰기 없는 한문이 긴 덩어리째
다음 줄로 넘어가며 줄 가운데 큰 빈칸이 생긴다. 스타일 연결은 그대로라 한글에서 스타일을 다시 적용하면 양식대로 돌아간다."""
from __future__ import annotations
import copy, re
from pathlib import Path
from .blocks import Block
from .grades import styled_runs, split_marks, GRADE_STYLE, STYLE_ALIAS

HH = "{http://www.hancom.co.kr/hwpml/2011/head}"
HP = "{http://www.hancom.co.kr/hwpml/2011/paragraph}"
HWPUNIT_PER_MM = 7200 / 25.4
# 쓰임 → 양식 스타일 이름(앞의 것이 있으면 그것). 없으면 바탕글. 올려 받은 본초학 양식의 이름을 기본으로 한다.
PARA_STYLES = {
    "chapter": ("제목-장", "편명"),
    "section": ("제목-절",),
    "subsection": ("제목-본초명(소)", "제목-절"),
    "entry": ("제목-본초명+생약명", "약재제목", "제목-본초명"),
    "body": ("본문",),
    "bonjing": ("본경 원문", "본문"),
    "bielu": ("별록원문", "본문"),
    "commentary": ("주석", "본문"),
    "small": ("본문",),
    "caption": ("그림캡션", "캡션"),
    "note": ("바탕글",),
}
CHAR_STYLES = {"small": ("소주",), "source": ("출처",), "bonjing": ("본경 조문",)}   # 글자 스타일(문단 안 범위)
HEADING_ROLES = {"chapter", "section", "subsection", "entry", "caption"}
COLOR_NOTE = "#666666"
_HAN = re.compile("[㐀-䶿一-鿿豈-﫿\U00020000-\U0003134f]")


def template_styles(doc) -> dict[str, dict]:
    """양식의 스타일: {이름: {"id", "type", "para", "char"}}."""
    out = {}
    for st in doc.oxml.headers[0].element.iter(f"{HH}style"):
        out[st.get("name", "")] = {"id": st.get("id"), "type": st.get("type"), "para": st.get("paraPrIDRef"), "char": st.get("charPrIDRef")}
    return out


def _break_word_para(header, base_id: str, cache: dict) -> str:
    """base_id 문단 모양을 복사해 한글(한자 포함) 줄 나눔만 '글자 단위'로 바꾼 문단 모양 id. 이미 글자 단위면 그대로."""
    if base_id in cache:
        return cache[base_id]
    root = header.element
    props = root.find(f".//{HH}paraProperties")
    base = next((p for p in props.iter(f"{HH}paraPr") if p.get("id") == str(base_id)), None) if props is not None else None
    brk = base.find(f".//{HH}breakSetting") if base is not None else None
    if brk is None or brk.get("breakNonLatinWord") == "BREAK_WORD":
        cache[base_id] = str(base_id)
        return cache[base_id]
    new = copy.deepcopy(base)
    new_id = str(max(int(p.get("id", "0")) for p in props.iter(f"{HH}paraPr")) + 1)
    new.set("id", new_id)
    for b in new.iter(f"{HH}breakSetting"):
        b.set("breakNonLatinWord", "BREAK_WORD")
    props.append(new)
    props.set("itemCnt", str(sum(1 for _ in props.iter(f"{HH}paraPr"))))
    header.mark_dirty()
    cache[base_id] = new_id
    return new_id


def _body_size_mm(doc) -> tuple[float, float]:
    """양식 쪽의 본문 너비·높이(mm)."""
    try:
        sec = doc.sections[0].element
        pp = sec.find(f".//{HP}pagePr"); mg = pp.find(f"{HP}margin")
        w = int(pp.get("width")) - int(mg.get("left")) - int(mg.get("right")) - int(mg.get("gutter", "0"))
        h = int(pp.get("height")) - int(mg.get("top")) - int(mg.get("bottom")) - int(mg.get("header", "0")) - int(mg.get("footer", "0"))
        return w / HWPUNIT_PER_MM, h / HWPUNIT_PER_MM
    except Exception:
        return 160.0, 230.0


def build_hwpx_template(blocks: list[Block], out_path: str, template: str, min_mm: float = 70, large_min_mm: float = 120,
                        highlight: bool = True, memos: bool = True, author: str = "YeonE_OCR") -> dict:
    """blocks(쓰임이 붙은 내용별 블록)를 양식 template 의 스타일로 out_path 에 쓴다. 돌려주는 값은 hwpx_builder.build_hwpx 와 같은 꼴에
    bookmarks(책갈피 수)·styles(쓰임 → 쓴 스타일 이름)·missing(양식에 없어 바탕글로 쓴 쓰임)을 더한 것."""
    from hwpx import HwpxDocument
    from .hwpx_memo import MemoWriter
    doc = HwpxDocument.open(template)
    for extra in list(doc.sections[1:]):
        doc.remove_section(extra)
    doc.sections[0].clear_body(on_control_content="strip")
    for hide in list(doc.sections[0].element.iter(f"{HP}pageHiding")):   # 양식 첫 쪽(표지)의 머리말·쪽 번호 감추기는 빼고
        ctrl = hide.getparent(); ctrl.getparent().remove(ctrl)
    try:
        doc.media.remove_unused_images()                              # 비운 양식 본문의 그림(BinData)도 꾸러미에서 뺀다
    except Exception:
        pass
    header = doc.oxml.headers[0]
    styles = template_styles(doc)
    base = styles.get("바탕글") or {"id": "0", "type": "PARA", "para": "0", "char": "0"}
    mw = MemoWriter(doc, author)
    stats = {"paragraphs": 0, "headings": 0, "images": 0, "tables": 0, "uncertain_items": 0, "marks": 0, "page_breaks": 0,
             "green": 0, "blue": 0, "red_ctx": 0, "memos": 0, "bookmarks": 0, "styles": {}, "missing": []}
    body_w, body_h = _body_size_mm(doc)
    brk_cache: dict = {}; run_cache: dict = {}; names: set = set()

    def para_style(role: str) -> dict:
        for name in PARA_STYLES.get(role, ("바탕글",)):
            st = styles.get(name)
            if st and st["type"] == "PARA":
                stats["styles"][role] = name
                return st
        if role not in stats["missing"]:
            stats["missing"].append(role)
        stats["styles"][role] = "바탕글"
        return base

    def char_style(kind: str) -> str | None:
        for name in CHAR_STYLES.get(kind, ()):
            st = styles.get(name)
            if st:
                return st["char"]
        return None

    def derived(base_char: str, kind: str) -> str:
        """양식 글자 모양 base_char 에 등급·작업 표시 모양을 덧입힌 글자 모양 id(같은 것은 한 번만)."""
        if kind == "body" or (not highlight and kind != "note"):
            return base_char
        key = (base_char, kind)
        if key not in run_cache:
            if kind == "note":
                kw = dict(color=COLOR_NOTE)
            else:
                g = GRADE_STYLE[STYLE_ALIAS.get(kind, kind)]
                kw = dict(bold=g["bold"], color=g["color"])
                if g["highlight"]:
                    kw["highlight"] = g["highlight"]
                if g["underline"]:
                    kw.update(underline=True, underline_shape=g["underline"], underline_color=g["color"])
            try:
                run_cache[key] = doc.ensure_run_style(base_char_pr_id=base_char, **kw)
            except (TypeError, ValueError):
                kw.pop("underline_shape", None); kw.pop("underline_color", None)
                run_cache[key] = doc.ensure_run_style(base_char_pr_id=base_char, **kw)
        return run_cache[key]

    def bookmark(p, label: str, cpr: str) -> None:
        name = label; k = 2
        while name in names:
            name = f"{label} ({k})"; k += 1
        names.add(name)
        p.add_bookmark(name, char_pr_id_ref=cpr)
        stats["bookmarks"] += 1

    def write(b: Block, role: str) -> None:
        st = para_style(role)
        text = b.text
        han = len(_HAN.findall(text)) >= max(1, len(text.strip())) * 0.5
        ppr = _break_word_para(header, st["para"], brk_cache) if han and role not in HEADING_ROLES else st["para"]
        p = doc.add_paragraph("", para_pr_id_ref=ppr, style_id_ref=st["id"], char_pr_id_ref=st["char"], include_run=False, inherit_style=False)
        notes = list(b.memos) if memos else []
        if not memos and b.memos:
            text = text + "".join(f"〔고지: {m}〕" for _, m in b.memos)
        segs = styled_runs(text, b.spans if highlight else (), notes) if text else []
        stats["marks"] += sum(1 for _, k in split_marks(text) if k == "mark")
        cs = [None] * len(text)                                        # 글자마다 글자 스타일(양식의 소주·출처·본경 조문)
        if role == "small":
            cs = ["small"] * len(text)
        for a, e, kind in b.cstyles:
            for i in range(max(0, a), min(len(text), e)):
                cs[i] = kind
        anchors: dict[int, list[str]] = {}
        for o, label in b.anchors:
            anchors.setdefault(min(max(0, o), len(text)), []).append(label)
        pos = 0
        for seg, kind, msgs in segs:
            if role == "note" and kind == "body":
                kind = "note"                                           # 작업 표시·확인 목록은 회색
            cuts = sorted({pos, pos + len(seg)} | {o for o in anchors if pos < o < pos + len(seg)}
                          | {i for i in range(pos + 1, pos + len(seg)) if cs[i] != cs[i - 1]})
            for a, e in zip(cuts, cuts[1:]):
                kind_c = cs[a] if a < len(cs) else None
                base_char = (char_style(kind_c) if kind_c else None) or st["char"]
                cpr = derived(base_char, STYLE_ALIAS.get(kind, kind) if kind not in ("body", "note") else kind)
                for label in anchors.pop(a, []):
                    bookmark(p, label, cpr)
                r = p.add_run(text[a:e], char_pr_id_ref=cpr)
                if kind in ("green", "blue", "red_ctx") and text[a:e].strip():
                    stats[kind] += 1
                for m in (msgs if a == pos else []):
                    if mw.wrap_run(getattr(r, "element", None), m):
                        stats["memos"] += 1
            pos += len(seg)
        for o in sorted(anchors):                                       # 글 끝(다음 쪽 글이 없는 쪽 등)
            for label in anchors[o]:
                bookmark(p, label, st["char"])
        stats["paragraphs"] += 1
        if role in HEADING_ROLES:
            stats["headings"] += 1

    for b in blocks:
        if b.type in ("heading", "para"):
            write(b, b.role or ("section" if b.type == "heading" else "body"))
        elif b.type == "note" or b.type == "meta":
            write(Block(type="para", text=b.text, memos=b.memos, anchors=b.anchors), "note")
        elif b.type == "image":
            data = Path(b.path).read_bytes()
            floor = large_min_mm if b.kind in ("원형나경도", "복잡한표", "계통도", "도식") else min_mm
            w = max(min(floor, body_w), min(body_w, body_w * max(0.0, min(1.0, b.frac_w)) * 1.15))
            h = w * b.ratio
            if h > body_h * 0.9:
                h = body_h * 0.9; w = h / b.ratio
            try:
                doc.add_picture(data, Path(b.path).suffix.lstrip(".").lower() or "png", width_mm=round(w, 1), height_mm=round(h, 1), align="CENTER")
            except Exception:
                doc.add_picture(data, Path(b.path).suffix.lstrip(".").lower() or "png", width_mm=round(w, 1), height_mm=round(h, 1))
            stats["images"] += 1
        elif b.type == "table":
            t = doc.add_table(len(b.rows), len(b.rows[0]))
            for i, row in enumerate(b.rows):
                for j, cell in enumerate(row):
                    t.cell(i, j).text = cell
            stats["tables"] += 1
        elif b.type == "uncertain":
            write(Block(type="para", text=b.text or "[판독 확인 필요]"), "note")
            for it in b.items:
                write(Block(type="para", text="· " + it), "note"); stats["uncertain_items"] += 1
        elif b.type == "report":
            for line in ["〔작업 기록〕"] + b.text.splitlines():
                write(Block(type="para", text=line), "note")
    try:                                                              # 쪽 번호: 컨트롤을 구역 설정 문단으로 옮겨 맨 앞 빈 줄이 생기지 않게
        doc.set_page_number(position="BOTTOM_CENTER")
        sec = doc.sections[0].element
        paras = sec.findall(f"{HP}p")
        holder = next((q for q in paras[1:] if q.find(f".//{HP}pageNum") is not None and not "".join(t.text or "" for t in q.iter(f"{HP}t")).strip()), None)
        if holder is not None and paras:
            for run in holder.findall(f"{HP}run"):
                paras[0].insert(len(paras[0].findall(f"{HP}run")), run)
            sec.remove(holder)
            doc.sections[0].mark_dirty()
    except Exception:
        pass
    try:
        doc.save_to_path(out_path)
    finally:
        doc.close()
    re_ = HwpxDocument.open(out_path)
    try:
        try:
            imgs = re_.media.images
            stats["images_in_file"] = len(list(imgs() if callable(imgs) else imgs))
        except Exception:
            stats["images_in_file"] = len(list(re_.list_images()))
        try:
            rep = re_.validate(); stats["validate_issues"] = len(getattr(rep, "issues", ()) or ())
        except Exception as e:
            stats["validate_issues"] = f"검사 불가: {e}"
    finally:
        re_.close()
    return stats
