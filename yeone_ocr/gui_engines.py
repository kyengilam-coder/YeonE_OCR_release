"""엔진 관리 창: 이 PC의 GPU 점검 결과 · 설치할 로컬 OCR 엔진 고르기 · 진행·로그 · 실패한 것 다시 시도.

설치는 engine_manager.run_install 이 작업 스레드에서 하고, 화면은 큐를 after() 로 비워 갱신한다
(Tk 는 만든 스레드에서만 만진다). 창을 닫거나 [취소]하면 설치 프로세스와 그 하위 프로세스를 끝낸다.
문서·API 키를 읽지 않으며, 설치할 때만 공개 파일을 내려받는다.
"""
from __future__ import annotations

import queue
import threading
import tkinter as tk
from tkinter import messagebox, ttk

from . import engine_manager as em

STATUS_TEXT = {"ok": "설치 완료", "unverified": "설치됨 · 확인 실패", "failed": "실패", "skipped": "건너뜀", "cancelled": "취소됨"}


class EngineManagerDialog(tk.Toplevel):
    def __init__(self, app, *, selected_root=None, on_installed=None):
        super().__init__(app)
        self.app, self.selected_root, self.on_installed = app, selected_root or None, on_installed
        self.events: queue.Queue = queue.Queue()
        self.cancel = threading.Event()
        self.worker: threading.Thread | None = None
        self.gpus: list = []
        self.advice: dict = {}
        self.status: dict = {}
        self.last_log = None
        self._closing = False
        self._poll_job = None
        C, P, F = app.colors, app.px, app.fonts
        self.title("엔진 관리")
        self.transient(app)
        self.configure(bg=C["PANEL"], highlightthickness=1, highlightbackground=C["RULE"])
        self.minsize(P(680), P(600))
        body = tk.Frame(self, bg=C["PANEL"], padx=P(22), pady=P(16)); body.pack(fill="both", expand=True)

        def label(parent, text="", font="BODY", fg="INK", **kw):
            return tk.Label(parent, text=text, font=F[font], fg=C[fg], bg=C["PANEL"], bd=0, padx=0, pady=0, **kw)

        head = tk.Frame(body, bg=C["PANEL"]); head.pack(fill="x")
        label(head, "엔진 관리", "NUM").pack(side="left")
        ttk.Label(head, text="이 PC · 모든 책 공통", style="Chip.TLabel").pack(side="right")
        label(body, "GPU를 점검하고, 고른 OCR 엔진을 이 PC의 사용자 폴더에 내려받아 설치합니다. 앱을 업데이트해도 설치한 엔진은 남습니다.",
              "CAP", "MUTED", justify="left", anchor="w", wraplength=P(630)).pack(fill="x", pady=(P(6), P(10)))

        gpu = tk.Frame(body, bg=C["PANEL"]); gpu.pack(fill="x")
        label(gpu, "GPU", "BODY_B", width=8, anchor="w").pack(side="left")
        self.b_probe = ttk.Button(gpu, text="다시 점검", command=self.refresh, cursor="hand2"); self.b_probe.pack(side="right")
        self.v_gpu = tk.StringVar(value="점검 중…")
        self.l_gpu = label(gpu, justify="left", anchor="w", wraplength=P(470), textvariable=self.v_gpu)
        self.l_gpu.pack(side="left", fill="x", expand=True)
        place = tk.Frame(body, bg=C["PANEL"]); place.pack(fill="x", pady=(P(6), 0))
        label(place, "설치 위치", "BODY_B", width=8, anchor="w").pack(side="left")
        ttk.Button(place, text="폴더 열기", command=self.open_folder, cursor="hand2").pack(side="right")
        self.v_root = tk.StringVar(value=str(em.install_root(self.selected_root)))
        label(place, font="CAP", fg="MUTED", justify="left", anchor="w", wraplength=P(470), textvariable=self.v_root).pack(
            side="left", fill="x", expand=True)

        tk.Frame(body, bg=C["LINE"], height=max(1, P(1))).pack(fill="x", pady=(P(12), P(4)))
        self.rows: dict[str, dict] = {}
        for option in em.OPTIONS:
            row = tk.Frame(body, bg=C["PANEL"]); row.pack(fill="x", pady=(P(4), 0))
            variable = tk.BooleanVar(value=False)
            check = ttk.Checkbutton(row, text=option.label, variable=variable, cursor="hand2", state="disabled",
                                    command=lambda key=option.key: self._picked(key))
            check.pack(side="left", padx=(P(18) if option.requires else 0, 0))
            state = label(row, "", "CAP_B", "MUTED"); state.pack(side="right")
            detail = label(body, f"{option.summary} · 내려받기 약 {option.download_gb:g} GB"
                           + (f"\n{option.notice}" if option.notice else ""), "CAP", "MUTED",
                           justify="left", anchor="w", wraplength=P(600))
            detail.pack(fill="x", padx=(P(42) if option.requires else P(24), 0))
            self.rows[option.key] = {"var": variable, "check": check, "state": state, "detail": detail}

        tk.Frame(body, bg=C["LINE"], height=max(1, P(1))).pack(fill="x", pady=(P(12), P(8)))
        self.v_stage = tk.StringVar(value="")
        label(body, anchor="w", textvariable=self.v_stage).pack(fill="x")
        self.bar = ttk.Progressbar(body, style="Panel.Bar.Horizontal.TProgressbar", maximum=1000, mode="determinate")
        self.bar.pack(fill="x", pady=(P(4), P(2)))
        self.v_detail = tk.StringVar(value="")
        label(body, font="CAP", fg="MUTED", anchor="w", textvariable=self.v_detail).pack(fill="x")
        # 바닥 줄을 먼저 붙인다(side=bottom) — 창이 낮아도 단추가 잘리지 않고 로그 칸이 남은 높이를 쓴다
        foot = tk.Frame(body, bg=C["PANEL"]); foot.pack(side="bottom", fill="x", pady=(P(8), 0))
        self.b_close = ttk.Button(foot, text="닫기", command=self.close, style="Dlg.TButton", cursor="hand2")
        self.b_close.pack(side="right")
        self.b_install = ttk.Button(foot, text="설치", command=self.start, style="DlgInkDef.TButton", cursor="hand2", state="disabled")
        self.b_install.pack(side="right", padx=(0, P(6)))
        self.b_log = ttk.Button(foot, text="로그 파일 열기", command=self.open_log, cursor="hand2", state="disabled")
        self.b_log.pack(side="left")
        label(body, "설치할 때만 공개 파일을 내려받습니다 · 문서와 API 키는 읽지 않습니다.", "CAP", "MUTED",
              justify="left", anchor="w").pack(side="bottom", fill="x", pady=(P(8), 0))
        box = tk.Frame(body, bg=C["PANEL"]); box.pack(fill="both", expand=True, pady=(P(6), 0))
        self.log = tk.Text(box, height=6, wrap="word", font=F["LOG"], bg=C["QUIET"], fg=C["INK"], relief="flat", bd=0,
                           padx=P(8), pady=P(6), state="disabled", highlightthickness=1, highlightbackground=C["LINE"])
        scroll = ttk.Scrollbar(box, orient="vertical", command=self.log.yview)
        self.log.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y"); self.log.pack(side="left", fill="both", expand=True)
        self.log.tag_configure("fail", foreground=C["RED"]); self.log.tag_configure("good", foreground=C["GREEN"])
        self.log.tag_configure("warn", foreground=C["WARN"])
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.bind("<Escape>", lambda _event: (self.close(), "break")[1])
        try:
            from .gui import place_over
            self.geometry(f"{P(700)}x{P(800)}"); place_over(self, app)
        except (ImportError, tk.TclError):
            pass
        self._poll()
        self.refresh()

    # ---------- 화면 갱신(주 스레드) ----------
    @property
    def busy(self) -> bool:
        return self.worker is not None and self.worker.is_alive()

    def _poll(self):
        self._poll_job = None
        try:
            while True:
                kind, payload = self.events.get_nowait()
                getattr(self, "_on_" + kind)(payload)
        except queue.Empty:
            pass
        except tk.TclError:
            return
        self._poll_job = self.after(100, self._poll)

    def _write(self, text: str, tag: str | None = None):
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n", (tag,) if tag else ())
        if int(self.log.index("end-1c").split(".")[0]) > 3000:          # 긴 pip 출력으로 창이 느려지지 않게
            self.log.delete("1.0", "500.0")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _on_log(self, payload):
        line, transient = payload
        if transient:
            self.v_detail.set(line[-110:])
        else:
            self._write(line, "fail" if ("실패" in line or "Error" in line or "error:" in line.lower()) else None)

    def _on_progress(self, payload):
        index, total, name, phase = payload
        if phase == "done":
            self.bar.configure(value=1000)
            return
        self.v_stage.set(f"{index + 1}/{total} · {name} — " + ("설치 확인 중" if phase == "verify" else "내려받아 설치하는 중"))
        self.v_detail.set("")
        self.bar.configure(value=int(1000 * (index + (0.9 if phase == "verify" else 0.05)) / max(1, total)))

    def _on_probed(self, payload):
        self.gpus, message, self.status = payload["gpus"], payload["message"], payload["status"]
        self.advice = em.recommend(self.gpus, self.status)
        self.v_gpu.set(message)
        self.l_gpu.configure(fg=self.app.colors["INK" if self.gpus else "WARN"])
        self._show_options(preselect=payload.get("preselect", True))
        self.b_probe.configure(state="normal")

    def _show_options(self, preselect: bool, keep=()):
        C = self.app.colors
        for option in em.OPTIONS:
            row, advice = self.rows[option.key], self.advice[option.key]
            installed = self.status[option.key]["installed"]
            row["check"].configure(state="normal" if advice["selectable"] else "disabled")
            if installed or not advice["selectable"]:
                row["var"].set(False)
            elif option.key in keep:
                row["var"].set(True)
            elif preselect:
                row["var"].set(advice["recommended"])
            text, color = (("설치됨", "GREEN") if installed else ("추천", "BLUE") if advice["recommended"]
                           else (advice["reason"] or "선택 가능", "WARN" if advice["reason"] else "MUTED"))
            row["state"].configure(text=text if len(text) <= 46 else text[:45] + "…", fg=C[color])
        self._sync_install_button()

    def _picked(self, key: str):
        """GPU 가속을 고르면 그 바탕 엔진도 함께, 바탕 엔진을 빼면 그 GPU 가속도 함께."""
        chosen = self.rows[key]["var"].get()
        option = em.OPTION_BY_KEY[key]
        if chosen and option.requires and not self.status[option.requires]["installed"]:
            self.rows[option.requires]["var"].set(True)
        if not chosen:
            for other in em.OPTIONS:
                if other.requires == key and not self.status[key]["installed"]:
                    self.rows[other.key]["var"].set(False)
        self._sync_install_button()

    def selected(self) -> list[str]:
        return [key for key, row in self.rows.items() if row["var"].get()]

    def _sync_install_button(self):
        keys = self.selected()
        size = sum(em.OPTION_BY_KEY[key].download_gb for key in keys)
        self.b_install.configure(state="normal" if keys and not self.busy else "disabled",
                                 text=f"설치 (약 {size:g} GB)" if keys else "설치")

    # ---------- 작업(다른 스레드 — 화면은 큐로만) ----------
    def refresh(self, preselect: bool = True):
        if self.busy:
            return
        self.b_probe.configure(state="disabled"); self.v_gpu.set("점검 중…")
        root = self.selected_root

        def work():
            probe = em.probe_gpus()
            self.events.put(("probed", {**probe, "status": em.statuses(root), "preselect": preselect}))
        self.worker = threading.Thread(target=work, name="engine-probe", daemon=True)
        self.worker.start()

    def start(self):
        keys = self.selected()
        if not keys or self.busy:
            return
        order = em.plan(keys, self.status)
        size = sum(em.OPTION_BY_KEY[key].download_gb for key in order)
        names = "\n".join("  · " + em.OPTION_BY_KEY[key].label for key in order)
        notices = "\n".join(em.OPTION_BY_KEY[key].notice for key in order if em.OPTION_BY_KEY[key].notice)
        if not messagebox.askokcancel("엔진 관리", f"다음을 내려받아 설치합니다(약 {size:g} GB · 인터넷 필요 · 수 분~수십 분).\n\n{names}\n\n"
                                      f"설치 위치: {self.v_root.get()}" + (f"\n\n{notices}" if notices else ""), parent=self):
            return
        self.cancel = threading.Event()
        self._set_running(True)
        self.v_stage.set("설치 준비 중…"); self.bar.configure(value=0)
        cancel, root, gpus, events = self.cancel, self.selected_root, list(self.gpus), self.events

        def work():
            try:
                outcome = em.run_install(order, selected_root=root, cancel=cancel, gpus=gpus,
                                         log=lambda line, transient=False: events.put(("log", (line, transient))),
                                         progress=lambda *step: events.put(("progress", step)))
            except Exception as exc:                                  # 화면에 알리고 끝낸다(스레드에서 조용히 죽지 않게)
                outcome = {"results": [{"key": key, "label": em.OPTION_BY_KEY[key].label, "status": "failed",
                                        "message": f"{type(exc).__name__}: {exc}"} for key in order], "log": None, "roots": []}
            outcome["status"] = em.statuses(root)
            events.put(("finished", outcome))
        self.worker = threading.Thread(target=work, name="engine-install", daemon=True)
        self.worker.start()

    def _set_running(self, on: bool):
        for row in self.rows.values():
            row["check"].configure(state="disabled")
        self.b_probe.configure(state="disabled" if on else "normal")
        self.b_install.configure(state="disabled")
        self.b_close.configure(text="취소" if on else "닫기", state="normal")

    def _on_finished(self, outcome):
        self.last_log = outcome.get("log")
        self.b_log.configure(state="normal" if self.last_log else "disabled")
        self.status = outcome["status"]
        self.advice = em.recommend(self.gpus, self.status)
        results = outcome["results"]
        self._write("")
        for item in results:
            tag = {"ok": "good", "unverified": "warn"}.get(item["status"], "fail")
            self._write(f"{item['label']}: {STATUS_TEXT[item['status']]}" + (f" — {item['message']}" if item["message"] else ""), tag)
        retry = [item["key"] for item in results if item["status"] in ("failed", "skipped", "cancelled")]
        good = sum(item["status"] in ("ok", "unverified") for item in results)
        self.v_detail.set("")
        self.v_stage.set(f"끝 — {good}개 설치" + (f" · {len(retry)}개는 끝나지 않음(체크된 채로 두었습니다 — [설치]를 누르면 이어서 다시 시도)"
                                                if retry else ""))
        self._set_running(False)
        self._show_options(preselect=False, keep=retry)
        if good and self.on_installed is not None:
            self.on_installed(outcome)

    # ---------- 단추 ----------
    def open_folder(self):
        root = em.install_root(self.selected_root)
        try:
            root.mkdir(parents=True, exist_ok=True)
            from .gui import open_path
            open_path(root)
        except OSError as exc:
            messagebox.showerror("엔진 관리", f"폴더를 열지 못했습니다.\n{root}\n{exc}", parent=self)

    def open_log(self):
        if self.last_log:
            try:
                from .gui import open_path
                open_path(self.last_log)
            except OSError as exc:
                messagebox.showerror("엔진 관리", f"로그를 열지 못했습니다.\n{self.last_log}\n{exc}", parent=self)

    def close(self):
        if self._closing:
            return
        if self.worker is not None and self.worker.name == "engine-install" and self.busy:
            if not messagebox.askyesno("엔진 관리", "설치를 취소할까요?\n받은 파일은 남아 있어, 다음에 이어서 설치할 수 있습니다.", parent=self):
                return
            self.cancel.set()
            self.v_stage.set("취소하는 중…"); self.b_close.configure(state="disabled")
            return                                                   # 창은 남겨 결과를 보여 준다 — 한 번 더 누르면 닫힌다
        self._finish_close()

    def _finish_close(self):
        self._closing = True
        if self._poll_job is not None:
            try:
                self.after_cancel(self._poll_job)
            except tk.TclError:
                pass
            self._poll_job = None
        self.destroy()
