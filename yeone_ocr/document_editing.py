"""Reversible document-only paragraph and spacing policy. Never saves OCR records."""
from __future__ import annotations
from copy import deepcopy
import re
import unicodedata
from collections import Counter
from .blocks import Block
from .schema import PageReading, paragraph_text, TEXT_KINDS, SKIP_KINDS, TITLE_KINDS
from .marks import MARK_RE
from .hwpx_output_rules import HwpxOutputRules

HAN = r'\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff'
ENTRY = re.compile(rf'^([{HAN}]{{1,6}})[ \t\u3000]+(?=(?:味|一名|性|氣|气)[{HAN}])')
HERB_NAME = rf'(?:[ \t\u3000]*[{HAN}]){{1,12}}'
MARKED_ENTRY = re.compile(rf'◎{HERB_NAME}')
BAD_START = '凡又其此是以故若則则乃每諸诸'
TOC = re.compile(r'[.·…．]{2,}.*\d+[）)]?\s*$')
META = re.compile(r'ISBN|出版社|出版发行|出版發行|邮政|郵政|责任编辑|責任編輯|开本|開本|印刷|定价|定價')
ENTRY_FIELD = re.compile(r'[\[［〔【]\s*(?:[氣气]\s*味|主\s*治|修\s*治|附\s*方|愚\s*按|禁\s*忌|炮\s*製|炮\s*制)\s*[\]］〕】]')
NUMBERED_SOURCE_TITLE=re.compile(r'^\s*(?:[一二三四五六七八九十百千〇○零O0-9０-９]\s*){1,8}[·・.．、,，]\s*'
    r'[^。!?？！;；()（）\n]{4,85}[（(]\s*(?:『[^『』\n]{1,35}』|《[^《》\n]{1,35}》)\s*[）)]')


def numbered_source_title(text,titles):
    """A numbered title + source needs independent template/rule evidence.

    Ordinary inline citations and TOC dot leaders must never become headings.
    """
    from .citation_layout import heading_key
    match=NUMBERED_SOURCE_TITLE.match(text)
    if not match:return None
    role=(titles or {}).get(heading_key(match[0]))
    if isinstance(role,(tuple,list)):role=role[0]
    if role not in ('chapter','section','subsection','catalog'):return None
    if re.match(r'\s*(?:[.…．]{2,}|[（(]?\d{1,4}[）)]?\s*$)',text[match.end():]):return None
    return match.end(),('section' if role=='catalog' else role)


def fused_entry_title(text, titles):
    """A known herb followed by the FIRST numbered source is a lost boundary.

    Never split an arbitrary herb mention in prose or a later citation. Plain
    catalog entries alone do not establish the title level here.
    """
    from .citation_layout import heading_key
    match = re.match(rf'^\s*([{HAN}][{HAN}\s]{{0,23}}?)\s*(?=1\s*[.．、]\s*[《『])', text)
    if not match:
        return None
    role = (titles or {}).get(heading_key(match[1]))
    if isinstance(role, (tuple, list)):
        role = role[0]
    return (match.end(), role) if role == 'section' else None


def _edge_rows(reading):
    rows=[(r,paragraph_text(r.paragraphs[0]).strip()) for r in reading.regions if len(r.paragraphs)==1 and paragraph_text(r.paragraphs[0]).strip()]
    result=[]
    for i,(region,text) in enumerate(rows):
        y0,x0,y1,x1=region.bbox
        # A running head can be split into several OCR boxes. Page numbers
        # remain marginalia when they happen to be the third or fourth box.
        measured=(0<y0<y1<=100 or 900<=y0<y1<=1000 or 0<=x0<x1<=60 or 940<=x0<x1<=1000)
        if i<2 or i>=len(rows)-2 or measured:result.append((region,text))
    return result


