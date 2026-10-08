"""0.6.0 — 한 번에 인식: 읽기 방향·인쇄 쪽수 차이 자동 판정, 이전 판 기록 갱신, 평면 구획, 대조 항목의 자형 3차 판별, 명령줄 선택지.
실행: python -m unittest discover -s tests   (저장소 루트에서)"""
import os, sys, json, tempfile, unittest
from argparse import Namespace
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import pymupdf
from PIL import Image
from yeone_ocr.schema import PageReading, Region, Paragraph, Column, RegionReread, Uncertain, region_text, bbox_frac
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


# ---------------- 0.6.1: 좌표 보정·가로쓰기 덮개 검사·묶음 응답 검사·인쇄 쪽수 판정·글자 창 검사 ----------------
def _drifted_page():
    """글줄 10개짜리 가로쓰기 쪽. 모델 좌표는 아래로 갈수록 밀렸다(실측 130쪽 모양). 참 좌표와 함께 돌려준다."""
    from PIL import ImageDraw
    W, H = 1000, 1400
    im = Image.new("L", (W, H), 255); d = ImageDraw.Draw(im)
    tops = [0.10 + i * 0.05 for i in range(10)]                  # 줄 간격 5%, 줄 높이 2.4%
    for t in tops:
        d.rectangle([int(0.1 * W), int(t * H), int(0.9 * W), int((t + 0.024) * H)], fill=0)
    d.rectangle([int(0.45 * W), int(0.03 * H), int(0.55 * W), int(0.05 * H)], fill=0)     # 머리글(어느 영역에도 없음)
    truth = {"r1": (0.10, 0.174), "r2": (0.20, 0.324), "r3": (0.35, 0.474), "r4": (0.50, 0.574)}    # 줄 2·3·3·2
    regs = []
    for rid, n, drift in (("r1", 2, 0.0), ("r2", 3, 0.02), ("r3", 3, 0.045), ("r4", 2, 0.07)):
        y0, y1 = truth[rid]
        regs.append(Region(id=rid, kind="본문", bbox=[int((y0 + drift) * 1000), 100, int((y1 + drift) * 1000), 900],
                           paragraphs=[Paragraph(columns=[Column(col_from_right=k + 1, text=f"{rid}줄{k + 1}의글자들입니다") for k in range(n)])]))
    layer = {"page_width": 500.0, "page_height": 700.0, "horizontal_lines": [], "vertical_columns": []}
    k = 0
    for rid, n, _ in (("r1", 2, 0), ("r2", 3, 0), ("r3", 3, 0), ("r4", 2, 0)):
        for j in range(n):
            t = tops[k]; k += 1
            layer["horizontal_lines"].append({"bbox": [50.0, t * 700, 450.0, (t + 0.024) * 700], "text": f"{rid}줄{j + 1}의글자들입니다"})
    layer["horizontal_lines"].insert(0, {"bbox": [225.0, 0.03 * 700, 275.0, 0.05 * 700], "text": "114"})
    return im, PageReading(pdf_page=130, regions=regs), truth, layer


