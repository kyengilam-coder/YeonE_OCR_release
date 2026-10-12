"""0.6.8 병합(merge) 규칙: 표시 곁의 글자 차이 · 이체자 호환 한자 보존 · 접어서만 같은 표기 차이 알림. 모델 없이 돌아간다."""
import sys, unicodedata, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from yeone_ocr import merge
from yeone_ocr.merge import merge_passes, diff_mark, nfc_text, compare_textlayer, NOTATION_DIFF
from yeone_ocr.schema import PageReading, Region, Paragraph, Column, RegionReread, Uncertain
from yeone_ocr.collate import zh


def one_region(t1: str, t2: str, fold_zh: bool = False):
    """1차 열 하나 · 2차 열 하나를 병합한 (열 글, 통계, 확인 항목 문맥 목록)."""
    p1 = PageReading(pdf_page=1, regions=[Region(id="b", kind="본문", paragraphs=[Paragraph(columns=[Column(col_from_right=1, text=t1)])])])
    rr = {"b": RegionReread(columns=[Column(col_from_right=1, text=t2)])}
    old = merge.FOLD_ZH; merge.FOLD_ZH = fold_zh
    try:
        m, st = merge_passes(p1, rr)
    finally:
        merge.FOLD_ZH = old
    return m.regions[0].paragraphs[0].columns[0].text, st, [u.context for u in m.uncertain]


