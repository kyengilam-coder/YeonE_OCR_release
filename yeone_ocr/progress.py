"""작업 진행률. 명령(cli)이 단계마다 알리는 (단계, 끝난 수, 전체 수)를 창 프로그램의 진행 막대 하나(0~1)로 모은다.

막대는 처음부터 끝까지 한 방향으로만 찬다(뒤로 가지 않음). 명령마다 단계와 무게(걸리는 시간 어림)를 정해 두고,
지금 단계의 진행분을 그 무게만큼 더한다. 단계가 건너뛰어지면(예: 판별할 자리가 없음) 다음 단계의 알림이 오는 순간 앞 단계는 끝난 것으로 본다.
tkinter 없이 검사할 수 있게 창 프로그램과 따로 둔다.
"""
from __future__ import annotations
import time

STAGE_NAMES = {"prepare": "준비", "read": "판독", "adjudicate": "3차 판별", "build": "문서 만들기", "review": "검수 화면",
               "report": "보고", "collate": "구획 대조", "remerge": "다시 계산",
               "resolve": "패키지 확인", "download": "내려받기", "setup": "설치"}
UNITS = {"collate": "구획", "resolve": "개", "download": "개"}            # 나머지는 '쪽'
# 남은 시간을 어림하지 않는 명령: pip 은 큰 파일(opencv·pymupdf 등)을 먼저 내려받아 개수로 어림하면 크게 빗나간다.
NO_ETA = {"install"}
# 쪽 수 뒤에 한 걸음이 더 붙는 단계: 문서 만들기는 쪽마다 도판을 자른 뒤 문서를 한 번 조립한다(전체 = 쪽 수 + 1).
TAIL_STEP = {"build": "문서 조립"}

# 명령별 단계와 무게. 한 번에 인식(run)은 판독이 대부분이다.
PLANS: dict[str, tuple[tuple[str, float], ...]] = {
    "run": (("prepare", 0.03), ("read", 0.75), ("adjudicate", 0.12), ("build", 0.07), ("review", 0.015), ("report", 0.015)),
    "read": (("read", 1.0),), "prepare": (("prepare", 1.0),), "adjudicate": (("adjudicate", 1.0),), "build": (("build", 1.0),),
    "remerge": (("remerge", 1.0),), "review": (("review", 1.0),), "report": (("report", 1.0),),
    "collate": (("collate", 1.0),),
    "install": (("resolve", 0.3), ("download", 0.55), ("setup", 0.15)),   # pip: 확인 → 내려받기 → 설치
}
# 한 단위(구획) 안에서 일어나는 하위 단계: 구획마다 쪽을 준비·판독하므로 그 진행을 구획 한 칸 안의 몫으로 본다.
NESTED: dict[str, dict[str, tuple[float, float]]] = {"collate": {"prepare": (0.0, 0.1), "read": (0.1, 0.95)}}


def fmt_duration(sec: float) -> str:
    if sec < 60:
        return "1분 안"
    m = int(round(sec / 60))
    if m < 60:
        return f"{m}분"
    return f"{m // 60}시간 {m % 60}분" if m % 60 else f"{m // 60}시간"


def fmt_elapsed(sec: float) -> str:
    """지난 시간(초 단위까지): 진행률을 알 수 없는 작업이 아직 돌고 있음을 보여 줄 때."""
    s = max(0, int(sec))
    if s < 60:
        return f"{s}초"
    if s < 3600:
        return f"{s // 60}분 {s % 60:02d}초"
    return f"{s // 3600}시간 {s % 3600 // 60:02d}분"


class Tracker:
    """한 작업의 진행률. update() 로 알림을 넣고 frac(0~1)·text() 로 읽는다."""

    def __init__(self, command: str = "", now: float | None = None):
        self.command = command
        self.plan = PLANS.get(command, ())
        self.nested = NESTED.get(command, {})
        self.frac = 0.0
        self.stage = ""; self.done = 0; self.total = 0; self.sub = 0.0
        self._idx = -1
        self.t0 = time.time() if now is None else now
        # 남은 시간은 가장 무거운 단계(한 번에 인식이면 판독)가 시작된 때부터의 속도로 어림한다.
        # 빠른 준비 단계의 속도로 어림하면 첫 쪽이 끝날 때까지 남은 시간이 자꾸 늘어 보이므로.
        self._main = max(range(len(self.plan)), key=lambda k: self.plan[k][1]) if self.plan else -1
        self._t_main = self.t0 if self._main == 0 else None
        self._f_main = 0.0

    def update(self, stage: str, done: int, total: int, now: float | None = None) -> float:
        names = [s for s, _ in self.plan]
        before_frac = self.frac
        if stage in names:
            i = names.index(stage)
            if i < self._idx:
                return self.frac                                  # 지난 단계의 늦은 알림
            if i != self._idx or done != self.done:
                self.sub = 0.0                                    # 새 단위가 시작되면 하위 진행은 처음부터
            self._idx = i; self.stage = stage
            self.done, self.total = max(0, int(done)), max(0, int(total))
        elif stage in self.nested and self._idx >= 0:
            a, b = self.nested[stage]
            f = (done / total) if total else 1.0
            self.sub = a + (b - a) * min(1.0, max(0.0, f))
        else:
            return self.frac
        before = sum(w for _, w in self.plan[:self._idx])
        w = self.plan[self._idx][1]
        if self.total <= 0:
            f = 1.0
        else:
            f = min(1.0, (self.done + (self.sub if self.done < self.total else 0.0)) / self.total)
        self.frac = max(self.frac, min(1.0, before + w * f))
        if self._t_main is None and self._main >= 0 and self._idx >= self._main:
            self._t_main = time.time() if now is None else now
            self._f_main = before_frac if (self._idx == self._main and self.total > 0) else self.frac
        return self.frac

    def finish(self) -> None:
        self.frac = 1.0

    def eta(self, now: float | None = None) -> str:
        now = time.time() if now is None else now
        if self.command in NO_ETA or self.frac < 0.03 or now - self.t0 < 15 or self.frac >= 1.0 or self._t_main is None:
            return ""
        el, gained = now - self._t_main, self.frac - self._f_main
        if el <= 0 or gained <= 0.005:
            return ""                                             # 무거운 단계가 아직 한 칸도 끝나지 않음 — 어림하지 않는다
        return "남은 시간 약 " + fmt_duration(el * (1 - self.frac) / gained)

    def live_text(self, now: float | None = None) -> str:
        """작업 중 진행 글: 남은 시간을 어림할 수 없는 동안(첫 쪽이 끝나기 전 등)에는 지난 시간을 붙여 멈춘 것이 아님을 보인다."""
        now = time.time() if now is None else now
        if not self.plan:
            return "진행 중 · 지난 시간 " + fmt_elapsed(now - self.t0)
        t = self.text(now)
        return t if self.eta(now) else t + " · 지난 시간 " + fmt_elapsed(now - self.t0)

    def text(self, now: float | None = None) -> str:
        parts = [f"{self.frac * 100:.0f}%"]
        if self.stage:
            name = STAGE_NAMES.get(self.stage, self.stage)
            total = self.total - (1 if self.stage in TAIL_STEP else 0)
            if self.stage in TAIL_STEP and self.done >= total:
                parts.append(f"{name} · {TAIL_STEP[self.stage]}")
            elif total > 1 or (total == 1 and self.stage not in ("review", "report", "setup")):
                parts.append(f"{name} {min(self.done, total)}/{total}{UNITS.get(self.stage, '쪽')}")
            else:
                parts.append(name)
        e = self.eta(now)
        if e:
            parts.append(e)
        return " · ".join(parts)
