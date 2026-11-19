"""Output-only citation paragraphs. No recognition, renumbering or title expansion."""
from copy import deepcopy
from dataclasses import replace
import re
from functools import lru_cache

PREFIX = re.compile(r'^\s*(?:[0-9０-９]{1,3}\s*[.．、)）]\s*)?(?:《[^《》\r\n]{1,120}》|『[^『』\r\n]{1,120}』)')
NUMBER = re.compile(r'^\s*[0-9０-９]{1,3}\s*[.．、)）]')
CATALOG = re.compile(r'[.·…]{3,}\s*[（(]?\d{1,4}[）)]?\s*$')
EXCLUDED = {'目錄','目录','目次','索引','引用書目','引用书目','參考文獻','参考文献','參考書目','参考书目','書目','书目'}
PARTS = {'前言','序言','綜合論述','综合论述','各論','各论'}


@lru_cache(maxsize=8192)
def heading_key(text):
    import unicodedata
    from .collate.zh import _cc
    from .korean_forms import korean_forms
    text=korean_forms(_cc('s2t').convert(unicodedata.normalize('NFKC',text)))
    key=re.sub(r'[\s·.,，。、:：()（）《》『』\[\]「」]','',text)
    prefix=re.match(r'^[一二三四五六七八九十百千〇○零O0-9]+',key)
    if prefix and re.search(r'[一二三四五六七八九十百千〇○零]',prefix[0]):
        key=prefix[0].translate(str.maketrans({'○':'〇','零':'〇','O':'〇','0':'〇'}))+key[prefix.end():]
    return key


def heading_inventory(headings):
    inventory={}
    for role,titles in headings.items():
        for text in titles:
            key=heading_key(text)
            if key in inventory and inventory[key]!=role:
                inventory[key]=None  # ambiguous level: leave source structure alone
            else: inventory.setdefault(key,role)
    return inventory


def source_end(text, rule):
    """Return a proven source-prefix boundary, retaining original punctuation."""
    if CATALOG.search(text):
        return None
    match = PREFIX.match(text)
    if not match:
        return None
    title = re.search(r'[《『](.*)[》』]$', match.group(0))
    if not title or len(title[1]) > rule.max_title or re.search(r'[。!?？！〔〕]', title[1]):
        return None
    end = match.end()
    colon = re.match(r'[ \t]*[:：]', text[end:])
    if colon:
        return end + colon.end()
    if text[end:].strip() and not NUMBER.match(text):
        return None
    return end


def apply_citations(blocks, rule):
    output = []; active = False; excluded = False
    items = deepcopy(blocks)
    for i, block in enumerate(items):
        compact = re.sub(r'\s', '', block.text)
        if compact in EXCLUDED:
            excluded = True; active = False
        elif compact in PARTS:
            excluded = False; active = False
        if block.type == 'heading':
            active = False
        if block.type != 'para' or block.kind == '소주' or excluded or CATALOG.search(block.text):
            output.append(block)
            if block.type in ('table', 'image') or CATALOG.search(block.text): active = False
            continue
        end = source_end(block.text, rule)
        if end is not None and not block.text[end:].strip():
            # A sequence of book names is a bibliography, not empty quotations.
            following = next((b for b in items[i+1:] if b.type not in ('spacer','meta') and b.text.strip()), None)
            closes_herb = (following is not None and following.type == 'heading'
                           and following.layout.get('kind') == 'entry' and output
                           and output[-1].type == 'para' and output[-1].role != 'citation_source')
            if not closes_herb and (following is None or following.type != 'para' or source_end(following.text, rule) is not None):
                end = None
                active = False
        if end is not None:
            start = end
            while start < len(block.text) and block.text[start].isspace(): start += 1
            head, body = block.cut(end, start)
            head.role = 'citation_source'
            head.layout = dict(alignment='LEFT', keep_with_next=True, page_break_before=False)
            output.append(head)
            active = True
            if body.text:
                body.role = 'quotation'; body.layout = dict(alignment='JUSTIFY', page_break_before=False)
                output.append(body)
        else:
            if active and rule.continuations:
                block.role = 'quotation'
                block.layout = dict(alignment='JUSTIFY', page_break_before=False)
            # Keep malformed source text and expose its uncertainty in existing red ink.
            if NUMBER.match(block.text) and re.search(r'[《『]', block.text[:12]) and not PREFIX.match(block.text):
                block.spans.append((0, min(len(block.text), rule.max_title+12), 'pending'))
                active = False
            output.append(block)
    return output
