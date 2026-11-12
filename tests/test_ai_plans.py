"""ChatGPT (Codex CLI) and Gemini (Gemini CLI) subscription transports and the plan choice, with fake CLIs:
no network, no real sign-in, and no credential file is ever read."""
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

from yeone_ocr import chatgpt_subscription as codex
from yeone_ocr import claude_subscription as claude
from yeone_ocr import comparison_ai, subscriptions
from yeone_ocr import gemini_subscription as gemini
from yeone_ocr.claude_subscription import SubscriptionError

SCHEMA = {"type": "object", "additionalProperties": False, "required": ["lines"],
          "properties": {"lines": {"type": "array", "items": {"type": "string"}}}}


@pytest.fixture(autouse=True)
def _fresh(tmp_path, monkeypatch):
    subscriptions.reset_status_cache(); subscriptions.reset_exhausted()
    codex.reset_model_choice(); claude._npm_prefix_cache.clear()
    for name, binary in (("CODEX", "codex"), ("GEMINI", "gemini"), ("CLAUDE", "claude")):
        path = tmp_path / "bin" / (binary + (".exe" if os.name == "nt" else ""))
        path.parent.mkdir(exist_ok=True)
        path.write_text("")
        monkeypatch.setenv(f"YEONE_OCR_{name}_CLI_PATH", str(path))
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("YEONE_OCR_GEMINI_CLI_HOME", str(home))
    for name in ("YEONE_OCR_AI_PLAN", "YEONE_OCR_CLAUDE_AUTH", "YEONE_OCR_CHATGPT_MODEL", "YEONE_OCR_GEMINI_MODEL",
                 "YEONE_OCR_CHATGPT_EFFORT", "YEONE_OCR_CODEX_HOME"):
        monkeypatch.delenv(name, raising=False)
    yield home
    subscriptions.reset_status_cache(); subscriptions.reset_exhausted()


def _ok(**kwargs):
    return SimpleNamespace(**{"returncode": 0, "stdout": "", "stderr": "", **kwargs})


# ---- ChatGPT: codex exec ----

def _codex_status(text, code=0):
    def run(args, **kwargs):
        assert args[1:] == ["login", "status"]
        return SimpleNamespace(stdout="", stderr=text, returncode=code)
    return run


def test_codex_status_needs_the_chatgpt_sign_in():
    assert codex.status(refresh=True, runner=_codex_status("Logged in using ChatGPT"))["usable"]
    state = codex.status(refresh=True, runner=_codex_status("Logged in using an API key - sk-***"))
    assert not state["usable"] and state["billing"] == "other" and "Sign in with ChatGPT" in state["reason"]
    state = codex.status(refresh=True, runner=_codex_status("Not logged in", 1))
    assert not state["usable"] and "codex login" in state["reason"]


