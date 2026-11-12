"""설치·로그인 줄: 공식 설치 명령과 로그인 창. 실제로 설치하거나 창을 열지 않는다(가짜 실행)."""
import sys
from types import SimpleNamespace
from unittest import mock

import pytest

from yeone_ocr import plan_setup


def test_install_commands_use_the_official_npm_packages(monkeypatch):
    monkeypatch.setattr(plan_setup, "find_npm", lambda: "/usr/bin/npm")
    assert plan_setup.install_command("gemini") == ["/usr/bin/npm", "install", "-g", "@google/gemini-cli"]
    assert plan_setup.install_command("chatgpt") == ["/usr/bin/npm", "install", "-g", "@openai/codex"]
    assert plan_setup.install_command("claude") is None                     # Claude Code: its own installer
    monkeypatch.setattr(plan_setup, "find_npm", lambda: None)
    assert plan_setup.install_command("gemini") is None                     # no Node.js: ask for it first


def test_npm_is_found_outside_path(monkeypatch, tmp_path):
    if sys.platform == "win32":
        pytest.skip("POSIX layout")
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    monkeypatch.setattr(plan_setup.Path, "home", lambda: tmp_path)
    assert plan_setup.find_npm() is None or "/usr/local/bin" in plan_setup.find_npm() or "/opt/homebrew" in plan_setup.find_npm()
    (tmp_path / ".volta" / "bin").mkdir(parents=True)
    (tmp_path / ".volta" / "bin" / "npm").write_text("")
    found = plan_setup.find_npm()
    assert found is not None and found.endswith("npm")


def test_run_streams_the_output_to_the_log_and_returns_the_code():
    lines = []
    code = plan_setup.run([sys.executable, "-c", "print('added 1 package'); print(''); import sys; sys.exit(3)"], lines.append)
    assert code == 3 and lines == ["  added 1 package"]
    assert plan_setup.run(["/nonexistent/npm", "install"], lines.append) == 1 and "실행하지 못했습니다" in lines[-1]


@pytest.mark.parametrize("platform", ["win32", "darwin", "linux"])
def test_login_opens_the_program_in_a_new_window(monkeypatch, platform):
    opened = []
    monkeypatch.setattr(plan_setup.sys, "platform", platform)
    monkeypatch.setattr(plan_setup.shutil, "which", lambda name: "/usr/bin/x-terminal-emulator" if name == "x-terminal-emulator" else None)
    message = plan_setup.open_login("chatgpt", "/bin/codex", popen=lambda args, **kw: opened.append(args))
    assert message is None
    assert opened[0][-2:] == ["/bin/codex", "login"] or "codex login" in opened[0][-1]
    if platform == "win32":
        assert opened[0][:2] == ["cmd.exe", "/k"]
    opened.clear()
    monkeypatch.setattr(plan_setup.shutil, "which", lambda name: None)
    if platform == "linux":
        assert "터미널에서 실행하세요: /bin/gemini" in plan_setup.open_login("gemini", "/bin/gemini", popen=opened.append)
        assert opened == []
