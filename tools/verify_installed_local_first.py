"""Verify real bundled OCR through the installed CLI without API credentials.

Only a newly generated public synthetic image is used. The frozen application
loads its own bundled modules/model and produces a searchable PDF and records.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
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
    pdfs = list((work / "out").glob("*.pdf"))
    if not pdfs:
        raise ValueError("Expected a searchable PDF from installed local-first CLI")
    pdfs.sort(key=lambda p:p.stat().st_mtime_ns)
    for path in pdfs:
        with pymupdf.open(path) as document:
            text = "".join(page.get_text() for page in document)
        if "".join(text.split()) != recognized:
            raise ValueError("Searchable PDF text differs from the stored local recognition")
    return {"engine": "ppocrv6", "pipeline": POLICY, "api_calls": 0,
            "reference_characters": len(recognized), "character_errors": 0,
            "pdf_sha256": hashlib.sha256(pdfs[-1].read_bytes()).hexdigest(), "pdf_versions":len(pdfs), "pdf_text_matches": True}


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
               OMP_NUM_THREADS="1", OPENBLAS_NUM_THREADS="1",
               YEONE_OCR_CONFUSIONS_PATH=str(output / "confusions.json"),
               YEONE_OCR_GLYPH_SAMPLES_PATH=str(output / "glyph-samples"))
    env["PATH"] = str(Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32")
    argv = [str(executable), "--cli", "run", str(image_path), "--work", str(work),
            "--ocr-strategy", "local-first", "--local-profile", "simplified", "--mode", "horizontal_zh",
            "--local-engine-root", str(app), "--no-local-ai-review", "--local-ai-max-calls", "1",
            "--local-ai-auth", "api", "--local-ai-page", "off",
            "--pdf-only", "--checkpoint", "0"]
    runs = []
    first_history = None
    prior_outputs = {}
    for name in ("initial", "resume"):
        result = subprocess.run(argv, cwd=output, env=env, capture_output=True, timeout=300)
        (output / (name + ".log")).write_bytes(result.stdout + result.stderr)
        if result.returncode:
            raise RuntimeError(f"Installed local-first {name} failed: exit {result.returncode}")
        runs.append(inspect_outputs(work, app))
        if any(not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest()!=digest for path,digest in prior_outputs.items()):
            raise ValueError('Saving another PDF changed an earlier output version')
        prior_outputs={p:hashlib.sha256(p.read_bytes()).hexdigest() for p in (work/'out').glob('*.pdf')}
        record = json.loads((work / "readings/p001.json").read_text(encoding="utf-8"))
        page = json.loads(Path(record["local_first"]["project_path"]).read_text(encoding="utf-8"))["pages"]["1"]
        history = {key: page.get(key, []) for key in ("runs", "edits", "ai_reviews", "ai_decisions")}
        if first_history is None:
            first_history = history
        elif history != first_history:
            raise ValueError("Installed resume duplicated or changed existing reading/correction history")
    if hashlib.sha256(image_path.read_bytes()).hexdigest() != source_hash:
        raise ValueError("Installed inference changed the synthetic input")
    if len(prior_outputs)!=2 or not all(re.fullmatch(re.escape(image_path.stem)+r' - \d{6}_\d{6}\.pdf',p.name) for p in prior_outputs):
        raise ValueError('Installed repeated save did not preserve two YYMMDD_HHMMSS PDF outputs')
    styled = verify_styled_pdf(executable, image_path, output, env, app)
    report = {"ok": True, "installed_app": str(app), "runs": runs, "source_sha256": source_hash, "styled_hwpx": styled,
              "network": "API auth selected, AI page/review disabled; local worker blocks networking",
              "source_preserved": True, "resume_history_preserved": True, "duplicate_readings": 0,
              "dated_output_names_verified":True,"previous_output_versions_preserved":True}
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


def verify_styled_pdf(executable, image_path, output, env, app):
    """Exercise the first-use task with the installed OCR engine on a scanned PDF."""
    from xml.etree import ElementTree as ET
    from zipfile import ZipFile
    import pymupdf
    from hwpx import HwpxDocument

    pdf = output / "scanned.pdf"
    with pymupdf.open() as doc:
        page = doc.new_page(width=264, height=62.4)
        page.insert_image(page.rect, filename=str(image_path))
        doc.save(pdf)
    template = output / "styled.hwpx"
    doc = HwpxDocument.new()
    header = doc.oxml.headers[0]
    char_id = doc.oxml.ensure_run_style(font="Source Han Serif K Medium", size=15, color="#224488")
    header.ensure_style("시험 본문", style_type="PARA", para_pr_id_ref=0, char_pr_id_ref=char_id)
    doc.save_to_path(str(template))
    doc.close()
    rule = output / "style.json"
    rule.write_text(json.dumps({"schema": 1, "styles": {"paragraph": {"body": ["시험 본문"]}}}), encoding="utf-8")
    work = output / "styled-output"
    argv = [str(executable), "--cli", "run", str(pdf), "--work", str(work), "--ocr-strategy", "local-first",
            "--local-profile", "simplified", "--mode", "horizontal_zh", "--local-engine-root", str(app),
            "--no-local-ai-review", "--local-ai-auth", "api", "--local-ai-page", "off",
            "--layout", "flow", "--template", str(template), "--preset", str(rule), "--no-normalize", "--no-context-model",
            "--textlayer-pdf", "--no-highlight", "--checkpoint", "0", "--dpi", "300"]
    result = subprocess.run(argv, cwd=output, env=env, capture_output=True, timeout=300)
    (output / "styled.log").write_bytes(result.stdout + result.stderr)
    if result.returncode:
        raise RuntimeError(f"Installed PDF to styled HWPX failed with exit {result.returncode}")
    state = json.loads((work / "state.json").read_text(encoding="utf-8"))
    build = state["last_build"]
    assert Path(build["template"]).samefile(template)
    document, = build["outputs"]
    with ZipFile(work / "out" / document) as archive:
        header = ET.fromstring(archive.read("Contents/header.xml"))
        section = ET.fromstring(archive.read("Contents/section0.xml"))
    style_id, = [s.get("id") for s in header.iterfind(".//{*}style") if s.get("name") == "시험 본문"]
    body = "".join("".join(node.itertext()) for para in section.iterfind(".//{*}p")
                   if para.get("styleIDRef") == style_id for node in para.iterfind("./{*}run/{*}t"))
    # Printed glyphs may also be represented with Korean or traditional Han forms.
    if not all(token in body for token in ("天地", "宇宙", "洪荒")):
        raise ValueError("Recognized text is missing from paragraphs using the selected template style")
    paired = (work / "out" / document).with_suffix(".pdf")
    with pymupdf.open(paired) as exported:
        text = "".join(page.get_text() for page in exported)
        if len(exported) != 1 or "".join(text.split()) != "".join(REFERENCE.split()):
            raise ValueError("The matching PDF differs from the reference scan text")
        font, = exported[0].get_fonts()
        if font[5] != "Identity-H" or not exported.extract_font(font[0])[3].startswith(b"\x01\x00"):
            raise ValueError("The matching PDF did not embed a standard CFF OCR font")
    return {"template_used": True, "body_style": "시험 본문", "document": document, "paired_pdf": paired.name}


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