def test_codex_request_runs_an_isolated_exec_and_returns_the_last_message(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-should-not-reach-the-cli")
    monkeypatch.setenv("CODEX_API_KEY", "should-not-reach-the-cli")
    seen = {}

    def run(args, *, input, env, cwd, **kwargs):
        seen.update(args=args, input=input, env=env, cwd=cwd)
        folder = Path(cwd)
        seen["schema"] = json.loads(Path(args[args.index("--output-schema") + 1]).read_text(encoding="utf-8"))
        seen["instructions"] = (folder / "instructions.md").read_text(encoding="utf-8")
        seen["images"] = [Path(args[i + 1]).read_bytes() for i, a in enumerate(args) if a == "-i"]
        Path(args[args.index("-o") + 1]).write_text('{"lines": ["天地玄黃"]}', encoding="utf-8")
        events = [{"type": "thread.started", "thread_id": "t"}, {"type": "turn.started"},
                  {"type": "error", "message": "Reconnecting... 1/5"},
                  {"type": "item.completed", "item": {"type": "agent_message", "text": '{"lines": ["天地玄黃"]}'}},
                  {"type": "turn.completed", "usage": {"input_tokens": 900, "cached_input_tokens": 0, "output_tokens": 12}}]
        return _ok(stdout="\n".join(json.dumps(e) for e in events))

    client = codex.SubscriptionClient(runner=run)
    text, complete, reason, tokens, actual = client.request(
        model_name=subscriptions.model("chatgpt"), system="SYSTEM TEXT", prompt="PROMPT", images=[b"png-1", b"png-2"],
        schema=SCHEMA)
    assert (json.loads(text), complete, reason, actual) == ({"lines": ["天地玄黃"]}, True, "completed", "codex-default")
    assert tokens == {"input_tokens": 900, "output_tokens": 12}
    args = seen["args"]
    assert args[1:3] == ["exec", "--json"] and args[-1] == "-"            # prompt through stdin
    for flag in ("--ephemeral", "--ignore-user-config", "--ignore-rules", "--skip-git-repo-check"):
        assert flag in args
    assert args[args.index("-s") + 1] == "read-only"
    assert 'approval_policy="never"' in args and 'web_search="disabled"' in args
    assert "-m" not in args                                               # the plan's own default model
    assert seen["input"] == "PROMPT" and seen["instructions"] == "SYSTEM TEXT" and seen["schema"] == SCHEMA
    assert seen["images"] == [b"png-1", b"png-2"]
    assert "OPENAI_API_KEY" not in seen["env"] and "CODEX_API_KEY" not in seen["env"]
    assert not Path(seen["cwd"]).exists()                                 # the request folder is removed


def test_codex_named_model_and_plan_limit_error():
    seen = []

    def run(args, **kwargs):
        seen.append(args)
        events = [{"type": "turn.started"}, {"type": "turn.failed", "error": {"message": "You've hit your usage limit."}}]
        return SimpleNamespace(returncode=1, stdout="\n".join(json.dumps(e) for e in events), stderr="")
    with pytest.raises(SubscriptionError, match="사용량 한도"):
        codex.SubscriptionClient(runner=run).request(model_name="gpt-5.5", system="s", prompt="p", images=[], schema=SCHEMA)
    assert seen[0][seen[0].index("-m") + 1] == "gpt-5.5"


def test_codex_missing_cli_is_explained(monkeypatch, tmp_path):
    monkeypatch.setenv("YEONE_OCR_CODEX_CLI_PATH", str(tmp_path / "nowhere" / "codex"))
    with pytest.raises(SubscriptionError, match="npm install -g @openai/codex"):
        codex.SubscriptionClient()
    assert codex.status(refresh=True)["installed"] is False


# ---- Gemini: gemini -p ----

def _settings(home, auth):
    folder = home / ".gemini"
    folder.mkdir(exist_ok=True)
    (folder / "settings.json").write_text(json.dumps({"security": {"auth": {"selectedType": auth}}}), encoding="utf-8")


def _version(args, **kwargs):
    assert args[1:] == ["--version"]
    return _ok(stdout="0.68.0")


def test_gemini_status_reads_only_the_chosen_sign_in_method(_fresh):
    home = _fresh
    (home / ".gemini").mkdir()
    (home / ".gemini" / "oauth_creds.json").write_text("{not json: never read}", encoding="utf-8")
    state = gemini.status(refresh=True, runner=_version)
    assert not state["usable"] and "Login with Google" in state["reason"]
    _settings(home, "gemini-api-key")
    state = gemini.status(refresh=True, runner=_version)
    assert not state["usable"] and state["billing"] == "other"
    _settings(home, "oauth-personal")
    assert gemini.status(refresh=True, runner=_version)["usable"]
    real_read = Path.read_text

    def guarded(self, *args, **kwargs):
        assert self.name != "oauth_creds.json", "credential file read"
        return real_read(self, *args, **kwargs)
    with mock.patch.object(Path, "read_text", guarded):
        gemini.status(refresh=True, runner=_version)


def test_gemini_request_sends_images_as_file_references_and_checks_json(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "should-not-reach-the-cli")
    monkeypatch.setenv("GOOGLE_API_KEY", "should-not-reach-the-cli")
    seen = {}

    def run(args, *, input, env, cwd, **kwargs):
        folder = Path(cwd)
        seen.update(args=args, input=input, env=env, files=sorted(p.name for p in folder.iterdir()),
                    system=Path(env["GEMINI_SYSTEM_MD"]).read_text(encoding="utf-8"),
                    image=(folder / "image1.png").read_bytes())
        out = {"response": '```json\n{"lines": ["宇宙洪荒"]}\n```',
               "stats": {"models": {"gemini-3-pro": {"tokens": {"prompt": 1200, "candidates": 30, "total": 1230}}}}}
        return _ok(stdout=json.dumps(out))

    client = gemini.SubscriptionClient(runner=run)
    text, complete, reason, tokens, actual = client.request(
        model_name=subscriptions.model("gemini"), system="SYSTEM TEXT", prompt="PROMPT", images=[b"png-1"], schema=SCHEMA)
    assert (json.loads(text), complete, reason, actual) == ({"lines": ["宇宙洪荒"]}, True, "STOP", "gemini-3-pro")
    assert tokens == {"input_tokens": 1200, "output_tokens": 30}
    args, env = seen["args"], seen["env"]
    assert args[args.index("-o") + 1] == "json" and args[args.index("--approval-mode") + 1] == "plan"
    assert args[args.index("-e") + 1] == "none" and args[args.index("--allowed-mcp-server-names") + 1] == "none"
    assert "-m" not in args                                               # the CLI's own model choice
    assert "@image1.png" in seen["input"] and "PROMPT" in seen["input"] and '"lines"' in seen["input"]
    assert seen["files"] == ["image1.png", "system.md"] and seen["image"] == b"png-1"
    assert seen["system"] == "SYSTEM TEXT"
    assert "GEMINI_API_KEY" not in env and "GOOGLE_API_KEY" not in env and env["GOOGLE_GENAI_USE_GCA"] == "true"


def test_gemini_errors_are_explained():
    def auth(args, **kwargs):
        return SimpleNamespace(returncode=41, stdout="", stderr="Manual authorization is required but the current session is non-interactive.")
    with pytest.raises(SubscriptionError, match="Login with Google"):
        gemini.SubscriptionClient(runner=auth).request(model_name="gemini-cli-auto", system="s", prompt="p", images=[], schema=SCHEMA)

    def quota(args, **kwargs):
        return SimpleNamespace(returncode=1, stderr="", stdout=json.dumps({"error": {"type": "Error", "message": "RESOURCE_EXHAUSTED: quota"}}))
    with pytest.raises(SubscriptionError, match="사용량 한도"):
        gemini.SubscriptionClient(runner=quota).request(model_name="gemini-2.5-pro", system="s", prompt="p", images=[], schema=SCHEMA)

    def prose(args, **kwargs):
        return _ok(stdout=json.dumps({"response": "I cannot read this page."}))
    _, complete, reason, _, _ = gemini.SubscriptionClient(runner=prose).request(
        model_name="gemini-cli-auto", system="s", prompt="p", images=[], schema=SCHEMA)
    assert (complete, reason) == (False, "invalid_json")


# ---- plan choice ----

def _states(**usable):
    def fake(plan):
        return lambda refresh=False, runner=None: {"usable": usable.get(plan, False), "installed": True,
                                                   "plan": "p", "reason": f"{plan} 로그인 필요"}
    return [mock.patch.object(subscriptions.module(plan), "status", side_effect=fake(plan)) for plan in subscriptions.PLANS]


def _with(patches):
    from contextlib import ExitStack
    stack = ExitStack()
    for patch in patches:
        stack.enter_context(patch)
    return stack


def test_auto_takes_claude_then_chatgpt_then_gemini():
    with _with(_states(chatgpt=True, gemini=True)):
        assert subscriptions.pick()[0] == "chatgpt"
        assert subscriptions.pick("gemini")[0] == "gemini"
        assert subscriptions.pick("claude")[0] == "chatgpt"                   # a named plan is the first choice, not the only one
    with _with(_states(claude=True, chatgpt=True, gemini=True)):
        assert subscriptions.pick()[0] == "claude"
        assert subscriptions.pick(prefer="gemini")[0] == "gemini"
    with _with(_states()):
        plan, reason = subscriptions.pick()
        assert plan is None and "gemini 로그인 필요" in reason


def test_choose_respects_api_mode_keys_and_the_provider_plan(monkeypatch):
    with _with(_states(claude=True, chatgpt=True, gemini=True)):
        assert subscriptions.choose("api", "openai", "")[0] is None
        assert subscriptions.choose("auto", "openai", "sk-real-key")[0] is None              # a key is the user's choice
        plan, message = subscriptions.choose("auto", "openai", "")
        assert plan == "chatgpt" and "Codex CLI" in message and "API 요금 없음" in message
        assert subscriptions.choose("subscription", "gemini", "key")[0] == "claude"         # fixed order
        assert subscriptions.choose("subscription", "gemini", "key", plan="gemini")[0] == "gemini"
        monkeypatch.setenv("YEONE_OCR_AI_PLAN", "chatgpt")
        assert subscriptions.choose("subscription", "anthropic", "")[0] == "chatgpt"


def test_review_client_follows_the_subscription_key():
    settings = SimpleNamespace(provider="openai", model="codex-default")
    client = comparison_ai.create_client(settings, subscriptions.KEYS["chatgpt"])
    assert isinstance(client, codex.SubscriptionClient) and client.transport == codex.TRANSPORT
    client = comparison_ai.create_client(SimpleNamespace(provider="gemini", model="gemini-cli-auto"), subscriptions.KEYS["gemini"])
    assert isinstance(client, gemini.SubscriptionClient)
    assert isinstance(comparison_ai.create_client(SimpleNamespace(provider="anthropic", model="opus"), claude.SUBSCRIPTION_KEY),
                      claude.SubscriptionClient)
    with pytest.raises(comparison_ai.AIReviewError, match="ChatGPT"):
        comparison_ai.create_client(SimpleNamespace(provider="gemini", model="x"), subscriptions.KEYS["chatgpt"])


def test_review_request_records_the_plan_transport_and_stops_on_plan_errors():
    class Fake:
        subscription = True
        transport = gemini.TRANSPORT

        def __init__(self, error=None):
            self.error, self.seen = error, {}

        def request(self, **kwargs):
            self.seen.update(kwargs)
            if self.error:
                raise SubscriptionError(self.error)
            return '{"items": []}', True, "STOP", {"input_tokens": 5, "output_tokens": 2}, "gemini-3-pro"
    settings = SimpleNamespace(provider="gemini", model="gemini-cli-auto", max_output_tokens=4096, timeout_seconds=60)
    fake = Fake()
    text, complete, metadata = comparison_ai._request(fake, settings, "PROMPT", [b"img"])
    assert (text, complete) == ('{"items": []}', True)
    assert metadata["transport"] == "gemini-cli-subscription" and metadata["actual_model"] == "gemini-3-pro"
    assert metadata["usage"] == {"input_tokens": 5, "output_tokens": 2}
    assert fake.seen["system"] == comparison_ai.SYSTEM and fake.seen["images"] == [b"img"]
    with pytest.raises(comparison_ai.AIReviewError) as caught:
        comparison_ai._request(Fake("Google 계정의 Gemini 사용량 한도에 도달했습니다."), settings, "PROMPT", [])
    assert caught.value.fatal is True


def test_model_names_belong_to_their_plan():
    assert subscriptions.accepts_model("chatgpt", "gpt-5.5") and not subscriptions.accepts_model("chatgpt", "opus")
    assert subscriptions.accepts_model("gemini", "gemini-3-pro") and not subscriptions.accepts_model("gemini", "gpt-5.5")
    assert subscriptions.accepts_model("claude", "opus") and not subscriptions.accepts_model("claude", "")
    assert subscriptions.model("claude") == "opus"
    assert (subscriptions.model("chatgpt"), subscriptions.model("gemini")) == ("codex-default", "gemini-cli-auto")


def test_plans_command_lists_each_plan(capsys):
    from yeone_ocr import cli
    with _with(_states(gemini=True)):
        assert cli.main(["plans"]) == 0
    out = capsys.readouterr().out
    assert "Gemini" in out and "사용 가능" in out and "자동 선택" in out


class _Plan:
    subscription = True

    def __init__(self, name, error=None):
        self.name, self.error, self.models = name, error, []

    def request(self, *, model_name, **kwargs):
        self.models.append(model_name)
        if self.error:
            raise SubscriptionError(self.error)
        return '{"lines": []}', True, "done", {}, self.name


def test_session_moves_to_the_next_plan_then_the_api_key_then_stops():
    plans = {"claude": _Plan("claude", "사용량 한도에 도달했습니다."), "chatgpt": _Plan("chatgpt", "로그인이 만료되었습니다."),
             "gemini": _Plan("gemini")}
    logs = []
    with _with(_states(claude=True, chatgpt=True, gemini=True)):
        session = subscriptions.Session("claude", api=None, log=logs.append, factory=plans.__getitem__)
        out = session.request(model_name="opus", system="s", prompt="p", images=[], schema=SCHEMA)
        assert out[-1] == "gemini" and session.plan == "gemini" and session.transport == gemini.TRANSPORT
        assert plans["claude"].models == ["opus"] and plans["chatgpt"].models == ["codex-default"]
        assert plans["gemini"].models == ["gemini-cli-auto"]
        assert set(subscriptions.exhausted()) == {"claude", "chatgpt"}
        assert subscriptions.pick()[0] == "gemini"                            # other callers skip the spent plans
        assert len(logs) == 2 and "Gemini 구독으로 넘어갑니다" in logs[-1]

        plans["gemini"].error = "사용량 한도에 도달했습니다."
        api = mock.Mock(provider="openai", model="gpt-x", left=2, transport="openai-api-key")
        api.request.return_value = ('{"lines": []}', True, "completed", {}, "gpt-x")
        session.api = api
        assert session.request(model_name="opus", system="s", prompt="p", images=[], schema=SCHEMA)[-1] == "gpt-x"
        assert session.plan is None and session.transport == "openai-api-key" and "API 키(openai" in logs[-1]
        assert session.request(model_name="opus", system="s", prompt="p", images=[], schema=SCHEMA)[-1] == "gpt-x"
        assert api.request.call_count == 2                                    # no plan is retried once all are spent

    subscriptions.reset_exhausted()
    with _with(_states(claude=True)):
        session = subscriptions.Session("claude", api=None, log=logs.append,
                                        factory={"claude": _Plan("claude", "사용량 한도")}.__getitem__)
        with pytest.raises(SubscriptionError, match="API 키도 없습니다"):
            session.request(model_name="opus", system="s", prompt="p", images=[], schema=SCHEMA)
        assert not session.available


def test_api_transport_counts_its_calls_and_uses_the_given_system_prompt(monkeypatch):
    sent = []

    class Responses:
        def create(self, **kwargs):
            sent.append(kwargs)
            return SimpleNamespace(output_text='{"lines": ["甲"]}', output=[], status="completed",
                                   usage=SimpleNamespace(input_tokens=10, output_tokens=3), model="gpt-x", id="r1")
    monkeypatch.setattr(comparison_ai, "create_client", lambda settings, key: SimpleNamespace(responses=Responses()))
    api = comparison_ai.ApiTransport("openai", "gpt-x", "sk-test", 1)
    text, complete, reason, tokens, actual = api.request(model_name="ignored", system="PAGE SYSTEM", prompt="p",
                                                         images=[b"png"], schema=SCHEMA)
    assert (json.loads(text), complete, reason, tokens, actual) == ({"lines": ["甲"]}, True, "completed",
                                                                     {"input_tokens": 10, "output_tokens": 3}, "gpt-x")
    assert sent[0]["instructions"] == "PAGE SYSTEM" and sent[0]["model"] == "gpt-x"
    with pytest.raises(SubscriptionError, match="호출 상한"):
        api.request(model_name="x", system="s", prompt="p", images=[], schema=SCHEMA)
    assert len(sent) == 1


# ---- [다음 구독으로 넘기기] ----

class _Slow:
    """Answers through the session's own runner with a child process that would take 60 s."""
    subscription = True

    def __init__(self, session):
        self.session, self.ran = session, []

    def request(self, **kwargs):
        import sys as _sys
        proc = self.session._run([_sys.executable, "-c", "import time; time.sleep(60)"], input="prompt", text=True,
                                 timeout=120)
        self.ran.append(proc.returncode)
        raise SubscriptionError(f"CLI 실행 실패(코드 {proc.returncode})")


def test_switch_stops_the_running_cli_and_sends_the_request_to_the_chosen_plan():
    import threading
    import time
    logs, states = [], []
    with _with(_states(claude=True, chatgpt=True, gemini=True)):
        session = subscriptions.Session("claude", log=logs.append)
        slow, gemini_plan = _Slow(session), _Plan("gemini")
        session._factory = {"claude": slow, "gemini": gemini_plan, "chatgpt": _Plan("chatgpt")}.__getitem__
        subscriptions.ON_CHANGE = states.append
        subscriptions.activate(session)
        try:
            out = {}
            worker = threading.Thread(target=lambda: out.setdefault("r", session.request(
                model_name="opus", system="s", prompt="p", images=[], schema=SCHEMA)))
            started = time.monotonic()
            worker.start()
            for _ in range(200):
                if session._procs:
                    break
                time.sleep(0.02)
            assert session._procs, "the CLI never started"
            assert states[0]["plan"] == "claude" and states[0]["next"] == "chatgpt"
            message = subscriptions.switch("gemini")
            worker.join(15)
            assert not worker.is_alive() and time.monotonic() - started < 15     # killed, not waited for
            assert out["r"][-1] == "gemini" and gemini_plan.models == ["gemini-cli-auto"]
            assert slow.ran and slow.ran[0] != 0
            assert "Claude 구독을 멈추고 Gemini 구독에 연결합니다" in message and logs[-1].endswith(message)
            assert session.switches[-1] == {"from": "claude", "to": "gemini", "reason": "사용자가 넘김", "by": "user"}
            assert states[-1]["plan"] == "gemini" and "claude" in subscriptions.exhausted()
        finally:
            subscriptions.ON_CHANGE = None
            subscriptions.deactivate()
    assert subscriptions.switch() == "지금 구독 계정으로 연결된 실행이 없습니다."


def test_switch_without_a_choice_follows_the_order_and_ends_at_the_api_key():
    api = mock.Mock(provider="openai", model="gpt-x", left=3, transport="openai-api-key")
    with _with(_states(claude=True, chatgpt=True, gemini=False)):
        session = subscriptions.Session("claude", api=api, log=lambda _m: None, factory=lambda plan: _Plan(plan))
        assert session.state()["targets"] == ["chatgpt", "gemini", "api"]
        assert "ChatGPT 구독에 연결" in session.switch()                       # auto: the next signed-in plan
        assert session.plan == "chatgpt" and session.state()["next"] == "api"
        message = session.switch("gemini")                                   # not signed in: falls back to the order
        assert "Gemini 구독은 쓸 수 없어" in message and session.plan is None and "API 키(openai" in message
        assert session.switch() == "넘길 구독 계정이 없습니다. 지금 연결을 그대로 씁니다."
        assert "Claude 구독에 연결" in session.switch("claude")             # chosen by hand: used again
        assert session.plan == "claude"


def test_switch_with_nowhere_to_go_keeps_the_plan_in_use():
    with _with(_states(claude=True)):
        session = subscriptions.Session("claude", log=lambda _m: None, factory=lambda plan: _Plan(plan))
        assert "그대로 씁니다" in session.switch() and session.plan == "claude"
        assert "claude" not in subscriptions.exhausted()
        assert session.state()["next"] is None


# ---- refused requests are not account problems ----

def test_a_refused_schema_fails_that_request_alone_and_keeps_the_plan():
    from yeone_ocr.claude_subscription import RequestRejected, failure

    def classify(_text):
        return None
    error = failure("Claude Code", "schema is not a valid JSON Schema: data/properties/id/enum must NOT have duplicate items",
                    classify, "fallback")
    assert isinstance(error, RequestRejected) and "원문: schema is not a valid JSON Schema" in str(error)
    with _with(_states(claude=True, chatgpt=True)):
        refused = _Plan("claude")
        refused.error = None
        refused.request = mock.Mock(side_effect=error)
        session = subscriptions.Session("claude", log=lambda _m: None, factory={"claude": refused}.__getitem__)
        with pytest.raises(RequestRejected):
            session.request(model_name="opus", system="s", prompt="p", images=[], schema=SCHEMA)
        assert session.plan == "claude" and subscriptions.exhausted() == {}       # no switch for the request's own fault

    settings = SimpleNamespace(provider="anthropic", model="opus", max_output_tokens=4096, timeout_seconds=60)
    client = SimpleNamespace(subscription=True, transport="x", request=mock.Mock(side_effect=error))
    with pytest.raises(comparison_ai.AIReviewError) as caught:
        comparison_ai._request(client, settings, "PROMPT", [])
    assert caught.value.fatal is False                                           # the run goes on with the next batch


def test_account_messages_keep_the_programs_own_words_and_model_needs_a_clear_refusal():
    from yeone_ocr import chatgpt_subscription as codex_module
    from yeone_ocr.claude_subscription import failure
    error = failure("Codex CLI", "You've hit your usage limit. Try again later.", codex_module._classify, "fallback")
    assert "사용량 한도" in str(error) and "원문: You've hit your usage limit" in str(error)
    assert codex_module._classify("unknown error from model provider: stream disconnected") is None
    assert "쓸 수 없는 모델" in codex_module._classify(
        "The 'gpt-x' model is not supported when using Codex with a ChatGPT account.")
    assert str(failure("Codex CLI", "boom", codex_module._classify, "Codex CLI 실행 실패(코드 1): boom")) == \
        "Codex CLI 실행 실패(코드 1): boom"


def test_window_label_is_short_and_the_details_stay_available():
    api = mock.Mock(provider="gemini", model="gemini-3.1-pro-preview", left=20, transport="gemini-api-key")
    with _with(_states()):
        session = subscriptions.Session("claude", api=api, log=lambda _m: None, factory=lambda plan: _Plan(plan))
        session.plan = None
        state = session.state()
    assert state["label"] == "API 키 · gemini · 남은 20회" and "남은 호출 20회" in state["detail"]
    api.calls, api.left = 20, 0
    assert "호출 상한(20회)에 도달" in session.stop_reason() and "직접 고르면" in session.stop_reason()
    session.api = None
    assert "API 키도 없음" in session.stop_reason()


# ---- AI 작업자: 엔진은 기다리지 않고, 계정마다 여러 쪽을 함께 (--local-ai-split · --local-ai-pages) ----

def _workers(stage, plans=("claude", "chatgpt"), pages=1, available=None, **hooks):
    from yeone_ocr.local_first import _AiWorkers
    failed = []
    crews = [{"plan": p, "session": SimpleNamespace(available=(available or {}).get(p, True), plan=p), "reader": None,
              "pages": pages} for p in plans]
    return _AiWorkers(crews, stage, lambda number, exc: failed.append((number, str(exc))), lambda _m: None, **hooks), failed


def test_split_pages_run_side_by_side_one_plan_per_page():
    import threading
    import time
    seen, lock, live = [], threading.Lock(), {"now": 0, "most": 0}

    def stage(number, started, review, extras, session, reader, saved):
        with lock:
            live["now"] += 1
            live["most"] = max(live["most"], live["now"])
        time.sleep(0.2)
        with lock:
            live["now"] -= 1
            seen.append((number, session.plan))
    workers, failed = _workers(stage)
    began = time.perf_counter()
    for number in range(1, 5):
        workers.submit(number, 0, {}, {})
    assert workers.finish() is None
    assert time.perf_counter() - began < 4 * 0.2 * 0.75 and live["most"] == 2       # two plans at a time
    assert sorted(n for n, _ in seen) == [1, 2, 3, 4] and failed == []
    assert {p for _, p in seen} == {"claude", "chatgpt"}
    assert len({n for n, _ in seen}) == 4                                            # each page once, by one plan


def test_one_plan_takes_several_pages_at_a_time():
    import threading
    import time
    lock, live, done = threading.Lock(), {"now": 0, "most": 0}, []

    def stage(number, started, review, extras, session, reader, saved):
        with lock:
            live["now"] += 1
            live["most"] = max(live["most"], live["now"])
        time.sleep(0.15)
        with lock:
            live["now"] -= 1
    workers, _ = _workers(stage, plans=("claude",), pages=3, on_done=done.append)
    for number in range(1, 7):
        workers.submit(number, 0, {}, {})
    assert workers.finish() is None
    assert live["most"] == 3 and sorted(done) == [1, 2, 3, 4, 5, 6]                 # --local-ai-pages 3


def test_the_engines_do_not_wait_for_the_ai_pages_queue_in_page_order():
    import threading
    gate, started, order = threading.Event(), [], []

    def stage(number, started_at, review, extras, session, reader, saved):
        order.append(number)
        gate.wait(5)
    workers, _ = _workers(stage, plans=("claude",), pages=1, on_start=started.append)
    for number in range(1, 6):
        workers.submit(number, 0, {}, {}, True)                                       # returns at once: nothing waits for an answer
    for _ in range(200):
        if started:
            break
        threading.Event().wait(0.01)
    assert started == [1] and workers.waiting() == 4                                 # four pages read by the engines wait for their AI turn
    gate.set()
    assert workers.finish() is None and order == [1, 2, 3, 4, 5]


def test_workers_report_a_page_failure_and_a_stop_leaves_the_waiting_pages():
    from yeone_ocr.comparison import ComparisonError
    seen = []

    def stage(number, started, review, extras, session, reader, saved):
        seen.append(number)
        if number == 1:
            raise ComparisonError("쪽 오류")
        return 130 if number == 2 else None
    workers, failed = _workers(stage, plans=("claude",))
    for number in (1, 2, 3, 4):
        workers.submit(number, 0, {}, {})
    assert workers.finish() == 130
    assert failed == [(1, "쪽 오류")] and seen == [1, 2]                              # 3 and 4 keep their engine reading


def test_a_connection_with_no_way_left_leaves_the_pages_to_the_others():
    seen = []
    workers, _ = _workers(lambda number, *_a: seen.append((number, _a[3].plan)), available={"chatgpt": False})
    for number in range(1, 6):
        workers.submit(number, 0, {}, {})
    workers.finish()
    assert [plan for _, plan in seen] == ["claude"] * 5


# ---- 일시 오류는 다시 보내고, 한도는 초기화되면 돌아온다 ----

def _error(message, kind, retry_after=None):
    error = SubscriptionError(message)
    error.kind, error.retry_after = kind, retry_after
    return error


class _Flaky:
    """A plan's client that fails with the given errors in turn, then answers."""

    def __init__(self, name, errors=()):
        self.name, self.errors, self.calls = name, list(errors), 0

    def request(self, **_kwargs):
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)
        return '{"lines": []}', True, "done", {}, self.name


