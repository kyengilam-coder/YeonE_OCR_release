"""맛을 뜻하는 확실한 咸 용례만 鹹으로 정리한다."""
import re


def xian_usage(text, position):
    if text[position:position+1]!='咸':return 'review'
    left=''.join(text[:position].split());right=''.join(text[position+1:].split())
    if right.startswith(('陽','阳','寧','宁','豐','丰','平','淳','熙','池','亨','和')):
        return 'keep'
    if left.endswith(('姓','姓氏爲','姓氏為','姓氏为')) or re.search(r'(?:作者|姓名|名爲|名為|名为)[:：]?$',left):
        return 'keep'
    if right.startswith(('氏','先生','公','某')):return 'keep'
    for opening,closing in (('『','』'),('《','》'),('〈','〉')):
        if text.rfind(opening,0,position)>text.rfind(closing,0,position) and closing in text[position:]:return 'keep'
    # 味甘、咸 / 味咸 / 甘咸溫潤 are taste descriptions. A preceding
    # 甘 alone is insufficient without the medicinal property that follows.
    tastes='甘苦辛酸淡澀涩鹹咸'
    separators=r'[、，,·・/和兼而]*'
    if re.search(rf'味(?:[{tastes}]{separators})*$',left):return 'auto'
    if re.search(r'[甘苦辛酸淡澀涩][、，,·・]*$',left) and re.match(r'^[、，,·・]*(?:溫|温|寒|涼|凉|潤|润|燥|無毒|无毒|入腎|入肾)',right):return 'auto'
    if right.startswith(('味','能軟堅','能软坚','以軟堅','以软坚','入腎','入肾','走血')):return 'auto'
    return 'review'