class MarkNeighbour(unittest.TestCase):
    """1a — 표시 토큰이 든 차이 구간에서 표시 곁의 글자 차이가 삼켜지지 않는다(0.6.7 까지는 구간 전체를 1차 글로 덮었다)."""
    def test_disagreement_next_to_mark_is_kept(self):
        text, st, ctx = one_region("能入〔疑: 肝〕經血分", "能人肝經血分")
        self.assertEqual(text, "能〔入/人〕〔疑: 肝〕經血分"); self.assertEqual((st["marks"], st["columns_differ"]), (1, 1))
        self.assertEqual(ctx, ["「能〔入/人〕〔疑: 肝〕經血」"])
        self.assertEqual(one_region("能入肝經血分", "能人肝經血分")[0], "能〔入/人〕肝經血分")        # 대조군: 표시가 없을 때와 같은 자리에 같은 표시

    def test_mark_content_agreed_is_unchanged(self):
        text, st, ctx = one_region("甲〔疑: 乙〕丙", "甲乙丙")
        self.assertEqual((text, st["marks"], st["columns_differ"], ctx), ("甲〔疑: 乙〕丙", 0, 0, []))

    def test_pass2_mark_is_kept(self):
        text, st, ctx = one_region("甲乙丙", "甲〔疑: 乙〕丙")
        self.assertEqual((text, st["marks"], ctx), ("甲〔疑: 乙〕丙", 1, ["「甲〔疑: 乙〕丙」"]))

    def test_same_mark_on_both_sides(self):
        text, st, ctx = one_region("能入〔疑: 肝〕經", "能人〔疑: 肝〕經")
        self.assertEqual((text, st["marks"], ctx), ("能〔入/人〕〔疑: 肝〕經", 1, ["「能〔入/人〕〔疑: 肝〕經」"]))

    def test_mark_followed_by_deletion(self):
        text, st, ctx = one_region("甲〔疑: 乙〕丙丁", "甲乙丁")
        self.assertEqual((text, st["marks"], ctx), ("甲〔疑: 乙〕〔疑: 丙〕丁", 1, ["「甲〔疑: 乙〕〔疑: 丙〕丁」"]))
        text, st, _ = one_region("甲〔疑: 乙〕丙丁戊", "甲乙丙己戊")
        self.assertEqual((text, st["marks"]), ("甲〔疑: 乙〕丙〔丁/己〕戊", 1))

    def test_mark_content_differs_is_noted_not_marked(self):
        """1차 표시의 알맹이와 2차 글자가 다르면 표시는 그대로(1차 우선), 2차 글은 확인 항목 설명에만. 2차의 다른 표시도 1차 표시에 밀린다."""
        text, st, ctx = one_region("甲〔疑: 乙〕丙", "甲丁丙")
        self.assertEqual((text, st["marks"], st["columns_differ"], ctx), ("甲〔疑: 乙〕丙", 0, 1, ["「甲〔疑: 乙〕丙」 ↔ 2차 「丁」"]))
        self.assertEqual(one_region("甲〔疑: 乙〕丙", "甲〔疑: 丁〕丙")[::2], ("甲〔疑: 乙〕丙", ["「甲〔疑: 乙〕丙」 ↔ 2차 「丁」"]))
        self.assertEqual(one_region("甲〔疑: 乙〕丙", "甲丙")[::2], ("甲〔疑: 乙〕丙", ["「甲〔疑: 乙〕丙」 ↔ 2차 없음"]))
        text, st, ctx = one_region("甲〔판독불명〕丙", "甲乙丙")                          # 알맹이 없는 표시도 자리를 지킨다
        self.assertEqual((text, st["marks"], ctx), ("甲〔판독불명〕丙", 0, ["「甲〔판독불명〕丙」 ↔ 2차 「乙」"]))

    def test_marked_segment_across_columns(self):
        p1 = PageReading(pdf_page=1, regions=[Region(id="b", kind="본문", paragraphs=[Paragraph(columns=[
            Column(col_from_right=1, text="能入〔疑: 肝〕"), Column(col_from_right=2, text="經血分")])])])
        rr = {"b": RegionReread(columns=[Column(col_from_right=1, text="能人肝經"), Column(col_from_right=2, text="血分")])}
        m, st = merge_passes(p1, rr)
        self.assertEqual([c.text for c in m.regions[0].paragraphs[0].columns], ["能〔入/人〕〔疑: 肝〕", "經血分"]); self.assertEqual(st["marks"], 1)
        self.assertEqual((m.uncertain[0].col_from_right, m.uncertain[0].context), (1, "「能〔入/人〕〔疑: 肝〕經血」"))

    def test_diff_mark_keeps_neighbour_difference(self):
        t, marks = diff_mark("能入〔疑: 肝〕經血分", "能人肝經血分")
        self.assertEqual((t, marks), ("能〔入/人〕〔疑: 肝〕經血分", [(1, "「能〔入/人〕〔疑: 肝〕經血」")]))
        self.assertEqual(diff_mark("甲〔疑: 乙〕丙丁", "甲乙丁")[0], "甲〔疑: 乙〕〔疑: 丙〕丁")
        self.assertEqual(diff_mark("甲〔疑: 乙〕丙", "甲乙丙"), ("甲〔疑: 乙〕丙", []))
        self.assertEqual(diff_mark("甲乙丙", "甲〔疑: 乙〕丙")[0], "甲〔疑: 乙〕丙")
        self.assertEqual(diff_mark("納甲", "納〔疑: 甲〕")[0], "納〔疑: 甲〕")


