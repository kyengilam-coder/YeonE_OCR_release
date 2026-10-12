"""HWPX 구획 글과 PDF 판독 글을 맞춰 보고 차이를 분류한다.

비교 공간: 양쪽을 compare_key(NFC → 이체자 접기 → 간체)로 바꾸고, 부호·공백은 빼고(부호 차이는 D) 글자만 정렬한다.
  HWPX 글 속 연어OCR 작업 표시(일반 OCR 의 〔고지: …〕, 교정의 〔확인: …〕·〔제안: …〕 …)도 본문이 아니므로 뺀다(note_spans).
정렬: difflib.SequenceMatcher 로 구획 전체를 한 줄로 이어 글자 단위 정렬. 문단 경계는 HWPX 쪽 글자 지도로 되찾는다.

분류
  A  확실한 오탈: 판독이 확실(〔〕 없음)하고 간→번이 1:1 이며 HWPX 와 다름 → 자동 교정. 단 자형이 비슷한 쌍(CONFUSABLES)은 B1 로 내려 3차 판별이 가린다
  B2 1:多 글자: 판독은 확실하나 간체 한 글자가 여러 번체로 갈라짐 → 어구 사전으로 고른 번체로 자동 교정, 후보를 메모에
  B1 판독 불확실: 그 자리의 판독에 〔疑〕·〔a/b〕 표시가 있거나, 2차 판독이 빠져 1차만으로 읽은 영역(once)의 글자 → 글자는 두고 표시만
  C  구조 차이: 긴 구간(LONG 이상)이 한쪽에만 있음(옆 글자와 치환으로 묶인 것도)·HWPX 여러 글자가 판독에 없음 → 어느 정책에서도 표시만. 누락 구간은 '삽입 제안'으로 메모·보고에 남기고 사람이 원본 확인 뒤 넣는다
  D  표기 차이: 부호·공백·이체자만 다름 → 기본 무시
"""
from __future__ import annotations
import difflib, re, unicodedata
from dataclasses import dataclass, field
from . import zh

LONG = 20                      # 이 길이 이상이 한쪽에만 있으면 글자 오탈이 아니라 구조 차이(C)로 본다
CTX = 6                        # 보고에 보여 줄 앞뒤 글자 수
# 자형이 비슷해 판독기가 서로 헷갈리기 쉬운 글자 묶음(간체 기준). 한 글자 차이(A·B2)는 1차·2차가 같은 쪽으로 틀린 것일 수 있어 자동 반영하지 않고
# 표시만 한다(B1) — 글자 조각 확인(3차 판별)이 가린다
CONFUSABLES = [set(g) for g in ("干于千", "日曰", "己已巳", "土士", "未末", "天夭", "人入", "刀力", "木本", "戊戌戍", "冶治", "乌鸟", "纲网", "问间闻",
                                 "王玉", "大太犬", "子于", "几凡", "血皿", "目日月", "白自", "戈弋", "丸九", "旦且", "田由甲申", "史吏", "宫官",
                                 "侯候", "兔免", "祟崇", "荼茶", "午牛", "币市", "师帅", "凤风", "析折", "斤斥", "卯卵", "微徽",
                                 # 0.6.0 — 중의역대약론선 실측(1차·2차 갈림, 글자층·HWPX 대조)에서 실제로 섞인 묶음
                                 "府腐", "山由", "失尖", "信倍", "届属", "镇慎", "宜宣", "囊裹", "胀脓", "恶悉", "摄慑", "源原", "常尝", "僻澼", "浥挹",
                                 "障癃", "授援", "元无", "火灭", "今令", "予子", "性桂", "混温", "革草", "元元",
                                 "瘕疲痼痕瘦", "疠疣", "痛痈", "瘌癞", "癃痉", "哕啰", "芩苓", "菜茱", "葛菖", "芪黄", "英萸",
                                 "校枝棱", "植楂", "拘枸", "脘院皖", "腠媵膜")]
