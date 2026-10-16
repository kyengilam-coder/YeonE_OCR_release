"""Korean desktop workbench for reference-based OCR evaluation.

Workers return plain data through a queue; every Tk operation stays on the
window's thread. Texts in the panes are working copies, never source files.
"""
from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
import queue
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from typing import Callable


EXAMPLE_REFERENCE = "한글과 漢字를 함께 읽습니다.\n天地玄黃 宇宙洪荒\n대한민국의 문헌을 정확하게 기록합니다.\n學而時習之 不亦說乎\n"
EXAMPLE_HYPOTHESIS = "한글과 漢宇를 함께 읽습니다.\n天地玄黃 宇宙洪荒\n대한민극의 문헌을 정확하게 기록합니다.\n學而時習之 不亦乎\n"
OP_LABELS = {"substitution": "대치", "deletion": "누락", "insertion": "삽입"}
WHITESPACE = {"그대로 비교": "preserve", "연속 공백 합치기": "collapse", "모든 공백 제외": "remove"}
UNICODE = {"원문 그대로": "none", "한글 조합 NFC": "NFC"}


def _cer_text(summary: dict) -> str:
    value = summary.get("cer_percent")
    if value is None and summary.get("cer") is not None:
        value = summary["cer"] * 100
    return "산출 불가" if value is None else f"{value:.2f}%"


def _text_index(widget: tk.Text, text: str, position: int) -> str:
    """Translate Python codepoint offsets to this Tcl build's text indices."""
    prefix = text[:max(0, min(len(text), position))]
    line = prefix.count("\n") + 1
    column = int(widget.tk.call("string", "length", prefix.rsplit("\n", 1)[-1]))
    return f"{line}.{column}"


