"""Gemini subscription through Google's Antigravity CLI (`agy`), the successor of Gemini CLI.

Since 2026-06-18 Gemini CLI no longer serves personal Google accounts (free tier, Google AI Pro, Ultra:
"This client is no longer supported for Gemini Code Assist for individuals"); Google moved them to
Antigravity CLI. This module runs the user's own, unmodified `agy`, which signs in through Google's
own login and keeps its credentials in the system keyring: no token is read here. Personal use only.

One `agy` process per request, in a new empty folder that holds only that request's images.
Headless protocol (documented "stream prompts from stdin"; the same calls a Windows Python program
made through pipes in google-antigravity/antigravity-cli#1166): one {"event": "user"} line on stdin,
`--input-format stream-json --output-format stream-json --json-schema <schema>`; the answer is the
`result` event's `structured_output`. Print mode has no image attachment flag (#1278): the images'
folder is opened with `--add-dir` and the prompt asks the agent to view those files. A result that
reports SUCCESS without structured output (#1065, #794) is a failure here, never an answer.

Not verified on a real account from this code's development environment (agy could not be
installed there): docs/AI_REVIEW.md lists the checks to run on a PC.

Environment:
  YEONE_OCR_AGY_MODEL       model for agy (default: agy's own choice), e.g. gemini-3.8-flash-high
  YEONE_OCR_AGY_CLI_PATH    agy executable (default: PATH, then %LOCALAPPDATA%\\agy\\bin\\agy.exe)
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
DEFAULT_MODEL = ""                      # agy chooses
_STRIP_ENV = ("GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_GENAI_USE_VERTEXAI", "GOOGLE_APPLICATION_CREDENTIALS")
_STATUS_TTL = 60.0
_NO_WINDOW = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)} if sys.platform == "win32" else {}

LOGIN_HINT = ("PowerShell에서 `agy`를 한 번 실행해 Google 계정으로 로그인한 뒤 /quit로 닫고 다시 실행하세요. "
              "로그인은 Google 공식 화면에서 하며 이 프로그램은 토큰을 보지 않습니다.")
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
    configured = os.environ.get("YEONE_OCR_AGY_CLI_PATH", "").strip()
    if configured:
        path = Path(configured).expanduser()
        return str(path) if path.exists() else None
    found = (shutil.which("agy.exe") or shutil.which("agy")) if sys.platform == "win32" else shutil.which("agy")
    if found:
        return found
    local = os.environ.get("LOCALAPPDATA", "")
    candidates = [Path(local) / "agy" / "bin" / "agy.exe"] if local else []
    candidates.append(Path.home() / ".local" / "bin" / "agy")
    return next((str(p) for p in candidates if p.is_file()), None)


def cli_env() -> dict:
    env = {k: v for k, v in os.environ.items() if k not in _STRIP_ENV}   # an API key would replace the sign-in
    env.setdefault("PYTHONUTF8", "1")
    return env


def _empty_dir() -> str:
    path = Path(tempfile.gettempdir()) / "yeone-ocr-agy"
    path.mkdir(exist_ok=True)
    return str(path)


def status(refresh: bool = False, runner=subprocess.run) -> dict:
    """installed, logged_in, billing, plan, usable, reason. Never raises; runs nothing but `agy --version`.
    agy keeps its sign-in in the system keyring and has no status command, so an installed agy that
    runs counts as usable; a missing sign-in shows on the first request and takes this plan out of the run."""
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
        result["reason"] = not_found_reason(PROGRAM, "agy", INSTALL_HINT)
        return result
    result.update(installed=True, path=cli)
    try:
        proc = runner([cli, "--version"], env=cli_env(), cwd=_empty_dir(), capture_output=True, text=True,
                      encoding="utf-8", errors="replace", timeout=120, **_NO_WINDOW)
    except subprocess.TimeoutExpired:
        proc = None
    except (OSError, subprocess.SubprocessError) as exc:
        result.update(runs=False, reason=f"{PROGRAM}({cli})를 실행하지 못했습니다: {exc}. PowerShell에서 `agy --version`이 되는지 확인하세요.")
        return result
    if proc is not None and proc.returncode != 0:
        detail = program_error(proc.stderr, proc.stdout)[1]
        result.update(runs=False, reason=f"{PROGRAM}({cli})를 실행하지 못했습니다(코드 {proc.returncode})"
                                         + (f": {detail}" if detail else "") + ". PowerShell에서 `agy --version`이 되는지 확인하세요.")
        return result
    result.update(logged_in=True, billing="subscription", plan="Google 계정(Antigravity)", usable=True, reason=None)
    return result


_SIGN_IN = re.compile(r"please (?:log|sign) ?in|login required|not (?:logged|signed) in|unauthenticated|"
                      r"authentication (?:failed|required|error)|sign[- ]in (?:required|failed)|no active session|"
                      r"invalid[_ ]grant|(?:credentials?|token)\b[^.\n]{0,40}\b(?:expired|invalid|missing|revoked)|"
                      r"(?:status|http|code|error)\D{0,12}401\b")
_LIMIT = re.compile(r"resource_exhausted|resource exhausted|quota|rate[_ ]limit|usage limit|too many requests|"
                    r"(?:status|http|code|error)\D{0,12}429\b")
_MODEL = re.compile(r"unknown model|model not found|model_not_found|failed to construct executor")


def _classify(text: str) -> str | None:
    low = text.lower()
    if _SIGN_IN.search(low):
        return problem("account", "Antigravity CLI 로그인이 필요하거나 만료되었습니다. " + LOGIN_HINT)
    if _LIMIT.search(low):
        return problem("limit", "Google 계정의 Antigravity(Gemini) 사용량 한도에 도달했습니다. 한도가 초기화되면 다시 씁니다.")
    if _MODEL.search(low):
        return problem("account", "Antigravity CLI가 이 모델을 쓸 수 없습니다. YEONE_OCR_AGY_MODEL을 비우면 agy의 기본 선택을 씁니다.")
    return None


def _events(stdout: str) -> list[dict]:
    events = []
    for line in (stdout or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict):
            events.append(value)
    return events


def _kind(event: dict) -> str:
    return str(event.get("event") or event.get("type") or "")


def _result(events: list[dict]) -> dict | None:
    """The last `result` event's payload (the event itself when the fields sit at its top level)."""
    for event in reversed(events):
        if _kind(event) == "result":
            payload = event.get("result")
            return payload if isinstance(payload, dict) else event
    return None


