"""YeonE_OCR 창 프로그램. 모든 기능을 단추로 조작한다.

표준 라이브러리 tkinter 만 쓰므로 Python 만 있으면 뜬다. 무거운 패키지(pymupdf, hwpx, google-genai…)는
단추를 눌렀을 때 비로소 불러오며, 없으면 [필요한 패키지 설치·점검] 단추로 설치한다.
"""
from __future__ import annotations
import contextlib, io, json, os, queue, subprocess, sys, threading, traceback, webbrowser
from pathlib import Path
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from tkinter.scrolledtext import ScrolledText

from . import __version__

APP_NAME = "YeonE_OCR"
CONFIG_PATH = Path.home() / ".yeone_ocr" / "config.json"
PACKAGES = [("pymupdf", "pymupdf"), ("PIL", "pillow"), ("numpy", "numpy"), ("cv2", "opencv-python-headless"),
            ("pydantic", "pydantic"), ("google.genai", "google-genai"), ("hwpx", "python-hwpx"), ("docx", "python-docx")]
DEFAULTS = {"pages": "", "print_offset": "", "upscale": "2", "reader": "gemini", "model": "gemini-3.1-pro-preview",
            "api_key": "", "second_pass": True, "docx": False, "join_pages": True, "force": False, "highlight": True, "page_breaks": True,
            "prices": {}, "fx_krw": "", "workers": 6, "second_model": "auto", "checkpoint": 20, "use_cache": True, "second_thinking_low": True, "check_list": "none", "last_pdf": ""}
CHECK_LIST_LABELS = {"none": "없음(본문만)", "extra": "본문에 표시 못 한 것만", "all": "전부(검수 화면과 같음)"}
STATUSES = ["원본 대조 완료(사람)", "판독 확인 필요", "이미지 보존 확인 완료", "문서 반영 완료", "파일 구조 검사 완료"]

HELP = f"""{APP_NAME} · 연어OCR {__version__}

세로쓰기 한자·한글 혼용 문헌(스캔 PDF)을 지침에 따라 OCR·교정해 HWPX 로 만듭니다.
글자는 Gemini 가 읽고, 프로그램은 영역 잘라내기·두 판독 비교·문서 조립·상태 기록을 맡습니다.

차례
 1. [필요한 패키지 설치·점검] — 처음 한 번.
 2. API 키를 넣고 [키 확인·모델 고르기] — 키가 맞는지 확인하고 쓸 수 있는 모델을 고릅니다. (https://aistudio.google.com/apikey)
 3. [PDF 고르기…] 로 파일을 고릅니다. 쪽 범위는 비워 두면 전체이고, 인쇄 쪽수 차이(예 29)를 적어 두면 쪽수 어긋남을 잡아 줍니다.
 4. [▶ PDF 전체를 HWPX로] 를 누르면 먼저 예상 호출·토큰·비용을 보여 주고([비용 미리 보기] 와 같음), [계속 실행] 을 누르면
    준비 → 판독 → 문서 만들기 → 검수 화면 → 보고 까지 한 번에 갑니다. 단가를 한 번 넣어 두면 금액도 나옵니다.
    쪽이 많으면 오래 걸립니다(쪽당 30초~1분). [■ 중지] 로 멈춰도 끝난 쪽은 남고, 다시 누르면 남은 쪽부터 이어서 합니다.
 5. [⑤ 검수 화면 열기] 에서 원본과 대조하고, 글자를 고치려면 결과 폴더의 readings/pNNN.json 을 고친 뒤 [④ 문서 만들기] 를 다시 누릅니다.
    [비교 다시 계산] 은 저장된 1차·2차 판독으로 확인 표시만 다시 만듭니다(모델 호출 없음). "기록 있는 쪽도 다시 판독" 을 켜면 ③ 판독이 기존 기록을 덮어씁니다.
 6. 사람이 대조를 마친 쪽은 [검수 결과 기록…] 으로 "원본 대조 완료(사람)" 을 남깁니다.

결과는 PDF 와 같은 폴더의 「<PDF 이름>_YeonE_OCR」 안에 생깁니다.
 out/      HWPX (+DOCX)
 review.html  검수 화면      report.md  상태 보고
 readings/ 쪽별 판독 기록    crops/     이미지로 보존한 도판

〔 〕 표시는 확인이 필요한 자리입니다. 프로그램은 추측으로 채우지 않습니다.
"""


SPEED_DEFAULTS_VERSION = 3  # 2: 0.4.1 동시 6(자동 조절)·2차 모델 auto·2차 생각 줄이기 / 3: 0.4.4 쪽 끝 확인 목록 없음


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
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=1), encoding="utf-8")


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
    p = Path(pdf)
    return p.with_name(p.stem + "_YeonE_OCR")


def open_path(path: str | Path) -> None:
    path = str(path)
    if sys.platform.startswith("win"):
        os.startfile(path)                      # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path])


class QueueWriter(io.TextIOBase):
    """작업 스레드의 출력을 창으로 보낸다. '오류:' 로 시작하는 줄은 따로 모아 끝난 뒤 알림창에 보여 준다."""
    def __init__(self, q: queue.Queue):
        self.q = q; self.problems: list[str] = []; self._buf = ""   # (errors 는 TextIOBase 의 예약 속성이라 못 씀)
    def write(self, s):
        if s:
            self.q.put(("log", s))
            self._buf += s
            while "\n" in self._buf:
                line, self._buf = self._buf.split("\n", 1)
                if line.strip().startswith(("오류:", "주의:")):
                    self.problems.append(line.strip())
        return len(s)
    def flush(self):
        pass


