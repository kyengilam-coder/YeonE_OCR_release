"""Gemini through Google Antigravity CLI (`agy -p`), with a fake agy: no network, no real sign-in,
no credential is ever read. Flags and output follow the CLI's own changelog (1.5.x): -p, --output-format
json (status, response, structured_output, usage, finished_reason), --json-schema, --mode, AGY_ERROR."""
import json
import os
import subprocess as sp
from pathlib import Path

import pytest

from yeone_ocr import antigravity_subscription as agy
from yeone_ocr import gemini_subscription as gemini
from yeone_ocr import subscriptions
from yeone_ocr.claude_subscription import RequestRejected, SubscriptionError

SCHEMA = {"type": "object", "additionalProperties": False, "required": ["lines"],
          "properties": {"lines": {"type": "array", "items": {"type": "string"}}}}


@pytest.fixture(autouse=True)
def _fresh(tmp_path, monkeypatch):
    subscriptions.reset_status_cache(); subscriptions.reset_exhausted()
    program = tmp_path / "bin" / ("agy.exe" if os.name == "nt" else "agy")
    program.parent.mkdir()
    program.write_text("")
    monkeypatch.setenv("YEONE_OCR_ANTIGRAVITY_CLI_PATH", str(program))
    monkeypatch.setenv("YEONE_OCR_GEMINI_CLI_PATH", str(tmp_path / "no-gemini-cli"))
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("YEONE_OCR_GEMINI_CLI_HOME", str(home))
    for name in ("YEONE_OCR_AI_PLAN", "YEONE_OCR_GEMINI_MODEL", "YEONE_OCR_GEMINI_PROGRAM", "YEONE_OCR_CLAUDE_AUTH"):
        monkeypatch.delenv(name, raising=False)
    yield program
    subscriptions.reset_status_cache(); subscriptions.reset_exhausted()


def _done(stdout="", stderr="", code=0):
    return sp.CompletedProcess([], code, stdout, stderr)


def _envelope(**fields):
    data = {"conversation_id": "c1", "status": "SUCCESS", "response": "", "duration_seconds": 4.2, "num_turns": 1,
            "usage": {"input_tokens": 1500, "output_tokens": 40, "thinking_tokens": 200, "cache_read_tokens": 0,
                      "total_tokens": 1740}}
    data.update(fields)
    return json.dumps(data)


# ---- one request ----

def test_request_runs_agy_headless_read_only_with_the_schema_and_images(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "should-not-reach-the-cli")
    monkeypatch.setenv("AGY_ADC_AUTH", "1")
    seen = {}

    def run(args, *, input, env, cwd, timeout, **kwargs):
        folder = Path(cwd)
        seen.update(args=args, input=input, env=env, files=sorted(p.name for p in folder.iterdir()),
                    image=(folder / "image1.png").read_bytes(),
                    schema=json.loads(Path(args[args.index("--json-schema") + 1]).read_text(encoding="utf-8")))
        return _done(_envelope(response='{"lines": ["宇宙洪荒"]}', structured_output={"lines": ["宇宙洪荒"]}))

    client = agy.SubscriptionClient(runner=run)
    assert client.transport == "antigravity-cli-subscription"
    text, complete, reason, tokens, actual = client.request(
        model_name="antigravity-auto", system="SYSTEM TEXT", prompt="PROMPT", images=[b"png-1", b"png-2"], schema=SCHEMA)
    assert (json.loads(text), complete, reason, actual) == ({"lines": ["宇宙洪荒"]}, True, "STOP", "antigravity-auto")
    assert tokens == {"input_tokens": 1500, "output_tokens": 40}
    args, env = seen["args"], seen["env"]
    message = args[args.index("-p") + 1]
    assert "SYSTEM TEXT" in message and "PROMPT" in message and "@image1.png @image2.png" in message
    assert '"lines"' in message                                             # the schema is in the words as well
    assert args[args.index("--output-format") + 1] == "json"
    assert args[args.index("--mode") + 1] == "default"                     # not Auto: no edits, no commands
    assert "--dangerously-skip-permissions" not in args and "--model" not in args
    assert seen["schema"] == SCHEMA and seen["files"] == ["image1.png", "image2.png", "schema.json"]
    assert seen["image"] == b"png-1" and seen["input"] == ""               # stdin: empty and closed
    assert "GEMINI_API_KEY" not in env and "AGY_ADC_AUTH" not in env and env["NO_COLOR"] == "1"


