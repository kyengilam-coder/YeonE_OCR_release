"""Hanja rule editor. Changes affect output documents, never raw OCR/history."""
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from pathlib import Path

from .hanja_rules import MODES, RULE_GROUPS, CONTEXT_HELP, rule_group, default_hanja_rules, load_hanja_rules, save_hanja_rules, validate_hanja_rules, import_msr_hanja_rules


class HanjaRulesDialog(tk.Toplevel):
    def __init__(self, app):
        path = app.v_hanja_rules.get().strip()
        rows = load_hanja_rules(path) if path else default_hanja_rules()
        super().__init__(app)
        self.app, self.rules = app, rows
        self.title('한자 변환 규칙'); self.transient(app)
        P = app.px
        self.geometry(f'{P(850)}x{P(640)}'); self.minsize(P(750), P(540))
        frame = ttk.Frame(self, padding=P(16)); frame.pack(fill='both', expand=True)
        ttk.Label(frame, text='한자 변환 규칙 — 모든 책 공통', font=app.fonts['BODY_B']).pack(anchor='w')
        ttk.Label(frame, text='자동 치환은 일치하는 글자를 바로 바꿉니다. 뜻이 달라질 수 있으면 문맥 확인을 선택하십시오.\n'
                             'HWPX·DOCX 출력에만 적용하며 OCR 원본·교정 이력·문자 PDF는 보존합니다.', wraplength=P(780)).pack(fill='x', pady=P(8))
        top = ttk.Frame(frame); top.pack(fill='x')
        ttk.Label(top, text='찾기').pack(side='left'); self.search = tk.StringVar()
        ttk.Entry(top, textvariable=self.search, width=24).pack(side='left', padx=6)
        self.group=tk.StringVar(value='전체')
        ttk.Combobox(top,textvariable=self.group,values=RULE_GROUPS,state='readonly',width=17).pack(side='left',padx=6)
        ttk.Button(top, text='MSR에서 가져오기…', command=self.import_msr).pack(side='right')
        self.count = tk.StringVar(); ttk.Label(top, textvariable=self.count).pack(side='left', padx=8)
        self.explanation=tk.StringVar(value='문맥 판별 규칙을 고르면 뜻 구분과 보존 예시를 볼 수 있습니다.')
        ttk.Label(frame,textvariable=self.explanation,wraplength=P(780)).pack(fill='x',pady=(8,0))
        table = ttk.Frame(frame); table.pack(fill='both', expand=True, pady=8)
        self.tree = ttk.Treeview(table, columns=('find', 'replace', 'mode'), show='headings', selectmode='browse', height=12)
        for key, label, width in [('find','찾을 글자',200),('replace','바꿀 글자',200),('mode','적용 방법',220)]:
            self.tree.heading(key, text=label); self.tree.column(key, width=P(width), anchor='w')
        scroll = ttk.Scrollbar(table, command=self.tree.yview); scroll.pack(side='right', fill='y')
        self.tree.configure(yscrollcommand=scroll.set); self.tree.pack(fill='both', expand=True)
        fields = ttk.Frame(frame); fields.pack(fill='x')
        self.find, self.replace = tk.StringVar(), tk.StringVar()
        self.mode = tk.StringVar(value=MODES['auto'])
        self.entries = []
        for label, var in [('찾을 글자',self.find),('바꿀 글자',self.replace)]:
            ttk.Label(fields, text=label).pack(side='left', padx=(0,5))
            entry = ttk.Entry(fields, textvariable=var, width=13); entry.pack(side='left', padx=(0,10)); self.entries.append(entry)
        ttk.Combobox(fields, textvariable=self.mode, values=list(MODES.values()), state='readonly', width=22).pack(side='left')
        buttons = ttk.Frame(frame); buttons.pack(fill='x', pady=10)
        for label, callback in [('추가',self.add),('선택 수정',self.update_rule),('선택 삭제',self.remove),
                                ('위로',lambda:self.move(-1)),('아래로',lambda:self.move(1))]:
            ttk.Button(buttons, text=label, command=callback).pack(side='left', padx=(0,6))
        ttk.Label(frame, text='삭제한 규칙은 사용자 규칙에서 제외됩니다. 간체→번체 사전 변환은 별도 옵션입니다.\n'
                             '문맥 판별 규칙에서 云/雲·松/鬆·干/乾/幹·着/著·极/極·胜/勝·于/於·咸/鹹·叶/葉 및 기존 확인 후보를 함께 관리합니다.\n'
                             '확실한 뜻은 정리하고, 애매한 뜻은 기존 문맥 판별 설정을 따르며 판별이 없으면 원문·검수 표시를 유지합니다.', wraplength=780).pack(fill='x')
        foot = ttk.Frame(frame); foot.pack(fill='x', pady=(12,0))
        ttk.Button(foot, text='취소', command=self.destroy).pack(side='right')
        ttk.Button(foot, text='파일로 저장하고 적용…', command=self.save).pack(side='right', padx=8)
        self.tree.bind('<<TreeviewSelect>>', self.selected)
        self.search.trace_add('write', lambda *_:self.refresh())
        self.group.trace_add('write',lambda *_:self.refresh())
        self.bind('<Escape>', lambda _e:self.destroy())
        self.refresh(); self.grab_set()
        try:
            from .gui import place_over
            place_over(self, app)
        except tk.TclError:
            pass

    def index(self):
        selected = self.tree.selection()
        return int(selected[0]) if selected else None

    def refresh(self, select=None):
        self.tree.delete(*self.tree.get_children()); query = self.search.get().strip()
        for i, row in enumerate(self.rules):
            group=self.group.get() if hasattr(self,'group') else '전체'
            if (group=='전체' or rule_group(row)==group) and (not query or query in row['find'] or query in row['replace']):
                self.tree.insert('', 'end', iid=str(i), values=(row['find'],row['replace'] or '(지움)',MODES[row['mode']]))
        self.count.set(f'{len(self.tree.get_children())} / {len(self.rules)}개')
        if select is not None and self.tree.exists(str(select)):
            self.tree.selection_set(str(select)); self.tree.see(str(select))

    def selected(self, *_):
        index = self.index()
        if index is not None:
            row = self.rules[index]; self.find.set(row['find']); self.replace.set(row['replace']); self.mode.set(MODES[row['mode']])
            if hasattr(self,'explanation'):self.explanation.set(CONTEXT_HELP.get(row['mode'],'일치하는 글자를 자동으로 바꾸는 규칙입니다. 뜻 구분이 필요하면 문맥 판별로 지정하십시오.'))

    def edited(self, index=None):
        mode = next((k for k,v in MODES.items() if v == self.mode.get()),'review')
        number = self.rules[index]['id'] if index is not None else max((r['id'] for r in self.rules),default=1_000_000)+1
        row = dict(id=number, find=self.find.get(), replace=self.replace.get(), mode=mode)
        try:
            trial = [r for i,r in enumerate(self.rules) if i != index] + [row]
            validate_hanja_rules(trial)
            return row
        except ValueError as exc:
            messagebox.showerror(self.title(), str(exc), parent=self)

    def add(self):
        row = self.edited()
        if row is not None:
            self.rules.append(row); self.group.set(rule_group(row)); self.search.set(''); self.refresh(len(self.rules)-1)

    def update_rule(self):
        index = self.index()
        if index is not None:
            row = self.edited(index)
            if row is not None:self.rules[index] = row; self.group.set(rule_group(row)); self.search.set(''); self.refresh(index)

    def remove(self):
        index = self.index()
        if index is not None:self.rules.pop(index); self.refresh(min(index,len(self.rules)-1))

    def move(self, delta):
        index = self.index()
        if index is not None and 0 <= index+delta < len(self.rules):
            self.rules[index],self.rules[index+delta] = self.rules[index+delta],self.rules[index]; self.refresh(index+delta)

    def import_msr(self):
        path = filedialog.askopenfilename(parent=self, title='한자 치환 MSR 가져오기(실행하지 않음)', filetypes=[('한글 매크로','*.msr')])
        if not path:return
        try:self.rules = import_msr_hanja_rules(path); self.search.set(''); self.refresh()
        except (ValueError,OSError) as exc:messagebox.showerror(self.title(),str(exc),parent=self)

    def save(self):
        current = self.app.v_hanja_rules.get().strip()
        path = filedialog.asksaveasfilename(parent=self, title='한자 변환 규칙 저장', defaultextension='.json',
                                          initialfile=Path(current).name if current else '한자변환_내규칙.json', filetypes=[('JSON 규칙','*.json')])
        if not path:return
        try:
            saved = save_hanja_rules(path,self.rules)
            self.app.v_hanja_rules.set(str(saved)); self.app.save_settings(quiet=True)
        except (ValueError,OSError) as exc:
            messagebox.showerror(self.title(),f'저장하지 못했습니다.\n{exc}',parent=self); return
        self.destroy()
