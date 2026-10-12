"""검수용 정적 HTML. 왼쪽 원본 쪽 이미지, 오른쪽 판독문·확인 필요 목록·기존 OCR 과의 차이·구조 검사 결과."""
from __future__ import annotations
import html, os, re
from pathlib import Path
from .state import Work
from .schema import PageReading, TEXT_KINDS, TITLE_KINDS, IMAGE_KINDS, paragraph_text, bbox_frac

CSS = """
body{font-family:'Noto Serif KR','Batang','NanumMyeongjo',serif;margin:0;background:#f4efe4;color:#231c18}
header{padding:12px 20px;background:#3b1b14;color:#f1e7cf;display:flex;gap:16px;align-items:baseline;flex-wrap:wrap}
header h1{font-size:18px;margin:0}header small{opacity:.8}
.page{display:grid;grid-template-columns:minmax(280px,42%) 1fr;gap:16px;padding:16px 20px;border-top:3px solid #b99552;background:#faf6ec}
.page:nth-child(even){background:#f1e9d6}
.img img{width:100%;height:auto;border:1px solid #c9b88a;background:#fff;display:block}
.sheet{position:relative;display:inline-block;width:100%}
.box{position:absolute;box-sizing:border-box;border:1px solid rgba(90,74,62,.55);pointer-events:none}
.box span{position:absolute;left:0;top:-14px;font-size:10px;line-height:12px;padding:0 3px;background:rgba(90,74,62,.75);color:#fff;border-radius:2px;white-space:nowrap}
.box.img-kind{border-color:rgba(31,95,191,.8)}.box.img-kind span{background:rgba(31,95,191,.85)}
.box.title{border-color:rgba(193,52,31,.8)}.box.title span{background:rgba(193,52,31,.85)}
.box.orphan{border:2px dashed #b3261e;background:rgba(179,38,30,.12)}.box.orphan span{background:#b3261e;top:auto;bottom:-14px}
.legend{font-size:11px;color:#5a4a3e;margin-top:4px}
.img .crops{display:flex;flex-wrap:wrap;gap:8px;margin-top:8px}.img .crops img{width:120px}
.meta{font-size:13px;color:#5a4a3e;margin-bottom:8px}.meta b{color:#c1341f}
.reg{margin:8px 0;padding:8px 10px;border-left:4px solid #b99552;background:rgba(255,255,255,.55);border-radius:4px}
.reg .k{font-size:12px;color:#fff;background:#5a4a3e;border-radius:3px;padding:1px 6px;margin-right:6px}
.reg.img-kind{border-left-color:#1f5fbf}.reg.title{border-left-color:#c1341f}
.reg p{margin:4px 0;line-height:1.75;font-size:15px;white-space:pre-wrap}
mark{background:#ffd9a8;padding:0 2px}
.g{color:#0B7A3B;text-decoration:underline dotted #0B7A3B}.b{color:#1F4FBF;background:#E1EBFF;text-decoration:underline dashed #1F4FBF}
mark{color:#C00000;font-weight:bold;background:#FFFF00}mark.c{text-decoration:underline solid #C00000}.note{color:#777;font-size:12px}
.grades{font-size:12px;color:#5a4a3e}.ctx td{font-size:12px;padding:1px 6px}
.unc li,.diff li,.ver li{font-size:13px;margin:2px 0}
.diff{columns:2;column-gap:16px}.ver .error{color:#b3261e}
.status span{display:inline-block;font-size:12px;background:#e4d7b4;border-radius:10px;padding:1px 8px;margin:2px}
details summary{cursor:pointer;font-size:13px;color:#5a4a3e}
"""


def _mark(t: str) -> str:
    t = html.escape(t)
    return t.replace("〔", "<mark>〔").replace("〕", "〕</mark>")


