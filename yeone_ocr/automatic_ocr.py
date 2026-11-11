"""Offline page-level engine selection with inspectable quality estimates.

Scores are heuristics, never measured recognition accuracy. Select one complete
reading without rewriting text or mixing incompatible confidence scales.
"""
from __future__ import annotations

import difflib
import statistics
import math
import unicodedata

from ab_ocr import ocr
from ab_ocr.runtime import engine_root_for

POLICY = "local-auto-engine-quality-v1"
PREFERENCE = ("ppocrv6", "rapidocr", "easyocr", "tesseract")
# Conservative scheduling estimates, not model specifications. Free commit is
# often the limiting resource even when physical RAM is plentiful on Windows.
MEMORY_BUDGET_MB = {"ppocrv6": 640, "rapidocr": 640, "easyocr": 2048, "tesseract": 128}


def memory_status():
    import sys
    if sys.platform != "win32":
        return None
    import ctypes
    class MemoryStatus(ctypes.Structure):
        _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong),
                    *[(name, ctypes.c_ulonglong) for name in (
                        "physical", "available_physical", "commit", "available_commit", "virtual", "available_virtual", "extended")]]
    status = MemoryStatus()
    status.length = ctypes.sizeof(status)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
        return None
    return {"available_commit_mb": status.available_commit // (1024 * 1024),
            "available_physical_mb": status.available_physical // (1024 * 1024)}


def language_options(engine, profile, direction):
    languages = ("kor+chi_tra" if engine == "tesseract" else "kor") if profile == "mixed" else (
        "chi_sim" if profile == "simplified" else "chi_tra")
    if engine == "tesseract" and direction == "vertical":
        languages = "+".join(part + "_vert" for part in languages.split("+"))
    return languages


def available_engines(profile, direction, engine_root=None, should_stop=None, resident=()):
    from .comparison import ComparisonError, PROFILES
    if profile not in PROFILES or direction not in ("horizontal", "vertical"):
        raise ComparisonError("문서 종류 또는 읽기 방향을 확인하세요.")
    ready, excluded = [], {}
    memory = memory_status()
    for engine in PREFERENCE:
        if should_stop and should_stop():
            raise ocr.OCRCancelled("자동 선택을 중지했습니다.")
        # The Korean neural models do not cover Hanja. Do not treat their
        # omissions as evidence against a mixed Korean/Hanja reading.
        if profile == "mixed" and engine != "tesseract":
            excluded[engine] = "한글·한자를 함께 지원하는 모델이 필요합니다."
            continue
        # A resident worker (engine_pool) already holds its memory; free memory says nothing about it.
        if (memory and engine not in resident
                and min(memory["available_commit_mb"], memory["available_physical_mb"]) < MEMORY_BUDGET_MB[engine]):
            excluded[engine] = "메모리 할당 여유 부족으로 이번 쪽에서 제외했습니다."
            continue
        root = engine_root_for(engine, engine_root)
        try:
            info = ocr.doctor(engine=engine, engine_root=root, should_stop=should_stop)
        except ocr.OCRCancelled:
            raise
        except (ocr.OCRError, OSError, ValueError):
            excluded[engine] = "설치 상태를 확인하지 못했습니다."
            continue
        needed = language_options(engine, profile, direction)
        installed = set(info.get("languages", []))
        supported = needed in installed or (engine == "tesseract" and set(needed.split("+")) <= installed)
        if info.get("available") and supported:
            ready.append(engine)
        else:
            excluded[engine] = info.get("message", "선택 언어 모델이 없습니다.") if not info.get("available") else "선택 언어 모델이 없습니다."
    return ready, excluded


def _compact(text):
    return "".join(text.split())


def _han(char):
    value = ord(char)
    return 0x3400 <= value <= 0x9fff or 0xf900 <= value <= 0xfaff or 0x20000 <= value <= 0x323af


def assess_readings(runs, profile):
    """Rank complete current readings; never consult a reference/fixture text."""
    texts = {run["id"]: _compact(run["raw"]["text"]) for run in runs}
    lengths = [len(text) for text in texts.values() if text]
    median = statistics.median(lengths) if lengths else 1
    coverage = {}
    for run in runs:
        raw, cells = run["raw"], set()
        width, height = raw.get("image_width", 0), raw.get("image_height", 0)
        if width > 0 and height > 0:
            for word in raw.get("words", []):
                box = word.get("bbox", [])
                if len(box) != 4 or not word.get("text", "").strip():
                    continue
                x1, y1, x2, y2 = box
                left, right = max(0, int(48*x1/width)), min(48, max(1, math.ceil(48*x2/width)))
                top, bottom = max(0, int(48*y1/height)), min(48, max(1, math.ceil(48*y2/height)))
                cells.update(y*48+x for y in range(top, bottom) for x in range(left, right))
        coverage[run["id"]] = cells
    union = set().union(*coverage.values()) if coverage else set()
    records = []
    for run in runs:
        text = texts[run["id"]]
        size = len(text)
        invalid = sum(c == "\ufffd" or unicodedata.category(c) in ("Cc", "Cs", "Co")
                      or (unicodedata.category(c) == "Cn" and not _han(c)) for c in text)
        quality = max(0.0, 1 - 4 * invalid / max(1, size)) if size else 0.0
        letters = [c for c in text if unicodedata.category(c).startswith("L")]
        expected = sum(_han(c) or (profile == "mixed" and (0xac00 <= ord(c) <= 0xd7a3
                       or 0x1100 <= ord(c) <= 0x11ff or 0x3130 <= ord(c) <= 0x318f)) for c in letters)
        script_fit = expected / len(letters) if letters else 1.0
        peers = [other for identifier, other in texts.items() if identifier != run["id"] and other]
        agreements = [difflib.SequenceMatcher(None, text[:20000], other[:20000], autojunk=True).ratio()
                      for other in peers] if size else []
        agreement = sum(agreements) / len(agreements) if agreements else 0.0
        completeness = min(1.0, size / max(1, median))
        spatial_coverage = len(coverage[run["id"]]) / len(union) if union and coverage[run["id"]] else 1.0
        # Repeated detections at the same coordinates are distinct from repeated
        # lines on the page. Confidence values are intentionally not compared.
        words = run["raw"].get("words", [])
        boxes = [(w.get("text"), tuple(round(v, 1) for v in w.get("bbox", []))) for w in words]
        duplicates = (len(boxes) - len(set(boxes))) / max(1, len(boxes))
        score = max(0.0, min(1.0, .35 * quality + .20 * agreement + .10 * completeness + .15 * script_fit
                            + .20 * spatial_coverage - .10 * duplicates)) if size and quality else 0.0
        records.append({"engine": run["engine"], "run_id": run["id"], "score": round(score, 6),
                        "characters": size, "invalid_characters": invalid,
                        "agreement": round(agreement, 6), "completeness": round(completeness, 6),
                        "spatial_coverage": round(spatial_coverage, 6),
                        "script_fit": round(script_fit, 6), "duplicate_detections": round(duplicates, 6)})
    records.sort(key=lambda r: (-r["score"], PREFERENCE.index(r["engine"])))
    usable = [r for r in records if r["characters"] and r["score"] > 0]
    selected = usable[0] if usable else None
    disagreement = len({text for text in texts.values() if text}) > 1
    return {"policy": POLICY, "scores": records,
            "selected_engine": selected["engine"] if selected else None,
            "selected_run_id": selected["run_id"] if selected else None,
            "needs_review": selected is None or len(usable) == 1 or disagreement,
            "reason": "사용 가능한 단일 판독" if len(usable) == 1 else (
                "문자 품질·누락 정도·지원 문자·엔진 간 일치도에 따른 쪽별 추정" if selected else "사용 가능한 텍스트 없음")}


def run_automatic_page(project, number, profile="traditional", direction="horizontal", dpi=300,
                       should_stop=None, on_result=None, timeout=300.0, engine_root=None, *, force=False,
                       recognizer=None, parallel=1, resident=()):
    from .comparison import ComparisonError, latest_runs, run_page
    if type(dpi) is not int or not 72 <= dpi <= 600 or type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
        raise ComparisonError("렌더링 DPI와 엔진 제한 시간을 확인하세요.")
    project._check_page(number)
    project.validate_source()
    previous = project.page(number)
    preserve_baseline = bool(previous["reviewed"] or previous.get("baseline_choice") == "manual"
                             or previous["corrected_text"] is not None or previous["edits"])
    try:
        engines, excluded = (available_engines(profile, direction, engine_root, should_stop, resident=resident)
                             if resident else available_engines(profile, direction, engine_root, should_stop))
    except ocr.OCRCancelled:
        return {"completed": [], "errors": {}, "cancelled": True, "skipped": [], "unsupported": {}}
    if not engines:
        raise ComparisonError("선택한 문서 종류를 읽을 로컬 엔진이 없습니다. 설치 확인에서 언어 모델을 확인하세요.")
    if callable(parallel):
        parallel = parallel(engines)
    outcome = run_page(project, number, profile, direction, dpi, should_stop, on_result, timeout,
                       engine_root, force=force, engines=engines, recognizer=recognizer, parallel=parallel)
    outcome["unavailable"] = excluded
    if outcome["cancelled"] or (should_stop and should_stop()):
        outcome["cancelled"] = True
        return outcome
    current = latest_runs(project.page(number))
    successful = set(outcome["completed"]) | set(outcome["skipped"])
    runs = [current[engine] for engine in engines if engine in successful and engine in current]
    selection = assess_readings(runs, profile)
    selection.update(profile=profile, direction=direction, unavailable=excluded,
                     preserve_baseline=preserve_baseline, memory=memory_status(),
                     preserved_baseline_run_id=previous.get("baseline_run_id") if preserve_baseline else None)
    outcome["selection"] = project.record_auto_selection(number, selection)
    if selection["selected_engine"] is None:
        outcome["errors"]["automatic"] = "자동 선택에 사용할 텍스트를 인식하지 못했습니다. 원본을 확인하세요."
    return outcome