class SameMarkTwice(unittest.TestCase):
    """같은 표시가 한 글에 두 번 — 토큰 표의 역방향 사전이 토큰 글자 대신 표시 글을 가리켜 둘째 표시가 토큰이 되지 않던 문제(뒤 글자가 지워짐)."""
    def test_tokenize_gives_each_copy_the_same_token(self):
        t = merge._tokenize("〔疑: 乙〕〔疑: 乙〕", {})
        self.assertEqual((len(t), t[0] == t[1]), (2, True))

    def test_identical_readings_unchanged(self):
        for t in ("甲〔판독불명〕〔판독불명〕乙丙丁", "天地玄黃宇宙洪荒〔판독불명〕〔판독불명〕日月盈昃辰宿列張", "甲〔疑: 乙〕丙〔疑: 乙〕丁"):
            text, st, ctx = one_region(t, t)
            self.assertEqual((text, st["marks"], st["columns_differ"], ctx), (t, 0, 0, []))
            self.assertEqual(diff_mark(t, t), (t, []))
        text, st, ctx = one_region("甲〔疑: 乙〕丙〔疑: 乙〕丁", "甲乙丙乙丁")                          # 알맹이대로 읽은 2차
        self.assertEqual((text, st["marks"], st["columns_differ"], ctx), ("甲〔疑: 乙〕丙〔疑: 乙〕丁", 0, 0, []))

    def test_pass2_mark_twice(self):
        text, st, ctx = one_region("甲乙丙丁", "甲〔판독불명〕乙〔판독불명〕丁")
        self.assertEqual((text, st["marks"]), ("甲〔판독불명〕乙〔판독불명〕丁", 2))
        self.assertEqual(ctx, ["「甲〔판독불명〕乙丙丁」 ← 1차 없음", "「甲乙〔판독불명〕丁」 ← 1차 「丙」"])
        p1 = PageReading(pdf_page=1, regions=[Region(id="b", kind="본문", paragraphs=[Paragraph(columns=[
            Column(col_from_right=1, text="天地玄黃宇宙"), Column(col_from_right=2, text="日月盈昃辰宿")])])])
        rr = {"b": RegionReread(columns=[Column(col_from_right=1, text="天地〔판독불명〕黃宇宙"), Column(col_from_right=2, text="日月盈昃〔판독불명〕宿")])}
        m, st = merge_passes(p1, rr)
        self.assertEqual([c.text for c in m.regions[0].paragraphs[0].columns], ["天地〔판독불명〕黃宇宙", "日月盈昃〔판독불명〕宿"])
        self.assertEqual([u.context[-8:] for u in m.uncertain], ["← 1차 「玄」", "← 1차 「辰」"])


class RawPrivateUse(unittest.TestCase):
    """판독 글에 날것으로 든 사용자 영역 글자(U+E000~F8FF — 옛 글꼴·OCR 이 드문 한자를 넣는 자리)가 〔〕 표시 토큰과 겹쳐 표시로 바뀌던 문제(0.6.7 까지 토큰이 U+E000~)."""
    A, B = "\ue000", "\ue001"

    def test_raw_chars_kept_beside_marks(self):
        a, b = self.A, self.B
        for t1, t2, want in (
                (f"〔疑: 乙〕甲{a}丙", f"乙甲{a}丙", f"〔疑: 乙〕甲{a}丙"),                    # 첫 표시의 토큰이 U+E000 이라 날글자가 표시로 바뀌었다
                (f"{a}〔판독불명〕{b}丙", f"{a}〔판독불명〕{b}丙", f"{a}〔판독불명〕{b}丙"),
                (f"甲{a}〔疑: {b}〕丙", f"甲{a}{b}丙", f"甲{a}〔疑: {b}〕丙"),                    # 표시 알맹이가 사용자 영역 글자
                (f"〔疑: 乙〕甲{a}丙", f"乙甲{b}丙", f"〔疑: 乙〕甲〔{a}/{b}〕丙"),               # 날글자끼리 갈리면 여느 글자처럼 〔a/b〕
                (f"{a}甲〔판독불명〕", f"{a}乙〔판독불명〕", f"{a}〔甲/乙〕〔판독불명〕")):
            text, st, ctx = one_region(t1, t2)
            self.assertEqual(text, want, (t1, t2)); self.assertEqual(diff_mark(t1, t2)[0], want, (t1, t2))
            self.assertFalse(any(0xD800 <= ord(ch) <= 0xDFFF for ch in text + "".join(ctx)))

    def test_identical_readings_unchanged(self):
        a, b = self.A, self.B
        for t in (f"{a}{b}甲乙", f"〔疑: 乙〕{a}甲〔판독불명〕{b}", f"{a}〔{a}/{b}〕{b}", f"〔疑: {a}〕{a}"):
            text, st, ctx = one_region(t, t)
            self.assertEqual((text, st["marks"], st["columns_differ"], ctx), (t, 0, 0, []))
            self.assertEqual(diff_mark(t, t), (t, []))

    def test_columns_share_one_table(self):
        """앞 열의 표시가 받은 토큰이 뒤 열·2차의 날글자와 같아 열 글이 바뀌던 경로(_strip_map → _apply_edits)."""
        a, b = self.A, self.B
        p1 = PageReading(pdf_page=1, regions=[Region(id="b", kind="본문", paragraphs=[Paragraph(columns=[
            Column(col_from_right=1, text="甲〔疑: 乙〕丙"), Column(col_from_right=2, text=f"{a}丁戊{b}"), Column(col_from_right=3, text=f"{b}{a}")])])])
        rr = {"b": RegionReread(columns=[Column(col_from_right=1, text="甲乙丙"), Column(col_from_right=2, text=f"{a}丁己{b}"), Column(col_from_right=3, text=f"{b}{a}")])}
        m, st = merge_passes(p1, rr)
        self.assertEqual([c.text for c in m.regions[0].paragraphs[0].columns], ["甲〔疑: 乙〕丙", f"{a}丁〔戊/己〕{b}", f"{b}{a}"])
        self.assertEqual(([u.context for u in m.uncertain], st["marks"]), ([f"「丙{a}丁〔戊/己〕{b}{b}{a}」"], 1))

    def test_tokens_never_collide(self):
        a, b = self.A, self.B; table: dict = {}
        s1 = merge._tokenize(f"〔疑: 乙〕{a}甲", table); s2 = merge._tokenize(f"{b}〔판독불명〕\ud800〔疑: {a}〕", table)
        self.assertEqual((len(s1), len(s2), len(table)), (3, 4, 3))                                 # 표시 하나 = 글자 하나(위치 1:1)
        self.assertFalse(set(table) & set(f"〔疑: 乙〕{a}甲{b}〔판독불명〕"))
        self.assertFalse(any(0xF0000 <= ord(t) <= 0xFFFFD for t in table))                         # evaluate 의 표지 자리와도 겹치지 않는다
        self.assertEqual(merge._detok(s1, table), f"〔疑: 乙〕{a}甲")
        self.assertEqual(merge._detok(s2, table), f"{b}〔판독불명〕\ufffd〔疑: {a}〕")                 # 깨진 대리 부호값은 U+FFFD(저장도 못 하는 글)
        once = merge._detok(s1 + s2, table)
        self.assertEqual(merge._detok(once, table), once)                                            # 되돌린 글이 다시 들어와도 그대로
        self.assertEqual((merge._fold(s1), merge._fold(a + b)), (s1, a + b))                         # 토큰·사용자 영역 글자 모두 접어도 그대로


