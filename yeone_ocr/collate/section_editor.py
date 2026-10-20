"""Bookmark-scoped PDF comparison and direct editing of a corrected HWPX."""
from __future__ import annotations

from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
import difflib
from pathlib import Path
import re
import sys
import tkinter as tk
from tkinter import font as tkfont
from tkinter import ttk, messagebox


_PDF_EXECUTOR = None


def paragraph_input_issue(text, baseline=""):
    """Return the first character that cannot belong to one editable paragraph."""
    existing = iter(char for char in baseline if ord(char) < 32 or char in "\u2028\u2029")
    for offset, char in enumerate(text):
        if (ord(char) < 32 or char in "\u2028\u2029") and not any(old == char for old in existing):
            return offset, char
    return None


def join_paragraph_lines(text, baseline=""):
    """Join pasted line separators without trimming any surrounding text."""
    if not baseline:
        return re.sub(r"\r\n|[\r\n\t\u2028\u2029]", " ", text)
    old_controls = [char for char in baseline if ord(char) < 32 or char in "\u2028\u2029"]
    controls = [(offset, char) for offset, char in enumerate(text) if ord(char) < 32 or char in "\u2028\u2029"]
    added = {offset for offset, _char in controls}
    for op, _i, _j, first, last in difflib.SequenceMatcher(
            None, old_controls, [char for _offset, char in controls], autojunk=False).get_opcodes():
        if op == "equal":
            added.difference_update(offset for offset, _char in controls[first:last])
    result = []
    for offset, char in enumerate(text):
        if offset in added and char in "\r\n\t\u2028\u2029":
            if char == "\n" and offset - 1 in added and text[offset - 1] == "\r":
                continue
            result.append(" ")
        else:
            result.append(char)
    return "".join(result)


def copy_selected_text(widget, *, on_status=None):
    """Copy exactly the selection, including unsaved Unicode edits."""
    try:
        value = widget.get("sel.first", "sel.last")
    except tk.TclError:
        if on_status:
            on_status("복사할 글자를 먼저 드래그해 선택하세요. 파란 교정 표시는 글자 선택과 다릅니다.")
        return "break"
    if not value:
        return "break"
    try:
        widget.clipboard_clear()
        widget.clipboard_append(value)
    except tk.TclError:
        messagebox.showerror("복사하지 못했습니다", "클립보드를 사용할 수 없습니다. 잠시 뒤 다시 복사해 주세요.",
                             parent=widget.winfo_toplevel())
        return "break"
    if on_status:
        on_status(f"선택한 글자 {len(value)}개를 복사했습니다. 입력한 수정은 그대로 남아 있습니다.")
    return "break"


def _copy_control_key(widget, event, *, on_status=None):
    """Windows IMEs can change keysyms; the physical C key still means copy."""
    state = int(getattr(event, "state", 0))
    if (sys.platform == "win32" and getattr(event, "keycode", None) == 67
            and state & 4 and not state & (8 | 0x20000)):
        return copy_selected_text(widget, on_status=on_status)
    return None


def bind_text_copy(widget, *, on_status=None):
    """Provide stable selection, standard shortcuts and a discoverable copy menu."""
    widget.configure(exportselection=False)
    widget.tag_raise("sel")

    def copy(_event=None):
        return copy_selected_text(widget, on_status=on_status)

    def select_all(_event=None):
        widget.focus_set()
        widget.tag_add("sel", "1.0", "end-1c")
        widget.tag_raise("sel")
        return "break"

    menu = tk.Menu(widget, tearoff=False)
    menu.add_command(label="복사", accelerator="Ctrl+C", command=copy)
    menu.add_command(label="전체 선택", accelerator="Ctrl+A", command=select_all)

    def popup(event):
        # Right-click must not move the insertion point or discard the selection.
        menu.entryconfigure(0, state="normal" if widget.tag_ranges("sel") else "disabled")
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()
        return "break"

    widget._copy_menu = menu
    widget._copy_popup = popup
    for sequence in ("<<Copy>>", "<Control-c>", "<Control-C>", "<Control-Insert>"):
        widget.bind(sequence, copy)
    for sequence in ("<Control-a>", "<Control-A>"):
        widget.bind(sequence, select_all)
    widget.bind("<Control-KeyPress>", lambda event: _copy_control_key(widget, event, on_status=on_status))
    widget.bind("<Button-3>", popup)
    return menu


def shared_pdf_worker():
    """Isolate native PDF work from the main app's PDF viewers and threads."""
    global _PDF_EXECUTOR
    if _PDF_EXECUTOR is None:
        _PDF_EXECUTOR = ProcessPoolExecutor(max_workers=1)
    return _PDF_EXECUTOR


