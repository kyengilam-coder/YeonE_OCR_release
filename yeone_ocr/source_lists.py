"""Evidence-based list layout from PDF text/OCR coordinates, never text correction."""
from copy import deepcopy
from statistics import median
import re

from .collate.hwpx_edit import HP, HH, paragraph_text
from .collate.zh import to_simplified
from .korean_forms import korean_forms
from .toc_tabs import PAGE_NUMBER, list_entry

BIB = re.compile(r'^\s*(\d{1,3})\s*[.．、]?\s*[《『](.+?)[》』]')
SOURCE_BIB = re.compile(r'^\s*([0-9IlL]{1,3})\s*[.．、]?\s*[《『](.+?)[》』]')
INLINE_AUTHOR = re.compile(r'^(?:南北朝|民國|民国|朝鮮|朝鲜|漢|汉|晉|晋|隋|唐|宋|金|元|明|清|淸|日)[\s·•・\-—]')
DOTS = re.compile(r'[.…⋯·．]{2,}')


def separator_shape(text):
    """Only the actual separator determines appearance, never the row kind."""
    if DOTS.search(text):return 'DASH'
    if not text.strip():return 'NONE'
    return None


def key(text):
    return ''.join(c for c in korean_forms(to_simplified(text)) if c.isalnum())


def _page_cell(text):
    if re.fullmatch(r'\s*'+PAGE_NUMBER+r'\s*',text):return text.strip(),len(text)-len(text.lstrip())
    match=list_entry(text)
    if match and not any(c.isalnum() for c in text[:match.start('page')]):
        return match['page'],match.start('page')
    return None


def page_lists(lines, page):
    """lines: (text, x0, y0, x1, y1) in normalized page coordinates."""
    rows = []
    for text,x0,y0,x1,y1 in lines:
        if not .08 < y0 < .96:
            continue
        m = list_entry(text)
        b = SOURCE_BIB.match(text)
        if m:
            label=key(text[:m.start()])
            if not label or not any(c.isalpha() for c in label):continue
            rows.append(dict(kind='toc', key=label+key(m['page']), label=label, target=key(m['page']), text=text[:m.start()].strip(), x=x0,y=y0,bottom=y1,right=x1,page=page,
                             leader=separator_shape(m['leader']), separator=(m.start('leader'),m.end('leader'))))
        elif b:
            number=int(b[1].translate(str.maketrans({'I':'1','l':'1','L':'1'})))
            # A separate, aligned author cell is evidence of a bibliography,
            # unlike numbered citations followed by full-width body prose.
            authors=[v for v in lines if v[1]>x1 and abs((v[2]+v[4]-y0-y1)/2)<.008 and 1<len(v[0].strip())<45 and not BIB.match(v[0]) and not re.fullmatch(r'[.…⋯·．\s]+',v[0])]
            suffix=text[b.end():]
            sep=re.match(r'[.…⋯·．\s]*',suffix)[0]
            inline=suffix[len(sep):].strip()
            if 1<len(inline)<45 and INLINE_AUTHOR.match(inline):
                rows.append(dict(kind='bibliography',key=key(b[2]),text=b[2],author=inline,number=number,x=x0,y=y0,bottom=y1,right=x1,page=page,
                                 leader=separator_shape(sep),separator=(b.end(),b.end()+len(sep))))
            elif authors:
                author=min(authors,key=lambda v:v[1])
                between=''.join(v[0] for v in lines if x1<=v[1]<author[1] and abs((v[2]+v[4]-y0-y1)/2)<.008)
                rows.append(dict(kind='bibliography', key=key(b[2]),text=b[2],author=author[0], number=number,x=x0,y=y0,bottom=y1,right=author[3],target_x=author[1],page=page,
                                 leader=separator_shape(suffix+between),gap=(x1,y0,author[1],y1)))
        else:
            # Page numbers can be separate PDF cells with a blank gap.
            numbers=[v for v in lines if v[1]>x1 and abs((v[2]+v[4]-y0-y1)/2)<.008 and _page_cell(v[0])]
            if numbers and any(c.isalpha() for c in text) and not re.fullmatch(r'[\d\s().（）]+',text):
                number=min(numbers,key=lambda v:v[1])
                target,offset=_page_cell(number[0])
                between=''.join(v[0] for v in lines if x1<=v[1]<number[1] and abs((v[2]+v[4]-y0-y1)/2)<.008)
                rows.append(dict(kind='toc',key=key(text)+key(target),label=key(text),target=key(target),text=text.strip(),x=x0,y=y0,bottom=y1,right=number[3],target_x=number[1],target_cell=(number[1],number[2]),target_offset=offset,page=page,
                                 leader=separator_shape(between),gap=(x1,y0,number[1],y1)))
    result=[]
    for kind in ('toc','bibliography'):
        group=[r for r in rows if r['kind']==kind]
        if len(group)<4:
            continue
        right=[r for r in group if r['x']>.45]
        segments=[group]
        if len(right)>=3:
            # A page may start full width and switch to two columns below a
            # heading. Do not reject that whole page as an inconsistent layout.
            top=[r for r in group if r['y']<min(v['y'] for v in right)-.012 and r['x']<.4]
            if len(top)>=4:
                segments=[top,[r for r in group if r not in top]]
        for segment,items in enumerate(segments):
            measured = _measure_columns(items,segment)
            if kind == 'toc' and measured and measured[0]['columns'] == 2:
                # Standalone rows that span both columns divide the bands.
                # A long row touching another column on the same line is
                # ambiguous and is kept out of the source correspondences.
                spanning = sorted((r for r in items if r not in measured and r['x']<.4 and
                                   not any(abs(r['y']-v['y'])<.010 for v in measured)), key=lambda r:r['y'])
                for row in measured:
                    row['segment'] = segment + 2 * sum(wide['y'] < row['y'] for wide in spanning)
                for offset, row in enumerate(spanning):
                    row.update(columns=1,column=0,segment=segment+2*offset+1,column_start=row['x'],spanning=True,
                               column_end=row['right'],gap_fraction=0)
                measured.extend(spanning)
            result.extend(measured)
    return result


