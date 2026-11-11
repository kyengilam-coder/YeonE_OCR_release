"""Claude subscription transport (`claude -p`) with a fake CLI: no network, no real sign-in."""
import json
import os
import subprocess
from types import SimpleNamespace
from unittest import mock

import pytest

from yeone_ocr import claude_subscription as sub
from yeone_ocr import comparison_ai


@pytest.fixture(autouse=True)
def _fresh(tmp_path, monkeypatch):
    sub.reset_status_cache()
    cli = tmp_path / ("claude.exe" if os.name == "nt" else "claude")
    cli.write_text("")
    monkeypatch.setenv("YEONE_OCR_CLAUDE_CLI_PATH", str(cli))
    for name in ("YEONE_OCR_CLAUDE_AUTH", "YEONE_OCR_CLAUDE_MODEL", "YEONE_OCR_CLAUDE_EFFORT"):
        monkeypatch.delenv(name, raising=False)
    yield
    sub.reset_status_cache()


def _status_runner(payload):
    def run(args, **kwargs):
        assert args[1:] == ["auth", "status"]
        return SimpleNamespace(stdout=json.dumps(payload), stderr="", returncode=0)
    return run


SIGNED_IN = {"loggedIn": True, "authMethod": "claude.ai", "subscriptionType": "max", "email": "someone@example.com"}


def test_status_reports_plan_without_keeping_the_email():
    state = sub.status(runner=_status_runner(SIGNED_IN))
    assert state["usable"] and state["billing"] == "subscription" and state["plan"] == "max"
    assert "email" not in state


@pytest.mark.parametrize("payload, words", [
    ({"loggedIn": False, "authMethod": "none"}, "로그인되어 있지 않습니다"),
    ({"loggedIn": True, "authMethod": "api_key"}, "구독 계정이 아니라"),
])
def test_status_explains_why_it_cannot_be_used(payload, words):
    state = sub.status(runner=_status_runner(payload))
    assert not state["usable"] and words in state["reason"]


def test_choose_modes():
    runner = _status_runner(SIGNED_IN)
    assert sub.choose("api", "anthropic", "", runner=runner) == (False, "")
    assert sub.choose("auto", "anthropic", "sk-real-key", runner=runner) == (False, "")      # a key keeps the API path
    assert sub.choose("auto", "gemini", "", runner=runner) == (False, "")                    # other providers untouched
    use, note = sub.choose("auto", "anthropic", "", runner=runner)
    assert use and "구독" in note
    assert sub.choose("subscription", "gemini", "x", runner=runner)[0]


def test_subscription_mode_says_why_when_not_signed_in():
    use, note = sub.choose("subscription", "anthropic", "", runner=_status_runner({"loggedIn": False}))
    assert not use and "claude auth login" in note


def test_request_runs_the_cli_without_tools_or_api_keys(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-must-not-leak")
    seen = {}

    def run(args, **kwargs):
        seen.update(args=args, **kwargs)
        events = [{"type": "rate_limit_event", "rate_limit_info": {"five_hour": 0.1}},
                  {"type": "result", "subtype": "success", "is_error": False,
                   "structured_output": {"items": []}, "usage": {"input_tokens": 1200, "output_tokens": 30},
                   "modelUsage": {"claude-opus-5-5": {}}}]
        return SimpleNamespace(stdout="\n".join(json.dumps(e) for e in events), stderr="", returncode=0)

    client = sub.SubscriptionClient(runner=run)
    text, complete, reason, tokens, actual = client.request(
        model_name="opus", system="SYSTEM", prompt="PROMPT", images=[b"\x89PNG"], schema={"type": "object"})
    assert json.loads(text) == {"items": []} and complete and reason == "end_turn"
    assert tokens == {"input_tokens": 1200, "output_tokens": 30} and actual == "claude-opus-5-5"
    args = seen["args"]
    assert args[1] == "-p" and args[args.index("--tools") + 1] == ""
    assert {"--strict-mcp-config", "--no-session-persistence", "--disable-slash-commands"} <= set(args)
    assert args[args.index("--model") + 1] == "opus" and args[args.index("--effort") + 1] == "medium"
    assert "ANTHROPIC_API_KEY" not in seen["env"]
    message = json.loads(seen["input"])["message"]
    assert [part["type"] for part in message["content"]] == ["text", "image"]
    assert sub.status(runner=_status_runner(SIGNED_IN))["limits"] == {"five_hour": 0.1}


def test_request_errors_are_explained_in_korean():
    def limited(args, **kwargs):
        return SimpleNamespace(stdout=json.dumps({"type": "result", "is_error": True, "result": "Claude usage limit reached"}),
                               stderr="", returncode=1)
    with pytest.raises(sub.SubscriptionError, match="사용량 한도"):
        sub.SubscriptionClient(runner=limited).request(model_name="opus", system="", prompt="", images=[], schema={})

    def slow(args, **kwargs):
        raise subprocess.TimeoutExpired(args, 1)
    with pytest.raises(TimeoutError):
        sub.SubscriptionClient(runner=slow).request(model_name="opus", system="", prompt="", images=[], schema={})


def test_cmd_shim_is_refused(tmp_path, monkeypatch):
    shim = tmp_path / "claude.cmd"
    shim.write_text("")
    monkeypatch.setenv("YEONE_OCR_CLAUDE_CLI_PATH", str(shim))
    with pytest.raises(sub.SubscriptionError, match="claude.exe"):
        sub.SubscriptionClient()


def test_comparison_review_uses_the_cli_for_the_subscription_marker():
    settings = comparison_ai.AISettings("anthropic", "opus")
    client = comparison_ai.create_client(settings, sub.SUBSCRIPTION_KEY)
    assert isinstance(client, sub.SubscriptionClient)
    with pytest.raises(comparison_ai.AIReviewError, match="Claude"):
        comparison_ai.create_client(comparison_ai.AISettings("gemini", "gemini-2.5-pro"), sub.SUBSCRIPTION_KEY)

    calls = []

    class Fake:
        subscription = True

        def request(self, **kwargs):
            calls.append(kwargs)
            return '{"items": []}', True, "end_turn", {"input_tokens": 5, "output_tokens": 2}, "claude-opus-5-5"

    text, complete, metadata = comparison_ai._request(Fake(), settings, "prompt", [b"png"])
    assert text == '{"items": []}' and complete
    assert metadata["transport"] == "claude-code-subscription" and metadata["usage"]["input_tokens"] == 5
    assert metadata["actual_model"] == "claude-opus-5-5" and calls[0]["system"] == comparison_ai.SYSTEM

    class Broken:
        subscription = True

        def request(self, **kwargs):
            raise sub.SubscriptionError("Claude 구독 사용량 한도")
    with pytest.raises(comparison_ai.AIReviewError) as caught:
        comparison_ai._request(Broken(), settings, "prompt", [])
    assert comparison_ai._fatal_request_error(caught.value)          # stop the run instead of retrying every span
