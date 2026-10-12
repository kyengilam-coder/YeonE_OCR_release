"""0.6.8 평가 체계(evaluate): 저장된 기록 ↔ 사람이 입력한 HWPX 로 단계별 CER·표시 밖 오류율·3차 정밀도·오류 상관비를 잰다(모델 호출 없음).
실행: python -m unittest discover -s tests   (저장소 루트에서)"""
import contextlib, dataclasses, io, json, os, random, sys, tempfile, unittest
from argparse import ArgumentParser, Namespace
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from yeone_ocr.schema import PageReading, RegionReread
from yeone_ocr.state import Work
from yeone_ocr import evaluate as E, cli
from yeone_ocr.collate import zh

REF = "天地玄黃宇宙洪荒日月盈昃辰宿列張寒來暑往秋收冬藏"     # 기준 24자


def region(rid, cols, kind="본문"):
    return {"id": rid, "kind": kind, "bbox": [0, 0, 1000, 1000], "paras": [{"cols": [{"n": i + 1, "t": t} for i, t in enumerate(cols)]}]}


def rec(page, merged_cols, p1_cols=None, p2_cols=None, adj=None, **kw):
    """저장되는 모양(model_dump, 긴 이름) 그대로의 판독 기록. p1/p2 가 없으면 1차·2차 없는 기록."""
    r = {"file": "b.pdf", "pdf_page": page, "merged": PageReading.model_validate({"pdf_page": page, "regions": [region("r1", merged_cols)]}).model_dump(),
         "adjudications": adj or [], "merge_stats": {"long_segments": 1}}
    if p1_cols is not None:
        r["pass1"] = PageReading.model_validate({"pdf_page": page, "regions": [region("r1", p1_cols)]}).model_dump()
        r["pass2"] = {"r1": RegionReread.model_validate({"cols": [{"n": i + 1, "t": t} for i, t in enumerate(p2_cols or [])]}).model_dump()} if p2_cols else {}
    r.update(kw)
    return r


def adj_entry(mark, a, b, choice, ci, idx, pre="", post="", **kw):
    return {"region_id": "r1", "col_index": ci, "n_cols": 2, "char_index": idx, "n_chars": 8, "a": a, "b": b, "pre": pre, "post": post, "mark": mark,
            "choice": choice, "evidence": "획", "model": "pro", "loc": "layer", **kw}


def two_page_work(td):
    """1쪽: 荒→慌(1차·2차 같이 틀림), 〔日/曰〕(1차 맞음), 2쪽: 〔來/未〕→來 3차 확정(맞음), 〔牧/收〕 미정(2차가 맞음)."""
    w = Work(os.path.join(td, "b.pdf"), os.path.join(td, "w"))
    w.save_record(1, rec(1, ["天地玄黃宇宙洪慌", "〔日/曰〕月盈昃辰宿列張"], ["天地玄黃宇宙洪慌", "日月盈昃辰宿列張"], ["天地玄黃宇宙洪慌", "曰月盈昃辰宿列張"]))
    w.textlayer_path(1).write_text(json.dumps({"plain": "天地玄黃宇宙洪荒\n日月盈昃辰宿列張"}), encoding="utf-8")
    adj = [adj_entry("〔來/未〕", "來", "未", "來", 0, 1, "寒", "暑往"), adj_entry("〔牧/收〕", "牧", "收", "", 1, 1, "秋", "冬藏", loc="ink")]
    w.save_record(2, rec(2, ["寒來暑往", "秋〔牧/收〕冬藏"], ["寒來暑往", "秋牧冬藏"], ["寒未暑往", "秋收冬藏"], adj=adj))
    w.textlayer_path(2).write_text(json.dumps({"plain": "寒來暑往\n秋收冬藏"}), encoding="utf-8")
    return w


def unit_of(w, ref, pages=(1, 2)):
    return E.eval_unit(E.build_unit(w, None, "전체", list(pages), [ref], 0), False)


def brute(a, b):
    """독립 구현: 표 전체를 채우는 교과서 Levenshtein."""
    D = [list(range(len(b) + 1))] + [[i + 1] + [0] * len(b) for i in range(len(a))]
    for i in range(1, len(a) + 1):
        for j in range(1, len(b) + 1):
            D[i][j] = min(D[i - 1][j] + 1, D[i][j - 1] + 1, D[i - 1][j - 1] + (a[i - 1] != b[j - 1]))
    return D[-1][-1]


def check_ops(t, ops, ref, hyp):
    """정렬이 기준 → 가설을 빠짐없이 차례대로 덮는가: '='·'R' 은 두 글자를, 'D' 는 기준만, 'I' 는 가설만 하나씩 먹는다."""
    i = j = 0
    for op, oi, oj in ops:
        t.assertEqual((oi, oj), (i, j), ops)
        if op in "=R":
            t.assertEqual(ref[i] == hyp[j], op == "=", ops); i += 1; j += 1
        elif op == "D":
            i += 1
        else:
            j += 1
    t.assertEqual((i, j), (len(ref), len(hyp)))


