"""Conservative output-only book layout. Never recognizes or changes letters."""
from __future__ import annotations
from collections import Counter
from copy import deepcopy
import re
from .schema import PageReading, paragraph_text, TITLE_KINDS, TEXT_KINDS

NUM=r'[0-9一二三四五六七八九十百千零〇壹貳參肆伍陸柒捌玖拾일이삼사오육칠팔구십백천IVXLCDM]+'
MAJOR=re.compile(rf'^(?:[^。.!?？！,，:：]{{0,32}}卷\s*第?\s*{NUM}|第\s*{NUM}\s*[卷章編编篇部](?:[\s:：.-]*[^。.!?？！]{{1,32}})?|제\s*{NUM}\s*[권장편](?:[\s:：.-]*[^.!?]{{1,40}})?|(?:book|volume|part|chapter)\s+{NUM}(?:[\s:.-]+[^.!?]{{1,50}})?)$',re.I)
SECTION=re.compile(rf'^(?:第\s*{NUM}\s*[節节項项](?:[\s:：.-]*[^。.!?？！]{{1,32}})?|제\s*{NUM}\s*절(?:[\s:：.-]*[^.!?]{{1,40}})?|section\s+{NUM}(?:[\s:.-]+[^.!?]{{1,50}})?)$',re.I)
FRONT={'序','序言','前言','凡例','目錄','目录','總目','总目','內容提要','内容提要','索引','後記','后记','跋','輯復本','辑复本','서문','목차','일러두기','후기','目次','序文','はじめに','あとがき','Contents','Preface','Introduction','Index'}
SENTENCE=re.compile(r'[。.!?？！,，;；]')


def is_contents_page(reading):
    texts=[paragraph_text(p).strip() for r in reading.regions for p in r.paragraphs if paragraph_text(p).strip()]
    return sum(bool(re.search(r'[·.…]{2,}|(?:本[經经]|[別别][錄录]|今附)\s*\d{1,4}$',t)) for t in texts)>=5


