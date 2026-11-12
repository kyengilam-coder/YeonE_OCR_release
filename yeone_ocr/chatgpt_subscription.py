"""ChatGPT subscription (Plus/Pro/Business/Edu/Enterprise) transport: runs the user's own, unmodified
OpenAI Codex CLI (`codex exec`).

OpenAI includes Codex in ChatGPT plans and lets a person sign in to the official Codex CLI with
"Sign in with ChatGPT" (https://help.openai.com/en/articles/11369540). Same rule as
claude_subscription: this module never reads ~/.codex/auth.json or any token. It starts `codex`,
which signs in through OpenAI's own login, and usage counts against that person's plan limits.
Personal use only: an app handed to other people should use API keys instead.

One `codex exec` process per request: no session is saved (--ephemeral), the user's config.toml,
rules, web search and project files are not used, the sandbox is read-only and nothing is ever
approved, and the run happens in a new empty folder that holds only the images, the JSON schema
(--output-schema) and the instructions file of that request.

Environment:
  YEONE_OCR_CHATGPT_MODEL       model for the CLI (default: the CLI's own default for the plan)
  YEONE_OCR_CHATGPT_EFFORT      minimal | low | medium (default) | high | xhigh
  YEONE_OCR_CODEX_CLI_PATH      codex executable (default: PATH)
  YEONE_OCR_CODEX_HOME          separate Codex sign-in for this app only (CODEX_HOME)
  YEONE_OCR_CHATGPT_CLI_TIMEOUT seconds per call (default 300)
"""
from __future__ import annotations

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

from .claude_subscription import SubscriptionError, failure, npm_global_cli

SUBSCRIPTION_KEY = "chatgpt-subscription"
PROVIDER = "openai"
TRANSPORT = "codex-cli-subscription"
DEFAULT_MODEL = ""                       # the CLI picks the plan's default model
EFFORTS = ("minimal", "low", "medium", "high", "xhigh")
# `codex exec` prefers an API key from the environment over the ChatGPT sign-in: drop them for the child.
_STRIP_ENV = ("OPENAI_API_KEY", "CODEX_API_KEY", "OPENAI_BASE_URL", "OPENAI_ORG_ID", "OPENAI_PROJECT_ID")
_STATUS_TTL = 60.0
_NO_WINDOW = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)} if sys.platform == "win32" else {}

LOGIN_HINT = ("PowerShell에서 `codex login`을 실행해 'Sign in with ChatGPT'로 ChatGPT 계정(Plus/Pro 등)에 로그인한 뒤 "
              "다시 실행하세요. 로그인은 OpenAI 공식 화면에서 하며 이 프로그램은 토큰을 보지 않습니다.")
INSTALL_HINT = ("OpenAI Codex CLI가 설치되어 있지 않습니다. PowerShell에서 `npm install -g @openai/codex`로 설치한 뒤 "
                "`codex login`으로 ChatGPT 계정에 로그인하세요.")

_lock = threading.Lock()
_status_cache: tuple[float, dict] | None = None


def model(requested: str | None = None) -> str:
    return (requested or os.environ.get("YEONE_OCR_CHATGPT_MODEL", "") or DEFAULT_MODEL).strip()


def label(model_name: str) -> str:
    """Model name for logs, records and caches (the CLI default has no name of its own here)."""
    return model_name or "codex-default"


def effort() -> str:
    value = os.environ.get("YEONE_OCR_CHATGPT_EFFORT", "").strip().lower()
    return value if value in EFFORTS else "medium"


def timeout() -> float:
    try:
        value = float(os.environ.get("YEONE_OCR_CHATGPT_CLI_TIMEOUT", "") or 300)
    except ValueError:
        value = 300.0
    return min(max(value, 30.0), 1800.0)


def find_cli() -> str | None:
    configured = os.environ.get("YEONE_OCR_CODEX_CLI_PATH", "").strip()
    if configured:
        path = Path(configured).expanduser()
        return str(path) if path.exists() else None
    if sys.platform == "win32":
        found = shutil.which("codex.exe") or shutil.which("codex.cmd") or shutil.which("codex")
    else:
        found = shutil.which("codex")
    return found or npm_global_cli("codex")


def cli_env() -> dict:
    env = {k: v for k, v in os.environ.items() if k not in _STRIP_ENV}
    home = os.environ.get("YEONE_OCR_CODEX_HOME", "").strip()
    if home:
        env["CODEX_HOME"] = str(Path(home).expanduser())
    env.setdefault("PYTHONUTF8", "1")
    return env


def _empty_dir() -> str:
    path = Path(tempfile.gettempdir()) / "yeone-ocr-codex-cli"
    path.mkdir(exist_ok=True)
    return str(path)


def status(refresh: bool = False, runner=subprocess.run) -> dict:
    """`codex login status`, reduced: installed, logged_in, billing, plan, usable, reason. Never raises."""
    global _status_cache
    with _lock:
        if not refresh and _status_cache and time.monotonic() - _status_cache[0] < _STATUS_TTL:
            return dict(_status_cache[1])
        result = _query_status(runner)
        _status_cache = (time.monotonic(), result)
        return dict(result)


def reset_status_cache() -> None:
    global _status_cache
    with _lock:
        _status_cache = None