# OCR 엔진들이 같은 방향으로 틀리기 쉬운 자형 쌍. 글자층(다른 OCR)이 판독과 같아도 증거가 약하고, 인쇄 자체가 오자인 일도 있어
# (430쪽 「能人陰分」) 한 글자 차이는 자동 반영하지 않고 글자 조각 확인(3차 판별)으로 넘긴다.
WEAK_WITNESS = [set(g) for g in ("人入", "日曰", "土士", "未末", "己已巳", "大太犬", "白自")]
REPEAT_MIN = 5                 # 같은 (HWPX, 판독) 한 글자 쌍이 한 구획에서 이만큼 반복되면 책 전체의 변환 오류로 보고 교정 후보로 올린다
MAX_DELETE_AUTO = 2            # HWPX 에만 있는 글자를 자동으로 지우는 최대 길이(그 이상은 C 로 표시만)
ARABIC_RE = re.compile(r"^[0-9０-９.．]+$")
CJK_NUM_RE = re.compile(r"^[一二三四五六七八九十〇零百千兩两]+$")
HANGUL_RE = re.compile(r"[가-힣ㄱ-ㅎㅏ-ㅣ]")
FOOTNOTE_RE = re.compile(r"^[注註]\s*[0-9０-９一二三四五六七八九十]+")
TITLE_RE = re.compile(r"[『《〈「]([^』》〉」]{2,14})[』》〉」]")
MARK_RE = re.compile(r"〔[^〔〕]*〕")
PUNCT_RE = re.compile(r"[\s　·ㆍ・•\.。,，、:：;；!！?？()（）\[\]【】『』「」《》〈〉“”\"'‘’—─―－\-_/／\\|*＊~～…‥]+")
# 부호를 버리기 전에 확실한 번호 표기만 구별한다. '10毫升'·'2024年'처럼 문장 속 수치는 남긴다.
NUMBER_NOTATION_RE = re.compile(
    r"(?m)^\s*[0-9０-９]{1,3}[.．、)）](?![0-9０-９])\s*(?=\S)"
    r"|[\[［][0-9０-９]+(?:\s*[,，;；、–-]\s*[0-9０-９]+)*[\]］]"
    r"|(?m:^\s*[-–—]\s*[0-9０-９]+\s*[-–—]\s*$)"
)
# 교정이 글 속에 남기는 짧은 표기(hwpx_edit._short_note, inline·0.6.3 까지의 메모 없음). 두 글자 후보 표시(〔확인/학인〕)와 겹치지 않게 쌍점·닫는 괄호까지
INLINE_NOTE_PREFIXES = ("〔확인〕", "〔확인: ", "〔제안: ", "〔삽입〕", "〔삽입·확인〕", "〔삽입 제안: ", "〔삭제: ", "〔삭제 제안: ", "〔원: ", "〔자동 교정")


@dataclass
class Char:
    ch: str                    # 원래 글자
    key: str                   # 비교용 글자(간체)
    para: int                  # HWPX: 문단 번호 / OCR: pdf 쪽
    pos: int                   # HWPX: 문단 안 글자 위치 / OCR: 글자 위치
    uncertain: bool = False    # OCR: 〔〕 표시 안이거나 바로 옆, 또는 once
    once: bool = False         # OCR: 2차 판독이 빠져 1차만으로 읽은 영역의 글자(run.single_read_regions) — 어느 정책에서도 표시만(B1)


@dataclass
class Item:
    kind: str                  # A | B1 | B2 | C | D
    action: str                # replace | insert | delete | mark
    para: int                  # HWPX 문단 번호(삽입이면 그 앞 문단)
    start: int                 # 문단 안 시작 위치
    end: int                   # 문단 안 끝 위치(미포함)
    hwpx: str                  # HWPX 쪽 글
    ocr: str                   # 판독 쪽 글(인쇄된 글자 그대로, 간체)
    new: str                   # 반영할 글(번체). mark 면 빈 글
    candidates: list[str] = field(default_factory=list)   # B2 다른 후보
    pdf_page: int = 0
    note: str = ""
    auto: bool = False         # 정책에 따라 실제로 반영할지
    hw_ctx: str = ""           # HWPX 앞뒤 글자(「」 안이 해당 자리)
    oc_ctx: str = ""           # 판독 앞뒤 글자
    witness: str = ""          # 문맥 규칙·글자층이 어느 쪽 편인지(hwpx | ocr | "")
    notice: bool = False       # 구조 규칙이 붙인 고지(문서에 메모로 알릴 것 — grades.needs_memo)


def strip_marks(text: str) -> tuple[str, list[tuple[int, int]]]:
    """판독 글의 〔〕 표시를 지운다. 〔a/b〕 → a(1차 판독), 〔疑: x〕 → x, 그 밖(〔판독불명〕·작업 표시) → 빈 글.
    돌려주는 구간은 지운 자리 앞뒤 1자까지 '불확실'로 볼 범위."""
    pieces: list[str] = []; spans: list[tuple[int, int]] = []; last = 0; n = 0
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


def note_spans(text: str) -> list[tuple[int, int]]:
    """HWPX 글 속 연어OCR 작업 표시의 자리 [(시작, 끝)]: 일반 OCR 의 〔고지: …〕 같은 작업 표시(grades.NOTE_PREFIXES)와 교정의 글 속 표기
    (〔확인: …〕·〔제안: …〕 …). 본문이 아니므로 대조하지 않는다 — 그 안의 한글이 한글 풀이(B1)로 잡혀 표기 안에 또 표기가 붙지 않게.
    괄호가 겹쳐 있으면(예전 표기 안의 표기) 바깥 괄호까지. 〔판독 메모〕·〔작업 메모〕 는 메모 글이 〕 뒤에 오는 메모 문단(blocks.py)이라
    문단 끝까지. 〔a/b〕 후보·〔판독불명〕 같은 지침 표시는 지금처럼 대조한다."""
    from ..grades import NOTE_PREFIXES
    pre = NOTE_PREFIXES + INLINE_NOTE_PREFIXES; out = []; i = text.find("〔")
    while i >= 0:
        end = None
        if text.startswith(("〔판독 메모〕", "〔작업 메모〕"), i):
            out.append((i, len(text))); break
        if text.startswith(pre, i):
            d = 0
            for j in range(i, len(text)):
                d += (text[j] == "〔") - (text[j] == "〕")
                if d == 0:
                    end = j + 1; out.append((i, end)); break
        i = text.find("〔", end or i + 1)
    return out


