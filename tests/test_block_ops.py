"""Block 의 글자 자리 셈(cut·extend·joined·remap·anchor_map, clip_ranges·shift_ranges): 예전에 structure·blocks·normalization 에 따로 있던 셈
(아래 _old_*, 0.7.4 그대로 옮겨 둠)과 무작위 블록 300개에서 같은 값을 내는지."""
import random
import sys
import unittest
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from yeone_ocr.blocks import Block, anchor_map, clip_ranges, shift_ranges   # noqa: E402


# ---------- 예전 셈(비교 기준) ----------
def _old_split(b, cut, rest):                       # structure._split
    def rng(rs, lo, hi):
        return [(max(a, lo) - lo, min(e, hi) - lo, k) for a, e, k in rs if a < hi and e > lo]
    n = len(b.text)
    head = replace(b, text=b.text[:cut], spans=rng(b.spans, 0, cut), cstyles=rng(b.cstyles, 0, cut),
                   memos=[(min(o, cut - 1), m) for o, m in b.memos if o < cut], anchors=[(o, a) for o, a in b.anchors if o < cut])
    tail = replace(b, text=b.text[rest:], spans=rng(b.spans, rest, n), cstyles=rng(b.cstyles, rest, n),
                   memos=[(max(0, o - rest), m) for o, m in b.memos if o >= cut], anchors=[(max(0, o - rest), a) for o, a in b.anchors if o >= cut])
    return head, tail


def _old_join(a, b):                                # structure._join
    k = len(a.text)
    sh = lambda rs: [(x + k, y + k, z) for x, y, z in rs]
    return replace(b, text=a.text + b.text, spans=list(a.spans) + sh(b.spans), cstyles=list(a.cstyles) + sh(b.cstyles),
                   memos=list(a.memos) + [(o + k, m) for o, m in b.memos], anchors=list(a.anchors) + [(o + k, t) for o, t in b.anchors])


def _old_remap(block, text, origins):               # normalization.normalize_blocks 의 블록 갈래
    block.text = text
    mapped = {}
    for new, source in enumerate(origins):
        mapped.setdefault(source, []).append(new)
    spans = []
    for begin, end, kind in block.spans:
        covered = [new for source in range(begin, end) for new in mapped.get(source, [])]
        if covered:
            spans.append((min(covered), max(covered) + 1, kind))
    block.spans = spans
    block.memos = [(mapped[offset][0] if offset in mapped else min(len(block.text) - 1, max(0, offset)), message) for offset, message in block.memos]
    cst = []
    for begin, end, kind in block.cstyles:
        covered = [new for source in range(begin, end) for new in mapped.get(source, [])]
        if covered:
            cst.append((min(covered), max(covered) + 1, kind))
    block.cstyles = cst
    block.anchors = [(min((new for source, news in mapped.items() if source >= offset for new in news), default=len(block.text)), label)
                     for offset, label in block.anchors]


def _old_bold_of(rs, raw, t, shift):                # blocks.assemble_blocks.bold_of
    lead = len(raw) - len(raw.lstrip()); out = []
    for a, b, k in rs:
        if a == b and lead <= a <= lead + len(t):
            out.append((a - lead + shift, a - lead + shift, k))
        elif a < b and a < lead + len(t) and b > lead:
            out.append((max(lead, a) - lead + shift, min(b, lead + len(t)) - lead + shift, k))
    return out


def _old_amap(anchors, text):                       # 두 작성기의 amap
    amap = {}
    for o, label in anchors:
        amap.setdefault(min(max(0, o), len(text)), []).append(label)
    return amap


CHARS = "甲乙丙丁　 〔入/人〕味辛，"


def rand_block(rng: random.Random) -> Block:
    n = rng.randint(0, 20)
    text = "".join(rng.choice(CHARS) for _ in range(n))

    def ranges(kinds):
        out = []
        for _ in range(rng.randint(0, 4)):
            a = rng.randint(0, n); e = a if rng.random() < 0.25 else rng.randint(a, n)   # 넓이 없는 자리(틈)도
            out.append((a, e, rng.choice(kinds)))
        return out
    memos = [(rng.randint(0, max(0, n - 1)), f"m{i}") for i in range(rng.randint(0, 3))]
    anchors = [(rng.choice([0, n, rng.randint(0, n)]), f"PDF {i}쪽") for i in range(rng.randint(0, 3))]   # 끝 자리 책갈피도
    return Block(type=rng.choice(["para", "heading"]), text=text, kind=rng.choice(["본문", "소주"]), role=rng.choice(["", "body", "entry"]),
                 page=rng.randint(1, 9), level=rng.randint(0, 3), spans=ranges(["green", "blue", "red_ctx"]), memos=memos,
                 cstyles=ranges(["bold", "gap", "source", "bonjing"]), anchors=anchors)


