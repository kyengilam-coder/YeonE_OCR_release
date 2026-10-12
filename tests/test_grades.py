"""0.6.4 확인 등급 · 문맥 추정 · 고지 사항만 메모. 글은 한 글자도 바꾸지 않고 글자 모양으로만 보인다(지침 2·7·19·21·22)."""
import contextlib, io, json, os, sys, tempfile, unittest, zipfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from yeone_ocr.schema import PageReading, RegionReread
from yeone_ocr.context import analyze
from yeone_ocr.blocks import assemble_blocks
from yeone_ocr.grades import page_grades, classify_uncertain, page_body, decided_keys, locate_choice, styled_runs, collate_grade, needs_memo, split_marks

FILLER = "此方治諸般虛勞之證其效甚速宜先服之後再議"


def rec(page, cols, adj=None, p1_cols=None, p2_cols=None, kind="본문", unc=None, extra_regions=None, **kw):
    """저장되는 모양(model_dump, 긴 이름) 그대로의 판독 기록."""
    def region(rid, cs, k=kind):
        return {"id": rid, "kind": k, "bbox": [0, 0, 1000, 1000], "paras": [{"cols": [{"n": i + 1, "t": t} for i, t in enumerate(cs)]}]}
    regs = [region("r1", cols)] + [region(r, c, k) for r, c, k in (extra_regions or [])]
    merged = PageReading.model_validate({"pdf_page": page, "regions": regs, "unc": unc or []})
    p1 = PageReading.model_validate({"pdf_page": page, "regions": [region("r1", p1_cols if p1_cols is not None else [c.replace("〔", "").replace("〕", "") for c in cols])]})
    p2 = {"r1": RegionReread.model_validate({"cols": [{"n": i + 1, "t": t} for i, t in enumerate(p2_cols if p2_cols is not None else ["x"])]}).model_dump()}
    r = {"file": "book.pdf", "pdf_page": page, "merged": merged.model_dump(), "pass1": p1.model_dump(), "pass2": p2, "adjudications": adj or []}
    r.update(kw)
    return r


def corpus():
    return [rec(1, ["此藥能入肝經甚效又入肝經" + FILLER, "能入肝經故用之入肝"]), rec(2, ["入肝經入肝經入肝" + FILLER, "曰：此人參甚好也"])]


def adj(choice, a, b, pre, post, mark, window, ci=0, idx=2):
    return {"region_id": "r1", "col_index": ci, "char_index": idx, "n_cols": 2, "n_chars": 40, "a": a, "b": b, "pre": pre, "post": post,
            "mark": mark, "choice": choice, "window": window, "evidence": "획"}


def site(target, books=None):
    res = analyze((books if books is not None else corpus()) + [target], [target])
    return res[target["pdf_page"]]["sites"]