def _catalog_heading(reading,index,citation):
    """A TOC entry alone is insufficient: require a measured heading and 1st citation."""
    if citation is None:return False
    region=reading.regions[index]
    text=(region.title_text or ''.join(paragraph_text(p) for p in region.paragraphs)).strip()
    if not re.fullmatch(rf'[{HAN}()（）·\s]{{1,40}}',text):return False
    y0,x0,y1,x1=region.bbox
    if not (100<=y0<y1<=900 and y1-y0>=18 and 20<=x1-x0<=550 and abs((x0+x1)/2-500)<=35):return False
    following=next((r for r in reading.regions[index+1:] if r.title_text or any(paragraph_text(p).strip() for p in r.paragraphs)),None)
    if following is None or following.kind not in TEXT_KINDS:return False
    text=''.join(paragraph_text(p) for p in following.paragraphs).strip()
    from .citation_layout import source_end
    return bool(re.match(r'^\s*[1１]\s*[.．、)）]',text) and source_end(text,citation) is not None)


def _member_heading(reading,index,citation):
    if citation is None:return False
    region=reading.regions[index]
    text=(region.title_text or ''.join(paragraph_text(p) for p in region.paragraphs)).strip()
    if not re.fullmatch(rf'[{HAN}]{{1,12}}',re.sub(r'\s','',text)):return False
    y0,x0,y1,x1=region.bbox
    if region.bbox==[0,0,1000,1000] or not (90<=y0<y1<=950 and x1-x0<=400):return False
    following=next((r for r in reading.regions[index+1:] if any(paragraph_text(p).strip() for p in r.paragraphs)),None)
    if following is None:return False
    text=''.join(paragraph_text(p) for p in following.paragraphs).strip()
    from .citation_layout import source_end
    return bool(re.match(r'^\s*[1１]\s*[.．、)）]',text) and source_end(text,citation) is not None)


def _header_signature(text):
    from .collate.zh import _cc
    converter=_cc('t2s')
    text=converter.convert(text) if converter else text
    return ''.join(c for c in text if not c.isspace() and not c.isdecimal()
                   and unicodedata.category(c)[0] not in 'PSZ')


def _header_distance_one(a,b):
    if a==b:return True
    if min(len(a),len(b))<6 or abs(len(a)-len(b))>1:return False
    if len(a)==len(b):return sum(x!=y for x,y in zip(a,b))<=1
    short,long=(a,b) if len(a)<len(b) else (b,a)
    return any(long[:i]+long[i+1:]==short for i in range(len(long)))


def _trim_header_prefix(record,region,cut):
    """Trim an output-view prefix and move its review spans with the body."""
    remaining=cut
    for column in sorted(region.paragraphs[0].columns,key=lambda c:c.col_from_right):
        used=min(remaining,len(column.text));column.text=column.text[used:];remaining-=used
    for field in ('local_grades','_cosmetic_punctuation'):
        grades=[]
        for item in record.get(field,[]):
            item=dict(item)
            if item.get('region_id')==region.id and item.get('para',0)==0:
                if item['end']<=cut:continue
                item['start']=max(cut,item['start'])-cut;item['end']-=cut
            grades.append(item)
        if field in record:record[field]=grades


