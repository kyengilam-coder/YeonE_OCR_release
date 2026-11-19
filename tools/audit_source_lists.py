"""Replay production list layout on a copy of an existing OCR HWPX.

Usage: python tools/audit_source_lists.py source.pdf before.hwpx output.hwpx
No AI calls. Input files are never overwritten. Source OCR glyphs/colors must
remain identical; only whitespace/leaders, paragraph joins/order/layout change.
"""
import argparse
from collections import Counter
from pathlib import Path
import sys
from types import SimpleNamespace
from zipfile import ZipFile
from lxml import etree

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from yeone_ocr.collate.hwpx_edit import HP, paragraph_text, _slots
from yeone_ocr.source_lists import pdf_lists, apply_source_lists
from yeone_ocr.toc_tabs import align_toc_tabs
from yeone_ocr.list_review import write_list_review


def glyphs(sections):
    counts=Counter()
    for section in sections:
        for p in section.element.findall(HP+'p'):
            for slot in _slots(p):
                for char in slot.text:
                    if not char.isspace() and char not in '.…⋯·．':
                        counts[char,slot.run.get('charPrIDRef')]+=1
    return counts


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('pdf',type=Path);parser.add_argument('source',type=Path);parser.add_argument('output',type=Path)
    args=parser.parse_args()
    if args.output.resolve() in (args.source.resolve(),args.pdf.resolve()) or args.output.exists():
        parser.error('Output must be a new file, distinct from the inputs.')
    with ZipFile(args.source) as archive:
        infos=archive.infolist();parts={i.filename:archive.read(i.filename) for i in infos}
    header=SimpleNamespace(element=etree.fromstring(parts['Contents/header.xml']),mark_dirty=lambda:None)
    names=sorted(n for n in parts if n.startswith('Contents/section') and n.endswith('.xml'))
    sections=[SimpleNamespace(element=etree.fromstring(parts[n]),mark_dirty=lambda:None) for n in names]
    doc=SimpleNamespace(oxml=SimpleNamespace(headers=[header]),sections=sections)
    before=glyphs(sections)
    evidence=pdf_lists(args.pdf)
    stats=apply_source_lists(doc,evidence);stats.update(align_toc_tabs(doc,evidence))
    assert glyphs(sections)==before,'OCR characters or character styles changed'
    stats['glyphs_and_colors_preserved']=True
    for name,node in [('Contents/header.xml',header.element)]+[(n,s.element) for n,s in zip(names,sections)]:
        parts[name]=etree.tostring(node,encoding='UTF-8',xml_declaration=True)
    args.output.parent.mkdir(parents=True,exist_ok=True)
    with ZipFile(args.output,'w') as archive:
        for info in infos:archive.writestr(info,parts[info.filename])
    from hwpx import HwpxDocument
    result=HwpxDocument.open(args.output)
    try:assert not list(result.validate().issues)
    finally:result.close()
    report,_=write_list_review(args.output,stats)
    print({k:v for k,v in stats.items() if not isinstance(v,list)})
    print('Review:',report)


if __name__=='__main__':main()
