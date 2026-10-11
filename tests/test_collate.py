"""책갈피 교정(collate) 시험: 색인·짝짓기·대조 분류·서식 보존 편집."""
import contextlib, io, json, os, re, sys, tempfile, unittest, zipfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from yeone_ocr.collate import zh
from yeone_ocr.collate.sections import HwpxBook, Anchor, find_anchor, normalize_name, heading_styles
from yeone_ocr.collate.pdf_outline import PdfAnchor, match_anchors, page_range
from yeone_ocr.collate.collate import hwpx_cells, ocr_cells, collate, summary, strip_marks, book_titles, witness_filter
from yeone_ocr.collate.hwpx_edit import Editor, paragraph_text
from yeone_ocr.collate.run import trim_to_section


def make_book(path: str, plain: bool = False):
    """개요 스타일 제목 + 본문으로 된 작은 HWPX(번체). plain=True 면 제목도 바탕글(책갈피·제목 스타일이 전혀 없는 문서)."""
    from hwpx.document import HwpxDocument
    doc = HwpxDocument.new()
    body = doc.ensure_run_style(font="함초롬바탕", size=11)
    def heading(text, level):
        if plain:
            para(text)
        else:
            doc.add_heading(text, level=level)
    def para(text):
        p = doc.add_paragraph("", para_pr_id_ref=0, style_id_ref=0, char_pr_id_ref=body, include_run=False, inherit_style=False)
        p.add_run(text, char_pr_id_ref=body)
    heading("各論", 1)
    heading("大薊", 2)
    para("1. 『本草經疏』：大薊根, 陶云有毒, 誤也. 女子赤白沃, 血熱所致也.")
    para("2. 『本草新編』：大薊, 破血止血甚奇, 但用於初起之血症大獲奇功.")
    heading("小薊", 2)
    para("小薊, 性涼. 治吐血衄血.")
    heading("人參", 2)
    para("人蔘, 大補元氣.")
    doc.save_to_path(path)
    return path


class Sections(unittest.TestCase):
    def test_index_and_range(self):
        with tempfile.TemporaryDirectory() as td:
            bk = HwpxBook(make_book(os.path.join(td, "b.hwpx")))
            an = bk.anchors()
            self.assertEqual([a.name for a in an], ["各論", "大薊", "小薊", "人參"])
            self.assertEqual([a.level for a in an], [1, 2, 2, 2])
            sec = bk.section(an, 1)
            self.assertEqual(bk.section_texts(sec)[0], "大薊"); self.assertEqual(sec.n_paras, 3); self.assertEqual(sec.next_anchor.name, "小薊")
            top = bk.section(an, 0); self.assertEqual(top.end, len(bk.paragraphs))          # 급 1 은 끝까지
            self.assertEqual([a.name for a in find_anchor(an, "人参")], ["人參"])            # 간체로 찾아도(정규화 뒤 포함 검색)
            self.assertEqual(find_anchor(an, "薊"), [an[1], an[2]])
        self.assertEqual(normalize_name(" 三棱 (京三棱·黑三棱) "), "三棱京三棱黑三棱")
        self.assertEqual(heading_styles([("0", "바탕글"), ("6", "제목-장"), ("8", "제목-본초명"), ("9", "제목-본초명(소)")]), {"6": 1, "8": 2, "9": 3})


class PlainBook(unittest.TestCase):
    def test_anchors_from_pdf_titles(self):
        """책갈피도 제목 스타일도 없는 HWPX: PDF 책갈피 이름을 본문에서 찾아 기준으로 삼는다."""
        from yeone_ocr.collate.run import index_both
        class FakePdf:
            def __len__(self): return 200
            def get_toc(self, simple=False): return [[1, "各论", 1, {}], [2, "大蓟", 10, {}], [2, "小蓟", 12, {}], [2, "人参", 15, {}], [2, "없는항목", 20, {}]]
        with tempfile.TemporaryDirectory() as td:
            bk = HwpxBook(make_book(os.path.join(td, "p.hwpx"), plain=True))
            self.assertEqual(bk.anchors(), [])                                            # 제목 스타일이 없으니 HWPX 기준은 0개
            anchors, pdf_anchors, matches = index_both(FakePdf(), bk, "auto")
            self.assertEqual([a.name for a in anchors], ["各論", "大薊", "小薊", "人參"])   # 간체 책갈피 → 번체 본문 문단
            self.assertEqual([a.kind for a in anchors], ["pdf"] * 4); self.assertEqual([m.how for m in matches], ["pdf"] * 4)
            self.assertEqual(matches[1].pdf.page, 10)
            sec = bk.section(anchors, 1); self.assertEqual(bk.section_texts(sec)[0], "大薊"); self.assertEqual(sec.next_anchor.name, "小薊")
            hw_only = index_both(FakePdf(), bk, "hwpx")[0]; self.assertEqual(hw_only, [])
            from yeone_ocr.collate import reading
            if reading.available():
                class KoPdf(FakePdf):
                    def get_toc(self, simple=False): return [[1, "각론", 1, {}], [2, "대계", 10, {}], [2, "소계", 12, {}], [2, "인삼", 15, {}]]
                ko = index_both(KoPdf(), bk, "pdf")[0]
                self.assertEqual([a.name for a in ko], ["各論", "大薊", "小薊", "人參"])                       # 한글 책갈피 → 한자 본문


class Reading(unittest.TestCase):
    def test_hangul_reading_search(self):
        from yeone_ocr.collate import reading
        if not reading.available():
            self.skipTest("hanja 없음")
        self.assertEqual(reading.reading("大薊"), "대계"); self.assertEqual(reading.reading("人參"), "인삼"); self.assertEqual(reading.reading("羅經"), "나경")
        self.assertTrue(reading.matches("大薊", "대계")); self.assertTrue(reading.matches("人參葉", "인삼")); self.assertTrue(reading.matches("人蔘", "인삼"))
        self.assertTrue(reading.matches("三棱(京三棱·黑三棱)", "삼릉")); self.assertTrue(reading.matches("三棱", "삼능"))      # 두음법칙 어느 쪽이든
        self.assertTrue(reading.matches("黨參(上黨人參)", "당삼")); self.assertTrue(reading.matches("黨參", "당참"))          # 여러 음 글자
        self.assertTrue(reading.matches("大薊 與 小薊", "소계")); self.assertFalse(reading.matches("大薊", "소계")); self.assertFalse(reading.matches("大薊", ""))
        self.assertTrue(reading.matches("羅經必備五行", "나경")); self.assertTrue(reading.matches("羅經必備五行", "라경"))
        self.assertTrue(reading.matches("大薊", "大薊"))                                                                   # 한자 그대로도

    def test_styles_as_bookmarks(self):
        with tempfile.TemporaryDirectory() as td:
            bk = HwpxBook(make_book(os.path.join(td, "b.hwpx")))
            names = bk.heading_style_names(); self.assertTrue(any("개요" in n for n in names))
            only2 = bk.anchors(styles=["개요 2"])
            self.assertEqual([a.name for a in only2], ["大薊", "小薊", "人參"]); self.assertEqual({a.level for a in only2}, {1})
            both = bk.anchors(styles=["개요 2", "개요 1"])                                  # 앞에 적은 것이 급 1
            self.assertEqual([(a.name, a.level) for a in both][:2], [("各論", 2), ("大薊", 1)])
            self.assertTrue(any(n == "개요 1" for n, _ in bk.style_counts()))
            an = bk.anchors(); self.assertEqual([a.name for a in find_anchor(an, "대계")], ["大薊"]); self.assertEqual([a.name for a in find_anchor(an, "인삼")], ["人參"])


class Outline(unittest.TestCase):
    def test_tolerant_matching(self):
        from yeone_ocr.collate.pdf_outline import match_key
        self.assertEqual(match_key("冬瓜 ……… 123"), match_key("冬瓜")); self.assertEqual(match_key("121. 冬瓜 Dongguā"), match_key("冬瓜"))
        an = [Anchor("heading", "冬瓜", 10, 2), Anchor("heading", "大薊", 20, 2), Anchor("heading", "人參", 30, 2)]
        pdf = [PdfAnchor("第三节 冬瓜(附：冬瓜子) 123", 1, 123), PdfAnchor("大蓟 ……… 130", 1, 130), PdfAnchor("人参", 1, 140)]
        ms = match_anchors(an, pdf)
        self.assertEqual([m.how for m in ms], ["contains", "exact", "exact"]); self.assertEqual(ms[0].pdf.page, 123)
        from yeone_ocr.collate.pdf_outline import HOW_LABELS
        self.assertTrue(all(m.how in HOW_LABELS for m in ms)); self.assertIn("pdf", HOW_LABELS); self.assertIn("none", HOW_LABELS)   # 화면 표시 이름이 모두 있어야 색인이 실패하지 않는다

    def test_hangul_pdf_bookmarks_match_hanja_headings(self):
        """PDF 책갈피가 한글 독음(동과, 백과(은행) …)이어도 HWPX 한자 제목과 짝이 맞는다."""
        from yeone_ocr.collate import reading
        if not reading.available():
            self.skipTest("hanja 없음")
        an = [Anchor("heading", "白果(銀杏)", 10, 2), Anchor("heading", "冬瓜", 20, 2), Anchor("heading", "大豆(黑大豆·黑豆)", 30, 2),
              Anchor("heading", "龍眼(桂圓)", 40, 2), Anchor("heading", "三棱", 50, 2), Anchor("heading", "大薊", 60, 2)]
        pdf = [PdfAnchor("4. 식물류", 1, 400), PdfAnchor("백과(은행)", 2, 409), PdfAnchor("동과", 2, 410), PdfAnchor("대두(흑대두, 흑두)", 2, 412),
               PdfAnchor("용안(계원)", 2, 415), PdfAnchor("삼능", 2, 420), PdfAnchor("대계", 2, 430)]
        ms = match_anchors(an, pdf)
        self.assertEqual([m.pdf.page if m.pdf else None for m in ms], [409, 410, 412, 415, 420, 430])
        self.assertEqual([m.how for m in ms], ["exact"] * 6)
        # 한자끼리는 음이 같아도 다른 글자면 안 맞는다
        self.assertIsNone(match_anchors([Anchor("heading", "大薊", 1, 2)], [PdfAnchor("大計", 1, 5)])[0].pdf)

    def test_find_title_in_text_layer(self):
        import pymupdf
        doc = pymupdf.open()
        for text in ["目录 大蓟 12 小蓟 13", "大蓟 本草经疏 大蓟根 陶云有毒", "续 大蓟 本草新编", "小蓟 性凉 治吐血", "人参 大补元气"]:
            pg = doc.new_page(); pg.insert_text((72, 100), text, fontname="china-s", fontsize=14)
        from yeone_ocr.collate.pdf_outline import find_title_pages
        self.assertEqual(find_title_pages(doc, "大薊", "小薊", start_page=2), (2, 4))     # 목차 쪽을 건너뛰고 본문에서, 다음 제목 쪽까지
        self.assertEqual(find_title_pages(doc, "人參", None, start_page=2), (5, 5))
        self.assertIsNone(find_title_pages(doc, "甘草", None))

    def test_match_and_pages(self):
        an = [Anchor("heading", "大薊", 10, 2), Anchor("heading", "小薊", 20, 2), Anchor("heading", "大薊 與 小薊", 30, 2), Anchor("heading", "없는것", 40, 2)]
        pdf = [PdfAnchor("大蓟", 1, 120), PdfAnchor("小蓟", 1, 122), PdfAnchor("大蓟与小蓟", 1, 123), PdfAnchor("人参", 1, 130)]
        ms = match_anchors(an, pdf)
        self.assertEqual([m.how for m in ms], ["exact", "exact", "exact", "none"])      # 간체 책갈피와 번체 제목이 바로 맞는다
        self.assertEqual(page_range(pdf, 0, 300), (120, 122)); self.assertEqual(page_range(pdf, 3, 300), (130, 300))
        fuzzy = match_anchors([Anchor("heading", "三棱(京三棱·黑三棱)", 1, 2)], [PdfAnchor("三棱（京三棱、黑三棱）", 1, 50)])
        self.assertEqual(fuzzy[0].how, "exact")


