"""GUI 미리보기·색인의 소유 PDF/HWPX 핸들은 성공·오류 모두에서 닫힌다(Tk 창·모델 호출 없음)."""
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

    def test_estimate_closes_pdf_on_success_and_bad_page_spec(self):
        app = SimpleNamespace(pdf=lambda: str(self.pdf), work=lambda: self.root / "result", cfg={}, fx_setting=lambda: (None, ""),
                              v_pages=var("1"), v_up=var("2"), v_second=var(True), v_model=var("x"), v_model2=var("auto"),
                              v_cache=var(True), v_force=var(False))
        with patch("yeone_ocr.pdfio.open_pdf", side_effect=self.open_pdf), patch("yeone_ocr.estimate.estimate", return_value={"ok": True}):
            self.assertEqual(gui.App.estimate_data(app), {"ok": True})
            self.assert_closed()
            app.v_pages = var("invalid")
            with self.assertRaises(ValueError):
                gui.App.estimate_data(app)
            self.assert_closed()

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

    def test_adjudication_preview_closes_pdf_before_confirming_or_reporting_error(self):
        page = PageReading(pdf_page=1, regions=[Region(id="r1", kind="본문", paragraphs=[Paragraph(columns=[Column(n=1, t="甲〔入/人〕乙")])])])
        rec = {"pdf_page": 1, "merged": page.model_dump()}
        app = SimpleNamespace(_need_records=lambda _: True, _prepare_env=lambda: True, v_reader=var("gemini"), v_suspects=var(True),
                              v_pages=var("1"), v_blind=var(False), pdf=lambda: str(self.pdf), work=lambda: self.root / "result")
        for bad_record in (False, True):
            with self.subTest(bad_record=bad_record):
                work = SimpleNamespace(dir=self.root, records=lambda _: [{"merged": {}}] if bad_record else [rec])
                def prompt(*args, **kw):
                    self.assert_closed()
                    return False
                def layer(work, doc, page):
                    self.assertFalse(doc.is_closed)
                    return {"plain": "甲人乙"}
                with patch("yeone_ocr.pdfio.open_pdf", side_effect=self.open_pdf), patch("yeone_ocr.state.Work", return_value=work), \
                     patch("yeone_ocr.cli._load_layer", side_effect=layer), patch("yeone_ocr.adjudicate.suspects_pending", return_value=0), \
                     patch("yeone_ocr.adjudicate.suspect_freq", return_value={}), patch.object(gui.messagebox, "askyesno", side_effect=prompt), \
                     patch.object(gui.messagebox, "showerror", side_effect=prompt):
                    gui.App.do_adjudicate(app)
                self.assert_closed()


if __name__ == "__main__":
    unittest.main()
