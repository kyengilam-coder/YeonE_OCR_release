"""Source-preserving document projects and per-page OCR/correction checkpoints."""
from __future__ import annotations

import copy
from dataclasses import asdict, dataclass, field
import hashlib
import json
import math
import os
from pathlib import Path
import threading
import uuid

from . import ocr
from .reports import atomic_write

PROJECT_FORMAT = "ab-ocr-document"
PROJECT_VERSION = 1
MAX_PROJECT_BYTES = 128_000_000


class ProjectError(ValueError):
    pass


class SourceChangedError(ProjectError):
    pass


@dataclass(frozen=True)
class PageState:
    number: int
    raw: dict | None = None
    corrected_text: str | None = None
    reviewed: bool = False
    last_error: str = ""
    previous_recognitions: list[dict] = field(default_factory=list)

    @property
    def raw_text(self) -> str:
        return self.raw["text"] if self.raw is not None else ""

    @property
    def text(self) -> str:
        return self.corrected_text if self.corrected_text is not None else self.raw_text

    @property
    def status(self) -> str:
        if self.last_error:
            return "오류 · 이전 결과 보존" if self.raw is not None else "오류"
        if self.reviewed:
            return "검수 완료"
        if self.corrected_text is not None:
            return "교정 중"
        return "OCR 완료" if self.raw is not None else "미처리"


def _same_file(first: Path, second: Path) -> bool:
    return first.resolve() == second.resolve() or (first.exists() and second.exists() and os.path.samefile(first, second))


def _read_project(path: Path) -> tuple[dict, str]:
    with path.open("rb") as stream:
        data = stream.read(MAX_PROJECT_BYTES + 1)
    if len(data) > MAX_PROJECT_BYTES:
        raise ProjectError("프로젝트 파일이 허용 크기를 넘습니다.")
    def invalid_constant(value):
        raise ProjectError(f"프로젝트에 올바르지 않은 숫자가 있습니다: {value}")
    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ProjectError(f"프로젝트에 중복된 항목이 있습니다: {key}")
            result[key] = value
        return result
    try:
        payload = json.loads(data.decode("utf-8-sig"), parse_constant=invalid_constant,
                             object_pairs_hook=unique_object)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ProjectError("프로젝트 파일이 손상되었습니다. 마지막 정상 저장본 또는 복구 파일을 여세요.") from exc
    if (not isinstance(payload, dict) or payload.get("format") != PROJECT_FORMAT
            or type(payload.get("version")) is not int or payload["version"] != PROJECT_VERSION):
        raise ProjectError("지원하지 않는 AB_OCR 프로젝트 형식입니다. 파일은 변경하지 않았습니다.")
    return payload, hashlib.sha256(data).hexdigest()


