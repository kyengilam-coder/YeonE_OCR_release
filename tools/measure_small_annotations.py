"""Separate image-only acquisition from provisional small-annotation evaluation.

``baseline`` reads an input-only manifest; ``reread`` uses frozen baseline work.
Only ``evaluate`` can read gold. No reference text or gold ROI reaches a reader.
The baseline has no character coordinates: report exact containment, never call
an oracle substring score its ROI CER. Proposal CER uses spatial matches only.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import sys

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ab_ocr.evaluation import classify_character, compare_texts


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def write_json(path, value):
    from yeone_ocr.state import write_atomic
    write_atomic(Path(path), json.dumps(value, ensure_ascii=False, indent=2))


def han_text(text):
    """Explicit projection, preserving compatibility ideographs and both VS blocks."""
    return "".join(c for c in text if classify_character(c) in ("hanja", "variation_selector"))


def compact(text):
    return "".join(c for c in text if not c.isspace())


def reading_text(record):
    return "\n".join(c.get("text", "") for r in record.get("merged", {}).get("regions", [])
                     for p in r.get("paragraphs", []) for c in p.get("columns", []))


def source_text(record):
    """Serialize actual text fields, including tables; exclude commentary metadata.

    Layout boundaries add no characters. Literal text, spaces and uncertainty
    marks are retained. The reference uses the same cell/line concatenation.
    """
    parts = []
    for region in record.get("merged", {}).get("regions", []):
        if region.get("title_text"):
            parts.append(region["title_text"])
        parts.extend(c.get("text", "") for p in region.get("paragraphs", [])
                     for c in p.get("columns", []))
        parts.extend(cell for row in region.get("table_rows", []) for cell in row.get("cells", []))
    return "".join(parts)


def input_mode(item):
    mode = item.get("mode") or ("horizontal_zh" if item.get("orientation") == "horizontal" else "vertical")
    if mode not in ("vertical", "horizontal_zh"):
        raise ValueError("Input mode must be vertical or horizontal_zh")
    if item.get("orientation") not in (None, "horizontal", "vertical"):
        raise ValueError("Input orientation must be horizontal or vertical")
    if item.get("orientation") and (mode == "horizontal_zh") != (item["orientation"] == "horizontal"):
        raise ValueError("Input mode and orientation disagree")
    return mode


def inference_inputs(path):
    """Reject gold and arbitrary nested fields before any model client exists."""
    value = read_json(path)
    allowed = {"id", "source_path", "source_sha256", "crop_box_px", "image_path",
               "image_sha256", "width", "height", "mode", "orientation", "render_dpi", "pdf_page"}
    top_allowed = {"schema_version", "items", "frozen_date", "development_canvases",
                   "holdout_canvases", "selection_policy"}  # Original vertical input manifest.
    items = value.get("items", [])
    if set(value) - top_allowed or not items or any(set(item) - allowed for item in items):
        raise ValueError("Inference manifest must contain image metadata only, never gold or notes")
    ids = [item.get("id", "") for item in items]
    if len(set(ids)) != len(ids) or any(not re.fullmatch(r"[A-Za-z0-9_-]+", i) for i in ids):
        raise ValueError("Input IDs must be unique safe filenames")
    for item in items:
        input_mode(item)
        if Path(item["image_path"]).suffix.lower() not in (".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp"):
            raise ValueError("Inference image must be a raster, never HWPX/PDF text")
        if item.get("source_path") and Path(item["source_path"]).suffix.lower() == ".hwpx":
            raise ValueError("HWPX source belongs to evaluation only")
        if digest(item["image_path"]) != item["image_sha256"]:
            raise ValueError("Input image hash mismatch")
        if item.get("source_path") and digest(item["source_path"]) != item["source_sha256"]:
            raise ValueError("Original scan hash mismatch")
    return items


class RecordedReader:
    """Keep structured raw replies before read_one adjusts region coordinates."""
    def __init__(self, reader, path):
        self.reader, self.path = reader, Path(path)

    def __getattr__(self, name):
        method = getattr(self.reader, name)
        if name not in {"read_page", "reread_region", "reread_regions"}:
            return method

        def call(*args, **kwargs):
            result = method(*args, **kwargs)
            raw = ({k: v.model_dump() for k, v in result.items()} if isinstance(result, dict)
                   else result.model_dump())
            with self.path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps({"method": name, "response": raw}, ensure_ascii=False) + "\n")
            return result
        return call


def baseline(inputs, out_dir, *, provider, model, api_key=None, max_calls=32,
             baseline_cap=12, second_pass=True):
    from yeone_ocr import cli, prompts
    from yeone_ocr.pdfio import open_pdf
    from yeone_ocr.reader_gemini import CALL_CAP, set_call_cap
    from yeone_ocr.readers import create_reader
    from yeone_ocr.state import Work
    if not 1 <= baseline_cap <= max_calls <= 36:
        raise ValueError("Require 1 <= baseline_cap <= max_calls <= 36")
    items = inference_inputs(inputs)
    modes = {input_mode(item) for item in items}
    if len(modes) != 1:
        raise ValueError("One acquisition must use one reading mode")
    mode = modes.pop()
    root = Path(out_dir)
    root.mkdir(parents=True, exist_ok=False)  # Never overwrite historical work.
    manifest = {"schema_version": 1, "inputs_manifest": str(Path(inputs).resolve()),
                "inputs_sha256": digest(inputs), "provider": provider, "model": model,
                "max_calls": max_calls, "baseline_cap": baseline_cap,
                "baseline_scope": "cli.read_one pass1+pass2" if second_pass else "cli.read_one pass1 only",
                "use_cache": False, "items": [], "baseline_calls": 0,
                "baseline_complete": False, "gold_loaded_for_inference": False, "mode": mode}
    manifest_path = root / "run.json"
    write_json(manifest_path, manifest)
    cli.apply_mode(argparse.Namespace(mode=mode))
    set_call_cap(baseline_cap)
    reader = create_reader(provider, model, api_key=api_key, use_cache=False)
    try:
        for item in items:
            unit = root / item["id"]
            unit.mkdir()
            work = Work(item["image_path"], str(unit / "work"))
            work.ensure_source(300)
            args = cli.build_parser().parse_args(["read", item["image_path"], "--work", str(work.dir),
                                                  "--mode", mode, "--upscale", "1", "--no-cache"])
            args.no_second_pass = not second_pass
            wrapped = RecordedReader(reader, unit / "baseline-raw.jsonl")
            with open_pdf(item["image_path"]) as doc:
                cli.read_one(work, doc, wrapped, wrapped, 1, args)
            record = work.record_path(1)
            frozen = unit / "baseline-record.json"
            frozen.write_bytes(record.read_bytes())
            saved_record = read_json(frozen)
            manifest["items"].append({**item, "work_dir": str(work.dir.resolve()),
                                      "record_path": str(record.resolve()),
                                      "baseline_path": str(frozen.resolve()),
                                      "baseline_sha256": digest(frozen),
                                      "pass2_region_count": len(saved_record.get("pass2", {})),
                                      "pass2_failed_regions": [k for k, v in saved_record.get("pass2", {}).items()
                                                               if v.get("error") or v.get("failed")]})
            manifest["baseline_calls"] = CALL_CAP.used
            write_json(manifest_path, manifest)
        manifest["baseline_complete"] = True
    finally:
        manifest["baseline_calls"] = CALL_CAP.used
        write_json(manifest_path, manifest)
    return manifest


def reread(run_path, *, api_key=None, max_candidates_per_page=6):
    from yeone_ocr.small_annotations import scan_work, reread_work
    run = read_json(run_path)
    if not run.get("baseline_complete"):
        raise ValueError("Baseline is incomplete")
    if not 1 <= max_candidates_per_page <= 16:
        raise ValueError("Candidate cap must be between 1 and 16")
    if run.get("reread_started"):
        raise ValueError("This acquisition was already started; preserve it and use a new run")
    run["reread_started"] = True
    run["reread_calls"] = 0
    run["candidate_selection"] = "Detector order only; first N per page; no gold-based selection"
    run["max_candidates_per_page"] = max_candidates_per_page
    write_json(run_path, run)
    for item in run["items"]:
        if digest(item["record_path"]) != item["baseline_sha256"]:
            raise ValueError("Baseline record changed before detection")
        if digest(item["image_path"]) != item["image_sha256"]:
            raise ValueError("Input image changed")
        bundle = scan_work(item["image_path"], item["work_dir"], pages=[1])
        candidates = bundle["pages"][0]["candidates"]
        selected = candidates[:max_candidates_per_page]
        ids = [c["id"] for c in selected]
        item["selected_candidate_ids"] = ids
        remaining = run["max_calls"] - run["baseline_calls"] - run["reread_calls"]
        if remaining > 0 and ids:
            bundle = reread_work(item["image_path"], item["work_dir"], pages=[1], candidate_ids=ids,
                                 provider=run["provider"], model=run["model"], api_key=api_key,
                                 max_calls=min(remaining, sum(len(c["subcolumns"]) for c in selected)))
            run["reread_calls"] += bundle["run"]["attempted_calls"]
        item["record_preserved"] = digest(item["record_path"]) == item["baseline_sha256"]
        target = Path(item["baseline_path"]).parent / "reread-output.json"
        write_json(target, bundle)
        item["reread_path"] = str(target.resolve())
        item["reread_sha256"] = digest(target)
        write_json(run_path, run)
        if not item["record_preserved"]:
            raise ValueError("Reread changed baseline record")
    run["reread_complete"] = True
    write_json(run_path, run)
    return run


def box_iou(a, b):
    x0, y0 = max(a[0], b[0]), max(a[1], b[1])
    x1, y1 = min(a[2], b[2]), min(a[3], b[3])
    intersection = max(0, x1 - x0) * max(0, y1 - y0)
    area = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - intersection
    return intersection / area if area > 0 else 0.0


def match_boxes(notes, candidates, threshold=0.5):
    """Maximum-cardinality one-to-one matching; prefer larger IoU on ties."""
    edges = {i: sorted((j for j, c in enumerate(candidates)
                        if box_iou(note["bbox_px"], c["bbox_px"]) >= threshold),
                       key=lambda j: (-box_iou(note["bbox_px"], candidates[j]["bbox_px"]), j))
             for i, note in enumerate(notes)}
    matched = {}

    def augment(i, visited):
        for j in edges[i]:
            if j not in visited:
                visited.add(j)
                if j not in matched or augment(matched[j], visited):
                    matched[j] = i
                    return True
        return False
    for i in range(len(notes)):
        augment(i, set())
    return {i: j for j, i in matched.items()}


def ordered_presence(text, right, left):
    start = text.find(right)
    while start >= 0:
        if text.find(left, start + len(right)) >= 0:
            return True
        start = text.find(right, start + 1)
    return False


def evaluate(gold_path, run_path):
    """Read gold only after acquisition; never create/import an inference client."""
    gold, run = read_json(gold_path), read_json(run_path)
    if gold.get("schema_version") == "horizontal-hwpx-source-gold-v1":
        return evaluate_horizontal(gold_path, run_path, gold, run)
    by_id = {i["id"]: i for i in run["items"]}
    rows = []
    for item in gold["items"]:
        acquired = by_id[item["id"]]
        if acquired["image_sha256"] != item["image_sha256"]:
            raise ValueError("Gold and run use different pixels")
        for path, expected in [(acquired["baseline_path"], acquired["baseline_sha256"]),
                               (acquired["reread_path"], acquired["reread_sha256"]),
                               (acquired["record_path"], acquired["baseline_sha256"]),
                               (item["image_path"], item["image_sha256"])]:
            if digest(path) != expected:
                raise ValueError("Frozen input or output hash mismatch")
        base_raw = reading_text(read_json(acquired["baseline_path"]))
        base = han_text(base_raw)
        bundle = read_json(acquired["reread_path"])
        candidates = bundle["pages"][0]["candidates"]
        matches = match_boxes(item["notes"], candidates, gold.get("detection_iou_threshold", 0.5))
        for i, note in enumerate(item["notes"]):
            candidate = candidates[matches[i]] if i in matches else None
            text = candidate.get("combined_text", "") if candidate else ""
            reference = note["text"]
            sides = note["subcolumns"]
            before = reference in base
            proposal = han_text(text)
            after = before or proposal == reference
            row = {"id": note["id"], "detected": candidate is not None,
                   "iou": box_iou(note["bbox_px"], candidate["bbox_px"]) if candidate else 0.0,
                   "candidate_id": candidate["id"] if candidate else None,
                   "proposal_status": candidate.get("status") if candidate else "not_detected",
                   "baseline_exact_pair_present": before,
                   "baseline_exact_sides_in_order": ordered_presence(base, sides[0]["text"], sides[1]["text"]),
                   "baseline_raw_pair_present": compact(note.get("raw_text", reference)) in compact(base_raw),
                   "baseline_or_proposal_exact_pair_present": after,
                   "proposal_text": text, "reference": reference,
                   "proposal_strict_han": compare_texts(reference, proposal)["raw"],
                   "proposal_raw_codepoint": compare_texts(compact(note.get("raw_text", reference)), compact(text))["raw"],
                   "proposal_reversed_exact": bool(proposal and proposal == sides[1]["text"] + sides[0]["text"] and proposal != reference),
                   "subcolumn_order_right_left": bool(candidate and [s["side"] for s in candidate["subcolumns"]] == ["right", "left"]),
                   "record_bytes_preserved": True}
            rows.append(row)
    def total(field):
        return sum(bool(row[field]) for row in rows)
    errors = sum(row["proposal_strict_han"]["distance"] for row in rows)
    count = sum(len(row["reference"]) for row in rows)
    result = {"schema_version": 1, "gold_sha256": digest(gold_path), "run_sha256": digest(run_path),
              "gold_status": gold["status"], "scope": gold["scope"], "text_policy": gold["text_policy"],
              "raw_text_policy": gold.get("raw_text_policy"),
              "baseline_cer": None, "baseline_cer_reason": "No baseline character coordinates: ROI substring cannot be measured without an oracle.",
              "precision": None, "precision_reason": "Gold labels selected ROIs only, not every annotation on the page.",
              "selected_roi_count": len(rows), "detected_selected_rois": total("detected"),
              "selected_roi_detection_recall": total("detected") / len(rows) if rows else None,
              "baseline_exact_pairs": total("baseline_exact_pair_present"),
              "baseline_or_proposal_exact_pairs": total("baseline_or_proposal_exact_pair_present"),
              "proposal_cer_including_missing_as_deletions": errors / count if count else None,
              "proposal_reference_codepoints": count, "proposal_edit_distance": errors,
              "body_record_bytes_preserved": all(row["record_bytes_preserved"] for row in rows),
              "automatic_body_edits": 0, "rows": rows}
    return result


def distinct_occurrences(text, references):
    """Consume non-overlapping exact occurrences in source order, without reuse.

    These are occurrence counts, not spatial localization. A missing reference
    does not advance the cursor, allowing later independent notes to match.
    """
    cursor, spans = 0, []
    for reference in references:
        start = text.find(reference, cursor) if reference else -1
        span = [start, start + len(reference)] if start >= 0 else None
        spans.append(span)
        if span:
            cursor = span[1]
    return spans


def _aggregate(rows, field):
    distance = sum(row[field]["edit_distance"] for row in rows)
    length = sum(row[field]["reference_length"] for row in rows)
    return {"edit_distance": distance, "reference_codepoints": length,
            "cer": distance / length if length else None}


def evaluate_horizontal(gold_path, run_path, gold=None, run=None):
    """HWPX exact text with N horizontal lines; inference never imports this gold.

    Baseline exact occurrence counts cannot establish which repeated ROI was
    read. Do not union those anonymous occurrences with located proposals.
    """
    gold, run = gold or read_json(gold_path), run or read_json(run_path)
    if len(run["items"]) != 1:
        raise ValueError("Horizontal source gold currently describes one raster page")
    acquired = run["items"][0]
    if acquired["image_sha256"] != gold["image_sha256"]:
        raise ValueError("Gold and run use different pixels")
    if acquired.get("source_sha256") != gold["source_pdf_sha256"]:
        raise ValueError("Gold and run use different source PDF")
    paths = [(acquired["baseline_path"], acquired["baseline_sha256"]),
             (acquired["reread_path"], acquired["reread_sha256"]),
             (acquired["record_path"], acquired["baseline_sha256"]),
             (gold["image_path"], gold["image_sha256"]),
             (gold["source_hwpx"], gold["source_hwpx_sha256"]),
             (gold["source_pdf"], gold["source_pdf_sha256"])]
    if gold.get("derived_from_gold_path"):
        paths.append((gold["derived_from_gold_path"], gold["derived_from_gold_sha256"]))
    for path, expected in paths:
        if digest(path) != expected:
            raise ValueError("Frozen input or output hash mismatch")
    baseline_record = read_json(acquired["baseline_path"])
    baseline_raw = source_text(baseline_record)
    bundle = read_json(acquired["reread_path"])
    candidates = bundle["pages"][0]["candidates"]
    notes = gold["notes"]
    matches = match_boxes(notes, candidates, gold.get("detection_iou_threshold", 0.5))
    width, height = gold.get("image_size", [acquired.get("width"), acquired.get("height")])
    baseline_regions = []
    if width and height:
        for region in baseline_record.get("merged", {}).get("regions", []):
            if len(region.get("bbox", [])) != 4:
                continue
            y0, x0, y1, x1 = region["bbox"]
            baseline_regions.append({"id": region.get("id"),
                                     "bbox_px": [x0*width/1000, y0*height/1000, x1*width/1000, y1*height/1000],
                                     "text": source_text({"merged": {"regions": [region]}})})
    baseline_matches = match_boxes(notes, baseline_regions, gold.get("detection_iou_threshold", 0.5))
    occurrence_sets = {}
    for name, projection in [("raw_codepoint", lambda t: t), ("without_whitespace", compact),
                              ("han_diagnostic", han_text)]:
        occurrence_sets[name] = distinct_occurrences(projection(baseline_raw),
                                                     [projection(n["text"]) for n in notes])
    rows = []
    for i, note in enumerate(notes):
        candidate = candidates[matches[i]] if i in matches else None
        text = candidate.get("combined_text", "") if candidate else ""
        reference = note["text"]
        baseline_region = baseline_regions[baseline_matches[i]] if i in baseline_matches else None
        sides = candidate.get("subcolumns", []) if candidate else []
        y_positions = [s.get("bbox_px", [0, -1, 0, -1])[1] for s in sides]
        row = {"id": note["id"], "kind": note["kind"], "reference_line_count": len(note["lines"]),
               "detected": candidate is not None,
               "iou": box_iou(note["bbox_px"], candidate["bbox_px"]) if candidate else 0.0,
               "candidate_id": candidate["id"] if candidate else None,
               "proposal_status": candidate.get("status") if candidate else "not_detected",
               "proposal_text": text, "reference": reference,
               "proposal_source_exact": compare_texts(reference, text)["raw"],
               "proposal_without_whitespace": compare_texts(compact(reference), compact(text))["raw"],
               "proposal_han_diagnostic": compare_texts(han_text(reference), han_text(text))["raw"],
               "baseline_occurrence_spans": {name: spans[i] for name, spans in occurrence_sets.items()},
               "baseline_occurrence_is_spatially_localized": False,
               "candidate_line_count": len(sides),
               "line_count_matches_reference": bool(candidate and len(sides) == len(note["lines"])),
               "candidate_line_order_top_bottom": bool(sides and all(y >= 0 for y in y_positions)
                                                       and y_positions == sorted(y_positions)),
               "record_bytes_preserved": True}
        row.update(baseline_region_id=baseline_region["id"] if baseline_region else None,
                   baseline_region_matched=baseline_region is not None,
                   baseline_region_iou=box_iou(note["bbox_px"], baseline_region["bbox_px"]) if baseline_region else 0.0,
                   baseline_region_text=baseline_region["text"] if baseline_region else None)
        for name, projection in [("source_exact", lambda t: t), ("without_whitespace", compact),
                                  ("han_diagnostic", han_text)]:
            row["baseline_region_" + name] = (compare_texts(projection(reference), projection(baseline_region["text"]))["raw"]
                                              if baseline_region else None)
        rows.append(row)
    page_metrics = None
    if "reference_page_text" in gold:
        reference = gold["reference_page_text"]
        page_metrics = {"source_exact": compare_texts(reference, baseline_raw)["raw"],
                        "without_whitespace": compare_texts(compact(reference), compact(baseline_raw))["raw"],
                        "han_diagnostic": compare_texts(han_text(reference), han_text(baseline_raw))["raw"]}
    fields = ("proposal_source_exact", "proposal_without_whitespace", "proposal_han_diagnostic")
    result = {"schema_version": 2, "evaluation_kind": "horizontal_hwpx_source",
              "gold_sha256": digest(gold_path), "run_sha256": digest(run_path),
              "gold_status": gold["gold_status"], "scope": gold["scope"], "text_policy": gold["text_policy"],
              "sample_role": "User development sample; not independent holdout",
              "selected_roi_count": len(rows), "detected_selected_rois": len(matches),
              "selected_roi_detection_recall": len(matches) / len(rows) if rows else None,
              "baseline_distinct_exact_occurrences": {name: sum(s is not None for s in spans)
                                                       for name, spans in occurrence_sets.items()},
              "baseline_occurrence_policy": "Greedy monotonic non-overlapping consumption; an occurrence is never reused for another note; no spatial attribution is inferred.",
              "baseline_or_proposal_exact_notes": None,
              "combined_score_reason": "Baseline occurrences are not localized; union with repeated-note proposals could double-count the same source location.",
              "baseline_roi_cer": None,
              "baseline_roi_cer_reason": "No glyph-coordinate substring CER. Whole-region spatial CER is separately available only for matched regions.",
              "baseline_region_policy": "Maximum-cardinality one-to-one region bbox IoU>=threshold matching, no text-based selection. Score all text in each matched region, including extraneous content. Unmatched regions are not scored as empty. Added after observing baseline region structure; inference and gold unchanged.",
              "baseline_region_matched_count": len(baseline_matches),
              "baseline_region_metrics_matched_only": {
                  f: _aggregate([r for r in rows if r["baseline_region_matched"]], "baseline_region_" + f)
                  for f in ("source_exact", "without_whitespace", "han_diagnostic")},
              "paired_region_metrics": {
                  "matched_roi_count": sum(r["baseline_region_matched"] and r["detected"] for r in rows),
                  **{f: {"baseline": _aggregate([r for r in rows if r["baseline_region_matched"] and r["detected"]], "baseline_region_" + f),
                         "proposal": _aggregate([r for r in rows if r["baseline_region_matched"] and r["detected"]], "proposal_" + f)}
                     for f in ("source_exact", "without_whitespace", "han_diagnostic")}},
              "baseline_page_metrics": page_metrics,
              "baseline_page_text": baseline_raw,
              "proposal_metrics_including_missing_as_deletions": {f: _aggregate(rows, f) for f in fields},
              "proposal_metrics_matched_only": {f: _aggregate([r for r in rows if r["detected"]], f) for f in fields},
              "subsets": {label: {"roi_count": len(subset), "detected": sum(r["detected"] for r in subset),
                                   "proposal_source_exact": _aggregate(subset, "proposal_source_exact")}
                          for label, subset in [("two_line", [r for r in rows if r["reference_line_count"] == 2]),
                                                ("multiline", [r for r in rows if r["reference_line_count"] > 2])]},
              "detected_candidates": len(candidates), "unmatched_candidates": len(candidates)-len(matches),
              "precision": None,
              "precision_reason": "Unmatched candidates may be partial or duplicated notes; requires independent visual classification.",
              "body_record_bytes_preserved": True, "automatic_body_edits": 0, "rows": rows}
    return result


def configured_credentials(provider):
    """Read only selected provider values, never serialize or print configuration."""
    from yeone_ocr.gui import load_config, provider_profile
    cfg = load_config()
    profile = provider_profile(cfg, provider)
    return profile["model"], profile["api_key"]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("baseline")
    p.add_argument("inputs"); p.add_argument("out")
    p.add_argument("--provider", default="gemini"); p.add_argument("--model")
    p.add_argument("--max-calls", type=int, default=32)
    p.add_argument("--baseline-cap", type=int, default=12)
    p.add_argument("--first-pass-only", action="store_true")
    p = sub.add_parser("reread"); p.add_argument("run")
    p.add_argument("--max-candidates-per-page", type=int, default=6)
    p = sub.add_parser("evaluate"); p.add_argument("gold"); p.add_argument("run"); p.add_argument("out")
    args = parser.parse_args(argv)
    if args.command == "evaluate":
        write_json(args.out, evaluate(args.gold, args.run))
    elif args.command == "baseline":
        model, key = configured_credentials(args.provider)
        baseline(args.inputs, args.out, provider=args.provider, model=args.model or model,
                 api_key=key, max_calls=args.max_calls, baseline_cap=args.baseline_cap,
                 second_pass=not args.first_pass_only)
    else:
        run = read_json(args.run)
        _model, key = configured_credentials(run["provider"])
        reread(args.run, api_key=key, max_candidates_per_page=args.max_candidates_per_page)
    print("Saved benchmark artifacts; no credentials printed.")


if __name__ == "__main__":
    main()
