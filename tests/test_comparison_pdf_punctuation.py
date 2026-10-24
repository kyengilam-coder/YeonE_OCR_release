"""Synthetic PDF/source evidence tests; no user documents or model calls."""
import copy
import hashlib
import json
from types import SimpleNamespace

import pymupdf
import pytest
from PIL import Image

from yeone_ocr import comparison_pdf_punctuation as pdf


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def fixture(tmp_path, *, text="ABCD:EFGH", snapshot=None, hidden=False, opacity=1, occluded=False, duplicate=False, rotation=0, clipped=False, as_image=False, crop=None, hidden_anchors=False, black_background=False):
    source = tmp_path / "synthetic.pdf"
    with pymupdf.open() as document:
        page = document.new_page(width=240, height=100)
        if black_background:
            page.draw_rect(page.rect, color=None, fill=(0, 0, 0))
        if hidden_anchors:
            font = pymupdf.Font("helv")
            page.insert_text((25, 50), "ABCD", fontsize=16, fontname="helv", render_mode=3)
            x = 25 + font.text_length("ABCD", fontsize=16)
            page.insert_text((x, 50), ":", fontsize=16, fontname="helv")
            page.insert_text((x + font.text_length(":", fontsize=16), 50), "EFGH", fontsize=16, fontname="helv", render_mode=3)
        else:
            page.insert_text((25, 50), text, fontsize=16, fontname="helv", render_mode=3 if hidden else 0, fill_opacity=opacity)
        if duplicate:
            page.insert_text((25, 80), text, fontsize=16, fontname="helv")
        if occluded:
            page.draw_rect(pymupdf.Rect(20, 25, 180, 55), color=None, fill=(1, 1, 1), overlay=True)
        if clipped:
            stream = page.get_contents()[0]
            document.update_stream(stream, b"q 0 0 5 5 re W n\n" + document.xref_stream(stream) + b"\nQ")
        if rotation:
            page.set_rotation(rotation)
        document.save(source)
    with pymupdf.open(source) as document:
        page = document[0]
        pix = page.get_pixmap(dpi=300, colorspace=pymupdf.csRGB, alpha=False, annots=True)
        bounds = page.get_text("blocks")[0][:4] if page.get_text("blocks") else (25, 30, 150, 50)
        box = [v * 300 / 72 for v in bounds]
    selected = source
    width, height = pix.width, pix.height
    if as_image:
        selected = tmp_path / "synthetic.png"
        image = Image.frombytes("RGB", (width, height), pix.samples)
        if crop is not None:
            image = image.crop(crop)
            box = [box[0] - crop[0], box[1] - crop[1], box[2] - crop[0], box[3] - crop[1]]
        image.save(selected)
        width, height = image.size
    snapshot = text if snapshot is None else snapshot
    source_sha = hashlib.sha256(selected.read_bytes()).hexdigest()
    raw = {"text": snapshot, "words": [{"text": snapshot, "bbox": box}], "source_sha256": source_sha,
           "page_number": 1, "image_width": width, "image_height": height}
    run = {"id": "synthetic-run", "raw_sha256": digest(raw), "raw": raw, "request": {"dpi": 300}}
    page_data = {"runs": [run], "baseline_run_id": run["id"]}
    project = SimpleNamespace(source_path=selected, source_sha256=source_sha, page_count=1,
                              page=lambda number: copy.deepcopy(page_data), text=lambda number: snapshot)
    before = snapshot[4]
    item = {"id": "synthetic-proposal", "kind": "punctuation", "before": before, "after": "：" if before == ":" else ":",
            "start": 4, "end": 5, "snapshot_sha256": hashlib.sha256(snapshot.encode()).hexdigest(),
            "baseline_run_id": run["id"], "baseline_raw_sha256": run["raw_sha256"], "source_sha256": source_sha, "page_number": 1,
            "guard_evidence": {"content_left": "ABCD", "content_right": "EFGH", "punctuation_ordinal": 0, "expected_punctuation_count": 1},
            "evidence": {"run_id": run["id"], "raw_sha256": run["raw_sha256"], "context_start": 0, "context_end": len(snapshot), "bbox": box}}
    kwargs = {"pdf_path": source, "pdf_page": 1, "crop": crop} if as_image else {}
    return project, item, source, kwargs


