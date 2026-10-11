"""확인 등급: 글자 하나하나를 얼마나 믿어도 되는지 색으로 보인다. 문서 조립(HWPX·DOCX)·검수 화면이 같이 쓴다.

판독문(merged)의 글은 바꾸지 않는다. 등급은 문서를 만들 때 기록(rec['adjudications'], rec['context'])에서 다시 계산해
글자 모양으로만 입힌다. 그래서 글을 복사하거나 흑백으로 인쇄해도 〔a/b〕·〔疑: x〕 같은 불확실 표기는 그대로 남는다(지침 19·22).

이 파일의 1부는 '3차 판별로 확정한 글자가 지금 판독문의 어디에 있는지' 찾는 일이다. 3차는 〔a/b〕 표시를 고른 글자로
바꿔 넣으므로(adjudicate.apply_choice) 확정 글자는 보통 글자와 구별되지 않는다. 판별 기록의 자리(영역·열·글자 번호·앞뒤 글자)로
다시 찾고, 그 자리 글자가 고른 글자와 같을 때만 확정 글자로 본다. 못 찾으면 칠하지 않고 '못 찾은 자리'로 센다.
"""
from __future__ import annotations
import re
from dataclasses import dataclass
from .merge import MARK_RE, columns_in_reading_order
from .schema import PageReading, TEXT_KINDS

# ---------- 등급별 글자 모양(HWPX·DOCX·검수 화면 공용) ----------
# 색만으로는 색각 이상·흑백 인쇄에서 구별되지 않으므로 밑줄 모양도 등급마다 다르게 둔다.
GRADE_STYLE: dict[str, dict] = {
    "green": dict(color="#0B7A3B", highlight=None, bold=False, underline="DOT", label="초록"),
    "blue": dict(color="#1F4FBF", highlight="#E1EBFF", bold=False, underline="DASH", label="파랑"),
    "red": dict(color="#C00000", highlight="#FFFF00", bold=True, underline=None, label="빨강"),
    "red_ctx": dict(color="#C00000", highlight="#FFFF00", bold=True, underline="SOLID", label="빨강 안 문맥 후보(실선 밑줄)"),
}
STYLE_ALIAS = {"mark": "red"}          # split_marks 의 mark(〔〕 표시) = 빨강

# ---------- 글자 모양 나누기(문서 조립 공용) ----------
SPLIT_RE = re.compile(r"(〔[^〔〕]*〕)")
NOTE_PREFIXES = ("〔→", "〔소주〕", "〔작업 메모〕", "〔판독 메모〕", "〔도판 미삽입", "〔이어짐〕", "〔고지: ")   # 〔고지/구자〕 같은 두 글자 후보 표시와 겹치지 않게 쌍점까지


def split_marks(text: str) -> list[tuple[str, str]]:
    """글을 (조각, 종류) 로 나눈다. 종류: body | mark(의심 자리) | note(작업 표시)."""
    out = []
    for seg in SPLIT_RE.split(text):
        if not seg:
            continue
        if seg.startswith("〔") and seg.endswith("〕"):
            out.append((seg, "note" if seg.startswith(NOTE_PREFIXES) else "mark"))
        else:
            out.append((seg, "body"))
    return out


def styled_runs(text: str, spans=(), memos=()) -> list[tuple[str, str, list[str]]]:
    """글 → [(조각, 모양, 그 조각에 걸 메모들)]. 기본 모양은 split_marks(body·mark·note), spans 의 (시작, 끝, 모양) 이 덮어쓴다.
    메모는 글자 하나에 건다: 메모 자리의 글자는 따로 조각이 된다(한컴에서 메모 범위가 글자 하나만 물들게)."""
    if not text:
        return []
    st: list[str] = []
    for seg, kind in split_marks(text):
        st.extend([kind] * len(seg))
    for a, b, k in spans:
        for i in range(max(0, a), min(len(text), b)):
            st[i] = k
    at: dict[int, list[str]] = {}
    for off, msg in memos:
        k = min(max(0, off), len(text) - 1)
        if msg not in at.get(k, []):
            at.setdefault(k, []).append(msg)
    at = {k: [" / ".join(v)] for k, v in at.items()}                   # 한 글자에는 메모 하나(겹친 메모 범위를 만들지 않게)
    out: list[tuple[str, str, list[str]]] = []
    i = 0
    while i < len(text):
        j = i + 1
        if i not in at:
            while j < len(text) and st[j] == st[i] and j not in at:
                j += 1
        out.append((text[i:j], st[i], at.get(i, [])))
        i = j
    return out