class Zh(unittest.TestCase):
    def test_conversion(self):
        if not zh.available():
            self.skipTest("opencc 없음")
        self.assertEqual(zh.to_traditional("白术 干姜 其后"), "白朮 乾薑 其後")
        self.assertEqual(zh.compare_key("人蔘"), zh.compare_key("人参")); self.assertEqual(zh.compare_key("表裡"), zh.compare_key("表裏"))
        self.assertTrue(zh.is_ambiguous("发")); self.assertFalse(zh.is_ambiguous("无")); self.assertEqual(sorted(zh.candidates("术")), ["朮", "術"])
        self.assertEqual(strip_marks("大〔疑: 薊〕根〔판독불명〕 〔有/無〕毒")[0], "大薊根 有毒")


class Collate(unittest.TestCase):
    HW = ["大薊", "1. 『本草經疏』：大薊根, 陶云有毒, 誤也. 女子赤白沃, 血熱所致也.", "2. 『本草新編』：大薊, 破血止血甚奇, 但用於初起之血症大獲奇功."]

    def _ocr(self):
        if not zh.available():
            self.skipTest("opencc 없음")
        o = [zh.to_simplified(t) for t in self.HW]
        o[1] = o[1].replace("陶云有毒", "陶云无毒").replace("血热所致也", "血热所致也〔疑: 胎〕因热则不安")
        o[2] = o[2].replace("初起之血症", "初起之发血症") + "此乃前人未发之秘，学者宜细玩之，不可忽也，切记切记切记。"
        return o

    def test_classification(self):
        items = collate(hwpx_cells(self.HW, 100), ocr_cells([(7, "\n".join(self._ocr()))]), policy="all")
        kinds = {(it.kind, it.action) for it in items}
        self.assertEqual(kinds, {("A", "replace"), ("B1", "mark"), ("B2", "insert"), ("C", "insert")})
        a = next(it for it in items if it.kind == "A"); self.assertEqual((a.hwpx, a.ocr, a.new, a.para, a.auto), ("有", "无", "無", 101, True))
        b2 = next(it for it in items if it.kind == "B2"); self.assertEqual(b2.new, "發"); self.assertEqual(b2.candidates, ["發", "髮"]); self.assertTrue(b2.auto)
        self.assertFalse(next(it for it in items if it.kind == "B1").auto)
        self.assertEqual(summary(items)["auto"], 3)
        sure = collate(hwpx_cells(self.HW, 100), ocr_cells([(7, "\n".join(self._ocr()))]), policy="sure")
        self.assertEqual([it.kind for it in sure if it.auto], ["A"])
        memo = collate(hwpx_cells(self.HW, 100), ocr_cells([(7, "\n".join(self._ocr()))]), policy="memo")
        self.assertFalse(any(it.auto for it in memo))
        # 표기만 다른 것(이체자·부호)은 차이로 보지 않는다
        same = collate(hwpx_cells(["人蔘, 大補元氣."], 0), ocr_cells([(1, "人参，大补元气。")]))
        self.assertEqual(same, [])

    def test_korean_variants_and_confusables(self):
        if not zh.available():
            self.skipTest("opencc 없음")
        # 幷(한국식)↔并: 차이가 아니다
        self.assertEqual(collate(hwpx_cells(["則在上者幷自吐去"], 0), ocr_cells([(1, "则在上者并自吐去")])), [])
        # 幹(→干) vs 판독 于: 干/于 묶음은 문맥 규칙(能 뒤 → 干)으로 HWPX 편 → 차이에서 뺀다
        self.assertEqual(collate(hwpx_cells(["苦瓠之肉能幹"], 0), ocr_cells([(1, "苦瓠之肉能于")]), policy="all"), [])
        # 凡 vs 판독 几(幾/几): 자형 혼동 + 번체 후보 여럿 → 자동 반영하지 않고 표시만
        items = collate(hwpx_cells(["凡例十則"], 0), ocr_cells([(1, "几例十则")]), policy="all")
        self.assertEqual([(it.kind, it.action, it.auto) for it in items], [("B1", "mark", False)]); self.assertIn("헷갈리기", items[0].note)
        self.assertIn("「凡」", items[0].hw_ctx); self.assertIn("「几」", items[0].oc_ctx)
        # 問 vs 间(1:1)은 그대로 A 자동
        items = collate(hwpx_cells(["津液充滿無問"], 0), ocr_cells([(1, "津液充满无间")]), policy="all")
        self.assertEqual([(it.kind, it.new, it.auto) for it in items], [("A", "間", True)]); self.assertEqual(items[0].hw_ctx, "津液充滿無「問」")

    def test_trim(self):
        paras = [(5, "앞 구획 끝 문장"), (5, "大蓟"), (5, "1. 本草经疏"), (6, "2. 本草新编"), (6, "小蓟"), (6, "小蓟 本문")]
        cut, note = trim_to_section(paras, "大薊", "小薊")
        self.assertEqual([t for _, t in cut], ["大蓟", "1. 本草经疏", "2. 本草新编"]); self.assertEqual(note, "")


class Edit(unittest.TestCase):
    def test_apply_keeps_structure(self):
        if not zh.available():
            self.skipTest("opencc 없음")
        with tempfile.TemporaryDirectory() as td:
            bk = HwpxBook(make_book(os.path.join(td, "b.hwpx"))); an = bk.anchors(); sec = bk.section(an, 1)
            hw = bk.section_texts(sec)
            ocr = Collate._ocr(Collate("test_classification"))
            items = collate(hwpx_cells(hw, sec.start), ocr_cells([(7, "\n".join(ocr))]), policy="all")
            self.assertTrue(all(paragraph_text(p.element) == p.text for p in bk.paragraphs))
            ed = Editor(bk, memo=True); log = ed.apply(items)
            self.assertTrue(all(l.ok for l in log), [l.note for l in log if not l.ok])
            out = ed.save_copy(os.path.join(td, "out.hwpx"))
            bk2 = HwpxBook(out); t = bk2.texts()
            self.assertIn("陶云無毒", t[sec.start + 1]); self.assertIn("初起之發血症", t[sec.start + 2]); self.assertIn("切記切記切記", t[sec.start + 2])
            self.assertEqual(t[sec.start + 1].count("胎因熱則不安"), 0)                 # B1 은 글자를 넣지 않음
            self.assertEqual(len(bk2.paragraphs), len(bk.paragraphs))                   # 문단 수 그대로
            z = zipfile.ZipFile(out); x = z.read("Contents/section0.xml").decode(); h = z.read("Contents/header.xml").decode()
            self.assertEqual(x.count('type="MEMO"'), 4); self.assertEqual(x.count("<hp:fieldEnd"), 4)       # 'all'(파이썬 기본): 항목마다
            self.assertIn('textColor="#1F4FBF"', h); self.assertIn('textColor="#C00000"', h); self.assertIn('shadeColor="#E1EBFF"', h)   # 파랑·빨강
            self.assertIn("[자동 교정] 원래 「有」 → 「無」", x); self.assertIn("여러 번체로 갈라짐: 發/髮", x)
            # 다른 구획은 그대로
            self.assertEqual(t[sec.end], "小薊"); self.assertEqual(t[-1], "人蔘, 大補元氣.")
            # 고지 사항만(기본): 색으로 전할 수 있는 자동 교정·표시에는 메모가 없고, 범례와 고지만 남는다
            bk4 = HwpxBook(make_book(os.path.join(td, "d.hwpx"))); ed4 = Editor(bk4, memo="notice")
            items4 = collate(hwpx_cells(bk4.section_texts(bk4.section(bk4.anchors(), 1)), sec.start), ocr_cells([(7, "\n".join(ocr))]), policy="all")
            from yeone_ocr.grades import needs_memo, collate_grade
            ed4.apply(items4)
            n_par = len(bk4.paragraphs); texts_before = bk4.texts()
            self.assertTrue(ed4.add_legend_memo()); self.assertFalse(ed4.add_legend_memo())     # 범례는 한 번만
            self.assertEqual(len(bk4.paragraphs), n_par); self.assertEqual(bk4.texts(), texts_before)   # 문단·글 그대로
            out4 = ed4.save_copy(os.path.join(td, "out4.hwpx"))
            x4 = zipfile.ZipFile(out4).read("Contents/section0.xml").decode()
            self.assertEqual(x4.count('type="MEMO"'), 1 + sum(1 for it in items4 if needs_memo(it, "notice")))
            self.assertLess(x4.count('type="MEMO"'), x.count('type="MEMO"') + 1)
            self.assertEqual(x4.count("[연어OCR 표시 범례"), 1)
            auto_a = [it for it in items4 if it.kind == "A" and it.auto and it.action == "replace"]
            self.assertTrue(auto_a and all(not needs_memo(it, "notice") for it in auto_a))       # 자동 교정 치환은 색만
            self.assertTrue(all(collate_grade(it) == "blue" for it in auto_a))                    # 판독 일치만 = 파랑(정밀도 미측정)
            # 메모 없이(〔〕 표기)도 된다
            bk3 = HwpxBook(make_book(os.path.join(td, "c.hwpx"))); ed3 = Editor(bk3, memo=False)
            ed3.apply(collate(hwpx_cells(bk3.section_texts(bk3.section(bk3.anchors(), 1)), sec.start), ocr_cells([(7, "\n".join(ocr))])))
            t3 = bk3.texts()[sec.start + 1]
            self.assertIn("陶云無毒", t3); self.assertNotIn("〔원: 有〕", t3)          # 자동 교정은 색으로만(원래 글자는 보고에)
            self.assertIn("〔확인: 판독", t3)                                            # 고지(폭 없는 표시)만 글 속 〔…〕