class MarkSpan(unittest.TestCase):
    """표시 하나가 여러 맞춤 조각에 걸칠 때: 설명은 상대 글 전체로 한 번, 겹친 2차 표시는 버리되 1차 글자는 남긴다, 사이 글은 표시의 몫."""
    def test_partly_matched_pass1_mark_keeps_note(self):
        self.assertEqual(one_region("甲〔乙丙/丁〕戊", "甲乙己戊")[::2], ("甲〔乙丙/丁〕戊", ["「甲〔乙丙/丁〕戊」 ↔ 2차 「乙己」"]))
        self.assertEqual(one_region("甲〔乙丙/丁〕戊", "甲己丙戊")[2], ["「甲〔乙丙/丁〕戊」 ↔ 2차 「己丙」"])
        self.assertEqual(one_region("甲〔疑: 乙丙〕戊", "甲乙戊")[2], ["「甲〔疑: 乙丙〕戊」 ↔ 2차 「乙」"])
        self.assertEqual(one_region("〔疑: 甲乙〕丙", "甲丁乙丙")[::2], ("〔疑: 甲乙〕丙", ["「〔疑: 甲乙〕丙」 ↔ 2차 「甲丁乙」"]))

    def test_partly_matched_pass2_mark_keeps_pass1_text_in_note(self):
        text, st, ctx = one_region("甲乙己戊", "甲〔乙丙/丁〕戊")
        self.assertEqual((text, st["marks"], ctx), ("甲〔乙丙/丁〕戊", 1, ["「甲〔乙丙/丁〕戊」 ← 1차 「乙己」"]))
        self.assertEqual(one_region("丙甲戊乙丁", "丙〔甲乙/己〕丁")[::2], ("丙〔甲乙/己〕丁", ["「丙〔甲乙/己〕丁」 ← 1차 「甲戊乙」"]))   # 순서도 1차대로
        self.assertEqual(one_region("甲丙", "甲〔疑: 乙〕丙")[2], ["「甲〔疑: 乙〕丙」 ← 1차 없음"])
        self.assertEqual(one_region("丁乙", "〔甲丁/丁〕乙")[::2], ("〔甲丁/丁〕乙", ["「〔甲丁/丁〕乙」 ← 1차 「丁」"]))     # 퍼즈 최소 사례
        p1 = PageReading(pdf_page=1, regions=[Region(id="b", kind="본문", paragraphs=[Paragraph(columns=[
            Column(col_from_right=1, text="乙"), Column(col_from_right=2, text="丁丙")])])])
        m, _ = merge_passes(p1, {"b": RegionReread(columns=[Column(col_from_right=1, text="乙〔疑: 丁丙〕丙")])})
        self.assertEqual(([c.text for c in m.regions[0].paragraphs[0].columns], [u.context for u in m.uncertain]),
                         (["乙", "〔疑: 丁丙〕丙"], ["「乙〔疑: 丁丙〕丙」 ← 1차 「丁」"]))

    def test_pass2_mark_over_pass1_mark_drops_no_pass1_text(self):
        self.assertEqual(one_region("〔疑: 甲〕乙丙", "〔甲乙/丁〕丙")[::2], ("〔疑: 甲〕乙丙", []))
        self.assertEqual(one_region("丙乙〔疑: 甲〕", "丙〔乙甲/丁〕")[::2], ("丙乙〔疑: 甲〕", []))
        self.assertEqual(diff_mark("〔疑: 甲〕乙", "〔甲乙/丙〕"), ("〔疑: 甲〕乙", []))

    def test_pass2_unreadable_keeps_its_slot(self):
        """2차 〔판독불명〕 이 자리 나누기에서 빠지면 다음 2차 글자가 1차 표시에 붙어 丙/丁 갈림이 〔疑: 丙〕 이 되던 문제."""
        text, st, ctx = one_region("甲〔疑: 乙〕丙戊", "甲〔판독불명〕丁戊")
        self.assertEqual((text, st["marks"]), ("甲〔疑: 乙〕〔丙/丁〕戊", 1))
        self.assertEqual(ctx, ["「甲〔疑: 乙〕〔丙/丁〕戊」 ↔ 2차 판독불명", "「甲〔疑: 乙〕〔丙/丁〕戊」"])
        self.assertEqual(one_region("甲丙〔疑: 乙〕戊", "甲丁〔판독불명〕戊")[::2],
                         ("甲〔丙/丁〕〔疑: 乙〕戊", ["「甲〔丙/丁〕〔疑: 乙〕戊」", "「甲〔丙/丁〕〔疑: 乙〕戊」 ↔ 2차 판독불명"]))

    def test_empty_pass1_region(self):
        """1차 열 글이 비었고 2차만 읽은 영역 — 위치를 못 찾아 IndexError 로 쪽 전체가 실패하던 문제."""
        self.assertEqual(one_region("", "甲乙")[::2], ("〔疑: 甲乙〕", ["「〔疑: 甲乙〕」"]))
        p1 = PageReading(pdf_page=1, regions=[Region(id="b", kind="본문", paragraphs=[Paragraph(columns=[
            Column(col_from_right=1, text=" "), Column(col_from_right=2, text="")])])])
        m, st = merge_passes(p1, {"b": RegionReread(columns=[Column(col_from_right=1, text="甲乙")])})
        self.assertEqual([c.text for c in m.regions[0].paragraphs[0].columns], ["〔疑: 甲乙〕", ""])


