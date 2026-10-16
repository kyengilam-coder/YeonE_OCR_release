"""작업 탭 진행 카드의 그림 값(디자인 명세 §5.3~§5.5·§7): 단계 띠(6단계 — 한 번에 인식은 progress.PLANS['run'], 문자 PDF 는 ['run-pdf']),
상태 알약, 큰 숫자 줄, 쪽 띠의 칸 나누기·눈금. 창(tkinter) 없이 검사할 수 있게 창 프로그램과 따로 둔다 — 창은 여기서 받은 값대로 그리기만 한다.

줄 하나는 (글, 꾸밈) 조각의 목록이다. 꾸밈: 'cap' 보조 글(MUTED) · 'cap_b' 굵은 먹 · 'red_b' 굵은 빨강(실패) · 'red' · 'warn'(주의).
'TAG' 줄은 꼬리표 `[아니요]면 뺌`(제 줄 — 옆 글과 겹치지 않게).
"""
from __future__ import annotations
import math
import re

from .progress import PLANS, STAGE_NAMES, TAIL_STEP, UNITS

TAG = "TAG"
RUN_CMDS = ("run", "run-pdf")
STEP_COMMANDS = ("prepare", "read", "adjudicate", "build", "review", "report")     # 한 번에 인식의 한 단계만 따로 하는 명령(고급 도구 ①~⑥)
# 열 최소 너비(논리 px)와 무게 — 명세 §4 '단계 띠 열'. 창은 그 열의 가장 긴 줄을 잰 값 + 10 과 견줘 큰 쪽을 쓴다.
COL_MIN = (100, 138, 128, 128, 84, 74)
COL_WEIGHT = (8, 10, 13, 13, 8, 8)
FAILED_PAGES = re.compile(r"판독에 실패한 쪽 (\d+)개: ([\d, ]+)")


def steps_of(cmd: str) -> list[str]:
    """한 번에 인식의 단계 이름(열쇠) — 문자 PDF 는 4번이 textlayer(글자층 넣기)."""
    return [s for s, _ in PLANS.get("run-pdf" if cmd == "run-pdf" else "run")]


def failed_pages(problems: list[str] | None) -> list[int]:
    """끝난 실행의 '오류: 판독에 실패한 쪽 n개: 9, 12 …' 줄에서 실패한 쪽(앞 30개까지 — cmd_read 가 그만큼만 적음)."""
    for p in reversed(problems or []):
        m = FAILED_PAGES.search(p)
        if m:
            return [int(x) for x in m.group(2).replace(" ", "").split(",") if x.isdigit()]
    return []


def failed_count(problems: list[str] | None) -> int:
    for p in reversed(problems or []):
        m = FAILED_PAGES.search(p)
        if m:
            return int(m.group(1))
    return 0


def money(v) -> str:
    if v is None:
        return "단가 미입력"
    return f"${v[0]:.2f}" if abs(v[0] - v[1]) < 0.005 else f"${v[0]:.2f}~{v[1]:.2f}"


def _sum(rows: list[dict], steps: tuple[str, ...]):
    """장부 줄(step·calls·usd)에서 그 단계들의 호출·달러 합. 줄이 없으면 None, 단가가 없으면 usd None."""
    mine = [r for r in rows or [] if r.get("step") in steps]
    if not mine:
        return None
    calls = [sum((r.get("calls") or [0, 0])[0] for r in mine), sum((r.get("calls") or [0, 0])[1] for r in mine)]
    usd = None if any(r.get("usd") is None and (r.get("calls") or [0, 0])[1] for r in mine) else \
        [sum((r.get("usd") or [0, 0])[0] for r in mine), sum((r.get("usd") or [0, 0])[1] for r in mine)]
    return calls, usd


def _spend(prefix: str, s) -> list[tuple[str, str]]:
    calls, usd = s
    if usd is None:
        return [(prefix, "cap"), ("단가 미입력", "warn")]
    if not calls[1]:
        return [(prefix + "$0.00", "cap")]
    return [(prefix, "cap"), (money(usd), "cap_b")]