def _ask(session):
    return session.request(model_name="opus", system="s", prompt="p", images=[], schema=SCHEMA)


def test_a_temporary_failure_is_sent_again_to_the_same_plan():
    plans = {"chatgpt": _Flaky("chatgpt", [_error("high demand", "temporary")] * 2), "claude": _Flaky("claude")}
    logs, waits = [], []
    with _with(_states(claude=True, chatgpt=True)):
        session = subscriptions.Session("chatgpt", "chatgpt", log=logs.append, factory=plans.__getitem__, sleep=waits.append)
        with mock.patch.object(subscriptions, "RETRY_WAITS", (0.01, 0.01)):
            assert _ask(session)[-1] == "chatgpt"
        assert session.plan == "chatgpt" and subscriptions.exhausted() == {}          # ChatGPT stays in the run
        assert plans["chatgpt"].calls == 3 and plans["claude"].calls == 0
        assert len(logs) == 2 and "일시 오류" in logs[0] and waits


def test_a_plan_that_keeps_failing_rests_and_is_used_again_after_the_rest():
    now = [1000.0]
    plans = {"chatgpt": _Flaky("chatgpt", [_error("stream disconnected", "temporary")] * 3), "claude": _Flaky("claude")}
    logs = []
    with _with(_states(claude=True, chatgpt=True)), mock.patch.object(subscriptions, "_clock", lambda: now[0]), \
            mock.patch.object(subscriptions, "RETRY_WAITS", (0.0, 0.0)):
        session = subscriptions.Session("chatgpt", "chatgpt", log=logs.append, factory=plans.__getitem__, sleep=lambda _s: None)
        assert _ask(session)[-1] == "claude" and session.plan == "claude"            # the request itself is answered
        assert "약 10분 뒤 다시 씁니다" in logs[-1]
        assert [item["minutes"] for item in subscriptions.resting()] == [10]
        now[0] += 300
        assert _ask(session)[-1] == "claude"                                          # still resting
        now[0] += 301
        assert _ask(session)[-1] == "chatgpt" and session.plan == "chatgpt"          # back on its own plan
        assert "다시 씁니다(쉬는 시간이 지남)" in logs[-1] and subscriptions.resting() == []


