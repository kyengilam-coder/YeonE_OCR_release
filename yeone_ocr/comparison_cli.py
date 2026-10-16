"""Headless local three-engine jobs; no cloud reader or shared CLI globals."""
from pathlib import Path
import re
import threading


def parse_pages(value, count):
    if not value or not value.strip():
        return list(range(1, count + 1))
    pages = set()
    for part in value.split(","):
        match = re.fullmatch(r"\s*(\d+)\s*(?:-\s*(\d+)\s*)?", part)
        if match is None:
            raise ValueError("쪽 범위는 1-5,8 형식으로 입력하세요.")
        first = int(match.group(1)); last = int(match.group(2) or first)
        if not 1 <= first <= last <= count:
            raise ValueError(f"쪽 범위는 1~{count} 안에서 지정하세요.")
        pages.update(range(first, last + 1))
    return sorted(pages)


def command(args):
    from ab_ocr.ocr import document_info, OCRError
    from .comparison import ComparisonProject, run_page
    from .comparison_bridge import default_project_path, open_project
    stop = threading.Event()
    project = None
    def recover():
        if project is not None and project.project_path:
            try:
                project.save_recovery(project.project_path.with_name(project.project_path.stem + ".recovery.json"))
            except (OSError, ValueError):
                print("복구 사본을 저장하지 못했습니다. 마지막 정상 프로젝트와 엔진별 완료 기록을 확인하세요.", flush=True)
    try:
        numbers = parse_pages(args.pages, document_info(args.pdf)["page_count"])
        if not 72 <= args.dpi <= 600:
            raise ValueError("렌더링 DPI는 72~600이어야 합니다.")
        if args.project and Path(args.project).exists():
            project = ComparisonProject.load(args.project, source_path=args.pdf)
        else:
            project = open_project(args.pdf, args.work)
        path = Path(args.project) if args.project else (project.project_path or default_project_path(args.pdf, args.work))
        path.parent.mkdir(parents=True, exist_ok=True)
        project.save(path)
        failures = 0
        for number in numbers:
            result = run_page(project, number, profile=args.profile, direction=args.direction,
                dpi=args.dpi, should_stop=stop.is_set, on_result=lambda _result: project.save(path),
                engine_root=args.engine_root, force=args.force)
            project.save(path)
            failures += bool(result["errors"])
            print(f"{number}쪽: 완료 {len(result['completed'])}개 엔진 · 재사용 {len(result.get('skipped', []))}개 · 실패 {len(result['errors'])}개", flush=True)
            if result["cancelled"]:
                return 130
        return 3 if failures else 0
    except KeyboardInterrupt:
        stop.set()
        recover()
        return 130
    except (OSError, ValueError, OCRError) as exc:
        recover()
        print(f"로컬 비교 작업을 완료하지 못했습니다: {exc}", flush=True)
        return 2