def exclude_marginalia(records, *, source_pdf=None):
    """Exclude identified running matter in the output view, never raw OCR.

    Repetition is counted per page. A line of body text containing the book
    title is never removed: a short title line must be at a page edge and
    either have a page-number prefix or a measured margin position.
    """
    readings=[PageReading.model_validate(r['merged']) for r in records]
    side_titles = {}
    if source_pdf is not None:
        from .side_titles import repeated_side_titles
        side_titles = repeated_side_titles(source_pdf, readings)
    signatures=Counter()
    margin_signatures=Counter()
    for reading in readings:
        signatures.update({_header_signature(t) for _,t in _edge_rows(reading) if re.fullmatch(rf'[\d\s{HAN}·.―—-]{{1,60}}',t)})
        observed=set()
        for region,text in _edge_rows(reading):
            y0,x0,y1,x1=region.bbox
            citation=bool(re.match(r'^\s*\d{1,3}\s*[.．、)）]\s*[《『]',text))
            if region.kind not in TITLE_KINDS and not citation and 0<y0<y1<=90 and x1-x0<=700 and y1-y0<=40 and len(text)<=60:
                signature=_header_signature(text)
                if len(signature)>=4:observed.add(signature)
        margin_signatures.update(observed)
    repeated={s for s,n in margin_signatures.items() if n>=3}
    for record,reading in zip(records,readings):
        from .common_layout import is_contents_page
        contents=is_contents_page(reading)
        edges={r.id for r,_ in _edge_rows(reading)}
        removed=[];keep=[]
        for region in reading.regions:
            text=paragraph_text(region.paragraphs[0]).strip() if len(region.paragraphs)==1 else ''
            reason=''
            side_bbox = side_titles.get(reading.pdf_page, {}).get(region.id)
            if region.kind in SKIP_KINDS:
                reason='판독에서 분류한 머리말/쪽번호'
            elif side_bbox is not None:
                reason='원본 PDF 좌우 여백에서 반복 확인된 세로 제목'
            elif text and region.id in edges and region.kind not in TITLE_KINDS:
                y0,x0,y1,x1=region.bbox
                margin=y1<=100 or y0>=900 or x1<=60 or x0>=940
                signature=_header_signature(text)
                short=bool(re.fullmatch(rf'[\d\s{HAN}·.―—-]{{1,60}}',text))
                numbered=bool(re.match(r'^\d{1,4}\s*[^\d\s]',text))
                title_like=bool(re.search(r'[开開]?[宝寶]本[草艸].*(?:卷|[目日]录|目錄)',signature))
                volume_line=bool(re.search(r'[卷券]\s*第?\s*[一二三四五六七八九十百千0-9]*\s*$',text))
                declared=bool(reading.running_header.strip() and signature==_header_signature(reading.running_header))
                learned=next((s for s in repeated if _header_distance_one(signature,s)),None)
                if text.isdecimal() and margin and not (contents and y0>=800):
                    reason='여백의 쪽번호'
                elif declared and margin and not (contents and y0>=800):
                    reason='원본 판독에 명시된 여백 머리말'
                elif learned and 0<y0<y1<=100 and y1-y0<=40 and not contents:
                    reason='위쪽 여백에서 반복 확인된 머리말(부호·간번체·한 글자 오인식 허용)'
                elif short and not (contents and y0>=800) and ((declared and (margin or numbered)) or
                     ((title_like or volume_line) and (numbered or margin)) or
                     (signatures[signature]>=3 and (numbered or margin))):
                    reason='쪽 가장자리의 머리말/꼬리말'
                if not reason and not contents and 0<y0<100 and region.bbox!=[0,0,1000,1000]:
                    # Some engines put the running head and first body line in
                    # one region. Only a header independently repeated on other
                    # pages may be removed from a prefix; never search the body.
                    first=paragraph_text(region.paragraphs[0])
                    prefix_cut=None
                    for cut in range(1,min(len(first),100)+1):
                        sig=_header_signature(first[:cut])
                        if sig in repeated:prefix_cut=cut;break
                    if prefix_cut is not None:
                        while prefix_cut<len(first) and (first[prefix_cut].isspace() or first[prefix_cut].isdecimal()
                                or unicodedata.category(first[prefix_cut])[0] in 'PSZ'):
                            prefix_cut+=1
                        if prefix_cut<len(first):
                            _trim_header_prefix(record,region,prefix_cut)
                            removed.append({'region':region.id,'text':first[:prefix_cut],
                                            'reason':'반복 머리말 접두부만 제외, 뒤 본문 보존','partial':True})
            if reason:
                item = {'region':region.id,'text':text,'reason':reason}
                if side_bbox is not None:
                    item['source_bbox'] = side_bbox
                removed.append(item)
            else:keep.append(region)
        reading.regions=keep
        record['merged']=reading.model_dump()
        record['_document_excluded']=removed
    return records


