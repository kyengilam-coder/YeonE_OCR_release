"""YeonE_OCR 창 프로그램. 모든 기능을 단추로 조작한다.

표준 라이브러리 tkinter 만 쓰므로 Python 만 있으면 뜬다. 무거운 패키지(pymupdf, hwpx, google-genai…)는
단추를 눌렀을 때 비로소 불러오며, 없으면 [필요한 패키지 설치·점검] 단추로 설치한다.
"""
from __future__ import annotations
import contextlib, io, json, os, queue, subprocess, sys, threading, traceback, webbrowser
from pathlib import Path
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, simpledialog
from tkinter.scrolledtext import ScrolledText

from . import __version__

APP_NAME = "YeonE_OCR"
CONFIG_PATH = Path.home() / ".yeone_ocr" / "config.json"
PACKAGES = [("pymupdf", "pymupdf"), ("PIL", "pillow"), ("numpy", "numpy"), ("cv2", "opencv-python-headless"),
            ("pydantic", "pydantic"), ("google.genai", "google-genai"), ("hwpx", "python-hwpx"), ("docx", "python-docx"), ("opencc", "opencc-python-reimplemented"), ("hanja", "hanja")]
DEFAULTS = {"pages": "", "print_offset": "", "upscale": "2", "reader": "gemini", "model": "gemini-3.1-pro-preview",
            "api_key": "", "second_pass": True, "docx": False, "join_pages": True, "force": False, "highlight": True, "page_breaks": True,
            "prices": {}, "fx_krw": "", "workers": 6, "second_model": "auto", "checkpoint": 20, "use_cache": True, "second_thinking_low": True, "check_list": "none", "last_pdf": "", "hwpx": "", "collate_pdf": "", "anchor_source": "auto", "heading_styles": "", "collate_policy": "all", "collate_memo": True, "collate_vertical": False,
            "chunk_lines": 8, "media_high": True}
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
 6. [3차 글자 판별] 은 1차·2차가 갈린 〔a/b〕 자리마다 그 글자 하나를 크게 잘라 모델에 자형만으로 묻습니다(자리 하나에 호출 한 번).
    획이 보이면 확정해 본문에 넣고, 보이지 않으면 표시를 그대로 둡니다. [혼동 장부] 는 어느 글자가 어느 글자로 읽혔는지 결과 폴더 전체에서 모읍니다(호출 없음).
 7. 사람이 대조를 마친 쪽은 [검수 결과 기록…] 으로 "원본 대조 완료(사람)" 을 남깁니다.

결과는 PDF 와 같은 폴더의 「<PDF 이름>_YeonE_OCR」 안에 생깁니다.
 out/      HWPX (+DOCX)
 review.html  검수 화면      report.md  상태 보고
 readings/ 쪽별 판독 기록    crops/     이미지로 보존한 도판

