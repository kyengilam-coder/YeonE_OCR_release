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
