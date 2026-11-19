"""Read title hierarchy from the selected HWPX; never change recognition data."""
from collections import defaultdict
from zipfile import ZipFile
import re
import unicodedata
from xml.etree import ElementTree as ET

from .citation_layout import heading_key
from .ziplimits import check_archive, read_member

HH = '{http://www.hancom.co.kr/hwpml/2011/head}'
HP = '{http://www.hancom.co.kr/hwpml/2011/paragraph}'


def title_keys(text):
    normalized=unicodedata.normalize('NFKC',text)
    return {heading_key(normalized),heading_key(normalized.replace('蔘','參'))}


def read_template_headings(path, preset):
    """Only named title styles are evidence, not font size or short text.

    Subtitles retain their parent. An isolated 桂 elsewhere in a quotation
    must not become a title merely because another entry has a 桂 subtitle.
    Conflicting occurrences are left to the original OCR structure.
    """
    if not path or not preset:
        return {}
    candidates = defaultdict(set)
    catalog=set()
    members=defaultdict(set)
    with ZipFile(path) as archive:
        check_archive(archive)
        header = ET.fromstring(read_member(archive, 'Contents/header.xml'))
        styles = {s.get('id'): s.get('name') for s in header.iter(HH+'style') if s.get('type') == 'PARA'}
        roles = {}
        for role in ('chapter', 'section', 'subsection'):
            for name in preset.para_styles.get(role, ()):
                roles.setdefault(name, role)
        parent = ''
        sections = sorted((n for n in archive.namelist() if re.fullmatch(r'Contents/section\d+\.xml', n)),
                          key=lambda n: int(re.search(r'\d+', n)[0]))
        for name in sections:
            root = ET.fromstring(read_member(archive, name))
            for paragraph in root.findall(HP+'p'):
                style=styles.get(paragraph.get('styleIDRef')) or ''
                role = roles.get(style)
                text = ''.join(t.text or '' for t in paragraph.iter(HP+'t')).strip()
                if style.casefold().startswith(('목차','toc','contents')) and text and len(text)<=120:
                    catalog.add(heading_key(text))
                    # An index may list 百部(百部), while the scan prints 百部.
                    alias=unicodedata.normalize('NFKC',text).split('(',1)[0].strip()
                    if re.fullmatch(r'[\u3400-\u9fff]{1,12}',alias):catalog.add(heading_key(alias))
                if not role or not text or len(text) > 120:
                    continue
                key = heading_key(text)
                if role != 'subsection':
                    parent = key
                candidates[key].add((role, parent if role == 'subsection' else ''))
                if role=='section':
                    for alias in title_keys(text):candidates[alias].add((role,''))
                    match=re.fullmatch(r'[^()]+\((?:附[:：]?)?([^()]+)\)',unicodedata.normalize('NFKC',text))
                    if match:
                        for member in re.split(r'[、,，·・;；]',match[1]):
                            member=member.strip()
                            if re.fullmatch(r'[\u3400-\u9fff]{1,12}',member):
                                for alias in title_keys(member):members[alias].update(title_keys(text))
    result={}
    for key,values in candidates.items():
        if len(values)==1:
            result[key]=next(iter(values))
        elif all(role=='subsection' for role,parent in values):
            result[key]=('subsection',tuple(sorted(parent for role,parent in values)))
    for key in catalog:
        if key and key not in candidates:result[key]=('catalog','')
    for key,parents in members.items():
        if key and key not in candidates:result[key]=('member_subsection',tuple(sorted(parents)))
    return result
