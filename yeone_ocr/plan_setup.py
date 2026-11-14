"""Install and sign in to the subscription programs from the window (설정 「AI 판독기」 ① 설치·로그인).

Install runs the provider's own published install command on this PC (npm for Codex CLI; Google's
installer script for Antigravity CLI, which serves personal Google accounts since Gemini CLI stopped
serving them on 2026-06-18); sign-in opens the provider's own program in a new console window, where the person signs in on
the provider's own screen. This program never sees a password or token.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

NPM_PACKAGES = {"chatgpt": "@openai/codex"}
PROGRAMS = {"claude": "claude", "chatgpt": "codex", "gemini": "gemini"}
LOGIN_ARGS = {"claude": ["auth", "login"], "chatgpt": ["login"], "gemini": []}
LOGIN_NOTES = {"claude": "Claude 계정(Pro/Max)으로 로그인하세요.",
               "chatgpt": "'Sign in with ChatGPT'를 고르고 ChatGPT 계정으로 로그인하세요.",
               "gemini": "열리는 Google 로그인 화면(브라우저)에서 Google 계정으로 로그인한 뒤 창을 닫으세요. "
                         "Gemini CLI가 열렸다면 'Login with Google'을 고르고 /quit 로 닫으세요."}
CLAUDE_INSTALL = "PowerShell에서 `irm https://claude.ai/install.ps1 | iex` (Mac·Linux: `curl -fsSL https://claude.ai/install.sh | bash`)"
NODE_PAGE = "https://nodejs.org/"
_NO_WINDOW = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)} if sys.platform == "win32" else {}


def find_npm() -> str | None:
    """npm of an installed Node.js, on PATH or in the usual install folders."""
    names = ("npm.cmd", "npm") if sys.platform == "win32" else ("npm",)
    for name in names:
        found = shutil.which(name)
        if found:
            return found
    folders = []
    if sys.platform == "win32":
        for variable in ("ProgramFiles", "ProgramFiles(x86)", "NVM_SYMLINK", "LOCALAPPDATA"):
            base = os.environ.get(variable, "").strip()
            if base:
                folders.append(Path(base) / ("Programs/nodejs" if variable == "LOCALAPPDATA" else
                                             "" if variable == "NVM_SYMLINK" else "nodejs"))
    else:
        folders += [Path("/usr/local/bin"), Path("/opt/homebrew/bin"), Path.home() / ".volta" / "bin"]
    for folder in folders:
        for name in names:
            if (folder / name).is_file():
                return str(folder / name)
    return None


def find_winget() -> str | None:
    return shutil.which("winget") if sys.platform == "win32" else None


def install_command(plan: str) -> list | None:
    """The install command for a plan's program, or None (Claude: its own installer; no npm; no shell
    for Google's installer script)."""
    if plan == "gemini":
        return antigravity_install_command()
    package = NPM_PACKAGES.get(plan)
    npm = find_npm()
    if package is None or npm is None:
        return None
    return [npm, "install", "-g", package]


def antigravity_install_command() -> list | None:
    """Google's published Antigravity CLI installer (agy lands in %LOCALAPPDATA%\\agy\\bin or ~/.local/bin)."""
    from .antigravity_subscription import INSTALL_COMMAND_POSIX, INSTALL_COMMAND_WINDOWS
    if sys.platform == "win32":
        shell = shutil.which("powershell.exe") or shutil.which("powershell") or shutil.which("pwsh")
        return [shell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", INSTALL_COMMAND_WINDOWS] if shell else None
    shell = shutil.which("bash")
    return [shell, "-c", INSTALL_COMMAND_POSIX] if shell and shutil.which("curl") else None


def manual_install(plan: str) -> str:
    """The install command for a person to run themselves."""
    from .antigravity_subscription import INSTALL_COMMAND_POSIX, INSTALL_COMMAND_WINDOWS
    if plan == "gemini":
        return f"PowerShell: {INSTALL_COMMAND_WINDOWS}\nMac·Linux: {INSTALL_COMMAND_POSIX}"
    return CLAUDE_INSTALL if plan == "claude" else f"npm install -g {NPM_PACKAGES.get(plan, '')}"


def node_install_command() -> list | None:
    winget = find_winget()
    if winget is None:
        return None
    return [winget, "install", "--id", "OpenJS.NodeJS.LTS", "-e", "--accept-source-agreements",
            "--accept-package-agreements"]


def run(command: list, log, runner=subprocess.Popen) -> int:
    """Run an install command, passing its output to log line by line. Returns the exit code."""
    env = dict(os.environ)
    env.setdefault("PYTHONUTF8", "1")
    try:
        proc = runner(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                      text=True, encoding="utf-8", errors="replace", env=env, **_NO_WINDOW)
    except OSError as exc:
        log(f"  실행하지 못했습니다: {exc}")
        return 1
    for line in proc.stdout:
        line = line.rstrip()
        if line:
            log("  " + line)
    return proc.wait()


def login_command(plan: str, cli: str) -> list:
    return [cli, *LOGIN_ARGS[plan]]


def open_login(plan: str, cli: str, popen=subprocess.Popen) -> str | None:
    """Open the program's sign-in in a new console window. Returns a message when no window can be
    opened here (the person runs the command themselves)."""
    command = login_command(plan, cli)
    if sys.platform == "win32":
        popen(["cmd.exe", "/k", *command], creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0x10))
        return None
    if sys.platform == "darwin":
        script = " ".join(_quote(part) for part in command)
        popen(["osascript", "-e", f'tell application "Terminal" to do script "{script}"'])
        return None
    terminal = shutil.which("x-terminal-emulator") or shutil.which("gnome-terminal") or shutil.which("konsole")
    if terminal:
        popen([terminal, "-e", *command])
        return None
    return "터미널에서 실행하세요: " + " ".join(command)


def _quote(part: str) -> str:
    return "'" + part.replace("'", "'\\''") + "'" if any(c in part for c in " '\"") else part