def entry_name(text, names=()):
    text = text.lstrip()
    match = ENTRY.match(text)
    if match and match[1][0] not in BAD_START:
        return match[1]
    for name in sorted(names, key=len, reverse=True):
        if text.startswith(name) and re.match(r'^(?:味|一名|性|氣|气)', text[len(name):].lstrip()):
            return name
    return ''


def known_entries(records):
    names = set()
    for record in records:
        for region in PageReading.model_validate(record['merged']).regions:
            for paragraph in region.paragraphs:
                if name := entry_name(paragraph_text(paragraph)):
                    names.add(name)
    return names


def _marked_entry_hints(reading, pdf, chapter_keys, seen_chapters):
    """Use standalone marked titles, with same-page PDF evidence for OCR defects."""
    from .citation_layout import heading_key

    def region_text(region):
        if region.kind == '제목':
            return region.title_text.strip()
        if region.kind == '본문' and len(region.paragraphs) == 1:
            return paragraph_text(region.paragraphs[0]).strip()
        return ''

    rows = [(region, text) for region in reading.regions if (text := region_text(region))]
    direct = {region.id: '' for region, text in rows if MARKED_ENTRY.fullmatch(text)}
    if chapter_keys:
        seen_chapters.update(chapter_keys.intersection(heading_key(text) for _, text in rows if len(text) <= 40))
    if pdf is None or not 1 <= reading.pdf_page <= len(pdf):
        return direct
    page = pdf[reading.pdf_page - 1]
    source = set()
    for block in page.get_text('dict', flags=0)['blocks']:
        for line in block.get('lines', []):
            text = ''.join(span['text'] for span in line['spans']).strip()
            if MARKED_ENTRY.fullmatch(text):
                x0, _, x1, _ = line['bbox']
                if abs((x0 + x1) / 2 - page.rect.width / 2) <= page.rect.width * .2:
                    source.add(heading_key(text[1:]))
    source -= {heading_key(text[1:]) for _, text in rows if MARKED_ENTRY.fullmatch(text)}
    for key in source:
        candidates = []
        for i, (region, text) in enumerate(rows):
            if region.id in direct:
                continue
            prefix, marked, suffix = text.partition('◎')
            if marked and heading_key(prefix) in seen_chapters and MARKED_ENTRY.fullmatch(marked + suffix):
                if heading_key(suffix) == key:
                    candidates.append((region.id, 'strip-prefix'))
                continue
            match = re.fullmatch(rf'([©⊙])?({HERB_NAME})', text)
            if not match:
                continue
            name = heading_key(match[2])
            if name == key:
                candidates.append((region.id, 'replace-marker' if match[1] else 'add-marker'))
            elif (match[1] and len(name) == len(key) and sum(a != b for a, b in zip(name, key)) == 1
                  and i + 1 < len(rows) and re.search(r'[歸归][^。]{1,18}經|[歸归][^。]{1,18}经', rows[i+1][1])):
                # The OCR has the marker and the following herb summary.
                # Keep its name when the PDF's hidden layer differs by one glyph.
                candidates.append((region.id, 'replace-marker'))
        if len(candidates) == 1:
            region_id, repair = candidates[0]
            direct[region_id] = repair
    return direct


