"""Book-specific template style mapping, saved as a reusable book rule."""
import json
import tempfile
from pathlib import Path
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from zipfile import ZipFile, BadZipFile
from xml.etree import ElementTree as ET

from .presets import resolve_preset, load_preset, PresetError
from .rule_library import save_rule, rules_dir
from .state import write_atomic
from .ziplimits import check_archive, read_member

HH='{http://www.hancom.co.kr/hwpml/2011/head}'
LABELS={'chapter':'책·권·장 제목','section':'서문·절·목차 제목','subsection':'소분류 제목',
        'entry':'조문 약재명','body':'본문','citation_source':'인용 출전','quotation':'인용문','small':'소주'}


class BookStylesDialog(tk.Toplevel):
    def __init__(self,app):
        self.rule=resolve_preset(app.preset_ref())
        self.paragraph_styles={**resolve_preset('regions').para_styles, **self.rule.para_styles}
        super().__init__(app);self.app=app
        self.title('이 책 · 양식 스타일 설정');self.transient(app)
        P=app.px;self.geometry(f'{P(780)}x{P(680)}')
        frame=ttk.Frame(self,padding=P(16));frame.pack(fill='both',expand=True)
        ttk.Label(frame,text='책의 제목과 문단에 적용할 양식 스타일을 고릅니다.',font=app.fonts['BODY_B']).pack(anchor='w')
        ttk.Label(frame,text='약재 제목 아래의 소분류 제목도 별도 스타일로 연결합니다.\n'
                  '원본에 없는 제목은 만들지 않으며, 원본 양식과 OCR·교정 기록은 바꾸지 않습니다.',wraplength=P(720)).pack(anchor='w',pady=P(8))
        self.path=tk.StringVar(value=app.v_template.get().strip() or self.rule.template)
        line=ttk.Frame(frame);line.pack(fill='x')
        ttk.Entry(line,textvariable=self.path).pack(side='left',fill='x',expand=True)
        ttk.Button(line,text='양식 고르기…',command=self.choose).pack(side='left',padx=P(6))
        table=ttk.Frame(frame);table.pack(fill='both',expand=True,pady=P(12));table.columnconfigure(1,weight=1)
        self.variables={};self.combos={};self.details={}
        for i,(role,label) in enumerate(LABELS.items()):
            ttk.Label(table,text=label).grid(row=2*i,column=0,sticky='w',padx=(0,P(12)))
            v=self.variables[role]=tk.StringVar();combo=self.combos[role]=ttk.Combobox(table,textvariable=v,state='readonly')
            combo.grid(row=2*i,column=1,sticky='ew');combo.bind('<<ComboboxSelected>>',lambda e:self.describe())
            detail=self.details[role]=tk.StringVar()
            ttk.Label(table,textvariable=detail,wraplength=P(540)).grid(row=2*i+1,column=1,sticky='w',pady=(0,P(5)))
        ttk.Label(frame,text='원본에 맞춘 위치·크기·쪽 나눔은 ‘이 책 설정’의 HWPX 규칙을 따릅니다.',wraplength=P(720)).pack(anchor='w')
        buttons=ttk.Frame(frame);buttons.pack(fill='x',pady=(P(12),0))
        ttk.Button(buttons,text='책별 규칙으로 저장하고 적용…',command=self.save).pack(side='right')
        ttk.Button(buttons,text='취소',command=self.destroy).pack(side='right',padx=P(8))
        self.styles={};self.loaded_path=''
        if self.path.get():self.load()

    def choose(self):
        path=filedialog.askopenfilename(parent=self,title='이 책의 양식 HWPX',filetypes=[('HWPX','*.hwpx')])
        if path:self.path.set(path);self.load()

    def load(self):
        try:
            with ZipFile(self.path.get()) as z:
                check_archive(z);header=ET.fromstring(read_member(z,'Contents/header.xml'))
            chars={s.get('id'):s for s in header.iter(HH+'charPr')}
            fonts={f.get('lang','').lower():{n.get('id'):n.get('face') for n in f.findall(HH+'font')} for f in header.iter(HH+'fontface')}
            self.styles={}
            for s in header.iter(HH+'style'):
                if s.get('type')!='PARA':continue
                c=chars.get(s.get('charPrIDRef'));detail=''
                if c is not None:
                    ref=c.find(HH+'fontRef');faces=[]
                    if ref is not None:
                        faces=list(dict.fromkeys(fonts.get(k,{}).get(v,'') for k,v in ref.attrib.items()))
                    detail=f"{' / '.join(f for f in faces if f)} · {int(c.get('height','1000'))/100:g}pt · #{c.get('textColor','000000').lstrip('#')}"
                self.styles[s.get('name')]=detail
            for role,combo in self.combos.items():
                combo.configure(values=list(self.styles))
                selected=next((n for n in self.paragraph_styles.get(role,()) if n in self.styles),'바탕글')
                self.variables[role].set(selected if selected in self.styles else next(iter(self.styles),''))
            self.describe()
            self.loaded_path=self.path.get()
        except (OSError,ValueError,KeyError,BadZipFile,ET.ParseError) as exc:
            self.styles={};messagebox.showerror('양식 스타일',f'양식을 읽을 수 없습니다.\n{exc}',parent=self)

    def describe(self):
        for role,v in self.variables.items():self.details[role].set(self.styles.get(v.get(),''))

    def save(self):
        if self.path.get()!=self.loaded_path:
            self.load()
            return  # Review this newly loaded template before saving.
        if not self.styles or any(v.get() not in self.styles for v in self.variables.values()):
            messagebox.showerror('양식 스타일','양식을 고르고 각 역할의 스타일을 선택하십시오.',parent=self);return
        directory=rules_dir();directory.mkdir(parents=True,exist_ok=True)
        destination=filedialog.asksaveasfilename(parent=self,title='이 책의 스타일 규칙 저장',initialdir=str(directory),
                    initialfile=self.rule.label+'_스타일.json',defaultextension='.json',filetypes=[('책별 규칙 JSON','*.json')])
        if not destination:return
        temporary=None
        try:
            target=Path(destination)
            if target.suffix.lower()!='.json':raise ValueError('규칙은 JSON 파일로 저장하십시오.')
            if target.exists():load_preset(target)  # Never overwrite configuration or OCR projects.
            with tempfile.NamedTemporaryFile(dir=target.parent,suffix='.json',delete=False) as f:temporary=Path(f.name)
            save_rule(self.rule.ref,temporary,template=self.path.get(),label=target.stem)
            data=json.loads(temporary.read_text(encoding='utf-8-sig'))
            mapping=data.setdefault('styles',{}).setdefault('paragraph',{})
            mapping.update({role:[v.get()] for role,v in self.variables.items()})
            temporary.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
            load_preset(temporary);write_atomic(target,temporary.read_bytes())
            self.app.use_book_rule(str(target.resolve()))
            self.destroy()
        except (OSError,ValueError,PresetError) as exc:
            messagebox.showerror('양식 스타일',f'규칙을 저장하지 못했습니다.\n{exc}',parent=self)
        finally:
            if temporary is not None:temporary.unlink(missing_ok=True)
