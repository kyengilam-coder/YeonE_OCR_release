"""Verify real bundled OCR through the installed CLI without API credentials.

Only a newly generated public synthetic image is used. The frozen application
loads its own bundled modules/model and produces a searchable PDF and records.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess

REFERENCE = "天地玄黄，宇宙洪荒。"
POLICY = "local-first-selective-review-v1"


def inspect_outputs(work, app, reference=REFERENCE):
    """Reject missing recognition, unexpected API work, or bad PDF text."""
    import pymupdf
    work, app = Path(work), Path(app).resolve()
    record = json.loads((work / "readings/p001.json").read_text(encoding="utf-8"))
    if record.get("pipeline") != POLICY or record.get("model") != "ppocrv6":
        raise ValueError("Installed local-first PP-OCRv6 reading is missing")
    if record.get("image_frame") != "full-page":
        raise ValueError("Local OCR lost its full-page coordinate frame")
    state = json.loads((work / "state.json").read_text(encoding="utf-8"))
    if state.get("local_first_last_run", {}).get("calls") != 0 or record.get("usage", {}).get("calls") != 0:
        raise ValueError("Offline installed smoke unexpectedly used an API")
    project = json.loads(Path(record["local_first"]["project_path"]).read_text(encoding="utf-8"))
    page = project["pages"]["1"]
    baseline = next(r for r in page["runs"] if r["id"] == page["baseline_run_id"])
    if baseline["engine"] != "ppocrv6" or Path(baseline["request"]["engine_root"]).resolve() != app:
        raise ValueError("Installed app did not use its own PP-OCRv6 engine")
    if page.get("ai_reviews"):
        raise ValueError("Offline installed smoke unexpectedly recorded an API review")
    recognized = "".join(baseline["raw"]["text"].split())
    if recognized != "".join(reference.split()):
        raise ValueError("Installed synthetic recognition differs from the exact reference: " + recognized)
    pdfs = list((work / "out").glob("*_글자층.pdf"))
    if len(pdfs) != 1:
        raise ValueError("Expected one searchable PDF from installed local-first CLI")
    with pymupdf.open(pdfs[0]) as document:
        text = "".join(page.get_text() for page in document)
    if "".join(text.split()) != recognized:
        raise ValueError("Searchable PDF text differs from the stored local recognition")
    return {"engine": "ppocrv6", "pipeline": POLICY, "api_calls": 0,
            "reference_characters": len(recognized), "character_errors": 0,
            "pdf_sha256": hashlib.sha256(pdfs[0].read_bytes()).hexdigest(), "pdf_text_matches": True}


def verify(app, output):
    from PIL import Image, ImageDraw, ImageFont
    app, output = Path(app).resolve(), Path(output).resolve()
    executable = app / "YeonE_OCR.exe"
    if not executable.is_file():
        raise ValueError("Installed YeonE_OCR.exe is missing")
    output.mkdir(parents=True, exist_ok=False)
    font = Path(os.environ.get("SystemRoot", "C:/Windows")) / "Fonts/simsun.ttc"
    image_path = output / "public-synthetic.png"
    with Image.new("RGB", (1100, 260), "white") as image:
        ImageDraw.Draw(image).text((70, 85), REFERENCE, font=ImageFont.truetype(str(font), 56), fill="black")
        image.save(image_path)
    source_hash = hashlib.sha256(image_path.read_bytes()).hexdigest()
    work = output / "work"
    excluded = {"PYTHONPATH", "PYTHONHOME", "YEONE_OCR_ENGINE_ROOT", "AB_OCR_ENGINE_ROOT",
                "GEMINI_API_KEY", "GOOGLE_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"}
    env = {k: v for k, v in os.environ.items() if k.upper() not in excluded}
    env.update(HF_HUB_OFFLINE="1", HF_HUB_DISABLE_TELEMETRY="1", DO_NOT_TRACK="1",
               OMP_NUM_THREADS="1", OPENBLAS_NUM_THREADS="1")
    env["PATH"] = str(Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32")
    argv = [str(executable), "--cli", "run", str(image_path), "--work", str(work),
            "--ocr-strategy", "local-first", "--local-profile", "simplified", "--mode", "horizontal_zh",
            "--local-engine-root", str(app), "--no-local-ai-review", "--local-ai-max-calls", "1",
            "--pdf-only", "--checkpoint", "0"]
    runs = []
    for name in ("initial", "resume"):
        result = subprocess.run(argv, cwd=output, env=env, capture_output=True, timeout=300)
        (output / (name + ".log")).write_bytes(result.stdout + result.stderr)
        if result.returncode:
            raise RuntimeError(f"Installed local-first {name} failed: exit {result.returncode}")
        runs.append(inspect_outputs(work, app))
    if hashlib.sha256(image_path.read_bytes()).hexdigest() != source_hash:
        raise ValueError("Installed inference changed the synthetic input")
    report = {"ok": True, "installed_app": str(app), "runs": runs, "source_sha256": source_hash,
              "network": "No API credentials; local worker blocks networking", "source_preserved": True}
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    report = verify(args.app, args.output)
    print(json.dumps(report, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
