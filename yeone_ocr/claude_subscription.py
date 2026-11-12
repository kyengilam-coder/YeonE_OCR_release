"""Claude subscription (Pro/Max) transport: runs the user's own, unmodified Claude Code CLI (`claude -p`).

Same approach as Yeoneo Photo Editor (docs/07-claude-subscription.md there). Anthropic lets a person
sign in to the unmodified Claude Code binary with their own plan; it does not let a third-party app
collect or relay claude.ai credentials. So this module never reads a token: it starts `claude`, which
authenticates through Anthropic's own login, and usage counts against that person's plan limits.
Personal use only: an app handed to other people should use API keys instead.

The selective AI review (comparison_ai) and the AI page reading (ai_page) use it; subscriptions.py
chooses between this and the ChatGPT (Codex CLI) and Gemini (Gemini CLI) plans. The request is the same prompt, images and
JSON schema as the Anthropic API path; the transport is one `claude -p` process per call with every
tool, MCP server, user setting and session history switched off.

Environment:
  YEONE_OCR_CLAUDE_AUTH        auto (default) | subscription | api
  YEONE_OCR_CLAUDE_MODEL       model or alias for the CLI (default: opus)
  YEONE_OCR_CLAUDE_EFFORT      low | medium (default) | high | max
  YEONE_OCR_CLAUDE_CLI_PATH    claude executable (default: PATH, then ~/.local/bin)
  YEONE_OCR_CLAUDE_CONFIG_DIR  separate Claude Code sign-in for this app only
  YEONE_OCR_CLAUDE_CLI_TIMEOUT seconds per call (default 300)
"""
from __future__ import annotations

import base64
import datetime
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

# Not a key: run_plan / create_client take this in place of an API key to mean "the signed-in CLI".
SUBSCRIPTION_KEY = "claude-subscription"
PROVIDER = "anthropic"
TRANSPORT = "claude-code-subscription"
AUTH_MODES = ("auto", "subscription", "api")
DEFAULT_MODEL = "opus"
EFFORTS = ("low", "medium", "high", "max")
# In `-p` mode an API key in the environment wins over the signed-in plan: drop it for the child.
_STRIP_ENV = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")
SUBSCRIPTION_METHODS = {"claude.ai", "oauth_token"}
_STATUS_TTL = 60.0
_NO_WINDOW = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)} if sys.platform == "win32" else {}

LOGIN_HINT = ("PowerShell에서 `claude auth login`을 실행해 Claude 계정(Pro/Max)으로 로그인한 뒤 다시 실행하세요. "
              "로그인은 Anthropic 공식 화면에서 하며 이 프로그램은 토큰을 보지 않습니다.")
INSTALL_HINT = ("Claude Code가 설치되어 있지 않습니다. PowerShell에서 `irm https://claude.ai/install.ps1 | iex`로 "
                "설치한 뒤 `claude auth login`으로 로그인하세요.")
CMD_SHIM_HINT = ("npm으로 설치한 claude.cmd는 cmd.exe 명령줄 길이 제한(8191자) 때문에 검증 스키마를 넘길 수 없습니다. "
                 "공식 설치본(claude.exe)을 쓰세요: PowerShell에서 `irm https://claude.ai/install.ps1 | iex`")

_lock = threading.Lock()
_status_cache: tuple[float, dict] | None = None
_last_limits: dict | None = None


class SubscriptionError(RuntimeError):
    """A plan's own program could not answer. `kind` says what a run does next (subscriptions.Session):
    "limit" (usage limit: the plan rests until `retry_after` seconds have passed, when the program said
    when it resets), "account" (sign-in, missing program, a model the plan lacks: out for this run) or
    "temporary" (server trouble, network, anything not recognised: tried again, then a short rest)."""
    kind = "account"
    retry_after = None


class Problem(str):
    """What a module's _classify recognised in a program's error text: the message, with its kind."""
    kind = "account"


def problem(kind: str, message: str) -> Problem:
    value = Problem(message)
    value.kind = kind
    return value


class RequestRejected(SubscriptionError):
    """The program refused this request itself (its JSON schema or input), not the account: another
    plan would refuse the same request, so the connection stays and only this request fails."""


