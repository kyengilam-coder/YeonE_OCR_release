"""Review small interlinear notes without changing a reading implicitly.

The service owns detection, model calls, fingerprints and writes. This window
keeps all those operations off Tk's thread and requires an explicit column and
caret position before requesting an insertion.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import gc
import queue
import threading
import tkinter as tk
from tkinter import messagebox, ttk


STATUS_LABELS = {
    "detected": "검출 후보", "read": "판독 후보", "uncertain": "원본 확인",
    "already_present": "기존 글에 있음", "partial_present": "일부가 이미 있음",
    "position_unverified": "기존 판독 위치 미확인",
    "applied": "삽입함", "failed": "판독 실패",
}


@dataclass(frozen=True)
class ColumnTarget:
    region_id: str
    paragraph_index: int
    column_index: int
    text: str
    label: str


def column_targets(record: dict | None) -> list[ColumnTarget]:
    """Retain saved list indices and exact Unicode for the service's guard."""
    targets = []
    for region in ((record or {}).get("merged") or {}).get("regions", []):
        direction = str(region.get("reading_direction") or region.get("dir") or "")
        horizontal = direction.startswith("가로") or (record or {}).get("mode") == "horizontal_zh"
        for pi, paragraph in enumerate(region.get("paragraphs") or []):
            for ci, column in enumerate(paragraph.get("columns") or []):
                text = column.get("text") or ""
                rid = str(region.get("id") or "")
                targets.append(ColumnTarget(rid, pi, ci, text,
                    f"{rid} · {pi + 1}문단 · {column.get('col_from_right', ci + 1)}{'행' if horizontal else '열'}"))
    return targets


def candidate_rows(bundle: dict) -> list[tuple[dict, dict]]:
    return [(page, candidate) for page in bundle.get("pages", [])
            for candidate in page.get("candidates", [])]


def subcolumn_texts(candidate: dict) -> tuple[str, str]:
    columns = candidate.get("subcolumns") or []
    by_side = {column.get("side"): column.get("text") or "" for column in columns}
    return by_side.get("right", ""), by_side.get("left", "")


def annotation_parts(candidate: dict) -> list[tuple[str, str, str]]:
    """Display source rows in reading order while retaining service side IDs."""
    columns = candidate.get("subcolumns") or []
    if candidate.get("direction") == "horizontal":
        parts = []
        for index, column in enumerate(columns):
            side = column.get("side") or ""
            number = side.removeprefix("line-")
            if side.startswith("line-") and number.isdecimal() and int(number) > 0:
                parts.append((int(number), side, f"{int(number)}행", column.get("text") or ""))
            else:
                parts.append((len(columns) + index + 1, side, "행 번호 확인 필요", column.get("text") or ""))
        return [(side, label, text) for _, side, label, text in sorted(parts)]
    return [(side, label, column.get("text") or "")
            for side, label in (("right", "오른쪽 소열"), ("left", "왼쪽 소열"))
            for column in columns if column.get("side") == side]


def insertion_text(candidate: dict, side: str = "both") -> str:
    """A partial note requires an explicit choice of its missing subcolumn."""
    status = candidate.get("status")
    if status == "partial_present":
        matching = [text for item_side, _, text in annotation_parts(candidate) if item_side == side]
        if side == "both" or not side or len(matching) != 1:
            raise ValueError("기존 글과 대조하여 누락된 소열·행을 선택하십시오.")
        text = matching[0]
    elif status in ("read", "uncertain") and side == "both":
        text = candidate.get("combined_text") or ""
    else:
        raise ValueError("삽입 가능한 재판독 후보를 선택하십시오.")
    if not text:
        raise ValueError("선택한 소열·행의 판독문이 없습니다. 먼저 재판독하십시오.")
    return text