if __name__ == "__main__":
    unittest.main()


class MergeFold(unittest.TestCase):
    def _merge(self, t1, t2, fold_zh):
        from yeone_ocr import merge
        from yeone_ocr.schema import PageReading, Region, Paragraph, Column, RegionReread
        p1 = PageReading(pdf_page=1, regions=[Region(id="b", kind="본문", paragraphs=[Paragraph(columns=[Column(col_from_right=1, text=t1)])])])
        rr = {"b": RegionReread(columns=[Column(col_from_right=1, text=t2)])}
        old = merge.FOLD_ZH; merge.FOLD_ZH = fold_zh
        try:
            m, st = merge.merge_passes(p1, rr)
        finally:
            merge.FOLD_ZH = old
        return m.regions[0].paragraphs[0].columns[0].text, st["marks"]

    def test_punctuation_variants_are_not_differences(self):
        text, marks = self._merge("冬瓜，性凉。治水肿、小便不利；", "冬瓜,性凉.治水肿,小便不利;", fold_zh=False)
        self.assertEqual(marks, 0); self.assertEqual(text, "冬瓜，性凉。治水肿、小便不利；")        # 1차 글(전각 부호) 그대로

    def test_simplified_traditional_only_folded_in_horizontal_mode(self):
        if not zh.available():
            self.skipTest("opencc 없음")
        text, marks = self._merge("中医古籍之有药论", "中醫古籍之有藥論", fold_zh=True)
        self.assertEqual(marks, 0); self.assertEqual(text, "中医古籍之有药论")                       # 간체 모드: 자형 차이는 접고, 저장 글은 1차(간체) 그대로
        text, marks = self._merge("中医古籍之有药论", "中醫古籍之有藥論", fold_zh=False)
        self.assertGreater(marks, 0)                                                                 # 세로쓰기 모드: 자형이 다르면 표시
        text, marks = self._merge("冬瓜性凉", "冬瓜性温", fold_zh=True)
        self.assertEqual(marks, 1); self.assertIn("〔凉/温〕", text)                                   # 진짜 다른 글자는 그대로 표시


class Accumulate(unittest.TestCase):
    def test_second_section_keeps_first_and_rerun_is_idempotent(self):
        if not zh.available():
            self.skipTest("opencc 없음")
        from yeone_ocr.collate.run import corrected_path
        with tempfile.TemporaryDirectory() as td:
            src = make_book(os.path.join(td, "b.hwpx")); out = corrected_path(src)
            self.assertTrue(out.endswith("b_교정.hwpx")); self.assertEqual(corrected_path(out), out)
            # 1) 大薊 구획 교정 → 교정본 저장
            bk = HwpxBook(src); an = bk.anchors(); sec = bk.section(an, 1)
            ocr = Collate._ocr(Collate("test_classification"))
            items = collate(hwpx_cells(bk.section_texts(sec), sec.start), ocr_cells([(7, "\n".join(ocr))]))
            Editor(bk).apply(items); Editor(bk).save_copy(out)
            # 2) 교정본을 열어 人參 구획 교정(人蔘 → 판독 '人参大补元气甚速' 로 누락 글자 삽입)
            bk2 = HwpxBook(out); an2 = bk2.anchors(); self.assertEqual([a.para for a in an2], [a.para for a in an])
            sec2 = bk2.section(an2, 3)
            items2 = collate(hwpx_cells(bk2.section_texts(sec2), sec2.start), ocr_cells([(9, "人参\n人参,大补元气甚速。")]))
            self.assertTrue(items2); Editor(bk2).apply(items2); Editor(bk2).save_copy(out)
            t = HwpxBook(out).texts()
            self.assertIn("陶云無毒", t[sec.start + 1]); self.assertIn("甚速", t[-1])          # 첫 구획 교정이 남아 있고 둘째 구획도 고쳐짐
            # 3) 같은 구획(大薊)을 한 번 더 돌려도 메모가 겹치지 않는다(이미 고친 글자는 차이가 없고, 표시만 한 자리는 다시 표시하지 않음)
            bk3 = HwpxBook(out); sec3 = bk3.section(bk3.anchors(), 1)
            items3 = collate(hwpx_cells(bk3.section_texts(sec3), sec3.start), ocr_cells([(7, "\n".join(ocr))]))
            self.assertEqual({it.kind for it in items3}, {"B1"})                             # 고쳐진 것은 사라지고 표시만 하던 B1 만 남음
            ed3 = Editor(bk3); log3 = ed3.apply(items3); ed3.save_copy(out)
            self.assertTrue(all(l.ok for l in log3)); self.assertTrue(all("이미 표시" in l.note for l in log3))
            x = zipfile.ZipFile(out).read("Contents/section0.xml").decode()
            self.assertEqual(x.count('type="MEMO"'), 4 + len(items2))                        # 첫 구획 4 + 둘째 구획 메모, 세 번째 실행은 0


class GanYu(unittest.TestCase):
    """于·干·於·幹·乾 묶음 — 문맥 규칙과 PDF 글자층 증인."""
    def setUp(self):
        if not zh.available():
            self.skipTest("opencc 없음")

    def test_context_rule_trusts_hwpx(self):
        from yeone_ocr.collate.collate import gan_yu_guess
        self.assertEqual(gan_yu_guess("有從「於」走而性急"), "于"); self.assertEqual(gan_yu_guess("苦瓠之肉能「幹」冬瓜"), "干"); self.assertEqual(gan_yu_guess("「乾」薑"), "干")
        self.assertEqual(gan_yu_guess("甲「於」乙"), "")
        # 판독이 于→干 으로 잘못 읽어 幹 이 되는 경우: HWPX 於 가 전치사 자리 → 차이로 세지 않음
        self.assertEqual(collate(hwpx_cells(["有從於走而性急也"], 0), ocr_cells([(1, "有从干走而性急也")])), [])
        # 반대로 干→于: HWPX 幹 앞이 能 → HWPX 편
        self.assertEqual(collate(hwpx_cells(["苦瓠之肉能幹"], 0), ocr_cells([(1, "苦瓠之肉能于")])), [])
        # 문맥으로 못 가리면 표시만
        items = collate(hwpx_cells(["甲於乙"], 0), ocr_cells([(1, "甲干乙")]), policy="all")
        self.assertEqual([(it.kind, it.action, it.auto) for it in items], [("B1", "mark", False)]); self.assertIn("干/于 혼동", items[0].note)
        # 문맥 규칙이 판독 편이면 교정 후보로 남는다(HWPX 오탈): 生干 → 生於
        items = collate(hwpx_cells(["生干斯長干斯"], 0), ocr_cells([(1, "生于斯长于斯")]), policy="all")
        self.assertTrue(items); self.assertTrue(all(it.witness == "ocr" and it.new == "於" and it.auto for it in items))

    def test_layer_witness(self):
        from yeone_ocr.collate.collate import witness_filter
        # 문맥으로 못 가린 B1 자리: 글자층이 HWPX 편이면 뺀다
        items = collate(hwpx_cells(["甲於乙丙丁"], 0), ocr_cells([(3, "甲干乙丙丁")]), policy="all")
        st = witness_filter(items, {3: "甲于乙丙丁"}); self.assertEqual(items, []); self.assertEqual((st["checked"], st["hwpx"]), (1, 1))
        # 글자층이 판독 편이면 두 증인이 겹쳐 교정 후보(B2)로
        items = collate(hwpx_cells(["甲於乙丙丁"], 0), ocr_cells([(3, "甲干乙丙丁")]), policy="all")
        st = witness_filter(items, {3: "甲干乙丙丁"}); self.assertEqual([(it.kind, it.auto) for it in items], [("B2", True)]); self.assertIn("모두", items[0].note)
        # 일반 A 차이도 글자층이 HWPX 편이면 뺀다(판독 오류)
        items = collate(hwpx_cells(["津液充滿無問"], 0), ocr_cells([(5, "津液充满无间")]), policy="all")
        witness_filter(items, {5: "津液充满无问"}); self.assertEqual(items, [])
        # 글자층이 없으면 그대로
        items = collate(hwpx_cells(["津液充滿無問"], 0), ocr_cells([(5, "津液充满无间")]), policy="all")
        st = witness_filter(items, {}); self.assertEqual(len(items), 1); self.assertEqual(st["checked"], 0)


