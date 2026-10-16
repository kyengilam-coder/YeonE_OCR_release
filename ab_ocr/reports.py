"""Portable reports with escaped content and atomic, source-protecting writes."""
from __future__ import annotations

import csv
from datetime import datetime, timezone
import html
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile

from . import __version__


def atomic_write(path: str | Path, content: str, *, protected_paths=()) -> Path:
    destination = Path(path).expanduser().resolve()
    protected = {Path(p).expanduser().resolve() for p in protected_paths if p}
    if destination in protected or any(
        destination.exists() and p.exists() and os.path.samefile(destination, p)
        for p in protected
    ):
        raise ValueError("원본·정답·인식 결과를 덮어쓸 수 없습니다. 다른 저장 이름을 고르십시오.")
    if not destination.parent.is_dir():
        raise ValueError("저장할 폴더가 없습니다.")
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="",
                                         dir=destination.parent, prefix=".abocr-", suffix=".tmp",
                                         delete=False) as stream:
            temp_path = Path(stream.name)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, destination)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
    return destination


def _escape(value) -> str:
    return html.escape(str(value), quote=True)


def write_text(text: str, path: str | Path, *, protected_paths=()) -> Path:
    return atomic_write(path, text, protected_paths=protected_paths)


def read_utf8(path: str | Path, *, max_bytes=8_000_000) -> str:
    return read_utf8_snapshot(path, max_bytes=max_bytes)[0]


def read_utf8_snapshot(path: str | Path, *, max_bytes=8_000_000) -> tuple[str, str]:
    with Path(path).open("rb") as stream:
        data = stream.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise ValueError("텍스트 파일이 너무 큽니다. 쪽 단위로 나누십시오.")
    return data.decode("utf-8-sig"), hashlib.sha256(data).hexdigest()


def _percentage(value) -> str:
    return "해당 없음" if value is None else f"{float(value):.2%}"


def _metric_section(label: str, metric: dict) -> str:
    cards = [("글자 오류율 CER", _percentage(metric.get("cer"))),
             ("정답 글자", metric.get("reference_length", 0)),
             ("치환", metric.get("substitutions", 0)), ("누락", metric.get("deletions", 0)),
             ("추가", metric.get("insertions", 0))]
    content = "<h2>" + _escape(label) + "</h2><div class='metrics'>"
    content += "".join(f"<div><small>{_escape(k)}</small><strong>{_escape(v)}</strong></div>" for k, v in cards)
    content += "</div>"
    rows = []
    for error in metric.get("errors", []):
        rows.append("<tr>" + "".join(f"<td>{_escape(value)}</td>" for value in (
            error.get("operation", ""), error.get("reference_position", ""),
            error.get("hypothesis_position", ""), error.get("reference", "") or "∅",
            error.get("hypothesis", "") or "∅",
            error.get("reference_context", ""), error.get("hypothesis_context", ""))) + "</tr>")
    if rows:
        content += ("<p>위치는 0부터 세는 Unicode 코드 포인트입니다. ∅는 글자 없음을 뜻합니다.</p>"
                    "<div class='scroll'><table><thead><tr><th>종류</th><th>정답 위치</th><th>결과 위치</th>"
                    "<th>정답</th><th>결과</th><th>정답 문맥</th><th>결과 문맥</th></tr></thead><tbody>"
                    + "".join(rows) + "</tbody></table></div>")
    if metric.get("errors_truncated"):
        content += "<p>오류 목록은 일부만 표시합니다. CER과 오류 합계는 전체 글자를 기준으로 계산했습니다.</p>"
    by_script = metric.get("by_script")
    if by_script:
        content += "<details><summary>문자 종류별 오류</summary><pre>" + _escape(
            json.dumps(by_script, ensure_ascii=False, indent=2)) + "</pre></details>"
    return content