class BlockOps(unittest.TestCase):
    def test_cut_join_remap_match_old_arithmetic(self):
        rng = random.Random(20261015)
        for i in range(300):
            b = rand_block(rng); n = len(b.text)
            with self.subTest(i=i, block=b):
                cut = rng.randint(0, n); rest = rng.randint(cut, n)
                self.assertEqual(b.cut(cut, rest), _old_split(b, cut, rest))
                self.assertEqual(b.cut(cut), _old_split(b, cut, cut))
                c = rand_block(rng)
                self.assertEqual(Block.joined(b, c), _old_join(b, c))
                self.assertEqual(b.anchor_map(), _old_amap(b.anchors, b.text))
                self.assertEqual(anchor_map(b.anchors, n + 3), _old_amap(b.anchors, b.text + "〔고"))
                # 정리 뒤 자리 옮기기: 단조 증가·늘고 주는 자리 대응(부호 뒤 빈칸 넣기·두 글자 → 한 글자)
                origins = sorted(rng.choice(range(n)) for _ in range(rng.randint(0, n + 4))) if n else []
                new_text = "".join(rng.choice(CHARS) for _ in origins)
                x, y = replace(b, spans=list(b.spans), memos=list(b.memos), cstyles=list(b.cstyles), anchors=list(b.anchors)), replace(b)
                x.remap(new_text, origins); _old_remap(y, new_text, origins)
                self.assertEqual(x, y)
                pad = rng.randint(0, 3); raw = " " * pad + b.text; lead = len(raw) - len(raw.lstrip())   # 판독 문단 글(앞 빈칸) → 잘라 낸 글
                t = raw.strip(); shift = rng.randint(0, 3)
                raw_rs = [(a + pad, e + pad, k) for a, e, k in b.cstyles] + [(rng.randint(0, len(raw)), rng.randint(0, len(raw)), "bold")]
                self.assertEqual(shift_ranges(clip_ranges(raw_rs, lead, lead + len(t), points="inclusive"), shift), _old_bold_of(raw_rs, raw, t, shift))

    def test_remap_with_real_normalization_origins(self):
        from yeone_ocr.normalization import _prepare_text, _opencc_converter
        conv = _opencc_converter()
        rng = random.Random(7)
        for text in ("香薷　味辛，微温。主霍乱腹痛吐下，散水肿。", "［陶隐居云］处处有此,用之.　唐附", "大黄〔入/人〕肝经主之。云云", ""):
            res = _prepare_text(text, conv)
            for _ in range(20):
                b = rand_block(rng); b.text = text; n = len(text)
                b.spans = [(a % (n + 1), max(a % (n + 1), e % (n + 1)), k) for a, e, k in b.spans]
                b.cstyles = [(a % (n + 1), max(a % (n + 1), e % (n + 1)), k) for a, e, k in b.cstyles]
                b.memos = [(o % (n + 1), m) for o, m in b.memos]; b.anchors = [(o % (n + 1), a) for o, a in b.anchors]
                x, y = replace(b, spans=list(b.spans), memos=list(b.memos), cstyles=list(b.cstyles), anchors=list(b.anchors)), replace(b)
                x.remap(res.text, res.origins); _old_remap(y, res.text, res.origins)
                self.assertEqual(x, y)

    def test_extend_and_clip_edges(self):
        b = Block(type="para", text="甲乙", spans=[(0, 1, "green")], anchors=[(0, "PDF 1쪽")])
        self.assertEqual(b.extend("〔→PDF 2쪽〕"), 2)
        base = b.extend("丙丁", spans=[(0, 2, "blue")], memos=[(1, "m")], cstyles=[(1, 1, "gap")], anchors=[(0, "PDF 2쪽")])
        self.assertEqual((base, b.text), (11, "甲乙〔→PDF 2쪽〕丙丁"))
        self.assertEqual((b.spans, b.memos, b.cstyles, b.anchors), ([(0, 1, "green"), (11, 13, "blue")], [(12, "m")], [(12, 12, "gap")], [(0, "PDF 1쪽"), (11, "PDF 2쪽")]))
        rs = [(0, 0, "gap"), (3, 3, "gap"), (5, 5, "gap"), (4, 2, "bold"), (1, 9, "bold")]
        self.assertEqual(clip_ranges(rs, 0, 5), [(3, 3, "gap"), (1, 5, "bold")])                     # 끝 자리·첫 자리 틈은 빼고, 거꾸로 된 범위는 버림
        self.assertEqual(clip_ranges(rs, 0, 5, points="inclusive"), [(0, 0, "gap"), (3, 3, "gap"), (5, 5, "gap"), (1, 5, "bold")])
        with self.assertRaises(ValueError):
            clip_ranges(rs, 0, 5, points="both")


if __name__ == "__main__":
    unittest.main()
