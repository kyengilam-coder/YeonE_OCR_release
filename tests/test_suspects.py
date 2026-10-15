"""0.6.8 — 합의 판독 의심(단계 3 신호 ①): 1차·2차가 같게 읽었는데 PDF 글자층과 자형 혼동 쌍 한 글자가 다른 자리를 3차처럼 묻고,
자형이 글자층 쪽이면 본문은 그대로 두고 확인 항목만 단다(지침 20·21). 모델·API 키 없이 가짜 판독기로.
실행: python -m unittest discover -s tests   (저장소 루트에서)"""
import contextlib, io, json, os, sys, tempfile, unittest, zipfile
from argparse import Namespace
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pymupdf
from PIL import Image
from yeone_ocr.schema import PageReading, RegionReread, GlyphChoice, Uncertain, paragraph_text
from yeone_ocr.adjudicate import (find_suspects, suspect_page, suspects_pending, suspect_uncertain, suspect_notices, suspect_freq, candidate_order,
                                  confusable_pair, variant_pair, SUSPECT_NOTE, SUSPECT_SOURCE)
from yeone_ocr.reader_gemini import ReaderError
from yeone_ocr.state import Work

FILLER = "此方治諸般虛勞之證其效甚速宜先服之後再議"
COL1 = "能入肝經" + FILLER                 # 1차·2차 모두 入, 글자층 人
COL2 = "大黃日一兩又曰主之" + FILLER       # 1차·2차 모두 曰, 글자층 日
LAYER = COL1.replace("能入", "能人") + "\n" + COL2.replace("又曰", "又日")
IMG = Image.new("L", (900, 1300), 255)


def rec(cols, page=1, layer_cols=None, adjudications=None, pass2=True, unc=None):
    """저장되는 모양 그대로의 판독 기록(영역 r1 하나, 문단 하나). pass2 가 있으면 표시 밖 글자는 1차·2차 합의."""
    def region(cs):
        return {"id": "r1", "kind": "본문", "bbox": [100, 50, 900, 950], "paras": [{"cols": [{"n": i + 1, "t": t} for i, t in enumerate(cs)]}]}
    merged = PageReading.model_validate({"pdf_page": page, "regions": [region(cols)], "unc": unc or []})
    clean = [c for c in cols]
    p1 = PageReading.model_validate({"pdf_page": page, "regions": [region(clean)]})
    r = {"file": "book.pdf", "pdf_page": page, "merged": merged.model_dump(), "pass1": p1.model_dump(), "adjudications": adjudications or []}
    if pass2:
        r["pass2"] = {"r1": RegionReread.model_validate({"cols": [{"n": i + 1, "t": t} for i, t in enumerate(clean)]}).model_dump()}
    return r


def body(r):
    return [paragraph_text(p) for reg in PageReading.model_validate(r["merged"]).regions for p in reg.paragraphs]


class Reader:
    """가짜 3차 판독기: 후보 묶음(섞여 오므로 집합으로)마다 정해 둔 (고른 글자, 보인 글)을 낸다. 앞뒤 글자는 받지 못한다(눈가림)."""
    model = "fake"

    def __init__(self, answers, blind=None):
        self.answers = answers; self.blind = blind; self.batches = []; self.single = []; self.identified = []

    def _ans(self, ask):
        c, w = self.answers.get(frozenset(ask.candidates), ("", ""))
        return GlyphChoice(choice=c, evidence="획", window=w)

    def adjudicate_many(self, crops, horizontal, pdf_page):
        self.batches.append([a for _, _, a in crops]); return {sid: self._ans(a) for sid, _, a in crops}

    def adjudicate(self, crop, ask, horizontal, pdf_page):
        self.single.append(ask); return self._ans(ask)


ANS = {frozenset("入人"): ("人", "能人肝"), frozenset("曰日"): ("曰", "又曰主")}


class Never:
    model = "never"

    def adjudicate_many(self, *a, **k): raise AssertionError("이미 물은 자리를 다시 물음")

    def adjudicate(self, *a, **k): raise AssertionError("이미 물은 자리를 다시 물음")


