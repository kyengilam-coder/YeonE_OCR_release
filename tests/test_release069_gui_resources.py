"""GUI 비용 어림(costplan)·색인의 소유 PDF/HWPX 핸들은 성공·오류 모두에서 닫힌다(Tk 창·모델 호출 없음)."""
import queue
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pymupdf
from yeone_ocr import gui
from yeone_ocr.schema import Column, PageReading, Paragraph, Region


def var(value):
    return SimpleNamespace(get=lambda: value, set=Mock())


class ImmediateThread:
    def __init__(self, target, **kw):
        self.target = target

    def start(self):
        self.target()


class GuiResources(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.pdf = self.root / "book.pdf"
        with pymupdf.open() as doc:
            doc.new_page()
            doc.save(self.pdf)
        self.docs = []
        self.addCleanup(self.close_leftovers)

    def close_leftovers(self):
        for doc in self.docs:
            if not doc.is_closed:
                doc.close()

    def open_pdf(self, path):
        doc = pymupdf.open(path)
        self.docs.append(doc)                    # 강한 참조를 남겨 GC 가 누수를 숨기지 못하게 한다.
        return doc

    def assert_closed(self):
        self.assertTrue(self.docs)
        self.assertTrue(all(doc.is_closed for doc in self.docs))

    def test_cost_plan_closes_pdf_on_success_and_bad_page_spec(self):
        from yeone_ocr.costplan import PlanInput, count_plan
        inp = PlanInput(kind="read", pdf=str(self.pdf), work_dir=str(self.root / "result"), pages="1", reader="gemini", model="x")
        with patch("yeone_ocr.pdfio.open_pdf", side_effect=self.open_pdf):
            self.assertEqual(count_plan(inp)["errors"], [])
            self.assert_closed()
            inp.pages = "invalid"
            self.assertEqual(count_plan(inp)["errors"][0]["step"], "plan")                    # 쪽 범위 오류도 닫고 '전체 실패'로 알린다
            self.assert_closed()
        self.assertFalse((self.root / "result").exists())

    def test_style_previews_close_hwpx_even_when_reading_fails(self):
        source = self.root / "book.hwpx"
        source.write_bytes(b"mock book")
        app = SimpleNamespace(v_hwpx=var(str(source)), v_hstyles=var(""), q=queue.Queue())
        for method, read_method in ((gui.App.suggest_styles, "heading_style_names"), (gui.App.pick_styles, "style_counts")):
            for fail in (False, True):
                with self.subTest(method=method.__name__, fail=fail):
                    doc = SimpleNamespace(close=Mock())
                    read = Mock(side_effect=RuntimeError("read failed") if fail else None, return_value=[])
                    book = SimpleNamespace(doc=doc, **{read_method: read})
                    with patch("yeone_ocr.collate.sections.HwpxBook", return_value=book), patch.object(gui.threading, "Thread", ImmediateThread), \
                         patch.object(gui, "StylePicker"), patch.object(gui.messagebox, "showerror"):
                        method(app)
                    doc.close.assert_called_once()

    def test_index_closes_both_documents_on_success_or_error(self):
        app = SimpleNamespace(_need_hwpx=lambda: True, pdf2=lambda: str(self.pdf), v_hwpx=var("book.hwpx"), anchor_source=lambda: "auto",
                              heading_styles_list=lambda: [], v_hstyles=var(""), cfg={}, v_cstat=var(""), q=queue.Queue())
        for fail in (False, True):
            with self.subTest(fail=fail):
                book_doc = SimpleNamespace(close=Mock())
                with patch("yeone_ocr.pdfio.open_pdf", side_effect=self.open_pdf), \
                     patch("yeone_ocr.collate.sections.HwpxBook", return_value=SimpleNamespace(doc=book_doc)), \
                     patch("yeone_ocr.collate.run.index_both", side_effect=RuntimeError("index failed") if fail else None, return_value=([], [], [])), \
                     patch.object(gui.threading, "Thread", ImmediateThread), patch.object(gui, "save_config"):
                    gui.App.do_collate_index(app)
                self.assert_closed()
                book_doc.close.assert_called_once()

    def test_index_closes_pdf_when_book_constructor_fails(self):
        app = SimpleNamespace(_need_hwpx=lambda: True, pdf2=lambda: str(self.pdf), v_hwpx=var("book.hwpx"), anchor_source=lambda: "auto",
                              heading_styles_list=lambda: [], v_hstyles=var(""), cfg={}, v_cstat=var(""), q=queue.Queue())
        with patch("yeone_ocr.pdfio.open_pdf", side_effect=self.open_pdf), \
             patch("yeone_ocr.collate.sections.HwpxBook", side_effect=RuntimeError("invalid HWPX")), \
             patch.object(gui.threading, "Thread", ImmediateThread), patch.object(gui, "save_config"):
            gui.App.do_collate_index(app)
        self.assert_closed()

    def test_missing_bookmark_title_search_closes_before_user_prompt(self):
        app = SimpleNamespace(_need_hwpx=lambda: True, _prepare_env=lambda: True, _selected_anchor=lambda: 1, pdf2=lambda: str(self.pdf),
                              collate_rows=[{"no": 1, "lv": 1, "name": "天地", "pdf": ""}])
        for fail in (False, True):
            with self.subTest(fail=fail):
                def prompt(*args, **kw):
                    self.assert_closed()
                    return None
                with patch("yeone_ocr.pdfio.open_pdf", side_effect=self.open_pdf), \
                     patch("yeone_ocr.collate.pdf_outline.find_title_pages", side_effect=RuntimeError("lookup failed") if fail else None, return_value=(1, 1)), \
                     patch.object(gui.simpledialog, "askstring", side_effect=prompt):
                    gui.App.do_collate(app, False)

    def test_adjudication_plan_closes_pdf_and_never_writes_the_layer(self):
        """3차 판별 비용 어림: 합의 의심 셈은 PDF 가 열린 동안 하고(글자층 파일을 쓰는 cli._load_layer 는 부르지 않음), 끝나면(기록이 깨져도) PDF 를 닫는다."""
        from yeone_ocr.costplan import PlanInput, count_plan
        page = PageReading(pdf_page=1, regions=[Region(id="r1", kind="본문", paragraphs=[Paragraph(columns=[Column(n=1, t="甲〔入/人〕乙")])])])
        rec = {"pdf_page": 1, "merged": page.model_dump()}
        inp = PlanInput(kind="adjudicate", pdf=str(self.pdf), work_dir=str(self.root / "result"), pages="1", reader="gemini")
        for bad_record in (False, True):
            with self.subTest(bad_record=bad_record):
                work = SimpleNamespace(dir=self.root, out_dir=self.root / "out", source_matches=lambda dpi=None: True,
                                       textlayer_path=lambda n: self.root / "없음.json", records=lambda pages, repair=True: [{"merged": {}}] if bad_record else [rec])
                def pending(rec, layer, *a):
                    self.assertTrue(self.docs and not self.docs[-1].is_closed)
                    return 0
                with patch("yeone_ocr.pdfio.open_pdf", side_effect=self.open_pdf), patch("yeone_ocr.state.Work", return_value=work), \
                     patch("yeone_ocr.cli._load_layer", side_effect=AssertionError("글자층 파일을 쓰면 안 됨")), \
                     patch("yeone_ocr.adjudicate.suspects_pending", side_effect=pending), patch("yeone_ocr.adjudicate.suspect_freq", return_value={}):
                    plan = count_plan(inp)
                self.assert_closed()
                self.assertEqual([e["step"] for e in plan["errors"]], ["glyph", "suspect"] if bad_record else [])


if __name__ == "__main__":
    unittest.main()