_REQUEST_REJECTIONS = ("not a valid json schema", "invalid_json_schema", "invalid schema", "invalid_request_error",
                       "response_format", "output_schema", "text.format")


def failure(program: str, detail: str, classify, fallback: str) -> SubscriptionError:
    """The error for a failed call, with the program's own words kept: the account problem `classify`
    recognises (sign-in, plan limit), else RequestRejected for a refused request, else `fallback` as a
    temporary failure (the plan itself is not at fault as far as this text says)."""
    text = (detail or "").strip()
    raw = f" (원문: {text[:240]})" if text else ""
    message = classify(text)                 # account problems first: sign-in, plan limit, a model the plan lacks
    if message:
        error = SubscriptionError(message + raw)
        error.kind = getattr(message, "kind", "account")
        if error.kind == "limit":
            error.retry_after = reset_after(text)
        return error
    if any(k in text.lower() for k in _REQUEST_REJECTIONS):
        return RequestRejected(f"{program}가 이번 요청(검증 스키마·입력)을 거부했습니다{raw}")
    error = SubscriptionError(fallback)
    error.kind = "temporary"
    return error


_CLOCK = re.compile(r"\bresets?(?:\s+at)?\s+(\d{1,2})(?::(\d{2}))?\s*([ap])\.?m\b")
_SPAN = re.compile(r"\b(?:try again|retry|resets?|reset)\s+(?:in|after)\s+((?:[\s,]*(?:about|and)?\s*\d+\s*"
                   r"(?:days?|hours?|hrs?|minutes?|mins?|seconds?|secs?|d|h|m|s)(?![a-z]))+)")
_UNIT = re.compile(r"(\d+)\s*(d|h|m|s)")


def reset_after(text: str, now=None) -> float | None:
    """Seconds until a plan limit resets, as the program's message says: "resets 12:10am" (the next
    such local time), "try again in 3 hours 42 minutes", "reset after 3h12m5s". None when it does not say."""
    low = (text or "").lower()
    span = _SPAN.search(low)
    if span:
        seconds = sum(int(n) * {"d": 86400, "h": 3600, "m": 60, "s": 1}[u] for n, u in _UNIT.findall(span.group(1)))
        return float(min(max(seconds + 60, 60), 7 * 86400)) if seconds else None
    clock = _CLOCK.search(low)
    if clock:
        hour, minute = int(clock.group(1)) % 12 + (12 if clock.group(3) == "p" else 0), int(clock.group(2) or 0)
        if minute > 59:
            return None
        now = now or datetime.datetime.now()
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if target <= now:
            target += datetime.timedelta(days=1)
        return (target - now).total_seconds() + 60
    return None


def auth_mode(requested: str | None = None) -> str:
    mode = (requested or os.environ.get("YEONE_OCR_CLAUDE_AUTH", "") or "auto").strip().lower()
    return mode if mode in AUTH_MODES else "auto"


def model(requested: str | None = None) -> str:
    return (requested or os.environ.get("YEONE_OCR_CLAUDE_MODEL", "") or DEFAULT_MODEL).strip()


def label(model_name: str) -> str:
    return model_name or DEFAULT_MODEL


def effort() -> str:
    value = os.environ.get("YEONE_OCR_CLAUDE_EFFORT", "").strip().lower()
    return value if value in EFFORTS else "medium"


def timeout() -> float:
    try:
        value = float(os.environ.get("YEONE_OCR_CLAUDE_CLI_TIMEOUT", "") or 300)
    except ValueError:
        value = 300.0
    return min(max(value, 30.0), 1800.0)


def find_cli() -> str | None:
    configured = os.environ.get("YEONE_OCR_CLAUDE_CLI_PATH", "").strip()
    if configured:
        path = Path(configured).expanduser()
        return str(path) if path.exists() else None
    native = Path.home() / ".local" / "bin" / ("claude.exe" if sys.platform == "win32" else "claude")
    on_path = (shutil.which("claude.exe") if sys.platform == "win32" else None) or shutil.which("claude")
    if on_path and not _is_cmd_shim(on_path):
        return on_path
    if native.exists():
        return str(native)
    return on_path


