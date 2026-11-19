"""Pinned HWPX transcription as advisory context, never image evidence."""
from array import array
from bisect import bisect_left
from collections import Counter
from difflib import SequenceMatcher
from functools import lru_cache
import hashlib
from pathlib import Path
import re
import threading
import unicodedata
from xml.etree import ElementTree as ET
from zipfile import ZipFile

from .collate.zh import script_fold


@lru_cache(maxsize=8192)
def _char(value):
    return ''.join(c for c in script_fold(unicodedata.normalize('NFKC', value)) if c.isalnum())


def _fold(text):
    positions = array('I')
    parts = []
    for i, char in enumerate(text):
        value = _char(char)
        parts.append(value)
        positions.extend([i]*len(value))
    return ''.join(parts), positions


class ReferenceOCR:
    def __init__(self, path):
        path = Path(path).expanduser().resolve(strict=True)
        if path.stat().st_size > 64_000_000:
            raise ValueError('참고 HWPX 파일이 너무 큽니다(최대 64 MB).')
        before = path.read_bytes()
        self.path, self.sha256 = path, hashlib.sha256(before).hexdigest()
        from io import BytesIO
        texts = []
        with ZipFile(BytesIO(before)) as package:
            sections = [p for p in package.infolist() if re.fullmatch(r'Contents/section\d+\.xml', p.filename)]
            if not sections or sum(p.file_size for p in sections) > 64_000_000:
                raise ValueError('참고 HWPX 본문의 크기 또는 구조를 확인하세요.')
            for section in sorted(sections, key=lambda p:int(re.search(r'\d+',p.filename).group())):
                root = ET.fromstring(package.read(section))
                hp = '{http://www.hancom.co.kr/hwpml/2011/paragraph}'
                texts.extend(''.join(n.text or '' for n in p.iter(hp+'t')) for p in root.findall(hp+'p'))
        self.text = '\n'.join(texts)
        if not self.text.strip() or len(self.text) > 8_000_000:
            raise ValueError('참고 HWPX에서 유효한 본문을 읽지 못했습니다.')
        self.folded, self.positions = _fold(self.text)
        self._matches, self._lock = {}, threading.Lock()

    def _match(self, text):
        key = hashlib.sha256(text.encode('utf-8')).hexdigest()
        with self._lock:
            if key in self._matches:
                return self._matches[key]
        query, positions = _fold(text)
        if len(query) > 20_000:
            return None
        votes, anchors = Counter(), 0
        for i in range(0, max(0,len(query)-12), max(24,len(query)//60)):
            gram = query[i:i+12]
            pos = self.folded.find(gram)
            if pos >= 0 and self.folded.find(gram,pos+1) < 0:
                anchors += 1
                votes[(pos-i)//300] += 1
        best = None
        if anchors >= 3:
            for offset,_ in votes.most_common(4):
                start = max(0,offset*300-500)
                end = min(len(self.folded),(offset+1)*300+len(query)+500)
                blocks = SequenceMatcher(None,query,self.folded[start:end],autojunk=False).get_matching_blocks()[:-1]
                coverage = sum(b.size for b in blocks)/max(1,len(query))
                if coverage >= .8 and (best is None or coverage > best['coverage']):
                    best = dict(coverage=coverage,anchors=anchors,blocks=blocks,start=start,positions=positions)
        with self._lock:
            if len(self._matches) >= 128:
                self._matches.pop(next(iter(self._matches)))
            self._matches[key] = best
        return best

    def hint(self, batch):
        match = self._match(batch['snapshot_text'])
        if match is None:
            return None
        excerpts = []
        for target in batch['targets']:
            at = bisect_left(match['positions'],target['start'])
            block = min(match['blocks'],key=lambda b:0 if b.a<=at<b.a+b.size else min(abs(at-b.a),abs(at-b.a-b.size)))
            if not block.a-80 <= at <= block.a+block.size+80:
                continue
            center = max(0,min(len(self.positions)-1,match['start']+block.b+(at-block.a)))
            left,right = max(0,center-80),min(len(self.positions)-1,center+120)
            excerpts.append({'target_id':target['id'],'text':self.text[self.positions[left]:self.positions[right]+1]})
        if not excerpts:
            return None
        return {'document_name':self.path.name,'document_sha256':self.sha256,
                'role':'untrusted-secondary-OCR; advisory only; never ground truth',
                'normalized_alignment_coverage':round(match['coverage'],4),'unique_anchors':match['anchors'],
                'excerpts':excerpts,'image_checks_skipped':0}

    def page_context(self, text):
        """Return only an aligned passage for structural analysis, never guess by page number."""
        match = self._match(text)
        if match is None or not match['blocks']:
            return None
        left = match['start'] + min(block.b for block in match['blocks'])
        right = match['start'] + max(block.b + block.size for block in match['blocks'])
        start = self.positions[max(0, left - 80)]
        end = self.positions[min(len(self.positions) - 1, right + 120)] + 1
        passage = self.text[start:end]
        return {'document_name': self.path.name, 'document_sha256': self.sha256,
                'role': 'untrusted-secondary-OCR; advisory only; never ground truth',
                'normalized_alignment_coverage': round(match['coverage'], 4),
                'unique_anchors': match['anchors'], 'text': passage[:16000],
                'truncated': len(passage) > 16000}
