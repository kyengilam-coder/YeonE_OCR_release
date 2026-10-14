"""내용별 문서(--layout flow): 쪽 책갈피 조립, 本草 조문 짜임 규칙, 획 굵기(本經·別錄), 양식 HWPX 채우기. 모델 호출 없음."""
import contextlib
import io
import re
import tempfile
import unittest
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pymupdf

from yeone_ocr.blocks import Block, assemble_blocks
from yeone_ocr.pdfio import images_to_pdf, page_frame, page_image
from yeone_ocr.schema import PageReading, frac_bbox
from yeone_ocr.structure import apply_structure, bold_spans, measure_bold

HW = "{http://www.hancom.co.kr/hwpml/2011/head}"
HP = "{http://www.hancom.co.kr/hwpml/2011/paragraph}"


def rec(page: int, regions: list[dict], print_page: str = "") -> dict:
    pr = PageReading.model_validate({"pdf_page": page, "print": print_page, "regions": regions}).model_dump()
    return {"file": "책.pdf", "pdf_page": page, "merged": pr, "pass1": pr, "pass2": {}, "crops": {}, "verify": []}


def text_region(rid: str, lines: list[str], kind: str = "본문", cont: bool = False, box=(0.1, 0.1, 0.9, 0.9)) -> dict:
    return {"id": rid, "k": kind, "bbox": frac_bbox(box), "dir": "가로(좌→우)",
            "paras": [{"cols": [{"n": i + 1, "t": t} for i, t in enumerate(lines)], "cont": cont}]}


class FlowAssembly(unittest.TestCase):
    def test_pages_become_bookmarks_and_split_paragraph_is_joined(self):
        r1 = rec(5, [{"id": "t", "k": "제목", "title": "菜　下", "lv": 2, "bbox": frac_bbox((0.3, 0.05, 0.7, 0.1))},
                     text_region("a", ["香薷　味辛，微温。主霍乱腹痛"])], print_page="394")
        r2 = rec(6, [text_region("b", ["吐下，散水肿。"], cont=True), text_region("c", ["［陶隐居云］　处处有此。"])], print_page="395")
        page = assemble_blocks([r1, r2], grades=False)
        flow = assemble_blocks([r1, r2], grades=False, layout="flow")
        self.assertEqual([b.type for b in page].count("meta"), 2)
        self.assertNotIn("meta", [b.type for b in flow])
        self.assertIn("〔→PDF 6쪽/인쇄 395쪽〕", page[2].text if page[2].type == "para" else "".join(b.text for b in page))
        joined = next(b for b in flow if b.type == "para")
        self.assertEqual(joined.text, "香薷　味辛，微温。主霍乱腹痛吐下，散水肿。")                # 쪽을 넘은 문단을 표시 없이 잇는다
        self.assertEqual(flow[0].anchors, [(0, "PDF 5쪽 · 인쇄 394쪽")])
        self.assertEqual(joined.anchors, [(len("香薷　味辛，微温。主霍乱腹痛"), "PDF 6쪽 · 인쇄 395쪽")])
        with self.assertRaises(ValueError):
            assemble_blocks([r1], layout="pages")

    def test_legend_memo_goes_to_first_text_and_pages_without_text_carry_over(self):
        r1 = rec(1, [{"id": "f", "k": "삽화", "bbox": frac_bbox((0.1, 0.1, 0.9, 0.9))}])
        r2 = rec(2, [text_region("a", ["天地玄黃"])])
        blocks = assemble_blocks([r1, r2], grades=True, layout="flow")
        para = next(b for b in blocks if b.type == "para")
        self.assertEqual([a for _, a in para.anchors], ["PDF 1쪽", "PDF 2쪽"])         # 그림만 있는 쪽의 책갈피도 다음 글 첫머리에
        self.assertTrue(para.memos and para.memos[0][0] == 0 and "범례" in para.memos[0][1])


def title_region(rid: str, text: str, top: float = 0.03, level: int = 1) -> dict:
    return {"id": rid, "k": "제목", "bbox": frac_bbox((0.2, top, 0.8, top + 0.04)), "title": text, "lv": level}


