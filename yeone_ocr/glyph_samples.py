"""글자 이미지 조각 모으기: 로컬 엔진 미세조정에 쓸 (줄 이미지, 확정 글) 쌍을 쌓는다.

엔진 혼동 장부(confusion_memory)가 "어느 글자를 틀렸나"를 센다면, 여기서는 그 자리의 **원본 이미지**를 함께 남긴다.
나중에 PaddleOCR 인식 모델 등을 추가 학습시키려면 이 자료가 있어야 하며, 지금 모아 두지 않으면 그때 가서는 없다.

무엇을 자르나
- 엔진이 준 낱말·줄 상자(raw words 의 bbox)를 그대로 자른다. 글자 하나하나의 위치는 추측하지 않는다.
  인식 모델의 학습 자료 형식(줄 이미지 + 그 줄의 글)이 바로 이것이다.
- corrected: 엔진이 읽은 글과 확정 글이 다른 줄. 정답은 확정 글, 등급은 그 줄을 고친 주체 가운데 가장 약한 것.
- verified: 사람이 검수 완료로 표시한 쪽에서 엔진이 읽은 그대로 확정된 줄(쪽마다 몇 개만). 맞게 읽던 것을
  잊지 않게 하는 데 쓴다.

넣지 않는 것
- 간체·번체 변환, 전각·반각, 부호를 넣고 빼고 바꾼 줄: 편집 단계의 교정이라 이미지가 보여 주는 글이 아니다.
- 줄 경계에 글이 끼어든 자리, 상자가 쪽 전체인 판독(GLM-OCR 등 좌표 없는 엔진), 원본이 바뀐 문서.

어디에 두나: %LOCALAPPDATA%\\YeonE_OCR\\glyph_samples\\<문서 식별자>\\ (원본 문서의 일부가 그대로 들어 있다 —
이 PC 밖으로 보내지 않는다). export() 가 PaddleOCR 인식 학습 형식(rec_gt.txt)으로 내보낸다.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import threading
import time
import unicodedata

FORMAT = "yeone-ocr-glyph-samples"
VERSION = 1
ENV_PATH = "YEONE_OCR_GLYPH_SAMPLES_PATH"
KINDS = ("corrected", "verified")
GRADE_ORDER = ("ai_auto", "ai_confirmed", "human")      # 약한 것부터
MAX_VERIFIED_PER_PAGE = 4
MAX_SAMPLES_PER_DOCUMENT = 20_000
MAX_CROP_PIXELS = 4_000_000
MIN_CROP_SIDE = 4
PADDING = 4

_LOCK = threading.Lock()


def samples_root() -> Path:
    override = os.environ.get(ENV_PATH, "").strip()
    if override:
        return Path(override).expanduser()
    from .runtime import user_data_dir
    return user_data_dir() / "glyph_samples"


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def _write(path: Path, data) -> None:
    from .state import write_atomic
    path.parent.mkdir(parents=True, exist_ok=True)
    write_atomic(path, data)


# --------------------------------------------------------------------- 설정


def is_enabled(root=None) -> bool:
    root = Path(root) if root is not None else samples_root()
    try:
        value = json.loads((root / "settings.json").read_text(encoding="utf-8")).get("enabled", True)
    except (OSError, ValueError, AttributeError):
        return True
    return value if type(value) is bool else True


def set_enabled(enabled: bool, root=None) -> None:
    if type(enabled) is not bool:
        raise ValueError("사용 여부는 참 또는 거짓이어야 합니다.")
    root = Path(root) if root is not None else samples_root()
    _write(root / "settings.json", json.dumps({"enabled": enabled}) + "\n")


# ------------------------------------------------------------- 쪽 기록 → 조각 목록


def _map(opcodes, position: int):
    """기준 판독의 경계 위치를 교정문의 위치로. 경계에 글이 끼어들었거나 고친 자리 한가운데면 None."""
    found = None
    for op, i1, i2, j1, j2 in opcodes:
        if op == "insert" and i1 == position:
            return None
        if op != "equal" and i1 < position < i2:
            return None
        if found is None:
            if op == "equal" and i1 <= position <= i2:
                found = j1 + position - i1
            elif op != "equal" and position == i1:
                found = j1
            elif op != "equal" and position == i2:
                found = j2
    return found


def _editorial(base: str, final: str, opcodes, start: int, end: int) -> bool:
    """이 줄의 변경 가운데 이미지가 아닌 편집 규칙에서 온 것(간번체·전각반각·부호 넣고 빼기)이 있는가."""
    from .confusion_memory import _noise
    for op, i1, i2, j1, j2 in opcodes:
        if op == "equal" or not start <= i1 <= i2 <= end or (op == "insert" and not start < i1 < end):
            continue
        before, after = base[i1:i2], final[j1:j2]
        if op == "replace" and len(before) == len(after):
            if any(a != b and _noise(a, b) for a, b in zip(before, after)):
                return True
        elif op == "replace":
            if _noise(before, after):
                return True
        elif any(unicodedata.category(c)[0] in ("P", "S") for c in before + after):
            return True
    return False


def _usable_box(box, width: int, height: int):
    if not isinstance(box, list) or len(box) != 4:
        return None
    left, top, right, bottom = (int(round(v)) for v in box)
    if right - left < MIN_CROP_SIDE or bottom - top < MIN_CROP_SIDE:
        return None
    if right - left >= width * .9 and bottom - top >= height * .9:
        return None                          # 쪽 전체 상자: 줄 위치를 모른다
    left, top = max(0, left - PADDING), max(0, top - PADDING)
    right, bottom = min(width, right + PADDING), min(height, bottom + PADDING)
    if (right - left) * (bottom - top) > MAX_CROP_PIXELS:
        return None
    return [left, top, right, bottom]


def _sample_id(document_id: str, number: int, run_id: str, start: int, end: int, label: str) -> str:
    payload = json.dumps([document_id, number, run_id, start, end, label], ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def page_samples(page: dict, document_id: str, number: int) -> list[dict]:
    """한 쪽에서 남길 조각의 목록(이미지는 아직 자르지 않음)."""
    from .confusion_memory import aligned_page, _countable
    aligned = aligned_page(page)
    if aligned is None:
        return []
    baseline, raw = aligned["baseline"], aligned["raw"]
    base, final, labels, opcodes = aligned["base"], aligned["final"], aligned["labels"], aligned["opcodes"]
    width, height = raw.get("image_width"), raw.get("image_height")
    direction = (baseline.get("request") or {}).get("direction") or page.get("direction") or "horizontal"
    samples, verified, position = [], 0, 0
    for word in raw.get("words", []):
        text = word.get("text", "")
        start = base.find(text, position) if text else -1
        if start < 0:
            continue
        end = position = start + len(text)
        box = _usable_box(word.get("bbox"), width, height)
        if box is None or not any(_countable(c) for c in text):
            continue
        f_start, f_end = _map(opcodes, start), _map(opcodes, end)
        if f_start is None or f_end is None or f_end < f_start:
            continue
        label = final[f_start:f_end]
        if not label.strip() or any(c in label for c in "\t\r\n") or any(c in text for c in "\t\r\n"):
            continue
        if label == text:
            if not aligned["upgraded"] or verified >= MAX_VERIFIED_PER_PAGE or sum(map(_countable, text)) < 2:
                continue
            kind, grade = "verified", "human"
            verified += 1
        else:
            if _editorial(base, final, opcodes, start, end):
                continue
            grades = {g for g in labels[f_start:f_end] if g != "engine"}
            if not grades:
                continue                     # 지우기만 했다: 누가 지웠는지 글자에 남지 않는다
            kind = "corrected"
            grade = next(g for g in GRADE_ORDER if g in grades)
            if grade == "ai_auto" and aligned["upgraded"]:
                grade = "ai_confirmed"
        samples.append({"id": _sample_id(document_id, number, baseline["id"], start, end, label),
                        "page": number, "run_id": baseline["id"], "engine": baseline["engine"],
                        "engine_version": raw.get("engine_version", ""), "direction": direction,
                        "dpi": (baseline.get("request") or {}).get("dpi"),
                        "bbox": box, "read": text, "label": label, "kind": kind, "grade": grade})
    return samples


# --------------------------------------------------------------------- 저장소


def _file(sample: dict) -> str:
    """목록에 적힌 조각 파일 이름(경로 부분은 버림: 문서 폴더 밖을 가리키지 않게)."""
    return Path(str(sample.get("file") or f"{sample.get('id', '')}.png")).name


def _manifest_path(root: Path, document_id: str) -> Path:
    return root / document_id / "manifest.json"


def _read_manifest(path: Path):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        return False
    if (not isinstance(data, dict) or data.get("format") != FORMAT or data.get("version") != VERSION
            or not isinstance(data.get("samples"), list)):
        return False
    return data


def collect_project(project, root=None, *, should_stop=None) -> dict:
    """프로젝트의 현재 기록에 맞춰 그 문서의 조각을 더하고, 더는 맞지 않는 조각(되돌린 교정 등)은 지운다.

    돌려주는 값: {"added", "removed", "kept", "pages_rendered", "warning"}."""
    root = Path(root) if root is not None else samples_root()
    outcome = {"added": 0, "removed": 0, "kept": 0, "pages_rendered": 0, "warning": ""}
    if not is_enabled(root):
        return outcome
    document_id = getattr(project, "_id", None)
    if not isinstance(document_id, str) or not document_id or "/" in document_id or "\\" in document_id:
        outcome["warning"] = "프로젝트 식별자가 없어 글자 조각을 모으지 않았습니다."
        return outcome
    wanted: dict[int, list[dict]] = {}
    for number in project.completed_pages:
        samples = page_samples(project.page(number), document_id, number)
        if samples:
            wanted[number] = samples
    with _LOCK:
        path = _manifest_path(root, document_id)
        manifest = _read_manifest(path)
        if manifest is False:
            outcome["warning"] = "글자 조각 목록을 읽지 못해 이 문서의 조각은 이번에 모으지 않았습니다."
            return outcome
        if manifest is None and not wanted:
            return outcome
        existing = {s["id"]: s for s in (manifest or {}).get("samples", []) if isinstance(s, dict) and "id" in s}
        desired = {s["id"]: s for samples in wanted.values() for s in samples}
        folder = path.parent
        kept = []
        for identifier, old in existing.items():
            if identifier in desired and (folder / _file(old)).is_file():
                updated = {**old, **{k: desired[identifier][k] for k in ("grade", "kind")}}
                kept.append(updated)
            else:
                try:
                    if _file(old) != ".png":
                        (folder / _file(old)).unlink(missing_ok=True)
                except OSError:
                    pass
                if identifier not in desired:
                    outcome["removed"] += 1
        have = {s["id"] for s in kept}
        missing = [s for s in desired.values() if s["id"] not in have]
        room = max(0, MAX_SAMPLES_PER_DOCUMENT - len(kept))
        missing = missing[:room]
        outcome["kept"] = len(kept)
        added = []
        if missing:
            try:
                project.validate_source()
            except Exception:
                outcome["warning"] = "원본이 바뀌었거나 열 수 없어 새 글자 조각은 모으지 않았습니다."
                missing = []
        by_page: dict[int, list[dict]] = {}
        for sample in missing:
            by_page.setdefault(sample["page"], []).append(sample)
        from ab_ocr import ocr
        for number, samples in sorted(by_page.items()):
            if should_stop is not None and should_stop():
                break
            raw = next(r for r in project.page(number)["runs"] if r["id"] == samples[0]["run_id"])["raw"]
            try:
                image = ocr.render_page(project.source_path, number, dpi=samples[0]["dpi"] or 300, should_stop=should_stop)
            except Exception:
                continue
            try:
                if image.size != (raw["image_width"], raw["image_height"]):
                    continue                 # 인식 당시와 다른 좌표계: 자르면 다른 자리가 나온다
                outcome["pages_rendered"] += 1
                for sample in samples:
                    buffer = io.BytesIO()
                    with image.crop(tuple(sample["bbox"])) as crop, crop.convert("L") as gray:
                        gray.save(buffer, format="PNG", optimize=True)
                    data = buffer.getvalue()
                    name = sample["id"] + ".png"
                    _write(folder / name, data)
                    added.append({**sample, "file": name, "sha256": hashlib.sha256(data).hexdigest(), "at": _now()})
            finally:
                image.close()
        outcome["added"] = len(added)
        samples = sorted(kept + added, key=lambda s: (s["page"], s["bbox"][1], s["bbox"][0], s["id"]))
        if not samples:
            if manifest is not None:
                shutil.rmtree(folder, ignore_errors=True)
            return outcome
        _write(path, json.dumps({"format": FORMAT, "version": VERSION, "document_id": document_id,
                                 "source": Path(str(project.source_path)).name,
                                 "source_sha256": project.source_sha256, "updated_at": _now(),
                                 "samples": samples}, ensure_ascii=False, indent=1) + "\n")
    return outcome


def collect_quietly(project, log=None, root=None, *, should_stop=None) -> dict | None:
    """저장·실행 흐름을 막지 않는 수집: 실패는 한 줄 알리고 넘어간다."""
    try:
        outcome = collect_project(project, root, should_stop=should_stop)
    except Exception as exc:                 # 보조 기능: 교정본 저장·문서 출력을 실패시키지 않는다
        if log:
            log(f"  글자 조각을 모으지 못했습니다(교정 결과는 그대로): {exc}")
        return None
    if log:
        if outcome.get("warning"):
            log("  " + outcome["warning"])
        if outcome["added"] or outcome["removed"]:
            log(f"  글자 조각: 새로 {outcome['added']}개 · 지움 {outcome['removed']}개 (미세조정용, 이 PC에만 보관)")
    return outcome


def _documents(root: Path):
    if not root.is_dir():
        return
    for folder in sorted(p for p in root.iterdir() if p.is_dir()):
        manifest = _read_manifest(folder / "manifest.json")
        if manifest:
            yield folder, manifest


def stats(root=None) -> dict:
    root = Path(root) if root is not None else samples_root()
    result = {"documents": 0, "samples": 0, "bytes": 0, "by_engine": {}, "by_grade": {}, "by_kind": {},
              "enabled": is_enabled(root), "root": str(root)}
    for folder, manifest in _documents(root):
        result["documents"] += 1
        for sample in manifest["samples"]:
            result["samples"] += 1
            for key, field in (("by_engine", "engine"), ("by_grade", "grade"), ("by_kind", "kind")):
                result[key][sample.get(field, "")] = result[key].get(sample.get(field, ""), 0) + 1
            try:
                result["bytes"] += (folder / _file(sample)).stat().st_size
            except (OSError, KeyError):
                pass
    return result


def export(destination, root=None, *, min_grade="ai_confirmed", engines=None, kinds=KINDS,
           rotate_vertical=False) -> dict:
    """PaddleOCR 인식 학습 형식으로 내보낸다: images/*.png, rec_gt.txt('images/이름.png<탭>정답'), samples.jsonl.

    rotate_vertical: 세로쓰기 줄 조각을 90° 돌린다(세로 줄을 돌려 읽는 인식 모델의 추론 전처리와 맞출 때)."""
    if min_grade not in GRADE_ORDER:
        raise ValueError("등급은 ai_auto · ai_confirmed · human 가운데 하나여야 합니다.")
    if any(kind not in KINDS for kind in kinds):
        raise ValueError("조각 종류는 corrected · verified 가운데서 고릅니다.")
    root = Path(root) if root is not None else samples_root()
    destination = Path(destination).expanduser()
    if destination.exists() and any(destination.iterdir()):
        raise ValueError("빈 폴더로 내보내야 합니다(기존 파일을 덮어쓰지 않음).")
    floor = GRADE_ORDER.index(min_grade)
    images = destination / "images"
    images.mkdir(parents=True, exist_ok=True)
    lines, metadata, skipped = [], [], 0
    from PIL import Image
    for folder, manifest in _documents(root):
        for sample in manifest["samples"]:
            if (GRADE_ORDER.index(sample.get("grade", "ai_auto")) < floor or sample.get("kind") not in kinds
                    or (engines and sample.get("engine") not in engines)):
                continue
            source = folder / _file(sample)
            try:
                data = source.read_bytes()
            except OSError:
                skipped += 1
                continue
            if hashlib.sha256(data).hexdigest() != sample.get("sha256"):
                skipped += 1                 # 조각 파일이 바뀌었다: 정답과 짝이 맞는다고 할 수 없다
                continue
            name = Path(f"{sample.get('id', '')}.png").name
            if name == ".png" or not isinstance(sample.get("label"), str) or any(c in sample["label"] for c in "\t\r\n"):
                skipped += 1
                continue
            if rotate_vertical and sample.get("direction") == "vertical":
                with Image.open(io.BytesIO(data)) as picture, picture.rotate(90, expand=True) as turned:
                    turned.save(images / name, format="PNG")
            else:
                (images / name).write_bytes(data)
            lines.append(f"images/{name}\t{sample['label']}")
            metadata.append({key: sample.get(key) for key in ("id", "engine", "engine_version", "direction", "read",
                                                              "label", "kind", "grade", "page")}
                            | {"document": manifest.get("source", ""), "image": f"images/{name}"})
    (destination / "rec_gt.txt").write_text("".join(line + "\n" for line in lines), encoding="utf-8")
    (destination / "samples.jsonl").write_text(
        "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in metadata), encoding="utf-8")
    return {"exported": len(lines), "skipped": skipped, "destination": str(destination)}


def reset(root=None) -> int:
    """모은 조각을 모두 지운다(설정은 남김). 이 모듈이 만든 문서 폴더만 지운다. 지운 문서 수를 돌려준다."""
    root = Path(root) if root is not None else samples_root()
    removed = 0
    with _LOCK:
        for folder, _ in list(_documents(root)):
            shutil.rmtree(folder, ignore_errors=True)
            removed += 1
    return removed


def summary_lines(info: dict) -> list[str]:
    size = info["bytes"] / 1_000_000
    lines = [f"글자 조각: {info['root']} · 모으기 {'켬' if info['enabled'] else '끔'}",
             f"문서 {info['documents']}개 · 조각 {info['samples']}개 · {size:.1f} MB"]
    if info["samples"]:
        for title, key in (("엔진별", "by_engine"), ("등급별", "by_grade"), ("종류별", "by_kind")):
            lines.append(f"{title}: " + ", ".join(f"{k} {v}" for k, v in sorted(info[key].items())))
    return lines
