"""문맥 추정: 자형으로 가리지 못한 〔a/b〕 자리에서, 이 책의 다른 대목이 어느 후보를 더 쓰는지 센다(모델 호출 없음).

지침과의 관계
- 판정 우선순위는 자형 > 읽기 순서 > 앞뒤 문맥(지침 2). 문맥이 유력해도 자형을 확인할 수 없으면 확정하지 않는다(지침 7).
  그래서 이 모듈은 글을 한 글자도 바꾸지 않는다. 결과는 문서에서 빨간 〔a/b〕 표시 안의 한 후보에 밑줄을 긋는 것뿐이고,
  괄호와 두 후보는 그대로 남는다. 3차(자형 판별)는 이 결과를 보지 않는다(문맥에 끌려가지 않게).
- 〔판독불명〕·〔원본 결락〕·〔疑: x〕 에는 추정을 만들지 않는다(지침 19, 21 "의미를 보고 누락된 글자를 추정하여 채운다" 금지).
- 숫자(처방 분량)·干支 나열·괘상·이체자 쌍·한글 후보·자형이 다른 쌍은 대상이 아니다(지침 8·13·14·21·27).
- 세는 바탕은 이 판독의 1차·2차가 서로 같게 읽은 글뿐이다. 기존 OCR 글자층·HWPX·장부는 바탕에 넣지 않는다(지침 20).
  다만 이 바탕도 OCR 이므로 두 판독이 같은 방향으로 틀린 글자(入→人 등)는 세는 수를 그쪽으로 기울인다. 그래서 밑줄은
  '이 판독의 다른 대목이 이렇게 쓴다'는 힌트일 뿐이고, 자형 편향 쌍에는 문턱을 두 배로 둔다. 정밀도는 아직 재지 않았다.

세는 법
- 바탕 글: 2차 판독이 있는(1차만 들어간 긴 구간이 없는) 본문·설명문·소주 영역의 문단 글. 〔…〕 표시와 그 앞뒤 한 글자, 3차가 고른 글자는 뺀다(스스로에게 표를 주지 않게).
  공백에서 자르고, 부호는 지우지 않고 반각으로 접어 둔다(「曰：」 의 ： 같은 단서를 살린다).
- 자리마다 후보 c 에 대해 L1+c, c+R1(2글자), L2+c, c+R2, L1+c+R1(3글자)을 센다. L1·L2 는 왼쪽 1·2글자, R 은 오른쪽.
  점수 s(c) = n(L1c) + n(cR1) + 2·[n(L2c) + n(cR2) + n(L1cR1)].
- s(c) ≥ max(S_MIN, 4·max(1, s(다른 후보))) 이면 c 를 '가리킴'. S_MIN 은 3, 자형 편향 쌍(入人·曰日 …)은 6.
- 3차가 자형으로 고른 자리도 같은 셈을 해, 다른 후보를 강하게 가리키면 '어긋남'으로 적는다(초록 → 파랑, 글자는 그대로).
"""
from __future__ import annotations
import re, time, unicodedata
from dataclasses import dataclass, field
from .merge import MARK_RE, _PUNCT_FOLD, LONG_SEG_NO_P2, LONG_SEG_DIFFER
from .schema import PageReading, TEXT_KINDS

S_MIN, S_MIN_WEAK, RATIO = 3, 6, 4
NEAR_MARKS = 4                  # 자리 앞뒤 이 글자 안에 다른 표시가 있으면(오류가 몰린 곳, 지침 28) 추정하지 않는다
PAGE_MARKS_MIN, PAGE_MARK_RATIO = 3, 0.05   # 표시가 3개 이상이면서 글자 100자에 5개를 넘는 쪽(지침 28)
LAYER_RATIO_MIN = 0.5           # 판독과 PDF 글자층의 일치율이 이보다 낮은 쪽(판독이 크게 흔들린 쪽)
LEDGER_SOURCES = ("판독↔글자층", "HWPX↔판독")   # 자형 쌍 거름에 쓰는 장부 출처('1차↔2차'는 지금 판정하는 표시 자체라 뺀다)
LONG_SEG_NOTES = (LONG_SEG_NO_P2, LONG_SEG_DIFFER)   # merge.LONG_SEGMENT 확인 항목: 표시 없이 1차만 들어간 긴 구간(글 머리를 merge 와 같이 쓴다)
MARK_AB = re.compile(r"〔([^〔〕/]{1,2})/([^〔〕]{1,2})〕")
NUMERALS = set("0123456789０１２３４５６７８９〇零一二三四五六七八九十百千萬万兩两廿卅")
GANZHI = set("甲乙丙丁戊己庚辛壬癸子丑寅卯辰巳午未申酉戌亥")
SEQ_NEIGHBORS = GANZHI | NUMERALS | set("東西南北中乾坤震巽坎離艮兌东离兑")
HANGUL = re.compile(r"[가-힣ㄱ-ㆎ]")


