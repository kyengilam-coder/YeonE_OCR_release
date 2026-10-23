"""Selection uses current OCR evidence, never a hidden reference or confidence."""
import copy
import json
import threading
from unittest.mock import patch

from PIL import Image
import pytest

from ab_ocr import ocr
from ab_ocr import runtime
from yeone_ocr import automatic_ocr as auto
from yeone_ocr.comparison import ComparisonProject, ComparisonError, latest_runs


@pytest.fixture
def project(tmp_path):
    source = tmp_path / "synthetic.png"
    Image.new("RGB", (400, 200), "white").save(source)
    return ComparisonProject.open(source)


def reading(project, engine, text, confidence=80):
    return ocr.RecognitionResult(text=text, engine=engine, source_path=str(project.source_path),
        source_sha256=project.source_sha256, page_number=1, image_width=400, image_height=200,
        engine_version="synthetic", languages="chi_tra", psm=6 if engine == "tesseract" else None,
        duration_seconds=.01, words=[{"text": text, "bbox": [10, 10, 390, 100], "confidence": confidence}] if text else [])


def execute(project, texts, **options):
    def recognize(source, *, engine, **kwargs):
        value = texts[engine]
        if isinstance(value, Exception):
            raise value
        return reading(project, engine, value)
    with patch.object(auto, "available_engines", return_value=(list(texts), {})), \
            patch.object(auto, "memory_status", return_value=None), \
            patch.object(ocr, "recognize_file", side_effect=recognize):
        return auto.run_automatic_page(project, 1, **options)


def test_omission_and_garbled_readings_lose_to_complete_agreeing_text(project):
    texts = {"ppocrv6": "天地玄黃，宇宙洪荒。", "rapidocr": "天地玄黃，宇宙洪荒。",
             "easyocr": "天地", "tesseract": "\ufffd" * 30}
    result = execute(project, texts)
    assert result["selection"]["selected_engine"] == "ppocrv6"
    assert result["selection"]["applied"]
    assert result["selection"]["needs_review"]
    assert project.text(1) == texts["ppocrv6"]
    for engine, run in latest_runs(project.page(1)).items():
        assert run["raw"]["text"] == texts[engine]
    project.compare_page(1)
    assert project.page(1)["baseline_engine"] == "ppocrv6"


def test_high_model_confidence_does_not_override_bad_text(project):
    for engine, text, confidence in (("ppocrv6", "山川風雨", 20), ("easyocr", "\ufffd\ufffd\ufffd", 100)):
        project.add_result(reading(project, engine, text, confidence))
    selected = auto.assess_readings(list(latest_runs(project.page(1)).values()), "traditional")
    assert selected["selected_engine"] == "ppocrv6"


def test_two_engines_agreeing_on_a_small_fragment_do_not_hide_spatial_omissions(project):
    project.add_result(reading(project, "ppocrv6", "天地玄黃宇宙洪荒日月盈昃辰宿列張"))
    for engine in ("rapidocr", "easyocr"):
        raw = reading(project, engine, "天地").to_dict()
        raw["words"][0]["bbox"] = [10, 10, 40, 30]
        project.add_result(raw)
    selected = auto.assess_readings(list(latest_runs(project.page(1)).values()), "traditional")
    assert selected["selected_engine"] == "ppocrv6"
    assert selected["needs_review"]


def test_script_variants_and_punctuation_are_preserved_in_one_whole_reading(project):
    text = "爲為、羣群、裏裡、臺台。"
    result = execute(project, {"ppocrv6": text, "rapidocr": text})
    assert project.text(1) == text
    assert not result["selection"]["needs_review"]
    assert project.page(1)["corrected_text"] is None
    assert project.page(1)["edits"] == []


def test_empty_or_replacement_only_results_do_not_produce_a_selection(project):
    result = execute(project, {"ppocrv6": "", "rapidocr": "\ufffd\ufffd"})
    assert result["selection"]["selected_engine"] is None
    assert not result["selection"]["applied"]
    assert result["selection"]["needs_review"]
    assert "automatic" in result["errors"]


