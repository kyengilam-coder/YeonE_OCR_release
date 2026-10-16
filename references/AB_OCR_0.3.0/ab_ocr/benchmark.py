"""Reproducible, offline corpus evaluation of already-produced OCR transcripts.

This module never invokes an OCR engine. A missing page is an explicit failure,
and an incomplete evaluation cannot produce an engine ranking. All file paths
are relative to, and confined to, the manifest directory (including symlinks).
"""
from __future__ import annotations

import hashlib
import json
import platform
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path, PureWindowsPath
from typing import Any, Callable

from . import __version__
from .evaluation import NormalizationPolicy, compare_texts, normalize_text

BENCHMARK_VERSION = "1.0"
MAX_MANIFEST_BYTES = 2 * 1024 * 1024
MAX_TEXT_BYTES = 16 * 1024 * 1024


class ManifestError(ValueError):
    """Invalid or unsafe benchmark manifest."""


class BenchmarkCancelled(RuntimeError):
    """Evaluation stopped between page comparisons; input files are unchanged."""


@dataclass(frozen=True)
class EngineSpec:
    id: str
    name: str
    version: str | None
    settings: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PageSpec:
    id: str
    reference: Path
    outputs: dict[str, Path | None]
    tags: tuple[str, ...]
    split: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class BenchmarkManifest:
    path: Path
    sha256: str
    name: str
    description: str
    synthetic: bool
    engines: tuple[EngineSpec, ...]
    pages: tuple[PageSpec, ...]
    metadata: dict[str, Any]

    @property
    def protected_input_paths(self) -> list[str]:
        paths = {str(self.path)}
        for page in self.pages:
            paths.add(str(page.reference))
            paths.update(str(path) for path in page.outputs.values() if path is not None)
        return sorted(paths)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _json_hash(value: Any) -> str:
    return _sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                              allow_nan=False).encode("utf-8"))


def _object(value: Any, label: str) -> dict:
    if not isinstance(value, dict):
        raise ManifestError(f"{label}: JSON 객체여야 합니다.")
    return value


def _string(value: Any, label: str, *, empty: bool = False) -> str:
    if not isinstance(value, str) or (not empty and not value.strip()) or "\x00" in value:
        raise ManifestError(f"{label}: {'비어 있지 않은 ' if not empty else ''}문자열이어야 합니다.")
    return value


def _identifier(value: Any, label: str) -> str:
    value = _string(value, label)
    if len(value) > 160 or value != value.strip() or any(ord(c) < 32 for c in value):
        raise ManifestError(f"{label}: ID에는 앞뒤 공백·제어 문자를 사용할 수 없으며 160자 이하여야 합니다.")
    return value


def _local_path(root: Path, value: Any, label: str) -> Path:
    raw = _string(value, label)
    path = Path(raw)
    win = PureWindowsPath(raw)
    if path.is_absolute() or win.is_absolute() or win.drive or win.root:
        raise ManifestError(f"{label}: manifest 폴더 안의 상대 경로만 사용할 수 있습니다.")
    # Treat Windows separators consistently when evaluating a manifest elsewhere.
    path = root.joinpath(*PureWindowsPath(raw).parts).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ManifestError(f"{label}: manifest 폴더 밖을 참조할 수 없습니다.") from exc
    return path


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ManifestError(f"JSON 키가 중복되었습니다: {key}")
        result[key] = value
    return result


def _invalid_constant(value: str) -> None:
    raise ManifestError(f"JSON에서 유한하지 않은 수를 사용할 수 없습니다: {value}")


def _read_bytes(path: Path, limit: int) -> bytes:
    with path.open("rb") as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError(f"입력 파일 크기 제한({limit:,}바이트)을 초과했습니다.")
    return data


