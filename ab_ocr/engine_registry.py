"""Explicit local engine identities and document-profile routing.

No discovery, download, model inference or document-dependent selection occurs
here. Existing projects retain their stored baseline and original run records.
"""
from __future__ import annotations

LEGACY_ENGINES = ("tesseract", "rapidocr", "easyocr")
NEW_ENGINES = ("paddle_server", "glmocr", "surya", "ppocrv6")
ENGINES = LEGACY_ENGINES + NEW_ENGINES
ENGINE_LABELS = {
    "tesseract": "Tesseract", "rapidocr": "RapidOCR mobile", "easyocr": "EasyOCR",
    "ppocrv6": "PP-OCRv6 medium", "paddle_server": "PaddleOCR server", "glmocr": "GLM-OCR", "surya": "Surya",
}

# Fixed 120-unit development comparison: strict Unicode, whitespace removed;
# primary criterion is Han/Hangul attributed errors, secondary is full CER.
# See docs/LOCAL_ENGINE_COMPARISON.md for scope and punctuation trade-offs.
PRIMARY_ENGINES = {"mixed": "surya", "traditional": "glmocr", "simplified": "glmocr"}
RECOMMENDED_ENGINES = {
    "mixed": ("surya", "glmocr", "tesseract"),
    # EasyOCR remains a reference because it preserved Chinese punctuation well.
    "traditional": ("glmocr", "surya", "easyocr"),
    "simplified": ("glmocr", "surya", "easyocr"),
}


def language_for(engine: str, profile: str, direction: str = "horizontal") -> str:
    if engine not in ENGINES or profile not in PRIMARY_ENGINES or direction not in ("horizontal", "vertical"):
        raise ValueError("OCR 엔진·문서 종류·읽기 방향을 확인하세요.")
    if profile == "mixed":
        language = ("kor+chi_tra" if engine in ("tesseract", "glmocr", "surya")
                    else "chi_tra" if engine == "paddle_server" else "kor")
    else:
        language = "chi_sim" if profile == "simplified" else "chi_tra"
    if direction == "vertical" and engine == "tesseract":
        language = language.replace("chi_tra", "chi_tra_vert").replace("chi_sim", "chi_sim_vert").replace("kor", "kor_vert")
    return language


def primary_engine(profile: str, direction: str = "horizontal") -> str:
    if profile not in PRIMARY_ENGINES or direction not in ("horizontal", "vertical"):
        raise ValueError("문서 종류·읽기 방향을 확인하세요.")
    # Current comparison labels contain horizontal body text only. Preserve the
    # established vertical baseline until vertical documents are also measured.
    return "tesseract" if direction == "vertical" else PRIMARY_ENGINES[profile]


def recommended_engines(profile: str, direction: str = "horizontal") -> tuple[str, ...]:
    preferred = primary_engine(profile, direction)
    # No new vertical accuracy labels were measured. Preserve the legacy set.
    return (LEGACY_ENGINES if direction == "vertical"
            else tuple(dict.fromkeys((preferred, *RECOMMENDED_ENGINES[profile])))[:3])


def engine_set(mode: str, profile: str, direction: str = "horizontal") -> tuple[str, ...]:
    if mode == "legacy":
        return LEGACY_ENGINES
    if mode == "all":
        return ENGINES
    if mode == "primary":
        return (primary_engine(profile, direction),)
    if mode == "recommended":
        return recommended_engines(profile, direction)
    raise ValueError("판독 모델 묶음을 확인하세요.")