def test_a_request_that_fails_on_two_plans_is_the_requests_own_problem():
    failure_ = [_error("boom", "temporary")] * 3
    plans = {"claude": _Flaky("claude", list(failure_)), "chatgpt": _Flaky("chatgpt", list(failure_)), "gemini": _Flaky("gemini")}
    with _with(_states(claude=True, chatgpt=True, gemini=True)), mock.patch.object(subscriptions, "RETRY_WAITS", (0.0, 0.0)):
        session = subscriptions.Session("claude", log=lambda _m: None, factory=plans.__getitem__, sleep=lambda _s: None)
        with pytest.raises(SubscriptionError, match="boom"):
            _ask(session)
        assert subscriptions.exhausted() == {} and plans["gemini"].calls == 0         # no plan is lost to one bad request
        assert _ask(session)[-1] == "claude"                                          # and the next request is back on Claude


def test_a_plan_at_its_limit_comes_back_when_the_limit_resets():
    now = [0.0]
    plans = {"claude": _Flaky("claude", [_error("5-hour limit", "limit", 3600.0)]), "chatgpt": _Flaky("chatgpt")}
    logs = []
    with _with(_states(claude=True, chatgpt=True)), mock.patch.object(subscriptions, "_clock", lambda: now[0]):
        session = subscriptions.Session("claude", log=logs.append, factory=plans.__getitem__)
        assert _ask(session)[-1] == "chatgpt" and "약 1시간 0분 뒤 다시 씁니다" in logs[-1]
        now[0] = 3599
        assert _ask(session)[-1] == "chatgpt"
        now[0] = 3601
        assert _ask(session)[-1] == "claude"
        # without a stated time: looked at again after LIMIT_REST_SECONDS, twice as long the next time
        plans["claude"].errors = [_error("limit", "limit")]
        assert _ask(session)[-1] == "chatgpt"
        assert subscriptions.resting()[0]["minutes"] == round(subscriptions.LIMIT_REST_SECONDS * 2 / 60)


