"""Local multi-engine review window, hosted by the existing YeonE OCR application.

Recognition and preview rendering only publish messages to the UI queue.  Original
engine records belong to ComparisonProject; editing widgets never write into them.
"""
from __future__ import annotations

import difflib
from dataclasses import replace
import gc
import hashlib
import json
from pathlib import Path
import queue
import threading
import tkinter as tk
from tkinter import filedialog, ttk, font as tkfont
import uuid

from ab_ocr.engine_registry import (ENGINES, ENGINE_LABELS, LEGACY_ENGINES,
                                    engine_set, primary_engine)

PROFILES = {"번체·한문": "traditional", "간체·중문": "simplified", "한글·한자 혼용": "mixed"}
DIRECTIONS = {"가로쓰기": "horizontal", "세로쓰기": "vertical"}
ENGINE_MODES = {"권장 비교": "recommended", "기존 3엔진": "legacy",
                f"전체 {len(ENGINES)}엔진": "all", "주력만": "primary", "PP-OCRv6만": "ppocrv6"}
RAW_TAB_LABELS = {"tesseract": "Tess", "rapidocr": "Rapid", "easyocr": "Easy",
                  "paddle_server": "Paddle", "glmocr": "GLM", "surya": "Surya", "ppocrv6": "PP-v6"}


def default_selected_engines(engine_root=None):
    """A self-contained installer starts with its included engine selected."""
    from ab_ocr.runtime import engine_root_for
    path = engine_root_for("ppocrv6", engine_root) / "tools/ppocrv6/installation.json"
    try:
        if path.stat().st_size <= 1_000_000:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
            if (isinstance(data, dict) and data.get("engine") == "ppocrv6"
                    and data.get("runtime_layout") == "standalone-python"):
                return ("ppocrv6",)
    except (OSError, ValueError):
        pass
    return LEGACY_ENGINES


def parse_page_range(value: str, page_count: int) -> list[int]:
    """Parse explicit one-based ranges; invalid input must never expand scope."""
    if not value.strip():
        return list(range(1, page_count + 1))
    result = set()
    for part in value.replace("，", ",").split(","):
        bounds = part.strip().split("-")
        if not 1 <= len(bounds) <= 2 or any(not b.strip().isascii() or not b.strip().isdigit() for b in bounds):
            raise ValueError("쪽 범위는 1-5,8처럼 입력하십시오.")
        first = int(bounds[0])
        last = int(bounds[-1])
        if not 1 <= first <= last <= page_count:
            raise ValueError(f"쪽 범위는 1~{page_count} 안에서 작은 쪽부터 입력하십시오.")
        result.update(range(first, last + 1))
    return sorted(result)


def mapped_span(baseline: str, corrected: str, start: int, end: int):
    """Locate unchanged baseline evidence after unrelated edits; never guess."""
    if baseline == corrected:
        return start, end
    for block in difflib.SequenceMatcher(None, baseline, corrected, autojunk=False).get_matching_blocks():
        if block.a <= start <= end <= block.a + block.size and (end > start or block.size):
            return block.b + start - block.a, block.b + end - block.a
    return None


def _short(value, limit=42):
    value = str(value).replace("\n", "↵").replace("\t", "⇥")
    return (value[:limit] + "…") if len(value) > limit else value or "∅"


def _page_labels(numbers):
    """Show exact page scope compactly without dropping any selected page."""
    if not isinstance(numbers, (list, tuple)) or any(type(number) is not int for number in numbers):
        return str(numbers)
    ordered = sorted(set(numbers))
    groups = []
    for number in ordered:
        if groups and groups[-1][-1] + 1 == number:
            groups[-1].append(number)
        else:
            groups.append([number])
    return ", ".join(str(group[0]) if len(group) == 1 else f"{group[0]}-{group[-1]}" for group in groups)


def _anchor_recovery_notice(record, proposal=None):
    """Describe deterministic position recovery separately from the AI answer."""
    recovery = record.get("metadata", {}).get("anchor_recovery", {})
    if recovery.get("policy") != "unique-literal-v1":
        return ""
    relocations = recovery.get("relocations", [])
    if proposal is None:
        return f"위치 복원 {len(relocations)}곳: 같은 검증 범위에서 원문이 한 곳에만 일치하는지 확인했습니다."
    before_sha256 = hashlib.sha256(proposal.get("before", "").encode("utf-8")).hexdigest()
    matches = [item for item in relocations if item.get("start") == proposal.get("start")
               and item.get("end") == proposal.get("end") and item.get("before_sha256") == before_sha256]
    if not matches:
        return ""
    item = matches[0]
    return (f"위치 복원: AI가 지정한 {item['original_start']}–{item['original_end']}에서 "
            f"원문이 한 곳에만 일치하는 {item['start']}–{item['end']}로 복원했습니다. "
            "문자 위치는 0부터 셉니다. AI 원시 응답은 이력에 그대로 보존됩니다.")