def prepare_records(records, *, common_layout=False, citation=None, headings=None, template_headings=None,
                    source_pdf=None, page_types=None, output_rules=HwpxOutputRules()):
    """Restore paragraphs in local line records using explicit separators.

    Proper structured OCR paragraphs keep their existing boundaries. Local
    line records preserve blank separators; list/contents and imprint rows
    remain separate. All changes are confined to an in-memory output view.
    """
    output = deepcopy(records)
    if common_layout:
        for record in output:
            reading=PageReading.model_validate(record['merged'])
            overrides=record.get('_verified_layout',{})
            for region in reading.regions:
                item=overrides.get(region.id,{}) if isinstance(overrides,dict) else {}
                text=region.title_text or ''.join(paragraph_text(p) for p in region.paragraphs)
                if isinstance(item,dict) and item.get('verified') is True and item.get('text')==text.strip() and item.get('kind') in ('header','footer','page_number','decoration'):
                    region.kind='쪽번호' if item['kind']=='page_number' else '머리말'
            record['merged']=reading.model_dump()
    exclude_marginalia(output, source_pdf=source_pdf)
    if common_layout:
        from .common_layout import infer_record_layout
        infer_record_layout(output)
    if headings or template_headings:
        from .citation_layout import heading_inventory, heading_key
        from .common_layout import is_contents_page
        inventory=heading_inventory(headings or {})
        parent=''
        for record in output:
            reading=PageReading.model_validate(record['merged'])
            contents=is_contents_page(reading)
            for index,region in enumerate(reading.regions):
                text=(region.title_text or ''.join(paragraph_text(p) for p in region.paragraphs)).strip()
                key=heading_key(text) if len(text)<=120 else ''
                role=inventory.get(key)
                template_role,template_parent=(template_headings or {}).get(key,('', ''))
                catalog=template_role=='catalog'
                if catalog:template_role='section' if not contents and _catalog_heading(reading,index,citation) else ''
                member=template_role=='member_subsection'
                if member:template_role='subsection' if not contents and _member_heading(reading,index,citation) else ''
                # A template proves the subtitle level only inside its parent.
                # Explicit book/volume/chapter rules keep their page-start role.
                if template_role and not contents:
                    if template_role!='subsection' or template_parent and (template_parent==parent or isinstance(template_parent,tuple) and parent in template_parent):
                        if role!='chapter': role=template_role
                    elif role=='section':
                        role=None
                if role:
                    if role=='chapter' and contents: role='section'
                    if not contents and role in ('chapter','section'): parent=key
                    record.setdefault('_common_layout_regions',{})[region.id]={'kind':role,'level':{'chapter':1,'section':2,'subsection':3}[role],
                        'alignment':'CENTER','evidence':'template-member-and-source-citation' if member and template_role else 'template-catalog-and-source-layout' if catalog and template_role else 'template-title-style' if template_role else 'book-rule-exact-title'}
    if source_pdf is not None:
        from .contents_layout import bookmark_contents, prepare_contents_pages
        from .index_layout import prepare_list_pages
        types = page_types or {}
        contents_pages = bookmark_contents(source_pdf) | set(types.get('목차', ())) | set(types.get('차례', ()))
        prepare_list_pages(output, source_pdf, set(types.get('색인', ())), contents_pages=contents_pages)
        prepare_contents_pages([r for r in output if not r.get('_source_index_page')], source_pdf, page_types)
    names = known_entries(output)
    from .citation_layout import heading_key
    from .common_layout import is_contents_page
    chapter_keys = {heading_key(title) for title in (headings or {}).get('chapter', ())}
    seen_chapters = set()
    for record in output:
        if record.get('_source_index_page') or record.get('_source_contents_page'):
            continue
        reading = PageReading.model_validate(record['merged'])
        if not is_contents_page(reading):
            for region_id, repair in _marked_entry_hints(reading, source_pdf, chapter_keys, seen_chapters).items():
                hints = record.setdefault('_common_layout_regions', {})
                if hints.get(region_id, {}).get('evidence') != 'image-reviewed-layout':
                    hints[region_id] = {'kind':'entry', 'level':3, 'alignment':'CENTER',
                                        'evidence':'explicit-herb-title-marker', 'entry_marker_repair':repair}
        previous = False
        inferred = []
        list_regions = set(record.get('_source_list_regions', ()))
        for region in reading.regions:
            if not region.id.startswith('local-line-') or region.kind not in TEXT_KINDS or len(region.paragraphs) != 1:
                previous = False
                continue
            paragraph = region.paragraphs[0]
            text = paragraph_text(paragraph).strip()
            if region.id in list_regions:
                paragraph.continues_from_previous = False
                previous = False
                continue
            centered=record.get('_common_layout_regions',{}).get(region.id,{}).get('alignment')=='CENTER'
            if centered:
                paragraph.continues_from_previous=False
            if not text:
                previous = False
                continue
            from .citation_layout import source_end
            citation_start = citation is not None and source_end(text, citation) is not None
            if citation_start: paragraph.continues_from_previous=False
            prefix_title=numbered_source_title(text,template_headings or {})
            if prefix_title:paragraph.continues_from_previous=False
            entry_field_start = output_rules.entry_paragraphs and ENTRY_FIELD.match(text)
            boundary = bool(prefix_title or citation_start or centered or entry_field_start
                            or entry_name(text, names) or TOC.search(text) or META.search(text) or text.isdecimal()
                            or re.match(r'^[\[［【〔](?:唐|陶|今|本|別|别|新)', text))
            if boundary:
                paragraph.continues_from_previous = False
            elif previous:
                paragraph.continues_from_previous = True
                inferred.append(region.id)
            previous = not bool(centered or TOC.search(text) or META.search(text) or text.isdecimal())
        record['merged'] = reading.model_dump()
        record['_document_continuations'] = inferred
    if source_pdf is not None:
        from .source_typography import measure_source_typography
        measure_source_typography(output, source_pdf, infer_titles=common_layout, output_rules=output_rules)
    return output, names