def test_the_last_plan_resting_makes_the_connection_available_again_later():
    now = [0.0]
    plans = {"claude": _Flaky("claude", [_error("limit", "limit", 600.0)])}
    with _with(_states(claude=True)), mock.patch.object(subscriptions, "_clock", lambda: now[0]):
        session = subscriptions.Session("claude", log=lambda _m: None, factory=plans.__getitem__)
        with pytest.raises(SubscriptionError):
            _ask(session)
        assert not session.available
        now[0] = 601
        assert session.available and session.plan == "claude" and _ask(session)[-1] == "claude"


def test_the_users_own_switch_is_not_undone_by_a_rest_that_ends():
    now = [0.0]
    plans = {"claude": _Flaky("claude"), "chatgpt": _Flaky("chatgpt")}
    with _with(_states(claude=True, chatgpt=True)), mock.patch.object(subscriptions, "_clock", lambda: now[0]):
        session = subscriptions.Session("claude", log=lambda _m: None, factory=plans.__getitem__)
        session.switch("chatgpt")
        now[0] = 10 ** 6
        assert _ask(session)[-1] == "chatgpt" and "claude" in subscriptions.exhausted()


def test_an_answer_is_recorded_by_the_plan_that_gave_it_even_if_the_connection_moved_on_meanwhile():
    import threading
    out, answer, results = threading.Event(), threading.Event(), {}

    class Gemini:
        calls = 0

        def request(self, **_kwargs):
            Gemini.calls += 1
            if Gemini.calls == 1:
                out.set()
                answer.wait(5)                                                        # still out when the plan is left
                return '{"lines": []}', True, "STOP", {}, "gemini"
            raise _error("limit", "limit", 600.0)
    plans = {"gemini": Gemini(), "claude": _Flaky("claude")}
    with _with(_states(claude=True, gemini=True)):
        session = subscriptions.Session("gemini", "gemini", log=lambda _m: None, factory=plans.__getitem__, record_provider="anthropic")
        first = threading.Thread(target=lambda: results.update(first=_ask(session)))
        first.start()
        assert out.wait(5)
        assert _ask(session)[-1] == "claude" and session.plan == "claude"            # another request of it hit the limit
        answer.set()
        first.join(5)
    # Gemini's own "STOP" is written in the record's provider's terms (the review record names anthropic)
    assert results["first"][2] == "end_turn" and results["first"][-1] == "gemini"