_npm_prefix_cache: dict = {}


def npm_global_prefix(runner=subprocess.run) -> str | None:
    """npm's own global folder (`npm prefix -g`), which also follows a prefix set in .npmrc. Cached."""
    if "value" in _npm_prefix_cache:
        return _npm_prefix_cache["value"]
    value = None
    names = ("npm.cmd", "npm") if sys.platform == "win32" else ("npm",)
    npm = next((found for found in (shutil.which(n) for n in names) if found), None)
    if npm is None and sys.platform == "win32":
        base = os.environ.get("ProgramFiles", "").strip()
        candidate = Path(base) / "nodejs" / "npm.cmd" if base else None
        npm = str(candidate) if candidate is not None and candidate.is_file() else None
    if npm:
        try:
            proc = runner([npm, "prefix", "-g"], capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=30, **_NO_WINDOW)
            line = (proc.stdout or "").strip().splitlines()
            value = line[-1].strip() if proc.returncode == 0 and line else None
        except (OSError, subprocess.SubprocessError):
            value = None
    _npm_prefix_cache["value"] = value
    return value


def cli_search_folders(name: str, runner=subprocess.run) -> list:
    """Where a program installed outside this process's PATH may be: npm's global folder (asked from npm,
    then the defaults), Node installers, nvm, Volta, pnpm, bun, Scoop, Chocolatey, winget, Homebrew."""
    home = Path.home()
    folders = []
    prefixes = [p for p in (os.environ.get("NPM_CONFIG_PREFIX", "").strip(), npm_global_prefix(runner)) if p]
    if sys.platform == "win32":
        folders += [Path(p) for p in prefixes]
        for variable, tail in (("APPDATA", "npm"), ("ProgramFiles", "nodejs"), ("NVM_SYMLINK", ""), ("VOLTA_HOME", "bin"),
                               ("LOCALAPPDATA", "Volta/bin"), ("LOCALAPPDATA", "pnpm"), ("SCOOP", "shims"),
                               ("ChocolateyInstall", "bin"), ("LOCALAPPDATA", "Microsoft/WinGet/Links"),
                               ("BUN_INSTALL", "bin")):
            base = os.environ.get(variable, "").strip()
            if base:
                folders.append(Path(base) / tail if tail else Path(base))
        folders += [home / "scoop" / "shims", home / ".bun" / "bin", Path("C:/ProgramData/chocolatey/bin")]
    else:
        folders += [Path(p) / "bin" for p in prefixes]
        folders += [home / ".npm-global" / "bin", home / ".local" / "bin", home / ".volta" / "bin", home / ".bun" / "bin",
                    Path("/usr/local/bin"), Path("/opt/homebrew/bin")]
        nvm = home / ".nvm" / "versions" / "node"
        if nvm.is_dir():
            folders += sorted((version / "bin" for version in nvm.iterdir()), reverse=True)
    seen, unique = set(), []
    for folder in folders:
        key = str(folder).lower() if sys.platform == "win32" else str(folder)
        if key not in seen:
            seen.add(key)
            unique.append(folder)
    return unique


def npm_global_cli(name: str, runner=subprocess.run) -> str | None:
    """A command-line program (Codex CLI, Gemini CLI) found in cli_search_folders but not on PATH."""
    names = (f"{name}.cmd", f"{name}.exe") if sys.platform == "win32" else (name,)   # Windows: not npm's sh script
    for folder in cli_search_folders(name, runner):
        for candidate in names:
            path = folder / candidate
            if path.is_file():
                return str(path)
    return None


def not_found_reason(program: str, name: str, hint: str) -> str:
    """Why a program counts as missing: where it was looked for, and how to install it."""
    places = ", ".join(str(f) for f in cli_search_folders(name)[:8])
    return (f"{program}({name})를 찾지 못했습니다 — PATH와 {places} 등을 찾아봤습니다. {hint} "
            f"다른 곳에 설치했다면 PowerShell에서 `where.exe {name}`(Mac·Linux: `which {name}`)로 위치를 보고 "
            f"환경 변수 YEONE_OCR_{'GEMINI' if name == 'gemini' else 'CODEX'}_CLI_PATH로 지정하세요. "
            "`npx`로만 실행했다면 전역 설치가 아니므로 [설치]를 누르세요.")