def insertion_request(page: int, candidate: dict, target: ColumnTarget | None,
                      offset: int | None, side: str = "both") -> dict:
    """No inferred target, automatic start-of-column, or editable body copy."""
    if target is None or offset is None:
        raise ValueError("기존 글의 열·행을 고르고 삽입할 위치를 클릭하십시오.")
    if not isinstance(offset, int) or isinstance(offset, bool) or not 0 <= offset <= len(target.text):
        raise ValueError("삽입 위치가 현재 열의 글자 범위를 벗어났습니다.")
    insertion_text(candidate, side)
    candidate_id = candidate.get("id")
    if not candidate_id:
        raise ValueError("선택한 후보의 식별자가 없습니다. 다시 검출하십시오.")
    request = {"page": page, "candidate_id": candidate_id, "region_id": target.region_id,
            "paragraph_index": target.paragraph_index, "column_index": target.column_index,
            "offset": offset, "expected_text": target.text, "actor": "human"}
    if side != "both":
        request["side"] = side
    return request


def offset_from_prefix(text: str, prefix: str) -> int:
    """Python code-point offset; Tcl character counts differ for rare CJK."""
    if not text.startswith(prefix):
        raise ValueError("화면의 글과 저장된 글이 다릅니다. 다시 불러오십시오.")
    return len(prefix)


def safe_error(error: Exception, secrets=()) -> str:
    from .windows_app import redact_log
    return redact_log(f"{type(error).__name__}: {error}", secrets)[:600]


class AnnotationWorker:
    """Small queue bridge; the worker never accesses Tk variables/widgets."""
    def __init__(self):
        self.results = queue.Queue()
        self.stop = threading.Event()
        self.running = False
        self.thread = None

    def start(self, operation: str, task, *, secrets=()) -> None:
        if self.running:
            raise RuntimeError("진행 중인 작업을 마친 뒤 실행하십시오.")
        self.stop.clear()
        self.running = True

        def run():
            try:
                self.results.put((operation, task(self.stop.is_set), ""))
            except Exception as error:
                self.results.put((operation, None, safe_error(error, secrets)))
        self.thread = threading.Thread(target=run, daemon=True, name="small-annotation-review")
        self.thread.start()

    def take_result(self):
        result = self.results.get_nowait()
        self.running = False
        return result