def test_visible_source_can_only_hold_without_patching(tmp_path):
    project, item, _, kwargs = fixture(tmp_path)
    original = copy.deepcopy(item)
    capture = pdf.capture(project, 1, **kwargs)
    assert capture["status"] == "available"
    assert pdf.validate_capture(capture, project.source_sha256, 1)
    verdict = pdf.assess(capture, item)
    assert verdict["status"] == "blocked" and verdict["blocked"]
    assert verdict["native_glyph"]["char"] == ":" and item == original
    assert "after" not in verdict and "replacement" not in verdict
    assert pdf.assert_fresh(capture)


def test_original_agrees_with_candidate_is_separate_evidence(tmp_path):
    project, item, _, kwargs = fixture(tmp_path, snapshot="ABCD：EFGH")
    verdict = pdf.assess(pdf.capture(project, 1, **kwargs), item)
    assert verdict["status"] == "source_supports_candidate" and not verdict["blocked"]


@pytest.mark.parametrize("options", [{"hidden": True}, {"opacity": 0}, {"occluded": True}, {"duplicate": True}, {"rotation": 90}, {"clipped": True}])
def test_hidden_covered_duplicate_rotated_clipped_never_veto(tmp_path, options):
    project, item, _, kwargs = fixture(tmp_path, **options)
    capture = pdf.capture(project, 1, **kwargs)
    assert pdf.assess(capture, item)["status"] == "unavailable"
    assert not pdf.assess(capture, item)["blocked"]


def test_exact_pixel_bound_image_crop_maps_real_coordinates(tmp_path):
    project, item, _, kwargs = fixture(tmp_path, as_image=True, crop=(50, 50, 850, 350))
    capture = pdf.capture(project, 1, **kwargs)
    assert capture["status"] == "available"
    assert capture["crop"] == [50, 50, 850, 350]
    verdict = pdf.assess(capture, item)
    assert verdict["status"] == "blocked" and verdict["native_glyph"]["input_bbox"][0] >= 0
    assert pdf.capture(project, 1)["reason_codes"] == ["explicit-pdf-origin-required"]


def test_wrong_image_pixels_or_wrong_page_do_not_use_native_layer(tmp_path):
    project, item, _, kwargs = fixture(tmp_path, as_image=True)
    with Image.open(project.source_path) as source:
        changed = source.copy()
    changed.putpixel((0, 0), (0, 0, 0))
    changed.save(project.source_path)
    project.source_sha256 = hashlib.sha256(project.source_path.read_bytes()).hexdigest()
    cap = pdf.capture(project, 1, **kwargs)
    assert cap["status"] == "unavailable" and cap["reason_codes"] == ["pdf-input-rgb-pixel-mismatch"]
    assert pdf.assess(cap, item)["status"] == "unavailable"
    assert pdf.capture(project, 1, pdf_path=kwargs["pdf_path"], pdf_page=2)["status"] == "unavailable"


def test_transparent_png_hidden_rgb_is_not_original_ocr_pixels(tmp_path):
    project, _, _, kwargs = fixture(tmp_path, as_image=True)
    with Image.open(project.source_path) as source:
        invisible = source.convert("RGBA")
    invisible.putalpha(0)
    invisible.save(project.source_path)
    project.source_sha256 = hashlib.sha256(project.source_path.read_bytes()).hexdigest()
    cap = pdf.capture(project, 1, **kwargs)
    assert cap["status"] == "unavailable"
    assert cap["reason_codes"] == ["pdf-input-rgb-pixel-mismatch"]


def test_capture_is_deterministic_and_pure_replay_detects_tampering(tmp_path):
    project, item, _, kwargs = fixture(tmp_path)
    cap = pdf.capture(project, 1, **kwargs)
    assert cap == pdf.capture(project, 1, **kwargs)
    bad = copy.deepcopy(cap)
    bad["chars"][4]["char"] = "；"
    with pytest.raises(pdf.PDFPunctuationError):
        pdf.validate_capture(bad, project.source_sha256, 1)
    with pytest.raises(pdf.PDFPunctuationError):
        pdf.validate_capture(cap, "0" * 64, 1)
    with pytest.raises(pdf.PDFPunctuationError):
        pdf.validate_capture(cap, project.source_sha256, 2)
    assert pdf.assess(bad, item)["status"] == "unavailable"