def test_a_chosen_model_is_passed_and_the_answer_text_is_used_without_structured_output():
    seen = {}

    def run(args, **kwargs):
        seen["args"] = args
        return _done("Loading...\n" + _envelope(response='```json\n{"lines": ["天地玄黃"]}\n```', model="gemini-4-hydrogen"))

    text, complete, reason, _, actual = agy.SubscriptionClient(runner=run).request(
        model_name="gemini-4-hydrogen", system="s", prompt="p", images=[], schema=SCHEMA)
    assert (json.loads(text), complete, reason, actual) == ({"lines": ["天地玄黃"]}, True, "STOP", "gemini-4-hydrogen")
    assert seen["args"][seen["args"].index("--model") + 1] == "gemini-4-hydrogen"
    assert "Attached images" not in seen["args"][seen["args"].index("-p") + 1]


def test_a_long_request_goes_in_a_file_and_a_cut_off_answer_is_incomplete():
    seen = {}

    def run(args, *, cwd, **kwargs):
        seen["args"], seen["request"] = args, (Path(cwd) / "request.md").read_text(encoding="utf-8")
        return _done(_envelope(response='{"lines": [', finished_reason="print_timeout"))

    long_prompt = "字" * 60_000
    text, complete, reason, _, _ = agy.SubscriptionClient(runner=run).request(
        model_name="", system="SYS", prompt=long_prompt, images=[b"x"], schema=SCHEMA)
    assert (complete, reason) == (False, "print_timeout")
    message = seen["args"][seen["args"].index("-p") + 1]
    assert "request.md" in message and len(message) < 1000
    assert long_prompt in seen["request"] and "@image1.png" in seen["request"] and "SYS" in seen["request"]


def test_a_prose_answer_is_not_complete():
    def run(args, **kwargs):
        return _done(_envelope(response="I cannot read this page."))
    _, complete, reason, _, _ = agy.SubscriptionClient(runner=run).request(
        model_name="", system="s", prompt="p", images=[], schema=SCHEMA)
    assert (complete, reason) == (False, "invalid_json")


def test_a_schema_without_an_object_root_is_not_passed_to_agy():
    seen = {}

    def run(args, **kwargs):
        seen["args"] = args
        return _done(_envelope(response='["a"]'))
    agy.SubscriptionClient(runner=run).request(model_name="", system="s", prompt="p", images=[],
                                                schema={"type": "array", "items": {"type": "string"}})
    assert "--json-schema" not in seen["args"]


@pytest.mark.parametrize("stderr, code, kind, words", [
    ('AGY_ERROR: {"status": "RESOURCE_EXHAUSTED", "code": 429, "retryable": false, "message": "Individual quota reached"}',
     3, "limit", "사용량 한도"),
    ("error: Your AI credits balance is too low to continue.", 3, "limit", "사용량 한도"),
    ("error: authentication required: run agy to sign in", 1, "account", "로그인"),
    ("Eligibility check failed: this account is not eligible", 1, "account", "로그인"),
    ("error: unknown model 'gemini-9'. Available models: gemini-4-hydrogen", 1, "account", "YEONE_OCR_GEMINI_MODEL"),
    ('AGY_ERROR: {"status": "UNAVAILABLE", "code": 503, "retryable": true}', 3, "temporary", "실행 실패"),
])
def test_failures_are_explained_and_sorted(stderr, code, kind, words):
    def run(args, **kwargs):
        return _done("", stderr, code)
    with pytest.raises(SubscriptionError, match=words) as caught:
        agy.SubscriptionClient(runner=run).request(model_name="gemini-9", system="s", prompt="p", images=[], schema=SCHEMA)
    assert caught.value.kind == kind


