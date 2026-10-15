"""Editable, ordered literal punctuation rules for document output."""
import json
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk


class PunctuationRulesDialog(tk.Toplevel):
    def __init__(self, app):
        from .normalization import default_punctuation_rules, load_punctuation_rules
        path = app.v_punctuation_rules.get().strip()
        rules = load_punctuation_rules(path) if path else default_punctuation_rules()
        super().__init__(app)
        self.app = app
        self.title("중문부호 수정 규칙")
        self.transient(app)
        self.minsize(620, 420)
        self.rules = rules
        body = ttk.Frame(self, padding=16); body.pack(fill="both", expand=True)
        ttk.Label(body, text="위에서 아래 순서대로 찾은 부호를 바꿉니다. 규칙을 추가·수정한 뒤 파일로 저장해 적용하십시오.",
                  wraplength=650).pack(fill="x", pady=(0, 10))
        table = ttk.Frame(body); table.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(table, columns=("find", "replace"), show="headings", selectmode="browse", height=12)
        self.tree.heading("find", text="찾을 부호"); self.tree.heading("replace", text="바꿀 부호")
        self.tree.column("find", width=260); self.tree.column("replace", width=260)
        sb = ttk.Scrollbar(table, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y"); self.tree.pack(side="left", fill="both", expand=True)
        self.tree.bind("<<TreeviewSelect>>", self._selected)
        fields = ttk.Frame(body); fields.pack(fill="x", pady=10)
        self.find = tk.StringVar(); self.replace = tk.StringVar()
        ttk.Label(fields, text="찾을 부호").pack(side="left")
        ttk.Entry(fields, textvariable=self.find, width=18).pack(side="left", padx=(6, 16))
        ttk.Label(fields, text="바꿀 부호").pack(side="left")
        ttk.Entry(fields, textvariable=self.replace, width=18).pack(side="left", padx=6)
        ttk.Label(body, text="빈 바꿀 부호는 삭제입니다. 공백도 그대로 적용하며, 정규식이나 매크로는 실행하지 않습니다.",
                  wraplength=650).pack(fill="x")
        edit = ttk.Frame(body); edit.pack(fill="x", pady=10)
        for label, command in (("추가", self.add), ("선택 수정", self.update_rule), ("선택 삭제", self.remove),
                               ("위로", lambda: self.move(-1)), ("아래로", lambda: self.move(1))):
            ttk.Button(edit, text=label, command=command).pack(side="left", padx=(0, 6))
        actions = ttk.Frame(body); actions.pack(fill="x")
        ttk.Button(actions, text="취소", command=self.destroy).pack(side="right")
        ttk.Button(actions, text="파일로 저장하고 적용…", command=self.save).pack(side="right", padx=8)
        self._refresh()
        self.grab_set()

    def _index(self):
        selected = self.tree.selection()
        return int(selected[0]) if selected else None

    def _refresh(self, select=None):
        self.tree.delete(*self.tree.get_children())
        for i, rule in enumerate(self.rules):
            # repr makes whitespace and empty replacement values visible.
            self.tree.insert("", "end", iid=str(i), values=(repr(rule["find"]), repr(rule["replace"])))
        if select is not None and 0 <= select < len(self.rules):
            self.tree.selection_set(str(select)); self.tree.see(str(select))

    def _selected(self, *_):
        index = self._index()
        if index is not None:
            self.find.set(self.rules[index]["find"])
            self.replace.set(self.rules[index]["replace"])

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
