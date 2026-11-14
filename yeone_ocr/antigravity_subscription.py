"""Gemini subscription through Google Antigravity CLI (`agy -p`): personal Google accounts (free,
Google AI Pro/Ultra), which Gemini CLI no longer serves since 2026-06-18.

Same rule as the other plans: this module only starts the user's own, unmodified `agy`, signed in
with their own Google account (the CLI keeps the sign-in in the system keyring); it never reads,
stores or relays a token, and usage counts against that account's Antigravity quota. Personal use
only: an app handed to other people should use API keys instead.

One `agy -p` run per request, in a new empty folder that holds only that request's images, its JSON
schema and, for a long request, its text:
- `--output-format json` gives one JSON envelope (status, response, structured_output, usage,
  finished_reason); `--json-schema schema.json` makes the agent return the answer in
  `structured_output`, and the text is checked as JSON here as well.
- `--mode default`: since 1.5.7 a run with no saved mode starts in Auto mode, which edits files and
  runs commands without asking. In Default mode a headless run only reads inside its own folder and
  refuses everything that needs approval (edits, commands, access outside the folder).
- stdin is an empty, closed pipe: `agy -p` does not read it, and an open one has hung it on Windows.
- Images are files in that folder, named with @ references in the prompt; the prompt also asks the
  agent to open them with its file viewer if they did not arrive attached.
- The agent keeps its own system prompt (agy has no option to replace it): ours goes first in the message.

Environment:
  YEONE_OCR_ANTIGRAVITY_CLI_PATH  agy executable (default: PATH, %LOCALAPPDATA%\\agy\\bin, ~/.local/bin)
  YEONE_OCR_GEMINI_MODEL          model slug for `--model` (default: the CLI's own choice for the account)
  YEONE_OCR_GEMINI_CLI_TIMEOUT    seconds per call (default 300), shared with Gemini CLI
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

from .claude_subscription import SubscriptionError, failure, problem

PROVIDER = "gemini"
TRANSPORT = "antigravity-cli-subscription"
DEFAULT_LABEL = "antigravity-auto"         # the CLI chooses the model for the signed-in account
PROGRAM = "Antigravity CLI"
# A key, Application Default Credentials or a custom endpoint in the environment would bill a Cloud
# project or the Gemini API instead of the signed-in account: drop them for the child.
_STRIP_ENV = ("GEMINI_API_KEY", "GOOGLE_API_KEY", "AGY_ADC_AUTH", "GOOGLE_APPLICATION_CREDENTIALS",
              "GOOGLE_GEMINI_BASE_URL", "GEMINI_BASE_URL", "GOOGLE_GENAI_USE_VERTEXAI", "GEMINI_MODEL")
_STATUS_TTL = 60.0
_NO_WINDOW = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)} if sys.platform == "win32" else {}
# Longest request text passed on the command line; longer text goes in request.md (Windows: 32,767
# characters for the whole command line; elsewhere one argument may hold 128 KiB).
_ARG_LIMIT_WINDOWS = 16_000
_ARG_LIMIT_BYTES = 100_000

INSTALL_COMMAND_WINDOWS = "irm https://antigravity.google/cli/install.ps1 | iex"
INSTALL_COMMAND_POSIX = "curl -fsSL https://antigravity.google/cli/install.sh | bash"
LOGIN_HINT = ("PowerShell에서 `agy`를 한 번 실행해 열리는 Google 로그인 화면에서 Google 계정(무료 · Google AI Pro/Ultra)으로 "
              "로그인한 뒤 창을 닫고 다시 실행하세요. 로그인은 Google 공식 화면에서 하며 이 프로그램은 토큰을 보지 않습니다.")
INSTALL_HINT = ("Antigravity CLI(agy)가 설치되어 있지 않습니다. 2026-06-18부터 개인 Google 계정(무료 · Google AI Pro/Ultra)은 "
                "Gemini CLI 대신 Antigravity CLI로만 씁니다. PowerShell에서 `" + INSTALL_COMMAND_WINDOWS + "`"
                "(Mac·Linux: `" + INSTALL_COMMAND_POSIX + "`)로 설치하거나 설정 「AI 판독기」 ①의 [설치]를 누른 뒤 "
                "`agy`를 한 번 실행해 로그인하세요.")

_lock = threading.Lock()
_status_cache: tuple[float, dict] | None = None


def label(model_name: str) -> str:
    return model_name or DEFAULT_LABEL


def timeout() -> float:
    try:
        value = float(os.environ.get("YEONE_OCR_GEMINI_CLI_TIMEOUT", "") or 300)
    except ValueError:
        value = 300.0
    return min(max(value, 30.0), 1800.0)


def search_paths() -> list:
    """Where the official installer puts agy, looked at after PATH."""
    home = Path.home()
    if sys.platform == "win32":
        folders = []
        local = os.environ.get("LOCALAPPDATA", "").strip()
        if local:
            folders += [Path(local) / "agy" / "bin", Path(local) / "Microsoft" / "WinGet" / "Links"]
        folders.append(home / "AppData" / "Local" / "agy" / "bin")
        return [folder / "agy.exe" for folder in folders]
    return [home / ".local" / "bin" / "agy", Path("/usr/local/bin/agy"), Path("/opt/homebrew/bin/agy")]


def find_cli() -> str | None:
    """The agy executable. On Windows only the native agy.exe: an `agy.cmd`/`agy.bat` on PATH can be
    another program's launcher (the Antigravity desktop app's), which would open a window instead."""
    configured = os.environ.get("YEONE_OCR_ANTIGRAVITY_CLI_PATH", "").strip()
    if configured:
        path = Path(configured).expanduser()
        return str(path) if path.is_file() else None
    if sys.platform == "win32":
        installed = next((str(p) for p in search_paths()[:1] if p.is_file()), None)   # the installer's own folder first
        if installed:
            return installed
        found = shutil.which("agy.exe")
        if found and Path(found).suffix.lower() == ".exe":
            return found
    else:
        found = shutil.which("agy")
        if found:
            return found
    return next((str(p) for p in search_paths() if p.is_file()), None)


def not_found_reason() -> str:
    places = ", ".join(str(p.parent) for p in search_paths())
    return (f"Antigravity CLI(agy)를 찾지 못했습니다 — PATH와 {places}를 찾아봤습니다. {INSTALL_HINT} "
            "다른 곳에 설치했다면 PowerShell에서 `where.exe agy`(Mac·Linux: `which agy`)로 위치를 보고 "
            "환경 변수 YEONE_OCR_ANTIGRAVITY_CLI_PATH로 지정하세요.")


def cli_env() -> dict:
    env = {k: v for k, v in os.environ.items() if k not in _STRIP_ENV}
    env["NO_COLOR"] = "1"
    env.setdefault("PYTHONUTF8", "1")
    return env


def _empty_dir() -> str:
    path = Path(tempfile.gettempdir()) / "yeone-ocr-antigravity-cli"
    path.mkdir(exist_ok=True)
    return str(path)


def status(refresh: bool = False, runner=subprocess.run) -> dict:
    """installed, logged_in, billing, plan, usable, reason, program. Never raises. Runs `agy --version`
    and `agy -p /quota` (answered by the CLI itself: no model call, no quota spent, but it does need
    the sign-in, so it tells a signed-in account from one that is not)."""
    global _status_cache
    with _lock:
        if not refresh and _status_cache and time.monotonic() - _status_cache[0] < _STATUS_TTL:
            return dict(_status_cache[1])
        result = _query_status(runner)
        _status_cache = (time.monotonic(), result)
        return dict(result)


def cached_status() -> dict | None:
    with _lock:
        return dict(_status_cache[1]) if _status_cache else None


def reset_status_cache() -> None:
    global _status_cache
    with _lock:
        _status_cache = None


def _run_quiet(runner, args, seconds):
    return runner(args, env=cli_env(), cwd=_empty_dir(), stdin=subprocess.DEVNULL, capture_output=True, text=True,
                  encoding="utf-8", errors="replace", timeout=seconds, **_NO_WINDOW)


def _query_status(runner) -> dict:
    result = {"installed": False, "logged_in": False, "billing": None, "plan": None, "usable": False,
              "reason": INSTALL_HINT, "limits": None, "program": "antigravity"}
    cli = find_cli()
    if not cli:
        result["reason"] = not_found_reason()
        return result
    result.update(installed=True, path=cli)
    version = ""
    try:
        proc = _run_quiet(runner, [cli, "--version"], 60)
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip()[-240:]
            result.update(runs=False, reason=f"Antigravity CLI({cli})를 실행하지 못했습니다(코드 {proc.returncode})"
                                             + (f": {detail}" if detail else "")
                                             + ". PowerShell에서 `agy --version`이 되는지 확인하세요.")
            return result
        version = (proc.stdout or "").strip().splitlines()[0][:40] if (proc.stdout or "").strip() else ""
    except subprocess.TimeoutExpired:
        pass                                             # a slow first start: judge the sign-in below
    except (OSError, subprocess.SubprocessError) as exc:
        result.update(runs=False, reason=f"Antigravity CLI({cli})를 실행하지 못했습니다: {exc}. "
                                         "PowerShell에서 `agy --version`이 되는지 확인하세요.")
        return result
    try:
        proc = _run_quiet(runner, [cli, "-p", "/quota"], 90)
    except subprocess.TimeoutExpired:
        result["reason"] = ("Antigravity CLI가 90초 안에 로그인 상태를 알려 주지 않았습니다. " + LOGIN_HINT
                            + " 로그인되어 있다면 [상태 확인]을 다시 누르세요.")
        return result
    except (OSError, subprocess.SubprocessError) as exc:
        result.update(runs=False, reason=f"Antigravity CLI({cli})를 실행하지 못했습니다: {exc}.")
        return result
    if proc.returncode == 0:
        name = "Google 계정 · Antigravity CLI" + (f" {version}" if version else "")
        result.update(logged_in=True, billing="subscription", plan=name, usable=True, reason=None)
        return result
    text = _error_text(proc.stdout, proc.stderr)
    # Sign-in words first: the failed command itself is /quota, so its error text may say "quota".
    found = (problem("account", "Antigravity CLI에 로그인되어 있지 않거나 로그인이 만료되었습니다. " + LOGIN_HINT)
             if _SIGN_IN.search(text.lower()) else _classify(text))
    if found is not None and found.kind == "limit":       # signed in; the run rests until the limit resets
        result.update(logged_in=True, billing="subscription", plan="Google 계정 · Antigravity CLI", usable=True,
                      reason=None, limits=str(found))
    elif found is not None:
        result["reason"] = str(found) + (f" (원문: {text[:200]})" if text else "")
    else:
        result["reason"] = (f"Antigravity CLI의 로그인 상태를 확인하지 못했습니다(코드 {proc.returncode})"
                            + (f": {text[:240]}" if text else "") + ". " + LOGIN_HINT)
    return result


# ---- errors ----
# agy prints `AGY_ERROR: {...}` on stderr (canonical status, HTTP code, retryability) and exits with 3
# when a turn fails on the model or agent; startup and sign-in problems are words on stderr.
_AGY_ERROR = re.compile(r"AGY_ERROR:\s*(\{.*\})")
_LIMIT = re.compile(r"quota|resource_exhausted|resource exhausted|rate[_ ]limit|usage limit|too many requests|"
                    r"credits? balance is too low|out of (?:credits|quota)|(?:status|http|code|error)\D{0,12}429\b|\b429\s+too")
_SIGN_IN = re.compile(r"authentication required|auth(?:entication)? (?:failed|error)|please (?:log|sign) ?in|"
                      r"login required|not (?:logged|signed) in|unauthenticated|sign-in update needed|run `?/login|"
                      r"/logout|eligibility check failed|not eligible|invalid[_ ]grant|"
                      r"(?:credentials?|token)\b[^.\n]{0,40}\b(?:expired|invalid|missing|revoked)|"
                      r"(?:status|http|code|error)\D{0,12}401\b|\b401\s+unauth")
_MODEL = re.compile(r"unknown model|model\b[^.\n]{0,80}\b(?:is not supported|does not exist|not found|not available)|"
                    r"available models|model_not_found")


def _error_text(stdout, stderr) -> str:
    """The failure in the CLI's own words: the AGY_ERROR line, the JSON error, or stderr."""
    err = (stderr or "").strip()
    found = _AGY_ERROR.search(err)
    if found:
        try:
            data = json.loads(found.group(1))
            if isinstance(data, dict):
                parts = [str(data.get(k)) for k in ("status", "code", "http_status", "message", "error") if data.get(k)]
                if parts:
                    return " · ".join(parts)
        except ValueError:
            pass
    data = _envelope(stdout)
    if isinstance(data, dict):
        error = data.get("error")
        message = (error.get("message") if isinstance(error, dict) else error) or ""
        if message:
            return str(message).strip()
    return (err or (stdout or "")).strip()[-400:]