def test_failed_engine_cannot_select_its_stale_history(project):
    project.add_result(reading(project, "ppocrv6", "過去の別結果"))
    result = execute(project, {"ppocrv6": ocr.OCRError("synthetic failure"), "rapidocr": "山川風雨"}, force=True)
    assert result["selection"]["selected_engine"] == "rapidocr"
    assert project.text(1) == "山川風雨"
    assert result["errors"]
    assert len(project.page(1)["runs"]) == 2


@pytest.mark.parametrize("protect", ["manual", "correction", "reviewed"])
def test_human_baseline_correction_and_review_are_preserved(project, protect):
    original = project.add_result(reading(project, "tesseract", "原文"))
    if protect == "manual":
        project.choose_baseline(1, "tesseract")
    elif protect == "correction":
        project.set_text(1, "人が校訂した文字")
    else:
        project.set_reviewed(1)
    before = project.text(1)
    edits = copy.deepcopy(project.page(1)["edits"])
    result = execute(project, {"ppocrv6": "新的結果", "easyocr": "新的結果"})
    assert not result["selection"]["applied"]
    assert result["selection"]["preserved_human_work"]
    assert project.text(1) == before
    assert project.page(1)["baseline_run_id"] == original
    assert project.page(1)["edits"] == edits


def test_cancel_checkpoints_raw_results_without_selecting(project):
    stop = threading.Event()
    with patch.object(auto, "available_engines", return_value=(["ppocrv6", "easyocr"], {})), \
            patch.object(ocr, "recognize_file", side_effect=lambda *a, **k: reading(project, k["engine"], "原文")):
        outcome = auto.run_automatic_page(project, 1, should_stop=stop.is_set, on_result=lambda _: stop.set())
    assert outcome["cancelled"]
    assert len(project.page(1)["runs"]) == 1
    assert not project.page(1).get("auto_selections")


def test_selection_audit_survives_reopen_and_rejects_broken_run_references(project, tmp_path):
    execute(project, {"ppocrv6": "原文", "easyocr": "原文"})
    path = tmp_path / "selection.json"
    project.save(path)
    loaded = ComparisonProject.load(path)
    assert loaded.page(1)["auto_selections"] == project.page(1)["auto_selections"]
    assert loaded.text(1) == "原文"
    value = json.loads(path.read_text(encoding="utf-8"))
    value["pages"]["1"]["auto_selections"][0]["scores"][0]["run_id"] = "0" * 32
    path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(ComparisonError, match="자동 엔진"):
        ComparisonProject.load(path)


def test_mixed_profile_only_uses_engine_covering_both_hangul_and_hanja():
    with patch.object(auto, "memory_status", return_value=None), \
            patch.object(ocr, "doctor", return_value={"available": True, "languages": ["kor_vert", "chi_tra_vert"]}) as inspect:
        ready, excluded = auto.available_engines("mixed", "vertical")
    assert ready == ["tesseract"]
    assert len(excluded) == 3
    inspect.assert_called_once()


def test_low_commit_skips_heavy_models_without_importing_or_starting_them():
    with patch.object(auto, "memory_status", return_value={"available_commit_mb": 700, "available_physical_mb": 17000}), \
            patch.object(ocr, "doctor", return_value={"available": True, "languages": ["chi_tra"]}) as inspect:
        ready, excluded = auto.available_engines("traditional", "horizontal")
    assert "easyocr" not in ready and "메모리" in excluded["easyocr"]
    assert "ppocrv6" in ready
    assert inspect.call_count == 3


def test_unavailable_models_are_excluded_without_downloads():
    with patch.object(auto, "memory_status", return_value=None), \
            patch.object(ocr, "doctor", return_value={"available": False, "message": "missing"}):
        ready, excluded = auto.available_engines("traditional", "horizontal")
    assert ready == [] and len(excluded) == 4


def test_no_available_engine_is_an_explicit_error(project):
    with patch.object(auto, "available_engines", return_value=([], {})), pytest.raises(ComparisonError):
        auto.run_automatic_page(project, 1)
