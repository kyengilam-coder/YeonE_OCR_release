"""Image-only small-note recovery, kept separate from historical OCR records.

Detection and model reads never write readings/. Adoption requires an explicit
column/caret and a matching snapshot; it only inserts, with a byte-exact backup.
"""
from __future__ import annotations

from contextlib import contextmanager
from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import html
import io
import json
import os
from pathlib import Path
import threading
import time
import unicodedata

from PIL import Image

from . import prompts
from .comparison import _atomic_write
from .pdfio import open_pdf, page_image, parse_pages
from .schema import PageReading, RegionReread
from .state import Work, free_name, write_atomic

SCHEMA_VERSION = 1
KIND = "쌍행소주 한 소열"
HORIZONTAL_KIND = "소주 한 가로줄"
_RUN_LOCK = threading.Lock()


class _StopEvent:
    """Bridge a UI cancellation callback to provider retry waits."""
    def __init__(self, callback):
        self.callback = callback

    def is_set(self):
        return bool(self.callback and self.callback())

    def wait(self, timeout=None):
        end = None if timeout is None else time.monotonic() + timeout
        while not self.is_set():
            if end is not None and time.monotonic() >= end:
                return False
            time.sleep(min(0.05, max(0, end - time.monotonic())) if end is not None else 0.05)
        return True


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _json(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=1).encode("utf-8")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read(path: Path) -> tuple[dict | None, bytes | None]:
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None, None
    value = json.loads(raw.decode("utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError(f"기록 형식이 올바르지 않습니다: {path.name}")
    return value, raw


def _unchanged(path: Path, expected: bytes | None) -> None:
    try:
        actual = path.read_bytes()
    except FileNotFoundError:
        actual = None
    if actual != expected:
        raise ValueError("다른 작업에서 기록이 바뀌었습니다. 다시 불러온 뒤 실행하십시오.")


def _save(path: Path, value: dict, expected: bytes | None) -> bytes:
    raw = _json(value)
    _atomic_write(path, raw, before_replace=lambda: _unchanged(path, expected))
    return raw


@contextmanager
def _work_lock(work_dir):
    """Exclude competing annotation jobs, including other application windows.

    OS byte locks are released on process exit; an interrupted job cannot leave
    a stale lock that prevents resuming successful subcolumn reads.
    """
    root = Path(work_dir) / "annotations"
    root.mkdir(parents=True, exist_ok=True)
    with (root / ".lock").open("a+b") as stream:
        if stream.tell() == 0:
            stream.write(b"0"); stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise ValueError("다른 창에서 쌍행소주 작업 중입니다. 완료 후 다시 실행하십시오.") from exc
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _source(work: Work, dpi=None) -> dict:
    # Never call ensure_source: opening a mismatched work folder must not move
    # its previous readings, corrections, or outputs.
    # Do not let Work's legacy tolerant manifest reader silently ignore a
    # damaged source identity before an edit.
    if (work.dir / "source.json").exists():
        manifest, _ = _read(work.dir / "source.json")
        if not manifest.get("sha256"):
            raise ValueError("원본 지문 기록이 손상되었습니다. 원본과 작업 폴더를 확인하십시오.")
    previous, current, _same = work._source_state(dpi)
    if previous.get("sha256") and previous["sha256"] != current["sha256"]:
        raise ValueError("작업 폴더의 원본과 다른 파일입니다. 이 원본의 작업 폴더를 선택하십시오.")
    return current


def _pages(pages, count):
    if isinstance(pages, (list, tuple, set)):
        if any(type(p) is not int or not 1 <= p <= count for p in pages):
            raise ValueError("쪽 번호가 원본 범위를 벗어났습니다.")
        return sorted(set(pages))
    return parse_pages(pages, count)


def _image_sha(image):
    return _sha(str(image.size).encode("ascii") + image.convert("RGB").tobytes())


def _png(image):
    stream = io.BytesIO()
    image.save(stream, format="PNG")
    return stream.getvalue()


def _record_reading(record, page):
    return PageReading.model_validate(record["merged"]) if record and record.get("merged") else PageReading(pdf_page=page)


def _compact(text):
    # Only layout whitespace. No NFC/NFKC, variant folding, selector removal,
    # or punctuation conversion, even for duplicate detection.
    return "".join(c for c in text if not c.isspace())


def _text_sources(record, candidate=None):
    if not record or not record.get("merged"):
        return []
    reading = PageReading.model_validate(record["merged"])
    sources = []
    for region in reading.regions:
        if candidate and candidate.get("bbox"):
            y0, x0, y1, x1 = candidate["bbox"]
            ry0, rx0, ry1, rx1 = region.bbox
            overlap = max(0, min(y1, ry1) - max(y0, ry0)) * max(0, min(x1, rx1) - max(x0, rx0))
            if overlap < (y1 - y0) * (x1 - x0) * 0.4:
                continue
        for index, paragraph in enumerate(region.paragraphs):
            sources.append(((region.id, index), _compact("".join(c.text for c in paragraph.columns))))
    return sources


def _texts(record, candidate=None):
    return [text for _key, text in _text_sources(record, candidate)]


def _sequence_present(parts, texts):
    """Consume distinct text spans in saved reading order, including repeats."""
    text_index, offset = 0, 0
    for part in parts:
        probe = _compact(part)
        if not probe:
            return False
        while text_index < len(texts):
            found = texts[text_index].find(probe, offset)
            if found >= 0:
                offset = found + len(probe)
                break
            text_index += 1
            offset = 0
        else:
            return False
    return True


def _status(candidate, record):
    if candidate.get("application"):
        return "applied"
    sides = candidate["subcolumns"]
    if not all(side.get("text") for side in sides):
        return "failed" if any(s.get("last_error") for s in sides) else "detected"
    whole = _compact(candidate["combined_text"])
    texts = _texts(record, candidate)
    if whole and any(whole in t for t in texts):
        return "already_present"
    if any(_compact(s["text"]) and any(_compact(s["text"]) in t for t in texts) for s in sides):
        return "partial_present"
    if any(s.get("uncertain") or "〔" in s["text"] for s in sides):
        return "uncertain"
    return "read"


def _refresh(candidate, record):
    candidate["combined_text"] = "".join(s.get("text", "") for s in candidate["subcolumns"])
    candidate["status"] = _status(candidate, record)


def _refresh_candidates(candidates, record):
    """A repeated phrase at one unknown position is not evidence for every ROI."""
    groups = {}
    for candidate in candidates:
        _refresh(candidate, record)
        candidate.pop("presence_warning", None)
        if candidate["status"] not in ("already_present", "partial_present", "applied"):
            continue
        sources = _text_sources(record, candidate)
        probes = Counter(_compact(s.get("text", "")) for s in candidate["subcolumns"])
        probes.setdefault(_compact(candidate["combined_text"]), 1)
        for probe, required in probes.items():
            if probe and any(probe in text for _key, text in sources):
                tokens = set()
                for source_id, text in sources:
                    offset = 0
                    while (found := text.find(probe, offset)) >= 0:
                        tokens.add((source_id, found))
                        offset = found + len(probe)
                groups.setdefault(probe, []).append((candidate, tokens, required))
    for group in groups.values():
        pending = set(range(len(group)))
        while pending:
            component = {pending.pop()}
            available = set(group[next(iter(component))][1])
            while True:
                neighbors = {i for i in pending if available & group[i][1]}
                if not neighbors:
                    break
                component.update(neighbors)
                pending.difference_update(neighbors)
                for i in neighbors:
                    available.update(group[i][1])
            owners = {}

            def assign(index, seen):
                for token in sorted(group[index][1]):
                    if token in seen:
                        continue
                    seen.add(token)
                    if token not in owners or assign(owners[token], seen):
                        owners[token] = index
                        return True
                return False

            matched = sum(assign(i, set()) for i in sorted(component) for _ in range(group[i][2]))
            if matched < sum(group[i][2] for i in component):
                for i in component:
                    candidate = group[i][0]
                    if candidate.get("application"):
                        continue
                    candidate["status"] = "position_unverified"
                    candidate["presence_warning"] = "같은 소주가 여러 위치에 있으나 기존 판독의 위치를 구별할 수 없습니다."


def _check_snapshot(work, page_data, record_raw):
    if page_data.get("record_sha256") != (_sha(record_raw) if record_raw is not None else None):
        raise ValueError("검출 뒤 판독·교정 기록이 바뀌었습니다. 다시 검출하여 현재 기록과 대조하십시오.")


def load_bundle(work_dir) -> dict:
    root = Path(work_dir)
    result = {"schema_version": SCHEMA_VERSION, "work_dir": str(root.resolve()), "pages": []}
    for path in sorted((root / "annotations").glob("p[0-9]*.json")):
        data, _raw = _read(path)
        if data.get("schema_version") != SCHEMA_VERSION:
            raise ValueError(f"지원하지 않는 소주 기록 형식: {path.name}")
        record, record_raw = _read(root / "readings" / f"p{data['pdf_page']:03d}.json")
        data["record"] = record
        data["stale"] = data.get("record_sha256") != (_sha(record_raw) if record_raw is not None else None)
        # Reading is the authoritative commit if a process stopped between the
        # two writes of an explicit adoption. Never insert it a second time.
        applications = {a["candidate_id"]: a for a in (record or {}).get("small_annotation_history", [])}
        for candidate in data["candidates"]:
            if candidate["id"] in applications:
                candidate["application"] = applications[candidate["id"]]
        _refresh_candidates(data["candidates"], record)
        result["pages"].append(data)
    return result


def scan_work(pdf, work_dir, pages=None, dpi=300, should_stop=None, horizontal=None) -> dict:
    from .small_annotation_detection import detect_small_annotations
    work = Work(str(pdf), str(work_dir), initialize=False)
    source = _source(work, dpi)
    with _work_lock(work.dir), open_pdf(str(pdf)) as doc:
        for page in _pages(pages, len(doc)):
            if should_stop and should_stop():
                break
            record, record_raw = _read(work.record_path(page))
            reading = _record_reading(record, page)
            image, image_source = page_image(doc, page, dpi=dpi)
            image_hash = _image_sha(image)
            is_horizontal = horizontal
            if is_horizontal is None:
                is_horizontal = ((record or {}).get("mode") == "horizontal_zh"
                                 or bool(reading.regions) and all(
                                     "가로" in r.reading_direction for r in reading.regions
                                     if r.kind in ("본문", "소주", "설명문"))
                                 and any(r.kind in ("본문", "소주", "설명문") for r in reading.regions))
            detected = detect_small_annotations(image, reading, horizontal=bool(is_horizontal))
            path = work.dir / "annotations" / f"p{page:03d}.json"
            old, old_raw = _read(path)
            reusable = bool(old and old.get("source_sha256") == source["sha256"]
                            and old.get("image_sha256") == image_hash
                            and old.get("detector") == detected["detector"])
            previous_candidates = {c["id"]: c for c in old.get("candidates", [])} if reusable else {}
            data = {**detected, "pdf_page": page, "source_sha256": source["sha256"],
                    "source_path": source["path"], "image_sha256": image_hash,
                    "image_source": image_source, "dpi": dpi,
                    "record_sha256": _sha(record_raw) if record_raw is not None else None,
                    "scanned_at": _now()}
            crop_dir = path.parent / "crops"
            crop_dir.mkdir(exist_ok=True)
            for candidate in data["candidates"]:
                previous = previous_candidates.get(candidate["id"], {})
                if previous.get("bbox_px") == candidate["bbox_px"] and previous.get("subcolumns"):
                    old_sides = {s["side"]: s for s in previous["subcolumns"]}
                    for side in candidate["subcolumns"]:
                        saved = old_sides.get(side["side"], {})
                        if side["bbox_px"] == saved.get("bbox_px"):
                            for key in ("runs", "text", "uncertain", "last_error", "active_request"):
                                if key in saved:
                                    side[key] = deepcopy(saved[key])
                    if previous.get("application"):
                        candidate["application"] = previous["application"]
                # Never trust caller-supplied IDs as filenames.
                crop_name = f"p{page:03d}-{_sha(_json([image_hash, candidate['bbox_px']]))[:20]}.png"
                crop_path = crop_dir / crop_name
                if not crop_path.exists():
                    write_atomic(crop_path, _png(image.crop(candidate["bbox_px"])))
                candidate["crop_path"] = str(crop_path.resolve())
            _refresh_candidates(data["candidates"], record)
            _unchanged(work.record_path(page), record_raw)
            if old_raw is not None and old != data:
                archive = path.parent / "history"
                archive.mkdir(exist_ok=True)
                write_atomic(free_name(archive / path.name), old_raw)
            _save(path, data, old_raw)
    return load_bundle(work.dir)


def reread_work(pdf, work_dir, pages=None, candidate_ids=None, provider="gemini",
                model=None, api_key=None, max_calls=12, should_stop=None) -> dict:
    """Read each small subcolumn independently. Save every response immediately.

    Successful requests with identical pixels/settings are reused. Failures and
    cancellation retain earlier raw results and leave all OCR records untouched.
    The caller must serialize other model jobs (the GUI opens a modal tool).
    """
    from .providers import default_model, provider_name
    from .readers import create_reader, ReaderError
    from . import reader_gemini
    from .reader_gemini import CALL_CAP, set_call_cap
    if type(max_calls) is not int or max_calls < 1:
        raise ValueError("소주 재판독 호출 상한은 1 이상의 정수여야 합니다.")
    if not _RUN_LOCK.acquire(blocking=False):
        raise ValueError("다른 소주 재판독이 실행 중입니다.")
    previous_cancel = reader_gemini._CANCEL_EVENT
    reader_gemini.set_cancel_event(_StopEvent(should_stop))
    try:
        work = Work(str(pdf), str(work_dir), initialize=False)
        source = _source(work)
        provider = provider_name(provider)
        model = model or default_model(provider)
        wanted = set(candidate_ids) if candidate_ids is not None else None
        readers = {}
        attempted = 0
        stopped = False
        set_call_cap(max_calls)
        with _work_lock(work.dir), open_pdf(str(pdf)) as doc:
            selected_pages = _pages(pages, len(doc))
            saved = {p["pdf_page"]: p for p in load_bundle(work.dir)["pages"] if p["pdf_page"] in selected_pages}
            if not saved:
                raise ValueError("선택한 쪽의 검출 기록이 없습니다. 먼저 쌍행소주를 검출하십시오.")
            known_ids = {c["id"] for p in saved.values() for c in p["candidates"]}
            if wanted is not None and wanted - known_ids:
                raise ValueError("선택한 후보 ID가 없습니다. 다시 불러온 뒤 선택하십시오.")
            for page in selected_pages:
                path = work.dir / "annotations" / f"p{page:03d}.json"
                data, raw = _read(path)
                if data is None:
                    continue
                if data["source_sha256"] != source["sha256"]:
                    raise ValueError("소주 기록의 원본이 달라졌습니다. 다시 검출하십시오.")
                record, record_raw = _read(work.record_path(page))
                _check_snapshot(work, data, record_raw)
                image, _ = page_image(doc, page, dpi=data["dpi"])
                if _image_sha(image) != data["image_sha256"]:
                    raise ValueError("검출 당시와 쪽 이미지가 달라졌습니다. 다시 검출하십시오.")
                for candidate in data["candidates"]:
                    if wanted is not None and candidate["id"] not in wanted:
                        continue
                    _refresh_candidates(data["candidates"], record)
                    if candidate["status"] in ("applied", "already_present"):
                        continue
                    horizontal = candidate.get("direction") == "horizontal"
                    kind = HORIZONTAL_KIND if horizontal else KIND
                    system = prompts.small_annotation_system_prompt(horizontal=horizontal)
                    requests = {}
                    for side in candidate["subcolumns"]:
                        request = {"provider": provider, "model": model,
                                   "image_sha256": data["image_sha256"], "bbox_px": side["bbox_px"],
                                   "prompt_sha256": _sha((system + prompts.region_prompt(
                                       kind, candidate["id"] + "-" + side["side"], page)).encode("utf-8"))}
                        request_id = _sha(_json(request))
                        requests[side["side"]] = (request_id, request)
                        cached = next((r for r in reversed(side.get("runs", []))
                                       if r.get("request_id") == request_id and r.get("ok")), None)
                        if cached:
                            side.update(text=cached["text"], uncertain=cached["uncertain"],
                                        active_request=request_id)
                            side.pop("last_error", None)
                        else:
                            # A new provider/model/prompt must never leave a
                            # half-old, half-new pair available for insertion.
                            for key in ("text", "uncertain", "active_request", "last_error"):
                                side.pop(key, None)
                    _refresh_candidates(data["candidates"], record)
                    raw = _save(path, data, raw)
                    for side in candidate["subcolumns"]:
                        request_id, request = requests[side["side"]]
                        if side.get("active_request") == request_id:
                            continue
                        if (should_stop and should_stop()) or max(CALL_CAP.used, attempted) >= max_calls:
                            stopped = True
                            break
                        _unchanged(work.record_path(page), record_raw)
                        if horizontal not in readers:
                            readers[horizontal] = create_reader(provider, model, api_key=api_key, use_cache=False, system=system)
                        reader = readers[horizontal]
                        before_calls = CALL_CAP.used
                        reader.usage_begin()
                        run = {"request_id": request_id, "request": request, "at": _now(), "ok": False}
                        try:
                            attempted += 1
                            response = reader.reread_region(image.crop(side["bbox_px"]), kind,
                                                           candidate["id"] + "-" + side["side"], page)
                            response = RegionReread.model_validate(response)
                            # A single subcolumn should produce one column. Retain
                            # anomalous raw replies but never offer them for insertion.
                            run["response"] = response.model_dump()
                            run["ok"] = len(response.columns) == 1 and bool(response.columns[0].text.strip())
                            if run["ok"]:
                                run["text"] = response.columns[0].text
                                run["uncertain"] = [u.model_dump() for u in response.uncertain]
                                side.update(text=run["text"], uncertain=run["uncertain"], active_request=request_id)
                                side.pop("last_error", None)
                            else:
                                side["last_error"] = "한 소열·행에 해당하는 판독을 얻지 못했습니다. 원본 확인이 필요합니다."
                        except ReaderError as exc:
                            run["error"] = str(exc)
                            side["last_error"] = str(exc)
                            stopped = bool(exc.fatal or getattr(exc, "kind", None) == "cancelled")
                        finally:
                            run["usage"] = reader.usage_end()
                            run["attempted_calls"] = max(1, CALL_CAP.used - before_calls)
                        side.setdefault("runs", []).append(run)
                        _refresh_candidates(data["candidates"], record)
                        raw = _save(path, data, raw)
                        if stopped:
                            break
                    _refresh_candidates(data["candidates"], record)
                    raw = _save(path, data, raw)
                    if stopped:
                        break
                if stopped:
                    break
        result = load_bundle(work.dir)
        result["run"] = {"attempted_calls": max(CALL_CAP.used, attempted), "max_calls": max_calls,
                         "stopped": stopped}
        return result
    finally:
        reader_gemini.set_cancel_event(previous_cancel)
        _RUN_LOCK.release()


def apply_candidate(pdf, work_dir, page, candidate_id, region_id, paragraph_index,
                    column_index, offset, expected_text, actor="human", side="both") -> dict:
    """Insert a complete reviewed pair at a user-selected caret; never replace.

    Offsets are Python Unicode code-point offsets, not UTF-16 indices. A caret
    inside a variation/combining sequence is rejected. Full record bytes are
    checked again immediately before replacement, including Windows retries.
    """
    if actor != "human":
        raise ValueError("소주 삽입 위치는 사람이 지정해야 합니다.")
    work = Work(str(pdf), str(work_dir), initialize=False)
    source = _source(work)
    with _work_lock(work.dir), Work._lock:
        path = work.dir / "annotations" / f"p{page:03d}.json"
        data, raw = _read(path)
        if data is None or data["source_sha256"] != source["sha256"]:
            raise ValueError("이 원본의 소주 검출 기록이 없습니다.")
        record, record_raw = _read(work.record_path(page))
        if not record or not record.get("merged"):
            raise ValueError("본문 판독 기록을 먼저 만들어야 삽입 위치를 지정할 수 있습니다.")
        _check_snapshot(work, data, record_raw)
        candidates = [c for c in data["candidates"] if c["id"] == candidate_id]
        if len(candidates) != 1:
            raise ValueError("소주 후보를 유일하게 찾을 수 없습니다.")
        candidate = candidates[0]
        if any(a["candidate_id"] == candidate_id for a in record.get("small_annotation_history", [])):
            raise ValueError("이미 반영한 소주입니다.")
        _refresh_candidates(data["candidates"], record)
        if candidate["status"] == "position_unverified":
            raise ValueError("반복된 소주의 기존 판독 위치를 확인할 수 없습니다. 원본과 대조하여 본문에서 교정하십시오.")
        if side not in {"both", *(s["side"] for s in candidate["subcolumns"])}:
            raise ValueError("삽입할 소열·행을 올바르게 선택하십시오.")
        selected_sides = candidate["subcolumns"]
        if candidate["status"] == "partial_present" and side != "both":
            selected_sides = [s for s in candidate["subcolumns"] if s["side"] == side]
            other_sides = [s for s in candidate["subcolumns"] if s["side"] != side]
            texts = _texts(record, candidate)
            if (len(selected_sides) != 1 or not other_sides
                    or not selected_sides[0].get("text")
                    or any(_compact(selected_sides[0]["text"]) in t for t in texts)
                    or not _sequence_present([s.get("text", "") for s in other_sides], texts)):
                raise ValueError("선택한 소열·행이 이미 있거나 나머지 모든 소열·행의 존재를 확인할 수 없습니다.")
        elif candidate["status"] not in ("read", "uncertain") or side != "both":
            raise ValueError("모든 소열·행 판독과 중복 여부를 확인하십시오. 일부가 이미 있으면 빠진 한 소열·행을 선택하십시오.")
        regions = [r for r in record["merged"]["regions"] if r["id"] == region_id]
        if len(regions) != 1 or regions[0].get("kind") not in ("본문", "소주", "설명문"):
            raise ValueError("삽입 대상 문자 영역을 유일하게 찾을 수 없습니다.")
        if any(type(i) is not int or i < 0 for i in (paragraph_index, column_index, offset)):
            raise ValueError("문단·열·위치는 0 이상의 정수여야 합니다.")
        try:
            column = regions[0]["paragraphs"][paragraph_index]["columns"][column_index]
        except (KeyError, IndexError) as exc:
            raise ValueError("선택한 문단·열이 없습니다.") from exc
        text = column["text"]
        if text != expected_text or offset > len(text):
            raise ValueError("선택한 열의 글이나 삽입 위치가 달라졌습니다. 다시 선택하십시오.")
        if offset < len(text) and (unicodedata.category(text[offset]).startswith("M")
                                  or 0xE0100 <= ord(text[offset]) <= 0xE01EF):
            raise ValueError("글자와 결합 문자·이체자 선택자 사이에는 삽입할 수 없습니다.")
        inserted = "".join(s["text"] for s in selected_sides)
        before_hash = _sha(record_raw)
        application = {"candidate_id": candidate_id, "at": _now(), "actor": actor,
                       "source_sha256": data["source_sha256"], "image_sha256": data["image_sha256"],
                       "bbox_px": candidate["bbox_px"],
                       "region_id": region_id, "paragraph_index": paragraph_index,
                       "column_index": column_index, "offset": offset, "inserted_text": inserted,
                       "selected_side": side,
                       "before_record_sha256": before_hash,
                       "before_column_text": text,
                       "requests": [s.get("active_request") for s in selected_sides]}
        column["text"] = text[:offset] + inserted + text[offset:]
        record.setdefault("small_annotation_history", []).append(application)
        for selected in selected_sides:
            for notice in selected.get("uncertain", []):
                notice = deepcopy(notice)
                notice.update(region_id=region_id, col_from_right=column.get("col_from_right", 0))
                if notice not in record["merged"].setdefault("uncertain", []):
                    record["merged"]["uncertain"].append(notice)
        _unchanged(work.record_path(page), record_raw)
        backup_dir = work.readings_dir / "prev"
        backup_dir.mkdir(exist_ok=True)
        backup_path = free_name(backup_dir / f"p{page:03d}.json")
        write_atomic(backup_path, record_raw)
        new_raw = _save(work.record_path(page), record, record_raw)
        candidate["application"] = application
        data["record_sha256"] = _sha(new_raw)
        _refresh_candidates(data["candidates"], record)
        _save(path, data, raw)
        return {"applied": True, "backup_path": str(backup_path.resolve()),
                "inserted_text": inserted, "application": application}


def write_report(work_dir) -> Path:
    """A portable local review report; machine proposals are never certified."""
    bundle = load_bundle(work_dir)
    sections = []
    for page in bundle["pages"]:
        sections.append(f"<h2>PDF {page['pdf_page']}쪽</h2>")
        for c in page["candidates"]:
            sides = "".join(f"<p>{html.escape({'right': '오른쪽', 'left': '왼쪽'}.get(s['side'], s['side'].replace('line-', '행 ')))}: "
                            f"<code>{html.escape(s.get('text', '미판독'))}</code></p>" for s in c["subcolumns"])
            sections.append(f"<article><h3>{html.escape(c['id'])} · {html.escape(c['status'])}</h3>"
                            f"<img src='{html.escape(Path(c['crop_path']).as_uri(), quote=True)}' alt='원본 소주 후보'>"
                            f"{sides}</article>")
    content = ("<!doctype html><html lang='ko'><meta charset='utf-8'><title>쌍행소주 확인</title>"
               "<style>body{max-width:1100px;margin:2em auto;font:18px sans-serif}"
               "article{border-top:1px solid #aaa;padding:1em}img{max-height:600px;max-width:100%}"
               "code{white-space:pre-wrap}</style><h1>쌍행소주 후보·재판독</h1>"
               "<p>기계 판독 제안입니다. 본문은 바뀌지 않습니다. 세로 소열은 오른쪽→왼쪽, 가로 행은 위→아래 순서를 확인하고 "
               "앱의 소주 검수 도구에서 삽입 위치를 지정하십시오. 검출되지 않은 소주도 있을 수 있습니다.</p>"
               + "".join(sections) + "</html>")
    path = Path(work_dir) / "annotations" / "review.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_atomic(path, content)
    return path


def command(args):
    from .cli import CANCEL
    work_dir = args.work or str(Work(args.pdf, initialize=False).dir)
    if not args.reread:
        direction = getattr(args, "direction", "auto")
        result = scan_work(args.pdf, work_dir, args.pages, args.dpi, CANCEL.is_set,
                           horizontal={"auto": None, "vertical": False, "horizontal": True}[direction])
    else:
        result = reread_work(args.pdf, work_dir, args.pages, args.candidate,
                             args.reader, args.model, max_calls=args.max_calls, should_stop=CANCEL.is_set)
    path = write_report(work_dir)
    print(f"쌍행소주 후보 {sum(len(p['candidates']) for p in result['pages'])}곳 · {path}")
    if result.get("run"):
        print(f"이번 요청 {result['run']['attempted_calls']}회 / 상한 {args.max_calls}회")
    return 130 if CANCEL.is_set() else 0
