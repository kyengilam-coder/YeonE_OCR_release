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
review targets stay saved.
"""
from __future__ import annotations

import importlib
import os
import signal
import subprocess
import sys
import threading

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


# Plans that failed during this run (usage limit, sign-in expired, CLI missing): skipped until reset.
_exhausted: dict[str, str] = {}
_exhausted_lock = threading.Lock()


def mark_exhausted(plan: str, reason: str) -> None:
    with _exhausted_lock:
        _exhausted[plan] = reason


def exhausted() -> dict:
    with _exhausted_lock:
        return dict(_exhausted)


def reset_exhausted() -> None:
    with _exhausted_lock:
        _exhausted.clear()


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

    def __init__(self, plan, requested=None, *, api=None, log=print, factory=None):
        self.first = self.plan = plan
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

    def state(self) -> dict:
        """For the GUI: what is connected now and where [다음 구독으로 넘기기] can go."""
        with self._lock:
            targets = [plan for plan in PLANS if plan != self.plan]
            if self.plan is not None and self._api_left():
                targets.append("api")
            return {"plan": self.plan, "label": self.describe(), "available": self.available,
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
        from .claude_subscription import SubscriptionError
        while True:
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
                return self._as_first(self.api.request(model_name=model_name, system=system, prompt=prompt,
                                                       images=images, schema=schema))
            name = model_name if plan == self.first else model(plan)
            try:
                return self._as_first(current.request(model_name=name, system=system, prompt=prompt,
                                                      images=images, schema=schema))
            except Exception as exc:
                with self._lock:
                    if self.plan != plan or self._client is not current:
                        continue                      # switched while it ran (by the user or another caller): send again
                    if not isinstance(exc, SubscriptionError):
                        raise
                    self._fail(plan, exc)

    def _as_first(self, result):
        """A completed answer from another provider is recorded under the first plan's provider
        (the review record and its checks name that one); the actual model and transport stay in
        the metadata."""
        text, complete, finish, tokens, actual = result
        if complete and self.plan != self.first:
            finish = FINISHED[PROVIDERS[self.first]]
        return text, complete, finish, tokens, actual

    def _fail(self, plan, exc):
        mark_exhausted(plan, str(exc))
        self._client = None
        following, _ = pick(self.requested)
        self.switches.append({"from": plan, "to": following or ("api" if self.api is not None else None),
                              "reason": str(exc), "by": "limit"})
        if following:
            self.plan = following
            self.log(f"  {NAMES[plan]} 구독을 더 쓸 수 없습니다 — {exc} → {NAMES[following]} 구독으로 넘어갑니다.")
        else:
            self.plan = None
            if self.api is not None:
                self.log(f"  {NAMES[plan]} 구독을 더 쓸 수 없고({exc}) 쓸 수 있는 다른 구독 계정이 없어 "
                         f"API 키({self.api.provider} · 모델 {self.api.model})로 넘어갑니다. 이번 실행 API 호출 최대 {self.api.left}회.")
            else:
                self.reason = f"{NAMES[plan]} 구독을 더 쓸 수 없고 다른 구독 계정도 API 키도 없습니다: {exc}"
                self.log(f"  {self.reason} — AI 판독·검증을 멈추고 검증 대상은 저장합니다.")
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
                    with _exhausted_lock:
                        _exhausted.pop(target, None)       # chosen by hand: try it again even if it failed earlier
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
                with _exhausted_lock:
                    _exhausted.pop(old, None)               # nothing to go to: keep the plan in use
                return note + f"넘길 구독 계정도 API 키도 없어 {NAMES[old]} 구독을 그대로 씁니다."
            self.plan = None if chosen == "api" else chosen
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


# ---- the run's connection, for the window ----
_active: Session | None = None
ON_CHANGE = None          # the GUI sets a callable(state dict or None) while a run is going


def activate(session: Session) -> None:
    global _active
    _active = session
    _notify(session)


def deactivate() -> None:
    global _active
    _active = None
    if ON_CHANGE is not None:
        ON_CHANGE(None)


def active() -> Session | None:
    return _active


def switch(target=None) -> str:
    """[다음 구독으로 넘기기]: switch the running connection (see Session.switch)."""
    session = _active
    if session is None:
        return "지금 구독 계정으로 연결된 실행이 없습니다."
    return session.switch(target)


def _notify(session) -> None:
    if ON_CHANGE is not None and session is _active:
        ON_CHANGE(session.state())
