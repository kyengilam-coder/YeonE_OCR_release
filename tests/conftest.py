"""Tests never start a real Claude Code CLI: the development machine may have one signed in."""
import os

os.environ["YEONE_OCR_CLAUDE_AUTH"] = "api"                       # automatic subscription use stays off
os.environ["YEONE_OCR_CLAUDE_CLI_PATH"] = os.path.join(os.path.dirname(__file__), "_no_claude_cli_")