def test_an_error_envelope_an_empty_run_and_a_refused_schema():
    def failed(args, **kwargs):
        return _done(_envelope(status="ERROR", response="partial", error={"message": "RESOURCE_EXHAUSTED: quota"}), "", 3)
    with pytest.raises(SubscriptionError, match="사용량 한도") as caught:
        agy.SubscriptionClient(runner=failed).request(model_name="", system="s", prompt="p", images=[], schema=SCHEMA)
    assert caught.value.kind == "limit"

    def silent(args, **kwargs):
        return _done("", "", 0)                                     # old agy on Windows: nothing on a pipe
    with pytest.raises(SubscriptionError, match="최신") as caught:
        agy.SubscriptionClient(runner=silent).request(model_name="", system="s", prompt="p", images=[], schema=SCHEMA)
    assert caught.value.kind == "temporary"

    def schema(args, **kwargs):
        return _done("", "error: --json-schema: invalid schema: root must be \"type\": \"object\"", 1)
    with pytest.raises(RequestRejected):
        agy.SubscriptionClient(runner=schema).request(model_name="", system="s", prompt="p", images=[], schema=SCHEMA)


def test_a_timeout_is_reported():
    def slow(args, **kwargs):
        raise sp.TimeoutExpired(args, kwargs.get("timeout"))
    with pytest.raises(TimeoutError, match="Antigravity CLI"):
        agy.SubscriptionClient(runner=slow, timeout_seconds=30).request(model_name="", system="s", prompt="p", images=[], schema=SCHEMA)


# ---- status: --version, then /quota (answered by the CLI, no model call) ----

def _status_runner(quota=(0, "Gemini Pro quota: 80% left", ""), version=(0, "1.5.9\n", "")):
    calls = []

    def run(args, **kwargs):
        calls.append(args[1:])
        assert kwargs.get("stdin") is sp.DEVNULL
        code, out, err = version if args[1:] == ["--version"] else quota
        if args[1:] not in (["--version"], ["-p", "/quota"]):
            raise AssertionError(args)
        return _done(out, err, code)
    return run, calls


def test_status_is_usable_when_signed_in():
    run, calls = _status_runner()
    state = agy.status(refresh=True, runner=run)
    assert state["usable"] and state["logged_in"] and state["billing"] == "subscription"
    assert state["program"] == "antigravity" and "Antigravity CLI 1.5.9" in state["plan"]
    assert calls == [["--version"], ["-p", "/quota"]]


def test_status_needs_the_sign_in_and_says_why():
    run, _ = _status_runner(quota=(1, "", "error: authentication required. Run `agy` in a terminal to sign in."))
    state = agy.status(refresh=True, runner=run)
    assert not state["usable"] and state["installed"] and not state["logged_in"] and "agy" in state["reason"]
    run, _ = _status_runner(quota=(1, "", "error: /quota: authentication required"))
    state = agy.status(refresh=True, runner=run)
    assert not state["usable"] and "로그인" in state["reason"]          # not read as a quota limit
    run, _ = _status_runner(quota=(3, "", 'AGY_ERROR: {"status": "RESOURCE_EXHAUSTED", "code": 429}'))
    state = agy.status(refresh=True, runner=run)
    assert state["usable"] and state["limits"]                     # signed in; the run rests until the reset
    run, _ = _status_runner(version=(1, "", "Access is denied."))
    state = agy.status(refresh=True, runner=run)
    assert state["runs"] is False and "Access is denied" in state["reason"]


def test_status_without_agy_says_where_it_looked_and_how_to_install(monkeypatch, tmp_path):
    monkeypatch.setenv("YEONE_OCR_ANTIGRAVITY_CLI_PATH", str(tmp_path / "nowhere" / "agy"))
    state = agy.status(refresh=True, runner=lambda *a, **k: pytest.fail("nothing to run"))
    assert not state["installed"] and "찾아봤습니다" in state["reason"] and "install.ps1" in state["reason"]
    assert "YEONE_OCR_ANTIGRAVITY_CLI_PATH" in state["reason"]


def test_agy_is_found_where_the_installer_puts_it(monkeypatch, tmp_path):
    monkeypatch.delenv("YEONE_OCR_ANTIGRAVITY_CLI_PATH")
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    if os.name == "nt":
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
        program = tmp_path / "local" / "agy" / "bin" / "agy.exe"
    else:
        monkeypatch.setenv("HOME", str(tmp_path))
        program = tmp_path / ".local" / "bin" / "agy"
    assert agy.find_cli() is None
    program.parent.mkdir(parents=True)
    program.write_text("")
    assert agy.find_cli() == str(program)


