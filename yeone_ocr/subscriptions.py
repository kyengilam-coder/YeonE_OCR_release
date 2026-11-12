"""Which signed-in AI plan reads and reviews: Claude (Claude Code), ChatGPT (Codex CLI) or Gemini (Gemini CLI).

Every plan follows the same rule: this program only starts the provider's own, unmodified command-line
program, which the person signed in to with their own account; it never reads, stores or relays a
token, and usage counts against that person's plan limits (no per-call API charge). Personal use only.

Plan choice: --local-ai-plan or YEONE_OCR_AI_PLAN = auto (default) | claude | chatgpt | gemini.
auto takes the first plan that is installed and signed in, in the order Claude, ChatGPT, Gemini
(Claude first: the 2026-11 defaults were measured with it); a named plan is tried first, then the
others. Only a review left on auto without an API key looks at that provider's own plan first.

During a run (Session): when a plan stops working — usage limit reached, sign-in expired, CLI
missing — the same request goes to the next signed-in plan; when none is left, to the API key
(comparison_ai.ApiTransport, within the run's API call cap); without a key, AI work stops and the
review targets stay saved. A plan at its limit rests until the limit resets (the time its program
gives, else LIMIT_REST_SECONDS) and is used again after that; a temporary failure (server trouble,
network) is tried again on the same plan first, and then rests the plan for a short while only.
"""
from __future__ import annotations

import importlib
import os
import signal
import subprocess
import sys
import threading
import time

PLANS = ("claude", "chatgpt", "gemini")
PLAN_CHOICES = ("auto",) + PLANS
NAMES = {"claude": "Claude", "chatgpt": "ChatGPT", "gemini": "Gemini"}
PROGRAMS = {"claude": "Claude Code", "chatgpt": "Codex CLI", "gemini": "Gemini CLI"}
PROVIDERS = {"claude": "anthropic", "chatgpt": "openai", "gemini": "gemini"}
_MODULES = {"claude": "claude_subscription", "chatgpt": "chatgpt_subscription", "gemini": "gemini_subscription"}
KEYS = {"claude": "claude-subscription", "chatgpt": "chatgpt-subscription", "gemini": "gemini-subscription"}
# A completed answer's finish reason as each provider writes it; review records are checked against these.
FINISHED = {"anthropic": "end_turn", "openai": "completed", "gemini": "STOP"}


def module(plan: str):
    return importlib.import_module(f".{_MODULES[plan]}", __package__)


def plan_choice(requested: str | None = None) -> str:
    value = (requested or os.environ.get("YEONE_OCR_AI_PLAN", "") or "auto").strip().lower()
    return value if value in PLAN_CHOICES else "auto"


def plan_for_provider(provider: str | None) -> str | None:
    return next((plan for plan, name in PROVIDERS.items() if name == provider), None)


def plan_for_key(key) -> str | None:
    return next((plan for plan, value in KEYS.items() if value == key), None)


def is_key(key) -> bool:
    return plan_for_key(key) is not None


def status(plan: str, refresh: bool = False, runner=subprocess.run) -> dict:
    return module(plan).status(refresh=refresh, runner=runner)


def reset_status_cache() -> None:
    for plan in PLANS:
        module(plan).reset_status_cache()


# Plans out of use in this run: until a time (a limit that resets, a short rest after a temporary
# failure) or for the whole run (sign-in, missing program, the user's own switch).
RETRY_WAITS = (3.0, 10.0)        # a temporary failure: the same plan again, after these waits
REST_SECONDS = 600.0             # still failing: the plan rests this long (twice as long each further time)
LIMIT_REST_SECONDS = 1800.0      # a limit whose program does not say when it resets: looked at again after this
MAX_REST_SECONDS = 6 * 3600.0
_exhausted: dict[str, dict] = {}
_rests: dict[str, int] = {}      # how many times a plan has rested in this run
_exhausted_lock = threading.Lock()
_clock = time.monotonic