def _calls(s) -> list[tuple[str, str]]:
    c = s[0]
    return [(f"호출 {c[0]:,}회" if c[0] == c[1] else f"호출 {c[0]:,}~{c[1]:,}회", "cap")]


def _cost_lines(key: str, rows, est: str) -> list:
    """단계 아래 비용 줄(장부와 같은 어림 — 명세 §5.3·§9). est: 'none' 어림 전 · 'stale' 어림 다시 필요 · 'ok'."""
    if est == "none":
        return [[("어림 전", "cap")]]
    if est == "stale":
        return [[("어림 다시 필요", "warn")]]
    out = []
    if key == "read":
        for step, name in (("read1", "1차 예상 "), ("read2", "2차 예상 ")):
            s = _sum(rows, (step,))
            if s is not None:
                out.append(_spend(name, s))
    elif key == "adjudicate":
        s = _sum(rows, ("glyph", "suspect"))
        if s is not None:
            out += [_spend("예상 ", s), _calls(s)]
    elif key == "build":
        s = _sum(rows, ("choice",))
        if s is not None:
            out += [_spend("예상 ", s), _calls(s)]
    return out


WHAT = {"prepare": "쪽 이미지", "read": "1차·2차", "adjudicate": "글자 판별·합의 의심", "build": "HWPX", "textlayer": "문자 PDF",
        "review": "review.html", "report": "report.md"}
DONE = {"build": "끝 · HWPX", "textlayer": "끝 · 문자 PDF", "review": "끝 · review.html", "report": "끝 · report.md"}


def _now_status(key: str, done: int, total: int, note: str = "") -> list[tuple[str, str]]:
    if key in TAIL_STEP:
        t = total - 1
        if done >= t:
            return [(note or TAIL_STEP[key], "cap_b")]
        return [(f"{done:,} / {t:,}{UNITS.get(key, '쪽')}", "cap_b")]
    if key in ("review", "report") or total <= 0:
        return [("만드는 중", "cap_b")]
    return [(f"{min(done, total):,} / {total:,}{UNITS.get(key, '쪽')}", "cap_b")]


