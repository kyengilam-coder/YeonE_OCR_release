import pytest
from yeone_ocr.hanja_rules import default_hanja_rules
from yeone_ocr.normalization import normalize_text,NormalizationOptions,normalize_blocks
from yeone_ocr.blocks import Block


@pytest.mark.parametrize('source,expected',[
    ('甘咸溫潤色黑, 專入肝腎','甘鹹溫潤色黑, 專入肝腎'),
    ('味咸，寒。','味鹹, 寒. '),('味甘、咸。','味甘·鹹. '),
    ('咸能軟堅','鹹能軟堅'),('咸陽人咸氏，眾人咸服。','咸陽人咸氏, 衆人咸服. '),
    ('『咸味書』咸寧，咸平。','『咸味書』咸寧, 咸平. '),
])
def test_taste_context_and_names(source,expected):
    for rules in (None,default_hanja_rules()):
        assert normalize_text(source,options=NormalizationOptions(hanja_rules=rules)).text==expected


def test_deleting_context_rule_and_review_evidence():
    rules=[r for r in default_hanja_rules() if r['mode']!='xian']
    assert '甘咸溫潤' in normalize_text('甘咸溫潤',options=NormalizationOptions(hanja_rules=rules)).text
    source=Block(type='para',text='甘咸溫潤',spans=[(1,2,'red')],memos=[(1,'검토')])
    out,_=normalize_blocks([source])
    assert out[0].text=='甘鹹溫潤' and (1,2,'red') in out[0].spans and out[0].memos==[(1,'검토')]
    assert source.text=='甘咸溫潤'
