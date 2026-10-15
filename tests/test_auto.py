"""0.6.0 — 한 번에 인식: 읽기 방향·인쇄 쪽수 차이 자동 판정, 이전 판 기록 갱신, 평면 구획, 대조 항목의 자형 3차 판별, 명령줄 선택지.
실행: python -m unittest discover -s tests   (저장소 루트에서)"""
import contextlib, io, os, sys, json, tempfile, unittest
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
            """앞뒤 글자는 받지 못한다(눈가림) — 묻는 차례로 답한다: 1번(能「入」) 人, 2번(曰/日) 曰, 3번(色赤「入」) 못 가림."""
            def __init__(self): self.calls = []
            def adjudicate(self, crop, ask, horizontal, pdf_page):
                self.calls.append((ask, None, None, crop.size, pdf_page))
                if len(self.calls) == 1:
                    return GlyphChoice(choice="人", evidence="왼쪽 삐침이 위에서 시작")      # 인쇄가 HWPX 글자 → 차이 아님
                if set(ask.candidates) == {"日", "曰"}:
                    return GlyphChoice(choice="曰", evidence="가로획이 오른쪽에 닿지 않음")   # 인쇄가 판독 글자 → HWPX 오탈
                return GlyphChoice(choice="", evidence="획이 뭉개짐")
        with tempfile.TemporaryDirectory() as td:
            work = Work(os.path.join(td, "a.pdf"), os.path.join(td, "w"))
            work.save_record(430, _page_rec()); Image.new("L", (900, 1300), 255).save(work.page_image_path(430))
            fake = Fake()
            st = adjudicate_items(items, work=work, doc=None, reader=fake, horizontal=True, policy="all")
        self.assertEqual(st, {"asked": 3, "hwpx": 1, "ocr": 1, "undecided": 1, "unlocated": 1, "lowres": 0, "blind_rejected": 0})
        self.assertEqual([set(c[0].candidates) for c in fake.calls], [{"人", "入"}, {"日", "曰"}, {"人", "入"}])
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

    def test_blind_recheck_and_low_resolution(self):
        """눈가림 재확인(blind): 후보 없이 읽은 답이 객관식 답과 다르면 따르지 않는다. 글자가 문턱보다 작으면 묻지 않는다."""
        from yeone_ocr.schema import GlyphChoice
        from yeone_ocr.collate.collate import Item
        from yeone_ocr.collate.run import adjudicate_items
        def items():
            return [Item("B1", "mark", 4, 2, 3, "日", "曰", "", pdf_page=430, note="헷갈리기 쉬운 글자(曰/日) — 표시만", oc_ctx="唐容川「曰」：物下", hw_ctx="唐容川「日」：物下")]
        class Fake:
            def __init__(self, seen): self.seen = seen; self.open = []
            def adjudicate(self, crop, ask, h, p): return GlyphChoice(choice="曰", evidence="가로획이 닿지 않음")
            def identify(self, crop, ask, h, p): self.open.append(ask); return GlyphChoice(choice=self.seen)
        with tempfile.TemporaryDirectory() as td:
            work = Work(os.path.join(td, "a.pdf"), os.path.join(td, "w"))
            work.save_record(430, _page_rec()); Image.new("L", (900, 1300), 255).save(work.page_image_path(430))
            its = items(); f = Fake("日")
            st = adjudicate_items(its, work=work, doc=None, reader=f, horizontal=True, blind=True)
            self.assertEqual((st["ocr"], st["undecided"], st["blind_rejected"]), (0, 1, 1)); self.assertEqual(f.open[0].candidates, ())
            self.assertEqual(its[0].kind, "B1"); self.assertIn("눈가림 답 「日」", its[0].note)
            its = items(); st = adjudicate_items(its, work=work, doc=None, reader=Fake("曰"), horizontal=True, blind=True)
            self.assertEqual((st["ocr"], its[0].kind), (1, "A"))
            its = items(); st = adjudicate_items(its, work=work, doc=None, reader=Fake("曰"), horizontal=True, min_px=10_000)
            self.assertEqual((st["asked"], st["lowres"], its[0].kind), (0, 1, "B1")); self.assertIn("저해상", its[0].note)

    def test_blind_options_reach_run_adjudicate_collate_only(self):
        from yeone_ocr import cli
        from yeone_ocr.adjudicate import MIN_GLYPH_PX
        p = cli.build_parser()
        for argv in (["run", "x.pdf"], ["adjudicate", "x.pdf"], ["collate", "x.pdf", "--hwpx", "a.hwpx"]):
            a = p.parse_args(argv + ["--adjudicate-blind", "--adjudicate-min-px", "12"])
            self.assertEqual((a.adjudicate_blind, a.adjudicate_min_px), (True, 12.0))
            self.assertEqual(p.parse_args(argv).adjudicate_min_px, MIN_GLYPH_PX)
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            p.parse_args(["read", "x.pdf", "--adjudicate-blind"])                                # read 에는 3차가 없다(창 프로그램도 run·adjudicate·collate 에만 붙인다)