class MarkDialog(tk.Toplevel):
    def __init__(self, app: "App"):
        super().__init__(app)
        self.app = app
        self.title("검수 결과 기록")
        self.resizable(False, False)
        f = ttk.Frame(self, padding=12); f.grid()
        ttk.Label(f, text="쪽 범위").grid(row=0, column=0, sticky="w")
        self.pages = ttk.Entry(f, width=16); self.pages.insert(0, app.v_pages.get()); self.pages.grid(row=0, column=1, sticky="w")
        ttk.Label(f, text="상태").grid(row=1, column=0, sticky="w")
        self.status = ttk.Combobox(f, values=STATUSES, state="readonly", width=24); self.status.current(0); self.status.grid(row=1, column=1, sticky="w")
        ttk.Label(f, text="메모").grid(row=2, column=0, sticky="w")
        self.note = ttk.Entry(f, width=40); self.note.grid(row=2, column=1, sticky="w")
        self.clear = tk.BooleanVar(value=True)
        ttk.Checkbutton(f, text="'판독 확인 필요' 표시 지우기(사람이 확인한 뒤)", variable=self.clear).grid(row=3, column=0, columnspan=2, sticky="w", pady=(6, 0))
        b = ttk.Frame(f); b.grid(row=4, column=0, columnspan=2, pady=(10, 0))
        ttk.Button(b, text="기록", command=self.ok).pack(side="left", padx=4)
        ttk.Button(b, text="닫기", command=self.destroy).pack(side="left", padx=4)
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
        f = ttk.Frame(self, padding=12); f.grid()
        cur = app.v_model.get().strip(); probe = probe or {}
        head = f"키 확인됨. 모델 {len(models)}개 중 고르십시오."
        if cur:
            head += f"\n지금 '{cur}': " + ("호출 확인됨" if probe.get(cur) == "호출 확인됨" else ("목록에 있지만 호출 거부됨 — " + probe.get(cur, "") if cur in models else "목록에 없음"))
        if working and working != cur:
            head += f"\n→ 호출 확인된 모델 '{working}' 을 미리 골라 두었습니다."
        ttk.Label(f, text=head, justify="left", wraplength=560).grid(row=0, column=0, columnspan=2, sticky="w")
        ordered = [(m + ("   ✓ 호출 확인" if probe.get(m) == "호출 확인됨" else ("   ✗ 거부" if m in probe else ""))) for m in models]
        self._names = list(models)
        self.cb = ttk.Combobox(f, values=ordered, width=56, state="readonly")
        pick = working if working in models else (cur if cur in models else (models[0] if models else ""))
        if pick: self.cb.current(models.index(pick))
        self.cb.grid(row=1, column=0, columnspan=2, sticky="w", pady=8)
        ttk.Label(f, text="위쪽이 최신입니다. 판독 품질은 pro 계열이 낫고, flash 계열은 빠르고 쌉니다. preview·exp 는 시험판입니다.").grid(row=2, column=0, columnspan=2, sticky="w")
        b = ttk.Frame(f); b.grid(row=3, column=0, columnspan=2, pady=(10, 0))
        ttk.Button(b, text="이 모델로", command=self.ok).pack(side="left", padx=4)
        ttk.Button(b, text="닫기", command=self.destroy).pack(side="left", padx=4)
        self.transient(app); self.grab_set()

    def ok(self):
        i = self.cb.current()
        if i >= 0:
            name = self._names[i]
            self.app.v_model.set(name); self.app.save_settings(quiet=True)
            self.app.v_status.set(f"모델 '{name}' 로 설정됨")
        self.destroy()