class Align(unittest.TestCase):
    def test_exact_dp_matches_brute_force(self):
        """닻 사이의 DP 는 정확한 편집 거리(짧은 무작위 글로 무식한 재귀와 맞춤)."""
        def brute(a, b):
            if not a or not b:
                return len(a) + len(b)
            return min(brute(a[1:], b) + 1, brute(a, b[1:]) + 1, brute(a[1:], b[1:]) + (a[0] != b[0]))
        rng = random.Random(1)
        for _ in range(60):
            a = "".join(rng.choice("甲乙丙丁") for _ in range(rng.randint(0, 6))); b = "".join(rng.choice("甲乙丙丁") for _ in range(rng.randint(0, 6)))
            self.assertEqual(E._lev(a, b), brute(a, b), (a, b))

    def test_align_is_exact_levenshtein(self):
        """align() 자체가 정확한 편집 거리다(닻은 상한을 얻는 데만 쓰고 _fr_align·_band_dp 로 다시 맞춤): 무작위 글로 독립 구현과 맞추고, 정렬이 두 글을 차례대로 덮는지도 본다.
        닻이 엉뚱한 되풀이 구절에 걸리는 글(예전 38 ↔ 정확 25)도 정확하다. 어림이 없으면 st 의 inexact 는 0."""
        rng = random.Random(7); st = {}
        for _ in range(300):
            al = "甲乙丙丁戊"[:rng.randint(2, 5)]; base = "".join(rng.choice(al) for _ in range(rng.randint(0, 40)))
            ref = base if rng.random() < 0.5 else "".join(rng.choice(al) for _ in range(rng.randint(0, 40)))
            hyp = "".join(c for c in base if rng.random() > 0.15).replace(al[0], al[-1], rng.randint(0, 2))
            if rng.random() < 0.3:
                hyp = ref[len(ref) // 2:] + ref[:len(ref) // 2]                                   # 덩어리 자리바꿈(닻이 길을 잘못 잡기 쉬운 경우)
            ops = E.align(ref, hyp, st); D = brute(ref, hyp); self.assertEqual(E._dist(ops), D, (ref, hyp)); check_ops(self, ops, ref, hyp)
            for f in (E._fr_align, E._band_dp) if ref and hyp else ():                              # 두 정확한 방법을 가장 좁은 상한(U = 거리)으로
                ops = f(ref, hyp, D) if f is E._fr_align else f(ref, hyp, 0, 0, D); self.assertEqual(E._dist(ops), D, (f.__name__, ref, hyp)); check_ops(self, ops, ref, hyp)
        self.assertEqual(st, {"alignments": 300})                                                     # 어림 없음
        ref = "甲乙丙丁戊己庚辛味甘平無毒壬癸子丑寅卯辰巳味甘平無毒午未申酉"; hyp = "口" * 21 + "味甘平無毒" + "口" * 4
        self.assertEqual(E._dist(E.align(ref, hyp)), brute(ref, hyp)); self.assertEqual(brute(ref, hyp), 25)

    def test_cell_budget_fallback_is_upper_bound_and_counted(self):
        """칸 수 상한(CELLS)을 넘으면 표를 만들지 않고 difflib 로 어림한다 — 그때만 거리가 상한이고, 어림한 정렬·구간 수가 st 에 남는다."""
        old = E.CELLS
        try:
            E.CELLS = 60; rng = random.Random(3); st = {}
            ref = "".join(rng.choice("甲乙丙丁戊") for _ in range(200)); hyp = "".join(rng.choice("甲乙丙口") for _ in range(30))
            self.assertIsNone(E._band_dp(ref, hyp, 0, 0, 200))                                     # 200×30 칸 > 상한 → 표 없이 None
            ops = E.align(ref, hyp, st); check_ops(self, ops, ref, hyp)
            self.assertGreater(E._dist(ops), brute(ref, hyp)); self.assertEqual((st["alignments"], st["inexact"]), (1, 1)); self.assertGreaterEqual(st["approx_gaps"], 1)
        finally:
            E.CELLS = old
        self.assertEqual(E._dist(E.align(ref, hyp)), brute(ref, hyp))                                 # 상한 안이면 같은 글도 정확

    def test_ops_and_anchor(self):
        """바꿈·빠짐·덧남이 op 와 자리로 나오고, 닻(4자 이상 일치) 밖에서도 거리가 맞는다. 긴 구간은 difflib 어림으로 떨어진다."""
        ops = E.align("甲乙丙丁戊", "甲乙丙丁己")
        self.assertEqual([op for op, _, _ in ops], ["=", "=", "=", "=", "R"]); self.assertEqual(ops[-1], ("R", 4, 4))
        self.assertEqual([op for op, _, _ in E.align("甲乙丙丁戊", "甲乙丙丁")], ["=", "=", "=", "=", "D"])
        self.assertEqual(E.align("甲乙丙丁", "甲乙丙丁戊")[-1], ("I", 4, 4))
        self.assertEqual(E._dist(E.align("天地玄黃宇宙洪荒", "天地玄黃宇宙洪")), 1)
        old = E.CELLS
        try:
            E.CELLS = 10; a = "甲乙丙丁戊己庚辛"; b = "甲乙丙戊己庚壬癸"; st = {}    # 4자 닻이 없고 칸 수가 상한보다 많다 → difflib 어림 경로
            self.assertEqual([op for op, _, _ in E.align(a, b, st)], ["=", "=", "=", "D", "=", "=", "=", "R", "I"]); self.assertEqual(E._dist(E.align(a, b)), 3)
            self.assertEqual(st, {"alignments": 1, "inexact": 1, "approx_gaps": 1})
        finally:
            E.CELLS = old


class Flatten(unittest.TestCase):
    def test_marks_and_sentinels(self):
        """공백·폭 없는 글자 제거, 〔a/b〕→a·〔疑: x〕→x·〔판독불명〕→빈 글로 접으며 후보·쪽·표지를 남긴다."""
        f = E.flatten([(3, "甲 乙〔丙/丁〕​戊"), (4, chr(E._SENT0 + 2) + "〔疑: 己〕庚〔판독불명〕辛")])
        self.assertEqual(f.text, "甲乙丙戊己庚辛"); self.assertEqual(f.page, [3, 3, 3, 3, 4, 4, 4])
        self.assertEqual([(m["start"], m["end"], m["cands"], m["entry"]) for m in f.marks], [(2, 3, ["丙", "丁"], None), (4, 5, ["己", ""], 2), (6, 6, [""], None)])
        g = E.flatten([(1, E._COVER + "甲乙"), (1, "丙")]); self.assertEqual(g.cov, [True, True, False])
        self.assertEqual(E.flatten([(0, "〔丙/丁〕")], expand=False).text, "〔丙/丁〕")      # 기준 글은 표시를 접지 않는다


class Metrics(unittest.TestCase):
    def test_hand_counted_numbers(self):
        """손으로 센 값: 1차 2자(荒·收) · 2차 3자(荒·日·來) · 병합 2자 · oracle 1자(收 로 고침) · 표시 3곳 중 타당 1 · 표시 밖 0 · 3차 1/1, 결정률 1/2 ·
        공통 오류 1 · 상관비 (1/2)/(3/24)=4 · 긴 구간 2."""
        with tempfile.TemporaryDirectory() as td:
            r = unit_of(two_page_work(td), REF)
        s = r["stages"]; self.assertEqual(r["ref_chars"], 24)
        self.assertEqual([s[k]["edits"] for k in ("layer", "pass1", "pass2", "merged_default", "merged_oracle", "final")], [0, 2, 3, 2, 1, 2])
        self.assertAlmostEqual(s["pass1"]["cer"], 2 / 24); self.assertEqual(s["pass1"]["edits_raw"], 2); self.assertEqual(s["merged_oracle"]["replaced"], 1)
        self.assertLessEqual(s["merged_oracle"]["edits"], s["merged_default"]["edits"])
        self.assertEqual((r["marks"], r["marks_warranted"], r["outside_errors"]), (3, 1, 0)); self.assertAlmostEqual(r["mark_precision"], 1 / 3)
        A = r["adjudication"]; self.assertEqual((A["asked"], A["decided"], A["correct"]), (2, 1, 1)); self.assertEqual(A["decision_rate"], 0.5); self.assertEqual(A["by"]["model"]["pro"]["correct"], 1)
        self.assertEqual(r["common_errors"], 1); self.assertAlmostEqual(r["error_corr_ratio"], 4.0); self.assertEqual(r["pass2_coverage"], 1.0)
        self.assertEqual(r["long_segments"], 2); self.assertEqual(r["collate_space"], {"ref_chars": 24, "edits": 2})
        self.assertEqual([(d["ref"], d["hyp"], d["page"], d["kind"], d["marked"], d["layer"], d["pass1"], d["pass2"]) for d in r["diffs"]],
                         [("荒", "慌", 1, "바꿈", True, False, True, True), ("收", "牧", 2, "바꿈", True, False, True, False)])
        self.assertEqual(r["by_page"]["final"][2], {"ref_chars": 8, "edits": 1})

    def test_outside_and_oracle_cases(self):
        """표시 ±1자 밖 오류만 '표시 밖'. 표시 안 빠짐(〔疑: x〕 의 x 가 기준에 없음)은 oracle 이 빈 후보로 고쳐 0, 기본은 1. 3차 확정이 틀리면 정밀도 0."""
        with tempfile.TemporaryDirectory() as td:
            w = Work(os.path.join(td, "b.pdf"), os.path.join(td, "w"))
            w.save_record(1, rec(1, ["甲乙丙丁戊己庚辛", "壬癸子丑寅卯辰巳"]))                          # 1차·2차 없는 기록: 모든 단계 = merged
            r = unit_of(w, "甲乙丙丁戊己庚辛壬癸子丑寅卯辰午", (1,))
            self.assertEqual((r["stages"]["final"]["edits"], r["outside_errors"], r["marks"]), (1, 1, 0)); self.assertAlmostEqual(r["outside_rate"], 1 / 16)
            self.assertIsNone(r["mark_precision"]); self.assertIsNone(r["error_corr_ratio"]); self.assertEqual(r["stages"]["pass1"]["edits"], 1)
            w.save_record(1, rec(1, ["甲乙丙〔疑: 丁〕戊己庚辛", "壬癸子丑寅卯辰巳"]))
            r = unit_of(w, "甲乙丙戊己庚辛壬癸子丑寅卯辰巳", (1,))
            self.assertEqual((r["stages"]["merged_default"]["edits"], r["stages"]["merged_oracle"]["edits"], r["marks_warranted"], r["outside_errors"]), (1, 0, 1, 0))
            w.save_record(1, rec(1, ["甲乙丙丁戊己庚辛", "壬癸子丑寅卯辰巳"]))
            r = unit_of(w, "甲乙丙丁戊己庚辛壬癸子丑寅卯午巳", (1,))                                  # 표시 없는데 틀림 → 밖
            self.assertEqual(r["outside_errors"], 1)
            w.save_record(1, rec(1, ["甲乙丙〔丁/戊〕己庚辛", "壬癸子丑寅卯辰巳"]))
            r = unit_of(w, "甲乙丙丁己庚壬壬癸子丑寅卯辰巳", (1,))                                    # 辛→壬 은 표시에서 3자 떨어짐 → 밖, 표시는 타당하지 않음
            self.assertEqual((r["outside_errors"], r["marks_warranted"]), (1, 0))
            a = [adj_entry("〔丁/戊〕", "丁", "戊", "戊", 0, 3, "甲乙丙", "己庚辛")]
            w.save_record(1, rec(1, ["甲乙丙戊己庚辛", "壬癸子丑寅卯辰巳"], ["甲乙丙丁己庚辛", "壬癸子丑寅卯辰巳"], ["甲乙丙戊己庚辛", "壬癸子丑寅卯辰巳"], adj=a))
            r = unit_of(w, "甲乙丙丁己庚辛壬癸子丑寅卯辰巳", (1,))
            self.assertEqual(r["stages"]["merged_default"]["edits"], 0); self.assertEqual(r["stages"]["final"]["edits"], 1)    # 3차가 틀린 쪽을 골랐다
            self.assertEqual((r["adjudication"]["decided"], r["adjudication"]["correct"], r["adjudication"]["precision"]), (1, 0, 0.0))
            self.assertEqual(E.build_unit(w, None, "x", [1], [""], 0)["merged_default"].marks[0]["entry"], 0)       # 되돌린 표시에 기록 번호가 붙어 있다

    @unittest.skipUnless(zh.available(), "opencc 없음")
    def test_zh_fold_raw_vs_folded(self):
        """가로쓰기 한문 모드: 번체 HWPX ↔ 간체 판독은 접은 CER 0, 원글 CER 은 글자 수만큼."""
        with tempfile.TemporaryDirectory() as td:
            w = Work(os.path.join(td, "b.pdf"), os.path.join(td, "w")); w.save_record(1, rec(1, ["人参，大补元气。"]))
            u = E.build_unit(w, None, "전체", [1], ["人參, 大補元氣."], 0)
            r = E.eval_unit(u, True); self.assertEqual(r["stages"]["final"]["edits"], 0); self.assertEqual(r["stages"]["final"]["edits_raw"], 5)
            r = E.eval_unit(u, False); self.assertEqual(r["stages"]["final"]["edits"], 3); self.assertEqual(r["stages"]["final"]["edits_raw"], 5)   # 세로 모드는 부호만 접는다(參·補·氣)

    def test_longer_choice_scored_correct(self):
        """3차가 첫 후보보다 긴 글자를 골랐을 때(〔已/己入〕→己入): 남는 기준 글자가 표시 가장자리의 빠짐으로 정렬돼도 맞음으로 센다.
        반대로 짧은 쪽(〔己入/已〕→已)도 맞음, 뒤에 入 이 이미 있는데 己入 을 고르면(入 이 겹침) 틀림."""
        with tempfile.TemporaryDirectory() as td:
            w = Work(os.path.join(td, "b.pdf"), os.path.join(td, "w"))
            for pre1, mark, a, b, final, post, ref, ok in (("甲乙丙已丁戊庚", "〔已/己入〕", "已", "己入", "甲乙丙己入丁戊庚", "丁戊", "甲乙丙己入丁戊庚辛壬癸子", 1),
                                                              ("甲乙丙己入丁戊庚", "〔己入/已〕", "己入", "已", "甲乙丙已丁戊庚", "丁戊", "甲乙丙已丁戊庚辛壬癸子", 1),
                                                              ("甲乙丙已入丁戊", "〔已/己入〕", "已", "己入", "甲乙丙己入入丁戊", "入丁", "甲乙丙己入丁戊辛壬癸子", 0)):
                w.save_record(1, rec(1, [final, "辛壬癸子"], [pre1, "辛壬癸子"], [final, "辛壬癸子"], adj=[adj_entry(mark, a, b, b, 0, 3, "甲乙丙", post)]))
                A = unit_of(w, ref, (1,))["adjudication"]; self.assertEqual((A["decided"], A["correct"]), (1, ok), (mark, final))
        ops = E.align("甲乙丙己入丁", "甲乙丙口丁")                                                    # 빠짐이 표시 앞뒤 어느 쪽에 붙든 같은 답
        self.assertTrue(E._choice_ok(ops, 3, 4, "己入", "甲乙丙己入丁")); self.assertFalse(E._choice_ok(ops, 3, 4, "己入口", "甲乙丙己入丁"))
        self.assertFalse(E._choice_ok(E.align("甲乙丙己丁", "甲乙丙已丁"), 3, 4, "己入", "甲乙丙己丁"))       # 기준에 入 이 없음

    def test_partial_pass2_stays_pass1_uncovered(self):
        """조각 하나가 일시 오류로 빠진 2차(chunks 2 · chunks_read 1 · pass2_failed)는 2차 판독 오류로 세지 않는다 — 그 영역은 1차 글·덮지 않음으로 두고 수를 남긴다."""
        cols = ["天地玄黃宇宙洪荒", "日月盈昃辰宿列張", "寒來暑往秋收冬藏"]
        with tempfile.TemporaryDirectory() as td:
            w = Work(os.path.join(td, "b.pdf"), os.path.join(td, "w")); r = rec(1, cols, cols, cols[:1])
            r["pass2"]["r1"].update({"chunks": 2, "chunks_read": 1, "pass2_failed": "429"}); w.save_record(1, r)
            u = unit_of(w, "".join(cols), (1,))
            self.assertEqual((u["stages"]["pass1"]["edits"], u["stages"]["pass2"]["edits"], u["pass2_coverage"], u["pass2_partial"]), (0, 0, 0.0, 1))
            r["pass2"]["r1"] = RegionReread.model_validate({"cols": [{"n": i + 1, "t": t} for i, t in enumerate(cols)]}).model_dump() | {"chunks": 2, "chunks_read": 2}
            w.save_record(1, r); u = unit_of(w, "".join(cols), (1,))
            self.assertEqual((u["pass2_coverage"], u["pass2_partial"]), (1.0, 0))                      # 조각을 다 읽었으면 그대로 2차

    def test_duplicate_decided_positions_not_applied_twice(self):
        """이웃 표시가 같은 후보이고 달리 정해져 두 3차 기록이 한 자리로 찾아지면, 한 자리에 두 번 되돌리지 않는다(뒤 기록은 '잇지 못함'으로 센다)."""
        from yeone_ocr.adjudicate import find_sites, apply_choice
        pre = PageReading.model_validate({"pdf_page": 1, "regions": [region("r1", ["未〔未/末〕甲〔未/末〕〔未/末〕〔于/於〕乙"])]})
        merged = pre.model_copy(deep=True); adj = []; moved = 0
        for s, c in zip(find_sites(merged), "abab"):
            ch = s.a if c == "a" else s.b; self.assertTrue(apply_choice(merged, s, ch, moved)); moved += len(ch) - len(s.a)
            adj.append(dataclasses.asdict(s) | {"choice": ch, "evidence": "획", "model": "pro", "loc": "ink"})
        with tempfile.TemporaryDirectory() as td:
            w = Work(os.path.join(td, "b.pdf"), os.path.join(td, "w")); w.save_record(1, {"file": "b.pdf", "pdf_page": 1, "merged": merged.model_dump(), "adjudications": adj})
            u = E.build_unit(w, None, "x", [1], ["未未甲末未於乙"], 0)
            self.assertEqual(u["final"].text, "未未甲末未於乙"); self.assertEqual(u["merged_default"].text, "未未甲未未于乙")   # 3차 전 첫 후보 글 그대로(덧난 글자 없음)
            r = E.eval_unit(u, False); self.assertEqual(r["stages"]["merged_default"]["edits"], 2)
            A = r["adjudication"]; self.assertEqual((A["correct"], A["decided"] + r["adj_unlinked"]), (A["decided"], 4)); self.assertGreaterEqual(A["decided"], 3)   # 지금은 뒤 기록 하나가 잇지 못함

    def test_corr_ratio_pooled_and_coverage_bounded(self):
        """오류 상관비는 단위별 비의 평균이 아니라 센 수를 모아 낸 비. 2차 끝에 덧난 글자가 있어도 덮은 비율은 1 을 넘지 않는다."""
        with tempfile.TemporaryDirectory() as td:
            wa = Work(os.path.join(td, "a.pdf"), os.path.join(td, "wa")); wa.save_record(1, rec(1, ["甲乙丙丁戊己庚辛"], ["甲乙丙丁戊己庚口"], ["甲乙丙丁戊己庚口"]))
            ua = unit_of(wa, "甲乙丙丁戊己庚辛", (1,))
            ref = "".join(chr(0x4E00 + 100 + k) for k in range(40)); p1 = "口" * 10 + ref[10:]; p2 = ref[:5] + "口" * 10 + ref[15:]
            wb = Work(os.path.join(td, "b.pdf"), os.path.join(td, "wb")); wb.save_record(1, rec(1, [ref], [p1], [p2])); ub = unit_of(wb, ref, (1,))
            self.assertAlmostEqual(ua["error_corr_ratio"], 8.0); self.assertAlmostEqual(ub["error_corr_ratio"], 2.0)
            self.assertAlmostEqual(E._overall([ua, ub])["error_corr_ratio"], (6 / 11) / (11 / 48))
            w = Work(os.path.join(td, "c.pdf"), os.path.join(td, "wc")); w.save_record(1, rec(1, ["甲乙丙丁戊己"], ["甲乙丙丁戊己"], ["甲乙丙丁戊己庚辛壬"]))
            r = unit_of(w, "甲乙丙丁戊己", (1,)); self.assertEqual(r["pass2_coverage"], 1.0); self.assertEqual(r["stages"]["pass2"]["edits"], 3)

    def test_sample_and_labels(self):
        """층화 표본은 층마다 최소 1건·씨앗 고정, 판정(HWPX 틀림 등)은 같은 층의 오류를 그 비율만큼 뺀다."""
        diffs = [{"id": f"u1-{k:04d}", "kind": "바꿈" if k % 2 else "빠짐", "marked": k % 3 == 0, "layer": False, "chars": 1} for k in range(1, 41)]
        s1, rule = E.sample_diffs(diffs, 10); s2, _ = E.sample_diffs(diffs, 10)
        self.assertEqual([d["id"] for d in s1], [d["id"] for d in s2]); self.assertTrue(8 <= len(s1) <= 12); self.assertIn("층화", rule)
        self.assertEqual(len({E._stratum(d) for d in s1}), len({E._stratum(d) for d in diffs}))
        self.assertEqual(len(E.sample_diffs(diffs, 100)[0]), 40)
        la = E.apply_labels(diffs, {"u1-0001": "HWPX 틀림", "u1-0005": "판독 틀림"}, 400)   # 층(바꿈·표시 밖): 13건 중 판정 2, 그중 1이 OCR 오류 아님 → 그 층 13 → 6.5
        self.assertEqual((la["labelled"], la["non_ocr_labelled"]), (2, 1)); self.assertAlmostEqual(la["cer_default_corrected"], (40 - 6.5) / 400)
        self.assertAlmostEqual(la["outside_rate_corrected"], (27 - 6.5) / 400)


def make_parser():
    p = ArgumentParser(); p.add_argument("pdf"); p.add_argument("--work"); p.add_argument("--pages"); p.add_argument("--dpi", type=int, default=300); E.add_arguments(p)
    return p


def plain_hwpx(path, paras):
    from hwpx.document import HwpxDocument
    doc = HwpxDocument.new(); body = doc.ensure_run_style(font="함초롬바탕", size=11)
    for t in paras:
        p = doc.add_paragraph("", para_pr_id_ref=0, style_id_ref=0, char_pr_id_ref=body, include_run=False, inherit_style=False); p.add_run(t, char_pr_id_ref=body)
    doc.save_to_path(path); return path


def run_main(argv):
    """명령줄 진입점(cli.main) 그대로 — 출력도 돌려준다."""
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = cli.main(argv)
    return rc, out.getvalue()


class Command(unittest.TestCase):
    def test_owned_pdf_closes_on_early_return_and_exception(self):
        from unittest import mock
        from yeone_ocr import pdfio

        with tempfile.TemporaryDirectory() as td:
            hw = Path(td, "reference.hwpx")
            hw.write_bytes(b"fixture")  # 기록이 없는 분기는 HWPX 를 파싱하기 전에 끝난다.
            args = make_parser().parse_args([str(Path(td, "input.pdf")), "--hwpx", str(hw), "--work", str(Path(td, "work"))])
            for fail in (False, True):
                with self.subTest(exception=fail):
                    doc = mock.Mock()
                    with mock.patch.object(pdfio, "open_pdf", return_value=doc), \
                            mock.patch.object(E.Records, "pages", return_value=[], side_effect=RuntimeError("fixture failure") if fail else None), \
                            contextlib.redirect_stdout(io.StringIO()):
                        if fail:
                            with self.assertRaisesRegex(RuntimeError, "fixture failure"):
                                E.cmd_eval(args)
                        else:
                            self.assertEqual(E.cmd_eval(args), 2)
                    doc.close.assert_called_once_with()

    def test_no_pdf_whole_unit_writes_outputs(self):
        """명령줄(cli.main)로: PDF 가 없어도 --pages 가 있으면 기록만으로 평가(구획 없음 → 전체 한 단위)하고 summary.json·md·diffs.html 을 쓴다.
        --pages 가 없으면 PDF 가 있어야 한다(없는 파일 → '파일이 없습니다', 못 여는 파일 → '오류: PDF 를 열지 못했습니다'). 교정본은 거부."""
        with tempfile.TemporaryDirectory() as td:
            w = two_page_work(td); hw = plain_hwpx(os.path.join(td, "ref.hwpx"), [REF[:16], REF[16:]]); pdf = os.path.join(td, "없는.pdf")
            argv = ["eval", pdf, "--work", str(w.dir), "--pages", "1-2", "--hwpx", hw]
            rc, out = run_main(argv); self.assertEqual(rc, 0, out)
            self.assertIn("주의: PDF 를 열지 못해", out); self.assertIn("평가 끝", out)
            ev = w.dir / "eval"; S = json.loads((ev / "summary.json").read_text(encoding="utf-8"))
            self.assertEqual([u["name"] for u in S["units"]], ["전체"]); self.assertEqual(S["overall"]["ref_chars"], 24); self.assertEqual(S["overall"]["stages"]["pass2"]["edits"], 3)
            self.assertEqual(S["diff_total"], 2); self.assertEqual(S["sample"]["n"], 2); self.assertIn("method", S); self.assertNotIn("diffs", S["units"][0])
            self.assertEqual(S["overall"]["align"], {"alignments": S["overall"]["align"]["alignments"], "inexact": 0, "approx_gaps": 0}); self.assertGreater(S["overall"]["align"]["alignments"], 0)
            md = (ev / "summary.md").read_text(encoding="utf-8"); self.assertIn("## 단계별 CER", md); self.assertIn("| 병합(oracle) | 1 |", md); self.assertIn("유의할 점", md)
            h = (ev / "diffs.html").read_text(encoding="utf-8"); self.assertIn("표본이지 전체가 아님", h); self.assertIn(S["sample"]["ids"][1], h); self.assertIn("荒", h)
            (ev / "labels.json").write_text(json.dumps({S["sample"]["ids"][0]: "HWPX 틀림"}), encoding="utf-8")
            self.assertEqual(run_main(argv)[0], 0)
            S = json.loads((ev / "summary.json").read_text(encoding="utf-8")); self.assertEqual(S["labels_applied"]["labelled"], 1)
            self.assertLess(S["labels_applied"]["cer_default_corrected"], S["overall"]["stages"]["merged_default"]["cer"])
            self.assertIn("selected", (ev / "diffs.html").read_text(encoding="utf-8"))
            rc, out = run_main(["eval", pdf, "--work", str(w.dir), "--pages", "1-2", "--hwpx", os.path.join(td, "ref_교정.hwpx")])
            self.assertEqual(rc, 2); self.assertIn("오류: 교정본", out)
            rc, out = run_main(["eval", pdf, "--work", str(w.dir), "--hwpx", hw]); self.assertEqual(rc, 2); self.assertIn("파일이 없습니다", out)   # --pages 없이는 PDF 가 있어야
            bad = Path(td, "깨진.pdf"); bad.write_bytes(b"not a pdf")
            rc, out = run_main(["eval", str(bad), "--work", str(w.dir), "--hwpx", hw]); self.assertEqual(rc, 2); self.assertIn("오류: PDF 를 열지 못했습니다", out)

    def test_refuses_corrected_copies_by_name_and_content(self):
        """순환 평가 방지: <이름>_교정.hwpx 만이 아니라 collate 가 만드는 그 백업·임시·옮겨 둔 사본(_교정.bak·_교정.tmp·_교정_이전_N)도,
        이름을 바꾼 교정본(교정 범례 메모·교정 표지 달린 글자·이 프로그램의 교정 메모 — hwpx_edit 가 제 표시를 알아보는 셈)도 기준으로 받지 않는다.
        사람이 칠한 빨강·사람이 단 메모, 이름에 '교정' 이 든 사람 원고는 받는다."""
        import shutil
        from yeone_ocr.collate.sections import HwpxBook
        from yeone_ocr.collate.hwpx_edit import Editor, HH, HP
        from yeone_ocr.collate.collate import Item
        from yeone_ocr.hwpx_memo import MemoWriter
        with tempfile.TemporaryDirectory() as td:
            w = two_page_work(td); hw = plain_hwpx(os.path.join(td, "ref.hwpx"), [REF[:16], REF[16:]])
            ev = lambda h: run_main(["eval", os.path.join(td, "x.pdf"), "--work", str(w.dir), "--pages", "1-2", "--hwpx", h])
            for name in ("ref_교정.bak.hwpx", "ref_교정.tmp.hwpx", "ref_교정_이전_1.hwpx", "ref_교정_이전_12.hwpx"):
                shutil.copy(hw, os.path.join(td, name)); rc, out = ev(os.path.join(td, name))
                self.assertEqual(rc, 2, name); self.assertIn("오류: 교정본", out); self.assertNotIn("평가 끝", out)
            shutil.copy(hw, os.path.join(td, "ref_교정 원고.hwpx")); rc, out = ev(os.path.join(td, "ref_교정 원고.hwpx")); self.assertEqual(rc, 0, out)
            def corrected(name, memo, legend=True, untag=False):
                bk = HwpxBook(hw); pi = bk.texts().index(REF[:16])
                ed = Editor(bk, memo=memo)
                log = ed.apply([Item("A", "replace", pi, 7, 8, "荒", "荒", "慌", auto=True), Item("B1", "mark", pi, 2, 3, "玄", "立", "", note="확인")])
                self.assertTrue(all(l.ok for l in log), [l.note for l in log])
                if legend:
                    self.assertTrue(ed.add_legend_memo())
                if untag:                                                                         # 표지를 달기 전 판의 교정본처럼
                    for x in ed._header().element.iter(f"{HH}strikeout"):
                        x.set("color", "#000000")
                return ed.save_copy(os.path.join(td, name))
            for name, memo, legend, untag in (("최종본.hwpx", "notice", True, False), ("최종본2.hwpx", "inline", False, False), ("옛 교정.hwpx", "all", False, True)):
                rc, out = ev(corrected(name, memo, legend, untag))
                self.assertEqual(rc, 2, (name, out)); self.assertIn("오류: 교정본", out); self.assertNotIn("평가 끝", out)
            self.assertEqual(E.collate_traces(HwpxBook(os.path.join(td, "최종본2.hwpx"))), "교정 표지 달린 글자")
            self.assertEqual(E.collate_traces(HwpxBook(os.path.join(td, "옛 교정.hwpx"))), "이 프로그램의 교정 메모")
            # 사람 원고: 빨강 글자(표지 없음)와 사람이 단 '[확인] …' 메모 — 교정 흔적이 아니다
            bk = HwpxBook(hw); red = bk.doc.styles.ensure_run(color="#C00000", bold=True)
            p0 = next(p for p in bk.paragraphs if REF[:4] in "".join(t.text or "" for t in p.element.iter(f"{HP}t")))
            run = next(p0.element.iter(f"{HP}run")); run.set("charPrIDRef", str(red))
            MemoWriter(bk.doc, author="홍길동").wrap_t(next(run.iter(f"{HP}t")), "[확인] 원본과 다시 대조")
            p0.section.mark_dirty(); human = os.path.join(td, "사람.hwpx"); bk.doc.save_to_path(human)
            self.assertEqual(E.collate_traces(HwpxBook(human)), "")
            rc, out = ev(human); self.assertEqual(rc, 0, out)

    def test_labels_follow_diff_content(self):
        """판정은 차이의 내용(쪽·기준·가설·앞뒤 기준 글)으로 붙는다: 한 쪽을 다시 읽어 앞 차이가 사라져도 남은 차이의 판정은 그대로 따라가고,
        사라진 차이의 판정은 다른 차이로 옮겨 가지 않고 '맞지 않음'으로 센다. 표본에 없는 차이의 판정은 다음 내려받기에도 남는다."""
        with tempfile.TemporaryDirectory() as td:
            w = two_page_work(td); hw = plain_hwpx(os.path.join(td, "ref.hwpx"), [REF[:16], REF[16:]]); ev = w.dir / "eval"
            argv = ["eval", os.path.join(td, "x.pdf"), "--work", str(w.dir), "--pages", "1-2", "--hwpx", hw]
            self.assertEqual(run_main(argv)[0], 0); ids = json.loads((ev / "summary.json").read_text(encoding="utf-8"))["sample"]["ids"]
            self.assertEqual(run_main(argv)[0], 0); self.assertEqual(json.loads((ev / "summary.json").read_text(encoding="utf-8"))["sample"]["ids"], ids)   # 같은 기록이면 같은 id
            (ev / "labels.json").write_text(json.dumps({ids[0]: "HWPX 틀림", ids[1]: "판독 틀림"}), encoding="utf-8")   # 荒/慌 → HWPX 틀림 · 收/牧 → 판독 틀림
            w.save_record(1, rec(1, ["天地玄黃宇宙洪荒", "〔日/曰〕月盈昃辰宿列張"], ["天地玄黃宇宙洪荒", "日月盈昃辰宿列張"], ["天地玄黃宇宙洪荒", "曰月盈昃辰宿列張"]))
            rc, out = run_main(argv); self.assertEqual(rc, 0)
            S = json.loads((ev / "summary.json").read_text(encoding="utf-8")); la = S["labels_applied"]
            self.assertEqual(S["sample"]["ids"], [ids[1]]); self.assertEqual((la["labelled"], la["non_ocr_labelled"], la["unmatched"]), (1, 0, 1))
            self.assertAlmostEqual(la["cer_default_corrected"], S["overall"]["stages"]["merged_default"]["cer"]); self.assertIn("주의: 판정 1건", out)
            h = (ev / "diffs.html").read_text(encoding="utf-8"); self.assertIn("<option value='판독 틀림' selected>", h); self.assertNotIn("<option value='HWPX 틀림' selected>", h)
            picked = [d["id"] for d in E.sample_diffs([{"id": f"{99 - k}", "kind": "바꿈", "marked": k % 2, "layer": False} for k in range(30)], 10)[0]]
            self.assertEqual(picked, sorted(picked, reverse=True))                                    # 표본은 id 차례가 아니라 글 차례
            S2 = {"hwpx": "h", "pdf": "p", "at": "", "diff_total": 3, "sample": {"rule": ""}, "overall": {"ref_chars": 1, "stages": {"merged_default": {"cer": 0, "edits": 0}}}}
            E.write_html(Path(td, "d.html"), S2, [], {}, {"keep1": "이체자"}); self.assertIn('"keep1": "이체자"', Path(td, "d.html").read_text(encoding="utf-8"))

    def test_read_only_skips_broken_record(self):
        """eval 은 읽기 전용: 쓰다 만 기록(p002.json)·깨진 state.json 이 있어도 옮기거나 고치지 않고, 결과 폴더 하위 폴더도 만들지 않는다.
        못 읽는 쪽은 '주의:' 한 줄과 함께 빼고 나머지로 평가한다. 기록 폴더가 없으면 아무것도 만들지 않고 2."""
        with tempfile.TemporaryDirectory() as td:
            w = two_page_work(td); hw = plain_hwpx(os.path.join(td, "ref.hwpx"), [REF[:16], REF[16:]])
            p2 = w.record_path(2); p2.write_bytes(p2.read_bytes()[:200]); w.state_path.write_text("{\"file\": \"b.pd", encoding="utf-8")
            for d in (w.pages_dir, w.crops_dir, w.out_dir):
                d.rmdir()
            snap = lambda: sorted((str(q.relative_to(w.dir)), q.stat().st_size) for q in w.dir.rglob("*") if "eval" not in q.parts)
            before = snap()
            rc, out = run_main(["eval", os.path.join(td, "x.pdf"), "--work", str(w.dir), "--pages", "1-2", "--hwpx", hw]); self.assertEqual(rc, 0, out)
            self.assertIn("주의: 판독 기록 p002.json", out); self.assertEqual(snap(), before)
            S = json.loads((w.dir / "eval" / "summary.json").read_text(encoding="utf-8")); self.assertEqual(S["pages"], [1])
            nowhere = Path(td, "없는_폴더")
            rc, out = run_main(["eval", os.path.join(td, "x.pdf"), "--work", str(nowhere), "--pages", "1-2", "--hwpx", hw])
            self.assertEqual(rc, 2); self.assertIn("오류: 판독 기록이 없습니다", out); self.assertFalse(nowhere.exists())

    def test_sections_with_real_pdf_and_hwpx(self):
        """제목 스타일 HWPX ↔ PDF 책갈피 짝으로 구획마다 한 단위(trim_to_section 으로 잘라), --pages 밖 구획은 건너뛴다. 글자층은 PDF 에서 읽는다."""
        import pymupdf
        from tests.test_collate import make_book
        with tempfile.TemporaryDirectory() as td:
            hw = make_book(os.path.join(td, "b.hwpx")); pdf = os.path.join(td, "a.pdf"); d = pymupdf.open()
            for t in ("大薊大薊根陶云有毒誤也女子赤白沃血熱所致也大薊破血止血甚奇但用於初起之血症大獲奇功", "小薊小薊性涼治吐血衄血", "人參人蔘大補元氣"):
                d.new_page(width=500, height=800).insert_text((450, 60), t, fontname="china-t", fontsize=14, rotate=270)
            d.set_toc([[1, "大薊", 1], [1, "小薊", 2], [1, "人參", 3]]); d.save(pdf); d.close()
            w = Work(pdf, os.path.join(td, "w"))
            w.save_record(1, rec(1, ["大薊", "1. 『本草經疏』：大薊根, 陶云有毒, 誤也. 女子赤白沃, 血熱所致也.", "2. 『本草新編』：大薊, 破血止血甚奇, 但用於初起之血症大獲奇功."]))
            w.save_record(2, rec(2, ["小薊", "小薊, 性凉. 治吐血衄血."])); w.save_record(3, rec(3, ["人參", "人蔘, 大補元氣."]))
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(E.cmd_eval(make_parser().parse_args([pdf, "--work", str(w.dir), "--hwpx", hw, "--mode", "vertical"])), 0)
            S = json.loads((w.dir / "eval" / "summary.json").read_text(encoding="utf-8"))
            self.assertEqual([(u["name"], u["pages"]) for u in S["units"]], [("大薊", [1, 2]), ("小薊", [2, 3]), ("人參", [3])])      # 각론은 제목뿐이라 뺌
            by = {u["name"]: u for u in S["units"]}
            self.assertEqual(by["大薊"]["stages"]["final"]["edits"], 0); self.assertEqual(by["人參"]["stages"]["final"]["edits"], 0)
            self.assertEqual(by["小薊"]["stages"]["final"]["edits"], 1); self.assertEqual(by["小薊"]["ref_chars"], 14)                # 凉 ↔ 涼 (세로 모드는 접지 않음)
            self.assertGreater(by["大薊"]["stages"]["layer"]["hyp_chars"], 0); self.assertEqual(by["大薊"]["trim_notes"], {})         # 글자층도 PDF 에서 읽어 구획으로 자른다
            with contextlib.redirect_stdout(out):
                self.assertEqual(E.cmd_eval(make_parser().parse_args([pdf, "--work", str(w.dir), "--hwpx", hw, "--pages", "1-2", "--out", os.path.join(td, "ev2")])), 0)
            S = json.loads(Path(td, "ev2", "summary.json").read_text(encoding="utf-8"))
            self.assertEqual([u["name"] for u in S["units"]], ["大薊"]); self.assertEqual(len(S["skipped"]), 2); self.assertIn("小薊", S["skipped"][0])
            self.assertTrue(all((w.dir / "readings" / f"p00{n}.json").exists() for n in (1, 2, 3)))                              # 기록은 그대로(읽기 전용)
            self.assertFalse(Path(td, "b_교정.hwpx").exists())


if __name__ == "__main__":
    unittest.main()
