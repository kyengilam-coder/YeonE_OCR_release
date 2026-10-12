"""입력 원본·캐시·페이지 표시 및 빈 2차 응답의 회귀 검사(API 호출 없음)."""
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from types import SimpleNamespace
from unittest.mock import patch

import pymupdf
from PIL import Image
from yeone_ocr import cli
from yeone_ocr.pdfio import page_image, page_frame, open_pdf
from yeone_ocr.schema import PageReading, Region, Paragraph, Column, RegionReread
from yeone_ocr.state import Work


def png(im):
    buf = io.BytesIO(); im.save(buf, "PNG"); return buf.getvalue()


def solid_pdf(path, color):
    with pymupdf.open() as doc:
        p = doc.new_page(width=40, height=40)
        p.insert_image(p.rect, stream=png(Image.new("RGB", (40, 40), color)))
        doc.save(path)


class InputIdentity(unittest.TestCase):
    def test_changed_source_archives_images_records_and_output(self):
        with tempfile.TemporaryDirectory() as td:
            src = Path(td, "book.pdf"); wd = Path(td, "work")
            solid_pdf(src, "red"); w = Work(str(src), str(wd))
            with open_pdf(str(src)) as doc:
                cli._prepare_page(w, doc, 1, 72)
            w.save_record(1, {"pdf_page": 1, "marker": "original"})
            (w.out_dir / "old.hwpx").write_bytes(b"previous output")
            solid_pdf(src, "blue")
            with contextlib.redirect_stdout(io.StringIO()):
                fresh = Work(str(src), str(wd)); fresh.ensure_source()
            self.assertIsNone(fresh.load_record(1))
            self.assertFalse(fresh.page_image_path(1).exists())
            archive = next((wd / "archives").iterdir())
            self.assertEqual(json.loads((archive / "readings/p001.json").read_text())["marker"], "original")
            self.assertEqual((archive / "out/old.hwpx").read_bytes(), b"previous output")
            with open_pdf(str(src)) as doc:
                cli._prepare_page(fresh, doc, 1, 72)
            with Image.open(fresh.page_image_path(1)) as im:
                self.assertEqual(im.getpixel((0, 0)), (0, 0, 255))

    def test_same_source_retains_results_and_dpi_change_invalidates(self):
        with tempfile.TemporaryDirectory() as td:
            src = Path(td, "book.pdf"); solid_pdf(src, "white")
            w = Work(str(src), str(Path(td, "w"))); w.ensure_source(72)
            w.save_record(1, {"pdf_page": 1, "marker": "keep"})
            fresh = Work(str(src), str(w.dir)); fresh.ensure_source(72)
            self.assertEqual(fresh.load_record(1)["marker"], "keep")
            with contextlib.redirect_stdout(io.StringIO()):
                fresh.ensure_source(144)
            self.assertIsNone(fresh.load_record(1))
            self.assertTrue(list((w.dir / "archives").glob("*/readings/p001.json")))

    def test_unverified_legacy_results_preserved_but_not_reused(self):
        with tempfile.TemporaryDirectory() as td:
            src = Path(td, "a.pdf"); solid_pdf(src, "white")
            wd = Path(td, "old"); (wd / "readings").mkdir(parents=True)
            (wd / "readings/p001.json").write_text('{"pdf_page":1}', encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()):
                w = Work(str(src), str(wd))
                self.assertTrue(w.load_record(1))  # 조회만으로는 자료를 옮기지 않음
                w.ensure_source()
            self.assertIsNone(w.load_record(1))
            self.assertTrue(list((wd / "archives").glob("*/readings/p001.json")))

    def test_same_basename_different_inputs_do_not_reuse_record(self):
        with tempfile.TemporaryDirectory() as td:
            a = Path(td, "a"); b = Path(td, "b"); a.mkdir(); b.mkdir()
            solid_pdf(a / "book.pdf", "red"); solid_pdf(b / "book.pdf", "blue")
            w = Work(str(a / "book.pdf"), str(Path(td, "shared")))
            w.save_record(1, {"pdf_page": 1})
            with contextlib.redirect_stdout(io.StringIO()):
                newer = Work(str(b / "book.pdf"), str(w.dir)); newer.ensure_source()
            self.assertIsNone(newer.load_record(1))

    def test_image_memory_cache_checks_content_even_with_same_stat(self):
        with tempfile.TemporaryDirectory() as td:
            src = Path(td, "a.bmp")
            Image.new("RGB", (20, 20), "red").save(src)
            old_stat = src.stat()
            with open_pdf(str(src)) as doc:
                self.assertEqual(page_image(doc, 1)[0].getpixel((0, 0)), (255, 0, 0))
            Image.new("RGB", (20, 20), "blue").save(src)
            os.utime(src, ns=(old_stat.st_atime_ns, old_stat.st_mtime_ns))
            self.assertEqual(src.stat().st_size, old_stat.st_size)
            with open_pdf(str(src)) as doc:
                self.assertEqual(page_image(doc, 1)[0].getpixel((0, 0)), (0, 0, 255))