class Find(unittest.TestCase):
    def test_pairs_and_positions(self):
        m = PageReading.model_validate(rec([COL1, COL2])["merged"])
        sites, info = find_suspects(m, LAYER)
        self.assertEqual([(s.a, s.b, s.col_index, s.char_index, s.pre, s.post, s.mark) for s in sites],
                         [("入", "人", 0, 1, "能", "肝經此方", ""), ("曰", "日", 1, 6, "日一兩又", "主之此方", "")])
        self.assertGreater(info["ratio"], 0.9)
        self.assertEqual(m.regions[0].paragraphs[0].columns[1].text[6], "曰")              # 글자 번호는 열 글 기준

    def test_exclusions(self):
        """표시 안·표시 곁·숫자·이체자·혼동 쌍 아님·두 글자 치환·간번체는 묻지 않는다. 번체로 읽은 問 ↔ 간체 间 은 묻는다(compare_key 로 접어 묶음에 듦)."""
        def found(col, layer, **kw):
            return [(s.a, s.b) for s in find_suspects(PageReading.model_validate(rec([col])["merged"]), layer, **kw)[0]]
        self.assertEqual(found("能〔己/已〕入肝" + FILLER, "能已人肝" + FILLER), [])               # 표시 안(己/已)과 표시 바로 곁(入)
        self.assertEqual(found("能〔己/已〕之入肝" + FILLER, "能已之人肝" + FILLER), [("入", "人")])  # 한 자 떨어지면 묻는다
        self.assertEqual(found("用千金" + FILLER, "用干金" + FILLER), [])                         # 숫자(干于千 묶음이어도)
        self.assertEqual(found("大黃一兩" + FILLER, "大黄一兩" + FILLER), [])                     # 이체자(黃/黄)
        self.assertEqual(found("薏苡仁" + FILLER, "薏苢仁" + FILLER), [])                         # 이체자(苡/苢, 자형 단서 묶음이어도)
        self.assertEqual(found("天地人" + FILLER, "天池人" + FILLER), [])                         # 혼동 묶음이 아님
        self.assertEqual(found("能入肝經" + FILLER, "能人汗經" + FILLER), [])                     # 1:1 이 아님(두 글자 치환)
        self.assertEqual(found("其後" + FILLER, "其后" + FILLER), [])                             # 간체·번체
        self.assertEqual(found("問診" + FILLER, "间診" + FILLER), [("問", "间")])
        self.assertEqual(found("가入나" + FILLER, "가人나" + FILLER), [("入", "人")])
        self.assertEqual(found("能가肝" + FILLER, "能나肝" + FILLER), [])                         # 한글
        self.assertTrue(confusable_pair("問", "間") and not confusable_pair("後", "后") and variant_pair("黃", "黄") and variant_pair("塚", "塚"))

    def test_page_and_region_guards(self):
        """글자층 일치율이 낮은 쪽(지침 28)·2차 판독이 없는 영역·3차가 정한 글자와 그 곁은 묻지 않는다."""
        m = PageReading.model_validate(rec([COL1, COL2])["merged"])
        sites, info = find_suspects(m, "完全不同之文字" * 3)
        self.assertEqual((sites, info.get("low_ratio")), ([], True))
        self.assertEqual(find_suspects(m, "")[0], [])
        self.assertEqual(find_suspects(m, LAYER, regions=set())[0], [])
        self.assertEqual(len(find_suspects(m, LAYER, regions={"r1"})[0]), 2)
        adj = [{"region_id": "r1", "col_index": 0, "char_index": 1, "n_cols": 2, "n_chars": len(COL1), "a": "入", "b": "人", "pre": "能", "post": "肝經此方",
                "mark": "〔入/人〕", "choice": "入", "run": 1}]
        self.assertEqual([(s.a, s.b) for s in find_suspects(m, LAYER, adjudications=adj)[0]], [("曰", "日")])
        adj[0].update(char_index=0, a="能", b="熊", mark="〔能/熊〕", choice="能", pre="", post="入肝經此")      # 3차가 정한 글자의 곁
        self.assertEqual([(s.a, s.b) for s in find_suspects(m, LAYER, adjudications=adj)[0]], [("曰", "日")])
        r = rec([COL1, COL2], pass2=False)
        self.assertEqual(suspects_pending(r, LAYER), 0)                                         # 2차가 없으면 합의가 아니다

    def test_printed_forms_not_folded_keys(self):
        """간체로만 닮은 번체 쌍(於/子 ← 于子, 無/元 ← 无元 …)은 compare_key 로 접혀도 묻지 않는다 — 인쇄된 꼴이 한 묶음에 들어야 한다."""
        for a, b in ("於子", "無元", "幣市", "滅火", "屬届", "於干", "膿脹", "囉噦"):
            self.assertFalse(confusable_pair(a, b) or confusable_pair(b, a), a + b)
            m = PageReading.model_validate(rec(["能" + a + "肝經" + FILLER])["merged"])
            self.assertEqual(find_suspects(m, "能" + b + "肝經" + FILLER)[0], [], a + b)
        for a, b in ("入人", "曰日", "問間", "問间", "问間", "于干", "于子", "烏鳥", "溫混"):  # 같은 꼴 묶음 · 번체 묶음 · 간체 글자층(间 = 인쇄된 間) · 이체자(溫=温)
            self.assertTrue(confusable_pair(a, b) and confusable_pair(b, a), a + b)
        m = PageReading.model_validate(rec(["能問肝經" + FILLER])["merged"])
        self.assertEqual([(s.a, s.b) for s in find_suspects(m, "能間肝經" + FILLER)[0]], [("問", "間")])

    def test_one_sided_long_segment_is_not_consensus(self):
        """1차만 들어간 긴 구간(2차가 못 읽음)은 합의가 아니다 — '1차·2차 「入」' 알림·합의↔자형 장부가 틀린 말이 되지 않게 그 영역은 보지 않는다."""
        from yeone_ocr.merge import merge_passes
        head, tail = "此方治諸般虛勞之證其效甚速", "宜先服之後再議能入肝經而補其虛也"
        p1 = PageReading.model_validate({"pdf_page": 1, "regions": [{"id": "r1", "kind": "본문", "bbox": [100, 50, 900, 950],
                                                                     "paras": [{"cols": [{"n": 1, "t": head}, {"n": 2, "t": tail}]}]}]})
        def page(cols):
            rr = {"r1": RegionReread.model_validate({"cols": [{"n": i + 1, "t": t} for i, t in enumerate(cols)]})}
            return {"pdf_page": 1, "merged": merge_passes(p1, rr)[0].model_dump(), "pass1": p1.model_dump(),
                    "pass2": {k: v.model_dump() for k, v in rr.items()}, "adjudications": []}
        layer = head + "\n" + tail.replace("能入", "能人")
        r = page([head])                                                                         # 2차가 둘째 열을 못 읽음
        self.assertEqual(suspects_pending(r, layer), 0)
        self.assertEqual(suspect_page(r, IMG, Never(), False, layer)["suspects"], [])
        self.assertEqual(suspects_pending(page([head, tail]), layer), 1)                        # 2차도 같게 읽었으면 묻는다

    def test_ledger_frequency_orders(self):
        m = PageReading.model_validate(rec([COL1, COL2])["merged"])
        self.assertEqual([s.a for s in find_suspects(m, LAYER, freq={frozenset("日曰"): 5, frozenset("入人"): 1})[0]], ["曰", "入"])
        self.assertEqual([s.a for s in find_suspects(m, LAYER, freq={})[0]], ["入", "曰"])