def _cells(text: str, para: int, *, strip: bool = False, skip: list[tuple[int, int]] = (), once: bool = False) -> list[Char]:
    """글 → 비교 글자 목록. 부호·공백은 뺀다. 위치(pos)는 원래 글의 글자 번호 그대로라 편집에 바로 쓴다. skip 구간(작업 표시)의 글자도 뺀다.
    once: 1차만으로 읽은 영역의 글 — 글자마다 불확실로 둔다."""
    s, unc_spans = (strip_marks(text) if strip else (text, []))
    number_spans = [(m.start(), m.end()) for m in NUMBER_NOTATION_RE.finditer(s)]
    excluded = [*skip, *number_spans]
    out: list[Char] = []
    for i, ch in enumerate(s):
        if PUNCT_RE.match(ch) or unicodedata.category(ch)[0] in "ZCPS" or any(a <= i < b for a, b in excluded):
            continue
        key = zh.compare_key(ch)
        if not key:
            continue
        out.append(Char(ch, key[0], para, i, once or any(a <= i < b for a, b in unc_spans), once))
    return out


def hwpx_cells(paras: list[str], first_para: int) -> list[Char]:
    out = []
    for k, t in enumerate(paras):
        out.extend(_cells(t, first_para + k, skip=note_spans(t)))
    return out


def ocr_cells(pages: list[tuple]) -> list[Char]:
    """[(pdf 쪽, 판독 글[, 1차만])] → 글자 목록. 판독 글은 build 와 같은 문단 글(〔〕 표시 포함).
    셋째 값이 참이면 2차 판독이 빠져 1차만으로 읽은 영역의 글(run.reading_paras) — 그 글자의 차이는 어느 정책에서도 표시만(B1)."""
    out = []
    for page, t, *once in pages:
        out.extend(_cells(t, page, strip=True, once=bool(once and once[0])))
    return out


def _s2t_context(ocr: list[Char], i0: int, i1: int) -> str:
    """OCR 글자 i0:i1 을 번체로. 앞뒤 4자를 붙여 어구 사전이 문맥을 보게 한 뒤 가운데만 잘라 낸다."""
    lo, hi = max(0, i0 - 4), min(len(ocr), i1 + 4)
    ctx = "".join(c.ch for c in ocr[lo:hi])
    t = zh.to_traditional(ctx)
    if len(t) == len(ctx):
        return t[i0 - lo:i1 - lo]
    return zh.to_traditional("".join(c.ch for c in ocr[i0:i1]))


def _confusable(a: str, b: str) -> bool:
    return a != b and any(a in g and b in g for g in CONFUSABLES)


def _weak_pair(a: str, b: str) -> bool:
    return a != b and any(a in g and b in g for g in WEAK_WITNESS)


def _long_change(n_hw: int, n_oc: int) -> bool:
    """글자 오탈이 아니라 구조 차이(C)로 볼 길이: 한쪽이 LONG 이상이거나, HWPX 에서 MAX_DELETE_AUTO 넘게 지워지는 차이.
    삽입·삭제뿐 아니라 치환(1자 ↔ 23자 …)에도 같이 쓴다 — 옆 글자 하나가 달라 difflib 이 치환으로 묶어도 규칙을 비켜 가지 않게."""
    return max(n_hw, n_oc) >= LONG or n_hw - n_oc > MAX_DELETE_AUTO


def book_titles(texts: list[str]) -> set[str]:
    """HWPX 본문의 『』《》 안 글(서명)을 모은다. 「本草」綱目 처럼 책이 줄여 적은 서명을 HWPX 가 풀어 쓴 자리를 자동으로 지우지 않기 위해."""
    out: set[str] = set()
    for t in texts:
        for m in TITLE_RE.finditer(t or ""):
            name = "".join(ch for ch in m.group(1) if zh.is_cjk(ch))
            if len(name) >= 2:
                out.add(name)
    return out


def _s2t_from_ctx(oc_ctx: str, ch: str) -> str:
    """판독 앞뒤 글자(oc_ctx)를 붙여 어구 사전이 문맥을 보게 한 뒤 가운데 글자의 번체만 꺼낸다(于→於, 晒干→曬乾)."""
    pre, post = _split_ctx(oc_ctx)
    src = pre + ch + post
    t = zh.to_traditional(src)
    if len(t) == len(src):
        return t[len(pre)]
    return zh.to_traditional(ch)


def _mark(it: "Item", kind: str, note: str, notice: bool = False) -> None:
    it.kind, it.action, it.new, it.auto = kind, "mark", "", False
    it.note = note
    if notice:
        it.notice = True


