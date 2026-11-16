"""Reversible document-only paragraph and spacing policy. Never saves OCR records."""
from __future__ import annotations
from copy import deepcopy
import re
from collections import Counter
from .blocks import Block
from .schema import PageReading, paragraph_text, TEXT_KINDS, SKIP_KINDS
from .marks import MARK_RE

HAN = r'\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff'
ENTRY = re.compile(rf'^([{HAN}]{{1,6}})[ \t\u3000]+(?=(?:味|一名|性|氣|气)[{HAN}])')
BAD_START = '凡又其此是以故若則则乃每諸诸'
TOC = re.compile(r'[.·…．]{2,}.*\d+\s*$')
META = re.compile(r'ISBN|出版社|出版发行|出版發行|邮政|郵政|责任编辑|責任編輯|开本|開本|印刷|定价|定價')


def _edge_rows(reading):
    rows=[(r,paragraph_text(r.paragraphs[0]).strip()) for r in reading.regions if len(r.paragraphs)==1 and paragraph_text(r.paragraphs[0]).strip()]
    return rows[:2]+rows[-2:]


def _header_signature(text):
    return re.sub(r'[\d\s·.―—-]','',text)


def exclude_marginalia(records):
    """Exclude identified running matter in the output view, never raw OCR.

    Repetition is counted per page. A line of body text containing the book
    title is never removed: a short title line must be at a page edge and
    either have a page-number prefix or a measured margin position.
    """
    readings=[PageReading.model_validate(r['merged']) for r in records]
    signatures=Counter()
    for reading in readings:
        signatures.update({_header_signature(t) for _,t in _edge_rows(reading) if re.fullmatch(rf'[\d\s{HAN}·.―—-]{{1,60}}',t)})
    for record,reading in zip(records,readings):
        edges={r.id for r,_ in _edge_rows(reading)}
        removed=[];keep=[]
        for region in reading.regions:
            text=paragraph_text(region.paragraphs[0]).strip() if len(region.paragraphs)==1 else ''
            reason=''
            if region.kind in SKIP_KINDS:
                reason='판독에서 분류한 머리말/쪽번호'
            elif text and region.id in edges:
                y0,x0,y1,x1=region.bbox
                margin=y1<=100 or y0>=900 or x1<=60 or x0>=940
                signature=_header_signature(text)
                short=bool(re.fullmatch(rf'[\d\s{HAN}·.―—-]{{1,60}}',text))
                numbered=bool(re.match(r'^\d{1,4}\s*[^\d\s]',text))
                title_like=bool(re.search(r'[开開]?[宝寶]本[草艸].*(?:卷|[目日]录|目錄)',signature))
                declared=bool(reading.running_header.strip() and signature==_header_signature(reading.running_header))
                if text.isdecimal() and margin:
                    reason='여백의 쪽번호'
                elif short and ((declared and (margin or numbered)) or
                     (title_like and (numbered or margin)) or
                     (signatures[signature]>=3 and (numbered or margin))):
                    reason='쪽 가장자리의 머리말/꼬리말'
            if reason:
                removed.append({'region':region.id,'text':text,'reason':reason})
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


def prepare_records(records):
    """Restore paragraphs in local line records using explicit separators.

    Proper structured OCR paragraphs keep their existing boundaries. Local
    line records preserve blank separators; list/contents and imprint rows
    remain separate. All changes are confined to an in-memory output view.
    """
    output = deepcopy(records)
    exclude_marginalia(output)
    names = known_entries(output)
    for record in output:
        reading = PageReading.model_validate(record['merged'])
        previous = False
        inferred = []
        for region in reading.regions:
            if not region.id.startswith('local-line-') or region.kind not in TEXT_KINDS or len(region.paragraphs) != 1:
                previous = False
                continue
            paragraph = region.paragraphs[0]
            text = paragraph_text(paragraph).strip()
            if not text:
                previous = False
                continue
            boundary = bool(entry_name(text, names) or TOC.search(text) or META.search(text) or text.isdecimal()
                            or re.match(r'^[\[［【〔](?:唐|陶|今|本|別|别|新)', text))
            if previous and not boundary:
                paragraph.continues_from_previous = True
                inferred.append(region.id)
            previous = not bool(TOC.search(text) or META.search(text) or text.isdecimal())
        record['merged'] = reading.model_dump()
        record['_document_continuations'] = inferred
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


def format_blocks(blocks, names=(), *, punctuation=True):
    """Inline herb headings, keep one blank paragraph, and remap all evidence."""
    blocks=deepcopy(blocks); joined=[]; i=0
    while i<len(blocks):
        b=blocks[i]
        if b.type=='heading' and b.role=='entry' and i+1<len(blocks) and blocks[i+1].type=='para':
            body=blocks[i+1]
            head=deepcopy(b);head.extend(' ')
            b=Block.joined(head,body);b.type='para';b.role='entry';i+=1
        joined.append(b);i+=1
    output=[]; stats={'entries':0,'removed_line_breaks':0,'blank_paragraphs':0}
    for b in joined:
        if b.type in ('para','heading'):
            stats['removed_line_breaks'] += len(re.findall(r'\r\n|\r|\n',b.text))
            def linebreak(m,text):
                left=text[m.start()-1] if m.start() else ''
                right=text[m.end()] if m.end()<len(text) else ''
                return ' ' if left and right and left.isascii() and right.isascii() and left.isalnum() and right.isalnum() else ''
            _replace(b,r'[ \t]*(?:\r\n|\r|\n)[ \t]*',linebreak)
            name=entry_name(b.text,names) if b.type=='para' else ''
            if name:
                _replace(b,rf'^{re.escape(name)}[ \t\u3000]*',lambda m,t:name+' ')
                b.role='entry'
            if b.type=='para' and b.role=='entry':
                if output and output[-1].type!='spacer':
                    output.append(Block(type='spacer',page=b.page));stats['blank_paragraphs']+=1
                stats['entries']+=1
            if punctuation:
                def comma(m,text):
                    # Preserve an existing numeric separator, including 1,000.
                    if m.end()==m.start()+1 and m.start()>0 and m.end()<len(text) and text[m.start()-1].isdigit() and text[m.end()].isdigit():
                        return None
                    return ', '
                _replace(b,r',[^\S\r\n]*',comma,protect=True)
        output.append(b)
    return output,stats