class Context(unittest.TestCase):
    def test_suggest_and_text_unchanged(self):
        t = rec(3, ["其性又能〔入/人〕肝經而走" + FILLER, FILLER * 2])
        before = json.dumps(t, ensure_ascii=False, sort_keys=True)
        s = site(t)[0]
        self.assertEqual((s["verdict"], s["suggest"], s["state"]), ("suggest", "入", "open"))
        self.assertTrue(s["counts"]["入"])
        self.assertEqual(json.dumps(t, ensure_ascii=False, sort_keys=True), before)        # 기록은 그대로(I1)

    def test_thresholds(self):
        one = [rec(1, ["又能入肝" + FILLER, FILLER])]                                     # 入肝 한 번 → 근거 부족
        self.assertEqual(site(rec(3, ["其性〔入/人〕肝而走" + FILLER, FILLER * 2]), one)[0]["verdict"], "neutral")
        both = [rec(1, ["入肝入肝入肝人肝人肝" + FILLER, FILLER])]                         # 두 후보 다 쓰임(4배 미만)
        self.assertEqual(site(rec(3, ["其性〔入/人〕肝而走" + FILLER, FILLER * 2]), both)[0]["verdict"], "neutral")

    def test_exclusions(self):
        reason = lambda cols: site(rec(3, [cols + FILLER, FILLER * 2]))[0]["reason"]
        self.assertIn("숫자", reason("大黃〔二/三〕錢"))
        self.assertIn("干支", reason("甲〔己/巳〕年"))
        self.assertIn("이체자", reason("大黃〔說/説〕之"))
        self.assertIn("한글", reason("大黃〔의/的〕之"))
        self.assertIn("두 글자", reason("大黃〔人參/入參〕之"))
        self.assertIn("자형이 다른 쌍", reason("大黃〔山/水〕之"))
        self.assertIn("가까이에 다른 표시", reason("大〔入/人〕肝〔日/曰〕之"))
        self.assertIn("가까이에 다른 표시", reason("大〔원본 결락〕其性能〔入/人〕肝經而走"))      # 앞의 긴 표시도 두 표시 사이 글자 수로 잰다(앞뒤 같게)
        self.assertIn("가까이에 다른 표시", reason("大其性能〔入/人〕肝經而〔원본 결락〕走"))
        self.assertNotIn("가까이", reason("大〔원본 결락〕其性又能而〔入/人〕肝經"))             # 사이 5자는 가깝지 않음
        noisy = rec(3, ["〔入/人〕肝〔日/曰〕之〔土/士〕也", "又"])
        self.assertTrue(all("표시가 많은 쪽" in x["reason"] or "가까이" in x["reason"] for x in site(noisy)))
        low = rec(3, ["其性〔入/人〕肝而走" + FILLER, FILLER * 2], textlayer_diff={"layer_chars": 100, "ratio": 0.3})
        self.assertIn("글자층", site(low)[0]["reason"])
        self.assertEqual(site(rec(3, ["大〔疑: 入〕肝〔판독불명〕" + FILLER, FILLER * 2])), [])  # 〔疑〕·〔판독불명〕 은 자리를 만들지 않음

    def test_model_origin(self):
        # 1차 모델이 〔人/入〕(자형 人 / 문맥 入) 으로 적었고 다른 대목이 入 을 가리킴 → 다시 밀지 않음
        t = rec(3, ["其性又能〔人/入〕肝經而走" + FILLER, FILLER * 2], p1_cols=["其性又能〔人/入〕肝經而走" + FILLER, FILLER * 2])
        s = site(t)[0]
        self.assertEqual(s["verdict"], "neutral"); self.assertIn("1차 모델", s["reason"])
        t2 = rec(3, ["其性又能〔人/入〕肝經而走" + FILLER, FILLER * 2], p2_cols=["其性又能〔人/入〕肝經"])     # 2차가 쓴 표시도 같음
        self.assertEqual(site(t2)[0]["verdict"], "neutral")
        t3 = rec(3, ["其性又能〔人/入〕肝經而走" + FILLER, FILLER * 2])                              # 모델이 쓴 표시가 아니면 밑줄
        self.assertEqual(site(t3)[0]["verdict"], "suggest")

    def test_corpus_hygiene_leave_one_out(self):
        alone = rec(3, ["其性又能〔入/人〕肝經而走" + FILLER, FILLER * 2])
        self.assertEqual(site(alone, books=[])[0]["verdict"], "neutral")                   # 자기 자리만으로는 근거 없음
        no2 = rec(1, ["入肝經入肝經入肝" + FILLER, FILLER]); no2["pass2"] = {}
        self.assertEqual(site(alone, books=[no2, no2])[0]["verdict"], "neutral")          # 2차 없는 영역은 바탕이 아님
        dec = rec(1, ["其人肝經" + FILLER, FILLER], adj=[adj("人", "人", "入", "其", "肝經", "〔人/入〕", "其人肝", idx=1)] * 1)
        from yeone_ocr.context import settled_segments, Folder
        self.assertFalse(any("人肝" in seg for seg in settled_segments(dec, Folder())))      # 3차가 고른 글자는 바탕에서 뺌

    def test_corpus_skips_long_segment_regions(self):
        """2차가 읽지 못해(또는 크게 달라) 1차만 들어간 긴 구간(merge.LONG_SEGMENT)은 '같게 읽은 글'이 아니므로 바탕에 넣지 않는다."""
        from yeone_ocr.merge import merge_passes
        from yeone_ocr.context import settled_segments, Folder
        c1, c2 = "此方治諸般虛勞之證其效甚速", "又能入肝經故入肝經而入肝經也"
        p1 = PageReading.model_validate({"pdf_page": 1, "regions": [{"id": "r1", "kind": "본문", "bbox": [0, 0, 1000, 1000], "paras": [{"cols": [{"n": 1, "t": c1}, {"n": 2, "t": c2}]}]}]})
        p2 = RegionReread.model_validate({"cols": [{"n": 1, "t": c1}]})                       # 2차 조각이 둘째 열을 놓침
        merged, st = merge_passes(p1, {"r1": p2})
        self.assertEqual(st["long_segments"], 1)
        r = {"file": "b.pdf", "pdf_page": 1, "merged": merged.model_dump(), "pass1": p1.model_dump(), "pass2": {"r1": p2.model_dump()}, "adjudications": []}
        self.assertFalse(any("入肝" in seg for seg in settled_segments(r, Folder())))
        self.assertEqual(site(rec(3, ["其性又能〔入/人〕肝經而走" + FILLER, FILLER * 2]), books=[r, r])[0]["verdict"], "neutral")
        big = rec(1, ["入肝經入肝經入肝" + FILLER, FILLER], unc=[{"r": "r1", "n": 1, "ctx": "1차·2차 판독이 크게 달라 1차를 따름(20자): 「入肝經入肝經入肝」"}])
        self.assertEqual(settled_segments(big, Folder()), [])

    def test_long_segment_notes_shared_with_merge(self):
        """merge 가 남기는 긴 구간 확인 항목 글 머리와 문맥 바탕이 빼는 글 머리가 같은 상수(글이 바뀌어도 어긋나지 않게)."""
        from yeone_ocr.merge import merge_passes, LONG_SEG_NO_P2, LONG_SEG_DIFFER
        from yeone_ocr.context import settled_segments, Folder, LONG_SEG_NOTES
        self.assertEqual(LONG_SEG_NOTES, (LONG_SEG_NO_P2, LONG_SEG_DIFFER))
        c1, c2 = "此方治諸般虛勞之證其效甚速", "又能入肝經故入肝經而入肝經也"
        p1 = PageReading.model_validate({"pdf_page": 1, "regions": [{"id": "r1", "kind": "본문", "bbox": [0, 0, 1000, 1000], "paras": [{"cols": [{"n": 1, "t": c1}, {"n": 2, "t": c2}]}]}]})
        for cols2, head in (([c1], LONG_SEG_NO_P2), ([c1, "甲乙丙丁戊庚辛壬癸子丑寅卯"], LONG_SEG_DIFFER)):   # 2차가 열을 놓침 · 크게 달리 읽음
            p2 = RegionReread.model_validate({"cols": [{"n": i + 1, "t": t} for i, t in enumerate(cols2)]})
            merged, st = merge_passes(p1, {"r1": p2})
            self.assertEqual(st["long_segments"], 1); self.assertTrue(merged.uncertain[-1].context.startswith(head))
            r = {"file": "b.pdf", "pdf_page": 1, "merged": merged.model_dump(), "pass1": p1.model_dump(), "pass2": {"r1": p2.model_dump()}, "adjudications": []}
            self.assertFalse(any("入肝" in seg for seg in settled_segments(r, Folder())))

    def test_contradiction_and_cap(self):
        rows = []
        for k in range(4):
            rows.append("大黃人肝經主之" + FILLER)
        a = [adj("人", "人", "入", "大黃", "肝經", "〔人/入〕", "黃人肝", ci=k) for k in range(4)]
        t = rec(5, rows, adj=a)
        ctx = analyze(corpus() + [t], [t])
        pg = page_grades(t, ctx[5])
        self.assertEqual((pg.stats["blue"], pg.stats["conflict"]), (4, 4))
        n_conf = sum(1 for v in pg.memos.values() for _, m in v if "어긋남" in m)
        self.assertEqual(n_conf, 3); self.assertTrue(any("1곳 더" in x for x in pg.page_notices))
        self.assertIn("大黃人肝經", PageReading.model_validate(t["merged"]).regions[0].paragraphs[0].columns[0].text)

    def test_determinism(self):
        t = rec(3, ["其性又能〔入/人〕肝經而走" + FILLER, FILLER * 2])
        a, b = analyze(corpus() + [t], [t]), analyze(corpus() + [t], [t])
        strip = lambda r: {k: {kk: vv for kk, vv in v.items() if kk != "at"} for k, v in r.items()}
        self.assertEqual(strip(a), strip(b))