class PageAppearance(unittest.TestCase):
    def test_cropbox_excludes_hidden_neighbor(self):
        im = Image.new("RGB", (200, 100), "white"); im.paste("black", (100, 0, 200, 100))
        with pymupdf.open() as doc:
            p = doc.new_page(width=200, height=100)
            p.insert_image(p.rect, stream=png(im)); p.set_cropbox(pymupdf.Rect(0, 0, 100, 100))
            got, desc = page_image(doc, 1, dpi=72)
            self.assertEqual(got.size, (100, 100)); self.assertEqual(got.getextrema(), ((255, 255),) * 3)
            self.assertIn("렌더", desc); self.assertEqual(page_frame(doc, 1), (0, 0, 100, 100))

    def test_soft_mask_is_composited(self):
        im = Image.new("RGBA", (100, 100), (0, 0, 0, 0)); im.paste((0, 0, 0, 255), (40, 40, 60, 60))
        with pymupdf.open() as doc:
            p = doc.new_page(width=100, height=100); p.insert_image(p.rect, stream=png(im))
            got, desc = page_image(doc, 1, dpi=72)
            self.assertEqual(got.getpixel((0, 0)), (255, 255, 255))
            self.assertEqual(got.getpixel((50, 50)), (0, 0, 0)); self.assertIn("렌더", desc)

    def test_visible_text_overlay_is_not_lost(self):
        with pymupdf.open() as doc:
            p = doc.new_page(width=100, height=100)
            p.insert_image(p.rect, stream=png(Image.new("RGB", (100, 100), "white")))
            p.insert_text((10, 40), "Visible", fontsize=18)
            got, desc = page_image(doc, 1, dpi=72)
            self.assertIn("렌더", desc); self.assertLess(got.convert("L").getextrema()[0], 50)

    def test_hidden_text_layer_keeps_lossless_fast_path(self):
        with pymupdf.open() as doc:
            p = doc.new_page(width=100, height=100)
            p.insert_image(p.rect, stream=png(Image.new("RGB", (100, 100), "white")))
            p.insert_text((10, 40), "OCR", render_mode=3)
            got, desc = page_image(doc, 1, dpi=72)
            self.assertIn("내장 스캔", desc); self.assertEqual(got.size, (100, 100))