def _graded(text: str, spans) -> str:
    """문단 글을 문서와 같은 확인 등급으로: 〔〕 표시는 노랑, 3차가 고른 글자는 초록·파랑, 〔〕 안 문맥 추정 후보는 파란 밑줄."""
    from .grades import styled_runs
    out = []
    for seg, kind, _ in styled_runs(text, spans):
        e = html.escape(seg)
        out.append({"body": e, "mark": f"<mark>{e}</mark>", "note": f"<span class=note>{e}</span>", "green": f"<span class=g>{e}</span>",
                    "blue": f"<span class=b>{e}</span>", "red_ctx": f"<mark class=c>{e}</mark>"}.get(kind, e))
    return "".join(out)


ORPHAN_RE = re.compile(r"가로 (\d+)~(\d+)%, 세로 (\d+)~(\d+)%")
ORPHAN_RE_OLD = re.compile(r"x (\d\.\d+)~(\d\.\d+), y (\d\.\d+)~(\d\.\d+)")


def orphan_boxes(pr: PageReading) -> list[tuple[float, float, float, float]]:
    """확인 항목 글에서 '영역 밖 글줄' 좌표를 읽어 (x0,y0,x1,y1) 비율로. 새 표기(가로 a~b%)와 옛 표기(x 0.48~0.49) 모두 읽는다."""
    out = []
    for u in pr.uncertain:
        m = ORPHAN_RE.search(u.context)
        if m:
            x0, x1, y0, y1 = (int(v) / 100 for v in m.groups()); out.append((x0, y0, x1, y1)); continue
        m = ORPHAN_RE_OLD.search(u.context)
        if m:
            x0, x1, y0, y1 = (float(v) for v in m.groups()); out.append((x0, y0, x1, y1))
    return out


def overlay_boxes(pr: PageReading) -> str:
    """원본 이미지 위에 겹쳐 그릴 상자들: 모델이 나눈 영역(회색=문자, 파랑=도판, 빨강=제목)과 영역 밖 글줄 흔적(붉은 점선)."""
    def style(fr):
        x0, y0, x1, y1 = fr
        return f"left:{x0 * 100:.2f}%;top:{y0 * 100:.2f}%;width:{max(0.3, (x1 - x0) * 100):.2f}%;height:{max(0.3, (y1 - y0) * 100):.2f}%"
    parts = []
    for reg in pr.regions:
        if len(reg.bbox) != 4:
            continue
        cls = "img-kind" if reg.kind in IMAGE_KINDS else ("title" if reg.kind in TITLE_KINDS else "")
        parts.append(f"<div class='box {cls}' style='{style(bbox_frac(reg.bbox))}'><span>{html.escape(reg.id)} {html.escape(reg.kind)}</span></div>")
    for i, fr in enumerate(orphan_boxes(pr), 1):
        parts.append(f"<div class='box orphan' style='{style(fr)}'><span>영역 밖 글줄 {i}</span></div>")
    return "".join(parts)


