"""Conservative semantic hints for loose tissue/pine and dry/other 干 senses.

Clear local evidence may settle a sense. Unknown or conflicting evidence goes
to the existing full-context resolver; hints are never a blanket substitution.
"""
import re

PAIRS={'song':('松','鬆'),'gan':('干','乾','幹'),'zhu':('着','著'),'ji':('极','極'),'sheng':('胜','勝'),'remainder':('余','餘'),'atractylodes':('術','朮')}
EXAMPLES={'song':(('根中虛松竅大者','根中虛鬆竅大者'),('其體极松','其體极鬆'),('松柏','松柏')),
          'gan':(('咽干','咽乾'),('成根荄枝干','成根荄枝幹'),('干姜','乾薑'),('干涉','干涉')),
          'zhu':(('着作','著作'),('着衣','着衣')),
          'ji':(('此藥質极黏膩','此藥質極黏膩'),('其體极松','其體極鬆')),
          'sheng':(('土旺則能胜溼','土旺則能勝濕'),),
          'remainder':(('有余','有餘'),('十余年','十餘年'),('余曰','余曰'),('余氏','余氏')),
          'atractylodes':(('術雖二種，補脾燥濕，功用皆同','朮雖二種，補脾燥濕，功用皆同'),('白術','白朮'),('醫術','醫術'))}


def _covers(text,position,words):
    return [word for word in words if any(m.start()<=position<m.end() for m in re.finditer(re.escape(word),text))]


def sense_choice(text,position,mode):
    if mode=='remainder':
        return None,'余/餘: 남음·나머지·수량의 초과는 餘, 나(1인칭)·성씨·인명은 余. 전체 문맥에서 판별하며 불확실하면 원문 보존'
    if mode=='sheng':
        return None,'胜/勝: 이 문헌에서 胜은 거의 대부분 勝의 뜻으로 쓰입니다. 이를 참고하되 인접 글자만으로 확정하지 말고 제공된 전체 문맥에서 胜 또는 勝을 판별하십시오. 제어·극복·감당은 勝, 胜 자체의 뜻이면 胜을 유지하며 불확실하면 원문을 보존합니다.'
    # Do not cross clauses or delete internal spaces to invent a compound.
    left=max((text.rfind(c,0,position) for c in '。.!?！？;；\n'),default=-1)+1
    right=min((v for c in '。.!?！？;；\n' if (v:=text.find(c,position))>=0),default=len(text))
    clause=text[left:right];at=position-left
    if mode=='atractylodes':
        term = clause[:at] + '術' + clause[at+1:]
        if _covers(term,at,('醫術','医術','方術','技術','學術','学術','手術','法術','算術','藝術','道術','治術','術數','術数','術业','術業')):
            return '術','의술·기술·방법을 뜻하는 용례'
        if _covers(term,at,('白術','蒼術','苍術','於術','于術')):
            return '朮','본초 약재명'
        after=clause[at+1:]
        if re.match(r'(?:雖|虽|有|分)\s*(?:二|兩|两)\s*(?:種|种)',after) and re.search(
                r'補脾|补脾|健脾|燥[濕湿]|白[術朮术]|[蒼苍][術朮术]',clause):
            return '朮','백출·창출 두 종류와 약효를 비교하는 문맥'
        return None,'術/朮: 본초서에서 약재 백출·창출 또는 그 총칭이면 朮을 우선 고려하되, 의술·기술·방법은 術입니다. 앞뒤 문맥을 함께 보고 불확실하면 원문을 보존합니다.'
    if mode=='ji':
        if re.match(r'^(?:黏膩|粘膩|黏腻|粘腻|松|鬆|寒|熱|热|苦|酸|澀|涩|燥|薄|厚|強|强|弱|細|细|微|輕|轻|重|大|小)',clause[at+1:]):
            return '極','성상·성미의 정도가 매우 심함을 나타내는 문맥'
        return None,'极/極의 전체 문맥 판별 필요'
    if mode=='zhu':
        keep=_covers(clause,at,('着衣','着火','着地','着陸','着陆','着手','着眼','執着','执着','附着','黏着'))
        change=_covers(clause,at,('着作','着書','着书','顯着','显着','卓着'))
        if keep and not change:return '着','입음·붙음·도달 등의 용례: '+', '.join(keep)
        if change and not keep:return '著','저술·드러남의 용례: '+', '.join(change)
        return None,'着(입다·붙다·도달)/著(저술·드러남)의 전체 문맥 판별 필요'
    if mode=='song':
        keep=_covers(clause,at,('松樹','松树','松柏','松脂','松香','松子','松仁','松花','松葉','松叶',
                              '松針','松针','松節','松节','松根','松實','松实','松枝','松木','松皮','赤松','馬尾松','马尾松'))
        change=_covers(clause,at,('疏松','虛松','虚松','松軟','松软','松脆','松散','蓬松','寬松','宽松',
                                '質松','质松','肉松','骨松'))
        if re.search(r'(?:根|莖|茎|葉|叶|組織|组织|質地|质地|肌肉|體|体)(?:中|內|内|部|質|质|體|体|頗|颇|甚|較|较|虛|虚|疏|而|則|则|性|皆|多|尤|稍|柔|輕|轻|燥|脆|極|极){1,5}$',clause[:at]):
            change.append('根·組織·質地의 성김을 서술하는 문맥')
        if re.search(r'(?:姓|姓名|名爲|名為|名为)[:：]?$',clause[:at]) or clause[at+1:].startswith(('先生','氏','某')):
            keep.append('인명·성씨 문맥')
        if keep and not change:return '松','소나무·약재명·인명 보존: '+', '.join(keep)
        if change and not keep:return '鬆','성기거나 느슨한 조직·성상: '+', '.join(change)
        return None,'松(소나무·이름)/鬆(성기다·느슨하다)의 전체 문맥 판별 필요'
    keep=_covers(clause,at,('干戈','干涉','干支','干將','干将','干寶','干宝','干犯','干預','干预','相干','若干','干擾','干扰'))
    change=_covers(clause,at,('咽干','口干','唇干','舌干','干姜','干薑','干燥','干枯','曬干','晒干','陰干','阴干','焙干','烘干','風干','风干','蒸干','煎干'))
    trunk=_covers(clause,at,('枝干','樹干','树干','莖干','茎干','根干'))
    if keep and not change and not trunk:return '干','건조 이외의 어휘·이름 보존: '+', '.join(keep)
    if change and not keep and not trunk:return '乾','목마름·건조·말리는 문맥: '+', '.join(change)
    if trunk and not change and not keep:return '幹','식물의 줄기·가지 문맥: '+', '.join(trunk)
    return None,'干/乾(마르다)/幹(줄기·일)의 전체 문맥 판별 필요'