def _measure_columns(group,segment):
    if len(group)<3:return []
    # Two stable start positions separated by a real gutter. A title or
    # wide row crossing the midpoint is never evidence of two columns.
    left=[r for r in group if r['x']<.4]
    right=[r for r in group if r['x']>.45]
    right_start=median(r['x'] for r in right) if right else 0
    narrow_left=[r for r in left if r['right']<right_start]
    columns=2 if len(narrow_left)>=3 and len(right)>=3 else 1
    if columns==1:
        if len(left)<3 or len(left)<len(group)*.75:return []
        group=left  # isolated right-hand page numbers are not a column
    else:
        group=[r for r in group if r in narrow_left or r in right]
    groups=[[r for r in group if columns==1 or r['x']<.4]]
    if columns==2:groups.append([r for r in group if r['x']>.45])
    starts=[median(r['x'] for r in g) for g in groups]
    ends=[max(r['right'] for r in g) for g in groups]
    extent=max(ends)-min(starts)
    gap=(starts[1]-ends[0])/extent if columns==2 else 0
    if extent<=0 or gap<0:return []
    for row in group:
        row['columns']=columns
        row['segment']=segment
        row['column']=int(columns==2 and row['x']>.45)
        col=row['column']
        row['column_start']=starts[col];row['column_end']=ends[col]
        row['gap_fraction']=gap
        if 'target_x' in row:
            row['tab_fraction']=(row['target_x']-starts[col])/(ends[col]-starts[col])
    return group


def visible_gap_shape(page, gap, rendered=None):
    """Inspect the rendered gap, including raster/vector dots absent from OCR.

    Blank is positive evidence for NONE. Repeated small ink islands are dot
    leaders. Text, solid rules, tight gaps and noisy scans remain unresolved.
    """
    import pymupdf
    from statistics import median
    if gap is None or page.rotation:
        return None
    x0,y0,x1,y1=gap;w,h=page.rect.width,page.rect.height
    rect=pymupdf.Rect(x0*w+1,y0*h,x1*w-1,y1*h)&page.rect
    if rect.width<8 or rect.height<3:return None
    if rendered is None:
        pix=page.get_pixmap(matrix=pymupdf.Matrix(3,3),clip=rect,colorspace=pymupdf.csGRAY,alpha=False)
        pixels=pix.samples;width,height=pix.width,pix.height
    else:
        crop=rendered.crop(tuple(round(v*3) for v in rect))
        pixels=crop.tobytes();width,height=crop.size
    columns=[any(pixels[y*width+x]<160 for y in range(height)) for x in range(width)]
    ink=sum(v<160 for v in pixels)
    if ink<=max(1,len(pixels)*.0005):return 'NONE'
    runs=[];start=None
    for x,filled in enumerate(columns+[False]):
        if filled and start is None:start=x
        elif not filled and start is not None:runs.append((start,x));start=None
    def regular(parts):
        if len(parts) < 4:
            return False
        sizes = [b-a for a,b in parts]
        gaps = [right[0]-left[1] for left,right in zip(parts,parts[1:])]
        return max(sizes) <= height*.55 and max(sizes) <= median(sizes)*2.5 and max(gaps) <= median(gaps)*2.5

    if regular(runs):
        return 'DASH'
    # OCR boxes can leave a sliver of the neighbouring letter or parenthesis
    # in the crop. Accept a long regular dotted interior, never a blank guess.
    for left, right in ((1, 0), (0, 1), (1, 1)):
        core = runs[left:len(runs)-right]
        if (len(core) >= 8 and core[-1][1]-core[0][0] >= width*.65 and regular(core)
                and (not left or runs[0][0] <= width*.1) and (not right or runs[-1][1] >= width*.9)):
            return 'DASH'
    return None


