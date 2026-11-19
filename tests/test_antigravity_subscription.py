"""Gemini 구독을 Antigravity CLI(agy)로: Google이 2026-06-18에 개인 계정의 Gemini CLI를 끝냈다.
agy 는 여기서 실행하지 않는다 — Windows · agy 1.5.9 · Google AI Pro 계정에서 실제로 받은 출력 모양
(`-p … --output-format json --settings … --json-schema …`, docs/AI_REVIEW.md)으로 흉내 낸다."""
import json
import subprocess as sp
from pathlib import Path

import pytest

from yeone_ocr import antigravity_subscription as agy, gemini_subscription, subscriptions
from yeone_ocr.claude_subscription import RequestRejected, SubscriptionError

SCHEMA = {"type": "object", "additionalProperties": False, "required": ["lines"],
          "properties": {"lines": {"type": "array", "items": {"type": "string"}}}}


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    for name in ("YEONE_OCR_AGY_MODEL", "YEONE_OCR_GEMINI_TRANSPORT"):
        monkeypatch.delenv(name, raising=False)
    subscriptions.reset_status_cache(); subscriptions.reset_exhausted()
    yield
    subscriptions.reset_status_cache(); subscriptions.reset_exhausted()


def _done(stdout="", stderr="", code=0):
    return sp.CompletedProcess([], code, stdout, stderr)


def _envelope(**fields):
    """The line agy 1.5.9 printed on Windows (Google AI Pro, one image, --json-schema)."""
    data = {"conversation_id": "f5d3575d", "status": "SUCCESS", "response": "", "duration_seconds": 17.1, "num_turns": 1,
            "usage": {"input_tokens": 12146, "output_tokens": 377, "thinking_tokens": 306, "cache_read_tokens": 6796,
                      "total_tokens": 12523}}
    data.update(fields)
    return json.dumps(data)


# ---- one request ----

def test_request_runs_agy_headless_in_default_mode_with_the_schema_and_images(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "must-not-replace-the-sign-in")
    monkeypatch.setenv("AGY_ADC_AUTH", "1")
    seen = {}

    def run(args, *, input, env, cwd, **kwargs):
        folder = Path(cwd)
        seen.update(args=args, input=input, env=env, files=sorted(p.name for p in folder.iterdir()),
                    image=(folder / "image1.png").read_bytes(),
                    settings=json.loads(Path(args[args.index("--settings") + 1]).read_text(encoding="utf-8")),
                    schema=json.loads(Path(args[args.index("--json-schema") + 1]).read_text(encoding="utf-8")))
        return _done(_envelope(response='{"lines": ["宇宙洪荒"]}\n', structured_output={"lines": ["宇宙洪荒"]}))

    client = agy.SubscriptionClient(cli="agy", runner=run)
    assert client.transport == "antigravity-cli-subscription"
    text, complete, reason, tokens, actual = client.request(
        model_name=agy.label(""), system="SYSTEM TEXT", prompt="PROMPT", images=[b"png-1", b"png-2"], schema=SCHEMA)
    assert (json.loads(text), complete, reason, actual) == ({"lines": ["宇宙洪荒"]}, True, "STOP", "agy-auto")
    assert tokens == {"input_tokens": 12146, "output_tokens": 377}
    args, env = seen["args"], seen["env"]
    assert args[:2] == ["agy", "-p"]
    message = args[2]
    assert message.startswith("SYSTEM TEXT") and "PROMPT" in message and "@image1.png @image2.png" in message
    assert args[args.index("--output-format") + 1] == "json" and "--disable-slash-commands" in args
    # agy 1.5.9: --mode takes only accept-edits/auto/plan and runs "default" in Auto; --settings gives Default.
    assert "--mode" not in args and seen["settings"] == {"agentMode": "", "toolPermission": "request-review", "useG1Credits": False}
    assert "--dangerously-skip-permissions" not in args and "--model" not in args
    assert seen["schema"] == SCHEMA and seen["image"] == b"png-1" and seen["input"] == ""   # stdin: empty, closed
    assert seen["files"] == ["image1.png", "image2.png", "schema.json", "yeone-settings.json"]
    assert "GEMINI_API_KEY" not in env and "AGY_ADC_AUTH" not in env and env["NO_COLOR"] == "1"


def test_a_chosen_model_is_passed_and_an_unknown_choice_never_silently_falls_back():
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        if "--model" in args and args[args.index("--model") + 1] == "gemini-9":
            return _done("", "error: unknown model 'gemini-9'. Available models: gemini-4-argon", 1)
        return _done(_envelope(response='```json\n{"lines": ["天地玄黃"]}\n```'))

    client = agy.SubscriptionClient(cli="agy", runner=run)
    text, complete, _, _, actual = client.request(model_name="gemini-4-argon", system="s", prompt="p", images=[], schema=SCHEMA)
    assert (json.loads(text), complete, actual) == ({"lines": ["天地玄黃"]}, True, "gemini-4-argon")
    assert calls[0][calls[0].index("--model") + 1] == "gemini-4-argon" and "Attached images" not in calls[0][2]
    calls.clear()
    with pytest.raises(SubscriptionError):
        client.request(model_name="gemini-9", system="s", prompt="p", images=[], schema=SCHEMA)
    assert len(calls) == 1 and "--model" in calls[0]