def test_reset_time_is_read_from_the_programs_own_words():
    import datetime
    from yeone_ocr.claude_subscription import reset_after
    at = datetime.datetime(2026, 11, 12, 22, 40)
    assert reset_after("You've hit your 5-hour limit · resets 12:10am (Asia/Seoul)", now=at) == 90 * 60 + 60
    assert reset_after("limit reached, resets 3pm", now=at) == (16 * 60 + 20) * 60 + 60        # tomorrow afternoon
    assert reset_after("You've hit your usage limit. Try again in 3 hours 42 minutes.") == (3 * 60 + 42) * 60 + 60
    assert reset_after("Your quota will reset after 1h2m5s.") == 3600 + 125 + 60
    assert reset_after("try again later") is None and reset_after("") is None


def test_claude_limit_words_and_unknown_failures_are_told_apart():
    from yeone_ocr.claude_subscription import failure
    error = failure("Claude Code", "You've hit your 5-hour limit · resets 12:10am (Asia/Seoul)", claude._classify, "fallback")
    assert error.kind == "limit" and error.retry_after and "사용량 한도" in str(error) and "5-hour limit" in str(error)
    assert failure("Claude Code", "API Error: 529 overloaded", claude._classify, "Claude Code 오류: overloaded").kind == "temporary"
    assert failure("Claude Code", 'API Error: 429 {"type":"error","error":{"type":"rate_limit_error"}}', claude._classify, "f").kind == "temporary"
    assert failure("Claude Code", "Claude usage limit reached. Your limit will reset at 3pm.", claude._classify, "f").kind == "limit"
    assert failure("Claude Code", "Not logged in · Please run /login", claude._classify, "x").kind == "account"


