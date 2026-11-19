"""Explicit selection of a preserved OCR source and rereading scope."""
from pathlib import Path
import time
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import uuid


def open_dialog(app):
    from .gui import App, template_ok, preset_ok
    from .recheck import inspect
    from .state import INTERNAL_DIR, public_root
    if app.running or not app._need_pdf():
        return
    if not App._result_editors_ready(app, app.pdf()):
        return
    pdf = app.pdf()
    window = tk.Toplevel(app)
    window.title('이 책 · 기존 OCR 재검수·재판독')
    window.transient(app)
    window.grab_set()
    frame = ttk.Frame(window, padding=20); frame.pack(fill='both', expand=True)
    ttk.Label(frame, text=Path(pdf).name).pack(anchor='w')
    ttk.Label(frame, text='기존 OCR·교정 이력은 보존하고 새 폴더에서 독립 판독합니다.\n'
              '새 결과는 교정 후보입니다. 완료 후 이전·새 본문을 원본과 대조하세요.', wraplength=640).pack(anchor='w', pady=10)
    source = tk.StringVar(value=str(app.selected_result_work()))
    ttk.Label(frame, text='기존 OCR 작업자료 또는 결과 폴더').pack(anchor='w')
    row = ttk.Frame(frame); row.pack(fill='x')
    ttk.Entry(row, textvariable=source, width=65).pack(side='left', fill='x', expand=True)
    def browse():
        path = filedialog.askdirectory(parent=window, title='보존할 기존 OCR 작업 폴더')
        if path:
            source.set(path)
    ttk.Button(row, text='고르기…', command=browse).pack(side='left', padx=6)
    mode = tk.StringVar(value='suspect')
    ttk.Radiobutton(frame, text='의심 부분 우선 · 의심 표시·확인 항목·미완료 쪽을 다시 읽기',
                    variable=mode, value='suspect').pack(anchor='w', pady=(12, 4))
    ttk.Radiobutton(frame, text='전체 재판독 · 책의 모든 쪽을 처음부터 다시 읽기',
                    variable=mode, value='all').pack(anchor='w')
    ttk.Label(frame, text='현재 판독기·AI·출력 설정을 사용합니다. AI 사용 시 구독 사용량 또는 API 비용이 듭니다.\n'
              '기존 글자별 의심 위치만이 아니라 해당 쪽 전체를 다시 읽습니다.', wraplength=640).pack(anchor='w', pady=12)
    def run():
        if app.running or app.pdf() != pdf:
            return
        try:
            work, records, selected = inspect(pdf, source.get(), mode.get())
        except Exception as exc:
            messagebox.showerror(window.title(), str(exc), parent=window); return
        if not selected:
            messagebox.showinfo(window.title(), '의심·미완료 쪽이 없습니다. 필요하면 전체 재판독을 선택하십시오.', parent=window); return
        root = public_root(work.dir).parent / ('재판독-' + time.strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:6])
        if not messagebox.askyesno(window.title(),
                f'전체 {len(records)}쪽 중 {len(selected)}쪽을 새로 판독합니다.\n'
                f'새 결과: {root}\n\n현재 AI 설정에 따른 사용량·비용이 발생할 수 있습니다. 실행하시겠습니까?', parent=window):
            return
        if App._local_first(app):
            app._env_optional_key = True
        if not app._prepare_env() or not template_ok(app) or not preset_ok(app):
            return
        argv = (['run', pdf, '--work', str(root / INTERNAL_DIR), '--recheck-from', str(work.dir),
                 '--recheck-mode', mode.get()] + app._read_opts(force=True) + app._glyph_opts(suspects=True)
                + app._answered_opts() + app._build_opts() + App._strategy_opts(app))
        window.destroy()
        app.start(argv, '기존 OCR 재검수·재판독 · 원본 이력 보존', open_after=root)
    ttk.Button(frame, text='대상 확인 후 새 폴더에서 실행…', command=run).pack(anchor='e')