def band_view(cmd: str, *, phase: str, idx: int = -1, done: int = 0, total: int = 0, note: str = "", seen: dict | None = None,
              result: dict | None = None, without: bool = False, counts: dict | None = None, rows: list | None = None,
              est: str = "none", ctx_model: bool = True) -> list[dict]:
    """단계 띠 여섯 열의 그림 값. phase: 'empty'(원본 없음) · 'ready'(실행 전) · 'running' · 'done'(끝난 뒤 — result 의 kind·rc).
    idx·done·total·note 는 Tracker 의 지금 단계, seen 은 실행 중 단계마다 마지막으로 본 (끝난 수, 전체), counts 는 실행을 시작할 때의 책 수치
    (target·records·to_read), rows 는 장부(마지막 run 어림)의 줄, without 은 [아니요 · 모델 단계 없이]로 시작한 실행, ctx_model 은 문맥 교정 모델을 쓰는가.
    열마다 {key, name, num, circle, receipt, bold, tag_top, lines, connector} — circle: done·now·wait·skip·fail·stop."""
    keys = steps_of(cmd)
    seen = seen or {}
    res = result or {}
    kind, rc = res.get("kind"), res.get("rc")
    nfail = int(res.get("failed_n") or 0) if rc == 3 and res.get("failed") else 0
    out = []
    for i, key in enumerate(keys):
        # ---- 동그라미 ----
        if phase in ("empty", "ready"):
            circle = "wait"
        elif phase == "running":
            circle = "done" if i < idx else "now" if i == idx else "wait"
        elif kind in ("ok", "partial"):
            circle = "done"
            if key == "read" and nfail:
                circle = "fail"
            if key == "adjudicate" and rc == 4:
                circle = "fail"
        else:                                                           # 중지·오류: 그때 단계까지
            at = max(0, idx)
            if kind == "error" and res.get("failed_n") and "read" in keys:
                at = min(at, keys.index("read"))                        # 판독이 실패한 실행 — 뒤 단계는 기록 없이 지나갔을 뿐(끝난 것이 아님)
            circle = "done" if i < at else ("stop" if kind == "stopped" else "fail") if i == at else "wait"
        if key == "adjudicate" and without and phase in ("running", "done") and circle in ("wait", "now", "done"):
            circle = "skip"                                             # [아니요]로 시작한 실행 — 3차 판별을 하지 않음
        # ---- 상태 줄 ----
        sd, st = seen.get(key, (0, 0))
        if circle == "wait" and phase in ("running", "done"):
            status = [("기다림", "cap")]
        elif circle == "wait":                                          # 실행 전·원본 없음
            if key == "prepare" and counts and phase == "ready":
                r = counts.get("records") or 0
                status = [(f"기록 있는 {r:,}쪽" if r else "기록 있는 쪽 없음", "cap")]
            elif key == "read" and counts and phase == "ready":
                status = [(f"새로 읽을 {counts.get('to_read', 0):,}쪽", "cap")]
            else:
                status = [(WHAT[key], "cap")]
        elif circle == "now":
            status = _now_status(key, done, total, note)
        elif circle == "skip":
            status = [("이번엔 뺌", "cap")]
        elif circle == "stop":
            left = max(0, total - done)
            status = [("중지됨 · 남은 " + f"{left:,}{UNITS.get(key, '쪽')}" if total and key not in ("review", "report") else "중지됨", "cap")]
        elif circle == "fail":
            if key == "read" and nfail:
                t = st or (counts or {}).get("to_read") or 0
                status = [(f"끝 {max(0, t - nfail):,}쪽 · ", "cap"), (f"실패 {nfail:,}쪽", "red_b")]
            elif key == "adjudicate" and rc == 4:
                status = [("못 마침 · 〔a/b〕 남음", "red")]
            else:
                status = [("오류 — 진행 기록", "red")]
        else:                                                           # 끝남
            if key == "build" and rc == 5:
                status = [("끝 · 교정 기록 확인", "warn")]
            elif key in DONE:
                status = [(DONE[key], "cap")]
            elif key in ("prepare", "read"):
                status = [(f"끝 · {st:,}쪽" if st else "끝", "cap")]
            else:
                status = [("끝", "cap")]
        lines: list = [status]
        # ---- 더 붙는 줄(비용·설명) ----
        if key == "prepare":
            if counts and phase != "empty" and counts.get("to_read"):
                n = counts["to_read"]
                lines += [[(f"나머지 {n:,}쪽은" if counts.get("records") else f"{n:,}쪽은", "cap")], [("판독하며 준비", "cap")]]
            lines.append([("모델 안 씀", "cap")])
        elif key == "read":
            lines += _cost_lines(key, rows, "none" if phase == "empty" else est)
        elif key == "adjudicate":
            if circle != "skip":
                lines += _cost_lines(key, rows, "none" if phase == "empty" else est)
        elif key == "build":
            if not ctx_model:
                lines.append([("문맥 교정 모델 꺼짐(설정)", "cap")])
            else:
                lines += [[("문맥 교정(번체)", "cap")], TAG]
                if without and phase in ("running", "done"):
                    lines.append([("이번엔 뺌", "cap")])
                else:
                    lines += _cost_lines(key, rows, "none" if phase == "empty" else est)
        else:                                                           # 글자층 넣기·검수 화면·보고
            if key == "textlayer" and circle != "done" and status != [(WHAT[key], "cap")]:
                lines.append([(WHAT[key], "cap")])                      # 실행 중에도 무엇을 만드는지(문자 PDF) — 명세 §5.3
            lines.append([("모델 안 씀", "cap")])
        receipt = key in ("read", "adjudicate") or (key == "build" and ctx_model)
        out.append({"key": key, "name": STAGE_NAMES.get(key, key), "num": i + 1, "circle": circle, "receipt": receipt,
                    "bold": circle in ("done", "now", "fail", "stop"), "tag_top": key == "adjudicate", "lines": lines})
    for i, v in enumerate(out):
        v["connector"] = None if i == len(out) - 1 else ("done" if v["circle"] == "done" else "plain")
    return out


