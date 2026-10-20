"""Bookmark-scoped PDF comparison and direct editing of a corrected HWPX."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import difflib
from pathlib import Path
import tkinter as tk
from tkinter import ttk, messagebox


def render_pdf_page(path: str, page_number: int, width: int, zoom: float | None):
    """Render one local PDF page off the Tk thread; return a Pillow image."""
    import pymupdf
    from PIL import Image
    with pymupdf.open(path) as document:
        if not 1 <= page_number <= len(document):
            raise ValueError("PDF 쪽 번호가 범위를 벗어났습니다.")
        page = document[page_number - 1]
        factor = min(4.0, max(.2, zoom if zoom is not None else max(180, width - 32) / page.rect.width))
        # Keep unusually large page dimensions within a predictable memory bound.
        factor = min(factor, (18_000_000 / max(1, page.rect.width * page.rect.height)) ** .5)
        pix = page.get_pixmap(matrix=pymupdf.Matrix(factor, factor), alpha=False)
        return Image.frombytes("RGB", (pix.width, pix.height), pix.samples)


class ChangeReviewDialog(tk.Toplevel):
    """A read-only before/after review. File writing stays in the editor."""
    def __init__(self, parent, changes: dict[int, str], baseline: dict[int, str], target: Path):
        super().__init__(parent)
        self.accepted = False
        self.title("변경 확인 · 교정본에 저장")
        self.transient(parent)
        self.geometry("760x620")
        self.minsize(560, 420)
        self.configure(bg=parent.colors["BG"])
        self.columnconfigure(0, weight=1); self.rowconfigure(2, weight=1)
        tk.Label(self, text="수정한 내용을 확인하세요", anchor="w", bg=parent.colors["BG"],
                 fg=parent.colors["INK"], font=("Malgun Gothic", 16, "bold")).grid(
                     row=0, column=0, sticky="ew", padx=22, pady=(20, 8))
        tk.Label(self, text=f"문단 {len(changes)}개 · {target.name}\n기존 교정본이 있으면 저장 전 사본을 보관합니다.",
                 anchor="w", justify="left", bg=parent.colors["BG"], fg=parent.colors["MUTED"]).grid(
                     row=1, column=0, sticky="ew", padx=22, pady=(0, 14))
        body = tk.Frame(self, bg=parent.colors["PANEL"])
        body.grid(row=2, column=0, sticky="nsew", padx=22)
        text = tk.Text(body, wrap="word", relief="flat", bg=parent.colors["PANEL"], fg=parent.colors["INK"],
                       font=("Malgun Gothic", 11), padx=14, pady=12, spacing3=8)
        scroll = ttk.Scrollbar(body, orient="vertical", command=text.yview)
        text.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y"); text.pack(side="left", fill="both", expand=True)
        text.tag_configure("head", font=("Malgun Gothic", 10, "bold"), foreground=parent.colors["ACCENT"])
        text.tag_configure("before", foreground="#867b70")
        for index, value in changes.items():
            text.insert("end", f"문단 {index + 1}\n", "head")
            text.insert("end", f"수정 전\n{baseline[index]}\n\n", "before")
            text.insert("end", f"수정 후\n{value}\n\n")
        text.configure(state="disabled")
        buttons = tk.Frame(self, bg=parent.colors["BG"])
        buttons.grid(row=3, column=0, sticky="ew", padx=22, pady=18)
        ttk.Button(buttons, text="확인 · 교정본에 저장", command=self._accept,
                   style="Ink.TButton").pack(side="right")
        ttk.Button(buttons, text="계속 편집", command=self.destroy).pack(side="right", padx=(0, 8))
        self.bind("<Escape>", lambda _event: self.destroy())
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self.grab_set()
        self.focus_set()

    def _accept(self):
        self.accepted = True
        self.destroy()


class BookmarkSectionEditor(tk.Toplevel):
    """Read-only PDF on the left, flowing editable HWPX paragraphs on the right.

    PDF pages and HWPX paragraphs are deliberately independent controls. The
    backend preserves document structure, verifies snapshots and writes only
    the cumulative corrected document, with a backup.
    """
    def __init__(self, parent, *, pdf_path, hwpx_path, section_name, section_start,
                 section_end, pdf_pages, can_write=None, on_saved=None, heading_styles=None,
                 anchor_source="auto"):
        from .section_edit import SectionEditSession
        # Validate/load before creating a window, so the caller can report failure.
        scope_options = {"heading_styles": list(heading_styles) if heading_styles else None, "anchor_source": anchor_source}
        session = SectionEditSession.open(str(pdf_path), str(hwpx_path), section_name, section_start, section_end, **scope_options)
        import pymupdf
        with pymupdf.open(pdf_path) as document:
            first, last = map(int, pdf_pages)
            if not 1 <= first <= last <= len(document):
                raise ValueError("선택 구획의 PDF 쪽 범위를 확인한 뒤 대조 편집을 여십시오.")
        super().__init__(parent)
        self.session = session
        self.pdf_path = str(pdf_path)
        self.hwpx_path = str(hwpx_path)
        self.section_name = section_name
        self.section_start, self.section_end = section_start, section_end
        self._scope_options = scope_options
        self.pdf_pages = first, last
        self.can_write = can_write or (lambda: True)
        self.on_saved = on_saved
        self.colors = {"BG": "#f5f2eb", "PANEL": "#fffefa", "INK": "#302b26", "MUTED": "#827970",
                       "LINE": "#e6dfd4", "ACCENT": "#a53c29", "BLUE": "#2c5e87"}
        inherited = getattr(parent, "colors", {})
        for key in ("BG", "PANEL", "INK", "MUTED", "LINE"):
            if key in inherited:
                self.colors[key] = inherited[key]
        self._paragraphs = {p.index: p for p in session.paragraphs}
        self._baseline = {p.index: p.corrected_text for p in session.paragraphs}
        self._editors = {}
        self._cards = {}
        self._markers = {}
        self._active = None
        self._show_marks = True
        self._show_original = True
        self._text_size = 12
        self._saving = False
        self._reviewing = False
        self._closed = False
        self._pdf_page = first
        self._zoom = None
        self._render_generation = 0
        self._render_future = None
        self._render_job = None
        self._poll_job = None
        self._layout_job = None
        self._change_jobs = {}
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="bookmark-pdf")
        self._photo = None
        self.title(f"대조 편집 — {section_name}")
        self.geometry("1280x900")
        self.minsize(900, 640)
        self.configure(bg=self.colors["BG"])
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.bind("<Destroy>", self._destroyed, add="+")
        self.bind("<Control-s>", self._save_shortcut)
        self.bind("<Control-S>", self._save_shortcut)
        self._build()
        self._build_paragraphs()
        self._initial_job = self.after_idle(self._initial_split)
        self._poll_job = self.after(60, self._poll_render)

    @property
    def dirty(self) -> bool:
        return bool(self._changes()) if not self._closed else False

    @property
    def busy(self) -> bool:
        return self._saving or self._reviewing

    def _label(self, parent, text="", *, size=10, bold=False, muted=False, **kw):
        return tk.Label(parent, text=text, bg=kw.pop("bg", self.colors["PANEL"]),
                        fg=self.colors["MUTED"] if muted else self.colors["INK"],
                        font=("Malgun Gothic", size, "bold" if bold else "normal"),
                        anchor="w", **kw)

    def _tool_button(self, parent, text, command, *, width=None):
        options = {"text": text, "command": command, "font": ("Malgun Gothic", 9),
                   "bg": self.colors["PANEL"], "fg": self.colors["INK"], "activebackground": "#eee9df",
                   "relief": "solid", "borderwidth": 1, "padx": 6, "pady": 5, "cursor": "hand2",
                   "highlightthickness": 0}
        if width is not None:
            options["width"] = width
        return tk.Button(parent, **options)

    def _build(self):
        C = self.colors
        self.columnconfigure(0, weight=1); self.rowconfigure(1, weight=1)
        head = tk.Frame(self, bg=C["BG"])
        head.grid(row=0, column=0, sticky="ew", padx=20, pady=(15, 12))
        self._label(head, f"책갈피 OCR  /  대조 편집", muted=True, bg=C["BG"]).pack(anchor="w")
        self._label(head, self.section_name, size=21, bold=True, bg=C["BG"]).pack(anchor="w", pady=(5, 2))
        self._label(head, f"PDF {self.pdf_pages[0]}–{self.pdf_pages[1]}쪽  ·  HWPX {len(self._paragraphs)}문단  ·  선택한 구획만 편집",
                    muted=True, bg=C["BG"]).pack(anchor="w")
        self.panes = ttk.Panedwindow(self, orient="horizontal")
        self.panes.grid(row=1, column=0, sticky="nsew", padx=18)
        self.pdf_panel = tk.Frame(self.panes, bg=C["PANEL"], highlightthickness=1, highlightbackground=C["LINE"])
        self.doc_panel = tk.Frame(self.panes, bg=C["PANEL"], highlightthickness=1, highlightbackground=C["LINE"])
        self.panes.add(self.pdf_panel, weight=1); self.panes.add(self.doc_panel, weight=1)
        self._build_pdf()
        self._build_document()
        footer = tk.Frame(self, bg=C["BG"])
        footer.grid(row=2, column=0, sticky="ew", padx=20, pady=(12, 16))
        footer.columnconfigure(0, weight=1)
        self.v_status = tk.StringVar(value="저장할 변경 없음")
        self.status_label = self._label(footer, textvariable=self.v_status, bg=C["BG"], size=10)
        self.status_label.grid(row=0, column=0, sticky="ew")
        self.target_label = self._label(footer, f"저장 대상 · {Path(self.session.corrected_path).name}",
                                        muted=True, bg=C["BG"], size=9)
        self.target_label.grid(row=1, column=0, sticky="ew", pady=(4, 0))
        actions = tk.Frame(footer, bg=C["BG"])
        actions.grid(row=0, column=1, rowspan=2, sticky="e", padx=(12, 0))
        self.revert_button = ttk.Button(actions, text="이번 수정 취소", command=self.revert_changes, state="disabled")
        self.revert_button.pack(side="left", padx=(0, 7))
        self.save_button = ttk.Button(actions, text="변경 확인·저장  Ctrl S", command=self.save, style="Ink.TButton", state="disabled")
        self.save_button.pack(side="left")
        footer.bind("<Configure>", lambda e: (
            self.status_label.configure(wraplength=max(160, e.width - actions.winfo_reqwidth() - 20)),
            self.target_label.configure(wraplength=max(160, e.width - actions.winfo_reqwidth() - 20))))

    def _build_pdf(self):
        panel = self.pdf_panel
        panel.columnconfigure(0, weight=1); panel.rowconfigure(2, weight=1)
        heading = tk.Frame(panel, bg=self.colors["PANEL"])
        heading.grid(row=0, column=0, sticky="ew", padx=14, pady=(12, 9))
        self._label(heading, "원본 PDF", bold=True, size=11).pack(anchor="w")
        self._label(heading, Path(self.pdf_path).name, muted=True, size=9).pack(anchor="w", pady=(3, 0))
        tools = tk.Frame(panel, bg="#fcfaf5")
        tools.grid(row=1, column=0, sticky="ew", padx=7, pady=(0, 7))
        self.previous_button = self._tool_button(tools, "‹", lambda: self.change_page(-1), width=2)
        self.previous_button.pack(side="left")
        self.v_page = tk.StringVar(value=str(self._pdf_page))
        self.page_choice = ttk.Combobox(tools, textvariable=self.v_page, width=6, state="readonly",
                                       values=[str(n) for n in range(self.pdf_pages[0], self.pdf_pages[1] + 1)])
        self.page_choice.pack(side="left", padx=4)
        self.page_choice.bind("<<ComboboxSelected>>", lambda _event: self.set_page(int(self.v_page.get())))
        self.next_button = self._tool_button(tools, "›", lambda: self.change_page(1), width=2)
        self.next_button.pack(side="left")
        self._tool_button(tools, "맞춤", lambda: self.set_zoom(None)).pack(side="right")
        self._tool_button(tools, "+", lambda: self.change_zoom(1), width=2).pack(side="right", padx=2)
        self.v_zoom = tk.StringVar(value="너비 맞춤")
        self._label(tools, textvariable=self.v_zoom, size=9, muted=True, bg="#fcfaf5").pack(side="right", padx=3)
        self._tool_button(tools, "−", lambda: self.change_zoom(-1), width=2).pack(side="right", padx=2)
        area = tk.Frame(panel, bg="#e8e4dc")
        area.grid(row=2, column=0, sticky="nsew")
        area.columnconfigure(0, weight=1); area.rowconfigure(0, weight=1)
        self.pdf_canvas = tk.Canvas(area, bg="#e8e4dc", highlightthickness=0, width=360, height=300)
        y = ttk.Scrollbar(area, orient="vertical", command=self.pdf_canvas.yview)
        x = ttk.Scrollbar(area, orient="horizontal", command=self.pdf_canvas.xview)
        self.pdf_canvas.configure(yscrollcommand=y.set, xscrollcommand=x.set)
        self.pdf_canvas.grid(row=0, column=0, sticky="nsew"); y.grid(row=0, column=1, sticky="ns"); x.grid(row=1, column=0, sticky="ew")
        self.pdf_canvas.bind("<Configure>", lambda _event: self._request_render())
        self.pdf_canvas.bind("<MouseWheel>", lambda event: self._wheel(self.pdf_canvas, event))
        self.pdf_canvas.bind("<Control-MouseWheel>", lambda event: (self.change_zoom(1 if event.delta > 0 else -1), "break")[1])
        self.pdf_canvas.bind("<Button-4>", lambda _event: self.pdf_canvas.yview_scroll(-3, "units"))
        self.pdf_canvas.bind("<Button-5>", lambda _event: self.pdf_canvas.yview_scroll(3, "units"))
        self.pdf_note = self._label(panel, "PDF 쪽 이동과 오른쪽 문단 선택은 각각 조작합니다.", muted=True, size=9, justify="left")
        self.pdf_note.grid(row=3, column=0, sticky="ew", padx=12, pady=8)
        panel.bind("<Configure>", lambda event: self.pdf_note.configure(wraplength=max(180, event.width - 28)))
        self._sync_page_buttons()

    def _build_document(self):
        panel = self.doc_panel
        panel.columnconfigure(0, weight=1); panel.rowconfigure(2, weight=1)
        heading = tk.Frame(panel, bg=self.colors["PANEL"])
        heading.grid(row=0, column=0, sticky="ew", padx=14, pady=(12, 9))
        heading.columnconfigure(0, weight=1)
        self._label(heading, "교정본 HWPX", bold=True, size=11).grid(row=0, column=0, sticky="w")
        self.file_label = self._label(heading, Path(self.session.corrected_path).name, muted=True, size=9)
        self.file_label.grid(row=1, column=0, sticky="w", pady=(3, 0))
        self.reload_button = self._tool_button(heading, "다시 불러오기", self.reload)
        self.reload_button.grid(row=0, column=1, rowspan=2, sticky="e", padx=(8, 0))
        tools = tk.Frame(panel, bg="#fcfaf5")
        tools.grid(row=1, column=0, sticky="ew", padx=7, pady=(0, 7))
        self._label(tools, "파랑 · 원본 HWPX와 차이", size=9, muted=True, bg="#fcfaf5").pack(side="left", padx=6)
        self._tool_button(tools, "가+", self.toggle_text_size, width=2).pack(side="right")
        self.marks_button = self._tool_button(tools, "차이 켜짐", self.toggle_marks)
        self.marks_button.pack(side="right", padx=4)
        area = tk.Frame(panel, bg=self.colors["PANEL"])
        area.grid(row=2, column=0, sticky="nsew")
        area.columnconfigure(0, weight=1); area.rowconfigure(0, weight=1)
        self.doc_canvas = tk.Canvas(area, bg=self.colors["PANEL"], highlightthickness=0, width=360, height=300,
                                    yscrollincrement=24)
        scroll = ttk.Scrollbar(area, orient="vertical", command=self.doc_canvas.yview)
        self.doc_canvas.configure(yscrollcommand=scroll.set)
        self.doc_canvas.grid(row=0, column=0, sticky="nsew"); scroll.grid(row=0, column=1, sticky="ns")
        self.document = tk.Frame(self.doc_canvas, bg=self.colors["PANEL"])
        self._doc_window = self.doc_canvas.create_window(0, 0, anchor="nw", window=self.document)
        self.document.bind("<Configure>", lambda _event: self.doc_canvas.configure(scrollregion=self.doc_canvas.bbox("all")))
        self.doc_canvas.bind("<Configure>", self._document_resized)
        self.doc_canvas.bind("<MouseWheel>", lambda event: self._wheel(self.doc_canvas, event))
        self.compare = tk.Frame(panel, bg="#faf7f0", highlightthickness=1, highlightbackground=self.colors["LINE"])
        self.compare.grid(row=3, column=0, sticky="ew")
        self.compare.columnconfigure(0, weight=1)
        top = tk.Frame(self.compare, bg="#faf7f0")
        top.grid(row=0, column=0, sticky="ew", padx=12, pady=(7, 4))
        self.v_compare = tk.StringVar(value="선택 문단")
        self._label(top, textvariable=self.v_compare, size=9, muted=True, bg="#faf7f0").pack(side="left")
        self.original_button = self._tool_button(top, "원문 접기", self.toggle_original)
        self.original_button.pack(side="right")
        self.original_text = tk.Text(self.compare, height=2, wrap="word", bg="#faf7f0", fg="#7d6e5f", relief="flat",
                                     font=("Malgun Gothic", 10), padx=12, pady=6)
        self.original_text.grid(row=1, column=0, sticky="ew")
        self.original_text.tag_configure("changed", background="#f7e4dc", foreground="#903d28")
        self.original_text.configure(state="disabled")

    def _build_paragraphs(self):
        for paragraph in self.session.paragraphs:
            index = paragraph.index
            card = tk.Frame(self.document, bg=self.colors["PANEL"], highlightthickness=1,
                            highlightbackground=self.colors["PANEL"], padx=10, pady=7)
            card.pack(fill="x", padx=15, pady=(5, 2))
            self._cards[index] = card
            marker = self._label(card, f"문단 {index + 1}" + (" · 읽기 전용" if not paragraph.editable else ""), size=9, muted=True)
            marker.pack(anchor="w")
            self._markers[index] = marker
            editor = tk.Text(card, wrap="word", relief="flat", bg=self.colors["PANEL"], fg=self.colors["INK"],
                             insertbackground=self.colors["ACCENT"], font=("Malgun Gothic", self._text_size),
                             height=2, padx=0, pady=3, spacing1=2, spacing3=4, undo=paragraph.editable, maxundo=100)
            editor.pack(fill="x")
            editor.insert("1.0", paragraph.corrected_text)
            editor.tag_configure("changed", background="#e9f2fa", foreground="#235880")
            editor.edit_modified(False)
            editor.bind("<<Modified>>", lambda _event, i=index: self._modified(i))
            editor.bind("<FocusIn>", lambda _event, i=index: self.select_paragraph(i))
            editor.bind("<Button-1>", lambda _event, i=index: self.select_paragraph(i), add="+")
            editor.bind("<MouseWheel>", lambda event: self._wheel(self.doc_canvas, event))
            editor.bind("<Button-4>", lambda _event: (self.doc_canvas.yview_scroll(-3, "units"), "break")[1])
            editor.bind("<Button-5>", lambda _event: (self.doc_canvas.yview_scroll(3, "units"), "break")[1])
            if not paragraph.editable:
                editor.configure(state="disabled")
                if "제목" not in paragraph.readonly_reason:
                    self._label(card, paragraph.readonly_reason or "문서 구조를 보존하기 위해 이 문단은 보기만 가능합니다.",
                                size=9, muted=True, justify="left", wraplength=320).pack(anchor="w")
            self._editors[index] = editor
            self._highlight(index)
        if self._paragraphs:
            self.select_paragraph(next(iter(self._paragraphs)))
        self._schedule_layout()

    @staticmethod
    def _wheel(canvas, event):
        canvas.yview_scroll(-3 if event.delta > 0 else 3, "units")
        return "break"

    def _initial_split(self):
        self._initial_job = None
        if not self._closed:
            self.panes.sashpos(0, self.panes.winfo_width() // 2)
            self._request_render()

    def _document_resized(self, event):
        self.doc_canvas.itemconfigure(self._doc_window, width=max(250, event.width))
        self._schedule_layout()

    def _schedule_layout(self):
        if not self._closed and self._layout_job is None:
            self._layout_job = self.after(80, self._layout_paragraphs)

    def _layout_paragraphs(self):
        self._layout_job = None
        if self._closed:
            return
        for editor in self._editors.values():
            lines = editor.count("1.0", "end-1c", "displaylines")
            height = max(1, (lines[0] if lines else 0) + 1)
            if int(editor.cget("height")) != height:
                editor.configure(height=height)
        self.doc_canvas.configure(scrollregion=self.doc_canvas.bbox("all"))

    def _modified(self, index):
        if self._closed:
            return
        editor = self._editors.get(index)
        if editor is None or not editor.edit_modified():
            return
        editor.edit_modified(False)
        if not self._paragraphs[index].editable:
            return
        self._update_status()
        self._schedule_layout()
        previous = self._change_jobs.pop(index, None)
        if previous:
            self.after_cancel(previous)
        self._change_jobs[index] = self.after(130, lambda: self._refresh_change(index))

    def _refresh_change(self, index):
        self._change_jobs.pop(index, None)
        if not self._closed:
            self._highlight(index)
            if index == self._active:
                self._update_original()

    def _changes(self):
        return {index: editor.get("1.0", "end-1c") for index, editor in self._editors.items()
                if self._paragraphs[index].editable and editor.get("1.0", "end-1c") != self._baseline[index]}

    def _tk_offset(self, widget, text, offset):
        # Text's "+ N chars" advances Unicode characters, unlike Tcl string
        # length on 8.6, which may count a supplementary character twice.
        return f"1.0+{offset}c"

    def _highlight(self, index):
        editor = self._editors[index]
        editor.tag_remove("changed", "1.0", "end")
        if not self._show_marks:
            return
        original = self._paragraphs[index].original_text
        current = editor.get("1.0", "end-1c")
        for tag, _, _, start, end in difflib.SequenceMatcher(None, original, current, autojunk=False).get_opcodes():
            if tag != "equal" and end > start:
                editor.tag_add("changed", self._tk_offset(editor, current, start), self._tk_offset(editor, current, end))

    def select_paragraph(self, index):
        if index not in self._paragraphs:
            return
        previous = self._active
        self._active = index
        if previous in self._cards:
            self._cards[previous].configure(highlightbackground=self.colors["PANEL"])
        self._cards[index].configure(highlightbackground="#c9b395")
        self.v_compare.set(f"선택 문단 · {index + 1}  /  원본 HWPX")
        self._update_original()

    def _update_original(self):
        if self._active is None:
            return
        paragraph = self._paragraphs[self._active]
        current = self._editors[self._active].get("1.0", "end-1c")
        text = self.original_text
        text.configure(state="normal")
        text.delete("1.0", "end")
        text.insert("1.0", paragraph.original_text)
        for tag, start, end, _, _ in difflib.SequenceMatcher(None, paragraph.original_text, current, autojunk=False).get_opcodes():
            if tag != "equal" and end > start:
                text.tag_add("changed", self._tk_offset(text, paragraph.original_text, start),
                             self._tk_offset(text, paragraph.original_text, end))
        text.configure(state="disabled")

    def _update_status(self):
        changes = self._changes()
        self.v_status.set(f"수정한 문단 {len(changes)}개 · 아직 저장하지 않음" if changes else "저장할 변경 없음")
        for index, marker in self._markers.items():
            readonly = not self._paragraphs[index].editable
            tail = " · 수정 중" if index in changes else " · 읽기 전용" if readonly else ""
            marker.configure(text=f"문단 {index + 1}{tail}",
                             fg=self.colors["ACCENT"] if index in changes else self.colors["MUTED"])
        state = "normal" if changes and not self.busy else "disabled"
        self.save_button.configure(state=state); self.revert_button.configure(state=state)
        self.reload_button.configure(state="disabled" if self.busy else "normal")

    def toggle_marks(self):
        self._show_marks = not self._show_marks
        self.marks_button.configure(text="차이 켜짐" if self._show_marks else "차이 꺼짐")
        for index in self._editors:
            self._highlight(index)

    def toggle_text_size(self):
        self._text_size = 14 if self._text_size == 12 else 12
        for editor in self._editors.values():
            editor.configure(font=("Malgun Gothic", self._text_size))
        self._schedule_layout()

    def toggle_original(self):
        self._show_original = not self._show_original
        if self._show_original:
            self.original_text.grid()
        else:
            self.original_text.grid_remove()
        self.original_button.configure(text="원문 접기" if self._show_original else "원문 펼치기")

    def revert_changes(self):
        if self.busy or not self.dirty:
            return
        if not messagebox.askyesno("이번 수정 취소", "아직 저장하지 않은 수정을 모두 되돌릴까요?\n마지막으로 저장된 교정문은 그대로 남습니다.", parent=self):
            return
        for index in self._changes():
            editor = self._editors[index]
            editor.delete("1.0", "end"); editor.insert("1.0", self._baseline[index])
            editor.edit_reset(); editor.edit_modified(False)
            self._highlight(index)
        self._update_original(); self._update_status(); self._schedule_layout()

    def _sync_page_buttons(self):
        self.v_page.set(str(self._pdf_page))
        self.previous_button.configure(state="disabled" if self._pdf_page <= self.pdf_pages[0] else "normal")
        self.next_button.configure(state="disabled" if self._pdf_page >= self.pdf_pages[1] else "normal")

    def set_page(self, page):
        page = min(self.pdf_pages[1], max(self.pdf_pages[0], int(page)))
        if page != self._pdf_page:
            self._pdf_page = page
            self.pdf_canvas.yview_moveto(0); self.pdf_canvas.xview_moveto(0)
            self._sync_page_buttons(); self._request_render()

    def change_page(self, offset):
        self.set_page(self._pdf_page + offset)

    def set_zoom(self, value):
        self._zoom = None if value is None else min(4.0, max(.35, float(value)))
        self.v_zoom.set("너비 맞춤" if self._zoom is None else f"{self._zoom * 100:.0f}%")
        self._request_render()

    def change_zoom(self, direction):
        if self._zoom is None:
            import pymupdf
            with pymupdf.open(self.pdf_path) as document:
                scale = max(180, self.pdf_canvas.winfo_width() - 32) / document[self._pdf_page - 1].rect.width
        else:
            scale = self._zoom
        self.set_zoom(scale * (1.2 if direction > 0 else 1 / 1.2))

    def _request_render(self):
        if self._closed:
            return
        self._render_generation += 1
        if self._render_job is not None:
            self.after_cancel(self._render_job)
        self._render_job = self.after(100, self._submit_render)

    def _submit_render(self):
        self._render_job = None
        if self._closed:
            return
        if self._render_future is not None:
            self._render_future[1].cancel()
        generation = self._render_generation
        self._render_future = (generation, self._executor.submit(
            render_pdf_page, self.pdf_path, self._pdf_page, self.pdf_canvas.winfo_width(), self._zoom))

    def _poll_render(self):
        self._poll_job = None
        if self._closed:
            return
        pending = self._render_future
        if pending and pending[1].done():
            self._render_future = None
            generation, future = pending
            if generation == self._render_generation and not future.cancelled():
                try:
                    picture = future.result()
                    self._display_pdf(picture)
                except Exception as exc:
                    self.pdf_canvas.delete("all")
                    self.pdf_canvas.create_text(20, 25, anchor="nw", width=max(180, self.pdf_canvas.winfo_width() - 40),
                                                text=f"PDF를 표시하지 못했습니다.\n{exc}", fill=self.colors["INK"])
        self._poll_job = self.after(60, self._poll_render)

    def _display_pdf(self, picture):
        from PIL import ImageTk
        self._photo = ImageTk.PhotoImage(picture, master=self)
        self.pdf_canvas.delete("all")
        self.pdf_canvas.create_image(16, 16, anchor="nw", image=self._photo)
        self.pdf_canvas.configure(scrollregion=(0, 0, picture.width + 32, picture.height + 32))
        picture.close()

    def _save_shortcut(self, _event=None):
        self.save()
        return "break"

    def _confirm_changes(self, changes):
        dialog = ChangeReviewDialog(self, changes, self._baseline, Path(self.session.corrected_path))
        self.wait_window(dialog)
        return dialog.accepted

    def save(self) -> bool:
        if self.busy or self._closed:
            return False
        changes = self._changes()
        if not changes:
            return True
        if not self.can_write():
            messagebox.showinfo("교정본 사용 중", "OCR·교정 작업이 끝난 뒤 저장하십시오. 입력한 수정은 이 창에 남아 있습니다.", parent=self)
            return False
        self._reviewing = True
        self._update_status()
        try:
            accepted = self._confirm_changes(changes)
        finally:
            self._reviewing = False
        if not accepted:
            self._update_status()
            return False
        if not self.can_write():
            self._update_status()
            messagebox.showinfo("교정본 사용 중", "다른 작업이 시작되어 저장하지 않았습니다. 입력한 수정은 이 창에 남아 있습니다.", parent=self)
            return False
        self._saving = True
        self._update_status()
        self.configure(cursor="watch")
        self.update_idletasks()
        try:
            result = self.session.save(changes)
        except (OSError, ValueError) as exc:
            messagebox.showerror("교정본을 저장하지 못했습니다", f"{exc}\n\n입력한 수정은 이 창에 남아 있습니다.", parent=self)
            return False
        finally:
            self._saving = False
            self.configure(cursor="")
            self._update_status()
        self._paragraphs = {p.index: p for p in self.session.paragraphs}
        self._baseline = {p.index: p.corrected_text for p in self.session.paragraphs}
        for editor in self._editors.values():
            editor.edit_modified(False)
        self._update_status()
        notice = f"저장됨 · 문단 {result.saved_count}개"
        if result.backup_path:
            notice += " · 수정 전 사본 보관"
        if result.reports_stale:
            notice += " · 기존 대조 보고는 저장 전 기록입니다."
        self.v_status.set(notice)
        if self.on_saved:
            try:
                self.on_saved(result.path)
            except Exception:
                self.v_status.set(notice + " · 목록은 다시 색인하면 갱신됩니다.")
        return True

    def close(self) -> bool:
        if self._closed:
            return True
        if self.busy:
            return False
        if self.dirty:
            choice = messagebox.askyesnocancel("저장하지 않은 수정",
                f"수정한 문단 {len(self._changes())}개가 있습니다.\n\n"
                "예: 변경 내용을 확인하고 저장\n아니요: 이번 수정을 버리고 닫기\n취소: 계속 편집", parent=self)
            if choice is None or (choice and not self.save()):
                return False
        self.destroy()
        return True

    def reload(self) -> bool:
        if self.busy or self._closed:
            return False
        if self.dirty:
            choice = messagebox.askyesnocancel("교정본 다시 불러오기",
                "저장하지 않은 수정이 있습니다.\n\n예: 변경 확인·저장 후 불러오기\n"
                "아니요: 이번 수정을 버리고 불러오기\n취소: 계속 편집", parent=self)
            if choice is None or (choice and not self.save()):
                return False
        from .section_edit import SectionEditSession
        try:
            session = SectionEditSession.open(self.pdf_path, self.hwpx_path, self.section_name,
                                              self.section_start, self.section_end, **self._scope_options)
        except (OSError, ValueError) as exc:
            messagebox.showerror("교정본을 다시 불러오지 못했습니다", f"{exc}\n\n현재 화면과 입력한 수정은 그대로 남아 있습니다.", parent=self)
            return False
        for job in self._change_jobs.values():
            self.after_cancel(job)
        self._change_jobs.clear()
        previous = self._active
        for card in self._cards.values():
            card.destroy()
        self.session = session
        self._paragraphs = {p.index: p for p in session.paragraphs}
        self._baseline = {p.index: p.corrected_text for p in session.paragraphs}
        self._editors.clear(); self._cards.clear(); self._markers.clear()
        self._active = None
        self._build_paragraphs()
        if previous in self._paragraphs:
            self.select_paragraph(previous)
        self._update_status()
        self.v_status.set("교정본을 다시 불러왔습니다.")
        self._request_render()
        return True

    def _cleanup(self):
        if self._closed:
            return
        self._closed = True
        for job in (self._initial_job, self._render_job, self._poll_job, self._layout_job, *self._change_jobs.values()):
            if job is not None:
                try:
                    self.after_cancel(job)
                except tk.TclError:
                    pass
        if self._render_future is not None:
            self._render_future[1].cancel()
        self._executor.shutdown(wait=False, cancel_futures=True)
        self._photo = None

    def _destroyed(self, event):
        if event.widget is self:
            self._cleanup()

    def destroy(self):
        self._cleanup()
        super().destroy()