class Page(unittest.TestCase):
    def test_outcomes_body_unchanged_and_notice(self):
        r = rec([COL1, COL2]); before = body(r); rd = Reader(ANS)
        r = suspect_page(r, IMG, rd, False, LAYER)
        self.assertEqual(body(r), before)                                                        # 지침 20: 글자층은 본문을 바꾸지 않는다
        self.assertEqual(len(rd.batches), 1); asks = rd.batches[0]
        self.assertEqual([a.candidates for a in asks], [candidate_order(["入", "人"], 1, "r1", 0, 1), candidate_order(["曰", "日"], 1, "r1", 1, 6)])
        self.assertFalse(any(hasattr(a, "pre") for a in asks))
        sus = r["suspects"]
        self.assertEqual([(e["consensus"], e["layer"], e["choice"], e["outcome"]) for e in sus], [("入", "人", "人", "differs"), ("曰", "日", "曰", "confirmed")])
        for k in ("region_id", "col", "char_index", "pre", "post", "consensus", "layer", "choice", "evidence", "model", "outcome", "asked_at"):
            self.assertIn(k, sus[0])
        self.assertEqual((sus[0]["region_id"], sus[0]["col"], sus[0]["char_index"], sus[0]["model"]), ("r1", 0, 1, "fake"))
        st = r["suspect_stats"]
        self.assertEqual({k: st[k] for k in ("candidates", "asked", "differs", "confirmed", "undecided", "failed")},
                         {"candidates": 2, "asked": 2, "differs": 1, "confirmed": 1, "undecided": 0, "failed": 0})
        unc = PageReading.model_validate(r["merged"]).uncertain
        self.assertEqual(len(unc), 1); u = unc[0]
        self.assertTrue(u.context.startswith(SUSPECT_NOTE)); self.assertIn("1차·2차 「入」", u.context); self.assertIn("자형 판별 「人」(글자층도 「人」)", u.context)
        self.assertEqual((u.region_id, u.col_from_right, u.position), ("r1", 1, "위"))
        self.assertEqual(suspect_notices(r), [u]); self.assertEqual(suspect_uncertain(sus[0]), u)

    def test_idempotent_force_and_cap(self):
        r = suspect_page(rec([COL1, COL2]), IMG, Reader(ANS), False, LAYER)
        again = suspect_page(json.loads(json.dumps(r)), IMG, Never(), False, LAYER)            # 같은 자리·같은 두 글자는 다시 묻지 않는다
        self.assertEqual((again["suspects"], again["merged"]["uncertain"]), (r["suspects"], r["merged"]["uncertain"]))
        self.assertEqual((again["suspect_stats"]["asked"], again["suspect_stats"]["already"]), (0, 2))
        self.assertEqual(suspects_pending(again, LAYER), 0)
        forced = suspect_page(again, IMG, Reader({frozenset("入人"): ("入", "能入肝"), frozenset("曰日"): ("曰", "又曰主")}), False, LAYER, force=True)
        self.assertEqual([e["outcome"] for e in forced["suspects"]], ["confirmed", "confirmed"])  # 앞 기록을 바꾸고
        self.assertEqual(forced["merged"]["uncertain"], [])                                     # 그 확인 항목도 지운다
        r = suspect_page(rec([COL1, COL2]), IMG, Reader(ANS), False, LAYER, max_n=1)
        self.assertEqual([e["consensus"] for e in r["suspects"]], ["入"])
        r = suspect_page(r, IMG, Never(), False, LAYER, max_n=1)                                # 상한에는 이미 물은 자리도 센다
        r = suspect_page(r, IMG, Reader(ANS), False, LAYER, max_n=2)
        self.assertEqual([e["consensus"] for e in r["suspects"]], ["入", "曰"])
        self.assertEqual(suspect_page(rec([COL1, COL2]), IMG, Never(), False, LAYER, max_n=0)["suspects"], [])

    def test_reask_after_nearby_mark_changes_length(self):
        """가까운 앞 〔己入/已〕 가 뒤 실행에서 정해져 글자 번호·앞 글자가 바뀌어도 같은 자리로 본다(다시 사지 않고 기록·알림·메모를 겹치지 않는다)."""
        from yeone_ocr.adjudicate import adjudicate_page
        from yeone_ocr.grades import page_grades
        col, layer = "能〔己入/已〕之入肝經" + FILLER, "能已之人肝經" + FILLER
        class Rd(Reader):
            fail = True
            def _ans(self, ask):
                if set(ask.candidates) == {"己入", "已"}:
                    if self.fail: raise ReaderError("일시 오류 503", fatal=False)
                    return GlyphChoice(choice="已", evidence="획", window="能已之")
                return GlyphChoice(choice="人", evidence="획", window="之人肝")
        r = adjudicate_page(rec([col]), IMG, Rd({}), False, batch=1)                          # 1회: 표시 판별 실패, 합의 의심은 물음
        r = suspect_page(r, IMG, Rd({}), False, layer, batch=1)
        self.assertEqual([(e["char_index"], e["outcome"]) for e in r["suspects"]], [(4, "differs")])
        rd = Rd({}); rd.fail = False
        r = adjudicate_page(r, IMG, rd, False, batch=1)                                        # 2회: 표시가 已 로 정해짐(두 자 → 한 자)
        self.assertEqual(body(r), ["能已之入肝經" + FILLER])
        self.assertEqual(suspects_pending(r, layer), 0)
        r = suspect_page(r, IMG, Never(), False, layer, batch=1)
        self.assertEqual((len(r["suspects"]), r["suspect_stats"]["already"]), (1, 1))
        self.assertEqual(sum(u["context"].startswith(SUSPECT_NOTE) for u in r["merged"]["uncertain"]), 1)
        self.assertEqual(len(page_grades(r).memos[("r1", 0)]), 1)

    def test_forced_reask_failure_keeps_answer(self):
        """--force-suspects 로 다시 물었는데 호출이 실패하면(503) 앞 답(differs)과 그 확인 항목을 지우지 않는다."""
        from yeone_ocr.grades import page_grades
        r = suspect_page(rec([COL1, COL2]), IMG, Reader(ANS), False, LAYER)
        class Down(Reader):
            def adjudicate_many(self, *a, **k): raise ReaderError("503 일시 오류", fatal=False)
        r = suspect_page(r, IMG, Down(ANS), False, LAYER, force=True)
        self.assertEqual([(e["consensus"], e["outcome"]) for e in r["suspects"]], [("入", "differs"), ("曰", "confirmed")])
        self.assertEqual(r["suspect_stats"]["failed"], 2)
        self.assertEqual(sum(u["context"].startswith(SUSPECT_NOTE) for u in r["merged"]["uncertain"]), 1)
        self.assertIn(("r1", 0), page_grades(r).memos)
        r = suspect_page(r, IMG, Reader({frozenset("入人"): ("入", "能入肝"), frozenset("曰日"): ("曰", "又曰主")}), False, LAYER, force=True)
        self.assertEqual([e["outcome"] for e in r["suspects"]], ["confirmed", "confirmed"])  # 답이 오면 바꾼다
        self.assertEqual(r["merged"]["uncertain"], [])

    def test_cap_is_per_page_total_whatever_the_order(self):
        """장부 빈도로 차례가 바뀌어도 쪽당 상한(이미 물은 자리 포함)을 넘겨 새로 묻지 않는다."""
        from yeone_ocr.adjudicate import _pair_key
        pairs = ["入人", "曰日", "己已", "未末", "土士", "白自", "天夭", "木本", "玉王", "今令", "大太", "刀力"]
        fil = "此方治諸般虛勞之證其效"
        reading = "".join(fil[i % len(fil)] + fil[(i + 3) % len(fil)] + p[0] for i, p in enumerate(pairs)) + fil
        layer = "".join(fil[i % len(fil)] + fil[(i + 3) % len(fil)] + p[1] for i, p in enumerate(pairs)) + fil
        r = suspect_page(rec([reading]), IMG, Reader({}), False, layer, batch=1)
        self.assertEqual(len(r["suspects"]), 8)
        freq = {_pair_key(p[0], p[1]): 5 for p in pairs[8:]}                                  # 뒤 실행: 장부가 다른 쌍을 앞에 세움
        self.assertEqual(suspects_pending(r, layer, freq=freq), 0)
        r = suspect_page(r, IMG, Never(), False, layer, batch=1, freq=freq)
        self.assertEqual((len(r["suspects"]), r["suspect_stats"]["already"]), (8, 8))
        self.assertEqual(suspects_pending(r, layer, max_n=10, freq=freq), 2)
        self.assertEqual(len(suspect_page(r, IMG, Reader({}), False, layer, max_n=10, freq=freq)["suspects"]), 10)

    def test_window_blind_failure_stop_and_lowres(self):
        r = suspect_page(rec([COL1, COL2]), IMG, Reader({frozenset("入人"): ("人", "全然別字"), frozenset("曰日"): ("", "又?主")}), False, LAYER)
        self.assertEqual([e["outcome"] for e in r["suspects"]], ["undecided", "undecided"])     # 창이 앞뒤와 안 맞음(낱개로 다시 물어도) · 못 가림
        self.assertIn("조각 위치가 어긋남", r["suspects"][0]["evidence"]); self.assertEqual(r["merged"]["uncertain"], [])
        rd = Reader(ANS); rd.identify = lambda crop, ask, h, p: GlyphChoice(choice="入", evidence="", window="")
        r = suspect_page(rec([COL1, COL2]), IMG, rd, False, LAYER, blind=True)
        self.assertEqual([e["outcome"] for e in r["suspects"]], ["undecided", "undecided"])     # 눈가림 답이 다르면 보류
        self.assertTrue(r["suspects"][0]["evidence"].startswith("눈가림 답 「入」"))
        class Down(Reader):
            def adjudicate_many(self, *a, **k): raise ReaderError("한도", fatal=False)
        r = suspect_page(rec([COL1, COL2]), IMG, Down(ANS), False, LAYER)
        self.assertEqual([e["outcome"] for e in r["suspects"]], ["failed", "failed"])
        self.assertEqual(suspects_pending(r, LAYER), 2)                                          # 실패한 자리는 다음 실행에서 다시
        r = suspect_page(r, IMG, Reader(ANS), False, LAYER)
        self.assertEqual([e["outcome"] for e in r["suspects"]], ["differs", "confirmed"])
        class Fatal(Reader):
            def adjudicate_many(self, *a, **k): raise ReaderError("키 오류", fatal=True)
        with self.assertRaises(ReaderError):
            suspect_page(rec([COL1, COL2]), IMG, Fatal(ANS), False, LAYER)
        r = suspect_page(rec([COL1, COL2]), IMG, Never(), False, LAYER, should_stop=lambda: True)
        self.assertEqual((r["suspects"], r["suspect_stats"]["skipped"]), ([], 2))              # 중지로 못 물은 자리는 기록하지 않는다
        r = suspect_page(rec([COL1, COL2]), IMG, Never(), False, LAYER, min_px=10_000)
        self.assertEqual((r["suspects"], r["suspect_stats"]["lowres"]), ([], 2))               # 지침 24: 작은 글자는 묻지 않는다

    def test_fallback_reader(self):
        flash = Reader({}); flash.model = "flash"
        pro = Reader(ANS); pro.model = "pro"
        r = suspect_page(rec([COL1, COL2]), IMG, flash, False, LAYER, fallback_reader=pro)
        self.assertEqual([(e["outcome"], e["model"]) for e in r["suspects"]], [("differs", "pro"), ("confirmed", "pro")])
        self.assertEqual(r["suspect_stats"]["fallback_decided"], 2)