def _is_cmd_shim(path: str) -> bool:
    return Path(path).suffix.lower() in (".cmd", ".bat")


def cli_env() -> dict:
    env = {k: v for k, v in os.environ.items() if k not in _STRIP_ENV}
    config = os.environ.get("YEONE_OCR_CLAUDE_CONFIG_DIR", "").strip()
    if config:
        env["CLAUDE_CONFIG_DIR"] = str(Path(config).expanduser())
    env.setdefault("PYTHONUTF8", "1")
    return env


def _workdir() -> str:
    """An empty directory: no project CLAUDE.md, .mcp.json or hooks are picked up by the child."""
    path = Path(tempfile.gettempdir()) / "yeone-ocr-claude-cli"
    path.mkdir(exist_ok=True)
    return str(path)


def status(refresh: bool = False, runner=subprocess.run) -> dict:
    """`claude auth status`, reduced: installed, logged_in, billing, plan, usable, reason. Never raises."""
    global _status_cache
    with _lock:
        if not refresh and _status_cache and time.monotonic() - _status_cache[0] < _STATUS_TTL:
            return dict(_status_cache[1], limits=_last_limits)
        result = _query_status(runner)
        _status_cache = (time.monotonic(), result)
        return dict(result, limits=_last_limits)


def reset_status_cache() -> None:
    global _status_cache, _last_limits
    with _lock:
        _status_cache, _last_limits = None, None


def _query_status(runner) -> dict:
    result = {"installed": False, "logged_in": False, "billing": None, "plan": None, "usable": False,
              "reason": INSTALL_HINT}
    cli = find_cli()
    if not cli:
        return result
    result["installed"] = True
    try:
        proc = runner([cli, "auth", "status"], env=cli_env(), cwd=_workdir(), capture_output=True,
                      text=True, encoding="utf-8", errors="replace", timeout=30, **_NO_WINDOW)
        data = json.loads(proc.stdout or "{}")
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        result["reason"] = f"Claude Code 상태를 읽지 못했습니다: {exc}"
        return result
    method = data.get("authMethod")
    result.update(logged_in=bool(data.get("loggedIn")), plan=data.get("subscriptionType"))
    if method in SUBSCRIPTION_METHODS:
        result["billing"] = "subscription"
    elif method:
        result["billing"] = "other"
    if not result["logged_in"] or method in (None, "none"):
        result["reason"] = "Claude Code에 로그인되어 있지 않습니다. " + LOGIN_HINT
    elif result["billing"] != "subscription":
        result["reason"] = ("Claude Code가 구독 계정이 아니라 API 키나 다른 방식으로 로그인되어 있습니다. "
                            "`claude auth logout` 뒤 `claude auth login`으로 Claude 계정에 로그인하세요.")
    elif _is_cmd_shim(cli):
        result["reason"] = CMD_SHIM_HINT
    else:
        result["usable"], result["reason"] = True, None
    return result


def choose(requested_mode: str | None, provider: str, api_key: str, runner=subprocess.run):
    """(use_subscription, message). subscription: always (if usable); api: never;
    auto: Claude review without an API key falls back to the signed-in CLI when usable."""
    mode = auth_mode(requested_mode)
    if mode == "api" or (mode == "auto" and (provider != "anthropic" or api_key)):
        return False, ""
    state = status(runner=runner)
    if state["usable"]:
        return True, f"AI 검증: Claude 구독({state.get('plan') or '요금제'}) · Claude Code 실행 · API 요금 없음(구독 사용량에서 차감)"
    return False, ("Claude 구독으로 검증할 수 없습니다: " + (state["reason"] or "")) if mode == "subscription" else ""


def _parse_events(stdout: str) -> list:
    events = []
    for line in (stdout or "").splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                events.append(json.loads(line))
            except ValueError:
                continue
    return events