class StructuralRules(unittest.TestCase):
    """0.6.0 — 번호·한글·각주·서명·자형 편향·반복 쌍 규칙(중의역대약론선 실측 근거)."""
    def setUp(self):
        if not zh.available():
            self.skipTest("opencc 없음")

    def _items(self, hw, oc, **kw):
        return collate(hwpx_cells(hw, 0), ocr_cells([(1, "\n".join(oc))]), policy="all", **kw)

    def test_variants_added_are_not_differences(self):
        # 澁·汚·沈·昻·濬·着 — 한국식 표기와 인쇄 글자(간체·이체자)는 표기 차이일 뿐
        self.assertEqual(self._items(["味甘治前陰竅澁不利性沈重能泄氣"], ["味甘治前阴窍涩不利性沉重能泄气"]), [])
        self.assertEqual(self._items(["凡著油汚卽屑滑石其上氣味昻"], ["凡着油污即屑滑石其上气味昂"]), [])
        self.assertEqual(self._items(["水同源而異派浚其源"], ["水同源而异派濬其源"]), [])
        self.assertEqual(self._items(["沈氏女科"], ["沈氏女科"]), [])                     # 성씨 沈 도 양쪽 같이 접혀 거짓 차이가 없다

    def test_arabic_numbers_are_notation(self):
        # 인용 번호 "1. 2." 가 HWPX 에는 자동 번호라 글자로 없다 → 표기 차이(D), 항목 없음
        self.assertEqual(self._items(["本草發揮丹溪曰冬瓜"], ["1本草发挥丹溪曰冬瓜"]), [])
        # 「本草」→「4」 처럼 숫자와 글자가 섞인 치환은 표시만
        items = self._items(["液而渴反盛矣本草綱目滑石利竅"], ["液而渴反盛矣4纲目滑石利窍"])
        self.assertTrue(items and all(it.kind == "C" and it.action == "mark" and not it.auto for it in items), [(i.kind, i.action, i.hwpx, i.ocr) for i in items])
        # 한자 숫자끼리의 치환(분량)은 그대로 자동 교정 대상
        items = self._items(["滑石六錢甘草一錢"], ["滑石六钱甘草三钱"])
        self.assertEqual([(it.kind, it.hwpx, it.new, it.auto) for it in items], [("A", "一", "三", True)])

    def test_hangul_gloss_is_never_auto(self):
        hw = ["六一散으로天一生水地六成之의의미를취해서滑石六錢甘草一錢또는辰砂가한처방"]
        oc = ["一名六一散取天一生水地六成之之义滑石六钱甘草一钱或加辰砂"]
        items = self._items(hw, oc)
        self.assertTrue(items)
        self.assertFalse(any(it.auto for it in items), [(i.kind, i.action, i.hwpx, i.ocr) for i in items])
        self.assertTrue(any("한글" in it.note for it in items))

    def test_footnote_insert_is_mark_only(self):
        items = self._items(["之品以退熱也"], ["之品以退热也注1益元散本草备要云益元散一名天水散一名六一散取天一生水地六成之之义滑石六钱甘草一钱或加辰砂"])
        self.assertEqual([(it.kind, it.action, it.auto) for it in items], [("C", "mark", False)]); self.assertIn("각주", items[0].note)

    def test_book_title_fragment_is_not_deleted(self):
        titles = book_titles(["『本草綱目』에 이르기를", "《本草備要》"])
        self.assertIn("本草綱目", titles)
        items = self._items(["赤小豆本草綱目好古曰治水者"], ["赤小豆纲目好古曰治水者"], titles=titles)
        self.assertEqual([(it.kind, it.action, it.auto) for it in items], [("C", "mark", False)]); self.assertIn("서명", items[0].note)
        # 서명 집합이 없으면 지금처럼 두 글자 삭제는 자동
        items = self._items(["赤小豆本草綱目好古曰治水者"], ["赤小豆纲目好古曰治水者"])
        self.assertEqual([(it.kind, it.action) for it in items], [("A", "delete")])

    def test_weak_pairs_are_marked_not_corrected(self):
        # 入/人: HWPX 人, 판독 入 — 인쇄가 人(오자)인 자리도 있어 자동 교정하지 않고 글자 조각 확인으로
        items = self._items(["行通乎小腸能人陰分治有形之病"], ["行通乎小肠能入阴分治有形之病"])
        self.assertEqual([(it.kind, it.action, it.auto) for it in items], [("B1", "mark", False)]); self.assertIn("자형 편향", items[0].note)
        # 글자층이 판독 편이어도 승격하지 않는다
        st = witness_filter(items, {1: "行通乎小肠能入阴分治有形之病"})
        self.assertEqual([(it.kind, it.action) for it in items], [("B1", "mark")]); self.assertEqual(st["weak"], 1)
        # 曰/日 도 같다
        items = self._items(["唐容川曰物下極則反上"], ["唐容川日物下极则反上"])
        self.assertEqual([(it.kind, it.action) for it in items], [("B1", "mark")])

    def test_repeated_pair_is_promoted(self):
        # 幹↔于 가 한 구획에서 5회 이상 되풀이되고 문맥으로도 못 가리면 → 책 전체 변환 오류로 보고 교정 후보(於)로
        hw = ["甲幹乙", "丙幹丁", "戊幹己", "庚幹辛", "壬幹癸", "子幹丑"]
        oc = ["甲于乙", "丙于丁", "戊于己", "庚于辛", "壬于癸", "子于丑"]
        items = self._items(hw, oc)
        self.assertEqual(len(items), 6)
        self.assertTrue(all(it.kind == "B2" and it.action == "replace" and it.new == "於" and it.auto and "repeat" in it.witness for it in items),
                        [(i.kind, i.action, i.new, i.witness) for i in items])
        # 네 번이면 그대로 표시만
        items = self._items(hw[:4], oc[:4])
        self.assertTrue(all(it.kind == "B1" and it.action == "mark" for it in items))


class TrimDedupe(unittest.TestCase):
    def test_repeated_title_lines_are_dropped(self):
        paras = [(1, "车前子"), (1, "车前子"), (1, "1汤液本草东垣"), (2, "车前子"), (2, "2本草纲目"), (2, "小蓟")]
        cut, note = trim_to_section(paras, "車前子", "小薊")
        self.assertEqual([t for _, t in cut], ["车前子", "1汤液本草东垣", "2本草纲目"]); self.assertEqual(note, "")
        # 구획 제목의 일부만으로 된 소제목·머리 제목(车前子 / 车前草)도 첫 줄 뒤로는 거른다
        paras = [(1, "车前子车前草"), (1, "车前子"), (1, "车前子"), (1, "1汤液本草"), (2, "车前草"), (2, "车前草"), (2, "1本草衍义陶隐"), (2, "小蓟")]
        cut, _ = trim_to_section(paras, "車前子·車前草", "小薊")
        self.assertEqual([t for _, t in cut], ["车前子车前草", "1汤液本草", "1本草衍义陶隐"])


class StopCode(unittest.TestCase):
    """0.6.3 검토 — [중지]로 전체 교정이 멈추면 130 을 돌려준다(창 프로그램이 '100% · 완료'로 잘못 보이지 않게)."""
    def test_all_returns_130_when_cancelled(self):
        import fitz
        from argparse import Namespace
        from yeone_ocr import cli
        from yeone_ocr.collate import run as R
        with tempfile.TemporaryDirectory() as td:
            pdf = os.path.join(td, "a.pdf"); d = fitz.open(); d.new_page(); d.save(pdf); d.close()
            src = make_book(os.path.join(td, "b.hwpx"))
            args = Namespace(pdf=pdf, work=os.path.join(td, "w"), hwpx=src, all=True, anchor_source="hwpx", heading_styles="", out=None)
            old_all, old_sec = R.run_all, R.run_section
            try:
                R.run_all = lambda *a, **k: {"stopped": True}           # 구획 몇 개를 하다 [중지]가 눌린 셈
                self.assertEqual(R.cmd_collate(args), 130)
                R.run_all = lambda *a, **k: {"stopped": False}
                self.assertEqual(R.cmd_collate(args), 0)
                # 한 구획(--anchor-index): 쪽 판독 도중 [중지] → 130(오류 아님), 판독 뒤에 눌린 [중지]는 구획을 마저 끝냈으므로 0
                one = Namespace(**{**vars(args), "all": False, "anchor_index": 2})
                def stop_in_read(*a, **k):
                    raise R.Cancelled()
                R.run_section = stop_in_read
                self.assertEqual(R.cmd_collate(one), 130)
                def finish_then_stop(*a, **k):
                    cli.CANCEL.set(); return {}
                R.run_section = finish_then_stop
                self.assertEqual(R.cmd_collate(one), 0)
            finally:
                R.run_all, R.run_section = old_all, old_sec; cli.CANCEL.clear()

    def test_all_stops_with_error_on_fatal_read(self):
        """키·한도 같은 판독 오류는 구획마다 되풀이되므로 전체 교정을 멈추고 1(오류)을 돌려준다 — '100% · 완료'로 보이지 않게."""
        import fitz
        from argparse import Namespace
        from yeone_ocr.collate import run as R
        with tempfile.TemporaryDirectory() as td:
            pdf = os.path.join(td, "a.pdf"); d = fitz.open(); d.new_page(); d.save(pdf); d.close()
            src = make_book(os.path.join(td, "b.hwpx"))
            args = Namespace(pdf=pdf, work=os.path.join(td, "w"), hwpx=src, all=True, anchor_source="hwpx", heading_styles="", out=None)
            calls = []
            def fail(*a, **k):
                calls.append(1); raise R.ReadFailed("판독이 끝나지 않음(rc=1)")
            old_sec, old_find = R.run_section, R.find_title_pages
            try:
                R.run_section = fail; R.find_title_pages = lambda *a, **k: (1, 1)
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(R.cmd_collate(args), 1)
                self.assertEqual(len(calls), 1)                                   # 첫 오류에서 멈춤(다음 구획에서 또 부르지 않음)
                summ = json.loads(open(os.path.join(td, "w", "collate", "_전체_요약.json"), encoding="utf-8").read())
                self.assertTrue(summ["failed"])
            finally:
                R.run_section, R.find_title_pages = old_sec, old_find

    def test_ensure_pages_read_signals_stop(self):
        """판독이 [중지]로 130 을 돌려주면 오류(RuntimeError)가 아니라 Cancelled — 창에 '멈춤(오류)'·추적 글이 뜨지 않게."""
        from argparse import Namespace
        from yeone_ocr import cli
        from yeone_ocr.collate import run as R
        class W:
            def load_record(self, n): return None
        old = cli.cmd_prepare, cli.cmd_read
        try:
            cli.cmd_prepare = lambda *a: 0
            cli.cmd_read = lambda *a: 130
            with self.assertRaises(R.Cancelled):
                R.ensure_pages_read(Namespace(pages=None), W(), None, (1, 2))
            cli.cmd_read = lambda *a: 2
            with self.assertRaises(RuntimeError):
                R.ensure_pages_read(Namespace(pages=None), W(), None, (1, 2))
        finally:
            cli.cmd_prepare, cli.cmd_read = old; cli.CANCEL.clear()

    def test_labels_import_without_heavy_packages(self):
        import subprocess
        code = ("import sys; sys.modules['lxml'] = None; sys.modules['fitz'] = None\n"
                "from yeone_ocr.collate.labels import POLICIES, ANCHOR_SOURCES\n"
                "assert 'all' in POLICIES and 'auto' in ANCHOR_SOURCES\n")
        r = subprocess.run([sys.executable, "-c", code], cwd=str(Path(__file__).resolve().parents[1]), capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)


def make_paras(path: str, paras: list[str]) -> str:
    """바탕글 문단만으로 된 작은 HWPX."""
    from hwpx.document import HwpxDocument
    doc = HwpxDocument.new(); body = doc.ensure_run_style(font="함초롬바탕", size=11)
    for t in paras:
        p = doc.add_paragraph("", para_pr_id_ref=0, style_id_ref=0, char_pr_id_ref=body, include_run=False, inherit_style=False)
        p.add_run(t, char_pr_id_ref=body)
    doc.save_to_path(path)
    return path


