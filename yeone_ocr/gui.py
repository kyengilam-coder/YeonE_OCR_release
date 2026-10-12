"""YeonE_OCR 창 프로그램. 모든 기능을 단추로 조작한다.

표준 라이브러리 tkinter 만 쓰므로 Python 만 있으면 뜬다. 무거운 패키지(pymupdf, hwpx, google-genai…)는
단추를 눌렀을 때 비로소 불러온다. Windows 실행 파일에는 포함되며, Python 소스 실행에서 빠졌으면
[▸ 고급 → 패키지 설치·점검] 단추로 설치한다.
"""
from __future__ import annotations
import contextlib, io, json, os, queue, re, subprocess, sys, threading, time, traceback, webbrowser
from pathlib import Path
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, simpledialog

from . import __version__
from .progress import Tracker, fmt_duration, fmt_elapsed

IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp", ".gif", ".jp2", ".j2k", ".jpx")   # pdfio.IMAGE_EXTS 와 같게(창은 pymupdf 없이도 떠야 하므로 따로 둠)

APP_NAME = "YeonE_OCR"
CONFIG_PATH = Path.home() / ".yeone_ocr" / "config.json"
PACKAGES = [("pymupdf", "pymupdf"), ("PIL", "pillow"), ("numpy", "numpy"), ("cv2", "opencv-python-headless"),
            ("pydantic", "pydantic"), ("google.genai", "google-genai"), ("hwpx", "python-hwpx"), ("docx", "python-docx"), ("opencc", "opencc-python-reimplemented"), ("hanja", "hanja")]
DEFAULTS = {"pages": "", "print_offset": "", "upscale": "2", "reader": "gemini", "model": "gemini-3.1-pro-preview",
            "api_key": "", "second_pass": True, "docx": False, "join_pages": True, "force": False, "highlight": True, "page_breaks": True,
            "prices": {}, "fx_krw": "", "workers": 8, "second_model": "auto", "checkpoint": 20, "use_cache": True, "second_thinking_low": True, "adjudicate_blind": False, "check_list": "none", "last_pdf": "", "hwpx": "", "collate_pdf": "", "anchor_source": "auto", "heading_styles": "", "collate_policy": "sure", "collate_memo": "notice", "memo_mode": "notice", "context_hint": True, "collate_vertical": False,
            "chunk_lines": 8, "media_res": "high", "mode": "auto", "advanced_open": False, "details_open": False, "auto_update": True}
CHECK_LIST_LABELS = {"none": "없음(본문만)", "extra": "본문에 표시 못 한 것만", "all": "전부(검수 화면과 같음)"}
from .collate.labels import MEMO_MODES as MEMO_LABELS          # 한컴 메모를 어디에 달지(패키지 없이도 창이 뜨게 가벼운 모듈에서)
MODE_LABELS = {"auto": "자동 판정", "vertical": "세로쓰기 한자·한글", "horizontal_zh": "가로쓰기 한문(간체)"}
MRES_LABELS = {"default": "기본", "high": "높음(high)", "ultra_high": "최고(ultra_high · Gemini 3)"}   # 2차 조각 이미지 토큰 예산
PAGE_THINK_LABELS = {"auto": "자동(모델 기본)", "low": "낮음(low)", "medium": "중간(medium)", "high": "높음(high)"}         # 1차(쪽 전체) 생각 수준 --page-thinking
PAGE_MRES_LABELS = {"auto": "자동(설정 없음)", "high": "높음(high)", "ultra_high": "최고(ultra_high · Gemini 3)"}          # 1차 쪽 이미지 토큰 예산 --page-media-resolution
STATUSES = ["원본 대조 완료(사람)", "판독 확인 필요", "이미지 보존 확인 완료", "문서 반영 완료", "파일 구조 검사 완료"]

HELP = f"""{APP_NAME} · 연어OCR {__version__}

한자·한글 혼용 문헌(세로쓰기 스캔 PDF, 가로쓰기 한문 책)을 지침에 따라 OCR·교정해 HWPX 로 만듭니다.
글자는 Gemini 가 읽고, 프로그램은 영역 잘라내기·두 판독 비교·문서 조립·상태 기록을 맡습니다.
맨 위의 [일반 OCR] 은 PDF → HWPX, [책갈피 OCR] 은 이미 입력된 HWPX 를 PDF 와 대조해 고친 사본을 만듭니다.

쓰는 법은 세 단계입니다. ① API 키 넣기(처음 한 번) → ② [PDF·이미지 고르기…] → ③ [▶ 한 번에 인식].
읽기 방향·인쇄 쪽수 차이·이전 판 기록 갱신·2차 비교·3차 글자 판별·문서·검수 화면·보고·혼동 장부를 프로그램이 알아서 이어서 합니다.
진행 막대는 처음부터 끝까지 한 방향으로 차며, 오른쪽에 단계·쪽 수·남은 시간이 나옵니다.
2차 판독 모델·속도·조각 같은 설정은 판독 칸의 [세부 설정 ▾] 에, 단계별 단추는 [▸ 고급] 에 접혀 있습니다. 단추에 마우스를 올리면 하는 일이 나옵니다.

차례
 1. Windows 실행 파일에는 패키지가 들어 있습니다. Python 소스로 실행하면 [패키지 설치·점검](고급 안)으로 설치할 수 있습니다.
 2. API 키를 넣고 [키 확인·모델 고르기] — 키가 맞는지 확인하고 쓸 수 있는 모델을 고릅니다. (https://aistudio.google.com/apikey)
 3. [PDF·이미지 고르기…] 로 파일을 고릅니다. 스캔 이미지(PNG·JPG·TIFF 등)도 됩니다 — 여러 장을 함께 고르면 이름의 숫자 순서로
    한 권(PDF)으로 묶어 읽습니다. 쪽 범위는 비워 두면 전체입니다. 읽기 방향(세로쓰기 한자·한글 / 가로쓰기 한문)과
    인쇄 쪽수 차이는 PDF 글자층으로 자동 판정합니다("자동 판정"·빈 칸 그대로). 이미지는 글자층이 없어 세로쓰기로 보므로
    가로쓰기 책이면 읽기 방향을 직접 고르십시오.
 4. [▶ 한 번에 인식 (PDF → HWPX)] 를 누르면 먼저 예상 호출·토큰·비용을 보여 주고([비용 미리 보기] 와 같음), [계속 실행] 을 누르면
    준비 → 판독(1차·2차) → 3차 글자 판별 → 문서 만들기 → 검수 화면 → 보고·혼동 장부 까지 한 번에 갑니다. 단가를 한 번 넣어 두면 금액도 나옵니다.
    이전 판으로 만든 판독 기록이 있으면 지금 규칙으로 저절로 다시 비교합니다(모델 호출 없음).
    쪽이 많으면 오래 걸립니다(쪽당 30초~1분). [■ 중지] 로 멈춰도 끝난 쪽은 남고, 다시 누르면 남은 쪽부터 이어서 합니다.
 5. 끝나면 검수 화면을 열지 묻습니다. [HWPX 열기] 로 결과를 보고 검수 화면에서 원본과 대조합니다. 〔 〕 로 남은 자리만 사람이 보면 됩니다.
 6. 책갈피 OCR 탭: 이미 입력된 HWPX 가 있으면 [PDF 고르기…] → [HWPX 고르기…] → [▶ HWPX 전체 교정]. 구획을 차례로 전부 대조해
    사본(<이름>_교정.hwpx)에 고쳐 넣고 요약을 남깁니다. 入/人 같은 자형 편향 자리는 글자 조각을 모델에 다시 물어 가립니다.
 7. "▸ 고급 — 단계별 실행·도구" 를 펼치면 ①~⑥ 단계별 단추와 [3차 글자 판별]·[혼동 장부]·[비교 다시 계산]·[검수 결과 기록…] 이 보입니다.
    글자를 손으로 고치려면 결과 폴더의 readings/pNNN.json 을 고친 뒤 [④ 문서 만들기] 를 다시 누릅니다. 사람이 대조를 마친 쪽은 [검수 결과 기록…] 으로 남깁니다.
 8. 시작할 때 새 버전을 확인하며, 위의 [업데이트 확인]으로 직접 확인할 수도 있습니다. [설치 후 재시작]을 고르면
    실행 파일만 교체합니다. API 키·설정·판독 기록은 유지됩니다. 자동 확인은 고급에서 끌 수 있습니다.

결과는 PDF 와 같은 폴더의 「<PDF 이름>_YeonE_OCR」 안에 생깁니다.
 out/      HWPX (+DOCX)
 review.html  검수 화면      report.md  상태 보고
 readings/ 쪽별 판독 기록    crops/     이미지로 보존한 도판

〔 〕 표시는 확인이 필요한 자리입니다. 프로그램은 추측으로 채우지 않습니다.
"""


SPEED_DEFAULTS_VERSION = 6  # 2: 0.4.1 동시 6(자동 조절)·2차 모델 auto·2차 생각 줄이기 / 3: 0.4.4 쪽 끝 확인 목록 없음 / 4: 0.6.0 조각 8줄 묶음·2차 조각 고해상 / 5: 0.6.2 조각 해상도 3단(media_res) / 6: 0.6.8 동시 8·책갈피 교정 정책 '확실한 것만'
CLOSE_WAIT_SEC = 15.0       # 작업 중 창을 닫을 때 작업 스레드가 쪽을 마저 쓰기를 기다리는 한도


def upgrade_config(cfg: dict) -> dict:
    """이전 판이 저장한 설정에 새 속도 기본값을 입힌다(사용자가 바꾼 값은 그대로)."""
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
    return cfg


def load_config() -> dict:
    cfg = dict(DEFAULTS)
    try:
        cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
    except Exception:
        pass
    return upgrade_config(cfg)