def task_view(cmd: str, label: str, *, phase: str, result: dict | None = None) -> dict:
    """한 번에 인식이 아닌 명령(§7.2): 단계 띠 대신 한 줄 — 동그라미 + '이 작업: {label}' + 부제."""
    kind = (result or {}).get("kind")
    if phase == "running":
        circle = "now"
    elif kind in ("ok", "partial"):
        circle = "fail" if kind == "partial" and (result or {}).get("failed") else "done"
    elif kind == "stopped":
        circle = "stop"
    else:
        circle = "fail"
    sub = "한 번에 인식의 한 단계만 따로 합니다" if cmd in STEP_COMMANDS else ""
    return {"circle": circle, "title": f"이 작업: {label}", "sub": sub}


def pill_view(*, phase: str, stage: str = "", label: str = "", result: dict | None = None) -> tuple[str, str]:
    """상태 알약(글, 모양) — 모양: idle(EDGE 테) · run(먹 테·굵게) · green · fail(RED) · warn · mute. v_status 에 묶지 않는다(§5.5)."""
    if phase == "empty":
        return "원본 없음", "mute"
    if phase == "ready":
        return "준비됨", "idle"
    if phase == "running":
        if stage:
            return f"● {STAGE_NAMES.get(stage, stage)} 중", "run"
        return (f"● {label[:12]} 중" if label else "● 시작 중"), "run"
    res = result or {}
    kind, rc = res.get("kind"), res.get("rc")
    if kind == "ok":
        return "✓ 끝", "green"
    if kind == "partial":
        if rc == 3 and res.get("failed"):
            return "! 끝 · 실패한 쪽 있음", "fail"
        return {3: "끝 · 확인할 것 있음", 4: "끝 · 3차 판별 못 마침", 5: "끝 · 교정 기록 확인"}.get(rc, "끝 · 확인할 것 있음"), "warn"
    if kind == "stopped":
        return "■ 중지됨", "mute"
    return "오류로 멈춤", "fail"


def now_text(pages) -> str:
    """지금 판독 중인 쪽들(쪽 알림의 'now') → '14~18'(이어지면) · '14·16·19'(띄엄띄엄 — 넷이 넘으면 앞 넷 뒤에 '…')."""
    ps = sorted(set(int(n) for n in pages or ()))
    if not ps:
        return ""
    if len(ps) > 1 and ps[-1] - ps[0] == len(ps) - 1:
        return f"{ps[0]}~{ps[-1]}"
    return "·".join(map(str, ps[:4])) + ("…" if len(ps) > 4 else "")