class EstimateDialog(tk.Toplevel):
    """판독 전 호출·토큰·비용 어림. 단가와 환율을 넣어 저장하면 금액도 보여 준다. go=True 면 [계속] 단추로 실행을 잇는다."""
    def __init__(self, app: "App", text: str, go: bool = False):
        super().__init__(app)
        self.app = app; self.proceed = False
        self.title("비용 미리 보기"); self.resizable(False, False)
        f = ttk.Frame(self, padding=12); f.grid()
        self.lbl = ttk.Label(f, text=text, justify="left", wraplength=620); self.lbl.grid(row=0, column=0, columnspan=6, sticky="w")
        from .estimate import resolve_price, PRICES_DATE
        model = app.v_model.get().strip(); pr = resolve_price(model, app.cfg.get("prices") or {}) or {}
        fx, _ = app.fx_setting()
        ttk.Label(f, text=f"단가·환율은 자동으로 넣습니다(내장 요금표 {PRICES_DATE} · 환율 자동 조회). 요금이 바뀌었을 때만 아래를 고쳐 저장하십시오.",
                  foreground="#666", wraplength=620, justify="left").grid(row=1, column=0, columnspan=6, sticky="w", pady=(10, 0))
        ttk.Label(f, text=f"'{model}' 단가 (USD / 100만 토큰)  입력").grid(row=2, column=0, sticky="w", pady=(6, 0))
        self.e_in = ttk.Entry(f, width=8); self.e_in.insert(0, f"{pr['in']:g}" if pr else ""); self.e_in.grid(row=2, column=1, sticky="w", pady=(6, 0))
        ttk.Label(f, text="출력").grid(row=2, column=2, sticky="e", pady=(6, 0))
        self.e_out = ttk.Entry(f, width=8); self.e_out.insert(0, f"{pr['out']:g}" if pr else ""); self.e_out.grid(row=2, column=3, sticky="w", pady=(6, 0))
        ttk.Label(f, text="원/달러").grid(row=2, column=4, sticky="e", pady=(6, 0))
        self.e_fx = ttk.Entry(f, width=8); self.e_fx.insert(0, f"{fx:.0f}" if fx else ""); self.e_fx.grid(row=2, column=5, sticky="w", pady=(6, 0))
        b = ttk.Frame(f); b.grid(row=3, column=0, columnspan=6, pady=(10, 0))
        ttk.Button(b, text="고친 값으로 다시 계산", command=self.save).pack(side="left", padx=4)
        ttk.Button(b, text="자동값으로 되돌리기", command=self.reset).pack(side="left", padx=4)
        if go:
            ttk.Button(b, text="계속 실행", command=self.ok).pack(side="left", padx=4)
        ttk.Button(b, text="닫기" if not go else "취소", command=self.destroy).pack(side="left", padx=4)
        self.transient(app); self.grab_set()

    def save(self):
        model = self.app.v_model.get().strip()
        prices = dict(self.app.cfg.get("prices") or {})
        def num(s):
            try: return float(s.strip()) if s.strip() else 0.0
            except ValueError: return 0.0
        prices[model] = {"in": num(self.e_in.get()), "out": num(self.e_out.get())}
        self.app.cfg["prices"] = prices; self.app.cfg["fx_krw"] = self.e_fx.get().strip(); save_config(self.app.cfg)
        self.lbl.configure(text=self.app.estimate_text())

    def reset(self):
        model = self.app.v_model.get().strip()
        prices = dict(self.app.cfg.get("prices") or {}); prices.pop(model, None)
        self.app.cfg["prices"] = prices; self.app.cfg["fx_krw"] = ""; save_config(self.app.cfg)
        from .estimate import resolve_price
        pr = resolve_price(model, {}) or {}; fx, _ = self.app.fx_setting()
        for e, v in ((self.e_in, f"{pr['in']:g}" if pr else ""), (self.e_out, f"{pr['out']:g}" if pr else ""), (self.e_fx, f"{fx:.0f}" if fx else "")):
            e.delete(0, "end"); e.insert(0, v)
        self.lbl.configure(text=self.app.estimate_text())

    def ok(self):
        self.proceed = True; self.destroy()


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f"{APP_NAME} · 연어OCR {__version__}")
        self.geometry("1140x880"); self.minsize(980, 720)
        self.cfg = load_config()
        self._style()
        self.q: queue.Queue = queue.Queue()
        self.running = False
        self.open_after: Path | None = None
        self.buttons: list[ttk.Button] = []
        self._build()
        self.after(100, self._poll)
        self.after(400, self.check_packages)
        self._fx: tuple[float, str] | None = None
        threading.Thread(target=self._fetch_fx, daemon=True).start()
        if self.cfg.get("last_pdf") and Path(self.cfg["last_pdf"]).exists():
            self.v_pdf.set(self.cfg["last_pdf"]); self.refresh_table()
        self.protocol("WM_DELETE_WINDOW", self.on_close)

    # ---------- 화면 ----------
    def _style(self):
        """차분한 종이색 바탕에 옻칠색 머리띠. clam 테마 위에 색·여백·글꼴을 통일한다."""
        st = ttk.Style(self)
        try:
            st.theme_use("clam")
        except tk.TclError:
            pass
        base = "맑은 고딕" if sys.platform.startswith("win") else ("Apple SD Gothic Neo" if sys.platform == "darwin" else "Noto Sans CJK KR")
        self.font_base, self.font_small, self.font_h = (base, 10), (base, 9), (base, 10, "bold")
        self.font_title, self.font_sub = (base, 17, "bold"), (base, 10)
        self.font_mono = ("Consolas" if sys.platform.startswith("win") else "DejaVu Sans Mono", 10)
        self.option_add("*Font", self.font_base)
        C = self.colors = dict(BG="#f3efe7", PANEL="#fbf9f4", LINE="#d9d2c3", INK="#2a2420", MUTED="#7a7066",
                               ACC="#3b1b14", ACC_HI="#55281e", GOLD="#b99552", RED="#b3261e", FIELD="#ffffff")
        self.configure(bg=C["BG"])
        st.configure(".", background=C["BG"], foreground=C["INK"], font=self.font_base, bordercolor=C["LINE"])
        st.configure("TFrame", background=C["BG"]); st.configure("Panel.TFrame", background=C["PANEL"])
        st.configure("TLabel", background=C["BG"]); st.configure("Panel.TLabel", background=C["PANEL"])
        st.configure("Hint.TLabel", background=C["PANEL"], foreground=C["MUTED"], font=self.font_small)
        st.configure("Status.TLabel", background=C["BG"], foreground=C["ACC"], font=self.font_h)
        st.configure("TLabelframe", background=C["PANEL"], bordercolor=C["LINE"], relief="solid", borderwidth=1, padding=10)
        st.configure("TLabelframe.Label", background=C["PANEL"], foreground=C["ACC"], font=self.font_h)
        st.configure("TButton", padding=(10, 5), background="#efeae0", lightcolor="#f6f3ec", darkcolor="#e2dccd", focuscolor=C["PANEL"])
        st.map("TButton", background=[("active", "#e6dfd2"), ("disabled", "#f0ece4")], foreground=[("disabled", "#a39a90")])
        st.configure("Accent.TButton", background=C["ACC"], foreground="#fff7ea", font=self.font_h, padding=(18, 9),
                     lightcolor=C["ACC"], darkcolor=C["ACC"], focuscolor=C["ACC"])
        st.map("Accent.TButton", background=[("active", C["ACC_HI"]), ("disabled", "#b8a79d")], foreground=[("disabled", "#f3eee6")])
        st.configure("Stop.TButton", foreground=C["RED"], padding=(12, 9))
        st.configure("TCheckbutton", background=C["PANEL"]); st.configure("TRadiobutton", background=C["PANEL"])
        st.configure("TEntry", fieldbackground=C["FIELD"], padding=3); st.configure("TSpinbox", fieldbackground=C["FIELD"], padding=3)
        st.configure("TNotebook", background=C["BG"], tabmargins=(2, 4, 2, 0))
        st.configure("TNotebook.Tab", padding=(14, 6), background="#e9e3d6")
        st.map("TNotebook.Tab", background=[("selected", C["PANEL"])], foreground=[("selected", C["ACC"])])
        st.configure("Treeview", background=C["FIELD"], fieldbackground=C["FIELD"], rowheight=24)
        st.configure("Treeview.Heading", background="#efeae0", font=self.font_h, relief="flat")
        st.configure("Horizontal.TProgressbar", troughcolor="#e8e2d6", background=C["GOLD"], lightcolor=C["GOLD"], darkcolor=C["GOLD"])

    def _build(self):
        C = self.colors
        # ---- 머리띠 ----
        hd = tk.Frame(self, bg=C["ACC"], padx=18, pady=10); hd.pack(fill="x")
        tk.Label(hd, text=APP_NAME, font=self.font_title, bg=C["ACC"], fg="#f1e7cf").pack(side="left")
        tk.Label(hd, text="  연어OCR · 세로쓰기 한자·한글 문헌을 지침대로 판독해 HWPX로", font=self.font_sub, bg=C["ACC"], fg="#d9c9a8").pack(side="left", pady=(6, 0))
        self.v_pkg = tk.StringVar(value="패키지 점검 중…")
        tk.Label(hd, textvariable=self.v_pkg, font=self.font_small, bg=C["ACC"], fg="#e8c99a").pack(side="right", pady=(6, 0))
        tk.Label(hd, text=f"v{__version__}   ", font=self.font_small, bg=C["ACC"], fg="#bfae8e").pack(side="right", pady=(6, 0))

        root = ttk.Frame(self, padding=(14, 10, 14, 10)); root.pack(fill="both", expand=True)
        pad = {"padx": 4, "pady": 3}

        # ---- 파일 ----
        ff = ttk.LabelFrame(root, text="파일"); ff.pack(fill="x")
        ff.columnconfigure(1, weight=1)
        ttk.Label(ff, text="PDF", style="Panel.TLabel").grid(row=0, column=0, sticky="w", **pad)
        self.v_pdf = tk.StringVar()
        ttk.Entry(ff, textvariable=self.v_pdf, state="readonly").grid(row=0, column=1, columnspan=6, sticky="ew", **pad)
        self._btn(ff, "PDF 고르기…", self.choose_pdf, width=12).grid(row=0, column=7, sticky="e", **pad)
        self.v_pages = tk.StringVar(value=self.cfg["pages"]); self.v_off = tk.StringVar(value=self.cfg["print_offset"])
        ttk.Label(ff, text="쪽 범위", style="Panel.TLabel").grid(row=1, column=0, sticky="w", **pad)
        pf = ttk.Frame(ff, style="Panel.TFrame"); pf.grid(row=1, column=1, columnspan=3, sticky="w")
        ttk.Entry(pf, textvariable=self.v_pages, width=12).pack(side="left", padx=(4, 2))
        self._btn(pf, "전체", lambda: self.v_pages.set(""), width=5).pack(side="left")
        ttk.Label(pf, text="비우면 PDF 전체(기본). 일부만 할 때 예 1-5, 8", style="Hint.TLabel").pack(side="left", padx=8)
        ttk.Label(ff, text="인쇄 쪽수 = PDF 쪽수 +", style="Panel.TLabel").grid(row=1, column=4, sticky="e", **pad)
        ttk.Entry(ff, textvariable=self.v_off, width=6).grid(row=1, column=5, sticky="w", **pad)
        ttk.Label(ff, text="예: PDF 1쪽이 인쇄 30쪽이면 29. 어긋남만 표시", style="Hint.TLabel").grid(row=1, column=6, columnspan=2, sticky="w", **pad)

        # ---- 판독 / 문서 ----
        mid = ttk.Frame(root); mid.pack(fill="x", pady=(8, 0)); mid.columnconfigure(0, weight=3); mid.columnconfigure(1, weight=2)
        rf = ttk.LabelFrame(mid, text="판독"); rf.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        df = ttk.LabelFrame(mid, text="문서"); df.grid(row=0, column=1, sticky="nsew")
        self.v_up = tk.StringVar(value=self.cfg["upscale"]); self.v_reader = tk.StringVar(value=self.cfg["reader"])
        self.v_model = tk.StringVar(value=self.cfg["model"]); self.v_key = tk.StringVar(value=self.cfg["api_key"])
        self.v_second = tk.BooleanVar(value=self.cfg["second_pass"]); self.v_docx = tk.BooleanVar(value=self.cfg["docx"])
        self.v_join = tk.BooleanVar(value=self.cfg["join_pages"]); self.v_force = tk.BooleanVar(value=self.cfg.get("force", False))
        self.v_hl = tk.BooleanVar(value=self.cfg.get("highlight", True)); self.v_pb = tk.BooleanVar(value=self.cfg.get("page_breaks", True))
        self.v_chk = tk.StringVar(value=CHECK_LIST_LABELS.get(self.cfg.get("check_list", "none"), CHECK_LIST_LABELS["none"]))
        self.v_workers = tk.StringVar(value=str(self.cfg.get("workers", 3))); self.v_model2 = tk.StringVar(value=self.cfg.get("second_model", ""))
        self.v_ckpt = tk.StringVar(value=str(self.cfg.get("checkpoint", 20))); self.v_cache = tk.BooleanVar(value=self.cfg.get("use_cache", True))
        self.v_think2 = tk.BooleanVar(value=self.cfg.get("second_thinking_low", True))
        rf.columnconfigure(1, weight=1)
        r = 0
        ttk.Label(rf, text="판독기", style="Panel.TLabel").grid(row=r, column=0, sticky="w", **pad)
        rr = ttk.Frame(rf, style="Panel.TFrame"); rr.grid(row=r, column=1, columnspan=3, sticky="w")
        ttk.Radiobutton(rr, text="Gemini (원본 이미지 판독)", variable=self.v_reader, value="gemini", command=self._reader_changed).pack(side="left", padx=(4, 14))
        ttk.Radiobutton(rr, text="기존 텍스트 레이어만 (API 없이 시험)", variable=self.v_reader, value="mock", command=self._reader_changed).pack(side="left")
        r += 1
        ttk.Label(rf, text="모델", style="Panel.TLabel").grid(row=r, column=0, sticky="w", **pad)
        self.e_model = ttk.Entry(rf, textvariable=self.v_model, width=26); self.e_model.grid(row=r, column=1, sticky="w", **pad)
        self._btn(rf, "키 확인·모델 고르기", self.check_key, width=17).grid(row=r, column=2, sticky="w", **pad)
        r += 1
        ttk.Label(rf, text="2차 판독 모델", style="Panel.TLabel").grid(row=r, column=0, sticky="w", **pad)
        self.e_model2 = ttk.Entry(rf, textvariable=self.v_model2, width=26); self.e_model2.grid(row=r, column=1, sticky="w", **pad)
        ttk.Label(rf, text="auto = 1차와 같은 세대의 flash 자동 선택. 비우면 1차와 같음", style="Hint.TLabel").grid(row=r, column=2, columnspan=2, sticky="w", **pad)
        r += 1
        ttk.Label(rf, text="Gemini API 키", style="Panel.TLabel").grid(row=r, column=0, sticky="w", **pad)
        self.e_key = ttk.Entry(rf, textvariable=self.v_key, width=40, show="•"); self.e_key.grid(row=r, column=1, columnspan=2, sticky="ew", **pad)
        self._btn(rf, "보기", self.toggle_key, width=5).grid(row=r, column=3, sticky="w", **pad)
        r += 1
        ttk.Label(rf, text="속도", style="Panel.TLabel").grid(row=r, column=0, sticky="w", **pad)
        sp = ttk.Frame(rf, style="Panel.TFrame"); sp.grid(row=r, column=1, columnspan=3, sticky="w")
        ttk.Label(sp, text="동시 최대", style="Panel.TLabel").pack(side="left", padx=(4, 2))
        ttk.Spinbox(sp, from_=1, to=8, width=3, textvariable=self.v_workers).pack(side="left")
        ttk.Label(sp, text="쪽(자동 조절) ·  확대", style="Panel.TLabel").pack(side="left", padx=(8, 2))
        ttk.Entry(sp, textvariable=self.v_up, width=4).pack(side="left")
        ttk.Label(sp, text="배 ·  중간 저장", style="Panel.TLabel").pack(side="left", padx=(8, 2))
        ttk.Entry(sp, textvariable=self.v_ckpt, width=4).pack(side="left")
        ttk.Label(sp, text="쪽마다", style="Panel.TLabel").pack(side="left", padx=(2, 0))
        r += 1
        ck = ttk.Frame(rf, style="Panel.TFrame"); ck.grid(row=r, column=0, columnspan=4, sticky="w")
        ttk.Checkbutton(ck, text="영역 확대 2차 독립 판독·비교", variable=self.v_second).pack(side="left", padx=(4, 10))
        ttk.Checkbutton(ck, text="지침 캐시", variable=self.v_cache).pack(side="left", padx=(0, 10))
        ttk.Checkbutton(ck, text="2차 생각 줄이기", variable=self.v_think2).pack(side="left", padx=(0, 10))
        ttk.Checkbutton(ck, text="기록 있는 쪽도 다시 판독", variable=self.v_force).pack(side="left")
        # 문서
        ttk.Checkbutton(df, text="의심 자리 붉은 글자 · 노란 형광펜", variable=self.v_hl).grid(row=0, column=0, sticky="w", **pad)
        ttk.Checkbutton(df, text="원본 쪽마다 쪽 나누기", variable=self.v_pb).grid(row=1, column=0, sticky="w", **pad)
        ttk.Checkbutton(df, text="앞 쪽에서 이어지는 문단 잇기", variable=self.v_join).grid(row=2, column=0, sticky="w", **pad)
        ttk.Checkbutton(df, text="DOCX 도 함께 만들기", variable=self.v_docx).grid(row=3, column=0, sticky="w", **pad)
        cl = ttk.Frame(df, style="Panel.TFrame"); cl.grid(row=4, column=0, sticky="w", **pad)
        ttk.Label(cl, text="쪽 끝 확인 목록").pack(side="left", padx=(0, 6))
        ttk.Combobox(cl, textvariable=self.v_chk, values=list(CHECK_LIST_LABELS.values()), state="readonly", width=22).pack(side="left")
        ttk.Label(df, text="가로쓰기 정리본 · 함초롬바탕 · 도판은 원본 이미지", style="Hint.TLabel").grid(row=5, column=0, sticky="w", **pad)
        self._btn(df, "설정 저장", self.save_settings, width=10).grid(row=6, column=0, sticky="e", pady=(6, 0))

        # ---- 실행 ----
        act = ttk.Frame(root, style="Panel.TFrame", padding=10); act.pack(fill="x", pady=(8, 0))
        self._btn(act, "▶  PDF 전체를 HWPX로", self.do_run, style="Accent.TButton").pack(side="left")
        self.b_stop = ttk.Button(act, text="■ 중지", command=self.do_stop, state="disabled", style="Stop.TButton"); self.b_stop.pack(side="left", padx=(8, 0))
        for t, c in (("HWPX 열기", self.open_hwpx), ("결과 폴더", self.open_work), ("비용 미리 보기", self.do_estimate)):
            self._btn(act, t, c, width=13).pack(side="right", padx=(6, 0))

        # ---- 단계별 / 도구 ----
        sf = ttk.LabelFrame(root, text="단계별 · 도구"); sf.pack(fill="x", pady=(8, 0))
        steps = [("① 구조 보기", self.do_inspect), ("② 준비", self.do_prepare), ("③ 판독", self.do_read),
                 ("④ 문서 만들기", self.do_build), ("⑤ 검수 화면", self.do_review), ("⑥ 보고", self.do_report)]
        for i, (t, c) in enumerate(steps):
            self._btn(sf, t, c, width=12).grid(row=0, column=i, padx=3, pady=2)
        for i, (t, c) in enumerate([("검수 결과 기록…", self.do_mark), ("비교 다시 계산", self.do_remerge), ("패키지 설치·점검", self.install_packages), ("도움말", self.show_help)]):
            self._btn(sf, t, c, width=12).grid(row=1, column=i, padx=3, pady=(6, 2))
        ttk.Label(sf, text="검수 뒤 사람이 확인한 쪽은 [검수 결과 기록…] · 프로그램을 새 판으로 바꾼 뒤에는 [비교 다시 계산](모델 호출 없음)",
                  style="Hint.TLabel", wraplength=900).grid(row=2, column=0, columnspan=6, sticky="w", padx=6, pady=(4, 0))

        # ---- 진행 ----
        pf2 = ttk.Frame(root); pf2.pack(fill="x", pady=(10, 0))
        self.progress = ttk.Progressbar(pf2, mode="indeterminate", length=260); self.progress.pack(side="left")
        self.v_status = tk.StringVar(value="준비됨"); ttk.Label(pf2, textvariable=self.v_status, style="Status.TLabel").pack(side="left", padx=12)

        # ---- 기록 / 쪽별 상태 ----
        nb = ttk.Notebook(root); nb.pack(fill="both", expand=True, pady=(8, 0))
        self.log = ScrolledText(nb, height=14, wrap="word", state="disabled", font=self.font_mono, bg="#fffdf8", fg=C["INK"],
                                relief="flat", padx=10, pady=8, insertbackground=C["INK"])
        nb.add(self.log, text="진행 기록")
        tf = ttk.Frame(nb); nb.add(tf, text="쪽별 상태")
        self.table = ttk.Treeview(tf, columns=("pdf", "print", "status", "unc", "figs"), show="headings")
        for c, t, w in (("pdf", "PDF 쪽", 70), ("print", "인쇄 쪽", 70), ("status", "상태", 560), ("unc", "확인 필요", 80), ("figs", "도판", 60)):
            self.table.heading(c, text=t); self.table.column(c, width=w, anchor="center" if c != "status" else "w")
        sb = ttk.Scrollbar(tf, orient="vertical", command=self.table.yview); self.table.configure(yscrollcommand=sb.set)
        self.table.pack(side="left", fill="both", expand=True); sb.pack(side="right", fill="y")
        self.nb = nb
        self._reader_changed()

    def _btn(self, parent, text, cmd, **kw) -> ttk.Button:
        b = ttk.Button(parent, text=text, command=cmd, **kw); self.buttons.append(b); return b

    # ---------- 도우미 ----------
    def pdf(self) -> str:
        return self.v_pdf.get().strip()

    def work(self) -> Path:
        return work_dir_for(self.pdf())

    def _need_pdf(self) -> bool:
        if not self.pdf() or not Path(self.pdf()).exists():
            messagebox.showinfo(APP_NAME, "먼저 [PDF 고르기…] 로 PDF 파일을 고르십시오."); return False
        return True

    def _pages(self) -> list[str]:
        p = self.v_pages.get().strip()
        return ["--pages", p] if p else []

    def _read_opts(self) -> list[str]:
        o = ["--reader", self.v_reader.get(), "--upscale", self.v_up.get().strip() or "2"]
        if self.v_reader.get() == "gemini" and self.v_model.get().strip():
            o += ["--model", self.v_model.get().strip()]
        if not self.v_second.get():
            o.append("--no-second-pass")
        if self.v_off.get().strip():
            o += ["--print-offset", self.v_off.get().strip()]
        if self.v_force.get():
            o.append("--force")
        o += ["--workers", (self.v_workers.get().strip() or "6")]
        o += ["--second-model", self.v_model2.get().strip()]          # 빈 값이면 1차와 같은 모델, auto 면 자동
        o += ["--second-thinking", "low" if self.v_think2.get() else "full"]
        if not self.v_cache.get():
            o.append("--no-cache")
        o += ["--checkpoint", (self.v_ckpt.get().strip() or "0")]
        return o

    def _build_opts(self) -> list[str]:
        o = []
        if self.v_docx.get(): o.append("--docx")
        if not self.v_join.get(): o.append("--no-join-pages")
        if not self.v_hl.get(): o.append("--no-highlight")
        if not self.v_pb.get(): o.append("--no-page-breaks")
        o += ["--check-list", self.check_list_mode()]
        return o

    def check_list_mode(self) -> str:
        return next((k for k, v in CHECK_LIST_LABELS.items() if v == self.v_chk.get()), "none")

    def _prepare_env(self) -> bool:
        if self.v_reader.get() == "gemini":
            from .reader_gemini import normalize_key, key_shape_warning
            key = normalize_key(self.v_key.get())
            if key != self.v_key.get():
                self.v_key.set(key)                  # 공백·따옴표 등을 걷어 낸 값으로 바꿔 보여 준다
            if not key:
                messagebox.showwarning(APP_NAME, "Gemini API 키가 없습니다. 설정에 키를 넣거나 판독기를 '기존 텍스트 레이어만' 으로 바꾸십시오."); return False
            warn = key_shape_warning(key)
            if warn and not messagebox.askyesno(APP_NAME, warn + "\n\n그래도 이 키로 진행할까요?"):
                return False
            os.environ["GEMINI_API_KEY"] = key
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
        self.log.see("end"); self.log.configure(state="disabled")

    def _set_running(self, on: bool):
        self.running = on
        for b in self.buttons:
            b.configure(state="disabled" if on else "normal")
        self.b_stop.configure(state="normal" if on else "disabled")
        if on: self.progress.start(12)
        else: self.progress.stop()

    def start(self, argv: list[str], label: str, open_after: Path | None = None):
        if self.running:
            return
        self.open_after = open_after
        self._set_running(True); self.v_status.set(label + " 중…")
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
                    rc = cli.main(argv)
            except SystemExit as e:
                rc = e.code if isinstance(e.code, int) else 1
                if e.code and not isinstance(e.code, int): self.q.put(("log", str(e.code) + "\n"))
            except ModuleNotFoundError as e:
                self.q.put(("log", f"\n오류: 패키지가 없습니다: {e.name}. [필요한 패키지 설치·점검] 단추를 누르십시오.\n"))
                if w: w.problems.append(f"오류: 패키지가 없습니다: {e.name}")
            except Exception:
                self.q.put(("log", traceback.format_exc()))
            finally:
                self.q.put(("done", rc, label))          # 어떤 경우에도 창에 끝을 알린다
        threading.Thread(target=target, daemon=True).start()

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
        except queue.Empty:
            pass
        self.after(100, self._poll)

    def _finish(self, rc, label):
        self._set_running(False)
        ok = (rc == 0 or rc is None)
        self.v_status.set(f"{label} {'끝' if ok else ('중지됨' if rc == 130 else '오류 (진행 기록 확인)')}")
        self._append(f"— {label} {'끝' if ok else '중단'} (rc={rc})\n")
        self.refresh_table()
        w = getattr(self, "_last_writer", None)
        if not ok and rc != 130 and w is not None and w.problems:
            messagebox.showerror(APP_NAME, f"{label} 이(가) 중단되었습니다.\n\n" + "\n".join(w.problems[-3:]))
        self._last_writer = None
        if ok and self.open_after and Path(self.open_after).exists():
            open_path(self.open_after)
        elif ok and ("HWPX로" in label):
            if messagebox.askyesno(APP_NAME, "끝났습니다. 검수 화면(review.html)을 열까요?"):
                self.do_review()
        self.open_after = None

    # ---------- 단추 동작 ----------
    def choose_pdf(self):
        p = filedialog.askopenfilename(title="PDF 고르기", filetypes=[("PDF", "*.pdf"), ("모든 파일", "*.*")])
        if p:
            self.v_pdf.set(p); self.cfg["last_pdf"] = p; save_config(self.cfg); self.refresh_table()
            self._append(f"PDF: {p}\n결과 폴더: {self.work()}\n")

    def toggle_key(self):
        self.e_key.configure(show="" if self.e_key.cget("show") else "•")

    def check_key(self):
        """키가 쓸 수 있는지 Gemini 에 물어보고, 쓸 수 있는 모델 목록에서 고르게 한다."""
        if self.running: return
        from .reader_gemini import normalize_key
        key = normalize_key(self.v_key.get()); self.v_key.set(key)
        model = self.v_model.get().strip()
        self._set_running(True); self.v_status.set("키 확인 중… (모델을 실제로 짧게 호출해 봅니다)")
        def t():
            try:
                from .reader_gemini import check_key
                res = check_key(key, model)
            except Exception as e:
                res = {"ok": False, "message": f"확인 실패: {e}", "models": [], "shape_warning": ""}
            self.q.put(("keycheck", res))
        threading.Thread(target=t, daemon=True).start()

    def _keycheck_result(self, res: dict):
        self._set_running(False)
        self.v_status.set(res["message"][:90])
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
                         "workers": int(self.v_workers.get() or 6), "second_model": self.v_model2.get().strip(),
                         "checkpoint": int(self.v_ckpt.get() or 0), "use_cache": self.v_cache.get(), "second_thinking_low": self.v_think2.get(), "last_pdf": self.pdf()})
        save_config(self.cfg)
        if not quiet:
            self.v_status.set(f"설정 저장됨 → {CONFIG_PATH}")

    def _fetch_fx(self):
        try:
            from .estimate import fetch_fx_krw
            self._fx = fetch_fx_krw()
        except Exception:
            self._fx = None

    def fx_setting(self) -> tuple[float | None, str]:
        """환율: 직접 입력값 > 자동 조회값 > (없음 → estimate 가 기본값을 씀)."""
        fx = str(self.cfg.get("fx_krw") or "").strip()
        if fx:
            try: return float(fx), "직접 입력"
            except ValueError: pass
        if self._fx:
            return self._fx
        return None, ""

    def estimate_text(self) -> str:
        """현재 설정으로 호출·토큰·비용 어림 글. 단가는 내장 요금표, 환율은 자동 조회라 따로 넣을 것이 없다."""
        try:
            from .estimate import estimate, format_estimate
            from .pdfio import open_pdf, parse_pages
            from .state import Work
            doc = open_pdf(self.pdf()); pages = parse_pages(self.v_pages.get().strip() or None, len(doc))
            w = Work(self.pdf(), str(self.work()))
            fx, fx_src = self.fx_setting()
            e = estimate(self.pdf(), pages, upscale=float(self.v_up.get() or 2), second_pass=self.v_second.get(), model=self.v_model.get().strip(),
                         second_model=self.v_model2.get().strip(), use_cache=self.v_cache.get(),
                         prices=self.cfg.get("prices") or {}, fx_krw=fx, fx_source=fx_src, records=[] if self.v_force.get() else w.records(pages))
            return format_estimate(e)
        except ModuleNotFoundError as e:
            return f"패키지가 없어 계산하지 못했습니다: {e.name}. [필요한 패키지 설치·점검] 을 누르십시오."
        except Exception as e:
            return f"계산 실패: {e}"

    def do_estimate(self):
        if self._need_pdf(): EstimateDialog(self, self.estimate_text())

    def _confirm_cost(self) -> bool:
        """Gemini 판독 전에 어림을 보여 주고 계속할지 묻는다. 읽을 쪽이 없으면 묻지 않는다."""
        if self.v_reader.get() != "gemini":
            return True
        text = self.estimate_text()
        if "예상 호출 0회" in text:
            return True
        d = EstimateDialog(self, text, go=True); self.wait_window(d)
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
        if not self._need_pdf(): return
        rv = self.work() / "review.html"
        self.start(["review", self.pdf(), "--work", str(self.work())] + self._pages(), "⑤ 검수 화면", open_after=rv)

    def do_report(self):
        if self._need_pdf():
            self.nb.select(0)
            self.start(["report", self.pdf(), "--work", str(self.work())], "⑥ 보고")

    def do_run(self):
        if self._need_pdf() and self._prepare_env() and self._confirm_cost():
            self.nb.select(0)
            self.start(["run", self.pdf(), "--work", str(self.work())] + self._pages() + self._read_opts() + self._build_opts(), "PDF 전체를 HWPX로" if not self.v_pages.get().strip() else f"{self.v_pages.get().strip()}쪽을 HWPX로")

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
        if not self._need_pdf(): return
        if not messagebox.askyesno(APP_NAME, "저장된 1차·2차 판독으로 비교와 확인 표시를 다시 계산합니다(모델 호출 없음).\n"
                                             "readings/pNNN.json 의 merged 를 손으로 고친 내용은 사라집니다. 계속할까요?"):
            return
        self.nb.select(0)
        self.start(["remerge", self.pdf(), "--work", str(self.work())] + self._pages(), "비교 다시 계산")

    def open_work(self):
        if not self._need_pdf(): return
        w = self.work()
        if not w.exists():
            messagebox.showinfo(APP_NAME, "아직 결과 폴더가 없습니다. 먼저 [② 준비] 또는 [▶ 한 번에 실행] 을 누르십시오."); return
        open_path(w)

    def open_hwpx(self):
        if not self._need_pdf(): return
        files = sorted((self.work() / "out").glob("*.hwpx"), key=lambda p: p.stat().st_mtime) if (self.work() / "out").exists() else []
        if not files:
            messagebox.showinfo(APP_NAME, "아직 HWPX 가 없습니다. [④ 문서 만들기] 를 먼저 누르십시오."); return
        open_path(files[-1])

    def show_help(self):
        messagebox.showinfo(f"{APP_NAME} 도움말", HELP)

    # ---------- 패키지 ----------
    def check_packages(self):
        here = Path(__file__).resolve().parent.parent
        self.q.put(("log", f"{APP_NAME} v{__version__} · 프로그램 위치: {here} · Python {sys.version.split()[0]}\n"
                           f"(다른 판이 뜨면 이 위치의 YeonE_OCR.pyw 를 실행하는지 확인하십시오)\n"))
        def t():
            self.q.put(("pkg", missing_packages()))
        threading.Thread(target=t, daemon=True).start()

    def _pkg_result(self, missing: list[str]):
        if missing:
            self.v_pkg.set("빠진 패키지: " + ", ".join(missing) + "  → [필요한 패키지 설치·점검]")
        else:
            self.v_pkg.set("패키지 모두 있음")

    def install_packages(self):
        if self.running: return
        req = Path(__file__).resolve().parent.parent / "requirements.txt"
        cmd = [sys.executable, "-m", "pip", "install", "--upgrade"] + (["-r", str(req)] if req.exists() else [p for _, p in PACKAGES])
        self._set_running(True); self.v_status.set("패키지 설치 중… (몇 분 걸릴 수 있음)"); self.nb.select(0)
        self.q.put(("log", "\n▶ 패키지 설치\n   " + " ".join(cmd) + "\n"))
        def t():
            rc = 1
            try:
                p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
                for line in p.stdout:
                    self.q.put(("log", line))
                rc = p.wait()
            except Exception:
                self.q.put(("log", traceback.format_exc()))
            self.q.put(("pkg", missing_packages()))
            self.q.put(("done", rc, "패키지 설치"))
        threading.Thread(target=t, daemon=True).start()

    # ---------- 쪽별 상태 ----------
    def refresh_table(self):
        for i in self.table.get_children():
            self.table.delete(i)
        if not self.pdf():
            return
        w = self.work(); sp = w / "state.json"
        if not sp.exists():
            return
        try:
            st = json.loads(sp.read_text(encoding="utf-8"))
        except Exception:
            return
        total = st.get("total_pages") or (max((int(k) for k in st.get("pages", {})), default=0))
        for n in range(1, total + 1):
            p = st.get("pages", {}).get(str(n), {})
            statuses = " · ".join(p.get("statuses", ["미처리"])) or "미처리"
            pr, unc, figs = "", "", ""
            rp = w / "readings" / f"p{n:03d}.json"
            if rp.exists():
                try:
                    rec = json.loads(rp.read_text(encoding="utf-8")); m = rec["merged"]
                    pr = m.get("print_page", ""); unc = len(m.get("uncertain", []))
                    figs = sum(1 for r in m["regions"] if r["kind"] in ("원형나경도", "산도", "계통도", "도식", "삽화", "복잡한표"))
                except Exception:
                    pass
            self.table.insert("", "end", values=(n, pr, statuses, unc, figs))

    def on_close(self):
        if self.running and not messagebox.askyesno(APP_NAME, "작업이 진행 중입니다. 그래도 닫을까요?"):
            return
        self.save_settings(quiet=True)
        self.destroy()


def main():
    app = App()
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
