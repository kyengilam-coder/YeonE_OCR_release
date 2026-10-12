"""모델 없이 돌아가는 검사: 스키마·병합·검증 규칙·도판 외곽 맞춤·HWPX 조립과 파일 구조.
실행: python -m unittest discover -s YeonE_OCR/tests -v   (저장소 루트에서)"""
import io, os, sys, json, tempfile, unittest, zipfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PIL import Image, ImageDraw
from yeone_ocr.schema import PageReading, Region, Paragraph, Column, RegionReread, Uncertain, frac_bbox, bbox_frac, paragraph_text
from yeone_ocr.merge import diff_mark, merge_passes, compare_textlayer
from yeone_ocr.verify import verify_page, check_hwpx_file
from yeone_ocr.layout import fit_figure_bbox, edge_ink, detect_figures, crop_frac
from yeone_ocr.blocks import assemble_blocks
from yeone_ocr.hwpx_builder import build_hwpx


def page_with_circle(W=900, H=1300):
    im = Image.new("L", (W, H), 245); d = ImageDraw.Draw(im)
    for r in (300, 230, 150):
        d.ellipse([450 - r, 850 - r, 450 + r, 850 + r], outline=20, width=3)
    for x in range(820, 600, -30):                   # 세로 열 흉내(검은 사각 점)
        for y in range(100, 500, 24):
            d.rectangle([x - 8, y, x + 8, y + 16], fill=30)
    d.rectangle([120, 1200, 200, 1220], fill=30)       # 도판 아래 가까운 글자 흉내
    return im


class SchemaMerge(unittest.TestCase):
    def test_bbox_roundtrip(self):
        fr = (0.1, 0.2, 0.6, 0.9)
        self.assertEqual(tuple(round(v, 3) for v in bbox_frac(frac_bbox(fr))), fr)

    def test_paragraph_join_no_insert(self):
        p = Paragraph(columns=[Column(col_from_right=2, text="在乎陰陽"), Column(col_from_right=1, text="風水之理")])
        self.assertEqual(paragraph_text(p), "風水之理在乎陰陽")

    def test_diff_mark(self):
        t, marks = diff_mark("山은 높고 水는 낮으며", "山은 높고 氷는 낮으며")
        self.assertIn("〔水/氷〕", t); self.assertEqual(len(marks), 1)
        t2, m2 = diff_mark("甲乙丙", "甲乙丙")          # 같으면 표시 없음
        self.assertEqual((t2, m2), ("甲乙丙", []))
        t3, _ = diff_mark("甲 乙丙", "甲乙丙")           # 공백만 다르면 무시
        self.assertNotIn("〔", t3)
        t4, _ = diff_mark("納甲", "納〔疑: 甲〕")        # 한쪽의 불확실 표시는 살린다
        self.assertIn("〔疑: 甲〕", t4)

    def test_merge_passes_marks_and_counts(self):
        p1 = PageReading(pdf_page=1, regions=[Region(id="b1", kind="본문", paragraphs=[Paragraph(columns=[
            Column(col_from_right=1, text="納甲은 乾으로서"), Column(col_from_right=2, text="老陽이라")])])])
        rr = {"b1": RegionReread(columns=[Column(col_from_right=1, text="納화은 乾으로서"), Column(col_from_right=2, text="老陽이라")])}
        m, st = merge_passes(p1, rr)
        self.assertEqual(st["columns_compared"], 2); self.assertEqual(st["columns_differ"], 1)
        self.assertIn("〔甲/화〕", paragraph_text(m.regions[0].paragraphs[0]))
        self.assertEqual(len(m.uncertain), 1); self.assertEqual(m.uncertain[0].col_from_right, 1)

    def test_merge_aligns_by_text_not_number(self):
        """1차가 문단마다 열 번호를 1부터 다시 세고 2차가 영역 전체로 세어도, 같은 열끼리 비교해야 한다(3쪽 50건 사태 방지)."""
        p1 = PageReading(pdf_page=3, regions=[Region(id="b", kind="본문", paragraphs=[
            Paragraph(columns=[Column(col_from_right=1, text="이것은 十二地支下에 各排五子位이니"), Column(col_from_right=2, text="甲丙戊庚壬子하고 乙丁己辛癸丑의 順으로 排卦한 것이다")]),
            Paragraph(columns=[Column(col_from_right=1, text="이것을 一名胎骨龍이라고도 하고 또 六十分金이라"), Column(col_from_right=2, text="고도 하는데 이는 五子를 비롯한 六十龍直下에")])])])
        rr = {"b": RegionReread(columns=[
            Column(col_from_right=1, text="이것은 十二地支下에 各排五子位이니"), Column(col_from_right=2, text="甲丙戊庚壬子하고 乙丁己辛癸丑의 順으로 排卦한 것이다"),
            Column(col_from_right=3, text="이것을 一名胎骨龍이라고도 하고 또 六十分金이라"), Column(col_from_right=4, text="고도 하는데 이는 五子를 비롯한 六十龍直下에")])}
        m, st = merge_passes(p1, rr)
        self.assertEqual(st["columns_compared"], 4); self.assertEqual(st["columns_differ"], 0); self.assertEqual(m.uncertain, [])
        self.assertEqual([c.col_from_right for p in m.regions[0].paragraphs for c in p.columns], [1, 2, 3, 4])   # 영역 전체 번호로 통일
        # 2차에 열이 하나 빠지면 긴 구간을 본문 안에 표시하지 않고 확인 항목 한 건으로
        rr2 = {"b": RegionReread(columns=[rr["b"].columns[0], rr["b"].columns[2], rr["b"].columns[3]])}
        m2, st2 = merge_passes(p1, rr2)
        self.assertEqual(st2["long_segments"], 1); self.assertEqual(st2["marks"], 0)
        self.assertTrue(any("1차 판독만으로" in u.context and "甲丙戊庚壬子" in u.context for u in m2.uncertain))
        self.assertNotIn("〔", paragraph_text(m2.regions[0].paragraphs[0]))
        # 1차가 두 열을 한 열로 묶어 읽어도 글이 같으면 표시하지 않는다(9쪽 사례)
        p3 = PageReading(pdf_page=9, regions=[Region(id="b", kind="본문", paragraphs=[Paragraph(columns=[
            Column(col_from_right=1, text="天紀六十龍은 賴公에 依해 傳해졌다고 하나 或 一說에는 邱公의 所作이라는 말도 있다."),
            Column(col_from_right=2, text="이는 위로 天星의 瀾狹에 應하는 故로 盈縮이라 한다.")])])])
        rr3 = {"b": RegionReread(columns=[Column(col_from_right=1, text="天紀六十龍은 賴公에 依해 傳"), Column(col_from_right=2, text="해졌다고 하나 或 一說에는 邱公의 所作이라는 말"),
                                            Column(col_from_right=3, text="도 있다."), Column(col_from_right=4, text="이는 위로 天星의 瀾狹에 應하는 故로 盈縮이라 한다.")])}
        m3, st3 = merge_passes(p3, rr3)
        self.assertEqual(m3.uncertain, []); self.assertEqual(st3["marks"], 0); self.assertEqual(st3["column_count_mismatch"], [("b", 2, 4)])
        # 열 경계가 다르면서 글자 하나가 다르면 그 글자만 표시
        rr3["b"].columns[2].text = "도 있다,"
        m4, st4 = merge_passes(p3, rr3)
        self.assertEqual(st4["marks"], 1); self.assertIn("〔./,〕", paragraph_text(m4.regions[0].paragraphs[0]))

    def test_remerge_record_and_page_marks(self):
        from yeone_ocr.cli import remerge_record, fill_page_marks
        p1 = PageReading(pdf_page=4, regions=[
            Region(id="b", kind="본문", paragraphs=[Paragraph(columns=[Column(col_from_right=1, text="剋하니 冲和가 되고")])]),
            Region(id="n", kind="쪽번호", title_text="- 33 -")])
        fill_page_marks(p1); self.assertEqual((p1.page_label, p1.print_page), ("- 33 -", "33"))
        rec = {"pdf_page": 4, "pass1": p1.model_dump(), "pass2": {"b": RegionReread(columns=[Column(col_from_right=1, text="剋하니 沖和가 되고")]).model_dump()},
               "merged": PageReading(pdf_page=4, uncertain=[Uncertain(context="자동 감지한 큰 도형 (0.1, 0.5, 0.5, 0.9) 가 도판 영역으로 분류되지 않음")]).model_dump()}
        rec = remerge_record(rec, "剋하니冲和가되고")
        ctx = [u["context"] for u in rec["merged"]["uncertain"]]
        self.assertTrue(any("冲/沖" in c for c in ctx)); self.assertTrue(any(c.startswith("자동 감지한 큰 도형") for c in ctx))
        self.assertEqual(rec["merge_stats"]["columns_compared"], 1); self.assertIn("remerged_at", rec)

    def test_compare_textlayer(self):
        pr = PageReading(pdf_page=1, regions=[Region(id="b1", kind="본문", paragraphs=[Paragraph(columns=[Column(col_from_right=1, text="納甲은 乾")])])])
        d = compare_textlayer(pr, "納화은乾")
        self.assertEqual(len(d["items"]), 1); self.assertEqual(d["items"][0]["layer"], "화")


class Verify(unittest.TestCase):
    def test_rules(self):
        pr = PageReading(pdf_page=1, page_label="- 30 -", regions=[
            Region(id="b1", kind="본문", paragraphs=[Paragraph(columns=[Column(col_from_right=1, text="乾卦 === 坤卦 ㅅ - 30 - 〔疑: 龍")])]),
            Region(id="f1", kind="원형나경도", paragraphs=[Paragraph(columns=[Column(col_from_right=1, text="子癸丑")])])])
        rules = {i["rule"] for i in verify_page(pr)}
        self.assertTrue({"괘상 ASCII 대체", "한글 자모 분리", "쪽번호 혼입", "표시 괄호 불균형", "그림 안 글자 전사"} <= rules)
        clean = PageReading(pdf_page=1, regions=[Region(id="b1", kind="본문", paragraphs=[Paragraph(columns=[Column(col_from_right=1, text="山은 높고")])])])
        self.assertEqual(verify_page(clean), [])


class SpeedHelpers(unittest.TestCase):
    def test_pick_second_model(self):
        from yeone_ocr.reader_gemini import pick_second_model
        models = ["gemini-3.1-pro-preview", "gemini-3.1-flash", "gemini-3.1-flash-lite", "gemini-3.1-flash-preview", "gemini-2.5-pro", "gemini-2.5-flash", "gemini-3.8-flash-tts"]
        self.assertEqual(pick_second_model(models, "gemini-3.1-pro-preview"), "gemini-3.1-flash")
        self.assertEqual(pick_second_model(models, "gemini-2.5-pro"), "gemini-2.5-flash")
        self.assertEqual(pick_second_model(["gemini-2.5-pro", "gemini-3.1-flash"], "gemini-2.5-pro"), "gemini-3.1-flash")   # 같은 세대 없으면 더 새 flash
        self.assertEqual(pick_second_model(["gemini-2.5-pro"], "gemini-2.5-pro"), "gemini-2.5-pro")

    def test_short_json_keys_roundtrip(self):
        """모델은 짧은 키로 답하고, 코드·저장 파일은 긴 이름을 쓴다."""
        js = '{"pdf_page": 3, "print": "32", "header": "", "label": "- 32 -", "note": "", "unc": [{"r": "b1", "n": 2, "pos": "위", "ctx": "「〔疑: 子〕」"}],' \
             ' "regions": [{"id": "b1", "k": "본문", "bbox": [500, 100, 870, 650], "dir": "세로", "title": "", "lv": 0, "rows": [],' \
             ' "paras": [{"cont": false, "cols": [{"n": 1, "t": "이것은 十二地支下에"}, {"n": 2, "t": "各排五子位이니"}]}], "note": ""}]}'
        pr = PageReading.model_validate_json(js)
        self.assertEqual(pr.print_page, "32"); self.assertEqual(pr.regions[0].kind, "본문"); self.assertEqual(pr.regions[0].paragraphs[0].columns[1].text, "各排五子位이니")
        d = pr.model_dump(); self.assertIn("print_page", d); self.assertIn("paragraphs", d["regions"][0]); self.assertIn("col_from_right", d["regions"][0]["paragraphs"][0]["columns"][0])
        self.assertEqual(PageReading.model_validate(d).regions[0].paragraphs[0].columns[0].col_from_right, 1)   # 긴 이름으로도 다시 읽힘
        self.assertIn("cols", json.dumps(Paragraph.model_json_schema())); self.assertNotIn('"columns"', json.dumps(Paragraph.model_json_schema()))


class Gua(unittest.TestCase):
    def test_hexagram_table_and_check(self):
        from yeone_ocr.verify import HEXAGRAM, check_gua_symbols
        self.assertEqual((HEXAGRAM["乾"], HEXAGRAM["坤"], HEXAGRAM["豫"], HEXAGRAM["賁"], HEXAGRAM["困"], HEXAGRAM["未濟"]), ("䷀", "䷁", "䷏", "䷕", "䷮", "䷿"))
        self.assertEqual(HEXAGRAM["豊"], HEXAGRAM["豐"])
        ok = check_gua_symbols("丙子卦 困卦(䷮) 戊子卦 師卦(䷆)")
        self.assertEqual(ok, [])
        bad = check_gua_symbols("庚子卦 解卦(䷧) 壬子卦 解卦(䷯)")
        self.assertEqual(len(bad), 1); self.assertEqual(bad[0][0], "error"); self.assertIn("䷧", bad[0][3])
        tri = check_gua_symbols("甲子卦 坎卦(☵)")
        self.assertEqual(tri[0][0], "warn"); self.assertIn("䷜", tri[0][3])
        two = check_gua_symbols("雷地豫卦(☷☳)")
        self.assertEqual(two[0][0], "error"); self.assertIn("䷏", two[0][3])

    def test_nfc_in_merge(self):
        p1 = PageReading(pdf_page=15, regions=[Region(id="b", kind="본문", paragraphs=[Paragraph(columns=[Column(col_from_right=1, text="音樂이 廣潤하고")])])])
        rr = {"b": RegionReread(columns=[Column(col_from_right=1, text="音\uf914이 廣潤하고")])}   # 호환 한자 潤
        m, st = merge_passes(p1, rr)
        self.assertEqual(st["marks"], 0); self.assertEqual(m.uncertain, [])


class GeminiKeys(unittest.TestCase):
    def test_split_keys(self):
        from yeone_ocr.reader_gemini import split_keys
        a, b, c = "AIza" + "a" * 35, "AIza" + "b" * 35, "AQ." + "C" * 50
        self.assertEqual(split_keys(a), [a])
        self.assertEqual(split_keys(a[:10] + " \n" + a[10:]), [a])            # 줄이 꺾인 키 하나는 하나
        self.assertEqual(split_keys(f"{a}, {b}\n{c};{a}"), [a, b, c])        # 쉼표·줄바꿈·세미콜론, 겹친 키는 하나로
        self.assertEqual(split_keys(a + b), [a, b])                           # 구분 없이 붙여 넣어도 머리로 가름
        self.assertEqual(split_keys(""), [])

    def test_daily_quota_rotates_to_next_key(self):
        import pydantic
        from yeone_ocr import reader_gemini as rg
        a, b = "AIza" + "a" * 35, "AIza" + "b" * 35
        class Out(pydantic.BaseModel):
            x: int
        class Q(Exception):
            code = 429
        daily = Q("429 RESOURCE_EXHAUSTED {'quotaId': 'GenerateRequestsPerDayPerProjectPerModel'} retry in 1s")
        calls = []
        class Models:
            def __init__(self, key): self.key = key
            def generate_content(self, **kw):
                calls.append(self.key)
                if self.key == a:
                    raise daily
                class R: text = '{"x": 7}'; usage_metadata = None
                return R()
        class Client:
            def __init__(self, key): self.models = Models(key)
        orig = rg.make_client
        rg.make_client = lambda key, vx=False, timeout_ms=0: Client(key)
        try:
            r = rg.GeminiReader(model="gemini-3.1-pro-preview", api_key=f"{a}, {b}", use_cache=False)
            out = r._call(None, "p", Out, extra_parts=["p"])
            self.assertEqual(out.x, 7); self.assertEqual(calls, [a, b]); self.assertEqual(r.key_idx, 1)
            self.assertEqual(r.n_429, 0)                                       # 키 바꾸기는 속도 조절(동시 쪽 수 줄이기) 대상이 아님
            r2 = rg.GeminiReader(model="gemini-3.1-pro-preview", api_key=a, use_cache=False)
            with self.assertRaises(rg.ReaderError) as cm:                     # 키가 하나뿐이면 전처럼 멈춤
                r2._call(None, "p", Out, extra_parts=["p"])
            self.assertTrue(cm.exception.fatal)
            r.keys = [a, b]; r.key_idx = 1; r.key = b
            Models.generate_content = lambda self, **kw: (_ for _ in ()).throw(daily)
            with self.assertRaises(rg.ReaderError) as cm:                     # 모든 키가 한도면 멈추고 그렇다고 알림
                r._call(None, "p", Out, extra_parts=["p"])
            self.assertIn("키 2개가 모두", str(cm.exception))
        finally:
            rg.make_client = orig


