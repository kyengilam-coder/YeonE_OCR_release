"""Document actions offered after a book has been selected."""
import tkinter as tk
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk

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


def gemini_app_menu(app, button):
    if app.running or vars(app).get("_planning") or not app._need_pdf():
        return
    menu = tk.Menu(app, tearoff=False)
    menu.add_command(label="남은 쪽을 Gemini 앱용 PDF로 내보내기…", command=lambda: _gemini_export(app))
    menu.add_separator()
    menu.add_command(label="복사한 Gemini 판독 가져오기", command=lambda: _gemini_import(app, clipboard=True))
    menu.add_command(label="Gemini 판독 파일 가져오기…", command=lambda: _gemini_import(app))
    _show(app, menu, button)


def _gemini_task(app, caption, operation, *args):
    """Keep the window responsive while the selected book is locked by the modal dialog."""
    window = tk.Toplevel(app)
    window.title("Gemini 앱 판독")
    window.transient(app)
    window.protocol("WM_DELETE_WINDOW", lambda: None)
    ttk.Label(window, text=caption).pack(padx=24, pady=(18, 8))
    bar = ttk.Progressbar(window, mode="indeterminate")
    bar.pack(fill="x", padx=24, pady=(0, 18))
    bar.start()
    window.grab_set()
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(operation, *args)

        def check():
            if future.done():
                bar.stop()
                window.destroy()
            else:
                window.after(75, check)

        window.after(75, check)
        app.wait_window(window)
        return future.result()


def _gemini_export(app):
    from .gemini_app_exchange import export_batch
    from .gui import APP_NAME, open_path
    from .gui_run_start import recorded_pages
    from .state import Work, target_pages

    work = Work(app.pdf(), app.work(), initialize=False)
    scope = app.v_pages.get().strip()
    try:
        existing = set(recorded_pages(work.dir))
        remaining = [n for n in target_pages(app.pdf(), scope) if n not in existing]
        if not remaining:
            messagebox.showinfo(APP_NAME, "현재 쪽 범위에 판독 기록이 없는 쪽이 없습니다.", parent=app)
            return
        selection = simpledialog.askstring(
            "Gemini 앱에 보낼 원본 쪽",
            f"현재 범위에 판독 기록이 없는 쪽이 {len(remaining):,}개 있습니다.\n"
            "보낼 원본 쪽 번호를 입력하세요(예: 175-178, 185).\n"
            "한 번에 몇 쪽씩 보내면 답변이 중간에 잘릴 가능성이 줄어듭니다.",
            initialvalue=", ".join(map(str, remaining[:5])), parent=app)
        if selection is None or not selection.strip():
            return
        folder = filedialog.askdirectory(parent=app, title="Gemini 앱에 보낼 파일을 저장할 폴더 선택")
        if not folder:
            return
        path, pages, skipped = _gemini_task(app, "Gemini 앱에 보낼 PDF를 만드는 중입니다…",
                                            export_batch, work, Path(folder), scope, selection)
    except (OSError, ValueError, RuntimeError) as exc:
        messagebox.showerror(APP_NAME, f"Gemini 앱용 파일을 내보내지 못했습니다.\n{exc}", parent=app)
        return
    info = (f"원본 {len(pages):,}쪽을 내보냈습니다.\n"
            "Gemini 앱에 [Gemini-업로드.pdf]를 첨부하고 [Gemini-요청문.txt]의 내용을 붙여 넣으세요.\n"
            "답변의 JSON 전체를 복사한 뒤 [복사한 Gemini 판독 가져오기]를 누르세요.")
    if skipped:
        info += f"\n기존 판독이 있거나 현재 쪽 범위 밖인 {len(skipped):,}쪽은 건너뛰었습니다."
    if messagebox.askyesno(APP_NAME, f"{info}\n\n파일을 저장한 폴더를 열까요?", parent=app):
        try:
            open_path(path)
        except OSError:
            messagebox.showinfo(APP_NAME, f"파일을 저장한 폴더\n{path}", parent=app)


def _gemini_import(app, *, clipboard=False):
    from .gemini_app_exchange import import_batch
    from .gui import APP_NAME
    from .state import Work

    if not app._result_editors_ready(app.pdf()):
        return
    try:
        if clipboard:
            response = app.clipboard_get()
        else:
            path = filedialog.askopenfilename(parent=app, title="Gemini 판독 JSON 또는 텍스트 파일",
                                              filetypes=[("Gemini 판독", "*.json *.txt"), ("모든 파일", "*.*")])
            if not path:
                return
            response = Path(path).read_text(encoding="utf-8-sig")
        work = Work(app.pdf(), app.work(), initialize=False)
        added, skipped, missing = _gemini_task(app, "Gemini 판독을 기본 OCR 작업에 합치는 중입니다…",
                                               import_batch, work, response)
    except (OSError, ValueError, RuntimeError, tk.TclError) as exc:
        messagebox.showerror(APP_NAME, f"Gemini 판독을 가져오지 못했습니다.\n{exc}", parent=app)
        return
    finally:
        app._request_counts(refresh=True)
        app.refresh_table()
    info = f"기본 OCR 작업에 {len(added):,}쪽을 추가했습니다."
    if added:
        info += "\n가져온 판독은 검수 화면에서 원본과 대조할 수 있습니다."
    if skipped:
        info += f"\n이미 기록이 있는 {len(skipped):,}쪽은 건너뛰었습니다."
    if missing:
        info += f"\n이번 답변에 빠진 {len(missing):,}쪽은 결과가 나오면 따로 가져올 수 있습니다."
    messagebox.showinfo(APP_NAME, info, parent=app)


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