def big_view(cmd: str, *, phase: str, stage: str = "", done: int = 0, total: int = 0, note: str = "", seen: dict | None = None,
             result: dict | None = None, counts: dict | None = None, screenshot: bool = False, label: str = "",
             now_pages=()) -> list[tuple[str, str]]:
    """큰 숫자 줄의 왼쪽 조각(§5.5). 꾸밈: 'm' 보조(MUTED) · 'big' 큰 숫자(바탕 24) · 'unit' 단위(맑은 고딕 12 MUTED) · 'body' · 'b' 굵게 · 'red_b'.
    now_pages 는 판독 중 쪽 알림이 '지금'이라 한 쪽들 — '· 지금 14~18쪽 판독 중 (동시 5쪽)'."""
    seen = seen or {}
    if phase == "empty":
        return [("원본을 고르면 쪽마다 칸이 생기고, 새로 읽을 쪽 수와 어림 호출이 여기 나옵니다.", "m")]
    if phase == "ready":
        if not counts:
            return [("새로 읽을 쪽을 세는 중…", "m")]
        t, r, n = counts.get("target", 0), counts.get("records", 0), counts.get("to_read", 0)
        if screenshot and t == 1:
            tail = " · 스크린샷 한 장 · " + ("판독 기록 있음" if r else "판독 기록 없음")
        elif n == t and r:
            tail = f" · 대상 {t:,}쪽 모두 다시 판독"
        elif r:
            tail = f" · 대상 {t:,}쪽 가운데 판독 기록 있는 {r:,}쪽은 건너뜀"
        else:
            tail = f" · 대상 {t:,}쪽 · 판독 기록 없음"
        return [("새로 읽을", "m"), (f"{n:,}", "big"), ("쪽", "unit"), (tail, "m")]
    if phase == "running":
        if not stage:
            return [(f"{label} 시작 중…" if label else "시작 중…", "m")]
        name = STAGE_NAMES.get(stage, stage)
        if stage in TAIL_STEP and done >= total - 1:
            return [(name, "m"), (" · " + (note or TAIL_STEP[stage]), "body")]
        if stage in ("review", "report", "setup") or total <= 0:
            return [(f"{name} 만드는 중" if stage in ("review", "report") else f"{name} 중", "body")]
        t = total - (1 if stage in TAIL_STEP else 0)
        out = [(name, "m"), (f"{min(done, t):,}", "big"), (f"/ {t:,}{UNITS.get(stage, '쪽')}", "unit")]
        if note:
            out.append((" · " + note, "m"))
        now = now_text(now_pages) if stage == "read" else ""
        if now:
            k = len(set(now_pages))
            out.append((f" · 지금 {now}쪽 판독 중" + (f" (동시 {k}쪽)" if k > 1 else ""), "m"))
        return out
    # 끝난 뒤(§7.3)
    res = result or {}
    kind, rc = res.get("kind"), res.get("rc")
    key = "read" if "read" in seen else (stage or (next(iter(seen)) if seen else ""))
    if not key:
        word = {"ok": " 끝", "partial": " 끝", "stopped": " 중지됨"}.get(kind, " 오류로 멈춤")
        return [(f"{label}{word}", "body" if kind != "error" else "red_b")]
    d, t = seen.get(key, (done, total))
    if key in TAIL_STEP:
        t -= 1
    name = STAGE_NAMES.get(key, key)
    unit = UNITS.get(key, "쪽")
    nfail = int(res.get("failed_n") or 0) if rc == 3 and res.get("failed") else 0
    if kind in ("ok", "partial"):
        d = t - nfail if key == "read" else t
    out = [(name, "m"), (f"{max(0, min(d, t)):,}", "big"), (f"/ {t:,}{unit}", "unit")]
    if nfail:
        pages = res.get("failed_pages") or []
        which = f"({', '.join(map(str, pages[:5]))}{'…' if len(pages) > 5 else ''}쪽)" if pages else ""
        out += [(f" · 실패 {nfail:,}쪽{which}", "red_b"), (" — 다시 누르면 그 쪽만 다시 판독", "m")]
    elif kind in ("ok", "partial"):
        out.append((" · 끝", "body"))
    elif kind == "stopped":
        out.append((" · 중지됨", "body"))
    else:
        out.append((" · 오류로 멈춤", "red_b"))
    return out


# ---------- 이번 실행의 호출·어림(§5.6·§5.7·§9) ----------
READ_STAGES = ("prepare", "read")                     # 장부의 '판독' 묶음
MODEL_STAGES = ("adjudicate", "build")                # 장부의 '모델 단계' 묶음(3차 판별 · 문서 만들기의 문맥 교정)
RUN_STEPS = {"all": ("read1", "read2", "glyph", "suspect", "choice"), "without": ("read1", "read2")}   # [예]·[아니요]가 이번 실행에서 하는 어림 줄


