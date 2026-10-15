"""사용자 양식(HWPX)의 스타일로 내용별 문서를 채운다. 양식의 쪽 설정(용지·여백)은 그대로 두고 본문은 비운 뒤, 블록의 쓰임(structure.ROLES)마다
양식 스타일을 골라 문단을 넣는다. 원본 PDF 쪽은 그 쪽이 시작되는 글자 자리의 책갈피('PDF 6쪽 · 인쇄 394쪽')로 남기고, 확인 등급 색·고지 메모는
양식 글자 모양 위에 덧입힌다(바탕은 양식 그대로, 색·굵기·형광펜만 바꾼 글자 모양을 따로 만든다).

한문 문단은 줄 나눔을 '글자 단위'로 바꾼 문단 모양을 쓴다: 양식 스타일 대부분이 '어절 단위'(KEEP_WORD)라 띄어쓰기 없는 한문이 긴 덩어리째
다음 줄로 넘어가며 줄 가운데 큰 빈칸이 생긴다. 스타일 연결은 그대로라 한글에서 스타일을 다시 적용하면 양식대로 돌아간다.
작성기 본체는 hwpx_writer(양식 모양 TemplateLook) — 이 모듈은 예전 이름(build_hwpx_template·template_styles 등)을 그대로 내보낸다.
쓰임 → 스타일 이름은 짜임 규칙(presets)의 styles 에서 온다(예전 PARA_STYLES·CHAR_STYLES 는 data/preset_kaibao.json 으로 옮겼다)."""
from __future__ import annotations
from .blocks import Block
from .hwpx_writer import HH, HP, HC, OPF, HWPUNIT_PER_MM, HEADING_ROLES, COLOR_NOTE, TemplateLook, template_styles, write_hwpx
from .presets import Preset

__all__ = ["HH", "HP", "HC", "OPF", "HWPUNIT_PER_MM", "HEADING_ROLES", "COLOR_NOTE", "template_styles", "build_hwpx_template"]


def build_hwpx_template(blocks: list[Block], out_path: str, template: str, min_mm: float = 70, large_min_mm: float = 120,
                        highlight: bool = True, memos: bool = True, author: str = "YeonE_OCR", preset: Preset | None = None) -> dict:
    """blocks(쓰임이 붙은 내용별 블록)를 양식 template 의 스타일로 out_path 에 쓴다. 돌려주는 값은 hwpx_builder.build_hwpx 와 같은 꼴에
    styles(쓰임 → 쓴 스타일 이름)·missing(양식에 없어 바탕글로 쓴 쓰임)·template_dropped(양식에서 뺀 구역·머리말·꼬리말)·page_number_from_template 를 더한 것.
    preset: 쓰임 → 스타일 이름을 줄 짜임 규칙(None = kaibao 이름 — 예전 그대로)."""
    return write_hwpx(blocks, out_path, TemplateLook(template, preset=preset, author=author), min_mm=min_mm, large_min_mm=large_min_mm,
                      highlight=highlight, memos=memos)