def _render_page(page):
    import pymupdf
    from PIL import Image
    pix=page.get_pixmap(matrix=pymupdf.Matrix(3,3),colorspace=pymupdf.csGRAY,alpha=False)
    return Image.frombytes('L',(pix.width,pix.height),pix.samples)


def _pdf_lines(page):
    """Split extractor lines that span a physical gutter; retain glyph boxes."""
    import pymupdf
    w,h=page.rect.width,page.rect.height
    lines=[];characters={}
    for block in page.get_text('rawdict',flags=pymupdf.TEXTFLAGS_TEXT)['blocks']:
        for line in block.get('lines',[]):
            chars=[c for span in line['spans'] for c in span['chars']]
            if not chars:continue
            size=median(c['bbox'][3]-c['bbox'][1] for c in chars)
            fragments=[[]]
            for c in chars:
                if c['c'].isspace() and c['bbox'][2]-c['bbox'][0]>max(8,size):
                    if fragments[-1]:fragments.append([])
                    continue
                last=fragments[-1][-1] if fragments[-1] else None
                if last and c['bbox'][0]-last['bbox'][2]>max(8,size):fragments.append([])
                fragments[-1].append(c)
            for fragment in fragments:
                if not fragment:continue
                x0=min(c['bbox'][0] for c in fragment);y0=min(c['bbox'][1] for c in fragment)
                x1=max(c['bbox'][2] for c in fragment);y1=max(c['bbox'][3] for c in fragment)
                lines.append((''.join(c['c'] for c in fragment),x0/w,y0/h,x1/w,y1/h))
                characters[(x0/w,y0/h)]=fragment
    return lines,characters


def pdf_lists(path, records=None):
    from .pdfio import open_pdf
    rows=[]; empty=[]
    selected = {r.get('pdf_page') for r in records or () if type(r.get('pdf_page')) is int}
    with open_pdf(path) as pdf:
        numbers = sorted(n for n in selected if 1 <= n <= len(pdf)) if selected else range(1, len(pdf) + 1)
        for number in numbers:
            page = pdf[number - 1]
            w,h=page.rect.width,page.rect.height
            lines,characters=_pdf_lines(page)
            found=page_lists(lines,number)
            rendered=_render_page(page) if found else None
            for row in found:
                if 'target_cell' in row:
                    chars=characters.get(tuple(row['target_cell']),[]);offset=row['target_offset']
                    if offset<len(chars):
                        x0,y0,_,y1=row['gap'];target=chars[offset]['bbox'][0]/w
                        row['gap']=(x0,y0,target,y1);row['target_x']=target
                if 'separator' in row:
                    chars=characters.get((row['x'],row['y']),[])
                    a,b=row['separator']
                    if 0<a<=b<len(chars):
                        row['gap']=(chars[a-1]['bbox'][2]/w,chars[a-1]['bbox'][1]/h,
                                    chars[b]['bbox'][0]/w,chars[b]['bbox'][3]/h)
                        row['target_x']=chars[b]['bbox'][0]/w
                if row.get('target_x') is not None:
                    row['tab_fraction']=(row['target_x']-row['column_start'])/(row['column_end']-row['column_start'])
                row['leader']=visible_gap_shape(page,row.get('gap'),rendered)
                row['leader_basis']='rendered-pdf-gap' if row['leader'] else 'unresolved-pdf-gap'
            rows.extend(found)
            if not found:empty.append(number)
    # Scanned PDFs can use existing OCR region geometry; no extra AI calls.
    if records:
        from .schema import PageReading, paragraph_text as region_text
        for record in records:
            number=record.get('page') or record.get('pdf_page') or (record.get('merged') or {}).get('pdf_page')
            if number not in empty:continue
            value=record.get('merged')
            if not value:continue
            reading=PageReading.model_validate(value)
            lines=[]
            for region in reading.regions:
                y0,x0,y1,x1=region.bbox
                text=region.title_text or ' '.join(region_text(p) for p in region.paragraphs)
                lines.append((text,x0/1000,y0/1000,x1/1000,y1/1000))
            found=page_lists(lines,number)
            with open_pdf(path) as pdf:
                rendered=_render_page(pdf[number-1]) if found else None
                for row in found:
                    row['leader']=visible_gap_shape(pdf[number-1],row.get('gap'),rendered)
                    row['leader_basis']='rendered-pdf-gap' if row['leader'] else 'unresolved-pdf-gap'
            rows.extend(found)
    return rows


