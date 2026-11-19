"""Book-rule generation UI: analyze, review, then explicitly apply."""
import json
from pathlib import Path
import queue
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from .rule_library import rules_dir
from .book_output import LAYOUT_LABELS, select_template


class BookRuleDialog(tk.Toplevel):
    def __init__(self, app, pdf, work_dir, plan='auto'):
        super().__init__(app)
        self.app, self.pdf, self.work_dir = app, pdf, work_dir
        self.result = None
        self.busy = False
        self.stop = threading.Event()
        self.events = queue.Queue()
        self.pages = tk.StringVar(master=self)
        self.template = app.v_template
        self.reference = getattr(app, 'v_reference_hwpx', None) or tk.StringVar(master=self)
        self.layout = getattr(app, 'v_layout', None) or tk.StringVar(master=self, value=LAYOUT_LABELS['flow'])
        self.plan = tk.StringVar(master=self, value=plan if plan in ('auto', 'chatgpt', 'gemini', 'claude') else 'auto')
        self.status = tk.StringVar(master=self)
        self.include_json = tk.BooleanVar(master=self)
        self.preferred_pages = ()
        self.title('이 책 설정')
        self.transient(app)
        from .gui import work_area
        width, height = work_area(self)
        self.geometry(f'{min(740, width - 36)}x{min(600, height - 76)}')
        self.protocol('WM_DELETE_WINDOW', self.close)

        frame = ttk.Frame(self, padding=12)
        frame.pack(fill='both', expand=True)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(1, weight=1)
        ttk.Label(frame, text=Path(pdf).name, wraplength=600).grid(row=0, column=0, sticky='w', pady=(0, 8))
        tabs = ttk.Notebook(frame)
        tabs.grid(row=1, column=0, sticky='nsew')
        materials = ttk.Frame(tabs, padding=12)
        output = ttk.Frame(tabs, padding=12)
        analysis = ttk.Frame(tabs, padding=12)
        tabs.add(materials, text='책 자료')
        tabs.add(output, text='출력 모양')
        tabs.add(analysis, text='구조 분석')
        self._materials(materials)
        self._output(output)
        self._analysis(analysis)
        ttk.Label(frame, textvariable=self.status, wraplength=600).grid(row=2, column=0, sticky='w', pady=4)
        footer = ttk.Frame(frame)
        footer.grid(row=3, column=0, sticky='ew')
        self.save = ttk.Button(footer, text='이 책 설정 저장', command=self.save_selection)
        self.save.pack(side='right')
        self.settings = ttk.Button(footer, text='설정 → 이 책', command=self.open_settings)
        self.settings.pack(side='left')

        self.traces = [(variable, variable.trace_add('write', self.invalidate_result))
                       for variable in (self.template, self.reference, self.pages)]
        pdf_variable = app._input_vars[0]['pdf']
        self.traces.append((pdf_variable, pdf_variable.trace_add('write', self.book_changed)))
        self.poll_id = self.after(100, self.poll)

    def _materials(self, frame):
        ttk.Label(frame, text='원본 PDF').pack(anchor='w')
        ttk.Label(frame, text=self.pdf, wraplength=580).pack(anchor='w', pady=(2, 4))
        self.change_pdf = ttk.Button(frame, text='다른 PDF 선택…', command=self.choose_pdf)
        self.change_pdf.pack(anchor='w')
        ttk.Label(frame, text='참고 OCR HWPX · 이 책의 이전 판독문').pack(anchor='w', pady=(16, 4))
        row = ttk.Frame(frame)
        row.pack(fill='x')
        self.reference_entry = ttk.Entry(row, textvariable=self.reference)
        self.reference_entry.pack(side='left', fill='x', expand=True)
        self.reference_pick = ttk.Button(row, text='고르기…', command=self.pick_reference)
        self.reference_pick.pack(side='right', padx=(6, 0))
        ttk.Label(frame, text='선택한 OCR 작업 폴더').pack(anchor='w', pady=(16, 2))
        ttk.Label(frame, text=str(self.work_dir), wraplength=580).pack(anchor='w')
        self.recheck = ttk.Button(frame, text='기존 OCR 재검수·재판독…', command=self.open_recheck)
        self.recheck.pack(anchor='w', pady=6)

    def _output(self, frame):
        ttk.Label(frame, text='HWPX 용도').pack(anchor='w')
        self.layout_box = ttk.Combobox(frame, textvariable=self.layout, values=list(LAYOUT_LABELS.values()),
                                      state='readonly')
        self.layout_box.pack(fill='x', pady=(2, 8))
        ttk.Label(frame, text='출력 양식 HWPX · 선택하면 편집용 문서에 적용').pack(anchor='w')
        row = ttk.Frame(frame)
        row.pack(fill='x')
        # Only a deliberate edit selects flow; restoring saved settings does not.
        def editing(reason, value):
            if reason == 'key' and value.strip():
                self.layout.set(LAYOUT_LABELS['flow'])
            return True
        self.template_entry = ttk.Entry(row, textvariable=self.template, validate='key',
                                        validatecommand=(self.register(editing), '%V', '%P'))
        self.template_entry.pack(side='left', fill='x', expand=True)
        self.pick = ttk.Button(row, text='고르기…', command=self.pick_template)
        self.pick.pack(side='right', padx=(6, 0))
        self.styles = ttk.Button(frame, text='제목·본문 스타일 연결…', command=self.edit_styles)
        self.styles.pack(anchor='w', pady=6)
        ttk.Label(frame, text='현재 책의 규칙').pack(anchor='w', pady=(8, 2))
        if getattr(self.app, 'v_preset', None) is not None:
            ttk.Label(frame, textvariable=self.app.v_preset).pack(anchor='w')
        self.rules = ttk.Button(frame, text='규칙 선택·관리 ▾', command=self.choose_rule)
        self.rules.pack(anchor='w')
        self.sample = ttk.Button(frame, text='이 책의 HWPX 미리보기', command=self.build_preview)
        self.sample.pack(anchor='w', pady=(14, 2))
        ttk.Label(frame, text='저장된 판독 또는 PDF 글자층에서 최대 3쪽을 현재 양식으로 엽니다.',
                  wraplength=560).pack(anchor='w')
        self.norm = ttk.Button(frame, text='원문 표기·자동 교정 설정…', command=self.open_normalization)
        self.norm.pack(anchor='w', pady=(8, 0))

    def _analysis(self, frame):
        ttk.Label(frame, text='전체 쪽에서 차례·목차·색인·본문·문헌을 찾아 쪽 유형을 규칙에 저장합니다. 출력할 때 목록의 행·열과 쪽번호를 원본에 맞춰 배치합니다. 쪽마다 구독 사용량이 듭니다.',
                  wraplength=570).pack(anchor='w')
        ttk.Label(frame, text='분석 범위 · 비우면 전체, 부분 시험은 최대 12쪽 (예 1-3, 20)').pack(anchor='w', pady=(8, 2))
        self.page_entry = ttk.Entry(frame, textvariable=self.pages)
        self.page_entry.pack(fill='x')
        row = ttk.Frame(frame)
        row.pack(fill='x', pady=6)
        ttk.Label(row, text='구독 계정 우선순위').pack(side='left')
        self.plan_box = ttk.Combobox(row, textvariable=self.plan, state='readonly', width=12,
                                    values=('auto', 'chatgpt', 'gemini', 'claude'))
        self.plan_box.pack(side='left', padx=6)
        self.manual = ttk.Button(row, text='수동 검토 자료…', command=lambda: self.app.make_book_rule_manual())
        self.manual.pack(side='right')
        row = ttk.Frame(frame)
        row.pack(fill='x')
        self.generate = ttk.Button(row, text='AI로 책별 규칙 만들기', command=self.start)
        self.generate.pack(side='left')
        self.cancel = ttk.Button(row, text='중지', state='disabled', command=self.cancel_generation)
        self.cancel.pack(side='left', padx=4)
        self.apply = ttk.Button(row, text='확인한 규칙 저장·적용', state='disabled', command=self.apply_result)
        self.apply.pack(side='left')
        ttk.Checkbutton(frame, text='규칙 JSON 보기', variable=self.include_json,
                        command=self.show_result).pack(anchor='w', pady=4)
        area = ttk.Frame(frame)
        area.pack(fill='both', expand=True)
        self.preview = tk.Text(area, wrap='word', height=6, state='disabled')
        self.preview.pack(side='left', fill='both', expand=True)
        scrollbar = ttk.Scrollbar(area, command=self.preview.yview)
        scrollbar.pack(side='right', fill='y')
        self.preview.configure(yscrollcommand=scrollbar.set)

    def book_changed(self, *_):
        if self.app._input_vars[0]['pdf'].get().strip() != self.pdf:
            self.close()

    def open_settings(self):
        if self.save_selection():
            self.close()
            self.app.open_book_settings()

    def open_normalization(self):
        if self.save_selection():
            self.close()
            self.app.select_tab(3)
            self.app._set_pick('auto')
            card = self.app._set_sections['auto']['card']
            self.app.after_idle(lambda: self.app._reveal(card))

    def open_recheck(self):
        if self.save_selection():
            self.close()
            self.app.do_recheck()

    def invalidate_result(self, *_):
        if self.result is not None and not self.busy:
            self.result = None
            self.show_result()
            self.apply.configure(state='disabled')
            self.status.set('자료 선택이 변경되었습니다. 현재 자료로 규칙을 다시 생성하십시오.')

    def pick_template(self):
        path = filedialog.askopenfilename(parent=self, title='출력 양식 HWPX', filetypes=[('HWPX', '*.hwpx')])
        if path:
            select_template(self.template, self.layout, path)
            self.app._suggest_preset(path)

    def pick_reference(self):
        path = filedialog.askopenfilename(parent=self, title='같은 책의 참고 OCR HWPX', filetypes=[('HWPX', '*.hwpx')])
        if path:
            self.reference.set(path)

    def save_selection(self):
        if self.busy or self.app.running or self.app._input_vars[0]['pdf'].get().strip() != self.pdf:
            messagebox.showinfo(self.title(), '원래 PDF를 선택하고 진행 중인 작업이 끝난 뒤 저장하십시오.', parent=self)
            return False
        try:
            template, reference = self.template.get().strip(), self.reference.get().strip()
            if template:
                from .rule_request import template_summary
                template_summary(template)
            if reference:
                from .reference_ocr import ReferenceOCR
                ReferenceOCR(reference)
        except Exception as exc:
            messagebox.showerror(self.title(), '선택한 책 자료를 읽을 수 없습니다: ' + str(exc), parent=self)
            return False
        if self.app.v_template.get() != template:
            self.app.v_template.set(template)
        reference_variable = getattr(self.app, 'v_reference_hwpx', None)
        if reference_variable is not None and reference_variable.get() != reference:
            reference_variable.set(reference)
        if self.app._save_book(create=True) is False:
            self.status.set('책 설정을 저장하지 못했습니다. 진행 기록의 저장 경로·오류를 확인하십시오.')
            return False
        self.status.set('이 책의 자료와 출력 설정을 저장했습니다.')
        return True

    def reload_selection(self):
        self.template.set(self.app.v_template.get().strip())

    def choose_rule(self):
        if self.app.running or self.app._input_vars[0]['pdf'].get().strip() != self.pdf:
            messagebox.showinfo(self.title(), '원래 PDF를 선택하고 작업이 끝난 뒤 규칙을 고르십시오.', parent=self)
            return
        self.app.book_rules_menu(self.rules, on_selected=self.reload_selection)

    def edit_styles(self):
        if self.save_selection():
            self.app.edit_book_styles()

    def choose_pdf(self):
        if self.save_selection():
            self.close()
            self.app.choose_pdf()
            self.app.make_book_rule()

    def set_busy(self, busy):
        self.busy = busy
        for button in (self.generate, self.pick, self.manual, self.page_entry, self.template_entry,
                       self.reference_entry, self.reference_pick, self.save, self.rules, self.styles,
                       self.change_pdf, self.settings, self.recheck, self.sample, self.norm):
            button.configure(state='disabled' if busy else 'normal')
        self.plan_box.configure(state='disabled' if busy else 'readonly')
        self.layout_box.configure(state='disabled' if busy else 'readonly')
        self.cancel.configure(state='normal' if busy else 'disabled')
        self.apply.configure(state='normal' if not busy and self.result else 'disabled')

    def build_preview(self):
        if self.busy or self.app.running:
            return
        from .book_output import preview_document
        options = self.app._build_opts()
        self.stop.clear()
        self.set_busy(True)
        self.cancel.configure(state='disabled')
        self.status.set('대표 쪽으로 HWPX 미리보기를 만드는 중입니다.')
        def worker():
            try:
                path, origin, numbers = preview_document(self.pdf, self.work_dir, options, self.preferred_pages)
                if self.stop.is_set():
                    path.unlink(missing_ok=True)
                else:
                    self.events.put(('sample', (path, origin, numbers, options)))
            except Exception as exc:
                self.events.put(('error', str(exc)))
        threading.Thread(target=worker, name='book-output-preview', daemon=True).start()

    def start(self):
        if self.busy:
            return
        if self.app.running or self.app._input_vars[0]['pdf'].get().strip() != self.pdf:
            messagebox.showinfo(self.title(), '원래 PDF를 선택하고 진행 중인 작업이 끝난 뒤 규칙을 생성하십시오.', parent=self)
            return
        from .rule_generation import generate_rule
        self.result = None
        self.show_result()
        self.stop.clear()
        self.set_busy(True)
        pages, template, plan = self.pages.get(), self.template.get().strip(), self.plan.get()
        reference = self.reference.get().strip()
        self.analysis_selection = (template, reference, pages)
        gemini_model = self.app.cfg.get('gemini_subscription_model') or None
        self.status.set('AI 자동 생성을 준비하고 있습니다…')
        def worker():
            try:
                result = generate_rule(self.pdf, template, pages, self.work_dir, plan=plan,
                                       gemini_model=gemini_model, stop=self.stop.is_set,
                                       cache_dir=rules_dir() / 'analysis_cache',
                                       reference_hwpx=reference,
                                       progress=lambda message: self.events.put(('status', message)))
                self.events.put(('done', result))
            except Exception as exc:
                self.events.put(('error', str(exc)))
        threading.Thread(target=worker, daemon=True).start()

    def poll(self):
        try:
            while True:
                kind, value = self.events.get_nowait()
                if kind == 'status':
                    self.status.set(value)
                elif kind == 'error':
                    self.set_busy(False)
                    self.status.set('생성 중단: ' + value)
                elif kind == 'sample':
                    from .gui import open_path
                    self.set_busy(False)
                    path, origin, numbers, options = value
                    if self.app._build_opts() != options:
                        path.unlink(missing_ok=True)
                        self.status.set('출력 설정이 바뀌었습니다. 미리보기를 다시 여십시오.')
                        continue
                    self.status.set(f'{origin} · PDF {", ".join(map(str, numbers))}쪽 미리보기')
                    try:
                        open_path(path)
                    except OSError:
                        messagebox.showinfo(self.title(), f'미리보기 파일을 열 수 있는 앱을 선택하십시오.\n{path}', parent=self)
                elif kind == 'done':
                    if getattr(self, 'analysis_selection', None) not in (None, (
                            self.template.get().strip(), self.reference.get().strip(), self.pages.get())):
                        self.result = None
                        self.set_busy(False)
                        self.status.set('분석 도중 책 자료가 변경되었습니다. 현재 자료로 다시 생성하십시오.')
                        continue
                    if self.stop.is_set():
                        self.set_busy(False)
                        self.status.set('생성을 중지했습니다. 규칙은 적용하지 않았습니다.')
                        continue
                    self.result = value
                    kinds = value.get('type_pages', {})
                    self.preferred_pages = tuple(dict.fromkeys(kinds[role][0] for role in
                        ('차례', '목차', '색인', '본문', '각주', '문헌 목록', '제목') if kinds.get(role)))
                    self.show_result()
                    self.status.set('생성 완료. 분석 범위·확인 항목·규칙을 검토한 뒤 저장·적용하십시오.')
                    self.set_busy(False)
        except queue.Empty:
            pass
        if self.winfo_exists():
            self.poll_id = self.after(100, self.poll)

    def show_result(self):
        self.preview.configure(state='normal')
        self.preview.delete('1.0', 'end')
        if self.result is not None:
            value = self.result
            coverage = value['coverage']
            scope = '전체' if coverage.get('selection') == 'whole_book' else '부분 시험 · 전체'
            text = [f"{scope} {coverage['page_count']}쪽 중 {len(coverage['selected_pages'])}쪽 분석",
                    value['summary'], '출력에 적용할 규칙', ', '.join(
                        label for key, label in (('entry', '항목 제목'), ('commentary', '주석'),
                        ('source', '출전'), ('emphasis', '굵기'), ('styles', '스타일 연결'),
                        ('citation', '인용'), ('headings', '제목 단계'), ('page_types', '쪽 유형과 원본 목록 배치')) if value['rule'].get(key)
                    ) or '기본 문단 규칙', '확인할 항목', '\n'.join(value['warnings']) or '없음']
            types = [f"{kind} · PDF {', '.join(map(str, numbers))}쪽"
                     for kind, numbers in value.get('type_pages', {}).items() if numbers]
            if types:
                text.extend(['발견한 쪽 유형', '\n'.join(types)])
            if self.include_json.get():
                text.extend(['규칙 JSON', json.dumps(value['rule'], ensure_ascii=False, indent=2)])
            self.preview.insert('1.0', '\n\n'.join(text))
        self.preview.configure(state='disabled')

    def cancel_generation(self):
        self.stop.set()
        self.status.set('중지 요청 중… 완료 전까지 규칙을 적용하지 않습니다.')
        self.cancel.configure(state='disabled')

    def apply_result(self):
        if self.busy or not self.result:
            return
        if self.app.running or self.app._input_vars[0]['pdf'].get().strip() != self.pdf:
            messagebox.showinfo(self.title(), '선택한 PDF가 바뀌었거나 OCR이 실행 중입니다. 원래 PDF를 선택하고 작업이 끝난 뒤 적용하십시오.', parent=self)
            return
        result = self.result
        template = result['rule']['template']
        if template and not Path(template).is_file():
            messagebox.showerror(self.title(), '생성에 사용한 양식 파일이 없습니다. 양식을 다시 선택해 생성하십시오.', parent=self)
            return
        from .rule_generation import save_generated
        try:
            path = save_generated(result, rules_dir())
            self.app.use_book_rule(str(path))
            reference_variable = getattr(self.app, 'v_reference_hwpx', None)
            if reference_variable is not None:
                reference_variable.set(result['inputs'].get('reference_hwpx', ''))
                self.app._save_book(create=True)
            self.reload_selection()
        except (OSError, ValueError) as exc:
            messagebox.showerror(self.title(), '규칙 저장 실패: ' + str(exc), parent=self)
            return
        self.status.set('새 규칙을 저장하고 이 책에 적용했습니다: ' + path.name)
        self.result = None
        self.apply.configure(state='disabled')

    def close(self):
        self.stop.set()
        self.after_cancel(self.poll_id)
        for variable, trace_id in self.traces:
            variable.trace_remove('write', trace_id)
        self.traces.clear()
        self.destroy()
