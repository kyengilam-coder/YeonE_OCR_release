"""판독 표시 〔…〕 한곳(yeone_ocr/marks.py): 모듈마다 있던 예전 사본(아래 _old_*, 0.7.4 그대로 옮겨 둠)과 같은 값을 내는지.
뜻을 맞춘 곳(등급·글자층 대조·책갈피 교정이 〔疑：X〕(전각 쌍점)·앞뒤 빈칸 있는 표시를 3차 판별과 같게 읽음)만 새 뜻으로 견준다."""
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from yeone_ocr import marks   # noqa: E402
from yeone_ocr.marks import MARK_RE, fold, fold_map, mark_kind, mark_reading, split_marks   # noqa: E402

_MARK = re.compile(r"〔([^〔〕]*)〕")


# ---------- 예전 사본(비교 기준) ----------
def _old_mark_reading(inner):                      # blocks.mark_reading
    inner = inner.strip()
    if inner.startswith(("疑:", "疑：")):
        return inner[2:].strip()
    if "/" in inner:
        return inner.split("/")[0]
    return "" if inner == "판독불명" else inner


def _old_first_reading(text):                      # blocks.first_reading
    return _MARK.sub(lambda m: _old_mark_reading(m.group(1)), text or "")


def _old_fold_marks(text):                         # blocks.fold_marks
    out = []; pos = []; i = 0
    for m in _MARK.finditer(text or ""):
        out.extend(text[i:m.start()]); pos.extend(range(i, m.start()))
        rep = _old_mark_reading(m.group(1))
        out.extend(rep); pos.extend([m.start()] * len(rep))
        i = m.end()
    out.extend((text or "")[i:]); pos.extend(range(i, len(text or "")))
    pos.append(len(text or ""))
    return "".join(out), pos


def _old_plain(text):                              # adjudicate.plain
    def rep(m):
        inner = m.group(0)[1:-1].strip()
        if inner.startswith(("疑:", "疑：")):
            return inner[2:].strip()
        if "/" in inner:
            return inner.split("/")[0]
        return ""
    return MARK_RE.sub(rep, text)


def _old_plain_map(text):                          # grades._plain_map
    out = []; pos = []; last = 0
    for m in MARK_RE.finditer(text):
        for i in range(last, m.start()):
            out.append(text[i]); pos.append(i)
        inner = m.group(0)[1:-1]
        core = inner[2:].strip() if inner.startswith("疑:") else (inner.split("/")[0] if "/" in inner else "")
        out.extend(core); pos.extend([None] * len(core))
        last = m.end()
    for i in range(last, len(text)):
        out.append(text[i]); pos.append(i)
    return "".join(out), pos


def _old_compare(s):                               # merge.strip_marks_for_compare
    def rep(m):
        inner = m.group(0)[1:-1]
        if inner.startswith("疑:"):
            return inner[2:].strip()
        if "/" in inner and not inner.startswith(("→", "소주", "작업", "판독 메모", "도판", "이어짐", "원본")):
            return inner.split("/")[0]
        return ""
    return MARK_RE.sub(rep, s)


def _old_collate_strip(text):                      # collate.strip_marks
    pieces = []; spans = []; last = 0; n = 0
    for m in MARK_RE.finditer(text):
        inner = m.group(0)[1:-1]
        if inner.startswith("疑:"):
            core = inner[2:].strip()
        elif "/" in inner and not inner.startswith(("→", "소주", "작업", "판독 메모", "도판", "이어짐", "원본")):
            core = inner.split("/")[0]
        else:
            core = ""
        pieces.append(text[last:m.start()]); n += m.start() - last
        pieces.append(core); spans.append((n - 1, n + len(core) + 1)); n += len(core)
        last = m.end()
    pieces.append(text[last:])
    return "".join(pieces), spans


def _old_token_positions(text):                    # textlayer.token_positions
    text = text or ""
    out = []; pos = 0
    for m in _MARK.finditer(text):
        out.extend((c, pos + k) for k, c in enumerate(text[pos:m.start()]) if not c.isspace())
        inner = m.group(1).strip()
        if inner.startswith("疑:") or inner.startswith("疑："):
            out.extend((c, m.start()) for c in inner[2:] if not c.isspace())
        elif "/" in inner:
            out.extend((c, m.start()) for c in inner.split("/")[0] if not c.isspace())
        elif inner == "판독불명":
            out.append((None, m.start()))
        pos = m.end()
    out.extend((c, pos + k) for k, c in enumerate(text[pos:]) if not c.isspace())
    return out


def _old_split_marks(text):                        # grades.split_marks
    out = []
    for seg in re.split(r"(〔[^〔〕]*〕)", text):
        if not seg:
            continue
        if seg.startswith("〔") and seg.endswith("〕"):
            out.append((seg, "note" if seg.startswith(("〔→", "〔소주〕", "〔작업 메모〕", "〔판독 메모〕", "〔도판 미삽입", "〔이어짐〕", "〔고지: ")) else "mark"))
        else:
            out.append((seg, "body"))
    return out


MARKS = ["〔入/人〕", "〔日一/旦〕", "〔疑: 龍〕", "〔疑：龍〕", "〔 疑: 龍〕", "〔판독불명〕", "〔원본 결락〕", "〔소주〕", "〔→PDF 2쪽/인쇄 3쪽〕",
         "〔소주/소추〕", "〔 a/b〕", "〔〕", "〔疑:〕", "〔 판독불명 〕", "〔고지: 범례〕", "〔고지/구자〕", "〔疑: a/b〕", "〔/〕"]