class RunningHeads(unittest.TestCase):
    """쪽마다 찍힌 머리말(書眉)을 판독이 '제목' 으로 읽으면 내용별 문서에서 장 제목이 되어 글을 끊었다(개보본초 462쪽에서 제목-장 263개)."""
    def test_running_heads_are_dropped_and_the_paragraph_rejoins(self):
        def body(rid, t, cont=False, top=0.2):
            return text_region(rid, [t], cont=cont, box=(0.1, top, 0.9, top + 0.3))
        recs = [rec(1, [title_region("t", "開寶本草草部上品之上  卷第六", top=0.05), body("b", "青芝本經145 赤芝本經145")]),
                rec(2, [body("b", "黃芝本經145", cont=True), title_region("t", "芫花草草部上品之上卷第六")]),      # 영역 순서가 거꾸로·잘못 읽은 머리말
                rec(3, [title_region("t", "開寶本草草部上品之上卷第六"), body("b", "肉蓯蓉本經165", cont=True)]),
                rec(4, [title_region("t", "開寶本草草部上品之下  卷第七"), body("b", "地膚子本經165")]),          # 권이 바뀌면 새 장
                rec(5, [title_region("t", "雲母", top=0.5), body("b", "味甘，平。", top=0.6)])]
        st = {}
        blocks = assemble_blocks(recs, grades=False, layout="flow", stats=st)
        heads = [b.text for b in blocks if b.type == "heading"]
        self.assertEqual(heads, ["開寶本草草部上品之上  卷第六", "開寶本草草部上品之下  卷第七", "雲母"])
        self.assertEqual(st["running_heads"], 2)
        para = next(b for b in blocks if b.type == "para")
        self.assertEqual(para.text, "青芝本經145 赤芝本經145黃芝本經145肉蓯蓉本經165")                        # 머리말에 끊기지 않고 이어짐
        self.assertEqual([a for a in para.anchors], [(15, "PDF 2쪽"), (22, "PDF 3쪽")])                   # 쪽 책갈피는 그 쪽 첫 글자에
        recs[3]["merged"]["header"] = "開寶本草 卷第七"; recs[3]["merged"]["regions"][0]["title"] = "開寶本草卷第七"     # 판독이 적은 머리말과 같으면 위치와 상관없이
        self.assertNotIn("開寶本草卷第七", [b.text for b in assemble_blocks(recs, grades=False, layout="flow") if b.type == "heading"])
        self.assertIn("芫花草草部上品之上卷第六", [b.text for b in assemble_blocks(recs, grades=False) if b.type == "heading"])  # 원본 쪽대로는 그대로


class HeadingEntries(unittest.TestCase):
    """판독이 약재명을 제목으로 읽은 블록: 다음 문단이 味·一名 으로 시작하면 약재명, 제목에 味… 가 붙었으면 떼어 다음 문단 앞에 잇는다."""
    def test_herb_headings_become_entries(self):
        blocks = [Block(type="heading", text="開寶本草玉石等部中品 卷第四", level=1, kind="제목"),
                  Block(type="heading", text="雄黃", level=1, kind="제목"),
                  Block(type="para", text="〔疑: 雄黄〕味苦，甘，平。主寒熱。", kind="본문"),
                  Block(type="heading", text="孔公孽　味辛，溫，無毒。", level=1, kind="제목"),
                  Block(type="para", text="主傷食不化。", kind="본문", spans=[(1, 2, "green")], anchors=[(0, "PDF 9쪽")]),
                  Block(type="heading", text="附子\u2003味辛·甘，溫。", level=2, kind="제목"),
                  Block(type="para", text="〔陶隱居云〕此物。", kind="본문"),
                  Block(type="heading", text="吐唾血", level=2, kind="제목"),
                  Block(type="para", text="生地黃『本經』寒。", kind="본문"),
                  Block(type="heading", text="菜　下", level=2, kind="제목"),
                  Block(type="para", text="香薷　味辛，微溫。", kind="본문")]
        out, st = apply_structure(blocks, "auto")
        got = [(b.type, b.role, b.text) for b in out]
        self.assertEqual(got, [("heading", "chapter", "開寶本草玉石等部中品 卷第四"),
                               ("heading", "entry", "雄黃"), ("para", "body", "〔疑: 雄黄〕味苦，甘，平。主寒熱。"),
                               ("heading", "entry", "孔公孽"), ("para", "body", "味辛，溫，無毒。主傷食不化。"),
                               ("heading", "entry", "附子"), ("para", "body", "味辛·甘，溫。"), ("para", "commentary", "〔陶隱居云〕此物。"),
                               ("heading", "section", "吐唾血"), ("para", "body", "生地黃『本經』寒。"),   # 序例 병증 제목 뒤 生… 은 약재명이 아님
                               ("heading", "section", "菜　下"), ("heading", "entry", "香薷"), ("para", "bielu", "味辛，微溫。")])  # 다음 문단이 제 이름으로 시작
        joined = out[4]
        self.assertEqual((joined.spans, joined.anchors), ([(9, 10, "green")], [(8, "PDF 9쪽")]))      # 이은 문단의 색·책갈피 자리도 함께 밀림
        self.assertEqual(st["entry"], 4)


