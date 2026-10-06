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
DEFAULTS = {"pages": "1-5", "print_offset": "", "upscale": "2", "reader": "gemini", "model": "gemini-2.5-pro",
            "api_key": "", "second_pass": True, "docx": False, "join_pages": True, "last_pdf": ""}
STATUSES = ["원본 대조 완료(사람)", "판독 확인 필요", "이미지 보존 확인 완료", "문서 반영 완료", "파일 구조 검사 완료"]

HELP = f"""{APP_NAME} · 연어OCR {__version__}

세로쓰기 한자·한글 혼용 문헌(스캔 PDF)을 지침에 따라 OCR·교정해 HWPX 로 만듭니다.
글자는 Gemini 가 읽고, 프로그램은 영역 잘라내기·두 판독 비교·문서 조립·상태 기록을 맡습니다.

차례
 1. [필요한 패키지 설치·점검] — 처음 한 번.
 2. API 키를 넣고 [키 확인·모델 고르기] — 키가 맞는지 확인하고 쓸 수 있는 모델을 고릅니다. (https://aistudio.google.com/apikey)
 3. [PDF 고르기…] 로 파일을 고르고 쪽 범위(예 1-5)와 인쇄 쪽수 차이를 적습니다.
 4. [▶ 한 번에 실행] 을 누르면 준비 → 판독 → 문서 만들기 → 검수 화면 → 보고 까지 갑니다.
 5. [⑤ 검수 화면 열기] 에서 원본과 대조하고, 글자를 고치려면 결과 폴더의 readings/pNNN.json 을 고친 뒤 [④ 문서 만들기] 를 다시 누릅니다.
 6. 사람이 대조를 마친 쪽은 [검수 결과 기록…] 으로 "원본 대조 완료(사람)" 을 남깁니다.

결과는 PDF 와 같은 폴더의 「<PDF 이름>_YeonE_OCR」 안에 생깁니다.
 out/      HWPX (+DOCX)
 review.html  검수 화면      report.md  상태 보고
 readings/ 쪽별 판독 기록    crops/     이미지로 보존한 도판

〔 〕 표시는 확인이 필요한 자리입니다. 프로그램은 추측으로 채우지 않습니다.
"""