def _is_trigram_symbol(ch: str) -> bool:
    o = ord(ch)
    return 0x2630 <= o <= 0x2637 or 0x4DC0 <= o <= 0x4DFF


@dataclass
class Folder:
    """비교용 접기: NFC, 전각 부호 → 반각, (가로쓰기 한문이면) 간체·이체자 대표 글자. 글자 수는 바꾸지 않는다."""
    zh: bool = False

    def __post_init__(self):
        self._key = None
        if self.zh:
            try:
                from .collate import zh
                if zh.available():
                    self._key = zh.compare_key
            except Exception:
                self._key = None

    def __call__(self, s: str) -> str:
        s = unicodedata.normalize("NFC", s).translate(_PUNCT_FOLD)
        if self._key is not None:
            out = self._key(s)
            return out if len(out) == len(s) else s
        return s


def _confusable_sets(ledger_pairs=None) -> list[set[str]]:
    """자형이 비슷해 판독이 실제로 갈리는 묶음: collate.CONFUSABLES + 3차 자형 단서 묶음 + 장부에서 2번 이상 갈린 쌍."""
    sets: list[set[str]] = []
    try:
        from .collate.collate import CONFUSABLES
        sets.extend(set(g) for g in CONFUSABLES)
    except Exception:
        pass
    try:
        from .prompts import GLYPH_HINTS
        sets.extend(set(k) for k in GLYPH_HINTS)
    except Exception:
        pass
    sums: dict[frozenset, int] = {}
    for p in ledger_pairs or []:
        src = p.get("source", "")
        if not any(src.startswith(x) or x in src for x in LEDGER_SOURCES):
            continue
        a, b = p.get("a", ""), p.get("b", "")
        if len(a) == 1 and len(b) == 1 and a != b:
            k = frozenset((a, b)); sums[k] = sums.get(k, 0) + int(p.get("count", 0))
    sets.extend(set(k) for k, n in sums.items() if n >= 2)
    return sets


def load_ledger_pairs(work_dir) -> list[dict]:
    """결과 폴더의 혼동 장부(confusion.json) 쌍. 없으면 빈 목록."""
    import json
    from pathlib import Path
    f = Path(work_dir) / "confusion.json"
    try:
        return json.loads(f.read_text(encoding="utf-8")).get("pairs") or []
    except Exception:
        return []


def fold_zh_for(records: list[dict]) -> bool:
    try:
        from . import prompts
        if prompts.MODE == "horizontal_zh":
            return True
    except Exception:
        pass
    return any(r.get("mode") == "horizontal_zh" for r in records)