class Layout(unittest.TestCase):
    def test_detect_and_fit(self):
        im = page_with_circle()
        figs = detect_figures(im)
        self.assertTrue(figs, "OpenCV 가 있으면 원을 찾아야 함")
        f = figs[0]
        self.assertAlmostEqual((f[0] + f[2]) / 2, 0.5, delta=0.05)
        # 모델이 준 좌표가 원을 조금 잘랐다고 가정 → 바깥으로 넓혀 복구
        cut = (f[0] + 0.02, f[1] + 0.02, f[2] - 0.02, f[3] - 0.02)
        fit = fit_figure_bbox(im, cut)
        self.assertEqual(fit["touching"], [])
        crop = crop_frac(im, fit["bbox"], pad=0.01)
        self.assertTrue(all(v == 0 for v in edge_ink(crop).values()))

    def test_grow_to_components_recovers_half_cut_figure(self):
        from yeone_ocr.layout import grow_to_components
        im = page_with_circle()
        f = detect_figures(im)[0]
        half = (f[0], f[1], (f[0] + f[2]) / 2, f[3])          # 모델이 원의 왼쪽 반만 잡은 경우(0.6.5 실측: 第七圖 艮丙龍)
        g = grow_to_components(im, half)
        self.assertAlmostEqual(g[2], f[2], delta=0.01)
        fit = fit_figure_bbox(im, g)
        self.assertEqual(fit["touching"], [])

    def test_grow_to_components_ignores_frame_and_glyphs(self):
        from yeone_ocr.layout import grow_to_components
        im = Image.new("L", (900, 1300), 245); d = ImageDraw.Draw(im)
        d.rectangle([40, 40, 860, 1260], outline=0, width=4)     # 판 테두리(版框): 도판 좌표 테두리를 건드리지 않음
        d.ellipse([300, 400, 600, 700], outline=0, width=5)      # 도판
        d.rectangle([640, 540, 660, 560], fill=0)                # 도판 옆 글자 하나(작은 성분)
        box = (290 / 900, 390 / 1300, 650 / 900, 710 / 1300)     # 오른쪽 변이 글자를 가로지름
        self.assertEqual(grow_to_components(im, box), box)


class ExpandText(unittest.TestCase):
    def test_expand_until_clean_recovers_cut_glyphs(self):
        from yeone_ocr.layout import expand_until_clean
        im = Image.new("L", (900, 1300), 245); d = ImageDraw.Draw(im)
        for x in range(820, 500, -30):                    # 세로 열 흉내: 글자 블록이 y 100~700
            for y in range(100, 700, 24):
                d.rectangle([x - 8, y, x + 8, y + 16], fill=30)
        tight = (520 / 900, 110 / 1300, 820 / 900, 690 / 1300)   # 모델 좌표가 글자 반 개 안쪽으로 들어온 경우
        fit = expand_until_clean(im, tight, pad=0.012)
        self.assertEqual(fit["touching"], []); self.assertGreater(fit["steps"], 0)
        x0, y0, x1, y1 = fit["bbox"]; pad = 0.012
        self.assertLess((x0 - pad) * 900, 512); self.assertGreater((x1 + pad) * 900, 828)
        self.assertLess((y0 - pad) * 1300, 100); self.assertGreater((y1 + pad) * 1300, 692)   # 마지막 글자 블록 676~692

    def test_uncovered_column_attached(self):
        from yeone_ocr.layout import uncovered_ink_boxes, attach_uncovered
        im = Image.new("L", (900, 1300), 245); d = ImageDraw.Draw(im)
        for x in (820, 790, 760):                      # 열 셋
            for y in range(600, 760, 24):
                d.rectangle([x - 8, y, x + 8, y + 16], fill=30)
        region = (775 / 900, 595 / 1300, 832 / 900, 780 / 1300)   # 모델이 두 열만 감싼 좌표
        boxes = uncovered_ink_boxes(im, [region])
        self.assertEqual(len(boxes), 1); self.assertAlmostEqual((boxes[0][0] + boxes[0][2]) / 2 * 900, 760, delta=6)
        grown, orphans = attach_uncovered(boxes, [("r3", region)])
        self.assertEqual(orphans, []); self.assertLess(grown["r3"][0] * 900, 753)
        # 큰 제목 글자가 좌표 밖으로 삐져나온 경우: cover_check 가 제목 영역을 넓히고 고아로 남기지 않아야 한다
        from yeone_ocr.cli import cover_check
        im2 = Image.new("L", (900, 1300), 245); d2 = ImageDraw.Draw(im2)
        for y in range(170, 420, 36):
            d2.rectangle([725, y, 765, y + 28], fill=30)                 # 제목 글자(큼) x 725~765
        p1 = PageReading(pdf_page=3, regions=[Region(id="t1", kind="제목", title_text="第十四層", bbox=[130, 829, 330, 847])])  # x 746~762: 왼쪽 획 삐져나옴
        notes = cover_check(im2, p1)
        self.assertTrue(any("t1" in n for n in notes), notes); self.assertEqual(p1.uncertain, [])
        self.assertLessEqual(p1.regions[0].bbox[1], 810)


class ReportUsage(unittest.TestCase):
    def test_usage_summary(self):
        from yeone_ocr.state import Work
        from yeone_ocr.report import make_report
        with tempfile.TemporaryDirectory() as td:
            pdf = os.path.join(td, "x.pdf"); open(pdf, "wb").write(b"%PDF-1.4\n")
            w = Work(pdf, os.path.join(td, "w"))
            for n, calls in ((1, 3), (2, 5)):
                w.save_record(n, {"file": "x.pdf", "pdf_page": n, "reader": "gemini", "model": "m", "seconds": 20, "verify": [],
                                  "usage": {"calls": calls, "prompt_tokens": 1000 * calls, "output_tokens": 100 * calls},
                                  "merged": PageReading(pdf_page=n, print_page=str(29 + n)).model_dump()})
                w.set_status(n, "1차 OCR 완료")
            rep = make_report(w, 2)
            self.assertIn("호출 8회", rep); self.assertIn("쪽당 평균: 호출 4.0회", rep); self.assertIn("입력 8,000", rep)


class Estimate(unittest.TestCase):
    def test_image_tokens_and_estimate(self):
        from yeone_ocr.estimate import image_tokens, estimate, format_estimate
        self.assertEqual(image_tokens(300, 300), 258); self.assertEqual(image_tokens(1788, 2518), 3 * 4 * 258)
        e = estimate("x.pdf", [1, 2, 3, 4], model="m", prices={"m": {"in": 2.0, "out": 12.0}}, fx_krw=1400, page_size_px=(894, 1261), guide_chars=13000)
        self.assertEqual(e["pages"], 4); self.assertEqual(e["basis"], "기본값"); self.assertEqual(e["calls"], 8)     # 쪽당 1차 1회 + 묶음 2차 1회
        self.assertGreater(e["in_high"], e["in_low"]); self.assertIsNotNone(e["usd_low"]); self.assertGreater(e["krw_high"], e["krw_low"])
        txt = format_estimate(e); self.assertIn("예상 비용 $", txt); self.assertIn("원 ·", txt); self.assertIn("직접 입력", txt)
        # 실측이 있으면 그 평균으로, 이미 읽은 쪽은 뺀다
        recs = [{"pdf_page": 1, "pass1": {}, "usage": {"calls": 4, "prompt_tokens": 40000, "output_tokens": 6000, "thinking_tokens": 2000}}]
        e2 = estimate("x.pdf", [1, 2, 3], model="m", prices={}, records=recs, page_size_px=(894, 1261), guide_chars=13000)
        self.assertEqual(e2["already_read"], 1); self.assertEqual(e2["calls"], 8); self.assertTrue(e2["basis"].startswith("실측")); self.assertIsNone(e2["usd_low"])
        self.assertEqual(e2["in_low"], round(40000 * 2 * 0.85))

    def test_builtin_prices_and_auto_second_model(self):
        from yeone_ocr.estimate import default_price, resolve_price, guess_second_model, estimate, format_estimate, DEFAULT_FX_KRW
        self.assertEqual(default_price("gemini-3.1-pro-preview")["key"], "gemini-3.1-pro")
        self.assertEqual(default_price("models/gemini-2.5-flash-lite-preview-06-17")["key"], "gemini-2.5-flash-lite")
        self.assertEqual(default_price("gemini-3-flash-preview")["in"], 0.50); self.assertIsNone(default_price("my-model"))
        self.assertEqual(resolve_price("gemini-3.1-pro-preview", {"gemini-3.1-pro-preview": {"in": 9, "out": 9}})["source"], "직접 입력")
        self.assertEqual(guess_second_model("gemini-3.1-pro-preview", "auto"), "gemini-3-flash")
        self.assertEqual(guess_second_model("gemini-2.5-pro", "auto"), "gemini-2.5-flash")
        self.assertEqual(guess_second_model("gemini-3-flash-preview", "auto"), "gemini-3-flash-preview")
        self.assertEqual(guess_second_model("gemini-3.1-pro-preview", ""), "gemini-3.1-pro-preview")
        self.assertEqual(guess_second_model("gemini-3.1-pro-preview", "gemini-2.5-flash"), "gemini-2.5-flash")
        # 단가·환율을 넣지 않아도 금액이 나온다(내장 요금표 + 기본 환율), 2차는 flash 단가라 1차만 pro 로 읽을 때보다 싸다
        e = estimate("x.pdf", [1, 2], model="gemini-3.1-pro-preview", second_model="auto", page_size_px=(894, 1261), guide_chars=13000)
        self.assertIsNotNone(e["usd_low"]); self.assertEqual(e["fx"], DEFAULT_FX_KRW); self.assertEqual(len(e["price_lines"]), 2)
        e_pro = estimate("x.pdf", [1, 2], model="gemini-3.1-pro-preview", second_model="", page_size_px=(894, 1261), guide_chars=13000)
        self.assertLess(e["usd_high"], e_pro["usd_high"])
        e_nc = estimate("x.pdf", [1, 2], model="gemini-3.1-pro-preview", second_model="auto", use_cache=False, page_size_px=(894, 1261), guide_chars=13000)
        self.assertGreater(e_nc["usd_low"], e["usd_low"])                       # 캐시를 끄면 지침이 전액 과금
        txt = format_estimate(e); self.assertIn("내장 요금표", txt); self.assertIn("auto 추정", txt)
        # 모델별 실측이 있으면 모델마다 제 단가로
        recs = [{"pdf_page": 1, "pass1": {}, "usage": {"calls": 2, "prompt_tokens": 30000, "output_tokens": 4000, "thinking_tokens": 1000, "cached_tokens": 20000,
                 "by_model": {"gemini-3.1-pro-preview": {"calls": 1, "prompt_tokens": 15000, "cached_tokens": 10000, "output_tokens": 2500, "thinking_tokens": 1000},
                              "gemini-3-flash-preview": {"calls": 1, "prompt_tokens": 15000, "cached_tokens": 10000, "output_tokens": 1500, "thinking_tokens": 0}}}}]
        e3 = estimate("x.pdf", [1, 2, 3], model="gemini-3.1-pro-preview", second_model="auto", records=recs, page_size_px=(894, 1261), guide_chars=13000)
        self.assertTrue(e3["basis"].startswith("실측")); self.assertIsNotNone(e3["usd_low"])
        pro_only = (5000 * 2.0 + 10000 * 0.20 + 3500 * 12.0) / 1e6; flash = (5000 * 0.5 + 10000 * 0.05 + 1500 * 3.0) / 1e6
        self.assertAlmostEqual(e3["usd_low"], (pro_only + flash) * 2 * 0.8, places=6)


class Build(unittest.TestCase):
    def test_hwpx_structure_and_images(self):
        with tempfile.TemporaryDirectory() as td:
            im = page_with_circle(); fig = im.crop((100, 500, 800, 1200)); figp = os.path.join(td, "f1.png"); fig.save(figp)
            rec = {"file": "x.pdf", "pdf_page": 3, "reader": "test", "verify": [],
                   "merged": PageReading(pdf_page=3, print_page="32", regions=[
                       Region(id="t1", kind="제목", title_text="第十四層 順排六十龍先天卦", title_level=2),
                       Region(id="f1", kind="원형나경도", bbox=[380, 100, 920, 900]),
                       Region(id="c1", kind="설명문", paragraphs=[Paragraph(columns=[Column(col_from_right=1, text="一層 地盤正針")])]),
                       Region(id="tb", kind="단순표", table_rows=[{"cells": ["甲山", "庚山"]}, {"cells": ["항목", "항목"]}]),
                       Region(id="n1", kind="쪽번호", title_text="- 32 -")],
                       uncertain=[Uncertain(region_id="c1", col_from_right=1, position="위", context="「〔疑: 針〕」")]).model_dump(),
                   "crops": {"f1": {"path": figp, "frac_w": 0.8, "ratio": fig.height / fig.width}}}
            blocks = assemble_blocks([rec])
            types = [b.type for b in blocks]
            self.assertEqual(types, ["meta", "heading", "image", "para", "table"])                 # 확인 목록은 기본으로 넣지 않음
            self.assertEqual([b.type for b in assemble_blocks([rec], check_list="all")][-1], "uncertain")
            out = os.path.join(td, "o.hwpx")
            rec2 = dict(rec); rec2["pdf_page"] = 4; rec2["merged"] = PageReading(pdf_page=4, print_page="33", regions=[
                Region(id="b", kind="본문", paragraphs=[Paragraph(columns=[Column(col_from_right=1, text="山은 높고 〔疑: 龍〕 水는 〔낮/낯〕다 〔→PDF 4쪽〕")])])]).model_dump()
            rec2["crops"] = {}
            blocks = assemble_blocks([rec, rec2])
            st = build_hwpx(blocks, out)
            self.assertEqual(st["images"], 1); self.assertEqual(st["images_in_file"], 1); self.assertEqual(st["tables"], 1)
            self.assertEqual(st["page_breaks"], 1)                     # 둘째 쪽 앞에서만
            self.assertGreaterEqual(st["marks"], 2)                    # 〔疑: 龍〕 + 〔낮/낯〕 ; 〔→PDF 4쪽〕 은 작업 표시
            self.assertEqual(check_hwpx_file(out), [])
            with zipfile.ZipFile(out) as z:
                self.assertEqual(z.namelist()[0], "mimetype")
                allxml = (z.read("Contents/header.xml") + z.read("Contents/section0.xml")).decode("utf-8")
            self.assertIn("FFFF00", allxml.upper()); self.assertIn("C00000", allxml.upper()); self.assertIn('pageBreak="1"', allxml)
            import re
            refs = re.findall(r'<hp:p\b[^>]*styleIDRef="(\d+)"', allxml)
            self.assertTrue(refs and set(refs) == {"0"}, f"개요 스타일 문단이 있으면 한컴이 자동 번호를 붙인다: {set(refs)}")
            self.assertIn('horizontal="CENTER"', allxml)                 # 제목·도판 가운데 정렬 문단 모양
            from yeone_ocr.hwpx_builder import split_marks
            self.assertEqual(split_marks("가〔疑: 龍〕나〔→PDF 2쪽〕"), [("가", "body"), ("〔疑: 龍〕", "mark"), ("나", "body"), ("〔→PDF 2쪽〕", "note")])
            # DOCX 를 이름만 바꾼 가짜는 걸러낸다
            fake = os.path.join(td, "fake.hwpx")
            with zipfile.ZipFile(fake, "w") as zf: zf.writestr("word/document.xml", "<x/>")
            self.assertTrue(any(i["rule"] == "DOCX 구조" for i in check_hwpx_file(fake)))

    def test_join_pages(self):
        r1 = {"file": "x.pdf", "pdf_page": 1, "reader": "test", "verify": [], "crops": {},
              "merged": PageReading(pdf_page=1, print_page="30", regions=[Region(id="b1", kind="본문", paragraphs=[Paragraph(columns=[Column(col_from_right=1, text="風水之理")])])]).model_dump()}
        r2 = {"file": "x.pdf", "pdf_page": 2, "reader": "test", "verify": [], "crops": {},
              "merged": PageReading(pdf_page=2, print_page="31", regions=[Region(id="b1", kind="본문", paragraphs=[Paragraph(continues_from_previous=True, columns=[Column(col_from_right=1, text="在乎陰陽")])])]).model_dump()}
        paras = [b for b in assemble_blocks([r1, r2]) if b.type == "para"]
        self.assertEqual(len(paras), 1); self.assertEqual(paras[0].text, "風水之理〔→PDF 2쪽/인쇄 31쪽〕在乎陰陽")
        paras2 = [b for b in assemble_blocks([r1, r2], join_pages=False) if b.type == "para"]
        self.assertEqual(len(paras2), 2)


class Guide(unittest.TestCase):
    def test_guide_is_real(self):
        """지침 전문이 패키지 안에 있어 모델 시스템 지시로 들어가야 한다."""
        from yeone_ocr.prompts import load_guide, system_prompt
        g = load_guide()
        self.assertIn("## 36. 최종 품질 기준", g)
        self.assertIn("## 출력 형식 규칙", system_prompt())


