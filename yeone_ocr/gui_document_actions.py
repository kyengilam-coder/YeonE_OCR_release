"""Document actions offered after a book has been selected."""
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox

from .output_names import next_document_path
from .state import INTERNAL_DIR, public_root, work_dir_in


def _show(app, menu, button):
    previous = vars(app).get('_document_menu')
    if previous is not None:
        previous.destroy()
    app._document_menu = menu
    try:
        menu.tk_popup(button.winfo_rootx(), button.winfo_rooty() + button.winfo_height())
    finally:
        menu.grab_release()


def make_hwpx(app):
    """Start or continue OCR for the basic work, with HWPX output."""
    if app.running or not app._need_pdf():
        return
    app.do_run()


def export_menu(app, button):
    if app.running or not app._need_pdf():
        return
    current = app.selected_result_work()
    menu = tk.Menu(app, tearoff=False)
    state = 'normal' if (current / 'source.json').is_file() else 'disabled'
    menu.add_command(label='현재 결과 → HWPX', state=state,
                     command=lambda: export_records(app, folder=current))
    menu.add_command(label='현재 결과 → 문자 PDF', state=state,
                     command=lambda: export_records(app, pdf_only=True, folder=current))
    basic = app.work()
    if basic != current:
        basic_state = 'normal' if (basic / 'source.json').is_file() else 'disabled'
        menu.add_command(label='기본 OCR 작업 → HWPX', state=basic_state,
                         command=lambda: export_records(app, folder=basic))
        menu.add_command(label='기본 OCR 작업 → 문자 PDF', state=basic_state,
                         command=lambda: export_records(app, pdf_only=True, folder=basic))
    other = tk.Menu(menu, tearoff=False)
    other.add_command(label='HWPX 만들기…', command=app.do_reuse_build)
    other.add_command(label='문자 PDF 만들기…', command=app.do_reuse_textlayer)
    menu.add_cascade(label='다른 OCR 작업 폴더에서 내보내기', menu=other)
    _show(app, menu, button)


def review_menu(app, button):
    if not app._need_pdf():
        return
    menu = tk.Menu(app, tearoff=False)
    recheck = (public_root(app.selected_result_work()) / '재판독_비교.json').is_file()
    if recheck:
        menu.add_command(label='재판독 변경 검토·확정', command=app.open_review)
    menu.add_command(label='원본과 HWPX 나란히 대조·편집', command=app.open_result_editor)
    if not recheck:
        menu.add_command(label='쪽별 판독 표시 보기', command=app.open_review)
    menu.add_command(label='모델별 판독 비교·교정', command=app.open_local_comparison)
    _show(app, menu, button)


def export_records(app, pdf_only=False, folder=None):
    """Format selected readings using the same options as the main HWPX action."""
    from .gui import APP_NAME, App, template_ok, preset_ok
    if app.running or not app._need_pdf() or not App._result_editors_ready(app, app.pdf()):
        return
    if not pdf_only and not App._corrections_ok(app):
        return
    if folder is None:
        selected = filedialog.askdirectory(parent=app, title='기존 OCR 작업자료 또는 결과 폴더 선택',
                                           initialdir=str(Path(app.pdf()).parent))
        if not selected:
            return
        folder = Path(selected)
        if folder.name != INTERNAL_DIR:
            folder = work_dir_in(folder)
    if not (folder / 'source.json').is_file() or not any((folder / 'readings').glob('p*.json')):
        messagebox.showerror(APP_NAME, '선택한 폴더에서 원본 정보와 OCR 기록을 찾을 수 없습니다.', parent=app)
        return
    out_dir = public_root(folder) / 'out'
    if pdf_only:
        out = next_document_path(out_dir, app.pdf(), '.pdf')
        app.start(['textlayer', app.pdf(), '--work', str(folder), '--out', str(out)] + app._pages(),
                  '기존 OCR로 문자 PDF 만들기', open_after=out)
        return
    app._env_need_key = False
    if app._prepare_env() and template_ok(app) and preset_ok(app):
        app.start(['build', app.pdf(), '--work', str(folder), '--reuse-records', '--no-context-model']
                  + app._pages() + app._build_opts(), '기존 OCR로 HWPX 만들기', open_after=out_dir)
