"""Recover a damaged contents page only with independent name/page anchors."""
import re
from .collate.zh import script_fold

ENTRY=re.compile(r'([\u3400-\u9fff、，,]{1,50})[·.…．]+[（(](\d{1,4})[）)]')


def catalog_entries(text):
    compact=re.sub(r'\s','',text)
    return compact,list(ENTRY.finditer(compact))


def recover_catalog(answer,witnesses):
    """Return None unless a clean AI contents page agrees with local OCR.

    Reading order may differ in a two-column contents page; compare complete
    (name, printed page) pairs, never a naked page number or a name alone.
    """
    if not isinstance(answer,dict) or not isinstance(answer.get('text'),str) or not isinstance(answer.get('uncertain',[]),list):return None
    text,entries=catalog_entries(answer['text'])
    if len(entries)<15 or sum(m.end()-m.start() for m in entries)<len(text)*.90:return None
    key=lambda m:(script_fold(re.sub(r'[、，,]','',m[1])),m[2])
    keys={key(m) for m in entries}
    if len(keys)!=len(entries):return None
    best=set()
    for witness in witnesses:
        _,items=catalog_entries(witness)
        other={key(m) for m in items};same=keys&other
        if len(same)>=12 and len(same)>=len(keys)*.80 and len(same)>=len(other)*.80 and len(same)>len(best):best=same
    if not best:return None
    uncertain=tuple(script_fold(re.sub(r'\s','',u)) for u in answer.get('uncertain',()) if isinstance(u,str) and u.strip())
    lines=[];pending=[];offset=0
    for m in entries:
        value=f'{m[1]}……({m[2]})'
        if key(m) not in best or any(u in script_fold(m[1]) for u in uncertain):pending.append((offset,offset+len(m[1])))
        lines.append(value);offset+=len(value)+1
    return '\n'.join(lines),pending,{'items':len(entries),'independently_matched':len(best),'pending_items':len(pending),'policy':'catalog-name-and-page-crosscheck-v1'}