class Structure(unittest.TestCase):
    def para(self, text: str, **kw) -> Block:
        return Block(type="para", text=text, kind=kw.pop("kind", "본문"), **kw)

    def test_bencao_entry_commentary_and_source(self):
        blocks = [self.para("香薷　味辛，微温。主霍乱腹痛吐下，散水肿。", anchors=[(0, "PDF 1쪽")], memos=[(0, "고지")],
                            spans=[(5, 6, "green")]),
                  self.para("［陶隐居云］　处处有此，惟供生食。"),
                  self.para("秦荻梨　味辛，温，无毒。所在有之。　唐附"),
                  self.para("中，今人食之，录在菜部也。"),
                  Block(type="heading", text="菜　下", level=2)]
        out, stats = apply_structure(blocks, "bencao")
        self.assertEqual([(b.type, b.role, b.text) for b in out[:2]],
                         [("heading", "entry", "香薷"), ("para", "bielu", "味辛，微温。主霍乱腹痛吐下，散水肿。")])
        self.assertEqual(out[0].anchors, [(0, "PDF 1쪽")]); self.assertEqual(out[0].memos, [(0, "고지")])
        self.assertEqual(out[1].spans, [(2, 3, "green")])                        # 떼어 낸 만큼 등급 범위도 옮긴다(微)
        self.assertEqual(out[2].role, "commentary")
        src = out[4]
        self.assertEqual([src.text[a:e] for a, e, k in src.cstyles if k == "source"], ["唐附"])
        self.assertEqual(out[5].role, "body"); self.assertEqual(out[6].role, "section")
        self.assertEqual(stats["entry"], 2)
        self.assertEqual("".join(b.text for b in out).replace("　", ""), "".join(b.text for b in blocks).replace("　", ""))  # 글자는 그대로

    def test_entry_without_space_uses_measured_gap_and_auto_needs_three(self):
        b = self.para("苦瓠味苦，寒，有毒。", cstyles=[(2, 2, "gap")])
        out, _ = apply_structure([b], "bencao")
        self.assertEqual([x.text for x in out], ["苦瓠", "味苦，寒，有毒。"])
        out, _ = apply_structure([b], "auto")                                     # 한 번뿐이면 우연한 꼴일 수 있어 떼지 않는다
        self.assertEqual([x.text for x in out], ["苦瓠味苦，寒，有毒。"])
        three = [self.para(f"{n}　味甘，平。") for n in ("水斳", "落葵", "繁蔞")]
        out, _ = apply_structure(three, "auto")
        self.assertEqual([x.role for x in out].count("entry"), 3)
        out, _ = apply_structure(three, "none")
        self.assertEqual([x.role for x in out], ["body"] * 3)

    def test_bold_ratio_decides_bonjing_and_keeps_mixed_ranges(self):
        t = "苦瓠　味苦寒有毒主大水面目四肢浮腫下水"
        full = self.para(t, cstyles=[(3, len(t), "bold")])
        half = self.para(t, cstyles=[(3, 9, "bold")])
        out_full, _ = apply_structure([full], "bencao")
        out_half, _ = apply_structure([half], "bencao")
        self.assertEqual(out_full[1].role, "bonjing"); self.assertEqual(out_full[1].cstyles, [])
        self.assertEqual(out_half[1].role, "bielu")
        self.assertEqual([(a, e, k) for a, e, k in out_half[1].cstyles], [(0, 6, "bonjing")])

    def test_bold_spans_smooth_noise_and_merge_short_runs(self):
        thin, thick = 3.0, 4.6
        ws = [thick] * 6 + [3.1, 2.6] + [thick] * 6 + [thin] * 10 + [4.2] + [thin] * 5
        spans = bold_spans({("r", 0): [(i, w) for i, w in enumerate(ws)]}, median=3.1)
        self.assertEqual(spans, {("r", 0): [(0, 14)]})                            # 가운데 두 글자의 흔들림·뒤의 낱글자는 무시