def _plain_map(text: str) -> tuple[str, list[int | None]]:
    """열 글 → (표시를 1차 글자로 접은 글, 접은 글의 글자마다 원래 글에서의 위치). 표시 안에서 온 글자는 위치가 None."""
    out: list[str] = []; pos: list[int | None] = []; last = 0
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


def locate_choice(col_text: str, char_index: int, choice: str, pre: str = "", post: str = "", near: int = 3) -> int | None:
    """3차가 고른 글자(choice)가 열 글의 어느 위치(원래 글의 글자 번호)에 들어 있는지. 표시 밖의 보통 글자여야 하고,
    제자리(char_index) 가까이(±near)에서 앞뒤 글자가 가장 잘 맞는 자리를 고른다. 없으면 None.
    (제자리는 판별 때의 글자 번호를 같은 차례의 앞쪽 선택과 뒤 차례의 선택이 바꾼 길이만큼 고친 번호 — decided_positions 가 고쳐 넘긴다.
    그래도 사람이 고쳤거나 비교를 다시 계산해 한두 자 밀릴 수 있으므로 가까운 범위를 본다.)
    제자리 글자가 앞뒤 글자 하나라도 맞으면 그 자리를 앞세우고, 제자리가 아닌 후보는 앞뒤 글자 하나는 맞아야 받는다
    (이웃 표시를 b 로 정했거나 사람이 고친 뒤에 근처의 같은 글자 — 1차·2차가 같게 읽은 글자 — 를 잡지 않게)."""
    if not choice:
        return None
    plain, pos = _plain_map(col_text)
    best: tuple | None = None; best_at: int | None = None
    for i in range(max(0, char_index - near), min(len(plain) - len(choice), char_index + near) + 1):
        if plain[i:i + len(choice)] != choice:
            continue
        span = pos[i:i + len(choice)]
        if any(p is None for p in span) or span != list(range(span[0], span[0] + len(choice))):
            continue                                                  # 아직 표시 안에 있는 글자(미정)이거나 끊긴 자리
        fit = (1 if pre and plain[:i].endswith(pre[-1:]) else 0) + (1 if post and plain[i + len(choice):].startswith(post[:1]) else 0)
        if i != char_index and fit == 0:
            continue                                                  # 제자리가 아니면 앞뒤 글자 하나는 맞아야 한다(근처의 같은 글자를 잡지 않게)
        key = (i == char_index and fit > 0, fit, -abs(i - char_index))   # 제자리에서 이웃이 하나라도 맞으면 그 자리
        if best is None or key > best:
            best, best_at = key, span[0]
    return best_at


@dataclass
class Located:
    region_id: str
    para_index: int        # 영역 안 문단 번호
    start: int             # 문단 글(paragraph_text) 안 시작 위치
    end: int
    entry: dict            # 판별 기록 한 줄(rec['adjudications'] 의 원소)


def dup_region_ids(pr: PageReading) -> set[str]:
    """한 쪽 안에서 둘 이상 나온 영역 id(모델이 id 를 겹쳐 낸 경우). 등급·문맥 자리는 (영역 id, 문단 번호)로 붙으므로
    이 영역들에는 칠하지 않는다(다른 영역의 같은 문단 번호에 칠해지지 않게). 삽화·표 같은 글 아닌 영역과 겹친 id 도 일부러 센다:
    3차가 조각을 자를 때 영역 상자를 id 로 찾으므로(adjudicate 의 {r.id: r.bbox}) 그 조각이 글 아닌 영역에서 잘렸을 수 있다."""
    ids = [r.id for r in pr.regions]
    return {i for i in ids if ids.count(i) > 1}


