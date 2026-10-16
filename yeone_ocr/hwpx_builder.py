"""python-hwpx 로 실제 HWPX(OWPML zip) 파일을 만든다. 확장자만 바꾼 파일이 아니다.

가로쓰기 정리본: 제목은 계층대로, 본문은 함초롬바탕, 소주는 작게, 쪽 기록 줄은 작은 회색.
원본 쪽마다 쪽 나누기. 글자는 확인 등급(grades.GRADE_STYLE)으로 칠한다: 〔疑: ○〕〔○/△〕〔판독불명〕 같은 의심 자리는 빨강
(굵게 + 노란 형광펜), 3차 자형 판별로 정한 글자는 초록, 문맥이 가리키는 후보는 파랑. 메모(한컴 메모)는 고지 사항에만 글자 하나에 단다.
〔→PDF 2쪽〕〔소주〕 같은 작업 표시는 회색 작은 글씨로 구분한다(지침 22: 작업 메모는 원문과 분명히 구분).
작성기 본체는 hwpx_writer(기본 모양 DefaultLook) — 이 모듈은 예전 이름(build_hwpx·상수·split_marks 등)을 그대로 내보낸다.
"""
from __future__ import annotations
from .blocks import Block
from .grades import styled_runs, GRADE_STYLE, STYLE_ALIAS
from .marks import split_marks, NOTE_PREFIXES
from .hwpx_writer import (BODY_WIDTH_MM, HP, MEMO_AUTHOR, FONT_BODY, FONT_HEAD, HEADING_FORMAT, COLOR_MARK, SHADE_MARK, COLOR_NOTE, COLOR_META,
                          BasicLook, DefaultLook, figure_width_mm, write_hwpx)
from .presets import Preset, resolve_preset

__all__ = ["BODY_WIDTH_MM", "HP", "MEMO_AUTHOR", "FONT_BODY", "FONT_HEAD", "HEADING_FORMAT", "COLOR_MARK", "SHADE_MARK", "COLOR_NOTE", "COLOR_META",
           "split_marks", "styled_runs", "GRADE_STYLE", "STYLE_ALIAS", "NOTE_PREFIXES", "figure_width_mm", "build_hwpx"]


def build_hwpx(blocks: list[Block], out_path: str, min_mm: float = 70, large_min_mm: float = 120,
               highlight: bool = True, page_breaks: bool = True, memos: bool = True, preset: Preset | str | None = None) -> dict:
    """highlight=False 면 등급 색·형광펜·밑줄 없이 글자만(〔〕 표기는 그대로). memos=False 면 고지를 메모 대신 회색 〔고지: …〕 로 글 속에.
    preset=basic 은 대제목·중제목·소제목·문단-1~5 한글 스타일을 만든다. None 은 예전 기본 모양을 그대로 쓴다."""
    preset = resolve_preset(preset) if isinstance(preset, str) else preset
    look = BasicLook(preset, page_breaks=page_breaks) if preset is not None and preset.id == "basic" else DefaultLook(page_breaks=page_breaks)
    return write_hwpx(blocks, out_path, look, min_mm=min_mm, large_min_mm=large_min_mm, highlight=highlight, memos=memos)