def _classify(text: str):
    low = (text or "").lower()
    if _LIMIT.search(low):
        return problem("limit", "Google 계정의 Antigravity 사용량 한도에 도달했습니다. 한도가 초기화되면 다시 씁니다.")
    if _SIGN_IN.search(low):
        return problem("account", "Antigravity CLI 로그인이 필요하거나 만료되었거나, 이 Google 계정을 쓸 수 없습니다. " + LOGIN_HINT)
    if _MODEL.search(low):
        return problem("account", "이 Google 계정의 Antigravity CLI에서 쓸 수 없는 모델입니다. "
                                  "YEONE_OCR_GEMINI_MODEL을 비우면 Antigravity CLI의 기본 선택을 씁니다(`agy models`로 목록 확인).")
    return None


def _envelope(stdout):
    """The `--output-format json` envelope: the last line of stdout that is a JSON object."""
    out = (stdout or "").strip()
    if not out:
        return None
    for candidate in [out] + [line.strip() for line in reversed(out.splitlines())]:
        if candidate.startswith("{"):
            try:
                data = json.loads(candidate)
            except ValueError:
                continue
            if isinstance(data, dict):
                return data
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


def _usage(data) -> dict:
    tokens = {"input_tokens": 0, "output_tokens": 0}
    usage = data.get("usage") if isinstance(data, dict) else None
    if not isinstance(usage, dict):
        return tokens
    try:                                                 # informational only
        tokens["input_tokens"] = int(usage.get("input_tokens") or usage.get("input") or usage.get("prompt_tokens") or 0)
        tokens["output_tokens"] = int(usage.get("output_tokens") or usage.get("output") or 0)
    except (TypeError, ValueError):
        return {"input_tokens": 0, "output_tokens": 0}
    return tokens