class Parser(unittest.TestCase):
    """한 번에 인식·전체 교정의 명령줄: 모드는 auto 가 기본, 3차 판별·자형 판별은 끌 수 있다."""
    def test_skip_answered_values_and_provider_guess(self):
        """--skip-answered: adjudicate·build·run 에 하나씩(run 은 겹치지 않게), 기본 any-model. 옛 호출(값 없음)도 any-model. collate 에는 없다."""
        from yeone_ocr import cli, estimate
        from yeone_ocr.providers import provider_of_model
        ap = cli.build_parser()
        for cmd in ("adjudicate", "build", "run"):
            self.assertEqual(ap.parse_args([cmd, "x.pdf"]).skip_answered, "any-model", cmd)
            a = ap.parse_args([cmd, "x.pdf", "--skip-answered", "same-model"])
            self.assertEqual(a.skip_answered, "same-model"); self.assertFalse(cli.skip_any(a))
        self.assertTrue(cli.skip_any(Namespace())); self.assertTrue(cli.skip_any(ap.parse_args(["run", "x.pdf"])))
        self.assertFalse(hasattr(ap.parse_args(["collate", "x.pdf", "--hwpx", "b.hwpx"]), "skip_answered"))
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            ap.parse_args(["run", "x.pdf", "--skip-answered", "never"])
        self.assertEqual([provider_of_model(m) for m in ("gemini-2.5-pro", "models/gemini-3-flash", "claude-sonnet-4-6", "gpt-4.1 (3차)", "o3-mini",
                                                         "chatgpt-4o", "mock", "", None)],
                         ["gemini", "gemini", "anthropic", "openai", "openai", "openai", "", "", ""])
        self.assertIs(estimate._model_provider, provider_of_model)                       # 비용 어림과 같은 짐작

    def test_defaults_and_switches(self):
        from yeone_ocr.cli import build_parser
        ap = build_parser()
        a = ap.parse_args(["run", "x.pdf"])
        self.assertEqual((a.mode, a.no_adjudicate, a.print_offset, a.chunk_lines, a.media_resolution), ("auto", False, None, 8, "auto"))
        self.assertEqual((a.adjudicate_model, a.adjudicate_thinking, a.batch_sites, a.workers, a.workers_start), ("", "low", 8, 8, 4))   # 0.6.2 3차 판별 옵션 · 0.6.8 동시 8
        self.assertEqual(ap.parse_args(["collate", "x.pdf", "--hwpx", "b.hwpx"]).policy, "sure")                                        # 0.6.8 책갈피 교정은 확실한 것(A)만 자동
        j = ap.parse_args(["adjudicate", "x.pdf", "--adjudicate-model", "auto", "--batch-sites", "4", "--workers", "2"])
        self.assertEqual((j.adjudicate_model, j.batch_sites, j.workers, j.workers_start, j.adjudicate_thinking), ("auto", 4, 2, 4, "low"))
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

    def _repeated_page(self, second_same=False, last_same=False):
        """글줄 9개: 약재 셋이 각각 3줄, 모두 「性味甘平」 로 시작. 1차는 가운데 약재(줄 3~5)를 빠뜨리고 r1(줄 0~2)·r3(줄 6~8)만 냈다.
        줄 간격 4%. r3 모델 좌표는 제자리(줄 6) — 빠진 영역의 첫 줄(줄 3)도 MAX_MOVE 안이라 첫 줄 짝 후보가 둘이다."""
        tops = [0.10 + i * 0.04 for i in range(9)]
        texts = ["性味甘平無毒", "r1둘째줄의글자들", "r1끝줄의글자들",
                 "性味甘平無毒", "性味甘平無毒" if second_same else "빠진둘째줄의글자들", "r3끝줄의글자들" if last_same else "빠진끝줄의글자들",
                 "性味甘平無毒", "性味甘平無毒" if second_same else "r3둘째줄의글자들", "r3끝줄의글자들"]
        layer = {"page_width": 500.0, "page_height": 700.0, "vertical_columns": [],
                 "horizontal_lines": [{"bbox": [50.0, t * 700, 450.0, (t + 0.02) * 700], "text": x} for t, x in zip(tops, texts)]}
        regs = []
        for rid, k in (("r1", 0), ("r3", 6)):
            regs.append(Region(id=rid, kind="본문", bbox=[int(tops[k] * 1000), 100, int((tops[k + 2] + 0.02) * 1000), 900],
                               paragraphs=[Paragraph(columns=[Column(col_from_right=j + 1, text=texts[k + j]) for j in range(3)])]))
        return PageReading(pdf_page=7, regions=regs), layer, tops

    def test_repeated_first_line_does_not_swallow_missing_region(self):
        """첫 줄이 되풀이되고 1차가 가운데 영역을 빠뜨려도 r3 의 상자가 빠진 영역의 줄(3~5)까지 넓어지지 않는다
        (이전엔 첫 줄을 줄 3에 붙이고 끝 줄을 줄 8에서 찾아 6줄짜리 상자가 됐다 → 2차가 빠진 글을 읽어 '2차에만 있음')."""
        from yeone_ocr.refit import refit_by_layer, span_slack
        for kw in ({}, {"second_same": True}, {"second_same": True, "last_same": True}):
            p1, layer, tops = self._repeated_page(**kw)
            fixes = refit_by_layer(p1, layer, True)
            self.assertEqual(fixes["r1"]["lines"], [0, 2], kw); self.assertEqual(fixes["r3"]["lines"], [6, 8], kw)
            r3 = p1.regions[1]
            self.assertAlmostEqual(r3.bbox[0] / 1000, tops[6], delta=0.008, msg=kw); self.assertAlmostEqual(r3.bbox[2] / 1000, tops[8] + 0.02, delta=0.008, msg=kw)
        self.assertEqual([span_slack(n) for n in (1, 2, 3, 4, 5, 9)], [1, 1, 1, 2, 2, 2])

    def test_unfit_span_leaves_region_unfixed_with_reason(self):
        """첫·끝 줄로 맞춘 글자층 줄 수가 영역 줄 수와 크게 다르고 다른 짝도 없으면 좌표를 손대지 않고 사유만 남긴다. refit_regions 는 이를 세지 않는다."""
        from yeone_ocr.refit import refit_by_layer, refit_regions
        tops = [0.10 + i * 0.04 for i in range(5)]
        texts = ["甲乙丙丁戊己", "아무줄하나", "아무줄둘", "아무줄셋", "庚辛壬癸子丑"]
        layer = {"page_width": 500.0, "page_height": 700.0, "vertical_columns": [],
                 "horizontal_lines": [{"bbox": [50.0, t * 700, 450.0, (t + 0.02) * 700], "text": x} for t, x in zip(tops, texts)]}
        reg = Region(id="r", kind="본문", bbox=[110, 100, 190, 900], paragraphs=[Paragraph(columns=[Column(col_from_right=1, text=texts[0]), Column(col_from_right=2, text=texts[4])])])
        p1 = PageReading(pdf_page=1, regions=[reg]); before = list(reg.bbox)
        fixes = refit_by_layer(p1, layer, True)
        self.assertEqual(reg.bbox, before); self.assertFalse(fixes["r"]["fixed"]); self.assertIn("줄 수(5)", fixes["r"]["why"]); self.assertIn("영역 줄 수(2)", fixes["r"]["why"])
        fix = refit_regions(None, PageReading(pdf_page=1, regions=[reg.model_copy(deep=True)]), True, layer)
        self.assertEqual((fix["fixed"], fix["how"], fix["max_move"]), (0, "", 0.0)); self.assertFalse(fix["regions"]["r"]["fixed"])
    @staticmethod
    def _hlayer(texts, tops, h=0.02):
        return {"page_width": 500.0, "page_height": 700.0, "vertical_columns": [],
                "horizontal_lines": [{"bbox": [50.0, t * 700, 450.0, (t + h) * 700], "text": x} for t, x in zip(tops, texts)]}

    @staticmethod
    def _hreg(rid, texts, y0, y1, kind="본문"):
        if kind == "제목":
            return Region(id=rid, kind=kind, bbox=[round(y0 * 1000), 100, round(y1 * 1000), 900], title_text=texts[0])
        return Region(id=rid, kind=kind, bbox=[round(y0 * 1000), 100, round(y1 * 1000), 900],
                      paragraphs=[Paragraph(columns=[Column(col_from_right=k + 1, text=x) for k, x in enumerate(texts)])])

    def test_repeated_one_line_regions_follow_the_drift(self):
        """제목 「【性味】」·본문 한 줄씩인 약재가 이어지고 모델 좌표가 아래로 갈수록 밀리면(줄 간격 4%, 줄마다 0.5%) 밀림이 줄 간격의 절반을
        넘는 곳부터 제목 상자가 다음 약재의 제목 줄에 붙고 그 본문은 못 맞추던 문제(모델 좌표 그대로에 가까운 줄을 골랐다)."""
        from yeone_ocr.refit import refit_by_layer
        tops = [0.10 + i * 0.04 for i in range(16)]
        texts = [x for e in range(8) for x in ("【性味】", "甘平無毒主治" + "甲乙丙丁戊己庚辛"[e] * 3)]
        regs = [self._hreg(("h", "b")[k % 2] + str(k // 2), [texts[k]], tops[k] + 0.005 * k, tops[k] + 0.02 + 0.005 * k, ("제목", "본문")[k % 2]) for k in range(16)]
        fixes = refit_by_layer(PageReading(pdf_page=1, regions=regs), self._hlayer(texts, tops), True)
        self.assertEqual({r: f.get("lines") for r, f in fixes.items()}, {r.id: [k, k] for k, r in enumerate(regs)})

    def test_one_line_region_leaves_the_next_regions_first_line(self):
        """한 줄 영역(「性味甘平無毒」)의 상자가 제 줄과 다음 영역의 같은 첫 줄 사이에서 다음 줄 쪽에 가까워도 다음 영역의 첫 줄을 가져가지 않는다
        (가져가면 다음 영역은 맞출 첫 줄이 없어 손대지 못한다 — 영역을 하나씩 맞추지 않고 쪽 전체로 맞춘다)."""
        from yeone_ocr.refit import refit_by_layer
        tops = [0.10 + i * 0.04 for i in range(5)]
        texts = ["甲乙丙丁戊己庚", "性味甘平無毒", "性味甘平無毒", "主治風寒濕痺", "久服輕身延年"]
        d = 0.025                                                                                   # 모델 좌표가 줄 간격의 0.6 만큼 아래로
        regs = [self._hreg("r0", texts[:1], tops[0], tops[0] + 0.02), self._hreg("x", texts[1:2], tops[1] + d, tops[1] + 0.02 + d),
                self._hreg("y", texts[2:], tops[2] + d, tops[4] + 0.02 + d)]
        fixes = refit_by_layer(PageReading(pdf_page=1, regions=regs), self._hlayer(texts, tops), True)
        self.assertEqual({r: f.get("lines") for r, f in fixes.items()}, {"r0": [0, 0], "x": [1, 1], "y": [2, 4]})

    def test_vertical_regions_are_placed_by_their_first_column(self):
        """세로쓰기 영역의 첫 열은 오른쪽 끝 열이다. 영역 왼쪽 가장자리(마지막 열)로 재면 첫 두 열이 같은 약재 셋이 한 약재씩 밀리고(밀림 없이도),
        5열 넘는 영역은 첫 열이 MAX_MOVE 밖으로 나가 아예 맞추지 못했다."""
        from yeone_ocr.refit import refit_by_layer
        W, H, pitch = 1000.0, 1400.0, 40.0
        xs = [900 - i * pitch for i in range(9)]
        txt = lambda e, j: ("性味甘平無毒主治", "久服輕身延年不老")[j] if j < 2 else "甲乙丙丁戊己庚辛"[e] * 6
        layer = {"page_width": W, "page_height": H, "horizontal_lines": [],
                 "vertical_columns": [{"x": xs[3 * e + j], "y0": 100.0, "y1": 1200.0, "text": txt(e, j)} for e in range(3) for j in range(3)]}
        def reg(rid, cols, texts):
            return Region(id=rid, kind="본문", bbox=[71, round((xs[cols[-1]] - pitch / 2) / W * 1000), 857, round((xs[cols[0]] + pitch / 2) / W * 1000)],
                          paragraphs=[Paragraph(columns=[Column(col_from_right=k + 1, text=t) for k, t in enumerate(texts)])])
        p1 = PageReading(pdf_page=1, regions=[reg(f"r{e}", [3 * e, 3 * e + 2], [txt(e, j) for j in range(3)]) for e in range(3)])
        fixes = refit_by_layer(p1, layer, False)
        self.assertEqual({r: f.get("lines") for r, f in fixes.items()}, {"r0": [0, 2], "r1": [3, 5], "r2": [6, 8]})
        layer8 = {**layer, "vertical_columns": [{**c, "text": "天地玄黃宇宙洪荒"[k] * 6} for k, c in enumerate(layer["vertical_columns"][:8])]}
        fixes = refit_by_layer(PageReading(pdf_page=1, regions=[reg("w", [0, 7], [c["text"] for c in layer8["vertical_columns"]])]), layer8, False)
        self.assertEqual(fixes["w"].get("lines"), [0, 7])                                          # 8열 영역

    def test_omitted_long_entry_is_not_swallowed_by_a_guessed_last_line(self):
        """첫 두 줄이 되풀이되는 약재 셋 가운데 4줄짜리를 1차가 빠뜨렸고 r3 의 모델 상자가 빠진 약재 쪽에 있으면, 끝 줄을 못 찾아 줄 수로 짐작한 짝
        (늘 줄 수가 맞아 검사를 통과)이 r3 을 빠진 약재의 줄에 붙이던 문제 — 끝 줄을 글로 찾은 짝을 앞세운다."""
        from yeone_ocr.refit import refit_by_layer
        tops = [0.10 + i * 0.03 for i in range(10)]
        texts = ["性味甘平無毒", "主治風寒濕痺", "天地玄黃宇宙洪荒", "性味甘平無毒", "主治風寒濕痺", "甲乙丙丁戊己庚", "子丑寅卯辰巳午未",
                 "性味甘平無毒", "主治風寒濕痺", "日月盈昃辰宿列張"]
        for top in (3, 5, 7):
            regs = [self._hreg("r1", texts[:3], tops[0], tops[2] + 0.02), self._hreg("r3", texts[7:], tops[top], tops[9] + 0.02)]
            fixes = refit_by_layer(PageReading(pdf_page=1, regions=regs), self._hlayer(texts, tops), True)
            self.assertEqual((fixes["r1"]["lines"], fixes["r3"]["lines"]), ([0, 2], [7, 9]), top)

    def test_random_pages_never_cover_another_regions_lines(self):
        """무작위 쪽(가로·세로, 줄 간격 1.8~5%, 쪽 맨 아래 밀림 ±8%, 상자 가장자리 잡음, 첫·둘째·끝 줄이 되풀이되는 약재, 한 줄 제목, 머리글 줄,
        1차 판독 잡음): 맞춘 영역의 줄에 다른 영역(1차가 빠뜨린 영역·머리글 포함)의 줄이 들지 않고, 빠뜨린 영역이 없으면 모든 영역이 제 줄에 맞는다."""
        import random
        from yeone_ocr.refit import refit_by_layer
        from yeone_ocr.schema import frac_bbox
        pool = [chr(c) for c in range(0x4E00, 0x4E00 + 3000)]
        for seed in range(160):
            rng = random.Random(seed); horizontal = seed % 2 == 0; omit = seed % 4 >= 2
            rnd = lambda a, b: "".join(rng.choice(pool) for _ in range(rng.randint(a, b)))
            sp = rng.uniform(0.018, 0.05); f1, f2, fl = rng.choice(["性味甘平無毒", "【性味】甘寒無毒"]), "主治風寒濕痺", rnd(8, 8)
            rep = (rng.random() < 0.7, rng.random() < 0.4, rng.random() < 0.15); style = rng.choice("bhm")
            entries = [("hdr", "머리글", [str(rng.randint(10, 300))])] if rng.random() < 0.3 else []
            while sum(len(e[2]) for e in entries) < 0.84 / sp - 8:
                if style == "h" or (style == "m" and rng.random() < 0.5):
                    entries.append((f"h{len(entries)}", "제목", ["【性味】"]))
                n = rng.randint(1, 6)
                entries.append((f"b{len(entries)}", "본문", [f1 if j == 0 and rep[0] else f2 if j == 1 and rep[1] else fl if j == n - 1 and n > 1 and rep[2] else rnd(4, 14) for j in range(n)]))
            owner = [e[0] for e in entries for _ in e[2]]; texts = [t for e in entries for t in e[2]]
            mid = [k for k, e in enumerate(entries) if 0 < k < len(entries) - 1 and e[0] != "hdr"]
            omitted = {"hdr"} | ({entries[k][0] for k in rng.sample(mid, min(len(mid), rng.randint(1, 2)))} if omit else set())
            starts = [0.08 + i * sp for i in range(len(texts))]; th = 0.6 * sp
            D = rng.uniform(0, 0.08) * rng.choice([1, 1, 1, -0.5]); noise = rng.uniform(0, 0.25) * sp
            layer = {"page_width": 500.0, "page_height": 700.0, "vertical_columns": [], "horizontal_lines": []}
            for a, t in zip(starts, texts):
                if horizontal:
                    layer["horizontal_lines"].append({"bbox": [50.0, a * 700, 450.0, (a + th) * 700], "text": t})
                else:
                    layer["vertical_columns"].append({"x": (1 - a - th / 2) * 500, "y0": 60.0, "y1": 640.0, "text": t})
            regs = []; truth = {}; i = 0
            for eid, kind, ls in entries:
                truth[eid] = [i, i + len(ls) - 1]; a0, a1 = starts[i], starts[i + len(ls) - 1] + th; i += len(ls)
                if eid in omitted:
                    continue
                m0, m1 = (a + D * (a - starts[0]) / 0.84 + rng.uniform(-noise, noise) for a in (a0, a1))
                mt = [t if len(t) < 6 or rng.random() > 0.1 else t[:len(t) // 2] + rng.choice(pool) + t[len(t) // 2 + 1:] for t in ls]
                bbox = frac_bbox((0.1, m0, 0.9, m1) if horizontal else (1 - m1, 60 / 700, 1 - m0, 640 / 700))
                regs.append(Region(id=eid, kind=kind, bbox=bbox, title_text=mt[0]) if kind == "제목" else
                            Region(id=eid, kind=kind, bbox=bbox, paragraphs=[Paragraph(columns=[Column(col_from_right=k + 1, text=x) for k, x in enumerate(mt)])]))
            fixes = refit_by_layer(PageReading(pdf_page=1, regions=regs), layer, horizontal)
            for r in regs:
                got = fixes.get(r.id, {}).get("lines")
                if got:
                    self.assertEqual({owner[k] for k in range(got[0], got[1] + 1)}, {r.id}, (seed, r.id, got, truth[r.id]))
                if omitted == {"hdr"}:
                    self.assertEqual(got, truth[r.id], (seed, r.id))


class HorizontalCover(unittest.TestCase):
    """가로쓰기에서 영역 밖 머리글 한 줄은 항목 하나로(글자 수만큼의 '기둥' 20건이 아니라)."""
    def test_uncovered_lines_axis_y(self):
        from yeone_ocr.layout import uncovered_ink_boxes
        from yeone_ocr.cli import cover_check
        im, p1, truth, _ = _drifted_page()
        for r in p1.regions:                                   # 참 좌표로 놓고 머리글만 남긴다
            y0, y1 = truth[r.id]; r.bbox = [int(y0 * 1000), 100, int(y1 * 1000), 900]
        uncovered_ink_boxes(im, [bbox_frac(r.bbox) for r in p1.regions], axis="x")
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

    def test_window_needs_neighbours_around_the_slot(self):
        """앞뒤 글자가 창 어디엔가 흩어져 있는 것만으로는 자리를 믿지 않는다: 앞 글자 뒤 0~2자 안에 뒤 글자가 와야 한다(고른 글자에 붙어 보이면 그대로 통과)."""
        from yeone_ocr.adjudicate import window_ok, window_matches
        self.assertFalse(window_ok("阴x能x", "肠，能", "人", "阴分"))              # 차례가 뒤집힘
        self.assertFalse(window_ok("能xxxx阴", "肠，能", "人", "阴分"))            # 너무 멀다
        self.assertTrue(window_ok("能阴", "肠，能", "人", "阴分")); self.assertTrue(window_ok("能入阴", "肠，能", "人", "阴分"))
        self.assertTrue(window_ok("阴xx能人", "肠，能", "人", "阴分"))             # 앞 글자+고른 글자가 붙어 보이면 자리는 맞다
        self.assertFalse(window_matches("阴x能x", "肠，能", "阴分")); self.assertTrue(window_matches("色赤?心", "色赤", "心，性"))
        self.assertTrue(window_matches("赤", "色赤", "心，性")); self.assertTrue(window_matches("", "色赤", "心，性"))

    def test_adjudicate_page_rejects_misplaced_crop(self):
        from yeone_ocr.adjudicate import adjudicate_page
        from yeone_ocr.schema import GlyphChoice
        reg = Region(id="r6", kind="본문", bbox=[434, 77, 468, 889], paragraphs=[Paragraph(columns=[Column(col_from_right=1, text="〔6/7〕.《本草分经》：苦辛，生温熟热")])])
        rec = {"pdf_page": 130, "pass1": PageReading(pdf_page=130, regions=[reg]).model_dump(), "merged": PageReading(pdf_page=130, regions=[reg]).model_dump()}
        class Fake:
            def adjudicate(self, crop, ask, horizontal, pdf_page):
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


# ---------------- 0.6.2: 3차 판별 묶음·동시 실행 ----------------
class AdjudicateCommand(unittest.TestCase):
    """cmd_adjudicate 가 쪽을 동시에 돌리며 글자층 틀·글자 상자를 넘기고, 사용량을 모델별로 누적하고, 기록을 저장하는지(가짜 판독기)."""
    COL = "天地玄黃宇宙洪荒日月盈昃辰宿列張"

    def _work(self, td):
        doc = vertical_pdf(2); pdf = os.path.join(td, "v.pdf"); doc.save(pdf); doc.close()
        doc = pymupdf.open(pdf); work = Work(pdf, os.path.join(td, "w"))
        for pg in (1, 2):
            cols = [Column(col_from_right=1, text="天地玄〔黃/黄〕宇宙洪荒日月盈昃辰宿列張"), Column(col_from_right=2, text=self.COL), Column(col_from_right=3, text="天地玄黃宇宙洪荒〔日/曰〕月盈昃辰宿列張")]
            reg = Region(id="b1", kind="본문", bbox=[60, 780, 380, 960], paragraphs=[Paragraph(columns=cols)])
            pr = PageReading(pdf_page=pg, regions=[reg], uncertain=[Uncertain(region_id="b1", col_from_right=1, context="「天地玄〔黃/黄〕宇宙」")])
            work.save_record(pg, {"pdf_page": pg, "pass1": pr.model_dump(), "merged": pr.model_dump(), "usage": {"calls": 2, "prompt_tokens": 10}})
        return doc, work

    def test_parallel_batched_run_updates_records(self):
        import contextlib, io, threading
        from yeone_ocr import cli
        from yeone_ocr.schema import GlyphChoice

        class Fake:
            model = "fake-pro"; n_429 = 0; cache_note = ""; thinking = "low"
            def __init__(self): self.threads = set(); self.batches = []; self._tls = threading.local()
            def usage_begin(self): self._tls.u = {"calls": 0, "prompt_tokens": 0, "output_tokens": 0, "thinking_tokens": 0, "cached_tokens": 0}
            def usage_end(self): u = getattr(self._tls, "u", {}); self._tls.u = None; return u
            def adjudicate_many(self, crops, horizontal, pdf_page):
                self.threads.add(threading.get_ident()); self.batches.append((pdf_page, [sid for sid, *_ in crops]))
                self._tls.u["calls"] += 1; self._tls.u["prompt_tokens"] += 100
                out = {}
                for sid, crop, ask in crops:
                    self.assertTrue(crop.width > 4 and crop.height > 4)
                    c = next(x for x in ask.candidates if x in "黃日")              # 인쇄 글자(정자). 후보 차례는 섞여 오니 글자로 고른다
                    out[sid] = GlyphChoice(choice=c, evidence="획", window="")       # 창이 비면 자리 검사는 통과(검사 불가)
                return out
            def adjudicate(self, *a, **k):
                raise AssertionError("묶음으로 다 답했으니 낱개 호출이 없어야 한다")
            assertTrue = staticmethod(lambda c: None)
        fake = Fake()
        old = cli.make_glyph_readers; cli.make_glyph_readers = lambda args: (fake, None)
        try:
            with tempfile.TemporaryDirectory() as td, contextlib.ExitStack() as handles:
                doc, work = self._work(td)
                handles.callback(doc.close)
                args = Namespace(pdf=work.pdf_path.as_posix(), work=str(work.dir), pages=None, dpi=300, mode="vertical", reader="gemini", model="fake-pro",
                                 thinking_budget=None, no_cache=True, max_sites=0, zoom=6, batch_sites=8, workers=2, workers_start=2, adjudicate_model="", adjudicate_thinking="low")
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    rc = cli.cmd_adjudicate(args, work, doc, standalone=True)
                out = buf.getvalue()
                self.assertEqual(rc, 0, out)
                self.assertEqual(sorted(pg for pg, _ in fake.batches), [1, 2]); self.assertTrue(all(sids == ["1", "2"] for _, sids in fake.batches))
                for pg in (1, 2):
                    rec = work.load_record(pg); m = PageReading.model_validate(rec["merged"]); body = region_text(m.regions[0])
                    self.assertIn("天地玄黃宇宙", body); self.assertIn("洪荒日月", body); self.assertNotIn("〔", body)
                    st = rec["adjudicate_stats"]; self.assertEqual((st["sites"], st["decided"], st.get("batched")), (2, 2, 2))
                    self.assertEqual([a["loc"] for a in rec["adjudications"]], ["layer", "layer"])          # 글자층이 있는 PDF → 글자 상자로
                    self.assertEqual([a["model"] for a in rec["adjudications"]], ["fake-pro", "fake-pro"])
                    u = rec["usage"]; self.assertEqual(u["calls"], 3); self.assertEqual(u["by_model"]["fake-pro (3차)"]["calls"], 1)
                    self.assertTrue(any("3차 판별: 「黃」 확정" in x.context for x in m.uncertain))
                self.assertIn("자리: 글자층 4", out); self.assertIn("묶음으로 답한 자리 4", out); self.assertIn("4곳 중 확정 4", out)
                # 다시 돌리면 판별할 자리가 없다(표시가 다 확정됨)
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(cli.cmd_adjudicate(args, work, doc), 0)
        finally:
            cli.make_glyph_readers = old

    def test_fallback_reader_accounting_and_log(self):
        """flash 가 못 가린 자리를 pro 가 가리면 사용량이 두 모델 열쇠에 따로 쌓이고 요약에 '보완 확정' 이 찍힌다."""
        import contextlib, io, threading
        from yeone_ocr import cli
        from yeone_ocr.schema import GlyphChoice
        class Base:
            n_429 = 0; cache_note = ""; thinking = "low"
            def __init__(self): self._tls = threading.local()
            def usage_begin(self): self._tls.u = {"calls": 0, "prompt_tokens": 0, "output_tokens": 0, "thinking_tokens": 0, "cached_tokens": 0}
            def usage_end(self): u = getattr(self._tls, "u", {}); self._tls.u = None; return u
            def adjudicate(self, *a, **k): raise AssertionError("낱개 호출 없음")
        class Flash(Base):
            model = "flash"
            def adjudicate_many(self, crops, h, p):
                self._tls.u["calls"] += 1
                return {sid: GlyphChoice(choice="", evidence="뭉개짐", window="") for sid, _, _ in crops}
        class Pro(Base):
            model = "pro"
            def adjudicate_many(self, crops, h, p):
                self._tls.u["calls"] += 1
                return {sid: GlyphChoice(choice=a.candidates[0], evidence="획", window="") for sid, _, a in crops}
        old = cli.make_glyph_readers; cli.make_glyph_readers = lambda args: (Flash(), Pro())
        try:
            with tempfile.TemporaryDirectory() as td, contextlib.ExitStack() as handles:
                doc, work = self._work(td)
                handles.callback(doc.close)
                args = Namespace(pdf=work.pdf_path.as_posix(), work=str(work.dir), pages=None, dpi=300, mode="vertical", reader="gemini", model="pro",
                                 thinking_budget=None, no_cache=True, max_sites=0, zoom=6, batch_sites=8, workers=2, workers_start=2, adjudicate_model="auto", adjudicate_thinking="low")
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    self.assertEqual(cli.cmd_adjudicate(args, work, doc, standalone=True), 0)
                out = buf.getvalue(); self.assertIn("→ 못 가리면 pro", out); self.assertIn("보완 확정 4", out)
                rec = work.load_record(1); u = rec["usage"]
                self.assertEqual((u["by_model"]["flash (3차)"]["calls"], u["by_model"]["pro (3차 보완)"]["calls"], u["calls"]), (1, 1, 4))
                self.assertEqual(rec["adjudicate_stats"]["fallback_decided"], 2); self.assertEqual({a["model"] for a in rec["adjudications"]}, {"pro"})
        finally:
            cli.make_glyph_readers = old

    def _undecided_reader(self, model):
        import threading
        from yeone_ocr.schema import GlyphChoice
        class Fake:
            n_429 = 0; cache_note = ""; thinking = "low"
            def __init__(self): self.model = model; self.asked = 0; self._tls = threading.local()
            def usage_begin(self): self._tls.u = {"calls": 0, "prompt_tokens": 0, "output_tokens": 0, "thinking_tokens": 0, "cached_tokens": 0}
            def usage_end(self): u = getattr(self._tls, "u", None) or {}; self._tls.u = None; return u
            def adjudicate_many(self, crops, h, p):
                self.asked += len(crops); self._tls.u["calls"] += 1
                return {sid: GlyphChoice(choice="", evidence="뭉개짐", window="") for sid, _, _ in crops}
            def adjudicate(self, crop, ask, h, p):
                self.asked += 1; self._tls.u["calls"] += 1
                return GlyphChoice(choice="", evidence="뭉개짐", window="")
        return Fake()

    def test_rerun_does_not_buy_sites_the_same_model_already_left_undecided(self):
        """다시 실행해도 같은 모델이 앞서 답했지만 못 가린 〔a/b〕 자리는 다시 묻지 않는다(0.7.4: [▶] 를 누를 때마다 책 전체를 다시 물었다).
        다른 모델도 기본(--skip-answered any-model)은 묻지 않고, same-model 이거나 --readjudicate 면 다시 묻는다."""
        from yeone_ocr import cli
        old = cli.make_glyph_readers
        try:
            with tempfile.TemporaryDirectory() as td, contextlib.ExitStack() as handles:
                doc, work = self._work(td)
                handles.callback(doc.close)
                def run(model, **kw):
                    fake = self._undecided_reader(model); cli.make_glyph_readers = lambda args: (fake, None)
                    args = Namespace(pdf=work.pdf_path.as_posix(), work=str(work.dir), pages=None, dpi=300, mode="vertical", reader="gemini", model=model,
                                     thinking_budget=None, no_cache=True, max_sites=0, zoom=6, batch_sites=8, workers=2, workers_start=2,
                                     adjudicate_model="", adjudicate_thinking="low", no_suspects=True, **kw)
                    buf = io.StringIO()
                    with contextlib.redirect_stdout(buf):
                        rc = cli.cmd_adjudicate(args, work, doc, standalone=True)
                    self.assertEqual(rc, 0, buf.getvalue())
                    return fake.asked, buf.getvalue()
                asked, _ = run("gpt-4.1")
                self.assertEqual(asked, 4)                                                    # 2쪽 × 2자리, 모두 못 가림
                asked, out = run("gpt-4.1")
                self.assertEqual(asked, 0); self.assertIn("결론 난 미정 자리 4곳은 다시 묻지 않음", out)
                self.assertEqual(len(work.load_record(1)["adjudications"]), 2)                # 같은 기록을 다시 쌓지 않는다
                r1 = work.load_record(1); r1["adjudications"] = r1["adjudications"][:1]; work.save_record(1, r1)   # 한 쪽에 결론 난 자리와 새 자리가 섞임
                self.assertEqual(run("gpt-4.1")[0], 1)                                        # 새 자리만 묻는다(adjudicate_page 에도 건너뛸 모델이 넘어감)
                self.assertEqual(len(work.load_record(1)["adjudications"]), 2)
                asked, out = run("gemini-2.5-pro")                                            # 다른 모델도 기본은 다시 사지 않는다(any-model)
                self.assertEqual(asked, 0); self.assertIn("결론 난 미정 자리 4곳은 다시 묻지 않음(다른 모델이 물은 것 포함", out)
                self.assertIn("--skip-answered same-model", out)
                self.assertEqual(run("gemini-2.5-pro", skip_answered="same-model")[0], 4)     # 이번 모델로 다시 묻기
                self.assertEqual(run("gpt-4.1", readjudicate=True)[0], 4)                     # 일부러 다시 묻기(any-model 보다 앞섬)
                self.assertEqual(run("gemini-2.5-pro", skip_answered="same-model")[0], 0)
                asked, out = run("claude-test", readjudicate=True, skip_answered="any-model")
                self.assertEqual(asked, 4); self.assertNotIn("다시 묻지 않음", out)
        finally:
            cli.make_glyph_readers = old

    def test_answered_sites_rules(self):
        """다시 묻지 않을 자리: 그 모델이 답했지만 못 가린 자리(창 어긋남·눈가림 불일치 포함)와 문턱보다 작아 묻지 않은 자리.
        호출 실패·눈가림 확인 실패·다른 모델·이미 고른 자리·문턱을 낮춘 저해상 자리는 넣지 않는다."""
        from yeone_ocr.adjudicate import answered_sites, pending_sites, site_key
        key = lambda i: {"region_id": "b1", "col_index": i, "a": "黃", "b": "黄", "pre": "天地玄", "post": "宇宙洪荒"}
        rec = {"adjudications": [
            {**key(0), "model": "m", "choice": "", "evidence": "뭉개짐"},
            {**key(1), "model": "m", "choice": "", "evidence": "조각 위치가 어긋남(보인 글: 甲) — x"},
            {**key(2), "model": "m", "choice": "", "evidence": "판별 실패: 묶음 호출 실패: 429"},
            {**key(3), "model": "m", "choice": "", "evidence": "눈가림 확인 실패(응답 없음) — 확정 보류"},
            {**key(4), "model": "other", "choice": "", "evidence": "뭉개짐"},
            {**key(5), "model": "m", "choice": "黃", "evidence": "획"},
            {**key(6), "choice": "", "evidence": "저해상: 원본 글자 약 9px(문턱 16px)", "em_px": 9.0},
            {**key(7), "choice": "", "evidence": "저해상: 원본 글자 약 14px(문턱 16px)", "em_px": 14.0}]}
        got = answered_sites(rec, {"m"}, min_px=12)
        self.assertEqual(got, {site_key(key(0)), site_key(key(1)), site_key(key(6))})
        self.assertEqual(answered_sites(rec, set(), min_px=0), set())
        with tempfile.TemporaryDirectory() as td, contextlib.ExitStack() as handles:
            doc, work = self._work(td); handles.callback(doc.close)
            r = work.load_record(1)
            sites, skipped = pending_sites(r, {"m"})
            self.assertEqual((len(sites), skipped), (2, 0))
            r["adjudications"] = [{**{k: getattr(sites[0], k) for k in ("region_id", "col_index", "a", "b", "pre", "post")}, "model": "m", "choice": "", "evidence": "x"}]
            sites2, skipped = pending_sites(r, {"m"})
            self.assertEqual(([x.mark for x in sites2], skipped), ([sites[1].mark], 1))
            self.assertEqual(len(pending_sites(r, ())[0]), 2)                                  # 모델을 주지 않으면(--readjudicate) 모두
            s = sites[1]; e = {**{k: getattr(s, k) for k in ("region_id", "col_index", "a", "b", "post", "char_index")}, "model": "m", "choice": "", "evidence": "x"}
            swap = {"黃": "黄", "黄": "黃"}
            e["pre"] = (s.pre[:-1] + swap.get(s.pre[-1], "甲")) if s.pre else ""                 # 곁 자리의 답이 반영돼 앞 글 한 자가 바뀐 기록
            r["adjudications"].append(e)
            self.assertEqual(pending_sites(r, {"m"})[1], 2)                                     # 그래도 같은 자리로 보아 다시 사지 않는다
            e["char_index"] = s.char_index + 9
            self.assertEqual(pending_sites(r, {"m"})[1], 1)                                     # 글자 번호가 멀면 다른 자리

    def test_answered_sites_any_model(self):
        """--skip-answered any-model: 다른 모델(모델 칸이 없는 0.6.x 기록 포함)이 답했지만 못 가린 자리도 건너뛴다. 호출이 실패한 자리는 어느 모델이든 다시 묻는다.
        모델 집합이 비면(--readjudicate) any_model 이어도 모두 묻는다."""
        from yeone_ocr.adjudicate import answered_sites, pending_sites, site_key
        key = lambda i: {"region_id": "b1", "col_index": i, "a": "黃", "b": "黄", "pre": "天地玄", "post": "宇宙洪荒"}
        rec = {"adjudications": [
            {**key(0), "model": "m", "choice": "", "evidence": "뭉개짐"},
            {**key(1), "model": "other", "choice": "", "evidence": "뭉개짐"},
            {**key(2), "choice": "", "evidence": "획이 보이지 않음"},                                       # 0.6.x: 모델 칸 없음
            {**key(3), "model": "other", "choice": "", "evidence": "판별 실패: 묶음 호출 실패: 429"},
            {**key(4), "model": "other", "choice": "", "evidence": "눈가림 확인 실패(응답 없음) — 확정 보류"},
            {**key(5), "model": "other", "choice": "黃", "evidence": "획"}]}
        self.assertEqual(answered_sites(rec, {"m"}, any_model=True), {site_key(key(0)), site_key(key(1)), site_key(key(2))})
        self.assertEqual(answered_sites(rec, {"m"}), {site_key(key(0))})                                 # 기본(same-model)은 그 모델 것만
        with tempfile.TemporaryDirectory() as td, contextlib.ExitStack() as handles:
            doc, work = self._work(td); handles.callback(doc.close)
            r = work.load_record(1)
            sites, _ = pending_sites(r, {"m"})
            r["adjudications"] = [{**{k: getattr(sites[0], k) for k in ("region_id", "col_index", "a", "b", "pre", "post")}, "model": "other", "choice": "", "evidence": "x"}]
            self.assertEqual(pending_sites(r, {"m"}, any_model=True)[1], 1)
            self.assertEqual(pending_sites(r, {"m"})[1], 0)
            self.assertEqual(pending_sites(r, set(), any_model=True), (sites, 0))                     # --readjudicate 가 앞선다

    def test_mock_reader_counts_honour_skip_answered(self):
        """mock 판독기로 세기만 해도(호출 없음) 다른 모델이 못 가린 자리를 any-model 이면 빼고 same-model 이면 센다."""
        from yeone_ocr import cli
        from yeone_ocr.adjudicate import find_sites
        with tempfile.TemporaryDirectory() as td, contextlib.ExitStack() as handles:
            doc, work = self._work(td); handles.callback(doc.close)
            r = work.load_record(1); x = find_sites(PageReading.model_validate(r["merged"]))[0]
            r["adjudications"] = [{**{k: getattr(x, k) for k in ("region_id", "col_index", "a", "b", "pre", "post")}, "model": "gpt-4.1", "choice": "", "evidence": "뭉개짐"}]
            work.save_record(1, r)
            for value, want in (("any-model", "〔a/b〕 자리 3곳"), ("same-model", "〔a/b〕 자리 4곳")):
                args = cli.build_parser().parse_args(["adjudicate", work.pdf_path.as_posix(), "--work", str(work.dir), "--reader", "mock", "--no-suspects",
                                                      "--mode", "vertical", "--skip-answered", value])
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    self.assertEqual(cli.cmd_adjudicate(args, work, doc, standalone=True), 0)
                self.assertIn(want, buf.getvalue(), value)

    def test_window_asks_before_buying_third_pass_on_rerun(self):
        """[▶ 한 번에 인식] 전 비용 창의 3차 판별 셈(count_plan): 이미 판독한 쪽의 자리·호출 어림, 앞서 물어 못 가린 자리는 모델을 바꿔도 건너뜀(끄면 다시 삼),
        다시 판독은 모두 새로 읽는 셈. 창에서 [아니요]=--no-adjudicate(문맥 교정 모델이 켜져 있으면 + --no-context-model) · [취소]=실행 안 함."""
        from types import SimpleNamespace
        from unittest import mock
        from yeone_ocr.adjudicate import find_sites
        from yeone_ocr.costplan import PlanInput, count_plan
        with tempfile.TemporaryDirectory() as td:
            doc, work = self._work(td); doc.close()
            def plan(model="gpt-4.1", **kw):
                return count_plan(PlanInput(kind="run", pdf=work.pdf_path.as_posix(), work_dir=str(work.dir), reader="openai", model=model, **kw))
            def glyph(p, scope="have"):
                return next(r for r in p["rows"] if r["step"] == "glyph" and r["scope"] == scope)
            self.assertEqual((glyph(plan())["items"], glyph(plan())["calls"]), (4, [2, 6]))      # 쪽마다 8자리씩 묶어 2회, 낱개로 최대 4회 더
            r = work.load_record(1)                                                             # 1쪽 두 자리는 gpt-4.1 이 이미 물어 못 가림
            r["adjudications"] = [{**{k: getattr(x, k) for k in ("region_id", "col_index", "a", "b", "pre", "post")}, "model": "gpt-4.1", "choice": "", "evidence": "x"}
                                  for x in find_sites(PageReading.model_validate(r["merged"]))]
            work.save_record(1, r)
            p = plan()
            self.assertEqual((glyph(p)["items"], p["skipped"]["glyph"], p["skipped"]["glyph_other"]), (2, 2, 0))
            p = plan("gemini-2.5-pro")                                                          # 모델을 바꿔도 기본은 그 2곳을 사지 않는다
            self.assertEqual((glyph(p)["items"], p["skipped"]["glyph_other"]), (2, 2))
            self.assertEqual(glyph(plan("gemini-2.5-pro", skip_any_model=False))["items"], 4)   # 끄면 이번 모델로 다시 묻는다(호출 듦)
            self.assertEqual(glyph(plan(skip_any_model=False))["items"], 2)                     # 같은 모델이 물은 것은 끄더라도 건너뜀
            forced = plan(force=True)                                                           # 다시 판독: 기록 쪽 셈 대신 새로 읽을 쪽 어림
            self.assertFalse([r for r in forced["rows"] if r["scope"] == "have"])
            self.assertEqual(glyph(forced, "new")["label"], "3차 · 글자 판별 — 새로 읽을 2쪽(어림)")
            # 창: [아니요] 면 run 명령에 --no-adjudicate(+ 문맥 교정 모델이 켜져 있으면 --no-context-model), [취소] 면 실행하지 않음
            try:
                from yeone_ocr import gui
            except Exception as e:
                self.skipTest(f"gui 를 불러오지 못함: {e}")
            var = lambda v: Namespace(get=lambda v=v: v)
            app = gui.App.__new__(gui.App)
            app.v_reader = var("openai"); app.v_model = var("gpt-4.1"); app.v_pages = var(""); app.v_skipany = var(True)
            app.pdf = lambda: work.pdf_path.as_posix(); app.work = lambda: Path(work.dir)
            app._need_pdf = app._prepare_env = lambda: True
            app._pages = app._read_opts = app._build_opts = lambda: []; app._glyph_opts = lambda suspects=False: []
            app._answered_opts = lambda: ["--skip-answered", "any-model"]
            app.nb = mock.Mock(); started = []; app.start = lambda argv, label: started.append(argv); app._save_book = mock.Mock()
            app.cfg = {}; app._fx = None; app._append = lambda s: None; app.v_status = mock.Mock(); app.wait_window = lambda d: None
            app.buttons = []; app.running = False; app._plan_wait = None; app._plan_seq = 1
            for ctx, want in ((True, ["--no-adjudicate", "--no-context-model"]), (False, ["--no-adjudicate"])):
                p = plan(context_model=ctx)
                app._with_plan = lambda kind, go, p=p: gui.App._plan_done(app, 1, kind, ("ok", p), go)
                with mock.patch.object(gui, "CostDialog", lambda *a, **k: SimpleNamespace(choice="without")):
                    app.do_run()
                self.assertEqual(started[-1][-len(want):], want, ctx)
                self.assertEqual(started[-1][started[-1].index("--skip-answered") + 1], "any-model")   # 창은 늘 넘긴다
                app._save_book.assert_called_with(create=True)
            n, saved = len(started), app._save_book.call_count
            with mock.patch.object(gui, "CostDialog", lambda *a, **k: SimpleNamespace(choice=None)):
                app.do_run()
            self.assertEqual((len(started), app._save_book.call_count), (n, saved))             # [취소]: 실행도 책별 설정 저장도 없음

    def test_auto_counts_sites_after_flash_name_is_known_and_keeps_old_stats(self):
        """--adjudicate-model auto: flash 가 앞서 물어 못 가린 자리는 세기 전에 '다시 묻지 않을' 집합에 든다(전에는 세고 나서 더해 〔a/b〕 수가 많이 찍히고,
        물을 것이 없는 쪽도 adjudicate_page 를 거쳐 앞 실행의 adjudicate_stats 가 0 으로 덮였다)."""
        from yeone_ocr import cli
        from yeone_ocr.adjudicate import find_sites
        old = cli.make_glyph_readers
        try:
            with tempfile.TemporaryDirectory() as td, contextlib.ExitStack() as handles:
                doc, work = self._work(td); handles.callback(doc.close)
                stats = {"sites": 2, "decided": 0, "undecided": 2, "failed": 0}
                for pg in (1, 2):
                    r = work.load_record(pg)
                    r["adjudications"] = [{**{k: getattr(x, k) for k in ("region_id", "col_index", "a", "b", "pre", "post")}, "model": "flash", "choice": "", "evidence": "뭉개짐"}
                                          for x in find_sites(PageReading.model_validate(r["merged"]))]
                    r["adjudicate_stats"] = dict(stats); work.save_record(pg, r)
                flash, pro = self._undecided_reader("flash"), self._undecided_reader("pro")
                cli.make_glyph_readers = lambda args: (flash, pro)
                args = Namespace(pdf=work.pdf_path.as_posix(), work=str(work.dir), pages=None, dpi=300, mode="vertical", reader="gemini", model="pro",
                                 thinking_budget=None, no_cache=True, max_sites=0, zoom=6, batch_sites=8, workers=2, workers_start=2,
                                 adjudicate_model="auto", adjudicate_thinking="low", no_suspects=True)
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    self.assertEqual(cli.cmd_adjudicate(args, work, doc, standalone=True), 0)
                out = buf.getvalue()
                self.assertIn("판별할 〔a/b〕 자리가 없습니다", out); self.assertIn("결론 난 미정 자리 4곳은 다시 묻지 않음", out)
                self.assertNotIn("〔a/b〕 4곳", out)
                self.assertEqual((flash.asked, pro.asked), (0, 0))
                self.assertEqual(work.load_record(1)["adjudicate_stats"], stats)
                r = work.load_record(1); r["adjudications"] = r["adjudications"][:1]; work.save_record(1, r)   # 1쪽에 아직 묻지 않은 자리 하나
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    self.assertEqual(cli.cmd_adjudicate(args, work, doc, standalone=True), 0)
                self.assertIn("1쪽 · 〔a/b〕 1곳", buf.getvalue())
                self.assertEqual(flash.asked, 1)
                self.assertEqual(work.load_record(2)["adjudicate_stats"], stats)                               # 물을 것이 없는 쪽은 그대로
        finally:
            cli.make_glyph_readers = old

    def test_run_adaptive_throttles_and_stops_on_fatal(self):
        import contextlib, io
        from yeone_ocr import cli
        class R: n_429 = 0
        r = R(); seen = []; errors = []
        def fn(item):
            if item == 3: r.n_429 += 1                       # 한도 초과를 한 번 흉내
            if item == 7: raise RuntimeError("치명")
            return item * 10
        def on_result(item, res): seen.append(res)
        def on_error(item, e): errors.append(item); return True
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            stopped = cli.run_adaptive(list(range(12)), fn, [r], ceiling=3, start=2, on_result=on_result, on_error=on_error, pause=0.2)
        self.assertTrue(stopped); self.assertEqual(errors, [7]); self.assertLess(len(seen), 12)
        self.assertIn("한도 초과(429) 감지", buf.getvalue())
        cli.CANCEL.clear()


# ---------------- 0.6.3: 끝까지 차는 진행 막대 ----------------
class ProgressTracker(unittest.TestCase):
    """단계별 알림을 막대 하나(0~1)로 모은다: 한 방향으로만 차고, 건너뛴 단계는 끝난 것으로, 구획 안 판독은 그 구획 몫으로."""
    def test_run_plan_is_monotonic_and_skips(self):
        from yeone_ocr.progress import Tracker, PLANS
        self.assertAlmostEqual(sum(w for _, w in PLANS["run"]), 1.0)
        t = Tracker("run", now=0.0)
        self.assertEqual(t.update("prepare", 0, 10), 0.0)
        self.assertAlmostEqual(t.update("prepare", 10, 10), 0.03)
        self.assertAlmostEqual(t.update("read", 5, 10), 0.03 + 0.75 * 0.5)
        self.assertAlmostEqual(t.update("read", 3, 10), 0.03 + 0.75 * 0.5)         # 뒤로 가지 않는다
        self.assertAlmostEqual(t.update("prepare", 1, 10), 0.03 + 0.75 * 0.5)      # 지난 단계의 늦은 알림은 무시
        self.assertAlmostEqual(t.update("build", 0, 5), 0.78 + 0.12)               # 3차 판별이 없었으면(자리 0) 문서 단계 알림 때 끝난 것으로
        self.assertAlmostEqual(t.update("report", 1, 1), 1.0)
        self.assertEqual(Tracker("run").update("read", 0, 0), 0.03 + 0.75)          # 읽을 쪽이 없으면 판독 단계는 끝
        self.assertEqual(Tracker("unknown").update("read", 3, 4), 0.0)              # 계획에 없는 명령은 막대를 움직이지 않음

    def test_nested_collate_and_text(self):
        from yeone_ocr.progress import Tracker
        t = Tracker("collate", now=0.0)
        t.update("collate", 1, 4)                                                    # 구획 2 시작
        f = t.update("read", 2, 4)                                                   # 그 구획의 쪽 판독 절반
        self.assertAlmostEqual(f, (1 + 0.1 + 0.85 * 0.5) / 4)
        self.assertAlmostEqual(t.update("collate", 2, 4), 0.5)                       # 다음 구획 시작 → 하위 진행은 처음부터
        self.assertIn("50%", t.text(now=100.0)); self.assertIn("구획 대조 2/4구획", t.text(now=100.0))
        self.assertIn("남은 시간 약 2분", t.text(now=100.0))                          # 100초에 절반 → 100초 남음
        self.assertEqual(Tracker("run", now=0.0).eta(now=5.0), "")                   # 처음 몇 초·몇 %는 어림하지 않음
        t.finish(); self.assertEqual(t.frac, 1.0); self.assertNotIn("남은 시간", t.text(now=200.0))

    def test_eta_waits_for_the_main_stage(self):
        """남은 시간은 판독(가장 무거운 단계)이 한 칸이라도 끝난 뒤, 판독이 시작된 때부터의 속도로 어림한다."""
        from yeone_ocr.progress import Tracker
        t = Tracker("run", now=0.0)
        t.update("prepare", 10, 10, now=5.0); t.update("read", 0, 10, now=5.0)
        for now in (15.0, 30.0, 54.0):
            self.assertEqual(t.eta(now=now), "")                                      # 첫 쪽이 끝나기 전에는 어림하지 않는다(예전: 8분→16분→29분)
        t.update("read", 6, 10, now=55.0)
        self.assertEqual(t.eta(now=55.0), "남은 시간 약 1분 안")                       # 50초에 0.45 → 남은 0.52 는 약 58초
        skip = Tracker("run", now=0.0); skip.update("read", 0, 0, now=20.0)
        self.assertEqual(skip.eta(now=30.0), "")                                      # 판독할 쪽이 없으면 그 순간의 도약으로 어림하지 않는다

    def test_live_text_shows_elapsed_until_eta(self):
        """남은 시간을 어림할 수 없는 동안에는 지난 시간을 붙여 멈춘 것이 아님을 보인다. 설치는 남은 시간을 어림하지 않는다."""
        from yeone_ocr.progress import Tracker
        t = Tracker("run", now=0.0)
        t.update("prepare", 10, 10, now=5.0); t.update("read", 0, 40, now=5.0)
        self.assertEqual(t.live_text(now=57.0), "3% · 판독 0/40쪽 · 지난 시간 57초")
        self.assertEqual(Tracker("", now=0.0).live_text(now=65.0), "진행 중 · 지난 시간 1분 05초")
        i = Tracker("install", now=0.0); i.update("resolve", 10, 10, now=1.0); i.update("download", 3, 10, now=2.0)
        self.assertEqual(i.eta(now=100.0), ""); self.assertIn("지난 시간", i.live_text(now=100.0))

    def test_text_counts(self):
        from yeone_ocr.progress import Tracker
        t = Tracker("run", now=0.0)
        t.update("build", 3, 5); self.assertIn("문서 만들기 3/4쪽", t.text(now=1.0))    # 전체 5 = 4쪽 + 조립 한 번
        t.update("build", 4, 5); self.assertIn("문서 만들기 · 문서 조립", t.text(now=1.0))
        f = t.frac; t.update("normalize", 1200, 9000)                                  # 문서 조립 중 문맥 교정: 막대는 그대로, 글로 몇 건째인지
        self.assertEqual(t.frac, f); self.assertIn("문서 만들기 · 문맥 교정 1,200/9,000건", t.text(now=1.0))
        self.assertEqual(t.eta(now=500.0), ""); self.assertIn("지난 시간", t.live_text(now=500.0))   # 판독 속도로 '약 1분' 이라 하지 않는다
        t.update("normalize", 9000, 9000); self.assertIn("문서 만들기 · 문서 조립", t.text(now=1.0))
        t.update("review", 0, 1); self.assertTrue(t.text(now=1.0).endswith("검수 화면"))  # 0/1쪽 같은 뜻 없는 셈은 빼고
        i = Tracker("install", now=0.0)
        self.assertAlmostEqual(i.update("resolve", 10, 10), 0.3)                       # 확인만 끝나면 30%(예전: 몇 초 만에 90%)
        self.assertAlmostEqual(i.update("download", 2, 4), 0.3 + 0.55 * 0.5); self.assertIn("내려받기 2/4개", i.text(now=1.0))
        self.assertAlmostEqual(i.update("setup", 0, 1), 0.85); self.assertTrue(i.text(now=1.0).endswith("설치"))

    def test_fmt_duration(self):
        from yeone_ocr.progress import fmt_duration, fmt_elapsed
        self.assertEqual([fmt_duration(x) for x in (30, 90, 3600, 3720, 7200)], ["1분 안", "2분", "1시간", "1시간 2분", "2시간"])
        self.assertEqual([fmt_elapsed(x) for x in (-1, 7, 65, 3600, 3725)], ["0초", "7초", "1분 05초", "1시간 00분", "1시간 02분"])


class ProgressFromCommands(unittest.TestCase):
    """mock 판독기로 한 번에 인식을 돌리면 준비·판독·문서·검수 화면·보고가 차례로 알리고, 막대가 100%에 닿는다."""
    def test_run_reports_every_stage(self):
        import contextlib, io
        from yeone_ocr import cli
        from yeone_ocr.progress import Tracker
        events = []
        with tempfile.TemporaryDirectory() as td:
            doc = vertical_pdf(3); pdf = os.path.join(td, "v.pdf"); doc.save(pdf); doc.close()
            cli.PROGRESS = lambda st, d, t: events.append((st, d, t))
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    rc = cli.main(["run", pdf, "--work", os.path.join(td, "w"), "--reader", "mock", "--checkpoint", "0"])
            finally:
                cli.PROGRESS = None
        self.assertEqual(rc, 0)
        stages = []
        for st, _, _ in events:
            if not stages or stages[-1] != st:
                stages.append(st)
        self.assertEqual(stages, ["prepare", "read", "build", "review", "report"])     # mock 은 3차 판별을 하지 않는다
        last = {}
        for st, d, t in events:
            self.assertLessEqual(d, t); last[st] = (d, t)
        self.assertTrue(all(d == t for d, t in last.values()), last)
        tr = Tracker("run")
        for e in events:
            tr.update(*e)
        self.assertAlmostEqual(tr.frac, 1.0)

    def test_stop_after_last_page_is_not_a_stop(self):
        """[중지]가 마지막 쪽들이 도는 중에 눌렸으면 다 끝난 것이므로 0, 남은 쪽이 있으면 130."""
        import contextlib
        from yeone_ocr import cli
        with tempfile.TemporaryDirectory() as td:
            doc = vertical_pdf(3); pdf = os.path.join(td, "v.pdf"); doc.save(pdf); doc.close()
            def at_end(st, d, t):
                if st == "read" and t and d == t: cli.CANCEL.set()
            def at_start(st, d, t):
                if st == "read" and d == 0: cli.CANCEL.set()
            try:
                for cb, want, sub in ((at_end, 0, "a"), (at_start, 130, "b")):
                    cli.CANCEL.clear(); cli.PROGRESS = cb
                    with contextlib.redirect_stdout(io.StringIO()):
                        rc = cli.main(["read", pdf, "--work", os.path.join(td, sub), "--reader", "mock", "--checkpoint", "0"])
                    self.assertEqual(rc, want, sub)
            finally:
                cli.PROGRESS = None; cli.CANCEL.clear()

    def test_progress_callback_errors_do_not_stop_work(self):
        from yeone_ocr import cli
        cli.PROGRESS = lambda *a: 1 / 0
        try:
            cli.progress("read", 1, 2)                                                 # 예외를 삼킨다
        finally:
            cli.PROGRESS = None


class GuiArt(unittest.TestCase):
    """0.6.3 — 단추·체크 상자 그림: PNG 로 바로 읽히고, 알파 모서리·✓ 표시가 제자리에 있다(외부 패키지 없이 그림)."""
    def _decode(self, b64):
        import base64
        return Image.open(io.BytesIO(base64.b64decode(b64))).convert("RGBA")

    def test_rounded_box_and_stretch(self):
        from yeone_ocr import gui_art as A
        w, b = A.stretch_size(7, 96); self.assertEqual((w, b), (2 * 8 + 96, 8))
        im = self._decode(A.rounded_box(w, 40, 7, "#3d1d16", "#b8934e", 1.0))
        self.assertEqual(im.size, (w, 40))
        self.assertEqual(im.getpixel((0, 0))[3], 0)                                  # 둥근 모서리 밖은 투명
        self.assertEqual(im.getpixel((w // 2, 20)), (0x3d, 0x1d, 0x16, 255))         # 가운데는 채움색(표본 없이 빠르게 칠한 칸)
        self.assertEqual(im.getpixel((w // 2, 0))[:3], (0xb8, 0x93, 0x4e))           # 테두리
        half = self._decode(A.rounded_box(40, 20, 8, "#ffffff", corners="l"))
        self.assertEqual(half.getpixel((39, 0))[3], 255); self.assertEqual(half.getpixel((0, 0))[3], 0)   # 왼쪽만 둥글다

    def test_checkbox_marks(self):
        from yeone_ocr import gui_art as A
        on = self._decode(A.checkbox(16, True, "#3d1d16", "#d0c6b4", gap=8)); off = self._decode(A.checkbox(16, False, "#3d1d16", "#d0c6b4", gap=8))
        self.assertEqual(on.size, (24, 16))                                           # 오른쪽 8px 는 글자와의 간격(투명)
        self.assertEqual(on.getpixel((20, 8))[3], 0)
        self.assertEqual(on.getpixel((3, 3))[:3], (0x3d, 0x1d, 0x16))                 # 켜짐: 강조색 상자
        self.assertGreater(sum(on.getpixel((7, 11))[:3]), 600)                        # ✓ 획(흰색)이 지나는 자리
        self.assertEqual(off.getpixel((8, 8))[:3], (255, 255, 255))                   # 꺼짐: 흰 상자
        r = self._decode(A.radio(16, True, "#3d1d16", "#d0c6b4"))
        self.assertEqual(r.getpixel((8, 8))[:3], (0x3d, 0x1d, 0x16)); self.assertEqual(r.getpixel((0, 0))[3], 0)

    def test_flat_edges_match_full_sampling(self):
        """곧은 변을 한 칸만 계산해 되풀이해도(창이 빨리 뜨게) 하나하나 표본을 뜬 그림과 픽셀까지 같다."""
        from yeone_ocr import gui_art as A
        for w, h, r, bw in ((60, 30, 7, 1.0), (40, 40, 12, 2.0), (9, 9, 2.5, 1.0)):
            def at(x, y):
                if not A._in_rrect(x, y, 0, 0, w, h, r):
                    return None
                return (10, 20, 30, 255) if not A._in_rrect(x, y, bw, bw, w - bw, h - bw, max(0.0, r - bw)) else (250, 240, 230, 255)
            self.assertEqual(A._render(w, h, at, flat=int(r + bw) + 1), A._render(w, h, at))


# ---------------- 0.6.4: 이미지 입력 ----------------
class RotatedLayer(unittest.TestCase):
    """쪽 /Rotate: 글자층 좌표(PyMuPDF 는 /Rotate 를 뺀 좌표로 준다)를 쪽 이미지·page_frame 과 같은 틀로 옮긴다
    (/Rotate 180 내장 스캔을 바로 세워 쓰게 된 뒤, 3차 조각이 쪽의 반대편 구석을 자르지 않게)."""
    def _doc(self, rot: int, embedded: bool):
        src = pymupdf.open(); sp = src.new_page(width=600, height=800)
        sp.insert_text((60, 100), "一二三四五", fontsize=30, fontname="china-ss")
        if not embedded:
            sp.set_rotation(rot); return src
        pix = sp.get_pixmap(dpi=150)
        doc = pymupdf.open(); page = doc.new_page(width=600, height=800)
        page.insert_image(page.rect, pixmap=pix)
        page.insert_text((60, 100), "一二三四五", fontsize=30, fontname="china-ss", render_mode=3)   # 보이지 않는 글자층(스캔 위 OCR 글)
        page.set_rotation(rot)
        return doc

    def test_layer_boxes_land_on_ink(self):
        import numpy as np
        from yeone_ocr.pdfio import layer_chars, page_frame, page_image, text_layer
        for rot, emb in ((0, True), (180, True), (90, False), (180, False), (270, False)):
            doc = self._doc(rot, emb)
            im, src = page_image(doc, 1, dpi=100); a = np.asarray(im.convert("L"))
            fx, fy, fw, fh = page_frame(doc, 1); H, W = a.shape
            chars = [c for l in layer_chars(doc, 1) for c in l["chars"]]
            self.assertEqual(len(chars), 5, (rot, emb))
            for ch, bb in chars:
                x0, x1 = int((bb[0] - fx) / fw * W), int((bb[2] - fx) / fw * W); y0, y1 = int((bb[1] - fy) / fh * H), int((bb[3] - fy) / fh * H)
                sub = a[max(0, y0):y1, max(0, x0):x1]
                self.assertTrue(sub.size and (sub < 128).mean() > 0.02, (rot, emb, ch, src, bb))
            tl = text_layer(doc, 1)
            self.assertTrue(tl.vertical_columns if rot == 90 else tl.horizontal_lines, (rot, emb))   # 쪽에 보이는 방향으로 나눈다(90 은 위→아래로 흐름, 270 은 아래→위라 세로 열이 아님)


class ImageInput(unittest.TestCase):
    """PDF 대신 이미지·이미지 폴더를 주어도 같은 길로 읽는다: 쪽 이미지는 원본 그대로(무손실), 순서는 이름의 숫자 순."""
    def _imgs(self, d):
        from PIL import ImageDraw
        a = Image.new("L", (600, 900), 255); ImageDraw.Draw(a).rectangle((50, 50, 150, 150), fill=0); a.save(os.path.join(d, "p1.png"), dpi=(400, 400))
        Image.new("RGB", (500, 700), (240, 238, 230)).save(os.path.join(d, "p2.jpg"), quality=90)
        fr = [Image.new("L", (400, 600), v) for v in (200, 120)]; fr[0].save(os.path.join(d, "p10.tif"), save_all=True, append_images=fr[1:])
        Image.new("RGBA", (300, 300), (0, 0, 0, 0)).save(os.path.join(d, "p3.png"))
        return a

    def test_folder_order_and_lossless_pages(self):
        from PIL import ImageChops
        from yeone_ocr import pdfio
        with tempfile.TemporaryDirectory() as td:
            a = self._imgs(td)
            self.assertEqual([p.name for p in pdfio.image_sources(td)], ["p1.png", "p2.jpg", "p3.png", "p10.tif"])   # p10 은 p3 뒤
            doc = pdfio.open_pdf(td)
            self.assertEqual(len(doc), 5)                                                # 여러 쪽 TIFF 는 쪽마다
            im, desc = pdfio.page_image(doc, 1)
            self.assertIn("내장 스캔", desc); self.assertIsNone(ImageChops.difference(im.convert("L"), a).getbbox())   # 픽셀까지 같다
            self.assertAlmostEqual(doc[0].rect.width, 600 * 72 / 400, places=1)       # 쪽 크기는 이미지의 dpi 로
            raw = open(os.path.join(td, "p2.jpg"), "rb").read()
            self.assertEqual(doc.extract_image(doc[1].get_images(full=True)[0][0])["image"], raw)   # JPEG 는 다시 압축하지 않음
            self.assertEqual(pdfio.page_image(doc, 3)[0].getpixel((5, 5)), (255, 255, 255))     # 투명 → 흰 바탕
            self.assertEqual(pdfio.text_layer(doc, 1).char_count, 0)
            one = pdfio.open_pdf(os.path.join(td, "p2.jpg")); self.assertEqual(len(one), 1)
            out = pdfio.save_images_as_pdf([os.path.join(td, "p1.png"), os.path.join(td, "p2.jpg")], os.path.join(td, "b.pdf"))
            self.assertEqual(len(pdfio.open_pdf(out)), 2)
            empty = os.path.join(td, "empty"); os.makedirs(empty)
            with self.assertRaises(ValueError):
                pdfio.open_pdf(empty)

    def test_run_on_image_folder(self):
        import contextlib
        from yeone_ocr import cli
        with tempfile.TemporaryDirectory() as td:
            d = os.path.join(td, "scans"); os.makedirs(d); self._imgs(d)
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                rc = cli.main(["run", d, "--work", os.path.join(td, "w"), "--reader", "mock", "--checkpoint", "0"])
            self.assertEqual(rc, 0, output.getvalue())
            self.assertTrue(os.path.exists(os.path.join(td, "w", "out", "scans.hwpx")))
            self.assertEqual(len(os.listdir(os.path.join(td, "w", "readings"))), 5)

    def test_16bit_grayscale_keeps_ink(self):
        """16비트 회색조(PNG I;16·TIFF I;16B)는 위 8비트로: 255 에서 잘려 글자가 하얗게 사라지지 않는다."""
        import numpy as np
        from yeone_ocr import pdfio
        with tempfile.TemporaryDirectory() as td:
            g = np.tile(np.linspace(0, 65535, 200), (100, 1)).astype(np.uint16)
            Image.fromarray(g).save(os.path.join(td, "g16.png"))
            Image.frombytes("I;16B", (200, 100), g.astype(">u2").tobytes()).save(os.path.join(td, "g16b.tif"))
            ink = np.full((100, 200), 0xF000, np.uint16); ink[40:60, 50:150] = 0x1000; Image.fromarray(ink).save(os.path.join(td, "ink16.tif"))
            for n, mode in (("g16.png", "I;16"), ("g16b.tif", "I;16B")):
                with Image.open(os.path.join(td, n)) as im:
                    self.assertEqual(im.mode, mode)
            doc = pymupdf.open("pdf", pdfio.images_to_pdf([os.path.join(td, n) for n in ("g16.png", "g16b.tif", "ink16.tif")]))
            for k in (1, 2):
                self.assertTrue(100 < np.asarray(pdfio.page_image(doc, k)[0].convert("L")).mean() < 155)
            a = np.asarray(pdfio.page_image(doc, 3)[0].convert("L"))
            self.assertLess(a[40:60, 50:150].mean(), 40); self.assertGreater(a[:20].mean(), 220)      # 잉크는 어둡게, 바탕은 밝게

    def test_12bit_data_in_16bit_container(self):
        """16비트 그릇에 12비트 자료(0~4095)를 담은 스캔(마이크로필름·기록물 스캐너)도 실제로 쓰인 비트의 위 8비트로:
        바탕은 밝고 글자는 어둡다(늘 256 으로 나눠 쪽이 새까맣게 되지 않게). 32비트 정수(I)도 같다."""
        import numpy as np
        from yeone_ocr import pdfio
        with tempfile.TemporaryDirectory() as td:
            a = np.full((100, 200), 3800, np.uint16); a[40:60, 50:150] = 300                  # 바탕 3800, 글자 300 (12비트)
            Image.fromarray(a).save(os.path.join(td, "s12.tif"))
            Image.frombytes("I;16B", (200, 100), a.astype(">u2").tobytes()).save(os.path.join(td, "s12b.tif"))
            Image.fromarray(a.astype(np.int32)).save(os.path.join(td, "s12i.tif"))
            for n, mode in (("s12.tif", "I;16"), ("s12b.tif", "I;16B"), ("s12i.tif", "I")):
                with Image.open(os.path.join(td, n)) as im:
                    self.assertEqual(im.mode, mode)
            doc = pymupdf.open("pdf", pdfio.images_to_pdf([os.path.join(td, n) for n in ("s12.tif", "s12b.tif", "s12i.tif")]))
            for k in (1, 2, 3):
                g = np.asarray(pdfio.page_image(doc, k)[0].convert("L"))
                self.assertGreater(g[:20].mean(), 200); self.assertLess(g[40:60, 50:150].mean(), 40)

    def test_16bit_conversion_makes_no_float_copy(self):
        """16·32비트 회색조를 8비트로 바꿀 때 실수(float64) 임시 배열도, 쪽 전체의 정수 사본도 만들지 않는다: 큰 기록물 스캔 한 쪽에 화소당 몇 바이트씩 더 들지 않게.
        32비트 정수(I) TIFF 는 원본 파일만 화소당 4바이트다."""
        import numpy as np, tracemalloc
        from yeone_ocr import pdfio
        with tempfile.TemporaryDirectory() as td:
            w, h = 1500, 2000
            a = np.full((h, w), 3800, np.uint16); a[900:1100, 200:1300] = 300
            p16 = os.path.join(td, "big16.png"); Image.fromarray(a).save(p16)
            pi = os.path.join(td, "bigI.tif"); Image.fromarray(a.astype(np.int32)).save(pi); del a
            for p in (p16, pi):
                was = tracemalloc.is_tracing()
                if not was:
                    tracemalloc.start()
                try:
                    base = tracemalloc.get_traced_memory()[0]; tracemalloc.reset_peak()
                    pdfio.images_to_pdf([p]); peak = tracemalloc.get_traced_memory()[1] - base
                finally:
                    if not was:
                        tracemalloc.stop()
                self.assertLess(peak, 8 * w * h, p)   # 띠씩 하면 화소당 2~7바이트(32비트 TIFF 는 원본 파일 4바이트 포함), 쪽 전체를 numpy 로 받으면 32비트는 12바이트, float64 면 17바이트 넘게

    def test_mpo_jpeg_is_one_page_with_first_image_bytes(self):
        """MPF 로 미리보기가 붙은 JPEG(Pillow 는 MPO 로 연다)은 첫 그림 한 쪽만, 첫 그림의 원래 JPEG 바이트 그대로
        (뒤에 붙은 미리보기·HDR 이득 지도는 PDF 에 넣지 않고, page_image 도 그림 한 장)."""
        from yeone_ocr import pdfio
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "cam.jpg")
            Image.new("RGB", (300, 400), (200, 30, 30)).save(p, format="MPO", save_all=True, append_images=[Image.new("RGB", (150, 200), (30, 200, 30))])
            raw = Path(p).read_bytes()
            with Image.open(p) as im:
                self.assertEqual((im.format, im.n_frames), ("MPO", 2)); first = im.mpinfo[0xB002][0]["Size"]
            self.assertLess(first, len(raw))
            doc = pdfio.open_pdf(p)
            self.assertEqual(len(doc), 1)
            info = doc.extract_image(doc[0].get_images(full=True)[0][0])
            self.assertEqual((info["ext"], info["width"], info["height"]), ("jpeg", 300, 400))
            self.assertEqual(info["image"], raw[:first])                                    # 첫 그림만, 다시 압축하지 않고
            im = pdfio.page_image(doc, 1)[0]
            self.assertEqual((im.size, getattr(im, "n_frames", 1)), ((300, 400), 1))
            self.assertTrue(all(abs(x - y) < 12 for x, y in zip(im.getpixel((150, 200)), (200, 30, 30))))     # 첫 그림(빨강)이지 미리보기(초록)가 아니다

    def test_exif_orientation_stands_upright(self):
        """휴대전화 사진의 EXIF 방향: 쪽은 바로 선 크기. 순수 회전(3·6·8)은 원래 JPEG 바이트를 그대로 넣고 쪽 위에서 돌려 놓아
        PDF 가 원래 JPEG 만 하다(PNG 로 풀면 몇 배). page_image 는 위가 위인 비트맵이고 300dpi 렌더와 방향이 같다(page_frame 도 쪽 전체).
        뒤집힌 방향(2·5)은 바로 세운 PNG. 90도면 dpi 도 바뀐다."""
        import numpy as np
        from yeone_ocr import pdfio
        T = Image.Transpose
        up = Image.fromarray(np.random.default_rng(7).integers(150, 256, (400, 300, 3), dtype=np.uint8))   # 바로 선 모양 300x400: 위에 검은 띠, 왼쪽에 빨간 띠
        up.paste((0, 0, 0), (0, 0, 300, 40)); up.paste((255, 0, 0), (0, 40, 30, 400))
        stored = {3: up.transpose(T.ROTATE_180), 6: up.transpose(T.ROTATE_90), 8: up.transpose(T.ROTATE_270),   # 카메라가 적은 픽셀(누운 채)
                  2: up.transpose(T.FLIP_LEFT_RIGHT), 5: up.transpose(T.TRANSPOSE)}
        with tempfile.TemporaryDirectory() as td:
            for o, im in stored.items():
                ex = Image.Exif(); ex[0x0112] = o
                p = os.path.join(td, f"o{o}.jpg"); im.save(p, exif=ex.tobytes(), dpi=(300, 300), quality=92)
                data = pdfio.images_to_pdf([p]); doc = pymupdf.open("pdf", data)
                self.assertEqual((round(doc[0].rect.width, 1), round(doc[0].rect.height, 1)), (72.0, 96.0), o)     # 쪽은 바로 선 크기
                pim, desc = pdfio.page_image(doc, 1)
                self.assertEqual(pim.size, (300, 400), o)
                a = np.asarray(pim.convert("RGB")).astype(int)
                self.assertLess(a[:30, 40:].mean(), 30, o); self.assertGreater(a[50:, :20, 0].mean(), 200, o); self.assertLess(a[50:, :20, 1].mean(), 60, o)  # 위가 위, 왼쪽이 왼쪽
                pix = doc[0].get_pixmap(dpi=300); r = np.asarray(Image.frombytes("RGB", (pix.width, pix.height), pix.samples)).astype(int)
                diff = lambda x: float(np.abs(np.asarray(x.convert("RGB")).astype(int) - r).mean())
                self.assertLess(diff(pim), 10, o)                                                  # 화면에 보이는 쪽(렌더)과 같은 방향
                for t in (T.ROTATE_180, T.FLIP_LEFT_RIGHT, T.FLIP_TOP_BOTTOM):
                    self.assertGreater(diff(pim.transpose(t)), 25, o)
                self.assertEqual(tuple(round(v, 1) for v in pdfio.page_frame(doc, 1)), (0.0, 0.0, 72.0, 96.0), o)
                stream = doc.extract_image(doc[0].get_images(full=True)[0][0])["image"]
                if o in (3, 6, 8):
                    self.assertEqual(stream, Path(p).read_bytes(), o); self.assertIn("jpeg", desc)      # 다시 압축하지 않는다
                    self.assertLess(len(data), os.path.getsize(p) * 1.1 + 4096, o)                    # PDF 가 원래 JPEG 만 하다
                else:
                    self.assertIn("png", desc)
            up2 = Image.new("RGB", (300, 400), (255, 255, 255)); up2.paste((0, 0, 0), (0, 0, 300, 40))
            ex = Image.Exif(); ex[0x0112] = 6
            p = os.path.join(td, "phone.jpg"); up2.transpose(T.ROTATE_90).save(p, exif=ex.tobytes(), dpi=(300, 330))
            doc = pdfio.open_pdf(p)
            self.assertAlmostEqual(doc[0].rect.width, 300 * 72 / 330, places=1); self.assertAlmostEqual(doc[0].rect.height, 400 * 72 / 300, places=1)
            a = np.asarray(pdfio.page_image(doc, 1)[0].convert("L"))
            self.assertLess(a[:20].mean(), 30); self.assertGreater(a[-20:].mean(), 225)

    def test_upright_page_image_drops_orientation_tag(self):
        """쪽 위에서 돌려 놓인 사진을 바로 세워 꺼내면 EXIF 방향 태그는 지우고 dpi 도 바로 선 대로 바꾼다
        (받는 쪽이 흔히 쓰는 ImageOps.exif_transpose 나 exif=img.info['exif'] 저장으로 한 번 더 돌아 눕지 않게)."""
        import numpy as np
        from PIL import ImageOps
        from yeone_ocr import pdfio
        up = Image.new("RGB", (300, 400), (255, 255, 255)); up.paste((0, 0, 0), (0, 0, 300, 40))
        with tempfile.TemporaryDirectory() as td:
            for o, t in ((6, Image.Transpose.ROTATE_90), (8, Image.Transpose.ROTATE_270), (3, Image.Transpose.ROTATE_180)):
                ex = Image.Exif(); ex[0x0112] = o
                p = os.path.join(td, f"o{o}.jpg"); up.transpose(t).save(p, exif=ex.tobytes(), dpi=(300, 330) if o != 3 else (300, 300))
                im, desc = pdfio.page_image(pdfio.open_pdf(p), 1)
                self.assertIn("돌려 놓임", desc); self.assertEqual(im.size, (300, 400), o)
                self.assertIsNone(im.getexif().get(0x0112), o); self.assertNotIn("exif", im.info)
                if o != 3:
                    self.assertEqual(tuple(round(float(v)) for v in im.info["dpi"]), (330, 300))     # 누운 채의 (300, 330) → 바로 선 (330, 300)
                a = np.asarray(ImageOps.exif_transpose(im).convert("L"))
                self.assertEqual(a.shape, (400, 300), o); self.assertLess(a[:20].mean(), 30, o); self.assertGreater(a[-20:].mean(), 225, o)

    def test_page_rotate_matches_render(self):
        """쪽의 /Rotate 도 방향에 넣는다: '/Rotate 180 만'(Acrobat 의 쪽 돌리기로 바로 세운 거꾸로 된 스캔)과 '180도로 놓고 /Rotate 180' 은
        내장 비트맵을 화면(300dpi 렌더)과 같은 방향으로 꺼내고, /Rotate 90·270 인 쪽은 렌더한다. page_frame 은 page_image 가 간 길의 틀(쪽에 보이는 좌표)."""
        import numpy as np
        from yeone_ocr import pdfio
        up = Image.new("RGB", (300, 400), (255, 255, 255)); up.paste((0, 0, 0), (0, 0, 300, 40)); up.paste((255, 0, 0), (0, 40, 30, 400))   # 위에 검은 띠, 왼쪽에 빨간 띠
        buf = io.BytesIO(); up.save(buf, "PNG")
        for cm, rot, w, h in ((0, 180, 72, 96), (180, 180, 72, 96), (0, 90, 96, 96), (90, 270, 96, 96)):    # 90·270 은 정사각 쪽(판면 덮음 검사에 걸리지 않게)
            d = pymupdf.open(); pg = d.new_page(width=w, height=h)
            pg.insert_image(pymupdf.Rect(2, 4, w, h), stream=buf.getvalue(), rotate=cm, keep_proportion=False); pg.set_rotation(rot)
            doc = pymupdf.open("pdf", d.tobytes())
            fr = (0.0, 0.0, 70.0, 92.0) if rot == 180 else (0.0, 0.0, float(w), float(h))       # 180: 놓인 (2,4)~ 가 돌아 오른쪽 아래로 붙는다. 90·270: 렌더라 쪽 전체
            im, desc = pdfio.page_image(doc, 1)
            self.assertIn("내장 스캔" if rot == 180 else "렌더", desc)
            pix = doc[0].get_pixmap(dpi=300); k = 300 / 72
            r = np.asarray(Image.frombytes("RGB", (pix.width, pix.height), pix.samples)).astype(int)
            r = r[round(fr[1] * k):round((fr[1] + fr[3]) * k), round(fr[0] * k):round((fr[0] + fr[2]) * k)]          # 틀 안의 렌더
            diff = lambda x: float(np.abs(np.asarray(x.convert("RGB").resize((r.shape[1], r.shape[0]))).astype(int) - r).mean())
            self.assertLess(diff(im), 10, (cm, rot, desc))
            for t in (Image.Transpose.ROTATE_180, Image.Transpose.ROTATE_90, Image.Transpose.ROTATE_270):
                self.assertGreater(diff(im.transpose(t)), 25, (cm, rot, t))
            self.assertEqual(tuple(round(v, 1) for v in pdfio.page_frame(doc, 1)), fr, (cm, rot))       # 틀도 page_image 가 간 길대로(쪽에 보이는 좌표)


# ---------------- 0.6.5: 이미지 여러 장 묶기(창) ----------------
class _Var:
    def __init__(self): self.seen = []
    def set(self, v): self.seen.append(v)
    def get(self): return self.seen[-1] if self.seen else ""


class BundleImages(unittest.TestCase):
    """창에서 이미지 여러 장을 고르면: 고른 목록의 지문으로 묶음을 가리고, 묶기는 작업 스레드에서(그동안 단추 잠금)."""
    def _app(self):
        try:
            from yeone_ocr import gui
        except Exception as e:
            self.skipTest(f"gui 를 불러오지 못함: {e}")
        import queue, types
        app = gui.App.__new__(gui.App)                     # 창을 띄우지 않고 묶기 논리만
        app.q = queue.Queue(); app.running = False; app.log = []; app.used = []
        app._append = app.log.append; app._use_pdf = app.used.append
        app._set_running = lambda on, command="": setattr(app, "running", on)
        app.v_status = _Var(); app.v_prog = _Var(); app._bar_cap = 1
        app.progress = types.SimpleNamespace(configure=lambda **k: None, winfo_width=lambda: 100)
        app.b_stop = types.SimpleNamespace(configure=lambda **k: None); app.b_stop2 = None
        return gui, app

    def _drain(self, app):
        while app.running:
            item = app.q.get(timeout=30); self.assertEqual(item[0], "bundle"); app._bundle_msg(*item[1:])

    def _bundle(self, app, td, names):
        n = len(app.used); app._bundle_images([os.path.join(td, x) for x in names]); self._drain(app)
        self.assertEqual(len(app.used), n + 1)
        return app.used[-1]

    @staticmethod
    def _levels(pdf):
        import numpy as np
        from yeone_ocr import pdfio
        doc = pdfio.open_pdf(pdf)
        return [round(float(np.asarray(pdfio.page_image(doc, k)[0].convert("L")).mean())) for k in range(1, len(doc) + 1)]

    def test_fingerprint_picks_exact_selection(self):
        gui, app = self._app()
        with tempfile.TemporaryDirectory() as td:
            for i, v in enumerate((10, 20, 30, 40, 50), 1):
                Image.new("L", (40, 60), v).save(os.path.join(td, f"p{i:02d}.png"))
            a = self._bundle(app, td, ["p05.png", "p01.png", "p02.png"])
            self.assertEqual(self._levels(a), [10, 20, 50])
            b = self._bundle(app, td, ["p01.png", "p04.png", "p05.png"])          # 첫·끝 이름과 장 수가 같아도 가운데가 다르면 다른 묶음
            self.assertNotEqual(a, b); self.assertEqual(self._levels(b), [10, 40, 50])
            self.assertNotEqual(gui.work_dir_for(a), gui.work_dir_for(b))           # 결과 폴더(판독 기록)도 따로
            t0 = os.stat(a).st_mtime_ns
            self.assertEqual(self._bundle(app, td, ["p01.png", "p02.png", "p05.png"]), a)   # 정확히 같은 선택이면 그대로 다시 씀
            self.assertEqual(os.stat(a).st_mtime_ns, t0); self.assertIn("다시 씀", app.log[-1])
            self.assertIn("고른 이미지 확인 중(같은 묶음이 있는지)… 3/3장", app.v_status.seen)   # 지문을 읽는 동안에도 진행을 보인다
            Image.new("L", (40, 60), 99).save(os.path.join(td, "p02.png"))         # 바꿔 넣은 이미지의 수정 시각이 묶음보다 옛것이어도
            old = os.stat(a).st_mtime - 86400; os.utime(os.path.join(td, "p02.png"), (old, old))
            c = self._bundle(app, td, ["p01.png", "p02.png", "p05.png"])
            self.assertNotEqual(c, a); self.assertEqual(self._levels(c), [10, 99, 50])
            self.assertFalse([f for f in os.listdir(td) if f.endswith(".part")])

    def test_fingerprint_survives_moving_folder(self):
        """지문은 이름·크기·앞뒤 내용으로만: 폴더를 옮기거나 복사해 경로·수정 시각이 바뀌어도(FAT·클라우드 동기화) 같은 묶음을 다시 쓴다
        (묶음과 결과 폴더를 함께 옮기면 판독 기록이 이어진다)."""
        import shutil
        import numpy as np
        gui, app = self._app()
        with tempfile.TemporaryDirectory() as td:
            src = os.path.join(td, "a"); os.makedirs(src)
            for i, v in enumerate((10, 20), 1):
                Image.new("L", (40, 60), v).save(os.path.join(src, f"p{i:02d}.png"))
            Image.fromarray(np.random.default_rng(3).integers(0, 256, (400, 400), dtype=np.uint8)).save(os.path.join(src, "p03.png"))   # 128KiB 넘는 장
            self.assertGreater(os.path.getsize(os.path.join(src, "p03.png")), 2 * 65536)
            a = self._bundle(app, src, ["p01.png", "p02.png", "p03.png"])
            dst = os.path.join(td, "b", "moved"); shutil.copytree(src, dst)                     # 묶음째 다른 곳으로
            for f in os.listdir(dst):
                os.utime(os.path.join(dst, f), (1_000_000_000, 1_000_000_000))                  # 수정 시각도 달라짐
            b = self._bundle(app, dst, ["p03.png", "p01.png", "p02.png"])
            self.assertEqual(Path(b).name, Path(a).name); self.assertEqual(Path(b).parent, Path(dst))
            self.assertIn("다시 씀", app.log[-1])
            Image.fromarray(np.random.default_rng(4).integers(0, 256, (400, 400), dtype=np.uint8)).save(os.path.join(dst, "p03.png"))   # 내용이 다르면 다른 묶음
            self.assertNotEqual(Path(self._bundle(app, dst, ["p01.png", "p02.png", "p03.png"])).name, Path(a).name)

    def test_fingerprint_sees_same_size_middle_edit(self):
        """무압축 이미지(BMP·TIFF)의 가운데만 고쳐 크기가 같아도 다른 묶음(옛 묶음·판독 기록을 몰래 다시 쓰지 않게):
        8MiB 이하는 파일 전체를, 넘으면 앞·가운데·끝을 본다."""
        import numpy as np
        gui, _ = self._app()
        with tempfile.TemporaryDirectory() as td:
            for shape in ((1200, 900), (3000, 3000)):                                          # 약 1MB(전체), 약 9MB(앞·가운데·끝 64KiB)
                a = np.random.default_rng(1).integers(200, 256, shape, dtype=np.uint8)
                ps = [Path(td, f"p{i}.bmp") for i in (1, 2)]
                for p in ps:
                    Image.fromarray(a).save(p)
                before, n = gui.App._bundle_path(ps).name, os.path.getsize(ps[1])
                y = shape[0] // 2; a[y - 50:y + 50, 300:600] = 0; Image.fromarray(a).save(ps[1])   # p2 의 가운데를 고쳐 같은 크기로 다시 저장
                self.assertEqual(os.path.getsize(ps[1]), n)
                self.assertNotEqual(gui.App._bundle_path(ps).name, before, shape)

    def test_hint_for_bundle_from_older_version(self):
        """예전 판의 지문 없는 묶음 '<첫>~<끝> (N장).pdf' 과 그 결과 폴더(판독 기록)가 옆에 있으면, 새로 묶으면서 그 판독 기록은 그 결과 폴더에 있다고
        한 줄 알린다(단추 이름은 실제 단추 그대로). 묶기만 하고 판독하지 않아 결과 폴더가 없으면 있지도 않은 기록을 찾게 하지 않는다."""
        from yeone_ocr import pdfio
        gui, app = self._app()
        with tempfile.TemporaryDirectory() as td:
            for i in (1, 2, 5):
                Image.new("L", (40, 60), 30 * i).save(os.path.join(td, f"p{i:02d}.png"))
            old = pdfio.save_images_as_pdf([os.path.join(td, f"p{i:02d}.png") for i in (1, 2, 5)], os.path.join(td, "p01~p05 (3장).pdf"))
            hints = lambda: [l for l in "".join(app.log).splitlines() if "p01~p05 (3장).pdf" in l]
            a = self._bundle(app, td, ["p01.png", "p02.png", "p05.png"])
            self.assertNotEqual(a, old); self.assertEqual(hints(), [])                              # 결과 폴더가 없으면 알리지 않는다
            os.remove(a); os.makedirs(gui.work_dir_for(old) / "readings")                           # 예전 판으로 판독한 기록이 있으면
            self.assertEqual(self._bundle(app, td, ["p01.png", "p02.png", "p05.png"]), a)
            self.assertEqual(len(hints()), 1); self.assertIn("p01~p05 (3장)_YeonE_OCR", hints()[0])
            self.assertIn("[일반 OCR]의 [PDF 고르기…]", hints()[0])
            self.assertEqual(self._bundle(app, td, ["p01.png", "p02.png", "p05.png"]), a)         # 다시 쓸 때는 되풀이하지 않는다
            self.assertEqual(len(hints()), 1)

    def test_bundling_runs_off_main_thread(self):
        import threading
        from unittest import mock
        from yeone_ocr import pdfio
        gui, app = self._app()
        gate, seen, real = threading.Event(), {}, pdfio.save_images_as_pdf
        def slow(srcs, out, progress=None):
            seen["thread"] = threading.current_thread(); gate.wait(10); return real(srcs, out, progress=progress)
        with tempfile.TemporaryDirectory() as td:
            for i in (1, 2, 3):
                Image.new("L", (40, 60), 50 * i).save(os.path.join(td, f"s{i}.png"))
            with mock.patch.object(pdfio, "save_images_as_pdf", slow):
                app._bundle_images([os.path.join(td, f"s{i}.png") for i in (1, 2, 3)])
                self.assertTrue(app.running); self.assertEqual(app.used, [])         # 곧바로 돌아오고, 묶는 동안 단추는 잠겨 있다
                gate.set(); self._drain(app)
            self.assertIsNot(seen["thread"], threading.main_thread())
            self.assertFalse(app.running); self.assertEqual(len(app.used), 1); self.assertTrue(os.path.exists(app.used[0]))
            self.assertIn("이미지를 한 권으로 묶는 중… 3/3장", app.v_status.seen)          # 장마다 진행을 알린다
            # 묶다 실패하면 반쯤 쓴 파일을 남기지 않는다(다음에 그것을 '같은 묶음' 으로 다시 쓰지 않게)
            def broken(srcs, out, progress=None):
                Path(out).write_bytes(b"%PDF-"); raise OSError("디스크가 가득 참")
            Image.new("L", (40, 60), 7).save(os.path.join(td, "s4.png"))
            with mock.patch.object(pdfio, "save_images_as_pdf", broken), mock.patch.object(gui, "messagebox") as mb:
                app._bundle_images([os.path.join(td, f"s{i}.png") for i in (1, 4)]); self._drain(app)
            self.assertTrue(mb.showerror.called); self.assertEqual(len(app.used), 1); self.assertFalse(app.running)
            self.assertEqual(sorted(f for f in os.listdir(td) if not f.endswith(".png")), [Path(app.used[0]).name])


# ---------------- 0.6.8: 깨지지 않는 기록 · 실패 쪽 표시 · 미리 준비 · 부분 기록 · 묶음 나누기 ----------------
@contextlib.contextmanager
def patched_mock_reader(**hooks):
    """cmd_read 가 만드는 MockReader 에 검사용 갈고리를 끼우고, 만든 판독기들을 돌려준다."""
    from yeone_ocr import cli
    orig = cli.make_reader; made = []
    def mk(args, doc, work):
        r = orig(args, doc, work)
        for k, v in hooks.items():
            setattr(r, k, v)
        made.append(r); return r
    cli.make_reader = mk
    try:
        yield made
    finally:
        cli.make_reader = orig


def _join_prepare_ahead():
    import threading
    for t in threading.enumerate():
        if t.name == "prepare-ahead":
            t.join(30)


class AtomicState(unittest.TestCase):
    """기록·상태는 임시 파일에 다 쓴 뒤 바꿔 넣고(도중에 죽어도 쓰다 만 파일이 남지 않음), 깨진 JSON 은 치우고 계속한다."""
    def test_write_is_atomic_and_leaves_no_partial_file(self):
        from yeone_ocr import state as S
        with tempfile.TemporaryDirectory() as td:
            work = Work(os.path.join(td, "a.pdf"), os.path.join(td, "w"))
            work.save_record(1, {"pdf_page": 1, "x": "처음"})
            def half(t):                                                           # 쓰다가 죽는 흉내
                t.write_text('{"pdf_page": 1, "x": "반', encoding="utf-8"); raise OSError("죽음")
            with self.assertRaises(OSError):
                S.write_atomic_with(work.record_path(1), half)
            self.assertEqual(work.load_record(1)["x"], "처음")                         # 원래 기록 그대로
            self.assertEqual([p.name for p in work.readings_dir.iterdir()], ["p001.json"])   # 임시 파일이 남지 않는다
            work.save_state({"file": "a.pdf", "pages": {"1": {"statuses": ["미처리"], "notes": []}}})
            self.assertEqual([p.name for p in work.dir.iterdir() if p.is_file()], ["state.json"])
            self.assertEqual(work.load_state()["pages"]["1"]["statuses"], ["미처리"])

    def test_corrupt_record_is_moved_and_page_counts_as_unread(self):
        from yeone_ocr import state as S
        warns = []; old = S.WARN; S.WARN = warns.append
        try:
            with tempfile.TemporaryDirectory() as td:
                work = Work(os.path.join(td, "a.pdf"), os.path.join(td, "w"))
                work.save_record(1, {"pdf_page": 1}); work.save_record(2, {"pdf_page": 2})
                work.record_path(2).write_text('{"pdf_page": 2, "merged": {', encoding="utf-8")   # 쓰다 만 파일
                self.assertIsNone(work.load_record(2))
                self.assertFalse(work.record_path(2).exists()); self.assertTrue((work.readings_dir / "broken" / "p002.json").exists())
                self.assertEqual([r["pdf_page"] for r in work.records()], [1])
                self.assertEqual(len(warns), 1); self.assertTrue(warns[0].startswith("주의:"), warns)
                work.record_path(3).write_text("", encoding="utf-8")                            # 빈 파일도
                self.assertEqual([r["pdf_page"] for r in work.records()], [1]); self.assertTrue((work.readings_dir / "broken" / "p003.json").exists())
        finally:
            S.WARN = old

    def test_corrupt_state_is_renamed_and_restarted(self):
        from yeone_ocr import state as S
        warns = []; old = S.WARN; S.WARN = warns.append
        try:
            with tempfile.TemporaryDirectory() as td:
                work = Work(os.path.join(td, "a.pdf"), os.path.join(td, "w"))
                work.set_status(1, "1차 OCR 완료")
                work.state_path.write_text('{"file": "a.pdf", "pages": {"1": ', encoding="utf-8")
                self.assertEqual(work.load_state(), {"file": "a.pdf", "pages": {}})
                self.assertTrue(work.state_path.with_name("state.json.broken").exists()); self.assertFalse(work.state_path.exists())
                self.assertEqual(len(warns), 1); self.assertIn("state.json.broken", warns[0])
                work.set_status(2, "1차 OCR 완료")                                             # 계속 쓸 수 있다
                self.assertEqual(work.statuses(2), ["1차 OCR 완료"])
        finally:
            S.WARN = old

    def test_ensure_pages_and_backup_record(self):
        with tempfile.TemporaryDirectory() as td:
            work = Work(os.path.join(td, "a.pdf"), os.path.join(td, "w"))
            work.set_status(2, "1차 OCR 완료")
            work.ensure_pages([1, 2, 3], total_pages=5)
            st = work.load_state()
            self.assertEqual(sorted(st["pages"]), ["1", "2", "3"]); self.assertEqual(st["total_pages"], 5)
            self.assertEqual(st["pages"]["2"]["statuses"], ["1차 OCR 완료"])                 # 있던 것은 그대로
            self.assertIsNone(work.backup_record(1))                                           # 기록이 없으면 None
            work.save_record(1, {"pdf_page": 1, "merged": "손으로 고침"})
            self.assertEqual(work.backup_record(1), work.readings_dir / "prev" / "p001.json")
            work.save_record(1, {"pdf_page": 1, "merged": "다시 계산"})
            second = work.backup_record(1)                                                     # 내용이 다르면 앞의 사본을 덮지 않고 하나 더
            self.assertNotEqual(second.name, "p001.json"); self.assertEqual(json.loads(second.read_text(encoding="utf-8"))["merged"], "다시 계산")
            self.assertEqual(json.loads((work.readings_dir / "prev" / "p001.json").read_text(encoding="utf-8"))["merged"], "손으로 고침")
            self.assertEqual([r["pdf_page"] for r in work.records()], [1])                      # prev/·broken/ 은 기록으로 세지 않는다


class FailedPages(unittest.TestCase):
    """일시 오류로 실패한 쪽이 있으면 read 는 3, run 은 문서까지 만들되 3 을 돌려준다(창이 '완료' 로 보이지 않게). 다시 실행하면 그 쪽만 다시."""
    def test_read_and_run_return_3_when_a_page_fails(self):
        from yeone_ocr import cli
        with tempfile.TemporaryDirectory() as td:
            doc = vertical_pdf(3); pdf = os.path.join(td, "v.pdf"); doc.save(pdf); doc.close()
            with patched_mock_reader(fail_pages={2}):
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    rc = cli.main(["read", pdf, "--work", os.path.join(td, "w"), "--reader", "mock"])
                self.assertEqual(rc, 3)
                work = Work(pdf, os.path.join(td, "w"))
                self.assertEqual([r["pdf_page"] for r in work.records()], [1, 3]); self.assertIn("판독 확인 필요", work.statuses(2))
                self.assertTrue(any(l.startswith("오류: 판독에 실패한 쪽 1개: 2") for l in buf.getvalue().splitlines()), buf.getvalue())
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    rc = cli.main(["run", pdf, "--work", os.path.join(td, "w2"), "--reader", "mock", "--checkpoint", "0"])
                _join_prepare_ahead()
                self.assertEqual(rc, 3)                                                        # 문서·검수 화면·보고는 만들고
                self.assertEqual([p.name for p in Path(td, "w2", "out").glob("*.hwpx")], ["v_p001-003.hwpx"]); self.assertTrue(os.path.exists(os.path.join(td, "w2", "review.html")))   # 기록 있는 쪽만 문서에
                self.assertIn("오류: 판독에 실패한 쪽 1개: 2", buf.getvalue()); self.assertIn("실패한 쪽이 있습니다", buf.getvalue())
            with patched_mock_reader() as made:                                                # 다시 실행: 실패한 쪽만 다시 → 0
                with contextlib.redirect_stdout(io.StringIO()):
                    rc = cli.main(["run", pdf, "--work", os.path.join(td, "w2"), "--reader", "mock", "--checkpoint", "0"])
                _join_prepare_ahead()
            self.assertEqual(rc, 0); self.assertEqual(made[0].calls["read_page"], 1)
            self.assertEqual([r["pdf_page"] for r in Work(pdf, os.path.join(td, "w2")).records()], [1, 2, 3])


class PrepareAhead(unittest.TestCase):
    """한 번에 인식은 쪽 파일을 전부 꺼낸 뒤가 아니라 판독과 나란히 미리 만든다. 끝나면 모든 쪽의 이미지·글자층 파일과 기록이 있고 임시 파일은 없다."""
    def test_run_prepares_all_pages_alongside_reading(self):
        from yeone_ocr import cli
        events = []
        with tempfile.TemporaryDirectory() as td:
            doc = vertical_pdf(4); pdf = os.path.join(td, "v.pdf"); doc.save(pdf); doc.close()
            w = os.path.join(td, "w")
            cli.PROGRESS = lambda st, d, t: events.append((st, d, t))
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    rc = cli.main(["run", pdf, "--work", w, "--reader", "mock", "--checkpoint", "0"])
            finally:
                cli.PROGRESS = None
            _join_prepare_ahead()
            self.assertEqual(rc, 0)
            work = Work(pdf, w)
            self.assertEqual([r["pdf_page"] for r in work.records()], [1, 2, 3, 4])
            for n in range(1, 5):
                self.assertTrue(work.page_image_path(n).exists(), n); self.assertTrue(work.textlayer_path(n).exists(), n)
            self.assertEqual(sorted(p.name for p in work.pages_dir.iterdir()), sorted(f"p{n:03d}.{e}" for n in range(1, 5) for e in ("png", "src.txt")))
            self.assertEqual(sorted(p.name for p in work.textlayer_dir.iterdir()), [f"p{n:03d}.json" for n in range(1, 5)])
            self.assertEqual([e for e in events if e[0] == "prepare"], [("prepare", 0, 0)])    # 기록 없는 쪽은 앞서 꺼내지 않는다(미리 준비 스레드는 알리지 않음)
            st = work.load_state(); self.assertEqual(st["total_pages"], 4); self.assertEqual(sorted(st["pages"]), ["1", "2", "3", "4"])
            events.clear(); cli.PROGRESS = lambda st, d, t: events.append((st, d, t))        # 기록 있는 쪽은 앞서(동기) 준비한다 — 이전 판 기록 갱신에 글자층이 필요
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    rc = cli.main(["run", pdf, "--work", w, "--reader", "mock", "--checkpoint", "0", "--pages", "1-2"])
            finally:
                cli.PROGRESS = None
            _join_prepare_ahead()
            self.assertEqual(rc, 0); self.assertEqual([e for e in events if e[0] == "prepare"], [("prepare", 0, 2), ("prepare", 1, 2), ("prepare", 2, 2)])

    def test_quiet_prepare_is_silent_and_stops_on_request(self):
        import threading
        from yeone_ocr import cli
        events = []
        with tempfile.TemporaryDirectory() as td:
            doc = vertical_pdf(4); pdf = os.path.join(td, "v.pdf"); doc.save(pdf)
            work = Work(pdf, os.path.join(td, "w")); args = Namespace(pdf=pdf, work=None, pages=None, dpi=72, force=False)
            cli.PROGRESS = lambda st, d, t: events.append((st, d, t))
            try:
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    cli.cmd_prepare(args, work, doc, pages=[1, 2], quiet=True)
            finally:
                cli.PROGRESS = None
            self.assertEqual(events, []); self.assertEqual(buf.getvalue(), "")
            self.assertTrue(work.page_image_path(2).exists()); self.assertFalse(work.page_image_path(3).exists())
            stop = threading.Event(); stop.set()
            cli.cmd_prepare(args, work, doc, pages=[3, 4], quiet=True, stop=stop)
            self.assertFalse(work.page_image_path(3).exists())
            self.assertEqual(sorted(work.load_state()["pages"]), ["1", "2", "3", "4"])      # 등록은 한다
            cli.PROGRESS = lambda st, d, t: events.append((st, d, t))                      # 단독 prepare 는 전처럼 알린다
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    cli.cmd_prepare(args, work, doc, pages=[3])
            finally:
                cli.PROGRESS = None
            self.assertEqual(events, [("prepare", 0, 1), ("prepare", 1, 1)]); self.assertTrue(work.page_image_path(3).exists())
            self.assertTrue((work.pages_dir / "p003.src.txt").read_text(encoding="utf-8").startswith("렌더"))
            doc.close()


def _pdf_with_figure(n: int, path: str):
    """세로쓰기 글자층 + 1쪽에 큰 사각 도형(mock 판독기가 도판으로 잡는다)."""
    doc = vertical_pdf(n)
    doc[0].draw_rect(pymupdf.Rect(80, 250, 380, 650), color=(0, 0, 0), width=5)
    doc.save(path); doc.close()


class InterimCheckpoint(unittest.TestCase):
    """중간 저장(interim)은 도판은 잘라도 기록 파일은 손대지 않는다 — 다시 읽는 중인 쪽의 새 기록을 옛 기록으로 덮지 않게."""
    def test_interim_build_leaves_records_byte_identical(self):
        from yeone_ocr import cli
        with tempfile.TemporaryDirectory() as td:
            pdf = os.path.join(td, "v.pdf"); _pdf_with_figure(2, pdf); w = os.path.join(td, "w")
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(cli.main(["run", pdf, "--work", w, "--reader", "mock", "--checkpoint", "0"]), 0)
            _join_prepare_ahead()
            work = Work(pdf, w); rec = work.load_record(1)
            self.assertTrue(rec["crops"], "1쪽의 도형이 도판으로 잡혀야 한다")
            for c in rec["crops"].values():
                os.remove(c["path"])
            rec["crops"] = {}; work.save_record(1, rec); before = work.record_path(1).read_bytes()
            args = cli.build_parser().parse_args(["run", pdf, "--work", w, "--reader", "mock"]); doc = pymupdf.open(pdf)
            with contextlib.redirect_stdout(io.StringIO()):
                cli.build_outputs(work, doc, work.records([1]), args, interim=True)
            self.assertEqual(work.record_path(1).read_bytes(), before)                       # 기록은 그대로
            self.assertTrue(list(work.crops_dir.glob("p001_*.jpg"))); self.assertTrue((work.out_dir / "v_진행중.hwpx").exists())   # 도판·진행중 문서는 만든다
            with contextlib.redirect_stdout(io.StringIO()):
                cli.build_outputs(work, doc, work.records([1]), args, interim=False)
            self.assertTrue(work.load_record(1)["crops"])                                     # 최종 문서 때 적는다
            doc.close()


class PrevBackup(unittest.TestCase):
    """이전 판 기록을 다시 비교하기 전에 readings/prev/ 에 한 번 복사해 둔다 — 손으로 고친 merged 가 사라지지 않게."""
    def test_refresh_backs_up_before_rewriting(self):
        from yeone_ocr.cli import refresh_stale_records
        p1 = PageReading(pdf_page=4, regions=[Region(id="b", kind="본문", paragraphs=[Paragraph(columns=[Column(col_from_right=1, text="剋하니 冲和가 되고")])])])
        rec = {"pdf_page": 4, "version": "0.5.11", "pass1": p1.model_dump(), "pass2": {}, "merged": {**p1.model_dump(), "page_note": "손으로 고침"}}
        with tempfile.TemporaryDirectory() as td:
            work = Work(os.path.join(td, "a.pdf"), os.path.join(td, "w"))
            work.save_record(4, rec); before = work.record_path(4).read_bytes()
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                self.assertEqual(refresh_stale_records(work, None, [4]), 1)
            self.assertEqual((work.readings_dir / "prev" / "p004.json").read_bytes(), before)
            self.assertIn("readings/prev/", buf.getvalue()); self.assertIn("1쪽 복사", buf.getvalue())
            self.assertNotEqual(work.load_record(4)["merged"].get("page_note"), "손으로 고침")   # merged 는 다시 계산된다(그래서 사본이 필요)


class PartialRecord(unittest.TestCase):
    """2차 판독 도중 오류가 나도 이미 산 1차 결과는 readings/partial/ 에 남고, 다음 실행은 1차를 다시 사지 않는다. 조각 하나의 일시 오류는 그 영역만 2차 없이."""
    def test_pass1_is_reused_after_pass2_failure(self):
        from yeone_ocr import cli
        with tempfile.TemporaryDirectory() as td:
            doc = vertical_pdf(3); pdf = os.path.join(td, "v.pdf"); doc.save(pdf); doc.close(); w = os.path.join(td, "w")
            work = Work(pdf, w); pp = work.readings_dir / "partial" / "p002.json"
            with patched_mock_reader(pass2_fail_once={2: RuntimeError("2차 중 예외")}) as made:
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    rc = cli.main(["read", pdf, "--work", w, "--reader", "mock"])
            self.assertEqual(rc, 3); self.assertIsNone(work.load_record(2)); self.assertEqual(made[0].calls["read_page"], 3)
            self.assertTrue(pp.exists()); part = json.loads(pp.read_text(encoding="utf-8"))
            self.assertEqual((part["pdf_page"], part["model"], part["mode"]), (2, "textlayer+cv", "vertical")); self.assertTrue(part["pass1"]["regions"])
            self.assertIn("readings/partial/", buf.getvalue())
            with patched_mock_reader() as made:                                              # 다음 실행: 2쪽의 1차는 다시 사지 않는다
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    rc = cli.main(["read", pdf, "--work", w, "--reader", "mock"])
            self.assertEqual(rc, 0); self.assertEqual(made[0].calls["read_page"], 0); self.assertGreater(made[0].calls["reread_region"], 0)
            rec = work.load_record(2); self.assertTrue(rec["pass1"]["regions"]); self.assertIn("pass1_reused_from", rec)
            self.assertFalse(pp.exists()); self.assertIn("2쪽: 이전 실행의 1차 판독을 재사용(2차만 다시)", buf.getvalue())
            # 모델·판·모드가 다른 부분 기록은 쓰지 않는다
            os.remove(work.record_path(2)); pp.parent.mkdir(exist_ok=True)
            pp.write_text(json.dumps({**part, "model": "다른 모델"}), encoding="utf-8")
            with patched_mock_reader() as made:
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(cli.main(["read", pdf, "--work", w, "--reader", "mock"]), 0)
            self.assertEqual(made[0].calls["read_page"], 1)

    def test_every_region_failing_fails_the_page_and_keeps_pass1(self):
        """mock 의 쪽은 문자 영역이 b1 하나 — 그 조각의 일시 오류는 '모든 영역 2차 실패' 다. 1차만인 쪽을 완료로 저장하지 않고 실패로 세며(3),
        1차는 partial/ 에 남아 다음 실행이 2차만 다시 한다(0.6.7 처럼 다시 시도되되 1차는 다시 사지 않음)."""
        from yeone_ocr import cli
        from yeone_ocr.reader_gemini import ReaderError
        with tempfile.TemporaryDirectory() as td:
            doc = vertical_pdf(2); pdf = os.path.join(td, "v.pdf"); doc.save(pdf); doc.close(); w = os.path.join(td, "w")
            with patched_mock_reader(pass2_fail_once={2: ReaderError("5회 시도 뒤에도 실패: 서버 오류(503)", fatal=False)}):
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    rc = cli.main(["read", pdf, "--work", w, "--reader", "mock"])
            self.assertEqual(rc, 3)
            work = Work(pdf, w); self.assertIsNone(work.load_record(2)); self.assertIn("판독 확인 필요", work.statuses(2))
            self.assertIn("2쪽: b1 2차 판독 실패", buf.getvalue()); self.assertTrue((work.readings_dir / "partial" / "p002.json").exists())
            self.assertTrue(any(l.startswith("오류: 판독에 실패한 쪽 1개: 2") for l in buf.getvalue().splitlines()), buf.getvalue())
            with patched_mock_reader() as made:
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    rc = cli.main(["read", pdf, "--work", w, "--reader", "mock"])
            self.assertEqual(rc, 0); self.assertEqual(made[0].calls["read_page"], 0); self.assertGreater(made[0].calls["reread_region"], 0)
            self.assertIn("pass1_reused_from", work.load_record(2)); self.assertFalse((work.readings_dir / "partial" / "p002.json").exists())
            with patched_mock_reader(pass2_fail_once={1: ReaderError("하루 한도", fatal=True)}):     # 치명 오류는 전체를 멈추되 1차는 남긴다
                with contextlib.redirect_stdout(io.StringIO()):
                    rc = cli.main(["read", pdf, "--work", w, "--reader", "mock", "--force", "--pages", "1"])
            self.assertEqual(rc, 1); self.assertTrue((work.readings_dir / "partial" / "p001.json").exists())


class Batching(unittest.TestCase):
    """2차 조각은 ⌈n/per⌉ 묶음으로 고르게 나누고, 묶음이 일시 오류면 두 반으로 한 번 더 묻고 빠진 조각만 낱개로 간다."""
    def test_even_batches(self):
        from yeone_ocr.cli import _batches
        f = lambda n, per: [len(b) for b in _batches(list(range(n)), per)]
        self.assertEqual(f(9, 8), [5, 4]); self.assertEqual(f(17, 8), [6, 6, 5]); self.assertEqual(f(8, 8), [8]); self.assertEqual(f(2, 8), [2])
        self.assertEqual(f(3, 2), [2, 1]); self.assertEqual(f(0, 8), []); self.assertEqual(f(4, 1), [1, 1, 1, 1]); self.assertEqual(f(16, 8), [8, 8])
        self.assertEqual(_batches(list(range(9)), 8), [[0, 1, 2, 3, 4], [5, 6, 7, 8]])        # 순서 유지

    def _page(self):
        from PIL import ImageDraw
        im = Image.new("L", (1000, 1400), 255); d = ImageDraw.Draw(im); regs = []
        for k, y in enumerate((0.10, 0.20, 0.30, 0.40, 0.50), 1):
            d.rectangle([100, int(y * 1400), 880, int((y + 0.022) * 1400)], fill=0)
            regs.append(Region(id=f"r{k}", kind="본문", bbox=[int(y * 1000) - 11, 77, int(y * 1000) + 33, 889],
                               paragraphs=[Paragraph(columns=[Column(col_from_right=1, text=f"{k}.《本草汇言》：艾叶暖血温经，行气开郁之药也。治妇人经血不调第{k}条")])]))
        return im, PageReading(pdf_page=1, regions=regs)

    def test_batch_failure_halves_then_singles(self):
        from yeone_ocr import prompts
        from yeone_ocr.cli import _second_pass
        from yeone_ocr.reader_gemini import ReaderError
        im, p1 = self._page()
        texts = {r.id: r.paragraphs[0].columns[0].text for r in p1.regions}
        class Fake:
            def __init__(self, limit, fatal=False): self.limit = limit; self.fatal = fatal; self.batches = []; self.single = []
            def reread_regions(self, items, n):
                self.batches.append([rid for rid, _, _ in items])
                if len(items) > self.limit:
                    raise ReaderError("묶음 실패", fatal=self.fatal)
                return {rid: RegionReread(columns=[Column(col_from_right=1, text=texts[rid])]) for rid, _, _ in items}
            def reread_region(self, im, kind, rid, n):
                self.single.append(rid); return RegionReread(columns=[Column(col_from_right=1, text=texts[rid])])
        prompts.set_mode("horizontal_zh")
        try:
            fake = Fake(2); buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rr, meta = _second_pass(fake, im, p1, 1, Namespace(chunk_lines=8, second_mode="batch", batch_crops=8))
            self.assertEqual([len(b) for b in fake.batches], [5, 3, 2])                     # 5장 실패 → 3+2 로 한 번 더 → 3장만 실패
            self.assertEqual(fake.single, ["r1", "r2", "r3"]); self.assertEqual(set(rr), {"r1", "r2", "r3", "r4", "r5"})
            self.assertIn("반으로 나눠 다시", buf.getvalue())
            fake2 = Fake(1)                                                                  # 두 장짜리 묶음은 반으로 나누지 않고 바로 낱개
            with contextlib.redirect_stdout(io.StringIO()):
                _second_pass(fake2, im, p1, 1, Namespace(chunk_lines=8, second_mode="batch", batch_crops=2))
            self.assertEqual([len(b) for b in fake2.batches], [2, 2]); self.assertEqual(fake2.single, ["r1", "r2", "r3", "r4", "r5"])
            with self.assertRaises(ReaderError):                                             # 치명 오류는 그대로 올라간다
                with contextlib.redirect_stdout(io.StringIO()):
                    _second_pass(Fake(2, fatal=True), im, p1, 1, Namespace(chunk_lines=8, second_mode="batch", batch_crops=8))
        finally:
            prompts.set_mode("vertical")

    def test_single_failure_is_noted_not_raised(self):
        from yeone_ocr import prompts
        from yeone_ocr.cli import _second_pass
        from yeone_ocr.reader_gemini import ReaderError
        im, p1 = self._page()
        class Fake:
            def reread_region(self, im, kind, rid, n):
                if rid == "r3": raise ReaderError("3회 시도 뒤에도 실패: 형식", fatal=False)
                return RegionReread(columns=[Column(col_from_right=1, text="x")])
        prompts.set_mode("horizontal_zh")
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                rr, meta = _second_pass(Fake(), im, p1, 1, Namespace(chunk_lines=8, second_mode="each"))
        finally:
            prompts.set_mode("vertical")
        self.assertEqual(set(rr), {"r1", "r2", "r4", "r5"}); self.assertTrue(meta["r3"]["pass2_failed"].startswith("3회 시도"))


class AdaptiveClean(unittest.TestCase):
    """동시 쪽 수 자동 조절은 성공한 쪽만 '깨끗한' 것으로 센다 — 실패만 이어지는 동안 동시 쪽 수가 늘지 않게."""
    def test_failures_do_not_count_as_clean(self):
        from yeone_ocr import cli
        class R: n_429 = 0
        def run(ok):
            def fn(item):
                if not ok: raise RuntimeError("실패")
                return item
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                cli.run_adaptive(list(range(12)), fn, [R()], ceiling=2, start=1, on_result=lambda i, r: None, on_error=lambda i, e: False, pause=0.1)
            cli.CANCEL.clear()
            return buf.getvalue()
        self.assertNotIn("안정적", run(False))
        self.assertIn("안정적 → 동시 2쪽", run(True))


class PageUpscale(unittest.TestCase):
    """Gemini 3 계열은 이미지 토큰 예산이 고정이라 쪽 전체를 2배로 키우지 않는다(CPU·전송만 듦). 2.5 세대·mock 은 전처럼."""
    def test_gemini3_skips_whole_page_upscale(self):
        from yeone_ocr.cli import page_upscale, read_one
        class Fake:
            name = "fake"
            def __init__(self, model): self.model = model; self.sizes = []
            def read_page(self, img, file_name, pdf_page, total, extra=""):
                self.sizes.append(img.size); return PageReading(pdf_page=pdf_page, regions=[])
        self.assertEqual(page_upscale(Fake("gemini-3-pro"), 2.0, (500, 800)), 1.0)
        self.assertEqual(page_upscale(Fake("gemini-3.1-pro-preview"), 2.0, (500, 800)), 1.0)
        self.assertEqual(page_upscale(Fake("gemini-2.5-pro"), 2.0, (500, 800)), 2.0)
        self.assertEqual(page_upscale(Fake("gemini-2.5-pro"), 2.0, (3000, 800)), 1.0)         # 4200px 을 넘으면 전처럼 키우지 않는다
        self.assertEqual(page_upscale(Fake("textlayer+cv"), 2.0, (500, 800)), 2.0)
        with tempfile.TemporaryDirectory() as td:
            doc = vertical_pdf(1); pdf = os.path.join(td, "v.pdf"); doc.save(pdf)
            work = Work(pdf, os.path.join(td, "w"))
            args = Namespace(pdf=pdf, dpi=72, upscale=2.0, no_second_pass=True, print_offset=None, mode="vertical")
            for model, factor in (("gemini-3-pro", 1), ("gemini-2.5-pro", 2)):
                f = Fake(model)
                with contextlib.redirect_stdout(io.StringIO()):
                    rec = read_one(work, doc, f, f, 1, args)
                self.assertEqual(f.sizes[-1], (500 * factor, 800 * factor), model); self.assertEqual(rec["upscale"], float(factor))
            doc.close()


# ---------------- 0.6.8 검토 고침: 2차 덜 된 쪽 · 사본 · 깨진 파일 · 3차 못 마침 · 부분 기록 사용량 ----------------
class _TwoRegionReader:
    """문자 영역 둘(b1·b2)을 돌려주는 가짜 판독기. fail 에 든 영역은 2차(reread_region)가 일시 오류, fail_all 은 (fail_pages 쪽의, 없으면 모든 쪽의)
    2차 호출마다 던질 예외. usage_* 는 쓰레드별 사용량."""
    name = "fake"

    def __init__(self, model="fake-pro", fail=(), fail_all=None, fail_pages=None):
        import threading
        self.model = model; self.fail = set(fail); self.fail_all = fail_all; self.fail_pages = fail_pages; self._tls = threading.local()
        self.calls = {"read_page": 0, "reread_region": 0}

    def usage_begin(self):
        self._tls.page = {}

    def usage_end(self):
        d = getattr(self._tls, "page", None) or {}; self._tls.page = None; return d

    def _bill(self, i, o):
        p = getattr(self._tls, "page", None)
        if p is not None:
            p["calls"] = p.get("calls", 0) + 1; p["prompt_tokens"] = p.get("prompt_tokens", 0) + i; p["output_tokens"] = p.get("output_tokens", 0) + o

    def read_page(self, img, file_name, pdf_page, total, extra=""):
        self.calls["read_page"] += 1; self._bill(5000, 3000)
        col = lambda: [Paragraph(columns=[Column(col_from_right=1, text="天地玄黃宇宙洪荒")])]
        return PageReading(pdf_page=pdf_page, regions=[Region(id="b1", kind="본문", bbox=[50, 830, 700, 950], reading_direction="세로(우→좌)", paragraphs=col()),
                                                       Region(id="b2", kind="본문", bbox=[50, 740, 700, 820], reading_direction="세로(우→좌)", paragraphs=col())])

    def reread_region(self, crop, kind, region_id, pdf_page):
        from yeone_ocr.reader_gemini import ReaderError
        self.calls["reread_region"] += 1; self._bill(800, 100)
        if self.fail_all is not None and (self.fail_pages is None or pdf_page in self.fail_pages):
            raise self.fail_all
        if region_id.split("#")[0] in self.fail:
            raise ReaderError("5회 시도 뒤에도 실패: Gemini 서버 오류(503)", fatal=False)
        return RegionReread(columns=[Column(col_from_right=1, text="天地玄黃宇宙洪荒")])


@contextlib.contextmanager
def _use_reader(r):
    """cmd_read 가 r 을 판독기로 쓰게(읽기 방향 판정은 그대로)."""
    from yeone_ocr import cli
    orig = cli.make_reader
    cli.make_reader = lambda args, doc, work: (cli.resolve_mode(args, doc, work), r)[1]
    try:
        yield r
    finally:
        cli.make_reader = orig


class Pass2Incomplete(unittest.TestCase):
    """2차가 일부 영역에서 실패한 쪽은 1차로 살리되 끝나지 않은 쪽으로 센다(오류: 줄·3). 다음 실행은 기록의 1차로 2차만 다시 묻는다."""
    def test_some_regions_failing_keeps_page_counts_it_and_retries_pass2_only(self):
        from yeone_ocr import cli
        with tempfile.TemporaryDirectory() as td:
            doc = vertical_pdf(2); pdf = os.path.join(td, "v.pdf"); doc.save(pdf); doc.close(); w = os.path.join(td, "w")
            with _use_reader(_TwoRegionReader(fail={"b2"})) as r:
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    rc = cli.main(["read", pdf, "--work", w, "--reader", "mock", "--pages", "1", "--second-mode", "each"])
            self.assertEqual(rc, 3, buf.getvalue())
            work = Work(pdf, w); rec = work.load_record(1)
            self.assertEqual(rec["pass2_failed"], ["b2"]); self.assertEqual([x["id"] for x in rec["pass1_raw"]["regions"]], ["b1", "b2"])
            self.assertIn("b1", rec["pass2"]); self.assertNotIn("b2", rec["pass2"])
            self.assertTrue(any(u["context"].startswith("2차 판독 실패(") and u["region_id"] == "b2" for u in rec["merged"]["uncertain"]))
            self.assertTrue(any(l.startswith("오류: 2차 판독이 일부 영역에서 실패한 쪽 1개: 1") for l in buf.getvalue().splitlines()), buf.getvalue())
            self.assertIn("판독 확인 필요", work.statuses(1)); self.assertNotIn("2차 독립 판독 비교 완료", work.statuses(1))   # 덜 된 2차에 '완료' 를 붙이지 않는다
            with _use_reader(_TwoRegionReader()) as r:                                       # 다음 실행: 1차는 다시 사지 않고 2차만
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    rc = cli.main(["read", pdf, "--work", w, "--reader", "mock", "--pages", "1", "--second-mode", "each"])
            self.assertEqual(rc, 0, buf.getvalue()); self.assertEqual(r.calls["read_page"], 0); self.assertEqual(r.calls["reread_region"], 1)   # 덜 된 b2 만
            self.assertIn("2차만 다시 묻습니다", buf.getvalue()); self.assertIn("1쪽: 이전 실행의 1차 판독을 재사용(2차만 다시)", buf.getvalue())
            rec = work.load_record(1)
            self.assertNotIn("pass2_failed", rec); self.assertNotIn("pass1_raw", rec); self.assertEqual(sorted(rec["pass2"]), ["b1", "b2"]); self.assertIn("pass1_reused_from", rec)
            self.assertFalse(any(u["context"].startswith("2차 판독 실패(") for u in rec["merged"]["uncertain"]))
            self.assertIn("2차 독립 판독 비교 완료", work.statuses(1))
            self.assertEqual(rec["usage"]["calls"], 1 + 2 + 1)                                # 첫 실행(1차 1·2차 2)도 이 쪽의 비용으로 이어받는다
            with _use_reader(_TwoRegionReader()) as r:                                       # 다 끝난 쪽은 다시 묻지 않는다
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(cli.main(["read", pdf, "--work", w, "--reader", "mock", "--pages", "1"]), 0)
            self.assertEqual(r.calls, {"read_page": 0, "reread_region": 0})

    def test_no_second_pass_and_force_leave_or_reread_incomplete_pages(self):
        from yeone_ocr import cli
        with tempfile.TemporaryDirectory() as td:
            doc = vertical_pdf(1); pdf = os.path.join(td, "v.pdf"); doc.save(pdf); doc.close(); w = os.path.join(td, "w")
            with _use_reader(_TwoRegionReader(fail={"b1"})):
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(cli.main(["read", pdf, "--work", w, "--reader", "mock", "--second-mode", "each"]), 3)
            before = Work(pdf, w).record_path(1).read_bytes()
            with _use_reader(_TwoRegionReader()) as r:                                       # 2차를 끈 실행은 덜 된 쪽의 2차 결과를 버리지 않는다(건너뜀)
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(cli.main(["read", pdf, "--work", w, "--reader", "mock", "--no-second-pass"]), 0)
            self.assertEqual(r.calls, {"read_page": 0, "reread_region": 0}); self.assertEqual(Work(pdf, w).record_path(1).read_bytes(), before)
            with _use_reader(_TwoRegionReader()) as r:                                       # --force 는 처음부터 다시(1차도)
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(cli.main(["read", pdf, "--work", w, "--reader", "mock", "--force", "--second-mode", "each"]), 0)
            self.assertEqual(r.calls["read_page"], 1)

    def test_every_region_failing_in_run_is_a_failed_page_retried_with_pass1(self):
        """검토 재현(r1): 2차가 모든 조각에서 실패하면 0.6.7 처럼 그 쪽은 실패(3)이고 다음 실행에서 다시 — 다만 1차는 다시 사지 않는다."""
        from yeone_ocr import cli
        from yeone_ocr.reader_gemini import ReaderError
        with tempfile.TemporaryDirectory() as td:
            doc = vertical_pdf(3); pdf = os.path.join(td, "v.pdf"); doc.save(pdf); doc.close(); w = os.path.join(td, "w")
            with _use_reader(_TwoRegionReader(fail_all=ReaderError("5회 시도 뒤에도 실패: Gemini 서버 오류(503)", fatal=False), fail_pages={1, 2})):
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    rc = cli.main(["run", pdf, "--work", w, "--reader", "mock", "--checkpoint", "0", "--second-mode", "each"])
                _join_prepare_ahead()
            self.assertEqual(rc, 3, buf.getvalue()); self.assertIn("실패한 쪽이 있습니다", buf.getvalue())
            work = Work(pdf, w); self.assertEqual([x["pdf_page"] for x in work.records()], [3])        # 1차만인 쪽을 기록(완료)으로 남기지 않는다
            self.assertTrue(any(l.startswith("오류: 판독에 실패한 쪽 2개: 1, 2") for l in buf.getvalue().splitlines()), buf.getvalue())
            part = json.loads((work.readings_dir / "partial" / "p001.json").read_text(encoding="utf-8"))
            self.assertIn("문자 영역 2개 모두에서 실패", part["error"])
            with _use_reader(_TwoRegionReader()) as r:
                with contextlib.redirect_stdout(io.StringIO()):
                    rc = cli.main(["run", pdf, "--work", w, "--reader", "mock", "--checkpoint", "0", "--second-mode", "each"])
                _join_prepare_ahead()
            self.assertEqual(rc, 0); self.assertEqual(r.calls["read_page"], 0); self.assertEqual(r.calls["reread_region"], 4)
            self.assertEqual([x["pdf_page"] for x in work.records()], [1, 2, 3]); self.assertIn("2차 독립 판독 비교 완료", work.statuses(2))


class PartialUsageByModel(unittest.TestCase):
    """부분 기록을 재사용한 쪽도 모델별 사용량(by_model)에 1차(pro) 토큰이 남아 비용 어림이 1차를 빼먹지 않는다."""
    def test_reused_partial_keeps_pass1_model(self):
        from yeone_ocr import cli
        from yeone_ocr.estimate import estimate
        with tempfile.TemporaryDirectory() as td:
            doc = vertical_pdf(1); pdf = os.path.join(td, "v.pdf"); doc.save(pdf)
            work = Work(pdf, os.path.join(td, "w"))
            args = Namespace(pdf=pdf, dpi=72, upscale=1.0, no_second_pass=False, print_offset=None, mode="vertical", chunk_lines=0, second_mode="each")
            pro = _TwoRegionReader("gemini-3-pro"); flash = _TwoRegionReader("gemini-3-flash", fail_all=RuntimeError("2차 중 예외"))
            with contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(RuntimeError):
                    cli.read_one(work, doc, pro, flash, 1, args)
                flash.fail_all = None
                rec = cli.read_one(work, doc, pro, flash, 1, args)
            u = rec["usage"]; bm = u["by_model"]
            self.assertEqual((u["calls"], u["prompt_tokens"], u["output_tokens"]), (4, 5000 + 3 * 800, 3000 + 3 * 100))
            self.assertEqual(bm["gemini-3-pro"], {"calls": 1, "prompt_tokens": 5000, "output_tokens": 3000})
            self.assertEqual(bm["gemini-3-flash"], {"calls": 3, "prompt_tokens": 2400, "output_tokens": 300})
            e = estimate(pdf, [1], records=[rec], model="gemini-3-pro", second_model="gemini-3-flash",
                         prices={"gemini-3-pro": {"in": 2.0, "out": 12.0}, "gemini-3-flash": {"in": 0.5, "out": 3.0}})
            self.assertEqual(sorted((r["model"], r["label"]) for r in e["rows"]), [("gemini-3-flash", "2차 · 확대 조각 확인"), ("gemini-3-pro", "1차 · 쪽 전체 판독")])
            p1 = _TwoRegionReader("gemini-3-pro"); p2 = _TwoRegionReader("gemini-3-pro")     # 2차가 1차와 같은 모델(다른 설정의 판독기)이면 모델별 값을 덮지 않고 더한다
            with contextlib.redirect_stdout(io.StringIO()):
                rec = cli.read_one(work, doc, p1, p2, 1, args)
            self.assertEqual(rec["usage"]["by_model"]["gemini-3-pro"]["calls"], 3)
            doc.close()


class PrevCopies(unittest.TestCase):
    """readings/prev/ 사본은 처음 한 번만이 아니다 — 첫 사본 뒤에 손으로 고친 글도 다음 다시 비교 전에 따로 남는다(앞의 사본은 덮지 않음)."""
    def test_second_upgrade_keeps_edit_made_after_first_copy(self):
        from unittest import mock
        from yeone_ocr import cli
        p1 = PageReading(pdf_page=4, regions=[Region(id="b", kind="본문", paragraphs=[Paragraph(columns=[Column(col_from_right=1, text="剋하니 冲和가 되고")])])])
        with tempfile.TemporaryDirectory() as td:
            work = Work(os.path.join(td, "a.pdf"), os.path.join(td, "w")); prev = work.readings_dir / "prev"
            work.save_record(4, {"pdf_page": 4, "version": "0.6.6", "pass1": p1.model_dump(), "pass2": {}, "merged": {**p1.model_dump(), "page_note": "EDIT-1"}})
            with contextlib.redirect_stdout(io.StringIO()):
                cli.refresh_stale_records(work, None, [4])
            rec = work.load_record(4); rec["merged"]["page_note"] = "EDIT-2"; work.save_record(4, rec)       # 첫 갱신 뒤 손으로 고침
            buf = io.StringIO()
            with mock.patch.object(cli, "__version__", cli.__version__ + ".next"), contextlib.redirect_stdout(buf):  # 현재 판과 다른 다음 판
                self.assertEqual(cli.refresh_stale_records(work, None, [4]), 1)
            notes = {f.name: json.loads(f.read_text(encoding="utf-8"))["merged"].get("page_note") for f in prev.iterdir()}
            self.assertEqual(notes.pop("p004.json"), "EDIT-1"); self.assertEqual(list(notes.values()), ["EDIT-2"])
            self.assertIn(f"1쪽 복사: {next(iter(notes))}", buf.getvalue())                     # 이번에 쓴 사본 이름을 알린다
            work.save_record(4, rec); self.assertIsNone(work.backup_record(4))                    # 이미 남긴 것과 같은 내용이면 또 만들지 않는다
            self.assertEqual(len(list(prev.iterdir())), 2)
            work.save_record(4, {"pdf_page": 4, "merged": "다른 내용 1"}); a = work.backup_record(4)
            work.save_record(4, {"pdf_page": 4, "merged": "다른 내용 2"}); b = work.backup_record(4)
            self.assertEqual(len(set(prev.iterdir())), 4); self.assertNotEqual(a, b)              # 같은 초에도 이름이 겹치지 않는다
            self.assertTrue(a.name.startswith("p004.") and b.name.endswith(".json"))


class BrokenFiles(unittest.TestCase):
    """깨진 파일: BOM 이 붙은 손 고침은 깨진 것이 아니고, 치운 파일은 앞서 치운 것을 덮지 않는다. 쓰다 만 쪽 이미지·글자층은 쓸 때 다시 꺼낸다."""
    def test_bom_hand_edit_is_not_broken(self):
        from yeone_ocr import state as S
        warns = []; old = S.WARN; S.WARN = warns.append
        try:
            with tempfile.TemporaryDirectory() as td:
                work = Work(os.path.join(td, "a.pdf"), os.path.join(td, "w"))
                work.record_path(5).write_text("﻿" + json.dumps({"pdf_page": 5, "merged": "HAND-EDIT-1"}), encoding="utf-8")   # 메모장 저장
                self.assertEqual([r["merged"] for r in work.records()], ["HAND-EDIT-1"]); self.assertEqual(warns, [])
                self.assertFalse((work.readings_dir / "broken").exists()); self.assertTrue(work.record_path(5).exists())
                work.state_path.write_text("﻿" + json.dumps({"file": "a.pdf", "pages": {"1": {"statuses": ["1차 OCR 완료"], "notes": []}}}), encoding="utf-8")
                self.assertEqual(work.statuses(1), ["1차 OCR 완료"]); self.assertEqual(warns, [])
        finally:
            S.WARN = old

    def test_quarantine_keeps_every_copy(self):
        from yeone_ocr import state as S
        warns = []; old = S.WARN; S.WARN = warns.append
        try:
            with tempfile.TemporaryDirectory() as td:
                work = Work(os.path.join(td, "a.pdf"), os.path.join(td, "w")); bd = work.readings_dir / "broken"
                work.record_path(5).write_text('{"pdf_page": 5, "merged": "HAND-EDIT-2",}', encoding="utf-8")
                self.assertIsNone(work.load_record(5))
                work.record_path(5).write_text('{"pdf_page": 5, "merged": "HAND-EDIT-3",}', encoding="utf-8")
                self.assertEqual(work.records(), [])
                kept = "".join(f.read_text(encoding="utf-8") for f in bd.iterdir())
                self.assertEqual(len(list(bd.iterdir())), 2); self.assertIn("HAND-EDIT-2", kept); self.assertIn("HAND-EDIT-3", kept)
                self.assertIn("readings/broken/p005.json", warns[0]); self.assertNotIn("readings/broken/p005.json", warns[1])   # 알림이 실제 이름을 댄다
                for k in range(2):
                    work.state_path.write_text('{"file": "a.pdf", "pages": {' + str(k), encoding="utf-8"); work.load_state()
                self.assertEqual(len([f for f in work.dir.iterdir() if f.name.startswith("state.json.")]), 2)
        finally:
            S.WARN = old

    def test_torn_page_image_and_textlayer_are_rebuilt(self):
        """검토 재현(r7): 쓰다 만 pNNN.png·글자층 JSON 은 run 이든 run --force 든 매번 그 쪽을 실패시켰다(prepare 는 있는지만 봄)."""
        from yeone_ocr import cli
        with tempfile.TemporaryDirectory() as td:
            doc = vertical_pdf(3); pdf = os.path.join(td, "v.pdf"); doc.save(pdf); doc.close(); w = os.path.join(td, "w")
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(cli.main(["prepare", pdf, "--work", w, "--dpi", "72"]), 0)
            work = Work(pdf, w); ip = work.page_image_path(2); tp = work.textlayer_path(3)
            ip.write_bytes(ip.read_bytes()[: ip.stat().st_size // 3]); tp.write_text(tp.read_text(encoding="utf-8")[:40], encoding="utf-8")
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = cli.main(["run", pdf, "--work", w, "--reader", "mock", "--checkpoint", "0", "--dpi", "72"])
            _join_prepare_ahead()
            self.assertEqual(rc, 0, buf.getvalue()); self.assertEqual([r["pdf_page"] for r in work.records()], [1, 2, 3])
            lines = buf.getvalue().splitlines()
            self.assertTrue(any(l.startswith("주의: 쪽 이미지 pages/p002.png") for l in lines), buf.getvalue())
            self.assertTrue(any(l.startswith("주의: 글자층 파일 textlayer/p003.json") for l in lines), buf.getvalue())
            Image.open(ip).load(); self.assertEqual(json.loads(tp.read_text(encoding="utf-8"))["page_height"], 800)   # 고쳐 써 둔다


class AdjudicateUnfinished(unittest.TestCase):
    """3차 판별을 못 마친 것(〔a/b〕 남음)은 실패한 쪽과 다르다 — 다른 말, 끝 코드 4. 실패한 쪽도 있으면 3 이 앞선다."""
    def _run(self, td, adj, fail_pages=()):
        from unittest import mock
        from yeone_ocr import cli
        from yeone_ocr.reader_mock import MockReader
        doc = vertical_pdf(2); pdf = os.path.join(td, "v.pdf"); doc.save(pdf); doc.close()
        def mk(args, d, work):
            cli.resolve_mode(args, d, work); r = MockReader({}); r.fail_pages = set(fail_pages); return r
        buf = io.StringIO()
        with mock.patch.object(cli, "make_reader", mk), mock.patch.object(cli, "cmd_adjudicate", adj), contextlib.redirect_stdout(buf):
            rc = cli.main(["run", pdf, "--work", os.path.join(td, "w" + str(len(os.listdir(td)))), "--reader", "gemini", "--second-model", "",
                           "--second-thinking", "full", "--media-resolution", "default", "--checkpoint", "0"])
        _join_prepare_ahead()
        return rc, buf.getvalue()

    def test_adjudicate_error_is_not_reported_as_failed_pages(self):
        def adj_rc1(*a, **k):
            print("오류: 3차 판별 실패 — 모델 'gemini-3-pro' 의 하루 요청 한도를 다 썼습니다."); return 1
        def adj_raises(*a, **k):
            raise RuntimeError("조각을 만들지 못함")
        with tempfile.TemporaryDirectory() as td:
            for adj in (adj_rc1, adj_raises):
                rc, out = self._run(td, adj)
                self.assertEqual(rc, 4, out); self.assertNotIn("실패한 쪽이 있습니다", out)
                self.assertIn("3차 글자 판별을 마치지 못해 〔a/b〕 표시가 남았습니다", out); self.assertIn("3차 판별을 이어서 합니다", out)
            rc, out = self._run(td, adj_rc1, fail_pages={2})                                 # 실패한 쪽까지 있으면 3, 두 말 모두
            self.assertEqual(rc, 3); self.assertIn("실패한 쪽이 있습니다", out); self.assertIn("3차 글자 판별을 마치지 못해", out)
            rc, out = self._run(td, lambda *a, **k: 0)
            self.assertEqual(rc, 0); self.assertNotIn("3차 글자 판별을 마치지 못해", out)


class Pass2Retry068(unittest.TestCase):
    """0.6.8 검토 2 — 2차가 덜 된 쪽: 책갈피 교정도 다시 묻고, 다시 물을 때는 덜 된 영역만(다른 영역의 글·3차 기록은 그대로, 바꾸기 전 사본),
    1차의 모델·설정이 바뀌었으면 1차부터라고 말한다. 2차 도중 멈춘 쪽의 1차는 1차 설정이 같을 때만 다시 쓴다. 3차 판별은 호출 상한에서 멈춘다."""
    def test_collate_retries_incomplete_pages_and_keeps_finished_regions(self):
        """검토 재현(INT-1·INT-2): 책갈피 교정은 2차가 덜 된 쪽을 기록이 있다고 건너뛰었고('이전 판독 기록으로 대조' 라는 틀린 말까지),
        [▶] 로 다시 물으면 모든 영역을 다시 사서 기록을 통째로 바꿔 손 고침·3차 판별·합의 의심 기록이 사라졌다(prev/ 사본도 없음)."""
        from yeone_ocr import cli
        from yeone_ocr.collate import run as R
        from yeone_ocr.pdfio import open_pdf
        with tempfile.TemporaryDirectory() as td:
            doc = vertical_pdf(2); pdf = os.path.join(td, "v.pdf"); doc.save(pdf); doc.close(); w = os.path.join(td, "w")
            args = cli.build_parser().parse_args(["collate", pdf, "--work", w, "--hwpx", "x.hwpx", "--reader", "mock", "--second-mode", "each"])
            work = Work(pdf, w); d = open_pdf(pdf)
            try:
                with _use_reader(_TwoRegionReader(fail={"b2"})):
                    buf = io.StringIO()
                    with contextlib.redirect_stdout(buf):
                        self.assertTrue(R.ensure_pages_read(args, work, d, (1, 1)))                      # 2차 일부 실패(3)
                rec = work.load_record(1); self.assertEqual(rec["pass2_failed"], ["b2"])
                self.assertNotIn("이전 판독 기록으로 대조", buf.getvalue())                             # 새로 만든 기록을 '이전 기록' 이라 하지 않는다
                rec["merged"]["regions"][0]["paragraphs"][0]["columns"][0]["text"] = "天地玄黃宇宙洪荒(손)"   # 2차가 된 b1 을 손으로 고침
                rec["adjudications"] = [{"region_id": "b1", "choice": "天", "a": "天", "b": "夫", "col_index": 0, "char_index": 0}]
                rec["suspects"] = [{"region_id": "b1", "outcome": "confirmed", "consensus": "天", "layer": "夫"}]
                work.save_record(1, rec)
                with _use_reader(_TwoRegionReader()) as r:
                    buf = io.StringIO()
                    with contextlib.redirect_stdout(buf):
                        self.assertFalse(R.ensure_pages_read(args, work, d, (1, 1)))                     # 기록이 있어도 2차가 덜 된 쪽은 다시 묻는다
                self.assertEqual(r.calls, {"read_page": 0, "reread_region": 1}, buf.getvalue())         # 덜 된 b2 만
                rec = work.load_record(1)
                self.assertNotIn("pass2_failed", rec); self.assertEqual(sorted(rec["pass2"]), ["b1", "b2"])
                self.assertEqual(rec["merged"]["regions"][0]["paragraphs"][0]["columns"][0]["text"], "天地玄黃宇宙洪荒(손)")
                self.assertEqual((len(rec["adjudications"]), len(rec["suspects"])), (1, 1))
                self.assertFalse(any(u["context"].startswith("2차 판독 실패(") for u in rec["merged"]["uncertain"]))
                self.assertIn("2차가 덜 된 영역 b2 만 다시 물음", buf.getvalue())
                self.assertFalse([l for l in buf.getvalue().splitlines() if l.startswith("주의:")], buf.getvalue())   # 잃은 글이 없으니 주의 없음
                self.assertIn("(손)", (work.readings_dir / "prev" / "p001.json").read_text(encoding="utf-8"))   # 그래도 바꾸기 전 사본
                with _use_reader(_TwoRegionReader()) as r:
                    with contextlib.redirect_stdout(io.StringIO()):
                        self.assertFalse(R.ensure_pages_read(args, work, d, (1, 1)))
                self.assertEqual(r.calls, {"read_page": 0, "reread_region": 0})
            finally:
                d.close()

    def test_hand_edit_in_retried_region_is_backed_up_and_announced(self):
        """다시 물은 영역(2차 실패했던 영역) 자체를 손으로 고쳤으면 그 글은 새 비교로 바뀐다 — prev/ 사본을 남기고 '주의:' 로 그 경로를 알린다."""
        from yeone_ocr import cli
        with tempfile.TemporaryDirectory() as td:
            doc = vertical_pdf(1); pdf = os.path.join(td, "v.pdf"); doc.save(pdf); doc.close(); w = os.path.join(td, "w")
            cmd = ["read", pdf, "--work", w, "--reader", "mock", "--second-mode", "each"]
            with _use_reader(_TwoRegionReader(fail={"b2"})), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(cli.main(cmd), 3)
            work = Work(pdf, w); rec = work.load_record(1)
            rec["merged"]["regions"][1]["paragraphs"][0]["columns"][0]["text"] = "天地玄黃宇宙洪荒(손)"; work.save_record(1, rec)
            with _use_reader(_TwoRegionReader()):
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    self.assertEqual(cli.main(cmd), 0)
            warn = [l for l in buf.getvalue().splitlines() if l.startswith("주의:")]
            self.assertEqual(len(warn), 1, buf.getvalue()); self.assertIn("영역 b2", warn[0]); self.assertIn("readings/prev/p001.json", warn[0])
            self.assertIn("(손)", (work.readings_dir / "prev" / "p001.json").read_text(encoding="utf-8"))
            self.assertNotIn("(손)", work.record_path(1).read_text(encoding="utf-8"))

    def test_changed_model_or_pass1_settings_reread_pass1_and_say_so(self):
        """검토 재현(INT-4·ops-3): 모델이 바뀐 쪽도 '1차를 다시 사지 않고 2차만' 이라 알리고 1차를 다시 샀다. 2차 도중 멈춘 쪽의 1차(partial/)는
        1차 생각 수준이 달라도 다시 써서 머리줄(1차 생각 high)과 실제 판독(low)이 달랐다 — 기록에 1차 설정(pass1_cfg)을 남기고 맞을 때만 다시 쓴다."""
        from yeone_ocr import cli
        from yeone_ocr.reader_gemini import ReaderError
        with tempfile.TemporaryDirectory() as td:
            doc = vertical_pdf(1); pdf = os.path.join(td, "v.pdf"); doc.save(pdf); doc.close(); w = os.path.join(td, "w")
            cmd = ["read", pdf, "--work", w, "--reader", "mock", "--second-mode", "each"]
            with _use_reader(_TwoRegionReader(fail={"b2"})), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(cli.main(cmd), 3)
            work = Work(pdf, w); self.assertEqual(work.load_record(1)["pass1_cfg"], ["auto", "auto", None])
            with _use_reader(_TwoRegionReader(model="other-pro")) as r:
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    self.assertEqual(cli.main(cmd), 0)
            out = buf.getvalue()
            self.assertEqual(r.calls["read_page"], 1); self.assertNotIn("2차만 다시 묻습니다", out); self.assertIn("1차부터 다시 읽습니다", out)
            self.assertTrue(any(l.startswith("주의: 1쪽의 이전 기록을") and "readings/prev/" in l for l in out.splitlines()), out)
            stop = ReaderError("모델 호출 상한 1회에 닿아 멈춤", fatal=True)
            for think, reused in (("high", False), ("low", True)):
                with _use_reader(_TwoRegionReader(fail_all=stop)), contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(cli.main(cmd + ["--force", "--page-thinking", "low"]), 1)                 # 2차 도중 멈춤 → 1차는 partial/
                part = json.loads((work.readings_dir / "partial" / "p001.json").read_text(encoding="utf-8")); self.assertEqual(part["pass1_cfg"], ["low", "auto", None])
                with _use_reader(_TwoRegionReader()) as r, contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(cli.main(cmd + ["--force", "--page-thinking", think]), 0)
                self.assertEqual(r.calls["read_page"], 0 if reused else 1, think)
                self.assertEqual(work.load_record(1)["pass1_cfg"], [think, "auto", None])

    def test_glyph_items_stop_at_call_cap(self):
        """검토 재현(ops-2): 책갈피 교정의 3차 판별이 호출 상한(치명 오류)을 삼켜 남은 자리마다 '판별 실패' 로 적고 '3곳 물음' 이라 셌다.
        상한에 닿으면 남은 자리는 묻지 않고(표시 그대로) 멈춘 사유를 돌려주며, 실제로 부른 자리만 센다."""
        from yeone_ocr import reader_gemini as rg
        from yeone_ocr.schema import GlyphChoice
        from yeone_ocr.collate.collate import Item
        from yeone_ocr.collate.run import adjudicate_items
        items = [Item("B1", "mark", 3, 5, 6, "人", "入", "", pdf_page=430, note="자형 편향 쌍(入/人) — 표시만", oc_ctx="肠，能「入」阴分，治", hw_ctx="腸，能「人」陰分，治"),
                 Item("B1", "mark", 4, 2, 3, "日", "曰", "", pdf_page=430, note="헷갈리기 쉬운 글자(曰/日) — 표시만", oc_ctx="唐容川「曰」：物下", hw_ctx="唐容川「日」：物下"),
                 Item("B1", "mark", 3, 9, 10, "人", "入", "", pdf_page=430, note="자형 편향 쌍(入/人) — 표시만", oc_ctx="色赤「入」心，性", hw_ctx="色赤「人」心，性")]
        class Fake:
            def adjudicate(self, crop, ask, h, p):
                rg.CALL_CAP.take(); return GlyphChoice(choice="", evidence="획이 뭉개짐")
        rg.set_call_cap(1)
        try:
            with tempfile.TemporaryDirectory() as td:
                work = Work(os.path.join(td, "a.pdf"), os.path.join(td, "w"))
                work.save_record(430, _page_rec()); Image.new("L", (900, 1300), 255).save(work.page_image_path(430))
                st = adjudicate_items(items, work=work, doc=None, reader=Fake(), horizontal=True, policy="all")
        finally:
            rg.set_call_cap(0)
        self.assertEqual((st["asked"], st["undecided"], st.get("skipped")), (1, 1, 2)); self.assertIn("호출 상한", st.get("stopped", ""))
        self.assertEqual([(it.kind, it.action) for it in items], [("B1", "mark")] * 3)
        self.assertTrue(all("멈춤으로 묻지 못함" in it.note and "판별 실패" not in it.note for it in items[1:]), [it.note for it in items])