class DocumentProject:
    """Mutation and snapshot methods are thread-safe; callers serialize disk saves.

    The source and raw recognition are never edited. Failed OCR attempts retain
    previous results; explicit recovery files contain complete reopenable projects.
    """
    def __init__(self, source_path: Path, source_sha256: str, page_count: int, kind: str = ""):
        self.source_path = Path(source_path).resolve()
        self.source_sha256 = source_sha256
        self.page_count = page_count
        self.kind = kind
        self.project_path: Path | None = None
        self._document_id = uuid.uuid4().hex
        self._pages: dict[int, PageState] = {}
        self._lock = threading.RLock()
        self._revision = self._saved_revision = 0
        self._disk_sha256: str | None = None

    @classmethod
    def open(cls, source: str | Path) -> "DocumentProject":
        info = ocr.document_info(Path(source).expanduser().resolve())
        return cls(Path(info["source_path"]), info["source_sha256"], info["page_count"], info.get("kind", ""))

    @classmethod
    def load(cls, path: str | Path, source_path: str | Path | None = None) -> "DocumentProject":
        path = Path(path).expanduser().resolve()
        payload, disk_hash = _read_project(path)
        source = payload.get("source")
        if not isinstance(source, dict) or not isinstance(source.get("path"), str):
            raise ProjectError("프로젝트 원본 정보가 올바르지 않습니다.")
        document_id = payload.get("document_id")
        if not isinstance(document_id, str) or not document_id:
            raise ProjectError("프로젝트 식별자가 올바르지 않습니다.")
        resolved_source = Path(source_path).expanduser() if source_path is not None else Path(source["path"])
        if not resolved_source.is_absolute():
            resolved_source = path.parent / resolved_source
        try:
            current_hash = ocr.file_sha256(resolved_source)
        except OSError as exc:
            raise SourceChangedError("원본 파일을 읽을 수 없습니다. 같은 원본의 새 위치를 지정하세요.") from exc
        if current_hash != source.get("sha256"):
            raise SourceChangedError("원본이 저장할 당시와 다릅니다. 원래 원본을 복원하거나 같은 파일을 다시 연결하세요.")
        try:
            project = cls.open(resolved_source)
        except (OSError, ocr.OCRError) as exc:
            raise SourceChangedError("원본 파일을 열 수 없습니다. 원본 위치와 파일 상태를 확인하세요.") from exc
        if (project.source_sha256 != source.get("sha256") or type(source.get("page_count")) is not int
                or project.page_count != source.get("page_count")):
            raise SourceChangedError("원본이 저장할 당시와 다릅니다. 원래 원본을 복원하거나 같은 파일을 다시 연결하세요.")
        if _same_file(path, project.source_path):
            raise ProjectError("프로젝트와 원본 경로는 달라야 합니다.")
        pages = payload.get("pages")
        if not isinstance(pages, dict):
            raise ProjectError("프로젝트의 쪽별 기록이 올바르지 않습니다.")
        for key, data in pages.items():
            if not isinstance(key, str) or not key.isdecimal() or str(int(key)) != key or not isinstance(data, dict):
                raise ProjectError("프로젝트 쪽 번호가 올바르지 않습니다.")
            n = int(key)
            project._check_page(n)
            if data.get("number") != n or type(data.get("number")) is not int:
                raise ProjectError("쪽 번호와 기록이 일치하지 않습니다.")
            raw = data.get("raw")
            if raw is not None:
                project._validate_recognition(raw, n)
            corrected, reviewed, last_error = data.get("corrected_text"), data.get("reviewed", False), data.get("last_error", "")
            history = data.get("previous_recognitions", [])
            if (corrected is not None and not isinstance(corrected, str)) or type(reviewed) is not bool or not isinstance(last_error, str):
                raise ProjectError("교정문 또는 검수 기록 형식이 올바르지 않습니다.")
            if not isinstance(history, list):
                raise ProjectError("이전 인식 기록 형식이 올바르지 않습니다.")
            for old in history:
                project._validate_recognition(old, n)
            project._pages[n] = PageState(n, raw, corrected, reviewed, last_error, history)
        project._document_id = document_id
        project.project_path = path
        project._disk_sha256 = disk_hash
        return project

    def _check_page(self, number: int) -> None:
        if type(number) is not int or not 1 <= number <= self.page_count:
            raise ProjectError(f"쪽 번호는 1~{self.page_count} 사이여야 합니다.")

    @property
    def dirty(self) -> bool:
        with self._lock:
            return self._revision != self._saved_revision

    @property
    def completed_pages(self) -> list[int]:
        with self._lock:
            return sorted(n for n, page in self._pages.items() if page.raw is not None)

    def page(self, number: int) -> PageState:
        self._check_page(number)
        with self._lock:
            return copy.deepcopy(self._pages.get(number, PageState(number)))

    def text(self, number: int) -> str:
        return self.page(number).text

    def has_corrections(self, number: int) -> bool:
        return self.page(number).corrected_text is not None

    def _replace(self, page: PageState) -> None:
        if self._pages.get(page.number, PageState(page.number)) != page:
            self._pages[page.number] = page
            self._revision += 1

    def set_text(self, number: int, text: str) -> None:
        if not isinstance(text, str):
            raise ProjectError("교정문은 문자열이어야 합니다.")
        with self._lock:
            page = self.page(number)
            if page.text == text:
                return
            data = asdict(page)
            data.update(corrected_text=None if page.raw is not None and text == page.raw_text else text, reviewed=False)
            self._replace(PageState(**data))

    def restore_raw(self, number: int) -> None:
        with self._lock:
            page = self.page(number)
            if page.raw is None:
                raise ProjectError("이 쪽에는 복원할 원시 인식 결과가 없습니다.")
            data = asdict(page)
            data.update(corrected_text=None, reviewed=False)
            self._replace(PageState(**data))

    def set_reviewed(self, number: int, reviewed: bool) -> None:
        if type(reviewed) is not bool:
            raise ProjectError("검수 여부는 참 또는 거짓이어야 합니다.")
        with self._lock:
            data = asdict(self.page(number))
            data["reviewed"] = reviewed
            self._replace(PageState(**data))

    def _validate_recognition(self, raw: dict, number: int) -> None:
        self._check_page(number)
        if not isinstance(raw, dict) or not isinstance(raw.get("text"), str):
            raise ProjectError("원시 OCR 기록에 텍스트가 없습니다.")
        if raw.get("source_sha256") != self.source_sha256 or raw.get("page_number") != number:
            raise SourceChangedError("OCR 결과의 원본 또는 쪽 번호가 현재 문서와 다릅니다.")
        width, height = raw.get("image_width"), raw.get("image_height")
        if type(width) is not int or type(height) is not int or width <= 0 or height <= 0:
            raise ProjectError("OCR 결과 이미지 크기가 올바르지 않습니다.")
        if not isinstance(raw.get("words"), list):
            raise ProjectError("OCR 결과의 단어 위치 목록이 올바르지 않습니다.")
        for word in raw["words"]:
            if not isinstance(word, dict) or not isinstance(word.get("text"), str):
                raise ProjectError("OCR 단어 기록이 올바르지 않습니다.")
            box = word.get("bbox")
            if (not isinstance(box, list) or len(box) != 4 or
                    any(type(v) not in (int, float) or not math.isfinite(v) for v in box) or
                    not (0 <= box[0] < box[2] <= width and 0 <= box[1] < box[3] <= height)):
                raise ProjectError("OCR 단어 좌표가 이미지 범위를 벗어났습니다.")
            confidence = word.get("confidence")
            if confidence is not None and (type(confidence) not in (int, float) or not math.isfinite(confidence) or not 0 <= confidence <= 100):
                raise ProjectError("OCR 단어 신뢰도가 올바르지 않습니다.")
        try:
            json.dumps(raw, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise ProjectError("OCR 기록에 저장할 수 없는 값이 있습니다.") from exc

    def apply_recognition(self, result, *, replace_corrected: bool = False) -> None:
        raw = result.to_dict() if isinstance(result, ocr.RecognitionResult) else copy.deepcopy(result)
        if not isinstance(raw, dict):
            raise ProjectError("OCR 결과 형식이 올바르지 않습니다.")
        number = raw.get("page_number")
        self._validate_recognition(raw, number)
        with self._lock:
            page = self.page(number)
            history = page.previous_recognitions
            if page.raw is not None and page.raw != raw:
                history.append(page.raw)
            self._replace(PageState(number, copy.deepcopy(raw), None if replace_corrected else page.corrected_text,
                                    False, "", history))

    def record_error(self, number: int, message: str) -> None:
        if not isinstance(message, str):
            raise ProjectError("실패 원인은 문자열이어야 합니다.")
        with self._lock:
            data = asdict(self.page(number))
            data["last_error"] = message
            self._replace(PageState(**data))

    def validate_source(self) -> None:
        try:
            current = ocr.file_sha256(self.source_path)
        except OSError as exc:
            raise SourceChangedError("원본을 읽을 수 없습니다. 원본 위치와 연결을 확인하세요.") from exc
        if current != self.source_sha256:
            raise SourceChangedError("작업 중 원본 파일이 변경되었습니다. 다른 원본에 결과를 적용하지 않습니다.")

    def to_dict(self) -> dict:
        with self._lock:
            return {"format": PROJECT_FORMAT, "version": PROJECT_VERSION, "document_id": self._document_id,
                    "source": {"path": str(self.source_path), "sha256": self.source_sha256,
                               "page_count": self.page_count, "kind": self.kind},
                    "pages": {str(n): asdict(page) for n, page in sorted(self._pages.items())}}

    def _target(self, path: str | Path) -> Path:
        target = Path(path).expanduser().resolve()
        if not target.name.lower().endswith(".abocr.json"):
            raise ProjectError("편집 프로젝트는 .abocr.json 파일로 저장하세요.")
        if _same_file(target, self.source_path):
            raise ProjectError("원본 파일을 덮어쓸 수 없습니다.")
        return target

    def _check_existing(self, target: Path, *, check_current: bool) -> None:
        if not target.exists():
            return
        previous, digest = _read_project(target)
        if previous.get("document_id") != self._document_id:
            raise ProjectError("다른 프로젝트가 있는 경로입니다. 다른 저장 이름을 선택하세요.")
        if check_current and self.project_path == target and self._disk_sha256 != digest:
            raise ProjectError("다른 창에서 프로젝트 파일이 변경되었습니다. 다른 이름으로 저장하세요.")

    def save(self, path: str | Path | None = None) -> Path:
        with self._lock:
            chosen = path if path is not None else self.project_path
            if chosen is None:
                raise ProjectError("프로젝트 저장 경로가 필요합니다.")
            target = self._target(chosen)
            snapshot, revision = self.to_dict(), self._revision
        self.validate_source()
        self._check_existing(target, check_current=True)
        content = json.dumps(snapshot, ensure_ascii=False, allow_nan=False, indent=2) + "\n"
        atomic_write(target, content, protected_paths=[self.source_path])
        with self._lock:
            self.project_path = target
            self._disk_sha256 = hashlib.sha256(content.encode("utf-8")).hexdigest()
            self._saved_revision = revision
        return target

    def save_recovery(self, path: str | Path) -> Path:
        target = self._target(path)
        if self.project_path is not None and _same_file(target, self.project_path):
            raise ProjectError("복구 파일은 정식 프로젝트와 다른 경로에 저장하세요.")
        snapshot = self.to_dict()
        # Checkpoints must preserve completed OCR/corrections even if an external
        # drive disconnects or the source is replaced. Reopening still requires
        # the original content hash, optionally relinked to its new location.
        snapshot["recovery"] = {"source_validation": "required_on_reopen"}
        self._check_existing(target, check_current=False)
        target.parent.mkdir(parents=True, exist_ok=True)
        content = json.dumps(snapshot, ensure_ascii=False, allow_nan=False, indent=2) + "\n"
        protected = [self.source_path] + ([self.project_path] if self.project_path else [])
        return atomic_write(target, content, protected_paths=protected)

    def export_text(self, path: str | Path, *, corrected: bool = True, page_numbers: list[int] | None = None) -> Path:
        target = Path(path).expanduser().resolve()
        if target.suffix.lower() != ".txt":
            raise ProjectError("텍스트 결과는 .txt 파일로 저장하세요.")
        numbers = list(page_numbers) if page_numbers is not None else list(range(1, self.page_count + 1))
        if not numbers or len(numbers) != len(set(numbers)):
            raise ProjectError("내보낼 쪽을 중복 없이 선택하세요.")
        with self._lock:
            states = [self.page(n) for n in numbers]
        if any(page.raw is None and (not corrected or page.corrected_text is None) for page in states):
            raise ProjectError("선택한 쪽 중 미처리 쪽이 있습니다. 완료한 쪽을 선택해서 내보내세요.")
        text = "\n\f\n".join(page.text if corrected else page.raw_text for page in states)
        protected = [self.source_path] + ([self.project_path] if self.project_path else [])
        return atomic_write(target, text, protected_paths=protected)