def apply_structural_rules(items: list["Item"], titles: set[str] | None = None) -> None:
    """글자 오탈이 아닌 차이를 걸러 낸다(0.6.0, 중의역대약론선 실측 근거).
    - 확실한 인용·목록 번호와 쪽수 표기는 _cells 에서 뺀다. 나머지 아라비아 수치 차이는 원문 확인 대상으로 남긴다.
    - 숫자와 글자가 섞인 치환(「本草」→「4」): 표시만(C).
    - 한자 숫자만인 삽입·삭제: 분량·순서일 수 있어 표시만(C). 한자 숫자끼리의 치환(三錢↔二錢)은 그대로 둔다(지침 14).
    - HWPX 글자에 한글이 있거나 삽입 자리 바로 옆이 한글(편집자의 풀이): 어떤 정책에서도 자동 반영하지 않는다(B1).
    - 각주(注1 …)로 보이는 삽입: 본문에 끼워 넣지 않고 표시만(C).
    - 서명(『』) 일부로 보이는 짧은 삭제(「本草」綱目): 책이 줄여 적은 것일 수 있어 표시만(C)."""
    for it in items:
        hw, oc = it.hwpx, it.ocr
        pre, post = _split_ctx(it.hw_ctx)
        # HWPX 쪽에만 한글이 있으면(또는 삽입 자리 앞뒤가 한글 풀이 속이면) 편집자의 풀이를 판독 한문으로 덮어쓰는 셈 — 표시만.
        # 양쪽 다 한글이면(한글·한자 혼용 문헌의 한글 대 한글) 보통 흐름대로 둔다.
        hw_h, oc_h, ctx_h = bool(HANGUL_RE.search(hw)), bool(HANGUL_RE.search(oc)), bool(HANGUL_RE.search(pre + post))
        if (hw_h and not oc_h) or (not hw and ctx_h and not oc_h):
            _mark(it, "B1", "한글 풀이·주석 자리로 보임 — 자동 반영하지 않음(원본 확인)", notice=True); continue
        hw_ar, oc_ar = bool(hw) and bool(ARABIC_RE.match(hw)), bool(oc) and bool(ARABIC_RE.match(oc))
        hw_cn, oc_cn = bool(hw) and bool(CJK_NUM_RE.match(hw)), bool(oc) and bool(CJK_NUM_RE.match(oc))
        if it.action in ("insert", "delete") and (hw_ar or oc_ar):
            _mark(it, "C", "숫자가 한쪽에만 있음 — 분량·연도·번호인지 원본 확인", notice=True); continue
        if it.action == "replace" and hw_ar and oc_ar:
            _mark(it, "C", "수치가 서로 다름 — 분량·연도·번호인지 원본 확인", notice=True); continue
        if it.action == "replace" and (hw_ar != oc_ar) and (hw and oc):
            _mark(it, "C", f"숫자와 글자가 섞인 치환(「{hw}」↔「{oc}」) — 번호 체계 차이로 보여 자동 반영하지 않음", notice=True); continue
        if it.action in ("insert", "delete") and (hw_cn or oc_cn):
            _mark(it, "C", "한자 숫자만 한쪽에 있음 — 분량·순서일 수 있어 표시만", notice=True); continue
        if it.action == "insert" and FOOTNOTE_RE.match(oc.strip()):
            _mark(it, "C", f"각주로 보이는 구간({len(oc)}자) — 본문에 넣지 않고 표시만", notice=True); continue
        if it.action == "delete" and titles and 1 <= len(hw) <= 4:
            cand = {hw + post[:k] for k in (1, 2, 3)} | {pre[-k:] + hw for k in (1, 2, 3) if pre[-k:]}
            if any(c in t for c in cand for t in titles):
                _mark(it, "C", f"서명(『』) 일부로 보임 — 책이 줄여 적은 자리일 수 있어 자동 삭제하지 않음", notice=True); continue


def promote_repeated(items: list["Item"], min_count: int = REPEAT_MIN, policy: str = "all") -> int:
    """같은 (HWPX, 판독) 한 글자 쌍이 구획 안에서 min_count 회 이상 반복되고 그 자리가 干/于 '혼동 가능성' 표시(B1)로 남아 있으면,
    개별 자형 혼동이 아니라 책 전체에 걸친 변환 오류(간체 于→幹 일괄 변환 등)로 보고 교정 후보(B2/A)로 올린다. 올린 수를 돌려준다.
    자형이 헷갈리기 쉬운 쌍(CONFUSABLES, 問/間 …)은 올리지 않는다 — 한 글꼴에서 같은 쪽으로 되풀이해 틀리는 것이 바로 그 위험이라 3차 판별만 가린다.
    3차 판별(글자 조각)을 못 할 때만 부른다(run_section) — 3차가 가릴 자리를 반복 수만으로 먼저 올리지 않게."""
    from collections import Counter
    cnt = Counter((zh.compare_key(it.hwpx), zh.compare_key(it.ocr)) for it in items
                  if len(it.hwpx) == 1 and len(it.ocr) == 1 and it.action in ("replace", "mark") and it.kind in ("A", "B1", "B2"))
    n = 0
    for it in items:
        if not (it.kind == "B1" and it.action == "mark" and len(it.hwpx) == 1 and len(it.ocr) == 1 and "혼동" in it.note):
            continue
        key = (zh.compare_key(it.hwpx), zh.compare_key(it.ocr))
        if cnt[key] < min_count or _weak_pair(key[0], key[1]):
            continue
        new = _s2t_from_ctx(it.oc_ctx, it.ocr); cands = zh.candidates(it.ocr)
        it.kind = "B2" if len(cands) > 1 else "A"; it.action = "replace"; it.new = new; it.candidates = cands if len(cands) > 1 else []
        it.auto = (policy == "all") if it.kind == "B2" else (policy in ("sure", "all"))
        it.witness = (it.witness + "+" if it.witness else "") + "repeat"
        it.note += f" · 같은 쌍 「{it.hwpx}」↔「{it.ocr}」 이 이 구획에서 {cnt[key]}회 반복 — 책 전체의 변환 오류로 보여 교정 후보로 올림"
        n += 1
    return n


