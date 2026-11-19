"""Original images and proposed text changes for a completed rereading."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import tkinter as tk
from tkinter import messagebox, ttk

from .recheck_review import Review, spans


def open_review(app, work_dir):
    if app.running or not app._result_editors_ready(app.pdf()):
        return
    RecheckReviewWindow(app, app.pdf(), work_dir)


class RecheckReviewWindow(tk.Toplevel):
    def __init__(self, app, pdf, work_dir):
        super().__init__(app)
        self.app = app
        self.pdf = pdf
        self.session = None
        self.saving = False
        self.dirty = False
        self.page_shown = None
        self.executor = ThreadPoolExecutor(max_workers=1)
        self.title('재판독 변경 검토')
        self.transient(app)
        self.geometry(f'{min(1060, self.winfo_screenwidth()-40)}x{min(700, self.winfo_screenheight()-105)}')
        self.protocol('WM_DELETE_WINDOW', self.close)
        self.grab_set()
        frame = ttk.Frame(self, padding=12)
        frame.pack(fill='both', expand=True)
        self.message = tk.StringVar(value='판독문과 교정 이력을 대조하고 있습니다…')
        ttk.Label(frame, textvariable=self.message).pack(fill='x', pady=(0, 8))
        controls = ttk.Frame(frame)
        controls.pack(side='bottom', fill='x', pady=(8, 0))
        self.action_buttons = []
        for label, command in (('기존 유지', lambda: self.choose(False)), ('변경 채택', lambda: self.choose(True)),
                               ('남은 항목 기존 유지', self.keep_remaining), ('검토 저장', self.save),
                               ('확정 HWPX 만들기', self.export)):
            button = ttk.Button(controls, text=label, command=command, state='disabled')
            button.pack(side='left', padx=(0, 5))
            self.action_buttons.append(button)
        parts = ttk.Panedwindow(frame, orient='horizontal')
        parts.pack(fill='both', expand=True)
        source = ttk.Frame(parts)
        self.image_label = ttk.Label(source)
        self.image_label.pack(fill='both', expand=True)
        ttk.Button(source, text='원본 쪽 확대…', command=self.enlarge).pack(anchor='center')
        parts.add(source, weight=2)
        details = ttk.Frame(parts)
        parts.add(details, weight=3)
        box = ttk.Frame(details)
        box.pack(fill='both', expand=True)
        self.listing = tk.Listbox(box, exportselection=False, height=7, width=36)
        self.listing.pack(side='left', fill='both', expand=True)
        scroll = ttk.Scrollbar(box, command=self.listing.yview)
        scroll.pack(side='right', fill='y')
        self.listing.configure(yscrollcommand=scroll.set)
        self.listing.bind('<<ListboxSelect>>', lambda event: self.show())
        self.blocks = []
        for label in ('기존 교정문', '새 판독'):
            ttk.Label(details, text=label).pack(anchor='w', pady=(6, 1))
            panel = ttk.Frame(details)
            panel.pack(fill='both', expand=True)
            view = tk.Text(panel, height=4, width=35, wrap='word', state='disabled')
            view.tag_configure('change', background='#ffec9e', foreground='#201a00')
            scroll = ttk.Scrollbar(panel, command=view.yview)
            scroll.pack(side='right', fill='y')
            view.configure(yscrollcommand=scroll.set)
            view.pack(fill='both', expand=True)
            self.blocks.append(view)
        self.wait_for(self.executor.submit(Review, pdf, work_dir), self.ready)

    def wait_for(self, future, callback):
        if not future.done():
            self.after(80, self.wait_for, future, callback)
            return
        try:
            callback(future.result())
        except Exception as exc:
            self.saving = False
            messagebox.showerror(self.title(), str(exc), parent=self)
            if self.session:
                self.update_status()
            else:
                self.close()

    def ready(self, session):
        self.session = session
        self.labels = tk.Variable(value=tuple(self.label(i) for i in range(len(session.changes))))
        self.listing.configure(listvariable=self.labels)
        if session.changes:
            self.select(next((i for i in range(len(session.changes)) if i not in session.decisions), 0))
        self.update_status()
        if session.stale:
            messagebox.showinfo(self.title(), '이전 검토 후 판독문 또는 교정 내용이 바뀌어 새로 대조합니다.', parent=self)

    def label(self, i):
        change = self.session.changes[i]
        decision = self.session.decisions.get(i)
        status = '미검토' if decision is None else ('채택' if decision else '기존')
        return f'{status}   PDF {change.page}쪽 · {change.location}'

    def update_status(self):
        total = len(self.session.changes)
        reviewed = len(self.session.decisions)
        self.message.set(f'검토 {reviewed}/{total}건 · 채택 {sum(self.session.decisions.values())}건')
        for button in self.action_buttons:
            button.configure(state='normal' if total else 'disabled')
        self.action_buttons[-1].configure(state='normal' if total == reviewed else 'disabled')

    def select(self, i):
        self.listing.selection_clear(0, 'end')
        self.listing.selection_set(i)
        self.listing.see(i)
        self.show()

    def show(self):
        selected = self.listing.curselection()
        if not selected:
            return
        change = self.session.changes[selected[0]]
        edits = spans(change.before, change.after) if change.path is None else [(*change.left, *change.right)]
        for k, (view, value) in enumerate(zip(self.blocks, (change.before, change.after))):
            view.configure(state='normal')
            view.delete('1.0', 'end')
            view.insert('1.0', value or '(없음)')
            for edit in edits:
                a, b = edit[2*k:2*k+2]
                if b > a:
                    view.tag_add('change', f'1.0+{a}c', f'1.0+{b}c')
            if edits:
                view.see(f'1.0+{edits[0][2*k]}c')
            view.configure(state='disabled')
        if self.page_shown != change.page:
            from PIL import ImageTk
            from .gui_preview import preview_image
            try:
                self.thumbnail = ImageTk.PhotoImage(preview_image(str(self.pdf), (395, 520), change.page - 1), master=self)
                self.image_label.configure(image=self.thumbnail, text='')
            except Exception as exc:
                self.image_label.configure(image='', text=f'원본 쪽을 열 수 없습니다. {exc}')
            self.page_shown = change.page

    def enlarge(self):
        if self.page_shown is None:
            return
        from PIL import ImageTk
        from .gui_preview import preview_image
        view = tk.Toplevel(self)
        view.title(f'원본 PDF {self.page_shown}쪽')
        canvas = tk.Canvas(view, width=min(950, self.winfo_screenwidth()-80),
                           height=min(680, self.winfo_screenheight()-140))
        scroll = ttk.Scrollbar(view, command=canvas.yview)
        scroll.pack(side='right', fill='y')
        horizontal = ttk.Scrollbar(view, orient='horizontal', command=canvas.xview)
        horizontal.pack(side='bottom', fill='x')
        canvas.configure(yscrollcommand=scroll.set, xscrollcommand=horizontal.set)
        canvas.pack(fill='both', expand=True)
        try:
            view.image = ImageTk.PhotoImage(preview_image(str(self.pdf), (1700, 2200), self.page_shown-1), master=view)
            canvas.create_image(0, 0, anchor='nw', image=view.image)
            canvas.configure(scrollregion=canvas.bbox('all'))
            view.transient(self)
            view.grab_set()
            def close_image():
                view.destroy()
                self.grab_set()
            view.protocol('WM_DELETE_WINDOW', close_image)
        except Exception as exc:
            view.destroy()
            messagebox.showerror(self.title(), str(exc), parent=self)

    def choose(self, accepted):
        selection = self.listing.curselection()
        if not selection:
            return
        i = selection[0]
        self.session.decisions[i] = accepted
        self.dirty = True
        self.listing.delete(i)
        self.listing.insert(i, self.label(i))
        self.select(min(i + 1, self.listing.size() - 1))
        self.update_status()

    def keep_remaining(self):
        session = self.session
        remaining = len(session.changes) - len(session.decisions)
        if remaining and messagebox.askyesno(self.title(), f'미검토 {remaining}건의 기존 교정문을 유지할까요?', parent=self):
            for i in range(len(session.changes)):
                session.decisions.setdefault(i, False)
            self.dirty = True
            self.labels.set(tuple(self.label(i) for i in range(len(session.changes))))
            self.update_status()

    def save(self):
        try:
            self.session.save()
            self.dirty = False
            self.message.set('검토 선택을 저장했습니다.')
            return True
        except (OSError, ValueError) as exc:
            messagebox.showerror(self.title(), str(exc), parent=self)
            return False

    def export(self):
        if self.app.running or self.pdf != self.app.pdf():
            messagebox.showinfo(self.title(), '이 책을 선택하고 진행 중인 작업이 끝난 뒤 만들어 주세요.', parent=self)
            return
        options = self.app._build_opts()
        self.saving = True
        for button in self.action_buttons:
            button.configure(state='disabled')
        self.message.set('선택한 문단과 출력 양식으로 새 확정본을 만드는 중입니다…')
        self.wait_for(self.executor.submit(self.session.export, options), self.finished)

    def finished(self, output):
        directory, path = output
        self.dirty = self.saving = False
        self.app.select_output_file(directory, path, source=self.pdf)
        messagebox.showinfo(self.title(), f'확정 HWPX를 만들었습니다.\n{path}', parent=self)
        self.close()

    def close(self):
        if self.saving:
            return
        if self.dirty:
            answer = messagebox.askyesnocancel(self.title(), '변경한 검토 선택을 저장할까요?', parent=self)
            if answer is None or (answer and not self.save()):
                return
        self.executor.shutdown(wait=False, cancel_futures=True)
        self.destroy()
