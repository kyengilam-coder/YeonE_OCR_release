"""엔진 혼동 장부 창: 로컬 엔진이 자주 틀린 글자를 보고, 잘못 쌓인 쌍을 빼고, 장부를 비운다.

창 모양은 중문부호 규칙 창(gui_corrections)과 같다: 서재 바탕의 작은 창, 표(엔진 · 읽은 글자 · → · 확정 글자 ·
가중 오류 · 사람/AI확인/자동 · 오류율 · 의심), 고른 줄의 보기, 바닥 [선택 쌍 제외]·[제외 모두 되살리기]·[장부 비우기…]·[닫기].
장부는 어디를 다시 볼지만 정하며 글자를 바꾸지 않는다. 바꾼 내용은 바로 장부 파일에 저장된다."""
from pathlib import Path
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk


class ConfusionLedgerDialog(tk.Toplevel):
    def __init__(self, app):
        super().__init__(app)
        self.app = app
        C, P, F = app.colors, app.px, app.fonts
        self.title("엔진 혼동 장부")
        self.transient(app)
        self.configure(bg=C["PANEL"], highlightthickness=1, highlightbackground=C["RULE"])
        self.minsize(P(720), P(640))
        self.learned = None
        self.items = {}
        body = tk.Frame(self, bg=C["PANEL"], padx=P(22), pady=P(16)); body.pack(fill="both", expand=True)
        head = tk.Frame(body, bg=C["PANEL"]); head.pack(fill="x")
        tk.Label(head, text="엔진 혼동 장부", font=F["NUM"], fg=C["INK"], bg=C["PANEL"], bd=0).pack(side="left")
        ttk.Label(head, text="엔진별 · 모든 책 공통", style="Chip.TLabel").pack(side="right")
        self.intro = app._rich_line(body)
        self.intro.pack(fill="x", pady=(P(10), P(8)))
        table = tk.Frame(body, bg=C["PANEL"]); table.pack(fill="both", expand=True)
        st = ttk.Style(self)
        st.configure("Rules.Treeview", font=F["MONO_L"], rowheight=P(27))
        columns = (("engine", "엔진", 110, "w", True), ("read", "읽은 글자", 80, "center", False), ("to", "", 30, "center", False),
                   ("actual", "확정 글자", 80, "center", False), ("weight", "가중 오류", 80, "e", False),
                   ("grades", "사람·AI확인·자동", 130, "center", False), ("rate", "오류율", 80, "e", False),
                   ("risky", "의심", 50, "center", False))
        self.tree = ttk.Treeview(table, columns=[c[0] for c in columns], show="headings", selectmode="browse", height=7,
                                 style="Rules.Treeview")
        for col, text, width, anchor, stretch in columns:
            self.tree.heading(col, text=text, anchor=anchor)
            self.tree.column(col, width=P(width), minwidth=P(min(width, 30)), anchor=anchor, stretch=stretch)
        self.tree.tag_configure("odd", background=C["STRIPE"])
        sb = ttk.Scrollbar(table, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y", padx=(P(4), 0)); self.tree.pack(side="left", fill="both", expand=True)
        self.tree.bind("<<TreeviewSelect>>", self._selected)
        self.v_pick = tk.StringVar()
        tk.Label(body, textvariable=self.v_pick, font=F["CAP"], fg=C["MUTED"], bg=C["PANEL"], bd=0, justify="left",
                 anchor="w").pack(fill="x", pady=(P(8), 0))
        options = tk.Frame(body, bg=C["PANEL"]); options.pack(fill="x", pady=(P(8), 0))
        self.v_enabled = tk.BooleanVar(value=True)
        ttk.Checkbutton(options, text="검증 경로에 쓰기(의심 글자가 든 자리를 AI 검증에 보탬)", variable=self.v_enabled,
                        command=self.toggle).pack(side="left")
        edit = tk.Frame(body, bg=C["PANEL"]); edit.pack(fill="x", pady=(P(10), 0))
        self.b_exclude = ttk.Button(edit, text="선택 쌍 제외", command=self.exclude, cursor="hand2")
        self.b_exclude.pack(side="left")
        ttk.Button(edit, text="제외 모두 되살리기", command=self.restore, cursor="hand2").pack(side="left", padx=(P(6), 0))
        ttk.Button(edit, text="장부 비우기…", command=self.clear, cursor="hand2").pack(side="left", padx=(P(20), 0))
        tk.Frame(body, bg=C["LINE"], height=max(1, P(1))).pack(fill="x", pady=(P(14), P(10)))
        crops = tk.Frame(body, bg=C["PANEL"]); crops.pack(fill="x")
        self.v_samples = tk.BooleanVar(value=True)
        ttk.Checkbutton(crops, text="글자 이미지 조각 모으기(엔진 미세조정용 · 이 PC에만 보관)", variable=self.v_samples,
                        command=self.toggle_samples).pack(side="left")
        ttk.Button(crops, text="조각 지우기…", command=self.clear_samples, cursor="hand2").pack(side="right")
        ttk.Button(crops, text="조각 내보내기…", command=self.export_samples, cursor="hand2").pack(side="right", padx=(0, P(6)))
        self.v_samples_note = tk.StringVar()
        tk.Label(body, textvariable=self.v_samples_note, font=F["CAP"], fg=C["MUTED"], bg=C["PANEL"], bd=0, justify="left",
                 anchor="w").pack(fill="x", pady=(P(6), 0))
        tk.Frame(body, bg=C["LINE"], height=max(1, P(1))).pack(fill="x", pady=(P(14), P(10)))
        foot = tk.Frame(body, bg=C["PANEL"]); foot.pack(fill="x")
        self.b_close = ttk.Button(foot, text="닫기", command=self.destroy, style="DlgInkDef.TButton", cursor="hand2")
        self.b_close.pack(side="right")
        app._lab(foot, "모델 호출 없음 · 글자를 바꾸지 않음 · 비교·교정 창에서 저장하거나 로컬 우선 인식이 끝나면 쌓입니다.",
                 "CAP", "MUTED", wrap=True).pack(side="left", fill="x", expand=True, padx=(0, P(10)))
        self.bind("<Escape>", lambda e: (self.destroy(), "break")[1])
        self.bind("<Return>", lambda e: (self.destroy(), "break")[1])
        self._refresh()
        try:
            from .gui import place_over
            self.geometry(f"{P(760)}x{P(700)}"); place_over(self, app)
        except Exception:
            pass
        self.b_close.focus_set()
        self.grab_set()

    def _refresh(self):
        from .confusion_memory import LearnedConfusions, MIN_RATE, MIN_WEIGHT
        self.learned = LearnedConfusions.load()
        self.v_enabled.set(self.learned.enabled)
        self.tree.delete(*self.tree.get_children())
        self.items = {}
        for row in self.learned.rows:
            for actual, weight, counts, examples in row.alternatives:
                iid = str(len(self.items))
                self.items[iid] = (row, actual, examples)
                grades = "·".join(str(counts.get(g, 0)) for g in ("human", "ai_confirmed", "ai_auto"))
                self.tree.insert("", "end", iid=iid, tags=("odd",) if len(self.items) % 2 == 0 else (),
                                 values=(row.engine, row.read, "→", actual, f"{weight:g}", grades,
                                         f"{row.rate:.1%}" if row.seen else "-", "예" if row.risky else ""))
        risky = sum(r.risky for r in self.learned.rows)
        app = self.app
        parts = [(f"의심 글자 {risky}개 · 쌍 {len(self.items)}개", "bold"),
                 (f" — 가중 오류 {MIN_WEIGHT:g} 이상이고 그 엔진이 읽은 횟수의 {MIN_RATE:.0%} 이상 틀린 글자를 의심 글자로 봅니다. "
                  "가중치: 사람 교정 1 · 사람이 고르거나 검수한 기계 제안 0.7 · 자동 반영 0.3.", "")]
        if self.learned.warning:
            parts.append((" " + self.learned.warning, "warn"))
        app._set_rich(self.intro, parts)
        self._selected()
        self._refresh_samples()

    def _refresh_samples(self):
        from .glyph_samples import stats
        try:
            info = stats()
        except OSError as exc:
            self.v_samples_note.set(f"글자 조각 목록을 읽지 못했습니다: {exc}")
            return
        self.v_samples.set(info["enabled"])
        kinds = info["by_kind"]
        self.v_samples_note.set(f"문서 {info['documents']}개 · 고친 줄 {kinds.get('corrected', 0)}개 · 검수한 그대로의 줄 "
                                f"{kinds.get('verified', 0)}개 · {info['bytes'] / 1_000_000:.1f} MB · {info['root']}")

    def toggle_samples(self):
        from .glyph_samples import set_enabled
        self._apply(set_enabled, bool(self.v_samples.get()))

    def export_samples(self):
        from .glyph_samples import export
        folder = filedialog.askdirectory(parent=self, title="글자 조각을 내보낼 폴더")
        if not folder:
            return
        target = Path(folder) / ("연어OCR_글자조각_" + time.strftime("%Y%m%d-%H%M%S"))
        try:
            done = export(target)
        except (OSError, ValueError) as exc:
            messagebox.showerror(self.title(), f"글자 조각을 내보내지 못했습니다.\n{exc}", parent=self)
            return
        messagebox.showinfo(self.title(), f"{done['exported']}개를 내보냈습니다(사람이 고쳤거나 고르거나 검수한 것).\n{target}\n"
                            "images/ · rec_gt.txt(PaddleOCR 인식 학습 형식) · samples.jsonl", parent=self)

    def clear_samples(self):
        from .glyph_samples import reset
        if messagebox.askyesno(self.title(), "모은 글자 조각을 모두 지울까요?\n교정본·비교 프로젝트·혼동 장부는 그대로입니다.", parent=self):
            self._apply(reset)

    def _pick(self):
        selected = self.tree.selection()
        return self.items.get(selected[0]) if selected else None

    def _selected(self, *_):
        picked = self._pick()
        if picked is None:
            self.v_pick.set("줄을 고르면 그 자리의 보기를 보입니다. 잘못 쌓인 쌍은 [선택 쌍 제외]로 뺍니다(다시 쌓이지 않음).")
            return
        row, actual, examples = picked
        self.v_pick.set(f"{row.engine}: {row.read} → {actual} · 읽은 횟수 {row.seen}"
                        + (" · 보기 " + "  ".join(examples) if examples else ""))

    def _apply(self, action, *args):
        try:
            action(*args)
        except (OSError, ValueError) as exc:
            messagebox.showerror(self.title(), f"엔진 혼동 장부를 바꾸지 못했습니다.\n{exc}", parent=self)
            return False
        self._refresh()
        return True

    def toggle(self):
        from .confusion_memory import set_enabled
        self._apply(set_enabled, bool(self.v_enabled.get()))

    def exclude(self):
        from .confusion_memory import suppress
        picked = self._pick()
        if picked is not None:
            row, actual, _ = picked
            self._apply(suppress, row.engine, row.read, actual)

    def restore(self):
        from .confusion_memory import unsuppress
        if not self.learned.suppressed:
            return
        for engine, read, actual in sorted(self.learned.suppressed):
            if not self._apply(unsuppress, engine, read, actual):
                return

    def clear(self):
        from .confusion_memory import reset
        if messagebox.askyesno(self.title(), "엔진 혼동 장부를 비울까요?\n교정본과 비교 프로젝트는 그대로이며, 다음에 저장하거나 인식하면 다시 쌓입니다.",
                               parent=self):
            self._apply(reset)
