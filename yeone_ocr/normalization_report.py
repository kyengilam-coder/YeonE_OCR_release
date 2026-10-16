"""번체·문맥 교정 기록을 브라우저에서 읽을 수 있는 독립 HTML로 만든다."""
from __future__ import annotations

from html import escape
from urllib.parse import quote


def render_normalization_report(report: dict) -> str:
    def esc(value):
        return escape(str(value if value is not None else ""))

    def location(item):
        parts = []
        if item.get("page") is not None: parts.append(f"PDF {item['page']}쪽")
        if item.get("block") is not None: parts.append(f"문단 {item['block'] + 1}")
        if item.get("row") is not None: parts.append(f"표 {item['row'] + 1}행 {item['column'] + 1}열")
        return " · ".join(parts)

    def table(headers, rows):
        if not rows:
            return '<p class="empty">해당 항목이 없습니다.</p>'
        return ('<div class="scroll"><table><thead><tr>' + "".join(f"<th>{esc(h)}</th>" for h in headers)
                + "</tr></thead><tbody>" + "".join("<tr>" + "".join(f"<td>{esc(cell)}</td>" for cell in row) + "</tr>" for row in rows)
                + "</tbody></table></div>")

    summary = report.get("summary") or {}
    reader = report.get("reader") or {}
    output = report.get("output", "")
    enabled = report.get("enabled", True)
    status = "문맥 교정 확인 필요" if not report.get("complete", True) else "교정 처리 완료"
    if not enabled: status = "자동 교정 생략"
    review = table(["위치", "보존한 원문", "후보", "확인할 내용", "앞뒤 문맥"], [
        [location(item), item.get("before", ""), " / ".join(item.get("candidates") or [item.get("after", "")]),
         item.get("reason", ""), item.get("original_context", item.get("context", ""))]
        for item in report.get("review", [])])
    changed = table(["위치", "교정 전", "교정 후"], [
        [location(item), item["before"], item["after"]]
        for item in report.get("blocks", []) if item.get("before") != item.get("after")])
    decisions = table(["원문", "후보", "문맥 판정", "원문 문맥"], [
        [item.get("source", ""), " / ".join(item.get("candidates") or []),
         item.get("choice") or "보류 · 원문 유지", item.get("original_context", item.get("context", ""))]
        for item in report.get("decisions", [])])
    stages = {"opencc_s2t": "간체→번체", "msr": "중문부호 규칙", "yun_context": "云/雲 문맥", "model_context": "모델 문맥 판정"}
    changes = table(["위치", "교정 전", "교정 후", "처리", "근거"], [
        [location(item), item.get("before", ""), item.get("after", ""), stages.get(item.get("stage"), "문맥 교정"),
         item.get("reason") or (f"치환 규칙 {item['rule_id']}" if item.get("rule_id") else "간체→번체 변환")]
        for item in report.get("changes", [])])
    error = f'<p class="notice">{esc(reader["error"])}</p>' if reader.get("error") else ""
    other = (f"다른 모델이 앞서 고른 답을 그대로 씀 {int(reader.get('other_hits') or 0):,}건 · 다른 모델이 고르지 못해 원문대로 둠 "
             f"{int(reader.get('other_remembered') or 0):,}건 (이번 모델로 다시 물으려면 창에서 「다른 모델이 이미 물은 자리·후보는 다시 묻지 않기」를 끄거나 "
             "명령줄 --skip-answered same-model)<br>"
             if reader.get("other_hits") or reader.get("other_remembered") else "")
    link = f'<a href="{quote(str(output), safe="")}">HWPX 열기</a>' if output else ""
    options = report.get("options")
    selected = (" · ".join(f"{label} {'켬' if options.get(key) else '끔'}" for key, label in
                (("traditional", "간체→번체"), ("yun", "云/雲"), ("punctuation", "중문부호"))) if isinstance(options, dict) else "")
    selected_html = f"자동 교정: {esc(selected)}<br>" if selected else ""
    custom_html = "중문부호: 사용자 규칙 적용(규칙 원문은 JSON 보고에 보존)<br>" if report.get("custom_punctuation") else ""
    return f'''<!doctype html><html lang="ko"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>문맥 교정 기록 · {esc(output)}</title>
<style>
body{{font-family:"Malgun Gothic",sans-serif;line-height:1.65;margin:0;background:#f4f2ec;color:#25231f}}
main{{max-width:1200px;margin:auto;padding:32px 24px}}h1{{font-size:26px;margin:0 0 8px}}h2{{font-size:20px;margin-top:36px}}
.meta,.empty{{color:#666}}.notice{{padding:14px 18px;background:#fff1d4;border-left:4px solid #ad6615}}
.count{{display:flex;gap:12px;flex-wrap:wrap}}.count span{{padding:10px 18px;background:white;border:1px solid #ded9ce;border-radius:6px}}
.scroll{{overflow:auto}}table{{border-collapse:collapse;background:#fff;width:100%;font-family:"Malgun Gothic",serif}}
th,td{{padding:10px 12px;border:1px solid #ded9ce;text-align:left;vertical-align:top;white-space:pre-wrap;overflow-wrap:anywhere}}
th{{background:#e9e5dc;white-space:nowrap}}td{{min-width:60px}}a{{color:#245e9d}}summary{{cursor:pointer;margin:16px 0;font-weight:bold}}
@media print{{main{{max-width:none;padding:0}}.scroll{{overflow:visible}}details{{display:block}}}}
</style><main><h1>{esc(status)}</h1><p class="meta">{esc(output)} · {link}</p>
<div class="count"><span>변경 {int(summary.get('changes', 0)):,}건</span><span>확인 기록 {int(summary.get('review', 0)):,}건</span>
<span>문맥 판정 {len(report.get('decisions', [])):,}건</span></div>
<p>이 보고는 OCR 원문과 최종 문서의 차이를 보여 줍니다. 매크로의 한자 치환은 문맥 판정의 후보로만 사용합니다.
판정이 확실하지 않은 글자는 원문을 유지했습니다. 모델의 문맥 판단도 원본 대조가 필요할 수 있습니다.</p>
<p class="meta">문맥 모델: {esc(reader.get('provider', '없음'))} / {esc(reader.get('model', ''))}<br>{other}
{selected_html}{custom_html}치환 규칙: {esc(report.get('msr_source', ''))} · {int(report.get('rule_count', 0))}개<br>
원문 판독 기록은 별도로 보존됩니다.</p>{error}
<h2>확인할 자리</h2>{review}<h2>문단별 교정 전후</h2>{changed}
<details><summary>문맥 판정 전체</summary>{decisions}</details>
<details><summary>개별 변경 전체</summary>{changes}</details></main></html>'''
