"""Desktop scan/correction editor. Tk mutations are confined to its UI thread."""
from __future__ import annotations

import argparse
from pathlib import Path
import queue
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
import uuid


PSM_MODES = {"자동 문서 배치": 3, "세로쓰기 단일 구역": 5, "가로쓰기 단일 구역": 6, "흩어진 글자": 11}
LANGUAGES = ("kor+chi_tra+eng", "kor+chi_tra", "kor+chi_sim+eng", "kor+eng", "kor", "chi_tra", "chi_tra+eng", "chi_sim", "kor_vert+chi_tra_vert", "eng")
ENGINES = ("tesseract", "rapidocr", "easyocr")


class EditorWindow(tk.Tk):
    def __init__(self, source_path=None, project_path=None, *, recovery_dir=None):
        super().__init__()
        self.title("AB_OCR · 원본 대조 편집")
        self.geometry("1440x900")
        self.minsize(1080, 720)
        self.configure(background="#edf1f6")
        self.project = None
        self.page_number = 1
        self._generation = 0
        self._events = queue.Queue()
        self._job_id = 0
        self._busy = False
        self._job_kind = ""
        self._stop = threading.Event()
        self._closed = False
        self._loading = False
        self._pending_edit = False
        self._close_when_idle = False
        self._close_after_recovery = False
        self._deferred_open = None
        self._render_id = 0
        self._preview = None
        self._photo = None
        self._zoom = None
        self._canvas_origin = (0, 0)
        self._display_size = (1, 1)
        self._resize_after = None
        self._recovery_after = None
        self._recovery_running = False
        self._recovery_generation = None
        self._recovery_pending = False
        self._recovery_path = None
        self._recovery_error = None
        self.recovery_dir = Path(recovery_dir) if recovery_dir else Path(__file__).resolve().parent.parent / "work" / "recovery"
        self._controls = []
        self._document_controls = []
        self._ocr_counts = {"completed": 0, "failed": 0, "total": 0}
        self._style()
        self._build()
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.bind("<Control-s>", lambda _e: self.save_project())
        self.bind("<Control-o>", lambda _e: self.open_source())
        self.bind("<Control-Return>", lambda _e: self.recognize(False))
        self.bind("<Escape>", lambda _e: self.cancel())
        self._poll_id = self.after(60, self._poll)
        self._set_enabled()
        if project_path:
            self.after(100, lambda: self.open_project(project_path))
        elif source_path:
            self.after(100, lambda: self.open_source(source_path))

    def _style(self):
        style = ttk.Style(self)
        if "clam" in style.theme_names():
            style.theme_use("clam")
        style.configure("ED.TFrame", background="#edf1f6")
        style.configure("ED.Card.TFrame", background="white")
        style.configure("ED.TLabel", background="#edf1f6", foreground="#24354c", font=("맑은 고딕", 10))
        style.configure("ED.Hint.TLabel", background="#edf1f6", foreground="#627289", font=("맑은 고딕", 9))
        style.configure("ED.Card.TLabel", background="white", foreground="#24354c", font=("맑은 고딕", 10))
        style.configure("ED.TButton", font=("맑은 고딕", 9), padding=(9, 7))
        style.configure("ED.Primary.TButton", font=("맑은 고딕", 10, "bold"), padding=(12, 7), foreground="white", background="#245bba")
        style.map("ED.Primary.TButton", background=[("disabled", "#a5b4ca"), ("active", "#174b9b")])
        style.configure("ED.Treeview", rowheight=32, font=("맑은 고딕", 10))
        style.configure("ED.Treeview.Heading", font=("맑은 고딕", 9, "bold"))

    def _button(self, parent, text, command, *, document=False, primary=False):
        button = ttk.Button(parent, text=text, command=command, style="ED.Primary.TButton" if primary else "ED.TButton")
        self._controls.append(button)
        if document:
            self._document_controls.append(button)
        return button

    def _build(self):
        self.columnconfigure(0, weight=1)
        self.rowconfigure(3, weight=1)
        header = tk.Frame(self, background="#152a46", padx=20, pady=12)
        header.grid(row=0, column=0, sticky="ew")
        tk.Label(header, text="AB_OCR", font=("Segoe UI", 23, "bold"), foreground="white", background="#152a46").pack(side="left")
        tk.Label(header, text="원본 대조 · 인식 · 교정", font=("맑은 고딕", 11), foreground="#c4d7f0", background="#152a46").pack(side="left", padx=20)
        self.document_var = tk.StringVar(self, "PDF 또는 스캔 이미지를 열어 시작하세요")
        tk.Label(header, textvariable=self.document_var, font=("맑은 고딕", 10), foreground="#c4d7f0", background="#152a46", anchor="e").pack(side="right", fill="x", expand=True)

        toolbar = ttk.Frame(self, style="ED.TFrame", padding=(14, 10, 14, 5))
        toolbar.grid(row=1, column=0, sticky="ew")
        for label, command, document in (("원본 열기…", self.open_source, False), ("프로젝트 열기…", self.open_project, False),
                                          ("프로젝트 저장", self.save_project, True), ("다른 이름으로…", lambda: self.save_project(save_as=True), True),
                                          ("TXT 내보내기…", self.export_text, True), ("복구 사본 열기…", self.open_recovery, False)):
            self._button(toolbar, label, command, document=document).pack(side="left", padx=(0, 5))
        self._button(toolbar, "정확도 평가", self.open_evaluation).pack(side="right")

        settings_panel = ttk.Frame(self, style="ED.TFrame", padding=(14, 3, 14, 10))
        settings_panel.grid(row=2, column=0, sticky="ew")
        settings = ttk.Frame(settings_panel, style="ED.TFrame")
        settings.pack(fill="x")
        ttk.Label(settings, text="엔진", style="ED.TLabel").pack(side="left", padx=(0, 6))
        self.engine = tk.StringVar(self, "tesseract")
        self.engine_box = ttk.Combobox(settings, textvariable=self.engine, values=ENGINES, state="readonly", width=10)
        self.engine_box.pack(side="left", padx=(0, 10))
        self.engine_box.bind("<<ComboboxSelected>>", self._engine_changed)
        ttk.Label(settings, text="인식 언어", style="ED.TLabel").pack(side="left", padx=(0, 6))
        self.language = tk.StringVar(self, LANGUAGES[0])
        self.language_box = ttk.Combobox(settings, textvariable=self.language, values=LANGUAGES, width=20)
        self.language_box.pack(side="left", padx=(0, 10))
        ttk.Label(settings, text="읽기 방식", style="ED.TLabel").pack(side="left", padx=(0, 6))
        self.psm = tk.StringVar(self, next(iter(PSM_MODES)))
        self.psm_box = ttk.Combobox(settings, textvariable=self.psm, values=list(PSM_MODES), state="readonly", width=16)
        self.psm_box.pack(side="left", padx=(0, 10))
        ttk.Label(settings, text="PDF DPI", style="ED.TLabel").pack(side="left", padx=(0, 6))
        self.dpi = tk.IntVar(self, 300)
        self.dpi_box = ttk.Combobox(settings, textvariable=self.dpi, values=(150, 200, 300, 400, 450, 600), state="readonly", width=5)
        self.dpi_box.pack(side="left", padx=(0, 10))
        actions = ttk.Frame(settings_panel, style="ED.TFrame")
        actions.pack(fill="x", pady=(7, 0))
        self.current_ocr = self._button(actions, "현재 쪽 인식", lambda: self.recognize(False), document=True, primary=True)
        self.current_ocr.pack(side="left", padx=(0, 5))
        self.all_ocr = self._button(actions, "전체 쪽 인식", lambda: self.recognize(True), document=True)
        self.all_ocr.pack(side="left", padx=(0, 5))
        self.stop_button = ttk.Button(actions, text="중지", style="ED.TButton", command=self.cancel, state="disabled")
        self.stop_button.pack(side="left")
        self.engine_hint = tk.StringVar(self, "설치한 로컬 언어 모델을 사용합니다. 원시 결과는 보존됩니다.")
        ttk.Label(actions, textvariable=self.engine_hint, style="ED.Hint.TLabel", wraplength=500).pack(side="left", padx=12)

        panes = ttk.Panedwindow(self, orient="horizontal")
        panes.grid(row=3, column=0, sticky="nsew", padx=14)
        pages = ttk.Frame(panes, style="ED.Card.TFrame", padding=8, width=170)
        pages.columnconfigure(0, weight=1)
        pages.rowconfigure(1, weight=1)
        panes.add(pages, weight=0)
        ttk.Label(pages, text="문서의 쪽", style="ED.Card.TLabel", font=("맑은 고딕", 10, "bold")).grid(row=0, column=0, sticky="w", pady=(2, 8))
        self.page_tree = ttk.Treeview(pages, columns=("number", "state"), show="headings", selectmode="browse", style="ED.Treeview", height=12)
        self.page_tree.heading("number", text="쪽")
        self.page_tree.heading("state", text="상태")
        self.page_tree.column("number", width=42, minwidth=35, stretch=False, anchor="center")
        self.page_tree.column("state", width=88, minwidth=65)
        self.page_tree.grid(row=1, column=0, sticky="nsew")
        page_scroll = ttk.Scrollbar(pages, command=self.page_tree.yview)
        page_scroll.grid(row=1, column=1, sticky="ns")
        self.page_tree.configure(yscrollcommand=page_scroll.set)
        self.page_tree.bind("<<TreeviewSelect>>", self._page_selected)
        self.page_tree.tag_configure("failed", foreground="#a6402c")
        self.page_tree.tag_configure("reviewed", foreground="#22724b")

        original = ttk.Frame(panes, style="ED.Card.TFrame", padding=10)
        original.columnconfigure(0, weight=1)
        original.rowconfigure(1, weight=1)
        panes.add(original, weight=3)
        viewbar = ttk.Frame(original, style="ED.Card.TFrame")
        viewbar.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        ttk.Label(viewbar, text="원본 이미지", style="ED.Card.TLabel", font=("맑은 고딕", 11, "bold")).pack(side="left")
        self.zoom_var = tk.StringVar(self, "화면 맞춤")
        for label, command in (("−", lambda: self.change_zoom(.8)), ("+", lambda: self.change_zoom(1.25)), ("맞춤", self.fit_page)):
            ttk.Button(viewbar, text=label, command=command, width=4).pack(side="right", padx=2)
        ttk.Label(viewbar, textvariable=self.zoom_var, style="ED.Card.TLabel").pack(side="right", padx=8)
        canvas_frame = ttk.Frame(original)
        canvas_frame.grid(row=1, column=0, sticky="nsew")
        canvas_frame.columnconfigure(0, weight=1)
        canvas_frame.rowconfigure(0, weight=1)
        self.canvas = tk.Canvas(canvas_frame, background="#dfe5ed", highlightthickness=0, width=470, height=400)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        vertical = ttk.Scrollbar(canvas_frame, orient="vertical", command=self.canvas.yview)
        horizontal = ttk.Scrollbar(canvas_frame, orient="horizontal", command=self.canvas.xview)
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal.grid(row=1, column=0, sticky="ew")
        self.canvas.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        self.canvas.bind("<Configure>", self._schedule_draw)
        self.canvas.bind("<MouseWheel>", self._wheel)
        self.canvas.bind("<Control-MouseWheel>", self._wheel_zoom)
        self.canvas.bind("<ButtonPress-1>", lambda e: self.canvas.scan_mark(e.x, e.y))
        self.canvas.bind("<B1-Motion>", lambda e: self.canvas.scan_dragto(e.x, e.y, gain=1))
        self.canvas.bind("<Motion>", self._hover_word)
        self.canvas.bind("<Leave>", self._clear_hover)
        navigation = ttk.Frame(original, style="ED.Card.TFrame")
        navigation.grid(row=2, column=0, sticky="ew", pady=(8, 0))
        self.previous = ttk.Button(navigation, text="◀ 이전", command=lambda: self.show_page(self.page_number - 1), width=8)
        self.previous.pack(side="left")
        self.next = ttk.Button(navigation, text="다음 ▶", command=lambda: self.show_page(self.page_number + 1), width=8)
        self.next.pack(side="right")
        self.page_var = tk.StringVar(self, "0 / 0쪽")
        ttk.Label(navigation, textvariable=self.page_var, style="ED.Card.TLabel", anchor="center").pack(side="left", expand=True, fill="x")
        self.boxes = tk.BooleanVar(self, False)
        ttk.Checkbutton(original, text="낮은 신뢰도 단어 표시 (70 미만 · 정확도 점수 아님)", variable=self.boxes, command=self._draw).grid(row=3, column=0, sticky="w", pady=(6, 0))
        self.word_var = tk.StringVar(self, "단어 위에 마우스를 올려 원시 판독과 위치를 확인하세요.")
        ttk.Label(original, textvariable=self.word_var, style="ED.Card.TLabel", foreground="#657791", wraplength=550).grid(row=4, column=0, sticky="ew", pady=(4, 0))

        correction = ttk.Frame(panes, style="ED.Card.TFrame", padding=10)
        correction.columnconfigure(0, weight=1)
        correction.rowconfigure(2, weight=1)
        panes.add(correction, weight=2)
        editor_header = ttk.Frame(correction, style="ED.Card.TFrame")
        editor_header.grid(row=0, column=0, sticky="ew")
        ttk.Label(editor_header, text="인식 결과 · 교정문", style="ED.Card.TLabel", font=("맑은 고딕", 11, "bold")).pack(side="left")
        self.character_var = tk.StringVar(self, "0자")
        ttk.Label(editor_header, textvariable=self.character_var, style="ED.Card.TLabel").pack(side="right")
        self.text_state_var = tk.StringVar(self, "원본을 연 뒤 [현재 쪽 인식]을 누르세요.")
        ttk.Label(correction, textvariable=self.text_state_var, style="ED.Card.TLabel", foreground="#64758d", wraplength=410).grid(row=1, column=0, sticky="ew", pady=(7, 8))
        text_frame = ttk.Frame(correction)
        text_frame.grid(row=2, column=0, sticky="nsew")
        text_frame.columnconfigure(0, weight=1)
        text_frame.rowconfigure(0, weight=1)
        self.text = tk.Text(text_frame, wrap="word", undo=True, maxundo=200, font=("맑은 고딕", 12), foreground="#24354c", background="#fcfdff",
                            padx=11, pady=12, spacing1=3, spacing3=3, highlightthickness=1, highlightbackground="#dce3ed", relief="flat", width=34, height=12)
        self.text.grid(row=0, column=0, sticky="nsew")
        text_scroll = ttk.Scrollbar(text_frame, command=self.text.yview)
        text_scroll.grid(row=0, column=1, sticky="ns")
        self.text.configure(yscrollcommand=text_scroll.set)
        self.text.bind("<<Modified>>", self._text_changed)
        raw_bar = ttk.Frame(correction, style="ED.Card.TFrame")
        raw_bar.grid(row=3, column=0, sticky="ew", pady=(8, 0))
        self.raw_button = self._button(raw_bar, "원시 OCR 보기", self.show_raw, document=True)
        self.raw_button.pack(side="left", padx=(0, 5))
        self.restore_button = self._button(raw_bar, "원시 결과로 복원", self.restore_raw, document=True)
        self.restore_button.pack(side="left")
        self.reviewed = tk.BooleanVar(self, False)
        self.review_check = ttk.Checkbutton(correction, text="이 쪽을 원본과 대조했습니다", variable=self.reviewed, command=self._review_changed)
        self.review_check.grid(row=4, column=0, sticky="w", pady=(9, 0))
        self.error_var = tk.StringVar(self, "원시 OCR은 별도 보존합니다. 재인식해도 교정문은 유지합니다.")
        ttk.Label(correction, textvariable=self.error_var, style="ED.Card.TLabel", foreground="#805f46", wraplength=410).grid(row=5, column=0, sticky="ew", pady=(8, 0))

        footer = ttk.Frame(self, style="ED.TFrame", padding=(14, 9))
        footer.grid(row=4, column=0, sticky="ew")
        self.progress = ttk.Progressbar(footer, mode="determinate", length=150)
        self.progress.pack(side="left", padx=(0, 10))
        self.status = tk.StringVar(self, "준비 · 원본 파일은 변경하지 않습니다. 인식은 이 컴퓨터에서 실행합니다.")
        ttk.Label(footer, textvariable=self.status, style="ED.Hint.TLabel", wraplength=900).pack(side="left", fill="x", expand=True)
        self.save_state = tk.StringVar(self, "")
        ttk.Label(footer, textvariable=self.save_state, style="ED.Hint.TLabel").pack(side="right", padx=(10, 0))
        self.after_idle(self._draw)

    def _set_enabled(self):
        has_document = self.project is not None
        transitioning = self._close_after_recovery or self._deferred_open is not None
        for control in self._controls:
            allowed = not self._busy and not transitioning and (has_document or control not in self._document_controls)
            control.configure(state="normal" if allowed else "disabled")
        # During OCR, page navigation and correction are safe: each result is
        # merged by the model without replacing manual corrections.
        editable = has_document and not transitioning and (not self._busy or self._job_kind == "ocr")
        self.text.configure(state="normal" if editable else "disabled")
        self.review_check.configure(state="normal" if editable else "disabled")
        self.previous.configure(state="normal" if editable and self.page_number > 1 else "disabled")
        self.next.configure(state="normal" if editable and self.page_number < self.project.page_count else "disabled")
        self.engine_box.configure(state="disabled" if self._busy or transitioning else "readonly")
        self.language_box.configure(state="disabled" if self._busy or transitioning else "normal" if self.engine.get() == "tesseract" else "readonly")
        self.psm_box.configure(state="disabled" if self._busy or transitioning or self.engine.get() != "tesseract" else "readonly")
        self.dpi_box.configure(state="disabled" if self._busy or transitioning else "readonly")
        self.stop_button.configure(state="normal" if self._busy and self._job_kind == "ocr" and not self._stop.is_set() else "disabled")

    def _engine_changed(self, _event=None, *, preserve_language=False):
        from .local_engines import ENGINE_LANGUAGES
        engine = self.engine.get()
        choices = LANGUAGES if engine == "tesseract" else ENGINE_LANGUAGES.get(engine, ())
        self.language_box.configure(values=choices)
        if choices and not preserve_language and self.language.get() not in choices:
            self.language.set(choices[0])
        if engine == "tesseract":
            self.engine_hint.set("설치한 로컬 언어 모델을 사용합니다. 원시 결과는 보존됩니다.")
        else:
            self.engine_hint.set("자체 영역 검출 (PSM 미사용) · 영어 포함 모델 · 한글+한자 혼용 조합 미지원. 자동 다운로드 없음.")
        self._set_enabled()

    def _title(self):
        if self.project:
            star = " *" if self.project.dirty else ""
            self.title(f"{self.project.source_path.name}{star} · AB_OCR")
            self.document_var.set(self.project.source_path.name)
            self.save_state.set("변경 내용 있음" if self.project.dirty else "저장됨" if self.project.project_path else "새 문서")

    def _confirm_replace(self):
        self._commit_text()
        if self.project and self.project.dirty:
            return messagebox.askyesno("저장하지 않은 프로젝트", "프로젝트에 저장하지 않은 변경이 있습니다.\n현재 문서를 닫고 다른 문서를 열까요?\n교정문을 보관하려면 [아니요]를 누른 뒤 프로젝트를 저장하세요.", parent=self)
        return True

    def open_source(self, path=None):
        if self._busy or self._close_after_recovery or self._deferred_open is not None:
            return
        if path is None:
            path = filedialog.askopenfilename(parent=self, title="스캔 PDF 또는 이미지 열기", filetypes=[("PDF·이미지", "*.pdf *.png *.jpg *.jpeg *.tif *.tiff *.bmp *.webp"), ("모든 파일", "*.*")])
        if not path or not self._confirm_replace():
            return
        self._open_after_recovery("source", path)

    def open_project(self, path=None):
        if self._busy or self._close_after_recovery or self._deferred_open is not None:
            return
        if path is None:
            path = filedialog.askopenfilename(parent=self, title="AB_OCR 프로젝트 열기", filetypes=[("AB_OCR 프로젝트", "*.abocr.json *.json")])
        if not path or not self._confirm_replace():
            return
        self._open_after_recovery("project", path)

    def _open_after_recovery(self, kind, path):
        if self.project and (self.project.dirty or self._recovery_running):
            self._deferred_open = (kind, path)
            self._set_enabled()
            self._request_recovery()
            self.status.set("현재 문서의 최신 복구 사본을 보관한 뒤 새 문서를 엽니다.")
        else:
            self._load_document(kind, path)

    def _load_document(self, kind, path):
        def worker():
            from .document import DocumentProject
            return DocumentProject.open(path) if kind == "source" else DocumentProject.load(path)
        self._start_job("open", "프로젝트와 원본 정보를 확인하는 중…", worker, self._set_project)

    def open_recovery(self):
        if self._busy:
            return
        path = filedialog.askopenfilename(parent=self, title="자동 복구 사본 열기", initialdir=str(self.recovery_dir), filetypes=[("자동 복구 프로젝트", "*.json")])
        if path:
            self.open_project(path)

    def _set_project(self, project):
        self._commit_text()
        if self._recovery_after:
            self.after_cancel(self._recovery_after)
            self._recovery_after = None
        self._generation += 1
        self.project = project
        self.page_number = 1
        self._pending_edit = False
        self._recovery_path = self.recovery_dir / f"{project.source_sha256[:16]}-{uuid.uuid4().hex[:12]}.abocr.json"
        self._recovery_pending = False
        self._recovery_error = None
        for number in project.completed_pages:
            raw = project.page(number).raw
            if raw:
                self.engine.set(raw.get("engine", "tesseract"))
                if raw.get("languages"):
                    self.language.set(raw["languages"])
                selected_psm = next((label for label, value in PSM_MODES.items() if value == raw.get("psm")), None)
                if selected_psm:
                    self.psm.set(selected_psm)
                render_dpi = raw.get("preprocessing", {}).get("render_dpi")
                if type(render_dpi) is int and 72 <= render_dpi <= 600:
                    self.dpi.set(render_dpi)
                self._engine_changed(preserve_language=True)
                break
        self.page_tree.delete(*self.page_tree.get_children())
        for number in range(1, project.page_count + 1):
            self.page_tree.insert("", "end", iid=str(number), values=(number, "미인식"))
            self._update_page_row(number)
        self._zoom = None
        self.show_page(1, force=True)
        self.status.set(f"원본 열림 · {project.page_count}쪽 · 원시 OCR과 교정문을 따로 보존합니다.")
        self._title()
        self._set_enabled()

    def _page_selected(self, _event=None):
        selected = self.page_tree.selection()
        if selected:
            self.show_page(int(selected[0]))

    def show_page(self, number, *, force=False):
        if not self.project or not 1 <= number <= self.project.page_count:
            return
        if self._close_after_recovery or self._deferred_open is not None or (self._busy and self._job_kind != "ocr"):
            return
        if number == self.page_number and not force:
            return
        self._commit_text()
        self.page_number = number
        self._populate_text()
        self.page_tree.selection_set(str(number))
        self.page_tree.see(str(number))
        self.page_var.set(f"{number} / {self.project.page_count}쪽")
        self._request_render()
        self._set_enabled()

    def _populate_text(self, *, preserve_edit=False):
        page = self.project.page(self.page_number)
        content = self.project.text(self.page_number)
        self._loading = True
        try:
            self.text.configure(state="normal")
            if not preserve_edit or self.text.get("1.0", "end-1c") != content:
                self.text.delete("1.0", "end")
                self.text.insert("1.0", content)
                self.text.edit_reset()
            self.text.edit_modified(False)
            self._pending_edit = False
            self.reviewed.set(page.reviewed)
            self.character_var.set(f"{len(content):,}자")
            self.text_state_var.set("사람이 교정한 텍스트 · 원시 결과 별도 보존" if page.corrected_text is not None else "원시 OCR 결과 · 직접 고칠 수 있습니다" if page.raw else "아직 인식하지 않은 쪽 · 직접 입력하거나 인식하세요")
            notes = []
            preprocessing = (page.raw or {}).get("preprocessing", {})
            if preprocessing.get("omitted_word_boxes"):
                notes.append(f"좌표 제외 {preprocessing['omitted_word_boxes']}개")
            if preprocessing.get("clipped_word_boxes"):
                notes.append(f"좌표 보정 {preprocessing['clipped_word_boxes']}개")
            explanation = page.last_error or "원시 OCR은 별도 보존합니다. 재인식해도 교정문은 유지합니다."
            if notes:
                explanation += "\n" + " · ".join(notes) + " — 원시 텍스트는 유지됨"
            self.error_var.set(explanation)
        finally:
            self._loading = False

    def _text_changed(self, _event=None):
        if not self.text.edit_modified():
            return
        self.text.edit_modified(False)
        if self._loading or not self.project:
            return
        self._pending_edit = True
        self._commit_text()

    def _commit_text(self):
        if not self.project or self._loading or not (self._pending_edit or self.text.edit_modified()):
            return
        value = self.text.get("1.0", "end-1c")
        self.text.edit_modified(False)
        self.project.set_text(self.page_number, value)
        self._pending_edit = False
        self.character_var.set(f"{len(value):,}자")
        self.text_state_var.set("사람이 교정한 텍스트 · 원시 결과 별도 보존")
        self.reviewed.set(self.project.page(self.page_number).reviewed)
        self._update_page_row(self.page_number)
        self._title()
        self._schedule_recovery()

    def _review_changed(self):
        if self.project:
            selected = self.reviewed.get()
            self._commit_text()
            self.reviewed.set(selected)
            self.project.set_reviewed(self.page_number, selected)
            self._update_page_row(self.page_number)
            self._title()
            self._schedule_recovery()

    def _update_page_row(self, number):
        if not self.project or not self.page_tree.exists(str(number)):
            return
        page = self.project.page(number)
        label = "대조 완료" if page.reviewed else "교정됨" if page.corrected_text is not None else "인식됨" if page.raw else "미인식"
        if page.last_error:
            label = "오류 · " + label
        self.page_tree.item(str(number), values=(number, label), tags=("failed",) if page.last_error else ("reviewed",) if page.reviewed else ())

    def _request_render(self):
        self._render_id += 1
        render_id, generation = self._render_id, self._generation
        project = self.project
        path, number = project.source_path, self.page_number
        if self._preview is not None:
            self._preview.close()
            self._preview = None
        self.canvas.delete("all")
        self.canvas.create_text(max(200, self.canvas.winfo_width()) // 2, 150, text="원본 이미지를 불러오는 중…", fill="#536781", font=("맑은 고딕", 12))
        def worker():
            try:
                from .ocr import render_page
                project.validate_source()
                picture = render_page(path, page_number=number, dpi=150)
                try:
                    project.validate_source()
                except Exception:
                    picture.close()
                    raise
                self._events.put(("render", generation, render_id, picture))
            except Exception as exc:
                self._events.put(("render_error", generation, render_id, str(exc)))
        threading.Thread(target=worker, name="AB-OCR-preview", daemon=True).start()

    def _schedule_draw(self, _event=None):
        if self._resize_after:
            self.after_cancel(self._resize_after)
        self._resize_after = self.after(100, self._draw)

    def _draw(self):
        self._resize_after = None
        if self._closed:
            return
        canvas = self.canvas
        canvas.delete("all")
        if self._preview is None:
            canvas.create_text(max(100, canvas.winfo_width()) // 2, max(100, canvas.winfo_height()) // 2,
                               text="원본을 열면 이곳에 표시됩니다", fill="#657791", font=("맑은 고딕", 12))
            return
        from PIL import Image, ImageTk
        width, height = self._preview.size
        fit = min(max(1, canvas.winfo_width() - 24) / width, max(1, canvas.winfo_height() - 24) / height)
        scale = self._zoom if self._zoom is not None else fit
        # Bound the display bitmap independently of OCR's full-resolution image.
        scale = min(scale, 6000 / max(width, height))
        shown_size = (max(1, round(width * scale)), max(1, round(height * scale)))
        shown = self._preview.resize(shown_size, Image.Resampling.LANCZOS)
        self._photo = ImageTk.PhotoImage(shown, master=canvas)
        shown.close()
        x = max(12, (canvas.winfo_width() - shown_size[0]) // 2)
        y = max(12, (canvas.winfo_height() - shown_size[1]) // 2)
        self._canvas_origin, self._display_size = (x, y), shown_size
        canvas.create_image(x, y, anchor="nw", image=self._photo)
        canvas.configure(scrollregion=(0, 0, max(canvas.winfo_width(), shown_size[0] + 24), max(canvas.winfo_height(), shown_size[1] + 24)))
        self.zoom_var.set(f"{scale * 100:.0f}%" + (" · 맞춤" if self._zoom is None else ""))
        if self.boxes.get() and self.project:
            raw = self.project.page(self.page_number).raw or {}
            rw, rh = raw.get("image_width", width), raw.get("image_height", height)
            for word in raw.get("words", []):
                confidence = word.get("confidence")
                if confidence is None or confidence >= 70:
                    continue
                x0, y0, x1, y1 = word["bbox"]
                canvas.create_rectangle(x + x0 / rw * shown_size[0], y + y0 / rh * shown_size[1],
                                        x + x1 / rw * shown_size[0], y + y1 / rh * shown_size[1], outline="#da8a18", width=2)

    def fit_page(self):
        self._zoom = None
        self.canvas.xview_moveto(0)
        self.canvas.yview_moveto(0)
        self._draw()

    def change_zoom(self, multiplier):
        if self._preview is None:
            return
        current = self._zoom if self._zoom is not None else self._display_size[0] / self._preview.width
        self._zoom = max(.05, min(4, current * multiplier))
        self._draw()

    def _wheel(self, event):
        self.canvas.yview_scroll(-1 if event.delta > 0 else 1, "units")
        return "break"

    def _wheel_zoom(self, event):
        self.change_zoom(1.15 if event.delta > 0 else 1 / 1.15)
        return "break"

    def _clear_hover(self, _event=None):
        self.canvas.delete("hover_word")
        self.word_var.set("단어 위에 마우스를 올려 원시 판독과 위치를 확인하세요.")

    def _hover_word(self, event):
        if not self.project or self._preview is None:
            return
        raw = self.project.page(self.page_number).raw or {}
        width, height = raw.get("image_width", 0), raw.get("image_height", 0)
        if not width or not height:
            return
        ox, oy = self._canvas_origin
        dw, dh = self._display_size
        x = (self.canvas.canvasx(event.x) - ox) / dw * width
        y = (self.canvas.canvasy(event.y) - oy) / dh * height
        for word in raw.get("words", []):
            x0, y0, x1, y1 = word["bbox"]
            if x0 <= x <= x1 and y0 <= y <= y1:
                self.canvas.delete("hover_word")
                self.canvas.create_rectangle(ox + x0 / width * dw, oy + y0 / height * dh,
                                             ox + x1 / width * dw, oy + y1 / height * dh,
                                             outline="#245bba", width=2, tags="hover_word")
                confidence = word.get("confidence")
                score = "없음" if confidence is None else f"{confidence:.1f}"
                self.word_var.set(f"원시 단어: {word['text']}   |   엔진 신뢰도 {score} (정확도 아님)")
                return
        self._clear_hover()

    def recognize(self, all_pages=False):
        if self._busy or not self.project or self._close_after_recovery or self._deferred_open is not None:
            return
        self._commit_text()
        languages = self.language.get().strip()
        if not languages:
            self.status.set("인식할 언어 데이터를 선택하세요.")
            return
        pages = list(range(1, self.project.page_count + 1)) if all_pages else [self.page_number]
        project = self.project
        source, generation = project.source_path, self._generation
        engine = self.engine.get()
        psm = PSM_MODES[self.psm.get()] if engine == "tesseract" else None
        dpi = self.dpi.get()
        self._busy, self._job_kind = True, "ocr"
        self._job_id += 1
        job_id = self._job_id
        stop = self._stop = threading.Event()
        self._ocr_counts = {"completed": 0, "failed": 0, "total": len(pages)}
        self.progress.configure(mode="determinate", maximum=len(pages), value=0)
        self.status.set(f"{engine} 로컬 OCR 시작 · {len(pages)}쪽 · 교정문은 유지됩니다.")
        self._set_enabled()
        def worker():
            from .ocr import recognize_file, OCRCancelled
            try:
                project.validate_source()
            except Exception as exc:
                self._events.put(("ocr_error", generation, job_id, (pages[0], str(exc))))
                self._events.put(("ocr_done", generation, job_id, None))
                return
            for number in pages:
                if stop.is_set():
                    break
                self._events.put(("ocr_progress", generation, job_id, number))
                try:
                    result = recognize_file(source, languages=languages, page_number=number, psm=psm, dpi=dpi, engine=engine, should_stop=stop.is_set)
                except OCRCancelled:
                    break
                except Exception as exc:
                    self._events.put(("ocr_error", generation, job_id, (number, str(exc))))
                else:
                    self._events.put(("ocr_page", generation, job_id, result))
            self._events.put(("ocr_done", generation, job_id, None))
        threading.Thread(target=worker, name="AB-OCR-document", daemon=True).start()

    def cancel(self):
        if self._busy and self._job_kind == "ocr":
            self._stop.set()
            self.stop_button.configure(state="disabled")
            self.status.set("중지 요청됨 · 완료한 쪽과 교정문은 보존합니다.")

    def _apply_ocr_page(self, result):
        self._commit_text()
        self.project.apply_recognition(result)
        number = result.page_number if hasattr(result, "page_number") else result["page_number"]
        self._ocr_counts["completed"] += 1
        self._update_page_row(number)
        if number == self.page_number:
            self._populate_text(preserve_edit=True)
            self._draw()
        self.progress.configure(value=self._ocr_counts["completed"] + self._ocr_counts["failed"])
        self._title()
        self._request_recovery()

    def show_raw(self):
        if not self.project:
            return
        raw = self.project.page(self.page_number).raw
        if raw is None:
            self.status.set("이 쪽에는 아직 원시 OCR 결과가 없습니다.")
            return
        dialog = tk.Toplevel(self)
        dialog.title(f"{self.page_number}쪽 원시 OCR · 읽기 전용")
        dialog.geometry("740x600")
        toolbar = ttk.Frame(dialog, padding=10)
        toolbar.pack(fill="x")
        mode = f"PSM {raw.get('psm', '')}" if raw.get("engine", "tesseract") == "tesseract" else "엔진 자체 영역 검출"
        ttk.Label(toolbar, text=f"{raw.get('engine', 'tesseract')} · {raw.get('languages', '')} · {mode} · {raw.get('engine_version', '')}", wraplength=680).pack(anchor="w")
        body = ttk.Frame(dialog, padding=(10, 0, 10, 10))
        body.pack(fill="both", expand=True)
        text = tk.Text(body, wrap="word", font=("맑은 고딕", 12), padx=10, pady=10)
        scroll = ttk.Scrollbar(body, command=text.yview)
        text.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        text.pack(fill="both", expand=True)
        text.insert("1.0", raw.get("text", ""))
        text.configure(state="disabled")

    def restore_raw(self):
        if not self.project or self._busy or self._close_after_recovery or self._deferred_open is not None:
            return
        self._commit_text()
        page = self.project.page(self.page_number)
        if page.raw is None:
            self.status.set("복원할 원시 OCR 결과가 없습니다.")
            return
        if page.corrected_text is not None and not messagebox.askyesno("원시 결과 복원", "이 쪽의 교정문을 버리고 원시 OCR 결과로 되돌릴까요?", parent=self):
            return
        self.project.restore_raw(self.page_number)
        self._populate_text()
        self._update_page_row(self.page_number)
        self._title()
        self._request_recovery()

    def save_project(self, path=None, *, save_as=False):
        if self._busy or not self.project or self._close_after_recovery or self._deferred_open is not None:
            return False
        self._commit_text()
        if path is None and (save_as or not self.project.project_path or self.project.project_path.parent == self.recovery_dir):
            path = filedialog.asksaveasfilename(parent=self, title="AB_OCR 프로젝트 저장", initialfile=self.project.source_path.stem + ".abocr.json", defaultextension=".abocr.json", filetypes=[("AB_OCR 프로젝트", "*.abocr.json")])
            if not path:
                return False
        project = self.project
        self._start_job("save", "프로젝트를 안전하게 저장하는 중…", lambda: project.save(path),
                        lambda destination: (self._title(), self.status.set(f"프로젝트 저장 완료 · {destination}")))
        return True

    def export_text(self, path=None, *, current_only=False, scope=None):
        if self._busy or not self.project or self._close_after_recovery or self._deferred_open is not None:
            return
        self._commit_text()
        if path is None and scope is None:
            dialog = tk.Toplevel(self)
            dialog.title("TXT 내보내기 범위")
            dialog.transient(self)
            dialog.resizable(False, False)
            body = ttk.Frame(dialog, padding=18)
            body.pack(fill="both", expand=True)
            ttk.Label(body, text="교정문을 내보낼 쪽을 선택하세요.").pack(anchor="w", pady=(0, 12))
            selected = tk.StringVar(dialog, "complete")
            for value, label in (("current", "현재 쪽"), ("complete", "인식 또는 직접 입력을 완료한 쪽"), ("all", "전체 쪽 — 미처리 쪽이 있으면 중단")):
                ttk.Radiobutton(body, text=label, value=value, variable=selected).pack(anchor="w", pady=4)
            def choose():
                value = selected.get()
                dialog.destroy()
                self.export_text(scope=value)
            ttk.Button(body, text="저장 위치 선택…", command=choose).pack(anchor="e", pady=(12, 0))
            dialog.grab_set()
            return
        if path is None:
            path = filedialog.asksaveasfilename(parent=self, title="교정문 TXT 내보내기", initialfile=self.project.source_path.stem + "_교정.txt", defaultextension=".txt", filetypes=[("UTF-8 텍스트", "*.txt")])
        if not path:
            return
        project = self.project
        numbers = [self.page_number] if current_only or scope == "current" else None
        if scope == "complete":
            numbers = [n for n in range(1, project.page_count + 1) if project.page(n).raw is not None or project.page(n).corrected_text is not None]
        self._start_job("export", "교정문을 TXT로 내보내는 중…", lambda: project.export_text(path, page_numbers=numbers),
                        lambda destination: self.status.set(f"TXT 내보내기 완료 · {destination}"))

    def _schedule_recovery(self):
        if self._recovery_after:
            self.after_cancel(self._recovery_after)
        self._recovery_after = self.after(1000, self._request_recovery)

    def _request_recovery(self):
        if self._recovery_after:
            self.after_cancel(self._recovery_after)
        self._recovery_after = None
        if not self.project or not self._recovery_path or self._closed:
            return
        if self._recovery_running:
            self._recovery_pending = True
            return
        project, path, generation = self.project, self._recovery_path, self._generation
        self._recovery_running = True
        self._recovery_generation = generation
        self._recovery_pending = False
        def worker():
            try:
                project.save_recovery(path)
                self._events.put(("recovery_done", generation, 0, None))
            except Exception as exc:
                self._events.put(("recovery_done", generation, 0, str(exc)))
        threading.Thread(target=worker, name="AB-OCR-recovery", daemon=True).start()

    def _start_job(self, kind, message, worker, done):
        self._busy, self._job_kind = True, kind
        self._job_id += 1
        job_id, generation = self._job_id, self._generation
        self._job_callback = done
        self.status.set(message)
        self.progress.configure(mode="indeterminate")
        self.progress.start(12)
        self._set_enabled()
        def run():
            try:
                self._events.put(("job_done", generation, job_id, worker()))
            except Exception as exc:
                self._events.put(("job_error", generation, job_id, str(exc)))
        threading.Thread(target=run, name=f"AB-OCR-{kind}", daemon=True).start()

    def _finish_job(self):
        self._busy = False
        self._job_kind = ""
        self.progress.stop()
        self.progress.configure(mode="determinate")
        self._set_enabled()

    def _poll(self):
        if self._closed:
            return
        try:
            while True:
                kind, generation, job_id, value = self._events.get_nowait()
                if kind == "recovery_done":
                    if generation != self._recovery_generation:
                        continue
                    self._recovery_running = False
                    if generation != self._generation:
                        if self._recovery_pending:
                            self._request_recovery()
                        continue
                    self._recovery_error = value
                    if value:
                        had_transition = self._close_after_recovery or self._deferred_open is not None
                        self._recovery_pending = False
                        self._close_after_recovery = False
                        self._deferred_open = None
                        self.save_state.set("자동 복구 저장 실패")
                        self.status.set("자동 복구 저장 실패 · 프로젝트 저장으로 별도 보관하세요. " + value)
                        self._set_enabled()
                        if had_transition:
                            messagebox.showerror("자동 복구 저장 실패", value + "\n프로젝트를 직접 저장한 뒤 다시 시도하세요.", parent=self)
                        continue
                    self.save_state.set("자동 복구 사본 보관됨")
                    if self._recovery_pending:
                        self._request_recovery()
                    elif self._close_after_recovery:
                        self._close_after_recovery = False
                        self._destroy_window()
                        return
                    elif self._deferred_open is not None:
                        next_document = self._deferred_open
                        self._deferred_open = None
                        self._load_document(*next_document)
                    continue
                if generation != self._generation:
                    if kind == "render":
                        value.close()
                    continue
                if kind in ("render", "render_error"):
                    if job_id != self._render_id:
                        if kind == "render":
                            value.close()
                        continue
                    if kind == "render":
                        self._preview = value
                        self._draw()
                    else:
                        self.canvas.delete("all")
                        self.canvas.create_text(25, 35, anchor="nw", text="원본 표시 실패\n" + value, width=max(200, self.canvas.winfo_width() - 50), fill="#9d3f31")
                    continue
                if job_id != self._job_id:
                    continue
                if kind == "ocr_progress":
                    if not self._stop.is_set():
                        self.status.set(f"{value}쪽 인식 중 · 완료 {self._ocr_counts['completed']} / {self._ocr_counts['total']} · 교정 가능")
                elif kind == "ocr_page":
                    try:
                        self._apply_ocr_page(value)
                    except Exception as exc:
                        self._ocr_counts["failed"] += 1
                        self.progress.configure(value=self._ocr_counts["completed"] + self._ocr_counts["failed"])
                        number = value.page_number if hasattr(value, "page_number") else value.get("page_number")
                        if type(number) is int and 1 <= number <= self.project.page_count:
                            self.project.record_error(number, str(exc))
                            self._update_page_row(number)
                            if number == self.page_number:
                                self.error_var.set(str(exc))
                            self._request_recovery()
                        self.status.set("인식 결과 적용 실패 · " + str(exc))
                elif kind == "ocr_error":
                    number, error = value
                    self.project.record_error(number, error)
                    self._ocr_counts["failed"] += 1
                    self.progress.configure(value=self._ocr_counts["completed"] + self._ocr_counts["failed"])
                    self._update_page_row(number)
                    if number == self.page_number:
                        self.error_var.set(error)
                    self._request_recovery()
                elif kind == "ocr_done":
                    self._finish_job()
                    counts = self._ocr_counts
                    self.status.set(f"{'중지됨' if self._stop.is_set() else '인식 완료'} · 성공 {counts['completed']}쪽 · 실패 {counts['failed']}쪽 · 완료 결과는 자동 복구 사본에 보관합니다.")
                    self._title()
                    if self._close_when_idle:
                        self._close_when_idle = False
                        self._close()
                elif kind in ("job_done", "job_error"):
                    self._finish_job()
                    if kind == "job_error":
                        self._close_when_idle = False
                        self.status.set("작업 실패 · 기존 원본과 교정문을 보존했습니다.")
                        messagebox.showerror("AB_OCR 작업 오류", value, parent=self)
                    else:
                        try:
                            self._job_callback(value)
                        except Exception as exc:
                            self._close_when_idle = False
                            messagebox.showerror("결과 표시 오류", str(exc), parent=self)
                        if self._close_when_idle:
                            self._close_when_idle = False
                            self._close()
                if self._closed:
                    return
        except queue.Empty:
            pass
        self._poll_id = self.after(60, self._poll)

    def open_evaluation(self):
        options = {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}
        try:
            subprocess.Popen([sys.executable, "-m", "ab_ocr", "evaluate-gui"], **options)
        except OSError as exc:
            messagebox.showerror("정확도 평가 실행", str(exc), parent=self)

    def _close(self, *, force=False):
        if self._closed:
            return
        if force:
            self._destroy_window()
            return
        if self._close_after_recovery:
            return
        if self._deferred_open is not None:
            self._close_when_idle = True
            return
        if self._busy:
            self._close_when_idle = True
            if self._job_kind == "ocr":
                self.cancel()
                self.status.set("인식을 중지하고 완료한 쪽을 보관한 뒤 닫습니다.")
            else:
                self.status.set("현재 파일 작업을 마친 뒤 창을 닫습니다.")
            return
        self._commit_text()
        if self.project and self.project.dirty:
            answer = messagebox.askyesnocancel("프로젝트 저장", "교정문과 인식 결과를 프로젝트에 저장한 뒤 닫을까요?\n[아니요]를 선택해도 자동 복구 사본은 남습니다.", parent=self)
            if answer is None:
                return
            if answer:
                if self.save_project():
                    self._close_when_idle = True
                return
        if self.project and (self.project.dirty or self._recovery_running):
            self._close_after_recovery = True
            self._set_enabled()
            self._request_recovery()
            self.status.set("자동 복구 사본을 마저 저장한 뒤 닫습니다.")
            return
        self._destroy_window()

    def _destroy_window(self):
        self._closed = True
        self._stop.set()
        self._generation += 1
        for token in (self._poll_id, self._resize_after, self._recovery_after):
            if token:
                self.after_cancel(token)
        if self._preview is not None:
            self._preview.close()
        self.destroy()


def main(argv=None):
    parser = argparse.ArgumentParser(description="AB_OCR 스캔 원본 대조 편집기")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--source", type=Path, help="열 PDF 또는 스캔 이미지")
    group.add_argument("--project", type=Path, help="다시 열 AB_OCR 프로젝트")
    args = parser.parse_args(argv)
    app = EditorWindow(source_path=args.source, project_path=args.project)
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