def adjudication_runs(adjudications: list[dict] | None) -> list[int]:
    """판별 기록마다 몇 번째 3차 실행(find_sites 차례)에서 나왔는지. adjudicate_page 가 적은 run 을 쓰고, run 이 없는 예전 기록은
    (영역 id, 열 번호)마다 목록 차례로 어림한다: 그 열의 앞 기록과 글자 수(n_chars)가 다르거나 글자 번호가 커지지 않으면 새 차례
    (한 차례 안에서는 find_sites 가 열마다 글자 번호 순으로 낸다). 어림한 번호는 같은 열 안에서만 견준다."""
    out: list[int] = []; last: dict[tuple, tuple] = {}                 # (영역 id, 열 번호) → (차례, 글자 수, 글자 번호)
    for a in adjudications or []:
        key = (a.get("region_id", ""), int(a.get("col_index", -1))); i = int(a.get("char_index", 0)); p = last.get(key)
        run = int(a["run"]) if a.get("run") else 1 if p is None else p[0] + int(a.get("n_chars") != p[1] or i <= p[2])
        last[key] = (run, a.get("n_chars"), i); out.append(run)
    return out


def decided_positions(merged: PageReading, adjudications: list[dict] | None) -> tuple[list[Located], list[dict]]:
    """3차 확정 글자의 자리(문단 글 기준)와 못 찾은 판별 기록. 문단 글 = 그 문단 열들을 열 번호 순으로 이은 글(schema.paragraph_text).
    id 가 겹친 영역의 기록은 어느 영역인지 가릴 수 없으므로 못 찾은 것으로 둔다.
    제자리는 (영역, 열)마다 확정 기록을 목록 차례(시간 차례)로 다시 놓아 구한다: 판별 때 글자 번호 + 같은 차례(adjudication_runs)에서
    그보다 앞쪽에 고른 글자의 길이 차(〔己入/已〕→已 면 -1). 하나를 놓을 때마다 앞 차례에서 놓은 자리 중 그보다 뒤인 것을 그 길이 차만큼 민다
    (뒤 차례의 글자 번호는 앞 차례가 바꾼 글에서 셌으므로 앞 차례의 길이 차는 이미 들어 있고, 앞 차례의 자리는 뒤 차례가 바꾼 만큼 밀린다)."""
    found: list[Located] = []; missing: list[dict] = []
    dup = dup_region_ids(merged)
    regs = {r.id: r for r in merged.regions if r.kind in TEXT_KINDS and r.id not in dup}
    own: dict[int, int] = {}                                         # 기록 번호 → 지금 열 글(표시를 접은 글)에서의 제자리
    placed: dict[tuple, list[tuple[int, int, int, int]]] = {}         # (영역 id, 열 번호) → [(기록 번호, 차례, 판별 때 글자 번호, 고른 글자 길이 - 1차 글자 길이)]
    for k, (a, run) in enumerate(zip(adjudications or [], adjudication_runs(adjudications))):
        if not a.get("choice"):
            continue
        i0, d = int(a.get("char_index", 0)), len(a["choice"]) - len(a.get("a", ""))
        col = placed.setdefault((a.get("region_id", ""), int(a.get("col_index", -1))), [])
        own[k] = i0 + sum(dd for _, r, j, dd in col if r == run and j < i0)
        for kk, r, _, _ in col:
            if r < run and own[kk] > own[k]:
                own[kk] += d                                         # 앞 차례에서 놓은 뒤쪽 글자는 이 글자의 길이 차만큼 밀린다
        col.append((k, run, i0, d))
    for k, a in enumerate(adjudications or []):
        if not a.get("choice"):
            continue
        reg = regs.get(a.get("region_id", ""))
        if reg is None:
            missing.append(a); continue
        # 읽기 순서 열 번호 → (문단 번호, 문단 글 안의 그 열 시작 위치)
        where = []
        for pi, p in enumerate(reg.paragraphs):
            off = 0
            for c in sorted(p.columns, key=lambda c: c.col_from_right):
                where.append((pi, off, c)); off += len(c.text or "")
        ci = int(a.get("col_index", -1))
        if not 0 <= ci < len(where):
            missing.append(a); continue
        pi, off, col = where[ci]
        at = locate_choice(col.text or "", own[k], a["choice"], a.get("pre", ""), a.get("post", ""))
        if at is None:
            missing.append(a); continue
        found.append(Located(reg.id, pi, off + at, off + at + len(a["choice"]), a))
    return found, missing


def _check_columns_order(reg) -> bool:
    """columns_in_reading_order 와 decided_positions 의 열 차례가 같은지(시험용)."""
    flat = [c for p in reg.paragraphs for c in sorted(p.columns, key=lambda c: c.col_from_right)]
    return [id(c) for c in flat] == [id(c) for c in columns_in_reading_order(reg)]