def write_snapshot(work_dir, res: dict[int, dict]) -> None:
    """마지막 문서 만들기의 문맥 추정을 <결과 폴더>/context.json 에 남긴다(판독 기록은 건드리지 않음 — 판독과 동시에 쓰지 않게)."""
    import json
    from pathlib import Path
    first = next(iter(res.values()), {})
    snap = {"v": 1, "at": time.strftime("%Y-%m-%d %H:%M:%S"), "fold": first.get("fold", "nfc"), "corpus_chars": first.get("corpus_chars", 0),
            "corpus_pages": first.get("corpus_pages", 0), "pages": {str(k): v.get("sites", []) for k, v in sorted(res.items())}}
    try:
        (Path(work_dir) / "context.json").write_text(json.dumps(snap, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:
        pass


def _weak_sets() -> list[set[str]]:
    try:
        from .collate.collate import WEAK_WITNESS
        return [set(g) for g in WEAK_WITNESS]
    except Exception:
        return []


def _pair_in(a: str, b: str, sets: list[set[str]], fold) -> bool:
    fa, fb = fold(a), fold(b)
    return any((a in g and b in g) or (fa in g and fb in g) for g in sets)


@dataclass
class _Para:
    page: int
    region_id: str
    index: int
    text: str
    cols: list[tuple[int, int, int]] = field(default_factory=list)    # (읽기 순서 열 번호, 문단 글 안 시작, 끝)


def _paragraphs(rec: dict) -> list[_Para]:
    pr = PageReading.model_validate(rec["merged"])
    out: list[_Para] = []
    for reg in pr.regions:
        if reg.kind not in TEXT_KINDS:
            continue
        ci = 0
        for pi, p in enumerate(reg.paragraphs):
            text = ""; cols = []
            for c in sorted(p.columns, key=lambda c: c.col_from_right):
                cols.append((ci, len(text), len(text) + len(c.text or ""))); text += c.text or ""; ci += 1
            out.append(_Para(pr.pdf_page, reg.id, pi, text, cols))
    return out


def _decided_offsets(rec: dict) -> dict[tuple[str, int], set[int]]:
    """3차가 고른 글자의 문단 글 위치(바탕에서 빼려고)."""
    from .grades import decided_positions
    out: dict[tuple[str, int], set[int]] = {}
    if not rec.get("adjudications"):
        return out
    found, _ = decided_positions(PageReading.model_validate(rec["merged"]), rec["adjudications"])
    for f in found:
        out.setdefault((f.region_id, f.para_index), set()).update(range(f.start, f.end))
    return out


def settled_segments(rec: dict, fold) -> list[str]:
    """한 쪽의 '두 판독이 같게 읽은' 글 조각들(접은 글). 2차가 없는 영역, 1차만 들어간 긴 구간(merge.LONG_SEGMENT)이 있는 영역,
    한 쪽 안에서 id 가 겹친 영역(3차 글자를 가려 뺄 수 없음)은 넣지 않는다."""
    from .grades import _pass2_regions, dup_region_ids
    pr = PageReading.model_validate(rec["merged"])
    ok_regions = _pass2_regions(rec) - dup_region_ids(pr) - {u.region_id for u in pr.uncertain if (u.context or "").startswith(LONG_SEG_NOTES)}
    decided = _decided_offsets(rec)
    segs: list[str] = []
    for p in _paragraphs(rec):
        if p.region_id not in ok_regions:
            continue
        drop = set(decided.get((p.region_id, p.index), set()))
        for m in MARK_RE.finditer(p.text):
            drop.update(range(max(0, m.start() - 1), min(len(p.text), m.end() + 1)))
        cur = []
        for i, ch in enumerate(p.text):
            if i in drop or ch.isspace():
                if len(cur) >= 2:
                    segs.append(fold("".join(cur)))
                cur = []
            else:
                cur.append(ch)
        if len(cur) >= 2:
            segs.append(fold("".join(cur)))
    return segs


@dataclass
class Site:
    page: int
    region_id: str
    para_index: int
    start: int                 # 문단 글 안 표시 시작(〔 위치) — 결정된 자리면 고른 글자 위치
    end: int
    a: str
    b: str
    left: str                  # 접은 왼쪽 글(표시·공백에서 끊김, 최대 2자)
    right: str
    state: str                 # open(남은 〔a/b〕) | decided(3차가 고름)
    choice: str = ""
    excluded: str = ""         # 대상이 아닌 까닭(빈 글이면 대상)
    origin_model: bool = False # 1차 모델이 스스로 〔자형/문맥〕 으로 적은 표시


def _neighbors(text: str, start: int, end: int, fold) -> tuple[str, str]:
    """표시(또는 고른 글자) 앞뒤의 보통 글자 최대 2자. 다른 표시·공백에서 끊는다."""
    marks = [(m.start(), m.end()) for m in MARK_RE.finditer(text)]
    def blocked(i):
        return text[i].isspace() or any(s <= i < e for s, e in marks)
    left = []
    i = start - 1
    while i >= 0 and len(left) < 2 and not blocked(i):
        left.insert(0, text[i]); i -= 1
    right = []
    j = end
    while j < len(text) and len(right) < 2 and not blocked(j):
        right.append(text[j]); j += 1
    return fold("".join(left)), fold("".join(right))


def _exclusion(a: str, b: str, left: str, right: str, conf_sets, fold) -> str:
    if len(a) != 1 or len(b) != 1:
        return "두 글자 후보(이번 판은 한 글자만)"
    if HANGUL.search(a + b):
        return "한글 후보(지침 8)"
    if any(ch in NUMERALS for ch in a + b):
        return "숫자(지침 14)"
    if any(_is_trigram_symbol(ch) for ch in a + b):
        return "괘상(지침 13)"
    if fold(a) == fold(b):
        return "이체자 쌍(지침 21)"
    try:
        from .collate import zh
        if zh.available() and zh.compare_key(a) == zh.compare_key(b):
            return "이체자 쌍(지침 21)"
    except Exception:
        pass
    if (set(a + b) & GANZHI) and (set(left[-1:] + right[:1]) & SEQ_NEIGHBORS):
        return "干支·방위 나열(지침 27)"
    zfold = Folder(zh=True)                                   # 자형 쌍 거름은 어느 모드든 간체로 접어서 본다
    if not (_pair_in(a, b, conf_sets, fold) or _pair_in(a, b, conf_sets, zfold)):
        return "자형이 다른 쌍(문맥이 자연스러운 쪽으로 끌려가기 쉬움)"
    return ""


def page_sites(rec: dict, fold, conf_sets) -> list[Site]:
    """한 쪽의 문맥 추정 대상: 남은 〔a/b〕(미정) 자리와 3차가 고른 자리."""
    from .grades import _plain_map
    from .schema import RegionReread
    out: list[Site] = []
    paras = _paragraphs(rec)
    n_marks = sum(1 for p in paras for _ in MARK_RE.finditer(p.text))
    plain_chars = sum(len(_plain_map(p.text)[0]) for p in paras) or 1
    td = rec.get("textlayer_diff") or {}
    noisy = ""
    if n_marks >= PAGE_MARKS_MIN and n_marks / plain_chars > PAGE_MARK_RATIO:
        noisy = "표시가 많은 쪽(지침 28)"
    elif (td.get("layer_chars") or 0) > 0 and td.get("ratio") is not None and float(td.get("ratio")) < LAYER_RATIO_MIN:
        noisy = "글자층과 일치율이 낮은 쪽(지침 28)"
    # 모델이 쓴 표시: 1차·2차 판독 글에 그 표시가 그대로 있으면 모델이 〔자형 글자/문맥 글자〕 로 적은 것(두 번째가 문맥 후보)
    model_text: dict[str, str] = {}
    try:
        p1 = PageReading.model_validate(rec["pass1"]) if rec.get("pass1") else None
        for r in (p1.regions if p1 else []):
            model_text[r.id] = model_text.get(r.id, "") + "".join(c.text or "" for pa in r.paragraphs for c in pa.columns)
    except Exception:
        pass
    for rid, v in (rec.get("pass2") or {}).items():
        try:
            rr = RegionReread.model_validate(v)
            model_text[rid] = model_text.get(rid, "") + "".join(c.text or "" for c in rr.columns)
        except Exception:
            continue
    from .grades import dup_region_ids
    dup = dup_region_ids(PageReading.model_validate(rec["merged"]))
    for p in paras:
        if p.region_id in dup:
            continue                                          # id 가 겹친 영역: 자리를 (영역 id, 문단 번호)로 가릴 수 없음(쪽 고지는 grades)
        for m in MARK_AB.finditer(p.text):
            a, b = m.group(1), m.group(2)
            left, right = _neighbors(p.text, m.start(), m.end(), fold)
            s = Site(p.page, p.region_id, p.index, m.start(), m.end(), a, b, left, right, "open")
            s.origin_model = m.group(0) in model_text.get(p.region_id, "")
            s.excluded = _exclusion(a, b, left, right, conf_sets, fold)
            if not s.excluded:
                near = [x for x in MARK_RE.finditer(p.text) if x.start() != m.start() and max(m.start() - x.end(), x.start() - m.end()) <= NEAR_MARKS]   # 두 표시 사이 글자 수(앞뒤 같게)
                if near:
                    s.excluded = "가까이에 다른 표시가 있음(오류가 몰린 곳, 지침 28)"
                elif noisy:
                    s.excluded = noisy
            out.append(s)
    if rec.get("adjudications"):
        from .grades import decided_positions
        found, _ = decided_positions(PageReading.model_validate(rec["merged"]), rec["adjudications"])
        texts = {(p.region_id, p.index): p.text for p in paras}
        for f in found:
            t = texts.get((f.region_id, f.para_index), "")
            e = f.entry
            left, right = _neighbors(t, f.start, f.end, fold)
            s = Site(rec.get("pdf_page", 0), f.region_id, f.para_index, f.start, f.end, e.get("a", ""), e.get("b", ""), left, right, "decided", e["choice"])
            s.excluded = _exclusion(s.a, s.b, left, right, conf_sets, fold)
            out.append(s)
    return out


def _queries(c: str, left: str, right: str) -> list[tuple[str, int]]:
    """후보 c 로 셀 글자 묶음과 무게."""
    q = []
    if left:
        q.append((left[-1:] + c, 1))
        if len(left) >= 2:
            q.append((left[-2:] + c, 2))
    if right:
        q.append((c + right[:1], 1))
        if len(right) >= 2:
            q.append((c + right[:2], 2))
    if left and right:
        q.append((left[-1:] + c + right[:1], 2))
    return q


def count_ngrams(segments: list[str], wanted: set[str]) -> dict[str, int]:
    """바탕 글 조각에서 wanted 에 든 2·3글자 묶음만 센다(한 번 훑기)."""
    n: dict[str, int] = dict.fromkeys(wanted, 0)
    lens = sorted({len(w) for w in wanted})
    for s in segments:
        for L in lens:
            for i in range(len(s) - L + 1):
                g = s[i:i + L]
                if g in n:
                    n[g] += 1
    return n


def analyze(records: list[dict], targets: list[dict] | None = None, *, fold_zh: bool = False, ledger_pairs=None) -> dict[int, dict]:
    """records(바탕) 전체로 targets(기본 records) 쪽들의 문맥 추정을 낸다. 돌려주는 값: {쪽: rec['context'] 에 넣을 기록}."""
    fold = Folder(zh=fold_zh)
    conf = _confusable_sets(ledger_pairs); weak = _weak_sets()
    targets = records if targets is None else targets
    segments: list[str] = []
    for rec in records:
        try:
            segments.extend(settled_segments(rec, fold))
        except Exception:
            continue
    sites_by_page: dict[int, list[Site]] = {}
    wanted: set[str] = set()
    for rec in targets:
        try:
            ss = page_sites(rec, fold, conf)
        except Exception:
            ss = []
        sites_by_page[rec.get("pdf_page", 0)] = ss
        for s in ss:
            if s.excluded:
                continue
            for c in (s.a, s.b):
                wanted.update(q for q, _ in _queries(fold(c), s.left, s.right))
    counts = count_ngrams(segments, wanted) if wanted else {}
    corpus_chars = sum(len(s) for s in segments)
    out: dict[int, dict] = {}
    for page, ss in sites_by_page.items():
        items = []
        for s in ss:
            d = {"region_id": s.region_id, "para": s.para_index, "start": s.start, "end": s.end, "a": s.a, "b": s.b,
                 "left": s.left, "right": s.right, "state": s.state, "choice": s.choice}
            if s.excluded:
                d.update(verdict="excluded", reason=s.excluded); items.append(d); continue
            score, detail = {}, {}
            for c in (s.a, s.b):
                qs = _queries(fold(c), s.left, s.right)
                score[c] = sum(counts.get(q, 0) * w for q, w in qs)
                detail[c] = {q: counts.get(q, 0) for q, _ in qs if counts.get(q, 0)}
            smin = S_MIN_WEAK if _pair_in(s.a, s.b, weak, fold) else S_MIN
            def strong(c, o):
                return score[c] >= max(smin, RATIO * max(1, score[o]))
            d["counts"] = detail; d["score"] = score
            if s.state == "open":
                pick = s.a if strong(s.a, s.b) else (s.b if strong(s.b, s.a) else "")
                if pick and s.origin_model and pick == s.b:
                    d.update(verdict="neutral", reason="1차 모델이 문맥 후보로 적은 글자 — 다시 밀지 않음(지침 2·7)")
                elif pick:
                    d.update(verdict="suggest", suggest=pick, reason=f"이 판독의 다른 대목: {pick} {score[pick]} · {s.b if pick == s.a else s.a} {score[s.b if pick == s.a else s.a]}")
                else:
                    d.update(verdict="neutral", reason="근거 부족" if max(score.values()) < smin else "두 후보 모두 쓰임")
            else:
                other = s.b if s.choice == s.a else s.a
                if strong(other, s.choice):
                    d.update(verdict="contradicts", suggest=other, reason=f"3차 「{s.choice}」, 이 판독의 다른 대목 「{other}」 {score[other]}:{score[s.choice]}")
                else:
                    d.update(verdict="agrees" if strong(s.choice, other) else "neutral", reason="")
            items.append(d)
        out[page] = {"v": 1, "at": time.strftime("%Y-%m-%d %H:%M:%S"), "corpus_chars": corpus_chars, "corpus_pages": len(records),
                     "fold": "zh" if fold_zh else "nfc", "sites": items}
    return out