def bold_scan(tmp: Path) -> tuple[Path, PageReading]:
    """보통 글씨 두 문단 + 굵은 글씨(획을 덧그림) 한 문단의 가로쓰기 쪽."""
    W, H, fs = 420, 300, 18
    lines = [("香薷　味辛微温主霍乱腹痛吐下散水肿", False), ("苦瓠　味苦寒有毒主大水面目四肢浮肿", True), ("秦荻梨　味辛温无毒主心腹冷胀下气消食", False)]
    d = pymupdf.open(); p = d.new_page(width=W, height=H)
    regions = []
    for i, (t, bold) in enumerate(lines):
        y = 60 + i * 60
        kw = dict(render_mode=2, border_width=0.9 / fs, color=(0, 0, 0), fill=(0, 0, 0)) if bold else {}   # 획 테두리 0.9pt(글자 크기에 대한 비)
        p.insert_text((30, y + fs * 0.88), t, fontname="china-s", fontsize=fs, **kw)
        regions.append(text_region(f"r{i}", [t], box=((30 - 4) / W, (y - 4) / H, (30 + len(t) * fs + 4) / W, (y + fs + 4) / H)))
    png = tmp / "bold.png"; p.get_pixmap(dpi=200, colorspace=pymupdf.csGRAY).save(png); d.close()
    pdf = tmp / "bold.pdf"; pdf.write_bytes(images_to_pdf([png], default_dpi=200))
    return pdf, PageReading.model_validate({"pdf_page": 1, "regions": regions})


class Bold(unittest.TestCase):
    def test_measure_bold_finds_only_the_thick_paragraph_and_entry_gaps(self):
        with tempfile.TemporaryDirectory() as td:
            pdf, pr = bold_scan(Path(td))
            with pymupdf.open(pdf) as d:
                img, _ = page_image(d, 1); frame = page_frame(d, 1)
            bold, info = measure_bold([(1, pr, img, frame, False)])
        kinds = {key: {k for _, _, k in v} for key, v in bold.items()}
        self.assertEqual([key for key, ks in kinds.items() if "bold" in ks], [(1, "r1", 0)])
        self.assertTrue(all("gap" in kinds.get((1, f"r{i}", 0), set()) for i in range(3)), kinds)   # 약재명 뒤 빈칸
        self.assertEqual(info["bold_paragraphs"], 1)
        from yeone_ocr.structure import _box_gap, _ink, _ink_gap
        from yeone_ocr.textlayer import plan_page
        lines = plan_page(pr, img, frame, None, False)                              # 상자를 못 맞춘 줄(ink~)에 쓰는 먹 빈칸 재기도 같은 답
        self.assertEqual([_ink_gap(_ink(img), ln) for ln in lines], [_box_gap(ln) for ln in lines])
        self.assertEqual([_box_gap(ln) for ln in lines], [2, 2, 3])
        blocks = assemble_blocks([rec(1, [r.model_dump() for r in pr.regions])], grades=False, layout="flow", bold=bold)
        out, _ = apply_structure(blocks, "bencao")
        self.assertEqual([(b.role, b.text[:3]) for b in out if b.type == "para"], [("bielu", "味辛微"), ("bonjing", "味苦寒"), ("bielu", "味辛温")])


def make_template(path: Path) -> None:
    """양식 흉내: '제목-절'(가운데)·'주석' 스타일과 본문 글·그림이 든 HWPX. '본문'은 줄 나눔이 어절 단위(KEEP_WORD)."""
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