class Refit(unittest.TestCase):
    """모델 영역 좌표가 아래로 밀렸을 때 글자층 줄·먹 띠로 되돌린다(실측: 130쪽 맨 아래 영역 7.8% 밀림 → 2차 조각이 이웃 인용문을 읽음)."""
    def _check(self, p1, truth, tol=0.008):
        for r in p1.regions:
            y0, y1 = truth[r.id]
            self.assertAlmostEqual(r.bbox[0] / 1000, y0, delta=tol, msg=r.id); self.assertAlmostEqual(r.bbox[2] / 1000, y1, delta=tol, msg=r.id)

    def test_layer_refit(self):
        from yeone_ocr.refit import refit_by_layer
        im, p1, truth, layer = _drifted_page()
        fixes = refit_by_layer(p1, layer, horizontal=True)
        self.assertEqual(set(fixes), {"r1", "r2", "r3", "r4"}); self._check(p1, truth)
        self.assertGreater(fixes["r4"]["shift"], 0.06); self.assertEqual(fixes["r4"]["lines"], [9, 10])

    def test_ink_refit_aligns_runs_and_skips_header(self):
        from yeone_ocr.refit import refit_by_ink, align_runs
        im, p1, truth, _ = _drifted_page()
        fixes = refit_by_ink(im, p1, horizontal=True)
        self.assertEqual(set(fixes), {"r1", "r2", "r3", "r4"}); self._check(p1, truth)
        self.assertEqual(align_runs([0.15, 0.4], [1, 2], [0.05, 0.15, 0.35, 0.45]), [1, 2])      # 머리글 띠는 건너뛴다
        self.assertIsNone(align_runs([0.5], [3], [0.1, 0.2]))

    def test_ink_refit_refuses_when_unsure(self):
        from yeone_ocr.refit import refit_by_ink
        im, p1, truth, _ = _drifted_page()
        p1.regions[1].paragraphs[0].columns.append(Column(col_from_right=4, text="줄 수가 틀린 영역"))      # 1차가 줄 수를 잘못 세면
        self.assertEqual(refit_by_ink(im, p1, horizontal=True), {})

    def test_refit_regions_prefers_layer_and_describes(self):
        from yeone_ocr.refit import refit_regions, describe
        im, p1, truth, layer = _drifted_page()
        fix = refit_regions(im, p1, True, layer)
        self.assertEqual((fix["fixed"], fix["how"]), (4, "layer")); self.assertGreater(fix["max_shift"], 0.06); self.assertIn("글자층", describe(fix))
        im2, p2, _, _ = _drifted_page()
        self.assertEqual(refit_regions(im2, p2, True, None)["how"], "ink")

    def test_title_box_does_not_count_as_drift(self):
        from yeone_ocr.refit import refit_by_layer
        reg = Region(id="t", kind="제목", bbox=[300, 400, 320, 600], title_text="石韋")
        layer = {"page_width": 500.0, "page_height": 700.0, "vertical_columns": [],
                 "horizontal_lines": [{"bbox": [200.0, 196.0, 300.0, 245.0], "text": "石韋"}, {"bbox": [50.0, 300.0, 450.0, 310.0], "text": "本文"}]}
        p1 = PageReading(pdf_page=1, regions=[reg])
        fixes = refit_by_layer(p1, layer, True)
        self.assertEqual(fixes["t"]["shift"], 0.0); self.assertGreater(fixes["t"]["move"], 0.01)