def make_review(work: Work, records: list[dict], out_path: str | None = None, context: dict | None = None) -> str:
    out = Path(out_path) if out_path else work.dir / "review.html"
    parts = [f"<!doctype html><html lang=ko><head><meta charset=utf-8><title>검수 · {html.escape(work.pdf_path.name)}</title><style>{CSS}</style></head><body>",
             f"<header><h1>연어OCR 검수 — {html.escape(work.pdf_path.name)}</h1><small>쪽 {len(records)}개 · 〔 〕 표시는 확인이 필요한 자리 · 수정은 readings/pNNN.json 을 고친 뒤 build 를 다시 실행</small>"
             "<div class=grades>글자 색(문서와 같음): <span class=g>초록</span> = 1차·2차가 갈려 3차 자형 판별로 고름(정밀도 미측정, 훑어 확인) · "
             "<span class=b>파랑</span> = 골랐지만 조각 위치를 확인 못 했거나 이 판독의 다른 대목과 어긋남 · <mark>〔 〕</mark> = 정하지 못함(원본 확인) · "
             "<mark>〔</mark><mark class=c>入</mark><mark>/人〕</mark> 실선 밑줄 = 이 판독의 다른 대목이 더 쓰는 후보(문맥 추정, 확정 아님)</div></header>"]
    from .grades import page_grades
    for rec in records:
        pr = PageReading.model_validate(rec["merged"])
        n = pr.pdf_page
        try:
            pg = page_grades(rec, (context or {}).get(n), "notice")
        except Exception:
            pg = None
        img = os.path.relpath(work.page_image_path(n), out.parent)
        sts = " ".join(f"<span>{html.escape(s)}</span>" for s in work.statuses(n))
        parts.append(f"<section class=page id=p{n}><div class=img><div class=sheet><img src='{img}' loading=lazy alt='PDF {n}쪽'>{overlay_boxes(pr)}</div>"
                     "<div class=legend>상자: 회색 = 문자 영역 · 파랑 = 도판 · 빨강 = 제목 · 붉은 점선 = 영역 밖 글줄 흔적(모델이 어느 영역에도 넣지 않음, 확인 필요)</div>")
        crops = rec.get("crops") or {}
        if crops:
            parts.append("<div class=crops>" + "".join(f"<figure><img src='{os.path.relpath(c['path'], out.parent)}' alt='{k}'><figcaption style='font-size:11px'>{k} {'⚠ 외곽 '+','.join(c['touching']) if c.get('touching') else '외곽 OK'}</figcaption></figure>" for k, c in crops.items()) + "</div>")
        parts.append("</div><div class=txt>")
        parts.append(f"<div class=meta><b>PDF {n}쪽</b> · 인쇄 {html.escape(pr.print_page) or '확인 안 됨'} · 쪽번호 표기 「{html.escape(pr.page_label)}」 · 머리말 「{html.escape(pr.running_header)}」 · 판독기 {html.escape(str(rec.get('reader')))} {html.escape(str(rec.get('model') or ''))} · 이미지 {html.escape(str(rec.get('image_source') or ''))}</div>")
        parts.append(f"<div class=status>{sts}</div>")
        if pr.page_note:
            parts.append(f"<div class=meta>메모: {html.escape(pr.page_note)}</div>")
        for reg in pr.regions:
            cls = "img-kind" if reg.kind in IMAGE_KINDS else ("title" if reg.kind in TITLE_KINDS else "")
            parts.append(f"<div class='reg {cls}'><span class=k>{html.escape(reg.kind)}</span><small>{reg.id} bbox {reg.bbox} {html.escape(reg.reading_direction)}</small>")
            if reg.kind in TITLE_KINDS:
                parts.append(f"<p><b>{_mark(reg.title_text)}</b></p>")
            elif reg.kind in TEXT_KINDS or reg.kind == "기타":
                for pi, p in enumerate(reg.paragraphs):
                    cols = "".join(f"<li><small>{c.col_from_right}열</small> {_mark(c.text)}</li>" for c in sorted(p.columns, key=lambda c: c.col_from_right))
                    sp = pg.spans.get((reg.id, pi), []) if pg is not None else []
                    parts.append(f"<p>{_graded(paragraph_text(p), sp)}</p><details><summary>열별 보기</summary><ol>{cols}</ol></details>")
                p2 = (rec.get("pass2") or {}).get(reg.id)
                if p2 and p2.get("columns"):
                    cols2 = "".join(f"<li><small>{c.get('col_from_right')}열</small> {html.escape(c.get('text', ''))}</li>" for c in sorted(p2["columns"], key=lambda c: c.get("col_from_right", 0)))
                    parts.append(f"<details><summary>2차 판독(확대 조각) 열 {len(p2['columns'])}개 — 비교 원본</summary><ol>{cols2}</ol></details>")
            elif reg.kind == "단순표":
                parts.append("<table border=1 cellpadding=3>" + "".join("<tr>" + "".join(f"<td>{_mark(c)}</td>" for c in r.cells) + "</tr>" for r in reg.table_rows) + "</table>")
            if reg.note:
                parts.append(f"<small>메모: {html.escape(reg.note)}</small>")
            parts.append("</div>")
        from .grades import classify_uncertain, page_body, decided_keys, SUSPECT_NOTE
        body_t, dec = page_body(pr), decided_keys(rec)
        tag = {"shown": "본문 표시", "decided": "3차 확정", "notice": "고지"}
        parts.append("<h4>판독 확인 필요 <small style='font-weight:normal'>(본문 표시 = 문서의 〔 〕 · 3차 확정 = 문서의 초록·파랑 · 고지 = 문서에 메모로만 · "
                     "합의 의심 = 1차·2차가 같게 읽었지만 자형 판별과 글자층이 다른 글자, 글은 판독대로 · 문서에는 그 글자에 메모)</small></h4><ul class=unc>" + ("".join(f"<li><small>[{'합의 의심' if u.context.startswith(SUSPECT_NOTE) else tag[classify_uncertain(u, body_t, dec)]}]</small> {html.escape(u.region_id)} {('오른쪽에서 '+str(u.col_from_right)+'번째 열 ') if u.col_from_right else ''}{html.escape(u.position)} {_mark(u.context)}</li>" for u in pr.uncertain) or "<li>없음</li>") + "</ul>")
        ver = rec.get("verify") or []
        ctx_sites = ((context or {}).get(n) or {}).get("sites") or []
        if ctx_sites:
            name = {"suggest": "후보 밑줄", "contradicts": "3차와 어긋남(파랑)", "agrees": "3차와 같음", "neutral": "근거 부족", "excluded": "대상 아님"}
            rows = "".join(f"<tr><td>{html.escape(x.get('region_id', ''))}</td><td>{html.escape(x['a'])}/{html.escape(x['b'])}</td>"
                           f"<td>{'3차 「' + html.escape(x['choice']) + '」' if x.get('state') == 'decided' else '미정'}</td><td>{name.get(x['verdict'], x['verdict'])}</td>"
                           f"<td>{html.escape(x.get('left', ''))}□{html.escape(x.get('right', ''))} · {html.escape(' · '.join(f'{k} {v}' for k, v in (x.get('score') or {}).items()))}</td>"
                           f"<td>{html.escape(x.get('reason', ''))}</td></tr>" for x in ctx_sites)
            parts.append(f"<details><summary>문맥 추정 {sum(1 for x in ctx_sites if x.get('verdict') == 'suggest')}곳 — 이 판독의 다른 대목(1차·2차가 같게 읽은 글)에서 센 수, 확정 아님</summary>"
                         f"<table class=ctx><tr><td>자리</td><td>후보</td><td>상태</td><td>판정</td><td>근거(앞□뒤 · 점수)</td><td>까닭</td></tr>{rows}</table></details>")
        if pg is not None and pg.page_notices:
            parts.append("<h4>고지</h4><ul class=unc>" + "".join(f"<li>{html.escape(t)}</li>" for t in pg.page_notices) + "</ul>")
        parts.append("<h4>구조 검사</h4><ul class=ver>" + ("".join(f"<li class={i['level']}>{html.escape(i['rule'])} ({html.escape(i['where'])}) — {html.escape(i['msg'])}</li>" for i in ver) or "<li>이상 없음</li>") + "</ul>")
        td = rec.get("textlayer_diff") or {}
        if td:
            parts.append(f"<details><summary>기존 OCR 레이어와의 차이 (일치율 {td.get('ratio')}, 판독 {td.get('reading_chars')}자 / 레이어 {td.get('layer_chars')}자) — 참고용, 자동 반영 안 함</summary><ul class=diff>" +
                         "".join(f"<li>{html.escape(i['op'])}: 판독 「{html.escape(i['reading'])}」 ↔ 레이어 「{html.escape(i['layer'])}」 <small>{html.escape(i['ctx'])}</small></li>" for i in td.get("items", [])) + "</ul></details>")
        parts.append("</div></section>")
    parts.append("</body></html>")
    out.write_text("\n".join(parts), encoding="utf-8")
    return str(out)
