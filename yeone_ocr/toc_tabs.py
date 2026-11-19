"""Output-only TOC leaders; preserve runs and use the current column width."""
from copy import deepcopy
import re
import unicodedata

from .collate.hwpx_edit import HP, HH, paragraph_text, _slots

HC = '{http://www.hancom.co.kr/hwpml/2011/core}'
PAGE_NUMBER = r'(?:[(（][ \u3000]*\d{1,4}[ \u3000]*[)）]?|\d{1,4}[)）]?)'
ENTRY = re.compile(r'(?P<leader>[ \u3000]*(?:[.…⋯·．][ \u3000]*){2,}|[ \u3000]*\t[ \u3000]*)(?P<page>'+PAGE_NUMBER+r')\s*$')
SPACED_ENTRY = re.compile(r'(?P<leader>[ \u3000]+)(?P<page>'+PAGE_NUMBER+r')\s*$')


def list_entry(text):
    return ENTRY.search(text) or SPACED_ENTRY.search(text)


def _leader(p, match, leader='DASH', width=0):
    """Replace only literal leaders, leaving page-number runs and controls intact."""
    start, end = match.span('leader')
    text = paragraph_text(p)
    if '\t' in text[start:end]:
        for slot in _slots(p):
            if slot.holder.tag==HP+'tab' and start<=slot.start-1<end:
                slot.holder.set('leader','3' if leader=='DASH' else '0')
                if width:
                    slot.holder.set('width', str(width))
        return
    slots = _slots(p)
    anchor = next(s for s in slots if s.start <= start < s.end)
    for slot in slots:
        lo, hi = max(start, slot.start), min(end, slot.end)
        if lo < hi:
            slot.text = slot.text[:lo-slot.start] + slot.text[hi-slot.start:]
    pos = start-anchor.start
    before, after = anchor.text[:pos], anchor.text[pos:]
    tab = p.makeelement(HP+'tab', {'width':str(width), 'leader':'3' if leader=='DASH' else '0', 'type':'2'})
    tab.tail = after
    if anchor.attr == 'text':
        anchor.holder.text = before
        anchor.holder.insert(0, tab)
    else:
        anchor.holder.tail = before
        anchor.t.insert(list(anchor.t).index(anchor.holder)+1, tab)
    assert paragraph_text(p) == text[:start]+'\t'+text[end:]


