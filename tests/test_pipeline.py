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
        self.assertTrue(any("1차에만" in u.context and "甲丙戊庚壬子" in u.context for u in m2.uncertain))
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
            self.assertEqual(types, ["meta", "heading", "image", "para", "table", "uncertain"])
            out = os.path.join(td, "o.hwpx")
            st = build_hwpx(blocks, out)
            self.assertEqual(st["images"], 1); self.assertEqual(st["images_in_file"], 1); self.assertEqual(st["tables"], 1)
            self.assertEqual(check_hwpx_file(out), [])
            z = zipfile.ZipFile(out); self.assertEqual(z.namelist()[0], "mimetype")
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
