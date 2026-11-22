"""Bridge local OCR text and full-page geometry to existing document readings.

The text is authoritative: word boxes locate it, never reconstruct or reorder it.
Local recognition supplies no evidence for headings, figures, or table structure.
"""
from __future__ import annotations

import bisect
import difflib
import math
import re


def corrected_output_records(records, source):
    """Overlay current comparison edits on a transient output view only."""
    import copy
    from pathlib import Path
    from .comparison import ComparisonProject, ComparisonError
    from .punctuation_review import annotate_cosmetic_commas
    from .ai_priority import cached_priority
    from .ocr_disagreement import narrow_saved_grades
    projects={};output=[];ai_inventory={}
    for record in records:
        reference=(record.get('local_first') or {}).get('project_path')
        if not reference:
            output.append(record);continue
        key=str(Path(reference).resolve())
        if key not in projects:
            projects[key]=ComparisonProject.load(key,source_path=source,paged=True,lazy_pages=True)
        project=projects[key];number=record['pdf_page'];page=project.page(number,discard_cached=True)
        if project.source_sha256!=record['local_first'].get('source_sha256'):
            raise ComparisonError('교정 프로젝트와 판독 기록의 원본을 확인하세요.')
        recovered=_cached_catalog_output(record,page,Path(reference))
        if recovered is not None:
            output.append(recovered);continue
        if page.get('corrected_text') is None:
            preferred=cached_priority(narrow_saved_grades(record,page),page,Path(reference),ai_inventory)
            output.append(annotate_cosmetic_commas(preferred,page));continue
        baseline=next((r for r in page['runs'] if r['id']==page['baseline_run_id']),None)
        if baseline is None or project.source_sha256!=record['local_first'].get('source_sha256'):
            raise ComparisonError('교정 프로젝트와 판독 기록의 원본·기준 판독을 확인하세요.')
        reading=reading_from_run(baseline['raw'],number,'horizontal' if record.get('mode')=='horizontal_zh' else 'vertical',text=page['corrected_text'])
        current=copy.deepcopy(record);current['merged']['regions']=reading.model_dump()['regions']
        output.append(annotate_cosmetic_commas(current,page))
    return output


def _cached_catalog_output(record,page,reference):
    """Reuse only an image-keyed completed answer, never request a new one."""
    import copy,json
    from PIL import Image
    from .catalog_recovery import catalog_entries,recover_catalog
    from .ai_page import cached_page_path
    first=record.get('local_first') or {};summary=first.get('ai_page') or {}
    if summary.get('mode')!='prefer' or summary.get('used') or not summary.get('model'):return None
    if page.get('corrected_text') is not None or any(page.get(k) for k in ('edits','decisions','ai_decisions')):return None
    witnesses=[r['raw'] for r in page['runs'] if len(catalog_entries(r['raw']['text'])[1])>=15]
    if not witnesses:return None
    work=reference.parent.parent;number=record['pdf_page']
    direction='horizontal' if record.get('mode')=='horizontal_zh' else 'vertical'
    if direction!='horizontal':return None
    try:
        with Image.open(work/'pages'/f'p{number:03d}.png') as image:
            cache=cached_page_path(image,direction=direction,profile=first.get('profile','traditional'),model=summary['model'],cache_dir=work/'ai-page')
        answer=json.loads(cache.read_text(encoding='utf-8'))
    except (OSError,ValueError):return None
    if not isinstance(answer,dict):return None
    recovery=recover_catalog(answer,[w['text'] for w in witnesses])
    if recovery is None:return None
    text,pending,evidence=recovery
    raw=max(witnesses,key=lambda w:len(catalog_entries(w['text'])[1]))
    reading=reading_from_run(raw,number,direction,text=text)
    current=copy.deepcopy(record);current['merged']['regions']=reading.model_dump()['regions']
    current['_output_catalog_recovery']={**evidence,'cached_file':cache.name,'new_calls':0}
    grades=[];offset=0
    for index,line in enumerate(text.split('\n'),1):
        for a,b in pending:
            if a<offset+len(line) and offset<b:
                grades.append(dict(region_id=f'local-line-{index}',para=0,start=max(a-offset,0),end=min(b-offset,len(line)),grade='red'))
        offset+=len(line)+1
    current['local_grades']=grades
    return current