# ---------- 쪽마다 등급·고지 계산 ----------
LEGEND = ("[연어OCR 표시 범례] 검정 = 1차·2차 판독이 같게 읽은 글자(사람 대조 전) · 초록(점선 밑줄) = 갈린 자리를 3차 자형 판별로 고른 글자"
          "(기계 판별, 정밀도 미측정 — 훑어 확인) · 파랑(파선 밑줄) = 골랐지만 조각 위치를 확인하지 못했거나 이 판독의 다른 대목과 어긋남(확인 권장) · "
          "빨강 굵게·노랑 〔 〕 = 정하지 못함, 원본 확인 필수(지침 19) · 〔 〕 안 한 후보의 실선 밑줄 = 이 판독의 다른 대목이 가리키는 후보"
          "(문맥 추정, 확정 아님, 문턱 미측정) · 괄호 〔 〕가 기준: 색이 사라져도 남으므로 Ctrl+F 로 〔 를 찾아 남은 자리를 돈다 · "
          "색이 남은 곳 = 아직 사람이 확인하지 않은 곳, 확인 뒤 모양 복사(Alt+C)로 지움 · 메모 = 색으로 전할 수 없는 고지 사항")
COLLATE_LEGEND = ("[연어OCR 표시 범례 — 책갈피 교정] 초록(점선 밑줄) = 3차 자형 판별 근거로 고친 글자 · 파랑(파선 밑줄) = 자동으로 고쳤지만 확인 권장"
                  "(판독 일치·사전·글자층·반복 근거, 정밀도 미측정) · 빨강 굵게·노랑 = 확인 요청, 글자는 HWPX 그대로 · 메모 = 지운 자리·넣지 않은 자리·"
                  "구조 차이·반영하지 않은 제안 같은 고지 사항 · 원래 글자와 근거 전체는 보고(collate 폴더의 html) · "
                  "0.6.3 까지의 '파랑+연두'는 예전 자동 교정 색 · 확인한 글자는 모양 복사(Alt+C)로 색을 지움")
CONFLICT_MEMO_MAX = 3          # 쪽마다 글자에 다는 '자형·문맥 어긋남' 메모 수(나머지는 쪽 고지에 셈)


@dataclass
class PageGrades:
    spans: dict          # (영역 id, 문단 번호) → [(시작, 끝, 등급)]  문단 글(paragraph_text) 기준
    memos: dict          # (영역 id, 문단 번호) → [(위치, 메모 글)]
    page_notices: list   # 쪽 기록 줄에 달 고지
    stats: dict
    no_pass2: bool = False   # 이 쪽은 2차 판독 없이 1차만 들어감(범례에 쪽 목록으로)


def _where(u, horizontal: bool = False) -> str:
    """확인 항목의 자리(지침 19): 영역 · 오른쪽에서 n번째 열(가로쓰기면 위에서 n번째 줄) · 위/중간/아래."""
    parts = [u.region_id] if u.region_id else []
    if u.col_from_right:
        parts.append(f"위에서 {u.col_from_right}번째 줄" if horizontal else f"오른쪽에서 {u.col_from_right}번째 열")
    if u.position:
        parts.append(u.position)
    return " ".join(parts)


def classify_uncertain(u, body_text: str, decided: set) -> str:
    """확인 항목이 문서에 이미 보이는지: shown(본문의 〔〕 로 보임) | decided(3차가 정해 초록·파랑으로 보임) | notice(어디에도 안 보임 → 고지).
    '3차 판별: 「x」 확정' 주석 글에 기대지 않는다(비교 다시 계산이 확인 항목을 새로 만들면 그 주석이 사라진다)."""
    marks = [m for m in MARK_RE.findall(u.context or "") if not m.startswith(NOTE_PREFIXES)]
    if marks and all(m in body_text for m in marks):
        return "shown"
    missing = [m for m in marks if m not in body_text]
    if marks and all(((u.region_id, m) in decided) or (not u.region_id and any(k[1] == m for k in decided)) for m in missing):
        return "decided"
    return "notice"


def page_body(pr: PageReading) -> str:
    from .schema import paragraph_text
    return "".join(paragraph_text(p) for r in pr.regions for p in r.paragraphs) + "".join(r.title_text for r in pr.regions) \
        + "".join(c for r in pr.regions for row in r.table_rows for c in row.cells)