class GeminiHelpers(unittest.TestCase):
    def test_normalize_and_shape(self):
        from yeone_ocr.reader_gemini import normalize_key, key_shape_warning, friendly_error
        k = "AIza" + "x" * 35
        self.assertEqual(normalize_key(f'  "GEMINI_API_KEY={k}"\n'), k)
        self.assertEqual(normalize_key(k[:10] + " \n" + k[10:]), k)
        self.assertEqual(key_shape_warning(k), "")
        self.assertEqual(key_shape_warning("AQ." + "B" * 50), "")   # 새 형식 키
        self.assertIn("78자", key_shape_warning(k + k))          # 두 번 붙여 넣음
        from yeone_ocr.reader_gemini import model_sort_key, is_text_model, recommended_model, retry_delay_seconds
        names = ["gemini-2.5-flash", "gemini-3.1-pro-preview", "gemini-2.5-pro", "gemini-3.1-flash", "gemini-2.0-flash-lite"]
        self.assertEqual(sorted(names, key=model_sort_key)[:3], ["gemini-3.1-pro-preview", "gemini-3.1-flash", "gemini-2.5-pro"])
        self.assertFalse(is_text_model("gemini-3.8-flash-tts")); self.assertFalse(is_text_model("gemini-embedding-001"))
        self.assertFalse(is_text_model("gemini-2.5-flash-image")); self.assertTrue(is_text_model("gemini-3.1-pro-preview"))
        m404 = "404 NOT_FOUND. {'error': {'code': 404, 'message': 'This model models/gemini-2.5-pro is no longer available to new users. Please update your code to use models/gemini-3.1-pro-preview for the latest features'}}"
        self.assertEqual(recommended_model(m404), "gemini-3.1-pro-preview")
        class N4(Exception): code = 404
        msg, _ = friendly_error(N4(m404), "gemini-2.5-pro"); self.assertIn("gemini-3.1-pro-preview", msg)
        self.assertEqual(retry_delay_seconds("429 ... Please retry in 23.5s."), 23.5)
        self.assertEqual(retry_delay_seconds("{'retryDelay': '41s'}"), 41.0)
        self.assertEqual(retry_delay_seconds("no hint"), 20.0)
        class Q4(Exception): code = 429
        msg, retry = friendly_error(Q4("429 RESOURCE_EXHAUSTED ... limit: 0, model: gemini-3.1-pro-preview"), "gemini-3.1-pro-preview")
        self.assertIn("한도가 0", msg); self.assertFalse(retry)
        daily = ("429 RESOURCE_EXHAUSTED. {'error': {'code': 429, 'message': 'You exceeded your current quota. limit: 250, model: gemini-3.1-pro-preview. Please retry in 39.6s.', "
                 "'details': [{'violations': [{'quotaId': 'GenerateRequestsPerDayPerProjectPerModel-FreeTier'}]}, {'retryDelay': '39s'}]}}")
        msg, retry = friendly_error(Q4(daily), "gemini-3.1-pro-preview")
        self.assertIn("하루 요청 한도", msg); self.assertFalse(retry)     # 하루 한도는 재시도하지 않고 멈춤(fatal)
        minute = daily.replace("PerDay", "PerMinute")
        msg, retry = friendly_error(Q4(minute), "gemini-3.1-pro-preview"); self.assertTrue(retry)   # 분당 한도는 기다렸다 재시도
        self.assertTrue(key_shape_warning(""))
        class E(Exception): code = 400
        msg, retry = friendly_error(E("400 INVALID_ARGUMENT ... API_KEY_INVALID ... API key not valid"))
        self.assertIn("API 키가 올바르지 않습니다", msg); self.assertFalse(retry)
        class Q(Exception): code = 429
        msg, retry = friendly_error(Q("quota")); self.assertTrue(retry)
        class N(Exception): code = 404
        msg, retry = friendly_error(N("404 NOT_FOUND. {'error': {'message': 'models/gemini-9 is not found for API version v1beta'}}"), "gemini-9")
        self.assertIn("gemini-9", msg); self.assertIn("서버 메시지", msg); self.assertFalse(retry)


if __name__ == "__main__":
    unittest.main()


class GuiConfig(unittest.TestCase):
    def test_work_dir_names_do_not_collide(self):
        """같은 이름의 PDF·이미지·이미지 폴더가 한곳에 있어도 결과 폴더(판독 기록)가 섞이지 않는다."""
        import tempfile
        from yeone_ocr.gui import work_dir_for, IMAGE_EXTS
        from yeone_ocr.pdfio import IMAGE_EXTS as PDFIO_EXTS
        self.assertEqual(IMAGE_EXTS, PDFIO_EXTS)
        with tempfile.TemporaryDirectory() as td:
            os.makedirs(os.path.join(td, "book"))
            names = {work_dir_for(os.path.join(td, n)).name for n in ("book.pdf", "book.png", "book.jpg", "book")}
            self.assertEqual(names, {"book_YeonE_OCR", "book_png_YeonE_OCR", "book_jpg_YeonE_OCR", "book_이미지_YeonE_OCR"})

    def test_cli_default_work_dir_follows_gui_rule(self):
        """명령줄 기본 결과 폴더(--work 없이)도 창과 같은 규칙: book.pdf·book.png·book/ 이 work/book 하나에 섞이지 않는다."""
        import tempfile
        from yeone_ocr.state import Work
        from yeone_ocr.gui import work_dir_for
        cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as td:
            os.chdir(td)
            try:
                os.makedirs("book")
                dirs = {n: Work(n).dir for n in ("book.pdf", "book.png", "book.JPG", "book")}
                self.assertEqual({n: str(d) for n, d in dirs.items()}, {"book.pdf": os.path.join("work", "book"), "book.png": os.path.join("work", "book_png"),
                                                                       "book.JPG": os.path.join("work", "book_jpg"), "book": os.path.join("work", "book_이미지")})
                for n, d in dirs.items():
                    self.assertEqual(work_dir_for(n).name, d.name + "_YeonE_OCR")
                self.assertEqual(Work("book.pdf", "elsewhere").dir, Path("elsewhere"))      # --work 를 주면 그대로
            finally:
                os.chdir(cwd)

    def test_cli_hints_legacy_work_dir(self):
        """예전 판은 이미지·이미지 폴더의 결과를 work/<이름> 에 썼다. 새 기본 폴더가 아직 없고 그 옛 폴더에 이 입력의 기록이 있으면
        --work 로 이어 쓰라고 한 줄 알린다(저절로 옮겨 쓰지 않는다). 같은 이름의 PDF 결과는 가리키지 않는다."""
        import contextlib, tempfile
        from yeone_ocr import cli
        cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as td:
            os.chdir(td)
            try:
                os.makedirs("book")
                for n in ("scan.png", "doc.png", os.path.join("book", "p1.png")):
                    Image.new("L", (60, 80), 255).save(n)
                for d, f in (("scan", "scan.png"), ("book", "book"), ("doc", "doc.pdf")):        # 예전 판의 결과 폴더(doc 은 같은 이름의 PDF 것)
                    os.makedirs(os.path.join("work", d, "readings"))
                    Path("work", d, "state.json").write_text(json.dumps({"file": f, "pages": {}}), encoding="utf-8")
                def run(*a):
                    buf = io.StringIO()
                    with contextlib.redirect_stdout(buf):
                        self.assertEqual(cli.main(["report", *a]), 0)
                    return buf.getvalue()
                self.assertNotIn("예전 판", run("scan.png", "--work", "elsewhere"))           # --work 를 주면 알리지 않는다
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    self.assertEqual(cli.main(["inspect", "scan.png"]), 0)
                hint = [l for l in buf.getvalue().splitlines() if "예전 판" in l]                  # 결과 폴더를 만들지 않는 명령에도 맞는 말로, 이름의 받침과 상관없는 조사로
                self.assertEqual(hint, ["참고: 예전 판의 결과 폴더가 있습니다: " + os.path.join("work", "scan") + ' — 그 판독 기록을 이어 쓰려면 --work "'
                                        + os.path.join("work", "scan") + '" 을(를) 붙이십시오.'])
                out = run("scan.png")
                self.assertEqual(out.count("예전 판"), 1); self.assertIn('--work "' + os.path.join("work", "scan") + '"', out)
                self.assertTrue(os.path.isdir(os.path.join("work", "scan_png")))              # 새 기본 폴더로 간다(저절로 옮기지 않음)
                self.assertFalse(os.path.exists(os.path.join("work", "scan", "report.md")))
                self.assertNotIn("예전 판", run("scan.png"))                                   # 새 폴더가 생긴 뒤에는 알리지 않는다
                self.assertIn('--work "' + os.path.join("work", "book") + '"', run("book"))
                self.assertNotIn("예전 판", run("doc.png"))                                    # work/doc 은 doc.pdf 의 결과
            finally:
                os.chdir(cwd)

    def test_memo_tips_name_real_options(self):
        """도움말·힌트가 '모든 표시에 …' 로 가리키는 선택지가 실제 메모 콤보 선택지에 있다."""
        import re
        from yeone_ocr import gui
        from yeone_ocr.collate.labels import MEMO_MODES
        quoted = re.findall(r"'(모든 표시에[^']*)'", Path(gui.__file__).read_text(encoding="utf-8"))
        self.assertTrue(quoted)
        for q in quoted:
            self.assertTrue(any(v.startswith(q) for v in MEMO_MODES.values()), q)

    def test_upgrade_old_config_adopts_speed_defaults(self):
        from yeone_ocr.gui import upgrade_config, DEFAULTS, SPEED_DEFAULTS_VERSION
        old = dict(DEFAULTS); old.update({"workers": 3, "second_model": "", "checkpoint": 20}); old.pop("speed_defaults", None)
        up = upgrade_config(old)
        self.assertEqual(up["workers"], 8); self.assertEqual(up["second_model"], "auto")                  # 0.6.8: 동시 기본 8
        self.assertEqual(up["speed_defaults"], SPEED_DEFAULTS_VERSION)
        six = dict(DEFAULTS); six.update({"workers": 6, "collate_policy": "all", "speed_defaults": 5})    # 0.6.7 까지의 기본값(6·all)은 새 기본값으로
        self.assertEqual((upgrade_config(six)["workers"], upgrade_config(six)["collate_policy"]), (8, "sure"))
        four = dict(DEFAULTS); four.update({"workers": 4, "collate_policy": "memo", "speed_defaults": 5})  # 직접 고른 값은 그대로
        self.assertEqual((upgrade_config(four)["workers"], upgrade_config(four)["collate_policy"]), (4, "memo"))
        # 사용자가 직접 바꾼 값은 유지
        mine = dict(DEFAULTS); mine.update({"workers": 5, "second_model": "gemini-2.5-flash"}); mine.pop("speed_defaults", None)
        up2 = upgrade_config(mine)
        self.assertEqual(up2["workers"], 5); self.assertEqual(up2["second_model"], "gemini-2.5-flash")
        # 이미 새 판이면 손대지 않음
        new = {"speed_defaults": SPEED_DEFAULTS_VERSION, "workers": 2, "second_model": ""}
        up3 = upgrade_config(new)
        self.assertEqual(up3["workers"], 2); self.assertEqual(up3["second_model"], "")
        # 0.6.2: 조각 고해상 켬/끔 → 3단. load_config 처럼 DEFAULTS(media_res=high) 를 먼저 섞어도 '끔' 이 '기본' 으로 간다
        off = dict(DEFAULTS); off.update({"media_high": False, "speed_defaults": 4}); on = dict(DEFAULTS); on.update({"media_high": True, "speed_defaults": 4})
        self.assertEqual((upgrade_config(off)["media_res"], upgrade_config(on)["media_res"]), ("default", "high"))
        self.assertNotIn("media_high", upgrade_config(off))
        keep = dict(DEFAULTS); keep.update({"media_res": "ultra_high", "speed_defaults": SPEED_DEFAULTS_VERSION})
        self.assertEqual(upgrade_config(keep)["media_res"], "ultra_high")
        self.assertEqual(upgrade_config({"media_res": "weird", "speed_defaults": SPEED_DEFAULTS_VERSION})["media_res"], "high")


class CheckList(unittest.TestCase):
    def _rec(self):
        return {"file": "x.pdf", "pdf_page": 5, "reader": "test", "verify": [], "crops": {},
                "merged": PageReading(pdf_page=5, print_page="34", regions=[
                    Region(id="b", kind="본문", paragraphs=[Paragraph(columns=[Column(col_from_right=1, text="三層 六十〔睿/透〕卦 山은 〔疑: 龍〕이다")])])],
                    uncertain=[Uncertain(region_id="b", col_from_right=1, position="아래", context="「層六十〔睿/透〕卦」"),
                               Uncertain(region_id="b", col_from_right=1, position="위", context="「山은 〔疑: 龍〕이다」"),
                               Uncertain(region_id="", context="어느 영역에도 들지 않은 글 기둥 (x 0.81~0.82, y 0.13~0.32) — 원본 확인"),
                               Uncertain(region_id="b", col_from_right=2, context="「〔疑: 針〕」 — 본문에 표시 못 함")]).model_dump()}

    def test_default_is_clean_document(self):
        blocks = assemble_blocks([self._rec()])
        self.assertEqual([b.type for b in blocks], ["meta", "para"])                 # 확인 목록·건수 없이 OCR 본문만

    def test_extra_keeps_only_items_not_shown_inline(self):
        unc = [b for b in assemble_blocks([self._rec()], check_list="extra") if b.type == "uncertain"]
        self.assertEqual(len(unc), 1)
        self.assertEqual(len(unc[0].items), 2)                                  # 글 기둥 + 본문에 없는 〔疑: 針〕
        self.assertTrue(unc[0].items[0].startswith("어느 영역에도"))
        self.assertIn("본문에 표시하지 못한", unc[0].text)

    def test_all_and_none(self):
        unc = [b for b in assemble_blocks([self._rec()], check_list="all") if b.type == "uncertain"]
        self.assertEqual(len(unc[0].items), 4); self.assertEqual(unc[0].text, "[판독 확인 필요]")
        self.assertEqual([b for b in assemble_blocks([self._rec()], check_list="none") if b.type == "uncertain"], [])
        rec = self._rec(); rec["merged"]["uncertain"] = rec["merged"]["uncertain"][:2]   # 전부 본문에 표시된 쪽 → extra 에서도 목록 자체가 없음
        self.assertEqual([b for b in assemble_blocks([rec], check_list="extra") if b.type == "uncertain"], [])
        with self.assertRaises(ValueError): assemble_blocks([self._rec()], check_list="some")

    def test_builds_without_check_block(self):
        with tempfile.TemporaryDirectory() as td:
            rec = self._rec(); rec["merged"]["uncertain"] = rec["merged"]["uncertain"][:2]
            out = os.path.join(td, "o.hwpx"); st = build_hwpx(assemble_blocks([rec]), out)
            self.assertEqual(st["uncertain_items"], 0); self.assertGreaterEqual(st["marks"], 2); self.assertEqual(check_hwpx_file(out), [])


class ReviewOverlay(unittest.TestCase):
    def test_boxes_from_regions_and_orphans(self):
        from yeone_ocr.review_html import overlay_boxes, orphan_boxes
        from yeone_ocr.cli import orphan_message
        msg = orphan_message((0.48, 0.10, 0.49, 0.48))
        self.assertIn("쪽 가운데 위", msg); self.assertIn("가로 48~49%, 세로 10~48%", msg)
        pr = PageReading(pdf_page=1, regions=[Region(id="r1", kind="본문", bbox=[100, 200, 900, 800]), Region(id="f1", kind="원형나경도", bbox=[50, 50, 400, 500])],
                         uncertain=[Uncertain(context=msg), Uncertain(context="어느 영역에도 들지 않은 글 기둥 (x 0.81~0.82, y 0.13~0.32) — 원본 확인")])
        self.assertEqual(orphan_boxes(pr), [(0.48, 0.10, 0.49, 0.48), (0.81, 0.13, 0.82, 0.32)])
        h = overlay_boxes(pr)
        self.assertEqual(h.count("class='box"), 4); self.assertIn("img-kind", h); self.assertIn("영역 밖 글줄 2", h)
        self.assertIn("left:20.00%;top:10.00%;width:60.00%;height:80.00%", h)      # r1: bbox [y0 100, x0 200, y1 900, x1 800]


class TextlayerFold(unittest.TestCase):
    """0.6.0 — 글자층 대조에서 부호 표기와 〔〕 표시는 차이로 세지 않는다(중의역대약론선 실측: 글자층 차이의 60%가 ，↔, 였다)."""
    def test_punctuation_and_marks_are_folded(self):
        from yeone_ocr.merge import compare_textlayer
        from yeone_ocr.schema import PageReading, Region, Paragraph, Column
        pr = PageReading(pdf_page=1, regions=[Region(id="b", kind="본문", paragraphs=[Paragraph(columns=[
            Column(col_from_right=1, text="冬瓜，性凉。治水肿〔疑: 、〕小便不利；能〔人/入〕阴分")])])])
        d = compare_textlayer(pr, "冬瓜,性凉.治水肿、小便不利;能人阴分")
        self.assertEqual(d["items"], [], d)
        self.assertEqual(d["ratio"], 1.0)
        d2 = compare_textlayer(pr, "冬瓜,性温.治水肿、小便不利;能人阴分")       # 진짜 다른 글자는 그대로 잡힌다
        self.assertEqual([(i["reading"], i["layer"]) for i in d2["items"]], [("凉", "温")])


class Chunking(unittest.TestCase):
    """0.6.0 — 2차 조각: 줄 공백에 스냅, 줄 묶음으로 나누기."""
    def _page(self, lines=12, line_h=34, gap=25, W=900, margin=60):
        from PIL import Image, ImageDraw
        H = margin * 2 + lines * (line_h + gap)
        im = Image.new("L", (W, H), 255); d = ImageDraw.Draw(im)
        tops = []
        for k in range(lines):
            y = margin + k * (line_h + gap); tops.append(y)
            d.rectangle((margin, y, W - margin, y + line_h), fill=0)      # 글줄 흉내(검은 띠)
        return im, tops, line_h, gap

    def test_snap_to_gaps_keeps_out_neighbour_lines(self):
        from yeone_ocr.layout import snap_to_gaps
        im, tops, lh, gap = self._page()
        W, H = im.size
        # 모델 bbox 가 3번째 줄 한 줄인데 위·아래로 이웃 줄 글자 반쯤까지 걸쳐 있는 경우
        y0 = (tops[2] - gap - lh * 0.4) / H; y1 = (tops[2] + lh + gap + lh * 0.4) / H
        fr = snap_to_gaps(im, (0.05, y0, 0.95, y1), axis="y")
        self.assertGreater(fr[1] * H, tops[1] + lh)            # 위 변이 2번째 줄 아래 공백 안
        self.assertLess(fr[3] * H, tops[3])                     # 아래 변이 4번째 줄 위 공백 안
        self.assertLess(fr[1] * H, tops[2]); self.assertGreater(fr[3] * H, tops[2] + lh)   # 3번째 줄은 온전히 포함

    def test_split_bands_groups_lines(self):
        from yeone_ocr.layout import split_bands
        im, tops, lh, gap = self._page(lines=12)
        bands = split_bands(im, axis="y", max_bands=5)
        self.assertEqual(len(bands), 3)                          # 12줄 → 5+5+2… 끝 묶음이 한 줄이면 앞에 붙이므로 5+5+2
        self.assertEqual(bands[0][0], 0); self.assertEqual(bands[-1][1], im.height)
        for (a, b), (c, d) in zip(bands, bands[1:]):
            self.assertEqual(b, c)                               # 이어 붙으면 조각 전체
        # 경계가 글줄을 자르지 않는다
        for a, b in bands:
            for t in tops:
                self.assertFalse(a < t < b and not (a < t and t + lh < b) and (a < t + lh), f"경계 {a}~{b} 가 줄 {t}~{t + lh} 를 자름")
        self.assertEqual(split_bands(im, axis="y", max_bands=20), [(0, im.height)])       # 줄 수가 적으면 하나


