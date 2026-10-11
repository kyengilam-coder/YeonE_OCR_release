"""0.6.8 마무리: 비용 어림의 2차 덜 된 쪽, '기록 있는 쪽도 다시 판독' 전 사본, 끝 알림의 '실패한 쪽' 문구."""
import contextlib, io, os, sys, tempfile, unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_auto import vertical_pdf  # noqa: E402
from test_pipeline import _gui  # noqa: E402


class EstimateIncomplete(unittest.TestCase):
    """2차가 덜 된 쪽(pass2_failed)은 '이미 읽은 쪽' 으로 세지 않고 다시 읽을 쪽으로 잡는다(0 회로 보이지 않게)."""
    def test_pass2_failed_pages_are_not_done(self):
        from yeone_ocr.estimate import estimate
        with tempfile.TemporaryDirectory() as td:
            doc = vertical_pdf(3); pdf = os.path.join(td, "v.pdf"); doc.save(pdf); doc.close()
            u = {"calls": 3, "prompt_tokens": 9000, "output_tokens": 3000}
            recs = [{"pdf_page": 1, "pass1": {}, "usage": u}, {"pdf_page": 2, "pass1": {}, "usage": u, "pass2_failed": ["b2"]}]
            e = estimate(pdf, [1, 2, 3], model="gemini-2.5-pro", records=recs)
            self.assertEqual(e["already_read"], 1)
            self.assertTrue(any("2차가 덜 된 쪽 1개" in n for n in e["notes"]), e["notes"])
            self.assertGreater(e["calls"], 0)


class ForceBackup(unittest.TestCase):
    """--force(기록 있는 쪽도 다시 판독)는 덮어쓰기 전에 readings/prev/ 에 사본을 남긴다(손으로 고친 merged 보존)."""
    def test_force_reread_keeps_prev_copy(self):
        from yeone_ocr import cli
        from yeone_ocr.state import Work
        with tempfile.TemporaryDirectory() as td:
            doc = vertical_pdf(2); pdf = os.path.join(td, "v.pdf"); doc.save(pdf); doc.close(); w = os.path.join(td, "w")
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(cli.main(["read", pdf, "--work", w, "--reader", "mock", "--pages", "1"]), 0)
            work = Work(pdf, w); rec = work.load_record(1)
            rec["merged"]["note"] = "손으로 고침"; work.save_record(1, rec)
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(cli.main(["read", pdf, "--work", w, "--reader", "mock", "--pages", "1", "--force"]), 0)
            prev = Path(w) / "readings" / "prev" / "p001.json"
            self.assertTrue(prev.exists())
            self.assertIn("손으로 고침", prev.read_text(encoding="utf-8"))
            self.assertNotIn("손으로 고침", work.record_path(1).read_text(encoding="utf-8"))


class FinishWording(unittest.TestCase):
    """끝 코드 3 의 '실패한 쪽' 문구는 쪽 판독 실패 줄이 있을 때만 — 구획 오류·교정본 옮김은 '확인할 것'."""
    def test_pages_failed_only_for_page_failures(self):
        gui = _gui()
        self.assertIn("실패한 쪽", gui.finish_words(3, ["오류: 판독에 실패한 쪽 2개: 3, 5 — ③ 판독을 다시 누르면"])[1])
        self.assertIn("실패한 쪽", gui.finish_words(3, ["오류: 2차 판독이 일부 영역에서 실패한 쪽 1개: 4"])[1])
        st = gui.finish_words(3, ["오류: 구획 「大薊」 처리 중 예외 — 다음 구획을 계속합니다"])[1]
        self.assertIn("확인할 것", st); self.assertNotIn("실패한 쪽", st)
        k, msg = gui.finish_alert(3, "HWPX 전체 교정", ["오류: 구획 「大薊」 처리 중 예외"])
        self.assertEqual(k, "warning"); self.assertNotIn("실패한 쪽", msg)


if __name__ == "__main__":
    unittest.main()