def validate_pdf_range(path, pdf_pages):
    import pymupdf
    with pymupdf.open(path) as document:
        first, last = map(int, pdf_pages)
        if not 1 <= first <= last <= len(document):
            raise ValueError("선택 구획의 PDF 쪽 범위를 확인한 뒤 대조 편집을 여십시오.")
    return first, last


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
        picture = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
        picture.info["pdf_scale"] = factor
        return picture


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
        tk.Label(self, text=f"문단 {len(changes)}개 · {target.name}\n"
                 "직접 고친 글자는 OCR 교정 표시를 지우고 원래 서식으로 저장합니다.\n"
                 "수정 전후를 별도 기록하고, 같은 책의 다음 새 판독에 참고합니다. 저장 전 사본도 보관합니다.",
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
        bind_text_copy(text)
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


class CorrectionHistoryDialog(tk.Toplevel):
    """Show committed human edits without mixing in the current editor draft."""
    def __init__(self, parent, records):
        super().__init__(parent)
        self.title("사람 수정 기록")
        self.transient(parent)
        self.geometry("800x650")
        self.minsize(560, 420)
        colors = parent.colors
        self.configure(bg=colors["BG"])
        self.columnconfigure(0, weight=1)
        self.rowconfigure(2, weight=1)
        tk.Label(self, text="사람 수정 기록", anchor="w", bg=colors["BG"], fg=colors["INK"],
                 font=("Malgun Gothic", 16, "bold")).grid(
                     row=0, column=0, sticky="ew", padx=22, pady=(20, 8))
        self.note = tk.Label(
            self, text="이 구획에서 저장한 수정 전후입니다. 되돌린 이력도 확인할 수 있습니다.\n"
            "같은 책을 새로 판독할 때 최근의 유효한 짧은 수정 사례를 최대 24개 1차 판독기에 전달합니다.\n"
            "원본 이미지를 확인하는 2차 판독은 독립적으로 진행합니다. 기존 판독 기록은 유지됩니다.",
            anchor="w", justify="left", bg=colors["BG"], fg=colors["MUTED"])
        self.note.grid(row=1, column=0, sticky="ew", padx=22, pady=(0, 14))
        self.note.bind("<Configure>", lambda e: self.note.configure(wraplength=max(200, e.width)))
        body = tk.Frame(self, bg=colors["PANEL"])
        body.grid(row=2, column=0, sticky="nsew", padx=22)
        self.text = tk.Text(body, wrap="word", relief="flat", bg=colors["PANEL"], fg=colors["INK"],
                            font=("Malgun Gothic", 12), padx=14, pady=12, spacing3=8)
        scroll = ttk.Scrollbar(body, orient="vertical", command=self.text.yview)
        self.text.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.text.pack(side="left", fill="both", expand=True)
        self.text.tag_configure("heading", font=("Malgun Gothic", 10, "bold"), foreground=colors["ACCENT"])
        self.text.tag_configure("before", foreground=colors["MUTED"])
        self.text.tag_configure("changed", background="#e2edf3", foreground=colors["BLUE"])
        if not records:
            self.text.insert("end", "이 구획에 저장된 수정 전후 기록이 없습니다.\n\n"
                             "이 버전부터 대조 편집에서 수정하고 저장하면 기록이 쌓입니다. "
                             "이전 버전의 문단 보호 기록은 그대로 유지됩니다.")
        for record in reversed(records):
            self.text.insert("end", f"{record['time']}  ·  {record['section']}  ·  문단 {record['paragraph'] + 1}\n", "heading")
            self.text.insert("end", f"수정 전\n{record['before']}\n\n", "before")
            self.text.insert("end", "수정 후\n")
            before, after = record["before"], record["after"]
            for op, _, _, start, end in difflib.SequenceMatcher(None, before, after, autojunk=False).get_opcodes():
                self.text.insert("end", after[start:end], "changed" if op in ("replace", "insert") else ())
            self.text.insert("end", "\n\n")
        self.text.configure(state="disabled")
        bind_text_copy(self.text)
        ttk.Button(self, text="닫기", command=self.destroy).grid(row=3, column=0, sticky="e", padx=22, pady=18)
        self.bind("<Escape>", lambda _event: self.destroy())


class FindReplaceDialog(tk.Toplevel):
    """Preview literal replacements and stage them in the editor's existing draft."""
    SCOPES = {"선택 문단": "paragraph", "현재 구획": "section", "교정본 전체": "document"}

    def __init__(self, parent):
        super().__init__(parent)
        self.editor = parent
        self.plan = None
        self._occurrences = []
        self._occurrence = -1
        self.title("찾기 · 바꾸기")
        self.transient(parent)
        self.geometry("760x600")
        self.minsize(540, 430)
        self.configure(bg=parent.colors["BG"])
        self.columnconfigure(1, weight=1)
        self.rowconfigure(6, weight=1)
        self.v_find = tk.StringVar()
        self.v_replace = tk.StringVar()
        self.v_scope = tk.StringVar(value="현재 구획")
        self.v_summary = tk.StringVar(value="찾을 글자와 바꿀 글자를 입력하고 미리 보기를 누르세요.")
        for row, label, variable in ((0, "찾을 글자", self.v_find), (1, "바꿀 글자", self.v_replace)):
            tk.Label(self, text=label, bg=parent.colors["BG"], fg=parent.colors["INK"]).grid(
                row=row, column=0, sticky="w", padx=(18, 10), pady=(16 if row == 0 else 8, 0))
            entry = ttk.Entry(self, textvariable=variable)
            entry.grid(row=row, column=1, sticky="ew", padx=(0, 18), pady=(16 if row == 0 else 8, 0))
            if row == 0:
                self.find_entry = entry
        tk.Label(self, text="바꿀 범위", bg=parent.colors["BG"], fg=parent.colors["INK"]).grid(
            row=2, column=0, sticky="w", padx=(18, 10), pady=8)
        self.scope_choice = ttk.Combobox(self, state="readonly", values=list(self.SCOPES),
                                         textvariable=self.v_scope, width=22)
        self.scope_choice.grid(row=2, column=1, sticky="w", pady=8)
        note = tk.Label(self, text="입력한 글자 그대로 찾습니다. 빈 바꿀 글자는 삭제입니다.\n"
                       "미리 본 뒤 적용하면 편집 중인 수정에 합쳐집니다. 파일은 ‘변경 확인·저장’에서 저장합니다.",
                       bg=parent.colors["BG"], fg=parent.colors["MUTED"], anchor="w", justify="left")
        note.grid(row=3, column=0, columnspan=2, sticky="ew", padx=18, pady=(0, 8))
        note.bind("<Configure>", lambda event: note.configure(wraplength=max(200, event.width)))
        actions = tk.Frame(self, bg=parent.colors["BG"])
        actions.grid(row=4, column=0, columnspan=2, sticky="ew", padx=18)
        self.preview_button = ttk.Button(actions, text="찾기 · 미리 보기", command=self.preview)
        self.preview_button.pack(side="left")
        self.next_button = ttk.Button(actions, text="다음 찾기", command=self.find_next, state="disabled")
        self.next_button.pack(side="left", padx=6)
        self.undo_button = ttk.Button(actions, text="마지막 바꾸기 취소", command=self.undo,
                                      state="normal" if parent._replace_undo else "disabled")
        self.undo_button.pack(side="right")
        summary = tk.Label(self, textvariable=self.v_summary, bg=parent.colors["BG"],
                           fg=parent.colors["INK"], anchor="w", justify="left")
        summary.grid(row=5, column=0, columnspan=2, sticky="ew", padx=18, pady=10)
        summary.bind("<Configure>", lambda event: summary.configure(wraplength=max(200, event.width)))
        area = tk.Frame(self, bg=parent.colors["PANEL"])
        area.grid(row=6, column=0, columnspan=2, sticky="nsew", padx=18)
        self.preview_text = tk.Text(area, wrap="word", relief="flat", bg=parent.colors["PANEL"],
                                    fg=parent.colors["INK"], font=("Malgun Gothic", 11), padx=10, pady=8)
        scrollbar = ttk.Scrollbar(area, command=self.preview_text.yview)
        self.preview_text.configure(yscrollcommand=scrollbar.set, state="disabled")
        scrollbar.pack(side="right", fill="y")
        self.preview_text.pack(side="left", fill="both", expand=True)
        self.preview_text.tag_configure("heading", foreground=parent.colors["ACCENT"])
        self.preview_text.tag_configure("before", foreground=parent.colors["MUTED"])
        self.preview_text.tag_configure("match", background="#fff0aa")
        bind_text_copy(self.preview_text)
        buttons = tk.Frame(self, bg=parent.colors["BG"])
        buttons.grid(row=7, column=0, columnspan=2, sticky="ew", padx=18, pady=14)
        ttk.Button(buttons, text="닫기 · 계속 편집", command=self.destroy).pack(side="right")
        self.all_button = ttk.Button(buttons, text="모두 바꾸기 · 편집에 적용", command=self.replace_all,
                                     state="disabled")
        self.all_button.pack(side="right", padx=6)
        self.one_button = ttk.Button(buttons, text="한 곳 바꾸기", command=self.replace_one, state="disabled")
        self.one_button.pack(side="right")
        for variable in (self.v_find, self.v_replace, self.v_scope):
            variable.trace_add("write", self._invalidate)
        if parent._active in parent._editors:
            widget = parent._editors[parent._active]
            try:
                self.v_find.set(widget.get("sel.first", "sel.last"))
            except tk.TclError:
                pass
        self.bind("<Return>", lambda _event: (self.preview(), "break")[1])
        self.bind("<Escape>", lambda _event: self.destroy())
        self.find_entry.focus_set()

    def _invalidate(self, *_args):
        self.plan = None
        self._occurrences = []
        self._occurrence = -1
        for button in (self.all_button, self.one_button, self.next_button):
            button.configure(state="disabled")
        self.v_summary.set("조건이 바뀌었습니다. ‘찾기 · 미리 보기’를 눌러 결과를 확인하세요.")

    def _make_plan(self, *, match=None):
        options = dict(scope=self.SCOPES[self.v_scope.get()], paragraph_index=self.editor._active,
                       changes=self.editor._changes(), previous_plan=self.editor._replacement_plan)
        if match is not None:
            options["match"] = match
        return self.editor.session.preview_replace(self.v_find.get(), self.v_replace.get(), **options)

    def preview(self):
        if self.editor.busy or not self.editor._prepare_paragraph_input():
            return False
        self.configure(cursor="watch")
        self.update_idletasks()
        try:
            plan = self._make_plan()
        except (OSError, ValueError) as exc:
            self._invalidate()
            self.v_summary.set(str(exc))
            return False
        finally:
            self.configure(cursor="")
        self.plan = plan
        self._preview_draft = self.editor._changes()
        self._preview_generation = self.editor._snapshot_generation
        self._occurrences = []
        needle = self.v_find.get()
        for entry in plan.entries:
            offset = 0
            while needle and (offset := entry.before.find(needle, offset)) >= 0:
                self._occurrences.append((entry.index, offset))
                offset += len(needle)
        self._occurrence = -1
        skipped = sum(item.count for item in plan.skipped)
        self.v_summary.set(f"{self.v_scope.get()} · 바꾸기 가능 {plan.matches}곳 / {plan.paragraph_count}문단"
                           f" · 읽기 전용 등 제외 {skipped}곳\n아직 편집 내용이나 파일을 변경하지 않았습니다.")
        text = self.preview_text
        text.configure(state="normal")
        text.delete("1.0", "end")
        for entry in plan.entries[:100]:
            text.insert("end", f"문단 {entry.index + 1} · {entry.count}곳\n", "heading")
            text.insert("end", f"바꾸기 전  {entry.before}\n", "before")
            text.insert("end", f"바꾸기 후  {entry.after}\n\n")
        if len(plan.entries) > 100:
            text.insert("end", f"처음 100문단을 표시합니다. 전체 {len(plan.entries)}문단이 적용 대상입니다.\n\n")
        for item in plan.skipped[:30]:
            text.insert("end", f"제외 · 문단 {item.index + 1} · {item.count}곳 · {item.reason}\n", "before")
        if not plan.entries and not plan.skipped:
            text.insert("end", "찾는 글자가 없습니다.")
        text.configure(state="disabled")
        state = "normal" if plan.matches and self.v_find.get() != self.v_replace.get() else "disabled"
        self.all_button.configure(state=state)
        self.one_button.configure(state=state)
        self.next_button.configure(state="normal" if self._occurrences else "disabled")
        return True

    def find_next(self):
        if self.plan is None and not self.preview():
            return
        if (self.editor._changes() != self._preview_draft
                or self.editor._snapshot_generation != self._preview_generation):
            self._invalidate()
            self.v_summary.set("편집 내용이 바뀌었습니다. 다시 미리 보기를 눌러 주세요.")
            return
        if not self._occurrences:
            return
        self._occurrence = (self._occurrence + 1) % len(self._occurrences)
        index, offset = self._occurrences[self._occurrence]
        self.v_summary.set(f"{self._occurrence + 1} / {len(self._occurrences)}곳 · 문단 {index + 1}"
                           + (" · 현재 구획 밖의 문단입니다." if index not in self.editor._editors else ""))
        if index in self.editor._editors:
            self.editor._reveal_text(index, offset, len(self.v_find.get()), focus=False)
        entry = next(item for item in self.plan.entries if item.index == index)
        text = self.preview_text
        text.configure(state="normal")
        text.delete("1.0", "end")
        text.insert("end", f"문단 {index + 1} · 현재 찾은 위치\n", "heading")
        text.insert("end", entry.before[:offset], "before")
        text.insert("end", entry.before[offset:offset + len(self.v_find.get())], "match")
        text.insert("end", entry.before[offset + len(self.v_find.get()):] + "\n\n", "before")
        text.insert("end", "모두 바꾸기 후\n", "heading")
        text.insert("end", entry.after)
        text.configure(state="disabled")

    def _ready(self):
        if self.plan is None:
            self.preview()
            return False
        if (self.editor.busy or self.editor._changes() != self._preview_draft
                or self.editor._snapshot_generation != self._preview_generation):
            self._invalidate()
            self.v_summary.set("편집 내용이 바뀌었습니다. 다시 미리 보기를 눌러 주세요.")
            return False
        return True

    def replace_all(self):
        if not self._ready():
            return False
        count = self.plan.matches
        self.editor._apply_replacement(self.plan)
        self._invalidate()
        self.undo_button.configure(state="normal")
        self.v_summary.set(f"{count}곳을 편집 내용에 적용했습니다. 파일에 반영하려면 ‘변경 확인·저장’을 누르세요.")
        return True

    def replace_one(self):
        if not self._ready() or not self._occurrences:
            return False
        position = self._occurrences[max(0, self._occurrence)]
        try:
            plan = self._make_plan(match=position)
        except (OSError, ValueError) as exc:
            self.v_summary.set(str(exc))
            return False
        self.editor._apply_replacement(plan)
        self._invalidate()
        self.undo_button.configure(state="normal")
        self.v_summary.set(f"문단 {position[0] + 1}의 한 곳을 편집에 적용했습니다. 아직 파일에 저장하지 않았습니다.")
        return True

    def undo(self):
        if self.editor._undo_replacement():
            self._invalidate()
            self.undo_button.configure(state="normal" if self.editor._replace_undo else "disabled")
            self.v_summary.set("마지막 바꾸기를 취소했습니다. 그 전에 입력한 수정은 유지됩니다.")

    def destroy(self):
        editor = self.editor
        if editor is not None and editor._find_dialog is self:
            editor._find_dialog = None
        for variable in ("v_find", "v_replace", "v_scope", "v_summary"):
            setattr(self, variable, None)
        super().destroy()
        self.editor = None


class BookmarkSectionEditor(tk.Toplevel):
    """Read-only PDF on the left, flowing editable HWPX paragraphs on the right.

    Cursor following uses verified PDF text positions. Manual page navigation
    remains available. The backend preserves document structure, verifies
    snapshots and writes only the cumulative corrected document, with a backup.
    """
    def __init__(self, parent, *, pdf_path, hwpx_path, section_name, section_start,
                 section_end, pdf_pages, can_write=None, on_saved=None, heading_styles=None,
                 anchor_source="auto"):
        from .section_edit import SectionEditSession
        # Validate/load before creating a window, so the caller can report failure.
        scope_options = {"heading_styles": list(heading_styles) if heading_styles else None, "anchor_source": anchor_source}
        pdf_worker = shared_pdf_worker()
        session = SectionEditSession.open(str(pdf_path), str(hwpx_path),
                                          section_name, section_start, section_end, **scope_options)
        first, last = pdf_worker.submit(validate_pdf_range, pdf_path, pdf_pages).result()
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
        self._external_changes = {}
        self._replacement_plan = None
        self._replace_undo = []
        self._find_dialog = None
        self._snapshot_generation = 0
        self._editors = {}
        self._cards = {}
        self._markers = {}
        self._active = None
        self._show_marks = True
        self._show_original = True
        self._text_size = 12
        self._editor_font = tkfont.Font(self, family="Malgun Gothic", size=self._text_size)
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
        self._position_job = None
        self._position_ensure_visible = False
        self._layout_follow_cursor = False
        self._initial_job = None
        self._change_jobs = {}
        self._executor = pdf_worker
        self._alignment_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="bookmark-location")
        self._alignment_generation = 0
        self._alignment_future = None
        self._index_future = None
        self._cursor_map = None
        self._cursor_generation = 0
        self._cursor_requested = False
        self._cursor_future = None
        self._cursor_job = None
        self._cursor_location = None
        self._cursor_scroll_pending = False
        self._image_size = None
        self._displayed_page = None
        self._rendered_scale = 1.0
        self._photo = None
        self.title(f"대조 편집 — {section_name}")
        self.geometry("1280x900")
        self.minsize(900, 640)
        self.configure(bg=self.colors["BG"])
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.bind("<Destroy>", self._destroyed, add="+")
        self.bind("<Control-s>", self._save_shortcut)
        self.bind("<Control-S>", self._save_shortcut)
        for shortcut in ("<Control-h>", "<Control-H>", "<Control-f>", "<Control-F>"):
            self.bind(shortcut, self.open_find_replace)
        self._build()
        self._build_paragraphs()
        self._start_cursor_map()
        self._initial_job = self.after(120, self._initial_split)
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
        self._label(head, f"PDF {self.pdf_pages[0]}–{self.pdf_pages[1]}쪽  ·  HWPX {len(self._paragraphs)}문단  ·  찾기·바꾸기로 전체 교정본도 수정",
                    muted=True, bg=C["BG"]).pack(anchor="w")
        self.panes = ttk.Panedwindow(self, orient="horizontal")
        self.panes.grid(row=1, column=0, sticky="nsew", padx=18)
        self.pdf_panel = tk.Frame(self.panes, bg=C["PANEL"], highlightthickness=1, highlightbackground=C["LINE"])
        self.doc_panel = tk.Frame(self.panes, bg=C["PANEL"], highlightthickness=1, highlightbackground=C["LINE"])
        for panel in (self.pdf_panel, self.doc_panel):
            panel.configure(width=360, height=300)
            panel.grid_propagate(False)
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
        self.history_button = ttk.Button(actions, text="수정 기록 보기", command=self.show_correction_history)
        self.history_button.pack(side="left", padx=(0, 7))
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
        follow = tk.Frame(panel, bg=self.colors["PANEL"])
        follow.grid(row=3, column=0, sticky="ew", padx=10, pady=(6, 0))
        self.v_follow_cursor = tk.BooleanVar(value=True)
        self.follow_check = tk.Checkbutton(follow, text="커서 따라가기", variable=self.v_follow_cursor,
                                          command=self._toggle_cursor_follow, bg=self.colors["PANEL"],
                                          activebackground=self.colors["PANEL"], fg=self.colors["INK"],
                                          font=("Malgun Gothic", 9), highlightthickness=0)
        self.follow_check.pack(side="left")
        self._label(follow, "노랑 · 대응 줄    테두리 · 커서 글자", muted=True, size=9).pack(side="right")
        self.pdf_note = self._label(panel, "원본의 글자 위치를 연결하고 있습니다.", muted=True, size=9, justify="left")
        self.pdf_note.grid(row=4, column=0, sticky="ew", padx=12, pady=(3, 8))
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
        self.find_button = self._tool_button(heading, "찾기·바꾸기  Ctrl H", self.open_find_replace)
        self.find_button.grid(row=2, column=0, columnspan=2, sticky="w", pady=(7, 0))
        tools = tk.Frame(panel, bg="#fcfaf5")
        tools.grid(row=1, column=0, sticky="ew", padx=7, pady=(0, 7))
        self.v_match_pdf_font = tk.BooleanVar(value=True)
        self.font_check = tk.Checkbutton(tools, text="PDF 글자 크기 맞춤", variable=self.v_match_pdf_font,
                                        command=self._sync_text_size, bg="#fcfaf5", activebackground="#fcfaf5",
                                        fg=self.colors["INK"], font=("Malgun Gothic", 9), highlightthickness=0)
        self.font_check.pack(side="left")
        self._tool_button(tools, "가+", self.toggle_text_size, width=2).pack(side="right")
        self._tool_button(tools, "가−", lambda: self.change_text_size(-1), width=2).pack(side="right", padx=2)
        self.marks_button = self._tool_button(tools, "차이 켜짐", self.toggle_marks)
        self.marks_button.pack(side="right", padx=4)
        area = tk.Frame(panel, bg=self.colors["PANEL"])
        area.grid(row=2, column=0, sticky="nsew")
        area.columnconfigure(0, weight=1); area.rowconfigure(0, weight=1)
        self.doc_canvas = tk.Canvas(area, bg=self.colors["PANEL"], highlightthickness=0, width=360, height=300,
                                    yscrollincrement=1)
        scroll = ttk.Scrollbar(area, orient="vertical", command=self.doc_canvas.yview)
        self.doc_canvas.configure(yscrollcommand=lambda a, b: self._document_scrolled(scroll, a, b))
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
        bind_text_copy(self.original_text, on_status=self._copy_status)

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
                             insertbackground=self.colors["ACCENT"], font=self._editor_font,
                             height=2, padx=0, pady=3, spacing1=2, spacing3=4, undo=paragraph.editable, maxundo=100)
            editor.pack(fill="x")
            editor.insert("1.0", paragraph.corrected_text)
            editor.tag_configure("changed", background="#e9f2fa", foreground="#235880")
            editor.tag_configure("input_issue", background="#ffe29b", foreground="#8c271b")
            editor.tag_configure("search_match", background="#fff0aa")
            bind_text_copy(editor, on_status=self._copy_status)
            editor.edit_modified(False)
            editor.bind("<<Modified>>", lambda _event, i=index: self._modified(i))
            editor.bind("<FocusIn>", lambda _event, i=index: self.select_paragraph(i))
            editor.bind("<Button-1>", lambda _event, i=index: self.select_paragraph(i), add="+")
            editor.bind("<ButtonRelease-1>", lambda _event, i=index: self._schedule_cursor(i), add="+")
            editor.bind("<KeyRelease>", lambda _event, i=index: self._schedule_cursor(i), add="+")
            for shortcut in ("<Control-h>", "<Control-H>", "<Control-f>", "<Control-F>"):
                editor.bind(shortcut, self.open_find_replace)
            editor.bind("<Return>", self._paragraph_return)
            editor.bind("<KP_Enter>", self._paragraph_return)
            editor.bind("<Tab>", lambda _event, i=index: self._paragraph_tab(i, 1))
            editor.bind("<Shift-Tab>", lambda _event, i=index: self._paragraph_tab(i, -1))
            editor.bind("<<Paste>>", lambda _event, i=index: self._paste_paragraph(i))
            editor.bind("<<PasteSelection>>", lambda _event, i=index: self._paste_paragraph(i))
            editor.bind("<MouseWheel>", lambda event: self._wheel(self.doc_canvas, event))
            editor.bind("<Button-4>", lambda _event: (self.doc_canvas.yview_scroll(-72, "units"), "break")[1])
            editor.bind("<Button-5>", lambda _event: (self.doc_canvas.yview_scroll(72, "units"), "break")[1])
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
        step = 72 if float(canvas.cget("yscrollincrement")) == 1 else 3
        canvas.yview_scroll(-step if event.delta > 0 else step, "units")
        return "break"

    def _document_scrolled(self, scrollbar, first, last):
        scrollbar.set(first, last)
        if self._cursor_location is not None:
            self._schedule_cursor_position()

    def _schedule_cursor_position(self, *, ensure_visible=False):
        self._position_ensure_visible |= ensure_visible
        if not self._closed and self._position_job is None:
            self._position_job = self.after(90, self._position_cursor)

    def _position_cursor(self):
        self._position_job = None
        ensure_visible = self._position_ensure_visible
        self._position_ensure_visible = False
        if not self._closed:
            self._cursor_scroll_pending = True
            self._draw_cursor_marker(ensure_editor_visible=ensure_visible)

    def _initial_split(self):
        self._initial_job = None
        if not self._closed:
            # Pane widths exclude the theme's separator (5–12 px on Windows).
            # Balance their usable space, once, rather than centering its edge.
            difference = self.doc_panel.winfo_width() - self.pdf_panel.winfo_width()
            self.panes.sashpos(0, self.panes.sashpos(0) + difference // 2)
            self._request_render()

    def _document_resized(self, event):
        self.doc_canvas.itemconfigure(self._doc_window, width=max(250, event.width))
        self._layout_follow_cursor |= self._cursor_location is not None
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
        self._schedule_cursor_position(ensure_visible=self._layout_follow_cursor)
        self._layout_follow_cursor = False

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
        self._schedule_cursor(index)
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
        changes = dict(self._external_changes)
        changes.update({index: editor.get("1.0", "end-1c") for index, editor in self._editors.items()
                        if self._paragraphs[index].editable and editor.get("1.0", "end-1c") != self._baseline[index]})
        return changes

    def _copy_status(self, text):
        if not self._closed:
            self.v_status.set(text)

    def _paragraph_return(self, _event=None):
        self.v_status.set("문단 안에서는 줄바꿈을 넣지 않습니다. 다음 문단으로 이동하려면 Tab을 누르세요.")
        return "break"

    def _paragraph_tab(self, index, direction):
        editable = [i for i, paragraph in self._paragraphs.items() if paragraph.editable]
        if index in editable:
            position = editable.index(index) + direction
            if 0 <= position < len(editable):
                self._reveal_text(editable[position], 0)
            else:
                self.save_button.focus_set()
        return "break"

    def _paste_paragraph(self, index):
        if self.busy or not self._paragraphs[index].editable:
            return "break"
        try:
            value = self.clipboard_get()
        except tk.TclError:
            return "break"
        joined = join_paragraph_lines(value)
        issue = paragraph_input_issue(joined)
        if issue:
            messagebox.showerror("붙여넣을 글자 확인", f"붙여넣을 내용의 {issue[0] + 1}번째 글자에 "
                                 f"제어 문자 U+{ord(issue[1]):04X}가 있습니다.\n"
                                 "내용을 바꾸지 않았습니다. 이 문자를 제외한 글자를 붙여 넣어 주세요.", parent=self)
            return "break"
        if joined != value and not messagebox.askyesno(
                "한 문단으로 붙여넣기", "붙여넣을 내용에 줄바꿈이나 탭이 있습니다.\n"
                "줄바꿈·탭을 공백으로 바꾸어 한 문단에 붙여 넣을까요?\n"
                "기존 수정은 유지되며 Ctrl+Z로 이 붙여넣기를 취소할 수 있습니다.", parent=self):
            return "break"
        widget = self._editors[index]
        widget.edit_separator()
        automatic = widget.cget("autoseparators")
        widget.configure(autoseparators=False)
        try:
            try:
                start, end = widget.index("sel.first"), widget.index("sel.last")
                widget.delete(start, end)
                widget.mark_set("insert", start)
            except tk.TclError:
                pass
            widget.insert("insert", joined)
        finally:
            widget.configure(autoseparators=automatic)
        widget.edit_separator()
        self._modified(index)
        return "break"

    def _reveal_text(self, index, offset=0, length=1, *, focus=True):
        if index not in self._editors:
            return
        self.select_paragraph(index)
        widget = self._editors[index]
        for editor in self._editors.values():
            editor.tag_remove("search_match", "1.0", "end")
        start, end = f"1.0+{offset}c", f"1.0+{offset + length}c"
        widget.tag_add("search_match", start, end)
        widget.mark_set("insert", start)
        widget.see(start)
        self.update_idletasks()
        bounds = self.doc_canvas.bbox("all")
        if bounds and bounds[3] > bounds[1]:
            y = self._cards[index].winfo_y()
            self.doc_canvas.yview_moveto(max(0, (y - 20) / (bounds[3] - bounds[1])))
        if focus:
            widget.focus_set()
        self._schedule_cursor(index)

    def _set_draft_text(self, index, value):
        widget = self._editors[index]
        if widget.get("1.0", "end-1c") == value:
            return
        widget.edit_separator()
        automatic = widget.cget("autoseparators")
        widget.configure(autoseparators=False)
        try:
            widget.delete("1.0", "end")
            widget.insert("1.0", value)
        finally:
            widget.configure(autoseparators=automatic)
        widget.edit_separator()
        widget.edit_modified(False)
        self._highlight(index)

    def _prepare_paragraph_input(self):
        """Offer a reversible repair of accidental line breaks, retaining all drafts."""
        issues = [(index, value, paragraph_input_issue(value, self._baseline.get(index, "")))
                  for index, value in self._changes().items()
                  if paragraph_input_issue(value, self._baseline.get(index, ""))]
        if not issues:
            return True
        index, _, (offset, char) = issues[0]
        self._reveal_text(index, offset)
        repairs = {i: join_paragraph_lines(value, self._baseline.get(i, "")) for i, value, _issue in issues}
        unhandled = [(i, paragraph_input_issue(repairs[i], self._baseline.get(i, "")))
                     for i, _value, _issue in issues
                     if paragraph_input_issue(repairs[i], self._baseline.get(i, ""))]
        if unhandled:
            bad_index, (bad_offset, bad_char) = unhandled[0]
            self._reveal_text(bad_index, bad_offset)
            messagebox.showerror("저장할 글자 확인",
                                 f"문단 {bad_index + 1}의 {bad_offset + 1}번째 글자에 "
                                 f"제어 문자 U+{ord(bad_char):04X}가 있습니다.\n"
                                 "해당 위치를 표시했습니다. 이 글자를 지우거나 바꾼 뒤 다시 저장하세요.\n"
                                 "다른 문단에 입력한 수정은 모두 남아 있습니다.", parent=self)
            return False
        if not messagebox.askyesno("줄바꿈·탭 정리",
                                  f"문단 {index + 1} 등 {len(issues)}개 문단에 줄바꿈이나 탭이 있습니다.\n"
                                  "해당 문자를 공백으로 바꾸고 변경 내용을 확인할까요?\n\n"
                                  "다른 글자와 문단은 그대로 두며, 문단에서 Ctrl+Z로 되돌릴 수 있습니다.",
                                  parent=self):
            return False
        for index, value, _issue in issues:
            if index in self._editors:
                self._set_draft_text(index, repairs[index])
            else:
                self._external_changes[index] = repairs[index]
        self._update_original()
        self._update_status()
        self._schedule_layout()
        return True

    def open_find_replace(self, _event=None):
        if self.busy or self._closed:
            return "break"
        if self._find_dialog is not None and self._find_dialog.winfo_exists():
            self._find_dialog.lift()
            self._find_dialog.find_entry.focus_set()
        else:
            self._find_dialog = FindReplaceDialog(self)
        return "break"

    def _apply_replacement(self, plan):
        before = self._changes()
        undo = (before, dict(self._baseline), self._replacement_plan, dict(plan.changes))
        self._baseline.update(plan.baseline)
        for index, value in plan.changes.items():
            if index in self._editors:
                self._set_draft_text(index, value)
        # A replacement may revert a previous draft to its saved text.
        for index in before:
            if index in self._editors and index not in plan.changes:
                self._set_draft_text(index, self._baseline[index])
        self._external_changes = {i: value for i, value in plan.changes.items() if i not in self._editors}
        self._replacement_plan = plan
        self._replace_undo.append(undo)
        self._update_original()
        self._update_status()
        self._schedule_layout()
        self._schedule_cursor(self._active)

    def _undo_replacement(self):
        if self.busy or not self._replace_undo:
            return False
        before, baseline, previous_plan, expected = self._replace_undo[-1]
        if self._changes() != expected:
            messagebox.showinfo("바꾸기 취소", "바꾸기 이후에 다른 글자를 수정했습니다.\n"
                                "그 수정을 먼저 Ctrl+Z로 되돌리면 바꾸기를 취소할 수 있습니다.\n"
                                "현재 입력한 수정은 그대로 남아 있습니다.", parent=self)
            return False
        self._replace_undo.pop()
        for index in self._editors:
            if self._paragraphs[index].editable:
                self._set_draft_text(index, before.get(index, baseline[index]))
        self._baseline = baseline
        self._external_changes = {i: value for i, value in before.items() if i not in self._editors}
        self._replacement_plan = previous_plan
        self._update_original()
        self._update_status()
        self._schedule_layout()
        self._schedule_cursor(self._active)
        return True

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
        self._schedule_cursor(index)

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
        outside = len(self._external_changes)
        detail = f" · 현재 구획 밖 {outside}개 포함" if outside else ""
        self.v_status.set(f"수정한 문단 {len(changes)}개{detail} · 아직 저장하지 않음" if changes else "저장할 변경 없음")
        for index, marker in self._markers.items():
            readonly = not self._paragraphs[index].editable
            tail = " · 수정 중" if index in changes else " · 읽기 전용" if readonly else ""
            marker.configure(text=f"문단 {index + 1}{tail}",
                             fg=self.colors["ACCENT"] if index in changes else self.colors["MUTED"])
        state = "normal" if changes and not self.busy else "disabled"
        self.save_button.configure(state=state); self.revert_button.configure(state=state)
        self.reload_button.configure(state="disabled" if self.busy else "normal")
        self.find_button.configure(state="disabled" if self.busy else "normal")

    def toggle_marks(self):
        self._show_marks = not self._show_marks
        self.marks_button.configure(text="차이 켜짐" if self._show_marks else "차이 꺼짐")
        for index in self._editors:
            self._highlight(index)

    def toggle_text_size(self):
        self.change_text_size(1)

    def change_text_size(self, direction):
        self.v_match_pdf_font.set(False)
        current = int(self._editor_font.cget("size"))
        self._text_size = (max(-128, min(-8, current - 2 * direction)) if current < 0
                           else max(8, min(48, current + 2 * direction)))
        self._editor_font.configure(size=self._text_size)
        self._cursor_scroll_pending = self._cursor_location is not None
        self._layout_follow_cursor |= self._cursor_location is not None
        self._schedule_layout()

    def _sync_text_size(self):
        """Match measured CJK width in screen pixels, independent of Windows DPI."""
        if not self.v_match_pdf_font.get() or self._cursor_map is None or self._image_size is None:
            return
        width = self._cursor_map.page_font_width(self._displayed_page)
        height = self._cursor_map.page_font_height(self._displayed_page)
        if width is None and height is None:
            return
        target = max(8, min(128, width * self._image_size[0] if width is not None
                            else height * self._image_size[1]))
        probe = tkfont.Font(self, family=self._editor_font.cget("family"), size=-12)
        def measured(size):
            probe.configure(size=-size)
            return probe.measure("漢") if width is not None else probe.metrics("linespace")
        lo, hi = 6, 160
        while lo < hi:
            mid = (lo + hi) // 2
            if measured(mid) < target:
                lo = mid + 1
            else:
                hi = mid
        pixels = min((max(6, lo - 1), lo), key=lambda n: abs(measured(n) - target))
        if int(self._editor_font.cget("size")) != -pixels:
            self._text_size = -pixels
            self._editor_font.configure(size=-pixels)
            self._cursor_scroll_pending = self._cursor_location is not None
            self._layout_follow_cursor |= self._cursor_location is not None
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
            if index not in self._editors:
                continue
            editor = self._editors[index]
            editor.delete("1.0", "end"); editor.insert("1.0", self._baseline[index])
            editor.edit_reset(); editor.edit_modified(False)
            self._highlight(index)
        self._external_changes.clear()
        self._replacement_plan = None
        self._replace_undo.clear()
        if self._find_dialog is not None and self._find_dialog.winfo_exists():
            self._find_dialog._invalidate()
        self._update_original(); self._update_status(); self._schedule_layout()
        self._schedule_cursor(self._active)

    def _sync_page_buttons(self):
        self.v_page.set(str(self._pdf_page))
        self.previous_button.configure(state="disabled" if self._pdf_page <= self.pdf_pages[0] else "normal")
        self.next_button.configure(state="disabled" if self._pdf_page >= self.pdf_pages[1] else "normal")

    def set_page(self, page, *, from_cursor=False):
        if not from_cursor:
            self._cancel_cursor_request()
            self._clear_cursor_marker()
            self.pdf_note.configure(text="PDF 쪽을 직접 이동했습니다. 오른쪽 커서를 움직이면 다시 위치를 찾습니다."
                                    if self.v_follow_cursor.get() else "커서 따라가기 꺼짐 · PDF를 자유롭게 탐색합니다.")
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
        self._cursor_scroll_pending = self._cursor_location is not None
        self._request_render()

    def change_zoom(self, direction):
        scale = self._rendered_scale if self._zoom is None else self._zoom
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
                    self._image_size = None
                    self._displayed_page = None
                    self.pdf_canvas.delete("all")
                    self.pdf_canvas.create_text(20, 25, anchor="nw", width=max(180, self.pdf_canvas.winfo_width() - 40),
                                                text=f"PDF를 표시하지 못했습니다.\n{exc}", fill=self.colors["INK"])
        self._poll_cursor()
        self._poll_job = self.after(60, self._poll_render)

    def _display_pdf(self, picture):
        from PIL import ImageTk
        self._photo = ImageTk.PhotoImage(picture, master=self)
        self.pdf_canvas.delete("all")
        self.pdf_canvas.create_image(16, 16, anchor="nw", image=self._photo)
        self.pdf_canvas.configure(scrollregion=(0, 0, picture.width + 32, picture.height + 32))
        self._image_size = picture.size
        self._displayed_page = self._pdf_page
        self._rendered_scale = picture.info.get("pdf_scale", self._zoom or 1.0)
        picture.close()
        self._sync_text_size()
        self._draw_cursor_marker()

    def _start_cursor_map(self):
        """Index only the selected PDF pages; no OCR or document writes."""
        from .cursor_map import read_cursor_pages
        self._alignment_generation += 1
        self._cancel_cursor_request()
        self._clear_cursor_marker()
        self._cursor_map = None
        if self._alignment_future is not None:
            self._alignment_future[1].cancel()
            self._alignment_future = None
        if self._index_future is not None:
            self._index_future[1].cancel()
        self.pdf_note.configure(text="원본의 글자 위치를 연결하고 있습니다.")
        # PyMuPDF extraction and rendering share one isolated process. CPU
        # alignment never opens a PDF and stays off the Tk thread.
        self._index_future = (self._alignment_generation, self._executor.submit(
            read_cursor_pages, self.pdf_path, self.pdf_pages))
        self._cursor_requested = self.v_follow_cursor.get() and self._active is not None

    def _cancel_cursor_request(self):
        self._cursor_generation += 1
        self._cursor_requested = False
        if self._cursor_job is not None:
            self.after_cancel(self._cursor_job)
            self._cursor_job = None
        if self._cursor_future is not None:
            self._cursor_future[1].cancel()
            self._cursor_future = None

    def _clear_cursor_marker(self):
        self._cursor_location = None
        self._cursor_scroll_pending = False
        self._position_ensure_visible = False
        self._layout_follow_cursor = False
        self.pdf_canvas.delete("cursor-location")

    def _toggle_cursor_follow(self):
        self._cancel_cursor_request()
        self._clear_cursor_marker()
        if not self.v_follow_cursor.get():
            self.pdf_note.configure(text="커서 따라가기 꺼짐 · PDF를 자유롭게 탐색합니다.")
        elif self._cursor_map is not None:
            self._schedule_cursor(self._active)
        elif self._alignment_future is None and self._index_future is None:
            self._start_cursor_map()
        else:
            self._cursor_requested = self._active is not None
            self.pdf_note.configure(text="원본의 글자 위치를 연결하고 있습니다.")

    def _schedule_cursor(self, index):
        if self._closed or index != self._active or not self.v_follow_cursor.get():
            return
        self._cancel_cursor_request()
        self._clear_cursor_marker()
        self._cursor_requested = True
        self._cursor_job = self.after(90, self._submit_cursor)

    def _submit_cursor(self):
        self._cursor_job = None
        if self._closed or not self.v_follow_cursor.get() or self._active not in self._editors:
            return
        mapping = self._cursor_map
        if mapping is None:
            return
        if not mapping.available:
            self.pdf_note.configure(text="이 구획에 사용할 수 있는 PDF 글자 위치 정보가 없습니다.")
            return
        widget = self._editors[self._active]
        current = widget.get("1.0", "end-1c")
        # Reading the prefix keeps Python code-point offsets even on Tcl 8.6,
        # whose string length can count supplementary characters twice.
        offset = min(len(current), len(widget.get("1.0", "insert")))
        request = (self._active, current, offset)
        self._cursor_future = (self._cursor_generation, self._alignment_executor.submit(mapping.locate, *request))
        self.pdf_note.configure(text="커서의 원본 위치를 찾고 있습니다.")

    def _poll_cursor(self):
        pending = self._index_future
        if pending and pending[1].done():
            self._index_future = None
            if pending[0] == self._alignment_generation and not pending[1].cancelled():
                try:
                    from .cursor_map import align_cursor_map
                    self._alignment_future = (pending[0], self._alignment_executor.submit(
                        align_cursor_map, pending[1].result(), tuple(self.session.paragraphs)))
                except Exception:
                    self.pdf_note.configure(text="원본의 글자 위치를 불러오지 못했습니다. 커서 따라가기를 다시 켜서 재시도할 수 있습니다.")
        pending = self._alignment_future
        if pending and pending[1].done():
            self._alignment_future = None
            if pending[0] == self._alignment_generation and not pending[1].cancelled():
                try:
                    self._cursor_map = pending[1].result()
                    self._sync_text_size()
                    if self.v_follow_cursor.get() and self._cursor_requested:
                        self._schedule_cursor(self._active)
                except Exception:
                    self.pdf_note.configure(text="원본의 글자 위치를 불러오지 못했습니다. 커서 따라가기를 다시 켜서 재시도할 수 있습니다.")
        pending = self._cursor_future
        if pending and pending[1].done():
            self._cursor_future = None
            if pending[0] == self._cursor_generation and not pending[1].cancelled() and self.v_follow_cursor.get():
                try:
                    self._apply_cursor_location(pending[1].result())
                except Exception:
                    self._clear_cursor_marker()
                    self.pdf_note.configure(text="대응 위치를 확인하지 못했습니다. PDF를 직접 확인하십시오.")

    def _apply_cursor_location(self, location):
        if location is None or not self.pdf_pages[0] <= location.page <= self.pdf_pages[1]:
            self._clear_cursor_marker()
            self.pdf_note.configure(text="대응 위치를 확실히 찾지 못했습니다. PDF를 직접 확인하십시오.")
            return
        self._cursor_location = location
        self._cursor_scroll_pending = True
        self.pdf_note.configure(text=location.message)
        self.set_page(location.page, from_cursor=True)
        self._draw_cursor_marker()

    def _draw_cursor_marker(self, *, ensure_editor_visible=True):
        self.pdf_canvas.delete("cursor-location")
        location = self._cursor_location
        if (location is None or self._image_size is None or not self.v_follow_cursor.get()
                or location.page != self._displayed_page or location.page != self._pdf_page):
            return
        width, height = self._image_size
        def pixels(box):
            x0, y0, x1, y1 = box
            return (16 + max(0, min(1, x0)) * width, 16 + max(0, min(1, y0)) * height,
                    16 + max(0, min(1, x1)) * width, 16 + max(0, min(1, y1)) * height)
        targets = []
        for box in location.boxes:
            rect = pixels(box)
            targets.append(rect)
            self.pdf_canvas.create_rectangle(*rect, fill="#ffe28a", stipple="gray25", outline="#d59e20",
                                             width=1, tags=("cursor-location", "cursor-line"))
        if location.precise and location.caret_box is not None:
            rect = pixels(location.caret_box)
            targets = [rect]
            self.pdf_canvas.create_rectangle(*rect, outline="#ba3e28", width=2,
                                             tags=("cursor-location", "cursor-glyph"))
        if targets and self._cursor_scroll_pending:
            x0, y0, x1, y1 = targets[0]
            canvas_width, canvas_height = self.pdf_canvas.winfo_width(), self.pdf_canvas.winfo_height()
            left, top = self.pdf_canvas.canvasx(0), self.pdf_canvas.canvasy(0)
            screen_y = self._editor_cursor_y(ensure_visible=ensure_editor_visible)
            if screen_y is not None:
                wanted_top = (y0 + y1) / 2 - (screen_y - self.pdf_canvas.winfo_rooty())
                region_top = min(0, wanted_top - 16)
                region_bottom = max(height + 32, wanted_top + canvas_height + 16)
                self.pdf_canvas.configure(scrollregion=(0, region_top, width + 32, region_bottom))
                self.pdf_canvas.yview_moveto((wanted_top - region_top) / (region_bottom - region_top))
            elif ensure_editor_visible and (y0 < top + 25 or y1 > top + canvas_height - 25):
                self.pdf_canvas.yview_moveto(max(0, ((y0 + y1) / 2 - canvas_height / 2) / (height + 32)))
            if x0 < left + 20 or x1 > left + canvas_width - 20:
                self.pdf_canvas.xview_moveto(max(0, ((x0 + x1) / 2 - canvas_width / 2) / (width + 32)))
            self._cursor_scroll_pending = False

    def _editor_cursor_y(self, *, ensure_visible):
        widget = self._editors.get(self._active)
        if widget is None:
            return None
        box = widget.bbox("insert")
        if box is None:
            return None
        # Tk retains stale root coordinates when a Canvas child is clipped and
        # unmapped. Compute from document coordinates even for off-screen cards.
        world_y = (self.doc_canvas.coords(self._doc_window)[1] + self._cards[self._active].winfo_y()
                   + widget.winfo_y() + box[1] + box[3] / 2)
        y = self.doc_canvas.winfo_rooty() + world_y - self.doc_canvas.canvasy(0)
        top = max(self.doc_canvas.winfo_rooty(), self.pdf_canvas.winfo_rooty()) + 24
        bottom = min(self.doc_canvas.winfo_rooty() + self.doc_canvas.winfo_height(),
                     self.pdf_canvas.winfo_rooty() + self.pdf_canvas.winfo_height()) - 24
        if bottom <= top:
            return None
        if not top <= y <= bottom:
            if not ensure_visible:
                return None
            bounds = self.doc_canvas.bbox("all")
            if not bounds or bounds[3] <= bounds[1]:
                return None
            desired_y = (top + bottom) / 2
            scroll_top = self.doc_canvas.canvasy(0) + y - desired_y
            self.doc_canvas.yview_moveto((scroll_top - bounds[1]) / (bounds[3] - bounds[1]))
            self.update_idletasks()
            y = self.doc_canvas.winfo_rooty() + world_y - self.doc_canvas.canvasy(0)
        return y

    def _save_shortcut(self, _event=None):
        self.save()
        return "break"

    def _confirm_changes(self, changes):
        dialog = ChangeReviewDialog(self, changes, self._baseline, Path(self.session.corrected_path))
        self.wait_window(dialog)
        return dialog.accepted

    def show_correction_history(self):
        if self.busy or self._closed:
            return
        try:
            records = self.session.correction_records()
        except (OSError, ValueError) as exc:
            messagebox.showerror("수정 기록을 열지 못했습니다", str(exc), parent=self)
            return
        CorrectionHistoryDialog(self, records)

    def save(self) -> bool:
        if self.busy or self._closed:
            return False
        if not self._prepare_paragraph_input():
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
            if self._replacement_plan is None:
                result = self.session.save(changes)
            else:
                result = self.session.save(changes, replacement_plan=self._replacement_plan)
        except (OSError, ValueError) as exc:
            if getattr(exc, "paragraph_index", None) is not None:
                self._reveal_text(exc.paragraph_index, getattr(exc, "offset", None) or 0)
            messagebox.showerror("교정본을 저장하지 못했습니다", f"{exc}\n\n입력한 수정은 이 창에 남아 있습니다.", parent=self)
            return False
        finally:
            self._saving = False
            self.configure(cursor="")
            self._update_status()
        self._paragraphs = {p.index: p for p in self.session.paragraphs}
        self._snapshot_generation += 1
        self._baseline = {p.index: p.corrected_text for p in self.session.paragraphs}
        self._external_changes.clear()
        self._replacement_plan = None
        self._replace_undo.clear()
        if self._find_dialog is not None and self._find_dialog.winfo_exists():
            self._find_dialog._invalidate()
            self._find_dialog.undo_button.configure(state="disabled")
        for editor in self._editors.values():
            editor.edit_modified(False)
        self._update_status()
        notice = f"저장됨 · 문단 {result.saved_count}개"
        if getattr(result, "learning_warning", ""):
            notice += f" · 수정 사례 참고 보류: {result.learning_warning}"
        elif getattr(result, "learning_path", None):
            notice += f" · 수정 기록 보관 · 다음 새 판독 참고 {result.learning_count}건"
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
        self._snapshot_generation += 1
        self._paragraphs = {p.index: p for p in session.paragraphs}
        self._baseline = {p.index: p.corrected_text for p in session.paragraphs}
        self._external_changes.clear()
        self._replacement_plan = None
        self._replace_undo.clear()
        if self._find_dialog is not None and self._find_dialog.winfo_exists():
            self._find_dialog._invalidate()
            self._find_dialog.undo_button.configure(state="disabled")
        self._editors.clear(); self._cards.clear(); self._markers.clear()
        self._active = None
        self._build_paragraphs()
        if previous in self._paragraphs:
            self.select_paragraph(previous)
        self._update_status()
        self.v_status.set("교정본을 다시 불러왔습니다.")
        self._start_cursor_map()
        self._request_render()
        return True

    def _cleanup(self):
        if self._closed:
            return
        self._closed = True
        for job in (self._initial_job, self._render_job, self._poll_job, self._layout_job, self._cursor_job,
                    self._position_job, *self._change_jobs.values()):
            if job is not None:
                try:
                    self.after_cancel(job)
                except tk.TclError:
                    pass
        if self._render_future is not None:
            self._render_future[1].cancel()
        for pending in (self._index_future, self._alignment_future, self._cursor_future):
            if pending is not None:
                pending[1].cancel()
        # The PDF worker belongs to all windows. Closing this one must not
        # interrupt another window's rendering or create a competing worker.
        self._alignment_executor.shutdown(wait=False, cancel_futures=True)
        self._photo = None
        # Explicitly release Tcl-owned objects on the UI thread, even if a
        # background allocation later collects a closed window's Python cycle.
        self._editor_font = None
        for name in ("v_status", "v_compare", "v_page", "v_zoom", "v_follow_cursor", "v_match_pdf_font"):
            setattr(self, name, None)

    def _destroyed(self, event):
        if event.widget is self:
            self._cleanup()

    def destroy(self):
        self._cleanup()
        super().destroy()
