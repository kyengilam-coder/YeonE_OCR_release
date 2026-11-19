"""Persist list-layout decisions and source omissions beside an output HWPX."""
import html
import json
from pathlib import Path
from .state import write_atomic

REASONS={
    'missing-source-entry':'원본에는 있으나 출력 목록에서 찾지 못한 항목',
    'unmatched-entry':'원본 대응 확인 필요',
    'unresolved-source-leader':'원본 채움선 확인 필요',
    'default-leader':'원본 채움선 미확정으로 기본 점선 탭 적용',
    'default-leader-unmatched-entry':'인접 목록의 배치에 따라 기본 점선 탭 적용, 원본 대응 확인 필요',
    'unresolved-leader-or-tab-position':'채움선·탭 위치 확인 필요',
    'unresolved-column-geometry':'단 너비·간격 확인 필요',
    'source-reading-order-conflict':'읽기 순서 확인 필요',
    'source-entry-unmatched':'번호는 있으나 원본 항목 대응 확인 필요',
    'author-text-differs':'원본과 저자 표기 차이 확인 필요(글자는 유지)',
}


def write_list_review(output,stats):
    output=Path(output)
    report={'schema_version':1,'output':output.name,
            'summary':{k:v for k,v in stats.items() if k.startswith(('source_list_','source_bibliography_','toc_')) and not isinstance(v,list)},
            'findings':stats.get('source_list_findings',[])+stats.get('toc_findings',[]),
            'applied':stats.get('toc_correspondences',[])}
    json_path=output.with_suffix('.list-review.json')
    write_atomic(json_path,json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    rows=[]
    for item in report['findings']:
        page=str(item.get('source_page','—'))
        label=REASONS.get(item['reason'],item['reason'])
        text=item.get('text') or item.get('source_text','')
        rows.append('<tr>'+''.join('<td>'+html.escape(v)+'</td>' for v in (page,label,text))+'</tr>')
    content='''<!doctype html><html lang="ko"><meta charset="utf-8"><title>목록 정렬 확인</title>
<style>body{font-family:sans-serif;margin:2rem;line-height:1.6}table{border-collapse:collapse;width:100%}td,th{border:1px solid #ccc;padding:.6rem;text-align:left}th{background:#eee}</style>
<h1>목록 정렬 확인</h1>'''
    content+='<p>'+html.escape(output.name)+'</p>'
    content+='<p>확인 항목 '+str(len(rows))+'건. 쪽번호는 PDF 파일의 쪽번호입니다. 원본 대응이나 모양이 불확실한 항목은 글자를 보충하지 않고 확인 대상으로 남겼습니다.</p>'
    content+='<table><thead><tr><th>PDF 쪽</th><th>확인 사항</th><th>항목</th></tr></thead><tbody>'+''.join(rows)+'</tbody></table></html>'
    html_path=output.with_suffix('.list-review.html');write_atomic(html_path,content)
    return str(html_path),str(json_path)