class GradesUnit(unittest.TestCase):
    def test_locate_choice(self):
        self.assertEqual(locate_choice("大人肝〔己/已〕甚", 1, "人", "大", "肝"), 1)
        self.assertIsNone(locate_choice("大〔入/人〕肝", 1, "入"))
        self.assertEqual(locate_choice("其甲乙丙人肝", 3, "人", "丙", "肝"), 4)

    def test_locate_choice_prefers_own_site(self):
        """3차가 정한 제자리를 앞세우고, 근처의 같은 글자(1차·2차가 같게 읽은 글자)는 앞뒤 글자가 맞을 때만 잡는다."""
        from dataclasses import asdict
        from yeone_ocr.adjudicate import find_sites, apply_choice
        def run(col, choices):                                                            # 실제 3차 흐름(find_sites → apply_choice)
            m = PageReading.model_validate(rec(1, [col])["merged"]); es = []
            for s, ch in zip(find_sites(m), choices):
                apply_choice(m, s, ch); es.append(asdict(s))
            return locate_choice(m.regions[0].paragraphs[0].columns[0].text, es[-1]["char_index"], choices[-1], es[-1]["pre"], es[-1]["post"])
        self.assertEqual(run("〔己/已〕〔入/人〕肝己入肝" + FILLER, ["已", "入"]), 1)            # 이웃 표시를 b 로 정함 → 3자 뒤 入 이 아니라 제자리
        self.assertEqual(run("〔己入/已〕肝經〔人/入〕人參" + FILLER, ["已", "人"]), 3)          # 앞 표시의 길이 차로 번호가 밀림 → 앞뒤가 맞는 자리
        self.assertIsNone(locate_choice("能入肝經人參" + FILLER, 1, "人", "能", "肝經人參"))      # 사람이 人 → 入 으로 고침: 人參 의 人 을 잡지 않음
        r = rec(4, ["能入肝經人參" + FILLER, FILLER * 2], adj=[adj("人", "人", "入", "能", "肝經人參", "〔人/入〕", "能人肝", idx=1)])
        pg = page_grades(r)
        self.assertEqual((pg.spans, pg.stats["unmapped"]), ({}, 1)); self.assertTrue(any("찾지 못해" in x and "「人」" in x for x in pg.page_notices))

    def test_decided_positions_shift_corrected_own_site(self):
        """제자리 = 판별 때 글자 번호 + 같은 열 앞쪽에서 고른 글자의 길이 차(같은 find_sites 차례). 실제 3차 흐름으로 확인한다."""
        from dataclasses import asdict
        from yeone_ocr.adjudicate import find_sites, apply_choice
        from yeone_ocr.grades import decided_positions
        def run(col, choices):
            r = rec(1, [col]); m = PageReading.model_validate(r["merged"]); es = []
            for s, ch in zip(find_sites(m), choices):
                apply_choice(m, s, ch); es.append({**asdict(s), "choice": ch, "window": s.pre[-1:] + ch + s.post[:1], "evidence": "획"})
            r.update(merged=m.model_dump(), adjudications=es)
            return r, m.regions[0].paragraphs[0].columns[0].text
        r, text = run("〔己入/已〕肝經人〔人/入〕人參" + FILLER, ["已", "人"])                # 已肝經人人人參: 3차 人 은 4(-1 밀림), 5 는 人參 의 人
        self.assertTrue(text.startswith("已肝經人人人參"))
        self.assertEqual([(f.start, f.entry["choice"]) for f in decided_positions(PageReading.model_validate(r["merged"]), r["adjudications"])[0]], [(0, "已"), (4, "人")])
        self.assertEqual(page_grades(r).spans, {("r1", 0): [(0, 1, "green"), (4, 5, "green")]})   # 1차·2차가 같게 읽은 人(5)은 검정
        r, text = run("大〔經/已無〕之也〔於/不〕〔此/肝〕", ["已無", "不", "肝"])               # +1 밀림 · 이웃 표시를 b 로 · 열 끝(뒤 글자 없음)
        self.assertEqual(text, "大已無之也不肝")
        found, missing = decided_positions(PageReading.model_validate(r["merged"]), r["adjudications"])
        self.assertEqual(([(f.start, f.end) for f in found], missing), ([(1, 3), (5, 6), (6, 7)], []))

    def test_decided_positions_across_runs(self):
        """3차를 다시 돌린 쪽(중지·묶음 실패·저해상 뒤 다음 실행): 차례마다 제자리를 고치고, 뒤 차례가 바꾼 길이만큼 앞 차례의 자리를 민다.
        run 을 적은 기록과 run 이 없는 예전 기록(목록 차례로 어림)이 같은 자리를 내고, 다시 적용(apply_adjudications)해도 같은 글이 된다."""
        from dataclasses import asdict
        from yeone_ocr.adjudicate import find_sites, apply_choice, apply_adjudications
        from yeone_ocr.grades import decided_positions, adjudication_runs
        def run(col, runs, stamp):                                                        # runs: 차례마다 {자리 순번: 고른 글자}(없는 자리는 미정)
            r = rec(1, [col]); m = PageReading.model_validate(r["merged"]); es = []
            for n, picks in enumerate(runs, 1):
                for k, s in enumerate(find_sites(m)):
                    ch = picks.get(k, "")
                    if ch:
                        apply_choice(m, s, ch)
                    es.append({**asdict(s), **({"run": n} if stamp else {}), "choice": ch, "window": s.pre[-1:] + ch + s.post[:1], "evidence": "획"})
            r.update(merged=m.model_dump(), adjudications=es)
            text = m.regions[0].paragraphs[0].columns[0].text
            fresh = PageReading.model_validate(rec(1, [col])["merged"]); apply_adjudications(fresh, es)
            self.assertEqual(fresh.regions[0].paragraphs[0].columns[0].text, text)
            found, missing = decided_positions(m, es)
            return text, [(f.start, f.end, f.entry["choice"]) for f in found], missing, r, adjudication_runs(es)
        for stamp in (True, False):
            # 1차례: 已(-1)·已無(+1) — 열 글자 수가 그대로(29)라 글자 수로는 차례를 가를 수 없다. 2차례: 人(글자 번호 4)
            text, found, missing, r, runs = run("〔己入/已〕肝之人〔人/入〕人參〔經/已無〕" + FILLER, [{0: "已", 2: "已無"}, {0: "人"}], stamp)
            self.assertTrue(text.startswith("已肝之人人人參已無")); self.assertEqual(runs, [1, 1, 1, 2])
            self.assertEqual((found, missing), ([(0, 1, "已"), (7, 9, "已無"), (4, 5, "人")], []))       # 3 은 1차·2차가 같게 읽은 人
            self.assertEqual(page_grades(r).spans, {("r1", 0): [(0, 1, "green"), (7, 9, "green"), (4, 5, "green")]})
            # 1차례: 此·諸(글자 번호 5) / 2차례: 治·先之(+1, 諸 앞) → 此治之先之諸: 앞 차례의 諸 는 5 로 밀림(열 끝, 이웃 표시도 바뀜)
            text, found, missing, r, runs = run("〔勞證/此〕〔治/服議〕之〔勞/先之〕〔後速/諸〕", [{0: "此", 3: "諸"}, {0: "治", 1: "先之"}], stamp)
            self.assertEqual(text, "此治之先之諸"); self.assertEqual(runs, [1, 1, 1, 1, 2, 2])
            self.assertEqual((found, missing), ([(0, 1, "此"), (5, 6, "諸"), (1, 2, "治"), (3, 5, "先之")], []))
            self.assertFalse(any("찾지 못해" in x for x in page_grades(r).page_notices))

    def test_note_prefix_is_not_a_candidate_mark(self):
        """〔고지/구자〕 같은 두 글자 한글 후보 표시는 작업 표시(〔고지: …〕)가 아니다."""
        from yeone_ocr.merge import diff_mark
        t = diff_mark("이 땅은 고지라 한다", "이 땅은 구자라 한다")[0]
        self.assertIn(("〔고지/구자〕", "mark"), split_marks(t))
        self.assertEqual(split_marks("〔고지: 범례〕"), [("〔고지: 범례〕", "note")])
        u = PageReading.model_validate({"pdf_page": 1, "regions": [], "unc": [{"r": "r1", "ctx": "「땅은 〔고지/구자〕라」"}]}).uncertain[0]
        self.assertEqual(classify_uncertain(u, t, set()), "shown")

    def test_duplicate_region_ids_are_not_painted(self):
        """한 쪽에 같은 영역 id 가 둘이면 (영역 id, 문단 번호) 로 붙는 등급·문맥 밑줄을 칠하지 않고 쪽 고지로 알린다."""
        from yeone_ocr.context import settled_segments, Folder
        r = rec(3, ["其性又能〔入/人〕肝經而走" + FILLER, FILLER * 2], extra_regions=[("r1", ["大黃一兩主治諸病宜先服之" + FILLER], "본문")])
        ctx = analyze(corpus() + [r], [r])
        self.assertEqual(ctx[3]["sites"], [])
        self.assertEqual([b.spans for b in assemble_blocks([r], context=ctx) if b.type == "para"], [[], []])   # 둘째 r1 의 「治」 가 빨강 안 밑줄로 칠해지지 않음
        self.assertTrue(any("영역 id 겹침" in x for x in page_grades(r, ctx[3]).page_notices))
        r2 = rec(3, ["大黃日一兩" + FILLER, FILLER * 2], adj=[adj("日", "曰", "日", "大黃", "一兩", "〔曰/日〕", "黃日一")], extra_regions=[("r1", ["大黃日一兩" + FILLER], "본문")])
        pg = page_grades(r2)
        self.assertEqual((pg.spans, pg.stats["unmapped"]), ({}, 0))                            # [영역 id 겹침] 이 알리므로 '찾지 못해' 로 다시 세지 않음
        self.assertFalse(any("찾지 못해" in x for x in pg.page_notices))
        self.assertEqual(settled_segments(r2, Folder()), [])                                   # 3차 글자를 가려 뺄 수 없으니 바탕에서도 뺀다
        r3 = rec(3, ["大黃日一兩" + FILLER, FILLER * 2], adj=[adj("日", "曰", "日", "大黃", "一兩", "〔曰/日〕", "黃日一")], extra_regions=[("r1", [], "삽화")])
        pg = page_grades(r3)                                                                   # 삽화와 겹친 id 도 센다(3차 조각 상자를 id 로 찾으므로 삽화에서 잘렸을 수 있음)
        self.assertEqual((pg.spans, pg.stats["unmapped"]), ({}, 0))
        self.assertTrue(any("영역 id 겹침" in x for x in pg.page_notices)); self.assertFalse(any("찾지 못해" in x for x in pg.page_notices))

    def test_green_needs_adjacent_window(self):
        mk = lambda w: rec(4, ["大黃日一兩" + FILLER, FILLER * 2], adj=[adj("日", "曰", "日", "大黃", "一兩", "〔曰/日〕", w)])
        g = lambda r: page_grades(r).stats
        self.assertEqual(g(mk("黃日一"))["green"], 1)
        self.assertEqual(g(mk(""))["blue"], 1)                                             # 창이 비면 위치 확인 못 함 → 파랑
        self.assertEqual(g(mk("黃曰一"))["blue"], 1)                                       # 앞뒤 글자만 보임 → 파랑

    def test_unlocated_choice_is_announced(self):
        r = rec(4, ["大黃一兩" + FILLER, FILLER * 2], adj=[adj("日", "曰", "日", "大黃", "一兩", "〔曰/日〕", "黃日一")])
        pg = page_grades(r)
        self.assertEqual(pg.stats["unmapped"], 1); self.assertTrue(any("찾지 못해" in x for x in pg.page_notices))

    def test_classify_survives_remerge(self):
        """3차가 정한 자리는 '확인 항목' 주석 글이 없어도(비교 다시 계산 뒤) 고지로 보지 않는다."""
        u = PageReading.model_validate({"pdf_page": 1, "regions": [], "unc": [{"r": "r1", "ctx": "「大〔曰/日〕一」"}]}).uncertain[0]
        self.assertEqual(classify_uncertain(u, "大日一", {("r1", "〔曰/日〕")}), "decided")
        self.assertEqual(classify_uncertain(u, "大〔曰/日〕一", set()), "shown")
        self.assertEqual(classify_uncertain(u, "大日一", set()), "notice")

    def test_styled_runs_one_memo_per_char_and_text_kept(self):
        runs = styled_runs("[book / PDF 1쪽]", memos=[(0, "범례"), (0, "고지")])
        self.assertEqual("".join(r[0] for r in runs), "[book / PDF 1쪽]")
        self.assertEqual(runs[0][2], ["범례 / 고지"])

    def test_collate_grade_and_memo(self):
        from yeone_ocr.collate.collate import Item
        A = lambda **k: Item(**{"kind": "A", "action": "replace", "para": 0, "start": 0, "end": 1, "hwpx": "有", "ocr": "无", "new": "無", "auto": True, **k})
        self.assertEqual(collate_grade(A(witness="glyph=ocr")), "green")
        for w in ("", "layer=ocr", "repeat", "ocr"):
            self.assertEqual(collate_grade(A(witness=w)), "blue")
        self.assertEqual(collate_grade(A(kind="B2")), "blue")
        self.assertEqual(collate_grade(A(auto=False, action="mark", new="")), "red")
        self.assertIsNone(collate_grade(A(kind="D")))
        self.assertFalse(needs_memo(A(), "notice")); self.assertTrue(needs_memo(A(), "all"))
        self.assertTrue(needs_memo(A(action="delete", new=""), "notice"))
        self.assertTrue(needs_memo(A(auto=False), "notice"))                               # 반영하지 않은 제안
        self.assertTrue(needs_memo(A(kind="C", action="insert", start=0, end=0), "notice"))
        self.assertTrue(needs_memo(A(kind="B1", action="mark", auto=False, new="", start=3, end=3), "notice"))   # 폭 없는 표시
        self.assertFalse(needs_memo(A(kind="B1", action="mark", auto=False, new=""), "notice"))
        self.assertTrue(needs_memo(A(kind="B1", action="mark", auto=False, new="", notice=True), "notice"))

    def test_structural_rules_set_notice(self):
        from yeone_ocr.collate.collate import Item, apply_structural_rules
        it = Item("A", "replace", 0, 0, 2, "인삼", "人參", "人參", hw_ctx="「인삼」")
        apply_structural_rules([it]); self.assertTrue(it.notice)


