"""Tests never start a real Claude Code, Codex or Gemini CLI: the development machine may have one signed in."""
import os

os.environ["YEONE_OCR_CLAUDE_AUTH"] = "api"                       # automatic subscription use stays off
os.environ["YEONE_OCR_AI_PLAN"] = "auto"
for name in ("CLAUDE", "CODEX", "GEMINI"):
    os.environ[f"YEONE_OCR_{name}_CLI_PATH"] = os.path.join(os.path.dirname(__file__), f"_no_{name.lower()}_cli_")