def test_a_number_in_an_unrelated_failure_is_not_a_sign_in_or_limit_problem():
    from yeone_ocr.claude_subscription import failure
    noise = "2026-11-12T05:28:11.401220Z ERROR codex_api: stream disconnected (request 429f01)"
    assert codex._classify(noise) is None and failure("Codex CLI", noise, codex._classify, "f").kind == "temporary"
    assert codex._classify("unexpected status 401 Unauthorized").kind == "account"
    assert codex._classify("exceeded retry limit, last status: 429 Too Many Requests").kind == "limit"
    assert "오래되어" in codex._classify("The 'gpt-6.1-sol' model requires a newer version of Codex. Please upgrade.")
    # Gemini CLI prints "Loaded cached credentials." on every run: not a sign-in problem
    assert gemini._classify("Loaded cached credentials.\nError: socket hang up") is None
    assert gemini._classify("Please login again: credentials expired").kind == "account"
    assert gemini._classify("RESOURCE_EXHAUSTED: quota exceeded").kind == "limit"


def test_codex_keeps_an_option_between_the_last_image_and_the_stdin_dash():
    seen = {}

    def run(args, **kwargs):
        seen["args"] = args
        Path(args[args.index("-o") + 1]).write_text('{"lines": []}', encoding="utf-8")
        return _ok(stdout=json.dumps({"type": "turn.completed", "usage": {}}))
    codex.SubscriptionClient(runner=run).request(model_name="codex-default", system="s", prompt="p", images=[b"a", b"b"], schema=SCHEMA)
    args = seen["args"]
    last_image = max(i for i, a in enumerate(args) if a == "-i") + 1
    assert args[-1] == "-" and args[-3] == "-o" and last_image < args.index("--output-schema")   # -i takes every value after it


def test_codex_tries_current_model_names_after_an_old_clis_own_list():
    tried = []

    def run(args, **kwargs):
        if args[1:3] == ["debug", "models"]:
            return _ok(stdout=json.dumps({"models": [{"slug": "gpt-5.5", "priority": 1}, {"slug": "gpt-5.4", "priority": 2}]}))
        name = args[args.index("-m") + 1] if "-m" in args else "(default)"
        tried.append(name)
        if name in ("(default)", "gpt-5.5", "gpt-5.4"):
            return _ok(returncode=1, stdout=json.dumps({"type": "turn.failed", "error": {"message": REFUSED}}))
        Path(args[args.index("-o") + 1]).write_text('{"lines": []}', encoding="utf-8")
        return _ok(stdout=json.dumps({"type": "turn.completed", "usage": {}}))
    client = codex.SubscriptionClient(runner=run)
    assert client.request(model_name="codex-default", system="s", prompt="p", images=[], schema=SCHEMA)[-1] == codex.FALLBACK_MODELS[0]
    assert tried == ["(default)", "gpt-5.4", codex.FALLBACK_MODELS[0]]                # gpt-5.5 was the refused default


def test_window_state_names_every_connection_and_the_resting_plans():
    plans = {"claude": _Flaky("claude"), "chatgpt": _Flaky("chatgpt", [_error("limit", "limit", 1200.0)]), "gemini": _Flaky("gemini")}
    states = []
    with _with(_states(claude=True, chatgpt=True, gemini=True)), mock.patch.object(subscriptions, "ON_CHANGE", states.append):
        first = subscriptions.Session("claude", log=lambda _m: None, factory=plans.__getitem__)
        second = subscriptions.Session("chatgpt", "chatgpt", log=lambda _m: None, factory=plans.__getitem__)
        subscriptions.activate(first, [first, second])
        assert states[-1]["label"] == "Claude 구독 · ChatGPT 구독" and states[-1]["resting"] == []
        assert _ask(second)[-1] == "claude"                                           # ChatGPT at its limit: its pages go to Claude
        assert states[-1]["label"] == "Claude 구독"
        assert [(item["plan"], item["minutes"]) for item in states[-1]["resting"]] == [("chatgpt", 20)]
        subscriptions.deactivate()
        assert states[-1] is None


def test_connection_test_sends_one_small_real_request():
    seen = []

    class Client:
        def request(self, **kwargs):
            seen.append(kwargs)
            return '{"ok": true}', True, "completed", {}, "gpt-test"
    result = subscriptions.probe("chatgpt", client_factory=lambda _plan: Client())
    assert result["ok"] and result["model"] == "gpt-test" and result["error"] == ""
    assert seen[0]["images"][0][:8] == b"\x89PNG\r\n\x1a\n" and seen[0]["schema"]["required"] == ["ok"]

    def broken(_plan):
        raise SubscriptionError("Codex CLI 로그인이 필요합니다")
    result = subscriptions.probe("chatgpt", client_factory=broken)
    assert not result["ok"] and "로그인" in result["error"]


def test_plans_test_lists_each_plans_answer(capsys):
    from yeone_ocr import cli
    with _with(_states(claude=True, chatgpt=True)), \
            mock.patch.object(subscriptions, "probe", side_effect=lambda plan: {"ok": plan == "claude", "seconds": 4.2, "model": "opus",
                                                                              "error": "" if plan == "claude" else "원문: boom"}):
        lines = cli.plan_test_lines()
    assert lines[0] == "Claude: 응답함 · 모델 opus · 4초" and lines[1] == "ChatGPT: 실패(4초) — 원문: boom"
    assert lines[2].startswith("Gemini: 시험 안 함")


# ---- 찾았지만 실행되지 않는 CLI · PATH 밖의 npm 전역 폴더 ----

def test_gemini_found_but_not_running_is_not_reported_as_missing(_fresh):
    def broken(args, **kwargs):
        return SimpleNamespace(returncode=1, stdout="", stderr="Error: Node.js 18 is not supported; please use 20+")
    state = gemini.status(refresh=True, runner=broken)
    assert state["installed"] is True and state["runs"] is False and not state["usable"]
    assert "실행하지 못했습니다(코드 1)" in state["reason"] and "Node.js 18" in state["reason"]
    assert state["path"] == gemini.find_cli()


