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
            z = zipfile.ZipFile(out); self.assertEqual(z.namelist()[0], "mimetype")
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
    def test_upgrade_old_config_adopts_speed_defaults(self):
        from yeone_ocr.gui import upgrade_config, DEFAULTS, SPEED_DEFAULTS_VERSION
        old = dict(DEFAULTS); old.update({"workers": 3, "second_model": "", "checkpoint": 20}); old.pop("speed_defaults", None)
        up = upgrade_config(old)
        self.assertEqual(up["workers"], 6); self.assertEqual(up["second_model"], "auto")
        self.assertEqual(up["speed_defaults"], SPEED_DEFAULTS_VERSION)
        # 사용자가 직접 바꾼 값은 유지
        mine = dict(DEFAULTS); mine.update({"workers": 5, "second_model": "gemini-2.5-flash"}); mine.pop("speed_defaults", None)
        up2 = upgrade_config(mine)
        self.assertEqual(up2["workers"], 5); self.assertEqual(up2["second_model"], "gemini-2.5-flash")
        # 이미 새 판이면 손대지 않음
        new = {"speed_defaults": SPEED_DEFAULTS_VERSION, "workers": 2, "second_model": ""}
        up3 = upgrade_config(new)
        self.assertEqual(up3["workers"], 2); self.assertEqual(up3["second_model"], "")


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
            def __init__(self): self.calls = []
            def adjudicate(self, crop, candidates, pre, post, horizontal, pdf_page):
                self.calls.append((candidates, pre, post, crop.size))
                if candidates == ["入", "人"] and pre.endswith("能"):
                    return GlyphChoice(choice="人", evidence="왼쪽 삐침이 위에서 시작")       # 인쇄가 人인 자리
                if candidates == ["入", "人"]:
                    return GlyphChoice(choice="入", evidence="오른쪽 삐침이 위로 솟음")
                return GlyphChoice(choice="", evidence="획이 뭉개짐")                           # 曰/日 은 못 가림
        fake = Fake()
        rec = adjudicate_page(rec, img, fake, horizontal=True)
        self.assertEqual(len(fake.calls), 3); self.assertTrue(all(sz[0] > 4 and sz[1] > 4 for *_, sz in fake.calls))
        m = PageReading.model_validate(rec["merged"]); body = region_text(m.regions[0])
        self.assertIn("能人阴分", body); self.assertIn("色赤入心", body); self.assertIn("〔曰/日〕", body)
        self.assertEqual(rec["adjudicate_stats"], {"sites": 3, "decided": 2, "undecided": 1, "a": 1, "b": 1, "failed": 0})
        self.assertTrue(any("3차 판별: 「人」 확정" in u.context for u in m.uncertain))
        # 비교 다시 계산(원본 pass1 에서 다시 병합) 뒤에도 판별 결과를 다시 적용할 수 있다
        fresh = PageReading.model_validate(rec["pass1"])
        self.assertEqual(apply_adjudications(fresh, rec["adjudications"]), 2)
        self.assertIn("能人阴分", region_text(fresh.regions[0])); self.assertIn("〔曰/日〕", region_text(fresh.regions[0]))

    def test_prompt_has_glyph_hint(self):
        from yeone_ocr.prompts import adjudicate_prompt
        t = adjudicate_prompt(["入", "人"], "肠，能", "阴分", True, 430)
        self.assertIn("入", t); self.assertIn("자형 단서", t); self.assertIn("문맥", t)
