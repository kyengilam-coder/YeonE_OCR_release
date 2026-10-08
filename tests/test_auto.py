"""0.6.0 — 한 번에 인식: 읽기 방향·인쇄 쪽수 차이 자동 판정, 이전 판 기록 갱신, 평면 구획, 대조 항목의 자형 3차 판별, 명령줄 선택지.
실행: python -m unittest discover -s tests   (저장소 루트에서)"""
import os, sys, json, tempfile, unittest
from argparse import Namespace
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import pymupdf
from PIL import Image
from yeone_ocr.schema import PageReading, Region, Paragraph, Column, RegionReread, Uncertain, region_text
from yeone_ocr.state import Work


def vertical_pdf(n: int = 5, label_from: int = 30):
    """세로쓰기 번체 글자층 + 아래쪽 '- N -' 쪽번호."""
    doc = pymupdf.open()
    for i in range(n):
        pg = doc.new_page(width=500, height=800)
        for k in range(3):
            pg.insert_text((450 - k * 30, 60), "天地玄黃宇宙洪荒日月盈昃辰宿列張", fontname="china-t", fontsize=14, rotate=270)
        pg.insert_text((230, 770), f"- {i + label_from} -", fontname="helv", fontsize=10)
    return doc


def horizontal_pdf(n: int = 5):
    """가로쓰기 간체 글자층, 쪽번호 없음."""
    doc = pymupdf.open()
    for i in range(n):
        pg = doc.new_page(width=500, height=800)
        for k in range(6):
            pg.insert_text((60, 80 + k * 24), "车前子味甘寒无毒主气癃止痛利水道小便除湿痹", fontname="china-s", fontsize=12)
    return doc


def blank_pdf(n: int = 3):
    doc = pymupdf.open()
    for _ in range(n):
        doc.new_page(width=500, height=800)
    return doc


class AutoMode(unittest.TestCase):
    """읽기 방향은 글자층으로 판정하고 결과 폴더(state.json)에 적어 두어 다음 명령이 그대로 따른다."""
    def tearDown(self):
        from yeone_ocr.cli import apply_mode
        apply_mode(Namespace(mode="vertical"))

    def test_detect_mode(self):
        from yeone_ocr.cli import detect_mode
        m, why = detect_mode(vertical_pdf()); self.assertEqual(m, "vertical"); self.assertIn("세로 열 글자", why)
        m, why = detect_mode(horizontal_pdf()); self.assertEqual(m, "horizontal_zh"); self.assertIn("가로 줄 글자", why)
        m, why = detect_mode(blank_pdf()); self.assertEqual(m, "vertical"); self.assertIn("글자층이 거의 없어", why)

    def test_detect_print_offset(self):
        from yeone_ocr.cli import detect_print_offset
        self.assertEqual(detect_print_offset(vertical_pdf(12, label_from=30)), 29)     # PDF 1쪽 = 인쇄 30쪽
        self.assertIsNone(detect_print_offset(horizontal_pdf(12)))                      # 쪽번호 표기가 없으면 모름
        self.assertIsNone(detect_print_offset(blank_pdf(12)))

    def test_resolve_mode_remembers_decision(self):
        from yeone_ocr.cli import resolve_mode
        from yeone_ocr import prompts, merge
        with tempfile.TemporaryDirectory() as td:
            work = Work(os.path.join(td, "a.pdf"), os.path.join(td, "w"))
            a = Namespace(mode="auto")
            self.assertEqual(resolve_mode(a, horizontal_pdf(), work), "horizontal_zh")
            self.assertEqual(a.mode, "horizontal_zh"); self.assertEqual(work.load_state()["mode"], "horizontal_zh")
            self.assertEqual(prompts.MODE, "horizontal_zh"); self.assertTrue(merge.FOLD_ZH)
            # 결과 폴더에 적힌 값이 글자층 판정보다 앞선다(다시 계산·3차 판별·책갈피 OCR 이 같은 모드를 따르도록)
            b = Namespace(mode="auto")
            self.assertEqual(resolve_mode(b, vertical_pdf(), work), "horizontal_zh")
            # 명시한 모드는 그대로 쓰고 적어 둔다
            c = Namespace(mode="vertical")
            self.assertEqual(resolve_mode(c, horizontal_pdf(), work), "vertical")
            self.assertEqual(work.load_state()["mode"], "vertical"); self.assertEqual(prompts.MODE, "vertical"); self.assertFalse(merge.FOLD_ZH)
            # 결과 폴더가 없어도(work=None) 판정은 된다
            self.assertEqual(resolve_mode(Namespace(mode="auto"), horizontal_pdf(), None), "horizontal_zh")

    def test_refresh_stale_records(self):
        from yeone_ocr import __version__
        from yeone_ocr.cli import refresh_stale_records
        p1 = PageReading(pdf_page=4, regions=[Region(id="b", kind="본문", paragraphs=[Paragraph(columns=[Column(col_from_right=1, text="剋하니 冲和가 되고")])])])
        rec = {"pdf_page": 4, "version": "0.5.11", "pass1": p1.model_dump(),
               "pass2": {"b": RegionReread(columns=[Column(col_from_right=1, text="剋하니 沖和가 되고")]).model_dump()},
               "merged": p1.model_dump()}
        with tempfile.TemporaryDirectory() as td:
            work = Work(os.path.join(td, "a.pdf"), os.path.join(td, "w"))
            work.save_record(4, rec)
            work.textlayer_path(4).write_text(json.dumps({"plain": "剋하니冲和가되고"}), encoding="utf-8")
            self.assertEqual(refresh_stale_records(work, None, [4]), 1)
            r = work.load_record(4)
            self.assertEqual(r["version"], __version__); self.assertIn("remerged_at", r)
            self.assertIn("〔冲/沖〕", region_text(PageReading.model_validate(r["merged"]).regions[0]))
            self.assertEqual(refresh_stale_records(work, None, [4]), 0)          # 이미 지금 판이면 손대지 않음