def test_source_kind_binding_pair_and_color_must_remain_structurally_valid(tmp_path):
    project, _, _, kwargs = fixture(tmp_path)
    cap = pdf.capture(project, 1, **kwargs)
    for field, change in (("source_binding", "native-render-crop-exact-RGB-identity"), ("source_sha256", "0" * 64)):
        bad = copy.deepcopy(cap)
        bad[field] = change
        bad["sha256"] = pdf._digest({k: v for k, v in bad.items() if k != "sha256"})
        with pytest.raises(pdf.PDFPunctuationError):
            pdf.validate_capture(bad, bad["source_sha256"], 1)
    bad = copy.deepcopy(cap)
    del bad["chars"][4]["color"]
    bad["sha256"] = pdf._digest({k: v for k, v in bad.items() if k != "sha256"})
    with pytest.raises(pdf.PDFPunctuationError):
        pdf.validate_capture(bad, project.source_sha256, 1)


def test_modified_pdf_source_rejected_even_after_valid_capture(tmp_path):
    project, _, source, kwargs = fixture(tmp_path)
    cap = pdf.capture(project, 1, **kwargs)
    source.write_bytes(source.read_bytes() + b"\n")
    with pytest.raises(pdf.PDFPunctuationError):
        pdf.assert_fresh(cap)
    assert pdf.capture(project, 1)["reason_codes"] == ["project-source-changed"]


def test_baseline_snapshot_box_and_count_mismatches_are_unavailable(tmp_path):
    project, item, _, kwargs = fixture(tmp_path)
    cap = pdf.capture(project, 1, **kwargs)
    changes = [{"baseline_raw_sha256": "0" * 64}, {"snapshot_sha256": "0" * 64}, {"page_number": 2}]
    for fields in changes:
        assert pdf.assess(cap, {**item, **fields})["status"] == "unavailable"
    bad = copy.deepcopy(item)
    bad["evidence"]["bbox"][0] += 1
    assert pdf.assess(cap, bad)["reason_codes"] == ["actual-ocr-region-mismatch"]
    bad = copy.deepcopy(item)
    bad["guard_evidence"]["expected_punctuation_count"] = 2
    assert pdf.assess(cap, bad)["status"] == "unavailable"


def test_han_or_variation_selectors_are_not_candidates(tmp_path):
    project, item, _, kwargs = fixture(tmp_path)
    cap = pdf.capture(project, 1, **kwargs)
    for before, after in (("甲", "乙"), ("\ufe00", "\ufe01"), (": ", "：")):
        verdict = pdf.assess(cap, {**item, "before": before, "after": after})
        assert verdict["status"] == "unavailable" and not verdict["blocked"]


def test_invisible_content_anchors_cannot_address_visible_punctuation(tmp_path):
    project, item, _, kwargs = fixture(tmp_path, hidden_anchors=True)
    cap = pdf.capture(project, 1, **kwargs)
    assert cap["status"] == "available"
    verdict = pdf.assess(cap, item)
    assert verdict["status"] == "unavailable"
    assert verdict["reason_codes"] == ["native-address-glyph-hidden-or-unsupported"]


def test_black_text_on_identical_background_is_not_visible_proof(tmp_path):
    project, item, _, kwargs = fixture(tmp_path, black_background=True)
    cap = pdf.capture(project, 1, **kwargs)
    assert cap["status"] == "available"
    verdict = pdf.assess(cap, item)
    assert verdict["status"] == "unavailable"
    assert verdict["reason_codes"] == ["native-address-glyph-uniform-pixels"]


def test_frozen_rgb_extrema_malformed_or_pixel_position_forged_rejected(tmp_path):
    project, _, _, kwargs = fixture(tmp_path)
    cap = pdf.capture(project, 1, **kwargs)
    for key, changed in (("rgb_extrema", [[0, 256]] * 3), ("pixel_bbox", [0, 0, 1, 1])):
        bad = copy.deepcopy(cap)
        bad["chars"][4][key] = changed
        bad["sha256"] = pdf._digest({k: v for k, v in bad.items() if k != "sha256"})
        with pytest.raises(pdf.PDFPunctuationError):
            pdf.validate_capture(bad, project.source_sha256, 1)