def decided_keys(rec: dict) -> set:
    return {(a.get("region_id", ""), a.get("mark", "")) for a in rec.get("adjudications") or [] if a.get("choice")}


def _pass2_regions(rec: dict) -> set:
    out = set()
    for rid, v in (rec.get("pass2") or {}).items():
        cols = (v or {}).get("columns") or (v or {}).get("cols") or []
        if any((c.get("text") or c.get("t") or "").strip() for c in cols if isinstance(c, dict)):
            out.add(rid)
    return out


def page_grades(rec: dict, ctx: dict | None = None, memo_mode: str = "notice", use_context: bool = True) -> PageGrades:
    """한 쪽의 등급(초록·파랑·빨강 안 문맥 밑줄)과 고지 메모. 글은 바꾸지 않는다.
    memo_mode: notice(고지 사항만) | all(등급 근거까지) | inline(메모 없이 — 고지는 문서 조립이 회색 글로 넣음)."""
    from .adjudicate import adjacent_seen
    pr = PageReading.model_validate(rec["merged"])
    horizontal = (rec.get("mode") or "") == "horizontal_zh"
    spans: dict = {}; memos: dict = {}; notices: list[str] = []
    stats = {"green": 0, "blue": 0, "red_ctx": 0, "unmapped": 0, "conflict": 0, "notices": 0}
    ctx_sites = ((ctx or {}).get("sites") or []) if use_context else []
    dup = dup_region_ids(pr)
    ctx_sites = [s for s in ctx_sites if s.get("region_id") not in dup]       # id 가 겹친 영역에는 문맥 밑줄도 칠하지 않는다
    contra = {(s["region_id"], s["para"], s["start"]): s for s in ctx_sites if s.get("state") == "decided" and s.get("verdict") == "contradicts"}
    found, missing = decided_positions(pr, rec.get("adjudications"))
    n_conflict_memo = 0
    for f in found:
        e = f.entry; key = (f.region_id, f.para_index)
        c = contra.get((f.region_id, f.para_index, f.start))
        seen = adjacent_seen(e.get("window", ""), e.get("pre", ""), e.get("choice", ""), e.get("post", ""))
        grade = "green" if (seen and c is None) else "blue"
        spans.setdefault(key, []).append((f.start, f.end, grade)); stats[grade] += 1
        if c is not None:
            stats["conflict"] += 1
            if n_conflict_memo < CONFLICT_MEMO_MAX:
                n_conflict_memo += 1
                sc = c.get("score") or {}
                memos.setdefault(key, []).append((f.start, f"[자형·문맥 어긋남] 3차 「{e.get('choice', '')}」, 이 판독의 다른 대목은 「{c.get('suggest', '')}」 "
                                                             f"{sc.get(c.get('suggest', ''), 0)}:{sc.get(e.get('choice', ''), 0)} — 원본 오자 또는 판별 오류일 수 있음, "
                                                             "글자는 3차대로 둠(원본 확인)"))
        elif memo_mode == "all":
            memos.setdefault(key, []).append((f.start, f"3차 자형 판별: 1차 「{e.get('a', '')}」·2차 「{e.get('b', '')}」 → 「{e.get('choice', '')}」"
                                                         + (f" · 근거 {e.get('evidence', '')[:60]}" if e.get("evidence") else "")
                                                         + ("" if grade == "green" else " · 조각 위치를 확인하지 못함")))
    for s in ctx_sites:
        if s.get("state") != "open" or s.get("verdict") != "suggest":
            continue
        key = (s["region_id"], s["para"])
        a, pick = s["a"], s.get("suggest", "")
        off = s["start"] + 1 + (0 if pick == a else len(a) + 1)          # 〔a/b〕 안 그 후보 자리 — 괄호 글은 그대로
        spans.setdefault(key, []).append((off, off + len(pick), "red_ctx")); stats["red_ctx"] += 1
        if memo_mode == "all":
            memos.setdefault(key, []).append((off, f"[문맥 추정 — 확정 아님] {s.get('reason', '')}"))
    # ---- 쪽 고지(쪽 기록 줄 끝 글자에 하나로) ----
    body = page_body(pr); dec = decided_keys(rec)
    hidden = [f"{_where(u, horizontal)}: {(u.context or '').strip()[:80]}".strip(": ") for u in pr.uncertain if classify_uncertain(u, body, dec) == "notice"]
    if hidden:
        notices.append("[본문에 표시되지 않은 확인 항목] " + " / ".join(hidden[:4]) + (f" 외 {len(hidden) - 4}건 — 검수 화면(review.html)" if len(hidden) > 4 else ""))
    errs = [i for i in rec.get("verify") or [] if i.get("level") == "error"]
    if errs:
        notices.append("[구조 검사 오류] " + " / ".join(f"{i.get('rule', '')}({i.get('where', '')}): {i.get('msg', '')[:50]}" for i in errs[:3]) + (f" 외 {len(errs) - 3}건" if len(errs) > 3 else ""))
    touching = [k for k, c in (rec.get("crops") or {}).items() if isinstance(c, dict) and c.get("touching")]
    if touching:
        notices.append(f"[도판 경계] 잘라 넣은 도판 {', '.join(touching[:4])} 의 가장자리에 글자·선이 닿음 — 잘림 확인")
    if dup:
        notices.append(f"[영역 id 겹침] 이 쪽에 같은 영역 id({', '.join(sorted(dup)[:5])})가 둘 이상 있어 그 영역들에는 확인 등급·문맥 밑줄을 칠하지 않음 — 원본 확인")
    missing = [m for m in missing if m.get("region_id", "") not in dup]          # id 가 겹친 영역의 기록은 위 [영역 id 겹침] 고지가 이미 알린다
    if missing:
        stats["unmapped"] = len(missing)
        notices.append(f"3차로 정한 {len(missing)}곳을 지금 판독문에서 찾지 못해 색을 입히지 못함: " + ", ".join(f"「{m.get('choice', '')}」" for m in missing[:5]))
    if stats["conflict"] > n_conflict_memo:
        notices.append(f"3차 판별과 이 판독의 다른 대목이 어긋난 자리가 {stats['conflict'] - n_conflict_memo}곳 더 있음(파랑) — 검수 화면 참고")
    text_regs = [r for r in pr.regions if r.kind in TEXT_KINDS and any((c.text or "").strip() for p in r.paragraphs for c in p.columns)]
    p2 = _pass2_regions(rec)
    no_pass2 = bool(text_regs) and not any(r.id in p2 for r in text_regs) and rec.get("reader") != "mock"
    if not no_pass2:
        lack = [r.id for r in text_regs if r.id not in p2]
        if lack and p2:
            notices.append(f"[2차 판독 없음] 영역 {', '.join(lack[:5])} 은 2차 판독 없이 1차만 들어감 — 갈림 표시가 없어도 원본 확인")
    stats["notices"] = len(notices)
    return PageGrades(spans, memos, notices, stats, no_pass2)