def test_cli_outside_path_is_found_in_the_npm_global_folder(monkeypatch, tmp_path):
    """Runs on every platform (the Windows release regression counts a skip as a failure)."""
    monkeypatch.delenv("YEONE_OCR_GEMINI_CLI_PATH", raising=False)
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    for name in ("APPDATA", "ProgramFiles", "NVM_SYMLINK", "VOLTA_HOME", "LOCALAPPDATA"):
        monkeypatch.delenv(name, raising=False)
    prefix = tmp_path / "npm-prefix"
    if os.name == "nt":
        prefix.mkdir()
        expected = prefix / "gemini.cmd"                                   # npm's Windows shim
    else:
        (prefix / "bin").mkdir(parents=True)
        expected = prefix / "bin" / "gemini"
    expected.write_text("")
    monkeypatch.setenv("NPM_CONFIG_PREFIX", str(prefix))
    assert gemini.find_cli() == str(expected)



# ---- Codex: the account refuses Codex's default model (model_not_found) ----

REFUSED = ('{"type":"error","status":400,"error":{"type":"invalid_request_error","message":"The \'gpt-5.5\' model is not '
           'supported when using Codex with a ChatGPT account.","code":"model_not_found"}}')


def _codex_runner(accepted, calls):
    def run(args, **kwargs):
        if args[1:3] == ["debug", "models"]:
            calls.append("catalog")
            catalog = {"models": [
                {"slug": "gpt-6.1-sol", "visibility": "list", "priority": 1, "input_modalities": ["text", "image"]},
                {"slug": "gpt-text-only", "visibility": "list", "priority": 2, "input_modalities": ["text"]},
                {"slug": "gpt-hidden", "visibility": "hide", "priority": 3, "input_modalities": ["text", "image"]},
                {"slug": "gpt-6-sol", "visibility": "list", "priority": 4, "input_modalities": ["text", "image"]}]}
            return SimpleNamespace(returncode=0, stdout=json.dumps(catalog), stderr="")
        named = args[args.index("-m") + 1] if "-m" in args else None
        calls.append(named)
        if named not in accepted:
            events = [{"type": "turn.started"}, {"type": "turn.failed", "error": {"message": REFUSED}}]
            return SimpleNamespace(returncode=1, stdout="\n".join(json.dumps(e) for e in events), stderr="")
        Path(args[args.index("-o") + 1]).write_text('{"lines": []}', encoding="utf-8")
        return SimpleNamespace(returncode=0, stderr="", stdout=json.dumps({"type": "turn.completed", "usage": {}}))
    return run


def test_codex_tries_the_accounts_next_model_when_its_default_is_refused_and_keeps_it():
    calls = []
    client = codex.SubscriptionClient(runner=_codex_runner({"gpt-6-sol"}, calls))
    out = client.request(model_name="codex-default", system="s", prompt="p", images=[], schema=SCHEMA)
    assert out[1] is True and out[-1] == "gpt-6-sol"
    assert calls == [None, "catalog", "gpt-6.1-sol", "gpt-6-sol"]              # text-only and hidden models skipped
    calls.clear()
    client.request(model_name="codex-default", system="s", prompt="p", images=[], schema=SCHEMA)
    assert calls == ["gpt-6-sol"]                                               # remembered: no refused call again


def test_codex_with_no_model_left_is_an_account_problem_not_a_refused_request():
    from yeone_ocr.claude_subscription import RequestRejected
    calls = []
    client = codex.SubscriptionClient(runner=_codex_runner(set(), calls))
    with pytest.raises(SubscriptionError) as caught:
        client.request(model_name="codex-default", system="s", prompt="p", images=[], schema=SCHEMA)
    assert not isinstance(caught.value, RequestRejected)                        # the session moves to the next plan
    assert "쓸 수 없는 모델" in str(caught.value) and "npm install -g @openai/codex" in str(caught.value)
    calls.clear()
    with pytest.raises(SubscriptionError):
        client.request(model_name="gpt-5.5", system="s", prompt="p", images=[], schema=SCHEMA)
    assert calls == ["gpt-5.5"]                                                 # a model chosen by the person is not changed


def test_model_refusal_is_classified_before_the_request_rejection_words():
    from yeone_ocr.claude_subscription import RequestRejected, failure
    error = failure("Codex CLI", REFUSED, codex._classify, "fallback")
    assert not isinstance(error, RequestRejected) and "쓸 수 없는 모델" in str(error)



# ---- npm's own global folder · a slow first start · where the program was looked for ----

def test_npm_reports_its_global_folder_set_in_npmrc(monkeypatch, tmp_path):
    monkeypatch.delenv("YEONE_OCR_GEMINI_CLI_PATH", raising=False)
    monkeypatch.delenv("NPM_CONFIG_PREFIX", raising=False)
    for name in ("APPDATA", "ProgramFiles", "NVM_SYMLINK", "VOLTA_HOME", "LOCALAPPDATA", "SCOOP", "ChocolateyInstall", "BUN_INSTALL"):
        monkeypatch.delenv(name, raising=False)
    bin_dir = tmp_path / "npm-bin"
    bin_dir.mkdir()
    npm = bin_dir / ("npm.cmd" if os.name == "nt" else "npm")
    npm.write_text("")
    npm.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir))
    custom = tmp_path / "custom-prefix"                                  # `npm config set prefix …` (.npmrc)
    program = custom / "gemini.cmd" if os.name == "nt" else custom / "bin" / "gemini"
    program.parent.mkdir(parents=True)
    program.write_text("")
    asked = []

    def run(args, **kwargs):
        asked.append(args[1:])
        return SimpleNamespace(returncode=0, stdout=str(custom) + "\n", stderr="")
    assert claude.npm_global_cli("gemini", runner=run) == str(program)
    assert asked == [["prefix", "-g"]]
    assert claude.npm_global_cli("gemini", runner=run) == str(program) and len(asked) == 1     # cached


def test_gemini_slow_to_answer_its_version_is_still_installed(_fresh):
    import subprocess as sp
    home = _fresh
    _settings(home, "oauth-personal")

    def slow(args, **kwargs):
        raise sp.TimeoutExpired(args, kwargs.get("timeout"))
    state = gemini.status(refresh=True, runner=slow)
    assert state["installed"] is True and state.get("runs") is not False and state["usable"]


def test_a_missing_program_says_where_it_was_looked_for(monkeypatch, tmp_path):
    monkeypatch.setenv("YEONE_OCR_GEMINI_CLI_PATH", str(tmp_path / "nowhere" / "gemini"))
    state = gemini.status(refresh=True)
    assert state["installed"] is False
    assert "찾아봤습니다" in state["reason"] and "YEONE_OCR_GEMINI_CLI_PATH" in state["reason"] and "npx" in state["reason"]