def load_manifest(path: str | Path) -> BenchmarkManifest:
    """Validate schema v1 without dropping missing transcript/reference paths."""
    path = Path(path).expanduser().resolve()
    try:
        raw = _read_bytes(path, MAX_MANIFEST_BYTES)
        data = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=_no_duplicate_keys,
                          parse_constant=_invalid_constant)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        if isinstance(exc, ManifestError):
            raise
        raise ManifestError(f"평가 manifest를 읽을 수 없습니다: {exc}") from exc
    data = _object(data, "manifest")
    if type(data.get("schema_version")) is not int or data["schema_version"] != 1:
        raise ManifestError("지원하는 manifest schema_version은 1입니다.")
    name = _string(data.get("name"), "name")
    description = _string(data.get("description", ""), "description", empty=True)
    synthetic = data.get("synthetic", False)
    if type(synthetic) is not bool:
        raise ManifestError("synthetic은 true 또는 false여야 합니다.")
    engines_data = data.get("engines")
    if not isinstance(engines_data, list) or not engines_data:
        raise ManifestError("engines는 한 개 이상의 엔진 정의 목록이어야 합니다.")
    engines, engine_ids = [], set()
    for index, item in enumerate(engines_data):
        item = _object(item, f"engines[{index}]")
        engine_id = _identifier(item.get("id"), f"engines[{index}].id")
        if engine_id in engine_ids:
            raise ManifestError(f"엔진 ID가 중복되었습니다: {engine_id}")
        engine_ids.add(engine_id)
        version = item.get("version")
        if version is not None:
            version = _string(version, f"engines[{index}].version")
        engines.append(EngineSpec(
            engine_id, _string(item.get("name", engine_id), f"engines[{index}].name"), version,
            _object(item.get("settings", {}), f"engines[{index}].settings"),
            _object(item.get("metadata", {}), f"engines[{index}].metadata"),
        ))
    pages_data = data.get("pages")
    if not isinstance(pages_data, list) or not pages_data:
        raise ManifestError("pages는 한 개 이상의 쪽 정의 목록이어야 합니다.")
    pages, page_ids = [], set()
    for index, item in enumerate(pages_data):
        item = _object(item, f"pages[{index}]")
        page_id = _identifier(item.get("id"), f"pages[{index}].id")
        if page_id in page_ids:
            raise ManifestError(f"쪽 ID가 중복되었습니다: {page_id}")
        page_ids.add(page_id)
        split = item.get("split")
        if split not in ("dev", "test"):
            raise ManifestError(f"pages[{index}].split은 dev 또는 test여야 합니다.")
        tags = item.get("tags", [])
        if not isinstance(tags, list) or any(not isinstance(tag, str) or not tag.strip() for tag in tags):
            raise ManifestError(f"pages[{index}].tags는 비어 있지 않은 문자열 목록이어야 합니다.")
        if len(set(tags)) != len(tags):
            raise ManifestError(f"pages[{index}].tags가 중복되었습니다.")
        outputs_data = _object(item.get("outputs", {}), f"pages[{index}].outputs")
        unknown = set(outputs_data) - engine_ids
        if unknown:
            raise ManifestError(f"pages[{index}].outputs에 정의되지 않은 엔진 ID가 있습니다: {', '.join(sorted(unknown))}")
        outputs = {key: None if value is None else _local_path(path.parent, value, f"pages[{index}].outputs.{key}")
                   for key, value in outputs_data.items()}
        pages.append(PageSpec(
            page_id, _local_path(path.parent, item.get("reference"), f"pages[{index}].reference"),
            outputs, tuple(tags), split,
            _object(item.get("metadata", {}), f"pages[{index}].metadata"),
        ))
    return BenchmarkManifest(path, _sha256(raw), name, description, synthetic, tuple(engines), tuple(pages),
                             _object(data.get("metadata", {}), "metadata"))


def _read_text(path: Path | None, root: Path) -> tuple[dict, str | None]:
    if path is None:
        return {"path": None, "sha256": None, "status": "missing", "error": "출력 경로가 지정되지 않았습니다."}, None
    info: dict[str, Any] = {"path": str(path.relative_to(root)), "sha256": None}
    try:
        # Recheck confinement if a symlink was added after manifest validation.
        path.resolve().relative_to(root)
        data = _read_bytes(path, MAX_TEXT_BYTES)
        info["sha256"] = _sha256(data)
        info["bytes"] = len(data)
        # One initial UTF-8 BOM is a transport marker; preserve all line endings.
        text = data.decode("utf-8-sig")
        info["input_decoding"] = {"encoding": "utf-8-sig", "initial_bom_removed": data.startswith(b"\xef\xbb\xbf"),
                                  "newlines": "preserved"}
        info["status"] = "ok"
        return info, text
    except FileNotFoundError:
        info.update(status="missing", error="파일을 찾을 수 없습니다.")
    except UnicodeDecodeError as exc:
        info.update(status="invalid_utf8", error=f"UTF-8 텍스트가 아닙니다 (바이트 위치 {exc.start}).")
    except (OSError, ValueError) as exc:
        info.update(status="read_error", error=str(exc))
    return info, None


def _aggregate(results: list[dict]) -> dict:
    keys = ("reference_length", "hypothesis_length", "distance", "substitutions", "deletions", "insertions")
    summary = {key: sum(result[key] for result in results) for key in keys}
    summary["edit_distance"] = summary["distance"]
    length = summary["reference_length"]
    summary["cer"] = summary["distance"] / length if length else None
    summary["cer_percent"] = summary["cer"] * 100 if summary["cer"] is not None else None
    summary["scored_pages"] = sum(result["reference_length"] > 0 for result in results)
    summary["blank_reference_pages"] = sum(result["reference_length"] == 0 for result in results)
    summary["blank_reference_insertions"] = sum(result["insertions"] for result in results if result["reference_length"] == 0)
    return summary


