"""Benchmark the synthetic Chinese PDF using the app's local comparison path.

Run from the source root: python -m scripts.benchmark_chinese_ocr --engine-root PATH
No API is called, no engine is installed and no model is downloaded here.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from ab_ocr.evaluation import NormalizationPolicy, compare_texts
from ab_ocr.ocr import doctor
from yeone_ocr.comparison import ComparisonProject, ENGINES, latest_runs, run_page


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, default=Path("output/pdf/chinese_ocr_fixture"))
    parser.add_argument("--engine-root", type=Path, required=True)
    parser.add_argument("--engines", nargs="+", choices=ENGINES, default=list(ENGINES))
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)
    folder = args.fixture.resolve()
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    source = folder / manifest["pdf"]
    if hashlib.sha256(source.read_bytes()).hexdigest() != manifest["pdf_sha256"]:
        raise ValueError("Fixture PDF changed; regenerate the fixture and references together")
    project_path = folder / "comparison.yeone-compare.json"
    project = ComparisonProject.load(project_path, source_path=source) if project_path.exists() else ComparisonProject.open(source)
    project.save(project_path)
    report = {"fixture_sha256": manifest["pdf_sha256"], "synthetic_only": True,
              "limitations": "Clean synthetic font samples; not a historical-book accuracy benchmark",
              "engines": {e: doctor(engine=e, engine_root=args.engine_root) for e in args.engines}, "pages": []}
    failures = 0
    for page in manifest["pages"]:
        reference = (folder / page["reference"]).read_text(encoding="utf-8")
        if hashlib.sha256(reference.encode("utf-8")).hexdigest() != page["reference_sha256"]:
            raise ValueError("Fixture reference changed")
        outcome = run_page(project, page["page"], profile=page["profile"], direction=page["direction"], dpi=200,
                           engine_root=args.engine_root, engines=args.engines, force=args.force,
                           on_result=lambda _result: project.save())
        project.save()
        scores = {}
        runs = latest_runs(project.page(page["page"]))
        for engine in outcome["completed"] + outcome["skipped"]:
            raw = runs[engine]["raw"]
            metric = compare_texts(reference, raw["text"], NormalizationPolicy(whitespace="remove"))
            scores[engine] = {"strict": metric["raw"], "whitespace_removed": metric["normalized"],
                              "seconds": raw["duration_seconds"], "engine_metadata": raw["engine_metadata"]}
            print(f"page {page['page']} {engine}: CER={metric['normalized']['cer_percent']:.2f}% (whitespace removed)", flush=True)
        failures += len(outcome["errors"])
        report["pages"].append({**page, "outcome": outcome, "scores": scores})
        (folder / "benchmark.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(folder / "benchmark.json")
    return 3 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
