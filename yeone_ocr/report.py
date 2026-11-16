"""작업 상태 보고(지침 33). 실제 확인한 범위에 한정해서 '완료'를 쓴다."""
from __future__ import annotations
from .state import Work
from .schema import IMAGE_KINDS


def _ranges(nums: list[int]) -> str:
    if not nums: return ""
    out, s, prev = [], nums[0], nums[0]
    for n in nums[1:]:
        if n == prev + 1: prev = n; continue
        out.append(f"{s}" if s == prev else f"{s}~{prev}"); s = prev = n
    out.append(f"{s}" if s == prev else f"{s}~{prev}")
    return ", ".join(out)


def make_report(work: Work, total_pages: int) -> str:
    st = work.load_state()
    lines = [f"# 작업 상태 보고 — {work.pdf_path.name}", ""]
    groups: dict[str, list[int]] = {}
    details = []
    tot = {"pages": 0, "calls": 0, "prompt_tokens": 0, "output_tokens": 0, "seconds": 0.0}; models: dict[str, int] = {}
    for n in range(1, total_pages + 1):
        p = st["pages"].get(str(n))
        key = " · ".join(p["statuses"]) if p and p.get("statuses") else "미처리"
        groups.setdefault(key, []).append(n)
        rec = work.load_record(n)
        if rec:
            m = rec["merged"]
            figs = [r for r in m["regions"] if r["kind"] in IMAGE_KINDS]
            unc = len(m.get("uncertain", []))
            errs = [i for i in rec.get("verify", []) if i["level"] == "error"]
            d = f"PDF {n}쪽" + (f" (인쇄 {m['print_page']}쪽)" if m.get("print_page") else " (인쇄 쪽수 확인 안 됨)") + f": 판독기 {rec.get('reader')}"
            if figs: d += f", 도판 {len(figs)}개 이미지 보존"
            if unc: d += f", 판독 확인 필요 {unc}건"
            if errs: d += f", 구조 검사 오류 {len(errs)}건"
            ms = rec.get("merge_stats") or {}
            if ms.get("columns_compared"): d += f", 2차 비교 {ms['columns_compared']}열 중 {ms['columns_differ']}열 불일치"
            if rec.get("reader") == "mock": d += " — 기존 OCR 레이어 그대로, 원본 대조 전"
            if rec.get("local_first"):
                review = rec["local_first"].get("review", {})
                d += f", 로컬 우선·선택 검증 {review.get('status', '')}·대상 {review.get('targets', 0)}개"
                page_ai = rec["local_first"].get("ai_page")
                if page_ai:
                    d += f", 쪽 AI 판독 {page_ai.get('model', '')}"
                    if page_ai.get("cached"):
                        d += "(저장본 재사용, 새 쪽 판독 호출 없음)"
                    if "usage" not in page_ai:
                        d += "(이전 기록: 쪽 AI 사용량 미집계)"
            u = rec.get("usage") or {}
            if u.get("calls"):
                d += f", 호출 {u['calls']}회·토큰 {u.get('prompt_tokens', 0):,}/{u.get('output_tokens', 0) + u.get('thinking_tokens', 0):,}"
                tot["pages"] += 1; tot["calls"] += u["calls"]; tot["prompt_tokens"] += u.get("prompt_tokens", 0); tot["output_tokens"] += u.get("output_tokens", 0) + u.get("thinking_tokens", 0)
                tot["seconds"] += float(rec.get("seconds") or 0); models[rec.get("model", "")] = models.get(rec.get("model", ""), 0) + 1
            details.append(d)
    lines.append("## 구간별 상태")
    for key, nums in groups.items():
        lines.append(f"- PDF {_ranges(nums)}쪽: {key}")
    lines.append("")
    lines.append("## 쪽별 요약")
    lines.extend(f"- {d}" for d in details) if details else lines.append("- (판독 기록 없음)")
    lines.append("")
    if tot["pages"]:
        lines.append("## 모델 호출 (판독 기록이 있는 쪽 누적)")
        lines.append(f"- 모델: {', '.join(f'{m} {c}쪽' for m, c in models.items())}")
        lines.append(f"- {tot['pages']}쪽 · 호출 {tot['calls']}회 · 토큰 입력 {tot['prompt_tokens']:,} 출력 {tot['output_tokens']:,} · 판독 시간 {tot['seconds']:.0f}초")
        lines.append(f"- 쪽당 평균: 호출 {tot['calls'] / tot['pages']:.1f}회 · 입력 {tot['prompt_tokens'] / tot['pages']:,.0f} 출력 {tot['output_tokens'] / tot['pages']:,.0f} 토큰 · {tot['seconds'] / tot['pages']:.0f}초")
        lines.append("- 저장된 응답의 누적 사용량입니다. 쪽 AI 판독과 선택 구간 검증을 합산하며, 저장본 재사용은 원래 응답을 한 번만 셉니다. 이번 실행의 새 호출 수나 청구 금액이 아닙니다.")
        lines.append("- 토큰은 제공사가 반환한 값만 집계합니다. 실패·재시도 등 저장되지 않은 응답은 포함하지 않으며, 이전 판의 쪽 AI 사용량은 기록에 없을 수 있습니다. 구독 사용량과 유료 API 요금은 각 제공사 내역에서 확인하세요.")
        lines.append("- 출력 합계에는 별도로 보고된 생각 토큰을 더합니다. 판독 시간은 쪽별 경과시간의 합으로, 병렬 실행의 실제 전체 소요시간과 다릅니다.")
        lines.append("")
    try:
        from .ledger import build_ledger, ledger_text
        led = build_ledger(work.dir)
        lines.append("## 다발 오인식 장부 (1차↔2차 갈림 · 판독↔글자층 · HWPX↔판독)")
        lines.extend(ledger_text(led)); lines.append("")
    except Exception as e:                       # 보고가 장부 때문에 멈추지 않게
        lines.append(f"## 다발 오인식 장부\n- (장부를 만들지 못함: {e})\n")
    lines.append("## 주의")
    lines.append("- '원본 대조 완료(사람)' 은 검수자가 `mark` 명령으로 붙일 때만 기록됩니다. 프로그램은 이 상태를 스스로 붙이지 않습니다.")
    lines.append("- 자동 OCR·2차 비교·구조 검사는 전수 교감을 대신하지 않습니다(지침 27).")
    return "\n".join(lines)