def evaluate_manifest(
    path: str | Path, *, policy: NormalizationPolicy | dict | None = None,
    split: str | None = None, should_stop: Callable[[], bool] | None = None,
    progress: Callable[[int, int], None] | None = None,
    max_cells: int = 10_000_000, max_errors: int = 500,
) -> dict:
    """Compare all declared engines against exactly the selected page cohort.

    Aggregates are micro CER: all completed edit distances divided by all
    completed reference characters. Blank-page insertions contribute errors,
    but an entirely blank reference cohort has no defined CER. Missing truth
    and missing output characters are counted separately, never as zero errors.
    Cancellation is cooperative between exact text comparisons. This function
    reads inputs and returns a report; it never writes files or calls engines.
    """
    def check_stop() -> None:
        if should_stop and should_stop():
            raise BenchmarkCancelled("평가를 중지했습니다. 입력 파일은 변경하지 않았습니다.")

    check_stop()
    manifest = load_manifest(path)
    if split is not None and split not in ("dev", "test"):
        raise ManifestError("평가 split은 dev 또는 test여야 합니다.")
    selected = [page for page in manifest.pages if split is None or page.split == split]
    if not selected:
        raise ManifestError(f"선택한 split에 쪽이 없습니다: {split}")
    # Delegate policy validation/versioning to the metric implementation.
    policy_info = compare_texts("", "", policy, max_cells=max_cells, max_errors=max_errors)
    records, all_errors, fingerprints = [], [], []
    done, total = 0, len(selected) * len(manifest.engines)
    if progress:
        progress(done, total)
    for page in selected:
        check_stop()
        reference_info, reference = _read_text(page.reference, manifest.path.parent)
        if reference is not None:
            reference_info.update(raw_length=len(reference), normalized_length=len(normalize_text(reference, policy)))
        else:
            all_errors.append({"page_id": page.id, "engine_id": None, "code": "reference_" + reference_info["status"],
                               "message": reference_info["error"]})
        fingerprints.append({"role": "reference", "page_id": page.id, **reference_info})
        record = {"id": page.id, "tags": list(page.tags), "split": page.split, "metadata": page.metadata,
                  "reference": reference_info, "engines": {}}
        for engine in manifest.engines:
            check_stop()
            output_info, hypothesis = _read_text(page.outputs.get(engine.id), manifest.path.parent)
            fingerprints.append({"role": "output", "page_id": page.id, "engine_id": engine.id, **output_info})
            output_record = dict(output_info)
            output_record["input_status"] = output_info["status"]
            if hypothesis is None:
                output_record["status"] = "missing_output" if output_info["status"] == "missing" else "output_error"
            elif reference is None:
                output_record.update(status="reference_error", error="정답 파일을 읽을 수 없어 비교하지 못했습니다.")
            else:
                try:
                    output_record["comparison"] = compare_texts(reference, hypothesis, policy,
                                                               max_cells=max_cells, max_errors=max_errors)
                    output_record["status"] = "ok"
                except (ValueError, MemoryError) as exc:
                    output_record.update(status="comparison_error", error=str(exc) or type(exc).__name__)
            if output_record["status"] != "ok":
                all_errors.append({"page_id": page.id, "engine_id": engine.id, "code": output_record["status"],
                                   "message": output_record["error"]})
            record["engines"][engine.id] = output_record
            done += 1
            if progress:
                progress(done, total)
        records.append(record)
    check_stop()
    valid_truth = [record for record in records if record["reference"]["status"] == "ok"]
    engines = {}
    for engine in manifest.engines:
        complete_records = [record for record in records if record["engines"][engine.id]["status"] == "ok"]
        completed = {record["id"] for record in complete_records}
        info = {"name": engine.name, "version": engine.version, "settings": engine.settings, "metadata": engine.metadata,
                "total_pages": len(records), "completed_pages": len(completed), "complete": len(completed) == len(records),
                "missing_output_pages": [r["id"] for r in records if r["engines"][engine.id]["status"] == "missing_output"],
                "error_pages": [error for error in all_errors if error["engine_id"] == engine.id],
                "unscored_page_ids": [r["id"] for r in records if r["id"] not in completed],
                "unknown_reference_pages": len(records) - len(valid_truth)}
        for mode in ("raw", "normalized"):
            info[mode] = _aggregate([r["engines"][engine.id]["comparison"][mode] for r in complete_records])
            length_key = "raw_length" if mode == "raw" else "normalized_length"
            info[mode]["expected_reference_length"] = sum(r["reference"][length_key] for r in valid_truth)
            info[mode]["unscored_reference_length"] = sum(r["reference"][length_key] for r in valid_truth if r["id"] not in completed)
        for field_name, group_name in (("tags", "by_tag"), ("split", "by_split")):
            keys = sorted({tag for record in records for tag in (record[field_name] if field_name == "tags" else [record[field_name]])})
            grouped = {}
            for key in keys:
                cohort = [r for r in records if (key in r["tags"] if field_name == "tags" else r["split"] == key)]
                successful = [r for r in cohort if r["id"] in completed]
                grouped[key] = {"total_pages": len(cohort), "completed_pages": len(successful),
                                **{mode: _aggregate([r["engines"][engine.id]["comparison"][mode] for r in successful])
                                   for mode in ("raw", "normalized")}}
            info[group_name] = grouped
        engines[engine.id] = info
    complete = all(info["complete"] for info in engines.values())
    common_ids = [r["id"] for r in records if all(r["engines"][e.id]["status"] == "ok" for e in manifest.engines)]
    ranking = {"available": False, "reason": "", "raw": [], "normalized": [], "cohort_page_ids": common_ids}
    if not complete:
        ranking["reason"] = "누락·오류가 있어 엔진 순위를 계산하지 않았습니다. 모든 엔진의 동일한 전체 쪽 결과가 필요합니다."
    elif len(engines) < 2:
        ranking["reason"] = "비교할 엔진이 두 개 이상 필요합니다."
    elif any(info[mode]["cer"] is None for info in engines.values() for mode in ("raw", "normalized")):
        ranking["reason"] = "정답 문자가 없는 비교 정책에서는 CER 순위를 정의할 수 없습니다."
    else:
        ranking["available"] = True
        ranking["reason"] = "모든 엔진이 동일한 전체 쪽 집합에서 비교되었습니다. 동점은 같은 순위입니다."
        for mode in ("raw", "normalized"):
            ordered = sorted(engines, key=lambda key: (engines[key][mode]["cer"], key))
            previous, rank = None, 0
            for index, key in enumerate(ordered, 1):
                score = engines[key][mode]["cer"]
                if score != previous:
                    rank = index
                ranking[mode].append({"engine_id": key, "rank": rank, "cer": score, "cer_percent": score * 100})
                previous = score
    protected = manifest.protected_input_paths
    corpus_hash = _json_hash([{key: item.get(key) for key in ("role", "page_id", "engine_id", "path", "sha256", "status")}
                             for item in fingerprints])
    reference_hash = _json_hash([{key: item.get(key) for key in ("page_id", "path", "sha256", "status")}
                                for item in fingerprints if item["role"] == "reference"])
    reproducibility = {
        "benchmark_version": BENCHMARK_VERSION, "application_version": __version__,
        "python_version": platform.python_version(), "unicode_version": unicodedata.unidata_version,
        "manifest_sha256": manifest.sha256, "corpus_sha256": corpus_hash, "reference_corpus_sha256": reference_hash,
        "policy_id": policy_info["policy_id"], "selected_split": split,
        "selected_page_ids": [p.id for p in selected], "max_cells": max_cells, "max_errors": max_errors,
        "input_hashes": fingerprints,
    }
    reproducibility["evaluation_id"] = _json_hash({k: v for k, v in reproducibility.items() if k != "input_hashes"})
    return {
        "kind": "benchmark", "schema_version": 1, "name": manifest.name, "description": manifest.description,
        "synthetic": manifest.synthetic, "metadata": manifest.metadata,
        "scope": "사전에 생성된 UTF-8 전사문 비교. OCR 엔진 실행·표 셀·영역 좌표·독립 읽기 순서 정확도는 측정하지 않습니다.",
        "warning": "합성 전사문은 평가기 동작 확인용이며 실제 OCR 정확도나 ABBYY 대비 성능을 나타내지 않습니다." if manifest.synthetic else
                   "이 보고서는 제공된 정답·전사문과 기록된 조건에 한정됩니다. 엔진 버전·정답 검수·원본과 출력 대응은 별도 확인해야 합니다.",
        "policy": policy_info["policy"], "policy_id": policy_info["policy_id"],
        "aggregation": "micro CER = 모든 완료 쪽의 편집 거리 합 / 정답 문자 수 합. 빈 정답의 삽입도 분자에 포함; 정답 합이 0이면 null.",
        "engines": engines, "pages": records, "errors": all_errors, "ranking": ranking,
        "completeness": {"complete": complete, "total_pages": len(records), "total_engines": len(engines),
                         "expected_comparisons": total, "completed_comparisons": sum(e["completed_pages"] for e in engines.values()),
                         "valid_reference_pages": len(valid_truth), "missing_reference_pages": [r["id"] for r in records if r["reference"]["status"] != "ok"],
                         "common_completed_pages": common_ids},
        "reproducibility": reproducibility, "protected_input_paths": protected,
    }