class ReadCompletion(unittest.TestCase):
    def tearDown(self):
        cli.CANCEL.clear()

    def test_empty_second_response_is_retried_and_keeps_first_read(self):
        class Reader:
            name = model = "fake"
            n_429 = 0
            first = second = 0
            def read_page(self, *args):
                self.first += 1
                return PageReading(pdf_page=1, regions=[Region(id="r", kind="본문", paragraphs=[Paragraph(columns=[Column(n=1, t="天地")])])])
            def reread_region(self, *args):
                self.second += 1
                return RegionReread(columns=[])
        with tempfile.TemporaryDirectory() as td:
            src = Path(td, "book.pdf"); solid_pdf(src, "white"); wd = Path(td, "w")
            argv = ["read", str(src), "--work", str(wd), "--reader", "mock", "--mode", "vertical", "--workers", "1", "--dpi", "72"]
            reader = Reader()
            with patch.object(cli, "make_reader", return_value=reader), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(cli.main(argv), 3)
                self.assertEqual(cli.main(argv), 3)
            self.assertEqual((reader.first, reader.second), (1, 2))
            self.assertTrue((wd / "readings/partial/p001.json").exists())
            self.assertFalse((wd / "readings/p001.json").exists())

    def test_completed_read_does_not_create_reader(self):
        with tempfile.TemporaryDirectory() as td:
            src = Path(td, "book.pdf"); solid_pdf(src, "white")
            w = Work(str(src), str(Path(td, "w"))); w.ensure_source(300)
            w.save_record(1, {"pdf_page": 1, "pass1": {}})
            with patch.object(cli, "make_reader", side_effect=AssertionError("unexpected reader")), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(cli.main(["read", str(src), "--work", str(w.dir)]), 0)

    def test_empty_batch_text_gets_single_retry_then_failure(self):
        class Reader:
            def reread_regions(self, batch, page):
                return {rid: RegionReread(columns=[Column(n=1, t="  ")]) for rid, _, _ in batch}
            def reread_region(self, *args):
                return RegionReread()
        p1 = PageReading(pdf_page=1, regions=[Region(id=rid, kind="본문", bbox=box)
            for rid, box in (("a", [0, 0, 400, 1000]), ("b", [600, 0, 1000, 1000]))])
        with contextlib.redirect_stdout(io.StringIO()):
            rr, meta = cli._second_pass(Reader(), Image.new("RGB", (100, 100), "white"), p1, 1,
                                       SimpleNamespace(chunk_lines=0, second_mode="batch", batch_crops=8))
        self.assertFalse(rr)
        self.assertTrue(all("빈 2차" in meta[rid]["pass2_failed"] for rid in ("a", "b")))

    def test_owned_pdf_is_closed_even_when_command_raises(self):
        with tempfile.TemporaryDirectory() as td:
            src = Path(td, "book.pdf"); solid_pdf(src, "white")
            real_open = cli.open_pdf; opened = []
            def track(path):
                doc = real_open(path); opened.append(doc); return doc
            with patch.object(cli, "open_pdf", side_effect=track), patch.object(cli, "text_layer", side_effect=RuntimeError("test")), contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(RuntimeError):
                    cli.main(["inspect", str(src)])
            self.assertTrue(opened[0].is_closed)


class CacheConcurrency(unittest.TestCase):
    def test_prepare_waits_for_same_page_open_image_to_close(self):
        """읽고 있는 PNG를 준비 스레드가 교체하면 Windows에서 쪽 판독이 실패한다."""
        with tempfile.TemporaryDirectory() as td:
            src = Path(td, "book.pdf"); solid_pdf(src, "white")
            w = Work(str(src), str(Path(td, "w")))
            with open_pdf(str(src)) as doc:
                cli._prepare_page(w, doc, 1, 72)
                opened = threading.Event(); release = threading.Event(); prepare_started = threading.Event()
                real_open = cli.Image.open
                def slow_open(*args, **kwargs):
                    im = real_open(*args, **kwargs)
                    real_load = im.load
                    def slow_load(*a, **k):
                        opened.set()
                        if not release.wait(5):
                            raise RuntimeError("test reader release timed out")
                        return real_load(*a, **k)
                    im.load = slow_load
                    return im
                def prepare_again():
                    prepare_started.set()
                    cli._prepare_page(w, doc, 1, 72, force=True)
                with patch.object(cli.Image, "open", side_effect=slow_open), ThreadPoolExecutor(2) as pool:
                    reader = pool.submit(cli._load_page_image, w, doc, 1, 72)
                    try:
                        self.assertTrue(opened.wait(5))
                        writer = pool.submit(prepare_again)
                        self.assertTrue(prepare_started.wait(5))
                        with self.assertRaises(TimeoutError):
                            writer.result(timeout=0.1)
                    finally:
                        release.set()
                    img, _ = reader.result(timeout=5); img.close()
                    writer.result(timeout=5)
                with Image.open(w.page_image_path(1)) as im:
                    im.verify()


if __name__ == "__main__":
    unittest.main()