class HorizontalCover(unittest.TestCase):
    """가로쓰기에서 영역 밖 머리글 한 줄은 항목 하나로(글자 수만큼의 '기둥' 20건이 아니라)."""
    def test_uncovered_lines_axis_y(self):
        from yeone_ocr.layout import uncovered_ink_boxes
        from yeone_ocr.cli import cover_check
        im, p1, truth, _ = _drifted_page()
        for r in p1.regions:                                   # 참 좌표로 놓고 머리글만 남긴다
            y0, y1 = truth[r.id]; r.bbox = [int(y0 * 1000), 100, int(y1 * 1000), 900]
        cols = uncovered_ink_boxes(im, [bbox_frac(r.bbox) for r in p1.regions], axis="x")
        rows = uncovered_ink_boxes(im, [bbox_frac(r.bbox) for r in p1.regions], axis="y")
        self.assertEqual(len(rows), 1); self.assertLess(rows[0][3], 0.06)                 # 세로 기둥 검사(axis x)는 납작한 머리글을 아예 못 보거나 여러 조각으로 쪼갠다
        notes = cover_check(im, p1, horizontal=True)
        self.assertEqual(len(notes), 1); self.assertIn("글줄 y", notes[0])
        self.assertEqual(len(p1.uncertain), 1); self.assertIn("가로 글줄", p1.uncertain[0].context); self.assertIn("머리글", p1.uncertain[0].context)

    def test_edge_lines_absorbed_from_layer(self):
        """머리글 '114 中医历代药论选' 은 확인 항목이 아니라 쪽번호·머리말로(책 전체에서 쪽마다 2건씩 쌓이던 잡음)."""
        from yeone_ocr.cli import cover_check, absorb_edge_lines
        im, p1, truth, layer = _drifted_page()
        for r in p1.regions:
            y0, y1 = truth[r.id]; r.bbox = [int(y0 * 1000), 100, int(y1 * 1000), 900]
        cover_check(im, p1, horizontal=True)
        layer["horizontal_lines"][0]["text"] = "114"
        layer["horizontal_lines"].insert(1, {"bbox": [300.0, 0.03 * 700, 400.0, 0.05 * 700], "text": "中医历代药论选"})
        notes = absorb_edge_lines(p1, layer)
        self.assertEqual(p1.uncertain, []); self.assertEqual((p1.print_page, p1.running_header), ("114", "中医历代药论选")); self.assertIn("머리글·쪽번호로 봄", notes[0])
        # 글자층에 그 자리 글이 없으면(선·그림) 항목은 남긴다
        im2, p2, _, _ = _drifted_page()
        for r in p2.regions:
            y0, y1 = truth[r.id]; r.bbox = [int(y0 * 1000), 100, int(y1 * 1000), 900]
        cover_check(im2, p2, horizontal=True)
        self.assertEqual(absorb_edge_lines(p2, {"page_height": 700.0, "horizontal_lines": [{"bbox": [50.0, 400.0, 450.0, 410.0], "text": "본문"}]}), [])
        self.assertEqual(len(p2.uncertain), 1)


class BatchGuard(unittest.TestCase):
    """묶음 2차 응답이 다른 영역의 글이면(모델이 조각 번호를 섞음) 버리고 낱개로 다시 읽는다. 조각은 --batch-crops 장씩 나눠 보낸다."""
    def _p1(self):
        return PageReading(pdf_page=1, regions=[
            Region(id="r3", kind="본문", bbox=[279, 77, 333, 889], paragraphs=[Paragraph(columns=[Column(col_from_right=1, text="3.《本草汇言》：艾叶暖血温经，行气开郁之药也。治妇人经血不调")])]),
            Region(id="r4", kind="본문", bbox=[344, 77, 378, 889], paragraphs=[Paragraph(columns=[Column(col_from_right=1, text="4.《本草经疏》：艾叶禀天地之阳气以生，故味苦微温，熟则大热")])]),
            Region(id="r5", kind="본문", bbox=[389, 77, 423, 889], paragraphs=[Paragraph(columns=[Column(col_from_right=1, text="5.《本经逢源》：艾性纯阳，故可以取太阳真火，可以回垂绝元阳")])])])

    def test_misassigned_detected(self):
        from yeone_ocr.cli import _misassigned
        p1 = self._p1()
        rr = {"r3": RegionReread(columns=[Column(col_from_right=1, text="4.《本草经疏》:艾叶禀天地之阳气以生,故味苦微温,熟则大热")]),
              "r4": RegionReread(columns=[Column(col_from_right=1, text="5.《本经逢源》:艾性纯阳,故可以取太阳真火,可以回垂绝元阳")]),
              "r5#1": RegionReread(columns=[Column(col_from_right=1, text="5.《本经逢源》:艾性纯阳,故可以取太阳真火")])}
        self.assertEqual(_misassigned(rr, p1), ["r3", "r4"])
        ok = {"r3": RegionReread(columns=[Column(col_from_right=1, text="3.《本草汇言》:艾叶暖血温经,行气开郁之药也。治妇人经血不调")])}
        self.assertEqual(_misassigned(ok, p1), [])

    def test_batches_are_split(self):
        from yeone_ocr.cli import _second_pass
        from PIL import ImageDraw
        im = Image.new("L", (1000, 1400), 255); d = ImageDraw.Draw(im)
        for y in (0.29, 0.355, 0.40):
            d.rectangle([100, int(y * 1400), 880, int((y + 0.022) * 1400)], fill=0)
        p1 = self._p1()
        class Fake:
            def __init__(self): self.batches = []; self.single = []
            def reread_regions(self, items, n):
                self.batches.append([rid for rid, _, _ in items])
                return {rid: RegionReread(columns=[Column(col_from_right=1, text=next(c.text for r in p1.regions if r.id == rid for p in r.paragraphs for c in p.columns))]) for rid, _, _ in items}
            def reread_region(self, im, kind, rid, n):
                self.single.append(rid); return RegionReread(columns=[])
        from yeone_ocr import prompts
        prompts.set_mode("horizontal_zh")
        try:
            fake = Fake()
            rr, meta = _second_pass(fake, im, p1, 1, Namespace(chunk_lines=8, second_mode="batch", batch_crops=2))
        finally:
            prompts.set_mode("vertical")
        self.assertEqual([len(b) for b in fake.batches], [2])              # 조각 3개 → 2장 묶음 하나 + 남은 하나는 낱개
        self.assertEqual(fake.single, ["r5"]); self.assertEqual(set(rr), {"r3", "r4"})