class Template(unittest.TestCase):
    def test_template_styles_bookmarks_break_word_and_clean_body(self):
        from yeone_ocr.hwpx_template import build_hwpx_template
        from yeone_ocr.verify import check_hwpx_file
        with tempfile.TemporaryDirectory() as td:
            tpl = Path(td) / "양식.hwpx"; make_template(tpl)
            blocks = [Block(type="heading", text="菜　下", level=2, role="section", anchors=[(0, "PDF 1쪽 · 인쇄 394쪽")]),
                      Block(type="heading", text="香薷", level=3, role="entry"),
                      Block(type="para", text="味辛，微温。主霍乱〔腹/腸〕痛吐下，散水肿。", role="bielu", anchors=[(9, "PDF 2쪽")]),
                      Block(type="para", text="［唐本注云］　功用相似。　唐附", role="commentary", cstyles=[(13, 15, "source")])]
            out = Path(td) / "out.hwpx"
            st = build_hwpx_template(blocks, str(out), str(tpl))
            self.assertEqual(check_hwpx_file(str(out)), [])
            self.assertEqual((st["bookmarks"], st["images_in_file"], st["marks"]), (2, 0, 1))
            self.assertEqual(st["styles"]["section"], "제목-절"); self.assertEqual(st["styles"]["commentary"], "주석")
            self.assertIn("entry", st["missing"]); self.assertNotIn("bielu", st["missing"])  # 양식에 없는 것은 바탕글로
            self.assertEqual(st["styles"]["bielu"], "본문")                                 # '별록원문'이 없으면 다음 후보 '본문'으로
            with zipfile.ZipFile(out) as z:
                sec = z.read("Contents/section0.xml").decode("utf-8"); head = z.read("Contents/header.xml").decode("utf-8")
        names = dict(re.findall(r'<hh:style id="(\d+)" type="\w+" name="([^"]*)"', head))
        paras = ET.fromstring(sec).findall(f"{HP}p")                                # 맨 위 문단만(꼬리말 안 문단은 빼고)
        self.assertNotIn("양식 본문", sec)
        self.assertIsNotNone(paras[0].find(f".//{HP}secPr")); self.assertIsNotNone(paras[0].find(f".//{HP}pageNum"))
        self.assertEqual("".join(t.text or "" for t in paras[0].iter(f"{HP}t")), "")  # 쪽 설정·쪽 번호는 첫 문단에, 빈 줄 없이
        body = paras[1:]
        self.assertEqual([names[q.get("styleIDRef")] for q in body], ["제목-절", "바탕글", "본문", "주석"])
        marks = lambda q: [("bm", e.get("name")) if e.tag == f"{HP}bookmark" else ("t", e.text) for e in q.iter() if e.tag in (f"{HP}bookmark", f"{HP}t")]
        self.assertEqual(marks(body[0])[0], ("bm", "PDF 1쪽 · 인쇄 394쪽"))
        m3 = marks(body[2])
        self.assertLess(m3.index(("bm", "PDF 2쪽")), m3.index(("t", "〔腹/腸〕")))       # 책갈피는 그 글자 자리(9번째) 앞에
        ppr = body[3].get("paraPrIDRef")                                            # 주석(본문 바탕 KEEP_WORD) → 글자 단위 사본
        self.assertRegex(head, rf'<hh:paraPr id="{ppr}"(?:(?!</hh:paraPr>).)*breakLatinWord="BREAK_WORD"(?:(?!</hh:paraPr>).)*breakNonLatinWord="BREAK_WORD"')   # 반각 ,. 이 든 한자 덩어리도 글자 단위로
        src_run = next(r for r in body[3].findall(f"{HP}run") if (r.findtext(f"{HP}t") or "") == "唐附")
        char_of_source = re.search(r'<hh:style id="\d+" type="CHAR" name="출처"[^>]*charPrIDRef="(\d+)"', head).group(1)
        self.assertEqual(src_run.get("charPrIDRef"), char_of_source)

    def test_numbered_outline_paragraphs_are_still_flagged(self):
        from hwpx import HwpxDocument
        from yeone_ocr.verify import check_hwpx_file
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "outline.hwpx"
            doc = HwpxDocument.new(); doc.add_heading("제목", level=1); doc.save_to_path(str(out)); doc.close()
            self.assertTrue(any(i["rule"] == "개요 문단" for i in check_hwpx_file(str(out))))


