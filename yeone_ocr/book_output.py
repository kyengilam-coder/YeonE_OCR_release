"""Book output selection and representative HWPX preview."""
from pathlib import Path
import json
import os
import shutil
import tempfile


LAYOUT_LABELS = {"source": "원본 배치 HWPX (쪽·열·구분선)",
                 "flow": "편집용 HWPX (제목·문단)", "page": "원본 대조용 HWPX (쪽별)"}


def select_template(template, layout, path):
    """Selecting a non-empty template selects the mode which uses it."""
    if path and layout is not None and layout.get() != LAYOUT_LABELS["flow"]:
        layout.set(LAYOUT_LABELS["flow"])
    if template.get() != path:
        template.set(path)


def _source_records(work, doc, preferred, limit=3, *, adaptive=False):
    """Prefer analyzed types, then pages with a different mix of OCR regions."""
    if work.readings_dir.is_dir() and work.source_matches():
        chosen, fallbacks, known = [], [], set()
        for n in dict.fromkeys([*preferred, *range(1, len(doc) + 1)]):
            if not 1 <= n <= len(doc):
                continue
            record = work.load_record(n, repair=False)
            if not isinstance(record, dict) or record.get("pdf_page") != n:
                continue
            regions = (record.get("merged") or {}).get("regions", [])
            kinds = {region.get("kind") or region.get("k") for region in regions}
            if not kinds:
                continue
            if n in preferred or kinds - known:
                chosen.append(record)
                known.update(kinds)
                if len(chosen) == limit:
                    break
            elif len(fallbacks) < limit:
                fallbacks.append(record)
        selected = chosen + fallbacks[:limit - len(chosen)]
        if selected:
            return sorted(selected, key=lambda rec: rec["pdf_page"]), "저장된 OCR"

    # A PDF with its own text can demonstrate the body style even before OCR.
    # Do not fabricate roles from guessed font sizes or substitute unrelated text.
    import pymupdf
    from .schema import PageReading, Region, Paragraph, Column
    flags = pymupdf.TEXTFLAGS_DICT & ~pymupdf.TEXT_PRESERVE_IMAGES
    selected = []
    for n in dict.fromkeys([*preferred, *range(1, len(doc) + 1)]):
        if not 1 <= n <= len(doc):
            continue
        page = doc[n - 1]
        width, height = page.cropbox.width, page.cropbox.height
        regions = []
        for block in page.get_text('dict', flags=flags, sort=True)['blocks']:
            for line in block.get('lines', []):
                text = ''.join(span['text'] for span in line['spans']).strip()
                if not text:
                    continue
                x0, y0, x1, y1 = line['bbox']
                bbox = [max(0, min(1000, round(1000 * value / extent)))
                        for value, extent in zip((y0, x0, y1, x1), (height, width, height, width))]
                prefix = 'local-line-pdf' if adaptive else 'pdf-text'
                regions.append(Region(id=f'{prefix}-{len(regions)}', kind='본문', bbox=bbox,
                                      reading_direction='세로' if line.get('wmode') else '가로',
                                      paragraphs=[Paragraph(columns=[Column(text=text)])]))
        if regions:
            reading = PageReading(pdf_page=n, regions=regions)
            selected.append({"file": work.pdf_path.name, "pdf_page": n, "merged": reading.model_dump()})
            if len(selected) == limit:
                break
    if not selected:
        raise ValueError("미리보기에 사용할 본문이 없습니다. PDF에서 대표 쪽을 먼저 판독한 뒤 다시 여십시오.")
    return sorted(selected, key=lambda rec: rec["pdf_page"]), "PDF 글자층 (원본 줄 위치 포함)"


def preview_document(pdf, work_dir, output_options, preferred=(), *, analysis=None):
    """Build selected pages with the normal writer in a disposable workspace."""
    from .cli import build_outputs, build_parser, preset_ref
    from .pdfio import open_pdf
    from .presets import resolve_preset
    from .state import Work

    source = Work(pdf, work_dir, initialize=False)
    with open_pdf(pdf) as doc, tempfile.TemporaryDirectory(prefix="yeone-preview-") as temporary:
        args = build_parser().parse_args(["build", str(pdf), *output_options, "--no-context-model"])
        if analysis:
            from .rule_generation import verify_inputs
            verify_inputs(analysis)
            rule_path = Path(temporary) / 'preview-rule.json'
            rule_path.write_text(json.dumps(analysis['rule'], ensure_ascii=False), encoding='utf-8')
            args.layout = 'flow'
            args.preset = str(rule_path)
            args.template = analysis['rule']['template'] or None
        if not preferred and args.layout == "flow":
            profile = resolve_preset(preset_ref(args)).page_types
            if profile:
                from ab_ocr.ocr import stable_file_sha256
                if stable_file_sha256(pdf) == profile['source_sha256']:
                    pages = profile['pages']
                    preferred = tuple(pages[kind][0] for kind in
                                      ('차례', '목차', '색인', '본문', '문헌 목록', '각주') if pages.get(kind))
        adaptive = args.layout == 'flow' and bool(resolve_preset(preset_ref(args)).source_sha256)
        records, origin = _source_records(source, doc, preferred, limit=max(3, min(5, len(set(preferred)))),
                                          adaptive=adaptive)
        args.docx = False
        args.out = "미리보기"
        staging = Work(pdf, Path(temporary) / "작업자료")
        if origin == "저장된 OCR":
            for name in ("document_layout.json", "output_repairs.json"):
                if (source.dir / name).is_file():
                    shutil.copyfile(source.dir / name, staging.dir / name)
        if not build_outputs(staging, doc, records, args):
            raise ValueError("미리보기 HWPX를 만들지 못했습니다. 양식과 책별 규칙을 확인하십시오.")
        fd, destination = tempfile.mkstemp(prefix="연어OCR-미리보기-", suffix=".hwpx")
        os.close(fd)
        try:
            shutil.copyfile(staging.out_dir / "미리보기.hwpx", destination)
        except BaseException:
            Path(destination).unlink(missing_ok=True)
            raise
    return Path(destination), origin, [record["pdf_page"] for record in records]