def mark_exhausted(plan: str, reason: str, seconds: float | None = None) -> None:
    """Take a plan out of use: for `seconds`, or (None) for the rest of the run."""
    with _exhausted_lock:
        _exhausted[plan] = {"reason": reason, "until": None if seconds is None else _clock() + seconds}


def _current() -> dict:
    now = _clock()
    for plan in [plan for plan, item in _exhausted.items() if item["until"] is not None and item["until"] <= now]:
        del _exhausted[plan]
    return _exhausted


def exhausted() -> dict:
    """{plan: reason} of the plans out of use now (a rest that is over no longer counts)."""
    with _exhausted_lock:
        return {plan: item["reason"] for plan, item in _current().items()}


def resting() -> list:
    """For the window: [{"plan", "reason", "minutes" (None: for the rest of the run)}]."""
    with _exhausted_lock:
        now = _clock()
        return [{"plan": plan, "reason": item["reason"],
                 "minutes": None if item["until"] is None else max(1, round((item["until"] - now) / 60))}
                for plan, item in _current().items()]


def release(plan: str) -> None:
    with _exhausted_lock:
        _exhausted.pop(plan, None)


def reset_exhausted() -> None:
    with _exhausted_lock:
        _exhausted.clear()
        _rests.clear()


def _rest_seconds(plan: str, kind: str, retry_after) -> float | None:
    """How long a failed plan stays out: None (account problems) = the rest of the run."""
    if kind not in ("limit", "temporary"):
        return None
    with _exhausted_lock:
        count = _rests[plan] = _rests.get(plan, 0) + 1
    if kind == "limit" and retry_after:
        return float(retry_after)
    base = LIMIT_REST_SECONDS if kind == "limit" else REST_SECONDS
    return min(base * 2 ** (count - 1), MAX_REST_SECONDS)


def _minutes(seconds: float) -> str:
    minutes = max(1, round(seconds / 60))
    return f"{minutes // 60}시간 {minutes % 60}분" if minutes >= 60 else f"{minutes}분"


def order(requested: str | None = None, prefer: str | None = None) -> list:
    """Plans in the order they are tried: the named plan (or `prefer`, a provider or a plan), then
    Claude, ChatGPT, Gemini."""
    choice = plan_choice(requested)
    first = choice if choice != "auto" else (prefer if prefer in PLANS else plan_for_provider(prefer))
    return ([first] if first else []) + [plan for plan in PLANS if plan != first]


def pick(requested: str | None = None, prefer: str | None = None, runner=subprocess.run):
    """(plan or None, reason): the first plan in order() that is installed, signed in and has not failed
    in this run. A named plan is the first choice, not the only one. The reason explains why none was usable."""
    reasons = []
    failed = exhausted()
    for plan in order(requested, prefer):
        if plan in failed:
            reasons.append(f"{NAMES[plan]}: 이번 실행에서 쓸 수 없게 됨({failed[plan]})")
            continue
        state = status(plan, runner=runner)
        if state["usable"]:
            return plan, ""
        if state.get("installed"):
            reasons.append(f"{NAMES[plan]}: {state.get('reason') or ''}")
    if not reasons:
        reasons.append("Claude Code, Codex CLI, Gemini CLI 중 설치된 것이 없습니다.")
    return None, " / ".join(reasons)


def note(plan: str) -> str:
    state = status(plan)
    return (f"AI 검증: {NAMES[plan]} 구독({state.get('plan') or '요금제'}) · {PROGRAMS[plan]} 실행 · "
            "API 요금 없음(구독 사용량에서 차감)")


def choose(requested_mode: str | None, provider: str, api_key: str, plan: str | None = None, runner=subprocess.run):
    """(plan or None, message). subscription: a usable plan; api: never; auto: only without an API key
    for the provider, and then that provider's own plan first."""
    from .claude_subscription import auth_mode
    mode = auth_mode(requested_mode)
    if mode == "api" or (mode == "auto" and api_key):
        return None, ""
    # auto without a key: that provider's own plan first; subscription: the fixed order (Claude first).
    chosen, reason = pick(plan, prefer=provider if mode == "auto" else None, runner=runner)
    if chosen:
        return chosen, note(chosen)
    return None, ("구독 계정으로 검증할 수 없습니다: " + reason) if mode == "subscription" else ""