def load_config() -> dict:
    cfg = dict(DEFAULTS)
    try:
        cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
    except Exception:
        pass
    return cfg


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
    """키 확인 뒤 쓸 수 있는 모델 목록에서 고른다. gemini 가 든 이름을 앞에 둔다."""
    def __init__(self, app: "App", models: list[str]):
        super().__init__(app)
        self.app = app; self.title("쓸 수 있는 모델 고르기"); self.resizable(False, False)
        f = ttk.Frame(self, padding=12); f.grid()
        cur = app.v_model.get().strip()
        ttk.Label(f, text=f"키 확인됨. 모델 {len(models)}개 중 고르십시오." + (f"  (지금 '{cur}' 은 목록에 {'있음' if cur in models else '없음'})" if cur else "")).grid(row=0, column=0, columnspan=2, sticky="w")
        pref = [m for m in models if "gemini" in m and "pro" in m] + [m for m in models if "gemini" in m and "flash" in m and "pro" not in m]
        ordered = pref + [m for m in models if m not in pref]
        self.cb = ttk.Combobox(f, values=ordered, width=48, state="readonly")
        if cur in ordered: self.cb.set(cur)
        elif ordered: self.cb.current(0)
        self.cb.grid(row=1, column=0, columnspan=2, sticky="w", pady=8)
        ttk.Label(f, text="판독 품질은 pro 계열이 낫고, flash 계열은 빠르고 쌉니다.").grid(row=2, column=0, columnspan=2, sticky="w")
        b = ttk.Frame(f); b.grid(row=3, column=0, columnspan=2, pady=(10, 0))
        ttk.Button(b, text="이 모델로", command=self.ok).pack(side="left", padx=4)
        ttk.Button(b, text="닫기", command=self.destroy).pack(side="left", padx=4)
        self.transient(app); self.grab_set()

    def ok(self):
        if self.cb.get():
            self.app.v_model.set(self.cb.get()); self.app.save_settings(quiet=True)
            self.app.v_status.set(f"모델 '{self.cb.get()}' 로 설정됨")
        self.destroy()


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f"{APP_NAME} · 연어OCR {__version__}")
        self.geometry("1040x760"); self.minsize(900, 640)
        self.cfg = load_config()
        self.q: queue.Queue = queue.Queue()
        self.running = False
        self.open_after: Path | None = None
        self.buttons: list[ttk.Button] = []
        self._build()
        self.after(100, self._poll)
        self.after(400, self.check_packages)
        if self.cfg.get("last_pdf") and Path(self.cfg["last_pdf"]).exists():
            self.v_pdf.set(self.cfg["last_pdf"]); self.refresh_table()
        self.protocol("WM_DELETE_WINDOW", self.on_close)

    # ---------- 화면 ----------
    def _build(self):
        pad = {"padx": 6, "pady": 3}
        root = ttk.Frame(self, padding=8); root.pack(fill="both", expand=True)

        # PDF 줄
        fr = ttk.Frame(root); fr.pack(fill="x")
        ttk.Label(fr, text="PDF 파일").pack(side="left")
        self.v_pdf = tk.StringVar()
        ttk.Entry(fr, textvariable=self.v_pdf, state="readonly").pack(side="left", fill="x", expand=True, padx=6)
        self._btn(fr, "PDF 고르기…", self.choose_pdf).pack(side="left")

        # 설정
        st = ttk.LabelFrame(root, text="설정", padding=8); st.pack(fill="x", pady=(8, 0))
        self.v_pages = tk.StringVar(value=self.cfg["pages"]); self.v_off = tk.StringVar(value=self.cfg["print_offset"])
        self.v_up = tk.StringVar(value=self.cfg["upscale"]); self.v_reader = tk.StringVar(value=self.cfg["reader"])
        self.v_model = tk.StringVar(value=self.cfg["model"]); self.v_key = tk.StringVar(value=self.cfg["api_key"])
        self.v_second = tk.BooleanVar(value=self.cfg["second_pass"]); self.v_docx = tk.BooleanVar(value=self.cfg["docx"])
        self.v_join = tk.BooleanVar(value=self.cfg["join_pages"])
        r = 0
        ttk.Label(st, text="쪽 범위").grid(row=r, column=0, sticky="w", **pad)
        ttk.Entry(st, textvariable=self.v_pages, width=12).grid(row=r, column=1, sticky="w", **pad)
        ttk.Label(st, text="예 1-5, 8   (비우면 전체. 본문 3~5쪽, 복잡한 판면 1~3쪽씩)").grid(row=r, column=2, columnspan=3, sticky="w", **pad)
        r += 1
        ttk.Label(st, text="인쇄 쪽수 = PDF 쪽수 +").grid(row=r, column=0, sticky="w", **pad)
        ttk.Entry(st, textvariable=self.v_off, width=12).grid(row=r, column=1, sticky="w", **pad)
        ttk.Label(st, text="어긋남만 표시하고 추정으로 확정하지 않음 (예: PDF 1쪽이 인쇄 30쪽이면 29)").grid(row=r, column=2, columnspan=3, sticky="w", **pad)
        r += 1
        ttk.Label(st, text="판독기").grid(row=r, column=0, sticky="w", **pad)
        rf = ttk.Frame(st); rf.grid(row=r, column=1, columnspan=4, sticky="w")
        ttk.Radiobutton(rf, text="Gemini (원본 이미지 판독)", variable=self.v_reader, value="gemini", command=self._reader_changed).pack(side="left", padx=(6, 12))
        ttk.Radiobutton(rf, text="기존 텍스트 레이어만 (API 없이 뼈대 시험, 원본 대조 전)", variable=self.v_reader, value="mock", command=self._reader_changed).pack(side="left")
        r += 1
        ttk.Label(st, text="모델 이름").grid(row=r, column=0, sticky="w", **pad)
        self.e_model = ttk.Entry(st, textvariable=self.v_model, width=24); self.e_model.grid(row=r, column=1, columnspan=2, sticky="w", **pad)
        ttk.Label(st, text="확대 배율").grid(row=r, column=3, sticky="e", **pad)
        ttk.Entry(st, textvariable=self.v_up, width=6).grid(row=r, column=4, sticky="w", **pad)
        r += 1
        ttk.Label(st, text="Gemini API 키").grid(row=r, column=0, sticky="w", **pad)
        self.e_key = ttk.Entry(st, textvariable=self.v_key, width=48, show="•"); self.e_key.grid(row=r, column=1, columnspan=3, sticky="w", **pad)
        kb = ttk.Frame(st); kb.grid(row=r, column=4, sticky="w")
        self._btn(kb, "보기", self.toggle_key, width=5).pack(side="left", padx=(6, 2))
        self._btn(kb, "키 확인·모델 고르기", self.check_key, width=18).pack(side="left")
        r += 1
        cf = ttk.Frame(st); cf.grid(row=r, column=0, columnspan=5, sticky="w")
        ttk.Checkbutton(cf, text="영역을 확대해 2차 독립 판독·비교", variable=self.v_second).pack(side="left", padx=6)
        ttk.Checkbutton(cf, text="DOCX 도 만들기", variable=self.v_docx).pack(side="left", padx=6)
        ttk.Checkbutton(cf, text="앞 쪽에서 이어지는 문단 잇기", variable=self.v_join).pack(side="left", padx=6)
        self._btn(cf, "설정 저장", self.save_settings).pack(side="left", padx=18)

        # 차례 단추
        sf = ttk.LabelFrame(root, text="차례대로 누르기", padding=8); sf.pack(fill="x", pady=(8, 0))
        steps = [("① 구조 보기", self.do_inspect), ("② 준비", self.do_prepare), ("③ 판독", self.do_read),
                 ("④ 문서 만들기", self.do_build), ("⑤ 검수 화면 열기", self.do_review), ("⑥ 보고 보기", self.do_report)]
        for i, (t, c) in enumerate(steps):
            self._btn(sf, t, c, width=14).grid(row=0, column=i, padx=3, pady=2)
        self._btn(sf, "▶ 한 번에 실행 (②~⑥)", self.do_run, width=24).grid(row=1, column=0, columnspan=3, padx=3, pady=(6, 2), sticky="w")
        self.b_stop = ttk.Button(sf, text="■ 중지", command=self.do_stop, state="disabled", width=10); self.b_stop.grid(row=1, column=3, padx=3, pady=(6, 2))

        # 그 밖에
        ef = ttk.LabelFrame(root, text="그 밖에", padding=8); ef.pack(fill="x", pady=(8, 0))
        for i, (t, c) in enumerate([("결과 폴더 열기", self.open_work), ("HWPX 열기", self.open_hwpx), ("검수 결과 기록…", self.do_mark),
                                    ("필요한 패키지 설치·점검", self.install_packages), ("도움말", self.show_help)]):
            self._btn(ef, t, c, width=22 if i == 3 else 16).grid(row=0, column=i, padx=3, pady=2)

        # 진행 표시
        pf = ttk.Frame(root); pf.pack(fill="x", pady=(8, 0))
        self.progress = ttk.Progressbar(pf, mode="indeterminate", length=220); self.progress.pack(side="left")
        self.v_status = tk.StringVar(value="준비됨"); ttk.Label(pf, textvariable=self.v_status).pack(side="left", padx=10)
        self.v_pkg = tk.StringVar(value="패키지 점검 중…"); ttk.Label(pf, textvariable=self.v_pkg, foreground="#8a4b00").pack(side="right")

        # 기록 / 쪽별 상태
        nb = ttk.Notebook(root); nb.pack(fill="both", expand=True, pady=(8, 0))
        self.log = ScrolledText(nb, height=14, wrap="word", state="disabled"); nb.add(self.log, text="진행 기록")
        tf = ttk.Frame(nb); nb.add(tf, text="쪽별 상태")
        self.table = ttk.Treeview(tf, columns=("pdf", "print", "status", "unc", "figs"), show="headings")
        for c, t, w in (("pdf", "PDF 쪽", 70), ("print", "인쇄 쪽", 70), ("status", "상태", 520), ("unc", "확인 필요", 80), ("figs", "도판", 60)):
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
        return o

    def _build_opts(self) -> list[str]:
        o = []
        if self.v_docx.get(): o.append("--docx")
        if not self.v_join.get(): o.append("--no-join-pages")
        return o

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

    def _append(self, s: str):
        self.log.configure(state="normal"); self.log.insert("end", s); self.log.see("end"); self.log.configure(state="disabled")

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
        elif ok and label.startswith("한 번에 실행"):
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
        self._set_running(True); self.v_status.set("키 확인 중…")
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
        ModelDialog(self, res["models"])

    def _reader_changed(self):
        st = "normal" if self.v_reader.get() == "gemini" else "disabled"
        self.e_model.configure(state=st); self.e_key.configure(state=st)

    def save_settings(self, quiet=False):
        self.cfg.update({"pages": self.v_pages.get(), "print_offset": self.v_off.get(), "upscale": self.v_up.get(), "reader": self.v_reader.get(),
                         "model": self.v_model.get(), "api_key": self.v_key.get(), "second_pass": self.v_second.get(), "docx": self.v_docx.get(),
                         "join_pages": self.v_join.get(), "last_pdf": self.pdf()})
        save_config(self.cfg)
        if not quiet:
            self.v_status.set(f"설정 저장됨 → {CONFIG_PATH}")

    def do_inspect(self):
        if self._need_pdf(): self.start(["inspect", self.pdf()], "① 구조 보기")

    def do_prepare(self):
        if self._need_pdf(): self.start(["prepare", self.pdf(), "--work", str(self.work())] + self._pages(), "② 준비")

    def do_read(self):
        if self._need_pdf() and self._prepare_env():
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
        if self._need_pdf() and self._prepare_env():
            self.nb.select(0)
            self.start(["run", self.pdf(), "--work", str(self.work())] + self._pages() + self._read_opts() + self._build_opts(), "한 번에 실행")

    def do_stop(self):
        try:
            from . import cli
            cli.CANCEL.set(); self.v_status.set("중지 요청 — 지금 쪽이 끝나면 멈춥니다")
        except Exception:
            pass

    def do_mark(self):
        if self._need_pdf(): MarkDialog(self)

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