def test_a_long_request_goes_in_a_file_and_a_cut_off_answer_is_incomplete():
    seen = {}

    def run(args, *, cwd, **kwargs):
        seen["args"], seen["request"] = args, (Path(cwd) / "request.md").read_text(encoding="utf-8")
        return _done(_envelope(response='{"lines": [', finished_reason="print_timeout"))

    long_prompt = "字" * 60_000
    _, complete, reason, _, _ = agy.SubscriptionClient(cli="agy", runner=run).request(
        model_name="", system="SYS", prompt=long_prompt, images=[b"x"], schema=SCHEMA)
    assert (complete, reason) == (False, "print_timeout")
    assert "request.md" in seen["args"][2] and len(seen["args"][2]) < 1000
    assert long_prompt in seen["request"] and "@image1.png" in seen["request"] and "SYS" in seen["request"]


def test_prose_is_incomplete_and_a_non_object_schema_is_not_passed():
    seen = {}

    def run(args, **kwargs):
        seen["args"] = args
        return _done(_envelope(response="I cannot read this page."))
    client = agy.SubscriptionClient(cli="agy", runner=run)
    assert client.request(model_name="", system="s", prompt="p", images=[], schema=SCHEMA)[1:3] == (False, "invalid_json")
    client.request(model_name="", system="s", prompt="p", images=[], schema={"type": "array"})
    assert "--json-schema" not in seen["args"]


def test_blank_structured_output_uses_valid_response():
    def run(args, **kwargs):
        return _done(_envelope(response='{"lines": ["宇宙洪荒"]}', structured_output='  '))

    client = agy.SubscriptionClient(cli="agy", runner=run)
    text, complete, reason, _, _ = client.request(model_name="", system="s", prompt="p", images=[], schema=SCHEMA)
    assert (json.loads(text), complete, reason) == ({"lines": ["宇宙洪荒"]}, True, "STOP")


@pytest.mark.parametrize("stdout, stderr, code, kind", [
    ("", 'AGY_ERROR: {"status": "RESOURCE_EXHAUSTED", "code": 429, "retryable": false, "message": "Individual quota reached"}',
     3, "limit"),
    ("", "error: Your AI credits balance is too low to continue.", 3, "limit"),
    ("", "error: authentication required: run agy to sign in", 1, "account"),
    ("", "Eligibility check failed: this account is not eligible", 1, "account"),
    ("", "Error: not signed in. Please sign in.\n    at auth (agy.js:1:1)", 1, "account"),
    ('{"status": "ERROR", "error": {"message": "RESOURCE_EXHAUSTED: quota"}}', "", 3, "limit"),
    ('{"status": "SUCCESS"}', "", 0, "temporary"),                      # SUCCESS without an answer is never one
    ("", 'AGY_ERROR: {"status": "UNAVAILABLE", "code": 503, "retryable": true}', 3, "temporary"),
    ("", "", 0, "temporary"),                                           # nothing on the pipe (old agy on Windows)
])
def test_failures_are_classified(stdout, stderr, code, kind):
    with pytest.raises(SubscriptionError) as caught:
        agy.SubscriptionClient(cli="agy", runner=lambda args, **k: _done(stdout, stderr, code)).request(
            model_name="", system="s", prompt="p", images=[], schema=SCHEMA)
    assert caught.value.kind == kind and "at auth" not in str(caught.value)


def test_a_refused_schema_and_a_timeout():
    def schema(args, **kwargs):
        return _done("", "error: --json-schema: invalid schema: root must be \"type\": \"object\"", 1)
    with pytest.raises(RequestRejected):
        agy.SubscriptionClient(cli="agy", runner=schema).request(model_name="", system="s", prompt="p", images=[], schema=SCHEMA)

    def slow(args, **kwargs):
        raise sp.TimeoutExpired(args, kwargs.get("timeout"))
    with pytest.raises(TimeoutError, match="Antigravity CLI"):
        agy.SubscriptionClient(cli="agy", runner=slow, timeout_seconds=30).request(
            model_name="", system="s", prompt="p", images=[], schema=SCHEMA)


# ---- status: --version, then /quota (answered by agy itself, no model call) ----

def _status(quota=(0, "Quota:\nGemini Models  Weekly Limit Remaining  100%", ""), version=(0, "1.5.9\n", "")):
    calls = []

    def run(args, **kwargs):
        calls.append(args[1:])
        assert kwargs.get("stdin") is sp.DEVNULL
        code, out, err = version if args[1:] == ["--version"] else quota
        assert args[1:] in (["--version"], ["-p", "/quota"])
        return _done(out, err, code)
    return run, calls