# "You've hit your 5-hour limit · resets 12:10am", "weekly limit reached", "usage limit". A bare
# rate_limit_error / overloaded (too many requests at this moment) is left to the temporary path:
# the same plan is tried again, and rests only for a short while if it keeps failing.
_LIMIT = re.compile(r"usage limit|(?<!rate )limit reached|hit your\b[^.\n]{0,40}\blimit|out of (?:extra )?usage")


def _classify(text: str) -> str | None:
    low = text.lower()
    if any(k in low for k in ("not logged in", "/login", "login expired", "authentication_failed",
                              "invalid api key", "oauth token has expired")):
        return problem("account", "Claude Code 로그인이 필요하거나 만료되었습니다. " + LOGIN_HINT)
    if _LIMIT.search(low):
        return problem("limit", "Claude 구독 사용량 한도(5시간 또는 주간)에 도달했습니다. 한도가 초기화되면 다시 씁니다.")
    return None


class SubscriptionClient:
    """One `claude -p` call per request; same contract as the Anthropic API branch of comparison_ai._request."""

    subscription = True
    plan = "claude"
    provider = PROVIDER
    transport = TRANSPORT

    def __init__(self, cli=None, runner=None, timeout_seconds=None, effort_level=None):
        self.cli = cli or find_cli()
        if not self.cli:
            raise SubscriptionError(INSTALL_HINT)
        if _is_cmd_shim(self.cli):
            raise SubscriptionError(CMD_SHIM_HINT)
        self._run = runner or subprocess.run
        self.timeout = timeout_seconds or timeout()
        self.effort = effort_level or effort()

    def request(self, *, model_name, system, prompt, images, schema):
        """-> (text, complete, finish_reason, usage dict, actual model). Images are PNG bytes."""
        global _last_limits
        content = [{"type": "text", "text": prompt}]
        content += [{"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                                 "data": base64.b64encode(data).decode("ascii")}} for data in images]
        message = {"type": "user", "message": {"role": "user", "content": content}}
        args = [self.cli, "-p",
                "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
                "--json-schema", json.dumps(schema, ensure_ascii=False, separators=(",", ":")),
                "--system-prompt", system,
                "--model", model_name, "--effort", self.effort,
                "--tools", "", "--strict-mcp-config", "--setting-sources", "", "--disable-slash-commands",
                "--no-session-persistence", "--max-turns", "4"]
        try:
            proc = self._run(args, input=json.dumps(message) + "\n", env=cli_env(), cwd=_workdir(),
                             capture_output=True, text=True, encoding="utf-8", errors="replace",
                             timeout=self.timeout, **_NO_WINDOW)
        except FileNotFoundError as exc:
            raise SubscriptionError(INSTALL_HINT) from exc
        except subprocess.TimeoutExpired as exc:
            raise TimeoutError(f"Claude Code 응답이 {self.timeout:.0f}초 안에 오지 않았습니다.") from exc
        events = _parse_events(proc.stdout)
        for event in events:
            if event.get("type") == "rate_limit_event" and isinstance(event.get("rate_limit_info"), dict):
                _last_limits = event["rate_limit_info"]
        result = next((e for e in reversed(events) if e.get("type") == "result"), None)
        if result is None:
            detail = (proc.stderr or proc.stdout or "").strip()[-400:]
            raise failure("Claude Code", detail, _classify, f"Claude Code 실행 실패(코드 {proc.returncode}): {detail}")
        structured = result.get("structured_output")
        if result.get("is_error") or structured is None:
            text = str(result.get("result") or "")
            if result.get("stop_reason") == "refusal":
                return text, False, "refusal", {}, model_name
            raise failure("Claude Code", text, _classify, f"Claude Code 오류: {text[:300] or result.get('subtype')}")
        usage = result.get("usage") or {}
        tokens = {name: int(usage.get(name) or 0) for name in ("input_tokens", "output_tokens")}
        actual = next(iter(result.get("modelUsage") or {}), model_name)
        return json.dumps(structured, ensure_ascii=False), True, "end_turn", tokens, actual
