"""Gemini 구독을 Antigravity CLI(agy)로: Google이 2026-06-18에 개인 계정의 Gemini CLI를 끝냈다.
agy 는 여기서 실행하지 않는다 — 문서·이슈(google-antigravity/antigravity-cli #1166·#1278·#1065)에 나온 입출력 모양으로 흉내 낸다."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from yeone_ocr import antigravity_subscription as agy, subscriptions
from yeone_ocr.claude_subscription import SubscriptionError

SCHEMA = {"type": "object", "properties": {"items": {"type": "array"}}, "required": ["items"]}


def stream(*events):
    return "\n".join(json.dumps(e) for e in events) + "\n"


def runner(stdout="", stderr="", code=0, seen=None):
    def run(args, **kwargs):
        if seen is not None:
            folder = Path(kwargs["cwd"])
            seen.update(args=args, input=kwargs["input"], files=sorted(p.name for p in folder.iterdir()),
                        bytes=[p.read_bytes() for p in sorted(folder.iterdir())], env=kwargs["env"])
        return SimpleNamespace(stdout=stdout, stderr=stderr, returncode=code)
    return run


def test_request_sends_one_stream_event_with_schema_and_images_and_reads_structured_output(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "must-not-replace-the-sign-in")
    seen = {}
    out = stream({"event": "init", "model": "gemini-3.8-flash-high"},
                 {"event": "result", "result": {"status": "SUCCESS", "response": "done", "structured_output": {"items": []},
                                                 "usage": {"input": 1200, "output": 30}}})
    client = agy.SubscriptionClient(cli="agy", runner=runner(out, seen=seen))
    text, complete, reason, tokens, actual = client.request(model_name="gemini-3.8-flash-high", system="SYSTEM",
                                                            prompt="PROMPT", images=[b"\x89PNG-1", b"\x89PNG-2"], schema=SCHEMA)
    assert json.loads(text) == {"items": []} and complete and reason == "STOP"
    assert tokens == {"input_tokens": 1200, "output_tokens": 30} and actual == "gemini-3.8-flash-high"
    args = seen["args"]
    assert args[:5] == ["agy", "--input-format", "stream-json", "--output-format", "stream-json"]
    assert json.loads(args[args.index("--json-schema") + 1]) == SCHEMA
    assert args[args.index("--model") + 1] == "gemini-3.8-flash-high"
    assert args[args.index("--add-dir") + 1].startswith(str(Path(args[args.index("--add-dir") + 1]).parent))
    assert seen["files"] == ["image1.png", "image2.png"] and seen["bytes"] == [b"\x89PNG-1", b"\x89PNG-2"]
    event = json.loads(seen["input"])
    assert event["event"] == "user"
    content = event["message"]["content"]
    assert content.startswith("SYSTEM") and "PROMPT" in content and "image1.png" in content and "image2.png" in content
    assert "GEMINI_API_KEY" not in seen["env"]


def test_default_model_is_not_passed_and_no_images_means_no_file_instruction():
    seen = {}
    out = stream({"event": "result", "result": {"status": "SUCCESS", "structured_output": {"items": []}}})
    agy.SubscriptionClient(cli="agy", runner=runner(out, seen=seen)).request(
        model_name=agy.label(""), system="S", prompt="P", images=[], schema=SCHEMA)
    assert "--model" not in seen["args"] and "file viewing tool" not in json.loads(seen["input"])["message"]["content"]


@pytest.mark.parametrize("out, stderr, code, kind", [
    # SUCCESS without structured output (#1065, #794) is never an answer
    (stream({"event": "result", "result": {"status": "SUCCESS", "response": ""}}), "", 0, "temporary"),
    (stream({"event": "result", "result": {"status": "ERROR", "error": {"message": "RESOURCE_EXHAUSTED: quota"}}}), "", 1, "limit"),
    ("", "Error: not signed in. Please sign in.\n    at auth (agy.js:1:1)", 1, "account"),
    ("", "failed to construct executor: unknown model key X: model not found", 1, "account"),
    ("", "", 0, "temporary"),                                                  # silent 0-byte stdout (#76, #1278)
])
def test_failures_are_classified(out, stderr, code, kind):
    client = agy.SubscriptionClient(cli="agy", runner=runner(out, stderr, code))
    with pytest.raises(SubscriptionError) as caught:
        client.request(model_name="", system="S", prompt="P", images=[], schema=SCHEMA)
    assert caught.value.kind == kind and "at auth" not in str(caught.value)


def test_status_finds_agy_and_never_reads_credentials(tmp_path, monkeypatch):
    agy.reset_status_cache()
    monkeypatch.setattr(agy, "find_cli", lambda: None)
    state = agy.status(refresh=True, runner=runner())
    assert not state["installed"] and "install.ps1" in state["reason"]
    monkeypatch.setattr(agy, "find_cli", lambda: "agy")
    state = agy.status(refresh=True, runner=runner("1.4.2"))
    assert state["installed"] and state["usable"] and state["billing"] == "subscription"
    state = agy.status(refresh=True, runner=runner("", "boom", 2))
    assert state["runs"] is False and not state["usable"]
    agy.reset_status_cache()


def test_gemini_plan_runs_agy_when_installed_else_gemini_cli(monkeypatch):
    monkeypatch.delenv("YEONE_OCR_GEMINI_TRANSPORT", raising=False)
    monkeypatch.setattr(agy, "find_cli", lambda: "agy")
    assert subscriptions.module("gemini") is agy and subscriptions.program("gemini") == "Antigravity CLI"
    monkeypatch.setenv("YEONE_OCR_GEMINI_TRANSPORT", "gemini-cli")
    assert subscriptions.module("gemini").__name__.endswith("gemini_subscription")
    monkeypatch.delenv("YEONE_OCR_GEMINI_TRANSPORT")
    monkeypatch.setattr(agy, "find_cli", lambda: None)
    from yeone_ocr import gemini_subscription
    monkeypatch.setattr(gemini_subscription, "find_cli", lambda: "gemini")
    assert subscriptions.module("gemini") is gemini_subscription
    assert subscriptions.program("gemini") == "Gemini CLI" and subscriptions.program("claude") == "Claude Code"
    monkeypatch.setattr(gemini_subscription, "find_cli", lambda: None)     # neither: the setup hint is agy's
    assert subscriptions.module("gemini") is agy


def test_missing_agy_names_its_own_path_variable(monkeypatch):
    monkeypatch.setattr(agy, "find_cli", lambda: None)
    reason = agy.status(refresh=True)["reason"]
    assert "YEONE_OCR_AGY_CLI_PATH" in reason and "install.ps1" in reason and "npx" not in reason


def test_find_cli_looks_in_the_windows_install_folder(tmp_path, monkeypatch):
    monkeypatch.delenv("YEONE_OCR_AGY_CLI_PATH", raising=False)
    monkeypatch.setattr(agy.shutil, "which", lambda name: None)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert agy.find_cli() is None or not str(agy.find_cli()).startswith(str(tmp_path))
    exe = tmp_path / "agy" / "bin" / "agy.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"")
    assert agy.find_cli() == str(exe)