class SecondPassChunks(unittest.TestCase):
    """0.6.0 — 긴 영역을 줄 묶음으로 나눠 2차 판독하고 열을 이어 붙여 되돌린다."""
    def test_chunks_are_recombined_in_order(self):
        from argparse import Namespace
        from PIL import Image, ImageDraw
        from yeone_ocr import prompts
        from yeone_ocr.cli import _second_pass
        from yeone_ocr.schema import PageReading, Region, Paragraph, Column, RegionReread
        lines, lh, gap, W, margin = 12, 34, 25, 900, 60
        H = margin * 2 + lines * (lh + gap)
        im = Image.new("L", (W, H), 255); d = ImageDraw.Draw(im)
        for k in range(lines):
            y = margin + k * (lh + gap); d.rectangle((margin, y, W - margin, y + lh), fill=0)
        y0 = margin / H; y1 = (margin + lines * (lh + gap) - gap) / H
        reg = Region(id="r1", kind="본문", bbox=[round(y0 * 1000), round(margin / W * 1000), round(y1 * 1000), round((W - margin) / W * 1000)],
                     paragraphs=[Paragraph(columns=[Column(col_from_right=i + 1, text=f"줄{i + 1}") for i in range(lines)])])
        p1 = PageReading(pdf_page=1, regions=[reg])

        class Fake:
            """조각 id 와 조각 높이를 기록하고, 조각마다 열 두 개를 돌려준다."""
            def __init__(self): self.seen = []
            def reread_regions(self, items, n):
                out = {}
                for rid, kind, img in items:
                    self.seen.append((rid, img.size))
                    out[rid] = RegionReread(columns=[Column(col_from_right=1, text=f"{rid}a"), Column(col_from_right=2, text=f"{rid}b")])
                return out
            def reread_region(self, im, kind, rid, n):
                self.seen.append((rid, im.size))                 # 영역이 하나뿐이면 낱개 호출로 간다
                return RegionReread(columns=[Column(col_from_right=1, text=f"{rid}a")])

        old = prompts.MODE; prompts.set_mode("horizontal_zh")
        try:
            fake = Fake()
            rereads, meta = _second_pass(fake, im, p1, 1, Namespace(second_mode="batch", chunk_lines=5))
        finally:
            prompts.set_mode(old)
        self.assertEqual([rid for rid, _ in fake.seen], ["r1#1", "r1#2", "r1#3"])        # 12줄 → 5+5+2
        self.assertIn("r1", rereads); self.assertNotIn("r1#1", rereads)
        cols = rereads["r1"].columns
        self.assertEqual([c.text for c in cols], ["r1#1a", "r1#1b", "r1#2a", "r1#2b", "r1#3a", "r1#3b"])
        self.assertEqual([c.col_from_right for c in cols], [1, 2, 3, 4, 5, 6])              # 열 번호는 1부터 다시
        self.assertEqual(meta["r1"]["chunks"], 3); self.assertEqual(meta["r1"]["chunks_read"], 3)
        # 조각 높이가 줄 수에 비례한다(5·5·2줄): 마지막 조각이 첫 조각의 절반보다 낮다
        hs = [sz[1] for _, sz in fake.seen]; self.assertLess(hs[2], hs[0] / 2, hs)
        # 나누지 않을 때(chunk_lines=0)는 영역 그대로 한 장
        fake2 = Fake(); prompts.set_mode("horizontal_zh")
        try:
            rereads2, meta2 = _second_pass(fake2, im, p1, 1, Namespace(second_mode="batch", chunk_lines=0))
        finally:
            prompts.set_mode(old)
        self.assertEqual([rid for rid, _ in fake2.seen], ["r1"]); self.assertEqual(meta2["r1"]["chunks"], 1)


class Adjudicate(unittest.TestCase):
    """0.6.0 — 3차 글자 판별: 〔a/b〕 자리 찾기, 글자 창 좌표, 확정 적용, 다시 계산 뒤 유지."""
    def _rec(self):
        from yeone_ocr.schema import PageReading, Region, Paragraph, Column, Uncertain
        reg = Region(id="r1", kind="본문", bbox=[100, 50, 300, 950], paragraphs=[Paragraph(columns=[
            Column(col_from_right=1, text="行通乎小肠，能〔入/人〕阴分，治有形之病"),
            Column(col_from_right=2, text="色赤〔入/人〕心，性下行而通"),
            Column(col_from_right=3, text="唐容川〔曰/日〕：物下极则反上")])])
        pr = PageReading(pdf_page=430, regions=[reg], uncertain=[Uncertain(region_id="r1", col_from_right=1, context="「，能〔入/人〕阴分」")])
        return {"pdf_page": 430, "pass1": pr.model_dump(), "merged": pr.model_dump()}

    def test_find_sites_and_boxes(self):
        from yeone_ocr.adjudicate import find_sites, glyph_box
        from yeone_ocr.schema import PageReading
        sites = find_sites(PageReading.model_validate(self._rec()["merged"]))
        self.assertEqual([(s.a, s.b, s.col_index, s.char_index) for s in sites], [("入", "人", 0, 7), ("入", "人", 1, 2), ("曰", "日", 2, 3)])
        self.assertEqual(sites[0].pre, "小肠，能"); self.assertEqual(sites[0].post, "阴分，治")
        box = glyph_box([100, 50, 300, 950], sites[0], horizontal=True)
        self.assertTrue(0.05 <= box[0] < box[2] <= 0.95 and 0.0 <= box[1] < box[3] <= 0.3, box)      # 첫 줄 → 영역 위쪽, 글자 7번째 → 왼쪽
        box3 = glyph_box([100, 50, 300, 950], sites[2], horizontal=True)
        self.assertGreater(box3[1], box[1])                                                          # 셋째 줄은 더 아래
        vbox = glyph_box([100, 50, 300, 950], sites[0], horizontal=False)
        self.assertGreater(vbox[0], 0.5); self.assertGreaterEqual(vbox[2], 0.95)                      # 세로쓰기면 첫 열은 오른쪽 셋째

    def test_adjudicate_page_applies_choice_and_survives_remerge(self):
        from PIL import Image
        from yeone_ocr.adjudicate import adjudicate_page, apply_adjudications
        from yeone_ocr.schema import PageReading, GlyphChoice, region_text
        rec = self._rec(); img = Image.new("L", (900, 1300), 255)

        class Fake:
            """판독기는 앞뒤 글자를 받지 못한다(눈가림) — 자리 차례로 답한다: 1번 人, 2번 入, 3번(曰/日) 못 가림."""
            def __init__(self): self.calls = []
            def adjudicate(self, crop, ask, horizontal, pdf_page):
                self.calls.append((ask, crop.size))
                n = len(self.calls)
                if n == 1:
                    return GlyphChoice(choice="人", evidence="왼쪽 삐침이 위에서 시작")       # 인쇄가 人인 자리
                if n == 2:
                    return GlyphChoice(choice="入", evidence="오른쪽 삐침이 위로 솟음")
                return GlyphChoice(choice="", evidence="획이 뭉개짐")                           # 曰/日 은 못 가림
        fake = Fake()
        rec = adjudicate_page(rec, img, fake, horizontal=True)
        self.assertEqual(len(fake.calls), 3); self.assertTrue(all(sz[0] > 4 and sz[1] > 4 for _, sz in fake.calls))
        asks = [a for a, _ in fake.calls]
        self.assertEqual([set(a.candidates) for a in asks], [{"入", "人"}, {"入", "人"}, {"曰", "日"}])
        self.assertTrue(all(a.pos == 3 and not a.measured and not a.tick for a in asks))                 # 균등 분할: 어림 순번(앞 두 글자 뒤), 눈금 없음
        self.assertFalse(any(hasattr(a, "pre") or hasattr(a, "post") for a in asks))                  # 질문에 앞뒤 글자 칸이 없다
        m = PageReading.model_validate(rec["merged"]); body = region_text(m.regions[0])
        self.assertIn("能人阴分", body); self.assertIn("色赤入心", body); self.assertIn("〔曰/日〕", body)
        st = rec["adjudicate_stats"]
        self.assertEqual({k: st[k] for k in ("sites", "decided", "undecided", "a", "b", "failed")}, {"sites": 3, "decided": 2, "undecided": 1, "a": 1, "b": 1, "failed": 0})
        self.assertEqual(st["loc"], {"uniform": 3})                                                   # 빈 쪽 이미지·글자층 없음 → 균등 분할
        self.assertTrue(all(a["loc"] == "uniform" for a in rec["adjudications"]))
        self.assertTrue(any("3차 판별: 「人」 확정" in u.context for u in m.uncertain))
        # 비교 다시 계산(원본 pass1 에서 다시 병합) 뒤에도 판별 결과를 다시 적용할 수 있다
        fresh = PageReading.model_validate(rec["pass1"])
        self.assertEqual(apply_adjudications(fresh, rec["adjudications"]), 2)
        self.assertIn("能人阴分", region_text(fresh.regions[0])); self.assertIn("〔曰/日〕", region_text(fresh.regions[0]))

    def test_prompt_has_glyph_hint(self):
        from yeone_ocr.prompts import adjudicate_prompt
        from yeone_ocr.schema import GlyphAsk
        t = adjudicate_prompt(GlyphAsk(candidates=("入", "人"), pos=3, count=5, measured=True, tick=True), True, 430)
        self.assertIn("入", t); self.assertIn("자형 단서", t); self.assertIn("왼쪽에서 3번째", t); self.assertIn("눈금", t)


def _site_ctx(rec):
    """자리 번호(1부터) → (앞 글자, 뒤 글자). 판독기는 앞뒤 글자를 받지 못하니(눈가림) 가짜 판독기가 창(w)을 꾸밀 때 테스트가 직접 꺼낸다."""
    from yeone_ocr.adjudicate import find_sites
    from yeone_ocr.schema import PageReading
    return {str(i): (s.pre, s.post) for i, s in enumerate(find_sites(PageReading.model_validate(rec["merged"])), 1)}


