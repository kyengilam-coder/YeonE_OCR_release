"""Repeated per-page work is reused without weakening source-change checks."""
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

from ab_ocr import ocr
from yeone_ocr.local_first import _Checkpoint


class StableHash(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / "source.bin"
        ocr._STABLE_HASHES.clear()
        patcher = mock.patch.object(ocr.time, "time_ns", return_value=time.time_ns())
        self.clock = patcher.start()
        self.addCleanup(patcher.stop)

    def settle(self, seconds=10):
        # ctime cannot be backdated, so move the clock the check reads instead.
        self.clock.return_value = time.time_ns() + seconds * 1_000_000_000

    def test_settled_file_is_read_once(self):
        self.path.write_bytes(b"abc")
        self.settle()
        with mock.patch.object(ocr, "file_sha256", wraps=ocr.file_sha256) as real:
            first, second = ocr.stable_file_sha256(self.path), ocr.stable_file_sha256(self.path)
        self.assertEqual(first, second)
        self.assertEqual(first, ocr.file_sha256(self.path))
        self.assertEqual(real.call_count, 1)

    def test_recently_written_file_is_always_read(self):
        self.path.write_bytes(b"abc")
        with mock.patch.object(ocr, "file_sha256", wraps=ocr.file_sha256) as real:
            ocr.stable_file_sha256(self.path)
            ocr.stable_file_sha256(self.path)
        self.assertEqual(real.call_count, 2)

    def test_same_size_rewrite_is_detected(self):
        self.path.write_bytes(b"abc")
        self.settle()
        before = ocr.stable_file_sha256(self.path)
        self.path.write_bytes(b"abd")
        self.assertNotEqual(ocr.stable_file_sha256(self.path), before)
        self.settle(seconds=20)
        self.assertEqual(ocr.stable_file_sha256(self.path), ocr.file_sha256(self.path))


class RenderCache(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.source = Path(self.folder.name) / "page.png"
        Image.new("RGB", (40, 30), "white").save(self.source)
        ocr._STABLE_HASHES.clear()
        patcher = mock.patch.object(ocr.time, "time_ns", return_value=time.time_ns() + 10_000_000_000)
        patcher.start()
        self.addCleanup(patcher.stop)

    def render(self, name, details=None):
        target = Path(self.folder.name) / name
        return target, ocr._render(self.source, target, 1, 300, details=details)

    def test_identical_bytes_and_details_with_one_real_render(self):
        plain_details = {}
        plain, size = self.render("plain.png", plain_details)
        with mock.patch.object(ocr, "_render_uncached", wraps=ocr._render_uncached) as real:
            with ocr.render_cache():
                first_details, second_details = {}, {}
                first, first_size = self.render("first.png", first_details)
                second, second_size = self.render("second.png", second_details)
                with ocr.render_page(self.source, 1, 300) as image:
                    self.assertEqual(image.size, (40, 30))
            self.assertEqual(real.call_count, 1)
        self.assertEqual(plain.read_bytes(), first.read_bytes())
        self.assertEqual(first.read_bytes(), second.read_bytes())
        self.assertEqual((size, plain_details), (first_size, first_details))
        self.assertEqual((size, plain_details), (second_size, second_details))
        self.assertEqual(ocr._RENDER_CACHE["depth"], 0)
        self.assertIsNone(ocr._RENDER_CACHE["folder"])

    def test_changed_source_is_rendered_again(self):
        with ocr.render_cache():
            _, size = self.render("first.png")
            Image.new("RGB", (50, 30), "white").save(self.source)
            _, changed = self.render("second.png")
        self.assertEqual((size, changed), ((40, 30), (50, 30)))

    def test_no_cache_outside_the_block(self):
        with mock.patch.object(ocr, "_render_uncached", wraps=ocr._render_uncached) as real:
            self.render("a.png")
            self.render("b.png")
        self.assertEqual(real.call_count, 2)


class Checkpoints(unittest.TestCase):
    def test_slow_saves_are_spaced_and_page_end_always_saves(self):
        now = [0.0]
        project = mock.Mock()
        point = _Checkpoint(project, clock=lambda: now[0])
        point.maybe()                      # first result: always
        point.maybe()                      # cheap saves stay immediate
        self.assertEqual(project.save.call_count, 2)
        project.save.side_effect = lambda: now.__setitem__(0, now[0] + 2.0)
        point.maybe()                      # this one turns out slow
        point.maybe()                      # skipped: too soon after a slow save
        self.assertEqual(project.save.call_count, 3)
        point.save()                       # page end is unconditional
        self.assertEqual(project.save.call_count, 4)
        now[0] += 31
        point.maybe()
        self.assertEqual(project.save.call_count, 5)


if __name__ == "__main__":
    unittest.main()