def model(plan: str, requested: str | None = None) -> str:
    """The model name to record and pass on; the CLI's own default has a label (codex-default, gemini-cli-auto)."""
    return module(plan).label(module(plan).model(requested))


def label(plan: str, model_name: str) -> str:
    return module(plan).label(model_name)


def accepts_model(plan: str, name: str | None) -> bool:
    """Whether an explicitly chosen model name belongs to this plan's provider."""
    name = (name or "").strip().lower()
    if not name:
        return False
    if plan == "claude":
        return name.startswith("claude") or name in ("opus", "sonnet", "haiku", "fable")
    if plan == "chatgpt":
        return name.startswith(("gpt", "codex", "o1", "o3", "o4"))
    return name.startswith("gemini")


def client(plan: str):
    return module(plan).SubscriptionClient()


class Session:
    """One run's AI connection, shared by page reading and review; the client both of them call.

    The chosen plan first; when it raises SubscriptionError (usage limit, sign-in expired, CLI
    missing) the request is sent again to the next usable plan, and when none is left to `api`
    (an ApiTransport, or None to stop). switch() does the same on the user's request (the GUI's
    [다음 구독으로 넘기기]): it stops the CLI that is answering now and the interrupted request goes
    to the chosen plan, or to the next one in order."""

    subscription = True

    def __init__(self, plan, requested=None, *, api=None, log=print, factory=None, record_provider=None,
                 should_stop=None, sleep=time.sleep):
        self.first = self.plan = plan
        self.should_stop = should_stop or (lambda: False)
        self._stay = False
        self._sleep = sleep
        # The provider the review records name (the run's plan settings); answers from another provider
        # report their finish reason in its terms. Split runs give every plan's session the run's provider.
        self.record_provider = record_provider or PROVIDERS[plan]
        self.requested = requested
        self.api = api
        self.log = log
        self._factory = factory or (lambda name: module(name).SubscriptionClient(runner=self._run))
        self._client = None
        self._lock = threading.RLock()
        self._procs = set()
        self.reason = ""
        self.switches = []

    @property
    def available(self) -> bool:
        if self.plan is None:
            self._come_back()                       # a plan whose rest is over makes the connection usable again
        return self.plan is not None or self._api_left()

    def _api_left(self) -> bool:
        return self.api is not None and self.api.left > 0

    @property
    def transport(self) -> str:
        if self.plan is not None:
            return module(self.plan).TRANSPORT
        return getattr(self.api, "transport", "none")

    @property
    def provider(self) -> str | None:
        return PROVIDERS[self.plan] if self.plan else getattr(self.api, "provider", None)

    def describe(self) -> str:
        if self.plan:
            return f"{NAMES[self.plan]} 구독"
        if self.api is not None:
            return f"API 키({self.api.provider} · {self.api.model}, 남은 호출 {self.api.left}회)"
        return "없음"

    def short(self) -> str:
        """For the window's one line: the plan, or the API key's provider and calls left."""
        if self.plan:
            return f"{NAMES[self.plan]} 구독"
        return f"API 키 · {self.api.provider} · 남은 {self.api.left}회" if self.api is not None else "없음"

    def stop_reason(self) -> str:
        """Why AI work stopped (available is False), in words a person can act on."""
        if self.api is None:
            return "쓸 수 있는 구독 계정도 API 키도 없음 — 구독 계정에 다시 로그인하거나 API 키를 넣으세요"
        return (f"구독 계정을 쓸 수 없어 넘어간 API 키의 이번 실행 호출 상한({self.api.calls}회)에 도달 — "
                "[다음 구독으로 넘기기]에서 구독 계정을 직접 고르면 이어서 검증하고, "
                "상한은 설정 「로컬 뒤 AI 검증」의 횟수로 늘립니다")

    def state(self) -> dict:
        """For the GUI: what is connected now and where [다음 구독으로 넘기기] can go."""
        with self._lock:
            targets = [plan for plan in PLANS if plan != self.plan]
            if self.plan is not None and self._api_left():
                targets.append("api")
            return {"plan": self.plan, "label": self.short(), "detail": self.describe(), "available": self.available,
                    "targets": targets, "next": self._next_auto()}

    def _next_auto(self):
        """Where an automatic switch would go now: a plan, "api" or None (no status calls: cached)."""
        failed = exhausted()
        for plan in order(self.requested):
            if plan != self.plan and plan not in failed and status(plan)["usable"]:
                return plan
        return "api" if self.plan is not None and self._api_left() else None

    # ---- requests ----
    def request(self, *, model_name, system, prompt, images, schema):
        from .claude_subscription import RequestRejected, SubscriptionError
        attempts, rested = 0, []                 # this request: tries on the plan in use, plans it sent to rest
        while True:
            self._come_back()
            with self._lock:
                plan, current = self.plan, self._client
                if plan is not None and current is None:
                    try:
                        current = self._client = self._factory(plan)
                    except SubscriptionError as exc:
                        self._fail(plan, exc)
                        continue
            if plan is None:
                if self.api is None:
                    raise SubscriptionError(self.reason or "쓸 수 있는 구독 계정도 API 키도 없습니다.")
                try:
                    return self._as_first(self.api.request(model_name=model_name, system=system, prompt=prompt,
                                                           images=images, schema=schema), None)
                finally:
                    _notify(self)                         # the window shows the calls left
            name = model_name if plan == self.first and accepts_model(plan, model_name) else model(plan)
            try:
                return self._as_first(current.request(model_name=name, system=system, prompt=prompt,
                                                      images=images, schema=schema), plan)
            except Exception as exc:
                wait = None
                with self._lock:
                    if self.plan != plan or self._client is not current:
                        continue                      # switched while it ran (by the user or another caller): send again
                    if not isinstance(exc, SubscriptionError) or isinstance(exc, RequestRejected):
                        raise                         # this request's own problem: another plan would refuse it too
                    if getattr(exc, "kind", "account") == "temporary":
                        if self.should_stop():
                            raise
                        if attempts < len(RETRY_WAITS):
                            wait = RETRY_WAITS[attempts]
                            attempts += 1
                        elif rested:
                            # The same request failed on a second plan as well: the request, not the plans.
                            for other in rested:
                                release(other)
                            raise
                        else:
                            rested.append(plan)
                            attempts = 0
                    if wait is None:
                        self._fail(plan, exc)
                if wait is not None:
                    self.log(f"  {NAMES[plan]} 구독: 일시 오류로 {wait:.0f}초 뒤 다시 보냅니다({attempts}/{len(RETRY_WAITS)}) — {str(exc)[:200]}")
                    self._pause(wait)

    def _pause(self, seconds):
        end = _clock() + seconds
        while not self.should_stop():
            left = end - _clock()
            if left <= 0:
                break
            self._sleep(min(0.5, left))

    def _come_back(self):
        """Back to the plan this connection started with once its rest is over (a limit that reset, a
        temporary failure), or from the API key to any plan that can be used again."""
        with self._lock:
            if self.plan == self.first or self._stay:
                return
            failed = exhausted()
            target = None
            if self.first not in failed and status(self.first)["usable"]:
                target = self.first
            elif self.plan is None:
                target = next((plan for plan in order(self.requested) if plan not in failed and status(plan)["usable"]), None)
            if target is None or target == self.plan:
                return
            before = self.describe()
            self.plan, self._client, self.reason = target, None, ""
            self.switches.append({"from": before, "to": target, "reason": "쉬는 시간이 지남", "by": "rest"})
            self.log(f"  {NAMES[target]} 구독을 다시 씁니다(쉬는 시간이 지남) — {before}에서 돌아옵니다.")
        _notify(self)

    def _as_first(self, result, plan):
        """A completed answer from another provider is recorded under the first plan's provider
        (the review record and its checks name that one); the actual model and transport stay in
        the metadata. `plan` is the one that answered (None: the API key) — the connection may have
        moved on while this request was out, when another request of it failed."""
        text, complete, finish, tokens, actual = result
        answered = PROVIDERS[plan] if plan is not None else getattr(self.api, "provider", None)
        if complete and answered != self.record_provider:
            finish = FINISHED[self.record_provider]
        return text, complete, finish, tokens, actual

    def _fail(self, plan, exc):
        kind = getattr(exc, "kind", "account")
        seconds = _rest_seconds(plan, kind, getattr(exc, "retry_after", None))
        mark_exhausted(plan, str(exc), seconds)
        again = (f" {NAMES[plan]} 구독은 약 {_minutes(seconds)} 뒤 다시 씁니다." if seconds is not None
                 else f" {NAMES[plan]} 구독은 이번 실행에서 다시 쓰지 않습니다.")
        self._client = None
        following, _ = pick(self.requested)
        self.switches.append({"from": plan, "to": following or ("api" if self.api is not None else None),
                              "reason": str(exc), "by": kind})
        if following:
            self.plan = following
            self.log(f"  {NAMES[plan]} 구독을 더 쓸 수 없습니다 — {exc} → {NAMES[following]} 구독으로 넘어갑니다.{again}")
        else:
            self.plan = None
            if self.api is not None:
                self.log(f"  {NAMES[plan]} 구독을 더 쓸 수 없고({exc}) 쓸 수 있는 다른 구독 계정이 없어 "
                         f"API 키({self.api.provider} · 모델 {self.api.model})로 넘어갑니다. 이번 실행 API 호출 최대 {self.api.left}회.{again}")
            else:
                self.reason = f"{NAMES[plan]} 구독을 더 쓸 수 없고 다른 구독 계정도 API 키도 없습니다: {exc}"
                self.log(f"  {self.reason} — AI 판독·검증을 멈추고 검증 대상은 저장합니다.{again}")
        _notify(self)

    # ---- the user's [다음 구독으로 넘기기] ----
    def switch(self, target=None) -> str:
        """Stop the plan in use and connect to `target` (a plan or "api"); None, or a target that
        cannot be used, takes the next one in order. Returns what happened, for the window."""
        with self._lock:
            old = self.plan
            if old is not None:
                mark_exhausted(old, "사용자가 넘김")
            note = ""
            chosen = None
            if target in PLANS and target != old:
                state = status(target, refresh=True)
                if state["usable"]:
                    release(target)                        # chosen by hand: try it again even if it failed earlier
                    chosen = target
                else:
                    note = f"{NAMES[target]} 구독은 쓸 수 없어({state.get('reason') or '로그인 필요'}) 순서대로 고릅니다. "
            elif target == "api" and old is not None:
                if self._api_left():
                    chosen = "api"
                else:
                    note = "API 키가 없거나 이번 실행 호출 상한에 닿아 순서대로 고릅니다. "
            if chosen is None:
                chosen = pick(self.requested)[0] or ("api" if old is not None and self._api_left() else None)
            if chosen is None and old is None:
                return note + "넘길 구독 계정이 없습니다. 지금 연결을 그대로 씁니다."
            if chosen is None and old is not None:
                release(old)                                # nothing to go to: keep the plan in use
                return note + f"넘길 구독 계정도 API 키도 없어 {NAMES[old]} 구독을 그대로 씁니다."
            self.plan = None if chosen == "api" else chosen
            self._stay = True                               # the person's choice: no coming back on its own
            self._client = None
            self._stop_running()
            self.switches.append({"from": old or "api", "to": chosen, "reason": "사용자가 넘김", "by": "user"})
            message = note + f"{NAMES[old] + ' 구독을' if old else 'API 키를'} 멈추고 {self.describe()}에 연결합니다."
            self.log("  [다음 구독으로 넘기기] " + message)
        _notify(self)
        return message

    def _run(self, args, *, input=None, timeout=None, capture_output=True, **kwargs):
        """subprocess.run that switch() can stop: the process (and its children) is killed and the
        request is sent again to the new connection."""
        popen = dict(kwargs)
        if sys.platform != "win32":
            popen["start_new_session"] = True              # its own process group: killpg stops the CLI's children too
        proc = subprocess.Popen(args, stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, **popen)
        with self._lock:
            self._procs.add(proc)
        try:
            out, err = proc.communicate(input, timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill_tree(proc)
            out, err = proc.communicate()
            raise subprocess.TimeoutExpired(args, timeout, out, err) from None
        finally:
            with self._lock:
                self._procs.discard(proc)
        return subprocess.CompletedProcess(args, proc.returncode, out, err)

    def _stop_running(self):
        for proc in list(self._procs):
            _kill_tree(proc)


def _kill_tree(proc):
    """Stop a CLI and what it started (npm's codex.cmd / gemini.cmd run node, which runs the program)."""
    if proc.poll() is not None:
        return
    try:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True, timeout=15, check=False,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except (OSError, subprocess.SubprocessError):
        pass
    if proc.poll() is None:
        try:
            proc.kill()
        except OSError:
            pass


# ---- the run's connections, for the window ----
_active: Session | None = None
_team: list = []          # the other plans' sessions of a split run
ON_CHANGE = None          # the GUI sets a callable(state dict or None) while a run is going


def activate(session: Session, team=()) -> None:
    global _active, _team
    _active, _team = session, [member for member in team if member is not session]
    _notify(session)


def deactivate() -> None:
    global _active, _team
    _active, _team = None, []
    if ON_CHANGE is not None:
        ON_CHANGE(None)


def active() -> Session | None:
    return _active


def state() -> dict | None:
    """The run's connections for the window: the first session's state, with every session's
    connection in the label and the plans that are resting."""
    session = _active
    if session is None:
        return None
    data = session.state()
    labels = []
    for member in [session, *_team]:
        if member.available and member.short() not in labels:
            labels.append(member.short())
    if labels:
        data["label"] = " · ".join(labels)
    data["resting"] = resting()
    return data


def switch(target=None) -> str:
    """[다음 구독으로 넘기기]: switch the running connection (see Session.switch); the other sessions
    of a split run that are on the same plan leave it too."""
    session = _active
    if session is None:
        return "지금 구독 계정으로 연결된 실행이 없습니다."
    old = session.plan
    message = session.switch(target)
    if old is not None and session.plan != old:
        for member in list(_team):
            if member.plan == old:
                member.switch(target)
    return message


def _notify(session) -> None:
    if ON_CHANGE is not None and (session is _active or session in _team):
        ON_CHANGE(state())


def probe(plan: str, client_factory=None) -> dict:
    """[연결 시험]: one small real request (a blank picture, a one-field answer) through the plan's own
    program, as a run sends it. Counts a little against the plan. {"ok", "seconds", "model", "error"}."""
    import base64
    from .claude_subscription import SubscriptionError
    picture = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAABAAAAAQCAIAAACQkWg2AAAAGUlEQVR4nGP8//8/AymAiSTVoxpGNQwpDQBVbQMdPVIhQwAAAABJRU5ErkJggg==")
    schema = {"type": "object", "additionalProperties": False, "required": ["ok"], "properties": {"ok": {"type": "boolean"}}}
    started = time.perf_counter()
    try:
        client = (client_factory or (lambda name: module(name).SubscriptionClient()))(plan)
        text, complete, finish, _tokens, actual = client.request(
            model_name=model(plan), system="This is a connection test. Answer with the requested JSON only.",
            prompt='Connection test. Answer exactly {"ok": true}.', images=[picture], schema=schema)
    except (SubscriptionError, TimeoutError, OSError) as exc:
        return {"ok": False, "seconds": time.perf_counter() - started, "model": "", "error": str(exc)}
    return {"ok": bool(complete), "seconds": time.perf_counter() - started, "model": actual,
            "error": "" if complete else f"답이 JSON이 아닙니다({finish}): {text[:120]}"}