CORPUS = ["", "天地玄黃", "甲 乙　丙", "甲〔乙", "甲〕乙〔", "〔〔入/人〕〕"] + [f"上{m}下" for m in MARKS] + \
         ["".join(MARKS), "大〔入/人〕〔疑: 龍〕肝 〔판독불명〕經〔원본 결락〕主之", "  〔 疑：龍 〕骨 〔 入 / 人 〕"]
def aligned(t: str) -> bool:
    """예전 등급·글자층 대조·교정이 다르게 읽던 표시(전각 쌍점·앞뒤 빈칸)가 든 글."""
    return any(m.group(0)[1:-1] != m.group(0)[1:-1].strip() or m.group(0)[1:-1].strip().startswith("疑：") for m in MARK_RE.finditer(t))


class Marks(unittest.TestCase):
    def test_kinds_and_readings(self):
        kinds = {m: mark_kind(m[1:-1]) for m in MARKS}
        self.assertEqual([kinds[m] for m in ("〔入/人〕", "〔疑：龍〕", "〔 疑: 龍〕", "〔판독불명〕", "〔원본 결락〕", "〔→PDF 2쪽/인쇄 3쪽〕", "〔疑: a/b〕")],
                         ["ab", "doubt", "doubt", "unknown", "other", "ab", "doubt"])
        self.assertEqual(mark_kind("→PDF 2쪽/인쇄 3쪽", work_notes=True), "other")
        self.assertEqual(mark_kind("소주/소추", work_notes=True), "other")       # 작업 표시 머리로 시작하면(예전 merge·collate 와 같게)
        self.assertEqual([mark_reading(m[1:-1]) for m in ("〔日一/旦〕", "〔 疑: 龍 〕", "〔원본 결락〕")], ["日一", "龍", ""])
        self.assertEqual(mark_reading(" 원본 결락 ", keep_other=True), "원본 결락")
        self.assertEqual(fold(None), ""); self.assertEqual(fold_map(None), ("", [0]))
        self.assertIs(marks.NOTE_PREFIXES, __import__("yeone_ocr.grades", fromlist=["NOTE_PREFIXES"]).NOTE_PREFIXES)

    def test_same_as_old_copies(self):
        for t in CORPUS:
            with self.subTest(t=t):
                self.assertEqual(fold(t, keep_other=True), _old_first_reading(t))
                self.assertEqual(fold_map(t, keep_other=True), _old_fold_marks(t))
                self.assertEqual(fold(t), _old_plain(t))
                self.assertEqual(split_marks(t), _old_split_marks(t))
                from yeone_ocr.textlayer import token_positions
                self.assertEqual(token_positions(t), _old_token_positions(t))
                from yeone_ocr.adjudicate import plain
                self.assertEqual(plain(t), _old_plain(t))
                if not aligned(t):
                    self.assertEqual(fold_map(t, inner_pos=False)[0], _old_plain_map(t)[0])
                    self.assertEqual(fold_map(t, inner_pos=False)[1][:-1], _old_plain_map(t)[1])
                    from yeone_ocr.merge import strip_marks_for_compare
                    self.assertEqual(strip_marks_for_compare(t), _old_compare(t))
                    from yeone_ocr.collate.collate import strip_marks
                    self.assertEqual(strip_marks(t), _old_collate_strip(t))

    def test_aligned_cases_read_like_adjudicate(self):
        """〔疑：X〕(전각 쌍점)·앞뒤 빈칸 있는 표시: 등급(자리 찾기)·글자층 대조·책갈피 교정도 3차 판별(adjudicate.plain)과 같은 1차 글자."""
        from yeone_ocr.adjudicate import plain
        from yeone_ocr.collate.collate import strip_marks
        from yeone_ocr.merge import strip_marks_for_compare
        cases = [t for t in CORPUS if aligned(t)]
        self.assertGreaterEqual(len(cases), 4)
        for t in cases:
            with self.subTest(t=t):
                self.assertEqual(fold_map(t, inner_pos=False)[0], plain(t))
                self.assertEqual(strip_marks(t)[0], strip_marks_for_compare(t))
                self.assertEqual(strip_marks_for_compare(t), fold(t, work_notes=True))
        self.assertEqual(strip_marks_for_compare("甲〔疑：龍〕乙〔 入/人〕"), "甲龍乙入")          # 예전: 「甲乙 入」
        self.assertEqual(strip_marks("甲〔疑：龍〕乙")[0], "甲龍乙")
        self.assertEqual(_old_compare("甲〔疑：龍〕乙〔 入/人〕"), "甲乙 入")

    def test_fold_map_positions(self):
        t = "上〔入/人〕〔판독불명〕下〔疑: 龍〕"
        self.assertEqual(fold_map(t), ("上入下龍", [0, 1, 12, 13, len(t)]))
        self.assertEqual(fold_map(t, inner_pos=False), ("上入下龍", [0, None, 12, None, len(t)]))
        from yeone_ocr.grades import locate_choice
        self.assertEqual(locate_choice("大〔疑：龍〕黃日一兩", 3, "日", "黃", "一"), 7)          # 전각 쌍점 표시 뒤의 확정 글자도 제자리(3차 글자 번호 = 표시를 접은 자리)


if __name__ == "__main__":
    unittest.main()