def clean_output_blocks(blocks):
    """Remove application navigation/metadata; keep textual uncertainty marks."""
    output=[]
    for block in deepcopy(blocks):
        if block.type in ('meta','note','uncertain','report'):
            continue
        block.anchors=[]
        if block.type in ('para','heading'):
            _replace(block,r'〔→PDF[^〕]*〕',lambda m,t:'')
        output.append(block)
    return output


def _replace(block, pattern, replacement, *, protect=False):
    text = block.text
    protected = [(m.start(),m.end()) for m in MARK_RE.finditer(text)] if protect else []
    pieces=[]; origins=[]; last=0
    for match in re.finditer(pattern, text):
        if any(a <= match.start() < b for a,b in protected):
            continue
        value = replacement(match, text)
        if value is None:
            continue
        pieces.append(text[last:match.start()]);origins.extend(range(last,match.start()))
        old=match.group();prefix=0
        while prefix<min(len(old),len(value)) and old[prefix]==value[prefix]:
            prefix+=1
        suffix=0
        while suffix<min(len(old)-prefix,len(value)-prefix) and old[-suffix-1]==value[-suffix-1]:
            suffix+=1
        pieces.append(value)
        origins.extend(range(match.start(),match.start()+prefix))
        origins.extend([match.start()+min(prefix,max(0,len(old)-1))]*(len(value)-prefix-suffix))
        origins.extend(range(match.end()-suffix,match.end()))
        last=match.end()
    pieces.append(text[last:]);origins.extend(range(last,len(text)))
    block.remap(''.join(pieces),origins)


def _separate_editorial_notes(block):
    """Keep explicit printed notes, separating them from the quoted body."""
    if block.type!='para':return [block]
    marks=[(m.start(),m.end()) for m in MARK_RE.finditer(block.text)]
    marker=next((m for m in re.finditer(r'【[注註]】',block.text)
                 if not any(a<=m.start()<z for a,z in marks)),None)
    if marker is None:return [block]
    result=[];tail=block
    if marker.start():
        end=len(block.text[:marker.start()].rstrip())
        head,tail=block.cut(end,marker.start())
        if head.text:result.append(head)
    # Numbered explanations are split only after an explicit 注 marker;
    # unrelated bracketed numbers in ordinary prose are left alone.
    positions=[m.start() for m in re.finditer(r'[\[［][0-9０-９]{1,3}[\]］]',tail.text)]
    first=len(marker.group())
    cuts=[p for p in positions if p>first and tail.text[first:p].strip()]
    offset=0
    for position in cuts:
        cut=position-offset
        head,tail=tail.cut(len(tail.text[:cut].rstrip()),cut)
        head.role='quotation' if block.role=='citation_source' else block.role
        result.append(head);offset=position
    tail.role='quotation' if block.role=='citation_source' else block.role
    result.append(tail)
    return result