# ---- the Gemini plan picks the program ----

def _both(agy_quota_code=0, gemini_settings=None):
    def run(args, **kwargs):
        if Path(args[0]).stem == "agy":
            if args[1:] == ["--version"]:
                return _done("1.5.9")
            return _done("", "" if agy_quota_code == 0 else "error: authentication required", agy_quota_code)
        return _done("0.68.0")                                      # gemini --version
    return run


def test_the_gemini_plan_uses_antigravity_when_it_is_signed_in(tmp_path, monkeypatch):
    state = gemini.status(refresh=True, runner=_both())
    assert state["usable"] and state["program"] == "antigravity"
    assert gemini.program() == "antigravity" and subscriptions.program("gemini") == "Antigravity CLI"
    assert subscriptions.model("gemini") == "antigravity-auto"
    assert isinstance(gemini.SubscriptionClient(), agy.SubscriptionClient)
    assert gemini.find_cli() == agy.find_cli()


def test_a_signed_in_gemini_cli_is_used_when_antigravity_is_not(tmp_path, monkeypatch):
    cli = tmp_path / "gemini"
    cli.write_text("")
    monkeypatch.setenv("YEONE_OCR_GEMINI_CLI_PATH", str(cli))
    home = Path(os.environ["YEONE_OCR_GEMINI_CLI_HOME"])
    (home / ".gemini").mkdir()
    (home / ".gemini" / "settings.json").write_text(json.dumps({"security": {"auth": {"selectedType": "oauth-personal"}}}))
    state = gemini.status(refresh=True, runner=_both(agy_quota_code=1))
    assert state["usable"] and state["program"] == "gemini-cli" and "2026-06-18" in state["note"]
    assert gemini.program() == "gemini-cli" and subscriptions.model("gemini") == "gemini-cli-auto"
    assert isinstance(gemini.SubscriptionClient(), gemini.GeminiCliClient)
    monkeypatch.setenv("YEONE_OCR_GEMINI_PROGRAM", "antigravity")      # the person's choice wins
    gemini.reset_status_cache()
    state = gemini.status(refresh=True, runner=_both(agy_quota_code=1))
    assert not state["usable"] and state["program"] == "antigravity"


def test_neither_signed_in_reports_antigravity_and_the_gemini_cli_reason(tmp_path, monkeypatch):
    cli = tmp_path / "gemini"
    cli.write_text("")
    monkeypatch.setenv("YEONE_OCR_GEMINI_CLI_PATH", str(cli))
    state = gemini.status(refresh=True, runner=_both(agy_quota_code=1))
    assert not state["usable"] and state["program"] == "antigravity"
    assert "Antigravity CLI에 로그인" in state["reason"] and "Gemini CLI도 쓸 수 없음" in state["reason"]


def test_gemini_cli_refusing_a_personal_account_points_to_antigravity():
    def retired(args, **kwargs):
        return _done("", "This service is no longer available for your account. Please use Antigravity CLI.", 1)
    with pytest.raises(SubscriptionError, match="Antigravity CLI") as caught:
        gemini.GeminiCliClient(cli="gemini", runner=retired).request(
            model_name="gemini-cli-auto", system="s", prompt="p", images=[], schema=SCHEMA)
    assert caught.value.kind == "account"                          # out for this run, not retried


def test_a_run_records_the_antigravity_transport_and_the_probe_answers():
    gemini.status(refresh=True, runner=_both())
    seen = []

    def factory(plan):
        def run(args, **kwargs):
            seen.append(args)
            return _done(_envelope(response='{"ok": true}', structured_output={"ok": True}))
        return agy.SubscriptionClient(runner=run)

    session = subscriptions.Session("gemini", factory=factory, log=lambda *_: None)
    text, complete, finish, _, actual = session.request(model_name=subscriptions.model("gemini"), system="s",
                                                        prompt="p", images=[], schema=SCHEMA)
    assert complete and finish == "STOP" and session.transport == "antigravity-cli-subscription"
    assert "--model" not in seen[0]
    result = subscriptions.probe("gemini", client_factory=factory)
    assert result["ok"] and result["model"] == "antigravity-auto"
    assert "Antigravity CLI 실행" in subscriptions.note("gemini")