class SmallAnnotationDialog(tk.Toplevel):
    def __init__(self, master, *, pdf: str, work_dir, pages="", settings=None, service=None):
        super().__init__(master)
        self.title("소주 검수 · 쌍행·다중행")
        self.transient(master)
        width = min(1280, max(800, self.winfo_screenwidth() - 80))
        height = min(850, max(600, self.winfo_screenheight() - 100))
        self.geometry(f"{width}x{height}")
        self.minsize(800, 600)
        self.pdf = str(Path(pdf).resolve())
        self.work_dir = Path(work_dir).resolve()
        self.settings = settings or (lambda: {})
        self._service = service
        self.jobs = AnnotationWorker()
        self.bundle = {"pages": []}
        self.rows = {}
        self.current = None
        self.targets = []
        self.target = None
        self.offset = None
        self._photo = self._source_image = None
        self._closing = False
        self._alive = True
        self._operation = ""
        self._poll_id = None
        self._applied_preview = None
        self.v_pages = tk.StringVar(value=pages or "")
        self.v_limit = tk.StringVar(value="12")
        self.v_status = tk.StringVar(value="저장된 소주 후보를 불러옵니다.")
        self.v_provider = tk.StringVar(value="재판독은 현재 설정의 판독기와 모델을 사용합니다.")
        self.v_target = tk.StringVar()
        self.v_position = tk.StringVar(value="기존 글에서 삽입 위치를 클릭하십시오.")
        self.v_candidate = tk.StringVar(value="왼쪽 목록에서 소주 후보를 선택하십시오.")
        self.v_native = tk.BooleanVar(value=False)
        self.v_side = tk.StringVar()
        self.v_direction = tk.StringVar(value="자동")
        self.v_order = tk.StringVar(value="세로 · 오른쪽 소열 → 왼쪽 소열")
        self.side_choices = {}
        self._build()
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.grab_set()  # Keep the parent's model operations out of this call-cap scope.
        self._poll_id = self.after(80, self._poll)
        self._start("load", lambda stop: self._get_service().load_bundle(self.work_dir))

    def _get_service(self):
        if self._service is None:
            from . import small_annotations
            self._service = small_annotations
        return self._service

    @staticmethod
    def _text(parent, *, height=6):
        widget = tk.Text(parent, wrap="word", height=height, font=("맑은 고딕", 11),
                         relief="solid", borderwidth=1, padx=7, pady=6, exportselection=False)
        widget.configure(state="disabled")
        return widget

    @staticmethod
    def _set_text(widget, text):
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("1.0", text)
        widget.configure(state="disabled")

    def _build(self):
        top = ttk.Frame(self, padding=(12, 10))
        top.pack(fill="x")
        ttk.Label(top, text=f"소주 검수 · {Path(self.pdf).name}", font=("맑은 고딕", 13, "bold")).pack(anchor="w")
        ttk.Label(top, text="세로 쌍행·가로 다중행 소주를 원본과 대조한 뒤 본문에 넣을 위치를 지정합니다.").pack(anchor="w", pady=(3, 7))
        toolbar = ttk.Frame(top)
        toolbar.pack(fill="x")
        ttk.Label(toolbar, text="PDF 쪽").pack(side="left")
        self.pages_entry = ttk.Entry(toolbar, textvariable=self.v_pages, width=13)
        self.pages_entry.pack(side="left", padx=(5, 8))
        ttk.Label(toolbar, text="방향").pack(side="left")
        self.direction_combo = ttk.Combobox(toolbar, textvariable=self.v_direction,
            values=("자동", "세로", "가로"), width=5, state="readonly")
        self.direction_combo.pack(side="left", padx=(5, 8))
        self.scan_button = ttk.Button(toolbar, text="소주 검출", command=self._scan)
        self.scan_button.pack(side="left", padx=3)
        self.read_button = ttk.Button(toolbar, text="선택 후보 재판독", command=self._read)
        self.read_button.pack(side="left", padx=(3, 12))
        ttk.Label(toolbar, text="호출 상한").pack(side="left")
        self.limit_entry = ttk.Spinbox(toolbar, from_=1, to=100, width=5, textvariable=self.v_limit)
        self.limit_entry.pack(side="left", padx=5)
        ttk.Label(toolbar, text="회 · 재시도 포함").pack(side="left")
        self.stop_button = ttk.Button(toolbar, text="중지", command=self._stop, state="disabled")
        self.stop_button.pack(side="right")
        ttk.Label(top, textvariable=self.v_provider).pack(anchor="w", pady=(5, 0))

        panes = ttk.Panedwindow(self, orient="horizontal")
        panes.pack(fill="both", expand=True, padx=12)
        listing = ttk.Frame(panes)
        panes.add(listing, weight=1)
        ttk.Label(listing, text="소주 후보", font=("맑은 고딕", 11, "bold")).pack(anchor="w", pady=(0, 5))
        self.tree = ttk.Treeview(listing, columns=("page", "status"), show="tree headings", selectmode="browse", height=20)
        self.tree.heading("#0", text="후보")
        self.tree.heading("page", text="쪽")
        self.tree.heading("status", text="상태")
        self.tree.column("#0", width=95, minwidth=60)
        self.tree.column("page", width=45, stretch=False)
        self.tree.column("status", width=90)
        scroll = ttk.Scrollbar(listing, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.tree.pack(fill="both", expand=True)
        self.tree.bind("<<TreeviewSelect>>", self._select_candidate)

        source = ttk.Frame(panes, padding=(10, 0))
        panes.add(source, weight=2)
        ttk.Label(source, text="원본 조각", font=("맑은 고딕", 11, "bold")).pack(anchor="w")
        picture_tools = ttk.Frame(source)
        picture_tools.pack(fill="x")
        ttk.Checkbutton(picture_tools, text="원본 픽셀 크기로 보기", variable=self.v_native, command=self._draw_crop).pack(side="left")
        self.context_button = ttk.Button(picture_tools, text="쪽 안 위치", command=self._context, state="disabled")
        self.context_button.pack(side="right")
        picture = ttk.Frame(source)
        picture.pack(fill="both", expand=True, pady=5)
        picture.rowconfigure(0, weight=1)
        picture.columnconfigure(0, weight=1)
        self.canvas = tk.Canvas(picture, background="#eeeae1", highlightthickness=0)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        sx = ttk.Scrollbar(picture, orient="horizontal", command=self.canvas.xview)
        sy = ttk.Scrollbar(picture, orient="vertical", command=self.canvas.yview)
        sx.grid(row=1, column=0, sticky="ew")
        sy.grid(row=0, column=1, sticky="ns")
        self.canvas.configure(xscrollcommand=sx.set, yscrollcommand=sy.set)
        self.canvas.bind("<Configure>", lambda event: self._draw_crop())
        ttk.Label(source, textvariable=self.v_candidate, wraplength=370).pack(anchor="w", pady=5)
        ttk.Label(source, textvariable=self.v_order).pack(anchor="w")
        small = self.vertical_frame = ttk.Frame(source)
        small.pack(fill="x", pady=5)
        # Physical placement mirrors the source: right subcolumn is on the right.
        left = ttk.LabelFrame(small, text="② 왼쪽 소열", padding=3)
        right = ttk.LabelFrame(small, text="① 오른쪽 소열", padding=3)
        left.pack(side="left", fill="both", expand=True, padx=(0, 3))
        right.pack(side="left", fill="both", expand=True)
        self.left_text = self._text(left, height=5)
        self.right_text = self._text(right, height=5)
        self.left_text.configure(width=14)
        self.right_text.configure(width=14)
        self.left_text.pack(fill="both", expand=True)
        self.right_text.pack(fill="both", expand=True)
        self.horizontal_frame = ttk.Frame(source)
        self.horizontal_text = self._text(self.horizontal_frame, height=10)
        self.horizontal_text.configure(width=32)
        row_scroll = ttk.Scrollbar(self.horizontal_frame, orient="vertical", command=self.horizontal_text.yview)
        self.horizontal_text.configure(yscrollcommand=row_scroll.set)
        row_scroll.pack(side="right", fill="y")
        self.horizontal_text.pack(fill="both", expand=True)

        target = ttk.Frame(panes, padding=(10, 0, 0, 0))
        panes.add(target, weight=3)
        ttk.Label(target, text="기존 판독문과 삽입 위치", font=("맑은 고딕", 11, "bold")).pack(anchor="w")
        self.target_combo = ttk.Combobox(target, textvariable=self.v_target, state="readonly")
        self.target_combo.pack(fill="x", pady=7)
        self.target_combo.bind("<<ComboboxSelected>>", self._select_column)
        ttk.Label(target, text="열·행을 선택하고 글자 사이를 클릭하십시오. 본문은 읽기 전용입니다.", wraplength=430).pack(anchor="w")
        self.original = self._text(target, height=10)
        self.original.pack(fill="both", expand=True, pady=6)
        self.original.tag_configure("caret", background="#ffe294")
        self.original.bind("<ButtonRelease-1>", self._choose_caret)
        self.original.bind("<KeyRelease>", self._move_caret)
        ttk.Label(target, textvariable=self.v_position, wraplength=430).pack(anchor="w", pady=(0, 7))
        self.side_frame = ttk.Frame(target)
        self.side_label = ttk.Label(self.side_frame, text="삽입할 소열")
        self.side_label.pack(side="left", padx=(0, 6))
        self.side_combo = ttk.Combobox(self.side_frame, textvariable=self.v_side,
                                      values=("오른쪽 소열", "왼쪽 소열"), state="readonly", width=13)
        self.side_combo.pack(side="left")
        self.side_combo.bind("<<ComboboxSelected>>", self._select_side)
        self.preview_label = ttk.Label(target, text="삽입 미리보기 · ⟦ ⟧ 안이 추가할 글")
        self.preview_label.pack(anchor="w")
        self.preview = self._text(target, height=6)
        self.preview.pack(fill="x", pady=6)
        self.apply_button = ttk.Button(target, text="선택 위치에 소주 삽입", command=self._apply, state="disabled")
        self.apply_button.pack(anchor="e", pady=(3, 8))
        footer = ttk.Frame(self, padding=12)
        footer.pack(fill="x")
        ttk.Label(footer, textvariable=self.v_status, wraplength=1050).pack(side="left", fill="x", expand=True)
        ttk.Button(footer, text="닫기", command=self._close).pack(side="right", padx=(10, 0))

    def _set_busy(self, busy):
        state = "disabled" if busy else "normal"
        self.scan_button.configure(state=state)
        self.pages_entry.configure(state=state)
        self.direction_combo.configure(state="disabled" if busy else "readonly")
        self.limit_entry.configure(state=state)
        self.stop_button.configure(state="normal" if busy and self._operation in ("scan", "read") else "disabled")
        self.target_combo.configure(state="disabled" if busy else "readonly")
        self.side_combo.configure(state="disabled" if busy else "readonly")
        self._refresh_actions()

    def _refresh_actions(self):
        stale = bool(self.current and self.current[0].get("stale"))
        active = bool(self.current) and not self.jobs.running and not stale
        self.read_button.configure(state="normal" if active else "disabled")
        self.context_button.configure(state="normal" if self.current and not self.jobs.running else "disabled")
        try:
            page, candidate = self.current or ({}, {})
            insertion_request(page.get("pdf_page", 0), candidate, self.target, self.offset, self._insertion_side())
            can_apply = not self.jobs.running and not stale
        except ValueError:
            can_apply = False
        self.apply_button.configure(state="normal" if can_apply else "disabled")

    def _start(self, operation, task, *, secrets=()):
        self._operation = operation
        # Finalize old widget cycles on Tk's thread before allocations in the
        # worker can otherwise collect a previous window's StringVars there.
        gc.collect()
        self.jobs.start(operation, task, secrets=secrets)
        self._set_busy(True)

    def _scan(self):
        pages = self.v_pages.get().strip() or None
        horizontal = {"자동": None, "세로": False, "가로": True}[self.v_direction.get()]
        self.v_status.set("원본에서 작은 소주를 찾는 중…")
        self._start("scan", lambda stop: self._get_service().scan_work(
            self.pdf, self.work_dir, pages=pages, dpi=300, horizontal=horizontal, should_stop=stop))

    def _read(self):
        if not self.current:
            return
        try:
            cap = int(self.v_limit.get())
            if not 1 <= cap <= 100:
                raise ValueError("호출 상한은 1~100회로 입력하십시오.")
            cfg = dict(self.settings())  # Snapshot Tk variables before starting the worker.
            provider, model = cfg.get("provider", "gemini"), cfg.get("model")
            if not model:
                raise ValueError("설정에서 판독 모델을 선택하십시오.")
        except (ValueError, TypeError) as error:
            messagebox.showerror("쌍행소주 재판독", safe_error(error), parent=self)
            return
        page, candidate = self.current
        page_number, candidate_id = page["pdf_page"], candidate["id"]
        key = cfg.get("api_key") or None
        self.v_provider.set(f"{provider} · {model} · 이번 실행 최대 {cap}회")
        self.v_status.set(f"PDF {page_number}쪽의 선택한 소주를 재판독하는 중…")
        self._start("read", lambda stop: self._get_service().reread_work(
            self.pdf, self.work_dir, pages=[page_number], candidate_ids=[candidate_id],
            provider=provider, model=model, api_key=key, max_calls=cap, should_stop=stop),
            secrets=(key,) if key else ())

    def _apply(self):
        if not self.current:
            return
        try:
            page, candidate = self.current
            request = insertion_request(page["pdf_page"], candidate, self.target, self.offset, self._insertion_side())
        except ValueError as error:
            messagebox.showerror("소주 삽입", str(error), parent=self)
            return
        self.v_status.set("지정한 위치와 현재 저장된 글을 확인하여 소주를 삽입하는 중…")

        def apply(stop):
            result = self._get_service().apply_candidate(self.pdf, self.work_dir, **request)
            return {"bundle": self._get_service().load_bundle(self.work_dir), "application": result,
                    "candidate": (page["pdf_page"], candidate["id"])}
        self._start("apply", apply)

    def _context(self):
        if not self.current:
            return
        page, candidate = self.current
        number, dpi = page["pdf_page"], page.get("dpi", 300)
        box = list(candidate.get("bbox_px") or [])
        self.v_status.set("원본 쪽에서 소주 위치를 불러오는 중…")

        def load(stop):
            from .pdfio import open_pdf, page_image
            with open_pdf(self.pdf) as document:
                image, _ = page_image(document, number, dpi=dpi)
            return {"image": image, "page": number, "box": box}
        self._start("context", load)

    def _show_context(self, value):
        from PIL import Image, ImageTk
        image = value["image"]
        original_width, original_height = image.size
        image.thumbnail((min(850, self.winfo_screenwidth() - 100),
                         min(760, self.winfo_screenheight() - 160)), Image.Resampling.LANCZOS)
        window = tk.Toplevel(self)
        window.title(f"PDF {value['page']}쪽 · 소주 위치")
        window.transient(self)
        ttk.Label(window, text="빨간 상자가 선택한 소주 후보입니다.", padding=8).pack(anchor="w")
        canvas = tk.Canvas(window, width=image.width, height=image.height, highlightthickness=0)
        canvas.pack(padx=8)
        window._annotation_photo = ImageTk.PhotoImage(image, master=window)
        canvas.create_image(0, 0, anchor="nw", image=window._annotation_photo)
        if len(value["box"]) == 4:
            sx, sy = image.width / original_width, image.height / original_height
            x0, y0, x1, y1 = value["box"]
            canvas.create_rectangle(x0 * sx, y0 * sy, x1 * sx, y1 * sy, outline="#d22323", width=3)
        image.close()

        def close():
            window.grab_release()
            window.destroy()
            if self._alive:
                self.grab_set()
        ttk.Button(window, text="닫기", command=close).pack(pady=8)
        window.protocol("WM_DELETE_WINDOW", close)
        window.grab_set()

    def _stop(self):
        self.jobs.stop.set()
        self.stop_button.configure(state="disabled")
        self.v_status.set("중지 요청을 보냈습니다. 진행 중인 호출을 마치면 멈춥니다.")

    def _poll(self):
        self._poll_id = None
        if not self._alive:
            return
        try:
            operation, value, error = self.jobs.take_result()
        except queue.Empty:
            pass
        else:
            if self._closing:
                if operation == "context" and value:
                    value["image"].close()
                self._destroy()
                return
            self._set_busy(False)
            if error:
                self.v_status.set("작업을 마치지 못했습니다. 저장된 후보를 확인한 뒤 다시 실행할 수 있습니다.")
                messagebox.showerror("쌍행소주 검수", error, parent=self)
            elif operation == "context":
                self._show_context(value)
                self.v_status.set("원본 쪽에서 소주의 위치와 본문 연결을 확인하십시오.")
            else:
                if operation == "apply":
                    self._applied_preview = (*value["candidate"], value["application"].get("inserted_text", ""))
                    value = value["bundle"]
                else:
                    self._applied_preview = None
                self._show_bundle(value)
                label = {"load": "저장된 후보", "scan": "검출 결과", "read": "재판독 결과", "apply": "삽입 결과"}[operation]
                note = " · 문서에 반영하려면 [문서 만들기]를 다시 실행하십시오." if operation == "apply" else ""
                self.v_status.set(f"{label} · {len(self.rows)}개 후보" + (" · 중지됨" if self.jobs.stop.is_set() else "") + note)
        self._poll_id = self.after(80, self._poll)

    def _show_bundle(self, bundle):
        self.bundle = bundle or {"pages": []}
        previous = ((self.current[0].get("pdf_page"), self.current[1].get("id")) if self.current else None)
        self.current = None
        self.tree.delete(*self.tree.get_children())
        self.rows = {}
        selected = None
        for index, (page, candidate) in enumerate(candidate_rows(self.bundle)):
            iid = str(index)
            self.rows[iid] = (page, candidate)
            cid = str(candidate.get("id") or index + 1)
            status = STATUS_LABELS.get(candidate.get("status"), "확인 필요")
            self.tree.insert("", "end", iid=iid, text=cid,
                             values=(page.get("pdf_page", ""), status))
            if (page.get("pdf_page"), candidate.get("id")) == previous:
                selected = iid
        if selected is not None:
            self.tree.selection_set(selected)
            self.tree.see(selected)
        else:
            self._clear_candidate()
        self._refresh_actions()

    def _clear_candidate(self):
        self.current = self.target = self.offset = None
        self.targets = []
        self.v_target.set("")
        self.v_side.set("")
        self.side_choices = {}
        self.side_combo.configure(values=())
        self.side_frame.pack_forget()
        self.horizontal_frame.pack_forget()
        self.vertical_frame.pack(fill="x", pady=5)
        self.v_order.set("세로 · 오른쪽 소열 → 왼쪽 소열")
        self.target_combo.configure(values=())
        self.v_position.set("기존 글에서 삽입 위치를 클릭하십시오.")
        for widget in (self.original, self.preview, self.left_text, self.right_text, self.horizontal_text):
            self._set_text(widget, "")
        if self._source_image is not None:
            self._source_image.close()
        self._source_image = self._photo = None
        self.canvas.delete("all")

    def _select_candidate(self, event=None):
        selected = self.tree.selection()
        if not selected or selected[0] not in self.rows:
            return
        current = self.rows[selected[0]]
        self._clear_candidate()
        self.current = current
        page, candidate = current
        self.targets = column_targets(page.get("record"))
        self.target_combo.configure(values=[target.label for target in self.targets])
        if not self.targets:
            self._set_text(self.original, "이 쪽의 기존 판독문이 없습니다. 검출·재판독 결과를 먼저 검수할 수 있습니다.")
        right, left = subcolumn_texts(candidate)
        self._set_text(self.right_text, right)
        self._set_text(self.left_text, left)
        parts = annotation_parts(candidate)
        horizontal = candidate.get("direction") == "horizontal"
        if horizontal:
            self.vertical_frame.pack_forget()
            self.horizontal_frame.pack(fill="x", pady=5)
            self.v_order.set(f"가로 · {len(parts)}행 · 위 → 아래 (각 행은 왼쪽 → 오른쪽)")
            self._set_text(self.horizontal_text, "\n\n".join(
                f"{part_label} · {len(text)}자\n{text or '(미판독)'}" for _, part_label, text in parts))
        label = STATUS_LABELS.get(candidate.get("status"), "확인 필요")
        shape = f"가로 {len(parts)}행" if horizontal else f"세로 · 우소열 {len(right)}자 / 좌소열 {len(left)}자"
        self.v_candidate.set(f"PDF {page.get('pdf_page')}쪽 · {label} · {shape}\n"
                             "누락 여부와 읽기 순서를 원본에서 확인하십시오.")
        if candidate.get("status") == "partial_present":
            noun = "행" if horizontal else "소열"
            self.side_label.configure(text=f"삽입할 {noun}")
            self.side_choices = {part_label: side for side, part_label, _ in parts}
            self.side_combo.configure(values=tuple(self.side_choices))
            self.v_candidate.set(self.v_candidate.get() + f"\n일부 소주가 이미 있습니다. 원본과 기존 글을 대조하고 누락된 {noun}만 선택하십시오."
                + (" 한 행만 누락되고 나머지 행이 모두 본문에 있을 때 삽입할 수 있습니다. 여러 행이 누락된 경우에는 기존 검수 도구에서 직접 교정하십시오." if horizontal else ""))
            self.side_frame.pack(fill="x", before=self.preview_label, pady=(0, 7))
        if self._applied_preview and self._applied_preview[:2] == (page.get("pdf_page"), candidate.get("id")):
            self._set_text(self.preview, "삽입한 글: ⟦" + self._applied_preview[2] + "⟧")
        if page.get("stale"):
            self.v_candidate.set(self.v_candidate.get() + "\n판독·교정 기록이 변경되었습니다. 먼저 [소주 검출]을 다시 실행하십시오.")
        if candidate.get("status") == "position_unverified":
            self.v_candidate.set(self.v_candidate.get() + "\n같은 문구의 기존 판독 위치를 특정할 수 없습니다. 원본과 대조하여 기존 검수 도구에서 직접 교정하십시오.")
        errors = [side.get("last_error") for side in candidate.get("subcolumns", []) if side.get("last_error")]
        if errors:
            self.v_candidate.set(self.v_candidate.get() + "\n최근 재판독 실패 · 저장된 이전 답을 함께 확인하십시오.")
        crop = candidate.get("crop_path")
        if crop:
            try:
                from PIL import Image
                path = Path(crop)
                if not path.is_absolute():
                    path = self.work_dir / path
                with Image.open(path) as image:
                    self._source_image = image.convert("RGB")
                self._draw_crop()
            except (OSError, ValueError) as error:
                self.canvas.create_text(10, 10, anchor="nw", text="원본 조각을 열지 못했습니다. 다시 검출하십시오.", width=300)
                self.v_status.set(safe_error(error))
        self._refresh_actions()

    def _draw_crop(self):
        if not self._alive or self._source_image is None:
            return
        from PIL import Image, ImageTk
        picture = self._source_image.copy()
        if not self.v_native.get():
            picture.thumbnail((max(80, self.canvas.winfo_width() - 8),
                               max(80, self.canvas.winfo_height() - 8)), Image.Resampling.LANCZOS)
        self._photo = ImageTk.PhotoImage(picture, master=self)
        self.canvas.delete("all")
        self.canvas.create_image(4, 4, anchor="nw", image=self._photo)
        self.canvas.configure(scrollregion=(0, 0, picture.width + 8, picture.height + 8))
        picture.close()

    def _select_column(self, event=None):
        index = self.target_combo.current()
        self.target = self.targets[index] if 0 <= index < len(self.targets) else None
        self.offset = None
        self._set_text(self.original, self.target.text if self.target else "")
        self._set_text(self.preview, "")
        self.v_position.set("삽입할 글자 사이를 클릭하십시오.")
        self._refresh_actions()

    def _choose_caret(self, event):
        if self.jobs.running or self.target is None:
            return
        self.original.mark_set("insert", self.original.index(f"@{event.x},{event.y}"))
        self._capture_caret()

    def _move_caret(self, event):
        if event.keysym in ("Left", "Right", "Up", "Down", "Home", "End") and self.offset is not None:
            self._capture_caret()

    def _capture_caret(self):
        if self.jobs.running or self.target is None or self.current is None:
            return
        prefix = self.original.get("1.0", "insert")
        try:
            self.offset = offset_from_prefix(self.target.text, prefix)
        except ValueError:
            self.offset = None
            self._refresh_actions()
            return
        self.original.tag_remove("caret", "1.0", "end")
        self.original.tag_add("caret", "insert", "insert + 1 chars")
        offset = self.offset
        before, after = self.target.text[:offset], self.target.text[offset:]
        self.v_position.set(f"{offset}자 뒤 · {before[-12:]}│{after[:12]}")
        self._refresh_preview()
        self._refresh_actions()

    def _insertion_side(self):
        if self.current and self.current[1].get("status") == "partial_present":
            return self.side_choices.get(self.v_side.get(), "")
        return "both"

    def _select_side(self, event=None):
        self._refresh_preview()
        self._refresh_actions()

    def _refresh_preview(self):
        if self.target is None or self.offset is None or self.current is None:
            self._set_text(self.preview, "")
            return
        try:
            addition = insertion_text(self.current[1], self._insertion_side())
        except ValueError as error:
            self._set_text(self.preview, str(error))
            return
        before, after = self.target.text[:self.offset], self.target.text[self.offset:]
        self._set_text(self.preview, before[-60:] + "⟦" + addition + "⟧" + after[:60])

    def _close(self):
        if self.jobs.running:
            self._closing = True
            if self._operation in ("scan", "read"):
                self._stop()
                self.v_status.set("중지 후 창을 닫습니다. 완료된 결과는 보관됩니다.")
            else:
                self.v_status.set("저장 중인 결과를 마친 뒤 창을 닫습니다.")
            return
        self._destroy()

    def _destroy(self):
        self._alive = False
        if self._poll_id is not None:
            self.after_cancel(self._poll_id)
            self._poll_id = None
        if self._source_image is not None:
            self._source_image.close()
            self._source_image = None
        self._photo = None
        self.grab_release()
        self.destroy()