def align_toc_tabs(doc, source_evidence=None, *, index_rows=None, excluded=()):
    """Require adjacent TOC-like lines; never change section/column structure.

    Unequal columns are left alone because their active column cannot be inferred
    from paragraph XML without pagination. Existing right tabs near the column
    edge are reused (including a user's 425 pt tab on a 425.2 pt text area).
    """
    header = doc.oxml.headers[0]
    root = header.element
    props = root.find('.//'+HH+'paraProperties')
    tabs = root.find('.//'+HH+'tabProperties')
    byid = {p.get('id'):p for p in props}
    tabids = {t.get('id'):t for t in tabs}
    cache = {}
    changed = skipped = 0;findings=[];correspondences=[]
    index_rows = index_rows or {}
    from .index_layout import index_entry
    for section in doc.sections:
        ps = list(section.element.findall(HP+'p'))
        if source_evidence is not None:
            from .list_matching import match_toc_paragraphs
            from .source_list_apply import finding
            source_matches=match_toc_paragraphs([p for p in ps if p not in index_rows and p not in excluded],source_evidence)
        matches = {}
        for i, p in enumerate(ps):
            if p in excluded:
                continue
            text = paragraph_text(p)
            if p in index_rows:
                row = index_rows[p]
                m = index_entry(text, allow_plain=row.get('allow_plain', False)) if row['is_entry'] else None
            else:
                m = list_entry(text) if source_evidence is not None else ENTRY.search(text)
            if m and m.start() > 0 and len(text) <= 300 and not any(c in text for c in '〔〕\n'):
                matches[i] = m
        page_width = None
        col = None
        for i, p in enumerate(ps):
            for pp in p.iter(HP+'pagePr'):
                margin = pp.find(HP+'margin')
                if margin is not None:
                    page_width = int(pp.get('width'))-sum(int(margin.get(k,'0')) for k in ('left','right','gutter'))
            for cp in p.iter(HP+'colPr'):
                col = cp
            index = index_rows.get(p)
            if i not in matches or (index is None and not (i-1 in matches or i+1 in matches)):
                continue
            match=matches[i]
            row = correspondence = None
            if index is not None:
                row = index
                leader = index['leader']
            elif source_evidence is not None:
                correspondence=source_matches.get(p)
                row=correspondence['row'] if correspondence else None
                if row is None:
                    nearby = ps[max(0,i-3):i+4]
                    if not ENTRY.search(paragraph_text(p)) or sum(q in source_matches for q in nearby)<2:
                        skipped+=1
                        findings.append(finding('unmatched-entry',p))
                        continue
                    findings.append(finding('default-leader-unmatched-entry',p))
                elif row.get('leader') not in ('DASH','NONE'):
                    findings.append(finding('default-leader',p,row))
                leader='NONE' if row and row.get('leader')=='NONE' else 'DASH'
            else:
                # Without PDF evidence only literal dots are converted. An
                # existing invisible tab must never acquire a dotted leader.
                if '\t' in paragraph_text(p)[match.start():match.end()]:continue
                leader='DASH'
            if page_width is None or (col is not None and col.get('sameSz','1') != '1'):
                skipped += 1
                continue
            count = int(col.get('colCount','1')) if col is not None else 1
            if source_evidence is not None and row is not None and count!=row['columns']:
                skipped+=1;findings.append(finding('unresolved-column-geometry',p,row));continue
            gap = int(col.get('sameGap','0')) if col is not None else 0
            baseid = p.get('paraPrIDRef','0')
            base = byid[baseid]
            # Modern HWPUNIT values take precedence over legacy doubled values.
            margins = base.findall('.//'+HH+'margin')
            margin = margins[0] if margins else None
            inset = 0
            if margin is not None:
                for side in ('left','right'):
                    n = margin.find(HC+side)
                    if n is not None:
                        inset += int(n.get('value','0'))
            width = (page_width-gap*(count-1))//max(1,count)-inset
            if width <= 0:
                skipped += 1
                continue
            key = (baseid,width,leader)
            if key not in cache:
                old = tabids.get(base.get('tabPrIDRef'))
                items = [] if old is None else list(old.iter(HH+'tabItem'))
                usable = old is not None and old.get('autoTabRight')=='1' and any(
                    t.get('type')=='RIGHT' and t.get('leader')==leader and abs(int(t.get('pos','0'))-width)<=100 for t in items)
                # Intermediate stops would consume the single inserted tab.
                usable = usable and not any(int(t.get('pos','0')) < width-100 for t in items)
                if usable:
                    tabid = old.get('id')
                else:
                    tabid = str(max(map(int,tabids),default=-1)+1)
                    tab = tabs.makeelement(HH+'tabPr', {'id':tabid,'autoTabLeft':'0','autoTabRight':'1'})
                    switch = tabs.makeelement(HP+'switch', {})
                    case = tabs.makeelement(HP+'case', {HP+'required-namespace':'http://www.hancom.co.kr/hwpml/2016/HwpUnitChar'})
                    case.append(tabs.makeelement(HH+'tabItem', {'pos':str(width),'type':'RIGHT','leader':leader,'unit':'HWPUNIT'}))
                    fallback = tabs.makeelement(HP+'default', {})
                    fallback.append(tabs.makeelement(HH+'tabItem', {'pos':str(width*2),'type':'RIGHT','leader':leader}))
                    switch.extend([case,fallback])
                    tab.append(switch)
                    tabs.append(tab)
                    tabids[tabid] = tab
                if base.get('tabPrIDRef') == tabid:
                    cache[key] = baseid
                else:
                    new = deepcopy(base)
                    newid = str(max(map(int,byid),default=-1)+1)
                    new.set('id',newid)
                    new.set('tabPrIDRef',tabid)
                    props.append(new)
                    byid[newid] = new
                    cache[key] = newid
            visible = paragraph_text(p)
            label_and_number = visible[:match.start()] + match['page']
            estimated_ink = sum(1000 if unicodedata.east_asian_width(c) in 'WF' else 500 for c in label_and_number)
            advance = max(1, width - estimated_ink)
            if source_evidence is not None and row is not None and row.get('gap') and row.get('column_end', 0) > row.get('column_start', 0):
                start, _, end, _ = row['gap']
                extent = row['column_end'] - row['column_start']
                advance = max(1, min(width, round(width * (end - start) / extent)))
            _leader(p, match, leader, advance)
            p.set('paraPrIDRef',cache[key])
            for lines in list(p.findall(HP+'linesegarray')):
                p.remove(lines)
            section.mark_dirty()
            changed += 1
            if source_evidence is not None and correspondence is not None:
                correspondences.append(finding('applied',p,row,method=correspondence['method'],score=correspondence['score'],leader=leader))
    if changed:
        props.set('itemCnt',str(len(props)))
        tabs.set('itemCnt',str(len(tabs)))
        header.mark_dirty()
    result={'toc_aligned':changed, 'toc_alignment_skipped':skipped}
    if source_evidence is not None:result.update(toc_findings=findings,toc_correspondences=correspondences)
    return result
