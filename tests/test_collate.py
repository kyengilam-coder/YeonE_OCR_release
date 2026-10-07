"""책갈피 교정(collate) 시험: 색인·짝짓기·대조 분류·서식 보존 편집."""
import os, re, sys, tempfile, unittest, zipfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from yeone_ocr.collate import zh
from yeone_ocr.collate.sections import HwpxBook, Anchor, find_anchor, normalize_name, heading_styles
from yeone_ocr.collate.pdf_outline import PdfAnchor, match_anchors, page_range
from yeone_ocr.collate.collate import hwpx_cells, ocr_cells, collate, summary, strip_marks
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
        # 幹(→干) vs 판독 于: 자형 혼동 + 번체 후보 여럿 → 자동 반영하지 않고 표시만
        items = collate(hwpx_cells(["苦瓠之肉能幹"], 0), ocr_cells([(1, "苦瓠之肉能于")]), policy="all")
        self.assertEqual([(it.kind, it.action, it.auto) for it in items], [("B1", "mark", False)]); self.assertIn("헷갈리기", items[0].note)
        self.assertIn("「幹」", items[0].hw_ctx); self.assertIn("「于」", items[0].oc_ctx)
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
            self.assertEqual(x.count('type="MEMO"'), 4); self.assertEqual(x.count("<hp:fieldEnd"), 4)
            self.assertIn('textColor="#1F4FBF"', h); self.assertIn('textColor="#C00000"', h); self.assertIn('shadeColor="#D8F5C8"', h)
            self.assertIn("[자동 교정] 원래 「有」 → 「無」", x); self.assertIn("여러 번체로 갈라짐: 發/髮", x)
            # 다른 구획은 그대로
            self.assertEqual(t[sec.end], "小薊"); self.assertEqual(t[-1], "人蔘, 大補元氣.")
            # 메모 없이(〔〕 표기)도 된다
            bk3 = HwpxBook(make_book(os.path.join(td, "c.hwpx"))); ed3 = Editor(bk3, memo=False)
            ed3.apply(collate(hwpx_cells(bk3.section_texts(bk3.section(bk3.anchors(), 1)), sec.start), ocr_cells([(7, "\n".join(ocr))])))
            self.assertIn("無〔원: 有〕", bk3.texts()[sec.start + 1])


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