class AdjudicateBatch(unittest.TestCase):
    """0.6.2 — 3차 판별 묶음 호출·낱개 재질문·보완 판독기·글자 자리 실측."""
    def _rec(self):
        return Adjudicate._rec(self)

    def test_batch_then_singles_and_fallback(self):
        from PIL import Image
        from yeone_ocr.adjudicate import adjudicate_page
        from yeone_ocr.schema import PageReading, GlyphChoice, region_text
        rec = self._rec(); img = Image.new("L", (900, 1300), 255)

        class Batch:
            """묶음 호출: 1번 자리는 답하고, 2번은 창(w)이 엉뚱하게(조각 번호 섞임), 3번은 빠뜨린다. 낱개 호출은 2번(入/人)만 가린다."""
            model = "flash"
            def __init__(self): self.many = []; self.single = []
            def adjudicate_many(self, crops, horizontal, pdf_page):
                self.many.append([sid for sid, *_ in crops])
                return {"1": GlyphChoice(choice="人", evidence="삐침", window="能人阴"), "2": GlyphChoice(choice="入", evidence="x", window="唐容川曰")}
            def adjudicate(self, crop, ask, horizontal, pdf_page):
                self.single.append(set(ask.candidates))
                if "入" in ask.candidates:
                    return GlyphChoice(choice="入", evidence="솟음", window="色赤入心")
                return GlyphChoice(choice="", evidence="뭉개짐", window="")

        class Pro:
            model = "pro"
            def __init__(self): self.calls = []
            def adjudicate(self, crop, ask, horizontal, pdf_page):
                self.calls.append(set(ask.candidates))
                return GlyphChoice(choice="曰", evidence="가로획이 닿지 않음", window="唐容川曰：")

        b, p = Batch(), Pro()
        rec = adjudicate_page(rec, img, b, horizontal=True, batch=8, fallback_reader=p)
        self.assertEqual(b.many, [["1", "2", "3"]])                                   # 한 호출에 세 자리
        self.assertEqual(b.single, [{"入", "人"}, {"曰", "日"}])                         # 2번(창 불일치)·3번(빠짐)만 낱개
        self.assertEqual(p.calls, [{"曰", "日"}])                                      # 첫 판독기가 못 가린 3번만 보완 판독기로
        m = PageReading.model_validate(rec["merged"]); body = region_text(m.regions[0])
        self.assertIn("能人阴分", body); self.assertIn("色赤入心", body); self.assertIn("唐容川曰：", body)
        st = rec["adjudicate_stats"]
        self.assertEqual((st["sites"], st["decided"], st["undecided"], st["batched"], st["fallback_decided"]), (3, 3, 0, 1, 1))
        models = [a["model"] for a in rec["adjudications"]]
        self.assertEqual(models, ["flash", "flash", "pro"])

    def test_batch_size_one_means_singles_and_fatal_propagates(self):
        from PIL import Image
        from yeone_ocr.adjudicate import adjudicate_page
        from yeone_ocr.schema import GlyphChoice
        from yeone_ocr.reader_gemini import ReaderError

        class Single:
            model = "m"
            def __init__(self): self.n = 0
            def adjudicate_many(self, *a, **k): raise AssertionError("batch=1 이면 묶음을 부르지 않는다")
            def adjudicate(self, *a, **k):
                self.n += 1; return GlyphChoice(choice="", evidence="")
        s = Single(); rec = adjudicate_page(self._rec(), Image.new("L", (900, 1300), 255), s, True, batch=1)
        self.assertEqual(s.n, 3); self.assertEqual(rec["adjudicate_stats"]["undecided"], 3)

        class Fatal:
            model = "m"
            def adjudicate_many(self, *a, **k): raise ReaderError("키가 틀림", fatal=True)
            def adjudicate(self, *a, **k): raise ReaderError("키가 틀림", fatal=True)
        with self.assertRaises(ReaderError):
            adjudicate_page(self._rec(), Image.new("L", (900, 1300), 255), Fatal(), True)

        class Flaky:
            model = "m"
            def adjudicate(self, *a, **k): raise ReaderError("5회 시도 뒤에도 실패", fatal=False)
        rec = adjudicate_page(self._rec(), Image.new("L", (900, 1300), 255), Flaky(), True, batch=1)
        self.assertEqual(rec["adjudicate_stats"]["failed"], 3)
        self.assertTrue(all(a["evidence"].startswith("판별 실패") for a in rec["adjudications"]))

    def test_apply_choice_targets_the_right_duplicate_mark(self):
        """같은 열에 같은 표시가 둘일 때 앞 자리가 미정이고 뒤 자리만 확정되면 뒤 자리를 바꾼다(이전엔 첫 표시를 바꿨다)."""
        from yeone_ocr.adjudicate import find_sites, apply_choice, apply_adjudications
        from yeone_ocr.schema import PageReading, Region, Paragraph, Column, region_text
        reg = Region(id="r1", kind="본문", bbox=[100, 50, 300, 950], paragraphs=[Paragraph(columns=[Column(col_from_right=1, text="甲〔入/人〕乙丙〔入/人〕丁")])])
        pr = PageReading(pdf_page=1, regions=[reg]); sites = find_sites(pr)
        self.assertEqual([s.char_index for s in sites], [1, 4])
        self.assertTrue(apply_choice(pr, sites[1], "人")); self.assertEqual(region_text(pr.regions[0]), "甲〔入/人〕乙丙人丁")
        self.assertTrue(apply_choice(pr, sites[0], "入")); self.assertEqual(region_text(pr.regions[0]), "甲入乙丙人丁")
        fresh = PageReading(pdf_page=1, regions=[Region(id="r1", kind="본문", bbox=[100, 50, 300, 950], paragraphs=[Paragraph(columns=[Column(col_from_right=1, text="甲〔入/人〕乙丙〔入/人〕丁")])])])
        from dataclasses import asdict
        self.assertEqual(apply_adjudications(fresh, [{**asdict(sites[1]), "choice": "人"}]), 1)
        self.assertEqual(region_text(fresh.regions[0]), "甲〔入/人〕乙丙人丁")

    def test_same_marks_after_length_change_keep_their_choices(self):
        """한 차례에서 길이가 바뀐 선택(〔效效/先〕→先) 뒤의 같은 표시 둘(〔再/後〕〔再/後〕)이 뒤바뀌지 않는다 — 판별 때도, 다시 적용할 때도.
        기록마다 3차 차례 번호(run)를 적고, 다시 돌리면 다음 번호를 적는다(run 이 없는 예전 기록은 어림한 차례 다음)."""
        from PIL import Image
        from yeone_ocr.adjudicate import adjudicate_page, apply_adjudications
        from yeone_ocr.grades import decided_positions
        from yeone_ocr.schema import PageReading, Region, Paragraph, Column, GlyphChoice
        pr = PageReading(pdf_page=1, regions=[Region(id="r1", kind="본문", bbox=[100, 50, 300, 950], paragraphs=[Paragraph(columns=[
            Column(col_from_right=1, text="〔效效/先〕〔再/後〕〔再/後〕速此虛其")])])])
        img = Image.new("L", (900, 1300), 255); text = lambda p: p.regions[0].paragraphs[0].columns[0].text

        class R:
            """눈가림 판독기: 자리 차례대로 정해 둔 답을 낸다(빈 답 = 못 가림)."""
            model = "m"
            def __init__(self, answers): self.answers = list(answers)
            def adjudicate(self, crop, ask, horizontal, pdf_page):
                return GlyphChoice(choice=self.answers.pop(0), evidence="획")
        rec = adjudicate_page({"pdf_page": 1, "pass1": pr.model_dump(), "merged": pr.model_dump()}, img, R(["先", "再", "後"]), True)
        m = PageReading.model_validate(rec["merged"]); self.assertEqual(text(m), "先再後速此虛其")
        self.assertEqual([a["run"] for a in rec["adjudications"]], [1, 1, 1])
        found, missing = decided_positions(m, rec["adjudications"])
        self.assertEqual(([(f.start, f.entry["choice"]) for f in found], missing), ([(0, "先"), (1, "再"), (2, "後")], []))
        fresh = PageReading.model_validate(rec["pass1"])                                          # 비교 다시 계산 뒤 다시 적용해도 같은 글
        self.assertEqual(apply_adjudications(fresh, rec["adjudications"]), 3); self.assertEqual(text(fresh), "先再後速此虛其")
        # 다시 돌린 쪽: 앞 차례(run 없는 예전 기록)는 先 만, 다음 차례는 둘째 〔再/後〕 만 정함 → 앞 차례의 길이 차를 다음 차례에 다시 더하지 않는다
        rec = adjudicate_page({"pdf_page": 1, "pass1": pr.model_dump(), "merged": pr.model_dump()}, img, R(["先", "", ""]), True)
        for a in rec["adjudications"]:
            a.pop("run")
        rec = adjudicate_page(rec, img, R(["", "後"]), True)
        self.assertEqual([a.get("run") for a in rec["adjudications"]], [None, None, None, 2, 2])
        m = PageReading.model_validate(rec["merged"]); self.assertEqual(text(m), "先〔再/後〕後速此虛其")
        fresh = PageReading.model_validate(rec["pass1"])
        self.assertEqual(apply_adjudications(fresh, rec["adjudications"]), 2); self.assertEqual(text(fresh), "先〔再/後〕後速此虛其")
        found, missing = decided_positions(m, rec["adjudications"])
        self.assertEqual(([(f.start, f.entry["choice"]) for f in found], missing), ([(0, "先"), (6, "後")], []))

    def test_batch_failure_does_not_fan_out_and_missing_site_id_is_dropped(self):
        from PIL import Image
        from yeone_ocr.adjudicate import adjudicate_page
        from yeone_ocr.schema import MultiGlyphChoice
        from yeone_ocr.reader_gemini import ReaderError

        class Quota:
            model = "m"
            def __init__(self): self.single = 0
            def adjudicate_many(self, *a, **k): raise ReaderError("5회 시도 뒤에도 실패: 한도", fatal=False)
            def adjudicate(self, *a, **k): self.single += 1; raise AssertionError("묶음이 실패하면 낱개로 번지지 않는다")
        q = Quota(); rec = adjudicate_page(self._rec(), Image.new("L", (900, 1300), 255), q, True, batch=8)
        self.assertEqual(q.single, 0); self.assertEqual(rec["adjudicate_stats"]["failed"], 3)
        self.assertTrue(all(a["evidence"].startswith("판별 실패: 묶음 호출 실패") for a in rec["adjudications"]))
        r = MultiGlyphChoice.model_validate_json('{"items":[{"s":"1","c":"入"},{"c":"人"}]}')         # 자리 번호가 빠진 항목은 응답을 깨지 않고 버려진다
        self.assertEqual([i.site_id for i in r.items], ["1", ""])

    def test_batch_undecided_with_foreign_window_is_reasked(self):
        """묶음 응답이 c="" 이어도 보인 글(w)이 앞뒤 글자와 전혀 안 맞으면 조각 번호가 섞인 것이라 낱개로 다시 묻는다."""
        from PIL import Image
        from yeone_ocr.adjudicate import adjudicate_page
        from yeone_ocr.schema import GlyphChoice
        class R:
            model = "m"
            def __init__(self): self.single = []
            def adjudicate_many(self, crops, h, p):
                return {"1": GlyphChoice(choice="", evidence="", window="全然다른글"), "2": GlyphChoice(choice="", evidence="뭉개짐", window="色赤?心"), "3": GlyphChoice(choice="", evidence="", window="")}
            def adjudicate(self, crop, ask, h, p):
                self.single.append(set(ask.candidates)); return GlyphChoice(choice="人", evidence="획", window="能人阴")
        r = R(); rec = adjudicate_page(self._rec(), Image.new("L", (900, 1300), 255), r, True, batch=8)
        self.assertEqual(r.single, [{"入", "人"}])                                                  # 1번만 다시(2번은 창이 맞는 진짜 미정, 3번은 창이 비어 검사 불가)
        st = rec["adjudicate_stats"]; self.assertEqual((st["decided"], st["undecided"], st["batched"], st["batch_rechecked"]), (1, 2, 2, 1))

    def test_out_of_candidate_answer_is_undecided_and_goes_to_fallback(self):
        """후보 밖 글자(八)를 답하면 미정으로 다뤄 보완 판독기에 넘긴다. 보완 호출이 실패하면 첫 판독기의 답(근거)을 남긴다."""
        from PIL import Image
        from yeone_ocr.adjudicate import adjudicate_page
        from yeone_ocr.schema import GlyphChoice
        from yeone_ocr.reader_gemini import ReaderError
        ctx = _site_ctx(self._rec())
        class Flash:
            model = "flash"
            def adjudicate_many(self, crops, h, p): return {sid: GlyphChoice(choice="八", evidence="엉뚱", window=ctx[sid][0][-1:] + "八" + ctx[sid][1][:1]) for sid, _, _ in crops}
            def adjudicate(self, *a, **k): raise AssertionError("창이 맞으니 낱개 재질문은 없다")
        class Pro:
            model = "pro"
            def __init__(self): self.n = 0
            def adjudicate_many(self, crops, h, p):
                self.n += 1; return {sid: GlyphChoice(choice=a.candidates[0], evidence="획", window=ctx[sid][0][-1:] + a.candidates[0] + ctx[sid][1][:1]) for sid, _, a in crops}
        pro = Pro(); rec = adjudicate_page(self._rec(), Image.new("L", (900, 1300), 255), Flash(), True, batch=8, fallback_reader=pro)
        st = rec["adjudicate_stats"]; self.assertEqual((pro.n, st["decided"], st["fallback_decided"], st["undecided"]), (1, 3, 3, 0))
        self.assertEqual({a["model"] for a in rec["adjudications"]}, {"pro"})
        class ProDown:
            model = "pro"
            def adjudicate_many(self, *a, **k): raise ReaderError("한도", fatal=False)
        rec = adjudicate_page(self._rec(), Image.new("L", (900, 1300), 255), Flash(), True, batch=8, fallback_reader=ProDown())
        st = rec["adjudicate_stats"]; self.assertEqual((st["decided"], st["undecided"], st["failed"]), (0, 3, 0))
        self.assertTrue(all(a["model"] == "flash" and "후보 밖 답(八)" in a["evidence"] and "보완 실패" in a["evidence"] for a in rec["adjudications"]))

    def test_layer_mapping_refuses_index_beyond_line(self):
        """글자층 줄이 열의 끝 글자를 빠뜨렸으면 대응을 포기한다(끝에서 잘린 창으로 엉뚱한 글자를 보지 않게)."""
        from yeone_ocr.adjudicate import _map_index
        self.assertIsNone(_map_index("abcdeX", "abcde", 5)); self.assertIsNone(_map_index("Xabcde", "abcde", 0))
        self.assertEqual(_map_index("abcde", "abcde", 4), 4); self.assertEqual(_map_index("abXde", "abYde", 2), 2)

    def test_stop_skips_remaining_batches(self):
        from PIL import Image
        from yeone_ocr.adjudicate import adjudicate_page
        from yeone_ocr.schema import GlyphChoice
        calls = []
        class R:
            model = "m"
            def adjudicate_many(self, crops, h, p): calls.append(len(crops)); return {sid: GlyphChoice(choice="", evidence="") for sid, *_ in crops}
            def adjudicate(self, *a, **k): calls.append(1); return GlyphChoice(choice="", evidence="")
        rec = adjudicate_page(self._rec(), Image.new("L", (900, 1300), 255), R(), True, batch=2, should_stop=lambda: len(calls) >= 1)
        self.assertEqual(calls, [2])                                                             # 첫 묶음 뒤 중지 → 남은 자리는 묻지 않고 기록도 남기지 않는다
        st = rec["adjudicate_stats"]; self.assertEqual((st["failed"], st["undecided"], st["skipped"], st["batched"]), (0, 2, 1, 2))
        self.assertEqual(len(rec["adjudications"]), 2)

    def test_batches_are_split_evenly(self):
        """9자리를 8장씩 묶으면 5+4 로(8+1 이 아니라). 묶음 수는 ⌈n/batch⌉, 크기는 거의 같고 batch 를 넘지 않는다. batch=2 의 홀수 장은 마지막 하나만 낱개."""
        from PIL import Image
        from yeone_ocr.adjudicate import adjudicate_page, even_chunks
        from yeone_ocr.schema import PageReading, Region, Paragraph, Column, GlyphChoice
        self.assertEqual([len(c) for c in even_chunks(list(range(9)), 8)], [5, 4])
        self.assertEqual([len(c) for c in even_chunks(list(range(17)), 8)], [6, 6, 5])
        self.assertEqual([len(c) for c in even_chunks(list(range(16)), 8)], [8, 8])
        self.assertEqual([len(c) for c in even_chunks(list(range(3)), 2)], [2, 1])
        self.assertEqual(even_chunks(list(range(5)), 8), [[0, 1, 2, 3, 4]]); self.assertEqual(even_chunks([], 8), [])
        self.assertEqual(sum(even_chunks(list(range(9)), 8), []), list(range(9)))                   # 차례는 그대로
        reg = Region(id="r1", kind="본문", bbox=[100, 50, 300, 950], paragraphs=[Paragraph(columns=[
            Column(col_from_right=k + 1, text=f"甲〔入/人〕乙{k}") for k in range(9)])])
        pr = PageReading(pdf_page=1, regions=[reg]); rec = {"pdf_page": 1, "pass1": pr.model_dump(), "merged": pr.model_dump()}

        class R:
            model = "m"
            def __init__(self): self.sizes = []; self.single = 0
            def adjudicate_many(self, crops, h, p):
                self.sizes.append(len(crops)); return {sid: GlyphChoice(choice="", evidence="뭉개짐", window="") for sid, *_ in crops}
            def adjudicate(self, *a, **k): self.single += 1; return GlyphChoice(choice="", evidence="", window="")
        r = R(); out = adjudicate_page(rec, Image.new("L", (900, 1300), 255), r, True, batch=8)
        self.assertEqual((r.sizes, r.single, out["adjudicate_stats"]["batched"]), ([5, 4], 0, 9))
        r = R(); adjudicate_page(dict(rec), Image.new("L", (900, 1300), 255), r, True, batch=4)
        self.assertEqual((r.sizes, r.single), ([3, 3, 3], 0))
        pr3 = PageReading(pdf_page=1, regions=[Region(id="r1", kind="본문", bbox=[100, 50, 300, 950], paragraphs=[Paragraph(columns=[
            Column(col_from_right=k + 1, text=f"甲〔入/人〕乙{k}") for k in range(3)])])])
        r = R(); adjudicate_page({"pdf_page": 1, "pass1": pr3.model_dump(), "merged": pr3.model_dump()}, Image.new("L", (900, 1300), 255), r, True, batch=2)
        self.assertEqual((r.sizes, r.single), ([2], 1))

    def test_apply_choice_requires_neighbour_match(self):
        """같은 열에 같은 표시가 둘이고 뒤 자리만 확정 기록이 있는데, 다시 적용할 때 글이 한 글자 밀려 있으면(사람이 고침·다시 계산)
        글자 번호가 맞는 첫 표시가 아니라 앞뒤 글자가 맞는 둘째 표시를 바꾼다(이전엔 거리만 보고 첫 표시를 바꿨다).
        앞뒤 글자가 하나도 안 맞는 자리뿐이면 손대지 않고 못 찾음으로 센다."""
        from dataclasses import asdict
        from yeone_ocr.adjudicate import find_sites, apply_choice, apply_adjudications
        from yeone_ocr.schema import PageReading, Region, Paragraph, Column, region_text
        def page(text):
            return PageReading(pdf_page=1, regions=[Region(id="r1", kind="본문", bbox=[100, 50, 300, 950], paragraphs=[Paragraph(columns=[Column(col_from_right=1, text=text)])])])
        sites = find_sites(page("甲〔入/人〕〔入/人〕丁"))
        self.assertEqual([(s.char_index, s.pre, s.post) for s in sites], [(1, "甲", "入丁"), (2, "甲入", "丁")])
        shifted = page("X甲〔入/人〕〔入/人〕丁")                                                        # 첫 표시가 기록의 글자 번호(2) 자리에 와 있다
        self.assertTrue(apply_choice(shifted, sites[1], "人")); self.assertEqual(region_text(shifted.regions[0]), "X甲〔入/人〕人丁")
        shifted = page("X甲〔入/人〕〔入/人〕丁"); st = {}
        self.assertEqual(apply_adjudications(shifted, [{**asdict(sites[1]), "choice": "人"}], st), 1)
        self.assertEqual((region_text(shifted.regions[0]), st["unmapped"]), ("X甲〔入/人〕人丁", 0))
        # 제자리의 표시는 이웃이 안 맞아도(이웃 표시가 다른 후보로 확정됨) 받는다 — 단, 이웃이 맞는 다른 자리가 없을 때
        own = page("甲人〔入/人〕丁")                                                                   # 앞 표시가 人 으로 확정된 뒤
        self.assertTrue(apply_choice(own, sites[1], "入")); self.assertEqual(region_text(own.regions[0]), "甲人入丁")
        # 바로 옆 글자가 다른 후보로 확정된 표시였고(〔於/不〕→不) 글이 한 글자 밀렸어도 그 옆 글자(也)가 맞으면 받는다
        s2 = find_sites(page("也〔於/不〕〔此/肝〕"))[1]; self.assertEqual((s2.char_index, s2.pre, s2.post), (2, "也於", ""))
        near = page("X也不〔此/肝〕"); self.assertTrue(apply_choice(near, s2, "肝")); self.assertEqual(region_text(near.regions[0]), "X也不肝")
        # 거리가 가까워도 앞뒤 글자가 하나도 안 맞으면 다른 자리의 표시 — 손대지 않는다
        other = page("乙丙丁〔入/人〕戊"); st = {}
        self.assertFalse(apply_choice(other, sites[1], "人"))
        self.assertEqual(apply_adjudications(other, [{**asdict(sites[1]), "choice": "人"}], st), 0)
        self.assertEqual((region_text(other.regions[0]), st["unmapped"]), ("乙丙丁〔入/人〕戊", 1))
        # 앞뒤 글자 기록이 없는 옛 기록은 가장 가까운 표시(이전 규칙)
        bare = {**asdict(sites[1]), "pre": "", "post": "", "choice": "人"}
        self.assertEqual(apply_adjudications(other, [bare]), 1); self.assertEqual(region_text(other.regions[0]), "乙丙丁人戊")

    def test_apply_choice_keeps_own_site_when_neighbours_were_just_decided(self):
        """열 끝 표시의 바로 앞 두 자리가 같은 차례에서 다른 후보로 정해지면(〔於/不〕〔於/不〕→不不, 〔效效/先〕→先) 기록한 앞 글자가 제자리 옆과
        안 맞아 보여, 같은 열의 미정 〔日/曰〕 을 바꾸고 정한 자리는 표시로 남기던 문제 — 3차 판별 때도, 다시 계산 뒤 다시 적용할 때도."""
        from PIL import Image
        from yeone_ocr.adjudicate import adjudicate_page, apply_adjudications
        from yeone_ocr.schema import PageReading, Region, Paragraph, Column, GlyphChoice
        def page(text):
            return PageReading(pdf_page=1, regions=[Region(id="r1", kind="본문", bbox=[100, 50, 300, 950], paragraphs=[Paragraph(columns=[Column(col_from_right=1, text=text)])])])

        class R:
            model = "m"
            def __init__(self, answers): self.answers = list(answers)
            def adjudicate(self, crop, ask, horizontal, pdf_page): return GlyphChoice(choice=self.answers.pop(0), evidence="획", window="")
        for text, answers, want in (("於〔日/曰〕乙丙丁戊〔於/不〕〔於/不〕〔日/曰〕", ["", "不", "不", "曰"], "於〔日/曰〕乙丙丁戊不不曰"),
                                    ("效〔日/曰〕乙丙丁戊〔效效/先〕〔日/曰〕", ["", "先", "曰"], "效〔日/曰〕乙丙丁戊先曰")):
            pr = page(text)
            rec = adjudicate_page({"pdf_page": 1, "pass1": pr.model_dump(), "merged": pr.model_dump()}, Image.new("L", (900, 1300), 255), R(answers), True, batch=1)
            self.assertEqual(PageReading.model_validate(rec["merged"]).regions[0].paragraphs[0].columns[0].text, want)
            fresh = page(text); st = {}
            self.assertEqual(apply_adjudications(fresh, rec["adjudications"], st), len([a for a in answers if a]))
            self.assertEqual((fresh.regions[0].paragraphs[0].columns[0].text, st["unmapped"]), (want, 0))

    def test_apply_choice_random_runs_change_only_decided_sites(self):
        """무작위 열(같은 표시가 되풀이되고 열 끝에 표시가 몰림, 길이가 바뀌는 후보 〔效效/先〕): 3차를 1~3차례 돌리며 차례마다 무작위 자리만 정한다.
        판별 때 바뀐 글, 원본에 기록을 다시 적용한 글(다시 계산), 차례 안 기록 차례를 섞어 다시 적용한 글이 모두 '정한 자리(참 번호)만 고른 글자,
        미정 자리는 표시 그대로' 와 같아야 한다."""
        import random
        from PIL import Image
        from yeone_ocr.adjudicate import adjudicate_page, apply_adjudications
        from yeone_ocr.schema import PageReading, Region, Paragraph, Column, GlyphChoice
        img = Image.new("L", (300, 900), 255)
        def page(texts):
            return PageReading(pdf_page=1, regions=[Region(id="r1", kind="본문", bbox=[50, 100, 950, 300],
                                                           paragraphs=[Paragraph(columns=[Column(col_from_right=c + 1, text=t) for c, t in enumerate(texts)])])])
        def render(cols, picks):
            return ["".join(t if isinstance(t, str) else picks.get(t[0], f"〔{t[1]}/{t[2]}〕") for t in toks) for toks in cols]
        def texts(p):
            return [c.text for c in p.regions[0].paragraphs[0].columns]

        class R:
            model = "m"
            def __init__(self, answers): self.answers = list(answers)
            def adjudicate(self, crop, ask, horizontal, pdf_page): return GlyphChoice(choice=self.answers.pop(0), evidence="획", window="")
        marks = [("日", "曰"), ("於", "不"), ("效效", "先")]
        for seed in range(120):
            rng = random.Random(seed); cols = []; k = 0
            for _ in range(rng.randint(1, 2)):
                toks = [rng.choice("於日不乙") if rng.random() < 0.5 else None for _ in range(rng.randint(3, 6))] + [None] * rng.randint(1, 3)   # None = 표시, 열 끝에 몰림
                for n, t in enumerate(toks):
                    if t is None:
                        toks[n] = (k, *rng.choice(marks)); k += 1
                cols.append(toks)
            orig = render(cols, {}); rec = {"pdf_page": 1, "pass1": page(orig).model_dump(), "merged": page(orig).model_dump()}; picks = {}
            for _ in range(rng.randint(1, 3)):
                answers = []
                for kk, a, b in (t for toks in cols for t in toks if not isinstance(t, str) and t[0] not in picks):   # find_sites 차례
                    answers.append(rng.choice([a, b]) if rng.random() < 0.6 else "")
                    if answers[-1]:
                        picks[kk] = answers[-1]
                rec = adjudicate_page(rec, img, R(answers), True, batch=1, ticks=False)
                self.assertEqual(texts(PageReading.model_validate(rec["merged"])), render(cols, picks), (seed, orig))
            recs = rec["adjudications"]
            shuffled = [a for r in sorted({a["run"] for a in recs}) for a in rng.sample([a for a in recs if a["run"] == r], sum(a["run"] == r for a in recs))]
            for rs in (recs, shuffled):
                fresh = page(orig); st = {}
                apply_adjudications(fresh, rs, st)
                self.assertEqual((texts(fresh), st["unmapped"]), (render(cols, picks), 0), (seed, orig))

    def test_window_symbols_between_neighbours_are_not_counted(self):
        """조각 검사(_around)는 앞뒤 글자 사이의 글자만 센다. 모델이 w 에 부호를 넣으면(〔曰〕·︑曰︒·▲曰▲) 맞는 창을 '조각 위치가 어긋남' 으로
        버리던 문제. □(못 읽은 글자)는 글자로 센다."""
        from yeone_ocr.adjudicate import window_ok, window_matches
        for w in ("子〔曰〕不", "子︑曰︒不", "子▲曰▲不", "子○曰○不", "子□不", "子 曰 不 亦"):
            self.assertTrue(window_ok(w, "學而子", "日", "不亦說"), w); self.assertTrue(window_matches(w, "學而子", "不亦說"), w)
        for w in ("子□□□不", "子甲乙丙不", "不x子"):
            self.assertFalse(window_ok(w, "學而子", "日", "不亦說"), w); self.assertFalse(window_matches(w, "學而子", "不亦說"), w)

    def test_many_prompt_and_schema(self):
        from yeone_ocr.prompts import adjudicate_many_prompt
        from yeone_ocr.schema import MultiGlyphChoice, GlyphAsk
        t = adjudicate_many_prompt([("1", GlyphAsk(("入", "人"), pos=3, count=5)), ("2", GlyphAsk(("曰", "日"), pos=2, count=4, measured=True))], True, 430)
        self.assertIn("【1】", t); self.assertIn("【2】", t); self.assertIn("자형 단서", t); self.assertIn("어림", t); self.assertIn("2번째", t)
        r = MultiGlyphChoice.model_validate_json('{"items":[{"s":"1","c":"人","e":"x","w":"能人阴"},{"s":"2","c":"","e":"","w":""}]}')
        self.assertEqual([(i.site_id, i.choice) for i in r.items], [("1", "人"), ("2", "")])