〔 〕 표시는 확인이 필요한 자리입니다. 프로그램은 추측으로 채우지 않습니다.
"""


SPEED_DEFAULTS_VERSION = 4  # 2: 0.4.1 동시 6(자동 조절)·2차 모델 auto·2차 생각 줄이기 / 3: 0.4.4 쪽 끝 확인 목록 없음 / 4: 0.6.0 조각 8줄 묶음·2차 조각 고해상


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
        cfg.setdefault("chunk_lines", DEFAULTS["chunk_lines"]); cfg.setdefault("media_high", DEFAULTS["media_high"])
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


class StylePicker(tk.Toplevel):
    """HWPX 스타일 목록에서 책갈피로 쓸 것을 고른다(여러 개, 고른 순서가 급)."""
    def __init__(self, app: "App", counts: list[tuple[str, int]]):
        super().__init__(app)
        self.app = app; self.title("책갈피로 쓸 스타일 고르기"); self.resizable(False, True)
        f = ttk.Frame(self, padding=12); f.grid(sticky="nsew")
        ttk.Label(f, text="문단에 쓰인 스타일입니다. 책갈피처럼 쓸 제목 스타일을 고르십시오(여러 개 가능, 고른 순서가 큰 단위 → 작은 단위).", wraplength=520, justify="left").grid(row=0, column=0, columnspan=2, sticky="w")
        self.lb = tk.Listbox(f, selectmode="multiple", height=min(18, max(6, len(counts))), width=48, exportselection=False, font=app.font_base)
        self.names = [n for n, _ in counts]
        chosen = app.heading_styles_list()
        for i, (n, c) in enumerate(counts):
            self.lb.insert("end", f"{n}   ({c:,}개 문단)")
            if (chosen and n in chosen) or (not chosen and "제목" in n):
                self.lb.selection_set(i)
        self.lb.grid(row=1, column=0, sticky="nsew", pady=(8, 0))
        sb = ttk.Scrollbar(f, orient="vertical", command=self.lb.yview); self.lb.configure(yscrollcommand=sb.set); sb.grid(row=1, column=1, sticky="ns", pady=(8, 0))
        b = ttk.Frame(f); b.grid(row=2, column=0, columnspan=2, pady=(10, 0))
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
        f = ttk.Frame(self, padding=(20, 16, 20, 14), style="Panel.TFrame"); f.grid(sticky="nsew")
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
            self.tv.heading(c, text=t, anchor=a); self.tv.column(c, width=w, anchor=a, stretch=(c == "model"))
        self.tv.tag_configure("sum", font=app.font_h)
        self.tv.grid(row=3, column=0, columnspan=2, sticky="we")
        f.columnconfigure(0, weight=1)
        # 바탕 정보
        self.v_info = tk.StringVar(); self.v_note = tk.StringVar()
        ttk.Label(f, textvariable=self.v_info, style="Hint.TLabel", wraplength=800, justify="left").grid(row=4, column=0, columnspan=2, sticky="w", pady=(8, 0))
        ttk.Label(f, textvariable=self.v_note, style="Hint.TLabel", wraplength=800, justify="left").grid(row=5, column=0, columnspan=2, sticky="w", pady=(2, 0))
        # 단가·환율 고치기(접힘)
        self.adj = ttk.Frame(f, style="Panel.TFrame", padding=(0, 10, 0, 0))
        self.e_in = ttk.Entry(self.adj, width=7); self.e_out = ttk.Entry(self.adj, width=7); self.e_fx = ttk.Entry(self.adj, width=7)
        self.l_model = ttk.Label(self.adj, text=""); self.l_model.grid(row=0, column=0, sticky="w")
        ttk.Label(self.adj, text="입력 $").grid(row=0, column=1, sticky="e", padx=(10, 2)); self.e_in.grid(row=0, column=2)
        ttk.Label(self.adj, text="출력 $").grid(row=0, column=3, sticky="e", padx=(10, 2)); self.e_out.grid(row=0, column=4)
        ttk.Label(self.adj, text="원/달러").grid(row=0, column=5, sticky="e", padx=(10, 2)); self.e_fx.grid(row=0, column=6)
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


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f"{APP_NAME} · 연어OCR {__version__}")
        self.geometry("1180x1000"); self.minsize(980, 760)
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
        """차분한 종이색 바탕에 옻칠색 머리띠. clam 테마 위에 색·여백·글꼴을 통일한다(0.6.0: 글꼴 자동 선택, 단추 위계, 간격 정돈)."""
        st = ttk.Style(self)
        try:
            st.theme_use("clam")
        except tk.TclError:
            pass
        sys_base = "맑은 고딕" if sys.platform.startswith("win") else ("Apple SD Gothic Neo" if sys.platform == "darwin" else "Noto Sans CJK KR")
        base = self._pick_font(self.FONT_CANDIDATES, sys_base)
        mono = self._pick_font(self.MONO_CANDIDATES, "Consolas" if sys.platform.startswith("win") else "DejaVu Sans Mono")
        self.font_base, self.font_small, self.font_h = (base, 10), (base, 9), (base, 10, "bold")
        self.font_title, self.font_sub, self.font_big = (base, 18, "bold"), (base, 10), (base, 12, "bold")
        self.font_mono = (mono, 10)
        self.option_add("*Font", self.font_base)
        self.option_add("*TCombobox*Listbox.font", self.font_base)
        C = self.colors = dict(BG="#f4f1ea", PANEL="#fcfbf7", LINE="#ddd6c7", INK="#27211d", MUTED="#7d7368",
                               ACC="#3a1c15", ACC_HI="#5a2a20", GOLD="#b8934e", RED="#b4271f", GREEN="#2f6f4a", FIELD="#ffffff",
                               BTN="#efe9dd", BTN_HI="#e5ddcd", SEL="#efe4c8")
        self.configure(bg=C["BG"])
        st.configure(".", background=C["BG"], foreground=C["INK"], font=self.font_base, bordercolor=C["LINE"], troughcolor="#e9e3d6")
        st.configure("TFrame", background=C["BG"]); st.configure("Panel.TFrame", background=C["PANEL"])
        st.configure("TLabel", background=C["BG"]); st.configure("Panel.TLabel", background=C["PANEL"])
        st.configure("Hint.TLabel", background=C["PANEL"], foreground=C["MUTED"], font=self.font_small)
        st.configure("Status.TLabel", background=C["BG"], foreground=C["ACC"], font=self.font_h)
        st.configure("Section.TLabel", background=C["PANEL"], foreground=C["ACC"], font=self.font_h)
        st.configure("TLabelframe", background=C["PANEL"], bordercolor=C["LINE"], relief="solid", borderwidth=1, padding=(12, 8, 12, 10))
        st.configure("TLabelframe.Label", background=C["PANEL"], foreground=C["ACC"], font=self.font_h, padding=(4, 0))
        # 단추 위계: 기본(도구) < 강조(실행) · 중지는 붉은 글자
        st.configure("TButton", padding=(12, 6), background=C["BTN"], lightcolor=C["BTN"], darkcolor=C["BTN"], bordercolor=C["LINE"], focuscolor=C["PANEL"], relief="flat")
        st.map("TButton", background=[("pressed", C["BTN_HI"]), ("active", C["BTN_HI"]), ("disabled", "#f1ede5")], foreground=[("disabled", "#aaa196")],
               bordercolor=[("active", C["GOLD"])])
        st.configure("Accent.TButton", background=C["ACC"], foreground="#fff7ea", font=self.font_h, padding=(20, 10),
                     lightcolor=C["ACC"], darkcolor=C["ACC"], bordercolor=C["ACC"], focuscolor=C["ACC"])
        st.map("Accent.TButton", background=[("pressed", C["ACC_HI"]), ("active", C["ACC_HI"]), ("disabled", "#b8a79d")], foreground=[("disabled", "#f3eee6")])
        st.configure("Stop.TButton", foreground=C["RED"], padding=(14, 10), font=self.font_h)
        st.configure("Small.TButton", padding=(8, 3), font=self.font_small)
        st.configure("TCheckbutton", background=C["PANEL"], padding=(2, 2)); st.configure("TRadiobutton", background=C["PANEL"], padding=(2, 2))
        st.map("TCheckbutton", background=[("active", C["PANEL"])]); st.map("TRadiobutton", background=[("active", C["PANEL"])])
        st.configure("TEntry", fieldbackground=C["FIELD"], padding=4, bordercolor=C["LINE"], lightcolor=C["LINE"], darkcolor=C["LINE"])
        st.map("TEntry", bordercolor=[("focus", C["GOLD"])], lightcolor=[("focus", C["GOLD"])], darkcolor=[("focus", C["GOLD"])])
        st.configure("TSpinbox", fieldbackground=C["FIELD"], padding=3, arrowsize=12)
        st.configure("TCombobox", fieldbackground=C["FIELD"], padding=3, arrowsize=14)
        st.map("TCombobox", fieldbackground=[("readonly", C["FIELD"])], selectbackground=[("readonly", C["FIELD"])], selectforeground=[("readonly", C["INK"])])
        st.configure("TNotebook", background=C["BG"], tabmargins=(2, 4, 2, 0), borderwidth=0)
        st.configure("TNotebook.Tab", padding=(16, 7), background="#e9e3d6", font=self.font_base)
        st.map("TNotebook.Tab", background=[("selected", C["PANEL"])], foreground=[("selected", C["ACC"])], padding=[("selected", (16, 7))])
        st.configure("Treeview", background=C["FIELD"], fieldbackground=C["FIELD"], rowheight=26, bordercolor=C["LINE"], lightcolor=C["LINE"], darkcolor=C["LINE"])
        st.map("Treeview", background=[("selected", C["SEL"])], foreground=[("selected", C["INK"])])
        st.configure("Treeview.Heading", background="#efe9dd", foreground=C["ACC"], font=self.font_h, relief="flat", padding=(6, 5))
        st.map("Treeview.Heading", background=[("active", C["BTN_HI"])])
        st.configure("Top.TNotebook", background=C["BG"], borderwidth=0, tabmargins=(0, 6, 0, 0))
        st.configure("Top.TNotebook.Tab", font=self.font_h, padding=(18, 9))
        st.map("Top.TNotebook.Tab", background=[("selected", C["PANEL"]), ("!selected", "#e6dfd0")], foreground=[("selected", C["ACC"]), ("!selected", C["MUTED"])])
        st.configure("Horizontal.TProgressbar", troughcolor="#e8e2d6", background=C["GOLD"], lightcolor=C["GOLD"], darkcolor=C["GOLD"], bordercolor="#e8e2d6", thickness=8)
        st.configure("TScrollbar", background=C["BTN"], troughcolor=C["BG"], bordercolor=C["BG"], arrowcolor=C["MUTED"], relief="flat")
        st.map("TScrollbar", background=[("active", C["BTN_HI"])])
        st.configure("TSeparator", background=C["LINE"])

    def _build(self):
        C = self.colors
        # ---- 머리띠 ----
        hd = tk.Frame(self, bg=C["ACC"], padx=20, pady=12); hd.pack(fill="x")
        tk.Frame(self, bg=C["GOLD"], height=3).pack(fill="x")                  # 머리띠 아래 금빛 선
        tk.Label(hd, text=APP_NAME, font=self.font_title, bg=C["ACC"], fg="#f3e9d2").pack(side="left")
        tk.Label(hd, text="연어OCR", font=self.font_big, bg=C["ACC"], fg="#d9c9a8").pack(side="left", padx=(12, 0), pady=(5, 0))
        tk.Label(hd, text="세로쓰기 한자·한글 문헌을 지침대로 판독해 HWPX로", font=self.font_sub, bg=C["ACC"], fg="#bfae8e").pack(side="left", padx=(14, 0), pady=(7, 0))
        self.v_pkg = tk.StringVar(value="패키지 점검 중…")
        tk.Label(hd, textvariable=self.v_pkg, font=self.font_small, bg=C["ACC"], fg="#e8c99a").pack(side="right", pady=(7, 0))
        tk.Label(hd, text=f"v{__version__}", font=(self.font_small[0], 9, "bold"), bg="#5a2a20", fg="#f3e9d2", padx=8, pady=2).pack(side="right", padx=(0, 14), pady=(5, 0))

        self.tabs = ttk.Notebook(self, style="Top.TNotebook"); self.tabs.pack(fill="x", padx=14, pady=(10, 0))
        root = ttk.Frame(self.tabs, padding=(10, 8, 10, 10)); self.tabs.add(root, text="   세로쓰기 인식   ")
        tab2 = ttk.Frame(self.tabs, padding=(10, 8, 10, 10)); self.tabs.add(tab2, text="   책갈피 OCR   ")
        bottom = ttk.Frame(self, padding=(14, 0, 14, 10)); bottom.pack(fill="both", expand=True)
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
        self.v_chunk = tk.StringVar(value=str(self.cfg.get("chunk_lines", 8))); self.v_mres = tk.BooleanVar(value=self.cfg.get("media_high", True))
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
        ttk.Label(rf, text="2차 조각", style="Panel.TLabel").grid(row=r, column=0, sticky="w", **pad)
        cp = ttk.Frame(rf, style="Panel.TFrame"); cp.grid(row=r, column=1, columnspan=3, sticky="w")
        ttk.Label(cp, text="한 장에", style="Panel.TLabel").pack(side="left", padx=(4, 2))
        ttk.Spinbox(cp, from_=0, to=30, width=3, textvariable=self.v_chunk).pack(side="left")
        ttk.Label(cp, text="줄(열)까지 · 넘으면 나눠 보냄 (0=안 나눔)", style="Panel.TLabel").pack(side="left", padx=(4, 10))
        ttk.Checkbutton(cp, text="조각 고해상(media_resolution high)", variable=self.v_mres).pack(side="left")
        ttk.Label(cp, text="글자당 픽셀을 늘려 入/人·曰/日 같은 획 하나 차이를 가립니다", style="Hint.TLabel").pack(side="left", padx=(10, 0))
        r += 1
        ck = ttk.Frame(rf, style="Panel.TFrame"); ck.grid(row=r, column=0, columnspan=4, sticky="w", pady=(2, 0))
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
        for i, (t, c) in enumerate([("3차 글자 판별", self.do_adjudicate), ("혼동 장부", self.do_ledger), ("비교 다시 계산", self.do_remerge),
                                    ("검수 결과 기록…", self.do_mark), ("패키지 설치·점검", self.install_packages), ("도움말", self.show_help)]):
            self._btn(sf, t, c, width=12).grid(row=1, column=i, padx=3, pady=(6, 2))
        ttk.Label(sf, text="[3차 글자 판별]은 1차·2차가 갈린 〔a/b〕 자리를 글자 하나씩 크게 잘라 자형만으로 묻습니다(자리마다 호출 한 번) · [혼동 장부]는 어느 글자가 어느 글자로 읽혔는지 모읍니다(호출 없음) · "
                           "프로그램을 새 판으로 바꾼 뒤에는 [비교 다시 계산] · 사람이 확인한 쪽은 [검수 결과 기록…]",
                  style="Hint.TLabel", wraplength=1000, justify="left").grid(row=2, column=0, columnspan=6, sticky="w", padx=6, pady=(4, 0))

        self._build_collate_tab(tab2)

        # ---- 진행(공통) ----
        pf2 = ttk.Frame(bottom); pf2.pack(fill="x", pady=(10, 0))
        self.progress = ttk.Progressbar(pf2, mode="indeterminate", length=240); self.progress.pack(side="left", pady=4)
        self.v_status = tk.StringVar(value="준비됨"); ttk.Label(pf2, textvariable=self.v_status, style="Status.TLabel").pack(side="left", padx=14)

        # ---- 기록 / 쪽별 상태(공통) ----
        nb = ttk.Notebook(bottom); nb.pack(fill="both", expand=True, pady=(8, 0))
        self.log = ScrolledText(nb, height=14, wrap="word", state="disabled", font=self.font_mono, bg="#fffdf8", fg=C["INK"],
                                relief="flat", padx=14, pady=10, insertbackground=C["INK"], spacing1=1, spacing3=1, selectbackground=C["SEL"])
        nb.add(self.log, text="진행 기록")
        tf = ttk.Frame(nb); nb.add(tf, text="쪽별 상태")
        self.table = ttk.Treeview(tf, columns=("pdf", "print", "status", "unc", "figs"), show="headings")
        for c, t, w in (("pdf", "PDF 쪽", 70), ("print", "인쇄 쪽", 70), ("status", "상태", 560), ("unc", "확인 필요", 80), ("figs", "도판", 60)):
            self.table.heading(c, text=t); self.table.column(c, width=w, anchor="center" if c != "status" else "w")
        sb = ttk.Scrollbar(tf, orient="vertical", command=self.table.yview); self.table.configure(yscrollcommand=sb.set)
        self.table.pack(side="left", fill="both", expand=True); sb.pack(side="right", fill="y")
        self.nb = nb
        self._reader_changed()

    # ---------- 책갈피 교정 탭 ----------
    def _build_collate_tab(self, tab):
        from .collate.run import POLICIES
        self.v_hwpx = tk.StringVar(value=self.cfg.get("hwpx", "")); self.v_pdf2 = tk.StringVar(value=self.cfg.get("collate_pdf", "")); self.v_find = tk.StringVar()
        self.v_policy = tk.StringVar(value=POLICIES.get(self.cfg.get("collate_policy", "all"), POLICIES["all"]))
        self.v_cmemo = tk.BooleanVar(value=self.cfg.get("collate_memo", True)); self.v_cvert = tk.BooleanVar(value=self.cfg.get("collate_vertical", False))
        self.collate_rows: list[dict] = []
        f = ttk.LabelFrame(tab, text="교정할 문서"); f.pack(fill="x")
        r0 = ttk.Frame(f, style="Panel.TFrame"); r0.pack(fill="x", padx=8, pady=(6, 2))
        ttk.Label(r0, text="PDF", style="Panel.TLabel", width=6).pack(side="left")
        ttk.Entry(r0, textvariable=self.v_pdf2).pack(side="left", fill="x", expand=True, padx=(4, 8))
        self._btn(r0, "PDF 고르기…", self.pick_pdf2, width=13).pack(side="left")
        r1 = ttk.Frame(f, style="Panel.TFrame"); r1.pack(fill="x", padx=8, pady=(2, 2))
        ttk.Label(r1, text="HWPX", style="Panel.TLabel", width=6).pack(side="left")
        ttk.Entry(r1, textvariable=self.v_hwpx).pack(side="left", fill="x", expand=True, padx=(4, 8))
        self._btn(r1, "HWPX 고르기…", self.pick_hwpx, width=13).pack(side="left")
        rs = ttk.Frame(f, style="Panel.TFrame"); rs.pack(fill="x", padx=8, pady=(2, 2))
        ttk.Label(rs, text="책갈피로 쓸 스타일", style="Panel.TLabel").pack(side="left")
        self.v_hstyles = tk.StringVar(value=self.cfg.get("heading_styles", ""))
        ttk.Entry(rs, textvariable=self.v_hstyles).pack(side="left", fill="x", expand=True, padx=(6, 8))
        self._btn(rs, "스타일 고르기…", self.pick_styles, width=13).pack(side="left")
        ttk.Label(rs, text="비우면 이름에 '제목'이 든 스타일 전부 · 앞에 적은 것이 큰 단위", style="Hint.TLabel").pack(side="left", padx=(10, 0))
        r2 = ttk.Frame(f, style="Panel.TFrame"); r2.pack(fill="x", padx=8, pady=(2, 8))
        ttk.Label(r2, text="판독기·모델·API 키·속도는 [세로쓰기 인식] 탭의 설정을 따릅니다 · 읽기 모드: 가로쓰기 한문(간체)", style="Hint.TLabel").pack(side="left")
        ttk.Checkbutton(r2, text="세로쓰기 문헌(지침 그대로)", variable=self.v_cvert).pack(side="right", padx=(12, 0))
        self.v_cforce = tk.BooleanVar(value=False)
        ttk.Checkbutton(r2, text="기록 있는 쪽도 다시 판독", variable=self.v_cforce).pack(side="right", padx=(12, 0))

        g = ttk.LabelFrame(tab, text="구획(책갈피·제목)"); g.pack(fill="both", expand=True, pady=(8, 0))
        r3 = ttk.Frame(g, style="Panel.TFrame"); r3.pack(fill="x", padx=8, pady=(6, 4))
        self._btn(r3, "구획 색인", self.do_collate_index, width=10).pack(side="left")
        ttk.Label(r3, text="기준", style="Panel.TLabel").pack(side="left", padx=(12, 4))
        from .collate.run import ANCHOR_SOURCES
        self.v_asrc = tk.StringVar(value=ANCHOR_SOURCES.get(self.cfg.get("anchor_source", "auto"), ANCHOR_SOURCES["auto"]))
        ttk.Combobox(r3, textvariable=self.v_asrc, values=list(ANCHOR_SOURCES.values()), state="readonly", width=44).pack(side="left")
        ttk.Label(r3, text="찾기(한자·독음)", style="Panel.TLabel").pack(side="left", padx=(12, 4))
        e = ttk.Entry(r3, textvariable=self.v_find, width=18); e.pack(side="left"); e.bind("<KeyRelease>", lambda _e: self.refresh_collate_table())
        self.v_cstat = tk.StringVar(value="HWPX 를 고르고 [구획 색인]을 누르십시오.")
        r3b = ttk.Frame(g, style="Panel.TFrame"); r3b.pack(fill="x", padx=8, pady=(0, 4))
        ttk.Label(r3b, textvariable=self.v_cstat, style="Hint.TLabel", wraplength=1080, justify="left").pack(side="left")
        tf = ttk.Frame(g); tf.pack(fill="both", expand=True, padx=8)
        cols = ("no", "lv", "name", "read", "para", "pdf", "page", "how")
        self.ctable = ttk.Treeview(tf, columns=cols, show="headings", height=8, selectmode="browse")
        for c, t, w, a in (("no", "번호", 50, "e"), ("lv", "급", 36, "center"), ("name", "HWPX 책갈피·제목", 280, "w"), ("read", "독음", 150, "w"), ("para", "문단", 60, "e"),
                           ("pdf", "PDF 책갈피", 220, "w"), ("page", "PDF 쪽", 60, "e"), ("how", "짝", 70, "center")):
            self.ctable.heading(c, text=t); self.ctable.column(c, width=w, anchor=a, stretch=(c in ("name", "pdf", "read")))
        sb = ttk.Scrollbar(tf, orient="vertical", command=self.ctable.yview); self.ctable.configure(yscrollcommand=sb.set)
        self.ctable.pack(side="left", fill="both", expand=True); sb.pack(side="right", fill="y")
        self.ctable.bind("<Double-1>", lambda _e: self.do_collate(apply=False))

        r4 = ttk.Frame(g, style="Panel.TFrame"); r4.pack(fill="x", padx=8, pady=(6, 8))
        ttk.Label(r4, text="교정 정책", style="Panel.TLabel").pack(side="left")
        ttk.Combobox(r4, textvariable=self.v_policy, values=list(POLICIES.values()), state="readonly", width=34).pack(side="left", padx=(4, 10))
        ttk.Checkbutton(r4, text="한컴 메모로 근거 남기기", variable=self.v_cmemo).pack(side="left")
        self._btn(r4, "교정본 열기", self.open_collated, width=11).pack(side="right")
        self._btn(r4, "보고 열기", self.open_collate_report, width=10).pack(side="right", padx=(0, 6))
        self._btn(r4, "교정 반영·저장", lambda: self.do_collate(apply=True), style="Accent.TButton").pack(side="right", padx=(0, 10))
        self._btn(r4, "대조만(보고)", lambda: self.do_collate(apply=False), width=12).pack(side="right", padx=(0, 6))
        ttk.Label(tab, text="구획을 고른 뒤 [대조만]은 차이 보고(HTML)만 만들고, [교정 반영·저장]은 HWPX 사본(<이름>_교정.hwpx)에 고쳐 넣습니다. 원본은 건드리지 않습니다. "
                           "자동 교정은 파란 글자·연두 형광펜, 확인 요청은 빨간 글자·노란 형광펜으로 표시되고 근거는 메모에 남습니다.", style="Hint.TLabel", wraplength=1080, justify="left").pack(fill="x", pady=(6, 0))

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
                names = HwpxBook(h).heading_style_names()
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
            counts = HwpxBook(h).style_counts()
        except Exception as e:
            messagebox.showerror(APP_NAME, f"스타일을 읽지 못했습니다: {e}"); return
        StylePicker(self, counts)

    def set_heading_styles(self, names: list[str]):
        self.v_hstyles.set(", ".join(names)); self.cfg["heading_styles"] = self.v_hstyles.get(); save_config(self.cfg)

    def heading_styles_list(self) -> list[str]:
        return [s.strip() for s in self.v_hstyles.get().split(",") if s.strip()]

    def anchor_source(self) -> str:
        from .collate.run import ANCHOR_SOURCES
        return next((k for k, v in ANCHOR_SOURCES.items() if v == self.v_asrc.get()), "auto")

    def collate_policy(self) -> str:
        from .collate.run import POLICIES
        return next((k for k, v in POLICIES.items() if v == self.v_policy.get()), "all")

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
                doc = open_pdf(pdf); book = HwpxBook(hwpx)
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
                    lines.append("   ⚠ 간체·번체 변환 패키지(opencc)가 없어 PDF 책갈피(간체)와 HWPX 제목(번체)이 맞지 않습니다. [세로쓰기 인식] 탭의 [패키지 설치·점검] 뒤 프로그램을 다시 시작하십시오.")
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
        for r in self.collate_rows:
            if q and q not in zh.compare_key(normalize_name(r["name"])) and q not in zh.compare_key(normalize_name(r["pdf"])) and not reading.matches(r["name"], raw):
                continue
            self.ctable.insert("", "end", iid=str(r["no"]), values=(r["no"], r["lv"], ("    " * (r["lv"] - 1)) + r["name"], r.get("read", ""), r["para"], r["pdf"], r["page"], r["how"]))

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
                guess = find_title_pages(open_pdf(self.pdf2()), row["name"], nxt)
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
        if not self.v_cmemo.get(): argv.append("--no-memo")
        argv += ["--mode", "vertical" if self.v_cvert.get() else "horizontal_zh"]
        self.cfg.update({"hwpx": self.v_hwpx.get().strip(), "collate_pdf": self.v_pdf2.get().strip(), "collate_policy": self.collate_policy(), "collate_memo": self.v_cmemo.get(), "collate_vertical": self.v_cvert.get()}); save_config(self.cfg)
        self.nb.select(0)
        self.start(argv, ("교정 반영·저장" if apply else "대조") + f" — {row['name'][:20]}")

    def _latest(self, folder: Path, pattern: str) -> Path | None:
        files = sorted(folder.glob(pattern), key=lambda p: p.stat().st_mtime) if folder.exists() else []
        return files[-1] if files else None

    def open_collate_report(self):
        if not self.pdf2(): return
        f = self._latest(self.work2() / "collate", "*.html")
        if f: open_path(f)
        else: messagebox.showinfo(APP_NAME, "아직 대조 보고가 없습니다. 구획을 고르고 [대조만] 또는 [교정 반영·저장]을 누르십시오.")

    def open_collated(self):
        h = self.v_hwpx.get().strip()
        if not h: return
        out = Path(h).with_name(Path(h).stem.replace("_교정", "") + "_교정.hwpx")
        if out.exists(): open_path(out)
        else: messagebox.showinfo(APP_NAME, f"교정본이 아직 없습니다: {out.name}")

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

    def _read_opts(self, print_offset: bool = True, force: bool | None = None) -> list[str]:
        o = ["--reader", self.v_reader.get(), "--upscale", self.v_up.get().strip() or "2"]
        if self.v_reader.get() == "gemini" and self.v_model.get().strip():
            o += ["--model", self.v_model.get().strip()]
        if not self.v_second.get():
            o.append("--no-second-pass")
        if print_offset and self.v_off.get().strip():
            o += ["--print-offset", self.v_off.get().strip()]          # 책갈피 OCR 은 다른 책이라 인식 탭의 보정값을 쓰지 않는다
        if (self.v_force.get() if force is None else force):
            o.append("--force")
        o += ["--workers", (self.v_workers.get().strip() or "6")]
        o += ["--second-model", self.v_model2.get().strip()]          # 빈 값이면 1차와 같은 모델, auto 면 자동
        o += ["--second-thinking", "low" if self.v_think2.get() else "full"]
        o += ["--chunk-lines", (self.v_chunk.get().strip() or "0"), "--media-resolution", "auto" if self.v_mres.get() else "default"]
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
                elif item[0] == "sections":
                    self.collate_rows = item[1]; self.v_cstat.set(item[2]); self.refresh_collate_table()
                elif item[0] == "styles_suggest":
                    if not self.v_hstyles.get().strip():
                        self.v_hstyles.set(item[1])
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
                         "checkpoint": int(self.v_ckpt.get() or 0), "use_cache": self.v_cache.get(), "second_thinking_low": self.v_think2.get(), "last_pdf": self.pdf(),
                         "chunk_lines": int(self.v_chunk.get() or 0), "media_high": self.v_mres.get()})
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

    def estimate_data(self) -> dict:
        """현재 설정으로 호출·토큰·비용 어림. 단가는 내장 요금표, 환율은 자동 조회라 따로 넣을 것이 없다."""
        from .estimate import estimate
        from .pdfio import open_pdf, parse_pages
        from .state import Work
        doc = open_pdf(self.pdf()); pages = parse_pages(self.v_pages.get().strip() or None, len(doc))
        w = Work(self.pdf(), str(self.work()))
        fx, fx_src = self.fx_setting()
        return estimate(self.pdf(), pages, upscale=float(self.v_up.get() or 2), second_pass=self.v_second.get(), model=self.v_model.get().strip(),
                        second_model=self.v_model2.get().strip(), use_cache=self.v_cache.get(),
                        prices=self.cfg.get("prices") or {}, fx_krw=fx, fx_source=fx_src, records=[] if self.v_force.get() else w.records(pages))

    def estimate_text(self) -> str:
        try:
            from .estimate import format_estimate
            return format_estimate(self.estimate_data())
        except ModuleNotFoundError as e:
            return f"패키지가 없어 계산하지 못했습니다: {e.name}. [필요한 패키지 설치·점검] 을 누르십시오."
        except Exception as e:
            return f"계산 실패: {e}"

    def do_estimate(self):
        if not self._need_pdf():
            return
        try:
            EstimateDialog(self, self.estimate_data())
        except ModuleNotFoundError as e:
            messagebox.showerror(APP_NAME, f"패키지가 없어 계산하지 못했습니다: {e.name}. [필요한 패키지 설치·점검] 을 누르십시오.")
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

    def do_adjudicate(self):
        """3차 글자 판별: 〔a/b〕 자리마다 글자 조각을 모델에 묻는다. 자리 수를 먼저 세어 보여 주고 묻는다."""
        if not self._need_pdf() or not self._prepare_env():
            return
        if self.v_reader.get() != "gemini":
            messagebox.showinfo(APP_NAME, "3차 판별은 자형을 보는 모델이 필요합니다. 판독기를 Gemini 로 바꾸십시오."); return
        try:
            from .state import Work
            from .adjudicate import find_sites
            from .schema import PageReading
            from .pdfio import parse_pages, open_pdf
            w = Work(self.pdf(), str(self.work())); doc = open_pdf(self.pdf())
            recs = w.records(parse_pages(self.v_pages.get().strip() or None, len(doc)))
            n_sites = sum(len(find_sites(PageReading.model_validate(r["merged"]))) for r in recs)
        except Exception as e:
            messagebox.showerror(APP_NAME, f"판독 기록을 읽지 못했습니다: {e}"); return
        if not n_sites:
            messagebox.showinfo(APP_NAME, "판별할 〔a/b〕 자리가 없습니다(1차·2차가 갈린 자리가 없음)."); return
        if not messagebox.askyesno(APP_NAME, f"1차·2차가 갈린 자리 {n_sites}곳을 글자 하나씩 크게 잘라 모델에 묻습니다(호출 {n_sites}회, 글자 조각이라 토큰은 적음).\n"
                                             f"획이 보이면 확정해 본문에 넣고, 보이지 않으면 표시를 그대로 둡니다. 계속할까요?"):
            return
        self.nb.select(0)
        argv = ["adjudicate", self.pdf(), "--work", str(self.work())] + self._pages() + (["--model", self.v_model.get().strip()] if self.v_model.get().strip() else [])
        if not self.v_cache.get(): argv.append("--no-cache")
        self.start(argv, "3차 글자 판별")

    def do_ledger(self):
        if not self._need_pdf(): return
        self.nb.select(0)
        self.start(["ledger", self.pdf(), "--work", str(self.work())], "혼동 장부")

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
            if rc == 0:
                self.q.put(("log", "설치가 끝났습니다. 새 패키지가 확실히 적용되도록 프로그램을 한 번 다시 시작하십시오.\n"))
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