class RemergePass2Failed(unittest.TestCase):
    """2차 판독 실패 항목이 비교 다시 계산(remerge·판 올림 자동 갱신)에서 사라지고 '판독 확인 필요' 가 풀리던 문제."""
    NOTE = "2차 판독 실패(조각 응답 형식 오류) — 이 영역은 1차 판독만으로 들어감, 원본 확인"

    def rec(self, merged_unc, **extra):
        p1 = PageReading(pdf_page=2, regions=[Region(id="b1", kind="본문", paragraphs=[Paragraph(columns=[Column(col_from_right=1, text="甲乙丙")])])])
        merged = p1.model_copy(deep=True); merged.uncertain = merged_unc
        return {"pdf_page": 2, "pass1": p1.model_dump(), "pass2": {}, "merged": merged.model_dump(), "bbox_fix": {"fixed": 0}, **extra}

    def test_notice_kept_and_idempotent(self):
        from yeone_ocr.cli import remerge_record
        rec = remerge_record(self.rec([Uncertain(region_id="b1", context=self.NOTE)], pass2_failed=["b1"]), "")
        first = [u["context"] for u in rec["merged"]["uncertain"]]
        self.assertEqual(first, [self.NOTE])
        self.assertEqual([u["context"] for u in remerge_record(rec, "")["merged"]["uncertain"]], first)
        rec = remerge_record(self.rec([Uncertain(region_id="b1", context=self.NOTE)]), "")          # pass2_failed 가 없는 기록도 항목으로 남긴다
        self.assertEqual([u["context"] for u in rec["merged"]["uncertain"]], [self.NOTE])

    def test_notice_rebuilt_from_record(self):
        from yeone_ocr.cli import remerge_record
        rec = remerge_record(self.rec([], pass2_failed=["b1"]), "")
        self.assertEqual([(u["region_id"], u["context"]) for u in rec["merged"]["uncertain"]], [("b1", "2차 판독 실패(사유 기록 없음) — 이 영역은 1차 판독만으로 들어감, 원본 확인")])
        rec = remerge_record(self.rec([], pass2_failed={"b1": "조각 응답 형식 오류"}), "")
        self.assertEqual([u["context"] for u in rec["merged"]["uncertain"]], [self.NOTE])
        rec = self.rec([]); rec["pass2"] = {"b1": {"columns": [{"n": 1, "t": "甲乙丙"}], "pass2_failed": "조각 응답 형식 오류", "chunks": 2, "chunks_read": 1}}
        self.assertEqual([u["context"] for u in remerge_record(rec, "")["merged"]["uncertain"]], [self.NOTE])     # 조각 일부만 실패한 영역

    def test_remerge_keeps_review_status(self):
        """끝에서 끝까지: 한 영역의 2차가 실패한 쪽(덜 된 쪽, read 는 3)을 다시 계산해도 '2차 판독 실패' 항목과 '판독 확인 필요' 가 남는다."""
        import contextlib, io, os, tempfile
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from test_auto import vertical_pdf, _TwoRegionReader, _use_reader
        from yeone_ocr import cli
        from yeone_ocr.state import Work
        with tempfile.TemporaryDirectory() as td:
            doc = vertical_pdf(2); pdf = os.path.join(td, "v.pdf"); doc.save(pdf); doc.close(); w = os.path.join(td, "w")
            with _use_reader(_TwoRegionReader(fail={"b2"})):
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(cli.main(["read", pdf, "--work", w, "--reader", "mock", "--pages", "2", "--second-mode", "each"]), 3)  # 2차 덜 된 쪽은 '완료'로 보이지 않는다
            work = Work(pdf, w)
            before = [u["context"] for u in work.load_record(2)["merged"]["uncertain"]]
            self.assertTrue(any(u.startswith("2차 판독 실패(") for u in before), before)
            for _ in range(2):
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(cli.main(["remerge", pdf, "--work", w, "--pages", "2"]), 0)
                self.assertEqual([u["context"] for u in work.load_record(2)["merged"]["uncertain"]], before)
                self.assertIn("판독 확인 필요", work.statuses(2))