# ---------- 于·干·於·幹·乾 묶음: 판독기가 자주 섞는 자리라 문맥으로 가린다 ----------
GAN_YU = set("于干於幹乾")
YU_PREV = set("生至属由对关在甚大等见出始过归利合宜从行施用发成得走入居处位寓寄流注达通倍胜优劣异同基鉴源起终止限介存安死忠败毁立传闻问系乃莫及急缓近远轻重强弱多少高低先后易难善恶美丑长短久速溢伤益损助趋散聚托")   # 于 앞에 흔한 글자(간체)
GAN_NEXT = set("姜燥涸枯渴涉支净预扰戈系犯结枝果货草品粮")      # 干 뒤에 흔한 글자
GAN_PREV = set("能若相不焙晒阴风曝晾树主饼")                  # 干 앞에 흔한 글자


def _split_ctx(ctx: str) -> tuple[str, str]:
    """「」 앞뒤 글자를 (앞, 뒤)로."""
    i, j = ctx.find("「"), ctx.find("」")
    if i < 0 or j < 0:
        return "", ""
    return ctx[:i], ctx[j + 1:]


def gan_yu_guess(hw_ctx: str) -> str:
    """HWPX 문맥(앞뒤 글자)으로 그 자리가 于(於)인지 干(幹·乾)인지 짐작한다. 모르면 ""."""
    pre, post = _split_ctx(hw_ctx)
    pk = zh.compare_key(pre[-1]) if pre else ""; nk = zh.compare_key(post[0]) if post else ""
    if nk and nk in GAN_NEXT:
        return "干"
    if pk and pk in GAN_PREV:
        return "干"
    if pk and pk in YU_PREV:
        return "于"
    return ""


def resolve_gan_yu(items: list[Item]) -> None:
    """于·干 묶음의 한 글자 차이: 문맥 규칙이 HWPX 편이면 차이에서 빼고(D), 판독 편이면 그대로 두되 혼동 규칙을 면제하고, 모르면 표시만(B1)."""
    for it in items:
        if it.action != "replace" or len(it.hwpx) != 1 or len(it.ocr) != 1:
            continue
        hk, ok = zh.compare_key(it.hwpx), zh.compare_key(it.ocr)
        if not (it.hwpx in GAN_YU and it.ocr in GAN_YU and hk != ok and {hk, ok} <= {"于", "干"}):
            continue
        g = gan_yu_guess(it.hw_ctx)
        if g == hk:
            it.kind, it.witness = "D", "hwpx"; it.note = f"干/于 문맥 규칙: HWPX 「{it.hwpx}」 가 맞고 판독 「{it.ocr}」 이 자형 혼동으로 보임 — 차이에서 뺌"
        elif g == ok:
            it.witness = "ocr"; it.note = f"干/于 문맥 규칙도 판독 「{it.ocr}」 편 — HWPX 오탈로 보임"
        else:
            it.kind, it.action, it.new, it.auto = "B1", "mark", "", False
            it.note = f"干/于 혼동 가능성: HWPX 「{it.hwpx}」 ↔ 판독 「{it.ocr}」, 문맥으로 가리지 못함 — 원본 확인"


def _ctx(cells: list[Char], lo: int, hi: int) -> str:
    """cells[lo:hi] 를 「」 로 감싸고 앞뒤 CTX 글자를 붙인다."""
    pre = "".join(c.ch for c in cells[max(0, lo - CTX):lo]); mid = "".join(c.ch for c in cells[lo:hi]); post = "".join(c.ch for c in cells[hi:hi + CTX])
    return f"{pre}「{mid}」{post}"


