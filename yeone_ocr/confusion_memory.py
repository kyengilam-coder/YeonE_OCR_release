"""엔진 혼동 장부: 로컬 엔진이 자주 잘못 읽는 글자를 문서를 넘어 기억한다.

비교 프로젝트에는 로컬 엔진의 원판독(기준 판독)과, 그 위에 쌓인 사람 교정·AI 제안 채택이 모두 남는다.
이 모듈은 그 차이를 "엔진 E 가 X 로 읽었는데 확정 글은 Y" 쌍으로 모아 사용자 전역 장부에 쌓는다.

쓰임새는 두 가지뿐이다.
1. 선택 검증(comparison_ai._selective_targets): 엔진 신뢰도가 높아도, 장부상 그 엔진이 자주 틀린 글자는
   의심 자리(learned-confusable)로 AI·사람 검증에 보낸다.
2. AI 검증 프롬프트: 대상 범위에 든 그런 글자와 과거에 확정된 글자를 참고 자료로 함께 보낸다.

어느 경우에도 글자를 바꾸지 않는다(지침 21: 전역 일괄 치환 금지). 장부는 "어디를 다시 볼지"만 정한다.

출처 등급과 가중치
- human        사람이 직접 고친 글(비교·교정 창의 수동 수정, AB 사람 교정 가져오기)             1.0
- ai_confirmed 사람이 고른 기계 제안(엔진 후보·AI 제안 채택), 또는 사람이 검수 완료로 표시한 쪽   0.7
- ai_auto      사람 확인 없이 자동 반영된 기계 제안(--local-ai-accept, 다수결 등, 출처 표시 없음)  0.3
자동 반영분의 가중치를 낮게 두는 까닭: AI 의 오답이 장부에 쌓여 다시 의심 자리를 만드는 자기 강화를 줄이려는 것.

검증 범위 가중 오류 비율 = 가중 오류 건수 / 실제 검증 범위 안의 해당 문자열 출현 횟수.
이는 선택된 구간의 참고 통계이며 전체 책의 OCR 정확도가 아니다. 구형 장부의 분모는 미확인으로 표시한다.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from difflib import SequenceMatcher
import hashlib
import json
import os
from pathlib import Path
import threading
import time
import unicodedata

FORMAT = "yeone-ocr-confusions"
VERSION = 1
COVERAGE_BASIS = 'verified-spans-v2'
ENV_PATH = "YEONE_OCR_CONFUSIONS_PATH"

GRADES = ("human", "ai_confirmed", "ai_auto")
WEIGHTS = {"human": 1.0, "ai_confirmed": 0.7, "ai_auto": 0.3}
MIN_WEIGHT = 2.0              # 가중 오류가 이만큼 쌓여야 의심 글자로 본다(사람 교정 2건, 자동 반영만이면 7건)
MIN_RATE = 0.05               # 그 엔진이 그 글자를 읽은 횟수 가운데 5% 이상 틀렸을 때
MAX_ROUTED_PER_PAGE = 20      # 장부 때문에 한 쪽에 더 보내는 검증 대상의 상한(호출 비용 상한)
MAX_HINTS_PER_TARGET = 8
MAX_ALTERNATIVES = 3
MAX_EXAMPLES = 3
MAX_DOCUMENTS = 500           # 오래된 문서의 기여부터 지운다
MAX_DIFF_PRODUCT = 8_000_000
MAX_CHANGED_SHARE = 0.3       # 기준 판독과 교정문이 이보다 많이 다르면(재배열·기준 교체) 그 쪽은 건너뜀
MAX_PAIR_CHARS = 4

_LOCK = threading.Lock()


def store_path() -> Path:
    """%LOCALAPPDATA%\\YeonE_OCR\\confusions.json (Windows) · ~/.local/share/YeonE_OCR/confusions.json."""
    override = os.environ.get(ENV_PATH, "").strip()
    if override:
        return Path(override).expanduser()
    from .runtime import user_data_dir
    return user_data_dir() / "confusions.json"


def _empty() -> dict:
    return {"format": FORMAT, "version": VERSION, "enabled": True, "suppressed": [], "documents": {}}


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


# ------------------------------------------------------------------ 장부 파일


def _valid(data) -> bool:
    if (not isinstance(data, dict) or data.get("format") != FORMAT or data.get("version") != VERSION
            or type(data.get("enabled")) is not bool or not isinstance(data.get("suppressed"), list)
            or not isinstance(data.get("documents"), dict)):
        return False
    for item in data["suppressed"]:
        if not isinstance(item, list) or len(item) != 3 or not all(isinstance(v, str) and v for v in item):
            return False
    for key, document in data["documents"].items():
        if (not isinstance(key, str) or not isinstance(document, dict) or not isinstance(document.get("at"), str)
                or not isinstance(document.get("engines"), dict)):
            return False
        for engine, entry in document["engines"].items():
            if (not isinstance(engine, str) or not isinstance(entry, dict)
                    or not isinstance(entry.get("seen"), dict) or not isinstance(entry.get("pairs"), list)
                    or any(not isinstance(c, str) or type(n) is not int or n < 0 for c, n in entry["seen"].items())):
                return False
            for pair in entry["pairs"]:
                if (not isinstance(pair, list) or len(pair) != 4 or not isinstance(pair[0], str) or not isinstance(pair[1], str)
                        or not pair[0] or not pair[1] or pair[0] == pair[1] or not isinstance(pair[2], dict)
                        or any(g not in GRADES or type(n) is not int or n < 0 for g, n in pair[2].items())
                        or not isinstance(pair[3], list) or any(not isinstance(e, str) for e in pair[3])):
                    return False
    return True


def read_store(path=None) -> tuple[dict, str]:
    """(장부, 경고). 없으면 빈 장부, 깨졌으면 빈 장부와 경고 — 깨진 파일은 쓰기 전에 옆에 남긴다."""
    path = Path(path) if path is not None else store_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return _empty(), ""
    except (OSError, ValueError) as exc:
        return _empty(), f"엔진 혼동 장부를 읽지 못해 이번에는 쓰지 않습니다: {exc}"
    if not _valid(data):
        return _empty(), "엔진 혼동 장부의 형식이 올바르지 않아 이번에는 쓰지 않습니다."
    return data, ""


def _write_store(data: dict, path: Path) -> None:
    from .state import write_atomic
    path.parent.mkdir(parents=True, exist_ok=True)
    write_atomic(path, json.dumps(data, ensure_ascii=False, separators=(",", ":")) + "\n")


def _update(mutate, path=None):
    """읽고-고치고-쓰기를 한 번에. 깨진 장부는 덮어쓰기 전에 .broken 사본으로 남긴다."""
    from .file_lock import exclusive_file_lock
    path = (Path(path) if path is not None else store_path()).resolve()
    with _LOCK, exclusive_file_lock(path.with_name(path.name + '.lock')):
        data, warning = read_store(path)
        if warning and path.exists():
            try:
                path.replace(path.with_name(path.name + f".broken-{time.strftime('%Y%m%d-%H%M%S')}"))
            except OSError:
                return warning
        if not warning and path.exists() and any(
                e.get('coverage_basis') != COVERAGE_BASIS for d in data['documents'].values() for e in d['engines'].values()):
            backup = path.with_name(path.name + '.before-verified-spans.json')
            try:
                with backup.open('xb') as stream:
                    stream.write(path.read_bytes())
            except FileExistsError:
                pass
        result = mutate(data)
        _write_store(data, path)
        return result


# ------------------------------------------------------------- 쪽 기록 → 혼동 쌍


def _grade(edit: dict) -> str:
    if edit.get("kind") == "manual":
        return "human"
    return "ai_confirmed" if edit.get("actor") == "human" else "ai_auto"


def _provenance(page: dict, base: str):
    """교정 이력을 다시 돌려 최종 교정문의 글자마다 누가 넣었는지('engine' 또는 등급)를 매긴다."""
    text, labels, stack = base, ["engine"] * len(base), []
    for edit in page.get("edits", []):
        before = base if edit.get("before") is None else edit["before"]
        after = base if edit.get("after") is None else edit["after"]
        if before != text:
            return None
        if edit.get("kind") == "undo":
            if not stack:
                return None
            text, labels = stack.pop()
            if text != after:
                return None
            continue
        if len(text) * len(after) > MAX_DIFF_PRODUCT:
            return None
        stack.append((text, labels))
        new = [_grade(edit)] * len(after)
        for op, i1, i2, j1, j2 in SequenceMatcher(None, text, after, autojunk=False).get_opcodes():
            if op == "equal":
                new[j1:j2] = labels[i1:i2]
        text, labels = after, new
    return text, labels


def _countable(char: str) -> bool:
    return not char.isspace() and unicodedata.category(char)[0] not in ("C", "Z")


def _noise(read: str, actual: str) -> bool:
    """판독 오류가 아닌 교정: 부호끼리, 전각·반각, 간체·번체(편집 단계의 번체 변환)."""
    if unicodedata.normalize("NFKC", read) == unicodedata.normalize("NFKC", actual):
        return True
    if all(unicodedata.category(c).startswith("P") or unicodedata.category(c).startswith("S") for c in read + actual):
        return True
    try:
        from .collate.zh import script_only
        return script_only((read, actual))
    except Exception:
        return False


def _reviewed_by_human(page: dict) -> bool:
    if not page.get("reviewed"):
        return False
    history = page.get("review_history") or []
    return not history or history[-1].get("actor") != "system"


def _checked(page: dict) -> bool:
    """이 쪽을 누군가(사람·AI) 실제로 확인했는가. 아무도 보지 않은 쪽은 분모에도 넣지 않는다."""
    return (bool(page.get("edits")) or bool(page.get("reviewed"))
            or any(r.get("status") == "completed" for r in page.get("ai_reviews", [])))


def aligned_page(page: dict):
    """기준 판독과 현재 교정문을 맞춘 결과. 장부와 글자 조각 수집(glyph_samples)이 함께 쓴다.

    돌려주는 값: {"baseline", "raw", "base", "final", "labels", "upgraded", "opcodes"} — labels[i] 는 final[i] 를
    넣은 주체('engine' 또는 등급). 아무도 확인하지 않았거나 교정의 바탕을 믿을 수 없는 쪽은 None."""
    baseline = next((run for run in page.get("runs", []) if run.get("id") == page.get("baseline_run_id")), None)
    if baseline is None or not _checked(page):
        return None
    raw = baseline["raw"]
    base = raw.get("text") or ""
    edits = page.get("edits") or []
    if edits:
        first = min((e.get("at") or "" for e in edits), default="")
        if first and (baseline.get("created_at") or "") > first:
            return None                      # 고친 뒤에 기준 판독이 새로 들어왔다: 고친 글의 바탕이 아니다
    accepted = {d.get("review_id") for d in page.get("ai_decisions", []) if d.get("decision") == "accepted"}
    bases = {r.get("request", {}).get("baseline_run_id") for r in page.get("ai_reviews", []) if r.get("id") in accepted}
    if bases - {baseline["id"], None}:
        return None                          # 채택한 AI 제안이 다른 기준 판독에서 나왔다
    final = page.get("corrected_text")
    upgraded = _reviewed_by_human(page)
    if final is None or final == base:
        return {"baseline": baseline, "raw": raw, "base": base, "final": base, "labels": ["engine"] * len(base),
                "upgraded": upgraded, "opcodes": [("equal", 0, len(base), 0, len(base))] if base else []}
    replay = _provenance(page, base)
    if replay is None or replay[0] != final or len(base) * len(final) > MAX_DIFF_PRODUCT:
        return None
    opcodes = SequenceMatcher(None, base, final, autojunk=False).get_opcodes()
    changed = sum(max(i2 - i1, j2 - j1) for op, i1, i2, j1, j2 in opcodes if op != "equal")
    if changed > max(8, MAX_CHANGED_SHARE * max(len(base), 1)):
        return None
    return {"baseline": baseline, "raw": raw, "base": base, "final": final, "labels": replay[1],
            "upgraded": upgraded, "opcodes": opcodes}


def span_grade(labels, upgraded: bool, start: int, end: int):
    """final[start:end] 를 넣은 주체의 등급. 엔진이 읽은 그대로인 글자가 섞였으면 None."""
    grades = set(labels[start:end])
    if not grades or "engine" in grades:
        return None
    grade = next(g for g in GRADES if g in grades)
    return "ai_confirmed" if grade == "ai_auto" and upgraded else grade


def verified_positions(page, aligned):
    """Union in baseline offsets: explicit edits, human review, confident visual responses.

    Editorial-only, failed, uncertain, stale-baseline and unmappable responses
    do not establish coverage. Overlapping/retried requests count only once.
    """
    from .comparison import ComparisonProject
    base = aligned['base']
    if aligned['upgraded']:
        return set(range(len(base)))
    covered = set()
    for op, a, b, c, d in aligned['opcodes']:
        if op != 'equal':
            covered.update(range(a, b))
    for review in page.get('ai_reviews', []):
        request = review.get('request', {})
        if (review.get('status') != 'completed' or request.get('phase') != 'visual'
                or request.get('baseline_run_id') != aligned['baseline']['id']
                or request.get('source_sha256') != aligned['raw'].get('source_sha256')):
            continue
        snapshot = request.get('snapshot_text')
        if not isinstance(snapshot, str):
            continue
        try:
            response = json.loads(review.get('response_text', ''))
            certain = {item['target_id'] for item in response['items'] if item.get('uncertain') is False}
        except (ValueError, KeyError, TypeError):
            continue
        for target in request.get('targets', []):
            a, b = target.get('start'), target.get('end')
            if (target.get('id') not in certain or type(a) is not int or type(b) is not int
                    or not 0 <= a < b <= len(snapshot) or target.get('before') != snapshot[a:b]):
                continue
            try:
                left, right = ComparisonProject._mapped_span(snapshot, base, a, b)
            except ValueError:
                continue
            covered.update(range(left, right))
    return covered


def _verified_counts(text, covered):
    # All 1..4-character literal sequences, so a pair learned in another book
    # also has a denominator here. Never join across gaps or unverified spans.
    counts = Counter()
    for start in sorted(covered):
        for size in range(1, MAX_PAIR_CHARS + 1):
            end = start + size
            if end > len(text) or end-1 not in covered or not _countable(text[end-1]):
                break
            counts[text[start:end]] += 1
    return counts


def page_observations(page: dict):
    """한 쪽 기록에서 (엔진, 엔진 판, 읽은 글자 수, 혼동 쌍 목록)을 뽑는다. 쓸 수 없는 쪽은 None."""
    aligned = aligned_page(page)
    if aligned is None:
        return None
    base, final, labels = aligned["base"], aligned["final"], aligned["labels"]
    pairs = []
    for op, i1, i2, j1, j2 in aligned["opcodes"]:
        if op != "replace":
            continue
        if i2 - i1 == j2 - j1:
            spans = [(i, i + 1, j, j + 1) for i, j in zip(range(i1, i2), range(j1, j2)) if base[i] != final[j]]
        elif max(i2 - i1, j2 - j1) <= MAX_PAIR_CHARS:
            spans = [(i1, i2, j1, j2)]       # 둘로 읽은 한 글자(口口→回) 등
        else:
            continue
        for a, b, c, d in spans:
            read, actual = base[a:b], final[c:d]
            if not all(_countable(ch) for ch in read + actual) or _noise(read, actual):
                continue
            grade = span_grade(labels, aligned["upgraded"], c, d)
            if grade is None:
                continue
            context = base[max(0, a - 6):a] + "「" + read + "」" + base[b:b + 6]
            pairs.append((read, actual, grade, context))
    seen = _verified_counts(base, verified_positions(page, aligned))
    if not seen and not pairs:
        return None
    raw = aligned["raw"]
    return {"engine": aligned["baseline"]["engine"], "engine_version": raw.get("engine_version", ""),
            "seen": seen, "pairs": pairs}


def project_contribution(project) -> dict:
    """비교 프로젝트 하나의 기여(엔진별 읽은 글자 수와 혼동 쌍)."""
    engines: dict[str, dict] = {}
    pages = 0
    for number in project.completed_pages:
        observed = page_observations(project.page(number))
        if observed is None:
            continue
        pages += 1
        entry = engines.setdefault(observed["engine"], {"versions": set(), "seen": Counter(), "pairs": {}})
        if observed["engine_version"]:
            entry["versions"].add(observed["engine_version"])
        entry["seen"].update(observed["seen"])
        for read, actual, grade, context in observed["pairs"]:
            counts, examples = entry["pairs"].setdefault((read, actual), (Counter(), []))
            counts[grade] += 1
            if len(examples) < MAX_EXAMPLES and context not in examples:
                examples.append(context)
    result = {}
    for engine, entry in sorted(engines.items()):
        result[engine] = {"versions": sorted(entry["versions"]), 'coverage_basis': COVERAGE_BASIS,
                          "seen": dict(sorted(entry["seen"].items())),
                          "pairs": [[read, actual, dict(sorted(counts.items())), examples]
                                    for (read, actual), (counts, examples) in sorted(entry["pairs"].items())]}
    return {"pages": pages, "engines": result}


def harvest_project(project, path=None) -> dict:
    """프로젝트의 현재 기록으로 그 문서의 기여를 바꿔 넣는다(같은 문서를 다시 거둬도 두 번 세지 않음).

    돌려주는 값: {"pages": 반영한 쪽 수, "pairs": 혼동 쌍 건수, "warning": 경고}."""
    contribution = project_contribution(project)
    identifier = getattr(project, "_id", None)
    if not isinstance(identifier, str) or not identifier:
        return {"pages": 0, "pairs": 0, "warning": "프로젝트 식별자가 없어 엔진 혼동 장부에 넣지 않았습니다."}
    pairs = sum(sum(sum(p[2].values()) for p in e["pairs"]) for e in contribution["engines"].values())

    def mutate(data):
        documents = data["documents"]
        if not contribution["engines"]:
            documents.pop(identifier, None)
            return
        documents[identifier] = {"at": _now(), "source": Path(str(project.source_path)).name,
                                 "pages": contribution["pages"], "engines": contribution["engines"]}
        if len(documents) > MAX_DOCUMENTS:
            for key in sorted(documents, key=lambda k: documents[k]["at"])[:len(documents) - MAX_DOCUMENTS]:
                documents.pop(key)

    warning = _update(mutate, path)
    return {"pages": contribution["pages"], "pairs": pairs, "warning": warning or ""}


def harvest_quietly(project, log=None, path=None) -> dict | None:
    """실행·저장 흐름을 막지 않는 거두기: 실패는 한 줄 알리고 넘어간다."""
    try:
        outcome = harvest_project(project, path)
    except Exception as exc:                 # 장부는 보조 기능: 교정본 저장·문서 출력을 실패시키지 않는다
        if log:
            log(f"  엔진 혼동 장부를 갱신하지 못했습니다(교정 결과는 그대로): {exc}")
        return None
    if log and outcome.get("warning"):
        log("  " + outcome["warning"])
    return outcome


# ------------------------------------------------------------------- 집계·조회


@dataclass(frozen=True)
class Confusion:
    engine: str
    read: str
    seen: int
    weight: float
    rate: float
    alternatives: tuple          # ((확정 글자, 가중치, {등급: 건수}, (보기, ...)), ...) 가중치 큰 순
    risky: bool
    rate_known: bool = True
    scoped_weight: float = 0.0


@dataclass(frozen=True)
class LearnedConfusions:
    """장부의 집계본. prepare_plan 에 넘기는 값이며, 비어 있으면 검증 계획을 전혀 바꾸지 않는다."""
    rows: tuple = ()
    enabled: bool = True
    warning: str = ""
    path: Path | None = None
    suppressed: frozenset = frozenset()
    _risky: dict = field(default_factory=dict, compare=False, repr=False)

    @classmethod
    def from_store(cls, data: dict, *, path=None, warning="") -> "LearnedConfusions":
        suppressed = frozenset(tuple(item) for item in data.get("suppressed", []))
        seen: dict[tuple[str, str], int] = Counter()
        pairs: dict[tuple[str, str], dict[str, list]] = {}
        scoped_pairs = Counter()
        legacy_pairs = Counter()
        for document in data.get("documents", {}).values():
            for engine, entry in document.get("engines", {}).items():
                scoped = entry.get('coverage_basis') == COVERAGE_BASIS
                if scoped:
                    for char, count in entry.get("seen", {}).items():
                        seen[(engine, char)] += count
                for read, actual, counts, examples in entry.get("pairs", []):
                    slot = pairs.setdefault((engine, read), {}).setdefault(actual, [Counter(), []])
                    slot[0].update(counts)
                    (scoped_pairs if scoped else legacy_pairs)[(engine, read, actual)] += sum(WEIGHTS[g]*n for g,n in counts.items())
                    for example in examples:
                        if len(slot[1]) < MAX_EXAMPLES and example not in slot[1]:
                            slot[1].append(example)
        rows, risky = [], {}
        for (engine, read), alternatives in pairs.items():
            listed = []
            for actual, (counts, examples) in alternatives.items():
                if (engine, read, actual) in suppressed:
                    continue
                weight = sum(WEIGHTS[g] * n for g, n in counts.items())
                listed.append((actual, round(weight, 3), dict(sorted(counts.items())), tuple(examples)))
            if not listed:
                continue
            listed.sort(key=lambda a: (-a[1], a[0]))
            weight = round(sum(a[1] for a in listed), 3)
            count = seen.get((engine, read), 0)
            scoped_weight = round(sum(scoped_pairs[(engine, read, a[0])] for a in listed), 3)
            legacy_weight = sum(legacy_pairs[(engine, read, a[0])] for a in listed)
            rate = min(1.0, scoped_weight / count) if count else 0.0
            flagged = (scoped_weight >= MIN_WEIGHT and rate >= MIN_RATE)
            # Old multi-character observations had no denominator. Retain their
            # repeated-error evidence as a count-based hint, never a fake rate.
            flagged = flagged or (1 < len(read) <= MAX_PAIR_CHARS and legacy_weight >= MIN_WEIGHT)
            row = Confusion(engine, read, count, weight, round(rate, 4), tuple(listed), flagged,
                            count > 0 and legacy_weight == 0, scoped_weight)
            rows.append(row)
            if flagged:
                risky.setdefault(engine, {})[read] = row
        rows.sort(key=lambda r: (not r.risky, -r.weight, r.engine, r.read))
        return cls(tuple(rows), bool(data.get("enabled", True)), warning, path, suppressed, risky)

    @classmethod
    def load(cls, path=None) -> "LearnedConfusions":
        path = Path(path) if path is not None else store_path()
        data, warning = read_store(path)
        return cls.from_store(data, path=path, warning=warning)

    def risky(self, engine: str) -> dict:
        """{글자: Confusion} — 이 엔진에서 검증에 보낼 만큼 자주 틀린 글자. 꺼져 있으면 빈 값."""
        return dict(self._risky.get(engine, {})) if self.enabled else {}

    def active(self) -> bool:
        return self.enabled and any(self._risky.values())

    def hints(self, engine: str, text: str) -> list[dict]:
        """대상 글에 든 의심 글자의 참고 자료(프롬프트용). 글자를 바꾸라는 지시가 아니다."""
        risky = self.risky(engine)
        result = []
        for char in sorted((read for read in risky if read in text), key=lambda read: (text.find(read), -len(read), read)):
            row = risky[char]
            result.append({"engine": engine, "read": char,
                           "verified_alternatives": [a[0] for a in row.alternatives[:MAX_ALTERNATIVES]],
                           "past_error_rate": row.rate if row.rate_known else None, "past_errors_weighted": row.weight})
            if len(result) == MAX_HINTS_PER_TARGET:
                break
        return result

    def digest(self) -> str:
        payload = [[r.engine, r.read, r.rate, r.rate_known, r.weight, [a[0] for a in r.alternatives]] for r in self.rows if r.risky]
        return hashlib.sha256(json.dumps([self.enabled, payload], ensure_ascii=False).encode("utf-8")).hexdigest()


def load_for_review(log=None) -> LearnedConfusions | None:
    """검증 계획에 넘길 장부. 꺼져 있거나 비어 있으면 None(계획이 장부 없던 때와 똑같다)."""
    try:
        learned = LearnedConfusions.load()
    except Exception as exc:
        if log:
            log(f"  엔진 혼동 장부를 읽지 못해 이번에는 쓰지 않습니다: {exc}")
        return None
    if learned.warning and log:
        log("  " + learned.warning)
    return learned if learned.active() else None


# ------------------------------------------------------------------- 사용자 조작


def set_enabled(enabled: bool, path=None) -> None:
    if type(enabled) is not bool:
        raise ValueError("사용 여부는 참 또는 거짓이어야 합니다.")
    _update(lambda data: data.__setitem__("enabled", enabled), path)


def suppress(engine: str, read: str, actual: str, path=None) -> None:
    """잘못 쌓인 쌍을 제외한다. 다시 거둬도 되살아나지 않는다."""
    item = [engine, read, actual]
    if not all(isinstance(v, str) and v for v in item):
        raise ValueError("제외할 혼동 쌍이 올바르지 않습니다.")

    def mutate(data):
        if item not in data["suppressed"]:
            data["suppressed"].append(item)
    _update(mutate, path)


def unsuppress(engine: str, read: str, actual: str, path=None) -> None:
    item = [engine, read, actual]
    _update(lambda data: data.__setitem__("suppressed", [s for s in data["suppressed"] if s != item]), path)


def reset(path=None) -> None:
    """장부를 비운다(사용 여부는 유지). 교정본·비교 프로젝트는 건드리지 않는다."""
    def mutate(data):
        data["documents"] = {}
        data["suppressed"] = []
    _update(mutate, path)


def summary_lines(learned: LearnedConfusions, top: int = 30) -> list[str]:
    """CLI·보고서용 표."""
    if not learned.rows:
        return ["(엔진 혼동 장부가 비어 있습니다. 비교·교정 창에서 고치거나 로컬 우선 인식을 돌리면 쌓입니다.)"]
    lines = [f"엔진 혼동 장부: {learned.path} · 검증 경로에 {'사용' if learned.enabled else '사용 안 함'}",
             "엔진 | 읽은 글자 → 확정 글자 | 검증 범위 가중 오류 비율(전체 OCR 정확도 아님) | 의심 글자"]
    for row in learned.rows[:top]:
        alternatives = ", ".join(f"{a[0]}({a[1]:g})" for a in row.alternatives[:MAX_ALTERNATIVES])
        ratio = f'{row.scoped_weight:g} / {row.seen} = {row.rate:.1%}' if row.rate_known else '검증 범위 미확인(구형 기록 포함)'
        lines.append(f"{row.engine} | {row.read} → {alternatives} | {ratio}"
                     f" | {'예' if row.risky else '-'}")
    if len(learned.rows) > top:
        lines.append(f"… 외 {len(learned.rows) - top}개")
    return lines