def _fits_argument(text: str) -> bool:
    if sys.platform == "win32":
        return len(text) <= _ARG_LIMIT_WINDOWS
    return len(text.encode("utf-8")) <= _ARG_LIMIT_BYTES


_DEFAULT_LABELS = {DEFAULT_LABEL, "gemini-cli-auto"}


class SubscriptionClient:
    """One `agy -p` run per request; same contract as claude_subscription.SubscriptionClient."""

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
        with tempfile.TemporaryDirectory(prefix="yeone-ocr-agy-") as folder:
            root = Path(folder)
            names = []
            for index, data in enumerate(images, 1):
                (root / f"image{index}.png").write_bytes(data)
                names.append(f"@image{index}.png")
            attached = ""
            if names:
                attached = ("Attached images, in order: " + " ".join(names) + "\n"
                            "They are image files in the current folder. If they are not already visible to you as "
                            "images, open each of them with your file viewing tool before answering; that is the only "
                            "tool you may use.\n\n")
            message = (f"{system}\n\n{attached}{prompt}\n\nDo not run commands, edit or create files, search the web "
                       "or use any tool other than viewing the attached images. Answer with exactly one JSON object "
                       "and nothing else (no code fence) that validates against this JSON Schema:\n"
                       + json.dumps(schema, ensure_ascii=False, separators=(",", ":")))
            if not _fits_argument(message):
                (root / "request.md").write_text(message, encoding="utf-8")
                message = ("Read the file request.md in the current folder with your file viewing tool and follow "
                           "its instructions exactly. It names the image files to view. Do not run commands, edit "
                           "or create files, or use any other tool. Answer with exactly one JSON object.")
            args = [self.cli, "-p", message, "--output-format", "json", "--mode", "default"]
            if isinstance(schema, dict) and schema.get("type") == "object":   # agy accepts only an object root
                (root / "schema.json").write_text(json.dumps(schema, ensure_ascii=False), encoding="utf-8")
                args += ["--json-schema", str(root / "schema.json")]
            if model_name and model_name not in _DEFAULT_LABELS:     # a default's label is not a model slug
                args += ["--model", model_name]
            try:
                # input="": stdin is an empty, closed pipe (agy -p reads no stdin; an open one has hung it).
                proc = self._run(args, input="", env=cli_env(), cwd=str(root), capture_output=True, text=True,
                                 encoding="utf-8", errors="replace", timeout=self.timeout, **_NO_WINDOW)
            except FileNotFoundError as exc:
                raise SubscriptionError(INSTALL_HINT) from exc
            except subprocess.TimeoutExpired as exc:
                raise TimeoutError(f"Antigravity CLI 응답이 {self.timeout:.0f}초 안에 오지 않았습니다.") from exc
        data = _envelope(proc.stdout)
        state = str((data or {}).get("status") or "").upper()
        reply = (data or {}).get("response")
        structured = (data or {}).get("structured_output")
        if proc.returncode != 0 or data is None or (state and state != "SUCCESS") or (
                not isinstance(reply, str) and structured is None):
            detail = _error_text(proc.stdout, proc.stderr)
            if proc.returncode == 0 and not detail:
                detail = "빈 응답(Antigravity CLI가 아무것도 출력하지 않음 — `agy`를 최신으로 올리세요)"
            raise failure("Antigravity CLI", detail, _classify,
                          f"Antigravity CLI 실행 실패(코드 {proc.returncode}{', ' + state if state else ''}): {detail[:400]}")
        actual = str(data.get("model")) if isinstance(data.get("model"), str) and data.get("model") else label(model_name)
        tokens = _usage(data)
        if isinstance(structured, (dict, list)):
            text = json.dumps(structured, ensure_ascii=False)
        else:
            text = _json_text(reply if isinstance(reply, str) else "")
        ended = data.get("finished_reason")
        if isinstance(ended, str) and ended:               # print_timeout / interrupted / error: a partial answer
            return text, False, ended[:80], tokens, actual
        try:
            json.loads(text)
        except ValueError:
            return text, False, "invalid_json", tokens, actual
        return text, True, "STOP", tokens, actual