def render_html(report: dict) -> str:
    """Self-contained report, no scripts, remote assets, or trusted input HTML."""
    title = "AB_OCR · 정확도 평가"
    sections = []
    if "raw" in report and "normalized" in report:
        sections.append(_metric_section("원문 그대로 비교", report["raw"]))
        sections.append(_metric_section("선택한 정규화 규칙으로 비교", report["normalized"]))
    else:
        # The complete structured batch result is retained, including missing
        # pages and cohort details; a generic table must not invent rankings.
        sections.append("<h2>문서 묶음 평가</h2><p>누락·실패와 공통 평가 범위를 함께 확인하십시오.</p>")
        rows = []
        for engine_id, engine in report.get("engines", {}).items():
            rows.append("<tr>" + "".join(f"<td>{_escape(v)}</td>" for v in (
                engine.get("name", engine_id), f"{engine.get('completed_pages', 0)} / {engine.get('total_pages', 0)}",
                _percentage(engine.get("raw", {}).get("cer")),
                _percentage(engine.get("normalized", {}).get("cer")),
                "전체 완료" if engine.get("complete") else "누락·실패 있음")) + "</tr>")
        sections.append("<table><tr><th>엔진</th><th>평가 쪽</th><th>원문 CER</th><th>정규화 CER</th>"
                        "<th>완료 상태</th></tr>" + "".join(rows) + "</table>")
        if not report.get("ranking", {}).get("available"):
            sections.append("<p>순위를 제시하지 않습니다: " + _escape(report.get("ranking", {}).get("reason", "")) + "</p>")
    metadata = {k: v for k, v in report.items() if k not in ("raw", "normalized", "pages")}
    sections.append("<details open><summary>실행 조건과 집계</summary><pre>" +
                    _escape(json.dumps(metadata, ensure_ascii=False, indent=2)) + "</pre></details>")
    if "pages" in report:
        sections.append("<details><summary>쪽별 결과</summary><pre>" +
                        _escape(json.dumps(report["pages"], ensure_ascii=False, indent=2)) + "</pre></details>")
    css = """
    :root{color-scheme:light;font-family:'Malgun Gothic',system-ui,sans-serif;color:#183047;background:#edf2f6}
    body{margin:0;padding:36px}main{max-width:1220px;margin:auto;background:white;border-radius:14px;padding:32px}
    .brand{color:#247b77;font-weight:800;letter-spacing:2px}h1{font-size:28px;margin:8px 0 14px}h2{margin-top:32px}
    p{line-height:1.8;color:#4b6172}.metrics{display:flex;gap:12px;flex-wrap:wrap}
    .metrics div{border:1px solid #d9e5eb;background:#f6f9fb;padding:18px;flex:1;min-width:115px;border-radius:10px}
    small,strong{display:block}strong{font-size:24px;margin-top:8px}small{color:#557080}
    .scroll{overflow-x:auto}table{border-collapse:collapse;width:100%;font-size:14px}
    th,td{text-align:left;border-bottom:1px solid #d9e5eb;padding:11px;white-space:pre-wrap;overflow-wrap:anywhere}
    th{background:#edf5f5}pre{white-space:pre-wrap;overflow-wrap:anywhere;font-size:13px;line-height:1.6}
    details{margin-top:24px;border-top:1px solid #d9e5eb;padding-top:16px}summary{cursor:pointer;font-weight:700}
    footer{color:#557080;margin-top:28px;font-size:12px}@media print{body{padding:0;background:white}main{padding:0}}
    """
    return ("<!doctype html><html lang='ko'><meta charset='utf-8'><meta name='viewport' content='width=device-width'>"
            f"<title>{_escape(title)}</title><style>{css}</style><main><div class='brand'>AB_OCR</div>"
            "<h1>한글·한자 인식 정확도 보고서</h1>"
            "<p>CER = (치환 + 누락 + 추가) / 정답 글자 수. 낮을수록 좋으며 100%를 넘을 수 있습니다. "
            "정답이 빈 경우 CER은 정의하지 않습니다. 인식 신뢰도와 실제 정확도는 다릅니다.</p>"
            + "".join(sections) + f"<footer>AB_OCR {_escape(__version__)} · 평가 자료와 정규화 규칙에 한정된 결과</footer>"
            "</main></html>")