class GlyphLocate(unittest.TestCase):
    """0.6.2 — 글자 자리 실측: 글자층 글자 상자 → 먹 띠·먹 구간 → 균등 분할. PyMuPDF 내장 CJK 글꼴로 실제 한자를 렌더해 잰다."""
    TEXTS = ["入人曰日三川于干未末二八小州心火山水", "己已巳大太犬白自土士", "一二三四五六七八九十百千萬億", "生于水而長于土能入肝"]

    def _page(self, vertical: bool, size: float = 22):
        import pymupdf
        from PIL import Image
        doc = pymupdf.open(); page = doc.new_page(width=600, height=800)
        for i, t in enumerate(self.TEXTS):
            if vertical: page.insert_text((560 - i * 2 * size, 40), t, fontsize=size, fontname="china-ss", rotate=270)
            else: page.insert_text((30, 60 + i * 2 * size), t, fontsize=size, fontname="china-ss")
        pix = page.get_pixmap(dpi=300)
        return doc, page, Image.frombytes("RGB", (pix.width, pix.height), pix.samples).convert("L")

    def _check(self, vertical: bool, size: float = 22):
        from yeone_ocr.adjudicate import Site, locate_by_layer, locate_by_ink, glyph_box, glyph_window
        from yeone_ocr.pdfio import layer_chars
        from yeone_ocr.schema import frac_bbox
        doc, page, im = self._page(vertical, size); Wp, Hp = page.rect.width, page.rect.height
        lines = [l for l in layer_chars(doc, 1) if l["dir"] == ("v" if vertical else "h")]
        self.assertEqual(["".join(c[0] for c in l["chars"]) for l in lines], self.TEXTS)      # 세로는 오른쪽 열부터, 가로는 위 줄부터
        allb = [c[1] for l in lines for c in l["chars"]]
        fr = (min(b[0] for b in allb) / Wp - 0.05, min(b[1] for b in allb) / Hp - 0.03, max(b[2] for b in allb) / Wp + 0.06, max(b[3] for b in allb) / Hp + 0.07)
        bbox = frac_bbox(fr)                                                                 # 모델 좌표처럼 느슨한 영역 상자
        hit = {"layer": 0, "ink": 0, "uniform": 0}; n = 0
        for ci, ln in enumerate(lines):
            txt = "".join(c[0] for c in ln["chars"]); L = len(txt)
            for k, (ch, bb) in enumerate(ln["chars"]):
                site = Site("r1", ci, len(lines), k, L, ch, "x", txt[max(0, k - 4):k], txt[k + 1:k + 5], ""); n += 1
                cx, cy = (bb[0] + bb[2]) / 2 / Wp, (bb[1] + bb[3]) / 2 / Hp
                for how, box in (("layer", locate_by_layer(site, bbox, txt, lines, (Wp, Hp), not vertical)),
                                 ("ink", locate_by_ink(im, bbox, site, not vertical)), ("uniform", glyph_box(bbox, site, not vertical))):
                    if box and box[0] <= cx <= box[2] and box[1] <= cy <= box[3]:
                        hit[how] += 1
        self.assertGreaterEqual(hit["layer"], 0.95 * n, hit); self.assertGreaterEqual(hit["ink"], 0.9 * n, hit)
        self.assertGreater(hit["layer"], hit["uniform"], hit); self.assertGreater(hit["ink"], hit["uniform"], hit)
        # glyph_window 는 글자층이 있으면 layer, 없으면 ink, 빈 이미지면 uniform
        site = Site("r1", 1, len(lines), 3, len(self.TEXTS[1]), "大", "太", "己已巳", "太犬白自", "")
        self.assertEqual(glyph_window(im, bbox, site, not vertical, self.TEXTS[1], lines, (Wp, Hp))[1], "layer")
        self.assertEqual(glyph_window(im, bbox, site, not vertical, self.TEXTS[1], [], None)[1], "ink")
        from PIL import Image
        self.assertEqual(glyph_window(Image.new("L", (300, 300), 255), bbox, site, not vertical)[1], "uniform")

    def test_vertical(self): self._check(True)
    def test_horizontal(self): self._check(False)
    def test_small_font_vertical(self): self._check(True, size=10.5)      # 실제 본문 크기(10.5pt·300dpi)에서도 二·三 획 틈이 글자를 가르지 않는다
    def test_small_font_horizontal(self): self._check(False, size=10.5)

    def test_frame_offset_of_embedded_scan(self):
        """내장 스캔이 쪽보다 작은 사각형에 놓인 PDF: 글자층 pt 좌표는 쪽 기준, 쪽 이미지는 그 사각형 기준이라 틀(frame)로 바꿔야 맞는다."""
        import pymupdf
        from yeone_ocr.pdfio import page_frame, page_image
        doc = pymupdf.open(); page = doc.new_page(width=400, height=600)
        pix = pymupdf.Pixmap(pymupdf.csGRAY, pymupdf.IRect(0, 0, 370, 560), False); pix.clear_with(255)
        page.insert_image(pymupdf.Rect(20, 30, 390, 590), pixmap=pix)                       # 쪽의 92%를 덮는 스캔(왼쪽 위 20,30 부터, 비율 같음)
        fr = page_frame(doc, 1)
        self.assertEqual([round(v) for v in fr], [20, 30, 370, 560])
        self.assertEqual([round(v) for v in page_frame(pymupdf.open().new_page(width=400, height=600).parent, 1)], [0, 0, 400, 600])   # 내장 스캔이 없으면 쪽 전체
        img, src = page_image(doc, 1); self.assertTrue(src.startswith("내장 스캔"), src)
        # 글자 상자 (210,330)~(230,350)pt 는 틀 기준으로 가로 (210-20)/370 ≈ 0.51 — 쪽 기준(0.525)으로 나누면 1.4% 밀린다
        from yeone_ocr.adjudicate import Site, locate_by_layer
        lines = [{"dir": "h", "bbox": [30, 330, 390, 350], "chars": [[c, [30 + i * 20, 330, 50 + i * 20, 350]] for i, c in enumerate("一二三四五六七八九十")]}]
        site = Site("r1", 0, 1, 9, 10, "十", "千", "七八九", "", "")
        box = locate_by_layer(site, [500, 0, 600, 1000], "一二三四五六七八九十", lines, fr, True, span=1)
        self.assertIsNotNone(box)
        self.assertAlmostEqual((box[0] + box[2]) / 2, ((30 + 8.5 * 20 + 10) - 20) / 370, delta=0.02)   # 九·十 창의 가운데(틀 기준)

    def test_layer_mapping_tolerates_misread_target(self):
        """글자층이 목표 글자를 다르게 읽었어도(그래서 〔a/b〕 가 됐어도) 앞뒤 글자 대응으로 자리를 찾는다. 닮지 않은 줄은 고르지 않는다."""
        from yeone_ocr.adjudicate import Site, locate_by_layer
        lines = [{"dir": "h", "bbox": [10, 10, 190, 30], "chars": [[c, [10 + i * 18, 10, 28 + i * 18, 30]] for i, c in enumerate("行通乎小肠能人阴分治")]},
                 {"dir": "h", "bbox": [10, 40, 190, 60], "chars": [[c, [10 + i * 18, 40, 28 + i * 18, 60]] for i, c in enumerate("色赤入心性下行而通也")]}]
        site = Site("r1", 0, 2, 6, 10, "入", "人", "小肠能", "阴分治", "〔入/人〕")
        box = locate_by_layer(site, [0, 0, 400, 1000], "行通乎小肠能入阴分治", lines, (200.0, 200.0), True, span=1)
        self.assertIsNotNone(box); cx = (box[0] + box[2]) / 2 * 200
        self.assertTrue(10 + 6 * 18 <= cx <= 28 + 6 * 18, cx)                                     # 7번째 글자(人) 자리
        self.assertIsNone(locate_by_layer(site, [0, 0, 400, 1000], "全然다른글자들입니다", lines, (200.0, 200.0), True))