def _usage(result: dict) -> dict:
    usage = result.get("usage") if isinstance(result.get("usage"), dict) else {}
    tokens = {"input_tokens": 0, "output_tokens": 0}
    try:
        tokens["input_tokens"] = int(usage.get("input") or usage.get("input_tokens") or 0)
        tokens["output_tokens"] = int(usage.get("output") or usage.get("output_tokens") or 0)
    except (TypeError, ValueError):
        pass
    return tokens


def _model_used(events: list[dict], result: dict, requested: str) -> str:
    for source in (result, *[e for e in events if _kind(e) == "init"]):
        name = source.get("model") if isinstance(source, dict) else None
        if isinstance(name, str) and name:
            return name
    return label(requested)


class SubscriptionClient:
    """One `agy` call per request; same contract as claude_subscription.SubscriptionClient."""

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
            root = Path(folder).resolve()
            paths = []
            for index, data in enumerate(images, 1):
                path = root / f"image{index}.png"
                path.write_bytes(data)
                paths.append(str(path))
            looked = ""
            if paths:
                looked = ("Before answering, open and look at each of these image files with your file viewing tool, "
                          "in this order (they are the only files you need; do not edit or run anything):\n"
                          + "\n".join(f"{i}. {p}" for i, p in enumerate(paths, 1)) + "\n\n")
            content = (f"{system}\n\n---\n\n{looked}{prompt}\n\n"
                       "Finish with the answer as the structured output required by the JSON Schema. "
                       "Do not write, edit or run anything.")
            message = json.dumps({"event": "user", "message": {"content": content}}, ensure_ascii=False) + "\n"
            limit = int(self.timeout)
            args = [self.cli, "--input-format", "stream-json", "--output-format", "stream-json",
                    "--json-schema", json.dumps(schema, ensure_ascii=False, separators=(",", ":")),
                    "--add-dir", str(root), "--print-timeout", f"{limit}s"]
            if model_name and model_name != label(""):
                args += ["--model", model_name]
            try:
                proc = self._run(args, input=message, env=cli_env(), cwd=str(root), capture_output=True, text=True,
                                 encoding="utf-8", errors="replace", timeout=limit + 60, **_NO_WINDOW)
            except FileNotFoundError as exc:
                raise SubscriptionError(INSTALL_HINT) from exc
            except subprocess.TimeoutExpired as exc:
                raise TimeoutError(f"{PROGRAM} 응답이 {limit}초 안에 오지 않았습니다.") from exc
        events = _events(proc.stdout)
        result = _result(events) or {}
        structured = result.get("structured_output")
        status_text = str(result.get("status") or "")
        if proc.returncode == 0 and status_text.upper() == "SUCCESS" and structured is not None:
            text = structured if isinstance(structured, str) else json.dumps(structured, ensure_ascii=False)
            try:
                json.loads(text)
            except ValueError:
                return text, False, "invalid_json", _usage(result), _model_used(events, result, model_name)
            return text, True, "STOP", _usage(result), _model_used(events, result, model_name)
        error = result.get("error")
        said = error.get("message") if isinstance(error, dict) else error
        full, detail = program_error(proc.stderr, *(json.dumps(e, ensure_ascii=False) for e in events if _kind(e) in ("error", "result")))
        if said:
            full, detail = f"{said}\n{full}", str(said)
        if not detail:
            detail = (f"상태 {status_text or '없음'} · 구조화된 답 없음" if events else "출력 없음")
        raise failure(PROGRAM, detail, _classify, f"{PROGRAM} 실행 실패(코드 {proc.returncode}): {detail}", full=full)
