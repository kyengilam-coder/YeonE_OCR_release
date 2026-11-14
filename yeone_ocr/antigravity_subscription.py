"""Gemini subscription through Google's Antigravity CLI (`agy`), the successor of Gemini CLI.

Since 2026-06-18 Gemini CLI no longer serves personal Google accounts (free tier, Google AI Pro, Ultra:
"This client is no longer supported for Gemini Code Assist for individuals"); Google moved them to
Antigravity CLI. This module runs the user's own, unmodified `agy`, which signs in through Google's
own login and keeps its credentials in the system keyring: no token is read here. Personal use only.

One `agy -p` run per request, in a new empty folder that holds only that request's images, its JSON
schema and run settings (and, for a long request, its text). Checked on Windows with agy 1.5.9 and a
Google AI Pro account (docs/AI_REVIEW.md):
- `--output-format json` prints one JSON line: {conversation_id, status ("SUCCESS"), response,
  duration_seconds, num_turns, structured_output, json_schema, usage {input_tokens, output_tokens,
  thinking_tokens, cache_read_tokens, total_tokens}, finished_reason (only when cut short)}; no model name.
  `--json-schema schema.json` makes `structured_output` the answer.
- `--settings yeone-settings.json` = {"agentMode": "", "toolPermission": "request-review"}, for this run
  only and never saved. agy starts a run with no saved mode in Auto mode (edits files and runs commands
  without asking), and `--mode` takes only accept-edits, auto or plan ("default" is warned about and
  runs in Auto). agentMode "" is the Default mode: a headless run reads inside its own folder and
  refuses what needs approval (edits, commands, access outside the folder). `agy -p /config` shows it.
- `--disable-slash-commands`: the request text is never read as a slash command or a skill.
- Images: `@image1.png` in the prompt attaches the file (one turn, no tool call).
- stdin is an empty, closed pipe: `agy -p` does not read it, and an open one has hung it on Windows.
- The sign-in: `agy -p /quota` is answered by the CLI itself (no model call, no quota spent) and fails
  without a sign-in.
- The agent keeps its own system prompt (no option replaces it): ours goes first in the message.

Environment:
  YEONE_OCR_AGY_MODEL       model for agy (`agy models`; default: agy's own choice for the account)
  YEONE_OCR_AGY_CLI_PATH    agy executable (default: %LOCALAPPDATA%\\agy\\bin\\agy.exe, PATH, ~/.local/bin/agy)
  YEONE_OCR_AGY_TIMEOUT     seconds per call (default 300)
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

from .claude_subscription import SubscriptionError, failure, not_found_reason, problem, program_error

SUBSCRIPTION_KEY = "gemini-subscription"
PROVIDER = "gemini"
TRANSPORT = "antigravity-cli-subscription"
PROGRAM = "Antigravity CLI"
DEFAULT_MODEL = ""                      # agy chooses for the signed-in account
# A key, Application Default Credentials or another endpoint would bill an API or Cloud project instead
# of the signed-in account.
_STRIP_ENV = ("GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_GENAI_USE_VERTEXAI", "GOOGLE_APPLICATION_CREDENTIALS",
              "AGY_ADC_AUTH", "GOOGLE_GEMINI_BASE_URL", "GEMINI_BASE_URL", "GEMINI_MODEL")
# This run's settings: the Default mode and approval asked for edits and commands (see above).
RUN_SETTINGS = {"agentMode": "", "toolPermission": "request-review"}
_STATUS_TTL = 60.0
_NO_WINDOW = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)} if sys.platform == "win32" else {}
# Longest request text on the command line (Windows: 32,767 characters for all of it; elsewhere one
# argument holds 128 KiB); a longer one goes in request.md.
_ARG_LIMIT_WINDOWS = 16_000
_ARG_LIMIT_BYTES = 100_000

LOGIN_HINT = ("PowerShell에서 `agy`를 한 번 실행해 열리는 Google 로그인 화면에서 Google 계정(무료 · Google AI Pro/Ultra)으로 "
              "로그인한 뒤 창을 닫고 다시 실행하세요. 로그인은 Google 공식 화면에서 하며 이 프로그램은 토큰을 보지 않습니다.")
INSTALL_HINT = ("Antigravity CLI(agy)가 설치되어 있지 않습니다. PowerShell에서 "
                "`irm https://antigravity.google/cli/install.ps1 | iex`로 설치한 뒤 `agy`를 실행해 로그인하세요.")

_lock = threading.Lock()
_status_cache: tuple[float, dict] | None = None


def model(requested: str | None = None) -> str:
    return (requested or os.environ.get("YEONE_OCR_AGY_MODEL", "") or DEFAULT_MODEL).strip()


def label(model_name: str) -> str:
    return model_name or "agy-auto"


def timeout() -> float:
    try:
        value = float(os.environ.get("YEONE_OCR_AGY_TIMEOUT", "") or 300)
    except ValueError:
        value = 300.0
    return min(max(value, 30.0), 1800.0)


def find_cli() -> str | None:
    """The agy executable: the installer's own folder first, then PATH. On Windows only the native agy.exe —
    an `agy.cmd`/`agy.bat` on PATH can be another program's launcher (the Antigravity desktop app's)."""
    configured = os.environ.get("YEONE_OCR_AGY_CLI_PATH", "").strip()
    if configured:
        path = Path(configured).expanduser()
        return str(path) if path.exists() else None
    local = os.environ.get("LOCALAPPDATA", "").strip()
    installed = [Path(local) / "agy" / "bin" / "agy.exe"] if local else []
    if sys.platform == "win32":
        first = next((str(p) for p in installed if p.is_file()), None)
        if first:
            return first
        found = shutil.which("agy.exe")
        if found and Path(found).suffix.lower() == ".exe":
            return found
    else:
        found = shutil.which("agy")
        if found:
            return found
    candidates = installed + [Path.home() / ".local" / "bin" / "agy"]
    return next((str(p) for p in candidates if p.is_file()), None)


def cli_env() -> dict:
    env = {k: v for k, v in os.environ.items() if k not in _STRIP_ENV}   # an API key would replace the sign-in
    env["NO_COLOR"] = "1"
    env.setdefault("PYTHONUTF8", "1")
    return env


def _empty_dir() -> str:
    path = Path(tempfile.gettempdir()) / "yeone-ocr-agy"
    path.mkdir(exist_ok=True)
    return str(path)


def status(refresh: bool = False, runner=subprocess.run) -> dict:
    """installed, logged_in, billing, plan, usable, reason. Never raises. Runs `agy --version` and
    `agy -p /quota` (answered by agy itself: no model call, no quota spent, but it needs the sign-in)."""
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


def _quiet(runner, args, seconds):
    return runner(args, env=cli_env(), cwd=_empty_dir(), stdin=subprocess.DEVNULL, capture_output=True, text=True,
                  encoding="utf-8", errors="replace", timeout=seconds, **_NO_WINDOW)


def _query_status(runner) -> dict:
    result = {"installed": False, "logged_in": False, "billing": None, "plan": None, "usable": False,
              "reason": INSTALL_HINT, "limits": None}
    cli = find_cli()
    if not cli:
        result["reason"] = not_found_reason(PROGRAM, "agy", INSTALL_HINT)
        return result
    result.update(installed=True, path=cli)
    version = ""
    try:
        proc = _quiet(runner, [cli, "--version"], 60)
        if proc.returncode != 0:
            detail = program_error(proc.stderr, proc.stdout)[1]
            result.update(runs=False, reason=f"{PROGRAM}({cli})를 실행하지 못했습니다(코드 {proc.returncode})"
                                             + (f": {detail}" if detail else "") + ". PowerShell에서 `agy --version`이 되는지 확인하세요.")
            return result
        version = ((proc.stdout or "").strip().splitlines() or [""])[0][:40]
    except subprocess.TimeoutExpired:
        pass                                             # a slow first start: the sign-in check below decides
    except (OSError, subprocess.SubprocessError) as exc:
        result.update(runs=False, reason=f"{PROGRAM}({cli})를 실행하지 못했습니다: {exc}. PowerShell에서 `agy --version`이 되는지 확인하세요.")
        return result
    try:
        proc = _quiet(runner, [cli, "-p", "/quota"], 90)
    except subprocess.TimeoutExpired:
        result["reason"] = (f"{PROGRAM}가 90초 안에 로그인 상태를 알려 주지 않았습니다. " + LOGIN_HINT
                            + " 로그인되어 있다면 [상태 확인]을 다시 누르세요.")
        return result
    except (OSError, subprocess.SubprocessError) as exc:
        result.update(runs=False, reason=f"{PROGRAM}({cli})를 실행하지 못했습니다: {exc}.")
        return result
    plan = "Google 계정 · Antigravity" + (f" {version}" if version else "")
    if proc.returncode == 0:
        result.update(logged_in=True, billing="subscription", plan=plan, usable=True, reason=None)
        return result
    full, detail = program_error(proc.stderr, proc.stdout)
    low = full.lower()
    if _SIGN_IN.search(low):                             # first: the failed command is /quota, so its text may say "quota"
        result["reason"] = f"{PROGRAM}에 로그인되어 있지 않거나 로그인이 만료되었습니다. " + LOGIN_HINT + (f" (원문: {detail[:200]})" if detail else "")
    elif _LIMIT.search(low):                             # signed in; a run rests until the limit resets
        result.update(logged_in=True, billing="subscription", plan=plan, usable=True, reason=None, limits=detail[:200])
    else:
        result["reason"] = (f"{PROGRAM}의 로그인 상태를 확인하지 못했습니다(코드 {proc.returncode})"
                            + (f": {detail[:240]}" if detail else "") + ". " + LOGIN_HINT)
    return result


# agy prints `AGY_ERROR: {...}` on stderr (canonical status, HTTP code, retryability) and exits with 3 when a
# turn fails on the model or agent; startup and sign-in problems are words on stderr.
_SIGN_IN = re.compile(r"please (?:log|sign) ?in|login required|not (?:logged|signed) in|unauthenticated|"
                      r"authentication (?:failed|required|error)|sign[- ]in (?:required|failed|update needed)|no active session|"
                      r"eligibility check failed|not eligible|run `?/login|/logout|"
                      r"invalid[_ ]grant|(?:credentials?|token)\b[^.\n]{0,40}\b(?:expired|invalid|missing|revoked)|"
                      r"(?:status|http|code|error)\D{0,12}401\b")
_LIMIT = re.compile(r"resource_exhausted|resource exhausted|quota|rate[_ ]limit|usage limit|too many requests|"
                    r"credits? balance is too low|(?:status|http|code|error)\D{0,12}429\b")
_MODEL = re.compile(r"unknown model|model not found|model_not_found|failed to construct executor|available models|"
                    r"model\b[^.\n]{0,80}\b(?:is not supported|does not exist|not available)")
_AGY_ERROR = re.compile(r"AGY_ERROR:\s*(\{.*\})")


def _classify(text: str) -> str | None:
    low = text.lower()
    if _SIGN_IN.search(low):
        return problem("account", f"{PROGRAM} 로그인이 필요하거나 만료되었거나, 이 Google 계정을 쓸 수 없습니다. " + LOGIN_HINT)
    if _LIMIT.search(low):
        return problem("limit", "Google 계정의 Antigravity 사용량 한도에 도달했습니다. 한도가 초기화되면 다시 씁니다.")
    if _MODEL.search(low):
        return problem("account", f"{PROGRAM}에 없는 모델입니다. YEONE_OCR_AGY_MODEL을 비우면 agy의 기본 선택을 씁니다"
                                  "(`agy models`로 목록 확인).")
    return None


def _envelope(stdout):
    """The `--output-format json` line: the last line of stdout that is a JSON object."""
    out = (stdout or "").strip()
    for candidate in [out] + [line.strip() for line in reversed(out.splitlines())]:
        if candidate.startswith("{"):
            try:
                data = json.loads(candidate)
            except ValueError:
                continue
            if isinstance(data, dict):
                return data
    return None


def _error_text(stdout, stderr, data) -> tuple[str, str]:
    """(all of the failure's words, the part to show): the AGY_ERROR line, the JSON error, stderr."""
    parts = []
    found = _AGY_ERROR.search(stderr or "")
    if found:
        try:
            error = json.loads(found.group(1))
            if isinstance(error, dict):
                parts.append(" · ".join(str(error[k]) for k in ("status", "code", "http_status", "message", "error") if error.get(k)))
        except ValueError:
            pass
    error = data.get("error") if isinstance(data, dict) else None
    said = error.get("message") if isinstance(error, dict) else error
    if said:
        parts.append(str(said))
    full, detail = program_error(*parts, stderr, "" if data else stdout)
    return full, (" / ".join(p for p in parts if p)[:400] if any(parts) else detail)


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
            try:
                json.loads(text[start:end + 1])
                return text[start:end + 1]
            except ValueError:
                pass
    return text


def _usage(data: dict) -> dict:
    usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
    tokens = {"input_tokens": 0, "output_tokens": 0}
    try:                                                 # informational only
        tokens["input_tokens"] = int(usage.get("input_tokens") or usage.get("input") or 0)
        tokens["output_tokens"] = int(usage.get("output_tokens") or usage.get("output") or 0)
    except (TypeError, ValueError):
        pass
    return tokens


def _fits_argument(text: str) -> bool:
    if sys.platform == "win32":
        return len(text) <= _ARG_LIMIT_WINDOWS
    return len(text.encode("utf-8")) <= _ARG_LIMIT_BYTES


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

    def _call(self, args, root):
        try:
            # input="": stdin is an empty, closed pipe (agy -p reads no stdin; an open one has hung it).
            return self._run(args, input="", env=cli_env(), cwd=str(root), capture_output=True, text=True,
                             encoding="utf-8", errors="replace", timeout=self.timeout, **_NO_WINDOW)
        except FileNotFoundError as exc:
            raise SubscriptionError(INSTALL_HINT) from exc
        except subprocess.TimeoutExpired as exc:
            raise TimeoutError(f"{PROGRAM} 응답이 {self.timeout:.0f}초 안에 오지 않았습니다.") from exc

    def request(self, *, model_name, system, prompt, images, schema):
        """-> (text, complete, finish_reason, usage dict, actual model). Images are PNG bytes."""
        with tempfile.TemporaryDirectory(prefix="yeone-ocr-agy-") as folder:
            root = Path(folder).resolve()
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
            message = (f"{system}\n\n---\n\n{attached}{prompt}\n\nDo not run commands, edit or create files, search "
                       "the web or use any tool other than viewing the attached images. Answer with exactly one JSON "
                       "object and nothing else (no code fence) that validates against this JSON Schema:\n"
                       + json.dumps(schema, ensure_ascii=False, separators=(",", ":")))
            if not _fits_argument(message):
                (root / "request.md").write_text(message, encoding="utf-8")
                message = ("Read the file request.md in the current folder with your file viewing tool and follow "
                           "its instructions exactly. It names the image files to view. Do not run commands, edit "
                           "or create files, or use any other tool. Answer with exactly one JSON object.")
            (root / "yeone-settings.json").write_text(json.dumps(RUN_SETTINGS), encoding="utf-8")
            args = [self.cli, "-p", message, "--output-format", "json",
                    "--settings", str(root / "yeone-settings.json"), "--disable-slash-commands"]
            if isinstance(schema, dict) and schema.get("type") == "object":   # agy takes only an object root
                (root / "schema.json").write_text(json.dumps(schema, ensure_ascii=False), encoding="utf-8")
                args += ["--json-schema", str(root / "schema.json")]
            chosen = model_name if model_name and model_name != label("") else ""
            proc = self._call(args + (["--model", chosen] if chosen else []), root)
            if chosen and proc.returncode != 0 and _MODEL.search(
                    _error_text(proc.stdout, proc.stderr, _envelope(proc.stdout))[0].lower()):
                # A model this account's agy does not have: its own choice instead of losing the plan.
                proc, model_name = self._call(args, root), ""
        data = _envelope(proc.stdout)
        state = str((data or {}).get("status") or "").upper()
        reply = (data or {}).get("response")
        structured = (data or {}).get("structured_output")
        if proc.returncode != 0 or data is None or state != "SUCCESS" or (not isinstance(reply, str) and structured is None):
            full, detail = _error_text(proc.stdout, proc.stderr, data)
            if not detail:
                detail = (f"상태 {state or '없음'} · 답 없음" if data else
                          "출력 없음 — `agy`를 최신으로 올리세요(PowerShell에서 `agy update`)")
            raise failure(PROGRAM, detail, _classify, f"{PROGRAM} 실행 실패(코드 {proc.returncode}): {detail}", full=full)
        actual = data["model"] if isinstance(data.get("model"), str) and data.get("model") else label(model_name)
        tokens = _usage(data)
        if isinstance(structured, (dict, list)):
            text = json.dumps(structured, ensure_ascii=False)
        else:
            text = _json_text(structured if isinstance(structured, str) else reply if isinstance(reply, str) else "")
        ended = data.get("finished_reason")
        if isinstance(ended, str) and ended:               # print_timeout / interrupted / error: a partial answer
            return text, False, ended[:80], tokens, actual
        try:
            json.loads(text)
        except ValueError:
            return text, False, "invalid_json", tokens, actual
        return text, True, "STOP", tokens, actual