def test_status_needs_the_sign_in_and_never_reads_credentials(monkeypatch):
    monkeypatch.setattr(agy, "find_cli", lambda: None)
    state = agy.status(refresh=True, runner=_status()[0])
    assert not state["installed"] and "install.ps1" in state["reason"]
    monkeypatch.setattr(agy, "find_cli", lambda: "agy")
    run, calls = _status()
    state = agy.status(refresh=True, runner=run)
    assert state["usable"] and state["billing"] == "subscription" and "Antigravity 1.5.9" in state["plan"]
    assert calls == [["--version"], ["-p", "/quota"]]
    state = agy.status(refresh=True, runner=_status(quota=(1, "", "error: /quota: authentication required"))[0])
    assert not state["usable"] and state["installed"] and "로그인" in state["reason"]     # not read as a limit
    state = agy.status(refresh=True, runner=_status(quota=(3, "", 'AGY_ERROR: {"status": "RESOURCE_EXHAUSTED"}'))[0])
    assert state["usable"] and state["limits"]                          # signed in; a run rests until the reset
    state = agy.status(refresh=True, runner=_status(version=(2, "", "boom"))[0])
    assert state["runs"] is False and not state["usable"]


def test_missing_agy_names_its_own_path_variable(monkeypatch):
    monkeypatch.setattr(agy, "find_cli", lambda: None)
    reason = agy.status(refresh=True)["reason"]
    assert "YEONE_OCR_AGY_CLI_PATH" in reason and "install.ps1" in reason and "npx" not in reason


def test_find_cli_looks_in_the_install_folder(tmp_path, monkeypatch):
    monkeypatch.delenv("YEONE_OCR_AGY_CLI_PATH", raising=False)
    monkeypatch.setattr(agy.shutil, "which", lambda name: None)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    assert agy.find_cli() is None
    exe = tmp_path / "agy" / "bin" / "agy.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"")
    assert agy.find_cli() == str(exe)


def test_windows_takes_only_the_native_agy_exe(tmp_path, monkeypatch):
    """An agy.cmd on PATH may be the Antigravity desktop app's launcher, which opens a window."""
    monkeypatch.delenv("YEONE_OCR_AGY_CLI_PATH", raising=False)
    monkeypatch.setattr(agy.sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "none"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    monkeypatch.setattr(agy.shutil, "which", lambda name: str(tmp_path / "Antigravity" / "bin" / "agy.cmd"))
    assert agy.find_cli() is None


# ---- the Gemini plan ----

def test_gemini_plan_runs_agy_when_installed_else_gemini_cli(monkeypatch):
    monkeypatch.setattr(agy, "find_cli", lambda: "agy")
    assert subscriptions.module("gemini") is agy and subscriptions.program("gemini") == "Antigravity CLI"
    assert subscriptions.NAMES["gemini"] == "Antigravity"
    monkeypatch.setenv("YEONE_OCR_GEMINI_TRANSPORT", "gemini-cli")
    assert subscriptions.module("gemini") is gemini_subscription and subscriptions.NAMES["gemini"] == "Gemini"
    monkeypatch.delenv("YEONE_OCR_GEMINI_TRANSPORT")
    monkeypatch.setattr(agy, "find_cli", lambda: None)
    monkeypatch.setattr(gemini_subscription, "find_cli", lambda: "gemini")
    assert subscriptions.module("gemini") is gemini_subscription
    assert subscriptions.program("gemini") == "Gemini CLI" and subscriptions.program("claude") == "Claude Code"
    monkeypatch.setattr(gemini_subscription, "find_cli", lambda: None)     # neither: the setup hint is agy's
    assert subscriptions.module("gemini") is agy


def test_the_windows_api_model_setting_is_not_passed_to_agy(monkeypatch):
    """The window's Gemini model (gemini-3.1-pro-preview) is an API model name: agy fails a run on it,
    which took the Gemini plan out of the run."""
    monkeypatch.setattr(agy, "find_cli", lambda: "agy")
    assert not subscriptions.accepts_model("gemini", "gemini-3.1-pro-preview")
    assert subscriptions.model("gemini", None) == "agy-auto"
    monkeypatch.setenv("YEONE_OCR_AGY_MODEL", "gemini-4-argon")
    assert subscriptions.model("gemini", None) == "gemini-4-argon"
    monkeypatch.setenv("YEONE_OCR_GEMINI_TRANSPORT", "gemini-cli")
    assert subscriptions.accepts_model("gemini", "gemini-3.1-pro-preview")   # Gemini CLI knows API model names


def test_a_run_records_the_antigravity_transport_and_the_probe_answers(monkeypatch):
    monkeypatch.setattr(agy, "find_cli", lambda: "agy")
    seen = []

    def factory(plan):
        def run(args, **kwargs):
            seen.append(args)
            return _done(_envelope(response='{"ok": true}', structured_output={"ok": True}))
        return agy.SubscriptionClient(cli="agy", runner=run)

    session = subscriptions.Session("gemini", factory=factory, log=lambda *_: None)
    _, complete, finish, _, _ = session.request(model_name="gemini-3.1-pro-preview", system="s", prompt="p", images=[],
                                                schema=SCHEMA)
    assert complete and finish == "STOP" and session.transport == "antigravity-cli-subscription"
    assert "--model" not in seen[0]                                     # the API model name stayed out
    result = subscriptions.probe("gemini", client_factory=factory)
    assert result["ok"] and result["model"] == "agy-auto"
