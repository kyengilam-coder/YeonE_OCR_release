"""0.6.8 — 2차 조각 차례 검사: 묶음 응답이 같은 영역 안에서 조각 번호를 바꾸거나 한 조각을 두 번 돌려주면 낱개로 다시 읽어 가린다.
조각은 늘 그림 차례대로 — 1차 글 안 위치만으로 옮기지 않는다(1차의 열 차례가 틀렸으면 그대로 드러나야 한다).
모델 없이 돌아간다(가짜 판독기, split_bands 를 바꿔 조각 수를 정한다)."""
import contextlib, io, sys, unittest
from argparse import Namespace
from pathlib import Path
from unittest import mock
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PIL import Image
from yeone_ocr import cli
from yeone_ocr.cli import _chunk_check, _flat, _locate, _second_pass
from yeone_ocr.reader_gemini import ReaderError
from yeone_ocr.schema import PageReading, Region, Paragraph, Column, RegionReread, region_text

TEXT = "天地玄黃宇宙洪荒日月盈昃辰宿列張寒來暑往秋收冬藏閏餘成歲律呂調陽雲騰致雨露結爲霜金生麗水玉出崑岡劍號巨闕珠稱夜光果珍李柰菜重芥薑海鹹河淡鱗潛羽翔龍師火帝鳥官人皇始制文字乃服衣裳推位讓國有虞陶唐弔民伐罪周發殷湯坐朝問道垂拱平章愛育黎首臣伏戎羌遐邇壹體率賓歸王"
COLS = [TEXT[k:k + 16] for k in range(0, 128, 16)]          # 8열 × 16자
CHUNK = {k: COLS[2 * k - 2:2 * k] for k in range(1, 5)}         # 조각 k = 열 2k-1, 2k


def _page(cols=COLS):
    p1 = PageReading(pdf_page=7, regions=[Region(id="b", kind="본문", bbox=[100, 100, 900, 900],
                                                 paragraphs=[Paragraph(columns=[Column(col_from_right=i + 1, text=t) for i, t in enumerate(cols)])])])
    return Image.new("L", (400, 560), 255), p1