def infer_record_layout(records):
    """Explicit syntax and source boundaries establish headings. Coarse
    full-page boxes never establish centering; repeated titles stay body."""
    readings=[PageReading.model_validate(r['merged']) for r in records]
    occurrences=Counter()
    for reading in readings:
        occurrences.update({paragraph_text(p).strip() for r in reading.regions for p in r.paragraphs if paragraph_text(p).strip()})
    book_titles=set()
    for record,reading in zip(records,readings):
        texts=[paragraph_text(p).strip() for r in reading.regions for p in r.paragraphs if paragraph_text(p).strip()]
        if record['pdf_page']<=2 and 1<len(texts)<=10 and len(texts[0])<=40 and any(re.search(r'出版社|出版|출판',t) for t in texts):
            book_titles.add(texts[0])
    for record,reading in zip(records,readings):
        hints={};candidates=[]
        texts=[paragraph_text(p).strip() for r in reading.regions for p in r.paragraphs if paragraph_text(p).strip()]
        cover=bool(record['pdf_page']<=2 and 1<len(texts)<=10 and any(re.search(r'出版社|出版|출판',t) for t in texts))
        contents=is_contents_page(reading)
        if not contents:
            for left,right in zip(reading.regions,reading.regions[1:]):
                if left.kind not in TEXT_KINDS or right.kind not in TEXT_KINDS or len(left.paragraphs)!=1 or len(right.paragraphs)!=1:continue
                a=paragraph_text(left.paragraphs[0]).strip();b=paragraph_text(right.paragraphs[0]).strip()
                ly,lx,lb,lr=left.bbox;ry,rx,rb,rr=right.bbox
                overlap=min(lb,rb)-max(ly,ry)
                if a and b and left.bbox!=[0,0,1000,1000] and right.bbox!=[0,0,1000,1000] and min(lb-ly,rb-ry)>=35 and overlap>=.6*min(lb-ly,rb-ry) and 0<=rx-lr<=60 and MAJOR.fullmatch(a+' '+b):
                    group=left.id+'+'+right.id
                    for item in (left,right):hints[item.id]={'kind':'volume','level':1,'alignment':'CENTER','group':group,'evidence':'measured-same-line-title-fragments'}
        for index,region in enumerate(reading.regions):
            overrides=record.get('_verified_layout',{})
            verified=overrides.get(region.id,{}) if isinstance(overrides,dict) else {}
            if not isinstance(verified,dict):verified={}
            current=region.title_text or ''.join(paragraph_text(p) for p in region.paragraphs)
            if verified.get('verified') is True and verified.get('text')==current.strip() and verified.get('kind') in ('book','volume','chapter','section','subsection','body'):
                kind=verified['kind'];alignment=verified.get('alignment','CENTER')
                if alignment not in ('CENTER','LEFT','RIGHT','JUSTIFY'):alignment='CENTER'
                hints[region.id]={'kind':kind,'level':1 if kind in ('book','volume','chapter') else 3 if kind=='subsection' else 2,'alignment':alignment,'evidence':'image-reviewed-layout'}
            if region.id in hints:continue
            if region.kind in TITLE_KINDS:
                section=region.title_text.strip() in FRONT or bool(SECTION.fullmatch(region.title_text.strip()))
                hints[region.id]={'kind':'chapter' if region.title_level==1 and not section else 'section','alignment':'CENTER','evidence':'structured-title'}
                continue
            if region.kind not in TEXT_KINDS or len(region.paragraphs)!=1:continue
            text=paragraph_text(region.paragraphs[0]).strip()
            if not text or len(text)>90 or SENTENCE.search(text):continue
            if re.search(r'[·.…]{2,}|\d\s*$',text) and not MAJOR.fullmatch(text):continue
            before=index==0 or not any(paragraph_text(p).strip() for p in reading.regions[index-1].paragraphs)
            after=index==len(reading.regions)-1 or not any(paragraph_text(p).strip() for p in reading.regions[index+1].paragraphs)
            kind=''
            y0,x0,y1,x1=region.bbox
            if text in book_titles and (before or after) and (cover or region.bbox!=[0,0,1000,1000] and y0>=100 and y1<=900):kind='book'
            elif not contents and not re.match(r'^\d{1,4}\s*\D',text) and (before or after) and occurrences[text]<=2 and MAJOR.fullmatch(text):
                kind='volume' if '卷' in text or re.match(r'volume\b',text,re.I) else 'chapter'
            elif before and after and (re.sub(r'\s','',text) in FRONT or SECTION.fullmatch(text) or any(name in text and re.search(r'序$|目錄|目录|考订$|考訂$',text) for name in book_titles)):
                kind='section'
            if kind:
                hints[region.id]={'kind':kind,'level':1 if kind in ('book','volume','chapter') else 2,'alignment':'CENTER','evidence':'cover-context' if kind=='book' else 'explicit-heading-and-paragraph-boundary'}
                continue
            y0,x0,y1,x1=region.bbox
            if before and after and 30<x1-x0<650 and y0>=100 and y1<=900 and abs((x0+x1)/2-500)<=35:
                hints[region.id]={'kind':'body','alignment':'CENTER','evidence':'measured-isolated-centered-paragraph'}
            elif (before or after) and len(text)<=40 and (text in book_titles or re.search(r'卷|章|節|节|序|目錄|目录|凡例',text)):
                candidates.append({'region':region.id,'text':text,'reason':'제목의 등급·배치 확인 필요 — 임의로 정렬/쪽 나눔하지 않음'})
        record['merged']=reading.model_dump()
        record['_common_layout_regions']=hints
        record['_common_layout_review']=candidates
    return records


def apply_common_layout(blocks):
    """Center headings; page-start books/volumes/chapters, never sections."""
    joined=[]
    for block in deepcopy(blocks):
        if joined and block.layout.get('group') and block.layout.get('group')==joined[-1].layout.get('group'):
            left=joined.pop();left.extend(' ')
            block=type(block).joined(left,block)
        joined.append(block)
    blocks=joined
    stats={'centered_headings':0,'centered_body':0,'chapter_page_breaks':0,'widow_orphan_protection':0}
    seen=False;heading_chain=False
    for block in blocks:
        hint=block.layout
        if block.type=='heading':
            kind=hint.get('kind') or ('chapter' if block.level<=1 else 'section')
            hint.update(alignment='CENTER',keep_with_next=True,widow_orphan=True,keep_lines=False)
            hint['page_break_before']=bool(seen and kind in ('book','volume','chapter') and block.kind not in ('그림제목','표제목'))
            stats['centered_headings']+=1;stats['chapter_page_breaks']+=int(hint['page_break_before'])
            heading_chain=True
        elif block.type=='para':
            follows_title=block.role=='citation_source' or bool(heading_chain and hint.get('alignment')=='CENTER' and len(block.text)<=90)
            hint.update(keep_with_next=follows_title,widow_orphan=True,keep_lines=False,page_break_before=False)
            stats['widow_orphan_protection']+=1
            if hint.get('alignment')=='CENTER':stats['centered_body']+=1
            heading_chain=follows_title
        elif block.type=='spacer':hint.update(keep_with_next=heading_chain,widow_orphan=False,keep_lines=False)
        if block.text.strip() or block.type in ('image','table'):seen=True
    return blocks,stats
