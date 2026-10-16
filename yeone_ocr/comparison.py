"""Local OCR comparison projects with immutable readings and an append-only audit.

No model is called at import time.  Differences are proposals, never corrections.
All offsets refer to exact Python Unicode code points in the selected raw reading;
compatibility ideographs, variants, punctuation, and whitespace are not normalized.
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone
import difflib
import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile
import threading
import uuid

from ab_ocr import ocr

FORMAT = "yeone-ocr-comparison"
VERSION = 1
MAX_BYTES = 256_000_000
MAX_TEXT = 2_000_000
ENGINES = ("tesseract", "rapidocr", "easyocr")
PROFILES = ("traditional", "simplified", "mixed")
_HEX = re.compile(r"[a-f0-9]{64}")


class ComparisonError(ValueError):
    """A rejected operation leaves source and project files unchanged."""


class SourceChangedError(ComparisonError):
    pass


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _digest(value):
    return hashlib.sha256(_canonical(value)).hexdigest()


def _text(value, *, nullable=False):
    if nullable and value is None:
        return
    if not isinstance(value, str) or len(value) > MAX_TEXT:
        raise ComparisonError("문자열 기록의 형식 또는 길이가 올바르지 않습니다.")
    try:
        value.encode("utf-8")
    except UnicodeError as exc:
        raise ComparisonError("기록에 올바르지 않은 Unicode 문자가 있습니다.") from exc


def _json_copy(value):
    try:
        encoded = _canonical(value)
        if len(encoded) > MAX_BYTES:
            raise ValueError("size")
        return json.loads(encoded)
    except (TypeError, ValueError, RecursionError, UnicodeError) as exc:
        raise ComparisonError("기록은 유효한 JSON 값이어야 합니다.") from exc


def _same_file(a, b):
    return a.resolve() == b.resolve() or (a.exists() and b.exists() and os.path.samefile(a, b))


def _read(path):
    try:
        with path.open("rb") as stream:
            data = stream.read(MAX_BYTES + 1)
        if len(data) > MAX_BYTES:
            raise ComparisonError("프로젝트 파일이 허용 크기를 넘습니다.")
        def pairs(items):
            result = {}
            for key, value in items:
                if key in result:
                    raise ComparisonError("프로젝트에 중복된 JSON 항목이 있습니다.")
                result[key] = value
            return result
        def constant(_):
            raise ComparisonError("프로젝트에 유효하지 않은 숫자가 있습니다.")
        value = json.loads(data.decode("utf-8-sig"), object_pairs_hook=pairs, parse_constant=constant)
    except (OSError, UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ComparisonError("프로젝트를 읽을 수 없습니다. 정상 저장본이나 복구본을 확인하세요.") from exc
    if (not isinstance(value, dict) or value.get("format") != FORMAT
            or type(value.get("version")) is not int or value["version"] != VERSION):
        raise ComparisonError("지원하지 않는 연어OCR 비교 프로젝트 형식입니다.")
    return value, hashlib.sha256(data).hexdigest()


def _atomic_write(target, content, *, protected=(), before_replace=None):
    for original in protected:
        if original is not None and _same_file(target, Path(original)):
            raise ComparisonError("원본이나 프로젝트 파일을 덮어쓸 수 없습니다.")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="wb", prefix="." + target.name + ".", suffix=".tmp",
                                         dir=target.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        if before_replace is not None:
            before_replace()
        # Recheck hard links and path aliases after writing the temporary file.
        for original in protected:
            if original is not None and _same_file(target, Path(original)):
                raise ComparisonError("보호된 원본 경로에는 저장할 수 없습니다.")
        os.replace(temporary, target)
        temporary = None
        return target
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _blank(number):
    return {"number": number, "profile": "traditional", "direction": "horizontal", "runs": [],
            "baseline_engine": None, "baseline_run_id": None, "corrected_text": None,
            "edits": [], "reviewed": False, "review_history": [], "suggestions": [],
            "comparisons": [], "decisions": [], "last_errors": {}}


def _latest_runs(page):
    latest, historical = {}, {}
    for run in page.get("runs", []):
        (latest if run.get("active", True) else historical)[run["engine"]] = run
    return {**historical, **latest}


def latest_runs(page):
    """Snapshot latest active readings, falling back to imported history if needed."""
    return copy.deepcopy(_latest_runs(page))


def _han(char):
    n = ord(char)
    return (0x3400 <= n <= 0x9fff or 0xf900 <= n <= 0xfaff
            or 0x20000 <= n <= 0x323af)


def _hangul(char):
    n = ord(char)
    return 0xac00 <= n <= 0xd7a3 or 0x1100 <= n <= 0x11ff or 0x3130 <= n <= 0x318f


def _matcher(a, b):
    # Limit the quadratic repeated-character case on unusually long pages.
    return difflib.SequenceMatcher(None, a, b, autojunk=max(len(a), len(b)) > 20_000)


def _union(intervals):
    result = []
    for start, end in sorted(intervals):
        if result and start <= result[-1][1]:
            result[-1][1] = max(result[-1][1], end)
        else:
            result.append([start, end])
    return result


def _slice_aligned(text, opcodes, start, end):
    """Return candidate text and its exact range corresponding to a raw span."""
    parts, spans = [], []
    for tag, i1, i2, j1, j2 in opcodes:
        if tag == "insert":
            if start <= i1 <= end:
                parts.append(text[j1:j2])
                spans.append((j1, j2))
        elif i1 < end and i2 > start:
            if tag == "equal":
                left, right = j1 + max(start - i1, 0), j2 - max(i2 - end, 0)
            else:
                left, right = j1, j2
            parts.append(text[left:right])
            spans.append((left, right))
    if spans:
        return "".join(parts), min(x[0] for x in spans), max(x[1] for x in spans)
    # For an empty candidate at an insertion point, retain a location for evidence.
    location = next((j1 + min(max(start - i1, 0), j2 - j1)
                     for _, i1, i2, j1, j2 in opcodes if i1 <= start <= i2), len(text))
    return "", location, location


def _bbox(raw, start, end):
    position, selected = 0, []
    for word in raw.get("words", []):
        value = word.get("text", "")
        found = raw["text"].find(value, position)
        if not value or found < 0:
            continue
        position = found + len(value)
        if ((found < end and position > start)
                or (start == end and found <= start <= position)):
            selected.append(word["bbox"])
    if not selected:
        return None
    return [min(b[0] for b in selected), min(b[1] for b in selected),
            max(b[2] for b in selected), max(b[3] for b in selected)]


def _eligibility(run, span, baseline_span, explicitly_selected=False):
    if run["engine"] not in ENGINES and not explicitly_selected:
        return False, "가져온 판독은 비교 근거로만 보관하며 자동 합의에 참여하지 않습니다."
    languages = run["raw"].get("languages", "")
    has_han = any(_han(c) for c in span + baseline_span)
    has_kor = any(_hangul(c) for c in span + baseline_span)
    if run["engine"] in ("rapidocr", "easyocr"):
        if has_han and languages == "kor":
            return False, "이 한국어 모델은 한자를 지원하지 않아 누락을 합의 근거로 세지 않습니다."
        if has_kor and languages in ("chi_sim", "chi_tra"):
            return False, "이 중국어 모델은 한글을 지원하지 않습니다."
    return True, ""


def _run_profile(run):
    requested = run.get("request", {})
    languages = run["raw"].get("languages", "")
    profile = requested.get("profile")
    if profile is None:
        profile = "mixed" if "kor" in languages else ("simplified" if "chi_sim" in languages else "traditional")
    direction = requested.get("direction", "vertical" if "_vert" in languages or run["raw"].get("psm") == 5 else "horizontal")
    return profile, direction


def _compatible(first, second):
    a, b = first["raw"], second["raw"]
    if any(a.get(key) != b.get(key) for key in ("source_sha256", "page_number", "image_width", "image_height")):
        return False
    if _run_profile(first) != _run_profile(second):
        return False
    adpi, bdpi = first.get("request", {}).get("dpi"), second.get("request", {}).get("dpi")
    return adpi is None or bdpi is None or adpi == bdpi


def _validate_request(request):
    if not isinstance(request, dict):
        raise ComparisonError("판독 요청 조건은 JSON 객체여야 합니다.")
    if request and (request.get("profile") not in PROFILES
                    or request.get("direction") not in ("horizontal", "vertical")
                    or type(request.get("dpi")) is not int or not 72 <= request["dpi"] <= 600
                    or not isinstance(request.get("engine_root"), (str, type(None)))
                    or request.get("policy") != "local-three-engine-v1"):
        raise ComparisonError("판독 요청 조건 기록이 올바르지 않습니다.")
    manifest = request.get("installation_sha256")
    if manifest is not None and (not isinstance(manifest, str) or not _HEX.fullmatch(manifest)):
        raise ComparisonError("판독 모델 설치 기록 해시가 올바르지 않습니다.")


class ComparisonProject:
    """Thread-safe snapshots; only this class mutates raw-run storage and audit."""

    def __init__(self, source_path, source_sha256, page_count, kind=""):
        self.source_path = Path(source_path).expanduser().resolve()
        if not isinstance(source_sha256, str) or not _HEX.fullmatch(source_sha256):
            raise ComparisonError("원본 해시가 올바르지 않습니다.")
        if type(page_count) is not int or not 1 <= page_count <= 1_000_000:
            raise ComparisonError("원본 쪽 수가 올바르지 않습니다.")
        self.source_sha256 = source_sha256
        self.page_count = page_count
        if not isinstance(kind, str):
            raise ComparisonError("원본 문서 종류가 올바르지 않습니다.")
        self.kind = kind
        self.project_path = None
        self._id = uuid.uuid4().hex
        self._pages = {}
        self._lock = threading.RLock()
        self._revision = self._saved_revision = 0
        self._disk_sha256 = None

    @classmethod
    def open(cls, source):
        info = ocr.document_info(source)
        return cls(info["source_path"], info["source_sha256"], info["page_count"], info.get("kind", ""))

    @classmethod
    def load(cls, path, source_path=None, *, allow_missing_source=False):
        path = Path(path).expanduser().resolve()
        value, digest = _read(path)
        source = value.get("source")
        if not isinstance(source, dict) or not isinstance(source.get("path"), str):
            raise ComparisonError("원본 정보가 올바르지 않습니다.")
        selected = Path(source_path if source_path is not None else source["path"]).expanduser()
        if not selected.is_absolute():
            selected = path.parent / selected
        project = cls(selected, source.get("sha256"), source.get("page_count"), source.get("kind", ""))
        if _same_file(path, project.source_path):
            raise ComparisonError("프로젝트와 원본은 다른 파일이어야 합니다.")
        if not allow_missing_source or project.source_path.exists():
            project.validate_source()
            info = ocr.document_info(project.source_path)
            if info["page_count"] != project.page_count or info["source_sha256"] != project.source_sha256:
                raise SourceChangedError("원본 쪽 수 또는 내용이 저장할 당시와 다릅니다.")
        identifier = value.get("document_id")
        if not isinstance(identifier, str) or not re.fullmatch(r"[a-f0-9]{32}", identifier):
            raise ComparisonError("프로젝트 식별자가 올바르지 않습니다.")
        pages = value.get("pages")
        if not isinstance(pages, dict) or len(pages) > project.page_count:
            raise ComparisonError("쪽별 기록이 올바르지 않습니다.")
        run_ids = set()
        for key, page in pages.items():
            if not isinstance(key, str) or not key.isdecimal() or str(int(key)) != key:
                raise ComparisonError("쪽 번호 형식이 올바르지 않습니다.")
            number = int(key)
            project._check_page(number)
            project._validate_page(page, number, run_ids)
            project._pages[number] = copy.deepcopy(page)
        project._id = identifier
        project.project_path = path
        project._disk_sha256 = digest
        return project

    def _check_page(self, number):
        if type(number) is not int or not 1 <= number <= self.page_count:
            raise ComparisonError(f"쪽 번호는 1~{self.page_count} 사이여야 합니다.")

    def _validate_raw(self, raw, number):
        if (not isinstance(raw, dict) or raw.get("kind") != "recognition"
                or type(raw.get("schema_version")) is not int or raw["schema_version"] != 1
                or raw.get("source_sha256") != self.source_sha256
                or type(raw.get("page_number")) is not int or raw["page_number"] != number):
            raise ComparisonError("OCR 원시 기록의 형식·원본 해시·쪽 번호가 일치하지 않습니다.")
        _text(raw.get("text"))
        engine = raw.get("engine")
        if (not isinstance(engine, str) or not engine.strip() or len(engine) > 80
                or any(ord(c) < 32 for c in engine)):
            raise ComparisonError("OCR 엔진 이름이 올바르지 않습니다.")
        if raw.get("coordinate_system") != "rendered-image pixels, xyxy":
            raise ComparisonError("지원하지 않는 OCR 좌표계입니다.")
        for name in ("source_path", "engine_version", "languages"):
            if not isinstance(raw.get(name), str):
                raise ComparisonError("OCR 원시 메타데이터가 올바르지 않습니다.")
        width, height = raw.get("image_width"), raw.get("image_height")
        if type(width) is not int or type(height) is not int or not 0 < width <= 100_000 or not 0 < height <= 100_000:
            raise ComparisonError("OCR 이미지 크기가 올바르지 않습니다.")
        duration = raw.get("duration_seconds")
        if type(duration) not in (int, float) or not math.isfinite(duration) or duration < 0:
            raise ComparisonError("OCR 실행 시간 기록이 올바르지 않습니다.")
        psm = raw.get("psm")
        if psm is not None and (type(psm) is not int or psm < 0 or psm > 13):
            raise ComparisonError("OCR 영역 분할 기록이 올바르지 않습니다.")
        words = raw.get("words")
        if not isinstance(words, list) or len(words) > MAX_TEXT:
            raise ComparisonError("OCR 영역 목록이 올바르지 않습니다.")
        for word in words:
            if not isinstance(word, dict):
                raise ComparisonError("OCR 영역 기록이 올바르지 않습니다.")
            _text(word.get("text"))
            box = word.get("bbox")
            if (not isinstance(box, list) or len(box) != 4
                    or any(type(v) not in (int, float) or not math.isfinite(v) for v in box)
                    or not (0 <= box[0] < box[2] <= width and 0 <= box[1] < box[3] <= height)):
                raise ComparisonError("OCR 영역 좌표가 이미지 범위를 벗어났습니다.")
            confidence = word.get("confidence")
            if confidence is not None and (type(confidence) not in (int, float)
                                          or not math.isfinite(confidence) or not 0 <= confidence <= 100):
                raise ComparisonError("OCR 영역 신뢰도 기록이 올바르지 않습니다.")
        for name in ("preprocessing", "engine_metadata"):
            if not isinstance(raw.get(name, {}), dict):
                raise ComparisonError("OCR 처리 메타데이터가 올바르지 않습니다.")
        _json_copy(raw)

    def _validate_page(self, page, number, run_ids):
        if (not isinstance(page, dict) or type(page.get("number")) is not int or page["number"] != number
                or page.get("profile") not in PROFILES or page.get("direction") not in ("horizontal", "vertical")
                or type(page.get("reviewed")) is not bool):
            raise ComparisonError("쪽별 기록의 형식이 올바르지 않습니다.")
        _text(page.get("corrected_text"), nullable=True)
        runs = page.get("runs")
        if not isinstance(runs, list):
            raise ComparisonError("원시 판독 이력이 올바르지 않습니다.")
        local_ids = {}
        for run in runs:
            if (not isinstance(run, dict) or not isinstance(run.get("id"), str)
                    or not re.fullmatch(r"[a-f0-9]{32}", run["id"]) or run["id"] in run_ids
                    or not isinstance(run.get("created_at"), str) or not isinstance(run.get("request", {}), dict)):
                raise ComparisonError("판독 실행 식별자나 이력이 올바르지 않습니다.")
            if type(run.get("active", True)) is not bool:
                raise ComparisonError("판독 실행의 활성·보관 상태가 올바르지 않습니다.")
            self._validate_raw(run.get("raw"), number)
            _validate_request(run.get("request", {}))
            if run.get("engine") != run["raw"]["engine"] or run.get("raw_sha256") != _digest(run["raw"]):
                raise ComparisonError("원시 판독 보존 해시가 일치하지 않습니다.")
            run_ids.add(run["id"])
            local_ids[run["id"]] = run
        baseline = page.get("baseline_run_id")
        if baseline is not None and (baseline not in local_ids or page.get("baseline_engine") != local_ids[baseline]["engine"]):
            raise ComparisonError("기준 판독 연결이 올바르지 않습니다.")
        if baseline is None and (runs or page.get("baseline_engine") is not None):
            raise ComparisonError("기준 판독이 없습니다.")
        edits = page.get("edits")
        if not isinstance(edits, list):
            raise ComparisonError("교정 이력이 올바르지 않습니다.")
        state, edit_ids, active = None, set(), []
        for edit in edits:
            if (not isinstance(edit, dict) or not isinstance(edit.get("id"), str) or edit["id"] in edit_ids
                    or edit.get("kind") not in ("manual", "candidate", "undo")
                    or not isinstance(edit.get("at"), str) or not isinstance(edit.get("reason"), str)
                    or edit.get("before") != state):
                raise ComparisonError("교정 이력의 순서나 내용이 일치하지 않습니다.")
            _text(edit.get("before"), nullable=True)
            _text(edit.get("after"), nullable=True)
            if edit["kind"] == "undo":
                if not active or edit.get("undo_of") != active[-1]["id"] or edit["after"] != active[-1]["before"]:
                    raise ComparisonError("되돌리기 이력이 일치하지 않습니다.")
                active.pop()
            else:
                if edit["after"] is None:
                    raise ComparisonError("교정문은 문자열이어야 합니다.")
                active.append(edit)
            edit_ids.add(edit["id"])
            state = edit["after"]
        if state != page.get("corrected_text"):
            raise ComparisonError("현재 교정문과 교정 이력이 일치하지 않습니다.")
        errors = page.get("last_errors")
        if not isinstance(errors, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in errors.items()):
            raise ComparisonError("실패 기록이 올바르지 않습니다.")
        for key in ("suggestions", "comparisons", "decisions", "review_history"):
            if not isinstance(page.get(key), list):
                raise ComparisonError("비교·검수 이력이 올바르지 않습니다.")
        for suggestion in page["suggestions"]:
            self._validate_suggestion(suggestion, local_ids)
        for comparison in page["comparisons"]:
            if (not isinstance(comparison, dict) or comparison.get("baseline_run_id") not in local_ids
                    or not isinstance(comparison.get("run_ids"), list)
                    or any(identifier not in local_ids for identifier in comparison["run_ids"])
                    or not isinstance(comparison.get("suggestions"), list)):
                raise ComparisonError("이전 비교 근거가 올바르지 않습니다.")
            for suggestion in comparison["suggestions"]:
                self._validate_suggestion(suggestion, local_ids)
        for decision in page["decisions"]:
            if (not isinstance(decision, dict) or not isinstance(decision.get("suggestion_id"), str)
                    or not isinstance(decision.get("at"), str)
                    or decision.get("decision") not in ("accepted", "rejected", "undone")
                    or (decision["decision"] == "accepted" and not isinstance(decision.get("engine"), str))):
                raise ComparisonError("교정 후보 결정 이력이 올바르지 않습니다.")
        for review in page["review_history"]:
            if (not isinstance(review, dict) or not isinstance(review.get("at"), str)
                    or type(review.get("before")) is not bool or type(review.get("after")) is not bool):
                raise ComparisonError("사람의 검수 이력이 올바르지 않습니다.")
        _json_copy(page)

    def _validate_suggestion(self, suggestion, runs):
        if not isinstance(suggestion, dict) or suggestion.get("baseline_run_id") not in runs:
            raise ComparisonError("교정 후보의 기준 판독이 올바르지 않습니다.")
        raw = runs[suggestion["baseline_run_id"]]["raw"]["text"]
        start, end = suggestion.get("start"), suggestion.get("end")
        if (type(start) is not int or type(end) is not int or not 0 <= start <= end <= len(raw)
                or suggestion.get("baseline_text") != raw[start:end]
                or suggestion.get("decision") not in ("pending", "accepted", "rejected")
                or not isinstance(suggestion.get("id"), str)
                or not isinstance(suggestion.get("candidates"), dict) or not isinstance(suggestion.get("evidence"), list)):
            raise ComparisonError("교정 후보의 위치나 내용이 올바르지 않습니다.")
        for engine, text in suggestion["candidates"].items():
            if not isinstance(engine, str):
                raise ComparisonError("교정 후보 엔진이 올바르지 않습니다.")
            _text(text)
        seen = set()
        for evidence in suggestion["evidence"]:
            if (not isinstance(evidence, dict) or evidence.get("run_id") not in runs
                    or evidence.get("engine") != runs[evidence["run_id"]]["engine"]
                    or evidence.get("engine") not in suggestion["candidates"]
                    or evidence["engine"] in seen or type(evidence.get("eligible")) is not bool):
                raise ComparisonError("교정 후보의 판독 근거가 올바르지 않습니다.")
            seen.add(evidence["engine"])
            result = runs[evidence["run_id"]]
            left, right = evidence.get("start"), evidence.get("end")
            if (type(left) is not int or type(right) is not int
                    or not 0 <= left <= right <= len(result["raw"]["text"])
                    or result["raw"]["text"][left:right] != suggestion["candidates"][evidence["engine"]]
                    or evidence.get("bbox") != _bbox(result["raw"], left, right)):
                raise ComparisonError("교정 후보의 문자 범위나 좌표 근거가 일치하지 않습니다.")
            eligible, _ = _eligibility(result, suggestion["candidates"][evidence["engine"]],
                                        suggestion["baseline_text"], evidence["run_id"] == suggestion["baseline_run_id"])
            if eligible != evidence["eligible"]:
                raise ComparisonError("교정 후보의 모델 언어 지원 정보가 일치하지 않습니다.")
        if seen != set(suggestion["candidates"]):
            raise ComparisonError("교정 후보와 판독 근거 목록이 일치하지 않습니다.")

    @property
    def dirty(self):
        with self._lock:
            return self._revision != self._saved_revision

    @property
    def completed_pages(self):
        with self._lock:
            return sorted(n for n, page in self._pages.items() if page["runs"])

    def page(self, number):
        self._check_page(number)
        with self._lock:
            return copy.deepcopy(self._pages.get(number, _blank(number)))

    def _page(self, number):
        self._check_page(number)
        return self._pages.setdefault(number, _blank(number))

    def _baseline(self, page):
        return next((run for run in page["runs"] if run["id"] == page["baseline_run_id"]), None)

    @staticmethod
    def _invalidate_review(page, reason):
        if page["reviewed"]:
            page["review_history"].append({"at": _now(), "before": True, "after": False,
                                           "reason": reason, "actor": "system"})
            page["reviewed"] = False

    def text(self, number):
        with self._lock:
            page = self.page(number)
            baseline = self._baseline(page)
            return page["corrected_text"] if page["corrected_text"] is not None else (baseline["raw"]["text"] if baseline else "")

    def validate_source(self):
        try:
            current = ocr.file_sha256(self.source_path)
        except OSError as exc:
            raise SourceChangedError("원본을 읽을 수 없습니다. 원래 원본을 다시 연결하세요.") from exc
        if current != self.source_sha256:
            raise SourceChangedError("원본이 변경되었습니다. 다른 내용에 판독·교정을 적용하지 않습니다.")

    def add_result(self, result, *, request=None, activate=True):
        if type(activate) is not bool:
            raise ComparisonError("판독 활성 상태는 참 또는 거짓이어야 합니다.")
        raw = result.to_dict() if hasattr(result, "to_dict") else result
        raw = _json_copy(raw)
        number = raw.get("page_number") if isinstance(raw, dict) else None
        self._check_page(number)
        self._validate_raw(raw, number)
        request = _json_copy(request or {})
        _validate_request(request)
        with self._lock:
            self.validate_source()
            page = self._page(number)
            run = {"id": uuid.uuid4().hex, "engine": raw["engine"], "created_at": _now(),
                   "raw": raw, "raw_sha256": _digest(raw), "request": request, "active": activate}
            page["runs"].append(run)
            if page["baseline_run_id"] is None or (activate and page["baseline_engine"] == raw["engine"]):
                page["baseline_engine"], page["baseline_run_id"] = raw["engine"], run["id"]
            if activate:
                page["suggestions"] = []
                self._invalidate_review(page, "new-recognition")
                page["last_errors"].pop(raw["engine"], None)
            self._revision += 1
            return run["id"]

    def choose_baseline(self, number, engine):
        with self._lock:
            page = self._page(number)
            run = _latest_runs(page).get(engine)
            if run is None:
                raise ComparisonError("선택한 엔진의 판독 결과가 없습니다.")
            if page["baseline_run_id"] != run["id"]:
                page["baseline_engine"], page["baseline_run_id"] = engine, run["id"]
                page["suggestions"] = []
                self._invalidate_review(page, "baseline-changed")
                self._revision += 1
            return self.text(number)

    def compare_page(self, number, baseline=None):
        with self._lock:
            page = self._page(number)
            if not page["runs"]:
                return []
            if baseline is None:
                preferred = "tesseract" if page["profile"] == "mixed" else "easyocr"
                matching = [r for r in _latest_runs(page).values() if _run_profile(r) == (page["profile"], page["direction"])]
                candidates = [r for r in matching if r["engine"] == preferred]
                chosen = candidates[-1] if candidates else (matching[-1] if matching else self._baseline(page))
                if page["baseline_run_id"] != chosen["id"]:
                    self._invalidate_review(page, "baseline-changed")
                page["baseline_engine"], page["baseline_run_id"] = chosen["engine"], chosen["id"]
            else:
                self.choose_baseline(number, baseline)
            selected = self._baseline(page)
            latest, excluded = {}, []
            for run in _latest_runs(page).values():
                if run["engine"] in ENGINES or run["id"] == selected["id"]:
                    latest[run["engine"]] = run
            for engine, run in list(latest.items()):
                if not _compatible(selected, run):
                    excluded.append({"engine": engine, "run_id": run["id"],
                                     "reason": "원본 렌더링 크기·문서 종류·방향·DPI가 기준 판독과 다릅니다."})
                    del latest[engine]
            baseline_text = selected["raw"]["text"]
            alignments, intervals = {}, []
            for engine, run in latest.items():
                codes = _matcher(baseline_text, run["raw"]["text"]).get_opcodes()
                alignments[engine] = codes
                intervals.extend((i1, i2) for tag, i1, i2, _, _ in codes if tag != "equal")
            key = _digest({"baseline": selected["id"], "runs": [r["id"] for r in latest.values()], "excluded": excluded,
                           "policy": "exact-codepoint-v1"})
            old = {s["id"]: s for s in page["suggestions"]}
            suggestions = []
            for start, end in _union(intervals):
                candidates, evidence = {}, []
                for engine, run in latest.items():
                    value, left, right = _slice_aligned(run["raw"]["text"], alignments[engine], start, end)
                    if run["id"] == selected["id"]:
                        value, left, right = baseline_text[start:end], start, end
                    candidates[engine] = value
                    eligible, reason = _eligibility(run, value, baseline_text[start:end], run["id"] == selected["id"])
                    evidence.append({"engine": engine, "run_id": run["id"], "eligible": eligible,
                                     "eligibility_reason": reason, "start": left, "end": right,
                                     "bbox": _bbox(run["raw"], left, right)})
                values = set(candidates.values())
                if len(values) < 2:
                    continue
                whitespace = {"".join(t.split()) for t in values}
                kind = "whitespace" if len(whitespace) == 1 else ("insertion" if start == end else "difference")
                identifier = _digest([key, start, end])[:32]
                proposal = {"id": identifier, "comparison_id": key, "baseline_run_id": selected["id"],
                            "start": start, "end": end, "baseline_text": baseline_text[start:end],
                            "candidates": candidates, "evidence": evidence, "kind": kind, "decision": "pending"}
                if identifier in old:
                    for name in ("decision", "decided_at", "selected_engine"):
                        if name in old[identifier]:
                            proposal[name] = old[identifier][name]
                else:
                    decision = next((d for d in reversed(page["decisions"]) if d["suggestion_id"] == identifier), None)
                    if decision is not None and decision["decision"] in ("accepted", "rejected"):
                        proposal["decision"] = decision["decision"]
                        proposal["decided_at"] = decision["at"]
                        if "engine" in decision:
                            proposal["selected_engine"] = decision["engine"]
                suggestions.append(proposal)
            page["suggestions"] = suggestions
            if not any(c["id"] == key for c in page["comparisons"]):
                page["comparisons"].append({"id": key, "at": _now(), "baseline_run_id": selected["id"],
                                            "run_ids": [r["id"] for r in latest.values()],
                                            "excluded_runs": excluded,
                                            "policy": "exact-codepoint-v1; whitespace classified separately; no normalization",
                                            "suggestions": copy.deepcopy(suggestions)})
            self._revision += 1
            return copy.deepcopy(suggestions)

    def _set_text(self, number, text, reason, *, kind="manual", extra=None):
        _text(text)
        _text(reason)
        page = self._page(number)
        if page["corrected_text"] == text:
            return False
        edit = {"id": uuid.uuid4().hex, "at": _now(), "kind": kind,
                "before": page["corrected_text"], "after": text, "reason": reason}
        if extra:
            edit.update(_json_copy(extra))
        page["edits"].append(edit)
        page["corrected_text"] = text
        self._invalidate_review(page, "text-edited")
        self._revision += 1
        return True

    def set_text(self, number, text, reason="manual"):
        with self._lock:
            return self._set_text(number, text, reason)

    def undo_edit(self, number):
        with self._lock:
            page = self._page(number)
            active = []
            for edit in page["edits"]:
                if edit["kind"] == "undo":
                    if active:
                        active.pop()
                else:
                    active.append(edit)
            if not active:
                return False
            edit = active[-1]
            page["edits"].append({"id": uuid.uuid4().hex, "at": _now(), "kind": "undo", "reason": "undo",
                                  "undo_of": edit["id"], "before": page["corrected_text"], "after": edit["before"]})
            page["corrected_text"] = edit["before"]
            if edit["kind"] == "candidate":
                identifier = edit.get("suggestion_id")
                for suggestion in page["suggestions"]:
                    if suggestion["id"] == identifier:
                        suggestion["decision"] = "pending"
                        suggestion.pop("decided_at", None)
                        suggestion.pop("selected_engine", None)
                page["decisions"].append({"suggestion_id": identifier, "at": _now(), "decision": "undone"})
            self._invalidate_review(page, "edit-undone")
            self._revision += 1
            return True

    def set_reviewed(self, number, reviewed=True):
        if type(reviewed) is not bool:
            raise ComparisonError("검수 상태는 참 또는 거짓이어야 합니다.")
        with self._lock:
            page = self._page(number)
            if reviewed and not page["runs"] and page["corrected_text"] is None:
                raise ComparisonError("판독·교정할 내용이 없습니다.")
            if page["reviewed"] != reviewed:
                page["review_history"].append({"at": _now(), "before": page["reviewed"], "after": reviewed,
                                               "reason": "manual", "actor": "human"})
                page["reviewed"] = reviewed
                self._revision += 1

    @staticmethod
    def _mapped_span(baseline, current, start, end):
        if baseline == current:
            return start, end
        blocks = _matcher(baseline, current).get_matching_blocks()
        if start < end:
            for i, j, length in blocks:
                if i <= start and end <= i + length:
                    return j + start - i, j + end - i
        else:
            positions = {j + start - i for i, j, length in blocks if length and i <= start <= i + length}
            if len(positions) == 1:
                point = positions.pop()
                # Insertion at document boundaries cannot overwrite prior insertions.
                if (start != 0 or point == 0) and (start != len(baseline) or point == len(current)):
                    return point, point
        raise ComparisonError("해당 위치는 이미 교정되었습니다. 현재 문장을 원본과 직접 대조하세요.")

    def accept_candidate(self, number, suggestion_id, engine):
        with self._lock:
            page = self._page(number)
            suggestion = next((s for s in page["suggestions"] if s["id"] == suggestion_id), None)
            if suggestion is None or suggestion["decision"] != "pending":
                raise ComparisonError("처리할 수 있는 교정 후보가 없습니다.")
            if suggestion["baseline_run_id"] != page["baseline_run_id"]:
                raise ComparisonError("기준 판독이 변경되었습니다. 비교를 다시 계산하세요.")
            evidence = next((e for e in suggestion["evidence"] if e["engine"] == engine), None)
            if evidence is None or not evidence["eligible"]:
                raise ComparisonError("선택한 모델은 이 글자의 판독을 지원하지 않습니다. 직접 교정하세요.")
            baseline = self._baseline(page)["raw"]["text"]
            current = self.text(number)
            start, end = self._mapped_span(baseline, current, suggestion["start"], suggestion["end"])
            value = suggestion["candidates"][engine]
            replacement = current[:start] + value + current[end:]
            self._set_text(number, replacement, "candidate:" + suggestion_id + ":" + engine, kind="candidate",
                           extra={"suggestion_id": suggestion_id, "engine": engine, "applied_start": start, "applied_end": end})
            suggestion.update(decision="accepted", selected_engine=engine, decided_at=_now())
            page["decisions"].append({"suggestion_id": suggestion_id, "at": _now(), "decision": "accepted", "engine": engine})
            self._revision += 1
            return replacement

    def reject_candidate(self, number, suggestion_id):
        with self._lock:
            page = self._page(number)
            suggestion = next((s for s in page["suggestions"] if s["id"] == suggestion_id), None)
            if suggestion is None or suggestion["decision"] != "pending":
                raise ComparisonError("처리할 수 있는 교정 후보가 없습니다.")
            suggestion.update(decision="rejected", decided_at=_now())
            page["decisions"].append({"suggestion_id": suggestion_id, "at": _now(), "decision": "rejected"})
            self._revision += 1

    def record_error(self, number, engine, message):
        _text(message)
        with self._lock:
            self._page(number)["last_errors"][str(engine)] = message
            self._revision += 1

    def to_dict(self):
        with self._lock:
            return {"format": FORMAT, "version": VERSION, "document_id": self._id,
                    "source": {"path": str(self.source_path), "sha256": self.source_sha256,
                               "page_count": self.page_count, "kind": self.kind},
                    "pages": {str(n): copy.deepcopy(page) for n, page in sorted(self._pages.items())}}

    def _check_existing(self, target, *, check_current=True):
        if not target.exists():
            return
        previous, digest = _read(target)
        if previous.get("document_id") != self._id:
            raise ComparisonError("다른 프로젝트가 있는 경로입니다. 다른 이름으로 저장하세요.")
        if check_current and self.project_path == target and digest != self._disk_sha256:
            raise ComparisonError("다른 창에서 프로젝트가 변경되었습니다. 다른 이름으로 저장하세요.")

    def save(self, path=None):
        with self._lock:
            chosen = path if path is not None else self.project_path
            if chosen is None:
                raise ComparisonError("프로젝트 저장 경로가 필요합니다.")
            target = Path(chosen).expanduser().resolve()
            if _same_file(target, self.source_path):
                raise ComparisonError("원본 파일을 덮어쓸 수 없습니다.")
            self.validate_source()
            self._check_existing(target)
            content = json.dumps(self.to_dict(), ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8") + b"\n"
            if len(content) > MAX_BYTES:
                raise ComparisonError("프로젝트가 허용 크기를 넘습니다.")
            def verify():
                self.validate_source()
                self._check_existing(target)
            _atomic_write(target, content, protected=[self.source_path], before_replace=verify)
            self.project_path = target
            self._disk_sha256 = hashlib.sha256(content).hexdigest()
            self._saved_revision = self._revision
            return target

    def save_recovery(self, path):
        with self._lock:
            target = Path(path).expanduser().resolve()
            if self.project_path and _same_file(target, self.project_path):
                raise ComparisonError("복구 파일은 정식 프로젝트와 다른 이름으로 저장하세요.")
            self._check_existing(target, check_current=False)
            snapshot = self.to_dict()
            snapshot["recovery"] = {"source_validation": "required_before_ocr_or_export", "at": _now()}
            content = json.dumps(snapshot, ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8") + b"\n"
            if len(content) > MAX_BYTES:
                raise ComparisonError("복구 프로젝트가 허용 크기를 넘습니다.")
            return _atomic_write(target, content, protected=[self.source_path, self.project_path],
                                 before_replace=lambda: self._check_existing(target, check_current=False))

    def export_text(self, path, page_numbers=None):
        target = Path(path).expanduser().resolve()
        if target.suffix.lower() != ".txt":
            raise ComparisonError("교정문은 .txt 파일로 내보내세요.")
        numbers = list(page_numbers) if page_numbers is not None else list(range(1, self.page_count + 1))
        if not numbers or len(numbers) != len(set(numbers)):
            raise ComparisonError("내보낼 쪽을 중복 없이 선택하세요.")
        with self._lock:
            for number in numbers:
                page = self.page(number)
                if not page["runs"] and page["corrected_text"] is None:
                    raise ComparisonError("선택한 쪽 중 판독이 없는 쪽이 있습니다.")
            self.validate_source()
            text = "\n\f\n".join(self.text(n) for n in numbers)
            return _atomic_write(target, text.encode("utf-8"), protected=[self.source_path, self.project_path],
                                 before_replace=self.validate_source)


def _request(profile, direction, dpi, engine_root):
    from ab_ocr.runtime import default_engine_root
    root = str(default_engine_root(engine_root))
    return {"profile": profile, "direction": direction, "dpi": dpi, "engine_root": root,
            "policy": "local-three-engine-v1"}


def _models_unchanged(run, engine_root):
    """A resume checkpoint is valid only for the recorded installed model bytes."""
    engine = run["engine"]
    metadata = run["raw"].get("engine_metadata", {})
    models = metadata.get("model_files", {})
    if not isinstance(models, dict):
        return False
    folder = Path(engine_root) / "tools" / engine
    for name, record in models.items():
        if not isinstance(name, str) or Path(name).name != name or not isinstance(record, dict):
            return False
        expected = record.get("sha256")
        if not isinstance(expected, str) or not _HEX.fullmatch(expected):
            return False
        path = folder / ("tessdata" if engine == "tesseract" else "models") / (
            name + ".traineddata" if engine == "tesseract" else name)
        try:
            if ocr.file_sha256(path) != expected:
                return False
        except OSError:
            return False
    expected = metadata.get("executable_sha256")
    if expected is not None and engine == "tesseract":
        try:
            return ocr.file_sha256(folder / "tesseract.exe") == expected
        except OSError:
            return False
    return True


def run_page(project, number, profile="traditional", direction="horizontal", dpi=300,
             should_stop=None, on_result=None, timeout=300.0, engine_root=None, *, force=False):
    """Run three local engines sequentially; checkpoint each completed reading.

    A failed engine never removes successful raw results or human edits.  Model
    confidence is not used as an accuracy score or as permission to auto-correct.
    Vertical comparison remains experimental; existing YeonE vertical OCR remains
    independently available, and no external AI is called from this function.
    """
    if profile not in PROFILES or direction not in ("horizontal", "vertical"):
        raise ComparisonError("문서 종류 또는 읽기 방향을 확인하세요.")
    if type(dpi) is not int or not 72 <= dpi <= 600:
        raise ComparisonError("렌더링 DPI는 72~600이어야 합니다.")
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
        raise ComparisonError("엔진 제한 시간은 양수여야 합니다.")
    project._check_page(number)
    project.validate_source()
    request = _request(profile, direction, dpi, engine_root)
    outcome = {"completed": [], "errors": {}, "cancelled": False, "skipped": []}
    with project._lock:
        page = project._page(number)
        if page["profile"] != profile or page["direction"] != direction:
            page["profile"], page["direction"] = profile, direction
            page["suggestions"] = []
            project._revision += 1
    for engine in ENGINES:
        if should_stop is not None and should_stop():
            outcome["cancelled"] = True
            break
        engine_request = dict(request)
        manifest = Path(request["engine_root"]) / "tools" / engine / "installation.json"
        engine_request["installation_sha256"] = ocr.file_sha256(manifest) if manifest.is_file() else None
        existing = _latest_runs(project.page(number)).get(engine)
        if (not force and existing and existing.get("request") == engine_request
                and _models_unchanged(existing, request["engine_root"])):
            outcome["skipped"].append(engine)
            continue
        languages = ("kor+chi_tra" if engine == "tesseract" else "kor") if profile == "mixed" else (
            "chi_sim" if profile == "simplified" else "chi_tra")
        psm = 6 if engine == "tesseract" else None
        if direction == "vertical" and engine == "tesseract":
            # Vertical Chinese models have a different traineddata identifier.
            languages = languages.replace("chi_tra", "chi_tra_vert").replace("chi_sim", "chi_sim_vert")
            languages = languages.replace("kor", "kor_vert")
            psm = 5
        try:
            result = ocr.recognize_file(project.source_path, engine=engine, page_number=number,
                                        languages=languages, psm=psm, dpi=dpi, timeout=timeout,
                                        should_stop=should_stop, engine_root=engine_root)
            project.add_result(result, request=engine_request)
        except ocr.OCRCancelled:
            outcome["cancelled"] = True
            break
        except SourceChangedError:
            raise
        except (ocr.OCRError, OSError, ValueError) as exc:
            project.validate_source()
            # Engine subprocess stderr is deliberately not copied into a project.
            message = f"{engine} 인식 실패: 엔진 설치·언어·원본 상태를 확인하세요. ({type(exc).__name__})"
            project.record_error(number, engine, message)
            outcome["errors"][engine] = message
        else:
            outcome["completed"].append(engine)
            try:
                project.compare_page(number)
            finally:
                if on_result is not None:
                    # Even a comparison failure checkpoints the completed raw OCR.
                    # Checkpoint failures stop orchestration, never masquerade as
                    # an engine failure, and leave in-memory raw/history intact.
                    on_result(result)
    if project.page(number)["runs"]:
        project.compare_page(number)
    return outcome