def calls_split(by_stage: dict | None, tail: tuple[str, int] | None = None) -> dict:
    """단계별 실제 호출(_calls_by_stage — 진행 알림이 가져온 그때의 CALL_CAP.used 로 나눈 몫)을 장부 묶음으로: read(준비·판독) ·
    model(3차 판별 · 문서 만들기의 문맥 교정) · other(그 밖 — 구획 대조 등). tail 은 다음 알림까지 지금 단계 몫으로 잠시 보이는 (단계, 수)."""
    out = {"read": 0, "model": 0, "other": 0}
    items = list((by_stage or {}).items()) + ([tail] if tail and tail[1] else [])
    for stage, n in items:
        out["read" if stage in READ_STAGES else "model" if stage in MODEL_STAGES else "other"] += int(n or 0)
    return out


def part_totals(rows: list | None, steps) -> tuple[list[int], list[float] | None]:
    """어림 줄(step·calls·usd) 가운데 steps 의 호출·달러 합 — 단가가 없어 금액을 낼 수 없는 줄(호출이 있는데 usd 없음)이 있으면 달러 None."""
    mine = [r for r in rows or [] if r.get("step") in steps]
    calls = [sum((r.get("calls") or [0, 0])[0] for r in mine), sum((r.get("calls") or [0, 0])[1] for r in mine)]
    if any(r.get("usd") is None and (r.get("calls") or [0, 0])[1] for r in mine):
        return calls, None
    return calls, [sum((r.get("usd") or [0, 0])[0] for r in mine), sum((r.get("usd") or [0, 0])[1] for r in mine)]


def run_totals(led: dict | None, without: bool) -> dict | None:
    """장부 머리 금액·호출 막대가 말할 '이번 실행'의 어림: [예]면 계획의 합계 그대로, [아니요 · 모델 단계 없이]로 시작한 실행이면 판독 줄만
    (합계(이번 실행)와 같은 셈 — 원화는 어림의 환율로). → {calls, usd(None = 단가 없음), krw, missing} 또는 어림이 없으면 None."""
    if not led:
        return None
    t = led.get("totals") or {}
    if not without:
        return {"calls": list(t.get("calls") or [0, 0]), "usd": t.get("usd"), "krw": led.get("krw"),
                "known": t.get("usd_known") or [0.0, 0.0], "missing": list(t.get("missing_prices") or [])}
    steps = RUN_STEPS["without"]
    calls, usd = part_totals(led.get("rows"), steps)
    _, known = part_totals([r for r in led.get("rows") or [] if r.get("usd") is not None], steps)
    fx = led.get("fx") or 0
    return {"calls": calls, "usd": usd, "krw": [v * fx for v in usd] if usd is not None and fx else None,
            "known": known or [0.0, 0.0], "missing": [] if usd is not None else list(t.get("missing_prices") or [])}


# ---------- 쪽 띠(§5.4) ----------
def merge_marks(disk: dict, live: dict | None, *, finished: bool, before: dict | None = None) -> dict:
    """작업 스레드가 센 칸(state.page_marks — 기록·상태 파일로 본 모양)에 이번 실행의 쪽 알림(cli.PAGE_EVENT)을 겹친다 — 알림이 온 쪽은
    알림이 맞다(다시 판독은 기록이 있어도 '남음'으로 세므로 파일로는 이번에 끝난 쪽을 가릴 수 없다). 실행이 끝났으면(중지·오류 포함)
    남은 '지금'은 settle_marks 처럼 되돌린다."""
    out = dict(disk)
    for n, st in (live or {}).items():
        if n in out:
            out[n] = _unfinished(n, before) if finished and st == "now" else st
    return out


def _unfinished(n: int, before: dict | None) -> str:
    return "fail" if (before or {}).get(n) == "fail" else "remain"


