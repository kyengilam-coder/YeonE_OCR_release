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

from .claude_subscription import SubscriptionError, failure, not_found_reason, program_error, npm_global_cli, problem

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


_newest: str | None = None             # the newest of several installed Codex CLIs, as the last status found it


def find_cli() -> str | None:
    """The Codex CLI to run: the one status() found newest when several are installed, else the first on PATH,
    else npm's global folder."""
    configured = os.environ.get("YEONE_OCR_CODEX_CLI_PATH", "").strip()
    if configured:
        path = Path(configured).expanduser()
        return str(path) if path.exists() else None
    if _newest and Path(_newest).is_file():
        return _newest
    found = candidates()
    return found[0] if found else None


def candidates() -> list:
    """Every Codex CLI on this PC that the app could run: each PATH folder's codex.exe / codex.cmd (Windows) or
    codex, then npm's global folder. A PC can hold an old standalone codex.exe ahead of npm's codex.cmd on PATH:
    [설치] (npm) then updates the one the app does not run (seen: 0.139.0 kept running after an update)."""
    names = ("codex.exe", "codex.cmd") if sys.platform == "win32" else ("codex",)
    found, seen = [], set()
    for folder in os.environ.get("PATH", "").split(os.pathsep):
        if not folder.strip():
            continue
        for name in names:
            path = Path(folder.strip().strip('"')) / name
            key = os.path.normcase(str(path))
            if key not in seen and path.is_file():
                seen.add(key)
                found.append(str(path))
    npm = npm_global_cli("codex")
    if npm and os.path.normcase(npm) not in seen:
        found.append(npm)
    return found


def _update_command(cli: str) -> str:
    """How to update the Codex CLI that runs: npm's copy through npm, OpenAI's standalone one through its installer."""
    low = (cli or "").replace("/", "\\").lower()
    if low.endswith(".exe") and "\\npm\\" not in low and "node_modules" not in low:
        return "irm https://chatgpt.com/codex/install.ps1 | iex"
    return "npm install -g @openai/codex@latest"


