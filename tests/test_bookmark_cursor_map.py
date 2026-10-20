"""Read-only PDF cursor links: geometry, disambiguation and edited positions."""
from pathlib import Path
from types import SimpleNamespace
import json
import tempfile
import unittest
from unittest.mock import patch

import pymupdf
from PIL import Image

from yeone_ocr.collate.cursor_map import (
    _trusted_equal_map, align_cursor_map, build_cursor_map, read_cursor_pages,
)


def paragraph(index, text, original=None):
    return SimpleNamespace(index=index, corrected_text=text,
                           original_text=text if original is None else original)


class CursorMap(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / "book.pdf"

    def pdf(self, pages, rotation=0):
        with pymupdf.open() as document:
            for lines in pages:
                page = document.new_page(width=300, height=400)
                for n, line in enumerate(lines):
                    page.insert_text((30, 40 + n * 25), line, fontsize=10)
                page.set_rotation(rotation)
            document.save(self.path)
        return self.path.read_bytes()

    def test_coordinates_and_selected_pages_are_read_only(self):
        before = self.pdf([["Outside previous section."], ["A unique example for the selected paragraph."],
                           ["Outside following section."]])
        text = "A unique example for the selected paragraph."
        mapping = build_cursor_map(self.path, (2, 2), [paragraph(17, text)])
        location = mapping.locate(17, text, text.index("selected"))
        self.assertTrue(mapping.available)
        self.assertTrue(location.precise)
        self.assertEqual(location.page, 2)
        self.assertTrue(all(0 <= v <= 1 for b in location.boxes for v in b))
        self.assertTrue(all(0 <= v <= 1 for v in location.caret_box))
        self.assertLess(location.caret_box[0], location.caret_box[2])
        self.assertLess(location.caret_box[1], location.caret_box[3])
        self.assertEqual(self.path.read_bytes(), before)
        self.assertIsNone(mapping.locate(18, text, 0))
        with patch("pymupdf.open", side_effect=AssertionError("locate must not reopen PDF")):
            self.assertEqual(mapping.locate(17, text, 0).page, 2)

    def test_rotated_page_coordinates_follow_visible_page(self):
        text = "A unique rotation alignment sentence."
        for rotation in (90, 180, 270):
            with self.subTest(rotation=rotation):
                if self.path.exists():
                    self.path.unlink()
                before = self.pdf([[text]], rotation=rotation)
                mapping = build_cursor_map(self.path, (1, 1), [paragraph(1, text)])
                location = mapping.locate(1, text, 0)
                with pymupdf.open(self.path) as doc:
                    char = doc[0].get_text("rawdict")["blocks"][0]["lines"][0]["spans"][0]["chars"][0]
                    expected = pymupdf.Rect(char["bbox"]) * doc[0].rotation_matrix
                    expected = (expected.x0 / doc[0].rect.width, expected.y0 / doc[0].rect.height,
                                expected.x1 / doc[0].rect.width, expected.y1 / doc[0].rect.height)
                for actual, value in zip(location.caret_box, expected):
                    self.assertAlmostEqual(actual, value, places=4)
                self.assertEqual(self.path.read_bytes(), before)

    def test_pdf_snapshot_is_immutable_and_alignment_never_opens_pdf(self):
        text = "Snapshot alignment has an independently unique passage."
        before = self.pdf([[text]])
        snapshot = read_cursor_pages(self.path, (1, 1))
        self.assertIsInstance(hash(snapshot), int)
        with self.assertRaises(AttributeError):
            snapshot.key = "changed"
        with patch("pymupdf.open", side_effect=AssertionError("alignment must not reopen PDF")):
            mapping = align_cursor_map(snapshot, [paragraph(1, text)])
            self.assertEqual(mapping.locate(1, text, 12).page, 1)
            self.assertGreater(mapping.page_font_height(1), 0)
            self.assertGreater(mapping.page_font_width(1), 0)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertIsNone(mapping.page_font_height(2))
        self.assertIsNone(mapping.page_font_width(2))

    def test_font_dimensions_use_cjk_median_and_ignore_punctuation(self):
        self.pdf([[]])
        rows = [{"bbox": [0, 0, 280, 150],
                 "chars": [["A", [1, 1, 80, 120]], [".", [90, 1, 95, 140]],
                           ["甲", [10, 160, 20, 172]], ["乙", [30, 160, 42, 174]],
                           ["丙", [50, 160, 64, 176]], ["巨", [70, 160, 110, 220]]]}]
        with patch("yeone_ocr.pdfio.layer_chars", return_value=rows):
            snapshot = read_cursor_pages(self.path, (1, 1))
        mapping = align_cursor_map(snapshot, [])
        self.assertAlmostEqual(mapping.page_font_width(1), 13 / 300)
        self.assertAlmostEqual(mapping.page_font_height(1), 15 / 400)
        self.assertFalse(mapping.available)

    def test_font_dimensions_follow_rotated_display_axes(self):
        self.pdf([["A unique displayed font dimension test."]], rotation=90)
        with pymupdf.open(self.path) as document:
            from statistics import median
            chars = [char for block in document[0].get_text("rawdict")["blocks"] if block["type"] == 0
                     for line in block["lines"] for span in line["spans"]
                     for char in span["chars"] if char["c"].isalnum()]
            width = median(char["bbox"][3] - char["bbox"][1] for char in chars) / document[0].rect.width
            height = median(char["bbox"][2] - char["bbox"][0] for char in chars) / document[0].rect.height
        mapping = align_cursor_map(read_cursor_pages(self.path, (1, 1)), [])
        self.assertAlmostEqual(mapping.page_font_width(1), width, places=4)
        self.assertAlmostEqual(mapping.page_font_height(1), height, places=4)

    def test_unresolved_duplicate_does_not_pick_first_page(self):
        text = "Repeated paragraph with no unique surroundings."
        self.pdf([[text], [text]])
        mapping = build_cursor_map(self.path, (1, 2), [paragraph(1, text)])
        self.assertFalse(mapping.available)
        self.assertIsNone(mapping.locate(1, text, 15))

    def test_missing_repeated_hwpx_paragraph_is_not_assigned_arbitrarily(self):
        text = "Repeated paragraph with no unique surroundings."
        self.pdf([[text]])
        mapping = build_cursor_map(self.path, (1, 1), [paragraph(1, text), paragraph(2, text)])
        self.assertFalse(mapping.available)
        self.assertIsNone(mapping.locate(1, text, 15))
        self.assertIsNone(mapping.locate(2, text, 15))

    def test_ordered_neighbor_context_resolves_duplicate(self):
        repeated = "The same repeated sentence."
        first, second = "Alpha unique introduction.", "Omega unique introduction."
        self.pdf([[first, repeated], [second, repeated]])
        mapping = build_cursor_map(self.path, (1, 2), [
            paragraph(10, first), paragraph(11, repeated), paragraph(12, second), paragraph(13, repeated)])
        self.assertEqual(mapping.locate(11, repeated, 8).page, 1)
        self.assertEqual(mapping.locate(13, repeated, 8).page, 2)

    def test_image_only_empty_and_short_ambiguous_have_no_link(self):
        self.pdf([[]])
        mapping = build_cursor_map(self.path, (1, 1), [paragraph(1, "No readable PDF text here.")])
        self.assertFalse(mapping.available)
        self.assertIsNone(mapping.page_font_height(1))
        self.assertIsNone(mapping.page_font_width(1))
        self.assertIsNone(mapping.locate(1, "No readable PDF text here.", 2))
        self.assertIsNone(mapping.locate(1, "", 0))
        self.path.unlink()
        self.pdf([["title"]])
        mapping = build_cursor_map(self.path, (1, 1), [paragraph(1, "title")])
        self.assertFalse(mapping.available)

    def test_inserted_characters_show_context_without_precise_caret(self):
        text = "The first unique sentence and the second distinct ending."
        self.pdf([[text]])
        mapping = build_cursor_map(self.path, (1, 1), [paragraph(1, text)])
        insertion = text.index("and")
        current = text[:insertion] + "NEW " + text[insertion:]
        location = mapping.locate(1, current, insertion + 1)
        self.assertIsNotNone(location)
        self.assertFalse(location.precise)
        self.assertIsNone(location.caret_box)
        old = mapping.locate(1, text, text.index("distinct"))
        new = mapping.locate(1, current, current.index("distinct"))
        self.assertTrue(new.precise)
        self.assertEqual(new.caret_box, old.caret_box)

    def test_deleted_prefix_and_replacement_keep_surviving_positions(self):
        text = "Prefix words. The remaining unique paragraph has a long ending."
        self.pdf([[text]])
        mapping = build_cursor_map(self.path, (1, 1), [paragraph(1, text)])
        current = text.removeprefix("Prefix words. ")
        self.assertEqual(mapping.locate(1, current, current.index("unique")).caret_box,
                         mapping.locate(1, text, text.index("unique")).caret_box)
        current = text.replace("unique", "UNRECOGNIZED")
        location = mapping.locate(1, current, current.index("UNRECOGNIZED") + 3)
        self.assertIsNotNone(location)
        self.assertFalse(location.precise)
        current = "Completely rewritten contents with nothing in common."
        self.assertIsNone(mapping.locate(1, current, 20))

    def test_punctuation_and_end_cursor_are_consistent(self):
        text = "Distinct beginning, and a unique ending."
        self.pdf([[text]])
        mapping = build_cursor_map(self.path, (1, 1), [paragraph(1, text)])
        punctuation = mapping.locate(1, text, text.index(","))
        self.assertFalse(punctuation.precise)
        self.assertIsNone(punctuation.caret_box)
        end = mapping.locate(1, text.rstrip("."), len(text) - 1)
        self.assertTrue(end.precise)

    def test_original_text_can_anchor_corrected_ocr_mismatch(self):
        original = "Distinct beginning and unusual central word with a long ending."
        corrected = original.replace("unusual", "unusuql")
        self.pdf([[original]])
        mapping = build_cursor_map(self.path, (1, 1), [paragraph(1, corrected, original)])
        position = corrected.index("unusuql") + 5
        location = mapping.locate(1, corrected, position)
        self.assertIsNotNone(location)
        self.assertFalse(location.precise)
        restored = mapping.locate(1, original, position)
        self.assertTrue(restored.precise)

    def test_conflicting_original_and_corrected_locations_are_not_guessed(self):
        shared = "A common paragraph phrase that occurs on both pages"
        original = shared + " with original ending."
        corrected = shared + " with corrected ending."
        self.pdf([[original], [corrected]])
        mapping = build_cursor_map(self.path, (1, 2), [paragraph(1, corrected, original)])
        # Both long contexts are independently present. The shared glyph must
        # not silently prefer whichever version happened to be considered first.
        self.assertIsNone(mapping.locate(1, corrected, corrected.index("common")))

    def test_large_insert_only_links_near_its_known_edges(self):
        text = "A unique prefix sentence followed by a distinctive final phrase."
        self.pdf([[text]])
        mapping = build_cursor_map(self.path, (1, 1), [paragraph(1, text)])
        insert = text.index("followed")
        current = text[:insert] + "Z" * 100 + text[insert:]
        self.assertIsNone(mapping.locate(1, current, insert + 50))
        edge = mapping.locate(1, current, insert + 2)
        self.assertIsNotNone(edge)
        self.assertFalse(edge.precise)

    def test_variants_and_supplementary_unicode_keep_python_offsets(self):
        text = "瀉白散淸熱平喘止咳𠀀獨特原文位置"
        shown = "泻白散清热平喘止咳𠀀独特原文位置"
        self.pdf([[]])
        rows = [{"bbox": [10, 10, 270, 30],
                 "chars": [[char, [10 + i * 10, 10, 19 + i * 10, 30]] for i, char in enumerate(shown)]}]
        with patch("yeone_ocr.pdfio.layer_chars", return_value=rows):
            mapping = build_cursor_map(self.path, (1, 1), [paragraph(1, text)])
        target = mapping.locate(1, text, text.index("獨"))
        self.assertTrue(target.precise)
        self.assertAlmostEqual(target.caret_box[0], (10 + shown.index("独") * 10) / 300)
        current = "🦋 " + text
        target2 = mapping.locate(1, current, current.index("獨"))
        self.assertEqual(target2.caret_box, target.caret_box)

    def test_invalid_ranges_are_rejected(self):
        self.pdf([["Valid document."]])
        for pages in ((0, 1), (2, 1), (1, 2), (True, 1)):
            with self.assertRaises(ValueError):
                build_cursor_map(self.path, pages, [paragraph(1, "Valid document.")])
        with self.assertRaises(ValueError):
            build_cursor_map(self.path, (1, 1), [paragraph(1, "A"), paragraph(1, "B")])

    def test_large_repetitive_alignment_is_rejected_before_expensive_matcher(self):
        source = "甲乙丙丁" * 3000
        target = source[:6000] + "戊" + source[6001:]
        with patch("yeone_ocr.collate.cursor_map.SequenceMatcher",
                   side_effect=AssertionError("cost limit must precede alignment")):
            self.assertEqual(_trusted_equal_map(source, target), {})

    def test_identical_repetitive_corpus_uses_identity_without_matcher(self):
        text = "甲" * 4000
        with patch("yeone_ocr.collate.cursor_map.SequenceMatcher",
                   side_effect=AssertionError("identity needs no alignment")):
            aligned = _trusted_equal_map(text, text)
            self.assertEqual(len(aligned), len(text))
            self.assertEqual(aligned[0], 0)
            self.assertEqual(aligned[len(text) - 1], len(text) - 1)
            self.assertEqual(_trusted_equal_map("", text), {})

    def cached(self, source, text, *, page=1, bounds=None, regions=None):
        from yeone_ocr.state import Work
        work = Work(str(source), str(Path(self.folder.name) / 'work'))
        if regions is None:
            regions = [{'id': 'body', 'kind': '본문', 'bbox': bounds or [100, 200, 600, 800],
                        'paragraphs': [{'columns': [{'col_from_right': 1, 'text': text}]}]}]
        record = {'file': source.name, 'pdf_page': page,
                  'merged': {'pdf_page': page, 'regions': regions}}
        work.save_record(page, record)
        return work

    def image(self, path=None, color='white'):
        path = path or Path(self.folder.name) / 'scan.png'
        with Image.new('RGB', (300, 400), color) as image:
            image.save(path)
        return path

    def test_image_cached_region_is_read_only_and_never_claims_precise_glyph(self):
        text = '瀉白散淸熱平喘止咳獨特原文位置'
        source = self.image()
        work = self.cached(source, text)
        paths = (source, work.dir / 'source.json', work.record_path(1))
        before = {path: path.read_bytes() for path in paths}
        with patch.object(type(work), 'ensure_source', side_effect=AssertionError('read only')):
            mapping = build_cursor_map(source, (1, 1), [paragraph(5, text)], work_dir=work.dir)
        location = mapping.locate(5, text, 8)
        self.assertTrue(mapping.available)
        self.assertEqual(len(location.boxes), 1)
        for actual, expected in zip(location.boxes[0], (.2, .1, .8, .6)):
            self.assertAlmostEqual(actual, expected, places=7)
        self.assertEqual(location.page, 1)
        self.assertFalse(location.precise)
        self.assertIsNone(location.caret_box)
        self.assertIn('저장된 OCR 영역', location.message)
        self.assertIsNone(mapping.page_font_height(1))
        self.assertIsNone(mapping.page_font_width(1))
        self.assertEqual(before, {path: path.read_bytes() for path in paths})

    def test_image_directory_natural_order_and_multiframe_page_range(self):
        text = 'Second image only has this unique saved reading.'
        folder = Path(self.folder.name) / 'images'
        folder.mkdir()
        self.image(folder / 'scan10.png', 'blue')
        self.image(folder / 'scan2.png', 'red')
        work = self.cached(folder, text, page=2)
        mapping = build_cursor_map(folder, (2, 2), [paragraph(2, text)], work_dir=work.dir)
        self.assertEqual(mapping.locate(2, text, 5).page, 2)
        self.image(folder / 'scan1.png', 'green')
        stale = build_cursor_map(folder, (2, 2), [paragraph(2, text)], work_dir=work.dir)
        self.assertFalse(stale.available)
        tiff = Path(self.folder.name) / 'two.tiff'
        with Image.new('RGB', (30, 40), 'white') as a, Image.new('RGB', (30, 40), 'black') as b:
            a.save(tiff, save_all=True, append_images=[b])
        self.assertEqual(read_cursor_pages(tiff, (2, 2)).key, '')
        with self.assertRaises(ValueError):
            read_cursor_pages(tiff, (3, 3))

    def test_pdf_text_layer_precedes_cached_region_and_keeps_precise_caret(self):
        text = 'Existing PDF glyphs have a known and unique position.'
        self.pdf([[text]])
        work = self.cached(self.path, text, bounds=[100, 100, 900, 900])
        with patch.object(type(work), 'source_matches', side_effect=AssertionError('PDF text needs no cache')):
            mapping = build_cursor_map(self.path, (1, 1), [paragraph(1, text)], work_dir=work.dir)
        self.assertTrue(mapping.locate(1, text, 5).precise)
        self.assertIsNotNone(mapping.page_font_width(1))

    def test_missing_malformed_and_stale_cached_sources_do_not_link_or_write(self):
        text = 'A cached region whose source must be verified before use.'
        source = self.image()
        missing = Path(self.folder.name) / 'not-created'
        self.assertFalse(build_cursor_map(source, (1, 1), [paragraph(1, text)], work_dir=missing).available)
        self.assertFalse(missing.exists())
        work = self.cached(source, text)
        raw = work.record_path(1).read_bytes()
        work.record_path(1).write_bytes(b'{broken-json')
        self.assertFalse(build_cursor_map(source, (1, 1), [paragraph(1, text)], work_dir=work.dir).available)
        self.assertEqual(work.record_path(1).read_bytes(), b'{broken-json')
        self.assertFalse((work.readings_dir / 'broken').exists())
        work.record_path(1).write_bytes(raw)
        self.image(source, 'black')
        self.assertFalse(build_cursor_map(source, (1, 1), [paragraph(1, text)], work_dir=work.dir).available)
        self.assertFalse((work.dir / 'archives').exists())

    def test_cached_geometry_requires_explicit_boxes_and_matching_page_numbers(self):
        text = 'A unique OCR body has valid geometry only when explicitly saved.'
        source = self.image()
        work = self.cached(source, text)
        original = json.loads(work.record_path(1).read_bytes())
        cases = []
        for key, value in [('pdf_page', 2), ('pdf_page', True)]:
            altered = json.loads(json.dumps(original))
            altered[key] = value
            cases.append(altered)
        for box in (None, [100, 0, 100, 1000], [0, 0, 1200, 1000], [False, 0, 500, 500]):
            altered = json.loads(json.dumps(original))
            if box is None:
                del altered['merged']['regions'][0]['bbox']
            else:
                altered['merged']['regions'][0]['bbox'] = box
            cases.append(altered)
        altered = json.loads(json.dumps(original))
        altered['merged']['pdf_page'] = 2
        cases.append(altered)
        for record in cases:
            with self.subTest(record=record):
                work.save_record(1, record)
                self.assertFalse(build_cursor_map(source, (1, 1), [paragraph(1, text)], work_dir=work.dir).available)

    def test_cached_repeated_text_and_duplicate_regions_are_not_guessed(self):
        text = 'Repeated passage without distinguishing surroundings.'
        source = self.image()
        region = lambda identity, box: {'id': identity, 'kind': '본문', 'bbox': box,
            'paragraphs': [{'columns': [{'text': text}]}]}
        work = self.cached(source, text, regions=[region('a', [0, 0, 400, 400]), region('b', [600, 600, 1000, 1000])])
        self.assertFalse(build_cursor_map(source, (1, 1), [paragraph(1, text)], work_dir=work.dir).available)
        work.save_record(1, {'pdf_page': 1, 'merged': {'pdf_page': 1, 'regions': [region('a', [0, 0, 400, 400]), region('a', [600, 600, 1000, 1000])]}})
        self.assertFalse(build_cursor_map(source, (1, 1), [paragraph(1, text)], work_dir=work.dir).available)

    def test_cached_region_uses_actual_embedded_image_frame(self):
        text = 'Cached image coordinates follow the actual scan inset.'
        image = self.image()
        with pymupdf.open() as doc:
            page = doc.new_page(width=300, height=400)
            page.insert_image(pymupdf.Rect(10, 10, 290, 390), filename=str(image), keep_proportion=False)
            doc.save(self.path)
        work = self.cached(self.path, text)
        mapping = build_cursor_map(self.path, (1, 1), [paragraph(1, text)], work_dir=work.dir)
        box = mapping.locate(1, text, 3).boxes[0]
        for actual, expected in zip(box, ((10 + 280 * .2) / 300, (10 + 380 * .1) / 400,
                                         (10 + 280 * .8) / 300, (10 + 380 * .6) / 400)):
            self.assertAlmostEqual(actual, expected, places=5)

    def test_cached_columns_only_use_region_box_and_ignore_nonbody_regions(self):
        text = '甲乙丙丁戊己庚辛壬癸子丑寅卯辰巳午未'
        source = self.image()
        work = self.cached(source, text, regions=[
            {'id': 'body', 'kind': '본문', 'bbox': [100, 200, 800, 900],
             'paragraphs': [{'columns': [{'col_from_right': 2, 'text': text[9:]},
                                         {'col_from_right': 1, 'text': text[:9]}]}]},
            {'id': 'image', 'kind': '삽화', 'bbox': [0, 0, 1000, 1000], 'title_text': 'not body text'}])
        mapping = build_cursor_map(source, (1, 1), [paragraph(1, text)], work_dir=work.dir)
        first, last = mapping.locate(1, text, 1), mapping.locate(1, text, len(text) - 1)
        self.assertEqual(first.boxes, last.boxes)
        self.assertFalse(first.precise)
        self.assertFalse(last.precise)
        self.assertIsNone(first.caret_box)


if __name__ == "__main__":
    unittest.main()
