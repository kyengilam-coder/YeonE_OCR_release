"""Output-only punctuation evidence; recognition and decisions stay untouched."""
import re


def compact_punctuation(text):
    return re.sub(r'\s','',text).translate(str.maketrans({'，':',','﹐':',','；':';','﹔':';'}))


def cosmetic_comma_offsets(base,values):
    """Prove comma agreement in a mixed glyph/spacing difference.

    Equal lengths after whitespace removal permit position-by-position proof.
    Insertions/deletions and different punctuation remain review candidates.
    """
    if len(values)<2:return set()
    canonical=[compact_punctuation(v) for v in values];target=compact_punctuation(base)
    if not target or any(len(v)!=len(target) for v in canonical):return set()
    positions=[i for i,c in enumerate(base) if not c.isspace()]
    return {positions[i] for i,c in enumerate(target) if c in ',;' and all(v[i]==c for v in canonical)}


def annotate_cosmetic_commas(record,page):
    import copy
    import difflib
    from .schema import PageReading,paragraph_text
    baseline=next((r for r in page['runs'] if r['id']==page.get('baseline_run_id')),None)
    if baseline is None or record.get('_output_catalog_recovery'):return record
    reading=PageReading.model_validate(record['merged'])
    lines=[(r.id,i,paragraph_text(p)) for r in reading.regions for i,p in enumerate(r.paragraphs)]
    current='\n'.join(t for _,_,t in lines);base=baseline['raw']['text'];clear=set();blocked=set()
    matches={}
    def map_span(origin,a,b):
        if origin==current:return a,b
        if origin not in matches:matches[origin]=difflib.SequenceMatcher(None,origin,current,autojunk=False).get_matching_blocks()
        for i,j,n in matches[origin]:
            if a<b and i<=a and b<=i+n:return j+a-i,j+b-i
        return None
    for suggestion in page.get('suggestions',[]):
        if suggestion.get('decision')!='pending':continue
        a,b=suggestion['start'],suggestion['end'];values=list(suggestion.get('candidates',{}).values())
        for offset in cosmetic_comma_offsets(base[a:b],values):
            span=map_span(base,a+offset,a+offset+1)
            if span:clear.update(range(*span))
    # Pending AI proposals are independent evidence: keep their uncertainty.
    for review in page.get('ai_reviews',[]):
        snapshot=(review.get('request') or {}).get('snapshot_text')
        if not isinstance(snapshot,str):continue
        for proposal in review.get('proposals',[]):
            a,b=proposal.get('start'),proposal.get('end')
            if not isinstance(a,int) or not isinstance(b,int):continue
            span=map_span(snapshot,a,b)
            if span:blocked.update(range(*span))
    clear-=blocked
    if not clear:return record
    result=copy.deepcopy(record);spans=[];offset=0
    for region,para,text in lines:
        for i,c in enumerate(text):
            if offset+i in clear and c in ',，﹐;；﹔':spans.append(dict(region_id=region,para=para,start=i,end=i+1))
        offset+=len(text)+1
    result['_cosmetic_punctuation']=spans
    return result
