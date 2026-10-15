"""실행 중 값(디자인 명세 §5.4·§5.6·§5.7·§9): 판독의 쪽 알림(cli.PAGE_EVENT → 큐 ('page', 쪽, 상태))과 쪽 띠의 칸·'지금 14~18쪽 판독 중',
진행 알림이 가져온 그때의 호출 수로 나눈 단계별 호출(_calls_by_stage)과 장부 묶음의 '호출 n회 / 예상 m회', [아니요] 실행의 판독만 어림,
멈춘 뒤 남은 '지금' 칸. 명령은 mock 판독기로 돌려 모델을 부르지 않는다. 쪽 알림은 출력·기록 파일을 바꾸지 않는다."""
import contextlib
import io
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_ops import gui_module

ROWS = [{"step": "read1", "calls": [45, 45], "usd": [2.82, 4.94]}, {"step": "read2", "calls": [90, 90], "usd": [2.61, 3.74]},
        {"step": "glyph", "calls": [0, 0], "usd": [0.0, 0.0]}, {"step": "suspect", "calls": [45, 180], "usd": [0.27, 1.41]},
        {"step": "choice", "calls": [15, 15], "usd": [0.48, 0.96]}]
LED = {"rows": ROWS, "fx": 1405.0, "krw": [8683.0, 15511.0],
       "totals": {"calls": [195, 330], "usd": [6.18, 11.05], "usd_known": [6.18, 11.05], "missing_prices": []}}


class PageEventsFromRead(unittest.TestCase):
    """cmd_read 가 쪽마다 알린다: 처음에 기록 있는 쪽 'record' · 읽을 쪽 'remain', 보낼 때 'now', 끝나면 'done', 일시 오류는 'fail',
    치명 오류(키·한도·호출 상한)는 그 쪽을 'remain' 으로 — 다시 실행하면 그 쪽부터. 알림 함수가 깨져도 판독은 그대로."""
    def setUp(self):
        from test_auto import vertical_pdf
        from yeone_ocr import cli
        self.cli = cli
        self.td = tempfile.TemporaryDirectory(); self.addCleanup(self.td.cleanup)
        doc = vertical_pdf(3); self.pdf = os.path.join(self.td.name, "v.pdf"); doc.save(self.pdf); doc.close()
        self.events = []
        cli.PAGE_EVENT = lambda n, st: self.events.append((n, st))
        self.addCleanup(setattr, cli, "PAGE_EVENT", None)

    def read(self, work="w", *extra):
        self.events.clear()
        with contextlib.redirect_stdout(io.StringIO()):
            return self.cli.main(["read", self.pdf, "--work", os.path.join(self.td.name, work), "--reader", "mock", "--workers", "1", *extra])

    def test_read_reports_each_page_state(self):
        from test_auto import patched_mock_reader
        with patched_mock_reader(fail_pages={2}):
            self.assertEqual(self.read(), 3)
        self.assertEqual(self.events, [(1, "remain"), (2, "remain"), (3, "remain"), (1, "now"), (1, "done"), (2, "now"), (2, "fail"),
                                       (3, "now"), (3, "done")])
        self.assertEqual(self.read(), 0)                                        # 다시: 실패한 쪽만 — 기록 있는 쪽은 'record'
        self.assertEqual(self.events, [(1, "record"), (2, "remain"), (3, "record"), (2, "now"), (2, "done")])
        self.assertEqual(self.read(), 0)                                        # 모두 기록 있음(모델 호출 없이 건너뜀)
        self.assertEqual(self.events, [(1, "record"), (2, "record"), (3, "record")])
        self.read("w", "--force")                                               # 다시 판독: 기록이 있어도 읽을 차례
        self.assertEqual(self.events[:3], [(1, "remain"), (2, "remain"), (3, "remain")])
        self.assertEqual(self.events[-1], (3, "done"))

    def test_fatal_error_returns_page_to_remain(self):
        from test_auto import patched_mock_reader
        from yeone_ocr.reader_gemini import ReaderError
        from yeone_ocr.reader_mock import MockReader
        made: list = []

        def fatal_on_2(img, file_name, pdf_page, total, extra=""):
            if pdf_page == 2:
                raise ReaderError("키가 거부되었습니다(검사용)", fatal=True)
            return MockReader.read_page(made[0], img, file_name, pdf_page, total, extra)
        with patched_mock_reader(read_page=fatal_on_2) as made:
            self.assertEqual(self.read(), 1)
        self.assertEqual(self.events, [(1, "remain"), (2, "remain"), (3, "remain"), (1, "now"), (1, "done"), (2, "now"), (2, "remain")])

    def test_page_hook_errors_do_not_stop_work(self):
        self.cli.PAGE_EVENT = lambda *a: 1 / 0
        self.assertEqual(self.read(), 0)
        from yeone_ocr.state import Work
        self.assertEqual([r["pdf_page"] for r in Work(self.pdf, os.path.join(self.td.name, "w")).records()], [1, 2, 3])
        self.cli.page_event(1, "done")                                          # 예외를 삼킨다

    def test_run_reports_pages_in_order(self):
        from test_auto import _join_prepare_ahead
        with contextlib.redirect_stdout(io.StringIO()):
            rc = self.cli.main(["run", self.pdf, "--work", os.path.join(self.td.name, "r"), "--reader", "mock", "--checkpoint", "0"])
        _join_prepare_ahead()
        self.assertEqual(rc, 0)
        last = {}
        for n, st in self.events:
            self.assertIn(st, self.cli.PAGE_STATES)
            if st == "done":
                self.assertEqual(last.get(n), "now", n)                         # 끝나기 전에 늘 '지금'
            last[n] = st
        self.assertEqual(last, {1: "done", 2: "done", 3: "done"})