class BlindAdjudicate(unittest.TestCase):
    """0.6.5 — 3차 눈가림: 앞뒤 글자를 숨기고 순번·눈금으로 가리키며, 후보를 섞고, 저해상 자리는 묻지 않고, 원하면 후보 없이 재확인한다."""
    def _rec(self):
        return Adjudicate._rec(self)

    def _ctx(self):
        return _site_ctx(self._rec())

    def test_prompts_carry_no_context(self):
        """판독기가 받는 질문(GlyphAsk)으로 만든 프롬프트에 앞뒤 글자가 하나도 없다. 자형 전용 시스템 지시에는 문맥 단서가 없다."""
        from PIL import Image
        from yeone_ocr import prompts
        from yeone_ocr.adjudicate import adjudicate_page, find_sites
        from yeone_ocr.schema import GlyphChoice, PageReading
        sent = []
        class R:
            model = "m"
            def adjudicate_many(self, crops, h, p):
                sent.append(prompts.adjudicate_many_prompt([(sid, a) for sid, _, a in crops], h, p)); return {}
            def adjudicate(self, crop, ask, h, p):
                sent.append(prompts.adjudicate_prompt(ask, h, p)); return GlyphChoice()
        adjudicate_page(self._rec(), Image.new("L", (900, 1300), 255), R(), True, batch=8)
        self.assertEqual(len(sent), 4)                                                            # 묶음 1 + 빠진 자리 낱개 3
        allowed = set("".join(prompts.GLYPH_HINTS.values())) | {c for k in prompts.GLYPH_HINTS for c in k}
        ctx_chars = {ch for s in find_sites(PageReading.model_validate(self._rec()["merged"])) for ch in s.pre + s.post
                     if "\u4e00" <= ch <= "\u9fff" and ch not in allowed}
        self.assertTrue(ctx_chars)
        for t in sent:
            self.assertFalse(ctx_chars & set(t), (ctx_chars & set(t), t[:200]))
            self.assertIn("3번째", t)
        sysp = prompts.glyph_system_prompt()
        self.assertNotIn("장부 앞은", sysp); self.assertNotIn("生于", sysp); self.assertNotIn("출력 형식 규칙", sysp)
        self.assertIn("지침 24", sysp); self.assertIn("入", sysp)
        self.assertLess(len(sysp), 6000)                                                          # 캐시 문턱보다 짧다 → 캐시를 만들지 않는다
        from yeone_ocr.schema import GlyphAsk
        t = prompts.identify_prompt(GlyphAsk(pos=2, count=4), False, 3)
        self.assertIn("후보는 주지 않는다", t); self.assertNotIn("후보:", t)

    def test_candidate_order_is_seeded_and_mixed(self):
        from yeone_ocr.adjudicate import candidate_order
        orders = [candidate_order(["入", "人"], 12, "r1", 0, i) for i in range(40)]
        self.assertEqual(orders, [candidate_order(["入", "人"], 12, "r1", 0, i) for i in range(40)])     # 같은 자리는 늘 같은 차례
        self.assertEqual({o for o in orders}, {("入", "人"), ("人", "入")})                              # 자리에 따라 섞인다
        self.assertEqual(candidate_order(["入", "入", ""], 1), ("入",))

    def test_answer_field_order_is_window_evidence_choice(self):
        from yeone_ocr.schema import GlyphChoice, MultiGlyphChoice
        self.assertEqual(list(GlyphChoice.model_json_schema(by_alias=True)["properties"]), ["w", "e", "c"])
        try:
            from google.genai import client, _transformers
        except Exception:
            self.skipTest("google-genai 없음")
        sc = _transformers.t_schema(client.Client(api_key="x")._api_client, MultiGlyphChoice)
        self.assertEqual(sc.properties["items"].items.property_ordering, ["s", "w", "e", "c"])          # 모델에 그 차례로 쓰라고 전달된다

    def test_measured_position_target_and_tick(self):
        """실제 렌더한 한자 쪽: 글자층·먹 셈이 맞으면 순번과 대상 상자가 실측이고, 눈금은 조각 바깥 흰 띠에만 그려진다."""
        import numpy as np
        from yeone_ocr.adjudicate import Site, locate_glyph, glyph_crop
        from yeone_ocr.pdfio import layer_chars
        from yeone_ocr.schema import frac_bbox
        g = GlyphLocate(); g.TEXTS = GlyphLocate.TEXTS
        for vertical in (True, False):
            doc, page, im = g._page(vertical, 10.5); Wp, Hp = page.rect.width, page.rect.height
            lines = [l for l in layer_chars(doc, 1) if l["dir"] == ("v" if vertical else "h")]
            allb = [c[1] for l in lines for c in l["chars"]]
            bbox = frac_bbox((min(b[0] for b in allb) / Wp - 0.05, min(b[1] for b in allb) / Hp - 0.03, max(b[2] for b in allb) / Wp + 0.06, max(b[3] for b in allb) / Hp + 0.07))
            n = ok_layer = ink_measured = ok_ink = 0
            for ci, ln in enumerate(lines):
                txt = "".join(c[0] for c in ln["chars"]); L = len(txt)
                for k, (ch, bb) in enumerate(ln["chars"]):
                    site = Site("r1", ci, len(lines), k, L, ch, "x", txt[max(0, k - 4):k], txt[k + 1:k + 5], ""); n += 1
                    cx, cy = (bb[0] + bb[2]) / 2 / Wp, (bb[1] + bb[3]) / 2 / Hp
                    w = locate_glyph(im, bbox, site, not vertical, txt, lines, (Wp, Hp))
                    self.assertEqual(w.how, "layer")
                    if w.measured and w.pos == min(k, 2) + 1 and w.target and w.target[0] <= cx <= w.target[2] and w.target[1] <= cy <= w.target[3]:
                        ok_layer += 1
                    self.assertTrue(35 < w.em_px < 55, w.em_px)                                       # 10.5pt·300dpi ≈ 44px
                    wi = locate_glyph(im, bbox, site, not vertical, txt, [], None)
                    self.assertEqual(wi.how, "ink")
                    if wi.measured:
                        ink_measured += 1
                        if wi.pos == min(k, 2) + 1 and wi.target[0] <= cx <= wi.target[2] and wi.target[1] <= cy <= wi.target[3]:
                            ok_ink += 1
            self.assertEqual(ok_layer, n)
            self.assertGreaterEqual(ink_measured, 0.3 * n, (ink_measured, n))                         # 먹 셈은 획이 갈라지는 글자(二·三·川·八)가 든 줄에서 안 맞는다 → 어림
            self.assertEqual(ok_ink, ink_measured)                                                    # 실측이라고 한 자리는 모두 맞다(눈금을 그려도 된다)
            # 눈금: 조각 바깥 띠에만 빨간 픽셀, 대상 글자 길이만큼
            site = Site("r1", 0, len(lines), 5, len(GlyphLocate.TEXTS[0]), GlyphLocate.TEXTS[0][5], "x", "", "", "")
            w = locate_glyph(im, bbox, site, not vertical, GlyphLocate.TEXTS[0], lines, (Wp, Hp))
            plain_c = glyph_crop(im, w.box); marked = glyph_crop(im, w.box, target=w.target, horizontal=not vertical)
            a = np.asarray(marked.convert("RGB")).astype(int); red = (a[..., 0] > 180) & (a[..., 1] < 60) & (a[..., 2] < 60)
            self.assertTrue(red.any())
            if vertical:
                self.assertEqual(marked.height, plain_c.height); self.assertGreater(marked.width, plain_c.width)
                self.assertFalse(red[:, :plain_c.width].any())                                       # 글자 쪽에는 그리지 않는다
                rows = np.where(red.any(axis=1))[0]; span = (rows.min(), rows.max())
                self.assertTrue(0.25 * plain_c.height < (span[0] + span[1]) / 2 < 0.75 * plain_c.height, span)   # 가운데(3번째) 글자 옆
            else:
                self.assertEqual(marked.width, plain_c.width); self.assertGreater(marked.height, plain_c.height)
                self.assertFalse(red[marked.height - plain_c.height:, :].any())
                cols = np.where(red.any(axis=0))[0]
                self.assertTrue(0.25 * plain_c.width < (cols.min() + cols.max()) / 2 < 0.75 * plain_c.width)

    def test_low_resolution_sites_are_not_asked(self):
        from PIL import Image
        from yeone_ocr.adjudicate import adjudicate_page, locate_glyph, Site
        from yeone_ocr.schema import PageReading, region_text
        class R:
            model = "m"
            def adjudicate_many(self, *a, **k): raise AssertionError("저해상 자리는 묻지 않는다")
            def adjudicate(self, *a, **k): raise AssertionError("저해상 자리는 묻지 않는다")
        rec = adjudicate_page(self._rec(), Image.new("L", (900, 1300), 255), R(), True, min_px=10_000)
        st = rec["adjudicate_stats"]; self.assertEqual((st["lowres"], st["undecided"], st["decided"]), (3, 3, 0))
        self.assertTrue(all(a["evidence"].startswith("저해상") and a["em_px"] > 0 for a in rec["adjudications"]))
        body = region_text(PageReading.model_validate(rec["merged"]).regions[0]); self.assertIn("〔入/人〕", body); self.assertIn("〔曰/日〕", body)
        # 실제 저해상 스캔: 10.5pt 를 75dpi 로 그리면 글자가 약 11px — 기본 문턱(16px)에 걸린다
        import pymupdf
        from yeone_ocr.adjudicate import MIN_GLYPH_PX
        doc = pymupdf.open(); page = doc.new_page(width=600, height=800)
        page.insert_text((30, 60), "生于水而長于土能入肝", fontsize=10.5, fontname="china-ss")
        pix = page.get_pixmap(dpi=75); im = Image.frombytes("RGB", (pix.width, pix.height), pix.samples).convert("L")
        site = Site("r1", 0, 1, 8, 10, "入", "人", "", "", "")
        w = locate_glyph(im, [50, 30, 120, 300], site, True, "生于水而長于土能入肝")
        self.assertLess(w.em_px, MIN_GLYPH_PX, w)

    def test_blind_recheck_confirms_only_matching_answers(self):
        from PIL import Image
        from yeone_ocr.adjudicate import adjudicate_page
        from yeone_ocr.schema import GlyphChoice, PageReading, region_text
        from yeone_ocr.reader_gemini import ReaderError
        ctx = self._ctx(); want = {"1": "人", "2": "入", "3": "曰"}
        class R:
            model = "m"
            def __init__(self): self.open_asks = []; self.single = []
            def adjudicate_many(self, crops, h, p):
                return {sid: GlyphChoice(window=ctx[sid][0][-1:] + want[sid] + ctx[sid][1][:1], evidence="획", choice=want[sid]) for sid, _, _ in crops}
            def identify_many(self, crops, h, p):
                self.open_asks += [a for _, _, a in crops]
                return {"1": GlyphChoice(choice="人"), "2": GlyphChoice(choice="人")}                # 2번은 객관식(入)과 다르고, 3번은 빠뜨림
            def identify(self, crop, ask, h, p):
                self.single.append(ask); return GlyphChoice(choice="曰")
        r = R(); rec = adjudicate_page(self._rec(), Image.new("L", (900, 1300), 255), r, True, batch=8, blind=True)
        st = rec["adjudicate_stats"]
        self.assertEqual((st["decided"], st["undecided"], st["blind_checked"], st.get("blind_rejected")), (2, 1, 3, 1))
        self.assertTrue(all(a.candidates == () for a in r.open_asks + r.single)); self.assertEqual(len(r.single), 1)
        body = region_text(PageReading.model_validate(rec["merged"]).regions[0])
        self.assertIn("能人阴分", body); self.assertIn("〔入/人〕", body); self.assertIn("唐容川曰", body)
        e2 = rec["adjudications"][1]; self.assertEqual((e2["choice"], e2["blind"]), ("", "人")); self.assertIn("눈가림 답 「人」 ≠ 객관식 「入」", e2["evidence"])
        class Down(R):
            def identify_many(self, *a, **k): raise ReaderError("한도", fatal=False)
        rec = adjudicate_page(self._rec(), Image.new("L", (900, 1300), 255), Down(), True, batch=8, blind=True)
        st = rec["adjudicate_stats"]; self.assertEqual((st["decided"], st.get("blind_failed")), (0, 3))      # 재확인을 못 했으면 확정하지 않는다
        # 끄면(기본) 재확인 호출이 없다
        class NoOpen(R):
            def identify_many(self, *a, **k): raise AssertionError("blind 가 꺼져 있으면 부르지 않는다")
        self.assertEqual(adjudicate_page(self._rec(), Image.new("L", (900, 1300), 255), NoOpen(), True, batch=8)["adjudicate_stats"]["decided"], 3)

    def test_blind_answer_folds_script_variants(self):
        from yeone_ocr.adjudicate import fold_glyph
        from yeone_ocr.collate import zh
        if not zh.available():
            self.skipTest("opencc 없음")
        self.assertEqual(fold_glyph("黄"), fold_glyph("黃")); self.assertNotEqual(fold_glyph("入"), fold_glyph("人"))

    def test_reader_takes_glyph_system_and_skips_cache_when_short(self):
        try:
            from yeone_ocr.reader_gemini import GeminiReader
            from yeone_ocr.prompts import glyph_system_prompt
            import contextlib, io
            with contextlib.redirect_stdout(io.StringIO()):
                r = GeminiReader(api_key="AIza" + "x" * 35, model="gemini-2.5-flash", use_cache=True, system=glyph_system_prompt())
        except Exception as e:
            self.skipTest(f"판독기를 만들 수 없음: {e}")
        self.assertEqual(r._system, glyph_system_prompt()); self.assertIsNone(r.cache_name); self.assertIn("캐시 안 씀", r.cache_note)
        self.assertTrue(r._config(None).system_instruction)


class BlindReviewFixes(unittest.TestCase):
    """0.6.5 검토 반영 — 눈금·실측 순번은 확실할 때만, 눈가림 재확인은 엄격하게, 재확인은 답을 낸 판독기에."""
    def test_unequal_candidate_lengths_do_not_claim_a_span(self):
        from yeone_ocr.adjudicate import Site, GlyphWindow, make_ask
        from yeone_ocr import prompts
        site = Site("r1", 0, 1, 4, 9, "日一", "旦", "水而", "土能", "〔日一/旦〕")
        win = GlyphWindow((0, 0, 1, 1), "layer", pos=3, count=6, measured=True, target=(0.1, 0.1, 0.2, 0.2))
        a = make_ask(site, win, [site.a, site.b], 1, tick=True)
        self.assertEqual((a.k, a.measured, a.tick), (0, False, False))
        t = prompts.adjudicate_prompt(a, True, 1); self.assertIn("1~2자", t); self.assertNotIn("눈금", t)
        same = make_ask(Site("r1", 0, 1, 4, 9, "入", "人", "", "", ""), win, ["入", "人"], 1, tick=True)
        self.assertEqual((same.k, same.measured, same.tick), (1, True, True))
        # 실제 흐름: 글자층이 있는 쪽에서도 길이가 다른 표시에는 눈금을 그리지 않는다
        import pymupdf
        from PIL import Image
        from yeone_ocr.adjudicate import adjudicate_page
        from yeone_ocr.pdfio import layer_chars, page_frame
        from yeone_ocr.schema import PageReading, Region, Paragraph, Column, GlyphChoice, frac_bbox
        doc = pymupdf.open(); page = doc.new_page(width=600, height=800)
        page.insert_text((30, 60), "生于水而旦土能入肝", fontsize=22, fontname="china-ss")
        pix = page.get_pixmap(dpi=300); im = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
        lines = layer_chars(doc, 1); allb = [c[1] for l in lines for c in l["chars"]]
        bbox = frac_bbox((min(b[0] for b in allb) / 600 - 0.02, min(b[1] for b in allb) / 800 - 0.02, max(b[2] for b in allb) / 600 + 0.02, max(b[3] for b in allb) / 800 + 0.02))
        reg = Region(id="r1", kind="본문", bbox=bbox, paragraphs=[Paragraph(columns=[Column(col_from_right=1, text="生于水而〔日一/旦〕土能入肝")])])
        pr = PageReading(pdf_page=1, regions=[reg]); rec = {"pdf_page": 1, "pass1": pr.model_dump(), "merged": pr.model_dump()}
        seen = []
        class R:
            model = "m"
            def adjudicate(self, crop, ask, h, p): seen.append(ask); return GlyphChoice()
        adjudicate_page(rec, im, R(), True, batch=1, layer_lines=lines, frame=page_frame(doc, 1))
        self.assertEqual((seen[0].k, seen[0].tick, seen[0].measured), (0, False, False))

    def test_blind_agreement_is_strict_where_folding_cannot_tell(self):
        from yeone_ocr.adjudicate import blind_agrees
        from yeone_ocr.collate import zh
        self.assertTrue(blind_agrees("入", "入", ("入", "人"))); self.assertFalse(blind_agrees("人", "入", ("入", "人")))
        self.assertFalse(blind_agrees("", "入", ("入", "人"))); self.assertFalse(blind_agrees("  ", "7", ("6", "7")))   # 빈 답은 같지 않다
        self.assertTrue(blind_agrees("7", "7", ("6", "7"))); self.assertFalse(blind_agrees("6", "7", ("6", "7")))      # 숫자는 접으면 빈 글 → 그대로
        self.assertFalse(blind_agrees("。", "，", ("，", "。")))
        if zh.available():
            self.assertFalse(blind_agrees("黄", "黃", ("黃", "黄")))               # 이체자 쌍: 접으면 같아지므로 글자 그대로
            self.assertFalse(blind_agrees("苢", "苡", ("苡", "苢")))
            self.assertTrue(blind_agrees("黄", "黃", ("黃", "寅")))                # 후보가 접어도 다르면 간체·번체는 같게 본다

    def test_ink_exact_count_ignores_spaces(self):
        """띄어쓰기가 있는 줄: 먹 구간에는 빈칸이 없으므로 공백을 뺀 글자 수로 맞춰 본다(전에는 공백까지 세어 쪼개진 글자와 우연히 맞으면 엉뚱한 글자에 눈금)."""
        import pymupdf
        from PIL import Image
        from yeone_ocr.adjudicate import find_sites, locate_glyph, column_plain
        from yeone_ocr.pdfio import layer_chars
        from yeone_ocr.schema import PageReading, Region, Paragraph, Column, frac_bbox
        doc = pymupdf.open(); page = doc.new_page(width=600, height=800)
        page.insert_text((30, 60), "水而儿能 入肝", fontsize=22, fontname="china-ss")
        pix = page.get_pixmap(dpi=300); im = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
        chars = layer_chars(doc, 1)[0]["chars"]; allb = [bb for _, bb in chars]
        bbox = frac_bbox((min(b[0] for b in allb) / 600 - 0.02, min(b[1] for b in allb) / 800 - 0.02, max(b[2] for b in allb) / 600 + 0.02, max(b[3] for b in allb) / 800 + 0.02))
        reg = Region(id="r1", kind="본문", bbox=bbox, paragraphs=[Paragraph(columns=[Column(col_from_right=1, text="水而儿能 〔入/人〕肝")])])
        m = PageReading(pdf_page=1, regions=[reg]); site = find_sites(m)[0]
        w = locate_glyph(im, bbox, site, True, column_plain(m, site), [], None)
        self.assertEqual(w.how, "ink")
        if w.measured:                                                                       # 실측이라고 하면 대상 상자에 入 이 있어야 한다
            bb = next(bb for c, bb in chars if c == "入"); cx = (bb[0] + bb[2]) / 2 / 600
            self.assertTrue(w.target[0] <= cx <= w.target[2], (w, bb))

    def test_layer_interpolation_is_not_measured(self):
        from yeone_ocr.adjudicate import _map_index_ex, _map_index
        self.assertEqual(_map_index_ex("abXde", "abYde", 2), (2, True))                       # 한 글자를 달리 읽음 → 틈 길이 같음 → 실측
        j, exact = _map_index_ex("abcXde", "abcde", 3); self.assertFalse(exact)              # 글자층이 대상 글자를 빠뜨림 → 어림
        j, exact = _map_index_ex("abXde", "abYZde", 2); self.assertFalse(exact)              # 글자층이 쪼갬 → 어림
        self.assertEqual(_map_index("abXde", "abYde", 2), 2)

    def test_blind_recheck_goes_to_the_reader_that_answered(self):
        from PIL import Image
        from yeone_ocr.adjudicate import adjudicate_page
        from yeone_ocr.schema import GlyphChoice
        ctx = _site_ctx(Adjudicate._rec(self)); want = {"1": "人", "2": "入", "3": "曰"}
        class Flash:
            model = "flash"
            def adjudicate_many(self, crops, h, p): return {sid: GlyphChoice(window=ctx[sid][0][-1:] + "?" + ctx[sid][1][:1]) for sid, _, _ in crops}
            def identify_many(self, *a, **k): raise AssertionError("못 가린 판독기에 재확인을 묻지 않는다")
            def identify(self, *a, **k): raise AssertionError("못 가린 판독기에 재확인을 묻지 않는다")
        class Pro:
            model = "pro"
            def __init__(self): self.open = 0
            def adjudicate_many(self, crops, h, p):
                return {sid: GlyphChoice(window=ctx[sid][0][-1:] + want[sid] + ctx[sid][1][:1], choice=want[sid]) for sid, _, _ in crops}
            def identify_many(self, crops, h, p):
                self.open += 1; return {sid: GlyphChoice(choice=("入" if sid == "1" else want[sid])) for sid, _, _ in crops}   # 1번은 재확인에서 갈림
        pro = Pro(); rec = adjudicate_page(Adjudicate._rec(self), Image.new("L", (900, 1300), 255), Flash(), True, batch=8, fallback_reader=pro, blind=True)
        st = rec["adjudicate_stats"]
        self.assertEqual((pro.open, st["decided"], st["fallback_decided"], st["blind_rejected"]), (1, 2, 2, 1))        # 보완 확정은 재확인 뒤 남은 자리만


class UltraHigh(unittest.TestCase):
    """0.6.2 — 조각별 최고 해상도(ultra_high): 조각(Part)에 붙이고, 거부되면 high → 기본값 순으로 내린다."""
    def _reader(self, model="gemini-3-pro"):
        import threading
        from yeone_ocr.reader_gemini import GeminiReader
        from google.genai import types
        r = GeminiReader.__new__(GeminiReader); r.types = types; r.cache_name = None; r._system = "s"; r.thinking = None; r.thinking_budget = None; r.model = model
        r._lock = threading.Lock(); r.media_resolution = "ultra_high"
        return r

    def test_part_level_and_fallback_order(self):
        from PIL import Image
        from yeone_ocr.reader_gemini import GeminiReader
        from google.genai import types
        r = self._reader()
        p = r.image_part(Image.new("L", (8, 8), 255))
        self.assertEqual(p.media_resolution.level, types.PartMediaResolutionLevel.MEDIA_RESOLUTION_ULTRA_HIGH)
        self.assertIsNone(r._config(types.Schema).media_resolution)                      # 전역 설정에는 넣지 않는다(SDK 열거형에 없음)
        GeminiReader._strip_part_resolution([p, "글"]); self.assertIsNone(p.media_resolution)
        r.media_resolution = "high"; self.assertEqual(r._config(types.Schema).media_resolution, types.MediaResolution.MEDIA_RESOLUTION_HIGH)
        self.assertIsNone(r.image_part(Image.new("L", (8, 8), 255)).media_resolution)
        from yeone_ocr.cli import build_parser
        self.assertEqual(build_parser().parse_args(["read", "x.pdf", "--media-resolution", "ultra_high"]).media_resolution, "ultra_high")

    def test_ultra_high_only_for_gemini_3(self):
        """2.5 계열은 조각별 설정을 받지 않으므로 ultra_high 를 고르면 high(전역)로 내려 쓴다 — 서버가 조각 설정을 무시해 기본 해상도로 떨어지지 않게."""
        import contextlib, io
        from PIL import Image
        from google.genai import types
        r = self._reader("gemini-2.5-flash")
        with contextlib.redirect_stdout(io.StringIO()):
            p = r.image_part(Image.new("L", (8, 8), 255))
        self.assertIsNone(p.media_resolution); self.assertEqual(r.media_resolution, "high")
        self.assertEqual(r._config(types.Schema).media_resolution, types.MediaResolution.MEDIA_RESOLUTION_HIGH)

    def test_shared_cache_registry(self):
        """같은 모델·지침의 판독기는 캐시를 하나만 만들고, 만료된 이름은 버리고 새로 만든다."""
        import threading
        from yeone_ocr import reader_gemini as rg
        made = []
        class C:
            def create(self, model, config): made.append(model); o = type("O", (), {})(); o.name = f"cachedContents/{len(made)}"; return o
        class Client: caches = C()
        from google.genai import types
        def mk(model):
            r = rg.GeminiReader.__new__(rg.GeminiReader); r.types = types; r.client = Client(); r.model = model; r.vertexai = False; r._system = "지침"; r._lock = threading.Lock(); r.cache_name = None; r.cache_note = ""
            return r
        rg._CACHE_REGISTRY.clear()
        a, b, c = mk("m1"), mk("m1"), mk("m2")
        a._make_cache(); b._make_cache(); c._make_cache()
        self.assertEqual(made, ["m1", "m2"]); self.assertEqual(a.cache_name, b.cache_name); self.assertNotEqual(a.cache_name, c.cache_name)
        self.assertIn("공유", b.cache_note)
        b._make_cache(stale=b.cache_name)                                                     # 만료 → 새로
        self.assertEqual(made, ["m1", "m2", "m1"]); self.assertNotEqual(a.cache_name, b.cache_name)
        rg._CACHE_REGISTRY.clear()


