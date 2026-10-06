"""검수용 정적 HTML. 왼쪽 원본 쪽 이미지, 오른쪽 판독문·확인 필요 목록·기존 OCR 과의 차이·구조 검사 결과."""
from __future__ import annotations
import html, os
from pathlib import Path
from .state import Work
from .schema import PageReading, TEXT_KINDS, TITLE_KINDS, IMAGE_KINDS, paragraph_text

CSS = """
body{font-family:'Noto Serif KR','Batang','NanumMyeongjo',serif;margin:0;background:#f4efe4;color:#231c18}
header{padding:12px 20px;background:#3b1b14;color:#f1e7cf;display:flex;gap:16px;align-items:baseline;flex-wrap:wrap}
header h1{font-size:18px;margin:0}header small{opacity:.8}
.page{display:grid;grid-template-columns:minmax(280px,42%) 1fr;gap:16px;padding:16px 20px;border-top:3px solid #b99552;background:#faf6ec}
.page:nth-child(even){background:#f1e9d6}
.img img{width:100%;height:auto;border:1px solid #c9b88a;background:#fff}
.img .crops{display:flex;flex-wrap:wrap;gap:8px;margin-top:8px}.img .crops img{width:120px}
.meta{font-size:13px;color:#5a4a3e;margin-bottom:8px}.meta b{color:#c1341f}
.reg{margin:8px 0;padding:8px 10px;border-left:4px solid #b99552;background:rgba(255,255,255,.55);border-radius:4px}
.reg .k{font-size:12px;color:#fff;background:#5a4a3e;border-radius:3px;padding:1px 6px;margin-right:6px}
.reg.img-kind{border-left-color:#1f5fbf}.reg.title{border-left-color:#c1341f}
.reg p{margin:4px 0;line-height:1.75;font-size:15px;white-space:pre-wrap}
mark{background:#ffd9a8;padding:0 2px}
.unc li,.diff li,.ver li{font-size:13px;margin:2px 0}
.diff{columns:2;column-gap:16px}.ver .error{color:#b3261e}
.status span{display:inline-block;font-size:12px;background:#e4d7b4;border-radius:10px;padding:1px 8px;margin:2px}
details summary{cursor:pointer;font-size:13px;color:#5a4a3e}
"""


def _mark(t: str) -> str:
    t = html.escape(t)
    return t.replace("〔", "<mark>〔").replace("〕", "〕</mark>")


def make_review(work: Work, records: list[dict], out_path: str | None = None) -> str:
    out = Path(out_path) if out_path else work.dir / "review.html"
    parts = [f"<!doctype html><html lang=ko><head><meta charset=utf-8><title>검수 · {html.escape(work.pdf_path.name)}</title><style>{CSS}</style></head><body>",
             f"<header><h1>연어OCR 검수 — {html.escape(work.pdf_path.name)}</h1><small>쪽 {len(records)}개 · 〔 〕 표시는 확인이 필요한 자리 · 수정은 readings/pNNN.json 을 고친 뒤 build 를 다시 실행</small></header>"]
    for rec in records:
        pr = PageReading.model_validate(rec["merged"])
        n = pr.pdf_page
        img = os.path.relpath(work.page_image_path(n), out.parent)
        sts = " ".join(f"<span>{html.escape(s)}</span>" for s in work.statuses(n))
        parts.append(f"<section class=page id=p{n}><div class=img><img src='{img}' loading=lazy alt='PDF {n}쪽'>")
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
                for p in reg.paragraphs:
                    cols = "".join(f"<li><small>{c.col_from_right}열</small> {_mark(c.text)}</li>" for c in sorted(p.columns, key=lambda c: c.col_from_right))
                    parts.append(f"<p>{_mark(paragraph_text(p))}</p><details><summary>열별 보기</summary><ol>{cols}</ol></details>")
            elif reg.kind == "단순표":
                parts.append("<table border=1 cellpadding=3>" + "".join("<tr>" + "".join(f"<td>{_mark(c)}</td>" for c in r.cells) + "</tr>" for r in reg.table_rows) + "</table>")
            if reg.note:
                parts.append(f"<small>메모: {html.escape(reg.note)}</small>")
            parts.append("</div>")
        parts.append("<h4>판독 확인 필요</h4><ul class=unc>" + ("".join(f"<li>{html.escape(u.region_id)} {('오른쪽에서 '+str(u.col_from_right)+'번째 열 ') if u.col_from_right else ''}{html.escape(u.position)} {_mark(u.context)}</li>" for u in pr.uncertain) or "<li>없음</li>") + "</ul>")
        ver = rec.get("verify") or []
        parts.append("<h4>구조 검사</h4><ul class=ver>" + ("".join(f"<li class={i['level']}>{html.escape(i['rule'])} ({html.escape(i['where'])}) — {html.escape(i['msg'])}</li>" for i in ver) or "<li>이상 없음</li>") + "</ul>")
        td = rec.get("textlayer_diff") or {}
        if td:
            parts.append(f"<details><summary>기존 OCR 레이어와의 차이 (일치율 {td.get('ratio')}, 판독 {td.get('reading_chars')}자 / 레이어 {td.get('layer_chars')}자) — 참고용, 자동 반영 안 함</summary><ul class=diff>" +
                         "".join(f"<li>{html.escape(i['op'])}: 판독 「{html.escape(i['reading'])}」 ↔ 레이어 「{html.escape(i['layer'])}」 <small>{html.escape(i['ctx'])}</small></li>" for i in td.get("items", [])) + "</ul></details>")
        parts.append("</div></section>")
    parts.append("</body></html>")
    out.write_text("\n".join(parts), encoding="utf-8")
    return str(out)