class Command(unittest.TestCase):
    def test_build_flow_with_template_from_mock_reading(self):
        from yeone_ocr import cli
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td); tpl = tmp / "양식.hwpx"; make_template(tpl)
            pdf, pr = bold_scan(tmp)
            doc = pymupdf.open(pdf)                                                  # mock 판독기는 세로 열 글자층을 옮긴다
            doc[0].insert_text((380, 40), "香薷　味辛微温", fontname="china-s", fontsize=18, rotate=270, render_mode=3)
            src = tmp / "책.pdf"; doc.save(src); doc.close()
            w = str(tmp / "w"); log = io.StringIO()
            with contextlib.redirect_stdout(log):
                cli.main(["run", str(src), "--work", w, "--reader", "mock", "--checkpoint", "0", "--mode", "vertical", "--no-normalize"])
                rc = cli.main(["build", str(src), "--work", w, "--layout", "flow", "--template", str(tpl), "--no-normalize", "--out", "내용별"])
            self.assertEqual(rc, 0, log.getvalue()[-800:])
            self.assertIn("원본 쪽 책갈피 1개", log.getvalue()); self.assertIn("내용별 짜임", log.getvalue())
            self.assertTrue((tmp / "w" / "out" / "내용별.hwpx").exists())

    def test_cli_options_and_window_command(self):
        from yeone_ocr.cli import build_parser
        a = build_parser().parse_args(["build", "x.pdf", "--layout", "flow", "--template", "t.hwpx", "--structure", "bencao", "--no-bold"])
        self.assertEqual((a.layout, a.template, a.structure, a.no_bold), ("flow", "t.hwpx", "bencao", True))
        a = build_parser().parse_args(["run", "x.pdf"])
        self.assertEqual((a.layout, a.template, a.structure, a.no_bold), ("page", None, "auto", False))
        try:
            from yeone_ocr import gui
        except ImportError:
            self.skipTest("tkinter 없음")
        V = lambda v: SimpleNamespace(get=lambda: v)
        app = SimpleNamespace(v_docx=V(False), v_join=V(True), v_hl=V(True), v_ctx=V(True), v_pb=V(True), v_tlpdf=V(False),
                              v_layout=V(gui.LAYOUT_LABELS["flow"]), v_template=V(" C:/양식.hwpx "), v_bold=V(False), v_ctxmodel=V(False),
                              check_list_mode=lambda: "none", memo_mode=lambda: "notice")
        app.layout_key = lambda: gui.App.layout_key(app)
        o = gui.App._build_opts(app)
        a = build_parser().parse_args(["build", "x.pdf"] + o)
        self.assertEqual((a.layout, a.template, a.no_bold, a.no_context_model), ("flow", "C:/양식.hwpx", True, True))
        app.v_layout = V(gui.LAYOUT_LABELS["page"])
        self.assertNotIn("--layout", gui.App._build_opts(app))
        self.assertEqual((gui.DEFAULTS["layout"], gui.DEFAULTS["template"], gui.DEFAULTS["bold_split"]), ("page", "", True))

    def test_window_greys_out_options_the_layout_does_not_use(self):
        try:
            from yeone_ocr import gui
        except ImportError:
            self.skipTest("tkinter 없음")
        from unittest import mock
        V = lambda v: SimpleNamespace(get=lambda: v)
        app = SimpleNamespace(v_layout=V(gui.LAYOUT_LABELS["flow"]), cb_pb=mock.Mock(), te_template=mock.Mock(), cb_bold=mock.Mock())
        app.layout_key = lambda: gui.App.layout_key(app)
        gui.App._layout_changed(app)                                                # 내용별: 쪽 나누기만 흐리게
        self.assertEqual([w.state.call_args.args[0] for w in (app.cb_pb, app.te_template, app.cb_bold)], [["disabled"], ["!disabled"], ["!disabled"]])
        app.v_layout = V(gui.LAYOUT_LABELS["page"])
        gui.App._layout_changed(app)                                                # 원본 쪽대로: 양식·획 굵기를 흐리게
        self.assertEqual([w.state.call_args.args[0] for w in (app.cb_pb, app.te_template, app.cb_bold)], [["!disabled"], ["disabled"], ["disabled"]])


if __name__ == "__main__":
    unittest.main()
