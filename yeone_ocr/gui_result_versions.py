"""Result chooser shared by the output card, document editor and review actions."""
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from . import result_versions as rv
from .state import INTERNAL_DIR, Work, public_root, work_dir_in


class ResultNavigation:
    def selected_result(self):
        return rv.versions(vars(self).get('cfg', {}), self.pdf())[1] if self.pdf() else None

    def selected_result_work(self):
        """Read path for the result on display. Normal OCR uses App.work()."""
        version = ResultNavigation.selected_result(self)
        return Path(version.work) if version else self.work()

    def _result_file(self, suffix):
        version = ResultNavigation.selected_result(self)
        if version:
            return version.file(suffix) if version.valid_source(self.pdf()) else None
        work = self.work()
        if (work / 'source.json').is_file() and not Work(self.pdf(), work, initialize=False).source_matches():
            return None
        return rv.latest_document(self.pdf(), work, suffix)

    def _store_result(self, source, version):
        from .gui import write_config
        same_book = bool(self.pdf()) and Path(self.pdf()).resolve() == Path(source).resolve()
        if same_book and self.selected_result() != version:
            dialog = vars(self).get('_rule_generation_dialog')
            if dialog is not None and dialog.winfo_exists():
                dialog.close()
        rv.remember(self.cfg, source, version)
        try:
            write_config(self)
        except OSError as exc:
            self._append(f'결과 선택을 다음 실행까지 저장하지 못했습니다. {exc}\n')
        if same_book:
            self._sync_output_location()

    def select_output_file(self, work_dir, path, *, label='확정본', source=None, editor_source=None):
        """Select a reviewed HWPX after a writer has successfully closed it."""
        path = Path(path).resolve(); work = Path(work_dir).resolve()
        source = source or self.pdf()
        if path.parent != public_root(work) / 'out' or path.suffix.lower() != '.hwpx' or not path.is_file():
            raise ValueError('확정본이 선택한 결과 폴더의 HWPX인지 확인하십시오.')
        version = rv.ResultVersion(str(work), label, path.name, editor_source=Path(editor_source).name if editor_source else '')
        if not version.valid_source(source):
            raise ValueError('선택한 책과 문서의 원본이 일치하지 않습니다.')
        self._store_result(source, version)

    def _build_result_selector(self, parent):
        row = ttk.Frame(parent)
        row.grid(row=5, column=0, columnspan=2, sticky='ew', pady=(self.px(8), 0))
        ttk.Label(row, text='현재 결과').pack(side='left')
        self.v_result_version = tk.StringVar(value='기본 작업')
        self.result_select = ttk.Menubutton(row, textvariable=self.v_result_version, width=1)
        self.buttons.append(self.result_select)
        self.result_select.pack(side='left', fill='x', expand=True, padx=(self.px(6), 0))
        self._result_menu = tk.Menu(self.result_select, tearoff=False, postcommand=self._fill_result_menu)
        self.result_select.configure(menu=self._result_menu)

    def _fill_result_menu(self):
        menu = self._result_menu
        menu.delete(0, 'end')
        source = self.pdf()
        if not source:
            menu.add_command(label='원본을 선택하세요', state='disabled')
            return
        entries, current = rv.versions(self.cfg, source)
        menu.add_command(label=('✓ ' if current is None else '') + '기본 작업의 최신 결과',
                         command=lambda: self._store_result(source, None))
        def select(version):
            if not version.valid_source(source) or not (version.file('.hwpx') or version.file('.pdf')):
                messagebox.showerror('결과 선택', '선택한 문서 파일과 책의 원본 정보를 확인하십시오.', parent=self)
                return
            self._store_result(source, version)
        for version in entries:
            text = f'{version.label} · {version.hwpx or version.pdf}'
            menu.add_command(label=('✓ ' if version == current else '') + text,
                             command=lambda version=version: select(version))
        menu.add_separator()
        menu.add_command(label='기존 결과 폴더 연결…', command=self._attach_result_folder)

    def _attach_result_folder(self):
        source = self.pdf()
        selected = filedialog.askdirectory(parent=self, title='이 책의 기존 결과 또는 작업자료 폴더 선택',
                                           initialdir=str(Path(source).parent))
        if not selected:
            return
        directory = Path(selected).resolve()
        work = directory if directory.name == INTERNAL_DIR else work_dir_in(directory)
        version = rv.ResultVersion(str(work), '기존 결과')
        if not version.valid_source(source):
            messagebox.showerror('결과 연결', '선택한 폴더의 OCR과 이 책의 원본이 일치하지 않습니다.', parent=self)
            return
        found = rv.documents(source, work)
        if not found:
            messagebox.showinfo('결과 연결', '이 책의 완성된 HWPX·문자 PDF가 없습니다.', parent=self)
            return
        path = filedialog.askopenfilename(parent=self, initialdir=str(public_root(work) / 'out'),
                                          title='현재 결과로 사용할 문서 선택',
                                          filetypes=[('HWPX · PDF', '*.hwpx *.pdf')])
        if not path:
            return
        path = Path(path).resolve()
        if path not in found:
            messagebox.showerror('결과 연결', '이 책의 결과 폴더에서 완성된 문서를 고르십시오.', parent=self)
            return
        self._store_result(source, rv.ResultVersion(str(work), '기존 결과',
                           hwpx=path.name if path.suffix.lower() == '.hwpx' else '',
                           pdf=path.name if path.suffix.lower() == '.pdf' else ''))
