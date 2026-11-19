"""Evidence-based AI priority for output grades, preserving recognition history."""
import copy,difflib,hashlib,json
from pathlib import Path
from .collate.zh import script_fold,script_pair


def confirmed_positions(current,answer,red=()):
    # Punctuation typography must not break an otherwise identical phrase.
    # Only letters/numbers gain support here; punctuation keeps its own review.
    positions=[i for i,c in enumerate(current) if c.isalnum()]
    mine=''.join(current[i] for i in positions);theirs=''.join(c for c in answer if c.isalnum())
    blocked={i for a,b in red for i in range(a,b)};confirmed=set()
    matcher=difflib.SequenceMatcher(None,script_fold(mine),script_fold(theirs),autojunk=False)
    if matcher.ratio()<.7:return set()
    for a,b,size in matcher.get_matching_blocks():
        # A few coincidental characters inside a displaced paragraph are not
        # evidence of correspondence. Require a substantial aligned phrase.
        if size<8:continue
        for k in range(size):
            i=positions[a+k];x=mine[a+k];y=theirs[b+k]
            if i not in blocked and (x==y or script_pair(x,y)):confirmed.add(i)
    return confirmed


def supported_grades(grades,current,confirmed):
    """Only replace a local red overlay; existing green/blue remain intact."""
    lines=current.split('\n');offsets=[];offset=0
    for line in lines:offsets.append(offset);offset+=len(line)+1
    result=[]
    for item in grades:
        if item.get('grade')!='red' or not item.get('region_id','').startswith('local-line-'):
            result.append(copy.deepcopy(item));continue
        try:index=int(item['region_id'].rsplit('-',1)[1])-1;base=offsets[index]
        except (ValueError,IndexError):result.append(copy.deepcopy(item));continue
        start=item['start'];kind=None
        for i in range(start,item['end']+1):
            value=('ai_supported' if base+i in confirmed else 'red') if i<item['end'] else None
            if value!=kind:
                if kind:result.append(dict(item,start=start,end=i,grade=kind))
                start=i;kind=value
    return result


def cached_priority(record,page,reference,inventory):
    from .ai_page import merge
    from .comparison import ComparisonProject,ComparisonError
    from .schema import PageReading,paragraph_text
    first=record.get('local_first') or {};summary=first.get('ai_page') or {}
    if (not summary.get('used') or summary.get('mode')!='prefer' or summary.get('ratio',0)<.7
            or page.get('corrected_text') is not None or page.get('edits') or page.get('decisions') or page.get('ai_decisions')
            or record.get('_output_catalog_recovery')):return record
    baseline=next((r for r in page['runs'] if r['id']==page.get('baseline_run_id')),None)
    if baseline is None:return record
    reading=PageReading.model_validate(record['merged'])
    current='\n'.join(paragraph_text(p) for r in reading.regions for p in r.paragraphs)
    compact=''.join(current.split());folded=''.join(c for c in script_fold(compact) if c.isalnum())
    folder=Path(reference).parent.parent/'ai-page';key=str(folder)
    if key not in inventory:
        candidates=[]
        for path in folder.glob('*.json'):
            try:data=json.loads(path.read_text(encoding='utf-8'))
            except (OSError,ValueError):continue
            if isinstance(data,dict) and isinstance(data.get('text'),str):
                candidates.append((path,data,''.join(c for c in script_fold(data['text']) if c.isalnum())))
        inventory[key]=candidates
    anchors=[folded[int(len(folded)*fraction):int(len(folded)*fraction)+16] for fraction in (.15,.35,.55,.75)]
    matches=[]
    for path,answer,fold in inventory[key]:
        if answer.get('model')!=summary.get('model') or sum(a in fold for a in anchors if len(a)==16)<2:continue
        value,red,blue,replayed=merge(baseline['raw']['text'],answer['text'],'prefer',uncertain=answer.get('uncertain',()))
        if ''.join(value.split())!=compact or not all(summary.get(k)==v for k,v in replayed.items()):continue
        # Map the replayed red evidence into the unchanged visible text.
        replay_positions=[i for i,c in enumerate(value) if not c.isspace()]
        current_positions=[i for i,c in enumerate(current) if not c.isspace()]
        blocked={current_positions[j] for j,i in enumerate(replay_positions) if any(a<=i<b for a,b in red)}
        confirmed=confirmed_positions(current,answer['text'])-blocked
        matches.append((path,answer,confirmed))
    if not matches or len({m[1]['text'] for m in matches})!=1:return record
    path,answer,confirmed=matches[0]
    # Human and selective-AI objections have priority over broad page agreement.
    for review in page.get('ai_reviews',()):
        snapshot=(review.get('request') or {}).get('snapshot_text')
        if not isinstance(snapshot,str):continue
        for proposal in review.get('proposals',()):
            try:a,b=ComparisonProject._mapped_span(snapshot,current,proposal['start'],proposal['end'])
            except (ComparisonError,KeyError):continue
            confirmed.difference_update(range(a,b))
    out=copy.deepcopy(record)
    out['local_grades']=supported_grades(record.get('local_grades',()),current,confirmed)
    out['_ai_priority']={'policy':'completed-ai-aligned-evidence-v1','cached_file':path.name,
                         'response_sha256':hashlib.sha256(answer['text'].encode()).hexdigest(),
                         'supported_characters':len(confirmed),'new_calls':0,'saved_result_exactly_replayed':True}
    return out