class VariantCompat(unittest.TestCase):
    """1b — NFC 가 자형이 다른 호환 한자(U+FA0E~FAFF: 塚·諸·者…)를 표준 글자로 접지 않는다(지침 21). 발음 중복 호환 한자(U+F900~FA0D)는 접는다."""
    def test_nfc_text(self):
        self.assertEqual(nfc_text("塚諸者"), "塚諸者")
        self.assertEqual(nfc_text("音樂"), "音樂")                                            # U+F914 → U+6A02
        self.assertEqual(nfc_text("簾"), unicodedata.normalize("NFC", "簾"))              # U+F9A6 → U+7C3E
        self.assertEqual(nfc_text("한塚樂"), "한塚樂")                 # 사이의 자모 결합은 그대로 된다
        self.assertEqual(nfc_text("\U0002f800"), "\U0002f800")                                   # 보충 구역도 이체자

    def test_merge_keeps_variant_and_does_not_mark_it(self):
        text, st, ctx = one_region("甲塚乙", "甲塚乙")
        self.assertEqual((text, st["marks"], st["notation_diffs"]), ("甲塚乙", 0, 1))      # 저장 글에 U+FA10 이 남고 〔〕 표시는 없다
        self.assertTrue(ctx[0].startswith(NOTATION_DIFF) and "U+FA10" in ctx[0], ctx)
        self.assertEqual(one_region("甲塚乙", "甲塚乙")[::2], ("甲塚乙", []))
        text, st, ctx = one_region("音樂이 廣潤", "音樂이 廣潤")                           # 발음 중복 호환 한자는 저장 글에서 접힌다
        self.assertEqual((text, st["marks"], ctx), ("音樂이 廣潤", 0, []))

    def test_variant_noted_beside_a_real_difference(self):
        """세로쓰기 모드: 다른 열의 갈림(丁/己)이 있어도 塚 U+FA10 ↔ U+585A 표기 차이를 알린다."""
        p1 = PageReading(pdf_page=1, regions=[Region(id="b", kind="본문", paragraphs=[Paragraph(columns=[
            Column(col_from_right=1, text="甲\ufa10乙"), Column(col_from_right=2, text="丙丁戊")])])])
        rr = {"b": RegionReread(columns=[Column(col_from_right=1, text="甲\u585a乙"), Column(col_from_right=2, text="丙己戊")])}
        m, st = merge_passes(p1, rr)
        self.assertEqual([c.text for c in m.regions[0].paragraphs[0].columns], ["甲\ufa10乙", "丙〔丁/己〕戊"])
        self.assertEqual((st["marks"], st["notation_diffs"]), (1, 1))
        self.assertTrue(m.uncertain[1].context.startswith(NOTATION_DIFF + " 1자: \ufa10(U+FA10)→\u585a"), m.uncertain[1].context)

    def test_compare_textlayer_and_column_similarity_fold_variant(self):
        pr = PageReading(pdf_page=1, regions=[Region(id="b", kind="본문", paragraphs=[Paragraph(columns=[Column(col_from_right=1, text="甲塚乙")])])])
        self.assertEqual(compare_textlayer(pr, "甲塚乙")["items"], [])
        self.assertEqual(merge._sim("甲塚乙", "甲塚乙"), 1.0)

    def test_nfc_reading_keeps_variant(self):
        from yeone_ocr.cli import nfc_reading
        pr = nfc_reading(PageReading(pdf_page=1, page_label="塚", uncertain=[Uncertain(context="者")], regions=[
            Region(id="b", kind="본문", title_text="樂諸", paragraphs=[Paragraph(columns=[Column(col_from_right=1, text="甲塚​乙")])])]))
        self.assertEqual(pr.regions[0].paragraphs[0].columns[0].text, "甲塚乙"); self.assertEqual(pr.regions[0].title_text, "樂諸")
        self.assertEqual((pr.page_label, pr.uncertain[0].context), ("塚", "者"))