def save_config(cfg: dict) -> None:
    """설정 파일에는 API 키가 그대로 들어가므로 본인만 읽게(0o600) 만든다 — Windows 에서는 뜻이 없고 그 밖에서는 맞는 권한."""
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(CONFIG_PATH, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(json.dumps(cfg, ensure_ascii=False, indent=1))
    try:
        os.chmod(CONFIG_PATH, 0o600)                           # 이전 판이 만든 파일도
    except OSError:
        pass


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
        return p.with_name(p.name + "_이미지_YeonE_OCR")
    if p.suffix.lower() not in ("", ".pdf"):
        return p.with_name(f"{p.stem}_{p.suffix.lower().lstrip('.')}_YeonE_OCR")
    return p.with_name(p.stem + "_YeonE_OCR")


SECRET_ENV = ("GEMINI_API_KEY", "GOOGLE_API_KEY")


def child_env() -> dict:
    """바깥 프로그램(pip·xdg-open·open)에 넘길 환경 — API 키는 뺀다(설치 스크립트나 열린 프로그램이 키를 물려받지 않게)."""
    return {k: v for k, v in os.environ.items() if k.upper() not in SECRET_ENV}


def open_path(path: str | Path) -> None:
    path = str(path)
    if sys.platform.startswith("win"):
        os.startfile(path)                      # type: ignore[attr-defined]   (ShellExecute 는 환경을 따로 받지 않는다)
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
            head += f"\n지금 '{cur}': " + ("호출 확인됨" if probe.get(cur) == "호출 확인됨" else ("목록에 있지만 호출 거부됨 — " + probe.get(cur, "") if cur in models else "목록에 없음"))
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
        ttk.Label(f, style="Panel.TLabel", text="위쪽이 최신입니다. 판독 품질은 pro 계열이 낫고, flash 계열은 빠르고 쌉니다. preview·exp 는 시험판입니다.").grid(row=2, column=0, columnspan=2, sticky="w")
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


class EstimateDialog(tk.Toplevel):
    """판독 전 비용 어림. 큰 글씨의 금액 → 단계별 표 → 바탕 정보 순으로 보여 주고, 단가·환율 고치기는 접어 둔다.
    go=True 면 [계속 실행] 단추로 실행을 잇는다."""
    def __init__(self, app: "App", e: dict, go: bool = False):
        super().__init__(app)
        self.app = app; self.proceed = False; self.e = e
        C = app.colors
        self.title("비용 미리 보기"); self.resizable(False, False); self.configure(bg=C["PANEL"])
        base = app.font_base[0]
        P = app.px
        f = ttk.Frame(self, padding=(P(20), P(16), P(20), P(14)), style="Panel.TFrame"); f.grid(sticky="nsew")
        # 머리: 금액
        self.v_cap, self.v_head, self.v_usd, self.v_sub = tk.StringVar(), tk.StringVar(), tk.StringVar(), tk.StringVar()
        tk.Label(f, textvariable=self.v_cap, font=app.font_small, fg=C["MUTED"], bg=C["PANEL"]).grid(row=0, column=0, columnspan=2, sticky="w")
        tk.Label(f, textvariable=self.v_head, font=(base, 24, "bold"), fg=C["ACC"], bg=C["PANEL"]).grid(row=1, column=0, sticky="w")
        tk.Label(f, textvariable=self.v_usd, font=(base, 12), fg=C["MUTED"], bg=C["PANEL"]).grid(row=1, column=1, sticky="sw", padx=(12, 0), pady=(0, 5))
        tk.Label(f, textvariable=self.v_sub, font=app.font_base, fg=C["INK"], bg=C["PANEL"]).grid(row=2, column=0, columnspan=2, sticky="w", pady=(2, 12))
        # 단계별 표
        cols = ("step", "model", "calls", "tin", "tout", "cost")
        self.tv = ttk.Treeview(f, columns=cols, show="headings", height=3, selectmode="none")
        for c, (t, w, a) in {"step": ("단계", 160, "w"), "model": ("모델", 230, "w"), "calls": ("호출", 60, "e"),
                             "tin": ("입력 토큰", 110, "e"), "tout": ("출력 토큰", 100, "e"), "cost": ("비용", 140, "e")}.items():
            self.tv.heading(c, text=t, anchor=a); self.tv.column(c, width=P(w), anchor=a, stretch=(c == "model"))
        self.tv.tag_configure("sum", font=app.font_h)
        self.tv.grid(row=3, column=0, columnspan=2, sticky="we")
        f.columnconfigure(0, weight=1)
        # 바탕 정보
        self.v_info = tk.StringVar(); self.v_note = tk.StringVar()
        ttk.Label(f, textvariable=self.v_info, style="Hint.TLabel", wraplength=P(800), justify="left").grid(row=4, column=0, columnspan=2, sticky="w", pady=(P(8), 0))
        ttk.Label(f, textvariable=self.v_note, style="Hint.TLabel", wraplength=P(800), justify="left").grid(row=5, column=0, columnspan=2, sticky="w", pady=(P(2), 0))
        # 단가·환율 고치기(접힘)
        self.adj = ttk.Frame(f, style="Panel.TFrame", padding=(0, 10, 0, 0))
        self.e_in = ttk.Entry(self.adj, width=7); self.e_out = ttk.Entry(self.adj, width=7); self.e_fx = ttk.Entry(self.adj, width=7)
        self.l_model = ttk.Label(self.adj, style="Panel.TLabel", text=""); self.l_model.grid(row=0, column=0, sticky="w")
        ttk.Label(self.adj, style="Panel.TLabel", text="입력 $").grid(row=0, column=1, sticky="e", padx=(10, 2)); self.e_in.grid(row=0, column=2)
        ttk.Label(self.adj, style="Panel.TLabel", text="출력 $").grid(row=0, column=3, sticky="e", padx=(10, 2)); self.e_out.grid(row=0, column=4)
        ttk.Label(self.adj, style="Panel.TLabel", text="원/달러").grid(row=0, column=5, sticky="e", padx=(10, 2)); self.e_fx.grid(row=0, column=6)
        ttk.Button(self.adj, text="이 값으로 다시 계산", command=self.save).grid(row=0, column=7, padx=(12, 4))
        ttk.Button(self.adj, text="자동값으로", command=self.reset).grid(row=0, column=8)
        ttk.Label(self.adj, text="100만 토큰당 미국 달러. 요금표 https://ai.google.dev/pricing 과 다를 때만 고치십시오. 고친 값은 이 모델에 저장됩니다.",
                  style="Hint.TLabel").grid(row=1, column=0, columnspan=9, sticky="w", pady=(4, 0))
        self.adj_shown = False
        # 단추
        b = ttk.Frame(f, style="Panel.TFrame"); b.grid(row=7, column=0, columnspan=2, sticky="we", pady=(14, 0))
        if go:
            ttk.Button(b, text="▶  계속 실행", command=self.ok, style="Accent.TButton").pack(side="left")
        ttk.Button(b, text="취소" if go else "닫기", command=self.destroy).pack(side="left", padx=(8, 0))
        self.b_adj = ttk.Button(b, text="단가·환율 고치기 ▾", command=self.toggle_adj); self.b_adj.pack(side="right")
        self.render(e)
        self.transient(app); self.grab_set()

    # ----- 그리기 -----
    def render(self, e: dict):
        from .estimate import fmt_tokens, fmt_krw, resolve_price
        self.e = e
        pages = f"{e['pages']}쪽" + (f"(이미 판독한 {e['already_read']}쪽 제외)" if e.get("already_read") else "")
        if e["usd_low"] is None:
            self.v_cap.set("예상 비용"); self.v_head.set("금액을 낼 수 없음"); self.v_usd.set("")
            self.v_sub.set(f"{pages} · {e['model']} 은 내장 요금표에 없는 모델입니다. 아래에서 단가를 넣으면 계산합니다.")
        else:
            self.v_cap.set("예상 비용 (범위는 생각 토큰 양에 따른 편차)")
            self.v_head.set(f"약 {fmt_krw(e['krw_low'])} ~ {fmt_krw(e['krw_high'])}")
            self.v_usd.set(f"$ {e['usd_low']:.2f} ~ {e['usd_high']:.2f}")
            basis = "기본값으로 어림" if e["basis"] == "기본값" else e["basis"] + "으로 보정"
            self.v_sub.set(f"{pages} · 1차 {e['model']}" + (f" · 2차 {e['second_model']}" if e.get("second_model") and e["second_model"] != e["model"] else "") + f" · {basis}")
        for i in self.tv.get_children():
            self.tv.delete(i)
        self.tv.configure(height=max(1, len(e.get("rows") or [1]) + (1 if e.get("rows") else 0)))   # 단계 줄 + 합계 줄이 다 보이게
        tot = {"calls": 0, "in_low": 0, "in_high": 0, "out_low": 0, "out_high": 0, "usd_low": 0.0, "usd_high": 0.0}
        for r in e.get("rows") or []:
            self.tv.insert("", "end", values=(r["label"], r["model"], f"{r['calls']:,}", f"{fmt_tokens(r['in_low'])}~{fmt_tokens(r['in_high'])}",
                                              f"{fmt_tokens(r['out_low'])}~{fmt_tokens(r['out_high'])}", f"${r['usd_low']:.2f}~{r['usd_high']:.2f}"))
            for k in tot:
                tot[k] += r.get(k, 0)
        if e.get("rows"):
            self.tv.insert("", "end", tags=("sum",), values=("합계", "", f"{tot['calls']:,}", f"{fmt_tokens(tot['in_low'])}~{fmt_tokens(tot['in_high'])}",
                                                             f"{fmt_tokens(tot['out_low'])}~{fmt_tokens(tot['out_high'])}", f"${tot['usd_low']:.2f}~{tot['usd_high']:.2f}"))
        else:
            self.tv.insert("", "end", values=("전체", e["model"], f"{e['calls']:,}", f"{fmt_tokens(e['in_low'])}~{fmt_tokens(e['in_high'])}",
                                              f"{fmt_tokens(e['out_low'])}~{fmt_tokens(e['out_high'])}", "-"))
        self._fit_columns()
        info = []
        if e.get("fx"):
            info.append(f"환율 {e['fx']:,.0f}원/달러 ({e['fx_source']})")
        if e.get("price_source"):
            info.append(f"단가 {e['price_source']}")
        info.append("지침 캐시 " + ("적용" if e.get("use_cache") else "없음"))
        if e.get("page_px"):
            info.append(f"쪽 이미지 {e['page_px'][0]}×{e['page_px'][1]} · 확대 {e.get('upscale', 2)}배")
        self.v_info.set(" · ".join(info))
        self.v_note.set("추정치입니다. 출력 토큰에는 생각(thinking) 토큰이 들어가며, 캐시 보관료(시간당 몇 센트)는 넣지 않았습니다.")
        model = self.app.v_model.get().strip(); pr = resolve_price(model, self.app.cfg.get("prices") or {}) or {}
        fx, _ = self.app.fx_setting()
        self.l_model.configure(text=f"'{model}' 단가")
        for ent, v in ((self.e_in, f"{pr['in']:g}" if pr else ""), (self.e_out, f"{pr['out']:g}" if pr else ""), (self.e_fx, f"{fx:.0f}" if fx else "")):
            ent.delete(0, "end"); ent.insert(0, v)

    def _fit_columns(self):
        """칸 너비를 가장 긴 값(합계 줄은 굵은 글씨)에 맞춘다 — 화면 배율이 커도 '1,200만~1,800만' 이 잘리지 않게."""
        try:
            from tkinter import font as tkfont
            reg, bold = tkfont.Font(self, font=self.app.font_base), tkfont.Font(self, font=self.app.font_h)
            pad = self.app.px(22)
            for k, c in enumerate(self.tv["columns"]):
                need = bold.measure(self.tv.heading(c, "text"))
                for it in self.tv.get_children():
                    v = str(self.tv.item(it, "values")[k]); f = bold if "sum" in self.tv.item(it, "tags") else reg
                    need = max(need, f.measure(v))
                if need + pad > int(self.tv.column(c, "width")):
                    self.tv.column(c, width=need + pad)
        except (tk.TclError, IndexError):
            pass

    def toggle_adj(self):
        self.adj_shown = not self.adj_shown
        if self.adj_shown:
            self.adj.grid(row=6, column=0, columnspan=2, sticky="we"); self.b_adj.configure(text="단가·환율 접기 ▴")
        else:
            self.adj.grid_forget(); self.b_adj.configure(text="단가·환율 고치기 ▾")

    # ----- 단추 -----
    def save(self):
        model = self.app.v_model.get().strip()
        prices = dict(self.app.cfg.get("prices") or {})
        def num(s):
            try: return float(s.strip()) if s.strip() else 0.0
            except ValueError: return 0.0
        prices[model] = {"in": num(self.e_in.get()), "out": num(self.e_out.get())}
        self.app.cfg["prices"] = prices; self.app.cfg["fx_krw"] = self.e_fx.get().strip(); save_config(self.app.cfg)
        self.render(self.app.estimate_data())

    def reset(self):
        model = self.app.v_model.get().strip()
        prices = dict(self.app.cfg.get("prices") or {}); prices.pop(model, None)
        self.app.cfg["prices"] = prices; self.app.cfg["fx_krw"] = ""; save_config(self.app.cfg)
        self.render(self.app.estimate_data())

    def ok(self):
        self.proceed = True; self.destroy()


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
                       if can_install else "Python 소스로 실행 중입니다. 릴리스 페이지에서 Windows 실행 파일을\n내려받을 수 있습니다. 현재 소스 파일은 자동으로 교체하지 않습니다.")
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
        if self.win is not None or not self.w.winfo_exists():
            return
        C, P = self.app.colors, self.app.px
        self.win = tw = tk.Toplevel(self.w); tw.wm_overrideredirect(True)
        try: tw.attributes("-topmost", True)
        except tk.TclError: pass
        tk.Label(tw, text=self.text, bg=C["INK"], fg="#f7f1e6", font=self.app.font_small, justify="left", wraplength=P(380), padx=P(10), pady=P(6)).pack()
        tw.update_idletasks()
        x = min(self.w.winfo_rootx() + P(6), self.w.winfo_screenwidth() - tw.winfo_reqwidth() - 8)
        y = self.w.winfo_rooty() + self.w.winfo_height() + P(6)
        tw.geometry(f"+{max(0, x)}+{y}")


