"""설치·로그인 줄: 공식 설치 명령과 로그인 창. 실제로 설치하거나 창을 열지 않는다(가짜 실행)."""
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

from yeone_ocr import plan_setup


def test_install_commands_use_the_official_installers(monkeypatch):
    monkeypatch.setattr(plan_setup, "standalone_codex", lambda: None)
    monkeypatch.setattr(plan_setup, "find_npm", lambda: "/usr/bin/npm")
    assert plan_setup.install_command("chatgpt") == ["/usr/bin/npm", "install", "-g", "@openai/codex"]
    assert plan_setup.install_command("claude") is None                     # Claude Code: its own installer
    # Gemini: Google's own Antigravity CLI installer (Gemini CLI no longer serves personal accounts), no npm
    command = plan_setup.install_command("gemini")
    assert any("https://antigravity.google/cli/install." in part for part in command)
    monkeypatch.setattr(plan_setup, "find_npm", lambda: None)
    assert plan_setup.install_command("chatgpt") is None                    # no Node.js: ask for it first
    assert plan_setup.install_command("gemini") == command                  # no Node.js needed


def test_npm_is_found_outside_path(monkeypatch, tmp_path):
    """Runs on every platform (the Windows release regression counts a skip as a failure)."""
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    if sys.platform == "win32":
        for name in ("ProgramFiles(x86)", "NVM_SYMLINK", "LOCALAPPDATA"):
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv("ProgramFiles", str(tmp_path / "pf"))
        (tmp_path / "pf" / "nodejs").mkdir(parents=True)
        npm = tmp_path / "pf" / "nodejs" / "npm.cmd"
    else:
        monkeypatch.setattr(plan_setup.Path, "home", lambda: tmp_path)
        (tmp_path / ".volta" / "bin").mkdir(parents=True)
        npm = tmp_path / ".volta" / "bin" / "npm"
    npm.write_text("")
    found = plan_setup.find_npm()
    assert found is not None and Path(found).name in ("npm", "npm.cmd")
    if sys.platform == "win32":
        assert found == str(npm)


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
        # System32's cmd.exe by full path: never a cmd.exe planted in the app or current folder.
        assert opened[0][0].replace("\\", "/").lower().endswith("/system32/cmd.exe") and opened[0][1] == "/k"
    opened.clear()
    monkeypatch.setattr(plan_setup.shutil, "which", lambda name: None)
    if platform == "linux":
        assert "터미널에서 실행하세요: /bin/gemini" in plan_setup.open_login("gemini", "/bin/gemini", popen=opened.append)
        assert opened == []


def test_a_standalone_codex_is_updated_by_openais_installer(monkeypatch):
    """Codex 0.139.0 installed by chatgpt.com/codex/install.ps1 kept running after [설치] updated npm's copy."""
    monkeypatch.setattr(plan_setup, "find_npm", lambda: "C:/node/npm.cmd")
    monkeypatch.setattr(plan_setup.sys, "platform", "win32")
    from yeone_ocr import chatgpt_subscription
    monkeypatch.setattr(chatgpt_subscription, "candidates", lambda: ["C:/Users/me/AppData/Local/OpenAI/Codex/bin/codex.exe",
                                                                    "C:/Users/me/AppData/Roaming/npm/codex.cmd"])
    command = plan_setup.install_command("chatgpt")
    assert command[0].lower().endswith("powershell.exe") and "https://chatgpt.com/codex/install.ps1" in command[-1]
    assert "CODEX_NON_INTERACTIVE=1" in command[-1]
    monkeypatch.setattr(chatgpt_subscription, "candidates", lambda: ["C:/Users/me/AppData/Roaming/npm/codex.cmd"])
    assert plan_setup.install_command("chatgpt") == ["C:/node/npm.cmd", "install", "-g", "@openai/codex"]
    assert "install.ps1" in chatgpt_subscription._update_command("C:/x/OpenAI/codex.exe")
    assert "npm install" in chatgpt_subscription._update_command("C:/Users/me/AppData/Roaming/npm/codex.cmd")