class EvaluationWindow(tk.Tk):
    """An independent AB_OCR window; importing this module has no side effects."""

    def __init__(self):
        super().__init__()
        self.title("AB_OCR · 한글·한자 정확도 평가")
        self.geometry("1280x880")
        self.minsize(960, 720)
        self.configure(background="#edf1f6")
        self.protocol("WM_DELETE_WINDOW", self._close)
        self._events: queue.Queue = queue.Queue()
        self._job_id = 0
        self._busy = False
        self._job_cancellable = True
        self._close_after_write = False
        self._cancel = threading.Event()
        self._closed = False
        self._loading = False
        self._revision = 0
        self._report: dict | None = None
        self._report_revision = -1
        self._report_kind = ""
        self._error_rows: dict[str, dict] = {}
        self._protected_paths: set[Path] = set()
        self._loaded_text = {"reference": "", "hypothesis": ""}
        self._saved_text: dict[str, str | None] = {"reference": "", "hypothesis": ""}
        self._input_metadata: dict[str, dict | None] = {"reference": None, "hypothesis": None}
        self._ocr_unsaved = False
        self._text_labels = {"reference": "아직 불러온 정답이 없습니다", "hypothesis": "아직 불러온 결과가 없습니다"}
        self._edited = {"reference": False, "hypothesis": False}
        self._ocr_record: dict | None = None
        self._manifest_path: Path | None = None
        self._controls: list[tuple[tk.Widget, str]] = []
        self._after_id = None
        self._style()
        self._build()
        self._after_id = self.after(80, self._poll)
        self.bind("<Control-Return>", lambda _event: self.compare())
        self.bind("<Escape>", lambda _event: self.cancel())

    def _style(self):
        style = ttk.Style(self)
        if "clam" in style.theme_names():
            style.theme_use("clam")
        style.configure("AB.TFrame", background="#edf1f6")
        style.configure("AB.Card.TFrame", background="#ffffff")
        style.configure("AB.TLabel", background="#edf1f6", foreground="#27364c", font=("맑은 고딕", 10))
        style.configure("AB.Hint.TLabel", background="#edf1f6", foreground="#617086", font=("맑은 고딕", 9))
        style.configure("AB.Card.TLabel", background="white", foreground="#27364c", font=("맑은 고딕", 10))
        style.configure("AB.Number.TLabel", background="white", foreground="#174fa3", font=("맑은 고딕", 23, "bold"))
        style.configure("AB.Button.TButton", font=("맑은 고딕", 9), padding=(10, 7))
        style.configure("AB.Primary.TButton", font=("맑은 고딕", 10, "bold"), padding=(17, 8), background="#245bba", foreground="white")
        style.map("AB.Primary.TButton", background=[("active", "#194991"), ("disabled", "#a6b4c8")])
        style.configure("AB.Treeview", rowheight=29, font=("맑은 고딕", 10), background="white", fieldbackground="white")
        style.configure("AB.Treeview.Heading", font=("맑은 고딕", 9, "bold"), padding=(5, 6))
        style.map("AB.Treeview", background=[("selected", "#dbe8ff")], foreground=[("selected", "#13294b")])
        style.configure("AB.TNotebook", background="#edf1f6", borderwidth=0)
        style.configure("AB.TNotebook.Tab", font=("맑은 고딕", 10), padding=(14, 7))

    def _button(self, parent, text, command, *, primary=False, **kwargs):
        button = ttk.Button(parent, text=text, command=command,
                            style="AB.Primary.TButton" if primary else "AB.Button.TButton", **kwargs)
        self._controls.append((button, "normal"))
        return button

    def _build(self):
        self.columnconfigure(0, weight=1)
        self.rowconfigure(3, weight=3, minsize=245)
        self.rowconfigure(5, weight=2, minsize=150)
        header = tk.Frame(self, background="#152a46", padx=20, pady=13)
        header.grid(row=0, column=0, sticky="ew")
        tk.Label(header, text="AB", font=("Segoe UI", 22, "bold"), foreground="white", background="#285fb8", padx=10, pady=3).pack(side="left", padx=(0, 14))
        titles = tk.Frame(header, background="#152a46")
        titles.pack(side="left")
        tk.Label(titles, text="AB_OCR", font=("Segoe UI", 20, "bold"), foreground="white", background="#152a46", anchor="w").pack(fill="x")
        tk.Label(titles, text="한글 · 한자 정확도 평가 작업대", font=("맑은 고딕", 10), foreground="#b9cbe5", background="#152a46").pack(anchor="w")
        tk.Label(header, text="정답 기반 검증\nCER는 낮을수록 좋습니다", font=("맑은 고딕", 10), foreground="#cfdef4", background="#152a46", justify="right").pack(side="right")

        toolbar = ttk.Frame(self, style="AB.TFrame", padding=(16, 10, 16, 6))
        toolbar.grid(row=1, column=0, sticky="ew")
        self._button(toolbar, "정답 TXT 열기", lambda: self.load_text("reference")).pack(side="left", padx=(0, 6))
        self._button(toolbar, "OCR 결과 TXT 열기", lambda: self.load_text("hypothesis")).pack(side="left", padx=(0, 6))
        self._button(toolbar, "여러 엔진 일괄 평가…", self.open_benchmark).pack(side="left", padx=(0, 6))
        self._button(toolbar, "예제", self.load_example).pack(side="left", padx=(0, 6))
        self._button(toolbar, "엔진 점검", self.check_engine).pack(side="left")
        self.cancel_button = ttk.Button(toolbar, text="취소", command=self.cancel, style="AB.Button.TButton", state="disabled")
        self.cancel_button.pack(side="right", padx=(6, 0))
        self.compare_button = self._button(toolbar, "정확도 비교  Ctrl+Enter", self.compare, primary=True)
        self.compare_button.pack(side="right")

        policy = ttk.Frame(self, style="AB.TFrame", padding=(18, 0, 18, 8))
        policy.grid(row=2, column=0, sticky="ew")
        ttk.Label(policy, text="비교용 정규화", style="AB.TLabel").pack(side="left", padx=(0, 12))
        self.unicode_var = tk.StringVar(self, "원문 그대로")
        self.whitespace_var = tk.StringVar(self, "그대로 비교")
        for variable, values, width in ((self.unicode_var, UNICODE, 16), (self.whitespace_var, WHITESPACE, 18)):
            box = ttk.Combobox(policy, textvariable=variable, values=list(values), width=width, state="readonly")
            box.pack(side="left", padx=(0, 8))
            self._controls.append((box, "readonly"))
            box.bind("<<ComboboxSelected>>", lambda _e: self._invalidate())
        ttk.Label(policy, text="한자 이체·간체·번체와 문장부호는 그대로 비교", style="AB.Hint.TLabel").pack(side="left", padx=(8, 0))

        panes = ttk.Panedwindow(self, orient="horizontal")
        panes.grid(row=3, column=0, sticky="nsew", padx=16)
        self.text_widgets: dict[str, tk.Text] = {}
        self.label_vars: dict[str, tk.StringVar] = {}
        self.count_vars: dict[str, tk.StringVar] = {}
        for key, title, subtitle in (("reference", "정답 원문", "사람이 원본과 대조한 텍스트"),
                                     ("hypothesis", "OCR 결과 · 비교용 사본", "인식 결과를 불러오거나 직접 붙여넣으세요")):
            card = ttk.Frame(panes, style="AB.Card.TFrame", padding=12)
            panes.add(card, weight=1)
            card.columnconfigure(0, weight=1)
            card.rowconfigure(2, weight=1)
            heading = ttk.Frame(card, style="AB.Card.TFrame")
            heading.grid(row=0, column=0, sticky="ew")
            ttk.Label(heading, text=title, style="AB.Card.TLabel", font=("맑은 고딕", 11, "bold")).pack(side="left")
            self.count_vars[key] = tk.StringVar(self, "0자")
            ttk.Label(heading, textvariable=self.count_vars[key], style="AB.Card.TLabel").pack(side="right")
            self.label_vars[key] = tk.StringVar(self, subtitle)
            ttk.Label(card, textvariable=self.label_vars[key], style="AB.Card.TLabel", foreground="#6b788c").grid(row=1, column=0, sticky="ew", pady=(5, 7))
            text_frame = ttk.Frame(card, style="AB.Card.TFrame")
            text_frame.grid(row=2, column=0, sticky="nsew")
            text_frame.columnconfigure(0, weight=1)
            text_frame.rowconfigure(0, weight=1)
            widget = tk.Text(text_frame, wrap="word", undo=True, maxundo=200, font=("맑은 고딕", 12),
                             background="#fcfdff", foreground="#202e42", selectbackground="#d3e4ff", selectforeground="#182a42",
                             insertbackground="#245bba", relief="flat", highlightthickness=1, highlightbackground="#dbe1eb",
                             padx=10, pady=10, spacing1=3, spacing3=3, height=9, width=32)
            scroll = ttk.Scrollbar(text_frame, orient="vertical", command=widget.yview)
            widget.configure(yscrollcommand=scroll.set)
            widget.grid(row=0, column=0, sticky="nsew")
            scroll.grid(row=0, column=1, sticky="ns")
            widget.tag_configure("error", background="#ffe5b8", foreground="#653e00")
            widget.bind("<<Modified>>", lambda _e, k=key: self._text_changed(k))
            self.text_widgets[key] = widget
            footer = ttk.Frame(card, style="AB.Card.TFrame")
            footer.grid(row=3, column=0, sticky="ew", pady=(7, 0))
            if key == "reference":
                self._button(footer, "입력 전으로 복원", lambda k=key: self.restore_text(k)).pack(side="left")
                self._button(footer, "사본 TXT 저장…", lambda k=key: self.save_text(k)).pack(side="right")
            else:
                self._button(footer, "원시 복원", lambda k=key: self.restore_text(k)).pack(side="left")
                save_menu_button = ttk.Menubutton(footer, text="결과 저장 ▾", style="AB.Button.TButton")
                save_menu_button.pack(side="left", padx=5)
                self._controls.append((save_menu_button, "normal"))
                menu = tk.Menu(save_menu_button, tearoff=False)
                menu.add_command(label="현재 비교용 사본 TXT 저장…", command=lambda: self.save_text("hypothesis"))
                menu.add_command(label="처음 불러온 원시 TXT 저장…", command=lambda: self.save_text("hypothesis", raw=True))
                menu.add_command(label="로컬 OCR 원시 기록 JSON 저장…", command=self.save_ocr_record)
                menu.configure(postcommand=lambda: menu.entryconfigure(2, state="normal" if self._ocr_record else "disabled"))
                save_menu_button.configure(menu=menu)
                self._button(footer, "이미지/PDF 로컬 OCR…", self.recognize_file).pack(side="right")

        summary = ttk.Frame(self, style="AB.TFrame", padding=(16, 10, 16, 10))
        summary.grid(row=4, column=0, sticky="ew")
        summary.columnconfigure(0, weight=1)
        summary.columnconfigure(1, weight=1)
        summary.columnconfigure(2, weight=2)
        self.raw_var = tk.StringVar(self, "—")
        self.normalized_var = tk.StringVar(self, "—")
        self.operations_var = tk.StringVar(self, "대치 —   누락 —   삽입 —")
        self.scripts_var = tk.StringVar(self, "정답과 OCR 결과를 입력한 뒤 비교하세요.")
        for col, label, variable in ((0, "원문 CER", self.raw_var), (1, "정규화 CER", self.normalized_var)):
            box = ttk.Frame(summary, style="AB.Card.TFrame", padding=(15, 10))
            box.grid(row=0, column=col, sticky="nsew", padx=(0, 10))
            ttk.Label(box, text=label, style="AB.Card.TLabel").pack(anchor="w")
            ttk.Label(box, textvariable=variable, style="AB.Number.TLabel").pack(anchor="w")
        box = ttk.Frame(summary, style="AB.Card.TFrame", padding=(15, 10))
        box.grid(row=0, column=2, sticky="nsew")
        ttk.Label(box, textvariable=self.operations_var, style="AB.Card.TLabel", font=("맑은 고딕", 12, "bold")).pack(anchor="w", pady=(2, 6))
        ttk.Label(box, textvariable=self.scripts_var, style="AB.Card.TLabel", foreground="#5c6d83").pack(anchor="w")

        self.notebook = ttk.Notebook(self, style="AB.TNotebook")
        self.notebook.grid(row=5, column=0, sticky="nsew", padx=16)
        errors = ttk.Frame(self.notebook, style="AB.Card.TFrame", padding=8)
        self.notebook.add(errors, text="글자 오류 대조")
        errors.columnconfigure(0, weight=1)
        errors.rowconfigure(1, weight=1)
        self.error_info = tk.StringVar(self, "오류 행을 선택하면 양쪽 텍스트의 해당 위치로 이동합니다. 원문 기준 위치를 표시합니다.")
        ttk.Label(errors, textvariable=self.error_info, style="AB.Card.TLabel").grid(row=0, column=0, sticky="ew", pady=(0, 6))
        self.error_tree = self._tree(errors, ("operation", "reference", "hypothesis", "change", "context"),
                                     (("유형", 65), ("정답 위치", 85), ("결과 위치", 85), ("정답 → 결과", 145), ("정답 주변 문맥", 560)))
        self.error_tree.bind("<<TreeviewSelect>>", self._select_error)
        benchmark = ttk.Frame(self.notebook, style="AB.Card.TFrame", padding=8)
        self.notebook.add(benchmark, text="엔진별 일괄 평가")
        benchmark.columnconfigure(0, weight=1)
        benchmark.rowconfigure(1, weight=1)
        self.benchmark_info = tk.StringVar(self, "평가 목록 JSON에 정답과 각 엔진의 결과 TXT를 지정해 같은 자료로 비교합니다.")
        ttk.Label(benchmark, textvariable=self.benchmark_info, style="AB.Card.TLabel").grid(row=0, column=0, sticky="ew", pady=(0, 6))
        self.benchmark_tree = self._tree(benchmark, ("engine", "pages", "raw", "normalized", "status"),
                                         (("엔진", 220), ("완료 쪽", 95), ("원문 CER", 125), ("정규화 CER", 125), ("평가 상태", 330)))

        bottom = ttk.Frame(self, style="AB.TFrame", padding=(16, 8, 16, 10))
        bottom.grid(row=6, column=0, sticky="ew")
        self.status_var = tk.StringVar(self, "준비 · 로컬에서 비교합니다. 엔진 신뢰도는 실제 정확도와 다릅니다.")
        self.progress = ttk.Progressbar(bottom, mode="indeterminate", length=80)
        self.progress.pack(side="left", padx=(0, 10))
        ttk.Label(bottom, textvariable=self.status_var, style="AB.Hint.TLabel", wraplength=750).pack(side="left", fill="x", expand=True)
        self.export_button = self._button(bottom, "평가 보고서 저장…", self.save_report)
        self.export_button.pack(side="right", padx=(8, 0))
        self.export_button.configure(state="disabled")

    def _tree(self, parent, columns, headings):
        tree = ttk.Treeview(parent, columns=columns, show="headings", selectmode="browse", style="AB.Treeview", height=4)
        for name, (label, width) in zip(columns, headings):
            tree.heading(name, text=label)
            tree.column(name, width=width, minwidth=45, stretch=name == columns[-1], anchor="w")
        tree.grid(row=1, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(parent, orient="vertical", command=tree.yview)
        scrollbar.grid(row=1, column=1, sticky="ns")
        tree.configure(yscrollcommand=scrollbar.set)
        return tree

    def _text(self, key):
        return self.text_widgets[key].get("1.0", "end-1c")

    def _text_changed(self, key):
        widget = self.text_widgets[key]
        if not widget.edit_modified():
            return
        widget.edit_modified(False)
        if self._loading:
            return
        text = self._text(key)
        self._edited[key] = text != self._loaded_text[key]
        self.count_vars[key].set(f"{len(text):,}자")
        label = self._text_labels[key]
        self.label_vars[key].set(f"{label}{' · 수정됨' if self._edited[key] else ''}")
        self._invalidate()

    def _set_text(self, key, text, label, *, loaded=True, saved=True):
        widget = self.text_widgets[key]
        self._loading = True
        try:
            previous_state = str(widget.cget("state"))
            widget.configure(state="normal")
            widget.delete("1.0", "end")
            widget.insert("1.0", text)
            widget.edit_reset()
            widget.edit_modified(False)
            widget.configure(state=previous_state)
            if loaded:
                self._loaded_text[key] = text
                self._saved_text[key] = text if saved else None
                self._input_metadata[key] = None
                self._text_labels[key] = label
                self._edited[key] = False
            self.label_vars[key].set(label)
            self.count_vars[key].set(f"{len(text):,}자")
        finally:
            self._loading = False
        self._invalidate()

    def _invalidate(self):
        self._revision += 1
        self._report = None
        self._error_rows.clear()
        for widget in self.text_widgets.values():
            widget.tag_remove("error", "1.0", "end")
        if hasattr(self, "error_tree"):
            self.error_tree.delete(*self.error_tree.get_children())
            self.benchmark_tree.delete(*self.benchmark_tree.get_children())
            self.raw_var.set("—")
            self.normalized_var.set("—")
            self.operations_var.set("대치 —   누락 —   삽입 —")
            self.scripts_var.set("입력 또는 비교 조건이 변경되었습니다. 다시 비교하세요.")
            self.export_button.configure(state="disabled")

    def _policy(self):
        from .evaluation import NormalizationPolicy
        return NormalizationPolicy(unicode_form=UNICODE[self.unicode_var.get()], whitespace=WHITESPACE[self.whitespace_var.get()])

    def _confirm_discard(self, keys, action):
        unsaved = []
        for key in keys:
            if self._text(key) != self._saved_text[key]:
                unsaved.append("정답의 변경 내용" if key == "reference" else "OCR 결과의 변경 내용")
        if "hypothesis" in keys and self._ocr_unsaved:
            unsaved.append("로컬 OCR 원시 결과")
        if not unsaved:
            return True
        return messagebox.askyesno("저장하지 않은 내용", "저장하지 않은 " + ", ".join(unsaved)
                                  + f"이 있습니다.\n\n이 내용을 버리고 {action}할까요?\n보존하려면 [아니요]를 누른 뒤 TXT 또는 원시 기록을 저장하세요.", parent=self)

    def load_text(self, key, path=None):
        if self._busy:
            return
        if path is None:
            path = filedialog.askopenfilename(parent=self, title="정답 TXT 열기" if key == "reference" else "OCR 결과 TXT 열기",
                                              filetypes=[("UTF-8 텍스트", "*.txt"), ("모든 파일", "*.*")])
        if not path:
            return
        if not self._confirm_discard((key,), "다른 텍스트를 불러오기"):
            return
        source = Path(path).resolve()
        def worker(_stop, _progress):
            from .reports import read_utf8_snapshot
            try:
                return read_utf8_snapshot(source)
            except ValueError as exc:
                raise ValueError("TXT 파일은 UTF-8 형식, 8 MB 이하로 준비해 주세요. 큰 문서는 쪽 단위로 나누세요.\n" + str(exc)) from exc
        def done(snapshot):
            text, file_hash = snapshot
            self._protected_paths.add(source)
            self._set_text(key, text, source.name)
            self._input_metadata[key] = {"path": str(source), "file_sha256": file_hash,
                                         "encoding": "utf-8-sig", "newline_translation": False}
            if key == "hypothesis":
                self._ocr_record = None
                self._ocr_unsaved = False
            self.status_var.set(f"불러옴 · {source.name} · UTF-8 · {len(text):,}자")
        self._start("텍스트 불러오는 중…", worker, done)

    def restore_text(self, key):
        if self._busy:
            return
        if self._text(key) != self._saved_text[key] and not self._confirm_discard((key,), "처음 불러온 텍스트로 복원"):
            return
        self._set_text(key, self._loaded_text[key], self._text_labels[key], loaded=False)
        self._edited[key] = False
        self.status_var.set("처음 불러온 텍스트를 복원했습니다. 원본 파일은 변경하지 않았습니다.")

    def load_example(self):
        if self._busy:
            return
        if not self._confirm_discard(("reference", "hypothesis"), "예제를 불러오기"):
            return
        self._set_text("reference", EXAMPLE_REFERENCE, "합성 예제 · 정답")
        self._set_text("hypothesis", EXAMPLE_HYPOTHESIS, "합성 예제 · 의도적으로 오류를 넣은 텍스트")
        self._ocr_record = None
        self._ocr_unsaved = False
        self.status_var.set("합성 예제 · 실제 OCR 엔진의 성능 측정 자료가 아닙니다.")
        self.compare()

    def compare(self):
        if self._busy:
            return
        reference, hypothesis = self._text("reference"), self._text("hypothesis")
        if not reference and not hypothesis:
            self.status_var.set("정답과 OCR 결과를 입력하거나 [예제]를 선택하세요.")
            self.text_widgets["reference"].focus_set()
            return
        policy = self._policy()
        revision = self._revision
        metadata = {
            "reference_label": self._text_labels["reference"], "result_label": self._text_labels["hypothesis"],
            "reference_sha256": hashlib.sha256(reference.encode("utf-8")).hexdigest(),
            "result_sha256": hashlib.sha256(hypothesis.encode("utf-8")).hexdigest(),
            "reference_edited": self._edited["reference"], "result_edited": self._edited["hypothesis"],
            "synthetic_example": self._text_labels["reference"].startswith("합성 예제"),
        }
        if self._ocr_record:
            metadata["recognition"] = {k: v for k, v in self._ocr_record.items() if k != "text"}
        for key, label in (("reference", "reference_source"), ("hypothesis", "result_source")):
            if self._input_metadata[key] is not None:
                metadata[label] = dict(self._input_metadata[key])
        def worker(_stop, _progress):
            from .evaluation import compare_texts
            report = compare_texts(reference, hypothesis, policy=policy)
            report["metadata"] = metadata
            return report
        def done(report):
            if revision != self._revision:
                self.status_var.set("입력이 바뀌어 이전 비교 결과를 반영하지 않았습니다.")
                return
            self._show_comparison(report)
        self._start("원문과 정규화 텍스트를 비교하는 중…", worker, done)

    def _show_comparison(self, report):
        self._report, self._report_revision, self._report_kind = report, self._revision, "text"
        self.benchmark_tree.delete(*self.benchmark_tree.get_children())
        self.benchmark_info.set("현재 화면은 두 텍스트의 비교 결과입니다. 평가 목록을 열면 여러 엔진을 함께 비교합니다.")
        raw, normalized = report["raw"], report["normalized"]
        self.raw_var.set(_cer_text(raw))
        self.normalized_var.set(_cer_text(normalized))
        self.operations_var.set(f"대치 {raw['substitutions']:,}   누락 {raw['deletions']:,}   삽입 {raw['insertions']:,}")
        by_script = raw.get("by_script", {})
        parts = []
        for names, label in ((('hangul', 'Hangul', '한글'), "한글"), (('hanja', 'Han', 'han', '한자'), "한자")):
            stats = next((by_script[k] for k in names if k in by_script), None)
            if stats:
                parts.append(f"{label}  대치 {stats['substitutions']} · 누락 {stats['deletions']} · 삽입 {stats['insertions']}")
        self.scripts_var.set("\n".join(parts) if parts else f"정답 {raw['reference_length']:,}자 · 결과 {raw['hypothesis_length']:,}자")
        self.error_tree.delete(*self.error_tree.get_children())
        self._error_rows.clear()
        for index, error in enumerate(raw.get("errors", [])):
            iid = str(index)
            self._error_rows[iid] = error
            before = error.get("reference", "").replace("\n", "↵").replace("\r", "␍") or "∅"
            after = error.get("hypothesis", "").replace("\n", "↵").replace("\r", "␍") or "∅"
            self.error_tree.insert("", "end", iid=iid, values=(OP_LABELS.get(error["operation"], error["operation"]),
                error["reference_position"] + 1, error["hypothesis_position"] + 1, f"{before} → {after}",
                error.get("reference_context", "").replace("\n", " ↵ ").replace("\r", "")))
        count = len(self._error_rows)
        self.error_info.set(f"원문 오류 {raw.get('distance', raw.get('edit_distance', 0)):,}개 · {count:,}개 표시"
                            + (" (표시 한도 초과 — 총 오류율 계산에는 모두 포함)" if raw.get("errors_truncated") else " · 행을 선택해 원문 위치 확인"))
        self.notebook.select(0)
        self.export_button.configure(state="normal")
        note = " · 합성 예제이며 엔진 성능 결과가 아닙니다" if report.get("metadata", {}).get("synthetic_example") else ""
        self.status_var.set(f"비교 완료 · 원문 CER {_cer_text(raw)} · 정답 대비 글자 오류율{note}")

    def _select_error(self, _event=None):
        selected = self.error_tree.selection()
        if not selected or selected[0] not in self._error_rows:
            return
        error = self._error_rows[selected[0]]
        for key, pos_key in (("reference", "reference_position"), ("hypothesis", "hypothesis_position")):
            widget = self.text_widgets[key]
            text = self._text(key)
            position = error[pos_key]
            start = _text_index(widget, text, position)
            end = _text_index(widget, text, position + len(error.get(key, "")))
            widget.tag_remove("error", "1.0", "end")
            if start != end:
                widget.tag_add("error", start, end)
            widget.mark_set("insert", start)
            widget.see(start)
        self.status_var.set(f"{OP_LABELS.get(error['operation'], error['operation'])} · 정답 {error['reference_position'] + 1}번째 / 결과 {error['hypothesis_position'] + 1}번째 · ∅는 해당 글자가 없음을 뜻합니다.")

    def open_benchmark(self, path=None):
        if self._busy:
            return
        if path is None:
            path = filedialog.askopenfilename(parent=self, title="평가 목록 JSON 열기", filetypes=[("평가 목록", "*.json")])
        if not path:
            return
        source, policy = Path(path).resolve(), self._policy()
        def worker(stop, progress):
            from .benchmark import evaluate_manifest
            return evaluate_manifest(source, policy=policy, should_stop=stop.is_set,
                                     progress=lambda done, total: progress(f"일괄 평가 {done} / {total}"))
        def done(report):
            self._manifest_path = source
            self._protected_paths.add(source)
            self._protected_paths.update(Path(p).resolve() for p in report.get("protected_input_paths", []))
            self._show_benchmark(report)
        self._start("평가 목록의 정답과 엔진별 결과를 읽는 중…", worker, done)

    def _show_benchmark(self, report):
        self._report, self._report_revision, self._report_kind = report, self._revision, "benchmark"
        self.error_tree.delete(*self.error_tree.get_children())
        self._error_rows.clear()
        self.error_info.set("일괄 평가의 쪽별 글자 오류는 저장한 보고서에서 확인하세요. 위 텍스트를 대조하려면 [정확도 비교]를 누르세요.")
        for widget in self.text_widgets.values():
            widget.tag_remove("error", "1.0", "end")
        self.benchmark_tree.delete(*self.benchmark_tree.get_children())
        for engine_id, engine in report.get("engines", {}).items():
            complete = engine.get("complete", False)
            count, total = engine.get("completed_pages", 0), engine.get("total_pages", 0)
            self.benchmark_tree.insert("", "end", values=(engine.get("name", engine_id), f"{count} / {total}",
                _cer_text(engine.get("raw", {})), _cer_text(engine.get("normalized", {})),
                "모든 쪽 평가 완료" if complete else "미완료 · 누락/오류 확인 필요"))
        self.benchmark_info.set("엔진별 CER는 완료한 쪽의 글자 수로 가중 합산합니다. 자료가 다르면 단순 순위를 매기지 않습니다.")
        self.notebook.select(1)
        self.raw_var.set("일괄 평가")
        self.normalized_var.set("엔진별 확인")
        self.operations_var.set(f"{len(report.get('engines', {}))}개 엔진 비교")
        self.scripts_var.set("쪽별 결과·누락·정규화 정책은 저장한 보고서에서 확인")
        self.export_button.configure(state="normal")
        self.status_var.set("일괄 평가 완료 · [평가 보고서 저장]으로 상세 결과를 보관하세요.")

    def save_report(self, path=None):
        if self._busy or self._report is None:
            return
        if path is None:
            formats = [("HTML 보고서", "*.html"), ("JSON 데이터", "*.json")]
            if self._report_kind != "benchmark":
                formats.append(("CSV 요약", "*.csv"))
            path = filedialog.asksaveasfilename(parent=self, title="평가 보고서 저장", defaultextension=".html",
                                               initialfile="AB_OCR_평가.html", filetypes=formats)
        if not path:
            return
        report, protected = self._report, tuple(self._protected_paths)
        destination = Path(path)
        def worker(_stop, _progress):
            from .reports import write_report
            write_report(report, destination, protected_paths=protected)
            return destination
        self._start("평가 보고서를 저장하는 중…", worker,
                    lambda saved: self.status_var.set(f"보고서 저장 완료 · {saved}"), cancellable=False)

    def check_engine(self):
        if self._busy:
            return
        def worker(stop, _progress):
            from .ocr import doctor
            return doctor(should_stop=stop.is_set)
        def done(info):
            languages = info.get("languages", [])
            message = str(info.get("message") or ("로컬 OCR 엔진을 사용할 수 있습니다." if info.get("available") else "로컬 OCR 엔진을 찾지 못했습니다."))
            if languages:
                message += "\n\n설치된 언어: " + ", ".join(languages)
            messagebox.showinfo("로컬 OCR 엔진 점검", message, parent=self)
            self.status_var.set("엔진 점검 완료 · 엔진 설치 여부와 언어 지원은 실제 인식 정확도와 별개입니다.")
        self._start("로컬 OCR 엔진과 언어 데이터를 확인하는 중…", worker, done)

    def save_text(self, key, *, raw=False, path=None):
        if self._busy:
            return
        if path is None:
            path = filedialog.asksaveasfilename(parent=self, title="원시 텍스트 저장" if raw else "비교용 사본 저장", defaultextension=".txt",
                                               initialfile="AB_OCR_원시결과.txt" if raw else "AB_OCR_비교용사본.txt", filetypes=[("UTF-8 텍스트", "*.txt")])
        if not path:
            return
        text = self._loaded_text[key] if raw else self._text(key)
        protected = tuple(self._protected_paths)
        def worker(_stop, _progress):
            from .reports import write_text
            return write_text(text, path, protected_paths=protected)
        def done(destination):
            if not raw or self._text(key) == text:
                self._saved_text[key] = text
            if key == "hypothesis" and text == self._loaded_text[key]:
                self._ocr_unsaved = False
            self.status_var.set(f"{'원시 텍스트' if raw else '비교용 사본'} 저장 완료 · {destination}")
        self._start("텍스트를 저장하는 중…", worker, done, cancellable=False)

    def save_ocr_record(self, path=None):
        if self._busy or self._ocr_record is None:
            return
        if path is None:
            path = filedialog.asksaveasfilename(parent=self, title="원시 OCR 기록 저장", defaultextension=".json", initialfile="AB_OCR_원시인식.json", filetypes=[("JSON 원시 기록", "*.json")])
        if not path:
            return
        record, protected = self._ocr_record, tuple(self._protected_paths)
        def worker(_stop, _progress):
            from .reports import write_report
            return write_report(record, path, protected_paths=protected)
        def done(_saved):
            self._ocr_unsaved = False
            if self._text("hypothesis") == record["text"]:
                self._saved_text["hypothesis"] = record["text"]
            self.status_var.set(f"원시 OCR 기록 저장 완료 · {path}")
        self._start("원시 OCR 기록을 저장하는 중…", worker, done, cancellable=False)

    def recognize_file(self):
        if self._busy:
            return
        path = filedialog.askopenfilename(parent=self, title="로컬 OCR 원본 선택", filetypes=[("PDF·이미지", "*.pdf *.png *.jpg *.jpeg *.tif *.tiff *.bmp *.webp"), ("모든 파일", "*.*")])
        if not path:
            return
        dialog = tk.Toplevel(self)
        dialog.title("로컬 OCR 실행 조건")
        dialog.transient(self)
        dialog.resizable(False, False)
        body = ttk.Frame(dialog, padding=18)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text=Path(path).name, font=("맑은 고딕", 11, "bold")).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 12))
        ttk.Label(body, text="언어 데이터").grid(row=1, column=0, sticky="w", padx=(0, 12))
        language = tk.StringVar(dialog, "kor+chi_tra+eng")
        ttk.Combobox(body, textvariable=language, width=30, values=("kor+chi_tra+eng", "kor+eng", "chi_tra+eng", "chi_sim+eng", "eng")).grid(row=1, column=1, sticky="ew")
        ttk.Label(body, text="쪽 번호 (1부터)").grid(row=2, column=0, sticky="w", pady=10)
        page = tk.StringVar(dialog, "1")
        ttk.Spinbox(body, from_=1, to=100000, textvariable=page, width=10).grid(row=2, column=1, sticky="w", pady=10)
        ttk.Label(body, text="Tesseract를 이 컴퓨터에서 실행합니다. 외부 전송은 없습니다.\n결과는 오른쪽 비교용 사본에 들어갑니다. 정답을 넣어 정확도를 검증하세요.", wraplength=480).grid(row=3, column=0, columnspan=2, sticky="w", pady=(0, 12))
        def run():
            try:
                page_number = int(page.get())
                if page_number < 1 or not language.get().strip():
                    raise ValueError
            except ValueError:
                messagebox.showerror("실행 조건 확인", "1 이상의 쪽 번호와 언어 데이터를 입력하세요.", parent=dialog)
                return
            languages = language.get().strip()
            dialog.destroy()
            self._run_ocr(Path(path).resolve(), languages, page_number)
        ttk.Button(body, text="로컬 OCR 실행", command=run).grid(row=4, column=1, sticky="e")
        dialog.grab_set()

    def _run_ocr(self, source, languages, page_number):
        if self._busy or not self._confirm_discard(("hypothesis",), "새 로컬 OCR 결과로 바꾸기"):
            return
        def worker(stop, _progress):
            from .ocr import recognize_file
            return recognize_file(source, languages=languages, page_number=page_number, should_stop=stop.is_set)
        def done(result):
            self._protected_paths.add(source)
            self._ocr_record = result.to_dict()
            self._ocr_unsaved = True
            self._set_text("hypothesis", result.text, f"로컬 OCR · {source.name} · {page_number}쪽", saved=False)
            self.status_var.set("로컬 OCR 완료 · 원시 결과를 메모리에 보존했습니다. 정답 원문과 비교해 정확도를 확인하세요.")
        self._start(f"로컬 OCR 실행 중 · {source.name} · {page_number}쪽…", worker, done)

    def _start(self, message, worker: Callable, done: Callable, *, cancellable=True):
        if self._busy:
            return False
        self._busy = True
        self._job_cancellable = cancellable
        self._job_id += 1
        job_id = self._job_id
        stop = self._cancel = threading.Event()
        self._done_callback = done
        self.status_var.set(message)
        for control, _state in self._controls:
            control.configure(state="disabled")
        for widget in self.text_widgets.values():
            widget.configure(state="disabled")
        self.cancel_button.configure(state="normal" if cancellable else "disabled")
        self.progress.start(12)
        def run():
            try:
                result = worker(stop, lambda value: self._events.put((job_id, "progress", value)))
            except Exception as exc:
                self._events.put((job_id, "error", exc))
            else:
                self._events.put((job_id, "done", result))
        threading.Thread(target=run, name=f"AB-OCR-{job_id}", daemon=True).start()
        return True

    def cancel(self):
        if self._busy and str(self.cancel_button.cget("state")) != "disabled":
            self._cancel.set()
            self.cancel_button.configure(state="disabled")
            self.status_var.set("취소 요청됨 · 현재 계산이 끝나면 결과를 반영하지 않습니다.")

    def _finish(self):
        self._busy = False
        self.progress.stop()
        self.cancel_button.configure(state="disabled")
        for control, state in self._controls:
            control.configure(state=state)
        for widget in self.text_widgets.values():
            widget.configure(state="normal")
        self.export_button.configure(state="normal" if self._report is not None else "disabled")

    def _poll(self):
        if self._closed:
            return
        try:
            while True:
                job_id, kind, payload = self._events.get_nowait()
                if job_id != self._job_id or not self._busy:
                    continue
                if kind == "progress":
                    if not self._cancel.is_set():
                        self.status_var.set(str(payload))
                    continue
                cancelled = self._cancel.is_set()
                self._finish()
                if cancelled:
                    self.status_var.set("취소됨 · 현재 입력과 이전 평가 결과를 유지했습니다.")
                elif kind == "error":
                    self._close_after_write = False
                    from .evaluation import ComparisonLimitError
                    details = str(payload)
                    if isinstance(payload, ComparisonLimitError):
                        details = "정확한 글자 정렬의 계산 한도를 넘었습니다. 쪽이나 문단 단위로 나눠 비교하세요.\n추정 오류율은 표시하지 않습니다.\n\n" + details
                    self.status_var.set("작업 실패 · 입력을 보존했습니다. 오류 창의 안내를 확인한 뒤 다시 시도하세요.")
                    messagebox.showerror("AB_OCR 작업 오류", details, parent=self)
                else:
                    try:
                        self._done_callback(payload)
                    except Exception as exc:
                        self._close_after_write = False
                        self.status_var.set(f"결과 표시 실패 · {exc}")
                        messagebox.showerror("결과 표시 오류", str(exc), parent=self)
                if self._close_after_write:
                    self._close_after_write = False
                    self._close()
                    if self._closed:
                        return
        except queue.Empty:
            pass
        self._after_id = self.after(80, self._poll)

    def _close(self, *, force=False):
        if self._closed:
            return
        if not force and self._busy and not self._job_cancellable:
            self._close_after_write = True
            self.status_var.set("저장을 마친 뒤 창을 닫습니다. 파일 쓰기가 끝날 때까지 기다려 주세요.")
            return
        if not force and not self._confirm_discard(("reference", "hypothesis"), "창 닫기"):
            return
        self._closed = True
        self._cancel.set()
        self._job_id += 1
        if self._after_id is not None:
            self.after_cancel(self._after_id)
        self.destroy()


def main(argv=None):
    parser = argparse.ArgumentParser(description="AB_OCR 한글·한자 정확도 평가 작업대")
    parser.add_argument("--reference", type=Path, help="UTF-8 정답 TXT")
    parser.add_argument("--result", type=Path, help="UTF-8 OCR 결과 TXT")
    parser.add_argument("--example", action="store_true", help="합성 오류 예제")
    args = parser.parse_args(argv)
    app = EvaluationWindow()
    # Startup loading uses the same queue as interactive work; chain files so
    # the second load cannot be dropped by the single-job guard.
    pending = [(key, path) for key, path in (("reference", args.reference), ("hypothesis", args.result)) if path]
    def load_next():
        if app._busy:
            app.after(100, load_next)
        elif pending:
            key, path = pending.pop(0)
            app.load_text(key, path)
            app.after(100, load_next)
        elif args.example and not (args.reference or args.result):
            app.load_example()
    app.after(100, load_next)
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
