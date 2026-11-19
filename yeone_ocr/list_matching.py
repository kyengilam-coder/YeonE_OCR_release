"""One-to-one list correspondence using labels, page numbers and neighbours.

Only layout evidence is returned. OCR characters are never replaced here.
"""
from difflib import SequenceMatcher

from .collate.hwpx_edit import paragraph_text
from .toc_tabs import list_entry


def match_toc_paragraphs(paragraphs, evidence):
    from .source_lists import key
    rows=sorted((r for r in evidence if r['kind']=='toc'),
                key=lambda r:(r['page'],r.get('segment',0),r.get('column',0),r['y'],r['x']))
    exact={};targets={}
    for j,row in enumerate(rows):
        exact.setdefault(row['key'],[]).append(j)
        targets.setdefault(row.get('target',''),[]).append(j)
    candidates=[]
    for p in paragraphs:
        text=paragraph_text(p);m=list_entry(text)
        if m and m.start()>0 and len(text)<=300 and not any(c in text for c in '〔〕\n'):
            label=key(text[:m.start()]);target=key(m['page'])
            candidates.append((p,label,target))
    matches={};anchors={};used=set()
    # Unique exact matches anchor a source sequence. Duplicate occurrences are
    # deliberately held until their neighbours identify a source location.
    doc_counts={}
    for _,label,target in candidates:doc_counts[label+target]=doc_counts.get(label+target,0)+1
    for i,(p,label,target) in enumerate(candidates):
        hits=exact.get(label+target,[])
        if len(hits)==1 and doc_counts[label+target]==1:
            j=hits[0];matches[p]={'row':rows[j],'method':'exact','score':1.0};anchors[i]=j;used.add(j)
    for i,(p,label,target) in enumerate(candidates):
        if p in matches:continue
        before=next((anchors[k] for k in range(i-1,max(-1,i-9),-1) if k in anchors),None)
        after=next((anchors[k] for k in range(i+1,min(len(candidates),i+9)) if k in anchors),None)
        bounded=before is not None and after is not None and before<after and after-before<=30
        bracketed=(i-1 in anchors and i+1 in anchors and anchors[i+1]-anchors[i-1]==2)
        hits=targets.get(target,[]) or exact.get(label+target,[])
        if bounded:hits=[j for j in hits if before<j<after]
        scores=[]
        for j in hits:
            if j in used:continue
            row=rows[j];other=row.get('label',row['key'][:-len(target)] if target else row['key'])
            score=SequenceMatcher(None,label,other,autojunk=False).ratio()
            threshold=.5 if bracketed and min(len(label),len(other))>=2 else (.70 if bounded and min(len(label),len(other))>=6 else .86)
            if score>=threshold:scores.append((score,j))
        scores.sort(reverse=True)
        if not scores or (len(scores)>1 and scores[0][0]-scores[1][0]<.10):continue
        score,j=scores[0]
        # Short labels cannot be corrected merely because they are adjacent.
        if min(len(label),len(rows[j].get('label','')))<4 and score<1 and not bracketed:continue
        matches[p]={'row':rows[j],'method':'bracketed-label' if bracketed and score<.86 else ('neighbour-label' if bounded else 'unique-label'),'score':round(score,4)}
        used.add(j)
    # Some OCR engines alternate left and right columns. Source-order
    # bracketing is then impossible, but two nearby exact anchors can still
    # identify the source page. Accept a unique one-glyph mismatch on it.
    for i, (p, label, target) in enumerate(candidates):
        if p in matches or len(label) < 2:
            continue
        before = next((anchors[k] for k in range(i-1, max(-1,i-5), -1) if k in anchors), None)
        after = next((anchors[k] for k in range(i+1, min(len(candidates),i+5)) if k in anchors), None)
        if before is None or after is None or rows[before]['page'] != rows[after]['page']:
            continue
        page = rows[before]['page']
        hits = [j for j in targets.get(target, ()) if j not in used and rows[j]['page'] == page
                and rows[j].get('columns') == 2
                and abs(rows[j]['y'] - rows[before]['y']) <= .10 and abs(rows[j]['y'] - rows[after]['y']) <= .10]
        if len(hits) == 1:
            j = hits[0]
            other = rows[j].get('label', '')
            if len(label) == len(other) and sum(a != b for a, b in zip(label, other)) == 1:
                matches[p] = {'row':rows[j], 'method':'same-page-anchors', 'score':1 - 1 / len(label)}
                used.add(j)
    return matches
