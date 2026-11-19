"""Apply measured source list layouts without changing OCR characters."""
from copy import deepcopy
from difflib import SequenceMatcher
import re

from .collate.hwpx_edit import HP, paragraph_text
from .list_matching import match_toc_paragraphs
from .source_lists import BIB, key, measured_layout, _columns, _prior_columns, _restore_columns, _list_tab_format
from .toc_tabs import list_entry


def finding(reason, paragraph=None, row=None, **extra):
    result={'reason':reason,**extra}
    if paragraph is not None:
        result.update(paragraph_id=paragraph.get('id'),text=paragraph_text(paragraph))
    if row:
        result.update(source_page=row['page'],source_text=row.get('text',row['key']),
                      source_bbox=[row['x'],row['y'],row['right'],row.get('bottom',row['y'])])
        if 'number' in row:result['source_number']=row['number']
    return result


def _bibliography_match(number,label,rows):
    exact=[r for r in rows if r['key']==key(label)]
    preferred=[r for r in exact if r['number']==number]
    if len(preferred)==1:return preferred[0]
    if len(exact)==1:return exact[0]  # OCR can misread 81 as 87; do not rewrite it
    scored=sorted(((SequenceMatcher(None,key(label),r['key'],autojunk=False).ratio(),j)
                   for j,r in enumerate(rows) if r['number']==number),reverse=True)
    if scored and scored[0][0]>=.65 and (len(scored)==1 or scored[0][0]-scored[1][0]>=.15):
        return rows[scored[0][1]]
    return None


def _clear_lines(p):
    for node in list(p.findall(HP+'linesegarray')):p.remove(node)


def _order_proven_columns(root, paragraphs, matches):
    """Restore down-column order inside uninterrupted, proven source bands."""
    from itertools import groupby

    def band(p):
        row = matches.get(p, {}).get('row', {})
        return (row['page'], row.get('segment', 0)) if row.get('columns') == 2 else None

    changed = False
    for identity, group in groupby(paragraphs, key=band):
        rows = list(group)
        if identity is None or len(rows) < 3 or any(p.find('.//'+HP+'colPr') is not None for p in rows):
            continue
        def place(p):
            row = matches[p]['row']
            return row['column'], row['y'], row['x']
        # Multiple rows across two columns are necessary to distinguish an
        # interleaved reading from an isolated uncertain match.
        if len({place(p)[0] for p in rows}) < 2:
            continue
        ordered = sorted(rows, key=place)
        if ordered == rows:
            continue
        index = list(root).index(rows[0])
        if list(root)[index:index+len(rows)] != rows:
            continue
        for p in rows:
            root.remove(p)
        for offset, p in enumerate(ordered):
            root.insert(index+offset, p)
            _clear_lines(p)
        changed = True
    return changed


def _split_proven_pairs(root, evidence):
    """Separate two OCR cells fused into a single paragraph."""
    from collections import defaultdict
    from .collate.hwpx_edit import _slots
    from .toc_tabs import ENTRY

    left = defaultdict(list)
    right = defaultdict(list)
    for row in evidence:
        if row['kind'] == 'toc' and row['columns'] == 2:
            (left if row['column'] == 0 else right)[row['target']].append(row)
    if not left or not right:
        return False
    internal_entry = re.compile(ENTRY.pattern.removesuffix(r'\s*$') + r'(?=\s+\D)')
    changed = False
    for p in list(root.findall(HP+'p')):
        text = paragraph_text(p)
        match = internal_entry.search(text)
        if match is None or not match['page'].endswith((')', '）')):
            continue
        split = match.end()
        tail = text[split:].lstrip()
        end = list_entry(tail)
        if end is None:
            continue
        labels = key(text[:match.start()]), key(tail[:end.start()])
        pairs = []
        for a in left[key(match['page'])]:
            for b in right[key(end['page'])]:
                if a['page'] != b['page'] or a['segment'] != b['segment'] or abs(a['y']-b['y']) > .012:
                    continue
                others = a['label'], b['label']
                if all(len(label) == len(other) and len(label) >= 2 and sum(x != y for x,y in zip(label,other)) <= 1
                       for label,other in zip(labels,others)):
                    pairs.append((a,b))
        if len(pairs) != 1:
            continue
        if any(child.tag != HP+'t' or len(child) for run in p.findall(HP+'run') for child in run):
            continue
        new = p.makeelement(HP+'p', dict(p.attrib))
        new.set('id', '0')
        for run in p.findall(HP+'run'):
            new.append(deepcopy(run))
        tail_start = len(text) - len(tail)
        for slot in _slots(new):
            slot.text = slot.text[max(0,tail_start-slot.start):]
        for slot in _slots(p):
            slot.text = slot.text[:max(0,split-slot.start)]
        _clear_lines(p)
        root.insert(list(root).index(p)+1, new)
        changed = True
    return changed