def _version_of(cli: str, runner) -> tuple:
    try:
        proc = runner([cli, "--version"], env=cli_env(), cwd=_empty_dir(), capture_output=True, text=True,
                      encoding="utf-8", errors="replace", timeout=60, **_NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        return ()
    found = re.search(r"(\d+)\.(\d+)\.(\d+)", proc.stdout or "")
    return tuple(int(n) for n in found.groups()) if found and proc.returncode == 0 else ()


def _pick_newest(runner) -> None:
    """With several Codex CLIs installed, run the newest (by `--version`); an explicit path is kept."""
    global _newest
    if os.environ.get("YEONE_OCR_CODEX_CLI_PATH", "").strip():
        return
    found = candidates()
    if len(found) < 2:
        _newest = None
        return
    versions = [(_version_of(cli, runner), -index, cli) for index, cli in enumerate(found)]
    best = max(versions)
    _newest = best[2] if best[0] else None


def codex_home() -> Path:
    """The Codex home this app's `codex` uses (CODEX_HOME, else ~/.codex)."""
    home = os.environ.get("YEONE_OCR_CODEX_HOME", "").strip() or os.environ.get("CODEX_HOME", "").strip()
    return Path(home).expanduser() if home else Path.home() / ".codex"


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
    global _status_cache, _newest
    with _lock:
        _status_cache, _newest = None, None


def _query_status(runner) -> dict:
    result = {"installed": False, "logged_in": False, "billing": None, "plan": None, "usable": False,
              "reason": INSTALL_HINT, "limits": None}
    _pick_newest(runner)
    cli = find_cli()
    if not cli:
        result["reason"] = not_found_reason("Codex CLI", "codex", INSTALL_HINT)
        return result
    result.update(installed=True, path=cli)
    try:
        proc = runner([cli, "login", "status"], env=cli_env(), cwd=_empty_dir(), capture_output=True,
                      text=True, encoding="utf-8", errors="replace", timeout=30, **_NO_WINDOW)
    except (OSError, subprocess.SubprocessError) as exc:
        result["reason"] = f"Codex CLI 상태를 읽지 못했습니다: {exc}"
        return result
    text = f"{proc.stdout or ''}\n{proc.stderr or ''}".lower()
    refused = _refusal_for(cli)
    if "logged in using chatgpt" in text and refused:
        result.update(logged_in=True, billing="subscription", plan="ChatGPT", closed=refused[1], reason=refused[0])
    elif "logged in using chatgpt" in text:
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


# An HTTP status is a sign only next to the word that names it ("status 401", "401 Unauthorized"):
# a bare number also appears in timestamps, request ids and paths of an unrelated failure.
_SIGN_IN = re.compile(r"not logged in|codex login|unauthorized|refresh token|token_expired|sign in again|"
                      r"(?:status|http|code|error)\D{0,12}401\b|\b401\s+unauth")
_LIMIT = re.compile(r"usage limit|usage_limit|rate[_ ]limit|limit reached|quota|too many requests|"
                    r"(?:status|http|code|error)\D{0,12}429\b|\b429\s+too")
_OLD_CLI = re.compile(r"requires? a newer version|newer version of codex|upgrade to the latest|please (?:update|upgrade) codex|"
                      r"unsupported client version|client version\b[^.\n]{0,40}\bnot supported")


def _classify(text: str) -> str | None:
    low = text.lower()
    if _SIGN_IN.search(low):
        return problem("account", "Codex CLI 로그인이 필요하거나 만료되었습니다. " + LOGIN_HINT)
    if _LIMIT.search(low):
        return problem("limit", "ChatGPT 요금제의 Codex 사용량 한도에 도달했습니다. 한도가 초기화되면 다시 씁니다.")
    if _OLD_CLI.search(low):
        return problem("account", UPDATE_HINT)
    if _MODEL_REFUSED.search(low):
        return problem("account", "이 ChatGPT 요금제에서 쓸 수 없는 모델입니다. YEONE_OCR_CHATGPT_MODEL을 비우면 "
                                  "Codex 기본 모델을 씁니다.")
    return None


UPDATE_HINT = ("설치된 Codex CLI가 오래되어 지금 ChatGPT 계정이 쓰는 모델을 부르지 못합니다. 설정 「AI 판독기」 ①의 ChatGPT [설치]를 "
               "눌러 최신으로 올리세요(PowerShell: npm install -g @openai/codex).")

# "The 'x' model is not supported when using Codex with a ChatGPT account", model_not_found, ...
_MODEL_REFUSED = re.compile(r"model_not_found|unsupported model|model\b[^.\n]{0,80}\b(?:is not supported|does not exist|not found)")


class ModelRefused(SubscriptionError):
    """The ChatGPT account does not serve this model through Codex (model_not_found)."""

    def __init__(self, model, message, detail="", newer=False):
        super().__init__(message)
        self.model = model
        self.detail = detail          # OpenAI's own words
        self.newer = newer            # "requires a newer version of Codex": the CLI, not the plan


# Tried after the models `codex debug models` lists, when Codex's own default is refused (2026-11 names).
FALLBACK_MODELS = ("gpt-6.1-sol", "gpt-6-sol", "gpt-6-luna", "gpt-5.6-sol")
_working_model: str | None = None        # the model that answered after the default was refused (this process)
_catalog_cache: list | None = None
_model_lock = threading.Lock()
# Every model refused: (the CLI file's stamp, the message). status() shows it instead of '사용 가능' until the
# CLI file changes ([설치] updated it) or the program restarts.
_refusal: tuple | None = None


def _stamp(cli: str):
    try:
        info = os.stat(cli)
        return (str(cli), info.st_size, info.st_mtime_ns)
    except OSError:
        return (str(cli), None, None)


def _refusal_for(cli: str) -> tuple | None:
    """(message, short mark) of the refusal remembered for this CLI file, or None."""
    global _refusal, _catalog_cache, _working_model
    with _model_lock:
        if _refusal is None:
            return None
        if _refusal[0] != _stamp(cli):                   # a new Codex CLI: its own list and default may work
            _refusal, _catalog_cache, _working_model = None, None, None
            return None
        return _refusal[1], _refusal[2]


def _mark_refused(cli: str, message: str, mark: str) -> None:
    global _refusal, _status_cache
    with _model_lock:
        _refusal = (_stamp(cli), message, mark)
    with _lock:
        _status_cache = None


def cached_models(home: Path | None = None) -> list:
    """The account's own models as Codex last fetched them from OpenAI (CODEX_HOME/models_cache.json), in
    Codex's order: image-capable, listed ones. Not a credential file (no token in it); [] when absent."""
    path = (home or codex_home()) / "models_cache.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return _slugs(data)


def _slugs(data) -> list:
    entries = data.get("models", data.get("data", [])) if isinstance(data, dict) else data
    if not isinstance(entries, list):
        return []
    listed = [m for m in entries if isinstance(m, dict) and isinstance(m.get("slug") or m.get("id"), str)
              and m.get("visibility", "list") == "list" and "image" in (m.get("input_modalities") or ["image"])]
    ordered = sorted(listed, key=lambda m: m.get("priority", 999) if isinstance(m.get("priority"), (int, float)) else 999)
    return [m.get("slug") or m.get("id") for m in ordered]


def _remember(model_name: str) -> None:
    global _working_model
    with _model_lock:
        _working_model = model_name


def reset_model_choice() -> None:
    global _working_model, _catalog_cache, _refusal
    with _model_lock:
        _working_model, _catalog_cache, _refusal = None, None, None


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
        """-> (text, complete, finish_reason, usage dict, actual model). Images are PNG bytes.

        Codex picks the plan's default model when none is named; when the ChatGPT account refuses the
        model it picked ("model is not supported when using Codex with a ChatGPT account"), the next
        image-capable models of the account's own catalog (`codex debug models`) are tried in order and
        the one that answers is used from then on."""
        default = not model_name or model_name == label("")
        first = (_working_model or model_name) if default else model_name
        try:
            return self._request_once(first, system, prompt, images, schema)
        except ModelRefused as refused:
            if not default:
                raise SubscriptionError(str(refused)) from refused          # a named model: the person's choice
            tried = [m for m in dict.fromkeys((refused.model, first)) if m and m != label("")]
            newer = [refused.model] if refused.newer else []
            for candidate in self._catalog():
                if candidate in tried:
                    continue
                tried.append(candidate)
                try:
                    result = self._request_once(candidate, system, prompt, images, schema)
                except ModelRefused as again:
                    if again.newer:
                        newer.append(candidate)
                    continue
                _remember(candidate)
                return result
            message = self._all_refused(refused, tried, newer)
            _mark_refused(self.cli, message, "Codex CLI 업데이트 필요" if newer else "모델 거부")
            raise SubscriptionError(message) from refused

    def _version(self) -> str:
        try:
            proc = self._run([self.cli, "--version"], env=cli_env(), cwd=_empty_dir(), capture_output=True, text=True,
                             encoding="utf-8", errors="replace", timeout=60, **_NO_WINDOW)
        except (OSError, subprocess.SubprocessError):
            return ""
        found = re.search(r"\d+\.\d+\.\d+", proc.stdout or "")
        return found.group(0) if found else ""

    def _all_refused(self, refused, tried, newer) -> str:
        """Why no model answered, in the order of what to do: an old CLI (OpenAI said so) first, then the plan."""
        version = self._version()
        cli = f"Codex CLI{f' {version}' if version else ''}"
        others = [path for path in candidates() if os.path.normcase(path) != os.path.normcase(self.cli)]
        where = f"실행한 파일: {self.cli}"
        if others:                     # [설치] (npm) updates npm's copy; another one may be the one that runs
            where += f" · 이 PC의 다른 Codex CLI: {', '.join(others[:3])} — 오래된 것을 지우면 새것을 씁니다"
        listed = ", ".join(tried) or "없음"
        if newer:
            head = (f"설치된 {cli}가 오래되었습니다 — OpenAI가 {', '.join(newer[:3])}{' 등' if len(newer) > 3 else ''}은 "
                    f"새 Codex가 필요하다고 답했고, 이 CLI의 기본 모델({refused.model or '기본값'})은 ChatGPT 계정에서 더 이상 쓰지 않습니다({where}). ")
            return (head + "설정 「AI 판독기」 ①에서 'ChatGPT (Codex CLI)'를 고르고 [설치]를 눌러 최신으로 올린 뒤 "
                    f"[상태 확인] → [연결 시험]을 다시 하십시오(PowerShell: {_update_command(self.cli)}). "
                    f"(시도한 모델: {listed} · 원문: {refused.detail[:200]})")
        return (f"이 ChatGPT 계정이 Codex에서 시도한 모델을 모두 거부했습니다({cli} · 시도: {listed}). "
                "ChatGPT 요금제가 Plus 이상이고 만료되지 않았는지 확인하고(Free·Go는 쓸 수 없는 모델이 많고, "
                "Business·Enterprise·Edu는 관리자가 Codex를 켜야 함), PowerShell에서 `codex`를 실행해 /model 로 "
                "쓸 수 있는 모델을 본 뒤 그 이름을 환경 변수 YEONE_OCR_CHATGPT_MODEL에 넣으십시오. Codex CLI가 오래되었다면 "
                f"[설치]로 최신으로 올리십시오(PowerShell: {_update_command(self.cli)}). (원문: {refused.detail[:200]})")

    def _catalog(self) -> list:
        """The account's image-capable models in Codex's own order (`codex debug models`), then a short
        built-in list of current names (an old CLI prints only old ones, or nothing)."""
        global _catalog_cache
        if _catalog_cache is not None:
            return _catalog_cache
        account = cached_models()                     # what OpenAI says this account serves (Codex's own cache)
        models = []
        try:
            proc = self._run([self.cli, "debug", "models"], env=cli_env(), cwd=_empty_dir(), capture_output=True,
                             text=True, encoding="utf-8", errors="replace", timeout=60, **_NO_WINDOW)
            models = _slugs(json.loads(proc.stdout or "{}"))
        except (OSError, subprocess.SubprocessError, ValueError, TypeError, AttributeError):
            models = []
        # An old CLI lists only the models of its own time, which the account may no longer serve:
        # the account's cached list leads, then the CLI's, then the current names.
        _catalog_cache = list(dict.fromkeys([*account[:8], *models[:6], *FALLBACK_MODELS]))
        return _catalog_cache

    def _request_once(self, model_name, system, prompt, images, schema):
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
                    "-c", "model_instructions_file=" + json.dumps(str(root / "instructions.md"))]
            if model_name and model_name != label(""):         # the label of the default is not a model name
                args += ["-m", model_name]
            for path in paths:
                args += ["-i", path]
            # -i takes every value that follows it: an option must stand between the last image and the
            # "-" (prompt from stdin: no command-line length limit), or Codex takes "-" for an image file.
            args += ["--output-schema", str(root / "schema.json"), "-o", str(last), "-"]
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
            full = None
            if not detail:
                errors = [str(e.get("message") or "") for e in events if e.get("type") == "error"]
                if errors:
                    detail = errors[-1]
                else:
                    full, detail = program_error(proc.stderr, proc.stdout)
            refused = _MODEL_REFUSED.search((detail or "").lower()) or _OLD_CLI.search((detail or "").lower())
            if refused and not _SIGN_IN.search((detail or "").lower()) and not _LIMIT.search((detail or "").lower()):
                slug = re.search(r"'([^']+)' model", detail or "")
                raise ModelRefused(slug.group(1) if slug else (model_name or ""),
                                   f"이 ChatGPT 요금제에서 쓸 수 없는 모델입니다 (원문: {(detail or '')[:240]})",
                                   detail=detail or "", newer=bool(_OLD_CLI.search((detail or "").lower())))
            raise failure("Codex CLI", detail or "", _classify, f"Codex CLI 실행 실패(코드 {proc.returncode}): {detail}", full=full)
        if not text:
            message = next((e.get("item", {}).get("text") for e in reversed(events) if e.get("type") == "item.completed"
                            and e.get("item", {}).get("type") == "agent_message"), "")
            text = (message or "").strip()
        usage = (done or {}).get("usage") or {}
        tokens = {name: int(usage.get(name) or 0) for name in ("input_tokens", "output_tokens")}
        try:
            json.loads(text)
        except ValueError:
            return text, False, "invalid_json", tokens, label(model_name or "")
        return text, True, "completed", tokens, label(model_name or "")