def _separate_entry_fields(block):
    """Split explicit entry labels in records that already contain fused text."""
    if block.type != 'para' or block.kind == '소주' or block.layout.keys() & {'source_contents', 'source_index', 'front_row'}:
        return [block]
    positions = [match.start() for match in ENTRY_FIELD.finditer(block.text) if match.start()]
    result, tail, offset = [], block, 0
    for position in positions:
        cut = position - offset
        head, tail = tail.cut(len(tail.text[:cut].rstrip()), cut)
        result.append(head)
        shape = dict(tail.layout.get('source_typography', {}))
        shape.pop('before', None)
        tail.layout = dict(tail.layout, page_break_before=False, source_typography=shape)
        if tail.role in ('entry', 'citation_source'):
            tail.role = 'body'
        offset = position
    return [*result, tail]


def format_blocks(blocks, names=(), *, punctuation=True, titles=None, output_rules=HwpxOutputRules()):
    """Inline herb headings, keep one blank paragraph, and remap all evidence."""
    blocks=deepcopy(blocks); separated=[]
    for block in blocks:
        title=(numbered_source_title(block.text,titles) or fused_entry_title(block.text,titles)) if block.type=='para' and not (block.layout.keys() & {'source_contents','front_row'}) else None
        if title:
            end,role=title;head,tail=block.cut(end)
            head.type='heading';head.role=role;head.level={'chapter':1,'section':2,'subsection':3}[role]
            head.layout={'kind':role,'alignment':'CENTER','keep_with_next':True,'page_break_before':role=='chapter',
                         'evidence':'numbered-source-prefix-and-template-title'}
            separated.append(head)
            if tail.text.strip():
                tail.role=block.role if block.role not in ('entry','citation_source') else 'body'
                tail.layout=dict(tail.layout,alignment='JUSTIFY',page_break_before=False)
                separated.append(tail)
        else:separated.append(block)
    blocks=separated;joined=[]; i=0
    while i<len(blocks):
        b=blocks[i]
        if (b.type=='heading' and b.role=='entry' and b.layout.get('kind') != 'entry'
                and i+1<len(blocks) and blocks[i+1].type=='para' and 'source_contents' not in blocks[i+1].layout):
            body=blocks[i+1]
            head=deepcopy(b);head.extend(' ')
            b=Block.joined(head,body);b.type='para';b.role='entry';i+=1
        joined.append(b);i+=1
    if output_rules.entry_paragraphs:
        joined = [part for block in joined for part in _separate_entry_fields(block)]
    output=[]; stats={'entries':0,'removed_line_breaks':0,'blank_paragraphs':0,'removed_character_spaces':0}
    for b in (part for block in joined for part in ([block] if 'source_contents' in block.layout else _separate_editorial_notes(block))):
        if 'source_contents' in b.layout:
            output.append(b)
            continue
        if b.type in ('para','heading'):
            if suffix := b.layout.get('printed_folio_suffix'):
                _replace(b, re.escape(suffix) + r'\s*$', lambda m,t: '')
            if b.type == 'heading' and b.layout.get('kind') == 'entry':
                repair = b.layout.get('entry_marker_repair')
                if repair == 'strip-prefix':
                    _replace(b, r'^[^◎]+(?=◎)', lambda m,t:'')
                elif repair == 'replace-marker':
                    _replace(b, r'^[©⊙]', lambda m,t:'◎')
                elif repair == 'add-marker' and not b.text.startswith('◎'):
                    _replace(b, r'^', lambda m,t:'◎')
            stats['removed_line_breaks'] += len(re.findall(r'\r\n|\r|\n',b.text))
            def linebreak(m,text):
                left=text[m.start()-1] if m.start() else ''
                right=text[m.end()] if m.end()<len(text) else ''
                return ' ' if left and right and left.isascii() and right.isascii() and left.isalnum() and right.isalnum() else ''
            _replace(b,r'[ \t]*(?:\r\n|\r|\n)[ \t]*',linebreak)
            # A spaced OCR alphabet is different from paragraph justification.
            # Require many predominantly single-Han tokens for prose; short
            # proven headings/citation labels have their own structural proof.
            tokens=re.findall(rf'[{HAN}]+',b.text)
            spaced=len(re.findall(rf'(?<=[{HAN}])[ \t\u3000]+(?=[{HAN}])',b.text))
            dense=spaced>=8 and len(tokens)>=12 and sum(len(t)==1 for t in tokens)/len(tokens)>=.65
            structural=b.type=='heading' or b.role=='citation_source'
            if (dense or structural) and not re.search(r'[가-힣ᄀ-ᇿㄱ-ㆎ]',b.text):
                before=len(b.text)
                _replace(b,rf'(?<=[{HAN}])[ \t\u3000]+(?=[{HAN}])',lambda m,t:'',protect=True)
                _replace(b,rf'(?<=[{HAN}])[ \t\u3000]+(?=[,，、;；:：。.!?！？』》」)）])',lambda m,t:'',protect=True)
                _replace(b,rf'(?<=[『《「(（])[ \t\u3000]+(?=[{HAN}])',lambda m,t:'',protect=True)
                stats['removed_character_spaces']+=before-len(b.text)
            name=entry_name(b.text,names) if b.type=='para' and b.role not in ('citation_source','quotation') else ''
            if name:
                _replace(b,rf'^{re.escape(name)}[ \t\u3000]*',lambda m,t:name+' ')
                b.role='entry'
            if b.type=='para' and b.role=='entry':
                if output and output[-1].type!='spacer':
                    output.append(Block(type='spacer',page=b.page));stats['blank_paragraphs']+=1
                stats['entries']+=1
            source_title=r'(?:『[^『』\r\n]+』|《[^《》\r\n]+》)'
            number=r'[0-9０-９]{1,3}\s*[.．、)）]\s*'
            prefix=rf'(?:{number})?' if b.role=='citation_source' else number
            if re.fullmatch(rf'\s*{prefix}(?:『綱目』|《綱目》)[^\S\r\n]*(?:[:：][^\S\r\n]*)?',b.text):
                _replace(b,r'(?<=[『《])綱目(?=[』》])',lambda m,t:'本草綱目',protect=True)
            if punctuation:
                # A contents leader connects an entry to a page number; it is
                # not an omission mark in prose. Keep it available to TOC checks.
                if not TOC.search(b.text):
                    _replace(b,r'[ \t]*(?:…(?:[ \t]*…)*|⋯(?:[ \t]*⋯)*|[.．]{3,}|[·]{3,})[ \t]*',
                             lambda m,t:' … ',protect=True)
                def comma(m,text):
                    # Preserve an existing numeric separator, including 1,000.
                    if m.end()==m.start()+1 and m.start()>0 and m.end()<len(text) and text[m.start()-1].isdigit() and text[m.end()].isdigit():
                        return None
                    return ', '
                _replace(b,r',[^\S\r\n]*',comma,protect=True)
                # Citation labels: 元素 曰 : 原文 → 元素曰: 原文.
                # Restrict this to Chinese labels; times, drive paths and URLs
                # retain their own colon conventions.
                _replace(b,rf'(?<=[{HAN}])[^\S\r\n]+(?=曰[^\S\r\n]*[:：])',lambda m,t:'',protect=True)
                _replace(b,rf'(?<=[{HAN}》』」])[^\S\r\n]*[:：][^\S\r\n]*',lambda m,t:': ',protect=True)
                # A source label has its quotation on the following paragraph;
                # its trailing colon is redundant. Keep inline 曰: 原文 intact.
                if re.fullmatch(rf'\s*{prefix}{source_title}[^\S\r\n]*[:：][^\S\r\n]*',b.text):
                    _replace(b,r'[^\S\r\n]*[:：][^\S\r\n]*$',lambda m,t:'',protect=True)
        output.append(b)
    return output,stats
