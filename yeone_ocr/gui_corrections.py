"""Editable, ordered literal punctuation rules for document output.

창 모양은 디자인 명세 §6.8(F_rules): 서재 바탕의 작은 창, 표(순서 · 찾을 부호 · → · 바꿀 부호 — 공백은 '␣', 빈 값은 '(지움)'),
고른 줄 번호를 칸 옆에, 바닥 [파일로 저장하고 적용…](먹 채움 · 기본 고리 — 돈이 들지 않으므로 인주를 쓰지 않음)·[취소].
Enter 는 초점이 단추면 그 단추, 찾을·바꿀 부호 칸이면 아무것도 하지 않고(칸의 Enter 가 저장 창을 띄우지 않게), 그 밖에는 기본 단추. Esc 는 [취소]."""
import json
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

SPACE = "␣"            # 공백 보이기(U+2423) — 저장 값은 그대로
CLEARED = "(지움)"      # 빈 바꿀 부호(그 부호를 지움)


def shown(value: str) -> str:
    """표에 보일 글: 공백은 '␣', 빈 값은 '(지움)'(지난 판의 repr 따옴표·이스케이프 대신)."""
    return CLEARED if value == "" else value.replace(" ", SPACE)


class PunctuationRulesDialog(tk.Toplevel):
    def __init__(self, app):
        from .normalization import default_punctuation_rules, load_punctuation_rules
        path = app.v_punctuation_rules.get().strip()
        rules = load_punctuation_rules(path) if path else default_punctuation_rules()
        super().__init__(app)
        self.app = app
        C, P, F = app.colors, app.px, app.fonts
        self.title("중문부호 수정 규칙")
        self.transient(app)
        self.configure(bg=C["PANEL"], highlightthickness=1, highlightbackground=C["RULE"])
        self.minsize(P(640), P(520))
        self.rules = rules
        self.source = Path(path).name if path else "내장 기본 규칙"
        body = tk.Frame(self, bg=C["PANEL"], padx=P(22), pady=P(16)); body.pack(fill="both", expand=True)
        head = tk.Frame(body, bg=C["PANEL"]); head.pack(fill="x")
        tk.Label(head, text="중문부호 수정 규칙", font=F["NUM"], fg=C["INK"], bg=C["PANEL"], bd=0).pack(side="left")
        ttk.Label(head, text="자동 교정 · 모든 책 공통", style="Chip.TLabel").pack(side="right")
        self.intro = app._rich_line(body)
        self.intro.pack(fill="x", pady=(P(10), P(8)))
        table = tk.Frame(body, bg=C["PANEL"]); table.pack(fill="both", expand=True)
        st = ttk.Style(self)
        st.configure("Rules.Treeview", font=F["MONO_L"], rowheight=P(27))
        self.tree = ttk.Treeview(table, columns=("n", "find", "to", "replace"), show="headings", selectmode="browse", height=9, style="Rules.Treeview")
        for col, text, width, anchor, stretch in (("n", "순서", 52, "e", False), ("find", "찾을 부호", 200, "w", True), ("to", "", 36, "center", False),
                                                  ("replace", "바꿀 부호", 200, "w", True)):
            self.tree.heading(col, text=text, anchor=anchor)
            self.tree.column(col, width=P(width), minwidth=P(min(width, 36)), anchor=anchor, stretch=stretch)
        self.tree.tag_configure("odd", background=C["STRIPE"])
        sb = ttk.Scrollbar(table, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y", padx=(P(4), 0)); self.tree.pack(side="left", fill="both", expand=True)
        self.tree.bind("<<TreeviewSelect>>", self._selected)
        fields = tk.Frame(body, bg=C["PANEL"]); fields.pack(fill="x", pady=(P(10), 0))
        self.find = tk.StringVar(); self.replace = tk.StringVar()
        self.entries = []
        for i, (label, var) in enumerate((("찾을 부호", self.find), ("바꿀 부호", self.replace))):
            tk.Label(fields, text=label, font=F["BODY"], fg=C["MUTED"], bg=C["PANEL"], bd=0).pack(side="left", padx=(P(10) if i else 0, P(8)))
            e = ttk.Entry(fields, textvariable=var, width=9, font=F["MONO_L"]); e.pack(side="left")
            self.entries.append(e)
        self.v_pick = tk.StringVar()
        tk.Label(fields, textvariable=self.v_pick, font=F["CAP"], fg=C["MUTED"], bg=C["PANEL"], bd=0, justify="left", anchor="w").pack(
            side="left", fill="x", expand=True, padx=(P(12), 0))
        edit = tk.Frame(body, bg=C["PANEL"]); edit.pack(fill="x", pady=(P(10), 0))
        for label, command, gap in (("추가", self.add, 0), ("선택 수정", self.update_rule, 6), ("선택 삭제", self.remove, 6),
                                    ("위로", lambda: self.move(-1), 20), ("아래로", lambda: self.move(1), 6)):
            ttk.Button(edit, text=label, command=command, cursor="hand2").pack(side="left", padx=(P(gap), 0))
        tk.Frame(body, bg=C["LINE"], height=max(1, P(1))).pack(fill="x", pady=(P(14), P(10)))
        foot = tk.Frame(body, bg=C["PANEL"]); foot.pack(fill="x")
        self.b_cancel = ttk.Button(foot, text="취소", command=self.destroy, style="Dlg.TButton", cursor="hand2")
        self.b_cancel.pack(side="right")
        self.b_save = ttk.Button(foot, text="파일로 저장하고 적용…", command=self.save, style="DlgInkDef.TButton", cursor="hand2")
        self.b_save.pack(side="right", padx=(0, P(6)))
        app._lab(foot, "모델 호출 없음 · 저장한 파일이 자동 교정의 중문부호 규칙이 됩니다(설정에 경로 저장).", "CAP", "MUTED", wrap=True).pack(
            side="left", fill="x", expand=True, padx=(0, P(10)))                               # 남은 너비에 맞춰 줄바꿈(단추 밑으로 들어가지 않게)
        self.bind("<Return>", self._on_return); self.bind("<KP_Enter>", self._on_return)
        self.bind("<Escape>", lambda e: (self.destroy(), "break")[1])
        self._refresh()
        try:
            from .gui import place_over
            self.geometry(f"{P(640)}x{P(560)}"); place_over(self, app)
        except Exception:
            pass
        self.b_save.focus_set()
        self.grab_set()

    def _on_return(self, e=None):
        """Enter(명세 §6.8 — 비용 창 §10-4 와 같은 처리): 초점이 단추면 그 단추, 찾을·바꿀 부호 칸이면 아무것도 하지 않음, 그 밖에는 기본 단추(파일로 저장하고 적용…)."""
        w = self.focus_get()
        if w in self.entries:
            return "break"
        if isinstance(w, ttk.Button):
            w.invoke(); return "break"
        self.b_save.invoke()
        return "break"

    def _index(self):
        selected = self.tree.selection()
        return int(selected[0]) if selected else None

    def _refresh(self, select=None):
        self.tree.delete(*self.tree.get_children())
        for i, rule in enumerate(self.rules):
            self.tree.insert("", "end", iid=str(i), values=(i + 1, shown(rule["find"]), "→", shown(rule["replace"])), tags=("odd",) if i % 2 else ())
        if select is not None and 0 <= select < len(self.rules):
            self.tree.selection_set(str(select)); self.tree.see(str(select))
        app = self.app
        app._set_rich(self.intro, [("위에서 아래 순서대로 찾은 부호를 바꿉니다. 규칙을 추가·수정한 뒤 파일로 저장해 적용하십시오. ", ""),
                                   (f"지금: {self.source} {len(self.rules)}개 · 정규식이나 매크로는 실행하지 않습니다.", "muted")])
        self.intro.tag_configure("muted", foreground=app.colors["MUTED"])
        self._pick_note()

    def _pick_note(self):
        index = self._index()
        rest = "빈 바꿀 부호는 삭제 · 공백도 그대로"
        self.v_pick.set(f"고른 {index + 1}번 줄 · {rest}" if index is not None else rest)

    def _selected(self, *_):
        index = self._index()
        if index is not None:
            self.find.set(self.rules[index]["find"])
            self.replace.set(self.rules[index]["replace"])
        self._pick_note()

    def _rule(self):
        from .normalization import validate_punctuation_rules
        try:
            return validate_punctuation_rules([{"find": self.find.get(), "replace": self.replace.get()}])[0]
        except ValueError as exc:
            messagebox.showerror(self.title(), str(exc), parent=self)
            return None

    def add(self):
        rule = self._rule()
        if rule is not None:
            self.rules.append(rule); self._refresh(len(self.rules) - 1)

    def update_rule(self):
        index = self._index()
        if index is None:
            return
        rule = self._rule()
        if rule is not None:
            self.rules[index] = rule; self._refresh(index)

    def remove(self):
        index = self._index()
        if index is not None:
            self.rules.pop(index); self._refresh(min(index, len(self.rules) - 1))

    def move(self, delta):
        index = self._index()
        if index is not None and 0 <= index + delta < len(self.rules):
            self.rules[index], self.rules[index + delta] = self.rules[index + delta], self.rules[index]
            self._refresh(index + delta)

    def save(self):
        from .normalization import validate_punctuation_rules
        try:
            rules = validate_punctuation_rules(self.rules)
            current = self.app.v_punctuation_rules.get().strip()
            path = filedialog.asksaveasfilename(parent=self, title="중문부호 규칙 저장", defaultextension=".json",
                                              initialfile=Path(current).name if current else "중문부호_규칙.json",
                                              filetypes=[("JSON 규칙", "*.json")])
            if not path:
                return
            Path(path).write_text(json.dumps({"rules": rules}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            self.app.v_punctuation_rules.set(str(Path(path).resolve()))
            self.app.save_settings(quiet=True)
        except (ValueError, OSError) as exc:
            messagebox.showerror(self.title(), f"규칙을 저장하지 못했습니다.\n{exc}", parent=self)
            return
        self.destroy()