from .schema import Column, PageReading, Paragraph, Region, Uncertain


def _box(value, width, height):
    if (not isinstance(value, (list, tuple)) or len(value) != 4
            or any(type(v) not in (int, float) or not math.isfinite(v) for v in value)):
        return None
    left, top, right, bottom = value
    left, top = max(0, left), max(0, top)
    right, bottom = min(width, right), min(height, bottom)
    return [left, top, right, bottom] if left < right and top < bottom else None


def _union(boxes):
    return [min(b[0] for b in boxes), min(b[1] for b in boxes),
            max(b[2] for b in boxes), max(b[3] for b in boxes)]


def _normalized(box, width, height):
    left, top, right, bottom = box
    x0, x1 = round(left * 1000 / width), round(right * 1000 / width)
    y0, y1 = round(top * 1000 / height), round(bottom * 1000 / height)
    x0, y0 = min(999, max(0, x0)), min(999, max(0, y0))
    x1, y1 = min(1000, max(x0 + 1, x1)), min(1000, max(y0 + 1, y1))
    return [y0, x0, y1, x1]


def _lines(text):
    """Include empty lines and preserve every code point, including final LF."""
    result, offset = [], 0
    for value in text.split("\n"):
        result.append((offset, offset + len(value), value))
        offset += len(value) + 1
    return result


def _source_boxes(raw, lines, width, height):
    text = raw["text"]
    # Matching without whitespace is only a positioning fallback. The source
    # text, spaces, line breaks, variants, and punctuation remain untouched.
    positions = [i for i, c in enumerate(text) if not c.isspace()]
    compact = "".join(text[i] for i in positions)
    cursor, located = 0, []
    for word in raw.get("words", []):
        value = word.get("text", "")
        box = _box(word.get("bbox"), width, height)
        if not isinstance(value, str) or not value or box is None:
            continue
        start = text.find(value, cursor)
        if start >= 0:
            end = start + len(value)
        else:
            needle = "".join(c for c in value if not c.isspace())
            found = compact.find(needle, bisect.bisect_left(positions, cursor)) if needle else -1
            if found < 0:
                continue
            start, end = positions[found], positions[found + len(needle) - 1] + 1
        located.append((start, end, box))
        cursor = end
    result, complete = [], []
    for start, end, _value in lines:
        boxes = [box for left, right, box in located if left < end and start < right]
        result.append(_union(boxes) if boxes else None)
        covered = {i for left, right, _box_value in located
                   for i in range(max(start, left), min(end, right))}
        complete.append(all(i in covered or text[i].isspace() for i in range(start, end)))
    return result, complete


