"""Which signed-in AI plan reads and reviews: Claude (Claude Code), ChatGPT (Codex CLI) or Gemini (Gemini CLI).

Every plan follows the same rule: this program only starts the provider's own, unmodified command-line
program, which the person signed in to with their own account; it never reads, stores or relays a
token, and usage counts against that person's plan limits (no per-call API charge). Personal use only.

Plan choice: --local-ai-plan or YEONE_OCR_AI_PLAN = auto (default) | claude | chatgpt | gemini.
auto takes the first plan that is installed and signed in, in the order Claude, ChatGPT, Gemini
(Claude first: the 2026-11 defaults were measured with it). Only a review left on auto without an
API key looks at that provider's own plan first.
"""
from __future__ import annotations

import importlib
import os
import subprocess

PLANS = ("claude", "chatgpt", "gemini")
PLAN_CHOICES = ("auto",) + PLANS
NAMES = {"claude": "Claude", "chatgpt": "ChatGPT", "gemini": "Gemini"}
PROGRAMS = {"claude": "Claude Code", "chatgpt": "Codex CLI", "gemini": "Gemini CLI"}
PROVIDERS = {"claude": "anthropic", "chatgpt": "openai", "gemini": "gemini"}
_MODULES = {"claude": "claude_subscription", "chatgpt": "chatgpt_subscription", "gemini": "gemini_subscription"}
KEYS = {"claude": "claude-subscription", "chatgpt": "chatgpt-subscription", "gemini": "gemini-subscription"}


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


def pick(requested: str | None = None, prefer: str | None = None, runner=subprocess.run):
    """(plan or None, reason). A named plan: that plan if usable. auto: `prefer` (a provider or a plan)
    first, then Claude, ChatGPT, Gemini. The reason explains why nothing was usable."""
    choice = plan_choice(requested)
    if choice != "auto":
        state = status(choice, runner=runner)
        return (choice, "") if state["usable"] else (None, f"{NAMES[choice]} 구독: {state.get('reason') or ''}")
    first = prefer if prefer in PLANS else plan_for_provider(prefer)
    order = ([first] if first else []) + [plan for plan in PLANS if plan != first]
    reasons = []
    for plan in order:
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
