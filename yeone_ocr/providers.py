"""GUI 시작 시 외부 패키지 없이 읽는 판독 제공사 설정."""
from __future__ import annotations
import os

PROVIDERS = ("gemini", "openai", "anthropic", "mock")
PROVIDER_LABELS = {"gemini": "Gemini", "openai": "ChatGPT (OpenAI)", "anthropic": "Claude (Anthropic)", "mock": "mock (시험용)"}
DEFAULT_MODELS = {"gemini": "gemini-2.5-pro", "openai": "gpt-4.1", "anthropic": "claude-sonnet-4-6", "mock": "mock"}
ENV_KEYS = {"gemini": ("GEMINI_API_KEY", "GOOGLE_API_KEY"), "openai": ("OPENAI_API_KEY",), "anthropic": ("ANTHROPIC_API_KEY",), "mock": ()}
MODEL_ENV_KEYS = {"gemini": "GEMINI_MODEL", "openai": "OPENAI_MODEL", "anthropic": "ANTHROPIC_MODEL", "mock": ""}
KEY_URLS = {"gemini": "https://aistudio.google.com/apikey", "openai": "https://platform.openai.com/api-keys", "anthropic": "https://platform.claude.com/settings/keys", "mock": ""}


def provider_name(provider: str | None) -> str:
    p = (provider or "gemini").strip().lower()
    p = {"chatgpt": "openai", "claude": "anthropic", "google": "gemini"}.get(p, p)
    if p not in PROVIDERS:
        raise ValueError(f"알 수 없는 판독 제공사: {p}")
    return p


def default_model(provider: str = "gemini") -> str:
    p = provider_name(provider)
    return os.environ.get(MODEL_ENV_KEYS[p], "").strip() or DEFAULT_MODELS[p]


def provider_of_model(model: str | None) -> str:
    """모델 이름으로 제공사를 짐작한다: gemini-→gemini · claude-→anthropic · gpt-·chatgpt-·o1·o3·o4→openai, 모르면 ''.
    ' (3차)' 같은 꼬리말과 'models/' 앞머리는 떼고 본다(사용량 기록의 모델 열쇠·재사용 기록의 모델 이름)."""
    name = (model or "").strip().lower().removeprefix("models/").split(" (")[0]
    if name.startswith("gemini-"):
        return "gemini"
    if name.startswith("claude-"):
        return "anthropic"
    if name.startswith(("gpt-", "chatgpt-", "o1", "o3", "o4")):
        return "openai"
    return ""