class NotationDiff(unittest.TestCase):
    """1c — 가로쓰기 한문 모드(FOLD_ZH): 접어서는 같은데 글자가 다른 1차·2차(後↔后)는 글도 표시도 바꾸지 않고 확인 항목 한 건으로 알린다."""
    def setUp(self):
        if not zh.available():
            self.skipTest("opencc 없음")

    def test_folded_equal_but_raw_different_is_noted(self):
        text, st, ctx = one_region("後體著，甲乙", "后体着,甲乙", fold_zh=True)
        self.assertEqual((text, st["marks"], st["notation_diffs"]), ("後體著，甲乙", 0, 3))        # 1차 글 그대로, 표시 없음, 부호 차이는 세지 않음
        self.assertEqual(len(ctx), 1); self.assertTrue(ctx[0].startswith(NOTATION_DIFF + " 3자: 後→后, 體→体, 著→着"), ctx[0]); self.assertIn("원본 확인", ctx[0])
        self.assertEqual(one_region("後體著，甲乙", "後體著,甲乙", fold_zh=True)[2], [])             # 부호만 다르면 알리지 않는다
        text2, st2, ctx2 = one_region("後體著乙", "后体着丙", fold_zh=True)                        # 다른 자리의 진짜 갈림이 표기 차이 알림을 끄지 않는다
        self.assertEqual((text2, st2["marks"], st2["notation_diffs"]), ("後體著〔乙/丙〕", 1, 3))
        self.assertEqual(ctx2[0], "「後體著〔乙/丙〕」"); self.assertTrue(ctx2[1].startswith(NOTATION_DIFF + " 3자: 後→后, 體→体, 著→着"), ctx2); self.assertEqual(len(ctx2), 2)
        _, st3, ctx3 = one_region("後體著，甲乙", "后体着,甲乙", fold_zh=False)                       # 세로쓰기 모드: 자형 차이는 표시로
        self.assertGreater(st3["marks"], 0); self.assertFalse(any(c.startswith(NOTATION_DIFF) for c in ctx3))

    def test_lists_at_most_five_pairs(self):
        p1 = PageReading(pdf_page=1, regions=[Region(id="b", kind="본문", paragraphs=[Paragraph(columns=[
            Column(col_from_right=1, text="後體著書"), Column(col_from_right=2, text="車東馬")])])])
        rr = {"b": RegionReread(columns=[Column(col_from_right=1, text="后体着书"), Column(col_from_right=2, text="车东马")])}
        old = merge.FOLD_ZH; merge.FOLD_ZH = True
        try:
            m, st = merge_passes(p1, rr)
        finally:
            merge.FOLD_ZH = old
        self.assertEqual((st["notation_diffs"], st["marks"], len(m.uncertain)), (7, 0, 1))
        u = m.uncertain[0]
        self.assertEqual((u.region_id, u.col_from_right, u.position), ("b", 1, "위")); self.assertEqual(u.context.count("→"), 5); self.assertIn(" …", u.context)
        self.assertEqual([c.text for c in m.regions[0].paragraphs[0].columns], ["後體著書", "車東馬"])

    def test_noted_beside_a_real_difference(self):
        """한 영역에 다른 갈림이 하나라도 있으면 표기 차이가 통째로 묻히던 문제 — 영역마다 한 건은 그대로 남는다."""
        p1 = PageReading(pdf_page=1, regions=[Region(id="b", kind="본문", paragraphs=[Paragraph(columns=[
            Column(col_from_right=1, text="後體著書"), Column(col_from_right=2, text="車東馬甲")])])])
        rr = {"b": RegionReread(columns=[Column(col_from_right=1, text="后体着书"), Column(col_from_right=2, text="车东马乙")])}
        old = merge.FOLD_ZH; merge.FOLD_ZH = True
        try:
            m, st = merge_passes(p1, rr)
        finally:
            merge.FOLD_ZH = old
        self.assertEqual([c.text for c in m.regions[0].paragraphs[0].columns], ["後體著書", "車東馬〔甲/乙〕"])
        self.assertEqual((st["notation_diffs"], st["marks"]), (7, 1))
        ctx = [u.context for u in m.uncertain]
        self.assertEqual(len(ctx), 2); self.assertEqual(ctx[0], "「車東馬〔甲/乙〕」"); self.assertTrue(ctx[1].startswith(NOTATION_DIFF + " 7자"), ctx)
        self.assertEqual((m.uncertain[1].col_from_right, m.uncertain[1].position), (1, "위"))


if __name__ == "__main__":
    unittest.main()