class PrintOffset(unittest.TestCase):
    """인쇄 쪽수 차이: 글자층의 머리글 숫자 → 판독한 쪽번호 순으로 판정하고 어긋난 쪽을 표시한다."""
    def test_header_digits_in_text_layer(self):
        from yeone_ocr.cli import detect_print_offset
        doc = pymupdf.open()
        for i in range(12):
            pg = doc.new_page(width=500, height=800)
            pg.insert_text((60, 50), f"{i + 114}", fontname="helv", fontsize=10)                  # 머리글 자리의 숫자만인 줄
            pg.insert_text((100, 50), "中医历代药论选", fontname="china-s", fontsize=10)
            for k in range(6):
                pg.insert_text((60, 100 + k * 24), "车前子味甘寒无毒主气癃止痛利水道", fontname="china-s", fontsize=12)
        self.assertEqual(detect_print_offset(doc), 113)

    def test_offset_from_records_and_flag(self):
        from yeone_ocr.cli import offset_from_records, flag_print_mismatch
        def rec(pg, pp): return {"pdf_page": pg, "merged": {"print_page": pp, "uncertain": []}}
        self.assertEqual(offset_from_records([rec(130, "114"), rec(131, "115"), rec(132, "")]), -16)
        self.assertIsNone(offset_from_records([rec(130, "114")]))                                 # 한 쪽만으로는 정하지 않는다
        self.assertIsNone(offset_from_records([rec(130, "114"), rec(131, "120")]))
        with tempfile.TemporaryDirectory() as td:
            work = Work(os.path.join(td, "a.pdf"), os.path.join(td, "w"))
            recs = [rec(130, "114"), rec(131, "115"), rec(132, "117")]
            for r in recs: work.save_record(r["pdf_page"], r)
            self.assertEqual(flag_print_mismatch(work, recs, -16), 1)
            ctx = work.load_record(132)["merged"]["uncertain"][0]["context"]
            self.assertIn("117", ctx); self.assertIn("116", ctx)
            self.assertEqual(flag_print_mismatch(work, work.records(), -16), 0)                   # 두 번 붙이지 않는다


