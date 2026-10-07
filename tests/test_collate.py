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


def make_book(path: str):
    """개요 스타일 제목 + 본문으로 된 작은 HWPX(번체)."""
    from hwpx.document import HwpxDocument
    doc = HwpxDocument.new()
    body = doc.ensure_run_style(font="함초롬바탕", size=11)
    def heading(text, level):
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


class Outline(unittest.TestCase):
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
