"""于의 문법 용례만 정리하고 이름·옛 고정 표현은 보존한다."""
import re

# 이름으로 확인되는 어휘는 문법 접두어보다 먼저 보호한다. 목록 밖의
# 불확실한 용례도 강제로 치환하지 않고 기존 문맥 확인 경로로 남긴다.
NAMES = ('于謙','于谦','于右任','于禁','于吉','于成龍','于成龙','于慎行',
         '于敏','于濆','于頔','于鵠','于鹄','于闐','于阗','于田','于都','于洪')
FIXED = ('于歸','于归','于飛','于飞','于嗟','于以')
LEFT = re.compile(r'(?:由|對|对|關|关|至|在|出|生|用|入|歸|归|屬|属|寄|寓|存|取|決|决|專|专|逾|干|施|見|见|始|終|终|起|止|異|异|同|合|勝|胜|優|优|求|取之|用之)$')
RIGHT = re.compile(r'^(?:(?:此|彼|是)(?:可|則|则|而|有|無|无|爲|為|为|見|见|得|所|不|足|能|已|既)|其(?:中|間|间)|\d)')
PAIRS = (('《','》'),('『','』'),('〈','〉'))


def yu_usage(text, position):
    """auto=확실한 문법 연결, keep=이름/고정 표현, review=확정 불가."""
    if text[position:position+1] != '于':
        return 'review'
    # OCR이 한자 사이에 끼운 공백은 판단에만 제거하고 원문 위치는 보존한다.
    left=''.join(text[:position].split());right=''.join(text[position:].split())
    if left.endswith(('淳','鮮','鲜','單','单')) or right.startswith(NAMES+FIXED):
        return 'keep'
    if (left.endswith(('姓','姓氏爲','姓氏为','姓氏為'))
            or re.search(r'(?:姓名|作者|編者|编者|醫家|医家|名爲|名為|名为)[：:]?$',left)
            or re.match(r'^于(?:氏|姓|公|某|先生|女士|教授|將軍|将军|太守|醫師|医师|醫生|医生)',right)):
        return 'keep'
    if re.match(r'^于[\u3400-\u9fff]{1,3}(?:先生|女士|教授|氏|著|撰|編著|编著)',right):
        return 'keep'
    for opening,closing in PAIRS:
        if text.rfind(opening,0,position)>text.rfind(closing,0,position) and closing in text[position:]:
            return 'keep'
    if len(right)>1 and (LEFT.search(left) or RIGHT.match(right[1:])):
        return 'auto'
    return 'review'