class LiveViews(unittest.TestCase):
    """창 없이 검사하는 그림 값(gui_progress): 지금 쪽 글, 큰 숫자 줄, 단계 몫 → 장부 묶음, 이번 실행 어림, 끝난 칸."""
    def setUp(self):
        from yeone_ocr import gui_progress
        self.G = gui_progress

    def test_now_text_and_big_line(self):
        G = self.G
        self.assertEqual(G.now_text([16, 14, 15, 18, 17]), "14~18")
        self.assertEqual(G.now_text([14, 16, 19]), "14·16·19")
        self.assertEqual(G.now_text([3, 9, 12, 20, 31, 40]), "3·9·12·20…")
        self.assertEqual((G.now_text([7]), G.now_text([])), ("7", ""))
        run = G.big_view("run", phase="running", stage="read", done=12, total=45, now_pages=[14, 15, 16, 17, 18])
        self.assertEqual(run[-1], (" · 지금 14~18쪽 판독 중 (동시 5쪽)", "m"))
        one = G.big_view("run", phase="running", stage="read", done=0, total=1, now_pages=[1])     # 스크린샷 한 장(쪽 띠는 숨김)
        self.assertEqual(one, [("판독", "m"), ("0", "big"), ("/ 1쪽", "unit"), (" · 지금 1쪽 판독 중", "m")])
        adj = G.big_view("run", phase="running", stage="adjudicate", done=3, total=44, now_pages=[5])
        self.assertNotIn("지금", "".join(t for t, _ in adj))                                       # 판독이 아닌 단계에는 붙이지 않음

    def test_calls_split_into_ledger_groups(self):
        by = {"": 2, "prepare": 1, "read": 30, "adjudicate": 10, "build": 5, "review": 0}
        self.assertEqual(self.G.calls_split(by, ("build", 3)), {"read": 31, "model": 18, "other": 2})
        self.assertEqual(self.G.calls_split(None, None), {"read": 0, "model": 0, "other": 0})

    def test_run_totals_follow_the_choice(self):
        G = self.G
        self.assertIsNone(G.run_totals(None, False))
        all_ = G.run_totals(LED, False)
        self.assertEqual((all_["calls"], all_["usd"], all_["krw"]), ([195, 330], [6.18, 11.05], [8683.0, 15511.0]))
        no = G.run_totals(LED, True)                                            # [아니요 · 모델 단계 없이]: 판독 줄만
        self.assertEqual(no["calls"], [135, 135])
        self.assertAlmostEqual(no["usd"][0], 5.43); self.assertAlmostEqual(no["usd"][1], 8.68)
        self.assertAlmostEqual(no["krw"][1], 8.68 * 1405.0)
        unpriced = dict(LED, rows=[dict(r, usd=None) if r["step"] == "read2" else r for r in ROWS],
                        totals=dict(LED["totals"], usd=None, missing_prices=["gpt-x"]))
        no = G.run_totals(unpriced, True)
        self.assertIsNone(no["usd"]); self.assertIsNone(no["krw"]); self.assertEqual(no["known"], [2.82, 4.94]); self.assertEqual(no["missing"], ["gpt-x"])

    def test_merge_and_settle_marks(self):
        G = self.G
        disk = {1: "record", 2: "remain", 3: "fail", 4: "remain", 5: "record"}
        live = {2: "done", 3: "now", 4: "now", 9: "done"}
        self.assertEqual(G.merge_marks(disk, live, finished=False), {1: "record", 2: "done", 3: "now", 4: "now", 5: "record"})
        self.assertEqual(G.merge_marks(disk, live, finished=True, before={3: "fail"}),
                         {1: "record", 2: "done", 3: "fail", 4: "remain", 5: "record"})        # 실패였던 쪽을 다시 읽다 멈춤 → 그대로 실패
        self.assertEqual(G.settle_marks({1: "now", 2: "done", 3: "now"}, {3: "fail"}), {1: "remain", 2: "done", 3: "fail"})

    def test_grouped_ticks_are_evenly_spaced(self):
        per, cells = self.G.strip_layout(462, 990, 2, 12)
        self.assertGreater(per, 1)
        ticks = self.G.strip_ticks(list(range(1, 463)), per, cells)
        xs = [x for x, _ in ticks]
        gaps = [b - a for a, b in zip(xs[1:-2], xs[2:-1])]                     # 첫·끝 눈금 사이를 뺀 둥근 수 눈금끼리
        self.assertGreaterEqual(min(gaps), 40 - 2); self.assertLess(max(gaps) - min(gaps), 3)
        for x, t in ticks:                                                      # 눈금은 그 쪽이 든 칸 안에
            k = int(t) - 1
            x0, x1, _, _ = cells[k // per]
            self.assertTrue(x0 <= x <= x1, t)


class CallsByStage(unittest.TestCase):
    """이번 실행 호출을 단계 몫으로: 진행 알림이 가져온 그때의 수로 앞 단계 몫을 확정(알림이 늦게 와도 앞 단계의 호출이 다음 단계로 새지 않음),
    알림 사이에는 지금 수를 지금 단계 몫으로 잠시. 명령이 세지 않을 때(_calls_live 꺼짐 — 시험의 가짜 실행 · 끝난 뒤)는 읽지 않는다."""
    def test_snapshots_settle_previous_stage(self):
        with gui_module() as gui:
            app = types.SimpleNamespace(running=True, _calls_live=True, tracker=types.SimpleNamespace(stage=""), _calls_used=0, _calls_base=None)
            count = gui.App._count_calls
            polled = [0]
            with mock.patch.object(gui.App, "_call_count", side_effect=lambda: polled[0]):
                self.assertFalse(count(app, 0))
                app.tracker.stage = "read"
                self.assertTrue(count(app, 31)); self.assertEqual((app._calls_by_stage, app._calls_used), ({"read": 31}, 31))
                polled[0] = 40
                self.assertTrue(count(app)); self.assertEqual((app._calls_tail, app._calls_used), (("read", 9), 40))
                self.assertTrue(count(app, 38))                                 # 3차 판별로 넘어가는 알림(그때 38) — 늦게 와도 판독 몫은 38까지
                self.assertEqual((app._calls_by_stage, app._calls_used), ({"read": 38}, 38))
                app.tracker.stage = "adjudicate"
                polled[0] = 45
                self.assertTrue(count(app)); self.assertEqual(app._calls_tail, ("adjudicate", 7))
                from yeone_ocr.gui_progress import calls_split
                self.assertEqual(calls_split(app._calls_by_stage, app._calls_tail), {"read": 38, "model": 7, "other": 0})
                app._calls_live = False; polled[0] = 99
                self.assertFalse(count(app)); self.assertEqual(app._calls_used, 45)
                app._calls_live = True; app.running = False
                self.assertFalse(count(app, 99))

    def test_polling_waits_for_the_first_snapshot(self):
        """명령이 CALL_CAP 을 0 으로 돌리기 전의 수(앞 명령의 것)를 이번 실행 호출로 세지 않는다."""
        with gui_module() as gui:
            app = types.SimpleNamespace(running=True, _calls_live=True, tracker=types.SimpleNamespace(stage=""), _calls_base=None)
            with mock.patch.object(gui.App, "_call_count", return_value=17):
                self.assertFalse(gui.App._count_calls(app)); self.assertFalse(hasattr(app, "_calls_used"))


if __name__ == "__main__":
    unittest.main()