TAB_HINTS = ("스캔 PDF·이미지를 지침대로 판독해 HWPX 문서로 만듭니다", "이미 입력된 HWPX를 PDF 원본과 대조해 고친 사본을 만듭니다")


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f"{APP_NAME} · 연어OCR {__version__}")
        self.cfg = load_config()
        self._style()
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        w, h = min(self.px(1180), sw - 40), min(self.px(980), sh - 80)
        self.geometry(f"{w}x{h}"); self.minsize(min(self.px(960), w), min(self.px(640), h))
        self.q: queue.Queue = queue.Queue()
        self.running = False
        self._update_checking = False
        self._update_installing = False
        self._update_pending = None
        self._update_prompt_open = False
        self.open_after: Path | None = None
        self.buttons: list[ttk.Button] = []
        self.tracker = Tracker()
        self._prog_shown = 0.0
        self._build()
        self.after(100, self._poll)
        self.after(400, self.check_packages)
        self.after(1800, lambda: self.check_updates(manual=False))
        self._fx, need_fx = fx_from_config(self.cfg, time.strftime("%Y-%m-%d"))     # 환율은 하루 한 번만 조회(받은 값은 설정에 남김)
        if need_fx:
            threading.Thread(target=self._fetch_fx, daemon=True).start()
        if self.cfg.get("last_pdf") and Path(self.cfg["last_pdf"]).exists():
            self.v_pdf.set(self.cfg["last_pdf"]); self.refresh_table()
        self.protocol("WM_DELETE_WINDOW", self.on_close)

    def px(self, v: float) -> int:
        """화면 배율에 맞춘 픽셀 수(글꼴은 Tk 가 알아서 키우지만 여백·그림은 직접 곱해야 한다)."""
        return max(1, int(round(v * getattr(self, "scale", 1.0))))

    # ---------- 화면 ----------
    FONT_CANDIDATES = ("Pretendard", "Pretendard Variable", "맑은 고딕", "Malgun Gothic", "Apple SD Gothic Neo", "Noto Sans CJK KR", "Noto Sans KR", "NanumGothic", "나눔고딕")
    MONO_CANDIDATES = ("D2Coding", "D2Coding ligature", "Cascadia Mono", "Consolas", "Noto Sans Mono CJK KR", "Menlo", "DejaVu Sans Mono")

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
        """종이색 바탕에 옻칠색 강조. clam 테마 위에 둥근 단추·카드·입력칸, ✓ 체크 상자, 끝까지 차는 진행 막대를
        그림(gui_art)으로 입힌다(0.6.3). 단추는 세 단계 위계 — 강조(실행) · 보통(테두리 단추) · 글자 단추(펼치기)."""
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
        self.font_base, self.font_small, self.font_h = (base, 10), (base, 9), (base, 10, "bold")
        self.font_title, self.font_sub, self.font_big = (base, 17, "bold"), (base, 10), (base, 12, "bold")
        self.font_card, self.font_btn_h = (base, 11, "bold"), (base, 11, "bold")
        self.font_mono = (mono, 10)
        # 기본 글꼴을 한글 글꼴로 바꾼다. (option_add("*Font") 는 ttk 이름표의 스타일 글꼴까지 덮어써 카드 제목·안내 글의 굵기·크기가 사라졌다)
        try:
            from tkinter import font as tkfont
            for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont", "TkCaptionFont", "TkSmallCaptionFont", "TkIconFont", "TkTooltipFont"):
                tkfont.nametofont(name, root=self).configure(family=base)
            tkfont.nametofont("TkDefaultFont", root=self).configure(size=10); tkfont.nametofont("TkTextFont", root=self).configure(size=10)
        except Exception:
            self.option_add("*Font", self.font_base)
        self.option_add("*TCombobox*Listbox.font", self.font_base)
        C = self.colors = dict(BG="#f3efe7", PANEL="#fdfcf9", LINE="#e3dbcc", FLINE="#d0c6b4", EDGE="#9d917f", FOCUS="#9a7637", INK="#2a221d", MUTED="#6b6155",
                               ACC="#3d1d16", ACC_HI="#5c2c21", ACC_DN="#2a130e", GOLD="#b8934e", RED="#b4271f", RED_SOFT="#fbeceb",
                               GREEN="#2f6f4a", FIELD="#ffffff", BTN="#ffffff", BTN_HI="#f8f2e6", BTN_DN="#efe5d1",
                               TRACK="#e7e0d1", TRACK_HI="#dcd2bf", SEL="#f1e5c9", STRIPE="#faf7f0", HEAD="#f4efe4",
                               DIS="#f5f2ec", DIS_LINE="#e6e0d5", DIS_INK="#ada395")
        self.configure(bg=C["BG"])
        self.option_add("*TCombobox*Listbox.background", C["FIELD"])
        self.option_add("*TCombobox*Listbox.selectBackground", C["SEL"])
        self.option_add("*TCombobox*Listbox.selectForeground", C["INK"])
        self._img: dict[str, tk.PhotoImage] = {}

        def img(key: str, b64: str) -> tk.PhotoImage:
            self._img[key] = tk.PhotoImage(master=self, data=b64)
            return self._img[key]

        bw = 1.0 * self.scale
        # ---- 바탕·글자 ----
        st.configure(".", background=C["BG"], foreground=C["INK"], font=self.font_base, bordercolor=C["LINE"], troughcolor=C["TRACK"],
                     focuscolor=C["BG"], selectbackground=C["SEL"], selectforeground=C["INK"], insertcolor=C["INK"])
        st.configure("TFrame", background=C["BG"]); st.configure("Panel.TFrame", background=C["PANEL"])
        st.configure("TLabel", background=C["BG"], foreground=C["INK"])
        st.configure("Panel.TLabel", background=C["PANEL"])
        st.configure("Field.TLabel", background=C["PANEL"], foreground=C["INK"])
        st.configure("Hint.TLabel", background=C["PANEL"], foreground=C["MUTED"], font=self.font_small)
        st.configure("PageHint.TLabel", background=C["BG"], foreground=C["MUTED"], font=self.font_small)
        st.configure("CardTitle.TLabel", background=C["PANEL"], foreground=C["ACC"], font=self.font_card)
        st.configure("Section.TLabel", background=C["PANEL"], foreground=C["MUTED"], font=self.font_h)
        st.configure("Status.TLabel", background=C["BG"], foreground=C["ACC"], font=self.font_h)
        st.configure("Prog.TLabel", background=C["BG"], foreground=C["MUTED"], font=self.font_base)
        # 가는 선: clam 의 구분선은 홈 파인 두 줄(짙은 그림자)로 그려져 무거워 보이므로 한 줄짜리 그림으로 바꾼다
        lh = max(1, P(1))
        st.element_create("Rule.line", "image", img("rule", A.rounded_box(64, lh, 0, C["LINE"])), sticky="ew", height=lh)
        st.layout("TSeparator", [("Rule.line", {"sticky": "ew"})]); st.layout("Horizontal.TSeparator", [("Rule.line", {"sticky": "ew"})])
        # ---- 카드(둥근 모서리 판) ----
        r = P(10); nw, bd_ = A.stretch_size(r, 192); nh, _ = A.stretch_size(r, 64)
        st.element_create("Card.border", "image", img("card", A.rounded_box(nw, nh, r, C["PANEL"], C["LINE"], bw)), border=bd_, sticky="nsew", width=2 * bd_, height=2 * bd_)
        st.layout("Card.TFrame", [("Card.border", {"sticky": "nsew"})])
        st.configure("Card.TFrame", background=C["BG"])

        # ---- 단추: 강조(실행) · 보통(테두리) · 중지 ----
        def button_style(name: str, look: dict, pad, font, fg: str, fg_dis: str, on: str):
            r = P(7); nw, bd_ = A.stretch_size(r, 96); nh, _ = A.stretch_size(r, 24)
            ims = {k: img(f"{name}_{k}", A.rounded_box(nw, nh, r, fill, line, bw)) for k, (fill, line) in look.items()}
            el = f"{name}.border"
            st.element_create(el, "image", ims["normal"], ("disabled", ims["disabled"]), ("pressed", ims["pressed"]),
                              ("active", ims["active"]), ("focus", ims["focus"]), border=bd_, sticky="nsew", width=2 * bd_, height=2 * bd_, padding=P(2))
            sname = "TButton" if name == "Btn" else f"{name}.TButton"
            st.layout(sname, [(el, {"sticky": "nsew", "children": [("Button.padding", {"sticky": "nsew", "children": [("Button.label", {"sticky": "nsew"})]})]})])
            st.configure(sname, padding=pad, font=font, foreground=fg, background=on, anchor="center")
            st.map(sname, foreground=[("disabled", fg_dis)], background=[("active", on), ("pressed", on), ("disabled", on)])

        button_style("Btn", {"normal": (C["BTN"], C["FLINE"]), "active": (C["BTN_HI"], C["GOLD"]), "pressed": (C["BTN_DN"], C["GOLD"]),
                             "disabled": (C["DIS"], C["DIS_LINE"]), "focus": (C["BTN"], C["FOCUS"])}, (P(14), P(6)), self.font_base, C["INK"], C["DIS_INK"], C["PANEL"])
        button_style("Accent", {"normal": (C["ACC"], None), "active": (C["ACC_HI"], None), "pressed": (C["ACC_DN"], None),
                                "disabled": ("#c2b4a9", None), "focus": (C["ACC"], C["FOCUS"])}, (P(24), P(9)), self.font_btn_h, "#fff8ec", "#f7f1e8", C["PANEL"])
        button_style("Stop", {"normal": (C["BTN"], "#e5c0bb"), "active": (C["RED_SOFT"], C["RED"]), "pressed": ("#f6dcd8", C["RED"]),
                              "disabled": (C["DIS"], C["DIS_LINE"]), "focus": (C["BTN"], C["RED"])}, (P(18), P(9)), self.font_btn_h, C["RED"], C["DIS_INK"], C["PANEL"])
        st.configure("TButton", width=-6)                            # clam 기본 최소 11글자 → 짧은 단추([전체]·[보기])가 지나치게 넓지 않게
        st.configure("Tool.TButton", padding=(P(10), P(6)))
        st.configure("Page.TButton", background=C["BG"]); st.map("Page.TButton", background=[("active", C["BG"]), ("pressed", C["BG"]), ("disabled", C["BG"])])
        # 글자 단추(펼치기·접기): 테두리 없이 강조색 글자
        for sname, bg in (("Link.TButton", C["PANEL"]), ("PageLink.TButton", C["BG"])):
            st.layout(sname, [("Button.padding", {"sticky": "nsew", "children": [("Button.focus", {"sticky": "nsew", "children": [("Button.label", {"sticky": "nsew"})]})]})])
            st.configure(sname, background=bg, foreground=C["ACC"], font=self.font_h, padding=(P(4), P(3)), width=0, focuscolor=C["FOCUS"], focusthickness=max(1, P(1)))
            st.map(sname, foreground=[("disabled", C["DIS_INK"]), ("active", C["ACC_HI"])], background=[("active", bg)],
                   font=[("active", (self.font_h[0], self.font_h[1], "bold", "underline"))])
        # ---- 체크 상자(✓)·라디오 ----
        cs, gap = P(16), P(8)
        G = C["FOCUS"]                                               # 초점 고리(바탕 대비 3:1 이상)
        ck = {k: img("chk_" + k, A.checkbox(cs, on, acc, edge, gap=gap, hover=hv, disabled=ds, ring=rg))
              for k, on, acc, edge, hv, ds, rg in (("off", False, C["ACC"], C["EDGE"], False, False, None), ("off_h", False, C["ACC"], C["EDGE"], True, False, None),
                                                   ("off_f", False, C["ACC"], C["EDGE"], False, False, G), ("on", True, C["ACC"], C["EDGE"], False, False, None),
                                                   ("on_h", True, C["ACC_HI"], C["EDGE"], False, False, None), ("on_f", True, C["ACC"], C["EDGE"], False, False, G),
                                                   ("dis_off", False, C["ACC"], C["EDGE"], False, True, None), ("dis_on", True, C["ACC"], C["EDGE"], False, True, None))}
        st.element_create("Check.indicator", "image", ck["off"], ("disabled", "selected", ck["dis_on"]), ("disabled", ck["dis_off"]),
                          ("selected", "active", ck["on_h"]), ("selected", "focus", ck["on_f"]), ("selected", ck["on"]), ("active", ck["off_h"]), ("focus", ck["off_f"]), sticky="")
        rd = {k: img("rad_" + k, A.radio(cs, on, acc, edge, gap=gap, hover=hv, disabled=ds, ring=rg))
              for k, on, acc, edge, hv, ds, rg in (("off", False, C["ACC"], C["EDGE"], False, False, None), ("off_h", False, C["ACC"], C["EDGE"], True, False, None),
                                                   ("off_f", False, C["ACC"], C["EDGE"], False, False, G), ("on", True, C["ACC"], C["EDGE"], False, False, None),
                                                   ("on_f", True, C["ACC"], C["EDGE"], False, False, G), ("dis_off", False, C["ACC"], C["EDGE"], False, True, None),
                                                   ("dis_on", True, C["ACC"], C["EDGE"], False, True, None))}
        st.element_create("Radio.indicator", "image", rd["off"], ("disabled", "selected", rd["dis_on"]), ("disabled", rd["dis_off"]),
                          ("selected", "focus", rd["on_f"]), ("selected", rd["on"]), ("active", rd["off_h"]), ("focus", rd["off_f"]), sticky="")
        for w, ind in (("Checkbutton", "Check.indicator"), ("Radiobutton", "Radio.indicator")):
            st.layout(f"T{w}", [(f"{w}.padding", {"sticky": "nswe", "children": [(ind, {"side": "left", "sticky": ""}), (f"{w}.label", {"side": "left", "sticky": "nswe"})]})])
            st.configure(f"T{w}", background=C["PANEL"], foreground=C["INK"], padding=(0, P(3), P(4), P(3)))
            st.map(f"T{w}", background=[("active", C["PANEL"]), ("disabled", C["PANEL"])], foreground=[("disabled", C["DIS_INK"])])
        # ---- 두 갈래 고르기(탭 대신): 붙어 있는 알약 모양 ----
        r = P(9); nw, bd_ = A.stretch_size(r, 96); nh, _ = A.stretch_size(r, 16)
        for side, corners in (("L", "l"), ("R", "r")):
            on, off, hov, on_f, off_f = (img(f"seg{side}_{k}", A.rounded_box(nw, nh, r, fill, line, bw * 1.6, corners=corners))
                                         for k, fill, line in (("on", C["ACC"], None), ("off", C["TRACK"], None), ("h", C["TRACK_HI"], None),
                                                               ("on_f", C["ACC"], G), ("off_f", C["TRACK"], G)))
            st.element_create(f"Seg{side}.border", "image", off, ("selected", "focus", on_f), ("selected", on), ("focus", off_f), ("active", hov),
                              border=bd_, sticky="nsew", width=2 * bd_, height=2 * bd_, padding=P(2))
            sname = f"Seg{side}.TRadiobutton"
            st.layout(sname, [(f"Seg{side}.border", {"sticky": "nsew", "children": [("Radiobutton.padding", {"sticky": "nsew", "children": [("Radiobutton.label", {"sticky": "nsew"})]})]})])
            st.configure(sname, background=C["BG"], foreground="#5f554a", font=self.font_h, padding=(P(24), P(8)), anchor="center")
            st.map(sname, foreground=[("selected", "#fff8ec"), ("active", C["INK"]), ("focus", C["INK"])], background=[("active", C["BG"]), ("selected", C["BG"])])
            st.configure(f"Small.{sname}", font=self.font_base, padding=(P(16), P(5)))
        # ---- 진행 막대: 처음부터 끝까지 한 방향으로 찬다 ----
        h = P(10); r = h // 2; nw, bd_ = A.stretch_size(r, 192)
        st.element_create("Bar.trough", "image", img("bar_tr", A.rounded_box(nw, h, r, C["TRACK"])), border=(bd_, 0), sticky="nsew", width=2 * bd_, padding=0)
        self._bar_cap = 2 * bd_                                       # 막대가 이보다 짧으면 둥근 끝이 겹쳐 그려진다
        st.element_create("Bar.pbar", "image", img("bar_pb", A.rounded_box(nw, h, r, C["GOLD"])), border=(bd_, 0), sticky="nsew", width=2 * bd_)
        st.layout("Bar.Horizontal.TProgressbar", [("Bar.trough", {"sticky": "nsew", "children": [("Bar.pbar", {"side": "left", "sticky": "ns"})]})])
        st.configure("Bar.Horizontal.TProgressbar", background=C["BG"])
        # ---- 입력칸·고르기 칸(둥근 테두리, 초점이면 금빛) ----
        r = P(5); nw, bd_ = A.stretch_size(r, 128); nh, _ = A.stretch_size(r, 20)
        fld = {k: img("fld_" + k, A.rounded_box(nw, nh, r, fill, line, w))
               for k, fill, line, w in (("n", C["FIELD"], C["EDGE"], bw), ("f", C["FIELD"], C["FOCUS"], bw * 1.6), ("d", C["DIS"], C["DIS_LINE"], bw), ("ro", "#faf8f3", C["FLINE"], bw))}
        st.element_create("Field.field", "image", fld["n"], ("disabled", fld["d"]), ("focus", fld["f"]), ("readonly", fld["ro"]), border=bd_, sticky="nsew", width=2 * bd_, height=2 * bd_, padding=P(3))
        st.element_create("Pick.field", "image", fld["n"], ("disabled", fld["d"]), ("focus", fld["f"]), border=bd_, sticky="nsew", width=2 * bd_, height=2 * bd_, padding=P(3))
        st.element_create("Pick.downarrow", "image", img("chev", A.chevron(P(12), P(8), C["MUTED"], 1.6 * self.scale)),
                          ("disabled", img("chev_d", A.chevron(P(12), P(8), C["DIS_INK"], 1.6 * self.scale))), sticky="", padding=(P(4), 0, P(2), 0))
        st.layout("TEntry", [("Field.field", {"sticky": "nswe", "children": [("Entry.padding", {"sticky": "nswe", "children": [("Entry.textarea", {"sticky": "nswe"})]})]})])
        st.configure("TEntry", padding=(P(6), P(4)), background=C["PANEL"], fieldbackground=C["FIELD"], foreground=C["INK"])
        st.map("TEntry", foreground=[("disabled", C["DIS_INK"])])
        st.layout("TCombobox", [("Pick.field", {"sticky": "nswe", "children": [("Pick.downarrow", {"side": "right", "sticky": ""}),
                                ("Combobox.padding", {"sticky": "nswe", "children": [("Combobox.textarea", {"sticky": "nswe"})]})]})])
        st.configure("TCombobox", padding=(P(6), P(4)), background=C["PANEL"], fieldbackground=C["FIELD"], foreground=C["INK"])
        st.map("TCombobox", fieldbackground=[("readonly", C["FIELD"])], selectbackground=[("readonly", C["FIELD"])], selectforeground=[("readonly", C["INK"])],
               foreground=[("disabled", C["DIS_INK"])], background=[("readonly", C["PANEL"]), ("active", C["PANEL"])])
        st.layout("TSpinbox", [("Field.field", {"side": "top", "sticky": "we", "children": [("null", {"side": "right", "sticky": "", "children": [
            ("Spinbox.uparrow", {"side": "top", "sticky": "e"}), ("Spinbox.downarrow", {"side": "bottom", "sticky": "e"})]}),
            ("Spinbox.padding", {"sticky": "nswe", "children": [("Spinbox.textarea", {"sticky": "nswe"})]})]})])
        st.configure("TSpinbox", padding=(P(6), P(3)), arrowsize=P(9), arrowcolor=C["MUTED"], background=C["PANEL"], fieldbackground=C["FIELD"],
                     bordercolor=C["FIELD"], lightcolor=C["FIELD"], darkcolor=C["FIELD"], foreground=C["INK"])
        st.map("TSpinbox", background=[("active", C["BTN_HI"]), ("pressed", C["BTN_DN"])])
        # ---- 표·글줄 ----
        st.configure("Treeview", background=C["FIELD"], fieldbackground=C["FIELD"], foreground=C["INK"], rowheight=P(28), bordercolor=C["LINE"],
                     lightcolor=C["FIELD"], darkcolor=C["FIELD"], borderwidth=0)
        st.map("Treeview", background=[("selected", C["SEL"])], foreground=[("selected", C["INK"])])
        st.configure("Treeview.Heading", background=C["HEAD"], foreground=C["MUTED"], font=self.font_h, relief="flat", padding=(P(8), P(6)),
                     bordercolor=C["LINE"], lightcolor=C["HEAD"], darkcolor=C["HEAD"])
        st.map("Treeview.Heading", background=[("active", C["BTN_HI"])])
        for o in ("Vertical", "Horizontal"):
            st.configure(f"{o}.TScrollbar", background=C["TRACK"], troughcolor=C["PANEL"], bordercolor=C["PANEL"], lightcolor=C["TRACK"],
                         darkcolor=C["TRACK"], arrowcolor=C["MUTED"], gripcount=0, arrowsize=P(12))
            st.map(f"{o}.TScrollbar", background=[("active", C["TRACK_HI"]), ("pressed", C["TRACK_HI"])])
        st.layout("Hidden.TNotebook.Tab", [])
        st.configure("Hidden.TNotebook", background=C["BG"], borderwidth=0, padding=0, tabmargins=0, bordercolor=C["BG"], lightcolor=C["BG"], darkcolor=C["BG"])
        # 대화 상자(별도 창)는 카드색 바탕
        st.configure("Dialog.TFrame", background=C["PANEL"])
        st.configure("TPanedwindow", background=C["BG"])
        st.configure("Sash", sashthickness=P(10), gripcount=0, background=C["BG"], lightcolor=C["BG"], darkcolor=C["BG"], bordercolor=C["BG"])
        # 위·아래 칸 경계: 가운데 작은 손잡이(끌어서 높이를 나눌 수 있다는 표시). 마우스를 올리면 커서가 ↕ 로 바뀐다.
        gw, gh = P(44), max(3, P(4))
        st.element_create("Grip.sash", "image", img("grip", A.rounded_box(gw, gh, gh / 2, C["FLINE"])), sticky="", width=gw, height=P(10))   # 그림은 얇게, 잡는 자리는 넉넉히
        for o in ("Vertical", "Horizontal"):
            st.layout(f"{o}.Sash", [("Grip.sash", {"sticky": ""})])

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

    def _segmented(self, parent, items: list[tuple[str, int]], var: tk.IntVar, command, small: bool = False) -> ttk.Frame:
        """붙어 있는 두 갈래 고르기. 탭처럼 화면을 바꾸지만 단추처럼 또렷하게 보인다."""
        f = ttk.Frame(parent)
        for k, (text, val) in enumerate(items):
            side = "L" if k == 0 else "R"
            ttk.Radiobutton(f, text=text, value=val, variable=var, command=command, cursor="hand2",
                            style=("Small." if small else "") + f"Seg{side}.TRadiobutton").pack(side="left")
        return f

    def _cw(self, values, extra: int = 3) -> int:
        """고르기 칸 너비(글자 '0' 단위): 가장 긴 값이 다 보이게."""
        try:
            from tkinter import font as tkfont
            f = tkfont.Font(self, font=self.font_base); zero = max(1, f.measure("0"))
            return max(6, -(-max(f.measure(str(v)) for v in values) // zero) + extra)
        except Exception:
            return max(6, max(len(str(v)) for v in values) * 2)

    def select_tab(self, i: int):
        """일반 OCR(0) · 책갈피 OCR(1). 고른 쪽만 보여 높이가 내용에 맞는다."""
        self.v_tab.set(i)
        for k, f in enumerate(self.tab_frames):
            if k == i:
                f.pack(fill="x")
            else:
                f.pack_forget()
        self.v_tabhint.set(TAB_HINTS[i])
        if hasattr(self, "_canvas"):
            self._canvas.yview_moveto(0)
        for k, a in enumerate(getattr(self, "act_cards", [])):
            if k == i:
                a.pack(fill="x")
            else:
                a.pack_forget()
        if hasattr(self, "pane"):
            self.after_idle(self._fit_sash)

    def _build(self):
        C = self.colors; P = self.px
        # ---- 머리띠 ----
        hd = tk.Frame(self, bg=C["ACC"], padx=P(20), pady=P(11)); hd.pack(fill="x")
        tk.Frame(self, bg=C["GOLD"], height=max(2, P(2))).pack(fill="x")             # 머리띠 아래 금빛 선
        self.v_pkg = tk.StringVar(value="패키지 점검 중…")
        # 제목·판 번호를 먼저 배치한다(pack 은 먼저 넣은 것에 자리를 먼저 주므로, 긴 안내 글이 제목을 밀어내지 않게).
        tk.Label(hd, text=APP_NAME, font=self.font_title, bg=C["ACC"], fg="#f3e9d2").pack(side="left")
        tk.Label(hd, text="연어OCR", font=self.font_big, bg=C["ACC"], fg="#d9c9a8").pack(side="left", padx=(P(10), 0), pady=(P(4), 0))
        tk.Label(hd, text=f"v{__version__}", font=(self.font_small[0], 9, "bold"), bg="#5a2a20", fg="#f3e9d2", padx=P(8), pady=P(2)).pack(side="right", pady=(P(5), 0))
        tk.Label(hd, textvariable=self.v_pkg, font=self.font_small, bg=C["ACC"], fg="#e8c99a").pack(side="right", padx=(0, P(14)), pady=(P(6), 0))
        tk.Label(hd, text="한자·한글 문헌 스캔 PDF·이미지를 지침대로 판독해 HWPX로", font=self.font_sub, bg=C["ACC"], fg="#bfae8e").pack(side="left", padx=(P(16), 0), pady=(P(6), 0))

        # ---- 일반 OCR / 책갈피 OCR 고르기 ----
        bar = ttk.Frame(self, padding=(P(16), P(12), P(16), 0)); bar.pack(fill="x")
        self.v_tab = tk.IntVar(value=0); self.v_tabhint = tk.StringVar(value=TAB_HINTS[0])
        self._segmented(bar, [("일반 OCR", 0), ("책갈피 OCR", 1)], self.v_tab, lambda: self.select_tab(self.v_tab.get())).pack(side="left")
        self.b_update = self._btn(bar, "업데이트 확인", self.check_updates, style="PageLink.TButton",
                                  tip="GitHub 릴리스에서 새 버전을 확인합니다. 설치와 재시작은 확인 후 진행합니다.")
        self.b_update.pack(side="right")
        ttk.Label(bar, textvariable=self.v_tabhint, style="PageHint.TLabel").pack(side="left", padx=(P(14), 0))
        # 위(설정·실행)는 스크롤되는 칸, 아래(진행·기록)는 늘 보이는 칸. 가운데 경계를 끌어 높이를 나눈다.
        self.pane = ttk.PanedWindow(self, orient="vertical"); self.pane.pack(fill="both", expand=True)
        upper = ttk.Frame(self.pane)
        cv = self._canvas = tk.Canvas(upper, bg=C["BG"], highlightthickness=0, borderwidth=0, yscrollincrement=P(20))
        cv.pack(fill="both", expand=True)
        self._vsb = ttk.Scrollbar(upper, orient="vertical", command=cv.yview); cv.configure(yscrollcommand=self._vsb.set)
        host = self._host = ttk.Frame(cv, padding=(P(16), P(10), P(16), P(8)))
        win = cv.create_window(0, 0, window=host, anchor="nw")
        host.bind("<Configure>", lambda e: (cv.configure(scrollregion=(0, 0, e.width, e.height)), self._update_scroll()))
        cv.bind("<Configure>", lambda e: (cv.itemconfigure(win, width=e.width), self._update_scroll()))
        self.bind_all("<MouseWheel>", self._wheel, add="+"); self.bind_all("<Button-4>", self._wheel, add="+"); self.bind_all("<Button-5>", self._wheel, add="+")
        for cls in ("TCombobox", "TSpinbox"):                      # 휠로 위 칸을 굴리다 고르기 칸 위를 지나면 값이 몰래 바뀌던 문제
            for ev in ("<MouseWheel>", "<Button-4>", "<Button-5>", "<Shift-MouseWheel>"):
                self.bind_class(cls, ev, "")
        self.bind_all("<<TraverseIn>>", self._focus_into_view, add="+")   # Tab 으로 옮길 때만(마우스로 누를 때 위 칸이 튀어 클릭이 씹히지 않게)
        self.pane.add(upper, weight=0)
        root = ttk.Frame(host); tab2 = ttk.Frame(host)
        self.tab_frames = [root, tab2]

        # ---- 카드 1: PDF 파일 ----
        c1 = self._card(root, "PDF · 이미지"); c1.pack(fill="x")
        b = c1.body; b.columnconfigure(1, weight=1)
        self.v_pdf = tk.StringVar()
        self._field_label(b, "파일", 0)
        ttk.Entry(b, textvariable=self.v_pdf, state="readonly").grid(row=0, column=1, sticky="ew", pady=P(3))
        self._btn(b, "PDF·이미지 고르기…", self.choose_pdf, tip="판독할 스캔 PDF 나 이미지(PNG·JPG·TIFF 등)를 고릅니다. 이미지를 여러 장 고르면 이름의 숫자 순서로 한 권으로 묶습니다. 결과는 파일 옆 '<이름>_YeonE_OCR' 폴더에 생깁니다.").grid(row=0, column=2, sticky="ew", padx=(P(8), 0), pady=P(3))
        self.v_pages = tk.StringVar(value=self.cfg["pages"]); self.v_off = tk.StringVar(value=self.cfg["print_offset"])
        self._field_label(b, "쪽 범위", 1, top=6)
        row = ttk.Frame(b, style="Panel.TFrame"); row.grid(row=1, column=1, columnspan=2, sticky="ew", pady=(P(6), P(3)))
        ttk.Entry(row, textvariable=self.v_pages, width=12).pack(side="left")
        self._btn(row, "전체", lambda: self.v_pages.set(""), tip="쪽 범위를 비워 PDF 전체를 합니다").pack(side="left", padx=(P(6), 0))
        ttk.Label(row, text="비우면 전체 · 예 1-5, 8", style="Hint.TLabel").pack(side="left", padx=(P(10), 0))
        ttk.Label(row, text="빈칸이면 자동 판정", style="Hint.TLabel").pack(side="right", padx=(P(8), 0))
        e_off = ttk.Entry(row, textvariable=self.v_off, width=6); e_off.pack(side="right")
        ttk.Label(row, text="인쇄 쪽수 = PDF 쪽수 +", style="Field.TLabel").pack(side="right", padx=(0, P(6)))
        Tip(e_off, "PDF 1쪽이 인쇄 30쪽이면 29. 비우거나 0 이면 글자층·판독한 쪽번호로 자동 판정하고, 어긋나는 쪽만 확인 항목으로 표시합니다.", self)

        # ---- 카드 2·3: 판독 / 문서 ----
        mid = ttk.Frame(root); mid.pack(fill="x", pady=(P(10), 0))
        mid.columnconfigure(0, weight=3); mid.columnconfigure(1, weight=2)
        c2 = self._card(mid, "판독"); c2.grid(row=0, column=0, sticky="nsew", padx=(0, P(10)))
        c3 = self._card(mid, "문서"); c3.grid(row=0, column=1, sticky="nsew")
        self.v_up = tk.StringVar(value=self.cfg["upscale"]); self.v_reader = tk.StringVar(value=self.cfg["reader"])
        self.v_model = tk.StringVar(value=self.cfg["model"]); self.v_key = tk.StringVar(value=self.cfg["api_key"])
        self.v_second = tk.BooleanVar(value=self.cfg["second_pass"]); self.v_docx = tk.BooleanVar(value=self.cfg["docx"])
        self.v_join = tk.BooleanVar(value=self.cfg["join_pages"]); self.v_force = tk.BooleanVar(value=self.cfg.get("force", False))
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
        self.v_mode = tk.StringVar(value=MODE_LABELS.get(self.cfg.get("mode", "auto"), MODE_LABELS["auto"]))
        b = c2.body; b.columnconfigure(1, weight=1)
        self._field_label(b, "판독기", 0)
        rr = ttk.Frame(b, style="Panel.TFrame"); rr.grid(row=0, column=1, columnspan=2, sticky="w", pady=P(3))
        ttk.Radiobutton(rr, text="Gemini (원본 이미지 판독)", variable=self.v_reader, value="gemini", command=self._reader_changed).pack(side="left", padx=(0, P(16)))
        ttk.Radiobutton(rr, text="기존 텍스트 레이어만 (API 없이 시험)", variable=self.v_reader, value="mock", command=self._reader_changed).pack(side="left")
        self._field_label(b, "읽기 방향", 1)
        cb = ttk.Combobox(b, textvariable=self.v_mode, values=list(MODE_LABELS.values()), state="readonly", width=self._cw(MODE_LABELS.values()))
        cb.grid(row=1, column=1, sticky="w", pady=P(3))
        Tip(cb, "자동 판정이면 PDF 글자층의 세로 열·가로 줄 글자 수로 세로쓰기 한자·한글 / 가로쓰기 한문(간체)을 고르고 결과 폴더에 적어 둡니다.", self)
        self._field_label(b, "모델", 2)
        self.e_model = ttk.Entry(b, textvariable=self.v_model); self.e_model.grid(row=2, column=1, sticky="ew", pady=P(3))
        self._btn(b, "키 확인·모델 고르기", self.check_key, tip="키가 맞는지 Gemini 에 확인하고, 실제로 호출되는 모델 목록에서 고릅니다.").grid(row=2, column=2, sticky="ew", padx=(P(8), 0), pady=P(3))
        self._field_label(b, "API 키", 3)
        self.e_key = ttk.Entry(b, textvariable=self.v_key, show="•"); self.e_key.grid(row=3, column=1, sticky="ew", pady=P(3))
        Tip(self.e_key, "API 키. 다른 프로젝트의 키를 쉼표로 이어 여러 개 넣을 수 있습니다(예: AIza…, AIza…). 한 키가 하루 한도에 걸리면 다음 키로 넘어가 이어서 읽습니다.", self)
        self._btn(b, "보기", self.toggle_key, tip="키를 가리거나 보입니다. 키는 이 PC 의 설정 파일에만 저장됩니다. 다른 프로젝트의 키를 쉼표로 더 넣으면 하루 한도에 걸릴 때 다음 키로 넘어갑니다.").grid(row=3, column=2, sticky="ew", padx=(P(8), 0), pady=P(3))
        foot = ttk.Frame(b, style="Panel.TFrame"); foot.grid(row=4, column=0, columnspan=3, sticky="ew", pady=(P(6), 0))
        ttk.Checkbutton(foot, text="기록 있는 쪽도 다시 판독", variable=self.v_force).pack(side="left")
        self.v_details = tk.BooleanVar(value=bool(self.cfg.get("details_open", False)))
        self.b_details = ttk.Button(foot, text="", command=self.toggle_details, style="Link.TButton", cursor="hand2")
        self.b_details.pack(side="right")
        d = self.details = ttk.Frame(b, style="Panel.TFrame"); d.columnconfigure(1, weight=1)
        ttk.Separator(d).grid(row=0, column=0, columnspan=3, sticky="ew", pady=(P(8), P(6)))
        self._field_label(d, "2차 판독 모델", 1)
        r2 = ttk.Frame(d, style="Panel.TFrame"); r2.grid(row=1, column=1, columnspan=2, sticky="ew", pady=P(3))
        self.e_model2 = ttk.Entry(r2, textvariable=self.v_model2, width=24); self.e_model2.pack(side="left")
        ttk.Label(r2, text="auto = 같은 세대 flash 자동 · 비우면 1차와 같음", style="Hint.TLabel").pack(side="left", padx=(P(10), 0))
        self._field_label(d, "속도", 2)
        sp = ttk.Frame(d, style="Panel.TFrame"); sp.grid(row=2, column=1, columnspan=2, sticky="w", pady=P(3))
        ttk.Label(sp, text="동시 최대", style="Field.TLabel").pack(side="left", padx=(0, P(4)))
        s1 = ttk.Spinbox(sp, from_=1, to=12, width=3, textvariable=self.v_workers); s1.pack(side="left")
        ttk.Label(sp, text="쪽 · 확대", style="Field.TLabel").pack(side="left", padx=(P(6), P(4)))
        ttk.Entry(sp, textvariable=self.v_up, width=4).pack(side="left")
        ttk.Label(sp, text="배 · 중간 저장", style="Field.TLabel").pack(side="left", padx=(P(6), P(4)))
        ttk.Entry(sp, textvariable=self.v_ckpt, width=4).pack(side="left")
        ttk.Label(sp, text="쪽마다", style="Field.TLabel").pack(side="left", padx=(P(4), 0))
        Tip(s1, "동시에 판독할 쪽 수의 최대. 4쪽에서 시작해 안정적이면 늘리고, 한도 초과(429)가 나면 저절로 줄입니다.", self)
        self._field_label(d, "2차 조각", 3)
        cp = ttk.Frame(d, style="Panel.TFrame"); cp.grid(row=3, column=1, columnspan=2, sticky="w", pady=P(3))
        ttk.Label(cp, text="한 장에", style="Field.TLabel").pack(side="left", padx=(0, P(4)))
        s2 = ttk.Spinbox(cp, from_=0, to=30, width=3, textvariable=self.v_chunk); s2.pack(side="left")
        ttk.Label(cp, text="줄(열)까지 · 해상도", style="Field.TLabel").pack(side="left", padx=(P(6), P(4)))
        mr = ttk.Combobox(cp, textvariable=self.v_mres, values=list(MRES_LABELS.values()), state="readonly", width=self._cw(MRES_LABELS.values()))
        mr.pack(side="left")
        Tip(s2, "2차 확대 조각 한 장에 넣을 글줄(세로쓰기는 열) 수. 넘으면 나눠 보내 글자당 픽셀을 늘립니다(0 = 나누지 않음).", self)
        Tip(mr, "2차 조각 이미지의 토큰 예산. 높을수록 入/人·曰/日 같은 획 하나 차이를 잘 가립니다. 최고는 Gemini 3 전용이며 거부되면 저절로 높음으로 내립니다.", self)
        self._field_label(d, "판독 단계", 4)
        ck = ttk.Frame(d, style="Panel.TFrame"); ck.grid(row=4, column=1, columnspan=2, sticky="w", pady=P(3))
        ttk.Checkbutton(ck, text="영역 확대 2차 독립 판독·비교", variable=self.v_second).pack(side="left", padx=(0, P(14)))
        ttk.Checkbutton(ck, text="지침 캐시", variable=self.v_cache).pack(side="left", padx=(0, P(14)))
        ttk.Checkbutton(ck, text="2차 생각 줄이기", variable=self.v_think2).pack(side="left")
        self._field_label(d, "3차 판별", 5)
        bl = ttk.Frame(d, style="Panel.TFrame"); bl.grid(row=5, column=1, columnspan=2, sticky="w", pady=P(3))
        cb = ttk.Checkbutton(bl, text="후보 없이 한 번 더 읽어 같을 때만 확정", variable=self.v_blind); cb.pack(side="left")
        ttk.Label(bl, text="호출 조금 더 · 더 엄격", style="Hint.TLabel").pack(side="left", padx=(P(10), 0))
        Tip(cb, "3차 판별은 앞뒤 글자를 글로 알려 주지 않고(조각 그림에는 이웃 글자가 보입니다) 후보 차례를 섞어 자형만 묻습니다. "
                "이것을 켜면 고른 자리를 후보도 주지 않고 한 번 더 읽게 해, 두 답이 같을 때만 확정합니다. 다르면 표시〔a/b〕를 그대로 남깁니다. "
                "호출이 더 듭니다(일반 OCR: 쪽마다 확정 자리 8곳당 1회 · 책갈피 OCR 교정에도 적용: 확정 항목마다 1회).", self)
        self.v_suspects = tk.BooleanVar(value=bool(self.cfg.get("suspects", True)))
        bs = ttk.Frame(d, style="Panel.TFrame"); bs.grid(row=6, column=1, columnspan=2, sticky="w", pady=P(3))
        cs = ttk.Checkbutton(bs, text="합의 판독도 글자층과 다르면 자형으로 확인(표시만)", variable=self.v_suspects); cs.pack(side="left")
        ttk.Label(bs, text="쪽당 최대 8곳 · 본문은 그대로", style="Hint.TLabel").pack(side="left", padx=(P(10), 0))
        Tip(cs, "1차·2차가 같게 읽어 〔a/b〕 가 없는 글자도, PDF 글자층(기존 OCR)과 한 글자만 다르고 그 두 글자가 入/人·曰/日·問/間 처럼 자형이 헷갈리는 쌍이면 "
                "3차 판별과 같은 방식으로 글자 조각을 묻습니다. 자형이 글자층 쪽이면 본문은 판독대로 두고 확인 항목 「합의 판독 의심」과 문서 메모만 답니다(지침 20 — 기존 OCR 은 의심만 올림). "
                "이미 물은 자리는 다시 묻지 않습니다. 끄면 〔a/b〕 자리만 묻습니다.", self)
        from tkinter import font as tkfont
        lw = max(tkfont.Font(self, font=self.font_base).measure(t) for t in ("판독기", "읽기 방향", "모델", "API 키", "2차 판독 모델", "속도", "2차 조각", "판독 단계", "3차 판별")) + P(18)
        b.columnconfigure(0, minsize=lw); d.columnconfigure(0, minsize=lw)   # 세부 설정을 펼쳐도 입력칸 왼쪽 끝이 한 줄로 맞게
        self.v_think1 = tk.StringVar(value=PAGE_THINK_LABELS.get(self.cfg.get("page_thinking", "auto"), PAGE_THINK_LABELS["auto"]))
        self.v_mres1 = tk.StringVar(value=PAGE_MRES_LABELS.get(self.cfg.get("page_media_res", "auto"), PAGE_MRES_LABELS["auto"]))
        self.v_maxcalls = tk.StringVar(value=str(self.cfg.get("max_calls", 0)))
        self._field_label(d, "1차 판독", 8)
        p1 = ttk.Frame(d, style="Panel.TFrame"); p1.grid(row=8, column=1, columnspan=2, sticky="w", pady=P(3))
        ttk.Label(p1, text="생각 수준", style="Field.TLabel").pack(side="left", padx=(0, P(4)))
        t1 = ttk.Combobox(p1, textvariable=self.v_think1, values=list(PAGE_THINK_LABELS.values()), state="readonly", width=self._cw(PAGE_THINK_LABELS.values()))
        t1.pack(side="left")
        ttk.Label(p1, text="· 쪽 해상도", style="Field.TLabel").pack(side="left", padx=(P(6), P(4)))
        m1 = ttk.Combobox(p1, textvariable=self.v_mres1, values=list(PAGE_MRES_LABELS.values()), state="readonly", width=self._cw(PAGE_MRES_LABELS.values()))
        m1.pack(side="left")
        Tip(t1, "1차 생각 수준: 쪽 전체 판독(영역 나누기·1차 글)의 생각 수준. 자동 = 모델 기본(지금까지와 같음). 낮추면 빠르고 싸지만 정확도가 달라질 수 있으니 "
                "같은 표본 쪽을 결과 폴더만 바꿔 측정(yeone-ocr eval)으로 비교한 뒤 바꾸십시오. Gemini 3 은 생각 수준, 2.5 는 생각 예산(낮음 512 · 중간 4096 · 높음 24576, 2.5 Pro 32768). "
                "모델이 받지 않으면 저절로 모델 기본으로 계속합니다.", self)
        Tip(m1, "1차 쪽 해상도: 쪽 전체 이미지의 토큰 예산. 자동 = 설정 없음(지금까지와 같음). 최고(ultra_high)는 Gemini 3 전용이며 거부되면 저절로 높음 → 기본으로 내립니다. "
                "측정(yeone-ocr eval)으로 비교한 뒤 바꾸십시오.", self)
        self._field_label(d, "호출 상한", 9)
        mcf = ttk.Frame(d, style="Panel.TFrame"); mcf.grid(row=9, column=1, columnspan=2, sticky="w", pady=P(3))
        mc_ = ttk.Spinbox(mcf, from_=0, to=1000000, increment=100, width=7, textvariable=self.v_maxcalls); mc_.pack(side="left")
        ttk.Label(mcf, text="회 · 0 = 없음 · 재시도 포함, 한 번 실행 전체", style="Hint.TLabel").pack(side="left", padx=(P(10), 0))
        Tip(mc_, "호출 상한(0=없음): 한 번 실행(한 번에 인식·판독·3차 판별·책갈피 교정)에서 모델을 부르는 횟수의 상한입니다. 재시도도 한 번으로 셉니다. "
                 "닿으면 멈추고 끝난 쪽은 기록에 남으며, 다시 누르면 남은 쪽부터 이어서 합니다. 비용이 걱정되면 미리 보기의 예상 호출 수보다 조금 크게 두십시오.", self)
        self._apply_details()
        # 문서
        b = c3.body; b.columnconfigure(0, weight=1)
        checks = (("확인 등급 색 (초록·파랑·빨강)", self.v_hl, "글자를 얼마나 믿어도 되는지 색과 밑줄로: 초록 = 1차·2차가 갈려 3차 자형 판별로 고름(훑어 확인) · "
                                                            "파랑 = 골랐지만 근거가 약하거나 다른 대목과 어긋남(확인 권장) · 빨강 〔 〕 = 정하지 못함, 원본 확인 필수(지침 19). "
                                                            "끄면 색 없이 〔 〕 표기만 남습니다."),
                  ("문맥 추정 밑줄 (확정 아님)", self.v_ctx, "빨간 〔a/b〕 안에서, 이 판독의 다른 대목(1차·2차가 같게 읽은 글)이 더 많이 쓰는 후보에 실선 밑줄을 긋습니다. "
                                                         "글자는 바꾸지 않고 두 후보도 그대로 둡니다(지침 2·7·19·21). 숫자·이체자·판독불명·자형이 다른 쌍에는 긋지 않습니다. "
                                                         "이 OCR 자신의 합의에서 센 힌트이며 문턱은 아직 재지 않았습니다."),
                  ("원본 쪽마다 쪽 나누기", self.v_pb, None), ("앞 쪽에서 이어지는 문단 잇기", self.v_join, None), ("DOCX 도 함께 만들기", self.v_docx, None))
        for i, (t, v, tip) in enumerate(checks):
            cb_ = ttk.Checkbutton(b, text=t, variable=v); cb_.grid(row=i, column=0, sticky="w")
            if tip: Tip(cb_, tip, self)
        cl = ttk.Frame(b, style="Panel.TFrame"); cl.grid(row=5, column=0, sticky="ew", pady=(P(8), 0)); cl.columnconfigure(1, weight=1)
        ttk.Label(cl, text="메모", style="Field.TLabel").grid(row=0, column=0, sticky="w", padx=(0, P(8)), pady=(0, P(4)))
        mc = ttk.Combobox(cl, textvariable=self.v_memo, values=list(MEMO_LABELS.values()), state="readonly", width=self._cw(list(MEMO_LABELS.values()) + list(CHECK_LIST_LABELS.values())))
        mc.grid(row=0, column=1, sticky="w", pady=(0, P(4)))
        Tip(mc, "고지 사항만(기본): 색으로 전할 수 없는 것 — 본문에 표시되지 않은 확인 항목, 구조 검사 오류, 3차와 다른 대목의 어긋남 — 과 첫머리의 표시 범례에만 한컴 메모를 답니다. "
                "모든 표시에 메모: 초록·파랑·문맥 밑줄 자리마다 근거 메모. 메모 없이: 고지를 회색 〔고지: …〕 로 글 속에.", self)
        ttk.Label(cl, text="쪽 끝 확인 목록", style="Field.TLabel").grid(row=1, column=0, sticky="w", padx=(0, P(8)))
        cc = ttk.Combobox(cl, textvariable=self.v_chk, values=list(CHECK_LIST_LABELS.values()), state="readonly", width=self._cw(list(MEMO_LABELS.values()) + list(CHECK_LIST_LABELS.values())))
        cc.grid(row=1, column=1, sticky="w")
        Tip(cc, "문서 끝에 확인 목록을 넣을지. 기본은 넣지 않으며(문서에는 OCR 본문만), 전체 목록은 검수 화면에 있습니다.", self)
        self._hint(b, "초록 = 3차 자형 판별로 고름 · 파랑 = 골랐지만 확인 권장 · 빨강〔〕= 원본 확인 필수 · 〔〕안 실선 밑줄 = 문맥 추정(확정 아님) · 메모 = 고지 사항").grid(row=6, column=0, sticky="ew", pady=(P(8), 0))
        self._btn(b, "설정 저장", self.save_settings, tip="지금 설정을 저장합니다(실행할 때와 창을 닫을 때도 저절로 저장).").grid(row=7, column=0, sticky="e", pady=(P(8), 0))

        # ---- 고급(접힘) ----
        self.v_adv = tk.BooleanVar(value=bool(self.cfg.get("advanced_open", False)))
        advbar = ttk.Frame(root); advbar.pack(fill="x", pady=(P(8), 0))
        self.b_adv = ttk.Button(advbar, text="", command=self.toggle_advanced, style="PageLink.TButton", cursor="hand2"); self.b_adv.pack(side="left")
        ttk.Label(advbar, text="보통은 필요 없습니다. 일부 단계만 다시 하거나 기록을 손볼 때 펼치십시오.", style="PageHint.TLabel").pack(side="left", padx=(P(10), 0))
        sf = self.adv_frame = self._card(root)
        g = sf.body
        steps = [("① 구조 보기", self.do_inspect, "쪽수, 쪽마다 스캔 이미지인지, 텍스트 레이어 글자 수, 쪽번호 표기를 보여 줍니다"),
                 ("② 준비", self.do_prepare, "쪽 이미지와 기존 텍스트 레이어를 결과 폴더에 꺼냅니다"),
                 ("③ 판독", self.do_read, "1차·2차 판독만 합니다(기록이 있는 쪽은 건너뜀)"),
                 ("④ 문서 만들기", self.do_build, "판독 기록으로 HWPX(+DOCX)를 만듭니다. readings 를 손으로 고친 뒤에 누릅니다"),
                 ("⑤ 검수 화면", self.do_review, "원본 위에 영역 상자를 그린 검수 화면(review.html)을 만들어 엽니다"),
                 ("⑥ 보고", self.do_report, "쪽별 상태와 다발 오인식 장부(report.md)를 진행 기록에 보여 줍니다")]
        tools = [("3차 글자 판별", self.do_adjudicate, "1차·2차가 갈린 〔a/b〕 자리를 글자 하나씩 크게 잘라 자형만으로 묻습니다(앞뒤 글자는 글로 주지 않고 후보 차례는 섞음 · 8자리씩 묶어 한 호출, 쪽은 동시에)"),
                 ("혼동 장부", self.do_ledger, "어느 글자가 어느 글자로 읽혔는지 결과 폴더 전체에서 모읍니다(모델 호출 없음)"),
                 ("비교 다시 계산", self.do_remerge, "저장된 1차·2차 판독으로 확인 표시만 다시 계산합니다(모델 호출 없음). 프로그램을 새 판으로 바꾼 뒤에"),
                 ("검수 결과 기록…", self.do_mark, "사람이 원본 대조를 마친 쪽에 '원본 대조 완료(사람)' 를 남깁니다"),
                 ("패키지 설치·점검", self.install_packages, "필요한 파이썬 패키지를 설치하거나 새 판으로 올립니다"),
                 ("도움말", self.show_help, "쓰는 법을 간단히 보여 줍니다")]
        for r_, (head, items) in enumerate((("단계", steps), ("도구", tools))):
            ttk.Label(g, text=head, style="Section.TLabel").grid(row=r_, column=0, sticky="w", padx=(0, P(12)))
            for i, (t, c, tip) in enumerate(items):
                button = self._btn(g, t, c, style="Tool.TButton", tip=tip)
                button.grid(row=r_, column=i + 1, sticky="ew", padx=P(3), pady=P(3))
                if c == self.install_packages:
                    self.b_packages = button
                    if getattr(sys, "frozen", False):
                        button.configure(text="패키지 내장", state="disabled")
        for i in range(1, 7):
            g.columnconfigure(i, weight=1, uniform="tools")
        self._hint(g, "단추에 마우스를 올리면 하는 일이 나옵니다. 프로그램을 새 판으로 바꾼 뒤에는 [비교 다시 계산], 사람이 확인한 쪽은 [검수 결과 기록…].").grid(
            row=2, column=0, columnspan=7, sticky="ew", pady=(P(6), 0))
        self.v_auto_update = tk.BooleanVar(value=bool(self.cfg.get("auto_update", True)))
        ttk.Checkbutton(g, text="시작할 때 업데이트 확인", variable=self.v_auto_update,
                        command=self._save_update_preference).grid(row=3, column=0, columnspan=7, sticky="w", pady=(P(8), 0))
        self._apply_advanced()

        self._build_collate_tab(tab2)
        self.select_tab(0)

        # ---- 진행(공통): 상태 글 · 진행 글 / 끝까지 차는 막대 ----
        bottom = self._bottom = ttk.Frame(self.pane, padding=(P(16), P(8), P(16), P(14))); self.pane.add(bottom, weight=1)
        self._build_actions(bottom)
        # 한 줄에 [진행 기록|쪽별 상태] 고르기 · 상태 글 · 진행 글, 그 아래 막대(낮은 화면에서 기록 칸에 한 줄이라도 더 주려고)
        pf = ttk.Frame(bottom); pf.pack(fill="x", pady=(P(10), 0))
        self.v_status = tk.StringVar(value="준비됨"); self.v_prog = tk.StringVar(value="")
        self.v_nb = tk.IntVar(value=0)
        self._segmented(pf, [("진행 기록", 0), ("쪽별 상태", 1)], self.v_nb, lambda: self.nb.select(self.v_nb.get()), small=True).pack(side="left")
        ttk.Label(pf, textvariable=self.v_prog, style="Prog.TLabel").pack(side="right", padx=(P(12), 0))
        ttk.Label(pf, textvariable=self.v_status, style="Status.TLabel").pack(side="left", padx=(P(14), 0))
        self.progress = ttk.Progressbar(bottom, style="Bar.Horizontal.TProgressbar", mode="determinate", maximum=1000, value=0)
        self.progress.pack(fill="x", pady=(P(8), 0))

        # ---- 진행 기록 / 쪽별 상태(공통) ----
        nb = self.nb = ttk.Notebook(bottom, style="Hidden.TNotebook"); nb.pack(fill="both", expand=True, pady=(P(10), 0))
        nb.bind("<<NotebookTabChanged>>", lambda _e: self.v_nb.set(nb.index("current")))
        lc = self._card(nb, pad=(P(6), P(6), P(4), P(6)))
        lb = lc.body
        self.log = tk.Text(lb, height=6, wrap="word", state="disabled", font=self.font_mono, bg=C["PANEL"], fg=C["INK"], relief="flat",
                           borderwidth=0, highlightthickness=0, padx=P(10), pady=P(6), insertbackground=C["INK"], spacing1=1, spacing3=1,
                           selectbackground=C["SEL"], selectforeground=C["INK"])
        sb = ttk.Scrollbar(lb, orient="vertical", command=self.log.yview); self.log.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y"); self.log.pack(side="left", fill="both", expand=True)
        nb.add(lc, text="진행 기록")
        tc = self._card(nb, pad=(P(6), P(6), P(4), P(6)))
        self.table = ttk.Treeview(tc.body, columns=("pdf", "print", "status", "unc", "figs"), show="headings")
        for c, t, w in (("pdf", "PDF 쪽", 70), ("print", "인쇄 쪽", 70), ("status", "상태", 560), ("unc", "확인 필요", 80), ("figs", "도판", 60)):
            self.table.heading(c, text=t); self.table.column(c, width=P(w), anchor="center" if c != "status" else "w", stretch=(c == "status"))
        self.table.tag_configure("odd", background=C["STRIPE"])
        sb = ttk.Scrollbar(tc.body, orient="vertical", command=self.table.yview); self.table.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y"); self.table.pack(side="left", fill="both", expand=True)
        nb.add(tc, text="쪽별 상태")
        self._reader_changed()
        self.select_tab(0)
        self.update_idletasks()
        mid.columnconfigure(1, minsize=c3.winfo_reqwidth())           # 문서 카드는 줄어도 글자가 잘리지 않게
        self.after(60, self._fit_sash)
        for ev in ("<Configure>", "<B1-Motion>", "<ButtonRelease-1>"):     # 창을 줄이거나 경계를 끌어 내려도 실행 줄·막대가 남게
            self.pane.bind(ev, lambda _e: self.after_idle(lambda: self._fit_sash(force=False)), add="+")
        # 마우스로 누른 체크·라디오·두 갈래·글자 단추는 초점을 창으로 돌린다 — 초점 고리는 키보드(Tab)로 옮겼을 때만 보이게
        for cls in ("TCheckbutton", "TRadiobutton", "TButton"):
            self.bind_class(cls, "<ButtonRelease-1>", lambda e: self.after_idle(self._drop_mouse_focus, e.widget), add="+")

    def _build_actions(self, host) -> None:
        """실행 줄 — 진행 막대 바로 위, 늘 보이는 자리. 탭마다 그 탭의 실행 단추를 보여 준다(실행 중에도 [중지]가 가려지지 않게)."""
        P = self.px
        self.actbars = ttk.Frame(host); self.actbars.pack(fill="x")
        act = self._card(self.actbars, pad=(P(16), P(10), P(16), P(10)))
        a = act.body
        self._btn(a, "▶  한 번에 인식 (PDF → HWPX)", self.do_run, style="Accent.TButton",
                  tip="준비 → 판독(1차·2차) → 3차 글자 판별 → 문서 → 검수 화면 → 보고까지 이어서 합니다. 먼저 예상 비용을 보여 주고 묻습니다.").pack(side="left")
        self.b_stop = ttk.Button(a, text="■  중지", command=self.do_stop, state="disabled", style="Stop.TButton", cursor="hand2"); self.b_stop.pack(side="left", padx=(P(8), 0))
        Tip(self.b_stop, "지금 쪽이 끝나면 멈춥니다. 끝난 쪽은 남고, 다시 누르면 이어서 합니다.", self)
        for t, c, tip in (("HWPX 열기", self.open_hwpx, "가장 최근에 만든 HWPX 를 한컴오피스로 엽니다"),
                          ("결과 폴더", self.open_work, "결과 폴더(쪽 이미지·판독 기록·문서·검수 화면)를 엽니다"),
                          ("비용 미리 보기", self.do_estimate, "쪽 수와 모델로 호출·토큰·금액을 어림합니다(이미 판독한 쪽이 있으면 그 실측으로 보정)")):
            self._btn(a, t, c, tip=tip).pack(side="right", padx=(P(6), 0))
        self._hint(a, "멈춰도 끝난 쪽은 남고, 다시 누르면 남은 쪽부터 이어서 합니다.").pack(side="left", fill="x", expand=True, padx=(P(14), P(8)))
        act2 = self._card(self.actbars, pad=(P(16), P(10), P(16), P(10)))
        a = act2.body
        self._btn(a, "▶  HWPX 전체 교정", self.do_collate_all, style="Accent.TButton",
                  tip="구획을 차례로 전부 대조해 HWPX 사본(<이름>_교정.hwpx) 하나에 고쳐 넣고 요약(_전체_요약.html)을 남깁니다").pack(side="left")
        self.b_stop2 = ttk.Button(a, text="■  중지", command=self.do_stop, state="disabled", style="Stop.TButton", cursor="hand2"); self.b_stop2.pack(side="left", padx=(P(8), 0))
        Tip(self.b_stop2, "지금 구획이 끝나면 멈춥니다. 고친 내용은 교정본에 남습니다.", self)
        self._btn(a, "교정본 열기", self.open_collated, tip="고친 사본(<이름>_교정.hwpx)을 한컴오피스로 엽니다").pack(side="right", padx=(P(6), 0))
        self._btn(a, "보고 열기", self.open_collate_report, tip="가장 최근 대조 보고(구획별 HTML)를 엽니다").pack(side="right", padx=(P(6), 0))
        self._hint(a, "원본 HWPX 는 건드리지 않고, 고친 사본 하나에 구획을 차례로 쌓습니다.").pack(side="left", fill="x", expand=True, padx=(P(14), P(8)))
        # 둘째 줄: 교정 방법과 표에서 고른 한 구획만 하는 단추(위 칸을 굴리지 않아도 늘 보이게)
        from .collate.labels import POLICIES
        ttk.Separator(act2).pack(fill="x", pady=(P(10), P(8)))
        r4 = ttk.Frame(act2, style="Panel.TFrame"); r4.pack(fill="x")
        ttk.Label(r4, text="교정 정책", style="Field.TLabel").pack(side="left", padx=(0, P(6)))
        ttk.Combobox(r4, textvariable=self.v_policy, values=list(POLICIES.values()), state="readonly", width=self._cw(POLICIES.values())).pack(side="left", padx=(0, P(14)))
        ttk.Label(r4, text="메모", style="Field.TLabel").pack(side="left", padx=(0, P(6)))
        cm = ttk.Combobox(r4, textvariable=self.v_cmemo, values=list(MEMO_LABELS.values()), state="readonly", width=self._cw(MEMO_LABELS.values())); cm.pack(side="left")
        Tip(cm, "고지 사항만(기본): 지운 글자·폭 없는 표시·구조 차이·반영하지 않은 제안에만 메모를 답니다. 자동 교정은 초록·파랑, 확인 요청은 빨강으로 칠하고, "
                "원래 글자와 근거 전체는 보고(collate 폴더의 html)에 남습니다. 0.6.3 처럼 모든 항목에 메모를 원하면 '모든 표시에 메모'.", self)
        self._btn(r4, "이 구획 교정", lambda: self.do_collate(apply=True), tip="표에서 고른 구획만 대조해 교정본에 고쳐 넣습니다").pack(side="right")
        self._btn(r4, "이 구획 대조만", lambda: self.do_collate(apply=False), tip="표에서 고른 구획만 대조해 보고를 만듭니다(문서는 고치지 않음). 표에서 두 번 눌러도 됩니다").pack(side="right", padx=(0, P(6)))
        ttk.Label(r4, text="표에서 고른 구획:", style="Field.TLabel").pack(side="right", padx=(0, P(8)))
        self.act_cards = [act, act2]

    def _update_scroll(self):
        """위 칸 내용이 넘칠 때만 스크롤 막대를 오른쪽 여백 위에 띄운다(카드 너비는 그대로)."""
        cv = self._canvas
        if self._host.winfo_reqheight() > cv.winfo_height() + 1:
            self._vsb.place(relx=1.0, rely=0.0, relheight=1.0, anchor="ne")
        else:
            self._vsb.place_forget(); cv.yview_moveto(0)

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

    def _bottom_keep(self, total: int | None = None) -> int:
        """아래 칸에 늘 남길 높이: 실행 줄·진행 막대·고르기 단추(기록을 뺀 부분) + 기록 2~5줄.
        창이 낮으면(노트북 화면) 설정 칸에 먼저 자리를 주고 기록 줄을 줄인다(설정 칸 최소 P(300))."""
        try:
            from tkinter import font as tkfont
            line = tkfont.Font(self, font=self.font_mono).metrics("linespace") + 2      # 기록 칸의 줄 사이(spacing1·spacing3) 포함
        except Exception:
            line = self.px(18)
        fixed = self._bottom.winfo_reqheight() - self.nb.winfo_reqheight() + self.px(26) + self.px(10)   # 기록 칸 여백 + 경계 손잡이 두께
        want = min(self._host.winfo_reqheight(), self.px(520))      # 설정 칸이 원하는 높이(책갈피 OCR 은 구획 표가 몇 줄 보이게)
        lines = 5 if total is None else max(2, min(5, (total - fixed - max(self.px(300), want)) // max(1, line)))
        return max(self.px(160), fixed + lines * line)

    def _fit_sash(self, force: bool = True):
        """위 칸이 내용을 다 보여 주게 경계를 내리되, 아래 진행·기록 칸은 최소 높이(실행 줄·막대·기록 다섯 줄)를 남긴다.
        force=False(창 크기가 바뀐 때)면 사용자가 끌어 놓은 자리를 두고, 아래 칸이 최소 높이보다 작아질 때만 올린다."""
        try:
            if force:
                self.update_idletasks()
            total = self.pane.winfo_height()
            if total <= 1:
                self.after(100, self._fit_sash); return
            keep = self._bottom_keep(total)
            low = max(self.px(80), min(self.px(160), total - keep))
            if force:
                pos = max(low, min(self._host.winfo_reqheight(), total - keep))
            else:
                cur = self.pane.sashpos(0)
                pos = max(low, min(cur, total - keep, self._host.winfo_reqheight()))
                if pos == cur:
                    return
            self.pane.sashpos(0, pos)
        except tk.TclError:
            pass

    # ---------- 책갈피 교정 탭 ----------
    def _build_collate_tab(self, tab):
        from .collate.labels import POLICIES, ANCHOR_SOURCES
        P = self.px
        self.v_hwpx = tk.StringVar(value=self.cfg.get("hwpx", "")); self.v_pdf2 = tk.StringVar(value=self.cfg.get("collate_pdf", "")); self.v_find = tk.StringVar()
        self.v_policy = tk.StringVar(value=POLICIES.get(self.cfg.get("collate_policy", "sure"), POLICIES["sure"]))
        self.v_cmemo = tk.StringVar(value=MEMO_LABELS.get(self.cfg.get("collate_memo", "notice"), MEMO_LABELS["notice"]))
        self.v_hstyles = tk.StringVar(value=self.cfg.get("heading_styles", ""))
        self.v_asrc = tk.StringVar(value=ANCHOR_SOURCES.get(self.cfg.get("anchor_source", "auto"), ANCHOR_SOURCES["auto"]))
        self.v_cforce = tk.BooleanVar(value=False)
        self.collate_rows: list[dict] = []
        c = self._card(tab, "교정할 문서"); c.pack(fill="x")
        b = c.body; b.columnconfigure(1, weight=1)
        rows = (("PDF", self.v_pdf2, "PDF 고르기…", self.pick_pdf2, "책갈피가 든 원본 PDF. 비우면 [일반 OCR] 의 PDF 를 씁니다."),
                ("HWPX", self.v_hwpx, "HWPX 고르기…", self.pick_hwpx, "교정할 HWPX. 원본은 그대로 두고 '<이름>_교정.hwpx' 사본에 고칩니다."),
                ("책갈피로 쓸 스타일", self.v_hstyles, "스타일 고르기…", self.pick_styles, "비우면 이름에 '제목'이 든 스타일 전부. 쉼표로 여러 개, 앞에 적은 것이 큰 단위입니다."))
        for i, (lab, var, bt, cmd, tip) in enumerate(rows):
            self._field_label(b, lab, i)
            e = ttk.Entry(b, textvariable=var); e.grid(row=i, column=1, sticky="ew", pady=P(3)); Tip(e, tip, self)
            self._btn(b, bt, cmd).grid(row=i, column=2, sticky="ew", padx=(P(8), 0), pady=P(3))
        foot = ttk.Frame(b, style="Panel.TFrame"); foot.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(P(6), 0))
        ttk.Checkbutton(foot, text="기록 있는 쪽도 다시 판독", variable=self.v_cforce).pack(side="right", padx=(P(12), 0))
        self._hint(foot, "판독기·모델·API 키·속도·읽기 방향은 [일반 OCR] 의 설정을 따릅니다. 읽기 방향이 '자동 판정'이면 글자층으로 가로·세로를 정합니다.").pack(side="left", fill="x", expand=True)

        g = self._card(tab, "구획 (책갈피·제목)"); g.pack(fill="x", pady=(P(10), 0))
        gb = g.body
        r3 = ttk.Frame(gb, style="Panel.TFrame"); r3.pack(fill="x")
        self._btn(r3, "구획 색인", self.do_collate_index, tip="HWPX 의 책갈피·제목 스타일로 구획을 나누고 PDF 책갈피와 짝을 맞춥니다").pack(side="left")
        ttk.Label(r3, text="기준", style="Field.TLabel").pack(side="left", padx=(P(14), P(6)))
        ttk.Combobox(r3, textvariable=self.v_asrc, values=list(ANCHOR_SOURCES.values()), state="readonly", width=self._cw(ANCHOR_SOURCES.values())).pack(side="left")
        e = ttk.Entry(r3, textvariable=self.v_find, width=16); e.pack(side="right"); e.bind("<KeyRelease>", lambda _e: self.refresh_collate_table())
        ttk.Label(r3, text="찾기(한자·독음)", style="Field.TLabel").pack(side="right", padx=(P(14), P(6)))
        Tip(e, "한자뿐 아니라 한글 독음으로도 찾습니다(대계 → 大薊, 인삼 → 人參·人蔘).", self)
        self.v_cstat = tk.StringVar(value="HWPX 를 고르고 [구획 색인]을 누르십시오.")
        self._hint(gb, textvariable=self.v_cstat).pack(fill="x", pady=(P(6), P(6)))
        tf = ttk.Frame(gb, style="Panel.TFrame"); tf.pack(fill="both", expand=True)
        cols = ("no", "lv", "name", "read", "para", "pdf", "page", "how")
        self.ctable = ttk.Treeview(tf, columns=cols, show="headings", height=7, selectmode="browse")
        for cc, t, w, an in (("no", "번호", 54, "e"), ("lv", "급", 40, "center"), ("name", "HWPX 책갈피·제목", 220, "w"), ("read", "독음", 120, "w"), ("para", "문단", 64, "center"),
                             ("pdf", "PDF 책갈피", 180, "w"), ("page", "PDF 쪽", 66, "center"), ("how", "짝", 74, "center")):
            self.ctable.heading(cc, text=t); self.ctable.column(cc, width=P(w), minwidth=P(min(w, 60)), anchor=an, stretch=(cc in ("name", "pdf", "read")))
        self.ctable.tag_configure("odd", background=self.colors["STRIPE"])
        sb = ttk.Scrollbar(tf, orient="vertical", command=self.ctable.yview); self.ctable.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y"); self.ctable.pack(side="left", fill="both", expand=True)
        self.ctable.bind("<Double-1>", lambda _e: self.do_collate(apply=False))
        self._hint(tab, "기록이 없는 쪽은 모델로 읽고, 入/人 같은 자형 편향 자리는 글자 조각을 모델에 다시 물어 가립니다. "
                        "자동 교정은 초록(3차 자형 판별 근거)·파랑(판독 일치·사전·반복·글자층 근거, 확인 권장), 확인 요청은 빨강으로 칠합니다. "
                        "메모는 지운 자리·넣지 않은 자리·구조 차이·반영하지 않은 제안에만 답니다(원래 글자와 근거 전체는 보고 화면). 이전처럼 모든 교정에 메모를 원하면 '모든 표시에 메모'.",
                   page=True).pack(fill="x", pady=(P(8), 0))

    def pick_pdf2(self):
        p = filedialog.askopenfilename(title="책갈피가 든 원본 PDF", filetypes=[("PDF", "*.pdf"), ("모든 파일", "*.*")])
        if p:
            self.v_pdf2.set(p); self.cfg["collate_pdf"] = p; save_config(self.cfg)

    def pdf2(self) -> str:
        """교정 탭의 PDF. 비어 있으면 인식 탭의 PDF 를 쓴다."""
        return self.v_pdf2.get().strip() or self.pdf()

    def work2(self) -> Path:
        return work_dir_for(self.pdf2())

    def pick_hwpx(self):
        p = filedialog.askopenfilename(title="교정할 HWPX", filetypes=[("HWPX", "*.hwpx"), ("모든 파일", "*.*")])
        if p:
            self.v_hwpx.set(p); self.cfg["hwpx"] = p; save_config(self.cfg)
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
        self.v_hstyles.set(", ".join(names)); self.cfg["heading_styles"] = self.v_hstyles.get(); save_config(self.cfg)

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
        self.cfg["anchor_source"] = src; self.cfg["heading_styles"] = self.v_hstyles.get(); save_config(self.cfg)
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
                    lines.append("   ⚠ 간체·번체 변환 패키지(opencc)가 없어 PDF 책갈피(간체)와 HWPX 제목(번체)이 맞지 않습니다. [일반 OCR] 의 고급 — [패키지 설치·점검] 뒤 프로그램을 다시 시작하십시오.")
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
        self.cfg.update({"hwpx": self.v_hwpx.get().strip(), "collate_pdf": self.v_pdf2.get().strip(), "collate_policy": self.collate_policy(), "collate_memo": self.collate_memo_mode()}); save_config(self.cfg)
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
        self.cfg.update({"hwpx": self.v_hwpx.get().strip(), "collate_pdf": self.v_pdf2.get().strip(), "collate_policy": self.collate_policy(), "collate_memo": self.collate_memo_mode()}); save_config(self.cfg)
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
        out = Path(h).with_name(Path(h).stem.replace("_교정", "") + "_교정.hwpx")
        if out.exists(): open_path(out)
        else: messagebox.showinfo(APP_NAME, f"교정본이 아직 없습니다: {out.name}")

    def _btn(self, parent, text, cmd, tip: str | None = None, **kw) -> ttk.Button:
        """작업 중에는 잠기는 단추. 너비는 글자에 맞춘다(고정 너비로 글자가 잘리던 문제). tip 은 마우스를 올리면 보이는 설명."""
        kw.pop("width", None)
        b = ttk.Button(parent, text=text, command=cmd, cursor="hand2", **kw); self.buttons.append(b)
        if tip:
            Tip(b, tip, self)
        return b

    # ---------- 도우미 ----------
    def pdf(self) -> str:
        return self.v_pdf.get().strip()

    def work(self) -> Path:
        return work_dir_for(self.pdf())

    def _need_pdf(self) -> bool:
        if not self.pdf() or not Path(self.pdf()).exists():
            messagebox.showinfo(APP_NAME, "먼저 [PDF·이미지 고르기…] 로 PDF 나 이미지를 고르십시오."); return False
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
                self.v_pdf.set(other); self.cfg["last_pdf"] = other; save_config(self.cfg); self.refresh_table()
                self._append(f"PDF: {other}\n결과 폴더: {self.work()}\n")
                return True
            return False
        messagebox.showinfo(APP_NAME, f"판독 기록이 없습니다.\n결과 폴더: {self.work()}\n\n먼저 [▶ 한 번에 인식] 을 하거나, "
                                      f"기록이 있는 원본 PDF 를 [PDF·이미지 고르기…] 로 고르십시오(복사본 PDF 옆에는 빈 결과 폴더가 새로 생깁니다).")
        return False

    def _read_opts(self, print_offset: bool = True, force: bool | None = None) -> list[str]:
        o = ["--reader", self.v_reader.get(), "--upscale", self.v_up.get().strip() or "2"]
        if self.v_reader.get() == "gemini" and self.v_model.get().strip():
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

    def _glyph_opts(self, suspects: bool = False) -> list[str]:
        """3차 판별 선택(run·adjudicate·collate 에만 — read 명령에는 없는 선택이다). suspects=True(run·adjudicate)면 합의 판독 의심을 끈 것도 넘긴다
        (collate 에는 그 선택이 없다)."""
        v = getattr(self, "v_suspects", None)
        return (["--adjudicate-blind"] if self.v_blind.get() else []) + (["--no-suspects"] if suspects and v is not None and not v.get() else [])

    def _build_opts(self) -> list[str]:
        o = []
        if self.v_docx.get(): o.append("--docx")
        if not self.v_join.get(): o.append("--no-join-pages")
        if not self.v_hl.get(): o.append("--no-highlight")
        if not self.v_ctx.get(): o.append("--no-context")
        if not self.v_pb.get(): o.append("--no-page-breaks")
        o += ["--check-list", self.check_list_mode(), "--memo", self.memo_mode()]
        return o

    def memo_mode(self) -> str:
        return next((k for k, v in MEMO_LABELS.items() if v == self.v_memo.get()), "notice")

    def collate_memo_mode(self) -> str:
        return next((k for k, v in MEMO_LABELS.items() if v == self.v_cmemo.get()), "notice")

    def check_list_mode(self) -> str:
        return next((k for k, v in CHECK_LIST_LABELS.items() if v == self.v_chk.get()), "none")

    def mode_key(self) -> str:
        return next((k for k, v in MODE_LABELS.items() if v == self.v_mode.get()), "auto")

    def media_res_key(self) -> str:
        return next((k for k, v in MRES_LABELS.items() if v == self.v_mres.get()), "high")

    def page_think_key(self) -> str:
        return next((k for k, v in PAGE_THINK_LABELS.items() if v == self.v_think1.get()), "auto")

    def page_mres_key(self) -> str:
        return next((k for k, v in PAGE_MRES_LABELS.items() if v == self.v_mres1.get()), "auto")

    def toggle_advanced(self):
        self.v_adv.set(not self.v_adv.get()); self._apply_advanced()
        self.cfg["advanced_open"] = self.v_adv.get(); save_config(self.cfg)
        if self.v_adv.get():
            self.after(60, lambda: self._reveal(self.adv_frame))          # 펼친 단추들이 화면 밖이면 보이게 굴린다

    def _apply_advanced(self):
        if self.v_adv.get():
            self.adv_frame.pack(fill="x", pady=(self.px(6), 0)); self.b_adv.configure(text="▾  고급 — 단계별 실행·도구")
        else:
            self.adv_frame.pack_forget(); self.b_adv.configure(text="▸  고급 — 단계별 실행·도구")
        if hasattr(self, "pane"):
            self.after_idle(self._fit_sash)

    def toggle_details(self):
        self.v_details.set(not self.v_details.get()); self._apply_details()
        self.cfg["details_open"] = self.v_details.get(); save_config(self.cfg)
        if self.v_details.get():
            self.after(60, lambda: self._reveal(self.details))

    def _apply_details(self):
        """판독 카드의 세부 설정(2차 판독 모델·속도·조각·단계)을 펼치거나 접는다. 보통은 기본값이면 된다."""
        if self.v_details.get():
            self.details.grid(row=5, column=0, columnspan=3, sticky="ew"); self.b_details.configure(text="세부 설정 접기 ▴")
        else:
            self.details.grid_remove(); self.b_details.configure(text="세부 설정 ▾")
        if hasattr(self, "pane"):
            self.after_idle(self._fit_sash)

    def _prepare_env(self) -> bool:
        if self.v_reader.get() == "gemini":
            from .reader_gemini import split_keys, join_keys, key_shape_warning
            keys = split_keys(self.v_key.get())
            key = join_keys(keys)
            if key != self.v_key.get():
                self.v_key.set(key)                  # 공백·따옴표 등을 걷어 낸 값으로 바꿔 보여 준다(키가 여러 개면 쉼표로)
            if not keys:
                messagebox.showwarning(APP_NAME, "Gemini API 키가 없습니다. 설정에 키를 넣거나 판독기를 '기존 텍스트 레이어만' 으로 바꾸십시오."); return False
            warn = "\n".join((f"키 {i}: " if len(keys) > 1 else "") + w for i, w in ((i, key_shape_warning(k)) for i, k in enumerate(keys, 1)) if w)
            if warn and not messagebox.askyesno(APP_NAME, warn + "\n\n그래도 이 키로 진행할까요?"):
                return False
            os.environ["GEMINI_API_KEY"] = key
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
        self.log.see("end-2c"); self.log.configure(state="disabled")    # 끝의 빈 줄 대신 마지막 글줄이 보이게

    def _set_running(self, on: bool, command: str = ""):
        """작업 중에는 단추를 잠그고, 진행 막대를 0 에서 새로 시작한다(좌우로 오가는 막대 대신 끝까지 차는 막대)."""
        self.running = on
        for b in self.buttons:
            b.configure(state="disabled" if on else "normal")
        if getattr(sys, "frozen", False) and getattr(self, "b_packages", None) is not None:
            self.b_packages.configure(state="disabled")
        for b in (self.b_stop, getattr(self, "b_stop2", None)):
            if b is not None:
                b.configure(state="normal" if on and command != "update" else "disabled")
        if on:
            self.tracker = Tracker(command)
            self.progress.configure(value=0); self._prog_shown = 0.0
            self.v_prog.set("0%" if self.tracker.plan else "")

    def _bar_value(self, f: float) -> int:
        """0~1 → 막대 값(0~1000). 0 보다 크면 둥근 끝 두 개가 들어갈 만큼은 보이게 한다(겹쳐 그려지는 모양 방지)."""
        v = int(round(f * 1000))
        width = max(1, self.progress.winfo_width())
        if 0 < v < 1000:
            v = max(v, -(-self._bar_cap * 1000 // width))
        return min(1000, v)

    def _on_progress(self, stage: str, done: int, total: int):
        f = self.tracker.update(stage, done, total)
        self.progress.configure(value=self._bar_value(f)); self._prog_shown = time.time()
        self.v_prog.set(self.tracker.live_text())

    def start(self, argv: list[str], label: str, open_after: Path | None = None):
        if self.running:
            return
        self.open_after = open_after
        cmd = argv[0] if argv else ""
        self._set_running(True, cmd); self.v_status.set(label + " 중…")
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
                    cli.PROGRESS = lambda st, d, t: self.q.put(("progress", st, d, t))
                    try:
                        rc = cli.main(argv)
                    finally:
                        cli.PROGRESS = None
            except SystemExit as e:
                rc = e.code if isinstance(e.code, int) else 1
                if e.code and not isinstance(e.code, int): self.q.put(("log", str(e.code) + "\n"))
            except ModuleNotFoundError as e:
                self.q.put(("log", f"\n오류: 패키지가 없습니다: {e.name}. [▸ 고급 → 패키지 설치·점검] 단추를 누르십시오.\n"))
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
                    self._on_progress(item[1], item[2], item[3])
                elif item[0] == "bundle":
                    self._bundle_msg(*item[1:])
                elif item[0] == "table":
                    self._fill_table(*item[1:])
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
        if getattr(self, "_closing", False) and getattr(self, "_update_installing", False):
            return
        self._offer_update()
        if getattr(self, "_closing", False):
            return
        if self.running and time.time() - self._prog_shown > 1.0:
            self.v_prog.set(self.tracker.live_text()); self._prog_shown = time.time()      # 남은 시간(또는 지난 시간) 글을 1초마다 새로
        self.after(100, self._poll)

    def _finish(self, rc, label):
        self._set_running(False)
        if getattr(self, "_closing", False):             # 닫는 중에 끝났으면 알림창 없이 조용히
            self._last_writer = None; return
        w = getattr(self, "_last_writer", None)
        kind, status, head = finish_words(rc, w.problems if w is not None else None)    # [중지]로 멈춘 명령은 130(멈추기 전에 일을 다 끝냈으면 0), 실패한 쪽이 있으면 3, 3차 판별을 못 마치면 4
        ok, partial, stopped = kind == "ok", kind == "partial", kind == "stopped"
        self.v_status.set(f"{label} {status}")
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
    def choose_pdf(self):
        imgs = " ".join("*" + e for e in IMAGE_EXTS)
        ps = filedialog.askopenfilenames(title="PDF·이미지 고르기 (이미지는 여러 장 고를 수 있음)",
                                         filetypes=[("PDF·이미지", "*.pdf " + imgs), ("PDF", "*.pdf"), ("이미지", imgs), ("모든 파일", "*.*")])
        ps = [p for p in (ps or ()) if p]
        if not ps:
            return
        if len(ps) > 1:
            self._bundle_images(ps); return             # 묶기는 작업 스레드에서 — 끝나면 _use_pdf 로 이어진다
        self._use_pdf(ps[0])

    def _use_pdf(self, p: str):
        self.v_pdf.set(p); self.cfg["last_pdf"] = p; save_config(self.cfg); self.refresh_table()
        kind = "이미지" if Path(p).suffix.lower() in IMAGE_EXTS else "PDF"
        self._append(f"{kind}: {p}\n결과 폴더: {self.work()}\n")
        if kind == "이미지" and self.mode_key() == "auto":
            self._append("  이미지에는 글자층이 없어 읽기 방향을 세로쓰기로 봅니다. 가로쓰기 책이면 [읽기 방향] 을 바꾸십시오.\n")

    @staticmethod
    def _bundle_path(srcs: list[Path], progress=None) -> Path:
        """묶음 파일 경로: '<첫 이름>~<끝 이름> (N장 지문).pdf'. 지문은 고른 이미지마다 (이름·크기·내용의 sha1) 을 이름 순서로 모아 낸
        sha1 앞 8자리라 가운데 이미지만 다른 선택이나 바꿔 넣은·고친 이미지는 이름이 달라지고(결과 폴더도 따로), 폴더를 옮기거나 복사해
        경로·수정 시각이 바뀌어도 그대로다(묶음·결과 폴더를 함께 옮기면 판독 기록이 이어진다). 내용은 8MiB 이하면 파일 전체(BMP·TIFF 의
        가운데만 고쳐 크기가 같아도 알아본다), 넘으면 앞·가운데·끝 64KiB 씩만 읽는다. progress(끝난 장, 전체) 로 진행을 알린다(느린 드라이브)."""
        import hashlib
        h = hashlib.sha1()
        for i, s in enumerate(srcs, 1):
            with open(s, "rb") as f:
                n = os.fstat(f.fileno()).st_size; d = hashlib.sha1()
                if n <= 8 << 20:
                    d.update(f.read())
                else:
                    for at in (0, n // 2, n - 65536):
                        f.seek(at); d.update(f.read(65536))
            h.update(f"{s.name}|{n}|{d.hexdigest()}\n".encode("utf-8"))
            if progress: progress(i, len(srcs))
        return srcs[0].with_name(f"{srcs[0].stem}~{srcs[-1].stem} ({len(srcs)}장 {h.hexdigest()[:8]}).pdf")

    def _bundle_images(self, paths: list[str]) -> None:
        """이미지 여러 장 → 이름의 숫자 순서로 한 PDF 로 묶어 첫 이미지 옆에 저장(무손실: JPEG 는 그대로, 나머지는 PNG).
        이름에 고른 이미지들의 지문이 들어가므로 정확히 같은 선택으로 이미 묶은 파일이 있을 때만 그대로 쓴다(판독 기록이 이어지게).
        지문 계산과 묶기는 작업 스레드에서 하고(느린 드라이브에서도 창이 멈추지 않게) 끝날 때까지 단추를 잠근다. 끝나면 _bundle_msg 가 _use_pdf 로 잇는다."""
        bad = [p for p in paths if Path(p).suffix.lower() not in IMAGE_EXTS]
        if bad:
            messagebox.showinfo(APP_NAME, "여러 개를 고를 때는 이미지만 고르십시오(PDF 는 한 번에 하나).\n  " + "\n  ".join(Path(b).name for b in bad[:5])); return
        if self.running: return
        from .pdfio import _natural_key
        srcs = sorted((Path(p) for p in paths), key=_natural_key)
        names = " → ".join(s.name for s in srcs[:8]) + (" …" if len(srcs) > 8 else "")
        self._set_running(True); self.v_status.set(f"이미지 {len(srcs)}장을 한 권으로 묶는 중…")
        for b in (self.b_stop, getattr(self, "b_stop2", None)):          # 묶기는 중간에 멈추지 않는다
            if b is not None: b.configure(state="disabled")
        def t():
            out = part = None; log = ""
            err = "이미지를 묶지 못했습니다"
            try:
                out = self._bundle_path(srcs, progress=lambda d, n: self.q.put(("bundle", "hash", d, n)))
                if out.exists():
                    err = None; log = f"같은 이미지 {len(srcs)}장으로 묶은 파일을 다시 씀: {out.name}\n  {names}\n"; return
                old = srcs[0].with_name(f"{srcs[0].stem}~{srcs[-1].stem} ({len(srcs)}장).pdf")       # 0.6.4 전 이름(지문 없음)
                part = out.with_name(out.name + ".part")                       # 다 쓴 뒤에만 이름을 붙인다(끊긴 묶음을 다시 쓰지 않게)
                from .pdfio import save_images_as_pdf
                save_images_as_pdf(srcs, part, progress=lambda d, n: self.q.put(("bundle", "prog", d, n)))
                os.replace(part, out); err = None
                log = f"이미지 {len(srcs)}장을 이름 순서로 묶음: {out.name}\n  {names}\n" + (
                    f"  참고: 예전 판이 묶은 '{old.name}' 이(가) 옆에 있습니다 — 그 판독 기록은 '{work_dir_for(str(old)).name}' 에 따로 있으니 이어 쓰려면 [PDF·이미지 고르기…] 로 그 PDF 를 고르십시오.\n"
                    if old.exists() and work_dir_for(str(old)).is_dir() else "")                # 판독 기록(결과 폴더)이 없으면 알리지 않는다
            except ModuleNotFoundError as e:
                err = f"패키지가 없어 이미지를 묶지 못했습니다: {e.name}. [▸ 고급 → 패키지 설치·점검] 을 누르십시오."
            except Exception as e:
                err = f"이미지를 묶지 못했습니다: {e}"
            finally:
                if err and part is not None:
                    with contextlib.suppress(OSError): part.unlink()
                self.q.put(("bundle", "done", out, err, log))
        threading.Thread(target=t, daemon=True).start()

    def _bundle_msg(self, kind: str, *a):
        """작업 스레드의 묶기 알림(주 스레드에서 _poll 이 부른다): ('prog', 끝난 장, 전체) 또는 ('done', 묶음 경로, 오류, 기록 글)."""
        if kind in ("prog", "hash"):
            d, n = a
            self.progress.configure(value=self._bar_value(d / n if n else 1.0))
            self.v_status.set(f"이미지를 한 권으로 묶는 중… {d}/{n}장" if kind == "prog" else f"고른 이미지 확인 중(같은 묶음이 있는지)… {d}/{n}장"); return
        out, err, log = a
        self._set_running(False); self.v_status.set("준비됨")
        if err:
            self.progress.configure(value=0); self.v_prog.set("이미지 묶기 실패")
            messagebox.showerror(APP_NAME, err); return
        self.progress.configure(value=1000); self.v_prog.set("이미지 묶기 끝")
        self._append(log); self._use_pdf(str(out))

    def toggle_key(self):
        self.e_key.configure(show="" if self.e_key.cget("show") else "•")

    def check_key(self):
        """키가 쓸 수 있는지 Gemini 에 물어보고, 쓸 수 있는 모델 목록에서 고르게 한다."""
        if self.running: return
        from .reader_gemini import split_keys, join_keys
        keys = split_keys(self.v_key.get()); self.v_key.set(join_keys(keys))
        model = self.v_model.get().strip()
        self._set_running(True); self.v_status.set("키 확인 중… (모델을 실제로 짧게 호출해 봅니다)")
        def t():
            try:
                from .reader_gemini import check_key
                res = check_key(keys[0] if keys else "", model)
                if res.get("ok") and len(keys) > 1:      # 나머지 키는 쓸 수 있는지만(모델 목록 조회) 본다
                    bad = [f"키 {i}(…{k[-4:]})" for i, k in enumerate(keys[1:], 2) if not check_key(k, model, probe=False).get("ok")]
                    res["message"] += f" · 키 {len(keys)}개" + (f" — 쓸 수 없는 키: {', '.join(bad)}" if bad else " 모두 쓸 수 있음(하루 한도에 걸리면 다음 키로 넘어감)")
            except Exception as e:
                res = {"ok": False, "message": f"확인 실패: {e}", "models": [], "shape_warning": ""}
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

    def _reader_changed(self):
        st = "normal" if self.v_reader.get() == "gemini" else "disabled"
        self.e_model.configure(state=st); self.e_key.configure(state=st); self.e_model2.configure(state=st)

    def save_settings(self, quiet=False):
        self.cfg.update({"pages": self.v_pages.get(), "print_offset": self.v_off.get(), "upscale": self.v_up.get(), "reader": self.v_reader.get(),
                         "model": self.v_model.get(), "api_key": self.v_key.get(), "second_pass": self.v_second.get(), "docx": self.v_docx.get(),
                         "join_pages": self.v_join.get(), "force": self.v_force.get(), "highlight": self.v_hl.get(), "page_breaks": self.v_pb.get(), "check_list": self.check_list_mode(),
                         "context_hint": self.v_ctx.get(), "memo_mode": self.memo_mode(),
                         "workers": int(self.v_workers.get() or 8), "second_model": self.v_model2.get().strip(),
                         "checkpoint": int(self.v_ckpt.get() or 0), "use_cache": self.v_cache.get(), "second_thinking_low": self.v_think2.get(), "adjudicate_blind": self.v_blind.get(), "last_pdf": self.pdf(),
                         "chunk_lines": int(self.v_chunk.get() or 0), "media_res": self.media_res_key(), "mode": self.mode_key(),
                         "page_thinking": self.page_think_key(), "page_media_res": self.page_mres_key()})
        if getattr(self, "v_suspects", None) is not None:
            self.cfg["suspects"] = self.v_suspects.get()
        mc = max_calls_value(self.v_maxcalls.get())
        self.cfg["max_calls"] = mc if mc is not None else self.cfg.get("max_calls", 0)     # 잘못 넣은 값은 저장하지 않는다(실행은 _prepare_env 가 막음)
        save_config(self.cfg)
        if not quiet:
            self.v_status.set(f"설정 저장됨 → {CONFIG_PATH}")

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
        save_config(self.cfg)

    def fx_setting(self) -> tuple[float | None, str]:
        """환율: 직접 입력값 > 자동 조회값 > (없음 → estimate 가 기본값을 씀)."""
        fx = str(self.cfg.get("fx_krw") or "").strip()
        if fx:
            try: return float(fx), "직접 입력"
            except ValueError: pass
        if self._fx:
            return self._fx
        return None, ""

    def estimate_data(self) -> dict:
        """현재 설정으로 호출·토큰·비용 어림. 단가는 내장 요금표, 환율은 자동 조회라 따로 넣을 것이 없다."""
        from .estimate import estimate
        from .pdfio import open_pdf, parse_pages
        from .state import Work
        with open_pdf(self.pdf()) as doc:
            pages = parse_pages(self.v_pages.get().strip() or None, len(doc))
        w = Work(self.pdf(), str(self.work()), initialize=False)
        same_source = w.source_matches(300)              # 창의 판독 명령은 기본 DPI 300 을 쓴다
        fx, fx_src = self.fx_setting()
        e = estimate(self.pdf(), pages, upscale=float(self.v_up.get() or 2), second_pass=self.v_second.get(), model=self.v_model.get().strip(),
                        second_model=self.v_model2.get().strip(), use_cache=self.v_cache.get(),
                        prices=self.cfg.get("prices") or {}, fx_krw=fx, fx_source=fx_src,
                        records=w.records(pages, repair=False) if same_source and not self.v_force.get() else [])
        if not same_source and any(w.readings_dir.glob("p*.json")):
            e["notes"].append("원본·영상 설정이 바뀌었거나 기존 기록의 원본을 확인할 수 없어 선택한 쪽을 모두 새로 판독하는 비용입니다. 실행할 때 이전 결과는 보관됩니다.")
        return e

    def estimate_text(self) -> str:
        try:
            from .estimate import format_estimate
            return format_estimate(self.estimate_data())
        except ModuleNotFoundError as e:
            return f"패키지가 없어 계산하지 못했습니다: {e.name}. [▸ 고급 → 패키지 설치·점검] 을 누르십시오."
        except Exception as e:
            return f"계산 실패: {e}"

    def do_estimate(self):
        if not self._need_pdf():
            return
        try:
            EstimateDialog(self, self.estimate_data())
        except ModuleNotFoundError as e:
            messagebox.showerror(APP_NAME, f"패키지가 없어 계산하지 못했습니다: {e.name}. [▸ 고급 → 패키지 설치·점검] 을 누르십시오.")
        except Exception as e:
            messagebox.showerror(APP_NAME, f"비용 계산 실패: {e}")

    def _confirm_cost(self) -> bool:
        """Gemini 판독 전에 어림을 보여 주고 계속할지 묻는다. 읽을 쪽이 없으면 묻지 않는다."""
        if self.v_reader.get() != "gemini":
            return True
        try:
            e = self.estimate_data()
        except Exception as ex:
            return messagebox.askyesno(APP_NAME, f"비용을 어림하지 못했습니다({ex}). 그래도 판독을 시작할까요?")
        if e["calls"] == 0:
            return True
        d = EstimateDialog(self, e, go=True); self.wait_window(d)
        return d.proceed

    def do_inspect(self):
        if self._need_pdf(): self.start(["inspect", self.pdf()], "① 구조 보기")

    def do_prepare(self):
        if self._need_pdf(): self.start(["prepare", self.pdf(), "--work", str(self.work())] + self._pages(), "② 준비")

    def do_read(self):
        if self._need_pdf() and self._prepare_env() and self._confirm_cost():
            self.start(["read", self.pdf(), "--work", str(self.work())] + self._pages() + self._read_opts(), "③ 판독")

    def do_build(self):
        if self._need_pdf(): self.start(["build", self.pdf(), "--work", str(self.work())] + self._pages() + self._build_opts(), "④ 문서 만들기")

    def do_review(self):
        if not self._need_records("검수 화면"): return
        rv = self.work() / "review.html"
        self.start(["review", self.pdf(), "--work", str(self.work())] + self._pages(), "⑤ 검수 화면", open_after=rv)

    def do_report(self):
        if self._need_records("보고"):
            self.nb.select(0)
            self.start(["report", self.pdf(), "--work", str(self.work())], "⑥ 보고")

    def do_run(self):
        if self._need_pdf() and self._prepare_env() and self._confirm_cost():
            self.nb.select(0)
            self.start(["run", self.pdf(), "--work", str(self.work())] + self._pages() + self._read_opts() + self._glyph_opts(suspects=True) + self._build_opts(), "한 번에 인식 (PDF 전체 → HWPX)" if not self.v_pages.get().strip() else f"한 번에 인식 ({self.v_pages.get().strip()}쪽 → HWPX)")

    def do_stop(self):
        try:
            from . import cli
            cli.CANCEL.set(); self.v_status.set("중지 요청 — 지금 쪽이 끝나면 멈춥니다")
        except Exception:
            pass

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
        """3차 글자 판별: 〔a/b〕 자리마다 글자 조각을 모델에 묻는다. 자리 수를 먼저 세어 보여 주고 묻는다."""
        if not self._need_records("3차 글자 판별") or not self._prepare_env():
            return
        if self.v_reader.get() != "gemini":
            messagebox.showinfo(APP_NAME, "3차 판별은 자형을 보는 모델이 필요합니다. 판독기를 Gemini 로 바꾸십시오."); return
        sus = bool(self.v_suspects.get()); n_sus = sus_calls = None
        try:
            from .state import Work
            from .adjudicate import find_sites
            from .schema import PageReading
            from .pdfio import parse_pages, open_pdf
            w = Work(self.pdf(), str(self.work()))
            with open_pdf(self.pdf()) as doc:
                recs = w.records(parse_pages(self.v_pages.get().strip() or None, len(doc)))
                per_page = [len(find_sites(PageReading.model_validate(r["merged"]))) for r in recs]
                n_sites = sum(per_page); n_calls = sum(-(-n // 8) for n in per_page if n)
                if sus and recs:
                    try:                                 # 합의 의심도 모델 없이 미리 센다. 조각을 읽는 동안만 PDF 를 연다.
                        from . import cli
                        from .adjudicate import suspects_pending, suspect_freq, SUSPECT_MAX
                        freq = suspect_freq(w.dir)
                        ks = [suspects_pending(r, cli._load_layer(w, doc, r["pdf_page"]).get("plain", ""), SUSPECT_MAX, False, freq) for r in recs]
                        n_sus = sum(ks); sus_calls = sum(-(-k // 8) for k in ks if k)
                    except Exception:
                        pass                             # 못 세면 수 없이 알린다.
        except Exception as e:
            messagebox.showerror(APP_NAME, f"판독 기록을 읽지 못했습니다: {e}"); return
        if not recs:
            messagebox.showinfo(APP_NAME, f"이 쪽 범위에 판독 기록이 없습니다.\n결과 폴더: {self.work()}"); return
        sus_note = ("1차·2차가 같게 읽었지만 PDF 글자층과 자형이 헷갈리는 쌍(入/人 등) 한 글자가 다른 자리(합의 판독 의심"
                    + (f") {n_sus}곳도 같은 방식으로 묻습니다(쪽당 최대 8곳 · 묶음 호출 약 {sus_calls}회 더" + (f", 재확인 최대 약 {sus_calls}회 더" if self.v_blind.get() else "") + ")"
                       if n_sus is not None else ", 쪽당 최대 8곳)도 같은 방식으로 묻습니다") + " — 본문은 그대로, 확인 항목만.\n")
        if not n_sites and (not sus or n_sus == 0):
            messagebox.showinfo(APP_NAME, f"판독 기록 {len(recs)}쪽에 판별할 〔a/b〕 자리가 없습니다(1차·2차가 갈린 자리가 없음)."
                                + ("\n합의 판독 의심 자리도 새로 물을 곳이 없습니다." if sus else "")); return
        if not n_sites:
            if not messagebox.askyesno(APP_NAME, f"판독 기록 {len(recs)}쪽에 판별할 〔a/b〕 자리가 없습니다(1차·2차가 갈린 자리가 없음).\n" + sus_note
                                       + ("물을 자리가 없으면 호출 없이 끝납니다. " if n_sus is None else "") + "계속할까요?"):
                return
        elif not messagebox.askyesno(APP_NAME, f"1차·2차가 갈린 자리 {n_sites}곳을 글자 하나씩 크게 잘라 모델에 묻습니다(쪽마다 8자리씩 묶어 한 호출, 쪽은 동시에 — 묶음 호출 약 {n_calls}회, 빠진 자리는 낱개로 더).\n"
                                             "앞뒤 글자는 글로 주지 않고(조각에는 보임) 후보 차례를 섞어 자형만 묻습니다(눈가림)."
                                             + (f" 고른 자리는 후보 없이 한 번 더 읽어 같을 때만 확정합니다(호출 최대 약 {n_calls}회 더)." if self.v_blind.get() else "") + "\n"
                                             f"획이 보이면 확정해 본문에 넣고, 보이지 않으면 표시를 그대로 둡니다. " + (sus_note if sus else "") + "계속할까요?"):
            return
        self.nb.select(0)
        argv = ["adjudicate", self.pdf(), "--work", str(self.work())] + self._pages() + (["--model", self.v_model.get().strip()] if self.v_model.get().strip() else [])
        argv += ["--workers", (self.v_workers.get().strip() or "8")] + self._glyph_opts(suspects=True) + self._cap_opts()
        self.start(argv, "3차 글자 판별")

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

    def open_hwpx(self):
        if not self._need_pdf(): return
        files = sorted((self.work() / "out").glob("*.hwpx"), key=lambda p: p.stat().st_mtime) if (self.work() / "out").exists() else []
        if not files:
            messagebox.showinfo(APP_NAME, "아직 HWPX 가 없습니다. [④ 문서 만들기] 를 먼저 누르십시오."); return
        open_path(files[-1])

    def show_help(self):
        messagebox.showinfo(f"{APP_NAME} 도움말", HELP)

    # ---------- 업데이트 ----------
    def _save_update_preference(self):
        self.cfg["auto_update"] = self.v_auto_update.get()
        save_config(self.cfg)

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
            dialog = UpdateDialog(self, info, bool(getattr(sys, "frozen", False)) and sys.platform == "win32")
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
        if not getattr(sys, "frozen", False) or sys.platform != "win32":
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

    def _pkg_result(self, missing: list[str]):
        if getattr(sys, "frozen", False):
            self.v_pkg.set("내장 패키지 확인 필요" if missing else "패키지 내장 · 준비됨")
            if missing:
                self._append("배포 파일에서 패키지를 불러오지 못했습니다: " + ", ".join(missing) + "\n공식 릴리스 실행 파일을 다시 내려받으십시오.\n")
            return
        if missing:
            self.v_pkg.set(f"빠진 패키지 {len(missing)}개 → ▸ 고급의 [패키지 설치·점검]")       # 긴 목록은 머리띠를 밀어내므로 기록에
            self._append("빠진 패키지: " + ", ".join(missing) + "\n")
            if not getattr(self, "_asked_install", False) and not self.running:
                self._asked_install = True
                if messagebox.askyesno(APP_NAME, "필요한 패키지가 빠져 있습니다:\n  " + ", ".join(missing) + "\n\n지금 설치할까요? (몇 분 걸릴 수 있습니다)"):
                    self.install_packages()
        else:
            self.v_pkg.set("패키지 모두 있음")

    def install_packages(self):
        if getattr(sys, "frozen", False):
            messagebox.showinfo(APP_NAME, "Windows 실행 파일에는 필요한 패키지가 포함되어 있습니다.\n[업데이트 확인]에서 새 실행 파일을 설치할 수 있습니다.")
            return
        if self.running: return
        req = Path(__file__).resolve().parent.parent / "requirements.txt"
        cmd = [sys.executable, "-m", "pip", "install", "--upgrade"] + (["-r", str(req)] if req.exists() else [p for _, p in PACKAGES])
        self._set_running(True, "install"); self.v_status.set("패키지 설치 중… (몇 분 걸릴 수 있음)"); self.nb.select(0)
        self.q.put(("log", "\n▶ 패키지 설치\n   " + " ".join(cmd) + "\n"))
        wanted = {p.lower().replace("_", "-") for _, p in PACKAGES}
        def t():
            rc = 1; seen: set[str] = set(); n_get = got = 0
            try:
                p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", env=child_env())   # 설치 스크립트가 API 키를 물려받지 않게
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

    def on_close(self):
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
        if keep_waiting(w is not None and w.is_alive(), self._closing_since, time.time()):
            self.after(200, self._close_when_idle); return
        self.save_settings(quiet=True)
        self.destroy()


def main():
    app = App()
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
