"""비용 한 창(costplan): 기록 있는 쪽은 정확히·새로 읽을 쪽은 이 책 평균으로 세고, 읽기만 하며(결과 폴더를 만들거나 고치지 않음),
어림이 실패하면 닫힌 쪽(모델 단계 없이·취소)을 기본으로 한다. Tk·모델 호출 없음."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pymupdf

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_suspects import rec, vertical_pdf
from yeone_ocr import adjudicate, cli, costplan, estimate, pdfio
from yeone_ocr.adjudicate import find_sites
from yeone_ocr.costplan import PlanCancelled, PlanInput, count_plan, default_choice, failed_plan, format_plan, needs_confirm, price_plan, without_flags
from yeone_ocr.prompts import glyph_system_prompt
from yeone_ocr.schema import PageReading
from yeone_ocr.state import Work

LINE = "天地玄黃宇宙洪荒日月盈昃辰宿列張"


def snapshot(root):
    return {p.relative_to(root).as_posix(): p.read_bytes() if p.is_file() else None for p in Path(root).rglob("*")}


def site_entry(r, a, **kw):
    s = next(x for x in find_sites(PageReading.model_validate(r["merged"])) if x.a == a)
    return {**{k: getattr(s, k) for k in ("region_id", "col_index", "a", "b", "pre", "post", "char_index")}, "run": 1, **kw}


def book(td, pages=5, records=True):
    """5쪽 세로쓰기 PDF, 앞 3쪽에 판독 기록:
    1쪽 〔玄/元〕 하나 + 합의 의심(1·2차 曰, 글자층 日) · 2쪽 〔玄/元〕·〔宙/寅〕(〔宙/寅〕 은 gemini-2.5-pro 가 물어 못 가림)
    · 3쪽 간체 '白云'(문맥 교정 云/雲 후보)·〔洪/共〕·3차가 앞서 고른 자리 하나와 그 사용량(gpt-4.1 (3차) 4회)."""
    doc = vertical_pdf(pages); pdf = os.path.join(td, "v.pdf"); doc.save(pdf); doc.close()
    w = Work(pdf, os.path.join(td, "w"))
    if not records:
        return pdf, w
    r1 = rec([LINE.replace("日月", "曰月"), LINE.replace("玄", "〔玄/元〕"), LINE], page=1)
    r2 = rec([LINE.replace("玄", "〔玄/元〕"), LINE.replace("宙", "〔宙/寅〕"), LINE], page=2)
    r2["adjudications"] = [site_entry(r2, "宙", model="gemini-2.5-pro", choice="", evidence="뭉개짐")]
    r3 = rec(["白云在天" + LINE[4:], LINE.replace("洪", "〔洪/共〕"), LINE], page=3)
    r3["adjudications"] = [{"region_id": "r1", "col_index": 2, "a": "黃", "b": "黄", "pre": "天地玄", "post": "宇宙洪荒", "char_index": 3,
                            "model": "gpt-4.1", "choice": "黃", "evidence": "획", "run": 1}]
    r3["usage"] = {"calls": 6, "prompt_tokens": 38000, "output_tokens": 3300, "thinking_tokens": 100,
                   "by_model": {"gpt-4.1": {"calls": 2, "prompt_tokens": 30000, "output_tokens": 3000},
                                "gpt-4.1 (3차)": {"calls": 4, "prompt_tokens": 8000, "output_tokens": 300, "thinking_tokens": 100}}}
    for i, r in enumerate((r1, r2, r3), 1):
        w.save_record(i, r)
    return pdf, w


def inp(pdf, w, **kw):
    return PlanInput(**{"kind": "run", "pdf": pdf, "work_dir": str(w.dir), "reader": "openai", "model": "gpt-4.1", "second_model": "auto", **kw})


def rows(plan, step, scope=None):
    return [r for r in plan["rows"] if r["step"] == step and (scope is None or r["scope"] == scope)]


class Counts(unittest.TestCase):
    def setUp(self):
        t = tempfile.TemporaryDirectory(); self.addCleanup(t.cleanup)
        self.td = t.name; self.pdf, self.w = book(self.td)

    def test_third_pass_exact_counts_honour_skip_any_model_and_blind(self):
        p = count_plan(inp(self.pdf, self.w, kind="adjudicate"))
        g, = rows(p, "glyph")
        self.assertEqual((g["scope"], g["items"], g["calls"]), ("have", 3, [3, 6]))           # 4곳 가운데 다른 모델이 못 가린 1곳은 묻지 않음 · 쪽마다 ⌈n/8⌉ + 낱개 n
        self.assertEqual((p["skipped"]["glyph"], p["skipped"]["glyph_other"]), (1, 1))
        self.assertIn("이 책 4회 호출 평균", g["basis"]); self.assertEqual(g["in"], [2000 * 3, 2000 * 6]); self.assertEqual(g["out"], [100 * 3, 100 * 6])
        self.assertEqual(g["label"], "3차 · 글자 판별 — 기록 있는 3쪽")
        same = count_plan(inp(self.pdf, self.w, kind="adjudicate", skip_any_model=False))
        g, = rows(same, "glyph")
        self.assertEqual((g["items"], g["calls"], same["skipped"]["glyph"]), (4, [3, 7], 0))   # 끄면 이번 모델로 다시 묻는다
        g, = rows(count_plan(inp(self.pdf, self.w, kind="adjudicate", blind=True)), "glyph")
        self.assertEqual(g["calls"], [6, 9])                                                  # 눈가림 재확인은 묶음 수만큼 더
        r3 = self.w.load_record(3); del r3["usage"]; self.w.save_record(3, r3)               # 이 책의 3차 실측이 없으면 토큰 기본값
        g, = rows(count_plan(inp(self.pdf, self.w, kind="adjudicate")), "glyph")
        sys_tok = len(glyph_system_prompt()) / estimate.CHARS_PER_TOKEN
        self.assertIn("토큰 기본값", g["basis"]); self.assertEqual(g["in"][0], round(3 * 500 + 3 * sys_tok))
        self.assertEqual(g["in"][1], round(2 * 3 * 1300 + 6 * sys_tok)); self.assertEqual(g["out"], [3 * 40, 2 * 3 * 160])
        self.assertEqual(count_plan(inp(self.pdf, self.w, kind="adjudicate", model="gemini-2.5-pro"))["skipped"]["glyph_other"], 0)   # 그 모델 자신의 것

    def test_suspects_counted_from_layer_cache_or_pdf_without_writing(self):
        before = snapshot(self.td)
        s, = rows(count_plan(inp(self.pdf, self.w, kind="adjudicate")), "suspect")
        self.assertEqual((s["items"], s["calls"]), (1, [1, 2]))                               # 글자층 파일이 없으면 PDF 에서 읽되 쓰지 않는다
        self.assertEqual(snapshot(self.td), before)
        with pymupdf.open(self.pdf) as doc:
            plain = pdfio.text_layer(doc, 1).plain()
        for n in (1, 2, 3):
            self.w.textlayer_path(n).write_text(json.dumps({"plain": plain}), encoding="utf-8")
        s, = rows(count_plan(inp(self.pdf, self.w, kind="adjudicate")), "suspect")
        self.assertEqual(s["items"], 1)
        self.w.textlayer_path(1).write_text(json.dumps({"plain": ""}), encoding="utf-8")       # 글자층 파일을 읽는다(글자층이 빈 쪽은 의심 없음 — PDF 로 가지 않음)
        s, = rows(count_plan(inp(self.pdf, self.w, kind="adjudicate")), "suspect")
        self.assertEqual((s["items"], s["calls"]), (0, [0, 0]))
        self.w.textlayer_path(1).write_text("{깨짐", encoding="utf-8")                          # 깨진 파일은 고치지 않고 PDF 로
        before = snapshot(self.td)
        s, = rows(count_plan(inp(self.pdf, self.w, kind="adjudicate")), "suspect")
        self.assertEqual(s["items"], 1); self.assertEqual(snapshot(self.td), before)
        self.assertEqual(rows(count_plan(inp(self.pdf, self.w, kind="adjudicate", suspects=False)), "suspect"), [])

    def test_context_choice_ask_equals_normalization_plan(self):
        recs = self.w.records()
        for layout, preset, join, anym in (("page", "regions", True, True), ("flow", "kaibao", False, False), ("flow", "regions", True, True)):
            with self.subTest(layout=layout, preset=preset, join=join):
                want = cli.normalization_plan(self.w, recs, "openai", "gpt-4.1", layout=layout, join_pages=join, preset=preset, any_model=anym)
                p = count_plan(inp(self.pdf, self.w, kind="build", layout=layout, preset=preset, join_pages=join, skip_any_model=anym))
                c, = rows(p, "choice")
                self.assertGreater(want["ask"], 0)
                self.assertEqual((c["items"], c["calls"]), (want["ask"], [want["calls"]] * 2))
                self.assertEqual((p["skipped"]["choice_reused"], p["skipped"]["choice_remembered"]), (want["reused"], want["remembered"]))
        self.assertEqual(rows(count_plan(inp(self.pdf, self.w, kind="build", context_model=False)), "choice"), [])
        with mock.patch.object(cli, "normalization_plan", side_effect=AssertionError("부르면 안 됨")):
            count_plan(inp(self.pdf, self.w, kind="adjudicate")); count_plan(inp(self.pdf, self.w, kind="read"))

    def test_context_estimate_respects_separate_correction_switches(self):
        from yeone_ocr.normalization import NormalizationOptions
        self.assertEqual(rows(count_plan(inp(self.pdf, self.w, kind="build", normalize_traditional=False, normalize_yun=False)), "choice"), [])
        for traditional, yun, punctuation in ((False, True, False), (True, False, True)):
            with self.subTest(traditional=traditional, yun=yun):
                options = NormalizationOptions(traditional=traditional, yun=yun, punctuation=punctuation)
                want = cli.normalization_plan(self.w, self.w.records(), "openai", "gpt-4.1", options=options)
                p = count_plan(inp(self.pdf, self.w, kind="build", normalize_traditional=traditional,
                                   normalize_yun=yun, normalize_punctuation=punctuation))
                c, = rows(p, "choice")
                self.assertEqual((c["items"], c["calls"]), (want["ask"], [want["calls"]] * 2))

    def test_new_pages_use_this_books_averages_or_defaults(self):
        p = count_plan(inp(self.pdf, self.w))
        self.assertEqual((p["pages"], p["records"], p["to_read"], p["new_pages"]), (5, 3, 2, 2))
        self.assertTrue(rows(p, "read1"))
        g, = rows(p, "glyph", "new")
        self.assertIn("이 책 3쪽 평균", g["basis"]); self.assertEqual(g["label"], "3차 · 글자 판별 — 새로 읽을 2쪽(어림)")
        self.assertEqual(g["items"], round((1 + 2 + 2) / 3 * 2))                                # 쪽당 자리 = 남은 〔a/b〕 + 3차가 고른 자리
        self.assertIn("기본값", rows(p, "suspect", "new")[0]["basis"])                          # 합의 의심 실측(suspect_stats)이 없는 책
        self.assertIn("이 책 3쪽 평균", rows(p, "choice", "new")[0]["basis"])
        self.assertTrue(rows(p, "glyph", "have") and rows(p, "choice", "have"))
        forced = count_plan(inp(self.pdf, self.w, force=True))                                  # 다시 판독: 모두 새로 읽는 셈, 평균은 지금 기록에서
        self.assertEqual((forced["to_read"], forced["new_pages"]), (5, 5))
        self.assertFalse([r for r in forced["rows"] if r["scope"] == "have"])
        g, = rows(forced, "glyph", "new")
        self.assertIn("이 책 3쪽 평균", g["basis"]); self.assertIn("새로 읽을 5쪽", g["label"])
        self.assertTrue(any("다시 판독" in n for n in forced["notes"]))
        self.assertEqual(count_plan(inp(self.pdf, self.w, kind="build", force=True))["rows"][0]["scope"], "have")   # 문서 만들기에는 다시 판독이 없다
        with tempfile.TemporaryDirectory() as td:                                                # 결과 폴더가 아직 없는 책
            pdf, _ = book(td, pages=2, records=False)
            folder = Path(td) / "아직없음"; before = snapshot(td)
            e = count_plan(PlanInput(kind="run", pdf=pdf, work_dir=str(folder), reader="openai", model="gpt-4.1"))
            self.assertEqual((e["to_read"], e["new_pages"], e["records"]), (2, 2, 0))
            for step in ("glyph", "suspect", "choice"):
                self.assertIn("기본값", rows(e, step, "new")[0]["basis"], step)
            self.assertIn(f"쪽당 {costplan.SITES_PER_PAGE}곳", rows(e, "glyph", "new")[0]["basis"])
            self.assertFalse(folder.exists()); self.assertEqual(snapshot(td), before)

    def test_run_choice_row_widens_for_candidates_next_to_sites_the_third_pass_decides(self):
        """run 은 3차 판별 뒤에 문서를 만든다 — 앞서 물은 후보라도 앞뒤 글의 〔a/b〕 가 정해지면 열쇠가 바뀌어 다시 묻는다. '후보 정확 · 0회' 로 보이지 않게."""
        from yeone_ocr.normalization import normalize_blocks
        recs = self.w.records()
        blocks = cli.assemble_blocks(recs, grades=False, layout="page", join_pages=True)
        seen = []
        def capture(items):
            seen.extend(items); return {}
        capture.batch_size = 1 << 30
        normalize_blocks(blocks, resolver=capture)
        rules_hash, prompt = cli._choice_hashes()
        und = {cli._normalization_keys(it, "openai", "gpt-4.1", rules_hash, prompt)[1]: "gpt-4.1" for it in seen}   # 앞선 문서 만들기가 다 물어 못 고름
        (self.w.dir / "normalization_cache.json").write_text(json.dumps({"schema_version": 1, "choices": {}, "undecided": und, "askers": [["openai", "gpt-4.1"]]}),
                                                             encoding="utf-8")
        np_ = cli.normalization_plan(self.w, recs, "openai", "gpt-4.1")
        self.assertEqual(np_["ask"], 0); self.assertGreater(np_["near_marks"], 0)
        c, = rows(count_plan(inp(self.pdf, self.w, kind="build")), "choice")
        self.assertEqual((c["calls"], c["basis"].split(" · ")[0]), ([0, 0], "후보 정확"))                # 문서 만들기만: 3차가 글을 바꾸지 않는다
        c, = rows(count_plan(inp(self.pdf, self.w, pages="1-3")), "choice")
        self.assertEqual(c["calls"], [0, 1]); self.assertGreater(c["in"][1], 0)
        self.assertIn(f"3차가 바꿀 〔a/b〕 곁 {np_['near_marks']:,}건은 다시 물을 수 있음", c["basis"])
        c, = rows(count_plan(inp(self.pdf, self.w, pages="3")), "choice")                                # 3쪽만: 3차가 물을 자리가 있으면 같은 규칙
        g, = rows(count_plan(inp(self.pdf, self.w, pages="3")), "glyph")
        self.assertEqual(c["calls"][0], 0); self.assertEqual(c["calls"][1] > 0, g["items"] > 0 and cli.normalization_plan(self.w, [self.w.load_record(3)], "openai", "gpt-4.1")["near_marks"] > 0)

    def test_older_records_that_run_will_move_to_stale_are_priced_as_reading(self):
        """run 이 판독 전에 하는 refresh_stale_records: 영역 좌표가 밀린 이전 판 기록은 stale/ 로 옮겨 1차·2차를 다시 산다 — 어림도 읽을 쪽으로 센다(읽기만)."""
        from test_auto import _drifted_page
        from yeone_ocr.schema import Column, RegionReread
        with tempfile.TemporaryDirectory() as td:
            doc = pymupdf.open(); doc.new_page(width=500, height=700); doc.new_page(width=500, height=700); pdf = os.path.join(td, "a.pdf"); doc.save(pdf); doc.close()
            w = Work(pdf, os.path.join(td, "w"))
            _, p1, _, layer = _drifted_page()
            layer["plain"] = "".join(line["text"] for line in layer["horizontal_lines"])
            w.textlayer_path(1).write_text(json.dumps(layer), encoding="utf-8")
            d = p1.model_dump(); d["pdf_page"] = 1
            r = {"file": "a.pdf", "pdf_page": 1, "version": "0.6.0", "mode": "horizontal_zh", "pass1": d, "merged": d,
                 "pass2": {"r1": RegionReread(columns=[Column(col_from_right=1, text="x")]).model_dump()}}
            w.save_record(1, r)
            before = snapshot(td)
            p = count_plan(PlanInput(kind="run", pdf=pdf, work_dir=str(w.dir), reader="gemini", model="gemini-3-pro", pages="1"))
            self.assertEqual(snapshot(td), before)                                                     # 옮기지도 다시 쓰지도 않는다
            self.assertEqual((p["to_read"], p["new_pages"], p["errors"]), (1, 1, []))
            self.assertTrue(rows(p, "read1")); self.assertFalse([x for x in p["rows"] if x["scope"] == "have"])
            self.assertTrue(any("이전 판 기록 1쪽(1)은 영역 좌표가 밀려 있어" in n for n in p["notes"]))
            self.assertEqual(count_plan(PlanInput(kind="read", pdf=pdf, work_dir=str(w.dir), reader="gemini", model="gemini-3-pro", pages="1"))["to_read"], 0)   # read 는 옮기지 않는다
            r["version"] = __import__("yeone_ocr").__version__; w.save_record(1, r)
            self.assertEqual(count_plan(PlanInput(kind="run", pdf=pdf, work_dir=str(w.dir), reader="gemini", model="gemini-3-pro", pages="1"))["to_read"], 0)

    def test_count_plan_never_writes_into_the_result_folder(self):
        cache = self.w.dir / "normalization_cache.json"
        cache.write_text(json.dumps({"schema_version": 1, "choices": {}, "undecided": {}}), encoding="utf-8")
        before = snapshot(self.td)
        for kind in ("run", "read", "build", "adjudicate"):
            count_plan(inp(self.pdf, self.w, kind=kind))
        self.assertEqual(snapshot(self.td), before)                                            # 글자층·재사용 기록·문맥 추정·기록 고침 없음


class Prices(unittest.TestCase):
    def plan(self, *models, cached=(0, 0)):
        rows = [{"step": "glyph", "label": m, "scope": "have", "model": m, "basis": "", "calls": [1, 2], "in": [1_000_000, 2_000_000],
                 "cached": list(cached), "out": [100_000, 200_000], "detail": "", "items": 1, "model_step": True} for m in models]
        return {"kind": "run", "rows": rows, "errors": [], "notes": [], "skipped": {}, "to_read": 0}

    def test_table_unknown_user_price_and_krw(self):
        p = price_plan(self.plan("claude-sonnet-4-6"), {}, 1500.0, "직접 입력")
        self.assertEqual(p["rows"][0]["usd"], [3 + 1.5, 6 + 3])                                # 입력 $3 · 출력 $15 (100만 토큰당)
        self.assertEqual(p["totals"]["usd"], [4.5, 9]); self.assertEqual(p["krw"], [4.5 * 1500, 9 * 1500])
        self.assertIn(f"내장 요금표 {estimate.PRICES_DATE}", p["price_lines"][0])
        p = price_plan(self.plan("claude-sonnet-4-6", "mystery-1"), {}, None)
        self.assertIsNone(p["rows"][1]["usd"]); self.assertIsNone(p["totals"]["usd"]); self.assertIsNone(p["krw"])
        self.assertEqual(p["totals"]["missing_prices"], ["mystery-1"]); self.assertEqual(p["totals"]["usd_known"], [4.5, 9])
        self.assertEqual(p["fx"], estimate.DEFAULT_FX_KRW); self.assertIn("금액을 다 낼 수 없음 — 단가 없는 모델: mystery-1", format_plan(p))
        p = price_plan(self.plan("claude-sonnet-4-6"), {"claude-sonnet-4-6": {"in": 1, "out": 1}}, None)
        self.assertEqual(p["rows"][0]["usd"], [1.1, 2.2]); self.assertIn("직접 입력", p["price_lines"][0])     # 넣은 단가가 요금표보다 먼저
        p = price_plan(self.plan("claude-sonnet-4-6", cached=(500_000, 500_000)), {}, None)
        self.assertAlmostEqual(p["rows"][0]["usd"][0], 0.5 * 3 + 0.5 * 0.30 + 1.5)              # 캐시 입력은 캐시 단가로
        p = price_plan(self.plan("claude-sonnet-4-6-20991231", "claude-opus-5-6"), {}, None)      # 날짜 꼬리만 붙은 이름은 같은 모델, 새 판 이름은 모름
        self.assertIsNotNone(p["rows"][0]["usd"]); self.assertEqual(p["totals"]["missing_prices"], ["claude-opus-5-6"])
        self.assertEqual({k: estimate.default_price("gpt-4.1")[k] for k in ("in", "out", "cached")}, {"in": 2.0, "out": 8.0, "cached": 0.5})   # 공식 모델 페이지
        self.assertEqual(estimate.default_price("gpt-4.1-mini")["key"], "gpt-4.1-mini"); self.assertIsNone(estimate.default_price("gpt-4.1-nano"))   # 확인하지 못한 모델은 비움
        self.assertEqual(estimate.default_price("claude-sonnet-5-5")["cached"], 0.20)

    def test_counted_anthropic_plan_is_priced_and_zero_rows_need_no_price(self):
        with tempfile.TemporaryDirectory() as td:
            pdf, w = book(td)
            p = price_plan(count_plan(inp(pdf, w, reader="anthropic", model="claude-sonnet-4-6")), {}, 1400.0)
            self.assertIsNotNone(p["totals"]["usd"]); self.assertEqual(p["models"], ["claude-sonnet-4-6"])
            self.assertTrue(format_plan(p).startswith("비용 어림: 호출 "))
        z = self.plan("mystery-1"); z["rows"][0].update(calls=[0, 0], **{"in": [0, 0], "out": [0, 0]})
        self.assertEqual(price_plan(z, {}, None)["totals"]["usd"], [0.0, 0.0])


class FailClosed(unittest.TestCase):
    def setUp(self):
        t = tempfile.TemporaryDirectory(); self.addCleanup(t.cleanup)
        self.pdf, self.w = book(t.name)

    def priced(self, **kw):
        return price_plan(count_plan(inp(self.pdf, self.w, **{"model": "gpt-test", **kw})), {}, None)   # 요금표에 없는 모델(단가 없음)

    def test_failed_stage_is_reported_and_defaults_close(self):
        with mock.patch.object(cli, "normalization_plan", side_effect=RuntimeError("boom")):
            p = self.priced()
        self.assertEqual(p["errors"], [{"step": "choice", "reason": "RuntimeError: boom"}])
        self.assertTrue(rows(p, "glyph"))                                                        # 다른 단계는 그대로 센다
        self.assertEqual(default_choice(p), "without"); self.assertTrue(needs_confirm(p))
        self.assertIn("어림하지 못함: 문맥 교정 — RuntimeError: boom", format_plan(p))
        with mock.patch.object(adjudicate, "pending_sites", side_effect=ValueError("깨진 기록")):
            p = self.priced(kind="adjudicate")
        self.assertEqual(p["errors"][0]["step"], "glyph"); self.assertEqual(default_choice(p), "without")
        with mock.patch.object(estimate, "estimate", side_effect=OSError("못 읽음")):
            p = self.priced()
        self.assertEqual(p["errors"][0]["step"], "read"); self.assertEqual(p["to_read"], 2); self.assertEqual(default_choice(p), "cancel")
        with mock.patch.object(estimate, "estimate", side_effect=AssertionError("읽을 쪽이 없으면 부르지 않음")):
            self.assertEqual(self.priced(pages="1-3")["errors"], [])
        p = self.priced(pages="엉뚱")
        self.assertEqual(p["errors"][0]["step"], "plan"); self.assertEqual(default_choice(p), "cancel"); self.assertTrue(needs_confirm(p))
        for kind in ("run", "read", "build", "adjudicate"):
            self.assertEqual(default_choice(price_plan(failed_plan(kind, "RuntimeError: x"), {}, None)), "cancel", kind)

    def test_unpriced_rows_default_to_the_closed_choice(self):
        """금액을 낼 수 없으면(요금표에 없는 모델) Enter 로 다 사지 않게: 판독이 있으면 취소, 모델 단계뿐이면 모델 단계 없이(판독·3차는 취소)."""
        p = self.priced()                                                                       # 새로 읽을 2쪽 + 모델 단계, 단가 없음
        self.assertIsNone(p["totals"]["usd"]); self.assertEqual(default_choice(p), "cancel")
        p = self.priced(pages="1-3")                                                             # 판독 없음 — 3차·문맥 교정만
        self.assertFalse(rows(p, "read1")); self.assertEqual(default_choice(p), "without")
        self.assertEqual(default_choice(self.priced(kind="build")), "without")
        self.assertEqual(default_choice(self.priced(kind="adjudicate")), "cancel")
        priced = price_plan(count_plan(inp(self.pdf, self.w, model="gpt-test")), {"gpt-test": {"in": 2, "out": 8}}, None)   # 단가를 넣으면 다시 '모두 실행'
        self.assertIsNotNone(priced["totals"]["usd"]); self.assertEqual(default_choice(priced), "all")
        builtin = price_plan(count_plan(inp(self.pdf, self.w)), {}, None)                       # 이 사용자의 gpt-4.1 은 내장 요금표로 금액이 나온다
        self.assertIsNotNone(builtin["totals"]["usd"]); self.assertEqual(default_choice(builtin), "all")
        other = price_plan(count_plan(inp(self.pdf, self.w, model="gpt-5")), {}, None)           # 모델을 바꾸면 옛 모델(gpt-4.1)의 실측을 그 단가로 셈하지 않는다
        self.assertTrue(other["rows"] and all(r["model"].startswith("gpt-5") for r in other["rows"]), other["rows"])
        self.assertIsNotNone(other["totals"]["usd"])

    def test_nothing_to_ask_needs_no_window(self):
        p = self.priced(kind="build", context_model=False)
        self.assertEqual((p["rows"], p["errors"]), ([], [])); self.assertFalse(needs_confirm(p)); self.assertEqual(default_choice(p), "all")
        p = self.priced(kind="read", pages="1-3")
        self.assertFalse(needs_confirm(p))
        self.assertTrue(needs_confirm(self.priced(kind="read")))

    def test_should_stop_cancels_and_every_pdf_is_closed(self):
        opened = []
        real = pdfio.open_pdf
        def track(path):
            d = real(path); opened.append(d); return d
        calls = []
        def stop_after(n):
            return lambda: calls.append(1) or len(calls) > n
        with mock.patch.object(pdfio, "open_pdf", side_effect=track):
            count_plan(inp(self.pdf, self.w))                                                    # 성공(판독 어림이 따로 여는 것까지)
            count_plan(inp(self.pdf, self.w, pages="엉뚱"))                                       # 쪽 범위 오류
            with mock.patch.object(cli, "normalization_plan", side_effect=RuntimeError("boom")):
                count_plan(inp(self.pdf, self.w))                                                # 단계 오류
            for n in (0, 3, 8):
                calls.clear()
                with self.assertRaises(PlanCancelled):
                    count_plan(inp(self.pdf, self.w), should_stop=stop_after(n))                 # 단계 사이·쪽 사이에서 멈춤
        self.assertGreaterEqual(len(opened), 6)
        self.assertTrue(all(d.is_closed for d in opened))
        seen = []
        count_plan(inp(self.pdf, self.w), progress=lambda d, t, s: seen.append((d, t, s)))
        self.assertTrue(any("3차 자리 세는 중" in s for _, _, s in seen)); self.assertTrue(any("문맥 교정 후보" in s for _, _, s in seen))

    def test_without_flags(self):
        self.assertEqual(without_flags("run", True), ["--no-adjudicate", "--no-context-model"])
        self.assertEqual(without_flags("run", False), ["--no-adjudicate"])
        self.assertEqual(without_flags("build", True), ["--no-context-model"])
        self.assertEqual(without_flags("build", False), [])
        self.assertEqual(without_flags("read", True), []); self.assertEqual(without_flags("adjudicate", True), [])


if __name__ == "__main__":
    unittest.main()
