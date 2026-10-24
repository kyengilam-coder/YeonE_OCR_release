"""Local proposals and image-grounded region readings in the comparison editor."""
from __future__ import annotations

import json
import tkinter as tk
from tkinter import ttk

from ab_ocr.engine_registry import ENGINES, ENGINE_LABELS


class RefinementPanel(ttk.Frame):
    def __init__(self, editor, parent):
        super().__init__(parent)
        self.editor = editor
        self.items = {}
        self.columnconfigure(0, weight=1)
        self.rowconfigure(2, weight=1)
        toolbar = ttk.Frame(self)
        toolbar.grid(row=0, column=0, sticky="ew")
        self.generate_button = ttk.Button(toolbar, text="교정 후보 생성", command=self.generate)
        self.generate_button.pack(side="left")
        self.image_button = ttk.Button(toolbar, text="이미지 OCR 근거 재검사", command=self.check_image)
        self.image_button.pack(side="left", padx=3)
        self.source_button = ttk.Button(toolbar, text="PDF 부호 확인", command=self.check_pdf)
        self.source_button.pack(side="left", padx=3)
        self.accept_button = ttk.Button(toolbar, text="채택", command=lambda: self.decide(True))
        self.accept_button.pack(side="left", padx=3)
        self.reject_button = ttk.Button(toolbar, text="거절", command=lambda: self.decide(False))
        self.reject_button.pack(side="left")
        self.details_button = ttk.Button(toolbar, text="재판독 기록", command=self.details)
        self.details_button.pack(side="right")
        regions = ttk.Frame(self)
        regions.grid(row=1, column=0, sticky="ew", pady=3)
        self.engine = tk.StringVar(self, "glmocr")
        self.engine_box = ttk.Combobox(regions, textvariable=self.engine, values=ENGINES,
                                      state="readonly", width=14)
        self.engine_box.pack(side="left")
        self.region_button = ttk.Button(regions, text="선택 영역 재판독", command=self.reread)
        self.region_button.pack(side="left", padx=3)
        self.difficult_button = ttk.Button(regions, text="어려운 곳 2개", command=lambda: self.reread(difficult=True))
        self.difficult_button.pack(side="left")
        self.count = tk.StringVar(self, "후보 0곳 · 원문은 채택할 때만 변경")
        ttk.Label(regions, textvariable=self.count, width=1).pack(side="left", fill="x", expand=True)
        self.tree = ttk.Treeview(self, columns=("stage", "before", "after", "state"),
                                 show="headings", height=4, selectmode="browse")
        for name, label, width in (("stage", "근거", 65), ("before", "변경 전", 100),
                                   ("after", "교정 후보", 160), ("state", "상태", 65)):
            self.tree.heading(name, text=label)
            self.tree.column(name, width=width, minwidth=40, stretch=name == "after")
        self.tree.grid(row=2, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(self, command=self.tree.yview)
        scroll.grid(row=2, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.bind("<<TreeviewSelect>>", self.selected)
        self.tree.bind("<Double-1>", self.details)

    def refresh(self):
        from .comparison_refinement import proposals
        selected = self.tree.selection()
        self.tree.delete(*self.tree.get_children())
        self.items = {}
        self.editor._selected_refinement = None
        if self.editor.project:
            for item in proposals(self.editor.project, self.editor.page_number, include_decided=True):
                key = item["review_id"] + ":" + item["id"]
                self.items[key] = item
                compact = lambda value: value.replace("\n", "↵")[:100] or "∅"
                source_hold = item.get("source_check", {}).get("blocked", False)
                image_hold = item.get("image_only_check", item.get("image_only_current", {})).get("blocked", False)
                state = ("처리됨" if not item["pending"] else "이미지 근거 보류" if image_hold else "PDF 근거 보류" if source_hold
                         else "대기" if item["eligible"] else "확인 필요")
                self.tree.insert("", "end", iid=key, values=("조합" if item["stage"] == "local" else "재판독",
                    compact(item["before"]), compact(item["after"]), state))
        pending = sum(item["eligible"] for item in self.items.values())
        held = sum(item["pending"] and item.get("source_check", {}).get("blocked", False)
                   for item in self.items.values())
        image_held = sum(item["pending"] and item.get("image_only_check", item.get("image_only_current", {})).get("blocked", False)
                         for item in self.items.values())
        self.count.set(f"대기 {pending}곳 · 이미지 보류 {image_held}곳 · PDF 보류 {held}곳 · 자동 확정 없음")
        if selected and selected[0] in self.items:
            self.tree.selection_set(selected[0])
            self.selected()
        self.set_enabled()

    def set_enabled(self):
        active = self.editor.project is not None and not self.editor.busy
        item = self.current() or {}
        for button in (self.generate_button, self.region_button, self.difficult_button, self.image_button):
            button.configure(state="normal" if active else "disabled")
        self.engine_box.configure(state="readonly" if active else "disabled")
        pdf = active and self.editor.project.source_path.suffix.lower() == ".pdf"
        self.source_button.configure(state="normal" if pdf else "disabled")
        self.accept_button.configure(state="normal" if active and item.get("eligible") else "disabled")
        self.reject_button.configure(state="normal" if active and item.get("pending") else "disabled")
        self.details_button.configure(state="normal" if self.editor.project else "disabled")

    def current(self):
        selection = self.tree.selection()
        return self.items.get(selection[0]) if selection else None

    def selected(self, _event=None):
        item = self.current()
        self.editor._selected_refinement = item
        if item:
            self.editor._selected_suggestion = None
            self.editor._selected_ai_proposal = None
            self.editor.status.set(item.get("blocked_reason") or
                "실제 OCR 영역과 양쪽 원문이 확인된 후보입니다. 원본을 확인한 뒤 채택하세요.")
            if item.get("eligible"):
                self.editor.text.tag_remove("selection-evidence", "1.0", "end")
                a, b = item["current_start"], item["current_end"]
                self.editor.text.tag_add("selection-evidence", f"1.0+{a}c", f"1.0+{b}c")
                self.editor.text.see(f"1.0+{a}c")
        self.editor._highlight_image()
        self.editor._set_enabled()

    @staticmethod
    def checkpoint(project, recovery):
        if recovery:
            project.save_recovery(recovery)
        if project.project_path:
            project.save()

    def generate(self):
        editor = self.editor
        if editor.busy or editor.project is None:
            return
        editor._commit_text()
        project, number, recovery = editor.project, editor.page_number, editor._recovery_path
        def worker(stop, _generation, _job):
            from .comparison_refinement import RefinementPolicy, build_candidates, build_region_candidates
            from .comparison_image_only import record_checks
            local = build_candidates(project, number, policy=RefinementPolicy(version="anchored-refinement-v3"), use_pdf_text=False)
            if not stop.is_set():
                local += build_candidates(project, number, policy=RefinementPolicy(version="anchored-refinement-v4"), use_pdf_text=False)
            regional = [] if stop.is_set() else build_region_candidates(project, number)
            if not stop.is_set():
                record_checks(project, number)
            self.checkpoint(project, recovery)
            return "refined", {"local": len(local), "regional": len(regional)}
        editor._start_job("원문 위치와 실제 OCR 좌표를 확인하여 교정 후보를 생성합니다.", worker)

    def check_image(self):
        editor = self.editor
        if editor.busy or editor.project is None:
            return
        editor._commit_text()
        project, number, recovery = editor.project, editor.page_number, editor._recovery_path
        def worker(_stop, _generation, _job):
            from .comparison_image_only import record_checks
            record = record_checks(project, number)
            self.checkpoint(project, recovery)
            return "image-checked", {"checked": len(record["verdicts"]) if record else 0,
                "held": sum(v["blocked"] for v in record["verdicts"]) if record else 0}
        editor._start_job("이미지 OCR 원시 판독으로 한자 보호와 부호 반대 근거를 재검사합니다.", worker)

    def check_pdf(self):
        editor = self.editor
        if editor.busy or editor.project is None or editor.project.source_path.suffix.lower() != ".pdf":
            return
        editor._commit_text()
        project, number, recovery = editor.project, editor.page_number, editor._recovery_path
        def worker(_stop, _generation, _job):
            from .comparison_refinement_source import record_pdf_check
            record = record_pdf_check(project, number)
            self.checkpoint(project, recovery)
            return "source-checked", {"checked": len(record["verdicts"]) if record else 0,
                "held": sum(v["blocked"] for v in record["verdicts"]) if record else 0,
                "unavailable": sum(v["status"] == "unavailable" for v in record["verdicts"]) if record else 0}
        editor._start_job("원본 PDF의 표시 문자와 OCR 영역을 대조해 부호 충돌을 확인합니다.", worker)

    def _selection(self):
        editor = self.editor
        try:
            a = len(editor.text.get("1.0", "sel.first"))
            b = len(editor.text.get("1.0", "sel.last"))
            return a, b
        except tk.TclError:
            pass
        item = self.current()
        if item and item.get("eligible"):
            return item["current_start"], item["current_end"]
        suggestion = editor._selected_suggestion
        if suggestion:
            from .comparison_editor import mapped_span
            baseline = editor.project._baseline(editor.project.page(editor.page_number))
            if baseline:
                span = mapped_span(baseline["raw"]["text"], editor.project.text(editor.page_number),
                                   suggestion["start"], suggestion["end"])
                if span:
                    return span
        raise ValueError("교정문에서 짧은 영역을 선택하거나 교정 후보를 선택하세요.")

    def reread(self, *, difficult=False):
        editor = self.editor
        if editor.busy or editor.project is None:
            return
        editor._commit_text()
        try:
            selection = None if difficult else self._selection()
            engine = self.engine.get()
            if engine not in ENGINES:
                raise ValueError("재판독 엔진을 선택하세요.")
        except ValueError as exc:
            editor.status.set(str(exc))
            return
        project, number, recovery = editor.project, editor.page_number, editor._recovery_path
        engine_root, events = editor.engine_root, editor._events
        def worker(stop, generation, job):
            from ab_ocr.local_engines import RecognitionSession
            from ab_ocr.engine_registry import language_for
            from .comparison import _run_profile
            from .comparison_refinement import plan_region, run_region, build_region_candidates, region_targets
            targets = region_targets(project, number, limit=2) if difficult else [
                {"start": selection[0], "end": selection[1]}]
            if not targets:
                return "region-read", "확인 가능한 어려운 영역 없음"
            statuses = []
            baseline = project._baseline(project.page(number))
            profile, direction = _run_profile(baseline)
            with RecognitionSession(engine, language_for(engine, profile, direction), engine_root=engine_root,
                    should_stop=stop.is_set,
                    on_wait=lambda value: events.put(("gpu-wait", generation, job, value))) as session:
                conditions = session.condition_snapshot()
                for target in targets:
                    if stop.is_set():
                        break
                    plan = plan_region(project, number, target["start"], target["end"], engine,
                                       engine_root=engine_root, should_stop=stop.is_set, conditions=conditions)
                    record = run_region(project, plan, session=session, should_stop=stop.is_set)
                    if not stop.is_set() and record["status"] == "completed":
                        build_region_candidates(project, number, region_ids=[record["id"]])
                    from .comparison_image_only import record_checks
                    record_checks(project, number)
                    self.checkpoint(project, recovery)
                    statuses.append(record["status"])
            return "region-read", ", ".join(statuses) or "cancelled"
        editor._start_job(f"{ENGINE_LABELS[engine]}로 실제 이미지 영역을 다시 읽습니다.", worker)

    def decide(self, accept):
        editor, item = self.editor, self.current()
        if editor.busy or editor.project is None or not item:
            return
        try:
            editor._commit_text()
            from .comparison_refinement import accept_proposal, reject_proposal
            function = accept_proposal if accept else reject_proposal
            function(editor.project, editor.page_number, item["review_id"], item["id"])
            self.checkpoint(editor.project, editor._recovery_path)
            editor._populate_page()
            editor._title()
            editor.status.set("사람의 채택 이력을 저장했습니다." if accept else "거절 이력을 저장했습니다.")
        except (OSError, ValueError) as exc:
            editor.status.set(str(exc))

    def details(self, _event=None):
        if self.editor.project is None:
            return
        page = self.editor.project.page(self.editor.page_number)
        window = tk.Toplevel(self.editor)
        window.title("로컬 교정 후보 · 영역 재판독 기록")
        window.geometry("780x540")
        text = tk.Text(window, wrap="word", padx=10, pady=10)
        text.pack(fill="both", expand=True)
        data = {"selected_proposal": self.current(), "region_readings": page.get("region_readings", []),
                "source_checks": page.get("refinement_source_checks", []),
                "image_only_checks": page.get("image_only_checks", []),
                "decisions": page.get("refinement_decisions", [])}
        text.insert("1.0", json.dumps(data, ensure_ascii=False, indent=2))
        text.configure(state="disabled")

    def highlight_image(self):
        item, editor = self.editor._selected_refinement, self.editor
        if not item or not editor.project or not editor._preview:
            return False
        page = editor.project.page(editor.page_number)
        review = next((row for row in page.get("refinement_reviews", []) if row["id"] == item["review_id"]), None)
        run = next((row for row in page["runs"] if review and row["id"] == review["baseline_run_id"]), None)
        evidence = item.get("evidence", {})
        box = evidence.get("bbox") or item.get("bbox")
        if not box or not run:
            return False
        raw = run["raw"]
        sx = editor._preview.width / raw["image_width"] * editor._image_scale
        sy = editor._preview.height / raw["image_height"] * editor._image_scale
        ox, oy = editor._origin
        editor.canvas.create_rectangle(ox + box[0] * sx, oy + box[1] * sy,
            ox + box[2] * sx, oy + box[3] * sy, outline="#087a9e", width=3, tags="evidence")
        return True