class DecidedReplay(unittest.TestCase):
    """0.6.8 — decided_positions 는 3차 기록을 차례대로 다시 놓아(replay) 이웃이 바뀐 앞뒤 글자로 견준다. 기록한 pre/post 는 판별 때(이웃 표시를 1차 글자로
    접은) 글이라, 같은 차례에서 이웃 표시가 다른 후보로 정해지면 낡는다 — 〔於/不〕〔於/不〕〔於/不〕 → 於·不·不 의 셋째 기록이 둘째 자리에 칠해졌다."""
    MARKS = [("於", "不"), ("於", "不"), ("己入", "已"), ("經", "已無"), ("人", "入"), ("之", "也之")]
    ALPHA = "於不已己入人之也經"

    def _flow(self, cols, runs):
        """실제 3차 흐름(find_sites → apply_choice(done=…), 차례마다 run 을 적음)과, 같은 흐름을 표지로 따라간 참 자리.
        runs: 차례마다 고르는 함수 f(자리) → 고른 글자(빈 글 = 미정). 돌려주는 값: (병합문, 기록, {기록 번호: (문단 글 시작, 끝)})."""
        import re
        from dataclasses import asdict
        from yeone_ocr.adjudicate import find_sites, apply_choice
        m = PageReading.model_validate(rec(1, cols)["merged"])
        toks = []
        for col in cols:                                                    # 열마다 [글자 | [1차, 2차, None 또는 (기록 번호, 고른 글자)]]
            t = []; last = 0
            for mm in re.finditer(r"〔([^〔〕/]{1,2})/([^〔〕]{1,2})〕", col):
                t.extend(col[last:mm.start()]); t.append([mm.group(1), mm.group(2), None]); last = mm.end()
            toks.append(t + list(col[last:]))
        render = lambda t: "".join(x if isinstance(x, str) else (f"〔{x[0]}/{x[1]}〕" if x[2] is None else x[2][1]) for x in t)
        es: list[dict] = []
        for n, pick in enumerate(runs, 1):
            pending = [[x for x in t if not isinstance(x, str) and x[2] is None] for t in toks]; done: dict = {}
            for s in find_sites(m):
                x = pending[s.col_index].pop(0); self.assertEqual((x[0], x[1]), (s.a, s.b))
                ch = pick(s)
                if ch:
                    self.assertTrue(apply_choice(m, s, ch, done=done.get(s.col_index, [])))
                    done.setdefault(s.col_index, []).append((s.char_index, s.a, ch)); x[2] = (len(es), ch)
                es.append({**asdict(s), "run": n, "choice": ch, "window": "", "evidence": "획"})
            self.assertEqual([c.text for c in m.regions[0].paragraphs[0].columns], [render(t) for t in toks])   # 표지 흐름 = 실제 흐름
        truth = {}; off = 0
        for t in toks:
            at = off
            for x in t:
                w = len(x) if isinstance(x, str) else len(f"〔{x[0]}/{x[1]}〕" if x[2] is None else x[2][1])
                if not isinstance(x, str) and x[2] is not None:
                    truth[x[2][0]] = (at, at + len(x[2][1]))
                at += w
            off = at
        return m, es, truth

    def _check(self, cols, runs):
        from yeone_ocr.grades import decided_positions
        from yeone_ocr.evaluate import _revert_adjudications, SENT_RE
        m, es, truth = self._flow(cols, runs)
        found, missing = decided_positions(m, es)
        ids = {id(e): k for k, e in enumerate(es)}
        self.assertEqual(({ids[id(f.entry)]: (f.start, f.end) for f in found}, missing), (truth, []), (cols, [(e["run"], e["choice"]) for e in es]))
        back, lost = _revert_adjudications(m, es, 0)                          # 3차 전 글로 되돌리기(eval)도 같은 자리를 쓴다
        self.assertEqual([SENT_RE.sub("", c.text) for c in back.regions[0].paragraphs[0].columns], cols)
        if len(runs) == 1:
            self.assertEqual(lost, 0)
        return found

    def test_repeated_marks_each_on_own_site(self):
        picks = iter("於不不")
        found = self._check(["〔於/不〕〔於/不〕〔於/不〕"], [lambda s: next(picks)])
        self.assertEqual([(f.start, f.end, f.entry["choice"]) for f in found], [(0, 1, "於"), (1, 2, "不"), (2, 3, "不")])
        r = rec(1, ["〔於/不〕〔於/不〕〔於/不〕" + FILLER]); m, es, _ = self._flow(["〔於/不〕〔於/不〕〔於/不〕" + FILLER], [lambda s, p=iter("於不不"): next(p)])
        for e in es:
            e["window"] = (e["pre"][-1:] or "") + e["choice"] + (e["post"][:1] or "")
        r.update(merged=m.model_dump(), adjudications=es)
        self.assertEqual(page_grades(r).spans, {("r1", 0): [(0, 1, "green"), (1, 2, "green"), (2, 3, "green")]})   # 색이 제 글자에
        picks = iter(["不", "", "不", "於"])                                                     # 둘째 차례: 미정이던 가운데 표시를 於 로
        self._check(["〔於/不〕〔於/不〕〔於/不〕"], [lambda s: next(picks), lambda s: next(picks)])
        picks = iter(["已", "已無", "也之", "不"])                                                # 길이가 바뀌는 이웃
        self._check(["〔己入/已〕〔經/已無〕〔之/也之〕〔於/不〕"], [lambda s: next(picks)])

    def test_fuzz_repeated_marks_and_length_changes(self):
        """같은 표시가 되풀이되는 열·글자 수가 바뀌는 후보·여러 차례·두 열에서, 확정 기록마다 찾은 자리가 참 자리와 같다."""
        import random
        rng = random.Random(20261011)
        for _ in range(400):
            cols = []
            for _ in range(rng.randint(1, 2)):
                if rng.random() < 0.3:
                    a, b = rng.choice(self.MARKS); cols.append("".join(f"〔{a}/{b}〕" + (rng.choice(self.ALPHA) if rng.random() < 0.3 else "") for _ in range(rng.randint(2, 5))))
                else:
                    cols.append("".join((lambda ab: f"〔{ab[0]}/{ab[1]}〕")(rng.choice(self.MARKS)) if rng.random() < 0.45 else rng.choice(self.ALPHA)
                                        for _ in range(rng.randint(2, 9))))
            pick = lambda s: rng.choice([s.a, s.b, s.b, ""])
            self._check(cols, [pick] * rng.randint(1, 3))