def collate(hw: list[Char], oc: list[Char], *, policy: str = "all", titles: set[str] | None = None) -> list[Item]:
    """정렬해 차이 항목을 만든다. policy: memo(아무것도 자동 반영 안 함) | sure(A만) | all(A+B2). C(긴 구간)는 어느 정책에서도 표시만.
    titles: HWPX 의 서명 집합(book_titles) — 서명 일부로 보이는 짧은 삭제를 자동으로 하지 않기 위해."""
    a = "".join(c.key for c in hw); b = "".join(c.key for c in oc)
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    items: list[Item] = []
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op == "equal":
            continue
        hw_span, oc_span = hw[i1:i2], oc[j1:j2]
        hw_text = "".join(c.ch for c in hw_span); oc_text = "".join(c.ch for c in oc_span)
        uncertain = any(c.uncertain for c in oc_span) or (not oc_span and j1 < len(oc) and oc[min(j1, len(oc) - 1)].uncertain)
        # 1차만 읽은 영역 끝에서 빠진 글자(그 뒤가 다른 영역·판독 끝)도 그 영역의 판독 — 앞 글자가 once 면 지우지 않는다
        once = any(c.once for c in oc_span) or (not oc_span and ((j1 < len(oc) and oc[j1].once) or (0 < j1 <= len(oc) and oc[j1 - 1].once)))
        uncertain = uncertain or once
        unote = "2차 판독이 빠져 1차만으로 읽은 영역(원본 확인)" if once else "판독이 불확실한 자리"
        page = oc_span[0].para if oc_span else (oc[min(j1, len(oc) - 1)].para if oc else 0)
        # 문단 경계를 넘는 HWPX 구간은 문단별로 쪼갠다(편집은 문단 안에서만)
        groups: list[list[Char]] = []
        for c in hw_span:
            if groups and groups[-1][-1].para == c.para:
                groups[-1].append(c)
            else:
                groups.append([c])
        long_op = _long_change(len(hw_span), len(oc_span))
        if op == "insert" or not groups:
            # HWPX 에 없는 글이 판독에 있음 → 삽입 자리는 앞 글자 뒤
            if i1 > 0:
                anchor = hw[i1 - 1]; para, pos = anchor.para, anchor.pos + 1
            elif hw:
                para, pos = hw[0].para, hw[0].pos
            else:
                para, pos = 0, 0
            hctx, octx = _ctx(hw, i1, i1), _ctx(oc, j1, j2)
            if uncertain:
                items.append(Item("B1", "mark", para, pos, pos, "", oc_text, "", pdf_page=page, note=unote + "라 넣지 않음", hw_ctx=hctx, oc_ctx=octx))
            elif len(oc_span) >= LONG:
                items.append(Item("C", "insert", para, pos, pos, "", oc_text, _s2t_context(oc, j1, j2), pdf_page=page, note=f"HWPX 에 없는 긴 구간({len(oc_span)}자) — 원본 확인 뒤 사람이 넣음", hw_ctx=hctx, oc_ctx=octx))
            else:
                new = _s2t_context(oc, j1, j2)
                amb = [c.ch for c in oc_span if zh.is_ambiguous(c.ch)]
                kind = "B2" if amb else "A"
                cands = sorted({x for ch in amb for x in zh.candidates(ch)}) if amb else []
                items.append(Item(kind, "insert", para, pos, pos, "", oc_text, new, cands, page, "빠진 글자", hw_ctx=hctx, oc_ctx=octx))
            continue
        offset = 0
        for g in groups:
            para, start, end = g[0].para, g[0].pos, g[-1].pos + 1
            g_text = "".join(c.ch for c in g)
            gi = i1 + offset; hctx = _ctx(hw, gi, gi + len(g)); octx = _ctx(oc, j1, j2)
            # 누적 경계를 공유해야 앞 문단 끝과 다음 문단 시작이 같아져 중복·누락이 없다.
            k1 = j1 + round(offset * len(oc_span) / len(hw_span))
            offset += len(g)
            k2 = j1 + round(offset * len(oc_span) / len(hw_span))
            if op == "delete" or not oc_span:
                if uncertain:
                    items.append(Item("B1", "mark", para, start, end, g_text, "", "", pdf_page=page, note=unote, hw_ctx=hctx, oc_ctx=octx))
                elif len(hw_span) >= LONG:
                    items.append(Item("C", "mark", para, start, end, g_text, "", "", pdf_page=page, note=f"판독에 없는 긴 구간({len(hw_span)}자) — PDF 범위·누락 확인", hw_ctx=hctx, oc_ctx=octx))
                elif len(hw_span) <= MAX_DELETE_AUTO:
                    items.append(Item("A", "delete", para, start, end, g_text, "", "", pdf_page=page, note="판독에 없는 글자", hw_ctx=hctx, oc_ctx=octx))
                else:
                    items.append(Item("C", "mark", para, start, end, g_text, "", "", pdf_page=page, note="판독에 없는 글자(여러 자)", hw_ctx=hctx, oc_ctx=octx))
                continue
            # replace: 길이 비례 분할은 보고용. 문단을 넘으며 길이도 달라지면 실제 경계를 모르므로 자동 교정하지 않는다.
            seg = oc[k1:k2]
            seg_text = "".join(c.ch for c in seg)
            octx = _ctx(oc, k1, k2)
            if uncertain or any(c.uncertain for c in seg):
                items.append(Item("B1", "mark", para, start, end, g_text, seg_text, "", pdf_page=page, note=unote, hw_ctx=hctx, oc_ctx=octx))
            elif len(groups) > 1 and len(hw_span) != len(oc_span):
                items.append(Item("C", "mark", para, start, end, g_text, seg_text, "", pdf_page=page,
                                  note="문단 경계를 넘는 길이 차이 — 글자 배정 위치를 원본에서 확인", hw_ctx=hctx, oc_ctx=octx, notice=True))
            elif len(g) >= LONG and len(seg) >= LONG:
                items.append(Item("C", "mark", para, start, end, g_text, seg_text, "", pdf_page=page, note="긴 구간이 서로 다름 — 문단 순서·범위 확인", hw_ctx=hctx, oc_ctx=octx))
            elif long_op or _long_change(len(g), len(seg)):
                # 한쪽에 긴 구간이 붙은 치환(「矣」↔「也甲乙…」 23자, 22자 ↔ 1자): 삽입(LONG)·삭제(MAX_DELETE_AUTO) 규칙과 같이 표시만
                items.append(Item("C", "mark", para, start, end, g_text, seg_text, "", pdf_page=page,
                                  note=f"한쪽에 긴 구간이 붙은 치환(HWPX {len(g)}자 ↔ 판독 {len(seg)}자) — 원본 확인 뒤 사람이 고침", hw_ctx=hctx, oc_ctx=octx))
            else:
                new = _s2t_context(oc, k1, k2) if seg else ""
                amb = [c.ch for c in seg if zh.is_ambiguous(c.ch)]
                if not seg:
                    items.append(Item("A", "delete", para, start, end, g_text, "", "", pdf_page=page, note="판독에 없는 글자", hw_ctx=hctx, oc_ctx=octx))
                elif amb:
                    cands = sorted({x for ch in amb for x in zh.candidates(ch)})
                    items.append(Item("B2", "replace", para, start, end, g_text, seg_text, new, cands, page, "간체 한 글자가 여러 번체로 갈라짐", hw_ctx=hctx, oc_ctx=octx))
                else:
                    items.append(Item("A", "replace", para, start, end, g_text, seg_text, new, [], page, "판독과 다름", hw_ctx=hctx, oc_ctx=octx))
    # 번호·한글·각주·서명 — 글자 오탈이 아닌 차이를 먼저 걸러 낸다
    apply_structural_rules(items, titles)
    # 于·干·於·幹·乾 묶음은 문맥으로 먼저 가린다
    resolve_gan_yu(items)
    # 혼동하기 쉬운 자형 쌍의 한 글자 차이(A·B2) → 1차·2차가 같은 쪽으로 틀렸을 수 있으니 표시만 하고 3차 판별(글자 조각)에 넘긴다
    # (문맥 규칙이 판독 편을 든 자리는 예외, 자형 편향 쌍은 아래에서 따로)
    for it in items:
        if it.action != "replace" or it.kind not in ("A", "B2") or len(it.hwpx) != 1 or len(it.ocr) != 1 or it.witness == "ocr":
            continue
        hk, ok = zh.compare_key(it.hwpx), zh.compare_key(it.ocr)      # 묶음은 간체 기준 — 번체로 읽은 판독(세로쓰기 間·帥 …)도 접어서 맞춘다
        if not _confusable(hk, ok):
            continue
        if it.kind == "B2":
            it.kind, it.action, it.new = "B1", "mark", ""
            it.note = f"판독 글자 「{it.ocr}」 는 「{hk}」 와 자형이 비슷해 헷갈리기 쉽고 번체 후보도 여럿({'/'.join(it.candidates)}) — 원본 확인"
        elif not _weak_pair(hk, ok):
            _mark(it, "B1", f"「{it.hwpx}」↔「{it.ocr}」 는 판독기가 헷갈리기 쉬운 자형 쌍 — 1차·2차가 같이 틀렸을 수 있어 자동 반영하지 않고 글자 조각 확인(3차 판별)으로 가림")
    # 入/人·曰/日 같은 자형 쌍은 OCR 엔진들이 같은 방향으로 틀리고 인쇄 자체가 오자인 일도 있어 한 글자 차이를 자동 반영하지 않는다
    for it in items:
        if it.action == "replace" and len(it.hwpx) == 1 and len(it.ocr) == 1 and it.kind in ("A", "B2") and _weak_pair(zh.compare_key(it.hwpx), zh.compare_key(it.ocr)):
            _mark(it, "B1", f"「{it.hwpx}」↔「{it.ocr}」 는 OCR 이 같은 방향으로 틀리기 쉬운 자형 편향 쌍 — 글자 조각으로 원본 확인(3차 판별)")
    # 같은 쌍이 되풀이되는 자리의 교정 후보 승격(promote_repeated)은 여기서 하지 않는다 — run_section 이 3차 판별을 못 할 때만 부른다
    # 정책에 따라 자동 반영 여부. C 삽입은 어느 정책에서도 자동으로 넣지 않는다 — 1차만 읽은 긴 구간·겹쳐 읽은 열·구획 자르기 오류가 섞여 사람 원고에 넣기엔 위험
    for it in items:
        if it.kind in ("A", "B2") and it.action in ("replace", "insert", "delete") and _long_change(len(it.hwpx), len(it.ocr)):
            _mark(it, "C", f"한쪽에 긴 구간이 붙은 차이(HWPX {len(it.hwpx)}자 ↔ 판독 {len(it.ocr)}자) — 원본 확인 뒤 사람이 고침")   # 어느 길로 왔든 긴 구간은 자동 반영하지 않는다
        if it.kind == "A" and it.action in ("replace", "insert", "delete"):
            it.auto = policy in ("sure", "all")
        elif it.kind == "B2" and it.action in ("replace", "insert"):
            it.auto = policy == "all"
        else:
            it.auto = False
        if it.action in ("replace", "insert") and it.new == it.hwpx:
            it.kind, it.action, it.auto, it.note = "D", "none", False, "표기만 다름(이체자·부호)"
    return [it for it in items if it.kind != "D"]