def _columns(p,count,gap=0):
    attrs={'id':'','type':'NEWSPAPER','layout':'LEFT','colCount':str(count),'sameSz':'1','sameGap':str(gap if count>1 else 0)}
    for run in p.findall(HP+'run'):
        for ctrl in run.findall(HP+'ctrl'):
            for old in ctrl.findall(HP+'colPr'):
                old.attrib.update(attrs)
                return
    run=p.makeelement(HP+'run',{'charPrIDRef':'0'})
    ctrl=p.makeelement(HP+'ctrl',{})
    ctrl.append(p.makeelement(HP+'colPr',attrs))
    run.append(ctrl);p.insert(0,run)


def _prior_columns(ps,index):
    prior=None
    for p in ps[:index]:
        for col in p.iter(HP+'colPr'):prior=deepcopy(col)
    return prior


def _restore_columns(p,prior):
    if p.find('.//'+HP+'colPr') is not None:return
    if prior is None:
        _columns(p,1)
    else:
        run=p.makeelement(HP+'run',{'charPrIDRef':'0'})
        ctrl=p.makeelement(HP+'ctrl',{});ctrl.append(deepcopy(prior));run.append(ctrl);p.insert(0,run)


def measured_layout(rows,body):
    counts={r['columns'] for r in rows}
    if len(counts)!=1:return None
    count=counts.pop()
    fractions=[r.get('gap_fraction') for r in rows if r.get('gap_fraction') is not None]
    if count>1 and not fractions:return None
    gap=round(body*median(fractions)) if count>1 else 0
    if gap<0 or gap*(count-1)>=body:return None
    return count,gap,(body-gap*(count-1))//count


def _list_tab_format(doc,p,width,cache,leader,tab_fraction):
    header=doc.oxml.headers[0];root=header.element
    props=root.find('.//'+HH+'paraProperties');tabs=root.find('.//'+HH+'tabProperties')
    baseid=p.get('paraPrIDRef','0');position=round(width*tab_fraction);ident=(baseid,position,leader)
    if ident not in cache:
        tid=str(max(int(x.get('id')) for x in tabs)+1)
        tab=tabs.makeelement(HH+'tabPr',{'id':tid,'autoTabLeft':'0','autoTabRight':'0'})
        switch=tabs.makeelement(HP+'switch',{})
        for tag,mult in [('case',1),('default',2)]:
            attrs={HP+'required-namespace':'http://www.hancom.co.kr/hwpml/2016/HwpUnitChar'} if tag=='case' else {}
            child=tabs.makeelement(HP+tag,attrs)
            attrs={'pos':str(position*mult),'type':'LEFT','leader':leader}
            if tag=='case':attrs['unit']='HWPUNIT'
            child.append(tabs.makeelement(HH+'tabItem',attrs));switch.append(child)
        tab.append(switch);tabs.append(tab)
        new=deepcopy(next(x for x in props if x.get('id')==baseid))
        pid=str(max(int(x.get('id')) for x in props)+1)
        new.set('id',pid);new.set('tabPrIDRef',tid)
        for n in new.iter():
            local=n.tag.rsplit('}',1)[-1]
            if local=='align':n.set('horizontal','LEFT')
            if local=='heading':n.set('type','NONE')
            if local in ('intent','left','right','prev','next'):n.set('value','0')
            if local=='breakSetting':
                n.set('keepWithNext','0');n.set('keepLines','1');n.set('pageBreakBefore','0')
                n.set('breakLatinWord','BREAK_WORD');n.set('breakNonLatinWord','BREAK_WORD')
        props.append(new);props.set('itemCnt',str(len(props)));tabs.set('itemCnt',str(len(tabs)))
        cache[ident]=pid;header.mark_dirty()
    p.set('paraPrIDRef',cache[ident])


def apply_source_lists(doc, evidence, **kwargs):
    from .source_list_apply import apply_source_lists as apply
    return apply(doc, evidence, **kwargs)