class ComparisonEditor(tk.Toplevel):
    """A Toplevel, not a second Tk root or a replacement of the existing GUI."""

    def __init__(self, master, source_path=None, project_path=None, *, work_dir=None,
                 engine_root=None, project=None, on_export=None, profile=None, direction=None, on_engine_root=None,
                 on_ai_settings=None):
        super().__init__(master)
        self.title("연어OCR · OCR 모델 비교와 원본 대조 편집")
        self.geometry("1440x930")
        self.minsize(1080, 720)
        self.project = None
        self.work_dir = Path(work_dir) if work_dir else None
        self.engine_root = Path(engine_root) if engine_root else None
        self.on_export = on_export
        self.on_engine_root = on_engine_root
        self.on_ai_settings = on_ai_settings
        self.page_number = 1
        self._generation = 0
        self._job_id = 0
        self._render_id = 0
        self._events = queue.Queue()
        self._stop = threading.Event()
        self._busy = False
        self._closed = False
        self._closed_event = threading.Event()
        self._closing = False
        self._loading = False
        self._pending_edit = False
        self._edit_after = None
        self._recovery_after = None
        self._draw_after = None
        self._layout_after = None
        self._startup_after = None
        self._last_window_size = None
        self._preview = None
        self._photo = None
        self._zoom = None
        self._origin = (0, 0)
        self._image_scale = 1.0
        self._selected_suggestion = None
        self._suggestions = {}
        self._ai_proposals = {}
        self._selected_ai_proposal = None
        self._selected_refinement = None
        self._recognition_engine_count = len(LEGACY_ENGINES)
        self._recovery_path = None
        self._tooltips = []
        self._controls = []
        self._document_controls = []
        self._local_styles()
        self._build()
        self._apply_local_styles(self)
        self._layout_after = self.after_idle(self._layout_panes)
        if profile in PROFILES.values():
            self.profile.set(next(label for label, value in PROFILES.items() if value == profile))
        if direction in DIRECTIONS.values():
            self.direction.set(next(label for label, value in DIRECTIONS.items() if value == direction))
        self._settings_changed()
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.bind("<Control-s>", self._key_save)
        self.bind("<Control-z>", self._key_undo)
        self.bind("<Escape>", lambda _event: self.cancel())
        self.bind("<Destroy>", self._destroyed, add="+")
        self.bind("<Configure>", self._window_resized, add="+")
        self._poll_id = self.after(60, self._poll)
        self._set_enabled()
        if project is not None:
            self._set_project(project)
        elif project_path:
            self._startup_after = self.after_idle(lambda: self.open_project(project_path))
        elif source_path:
            self._startup_after = self.after_idle(lambda: self.open_source(source_path))

    def _button(self, parent, text, command, document=False):
        button = ttk.Button(parent, text=text, command=command, padding=(7, 5))
        self._controls.append(button)
        if document:
            self._document_controls.append(button)
        return button

    def _local_styles(self):
        """Use compact native elements without modifying the host's ttk skin."""
        style = ttk.Style(self)
        self._body_font = ("맑은 고딕", 9)
        row_height = tkfont.Font(self, font=self._body_font).metrics("linespace") + 5
        layouts = {
            "TButton": style.layout("TButton"),
            "TEntry": [("Entry.field", {"sticky": "nswe", "border": "1", "children": [("Entry.padding", {"sticky": "nswe", "children": [("Entry.textarea", {"sticky": "nswe"})]})]})],
            "TCombobox": [("Combobox.downarrow", {"side": "right", "sticky": "ns"}), ("Combobox.field", {"sticky": "nswe", "children": [("Combobox.padding", {"sticky": "nswe", "children": [("Combobox.textarea", {"sticky": "nswe"})]})]})],
            "TCheckbutton": [("Checkbutton.padding", {"sticky": "nswe", "children": [("Checkbutton.indicator", {"side": "left", "sticky": ""}), ("Checkbutton.focus", {"side": "left", "sticky": "w", "children": [("Checkbutton.label", {"sticky": "nswe"})]})]})],
            "TNotebook": [("Notebook.client", {"sticky": "nswe"})],
            "TNotebook.Tab": [("Notebook.tab", {"sticky": "nswe", "children": [("Notebook.padding", {"side": "top", "sticky": "nswe", "children": [("Notebook.focus", {"side": "top", "sticky": "nswe", "children": [("Notebook.label", {"side": "top", "sticky": ""})]})]})]})],
        }
        for name, layout in layouts.items():
            style.layout("YECompare." + name, layout)
        for name in ("TButton", "TLabel", "TEntry", "TCombobox", "TCheckbutton", "Treeview", "Treeview.Heading", "TNotebook.Tab"):
            style.configure("YECompare." + name, font=self._body_font, padding=0)
        style.configure("YECompare.TButton", padding=(6, 2), width=0)
        style.map("YECompare.TButton", font=[("disabled", self._body_font)])
        style.configure("YECompare.TCombobox", padding=(3, 1))
        style.configure("YECompare.TEntry", padding=(3, 1))
        style.configure("YECompare.TCheckbutton", padding=(0, 1))
        style.configure("YECompare.Treeview", rowheight=row_height)
        style.configure("YECompare.Treeview.Heading", padding=(3, 2), font=("맑은 고딕", 9, "bold"))
        style.configure("YECompare.TNotebook.Tab", padding=(5, 2))

    def _apply_local_styles(self, widget):
        classes = {"TButton", "TLabel", "TEntry", "TCombobox", "TCheckbutton", "Treeview", "TNotebook"}
        kind = widget.winfo_class()
        if kind in classes:
            widget.configure(style="YECompare." + kind)
        if kind == "TButton":
            widget.configure(padding=(6, 2))
        if kind in ("TEntry", "TCombobox"):
            widget.configure(font=self._body_font)
        for child in widget.winfo_children():
            self._apply_local_styles(child)

    def _layout_panes(self):
        self._layout_after = None
        if self._closed:
            return
        height, width = self.review_panes.winfo_height(), self.main_panes.winfo_width()
        if height < 100 or width < 100:
            self._layout_after = self.after(80, self._layout_panes)
            return
        self.main_panes.sashpos(0, 162)
        self.main_panes.sashpos(1, 162 + round((width - 162) * .43))
        self.review_panes.sashpos(0, round(height * .26))
        self.review_panes.sashpos(1, round(height * .64))

    def _window_resized(self, event):
        if event.widget is not self or self._closed:
            return
        size = event.width, event.height
        if size == self._last_window_size:
            return
        self._last_window_size = size
        if event.height < 820:
            self._header_frame.grid_remove()
            self._hint_label.pack_forget()
        else:
            self._header_frame.grid()
            self._hint_label.pack(fill="x", pady=(4, 0))
        if self._layout_after:
            self.after_cancel(self._layout_after)
        self._layout_after = self.after(100, self._layout_panes)

    def _tooltip(self, widget, variable):
        """Single-line captions retain their complete explanation on hover."""
        state = {"after": None, "window": None}
        self._tooltips.append(state)
        def hide(_event=None):
            if state["after"]:
                self.after_cancel(state["after"])
                state["after"] = None
            if state["window"]:
                state["window"].destroy()
                state["window"] = None
        def show():
            state["after"] = None
            if self._closed:
                return
            popup = tk.Toplevel(self)
            popup.overrideredirect(True)
            popup.geometry(f"+{widget.winfo_rootx()}+{widget.winfo_rooty() + widget.winfo_height() + 4}")
            tk.Label(popup, text=variable.get(), justify="left", wraplength=650, font=self._body_font,
                     background="#fff8df", foreground="#30343a", padx=8, pady=6, relief="solid", borderwidth=1).pack()
            state["window"] = popup
        def schedule(_event=None):
            hide()
            state["after"] = self.after(500, show)
        widget.bind("<Enter>", schedule, add="+")
        widget.bind("<Leave>", hide, add="+")

    def _build(self):
        self.columnconfigure(0, weight=1)
        self.rowconfigure(3, weight=1)
        heading = ttk.Frame(self, padding=(12, 10, 12, 6))
        self._header_frame = heading
        heading.grid(row=0, column=0, sticky="ew")
        ttk.Label(heading, text="OCR 모델 비교 · 원본 대조 편집", font=("맑은 고딕", 12, "bold")).pack(side="left")
        self.document_label = tk.StringVar(self, "원본 PDF 또는 이미지를 열어 시작하세요")
        document_label = ttk.Label(heading, textvariable=self.document_label, anchor="e", width=1)
        document_label.pack(side="right", fill="x", expand=True, padx=15)
        self._tooltip(document_label, self.document_label)

        toolbar = ttk.Frame(self, padding=(12, 0, 12, 6))
        toolbar.grid(row=1, column=0, sticky="ew")
        for text, action, document in (("원본 열기…", self.open_source, False),
                ("프로젝트 열기…", self.open_project, False), ("저장", self.save_project, True),
                ("다른 이름으로…", lambda: self.save_project(save_as=True), True),
                ("기록 가져오기…", self.import_records, True), ("복구 사본…", self.open_recovery, False),
                ("내보내기…", self.export_document, True), ("엔진 폴더…", self.choose_engine_root, False),
                ("설치 확인", self.inspect_engines, False)):
            self._button(toolbar, text, action, document).pack(side="left", padx=(0, 5))

        settings = ttk.Frame(self, padding=(12, 0, 12, 6))
        settings.grid(row=2, column=0, sticky="ew")
        line = ttk.Frame(settings)
        line.pack(fill="x")
        self.profile = tk.StringVar(self, next(iter(PROFILES)))
        self.direction = tk.StringVar(self, next(iter(DIRECTIONS)))
        self.dpi = tk.StringVar(self, "300")
        self.range_var = tk.StringVar(self, "")
        self.force = tk.BooleanVar(self, False)
        self.engine_mode = tk.StringVar(self, next(iter(ENGINE_MODES)))
        self.setting_widgets = []
        for label, variable, values, width in (("문서", self.profile, list(PROFILES), 16),
                ("읽기", self.direction, list(DIRECTIONS), 11), ("PDF DPI", self.dpi, (200, 300, 400, 600), 5)):
            ttk.Label(line, text=label).pack(side="left", padx=(0, 5))
            combo = ttk.Combobox(line, textvariable=variable, values=values, state="readonly", width=width)
            combo.pack(side="left", padx=(0, 12))
            self.setting_widgets.append(combo)
            combo.bind("<<ComboboxSelected>>", self._settings_changed)
        ttk.Label(line, text="쪽 범위").pack(side="left", padx=(0, 5))
        self.range_entry = ttk.Entry(line, textvariable=self.range_var, width=14)
        self.range_entry.pack(side="left")
        ttk.Label(line, text="예: 1-5,8 · 빈칸은 전체").pack(side="left", padx=7)
        model_line = ttk.Frame(settings)
        model_line.pack(fill="x", pady=(5, 0))
        ttk.Label(model_line, text="판독 모델").pack(side="left", padx=(0, 5))
        self.engine_mode_box = ttk.Combobox(model_line, textvariable=self.engine_mode,
                                           values=list(ENGINE_MODES), state="readonly", width=13)
        self.engine_mode_box.pack(side="left", padx=(0, 10))
        self.engine_mode_box.bind("<<ComboboxSelected>>", self._settings_changed)
        self.setting_widgets.append(self.engine_mode_box)
        self.model_summary = tk.StringVar(self)
        self.model_details = tk.StringVar(self)
        model_summary = ttk.Label(model_line, textvariable=self.model_summary, width=1)
        model_summary.pack(side="left", fill="x", expand=True)
        self._tooltip(model_summary, self.model_details)
        actions = ttk.Frame(settings)
        actions.pack(fill="x", pady=(6, 0))
        self._button(actions, "현재 쪽 인식", lambda: self.recognize(False), True).pack(side="left", padx=(0, 5))
        self._button(actions, "지정 범위 · 이어 읽기", lambda: self.recognize(True), True).pack(side="left", padx=(0, 5))
        self.stop_button = ttk.Button(actions, text="중지", command=self.cancel, state="disabled")
        self.stop_button.pack(side="left")
        self.progress = ttk.Progressbar(actions, mode="determinate", maximum=1, length=160)
        self.progress.pack(side="left", padx=10)
        self.progress_label = tk.StringVar(self, "준비")
        ttk.Label(actions, textvariable=self.progress_label).pack(side="left")
        self.force_check = ttk.Checkbutton(actions, text="완료한 엔진도 다시 읽기 (이전 기록 보존)", variable=self.force)
        self.force_check.pack(side="right")
        self.hint = tk.StringVar(self)
        hint_label = ttk.Label(settings, textvariable=self.hint, foreground="#70553d", width=1)
        self._hint_label = hint_label
        hint_label.pack(fill="x", pady=(4, 0))
        self._tooltip(hint_label, self.hint)
        self._settings_changed()

        panes = ttk.Panedwindow(self, orient="horizontal")
        self.main_panes = panes
        panes.grid(row=3, column=0, sticky="nsew", padx=12)
        pages = ttk.Frame(panes, padding=5, width=140)
        pages.rowconfigure(1, weight=1)
        pages.columnconfigure(0, weight=1)
        panes.add(pages, weight=0)
        ttk.Label(pages, text="쪽별 진행", font=("맑은 고딕", 10, "bold")).grid(row=0, column=0, sticky="w", pady=(0, 5))
        self.page_tree = ttk.Treeview(pages, columns=("number", "state"), show="headings", selectmode="browse", height=15)
        self.page_tree.heading("number", text="쪽")
        self.page_tree.heading("state", text="인식·검수")
        self.page_tree.column("number", width=35, stretch=False, anchor="center")
        self.page_tree.column("state", width=96, stretch=False)
        self.page_tree.grid(row=1, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(pages, command=self.page_tree.yview)
        scrollbar.grid(row=1, column=1, sticky="ns")
        self.page_tree.configure(yscrollcommand=scrollbar.set)
        self.page_tree.bind("<<TreeviewSelect>>", self._page_selected)

        original = ttk.Frame(panes, padding=5)
        original.columnconfigure(0, weight=1)
        original.rowconfigure(1, weight=1)
        panes.add(original, weight=3)
        zoom = ttk.Frame(original)
        zoom.grid(row=0, column=0, sticky="ew", pady=(0, 5))
        ttk.Label(zoom, text="원본 이미지", font=("맑은 고딕", 11, "bold")).pack(side="left")
        self.zoom_label = tk.StringVar(self, "맞춤")
        for label, action in (("−", lambda: self.change_zoom(.8)), ("+", lambda: self.change_zoom(1.25)), ("맞춤", self.fit_page)):
            ttk.Button(zoom, text=label, command=action, width=4).pack(side="right", padx=2)
        ttk.Label(zoom, textvariable=self.zoom_label).pack(side="right", padx=5)
        canvas_frame = ttk.Frame(original)
        canvas_frame.grid(row=1, column=0, sticky="nsew")
        canvas_frame.rowconfigure(0, weight=1)
        canvas_frame.columnconfigure(0, weight=1)
        self.canvas = tk.Canvas(canvas_frame, background="#e4e6e9", highlightthickness=0, width=380, height=360)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        vertical = ttk.Scrollbar(canvas_frame, command=self.canvas.yview)
        horizontal = ttk.Scrollbar(canvas_frame, orient="horizontal", command=self.canvas.xview)
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal.grid(row=1, column=0, sticky="ew")
        self.canvas.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        self.canvas.bind("<Configure>", self._schedule_draw)
        self.canvas.bind("<ButtonPress-1>", lambda event: self.canvas.scan_mark(event.x, event.y))
        self.canvas.bind("<B1-Motion>", lambda event: self.canvas.scan_dragto(event.x, event.y, gain=1))
        self.canvas.bind("<MouseWheel>", lambda event: self.canvas.yview_scroll(-1 if event.delta > 0 else 1, "units"))
        self.canvas.bind("<Control-MouseWheel>", self._wheel_zoom)
        navigation = ttk.Frame(original)
        navigation.grid(row=2, column=0, sticky="ew", pady=5)
        self.previous_button = ttk.Button(navigation, text="◀ 이전", command=lambda: self.show_page(self.page_number - 1))
        self.previous_button.pack(side="left")
        self.next_button = ttk.Button(navigation, text="다음 ▶", command=lambda: self.show_page(self.page_number + 1))
        self.next_button.pack(side="right")
        self.page_label = tk.StringVar(self, "0 / 0쪽")
        ttk.Label(navigation, textvariable=self.page_label, anchor="center").pack(fill="x", expand=True)
        self.evidence_label = tk.StringVar(self, "불일치를 고르면 근거 위치를 표시합니다. 드래그로 이동 · Ctrl+휠로 확대")
        evidence_label = ttk.Label(original, textvariable=self.evidence_label, width=1, foreground="#5d6670")
        evidence_label.grid(row=3, column=0, sticky="ew")
        self._tooltip(evidence_label, self.evidence_label)

        review = ttk.Panedwindow(panes, orient="vertical")
        self.review_panes = review
        panes.add(review, weight=4)
        raw_frame = ttk.Frame(review, padding=5)
        review.add(raw_frame, weight=2)
        raw_frame.columnconfigure(0, weight=1)
        raw_frame.rowconfigure(1, weight=1)
        baseline_bar = ttk.Frame(raw_frame)
        baseline_bar.grid(row=0, column=0, sticky="ew", pady=(0, 5))
        ttk.Label(baseline_bar, text="원시 OCR · 읽기 전용", font=("맑은 고딕", 10, "bold")).pack(side="left")
        self.baseline = tk.StringVar(self, "easyocr")
        self.baseline_box = ttk.Combobox(baseline_bar, textvariable=self.baseline, values=ENGINES, state="readonly", width=11)
        self.baseline_box.pack(side="right")
        self.baseline_box.bind("<<ComboboxSelected>>", self.change_baseline)
        ttk.Label(baseline_bar, text="비교 기준").pack(side="right", padx=5)
        self.raw_notebook = ttk.Notebook(raw_frame)
        self.raw_notebook.grid(row=1, column=0, sticky="nsew")
        self.raw_widgets = {}
        self.raw_tabs = {}
        for engine in ENGINES:
            self._add_raw_tab(engine)
        self.raw_note = tk.StringVar(self, "엔진 신뢰도는 정확도 수치가 아니며 엔진끼리 직접 비교할 수 없습니다.")
        self._tooltip(baseline_bar, self.raw_note)
        self._tooltip(self.raw_notebook, self.raw_note)

        edit_frame = ttk.Frame(review, padding=5)
        edit_frame.columnconfigure(0, weight=1)
        edit_frame.rowconfigure(1, weight=1)
        review.add(edit_frame, weight=3)
        editbar = ttk.Frame(edit_frame)
        editbar.grid(row=0, column=0, sticky="ew", pady=(0, 5))
        ttk.Label(editbar, text="교정문", font=("맑은 고딕", 11, "bold")).pack(side="left")
        self.char_label = tk.StringVar(self, "0자")
        ttk.Label(editbar, textvariable=self.char_label).pack(side="left", padx=8)
        self.undo_button = ttk.Button(editbar, text="되돌리기", command=self.undo_edit)
        self.undo_button.pack(side="right")
        ttk.Button(editbar, text="교정 이력", command=self.show_history).pack(side="right", padx=4)
        self.text = tk.Text(edit_frame, wrap="word", font=("맑은 고딕", 10), height=7, width=44, undo=False, padx=8, pady=5)
        self.text.grid(row=1, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(edit_frame, command=self.text.yview)
        scroll.grid(row=1, column=1, sticky="ns")
        self.text.configure(yscrollcommand=scroll.set)
        self.text.bind("<<Modified>>", self._text_changed)
        self.text.tag_configure("difference", background="#fff0b8")
        self.text.tag_configure("selection-evidence", background="#cce7ff")
        self.reviewed = tk.BooleanVar(self, False)
        self.review_check = ttk.Checkbutton(edit_frame, text="이 쪽을 원본과 대조했습니다 (사람)", variable=self.reviewed, command=self.mark_reviewed)
        self.review_check.grid(row=2, column=0, sticky="w", pady=(4, 0))

        review_bottom = ttk.Frame(review, padding=5)
        review_bottom.rowconfigure(0, weight=1)
        review_bottom.columnconfigure(0, weight=1)
        self.proposal_notebook = ttk.Notebook(review_bottom)
        self.proposal_notebook.grid(row=0, column=0, sticky="nsew")
        differences = ttk.Frame(self.proposal_notebook)
        self.proposal_notebook.add(differences, text="엔진 불일치")
        differences.columnconfigure(0, weight=1)
        differences.rowconfigure(1, weight=1)
        review.add(review_bottom, weight=2)
        diffbar = ttk.Frame(differences)
        diffbar.grid(row=0, column=0, sticky="ew", pady=(0, 4))
        self.diff_label = tk.StringVar(self, "불일치")
        ttk.Label(diffbar, textvariable=self.diff_label, width=1, font=("맑은 고딕", 10, "bold")).pack(side="left", fill="x", expand=True)
        self.compare_button = ttk.Button(diffbar, text="갱신", command=self.compare_current)
        self.compare_button.pack(side="right")
        self.diff_tree = ttk.Treeview(differences, columns=("base", "candidates", "state"), show="headings", selectmode="browse", height=4)
        for name, label, width in (("base", "기준 판독", 110), ("candidates", "다른 판독", 240), ("state", "처리", 65)):
            self.diff_tree.heading(name, text=label)
            self.diff_tree.column(name, width=width, minwidth=50, stretch=name == "candidates")
        self.diff_tree.grid(row=1, column=0, sticky="nsew")
        diffscroll = ttk.Scrollbar(differences, command=self.diff_tree.yview)
        diffscroll.grid(row=1, column=1, sticky="ns")
        self.diff_tree.configure(yscrollcommand=diffscroll.set)
        self.diff_tree.bind("<<TreeviewSelect>>", self._difference_selected)
        self.diff_tree.bind("<Double-1>", self.show_candidate_details)
        candidatebar = diffbar
        self.candidate = tk.StringVar(self, "")
        self.candidate_box = ttk.Combobox(candidatebar, textvariable=self.candidate, state="readonly", width=10)
        self.candidate_box.pack(side="left")
        self.candidate_box.bind("<<ComboboxSelected>>", self._candidate_changed)
        self.accept_button = ttk.Button(candidatebar, text="채택", command=self.accept_candidate)
        self.accept_button.pack(side="left", padx=5)
        self.reject_button = ttk.Button(candidatebar, text="기준 유지", command=self.reject_candidate)
        self.reject_button.pack(side="left")
        self.candidate_detail = tk.StringVar(self, "후보를 선택하면 전체 판독을 확인할 수 있습니다.")
        self._tooltip(self.candidate_box, self.candidate_detail)
        self._tooltip(self.diff_tree, self.candidate_detail)

        ai_frame = ttk.Frame(self.proposal_notebook)
        ai_frame.columnconfigure(0, weight=1)
        ai_frame.rowconfigure(1, weight=1)
        self.proposal_notebook.add(ai_frame, text="AI 검증 제안")
        ai_bar = ttk.Frame(ai_frame)
        ai_bar.grid(row=0, column=0, sticky="ew", pady=(0, 4))
        self.ai_count = tk.StringVar(self, "제안 0곳")
        ttk.Label(ai_bar, textvariable=self.ai_count, width=1).pack(side="left", fill="x", expand=True)
        self.ai_start_button = ttk.Button(ai_bar, text="AI 검증…", command=self.open_ai_verification)
        self.ai_start_button.pack(side="left", padx=3)
        self.ai_history_button = ttk.Button(ai_bar, text="이력", command=self.show_ai_history)
        self.ai_history_button.pack(side="left", padx=3)
        self.ai_accept_button = ttk.Button(ai_bar, text="채택", command=self.accept_ai_proposal)
        self.ai_accept_button.pack(side="left", padx=3)
        self.ai_reject_button = ttk.Button(ai_bar, text="거절", command=self.reject_ai_proposal)
        self.ai_reject_button.pack(side="left")
        self.ai_tree = ttk.Treeview(ai_frame, columns=("before", "after", "kind", "state"), show="headings", selectmode="browse", height=4)
        for name, label, width in (("before", "변경 전", 110), ("after", "AI 제안", 170), ("kind", "종류", 65), ("state", "처리", 65)):
            self.ai_tree.heading(name, text=label)
            self.ai_tree.column(name, width=width, minwidth=40, stretch=name == "after")
        self.ai_tree.grid(row=1, column=0, sticky="nsew")
        ai_scroll = ttk.Scrollbar(ai_frame, command=self.ai_tree.yview)
        ai_scroll.grid(row=1, column=1, sticky="ns")
        self.ai_tree.configure(yscrollcommand=ai_scroll.set)
        self.ai_tree.bind("<<TreeviewSelect>>", self._ai_selected)
        self.ai_tree.bind("<Double-1>", self.show_ai_details)
        self.ai_detail = tk.StringVar(self, "원본 이미지와 OCR을 AI에 보내 검증합니다. 실행 범위 확인 뒤에만 전송하며 교정문은 자동으로 바꾸지 않습니다.")
        self._tooltip(self.ai_tree, self.ai_detail)
        self._tooltip(self.ai_start_button, self.ai_detail)

        from .comparison_refinement_ui import RefinementPanel
        self.refinement_panel = RefinementPanel(self, self.proposal_notebook)
        self.proposal_notebook.add(self.refinement_panel, text="로컬 교정·영역")

        footer = ttk.Frame(self, padding=(12, 6, 12, 8))
        footer.grid(row=4, column=0, sticky="ew")
        self.status = tk.StringVar(self, "모델별 원시 판독·AI 제안·사람의 교정 이력을 각각 보존합니다.")
        self.recovery_close_button = ttk.Button(footer, text="복구 사본을 보존하고 닫기", command=self._close_from_recovery)
        status_label = ttk.Label(footer, textvariable=self.status, anchor="w", width=1)
        status_label.pack(side="left", fill="x", expand=True)
        self._tooltip(status_label, self.status)

    def _add_raw_tab(self, engine):
        frame = ttk.Frame(self.raw_notebook)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)
        widget = tk.Text(frame, wrap="word", font=("맑은 고딕", 10), height=5, width=44, padx=6, pady=5, state="disabled")
        widget.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(frame, command=widget.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        widget.configure(yscrollcommand=scroll.set)
        widget.tag_configure("evidence", background="#ffe6aa")
        self.raw_notebook.add(frame, text=RAW_TAB_LABELS.get(engine, engine))
        self.raw_tabs[engine] = frame
        self.raw_widgets[engine] = widget
        self._apply_local_styles(frame)

    def _settings_changed(self, _event=None):
        selected = self._selected_engines()
        preferred = primary_engine(PROFILES[self.profile.get()], DIRECTIONS[self.direction.get()])
        baseline_note = ("새 쪽 기준: " + ENGINE_LABELS[preferred]
                         if preferred in selected else "기존 3엔진의 기준 판독 사용")
        self.model_summary.set(baseline_note + " · " + str(len(selected)) + "개 모델")
        self.model_details.set(self.model_summary.get() + "\n"
                               + ", ".join(ENGINE_LABELS[engine] for engine in selected))
        if self.direction.get() == "세로쓰기":
            hint = "세로 비교는 시험 기능입니다. 전용 모델·지원 범위가 다르며 원문 순서를 확인해야 합니다. 기존 AI 세로 판독은 기존 작업 탭에서 사용할 수 있습니다."
        elif self.profile.get() == "한글·한자 혼용":
            hint = "혼용: 모델마다 한글·한자 지원 범위가 다릅니다. 기존 한국어 전용 모델의 한자 누락을 다수결로 삭제하지 않습니다."
        else:
            hint = "같은 원본을 선택한 모델로 읽습니다. 원문 문자·이체자·부호를 보존하며 완료한 판독은 이어 읽기에 재사용합니다."
        if len(selected) == 1:
            hint += " 단독 판독도 낮은 신뢰도·누락 의심 구간을 AI로 검증할 수 있습니다."
        if selected == ("ppocrv6",) and self.profile.get() == "한글·한자 혼용":
            hint += " PP-OCRv6는 한글·한자 혼용을 지원하지 않습니다. 다른 모델을 선택하세요."
        self.hint.set(hint)
        if self.project is not None and hasattr(self, "page_tree"):
            for number in range(1, self.project.page_count + 1):
                self._update_page(number)
        if hasattr(self, "ai_start_button"):
            self._set_enabled()

    def _selected_engines(self):
        label = self.engine_mode.get()
        mode = ENGINE_MODES.get(label, "all" if label == "전체 6엔진" else "recommended")
        if mode == "ppocrv6":
            return ("ppocrv6",)
        return engine_set(mode, PROFILES[self.profile.get()],
                          DIRECTIONS[self.direction.get()])

    def _ai_ready_count(self):
        if self.project is None:
            return 0
        from .comparison import usable_runs
        return len(usable_runs(self.project.page(self.page_number)))

    def _set_enabled(self):
        active = self.project is not None and not self._busy
        for button in self._controls:
            enabled = not self._busy and (self.project is not None or button not in self._document_controls)
            button.configure(state="normal" if enabled else "disabled")
        for combo in self.setting_widgets:
            combo.configure(state="readonly" if not self._busy else "disabled")
        self.range_entry.configure(state="normal" if not self._busy else "disabled")
        self.force_check.configure(state="normal" if not self._busy else "disabled")
        self.stop_button.configure(state="normal" if self._busy else "disabled")
        self.text.configure(state="normal" if active else "disabled")
        self.baseline_box.configure(state="readonly" if active else "disabled")
        for button in (self.undo_button, self.compare_button, self.review_check):
            button.configure(state="normal" if active else "disabled")
        self.previous_button.configure(state="normal" if self.project and self.page_number > 1 else "disabled")
        self.next_button.configure(state="normal" if self.project and self.page_number < self.project.page_count else "disabled")
        selected = bool(self._selected_suggestion) and active
        eligible = all(entry.get("eligible", True) for entry in (self._selected_suggestion or {}).get("evidence", []) if entry.get("engine") == self.candidate.get())
        pending = (self._selected_suggestion or {}).get("decision", "pending") == "pending"
        self.accept_button.configure(state="normal" if selected and pending and eligible and self.candidate.get() else "disabled")
        self.reject_button.configure(state="normal" if selected else "disabled")
        self.candidate_box.configure(state="readonly" if selected else "disabled")
        self.ai_start_button.configure(state="normal" if active and self.on_ai_settings and self._ai_ready_count() >= 1 else "disabled")
        self.ai_history_button.configure(state="normal" if self.project else "disabled")
        ai_item = self._selected_ai_proposal or {}
        ai_pending = active and bool(ai_item) and ai_item.get("decision", "pending") in ("pending", "undone")
        self.ai_accept_button.configure(state="normal" if ai_pending and ai_item.get("applicable", True) and not ai_item.get("uncertain") and not ai_item.get("superseded") else "disabled")
        self.ai_reject_button.configure(state="normal" if ai_pending else "disabled")
        if hasattr(self, "refinement_panel"):
            self.refinement_panel.set_enabled()

    def _title(self):
        name = Path(self.project.source_path).name if self.project else ""
        marker = " *" if self.project and self.project.dirty else ""
        self.title(f"연어OCR · 원본 대조 편집 · {name}{marker}")

    def _start_job(self, label, worker):
        if self._busy or self._closed:
            return False
        self._commit_text()
        self._job_id += 1
        generation, job_id = self._generation, self._job_id
        self._busy = True
        self._stop = threading.Event()
        stop = self._stop
        self.status.set(label)
        self._set_enabled()
        # Tk variables in old widget cycles must be finalized on their owner
        # thread, before a Python allocation in the worker can trigger cyclic GC.
        gc.collect()
        events = self._events
        def run():
            try:
                value = worker(stop, generation, job_id)
                events.put(("done", generation, job_id, value))
            except Exception as exc:
                events.put(("error", generation, job_id, str(exc)))
        threading.Thread(target=run, name="YeonE-comparison-job", daemon=True).start()
        return True

    def _confirm_replace(self):
        self._commit_text()
        if self.project and self.project.dirty:
            return self._save_before_close()
        return True

    def open_source(self, path=None):
        if self._busy or not self._confirm_replace():
            return
        path = path or filedialog.askopenfilename(parent=self, title="원본 PDF 또는 이미지", filetypes=[("PDF·이미지", "*.pdf *.png *.jpg *.jpeg *.tif *.tiff *.bmp *.webp *.gif"), ("모든 파일", "*.*")])
        if not path:
            return
        work_dir = self.work_dir
        def worker(_stop, _generation, _job):
            from .comparison_bridge import open_project
            return "project", open_project(path, work_dir=work_dir)
        self._start_job("원본과 기존 판독 기록을 확인하는 중…", worker)

    def open_project(self, path=None):
        if self._busy or not self._confirm_replace():
            return
        path = path or filedialog.askopenfilename(parent=self, title="비교 프로젝트 열기", filetypes=[("비교 프로젝트", "*.json")])
        if not path:
            return
        def worker(_stop, _generation, _job):
            from .comparison import ComparisonProject
            return "project", ComparisonProject.load(path)
        self._start_job("프로젝트와 원본 변경 여부를 확인하는 중…", worker)

    def open_recovery(self):
        directory = self._recovery_path.parent if self._recovery_path else self.work_dir
        path = filedialog.askopenfilename(parent=self, title="원본을 보존한 자동 복구 사본", initialdir=str(directory) if directory else None, filetypes=[("복구 프로젝트", "*.json")])
        if path:
            self.open_project(path)

    def save_project(self, *, save_as=False):
        if not self.project or self._busy:
            return
        self._commit_text()
        path = self.project.project_path
        if save_as or not path:
            path = filedialog.asksaveasfilename(parent=self, title="비교 프로젝트 저장", initialfile=Path(self.project.source_path).stem + ".yeone-compare.json", defaultextension=".json", filetypes=[("비교 프로젝트", "*.json")])
        if not path:
            return
        project = self.project
        def worker(_stop, _generation, _job):
            project.save(path)
            return "saved", str(path)
        self._start_job("원시 결과와 교정 이력을 함께 저장하는 중…", worker)

    def _set_project(self, project):
        self._generation += 1
        self.project = project
        self.page_number = 1
        self._zoom = None
        self._selected_suggestion = None
        self._pending_edit = False
        base = self.work_dir or (Path(project.project_path).parent if project.project_path else Path(project.source_path).parent / (Path(project.source_path).stem + "_YeonE_OCR"))
        self._recovery_path = base / "comparison-recovery" / f"{project.source_sha256[:16]}-{uuid.uuid4().hex[:10]}.yeone-compare.json"
        self.document_label.set(f"{Path(project.source_path).name} · {project.page_count}쪽")
        self.page_tree.delete(*self.page_tree.get_children())
        for number in range(1, project.page_count + 1):
            self.page_tree.insert("", "end", iid=str(number), values=(number, ""))
            self._update_page(number)
        first = min(project.completed_pages) if project.completed_pages else 1
        self.show_page(first, force=True)
        self._title()
        self._set_enabled()

    def _latest_runs(self, page):
        from .comparison import latest_runs
        return latest_runs(page)

    def _update_page(self, number):
        if not self.project or not self.page_tree.exists(str(number)):
            return
        page = self.project.page(number)
        selected = self._selected_engines()
        count = sum(1 for engine, run in self._latest_runs(page).items() if engine in selected
                    and run.get("raw", {}).get("engine_metadata", {}).get("generation_complete") is not False)
        state = f"{count}/{len(selected)}"
        if page.get("reviewed"):
            state += " · 검수"
        elif page.get("corrected_text") is not None:
            state += " · 교정"
        if page.get("last_errors"):
            state += " !"
        self.page_tree.item(str(number), values=(number, state))

    def _page_selected(self, _event=None):
        selected = self.page_tree.selection()
        if selected:
            self.show_page(int(selected[0]))

    def show_page(self, number, *, force=False):
        if not self.project or not 1 <= number <= self.project.page_count or self._closing:
            return
        if not force and number == self.page_number:
            return
        self._commit_text()
        self.page_number = number
        self._populate_page()
        self.page_tree.selection_set(str(number))
        self.page_tree.see(str(number))
        self.page_label.set(f"{number} / {self.project.page_count}쪽")
        self._request_render()
        self._set_enabled()

    def _populate_page(self):
        page = self.project.page(self.page_number)
        latest = self._latest_runs(page)
        selected = next((run for run in page.get("runs", []) if run["id"] == page.get("baseline_run_id")), None)
        display_runs = dict(latest)
        if selected:
            display_runs[selected["engine"]] = selected
        if latest:
            self.profile.set(next((label for label, value in PROFILES.items() if value == page.get("profile")), self.profile.get()))
            self.direction.set(next((label for label, value in DIRECTIONS.items() if value == page.get("direction")), self.direction.get()))
            self._settings_changed()
        for engine in latest:
            if engine not in self.raw_widgets:
                self._add_raw_tab(engine)
        for engine, widget in self.raw_widgets.items():
            raw = display_runs.get(engine, {}).get("raw", {})
            widget.configure(state="normal")
            widget.delete("1.0", "end")
            widget.insert("1.0", raw.get("text", ""))
            widget.configure(state="disabled")
            label = RAW_TAB_LABELS.get(engine, engine) + ("" if raw else " ·")
            self.raw_notebook.tab(self.raw_tabs[engine], text=label)
        self.baseline_box.configure(values=tuple(latest) or ENGINES)
        self.baseline.set(page.get("baseline_engine") or primary_engine(PROFILES[self.profile.get()], DIRECTIONS[self.direction.get()]))
        errors = page.get("last_errors", {})
        self.raw_note.set(" · ".join(errors.values()) if errors else "신뢰도는 정확도 수치가 아닙니다. 각 엔진의 원문·좌표·모델 정보는 프로젝트에 별도로 보존됩니다.")
        self._loading = True
        try:
            value = self.project.text(self.page_number)
            self.text.configure(state="normal")
            self.text.delete("1.0", "end")
            self.text.insert("1.0", value)
            self.text.edit_modified(False)
            self.char_label.set(f"{len(value):,}자")
            self.reviewed.set(bool(page.get("reviewed")))
            self._pending_edit = False
        finally:
            self._loading = False
        self._populate_suggestions(page)
        self._populate_ai_proposals(page)
        self.refinement_panel.refresh()
        self._set_enabled()

    def _text_changed(self, _event=None):
        if not self.text.edit_modified():
            return
        self.text.edit_modified(False)
        if self._loading or self._busy or not self.project:
            return
        self._pending_edit = True
        self.char_label.set(f"{len(self.text.get('1.0', 'end-1c')):,}자")
        if self._edit_after:
            self.after_cancel(self._edit_after)
        self._edit_after = self.after(350, self._commit_text)

    def _commit_text(self):
        if self._edit_after:
            self.after_cancel(self._edit_after)
            self._edit_after = None
        if not self.project or self._loading or not (self._pending_edit or self.text.edit_modified()):
            return
        self.text.edit_modified(False)
        self.project.set_text(self.page_number, self.text.get("1.0", "end-1c"), reason="manual")
        self._pending_edit = False
        self.reviewed.set(bool(self.project.page(self.page_number).get("reviewed")))
        self._update_page(self.page_number)
        self._title()
        self._schedule_recovery()

    def _schedule_recovery(self):
        if self._recovery_after:
            self.after_cancel(self._recovery_after)
        self._recovery_after = self.after(500, self._write_recovery)

    def _write_recovery(self):
        self._recovery_after = None
        if not self.project or not self._recovery_path:
            return True
        if self._busy:
            return True  # Recognition writes a checkpoint after every engine.
        try:
            self.project.save_recovery(self._recovery_path)
            self.status.set("복구 사본 저장됨 · 원시 OCR과 교정 이력을 별도로 보존합니다.")
            return True
        except Exception as exc:
            self.status.set(f"복구 사본 저장 실패: {exc}")
            return False

    def undo_edit(self):
        if not self.project or self._busy:
            return
        self._commit_text()
        try:
            self.project.undo_edit(self.page_number)
            self._populate_page()
            self._update_page(self.page_number)
            self._schedule_recovery()
            self._title()
        except Exception as exc:
            self.status.set(str(exc))

    def mark_reviewed(self):
        if self.project and not self._busy:
            reviewed = self.reviewed.get()
            self._commit_text()
            self.project.set_reviewed(self.page_number, reviewed)
            self.reviewed.set(reviewed)
            self._update_page(self.page_number)
            self._schedule_recovery()
            self._title()

    def change_baseline(self, _event=None):
        if self.project and not self._busy:
            self._commit_text()
            try:
                self.project.choose_baseline(self.page_number, self.baseline.get())
                self.project.compare_page(self.page_number, baseline=self.baseline.get())
                self._populate_page()
                self._schedule_recovery()
                self._title()
            except Exception as exc:
                self.status.set(str(exc))

    def compare_current(self):
        if self.project and not self._busy:
            self._commit_text()
            try:
                self.project.compare_page(self.page_number, baseline=self.baseline.get())
                self._populate_suggestions(self.project.page(self.page_number))
                self._schedule_recovery()
                self._title()
            except Exception as exc:
                self.status.set(str(exc))

    def _populate_suggestions(self, page):
        self._suggestions = {item["id"]: item for item in page.get("suggestions", [])}
        self._selected_suggestion = None
        self.diff_tree.delete(*self.diff_tree.get_children())
        for identifier, item in self._suggestions.items():
            base = item.get("baseline_text", "")
            alternatives = " · ".join(f"{engine}: {_short(value, 20)}" for engine, value in item.get("candidates", {}).items() if value != base)
            decision = {"pending": "대기", "accepted": "채택", "rejected": "기준 유지"}.get(item.get("decision", "pending"), item.get("decision", ""))
            self.diff_tree.insert("", "end", iid=identifier, values=(_short(base, 18), alternatives, decision))
        self.diff_label.set(f"불일치 {len(self._suggestions):,}곳")
        self.candidate.set("")
        self.candidate_detail.set("후보를 선택하면 전체 판독을 확인할 수 있습니다.")
        self.text.tag_remove("selection-evidence", "1.0", "end")
        for widget in self.raw_widgets.values():
            widget.tag_remove("evidence", "1.0", "end")
        self.canvas.delete("evidence")
        self._set_enabled()

    def _difference_selected(self, _event=None):
        self._selected_refinement = None
        self._selected_ai_proposal = None
        selected = self.diff_tree.selection()
        self._selected_suggestion = self._suggestions.get(selected[0]) if selected else None
        item = self._selected_suggestion
        if not item:
            self._set_enabled()
            return
        options = [engine for engine, text in item.get("candidates", {}).items() if text != item.get("baseline_text", "")]
        self.candidate_box.configure(values=options)
        self.candidate.set(options[0] if options else "")
        self._highlight_text(item)
        self._candidate_changed()
        self._set_enabled()

    def _highlight_text(self, item):
        page = self.project.page(self.page_number)
        selected = next((run for run in page.get("runs", []) if run["id"] == page.get("baseline_run_id")), {})
        baseline = selected.get("raw", {}).get("text", "")
        start, end = item.get("start", 0), item.get("end", 0)
        for widget in self.raw_widgets.values():
            widget.tag_remove("evidence", "1.0", "end")
        raw_widget = self.raw_widgets.get(page.get("baseline_engine"))
        if raw_widget:
            raw_widget.tag_add("evidence", f"1.0+{start}c", f"1.0+{max(start + 1, end)}c")
            raw_widget.see(f"1.0+{start}c")
            self.raw_notebook.select(self.raw_tabs[page["baseline_engine"]])
        self.text.tag_remove("selection-evidence", "1.0", "end")
        span = mapped_span(baseline, self.text.get("1.0", "end-1c"), start, end)
        if span:
            self.text.tag_add("selection-evidence", f"1.0+{span[0]}c", f"1.0+{max(span[0] + 1, span[1])}c")
            self.text.see(f"1.0+{span[0]}c")

    def _candidate_changed(self, _event=None):
        item = self._selected_suggestion
        if not item:
            return
        engine = self.candidate.get()
        candidate = item.get("candidates", {}).get(engine, "")
        eligible = next((e.get("eligible", True) for e in item.get("evidence", []) if e.get("engine") == engine), True)
        suffix = ""
        if not eligible:
            suffix = " · 이 모델의 지원 범위 밖인 글자가 있어 판단에서 제외됩니다."
        self.candidate_detail.set(f"{engine}: {candidate if candidate else '∅ (글자 없음)'}{suffix}")
        self._highlight_image()
        if not eligible:
            self.accept_button.configure(state="disabled")

    def show_candidate_details(self, _event=None):
        item = self._selected_suggestion
        if not item:
            return
        window = tk.Toplevel(self)
        window.title("원시 판독 후보 전체 보기")
        window.geometry("780x520")
        text = tk.Text(window, wrap="word", font=("맑은 고딕", 11), padx=12, pady=10)
        text.pack(fill="both", expand=True)
        for engine, candidate in item.get("candidates", {}).items():
            evidence = next((entry for entry in item.get("evidence", []) if entry.get("engine") == engine), {})
            text.insert("end", f"{engine}\n{candidate or '∅ (글자 없음)'}\n")
            if not evidence.get("eligible", True):
                text.insert("end", evidence.get("eligibility_reason", "모델 지원 범위 밖") + "\n")
            text.insert("end", "\n")
        text.configure(state="disabled")

    def open_ai_verification(self):
        if not self.project or self._busy:
            return
        if self.on_ai_settings is None:
            self.status.set("기존 연어OCR 설정에서 AI 제공사·모델·API 키를 설정하십시오.")
            return
        try:
            credentials = dict(self.on_ai_settings())
            if any(not isinstance(credentials.get(name), str) for name in ("provider", "model", "api_key")):
                raise ValueError()
            provider = credentials["provider"].strip()
            model = credentials["model"].strip()
            key = credentials["api_key"].strip()
            if provider not in ("gemini", "openai", "anthropic") or not model or not key:
                raise ValueError()
            key_count = credentials.get("key_count", 1)
            if type(key_count) is not int or key_count < 1:
                raise ValueError()
        except Exception:
            self.status.set("선택한 AI 제공사·모델·API 키가 준비되지 않았습니다. 기존 설정 탭에서 확인하십시오.")
            return
        window = tk.Toplevel(self)
        window.title("AI 검증 · 전송 범위와 편집 정책")
        window.geometry("760x620")
        window.transient(self)
        frame = ttk.Frame(window, padding=16)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text=f"선택한 제공사: {provider}  /  모델: {model}", font=("맑은 고딕", 10, "bold"), wraplength=710).pack(anchor="w", pady=(0, 10))
        if key_count > 1:
            ttk.Label(frame, text=f"등록된 키 {key_count}개 중 첫 번째 키만 사용합니다. 다른 키로 자동 전환하지 않습니다.", wraplength=710).pack(anchor="w", pady=(0, 6))
        ttk.Label(frame, text="기본은 로컬 판독의 불일치·낮은 신뢰도·누락 의심 구간만 이미지와 문맥으로 검증합니다.\nPP-OCRv6 단독 판독도 사용할 수 있습니다. 먼저 대상·호출 수를 확인하며 결과는 제안으로 저장합니다.", wraplength=710).pack(anchor="w", pady=(0, 12))
        selected_id = (self._selected_suggestion or {}).get("id")
        scopes = ("선택한 불일치", "현재 쪽", "지정 범위")
        scope = tk.StringVar(window, scopes[0] if selected_id else scopes[1])
        row = ttk.Frame(frame)
        row.pack(fill="x", pady=4)
        ttk.Label(row, text="검증 범위").pack(side="left", padx=(0, 10))
        ttk.Combobox(row, textvariable=scope, values=scopes, state="readonly", width=22).pack(side="left")
        ttk.Label(row, text="범위는 위쪽 쪽 범위 입력을 사용합니다.").pack(side="left", padx=10)
        traditional = tk.BooleanVar(window, PROFILES[self.profile.get()] == "traditional")
        punctuation = tk.BooleanVar(window, True)
        visual_page = tk.BooleanVar(window, False)
        force = tk.BooleanVar(window, False)
        ttk.Checkbutton(frame, text="번체 변환 후보 포함 (교정문 자동 변경 없음)", variable=traditional).pack(anchor="w", pady=3)
        ttk.Checkbutton(frame, text="중문부호 수정 후보 포함", variable=punctuation).pack(anchor="w", pady=3)
        ttk.Checkbutton(frame, text="쪽 전체 이미지 검증 (선택한 불일치는 해당 영역만)", variable=visual_page).pack(anchor="w", pady=3)
        ttk.Label(frame, text="기본은 의심 구간과 앞뒤 문맥만 보냅니다. 번체·부호 검토도 같은 구간에 적용합니다.\n쪽 전체 검증은 위 옵션을 직접 켰을 때만 사용합니다.", wraplength=710).pack(anchor="w", pady=3)
        ttk.Label(frame, text="이체자·원문 부호를 보존합니다. 로컬 후보·영역 재판독도 미확정 근거로 함께 검증합니다.", wraplength=710).pack(anchor="w", pady=3)
        ttk.Checkbutton(frame, text="이미 검증한 같은 범위도 다시 전송하기", variable=force).pack(anchor="w", pady=3)
        cap_row = ttk.Frame(frame)
        cap_row.pack(fill="x", pady=5)
        max_calls = tk.StringVar(window, "20")
        ttk.Label(cap_row, text="이번 작업의 최대 호출 수").pack(side="left", padx=(0, 10))
        ttk.Entry(cap_row, textvariable=max_calls, width=7).pack(side="left")
        ttk.Label(cap_row, text="준비한 범위가 상한을 넘으면 전송하지 않습니다.").pack(side="left", padx=10)
        error = tk.StringVar(window)
        ttk.Label(frame, textvariable=error, foreground="#a04432", wraplength=710).pack(anchor="w", pady=4)
        buttons = ttk.Frame(frame)
        buttons.pack(side="bottom", fill="x", pady=(8, 0))
        def prepare():
            try:
                if scope.get() == scopes[0]:
                    if not selected_id:
                        raise ValueError("먼저 [엔진 불일치] 탭에서 한 곳을 선택하십시오.")
                    pages, identifiers = [self.page_number], [selected_id]
                else:
                    pages = [self.page_number] if scope.get() == scopes[1] else parse_page_range(self.range_var.get(), self.project.page_count)
                    identifiers = None
                limit = int(max_calls.get())
                if not 1 <= limit <= 100:
                    raise ValueError("호출 상한은 1~100으로 입력하십시오.")
                policy = {"traditional": traditional.get(), "punctuation": punctuation.get(),
                          "visual_scope": "page" if visual_page.get() and identifiers is None else "auto"}
                repeat = force.get()
            except (ValueError, TypeError) as exc:
                error.set(str(exc))
                return
            window.destroy()
            self._prepare_ai(pages, identifiers, policy, provider, model, key, limit, repeat, key_count=key_count)
        ttk.Button(buttons, text="범위 준비 (아직 전송하지 않음)", command=prepare).pack(side="right")
        ttk.Button(buttons, text="취소", command=window.destroy).pack(side="right", padx=8)
        self._apply_local_styles(window)
        window.grab_set()
        window.focus_set()

    def _prepare_ai(self, pages, identifiers, policy, provider, model, api_key, max_calls=20, force=False, *, key_count=1):
        if self._busy or not self.project:
            return
        self._commit_text()
        project = self.project
        def worker(stop, _generation, _job):
            from .comparison_ai import AISettings, ReviewPolicy, prepare_plan
            settings = AISettings(provider=provider, model=model)
            settings = replace(settings, max_calls=max_calls,
                               max_total_output_tokens=max_calls * settings.max_output_tokens)
            selected_policy = ReviewPolicy(traditional=policy["traditional"], punctuation=policy["punctuation"],
                                           preserve_variants=True, visual_scope=policy.get("visual_scope", "auto"),
                                           selective=policy.get("visual_scope", "auto") != "page")
            plan = prepare_plan(project, pages, selected_policy, settings=settings,
                                suggestion_ids=identifiers, force=force, should_stop=stop.is_set)
            return "ai-plan", (plan, api_key, project, bool(identifiers), key_count)
        self._start_job("AI에 보낼 이미지와 문장 범위를 준비합니다. 아직 외부로 전송하지 않습니다.", worker)

    def _confirm_ai_plan(self, plan, api_key, project, selected_only=False, key_count=1):
        if self._closing or self._closed or self.project is not project or self._stop.is_set():
            self.status.set("AI 검증 준비를 취소했습니다. 전송하지 않았습니다.")
            return
        summary = plan.summary
        calls = summary.get("calls", 0)
        if not calls:
            self.status.set("의심 구간의 위치를 특정하지 못해 전송하지 않았습니다. 원본과 로컬 판독을 직접 확인하십시오."
                            if summary.get("unlocalized_targets") else
                            "새로 AI에 검증할 의심 구간이 없습니다. 같은 구간 재전송은 별도로 선택할 수 있습니다.")
            return
        policy = plan.policy
        traditional = getattr(policy, "traditional", False)
        punctuation = getattr(policy, "punctuation", False)
        scope = _page_labels(summary.get("pages", []))
        full_pages = summary.get("full_page_image_pages", [])
        image_disclosure = ""
        if "full_page_images" in summary:
            image_disclosure = f"쪽 전체 이미지: {summary['full_page_images']}개"
            if full_pages:
                image_disclosure += f" ({_page_labels(full_pages)}쪽)"
            image_disclosure += f" · 잘라낸 이미지: {summary.get('crop_images', 0)}개\n"
        else:
            descriptors = [(batch.get("page_number"), image) for batch in getattr(plan, "batches", ()) for image in batch.get("images", [])]
            full = [(number, image) for number, image in descriptors if image.get("source_bbox") == [0, 0, image.get("source_width"), image.get("source_height")]]
            if descriptors:
                image_disclosure = f"쪽 전체 이미지: {len(full)}개 ({_page_labels([number for number, _image in full])}쪽) · 잘라낸 이미지: {len(descriptors) - len(full)}개\n"
        dense_pages = summary.get("dense_pages", [])
        if dense_pages:
            image_disclosure += f"불일치가 많은 {_page_labels(dense_pages)}쪽은 쪽 본문을 문장 묶음으로 나누어 검증합니다.\n"
        selective = bool(getattr(policy, "selective", False))
        if summary.get("unlocalized_targets"):
            image_disclosure += "위치를 특정하지 못한 의심 구간은 전송에서 제외했습니다. 원본 대조가 필요합니다.\n"
        output_per_call = summary.get("max_output_tokens", getattr(plan.settings, "max_output_tokens", 0))
        output_cap = summary.get("output_token_cap", calls * output_per_call)
        budget_disclosure = (f"이미지 판독 {summary.get('visual_calls', 0)}회 · 문자·부호 검토 {summary.get('editorial_calls', 0)}회\n"
                             f"호출당 출력 상한: {output_per_call:,}토큰 · 이번 범위 합계 상한: {output_cap:,}토큰\n")
        if "target_char_cap" in summary and "batch_target_cap" in summary:
            budget_disclosure += f"문장 묶음당 최대 {summary['target_char_cap']}자 · 호출당 최대 {summary['batch_target_cap']}묶음\n"
        if summary.get("codepoint_ledger_entries"):
            budget_disclosure += f"문자 위치 대조표: {summary['codepoint_ledger_entries']:,}항목을 전송 문맥에 포함\n"
        thinking = summary.get("gemini_thinking_level")
        if thinking:
            budget_disclosure += f"Gemini 생각 수준: {thinking}\n"
        if key_count > 1:
            budget_disclosure += f"등록된 키 {key_count}개 중 첫 번째 키만 사용 · 자동 전환 없음\n"
        if summary.get("anchor_policy") == "unique-literal-v1":
            budget_disclosure += "위치 복원: 같은 검증 범위의 원문이 한 곳에만 일치할 때만 수행\n"
        if summary.get("refinement_proposals") or summary.get("region_readings"):
            budget_disclosure += (f"미확정 로컬 후보 {summary.get('refinement_proposals', 0)}곳 · "
                                  f"영역 재판독 {summary.get('region_readings', 0)}건을 근거로 포함\n")
        details = (f"제공사: {plan.settings.provider} / 모델: {plan.settings.model}\n"
                   f"원본: {project.source_path}\n쪽: {scope}\n"
                   f"검증 단위 {summary.get('targets', 0)}개 · 이미지 {summary.get('images', 0)}개 · 최대 호출 {calls}회\n"
                   f"{image_disclosure}"
                   f"{budget_disclosure}"
                   f"번체 후보: {'포함' if traditional else '제외'} · 중문부호: {'포함' if punctuation else '제외'} · 이체자 보존\n"
                   + (("번체·부호 검토: 의심 구간만 수정 대상으로, 앞뒤 문맥도 전송\n" if selective else
                       "번체·부호 검토: 선택한 불일치만 수정 대상으로, 앞뒤 문맥도 전송\n" if selected_only else "번체·부호 검토: 위 쪽의 전체 본문 전송\n") if traditional or punctuation else "") +
                   "\n원본 이미지와 OCR 문장을 이 제공사에 보내며 API 요금이 발생할 수 있습니다.\n제안은 별도 저장하며, 사람이 채택할 때만 교정문을 바꿉니다.\n\n이 범위로 검증을 시작하시겠습니까?")
        if not self._ask_ai_plan_confirmation(details):
            self.status.set("AI 검증을 취소했습니다. 문서와 이미지를 전송하지 않았습니다.")
            return
        self._run_ai_plan(project, plan, api_key)

    def _ask_ai_plan_confirmation(self, details):
        """Keep the concrete scope readable and both actions visible on small screens."""
        window = tk.Toplevel(self)
        window.title("AI 검증 · 전송 범위 확인")
        width = min(840, max(360, self.winfo_screenwidth() - 80), max(360, self.winfo_width() - 40))
        height = min(680, max(320, self.winfo_screenheight() - 100), max(320, self.winfo_height() - 60))
        window.geometry(f"{width}x{height}")
        window.transient(self)
        frame = ttk.Frame(window, padding=12)
        frame.pack(fill="both", expand=True)
        buttons = ttk.Frame(frame)
        buttons.pack(side="bottom", fill="x", pady=(10, 0))
        body = ttk.Frame(frame)
        body.pack(fill="both", expand=True)
        text = tk.Text(body, wrap="word", font=("맑은 고딕", 10), padx=10, pady=8)
        scroll = ttk.Scrollbar(body, command=text.yview)
        scroll.pack(side="right", fill="y")
        text.configure(yscrollcommand=scroll.set)
        text.pack(fill="both", expand=True)
        text.insert("1.0", details)
        text.configure(state="disabled")
        approved = False
        def finish(value=False):
            nonlocal approved
            approved = value
            window.destroy()
        ttk.Button(buttons, text="검증 시작", command=lambda: finish(True)).pack(side="right")
        cancel = ttk.Button(buttons, text="취소", command=finish)
        cancel.pack(side="right", padx=8)
        window.protocol("WM_DELETE_WINDOW", finish)
        window.bind("<Escape>", lambda _event: finish())
        self._apply_local_styles(window)
        window.grab_set()
        cancel.focus_set()
        window.wait_window()
        return approved

    def _run_ai_plan(self, project, plan, api_key):
        events, recovery = self._events, self._recovery_path
        self.progress.configure(maximum=max(1, plan.summary.get("calls", 1)), value=0)
        self.progress_label.set("AI 검증")
        self.proposal_notebook.select(1)
        def worker(stop, generation, job):
            from .comparison_ai import run_plan
            completed = 0
            def checkpoint(record):
                nonlocal completed
                if recovery:
                    project.save_recovery(recovery)
                if project.project_path:
                    project.save()
                completed += 1
                number = record.get("page_number", record.get("request", {}).get("page_number"))
                events.put(("ai-result", generation, job, (number, completed, record.get("status", "completed"))))
            result = run_plan(project, plan, api_key=api_key, should_stop=stop.is_set, on_result=checkpoint)
            if recovery:
                project.save_recovery(recovery)
            if project.project_path:
                project.save()
            return "ai-reviewed", result
        self._start_job("선택한 AI가 이미지·문장을 검증합니다. 완료한 응답과 제안마다 복구 기록을 저장합니다.", worker)

    def _populate_ai_proposals(self, page):
        items = self.project.ai_proposals(self.page_number, include_decided=True) if hasattr(self.project, "ai_proposals") else []
        self._ai_proposals = {item["review_id"] + ":" + item["id"]: item for item in items}
        self._selected_ai_proposal = None
        self.ai_tree.delete(*self.ai_tree.get_children())
        for identifier, item in self._ai_proposals.items():
            kind = {"recognition": "판독", "traditional": "번체", "punctuation": "부호"}.get(item.get("kind"), "판독")
            decision = {"pending": "대기", "accepted": "채택", "rejected": "거절", "undone": "되돌림"}.get(item.get("decision", "pending"), "대기")
            if item.get("uncertain"):
                decision = "판독 보류"
            elif item.get("superseded"):
                decision = "대체됨"
            elif item.get("stale"):
                decision = "재검토"
            elif item.get("source_fidelity_blocked") and item.get("decision", "pending") == "pending":
                decision = "원본보존 보류"
            self.ai_tree.insert("", "end", iid=identifier, values=(_short(item.get("before", ""), 18), _short(item.get("after", ""), 24), kind, decision))
        ready = self._ai_ready_count()
        self.ai_count.set(f"제안 {len(items):,}곳" + (" · 정상 로컬 판독 필요" if ready < 1 else f" · 정상 판독 {ready}개"))
        self._set_enabled()

    def _ai_selected(self, _event=None):
        self._selected_refinement = None
        selected = self.ai_tree.selection()
        self._selected_ai_proposal = self._ai_proposals.get(selected[0]) if selected else None
        self._selected_suggestion = None
        item = self._selected_ai_proposal
        if item:
            record = next((record for record in self.project.page(self.page_number).get("ai_reviews", []) if record["id"] == item["review_id"]), {})
            recovery_notice = _anchor_recovery_notice(record, item)
            self.ai_detail.set(f"{item.get('provider')} / {item.get('model')} · {item.get('reason', '')}" + (" · " + item.get("blocked_reason", "") if item.get("blocked_reason") else "") + (" · " + recovery_notice if recovery_notice else ""))
            if item.get("source_fidelity_contract"):
                self.ai_detail.set(self.ai_detail.get() + " · 현재 보호 검사: " + item["source_fidelity_contract"])
            snapshot = record.get("request", {}).get("snapshot_text", "")
            span = mapped_span(snapshot, self.text.get("1.0", "end-1c"), item.get("start", 0), item.get("end", 0))
            self.text.tag_remove("selection-evidence", "1.0", "end")
            if span:
                self.text.tag_add("selection-evidence", f"1.0+{span[0]}c", f"1.0+{max(span[0] + 1, span[1])}c")
                self.text.see(f"1.0+{span[0]}c")
            self._highlight_image()
        self._set_enabled()

    def accept_ai_proposal(self):
        item = self._selected_ai_proposal
        if not self.project or self._busy or not item:
            return
        self._commit_text()
        try:
            self.project.accept_ai_proposal(self.page_number, item["review_id"], item["id"])
            self._populate_page()
            self._update_page(self.page_number)
            self._schedule_recovery()
            self._title()
            self.status.set("AI 제안을 사람이 채택했습니다. 원시 OCR·AI 응답·변경 전 문장은 모두 보존됩니다.")
        except Exception as exc:
            self.status.set(f"AI 제안을 적용하지 않았습니다: {exc}")

    def reject_ai_proposal(self):
        item = self._selected_ai_proposal
        if self.project and not self._busy and item:
            try:
                self.project.reject_ai_proposal(self.page_number, item["review_id"], item["id"])
                self._populate_ai_proposals(self.project.page(self.page_number))
                self._schedule_recovery()
                self._title()
            except Exception as exc:
                self.status.set(str(exc))

    def show_ai_details(self, _event=None):
        item = self._selected_ai_proposal
        if not item:
            return
        window = tk.Toplevel(self)
        window.title("AI 교정 제안 · 원본과 직접 대조")
        window.geometry("780x550")
        text = tk.Text(window, wrap="word", font=("맑은 고딕", 11), padx=12, pady=10)
        scroll = ttk.Scrollbar(window, command=text.yview)
        scroll.pack(side="right", fill="y")
        text.configure(yscrollcommand=scroll.set)
        text.pack(fill="both", expand=True)
        text.insert("end", f"{item.get('provider')} / {item.get('model')}\n종류: {item.get('kind')}\n\n변경 전:\n{item.get('before', '')}\n\nAI 제안:\n{item.get('after', '')}\n\n근거:\n{item.get('reason', '')}\n\n")
        record = next((record for record in self.project.page(self.page_number).get("ai_reviews", []) if record["id"] == item["review_id"]), {})
        recovery_notice = _anchor_recovery_notice(record, item)
        if recovery_notice:
            text.insert("end", recovery_notice + "\n\n")
        current = item.get("source_fidelity_current")
        if current is not None:
            text.insert("end", "현재 프로그램 검증 근거 (AI 원시 응답·당시 저장 판정과 별도로 계산):\n"
                        + json.dumps(current, ensure_ascii=False, indent=2) + "\n\n")
        stored = record.get("metadata", {}).get("source_fidelity")
        if stored is not None:
            selected = {key: value for key, value in stored.items() if key != "proposals"}
            selected["proposals"] = [proposal for proposal in stored.get("proposals", [])
                                     if proposal.get("proposal_id") == item["id"]]
            text.insert("end", "당시 저장된 원본보존 검증 (원래 기록을 보존한 프로그램 판정):\n"
                        + json.dumps(selected, ensure_ascii=False, indent=2) + "\n\n")
        if item.get("uncertain") or not item.get("applicable", True):
            text.insert("end", "제안 채택이 보류되었습니다. " + item.get("blocked_reason", "판독이 불확실하여 원본을 직접 확인해야 합니다."))
        text.configure(state="disabled")

    def show_ai_history(self):
        if not self.project:
            return
        page = self.project.page(self.page_number)
        window = tk.Toplevel(self)
        window.title(f"{self.page_number}쪽 · AI 원시 응답과 교정 이력")
        window.geometry("850x650")
        text = tk.Text(window, wrap="word", font=("맑은 고딕", 10), padx=12, pady=10)
        scroll = ttk.Scrollbar(window, command=text.yview)
        scroll.pack(side="right", fill="y")
        text.configure(yscrollcommand=scroll.set)
        text.pack(fill="both", expand=True)
        for record in page.get("ai_reviews", []):
            request = record.get("request", {})
            text.insert("end", f"\n{record.get('created_at')} · {record.get('provider')} / {record.get('model')} · {record.get('status')}\n단계: {request.get('phase', '')}\n")
            metadata = record.get("metadata", {})
            if "response_complete" in metadata or metadata.get("finish_reason"):
                text.insert("end", f"응답 완료: {metadata.get('response_complete', '미기록')} · 종료 사유: {metadata.get('finish_reason', '미기록')}\n")
            if record.get("error"):
                text.insert("end", "오류: " + record["error"] + "\n")
            text.insert("end", f"\n원시 응답:\n{record.get('response_text', '')}\n")
            if "source_fidelity" in metadata:
                text.insert("end", "\n당시 저장된 원본보존 검증 (프로그램의 파생 판정, AI 원시 응답과 구분):\n"
                            + json.dumps(metadata["source_fidelity"], ensure_ascii=False, indent=2) + "\n")
            recovery = metadata.get("anchor_recovery", {})
            recovery_notice = _anchor_recovery_notice(record)
            if recovery_notice:
                text.insert("end", f"\n{recovery_notice}\n위치를 복원한 검증용 응답 (AI 원시 응답과 구분):\n{recovery.get('derived_response_text', '')}\n")
            text.insert("end", f"\n정책: {json.dumps(record.get('policy', {}), ensure_ascii=False)}\n전송 대상과 문맥:\n{json.dumps(request.get('targets', []), ensure_ascii=False, indent=2)}\n")
            if recovery_notice:
                text.insert("end", f"복원 내역:\n{json.dumps(recovery.get('relocations', []), ensure_ascii=False, indent=2)}\n")
        text.insert("end", "\n사람의 채택·거절·되돌리기 이력:\n")
        for decision in page.get("ai_decisions", []):
            text.insert("end", json.dumps(decision, ensure_ascii=False) + "\n")
        text.configure(state="disabled")

    def accept_candidate(self):
        if not self.project or self._busy or not self._selected_suggestion:
            return
        self._commit_text()
        try:
            self.project.accept_candidate(self.page_number, self._selected_suggestion["id"], self.candidate.get())
            self._populate_page()
            self._update_page(self.page_number)
            self._schedule_recovery()
            self._title()
            self.status.set("선택 판독을 교정문에 반영했습니다. 원시 결과와 선택 근거는 보존됩니다.")
        except Exception as exc:
            self.status.set(f"후보를 적용하지 않았습니다: {exc}")

    def reject_candidate(self):
        if not self.project or self._busy or not self._selected_suggestion:
            return
        try:
            self.project.reject_candidate(self.page_number, self._selected_suggestion["id"])
            self._populate_suggestions(self.project.page(self.page_number))
            self._schedule_recovery()
            self._title()
        except Exception as exc:
            self.status.set(str(exc))

    def recognize(self, use_range=False):
        if not self.project or self._busy:
            return
        try:
            numbers = parse_page_range(self.range_var.get(), self.project.page_count) if use_range else [self.page_number]
            dpi = int(self.dpi.get())
            profile, direction = PROFILES[self.profile.get()], DIRECTIONS[self.direction.get()]
            force = self.force.get()
            selected_engines = self._selected_engines()
            preferred = primary_engine(profile, direction)
        except (ValueError, KeyError) as exc:
            self.status.set(str(exc))
            return
        project, recovery = self.project, self._recovery_path
        events, engine_root = self._events, self.engine_root
        self._recognition_engine_count = len(selected_engines)
        self.progress.configure(maximum=max(1, len(numbers) * len(selected_engines)), value=0)
        self.progress_label.set(f"0 / {len(numbers)}쪽")
        def worker(stop, generation, job):
            from .comparison import run_pages
            completed = 0
            touched = set()
            def checkpoint(_result):
                if recovery:
                    project.save_recovery(recovery)
                if project.project_path:
                    project.save()
            def progress(number, engine, _outcome):
                nonlocal completed
                completed += 1
                touched.add(number)
                events.put(("batch-progress", generation, job,
                            (number, engine, completed, len(numbers) * len(selected_engines))))
            result = run_pages(project, numbers, profile=profile, direction=direction, dpi=dpi,
                should_stop=stop.is_set, on_result=checkpoint, engine_root=engine_root, force=force,
                engines=selected_engines, primary_engine=preferred if preferred in selected_engines else None,
                on_wait=lambda value: events.put(("gpu-wait", generation, job, value)), on_page=progress)
            checkpoint(None)
            return "recognized", {"pages": len(touched),
                "errors": sum(len(errors) for errors in result["errors"].values()),
                "cancelled": result["cancelled"]}
        self._start_job(f"선택한 {len(selected_engines)}개 모델을 인식합니다. 완료한 모델마다 복구 기록을 저장합니다.", worker)

    def cancel(self):
        if self._busy:
            self._stop.set()
            self.status.set("중지하는 중… 완료한 엔진의 원시 결과는 복구 사본에 보존됩니다.")
            self.stop_button.configure(state="disabled")

    def import_records(self):
        if not self.project or self._busy:
            return
        path = filedialog.askopenfilename(parent=self, title="기존 AB_OCR 프로젝트 가져오기", filetypes=[("AB 프로젝트", "*.abocr.json *.json")])
        if not path:
            return
        project = self.project
        recovery = self._recovery_path
        def worker(_stop, _generation, _job):
            from .comparison_bridge import import_ab_project
            count = import_ab_project(project, path)
            if recovery:
                project.save_recovery(recovery)
            return "imported", count
        self._start_job("원본 지문을 확인하고 기존 판독·교정 기록을 가져오는 중…", worker)

    def choose_engine_root(self):
        if self._busy:
            return
        from ab_ocr.runtime import default_engine_root
        current = default_engine_root(self.engine_root)
        selected = filedialog.askdirectory(parent=self, title="tools 폴더를 포함한 엔진 설치 폴더 선택", initialdir=str(current))
        if not selected:
            return
        directory = Path(selected).expanduser().resolve()
        if directory.name.lower() == "tools" and any((directory / engine).is_dir() for engine in ENGINES):
            directory = directory.parent
        self.engine_root = directory
        if self.on_engine_root is not None:
            self.on_engine_root(directory)
        self.status.set(f"엔진 폴더: {directory} · [설치 확인]으로 사용 가능한 모델을 확인하십시오.")

    def inspect_engines(self):
        if self._busy:
            return
        selected = self.engine_root
        def worker(stop, _generation, _job):
            from ab_ocr.ocr import doctor
            from ab_ocr.runtime import default_engine_root
            root = default_engine_root(selected)
            results = []
            for engine in ENGINES:
                if stop.is_set():
                    break
                results.append(doctor(engine=engine, engine_root=root, should_stop=stop.is_set))
            return "inspected", (str(root), results)
        self._start_job("로컬 엔진과 모델 파일을 확인하는 중… 자동 다운로드나 문서 전송은 없습니다.", worker)

    def _show_engine_info(self, value):
        root, records = value
        window = tk.Toplevel(self)
        window.title("로컬 OCR 모델 · 설치 확인")
        window.geometry("760x520")
        frame = ttk.Frame(window, padding=12)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="엔진 설치 폴더", font=("맑은 고딕", 11, "bold")).pack(anchor="w")
        ttk.Label(frame, text=root, wraplength=700).pack(anchor="w", pady=(3, 10))
        text = tk.Text(frame, wrap="word", font=("맑은 고딕", 10), padx=8, pady=8)
        text.pack(fill="both", expand=True)
        for record in records:
            engine = record.get("engine", "")
            ready = "사용 가능" if record.get("available") else "준비 필요"
            text.insert("end", f"{engine} · {ready}\n")
            if record.get("languages"):
                text.insert("end", "언어: " + ", ".join(record["languages"]) + "\n")
            text.insert("end", record.get("message", "") + "\n\n")
        text.insert("end", "설치는 명시적으로 실행해야 합니다. 모델별 언어 지원 범위와 세로 비교의 글줄 순서는 원본과 확인해야 합니다.\n설치 절차: docs/LOCAL_OCR.md")
        text.configure(state="disabled")
        ttk.Button(frame, text="닫기", command=window.destroy).pack(anchor="e", pady=(8, 0))
        available = sum(bool(record.get("available")) for record in records)
        self.status.set(f"설치 확인 완료 · {available}/{len(records)} 엔진 사용 가능 · {root}")

    def export_document(self):
        if not self.project or self._busy:
            return
        self._commit_text()
        path = filedialog.asksaveasfilename(parent=self, title="현재 교정문 내보내기", initialfile=Path(self.project.source_path).stem + "_교정.hwpx", defaultextension=".hwpx", filetypes=[("한글 HWPX", "*.hwpx"), ("Word DOCX", "*.docx"), ("텍스트", "*.txt")])
        if not path:
            return
        format = Path(path).suffix.lower().lstrip(".")
        if format not in ("txt", "hwpx", "docx"):
            self.status.set("내보내기 형식은 HWPX, DOCX, TXT를 선택하십시오.")
            return
        try:
            numbers = parse_page_range(self.range_var.get(), self.project.page_count)
        except ValueError as exc:
            self.status.set(str(exc))
            return
        project = self.project
        callback = self.on_export
        def worker(_stop, _generation, _job):
            if callback:
                result = callback(project, path, format, numbers)
            else:
                from .comparison_bridge import export_project
                result = export_project(project, path, format, numbers)
            return "exported", str(result or path)
        self._start_job("교정문을 내보내는 중… 원시 판독과 프로젝트는 별도로 보존합니다.", worker)

    def show_history(self):
        if not self.project:
            return
        self._commit_text()
        page = self.project.page(self.page_number)
        window = tk.Toplevel(self)
        window.title(f"{self.page_number}쪽 · 원시 판독과 교정 이력")
        window.geometry("800x580")
        view = tk.Text(window, wrap="word", font=("맑은 고딕", 11), padx=12, pady=10)
        view.pack(fill="both", expand=True)
        for run in page.get("runs", []):
            view.insert("end", f"판독 · {run.get('engine')} · {run.get('created_at', '')} · {run.get('id')}\n")
        view.insert("end", "\n교정 이력 (원시 판독 보존)\n")
        for edit in page.get("edits", []):
            view.insert("end", f"\n{edit.get('at', '')} · {edit.get('kind', '')} · {edit.get('reason', '')}\n변경 전:\n{edit.get('before', '')}\n변경 후:\n{edit.get('after', '')}\n")
        view.configure(state="disabled")

    def _request_render(self):
        self._render_id += 1
        generation, render_id = self._generation, self._render_id
        project, number = self.project, self.page_number
        events, closed = self._events, self._closed_event
        if self._preview is not None:
            self._preview.close()
            self._preview = None
        self.canvas.delete("all")
        self.canvas.create_text(170, 100, text="원본을 불러오는 중…", fill="#5d6670")
        def worker():
            try:
                from ab_ocr.ocr import render_page, file_sha256
                if file_sha256(project.source_path) != project.source_sha256:
                    raise ValueError("원본 파일이 변경되었습니다. 원본을 다시 선택하십시오.")
                picture = render_page(project.source_path, number, dpi=150)
                if file_sha256(project.source_path) != project.source_sha256:
                    picture.close()
                    raise ValueError("원본을 읽는 동안 파일이 변경되었습니다.")
                if closed.is_set():
                    picture.close()
                else:
                    events.put(("render", generation, render_id, picture))
            except Exception as exc:
                events.put(("render-error", generation, render_id, str(exc)))
        gc.collect()
        threading.Thread(target=worker, name="YeonE-comparison-preview", daemon=True).start()

    def _schedule_draw(self, _event=None):
        if self._draw_after:
            self.after_cancel(self._draw_after)
        self._draw_after = self.after(80, self._draw)

    def _draw(self):
        self._draw_after = None
        if self._closed or self._preview is None:
            return
        from PIL import Image, ImageTk
        width, height = self._preview.size
        fit = min(max(1, self.canvas.winfo_width() - 16) / width, max(1, self.canvas.winfo_height() - 16) / height)
        scale = self._zoom if self._zoom is not None else fit
        scale = max(.03, min(scale, 8.0, 12000 / max(width, height)))
        target = (max(1, round(width * scale)), max(1, round(height * scale)))
        resized = self._preview.resize(target, Image.Resampling.LANCZOS)
        try:
            self._photo = ImageTk.PhotoImage(resized, master=self)
        finally:
            resized.close()
        x, y = max(8, (self.canvas.winfo_width() - target[0]) // 2), max(8, (self.canvas.winfo_height() - target[1]) // 2)
        self._origin, self._image_scale = (x, y), scale
        self.canvas.delete("all")
        self.canvas.create_image(x, y, image=self._photo, anchor="nw", tags="source")
        self.canvas.configure(scrollregion=(0, 0, max(self.canvas.winfo_width(), target[0] + 16), max(self.canvas.winfo_height(), target[1] + 16)))
        self.zoom_label.set(f"{round(scale * 100)}%" if self._zoom is not None else "맞춤")
        self._highlight_image()

    def fit_page(self):
        self._zoom = None
        self._draw()

    def change_zoom(self, multiplier):
        if self._preview is not None:
            self._zoom = max(.03, min(8.0, self._image_scale * multiplier))
            self._draw()

    def _wheel_zoom(self, event):
        self.change_zoom(1.2 if event.delta > 0 else 1 / 1.2)
        return "break"

    def _highlight_image(self):
        self.canvas.delete("evidence")
        if self._selected_refinement and self.refinement_panel.highlight_image():
            self.evidence_label.set("로컬 교정 후보 · 실제 OCR 영역")
            return
        if self._selected_ai_proposal:
            self._highlight_ai_image()
            return
        item = self._selected_suggestion
        if not item or not self.project or self._preview is None:
            return
        evidence = item.get("evidence", [])
        engine = self.candidate.get()
        choices = sorted(evidence, key=lambda entry: entry.get("engine") != engine)
        page = self.project.page(self.page_number)
        latest = self._latest_runs(page)
        for entry in choices:
            bbox = entry.get("bbox")
            run = next((run for run in page.get("runs", []) if run["id"] == entry.get("run_id")), None) or latest.get(entry.get("engine"), {})
            raw = run.get("raw", {})
            if entry.get("engine") == "glmocr" and not bbox:
                ox, oy = self._origin
                self.canvas.create_rectangle(ox, oy, ox + self._preview.width * self._image_scale,
                    oy + self._preview.height * self._image_scale, outline="#de652b", width=3, tags="evidence")
                self.evidence_label.set("GLM-OCR: 좌표 없음 · 원본 전체 표시")
                return
            if not bbox or len(bbox) != 4 or not raw.get("image_width") or not raw.get("image_height"):
                continue
            if entry.get("run_id") and entry["run_id"] != run.get("id"):
                continue
            x0, y0, x1, y1 = bbox
            if not 0 <= x0 < x1 <= raw["image_width"] or not 0 <= y0 < y1 <= raw["image_height"]:
                continue
            sx = self._preview.width / raw["image_width"] * self._image_scale
            sy = self._preview.height / raw["image_height"] * self._image_scale
            ox, oy = self._origin
            self.canvas.create_rectangle(ox + x0 * sx, oy + y0 * sy, ox + x1 * sx, oy + y1 * sy, outline="#de652b", width=3, tags="evidence")
            granularity = raw.get("engine_metadata", {}).get("bbox_granularity")
            region = "블록 범위" if granularity == "block" else "글줄/단어 범위"
            self.evidence_label.set(f"{ENGINE_LABELS.get(entry.get('engine'), entry.get('engine'))}의 판독 영역 · {region}이며 글자 위치와 다를 수 있습니다.")
            return
        self.evidence_label.set("이 차이에 신뢰할 수 있는 좌표가 없습니다. 원본과 판독문을 직접 확인하십시오.")

    def _highlight_ai_image(self):
        if not self.project or self._preview is None or not self._selected_ai_proposal:
            return
        item = self._selected_ai_proposal
        bbox = item.get("bbox")
        record = next((record for record in self.project.page(self.page_number).get("ai_reviews", []) if record["id"] == item["review_id"]), {})
        request = record.get("request", {})
        images = request.get("images", [])
        target = next((target for target in request.get("targets", []) if target.get("start", 0) <= item.get("start", 0) <= item.get("end", 0) <= target.get("end", 0)), {})
        image = next((image for image in images if image.get("id") == target.get("image_id")), images[0] if images else {})
        width, height = image.get("source_width"), image.get("source_height")
        if not bbox or len(bbox) != 4 or not width or not height or not (0 <= bbox[0] < bbox[2] <= width and 0 <= bbox[1] < bbox[3] <= height):
            self.evidence_label.set("AI 제안에 신뢰할 수 있는 이미지 좌표가 없습니다. 원본과 문장을 직접 대조하십시오.")
            return
        sx, sy = self._preview.width / width * self._image_scale, self._preview.height / height * self._image_scale
        ox, oy = self._origin
        self.canvas.create_rectangle(ox + bbox[0] * sx, oy + bbox[1] * sy,
            ox + bbox[2] * sx, oy + bbox[3] * sy, outline="#2774ad", width=3, tags="evidence")
        self.evidence_label.set("AI가 검증한 이미지 범위 · 제안 근거와 원본 글자를 직접 확인하십시오.")

    def _poll(self):
        if self._closed:
            return
        try:
            while True:
                self._dispatch(self._events.get_nowait())
        except queue.Empty:
            pass
        if not self._closed:
            self._poll_id = self.after(60, self._poll)

    def _dispatch(self, event):
        kind, generation, identifier, value = event
        if kind in ("render", "render-error"):
            if self._closed or generation != self._generation or identifier != self._render_id:
                if kind == "render":
                    value.close()
                return
            if kind == "render":
                if self._preview is not None:
                    self._preview.close()
                self._preview = value
                self._draw()
            else:
                self.canvas.delete("all")
                self.canvas.create_text(170, 100, text=value, width=300, fill="#a04432")
            return
        if generation != self._generation or identifier != self._job_id or self._closed:
            return
        if kind in ("done", "error"):
            self._busy = False
            if kind == "error":
                self.status.set(f"작업 실패: {value} · 완료한 판독은 복구 사본에서 이어 읽을 수 있습니다.")
            else:
                action, payload = value
                if action == "ai-plan":
                    self._set_enabled()
                    if self._closing:
                        if self._save_before_close():
                            self._dispose()
                        else:
                            self._closing = False
                        return
                    self._confirm_ai_plan(*payload)
                    return
                if action == "project":
                    self._set_project(payload)
                    notices = getattr(payload, "import_notices", [])
                    self.status.set("기존 기록 가져오기 주의: " + " · ".join(notices) if notices else "프로젝트를 열었습니다. 모델별 원시 판독과 사람의 교정문을 구분해 보관합니다.")
                elif action == "recognized":
                    self.status.set(f"{'중지됨' if payload['cancelled'] else '인식 완료'} · {payload['pages']}쪽 처리 · 엔진 오류 {payload['errors']}건 · 복구 사본 저장됨")
                elif action == "refined":
                    self.status.set(f"조합 후보 {payload['local']}곳 · 재판독 후보 {payload['regional']}곳 저장됨 · 사람의 확정문은 보존됨")
                elif action == "source-checked":
                    self.status.set(f"PDF 부호 후보 {payload['checked']}곳 대조 · 보류 {payload['held']}곳 · 근거 부족 {payload['unavailable']}곳 · 교정문 변경 없음")
                elif action == "image-checked":
                    self.status.set(f"이미지 OCR 제안 {payload['checked']}곳 재검사 · 보류 {payload['held']}곳 · 원시·AI 제안·사람 확정 이력 보존")
                elif action == "region-read":
                    self.status.set(f"영역 재판독 {payload} · 원시 결과·후보를 별도 저장했습니다.")
                elif action == "saved":
                    self.status.set(f"저장됨 · {payload}")
                elif action == "imported":
                    self.status.set(f"기존 기록 {payload}쪽을 가져왔습니다. 원본 기록은 보존됩니다.")
                elif action == "exported":
                    self.status.set(f"내보내기 완료 · {payload}")
                elif action == "inspected":
                    self._show_engine_info(payload)
                elif action == "ai-reviewed":
                    errors = payload.get("errors", [])
                    self.status.set(f"AI 검증 {'중지됨' if payload.get('cancelled') else '완료'} · 제안은 별도 저장됨 · 오류 {len(errors)}건 · 사람이 채택할 때만 교정문을 변경합니다.")
            if self.project:
                for number in range(1, self.project.page_count + 1):
                    self._update_page(number)
                self._populate_page()
            self._title()
            self._set_enabled()
            if self.project and self.project.dirty and not self._closing:
                self._schedule_recovery()
            if self._closing:
                if self._save_before_close():
                    self._dispose()
                else:
                    self._closing = False
            return
        if kind == "gpu-wait":
            position = value.get("position") if isinstance(value, dict) else None
            elapsed = value.get("elapsed_seconds") if isinstance(value, dict) else None
            wait_time = f" · {int(elapsed)}초" if isinstance(elapsed, (int, float)) else ""
            self.status.set(f"GPU 사용 순서를 기다리는 중{' · 대기 ' + str(position) + '번째' if position else ''}{wait_time} · 중지할 수 있습니다.")
        elif kind == "batch-progress":
            number, engine, completed, total = value
            self.progress.configure(value=completed)
            self.progress_label.set(f"{completed} / {total} 모델·쪽")
            self.status.set(f"{number}쪽 · {ENGINE_LABELS.get(engine, engine)} 처리됨 · 모델별 연속 처리")
            self._update_page(number)
            if number == self.page_number:
                self._populate_page()
        elif kind == "page-start":
            number, index, total = value
            self.status.set(f"{number}쪽 · 선택한 {self._recognition_engine_count}개 모델 인식 중 ({index}/{total}쪽)")
        elif kind == "engine-done":
            number, engine = value
            self.status.set(f"{number}쪽 · {engine} 원시 결과 저장됨")
            self.progress.step(1)
            self._update_page(number)
            if self.page_number == number:
                self._populate_page()
        elif kind == "page-done":
            number, index, total, result = value
            self.progress.configure(value=index * self._recognition_engine_count)
            self.progress_label.set(f"{index} / {total}쪽")
            self._update_page(number)
            if self.page_number == number:
                self._populate_page()
            if result.get("errors"):
                self.status.set(f"{number}쪽 · " + " · ".join(f"{engine}: {error}" for engine, error in result["errors"].items()))
        elif kind == "ai-result":
            number, completed = value[:2]
            state = value[2] if len(value) > 2 else "completed"
            self.progress.configure(value=completed)
            self.progress_label.set(f"AI {completed}회 저장")
            description = {"completed": "응답", "failed": "실패 기록", "cancelled": "취소 기록"}.get(state, "기록")
            self.status.set(f"AI 검증 {completed}회 {description} 저장됨 · 원시 결과와 교정 제안 보존")
            if isinstance(number, int):
                self._update_page(number)
                if number == self.page_number:
                    self._populate_page()

    def _key_save(self, _event=None):
        self.save_project()
        return "break"

    def _key_undo(self, _event=None):
        self.undo_edit()
        return "break"

    def close(self):
        if self._closed:
            return
        if self._busy:
            self._closing = True
            self.cancel()
            return
        self._commit_text()
        if self._save_before_close():
            self._dispose()

    def _save_before_close(self):
        if not self._write_recovery():
            return False
        if self.project and self.project.dirty and self.project.project_path:
            try:
                self.project.save()
            except Exception as exc:
                self.status.set(f"프로젝트 저장 실패: {exc} · 복구 사본은 보존했습니다. 다른 이름으로 저장하십시오.")
                self.recovery_close_button.pack(side="right", padx=(8, 0))
                return False
        return True

    def _close_from_recovery(self):
        if self._busy:
            return
        self._commit_text()
        if self._write_recovery():
            self._dispose()

    @property
    def busy(self):
        return self._busy

    def request_close(self):
        """Host close hook: let an active engine checkpoint before destroying Tk."""
        self.close()
        return self._closed

    def _dispose(self):
        if self._closed:
            return
        self._closed = True
        self._closed_event.set()
        self._cancel_callbacks()
        if self._preview is not None:
            self._preview.close()
            self._preview = None
        for state in self._tooltips:
            if state["window"]:
                try:
                    state["window"].destroy()
                except tk.TclError:
                    pass
        while True:
            try:
                kind, _generation, _identifier, value = self._events.get_nowait()
                if kind == "render":
                    value.close()
            except queue.Empty:
                break
        try:
            self.destroy()
        except tk.TclError:
            pass

    def _cancel_callbacks(self):
        for callback in (self._poll_id, self._edit_after, self._recovery_after, self._draw_after, self._layout_after, self._startup_after):
            if callback:
                try:
                    self.after_cancel(callback)
                except tk.TclError:
                    pass
        for state in self._tooltips:
            if state["after"]:
                try:
                    self.after_cancel(state["after"])
                except tk.TclError:
                    pass

    def _destroyed(self, event):
        if event.widget is self and not self._closed:
            self._closed = True
            self._closed_event.set()
            self._stop.set()
            self._cancel_callbacks()
            if self._preview is not None:
                self._preview.close()
                self._preview = None


def open_comparison_editor(master, source_path=None, project_path=None, **kwargs):
    return ComparisonEditor(master, source_path, project_path, **kwargs)