def apply_source_lists(doc,evidence, *, excluded=()):
    stats={'source_list_columns':0,'source_bibliography_rows':0,'source_list_unresolved':0,
           'source_list_missing':0,'source_list_findings':[]}
    issues=stats['source_list_findings'];cache={}
    bib=[r for r in evidence if r['kind']=='bibliography']
    for section in doc.sections:
        root=section.element;ps=list(root.findall(HP+'p'));pp=root.find('.//'+HP+'pagePr')
        if pp is None:continue
        margin=pp.find(HP+'margin')
        body=int(pp.get('width'))-sum(int(margin.get(k,'0')) for k in ('left','right','gutter'))
        if _split_proven_pairs(root,evidence):
            section.mark_dirty()
            ps=list(root.findall(HP+'p'))
        matches=match_toc_paragraphs([p for p in ps if p not in excluded],evidence)
        if _order_proven_columns(root,ps,matches):
            section.mark_dirty()
            ps=list(root.findall(HP+'p'))
            matches=match_toc_paragraphs([p for p in ps if p not in excluded],evidence)
        i=0
        while i<len(ps):
            if ps[i] in excluded or not list_entry(paragraph_text(ps[i])):i+=1;continue
            start=i;end=i
            while end<len(ps):
                if ps[end] in excluded:break
                text=paragraph_text(ps[end])
                if not list_entry(text) and not (len(text.strip())<=8 and end+1<len(ps) and list_entry(paragraph_text(ps[end+1]))):break
                end+=1
            proven=[(j,matches[ps[j]]['row']) for j in range(start,end) if ps[j] in matches]
            if len(proven)>=4 or any(row.get('spanning') for _,row in proven):
                prior=_prior_columns(ps,start)
                # Changes occur at matched source rows, not a majority vote
                # over a whole mixed 1/2-column contents section.
                segments=[]
                for j,row in proven:
                    if not segments or segments[-1][1][-1]['columns']!=row['columns']:
                        segments.append((j,[row]))
                    else:segments[-1][1].append(row)
                for j,rows in segments:
                    layout=measured_layout(rows,body)
                    if layout is None:continue
                    count,gap,_=layout
                    _columns(ps[j],count,gap);stats['source_list_columns']+=1
                if end<len(ps):_restore_columns(ps[end],prior)
                for p in ps[start:end]:_clear_lines(p)
                section.mark_dirty()
            i=max(end,i+1)
        i=0
        while i<len(ps):
            if not BIB.match(paragraph_text(ps[i])):i+=1;continue
            start=i;pairs=[];author_confirmed=0;author_conflicts=[]
            while i<len(ps):
                text=paragraph_text(ps[i]);m=BIB.match(text)
                if not m:break
                suffix=text[m.end():]
                if suffix.startswith('\t'):
                    author=None;step=1
                elif not suffix.strip() and i+1<len(ps):
                    author=ps[i+1];value=paragraph_text(author);step=2
                    if not value.strip() or len(value)>70 or BIB.match(value):break
                else:break
                if any(len(list(p.iter(HP+'p')))>1 for p in (ps[i],author) if p is not None):break
                number=int(m[1]);row=_bibliography_match(number,m[2],bib)
                author_text=paragraph_text(author) if author is not None else suffix[1:]
                if row is not None:
                    score=SequenceMatcher(None,key(author_text),key(row.get('author','')),autojunk=False).ratio()
                    if score>=.5:author_confirmed+=1
                    else:author_conflicts.append((ps[i],row))
                pairs.append((number,ps[i],author,row));i+=step
            proof=[r for _,_,_,r in pairs if r is not None]
            if len(pairs)<4 or len(proof)<max(3,len(pairs)*.8) or author_confirmed<max(3,len(pairs)*.6) or len({n for n,_,_,_ in pairs})!=len(pairs):
                i=max(i,start+1);continue
            # Use matched source positions to validate down-column order; a
            # misread source ordinal does not veto otherwise corroborated rows.
            order=sorted(((r['page'],r.get('segment',0),r.get('column',0),r['y'],n)
                          for n,_,_,r in pairs if r is not None))
            if [item[-1] for item in order]!=sorted(item[-1] for item in order):
                issues.append(finding('source-reading-order-conflict',ps[start]));stats['source_list_unresolved']+=1;continue
            layout=measured_layout(proof,body)
            if layout is None:
                issues.append(finding('unresolved-column-geometry',ps[start]));stats['source_list_unresolved']+=1;continue
            count,gap,width=layout
            for p,r in author_conflicts:issues.append(finding('author-text-differs',p,r))
            # Detect source entries absent between confirmed list endpoints.
            # Report them for image review; never silently insert PDF OCR text.
            used={id(r) for r in proof};lo,hi=min(n for n,_,_,_ in pairs),max(n for n,_,_,_ in pairs)
            pages={r['page'] for r in proof}
            for r in bib:
                if r['page'] in pages and lo<=r['number']<=hi and id(r) not in used:
                    reason='source-entry-unmatched' if r['number'] in {n for n,_,_,_ in pairs} else 'missing-source-entry'
                    issues.append(finding(reason,row=r))
                    if reason=='missing-source-entry':stats['source_list_missing']+=1
            prior=_prior_columns(ps,start);insertion=list(root).index(ps[start]);output=[]
            for number,title,author,row in sorted(pairs,key=lambda item:item[0]):
                if author is None:
                    root.remove(title);output.append(title);continue
                fraction=row.get('tab_fraction') if row else None
                ready=row is not None and row.get('leader') in ('DASH','NONE') and fraction is not None and 0<fraction<1
                if not ready:
                    reason='unmatched-entry' if row is None else 'unresolved-leader-or-tab-position'
                    issues.append(finding(reason,title,row,number=number));stats['source_list_unresolved']+=1
                    root.remove(title);root.remove(author);output.extend([title,author]);continue
                leader=row['leader']
                run=title.makeelement(HP+'run',{'charPrIDRef':'0'});t=title.makeelement(HP+'t',{})
                t.append(title.makeelement(HP+'tab',{'width':'0','leader':'3' if leader=='DASH' else '0','type':'0'}))
                run.append(t);title.append(run)
                for run in list(author.findall(HP+'run')):title.append(run)
                _clear_lines(title);_list_tab_format(doc,title,width,cache,leader,fraction)
                root.remove(title);root.remove(author);output.append(title);stats['source_bibliography_rows']+=1
            for offset,p in enumerate(output):root.insert(insertion+offset,p)
            _columns(output[0],count,gap)
            if i<len(ps):_restore_columns(ps[i],prior)
            section.mark_dirty();stats['source_list_columns']+=1
    return stats
