"""Gemini subscription (Google account sign-in: Google AI Pro/Ultra, Gemini Code Assist, free tier)
transport: runs the user's own, unmodified Google Gemini CLI (`gemini -p`).

Google lets a person use the official Gemini CLI with "Login with Google", with that account's
plan quotas. Its terms do not let a third-party program use the Gemini CLI sign-in (OAuth) itself,
so this module never reads ~/.gemini/oauth_creds.json or any token: it starts `gemini`, which
signs in through Google's own login. It reads only the CLI's settings.json to see which sign-in
method was chosen. Personal use only: an app handed to other people should use API keys instead.

One `gemini -p` process per request, in a new empty folder that holds only the images and the
system prompt of that request: read-only plan mode, no extensions, no MCP servers, no project
GEMINI.md. Images are attached with @file references (the CLI sends them as inline image data).
The CLI has no response-schema option, so the schema goes in the prompt and the answer is checked
as JSON here.

Environment:
  YEONE_OCR_GEMINI_MODEL       model for the CLI (default: the CLI's own automatic choice)
  YEONE_OCR_GEMINI_CLI_PATH    gemini executable (default: PATH)
  YEONE_OCR_GEMINI_CLI_HOME    separate Gemini CLI sign-in for this app only (GEMINI_CLI_HOME)
  YEONE_OCR_GEMINI_CLI_TIMEOUT seconds per call (default 300)
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

from .claude_subscription import SubscriptionError, failure

SUBSCRIPTION_KEY = "gemini-subscription"
PROVIDER = "gemini"
TRANSPORT = "gemini-cli-subscription"
DEFAULT_MODEL = ""                      # the CLI chooses (auto routing for the signed-in plan)
SIGN_IN_TYPES = {"oauth-personal"}      # "Login with Google"
# An API key or Vertex settings in the environment would replace the Google sign-in: drop them.
_STRIP_ENV = ("GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_GENAI_USE_VERTEXAI", "GOOGLE_GEMINI_BASE_URL",
              "GOOGLE_APPLICATION_CREDENTIALS", "GEMINI_CLI_USE_COMPUTE_ADC", "GEMINI_SYSTEM_MD",
              "GEMINI_MODEL")
_STATUS_TTL = 60.0
_NO_WINDOW = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)} if sys.platform == "win32" else {}

LOGIN_HINT = ("PowerShell에서 `gemini`를 한 번 실행해 'Login with Google'로 Google 계정에 로그인한 뒤 /quit로 닫고 "
              "다시 실행하세요. 로그인은 Google 공식 화면에서 하며 이 프로그램은 토큰을 보지 않습니다.")
INSTALL_HINT = ("Gemini CLI가 설치되어 있지 않습니다. PowerShell에서 `npm install -g @google/gemini-cli`로 설치한 뒤 "
                "`gemini`를 실행해 'Login with Google'로 로그인하세요.")

_lock = threading.Lock()
_status_cache: tuple[float, dict] | None = None


def model(requested: str | None = None) -> str:
    return (requested or os.environ.get("YEONE_OCR_GEMINI_MODEL", "") or DEFAULT_MODEL).strip()


def label(model_name: str) -> str:
    return model_name or "gemini-cli-auto"


def timeout() -> float:
    try:
        value = float(os.environ.get("YEONE_OCR_GEMINI_CLI_TIMEOUT", "") or 300)
    except ValueError:
        value = 300.0
    return min(max(value, 30.0), 1800.0)


def find_cli() -> str | None:
    configured = os.environ.get("YEONE_OCR_GEMINI_CLI_PATH", "").strip()
    if configured:
        path = Path(configured).expanduser()
        return str(path) if path.exists() else None
    if sys.platform == "win32":
        return shutil.which("gemini.cmd") or shutil.which("gemini.exe") or shutil.which("gemini")
    return shutil.which("gemini")


def _home() -> Path:
    configured = os.environ.get("YEONE_OCR_GEMINI_CLI_HOME", "").strip() or os.environ.get("GEMINI_CLI_HOME", "").strip()
    return Path(configured).expanduser() if configured else Path.home()


def cli_env() -> dict:
    env = {k: v for k, v in os.environ.items() if k not in _STRIP_ENV}
    configured = os.environ.get("YEONE_OCR_GEMINI_CLI_HOME", "").strip()
    if configured:
        env["GEMINI_CLI_HOME"] = str(Path(configured).expanduser())
    env["GOOGLE_GENAI_USE_GCA"] = "true"            # Login with Google, whatever settings.json says
    env.setdefault("PYTHONUTF8", "1")
    return env


def _sign_in_type() -> str | None:
    """security.auth.selectedType from the CLI's settings.json (not a credential file)."""
    home = _home()
    for path in (home / ".gemini" / "settings.json", home / "settings.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        auth = ((data.get("security") or {}).get("auth") or {}) if isinstance(data, dict) else {}
        chosen = auth.get("selectedType") if isinstance(auth, dict) else None
        if not chosen and isinstance(data, dict):
            chosen = data.get("selectedAuthType")      # older settings layout
        if isinstance(chosen, str):
            return chosen
    return None


def status(refresh: bool = False, runner=subprocess.run) -> dict:
    """installed, logged_in, billing, plan, usable, reason. Never raises; runs nothing but `gemini --version`."""
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
    try:
        proc = runner([cli, "--version"], env=cli_env(), cwd=_empty_dir(), capture_output=True, text=True,
                      encoding="utf-8", errors="replace", timeout=60, **_NO_WINDOW)
    except (OSError, subprocess.SubprocessError) as exc:
        result["reason"] = f"Gemini CLI를 실행하지 못했습니다: {exc}"
        return result
    if proc.returncode != 0:
        result["reason"] = f"Gemini CLI를 실행하지 못했습니다(코드 {proc.returncode})."
        return result
    result["installed"] = True
    chosen = _sign_in_type()
    if chosen in SIGN_IN_TYPES:
        result.update(logged_in=True, billing="subscription", plan="Google 계정", usable=True, reason=None)
    elif chosen:
        result.update(billing="other", reason="Gemini CLI가 Google 계정이 아니라 API 키나 Vertex AI로 설정되어 있습니다. "
                                              "`gemini`에서 /auth로 'Login with Google'을 고르세요.")
    else:
        result["reason"] = "Gemini CLI에 로그인되어 있지 않습니다. " + LOGIN_HINT
    return result


def _empty_dir() -> str:
    path = Path(tempfile.gettempdir()) / "yeone-ocr-gemini-cli"
    path.mkdir(exist_ok=True)
    return str(path)


def _classify(text: str) -> str | None:
    low = text.lower()
    if any(k in low for k in ("auth method", "login", "authentication", "unauthenticated", "401",
                              "consent", "credentials", "manual authorization")):
        return "Gemini CLI 로그인이 필요하거나 만료되었습니다. " + LOGIN_HINT
    if any(k in low for k in ("quota", "resource_exhausted", "rate limit", "429", "usage limit")):
        return "Google 계정의 Gemini 사용량 한도에 도달했습니다. 한도가 초기화된 뒤 다시 실행하세요."
    if re.search(r"model\b[^.\n]{0,80}\b(?:is not supported|does not exist|not found)|model_not_found", low):
        return "이 Google 계정에서 쓸 수 없는 모델입니다. YEONE_OCR_GEMINI_MODEL을 비우면 Gemini CLI 기본 선택을 씁니다."
    return None


_FENCE = re.compile(r"^\s*```(?:json)?\s*\n?(.*?)\n?\s*```\s*$", re.DOTALL)


def _json_text(text: str) -> str:
    """The answer as a JSON object string: strips a ``` fence or words around the one object."""
    text = (text or "").strip()
    fenced = _FENCE.match(text)
    if fenced:
        text = fenced.group(1).strip()
    try:
        json.loads(text)
        return text
    except ValueError:
        start, end = text.find("{"), text.rfind("}")
        if 0 <= start < end:
            candidate = text[start:end + 1]
            try:
                json.loads(candidate)
                return candidate
            except ValueError:
                pass
    return text


class SubscriptionClient:
    """One `gemini -p` call per request; same contract as claude_subscription.SubscriptionClient."""

    subscription = True
    plan = "gemini"
    provider = PROVIDER
    transport = TRANSPORT

    def __init__(self, cli=None, runner=None, timeout_seconds=None):
        self.cli = cli or find_cli()
        if not self.cli:
            raise SubscriptionError(INSTALL_HINT)
        self._run = runner or subprocess.run
        self.timeout = timeout_seconds or timeout()

    def request(self, *, model_name, system, prompt, images, schema):
        """-> (text, complete, finish_reason, usage dict, actual model). Images are PNG bytes."""
        with tempfile.TemporaryDirectory(prefix="yeone-ocr-gemini-") as folder:
            root = Path(folder)
            system_md = root / "system.md"
            system_md.write_text(system, encoding="utf-8")
            names = []
            for index, data in enumerate(images, 1):
                (root / f"image{index}.png").write_bytes(data)
                names.append(f"@image{index}.png")
            attached = ("Attached images, in order: " + " ".join(names) + "\n\n") if names else ""
            message = (f"{attached}{prompt}\n\nAnswer with exactly one JSON object and nothing else (no code fence) "
                       "that validates against this JSON Schema:\n"
                       + json.dumps(schema, ensure_ascii=False, separators=(",", ":")))
            args = [self.cli, "-p", "Follow the instructions above. Do not use any tools.", "-o", "json",
                    "--approval-mode", "plan", "-e", "none", "--allowed-mcp-server-names", "none", "--skip-trust"]
            if model_name and model_name != label(""):         # the label of the default is not a model name
                args += ["-m", model_name]
            env = cli_env()
            env["GEMINI_SYSTEM_MD"] = str(system_md)        # replaces the CLI's own agent system prompt
            try:
                # The request goes in through stdin (the -p text is appended): no command-line length limit.
                proc = self._run(args, input=message, env=env, cwd=str(root), capture_output=True, text=True,
                                 encoding="utf-8", errors="replace", timeout=self.timeout, **_NO_WINDOW)
            except FileNotFoundError as exc:
                raise SubscriptionError(INSTALL_HINT) from exc
            except subprocess.TimeoutExpired as exc:
                raise TimeoutError(f"Gemini CLI 응답이 {self.timeout:.0f}초 안에 오지 않았습니다.") from exc
        out = (proc.stdout or "").strip()
        try:
            data = json.loads(out[out.find("{"):]) if "{" in out else {}
        except ValueError:
            data = {}
        error = data.get("error") if isinstance(data, dict) else None
        if proc.returncode != 0 or error or not isinstance(data.get("response"), str):
            detail = (error.get("message") if isinstance(error, dict) else error) or (proc.stderr or out or "").strip()[-400:]
            raise failure("Gemini CLI", str(detail), _classify, f"Gemini CLI 실행 실패(코드 {proc.returncode}): {detail}")
        text = _json_text(data["response"])
        tokens, actual = {"input_tokens": 0, "output_tokens": 0}, label(model_name)
        models = ((data.get("stats") or {}).get("models") or {}) if isinstance(data.get("stats"), dict) else {}
        if isinstance(models, dict) and models:
            actual = str(next(iter(models)))
            try:                                         # usage numbers are informational only
                for item in models.values():
                    counts = (item or {}).get("tokens") or {}
                    tokens["input_tokens"] += int(counts.get("prompt") or counts.get("input") or 0)
                    tokens["output_tokens"] += int(counts.get("candidates") or 0)
            except (AttributeError, TypeError, ValueError):
                tokens = {"input_tokens": 0, "output_tokens": 0}
        try:
            json.loads(text)
        except ValueError:
            return text, False, "invalid_json", tokens, actual
        return text, True, "STOP", tokens, actual
