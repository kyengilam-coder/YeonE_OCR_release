"""단순 표시(--display simple)와 로컬 우선 인식의 글자별 등급. 모델 호출·네트워크 없음."""
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_hwpx_golden import records   # noqa: E402
from yeone_ocr.blocks import assemble_blocks, simplify_marks   # noqa: E402
from yeone_ocr.comparison import ComparisonProject   # noqa: E402
from yeone_ocr.grades import SIMPLE_GRADE_STYLE, page_grades   # noqa: E402
from yeone_ocr.local_first import _local_grades   # noqa: E402
from yeone_ocr.local_reading import reading_from_run   # noqa: E402


class Marks(unittest.TestCase):
    def test_marks_fold_to_first_reading_and_work_notes_drop(self):
        text, origins, red = simplify_marks("甲〔入/人〕乙〔疑: 龍〕丙〔판독불명〕〔→PDF 2쪽〕丁〔원본 결락〕")
        self.assertEqual(text, "甲入乙龍丙□丁〔원본 결락〕")
        self.assertEqual(len(origins), len(text))
        self.assertEqual([text[a:e] for a, e in red], ["入", "龍", "□", "〔원본 결락〕"])
        self.assertEqual(simplify_marks("無表示")[0], "無表示")


class SimpleBlocks(unittest.TestCase):
    def test_no_meta_memo_bracket_or_note_and_pages_become_bookmarks(self):
        with tempfile.TemporaryDirectory() as td:
            recs, ctx = records(Path(td))
            blocks = assemble_blocks(recs, grades=True, context=ctx, check_list="all", legend=True, display="simple")
        kinds = {b.type for b in blocks}
        self.assertNotIn("meta", kinds)
        self.assertNotIn("uncertain", kinds)
        self.assertFalse(any(b.memos for b in blocks))
        texts = "".join(b.text for b in blocks)
        self.assertNotIn("〔", texts)
        self.assertIn("主霍乱入腹痛，龍吐□", texts)
        anchors = [label for b in blocks for _, label in b.anchors]
        self.assertEqual(anchors, ["PDF 1쪽 · 인쇄 3쪽", "PDF 2쪽 · 인쇄 4쪽"])
        body = next(b for b in blocks if "霍乱" in b.text)
        reds = [body.text[a:e] for a, e, k in body.spans if k == "red"]
        self.assertEqual(reds, ["入", "龍", "□"])
        decided = next(b for b in blocks if "日一兩" in b.text)
        grades = sorted(k for _, _, k in decided.spans)
        self.assertEqual(grades, ["blue", "green", "red"])
        self.assertNotIn("red_ctx", grades)

    def test_standard_display_is_unchanged(self):
        with tempfile.TemporaryDirectory() as td:
            recs, ctx = records(Path(td))
            a = assemble_blocks(recs, grades=True, context=ctx, check_list="all", legend=True)
            recs, ctx = records(Path(td))
            b = assemble_blocks(recs, grades=True, context=ctx, check_list="all", legend=True, display="standard")
        self.assertEqual(a, b)
        self.assertTrue(any(x.type == "meta" for x in a))

    def test_simple_hwpx_has_no_memo_and_plain_highlight_runs(self):
        try:
            import hwpx  # noqa: F401
        except ImportError:
            self.skipTest("python-hwpx 없음")
        from yeone_ocr.hwpx_builder import build_hwpx
        with tempfile.TemporaryDirectory() as td:
            recs, ctx = records(Path(td))
            blocks = assemble_blocks(recs, grades=True, context=ctx, display="simple")
            out = Path(td) / "simple.hwpx"
            stats = build_hwpx(blocks, str(out), simple=True)
            with zipfile.ZipFile(out) as z:
                section = z.read("Contents/section0.xml").decode("utf-8")
                header = z.read("Contents/header.xml").decode("utf-8")
        self.assertEqual(stats["memos"], 0)
        self.assertNotIn('type="MEMO"', section)
        self.assertNotIn('pageBreak="1"', section)
        self.assertEqual(stats["bookmarks"], 2)
        self.assertIn(SIMPLE_GRADE_STYLE["red"]["color"], header)
        self.assertNotIn('shape="DOT"', header)
        self.assertNotIn('shape="DASH"', header)


class FakeProject:
    _mapped_span = staticmethod(ComparisonProject._mapped_span)

    def __init__(self, suggestions=(), proposals=(), reviews=()):
        self._page = {"suggestions": list(suggestions), "ai_reviews": list(reviews)}
        self._proposals = list(proposals)

    def page(self, _number):
        return self._page

    def ai_proposals(self, _number):
        return self._proposals


RAW = {"text": "甲乙丙\n丁戊己", "image_width": 100, "image_height": 60,
       "words": [{"text": "甲乙丙", "bbox": [0, 0, 90, 20], "confidence": 99.0},
                 {"text": "丁戊己", "bbox": [0, 30, 90, 50], "confidence": 40.0}]}


class LocalGrades(unittest.TestCase):
    def run_of(self, engine="ppocrv6"):
        return {"engine": engine, "raw": RAW}

    def test_low_confidence_is_blue_disagreement_red_and_edits_green(self):
        project = FakeProject(suggestions=[{"start": 1, "end": 2, "kind": "difference", "decision": "pending"},
                                           {"start": 2, "end": 3, "kind": "whitespace", "decision": "pending"}])
        current = "甲乙丙\n丁戊庚"
        grades = _local_grades(project, 1, self.run_of(), current)
        self.assertIn({"region_id": "local-line-1", "para": 0, "start": 1, "end": 2, "grade": "red"}, grades)
        self.assertIn({"region_id": "local-line-2", "para": 0, "start": 0, "end": 2, "grade": "blue"}, grades)
        self.assertIn({"region_id": "local-line-2", "para": 0, "start": 2, "end": 3, "grade": "green"}, grades)
        self.assertEqual(len(grades), 3)

    def test_edit_is_green_where_nothing_stronger_applies(self):
        grades = _local_grades(FakeProject(), 1, self.run_of("glmocr"), "甲乙丁\n丁戊己")
        self.assertEqual(grades, [{"region_id": "local-line-1", "para": 0, "start": 2, "end": 3, "grade": "green"}])

    def test_pending_ai_proposal_and_unknown_glyph_are_red(self):
        review = {"id": "r1", "request": {"snapshot_text": RAW["text"]}}
        proposal = {"review_id": "r1", "start": 0, "end": 1}
        grades = _local_grades(FakeProject(proposals=[proposal], reviews=[review]), 1, self.run_of("glmocr"), "甲乙�\n丁戊己")
        self.assertEqual([(g["region_id"], g["start"], g["grade"]) for g in grades],
                         [("local-line-1", 0, "red"), ("local-line-1", 2, "red")])

    def test_page_grades_paint_local_spans(self):
        reading = reading_from_run(RAW, 1, "horizontal")
        rec = {"pdf_page": 1, "merged": reading.model_dump(), "reader": "local", "pass2": {},
               "local_grades": [{"region_id": "local-line-2", "para": 0, "start": 0, "end": 3, "grade": "blue"}]}
        pg = page_grades(rec)
        self.assertEqual(pg.spans[("local-line-2", 0)], [(0, 3, "blue")])
        self.assertEqual(pg.stats["blue"], 1)


if __name__ == "__main__":
    unittest.main()