class FlatSections(unittest.TestCase):
    """전체 교정은 급과 무관하게 바로 다음 기준 직전까지를 한 구획으로 본다(겹침·빠짐 없음)."""
    def test_flat_section_ends_at_next_anchor(self):
        from test_collate import make_book
        from yeone_ocr.collate.sections import HwpxBook
        with tempfile.TemporaryDirectory() as td:
            bk = HwpxBook(make_book(os.path.join(td, "b.hwpx"))); an = bk.anchors()
            self.assertEqual([a.level for a in an], [1, 2, 2, 2])
            deep = bk.section(an, 0); flat = bk.section(an, 0, flat=True)
            self.assertEqual(deep.end, len(bk.paragraphs))                      # 급 1 은 하위 구획을 모두 품는다
            self.assertEqual(flat.end, an[1].para); self.assertEqual(flat.next_anchor.name, "大薊")
            self.assertEqual(bk.section(an, 1, flat=True).end, bk.section(an, 1).end)   # 같은 급끼리는 차이 없음
            ends = [bk.section(an, i, flat=True) for i in range(len(an))]
            self.assertTrue(all(ends[i].end == ends[i + 1].start for i in range(len(ends) - 1)))   # 이어 붙으면 문서 전체


def _page_rec():
    reg = Region(id="r1", kind="본문", bbox=[100, 50, 300, 950], paragraphs=[Paragraph(columns=[
        Column(col_from_right=1, text="行通乎小肠，能入阴分，治有形之病"),
        Column(col_from_right=2, text="色赤入心，性下行而通"),
        Column(col_from_right=3, text="唐容川曰：物下极则反上")])])
    pr = PageReading(pdf_page=430, regions=[reg])
    return {"pdf_page": 430, "pass1": pr.model_dump(), "merged": pr.model_dump()}


class LocateItems(unittest.TestCase):
    """책갈피 OCR 대조 항목(앞뒤 글자)으로 병합 판독문의 자리를 찾는다 — 부호는 빼고 맞춘다."""
    def test_locate_in_reading(self):
        from yeone_ocr.adjudicate import locate_in_reading
        m = PageReading.model_validate(_page_rec()["merged"])
        s = locate_in_reading(m, "小肠，能", "入", "阴分，治")
        self.assertIsNotNone(s); self.assertEqual((s.col_index, s.char_index, s.a), (0, 7, "入")); self.assertEqual(s.pre, "小肠，能")
        s2 = locate_in_reading(m, "色赤", "入", "心")
        self.assertEqual((s2.col_index, s2.char_index), (1, 2))
        s3 = locate_in_reading(m, "", "曰", "：物")                              # 앞 글자 없이 뒤 글자만으로
        self.assertEqual((s3.col_index, s3.char_index), (2, 3))
        self.assertIsNone(locate_in_reading(m, "乾", "坤", "震"))


