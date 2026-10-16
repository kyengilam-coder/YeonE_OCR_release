"""YeonE_OCR 창 프로그램. 모든 기능을 단추로 조작한다.

표준 라이브러리 tkinter 만 쓰므로 Python 만 있으면 뜬다. 무거운 패키지(pymupdf, hwpx, google-genai…)는
단추를 눌렀을 때 비로소 불러온다. Windows 실행 파일에는 포함되며, Python 소스 실행에서 빠졌으면
[고급 도구 ▸ → 패키지 설치·점검] 단추로 설치한다.
"""
from __future__ import annotations
import contextlib, copy, io, json, math, os, queue, re, subprocess, sys, threading, time, traceback, webbrowser
from pathlib import Path
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, simpledialog

from . import __version__
from .progress import STAGE_NAMES, Tracker, fmt_duration, fmt_elapsed
from .providers import DEFAULT_MODELS, PROVIDER_LABELS, ENV_KEYS, KEY_URLS
from .presets import (ALIASES, BUILTIN, DEFAULT_PRESET, PresetError, export_builtin, load_preset, preset_choices, preset_label,   # 표준 라이브러리만 — 창이 바로 뜬다
                      resolve_preset, template_suggests)
from .runtime import is_frozen, is_installed_bundle, can_replace_executable, safe_output_path, external_dll_search
from .state import BOOK_SETTINGS, LAST_COST, book_counts, clean_book_values, load_book_settings, page_marks, save_book_settings, target_pages, write_atomic   # 표준 라이브러리만(무거운 모듈은 그 안에서 늦게 부른다)

IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp", ".gif", ".jp2", ".j2k", ".jpx")   # pdfio.IMAGE_EXTS 와 같게(창은 pymupdf 없이도 떠야 하므로 따로 둠)

APP_NAME = "YeonE_OCR"
CONFIG_PATH = Path.home() / ".yeone_ocr" / "config.json"
PACKAGES = [("pymupdf", "pymupdf"), ("PIL", "pillow"), ("numpy", "numpy"), ("cv2", "opencv-python-headless"),
            ("pydantic", "pydantic"), ("google.genai", "google-genai"), ("openai", "openai"), ("anthropic", "anthropic"), ("hwpx", "python-hwpx"), ("docx", "python-docx"), ("opencc", "opencc-python-reimplemented"), ("hanja", "hanja")]
DEFAULTS = {"pages": "", "print_offset": "", "upscale": "2", "reader": "gemini", "model": "gemini-3.1-pro-preview",
            "api_key": "", "second_pass": True, "docx": False, "textlayer_pdf": False, "layout": "page", "template": "", "bold_split": True, "preset": DEFAULT_PRESET, "context_model": True, "join_pages": True, "force": False, "highlight": True, "page_breaks": True,
            "prices": {}, "fx_krw": "", "workers": 8, "second_model": "auto", "checkpoint": 20, "use_cache": True, "second_thinking_low": True, "adjudicate_blind": False, "skip_any_model": True, "check_list": "none", "last_pdf": "", "hwpx": "", "collate_pdf": "", "anchor_source": "auto", "heading_styles": "", "collate_policy": "sure", "collate_memo": "notice", "memo_mode": "notice", "context_hint": True, "collate_vertical": False,
            "chunk_lines": 8, "media_res": "high", "mode": "auto", "image_mode": "auto", "advanced_open": False, "details_open": False, "auto_update": True,
            "normalize_traditional": True, "normalize_yun": True, "normalize_punctuation": True, "punctuation_rules": ""}
CHECK_LIST_LABELS = {"none": "없음(본문만)", "extra": "본문에 표시 못 한 것만", "all": "전부(검수 화면과 같음)"}
LAYOUT_LABELS = {"page": "원본 쪽대로(검수용)", "flow": "내용별(제목·문단)"}   # 문서 짜임 --layout
PRESET_PICK = "파일에서 고르기…"                                   # 짜임 규칙 칸의 끝 항목 — 규칙 JSON 파일을 고른다(--preset 파일 경로)
PRESET_EXPORT = "本草 조문 규칙을 파일로 저장…"                     # 그 뒤 — 내장 kaibao 파일을 복사해 새 책의 규칙을 만드는 바탕으로(설치판·실행 파일 하나 판은 data 폴더를 찾기 어렵다)
LEGACY_PRESET = {"auto": "kaibao", "bencao": "bencao", "none": "regions"}   # 0.7.x 설정의 짜임 규칙(structure) → 짜임 규칙(preset). bencao 는 그대로(약재명 늘 떼기)
from .collate.labels import MEMO_MODES as MEMO_LABELS          # 한컴 메모를 어디에 달지(패키지 없이도 창이 뜨게 가벼운 모듈에서)
MODE_LABELS = {"auto": "자동 판정", "vertical": "세로쓰기 한자·한글", "horizontal_zh": "가로쓰기 한문(간체)"}
MRES_LABELS = {"default": "기본", "high": "높음(high)", "ultra_high": "최고(ultra_high · Gemini 3)"}   # 2차 조각 이미지 토큰 예산
PAGE_THINK_LABELS = {"auto": "자동(모델 기본)", "low": "낮음(low)", "medium": "중간(medium)", "high": "높음(high)"}         # 1차(쪽 전체) 생각 수준 --page-thinking
PAGE_MRES_LABELS = {"auto": "자동(설정 없음)", "high": "높음(high)", "ultra_high": "최고(ultra_high · Gemini 3)"}          # 1차 쪽 이미지 토큰 예산 --page-media-resolution
STATUSES = ["원본 대조 완료(사람)", "판독 확인 필요", "이미지 보존 확인 완료", "문서 반영 완료", "파일 구조 검사 완료"]

HELP = f"""{APP_NAME} · 연어OCR {__version__}

한자·한글 혼용 문헌(세로쓰기 스캔 PDF, 가로쓰기 한문 책)을 지침에 따라 OCR·교정해 HWPX 로 만듭니다.
글자는 선택한 Gemini·ChatGPT(OpenAI)·Claude 가 읽고, 프로그램은 영역 잘라내기·두 판독 비교·문서 조립·상태 기록을 맡습니다.
제공사별 API 키와 모델을 따로 기억합니다. 채팅 서비스 구독과 별도로 해당 제공사의 API 키가 필요합니다.
같은 파일·결과 폴더에서 판독기를 바꿔 실행하면 완료한 쪽은 보존하고 남은 작업을 이어 갑니다.
문서를 만들 때 [설정] → [자동 교정]에서 선택한 간체→번체, 중문부호 규칙, 云/雲 교정을 적용하고 변경 내역을 남깁니다.
중문부호 규칙은 [규칙 추가·수정]에서 편집하여 JSON 파일로 저장하거나 [파일에서 고르기]로 불러옵니다.
맨 위의 [일반 OCR]은 PDF를 판독하고, [책갈피 OCR]은 기존 HWPX를 PDF와 대조합니다.
[이미지(스크린샷) OCR]은 이미지 파일이나 캡처를 판독합니다. PDF와 이미지 입력·쪽 범위는 따로 기억합니다.
일반·이미지 OCR에서 [HWPX 만들기] 또는 [문자 PDF 만들기]를 선택하고 공통 [중지]로 멈출 수 있습니다.
[설정]에는 판독(판독기·모델·API 키) · 이 책(쪽 범위·읽기 방향·문서 짜임·양식·짜임 규칙·획 굵기, 고급의 인쇄 쪽수 보정) · 문서 만들기 · 자동 교정 · 모델 단계 · 책갈피 교정 옵션이 모여 있습니다. 단계별 실행·도구는 작업 탭 기록 카드의 [고급 도구 ▸]에 있습니다.
[설정 바꾸기]로 이동하고 [설정 저장] 후 [OCR로 돌아가기]로 하던 작업을 계속합니다.
문서 짜임·양식·짜임 규칙·획 굵기·쪽 범위·인쇄 쪽수 보정·읽기 방향은 책마다 그 결과 폴더의 book_settings.json 에 따로 기억합니다
(설정 저장·창 닫기·다른 책으로 바꿀 때·실행 직전에 저장, 새 책은 지난 문서 설정을 이어 쓰고 쪽 범위·보정은 비움).

쓰는 법은 세 단계입니다. ① API 키 넣기(처음 한 번) → ② 원본 선택 → ③ [▶ 한 번에 인식].
읽기 방향·인쇄 쪽수 차이·이전 판 기록 갱신·2차 비교·3차 글자 판별·문서·검수 화면·보고·혼동 장부를 프로그램이 알아서 이어서 합니다.
진행 카드의 막대는 처음부터 끝까지 한 방향으로 차고, 단계 띠(준비 → 판독 → 3차 판별 → 문서 만들기 → 검수 화면 → 보고)와 쪽·지난·남은 시간·호출 수가 나옵니다.
쪽 띠의 칸을 누르면 [쪽별 상태]에서 그 쪽을 보입니다.
2차 판독 모델·속도·조각 같은 설정은 판독 칸의 [세부 설정 ▾] 에, 단계별 단추는 작업 탭 기록 카드의 [고급 도구 ▸] 에 접혀 있습니다. 단추에 마우스를 올리면 하는 일이 나옵니다.

차례
 1. Windows 실행 파일에는 패키지가 들어 있습니다. Python 소스로 실행하면 [패키지 설치·점검]([고급 도구 ▸] 안)으로 설치할 수 있습니다.
 2. [설정]에서 판독기를 고르고 해당 제공사의 API 키를 넣어 [키 확인·모델 고르기] — 키가 맞는지 확인하고 쓸 수 있는 모델을 고릅니다.
 3. [일반 OCR]의 [PDF 고르기…]로 PDF를 고릅니다. 스캔 이미지(PNG·JPG·TIFF 등)는 [이미지(스크린샷) OCR]의 [이미지 고르기…]로 엽니다.
    이미지를 여러 장 함께 고르면 이름의 숫자 순서로
    한 권(PDF)으로 묶어 읽습니다. 쪽 범위는 비워 두면 전체입니다. 읽기 방향(세로쓰기 한자·한글 / 가로쓰기 한문)과
    인쇄 쪽수 차이는 PDF 글자층으로 자동 판정합니다("자동 판정"·빈 칸 그대로). 글자층이 없는 이미지·스캔의 읽기 방향은
    실제 글줄 배치로 자동 판정합니다. 이미지 OCR은 기본값이 자동 판정이며 PDF·책갈피와 방향 설정을 따로 기억합니다.
    화면 캡처는 Win+Shift+S → 이미지 탭의 큰 원본 상자 클릭 → Ctrl+V로 불러옵니다. 마우스 오른쪽 메뉴나 Ctrl+Shift+V도 됩니다.
    계속 붙여넣으면 기존 이미지 뒤에 순서대로 추가됩니다. 미리보기의 이전·다음 단추로 각 쪽을 확인한 뒤 전체를 한 번에 인식합니다.
    이미지를 두 번 클릭하면 원본을 크게 볼 수 있습니다. [입력 비우기]로 새 묶음을 시작하고, [이미지 고르기]로 새 원본을 선택합니다.
    붙여넣은 캡처는 사용자 데이터 폴더의 screenshots에 PNG로 보관합니다. 같은 PNG 원본을 재사용해도 다시 붙이면 한 쪽을 더 추가합니다.
    붙여넣기는 이전 쪽 범위·인쇄 쪽수 보정·강제 재판독을 초기화하며, 일반 Ctrl+V는 입력 칸의 글자 붙여넣기로 유지합니다.
    [입력 비우기]는 선택한 이미지·묶음과 쪽 범위·진행 표시를 비우고 새 입력을 기다립니다. 저장된 원본·판독 기록·HWPX는 그대로 남습니다.
 4. [▶ 한 번에 인식 (HWPX 만들기)] 를 누르면 먼저 판독·3차 글자 판별·합의 의심·문맥 교정의 예상 호출·토큰·비용을 한 창에 보여 주고
    ([비용 미리 보기] 와 같음), [예 · 모두 실행] 을 누르면 준비 → 판독(1차·2차) → 3차 글자 판별 → 문서 만들기 → 검수 화면 → 보고·혼동 장부 까지
    한 번에 갑니다([아니요 · 모델 단계 없이] 는 판독·문서 만들기만). 내장 요금표에 없는 모델은 단가를 한 번 넣어 두면 금액도 나옵니다.
    이전 판으로 만든 판독 기록이 있으면 지금 규칙으로 저절로 다시 비교합니다(모델 호출 없음).
    쪽이 많으면 오래 걸립니다(쪽당 30초~1분). [■ 중지] 로 멈춰도 끝난 쪽은 남고, 다시 누르면 남은 쪽부터 이어서 합니다.
    [▶ 한 번에 인식 (문자 PDF 만들기)]는 새 원본의 판독부터 검색·복사 가능한 PDF 생성까지 이어서 합니다.
    문자 PDF는 원본 모양과 글자 위치를 유지하며 HWPX용 자동 교정을 적용하지 않습니다. HWPX 양식도 필요 없습니다.
 5. 끝나면 실행 카드의 [HWPX 열기]·[문자 PDF 열기]·[검수 화면]으로 결과를 엽니다(실행 중에는 지난 결과의 읽기용 사본을 엽니다).
    책 카드의 [결과 저장]에 실제 저장 폴더가 표시되며 [저장 폴더]로 탐색기에서 확인합니다. 장부에는 마지막 비용 어림과 이번 실행의 호출 수가 남습니다.
    검수 화면에서 원본과 대조합니다. 〔 〕로 남은 자리만 사람이 보면 됩니다.
 6. 책갈피 OCR 탭: 이미 입력된 HWPX 가 있으면 [PDF 고르기…] → [HWPX 고르기…] → [▶ HWPX 전체 교정]. 구획을 차례로 전부 대조해
    사본(<이름>_교정.hwpx)에 고쳐 넣고 요약을 남깁니다. 入/人 같은 자형 편향 자리는 글자 조각을 모델에 다시 물어 가립니다.
 7. 작업 탭 기록 카드의 [고급 도구 ▸] 를 펼치면 ①~⑥ 단계별 단추와 [3차 글자 판별]·[혼동 장부]·[비교 다시 계산]·[PDF 글자층 넣기]·[검수 결과 기록…] 이 보입니다.
    [PDF 글자층 넣기]는 판독한 글을 원본 PDF 쪽 위에 보이지 않는 글자층으로 넣어 찾기·복사가 되는 PDF(out\\<이름>_글자층.pdf)를 만듭니다.
    문서 옵션의 "검색 가능한 PDF 도 만들기"를 켜면 [▶ 한 번에 인식] 끝에 저절로 만듭니다. 원본 PDF 는 고치지 않습니다.
    인쇄 쪽수 보정은 [설정] 탭 '이 책' 섹션의 [고급 ▸]에 있습니다. 파일 쪽수와 책에 적힌 쪽수를 맞춰 검수할 때 쓰며, 비우면 자동 판정합니다.
    글자를 손으로 고치려면 결과 폴더의 readings/pNNN.json 을 고친 뒤 [④ 문서 만들기] 를 다시 누릅니다. 사람이 대조를 마친 쪽은 [검수 결과 기록…] 으로 남깁니다.
 8. 시작할 때 새 버전을 확인하며, 위의 [업데이트 확인]으로 직접 확인할 수도 있습니다. [설치 후 재시작]을 고르면
    실행 파일만 교체합니다. API 키·설정·판독 기록은 유지됩니다. 자동 확인은 [설정] 탭 바닥의 '시작할 때 업데이트 확인'으로 끌 수 있습니다.

결과는 원본 옆의 「<이름>_YeonE_OCR」(이미지는 「<이름>_<확장자>_YeonE_OCR」) 안에 생깁니다.
붙여넣은 캡처는 Windows의 %LOCALAPPDATA%\\YeonE_OCR\\screenshots에 보관합니다. [결과 폴더]로 결과 위치를 엽니다.
 out/      HWPX (+DOCX)
 review.html  검수 화면      report.md  상태 보고
 readings/ 쪽별 판독 기록    crops/     이미지로 보존한 도판
 book_settings.json  책별 설정(원본을 바꿔 이전 결과를 보관해도 남음)

짜임 규칙은 판독한 글을 제목·본문·주석 등으로 나누고 문단 스타일에 연결하는 기준입니다.
한글 스타일은 글꼴·크기·문단 모양을 정합니다. [기본 규칙]은 대제목·중제목·소제목과 문단-1~5를 사용합니다.
문단-1은 본문, 문단-2는 해설, 문단-3은 소주, 문단-4는 그림 설명, 문단-5는 작업 메모용입니다.
원문에서 확인한 구분만 사용하고 제목·문단을 임의로 만들지 않습니다. 별도 책의 규칙은 [파일에서 고르기]로 불러옵니다.

〔 〕 표시는 확인이 필요한 자리입니다. 프로그램은 추측으로 채우지 않습니다.
"""


SPEED_DEFAULTS_VERSION = 6  # 2: 0.4.1 동시 6(자동 조절)·2차 모델 auto·2차 생각 줄이기 / 3: 0.4.4 쪽 끝 확인 목록 없음 / 4: 0.6.0 조각 8줄 묶음·2차 조각 고해상 / 5: 0.6.2 조각 해상도 3단(media_res) / 6: 0.6.8 동시 8·책갈피 교정 정책 '확실한 것만'
CLOSE_WAIT_SEC = 15.0       # 작업 중 창을 닫을 때 작업 스레드가 쪽을 마저 쓰기를 기다리는 한도
PENDING_MAX = 30            # 결과 폴더가 아직 없는 책의 설정을 기억해 두는 책 수(전체 설정의 book_pending)


def upgrade_config(cfg: dict, raw: dict | None = None) -> dict:
    """이전 판이 저장한 설정에 새 속도 기본값을 입힌다(사용자가 바꾼 값은 그대로). raw = 파일에서 읽은 그대로(DEFAULTS 를 섞기 전 — 없는 열쇠를 보려고).
    load_config 는 저장할 때(write_config)도 불리므로 몇 번 돌아도 같아야 한다."""
    raw = cfg if raw is None else raw
    if "preset" not in raw:                                   # 0.7.x 의 structure → 짜임 규칙(preset). 내용별로 쓰던 사람은 本草 조문 그대로(약재명 짜임을 잃지 않게)
        legacy = str(raw.get("structure", "auto"))
        used_flow = raw.get("layout") == "flow" or bool(str(raw.get("template") or "").strip()) or legacy == "bencao"   # 지금은 원본 쪽대로여도 양식을 골랐거나 '本草 규칙 늘'을 골랐으면 내용별을 쓴 사람
        cfg["preset"] = LEGACY_PRESET.get(legacy, "kaibao") if used_flow else DEFAULT_PRESET
    if not isinstance(cfg.get("preset"), str):                # 손으로 고쳐 망가진 값
        cfg["preset"] = DEFAULT_PRESET
    if not isinstance(cfg.get("book_pending", {}), dict):
        cfg["book_pending"] = {}
    ver = int(cfg.get("speed_defaults", 1) or 1)
    if ver < 2:
        if int(cfg.get("workers", 3) or 3) <= 3:
            cfg["workers"] = DEFAULTS["workers"]
        if not str(cfg.get("second_model", "")).strip():
            cfg["second_model"] = DEFAULTS["second_model"]
        cfg.setdefault("use_cache", True); cfg.setdefault("second_thinking_low", True)
    if ver < 3 and cfg.get("check_list", "none") == "extra":
        cfg["check_list"] = "none"
    if ver < 4:
        cfg.setdefault("chunk_lines", DEFAULTS["chunk_lines"])
    if ver < 6:                                               # 0.6.8: 옛 기본값(동시 6·정책 all)이면 새 기본값으로, 직접 바꾼 값은 그대로
        if int(cfg.get("workers", 6) or 6) == 6:
            cfg["workers"] = DEFAULTS["workers"]
        if cfg.get("collate_policy", "all") == "all":
            cfg["collate_policy"] = DEFAULTS["collate_policy"]
    if "media_high" in cfg:                                   # 0.6.0~0.6.1 의 켬/끔 → 3단 선택 (load_config 가 DEFAULTS 를 먼저 섞으므로 setdefault 가 아니라 대입)
        cfg["media_res"] = "high" if cfg.pop("media_high") else "default"
    cfg.setdefault("media_res", DEFAULTS["media_res"])
    cm = cfg.get("collate_memo", "notice")                       # 0.6.3 까지의 켬/끔 → 고지 사항만 / 글 속
    cfg["collate_memo"] = ("notice" if cm else "inline") if isinstance(cm, bool) else (cm if cm in MEMO_LABELS else "notice")
    if cfg.get("memo_mode") not in MEMO_LABELS:
        cfg["memo_mode"] = "notice"
    if cfg.get("media_res") not in MRES_LABELS:
        cfg["media_res"] = DEFAULTS["media_res"]
    cfg["speed_defaults"] = SPEED_DEFAULTS_VERSION
    reader = cfg.get("reader", "gemini")
    if reader not in PROVIDER_LABELS:
        reader = cfg["reader"] = "gemini"
    profiles = cfg.get("provider_settings")
    if not isinstance(profiles, dict):
        profiles = {}
    cfg["provider_settings"] = profiles
    if reader not in profiles:
        remember_provider(cfg, reader, cfg.get("model", DEFAULT_MODELS.get(reader, "")),
                          cfg.get("api_key", ""), cfg.get("second_model", "auto"))
    cfg.update(provider_profile(cfg, reader))
    return cfg


def provider_profile(cfg: dict, reader: str) -> dict:
    """키·모델을 제공사별로 분리한다. 다른 제공사의 키를 기본값으로 가져오지 않는다."""
    saved = (cfg.get("provider_settings") or {}).get(reader) or {}
    if not isinstance(saved, dict): saved = {}
    return {"model": saved.get("model", DEFAULT_MODELS.get(reader, "")),
            "api_key": saved.get("api_key", ""), "second_model": saved.get("second_model", "auto")}


def remember_provider(cfg: dict, reader: str, model: str, key: str, second_model: str) -> None:
    cfg.setdefault("provider_settings", {})[reader] = {"model": model, "api_key": key, "second_model": second_model}


def load_config() -> dict:
    cfg = dict(DEFAULTS); raw: dict = {}
    try:
        got = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        cfg.update(got)
        raw = got if isinstance(got, dict) else {}
    except Exception:
        pass
    return upgrade_config(cfg, raw)


def save_config(cfg: dict) -> None:
    """설정 파일에는 API 키가 그대로 들어가므로 본인만 읽게(0o600) 만든다 — Windows 에서는 뜻이 없고 그 밖에서는 맞는 권한.
    임시 파일에 다 쓴 뒤 바꿔 넣는다(원자적) — 쓰는 도중 창이 죽어도 설정이 비거나 반쪽만 남지 않게."""
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_PATH.with_name(CONFIG_PATH.name + f".{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(cfg, ensure_ascii=False, indent=1))
        os.replace(tmp, CONFIG_PATH)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
    try:
        os.chmod(CONFIG_PATH, 0o600)                           # 이전 판이 만든 파일도
    except OSError:
        pass


def merge_config(disk: dict, loaded: dict, new: dict) -> dict:
    """파일에 얹을 설정: new(이 창이 쓰려는 값) 가운데 loaded(이 창이 읽었거나 마지막으로 저장한 값)와 다른 열쇠와 파일에 없는 열쇠만 disk 위에 쓴다.
    다른 창이 그 사이 저장한 값은 그대로 둔다(한 창의 메모리 설정 전체를 덮어쓰면 다른 창이 고친 양식·짜임이 되돌아간다).
    provider_settings(제공사별)·book_pending(결과 폴더가 아직 없는 책별)은 안의 열쇠마다 같은 규칙 — 이 창이 뺀 열쇠는 다른 창이 그 사이 바꾸지 않았을 때만 뺀다."""
    out = dict(disk)
    for k, v in new.items():
        if k not in NESTED_KEYS and (k not in out or v != loaded.get(k)):
            out[k] = v
    for key in NESTED_KEYS:
        if key not in new:
            continue
        had = loaded.get(key) if isinstance(loaded.get(key), dict) else {}
        got = dict(out.get(key)) if isinstance(out.get(key), dict) else {}
        mine = new.get(key) if isinstance(new.get(key), dict) else {}
        for p, v in mine.items():
            if p not in got or v != had.get(p):
                got[p] = v
        for p in set(had) - set(mine):
            if p in got and got[p] == had[p]:
                got.pop(p)
        out[key] = got
    return out


NESTED_KEYS = ("provider_settings", "book_pending")


def write_config(app) -> None:
    """창의 설정(app.cfg)을 파일에 저장한다. 창이 읽어 둔 값(app._cfg_loaded)이 있으면 파일을 새로 읽어 이 창이 바꾼 값만 얹는다.
    (vars 로 본다 — 덜 만들어진 Tk 창의 getattr 은 재귀하고, 시험의 가짜 창은 그 값이 없어 전처럼 통째로 쓴다.)"""
    loaded = vars(app).get("_cfg_loaded") if hasattr(app, "__dict__") else None
    if loaded is None:
        save_config(app.cfg); return
    save_config(merge_config(load_config(), loaded, app.cfg))
    app._cfg_loaded = copy.deepcopy(app.cfg)                   # 다음 저장은 이 시점 이후 바뀐 값만 얹는다(메모리 값은 그대로 — 화면 칸과 같다)


def missing_template(app) -> str:
    """내용별 짜임에 양식 경로가 적혀 있는데 그 파일이 없으면 그 경로, 아니면 "". (vars 로 본다 — 덜 만들어진 Tk 창의 getattr 은 재귀하고, 시험의 가짜 창은 칸이 없다.)"""
    d = vars(app) if hasattr(app, "__dict__") else {}
    if d.get("v_layout") is None or d.get("v_template") is None or app.layout_key() != "flow":
        return ""
    tpl = str(app.v_template.get() or "").strip()
    return tpl if tpl and not Path(tpl).is_file() else ""


def preset_ok(app) -> bool:
    """[▶ 한 번에 인식]·[④ 문서 만들기] 전에: 내용별 짜임인데 짜임 규칙을 쓸 수 없으면(규칙 파일이 없어짐·고장) 알리고 False — 모델을 부르기 전에 막는다
    (명령 쪽도 같은 검사로 끝 코드 2). 원본 쪽대로면 보지 않는다(쓰지 않는 규칙 때문에 막지 않게). vars 로 보는 까닭은 missing_template 과 같다."""
    d = vars(app) if hasattr(app, "__dict__") else {}
    if d.get("v_layout") is None or app.layout_key() != "flow":
        return True
    try:
        resolve_preset(App.preset_ref(app))
    except PresetError as e:
        messagebox.showerror(APP_NAME, f"짜임 규칙을 쓸 수 없습니다: {e}\n[설정] → 이 책 → 짜임 규칙을 다시 고르십시오.")
        return False
    return True


def template_ok(app) -> bool:
    """[▶ 한 번에 인식]·[④ 문서 만들기] 전에: 양식 파일이 없으면 알리고 False(모델을 부르기 전에 막는다 — 명령 쪽도 같은 검사로 끝 코드 2)."""
    tpl = missing_template(app)
    if not tpl:
        return True
    messagebox.showerror(APP_NAME, f"양식 파일이 없습니다: {tpl}\n[설정] → 이 책의 양식 HWPX 를 있는 파일로 고르거나 [비우기]를 누르십시오(비우면 기본 모양으로 만듭니다).")
    return False


def _pages_failed(problems: list[str] | None) -> bool:
    """끝 코드 3 이 쪽 판독 실패 때문인지('오류:' 줄 — 판독 실패·2차 덜 됨). 책갈피 교정의 3 은 '주의:' 만 남긴다(교정본 옮김·다시 계산·판독 덜 된 구획 건너뜀)."""
    return problems is None or any(p.startswith("오류:") and "쪽" in p and ("실패" in p or "덜 " in p) for p in problems)


def finish_words(rc, problems: list[str] | None = None) -> tuple[str, str, str]:
    """끝난 명령의 반환값 → (종류, 상태 글 꼬리, 진행 글의 머리). 0=끝, 3=끝났지만 실패한 쪽 있음(쪽 실패 없이 '주의:' 만이면 확인할 것 있음),
    4=끝났지만 3차 글자 판별을 못 마침, 130=중지, 그 밖=오류. 창 없이 검사할 수 있게 따로 둔다."""
    if rc == 0 or rc is None:
        return "ok", "끝", "100% · 완료"
    if rc == 3 and not _pages_failed(problems):
        return "partial", "끝 (확인할 것 있음 — 진행 기록 확인)", "100% · 완료(확인할 것 있음)"
    if rc == 3:
        return "partial", "끝 (실패한 쪽 있음 — 진행 기록 확인)", "100% · 완료(실패 쪽 있음)"
    if rc == 4:
        return "partial", "끝 (3차 글자 판별 못 마침 — 〔a/b〕 남음)", "100% · 완료(3차 판별 못 마침)"
    if rc == 5:
        return "partial", "끝 (문맥 교정 확인 필요 — 교정 기록 확인)", "100% · 완료(문맥 교정 확인 필요)"
    if rc == 130:
        return "stopped", "중지됨", "중지됨"
    return "error", "오류 (진행 기록 확인)", "멈춤(오류)"


PASSING_NOTES = ("주의: 요청 한도 초과 —",)   # 스스로 풀린 일시 대기(429 재시도) — 끝난 뒤 알림창에 올리면 정작 볼 줄을 밀어낸다


def finish_alert(rc, label: str, problems: list[str]) -> tuple[str, str] | None:
    """끝난 명령 뒤 알림창 ("warning"|"error", 글) 또는 None. 창 없이 검사할 수 있게 따로 둔다.
    성공(0)이어도 '주의:'·'오류:' 줄이 있으면 알린다(깨진 기록 치움 등). 책갈피 교정의 다시 계산·교정본 옮김·판독 덜 된 구획은 끝 코드 3(확인할 것 있음)이다."""
    kind = finish_words(rc)[0]
    ps = list(dict.fromkeys(p for p in problems if not p.startswith(PASSING_NOTES)))
    if not ps or kind == "stopped":
        return None
    if kind == "error":
        return "error", f"{label} 이(가) 중단되었습니다.\n\n" + "\n".join(ps[-3:])
    if rc == 4:
        head = f"{label} 이(가) 끝났지만 3차 글자 판별을 마치지 못해 〔a/b〕 표시가 남았습니다. 다시 실행하면 3차 판별을 이어서 합니다."
    elif rc == 5:
        head = f"{label} 이(가) 끝났지만 문맥으로 확정하지 못한 글자가 있습니다. 원래 글자는 보존했으며 출력 폴더의 교정 기록에서 원문·후보·문맥을 확인할 수 있습니다."
    elif kind == "partial" and _pages_failed(ps):
        head = f"{label} 이(가) 끝났지만 실패한 쪽이 있습니다. 다시 실행하면 그 쪽만 다시 시도합니다."
    else:
        head = f"{label} 이(가) 끝났습니다 — 확인할 것:"
    return "warning", head + "\n\n" + "\n".join(ps[-6:])


def keep_waiting(alive: bool, since: float, now: float, limit: float = CLOSE_WAIT_SEC) -> bool:
    """창을 닫을 때 작업 스레드를 더 기다릴지: 아직 살아 있고 한도 안이면 True."""
    return bool(alive) and (now - since) < limit


def missing_packages() -> list[str]:
    import importlib
    out = []
    for mod, pipname in PACKAGES:
        try:
            importlib.import_module(mod)
        except Exception:
            out.append(pipname)
    return out


def work_dir_for(pdf: str) -> Path:
    """결과 폴더: PDF 옆 '<이름>_YeonE_OCR'. 이미지는 '<이름>_<확장자>_YeonE_OCR', 이미지 폴더는 '<폴더>_이미지_YeonE_OCR'
    (같은 이름의 PDF·이미지·폴더가 한곳에 있어도 판독 기록이 섞이지 않게)."""
    p = Path(pdf)
    if p.is_dir():
        out = p.with_name(p.name + "_이미지_YeonE_OCR")
    elif p.suffix.lower() not in ("", ".pdf"):
        out = p.with_name(f"{p.stem}_{p.suffix.lower().lstrip('.')}_YeonE_OCR")
    else:
        out = p.with_name(p.stem + "_YeonE_OCR")
    return safe_output_path(out, p)


def latest_result_hwpx(pdf: str, work: Path) -> Path | None:
    """이 원본의 완성된 문서만 고른다. 중간 저장본이나 다른 문서는 열지 않는다."""
    stem = Path(pdf).stem
    partial = re.compile(re.escape(stem) + r"_p\d+-\d+\Z")
    candidates = []
    for path in (work / "out").glob("*.hwpx"):
        if path.stem != stem and not partial.fullmatch(path.stem):
            continue
        try:
            if path.is_file():
                candidates.append((path.stat().st_mtime_ns, path.name, path))
        except FileNotFoundError:  # 다른 작업에서 결과를 정리한 경우
            continue
    return max(candidates)[2] if candidates else None


def ocr_input_config(cfg: dict) -> tuple[dict, int]:
    """이전 단일 입력을 보존하면서 PDF와 이미지 탭의 입력 상태를 분리한다."""
    legacy = str(cfg.get("last_pdf") or "")
    initial = cfg.get("input_tab")
    if initial not in (0, 2):
        initial = 2 if Path(legacy).suffix.lower() in IMAGE_EXTS else 0
    saved = cfg.get("ocr_inputs")
    saved = saved if isinstance(saved, dict) else {}
    states = {}
    for tab in (0, 2):
        state = saved.get(str(tab))
        if not isinstance(state, dict):
            state = {"pdf": legacy if tab == initial else "",
                     "pages": cfg.get("pages", "") if tab == initial else "",
                     "print_offset": cfg.get("print_offset", "") if tab == initial else "",
                     "force": cfg.get("force", False) if tab == initial else False}
        states[tab] = {key: str(state.get(key) or "") for key in ("pdf", "pages", "print_offset")}
        states[tab]["force"] = bool(state.get("force", False))
    return states, initial


def ocr_mode_config(cfg: dict) -> dict[int, str]:
    """이미지 방향은 PDF 설정을 물려받지 않고 자동 판정으로 시작한다."""
    return {tab: cfg.get(key) if cfg.get(key) in MODE_LABELS else "auto"
            for tab, key in ((0, "mode"), (2, "image_mode"))}


SECRET_ENV = ("GEMINI_API_KEY", "GOOGLE_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY")


def child_env() -> dict:
    """바깥 프로그램(pip·xdg-open·open)에 넘길 환경 — API 키는 뺀다(설치 스크립트나 열린 프로그램이 키를 물려받지 않게)."""
    return {k: v for k, v in os.environ.items() if k.upper() not in SECRET_ENV}


def open_path(path: str | Path) -> None:
    path = str(path)
    if sys.platform.startswith("win"):
        with external_dll_search():
            os.startfile(path)                  # type: ignore[attr-defined]   (ShellExecute 는 환경을 따로 받지 않는다)
    elif sys.platform == "darwin":
        subprocess.Popen(["open", path], env=child_env())
    else:
        subprocess.Popen(["xdg-open", path], env=child_env())


def max_calls_value(s) -> int | None:
    """호출 상한 칸 → 0 이상의 정수(비우면 0 = 상한 없음). 숫자가 아니면 None(상한 없음으로 잘못 읽지 않게 실행을 막는다)."""
    t = str(s if s is not None else "").strip().replace(",", "")
    if not t:
        return 0
    return int(t) if t.isascii() and t.isdigit() else None


def fx_from_config(cfg: dict, today: str) -> tuple[tuple[float, str] | None, bool]:
    """설정에 남긴 자동 조회 환율(fx_auto·fx_auto_date) → (쓸 값 또는 None, 새로 조회할지). 하루 한 번만 조회한다.
    지난날 값도 조회가 실패하면 기본값보다 나으므로 날짜를 붙여 쓴다."""
    try:
        rate = float(cfg.get("fx_auto") or 0)
    except (TypeError, ValueError):
        rate = 0.0
    day = str(cfg.get("fx_auto_date") or "")
    if not 0 < rate < 1e7:
        return None, True
    if day == today:
        return (rate, "오늘 자동 조회"), False
    return (rate, f"{day} 자동 조회" if day else "이전 자동 조회"), True


FIG_KINDS = ("원형나경도", "산도", "계통도", "도식", "삽화", "복잡한표")


def table_rows(w: Path) -> list[tuple]:
    """쪽별 상태 표의 줄 (PDF 쪽, 인쇄 쪽, 상태, 확인 필요, 도판). 판독 기록을 모두 읽으므로(700쪽이면 몇 초) 작업 스레드에서 부른다."""
    try:
        st = json.loads((w / "state.json").read_text(encoding="utf-8"))
        pages = st.get("pages", {})
        total = st.get("total_pages") or (max((int(k) for k in pages), default=0))
    except Exception:
        return []
    rows = []
    for n in range(1, total + 1):
        p = pages.get(str(n), {})
        statuses = " · ".join(p.get("statuses", ["미처리"])) or "미처리"
        pr, unc, figs = "", "", ""
        rp = w / "readings" / f"p{n:03d}.json"
        if rp.exists():
            try:
                m = json.loads(rp.read_text(encoding="utf-8"))["merged"]
                pr = m.get("print_page", ""); unc = len(m.get("uncertain", []))
                figs = sum(1 for r in m["regions"] if r["kind"] in FIG_KINDS)
            except Exception:
                pass
        rows.append((n, pr, statuses, unc, figs))
    return rows


SCREENSHOT_NAME = re.compile(r"screenshot-([0-9a-fA-F]+)(_\d+)?\.png", re.IGNORECASE)


def _is_han(c: str) -> bool:
    return "\uac00" <= c <= "\ud7a3" or "\u4e00" <= c <= "\u9fff" or "\u3400" <= c <= "\u4dbf" or "\uf900" <= c <= "\ufaff"


def book_title(pdf: str) -> tuple[str, str, str]:
    """원본 경로 → (책 제목, 도장 두 글자, 파일 이름 줄). 제목은 원본 이름에서 확장자와 끝의 '.ocr' 를 뗀 것(「개보본초」),
    도장은 제목의 앞 두 한글·한자(둘이 안 되면 앞 두 글자 — 영문은 대문자). 스크린샷(screenshot-<지문>.png)은 제목 '스크린샷'·도장 '화면'·
    파일 이름 줄 'screenshot-<지문 앞 8자>….png', 이미지 폴더는 폴더 이름이 제목."""
    pdf = (pdf or "").strip()
    if not pdf:
        return "", "", ""
    p = Path(pdf); name = p.name
    m = SCREENSHOT_NAME.fullmatch(name)
    if m:
        fp = m.group(1)
        return "스크린샷", "화면", (f"screenshot-{fp[:8]}….png" if len(fp) > 8 else name)
    title = name if p.is_dir() else p.stem
    if not p.is_dir() and title.lower().endswith(".ocr"):
        title = title[:-4]
    title = title or name
    han = [c for c in title if _is_han(c)]
    chars = "".join(han[:2]) if len(han) >= 2 else "".join(c for c in title if not c.isspace())[:2].upper()
    return title, chars, name


def elide_middle(text: str, font, px: int) -> str:
    """글이 px 보다 넓으면 가운데를 '…' 로 줄인다(앞은 드라이브, 뒤는 파일 이름이 보이게 — 뒤를 조금 더 남긴다). font 는 measure 가 있는 tkfont.Font."""
    text = text or ""
    if px <= 0 or font.measure(text) <= px:
        return text
    lo, hi = 0, len(text)
    while lo < hi:                                                       # 남길 글자 수 k 의 가장 큰 값(이분 탐색)
        k = (lo + hi + 1) // 2
        head = k * 2 // 5
        if font.measure(text[:head] + "…" + text[len(text) - (k - head):]) <= px:
            lo = k
        else:
            hi = k - 1
    head = lo * 2 // 5
    return text[:head] + "…" + text[len(text) - (lo - head):] if lo else "…"


# 이 책의 설정 판: 짜임 규칙 이름 아래 한 줄 설명(예전 이름은 그 내장의 설명, 규칙 파일은 파일 이름)
PRESET_HINTS = {"basic": "대제목·중제목·소제목 · 문단-1~5", "regions": "제목 1~3단계 · 본문 · 소주 · 그림 제목", "kaibao": "약재명 제목 · 주석 · 출처 · 本經/別錄"}
# 실행 카드(디자인 명세 §6.1): 두 실행은 모두 판독부터 해서 돈이 든다(인주) — HWPX 가 주(채움 48), 문자 PDF 가 버금(테두리 40), [■ 중지]는 둘 아래 하나
RUN_TEXT, RUN_PDF_TEXT = "▶  한 번에 인식 (HWPX 만들기)", "▶  한 번에 인식 (문자 PDF 만들기)"
RUN_TIP = ("준비 → 판독(1차·2차) → 3차 글자 판별 → 문서 → 검수 화면 → 보고까지 이어서 합니다. "
           "먼저 판독·모델 단계의 예상 비용을 한 창에 보여 주고 묻습니다.")
RUN_PDF_TIP = ("판독(1차·2차) → 3차 글자 판별 → 원본 모양 그대로 찾기·복사가 되는 PDF(out\\<이름>_글자층.pdf)까지 이어서 합니다. "
               "양식·짜임·HWPX 자동 교정은 쓰지 않습니다. 먼저 예상 비용을 한 창에 보여 주고 묻습니다.")
STOP_TIP = "두 실행 모두 같은 단추로 멈춥니다 — 지금 쪽이 끝나면 멈춥니다. 끝난 쪽은 남고, 다시 누르면 남은 쪽부터 이어서 합니다."
READY_CAP = "누르면 먼저 예상 비용을 보여 주고 묻습니다. 멈춰도 끝난 쪽은 남고, 다시 누르면 남은 쪽부터 이어서 합니다."
RUN_CAP = "지금 쪽이 끝나면 멈춥니다 · 끝난 쪽은 남습니다."
EMPTY_CAP = "원본을 고르면 누를 수 있습니다. 누르면 먼저 예상 비용을 보여 주고 묻습니다."
COPY_NOTE = "실행 중에는 지난 결과의 읽기용 사본을 엽니다."
CLEARED_STATUS = "새 이미지나 스크린샷을 입력하십시오"                 # [입력 비우기] 뒤 실행 카드의 상태 줄
COPY_TIP = "실행 중에는 읽기용 사본을 엽니다 — 실행이 끝날 때 이 파일을 다시 쓰는데, 한글·PDF 뷰어가 쥐고 있으면 저장이 막히기 때문입니다."
TAG_TIP = "[아니요]면 뺌: 실행 전 비용 창에서 [아니요]를 누르면 이번 실행에서 빠지는 단계"
LEDGER_GROUPS = (("판독", "1차·2차", ("read1", "read2")), ("모델 단계", "", ("glyph", "suspect", "choice")))   # 장부 묶음(비용 창의 run 묶음과 같은 단계)
PAGE_CMDS = ("run", "run-pdf", "read")       # 쪽 띠를 쪽 알림(cli.PAGE_EVENT)으로 움직이는 명령 — 이 탭 원본의 쪽을 판독한다
# 설정 탭(디자인 명세 §6.3): 바닥 알림 줄의 평소 글 · 문서 짜임에 따라 바뀌는 줄 설명 · 책별 설정의 저장 상태(이 책의 설정 판과 같은 말)
SETTINGS_NOTE = "바꾼 설정은 다음 실행에 적용됩니다. [설정 저장]으로 다음 시작에도 유지할 수 있습니다."
PB_DESC = {False: "원본 쪽대로 만들 때 원본 한 쪽마다 한글 쪽을 나눔", True: "원본 쪽대로 만들 때만 · 이 책은 내용별이라 쓰지 않음"}
TPL_DESC = "내용별 문서를 채울 양식 · 비우면 기본 모양 · 이름에 마우스를 올리면 전체 경로"
PRESET_DESC = "판독한 제목·본문·주석을 문단 역할로 나눠 한글 스타일에 잇는 규칙 · 기본 규칙 = 대제목·중제목·소제목 · 문단-1~5"
BOLD_DESC = "굵은 글씨·보통 글씨를 획 굵기로 가르기 (本草: 本經·別錄) · 모델 호출 없음"
SAVE_LOOKS = {"saved": ("결과 폴더에 저장됨", "GREEN", "CAP_B", True), "changed": ("바뀜 — 실행하거나 [설정 저장]하면 남김", "WARN", "CAP_B", False),
              "unsaved": ("아직 저장 안 함 — 실행하거나 [설정 저장]하면 남김", "MUTED", "CAP", False),
              "pending": ("결과 폴더가 생기면(첫 실행) 저장", "MUTED", "CAP", False),
              "none": ("원본이 없어 아직 저장할 곳이 없습니다", "MUTED", "CAP", False)}


PATH_RUN = re.compile(r"(?:[A-Za-z]:)?[\\/](?:[^\\/·\n]+[\\/]){2,}[^\\/·\n]+")


def short_paths(text: str) -> str:
    """알림 줄에 넣을 글: 긴 경로는 끝의 두 칸만 남기고 앞을 '…' 로(설정 저장됨 → …\\.yeone_ocr\\config.json). 전체 글은 풍선 도움말에."""
    def cut(m):
        path = m.group(0).rstrip()
        parts = re.split(r"[\\/]", path)
        sep = "\\" if "\\" in path else "/"
        short = "…" + sep + sep.join(parts[-2:])
        return short + m.group(0)[len(path):]
    return PATH_RUN.sub(cut, text or "")


def finish_caption(rc, cmd: str, failed: bool, pages=()) -> str:
    """끝난 뒤 실행 카드의 캡션(디자인 명세 §7.3). cmd 는 끝난 명령(run · run-pdf · 그 밖), failed 는 끝 코드 3 이 쪽 판독 실패 때문인지,
    pages 는 실패한 쪽(앞 다섯까지 적음 — 큰 숫자 줄의 '실패 1쪽(9쪽)'과 같게)."""
    kind = finish_words(rc)[0]
    pdf = cmd == "run-pdf"
    again = "[문자 PDF 만들기]" if pdf else "[HWPX 만들기]"
    if cmd not in ("run", "run-pdf"):
        return READY_CAP
    if kind == "ok":
        return "끝났습니다. 문자 PDF를 열었습니다 — 〔 〕 자리는 검수 화면에서 보십시오." if pdf else "끝났습니다. 결과는 HWPX와 검수 화면에서 보십시오."
    if rc == 3 and failed:
        which = f"실패한 {', '.join(map(str, pages[:5]))}{'…' if len(pages) > 5 else ''}쪽만" if pages else "실패한 쪽만"
        return (f"{again}를 다시 누르면 {which} 다시 판독하고 " + ("문자 PDF를" if pdf else "문서를") + " 새로 만듭니다. 누르면 먼저 예상 비용을 묻습니다.")
    if rc == 3:
        return "진행 기록의 주의 줄을 확인하십시오."
    if rc == 4:
        return f"{again}를 다시 누르면 3차 판별을 이어서 합니다."
    if rc == 5:
        return "교정 기록은 [결과 폴더]의 out 폴더에 있습니다."
    if kind == "stopped":
        return ("판독 기록은 보존 — PDF 저장·검증 전에 멈추면 이전 완성 PDF를 그대로 둡니다. " if pdf else "") + f"멈춘 곳부터 이어서 합니다. {again}를 다시 누르면 남은 쪽부터."
    return "진행 기록의 오류: 줄을 확인하십시오."


def work_area(widget) -> tuple[int, int]:
    """창을 둘 수 있는 화면 크기(작업 표시줄을 뺀 것) — Windows 는 SPI_GETWORKAREA, 그 밖에는 화면 높이에서 작업 표시줄 몫을 뺀다."""
    if sys.platform.startswith("win"):
        try:
            import ctypes
            from ctypes import wintypes
            r = wintypes.RECT()
            if ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(r), 0):
                return r.right - r.left, r.bottom - r.top
        except Exception:
            pass
    return widget.winfo_screenwidth(), widget.winfo_screenheight() - 48


def place_over(win, app) -> None:
    """작은 창을 앱 창 가운데(조금 위)에 둔다 — 작업 영역 밖으로 나가지 않게."""
    win.update_idletasks()
    w, h = win.winfo_reqwidth(), win.winfo_reqheight()
    aw, ah = work_area(win)
    try:
        x = app.winfo_rootx() + (app.winfo_width() - w) // 2; y = app.winfo_rooty() + max(0, (app.winfo_height() - h) // 3)
    except tk.TclError:
        x, y = (aw - w) // 2, (ah - h) // 3
    win.geometry(f"+{max(0, min(x, aw - w))}+{max(0, min(y, ah - h))}")


class QueueWriter(io.TextIOBase):
    """작업 스레드의 출력을 창으로 보낸다. '오류:'·'주의:' 로 시작하는 줄은 따로 모아 끝난 뒤 알림창에 보여 준다(finish_alert).
    줄은 스레드마다 따로 모아 끝난 줄만 한 번에(창·모음 함께, 잠금 안에서) 보낸다 — print 는 글과 줄바꿈을 따로 쓰므로(write 두 번) 그사이
    다른 판독 스레드의 줄이 끼면 한 버퍼에서는 줄이 섞여 '주의: 요청 한도 초과 —' 뒤에 붙은 '오류:' 줄이 주의로 모이거나 주의 줄이 진행 줄 속에 숨었다."""
    def __init__(self, q: queue.Queue):
        self.q = q; self.problems: list[str] = []                   # (errors 는 TextIOBase 의 예약 속성이라 못 씀)
        self._lock = threading.Lock()                                # 판독 스레드 여럿이 함께 쓴다(창에 보낸 차례와 problems 차례가 같게)
        self._tls = threading.local()                                # 스레드마다: buf = 아직 줄바꿈이 오지 않은 글, shown = 그중 flush 로 창에 먼저 보낸 길이
    def write(self, s):
        if s:
            t = self._tls; buf = getattr(t, "buf", "") + s
            if "\n" in buf:
                cut = buf.rindex("\n") + 1; done = buf[:cut].split("\n")[:-1]
                with self._lock:
                    self.q.put(("log", buf[getattr(t, "shown", 0):cut]))
                    self.problems.extend(x.strip() for x in done if x.strip().startswith(("오류:", "주의:")))
                buf = buf[cut:]; t.shown = 0
            t.buf = buf
        return len(s)
    def flush(self):
        """줄바꿈 없이 flush 한 글(진행 점 등)은 창에만 먼저 보낸다. 오류·주의 판정은 그 줄이 끝날 때 통째로."""
        t = self._tls; buf = getattr(t, "buf", ""); shown = getattr(t, "shown", 0)
        if len(buf) > shown:
            with self._lock:
                self.q.put(("log", buf[shown:]))
            t.shown = len(buf)


class MarkDialog(tk.Toplevel):
    def __init__(self, app: "App"):
        super().__init__(app)
        self.app = app
        self.title("검수 결과 기록")
        self.resizable(False, False)
        self.configure(bg=app.colors["PANEL"]); f = ttk.Frame(self, style="Dialog.TFrame", padding=app.px(18)); f.grid()
        P = app.px; lp = dict(sticky="w", padx=(0, P(10)), pady=P(4))
        ttk.Label(f, style="Panel.TLabel", text="쪽 범위").grid(row=0, column=0, **lp)
        self.pages = ttk.Entry(f, width=16); self.pages.insert(0, app.v_pages.get()); self.pages.grid(row=0, column=1, sticky="w", pady=P(4))
        ttk.Label(f, style="Panel.TLabel", text="상태").grid(row=1, column=0, **lp)
        self.status = ttk.Combobox(f, values=STATUSES, state="readonly", width=24); self.status.current(0); self.status.grid(row=1, column=1, sticky="w", pady=P(4))
        ttk.Label(f, style="Panel.TLabel", text="메모").grid(row=2, column=0, **lp)
        self.note = ttk.Entry(f, width=40); self.note.grid(row=2, column=1, sticky="w", pady=P(4))
        self.clear = tk.BooleanVar(value=True)
        ttk.Checkbutton(f, text="'판독 확인 필요' 표시 지우기(사람이 확인한 뒤)", variable=self.clear).grid(row=3, column=0, columnspan=2, sticky="w", pady=(P(6), 0))
        b = ttk.Frame(f, style="Dialog.TFrame"); b.grid(row=4, column=0, columnspan=2, pady=(P(12), 0))
        ttk.Button(b, text="기록", command=self.ok, style="Accent.TButton").pack(side="left", padx=P(4))
        ttk.Button(b, text="닫기", command=self.destroy).pack(side="left", padx=P(4))
        self.transient(app); self.grab_set()

    def ok(self):
        argv = ["mark", self.app.pdf(), "--work", str(self.app.work()), "--pages", self.pages.get().strip(), "--status", self.status.get()]
        if self.note.get().strip():
            argv += ["--note", self.note.get().strip()]
        if self.clear.get():
            argv.append("--clear-check")
        self.destroy()
        self.app.start(argv, "검수 결과 기록")


class ModelDialog(tk.Toplevel):
    """키 확인 뒤 쓸 수 있는 모델 목록에서 고른다. 실제 호출이 확인된 모델을 미리 골라 둔다."""
    def __init__(self, app: "App", models: list[str], probe: dict | None = None, working: str = ""):
        super().__init__(app)
        self.app = app; self.title("쓸 수 있는 모델 고르기"); self.resizable(False, False)
        self.configure(bg=app.colors["PANEL"]); f = ttk.Frame(self, style="Dialog.TFrame", padding=app.px(18)); f.grid()
        cur = app.v_model.get().strip(); probe = probe or {}
        head = f"키 확인됨. 모델 {len(models)}개 중 고르십시오."
        if cur:
            head += f"\n지금 '{cur}': " + ("호출 확인됨" if probe.get(cur) == "호출 확인됨" else
                                      (probe[cur] if cur in probe else ("목록 확인됨 · 호출 전" if cur in models else "목록에 없음")))
        if working and working != cur:
            head += f"\n→ 호출 확인된 모델 '{working}' 을 미리 골라 두었습니다."
        P = app.px
        ttk.Label(f, style="Panel.TLabel", text=head, justify="left", wraplength=P(560)).grid(row=0, column=0, columnspan=2, sticky="w")
        ordered = [(m + ("   ✓ 호출 확인" if probe.get(m) == "호출 확인됨" else ("   ✗ 거부" if m in probe else ""))) for m in models]
        self._names = list(models)
        self.cb = ttk.Combobox(f, values=ordered, width=56, state="readonly")
        pick = working if working in models else (cur if cur in models else (models[0] if models else ""))
        if pick: self.cb.current(models.index(pick))
        self.cb.grid(row=1, column=0, columnspan=2, sticky="w", pady=P(8))
        ttk.Label(f, style="Panel.TLabel", text="이미지 입력을 지원하는 모델을 고르십시오. 모델별 품질·속도·API 요금이 다릅니다.").grid(row=2, column=0, columnspan=2, sticky="w")
        b = ttk.Frame(f, style="Dialog.TFrame"); b.grid(row=3, column=0, columnspan=2, pady=(P(12), 0))
        ttk.Button(b, text="이 모델로", command=self.ok, style="Accent.TButton").pack(side="left", padx=P(4))
        ttk.Button(b, text="닫기", command=self.destroy).pack(side="left", padx=P(4))
        self.transient(app); self.grab_set()

    def ok(self):
        i = self.cb.current()
        if i >= 0:
            name = self._names[i]
            self.app.v_model.set(name); self.app.save_settings(quiet=True)
            self.app.v_status.set(f"모델 '{name}' 로 설정됨")
        self.destroy()


class StylePicker(tk.Toplevel):
    """HWPX 스타일 목록에서 책갈피로 쓸 것을 고른다(여러 개, 고른 순서가 급)."""
    def __init__(self, app: "App", counts: list[tuple[str, int]]):
        super().__init__(app)
        self.app = app; self.title("책갈피로 쓸 스타일 고르기"); self.resizable(False, True)
        self.configure(bg=app.colors["PANEL"]); f = ttk.Frame(self, style="Dialog.TFrame", padding=app.px(18)); f.grid(sticky="nsew")
        ttk.Label(f, style="Panel.TLabel", text="문단에 쓰인 스타일입니다. 책갈피처럼 쓸 제목 스타일을 고르십시오(여러 개 가능, 고른 순서가 큰 단위 → 작은 단위).", wraplength=app.px(520), justify="left").grid(row=0, column=0, columnspan=2, sticky="w")
        self.lb = tk.Listbox(f, selectmode="multiple", height=min(18, max(6, len(counts))), width=48, exportselection=False, font=app.font_base, relief="flat", highlightthickness=1, highlightbackground=app.colors["LINE"], selectbackground=app.colors["SEL"], selectforeground=app.colors["INK"])
        self.names = [n for n, _ in counts]
        chosen = app.heading_styles_list()
        for i, (n, c) in enumerate(counts):
            self.lb.insert("end", f"{n}   ({c:,}개 문단)")
            if (chosen and n in chosen) or (not chosen and "제목" in n):
                self.lb.selection_set(i)
        P = app.px
        self.lb.grid(row=1, column=0, sticky="nsew", pady=(P(8), 0)); f.columnconfigure(0, weight=1); f.rowconfigure(1, weight=1)
        sb = ttk.Scrollbar(f, orient="vertical", command=self.lb.yview); self.lb.configure(yscrollcommand=sb.set); sb.grid(row=1, column=1, sticky="ns", pady=(P(8), 0))
        b = ttk.Frame(f, style="Dialog.TFrame"); b.grid(row=2, column=0, columnspan=2, pady=(P(12), 0))
        ttk.Button(b, text="이 스타일들을 책갈피로", command=self.ok, style="Accent.TButton").pack(side="left", padx=4)
        ttk.Button(b, text="취소", command=self.destroy).pack(side="left", padx=4)
        self.transient(app); self.grab_set()

    def ok(self):
        sel = [self.names[i] for i in self.lb.curselection()]
        if not sel:
            messagebox.showinfo(APP_NAME, "하나 이상 고르십시오."); return
        self.app.set_heading_styles(sel); self.destroy()


class PlanWait(tk.Toplevel):
    """비용 어림 중(작업 스레드) 작은 창 — 단계 글과 막대(전체를 모르면 오가는 막대). [취소]·창 닫기는 cancel 을 세울 뿐이고,
    어림 스레드가 단계·쪽 사이에서 멈춰 큐로 알리면 창(_plan_done)이 닫는다. 모양은 비용 창과 같은 종이 판·바탕 제목·먹 막대."""
    def __init__(self, app: "App", cancel: threading.Event):
        super().__init__(app)
        self.cancel = cancel
        C, P, F = app.colors, app.px, app.fonts
        self.title("비용 어림 중"); self.resizable(False, False)
        self.configure(bg=C["PANEL"], highlightthickness=1, highlightbackground=C["RULE"], highlightcolor=C["RULE"])
        f = tk.Frame(self, bg=C["PANEL"], padx=P(24), pady=P(18)); f.pack(fill="both", expand=True)
        tk.Label(f, text="비용 어림 중", font=F["DLG"], fg=C["INK"], bg=C["PANEL"], bd=0).pack(anchor="w")
        tk.Label(f, text="판독 기록을 읽어 호출·토큰을 셉니다 — 모델은 부르지 않습니다.", font=F["CAP"], fg=C["MUTED"], bg=C["PANEL"], bd=0).pack(
            anchor="w", pady=(P(2), P(12)))
        self.v_stage = tk.StringVar(value="판독 기록 읽는 중")
        tk.Label(f, textvariable=self.v_stage, font=F["BODY"], fg=C["INK"], bg=C["PANEL"], bd=0, anchor="w", width=44).pack(anchor="w")
        self.bar = ttk.Progressbar(f, style="Panel.Bar.Horizontal.TProgressbar", length=P(380), maximum=1000, mode="indeterminate")
        self.bar.pack(fill="x", pady=(P(8), P(14))); self.bar.start(15)
        row = tk.Frame(f, bg=C["PANEL"]); row.pack(fill="x")
        self.b_cancel = ttk.Button(row, text="취소", command=self.do_cancel, cursor="hand2"); self.b_cancel.pack(side="right")
        tk.Label(row, text="Esc · 취소", font=F["CAP"], fg=C["MUTED"], bg=C["PANEL"], bd=0).pack(side="right", padx=(0, P(10)))
        self.protocol("WM_DELETE_WINDOW", self.do_cancel); self.bind("<Escape>", lambda e: self.do_cancel())
        place_over(self, app)
        self.transient(app); self.grab_set()

    def update_progress(self, done: int, total: int, stage: str):
        if self.cancel.is_set():
            return
        self.v_stage.set(stage)
        if total:
            if str(self.bar.cget("mode")) != "determinate":
                self.bar.stop(); self.bar.configure(mode="determinate")
            self.bar.configure(value=int(1000 * done / max(1, total)))
        elif str(self.bar.cget("mode")) != "indeterminate":
            self.bar.configure(mode="indeterminate"); self.bar.start(15)

    def do_cancel(self):
        self.cancel.set(); self.v_stage.set("취소하는 중… (지금 단계가 끝나면 멈춥니다)"); self.b_cancel.configure(state="disabled")


class CostDialog(tk.Toplevel):
    """실행 전 비용 한 창(costplan): 도장·제목·명령 알약 → 큰 원화 금액과 달러 → 단계별 표 → 문맥 교정 건너뜀·어림하지 못한 것 → [아니요] 단락 →
    (펼침) 근거·가정 · 단가·환율 → 바닥 줄(펼침 단추 · 단추와 그 아래 캡션).
    표는 Label 격자다(Treeview 는 칸 안 줄바꿈·행 병합·꼬리표 그림을 못 그림): 묶음 머리(영수증 + 이름 + [아니요]면 뺌), costplan 의 한 줄을
    '단계 — 대상' 으로 갈라 같은 단계의 두 줄은 단계 칸을 합치고, 호출 0 인 줄은 흐리게 '$0.00', 돈 드는 금액은 굵게. 머리 줄·합계 줄은 고정이고
    몸통만 스크롤한다. 열 너비는 재서 정하고 근거가 남은 너비를 갖는다 — 창은 900 에서 필요한 만큼(작업 영역 − 80 까지) 넓어진다.
    단가·환율 고치기는 접어 두고, 고치면 다시 세지 않고 금액만 다시 낸다(price_plan). choice: 'all' | 'without' | None(취소·닫기).
    기본 단추(먹 고리 · 캡션 'Enter · …')는 costplan.default_choice — 금액을 모르거나 어림이 실패한 단계가 있으면 닫힌 쪽이고, 그때 돈 드는 단추는
    인주 채움을 빼고 테두리만 둔다. Enter 는 창 하나에서만 받는다: 단가 칸이면 다시 계산만, 초점이 있는 단추면 그 단추, 창 자체면 기본 단추. Esc 는 늘 [취소]·[닫기]."""
    BUTTONS = {"run": (("all", "예 · 모두 실행"), ("without", "아니요 · 모델 단계 없이")), "build": (("all", "예 · 문맥 교정에 모델"), ("without", "아니요 · 모델 없이")),
               "read": (("all", "실행"),), "adjudicate": (("all", "실행"),)}
    CAPTIONS = {"run": {"all": "모든 단계", "without": "판독·문서 만들기만"}, "build": {"all": "문맥 교정까지", "without": "규칙 변환만"},
                "read": {"all": "판독"}, "adjudicate": {"all": "3차 판별"}}
    PILLS = {"run": "한 번에 인식 · HWPX", "build": "④ 문서 만들기", "read": "③ 판독", "adjudicate": "3차 글자 판별"}
    # 묶음: (이름, 부제, [아니요]면 뺌 꼬리표를 붙일 수 있나, 든 단계)
    GROUPS = {"run": (("판독", "— [예]·[아니요] 어느 쪽이든 실행", False, ("read1", "read2")),
                      ("모델 단계", "— 판독 뒤 모델을 더 부르는 단계", True, ("glyph", "suspect", "choice"))),
              "build": (("문서 만들기", "— 문맥 교정(번체)", True, ("choice",)),),
              "read": (("판독", "", False, ("read1", "read2")),),
              "adjudicate": (("3차 판별 · 합의 의심", "— 판독 기록에서 갈린 자리를 묻습니다", False, ("glyph", "suspect")),)}
    COLS = ("step", "scope", "model", "basis", "calls", "tin", "tout", "cost")
    HEADS = {"step": "단계", "scope": "대상", "model": "모델", "basis": "근거", "calls": "호출", "tin": "입력 토큰", "tout": "출력 토큰", "cost": "비용"}
    NUMERIC = ("calls", "tin", "tout", "cost")
    STEP_NAMES = {"read": "판독", "glyph": "3차 판별", "suspect": "합의 의심", "choice": "문맥 교정", "plan": "전체"}
    TAG = "[아니요]면 뺌"
    TAG_TIP = "[아니요]면 뺌: 실행 전 비용 창에서 [아니요]를 누르면 이번 실행에서 빠지는 단계"
    WHY = ("3차 판별·합의 의심·문맥 교정은 판독 뒤 모델을 더 부르는 단계입니다. [아니요]면 이번 실행에서 그 단계 없이 판독·문서 만들기만 하고, "
           "〔a/b〕와 갈리는 번체 후보는 표시대로 남아 나중에 따로 할 수 있습니다.")
    WHY_PDF = ("3차 판별·합의 의심은 판독 뒤 모델을 더 부르는 단계입니다. [아니요]면 그 단계 없이 판독·문자 PDF 만들기만 하고, "
               "〔a/b〕 자리는 1차 글자로 글자층에 들어갑니다(나중에 3차 판별 뒤 고급 도구의 [PDF 글자층 넣기]로 다시 만들 수 있음).")
    WHY_BUILD = ("문서 만들기에서 모델을 부르는 것은 문맥 교정(번체 후보)뿐입니다(판독·3차 판별은 하지 않음). [아니요]면 모델 없이 규칙 변환(간체→번체·부호)만 하고 "
                 "갈리는 후보는 원문대로 두어 교정 보고에 남깁니다.")
    WHY_PREVIEW = "표는 실행할 때 이 창에서 [아니요]를 누르면 빠지는 단계입니다. 미리 보기는 아무것도 쓰지 않고 결과 폴더도 만들지 않습니다."
    NOTE_PDF = ("문자 PDF는 문서를 만들지 않아 문맥 교정(번체) 줄이 없습니다 — 글자층에는 판독한 글을 인쇄된 그대로 넣습니다"
                "(양식·짜임·자동 교정 없음).")
    NOTE_PREVIEW = "실행은 [▶ 한 번에 인식]의 두 단추에서 — 누를 때 다시 어림해 이 창으로 묻습니다."

    def __init__(self, app: "App", plan: dict, kind: str, preview: bool = False, pdf_only: bool = False):
        super().__init__(app)
        from .costplan import without_flags
        self.app, self.plan, self.kind, self.choice = app, plan, kind, None
        self.preview, self.default = preview, None
        self.pdf_only = bool(pdf_only) and kind == "run"                 # [▶ 한 번에 인식 (문자 PDF 만들기)] — 계획·기본 단추는 run 그대로, 글만 바꾼다
        self.no_flags = without_flags(kind, plan.get("context_model", True))    # [아니요]가 빼는 것(비면 [아니요] 단추·꼬리표가 없음)
        C, P, F = app.colors, app.px, app.fonts
        self.title("비용 미리 보기" if preview else "실행 전 비용 확인"); self.resizable(False, False)
        self.configure(bg=C["PANEL"], highlightthickness=1, highlightbackground=C["RULE"], highlightcolor=C["RULE"])
        bg = C["PANEL"]
        label = lambda parent, **kw: tk.Label(parent, bg=kw.pop("bg", bg), bd=0, highlightthickness=0, **kw)
        self._label = label
        f = self.body = tk.Frame(self, bg=bg, padx=P(24), pady=0); f.pack(fill="both", expand=True, pady=(P(18), P(16)))
        f.columnconfigure(0, weight=1)
        # ---- 머리: 도장 · 제목 · 파일 이름 ········· 명령 알약 ----
        top = self._top = tk.Frame(f, bg=bg); top.grid(row=0, column=0, sticky="ew")
        _title, chars, fname = book_title(app.pdf() if hasattr(app, "pdf") else "")
        if chars:
            app._seal(top, chars, large=False).pack(side="left", padx=(0, P(10)))
        label(top, text=self.title(), font=F["DLG"], fg=C["INK"]).pack(side="left")
        label(top, text=fname, font=F["BODY"], fg=C["MUTED"]).pack(side="left", padx=(P(10), 0), pady=(P(4), 0))
        pill = self.pill = "HWPX 만들기 어림" if preview else ("한 번에 인식 · 문자 PDF" if self.pdf_only else self.PILLS.get(kind, ""))
        if pill:
            ttk.Label(top, text=pill, style="Chip.TLabel").pack(side="right")
        # ---- 금액: 왼쪽 원화(바탕 큰 글) · 오른쪽 달러와 대상 ----
        amt = self._amt = tk.Frame(f, bg=bg); amt.grid(row=1, column=0, sticky="ew", pady=(P(6), 0)); amt.columnconfigure(0, weight=1)
        left = tk.Frame(amt, bg=bg); left.grid(row=0, column=0, sticky="sw")
        self.v_cap, self.v_head, self.v_usd, self.v_sub = tk.StringVar(), tk.StringVar(), tk.StringVar(), tk.StringVar()
        label(left, textvariable=self.v_cap, font=F["CAP"], fg=C["MUTED"], justify="left").pack(anchor="w")
        self.l_head = label(left, textvariable=self.v_head, font=F["TITLE"], fg=C["INK"], justify="left"); self.l_head.pack(anchor="w")
        self.warn = tk.Frame(left, bg=bg)                                # 금액을 모를 때: '단가 없는 모델: **…** — 아래 칸에 …'
        self.v_missing = tk.StringVar()
        label(self.warn, text="단가 없는 모델: ", font=F["BODY"], fg=C["WARN"]).pack(side="left")
        label(self.warn, textvariable=self.v_missing, font=F["BODY_B"], fg=C["WARN"]).pack(side="left")
        label(self.warn, text=" — 아래 칸에 단가를 넣으면 다시 계산합니다", font=F["BODY"], fg=C["WARN"]).pack(side="left")
        right = tk.Frame(amt, bg=bg); right.grid(row=0, column=1, sticky="se", padx=(P(16), 0))
        self.l_usd = label(right, textvariable=self.v_usd, font=F["USD"], fg=C["INK"]); self.l_usd.pack(anchor="e")
        label(right, textvariable=self.v_sub, font=F["BODY"], fg=C["MUTED"]).pack(anchor="e")
        tk.Frame(f, bg=C["LINE"], height=max(1, P(1))).grid(row=2, column=0, sticky="ew", pady=(P(8), 0))
        # ---- 표(머리 줄 · 스크롤 몸통 · 합계 줄) ----
        self.tbl = tk.Frame(f, bg=bg); self.tbl.grid(row=3, column=0, sticky="ew", pady=(P(8), 0))
        # ---- 표 아래: 문맥 교정 건너뜀(문자 PDF 는 문맥 교정 줄이 없다는 안내) · 어림 실패 ----
        self.v_skip, self.v_err, self.v_note, self.v_info = tk.StringVar(), tk.StringVar(), tk.StringVar(), tk.StringVar()
        self.l_skip = label(f, textvariable=self.v_skip, font=F["BODY"], fg=C["MUTED"] if self.pdf_only else C["INK"], justify="left", anchor="w")
        self.l_skip.grid(row=4, column=0, sticky="ew", pady=(P(8), 0))
        self.l_err = label(f, textvariable=self.v_err, font=F["BODY_B"], fg=C["RED"], justify="left", anchor="w")
        self.l_err.grid(row=5, column=0, sticky="ew", pady=(P(4), 0))
        self.wrapped = [self.l_skip, self.l_err]
        # ---- [아니요] 단락: 꼬리표 + 까닭(글 사이에 꼬리표를 넣으려고 읽기 전용 Text) ----
        why = (self.WHY_PREVIEW if preview else self.WHY_PDF if self.pdf_only else self.WHY if kind == "run" else self.WHY_BUILD if kind == "build" else "")
        self.why = None
        if why and self.no_flags:
            t = self.why = tk.Text(f, font=F["BODY"], fg=C["MUTED"] if preview else C["INK"], bg=bg, relief="flat", bd=0, highlightthickness=0, wrap="word",
                                   padx=0, pady=0, height=2, width=1, cursor="arrow", takefocus=0, spacing1=0, spacing2=P(2))
            tag = ttk.Label(t, text=self.TAG, style="Tag.TLabel"); Tip(tag, self.TAG_TIP, app)
            t.window_create("end", window=tag, align="center", padx=0); t.insert("end", "  " + why)
            t.configure(state="disabled")
            t.grid(row=6, column=0, sticky="ew", pady=(P(6), 0))
            t.bind("<Configure>", lambda e: self.after_idle(self._fit_why))
        # ---- 펼침: 근거·가정(줄마다의 자세한 수·어림의 가정·단가 출처) — 바닥 단추 줄 **아래**(펼쳐도 단추가 화면 밖으로 밀려나지 않게).
        #      읽기 전용 글: 창이 작업 영역을 넘으면 _fit 이 줄 수를 줄이고 이 칸만 스크롤한다 ----
        self.more = tk.Frame(f, bg=bg); self.more.columnconfigure(0, weight=1)
        self.t_more = tk.Text(self.more, font=F["CAP"], fg=C["MUTED"], bg=bg, relief="flat", bd=0, highlightthickness=0, wrap="word",
                              padx=0, pady=0, height=1, width=1, cursor="arrow", takefocus=0)
        self.t_more.grid(row=0, column=0, sticky="nsew")
        self.sb_more = ttk.Scrollbar(self.more, orient="vertical", command=self.t_more.yview); self.t_more.configure(yscrollcommand=self.sb_more.set)
        self._more_lh = max(1, app._font("CAP").metrics("linespace"))
        self.more_shown = False
        # ---- 펼침: 단가·환율 고치기(조용한 판) — 이 창에 나온 모델 아무것이나(단가 없는 모델을 앞에) ----
        self.adj = ttk.Frame(f, style="Quiet.TFrame", padding=(P(14), P(10)))
        q = C["QUIET"]
        row = self._adj_row = tk.Frame(self.adj, bg=q); row.pack(fill="x")
        self.v_pm = tk.StringVar()
        cap = lambda text: label(row, text=text, font=F["BODY"], fg=C["MUTED"], bg=q)
        cap("모델").pack(side="left", padx=(0, P(6)))
        self.cb_model = ttk.Combobox(row, textvariable=self.v_pm, state="readonly", width=12, style="Quiet.TCombobox"); self.cb_model.pack(side="left")
        self.cb_model.bind("<<ComboboxSelected>>", lambda e: self._fill_price())
        self.e_in, self.e_out, self.e_fx = (ttk.Entry(row, width=w, style="Quiet.TEntry") for w in (7, 7, 7))
        for text, ent in (("입력 $", self.e_in), ("출력 $", self.e_out), ("원·달러", self.e_fx)):
            cap(text).pack(side="left", padx=(P(12), P(6))); ent.pack(side="left")
        self.b_recalc = ttk.Button(row, text="이 값으로 다시 계산", command=self.save, style="QuietSmall.TButton", cursor="hand2")
        self.b_recalc.pack(side="left", padx=(P(12), 0))
        self.b_reset = ttk.Button(row, text="자동값으로", command=self.reset, style="QuietSmall.TButton", cursor="hand2")
        self.b_reset.pack(side="left", padx=(P(6), 0))
        hint = tk.Frame(self.adj, bg=q); hint.pack(fill="x", pady=(P(6), 0))
        self.l_adj = label(hint, font=F["CAP"], fg=C["MUTED"], bg=q, justify="left", anchor="w",
                           text="100만 토큰당 미국 달러. 선택한 제공사의 API 요금표를 확인해 입력하십시오. 고친 값은 그 모델에 저장되고 내장 요금표보다 먼저 씁니다. "
                                "이 칸에서 Enter는 다시 계산만 합니다(창을 닫거나 실행하지 않음).")
        self.l_adj.pack(fill="x")
        self.v_recalc = tk.StringVar()                                   # 단가 칸 Enter 뒤: '다시 계산했습니다 — 실행하려면 단추를 누르십시오'
        recalc = label(hint, textvariable=self.v_recalc, font=F["CAP_B"], fg=C["INK"], bg=q, anchor="w")
        self.v_recalc.trace_add("write", lambda *_: recalc.pack(fill="x") if self.v_recalc.get() else recalc.pack_forget())
        self.adj_shown = False
        # ---- 바닥 줄: 왼쪽 펼침 단추(· 미리 보기 안내) · 오른쪽 단추와 그 아래 캡션 ----
        tk.Frame(f, bg=C["LINE"], height=max(1, P(1))).grid(row=9, column=0, sticky="ew", pady=(P(12), 0))
        foot = self._foot = tk.Frame(f, bg=bg); foot.grid(row=10, column=0, sticky="ew", pady=(P(8), 0)); foot.columnconfigure(0, weight=1)
        tog = tk.Frame(foot, bg=bg); tog.grid(row=0, column=0, sticky="nw", pady=(P(12), 0))
        self.b_more = ttk.Button(tog, text="근거·가정 ▾", command=self.toggle_more, style="Toggle.TButton", cursor="hand2"); self.b_more.pack(side="left")
        self.b_adj = ttk.Button(tog, text="단가·환율 고치기 ▾", command=self.toggle_adj, style="Toggle.TButton", cursor="hand2")
        self.b_adj.pack(side="left", padx=(P(12), 0))
        Tip(self.b_adj, "100만 토큰당 미국 달러 · 고친 값은 그 모델에 저장됩니다", app)
        if preview:
            label(tog, text=self.NOTE_PREVIEW, font=F["CAP"], fg=C["MUTED"], justify="left", wraplength=P(230)).pack(side="left", padx=(P(14), 0))
        bf = tk.Frame(foot, bg=bg); bf.grid(row=0, column=1, sticky="e", padx=(P(12), 0))
        self.btns, self.caps = {}, {}
        spec = list(() if preview else self.BUTTONS.get(kind, (("all", "실행"),)))
        if not self.no_flags:
            spec = [(ch, text) for ch, text in spec if ch != "without"]   # 뺄 것이 없으면 [아니요]는 [예]와 같은 일 — 보이지 않는다
        for ch, text in spec + [(None, "닫기" if preview else "취소")]:
            col = tk.Frame(bf, bg=bg); col.pack(side="left", padx=(P(2), 0))
            self.btns[ch] = ttk.Button(col, text=text, command=lambda c=ch: self.done(c), cursor="hand2", style="Dlg.TButton")
            self.btns[ch].pack()
            self.caps[ch] = label(col, text="", font=F["CAP"], fg=C["MUTED"]); self.caps[ch].pack(pady=(P(2), 0))
        self.render()
        if self.priced["totals"]["missing_prices"]:
            self.toggle_adj()                                           # 금액을 낼 수 없으면 단가 칸을 펼쳐 둔다(넣으면 다시 계산하고 기본 단추도 다시 정한다)
        self.bind("<Return>", self._on_return); self.bind("<KP_Enter>", self._on_return)
        self.bind("<Escape>", lambda e: (self.done(None), "break")[1])   # Esc 는 늘 [취소]·[닫기]
        for ev in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            self.bind(ev, self._wheel)                                  # 휠은 창 하나에서(표 칸·글 위에서 굴려도 표 몸통이 움직인다)
        self.bind("<Prior>", lambda e: self._page(-1)); self.bind("<Next>", lambda e: self._page(1))   # 키보드: PgUp·PgDn 으로 표 몸통을
        self.protocol("WM_DELETE_WINDOW", lambda: self.done(None))
        self._place()
        self.transient(app); self.grab_set(); self.btns[self.default].focus_set()

    # ----- 기본 단추·Enter -----
    def _set_default(self):
        """기본(Enter·고리) 단추를 costplan.default_choice 로 — 어림 실패·금액을 낼 수 없는 단계가 있으면 닫힌 쪽. 단가를 넣어 다시 계산하면 다시 정한다.
        돈 드는 단추([예]·[실행])는 기본일 때(금액을 앎)만 인주 채움, 아니면 인주 테두리. 캡션 맨 앞 'Enter · ' 는 기본 단추에만."""
        from .costplan import default_choice
        default = default_choice(self.priced)
        if default == "without" and "without" not in self.btns:
            default = "cancel"                                          # [아니요]가 없는 창(판독·3차 판별·뺄 것 없는 문서 만들기) — 닫힌 쪽(취소)으로
        self.default = None if self.preview or default == "cancel" else default
        C = self.app.colors
        for ch, btn in self.btns.items():
            if ch == "all":
                style = "DlgSealDef.TButton" if self.default == "all" else "DlgSealOut.TButton"
            else:
                style = "DlgDef.TButton" if ch == self.default else "Dlg.TButton"
            btn.configure(style=style)
            if ch is None:
                base = "Esc 닫기" if self.preview else "Esc · 하지 않음"
            else:
                base = "판독·문자 PDF만" if (ch == "without" and self.pdf_only) else self.CAPTIONS.get(self.kind, {}).get(ch, "")
            on = ch == self.default
            self.caps[ch].configure(text=("Enter · " + base) if on else base, fg=C["INK"] if on else C["MUTED"])

    def _on_return(self, e=None):
        """Enter 는 창 하나에서만 받는다(단추마다 묶지 않음 — 펼침 단추에서 친 Enter 가 기본 단추까지 누르지 않게).
        단가·환율 칸이면 다시 계산만(창을 닫거나 실행하지 않고 초점은 칸에), 모델 고르기 칸이면 아무것도, 초점이 단추면 그 단추, 창 자체면 기본 단추."""
        w = self.focus_get()
        if w in (self.e_in, self.e_out, self.e_fx):
            self.save()                                                 # render() 가 _set_default() 를 다시 부름 — 기본 단추가 바뀔 수 있음
            self.v_recalc.set("다시 계산했습니다 — 실행하려면 단추를 누르십시오")
            return "break"
        if w is self.cb_model:
            return "break"
        if isinstance(w, ttk.Button):
            w.invoke(); return "break"
        self.done(self.default)
        return "break"

    # ----- 그리기 -----
    def render(self):
        from .costplan import price_plan, skipped_lines
        from .estimate import PRICE_SOURCES, PRICES_DATE, PRICES_DATE_GEMINI, fmt_krw, resolve_price
        p = self.priced = price_plan(self.plan, self.app.cfg.get("prices") or {}, *self.app.fx_setting())
        t = p["totals"]
        self._set_default()
        self.v_cap.set("예상 비용 (판독 + 모델 단계, 범위는 생각 토큰·재질문 편차)"
                       + (" — 어림하지 못한 단계(아래 빨간 줄)는 이 금액에 빠져 있습니다" if p["errors"] and p["rows"] else ""))
        self.warn.pack_forget()
        if not p["rows"] and p["errors"]:
            self.v_head.set("비용을 어림하지 못했습니다"); self.v_usd.set("")
        elif t["usd"] is not None:
            self.v_head.set(f"약 {fmt_krw(p['krw'][0])} ~ {fmt_krw(p['krw'][1])}"); self.v_usd.set(f"$ {t['usd'][0]:.2f} ~ {t['usd'][1]:.2f}")
        else:
            self.v_head.set("금액을 다 낼 수 없음")
            self.v_missing.set(", ".join(t["missing_prices"])); self.warn.pack(anchor="w")
            k = t["usd_known"]
            self.v_usd.set(f"아는 부분만 약 {fmt_krw(p['krw_known'][0])}~{fmt_krw(p['krw_known'][1])} · $ {k[0]:.2f}~{k[1]:.2f}" if k[1] else "")
        self.l_usd.configure(font=self.app.fonts["USD" if t["usd"] is not None else "BODY_B"])
        sub = [f"대상 {p['pages']:,}쪽", f"판독 기록 {p['records']:,}쪽"]
        if self.kind in ("run", "read"):
            sub.append(f"새로 읽을 {p['to_read']:,}쪽")
        if p.get("models"):
            sub.append("모델 " + ", ".join(p["models"]))
        self.v_sub.set(" · ".join(sub))
        missing = t["missing_prices"]
        models = [m for m in missing] + [m for m in p.get("models") or [] if m not in missing]
        self.cb_model.configure(values=models, width=max(12, min(24, max((len(m) for m in models), default=0) + 2)))   # 시안처럼 좁게 — 긴 모델 이름만 넓힘(표 너비를 재기 전에)
        self._draw_table(p)
        self.v_skip.set(self.NOTE_PDF if self.pdf_only else "\n".join(skipped_lines(p)))
        self.v_err.set("\n".join(f"어림하지 못함: {self.STEP_NAMES.get(e['step'], e['step'])} — {e['reason']}" for e in p["errors"]))
        for lab, v in ((self.l_skip, self.v_skip), (self.l_err, self.v_err)):
            (lab.grid if v.get() else lab.grid_remove)()                # 빈 줄은 자리를 차지하지 않게
        self.v_note.set("\n".join([f"· {r['label']}: {r['detail']}" for r in p["rows"] if r.get("detail")] + ["· " + n for n in p.get("notes") or []]))
        self.v_info.set(f"환율 {p['fx']:,.0f}원/달러({p['fx_source']}) · 단가(USD/100만 토큰) — " + " | ".join(p.get("price_lines") or ["-"])
                        + f"\n내장 요금표: Gemini {PRICES_DATE_GEMINI}({PRICE_SOURCES['gemini']}) · Anthropic {PRICES_DATE}({PRICE_SOURCES['anthropic']}) 기준"
                        + ". 표에 없는 모델(OpenAI·gemini-…-latest 별칭 등)은 [단가·환율 고치기]에 넣으면 계산합니다.")
        tm = self.t_more
        tm.configure(state="normal"); tm.delete("1.0", "end")
        tm.insert("end", "\n".join(x for x in (self.v_note.get(), self.v_info.get()) if x)); tm.configure(state="disabled")
        if models and self.v_pm.get() not in models:
            self.v_pm.set(models[0])
        self._fill_price(resolve_price)
        self._fit()

    def _cells(self, p: dict) -> list:
        """표 몸통의 줄: ('group', 이름, 부제, 꼬리표) 또는 ('row', {칸: 값}, 꾸밈). 같은 단계가 이어지면 첫 줄만 단계 이름을 갖고 span 을 센다."""
        from .estimate import fmt_tokens
        span = lambda v, f=lambda x: f"{x:,}": f(v[0]) if v[0] == v[1] else f"{f(v[0])}~{f(v[1])}"
        groups = self.GROUPS.get(self.kind, self.GROUPS["run"])
        buckets = [[] for _ in groups]
        for r in p["rows"]:
            k = next((i for i, g in enumerate(groups) if r.get("step") in g[3]), len(groups) - 1)
            buckets[k].append(r)
        out = []
        for (name, sub, can_tag, _steps), rows in zip(groups, buckets):
            if not rows:
                continue
            out.append(("group", name, sub, bool(can_tag and self.no_flags)))
            for i, r in enumerate(rows):
                step, _, scope = r["label"].partition(" — ")
                first = i == 0 or rows[i - 1]["label"].partition(" — ")[0] != step
                n = 1
                while first and i + n < len(rows) and rows[i + n]["label"].partition(" — ")[0] == step:
                    n += 1
                last = i + 1 == len(rows) or rows[i + 1]["label"].partition(" — ")[0] != step
                zero = not r["calls"][1]
                if r["usd"] is None:
                    cost, look = "단가 미입력", "warn"
                else:
                    lo, hi = f"{r['usd'][0]:.2f}", f"{r['usd'][1]:.2f}"
                    cost, look = (f"${lo}" if lo == hi else f"${lo}~{hi}"), ("zero" if zero else "money")
                out.append(("row", {"step": step if first else None, "scope": scope, "model": r["model"], "basis": r["basis"], "calls": span(r["calls"]),
                                    "tin": span(r["in"], fmt_tokens), "tout": span(r["out"], fmt_tokens), "cost": cost},
                            {"zero": zero, "cost": look, "span": n if first else 0, "last": last}))
        return out

    def _total_cells(self, p: dict) -> dict:
        from .estimate import fmt_tokens
        t = p["totals"]
        span = lambda v, f=lambda x: f"{x:,}": f(v[0]) if v[0] == v[1] else f"{f(v[0])}~{f(v[1])}"
        if t["usd"] is not None:
            cost = f"${t['usd'][0]:.2f}~{t['usd'][1]:.2f}"
        elif t["usd_known"][1]:
            cost = f"${t['usd_known'][0]:.2f}~{t['usd_known'][1]:.2f}+"
        else:
            cost = "단가 미입력"
        return {"calls": span(t["calls"]), "tin": span(t["in"], fmt_tokens), "tout": span(t["out"], fmt_tokens), "cost": cost}

    def _widths(self, cells: list, total: dict) -> dict:
        """열 너비(SPEC §5.8): 머리 글(굵게)과 모든 칸 값을 재고 좌우 안쪽 5+5 를 더한다. 모델은 170 에서 접고, 근거는 남은 너비(최소 150)."""
        from tkinter import font as tkfont
        P, F = self.app.px, self.app.fonts
        reg, bold = tkfont.Font(self, font=F["BODY"]), tkfont.Font(self, font=F["BODY_B"])
        pad = 2 * P(5)                                                  # 칸의 좌우 안쪽(padx=P(5)) — P(10) 이면 150%에서 반올림으로 1px 모자라 열이 밀렸다
        need = {c: bold.measure(self.HEADS[c]) for c in self.COLS}
        for kind, *rest in cells:
            if kind != "row":
                continue
            vals, look = rest
            for c in self.COLS:
                v = vals.get(c)
                if v:
                    f = bold if c == "step" or (c == "cost" and look["cost"] == "money") else reg
                    need[c] = max(need[c], f.measure(str(v)))
        need["step"] = max(need["step"], bold.measure("합계"))
        for c, v in total.items():
            need[c] = max(need[c], bold.measure(v))
        w = {c: need[c] + pad for c in self.COLS}
        w["model"] = min(w["model"], P(170))                            # 긴 모델 이름(claude-sonnet-4-5-20250929)은 170 에서 글자 단위로 접음
        fixed = sum(v for c, v in w.items() if c != "basis")
        self.gutter = P(14)                                             # 스크롤 막대 자리(막대가 없으면 _fit 이 근거 열에 돌려준다)
        avail = work_area(self)[0] - P(80)
        dlg_w = min(max(P(900), fixed + P(150) + P(48) + self.gutter, self._rows_width() + P(48)), avail)
        basis = dlg_w - P(48) - self.gutter - fixed
        for c in ("scope", "step"):                                     # 아주 좁은 화면: 대상 → 단계 순으로 두 줄 접기를 허락하고 다시 셈
            if basis >= P(150):
                break
            give = min(w[c] - max(P(70), w[c] // 2 + pad), P(150) - basis)
            if give > 0:
                w[c] -= give; basis += give
        w["basis"] = max(P(80), basis)
        return w

    def _rows_width(self) -> int:
        """표 밖의 접히지 않는 줄(머리·금액·단가 칸 줄·바닥 단추 줄) 가운데 가장 넓은 것 — 표가 이보다 좁으면 표 오른쪽 끝이 단가 판·선·'대상 … 모델'
        줄보다 짧게 끝난다(단가 칸은 접혀 있어도 잼 — 펼칠 때 창 너비가 바뀌지 않게)."""
        self.update_idletasks()
        try:
            return max(self._top.winfo_reqwidth(), self._amt.winfo_reqwidth(), self._foot.winfo_reqwidth(),
                       self._adj_row.winfo_reqwidth() + 2 * self.app.px(14))
        except tk.TclError:
            return 0

    def _draw_table(self, p: dict):
        C, P, F = self.app.colors, self.app.px, self.app.fonts
        for child in self.tbl.winfo_children():
            child.destroy()
        self.cv = self.inner = self.sb = None; self.grids = ()
        if not p["rows"]:
            self.tbl.grid_remove(); self.content_w = max(P(852), min(self._rows_width(), work_area(self)[0] - P(128))); self._wrap_all(); return
        self.tbl.grid()
        cells, total = self._cells(p), self._total_cells(p)
        w = self.col_w = self._widths(cells, total)
        table_w = sum(w.values())
        self.content_w = table_w + self.gutter
        label = self._label
        num = lambda c: c in self.NUMERIC
        line = lambda parent, color, r, c0=0, h=1: tk.Frame(parent, bg=color, height=max(1, P(h))).grid(row=r, column=c0, columnspan=8 - c0, sticky="ew")

        def columns(g):
            for i, c in enumerate(self.COLS):
                g.columnconfigure(i, minsize=w[c], weight=0)

        def cell(parent, r, c, text, font, fg, bg, **kw):
            lab = label(parent, text=text, font=font, fg=fg, bg=bg, anchor="e" if num(c) else "w", justify="left", padx=P(5), pady=P(4),
                        wraplength=w[c] - 2 * P(5) if c in ("step", "scope", "model", "basis") else 0)
            lab.grid(row=r, column=self.COLS.index(c), sticky="nsew", **kw)
            return lab

        # 머리 줄(고정)
        head = tk.Frame(self.tbl, bg=C["PANEL"]); head.grid(row=0, column=0, sticky="w")
        hg = tk.Frame(head, bg=C["QUIET"]); hg.grid(row=0, column=0, sticky="w"); columns(hg)
        for c in self.COLS:
            label(hg, text=self.HEADS[c], font=F["CAP_B"], fg=C["MUTED"], bg=C["QUIET"], anchor="e" if num(c) else "w", padx=P(5), pady=P(5)).grid(
                row=0, column=self.COLS.index(c), sticky="nsew")
        line(hg, C["LINE"], 1)
        # 몸통(넘치면 이것만 스크롤)
        mid = self._mid = tk.Frame(self.tbl, bg=C["PANEL"]); mid.grid(row=1, column=0, sticky="w")
        cv = self.cv = tk.Canvas(mid, bg=C["PANEL"], highlightthickness=0, bd=0, width=table_w, height=P(100), yscrollincrement=P(20))
        cv.grid(row=0, column=0, sticky="nw")
        self.sb = ttk.Scrollbar(mid, orient="vertical", command=cv.yview); cv.configure(yscrollcommand=self.sb.set)
        mid.columnconfigure(1, minsize=self.gutter)
        g = self.inner = tk.Frame(cv, bg=C["PANEL"]); cv.create_window(0, 0, window=g, anchor="nw"); columns(g)
        g.bind("<Configure>", lambda e: cv.configure(scrollregion=(0, 0, e.width, e.height)))
        r = 0
        for item in cells:
            if item[0] == "group":
                _, name, sub, tag = item
                gh = tk.Frame(g, bg=C["PANEL"]); gh.grid(row=r, column=0, columnspan=8, sticky="ew", padx=P(5), pady=(P(5), P(2)))
                label(gh, image=self.app.icons["receipt"], text=" " + name, compound="left", font=F["BODY_B"], fg=C["INK"]).pack(side="left")
                if tag:
                    tg = ttk.Label(gh, text=self.TAG, style="Tag.TLabel"); tg.pack(side="left", padx=(P(6), 0)); Tip(tg, self.TAG_TIP, self.app)
                if sub:
                    label(gh, text=sub, font=F["CAP"], fg=C["MUTED"]).pack(side="left", padx=(P(6), 0))
                line(g, C["LINE"], r + 1); r += 2
                continue
            _, vals, look = item
            fg = C["MUTED"] if look["zero"] else C["INK"]
            if vals["step"] is not None:
                cell(g, r, "step", vals["step"], F["BODY_B"], C["INK"], C["PANEL"], rowspan=2 * look["span"] - 1).configure(anchor="nw")
            for c in self.COLS[1:]:
                if c == "cost":
                    font = F["BODY_B"] if look["cost"] == "money" else F["BODY"]
                    cfg = C["WARN"] if look["cost"] == "warn" else fg
                    cell(g, r, c, vals[c], font, cfg, C["PANEL"])
                else:
                    cell(g, r, c, vals[c], F["BODY"], fg, C["PANEL"])
            if look["last"]:
                line(g, C["LINE"], r + 1)                               # 단계 사이: LINE 온 너비
            else:
                line(g, C["HAIR"], r + 1, c0=1)                         # 같은 단계 안: HAIR(단계 칸은 건너뜀)
            r += 2
        # 합계 줄(고정): 위 겹줄(먹 1px 둘, 사이 2px)
        foot = tk.Frame(self.tbl, bg=C["PANEL"]); foot.grid(row=2, column=0, sticky="w")
        tg = tk.Frame(foot, bg=C["QUIET"]); tg.grid(row=0, column=0, sticky="w"); columns(tg)
        line(tg, C["INK"], 0); tk.Frame(tg, bg=C["QUIET"], height=P(2)).grid(row=1, column=0, columnspan=8, sticky="ew"); line(tg, C["INK"], 2)
        label(tg, text="합계", font=F["BODY_B"], fg=C["INK"], bg=C["QUIET"], anchor="w", padx=P(5), pady=P(5)).grid(row=3, column=0, columnspan=4, sticky="nsew")
        for c, v in total.items():
            cell(tg, 3, c, v, F["BODY_B"], C["WARN"] if v == "단가 미입력" else C["INK"], C["QUIET"]).configure(pady=P(5))
        self.grids = (hg, g, tg)                                        # 머리 줄 · 몸통 · 합계 줄(열 너비가 같아야 칸이 맞는다)
        self._wrap_all()

    def _wrap_all(self):
        """표 아래 글·펼침 글을 창 안 너비에 맞춰 접는다."""
        for lab in self.wrapped:
            lab.configure(wraplength=self.content_w)
        self.l_adj.configure(wraplength=self.content_w - self.app.px(28))
        self.body.columnconfigure(0, minsize=self.content_w)

    def _wheel(self, e):
        """창 어디서 굴려도 표 몸통을(창에 묶음). 근거·가정 글 위면 그 글이 스스로 구르고(Text 클래스 묶음), 모델 고르기 칸은 제 동작대로 둔다."""
        w = getattr(e, "widget", None)
        if w is self.t_more or w is self.cb_model:
            return None
        if self.cv is None or self.sb is None or not self.sb.winfo_ismapped():
            return "break"
        step = -1 if (getattr(e, "num", 0) == 4 or getattr(e, "delta", 0) > 0) else 1
        self.cv.yview_scroll(step, "units"); return "break"

    def _page(self, n: int):
        """PgUp·PgDn: 표 몸통을 한 화면씩(넘칠 때만)."""
        if self.cv is not None and self.sb is not None and self.sb.winfo_ismapped():
            self.cv.yview_scroll(n, "pages")
        return "break"

    def _fit_why(self):
        """[아니요] 단락(Text)의 높이를 접힌 줄 수에 맞춘다."""
        t = self.why
        if t is None or not t.winfo_exists():
            return
        try:
            t.tk.call(t._w, "sync")
            n = t.count("1.0", "end", "displaylines")
            n = n[0] if isinstance(n, tuple) else int(n or 1)
        except (tk.TclError, TypeError, IndexError):
            return
        if n and int(t.cget("height")) != n:
            t.configure(height=n); self.after_idle(self._fit)

    def _more_lines(self) -> int:
        """근거·가정 글이 지금 너비에서 접히는 줄 수."""
        t = self.t_more
        try:
            t.tk.call(t._w, "sync")
            n = t.count("1.0", "end", "displaylines")
            return max(1, n[0] if isinstance(n, tuple) else int(n or 1))
        except (tk.TclError, TypeError, IndexError):
            return 1

    def _fit(self):
        """높이(명세 §6.2): 작업 영역 − 40(창 제목 줄 몫)을 넘으면 표 몸통(최소 90)을 줄여 몸통만 스크롤하고, 펼친 근거·가정(바닥 줄 아래)도 넘치면
        그 글만 스크롤(최소 두 줄) — 머리·금액·단락·펼친 단가 칸·바닥 단추 줄은 늘 보인다. 펼치고 접을 때마다 불려 창을 작업 영역 안으로 다시 들인다."""
        P = self.app.px
        self.update_idletasks()
        cv_h = self.cv.winfo_reqheight() if self.cv is not None else 0
        more_h = self.t_more.winfo_reqheight() if self.more_shown else 0
        room = work_area(self)[1] - P(40) - (self.winfo_reqheight() - cv_h - more_h)   # 표 몸통과 근거·가정 글이 나눠 쓸 높이
        lines = self._more_lines() if self.more_shown else 0
        lh = self._more_lh
        if self.cv is not None:
            inner = self.inner.winfo_reqheight()
            floor = max(P(48), min(P(90), room - min(lines, 2) * lh))   # 몸통 최소 90 — 아주 작은 화면(1366×768 125%의 단가 없는 창)이면 한두 줄까지
            h = max(floor, min(inner, room - lines * lh))               # 근거·가정을 펼치면 몸통이 먼저 줄어든다
            self.cv.configure(height=h)
            bar = inner > h + 1
            if bar:
                self.sb.grid(row=0, column=1, sticky="ns", padx=(P(4), 0))
            else:
                self.sb.grid_remove(); self.cv.yview_moveto(0)
            self._gutter(bar)
            room -= h
        if self.more_shown:
            n = min(lines, max(2, room // lh))
            self.t_more.configure(height=n)
            if n < lines:
                self.sb_more.grid(row=0, column=1, sticky="ns", padx=(P(4), 0))
            else:
                self.sb_more.grid_remove(); self.t_more.yview_moveto(0)
        if getattr(self, "_placed", False):
            self._keep_on_screen()

    def _gutter(self, bar: bool):
        """스크롤 막대 자리: 막대가 없으면 그 너비를 근거 열에 돌려 표(머리·몸통·합계)가 창 안 너비를 다 쓴다 — 비용 열이 금액·단추 오른쪽 끝과 맞게."""
        extra = 0 if bar else self.gutter
        i = self.COLS.index("basis")
        for g in self.grids:
            g.columnconfigure(i, minsize=self.col_w["basis"] + extra)
        self.cv.configure(width=sum(self.col_w.values()) + extra)
        self._mid.columnconfigure(1, minsize=self.gutter if bar else 0)

    def _keep_on_screen(self):
        """펼쳐서 커진 창이 작업 영역(작업 표시줄 위) 밖으로 나가면 위·왼쪽으로 당긴다(맨 위·맨 왼쪽은 넘지 않음)."""
        self.update_idletasks()
        aw, ah = work_area(self)
        dy = self.winfo_rooty() + self.winfo_reqheight() - ah
        dx = self.winfo_rootx() + self.winfo_reqwidth() - aw
        if dy > 0 or dx > 0:
            x, y = self.winfo_x() - max(0, dx), self.winfo_y() - max(0, dy)
            self.geometry(f"+{max(0, x)}+{max(0, y)}")

    def _place(self):
        self.update_idletasks()
        self._fit_why()
        place_over(self, self.app)
        self._placed = True
        self._keep_on_screen()                                          # 창 제목 줄까지 친 실제 위치로 한 번 더(작은 화면에서 단추 줄이 작업 표시줄 뒤로 가지 않게)

    def _fill_price(self, resolve=None):
        if resolve is None:
            from .estimate import resolve_price as resolve
        model = self.v_pm.get().strip(); pr = resolve(model, self.app.cfg.get("prices") or {}) if model else None
        fx, _ = self.app.fx_setting()
        for ent, v in ((self.e_in, f"{pr['in']:g}" if pr else ""), (self.e_out, f"{pr['out']:g}" if pr else ""), (self.e_fx, f"{fx:.0f}" if fx else "")):
            ent.delete(0, "end"); ent.insert(0, v)

    def toggle_adj(self):
        self.adj_shown = not self.adj_shown
        if self.adj_shown:
            self.adj.grid(row=8, column=0, sticky="ew", pady=(self.app.px(10), 0)); self.b_adj.configure(text="단가·환율 접기 ▴")
        else:
            self.adj.grid_remove(); self.b_adj.configure(text="단가·환율 고치기 ▾")
        self._fit()

    def toggle_more(self):
        self.more_shown = not self.more_shown
        if self.more_shown:
            self.more.grid(row=11, column=0, sticky="ew", pady=(self.app.px(10), 0)); self.b_more.configure(text="근거·가정 접기 ▴")
        else:
            self.more.grid_remove(); self.b_more.configure(text="근거·가정 ▾")
        self._fit()

    # ----- 단추 -----
    def save(self):
        model = self.v_pm.get().strip()
        if not model:
            return
        def num(s):
            try: return float(s.strip()) if s.strip() else 0.0
            except ValueError: return 0.0
        prices = dict(self.app.cfg.get("prices") or {}); prices[model] = {"in": num(self.e_in.get()), "out": num(self.e_out.get())}
        self.app.cfg["prices"] = prices; self.app.cfg["fx_krw"] = self.e_fx.get().strip(); write_config(self.app)
        self.render()

    def reset(self):
        model = self.v_pm.get().strip()
        prices = dict(self.app.cfg.get("prices") or {}); prices.pop(model, None)
        self.app.cfg["prices"] = prices; self.app.cfg["fx_krw"] = ""; write_config(self.app)
        self.v_recalc.set("")
        self.render()

    def done(self, choice):
        self.choice = choice; self.destroy()


class UpdateDialog(tk.Toplevel):
    """조회만으로 설치하지 않는다. 새 판 확인 뒤 사용자가 설치·재시작을 고른다."""
    def __init__(self, app, info, can_install: bool):
        super().__init__(app)
        self.install = False
        self.title(f"{APP_NAME} 업데이트")
        self.transient(app); self.resizable(False, False)
        P = app.px
        body = ttk.Frame(self, padding=P(20)); body.pack(fill="both", expand=True)
        ttk.Label(body, text=f"새 버전 {info.version}을 사용할 수 있습니다.", style="Section.TLabel").pack(anchor="w")
        ttk.Label(body, text=f"현재 버전 {__version__}", style="PageHint.TLabel").pack(anchor="w", pady=(P(4), P(12)))
        explanation = ("실행 파일을 내려받아 확인한 뒤 프로그램을 다시 시작합니다.\nAPI 키·설정·판독 기록은 그대로 유지됩니다."
                       if can_install else ("설치판은 릴리스 페이지의 Setup 설치파일로 업데이트하십시오.\nAPI 키·설정·판독 기록은 그대로 유지됩니다." if is_installed_bundle() else
                       "Python 소스로 실행 중입니다. 릴리스 페이지에서 Windows 실행 파일을\n내려받을 수 있습니다. 현재 소스 파일은 자동으로 교체하지 않습니다."))
        ttk.Label(body, text=explanation, wraplength=P(530), justify="left").pack(anchor="w")
        if info.notes:
            notes = tk.Text(body, height=9, width=64, wrap="word", font=app.font_small)
            notes.insert("1.0", info.notes[:6000]); notes.configure(state="disabled")
            notes.pack(fill="x", pady=(P(12), 0))
        actions = ttk.Frame(body); actions.pack(fill="x", pady=(P(18), 0))
        ttk.Button(actions, text="릴리스 보기" if can_install else "릴리스 다운로드 열기", command=lambda: webbrowser.open(info.url)).pack(side="left")
        ttk.Button(actions, text="나중에", command=self.destroy).pack(side="right")
        if can_install:
            ttk.Button(actions, text="설치 후 재시작", command=self._install).pack(side="right", padx=P(8))
        self.grab_set()

    def _install(self):
        self.install = True
        self.destroy()


class Tip:
    """마우스를 올리면 잠시 뒤 짧은 설명을 띄운다(화면에 늘 보이던 긴 안내문을 줄이기 위해)."""
    def __init__(self, widget, text: str, app: "App"):
        self.w, self.text, self.app = widget, text, app
        self.win: tk.Toplevel | None = None; self.job = None
        widget.bind("<Enter>", self._enter, add="+"); widget.bind("<Leave>", self._leave, add="+"); widget.bind("<ButtonPress>", self._leave, add="+")

    def _enter(self, _e=None):
        self._cancel(); self.job = self.w.after(550, self._show)

    def _leave(self, _e=None):
        self._cancel()
        if self.win is not None:
            try: self.win.destroy()
            except tk.TclError: pass
            self.win = None

    def _cancel(self):
        if self.job:
            try: self.w.after_cancel(self.job)
            except tk.TclError: pass
            self.job = None

    def _show(self):
        self.job = None
        if self.win is not None or not self.text or not self.w.winfo_exists():
            return
        C, P = self.app.colors, self.app.px
        self.win = tw = tk.Toplevel(self.w); tw.wm_overrideredirect(True)
        try: tw.attributes("-topmost", True)
        except tk.TclError: pass
        tk.Label(tw, text=self.text, bg=C["INK"], fg=C["PANEL"], font=self.app.font_small, justify="left", wraplength=P(380), padx=P(10), pady=P(6)).pack()
        tw.update_idletasks()
        x = min(self.w.winfo_rootx() + P(6), self.w.winfo_screenwidth() - tw.winfo_reqwidth() - 8)
        y = self.w.winfo_rooty() + self.w.winfo_height() + P(6)
        tw.geometry(f"+{max(0, x)}+{y}")


ABOUT = "한자·한글 문헌 스캔 PDF·이미지를 지침대로 판독해 HWPX·문자 PDF로"     # 머리띠 이름의 풍선 도움말(창 제목에도)
TAB_HINTS = ("스캔 PDF를 판독해 HWPX·문자 PDF로 만듭니다", "이미 입력된 HWPX를 PDF 원본과 대조해 고친 사본을 만듭니다",
             "이미지·화면 캡처를 붙여넣고 HWPX·문자 PDF로 만듭니다", "판독기·모델·자동 교정·문서 옵션을 설정합니다")


class App(tk.Tk):
    def __init__(self):
        from .app_icon import apply_window_icon, set_windows_app_id
        set_windows_app_id()
        super().__init__()
        apply_window_icon(self)
        self.title(f"{APP_NAME} · 연어OCR {__version__} — {ABOUT}")
        self.cfg = load_config()
        self._cfg_loaded = copy.deepcopy(self.cfg)             # 읽은 그대로 — 저장할 때 이 창이 바꾼 값만 파일에 얹는다(write_config)
        self._style()
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        w, h = min(self.px(1280), sw - 40), min(self.px(980), sh - 80)          # 너비 1280: 작업 탭이 두 단(논리 1200 이상)으로 보이게
        self.geometry(f"{w}x{h}"); self.minsize(min(self.px(960), w), min(self.px(640), h))
        self.q: queue.Queue = queue.Queue()
        self.running = False
        self._update_checking = False
        self._update_installing = False
        self._update_pending = None
        self._update_prompt_open = False
        self.open_after: Path | None = None
        self.buttons: list[ttk.Button] = []
        self._lock_entries: list = []                                  # 실행 중에 잠그는 입력 칸(이 책의 설정 판의 쪽 범위)
        self._planning = False; self._plan_seq = 0; self._plan_wait = None     # 비용 어림(작업 스레드) — 한 번에 하나, 늦은 결과는 번호로 버림
        self.tracker = Tracker()
        self._prog_shown = 0.0
        self._build()
        self.bind("<Control-Shift-KeyPress-V>", self.paste_screenshot, add="+")
        self.bind("<Control-Shift-KeyPress-v>", self.paste_screenshot, add="+")
        self.after(100, self._poll)
        self.after(400, self.check_packages)
        self.after(1800, lambda: self.check_updates(manual=False))
        self._fx, need_fx = fx_from_config(self.cfg, time.strftime("%Y-%m-%d"))     # 환율은 하루 한 번만 조회(받은 값은 설정에 남김)
        if need_fx:
            threading.Thread(target=self._fetch_fx, daemon=True).start()
        self.protocol("WM_DELETE_WINDOW", self.on_close)

    def px(self, v: float) -> int:
        """화면 배율에 맞춘 픽셀 수(글꼴은 Tk 가 알아서 키우지만 여백·그림은 직접 곱해야 한다)."""
        return max(1, int(round(v * getattr(self, "scale", 1.0))))

    # ---------- 화면 ----------
    FONT_CANDIDATES = ("Pretendard", "Pretendard Variable", "맑은 고딕", "Malgun Gothic", "Apple SD Gothic Neo", "Noto Sans CJK KR", "Noto Sans KR", "NanumGothic", "나눔고딕")
    MONO_CANDIDATES = ("D2Coding", "D2Coding ligature", "Cascadia Mono", "Consolas", "Noto Sans Mono CJK KR", "Menlo", "DejaVu Sans Mono")
    LOG_CANDIDATES = ("D2Coding", "D2Coding ligature", "Noto Sans Mono CJK KR")    # 진행 기록: 한글이 든 고정폭 글꼴이 있을 때만(Consolas 는 한글을 굴림으로 대신 그려 흐리다)
    # 색 토큰(밝은 판, 대비는 WCAG 식으로 잰 값 — 디자인 명세 §2). 인주(SEAL)는 돈 드는 단추와 도장에만, 실패는 RED 테두리 + '!'(채우지 않음).
    PALETTE = dict(BG="#f3eee3", PANEL="#fcfaf5", QUIET="#f6f1e6", FIELD="#ffffff", DIS_FIELD="#f0ebe1", LINE="#e2d9c8", HAIR="#efe8db",
                   STRIPE="#faf7f0", RULE="#cbbfa9", EDGE="#8f8271", INK="#221c18", INK_HI="#3d342e", MUTED="#574d42",
                   SEAL="#b8391f", SEAL_HI="#93291a", SEAL_SOFT="#f5e2db", SEAL_LINE="#f0c9bd", BAND="#7f7262",
                   GREEN="#2f6f4a", BLUE="#2b5a8c", RED="#8c1d18", WARN="#875a12", SEL="#f1e5c9")
    # 예전 이름 → 새 토큰(아직 옛 이름을 쓰는 화면도 새 색으로). 옻칠(ACC)은 먹(INK)으로, 금빛 선(GOLD)은 없앰.
    LEGACY_COLORS = {"HEAD": "QUIET", "DIS": "QUIET", "BTN_HI": "QUIET", "BTN": "FIELD", "BTN_DN": "LINE", "TRACK": "LINE", "DIS_LINE": "LINE",
                     "FLINE": "RULE", "TRACK_HI": "RULE", "DIS_INK": "EDGE", "ACC": "INK", "FOCUS": "INK", "ACC_HI": "INK_HI", "ACC_DN": "SEAL_HI",
                     "RED_SOFT": "SEAL_SOFT"}

    def _pick_font(self, candidates: tuple[str, ...], fallback: str) -> str:
        """설치된 글꼴 가운데 앞에서부터 처음 있는 것. Pretendard(또는 D2Coding)가 있으면 그것을, 없으면 시스템 한글 글꼴을 쓴다."""
        try:
            from tkinter import font as tkfont
            have = {f.lower() for f in tkfont.families(self)}
            for c in candidates:
                if c.lower() in have:
                    return c
        except Exception:
            pass
        return fallback

    def _style(self):
        """서재(한지·먹·인주): 종이색 바탕에 먹 글자, 돈 드는 단추만 인주색. clam 테마 위에 둥근 단추·카드·입력칸, ✓ 체크 상자, 밑줄 탭,
        끝까지 차는 진행 막대를 그림(gui_art)으로 입힌다. 단추 위계 — 주 실행(인주 채움) · 버금 실행(인주 테두리) · 먹 채움(돈 안 드는 주 단추) ·
        강조(먹 테두리) · 보통(테두리 단추) · 글자 단추(밑줄)·펼침 단추(▾). 글꼴 이름(F_…)은 디자인 명세 §3."""
        from . import gui_art as A
        st = ttk.Style(self)
        try:
            st.theme_use("clam")
        except tk.TclError:
            pass
        try:
            self.scale = max(1.0, round(self.winfo_fpixels("1i") / 96.0 * 4) / 4)
        except Exception:
            self.scale = 1.0
        P = self.px
        sys_base = "맑은 고딕" if sys.platform.startswith("win") else ("Apple SD Gothic Neo" if sys.platform == "darwin" else "Noto Sans CJK KR")
        base = self._pick_font(self.FONT_CANDIDATES, sys_base)
        mono = self._pick_font(self.MONO_CANDIDATES, "Consolas" if sys.platform.startswith("win") else "DejaVu Sans Mono")
        disp = base                                                 # 책 제목·금액·큰 숫자도 본문 글꼴 굵게(명조 계열은 화면에서 흐려 읽기 어렵다)
        log = self._pick_font(self.LOG_CANDIDATES, base)
        F = self.fonts = {"APP": (disp, 16, "bold"), "TITLE": (disp, 21, "bold"), "BIG": (disp, 24, "bold"), "NUM": (disp, 16, "bold"), "DLG": (disp, 16, "bold"),
                          "UNIT": (base, 12), "SECTION": (base, 11, "bold"), "SEAL": (base, 11, "bold"), "SEAL_S": (base, 9, "bold"), "USD": (base, 13, "bold"),
                          "TAB": (base, 11), "TAB_ON": (base, 11, "bold"), "RUN": (base, 11, "bold"), "BTN_L": (base, 11), "BTN_L_B": (base, 11, "bold"),
                          "BODY": (base, 10), "BODY_B": (base, 10, "bold"), "CAP": (base, 10), "CAP_B": (base, 10, "bold"), "MONO": (mono, 10), "MONO_L": (mono, 11),
                          "LOG": (log, 10)}
        self.font_base, self.font_small, self.font_h = F["BODY"], F["CAP"], F["BODY_B"]
        self.font_title, self.font_sub, self.font_big = F["APP"], F["BODY"], (base, 12, "bold")
        self.font_card, self.font_btn_h = F["SECTION"], F["RUN"]
        self.font_mono = F["MONO"]; self.font_log = F["LOG"]
        # 기본 글꼴을 한글 글꼴로 바꾼다. (option_add("*Font") 는 ttk 이름표의 스타일 글꼴까지 덮어써 카드 제목·안내 글의 굵기·크기가 사라졌다)
        try:
            from tkinter import font as tkfont
            for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont", "TkCaptionFont", "TkSmallCaptionFont", "TkIconFont", "TkTooltipFont"):
                tkfont.nametofont(name, root=self).configure(family=base)
            tkfont.nametofont("TkDefaultFont", root=self).configure(size=10); tkfont.nametofont("TkTextFont", root=self).configure(size=10)
        except Exception:
            self.option_add("*Font", self.font_base)
        self.option_add("*TCombobox*Listbox.font", self.font_base)
        C = self.colors = dict(self.PALETTE)
        C.update({old: C[new] for old, new in self.LEGACY_COLORS.items()})
        self.configure(bg=C["BG"])
        self.option_add("*TCombobox*Listbox.background", C["FIELD"])
        self.option_add("*TCombobox*Listbox.selectBackground", C["SEL"])
        self.option_add("*TCombobox*Listbox.selectForeground", C["INK"])
        self._img: dict[str, tk.PhotoImage] = {}

        def img(key: str, b64: str) -> tk.PhotoImage:
            self._img[key] = tk.PhotoImage(master=self, data=b64)
            return self._img[key]

        def box(key: str, r: float, fill, line=None, lw: float = 1.0, ring=None, ring_w: float = 0.0, gap: float = 0.0, stretch=(96, 24)):
            """9칸 늘이기 둥근 판 그림과 그 테두리 폭(모서리 + 고리 자리)."""
            b = int(math.ceil(r + ring_w + gap)) + 1
            return img(key, A.rounded_box(2 * b + stretch[0], 2 * b + stretch[1], r, fill, line, lw, ring=ring, ring_w=ring_w, ring_gap=gap)), b

        bw = 1.0 * self.scale
        # ---- 바탕·글자 ----
        st.configure(".", background=C["BG"], foreground=C["INK"], font=F["BODY"], bordercolor=C["LINE"], troughcolor=C["LINE"],
                     focuscolor=C["BG"], selectbackground=C["SEL"], selectforeground=C["INK"], insertcolor=C["INK"])
        st.configure("TFrame", background=C["BG"]); st.configure("Panel.TFrame", background=C["PANEL"])
        st.configure("TLabel", background=C["BG"], foreground=C["INK"])
        st.configure("Panel.TLabel", background=C["PANEL"])
        st.configure("Field.TLabel", background=C["PANEL"], foreground=C["INK"])
        st.configure("Hint.TLabel", background=C["PANEL"], foreground=C["MUTED"], font=F["CAP"])
        st.configure("PageHint.TLabel", background=C["BG"], foreground=C["MUTED"], font=F["CAP"])
        st.configure("CardTitle.TLabel", background=C["PANEL"], foreground=C["INK"], font=F["SECTION"])
        st.configure("Section.TLabel", background=C["PANEL"], foreground=C["INK"], font=F["SECTION"])
        st.configure("Status.TLabel", background=C["BG"], foreground=C["INK"], font=F["BODY_B"])
        st.configure("Prog.TLabel", background=C["BG"], foreground=C["MUTED"], font=F["BODY"])
        # 가는 선: clam 의 구분선은 홈 파인 두 줄(짙은 그림자)로 그려져 무거워 보이므로 한 줄짜리 그림으로 바꾼다
        lh = max(1, P(1))
        st.element_create("Rule.line", "image", img("rule", A.rounded_box(64, lh, 0, C["LINE"])), sticky="ew", height=lh)
        st.layout("TSeparator", [("Rule.line", {"sticky": "ew"})]); st.layout("Horizontal.TSeparator", [("Rule.line", {"sticky": "ew"})])
        # ---- 카드(반지름 10 · LINE 테두리) · 조용한 판(QUIET, 반지름 8, 테두리 없음) ----
        im, b = box("card", P(10), C["PANEL"], C["LINE"], bw, stretch=(192, 64))
        st.element_create("Card.border", "image", im, border=b, sticky="nsew", width=2 * b, height=2 * b)
        st.layout("Card.TFrame", [("Card.border", {"sticky": "nsew"})])
        st.configure("Card.TFrame", background=C["BG"])
        im, b = box("quiet", P(8), C["QUIET"], stretch=(128, 48))
        st.element_create("Quiet.border", "image", im, border=b, sticky="nsew", width=2 * b, height=2 * b)
        st.layout("Quiet.TFrame", [("Quiet.border", {"sticky": "nsew"})])
        st.configure("Quiet.TFrame", background=C["PANEL"])
        # ---- 알약(판 번호·명령 이름)·꼬리표([아니요]면 뺌) — 글 길이에 맞춰 늘어나는 테두리 이름표 ----
        im, b = box("chip", P(10), C["PANEL"], C["EDGE"], bw, stretch=(32, 2))
        st.element_create("Chip.border", "image", im, border=b, sticky="nsew", width=2 * b, height=2 * b, padding=(P(8), P(1)))
        st.layout("Chip.TLabel", [("Chip.border", {"sticky": "nsew", "children": [("Label.padding", {"sticky": "nsew", "children": [("Label.label", {"sticky": "nsew"})]})]})])
        st.configure("Chip.TLabel", background=C["PANEL"], foreground=C["INK"], font=F["CAP"], padding=(P(0), 0), anchor="center")
        im, b = box("tag", P(4), C["PANEL"], C["EDGE"], bw, stretch=(32, 8))
        st.element_create("Tag.border", "image", im, border=b, sticky="nsew", width=2 * b, height=P(18), padding=(P(5), 0))
        st.layout("Tag.TLabel", [("Tag.border", {"sticky": "nsew", "children": [("Label.padding", {"sticky": "nsew", "children": [("Label.label", {"sticky": "nsew"})]})]})])
        st.configure("Tag.TLabel", background=C["PANEL"], foreground=C["INK"], font=F["CAP"], padding=(P(0), 0), anchor="center")
        st.configure("QuietTag.TLabel", background=C["QUIET"])
        st.map("Tag.TLabel", foreground=[("disabled", C["EDGE"])])                                   # 흐린 설정 줄의 꼬리표
        st.configure("SetVal.TLabel", background=C["PANEL"], foreground=C["INK"], font=F["BODY_B"])     # 설정 탭 양식 HWPX 이름(원본 쪽대로면 흐림)
        st.map("SetVal.TLabel", foreground=[("disabled", C["MUTED"])], background=[("disabled", C["PANEL"])])
        st.configure("SetNone.TLabel", background=C["PANEL"], foreground=C["MUTED"], font=F["BODY"])
        # 진행 카드의 상태 알약: 준비됨(EDGE 테) · 실행 중(먹 테 · 굵게) · 끝(초록) · 실패·오류(RED) · 주의(WARN) · 원본 없음·중지됨(EDGE 테 · MUTED 글)
        for kind, line, fg, font in (("idle", C["EDGE"], C["INK"], F["CAP"]), ("run", C["INK"], C["INK"], F["CAP_B"]), ("green", C["GREEN"], C["GREEN"], F["CAP_B"]),
                                     ("fail", C["RED"], C["RED"], F["CAP_B"]), ("warn", C["WARN"], C["WARN"], F["CAP_B"]), ("mute", C["EDGE"], C["MUTED"], F["CAP"])):
            im, b = box("pill_" + kind, P(10), C["PANEL"], line, bw, stretch=(32, 2))
            el = f"Pill{kind}.border"
            st.element_create(el, "image", im, border=b, sticky="nsew", width=2 * b, height=2 * b, padding=(P(8), P(1)))
            st.layout(f"Pill{kind}.TLabel", [(el, {"sticky": "nsew", "children": [("Label.padding", {"sticky": "nsew", "children": [("Label.label", {"sticky": "nsew"})]})]})])
            st.configure(f"Pill{kind}.TLabel", background=C["PANEL"], foreground=fg, font=font, padding=(P(0), 0), anchor="center")

        # ---- 단추 ----
        def button_style(sname: str, looks: dict, pad, font, fg: str, minh: int, r: float = 7, bg: str = C["PANEL"], fg_map=(),
                         ring: str | None = None, space: float = 0.0, focus: str = C["INK"]):
            """looks: 상태(normal·active·pressed·disabled) → (채움, 테두리, 테두리 두께). space 는 바깥 고리 자리(고리가 없어도 비워 둠)."""
            rw = P(2) if space else 0.0
            gap = space - rw if space else 0.0
            ims = {}
            for k, (fill, line, lw) in looks.items():
                ims[k], b = box(f"{sname}_{k}", P(r), fill, line, lw * self.scale, ring=ring, ring_w=rw, gap=gap)
            el = sname.replace(".TButton", "") + ".border" if sname != "TButton" else "Btn.border"
            st.element_create(el, "image", ims["normal"], ("disabled", ims["disabled"]), ("pressed", ims["pressed"]), ("active", ims["active"]),
                              border=b, sticky="nsew", width=2 * b, height=P(minh) + 2 * int(space), padding=P(2) + int(space))
            st.layout(sname, [(el, {"sticky": "nsew", "children": [("Button.padding", {"sticky": "nsew", "children": [
                ("Button.focus", {"sticky": "nsew", "children": [("Button.label", {"sticky": "nsew"})]})]})]})])
            st.configure(sname, padding=pad, font=font, foreground=fg, background=bg, anchor="center", focuscolor=focus, focusthickness=1, width=-6)
            st.map(sname, foreground=[("disabled", C["EDGE"]), *fg_map], background=[("active", bg), ("pressed", bg), ("disabled", bg)])

        off = (C["QUIET"], C["LINE"], 1.0)                           # 꺼진 단추: QUIET 판 · LINE 테두리 · EDGE 글자
        normal = {"normal": (C["FIELD"], C["EDGE"], 1.0), "active": (C["QUIET"], C["INK"], 1.0), "pressed": (C["LINE"], C["INK"], 1.0), "disabled": off}
        seal = {"normal": (C["SEAL"], None, 1.0), "active": (C["SEAL_HI"], None, 1.0), "pressed": (C["SEAL_HI"], None, 1.0), "disabled": off}
        seal_out = {"normal": (C["FIELD"], C["SEAL"], 1.5), "active": (C["SEAL_SOFT"], C["SEAL_HI"], 1.5), "pressed": (C["SEAL_SOFT"], C["SEAL_HI"], 1.5), "disabled": off}
        ink_line = {"normal": (C["FIELD"], C["INK"], 1.5), "active": (C["QUIET"], C["INK"], 1.5), "pressed": (C["LINE"], C["INK"], 1.5), "disabled": off}
        ink = {"normal": (C["INK"], None, 1.0), "active": (C["INK_HI"], None, 1.0), "pressed": (C["INK_HI"], None, 1.0), "disabled": off}
        seal_fg = (("active", C["SEAL_HI"]), ("pressed", C["SEAL_HI"]))
        button_style("TButton", normal, (P(14), P(4)), F["BODY"], C["INK"], 32)                                   # 보통(테두리)
        button_style("Small.TButton", normal, (P(10), P(2)), F["CAP"], C["INK"], 28)                              # 판 안의 작은 단추
        button_style("QuietSmall.TButton", normal, (P(10), P(2)), F["BODY"], C["INK"], 28, bg=C["QUIET"])        # 조용한 판(QUIET) 위의 작은 단추
        button_style("Seal.TButton", seal, (P(18), P(6)), F["RUN"], "#ffffff", 48, r=8, focus="#ffffff")                            # 주 실행 — 돈이 듦(인주 채움)
        button_style("SealOut.TButton", seal_out, (P(16), P(4)), F["BTN_L_B"], C["SEAL"], 40, r=8, fg_map=seal_fg)  # 버금 실행 — 돈이 들지만 주가 아님(인주 테두리)
        button_style("Stop.TButton", ink_line, (P(16), P(4)), F["BTN_L_B"], C["INK"], 40, r=8)                    # 중지(실행 전에는 꺼진 모양)
        # 도는 실행의 자리(눌리지 않음 — disabled 로 두고 그 모양까지 잠긴 인주 판): SEAL_SOFT 판 · SEAL_LINE 테두리 · SEAL_HI 글자와 자물쇠
        busy = {k: (C["SEAL_SOFT"], C["SEAL_LINE"], 1.0) for k in ("normal", "active", "pressed", "disabled")}
        for sname, font, minh, pad in (("SealBusy.TButton", F["RUN"], 48, (P(18), P(6))), ("SealOutBusy.TButton", F["BTN_L_B"], 40, (P(16), P(4)))):
            button_style(sname, busy, pad, font, C["SEAL_HI"], minh, r=8)
            st.map(sname, foreground=[("disabled", C["SEAL_HI"])], background=[("disabled", C["PANEL"])])
        button_style("Emph.TButton", ink_line, (P(14), P(4)), F["BODY_B"], C["INK"], 32)                          # 끝난 뒤 그 실행의 결과 단추
        button_style("Ink.TButton", ink, (P(16), P(4)), F["BODY_B"], "#ffffff", 32, focus="#ffffff")                               # 먹 채움 — 돈이 들지 않는 주 단추
        button_style("Accent.TButton", ink, (P(16), P(4)), F["BODY_B"], "#ffffff", 32, focus="#ffffff")                            # (작은 창의 주 단추 — 먹 채움과 같음)
        button_style("QuietInk.TButton", ink, (P(16), P(4)), F["BODY_B"], "#ffffff", 32, bg=C["QUIET"], focus="#ffffff")           # 빈 이미지 상자(QUIET) 안의 [이미지 붙여넣기]
        button_style("FootInk.TButton", ink, (P(20), P(4)), F["BTN_L_B"], "#ffffff", 36, r=8, focus="#ffffff")                      # 설정 탭 바닥 줄 [설정 저장]
        button_style("Foot.TButton", normal, (P(16), P(4)), F["BTN_L"], C["INK"], 36, r=8)                                          # 설정 탭 바닥 줄 [OCR로 돌아가기]
        # 비용 창 바닥 단추(높이 40): 기본 단추에만 먹 고리(2px, 틈 2px). 고리가 없는 단추도 같은 자리를 비워 두어 기본이 바뀌어도 단추 크기가 같다
        sp = P(2) + P(2)
        button_style("Dlg.TButton", normal, (P(16), P(4)), F["BTN_L"], C["INK"], 40, r=8, space=sp)
        button_style("DlgDef.TButton", normal, (P(16), P(4)), F["BTN_L"], C["INK"], 40, r=8, space=sp, ring=C["INK"])
        button_style("DlgSealDef.TButton", seal, (P(18), P(4)), F["BTN_L_B"], "#ffffff", 40, r=8, space=sp, ring=C["INK"], focus="#ffffff")
        button_style("DlgSealOut.TButton", seal_out, (P(18), P(4)), F["BTN_L_B"], C["SEAL"], 40, r=8, space=sp, fg_map=seal_fg)
        button_style("DlgInkDef.TButton", ink, (P(18), P(4)), F["BTN_L_B"], "#ffffff", 40, r=8, space=sp, ring=C["INK"], focus="#ffffff")   # 규칙 창의 기본 단추(돈 안 듦 — 먹)
        st.configure("Tool.TButton", padding=(P(10), P(3)))
        st.configure("Page.TButton", background=C["BG"]); st.map("Page.TButton", background=[("active", C["BG"]), ("pressed", C["BG"]), ("disabled", C["BG"])])
        # 글자 단추(밑줄 — 탭을 옮기거나 여는 것)·펼침 단추(▾ — 밑줄 없음): 마우스를 올리면 QUIET 판
        clear, b = box("link_n", P(4), None, stretch=(32, 8))
        hover = img("link_h", A.rounded_box(2 * b + 32, 2 * b + 8, P(4), C["QUIET"]))
        st.element_create("Link.border", "image", clear, ("active", hover), border=b, sticky="nsew", width=2 * b, height=2 * b, padding=0)
        under = (F["BODY_B"][0], F["BODY_B"][1], "bold", "underline")
        hover_q = img("link_hq", A.rounded_box(2 * b + 32, 2 * b + 8, P(4), C["LINE"]))
        st.element_create("QLink.border", "image", clear, ("active", hover_q), border=b, sticky="nsew", width=2 * b, height=2 * b, padding=0)
        for sname, font in (("QLink.TButton", under), ("QToggle.TButton", F["BODY_B"])):
            st.layout(sname, [("QLink.border", {"sticky": "nsew", "children": [("Button.padding", {"sticky": "nsew", "children": [
                ("Button.focus", {"sticky": "nsew", "children": [("Button.label", {"sticky": "nsew"})]})]})]})])
            st.configure(sname, background=C["QUIET"], foreground=C["INK"], font=font, padding=(P(4), P(2)), width=0, focuscolor=C["INK"], focusthickness=max(1, P(1)))
            st.map(sname, foreground=[("disabled", C["EDGE"])], background=[("active", C["QUIET"])], font=[("disabled", F["BODY_B"])])
        for sname, bg, font in (("Link.TButton", C["PANEL"], under), ("PageLink.TButton", C["BG"], under),
                                ("Toggle.TButton", C["PANEL"], F["BODY_B"]), ("PageToggle.TButton", C["BG"], F["BODY_B"]),
                                ("SecToggle.TButton", C["PANEL"], F["SECTION"]), ("SetToggle.TButton", C["PANEL"], F["BODY_B"])):   # 설정 탭의 접힌 섹션 제목·[고급 ▸]
            st.layout(sname, [("Link.border", {"sticky": "nsew", "children": [("Button.padding", {"sticky": "nsew", "children": [
                ("Button.focus", {"sticky": "nsew", "children": [("Button.label", {"sticky": "nsew"})]})]})]})])
            st.configure(sname, background=bg, foreground=C["INK"], font=font, padding=(P(4), P(2)), width=0, focuscolor=C["INK"], focusthickness=max(1, P(1)))
            st.map(sname, foreground=[("disabled", C["EDGE"])], background=[("active", bg)], font=[("disabled", font if sname == "SecToggle.TButton" else F["BODY_B"])])
        for sname in ("SecToggle.TButton", "SetToggle.TButton"):          # 왼쪽 끝을 섹션 제목·줄 이름에 맞춤(마우스를 올린 판의 모서리만큼만 들어감)
            st.configure(sname, padding=(0, P(2)))
        # ---- 머리띠의 밑줄 탭: 고른 탭은 INK 굵게 + 아래 3px INK 막대, 보통은 MUTED, 실행 중 잠긴 탭은 EDGE ----
        bar_h = max(2, P(3))
        tab_n = img("tab_n", A.underline_box(24, 24, C["PANEL"], None, bar_h))
        tab_s = img("tab_s", A.underline_box(24, 24, C["PANEL"], C["INK"], bar_h))
        st.element_create("Tab.border", "image", tab_n, ("selected", tab_s), border=(2, 2, 2, bar_h + 2), sticky="nsew", padding=0)
        st.layout("Tab.TRadiobutton", [("Tab.border", {"sticky": "nsew", "children": [("Radiobutton.padding", {"sticky": "nsew", "children": [
            ("Radiobutton.focus", {"sticky": "", "children": [("Radiobutton.label", {"sticky": "nsew"})]})]})]})])
        st.configure("Tab.TRadiobutton", background=C["PANEL"], foreground=C["MUTED"], font=F["TAB"], padding=(P(12), P(3), P(12), bar_h + P(3)),
                     anchor="center", focuscolor=C["INK"], focusthickness=1)
        st.map("Tab.TRadiobutton", foreground=[("selected", C["INK"]), ("disabled", C["EDGE"]), ("active", C["INK"])], font=[("selected", F["TAB_ON"])],
               background=[("active", C["PANEL"]), ("disabled", C["PANEL"])])
        st.layout("SubTab.TRadiobutton", st.layout("Tab.TRadiobutton"))           # 기록 카드 머리의 [진행 기록 | 쪽별 상태](본문 크기)
        st.configure("SubTab.TRadiobutton", background=C["PANEL"], foreground=C["MUTED"], font=F["BODY"], padding=(P(8), P(7), P(8), bar_h + P(5)),
                     anchor="center", focuscolor=C["INK"], focusthickness=1)
        st.map("SubTab.TRadiobutton", foreground=[("selected", C["INK"]), ("active", C["INK"])], font=[("selected", F["BODY_B"])],
               background=[("active", C["PANEL"]), ("disabled", C["PANEL"])])
        # ---- 체크 상자(✓)·라디오: 켜면 먹 채움. 키보드 초점은 글자 둘레 점선(마우스로 누르면 초점을 돌려 보이지 않음) ----
        cs, gap = P(16), P(8)
        G = C["INK"]
        ck = {k: img("chk_" + k, A.checkbox(cs, on, acc, C["EDGE"], gap=gap, hover=hv, disabled=ds, ring=rg))
              for k, on, acc, hv, ds, rg in (("off", False, C["INK"], False, False, None), ("off_h", False, C["INK"], True, False, None),
                                             ("off_f", False, C["INK"], False, False, G), ("on", True, C["INK"], False, False, None),
                                             ("on_h", True, C["INK_HI"], False, False, None), ("on_f", True, C["INK"], False, False, None),
                                             ("dis_off", False, C["INK"], False, True, None), ("dis_on", True, C["INK"], False, True, None))}
        st.element_create("Check.indicator", "image", ck["off"], ("disabled", "selected", ck["dis_on"]), ("disabled", ck["dis_off"]),
                          ("selected", "active", ck["on_h"]), ("selected", "focus", ck["on_f"]), ("selected", ck["on"]), ("active", ck["off_h"]), ("focus", ck["off_f"]), sticky="")
        rd = {k: img("rad_" + k, A.radio(cs, on, C["INK"], C["EDGE"], gap=gap, hover=hv, disabled=ds, ring=rg))
              for k, on, hv, ds, rg in (("off", False, False, False, None), ("off_h", False, True, False, None), ("off_f", False, False, False, G),
                                        ("on", True, False, False, None), ("on_f", True, False, False, None), ("dis_off", False, False, True, None),
                                        ("dis_on", True, False, True, None))}
        st.element_create("Radio.indicator", "image", rd["off"], ("disabled", "selected", rd["dis_on"]), ("disabled", rd["dis_off"]),
                          ("selected", "focus", rd["on_f"]), ("selected", rd["on"]), ("active", rd["off_h"]), ("focus", rd["off_f"]), sticky="")
        for w, ind in (("Checkbutton", "Check.indicator"), ("Radiobutton", "Radio.indicator")):
            st.layout(f"T{w}", [(f"{w}.padding", {"sticky": "nswe", "children": [(ind, {"side": "left", "sticky": ""}),
                                (f"{w}.focus", {"side": "left", "sticky": "", "children": [(f"{w}.label", {"sticky": "nswe"})]})]})])
            st.configure(f"T{w}", background=C["PANEL"], foreground=C["INK"], padding=(0, P(3), P(4), P(3)), focuscolor=C["INK"], focusthickness=1)
            st.map(f"T{w}", background=[("active", C["PANEL"]), ("disabled", C["PANEL"])], foreground=[("disabled", C["EDGE"])])
        # 글 없는 체크 상자(설정 줄의 조작): 초점 점선이 감쌀 글이 없으므로 상자를 감싼다 — 켜진 상자(먹 채움)에서도 초점이 보이게(상자 둘레 틈 3)
        bare = {k: img("chkb_" + k, A.checkbox(cs, on, acc, C["EDGE"], gap=0, hover=hv, disabled=ds))
                for k, on, acc, hv, ds in (("off", False, C["INK"], False, False), ("off_h", False, C["INK"], True, False), ("on", True, C["INK"], False, False),
                                           ("on_h", True, C["INK_HI"], False, False), ("dis_off", False, C["INK"], False, True), ("dis_on", True, C["INK"], False, True))}
        st.element_create("BareCheck.indicator", "image", bare["off"], ("disabled", "selected", bare["dis_on"]), ("disabled", bare["dis_off"]),
                          ("selected", "active", bare["on_h"]), ("selected", bare["on"]), ("active", bare["off_h"]), sticky="", width=cs + 2 * P(3), height=cs + 2 * P(3))
        st.layout("Bare.TCheckbutton", [("Checkbutton.padding", {"sticky": "nswe", "children": [
            ("Checkbutton.focus", {"sticky": "", "children": [("BareCheck.indicator", {"sticky": ""})]})]})])
        st.configure("Bare.TCheckbutton", background=C["PANEL"], padding=(0, P(1)), focuscolor=C["INK"], focusthickness=1)
        st.map("Bare.TCheckbutton", background=[("active", C["PANEL"]), ("disabled", C["PANEL"])])
        # ---- 진행 막대(6px): LINE 길 위에 먹으로 처음부터 끝까지 한 방향으로 찬다 ----
        h = max(4, P(6)); r = h / 2; nw, bd_ = A.stretch_size(int(r), 192)
        st.element_create("Bar.trough", "image", img("bar_tr", A.rounded_box(nw, h, r, C["LINE"])), border=(bd_, 0), sticky="nsew", width=2 * bd_, padding=0)
        self._bar_cap = 2 * bd_                                       # 막대가 이보다 짧으면 둥근 끝이 겹쳐 그려진다
        st.element_create("Bar.pbar", "image", img("bar_pb", A.rounded_box(nw, h, r, C["INK"])), border=(bd_, 0), sticky="nsew", width=2 * bd_)
        st.layout("Bar.Horizontal.TProgressbar", [("Bar.trough", {"sticky": "nsew", "children": [("Bar.pbar", {"side": "left", "sticky": "ns"})]})])
        st.configure("Bar.Horizontal.TProgressbar", background=C["BG"], thickness=h)
        st.configure("Panel.Bar.Horizontal.TProgressbar", background=C["PANEL"])
        # ---- 입력칸·고르기 칸(반지름 6, EDGE 테두리 · 초점이면 먹 · 읽기 전용은 QUIET · 잠기면 DIS_FIELD) ----
        fld = {}
        for k, fill, line, lw in (("n", C["FIELD"], C["EDGE"], 1.0), ("f", C["FIELD"], C["INK"], 1.5), ("d", C["DIS_FIELD"], C["LINE"], 1.0), ("ro", C["QUIET"], C["LINE"], 1.0)):
            fld[k], b = box("fld_" + k, P(6), fill, line, lw * self.scale, stretch=(128, 20))
        for prefix, minh in (("", 30), ("Q", 28)):                   # 판 안(QUIET)의 칸은 28
            st.element_create(f"{prefix}Field.field", "image", fld["n"], ("disabled", fld["d"]), ("focus", fld["f"]), ("readonly", fld["ro"]),
                              border=b, sticky="nsew", width=2 * b, height=P(minh), padding=P(3))
            st.element_create(f"{prefix}Pick.field", "image", fld["n"], ("disabled", fld["d"]), ("focus", fld["f"]), border=b, sticky="nsew", width=2 * b, height=P(minh), padding=P(3))
        st.element_create("Pick.downarrow", "image", img("chev", A.chevron(P(12), P(8), C["MUTED"], 1.6 * self.scale)),
                          ("disabled", img("chev_d", A.chevron(P(12), P(8), C["EDGE"], 1.6 * self.scale))), sticky="", padding=(P(4), 0, P(2), 0))
        for prefix, sname, bg in (("", "TEntry", C["PANEL"]), ("Q", "Quiet.TEntry", C["QUIET"])):
            st.layout(sname, [(f"{prefix}Field.field", {"sticky": "nswe", "children": [("Entry.padding", {"sticky": "nswe", "children": [("Entry.textarea", {"sticky": "nswe"})]})]})])
            st.configure(sname, padding=(P(7), P(3)), background=bg, fieldbackground=C["FIELD"], foreground=C["INK"])
            st.map(sname, foreground=[("disabled", C["MUTED"])], fieldbackground=[("disabled", C["DIS_FIELD"]), ("readonly", C["QUIET"])])
        for prefix, sname, bg in (("", "TCombobox", C["PANEL"]), ("Q", "Quiet.TCombobox", C["QUIET"])):
            st.layout(sname, [(f"{prefix}Pick.field", {"sticky": "nswe", "children": [("Pick.downarrow", {"side": "right", "sticky": ""}),
                              ("Combobox.padding", {"sticky": "nswe", "children": [("Combobox.textarea", {"sticky": "nswe"})]})]})])
            st.configure(sname, padding=(P(7), P(3)), background=bg, fieldbackground=C["FIELD"], foreground=C["INK"])
            st.map(sname, fieldbackground=[("readonly", C["FIELD"])], selectbackground=[("readonly", C["FIELD"])], selectforeground=[("readonly", C["INK"])],
                   foreground=[("disabled", C["MUTED"])], background=[("readonly", bg), ("active", bg)])
        st.layout("TSpinbox", [("Field.field", {"side": "top", "sticky": "we", "children": [("null", {"side": "right", "sticky": "", "children": [
            ("Spinbox.uparrow", {"side": "top", "sticky": "e"}), ("Spinbox.downarrow", {"side": "bottom", "sticky": "e"})]}),
            ("Spinbox.padding", {"sticky": "nswe", "children": [("Spinbox.textarea", {"sticky": "nswe"})]})]})])
        st.configure("TSpinbox", padding=(P(6), P(3)), arrowsize=P(9), arrowcolor=C["MUTED"], background=C["PANEL"], fieldbackground=C["FIELD"],
                     bordercolor=C["FIELD"], lightcolor=C["FIELD"], darkcolor=C["FIELD"], foreground=C["INK"])
        st.map("TSpinbox", background=[("active", C["QUIET"]), ("pressed", C["LINE"])])
        # ---- 표·글줄 ----
        st.configure("Treeview", background=C["FIELD"], fieldbackground=C["FIELD"], foreground=C["INK"], rowheight=P(28), bordercolor=C["LINE"],
                     lightcolor=C["FIELD"], darkcolor=C["FIELD"], borderwidth=0)
        st.map("Treeview", background=[("selected", C["SEL"])], foreground=[("selected", C["INK"])])
        st.configure("Treeview.Heading", background=C["QUIET"], foreground=C["MUTED"], font=F["CAP_B"], relief="flat", padding=(P(8), P(6)),
                     bordercolor=C["LINE"], lightcolor=C["QUIET"], darkcolor=C["QUIET"])
        st.map("Treeview.Heading", background=[("active", C["LINE"])])
        for o in ("Vertical", "Horizontal"):
            st.configure(f"{o}.TScrollbar", background=C["RULE"], troughcolor=C["PANEL"], bordercolor=C["PANEL"], lightcolor=C["RULE"],
                         darkcolor=C["RULE"], arrowcolor=C["MUTED"], gripcount=0, arrowsize=P(12))
            st.map(f"{o}.TScrollbar", background=[("active", C["EDGE"]), ("pressed", C["EDGE"])], lightcolor=[("active", C["EDGE"]), ("pressed", C["EDGE"])],
                   darkcolor=[("active", C["EDGE"]), ("pressed", C["EDGE"])])
        st.layout("Hidden.TNotebook.Tab", [])
        st.configure("Hidden.TNotebook", background=C["PANEL"], borderwidth=0, padding=0, tabmargins=0, bordercolor=C["PANEL"], lightcolor=C["PANEL"], darkcolor=C["PANEL"])
        # 대화 상자(별도 창)는 카드색 바탕
        st.configure("Dialog.TFrame", background=C["PANEL"])
        # ---- 아이콘(배율별로 구움) ----
        self.icons = {"receipt": img("ic_receipt", A.receipt(P(14), C["MUTED"], 1.4)), "lock": img("ic_lock", A.lock(P(13), C["INK"])),
                      "lock_seal": img("ic_lock_seal", A.lock(P(13), C["SEAL_HI"])),
                      "folder": img("ic_folder", A.folder(P(14), C["INK"])), "folder_dis": img("ic_folder_dis", A.folder(P(14), C["EDGE"])),
                      "folder_muted": img("ic_folder_muted", A.folder(P(14), C["MUTED"])), "check": img("ic_check", A.check(P(13), C["GREEN"])),
                      "clip": img("ic_clip", A.clip(P(14), "#ffffff")), "clip_dis": img("ic_clip_dis", A.clip(P(14), C["EDGE"])),
                      "expand": img("ic_expand", A.expand(P(12), C["MUTED"])),
                      "receipt_dis": img("ic_receipt_dis", A.receipt(P(14), C["EDGE"], 1.4)),          # 흐린 설정 줄(쓰이지 않는 모델 사용)의 영수증
                      "file": img("ic_file", A.file_doc(P(14), C["INK"])), "file_dis": img("ic_file_dis", A.file_doc(P(14), C["EDGE"]))}
        self._circles = {k: img("step_" + k, A.step_circle(P(26), k, C["INK"], C["EDGE"], C["RED"], C["PANEL"], self.scale))
                         for k in ("done", "now", "wait", "skip", "fail", "stop")}

    # ---------- 화면 부품 ----------
    def _card(self, parent, title: str | None = None, pad=None) -> ttk.Frame:
        """둥근 모서리 카드. 제목이 있으면 위에 굵게 적고, 내용은 card.body 에 넣는다."""
        P = self.px
        outer = ttk.Frame(parent, style="Card.TFrame", padding=pad or (P(18), P(12) if title else P(14), P(18), P(14)))
        if title:
            ttk.Label(outer, text=title, style="CardTitle.TLabel").pack(anchor="w", pady=(0, P(8)))
        body = ttk.Frame(outer, style="Panel.TFrame"); body.pack(fill="both", expand=True)
        outer.body = body                                   # type: ignore[attr-defined]
        return outer

    def _hint(self, parent, text: str = "", page: bool = False, **kw) -> ttk.Label:
        """흐린 안내 글. 차지한 너비에 맞춰 줄을 바꾼다(창이 좁아도 잘리지 않게)."""
        # width=1: 안내 글이 칸 너비를 정하지 않게 한다(줄바꿈 너비 ↔ 칸 너비가 서로를 바꾸며 끝없이 되먹임되는 것을 막음).
        # 실제 너비는 칸이 주는 만큼이고, 그 너비에 맞춰 줄을 바꾼다.
        lab = ttk.Label(parent, text=text, style="PageHint.TLabel" if page else "Hint.TLabel", justify="left", width=1, **kw)

        def wrap(e):
            w = max(self.px(40), e.width - 2)
            if abs(int(str(lab.cget("wraplength") or 0)) - w) > 3:
                lab.configure(wraplength=w)
        lab.bind("<Configure>", wrap)
        return lab

    def _field_label(self, parent, text: str, row: int, top: int = 3):
        ttk.Label(parent, text=text, style="Field.TLabel").grid(row=row, column=0, sticky="w", padx=(0, self.px(12)), pady=(self.px(top), self.px(3)))

    def _tabs(self, parent, items: list[tuple[str, int]], var: tk.IntVar, command) -> tk.Frame:
        """머리띠의 밑줄 탭(라디오 단추: 고른 탭은 굵게 + 아래 3px 먹 막대). 굵게 바뀌어도 너비가 흔들리지 않게 굵은 글 너비로 잡고,
        탭 소개(TAB_HINTS)는 풍선 도움말로 보인다."""
        f = tk.Frame(parent, bg=self.colors["PANEL"])
        try:
            from tkinter import font as tkfont
            bold, zero = tkfont.Font(self, font=self.fonts["TAB_ON"]), max(1, tkfont.Font(self, font=self.fonts["TAB"]).measure("0"))
        except Exception:
            bold = zero = None
        for text, val in items:
            width = -(-bold.measure(text) // zero) if bold else 0
            rb = ttk.Radiobutton(f, text=text, value=val, variable=var, command=command, cursor="hand2", style="Tab.TRadiobutton", width=width)
            rb.pack(side="left", fill="y", padx=(0, self.px(2)))
            if 0 <= val < len(TAB_HINTS):
                Tip(rb, TAB_HINTS[val], self)
        return f

    def _seal(self, parent, chars: str, large: bool = True, bg: str | None = None) -> tk.Canvas:
        """도장: 인주색 둥근 사각 + 안쪽 가는 테(gui_art.seal) 위에 두 글자를 세로로(흰 맑은 고딕 굵게). large 는 48px, 아니면 34px."""
        from . import gui_art as A
        C, P = self.colors, self.px
        size = P(48 if large else 34)
        key = f"seal_{size}_{int(large)}"
        if key not in self._img:
            self._img[key] = tk.PhotoImage(master=self, data=A.seal(size, C["SEAL"], C["SEAL_LINE"], large=large, scale=self.scale))
        cv = tk.Canvas(parent, width=size, height=size, bg=bg or C["PANEL"], highlightthickness=0, borderwidth=0)
        cv.create_image(0, 0, image=self._img[key], anchor="nw")
        font = self.fonts["SEAL" if large else "SEAL_S"]
        try:
            from tkinter import font as tkfont
            step = tkfont.Font(self, font=font).measure("가") * 1.05        # 한 글자 높이 ≈ 한글 한 자 너비(시안의 줄 높이 1.05)
        except Exception:
            step = size * 0.32
        for k, ch in enumerate((chars + "  ")[:2]):
            cv.create_text(size / 2, size / 2 + (k - 0.5) * step, text=ch, fill="#ffffff", font=font)
        return cv

    def _cw(self, values, extra: int = 3) -> int:
        """고르기 칸 너비(글자 '0' 단위): 가장 긴 값이 다 보이게."""
        try:
            from tkinter import font as tkfont
            f = tkfont.Font(self, font=self.font_base); zero = max(1, f.measure("0"))
            return max(6, -(-max(f.measure(str(v)) for v in values) // zero) + extra)
        except Exception:
            return max(6, max(len(str(v)) for v in values) * 2)

    def select_tab(self, i: int):
        """작업별 입력을 보관하고 설정 탭에서는 마지막 작업의 상태를 유지한다."""
        if self.running:
            self.v_tab.set(self._selected_tab)
            return
        if self._inputs_ready and i in (0, 2) and i != self._active_input:
            self._save_book(create=False)                    # 칸을 바꾸기 전에 — v_mode·v_pages·v_off 가 아직 앞 탭(앞 책)의 것
        self._selected_tab = i
        self.v_tab.set(i)
        if i != 3:
            self._last_ocr_tab = i
            self.v_mode = self._mode_vars[2 if i == 2 else 0]
            self.cb_mode.configure(textvariable=self.v_mode)
        if i in (0, 2):
            self._input_force[self._active_input] = self.v_force.get()
            self._active_input = i
            variables = self._input_vars[i]
            self.v_pdf, self.v_pages, self.v_off = variables["pdf"], variables["pages"], variables["print_offset"]
            for name, var in (("e_print_offset", self.v_off), ("e_pages_set", self.v_pages)):     # 설정 탭 '이 책'의 칸 — 지금 입력 탭(책)의 값에 다시 묶는다
                if getattr(self, name, None) is not None:
                    getattr(self, name).configure(textvariable=var)
            self.v_force.set(self._input_force[i])
            if self._inputs_ready and self._book["dir"] != self._book_dir():
                self._load_book(clear_if_missing=False)      # 그 탭 원본의 책별 설정(시작할 때도). 탭마다 기억한 쪽 범위·보정은 파일이 없으면 그대로
            elif self._inputs_ready:
                App._sync_book_label(self)
            self._sync_output_location()
        d = vars(self)
        if d.get("v_notice") is not None:
            self.v_notice.set(SETTINGS_NOTE)                 # 설정 탭 알림 줄은 탭을 옮기면 평소 글로
            if d.get("_notice_tip") is not None:
                self._notice_tip.text = ""
        if d.get("v_book_adv") is not None and i in (0, 2, 3) and self.v_off.get().strip() and not self.v_book_adv.get():
            self.v_book_adv.set(True); App._apply_book_adv(self)        # 인쇄 쪽수 보정 값이 있으면 '이 책' 고급을 펼친 채로(숨은 보정이 없게)
        work = d.get("_work")                              # 일반·이미지 OCR 의 두 단(그 책 자리에 탭 틀 0·2 가 든다)
        if work is not None:
            if i in (0, 2):
                work.pack(fill="both", expand=True)
            else:
                work.pack_forget()
        for k, f in enumerate(self.tab_frames):
            if k == i:
                f.pack(fill="both", expand=True) if k == 1 else f.pack(fill="x")      # 책갈피 OCR 은 기록 카드가 남은 높이를 채우게
            else:
                f.pack_forget()
        self.v_tabhint.set(TAB_HINTS[i])
        if hasattr(self, "_canvas"):
            self._canvas.yview_moveto(0)
        for k, a in enumerate(getattr(self, "act_cards", [])):   # 탭마다의 실행 자리: 일반·이미지 → 실행 카드, 책갈피 → 교정 실행 카드, 설정 → 바닥 줄
            if k == (2 if i == 3 else 0 if i == 2 else i):
                a.pack(fill="x")
            else:
                a.pack_forget()
        foot = d.get("_footer")                            # 설정 탭의 바닥 고정 줄(스크롤 밖)
        if foot is not None:
            if i == 3:
                foot.pack(side="bottom", fill="x", before=self._body)
            else:
                foot.pack_forget()
        App._place_cards(self)
        if self._inputs_ready:
            self._sync_settings_summary()
            self.refresh_table()
        App._schedule_cards(self)

    def return_to_ocr(self):
        self.select_tab(self._last_ocr_tab)

    def _sync_settings_summary(self, *_):
        document = "HWPX + DOCX" if self.v_docx.get() else "HWPX"
        if getattr(self, "v_tlpdf", None) is not None and self.v_tlpdf.get():
            document += " + 검색 PDF"
        if getattr(self, "v_layout", None) is not None and self.layout_key() == "flow":
            tpl = self.v_template.get().strip()
            document += " · 내용별" + (f" · 양식 {Path(tpl).name}" if tpl else " · 양식 없음(기본 모양)")
            ref = App.preset_ref(self); lab = preset_label(ref)
            document += " · 짜임 " + (lab.split(" (")[0] if (ref or DEFAULT_PRESET) in BUILTIN or ref in ALIASES else lab)   # 파일은 '(읽지 못함)' 까지 — 실행 전에 보이게
        self.v_settings_summary.set(f"{self.v_provider.get()} · {self.v_model.get().strip()} · {self.v_mode.get()} · {document}")
        scope = "이미지 OCR" if self._active_input == 2 else "일반 OCR"
        self.v_force_scope.set(f"{scope}: 기록 있는 쪽도 다시 판독")

    def _build(self):
        C = self.colors; P = self.px
        # ---- 머리띠(54 · 종이 판 · 아래 LINE): 이름 · 밑줄 탭 · ……… 패키지 상태 · 판 번호 · [업데이트 확인] ----
        hd = self._header = tk.Frame(self, bg=C["PANEL"], height=P(54)); hd.pack(fill="x"); hd.pack_propagate(False)
        self._header_rule = tk.Frame(self, bg=C["LINE"], height=max(1, P(1))); self._header_rule.pack(fill="x")
        self.v_pkg = tk.StringVar(value="패키지 점검 중…")
        name = tk.Frame(hd, bg=C["PANEL"]); name.pack(side="left", padx=(P(24), P(28)))
        for text, font, fg, pad in (("연어OCR", self.fonts["APP"], C["INK"], 0), (APP_NAME, self.fonts["CAP"], C["MUTED"], P(10))):
            lab = tk.Label(name, text=text, font=font, bg=C["PANEL"], fg=fg, bd=0)
            lab.pack(side="left", padx=(pad, 0), pady=(P(3) if pad else 0, 0))
            Tip(lab, ABOUT, self)
        right = tk.Frame(hd, bg=C["PANEL"]); right.pack(side="right", padx=(P(14), P(20)))
        self.b_update = self._btn(right, "업데이트 확인", self.check_updates, style="Link.TButton",
                                  tip="GitHub 릴리스에서 새 버전을 확인합니다. 설치와 재시작은 확인 후 진행합니다.")
        self.b_update.pack(side="right")
        ttk.Label(right, text=f"v{__version__}", style="Chip.TLabel").pack(side="right", padx=(0, P(10)))
        self.v_tab = tk.IntVar(value=0); self.v_tabhint = tk.StringVar(value=TAB_HINTS[0])     # 탭 소개는 탭마다 풍선 도움말로
        tabs = self._tab_bar = self._tabs(hd, [("일반 OCR", 0), ("책갈피 OCR", 1), ("이미지(스크린샷) OCR", 2), ("설정", 3)], self.v_tab,
                                          lambda: self.select_tab(self.v_tab.get()))
        tabs.pack(side="left", fill="y"); self._tab_buttons = list(tabs.winfo_children())
        local_tools = tk.Frame(self, bg=C["PANEL"])
        local_tools.pack(fill="x")
        self.b_comparison = self._btn(local_tools, "3엔진 비교·교정", self.open_local_comparison,
            style="Link.TButton", tip="선택한 원본을 Tesseract·RapidOCR·EasyOCR로 비교하고 원본 옆에서 교정합니다. 로컬 처리이며 API를 호출하지 않습니다.")
        self.b_comparison.pack(side="left", padx=(P(24), P(12)), pady=(0, P(3)))
        tk.Label(local_tools, text="원본 대조 편집 · 로컬 처리", font=self.fonts["CAP"],
                 bg=C["PANEL"], fg=C["MUTED"]).pack(side="left")
        # 패키지 상태(탭 줄과 판 번호 사이의 남은 자리): 다 들어가지 않으면 ' → ' 앞(예 '설치 구성요소 오류 2개')만, 그래도 넘치면 '…요소 오류 2개' —
        # 안내 꼬리만 '파일로 다시 설치'처럼 남던 것을 고침. 전체 글은 풍선 도움말로
        pkg = self._pkg_label = tk.Label(hd, text=self.v_pkg.get(), font=self.fonts["CAP"], bg=C["PANEL"], fg=C["MUTED"], bd=0, anchor="e")
        pkg.pack(side="right", fill="x", expand=True, padx=(P(12), 0))
        self._pkg_tip = Tip(pkg, self.v_pkg.get(), self)
        self.v_pkg.trace_add("write", lambda *_: App._fit_pkg(self))
        pkg.bind("<Configure>", lambda e: App._fit_pkg(self), add="+")
        # 잠금 줄(실행 중에만): 탭이 왜 눌리지 않는지 — 끝나거나 [■ 중지]로 멈추면 풀린다
        lk = self._lockline = tk.Frame(self, bg=C["QUIET"], height=P(30)); lk.pack_propagate(False)
        tk.Frame(lk, bg=C["LINE"], height=max(1, P(1))).pack(side="bottom", fill="x")
        self._lock_label = tk.Label(lk, image=self.icons["lock"], compound="left", bg=C["QUIET"], fg=C["INK"], font=self.fonts["BODY"], bd=0, padx=0,
                                    text="  실행 중에는 다른 탭으로 옮길 수 없습니다. 끝나거나 [■ 중지]로 멈추면 풀립니다.")
        self._lock_label.pack(side="left", padx=(P(24), 0))
        # 본문은 스크롤 캔버스 하나(머리띠·잠금 줄은 고정 — 지난 판의 위·아래 칸과 경계 손잡이는 없앴다: 진행·기록 카드가 작업 탭 안에 든다).
        # 내용이 창보다 낮으면 캔버스 높이만큼 늘려 기록 카드가 남은 높이를 채우고(_fit_host), 높으면 스크롤한다.
        self._footer = tk.Frame(self, bg=C["PANEL"])                        # 설정 탭의 바닥 고정 줄(스크롤 밖) — select_tab 이 설정 탭에서만 보인다
        tk.Frame(self._footer, bg=C["LINE"], height=max(1, P(1))).pack(fill="x")
        body = self._body = tk.Frame(self, bg=C["BG"]); body.pack(fill="both", expand=True)
        cv = self._canvas = tk.Canvas(body, bg=C["BG"], highlightthickness=0, borderwidth=0, yscrollincrement=P(20))
        cv.pack(fill="both", expand=True)
        self._vsb = ttk.Scrollbar(body, orient="vertical", command=cv.yview); cv.configure(yscrollcommand=self._vsb.set)
        host = self._host = ttk.Frame(cv, padding=(P(24), P(16), P(24), P(18)))
        win = self._host_win = cv.create_window(0, 0, window=host, anchor="nw")
        host.bind("<Configure>", lambda e: (cv.configure(scrollregion=(0, 0, e.width, e.height)), self._update_scroll()))
        cv.bind("<Configure>", lambda e: (cv.itemconfigure(win, width=e.width), self._layout_work(), self._fit_host()))
        self.bind_all("<MouseWheel>", self._wheel, add="+"); self.bind_all("<Button-4>", self._wheel, add="+"); self.bind_all("<Button-5>", self._wheel, add="+")
        for cls in ("TCombobox", "TSpinbox"):                      # 휠로 위 칸을 굴리다 고르기 칸 위를 지나면 값이 몰래 바뀌던 문제
            for ev in ("<MouseWheel>", "<Button-4>", "<Button-5>", "<Shift-MouseWheel>"):
                self.bind_class(cls, ev, "")
        self.bind_all("<<TraverseIn>>", self._focus_into_view, add="+")   # Tab 으로 옮길 때만(마우스로 누를 때 화면이 튀어 클릭이 씹히지 않게)
        # 일반·이미지 OCR 탭: 두 단(왼쪽 책·진행·고급 도구·기록 | 오른쪽 340 실행·장부) — 논리 너비 1200 미만이면 한 단(_place_cards).
        # 카드는 _work 의 자식이고 열 틀(_work_left·_work_right)에 grid(in_=…)로 든다(열 틀을 먼저 만들어야 카드가 그 위에 보인다). 탭 틀 0·2 는 책 자리(_book_slot)에
        self._work = ttk.Frame(host); self._work_left = ttk.Frame(self._work); self._work_right = ttk.Frame(self._work)
        self._book_slot = ttk.Frame(self._work); self._run_slot = ttk.Frame(self._work)
        self._book_cards: dict[int, dict] = {}
        root = ttk.Frame(self._book_slot); tab2 = ttk.Frame(host); image_tab = ttk.Frame(self._book_slot)
        self._common_frame = common = ttk.Frame(host)
        self.tab_frames = [root, tab2, image_tab, common]
        self._inputs_ready = False; self._selected_tab = 0; self._active_input = 0; self._last_ocr_tab = 0
        self._book = {"dir": None, "loaded": {}}             # 칸에 불러온 책별 설정: 결과 폴더·그때의 칸 값(저장할 때 이 창이 바꾼 것만 얹으려고)
        states, self._initial_input_tab = ocr_input_config(self.cfg)
        self._input_vars = {tab: {key: tk.StringVar(value=state[key]) for key in ("pdf", "pages", "print_offset")}
                            for tab, state in states.items()}
        self._input_force = {tab: state["force"] for tab, state in states.items()}
        variables = self._input_vars[0]
        self.v_pdf, self.v_pages, self.v_off = variables["pdf"], variables["pages"], variables["print_offset"]
        self.v_output_dir = tk.StringVar(value="")                       # 결과 저장(읽기 전용 칸) — 원본이 없으면 비움
        self.v_settings_summary = tk.StringVar()
        self.v_force_scope = tk.StringVar()
        self.v_status = tk.StringVar(value="준비됨"); self.v_prog = tk.StringVar(value="")
        self._mode_vars = {tab: tk.StringVar(value=MODE_LABELS[mode]) for tab, mode in ocr_mode_config(self.cfg).items()}
        self.v_mode = self._mode_vars[0]
        for variables in self._input_vars.values():
            variables["pdf"].trace_add("write", self._sync_output_location)

        # ---- 설정 탭(디자인 명세 §6.3): 왼쪽 판독 · 이 책(끝에 접힌 고급) · 책갈피 OCR 교정 ▸ | 오른쪽 문서 만들기 · 자동 교정 · 모델 단계 ----
        # 변수(설정 열쇠)는 그대로 — 자리만 섹션 줄로 옮겼다(_build_settings_tab)
        self.v_up = tk.StringVar(value=self.cfg["upscale"]); self.v_reader = tk.StringVar(value=self.cfg["reader"])
        self.v_model = tk.StringVar(value=self.cfg["model"]); self.v_key = tk.StringVar(value=self.cfg["api_key"])
        self._active_reader = self.v_reader.get()
        self.v_provider = tk.StringVar(value=PROVIDER_LABELS[self._active_reader])
        self.v_second = tk.BooleanVar(value=self.cfg["second_pass"]); self.v_docx = tk.BooleanVar(value=self.cfg["docx"])
        self.v_tlpdf = tk.BooleanVar(value=bool(self.cfg.get("textlayer_pdf", False)))
        self.v_layout = tk.StringVar(value=LAYOUT_LABELS.get(self.cfg.get("layout", "page"), LAYOUT_LABELS["page"]))
        self.v_template = tk.StringVar(value=self.cfg.get("template", "") or "")
        self.v_bold = tk.BooleanVar(value=bool(self.cfg.get("bold_split", True)))
        self._preset_ref = self.cfg.get("preset") or DEFAULT_PRESET       # 짜임 규칙(내장 id·예전 이름·규칙 파일의 절대 경로) — 칸에는 그 이름(v_preset)
        self.v_preset = tk.StringVar(value=preset_label(self._preset_ref))
        self.v_ctxmodel = tk.BooleanVar(value=bool(self.cfg.get("context_model", True)))
        self.v_traditional = tk.BooleanVar(value=bool(self.cfg.get("normalize_traditional", True)))
        self.v_yun = tk.BooleanVar(value=bool(self.cfg.get("normalize_yun", True)))
        self.v_punctuation = tk.BooleanVar(value=bool(self.cfg.get("normalize_punctuation", True)))
        self.v_punctuation_rules = tk.StringVar(value=str(self.cfg.get("punctuation_rules", "") or ""))
        self.v_join = tk.BooleanVar(value=self.cfg["join_pages"]); self.v_force = tk.BooleanVar(value=self._input_force[0])
        self.v_hl = tk.BooleanVar(value=self.cfg.get("highlight", True)); self.v_pb = tk.BooleanVar(value=self.cfg.get("page_breaks", True))
        self.v_ctx = tk.BooleanVar(value=self.cfg.get("context_hint", True))
        self.v_memo = tk.StringVar(value=MEMO_LABELS.get(self.cfg.get("memo_mode", "notice"), MEMO_LABELS["notice"]))
        self.v_chk = tk.StringVar(value=CHECK_LIST_LABELS.get(self.cfg.get("check_list", "none"), CHECK_LIST_LABELS["none"]))
        self.v_workers = tk.StringVar(value=str(self.cfg.get("workers", 8))); self.v_model2 = tk.StringVar(value=self.cfg.get("second_model", ""))
        self.v_ckpt = tk.StringVar(value=str(self.cfg.get("checkpoint", 20))); self.v_cache = tk.BooleanVar(value=self.cfg.get("use_cache", True))
        self.v_think2 = tk.BooleanVar(value=self.cfg.get("second_thinking_low", True))
        self.v_blind = tk.BooleanVar(value=bool(self.cfg.get("adjudicate_blind", False)))
        self.v_chunk = tk.StringVar(value=str(self.cfg.get("chunk_lines", 8)))
        self.v_mres = tk.StringVar(value=MRES_LABELS.get(self.cfg.get("media_res", "high"), MRES_LABELS["high"]))
        self.v_details = tk.BooleanVar(value=bool(self.cfg.get("details_open", False)))
        self.v_suspects = tk.BooleanVar(value=bool(self.cfg.get("suspects", True)))
        self.v_skipany = tk.BooleanVar(value=bool(self.cfg.get("skip_any_model", True)))
        self.v_think1 = tk.StringVar(value=PAGE_THINK_LABELS.get(self.cfg.get("page_thinking", "auto"), PAGE_THINK_LABELS["auto"]))
        self.v_mres1 = tk.StringVar(value=PAGE_MRES_LABELS.get(self.cfg.get("page_media_res", "auto"), PAGE_MRES_LABELS["auto"]))
        self.v_maxcalls = tk.StringVar(value=str(self.cfg.get("max_calls", 0)))
        self.v_docbook = tk.StringVar()                                 # 이 책 섹션의 칸들이 어느 책의 책별 설정인지(_sync_book_label — 섹션 제목의 풍선 도움말)
        self.v_punctuation_label = tk.StringVar()                       # 자동 교정 '중문부호 규칙' 줄의 설명(지금 쓰는 규칙)
        # 고급 도구(단계별 실행·도구)는 작업 탭 기록 카드의 [고급 도구 ▸](_build_tools_card) — 펼침은 설정에 남긴다(advanced_open)
        self.v_adv = tk.BooleanVar(value=bool(self.cfg.get("advanced_open", False)))
        App._build_settings_tab(self, common)
        self.v_layout.trace_add("write", self._layout_changed); self.v_template.trace_add("write", self._layout_changed); self._layout_changed()

        self._build_book_card(root, 0)
        self._build_book_card(image_tab, 2)
        self._build_collate_tab(tab2)
        self._build_collate_settings(common)                              # 설정 탭의 접힌 책갈피 섹션(교정 정책·메모 변수가 생긴 뒤)
        App._layout_settings(self)
        self.select_tab(0)
        self._build_actions()                                               # 실행 카드·장부(일반·이미지) · 교정 실행 카드(책갈피) · 설정 탭 바닥 줄
        self._prog_card = self._build_progress_card(self._work)
        self._adv_card = self._build_tools_card(self._work)
        self._log_card = self._build_log_card(host)                         # 기록 카드는 하나 — 보이는 탭의 왼쪽 열로 옮긴다(_place_cards)
        self._reader_changed()
        for variable in (self.v_provider, self.v_model, *self._mode_vars.values(), self.v_docx, self.v_tlpdf, self.v_layout, self.v_template):
            variable.trace_add("write", self._sync_settings_summary)
        self._inputs_ready = True
        self._cards_ready = True                                            # 이제부터 칸이 바뀌면 책 카드·실행 카드·장부·진행 카드를 다시 그린다(_schedule_cards)
        watched = [v for t in self._input_vars.values() for v in t.values()] + list(self._mode_vars.values()) + [
            self.v_force, self.v_layout, self.v_template, self.v_preset, self.v_bold, self.v_provider, self.v_model, self.v_reader, self.v_model2,
            self.v_traditional, self.v_yun, self.v_punctuation, self.v_punctuation_rules, self.v_docx, self.v_tlpdf, self.v_ctxmodel, self.v_status,
            self.v_second, self.v_suspects, self.v_blind, self.v_skipany, self.v_up, self.v_cache, self.v_join, self.v_policy, self.v_cmemo]
        for variable in watched:
            variable.trace_add("write", self._schedule_cards)
        self.v_output_dir.trace_add("write", lambda *_: self.after_idle(self._scroll_out_ends))
        self.select_tab(self._initial_input_tab)
        self.update_idletasks()
        # 마우스로 누른 체크·라디오·두 갈래·글자 단추는 초점을 창으로 돌린다 — 초점 고리는 키보드(Tab)로 옮겼을 때만 보이게
        for cls in ("TCheckbutton", "TRadiobutton", "TButton"):
            self.bind_class(cls, "<ButtonRelease-1>", lambda e: self.after_idle(self._drop_mouse_focus, e.widget), add="+")

    # ---------- 설정 탭(디자인 명세 §6.3·§5.9, F_settings) ----------
    def _build_settings_tab(self, common) -> None:
        """설정 탭: 두 열(각 최소 518, 사이 16 — 논리 너비 1100 미만이면 한 열, _layout_settings). 왼쪽(읽는 쪽) 판독 · 이 책 — {원본}(끝에 접힌 고급) ·
        접힌 책갈피 OCR 교정(_build_collate_settings), 오른쪽(만드는 쪽) 문서 만들기 · 자동 교정 · 모델 단계. 줄은 Windows 설정처럼 이름 + 한 줄 설명 | 조작
        (_setting_row). 변수·설정 열쇠·명령·풍선 도움말은 그대로이고 자리만 옮겼다. 영수증은 켜거나 끄면 호출 수가 바뀌는 줄에만, `[아니요]면 뺌`은
        비용 창 [아니요]로 이번 실행에서 빠지는 것(모델 단계 제목·문맥 교정의 모델 사용)에만."""
        C, P = self.colors, self.px
        PANEL = C["PANEL"]
        cols = self._set_cols = {"left": ttk.Frame(common), "right": ttk.Frame(common)}    # 열 틀을 먼저(섹션이 그 위에 보이게)
        for f in cols.values():
            f.columnconfigure(0, weight=1)
        S = self._set_sections = {}
        R = self._set_rows = {}
        row, chars, L = self._setting_row, self._chars, self._lab
        got: dict = {}                                                   # 조작을 만드는 함수 안에서 만든 위젯(바깥에서 다시 쓰는 것)

        def boxed(parent):
            return tk.Frame(parent, bg=PANEL)

        def check(var):
            return lambda p: ttk.Checkbutton(p, variable=var, style="Bare.TCheckbutton")

        # ---- 판독(모든 책 공통) ----
        def details_button(parent):
            self.b_details = ttk.Button(parent, text="", command=self.toggle_details, style="Toggle.TButton", cursor="hand2")
            Tip(self.b_details, "2차 판독 모델 · 속도 · 2차 조각 · 판독 단계 · 1차 판독 · 호출 상한", self)
            return self.b_details
        s = S["read"] = self._settings_section(common, "판독", "모든 책 공통 · 세 OCR 탭이 함께 씀", right=details_button)
        b = s["body"]

        def provider(parent):
            cb = self.cb_provider = ttk.Combobox(parent, textvariable=self.v_provider, values=list(PROVIDER_LABELS.values()), state="readonly", width=chars(220))
            cb.bind("<<ComboboxSelected>>", self._select_provider)
            Tip(cb, "제공사를 바꿔도 같은 결과 폴더의 완료 기록을 이어 씁니다. '기록 있는 쪽도 다시 판독'은 꺼 두십시오. 제공사마다 API 키가 필요합니다.", self)
            return cb
        R["provider"] = row(b, "판독기", "제공사를 바꿔도 같은 결과 폴더의 기록을 이어 씀", provider)

        def model(parent):
            f = boxed(parent)
            self.e_model = ttk.Entry(f, textvariable=self.v_model, width=chars(220)); self.e_model.pack(side="left", fill="x", expand=True)
            self._btn(f, "키 확인·모델 고르기", self.check_key, tip="선택한 제공사에 키를 확인하고 모델 목록에서 고릅니다.").pack(side="left", padx=(P(6), 0))
            return f
        R["model"] = row(b, "모델", "이미지 입력을 지원하는 모델 · 제공사별로 기억", model, stretch=360)

        def key(parent):
            f = boxed(parent)
            self.e_key = ttk.Entry(f, textvariable=self.v_key, show="•", width=chars(166)); self.e_key.pack(side="left")
            Tip(self.e_key, "선택한 제공사의 API 키를 넣으십시오. 키·모델은 제공사별로 따로 저장됩니다. Gemini는 쉼표로 여러 프로젝트의 키를 넣을 수 있습니다.", self)
            self._btn(f, "보기", self.toggle_key, tip="키를 가리거나 보입니다. 키는 이 PC의 설정 파일에 제공사별로 저장됩니다.").pack(side="left", padx=(P(6), 0))
            return f
        R["key"] = row(b, "API 키", "이 PC의 설정 파일에 제공사별로 저장", key)
        R["force"] = row(b, "", "켜면 판독 기록이 있는 쪽도 다시 삼 · 책마다 남기지 않음", check(self.v_force), receipt=True, textvariable=self.v_force_scope)
        # 세부 설정(펼치면 판독 섹션 아래에 — 보통은 기본값). 3차 판별 세 줄은 '모델 단계' 섹션으로 옮겼다
        d = self.details = boxed(b)
        dh = boxed(d); dh.pack(fill="x")
        tk.Frame(dh, bg=C["LINE"], height=max(1, P(1))).pack(fill="x")
        L(dh, "세부 설정", "CAP_B", "MUTED").pack(side="left", pady=(P(7), P(3)))
        L(dh, " · 보통은 기본값 그대로 두십시오", "CAP", "MUTED").pack(side="left", pady=(P(7), P(3)))
        R["model2"] = row(d, "2차 판독 모델", "비우면 1차와 같은 모델 · auto = Gemini는 flash, 그 밖은 1차와 같음",
                          lambda p: got.setdefault("e_model2", ttk.Entry(p, textvariable=self.v_model2, width=chars(200))))
        self.e_model2 = got["e_model2"]

        def speed(parent):
            f = boxed(parent)
            L(f, "동시 최대").pack(side="left", padx=(0, P(4)))
            s1 = ttk.Spinbox(f, from_=1, to=12, width=3, textvariable=self.v_workers); s1.pack(side="left")
            L(f, "쪽 · 확대").pack(side="left", padx=(P(6), P(4)))
            ttk.Entry(f, textvariable=self.v_up, width=4).pack(side="left")
            L(f, "배 · 중간 저장").pack(side="left", padx=(P(6), P(4)))
            ttk.Entry(f, textvariable=self.v_ckpt, width=4).pack(side="left")
            L(f, "쪽마다").pack(side="left", padx=(P(4), 0))
            Tip(s1, "동시에 판독할 쪽 수의 최대. 4쪽에서 시작해 안정적이면 늘리고, 한도 초과(429)가 나면 저절로 줄입니다.", self)
            return f
        R["speed"] = row(d, "속도", "동시 쪽 수는 4쪽에서 시작해 안정적이면 늘림 · 확대 = 모델에 보낼 쪽 이미지 배율 · 중간 저장 0 = 안 함", speed)

        def chunk(parent):
            f = boxed(parent)
            L(f, "한 장에").pack(side="left", padx=(0, P(4)))
            s2 = ttk.Spinbox(f, from_=0, to=30, width=3, textvariable=self.v_chunk); s2.pack(side="left")
            L(f, "줄(열)까지 · 해상도").pack(side="left", padx=(P(6), P(4)))
            mr = got["mr"] = ttk.Combobox(f, textvariable=self.v_mres, values=list(MRES_LABELS.values()), state="readonly", width=self._cw(MRES_LABELS.values()))
            mr.pack(side="left")
            Tip(s2, "2차 확대 조각 한 장에 넣을 글줄(세로쓰기는 열) 수. 넘으면 나눠 보내 글자당 픽셀을 늘립니다(0 = 나누지 않음).", self)
            Tip(mr, "2차 조각 이미지의 토큰 예산. 높을수록 入/人·曰/日 같은 획 하나 차이를 잘 가립니다. 최고는 Gemini 3 전용이며 거부되면 저절로 높음으로 내립니다.", self)
            return f
        R["chunk"] = row(d, "2차 조각", "넘으면 나눠 보내 글자당 픽셀을 늘림(0 = 나누지 않음) · 최고 해상도는 Gemini 3 전용", chunk)

        def stages(parent):
            f = boxed(parent)
            ttk.Checkbutton(f, text="영역 확대 2차 독립 판독·비교", variable=self.v_second).pack(side="left", padx=(0, P(14)))
            self.ck_cache = ttk.Checkbutton(f, text="지침 캐시 (Gemini)", variable=self.v_cache); self.ck_cache.pack(side="left", padx=(0, P(14)))
            self.ck_think2 = ttk.Checkbutton(f, text="2차 생각 줄이기 (Gemini)", variable=self.v_think2); self.ck_think2.pack(side="left")
            return f
        R["stages"] = row(d, "판독 단계", "지침 캐시·2차 생각 줄이기는 Gemini에서만", stages)

        def page1(parent):
            f = boxed(parent)
            L(f, "생각 수준").pack(side="left", padx=(0, P(4)))
            t1 = got["t1"] = ttk.Combobox(f, textvariable=self.v_think1, values=list(PAGE_THINK_LABELS.values()), state="readonly", width=self._cw(PAGE_THINK_LABELS.values()))
            t1.pack(side="left")
            L(f, "· 쪽 해상도").pack(side="left", padx=(P(6), P(4)))
            m1 = got["m1"] = ttk.Combobox(f, textvariable=self.v_mres1, values=list(PAGE_MRES_LABELS.values()), state="readonly", width=self._cw(PAGE_MRES_LABELS.values()))
            m1.pack(side="left")
            Tip(t1, "1차 생각 수준: 쪽 전체 판독(영역 나누기·1차 글)의 생각 수준. 자동 = 모델 기본(지금까지와 같음). 낮추면 빠르고 싸지만 정확도가 달라질 수 있으니 "
                    "같은 표본 쪽을 결과 폴더만 바꿔 측정(yeone-ocr eval)으로 비교한 뒤 바꾸십시오. Gemini 3 은 생각 수준, 2.5 는 생각 예산(낮음 512 · 중간 4096 · 높음 24576, 2.5 Pro 32768). "
                    "모델이 받지 않으면 저절로 모델 기본으로 계속합니다.", self)
            Tip(m1, "1차 쪽 해상도: 쪽 전체 이미지의 토큰 예산. 자동 = 설정 없음(지금까지와 같음). 최고(ultra_high)는 Gemini 3 전용이며 거부되면 저절로 높음 → 기본으로 내립니다. "
                    "측정(yeone-ocr eval)으로 비교한 뒤 바꾸십시오.", self)
            return f
        R["page1"] = row(d, "1차 판독", "자동 = 모델 기본 · 바꾸기 전에 측정(yeone-ocr eval)으로 비교", page1)

        def maxcalls(parent):
            f = boxed(parent)
            mc_ = ttk.Spinbox(f, from_=0, to=1000000, increment=100, width=7, textvariable=self.v_maxcalls); mc_.pack(side="left")
            L(f, "회").pack(side="left", padx=(P(4), 0))
            Tip(mc_, "호출 상한(0=없음): 한 번 실행(한 번에 인식·판독·3차 판별·책갈피 교정)에서 모델을 부르는 횟수의 상한입니다. 재시도도 한 번으로 셉니다. "
                     "닿으면 멈추고 끝난 쪽은 기록에 남으며, 다시 누르면 남은 쪽부터 이어서 합니다. 비용이 걱정되면 미리 보기의 예상 호출 수보다 조금 크게 두십시오.", self)
            return f
        R["maxcalls"] = row(d, "호출 상한", "0 = 없음 · 재시도 포함, 한 번 실행 전체 · 닿으면 멈추고 다시 누르면 이어서", maxcalls)
        self._gemini_widgets = ((got["mr"], "readonly"), (got["t1"], "readonly"), (got["m1"], "readonly"), (self.ck_cache, "normal"), (self.ck_think2, "normal"))
        self._apply_details()

        # ---- 이 책 — {원본 이름}(책별 설정 일곱 가지 — book_settings.json) ----
        card = ttk.Frame(common, style="Card.TFrame", padding=(P(20), P(12), P(20), P(10)))
        bk = self._set_book = {"card": card}
        head = boxed(card); head.pack(fill="x", pady=(0, P(6)))
        bk["seal"] = boxed(head); bk["seal"].pack(side="left", padx=(0, P(10)))
        tf = boxed(head); tf.pack(side="left", fill="x", expand=True)
        bk["title"] = L(tf, "이 책", "SECTION", wrap=True); bk["title"].pack(fill="x")
        bk["sub"] = L(tf, "", "CAP", "MUTED", wrap=True); bk["sub"].pack(fill="x")
        bk["title_tip"] = Tip(bk["title"], "", self)
        b = boxed(card); b.pack(fill="x")
        S["book"] = {"card": card, "head": head, "body": b}

        def pages(parent):
            f = boxed(parent)
            e = self.e_pages_set = ttk.Entry(f, textvariable=self.v_pages, width=chars(150)); e.pack(side="left")   # select_tab 이 지금 입력 탭의 변수로 다시 묶는다
            self._lock_entries.append(e)
            Tip(e, "판독할 쪽. 비우면 원본 전체 · 예 1-5, 8", self)
            self._btn(f, "전체", lambda: self.v_pages.set(""), tip="비우면 원본 전체를 판독합니다.").pack(side="left", padx=(P(6), 0))
            return f
        R["pages"] = row(b, "쪽 범위", "비우면 전체 · 예 1-5, 8", pages)

        def mode(parent):
            cb = self.cb_mode = ttk.Combobox(parent, textvariable=self.v_mode, values=list(MODE_LABELS.values()), state="readonly", width=chars(220))
            Tip(cb, "자동 판정이면 PDF 글자층을 먼저 보고, 글자층이 부족한 이미지·스캔은 실제 글줄 배치로 가로·세로를 고릅니다. 판정 근거는 진행 기록에 표시합니다. "
                    "이미지 OCR의 방향은 따로 기억합니다.", self)
            return cb
        R["mode"] = row(b, "읽기 방향", "자동이면 글자층·글줄 배치로 가로·세로를 고름", mode)

        def layout(parent):
            lc = ttk.Combobox(parent, textvariable=self.v_layout, values=list(LAYOUT_LABELS.values()), state="readonly", width=chars(220))
            Tip(lc, "원본 쪽대로: PDF 한 쪽을 한글 한 쪽에(쪽마다 기록 줄·쪽 나누기) — 원본과 대조할 때. "
                    "내용별: 쪽을 넘는 문단을 잇고 제목·약재명·조문·주석으로 나눠 이어 씁니다. 원본 쪽은 그 쪽이 시작되는 글자 자리의 책갈피(PDF 6쪽 · 인쇄 394쪽)로 남깁니다.", self)
            return lc
        R["layout"] = row(b, "문서 짜임", "원본 쪽대로(검수용) 또는 내용별(제목·문단)", layout)

        def template(parent):
            f = boxed(parent)
            lab = self.te_template = ttk.Label(f, text="", style="SetVal.TLabel", compound="left")   # 이름만(전체 경로는 풍선) — 원본 쪽대로면 흐리게(_layout_changed)
            lab.pack(side="left", padx=(0, P(8)))
            self._tpl_tip = Tip(lab, "", self)
            self._btn(f, "고르기…", self.pick_template, tip="내용별 문서를 채울 양식 HWPX 를 고릅니다(비우면 기본 모양).").pack(side="left")
            self._btn(f, "비우기", lambda: self.v_template.set(""), tip="양식을 비웁니다 — 내용별 문서를 기본 모양으로 만듭니다.").pack(side="left", padx=(P(6), 0))
            return f
        R["template"] = row(b, "양식 HWPX", TPL_DESC, template)
        Tip(R["template"]["name"], "내용별 문서는 이 양식의 쪽 설정과, 짜임 규칙이 정한 스타일(本草 조문: 제목-장·제목-절·제목-본초명+생약명·본경 원문·별록원문·주석·소주·출처·그림캡션)로 "
                                   "채웁니다. 양식의 본문 내용은 쓰지 않습니다. 한문 문단은 줄 나눔만 글자 단위로 바꿉니다(어절 단위면 줄 가운데 큰 빈칸이 생김).", self)
        self.v_template.trace_add("write", lambda *_: App._sync_template_label(self))

        def preset(parent):
            sc = self.cb_preset = ttk.Combobox(parent, textvariable=self.v_preset, values=self._preset_values(), state="readonly", font=self.fonts["BODY_B"],
                                               width=max(chars(220), self._cw([lab for _, lab in preset_choices(DEFAULT_PRESET)]) - 2))
            sc.bind("<<ComboboxSelected>>", self._preset_selected)
            Tip(sc, "짜임 규칙은 판독한 제목·본문·주석을 문단 역할로 나누고 한글 스타일에 연결하는 기준입니다. "
                    "기본 규칙: 대제목·중제목·소제목, 본문(문단-1)·해설(문단-2)·소주(문단-3)·그림 설명(문단-4)·작업 메모(문단-5). "
                    "글꼴·크기 등 모양은 연결된 HWPX 스타일이 정합니다. 원문에 없는 제목이나 문단 단계는 만들지 않습니다. "
                    "영역 종류만: 판독의 제목·본문·소주 구분만 사용합니다. "
                    "本草 조문: 開寶本草·證類 계열 本草 — 약재명 제목·［…云］ 주석·唐附 출처·굵은 글씨 本經/보통 글씨 別錄(양식 HWPX 가 있을 때 획 굵기를 잽니다). "
                    "파일에서 고르기…: 다른 책에 맞춰 만든 규칙 JSON(README '짜임 규칙 파일 만들기'). "
                    "本草 조문 규칙을 파일로 저장…: 내장 규칙의 사본을 남깁니다 — 고쳐서 다른 책의 규칙으로(고르던 규칙은 그대로).", self)
            return sc
        R["preset"] = row(b, "짜임 규칙", PRESET_DESC, preset)
        R["bold"] = row(b, "획 굵기", BOLD_DESC, lambda p: got.setdefault("bold", ttk.Checkbutton(p, variable=self.v_bold, style="Bare.TCheckbutton")))
        self.cb_bold = got["bold"]
        Tip(self.cb_bold, "양식 HWPX 로 만드는 내용별 문서에서 쪽 이미지의 획 굵기를 재어, 짜임 규칙이 정한 쓰임으로 넣습니다(모델 호출 없음) — 本草 조문: 굵게 찍힌 조문은 "
                          "'본경 원문', 보통 굵기는 '별록원문', 한 문단에 섞여 있으면 굵은 범위만 '본경 조문' 글자 스타일. 판독이 빠뜨린 약재명 뒤 빈칸도 이때 잽니다"
                          "(굵은 글씨 해석이 없고 약재명 떼기만 있는 규칙도 그래서 잼). 양식이 없거나 짜임 규칙에 약재명 떼기가 없으면(영역 종류만) 재지 않고 흐려집니다.", self)
        # 고급 ▸: 인쇄 쪽수 보정(평소 접힘 — 값이 있으면 탭을 열 때마다 펼침: 숨은 보정이 검수 기록의 쪽수를 몰래 바꾸지 않게, 펼침은 저장하지 않음)
        adv = boxed(b); adv.pack(fill="x")
        tk.Frame(adv, bg=C["HAIR"], height=max(1, P(1))).pack(fill="x")
        ah = boxed(adv); ah.pack(fill="x", pady=(P(3), P(3)))
        self.b_book_adv = ttk.Button(ah, text="고급 ▸", style="SetToggle.TButton", cursor="hand2", command=self.toggle_book_adv)
        self.b_book_adv.pack(side="left")
        Tip(self.b_book_adv, "인쇄 쪽수 보정 — 비우면 자동 판정이라 평소에는 접어 둡니다", self)
        L(ah, "평소 접힘 · 값을 넣으면 펼친 채로 둠", "CAP", "MUTED").pack(side="left", padx=(P(8), 0))
        self._book_adv = boxed(adv)

        def offset(parent):
            f = boxed(parent)
            L(f, "원본 쪽수 +").pack(side="left", padx=(0, P(6)))
            e = self.e_print_offset = ttk.Entry(f, textvariable=self.v_off, width=chars(60)); e.pack(side="left")   # select_tab 이 지금 입력 탭의 변수로 다시 묶는다
            self._lock_entries.append(e)
            Tip(e, "책에 적힌 쪽수와 파일 쪽수를 맞춰 검수 기록에 표시합니다. 원본 파일과 실제 쪽 순서는 바뀌지 않습니다. 비우면 자동 판정합니다.", self)
            return f
        R["offset"] = row(self._book_adv, "인쇄 쪽수 보정", "책에 적힌 쪽수와 파일 쪽수를 맞춰 검수 기록에 표시 · 원본 파일·쪽 순서는 그대로 · "
                                                           "비우면 자동(원본 1쪽이 인쇄 30쪽이면 29)", offset)
        self.v_book_adv = tk.BooleanVar(value=bool(self.v_off.get().strip()))
        App._apply_book_adv(self)
        # 바닥: 저장 상태와 경로(이 책의 설정 판과 같은 말)
        tk.Frame(card, bg=C["LINE"], height=max(1, P(1))).pack(fill="x", pady=(P(2), P(7)))
        sv = bk["save_row"] = boxed(card); sv.pack(fill="x"); sv.columnconfigure(2, weight=1)
        bk["save_icon"] = L(sv, ""); bk["save_icon"].grid(row=0, column=0, sticky="w")
        bk["save_state"] = L(sv, "", "CAP_B"); bk["save_state"].grid(row=0, column=1, sticky="w")
        bk["save_path"] = L(sv, "", "CAP", "MUTED", wrap=True)
        sv.bind("<Configure>", lambda ev: App._fit_save_row(self, bk, ev.width))

        # ---- 문서 만들기(모든 책 공통 · 모델 호출 없음) ----
        s = S["docs"] = self._settings_section(common, "문서 만들기", "모든 책 공통 · 모델 호출 없음")
        b = s["body"]
        grades = [("초록", "g"), (" 3차 자형 판별로 고름 · ", ""), ("파랑", "b"), (" 확인 권장 · ", ""), ("빨강〔〕", "r"), (" 원본 확인 필수", "")]
        checks = (("hl", "확인 등급 색 (초록·파랑·빨강)", self.v_hl, grades,
                   "글자를 얼마나 믿어도 되는지 색과 밑줄로: 초록 = 1차·2차가 갈려 3차 자형 판별로 고름(훑어 확인) · 파랑 = 골랐지만 근거가 약하거나 다른 대목과 "
                   "어긋남(확인 권장) · 빨강 〔 〕 = 정하지 못함, 원본 확인 필수(지침 19). 끄면 색 없이 〔 〕 표기만 남습니다."),
                  ("ctx", "문맥 추정 밑줄 (확정 아님)", self.v_ctx, "〔a/b〕 안에서 더 많이 쓰는 후보에 실선 밑줄 · 글자는 그대로",
                   "빨간 〔a/b〕 안에서, 이 판독의 다른 대목(1차·2차가 같게 읽은 글)이 더 많이 쓰는 후보에 실선 밑줄을 긋습니다. 글자는 바꾸지 않고 두 후보도 "
                   "그대로 둡니다(지침 2·7·19·21). 숫자·이체자·판독불명·자형이 다른 쌍에는 긋지 않습니다. 이 OCR 자신의 합의에서 센 힌트이며 문턱은 아직 재지 않았습니다."),
                  ("pb", "원본 쪽마다 쪽 나누기", self.v_pb, PB_DESC[False],
                   "원본 쪽대로 만들 때 PDF 한 쪽마다 한글 쪽을 나눕니다. 내용별 문서에서는 쓰지 않습니다(원본 쪽은 책갈피로 남김)."),
                  ("join", "앞 쪽에서 이어지는 문단 잇기", self.v_join, "", None), ("docx", "DOCX 도 함께 만들기", self.v_docx, "", None),
                  ("tlpdf", "검색 가능한 PDF 도 만들기", self.v_tlpdf, "HWPX를 만들 때 함께 · out\\<이름>_글자층.pdf · PDF만 원하면 [한 번에 인식 (문자 PDF 만들기)]",
                   "문서를 만들 때 원본 PDF 쪽 위에 판독한 글을 보이지 않는 글자층으로 넣은 사본(out\\<이름>_글자층.pdf)도 만듭니다. 화면은 원본 그대로이고 PDF 뷰어에서 "
                   "찾기·복사가 됩니다. 원본에 다른 OCR 의 숨은 글자층이 있으면 그 쪽에서는 지우고 넣습니다. 원본 PDF 는 고치지 않으며 모델을 부르지 않습니다. "
                   "고급 도구의 [PDF 글자층 넣기]로 따로 만들 수도 있습니다."))
        for k, title, var, desc, tip in checks:
            R[k] = row(b, title, desc, check(var))
            if tip:
                Tip(R[k]["ctrl"], tip, self); Tip(R[k]["name"], tip, self)
        self.cb_pb = R["pb"]["ctrl"]
        wide = chars(240)

        def memo(parent):
            mc = ttk.Combobox(parent, textvariable=self.v_memo, values=list(MEMO_LABELS.values()), state="readonly", width=wide)
            Tip(mc, "고지 사항만(기본): 색으로 전할 수 없는 것 — 본문에 표시되지 않은 확인 항목, 구조 검사 오류, 3차와 다른 대목의 어긋남 — 과 첫머리의 표시 범례에만 "
                    "한컴 메모를 답니다. 모든 표시에 메모: 초록·파랑·문맥 밑줄 자리마다 근거 메모. 메모 없이: 고지를 회색 〔고지: …〕 로 글 속에.", self)
            return mc
        R["memo"] = row(b, "메모", "색으로 전할 수 없는 고지에만 한컴 메모(기본)", memo)

        def checklist(parent):
            cc = ttk.Combobox(parent, textvariable=self.v_chk, values=list(CHECK_LIST_LABELS.values()), state="readonly", width=wide)
            Tip(cc, "문서 끝에 확인 목록을 넣을지. 기본은 넣지 않으며(문서에는 OCR 본문만), 전체 목록은 검수 화면에 있습니다.", self)
            return cc
        R["checklist"] = row(b, "쪽 끝 확인 목록", "전체 목록은 검수 화면에 있음", checklist)

        # ---- 자동 교정(모든 책 공통 · HWPX·DOCX에만) — 규칙 셋은 모델 호출 없음, 돈은 '불확실한 후보에 모델 사용' 한 줄(LINE 으로 갈라 맨 아래) ----
        s = S["auto"] = self._settings_section(common, "자동 교정", "모든 책 공통 · HWPX·DOCX에만 · 판독 기록·문자 PDF는 그대로 · 바꾼 내역은 out의 교정 보고에")
        b = s["body"]

        def rules(parent):
            f = boxed(parent)
            items = (("간체→번체", self.v_traditional), ("云/雲 문맥 판별", self.v_yun), ("중문부호 수정 규칙", self.v_punctuation))
            for i, (label, variable) in enumerate(items):
                ttk.Checkbutton(f, text=label, variable=variable).pack(side="left", padx=(0, P(16) if i < len(items) - 1 else 0))
            return f
        R["rules"] = row(b, "적용할 교정", "규칙으로 바꿈 · 모델 호출 없음", rules)

        def punctuation(parent):
            f = boxed(parent); self._punc_btns = []
            for label, command in (("규칙 추가·수정…", self.edit_punctuation_rules), ("파일에서 고르기…", self.pick_punctuation_rules),
                                   ("기본 규칙", lambda: self.v_punctuation_rules.set(""))):
                bt = self._btn(f, label, command, style="Small.TButton"); bt.pack(side="left", padx=(P(6) if self._punc_btns else 0, 0))
                self._punc_btns.append(bt)
            return f
        R["punc"] = row(b, "중문부호 규칙", "", punctuation, desc_var=self.v_punctuation_label)
        self._punc_tip = Tip(R["punc"]["desc"], "", self)

        def label_changed(*_):
            path = self.v_punctuation_rules.get().strip()
            self.v_punctuation_label.set(f"지금: {Path(path).name if path else '내장 기본 규칙'} · 위에서 아래 순서로 바꿈")
            self._punc_tip.text = path
        self.v_punctuation_rules.trace_add("write", label_changed)
        label_changed()
        R["ctxmodel"] = row(b, "번체·云/雲의 불확실한 후보에 모델 사용", "간체→번체나 云/雲을 켰을 때만 · 끄면 규칙으로 확인되는 것만 바꾸고 나머지는 교정 보고에",
                            check(self.v_ctxmodel), receipt=True, tag=True, line="LINE")
        Tip(R["ctxmodel"]["ctrl"], "간체→번체 또는 云/雲 교정을 선택했을 때만 적용합니다. 끄면 규칙으로 확인되는 것만 바꾸고 불확실한 후보는 보고에 남깁니다.", self)
        for v in (self.v_traditional, self.v_yun, self.v_punctuation):
            v.trace_add("write", lambda *_: App._sync_setting_states(self))

        # ---- 모델 단계 [아니요]면 뺌(판독 뒤 모델을 더 부르는 단계 — 셋 다 끄거나 켜면 호출 수가 바뀜) ----
        card = ttk.Frame(common, style="Card.TFrame", padding=(P(20), P(12), P(20), P(6)))
        head = boxed(card); head.pack(fill="x")
        L(head, "모델 단계", "SECTION").pack(side="left")
        tg = ttk.Label(head, text="[아니요]면 뺌", style="Tag.TLabel"); tg.pack(side="left", padx=(P(8), 0))
        Tip(tg, TAG_TIP, self)
        L(card, "판독 뒤 모델을 더 부르는 단계입니다. 실행 전 비용 창에서 [아니요]를 누르면 이번 실행에서 뺍니다. 켜거나 끄면 호출 수가 바뀌는 설정에는 영수증 표를 "
                "붙였습니다. 문맥 교정의 모델 사용은 위 [자동 교정]에 있습니다.", "CAP", "MUTED", wrap=True).pack(fill="x", pady=(P(3), P(6)))
        b = boxed(card); b.pack(fill="x")
        S["model"] = {"card": card, "head": head, "body": b, "tag": tg}
        R["suspects"] = row(b, "합의 판독도 글자층과 다르면 자형으로 확인(표시만)", "합의 판독 의심 · 쪽당 최대 8곳 · 본문은 그대로", check(self.v_suspects), receipt=True)
        Tip(R["suspects"]["ctrl"], "1차·2차가 같게 읽어 〔a/b〕 가 없는 글자도, PDF 글자층(기존 OCR)과 한 글자만 다르고 그 두 글자가 入/人·曰/日·問/間 처럼 자형이 헷갈리는 쌍이면 "
                                   "3차 판별과 같은 방식으로 글자 조각을 묻습니다. 자형이 글자층 쪽이면 본문은 판독대로 두고 확인 항목 「합의 판독 의심」과 문서 메모만 답니다"
                                   "(지침 20 — 기존 OCR 은 의심만 올림). 이미 물은 자리는 다시 묻지 않습니다. 끄면 〔a/b〕 자리만 묻습니다.", self)
        R["blind"] = row(b, "3차 판별: 후보 없이 한 번 더 읽어 같을 때만 확정", "호출 조금 더 · 더 엄격 · 책갈피 교정에도 적용(그쪽은 [아니요] 없음)",
                         check(self.v_blind), receipt=True)
        Tip(R["blind"]["ctrl"], "3차 판별은 앞뒤 글자를 글로 알려 주지 않고(조각 그림에는 이웃 글자가 보입니다) 후보 차례를 섞어 자형만 묻습니다. "
                                "이것을 켜면 고른 자리를 후보도 주지 않고 한 번 더 읽게 해, 두 답이 같을 때만 확정합니다. 다르면 표시〔a/b〕를 그대로 남깁니다. "
                                "호출이 더 듭니다(일반 OCR: 쪽마다 확정 자리 8곳당 1회 · 책갈피 OCR 교정에도 적용: 확정 항목마다 1회).", self)
        R["skipany"] = row(b, "다른 모델이 이미 물은 자리·후보는 다시 묻지 않기", "3차 판별·문맥 교정 · 끄면 이번 모델로 다시 물음(호출 듦)", check(self.v_skipany), receipt=True)
        Tip(R["skipany"]["ctrl"], "판독기나 모델을 바꿔도, 앞서 다른 모델이 물어 결론 난 것은 다시 사지 않습니다: 3차 판별에서 그 모델이 고르지 못한 〔a/b〕 자리는 표시대로 두고, "
                                  "문맥 교정에서 그 모델이 고른 답은 그대로 쓰며 고르지 못한 후보는 원문대로 둡니다. 이번 모델로 다시 물으려면 이것을 끄십시오(호출이 듭니다). "
                                  "3차 판별을 모두 다시 물으려면 명령줄 --readjudicate.", self)

    def _settings_section(self, parent, title: str, sub: str = "", right=None) -> dict:
        """설정 탭 섹션: 둥근 카드(안쪽 12×20) · 머리(제목 F_SECTION + 부제 F_CAP MUTED — 좁으면 줄바꿈, 오른쪽 끝에 right(틀)이 만든 펼침 단추) · 몸통."""
        C, P = self.colors, self.px
        card = ttk.Frame(parent, style="Card.TFrame", padding=(P(20), P(12), P(20), P(6)))
        head = tk.Frame(card, bg=C["PANEL"]); head.pack(fill="x", pady=(0, P(6)))
        head.columnconfigure(1, weight=1)
        t = self._lab(head, title, "SECTION"); t.grid(row=0, column=0, sticky="nw")
        s = self._lab(head, sub, "CAP", "MUTED", wrap=True); s.grid(row=0, column=1, sticky="new", padx=(P(10), 0), pady=(P(2), 0))   # 부제가 접혀도 제목은 첫 줄에
        if right is not None:
            right(head).grid(row=0, column=2, sticky="ne", padx=(P(10), 0))
        body = tk.Frame(card, bg=C["PANEL"]); body.pack(fill="x")
        return {"card": card, "head": head, "title": t, "sub": s, "body": body}

    def _chars(self, px: int) -> int:
        """칸 너비(100% 논리 px) → ttk 칸의 width(글자 '0' 수)."""
        try:
            zero = max(1, self._font("BODY").measure("0"))
        except tk.TclError:
            zero = 8
        return max(3, int(round(self.px(px) / zero)))

    def _setting_row(self, parent, title: str = "", desc="", control=None, *, receipt: bool = False, tag: bool = False, line: str = "HAIR",
                     stretch: int = 0, textvariable=None, desc_var=None) -> dict:
        """설정 줄(명세 §5.9): 위에 가는 줄(HAIR — 돈이 드는 갈림은 LINE) · 왼쪽 이름(F_BODY, 영수증·꼬리표가 이름 뒤에)과 한 줄 설명(F_CAP MUTED, 너비에 맞춰
        줄바꿈) · 오른쪽 조작(control(틀)이 만든 위젯). 이름 열 200 + 16 + 조작이 줄보다 넓으면 조작을 이름 아래로(_fit_row — stretch 는 늘어나는 조작의 최소
        너비, 모델 칸). desc 가 [(글, 꼬리)] 목록이면 색 글을 섞은 글줄. 체크 상자 줄은 이름·설명을 눌러도 켜고 끈다(HTML 의 label for 처럼)."""
        C, P, F = self.colors, self.px, self.fonts
        PANEL = C["PANEL"]
        f = tk.Frame(parent, bg=PANEL); f.pack(fill="x")
        f.columnconfigure(0, weight=1)
        tk.Frame(f, bg=C[line], height=max(1, P(1))).grid(row=0, column=0, columnspan=2, sticky="ew")
        left = tk.Frame(f, bg=PANEL); left.grid(row=1, column=0, sticky="ew", pady=P(5))
        nl = tk.Frame(left, bg=PANEL); nl.pack(fill="x")
        name = tk.Label(left, text=title, font=F["BODY"], fg=C["INK"], bg=PANEL, bd=0, padx=0, pady=0, justify="left", anchor="w", wraplength=P(260))
        if textvariable is not None:
            name.configure(textvariable=textvariable)
        name.pack(in_=nl, side="left")
        rc = None
        if receipt:
            rc = tk.Label(left, image=self.icons["receipt"], bg=PANEL, bd=0, padx=0, pady=0)
            rc.pack(in_=nl, side="left", padx=(P(5), 0))
            Tip(rc, "켜거나 끄면 모델 호출 수가 바뀌는 설정(돈이 듦)", self)
        tg = None
        if tag:
            tg = ttk.Label(left, text="[아니요]면 뺌", style="Tag.TLabel")
            tg.pack(in_=nl, side="left", padx=(P(6), 0))
            Tip(tg, TAG_TIP, self)
        dl = None
        if isinstance(desc, list):
            dl = App._rich_line(self, left, font="CAP", fg="MUTED"); App._set_rich(self, dl, desc); dl.pack(fill="x")
        elif desc or desc_var is not None:
            dl = tk.Label(left, text=desc, font=F["CAP"], fg=C["MUTED"], bg=PANEL, bd=0, padx=0, pady=0, justify="left", anchor="w", wraplength=P(260))
            if desc_var is not None:
                dl.configure(textvariable=desc_var)
            dl.pack(fill="x")
        ctrl = control(f) if control is not None else None
        r = {"frame": f, "left": left, "name_line": nl, "name": name, "receipt": rc, "tag": tg, "desc": dl, "ctrl": ctrl, "stretch": stretch,
             "below": None, "tag_beside": True, "muted": False}
        if ctrl is not None:
            ctrl.grid(row=1, column=1, sticky="e", padx=(P(16), 0), pady=P(4))
            if isinstance(ctrl, ttk.Checkbutton):
                def toggle(_e, c=ctrl):
                    if c.instate(["!disabled"]):
                        c.invoke()
                for w in (name, dl):
                    if isinstance(w, tk.Label):
                        w.bind("<Button-1>", toggle, add="+"); w.configure(cursor="hand2")
        f.bind("<Configure>", lambda e: App._fit_row(self, r, e.width))
        return r

    def _fit_row(self, r: dict, width: int) -> None:
        """설정 줄 맞추기: 조작이 이름 열(200) 옆에 들어가지 않으면 이름 아래로, 이름·설명은 남은 너비로 줄바꿈, 꼬리표는 이름 옆에 자리가 없으면 다음 줄."""
        if width <= 1:
            return
        P = self.px
        f, ctrl, st = r["frame"], r["ctrl"], r["stretch"]
        need = max(ctrl.winfo_reqwidth(), P(st)) if ctrl is not None else 0
        below = ctrl is not None and width < P(200) + P(16) + need
        if below != r["below"]:
            r["below"] = below
            if below:
                f.columnconfigure(0, weight=1, minsize=0); f.columnconfigure(1, weight=0)
                ctrl.grid_configure(row=2, column=0, columnspan=2, sticky="ew" if st else "w", padx=0, pady=(0, P(7)))
            else:
                f.columnconfigure(0, weight=0 if st else 1, minsize=0); f.columnconfigure(1, weight=1 if st else 0)
                ctrl.grid_configure(row=1, column=1, columnspan=1, sticky="ew" if st else "e", padx=(P(16), 0), pady=P(4))
        if ctrl is None or below:
            avail = width
        elif st:                                                          # 늘어나는 조작: 이름 열(바탕 200)과 조작(바탕 stretch)이 남는 너비를 반씩(시안의 flex 1 1)
            avail = P(200) + max(0, width - P(200) - P(16) - need) // 2
            if int(f.grid_columnconfigure(0)["minsize"]) != avail:
                f.columnconfigure(0, minsize=avail)
        else:
            avail = width - P(16) - ctrl.winfo_reqwidth()
        avail = max(P(80), avail)
        side = r["receipt"].winfo_reqwidth() + P(5) if r["receipt"] is not None else 0
        tg = r["tag"]
        if tg is not None:
            try:
                text_w = self._font("BODY").measure(r["name"].cget("text"))
            except tk.TclError:
                text_w = 0
            beside = text_w + side + P(6) + tg.winfo_reqwidth() <= avail
            if beside != r["tag_beside"]:
                r["tag_beside"] = beside
                if beside:
                    tg.pack(in_=r["name_line"], side="left", anchor="center", padx=(P(6), 0), pady=0)
                else:
                    tg.pack(in_=r["left"], after=r["name_line"], side="top", anchor="w", padx=0, pady=(P(3), P(1)))
            if beside:
                side += P(6) + tg.winfo_reqwidth()
        for w, wl in ((r["name"], max(P(40), avail - side)), (r["desc"], avail)):
            if isinstance(w, tk.Label) and int(float(str(w.cget("wraplength") or 0))) != wl:
                w.configure(wraplength=wl)

    def _mute_row(self, r: dict, muted: bool) -> None:
        """쓰이지 않는 설정 줄: 이름 MUTED, 영수증은 흐린 것으로, 꼬리표도 흐리게(값과 조작은 부르는 쪽이 정함)."""
        if r.get("muted") == muted:
            return
        r["muted"] = muted
        r["name"].configure(fg=self.colors["MUTED" if muted else "INK"])
        if r["receipt"] is not None:
            r["receipt"].configure(image=self.icons["receipt_dis" if muted else "receipt"])
        if r["tag"] is not None:
            r["tag"].state(["disabled"] if muted else ["!disabled"])

    def _layout_settings(self, width: int | None = None) -> None:
        """설정 탭 두 열 ↔ 한 열(캔버스 너비 ÷ 배율 < 1100): 두 열은 왼쪽 판독·이 책·책갈피 | 오른쪽 문서 만들기·자동 교정·모델 단계,
        한 열은 판독 → 이 책 → 책갈피 → 문서 만들기 → 자동 교정 → 모델 단계. 섹션은 설정 탭 틀의 자식이라 열 틀에 grid(in_=…)로 옮긴다."""
        d = vars(self)
        cols, S = d.get("_set_cols"), d.get("_set_sections")
        if not cols or not S:
            return
        if width is None:
            try:
                width = self._canvas.winfo_width()
            except (AttributeError, tk.TclError):
                width = 0
        two = width / getattr(self, "scale", 1.0) >= 1100 if width > 1 else True
        if two == d.get("_set_two"):
            return
        self._set_two = two
        P = self.px; common = self._common_frame
        order = [S[k]["card"] for k in ("read", "book", "collate", "docs", "auto", "model") if k in S]
        if two:
            for c in (0, 1):
                common.columnconfigure(c, weight=1, uniform="set", minsize=P(518))
            cols["left"].grid(row=0, column=0, sticky="new")
            cols["right"].grid(row=0, column=1, sticky="new", padx=(P(16), 0))
            n = len(order) - 3
            place = [(cols["left"], order[:n]), (cols["right"], order[n:])]
        else:
            common.columnconfigure(0, weight=1, uniform="", minsize=0); common.columnconfigure(1, weight=0, uniform="", minsize=0)
            cols["left"].grid(row=0, column=0, sticky="new"); cols["right"].grid_remove()
            place = [(cols["left"], order)]
        for col, cards in place:
            for r, card in enumerate(cards):
                card.grid(in_=col, row=r, column=0, sticky="ew", pady=(P(14) if r else 0, 0))
                card.lift()

    def _sync_settings(self) -> None:
        """설정 탭의 살아 있는 글: 이 책 섹션(제목·도장·부제·저장 상태), 쓰이지 않는 줄 흐리게, 양식 이름."""
        if vars(self).get("_set_book") is None:
            return
        App._sync_settings_book(self)
        App._sync_setting_states(self)
        App._sync_template_label(self)

    def _sync_settings_book(self) -> None:
        """'이 책 — {원본 이름}' 머리(작은 도장 · 부제 '아래 7가지(고급 포함)는 이 책에만 적용됩니다 · {탭} 탭에서 고른 책')와 바닥의 저장 상태(이 책의 설정 판과 같은 말)."""
        bk = vars(self).get("_set_book")
        if bk is None:
            return
        C, F = self.colors, self.fonts
        pdf = self.pdf(); scope = "이미지 OCR" if self._active_input == 2 else "일반 OCR"
        _title, chars, _fname = book_title(pdf)
        bk["title"].configure(text=f"이 책 — {Path(pdf).name}" if pdf else "이 책 — 원본 없음")
        bk["sub"].configure(text=f"아래 7가지(고급 포함)는 이 책에만 적용됩니다 · {scope} 탭에서 고른 책" if pdf
                            else f"{scope} 탭에서 원본을 고르면 아래 7가지(고급 포함)는 그 책의 값으로 바뀝니다")
        App._set_seal(self, bk, "seal", chars, large=False)
        bk["title_tip"].text = self.v_docbook.get()
        state = App._book_save_state(self)
        d = self._book.get("dir")
        where = f"— {d.name}{os.sep}{BOOK_SETTINGS}" if d is not None and state in ("saved", "changed", "unsaved") else ""
        text, color, font, icon = SAVE_LOOKS[state]
        bk["save_icon"].configure(image=self.icons["check"] if icon else "")
        bk["save_icon"].grid_configure(padx=(0, self.px(5)) if icon else 0)
        bk["save_state"].configure(text=text, fg=C[color], font=F[font])
        bk["save_path"].configure(text="" if state == "none" else (where + " · " if where else "· ") + "바꾼 값은 다음 실행에 적용")
        bk["save_fit"] = None
        App._fit_save_row(self, bk, bk["save_row"].winfo_width())

    def _sync_setting_states(self) -> None:
        """자동 교정의 흐린 줄: '중문부호 수정 규칙'을 끄면 '중문부호 규칙' 줄과 단추 셋, 간체→번체·云/雲을 둘 다 끄면 '불확실한 후보에 모델 사용' 줄과 체크 상자
        (값은 그대로 — 다시 켜면 돌아옴, _context_model_enabled 가 이미 무시함). 실행이 끝나 self.buttons 를 다시 켠 뒤에도 부른다(_set_running)."""
        R = vars(self).get("_set_rows")
        if not R:
            return
        punc = bool(self.v_punctuation.get())
        App._mute_row(self, R["punc"], not punc)
        for bt in vars(self).get("_punc_btns", ()):
            bt.configure(state="normal" if punc and not self.running else "disabled")
        ctx = bool(self.v_traditional.get() or self.v_yun.get())
        App._mute_row(self, R["ctxmodel"], not ctx)
        R["ctxmodel"]["ctrl"].state(["!disabled"] if ctx else ["disabled"])

    def _sync_layout_rows(self, flow: bool, bold: bool) -> None:
        """문서 짜임에 따라 쓰이지 않는 줄을 흐리게 하고 그 까닭을 설명 끝에(_layout_changed 가 칸을 켜고 끈 뒤): 내용별이면 '원본 쪽마다 쪽 나누기',
        원본 쪽대로면 양식·짜임 규칙, 재지 않는 조건이면 획 굵기(_bold_view 의 까닭)."""
        R = self._set_rows
        App._mute_row(self, R["pb"], flow)
        R["pb"]["desc"].configure(text=PB_DESC[flow])
        App._mute_row(self, R["template"], not flow); App._mute_row(self, R["preset"], not flow)
        R["template"]["desc"].configure(text=TPL_DESC + ("" if flow else " · 원본 쪽대로라 쓰지 않음"))
        R["preset"]["desc"].configure(text=PRESET_DESC + ("" if flow else " · 원본 쪽대로라 쓰지 않음"))
        App._mute_row(self, R["bold"], not bold)
        reason = "" if bold else App._bold_view(self, flow, self.v_template.get().strip(), App.preset_ref(self))[0]
        R["bold"]["desc"].configure(text=BOLD_DESC + (f" · 지금은 {reason}" if reason else ""))
        App._sync_template_label(self)

    def _sync_template_label(self) -> None:
        """양식 HWPX 줄: 파일 아이콘 + 이름(굵게, 170 에서 가운데 줄임 · 풍선 전체 경로) — 없으면 MUTED '없음 — 기본 모양'. 원본 쪽대로면 흐린 아이콘."""
        lab = vars(self).get("te_template")
        if lab is None or not isinstance(lab, ttk.Label):
            return
        tpl = self.v_template.get().strip()
        flow = self.layout_key() == "flow"
        if tpl:
            text = elide_middle(Path(tpl).name, self._font("BODY_B"), self.px(170))
            lab.configure(text=" " + text, style="SetVal.TLabel", image=self.icons["file" if flow else "file_dis"])
        else:
            lab.configure(text="없음 — 기본 모양", style="SetNone.TLabel", image="")
        self._tpl_tip.text = tpl

    def toggle_book_adv(self):
        """'이 책' 섹션 끝의 [고급 ▸]: 인쇄 쪽수 보정 줄을 펼치거나 접는다(펼침은 저장하지 않음 — 값이 있으면 설정 탭을 열 때마다 펼침)."""
        self.v_book_adv.set(not self.v_book_adv.get()); App._apply_book_adv(self)
        if self.v_book_adv.get():
            self.after(60, lambda: self._reveal(self._book_adv))

    def _apply_book_adv(self):
        box = vars(self).get("_book_adv")
        if box is None:
            return
        on = bool(self.v_book_adv.get())
        if on:
            box.pack(fill="x")
        else:
            box.pack_forget()
        self.b_book_adv.configure(text="고급 ▴" if on else "고급 ▸")

    def toggle_collate_settings(self):
        """접힌 '책갈피 OCR 교정 ▸' 섹션을 펼치거나 접는다(교정 정책 · 메모 · 책갈피 OCR: 기록 있는 쪽도 다시 판독)."""
        s = self._set_sections["collate"]
        on = not self.v_collate_set.get(); self.v_collate_set.set(on)
        if on:
            s["body"].pack(fill="x", pady=(self.px(6), 0))
        else:
            s["body"].pack_forget()
        self.b_collate_set.configure(text="책갈피 OCR 교정 ▴" if on else "책갈피 OCR 교정 ▸")
        if on:
            self.after(60, lambda: self._reveal(s["card"]))

    def _notice(self, text: str) -> None:
        """설정 탭 바닥의 알림 줄: 설정 탭이 보이는 동안 바뀐 상태 글(v_status)이나 마지막 기록 줄을 보인다(설정 탭에는 기록 카드가 없음).
        '준비됨'은 알릴 것이 없다는 뜻이라 평소 글을 그대로 둔다. 탭을 옮기면 평소 글(select_tab)."""
        d = vars(self); v = d.get("v_notice")
        if v is None or d.get("_selected_tab") != 3:
            return
        text = (text or "").strip()
        if text and text != "준비됨":
            v.set(short_paths(text))
            tip = d.get("_notice_tip")
            if tip is not None:
                tip.text = text if short_paths(text) != text else ""

    def pick_punctuation_rules(self):
        path = filedialog.askopenfilename(title="중문부호 수정 규칙", filetypes=[("JSON 규칙", "*.json")])
        if not path:
            return
        try:
            from .normalization import load_punctuation_rules
            load_punctuation_rules(path)
        except (ValueError, OSError) as exc:
            messagebox.showerror(APP_NAME, str(exc)); return
        self.v_punctuation_rules.set(str(Path(path).resolve()))

    def edit_punctuation_rules(self):
        try:
            from .gui_corrections import PunctuationRulesDialog
            PunctuationRulesDialog(self)
        except (ValueError, OSError) as exc:
            messagebox.showerror(APP_NAME, f"규칙을 열 수 없습니다. [기본 규칙] 또는 [파일에서 고르기]로 사용할 규칙을 선택하십시오.\n{exc}")

    def _corrections_ok(self):
        variable = vars(self).get("v_punctuation_rules")
        enabled = vars(self).get("v_punctuation")
        path = variable.get().strip() if variable is not None else ""
        if path and (enabled is None or enabled.get()):
            try:
                from .normalization import load_punctuation_rules
                load_punctuation_rules(path)
            except (ValueError, OSError) as exc:
                messagebox.showerror(APP_NAME, f"자동 교정의 중문부호 규칙을 확인하십시오.\n{exc}")
                return False
        return True

    def _normalization_opts(self):
        options = []
        for name, flag in (("v_traditional", "--no-traditional"), ("v_yun", "--no-yun"), ("v_punctuation", "--no-punctuation")):
            variable = vars(self).get(name)
            if variable is not None and not variable.get():
                options.append(flag)
        variable = vars(self).get("v_punctuation_rules")
        if variable is not None and variable.get().strip():
            options += ["--punctuation-rules", variable.get().strip()]
        return options

    def _context_model_enabled(self):
        def enabled(name):
            variable = vars(self).get(name)
            return variable is None or bool(variable.get())
        return enabled("v_ctxmodel") and (enabled("v_traditional") or enabled("v_yun"))

    # ---------- 작업 탭: 책 카드 · 이 책의 설정 · 실행 카드 · 장부(디자인 명세 §6.1·§6.5·§5.6·§5.7) ----------
    def _lab(self, parent, text: str = "", font: str = "BODY", fg: str = "INK", bg: str = "PANEL", wrap: bool = False, **kw) -> tk.Label:
        """색 토큰·글꼴 이름(self.fonts 의 열쇠)으로 만드는 tk.Label. wrap 이면 차지한 너비에 맞춰 줄을 바꾼다(width=1 — 글이 칸 너비를 정하지 않게)."""
        C = self.colors
        lab = tk.Label(parent, text=text, font=self.fonts.get(font, font), fg=C.get(fg, fg), bg=C.get(bg, bg), bd=0, highlightthickness=0,
                       padx=0, pady=0, **kw)
        if wrap:
            lab.configure(width=1, justify="left", anchor="w")

            def fit(e):
                w = max(self.px(40), e.width - 2)
                if abs(int(str(lab.cget("wraplength") or 0)) - w) > 3:
                    lab.configure(wraplength=w)
            lab.bind("<Configure>", fit, add="+")
        return lab

    def _counts_line(self, parent, bg: str = "PANEL") -> dict:
        """'대상 46쪽 · 판독 기록 1쪽 · 새로 읽을 45쪽' 한 줄(접힌 책 줄·이미지 탭) — 수만 먹 굵게."""
        f = tk.Frame(parent, bg=self.colors[bg]); out = {"frame": f}
        for key, text in (("target", "대상 "), ("records", " · 판독 기록 "), ("to_read", " · 새로 읽을 ")):
            self._lab(f, text, "BODY", "MUTED", bg).pack(side="left")
            out[key] = self._lab(f, "—", "BODY_B", "INK", bg); out[key].pack(side="left")
            out[key + "_u"] = self._lab(f, "", "BODY", "MUTED", bg); out[key + "_u"].pack(side="left")
        return out

    def _build_book_card(self, parent, tab: int) -> None:
        """일반·이미지 OCR 탭의 책 카드. 펼친 카드 = 정체 열(도장·제목·수치·원본 경로·단추·결과 저장 — 이미지 탭은 제목 한 줄 + 큰 원본 상자 + 수치 한 줄)
        | '이 책의 설정' 판(책별 설정 state.BOOK_KEYS 그대로: 쪽 범위·읽기 방향·문서 짜임·양식·짜임 규칙·획 굵기, 인쇄 쪽수는 값이 있을 때만)
        + 모든 책 공통 줄 + 저장 상태. 안 너비가 730 보다 좁으면 판이 정체 아래로. 실행 중에는 한 줄로 접는다(도장·제목·수치 · 이 책의 설정 · 잠김 ▸ ·
        [원본 보기]). 글·상태는 _sync_cards 가 채운다."""
        C, P = self.colors, self.px
        images = tab == 2; variables = self._input_vars[tab]
        L = self._lab; Q = C["QUIET"]
        w: dict = {"tab": tab, "expanded": False}
        self._book_cards[tab] = w
        # ---- 접힌 한 줄(실행 중): 도장 34 · 제목 · 파일 이름 · 수치 ……… 자물쇠 이 책의 설정 · 잠김 ▸ · [원본 보기] ----
        row = w["row"] = ttk.Frame(parent, style="Card.TFrame", padding=(P(20), P(9)))
        rb = tk.Frame(row, bg=C["PANEL"]); rb.pack(fill="x")
        w["row_seal"] = tk.Frame(rb, bg=C["PANEL"]); w["row_seal"].pack(side="left", padx=(0, P(12)))
        w["row_title"] = L(rb, "", "NUM"); w["row_title"].pack(side="left")
        w["row_fname"] = L(rb, "", "CAP", "MUTED"); w["row_fname"].pack(side="left", padx=(P(10), P(16)), pady=(P(4), 0))
        w["row_counts"] = self._counts_line(rb); w["row_counts"]["frame"].pack(side="left", pady=(P(3), 0))
        rr = tk.Frame(rb, bg=C["PANEL"]); rr.pack(side="right")
        self._btn(rr, "원본 보기", self.open_source, lock=False, tip="원본을 엽니다 — 열기만 하므로 실행 중에도 눌립니다.").pack(side="right")
        w["row_toggle"] = ttk.Button(rr, text=" 이 책의 설정 · 잠김 ▸", image=self.icons["lock"], compound="left", style="Toggle.TButton", cursor="hand2",
                                     command=lambda: App._toggle_locked_book(self, tab))
        w["row_toggle"].pack(side="right", padx=(0, P(12)))
        Tip(w["row_toggle"], "실행 중에는 고칠 수 없습니다 — 펼치면 이 책의 설정을 읽기 전용으로 봅니다", self)
        # ---- 펼친 카드 ----
        card = w["card"] = ttk.Frame(parent, style="Card.TFrame", padding=(P(20), P(16)))
        card.pack(fill="x")
        body = w["body"] = tk.Frame(card, bg=C["PANEL"]); body.pack(fill="both", expand=True)
        ident = w["ident"] = tk.Frame(body, bg=C["PANEL"]); ident.columnconfigure(0, weight=1)
        panel = w["panel"] = ttk.Frame(body, style="Quiet.TFrame", padding=(P(14), P(12)))
        body.bind("<Configure>", lambda e: App._layout_book(self, w, e.width))
        App._layout_book(self, w, 10 ** 6)
        gap = P(10) if images else P(12)
        # 정체 열 · 머리: 도장 + 제목 + 파일 이름(원본이 없으면 '원본을 고르십시오' + 안내)
        top = tk.Frame(ident, bg=C["PANEL"]); top.grid(row=0, column=0, sticky="ew")
        w["seal"] = tk.Frame(top, bg=C["PANEL"]); w["seal"].pack(side="left", padx=(0, P(10) if images else P(14)))
        if images:
            w["title"] = L(top, "", "NUM"); w["title"].pack(side="left")
            w["fname"] = L(top, "", "CAP", "MUTED", wrap=True); w["fname"].pack(side="left", fill="x", expand=True, padx=(P(10), 0), pady=(P(4), 0))
            w["sub"] = L(ident, "이미지를 여러 장 고르면 이름의 숫자 순서로 한 권으로 묶습니다.", "BODY", "MUTED", wrap=True)
        else:
            tt = tk.Frame(top, bg=C["PANEL"]); tt.pack(side="left", fill="x", expand=True)
            w["title"] = L(tt, "", "TITLE", wrap=True); w["title"].pack(fill="x")
            w["fname"] = L(tt, "", "BODY", "MUTED", wrap=True); w["fname"].pack(fill="x")
        w["title_tip"] = Tip(w["fname"], "", self)
        if images:
            # 큰 원본 상자(168): 누르면 초점 → Ctrl+V · 오른쪽 메뉴 · 두 번 누르면 원본. 빈 상자 가운데 먹 단추 [이미지 붙여넣기](마우스만 쓰는 길)
            from .gui_preview import ImageInputPreview
            pv = self.image_preview = ImageInputPreview(ident, variable=variables["pdf"], paste=self.paste_screenshot, open_source=self.open_source,
                                                        height=P(168), colors=self.colors, fonts=self.fonts, scale=self.scale, zoom_icon=self.icons["expand"])
            pv.grid(row=2, column=0, sticky="ew", pady=(gap, 0))
            pb = self._btn(pv.canvas, " 이미지 붙여넣기", lambda: (self.paste_screenshot(), pv.canvas.focus_set()), style="QuietInk.TButton",
                           image=(self.icons["clip"], "disabled", self.icons["clip_dis"]), compound="left",
                           tip="Win+Shift+S로 캡처한 이미지를 붙여 넣습니다(상자를 누르고 Ctrl+V와 같음)")
            pv.set_empty_widget(pb)
            w["counts_line"] = self._counts_line(ident); w["counts_line"]["frame"].grid(row=3, column=0, sticky="w", pady=(gap, 0))
        else:
            # 수치 3칸(위아래 LINE · 칸 사이 LINE 세로줄)
            cf = tk.Frame(ident, bg=C["PANEL"]); cf.grid(row=1, column=0, sticky="ew", pady=(gap, 0))
            hl = max(1, P(1))
            tk.Frame(cf, bg=C["LINE"], height=hl).grid(row=0, column=0, columnspan=5, sticky="ew")
            tk.Frame(cf, bg=C["LINE"], height=hl).grid(row=2, column=0, columnspan=5, sticky="ew")
            w["counts"] = []
            for i, name in enumerate(("대상", "판독 기록", "새로 읽을")):
                if i:
                    tk.Frame(cf, bg=C["LINE"], width=hl).grid(row=1, column=2 * i - 1, sticky="ns")
                cell = tk.Frame(cf, bg=C["PANEL"]); cell.grid(row=1, column=2 * i, sticky="nsew", padx=(P(14) if i else 0, 0), pady=P(6))
                L(cell, name, "CAP", "MUTED").pack(anchor="w")
                vf = tk.Frame(cell, bg=C["PANEL"]); vf.pack(anchor="w")
                num = L(vf, "—", "NUM"); num.pack(side="left")
                unit = L(vf, "", "BODY"); unit.pack(side="left", anchor="s", padx=(P(2), 0), pady=(0, P(3)))
                w["counts"].append((num, unit))
                cf.columnconfigure(2 * i, weight=1, uniform="counts")
            # 원본 경로(가운데 줄임 · 풍선 전체 경로 · 오른쪽 클릭 '경로 복사')
            pr = w["path_row"] = tk.Frame(ident, bg=C["PANEL"]); pr.grid(row=2, column=0, sticky="ew", pady=(gap, 0))
            L(pr, image=self.icons["folder_muted"]).pack(side="left", padx=(0, P(7)))
            w["path"] = L(pr, "", "CAP", "MUTED", anchor="w", width=1); w["path"].pack(side="left", fill="x", expand=True)
            w["path"].bind("<Configure>", lambda _e: App._fit_path(self, w), add="+")
            w["path_tip"] = Tip(w["path"], "", self)
            menu = tk.Menu(pr, tearoff=False)
            menu.add_command(label="경로 복사", command=lambda v=variables["pdf"]: App._copy_text(self, v.get().strip()))
            w["path"].bind("<Button-3>", lambda e: App._popup(menu, e))
        br = tk.Frame(ident, bg=C["PANEL"]); br.grid(row=4, column=0, sticky="w", pady=(gap, 0))
        if images:
            w["pick"] = self._btn(br, "이미지 고르기…", self.choose_images, tip="이미지를 여러 장 고르면 이름의 숫자 순서로 한 권으로 묶습니다.")
            w["clear"] = self._btn(br, "입력 비우기", self.clear_image_input,
                                   tip="불러온 이미지 선택과 쪽 범위를 모두 비우고 새 입력을 기다립니다. 저장된 원본·판독 기록·결과는 보존합니다.")
            w["pick"].pack(side="left"); w["clear"].pack(side="left", padx=(P(8), 0))
        else:
            w["pick"] = self._btn(br, "PDF 고르기…", self.choose_pdf, tip="판독할 스캔 PDF를 고릅니다.")
            w["source"] = self._btn(br, "원본 보기", self.open_source, lock=False, tip="고른 원본 PDF를 엽니다 — 열기만 하므로 실행 중에도 눌립니다.")
            w["pick"].pack(side="left"); w["source"].pack(side="left", padx=(P(8), 0))
        # 결과 저장(읽기 전용 — 전체 경로를 끝으로 굴려 보이고 Ctrl+C 는 진짜 경로를 복사) + [저장 폴더]. 결과를 여는 단추는 실행 카드에
        w["out_label"] = L(ident, "", "CAP", "MUTED", wrap=True); w["out_label"].grid(row=5, column=0, sticky="ew", pady=(gap, P(3)))
        orow = tk.Frame(ident, bg=C["PANEL"]); orow.grid(row=6, column=0, sticky="ew")
        out = w["out"] = ttk.Entry(orow, textvariable=self.v_output_dir, state="readonly", width=1)
        out.pack(side="left", fill="x", expand=True)
        out.bind("<Configure>", lambda _e: out.xview_moveto(1.0), add="+")
        Tip(out, "HWPX·문자 PDF가 저장되는 out 폴더 — 전체 경로(Ctrl+C 로 복사)", self)
        w["outdir"] = self._btn(orow, " 저장 폴더", self.open_output_folder, lock=False, image=(self.icons["folder"], "disabled", self.icons["folder_dis"]),
                                compound="left", tip="HWPX·문자 PDF가 저장되는 out 폴더를 탐색기로 엽니다.")
        w["outdir"].pack(side="left", padx=(P(8), 0))
        # ---- '이 책의 설정' 판(QUIET): 머리 · 6행(+ 인쇄 쪽수) · 바닥(모든 책 공통 · 저장 상태) ----
        ph = tk.Frame(panel, bg=Q); ph.pack(fill="x")
        L(ph, "이 책의 설정", "SECTION", bg="QUIET").pack(side="left")
        w["panel_cap"] = L(ph, "", "CAP", "MUTED", bg="QUIET"); w["panel_cap"].pack(side="left", padx=(P(10), 0), pady=(P(2), 0))
        w["fix"] = self._btn(ph, "설정에서 고치기", lambda: self.select_tab(3), style="QLink.TButton",
                             tip="설정 탭에서 이 책의 설정(쪽 범위·인쇄 쪽수·읽기 방향·문서 짜임·양식·짜임 규칙·획 굵기)을 고칩니다.")
        w["fix"].pack(side="right")
        w["fold"] = ttk.Button(ph, text=" 잠김 · 접기 ▴", image=self.icons["lock"], compound="left", style="QToggle.TButton", cursor="hand2",
                               command=lambda: App._toggle_locked_book(self, tab))
        foot = tk.Frame(panel, bg=Q); foot.pack(side="bottom", fill="x", pady=(P(8), 0))
        g = tk.Frame(panel, bg=Q); g.pack(fill="x", pady=(P(8), 0))
        g.columnconfigure(0, minsize=P(72)); g.columnconfigure(1, weight=1)

        def name(r: int, text: str, sticky: str = "w"):
            lab = L(g, text, "BODY", "MUTED", bg="QUIET"); lab.grid(row=r, column=0, sticky=sticky, padx=(0, P(10)), pady=P(2))
            return lab
        name(0, "쪽 범위")
        pf = w["pages_row"] = tk.Frame(g, bg=Q); pf.grid(row=0, column=1, sticky="ew", pady=P(2))
        e = ttk.Entry(pf, textvariable=variables["pages"], width=11, style="Quiet.TEntry"); e.grid(row=0, column=0, sticky="w")
        self._lock_entries.append(e)
        Tip(e, "판독할 쪽. 비우면 원본 전체 · 예 1-5, 8", self)
        allb = self._btn(pf, "전체", lambda v=variables["pages"]: v.set(""), style="QuietSmall.TButton", tip="비우면 원본 전체를 판독합니다.")
        allb.configure(width=0)
        allb.grid(row=0, column=1, padx=(P(6), 0))
        w["pages_hint"] = L(pf, "비우면 전체 · 예 1-5, 8", "CAP", "MUTED", bg="QUIET")
        w["pages_parts"] = (e, allb)
        pf.bind("<Configure>", lambda ev: App._fit_pages_hint(self, w, ev.width))
        App._fit_pages_hint(self, w, 10 ** 6)
        w["off_name"] = L(g, "인쇄 쪽수", "BODY", "MUTED", bg="QUIET")
        of = w["off_row"] = tk.Frame(g, bg=Q)
        L(of, "원본 쪽수 ", "BODY", bg="QUIET").pack(side="left")
        w["off_val"] = L(of, "", "BODY_B", bg="QUIET"); w["off_val"].pack(side="left")
        w["off_link"] = L(of, "· [설정] → 이 책 → 고급에서 고침", "CAP", "MUTED", bg="QUIET", cursor="hand2")
        w["off_link"].pack(side="left", padx=(P(6), 0), pady=(P(2), 0))
        w["off_link"].bind("<Button-1>", lambda _e: App._goto_print_offset(self))
        Tip(w["off_link"], "인쇄 쪽수 보정은 [설정] 탭 '이 책' 섹션의 고급에 있습니다(비우면 자동 판정). 누르면 그 칸으로 갑니다.", self)
        for r, (text, key) in enumerate((("읽기 방향", "mode"), ("문서 짜임", "layout"), ("양식", "template"), ("짜임 규칙", "preset"), ("획 굵기", "bold")), start=2):
            name(r, text, "nw" if key == "preset" else "w")
            if key == "preset":
                pr_ = tk.Frame(g, bg=Q); pr_.grid(row=r, column=1, sticky="ew", pady=P(2))
                w["v_preset"] = L(pr_, "", "BODY_B", bg="QUIET", wrap=True); w["v_preset"].pack(fill="x")
                w["v_preset_hint"] = L(pr_, "", "CAP", "MUTED", bg="QUIET", wrap=True); w["v_preset_hint"].pack(fill="x")
            else:
                w["v_" + key] = L(g, "", "BODY", bg="QUIET", wrap=True); w["v_" + key].grid(row=r, column=1, sticky="ew", pady=P(2))
        w["tip_template"] = Tip(w["v_template"], "", self); w["tip_preset"] = Tip(w["v_preset"], "", self)
        tk.Frame(foot, bg=C["LINE"], height=max(1, P(1))).pack(fill="x", pady=(0, P(7)))
        w["common"] = L(foot, "", "CAP", "MUTED", bg="QUIET", wrap=True); w["common"].pack(fill="x")
        sv = w["save_row"] = tk.Frame(foot, bg=Q); sv.pack(fill="x", pady=(P(1), 0)); sv.columnconfigure(2, weight=1)
        w["save_icon"] = L(sv, "", bg="QUIET"); w["save_icon"].grid(row=0, column=0, sticky="w")
        w["save_state"] = L(sv, "", "CAP_B", bg="QUIET"); w["save_state"].grid(row=0, column=1, sticky="w")
        w["save_path"] = L(sv, "", "CAP", "MUTED", bg="QUIET", wrap=True)
        sv.bind("<Configure>", lambda ev: App._fit_save_row(self, w, ev.width))

    def _layout_book(self, w: dict, width: int) -> None:
        """책 카드 안 두 열(정체 330 : 판 380, 사이 20) ↔ 판을 정체 아래로(안 너비 < 730)."""
        stacked = width < self.px(730)
        if stacked == w.get("stacked"):
            return
        w["stacked"] = stacked; body, P = w["body"], self.px
        if stacked:
            body.columnconfigure(0, weight=1, uniform=""); body.columnconfigure(1, weight=0, uniform="")
            w["ident"].grid(row=0, column=0, sticky="nsew"); w["panel"].grid(row=1, column=0, sticky="nsew", padx=0, pady=(P(16), 0))
        else:
            body.columnconfigure(0, weight=100, uniform="book"); body.columnconfigure(1, weight=115, uniform="book")
            w["ident"].grid(row=0, column=0, sticky="nsew"); w["panel"].grid(row=0, column=1, sticky="nsew", padx=(P(20), 0), pady=0)

    def _fit_pages_hint(self, w: dict, width: int) -> None:
        """쪽 범위 줄의 힌트: 칸·[전체] 옆에 들어가지 않으면 아래 줄로."""
        e, allb = w["pages_parts"]; hint = w["pages_hint"]; P = self.px
        need = e.winfo_reqwidth() + allb.winfo_reqwidth() + hint.winfo_reqwidth() + P(14)
        below = width < need
        if below == w.get("hint_below"):
            return
        w["hint_below"] = below
        if below:
            hint.grid(row=1, column=0, columnspan=3, sticky="w", padx=0, pady=(P(2), 0))
        else:
            hint.grid(row=0, column=2, sticky="w", padx=(P(8), 0), pady=0)

    def _fit_path(self, w: dict) -> None:
        """원본 경로 줄: 차지한 너비에 맞춰 가운데를 '…' 로(전체 경로는 풍선 도움말·'경로 복사')."""
        lab = w.get("path")
        if lab is None:
            return
        pdf = self._input_vars[w["tab"]]["pdf"].get().strip()
        try:
            from tkinter import font as tkfont
            f = vars(self).get("_cap_font") or tkfont.Font(self, font=self.fonts["CAP"])
            self._cap_font = f
            text = elide_middle(pdf, f, max(self.px(20), lab.winfo_width() - 4))
        except tk.TclError:
            text = pdf
        if lab.cget("text") != text:
            lab.configure(text=text)
        w["path_tip"].text = pdf

    def _copy_text(self, text: str) -> None:
        if text:
            self.clipboard_clear(); self.clipboard_append(text)

    @staticmethod
    def _popup(menu, e) -> None:
        try:
            menu.tk_popup(e.x_root, e.y_root)
        finally:
            menu.grab_release()

    def _toggle_locked_book(self, tab: int) -> None:
        """실행 중 접힌 책 줄의 '이 책의 설정 · 잠김 ▸': 펼친 카드를 잠긴 모양으로 보이거나(▴) 다시 접는다. 값은 고칠 수 없다(칸·단추가 잠김)."""
        w = self._book_cards.get(tab)
        if w is not None:
            w["expanded"] = not w["expanded"]
            App._sync_book_card(self, tab)

    def _goto_print_offset(self) -> None:
        """판의 '인쇄 쪽수 · [설정] → 이 책 → 고급에서 고침': 설정 탭을 열고 '이 책' 고급을 펼쳐 인쇄 쪽수 칸으로 간다(실행 중에는 탭을 옮기지 않는다)."""
        if self.running:
            return
        self.select_tab(3)
        if not self.v_book_adv.get():
            self.v_book_adv.set(True); App._apply_book_adv(self)
        self.after(120, lambda: (self._reveal(self.e_print_offset), self.e_print_offset.focus_set()))

    def _set_seal(self, w: dict, key: str, chars: str, large: bool) -> None:
        """도장 자리(w[key] 틀)를 chars 로 다시 그린다(같으면 그대로). 글자가 없으면 비우고 옆 여백도 뺀다."""
        holder = w[key]
        if w.get(key + "_chars") == chars:
            return
        w[key + "_chars"] = chars
        for c in holder.winfo_children():
            c.destroy()
        if chars:
            self._seal(holder, chars, large=large).pack()
        else:
            holder.configure(width=1, height=1)                         # 마지막 자식을 없애도 Tk 틀은 그 크기를 그대로 쥐고 있다 — 빈 자리가 제목을 밀지 않게
        pad = holder.pack_info().get("padx") if holder.winfo_manager() == "pack" else None
        if pad is not None:
            w.setdefault(key + "_pad", pad)
            holder.pack_configure(padx=w[key + "_pad"] if chars else 0)

    @staticmethod
    def _preset_view(ref: str) -> tuple[str, str, bool, str]:
        """짜임 규칙 줄: (값, 설명 줄, 설명이 경고인가, 풍선 글). 내장·예전 이름은 PRESET_HINTS, 규칙 파일은 그 안의 이름과 파일 이름(읽지 못하면 경고)."""
        ref = (ref or "").strip() or DEFAULT_PRESET
        if ref in BUILTIN:
            return preset_label(ref), PRESET_HINTS.get(ref, ""), False, ""
        if ref in ALIASES:
            return preset_label(ref), PRESET_HINTS.get(ALIASES[ref][0], ""), False, ""
        try:
            p = resolve_preset(ref)
        except PresetError:
            return preset_label(ref).replace(" (읽지 못함)", ""), "읽지 못함 — [설정]에서 다시 고르십시오", True, ref
        return p.label or preset_label(ref), Path(ref).name, False, ref

    def _bold_view(self, flow: bool, tpl: str, ref: str) -> tuple[str, bool]:
        """획 굵기 줄: (글, 흐리게) — 설정 탭이 칸을 흐리게 하는 조건(_layout_changed)과 같다."""
        if not flow:
            return "재지 않음 — 원본 쪽대로", True
        if not tpl:
            return "재지 않음 — 양식이 없음", True
        try:
            p = resolve_preset(ref)
        except PresetError:
            return "재지 않음 — 짜임 규칙을 읽지 못함", True
        if p.entry is None:
            return "재지 않음 — 짜임 규칙에 약재명 떼기가 없음", True
        if not self.v_bold.get():
            return "굵은·보통 글씨 가르기 끔", False
        return "굵은·보통 글씨 가르기 켬" + (" (本經·別錄)" if p.id == "kaibao" else ""), False

    def _book_save_state(self) -> str:
        """이 책의 설정이 결과 폴더(book_settings.json)에 남았는지: 'none'(원본 없음) · 'pending'(결과 폴더가 아직 없음 — 첫 실행 때 저장)
        · 'unsaved'(폴더는 있지만 설정 파일이 없음) · 'changed'(파일에 남긴 뒤 칸을 고침) · 'saved'."""
        d = (vars(self).get("_book") or {}).get("dir")
        if not self.pdf() or d is None or d != self._book_dir():
            return "none"
        if not d.is_dir():
            return "pending"
        if not (d / BOOK_SETTINGS).is_file():
            return "unsaved"
        return "changed" if self._book_values() != self._book.get("loaded") else "saved"

    def _common_line(self) -> str:
        """'모든 책 공통: ChatGPT (OpenAI) gpt-4.1 · 자동 교정 3가지' (+ HWPX와 함께 만드는 것)."""
        on = [n for n, v in (("간체→번체", self.v_traditional), ("云/雲", self.v_yun), ("중문부호", self.v_punctuation)) if v.get()]
        fix = "자동 교정 3가지" if len(on) == 3 else ("자동 교정: " + " · ".join(on) if on else "자동 교정 끔")
        also = [n for n, v in (("DOCX", self.v_docx), ("검색 가능한 PDF", self.v_tlpdf)) if v.get()]
        return (f"모든 책 공통: {self.v_provider.get()} {self.v_model.get().strip()} · {fix}"
                + (" · HWPX와 함께 " + " · ".join(also) if also else ""))

    # ---- 다시 그리기 ----
    def _schedule_cards(self, *_):
        """책 카드·실행 카드·장부를 다음 한가할 때 한 번 다시 그린다(칸이 여럿 바뀌어도 한 번). 창이 다 만들어지기 전·시험의 가짜 창에서는 아무것도 하지 않는다."""
        d = vars(self)
        if not d.get("_cards_ready") or d.get("_cards_job"):
            return
        try:
            self._cards_job = self.after_idle(self._sync_cards)
        except (tk.TclError, RuntimeError):
            pass

    def _sync_cards(self) -> None:
        self._cards_job = None
        try:
            key = (self._active_input, self.pdf())
            if key != vars(self).get("_cards_key"):                # 다른 원본·탭: 끝난 실행 표시를 거두고 그 책의 장부(last_cost.json)를 읽는다
                self._cards_key = key
                if not self.running:
                    self._run_result = None; self._ledger_live = False
                App._load_ledger(self)
            App._sync_book_card(self, self._active_input)
            App._sync_run_card(self)
            App._sync_ledger(self)
            App._sync_progress(self)
            App._apply_source_lock(self)
            App._sync_settings(self)
            App._request_counts(self)
        except tk.TclError:
            pass

    def _sync_book_card(self, tab: int) -> None:
        d = vars(self); w = (d.get("_book_cards") or {}).get(tab)
        if w is None:
            return
        F = self.fonts
        images = tab == 2
        pdf = self._input_vars[tab]["pdf"].get().strip()
        title, chars, fname = book_title(pdf)
        locked = self.running and d.get("_running_cmd") not in (None, "", "update")
        if not locked:
            w["expanded"] = False
        collapsed = locked and not w["expanded"]
        if collapsed != w.get("collapsed"):
            w["collapsed"] = collapsed
            if collapsed:
                w["card"].pack_forget(); w["row"].pack(fill="x")
            else:
                w["row"].pack_forget(); w["card"].pack(fill="x")
            if d.get("_host") is not None:
                self.after_idle(self._fit_host)
        App._set_seal(self, w, "seal", chars, large=not images)
        App._set_seal(self, w, "row_seal", chars, large=False)
        if pdf:
            w["title"].configure(text=title, font=F["NUM" if images else "TITLE"]); w["fname"].configure(text=fname)
        else:
            w["title"].configure(text="원본을 고르십시오", font=F["TITLE"]); w["fname"].configure(text="" if images else "PDF 한 권 또는 이미지 파일·폴더")
        w["title_tip"].text = pdf
        if images:
            if pdf:
                w["sub"].grid_remove()
            else:
                w["sub"].grid(row=1, column=0, sticky="ew")
            (w["fname"].pack_forget if not pdf else lambda: w["fname"].pack(side="left", fill="x", expand=True, padx=(self.px(10), 0), pady=(self.px(4), 0)))()
        w["row_title"].configure(text=title or "원본 없음"); w["row_fname"].configure(text=fname)
        counts = d.get("_counts") if tab == self._active_input else None
        vals = [counts.get(k) for k in ("target", "records", "to_read")] if counts and pdf else [None] * 3
        for line in [w["row_counts"]] + ([w["counts_line"]] if images else []):
            for k, v in zip(("target", "records", "to_read"), vals):
                line[k].configure(text="—" if v is None else f"{v:,}"); line[k + "_u"].configure(text="" if v is None else "쪽")
        for (num, unit), v in zip(w.get("counts", ()), vals):
            num.configure(text="—" if v is None else f"{v:,}"); unit.configure(text="" if v is None else "쪽")
        if not images:
            if pdf:
                w["path_row"].grid(); App._fit_path(self, w)
            else:
                w["path_row"].grid_remove()
            w["pick"].configure(style="TButton" if pdf else "Ink.TButton")
        w["out_label"].configure(text="결과 저장 (읽기 전용 · HWPX·문자 PDF가 저장되는 out 폴더)" if pdf else "결과 저장 (읽기 전용 · 원본을 고르면 정해짐)")
        # 이 책의 설정 판
        if locked:
            cap = "실행 중에는 잠김 · 끝나면 고칠 수 있음"
            if w["fold"].winfo_manager() != "pack":
                w["fix"].pack_forget(); w["fold"].pack(side="right")
        else:
            cap = "바꾸면 다음 실행에 적용" if pdf else "원본을 고르면 그 책의 값으로 바뀜"
            if w["fix"].winfo_manager() != "pack":
                w["fold"].pack_forget(); w["fix"].pack(side="right")
        w["panel_cap"].configure(text=cap)
        App._sync_panel(self, tab, w)

    def _sync_panel(self, tab: int, w: dict) -> None:
        """'이 책의 설정' 판의 값(책별 설정 일곱 가지 — 인쇄 쪽수는 값이 있을 때만)·모든 책 공통 줄·저장 상태."""
        C, F = self.colors, self.fonts
        flow = self.layout_key() == "flow"
        w["v_mode"].configure(text=self._mode_vars[2 if tab == 2 else 0].get() + (" (이미지 몫)" if tab == 2 else ""))
        w["v_layout"].configure(text=self.v_layout.get())
        tpl = self.v_template.get().strip()
        if tpl:
            w["v_template"].configure(text=Path(tpl).name, font=F["BODY_B"], fg=C["INK"] if flow else C["MUTED"])
        else:
            w["v_template"].configure(text="없음 — 기본 모양", font=F["BODY"], fg=C["MUTED"])
        w["tip_template"].text = tpl
        ref = App.preset_ref(self)
        val, hint, bad, tip = App._preset_view(ref)
        w["v_preset"].configure(text=val, fg=C["INK"] if flow else C["MUTED"])
        w["v_preset_hint"].configure(text=hint if flow else "원본 쪽대로라 쓰지 않음", fg=C["WARN"] if bad and flow else C["MUTED"])
        w["tip_preset"].text = tip
        text, muted = App._bold_view(self, flow, tpl, ref)
        w["v_bold"].configure(text=text, fg=C["MUTED"] if muted else C["INK"])
        off = self._input_vars[tab]["print_offset"].get().strip()
        if off:
            sign = "−" if off.startswith("-") else "+"
            w["off_val"].configure(text=f"{sign} {off.lstrip('+-')}" if off.lstrip("+-").isdigit() else off)
            w["off_name"].grid(row=1, column=0, sticky="w", padx=(0, self.px(10)), pady=self.px(2)); w["off_row"].grid(row=1, column=1, sticky="w", pady=self.px(2))
        else:
            w["off_name"].grid_remove(); w["off_row"].grid_remove()
        w["common"].configure(text=App._common_line(self))
        state = App._book_save_state(self) if tab == self._active_input else "none"
        d = self._book.get("dir")
        where = f"— {d.name}{os.sep}{BOOK_SETTINGS}" if d is not None and state in ("saved", "changed", "unsaved") else ""
        look = SAVE_LOOKS[state]
        w["save_icon"].configure(image=self.icons["check"] if look[3] else "")
        w["save_icon"].grid_configure(padx=(0, self.px(5)) if look[3] else 0)
        w["save_state"].configure(text=look[0], fg=C[look[1]], font=F[look[2]])
        w["save_path"].configure(text=where)
        w["save_fit"] = None
        App._fit_save_row(self, w, w["save_row"].winfo_width())

    def _fit_save_row(self, w: dict, width: int) -> None:
        """저장 상태 줄: '✓ 결과 폴더에 저장됨 — …\\book_settings.json' — 경로가 같은 줄에 들어가지 않으면 다음 줄로."""
        path = w["save_path"]; P = self.px
        text = path.cget("text")
        try:
            from tkinter import font as tkfont
            need = (w["save_icon"].winfo_reqwidth() + w["save_state"].winfo_reqwidth() + P(5)
                    + tkfont.Font(self, font=self.fonts["CAP"]).measure(text) + P(4))
        except tk.TclError:
            need = 0
        fit = ("none" if not text else "below" if width > 1 and need > width else "beside")
        if fit == w.get("save_fit"):
            return
        w["save_fit"] = fit
        if fit == "none":
            path.grid_remove()
        elif fit == "below":
            path.grid(row=1, column=0, columnspan=3, sticky="ew", padx=0)
        else:
            path.grid(row=0, column=2, sticky="ew", padx=(P(5), 0))

    def _scroll_out_ends(self) -> None:
        """결과 저장 칸은 전체 경로를 두고 끝('…\\개보본초.ocr_YeonE_OCR\\out')이 보이게 굴린다."""
        for w in (vars(self).get("_book_cards") or {}).values():
            try:
                w["out"].xview_moveto(1.0)
            except (KeyError, tk.TclError):
                pass

    # ---- 수치(대상 · 판독 기록 · 새로 읽을) ----
    def _request_counts(self, refresh: bool = False) -> None:
        """원본·쪽 범위·다시 판독이 바뀌면(또는 refresh — 실행이 끝난 뒤) 작업 스레드에서 state.book_counts 로 센다. 결과는 큐 ('counts', 번호, 값)."""
        d = vars(self)
        if not d.get("_cards_ready"):
            return
        pdf = self.pdf(); key = (pdf, self.v_pages.get().strip(), bool(self.v_force.get()))
        if not refresh and key == d.get("_counts_key"):
            return
        self._counts_key = key; self._counts = None
        self._counts_seq = seq = d.get("_counts_seq", 0) + 1
        if not pdf:
            self._strip_pages, self._strip_marks = [], {}
            return
        work = self.work()

        def count():
            try:
                res = book_counts(pdf, work, key[1], key[2])
            except Exception:
                res = None                                              # 쪽 범위를 읽지 못함·원본을 열지 못함 — 수치는 '—'(실행하면 명령이 까닭을 알린다)
            self.q.put(("counts", seq, res))
            marks = None
            if res is not None:
                try:
                    pages = target_pages(pdf, key[1])
                    marks = {"pages": pages, "marks": page_marks(work, pages, key[2])}
                except Exception:
                    marks = None
            self.q.put(("pagemap", seq, marks))                           # 쪽 띠(실행 전): 기록 있음 · 실패 · 남음
        threading.Thread(target=count, daemon=True, name="book-counts").start()

    def _counts_done(self, seq: int, res: dict | None) -> None:
        if seq == vars(self).get("_counts_seq"):
            self._counts = res
            App._sync_book_card(self, self._active_input)

    def _plan_counts(self, priced: dict, sig: dict | None) -> None:
        """어림(미리 보기·실행)이 오면 그 계획의 대상·기록·새로 읽을 쪽으로 바꾼다 — 원본이 바뀌어 옮겨질 낡은 기록까지 센 값이라 더 맞다."""
        if not vars(self).get("_cards_ready") or not sig:
            return
        if (sig.get("pdf"), sig.get("pages"), bool(sig.get("force"))) != (self.pdf(), self.v_pages.get().strip(), bool(self.v_force.get())):
            return
        if priced.get("errors") and not priced.get("pages"):
            return
        self._counts = {"target": priced.get("pages", 0), "records": priced.get("records", 0), "to_read": priced.get("to_read", 0)}
        self._counts_key = (self.pdf(), self.v_pages.get().strip(), bool(self.v_force.get()))
        App._schedule_cards(self)

    # ---- 단추 잠금 ----
    def _apply_source_lock(self) -> None:
        """원본이 없으면 실행 단추 둘·[비용 미리 보기]·열기 단추 넷·[원본 보기]·[저장 폴더]·[입력 비우기]를 끈다(키보드로도 실행되지 않게).
        실행·어림 중에는 잠긴 단추(self.buttons)를 건드리지 않는다 — 열기만 하는 단추(lock=False)만 원본에 따라."""
        d = vars(self)
        if "_src_locked" not in d:
            return
        has = bool(self.pdf())
        busy = bool(d.get("running") or d.get("_planning"))
        for b in d.get("_src_open", ()):
            b.configure(state="normal" if has else "disabled")
        if not busy:
            for b in d.get("_src_locked", ()):
                b.configure(state="normal" if has else "disabled")
        for tab, w in (d.get("_book_cards") or {}).items():
            mine = bool(self._input_vars[tab]["pdf"].get().strip())
            for k in ("source", "outdir"):
                if k in w:
                    w[k].configure(state="normal" if mine else "disabled")
            if "clear" in w and not busy:
                w["clear"].configure(state="normal" if mine else "disabled")

    # ---- 실행 카드 · 장부(오른쪽 단) ----
    def _build_run_column(self, parent) -> ttk.Frame:
        """오른쪽 단(340): 실행 카드 — '실행 · 둘 다 판독부터 · 호출 듦' · 판독기·모델 한 줄 · [▶ 한 번에 인식 (HWPX 만들기)](인주 채움 48) ·
        [▶ 한 번에 인식 (문자 PDF 만들기)](인주 테두리 40) · [■ 중지](둘이 함께, 온 너비) · 상태 줄 · 캡션 · 열기 2×2([HWPX 열기][문자 PDF 열기] /
        [검수 화면][결과 폴더]) · 실행 중 사본 안내. 그 아래 장부 — 마지막 비용 어림(costplan)과 이번 실행의 호출."""
        C, P = self.colors, self.px
        L = self._lab
        run = ttk.Frame(parent, style="Card.TFrame", padding=(P(18), P(16)))         # select_tab 이 실행 자리(_run_slot)에 넣고 뺀다
        b = tk.Frame(run, bg=C["PANEL"]); b.pack(fill="both", expand=True); b.columnconfigure(0, weight=1)
        head = tk.Frame(b, bg=C["PANEL"]); head.grid(row=0, column=0, sticky="ew")
        L(head, "실행", "SECTION").pack(side="left")
        L(head, "둘 다 판독부터 · 호출 듦", "CAP", "MUTED").pack(side="left", padx=(P(8), 0), pady=(P(2), 0))
        self._btn(head, "설정 바꾸기", lambda: self.select_tab(3), style="Link.TButton",
                  tip="판독기·모델·자동 교정·문서 옵션(모든 책 공통)과 이 책의 설정을 설정 탭에서 고칩니다.").pack(side="right")
        self.l_run_summary = L(b, "", "BODY_B", wrap=True); self.l_run_summary.grid(row=1, column=0, sticky="ew", pady=(P(6), 0))
        self.b_run = self._btn(b, RUN_TEXT, self.do_run, style="Seal.TButton", tip=RUN_TIP)
        self.b_run.grid(row=2, column=0, sticky="ew", pady=(P(8), 0))
        self.b_run_pdf = self._btn(b, RUN_PDF_TEXT, self.do_run_pdf, style="SealOut.TButton", tip=RUN_PDF_TIP)
        self.b_run_pdf.grid(row=3, column=0, sticky="ew", pady=(P(6), 0))
        self.b_stop = ttk.Button(b, text="■  중지", command=self.do_stop, state="disabled", style="Stop.TButton", cursor="hand2")
        self.b_stop.grid(row=4, column=0, sticky="ew", pady=(P(6), 0))
        Tip(self.b_stop, STOP_TIP, self)
        for btn in (self.b_run, self.b_run_pdf, self.b_stop):
            btn.configure(width=1)                                          # 글 길이로 오른쪽 단(340)을 넓히지 않는다 — 단 너비를 다 쓰고, 긴 글(실행 중)은 끝을 줄임
        self.t_run_status = App._rich_line(self, b)                        # 중지를 누른 뒤·끝난 뒤(·입력을 비운 뒤의 안내)만 — v_status
        self.l_run_cap = L(b, "", "CAP", "MUTED", wrap=True); self.l_run_cap.grid(row=6, column=0, sticky="ew", pady=(P(8), 0))
        opens = tk.Frame(b, bg=C["PANEL"]); opens.grid(row=7, column=0, sticky="ew", pady=(P(8), 0))
        opens.columnconfigure(0, weight=1, uniform="open"); opens.columnconfigure(1, weight=1, uniform="open")
        self.b_open_hwpx = self._btn(opens, "HWPX 열기", self.open_hwpx, lock=False, tip="이 원본의 가장 최근 완성 HWPX를 엽니다(중간 저장본은 제외). " + COPY_TIP)
        self.b_open_pdf = self._btn(opens, "문자 PDF 열기", self.open_textlayer, lock=False, tip="이 원본에서 마지막으로 만든 찾기·복사가 되는 PDF를 엽니다. " + COPY_TIP)
        self.b_open_review = self._btn(opens, "검수 화면", self.open_review, lock=False, tip="원본 위에 영역 상자를 그린 검수 화면(review.html)을 엽니다(열기만 함).")
        self.b_open_work = self._btn(opens, "결과 폴더", self.open_work, lock=False, tip="결과 폴더(쪽 이미지·판독 기록·문서·검수 화면·교정 기록)를 엽니다.")
        for k, btn in enumerate((self.b_open_hwpx, self.b_open_pdf, self.b_open_review, self.b_open_work)):
            btn.configure(padding=(P(6), P(4)))
            btn.grid(row=k // 2, column=k % 2, sticky="ew", padx=(0, P(3)) if k % 2 == 0 else (P(3), 0), pady=(P(6), 0) if k >= 2 else 0)
        self.l_copy_note = L(b, COPY_NOTE, "CAP", "MUTED", wrap=True)      # 실행 중에만
        # ---- 장부 ----
        led = self._led_card = ttk.Frame(self._work, style="Card.TFrame", padding=(P(18), P(16)))    # _place_cards 가 오른쪽 단(한 단이면 진행 아래)에
        lb = self._led_body = tk.Frame(led, bg=C["PANEL"]); lb.pack(fill="both", expand=True); lb.columnconfigure(0, weight=1)
        head = self._led_head = tk.Frame(lb, bg=C["PANEL"]); head.grid(row=0, column=0, sticky="ew")
        self._led_title = L(head, "장부", "SECTION"); self._led_title.pack(side="left")
        self.l_led_cap = L(head, "", "CAP", "MUTED", wrap=True)          # 긴 캡션('이 명령은 따로 셈 — …')은 줄을 바꿔 오른쪽 단(340)을 넓히지 않게
        self.l_led_cap.pack(side="left", fill="x", expand=True, padx=(P(8), P(6)), pady=(P(2), 0))
        self._led_cap_below = False
        head.bind("<Configure>", lambda _e: App._place_led_cap(self), add="+")
        self.b_estimate = self._btn(head, "비용 미리 보기", self.do_estimate, style="Link.TButton",
                                    tip="[▶ 한 번에 인식 (HWPX 만들기)]의 판독·3차 판별·합의 의심·문맥 교정 호출·토큰·금액을 한 창에 어림합니다"
                                        "(기록 있는 쪽은 정확히, 새로 읽을 쪽은 이 책 평균으로). 아무것도 쓰지 않고 결과 폴더도 만들지 않습니다.")
        self.b_estimate.pack(side="right")
        ch = self._led_choice = tk.Frame(lb, bg=C["PANEL"])               # '이번 실행: **예 · 모두 실행** · HWPX'(굵은 데가 있는 짧은 줄)
        self._led_choice_parts = [L(ch, "", "CAP", "MUTED"), L(ch, "", "BODY_B"), L(ch, "", "CAP", "MUTED")]
        for part in self._led_choice_parts:
            part.pack(side="left", anchor="s")
        self.l_led_note = L(lb, "", "CAP", "MUTED", wrap=True)              # 그 밖의 안내 한 줄(좁으면 줄바꿈)
        self.l_led_amount = L(lb, "—", "NUM"); self.l_led_amount.grid(row=2, column=0, sticky="w", pady=(P(6), 0))
        self.l_led_usd = L(lb, "", "CAP", "MUTED", wrap=True); self.l_led_usd.grid(row=3, column=0, sticky="ew")
        bar = self._led_bar = tk.Frame(lb, bg=C["PANEL"])                  # 호출 막대(어림이 있을 때)
        bt = tk.Frame(bar, bg=C["PANEL"]); bt.pack(fill="x")
        self._led_used = (L(bt, "이번 실행 호출 ", "CAP", "MUTED"), L(bt, "—", "CAP_B"))
        for part in self._led_used:
            part.pack(side="left")
        self._led_range = (L(bt, "", "CAP_B"), L(bt, "어림 ", "CAP", "MUTED"))
        for part in self._led_range:
            part.pack(side="right")
        self.c_led_bar = tk.Canvas(bar, height=P(8), bg=C["PANEL"], highlightthickness=0, bd=0, width=P(40))
        self.c_led_bar.pack(fill="x", pady=(P(4), 0))
        self.c_led_ticks = tk.Canvas(bar, height=P(16), bg=C["PANEL"], highlightthickness=0, bd=0, width=P(40))
        self.c_led_ticks.pack(fill="x")
        for cv in (self.c_led_bar, self.c_led_ticks):
            cv.bind("<Configure>", lambda _e: App._draw_calls_bar(self), add="+")
        self._led_bar_vals = None
        self._led_groups = tk.Frame(lb, bg=C["PANEL"]); self._led_groups.grid(row=5, column=0, sticky="ew", pady=(P(8), 0))
        self._led_groups.columnconfigure(0, weight=1)
        self._led_view = None
        tot = tk.Frame(lb, bg=C["PANEL"]); tot.grid(row=6, column=0, sticky="ew", pady=(P(8), 0))
        hl = max(1, P(1))
        tk.Frame(tot, bg=C["INK"], height=hl).pack(fill="x"); tk.Frame(tot, bg=C["PANEL"], height=P(2)).pack(fill="x"); tk.Frame(tot, bg=C["INK"], height=hl).pack(fill="x")
        tr = tk.Frame(tot, bg=C["PANEL"]); tr.pack(fill="x", pady=(P(4), 0))
        self.l_led_total_name = L(tr, "합계", "BODY_B"); self.l_led_total_name.pack(side="left")
        self.l_led_total = L(tr, "—", "BODY_B"); self.l_led_total.pack(side="right")
        # 원본이 있어야 눌리는 단추: 실행 둘·[비용 미리 보기](실행 중에도 잠김), 열기 넷(열기만 함 — 실행 중에도 눌림)
        self._src_locked = [self.b_run, self.b_run_pdf, self.b_estimate]
        self._src_open = [self.b_open_hwpx, self.b_open_pdf, self.b_open_review, self.b_open_work]
        return run

    def _sync_run_card(self) -> None:
        """실행 카드의 글: 판독기·모델 한 줄, 상태 줄(중지를 누른 뒤·끝난 뒤·원본이 없을 때만), 캡션(실행 전·실행 중·원본 없음·끝난 뒤),
        끝난 뒤 그 실행의 결과 단추 강조(HWPX → [HWPX 열기], 문자 PDF → [문자 PDF 열기], rc 5 → [결과 폴더]), 실행 중 사본 안내."""
        d = vars(self)
        if "l_run_summary" not in d:
            return
        P = self.px
        running = bool(self.running); has = bool(self.pdf()); res = d.get("_run_result")
        self.l_run_summary.configure(text=f"{self.v_provider.get()} · {self.v_model.get().strip() or '(모델 없음)'}")
        parts = []
        if running and d.get("_stop_requested"):
            parts = [(self.v_status.get(), "plain")]
        elif not running and res and self.v_status.get().startswith(res.get("label", "")):
            tail = self.v_status.get()[len(res.get("label", "")):]
            look = {"error": "red", "partial": "red" if res.get("rc") == 3 and res.get("failed") else "warn"}.get(res.get("kind"), "plain")
            parts = [(res.get("label", ""), "bold"), (tail, look)]
        elif not running and not has and self.v_status.get() == CLEARED_STATUS:
            parts = [(CLEARED_STATUS, "plain")]
        if parts:
            App._set_rich(self, self.t_run_status, parts); self.t_run_status.grid(row=5, column=0, sticky="ew", pady=(P(8), 0))
        else:
            self.t_run_status.grid_remove()
        if running:
            cap = RUN_CAP
        elif not has:
            cap = EMPTY_CAP
        elif res:
            cap = finish_caption(res.get("rc"), res.get("cmd", ""), bool(res.get("failed")), tuple(res.get("failed_pages") or ()))
        else:
            cap = READY_CAP
        self.l_run_cap.configure(text=cap)
        target = None
        if not running and res and res.get("kind") in ("ok", "partial"):
            if res.get("cmd") == "run":
                target = self.b_open_work if res.get("rc") == 5 else self.b_open_hwpx
            elif res.get("cmd") == "run-pdf":
                target = self.b_open_pdf
        for btn in (self.b_open_hwpx, self.b_open_pdf, self.b_open_review, self.b_open_work):
            want = "Emph.TButton" if btn is target else "TButton"
            if str(btn.cget("style")) != want:
                btn.configure(style=want)
        if running:
            self.l_copy_note.grid(row=8, column=0, sticky="ew", pady=(P(6), 0))
        else:
            self.l_copy_note.grid_remove()

    def _rich_line(self, parent, font: str = "BODY", fg: str = "INK") -> tk.Text:
        """굵은 글·빨강·주의 글을 한 문단에 섞어 차지한 너비대로 줄을 바꾸는 읽기 전용 글줄(높이는 줄 수에 맞춤). 설정 줄 설명은 font='CAP'·fg='MUTED'
        (확인 등급 색 g·b·r — 그 색 굵은 글)."""
        C, F = self.colors, self.fonts
        bold = F["CAP_B" if font == "CAP" else "BODY_B"]
        t = tk.Text(parent, height=1, width=1, wrap="word", bd=0, highlightthickness=0, bg=C["PANEL"], fg=C[fg], font=F[font],
                    padx=0, pady=0, cursor="arrow", takefocus=0, relief="flat", spacing1=0, spacing2=self.px(2))
        t.tag_configure("bold", font=bold); t.tag_configure("red", foreground=C["RED"]); t.tag_configure("warn", foreground=C["WARN"])
        t.tag_configure("plain", foreground=C["INK"])
        for tag, color in (("g", "GREEN"), ("b", "BLUE"), ("r", "RED")):
            t.tag_configure(tag, foreground=C[color], font=bold)
        t.configure(state="disabled")
        t.bind("<Configure>", lambda _e: App._fit_rich(self, t), add="+")
        return t

    def _set_rich(self, t: tk.Text, parts: list) -> None:
        t.configure(state="normal"); t.delete("1.0", "end")
        for text, tag in parts:
            t.insert("end", text, tag)
        t.configure(state="disabled")
        App._fit_rich(self, t)

    @staticmethod
    def _fit_rich(_self, t: tk.Text) -> None:
        try:
            n = t.count("1.0", "end", "displaylines")
            n = max(1, int(n[0] if isinstance(n, tuple) else n or 1))
        except (tk.TclError, TypeError, ValueError):
            n = 1
        if int(t.cget("height")) != n:
            t.configure(height=n)

    def _sync_run_slot(self) -> None:
        """도는 실행의 자리만 잠긴 모양(SEAL_SOFT · 자물쇠 · '… · {단계} 중'), 다른 실행은 꺼짐 — 단추 자리는 바뀌지 않는다(디자인 명세 §5.2·§7.1·§7.2).
        한 번에 인식이 아닌 명령(start)은 주 실행 자리에 '{명령} 중'. 키 확인·이미지 묶기처럼 명령이 없는 잠금은 둘 다 꺼진 모양으로 둔다."""
        d = vars(self); b1, b2 = d.get("b_run"), d.get("b_run_pdf")
        if b1 is None or b2 is None:
            return
        if not self.running:
            b1.configure(style="Seal.TButton", text=RUN_TEXT, image="")
            b2.configure(style="SealOut.TButton", text=RUN_PDF_TEXT, image="")
            return
        cmd = d.get("_running_cmd") or ""
        stage = getattr(d.get("tracker"), "stage", "") or ""
        doing = (STAGE_NAMES.get(stage, stage) if stage else "시작") + " 중"
        # 잠긴 단추(disabled)에 그림만 주면 ttk 가 그 위에 바탕색 점무늬(stipple)를 덮는다(Tk 는 macOS 에서만 건너뜀) — 잠긴 상태의 그림을 따로 준다.
        lock = (self.icons["lock_seal"], "disabled", self.icons["lock_seal"])
        def fit(b, texts: tuple[str, ...], font: str) -> str:
            """단추 너비(오른쪽 단 340 — 글이 단을 넓히지 않게 width=1)에 들어가는 첫 글. 다 넘치면 마지막 글의 가운데를 줄인다."""
            try:
                width = b.winfo_width()
                if width <= 1:
                    return texts[0]
                room = width - 2 * self.px(18) - self.px(13) - self.px(12)     # 좌우 안쪽 · 자물쇠 · 사이
                f = self._font(font)
                return next((t for t in texts if f.measure("  " + t) <= room), None) or elide_middle(texts[-1], f, max(self.px(60), room))
            except tk.TclError:
                return texts[0]
        if cmd == "run":
            b1.configure(style="SealBusy.TButton", image=lock, compound="left",
                         text="  " + fit(b1, (f"한 번에 인식 (HWPX) · {doing}", f"HWPX 만들기 · {doing}"), "RUN"))
        elif cmd == "run-pdf":
            b2.configure(style="SealOutBusy.TButton", image=lock, compound="left",
                         text="  " + fit(b2, (f"한 번에 인식 (문자 PDF) · {doing}", f"문자 PDF 만들기 · {doing}", f"문자 PDF · {doing}"), "BTN_L_B"))
        elif cmd and cmd != "update":
            label = d.get("_run_label") or {"install": "패키지 설치·점검"}.get(cmd, cmd)
            b1.configure(style="SealBusy.TButton", image=lock, compound="left", text="  " + fit(b1, (f"{label} 중",), "RUN"))

    # ---- 장부 ----
    @staticmethod
    def _sig_of(inp) -> dict:
        """비용 어림 입력(PlanInput)의 지문 — 설정이 바뀌었는지(어림 다시 필요) 견줄 때. JSON 으로 한 번 오간 꼴(파일에서 읽은 것과 같게)."""
        from dataclasses import asdict
        return json.loads(json.dumps(asdict(inp), ensure_ascii=False))

    @staticmethod
    def _pdf_input(inp):
        """[▶ 한 번에 인식 (문자 PDF 만들기)]의 어림 입력: 문서를 만들지 않으므로 문맥 교정·자동 교정·짜임은 쓰지 않는 셈(계획은 run 그대로)."""
        from dataclasses import replace
        return replace(inp, context_model=False, normalize=False, layout="page", preset=DEFAULT_PRESET, punctuation_rules="")

    def _ledger_sig(self, action: str) -> dict:
        inp = self._plan_input("run")
        return App._sig_of(App._pdf_input(inp) if action == "pdf" else inp)

    @staticmethod
    def _src_stamp(pdf: str | None):
        """원본 파일의 (크기, 고친 시각) — 같은 이름으로 원본을 바꿔 넣으면 장부의 어림이 낡은 것으로 보이게."""
        try:
            st = Path(pdf).stat() if pdf else None
            return [st.st_size, st.st_mtime_ns] if st else None
        except OSError:
            return None

    @staticmethod
    def _ledger_summary(priced: dict, action: str, choice: str | None, sig: dict | None, src) -> dict:
        """장부에 남길 어림 요약(last_cost.json): 줄마다 단계·대상·호출·달러, 합계·원화·모델·환율, 지문(sig)·선택(choice)·실행(action: hwpx|pdf)."""
        t = priced.get("totals") or {}
        span = lambda v: list(v) if v is not None else None
        return {"v": 1, "kind": "run", "action": action, "choice": choice, "saved": False, "sig": sig, "src": src,
                "time": time.strftime("%Y-%m-%d %H:%M"), "model": priced.get("model", ""), "models": list(priced.get("models") or []),
                "context_model": bool(priced.get("context_model", True)),
                "pages": priced.get("pages", 0), "records": priced.get("records", 0), "to_read": priced.get("to_read", 0),
                "rows": [{"step": r.get("step"), "scope": r.get("scope"), "calls": list(r.get("calls") or [0, 0]), "usd": span(r.get("usd"))}
                         for r in priced.get("rows") or []],
                "totals": {"calls": list(t.get("calls") or [0, 0]), "usd": span(t.get("usd")), "usd_known": list(t.get("usd_known") or [0.0, 0.0]),
                           "missing_prices": list(t.get("missing_prices") or [])},
                "krw": span(priced.get("krw")), "krw_known": span(priced.get("krw_known")), "fx": priced.get("fx"),
                "errors": [e.get("step") for e in priced.get("errors") or []]}

    def _remember_plan(self, kind: str, pdf_only: bool, priced: dict, choice: str | None, preview: bool) -> None:
        """한 번에 인식의 어림(미리 보기·실행)만 장부에 — 판독·문서 만들기·3차 판별 단독 어림은 일부 줄뿐이라 넣지 않는다.
        미리 보기는 메모리에만(저장 안 함), 실행은 go() 안에서 _save_ledger 가 결과 폴더(last_cost.json)에 쓴다."""
        if kind != "run":
            return
        d = vars(self)
        sig = d.get("_plan_sig")
        led = App._ledger_summary(priced, "pdf" if pdf_only else "hwpx", choice, sig, App._src_stamp((sig or {}).get("pdf")))
        if preview:
            self._ledger = led; self._ledger_live = False
            App._plan_counts(self, priced, sig)
            App._schedule_cards(self)
        else:
            self._ledger_pending = led
            App._plan_counts(self, priced, sig)

    def _save_ledger(self) -> None:
        """실행 직전(go 안 — 비용 창에서 고른 뒤, 책별 설정을 결과 폴더에 남긴 다음): 이번 어림을 결과 폴더의 last_cost.json 에 남기고 장부로 삼는다."""
        d = vars(self)
        led = d.pop("_ledger_pending", None)
        if not led:
            return
        try:
            path = self.work() / LAST_COST
            path.parent.mkdir(parents=True, exist_ok=True)
            write_atomic(path, json.dumps(dict(led, saved=True), ensure_ascii=False, indent=1))
            led["saved"] = True
        except Exception as e:
            self._append(f"주의: 비용 어림을 결과 폴더에 남기지 못했습니다({LAST_COST}): {type(e).__name__}: {e}\n")
        self._ledger = led
        self._ledger_for = "run-pdf" if led.get("action") == "pdf" else "run"
        App._schedule_cards(self)

    def _load_ledger(self) -> None:
        """책을 고르면 그 결과 폴더의 last_cost.json(마지막 실행 전 어림)을 읽어 장부를 채운다. 없거나 깨졌으면 '어림 전'."""
        self._ledger = None; self._ledger_live = False
        pdf = self.pdf()
        if not pdf:
            return
        try:
            data = json.loads((self.work() / LAST_COST).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if isinstance(data, dict) and data.get("kind") == "run" and isinstance(data.get("rows"), list) and isinstance(data.get("totals"), dict):
            data["saved"] = True
            self._ledger = data

    def _ledger_stale(self, led: dict) -> bool:
        """지금 칸으로 다시 어림하면 다른 입력인가(쪽 범위·모델·짜임·자동 교정·다시 판독 …) 또는 원본 파일이 바뀌었나."""
        try:
            return App._ledger_sig(self, led.get("action", "hwpx")) != led.get("sig") or App._src_stamp(self.pdf()) != led.get("src")
        except Exception:
            return True

    @staticmethod
    def _call_count() -> int | None:
        """이번 명령의 모델 호출 수(재시도 포함) — 판독기를 불러온 뒤에만(창이 판독기 모듈을 일부러 불러오지 않는다)."""
        mod = sys.modules.get(f"{__package__}.reader_gemini") if __package__ else None
        cap = getattr(mod, "CALL_CAP", None)
        return int(getattr(cap, "used", 0)) if cap is not None else None

    def _count_calls(self, snap: int | None = None) -> bool:
        """이번 실행의 모델 호출(재시도 포함, CALL_CAP.used)을 단계 몫으로 나눈다(명세 §9 _calls_by_stage — 장부의 '호출 n회 / 예상 m회').
        snap 은 진행 알림이 가져온 그때의 수(명령 스레드에서 알림과 함께 읽음): 그 알림 앞의 호출은 모두 앞 단계 몫으로 확정한다 — 알림이
        큐에 쌓여 늦게 와도 앞 단계의 호출이 다음 단계로 새지 않는다. None 이면 지금 수를 읽어 다음 알림까지 지금 단계 몫으로 잠시 보인다(100ms 마다).
        명령(cli.main)이 CALL_CAP 을 0 으로 돌린 뒤의 수만 믿도록 첫 알림 전에는 읽은 수를 쓰지 않는다(끝에서 snap 으로 한 번에 셈). 바뀌었으면 True."""
        d = vars(self)
        if not d.get("running") or not d.get("_calls_live"):
            return False
        stage = getattr(d.get("tracker"), "stage", "") or ""
        before = d.get("_calls_used")
        by = d.setdefault("_calls_by_stage", {})
        if snap is not None:
            base = d.get("_calls_base") or 0
            if snap > base:
                by[stage] = by.get(stage, 0) + snap - base
            self._calls_base = snap; tail = 0
        else:
            base, used = d.get("_calls_base"), App._call_count()
            if base is None or used is None:
                return False
            tail = max(0, used - base)
        self._calls_tail = (stage, tail)
        self._calls_used = sum(by.values()) + tail
        return self._calls_used != before

    def _sync_ledger(self) -> None:
        """장부: 머리 캡션(마지막 어림 · HWPX 만들기/문자 PDF · 미리 보기 어림 · 어림 다시 필요 · 어림 전 · 실행 중 '금액은 실행 전 어림 · 호출은 실제'),
        이번 실행 줄, 원화·달러, 호출 막대(어림 범위 BAND · 실제 INK), 묶음(판독 · 모델 단계 [아니요]면 뺌)과 줄, 합계.
        [아니요 · 모델 단계 없이]로 시작한 실행은 금액·막대의 어림도 판독 줄만(합계(이번 실행)와 같은 셈 — gui_progress.run_totals)."""
        d = vars(self)
        if "l_led_cap" not in d:
            return
        from .estimate import fmt_krw
        from .gui_progress import run_totals
        C, F = self.colors, self.fonts
        led = d.get("_ledger"); has = bool(self.pdf())
        running = bool(self.running); cmd = (d.get("_running_cmd") or "") if running else ""
        res = d.get("_run_result")
        live = bool(d.get("_ledger_live")) and led is not None
        other = running and cmd not in ("", "run", "run-pdf", "update")
        stale = led is not None and not live and not running and App._ledger_stale(self, led)
        self._led_stale = stale                                          # 진행 카드의 단계 띠 비용 줄도 같은 판정('어림 다시 필요')
        pdf_act = bool(led) and led.get("action") == "pdf"
        without = live and led.get("choice") == "without"
        tot = run_totals(led, without)
        # 머리 캡션
        cap_fg = C["MUTED"]
        if other:
            cap = "이 명령은 따로 셈 — 아래 금액은 마지막 한 번에 인식 어림"
        elif live:
            cap = "금액은 실행 전 어림 · 호출은 실제"
        elif led is None:
            cap = "어림 전"
        elif stale:
            cap, cap_fg = "어림 다시 필요 — 설정이 바뀜", C["WARN"]
        elif led.get("saved"):
            cap = "마지막 어림 · " + ("문자 PDF" if pdf_act else "HWPX 만들기")
        else:
            cap = "미리 보기 어림 · 저장 안 함"
        self.l_led_cap.configure(text=cap, fg=cap_fg)
        App._place_led_cap(self)
        # 이번 실행 줄
        if live:
            tail = ""
            if not running and res:
                tail = {"ok": " · 끝", "partial": " · 끝", "stopped": " · 중지됨"}.get(res.get("kind"), " · 오류로 멈춤")
            parts = ("이번 실행: ", "아니요 · 모델 단계 없이" if without else "예 · 모두 실행", " · " + ("문자 PDF" if pdf_act else "HWPX") + tail)
        elif led is None:
            parts = ("[비용 미리 보기]로 어림할 수 있습니다 — 실행 단추를 눌러도 먼저 어림해 묻습니다" if has
                     else "원본을 고르면 [비용 미리 보기]로 어림할 수 있습니다", "", "")
        else:
            parts = ("이번 실행: 누르면 비용 창에서 [예]·[아니요]를 고릅니다", "", "")
        if parts[1]:
            for lab, text in zip(self._led_choice_parts, parts):
                lab.configure(text=text)
            self.l_led_note.grid_remove(); self._led_choice.grid(row=1, column=0, sticky="ew", pady=(self.px(6), 0))
        else:
            self.l_led_note.configure(text=parts[0])
            self._led_choice.grid_remove(); self.l_led_note.grid(row=1, column=0, sticky="ew", pady=(self.px(6), 0))
        # 금액(이번 실행의 어림)
        model_now = self.v_model.get().strip()
        amt_fg = C["MUTED"] if stale else C["INK"]
        if led is None:
            amount, usd = "—", f"$ — · {model_now}"
        else:
            models = ", ".join(led.get("models") or [led.get("model", "")])
            if tot["usd"] is not None and tot["krw"]:
                amount = f"약 {fmt_krw(tot['krw'][0])} ~ {fmt_krw(tot['krw'][1])}"
                usd = f"$ {tot['usd'][0]:.2f} ~ {tot['usd'][1]:.2f} · {models}"
            else:
                amount = "금액을 다 낼 수 없음"
                known = tot["known"]
                usd = (f"아는 부분만 $ {known[0]:.2f} ~ {known[1]:.2f} · " if known[1] else "") + f"단가 없는 모델: {', '.join(tot['missing'])}"
        self.l_led_amount.configure(text=amount, fg=amt_fg, font=F["NUM"] if led is None or amount.startswith("약") else F["BODY_B"])
        self.l_led_usd.configure(text=usd)
        # 호출 막대: 이번 실행 호출(실제, 재시도 포함) 대 어림 범위
        used = d.get("_calls_used") if (live or other) else None
        if live and not running and res:
            used = res.get("calls", used)
        if led is not None or other:
            calls = tot["calls"] if tot is not None and not other else None
            self._led_used[0].configure(text="이번 명령 호출 " if other else "이번 실행 호출 ")
            over = bool(calls and used is not None and used > calls[1])
            self._led_used[1].configure(text="—" if used is None else (f"어림보다 많음 · {used:,}회" if over else f"{used:,}회"), fg=C["WARN"] if over else C["INK"])
            if calls:
                self._led_range[0].configure(text=f"{calls[0]:,}~{calls[1]:,}회" if calls[0] != calls[1] else f"{calls[1]:,}회")
                self._led_range[1].configure(text="어림 ")
            else:
                for part in self._led_range:
                    part.configure(text="")
            self._led_bar_vals = (calls, used or 0)
            self._led_bar.grid(row=4, column=0, sticky="ew", pady=(self.px(8), 0))
            show_canvas = bool(calls)
            for cv in (self.c_led_bar, self.c_led_ticks):
                if show_canvas and cv.winfo_manager() != "pack":
                    cv.pack(fill="x")
                elif not show_canvas:
                    cv.pack_forget()
            App._draw_calls_bar(self)
        else:
            self._led_bar.grid_remove(); self._led_bar_vals = None
        # 묶음과 줄 — 줄은 바뀌었을 때만 다시 만들고, 묶음 머리 오른쪽(실제 대 어림 호출)은 제자리에서 고친다
        view = App._ledger_rows(self, led, live, running, other, stale, used)
        shape = tuple(g[:3] + (g[4],) for g in view[:-1]) + (view[-1],)
        if shape != d.get("_led_view"):
            self._led_view = shape
            App._draw_ledger_rows(self, view)
        for g, heads in zip(view[:-1], d.get("_led_heads") or ()):
            App._set_group_right(self, heads, g[3], g[5] if len(g) > 5 else "plain")
        total, total_name = view[-1]
        self.l_led_total.configure(text=total, fg=amt_fg if total != "—" else C["INK"])
        self.l_led_total_name.configure(text=total_name)

    def _place_led_cap(self) -> None:
        """장부 머리 캡션: '장부'와 [비용 미리 보기] 사이 한 줄에 들어가면 그 사이에, 아니면 머리 아래 제 줄에 온 너비로
        (좁은 사이에서 두 줄로 접혀 '장부' 옆에 매달리지 않게 — 실행 중 '금액은 실행 전 어림 · 호출은 실제'는 오른쪽 단 304에 빠듯함)."""
        d = vars(self); head, lab, title, btn = d.get("_led_head"), d.get("l_led_cap"), d.get("_led_title"), d.get("b_estimate")
        if head is None or lab is None:
            return
        P = self.px
        try:
            width = head.winfo_width()
            if width <= 1:
                return
            need = title.winfo_reqwidth() + P(8) + self._font("CAP").measure(lab.cget("text")) + P(6) + btn.winfo_reqwidth()
        except tk.TclError:
            return
        below = need > width
        if below == d.get("_led_cap_below"):
            return
        self._led_cap_below = below
        lab.pack_forget()
        if below:
            lab.pack(side="bottom", fill="x", before=title, pady=(P(2), 0))
        else:
            lab.pack(side="left", fill="x", expand=True, padx=(P(8), P(6)), pady=(P(2), 0), after=title)

    def _ledger_rows(self, led: dict | None, live: bool, running: bool, other: bool, stale: bool, used) -> tuple:
        """장부 몸통의 그림 값: 묶음마다 (이름, 부제, 꼬리표, 오른쪽 글, [(줄 이름, 금액 글, 꾸밈)], 오른쪽 꾸밈) + (합계 글, 합계 이름).
        오른쪽 글은 실행 전 '예상 n회', 이번 실행이면 '호출 31회 / 예상 90회'(아직 없으면 '아직 · 예상 …') — 실제는 단계별 호출(_calls_by_stage)의
        묶음 몫, 어림보다 많으면 꾸밈 'over'(WARN). [아니요]로 시작한 실행은 모델 단계 줄을 지우지 않고 흐리게 '이번엔 뺌'."""
        from .costplan import LABELS, without_flags
        from .gui_progress import calls_split, part_totals
        def money(v):
            return "단가 미입력" if v is None else (f"${v[0]:.2f}" if abs(v[0] - v[1]) < 0.005 else f"${v[0]:.2f}~{v[1]:.2f}")
        span = lambda c: f"{c[0]:,}" if c[0] == c[1] else f"{c[0]:,}~{c[1]:,}"
        d = vars(self)
        without = bool(led) and led.get("choice") == "without" and live
        got_by = calls_split(d.get("_calls_by_stage"), d.get("_calls_tail"))
        groups = []
        tot_lo = tot_hi = 0.0; tot_missing = False
        for gi, (name, sub, steps) in enumerate(LEDGER_GROUPS):
            tag = gi == 1 and bool(without_flags("run", bool((led or {}).get("context_model", True))))
            rows = []
            for step in steps:
                mine = [r for r in (led or {}).get("rows", []) if r.get("step") == step]
                if not mine:
                    continue
                c, usd = part_totals(mine, (step,))
                if without and gi == 1:
                    rows.append((LABELS.get(step, step), "이번엔 뺌", "muted"))
                    continue
                if usd is None:
                    rows.append((LABELS.get(step, step), "단가 미입력", "warn")); tot_missing = True
                elif not c[1]:
                    rows.append((LABELS.get(step, step), "$0.00", "muted"))
                else:
                    rows.append((LABELS.get(step, step), money(usd), "stale" if stale else "money"))
                    tot_lo += usd[0]; tot_hi += usd[1]
            calls, _ = part_totals((led or {}).get("rows"), steps)
            look = "plain"
            if led is None or other:
                right = "—"
            elif without and gi == 1:
                right = "이번엔 뺌"
            elif live:
                got = got_by["read" if gi == 0 else "model"]
                right = (f"호출 {got:,}회 / 예상 {span(calls)}회" if got else f"아직 · 예상 {span(calls)}회")
                look = "over" if got > calls[1] else "plain"
            else:
                right = f"예상 {span(calls)}회"
            groups.append((name, sub, tag, right, tuple(rows), look))
        if led is None:
            total = ("—", "합계")
        elif tot_missing:
            total = ("단가 미입력", "합계(이번 실행)" if without else "합계")
        else:
            total = (money([tot_lo, tot_hi]), "합계(이번 실행)" if without else "합계")
        return tuple(groups) + (total,)

    def _draw_ledger_rows(self, view: tuple) -> None:
        C, P = self.colors, self.px
        L = self._lab
        box = self._led_groups
        for c in box.winfo_children():
            c.destroy()
        r = 0
        heads = []
        for name, sub, tag, right, rows, *_ in view[:-1]:
            h = tk.Frame(box, bg=C["PANEL"]); h.grid(row=r, column=0, sticky="ew", pady=(P(6) if r else 0, 0)); r += 1
            L(h, image=self.icons["receipt"]).pack(side="left", padx=(0, P(5)))
            L(h, name, "CAP_B").pack(side="left")
            if sub:
                L(h, sub, "CAP", "MUTED").pack(side="left", padx=(P(5), 0))
            if tag:
                t = ttk.Label(h, text=CostDialog.TAG, style="Tag.TLabel"); t.pack(side="left", padx=(P(6), 0)); Tip(t, TAG_TIP, self)
            rt = tk.Frame(h, bg=C["PANEL"]); rt.pack(side="right")
            parts = (L(rt, "", "CAP", "MUTED"), L(rt, "", "CAP_B"), L(rt, "", "CAP", "MUTED"))
            for part in parts:
                part.pack(side="left")
            head = {"parts": parts, "tips": [Tip(part, "", self) for part in parts], "spec": None, "row": h, "right": rt, "below": False}
            heads.append(head)
            for w in (h, rt):                                           # 오른쪽 글('호출 14회 / 예상 60~195회')이 꼬리표와 겹치면 아래 줄로
                w.bind("<Configure>", lambda _e, hd=head: App._fit_group_head(self, hd), add="+")
            tk.Frame(box, bg=C["LINE"], height=max(1, P(1))).grid(row=r, column=0, sticky="ew", pady=(P(2), P(2))); r += 1
            for label, amount, look in rows:
                row = tk.Frame(box, bg=C["PANEL"]); row.grid(row=r, column=0, sticky="ew", pady=(P(1), P(1))); r += 1
                fg = C["MUTED"] if look in ("muted", "stale") else C["WARN"] if look == "warn" else C["INK"]
                L(row, label, "BODY", fg="MUTED" if look == "muted" else "INK").pack(side="left")
                L(row, amount, "BODY_B" if look in ("money", "stale") else "BODY", fg=fg).pack(side="right")
                App._leader(self, row).pack(side="left", fill="x", expand=True, padx=P(8))
        self._led_heads = heads

    def _fit_group_head(self, head: dict) -> None:
        """묶음 머리: 왼쪽(영수증 · 이름 · 부제 · 꼬리표)과 오른쪽 호출 글이 한 줄에 들어가지 않으면 오른쪽을 아래 줄 오른쪽 끝으로(겹치지 않게)."""
        h, rt = head["row"], head["right"]
        try:
            width = h.winfo_width()
            if width <= 1:
                return
            def pads(w):                                                # pack 의 padx: 0 · 5 · (5, 0) · '5 0'
                v = w.pack_info().get("padx", 0)
                vals = [int(float(x)) for x in (str(v).replace("{", " ").replace("}", " ").split() if not isinstance(v, (tuple, list)) else v)]
                return sum(vals) if len(vals) > 1 else 2 * (vals[0] if vals else 0)
            left = sum(w.winfo_reqwidth() + pads(w) for w in h.pack_slaves() if w is not rt)
            below = left + rt.winfo_reqwidth() + self.px(10) > width
        except (tk.TclError, ValueError, IndexError):
            return
        if below == head["below"]:
            return
        head["below"] = below
        rt.pack_forget()
        if below:
            rt.pack(side="bottom", anchor="e", before=h.pack_slaves()[0], pady=(self.px(1), 0))
        else:
            rt.pack(side="right")

    def _set_group_right(self, head: dict, right: str, look: str) -> None:
        """묶음 머리 오른쪽: '호출 **31회** / 예상 90회'(실제 굵게 먹 — 어림보다 많으면 주의 색), 그 밖에는 보조 글 한 조각."""
        spec = (right, look)
        if head.get("spec") == spec:
            return
        head["spec"] = spec
        C = self.colors
        a, b, c = head["parts"]
        m = re.match(r"^(호출 )([\d,]+회)( / .*)$", right)
        fg = C["WARN"] if look == "over" else C["MUTED"]
        if m:
            a.configure(text=m.group(1), fg=fg); b.configure(text=m.group(2), fg=C["WARN"] if look == "over" else C["INK"]); c.configure(text=m.group(3), fg=fg)
        else:
            a.configure(text=right, fg=fg); b.configure(text=""); c.configure(text="")
        for tip in head["tips"]:
            tip.text = ("이번 실행에서 이 묶음이 부른 호출(재시도 포함) / 실행 전 어림" + (" — 어림보다 많습니다" if look == "over" else "")) if m else ""

    def _leader(self, parent) -> tk.Canvas:
        """장부 줄의 이름과 금액 사이 점선(RULE, 1px 긋고 2px 띄우는 조각을 손으로 — Tk dash 는 Windows 에서 몇 가지 펜 모양뿐)."""
        C, P = self.colors, self.px
        try:
            from tkinter import font as tkfont
            h = tkfont.Font(self, font=self.fonts["BODY"]).metrics("linespace")
        except tk.TclError:
            h = P(16)
        cv = tk.Canvas(parent, height=h, width=P(12), bg=C["PANEL"], highlightthickness=0, bd=0)

        def draw(_e=None):
            cv.delete("all"); y = cv.winfo_height() - P(4); x = 0; W = cv.winfo_width()
            while x < W:
                cv.create_line(x, y, x + max(1, P(1)), y, fill=C["RULE"]); x += P(3)
        cv.bind("<Configure>", draw)
        return cv

    def _draw_calls_bar(self) -> None:
        """호출 막대(8px): LINE 길 위에 어림 범위(lo/끝 ~ 끝, BAND)와 실제 호출(0 ~ 실제/끝, INK)을 겹쳐 그리고, 눈금 '0 · lo · 끝'."""
        d = vars(self); vals = d.get("_led_bar_vals")
        cv, tk_ = d.get("c_led_bar"), d.get("c_led_ticks")
        if cv is None or not vals:
            return
        from .gui_preview import clip_x, rounded_path
        C, P = self.colors, self.px
        calls, used = vals
        W, H = cv.winfo_width(), cv.winfo_height()
        cv.delete("all"); tk_.delete("all")
        if W <= 2 or not calls:
            return
        track = rounded_path(0, 0, W, H, H / 2)
        flat = lambda pts: [c for pt in pts for c in pt]
        cv.create_polygon(flat(track), fill=C["LINE"], outline="")
        top = max(calls[1], used, 1)
        bx = W * calls[0] / top
        band = clip_x(track, bx, W * calls[1] / top)
        if len(band) >= 3:
            cv.create_polygon(flat(band), fill=C["BAND"], outline="")
        if used:
            ink = clip_x(track, 0, W * min(used, top) / top)
            if len(ink) >= 3:
                cv.create_polygon(flat(ink), fill=C["INK"], outline="")
        font = self.fonts["CAP"]; y = tk_.winfo_height() // 2
        tk_.create_text(0, y, text="0", anchor="w", fill=C["MUTED"], font=font)
        end = tk_.create_text(W, y, text=f"{top:,}", anchor="e", fill=C["MUTED"], font=font)
        mid = tk_.create_text(bx, y, text=f"{calls[0]:,}", anchor="center", fill=C["MUTED"], font=font)
        x0, _, x1, _ = tk_.bbox(mid)
        if x0 < P(14) or x1 > tk_.bbox(end)[0] - P(6):
            tk_.delete(mid)                                              # 범위 시작이 끝·0 과 겹치면 빼고 위 줄의 '어림 lo~hi'가 말함

    # ---- 진행 카드(명세 §5.3~§5.5·§7): 머리(알약 · 전체 막대 · 전체 %) · 단계 띠 · 큰 숫자 줄 · 쪽 띠 ----
    def _font(self, name: str):
        """self.fonts 의 글꼴 객체(재기·높이) — 한 번 만들어 둔다."""
        cache = self.__dict__.setdefault("_font_objs", {})
        if name not in cache:
            from tkinter import font as tkfont
            cache[name] = tkfont.Font(self, font=self.fonts[name])
        return cache[name]

    def _build_progress_card(self, parent) -> ttk.Frame:
        C, P, L = self.colors, self.px, self._lab
        card = ttk.Frame(parent, style="Card.TFrame", padding=(P(20), P(14), P(20), P(14)))
        b = tk.Frame(card, bg=C["PANEL"]); b.pack(fill="both", expand=True); b.columnconfigure(0, weight=1)
        head = tk.Frame(b, bg=C["PANEL"]); head.grid(row=0, column=0, sticky="ew")
        L(head, "진행", "SECTION").pack(side="left")
        self.l_pill = ttk.Label(head, text="준비됨", style="Pillidle.TLabel"); self.l_pill.pack(side="left", padx=(P(10), 0))
        self.l_pct = L(head, "전체 0%", "BODY_B"); self.l_pct.pack(side="right")
        self.progress = ttk.Progressbar(head, style="Panel.Bar.Horizontal.TProgressbar", mode="determinate", maximum=1000, value=0)
        self.progress.pack(side="left", fill="x", expand=True, padx=(P(10), P(12)))
        Tip(self.progress, "전체 진행 — 단계마다 걸리는 시간의 어림 무게로 셈(판독이 대부분)", self)
        # 단계 띠: 열마다 동그라미 + 이음줄 / 이름(+ 영수증) / (3번) 꼬리표 / 상태 줄 / 비용 줄 — 줄 하나 = Label 하나(한 줄에 표지 둘을 두지 않음)
        band = self._band = tk.Frame(b, bg=C["PANEL"]); band.grid(row=1, column=0, sticky="ew", pady=(P(14), 0))
        self._band_cols: list[dict] = []
        for i in range(6):
            col = tk.Frame(band, bg=C["PANEL"]); col.grid(row=0, column=i, sticky="nsew")
            top = tk.Frame(col, bg=C["PANEL"]); top.pack(fill="x")
            circ = tk.Label(top, bg=C["PANEL"], bd=0, padx=0, pady=0, highlightthickness=0, compound="center", font=self.fonts["CAP"], fg=C["MUTED"])
            circ.pack(side="left")
            conn = tk.Frame(top, bg=C["EDGE"], height=1)
            if i < 5:
                conn.pack(side="left", fill="x", expand=True, padx=(P(8), P(8)))
            name = L(col, "", "BODY", compound="right"); name.pack(anchor="w", pady=(P(6), 0))
            lines = tk.Frame(col, bg=C["PANEL"]); lines.pack(anchor="w", fill="x", padx=(0, P(10)))
            self._band_cols.append({"circle": circ, "conn": conn, "name": name, "lines": lines, "head": None, "spec": None, "width": P(40)})
        # 한 번에 인식이 아닌 명령(§7.2): 단계 띠 대신 한 줄
        task = self._task_row = tk.Frame(b, bg=C["PANEL"])
        self.l_task_circle = tk.Label(task, bg=C["PANEL"], bd=0, padx=0, pady=0, highlightthickness=0, compound="center", font=self.fonts["CAP_B"], fg=C["RED"])
        self.l_task_circle.pack(side="left")
        self.l_task_title = L(task, "", "BODY_B"); self.l_task_title.pack(side="left", padx=(P(10), 0))
        self.l_task_sub = L(task, "", "CAP", "MUTED"); self.l_task_sub.pack(side="left", padx=(P(10), 0), pady=(P(2), 0))
        # 큰 숫자 줄: 왼쪽 '판독 12 / 45쪽 …' · 오른쪽 '지난 시간 … · 남은 시간 … · 이번 실행 호출 …'(실행 전 '어림 호출 …' · [비용 미리 보기])
        big = self._big_row = tk.Frame(b, bg=C["PANEL"]); big.grid(row=3, column=0, sticky="ew", pady=(P(12), 0))
        self._big_left = tk.Frame(big, bg=C["PANEL"]); self._big_left.pack(side="left", anchor="s")
        right = self._big_right_box = tk.Frame(big, bg=C["PANEL"]); right.pack(side="right", anchor="s")
        self._big_wrapped = False
        for w in (big, self._big_left, right):                         # 글이 바뀌어 왼쪽·오른쪽 너비가 바뀔 때도
            w.bind("<Configure>", lambda _e: App._fit_big(self), add="+")
        self._big_rparts = tk.Frame(right, bg=C["PANEL"]); self._big_rparts.pack(side="left", anchor="s")
        self.b_big_est = self._btn(right, "비용 미리 보기", self.do_estimate, style="Link.TButton",
                                   tip="[▶ 한 번에 인식 (HWPX 만들기)]의 호출·금액을 어림합니다 — 아무것도 쓰지 않고 결과 폴더도 만들지 않습니다.")
        self._big_spec = self._right_spec = None
        # 쪽 띠: 칸(손으로 그린 Canvas) + 눈금 / 범례 + '칸을 누르면 …'
        strip = self._strip = tk.Frame(b, bg=C["PANEL"])
        self.c_strip = tk.Canvas(strip, height=P(18) + P(2) + P(16), bg=C["PANEL"], highlightthickness=0, bd=0, width=P(40), cursor="hand2")
        self.c_strip.pack(fill="x")
        leg = tk.Frame(strip, bg=C["PANEL"]); leg.pack(fill="x", pady=(P(4), 0))
        L(leg, "칸을 누르면 그 쪽의 쪽별 상태", "CAP", "MUTED").pack(side="right")
        self.c_legend = tk.Canvas(leg, height=P(16), bg=C["PANEL"], highlightthickness=0, bd=0, width=P(40))
        self.c_legend.pack(side="left", fill="x", expand=True)
        self._strip_pages: list[int] = []; self._strip_marks: dict[int, str] = {}; self._strip_cells = (1, [])
        self._strip_tip = Tip(self.c_strip, "", self)
        self.c_strip.bind("<Configure>", lambda _e: App._draw_strip(self), add="+")
        self.c_legend.bind("<Configure>", lambda _e: App._draw_legend(self), add="+")
        self.c_strip.bind("<Motion>", lambda e: App._strip_hover(self, e.x), add="+")
        self.c_strip.bind("<Button-1>", lambda e: App._strip_click(self, e.x), add="+")
        return card

    STRIP_LABEL = {"record": "판독 기록 있음 · 건너뜀", "remain": "남음", "done": "끝남", "now": "판독 중", "fail": "판독 실패(다시 시도 가능)"}
    LEGEND = (("done", "끝남"), ("now", "지금"), ("fail", "실패"), ("record", "기록 있음 · 건너뜀"), ("remain", "남음"))
    LOOKS = {"cap": ("CAP", "MUTED"), "cap_b": ("CAP_B", "INK"), "red_b": ("CAP_B", "RED"), "red": ("CAP", "RED"), "warn": ("CAP", "WARN")}
    BIG_LOOKS = {"m": ("BODY", "MUTED"), "big": ("BIG", "INK"), "unit": ("UNIT", "MUTED"), "body": ("BODY", "INK"), "b": ("BODY_B", "INK"),
                 "red_b": ("BODY_B", "RED"), "warn": ("BODY", "WARN")}

    def _progress_state(self) -> tuple[str, str, str, dict | None]:
        """진행 카드가 그릴 상태 (phase, 명령, 이름, 끝난 결과): 실행 중 · 끝난 뒤(다음 실행·원본·탭을 바꾸기 전까지) · 실행 전 · 원본 없음.
        실행 전 단계 띠는 장부의 마지막 어림이 어느 실행인지를 따른다(문자 PDF 면 4번이 글자층 넣기 — §5.3)."""
        d = vars(self)
        if d.get("running"):
            cmd = d.get("_running_cmd") or ""
            label = d.get("_run_label") or ("업데이트" if cmd == "update" else {"install": "패키지 설치·점검"}.get(cmd, cmd) if cmd
                                            else self.v_status.get().rstrip("…").strip())
            return "running", cmd, label, None
        res = d.get("_run_result")
        if res:
            return "done", res.get("cmd") or "", res.get("label", ""), res
        led = d.get("_ledger")
        return ("ready" if self.pdf() else "empty"), ("run-pdf" if led and led.get("action") == "pdf" else "run"), "", None

    def _sync_progress(self) -> None:
        """진행 카드를 지금 상태로(바뀐 곳만 다시 그림): 알약 · 막대 · 전체 % · 단계 띠(또는 다른 명령의 한 줄) · 큰 숫자 줄 · 쪽 띠."""
        d = vars(self)
        if d.get("_band_cols") is None:
            return
        from . import gui_progress as G
        phase, cmd, label, res = App._progress_state(self)
        tr = self.tracker
        pill, look = G.pill_view(phase=phase, stage=tr.stage if phase == "running" else "", label=label, result=res)
        self.l_pill.configure(text=pill, style=f"Pill{look}.TLabel")
        if phase == "running":
            frac = tr.frac
        elif phase == "done":
            frac = 1.0 if res.get("kind") in ("ok", "partial") else tr.frac
        else:
            frac = 0.0
        self.l_pct.configure(text=f"전체 {frac * 100:.0f}%")
        if not (phase == "running" and not cmd):                        # 이미지 묶기·키 확인은 막대를 스스로 움직인다(_bundle_msg)
            self.progress.configure(value=self._bar_value(frac))
        led = d.get("_ledger"); live = bool(d.get("_ledger_live")) and led is not None
        counts = (d.get("_run_counts") or d.get("_counts")) if phase in ("running", "done") else d.get("_counts")
        est = "none" if led is None else ("stale" if phase == "ready" and d.get("_led_stale") else "ok")
        if cmd in G.RUN_CMDS:
            ctx = bool(led.get("context_model", True)) if live else App._context_model_enabled(self)
            view = G.band_view(cmd, phase=phase, idx=tr._idx, done=tr.done, total=tr.total, note=tr.note, seen=d.get("_seen"), result=res,
                               without=live and led.get("choice") == "without", counts=counts, rows=(led or {}).get("rows"), est=est, ctx_model=ctx)
            App._draw_band(self, view)
            self._task_row.grid_remove(); self._band.grid()
        else:
            tv = G.task_view(cmd, label, phase=phase, result=res)
            self.l_task_circle.configure(image=self._circles[tv["circle"]], text="!" if tv["circle"] == "fail" else "")
            self.l_task_title.configure(text=tv["title"]); self.l_task_sub.configure(text=tv["sub"])
            self._band.grid_remove(); self._task_row.grid(row=2, column=0, sticky="ew", pady=(self.px(14), 0))
        pdf = self.pdf()
        shot = bool(pdf) and bool(SCREENSHOT_NAME.fullmatch(Path(pdf).name))
        now = [n for n, st in (d.get("_page_live") or {}).items() if st == "now"] if phase == "running" else ()
        left = G.big_view(cmd, phase=phase, stage=tr.stage, done=tr.done, total=tr.total, note=tr.note, seen=d.get("_seen"),
                          result=res, counts=counts, screenshot=shot, label=label, now_pages=now)
        right, link = App._big_right(self, phase, cmd, res, led, est)
        big = any(lk == "big" for _, lk in left)
        App._set_parts(self, self._big_left, left, "_big_spec", big)
        App._set_parts(self, self._big_rparts, right, "_right_spec", big)
        App._fit_big(self)
        if link and self.b_big_est.winfo_manager() != "pack":
            self.b_big_est.pack(side="left", anchor="s", pady=(0, max(0, self._font("BIG").metrics("descent") - self._font("BODY").metrics("descent") - self.px(3)) if big else 0))
        elif not link and self.b_big_est.winfo_manager():
            self.b_big_est.pack_forget()
        show = phase != "empty" and len(d.get("_strip_pages") or ()) > 1
        if show and not self._strip.winfo_manager():
            self._strip.grid(row=4, column=0, sticky="ew", pady=(self.px(8), 0))
        elif not show and self._strip.winfo_manager():
            self._strip.grid_remove()
        App._sync_collate_progress(self, phase, cmd, label, res)

    def _big_right(self, phase: str, cmd: str, res, led, est: str) -> tuple[list, bool]:
        """큰 숫자 줄 오른쪽: 실행 중 '지난 시간 · 남은 시간 · 이번 실행 호출', 끝난 뒤 '걸린 시간 · 이번 실행 호출', 실행 전 '어림 호출 150~285회'
        (어림이 없거나 낡았으면 [비용 미리 보기] 글자 단추를 함께 — 두 번째 값이 True)."""
        from .gui_progress import RUN_CMDS
        tr = self.tracker
        model = cmd in ("run", "run-pdf", "read", "adjudicate", "build", "collate")
        word = "이번 실행 호출 " if cmd in RUN_CMDS else "이번 명령 호출 "
        if phase == "running":
            el = max(0.0, time.time() - tr.t0)
            parts = [("지난 시간 ", "m"), (fmt_elapsed(el) if el < 60 else fmt_duration(el), "b")]
            eta = tr.eta()
            if eta:
                parts += [(" · 남은 시간 ", "m"), (eta.replace("남은 시간 ", ""), "b")]
            calls = vars(self).get("_calls_used")
            if model and calls is not None:
                parts += [(" · " + word, "m"), (f"{calls:,}회", "b")]
            return parts, False
        if phase == "done":
            parts = []
            if res.get("elapsed") is not None:
                parts = [("걸린 시간 " if res.get("kind") in ("ok", "partial") else "지난 시간 ", "m"), (fmt_duration(res["elapsed"]), "b")]
            if model and res.get("calls") is not None:
                parts += [((" · " if parts else "") + word, "m"), (f"{res['calls']:,}회", "b")]
            return parts, False
        if phase == "ready" and cmd in RUN_CMDS:
            if est == "ok":
                c = (led.get("totals") or {}).get("calls") or [0, 0]
                return [("어림 호출 ", "m"), (f"{c[0]:,}~{c[1]:,}회" if c[0] != c[1] else f"{c[1]:,}회", "b")], False
            return ([("어림 다시 필요 · ", "warn")] if est == "stale" else [("어림 호출 — · ", "m")]), True
        return [], False

    def _fit_big(self) -> None:
        """큰 숫자 줄: 왼쪽('판독 12 / 45쪽 · 지금 14~18쪽 판독 중 (동시 5쪽)')과 오른쪽(지난·남은 시간 · 이번 실행 호출)이 한 줄에 들어가지 않으면
        오른쪽을 아래 줄로 내린다(좁은 창·462쪽 책의 긴 글 — 오른쪽이 눌려 잘리지 않게). 들어가면 다시 한 줄로."""
        d = vars(self); row, left, right = d.get("_big_row"), d.get("_big_left"), d.get("_big_right_box")
        if row is None:
            return
        try:
            avail = row.winfo_width()
            if avail <= 1:
                return
            wrap = left.winfo_reqwidth() + right.winfo_reqwidth() + self.px(16) > avail
        except tk.TclError:
            return
        if wrap == d.get("_big_wrapped"):
            return
        self._big_wrapped = wrap
        left.pack_forget(); right.pack_forget()
        if wrap:
            left.pack(side="top", anchor="w"); right.pack(side="top", anchor="w", pady=(self.px(2), 0))
        else:
            left.pack(side="left", anchor="s"); right.pack(side="right", anchor="s")

    def _set_parts(self, frame, parts: list, key: str, big: bool) -> None:
        """굵기·크기가 섞인 한 줄(큰 숫자 줄)을 Label 조각으로 — 같으면 그대로. big 이면 큰 숫자와 밑줄(글자 바닥)을 맞춘다."""
        if vars(self).get(key) == (parts, big):
            return
        setattr(self, key, (parts, big))
        for c in frame.winfo_children():
            c.destroy()
        base = self._font("BIG").metrics("descent") if big else 0
        P = self.px
        for text, look in parts:
            font, fg = self.BIG_LOOKS.get(look, ("BODY", "MUTED"))
            lab = self._lab(frame, text, font, fg)
            pad = max(0, base - self._font(font).metrics("descent")) if big and look != "big" else 0
            lab.pack(side="left", anchor="s", pady=(0, pad), padx=(P(8), P(4)) if look == "big" else 0)

    def _draw_band(self, view: list) -> None:
        """단계 띠를 그림 값(gui_progress.band_view)대로 — 열마다 머리(동그라미·이름)와 줄을 바뀐 것만 다시 만든다.
        열 최소 너비는 명세 §4 값과 그 열의 가장 긴 줄을 잰 값 + 10 가운데 큰 것(462쪽 책의 '호출 450~1,800회'도 들어가게)."""
        from .gui_progress import TAG, COL_MIN, COL_WEIGHT
        C, P = self.colors, self.px
        for i, (col, v) in enumerate(zip(self._band_cols, view)):
            head = (v["circle"], v["num"], v["name"], v["bold"], v["receipt"], v["connector"])
            if head != col["head"]:
                col["head"] = head
                k = v["circle"]
                col["circle"].configure(image=self._circles[k], text={"wait": str(v["num"]), "skip": "–", "fail": "!"}.get(k, ""),
                                        fg=C["RED"] if k == "fail" else C["MUTED"], font=self.fonts["CAP_B" if k == "fail" else "CAP"])
                done = v["connector"] == "done"
                col["conn"].configure(bg=C["INK"] if done else C["EDGE"], height=max(1, P(2)) if done else max(1, int(1.5 * self.scale)))
                col["name"].configure(text=v["name"] + (" " if v["receipt"] else ""), image=self.icons["receipt"] if v["receipt"] else "",
                                      font=self.fonts["BODY_B" if v["bold"] else "BODY"])
            spec = (v["tag_top"], tuple(ln if ln == TAG else tuple(ln) for ln in v["lines"]))
            if spec != col["spec"]:
                col["spec"] = spec
                box = col["lines"]
                for c in box.winfo_children():
                    c.destroy()
                lines = ([TAG] if v["tag_top"] else []) + list(v["lines"])
                need = self._font("BODY_B" if v["bold"] else "BODY").measure(v["name"]) + (P(18) if v["receipt"] else 0)
                for ln in lines:
                    if ln == TAG:
                        t = ttk.Label(box, text=CostDialog.TAG, style="Tag.TLabel"); t.pack(anchor="w", pady=(P(1), P(2)))
                        Tip(t, TAG_TIP, self)
                        need = max(need, self._font("CAP").measure(CostDialog.TAG) + P(12))
                        continue
                    row = tk.Frame(box, bg=C["PANEL"]); row.pack(anchor="w")
                    w = 0
                    for text, look in ln:
                        font, fg = self.LOOKS.get(look, ("CAP", "MUTED"))
                        self._lab(row, text, font, fg).pack(side="left")
                        w += self._font(font).measure(text)
                    need = max(need, w)
                col["width"] = need + P(10)
            self._band.columnconfigure(i, weight=COL_WEIGHT[i], minsize=max(P(COL_MIN[i]), col["width"]))

    # ---- 쪽 띠(명세 §5.4) ----
    def _pagemap_done(self, seq: int, data: dict | None) -> None:
        """작업 스레드가 센 쪽 띠의 칸(state.page_marks). 끝난 실행 뒤라면 실행 전에 기록이 없던 쪽 가운데 이제 기록이 생긴 쪽을 '끝남'으로,
        그리고 이번 실행의 쪽 알림이 온 쪽은 알림대로(다시 판독한 쪽도 '끝남', 멈출 때 남은 '지금'은 '남음'). 실행 중에 온 셈도 알림을 덮지 않는다."""
        from .gui_progress import merge_marks
        d = vars(self)
        if seq != d.get("_counts_seq") or not data:
            if seq == d.get("_counts_seq"):
                self._strip_pages, self._strip_marks = [], {}
                App._sync_progress(self); App._draw_strip(self); App._draw_legend(self)
            return
        marks = dict(data.get("marks") or {})
        res, before = d.get("_run_result"), d.get("_marks0")
        mine = bool(res) and res.get("cmd") in PAGE_CMDS
        if mine and before is not None:
            for n, st in marks.items():
                if st == "record" and before.get(n) != "record":
                    marks[n] = "done"
        if d.get("running") and d.get("_running_cmd") in PAGE_CMDS or mine:
            marks = merge_marks(marks, d.get("_page_live"), finished=not d.get("running"), before=before)
        self._strip_pages = list(data.get("pages") or [])
        self._strip_marks = marks
        App._sync_progress(self)
        App._draw_strip(self); App._draw_legend(self)

    def _on_page(self, n: int, state: str) -> None:
        """쪽 알림('page', 쪽, 상태 — cli.PAGE_EVENT): 그 칸을 바꾸고 큐를 다 비운 뒤 한 번 다시 그린다(_poll). 쪽 띠에 없던 쪽(셈이 오기 전)은 더한다.
        이 탭 원본을 판독하는 명령(한 번에 인식 둘 · ③ 판독)의 알림만 — 늦게 온 지난 명령의 알림은 버린다."""
        d = vars(self)
        if not d.get("running") or d.get("_running_cmd") not in PAGE_CMDS or state not in self.STRIP_LABEL:
            return
        n = int(n)
        marks = d.setdefault("_strip_marks", {})
        if state == "remain" and marks.get(n) == "fail":
            state = "fail"                                              # 지난번에 실패한 쪽은 다시 읽기 시작할 때까지 실패로(읽을 차례라는 첫 알림이 지우지 않게)
        if n not in marks:
            pages = d.setdefault("_strip_pages", [])
            if n not in pages:
                import bisect
                bisect.insort(pages, n)
        marks[n] = state
        d.setdefault("_page_live", {})[n] = state
        self._strip_dirty = True

    def _draw_strip(self) -> None:
        """칸(높이 18 · 사이 2 · 최소 12)과 눈금. 다섯 상태는 모양과 밝기로 갈린다: 끝남 먹 채움 · 지금 굵은 먹 테 · 실패 RED 테 + '!' ·
        기록 있음 LINE 속 + MUTED 점선(손으로 그은 조각) · 남음 EDGE 가는 테. 칸이 모자라면 여러 쪽을 한 칸에(gui_progress.strip_layout)."""
        d = vars(self); cv = d.get("c_strip")
        if cv is None:
            return
        from .gui_progress import strip_layout, cell_kind, strip_ticks
        from .gui_preview import rounded_path, dash_segments, clip_x
        C, P = self.colors, self.px
        cv.delete("all")
        pages, marks = d.get("_strip_pages") or [], d.get("_strip_marks") or {}
        W = cv.winfo_width()
        if W <= 2 or not pages:
            self._strip_cells = (1, []); return
        gap, ch, r = max(1, P(2)), P(18), max(1, P(2))
        per, cells = strip_layout(len(pages), W, gap, P(12))
        self._strip_cells = (per, cells)
        flat = lambda pts: [c for pt in pts for c in pt]
        poly = lambda pts, fill: cv.create_polygon(flat(pts), fill=fill, outline="")
        bold = max(2, round(2.5 * self.scale)); fail_w = max(2, P(2)); thin = max(1, int(self.scale))
        for x0, x1, a, b in cells:
            kind, frac = cell_kind([marks.get(pages[k], "remain") for k in range(a, b)])
            outer = rounded_path(x0, 0, x1, ch, r)
            if kind == "done":
                poly(outer, C["INK"])
            elif kind in ("now", "fail", "remain"):
                bw = {"now": bold, "fail": fail_w, "remain": thin}[kind]
                poly(outer, {"now": C["INK"], "fail": C["RED"], "remain": C["EDGE"]}[kind])
                inner = rounded_path(x0 + bw, bw, x1 - bw, ch - bw, max(0, r - bw))
                poly(inner, C["PANEL"])
                if frac > 0 and kind != "fail":
                    part = clip_x(inner, x0 + bw, x0 + bw + (x1 - x0 - 2 * bw) * frac)
                    if len(part) >= 3:
                        poly(part, C["INK"])
                if kind == "fail":
                    cv.create_text((x0 + x1) / 2, ch / 2, text="!", fill=C["RED"], font=self.fonts["CAP_B"])
            else:                                                       # 기록 있음 · 건너뜀
                poly(outer, C["LINE"])
                lw = max(1, int(1.5 * self.scale))
                path = rounded_path(x0 + lw / 2, lw / 2, x1 - lw / 2, ch - lw / 2, max(0, r - lw / 2))
                for seg in dash_segments(path, P(3), P(2)):
                    if len(seg) >= 4:
                        cv.create_line(*seg, fill=C["MUTED"], width=lw)
        font = self._font("CAP"); y = ch + gap + P(8)
        placed: list[tuple[float, float, int]] = []
        ticks = strip_ticks(pages, per, cells)
        for k, (x, text) in enumerate(ticks):
            w = font.measure(text)
            x = min(max(x, w / 2), W - w / 2)
            if placed and x - w / 2 < placed[-1][1] + P(6):
                if k == len(ticks) - 1:                                 # 끝 쪽은 늘 보인다 — 겹치는 앞 눈금을 뺀다
                    cv.delete(placed.pop()[2])
                else:
                    continue
            item = cv.create_text(x, y, text=text, fill=C["MUTED"], font=self.fonts["CAP"])
            placed.append((x - w / 2, x + w / 2, item))

    def _draw_legend(self) -> None:
        """범례: 견본(14px — 실패 견본의 '!'를 칸과 같은 F_CAP_B 9pt 로 그려도 들어가게) + '끝남 n' … — 수는 쪽 단위."""
        d = vars(self); cv = d.get("c_legend")
        if cv is None:
            return
        from .gui_preview import rounded_path, dash_segments
        C, P = self.colors, self.px
        cv.delete("all")
        marks = d.get("_strip_marks") or {}
        pages = d.get("_strip_pages") or []
        if not pages:
            return
        count = {k: 0 for k, _ in self.LEGEND}
        for n in pages:
            count[marks.get(n, "remain")] = count.get(marks.get(n, "remain"), 0) + 1
        font = self._font("CAP"); s = P(14); y0 = (cv.winfo_height() - s) / 2; x = 0
        flat = lambda pts: [c for pt in pts for c in pt]
        for kind, name in self.LEGEND:
            box = rounded_path(x, y0, x + s, y0 + s, max(1, P(2)))
            if kind == "done":
                cv.create_polygon(flat(box), fill=C["INK"], outline="")
            elif kind == "record":
                cv.create_polygon(flat(box), fill=C["LINE"], outline="")
                lw = max(1, int(1.5 * self.scale))
                for seg in dash_segments(rounded_path(x + lw / 2, y0 + lw / 2, x + s - lw / 2, y0 + s - lw / 2, 1), P(3), P(2)):
                    if len(seg) >= 4:
                        cv.create_line(*seg, fill=C["MUTED"], width=lw)
            else:
                bw = {"now": max(2, P(2)), "fail": max(1, int(1.5 * self.scale)) + 0, "remain": max(1, int(self.scale))}[kind]
                cv.create_polygon(flat(box), fill={"now": C["INK"], "fail": C["RED"], "remain": C["EDGE"]}[kind], outline="")
                cv.create_polygon(flat(rounded_path(x + bw, y0 + bw, x + s - bw, y0 + s - bw, 1)), fill=C["PANEL"], outline="")
                if kind == "fail":
                    cv.create_text(x + s / 2, y0 + s / 2, text="!", fill=C["RED"], font=self.fonts["CAP_B"])     # 칸의 '!'와 같은 9pt(앱에서 9pt 아래 글은 없음)
            text = f"{name} {count.get(kind, 0):,}"
            cv.create_text(x + s + P(5), y0 + s / 2, text=text, anchor="w", fill=C["INK"], font=self.fonts["CAP"])
            x += s + P(5) + font.measure(text) + P(14)

    def _strip_at(self, x: float):
        per, cells = vars(self).get("_strip_cells") or (1, [])
        for x0, x1, a, b in cells:
            if x0 - 1 <= x <= x1 + 1:
                return a, b
        return None

    def _strip_hover(self, x: float) -> None:
        """칸 위 풍선: '9쪽 · 판독 실패(다시 시도 가능)' / 묶음 '231~239쪽 · 끝남 7 · 남음 2'."""
        hit = App._strip_at(self, x); tip = vars(self).get("_strip_tip")
        if tip is None:
            return
        text = ""
        if hit:
            pages, marks = self._strip_pages, self._strip_marks
            a, b = hit
            if b - a == 1:
                text = f"{pages[a]}쪽 · {self.STRIP_LABEL.get(marks.get(pages[a], 'remain'), '')}"
            else:
                states = [marks.get(pages[k], "remain") for k in range(a, b)]
                text = f"{pages[a]}~{pages[b - 1]}쪽 · " + " · ".join(f"{name} {states.count(k)}" for k, name in self.LEGEND if states.count(k))
            text += "\n누르면 쪽별 상태에서 그 쪽을 보입니다"
        if text != tip.text:
            tip.text = text
            tip._leave()
            if text:
                tip._enter()

    def _strip_click(self, x: float) -> None:
        hit = App._strip_at(self, x)
        if hit:
            App._show_page_row(self, self._strip_pages[hit[0]])

    def _show_page_row(self, page: int) -> None:
        """[쪽별 상태] 탭을 열고 그 쪽 줄로 굴려 고른다(실행 중에도). 표가 비어 있으면 새로 채운 뒤에(_fill_table)."""
        self.v_nb.set(1); self.nb.select(1)
        for iid in self.table.get_children():
            vals = self.table.item(iid, "values")
            if vals and str(vals[0]) == str(page):
                self.table.see(iid); self.table.selection_set(iid)
                self.after(60, lambda: self._reveal(self._log_card))
                return
        self._want_row = page
        self.refresh_table()

    # ---- 책갈피 OCR 의 교정 실행·진행 카드 ----
    def _sync_collate_progress(self, phase: str, cmd: str, label: str, res) -> None:
        """책갈피 OCR 오른쪽 카드: 교정 실행의 판독기·모델 · 읽기 방향·교정 정책·메모, 상태 줄(중지 뒤·끝난 뒤), 작은 진행 카드(구획 단위)."""
        d = vars(self)
        if d.get("l_cpill") is None:
            return
        from .gui_progress import pill_view
        from .progress import UNITS
        self.l_crun_summary.configure(text=f"{self.v_provider.get()} · {self.v_model.get().strip() or '(모델 없음)'}")
        self.l_crun_opts.configure(text=f"읽기 방향 {self._mode_vars[0].get()} · 교정 정책 {self.v_policy.get()} · 메모 {self.v_cmemo.get()}")
        mine = cmd == "collate"
        ph = phase if mine and phase in ("running", "done") else "ready"
        tr = self.tracker
        pill, look = pill_view(phase=ph, stage=tr.stage if ph == "running" else "", label=label, result=res if ph == "done" else None)
        self.l_cpill.configure(text=pill, style=f"Pill{look}.TLabel")
        frac = tr.frac if ph == "running" else (1.0 if ph == "done" and res.get("kind") in ("ok", "partial") else tr.frac if ph == "done" else 0.0)
        self.l_cpct.configure(text=f"전체 {frac * 100:.0f}%")
        self.progress2.configure(value=self._bar_value(frac) if ph != "ready" else 0)
        unit = UNITS.get("collate", "구획")
        seen = (d.get("_seen") or {}).get("collate")
        if ph == "running":
            line = (f"구획 대조 {min(tr.done, tr.total):,} / {tr.total:,}{unit}" if tr.stage == "collate" and tr.total else "구획 대조 — 시작 중…")
            el = max(0.0, time.time() - tr.t0)
            eta = tr.eta().replace("남은 시간 ", "") or "—"
            calls = d.get("_calls_used")
            when = f"지난 시간 {fmt_elapsed(el) if el < 60 else fmt_duration(el)} · 남은 시간 {eta} · 이번 명령 호출 {'—' if calls is None else f'{calls:,}회'}"
        elif ph == "done":
            word = {"ok": "끝", "partial": "끝(확인할 것 있음)", "stopped": "중지됨"}.get(res.get("kind"), "오류로 멈춤")
            line = f"구획 대조 {seen[0]:,} / {seen[1]:,}{unit} · {word}" if seen else f"{label} {word}"
            calls = res.get("calls")
            when = f"걸린 시간 {fmt_duration(res.get('elapsed') or 0)} · 이번 명령 호출 {'—' if calls is None else f'{calls:,}회'}"
        else:
            rows = d.get("collate_rows") or []
            line = f"구획 대조 — 색인한 구획 {len(rows):,}개" if rows else "구획 대조 — 구획 색인 뒤에 나옴"
            when = "지난 시간 — · 남은 시간 — · 이번 명령 호출 —"
        self.l_cline.configure(text=line); self.l_ctime.configure(text=when)
        parts = []
        if mine and phase == "running" and d.get("_stop_requested"):
            parts = [(self.v_status.get(), "plain")]
        elif mine and phase == "done" and self.v_status.get().startswith(label):
            tail = self.v_status.get()[len(label):]
            parts = [(label, "bold"), (tail, {"error": "red", "partial": "warn"}.get(res.get("kind"), "plain"))]
        if parts:
            App._set_rich(self, self.t_crun_status, parts); self.t_crun_status.grid(row=5, column=0, sticky="ew", pady=(self.px(8), 0))
        else:
            self.t_crun_status.grid_remove()

    # ---- 고급 도구 카드(명세 §6.6) · 기록 카드 ----
    def _build_tools_card(self, parent) -> ttk.Frame:
        """진행 카드와 기록 카드 사이(펼쳤을 때만): 단계 ①~⑥ · 도구 일곱 — 이름·풍선·명령은 지난 판 설정 탭의 고급 그대로, 영수증은 돈 드는 단추에만.
        7열 균등 격자, 카드 안 너비가 700 보다 좁으면 4열로 접는다."""
        C, P, L = self.colors, self.px, self._lab
        card = ttk.Frame(parent, style="Card.TFrame", padding=(P(20), P(14)))
        b = tk.Frame(card, bg=C["PANEL"]); b.pack(fill="both", expand=True)
        head = tk.Frame(b, bg=C["PANEL"]); head.pack(fill="x")
        L(head, "고급 도구", "SECTION").pack(side="left")
        self._btn(head, "접기 ▴", self.toggle_advanced, style="Toggle.TButton", lock=False, tip="고급 도구를 접습니다(실행 중에도 — 단추만 잠김)").pack(side="right")
        L(head, "보통은 필요 없습니다. 일부 단계만 다시 하거나 기록을 손볼 때 씁니다.", "CAP", "MUTED", wrap=True).pack(
            side="left", fill="x", expand=True, padx=(P(10), P(8)), pady=(P(2), 0))
        steps = [("① 구조 보기", self.do_inspect, "쪽수, 쪽마다 스캔 이미지인지, 텍스트 레이어 글자 수, 쪽번호 표기를 보여 줍니다"),
                 ("② 준비", self.do_prepare, "쪽 이미지와 기존 텍스트 레이어를 결과 폴더에 꺼냅니다"),
                 ("③ 판독", self.do_read, "1차·2차 판독만 합니다(기록이 있는 쪽은 건너뜀)"),
                 ("④ 문서 만들기", self.do_build, "판독 기록으로 HWPX(+DOCX)를 만듭니다. readings 를 손으로 고친 뒤에 누릅니다. "
                                                "번체 문맥 교정으로 모델을 부릅니다(새 후보가 있을 때만 · [설정]에서 끌 수 있음)"),
                 ("⑤ 검수 화면", self.do_review, "원본 위에 영역 상자를 그린 검수 화면(review.html)을 만들어 엽니다"),
                 ("⑥ 보고", self.do_report, "쪽별 상태와 다발 오인식 장부(report.md)를 진행 기록에 보여 줍니다")]
        tools = [("3차 글자 판별", self.do_adjudicate, "1차·2차가 갈린 〔a/b〕 자리를 글자 하나씩 크게 잘라 자형만으로 묻습니다(앞뒤 글자는 글로 주지 않고 후보 차례는 섞음 · 8자리씩 묶어 한 호출, 쪽은 동시에)"),
                 ("혼동 장부", self.do_ledger, "어느 글자가 어느 글자로 읽혔는지 결과 폴더 전체에서 모읍니다(모델 호출 없음)"),
                 ("비교 다시 계산", self.do_remerge, "저장된 1차·2차 판독으로 확인 표시만 다시 계산합니다(모델 호출 없음). 프로그램을 새 판으로 바꾼 뒤에"),
                 ("PDF 글자층 넣기", self.do_textlayer, "판독 기록의 글을 원본 PDF 쪽 위에 보이지 않는 글자층으로 넣어 찾기·복사가 되는 PDF(out\\<이름>_글자층.pdf)를 만들고 엽니다"
                                                    "(모델 호출 없음, 원본은 그대로). 원본에 있던 다른 OCR 의 숨은 글자층은 그 쪽에서 지웁니다"),
                 ("검수 결과 기록…", self.do_mark, "사람이 원본 대조를 마친 쪽에 '원본 대조 완료(사람)' 를 남깁니다"),
                 ("구성요소 점검" if is_frozen() else "패키지 설치·점검", self.install_packages,
                  "설치판에 포함된 구성요소를 점검합니다" if is_frozen() else "필요한 파이썬 패키지를 설치하거나 새 판으로 올립니다"),
                 ("도움말", self.show_help, "쓰는 법을 간단히 보여 줍니다")]
        money = (self.do_read, self.do_build, self.do_adjudicate)       # 모델을 불러 돈이 드는 단추 — 영수증(먼저 비용 창으로 묻는다)
        g = self._tools_grid = tk.Frame(b, bg=C["PANEL"]); g.pack(fill="x", pady=(P(10), 0))
        self._tool_rows = []
        for name, items in (("단계", steps), ("도구", tools)):
            lab = L(g, name, "CAP_B", "MUTED")
            row = []
            for t, c, tip in items:
                kw = {"image": (self.icons["receipt"], "disabled", self.icons["receipt_dis"]), "compound": "right"} if c in money else {}     # 잠긴 때 그림을 따로(점무늬 방지)
                btn = self._btn(g, t + (" " if kw else ""), c, style="Tool.TButton", tip=tip, **kw)
                if c == self.install_packages:
                    self.b_packages = btn
                row.append(btn)
            self._tool_rows.append((lab, row))
        g.bind("<Configure>", lambda e: App._layout_tools(self, e.width), add="+")
        App._layout_tools(self, 10 ** 6)
        L(b, "단추에 마우스를 올리면 하는 일이 나옵니다. 영수증 표가 붙은 단추는 먼저 비용 창으로 묻습니다.\n"
             "프로그램을 새 판으로 바꾼 뒤에는 [비교 다시 계산], 사람이 확인한 쪽은 [검수 결과 기록…].", "CAP", "MUTED", wrap=True).pack(fill="x", pady=(P(8), 0))
        return card

    def _layout_tools(self, width: int) -> None:
        """고급 도구 격자: 7열 균등(카드 안 너비 700 이상이고 가장 긴 단추 일곱이 들어갈 때), 좁으면 4열 — 균등 열이 단추 글을 자르지 않게."""
        try:
            need = max(b.winfo_reqwidth() for _, row in self._tool_rows for b in row) + 2 * self.px(3)
            head = max(lab.winfo_reqwidth() for lab, _ in self._tool_rows) + self.px(12)
        except tk.TclError:
            need, head = self.px(100), self.px(40)
        cols = 7 if width >= max(self.px(700), head + 7 * need) else 4
        if cols == vars(self).get("_tools_cols"):
            return
        self._tools_cols = cols
        P, g = self.px, self._tools_grid
        r = 0
        for lab, row in self._tool_rows:
            lab.grid(row=r, column=0, sticky="nw", padx=(0, P(12)), pady=(P(9), 0))
            for k, btn in enumerate(row):
                btn.grid(row=r + k // cols, column=1 + k % cols, sticky="ew", padx=P(3), pady=P(3))
            r += -(-len(row) // cols)
        for c in range(1, 8):
            g.columnconfigure(c, weight=1 if c <= cols else 0, uniform="tools" if c <= cols else "")

    def _build_log_card(self, parent) -> ttk.Frame:
        """기록 카드(일반·이미지·책갈피 OCR이 함께 씀): 밑줄 탭 [진행 기록 | 쪽별 상태] · 오른쪽 '단계만 다시 하기 · PDF 글자층 넣기 외' [고급 도구 ▸]
        (실행 중에는 잠김 — '실행 중에는 고급 도구를 쓸 수 없습니다'), 아래는 진행 기록(지난 줄 MUTED · 마지막 줄 먹)과 쪽별 상태 표. 남은 높이를 채운다."""
        C, P, L = self.colors, self.px, self._lab
        card = ttk.Frame(parent, style="Card.TFrame", padding=(P(4), 0, P(4), P(4)))
        b = tk.Frame(card, bg=C["PANEL"]); b.pack(fill="both", expand=True)
        head = tk.Frame(b, bg=C["PANEL"]); head.pack(fill="x")
        self.v_nb = tk.IntVar(value=0)
        tabs = tk.Frame(head, bg=C["PANEL"]); tabs.pack(side="left", fill="y", padx=(P(4), 0))
        zero = max(1, self._font("BODY").measure("0"))
        self._nb_tabs = []
        for text, val in (("진행 기록", 0), ("쪽별 상태", 1)):
            rb = ttk.Radiobutton(tabs, text=text, value=val, variable=self.v_nb, command=lambda: self.nb.select(self.v_nb.get()), cursor="hand2",
                                 style="SubTab.TRadiobutton", width=-(-self._font("BODY_B").measure(text) // zero))
            rb.pack(side="left", fill="y", padx=(0, P(2)))
            self._nb_tabs.append(rb)
        self.b_adv = self._btn(head, "고급 도구 ▴" if self.v_adv.get() else "고급 도구 ▸", self.toggle_advanced, style="Toggle.TButton",
                               tip="단계별 실행(①~⑥)과 도구(3차 글자 판별 · 혼동 장부 · 비교 다시 계산 · PDF 글자층 넣기 · 검수 결과 기록 · 패키지 · 도움말)를 펼칩니다. 실행 중에는 쓸 수 없습니다.")
        self.b_adv.pack(side="right", padx=(0, P(6)))
        self.l_adv_cap = L(head, "단계만 다시 하기 · PDF 글자층 넣기 외", "CAP", "MUTED"); self.l_adv_cap.pack(side="right", padx=(0, P(8)))
        tk.Frame(b, bg=C["LINE"], height=max(1, P(1))).pack(fill="x")
        nb = self.nb = ttk.Notebook(b, style="Hidden.TNotebook"); nb.pack(fill="both", expand=True, pady=(P(4), 0))
        nb.bind("<<NotebookTabChanged>>", lambda _e: self.v_nb.set(nb.index("current")))
        lf = tk.Frame(nb, bg=C["PANEL"])
        self.log = tk.Text(lf, height=6, wrap="word", state="disabled", font=self.font_log, bg=C["PANEL"], fg=C["INK_HI"], relief="flat",
                           borderwidth=0, highlightthickness=0, padx=P(10), pady=P(4), insertbackground=C["INK"], spacing1=1, spacing3=1,
                           selectbackground=C["SEL"], selectforeground=C["INK"])
        self.log.tag_configure("now", foreground=C["INK"])
        self.log.bind("<Configure>", lambda _e: self.log.see("end-2c"), add="+")      # 카드가 접히고 펼쳐져 높이가 바뀌어도 마지막 줄이 보이게
        sb = ttk.Scrollbar(lf, orient="vertical", command=self.log.yview); self.log.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y"); self.log.pack(side="left", fill="both", expand=True)
        nb.add(lf, text="진행 기록")
        tf = tk.Frame(nb, bg=C["PANEL"])
        self.table = ttk.Treeview(tf, columns=("pdf", "print", "status", "unc", "figs"), show="headings", height=4)
        for c, t, w in (("pdf", "PDF 쪽", 70), ("print", "인쇄 쪽", 70), ("status", "상태", 560), ("unc", "확인 필요", 80), ("figs", "도판", 60)):
            self.table.heading(c, text=t); self.table.column(c, width=P(w), anchor="center" if c != "status" else "w", stretch=(c == "status"))
        self.table.tag_configure("odd", background=C["STRIPE"])
        sb = ttk.Scrollbar(tf, orient="vertical", command=self.table.yview); self.table.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y"); self.table.pack(side="left", fill="both", expand=True)
        nb.add(tf, text="쪽별 상태")
        return card

    def _sync_log_head(self, tab: int) -> None:
        """기록 카드 머리: 책갈피 OCR 에서는 [진행 기록]만(쪽별 상태 탭·고급 도구 숨김), 실행 중에는 고급 도구 옆 글이 까닭을 말한다."""
        d = vars(self)
        if d.get("_nb_tabs") is None:
            return
        collate = tab == 1
        page_tab = self._nb_tabs[1]
        if collate:
            if self.v_nb.get() == 1:
                self.v_nb.set(0); self.nb.select(0)
            self.nb.tab(1, state="hidden")
            for w in (page_tab, self.b_adv, self.l_adv_cap):
                w.pack_forget()
        else:
            self.nb.tab(1, state="normal")
            if not page_tab.winfo_manager():
                page_tab.pack(side="left", fill="y", padx=(0, self.px(2)))
            if not self.b_adv.winfo_manager():
                self.b_adv.pack(side="right", padx=(0, self.px(6)))
                self.l_adv_cap.pack(side="right", padx=(0, self.px(8)))
        self.l_adv_cap.configure(text="실행 중에는 고급 도구를 쓸 수 없습니다" if self.running else "단계만 다시 하기 · PDF 글자층 넣기 외")

    def _build_actions(self) -> None:
        """탭마다의 실행 자리(act_cards — select_tab 이 그 탭의 것만 보인다): 일반·이미지 OCR 은 오른쪽 단의 실행 카드(장부는 _work 의 카드),
        책갈피 OCR 은 오른쪽 '교정 실행' 카드, 설정 탭은 바닥 고정 줄(스크롤 밖 — [설정 저장](먹) [OCR로 돌아가기] · 알림 줄 · 오른쪽 끝 '시작할 때 업데이트 확인').
        설정 탭에서는 실행을 시작할 일이 없어 [■ 중지]를 두지 않는다(실행 중에는 탭을 옮길 수 없고, 고급 도구는 작업 탭에 있음)."""
        P = self.px
        act = self._build_run_column(self._run_slot)
        act2 = self._build_collate_run(self._collate_cols["run"])
        settings = tk.Frame(self._footer, bg=self.colors["PANEL"], padx=P(24), pady=P(12))
        self._btn(settings, "설정 저장", self.save_settings, style="FootInk.TButton",
                  tip="지금 설정을 저장합니다(실행할 때와 창을 닫을 때도 저절로 저장). 쪽 범위·인쇄 쪽수·읽기 방향·문서 짜임·양식·짜임 규칙·획 굵기는 "
                      "이 책의 결과 폴더(book_settings.json)에도 — 결과 폴더가 아직 없으면 실행할 때 남깁니다.").pack(side="left")
        self._btn(settings, "OCR로 돌아가기", self.return_to_ocr, style="Foot.TButton").pack(side="left", padx=(P(10), 0))
        self.v_auto_update = tk.BooleanVar(value=bool(self.cfg.get("auto_update", True)))
        au = ttk.Checkbutton(settings, text="시작할 때 업데이트 확인", variable=self.v_auto_update, command=self._save_update_preference)
        au.pack(side="right", padx=(P(16), 0))
        Tip(au, "새 판이 있으면 작업이 끝난 뒤 알립니다 · 지금 확인은 위의 [업데이트 확인]", self)
        self.v_notice = tk.StringVar(value=SETTINGS_NOTE)                    # 알림 줄(설정 탭에는 기록 카드가 없음 — 상태 글·마지막 기록 줄, _notice)
        self.l_notice = self._lab(settings, "", "CAP", "MUTED", wrap=True, textvariable=self.v_notice)
        self.l_notice.pack(side="left", fill="x", expand=True, padx=(P(16), 0))
        self._notice_tip = Tip(self.l_notice, "", self)                     # 줄인 경로의 전체 글
        self.v_status.trace_add("write", lambda *_: App._notice(self, self.v_status.get()))
        self.act_cards = [act, act2, settings]

    def _layout_work(self, *_):
        """두 단 ↔ 한 단: 캔버스 너비 ÷ 배율이 1200 이상이면 두 단(왼쪽 무게 1·최소 796 | 오른쪽 340 고정). 바뀌면 카드를 다시 놓는다(_place_cards)."""
        d = vars(self)
        if d.get("_work") is None:
            return
        try:
            width = self._canvas.winfo_width()
        except tk.TclError:
            return
        if width <= 1:
            return
        App._layout_settings(self, width)
        two = width / getattr(self, "scale", 1.0) >= 1200
        if two == d.get("_two_col"):
            return
        self._two_col = two
        App._place_cards(self)

    def _place_cards(self) -> None:
        """카드 놓기(명세 §6.1·§6.4). 일반·이미지 OCR — 두 단: 책·진행·고급 도구(펼쳤을 때)·기록 | 실행·장부(아래로 늘어남),
        한 단: 책 → 실행 → 진행 → 장부 → 고급 도구 → 기록(실행·중지가 위에 남게). 책갈피 OCR — 두 단: 교정할 문서·구획·기록 | 교정 실행·진행,
        한 단: 교정할 문서 → 교정 실행 → 구획 → 진행 → 기록. 카드는 탭 틀의 자식이라 열 틀에 grid(in_=…)로 옮긴다(위젯을 다시 만들지 않음).
        기록 카드(self.nb)는 하나 — 보이는 탭의 왼쪽 열 맨 아래에 두고 남은 높이를 채운다(설정 탭에서는 숨김)."""
        d = vars(self)
        if d.get("_prog_card") is None or d.get("_log_card") is None:
            return
        P = self.px
        two = d.get("_two_col", True)
        tab = d.get("_selected_tab", 0)
        adv = self._adv_card if self.v_adv.get() else None
        log = self._log_card
        if not adv:
            self._adv_card.grid_remove()
        work, cl = self._work, d.get("_collate_cols")
        if two:
            work.columnconfigure(0, weight=1, minsize=P(796)); work.columnconfigure(1, weight=0, minsize=P(340))
            self._work_right.grid(row=0, column=1, sticky="nsew", padx=(P(16), 0))
            left = [self._book_slot, self._prog_card, adv, log]
            right = [self._run_slot, self._led_card]
        else:
            work.columnconfigure(0, weight=1, minsize=0); work.columnconfigure(1, weight=0, minsize=0)
            self._work_right.grid_remove()
            left = [self._book_slot, self._run_slot, self._prog_card, self._led_card, adv, log]
            right = []
        work.rowconfigure(0, weight=1)
        self._work_left.grid(row=0, column=0, sticky="nsew")
        App._grid_column(self, self._work_left, left, log if tab in (0, 2) else None)
        App._grid_column(self, self._work_right, right, self._led_card if two else None)
        if cl:
            tab2, cleft, cright = cl["tab"], cl["left"], cl["right"]
            if two:
                tab2.columnconfigure(0, weight=1, minsize=P(796)); tab2.columnconfigure(1, weight=0, minsize=P(340))
                cright.grid(row=0, column=1, sticky="nsew", padx=(P(16), 0))
                left = [cl["doc"], cl["sections"], log]
                right = [cl["run"], cl["prog"]]
            else:
                tab2.columnconfigure(0, weight=1, minsize=0); tab2.columnconfigure(1, weight=0, minsize=0)
                cright.grid_remove()
                left = [cl["doc"], cl["run"], cl["sections"], cl["prog"], log]
                right = []
            tab2.rowconfigure(0, weight=1)
            cleft.grid(row=0, column=0, sticky="nsew")
            App._grid_column(self, cleft, left, log if tab == 1 else None)
            App._grid_column(self, cright, right, cl["prog"] if two else None)
        if tab == 3:
            log.grid_remove()
        App._sync_log_head(self, tab)
        self.after_idle(self._fit_host)

    def _grid_column(self, master, items: list, stretch=None) -> None:
        """한 열에 카드를 차례로(사이 14). stretch 카드가 남은 높이를 채운다(기록 카드는 최소 120). None 자리는 건너뛰고,
        기록 카드가 이 열의 것이 아니면(다른 탭) 놓지 않는다."""
        P = self.px
        for r in range(8):
            master.rowconfigure(r, weight=0, minsize=0)
        r = 0
        for w in items:
            if w is None:
                continue
            if w is vars(self).get("_log_card") and w is not stretch:
                continue
            w.grid(in_=master, row=r, column=0, sticky="nsew", pady=(P(14) if r else 0, 0))
            w.lift()
            if w is stretch:
                master.rowconfigure(r, weight=1, minsize=P(120) if w is vars(self).get("_log_card") else 0)
            r += 1
        master.columnconfigure(0, weight=1)

    def _update_scroll(self):
        """내용이 넘칠 때만 스크롤 막대를 오른쪽 여백 위에 띄운다(카드 너비는 그대로)."""
        cv = self._canvas
        if self._host.winfo_reqheight() > cv.winfo_height() + 1:
            self._vsb.place(relx=1.0, rely=0.0, relheight=1.0, anchor="ne")
        else:
            self._vsb.place_forget(); cv.yview_moveto(0)

    def _fit_host(self, *_):
        """본문 높이 = max(캔버스 높이, 내용이 바라는 높이): 창이 내용보다 크면 기록 카드(장부)가 남은 높이를 채우고, 작으면 스크롤한다.
        내용 높이는 카드가 접히거나 펼쳐질 때마다 바뀌므로 _poll 도 0.1초마다 부른다(값이 같으면 아무것도 하지 않음)."""
        d = vars(self)
        cv, host = d.get("_canvas"), d.get("_host")
        if cv is None or host is None:
            return
        try:
            ch, rh = cv.winfo_height(), host.winfo_reqheight()
            h = max(ch, rh)
            if (h, ch) != d.get("_host_h"):
                self._host_h = (h, ch)
                cv.itemconfigure(self._host_win, height=h)
                self._update_scroll()
        except tk.TclError:
            pass

    def _wheel(self, e):
        """마우스 휠: 포인터가 위 칸에 있고 내용이 넘칠 때만 위 칸을 굴린다(표·기록·목록은 제 휠을 쓴다)."""
        cv = self._canvas
        try:
            w = self.winfo_containing(e.x_root, e.y_root)
        except (KeyError, tk.TclError):
            return
        if w is None or not str(w).startswith(str(cv)) or self._host.winfo_reqheight() <= cv.winfo_height():
            return
        num = getattr(e, "num", 0)
        if num in (4, 5):
            n = -1 if num == 4 else 1                               # X11: 한 번에 한 칸
        elif sys.platform == "darwin":
            n = -int(e.delta) or (-1 if e.delta > 0 else 1)       # macOS: delta 가 작은 정수
        else:                                                     # Windows: 120 단위. 정밀 터치패드의 작은 delta 는 모아서
            self._wacc = getattr(self, "_wacc", 0) + int(e.delta)
            n = -int(self._wacc / 120); self._wacc -= -n * 120
            if not n:
                return
        if w.winfo_class() in ("Treeview", "Listbox", "Text"):
            try:
                top, bot = w.yview()
            except tk.TclError:
                return
            if (n < 0 and top > 0.0) or (n > 0 and bot < 1.0):
                return                                            # 표가 아직 굴러갈 수 있으면 표가 굴린다
        cv.yview_scroll(n * 2, "units")

    def _focus_into_view(self, e):
        """키보드(Tab)로 위 칸의 가려진 칸에 초점이 가면 그 칸이 보이게 굴린다."""
        w = e.widget
        try:
            cv = self._canvas
            if not isinstance(w, tk.Misc) or not str(w).startswith(str(self._host)) or self._host.winfo_reqheight() <= cv.winfo_height():
                return
            y0 = w.winfo_rooty() - self._host.winfo_rooty(); y1 = y0 + w.winfo_height()
            H = max(1, self._host.winfo_height()); top, bot = cv.yview()
            vis0, vis1 = top * H, bot * H
            if y0 < vis0:
                cv.yview_moveto(max(0.0, (y0 - self.px(12)) / H))
            elif y1 > vis1:
                cv.yview_moveto(min(1.0, (y1 + self.px(12) - (vis1 - vis0)) / H))
        except (tk.TclError, AttributeError):
            pass

    def _drop_mouse_focus(self, w):
        """마우스로 누른 단추의 초점을 그 단추가 든 창(대화 상자면 대화 상자)으로 돌린다 — 초점 고리는 Tab 으로 옮길 때만 보이게."""
        try:
            if self.focus_get() is w:
                w.winfo_toplevel().focus_set()
        except (KeyError, tk.TclError):
            pass

    def _reveal(self, w):
        """위 칸에서 w 가 다 보이게 굴린다(펼친 고급·세부 설정). 위 칸보다 크면 w 의 위쪽부터 보인다."""
        try:
            self.update_idletasks()
            App._fit_host(self); self.update_idletasks()               # 막 펼친 내용만큼 본문 높이부터 늘린다(그 전에는 늘어난 끝까지 굴러가지 않음)
            cv = self._canvas; H = max(1, self._host.winfo_height()); view = cv.winfo_height()
            if self._host.winfo_reqheight() <= view or not w.winfo_ismapped():
                return
            y0 = w.winfo_rooty() - self._host.winfo_rooty(); y1 = y0 + w.winfo_height()
            top, bot = cv.yview(); vis0, vis1 = top * H, bot * H
            if y1 + self.px(12) > vis1 or y0 < vis0:
                want = min(y0 - self.px(12), y1 + self.px(12) - view)
                cv.yview_moveto(max(0.0, min(1.0, want / H)) if y1 - y0 + self.px(24) <= view else max(0.0, (y0 - self.px(12)) / H))
        except (tk.TclError, AttributeError):
            pass

    # ---------- 책갈피 교정 탭 ----------
    def _build_collate_tab(self, tab):
        """책갈피 OCR 탭(명세 §6.4, F_collate): 왼쪽 '교정할 문서'·'구획 (책갈피·제목)'·기록 | 오른쪽 340 '교정 실행'·'진행'.
        카드는 탭 틀의 자식이고 _place_cards 가 열 틀에 grid(in_=…)로 놓는다(한 단이면 교정할 문서 → 교정 실행 → 구획 → 진행 → 기록)."""
        from .collate.labels import POLICIES, ANCHOR_SOURCES
        C, P, L = self.colors, self.px, self._lab
        self.v_hwpx = tk.StringVar(value=self.cfg.get("hwpx", "")); self.v_pdf2 = tk.StringVar(value=self.cfg.get("collate_pdf", "")); self.v_find = tk.StringVar()
        self.v_policy = tk.StringVar(value=POLICIES.get(self.cfg.get("collate_policy", "sure"), POLICIES["sure"]))
        self.v_cmemo = tk.StringVar(value=MEMO_LABELS.get(self.cfg.get("collate_memo", "notice"), MEMO_LABELS["notice"]))
        self.v_hstyles = tk.StringVar(value=self.cfg.get("heading_styles", ""))
        self.v_asrc = tk.StringVar(value=ANCHOR_SOURCES.get(self.cfg.get("anchor_source", "auto"), ANCHOR_SOURCES["auto"]))
        self.v_cforce = tk.BooleanVar(value=False)
        self.collate_rows: list[dict] = []
        cl = self._collate_cols = {"tab": tab, "left": ttk.Frame(tab), "right": ttk.Frame(tab)}    # 열 틀을 먼저(카드가 그 위에 보이게)
        c = cl["doc"] = ttk.Frame(tab, style="Card.TFrame", padding=(P(20), P(16)))
        b = tk.Frame(c, bg=C["PANEL"]); b.pack(fill="both", expand=True); b.columnconfigure(1, weight=1)
        L(b, "교정할 문서", "SECTION").grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, P(8)))
        rows = (("PDF", self.v_pdf2, "PDF 고르기…", self.pick_pdf2, "책갈피가 든 원본 PDF. 비우면 일반 OCR 탭의 PDF를 씁니다."),
                ("HWPX", self.v_hwpx, "HWPX 고르기…", self.pick_hwpx, "교정할 HWPX. 원본은 그대로 두고 <이름>_교정.hwpx 사본에 고칩니다."),
                ("책갈피로 쓸 스타일", self.v_hstyles, "스타일 고르기…", self.pick_styles, "비우면 이름에 '제목'이 든 스타일 전부. 쉼표로 여러 개, 앞에 적은 것이 큰 단위입니다."))
        lw = 0
        try:
            from tkinter import font as tkfont
            lw = max(tkfont.Font(self, font=self.fonts["BODY"]).measure(r[0]) for r in rows) + P(24)
        except tk.TclError:
            pass
        b.columnconfigure(0, minsize=max(lw, P(140)))
        for i, (lab, var, bt, cmd, tip) in enumerate(rows):
            r = 1 + 2 * i
            L(b, lab, "BODY", "MUTED").grid(row=r, column=0, sticky="w", pady=(P(4) if i else 0, 0))
            e = ttk.Entry(b, textvariable=var); e.grid(row=r, column=1, sticky="ew", pady=(P(4) if i else 0, 0)); Tip(e, tip, self)
            self._btn(b, bt, cmd).grid(row=r, column=2, sticky="ew", padx=(P(8), 0), pady=(P(4) if i else 0, 0))
            L(b, tip, "CAP", "MUTED", wrap=True).grid(row=r + 1, column=1, columnspan=2, sticky="ew", pady=(P(2), 0))

        g = cl["sections"] = ttk.Frame(tab, style="Card.TFrame", padding=(P(20), P(16)))
        gb = tk.Frame(g, bg=C["PANEL"]); gb.pack(fill="both", expand=True)
        L(gb, "구획 (책갈피·제목)", "SECTION").pack(anchor="w", pady=(0, P(8)))
        r3 = tk.Frame(gb, bg=C["PANEL"]); r3.pack(fill="x")
        self._btn(r3, "구획 색인", self.do_collate_index, tip="HWPX 의 책갈피·제목 스타일로 구획을 나누고 PDF 책갈피와 짝을 맞춥니다").pack(side="left")
        L(r3, "기준", "BODY", "MUTED").pack(side="left", padx=(P(14), P(6)))
        e = ttk.Entry(r3, textvariable=self.v_find, width=16); e.pack(side="right"); e.bind("<KeyRelease>", lambda _e: self.refresh_collate_table())
        L(r3, "찾기(한자·독음)", "BODY", "MUTED").pack(side="right", padx=(P(14), P(6)))
        ttk.Combobox(r3, textvariable=self.v_asrc, values=list(ANCHOR_SOURCES.values()), state="readonly", width=self._cw(ANCHOR_SOURCES.values())).pack(
            side="left", fill="x", expand=True)
        Tip(e, "한자뿐 아니라 한글 독음으로도 찾습니다(대계 → 大薊, 인삼 → 人參·人蔘).", self)
        self.v_cstat = tk.StringVar(value="HWPX 를 고르고 [구획 색인]을 누르십시오.")
        self._lab(gb, "", "CAP", "MUTED", wrap=True, textvariable=self.v_cstat).pack(fill="x", pady=(P(8), P(6)))
        tf = tk.Frame(gb, bg=C["LINE"], padx=max(1, P(1)), pady=max(1, P(1))); tf.pack(fill="both", expand=True)
        cols = ("no", "lv", "name", "read", "para", "pdf", "page", "how")
        self.ctable = ttk.Treeview(tf, columns=cols, show="headings", height=5, selectmode="browse")
        for cc, t, w, an in (("no", "번호", 54, "e"), ("lv", "급", 40, "center"), ("name", "HWPX 책갈피·제목", 220, "w"), ("read", "독음", 120, "w"), ("para", "문단", 64, "center"),
                             ("pdf", "PDF 책갈피", 180, "w"), ("page", "PDF 쪽", 66, "center"), ("how", "짝", 74, "center")):
            self.ctable.heading(cc, text=t); self.ctable.column(cc, width=P(w), minwidth=P(min(w, 60)), anchor=an, stretch=(cc in ("name", "pdf", "read")))
        self.ctable.tag_configure("odd", background=self.colors["STRIPE"])
        sb = ttk.Scrollbar(tf, orient="vertical", command=self.ctable.yview); self.ctable.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y"); self.ctable.pack(side="left", fill="both", expand=True)
        self.ctable.bind("<Double-1>", lambda _e: self.do_collate(apply=False))
        self._ctable_empty = tk.Label(self.ctable, text="구획 색인 전 — 색인하면 구획마다 한 줄씩 나옵니다(두 번 누르면 그 구획만 대조)",
                                      font=self.fonts["CAP"], fg=C["MUTED"], bg=C["FIELD"], bd=0)
        self._ctable_empty.place(relx=0.5, rely=0.55, anchor="center")
        r4 = tk.Frame(gb, bg=C["PANEL"]); r4.pack(fill="x", pady=(P(10), 0))
        b_fix = self._btn(r4, "이 구획 교정", lambda: self.do_collate(apply=True), tip="표에서 고른 구획만 대조해 교정본에 고쳐 넣습니다"); b_fix.pack(side="right")
        b_chk = self._btn(r4, "이 구획 대조만", lambda: self.do_collate(apply=False), tip="표에서 고른 구획만 대조해 보고를 만듭니다(문서는 고치지 않음). 표에서 두 번 눌러도 됩니다")
        b_chk.pack(side="right", padx=(0, P(6)))
        self._csec_btns = (b_chk, b_fix)                                    # 표에서 구획을 고른 때만 켠다(명세 §6.4 · F_collate)
        self.ctable.bind("<<TreeviewSelect>>", lambda _e: App._sync_collate_pick(self), add="+")
        App._sync_collate_pick(self)
        L(r4, "표에서 고른 구획:", "BODY", "MUTED").pack(side="right", padx=(0, P(8)))
        L(gb, "기록이 없는 쪽은 모델로 읽고, 入/人 같은 자형 편향 자리는 글자 조각을 모델에 다시 물어 가립니다. "
              "자동 교정은 초록(3차 자형 판별 근거)·파랑(판독 일치·사전·반복·글자층 근거, 확인 권장), 확인 요청은 빨강으로 칠합니다. "
              "메모는 지운 자리·넣지 않은 자리·구조 차이·반영하지 않은 제안에만 답니다(원래 글자와 근거 전체는 보고 화면). 이전처럼 모든 교정에 메모를 원하면 '모든 표시에 메모'.",
          "CAP", "MUTED", wrap=True).pack(fill="x", pady=(P(10), 0))
        cl["run"] = ttk.Frame(tab)                                          # '교정 실행' 카드 자리(select_tab 이 act_cards[1] 을 넣고 뺀다)
        cl["prog"] = self._build_collate_progress(tab)

    def _build_collate_run(self, parent) -> ttk.Frame:
        """책갈피 OCR 오른쪽 '교정 실행' 카드: 판독기·모델 · 읽기 방향·교정 정책·메모 · [▶ HWPX 전체 교정](인주 — 기록이 없는 쪽은 모델로 읽음) ·
        [■ 중지] · 캡션 · [보고 열기][교정본 열기](열기만 함 — 실행 중에도 눌림)."""
        C, P, L = self.colors, self.px, self._lab
        card = ttk.Frame(parent, style="Card.TFrame", padding=(P(18), P(16)))
        b = tk.Frame(card, bg=C["PANEL"]); b.pack(fill="both", expand=True); b.columnconfigure(0, weight=1)
        head = tk.Frame(b, bg=C["PANEL"]); head.grid(row=0, column=0, sticky="ew")
        L(head, "교정 실행", "SECTION").pack(side="left")
        self._btn(head, "설정 바꾸기", lambda: self.select_tab(3), style="Link.TButton",
                  tip="판독기·모델·속도·읽기 방향과 교정 정책·메모는 설정 탭에서 고릅니다.").pack(side="right")
        self.l_crun_summary = L(b, "", "BODY_B", wrap=True); self.l_crun_summary.grid(row=1, column=0, sticky="ew", pady=(P(6), 0))
        self.l_crun_opts = L(b, "", "CAP", "MUTED", wrap=True); self.l_crun_opts.grid(row=2, column=0, sticky="ew", pady=(P(2), 0))
        self._btn(b, "▶  HWPX 전체 교정", self.do_collate_all, style="Seal.TButton",
                  tip="구획을 차례로 전부 대조해 HWPX 사본(<이름>_교정.hwpx) 하나에 고쳐 넣고 요약(_전체_요약.html)을 남깁니다").grid(row=3, column=0, sticky="ew", pady=(P(10), 0))
        self.b_stop2 = ttk.Button(b, text="■  중지", command=self.do_stop, state="disabled", style="Stop.TButton", cursor="hand2")
        self.b_stop2.grid(row=4, column=0, sticky="ew", pady=(P(6), 0))
        Tip(self.b_stop2, "지금 구획이 끝나면 멈춥니다. 고친 내용은 교정본에 남습니다.", self)
        self.t_crun_status = App._rich_line(self, b)                       # 중지를 누른 뒤·끝난 뒤만 — v_status
        L(b, "원본 HWPX 는 건드리지 않고, 고친 사본 하나에 구획을 차례로 쌓습니다. 멈추면 지금 구획이 끝난 뒤 멈추고, 고친 내용은 교정본에 남습니다.",
          "CAP", "MUTED", wrap=True).grid(row=6, column=0, sticky="ew", pady=(P(8), 0))
        opens = tk.Frame(b, bg=C["PANEL"]); opens.grid(row=7, column=0, sticky="ew", pady=(P(8), 0))
        opens.columnconfigure(0, weight=1, uniform="copen"); opens.columnconfigure(1, weight=1, uniform="copen")
        for k, (text, cmd, tip) in enumerate((("보고 열기", self.open_collate_report, "가장 최근 대조 보고(구획별 HTML)를 엽니다 — 열기만 하므로 실행 중에도 눌립니다"),
                                              ("교정본 열기", self.open_collated, "고친 사본(<이름>_교정.hwpx)을 한컴오피스로 엽니다 — 실행 중에는 읽기용 사본을 엽니다(교정본을 계속 고쳐 쓰므로)"))):
            btn = self._btn(opens, text, cmd, lock=False, tip=tip)
            btn.configure(padding=(P(6), P(4)))
            btn.grid(row=0, column=k, sticky="ew", padx=(0, P(3)) if k == 0 else (P(3), 0))
        return card

    def _build_collate_progress(self, parent) -> ttk.Frame:
        """책갈피 OCR 오른쪽 '진행' 카드(작게): 알약 · 전체 % · 막대 · '구획 대조 n / m구획' · 지난·남은 시간 · 이번 명령 호출 · 바닥 캡션(호출 듦 안내)."""
        C, P, L = self.colors, self.px, self._lab
        card = ttk.Frame(parent, style="Card.TFrame", padding=(P(18), P(16)))
        b = tk.Frame(card, bg=C["PANEL"]); b.pack(fill="both", expand=True)
        head = tk.Frame(b, bg=C["PANEL"]); head.pack(fill="x")
        L(head, "진행", "SECTION").pack(side="left")
        self.l_cpill = ttk.Label(head, text="준비됨", style="Pillidle.TLabel"); self.l_cpill.pack(side="left", padx=(P(10), 0))
        self.l_cpct = L(head, "전체 0%", "BODY_B"); self.l_cpct.pack(side="right")
        self.progress2 = ttk.Progressbar(b, style="Panel.Bar.Horizontal.TProgressbar", mode="determinate", maximum=1000, value=0)
        self.progress2.pack(fill="x", pady=(P(10), 0))
        self.l_cline = L(b, "", "BODY", "MUTED", wrap=True); self.l_cline.pack(fill="x", pady=(P(12), 0))
        self.l_ctime = L(b, "", "CAP", "MUTED", wrap=True); self.l_ctime.pack(fill="x", pady=(P(6), 0))
        L(b, "[▶ HWPX 전체 교정]은 기록이 없는 쪽을 모델로 읽고 자형 판별을 묻습니다(호출 듦). 판독기·속도·읽기 방향과 교정 정책은 [설정] 탭에서 고릅니다.",
          "CAP", "MUTED", wrap=True).pack(side="bottom", fill="x", pady=(P(12), 0))
        return card

    def _build_collate_settings(self, parent):
        """설정 탭 왼쪽 열 끝의 접힌 '책갈피 OCR 교정 ▸' 섹션(펼침은 저장하지 않음): 교정 정책 · 메모 · 책갈피 OCR: 기록 있는 쪽도 다시 판독."""
        from .collate.labels import POLICIES
        C, P = self.colors, self.px
        card = ttk.Frame(parent, style="Card.TFrame", padding=(P(20), P(8), P(20), P(8)))
        head = tk.Frame(card, bg=C["PANEL"]); head.pack(fill="x")
        self.v_collate_set = tk.BooleanVar(value=False)
        self.b_collate_set = ttk.Button(head, text="책갈피 OCR 교정 ▸", style="SecToggle.TButton", cursor="hand2", command=self.toggle_collate_settings)
        self.b_collate_set.pack(side="left")
        self._lab(head, "교정 정책 · 메모 · 책갈피 OCR: 기록 있는 쪽도 다시 판독", "CAP", "MUTED", wrap=True).pack(side="left", fill="x", expand=True, padx=(P(8), 0))
        body = tk.Frame(card, bg=C["PANEL"])
        self._set_sections["collate"] = {"card": card, "head": head, "body": body}
        R, row = self._set_rows, self._setting_row
        R["policy"] = row(body, "교정 정책", "HWPX 글자를 얼마나 고칠지 — 메모만 · 확실한 것만(A) · 전부(A·B2)",
                          lambda p: ttk.Combobox(p, textvariable=self.v_policy, values=list(POLICIES.values()), state="readonly", width=self._cw(POLICIES.values())))

        def memo(p):
            cm = ttk.Combobox(p, textvariable=self.v_cmemo, values=list(MEMO_LABELS.values()), state="readonly", width=self._cw(MEMO_LABELS.values()))
            Tip(cm, "고지 사항만(기본): 지운 글자·폭 없는 표시·구조 차이·반영하지 않은 제안에만 메모를 답니다. 자동 교정은 초록·파랑, 확인 요청은 빨강으로 칠하고, "
                    "원래 글자와 근거 전체는 보고(collate 폴더의 html)에 남습니다. 모든 항목에 메모를 원하면 '모든 표시에 메모'.", self)
            return cm
        R["cmemo"] = row(body, "메모", "원래 글자와 근거 전체는 보고(collate 폴더의 html)에", memo)
        R["cforce"] = row(body, "책갈피 OCR: 기록 있는 쪽도 다시 판독", "켜면 판독 기록이 있는 쪽도 다시 삼", lambda p: ttk.Checkbutton(p, variable=self.v_cforce, style="Bare.TCheckbutton"),
                          receipt=True)

    def pick_pdf2(self):
        p = filedialog.askopenfilename(title="책갈피가 든 원본 PDF", filetypes=[("PDF", "*.pdf"), ("모든 파일", "*.*")])
        if p:
            self.v_pdf2.set(p); self.cfg["collate_pdf"] = p; write_config(self)

    def pdf2(self) -> str:
        """교정 탭의 PDF. 비어 있으면 일반 OCR의 PDF를 쓴다(이미지 탭과 무관)."""
        return self.v_pdf2.get().strip() or self._input_vars[0]["pdf"].get().strip()

    def work2(self) -> Path:
        return work_dir_for(self.pdf2())

    def pick_hwpx(self):
        p = filedialog.askopenfilename(title="교정할 HWPX", filetypes=[("HWPX", "*.hwpx"), ("모든 파일", "*.*")])
        if p:
            self.v_hwpx.set(p); self.cfg["hwpx"] = p; write_config(self)
            self.suggest_styles()

    def suggest_styles(self):
        """HWPX 를 고르면 제목으로 보이는 스타일을 뒤에서 읽어 칸을 채운다(비어 있을 때만)."""
        h = self.v_hwpx.get().strip()
        if not h or self.v_hstyles.get().strip():
            return
        def t():
            try:
                from .collate.sections import HwpxBook
                book = HwpxBook(h)
                with contextlib.closing(book.doc):
                    names = book.heading_style_names()
                self.q.put(("styles_suggest", ", ".join(names)))
            except Exception:
                pass
        threading.Thread(target=t, daemon=True).start()

    def pick_styles(self):
        h = self.v_hwpx.get().strip()
        if not h or not Path(h).exists():
            messagebox.showinfo(APP_NAME, "먼저 HWPX 를 고르십시오."); return
        try:
            from .collate.sections import HwpxBook
            book = HwpxBook(h)
            with contextlib.closing(book.doc):
                counts = book.style_counts()
        except Exception as e:
            messagebox.showerror(APP_NAME, f"스타일을 읽지 못했습니다: {e}"); return
        StylePicker(self, counts)

    def set_heading_styles(self, names: list[str]):
        self.v_hstyles.set(", ".join(names)); self.cfg["heading_styles"] = self.v_hstyles.get(); write_config(self)

    def heading_styles_list(self) -> list[str]:
        return [s.strip() for s in self.v_hstyles.get().split(",") if s.strip()]

    def anchor_source(self) -> str:
        from .collate.labels import ANCHOR_SOURCES
        return next((k for k, v in ANCHOR_SOURCES.items() if v == self.v_asrc.get()), "auto")

    def collate_policy(self) -> str:
        from .collate.labels import POLICIES
        return next((k for k, v in POLICIES.items() if v == self.v_policy.get()), "sure")

    def _need_hwpx(self) -> bool:
        if not self.pdf2() or not Path(self.pdf2()).exists():
            messagebox.showinfo(APP_NAME, "책갈피가 든 원본 PDF 를 먼저 고르십시오."); return False
        if not self.v_hwpx.get().strip() or not Path(self.v_hwpx.get().strip()).exists():
            messagebox.showinfo(APP_NAME, "교정할 HWPX 파일을 먼저 고르십시오."); return False
        return True

    def do_collate_index(self):
        if not self._need_hwpx():
            return
        pdf, hwpx, src = self.pdf2(), self.v_hwpx.get().strip(), self.anchor_source()
        styles = self.heading_styles_list() or None
        self.cfg["anchor_source"] = src; self.cfg["heading_styles"] = self.v_hstyles.get(); write_config(self)
        self.v_cstat.set("색인 중…")
        def t():
            try:
                from .collate.sections import HwpxBook
                from .collate.run import index_both
                from .collate.pdf_outline import HOW_LABELS as _HOW
                from .pdfio import open_pdf
                with open_pdf(pdf) as doc:
                    book = HwpxBook(hwpx)
                    with contextlib.closing(book.doc):
                        anchors, pdf_anchors, matches = index_both(doc, book, src, styles)
                from .collate import reading as _rd
                rows = [{"no": i + 1, "lv": a.level, "name": a.name, "read": _rd.reading(a.name), "para": a.para, "pdf": m.pdf.title if m.pdf else "", "page": m.pdf.page if m.pdf else "",
                         "how": _HOW.get(m.how, m.how) + (f" {m.score:.2f}" if m.how in ("fuzzy", "contains") else "")} for i, (a, m) in enumerate(zip(anchors, matches))]
                kinds = {"bookmark": "책갈피", "heading": "제목 스타일", "pdf": "PDF 책갈피로 본문 검색"}
                basis = ", ".join(f"{kinds[k]} {n}" for k in ("bookmark", "heading", "pdf") if (n := sum(1 for a in anchors if a.kind == k)))
                matched = sum(1 for m in matches if m.pdf)
                msg = (f"HWPX 기준 {len(anchors)}개({basis}) · PDF 책갈피 {len(pdf_anchors)}개 · 짝 맞음 {matched}개" if anchors
                       else f"구획 기준을 찾지 못했습니다(PDF 책갈피 {len(pdf_anchors)}개). 기준을 'PDF 책갈피 이름으로 HWPX 본문 검색'으로 바꿔 보거나, 한컴에서 제목 스타일·책갈피를 넣으십시오.")
                from .collate import zh as _zh, reading as _rd2
                lines = [f"\n▶ 구획 색인\n   {msg}"]
                if not _zh.available():
                    lines.append("   ⚠ 간체·번체 변환 패키지(opencc)가 없어 PDF 책갈피(간체)와 HWPX 제목(번체)이 맞지 않습니다. [일반 OCR] 기록 카드의 [고급 도구 ▸] — [패키지 설치·점검] 뒤 프로그램을 다시 시작하십시오.")
                if not _rd2.available():
                    lines.append("   ⚠ 독음 변환 패키지(hanja)가 없어 한글 발음으로 찾기가 되지 않습니다. 같은 방법으로 설치하십시오.")
                if pdf_anchors:
                    lines.append("   PDF 책갈피 표본: " + " | ".join(f"{p.title}({p.page}쪽)" for p in pdf_anchors[:12]) + (" | …" if len(pdf_anchors) > 12 else ""))
                    if anchors and matched == 0:
                        lines.append("   HWPX 제목 표본: " + " | ".join(a.name[:20] for a in anchors[:12]))
                    unmatched = [p for p in pdf_anchors if all(m.pdf is not p for m in matches)]
                    if anchors and 0 < len(unmatched) < len(pdf_anchors):
                        lines.append(f"   짝 못 찾은 PDF 책갈피 {len(unmatched)}개(앞 10개): " + " | ".join(p.title for p in unmatched[:10]))
                self.q.put(("log", "\n".join(lines) + "\n"))
                self.q.put(("sections", rows, msg))
            except Exception as e:
                self.q.put(("sections", [], f"색인 실패: {e}"))
        threading.Thread(target=t, daemon=True).start()

    def refresh_collate_table(self):
        from .collate import zh, reading
        from .collate.sections import normalize_name
        raw = self.v_find.get().strip(); q = zh.compare_key(normalize_name(raw))
        for i in self.ctable.get_children():
            self.ctable.delete(i)
        k = 0
        for r in self.collate_rows:
            if q and q not in zh.compare_key(normalize_name(r["name"])) and q not in zh.compare_key(normalize_name(r["pdf"])) and not reading.matches(r["name"], raw):
                continue
            k += 1
            self.ctable.insert("", "end", iid=str(r["no"]), values=(r["no"], r["lv"], ("    " * (r["lv"] - 1)) + r["name"], r.get("read", ""), r["para"], r["pdf"], r["page"], r["how"]),
                               tags=("odd",) if k % 2 == 0 else ())
        App._sync_collate_pick(self)                                    # 표를 다시 채우면 고른 줄이 없어진다
        empty = vars(self).get("_ctable_empty")                        # 빈 표 안내(색인 전 · 찾는 구획 없음)
        if empty is not None:
            if k:
                empty.place_forget()
            else:
                empty.configure(text="찾는 구획이 없습니다 — 찾기 칸을 비우면 모두 나옵니다" if self.collate_rows else
                                "구획 색인 전 — 색인하면 구획마다 한 줄씩 나옵니다(두 번 누르면 그 구획만 대조)")
                empty.place(relx=0.5, rely=0.55, anchor="center")

    def _sync_collate_pick(self) -> None:
        """[이 구획 대조만]·[이 구획 교정]은 표에서 구획을 고른 때만 켠다 — 고르지 않고 누르면 아무것도 하지 않던 단추가 눌릴 것처럼 보이지 않게.
        실행 중에는 _set_running 의 잠금이 먼저(끝나 self.buttons 를 다시 켠 뒤에도 부른다)."""
        btns = vars(self).get("_csec_btns")
        if not btns:
            return
        try:
            on = bool(self.ctable.selection()) and not vars(self).get("running")
        except tk.TclError:
            return
        for b in btns:
            b.configure(state="normal" if on else "disabled")

    def _selected_anchor(self) -> int | None:
        sel = self.ctable.selection()
        if not sel:
            messagebox.showinfo(APP_NAME, "표에서 구획(책갈피·제목)을 하나 고르십시오."); return None
        return int(sel[0])

    def do_collate(self, apply: bool):
        if not self._need_hwpx() or not self._prepare_env():
            return
        idx = self._selected_anchor()
        if idx is None:
            return
        row = next(r for r in self.collate_rows if r["no"] == idx)
        if not row["pdf"]:
            from .collate import zh
            from .collate.pdf_outline import find_title_pages
            from .pdfio import open_pdf
            guess = None
            try:
                nxt = next((r2["name"] for r2 in self.collate_rows if r2["no"] > idx and r2["lv"] <= row["lv"]), None)
                with open_pdf(self.pdf2()) as doc:
                    guess = find_title_pages(doc, row["name"], nxt)
            except Exception:
                guess = None
            msg = f"「{row['name']}」 에 맞는 PDF 책갈피가 없습니다.\n"
            if not zh.available():
                msg += "⚠ 간체·번체 변환 패키지(opencc)가 없어 책갈피 짝을 못 찾습니다. [패키지 설치·점검] 뒤 다시 시작하십시오.\n"
            msg += (f"PDF 글자층에서 제목을 찾았습니다: {guess[0]}~{guess[1]}쪽. 맞으면 그대로 [OK].\n" if guess else "PDF 글자층에서도 제목을 찾지 못했습니다.\n")
            msg += "PDF 쪽 범위 (예 120-123):"
            pages = simpledialog.askstring(APP_NAME, msg, parent=self, initialvalue=(f"{guess[0]}-{guess[1]}" if guess else ""))
            if not pages:
                return
            extra = ["--pdf-pages", pages.strip()]
        else:
            extra = []
        argv = ["collate", self.pdf2(), "--work", str(self.work2()), "--hwpx", self.v_hwpx.get().strip(), "--anchor-index", str(idx),
                "--anchor-source", self.anchor_source(), "--policy", self.collate_policy()] + (["--heading-styles", ",".join(self.heading_styles_list())] if self.heading_styles_list() else []) + extra + self._read_opts(print_offset=False, force=self.v_cforce.get())
        if apply: argv.append("--apply")
        argv += ["--memo", self.collate_memo_mode()] + self._glyph_opts()
        self.cfg.update({"hwpx": self.v_hwpx.get().strip(), "collate_pdf": self.v_pdf2.get().strip(), "collate_policy": self.collate_policy(), "collate_memo": self.collate_memo_mode()}); write_config(self)
        self.nb.select(0)
        self.start(argv, ("교정 반영·저장" if apply else "대조") + f" — {row['name'][:20]}")

    def do_collate_all(self):
        """구획 전체를 차례로 대조·교정한다. 색인이 아직 없으면 먼저 색인한 뒤 다시 누르게 한다."""
        if not self._need_hwpx() or not self._prepare_env():
            return
        if self.collate_rows:
            matched = sum(1 for r in self.collate_rows if r["pdf"])
            head = f"구획 {len(self.collate_rows)}개(PDF 책갈피 짝 {matched}개)를 차례로 대조해 교정본 하나에 고쳐 넣습니다.\n"
        else:                                           # 색인을 따로 누르지 않아도 된다 — 명령이 스스로 색인한다
            head = "HWPX 의 책갈피·제목 스타일로 구획을 나눈 뒤(색인은 저절로) 차례로 전부 대조해 교정본 하나에 고쳐 넣습니다.\n"
        if not messagebox.askyesno(APP_NAME, head + "짝이 없는 구획은 PDF 글자층에서 제목을 찾아 보고, 못 찾으면 건너뛰어 요약에 적습니다.\n"
                                             "판독 기록이 없는 쪽은 모델로 읽으므로(쪽당 30초~1분) 시간과 비용이 듭니다. [■ 중지]로 멈출 수 있습니다.\n\n시작할까요?"):
            return
        argv = ["collate", self.pdf2(), "--work", str(self.work2()), "--hwpx", self.v_hwpx.get().strip(), "--all", "--apply",
                "--anchor-source", self.anchor_source(), "--policy", self.collate_policy()] + (["--heading-styles", ",".join(self.heading_styles_list())] if self.heading_styles_list() else []) \
               + self._read_opts(print_offset=False, force=self.v_cforce.get())
        argv += ["--memo", self.collate_memo_mode()] + self._glyph_opts()
        self.cfg.update({"hwpx": self.v_hwpx.get().strip(), "collate_pdf": self.v_pdf2.get().strip(), "collate_policy": self.collate_policy(), "collate_memo": self.collate_memo_mode()}); write_config(self)
        self.nb.select(0)
        self.start(argv, "HWPX 전체 교정", open_after=self.work2() / "collate" / "_전체_요약.html")

    def _latest(self, folder: Path, pattern: str) -> Path | None:
        files = sorted(folder.glob(pattern), key=lambda p: p.stat().st_mtime) if folder.exists() else []
        return files[-1] if files else None

    def open_collate_report(self):
        if not self.pdf2(): return
        f = self._latest(self.work2() / "collate", "*.html")
        if f: open_path(f)
        else: messagebox.showinfo(APP_NAME, "아직 대조 보고가 없습니다. [▶ HWPX 전체 교정] 을 누르거나, 구획을 고르고 [이 구획 대조만]·[이 구획 교정] 을 누르십시오.")

    def open_collated(self):
        h = self.v_hwpx.get().strip()
        if not h: return
        original = Path(h)
        stem = original.stem if original.stem.endswith("_교정") else original.stem + "_교정"
        out = safe_output_path(original.with_name(stem + ".hwpx"), original)
        if not out.exists():
            messagebox.showinfo(APP_NAME, f"교정본이 아직 없습니다: {out.name}"); return
        try:
            App._open_result(self, out)         # 실행 중([▶ HWPX 전체 교정])이면 읽기용 사본 — 한컴이 교정본을 쥐면 다음 구획 저장이 막혀 전체 교정이 멈춘다
        except OSError as exc:
            messagebox.showerror(APP_NAME, f"교정본을 열지 못했습니다.\n{out}\n{exc}")

    def _btn(self, parent, text, cmd, tip: str | None = None, lock: bool = True, **kw) -> ttk.Button:
        """작업 중에는 잠기는 단추(lock=False 면 잠그지 않음 — 원본 보기·결과 열기처럼 열기만 하는 단추). 너비는 글자에 맞춘다(고정 너비로 글자가 잘리던 문제).
        tip 은 마우스를 올리면 보이는 설명."""
        kw.pop("width", None)
        b = ttk.Button(parent, text=text, command=cmd, cursor="hand2", **kw)
        if lock:
            self.buttons.append(b)
        if tip:
            Tip(b, tip, self)
        return b

    # ---------- 도우미 ----------
    def open_local_comparison(self):
        """Open the independent, source-preserving editor for the active input."""
        if self.running or getattr(self, "_closing", False):
            return
        from .comparison_editor import ComparisonEditor
        from ab_ocr.runtime import default_engine_root
        source = self.pdf2() if getattr(self, "_selected_tab", 0) == 1 else self.pdf()
        mode = self.mode_key(0 if getattr(self, "_selected_tab", 0) == 1 else None)
        windows = vars(self).setdefault("_comparison_windows", [])
        windows[:] = [window for window in windows if window.winfo_exists()]
        for window in windows:
            active = getattr(getattr(window, "project", None), "source_path", None)
            if source and active and Path(source).resolve() == Path(active).resolve():
                window.deiconify(); window.lift(); window.focus_set()
                return
        window = ComparisonEditor(self, source_path=source or None,
            work_dir=work_dir_for(source) if source else None,
            engine_root=default_engine_root(vars(self).get("cfg", {}).get("local_engine_root")),
            on_engine_root=lambda path: App._remember_local_engine_root(self, path),
            on_ai_settings=lambda: App._comparison_ai_settings(self),
            profile="mixed" if mode == "vertical" else ("simplified" if mode == "horizontal_zh" else None),
            direction="vertical" if mode == "vertical" else ("horizontal" if mode == "horizontal_zh" else None))
        windows.append(window)

    def _comparison_ai_settings(self):
        """Snapshot only the currently selected provider on the owning UI thread."""
        from .providers import PROVIDERS
        reader = self.v_reader.get().strip()
        model = self.v_model.get().strip()
        if reader not in PROVIDERS or not model:
            raise ValueError("[설정]에서 AI 판독기와 이미지 입력을 지원하는 모델을 선택하십시오.")
        return {"provider": reader, "model": model, "api_key": self.v_key.get().strip()}

    def _remember_local_engine_root(self, path):
        self.cfg["local_engine_root"] = str(Path(path).expanduser().resolve())
        self.save_settings(quiet=True)

    def pdf(self) -> str:
        return self.v_pdf.get().strip()

    def work(self) -> Path:
        return work_dir_for(self.pdf())

    def _sync_output_location(self, *_):
        """결과 저장 칸(읽기 전용): out 폴더의 전체 경로. 원본이 없으면 비운다(이름표가 '원본을 고르면 정해짐')."""
        self.v_output_dir.set(str((self.work() / "out").resolve()) if self.pdf() else "")

    def _need_pdf(self) -> bool:
        if getattr(self, "_selected_tab", None) == 3 and not self.running:
            self.select_tab(self._active_input)  # 고급 도구의 원본과 그 원본용 읽기 방향을 함께 복원한다.
        if not self.pdf() or not Path(self.pdf()).exists():
            messagebox.showinfo(APP_NAME, "일반 OCR의 [PDF 고르기…] 또는 이미지 OCR의 [이미지 고르기…]·[이미지 붙여넣기]로 원본을 불러오십시오."); return False
        return True

    def _pages(self) -> list[str]:
        p = self.v_pages.get().strip()
        return ["--pages", p] if p else []

    @staticmethod
    def _n_records(pdf: str) -> int:
        try:
            return len(list((work_dir_for(pdf) / "readings").glob("p*.json"))) if pdf else 0
        except Exception:
            return 0

    def _need_records(self, what: str) -> bool:
        """판독 기록을 쓰는 도구(비교 다시 계산·3차 판별·장부·보고·검수 화면) 앞에서: 일반 OCR 의 PDF 에 기록이 없고 책갈피 OCR 의 PDF 에 있으면 그 PDF 로 바꿔 준다.
        (대조용 복사본 PDF 를 일반 OCR 에 두면 결과 폴더가 비어 '판독 기록이 없습니다' 가 나던 문제)"""
        if not self._need_pdf():
            return False
        n1 = self._n_records(self.pdf())
        if n1:
            return True
        other = self.v_pdf2.get().strip() if hasattr(self, "v_pdf2") else ""
        n2 = self._n_records(other) if other and other != self.pdf() else 0
        if n2:
            if messagebox.askyesno(APP_NAME, f"[일반 OCR] 의 PDF 에는 판독 기록이 없습니다.\n  {self.pdf()}\n  → 결과 폴더 {self.work()}\n\n"
                                             f"[책갈피 OCR] 의 PDF 에는 {n2}쪽 기록이 있습니다.\n  {other}\n\n{what}을(를) 그 PDF 로 할까요? ([일반 OCR] 의 PDF 도 그것으로 바뀝니다)"):
                self.select_tab(0); self._use_pdf(other)
                return True
            return False
        messagebox.showinfo(APP_NAME, f"판독 기록이 없습니다.\n결과 폴더: {self.work()}\n\n먼저 [▶ 한 번에 인식] 을 하거나, "
                                      f"기록이 있는 원본을 [PDF 고르기…]나 [이미지 고르기…]로 고르십시오(복사본 PDF 옆에는 빈 결과 폴더가 새로 생깁니다).")
        return False

    def _read_opts(self, print_offset: bool = True, force: bool | None = None) -> list[str]:
        o = ["--reader", self.v_reader.get(), "--upscale", self.v_up.get().strip() or "2"]
        if self.v_reader.get() != "mock" and self.v_model.get().strip():
            o += ["--model", self.v_model.get().strip()]
        if not self.v_second.get():
            o.append("--no-second-pass")
        off = self.v_off.get().strip()
        if print_offset and off and off.lstrip("+-").isdigit() and int(off) != 0:
            o += ["--print-offset", off]          # 비우거나 0 이면 자동 판정. 책갈피 OCR 은 다른 책이라 인식 탭의 보정값을 쓰지 않는다
        if (self.v_force.get() if force is None else force):
            o.append("--force")
        o += ["--workers", (self.v_workers.get().strip() or "8")]
        o += ["--second-model", self.v_model2.get().strip()]          # 빈 값이면 1차와 같은 모델, auto 면 자동
        o += ["--second-thinking", "low" if self.v_think2.get() else "full"]
        o += ["--chunk-lines", (self.v_chunk.get().strip() or "0"), "--media-resolution", self.media_res_key()]
        o += ["--mode", self.mode_key()]
        if not self.v_cache.get():
            o.append("--no-cache")
        o += ["--checkpoint", (self.v_ckpt.get().strip() or "0")]
        o += ["--page-thinking", self.page_think_key(), "--page-media-resolution", self.page_mres_key()] + self._cap_opts()
        return o

    def _cap_opts(self) -> list[str]:
        """호출 상한(read·run·adjudicate·collate). 숫자가 아니면 _prepare_env 가 실행 전에 막는다."""
        n = max_calls_value(self.v_maxcalls.get()) or 0
        return ["--max-calls", str(n)] if n else []

    def _answered_opts(self) -> list[str]:
        """이미 물어 결론 난 3차 자리·문맥 교정 후보를 다시 묻지 않을 범위(run·adjudicate·build — collate 에는 없음). 늘 넘긴다(명령 쪽 기본값이 바뀌어도 창의 선택 그대로)."""
        return ["--skip-answered", "any-model" if self.v_skipany.get() else "same-model"]

    def _glyph_opts(self, suspects: bool = False) -> list[str]:
        """3차 판별 선택(run·adjudicate·collate 에만 — read 명령에는 없는 선택이다). suspects=True(run·adjudicate)면 합의 판독 의심을 끈 것도 넘긴다
        (collate 에는 그 선택이 없다)."""
        v = getattr(self, "v_suspects", None)
        return (["--adjudicate-blind"] if self.v_blind.get() else []) + (["--no-suspects"] if suspects and v is not None and not v.get() else [])

    def _build_opts(self) -> list[str]:
        o = []
        if self.v_docx.get(): o.append("--docx")
        if getattr(self, "v_tlpdf", None) is not None and self.v_tlpdf.get(): o.append("--textlayer-pdf")
        if getattr(self, "v_layout", None) is not None:
            o += ["--layout", self.layout_key()]                         # 늘 넘긴다(명령 쪽 기본값이 바뀌어도 창의 선택 그대로)
            if self.layout_key() == "flow":
                o += ["--preset", App.preset_ref(self)]
                tpl = (self.v_template.get() if getattr(self, "v_template", None) is not None else "").strip()
                if tpl:
                    o += ["--template", tpl]
                if getattr(self, "v_bold", None) is not None and not self.v_bold.get():
                    o.append("--no-bold")
        if not self.v_join.get(): o.append("--no-join-pages")
        if not self.v_hl.get(): o.append("--no-highlight")
        if not self.v_ctx.get(): o.append("--no-context")
        if not self.v_pb.get(): o.append("--no-page-breaks")
        if not App._context_model_enabled(self): o.append("--no-context-model")
        o += App._normalization_opts(self)
        o += ["--check-list", self.check_list_mode(), "--memo", self.memo_mode()]
        return o

    def memo_mode(self) -> str:
        return next((k for k, v in MEMO_LABELS.items() if v == self.v_memo.get()), "notice")

    def layout_key(self) -> str:
        return next((k for k, v in LAYOUT_LABELS.items() if v == self.v_layout.get()), "page")

    def preset_ref(self) -> str:
        """고른 짜임 규칙(내장 id·예전 이름·규칙 파일 경로) — --preset 으로 넘기고 설정에 'preset' 으로 저장한다."""
        return self._preset_ref

    def _preset_values(self) -> list[str]:
        return [lab for _, lab in preset_choices(self._preset_ref)] + [PRESET_PICK, PRESET_EXPORT]

    def _preset_selected(self, *_):
        """짜임 규칙 칸에서 고름: 내장·예전 이름은 그대로, '파일에서 고르기…' 는 규칙 JSON 을 골라 검사한 뒤 그 절대 경로를(쓸 수 없으면 알리고 앞의 것 그대로)."""
        lab = self.v_preset.get()
        if lab == PRESET_PICK:
            path = filedialog.askopenfilename(title="짜임 규칙 파일(JSON)", filetypes=[("JSON", "*.json"), ("모든 파일", "*.*")])
            if path:
                try:
                    load_preset(path)
                except PresetError as e:
                    messagebox.showerror(APP_NAME, f"이 짜임 규칙 파일은 쓸 수 없습니다.\n{e}")
                else:
                    self._preset_ref = path if os.path.isabs(path) else os.path.abspath(path)
        elif lab == PRESET_EXPORT:
            path = filedialog.asksaveasfilename(title="本草 조문 규칙의 사본 저장(JSON)", defaultextension=".json", initialfile="내책_규칙.json",
                                                filetypes=[("JSON", "*.json")])
            if path:
                try:
                    export_builtin("kaibao", path)
                except (OSError, PresetError) as e:
                    messagebox.showerror(APP_NAME, f"규칙 파일을 저장하지 못했습니다.\n{type(e).__name__}: {e}")
                else:
                    self._append(f"本草 조문 규칙의 사본을 저장했습니다: {path} — UTF-8 로 고쳐 저장한 뒤 [설정] → 이 책 → 짜임 규칙 → '{PRESET_PICK}' 로 고르십시오"
                                 "(칸의 뜻은 README '짜임 규칙 파일 만들기').\n")
        else:
            self._preset_ref = next((r for r, l in preset_choices(self._preset_ref) if l == lab), self._preset_ref)
        self.v_preset.set(preset_label(self._preset_ref))
        self.cb_preset.configure(values=self._preset_values())
        self._layout_changed(); self._sync_settings_summary()

    def _layout_changed(self, *_):
        """짜임에 쓰이지 않는 칸은 흐리게: 내용별이면 '쪽마다 쪽 나누기', 원본 쪽대로면 양식 칸·획 굵기·짜임 규칙(값은 그대로 둔다).
        획 굵기는 명령 쪽(build_outputs)이 재는 때만 켠다: 내용별 + 양식 HWPX + 짜임 규칙에 약재명 떼기(entry — 굵은 글씨 해석은 entry 가 있어야 씀;
        해석이 없어도 판독이 빠뜨린 약재명 뒤 빈칸을 잰다). 양식 [고르기…]는 늘 눌리게 둔다 — 고르면 내용별로 바뀐다."""
        flow = self.layout_key() == "flow"
        tv = getattr(self, "v_template", None)
        try:
            bold = flow and bool(tv is not None and str(tv.get() or "").strip()) and resolve_preset(App.preset_ref(self)).entry is not None
        except PresetError:
            bold = False                                                # 쓸 수 없는 규칙 — 실행 전에 preset_ok 가 알린다
        for w, on in ((getattr(self, "cb_pb", None), not flow), (getattr(self, "te_template", None), flow), (getattr(self, "cb_bold", None), bold),
                      (getattr(self, "cb_preset", None), flow)):
            if w is not None:
                w.state(["!disabled"] if on else ["disabled"])
        if vars(self).get("_set_rows"):
            App._sync_layout_rows(self, flow, bold)                     # 설정 탭: 이름도 흐리게, 까닭은 설명 끝에

    def pick_template(self):
        p = filedialog.askopenfilename(title="내용별 문서의 양식 HWPX", filetypes=[("HWPX", "*.hwpx"), ("모든 파일", "*.*")])
        if p:
            self.v_template.set(p)
            if self.layout_key() != "flow":
                self.v_layout.set(LAYOUT_LABELS["flow"])                # 양식은 내용별 문서에 쓰므로 함께 바꿔 둔다
            self._suggest_preset(p)

    def _suggest_preset(self, template: str) -> None:
        """양식에 本草 조문(kaibao) 스타일(약재명·본경·별록)이 있는데 지금 규칙에 약재명 떼기가 없으면 묻는다 — 0.7.x 설정이 덮인 채 옮겨져 영역 종류만이 된 사람이
        양식만 다시 골라 開寶本草를 약재명·주석·本經 짜임 없이 만들지 않게. [아니요]면 그대로 두고 진행 기록에 한 줄."""
        hit = template_suggests(template, App.preset_ref(self))
        if not hit:
            return
        msg = (f"이 양식에는 本草 조문 규칙(kaibao)의 스타일({'·'.join(hit[:4])})이 있는데, 짜임 규칙은 '{preset_label(App.preset_ref(self))}' 입니다"
               " — 이대로면 약재명 제목·주석·본경/별록 짜임이 들어가지 않습니다.")
        if messagebox.askyesno(APP_NAME, msg + "\n\n짜임 규칙을 '本草 조문'으로 바꿀까요?"):
            self._preset_ref = "kaibao"
            self.v_preset.set(preset_label("kaibao")); self.cb_preset.configure(values=self._preset_values())
            self._layout_changed(); self._sync_settings_summary()
            self._append(f"짜임 규칙을 '{preset_label('kaibao')}' 로 바꿨습니다(양식에 本草 조문 스타일이 있음).\n")
        else:
            self._append("주의: " + msg + " [설정] → 이 책 → 짜임 규칙에서 바꿀 수 있습니다.\n")

    def collate_memo_mode(self) -> str:
        return next((k for k, v in MEMO_LABELS.items() if v == self.v_cmemo.get()), "notice")

    def check_list_mode(self) -> str:
        return next((k for k, v in CHECK_LIST_LABELS.items() if v == self.v_chk.get()), "none")

    def mode_key(self, tab: int | None = None) -> str:
        variable = self.v_mode if tab is None else self._mode_vars[2 if tab == 2 else 0]
        return next((k for k, v in MODE_LABELS.items() if v == variable.get()), "auto")

    def media_res_key(self) -> str:
        return next((k for k, v in MRES_LABELS.items() if v == self.v_mres.get()), "high")

    def page_think_key(self) -> str:
        return next((k for k, v in PAGE_THINK_LABELS.items() if v == self.v_think1.get()), "auto")

    def page_mres_key(self) -> str:
        return next((k for k, v in PAGE_MRES_LABELS.items() if v == self.v_mres1.get()), "auto")

    def toggle_advanced(self):
        """기록 카드 머리의 [고급 도구 ▸]·고급 도구 카드의 [접기 ▴]: 진행 카드와 기록 카드 사이에 고급 도구 카드를 펼치거나 접는다(펼침은 설정에 남김)."""
        self.v_adv.set(not self.v_adv.get()); self._apply_advanced()
        self.cfg["advanced_open"] = self.v_adv.get(); write_config(self)
        if self.v_adv.get():
            self.after(60, lambda: self._reveal(self._adv_card))          # 펼친 단추들이 화면 밖이면 보이게 굴린다

    def _apply_advanced(self):
        b = vars(self).get("b_adv")
        if b is not None:
            b.configure(text="고급 도구 ▴" if self.v_adv.get() else "고급 도구 ▸")
        App._place_cards(self)

    def toggle_details(self):
        self.v_details.set(not self.v_details.get()); self._apply_details()
        self.cfg["details_open"] = self.v_details.get(); write_config(self)
        if self.v_details.get():
            self.after(60, lambda: self._reveal(self.details))

    def _apply_details(self):
        """판독 섹션의 세부 설정(2차 판독 모델·속도·2차 조각·판독 단계·1차 판독·호출 상한)을 섹션 아래에 펼치거나 접는다. 보통은 기본값이면 된다."""
        if self.v_details.get():
            self.details.pack(fill="x"); self.b_details.configure(text="세부 설정 접기 ▴")
        else:
            self.details.pack_forget(); self.b_details.configure(text="세부 설정 ▾")

    def _prepare_env(self) -> bool:
        """실행 전 점검: API 키(모델을 부르지 않는 실행은 묻지 않는다 — do_build 가 _env_need_key=False 를 둔다)·호출 상한 값, 그리고 설정 저장."""
        reader = self.v_reader.get()
        need_key = self.__dict__.pop("_env_need_key", True)
        if reader != "mock" and need_key:
            from .readers import split_keys, normalize_key, key_shape_warning
            env_keys = ENV_KEYS[reader]
            raw = self.v_key.get().strip() or next((os.environ[n] for n in env_keys if os.environ.get(n)), "")
            key = normalize_key(reader, raw)
            keys = split_keys(reader, key)
            if key != self.v_key.get():
                self.v_key.set(key)                  # 공백·따옴표 등을 걷어 낸 값으로 바꿔 보여 준다(키가 여러 개면 쉼표로)
            if not keys:
                messagebox.showwarning(APP_NAME, f"{PROVIDER_LABELS[reader]} API 키가 없습니다.\n선택한 제공사의 키를 넣으십시오.\n{KEY_URLS[reader]}"); return False
            warn = "\n".join((f"키 {i}: " if len(keys) > 1 else "") + w for i, w in ((i, key_shape_warning(reader, k)) for i, k in enumerate(keys, 1)) if w)
            if warn and not messagebox.askyesno(APP_NAME, warn + "\n\n그래도 이 키로 진행할까요?"):
                return False
            os.environ[env_keys[0]] = key
        if max_calls_value(self.v_maxcalls.get()) is None:
            messagebox.showwarning(APP_NAME, f"호출 상한은 0 이상의 정수여야 합니다(0 = 상한 없음). 지금 값: {self.v_maxcalls.get()!r}"); return False
        self.save_settings(quiet=True)
        return True

    MAX_LOG_LINES = 4000

    def _append(self, s: str):
        self.log.configure(state="normal"); self.log.insert("end", s)
        try:
            n = int(self.log.index("end-1c").split(".")[0])
            if n > self.MAX_LOG_LINES:                       # 700쪽 작업에서도 창이 무거워지지 않게 오래된 줄을 지운다
                self.log.delete("1.0", f"{n - self.MAX_LOG_LINES + 500}.0")
        except Exception:
            pass
        try:                                                            # 지난 줄은 MUTED, 마지막 글줄만 먹(지금 무엇을 하는지 눈에 띄게)
            self.log.tag_remove("now", "1.0", "end")
            last = self.log.search(r"\S", "end-1c", backwards=True, regexp=True)
            if last:
                line = last.split(".")[0]
                self.log.tag_add("now", f"{line}.0", f"{line}.end")
        except tk.TclError:
            pass
        self.log.see("end-2c"); self.log.configure(state="disabled")    # 끝의 빈 줄 대신 마지막 글줄이 보이게
        lines = s.strip().splitlines()
        if lines:
            App._notice(self, lines[-1])                                # 설정 탭이 보이면 바닥 알림 줄에도

    def _set_running(self, on: bool, command: str = ""):
        """작업 중에는 단추를 잠그고, 진행 막대를 0 에서 새로 시작한다(좌우로 오가는 막대 대신 끝까지 차는 막대).
        실행 카드: 도는 실행의 자리만 잠긴 모양, [■ 중지]는 살리고, 이 책의 설정 판의 쪽 범위 칸도 잠근다. 끝나면 원본이 없을 때 꺼 두는 단추를 다시 끈다."""
        self.running = on
        d = vars(self)
        if on:
            self._running_cmd = command; self._stop_requested = False; self._run_result = None
            self._ledger_live = d.get("_ledger_for") == command and command in ("run", "run-pdf")   # 이번 실행의 어림이 장부에 있음(go 안에서 남김)
            self._ledger_for = None
            self._calls_by_stage = {}; self._calls_base = None; self._calls_tail = None; self._calls_used = 0
            self._calls_live = False                                    # start() 가 켠다(명령 스레드가 CALL_CAP 을 셀 때만 읽음)
            self._page_live = {}                                        # 이번 실행의 쪽 알림(cli.PAGE_EVENT) — 끝난 뒤 다시 센 칸에도 겹친다
            self._seen = {}                                             # 단계마다 마지막으로 본 (끝난 수, 전체) — 끝난 뒤 단계 띠·큰 숫자 줄
            self._run_counts = dict(d["_counts"]) if d.get("_counts") else None      # 실행을 시작할 때의 책 수치(끝나면 다시 셈)
            self._strip_marks = {n: "record" if m == "done" else m for n, m in (d.get("_strip_marks") or {}).items()}   # 지난 실행의 '끝남'은 이번엔 기록 있음
            self._marks0 = dict(self._strip_marks)                     # 실행 전 쪽 띠 — 끝난 뒤 새로 기록이 생긴 쪽을 '끝남'으로
            App._draw_strip(self); App._draw_legend(self)
        else:
            self._run_label = None                                      # start() 가 정한 명령 이름은 그 실행 동안만
            for b in (d.get("b_stop"), d.get("b_stop2")):
                if b is not None:
                    b.configure(text="■  중지")                          # '멈추는 중…' 을 되돌린다
        for b in self.buttons:
            b.configure(state="disabled" if on else "normal")
        for button in getattr(self, "_tab_buttons", []):
            button.configure(state="disabled" if on else "normal")
        self._reader_changed()
        for b in (self.b_stop, getattr(self, "b_stop2", None)):
            if b is not None:
                b.configure(state="normal" if on and command != "update" else "disabled")
        App._show_lockline(self, on)
        for e in d.get("_lock_entries", ()):
            e.state(["disabled"] if on else ["!disabled"])
        if on:
            self.tracker = Tracker(command)
            self.progress.configure(value=0); self._prog_shown = 0.0
            self.v_prog.set("0%" if self.tracker.plan else "")
        else:
            App._apply_source_lock(self)
        App._sync_setting_states(self)                                  # self.buttons 를 다시 켠 뒤 — 꺼 둔 중문부호 규칙 단추
        App._sync_collate_pick(self)                                    # 〃 — 표에서 고른 구획이 없으면 [이 구획 대조만]·[이 구획 교정]은 꺼 둔다
        App._sync_run_slot(self)
        App._sync_log_head(self, d.get("_selected_tab", 0))
        App._schedule_cards(self)

    def _show_lockline(self, on: bool):
        """잠금 줄(머리띠 아래, 실행 중에만): 글은 탭 줄과 같은 x 에서 시작한다."""
        line = getattr(self, "_lockline", None)
        if line is None:
            return
        if on:
            try:
                x = self._tab_bar.winfo_x() + self.px(12)
            except (AttributeError, tk.TclError):
                x = self.px(24)
            self._lock_label.pack_configure(padx=(max(self.px(24), x), 0))
            line.pack(fill="x", after=self._header_rule)
        else:
            line.pack_forget()

    def _bar_value(self, f: float) -> int:
        """0~1 → 막대 값(0~1000). 0 보다 크면 둥근 끝 두 개가 들어갈 만큼은 보이게 한다(겹쳐 그려지는 모양 방지)."""
        v = int(round(f * 1000))
        width = max(1, self.progress.winfo_width())
        if 0 < v < 1000:
            v = max(v, -(-self._bar_cap * 1000 // width))
        return min(1000, v)

    def _on_progress(self, stage: str, done: int, total: int, calls: int | None = None):
        """명령의 진행 알림(단계, 끝난 수, 전체 수, 그때의 호출 수 — 창이 넣은 cli.PROGRESS 가 명령 스레드에서 함께 읽음)."""
        before = self.tracker.stage
        if App._count_calls(self, calls):                               # 이 알림 앞의 호출은 앞 단계 몫으로(바뀐 단계는 아래 update 뒤부터)
            App._sync_ledger(self)
        f = self.tracker.update(stage, done, total)
        if self.tracker.stage:
            vars(self).setdefault("_seen", {})[self.tracker.stage] = (self.tracker.done, self.tracker.total)
        if self.tracker.stage != before:
            App._sync_run_slot(self)
        self.progress.configure(value=self._bar_value(f)); self._prog_shown = time.time()
        self.v_prog.set(self.tracker.live_text())
        App._sync_progress(self)

    def start(self, argv: list[str], label: str, open_after: Path | None = None):
        if self.running:
            return
        self.open_after = open_after
        cmd = argv[0] if argv else ""
        if cmd == "run" and "--pdf-only" in argv:
            cmd = "run-pdf"
        self._run_label = label                                         # 한 번에 인식이 아닌 명령은 실행 단추 자리에 '{label} 중'
        self._set_running(True, cmd); self.v_status.set(label + " 중…")
        self._calls_live = True                                         # 이 명령(cli.main)이 CALL_CAP 을 센다 — 장부의 이번 실행 호출
        pages = cmd in PAGE_CMDS                                        # 이 탭 원본의 쪽을 판독하는 명령만 쪽 띠를 움직인다(책갈피 교정의 판독은 다른 PDF)
        self.q.put(("log", f"\n▶ {label}\n   yeone-ocr {' '.join(argv)}\n"))
        def target():
            rc = 1
            w = None
            try:
                w = QueueWriter(self.q)
                self._last_writer = w
                with contextlib.redirect_stdout(w), contextlib.redirect_stderr(w):
                    from . import cli
                    cli.CANCEL.clear()
                    cli.PROGRESS = lambda st, d, t: self.q.put(("progress", st, d, t, App._call_count()))    # 그때의 호출 수 — 단계 몫을 정확히
                    if pages:
                        cli.PAGE_EVENT = lambda n, st: self.q.put(("page", n, st))                     # 쪽 띠의 칸(판독의 쪽마다)
                    try:
                        rc = cli.main(argv)
                    finally:
                        cli.PROGRESS = None; cli.PAGE_EVENT = None
            except SystemExit as e:
                rc = e.code if isinstance(e.code, int) else 1
                if e.code and not isinstance(e.code, int): self.q.put(("log", str(e.code) + "\n"))
            except ModuleNotFoundError as e:
                self.q.put(("log", f"\n오류: 패키지가 없습니다: {e.name}. [고급 도구 ▸]의 [패키지 설치·점검] 단추를 누르십시오.\n"))
                if w: w.problems.append(f"오류: 패키지가 없습니다: {e.name}")
            except Exception:
                self.q.put(("log", traceback.format_exc()))
            finally:
                self.q.put(("done", rc, label))          # 어떤 경우에도 창에 끝을 알린다
        self._worker = threading.Thread(target=target, daemon=True)   # 창을 닫을 때 이 스레드가 쪽을 마저 쓰기를 기다린다(on_close)
        self._worker.start()

    def _poll(self):
        try:
            while True:
                item = self.q.get_nowait()
                if item[0] == "log":
                    self._append(item[1])
                elif item[0] == "done":
                    self._finish(item[1], item[2])
                elif item[0] == "pkg":
                    self._pkg_result(item[1])
                elif item[0] == "keycheck":
                    self._keycheck_result(item[1])
                elif item[0] == "sections":
                    self.collate_rows = item[1]; self.v_cstat.set(item[2]); self.refresh_collate_table()
                    self.after(60, lambda: self._reveal(self.ctable.master))      # 색인 결과 표가 위 칸 아래로 숨지 않게(낮은 화면)
                elif item[0] == "styles_suggest":
                    if not self.v_hstyles.get().strip():
                        self.v_hstyles.set(item[1])
                elif item[0] == "progress":
                    self._on_progress(*item[1:])                       # (단계, 끝난 수, 전체 수[, 그때의 호출 수])
                elif item[0] == "plan_progress":
                    if item[1] == self._plan_seq and self._plan_wait is not None:
                        try:
                            self._plan_wait.update_progress(*item[2:])
                        except tk.TclError:
                            pass
                elif item[0] == "plan":
                    self._plan_done(*item[1:])                           # 비용 창(wait_window)은 여기서 — 닫힐 때까지 큐는 기다린다(업데이트 알림이 비용 창 위로 뜨지 않게)
                elif item[0] == "bundle":
                    self._bundle_msg(*item[1:])
                elif item[0] == "table":
                    self._fill_table(*item[1:])
                elif item[0] == "counts":
                    App._counts_done(self, *item[1:])
                elif item[0] == "pagemap":
                    App._pagemap_done(self, *item[1:])
                elif item[0] == "page":
                    App._on_page(self, *item[1:])
                elif item[0] == "fx":
                    self._fx_fetched(item[1], item[2])
                elif item[0] == "update_check":
                    self._update_checked(item[1], item[2])
                elif item[0] == "update_download":
                    self._update_downloaded(item[1], item[2])
                    if getattr(self, "_closing", False):
                        return
        except queue.Empty:
            pass
        if vars(self).get("_strip_dirty"):                              # 쪽 알림은 큐를 다 비운 뒤 한 번만 다시 그린다(700쪽 책의 첫 알림 수백 개도)
            self._strip_dirty = False
            App._draw_strip(self); App._draw_legend(self); App._sync_progress(self)
        if getattr(self, "_closing", False) and getattr(self, "_update_installing", False):
            return
        self._offer_update()
        if getattr(self, "_closing", False):
            return
        if self.running and time.time() - self._prog_shown > 1.0:
            self.v_prog.set(self.tracker.live_text()); self._prog_shown = time.time()      # 남은 시간(또는 지난 시간) 글을 1초마다 새로
            App._sync_progress(self)
        App._fit_host(self)                                             # 카드가 접히거나 늘면 본문 높이를 맞춘다(같으면 아무것도 하지 않음)
        if self.running and App._count_calls(self):
            App._sync_ledger(self)                                      # 장부의 이번 실행 호출(재시도 포함)
        self.after(100, self._poll)

    def _finish(self, rc, label):
        App._count_calls(self, App._call_count() if vars(self).get("_calls_live") else None)   # 마지막 호출 수 — 명령이 끝났으니 지금 수가 이 명령의 것(끝난 뒤 장부에 남김)
        live = vars(self).get("_page_live")
        if live:                                                        # 쪽이 끝나기 전에 멈췄으면(중지·오류) 남은 '지금'은 '남음'
            from .gui_progress import settle_marks
            before = vars(self).get("_marks0")
            self._page_live = settle_marks(live, before); self._strip_marks = settle_marks(self._strip_marks, before)
            self._strip_dirty = True
        self._set_running(False)
        if getattr(self, "_closing", False):             # 닫는 중에 끝났으면 알림창 없이 조용히
            self._last_writer = None; return
        w = getattr(self, "_last_writer", None)
        kind, status, head = finish_words(rc, w.problems if w is not None else None)    # [중지]로 멈춘 명령은 130(멈추기 전에 일을 다 끝냈으면 0), 실패한 쪽이 있으면 3, 3차 판별을 못 마치면 4
        ok, partial = kind == "ok", kind == "partial"
        from .gui_progress import failed_count, failed_pages
        problems = w.problems if w is not None else None
        self._run_result = {"rc": rc, "label": label, "kind": kind, "cmd": getattr(self.tracker, "command", ""),     # 실행 카드·장부·진행 카드의 끝난 모양(§7.3)
                            "failed": rc == 3 and _pages_failed(problems), "calls": vars(self).get("_calls_used"),
                            "failed_n": failed_count(problems), "failed_pages": failed_pages(problems), "elapsed": time.time() - self.tracker.t0}
        self.v_status.set(f"{label} {status}")
        App._request_counts(self, refresh=True)                         # 판독 기록이 늘었다 — 책 카드의 수치를 다시 센다
        took = "걸린 시간 " + fmt_duration(time.time() - self.tracker.t0)
        if ok or partial:
            self.tracker.finish(); self.progress.configure(value=1000); self.v_prog.set(f"{head} · {took}")
        else:
            self.v_prog.set(f"{self.tracker.frac * 100:.0f}% · {head} · {took}")
        self._append(f"— {label} {'끝' if ok or partial else '중단'} (rc={rc})\n")
        self.refresh_table()
        alert = finish_alert(rc, label, w.problems if w is not None else [])
        if alert:
            (messagebox.showerror if alert[0] == "error" else messagebox.showwarning)(APP_NAME, alert[1])
        self._last_writer = None
        if (ok or partial) and self.open_after and Path(self.open_after).exists():
            open_path(self.open_after)
        elif (ok or partial) and ("HWPX" in label and "인식" in label):
            if messagebox.askyesno(APP_NAME, "끝났습니다. 검수 화면(review.html)을 열까요?"):
                self.do_review()
        self.open_after = None

    # ---------- 단추 동작 ----------
    def open_source(self):
        if self._need_pdf():
            open_path(self.pdf())

    def _reset_screenshot_pages(self):
        # 이전 책의 쪽 범위·쪽번호 보정·강제 재판독을 새 캡처에 적용하지 않는다. 비우기 전에 앞 책의 설정을 남기고 떼어 둔다(빈 쪽 범위가 앞 책에 저장되지 않게).
        self._save_book(create=False); self._book = {"dir": None, "loaded": {}}
        self.v_pages.set(""); self.v_off.set(""); self.v_force.set(False)
        self.cfg.update(pages="", print_offset="", force=False)

    def clear_image_input(self):
        """이미지 작업 선택만 비운다. 디스크 파일과 다른 작업의 입력은 보존한다."""
        if self.running or getattr(self, "_closing", False) or getattr(self, "_update_installing", False):
            return
        self.select_tab(2)
        self._reset_screenshot_pages()
        self.v_pdf.set(""); self.cfg["last_pdf"] = ""
        self._remember_inputs(); write_config(self)
        self.refresh_table()
        self.open_after = None
        self.progress.configure(value=0); self.v_prog.set("")
        self.v_status.set(CLEARED_STATUS)
        self._append("이미지 입력을 비웠습니다. 저장된 원본과 결과 파일은 보존됩니다. 새 이미지를 고르거나 스크린샷을 붙여넣으십시오.\n")

    def paste_screenshot(self, event=None):
        """명시적으로 붙여넣은 이미지를 현재 이미지 원본 뒤에 추가한다. API 호출은 실행 단추에서 한다."""
        if self.running or getattr(self, "_closing", False) or getattr(self, "_update_installing", False):
            return "break"
        captured = None
        try:
            from PIL import Image, ImageGrab
            captured = ImageGrab.grabclipboard()
            if isinstance(captured, Image.Image):
                from .screenshots import save_screenshot
                source = save_screenshot(captured)
                self.select_tab(2)
                App._append_image_input(self, [str(source)])
                self._append(f"스크린샷 붙여넣기 ({captured.width} × {captured.height} 픽셀). 원본은 자동 보관됩니다. 계속 붙여넣거나 [한 번에 인식]을 눌러 판독하십시오.\n")
            elif isinstance(captured, list) and captured:
                paths = [str(p) for p in captured]
                if any(Path(p).suffix.lower() not in IMAGE_EXTS or not Path(p).is_file() for p in paths):
                    messagebox.showinfo(APP_NAME, "복사한 파일 중 이미지가 아닌 파일이 있습니다. PNG·JPG 등의 이미지 파일만 복사하거나 Win+Shift+S로 화면을 캡처하십시오.")
                    return "break"
                self.select_tab(2)
                App._append_image_input(self, paths)
            else:
                messagebox.showinfo(APP_NAME, "클립보드에 이미지가 없습니다.\nWin+Shift+S로 원하는 부분을 캡처한 뒤 이미지 상자에서 Ctrl+V를 누르십시오.\n저장된 이미지는 [이미지 고르기…]로 열 수 있습니다.")
        except Exception as exc:
            messagebox.showerror(APP_NAME, f"스크린샷을 불러오지 못했습니다. 다시 캡처하거나 이미지 파일로 저장해 열어 보십시오.\n{exc}")
        finally:
            if captured is not None and hasattr(captured, "close"):
                captured.close()
        return "break"

    def _append_image_input(self, paths: list[str]):
        """Keep all existing image-tab pages before the new batch; commit only after bundling succeeds."""
        current = self.pdf()
        if current and Path(current).is_file() and Path(current).suffix.lower() in (*IMAGE_EXTS, ".pdf"):
            from .pdfio import _natural_key
            incoming = sorted(paths, key=lambda p: _natural_key(Path(p)))
            self._bundle_images([current, *incoming], reset_pages=True, preserve_order=True)
        elif len(paths) > 1:
            self._bundle_images(paths, reset_pages=True)
        else:
            self._reset_screenshot_pages()
            self._use_pdf(paths[0])

    def choose_pdf(self):
        if self.running: return
        path = filedialog.askopenfilename(title="PDF 고르기", filetypes=[("PDF", "*.pdf")])
        if path:
            self.select_tab(0)
            self._use_pdf(path)

    def choose_images(self):
        if self.running: return
        imgs = " ".join("*" + e for e in IMAGE_EXTS)
        ps = filedialog.askopenfilenames(title="이미지 고르기 (여러 장 선택 가능)", filetypes=[("이미지", imgs)])
        ps = [p for p in (ps or ()) if p]
        if not ps:
            return
        if any(Path(p).suffix.lower() not in IMAGE_EXTS for p in ps):
            messagebox.showinfo(APP_NAME, "PNG·JPG·TIFF 등의 이미지 파일을 고르십시오. PDF는 [일반 OCR] 탭에서 엽니다.")
            return
        self.select_tab(2)
        if len(ps) > 1:
            self._bundle_images(ps, reset_pages=True); return
        self._reset_screenshot_pages()
        self._use_pdf(ps[0])

    def _remember_inputs(self):
        self._input_force[self._active_input] = self.v_force.get()
        self.cfg["ocr_inputs"] = {str(tab): {**{key: value.get() for key, value in variables.items()},
                                            "force": self._input_force[tab]}
                                  for tab, variables in self._input_vars.items()}
        self.cfg["input_tab"] = self._active_input
        self.cfg["mode"] = self.mode_key(0)
        self.cfg["image_mode"] = self.mode_key(2)

    def _use_pdf(self, p: str):
        old = self.pdf()
        self._save_book(create=False)                        # 앞 책의 설정을 그 결과 폴더에(폴더가 있을 때만)
        self.v_pdf.set(p); self.cfg["last_pdf"] = p
        kind = "이미지" if Path(p).suffix.lower() in IMAGE_EXTS else "PDF"
        self._append(f"{kind}: {p}\n결과 폴더: {self.work()}\n")
        self._load_book(clear_if_missing=bool(old) and Path(p) != Path(old))   # 다른 책: 그 책의 설정, 없으면 앞 책의 쪽 범위·보정을 비움
        self._remember_inputs(); write_config(self); self.refresh_table()
        if kind == "이미지" and self.mode_key() == "auto":
            self._append("  실행할 때 이미지의 글줄 배치로 가로·세로 읽기 방향을 자동 판정합니다.\n")

    # ---- 책별 설정: 결과 폴더의 book_settings.json(state.BOOK_KEYS). 전체 설정(config.json)은 새 책의 기본값 ----
    def _book_dir(self) -> Path | None:
        """지금 원본의 결과 폴더(명령에 --work 로 넘기는 것과 같은 work_dir_for). 원본이 없으면 None. 폴더를 만들지 않는다."""
        return work_dir_for(self.pdf()) if self.pdf() else None

    def _book_values(self) -> dict:
        """책마다 기억하는 칸의 지금 값. 읽기 방향은 지금 입력 탭(일반·이미지)의 것 — 책갈피·설정 탭에서 저장해도 그 책의 방향이 남게."""
        return {"layout": self.layout_key(), "template": self.v_template.get().strip(), "preset": self.preset_ref(),
                "bold_split": bool(self.v_bold.get()), "pages": self.v_pages.get().strip(), "print_offset": self.v_off.get().strip(),
                "mode": self.mode_key(self._active_input)}

    def _apply_book(self, d: dict) -> None:
        """책별 설정의 있는 열쇠만 칸에 넣는다. 짜임 규칙을 짜임(v_layout)보다 먼저 — v_layout 을 쓰면 불리는 _layout_changed 가 새 규칙으로 획 굵기 칸을 켜고 끄게.
        넣은 문서 설정·읽기 방향은 전체 설정의 '읽어 둔 값'(_cfg_loaded)에도 적는다 — 책을 보여 주기만 한 창이 닫힐 때 그 책의 값을 다른 창이 저장한
        새 책 기본값 위에 덮어쓰지 않게(write_config 는 읽어 둔 값과 다른 것만 이 창이 바꾼 것으로 본다). 그 뒤 사용자가 고친 것만 이 창의 변경이다."""
        loaded = vars(self).get("_cfg_loaded")
        for k in ("layout", "template", "preset", "bold_split", "mode"):
            if k in d:
                ck = ("image_mode" if self._active_input == 2 else "mode") if k == "mode" else k
                self.cfg[ck] = (d[k] or DEFAULT_PRESET) if k == "preset" else d[k]
                if loaded is not None:
                    loaded[ck] = self.cfg[ck]
        if "preset" in d:
            self._preset_ref = d["preset"] or DEFAULT_PRESET
            self.v_preset.set(preset_label(self._preset_ref)); self.cb_preset.configure(values=self._preset_values())
        if "template" in d: self.v_template.set(d["template"])
        if "bold_split" in d: self.v_bold.set(d["bold_split"])
        if "layout" in d: self.v_layout.set(LAYOUT_LABELS[d["layout"]])
        if "pages" in d: self.v_pages.set(d["pages"])
        if "print_offset" in d: self.v_off.set(d["print_offset"])
        if "mode" in d: self._mode_vars[2 if self._active_input == 2 else 0].set(MODE_LABELS[d["mode"]])
        self._layout_changed(); self._sync_settings_summary()

    def _doc_summary(self, v: dict) -> str:
        return (f"짜임 {LAYOUT_LABELS[v['layout']]} · 양식 {Path(v['template']).name if v['template'] else '없음'} · 짜임 규칙 {preset_label(v['preset'])}"
                + (f" · 획 굵기 {'잼' if v['bold_split'] else '안 잼'}" if v["layout"] == "flow" and v["template"] else ""))   # 양식이 없으면 재지 않는다

    def _pending(self) -> dict:
        """결과 폴더가 아직 없는 책에서 이 창이 고친 책별 값 {결과 폴더: 값} — 전체 설정의 book_pending(창을 다시 열어도 남게). 실행해 폴더가 생겨 파일에 쓰면 뺀다."""
        p = self.cfg.get("book_pending")
        if not isinstance(p, dict):
            p = self.cfg["book_pending"] = {}
        return p

    def _sync_book_label(self) -> None:
        """'이 칸들은 어느 책의 것' 한 줄(설정 탭 '이 책' 섹션 제목의 풍선 도움말) — 설정 탭에서 고친 책별 설정 일곱 가지가 어느 책에 남는지(마지막에 쓴 입력 탭의 책)."""
        v = vars(self).get("v_docbook")
        if v is None:
            return
        d = self._book["dir"]; scope = "이미지 OCR" if self._active_input == 2 else "일반 OCR"
        if d is None:
            v.set(f"{scope} 탭에 원본을 고르면 쪽 범위·인쇄 쪽수·읽기 방향·문서 짜임·양식·짜임 규칙·획 굵기는 그 책의 책별 설정으로 남깁니다.")
        else:
            v.set(f"쪽 범위·인쇄 쪽수·읽기 방향·문서 짜임·양식·짜임 규칙·획 굵기는 「{Path(self.pdf()).name}」({scope} 탭)의 책별 설정입니다 — "
                  + (f"{d.name}/{BOOK_SETTINGS}" if d.is_dir() else "결과 폴더가 아직 없어 이 창이 기억했다가 처음 실행할 때 남깁니다") + ".")

    def _load_book(self, clear_if_missing: bool = False) -> None:
        """지금 원본의 책별 설정을 읽어 칸에 넣는다(읽기만 — 폴더·파일을 만들지 않는다). clear_if_missing(다른 책을 골랐을 때)이면 '다시 판독' 을 끈다.
        설정 파일이 없으면: 결과 폴더가 없던 그 책에서 이 창이 앞서 고친 값(_pending — 쪽 범위 포함)이 있으면 그것, 없으면 새 책 — 문서 설정은 지난 값(지금 칸)을
        이어 쓰고 진행 기록에 그 값을 한 줄로 남기며, clear_if_missing 이면 앞 책의 쪽 범위·인쇄 쪽수 보정을 비운다."""
        d = self._book_dir(); data = {}; pend = {}; unread = False
        if d is not None:
            try:
                data = load_book_settings(d, warn=lambda m: self._append(m + "\n"))
            except Exception as e:                                       # 읽지 못하는 폴더(권한·네트워크) — 알리고 지금 칸 그대로(새 책으로 보지 않음)
                self._append(f"주의: 책별 설정을 읽지 못했습니다({d / BOOK_SETTINGS}): {type(e).__name__}: {e}\n"); unread = True
            if data:
                App._pending(self).pop(str(d), None)                        # 파일이 생겼으면(다른 창·실행) 그것이 맞다
            elif not unread:
                pend = clean_book_values(App._pending(self).get(str(d)) or {})
        forced = clear_if_missing and bool(self.v_force.get())
        if forced:
            self.v_force.set(False)                                      # 다른 책을 '다시 판독' 으로 시작하지 않는다(기록 있는 쪽을 모두 다시 산다)
        off = " · '다시 판독' 은 껐습니다" if forced else ""
        if data or pend:
            got = data or pend
            if clear_if_missing:
                got = {"pages": "", "print_offset": "", **got}           # 손으로 고쳐 빠진 열쇠 — 앞 책의 쪽 범위로 새 책을 읽지 않게
            self._apply_book(got)
            v = self._book_values()
            where = f"책별 설정을 불러옴({d.name}/{BOOK_SETTINGS})" if data else "아직 실행하지 않은 책: 이 창에서 앞서 고른 설정을 되살림(결과 폴더가 생기면 남김)"
            self._append(f"{where}: {App._doc_summary(self, v)} · 쪽 범위 {v['pages'] or '전체'}"   # 쪽 범위는 늘 보인다(읽을 쪽 수 = 비용)
                         + (f" · 인쇄 쪽수 보정 {v['print_offset']}" if v["print_offset"] else "") + off + "\n")
        elif clear_if_missing and d is not None and not unread:
            cleared = bool(self.v_pages.get().strip() or self.v_off.get().strip())
            self.v_pages.set(""); self.v_off.set("")
            self._append("새 책: " + ("쪽 범위·인쇄 쪽수 보정은 비우고 '다시 판독' 은 껐습니다(책마다 따로 기억합니다). " if cleared or forced else "")
                         + f"문서 설정은 지난 설정({App._doc_summary(self, self._book_values())})을 이어 씁니다"
                         + " — 다른 책이면 [설정] → 이 책에서 고치십시오.\n")
        self._book = {"dir": d, "loaded": self._book_values() if d is not None else {}}
        App._sync_book_label(self)

    def _save_book(self, create: bool = False) -> None:
        """지금 원본의 책별 설정을 결과 폴더에 저장한다(파일을 새로 읽어 이 창이 바꾼 값만 — 다른 창이 그 사이 바꾼 값은 그대로). 이 창이 불러온 책에만 쓴다.
        결과 폴더가 없으면 create 일 때만(실행 직전) 만든다 — 고르기·탭 바꾸기·미리 보기로는 폴더가 생기지 않는다. 그때 이 창이 그 책에서 고친 값이 있으면
        _pending 에 둔다(다른 책을 보고 돌아와도·창을 다시 열어도 그 값으로 — 버리면 다른 책의 양식·짜임 규칙과 빈 쪽 범위 = 책 전체로 읽게 된다).
        실패는 '주의:' 한 줄(실행을 막지 않는다)."""
        d = self._book["dir"]
        if d is None or d != self._book_dir():                           # 불러오지 않은 책(칸 값이 그 책의 것이 아님)에는 쓰지 않는다
            return
        try:
            new = self._book_values()
            pend = App._pending(self)
            if save_book_settings(d, new, self._book["loaded"], create=create, warn=lambda m: self._append(m + "\n")) is not None:
                self._book["loaded"] = new; pend.pop(str(d), None)
            elif str(d) in pend or new != self._book["loaded"]:          # 폴더가 없는 책 — 고친 값만 기억(보기만 한 책은 새 책 기본값을 그대로 이어 쓰게)
                pend.pop(str(d), None); pend[str(d)] = new               # 끝으로 옮긴다(오래된 것부터 버림)
                for k in list(pend)[:-PENDING_MAX]:
                    pend.pop(k)
        except Exception as e:                                           # 읽기 전용·네트워크 폴더 등
            self._append(f"주의: 책별 설정을 저장하지 못했습니다({d / BOOK_SETTINGS}): {type(e).__name__}: {e}\n")
        App._schedule_cards(self)                                        # 이 책의 설정 판의 저장 상태

    @staticmethod
    def _bundle_path(srcs: list[Path], progress=None, *, compact: bool = False) -> Path:
        """묶음 파일 경로: '<첫 이름>~<끝 이름> (N장 지문).pdf'. 지문은 고른 이미지마다 (이름·크기·내용의 sha1) 을 입력 순서로 모아 낸
        sha1 앞 8자리라 가운데 이미지만 다른 선택이나 바꿔 넣은·고친 이미지는 이름이 달라지고(결과 폴더도 따로), 폴더를 옮기거나 복사해
        경로·수정 시각이 바뀌어도 그대로다(묶음·결과 폴더를 함께 옮기면 판독 기록이 이어진다). 내용은 8MiB 이하면 파일 전체(BMP·TIFF 의
        가운데만 고쳐 크기가 같아도 알아본다), 넘으면 앞·가운데·끝 64KiB 씩만 읽는다. compact=True는 전체 파일을 읽고 짧은 이름을 쓴다
        (기존 PDF 뒤에 계속 추가할 때 중첩된 파일명이 길어지지 않도록). progress(끝난 입력, 전체)로 진행을 알린다."""
        import hashlib
        h = hashlib.sha1()
        for i, s in enumerate(srcs, 1):
            with open(s, "rb") as f:
                n = os.fstat(f.fileno()).st_size; d = hashlib.sha1()
                if compact:
                    for chunk in iter(lambda: f.read(1 << 20), b""):
                        d.update(chunk)
                elif n <= 8 << 20:
                    d.update(f.read())
                else:
                    for at in (0, n // 2, n - 65536):
                        f.seek(at); d.update(f.read(65536))
            h.update(f"{s.name}|{n}|{d.hexdigest()}\n".encode("utf-8"))
            if progress: progress(i, len(srcs))
        # Appending an existing bundle must not keep growing its filename at every paste.
        name = (f"이미지모음-{h.hexdigest()[:16]}.pdf" if compact else
                f"{srcs[0].stem}~{srcs[-1].stem} ({len(srcs)}장 {h.hexdigest()[:8]}).pdf")
        out = srcs[0].with_name(name)
        return safe_output_path(out, srcs[0])

    def _bundle_images(self, paths: list[str], *, reset_pages: bool = False, preserve_order: bool = False) -> None:
        """이미지 여러 장 → 이름의 숫자 순서로 한 PDF 로 묶어 첫 이미지 옆에 저장(무손실: JPEG 는 그대로, 나머지는 PNG).
        preserve_order=True는 기존 이미지·PDF와 새 이미지의 입력 순서를 보존하며 모든 쪽을 새 묶음에 담는다.
        이름에 고른 이미지들의 지문이 들어가므로 정확히 같은 선택으로 이미 묶은 파일이 있을 때만 그대로 쓴다(판독 기록이 이어지게).
        지문 계산과 묶기는 작업 스레드에서 하고(느린 드라이브에서도 창이 멈추지 않게) 끝날 때까지 단추를 잠근다. 끝나면 _bundle_msg 가 _use_pdf 로 잇는다."""
        allowed = (*IMAGE_EXTS, ".pdf") if preserve_order else IMAGE_EXTS
        bad = [p for p in paths if Path(p).suffix.lower() not in allowed]
        if bad:
            messagebox.showinfo(APP_NAME, "여러 개를 고를 때는 이미지만 고르십시오(PDF 는 한 번에 하나).\n  " + "\n  ".join(Path(b).name for b in bad[:5])); return
        if self.running: return
        self._bundle_reset_pages = reset_pages
        self._bundle_append = preserve_order
        from .pdfio import _natural_key
        srcs = [Path(p) for p in paths]
        if not preserve_order:
            srcs.sort(key=_natural_key)
        names = " → ".join(s.name for s in srcs[:8]) + (" …" if len(srcs) > 8 else "")
        self._bundle_in_progress = True
        self._set_running(True); self.v_status.set("붙여넣은 이미지를 뒤에 추가하는 중…" if preserve_order else f"이미지 {len(srcs)}장을 한 권으로 묶는 중…")
        for b in (self.b_stop, getattr(self, "b_stop2", None)):          # 묶기는 중간에 멈추지 않는다
            if b is not None: b.configure(state="disabled")
        def t():
            out = part = None; log = ""
            err = "이미지를 묶지 못했습니다"
            try:
                out = self._bundle_path(srcs, progress=lambda d, n: self.q.put(("bundle", "hash", d, n)),
                                        **({"compact": True} if preserve_order else {}))
                if out.exists():
                    err = None
                    log = (f"같은 이미지 순서로 묶은 파일을 다시 씀: {out.name}\n  {names}\n" if preserve_order else
                           f"같은 이미지 {len(srcs)}장으로 묶은 파일을 다시 씀: {out.name}\n  {names}\n")
                    return
                old = None if preserve_order else srcs[0].with_name(f"{srcs[0].stem}~{srcs[-1].stem} ({len(srcs)}장).pdf")
                out.parent.mkdir(parents=True, exist_ok=True)
                part = out.with_name(out.name + ".part")                       # 다 쓴 뒤에만 이름을 붙인다(끊긴 묶음을 다시 쓰지 않게)
                if preserve_order:
                    from .image_bundle import save_sources_as_pdf as save_bundle
                else:
                    from .pdfio import save_images_as_pdf as save_bundle
                save_bundle(srcs, part, progress=lambda d, n: self.q.put(("bundle", "prog", d, n)))
                os.replace(part, out); err = None
                log = (f"기존 이미지 뒤에 붙여넣은 이미지를 추가했습니다: {out.name}\n  {names}\n" if preserve_order else
                       f"이미지 {len(srcs)}장을 이름 순서로 묶음: {out.name}\n  {names}\n") + (
                    f"  참고: 예전 판이 묶은 '{old.name}' 이(가) 옆에 있습니다 — 그 판독 기록은 '{work_dir_for(str(old)).name}' 에 따로 있으니 이어 쓰려면 [일반 OCR]의 [PDF 고르기…]로 그 PDF를 고르십시오.\n"
                    if old is not None and old.exists() and work_dir_for(str(old)).is_dir() else "")
            except ModuleNotFoundError as e:
                err = f"패키지가 없어 이미지를 묶지 못했습니다: {e.name}. [고급 도구 ▸]의 [패키지 설치·점검] 을 누르십시오."
            except Exception as e:
                err = f"이미지를 묶지 못했습니다: {e}"
            finally:
                if err and part is not None:
                    with contextlib.suppress(OSError): part.unlink()
                self.q.put(("bundle", "done", out, err, log))
        self._worker = threading.Thread(target=t, daemon=True, name="image-bundle")
        self._worker.start()

    def _bundle_msg(self, kind: str, *a):
        """작업 스레드의 묶기 알림(주 스레드에서 _poll 이 부른다): ('prog', 끝난 장, 전체) 또는 ('done', 묶음 경로, 오류, 기록 글)."""
        if kind in ("prog", "hash"):
            d, n = a
            self.progress.configure(value=self._bar_value(d / n if n else 1.0))
            if vars(self).get("_bundle_append", False):
                self.v_status.set(f"이미지를 뒤에 추가하는 중… 입력 {d}/{n}개" if kind == "prog" else f"추가할 원본 확인 중… 입력 {d}/{n}개")
            else:
                self.v_status.set(f"이미지를 한 권으로 묶는 중… {d}/{n}장" if kind == "prog" else f"고른 이미지 확인 중(같은 묶음이 있는지)… {d}/{n}장")
            return
        out, err, log = a
        self._bundle_in_progress = False
        reset_pages = getattr(self, "_bundle_reset_pages", False)
        self._bundle_reset_pages = False
        appended = vars(self).get("_bundle_append", False)
        self._bundle_append = False
        self._set_running(False); self.v_status.set("준비됨")
        if err:
            self.progress.configure(value=0); self.v_prog.set("이미지 묶기 실패")
            messagebox.showerror(APP_NAME, err); return
        self.progress.configure(value=1000); self.v_prog.set("이미지 묶기 끝")
        if reset_pages:
            self._reset_screenshot_pages()
        self._append(log); self._use_pdf(str(out))
        preview = vars(self).get("image_preview")
        if appended and preview is not None:
            preview.show_last_page()

    def toggle_key(self):
        self.e_key.configure(show="" if self.e_key.cget("show") else "•")

    def check_key(self):
        """선택한 제공사에 키를 확인하고, 모델 목록에서 고르게 한다."""
        if self.running: return
        reader = self.v_reader.get()
        if reader == "mock":
            messagebox.showinfo(APP_NAME, "텍스트 레이어 시험은 API 키가 필요하지 않습니다."); return
        from .readers import split_keys, normalize_key
        raw = self.v_key.get().strip() or next((os.environ[n] for n in ENV_KEYS[reader] if os.environ.get(n)), "")
        key = normalize_key(reader, raw)
        keys = split_keys(reader, key); self.v_key.set(key)
        model = self.v_model.get().strip()
        self._set_running(True); self.v_status.set(f"{PROVIDER_LABELS[reader]} 키·모델 확인 중…")
        def t():
            try:
                from .readers import check_key
                res = check_key(reader, keys[0] if keys else "", model)
                if res.get("ok") and len(keys) > 1:      # 나머지 키는 쓸 수 있는지만(모델 목록 조회) 본다
                    bad = [f"키 {i}(…{k[-4:]})" for i, k in enumerate(keys[1:], 2) if not check_key(reader, k, model, probe=False).get("ok")]
                    res["message"] += f" · 키 {len(keys)}개" + (f" — 쓸 수 없는 키: {', '.join(bad)}" if bad else " 모두 쓸 수 있음(하루 한도에 걸리면 다음 키로 넘어감)")
            except Exception as e:
                # 예상치 못한 SDK 오류에도 인증 문자열이 진행 기록에 남지 않게 한다.
                msg = str(e)
                for secret in keys:
                    if secret: msg = msg.replace(secret, "[API 키]")
                res = {"ok": False, "message": f"확인 실패: {msg}", "models": [], "shape_warning": ""}
            self.q.put(("keycheck", res))
        threading.Thread(target=t, daemon=True).start()

    def _keycheck_result(self, res: dict):
        self._set_running(False)
        self.progress.configure(value=1000 if res.get("ok") else 0); self.v_prog.set("키 확인됨" if res.get("ok") else "키 확인 실패")
        msg = res["message"]
        self.v_status.set(msg if len(msg) <= 48 else msg[:46].rstrip() + "… (진행 기록)")
        self._append("키 확인: " + res["message"] + ("\n주의: " + res["shape_warning"] if res.get("shape_warning") else "") + "\n")
        if not res["ok"]:
            messagebox.showerror(APP_NAME, res["message"] + ("\n\n" + res["shape_warning"] if res.get("shape_warning") else "")); return
        self.save_settings(quiet=True)
        ModelDialog(self, res["models"], res.get("probe"), res.get("working_model", ""))

    def _select_provider(self, event=None):
        self.v_reader.set(next(k for k, v in PROVIDER_LABELS.items() if v == self.v_provider.get()))
        self._reader_changed()

    def _reader_changed(self):
        reader = self.v_reader.get()
        previous = self._active_reader
        if previous != reader:
            remember_provider(self.cfg, previous, self.v_model.get(), self.v_key.get(), self.v_model2.get())
            profile = provider_profile(self.cfg, reader)
            self.v_model.set(profile["model"]); self.v_key.set(profile["api_key"]); self.v_model2.set(profile["second_model"])
            self._active_reader = reader
            self.save_settings(quiet=True)
        self.v_provider.set(PROVIDER_LABELS[reader])
        self.cb_provider.configure(state="disabled" if self.running else "readonly")
        st = "normal" if reader != "mock" and not self.running else "disabled"
        self.e_model.configure(state=st); self.e_key.configure(state=st); self.e_model2.configure(state=st)
        for widget, enabled in self._gemini_widgets:
            widget.configure(state=enabled if reader == "gemini" and not self.running else "disabled")

    def save_settings(self, quiet=False):
        remember_provider(self.cfg, self.v_reader.get(), self.v_model.get(), self.v_key.get(), self.v_model2.get().strip())
        self.cfg.update({"pages": self.v_pages.get(), "print_offset": self.v_off.get(), "upscale": self.v_up.get(), "reader": self.v_reader.get(),
                         "model": self.v_model.get(), "api_key": self.v_key.get(), "second_pass": self.v_second.get(), "docx": self.v_docx.get(),
                         "join_pages": self.v_join.get(), "force": self.v_force.get(), "highlight": self.v_hl.get(), "page_breaks": self.v_pb.get(), "check_list": self.check_list_mode(),
                         "context_hint": self.v_ctx.get(), "memo_mode": self.memo_mode(),
                         "collate_policy": self.collate_policy(), "collate_memo": self.collate_memo_mode(),
                         "workers": int(self.v_workers.get() or 8), "second_model": self.v_model2.get().strip(),
                         "checkpoint": int(self.v_ckpt.get() or 0), "use_cache": self.v_cache.get(), "second_thinking_low": self.v_think2.get(), "adjudicate_blind": self.v_blind.get(), "last_pdf": self.pdf(),
                         "chunk_lines": int(self.v_chunk.get() or 0), "media_res": self.media_res_key(), "mode": self.mode_key(0), "image_mode": self.mode_key(2),
                         "page_thinking": self.page_think_key(), "page_media_res": self.page_mres_key()})
        if getattr(self, "v_suspects", None) is not None:
            self.cfg["suspects"] = self.v_suspects.get()
        if getattr(self, "v_skipany", None) is not None:
            self.cfg["skip_any_model"] = bool(self.v_skipany.get())
        if getattr(self, "v_tlpdf", None) is not None:
            self.cfg["textlayer_pdf"] = self.v_tlpdf.get()
        for key, name in (("normalize_traditional", "v_traditional"), ("normalize_yun", "v_yun"),
                          ("normalize_punctuation", "v_punctuation"), ("punctuation_rules", "v_punctuation_rules")):
            variable = getattr(self, name, None)
            if variable is not None:
                self.cfg[key] = variable.get()
        if getattr(self, "v_layout", None) is not None:
            self.cfg.update({"layout": self.layout_key(), "template": self.v_template.get().strip(), "bold_split": bool(self.v_bold.get()),
                             "context_model": bool(self.v_ctxmodel.get()), "preset": App.preset_ref(self)})
        mc = max_calls_value(self.v_maxcalls.get())
        self.cfg["max_calls"] = mc if mc is not None else self.cfg.get("max_calls", 0)     # 잘못 넣은 값은 저장하지 않는다(실행은 _prepare_env 가 막음)
        self._remember_inputs()
        self._save_book(create=False)                          # 책별 설정도(결과 폴더가 있을 때만 — 없으면 book_pending 에 기억. 창 닫기·실행 전 점검도 여기로)
        write_config(self)                                     # 파일을 새로 읽어 이 창이 바꾼 값만 얹는다 — 다른 창이 저장한 양식·짜임을 되돌리지 않게
        if not quiet:
            d = (vars(self).get("_book") or {}).get("dir")
            self.v_status.set(f"설정 저장됨 → {CONFIG_PATH}" + ("" if d is None else
                              f" · 「{Path(self.pdf()).name}」의 책별 설정 → " + (str(d / BOOK_SETTINGS) if d.is_dir() else "처음 실행할 때 결과 폴더에(그때까지 이 창이 기억)")))
            App._sync_book_label(self)
        App._schedule_cards(self)                              # 이 책의 설정 판의 저장 상태

    def _fetch_fx(self):
        """환율 자동 조회(작업 스레드). 받은 값은 큐로 넘겨 창 스레드가 설정에 남긴다(하루 한 번만 묻게). 실패하면 있던 값(지난날 값) 그대로."""
        try:
            from .estimate import fetch_fx_krw
            got = fetch_fx_krw()
        except Exception:
            got = None
        if got:
            self.q.put(("fx", got[0], time.strftime("%Y-%m-%d")))

    def _fx_fetched(self, rate: float, day: str):
        self._fx = (rate, "오늘 자동 조회")
        self.cfg["fx_auto"] = rate; self.cfg["fx_auto_date"] = day
        write_config(self)

    def fx_setting(self) -> tuple[float | None, str]:
        """환율: 직접 입력값 > 자동 조회값 > (없음 → estimate 가 기본값을 씀)."""
        fx = str(self.cfg.get("fx_krw") or "").strip()
        if fx:
            try: return float(fx), "직접 입력"
            except ValueError: pass
        if self._fx:
            return self._fx
        return None, ""

    def _plan_input(self, kind: str):
        """비용 어림의 입력 — Tk 칸은 여기(창 스레드)에서 다 읽어 둔다. 어림 스레드는 Tk 를 만지지 않는다."""
        from .costplan import PlanInput
        try:
            up = float(self.v_up.get() or 2)
        except ValueError:
            up = 2.0
        return PlanInput(kind=kind, pdf=self.pdf(), work_dir=str(self.work()), pages=self.v_pages.get().strip(), reader=self.v_reader.get(),
                         model=self.v_model.get().strip(), second_model=self.v_model2.get().strip(), second_pass=bool(self.v_second.get()), upscale=up,
                         use_cache=bool(self.v_cache.get()), force=bool(self.v_force.get()), suspects=bool(self.v_suspects.get()), blind=bool(self.v_blind.get()),
                         context_model=App._context_model_enabled(self), layout=self.layout_key(), preset=App.preset_ref(self), join_pages=bool(self.v_join.get()),
                         normalize_traditional=bool(self.v_traditional.get()) if getattr(self, "v_traditional", None) is not None else True,
                         normalize_yun=bool(self.v_yun.get()) if getattr(self, "v_yun", None) is not None else True,
                         normalize_punctuation=bool(self.v_punctuation.get()) if getattr(self, "v_punctuation", None) is not None else True,
                         punctuation_rules=self.v_punctuation_rules.get().strip() if getattr(self, "v_punctuation_rules", None) is not None else "",
                         skip_any_model=bool(self.v_skipany.get()))

    def _lock_for_plan(self, on: bool):
        for b in self.buttons:
            b.configure(state="disabled" if on or self.running else "normal")
        if not on:
            App._apply_source_lock(self)

    def _with_plan(self, kind: str, go) -> None:
        """비용 한 창: 판독·3차 판별·합의 의심·문맥 교정의 어림(costplan.count_plan)을 작업 스레드에서 세고(462쪽이면 몇 초 — 창이 멈추지 않게
        작은 진행 창을 띄우고 단추를 잠근다) 끝나면 _poll → _plan_done. go(extra) 는 실행(extra = 모델 단계를 뺄 선택), go=None 은 [비용 미리 보기].
        mock 판독기는 모델을 부르지 않아 어림하지 않는다. 어림 중에 다시 눌러도(창이 잠겨 있지만) 두 번 세지 않는다."""
        pdf_only = kind == "run-pdf"
        if pdf_only:
            kind = "run"
        if self.v_reader.get() == "mock":
            if go is None:
                messagebox.showinfo(APP_NAME, "mock 판독기는 모델을 부르지 않아 비용이 없습니다.")
            else:
                go([])
            return
        if self._planning:
            return
        from . import costplan
        try:
            inp = self._plan_input(kind)
            if pdf_only:
                inp = App._pdf_input(inp)
            self._plan_sig = App._sig_of(inp)                              # 장부가 '어림 다시 필요'를 가릴 지문(_remember_plan)
            cancel = threading.Event()
            wait = PlanWait(self, cancel)
        except Exception as e:                                             # 어림을 시작하지 못하면 실행하지 않는다(닫힌 쪽으로)
            messagebox.showerror(APP_NAME, f"비용 어림을 시작하지 못해 실행하지 않습니다: {type(e).__name__}: {str(e)[:160]}"); return
        self._planning = True; self._plan_seq += 1; seq = self._plan_seq; self._plan_wait = wait
        self._lock_for_plan(True)

        def work():
            try:
                res = ("ok", costplan.count_plan(inp, progress=lambda d, t, s: self.q.put(("plan_progress", seq, d, t, s)), should_stop=cancel.is_set))
                if cancel.is_set():                                        # 다 센 뒤에 [취소]를 눌렀어도 취소
                    res = ("cancelled", None)
            except costplan.PlanCancelled:
                res = ("cancelled", None)
            except Exception as e:
                res = ("error", f"{type(e).__name__}: {str(e)[:160]}")
            self.q.put(("plan", seq, "run-pdf" if pdf_only else kind, res, go))   # 결과는 큐로만 — 창은 _poll 이 그린다(문자 PDF 는 창의 글만 다름)
        threading.Thread(target=work, daemon=True, name="cost-plan").start()

    def _plan_done(self, seq: int, kind: str, res: tuple, go) -> None:
        """어림이 끝남(창 스레드): 진행 창을 닫고, 물을 것이 없고 어림이 다 됐으면 바로 실행, 아니면 비용 창(CostDialog)으로 묻는다.
        [예]면 go([]), [아니요]면 go(모델 단계를 뺄 선택), [취소]·창 닫기면 아무것도 실행하지도 쓰지도 않는다(책별 설정은 go 안에서만 남긴다).
        어림이 실패했으면 물을 것이 0 이어도 창을 띄운다(닫힌 쪽으로). 지난 어림의 늦은 결과(seq 가 다름)는 버린다."""
        if seq != self._plan_seq:
            return
        from . import costplan
        pdf_only = kind == "run-pdf"                                       # [▶ 한 번에 인식 (문자 PDF 만들기)]: 계산은 run 그대로, 창의 알약·캡션·단락만 다름
        if pdf_only:
            kind = "run"
        self._planning = False
        wait, self._plan_wait = self._plan_wait, None
        if wait is not None:
            try:
                wait.destroy()
            except tk.TclError:
                pass
        self._lock_for_plan(False)
        status, val = res
        if status == "cancelled" or (isinstance(getattr(wait, "cancel", None), threading.Event) and wait.cancel.is_set()):   # 결과가 큐에 든 뒤 누른 [취소]도
            self.v_status.set("비용 어림을 취소했습니다"); return
        try:                                                               # 여기서 난 예외가 _poll 을 끊지 않게 — 보이지 못하면 실행하지 않는다
            plan = val if status == "ok" else costplan.failed_plan(kind, val)
            priced = costplan.price_plan(plan, self.cfg.get("prices") or {}, *self.fx_setting())
            self._append(costplan.format_plan(priced).splitlines()[0] + "\n")
            if kind == "adjudicate" and go is not None and not costplan.needs_confirm(priced):
                messagebox.showinfo(APP_NAME, f"이 쪽 범위에 판독 기록이 없습니다.\n결과 폴더: {self.work()}" if not plan["records"] else
                                    f"판독 기록 {plan['records']:,}쪽에 〔a/b〕 자리·합의 판독 의심 자리 모두 새로 물을 곳이 없습니다(1차·2차가 갈린 자리가 없거나 이미 물음)."
                                    + "".join("\n" + x for x in costplan.skipped_lines(plan)))
                return
            if go is not None and not costplan.needs_confirm(priced):
                extra, choice = [], "all"
            else:
                d = CostDialog(self, plan, kind, preview=go is None, **({"pdf_only": True} if pdf_only else {})); self.wait_window(d)
                priced = getattr(d, "priced", None) or priced                # 창에서 단가를 고쳤으면 그 금액으로 장부에
                if go is None:
                    App._remember_plan(self, kind, pdf_only, priced, None, preview=True)   # 미리 보기: 장부에 보이기만(결과 폴더에 쓰지 않음)
                    return
                if d.choice not in ("all", "without"):
                    return
                choice = d.choice
                extra = [] if d.choice == "all" else costplan.without_flags(kind, plan.get("context_model", True))
            App._remember_plan(self, kind, pdf_only, priced, choice, preview=False)   # 실행: go() 안에서 결과 폴더에 남긴다(_save_ledger)
        except Exception as e:
            messagebox.showerror(APP_NAME, f"비용 창을 보이지 못해 실행하지 않습니다: {type(e).__name__}: {str(e)[:160]}"); return
        go(extra)

    def do_estimate(self):
        """[비용 미리 보기]: [▶ 한 번에 인식] 전에 뜨는 비용 창과 같은 어림을 보기만 한다(아무것도 쓰지 않고 결과 폴더도 만들지 않는다)."""
        if self._need_pdf():
            self._with_plan("run", None)

    def do_inspect(self):
        if self._need_pdf(): self.start(["inspect", self.pdf()], "① 구조 보기")

    def do_prepare(self):
        if self._need_pdf(): self.start(["prepare", self.pdf(), "--work", str(self.work())] + self._pages(), "② 준비")

    def do_read(self):
        if self._need_pdf() and self._prepare_env():
            def go(extra):
                self._save_book(create=True)                               # 실행 직전(비용 확인을 받은 뒤): 이 책의 설정을 결과 폴더에
                self.start(["read", self.pdf(), "--work", str(self.work())] + self._pages() + self._read_opts(), "③ 판독")
            self._with_plan("read", go)

    def do_build(self):
        if not self._need_pdf() or not App._corrections_ok(self):
            return
        self._env_need_key = App._context_model_enabled(self)
        if self._prepare_env() and template_ok(self) and preset_ok(self):
            def go(extra):                                                 # extra: [아니요 · 모델 없이] 면 --no-context-model
                opts = ["--reader", self.v_reader.get()] + extra + self._answered_opts()
                if self.v_model.get().strip(): opts += ["--model", self.v_model.get().strip()]
                self._save_book(create=True)                               # 실행 직전(비용 확인을 받은 뒤): 이 책의 설정을 결과 폴더에
                self.start(["build", self.pdf(), "--work", str(self.work())] + self._pages() + self._build_opts() + opts + self._cap_opts(), "④ 문서 만들기·문맥 교정")
            self._with_plan("build", go)                                   # 문서 만들기도 문맥 교정으로 모델을 부른다

    def do_review(self):
        if not self._need_records("검수 화면"): return
        rv = self.work() / "review.html"
        self.start(["review", self.pdf(), "--work", str(self.work())] + self._pages(), "⑤ 검수 화면", open_after=rv)

    def do_report(self):
        if self._need_records("보고"):
            self.nb.select(0)
            self.start(["report", self.pdf(), "--work", str(self.work())], "⑥ 보고")

    def do_run(self):
        if self._need_pdf() and App._corrections_ok(self) and self._prepare_env() and template_ok(self) and preset_ok(self):
            def go(extra):                                                 # extra: [아니요 · 모델 단계 없이] 면 --no-adjudicate(문맥 교정 모델이 켜져 있으면 + --no-context-model)
                self.nb.select(0)
                self._save_book(create=True)                               # 실행 직전(비용 확인을 받은 뒤): 이 책의 설정을 결과 폴더에
                App._save_ledger(self)                                     # 이번 어림을 결과 폴더(last_cost.json)에 — 장부
                pages = self.v_pages.get().strip()
                self.start(["run", self.pdf(), "--work", str(self.work())] + self._pages() + self._read_opts() + self._glyph_opts(suspects=True)
                           + self._answered_opts() + extra + self._build_opts(), "한 번에 인식 (전체 → HWPX)" if not pages else f"한 번에 인식 ({pages}쪽 → HWPX)")
            self._with_plan("run", go)

    def do_run_pdf(self):
        """Read the selected source and produce a searchable PDF without HWPX assembly."""
        if self._need_pdf() and self._prepare_env():
            def go(extra):
                self.nb.select(0)
                self._save_book(create=True)
                App._save_ledger(self)                                     # 이번 어림을 결과 폴더(last_cost.json)에 — 장부
                out = self.work() / "out" / f"{Path(self.pdf()).stem}_글자층.pdf"
                argv = ["run", self.pdf(), "--work", str(self.work()), "--pdf-only"]
                argv += self._pages() + self._read_opts() + self._glyph_opts(suspects=True) + self._answered_opts() + extra
                self.start(argv, "한 번에 인식 (문자 PDF 만들기)", open_after=out)
            self._with_plan("run-pdf", go)

    def do_stop(self):
        """[■ 중지](두 실행이 함께 씀): 지금 쪽이 끝나면 멈춘다. 누른 뒤 끝날 때까지 '멈추는 중…' 이고 다시 눌리지 않는다."""
        try:
            from . import cli
            cli.CANCEL.set(); self.v_status.set("중지 요청 — 지금 쪽이 끝나면 멈춥니다")
        except Exception:
            return
        self._stop_requested = True
        for b in (vars(self).get("b_stop"), vars(self).get("b_stop2")):
            if b is not None:
                b.configure(text="멈추는 중…", state="disabled")
        App._schedule_cards(self)

    def do_mark(self):
        if self._need_pdf(): MarkDialog(self)

    def do_remerge(self):
        """저장된 1차·2차 판독으로 비교·표시만 다시 계산한다(모델 호출 없음). 프로그램을 고친 뒤 기존 기록에 적용할 때 쓴다."""
        if not self._need_records("비교 다시 계산"): return
        if not messagebox.askyesno(APP_NAME, "저장된 1차·2차 판독으로 비교와 확인 표시를 다시 계산합니다(모델 호출 없음).\n"
                                             "readings/pNNN.json 의 merged 를 손으로 고친 내용은 사라집니다. 계속할까요?"):
            return
        self.nb.select(0)
        self.start(["remerge", self.pdf(), "--work", str(self.work())] + self._pages(), "비교 다시 계산")

    def do_adjudicate(self):
        """3차 글자 판별: 〔a/b〕 자리마다 글자 조각을 모델에 묻는다. 자리·합의 의심 수와 호출·금액을 비용 창으로 먼저 보이고 묻는다(셈은 작업 스레드에서)."""
        if not self._need_records("3차 글자 판별") or not self._prepare_env():
            return
        if self.v_reader.get() == "mock":
            messagebox.showinfo(APP_NAME, "3차 판별은 자형을 보는 모델이 필요합니다. Gemini·ChatGPT·Claude 중에서 고르십시오."); return
        def go(extra):
            self.nb.select(0)
            argv = ["adjudicate", self.pdf(), "--work", str(self.work()), "--reader", self.v_reader.get()] + self._pages() + (["--model", self.v_model.get().strip()] if self.v_model.get().strip() else [])
            argv += ["--workers", (self.v_workers.get().strip() or "8")] + self._glyph_opts(suspects=True) + self._answered_opts() + self._cap_opts()
            self._save_book(create=True)                                   # 실행 직전(비용 확인을 받은 뒤): 이 책의 설정을 결과 폴더에
            self.start(argv, "3차 글자 판별")
        self._with_plan("adjudicate", go)

    def do_textlayer(self):
        """검색 가능한 PDF: 판독 기록의 글을 원본 PDF 쪽 위에 보이지 않는 글자층으로 넣어 out/<이름>_글자층.pdf 로 만들고 연다(모델 호출 없음)."""
        if not self._need_records("PDF 글자층 넣기"): return
        self.nb.select(0)
        out = self.work() / "out" / f"{Path(self.pdf()).stem}_글자층.pdf"          # textlayer.out_path 와 같은 규칙(창은 pymupdf 없이도 떠야 하므로 따로 셈)
        self.start(["textlayer", self.pdf(), "--work", str(self.work())] + self._pages(), "PDF 글자층 넣기", open_after=out)

    def do_ledger(self):
        if not self._need_records("혼동 장부"): return
        self.nb.select(0)
        self.start(["ledger", self.pdf(), "--work", str(self.work())], "혼동 장부")

    def open_work(self):
        if not self._need_pdf(): return
        w = self.work()
        if not w.exists():
            messagebox.showinfo(APP_NAME, "아직 결과 폴더가 없습니다. 먼저 [▶ 한 번에 인식] 을 누르십시오."); return
        open_path(w)

    def _open_result(self, path: Path) -> None:
        """결과 파일을 연다. 실행 중이면 읽기용 사본(%TEMP%\\YeonE_OCR\\보기\\{이름}_보기_{시각})을 연다 — 실행이 끝날 때 이 파일을 다시 쓰는데
        한글·PDF 뷰어가 원본을 쥐고 있으면 저장이 막히기 때문이다(판독·3차 판별에 낸 돈이 끝의 저장 실패로 헛되지 않게)."""
        if vars(self).get("running"):
            import shutil, tempfile
            folder = Path(tempfile.gettempdir()) / APP_NAME / "보기"
            folder.mkdir(parents=True, exist_ok=True)
            copy = folder / f"{path.stem}_보기_{time.strftime('%H%M%S')}{path.suffix}"
            shutil.copy2(path, copy)
            self._append(f"실행 중이라 읽기용 사본을 열었습니다: {copy}\n")
            path = copy
        open_path(path)

    def open_hwpx(self):
        if not self._need_pdf(): return
        try:
            path = latest_result_hwpx(self.pdf(), self.work())
            if path is None:
                messagebox.showinfo(APP_NAME, f"이 원본의 완성된 HWPX가 아직 없습니다. [한 번에 인식]으로 문서를 만든 뒤 누르십시오.\n\n저장 위치: {self.work() / 'out'}")
                return
            App._open_result(self, path)
        except OSError as exc:
            messagebox.showerror(APP_NAME, f"HWPX를 열지 못했습니다. [저장 폴더]에서 파일을 확인하거나 HWPX를 지원하는 프로그램의 파일 연결을 확인하십시오.\n{exc}")

    def open_textlayer(self):
        if not self._need_pdf(): return
        out = self.work() / "out" / f"{Path(self.pdf()).stem}_글자층.pdf"
        if not out.is_file():
            messagebox.showinfo(APP_NAME, f"문자 PDF가 아직 없습니다. [한 번에 인식 (문자 PDF 만들기)]를 먼저 실행하십시오.\n\n저장 위치: {out}")
            return
        try:
            App._open_result(self, out)
        except OSError as exc:
            messagebox.showerror(APP_NAME, f"문자 PDF를 열지 못했습니다. [저장 폴더]에서 파일을 확인하거나 PDF 뷰어의 파일 연결을 확인하십시오.\n{exc}")

    def open_review(self):
        """[검수 화면]: 결과 폴더의 review.html 을 연다(열기만 함 — 만들려면 한 번에 인식 또는 고급 도구의 [⑤ 검수 화면])."""
        if not self._need_pdf(): return
        rv = self.work() / "review.html"
        if not rv.is_file():
            messagebox.showinfo(APP_NAME, "아직 검수 화면이 없습니다. [▶ 한 번에 인식] 또는 [고급 도구 ▸]의 [⑤ 검수 화면]으로 만드십시오.\n\n"
                                          f"저장 위치: {rv}")
            return
        try:
            open_path(rv)
        except OSError as exc:
            messagebox.showerror(APP_NAME, f"검수 화면을 열지 못했습니다.\n{rv}\n{exc}")

    def open_output_folder(self):
        if not self._need_pdf(): return
        out = self.work() / "out"
        if not out.is_dir():
            messagebox.showinfo(APP_NAME, f"아직 HWPX 저장 폴더가 없습니다. [한 번에 인식]으로 문서를 만든 뒤 누르십시오.\n\n저장 위치: {out}")
            return
        try:
            open_path(out)
        except OSError as exc:
            messagebox.showerror(APP_NAME, f"저장 폴더를 열지 못했습니다.\n{out}\n{exc}")

    def show_help(self):
        messagebox.showinfo(f"{APP_NAME} 도움말", HELP)

    # ---------- 업데이트 ----------
    def _save_update_preference(self):
        self.cfg["auto_update"] = self.v_auto_update.get()
        write_config(self)

    def check_updates(self, manual=True):
        if getattr(self, "_closing", False) or self._update_installing:
            return
        if not manual and not self.cfg.get("auto_update", True):
            return
        if self._update_checking:
            self._update_manual = self._update_manual or manual
            return
        self._update_checking = True
        self._update_manual = manual
        if manual:
            self._append("업데이트 확인 중…\n")

        def target():
            try:
                from .updater import check_update
                info = check_update(__version__)
                self.q.put(("update_check", info, ""))
            except Exception as exc:
                self.q.put(("update_check", None, str(exc)))
        threading.Thread(target=target, daemon=True).start()

    def _update_checked(self, info, error: str):
        self._update_checking = False
        if getattr(self, "_closing", False):
            return
        manual = self._update_manual
        if not manual and not self.cfg.get("auto_update", True):
            return
        if error:
            self._append(f"업데이트 확인을 마치지 못했습니다: {error}\n")
            if manual:
                messagebox.showwarning(APP_NAME, "업데이트를 확인하지 못했습니다. 잠시 후 다시 확인하십시오.\n\n" + error)
            return
        if info is None:
            if manual:
                messagebox.showinfo(APP_NAME, f"현재 버전 {__version__}이 최신 버전입니다.")
            return
        self._update_pending = info
        self._append(f"새 버전 {info.version}이 있습니다. 작업 중이면 작업을 마친 뒤 안내합니다.\n")

    def _offer_update(self):
        worker = getattr(self, "_worker", None)
        if (not self._update_pending or self.running or self._update_installing or self._update_prompt_open
                or getattr(self, "_closing", False) or (worker is not None and worker.is_alive())):
            return
        info, self._update_pending = self._update_pending, None
        self._update_prompt_open = True
        try:
            dialog = UpdateDialog(self, info, can_replace_executable())
            self.wait_window(dialog)
            if dialog.install:
                self._start_update(info)
        finally:
            self._update_prompt_open = False

    def _start_update(self, info):
        worker = getattr(self, "_worker", None)
        if (self.running or self._update_installing or getattr(self, "_closing", False)
                or (worker is not None and worker.is_alive())):
            self._update_pending = info
            return
        if is_installed_bundle():
            messagebox.showinfo(APP_NAME, "설치판은 릴리스 페이지에서 Setup 설치파일을 내려받아 업데이트하십시오.\n\n" + info.url)
            return
        if not can_replace_executable():
            messagebox.showinfo(APP_NAME, "소스 실행 환경에서는 자동 설치하지 않습니다. 릴리스 페이지에서 실행 파일을 내려받으십시오.\n\n" + info.url)
            return
        try:
            self.save_settings(quiet=True)
        except Exception as exc:
            messagebox.showerror(APP_NAME, "설정을 저장하지 못해 업데이트를 시작하지 않았습니다.\n\n" + str(exc))
            return
        self._update_installing = True
        self._set_running(True, "update")
        self.v_status.set(f"버전 {info.version} 다운로드·검증 중…")
        self._append(f"업데이트 {info.version} 다운로드를 시작합니다.\n")

        def target():
            try:
                from .updater import download_update
                path = download_update(info)
                self.q.put(("update_download", path, ""))
            except Exception as exc:
                self.q.put(("update_download", None, str(exc)))
        threading.Thread(target=target, daemon=True).start()

    def _update_downloaded(self, path, error: str):
        if getattr(self, "_closing", False) or not self._update_installing:
            return
        try:
            if error:
                raise RuntimeError(error)
            self.save_settings(quiet=True)
            from .updater import install_update
            install_update(Path(path), Path(sys.executable), os.getpid())
        except Exception as exc:
            self._update_installing = False
            self._set_running(False)
            self.v_status.set("업데이트 실패 — 현재 버전을 계속 사용할 수 있습니다")
            self._append(f"업데이트 실패: {exc}\n")
            messagebox.showerror(APP_NAME, "업데이트를 설치하지 못했습니다. 현재 프로그램은 계속 사용할 수 있습니다.\n\n" + str(exc))
            return
        self._closing = True
        self.destroy()                  # 도우미가 이 프로세스 종료를 확인한 뒤 교체·재시작한다.

    # ---------- 패키지 ----------
    def check_packages(self):
        frozen = bool(getattr(sys, "frozen", False))
        here = Path(sys.executable).resolve() if frozen else Path(__file__).resolve().parent.parent
        hint = "Windows 실행 파일 · 패키지 포함" if frozen else "이 위치의 YeonE_OCR.pyw 를 실행하는지 확인하십시오"
        self.q.put(("log", f"{APP_NAME} v{__version__} · 프로그램 위치: {here} · Python {sys.version.split()[0]}\n({hint})\n"))
        def t():
            self.q.put(("pkg", missing_packages()))
        threading.Thread(target=t, daemon=True).start()

    def _fit_pkg(self) -> None:
        """머리띠의 패키지 상태 글을 남은 너비에 맞춘다: 전체 → ' → ' 앞 → 그 앞을 '…' 로 줄임(상태와 수가 남게). 풍선 도움말은 늘 전체 글."""
        full = self.v_pkg.get(); lab = self._pkg_label
        self._pkg_tip.text = full
        try:
            room = lab.winfo_width()
        except tk.TclError:
            return
        f = self._font("CAP")
        head = full.split(" → ")[0]
        text = next((t for t in (full, head) if room <= 1 or f.measure(t) <= room), None)
        if text is None:                                                # 앞을 줄인다 — 상태와 수('오류 2개'·'모두 있음'·'3개')는 끝에 있다
            text = head
            while text and f.measure("…" + text) > room:
                text = text[1:]
            text = ("…" + text.lstrip()) if text else ""
        if lab.cget("text") != text:
            lab.configure(text=text)

    def _pkg_result(self, missing: list[str]):
        if is_frozen():
            if missing:
                self.v_pkg.set(f"설치 구성요소 오류 {len(missing)}개 → 설치파일로 다시 설치")
                self._append("불러오지 못한 구성요소: " + ", ".join(missing) + "\n")
                if not getattr(self, "_asked_install", False) and not self.running:
                    self._asked_install = True
                    messagebox.showerror(APP_NAME, "설치판 구성요소를 불러오지 못했습니다:\n  " + ", ".join(missing)
                                         + "\n\nYeonE_OCR 설치파일로 다시 설치하십시오. Python이나 패키지를 따로 설치할 필요는 없습니다.")
            else:
                self.v_pkg.set("설치 구성요소 모두 있음")
            return
        if missing:
            self.v_pkg.set(f"빠진 패키지 {len(missing)}개 → [고급 도구 ▸]의 [패키지 설치·점검]")       # 긴 목록은 머리띠를 밀어내므로 기록에
            self._append("빠진 패키지: " + ", ".join(missing) + "\n")
            if not getattr(self, "_asked_install", False) and not self.running:
                self._asked_install = True
                if messagebox.askyesno(APP_NAME, "필요한 패키지가 빠져 있습니다:\n  " + ", ".join(missing) + "\n\n지금 설치할까요? (몇 분 걸릴 수 있습니다)"):
                    self.install_packages()
        else:
            self.v_pkg.set("패키지 모두 있음")

    def install_packages(self):
        if self.running: return
        if is_frozen():
            self.check_packages()
            return
        req = Path(__file__).resolve().parent.parent / "requirements.txt"
        cmd = [sys.executable, "-m", "pip", "install", "--upgrade"] + (["-r", str(req)] if req.exists() else [p for _, p in PACKAGES])
        self._set_running(True, "install"); self.v_status.set("패키지 설치 중… (몇 분 걸릴 수 있음)"); self.nb.select(0)
        self.q.put(("log", "\n▶ 패키지 설치\n   " + " ".join(cmd) + "\n"))
        wanted = {p.lower().replace("_", "-") for _, p in PACKAGES}
        def t():
            rc = 1; seen: set[str] = set(); n_get = got = 0
            try:
                p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", env=child_env(),
                                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))   # 설치 스크립트가 API 키를 물려받지 않게
                for line in p.stdout:
                    self.q.put(("log", line))
                    # pip 출력의 세 마디: 패키지 확인(Collecting·Requirement already satisfied) → 내려받기(Downloading·Using cached) → 설치
                    m = re.match(r"(?:Collecting|Requirement already satisfied:)\s+([A-Za-z0-9_.\-]+)", line)
                    if m:
                        if line.startswith("Collecting"):
                            n_get += 1
                        name = m.group(1).lower().replace("_", "-")
                        if name in wanted and name not in seen:
                            seen.add(name); self.q.put(("progress", "resolve", len(seen), len(wanted)))
                    elif re.match(r"\s*(?:Downloading|Using cached)\s+\S+\.(?:whl|tar\.gz|zip)\b", line) and ".metadata" not in line:
                        got += 1                                    # 이 줄은 내려받기 '시작' — 끝난 것은 앞 파일까지
                        self.q.put(("progress", "download", min(got - 1, n_get), max(n_get, 1)))
                    elif line.startswith("Installing collected packages"):
                        self.q.put(("progress", "download", max(n_get, 1), max(n_get, 1))); self.q.put(("progress", "setup", 0, 1))
                rc = p.wait()
            except Exception:
                self.q.put(("log", traceback.format_exc()))
            self.q.put(("pkg", missing_packages()))
            if rc == 0:
                self.q.put(("log", "설치가 끝났습니다. 새 패키지가 확실히 적용되도록 프로그램을 한 번 다시 시작하십시오.\n"))
            self.q.put(("done", rc, "패키지 설치"))
        threading.Thread(target=t, daemon=True).start()

    # ---------- 쪽별 상태 ----------
    def refresh_table(self):
        """쪽별 상태 표를 새로. 판독 기록 읽기는 작업 스레드에서 하고(300~700쪽이면 창이 1~3초 멈추던 것) 줄은 큐로 받아 창 스레드가 채운다."""
        pdf = self.pdf()
        self._table_gen = gen = getattr(self, "_table_gen", 0) + 1
        if pdf != getattr(self, "_table_pdf", None):            # 다른 PDF 의 줄은 바로 비운다(같은 PDF 면 새 줄이 올 때까지 둔다 — 깜박이지 않게)
            self._fill_table(gen, pdf, [])
        if not pdf:
            return
        w = self.work()
        def t():
            try:
                rows = table_rows(w)
            except Exception:
                rows = []
            self.q.put(("table", gen, pdf, rows))
        threading.Thread(target=t, daemon=True).start()

    def _fill_table(self, gen: int, pdf: str, rows: list) -> None:
        """작업 스레드가 읽은 줄로 표를 채운다(창 스레드). 그사이 PDF 를 바꿨거나 더 새 요청이 있으면 버린다."""
        if gen != getattr(self, "_table_gen", gen) or pdf != self.pdf():
            return
        for i in self.table.get_children():
            self.table.delete(i)
        for r in rows:
            self.table.insert("", "end", values=r, tags=("odd",) if r[0] % 2 else ())
        self._table_pdf = pdf
        want = vars(self).pop("_want_row", None)                       # 쪽 띠에서 누른 쪽(표가 비어 있었을 때)
        if want is not None:
            for iid in self.table.get_children():
                vals = self.table.item(iid, "values")
                if vals and str(vals[0]) == str(want):
                    self.table.see(iid); self.table.selection_set(iid); break

    def on_close(self):
        pending = False
        for window in list(vars(self).get("_comparison_windows", ())):
            if window.winfo_exists() and not window.request_close():
                if not window.busy:
                    self.v_status.set("비교·교정 창의 저장 상태를 확인하세요. 복구 사본은 보존되어 있습니다.")
                    window.lift()
                    return
                pending = True
        if pending:
            self.after(200, self.on_close)
            return
        if getattr(self, "_update_installing", False) and not getattr(self, "_closing", False):
            if not messagebox.askyesno(APP_NAME, "업데이트 다운로드를 취소하고 닫을까요?\n현재 실행 파일은 바뀌지 않습니다."):
                return
            self.save_settings(quiet=True)
            self._closing = True
            self.destroy()
            return
        if self.running and not getattr(self, "_closing", False):
            if not messagebox.askyesno(APP_NAME, "작업이 진행 중입니다. 그래도 닫을까요?\n(지금 쓰는 쪽을 마저 쓰고 멈춥니다 — 몇 초 걸릴 수 있습니다)"):
                return
            # 바로 끊지 않는다: 작업 스레드(daemon)가 기록을 쓰는 중에 창이 사라지면 쓰다 만 파일이 남는다 → 중지를 걸고 끝나기를 잠시 기다린다
            self._closing = True; self._closing_since = time.time()
            try:
                from . import cli
                cli.CANCEL.set()
            except Exception:
                pass
            self.v_status.set("멈추는 중… (지금 쪽을 마저 쓰면 닫힙니다)")
            self._close_when_idle(); return
        self.save_settings(quiet=True)
        self._closing = True
        self.destroy()

    def _close_when_idle(self):
        w = getattr(self, "_worker", None)
        active = (w is not None and w.is_alive()) or vars(self).get("_bundle_in_progress", False)
        if keep_waiting(active, self._closing_since, time.time()):
            self.after(200, self._close_when_idle); return
        self.save_settings(quiet=True)
        self.destroy()


def main():
    app = App()
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