def reading_from_run(raw, number, direction, text=None) -> PageReading:
    """Preserve current text while assigning source line boxes in source order.

    ``raw`` is a verified local RecognitionResult dictionary, whose boxes use
    rendered-image pixels. ``text`` may contain accepted or human corrections.
    Ambiguous edits retain the text and are explicitly flagged for inspection.
    """
    if type(number) is not int or number < 1:
        raise ValueError("쪽 번호는 양수여야 합니다.")
    if direction not in ("horizontal", "horizontal_zh", "vertical"):
        raise ValueError("읽기 방향을 확인하세요.")
    source = raw.get("text")
    current = source if text is None else text
    if not isinstance(source, str) or not isinstance(current, str):
        raise ValueError("로컬 판독문은 문자열이어야 합니다.")
    width, height = raw.get("image_width"), raw.get("image_height")
    if any(type(v) not in (int, float) or not math.isfinite(v) or v <= 0 for v in (width, height)):
        raise ValueError("로컬 판독 이미지 크기가 올바르지 않습니다.")
    reading = PageReading(pdf_page=number, page_note=(
        "로컬 문자 판독의 줄과 좌표를 보존했습니다. 제목·도판·표의 구조는 자동 추정하지 않았습니다."))
    if current == "":
        reading.page_note += " 인식된 문자가 없습니다."
        return reading
    source_lines = _lines(source)
    source_boxes, source_complete = _source_boxes(raw, source_lines, width, height)
    opcodes = difflib.SequenceMatcher(None, source, current,
                                      autojunk=max(len(source), len(current)) > 20000).get_opcodes()
    full = [0, 0, width, height]
    for index, (start, end, value) in enumerate(_lines(current)):
        source_indices, inserted = set(), False
        if source == current:
            source_indices.add(index)
        else:
            for kind, a0, a1, b0, b1 in opcodes:
                if b0 >= end or start >= b1:
                    continue
                if kind == "equal":
                    left, right = a0 + max(start, b0) - b0, a0 + min(end, b1) - b0
                elif kind == "insert":
                    left = right = a0
                    inserted = True
                else:
                    left, right = a0, a1
                for source_index, (line_start, line_end, _line) in enumerate(source_lines):
                    if ((left < line_end and line_start < right)
                            or (left == right and line_start <= left <= line_end)):
                        source_indices.add(source_index)
        boxes = [source_boxes[i] for i in sorted(source_indices) if source_boxes[i] is not None]
        reasons = []
        if not boxes:
            # A nearest source line provides a transparent inspection anchor;
            # it is not asserted to be an exact character/line measurement.
            anchor = min(source_indices) if source_indices else min(index, len(source_lines) - 1)
            known = [(abs(i - anchor), i, box) for i, box in enumerate(source_boxes) if box is not None]
            boxes = [min(known)[2]] if known else [full]
            if value.strip():
                reasons.append("원문 줄의 좌표가 없어 가까운 원문 영역 또는 쪽 전체를 확인 위치로 사용했습니다")
        if inserted and value.strip():
            reasons.append("추가한 글자의 정확한 위치는 원본 대조가 필요합니다")
        if boxes and any(not source_complete[i] for i in source_indices) and value.strip():
            reasons.append("원문 글자 일부의 좌표 근거가 없어 줄 영역을 원본과 대조해야 합니다")
        if len(source_indices) > 1 and source != current and value.strip():
            reasons.append("교정으로 원문 줄 경계가 달라져 합친 영역을 확인 위치로 사용했습니다")
        if re.search("[\ufffd\u25a1]|〔(?:판독불명|원본 결락)〕", value):
            reasons.append("판독 불명 또는 누락 표시가 남아 있습니다")
        region_id = f"local-line-{index + 1}"
        reading.regions.append(Region(id=region_id, kind="본문", bbox=_normalized(_union(boxes), width, height),
            reading_direction="세로(우→좌)" if direction == "vertical" else "가로",
            paragraphs=[Paragraph(columns=[Column(col_from_right=1, text=value)])], note=" · ".join(reasons)))
        if reasons:
            reading.uncertain.append(Uncertain(region_id=region_id, col_from_right=1,
                context=" · ".join(reasons)))
    return reading


def render_for_record(work, doc, n, dpi, rec):
    """Read an image and PDF frame consistently without changing cached files.

    The caller owns and closes the image. Full-page local boxes must never be
    interpreted against an inset native scan's placement rectangle.
    """
    from .pdfio import page_frame, page_image
    full_page = rec.get("image_frame") == "full-page" or rec.get("pipeline") == "local-first"
    image, _description = page_image(doc, n, dpi=dpi, prefer_embedded=not full_page)
    return image, page_frame(doc, n, prefer_embedded=not full_page)