class MemoAndMarks(unittest.TestCase):
    """0.6.4 검토 — inline 의 폭 있는 고지, 빨강 위 다시 칠하기, 보고의 메모 방식, 일반 OCR 문서 위 교정."""
    HH = "{http://www.hancom.co.kr/hwpml/2011/head}"

    def setUp(self):
        if not zh.available():
            self.skipTest("opencc 없음")

    def _look(self, ed, run):
        """(글자 색, 형광펜, 굵게, 밑줄 종류)."""
        el = ed._char_pr(run.get("charPrIDRef"))
        return el.get("textColor"), el.get("shadeColor"), el.find(f"{self.HH}bold") is not None, el.find(f"{self.HH}underline").get("type")

    def test_inline_ranged_notice_gets_short_note(self):
        from yeone_ocr.grades import needs_memo
        ocr = Collate._ocr(Collate("test_classification"))
        with tempfile.TemporaryDirectory() as td:
            # 반영하지 않은 제안(정책 '메모만'): 폭 있는 A 치환도 범위 뒤에 제안 글자를 남긴다
            out = os.path.join(td, "b_교정.hwpx")
            bk = HwpxBook(make_book(os.path.join(td, "b.hwpx"))); sec = bk.section(bk.anchors(), 1)
            items = collate(hwpx_cells(bk.section_texts(sec), sec.start), ocr_cells([(7, "\n".join(ocr))]), policy="memo")
            ed = Editor(bk, memo="inline"); log = ed.apply(items); ed.save_copy(out)
            self.assertTrue(all(l.ok and not l.note for l in log))
            t = bk.texts()[sec.start:sec.end]
            self.assertIn("陶云有〔제안: 無〕毒", t[1])
            self.assertEqual("".join(t).count("〔"), sum(1 for it in items if needs_memo(it, "inline")))   # 고지마다 글 속 표기 하나
            # 다시 돌려도 겹치지 않는다
            bk2 = HwpxBook(out); sec2 = bk2.section(bk2.anchors(), 1)
            log2 = Editor(bk2, memo="inline").apply(collate(hwpx_cells(bk2.section_texts(sec2), sec2.start), ocr_cells([(7, "\n".join(ocr))]), policy="memo"))
            self.assertTrue(log2 and all("이미 표시" in l.note for l in log2)); self.assertEqual(bk2.texts(), HwpxBook(out).texts())
            # 한글 풀이(B1, 폭 2, 고지) — 정책 '전부 자동'에서도 표시만 하고 범위 뒤에 판독 글자를 남긴다
            bk3 = HwpxBook(make_paras(os.path.join(td, "k.hwpx"), ["但用於초기之血症"])); pi = bk3.texts().index("但用於초기之血症")
            items3 = collate(hwpx_cells(["但用於초기之血症"], pi), ocr_cells([(1, "但用于初起之血症")]), policy="all")
            self.assertEqual([(it.kind, it.end - it.start, it.auto) for it in items3], [("B1", 2, False)])
            Editor(bk3, memo="inline").apply(items3)
            self.assertEqual(bk3.texts()[pi], "但用於초기〔확인: 판독 初起〕之血症")

    def test_run_section_counts_real_memos_and_reports_mode(self):
        from argparse import Namespace
        from types import SimpleNamespace
        from yeone_ocr import cli
        from yeone_ocr.collate import run as R
        from yeone_ocr.schema import PageReading
        ocr = Collate._ocr(Collate("test_classification"))
        with tempfile.TemporaryDirectory() as td:
            src = make_book(os.path.join(td, "b.hwpx")); out = os.path.join(td, "b_교정.hwpx")
            rec = {"merged": PageReading.model_validate({"pdf_page": 7, "regions": [{"id": "r1", "kind": "본문", "bbox": [0, 0, 1000, 1000],
                                                                                    "paras": [{"cols": [{"n": 1, "t": t}]} for t in ocr]}]}).model_dump()}
            work = SimpleNamespace(dir=Path(td), load_record=lambda n: rec, textlayer_path=lambda n: Path(td, "없음.json"))
            args = Namespace(pdf_pages="7", apply=True, memo="inline", policy="memo", out=out, no_glyph_check=True)
            logs = []; old = R.log, R.ensure_pages_read, cli.resolve_mode
            try:
                R.log = logs.append; R.ensure_pages_read = lambda *a, **k: None; cli.resolve_mode = lambda *a, **k: None
                for _ in range(2):                                     # 두 번째는 같은 교정본에 다시 — 모두 '이미 표시된 자리'
                    bk = HwpxBook(out if os.path.exists(out) else src); an = bk.anchors()
                    R.run_section(args, pdf_doc=[None] * 7, work=work, book=bk, anchors=an, pdf_anchors=[], matches=[SimpleNamespace(pdf=None, how="none")] * len(an), idx=1)
            finally:
                R.log, R.ensure_pages_read, cli.resolve_mode = old
            counts = [l for l in logs if l.startswith("표시:")]
            self.assertIn("메모 4건(글 속 〔〕)", counts[0]); self.assertIn("메모 0건(글 속 〔〕)", counts[1])
            rep = Path(td, "collate", "大薊.html").read_text(encoding="utf-8")
            self.assertIn("글 속 〔〕 표기", rep); self.assertNotIn("고지 메모", rep)            # inline 에는 메모가 없다

    def test_report_marks_memo_by_real_mode(self):
        from types import SimpleNamespace
        from yeone_ocr.collate.run import report_html
        from yeone_ocr.collate.collate import Item
        sec = SimpleNamespace(anchor=SimpleNamespace(name="大薊"), start=1, end=3)
        items = [Item("A", "replace", 1, 0, 1, "有", "无", "無", auto=True), Item("B1", "mark", 1, 3, 3, "", "胎", "")]
        with tempfile.TemporaryDirectory() as td:
            got = {}
            for mode in ("notice", "all", "inline"):
                p = Path(td, f"{mode}.html"); report_html(sec, None, (7, 7), [], [], items, "all", None, p, mode=mode); got[mode] = p.read_text(encoding="utf-8")
            self.assertEqual(got["notice"].count("· 고지 메모</small>"), 1)                          # 폭 없는 표시만
            self.assertEqual(got["all"].count("· 메모</small>"), 2)                                   # 모든 항목
            self.assertEqual(got["inline"].count("· 글 속 〔〕 표기</small>"), 1); self.assertNotIn("고지 메모", got["inline"])

    def test_recolor_over_red_drops_yellow_and_bold(self):
        from yeone_ocr.collate.hwpx_edit import _slots
        ocr = Collate._ocr(Collate("test_classification"))
        with tempfile.TemporaryDirectory() as td:
            bk = HwpxBook(make_book(os.path.join(td, "b.hwpx"))); sec = bk.section(bk.anchors(), 1)
            ed = Editor(bk, memo="notice"); ed.apply(collate(hwpx_cells(bk.section_texts(sec), sec.start), ocr_cells([(7, "\n".join(ocr))]), policy="memo"))
            out = ed.save_copy(os.path.join(td, "o.hwpx"))                                           # 1회: 有 는 빨강(표시만)
            bk2 = HwpxBook(out); sec2 = bk2.section(bk2.anchors(), 1)
            items = collate(hwpx_cells(bk2.section_texts(sec2), sec2.start), ocr_cells([(7, "\n".join(ocr))]), policy="all")
            next(it for it in items if it.kind == "A").witness = "glyph=ocr"                         # 2회: 3차가 판독 글자를 고름 → 초록 치환
            ed2 = Editor(bk2, memo="notice"); ed2.apply(items)
            run = next(s.run for s in _slots(bk2.paragraphs[sec2.start + 1].element) if s.text == "無")
            self.assertEqual(self._look(ed2, run), ("#0B7A3B", "none", False, "BOTTOM"))             # 빨강의 노랑·굵게를 물려받지 않음
            # 원래 굵은 글자(등급 색 아님)는 초록으로 고쳐도 굵게 그대로
            bold = ed2.doc.styles.ensure_run(bold=True, color="#000000", highlight="none", base_char_pr_id=run.get("charPrIDRef"))
            self.assertIsNotNone(ed2._char_pr(ed2._style_for(str(bold), "green")).find(f"{self.HH}bold"))

    def test_ocr_made_hwpx_gets_collate_legend_and_marks(self):
        from yeone_ocr.blocks import Block
        from yeone_ocr.grades import LEGEND, COLLATE_LEGEND
        from yeone_ocr.hwpx_builder import build_hwpx
        from yeone_ocr.collate.collate import Item
        from yeone_ocr.collate.hwpx_edit import legend_head, _slots
        with tempfile.TemporaryDirectory() as td:
            for memos in (True, False):                                    # 일반 OCR 범례가 메모로, 또는 글 속 〔고지: …〕 로
                src = os.path.join(td, f"g{memos}.hwpx")
                build_hwpx([Block(type="meta", text="[book.pdf / PDF 3쪽]", memos=[(0, LEGEND)]),
                            Block(type="para", kind="본문", text="大黃日一兩又能〔入/人〕肝經主之", spans=[(2, 3, "green"), (8, 9, "red_ctx")])], src, memos=memos)
                bk = HwpxBook(src); pi = next(i for i, t in enumerate(bk.texts()) if "大黃" in t)
                marks = lambda: [Item("B1", "mark", pi, 2, 3, "日", "曰", "", note="확인"), Item("B1", "mark", pi, 8, 9, "入", "人", "", note="확인")]
                ed = Editor(bk, memo="notice"); self.assertEqual(ed._ours, set())               # 일반 OCR 의 초록·빨강은 교정 표시가 아님
                self.assertEqual([l.note for l in ed.apply(marks())], ["", ""])                 # 3차 초록 글자·문맥 후보 위에도 빨강 표시
                runs = {s.text: s.run for s in _slots(bk.paragraphs[pi].element)}
                self.assertEqual(self._look(ed, runs["日"]), ("#C00000", "#FFFF00", True, "NONE"))   # 초록의 점선 밑줄을 물려받지 않음
                self.assertTrue(ed.add_legend_memo()); self.assertFalse(ed.add_legend_memo())    # 일반 OCR 범례는 교정 범례로 치지 않음
                out = ed.save_copy(os.path.join(td, f"c{memos}.hwpx"))
                x = zipfile.ZipFile(out).read("Contents/section0.xml").decode()
                self.assertEqual(x.count(legend_head(COLLATE_LEGEND)), 1); self.assertEqual(x.count(legend_head(LEGEND)), 1)
                ed2 = Editor(HwpxBook(out), memo="notice")
                self.assertIn(runs["日"].get("charPrIDRef"), ed2._ours); self.assertNotIn(runs["〔"].get("charPrIDRef"), ed2._ours)
                self.assertTrue(all("이미 표시" in l.note for l in ed2.apply(marks())))           # 교정이 칠한 자리는 다시 표시하지 않음

    def test_untagged_marks_in_old_corrected_copy_still_count(self):
        """표지 없는 예전 교정본(일반 OCR 범례 없음)의 등급 색은 그대로 '이미 표시된 자리'."""
        ocr = Collate._ocr(Collate("test_classification"))
        with tempfile.TemporaryDirectory() as td:
            bk = HwpxBook(make_book(os.path.join(td, "b.hwpx"))); sec = bk.section(bk.anchors(), 1)
            ed = Editor(bk); ed.apply(collate(hwpx_cells(bk.section_texts(sec), sec.start), ocr_cells([(7, "\n".join(ocr))])))
            for s in ed._header().element.iter(f"{self.HH}strikeout"):
                s.set("color", "#000000")                                                       # 표지 없던 때의 교정본처럼
            out = ed.save_copy(os.path.join(td, "o.hwpx"))
            bk2 = HwpxBook(out); sec2 = bk2.section(bk2.anchors(), 1)
            log = Editor(bk2).apply(collate(hwpx_cells(bk2.section_texts(sec2), sec2.start), ocr_cells([(7, "\n".join(ocr))])))
            self.assertTrue(log and all("이미 표시" in l.note for l in log))


