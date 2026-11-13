"""구독 CLI 실패의 종류는 출력 전체로 가린다. Node CLI 는 오류 이름·이유를 먼저 쓰고 긴 호출 경로를 뒤에 써서,
끝 400자만 보면 계정 문제(Gemini IneligibleOrProjectIdError, 429 한도)도 '일시 오류'로 보여 끝없이 다시 보냈다."""
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from yeone_ocr import claude_subscription, gemini_subscription
from yeone_ocr.claude_subscription import failure, program_error

FRAMES = "\n".join(f"    at async CodeAssistServer.requestPost (file:///C:/Users/me/npm/chunk.js:{n}:18)" for n in range(60))


def node_error(first_line):
    return f"Loaded cached credentials.\n{first_line}\n{FRAMES}\n"


def test_program_error_keeps_the_reason_and_drops_stack_frames():
    full, shown = program_error(node_error("Error: IneligibleOrProjectIdError: this account is not eligible"), "")
    assert "IneligibleOrProjectIdError" in shown and "at async" not in shown
    assert "CodeAssistServer" in full and len(shown) <= 400
    assert program_error("", "")[1] == ""
    assert program_error("    at only.frames (x.js:1:1)")[1].startswith("at only.frames")


@pytest.mark.parametrize("first_line, kind", [
    ("Error: IneligibleOrProjectIdError: Gemini Code Assist is not available for this account", "account"),
    ("GaxiosError: Resource has been exhausted (e.g. check quota). status 429", "limit"),
    ("Error: Please log in again: invalid_grant", "account"),
    ("Error: socket hang up", "temporary"),
])
def test_gemini_failure_kind_is_judged_on_the_whole_output(first_line, kind):
    full, shown = program_error(node_error(first_line), "")
    assert gemini_subscription._classify(full[-400:]) is None                  # the old tail-only view saw nothing
    error = failure("Gemini CLI", shown, gemini_subscription._classify, "Gemini CLI 실행 실패: " + shown, full=full)
    assert error.kind == kind
    if kind == "account" and "Ineligible" in first_line:
        assert "Gemini CLI를 쓸 수 없다고" in str(error)


def test_gemini_free_tier_that_dropped_the_cli_gets_its_own_explanation():
    """Gemini CLI 0.68.0, personal Google account (2026-11-14): Google ended CLI support for the free tier."""
    output = ("Error authenticating: IneligibleTierError: This client is no longer supported for Gemini Code Assist for "
              "individuals. To continue using Gemini, please migrate to the Antigravity suite of products: "
              "https://antigravity.google\n" + FRAMES + "\n{ ineligibleTiers: [ { reasonCode: 'UNSUPPORTED_CLIENT', "
              "tierId: 'free-tier', tierName: 'Gemini Code Assist for individuals' } ] }")
    full, shown = program_error(output, "")
    error = failure("Gemini CLI", shown, gemini_subscription._classify, "x", full=full)
    assert error.kind == "account"
    assert "개인 Google 계정" in str(error) and "Pro" in str(error) and "API 키" in str(error)
    assert "Antigravity CLI" in str(error) and "[설치]" in str(error)      # the way that still works, and where to start it
    assert "GOOGLE_CLOUD_PROJECT" not in str(error)


def test_gemini_cli_run_reports_an_ineligible_account_as_out_for_this_run(tmp_path, monkeypatch):
    def run(*_args, **_kwargs):
        return SimpleNamespace(returncode=1, stdout="", stderr=node_error("Error: IneligibleOrProjectIdError: not eligible"))
    cli = gemini_subscription.SubscriptionClient(cli="gemini", runner=run, timeout_seconds=30)
    with pytest.raises(claude_subscription.SubscriptionError) as caught:
        cli.request(model_name="", system="s", prompt="p", images=[], schema={"type": "object"})
    assert caught.value.kind == "account" and "at async" not in str(caught.value)


def test_gemini_cli_refused_for_a_personal_account_is_not_offered_as_usable_again(tmp_path, monkeypatch):
    """The [연결 시험] of a PC with Gemini CLI signed in and no Antigravity CLI: Google refuses the account,
    and from then on the status says so instead of '사용 가능' (every run would meet the same refusal)."""
    (tmp_path / ".gemini").mkdir()
    (tmp_path / ".gemini" / "settings.json").write_text('{"security": {"auth": {"selectedType": "oauth-personal"}}}', encoding="utf-8")
    monkeypatch.setenv("YEONE_OCR_GEMINI_CLI_HOME", str(tmp_path))
    monkeypatch.setattr(gemini_subscription, "find_cli", lambda: "gemini")
    monkeypatch.setattr(gemini_subscription, "_refused", False)
    monkeypatch.setattr(gemini_subscription, "_status_cache", None)
    refusal = ("Warning: 256-color support not detected. Using a terminal with at least 256-color support is recommended "
               "for a better visual experience.\nError authenticating: IneligibleTierError: This client is no longer "
               "supported for Gemini Code Assist for individuals. To continue using Gemini, please migrate to the "
               "Antigravity suite of products: https://antigravity.google\n" + FRAMES)

    def run(args, **_kwargs):
        if "--version" in args:
            return SimpleNamespace(returncode=0, stdout="0.68.0", stderr="")
        return SimpleNamespace(returncode=1, stdout="", stderr=refusal)
    assert gemini_subscription.status(refresh=True, runner=run)["usable"] is True      # signed in, as far as the PC shows
    cli = gemini_subscription.SubscriptionClient(cli="gemini", runner=run, timeout_seconds=30)
    with pytest.raises(claude_subscription.SubscriptionError) as caught:
        cli.request(model_name="", system="s", prompt="p", images=[], schema={"type": "object"})
    assert caught.value.kind == "account" and "Antigravity CLI" in str(caught.value)
    assert "GOOGLE_CLOUD_PROJECT" not in str(caught.value)
    state = gemini_subscription.status(runner=run)                                     # not the cached '사용 가능'
    assert state["usable"] is False and state["installed"] and state["closed"] and "Antigravity CLI" in state["reason"]
    assert gemini_subscription.status(refresh=True, runner=run)["usable"] is False     # [상태 확인] does not forget it


def test_another_gemini_cli_failure_does_not_close_the_plan(tmp_path, monkeypatch):
    monkeypatch.setattr(gemini_subscription, "_refused", False)
    monkeypatch.setattr(gemini_subscription, "_status_cache", None)

    def run(*_args, **_kwargs):
        return SimpleNamespace(returncode=1, stdout="", stderr=node_error("Error: socket hang up"))
    cli = gemini_subscription.SubscriptionClient(cli="gemini", runner=run, timeout_seconds=30)
    with pytest.raises(claude_subscription.SubscriptionError):
        cli.request(model_name="", system="s", prompt="p", images=[], schema={"type": "object"})
    assert gemini_subscription._refused is False


def test_claude_failure_without_result_is_judged_on_everything_printed():
    output = "Starting...\n" + "Error: You've hit your 5-hour limit · resets 12:10am\n" + FRAMES
    full, shown = program_error(output, "")
    assert claude_subscription._classify(full[-400:]) is None                  # the old tail-only view
    error = failure("Claude Code", shown, claude_subscription._classify, "x", full=full)
    assert error.kind == "limit" and error.retry_after is not None and "at async" not in shown