class GlyphWindow(unittest.TestCase):
    """3차 판별에서 모델이 본 글(w)이 기대한 앞뒤 글자와 안 이어지면 조각 위치가 어긋난 것 — 확정하지 않는다."""
    def test_window_ok(self):
        from yeone_ocr.adjudicate import window_ok
        self.assertTrue(window_ok("", "肠，能", "人", "阴分"))
        self.assertTrue(window_ok("，能人阴分", "肠，能", "人", "阴分"))
        self.assertTrue(window_ok("能入阴", "肠，能", "人", "阴分"))             # 앞뒤가 보이면 가운데를 달리 읽어도 자리는 맞다
        self.assertFalse(window_ok("7.《本草便读》", "", "6", ".《本草分经"))       # 실측: 이웃 인용문을 잘라 '7' 로 확정한 사례
        self.assertFalse(window_ok("血，逐寒湿", "肠，能", "人", "阴分"))

    def test_adjudicate_page_rejects_misplaced_crop(self):
        from yeone_ocr.adjudicate import adjudicate_page
        from yeone_ocr.schema import GlyphChoice
        reg = Region(id="r6", kind="본문", bbox=[434, 77, 468, 889], paragraphs=[Paragraph(columns=[Column(col_from_right=1, text="〔6/7〕.《本草分经》：苦辛，生温熟热")])])
        rec = {"pdf_page": 130, "pass1": PageReading(pdf_page=130, regions=[reg]).model_dump(), "merged": PageReading(pdf_page=130, regions=[reg]).model_dump()}
        class Fake:
            def adjudicate(self, crop, candidates, pre, post, horizontal, pdf_page):
                return GlyphChoice(choice="7", evidence="7의 형태", window="血，逐寒湿 7.《本草便读")
        rec = adjudicate_page(rec, Image.new("L", (900, 1300), 255), Fake(), horizontal=True)
        self.assertEqual(rec["adjudicate_stats"]["decided"], 0); self.assertEqual(rec["adjudicate_stats"].get("misplaced"), 1)
        self.assertIn("〔6/7〕", region_text(PageReading.model_validate(rec["merged"]).regions[0]))
        self.assertIn("조각 위치가 어긋남", rec["adjudications"][0]["evidence"])


class StaleRecords(unittest.TestCase):
    """이전 판 기록: 좌표가 크게 밀린 기록은 stale/ 로 옮겨 다시 판독하고, 조금 밀린 기록은 좌표만 고쳐 다시 비교한다."""
    def test_refresh_moves_drifted_records(self):
        from yeone_ocr import __version__
        from yeone_ocr.cli import refresh_stale_records, apply_mode
        im, p1, truth, layer = _drifted_page()
        with tempfile.TemporaryDirectory() as td:
            work = Work(os.path.join(td, "a.pdf"), os.path.join(td, "w"))
            layer["plain"] = "".join(l["text"] for l in layer["horizontal_lines"])
            work.textlayer_path(130).write_text(json.dumps(layer), encoding="utf-8")
            rec = {"pdf_page": 130, "version": "0.6.0", "mode": "horizontal_zh", "pass1": p1.model_dump(), "merged": p1.model_dump(),
                   "pass2": {"r1": RegionReread(columns=[Column(col_from_right=1, text="x")]).model_dump()}}
            work.save_record(130, rec)
            im2, p1b, truth2, layer2 = _drifted_page()
            for r in p1b.regions:                                            # 131쪽은 거의 안 밀린 기록
                y0, y1 = truth2[r.id]; r.bbox = [int((y0 + 0.004) * 1000), 100, int((y1 + 0.004) * 1000), 900]
            layer2["plain"] = layer["plain"]; work.textlayer_path(131).write_text(json.dumps(layer2), encoding="utf-8")
            work.save_record(131, {**rec, "pdf_page": 131, "pass1": p1b.model_dump(), "merged": p1b.model_dump()})
            apply_mode(Namespace(mode="horizontal_zh"))
            try:
                n = refresh_stale_records(work, None, [130, 131])
            finally:
                apply_mode(Namespace(mode="vertical"))
            self.assertEqual(n, 1); self.assertIsNone(work.load_record(130)); self.assertTrue((work.readings_dir / "stale" / "p130.json").exists())
            r131 = work.load_record(131); self.assertEqual(r131["version"], __version__); self.assertEqual(r131["bbox_fix"]["fixed"], 4)
