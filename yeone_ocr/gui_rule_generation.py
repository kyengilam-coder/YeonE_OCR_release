"""Analyze the selected PDF, preview its layout, and build both outputs."""
import json
from pathlib import Path
import queue
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from .rule_library import rules_dir
from .book_output import LAYOUT_LABELS, select_template
from .hwpx_output_rules import (CONVERSION_LABELS, CONVERSION_RULES, DEFAULTS as HWPX_DEFAULTS,
                                RULES as HWPX_OUTPUT_RULES, cli_flags, conversion_flags)

BOOKMARK_LABELS = {'headings': '제목마다 하나', 'none': '모두 빼기'}


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
        self.build_after_analysis = False
        from .gui import App
        self.companion = tk.BooleanVar(master=self, value=App._companion_pdf(app))
        review = vars(app).get('_book_source_review')
        self.source_review = tk.BooleanVar(master=self, value=True if review is None else review)
        self.bookmarks = tk.StringVar(master=self, value=BOOKMARK_LABELS.get(
            vars(app).get('_book_hwpx_bookmarks'), BOOKMARK_LABELS['headings']))
        self.preferred_pages = ()
        values = dict(getattr(app, '_hwpx_output_rules', {}))
        values.setdefault('hwpx_review_letters', True)
        if hasattr(app, 'v_hl'):
            values.setdefault('hwpx_review_colors', bool(app.v_hl.get()))
        self.hwpx_rules = {key: tk.BooleanVar(master=self, value=values.get(key, default))
                           for key, default in HWPX_DEFAULTS.items()}
        conversions = vars(app).get('_book_conversion_rules', {})
        self.conversion_rules = {key: tk.StringVar(master=self, value=CONVERSION_LABELS[conversions.get(key, 'default')])
                                 for key in CONVERSION_RULES}
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
        hwpx_rules = ttk.Frame(tabs, padding=12)
        analysis = ttk.Frame(tabs, padding=12)
        tabs.add(analysis, text='원본 분석·만들기')
        tabs.add(materials, text='책 자료')
        tabs.add(output, text='양식·출력')
        tabs.add(hwpx_rules, text='고급 편집')
        self._materials(materials)
        self._output(output)
        self._hwpx_rules(hwpx_rules)
        self._analysis(analysis)
        ttk.Button(analysis, text='출력 양식 고르기…', command=lambda: tabs.select(output)).pack(anchor='w', pady=(5, 0))
        ttk.Label(frame, textvariable=self.status, wraplength=600).grid(row=2, column=0, sticky='w', pady=4)
        footer = ttk.Frame(frame)
        footer.grid(row=3, column=0, sticky='ew')
        self.save = ttk.Button(footer, text='이 책 설정 저장', command=self.save_selection)
        self.save.pack(side='right')
        self.settings = ttk.Button(footer, text='설정 → 이 책', command=self.open_settings)
        self.settings.pack(side='left')

        self.traces = [(variable, variable.trace_add('write', self.invalidate_result))
                       for variable in (self.template, self.reference, self.pages, self.plan)]
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
        ttk.Label(frame, text='양식을 비우면 원본 용지와 문단 구조로 기본 HWPX를 구성합니다.',
                  wraplength=560).pack(anchor='w', pady=(6, 0))
        self.companion_check = ttk.Checkbutton(frame, text='이 책은 HWPX와 문자 PDF를 함께 만들기',
                                               variable=self.companion)
        self.companion_check.pack(anchor='w', pady=(8, 0))
        row = ttk.Frame(frame)
        row.pack(fill='x', pady=(8, 0))
        ttk.Label(row, text='HWPX 책갈피').pack(side='left')
        self.bookmark_box = ttk.Combobox(row, textvariable=self.bookmarks, values=list(BOOKMARK_LABELS.values()),
                                        state='readonly', width=18)
        self.bookmark_box.pack(side='left', padx=8)
        conversion = ttk.LabelFrame(frame, text='HWPX 글자 변환', padding=6)
        conversion.pack(anchor='w', fill='x', pady=(10, 0))
        self.conversion_controls = []
        for index, (key, (label, _, _)) in enumerate(CONVERSION_RULES.items()):
            row, column = divmod(index, 2)
            ttk.Label(conversion, text=label).grid(row=row, column=column * 2, sticky='w', padx=(3, 5), pady=3)
            control = ttk.Combobox(conversion, textvariable=self.conversion_rules[key],
                                   values=tuple(CONVERSION_LABELS.values()), state='readonly', width=15)
            control.grid(row=row, column=column * 2 + 1, sticky='w', padx=(0, 9), pady=3)
            self.conversion_controls.append(control)
        self.norm = ttk.Button(frame, text='공통 자동 교정 규칙 열기…', command=self.open_normalization)
        self.norm.pack(anchor='w', pady=(8, 0))

    def _hwpx_rules(self, frame):
        ttk.Label(frame, text='자동 분석 결과의 HWPX 적용 범위를 조정합니다. '
                              '항목 나누기는 이 PDF에서 확인한 표지를 사용합니다.',
                  wraplength=560).pack(anchor='w', pady=(0, 10))
        self.hwpx_controls = []
        for key, (label, _) in HWPX_OUTPUT_RULES.items():
            check = ttk.Checkbutton(frame, text=label, variable=self.hwpx_rules[key])
            check.pack(anchor='w', pady=(0, 5))
            self.hwpx_controls.append(check)
        self.recommend_hwpx = ttk.Button(frame, text='추천값 선택', command=self._select_hwpx_recommendation)
        self.recommend_hwpx.pack(anchor='w', pady=(6, 10))
        ttk.Label(frame, text='책·권·장에는 장 제목, 서문·절·목차에는 절 제목을 우선 적용합니다. '
                              '목차·색인의 행·열 배치는 원본과 구조 분석 결과를 참고합니다.',
                  wraplength=560).pack(anchor='w')
        if getattr(self.app, 'v_preset', None) is not None:
            ttk.Label(frame, textvariable=self.app.v_preset).pack(anchor='w', pady=(8, 0))
        row = ttk.Frame(frame)
        row.pack(anchor='w', pady=6)
        self.rules = ttk.Button(row, text='책별 짜임 규칙 관리 ▾', command=self.choose_rule)
        self.rules.pack(side='left')
        self.styles = ttk.Button(row, text='양식 스타일 연결…', command=self.edit_styles)
        self.styles.pack(side='left', padx=(6, 0))
        self.sample = ttk.Button(frame, text='이 책의 HWPX 미리보기', command=self.build_preview)
        self.sample.pack(anchor='w', pady=(8, 2))
        ttk.Label(frame, text='저장된 판독이나 PDF 글자층의 대표 쪽에 현재 규칙과 양식을 적용해 엽니다.',
                  wraplength=560).pack(anchor='w')

    def _select_hwpx_recommendation(self):
        for key, default in HWPX_DEFAULTS.items():
            self.hwpx_rules[key].set(default or key == 'hwpx_review_letters')
        self.status.set('추천 규칙을 선택했습니다. 미리보기로 확인하고 이 책 설정을 저장할 수 있습니다.')

    def _hwpx_values(self):
        return {key: variable.get() for key, variable in self.hwpx_rules.items()}

    def _conversion_values(self):
        choices = {label: key for key, label in CONVERSION_LABELS.items()}
        return {key: choices[variable.get()] for key, variable in self.conversion_rules.items()}

    def _preview_options(self):
        rule_flags = {flag for _, flag in HWPX_OUTPUT_RULES.values()}
        rule_flags.update(flag for _, _, flag in CONVERSION_RULES.values())
        opts = [opt for opt in self.app._build_opts() if opt not in rule_flags and opt not in ('--source-review', '--no-highlight')]
        if '--hwpx-bookmarks' in opts:
            index = opts.index('--hwpx-bookmarks')
            del opts[index:index + 2]
        return (opts + cli_flags(self._hwpx_values()) + conversion_flags(self.app, self._conversion_values())
                + ['--hwpx-bookmarks', self._bookmark_policy()])

    def _bookmark_policy(self):
        return next((key for key, label in BOOKMARK_LABELS.items() if label == self.bookmarks.get()), 'headings')

    def _analysis(self, frame):
        ttk.Label(frame, text='PDF 전체의 배치를 훑고 대표 쪽을 AI로 검토합니다. '
                              '제목, 문단 경계, 목록, 주석을 파악하고 선택한 양식에서 맞는 스타일을 찾습니다.',
                  wraplength=570).pack(anchor='w')
        row = ttk.Frame(frame)
        row.pack(fill='x', pady=(8, 4))
        self.build_both = ttk.Button(row, text='분석하고 HWPX·문자 PDF 만들기', command=self.analyze_and_build)
        self.build_both.pack(side='left')
        self.analysis_sample = ttk.Button(row, text='HWPX 미리보기', command=self.build_preview)
        self.analysis_sample.pack(side='left', padx=6)
        self.review_check = ttk.Checkbutton(frame, text='원본 대조 · OCR 오독 교정 · 각주 뒤 쪽 경계 검수',
                                            variable=self.source_review)
        self.review_check.pack(anchor='w', pady=(4, 0))
        ttk.Label(frame, text='설치된 OCR로 원본과 대조합니다. 확정하기 어려운 글자와 쪽 경계는 보고서에 남깁니다.',
                  wraplength=560).pack(anchor='w')
        ttk.Label(frame, text='대표 쪽 직접 지정 · 비우면 자동 선택 (최대 12쪽, 예 1-3, 20)').pack(anchor='w', pady=(8, 2))
        self.page_entry = ttk.Entry(frame, textvariable=self.pages)
        self.page_entry.pack(fill='x')
        row = ttk.Frame(frame)
        row.pack(fill='x', pady=6)
        ttk.Label(row, text='대표 쪽 분석 · 선택한 AI에 이미지 전송').pack(side='left')
        self.plan_box = ttk.Combobox(row, textvariable=self.plan, state='readonly', width=12,
                                    values=('auto', 'chatgpt', 'gemini', 'claude', '로컬만'))
        self.plan_box.pack(side='left', padx=6)
        self.manual = ttk.Button(row, text='수동 검토 자료…', command=lambda: self.app.make_book_rule_manual())
        self.manual.pack(side='right')
        row = ttk.Frame(frame)
        row.pack(fill='x')
        self.generate = ttk.Button(row, text='먼저 분석 결과 보기', command=self.start)
        self.generate.pack(side='left')
        self.cancel = ttk.Button(row, text='중지', state='disabled', command=self.cancel_generation)
        self.cancel.pack(side='left', padx=4)
        self.apply = ttk.Button(row, text='이 책에 분석 결과 적용', state='disabled', command=self.apply_result)
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
            self.preferred_pages = ()
            self.show_result()
            self.apply.configure(state='disabled')
            self.status.set('자료 선택이 변경되었습니다. 현재 자료로 규칙을 다시 생성하십시오.')

    def pick_template(self):
        path = filedialog.askopenfilename(parent=self, title='출력 양식 HWPX', filetypes=[('HWPX', '*.hwpx')])
        if path:
            select_template(self.template, self.layout, path)

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
        previous_rules = getattr(self.app, '_hwpx_output_rules', {})
        previous_conversion = vars(self.app).get('_book_conversion_rules', {})
        previous_pdf = vars(self.app).get('_book_textlayer_pdf')
        previous_review = vars(self.app).get('_book_source_review')
        previous_bookmarks = vars(self.app).get('_book_hwpx_bookmarks')
        self.app._hwpx_output_rules = self._hwpx_values()
        self.app._book_conversion_rules = self._conversion_values()
        self.app._book_textlayer_pdf = self.companion.get()
        self.app._book_source_review = self.source_review.get()
        self.app._book_hwpx_bookmarks = self._bookmark_policy()
        if self.app._save_book(create=True) is False:
            self.app._hwpx_output_rules = previous_rules
            self.app._book_conversion_rules = previous_conversion
            self.app._book_textlayer_pdf = previous_pdf
            self.app._book_source_review = previous_review
            self.app._book_hwpx_bookmarks = previous_bookmarks
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
        for button in (self.generate, self.build_both, self.analysis_sample, self.pick, self.manual, self.page_entry, self.template_entry,
                       self.reference_entry, self.reference_pick, self.save, self.rules, self.styles,
                       self.change_pdf, self.settings, self.recheck, self.sample, self.norm,
                       self.recommend_hwpx, self.companion_check, self.review_check, *self.hwpx_controls):
            button.configure(state='disabled' if busy else 'normal')
        self.plan_box.configure(state='disabled' if busy else 'readonly')
        for control in self.conversion_controls:
            control.configure(state='disabled' if busy else 'readonly')
        self.layout_box.configure(state='disabled' if busy else 'readonly')
        self.bookmark_box.configure(state='disabled' if busy else 'readonly')
        self.cancel.configure(state='normal' if busy else 'disabled')
        self.apply.configure(state='normal' if not busy and self.result else 'disabled')

    def build_preview(self):
        if self.busy or self.app.running:
            return
        from .book_output import preview_document
        options = self._preview_options()
        draft = self.result
        self.stop.clear()
        self.set_busy(True)
        self.cancel.configure(state='disabled')
        self.status.set('대표 쪽으로 HWPX 미리보기를 만드는 중입니다.')
        def worker():
            try:
                path, origin, numbers = preview_document(self.pdf, self.work_dir, options, self.preferred_pages,
                                                         analysis=draft)
                if self.stop.is_set():
                    path.unlink(missing_ok=True)
                else:
                    self.events.put(('sample', (path, origin, numbers, options)))
            except Exception as exc:
                self.events.put(('error', str(exc)))
        threading.Thread(target=worker, name='book-output-preview', daemon=True).start()

    def analyze_and_build(self):
        if self.busy:
            return
        if self.result:
            self.apply_result(build=True)
        else:
            self.start(build=True)

    def start(self, *, build=False):
        if self.busy:
            return
        if self.app.running or self.app._input_vars[0]['pdf'].get().strip() != self.pdf:
            messagebox.showinfo(self.title(), '원래 PDF를 선택하고 진행 중인 작업이 끝난 뒤 규칙을 생성하십시오.', parent=self)
            return
        from .rule_generation import generate_rule
        self.build_after_analysis = build
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
                    self.build_after_analysis = False
                    self.set_busy(False)
                    self.status.set('생성 중단: ' + value)
                elif kind == 'sample':
                    from .gui import open_path
                    self.set_busy(False)
                    path, origin, numbers, options = value
                    if self._preview_options() != options:
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
                        self.build_after_analysis = False
                        self.set_busy(False)
                        self.status.set('분석 도중 책 자료가 변경되었습니다. 현재 자료로 다시 생성하십시오.')
                        continue
                    if self.stop.is_set():
                        self.build_after_analysis = False
                        self.set_busy(False)
                        self.status.set('생성을 중지했습니다. 규칙은 적용하지 않았습니다.')
                        continue
                    self.result = value
                    kinds = value.get('type_pages', {})
                    samples = set(value['coverage']['selected_pages'])
                    self.preferred_pages = tuple(dict.fromkeys(next((n for n in kinds[role] if n in samples), kinds[role][0]) for role in
                        ('차례', '목차', '색인', '본문', '각주', '문헌 목록', '제목') if kinds.get(role)))
                    self.show_result()
                    self.status.set('분석 완료. 결과를 미리 보거나 이 책에 적용할 수 있습니다.')
                    self.set_busy(False)
                    if self.build_after_analysis:
                        self.build_after_analysis = False
                        if self.apply_result(build=True):
                            return
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
            if coverage.get('selection') == 'quick_layout':
                analyzed = coverage['analyzed_pages']
                scope = f"PDF {coverage['page_count']}쪽 로컬 검사 · AI 대표 {len(analyzed)}쪽 검토"
                if analyzed:
                    scope += '\nAI 검토 쪽 ' + ', '.join(map(str, analyzed))
            else:
                scope = f"PDF {coverage['page_count']}쪽 중 지정한 {len(coverage['selected_pages'])}쪽 검토"
            text = [scope,
                    value['summary'], '출력에 적용할 규칙', ', '.join(
                        label for key, label in (('entry', '항목 제목'), ('commentary', '주석'),
                        ('source', '출전'), ('emphasis', '굵기'), ('styles', '스타일 연결'),
                        ('citation', '인용'), ('headings', '제목 단계'), ('page_types', '쪽 유형과 원본 목록 배치')) if value['rule'].get(key)
                    ) or '기본 문단 규칙', '확인할 항목', '\n'.join(value['warnings']) or '없음']
            types = [f"{kind} · {len(numbers)}쪽 · PDF {', '.join(map(str, numbers[:8]))}"
                     + (' 외' if len(numbers) > 8 else '')
                     for kind, numbers in value.get('type_pages', {}).items() if numbers]
            if types:
                text.extend(['발견한 쪽 유형', '\n'.join(types)])
            rule = value['rule']
            if 'field_labels' in rule:
                text.extend(['문단을 나눌 항목 표지', ', '.join(rule['field_labels']) or '확인된 항목 표지 없음'])
            if paper := rule.get('paper'):
                text.append(f"원본 기준 용지 {paper['width']:g} × {paper['height']:g} mm")
            if styles := (rule.get('styles') or {}).get('paragraph'):
                text.extend(['양식 스타일 연결', '\n'.join(
                    f'{role} → {names[0]}' for role, names in styles.items())])
            if self.include_json.get():
                text.extend(['규칙 JSON', json.dumps(value['rule'], ensure_ascii=False, indent=2)])
            self.preview.insert('1.0', '\n\n'.join(text))
        self.preview.configure(state='disabled')

    def cancel_generation(self):
        self.stop.set()
        self.status.set('중지 요청 중… 완료 전까지 규칙을 적용하지 않습니다.')
        self.cancel.configure(state='disabled')

    def apply_result(self, *, build=False):
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
            if build:
                from .gui import MODE_LABELS
                if self.app.use_book_rule(str(path), save=False) is False:
                    return False
                self.app._mode_vars[0].set(MODE_LABELS['auto'])
                self.app._book_textlayer_pdf = True
                self.app.v_bold.set(False)
            elif self.app.use_book_rule(str(path)) is False:
                return False
            self.app._hwpx_output_rules = self._hwpx_values()
            self.app._book_conversion_rules = self._conversion_values()
            self.app._book_source_review = self.source_review.get()
            self.app._book_hwpx_bookmarks = self._bookmark_policy()
            reference_variable = getattr(self.app, 'v_reference_hwpx', None)
            if reference_variable is not None:
                reference_variable.set(result['inputs'].get('reference_hwpx', ''))
            if (reference_variable is not None or build) and self.app._save_book(create=True) is False:
                raise OSError('이 책의 결과 폴더에 설정을 저장할 수 없습니다.')
            self.reload_selection()
        except (OSError, ValueError) as exc:
            messagebox.showerror(self.title(), '규칙 저장 실패: ' + str(exc), parent=self)
            return
        self.status.set('새 규칙을 저장하고 이 책에 적용했습니다: ' + path.name)
        self.result = None
        self.apply.configure(state='disabled')
        if build:
            if not result['coverage']['analyzed_pages']:
                self.app._append('책 형식은 로컬 분석 결과를 적용했습니다. AI 대표 쪽 확인은 분석 보고서를 참조하십시오.\n')
            self.close()
            self.app.do_run()
        return True

    def close(self):
        self.stop.set()
        self.after_cancel(self.poll_id)
        for variable, trace_id in self.traces:
            variable.trace_remove('write', trace_id)
        self.traces.clear()
        self.destroy()