def _csv_cell(value):
    # Spreadsheet programs interpret formula-looking labels even in quoted CSV.
    if isinstance(value, str) and value.startswith(("=", "+", "-", "@", "\t", "\r")):
        return "'" + value
    return value


def render_csv(report: dict) -> str:
    stream = io.StringIO(newline="")
    writer = csv.writer(stream)
    writer.writerow(["mode", "reference_characters", "hypothesis_characters", "substitutions",
                     "deletions", "insertions", "distance", "CER", "policy_json", "metadata_json"])
    metadata = {key: value for key, value in report.items() if key not in ("raw", "normalized", "pages")}
    for mode in ("raw", "normalized"):
        if mode in report:
            metric = report[mode]
            selected_policy = {"unicode_form": "none", "whitespace": "preserve"} if mode == "raw" else report.get("policy", {})
            writer.writerow([mode, *[_csv_cell(metric.get(k, "")) for k in (
                "reference_length", "hypothesis_length", "substitutions", "deletions", "insertions", "distance", "cer")],
                json.dumps(selected_policy, ensure_ascii=False), json.dumps(metadata, ensure_ascii=False)])
    if "raw" not in report:
        raise ValueError("문서 묶음 보고서는 JSON 또는 HTML로 저장하십시오.")
    return "\ufeff" + stream.getvalue()


def write_report(report: dict, path: str | Path, *, protected_paths=()) -> Path:
    path = Path(path)
    payload = dict(report)
    payload.setdefault("application_version", __version__)
    payload.setdefault("generated_at", datetime.now(timezone.utc).isoformat())
    suffix = path.suffix.lower()
    if suffix == ".html":
        content = render_html(payload)
    elif suffix == ".json":
        content = json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    elif suffix == ".csv":
        content = render_csv(payload)
    else:
        raise ValueError("보고서 확장자는 .html, .json 또는 .csv여야 합니다.")
    inputs = list(protected_paths) + list(report.get("protected_input_paths", []))
    return atomic_write(path, content, protected_paths=inputs)


def _publish_new(temporary: Path, destination: Path) -> None:
    if os.name == "nt":
        # Windows rename fails when the destination exists; unlike replace.
        os.rename(temporary, destination)
    else:
        os.link(temporary, destination)


def write_recognition(result: dict, text_path: str | Path) -> tuple[Path, Path]:
    """Publish a new transcript + provenance pair; never overwrite an old pair."""
    text_path = Path(text_path).resolve()
    record_path = text_path.with_suffix(".ocr.json")
    if text_path.exists() or record_path.exists():
        raise ValueError("인식문 또는 인식 기록이 이미 있습니다. 새 출력 이름을 고르십시오.")
    if text_path.suffix.lower() != ".txt" or not text_path.parent.is_dir():
        raise ValueError("기존 폴더 안의 새 .txt 경로를 고르십시오.")
    source = Path(result["source_path"]).resolve()
    if source in (text_path, record_path):
        raise ValueError("원본 파일에 인식 결과를 저장할 수 없습니다.")
    contents = [result["text"].encode("utf-8"),
                (json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")]
    staged = []
    published = []
    try:
        for data in contents:
            with tempfile.NamedTemporaryFile(dir=text_path.parent, prefix=".abocr-", delete=False) as stream:
                staged.append(Path(stream.name))
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
        for temp_path, destination, data in zip(staged, (text_path, record_path), contents):
            _publish_new(temp_path, destination)
            stat = destination.stat()
            published.append((destination, (stat.st_dev, stat.st_ino), hashlib.sha256(data).digest()))
        return text_path, record_path
    except OSError:
        for destination, identity, digest in reversed(published):
            if destination.is_file():
                stat = destination.stat()
                if (stat.st_dev, stat.st_ino) == identity and hashlib.sha256(destination.read_bytes()).digest() == digest:
                    destination.unlink()
        raise
    finally:
        for temp_path in staged:
            temp_path.unlink(missing_ok=True)