class Downstream(unittest.TestCase):
    """remerge 가 남기거나 다시 만들고, 등급은 쪽 고지로·글자에 메모로, 장부는 따로 세고, 검수 화면은 [합의 의심] 으로."""
    def _rec(self):
        return suspect_page(rec([COL1, COL2]), IMG, Reader(ANS), False, LAYER)

    def test_remerge_keeps_or_rebuilds_idempotently(self):
        from yeone_ocr.cli import remerge_record
        r = self._rec(); want = suspect_notices(r)
        once = remerge_record(json.loads(json.dumps(r)), LAYER)
        u1 = PageReading.model_validate(once["merged"]).uncertain
        self.assertEqual([u for u in u1 if u.context.startswith(SUSPECT_NOTE)], want)
        twice = remerge_record(json.loads(json.dumps(once)), LAYER)
        self.assertEqual(twice["merged"]["uncertain"], once["merged"]["uncertain"])
        lost = json.loads(json.dumps(r)); lost["merged"]["uncertain"] = []                       # 항목이 사라진 기록도 rec['suspects'] 로 다시
        u3 = PageReading.model_validate(remerge_record(lost, LAYER)["merged"]).uncertain
        self.assertEqual([u for u in u3 if u.context.startswith(SUSPECT_NOTE)], want)
        self.assertEqual(body(once), body(r))

    def test_grades_memo_on_the_character(self):
        from yeone_ocr.grades import page_grades, classify_uncertain, page_body, decided_keys
        r = self._rec(); pg = page_grades(r)
        memos = pg.memos[("r1", 0)]
        self.assertEqual(len(memos), 1); off, msg = memos[0]
        self.assertEqual(body(r)[0][off], "入"); self.assertEqual(off, 1)
        self.assertTrue(msg.startswith("[합의 의심]") and "「人」" in msg)
        self.assertFalse(any(SUSPECT_NOTE in x for x in pg.page_notices))                     # 글자에 달았으니 쪽 고지에 되풀이하지 않는다
        self.assertEqual(pg.stats["suspects"], 1)
        pr = PageReading.model_validate(r["merged"])
        self.assertEqual(classify_uncertain(pr.uncertain[0], page_body(pr), decided_keys(r)), "notice")
        u = Uncertain(region_id="r1", context=SUSPECT_NOTE + " — 「又〔入/人〕肝」")              # 본문에 있는 〔〕 가 섞여도 고지
        self.assertEqual(classify_uncertain(u, "又〔入/人〕肝", set()), "notice")
        gone = json.loads(json.dumps(r)); gone["merged"]["regions"][0]["paragraphs"][0]["columns"][0]["text"] = FILLER     # 글자를 못 찾으면 쪽 고지로
        pg = page_grades(gone)
        self.assertNotIn(("r1", 0), pg.memos); self.assertTrue(any(SUSPECT_NOTE in x for x in pg.page_notices))

    def test_hwpx_memo_and_text(self):
        from yeone_ocr.blocks import assemble_blocks
        from yeone_ocr.hwpx_builder import build_hwpx
        from yeone_ocr.cli import check_hwpx_file
        r = self._rec()
        for mode in ("notice", "all"):
            paras = [b for b in assemble_blocks([r], memo_mode=mode) if b.type == "para"]
            self.assertEqual([b.text for b in paras], [body(r)[0]])
            hit = [(o, m) for o, m in paras[0].memos if m.startswith("[합의 의심]")]
            self.assertEqual([paras[0].text[o] for o, _ in hit], ["入"])
        with tempfile.TemporaryDirectory() as td:
            out = os.path.join(td, "s.hwpx")
            build_hwpx(assemble_blocks([r]), out)
            x = zipfile.ZipFile(out).read("Contents/section0.xml").decode()
            self.assertIn("[합의 의심] 1차·2차 「入」", x); self.assertEqual(check_hwpx_file(out), [])

    def test_ledger_and_review(self):
        from yeone_ocr.ledger import build_ledger, write_ledger
        from yeone_ocr.review_html import make_review
        r = self._rec()
        with tempfile.TemporaryDirectory() as td:
            work = Work(os.path.join(td, "book.pdf"), os.path.join(td, "w")); work.save_record(1, r)
            led = build_ledger(work.dir)
            rows = [(p["source"], p["a"], p["b"], p["count"]) for p in led["pairs"] if p["source"] == SUSPECT_SOURCE]
            self.assertEqual(rows, [(SUSPECT_SOURCE, "入", "人", 1)])                          # confirmed(曰) 은 세지 않는다
            write_ledger(work.dir)
            self.assertEqual(suspect_freq(work.dir).get(frozenset("入人")), 1)
            html = Path(make_review(work, [r])).read_text(encoding="utf-8")
            self.assertIn("[합의 의심]</small>", html); self.assertIn(SUSPECT_NOTE, html)