def settle_marks(marks: dict, before: dict | None = None) -> dict:
    """실행이 끝난 칸: '지금'이 남았으면(쪽이 끝나기 전에 멈춤·오류) '남음' — 다시 누르면 그 쪽부터 읽는다. 실행 전에 실패였던 쪽(before)을
    다시 읽다 멈췄으면 그대로 실패(상태 파일의 '판독 확인 필요'도 그대로라 다음 실행 전 칸과 같게)."""
    return {n: (_unfinished(n, before) if st == "now" else st) for n, st in marks.items()}

def strip_layout(n: int, width: float, gap: float, min_cell: float) -> tuple[int, list[tuple[float, float, int, int]]]:
    """대상 n쪽을 너비 width 에 칸으로 나눈다: 칸 최소 너비 min_cell, 칸 사이 gap. → (한 칸의 쪽 수, [(x0, x1, 첫 쪽 번호(0부터), 끝 쪽 번호+1)])."""
    if n <= 0 or width <= 0:
        return 1, []
    max_cells = max(1, int((width + gap) // (min_cell + gap)))
    per = math.ceil(n / max_cells)
    cells = math.ceil(n / per)
    cw = (width - gap * (cells - 1)) / cells
    out = []
    for c in range(cells):
        x0 = c * (cw + gap)
        out.append((x0, x0 + cw, c * per, min(n, (c + 1) * per)))
    return per, out


def cell_kind(states: list[str]) -> tuple[str, float]:
    """한 칸(묶음이면 여러 쪽)의 모양과 먹 채움 몫: 실패가 있으면 fail, 지금 읽는 쪽이 있으면 now(끝난 몫만큼 채움),
    모두 끝났으면 done, 기록만이면 record, 그 밖에는 remain(끝남 + 기록 몫만큼 채움)."""
    if not states:
        return "remain", 0.0
    if "fail" in states:
        return "fail", 0.0
    n = len(states)
    if "now" in states:
        return "now", states.count("done") / n
    if all(s == "done" for s in states):
        return "done", 1.0
    if all(s == "record" for s in states):
        return "record", 0.0
    return "remain", (states.count("done") + states.count("record")) / n if n > 1 else 0.0


def strip_ticks(pages: list[int], per: int, cells: list[tuple[float, float, int, int]], min_gap: float = 40.0) -> list[tuple[float, str]]:
    """눈금: 첫 쪽·끝 쪽과 그 사이 둥근 수(10·20·50·100 …)마다 — 눈금 사이가 min_gap 이상이 되는 가장 작은 둥근 수. → [(x, 글)].
    한 칸이 여러 쪽이면 눈금은 칸 가운데가 아니라 그 칸 안에서 그 쪽의 자리에(칸 가운데에 두면 20쪽마다의 눈금이 칸 2개·3개 건너로 고르지 않아 붙어 보임)."""
    if not cells or not pages:
        return []
    pitch = (cells[-1][1] - cells[0][0]) / max(1, len(cells)) if len(cells) > 1 else cells[0][1] - cells[0][0]
    step = 10
    while step / per * pitch < min_gap and step < 10 ** 7:
        step = step * 2 if str(step)[0] == "1" else (step * 5 // 2 if str(step)[0] == "2" else step * 2)

    def at(k):                                                          # k 번째 쪽(0부터)의 x — 칸 안에서 그 쪽 몫의 가운데
        x0, x1, a, b = cells[min(len(cells) - 1, k // per)]
        return x0 + (x1 - x0) * (k - a + 0.5) / max(1, b - a)
    ticks = [(at(0), str(pages[0]))]
    for k, p in enumerate(pages):
        if p % step == 0 and k > 0 and k < len(pages) - 1:
            ticks.append((at(k), str(p)))
    if len(pages) > 1:
        ticks.append((at(len(pages) - 1), str(pages[-1])))
    return ticks