class Documents(unittest.TestCase):
    def _blocks(self, r, **kw):
        ctx = analyze(corpus() + [r], [r])
        return assemble_blocks([r], context=ctx, **kw)

    def test_hwpx_styles_text_and_memos(self):
        from contextlib import closing
        from yeone_ocr.hwpx_builder import build_hwpx
        from yeone_ocr.cli import check_hwpx_file
        from hwpx import HwpxDocument
        r = rec(3, ["大黃日一兩又能〔入/人〕肝經主之" + FILLER, FILLER * 2], adj=[adj("日", "曰", "日", "大黃", "一兩", "〔曰/日〕", "黃日一")],
                extra_regions=[("r2", ["小注〔入/人〕肝經之" + FILLER], "소주")])
        r["pass2"]["r2"] = r["pass2"]["r1"]
        with tempfile.TemporaryDirectory() as td:
            out = os.path.join(td, "g.hwpx")
            st = build_hwpx(self._blocks(r), out)
            self.assertEqual((st["green"], st["red_ctx"], st["marks"]), (1, 2, 2))
            with zipfile.ZipFile(out) as z:
                x = z.read("Contents/section0.xml").decode(); h = z.read("Contents/header.xml").decode()
            self.assertIn('textColor="#0B7A3B"', h); self.assertIn('textColor="#C00000"', h)
            self.assertIn('type="BOTTOM" shape="DOT" color="#0B7A3B"', h); self.assertIn('type="BOTTOM" shape="SOLID" color="#C00000"', h)
            self.assertEqual(x.count('type="MEMO"'), 1); self.assertEqual(x.count("[연어OCR 표시 범례"), 1)
            self.assertEqual(check_hwpx_file(out), [])
            with closing(HwpxDocument.open(out)) as opened:
                texts = [p.text for p in opened.paragraphs]
            self.assertTrue(any("大黃日一兩又能〔入/人〕肝經主之" in t for t in texts))      # 괄호·두 후보·글 그대로(I1, I4)
            self.assertFalse(any("범례" in t for t in texts))                                   # 메모 글은 본문에 없음
            self.assertIn('height="950"', h)                                                    # 소주(9.5pt) 등급 글자
            # 색 끄기: 등급 모양·범례 없음, 글은 같음
            out2 = os.path.join(td, "plain.hwpx")
            st2 = build_hwpx(self._blocks(r, legend=False), out2, highlight=False)
            self.assertEqual((st2["green"], st2["red_ctx"]), (0, 0))
            with zipfile.ZipFile(out2) as z:
                x2 = z.read("Contents/section0.xml").decode(); h2 = z.read("Contents/header.xml").decode()
            self.assertNotIn('textColor="#0B7A3B"', h2); self.assertNotIn("범례", x2)
            with closing(HwpxDocument.open(out2)) as opened:
                self.assertEqual([p.text for p in opened.paragraphs], texts)

    def test_text_invariance_all_modes(self):
        """어떤 선택(등급·문맥·메모 방식·색)에서도 문단 글은 예전과 같다(글 속 〔고지〕 만 예외)."""
        r = rec(3, ["  大黃日一兩又能〔入/人〕肝經主之" + FILLER, FILLER * 2], adj=[adj("日", "曰", "日", "大黃", "一兩", "〔曰/日〕", "黃日一")])
        r["verify"] = [{"level": "error", "rule": "열 순서", "where": "r1", "msg": "겹침"}]
        legacy = [b.text for b in assemble_blocks([r], grades=False)]
        for grades in (True, False):
            for ctx in (True, False):
                for mode in ("notice", "all", "inline"):
                    bl = assemble_blocks([r], grades=grades, context=analyze(corpus() + [r], [r]) if ctx else None, memo_mode=mode)
                    self.assertEqual([b.text for b in bl], legacy)
                    for b in bl:
                        self.assertEqual("".join(x[0] for x in styled_runs(b.text, b.spans, b.memos)), b.text)

    def test_notices_and_inline(self):
        from yeone_ocr.hwpx_builder import build_hwpx
        r = rec(3, ["大黃一兩" + FILLER, FILLER * 2], unc=[{"r": "r1", "n": 2, "pos": "아래", "ctx": "2차 확대 판독이 이 구간을 읽지 못해 1차만 들어감"}])
        r["verify"] = [{"level": "error", "rule": "열 순서", "where": "r1", "msg": "열 번호가 겹침"}]
        pg = page_grades(r)
        self.assertTrue(any("오른쪽에서 2번째 열" in x for x in pg.page_notices))
        self.assertTrue(any("[구조 검사 오류]" in x for x in pg.page_notices))
        with tempfile.TemporaryDirectory() as td:
            out = os.path.join(td, "n.hwpx"); build_hwpx(assemble_blocks([r]), out)
            x = zipfile.ZipFile(out).read("Contents/section0.xml").decode()
            self.assertEqual(x.count('type="MEMO"'), 2)                                         # 범례 + 쪽 고지(하나로) — 겹친 메모 없음
            out3 = os.path.join(td, "i.hwpx"); build_hwpx(assemble_blocks([r], memo_mode="inline"), out3, memos=False)
            x3 = zipfile.ZipFile(out3).read("Contents/section0.xml").decode()
            self.assertEqual(x3.count('type="MEMO"'), 0); self.assertIn("[구조 검사 오류]", x3); self.assertIn("〔고지: [본문에 표시되지 않은 확인 항목]", x3); self.assertIn("〔고지: [연어OCR 표시 범례", x3)

    def test_legend_lists_pages_without_pass2(self):
        a = rec(3, ["大黃一兩" + FILLER, FILLER]); a["pass2"] = {}
        b = rec(4, ["大黃一兩" + FILLER, FILLER]); b["pass2"] = {}
        meta = next(x for x in assemble_blocks([a, b]) if x.type == "meta")
        self.assertIn("1차만 들어간 쪽: 3–4", meta.memos[0][1])

    def test_docx_comments_and_fallback(self):
        from yeone_ocr import docx_builder
        r = rec(3, ["大黃〔入/人〕肝經主之" + FILLER, FILLER * 2]); r["verify"] = [{"level": "error", "rule": "x", "where": "r1", "msg": "y"}]
        with tempfile.TemporaryDirectory() as td:
            out = os.path.join(td, "g.docx"); st = docx_builder.build_docx(assemble_blocks([r]), out)
            n = zipfile.ZipFile(out).read("word/comments.xml").decode().count("<w:comment ")
            self.assertEqual((st["memos"], n), (2, 2))
            st2 = docx_builder.build_docx(assemble_blocks([r]), os.path.join(td, "i.docx"), memos=False)
            self.assertEqual(st2["memos"], 0)
            doc = zipfile.ZipFile(os.path.join(td, "i.docx")).read("word/document.xml").decode()
            self.assertIn("〔고지:", doc)