def summary(items: list[Item]) -> dict:
    out = {"A": 0, "B1": 0, "B2": 0, "C": 0, "auto": 0, "mark": 0}
    for it in items:
        out[it.kind] = out.get(it.kind, 0) + 1
        if it.auto: out["auto"] += 1
        else: out["mark"] += 1
    return out


# ---------- PDF 글자층을 제3의 증인으로 ----------
def _layer_key(text: str) -> str:
    return "".join(c.key for c in _cells(text or "", 0))


def witness_filter(items: list[Item], layers: dict[int, str], policy: str = "all", promote: bool = True) -> dict:
    """한 글자 차이마다 그 쪽 PDF 글자층(다른 OCR 엔진)에서 같은 자리를 찾아 본다.
    글자층이 HWPX 와 같으면 판독 오류로 보고 차이에서 뺀다. 글자층이 판독과 같으면(promote) 두 증인이 겹치므로 표시만 하던 干/于 자리도 교정 후보(B2)로 올린다
    — 글자층은 같은 화소를 본 다른 OCR 이라 자형 판별을 대신하지 못하므로 3차 판별(글자 조각)을 못 할 때만(run.py 가 promote 로 알림).
    3차 판별이 이미 가린 자리(witness 에 glyph)는 어느 쪽으로도 건드리지 않는다. 돌려주는 값은 통계 {"checked", "hwpx", "ocr", "none", "weak", "glyph"}."""
    import re
    keys = {pg: _layer_key(t) for pg, t in (layers or {}).items() if t}
    st = {"checked": 0, "hwpx": 0, "ocr": 0, "none": 0, "weak": 0, "glyph": 0}
    for it in items:
        if it.action not in ("replace", "mark") or len(it.hwpx) != 1 or len(it.ocr) != 1 or it.kind == "D":
            continue
        if "glyph" in (it.witness or ""):
            st["glyph"] += 1; continue                  # 3차 판별이 인쇄 자형을 직접 보고 가린 자리 — 글자층이 뒤집지 않는다
        if "자형 편향" in it.note or _weak_pair(zh.compare_key(it.hwpx), zh.compare_key(it.ocr)):
            st["weak"] += 1; continue                   # 글자층도 같은 식으로 틀릴 수 있는 쌍 — 증인으로 쓰지 않는다
        lk = keys.get(it.pdf_page)
        if not lk:
            continue
        pre, post = _split_ctx(it.oc_ctx)
        pk, nk = _layer_key(pre), _layer_key(post)
        found = None
        for a, b in ((4, 4), (3, 3), (4, 0), (0, 4), (2, 2), (3, 1), (1, 3), (3, 0), (0, 3)):
            if len(pk) < a or len(nk) < b or a + b < 3:
                continue
            pat = (re.escape(pk[-a:]) if a else "") + "(.)" + (re.escape(nk[:b]) if b else "")
            if not a: pat = "(?:^|(?<=.))" + pat        # 앞 문맥이 없으면 어디서든
            ms = re.findall("(?=" + pat + ")", lk)   # 반복 문맥이 겹쳐 있어도 모두 살핀다
            if ms and len(set(ms)) == 1:
                found = ms[0]; break
            if ms:
                break                                  # 같은 문맥의 글자가 서로 다르면 위치를 모르므로 증거로 쓰지 않는다
        st["checked"] += 1
        if found is None:
            st["none"] += 1; continue
        hk, ok = zh.compare_key(it.hwpx), zh.compare_key(it.ocr)
        if found == hk:
            st["hwpx"] += 1
            it.kind, it.witness = "D", "layer=hwpx"; it.note = f"PDF 글자층도 HWPX 「{it.hwpx}」 와 같음 — 판독 「{it.ocr}」 이 오류로 보여 차이에서 뺌"
        elif found == ok:
            st["ocr"] += 1
            it.witness = (it.witness + "+" if it.witness else "") + "layer=ocr"
            if promote and it.kind == "B1" and "혼동" in it.note:   # 두 증인이 겹치는 干/于 자리 → 교정 후보로(3차 판별을 못 할 때만)
                new = zh.to_traditional(it.ocr)
                cands = zh.candidates(it.ocr)
                it.kind, it.action, it.new, it.candidates = ("B2" if len(cands) > 1 else "A"), "replace", new, (cands if len(cands) > 1 else [])
                it.auto = policy == "all" if it.kind == "B2" else policy in ("sure", "all")
                it.note = f"판독과 PDF 글자층이 모두 「{it.ocr}」 — HWPX 「{it.hwpx}」 가 오탈로 보임"
        else:
            st["none"] += 1
    items[:] = [it for it in items if it.kind != "D"]
    return st