def _query_status(runner) -> dict:
    result = {"installed": False, "logged_in": False, "billing": None, "plan": None, "usable": False,
              "reason": INSTALL_HINT, "limits": None}
    cli = find_cli()
    if not cli:
        return result
    result.update(installed=True, path=cli)
    try:
        proc = runner([cli, "login", "status"], env=cli_env(), cwd=_empty_dir(), capture_output=True,
                      text=True, encoding="utf-8", errors="replace", timeout=30, **_NO_WINDOW)
    except (OSError, subprocess.SubprocessError) as exc:
        result["reason"] = f"Codex CLI 상태를 읽지 못했습니다: {exc}"
        return result
    text = f"{proc.stdout or ''}\n{proc.stderr or ''}".lower()
    if "logged in using chatgpt" in text:
        result.update(logged_in=True, billing="subscription", plan="ChatGPT", usable=True, reason=None)
    elif "logged in" in text and "not logged in" not in text:
        result.update(logged_in=True, billing="other",
                      reason="Codex CLI가 ChatGPT 계정이 아니라 API 키로 로그인되어 있습니다. "
                             "`codex logout` 뒤 `codex login`에서 'Sign in with ChatGPT'를 고르세요.")
    else:
        result["reason"] = "Codex CLI에 로그인되어 있지 않습니다. " + LOGIN_HINT
    return result


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


def _classify(text: str) -> str | None:
    low = text.lower()
    if any(k in low for k in ("not logged in", "codex login", "unauthorized", "401", "refresh token",
                              "token_expired", "sign in again")):
        return "Codex CLI 로그인이 필요하거나 만료되었습니다. " + LOGIN_HINT
    if any(k in low for k in ("usage limit", "rate limit", "rate_limit", "429", "quota", "limit reached")):
        return "ChatGPT 요금제의 Codex 사용량 한도에 도달했습니다. 한도가 초기화된 뒤 다시 실행하세요."
    if _MODEL_REFUSED.search(low):
        return ("이 ChatGPT 요금제에서 쓸 수 없는 모델입니다. YEONE_OCR_CHATGPT_MODEL을 비우면 "
                "Codex 기본 모델을 씁니다.")
    return None


# "The 'x' model is not supported when using Codex with a ChatGPT account", model_not_found, ...
_MODEL_REFUSED = re.compile(r"model_not_found|unsupported model|model\b[^.\n]{0,80}\b(?:is not supported|does not exist|not found)")


class SubscriptionClient:
    """One `codex exec` call per request; same contract as claude_subscription.SubscriptionClient."""

    subscription = True
    plan = "chatgpt"
    provider = PROVIDER
    transport = TRANSPORT

    def __init__(self, cli=None, runner=None, timeout_seconds=None, effort_level=None):
        self.cli = cli or find_cli()
        if not self.cli:
            raise SubscriptionError(INSTALL_HINT)
        self._run = runner or subprocess.run
        self.timeout = timeout_seconds or timeout()
        self.effort = effort_level or effort()

    def request(self, *, model_name, system, prompt, images, schema):
        """-> (text, complete, finish_reason, usage dict, actual model). Images are PNG bytes."""
        with tempfile.TemporaryDirectory(prefix="yeone-ocr-codex-") as folder:
            root = Path(folder)
            (root / "instructions.md").write_text(system, encoding="utf-8")
            (root / "schema.json").write_text(json.dumps(schema, ensure_ascii=False), encoding="utf-8")
            paths = []
            for index, data in enumerate(images, 1):
                path = root / f"image{index}.png"
                path.write_bytes(data)
                paths.append(str(path))
            last = root / "last-message.txt"
            args = [self.cli, "exec", "--json", "--ephemeral", "--ignore-user-config", "--ignore-rules",
                    "--skip-git-repo-check", "-s", "read-only", "-C", str(root),
                    "-c", 'approval_policy="never"', "-c", 'web_search="disabled"',
                    "-c", f'model_reasoning_effort="{self.effort}"',
                    "-c", "model_instructions_file=" + json.dumps(str(root / "instructions.md")),
                    "--output-schema", str(root / "schema.json"), "-o", str(last)]
            if model_name and model_name != label(""):         # the label of the default is not a model name
                args += ["-m", model_name]
            for path in paths:
                args += ["-i", path]
            args.append("-")                                   # prompt from stdin: no command-line length limit
            try:
                proc = self._run(args, input=prompt, env=cli_env(), cwd=str(root), capture_output=True,
                                 text=True, encoding="utf-8", errors="replace", timeout=self.timeout, **_NO_WINDOW)
            except FileNotFoundError as exc:
                raise SubscriptionError(INSTALL_HINT) from exc
            except subprocess.TimeoutExpired as exc:
                raise TimeoutError(f"Codex CLI 응답이 {self.timeout:.0f}초 안에 오지 않았습니다.") from exc
            text = last.read_text(encoding="utf-8", errors="replace").strip() if last.exists() else ""
        events = _parse_events(proc.stdout)
        failed = next((e for e in reversed(events) if e.get("type") == "turn.failed"), None)
        done = next((e for e in reversed(events) if e.get("type") == "turn.completed"), None)
        if failed is not None or (done is None and not text):
            error = (failed or {}).get("error") or {}
            detail = error.get("message") if isinstance(error, dict) else str(error)
            if not detail:
                errors = [str(e.get("message") or "") for e in events if e.get("type") == "error"]
                detail = errors[-1] if errors else (proc.stderr or proc.stdout or "").strip()[-400:]
            raise failure("Codex CLI", detail or "", _classify, f"Codex CLI 실행 실패(코드 {proc.returncode}): {detail}")
        if not text:
            message = next((e.get("item", {}).get("text") for e in reversed(events) if e.get("type") == "item.completed"
                            and e.get("item", {}).get("type") == "agent_message"), "")
            text = (message or "").strip()
        usage = (done or {}).get("usage") or {}
        tokens = {name: int(usage.get(name) or 0) for name in ("input_tokens", "output_tokens")}
        try:
            json.loads(text)
        except ValueError:
            return text, False, "invalid_json", tokens, label(model_name)
        return text, True, "completed", tokens, label(model_name)