class NotesAndLegacy(unittest.TestCase):
    """0.6.4 검토 2차 — 괄호 안 글 속 표기·작업 표시 대조 제외·표지 이전 교정본·원래 형광펜·제목 문단·표지 만들기·제안 메모·메모 수."""
    HH = "{http://www.hancom.co.kr/hwpml/2011/head}"
    HP = "{http://www.hancom.co.kr/hwpml/2011/paragraph}"

    def _logs(self):
        """hwpx_edit.log 를 모으는 목록(시험이 끝나면 되돌림)."""
        from yeone_ocr.collate import hwpx_edit
        got, old = [], getattr(hwpx_edit, "log", None)
        hwpx_edit.log = got.append
        self.addCleanup(setattr, hwpx_edit, "log", old)
        return got

    def test_collate_skips_own_work_notes(self):
        # 일반 OCR 작업 표시(〔고지: …〕)·교정의 글 속 표기(〔확인: …〕, 겹친 예전 표기 포함)는 대조하지 않고, 〔a/b〕·〔판독불명〕은 지금처럼
        t = "甲〔고지: 풀이〕乙〔入/人〕丙〔확인: 판독 胎〕丁〔고지: 어긋남〔확인: 판독 -〕〕戊〔제안: 無〕己〔판독불명〕"
        self.assertEqual("".join(c.ch for c in hwpx_cells([t], 0)), "甲乙入人丙丁戊己판독불명")
        self.assertEqual([c.pos for c in hwpx_cells([t], 0)][:3], [0, 9, 11])                       # 글자 번호는 원래 글 그대로
        if not zh.available():
            return
        from yeone_ocr.blocks import Block
        from yeone_ocr.grades import LEGEND
        from yeone_ocr.hwpx_builder import build_hwpx
        with tempfile.TemporaryDirectory() as td:
            src = os.path.join(td, "g.hwpx")                                          # 메모를 끈 일반 OCR 문서: 고지가 글 속 〔고지: …〕
            build_hwpx([Block(type="meta", text="[book.pdf / PDF 3쪽]", memos=[(0, LEGEND)]),
                        Block(type="para", kind="본문", text="大黃日一兩又能入肝經主之", memos=[(3, "자형·문맥 어긋남")])], src, memos=False)
            bk = HwpxBook(src); pi = next(i for i, t in enumerate(bk.texts()) if "大黃" in t)
            self.assertIn("〔고지: 자형·문맥 어긋남〕", bk.texts()[pi])
            self.assertEqual(collate(hwpx_cells([bk.texts()[pi]], pi), ocr_cells([(3, "大黄日一两又能入肝经主之")]), policy="all"), [])   # 한글 풀이(B1)로 잡지 않음

    def test_inline_note_never_inside_existing_bracket(self):
        from yeone_ocr.collate.collate import Item
        with tempfile.TemporaryDirectory() as td:
            out = os.path.join(td, "o.hwpx")
            bk = HwpxBook(make_paras(os.path.join(td, "k.hwpx"), ["又能〔入/人〕肝經"])); pi = bk.texts().index("又能〔入/人〕肝經")
            items = lambda: [Item("B1", "mark", pi, 4, 4, "", "胎", ""), Item("B1", "mark", pi, 5, 6, "人", "入", "", notice=True)]   # 폭 없는 표시·폭 있는 고지, 둘 다 〔入/人〕 안
            ed = Editor(bk, memo="inline"); log = ed.apply(items()); ed.save_copy(out)
            self.assertEqual(bk.texts()[pi], "又能〔入/人〕〔확인: 판독 胎〕〔확인: 판독 入〕肝經")       # 표기는 감싼 〕 뒤에 — 괄호가 겹치지 않음
            self.assertTrue(all(l.ok and not l.note and l.memo for l in log), [(l.ok, l.note) for l in log])
            bk2 = HwpxBook(out); log2 = Editor(bk2, memo="inline").apply(items())          # 다시 돌려도 그대로
            self.assertTrue(all("이미 표시" in l.note and not l.memo for l in log2)); self.assertEqual(bk2.texts()[pi], bk.texts()[pi])

    def _legacy_copy(self, td, mode, memos=True):
        """표지를 달기 전 판(0.6.4–0.6.5, 410d330)이 쓴 교정본 그대로: 표지 없는 등급 색 글자, 일반 OCR 범례가 메모인 문서·inline 에는
        교정 범례 없음(그 판은 '[연어OCR 표시 범례' 로 시작하는 메모가 있으면 범례를 달지 않았다), inline 은 글 속 표기. 그 판과 같은 글을 쓰는
        항목(표시·폭 없는 표시)만 써서, 이 판의 Editor 를 표지 없이 돌려 만든다(410d330 의 출력과 XML 이 같음을 따로 확인함)."""
        from yeone_ocr.blocks import Block
        from yeone_ocr.grades import LEGEND
        from yeone_ocr.hwpx_builder import build_hwpx
        from yeone_ocr.collate.collate import Item
        src = os.path.join(td, f"g{mode}{memos}.hwpx")
        build_hwpx([Block(type="meta", text="[book.pdf / PDF 3쪽]", memos=[(0, LEGEND)]),
                    Block(type="para", kind="본문", text="大黃日一兩又能〔入/人〕肝經主之", spans=[(2, 3, "green"), (8, 9, "red_ctx")])], src, memos=memos)
        bk = HwpxBook(src); pi = next(i for i, t in enumerate(bk.texts()) if "大黃" in t)
        marks = lambda: [Item("B1", "mark", pi, 0, 1, "大", "太", "", note="확인"), Item("B1", "mark", pi, 14, 14, "", "胎", "")]   # 표시·폭 없는 표시(고지)
        ed = Editor(bk, memo=mode); ed._legacy = True; ed.apply(marks())
        if mode != "inline" and not memos:
            ed.add_legend_memo()                                                        # 일반 OCR 범례가 글 속이면 그 판도 교정 범례를 달았다
        return ed.save_copy(os.path.join(td, f"old{mode}{memos}.hwpx")), pi, marks

    def test_legacy_untagged_copy_of_ocr_made_doc_counts_as_marked(self):
        from yeone_ocr.grades import COLLATE_LEGEND
        from yeone_ocr.hwpx_memo import count_memos
        from yeone_ocr.collate.collate import Item
        from yeone_ocr.collate.hwpx_edit import _tag_of, legend_head
        got = self._logs()
        with tempfile.TemporaryDirectory() as td:
            for mode in ("notice", "inline"):                                           # 메모 교정본(교정 범례 없음)·inline 교정본(범례 없이 글 속 표기)
                old, pi, marks = self._legacy_copy(td, mode); got.clear()
                self.assertNotIn(legend_head(COLLATE_LEGEND), zipfile.ZipFile(old).read("Contents/section0.xml").decode())
                bk2 = HwpxBook(old); n0 = count_memos(p.element for p in bk2.paragraphs); t0 = bk2.texts()[pi]
                self.assertEqual(t0.count("〔확인: 판독 胎〕"), int(mode == "inline"))
                ed2 = Editor(bk2, memo=mode); self.assertTrue(ed2._legacy)
                self.assertTrue(any("표지를 달기 전 판" in m for m in got), got)           # 알림
                self.assertTrue(all("이미 표시" in l.note for l in ed2.apply(marks())))       # 예전 표시를 다시 달지 않음(글 속 표기도 겹치지 않음)
                self.assertEqual(bk2.texts()[pi], t0)
                ed2.apply([Item("B1", "mark", pi, 1, 2, "黃", "簧", "", note="확인")])     # 새 표시도 표지 없이 — 다음 구획(같은 책)·다음 실행도 같은 셈
                self.assertFalse([el for el in ed2._header().element.iter(f"{self.HH}charPr")
                                  if (el.get("textColor") or "").upper() in ("#C00000", "#0B7A3B", "#1F4FBF") and _tag_of(el) == el.get("textColor").upper()])
                self.assertTrue(all("이미 표시" in l.note for l in Editor(bk2, memo=mode).apply(marks())))
                self.assertEqual(count_memos(p.element for p in bk2.paragraphs), n0); self.assertEqual(bk2.texts()[pi], t0)

    def test_collate_legend_alone_is_not_a_legacy_copy(self):
        """교정 범례는 있는데 표지 달린 글자 모양도 이전 교정의 메모·글 속 표기도 없으면(표지만 사라진 이 판의 교정본 …) 일반 OCR 이 칠한
        빨강·초록을 교정 표시로 치지 않는다 — 그 위의 새 교정 표시가 '이미 표시된 자리'로 조용히 빠지지 않게."""
        from yeone_ocr.blocks import Block
        from yeone_ocr.grades import LEGEND
        from yeone_ocr.hwpx_builder import build_hwpx
        from yeone_ocr.collate.collate import Item
        got = self._logs()
        with tempfile.TemporaryDirectory() as td:
            src = os.path.join(td, "g.hwpx")
            build_hwpx([Block(type="meta", text="[book.pdf / PDF 3쪽]", memos=[(0, LEGEND)]),
                        Block(type="para", kind="본문", text="大黃日一兩又能〔入/人〕肝經主之", spans=[(2, 3, "green"), (8, 9, "red_ctx")])], src, memos=True)
            bk = HwpxBook(src); pi = next(i for i, t in enumerate(bk.texts()) if "大黃" in t)
            ed = Editor(bk, memo="notice"); ed.apply([Item("B1", "mark", pi, 0, 1, "大", "太", "", note="확인")]); self.assertTrue(ed.add_legend_memo())
            for s in ed._header().element.iter(f"{self.HH}strikeout"):
                s.set("color", "#000000")                                               # 표지가 사라짐
            ed2 = Editor(HwpxBook(ed.save_copy(os.path.join(td, "c.hwpx"))), memo="notice")
            self.assertFalse(ed2._legacy); self.assertFalse(got)
            self.assertEqual([l.note for l in ed2.apply([Item("B1", "mark", pi, 8, 9, "入", "人", "", note="확인")])], [""])   # 일반 OCR 빨강 위에도 표시

    def test_legacy_warning_once_per_book_without_version(self):
        got = self._logs()
        with tempfile.TemporaryDirectory() as td:
            old = self._legacy_copy(td, "notice", memos=False)[0]                      # 메모를 끈 일반 OCR 문서: 그 판도 교정 범례를 달았다
            bk = HwpxBook(old)
            for _ in range(3):                                                          # 전체 교정은 구획마다 Editor 를 새로 만든다
                self.assertTrue(Editor(bk, memo="notice")._legacy)
            self.assertEqual(len(got), 1, got)
            self.assertIn("표지를 달기 전 판의 교정본", got[0]); self.assertNotIn("0.6.", got[0])   # 이 판 번호와 헷갈리지 않게
            self.assertTrue(Editor(HwpxBook(old), memo="notice")._legacy); self.assertEqual(len(got), 2)     # 책(실행)이 바뀌면 다시 알림

    def test_human_memo_or_typed_note_is_not_a_legacy_trace(self):
        """일반 OCR 문서에 사람이 단 메모('[확인] …', 다른 작성자)나 손으로 친 〔확인〕 만 있으면 표지 이전 교정본으로 보지 않는다
        (그러면 일반 OCR 이 칠한 빨강·초록 위의 교정 표시가 '이미 표시된 자리'로 조용히 빠졌다)."""
        from lxml import etree
        from yeone_ocr.blocks import Block
        from yeone_ocr.grades import LEGEND
        from yeone_ocr.hwpx_builder import build_hwpx
        from yeone_ocr.hwpx_memo import memo_field, memo_shape_id
        from yeone_ocr.collate.collate import Item
        from yeone_ocr.collate.hwpx_edit import _slots
        got = self._logs()
        with tempfile.TemporaryDirectory() as td:
            for case in ("human_memo", "body_note"):
                src = os.path.join(td, f"g_{case}.hwpx")
                text = "大黃日一兩又能〔入/人〕肝經主之" + ("〔확인〕" if case == "body_note" else "")
                build_hwpx([Block(type="meta", text="[book.pdf / PDF 3쪽]", memos=[(0, LEGEND)]),
                            Block(type="para", kind="본문", text=text, spans=[(2, 3, "green"), (8, 9, "red_ctx")])], src, memos=True)
                if case == "human_memo":
                    bk = HwpxBook(src); pi = next(i for i, t in enumerate(bk.texts()) if "大黃" in t)
                    t0 = _slots(bk.paragraphs[pi].element)[0].t
                    b, e = memo_field("[확인] 이 줄 원본과 다시 대조할 것", 99, "홍길동", memo_shape_id(bk.doc))
                    empty = etree.Element(f"{self.HP}t"); empty.text = ""; t0.addprevious(b); b.addnext(empty); empty.addnext(e)
                    src = Editor(bk, memo="notice").save_copy(os.path.join(td, "g_human.hwpx"))
                got.clear()
                bk = HwpxBook(src); pi = next(i for i, t in enumerate(bk.texts()) if "大黃" in t)
                ed = Editor(bk, memo="notice")
                lg = ed.apply([Item("B1", "mark", pi, 8, 9, "入", "人", "", note="확인"), Item("B1", "mark", pi, 2, 3, "日", "曰", "", note="확인")])
                self.assertFalse(ed._legacy, case); self.assertEqual(got, [], case)
                self.assertTrue(all(x.ok and not x.note for x in lg), (case, [(x.ok, x.note) for x in lg]))     # 일반 OCR 색 위에도 교정 표시가 들어간다

    def _run_section(self, td, bk, an, idx, items, mode="inline"):
        """run_section 을 판독·대조 없이(collate 결과 = items) 돌린다. (기록 줄, 교정본 경로)."""
        from argparse import Namespace
        from types import SimpleNamespace
        from yeone_ocr import cli
        from yeone_ocr.collate import run as R
        out = os.path.join(td, "o_교정.hwpx")
        work = SimpleNamespace(dir=Path(td), load_record=lambda n: None, textlayer_path=lambda n: Path(td, "없음.json"))
        args = Namespace(pdf_pages="7", apply=True, memo=mode, policy="memo", out=out, no_glyph_check=True)
        logs = []; old = R.log, R.ensure_pages_read, cli.resolve_mode, R.collate
        try:
            R.log = logs.append; R.ensure_pages_read = lambda *a, **k: None; cli.resolve_mode = lambda *a, **k: None; R.collate = lambda *a, **k: list(items)
            R.run_section(args, pdf_doc=[None] * 7, work=work, book=bk, anchors=an, pdf_anchors=[], matches=[SimpleNamespace(pdf=None, how="none")] * len(an), idx=idx)
        finally:
            R.log, R.ensure_pages_read, cli.resolve_mode, R.collate = old
        return logs, out

    def test_inline_keeps_pdf_title_anchors(self):
        """제목 스타일·책갈피가 없어 PDF 책갈피 제목으로 찾은 구획 기준 문단(--anchor-source pdf·자동 대체)에도 글 속 표기를 넣지 않는다."""
        from yeone_ocr.collate.collate import Item
        with tempfile.TemporaryDirectory() as td:
            bk = HwpxBook(make_paras(os.path.join(td, "p.hwpx"), ["各論", "대계(大薊)", "大薊根대계陶云有毒, 誤也.", "小薊", "小薊, 性涼."]))
            titles = [("各論", 1), ("대계(大薊)", 2), ("小薊", 2)]
            an = [a for _, a in bk.anchors_from_titles(titles) if a is not None]; h = an[1].para
            self.assertEqual(bk.anchors(), []); self.assertEqual(len(an), 3)
            items = [Item("B1", "mark", h, 0, 2, "대계", "", "", notice=True), Item("B1", "mark", h + 1, 3, 5, "대계", "", "", notice=True)]   # 한글 풀이 고지
            logs, out = self._run_section(td, bk, an, 1, items)
            t = HwpxBook(out).texts()
            self.assertEqual(t[h], "대계(大薊)")                                                  # 제목(구획 이름)은 색만
            self.assertEqual(t[h + 1], "大薊根대계〔확인: 한글 풀이〕陶云有毒, 誤也.")
            self.assertEqual([a.para for _, a in HwpxBook(out).anchors_from_titles(titles) if a is not None], [a.para for a in an])   # 다음 실행도 같은 구획
            self.assertIn("메모 1건(글 속 〔〕)", next(l for l in logs if l.startswith("표시:")))

    def test_hidden_heading_marks_are_logged_once(self):
        """inline 제목 문단의 지운 글자·폭 없는 표시는 색 칠할 글자도 없어 문서에 안 보인다 — 구획마다 한 줄로 알린다."""
        from yeone_ocr.collate.collate import Item
        with tempfile.TemporaryDirectory() as td:
            bk = HwpxBook(make_book(os.path.join(td, "b.hwpx"))); an = bk.anchors(); h = an[1].para
            items = [Item("A", "delete", h, 0, 1, "大", "", "", auto=True), Item("B1", "mark", h, 2, 2, "", "胎", ""),   # 자동 삭제·폭 없는 제안(제목)
                     Item("B1", "mark", h, 1, 2, "薊", "蓟", "", notice=True), Item("B1", "mark", h + 1, 3, 3, "", "胎", "")]   # 색은 보임·본문
            logs, out = self._run_section(td, bk, an, 1, items)
            t = HwpxBook(out).texts()
            self.assertEqual(t[h], "薊"); self.assertEqual(t[h + 1].count("〔확인: 판독 胎〕"), 1)
            hid = [l for l in logs if l.startswith("제목 문단의 표시")]
            self.assertEqual(len(hid), 1, logs); self.assertIn("2건", hid[0]); self.assertIn("보고(html)에만", hid[0])
            logs2, _ = self._run_section(td, HwpxBook(make_book(os.path.join(td, "b2.hwpx"))), an, 1, items[3:])
            self.assertFalse([l for l in logs2 if l.startswith("제목 문단의 표시")])                 # 없으면 알리지 않음

    def test_green_keeps_authors_own_shading(self):
        from hwpx.document import HwpxDocument
        with tempfile.TemporaryDirectory() as td:
            doc = HwpxDocument.new(); body = doc.ensure_run_style(font="함초롬바탕", size=11)
            shaded = doc.ensure_run_style(font="함초롬바탕", size=11, highlight="#FFE0E0")
            p = doc.add_paragraph("", para_pr_id_ref=0, style_id_ref=0, char_pr_id_ref=body, include_run=False, inherit_style=False)
            p.add_run("大薊根陶云有毒誤也", char_pr_id_ref=shaded); path = os.path.join(td, "sh.hwpx"); doc.save_to_path(path)
            ed = Editor(HwpxBook(path), memo="notice")
            self.assertEqual(ed._char_pr(ed._style_for(str(shaded), "green")).get("shadeColor"), "#FFE0E0")   # 등급 색이 아닌 원래 형광펜은 그대로
            red = ed._style_for(str(shaded), "red")
            self.assertEqual(ed._char_pr(ed._style_for(red, "green")).get("shadeColor"), "none")              # 빨강 위 초록은 노랑을 물려받지 않음

    def test_inline_keeps_headings_and_names_hangul_gloss(self):
        if not zh.available():
            self.skipTest("opencc 없음")
        from hwpx.document import HwpxDocument
        with tempfile.TemporaryDirectory() as td:
            doc = HwpxDocument.new(); body = doc.ensure_run_style(font="함초롬바탕", size=11)
            def para(t):
                p = doc.add_paragraph("", para_pr_id_ref=0, style_id_ref=0, char_pr_id_ref=body, include_run=False, inherit_style=False); p.add_run(t, char_pr_id_ref=body)
            doc.add_heading("各論", level=1); doc.add_heading("대계(大薊)", level=2); para("大薊根대계陶云有毒, 誤也."); doc.add_heading("小薊", level=2); para("小薊, 性涼.")
            path = os.path.join(td, "h.hwpx"); doc.save_to_path(path)
            bk = HwpxBook(path); an = bk.anchors(); sec = bk.section(an, 1)
            items = collate(hwpx_cells(bk.section_texts(sec), sec.start), ocr_cells([(7, "大蓟\n大蓟根陶云有毒, 误也.")]), policy="all")
            self.assertEqual(sorted((it.kind, it.para - sec.start, it.hwpx, it.notice) for it in items), [("B1", 0, "대계", True), ("B1", 1, "대계", True)])
            ed = Editor(bk, memo="inline"); log = ed.apply(items)
            t = bk.texts()
            self.assertEqual(t[sec.start], "대계(大薊)")                                        # 제목(구획 이름)은 색만
            self.assertEqual(t[sec.start + 1], "大薊根대계〔확인: 한글 풀이〕陶云有毒, 誤也.")    # 판독 글자 없는 한글 풀이 자리
            self.assertEqual(sorted((l.item.para - sec.start, l.ok, l.memo) for l in log), [(0, True, False), (1, True, True)])   # 제목에는 표기 없음
            head = next(r for r in bk.paragraphs[sec.start].element.iter(f"{self.HP}run") if "".join(r.itertext()) == "대계")
            self.assertEqual(ed._char_pr(head.get("charPrIDRef")).get("textColor"), "#C00000")
            out = ed.save_copy(os.path.join(td, "o.hwpx"))
            self.assertEqual([a.name for a in HwpxBook(out).anchors()], [a.name for a in an])

    def test_tag_creates_missing_strikeout_and_reports_failure(self):
        from yeone_ocr.collate.hwpx_edit import _tag_of, _local
        got = self._logs()
        with tempfile.TemporaryDirectory() as td:
            bk = HwpxBook(make_paras(os.path.join(td, "k.hwpx"), ["大薊根陶云有毒"])); pi = bk.texts().index("大薊根陶云有毒")
            ed = Editor(bk, memo="notice"); base = bk.paragraphs[pi].element.find(f"{self.HP}run").get("charPrIDRef")
            el = ed._char_pr(base); el.remove(el.find(f"{self.HH}strikeout"))                # 취소선 요소가 없는 글자 모양(다른 프로그램의 HWPX)
            new = ed._char_pr(ed._tag(base, "#C00000"))
            self.assertEqual(_tag_of(new), "#C00000"); self.assertEqual(new.find(f"{self.HH}strikeout").get("shape"), "NONE")
            kids = [_local(c.tag) for c in new]; self.assertEqual(kids[kids.index("strikeout") + 1], "outline")   # 제자리(밑줄 뒤, 외곽선 앞)
            self.assertIsNone(el.find(f"{self.HH}strikeout"))                                # 원래 글자 모양은 그대로
            self.assertFalse(got)
            self.assertEqual(ed._tag("999999", "#C00000"), "999999"); self.assertTrue(any("표지를 달지 못함" in m for m in got), got)

    def test_unapplied_suggestion_memo_does_not_claim_correction(self):
        if not zh.available():
            self.skipTest("opencc 없음")
        ocr = Collate._ocr(Collate("test_classification"))
        with tempfile.TemporaryDirectory() as td:
            # 정책 '메모만'의 반영하지 않은 A 치환·B2·C 삽입: 메모가 고쳤다고 적지 않는다
            bk = HwpxBook(make_book(os.path.join(td, "b.hwpx"))); sec = bk.section(bk.anchors(), 1)
            ed = Editor(bk, memo="notice"); ed.apply(collate(hwpx_cells(bk.section_texts(sec), sec.start), ocr_cells([(7, "\n".join(ocr))]), policy="memo"))
            x = zipfile.ZipFile(ed.save_copy(os.path.join(td, "m.hwpx"))).read("Contents/section0.xml").decode()
            self.assertIn("[제안] 「有」 → 「無」", x)
            for claim in ("[자동 교정]", "[자동 교정·확인]", "[자동 삽입·확인]"):
                self.assertNotIn(claim, x)
            self.assertEqual(bk.texts()[sec.start + 1].count("無"), 0)                         # 글자는 그대로

    def test_memo_count_only_counts_notes_really_added(self):
        if not zh.available():
            self.skipTest("opencc 없음")
        from argparse import Namespace
        from types import SimpleNamespace
        from lxml import etree
        from yeone_ocr import cli
        from yeone_ocr.collate import run as R
        from yeone_ocr.collate.collate import Item
        from yeone_ocr.schema import PageReading
        ocr = Collate._ocr(Collate("test_classification"))
        with tempfile.TemporaryDirectory() as td:
            # 메모 수: 표기를 실제로 단 항목만(범위 안에 글 조각이 없어 표기를 못 단 고지는 세지 않음)
            src = make_book(os.path.join(td, "c.hwpx")); out = os.path.join(td, "c_교정.hwpx")
            bk = HwpxBook(src); an = bk.anchors(); pi = bk.section(an, 1).start + 1; L = len(bk.texts()[pi])
            etree.SubElement(bk.paragraphs[pi].element.find(f"{self.HP}run").find(f"{self.HP}t"), f"{self.HP}tab"); bk._texts = None   # 문단 끝 탭(글 조각 아님)
            fake = [Item("B1", "mark", pi, L, L + 1, "\t", "", "", notice=True), Item("B1", "mark", pi, 4, 5, "本", "夲", "", notice=True)]
            rec = {"merged": PageReading.model_validate({"pdf_page": 7, "regions": [{"id": "r1", "kind": "본문", "bbox": [0, 0, 1000, 1000],
                                                                                    "paras": [{"cols": [{"n": 1, "t": t}]} for t in ocr]}]}).model_dump()}
            work = SimpleNamespace(dir=Path(td), load_record=lambda n: rec, textlayer_path=lambda n: Path(td, "없음.json"))
            args = Namespace(pdf_pages="7", apply=True, memo="inline", policy="memo", out=out, no_glyph_check=True)
            logs = []; old = R.log, R.ensure_pages_read, cli.resolve_mode, R.collate
            try:
                R.log = logs.append; R.ensure_pages_read = lambda *a, **k: None; cli.resolve_mode = lambda *a, **k: None; R.collate = lambda *a, **k: fake
                R.run_section(args, pdf_doc=[None] * 7, work=work, book=bk, anchors=an, pdf_anchors=[], matches=[SimpleNamespace(pdf=None, how="none")] * len(an), idx=1)
            finally:
                R.log, R.ensure_pages_read, cli.resolve_mode, R.collate = old
            self.assertEqual(HwpxBook(out).texts()[pi].count("〔"), 1)
            self.assertIn("메모 1건(글 속 〔〕)", next(l for l in logs if l.startswith("표시:")))

    def test_work_note_paragraph_is_not_compared(self):
        """일반 OCR 의 〔판독 메모〕·〔작업 메모〕 문단은 메모 글이 〕 뒤에 온다 — 문단 끝까지 대조하지 않는다(한글 풀이 B1 로 잡지 않게)."""
        from yeone_ocr.collate.collate import note_spans
        for t in ("〔판독 메모〕 둘째 줄 끝이 흐림", "〔작업 메모〕 아래 본문은 기존 PDF 텍스트 레이어를 옮긴 것입니다."):
            self.assertEqual(note_spans(t), [(0, len(t))]); self.assertEqual(hwpx_cells([t], 0), [])
        self.assertEqual(note_spans("甲〔확인〕乙〔판독 메모〕 흐림"), [(1, 5), (6, 16)])
        if not zh.available():
            return
        from yeone_ocr.blocks import Block
        from yeone_ocr.grades import LEGEND
        from yeone_ocr.hwpx_builder import build_hwpx
        with tempfile.TemporaryDirectory() as td:
            src = os.path.join(td, "g.hwpx")
            build_hwpx([Block(type="meta", text="[book.pdf / PDF 3쪽]", memos=[(0, LEGEND)]), Block(type="para", kind="본문", text="大黃日一兩又能入肝經主之"),
                        Block(type="note", text="〔판독 메모〕 둘째 줄 끝이 흐림"), Block(type="para", kind="본문", text="陶云有毒血熱所致也")], src, memos=True)
            t = HwpxBook(src).texts(); lo = next(i for i, x in enumerate(t) if "大黃" in x); hi = next(i for i, x in enumerate(t) if "陶云" in x) + 1
            self.assertIn("〔판독 메모〕 둘째 줄 끝이 흐림", t[lo:hi])
            self.assertEqual(collate(hwpx_cells(t[lo:hi], lo), ocr_cells([(3, "大黄日一两又能入肝经主之\n陶云有毒血热所致也")]), policy="all"), [])

    def test_inline_unapplied_suggestions_read_as_suggestions(self):
        """정책 '메모만'의 반영하지 않은 B2·C 삽입: 글 속 표기가 고친 것처럼(〔원: …〕·〔삽입·확인〕) 읽히지 않고, 다시 돌려도 대조하지 않는 표기."""
        if not zh.available():
            self.skipTest("opencc 없음")
        from yeone_ocr.collate.collate import INLINE_NOTE_PREFIXES, note_spans
        ocr = Collate._ocr(Collate("test_classification"))
        with tempfile.TemporaryDirectory() as td:
            bk = HwpxBook(make_book(os.path.join(td, "b.hwpx"))); sec = bk.section(bk.anchors(), 1)
            items = collate(hwpx_cells(bk.section_texts(sec), sec.start), ocr_cells([(7, "\n".join(ocr))]), policy="memo")
            self.assertEqual(sorted((it.kind, it.action) for it in items if it.kind in ("B2", "C")), [("B2", "insert"), ("C", "insert")])
            Editor(bk, memo="inline").apply(items)
            t = bk.texts()[sec.start + 2]
            self.assertIn("初起之〔삽입 제안: 發 · 후보 發/髮〕血症", t)
            self.assertRegex(t, r"〔삽입 제안: 빠진 구간 \d+자〕")
            self.assertNotIn("〔원: ", t); self.assertNotIn("〔삽입·확인〕", t)
            notes = re.findall(r"〔[^〔〕]*〕", t)
            self.assertTrue(notes and all(n.startswith(INLINE_NOTE_PREFIXES) for n in notes), notes)
            self.assertEqual(sum(b - a for a, b in note_spans(t)), sum(len(n) for n in notes))       # 다음 실행에서 대조하지 않음
