"""Versioned text handling; absent policy always replays historical records."""
from __future__ import annotations

LEGACY_TEXT_POLICY = "legacy-nfc-v1"
SOURCE_TEXT_POLICY = "source-preserving-v1"


def validate_text_policy(policy: str) -> str:
    if policy not in (LEGACY_TEXT_POLICY, SOURCE_TEXT_POLICY):
        raise ValueError(f"지원하지 않는 문자 보존 정책: {policy!r}")
    return policy


def record_text_policy(record: dict | None) -> str:
    return validate_text_policy((record or {}).get("text_policy", LEGACY_TEXT_POLICY))


def record_fold_zh(record: dict | None) -> bool | None:
    mode = (record or {}).get("mode")
    return mode == "horizontal_zh" if mode in ("vertical", "horizontal_zh") else None


def variation_selector(char: str) -> bool:
    return len(char) == 1 and (0xFE00 <= ord(char) <= 0xFE0F or 0xE0100 <= ord(char) <= 0xE01EF)


def touches_selector(text: str, start: int, end: int) -> bool:
    """Also protect a changed base immediately before an unchanged selector."""
    return (any(variation_selector(c) for c in text[start:end])
            or (end < len(text) and variation_selector(text[end])))


def compatibility_ideograph(char: str) -> bool:
    return len(char) == 1 and (0xF900 <= ord(char) <= 0xFAFF or 0x2F800 <= ord(char) <= 0x2FA1F)