class Cli(unittest.TestCase):
    def test_parser_options(self):
        from yeone_ocr import cli
        ap = cli.build_parser()
        a = ap.parse_args(["build", "x.pdf", "--memo", "inline", "--no-context"]); self.assertEqual((a.memo, a.no_context), ("inline", True))
        self.assertEqual(ap.parse_args(["run", "x.pdf"]).memo, "notice")
        from yeone_ocr.collate.run import memo_mode
        self.assertEqual(memo_mode(ap.parse_args(["collate", "x.pdf", "--hwpx", "b.hwpx", "--no-memo"])), "inline")
        self.assertEqual(memo_mode(ap.parse_args(["collate", "x.pdf", "--hwpx", "b.hwpx"])), "notice")
        from yeone_ocr.collate.labels import MEMO_MODES
        self.assertEqual(set(MEMO_MODES), {"notice", "all", "inline"})

    def test_run_writes_context_json_not_records(self):
        from yeone_ocr import cli
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from test_auto import vertical_pdf
        with tempfile.TemporaryDirectory() as td:
            doc = vertical_pdf(2); pdf = os.path.join(td, "v.pdf"); doc.save(pdf); doc.close()
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                self.assertEqual(cli.main(["run", pdf, "--work", os.path.join(td, "w"), "--reader", "mock", "--checkpoint", "0"]), 0)
            self.assertTrue(os.path.exists(os.path.join(td, "w", "context.json")))
            r = json.load(open(os.path.join(td, "w", "readings", "p001.json"), encoding="utf-8"))
            self.assertNotIn("context", r)
            self.assertIn("등급: 초록", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