def page_ranges(pages: list[int]) -> str:
    """[3, 5, 6, 7, 9] → '3, 5–7, 9'."""
    out = []; ps = sorted(set(pages)); i = 0
    while i < len(ps):
        j = i
        while j + 1 < len(ps) and ps[j + 1] == ps[j] + 1:
            j += 1
        out.append(str(ps[i]) if i == j else f"{ps[i]}–{ps[j]}"); i = j + 1
    return ", ".join(out)


# ---------- 책갈피 OCR(collate) 의 등급과 메모 ----------
def collate_grade(it) -> str | None:
    """책갈피 교정 항목의 등급. 초록은 일반 OCR 과 같은 뜻(3차 자형 판별 근거)일 때만 — 판독 일치만으로 한 자동 교정은
    정밀도를 아직 재지 않았으므로 파랑(확인 권장). 표시만 하는 항목은 빨강, 표기 차이(D)는 없음."""
    if it.kind == "D":
        return None
    auto = it.auto and it.action in ("replace", "insert", "delete")
    if not auto:
        return "red"
    if it.kind == "A" and it.action in ("replace", "insert") and "glyph=ocr" in (it.witness or "").split("+"):
        return "green"
    return "blue"


def needs_memo(it, mode: str) -> bool:
    """책갈피 교정 항목에 메모(또는 inline 짧은 표기)를 달지. all 은 모두, notice·inline 은 색으로 전할 수 없는 고지 사항만:
    지운 글자, 폭 없는 표시, 구조 차이(C), 구조 규칙이 붙인 고지, 반영하지 않은 제안."""
    if mode == "all":
        return True
    return (it.action == "delete" or (it.end == it.start and not it.auto) or it.kind == "C" or bool(getattr(it, "notice", False))
            or (not it.auto and bool(it.new)))