def vertical_pdf(n=1):
    """세로쓰기 번체 글자층(열 셋)."""
    doc = pymupdf.open()
    for _ in range(n):
        pg = doc.new_page(width=500, height=800)
        for k in range(3):
            pg.insert_text((450 - k * 30, 60), "天地玄黃宇宙洪荒日月盈昃辰宿列張", fontname="china-t", fontsize=14, rotate=270)
    return doc


class Command(unittest.TestCase):
    """cmd_adjudicate: 〔a/b〕 가 없는 쪽도 합의 의심을 묻고, 요약 줄·'판독 확인 필요'·다시 실행·끄기·mock 셈."""
    LINE = "天地玄黃宇宙洪荒日月盈昃辰宿列張"

    def _work(self, td):
        doc = vertical_pdf(); pdf = os.path.join(td, "v.pdf"); doc.save(pdf); doc.close()
        doc = pymupdf.open(pdf); work = Work(pdf, os.path.join(td, "w"))
        r = rec([self.LINE.replace("日月", "曰月"), self.LINE, self.LINE])                     # 1차·2차 모두 曰, 글자층 日
        r["merged"]["regions"][0]["bbox"] = [60, 780, 380, 960]; r["pass1"]["regions"][0]["bbox"] = [60, 780, 380, 960]
        work.save_record(1, r)
        return doc, work

    def _args(self, work, **kw):
        a = dict(pdf=work.pdf_path.as_posix(), work=str(work.dir), pages=None, dpi=300, mode="vertical", reader="gemini", model="fake",
                 thinking_budget=None, no_cache=True, max_sites=0, zoom=6, batch_sites=8, workers=1, workers_start=1, adjudicate_model="", adjudicate_thinking="low")
        a.update(kw); return Namespace(**a)

    def _run(self, work, doc, rd, **kw):
        from unittest import mock
        from yeone_ocr import cli
        buf = io.StringIO()
        with mock.patch.object(cli, "make_glyph_readers", lambda args: (rd, None)), contextlib.redirect_stdout(buf):
            rc = cli.cmd_adjudicate(self._args(work, **kw), work, doc, standalone=True)
        return rc, buf.getvalue()

    def test_suspects_without_marks(self):
        rd = Reader({frozenset("日曰"): ("日", "洪荒日月")})
        with tempfile.TemporaryDirectory() as td, contextlib.ExitStack() as resources:
            doc, work = self._work(td); before = body(work.load_record(1))
            resources.callback(doc.close)
            rc, out = self._run(work, doc, rd)
            self.assertEqual(rc, 0, out); self.assertNotIn("판별할 〔a/b〕 자리가 없습니다", out)
            self.assertIn("합의 의심: 1곳 물음 → 자형이 글자층 쪽 1(확인 항목) · 판독 쪽 0 · 못 가림 0", out)
            r = work.load_record(1)
            self.assertEqual(body(r), before)
            self.assertEqual([(e["consensus"], e["layer"], e["outcome"], e["loc"]) for e in r["suspects"]], [("曰", "日", "differs", "layer")])
            self.assertIn("판독 확인 필요", work.statuses(1))
            self.assertTrue(any(u["context"].startswith(SUSPECT_NOTE) for u in r["merged"]["uncertain"]))
            rc, out = self._run(work, doc, Never())                                              # 다시 실행: 새로 물을 곳 없음
            self.assertEqual(rc, 0, out); self.assertIn("새로 물을 곳이 없습니다", out)
            rc, out = self._run(work, doc, Never(), force_suspects=True, reader="mock")        # mock 은 세기만
            self.assertEqual(rc, 0); self.assertIn("합의 판독 의심 1곳", out)

    def _mark_work(self, td, pages=1):
        doc = vertical_pdf(pages); pdf = os.path.join(td, "v.pdf"); doc.save(pdf); doc.close()
        doc = pymupdf.open(pdf); work = Work(pdf, os.path.join(td, "w"))
        for pg in range(1, pages + 1):
            r = rec([self.LINE.replace("日月", "曰月"), self.LINE.replace("玄", "〔玄/元〕"), self.LINE], page=pg)   # 〔玄/元〕 하나 + 합의 의심(曰/日) 하나
            r["merged"]["regions"][0]["bbox"] = [60, 780, 380, 960]; r["pass1"]["regions"][0]["bbox"] = [60, 780, 380, 960]
            work.save_record(pg, r)
        return doc, work

    def test_stop_in_suspects_keeps_paid_marks(self):
        """합의 의심 단계에서 호출 상한(치명)에 닿아도 그 쪽의 값을 치른 〔a/b〕 판정과 쓴 호출(usage)은 기록에 남는다 — 다시 사지 않게."""
        from yeone_ocr import reader_gemini as rg
        class Paid(Reader):
            def usage_begin(self): self.u = {}
            def usage_end(self):
                u, self.u = getattr(self, "u", None) or {}, None; return u
            def _ans(self, ask):
                rg.CALL_CAP.take(); self.u["calls"] = self.u.get("calls", 0) + 1
                return super()._ans(ask)
        rd = Paid({frozenset("玄元"): ("玄", "地玄黃"), frozenset("日曰"): ("日", "洪荒日月")})
        with tempfile.TemporaryDirectory() as td, contextlib.ExitStack() as resources:
            doc, work = self._mark_work(td)
            resources.callback(doc.close)
            rg.set_call_cap(1)
            try:
                rc, out = self._run(work, doc, rd)
            finally:
                rg.set_call_cap(0)
            self.assertEqual(rc, 1, out); self.assertIn("호출 상한", out)
            r = work.load_record(1)
            self.assertEqual([(a["a"], a["b"], a["choice"]) for a in r["adjudications"]], [("玄", "元", "玄")])
            self.assertNotIn("〔玄/元〕", body(r)[0])
            self.assertEqual(r["usage"]["calls"], 1); self.assertEqual(r["usage"]["by_model"]["fake (3차)"]["calls"], 1)
            self.assertNotIn("suspects", r)
            rd2 = Paid({frozenset("日曰"): ("日", "洪荒日月")})                                 # 다시 실행: 표시는 다시 묻지 않고 합의 의심만
            rc, out = self._run(work, doc, rd2)
            self.assertEqual(rc, 0, out)
            self.assertEqual([set(a.candidates) for a in [*rd2.single, *sum(rd2.batches, [])]], [set("日曰")])
            r = work.load_record(1)
            self.assertEqual(r["usage"]["calls"], 2); self.assertEqual([e["outcome"] for e in r["suspects"]], ["differs"])

    def test_stop_before_suspects_is_a_stop(self):
        """〔a/b〕 마지막 호출 중에 [중지]를 누르면 합의 의심을 건너뛰고 '중지'(130)로 끝난다(성공으로 보이지 않게)."""
        from yeone_ocr import cli
        class R(Reader):
            def _ans(self, ask):
                cli.CANCEL.set(); return GlyphChoice(choice="玄", evidence="획", window="地玄黃")
        with tempfile.TemporaryDirectory() as td, contextlib.ExitStack() as resources:
            doc, work = self._mark_work(td)
            resources.callback(doc.close)
            try:
                rc, out = self._run(work, doc, R({}))
            finally:
                cli.CANCEL.clear()
            self.assertEqual(rc, 130, out); self.assertIn("중지 요청으로 판별을 멈춤", out)
            r = work.load_record(1)
            self.assertNotIn("suspects", r); self.assertEqual([a["choice"] for a in r["adjudications"]], ["玄"])

    def test_gui_estimate_counts_suspects(self):
        """3차 판별 비용 창의 셈(costplan, 모델 없이): 〔a/b〕 자리와 합의 의심 자리·묶음 호출. 새로 물을 곳이 없으면 창은 실행하지 않고 알린다."""
        from types import SimpleNamespace
        from unittest import mock
        from yeone_ocr.costplan import PlanInput, count_plan
        with tempfile.TemporaryDirectory() as td:
            doc, work = self._mark_work(td, pages=3); doc.close()
            for pg in (2, 3):                                                                   # 〔a/b〕 는 1쪽에만
                r = work.load_record(pg); r["merged"] = rec([self.LINE.replace("日月", "曰月"), self.LINE, self.LINE], page=pg)["merged"]
                r["merged"]["regions"][0]["bbox"] = [60, 780, 380, 960]; work.save_record(pg, r)
            inp = PlanInput(kind="adjudicate", pdf=work.pdf_path.as_posix(), work_dir=str(work.dir), reader="gemini", model="gemini-2.5-pro")
            plan = count_plan(inp)
            got = {r["step"]: (r["items"], r["calls"]) for r in plan["rows"]}
            self.assertEqual(got, {"glyph": (1, [1, 2]), "suspect": (3, [3, 6])})            # 합의 의심 3곳 — 쪽마다 묶음 1회, 낱개 재질문 최대 3회 더
            for pg in (1, 2, 3):                                                                # 새로 물을 곳이 없으면 묻지 않고 알린다
                r = work.load_record(pg); r["merged"] = rec([self.LINE] * 3, page=pg)["merged"]; work.save_record(pg, r)
            plan = count_plan(inp)
            self.assertEqual(sum(r["calls"][1] for r in plan["rows"]), 0)
            try:
                from yeone_ocr import gui
            except Exception as e:                       # tkinter 가 없는 환경
                self.skipTest(f"gui 를 불러오지 못함: {e}")
            app = SimpleNamespace(_plan_seq=1, _plan_wait=None, _planning=True, buttons=[], running=False, cfg={}, fx_setting=lambda: (None, ""),
                                  _append=lambda s: None, v_status=mock.Mock(), work=lambda: Path(work.dir))
            app._lock_for_plan = gui.App._lock_for_plan.__get__(app)
            go = mock.Mock(); info = []
            with mock.patch.object(gui.messagebox, "showinfo", lambda title, msg: info.append(msg)), \
                    mock.patch.object(gui, "CostDialog", side_effect=AssertionError("물을 곳이 없는데 비용 창")):
                gui.App._plan_done(app, 1, "adjudicate", ("ok", plan), go)
            self.assertIn("새로 물을 곳이 없습니다", info[0]); go.assert_not_called()

    def test_no_suspects_and_cap(self):
        with tempfile.TemporaryDirectory() as td, contextlib.ExitStack() as resources:
            doc, work = self._work(td)
            resources.callback(doc.close)
            rc, out = self._run(work, doc, Never(), no_suspects=True)
            self.assertEqual(rc, 0); self.assertIn("판별할 〔a/b〕 자리가 없습니다.", out); self.assertNotIn("합의", out)
            rc, out = self._run(work, doc, Never(), suspect_max=0)
            self.assertEqual(rc, 0); self.assertNotIn("suspects", work.load_record(1))

    def test_parser_and_gui_options(self):
        from yeone_ocr.cli import build_parser
        ap = build_parser()
        for cmd in ("adjudicate", "run"):
            a = ap.parse_args([cmd, "x.pdf"]); self.assertEqual((a.no_suspects, a.suspect_max, a.force_suspects), (False, 8, False))
            a = ap.parse_args([cmd, "x.pdf", "--no-suspects", "--suspect-max", "3", "--force-suspects"])
            self.assertEqual((a.no_suspects, a.suspect_max, a.force_suspects), (True, 3, True))
        try:
            from yeone_ocr import gui
        except Exception as e:                       # tkinter 가 없는 환경
            self.skipTest(f"gui 를 불러오지 못함: {e}")
        app = gui.App.__new__(gui.App)
        var = lambda v: Namespace(get=lambda: v)
        app.v_blind, app.v_suspects = var(True), var(False)
        self.assertEqual(app._glyph_opts(suspects=True), ["--adjudicate-blind", "--no-suspects"])
        self.assertEqual(app._glyph_opts(), ["--adjudicate-blind"])                          # collate 에는 그 선택이 없다
        ap.parse_args(["collate", "x.pdf", "--hwpx", "a.hwpx"] + app._glyph_opts())
        ap.parse_args(["run", "x.pdf"] + app._glyph_opts(suspects=True)); ap.parse_args(["adjudicate", "x.pdf"] + app._glyph_opts(suspects=True))
        app.v_suspects = var(True)
        self.assertEqual(app._glyph_opts(suspects=True), ["--adjudicate-blind"])
        self.assertIn('self.cfg.get("suspects", True)', Path(gui.__file__).read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