def _four_bands(im, axis="y", max_bands=8, reverse=False, **kw):
    w = im.width if axis == "x" else im.height
    return [(k * w // 4, (k + 1) * w // 4) for k in range(4)]


def _rr(cols):
    return RegionReread(columns=[Column(col_from_right=i + 1, text=t) for i, t in enumerate(cols)])


class Fake:
    """묶음 응답에서 조각 id → 돌려줄 조각 번호(answer), 빠뜨릴 조각(missing), 엉뚱한 글(garbage). 낱개 호출은 제 조각 글(fail 은 일시 오류).
    chunks: 조각 번호 → 그 그림의 열 글(기본 CHUNK)."""
    def __init__(self, answer=None, missing=(), garbage=(), fail=(), chunks=None):
        self.answer = answer or {}; self.missing = set(missing); self.garbage = set(garbage); self.fail = set(fail)
        self.batches = []; self.single = []; self.chunk = chunks or CHUNK

    def reread_regions(self, items, n):
        self.batches.append([rid for rid, _, _ in items]); out = {}
        for rid, _, _ in items:
            if rid in self.missing:
                continue
            k = self.answer.get(rid, int(rid.split("#")[1]))
            out[rid] = _rr(["ABCDEFGHIJKLMNOP", "QRSTUVWXYZabcdef"] if rid in self.garbage else self.chunk[k])
        return out

    def reread_region(self, im, kind, rid, n):
        self.single.append(rid)
        if rid in self.fail:
            raise ReaderError("3회 시도 뒤에도 실패: 형식", fatal=False)
        return _rr(self.chunk[int(rid.split("#")[1])])


def run(fake, mode="batch", cols=COLS, full=False):
    im, p1 = _page(cols); before = region_text(p1.regions[0]); buf = io.StringIO()
    with mock.patch.object(cli, "split_bands", _four_bands), contextlib.redirect_stdout(buf):
        rr, meta = _second_pass(fake, im, p1, 7, Namespace(chunk_lines=8, second_mode=mode, batch_crops=8))
    assert region_text(p1.regions[0]) == before                    # 1차 글은 기준으로만 — 바뀌지 않는다
    got = [(c.col_from_right, c.text) for c in rr["b"].columns] if "b" in rr else []
    return (got, meta["b"], buf.getvalue()) + ((rr, p1) if full else ())


RIGHT = [(i + 1, t) for i, t in enumerate(COLS)]
NOTE = cli.CHUNK_ORDER_NOTE


class ChunkOrder(unittest.TestCase):
    """_second_pass: 겹쳐 읽었거나 차례가 1차 글과 다른 조각은 낱개로 다시 읽어 가린다. 조각은 1차 위치만으로 옮기지 않는다."""
    def test_in_order_untouched(self):
        fake = Fake(); cols, meta, out = run(fake)
        self.assertEqual(cols, RIGHT); self.assertEqual(fake.batches, [["b#1", "b#2", "b#3", "b#4"]]); self.assertEqual(fake.single, [])
        self.assertNotIn("chunk_fix", meta); self.assertNotIn("조각", out)

    def test_swapped_chunks_reread_singly(self):
        # 묶음이 조각 2·3 번호를 바꿈 — 1차 위치로 옮기지 않고 둘 다 낱개로(낱개 응답은 번호가 섞일 수 없다)
        fake = Fake(answer={"b#2": 3, "b#3": 2}); cols, meta, out, rr, _ = run(fake, full=True)
        self.assertEqual(cols, RIGHT); self.assertEqual(fake.single, ["b#2", "b#3"])
        self.assertIn("    7쪽: b 조각 2·3 응답 차례가 1차 글과 다름 → 낱개로 다시 읽어 가림", out)
        self.assertIn("    7쪽: b 조각 차례 — 낱개 판독은 1차 글 차례와 맞음(묶음 응답이 조각을 섞었음)", out)
        self.assertEqual((meta["chunk_fix"], meta["chunks_read"]), (["순서 2·3 낱개로"], 4))
        self.assertFalse(any(NOTE in u.context for u in rr["b"].uncertain)); self.assertNotIn("바로잡음", out)

    def test_reordered_region_merges_without_long_segments(self):
        """차례가 뒤바뀐 2차를 그대로 이어 붙이면 병합이 긴 구간('1차를 따름')으로 넘겨 그 열의 1차 오독이 검사 없이 들어간다."""
        from yeone_ocr.merge import merge_passes
        im, p1 = _page(); p1.regions[0].paragraphs[0].columns[4].text = COLS[4][:5] + "王" + COLS[4][6:]     # 1차 오독 하나(조각 3 안)
        with mock.patch.object(cli, "split_bands", _four_bands), contextlib.redirect_stdout(io.StringIO()):
            rr, _ = _second_pass(Fake(answer={"b#2": 3, "b#3": 2}), im, p1, 7, Namespace(chunk_lines=8, second_mode="batch", batch_crops=8))
        m, st = merge_passes(p1, rr)
        self.assertEqual((st["long_segments"], st["marks"]), (0, 1))
        self.assertEqual(m.regions[0].paragraphs[0].columns[4].text, COLS[4][:5] + f"〔王/{COLS[4][5]}〕" + COLS[4][6:])

    def test_rotated_chunks_reread_singly(self):
        fake = Fake(answer={"b#2": 3, "b#3": 4, "b#4": 2}); cols, meta, out = run(fake)
        self.assertEqual(cols, RIGHT); self.assertEqual(fake.single, ["b#2", "b#3", "b#4"])
        self.assertIn("조각 2·3·4 응답 차례가 1차 글과 다름", out)

    def test_missing_chunk_then_swap(self):
        fake = Fake(answer={"b#3": 4, "b#4": 3}, missing={"b#2"}); cols, meta, out = run(fake)
        self.assertEqual(cols, RIGHT); self.assertEqual(fake.single, ["b#2", "b#3", "b#4"]); self.assertIn("조각 3·4 응답 차례가", out)

    def test_duplicate_chunk_reread(self):
        fake = Fake(answer={"b#3": 2}); cols, meta, out = run(fake)                       # 조각 2 의 글이 두 번
        self.assertEqual(cols, RIGHT); self.assertEqual(fake.single, ["b#2", "b#3"])        # 어느 쪽이 맞는지 모르니 둘 다 낱개로
        self.assertIn("b 조각 2·3 응답이 1차의 같은 자리를 겹쳐 읽음 → 낱개로 다시 읽음", out)
        self.assertEqual(meta["chunk_fix"], ["겹침 2·3 낱개로"])

    def test_shifted_reply_after_missing_chunk(self):
        # 조각 2 응답이 빠지고 뒤 조각이 한 칸씩 밀림 — 낱개로 읽은 조각 2 와 겹치는 묶음 응답만 차례로 다시 읽는다
        fake = Fake(answer={"b#3": 2, "b#4": 3}, missing={"b#2"}); cols, meta, out = run(fake)
        self.assertEqual(cols, RIGHT); self.assertEqual(fake.single, ["b#2", "b#3", "b#4"])

    def test_unlocatable_chunk_left_alone(self):
        # 조각 1 응답이 1차와 맞지 않아 자리를 모른다 → 차례가 어긋난 조각만 낱개로
        fake = Fake(answer={"b#2": 3, "b#3": 2}, garbage={"b#1"}); cols, meta, out = run(fake)
        self.assertEqual(fake.single, ["b#2", "b#3"])
        self.assertEqual(cols[2:], RIGHT[2:])                                               # 조각 1(엉뚱한 글)은 그대로 — merge 가 긴 구간으로 알린다
        self.assertIn("응답 차례가 1차 글과 다름 → 낱개로 다시 읽어 가림", out)

    def test_reread_failure_marks_region(self):
        fake = Fake(answer={"b#3": 2}, fail={"b#3"}); cols, meta, out = run(fake)
        self.assertTrue(meta["pass2_failed"].startswith("3회 시도")); self.assertEqual(meta["chunks_read"], 3)
        self.assertEqual(cols, [(i + 1, t) for i, t in enumerate(CHUNK[1] + CHUNK[2] + CHUNK[4])])     # 겹친 응답은 버렸다(조각 2 는 낱개로 다시 읽은 것)
        self.assertIn("b#3 2차 판독 실패(3회 시도", out)

    def test_each_mode_not_checked(self):
        fake = Fake(); cols, meta, out = run(fake, mode="each")
        self.assertEqual(cols, RIGHT); self.assertEqual(fake.batches, []); self.assertEqual(len(fake.single), 4)


class Pass1Order(unittest.TestCase):
    """chunks-merge-1: 1차가 열을 틀린 차례로 적었고 묶음은 맞게 답했다 — 2차를 1차 차례로 옮겨 1차의 차례 오류를 가리면 안 된다."""
    WRONG = COLS[0:2] + COLS[4:6] + COLS[2:4] + COLS[6:8]          # 1차가 그림의 열 5·6 을 3·4 앞에 적음

    def test_pass1_misorder_not_copied_into_pass2(self):
        from yeone_ocr.merge import merge_passes
        fake = Fake(); cols, meta, out, rr, p1 = run(fake, cols=self.WRONG, full=True)
        self.assertEqual(cols, RIGHT)                                                       # 2차는 그림 차례 그대로(1차 차례로 옮기지 않음)
        self.assertEqual(fake.single, ["b#2", "b#3"])                                       # 갈리는 두 조각만 낱개로 한 번씩
        self.assertEqual(meta["chunk_fix"], ["순서 2·3 낱개로", "1차 차례 다름 2·3"])
        self.assertNotIn("바로잡음", out)
        self.assertIn("    7쪽: b 조각 2·3 낱개로 읽어도 차례가 1차 글과 다름 → 1차 판독의 글 차례를 의심(조각은 그림 차례대로 비교, 확인 목록에)", out)
        note = [u for u in rr["b"].uncertain if u.context.startswith(NOTE)]
        self.assertEqual(len(note), 1); self.assertIn("조각 2·3", note[0].context); self.assertEqual(note[0].region_id, "b")
        m, st = merge_passes(p1, rr)
        self.assertGreaterEqual(st["long_segments"], 1)                                     # 병합이 차례가 다른 구간을 알린다(본문은 1차 그대로)
        self.assertTrue(any(u.context.startswith(NOTE) for u in m.uncertain))
        self.assertEqual("".join(c.text for c in m.regions[0].paragraphs[0].columns), "".join(self.WRONG))

    def test_batch_and_each_report_the_same(self):
        from yeone_ocr.merge import merge_passes
        got = {}
        for mode in ("batch", "each"):
            _, _, _, rr, p1 = run(Fake(), mode=mode, cols=self.WRONG, full=True)
            m, st = merge_passes(p1, rr); got[mode] = (st["long_segments"], sorted(u.context for u in m.uncertain))
        self.assertEqual(got["batch"], got["each"]); self.assertGreaterEqual(got["each"][0], 1)

    def test_reversed_pass1_each_chunk_read_once(self):
        # 1차가 조각 차례를 통째로 거꾸로 — 모든 조각이 갈려도 낱개 호출은 조각마다 한 번까지
        wrong = COLS[6:8] + COLS[4:6] + COLS[2:4] + COLS[0:2]
        fake = Fake(); cols, meta, out = run(fake, cols=wrong)
        self.assertEqual(cols, RIGHT); self.assertEqual(sorted(fake.single), ["b#1", "b#2", "b#3", "b#4"])
        self.assertIn("1차 차례 다름 1·2·3·4", meta["chunk_fix"])

    def test_misorder_with_failed_single(self):
        # 갈린 조각의 낱개 재판독이 실패: 묶음 응답이 맞는지 가릴 수 없어 버리고 2차 실패로 남긴다(다음 실행이 다시)
        fake = Fake(fail={"b#3"}); cols, meta, out = run(fake, cols=self.WRONG)
        self.assertEqual(cols, [(i + 1, t) for i, t in enumerate(CHUNK[1] + CHUNK[2] + CHUNK[4])])
        self.assertTrue(meta["pass2_failed"].startswith("3회 시도")); self.assertEqual(meta["chunks_read"], 3)


class NeedlessReread(unittest.TestCase):
    """chunks-merge-2: 2차에 1차가 빠뜨린 글이 있어 자리가 겹쳐 보여도, 글이 서로 닮지 않으면 같은 조각을 두 번 읽은 것이 아니다."""
    def test_pass1_dropped_column(self):
        for drop in range(1, 7):                                                            # 안쪽 열 하나를 1차가 빠뜨림
            with self.subTest(drop=drop):
                fake = Fake(); cols, meta, out = run(fake, cols=COLS[:drop] + COLS[drop + 1:])
                self.assertEqual((cols, fake.single), (RIGHT, [])); self.assertNotIn("chunk_fix", meta)

    def test_pass1_dropped_notes(self):
        for drop in (2, 3, 4, 5):                                                           # 1차가 열마다 작은 주를 빠뜨림(2차 글이 14~45% 더 김)
            with self.subTest(drop=drop):
                fake = Fake(); cols, meta, out = run(fake, cols=[c[:8] + c[8 + drop:] for c in COLS])
                self.assertEqual((cols, fake.single), (RIGHT, [])); self.assertNotIn("chunk_fix", meta)

    def test_repeated_passage(self):
        # 조각 3 이 조각 1 의 글을 되풀이하는 쪽(후렴·되풀이 문구) — 1차·묶음 모두 맞게 읽었다
        rep = COLS[0:2] + COLS[2:4] + COLS[0:2] + COLS[6:8]; chunks = {k: rep[2 * k - 2:2 * k] for k in range(1, 5)}
        fake = Fake(chunks=chunks); cols, meta, out = run(fake, cols=rep)
        self.assertEqual((cols, fake.single), ([(i + 1, t) for i, t in enumerate(rep)], [])); self.assertNotIn("chunk_fix", meta)


class FailedReread(unittest.TestCase):
    """chunks-merge-3: 낱개 재판독이 실패해도, 짝의 낱개 판독으로 겹침이 풀린 묶음 응답은 버리지 않는다."""
    def test_partner_single_clears_duplicate(self):
        # 묶음이 조각 2 자리에 조각 3 의 글을 줌(조각 3 응답은 맞음) — 둘 다 낱개로 묻는데 조각 3 만 실패
        fake = Fake(answer={"b#2": 3}, fail={"b#3"}); cols, meta, out = run(fake)
        self.assertEqual(fake.single, ["b#2", "b#3"]); self.assertEqual(cols, RIGHT)
        self.assertNotIn("pass2_failed", meta); self.assertEqual(meta["chunks_read"], 4)
        self.assertIn("    7쪽: b#3 낱개 2차 판독 실패(3회 시도 뒤에도 실패: 형식) → 다시 검사한 묶음 응답을 그대로 씀", out)

    def test_dropped_column_failure_never_reached(self):
        # 1차가 열 4 를 빠뜨렸고 묶음은 다 맞음 — 다시 읽을 까닭이 없으니 낱개 호출의 일시 오류로 맞는 응답을 잃지 않는다
        fake = Fake(fail={"b#3"}); cols, meta, out = run(fake, cols=COLS[:3] + COLS[4:])
        self.assertEqual((cols, fake.single), (RIGHT, [])); self.assertNotIn("pass2_failed", meta); self.assertEqual(meta["chunks_read"], 4)


class ChunkCheck(unittest.TestCase):
    """_locate·_chunk_check 단위 — 조각 차례를 돌려주지 않는다(옮기지 않음). 낱개로 읽은 조각은 다시 읽지 않는다."""
    REF = _flat("".join(COLS))
    T = {f"c{k}": _flat("".join(CHUNK[k])) for k in range(1, 5)}

    def test_locate_ignores_stray_pair(self):
        whole = "甲乙丙丁戊己庚辛壬癸" + "天地玄黃宇宙洪荒日月盈昃"
        own, at = _locate("甲乙天地玄黃宇宙洪荒日月盈昃", whole)
        self.assertEqual((own, at), (1.0, 8))                                              # 앞의 우연한 두 글자가 아니라 큰 묶음의 자리
        self.assertEqual(_locate("", whole), (0.0, 0)); self.assertEqual(_locate("ABCDEF", whole), (0.0, 0))

    def test_order_never_from_pass1_alone(self):
        ids = ["c1", "c2", "c3"]; texts = {"c1": self.T["c1"], "c2": self.T["c3"], "c3": self.T["c2"]}
        self.assertEqual(_chunk_check(ids, texts, self.REF, set()), (["c2", "c3"], "순서", ["c2", "c3"]))   # 갈린 두 조각 모두 낱개로
        self.assertEqual(_chunk_check(ids, texts, self.REF, {"c2"}), (["c3"], "순서", ["c2", "c3"]))       # 낱개로 읽은 c2 는 다시 읽지 않는다
        self.assertEqual(_chunk_check(ids, texts, self.REF, {"c2", "c3"}), ([], "순서", ["c2", "c3"]))     # 둘 다 낱개인데도 다름 = 1차 차례가 다름

    def test_duplicate_with_trusted(self):
        ids = ["c1", "c2", "c3"]; texts = {"c1": self.T["c1"], "c2": self.T["c2"], "c3": self.T["c2"]}
        self.assertEqual(_chunk_check(ids, texts, self.REF, set()), (["c2", "c3"], "겹침", ["c2", "c3"]))
        self.assertEqual(_chunk_check(ids, texts, self.REF, {"c2"}), (["c3"], "겹침", ["c2", "c3"]))
        self.assertEqual(_chunk_check(ids, texts, self.REF, {"c2", "c3"}), ([], "", []))   # 둘 다 낱개 — 같은 자리라 차례 다툼도 아님

    def test_overlap_needs_similar_text(self):
        # 2차에만 있는 글(1차가 빠뜨린 열) 때문에 자리가 겹쳐 보여도 글이 다르면 겹침이 아니다
        ref = _flat("".join(COLS[:3] + COLS[4:]))
        self.assertEqual(_chunk_check(["c1", "c2", "c3", "c4"], self.T, ref, set()), ([], "", []))

    def test_short_or_unmatched_chunks_are_not_judged(self):
        ids = ["c1", "c2"]
        self.assertEqual(_chunk_check(ids, {"c1": self.T["c2"], "c2": "天地"}, self.REF, set()), ([], "", []))
        self.assertEqual(_chunk_check(ids, {"c1": self.T["c2"], "c2": self.T["c1"]}, "", set()), ([], "", []))
        marks = _flat("〔疑: 天〕地玄〔黃/黄〕宇宙 洪荒〔판독불명〕")                     # 표시는 알맹이로, 공백 없이
        self.assertEqual(marks, "天地玄黃宇宙洪荒")


if __name__ == "__main__":
    unittest.main()