class GlyphReadersAndEstimate(unittest.TestCase):
    """0.6.2 — 3차 판별 판독기 고르기(''/이름/auto)와 비용 어림의 3차 줄."""
    def test_make_glyph_readers_branches(self):
        from argparse import Namespace
        from yeone_ocr import reader_gemini as rg, cli
        made = []
        class FakeReader:
            def __init__(self, model=None, thinking_budget=None, use_cache=True, media_resolution=None, thinking=None, **k):
                self.model = model; self.thinking = thinking; self.thinking_budget = thinking_budget; self.media_resolution = media_resolution
                self.key = "k"; self.vertexai = False; made.append(model)
        old = (rg.GeminiReader, rg.list_models)
        rg.GeminiReader = FakeReader; rg.list_models = lambda key, vx: ["gemini-3-pro", "gemini-3-flash", "gemini-3-flash-lite"]
        try:
            base = dict(model="gemini-3-pro", thinking_budget=777, no_cache=False, adjudicate_thinking="low")
            r, f = cli.make_glyph_readers(Namespace(**base, adjudicate_model=""))
            self.assertEqual((r.model, f, r.thinking, r.thinking_budget, r.media_resolution), ("gemini-3-pro", None, "low", None, "high"))   # --thinking-budget 은 low 면 넘기지 않는다
            made.clear(); r, f = cli.make_glyph_readers(Namespace(**base, adjudicate_model="auto"))
            self.assertEqual((r.model, f.model, made), ("gemini-3-flash", "gemini-3-pro", ["gemini-3-pro", "gemini-3-flash"]))
            made.clear(); r, f = cli.make_glyph_readers(Namespace(**base, adjudicate_model="gemini-3-flash-lite"))
            self.assertEqual((r.model, f, made), ("gemini-3-flash-lite", None, ["gemini-3-flash-lite"]))                  # 쓰지 않을 판독기는 만들지 않는다
            r, f = cli.make_glyph_readers(Namespace(**dict(base, adjudicate_thinking="full"), adjudicate_model=""))
            self.assertEqual((r.thinking, r.thinking_budget), (None, 777))
            rg.list_models = lambda key, vx: ["gemini-3-pro"]
            r, f = cli.make_glyph_readers(Namespace(**base, adjudicate_model="auto"))
            self.assertEqual((r.model, f), ("gemini-3-pro", None))                                                       # 같은 세대 flash 가 없으면 보완 없음
        finally:
            rg.GeminiReader, rg.list_models = old

    def test_estimate_prices_third_pass_rows_by_model_name(self):
        from yeone_ocr.estimate import estimate
        u = {"calls": 3, "prompt_tokens": 3000, "output_tokens": 300, "thinking_tokens": 0, "cached_tokens": 2000,
             "by_model": {"gemini-2.5-pro": {"calls": 1, "prompt_tokens": 1000, "output_tokens": 100, "thinking_tokens": 0, "cached_tokens": 800},
                          "gemini-2.5-pro (3차)": {"calls": 1, "prompt_tokens": 1000, "output_tokens": 100, "thinking_tokens": 0, "cached_tokens": 600},
                          "gemini-2.5-flash (3차 보완)": {"calls": 1, "prompt_tokens": 1000, "output_tokens": 100, "thinking_tokens": 0, "cached_tokens": 600}}}
        e = estimate("x.pdf", [1, 2, 3], model="gemini-2.5-pro", second_model="", records=[{"pdf_page": 1, "pass1": {}, "usage": u}], fx_krw=1000.0)
        labels = {r["model"]: r["label"] for r in e["rows"]}
        self.assertEqual(labels["gemini-2.5-pro (3차)"], "3차 · 글자 판별"); self.assertEqual(labels["gemini-2.5-flash (3차 보완)"], "3차 · 글자 판별")
        self.assertEqual(labels["gemini-2.5-pro"], "1차 · 쪽 전체 판독"); self.assertIsNotNone(e["usd_low"]); self.assertEqual(e["calls"], 6)


# ---------------- 0.6.8: 호출 설정 오류·형식 오류 재시도 · 창 프로그램의 순수 논리 ----------------
class GeminiRetries(unittest.TestCase):
    """호출 설정을 못 만드는 것(옛 SDK)은 응답 오류가 아니라 바로 멈춤(fatal, 호출 없이). 응답 형식 오류는 3번까지, 서버 오류는 5번."""
    def _reader(self, gen):
        from yeone_ocr import reader_gemini as rg
        calls = []
        class Models:
            def generate_content(self, **kw):
                calls.append(kw); return gen(len(calls))
        class Client:
            models = Models()
        orig = rg.make_client; rg.make_client = lambda key, vx=False, timeout_ms=0: Client()
        try:
            r = rg.GeminiReader(model="gemini-2.5-pro", api_key="AIza" + "a" * 35, use_cache=False)
        finally:
            rg.make_client = orig
        return r, calls

    def test_config_error_is_fatal_without_calling(self):
        import pydantic
        from yeone_ocr import reader_gemini as rg
        class Out(pydantic.BaseModel):
            x: int
        r, calls = self._reader(lambda k: None)
        r._config = lambda schema: (_ for _ in ()).throw(TypeError("ThinkingConfig got an unexpected keyword 'thinking_level'"))
        with self.assertRaises(rg.ReaderError) as cm:
            r._call(None, "p", Out, extra_parts=["p"])
        self.assertTrue(cm.exception.fatal); self.assertIn("SDK", str(cm.exception)); self.assertIn("thinking_level", str(cm.exception))
        self.assertEqual(calls, [])                                                            # 호출·기다림 없이

    def test_format_errors_stop_after_three_but_server_errors_get_five(self):
        import pydantic
        from yeone_ocr import reader_gemini as rg
        class Out(pydantic.BaseModel):
            x: int
        class R:
            text = "이것은 JSON 이 아님"; usage_metadata = None
        slept = []; old = rg.time.sleep; rg.time.sleep = slept.append
        try:
            r, calls = self._reader(lambda k: R())
            with self.assertRaises(rg.ReaderError) as cm:
                r._call(None, "p", Out, extra_parts=["p"])
            self.assertFalse(cm.exception.fatal); self.assertEqual(len(calls), 3); self.assertIn("3회 시도", str(cm.exception)); self.assertIn("JSON", str(cm.exception))
            self.assertEqual(slept, [1, 2])
            class S(Exception):
                code = 503
            slept.clear(); r2, calls2 = self._reader(lambda k: (_ for _ in ()).throw(S("503 UNAVAILABLE")))
            with self.assertRaises(rg.ReaderError) as cm2:
                r2._call(None, "p", Out, extra_parts=["p"])
            self.assertEqual(len(calls2), 5); self.assertFalse(cm2.exception.fatal); self.assertEqual(slept, [1, 2, 4, 8])
            class Ok:
                text = '{"x": 3}'; usage_metadata = None
            r3, calls3 = self._reader(lambda k: R() if k < 3 else Ok())                   # 세 번째에 맞게 오면 그대로
            self.assertEqual(r3._call(None, "p", Out, extra_parts=["p"]).x, 3); self.assertEqual(len(calls3), 3)
        finally:
            rg.time.sleep = old
        try:
            Out.model_validate_json("nope")
        except Exception as e:
            self.assertTrue(rg.is_format_error(e))
        self.assertFalse(rg.is_format_error(RuntimeError("timed out")))


def _gui():
    """tkinter 가 없는 환경(검사 서버)에서도 창 모듈의 순수 논리를 검사할 수 있게 가짜 tkinter 를 끼운다. 있으면 진짜를 쓴다."""
    try:
        import tkinter  # noqa: F401
    except ImportError:
        import types
        def cls(n):
            return type(n, (), {"__init__": lambda self, *a, **k: None})
        def getter(n):
            return cls(n) if n[:1].isupper() else (lambda *a, **k: None)
        tk = types.ModuleType("tkinter"); tk.__getattr__ = getter; tk.TclError = type("TclError", (Exception,), {})
        for sub in ("ttk", "filedialog", "messagebox", "simpledialog", "font"):
            m = types.ModuleType("tkinter." + sub); m.__getattr__ = getter; setattr(tk, sub, m); sys.modules["tkinter." + sub] = m
        sys.modules["tkinter"] = tk
    from yeone_ocr import gui
    return gui


class GuiLogic(unittest.TestCase):
    """0.6.8 — 창 프로그램의 순수 논리: 끝난 명령의 말(실패한 쪽 있음), 닫을 때 기다리기, 설정 파일 권한, 새 기본값 입히기."""
    def test_finish_words(self):
        gui = _gui()
        self.assertEqual(gui.finish_words(0)[0], "ok"); self.assertEqual(gui.finish_words(None)[0], "ok")
        kind, status, head = gui.finish_words(3)
        self.assertEqual(kind, "partial"); self.assertIn("실패한 쪽 있음", status); self.assertIn("진행 기록 확인", status); self.assertEqual(head, "100% · 완료(실패 쪽 있음)")
        self.assertEqual(gui.finish_words(130)[:2], ("stopped", "중지됨")); self.assertEqual(gui.finish_words(1)[0], "error"); self.assertEqual(gui.finish_words(2)[0], "error")
        self.assertEqual(gui.finish_words(0)[2], "100% · 완료")
        # 책갈피 교정의 3 은 쪽 실패가 아니라 '주의:'(교정본 옮김·다시 계산) — '실패한 쪽' 이라 하지 않는다
        kind, status, head = gui.finish_words(3, ["주의: 교정본 a_교정.hwpx 의 구획 목록이 원본과 달라 a_교정_이전_1.hwpx 로 옮겼습니다"])
        self.assertEqual(kind, "partial"); self.assertIn("확인할 것", status); self.assertNotIn("실패", status + head)
        self.assertIn("실패한 쪽 있음", gui.finish_words(3, ["오류: 판독에 실패한 쪽 1개: 2"])[1])
        k, msg = gui.finish_alert(3, "HWPX 전체 교정", ["주의: 비교 표시를 다시 계산했습니다"])
        self.assertEqual(k, "warning"); self.assertIn("확인할 것", msg); self.assertNotIn("실패한 쪽", msg)

    def test_keep_waiting_for_worker_on_close(self):
        gui = _gui()
        self.assertTrue(gui.keep_waiting(True, 100.0, 105.0)); self.assertFalse(gui.keep_waiting(False, 100.0, 105.0))
        self.assertFalse(gui.keep_waiting(True, 100.0, 100.0 + gui.CLOSE_WAIT_SEC))                  # 한도가 차면 더 기다리지 않는다
        self.assertTrue(gui.keep_waiting(True, 0.0, 1.0, limit=2.0)); self.assertFalse(gui.keep_waiting(True, 0.0, 2.0, limit=2.0))

    def test_save_config_is_private(self):
        import stat, tempfile
        gui = _gui(); old = gui.CONFIG_PATH
        with tempfile.TemporaryDirectory() as td:
            gui.CONFIG_PATH = Path(td) / "cfg" / "config.json"
            try:
                gui.save_config({"api_key": "AIza" + "x" * 35, "workers": 8})
                self.assertEqual(json.loads(gui.CONFIG_PATH.read_text(encoding="utf-8"))["workers"], 8)
                if os.name != "nt":
                    self.assertEqual(stat.S_IMODE(gui.CONFIG_PATH.stat().st_mode), 0o600)
                gui.CONFIG_PATH.chmod(0o644); gui.save_config({"workers": 1})                      # 이전 판이 만든 느슨한 파일도 조인다
                if os.name != "nt":
                    self.assertEqual(stat.S_IMODE(gui.CONFIG_PATH.stat().st_mode), 0o600)
                self.assertEqual(json.loads(gui.CONFIG_PATH.read_text(encoding="utf-8")), {"workers": 1})
            finally:
                gui.CONFIG_PATH = old

    def test_new_defaults_match_cli_and_upgrade(self):
        gui = _gui()
        from yeone_ocr.cli import build_parser
        self.assertEqual((gui.DEFAULTS["workers"], gui.DEFAULTS["collate_policy"], gui.SPEED_DEFAULTS_VERSION), (8, "sure", 6))
        ap = build_parser()
        self.assertEqual(ap.parse_args(["run", "x.pdf"]).workers, gui.DEFAULTS["workers"]); self.assertEqual(ap.parse_args(["adjudicate", "x.pdf"]).workers, gui.DEFAULTS["workers"])
        self.assertEqual(ap.parse_args(["collate", "x.pdf", "--hwpx", "a.hwpx"]).policy, gui.DEFAULTS["collate_policy"])
        old = dict(gui.DEFAULTS); old.update({"workers": 6, "collate_policy": "all", "speed_defaults": 5})
        up = gui.upgrade_config(old); self.assertEqual((up["workers"], up["collate_policy"], up["speed_defaults"]), (8, "sure", 6))
        mine = dict(gui.DEFAULTS); mine.update({"workers": 4, "collate_policy": "memo", "speed_defaults": 5})
        up2 = gui.upgrade_config(mine); self.assertEqual((up2["workers"], up2["collate_policy"]), (4, "memo"))
        new = {"workers": 6, "collate_policy": "all", "speed_defaults": 6}
        up3 = gui.upgrade_config(new); self.assertEqual((up3["workers"], up3["collate_policy"]), (6, "all"))   # 새 판에서 직접 고른 6·all 은 그대로


class GuiFinishAlert(unittest.TestCase):
    """끝난 뒤 알림창: 성공(0)이어도 '주의:' 줄(책갈피 교정의 다시 계산·교정본 옮김 등)을 알리고, 3차 판별을 못 마친 것(4)은 실패한 쪽(3)과 다른 말로."""
    def test_finish_alert_words(self):
        gui = _gui()
        kind, status, head = gui.finish_words(4)
        self.assertEqual(kind, "partial"); self.assertIn("3차", status); self.assertNotIn("실패한 쪽", status); self.assertIn("3차", head)
        note = "주의: 판독 기록(merged)에 손으로 고친 글은 사라지고, 계산 전 기록은 readings/prev/p007.json 에 남김"
        wait = "주의: 요청 한도 초과 — 20초 뒤 다시 시도 (1/5)"
        self.assertIsNone(gui.finish_alert(0, "HWPX 전체 교정", []))
        self.assertIsNone(gui.finish_alert(0, "HWPX 전체 교정", [wait, wait]))                     # 스스로 풀린 대기만이면 알리지 않는다
        k, msg = gui.finish_alert(0, "HWPX 전체 교정", [note, wait, note])
        self.assertEqual(k, "warning"); self.assertIn("확인할 것", msg); self.assertEqual(msg.count(note), 1); self.assertNotIn("요청 한도", msg)
        self.assertIsNone(gui.finish_alert(130, "HWPX 전체 교정", [note]))                          # 중지는 알림창 없이
        k, msg = gui.finish_alert(3, "③ 판독", ["오류: 판독에 실패한 쪽 1개: 2"] + [wait] * 8)
        self.assertEqual(k, "warning"); self.assertIn("실패한 쪽이 있습니다", msg); self.assertIn("1개: 2", msg)   # 대기 줄이 정작 볼 줄을 밀어내지 않는다
        k, msg = gui.finish_alert(4, "HWPX 인식", ["오류: 3차 판별 실패 — 하루 요청 한도"])
        self.assertEqual(k, "warning"); self.assertIn("3차 글자 판별을 마치지 못해", msg); self.assertNotIn("실패한 쪽", msg)
        k, msg = gui.finish_alert(1, "HWPX 인식", [f"오류: {i}" for i in range(5)])
        self.assertEqual(k, "error"); self.assertIn("오류: 4", msg); self.assertNotIn("오류: 1", msg)

    def test_finish_shows_notes_after_success(self):
        """검토 재현(c5): 책갈피 교정이 '주의:' 를 남기고 0 으로 끝나면 알림창이 하나도 뜨지 않았다."""
        import queue, types
        from unittest import mock
        gui = _gui()
        w = gui.QueueWriter(queue.Queue())
        w.write("주의: 판독 기록(merged)에 손으로 고친 글은 사라지고 …\n"); w.write("  쪽 7: 다시 계산\n주의: 교정본 a_교정.hwpx 의 구획 목록이 원본과 달라 …\n")
        var = lambda: types.SimpleNamespace(set=lambda v: None)
        app = types.SimpleNamespace(_set_running=lambda on, command="": None, _closing=False, v_status=var(), v_prog=var(), _append=lambda s: None,
                                    tracker=types.SimpleNamespace(t0=0.0, frac=1.0, finish=lambda: None), progress=types.SimpleNamespace(configure=lambda **k: None),
                                    refresh_table=lambda: None, open_after=None, _last_writer=w)
        with mock.patch.object(gui, "messagebox") as mb:
            gui.App._finish(app, 0, "HWPX 전체 교정")
        self.assertEqual(mb.showwarning.call_count, 1); self.assertFalse(mb.showerror.called)
        self.assertIn("교정본 a_교정.hwpx", mb.showwarning.call_args[0][1]); self.assertIsNone(app._last_writer)
        app._last_writer = gui.QueueWriter(queue.Queue())
        with mock.patch.object(gui, "messagebox") as mb:
            gui.App._finish(app, 0, "HWPX 전체 교정")
        self.assertFalse(mb.showwarning.called)                                                       # 알릴 줄이 없으면 그대로
