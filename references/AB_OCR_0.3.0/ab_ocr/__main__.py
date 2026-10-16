"""Desktop and reproducible command-line entry points for AB_OCR."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

from . import __version__


def _policy(args):
    return {"unicode_form": args.unicode_form, "whitespace": args.whitespace}


def _add_policy(parser):
    parser.add_argument("--unicode-form", choices=["none", "NFC"], default="none",
                        help="선택적 한글 자모 결합; 호환 한자는 보존 (기본 none)")
    parser.add_argument("--whitespace", choices=["preserve", "collapse", "remove"], default="preserve",
                        help="정규화 점수의 공백 규칙; 원문 점수는 항상 별도 계산")


def _print_metrics(report):
    for mode, label in (("raw", "원문"), ("normalized", "정규화")):
        metric = report[mode]
        cer = "해당 없음 (빈 정답)" if metric["cer"] is None else f"{metric['cer']:.2%}"
        print(f"{label} CER: {cer} · 치환 {metric['substitutions']} / 누락 {metric['deletions']} / 추가 {metric['insertions']}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="AB_OCR — 한글·한자 OCR와 정확도 평가")
    parser.add_argument("--version", action="version", version=f"AB_OCR {__version__}")
    commands = parser.add_subparsers(dest="command")
    for name in ("gui", "editor"):
        editor = commands.add_parser(name, help="원본 대조 OCR 교정 편집기 열기 (기본)")
        start = editor.add_mutually_exclusive_group()
        start.add_argument("--source", type=Path, help="처음 열 PDF 또는 스캔 이미지")
        start.add_argument("--project", type=Path, help="이어 작업할 .abocr.json 프로젝트")
    commands.add_parser("evaluate-gui", help="정답·인식문 정확도 평가 화면 열기")
    check = commands.add_parser("doctor", help="로컬 OCR 엔진과 설치 언어 점검")
    check.add_argument("--engine", choices=["tesseract", "rapidocr", "easyocr"], default="tesseract")
    comparison = commands.add_parser("compare", help="정답 TXT와 인식 TXT 비교")
    comparison.add_argument("reference", type=Path)
    comparison.add_argument("hypothesis", type=Path)
    comparison.add_argument("--output", type=Path, help="보고서 (.html/.json/.csv)")
    _add_policy(comparison)
    benchmark = commands.add_parser("benchmark", help="같은 문서 묶음에서 엔진별 인식 결과 비교")
    benchmark.add_argument("manifest", type=Path)
    benchmark.add_argument("--split", choices=["dev", "test"])
    benchmark.add_argument("--output", type=Path, help="보고서 (.html/.json)")
    _add_policy(benchmark)
    recognition = commands.add_parser("recognize", help="선택한 로컬 OCR 엔진으로 이미지/PDF의 한 쪽 인식")
    recognition.add_argument("--engine", choices=["tesseract", "rapidocr", "easyocr"], default="tesseract")
    recognition.add_argument("source", type=Path)
    recognition.add_argument("--output", type=Path, required=True, help="원시 인식문을 저장할 새 TXT 경로")
    recognition.add_argument("--languages", default="kor+chi_tra+eng", help="선택한 엔진에서 지원하는 언어 조합; 새 엔진은 명시 지정")
    recognition.add_argument("--page", type=int, default=1)
    recognition.add_argument("--psm", type=int, default=None, choices=[3, 4, 5, 6, 7, 11, 12, 13], help="Tesseract 전용 영역 분할 (기본 3)")
    recognition.add_argument("--dpi", type=int, default=300)
    recognition.add_argument("--tesseract", type=Path)
    recognition.add_argument("--tessdata", type=Path)
    args = parser.parse_args(argv)
    if args.command in (None, "gui", "editor"):
        from .editor_gui import main as editor_main
        options = []
        for name in ("source", "project"):
            value = getattr(args, name, None)
            if value is not None:
                options.extend(["--" + name, str(value)])
        return editor_main(options) or 0
    if args.command == "evaluate-gui":
        from .gui import main as gui_main
        return gui_main([]) or 0
    try:
        from .reports import read_utf8_snapshot, write_report, write_recognition
        if args.command == "doctor":
            from .ocr import doctor
            info = doctor(engine=args.engine)
            print(json.dumps(info, ensure_ascii=False, indent=2))
            return 0 if info["available"] else 2
        if args.command == "compare":
            from .evaluation import compare_texts
            reference_text, reference_hash = read_utf8_snapshot(args.reference)
            hypothesis_text, hypothesis_hash = read_utf8_snapshot(args.hypothesis)
            report = compare_texts(reference_text, hypothesis_text, policy=_policy(args))
            report["kind"] = "comparison"
            report["inputs"] = {name: {"path": str(path.resolve()), "sha256": digest}
                                for name, path, digest in (("reference", args.reference, reference_hash),
                                                           ("hypothesis", args.hypothesis, hypothesis_hash))}
            report["protected_input_paths"] = [str(args.reference.resolve()), str(args.hypothesis.resolve())]
            report["input_decoding"] = "UTF-8, optional leading BOM removed, line endings preserved"
            _print_metrics(report)
            if args.output:
                print("보고서:", write_report(report, args.output))
            return 0
        if args.command == "benchmark":
            from .benchmark import evaluate_manifest
            report = evaluate_manifest(args.manifest, policy=_policy(args), split=args.split)
            for engine_id, engine in report["engines"].items():
                print(f"{engine.get('name', engine_id)}: {engine['completed_pages']} / {engine['total_pages']}쪽")
                _print_metrics(engine)
            if not report.get("ranking", {}).get("available"):
                print("순위 없음:", report.get("ranking", {}).get("reason", ""))
            if args.output:
                print("보고서:", write_report(report, args.output))
            return 0 if report["completeness"]["complete"] else 2
        if args.command == "recognize":
            from .ocr import recognize_file
            output = args.output.resolve()
            source = args.source.resolve()
            record = output.with_suffix(".ocr.json")
            if output.suffix.lower() != ".txt":
                raise ValueError("인식문 저장 경로는 .txt여야 합니다.")
            if output == source or record == source:
                raise ValueError("원본과 다른 출력 파일을 고르십시오.")
            if not output.parent.is_dir():
                raise ValueError("출력 폴더를 먼저 만들어 주십시오.")
            if output.exists() or record.exists():
                raise ValueError("인식문 또는 인식 기록이 이미 있습니다. 새 출력 이름을 고르십시오.")
            if args.engine != "tesseract" and args.psm is not None:
                raise ValueError("--psm은 Tesseract 전용입니다. 다른 엔진은 자체 영역 검출을 사용합니다.")
            result = recognize_file(source, languages=args.languages, page_number=args.page,
                                    psm=(args.psm if args.psm is not None else 3) if args.engine == "tesseract" else None,
                                    dpi=args.dpi, executable=args.tesseract, tessdata_dir=args.tessdata, engine=args.engine)
            payload = result.to_dict()
            payload["created_at"] = datetime.now(timezone.utc).isoformat()
            text_path, record_path = write_recognition(payload, output)
            print("원시 인식문:", text_path)
            print("인식 기록:", record_path)
            return 0
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"AB_OCR: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