class AdjudicateItems(unittest.TestCase):
    """자형 편향·혼동으로 표시만 한 대조 항목을 글자 조각으로 묻는다: 인쇄가 HWPX 글자면 뺌, 판독 글자면 교정 후보, 못 가리면 표시 그대로."""
    def test_items_are_dropped_promoted_or_kept(self):
        from yeone_ocr.schema import GlyphChoice
        from yeone_ocr.collate.collate import Item
        from yeone_ocr.collate.run import adjudicate_items
        items = [Item("B1", "mark", 3, 5, 6, "人", "入", "", pdf_page=430, note="자형 편향 쌍(入/人) — 표시만", oc_ctx="肠，能「入」阴分，治", hw_ctx="腸，能「人」陰分，治"),
                 Item("B1", "mark", 4, 2, 3, "日", "曰", "", pdf_page=430, note="헷갈리기 쉬운 글자(曰/日) — 표시만", oc_ctx="唐容川「曰」：物下", hw_ctx="唐容川「日」：物下"),
                 Item("B1", "mark", 3, 9, 10, "人", "入", "", pdf_page=430, note="자형 편향 쌍(入/人) — 표시만", oc_ctx="色赤「入」心，性", hw_ctx="色赤「人」心，性"),
                 Item("B1", "mark", 5, 0, 1, "乾", "坤", "", pdf_page=430, note="자형 편향 쌍 — 표시만", oc_ctx="乾「坤」震"),
                 Item("A", "replace", 6, 0, 1, "幹", "于", "於", pdf_page=430, note="", oc_ctx="行「于」水")]

        class Fake:
            def __init__(self): self.calls = []
            def adjudicate(self, crop, candidates, pre, post, horizontal, pdf_page):
                self.calls.append((candidates, pre, post, crop.size, pdf_page))
                if candidates == ["人", "入"] and pre.endswith("能"):
                    return GlyphChoice(choice="人", evidence="왼쪽 삐침이 위에서 시작")      # 인쇄가 HWPX 글자 → 차이 아님
                if candidates == ["日", "曰"]:
                    return GlyphChoice(choice="曰", evidence="가로획이 오른쪽에 닿지 않음")   # 인쇄가 판독 글자 → HWPX 오탈
                return GlyphChoice(choice="", evidence="획이 뭉개짐")
        with tempfile.TemporaryDirectory() as td:
            work = Work(os.path.join(td, "a.pdf"), os.path.join(td, "w"))
            work.save_record(430, _page_rec()); Image.new("L", (900, 1300), 255).save(work.page_image_path(430))
            fake = Fake()
            st = adjudicate_items(items, work=work, doc=None, reader=fake, horizontal=True, policy="all")
        self.assertEqual(st, {"asked": 3, "hwpx": 1, "ocr": 1, "undecided": 1, "unlocated": 1})
        self.assertEqual(len(fake.calls), 3); self.assertTrue(all(c[4] == 430 and c[3][0] > 4 for c in fake.calls))
        kinds = [(it.kind, it.action, it.hwpx, it.ocr) for it in items]
        self.assertNotIn(("D", "mark", "人", "入"), kinds)                                       # 뺀 항목은 목록에서 사라진다
        self.assertEqual(len(items), 4)
        promoted = next(it for it in items if it.hwpx == "日")
        self.assertEqual((promoted.kind, promoted.action, promoted.new, promoted.auto), ("A", "replace", "曰", True))
        self.assertIn("glyph=ocr", promoted.witness); self.assertIn("오탈로 보임", promoted.note)
        kept = next(it for it in items if it.oc_ctx.startswith("色赤"))
        self.assertEqual((kept.kind, kept.action), ("B1", "mark")); self.assertIn("가리지 못함", kept.note)
        unloc = next(it for it in items if it.ocr == "坤")
        self.assertEqual((unloc.kind, unloc.note), ("B1", "자형 편향 쌍 — 표시만"))                 # 자리를 못 찾으면 손대지 않음
        self.assertTrue(any(it.kind == "A" and it.ocr == "于" for it in items))                   # 다른 종류 항목은 그대로


class Parser(unittest.TestCase):
    """한 번에 인식·전체 교정의 명령줄: 모드는 auto 가 기본, 3차 판별·자형 판별은 끌 수 있다."""
    def test_defaults_and_switches(self):
        from yeone_ocr.cli import build_parser
        ap = build_parser()
        a = ap.parse_args(["run", "x.pdf"])
        self.assertEqual((a.mode, a.no_adjudicate, a.print_offset, a.chunk_lines, a.media_resolution), ("auto", False, None, 8, "auto"))
        a = ap.parse_args(["run", "x.pdf", "--mode", "horizontal_zh", "--no-adjudicate", "--print-offset", "29"])
        self.assertEqual((a.mode, a.no_adjudicate, a.print_offset), ("horizontal_zh", True, 29))
        c = ap.parse_args(["collate", "x.pdf", "--hwpx", "b.hwpx", "--all", "--apply", "--no-glyph-check"])
        self.assertTrue(c.all and c.apply and c.no_glyph_check); self.assertEqual(c.mode, "auto")
        self.assertEqual(ap.parse_args(["remerge", "x.pdf"]).mode, "auto")
        self.assertEqual(ap.parse_args(["adjudicate", "x.pdf", "--mode", "vertical"]).mode, "vertical")

    def test_gui_mode_labels_match_cli(self):
        try:
            from yeone_ocr import gui
        except Exception as e:                       # tkinter 가 없는 환경
            self.skipTest(f"gui 를 불러오지 못함: {e}")
        from yeone_ocr.cli import build_parser
        ap = build_parser(); sp = next(a for a in ap._actions if getattr(a, "choices", None) and "run" in a.choices)
        mode = next(a for a in sp.choices["run"]._actions if a.dest == "mode")
        self.assertEqual(set(gui.MODE_LABELS), set(mode.choices))
        self.assertEqual(gui.DEFAULTS.get("mode"), "auto"); self.assertIs(gui.DEFAULTS.get("advanced_open"), False)


if __name__ == "__main__":
    unittest.main()
