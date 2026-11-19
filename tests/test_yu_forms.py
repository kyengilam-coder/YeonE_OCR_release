import copy
import pytest
from yeone_ocr.blocks import Block
from yeone_ocr.hanja_rules import default_hanja_rules,validate_hanja_rules
from yeone_ocr.normalization import NormalizationOptions,normalize_text,normalize_blocks


def options(**kw):
    return NormalizationOptions(punctuation=False,yun=False,**kw)


@pytest.mark.parametrize('before,after',[
    ('五臟取決于膽','五臟取決於膽'),('其功專于利水','其功專於利水'),
    ('無逾于此','無逾於此'),('邪氣干于其間','邪氣干於其間'),
    ('生于北京','生於北京'),('用于臨床','用於臨牀'),('取之于民','取之於民'),
    ('于此可見','於此可見'),('取 決 于 膽','取 決 於 膽')])
def test_ordinary_usage_is_converted_without_AI(before,after):
    assert normalize_text(before,options=options()).text==after
    assert normalize_text(before,options=options(hanja_rules=default_hanja_rules())).text==after


@pytest.mark.parametrize('name',['于謙','于右任','于成龍','于闐','于田','淳于意','鮮于樞','單于',
                               '于先生','于某','姓于','于思明先生','《于氏醫案》','于歸','于飛','作者：于中平'])
def test_names_and_fixed_expressions_are_preserved_even_with_resolver(name):
    original='見'+name
    result=normalize_text(original,options=options(),resolver=lambda items:{i['id']:'於' for i in items})
    assert result.text==original
    assert not any(p.get('source')=='于' for p in result.proposals)


def test_preposition_before_a_surname_and_uncertain_use():
    assert normalize_text('至于于謙',options=options()).text=='至於于謙'
    assert normalize_text('于某生于北京',options=options()).text=='于某生於北京'
    # No rule pretends that arbitrary names can be recognized from one glyph.
    uncertain=normalize_text('甲于乙',options=options())
    assert uncertain.text=='甲于乙' and uncertain.review
    assert normalize_text('于中平',options=options()).text=='于中平'


def test_delete_or_disable_rule_and_preserve_original_evidence():
    rules=[r for r in default_hanja_rules() if r['find']!='于']
    assert normalize_text('無逾于此',options=options(hanja_rules=rules)).text=='無逾于此'
    assert normalize_text('無逾于此',options=options(korean_forms=False)).text=='無逾于此'
    block=Block(type='para',text='取決于膽',spans=[(3,4,'red')]);before=copy.deepcopy(block)
    output,report=normalize_blocks([block],options=options())
    assert block==before and output[0].text=='取決於膽' and (3,4,'red') in output[0].spans
    assert any(c['stage']=='yu_context' for c in report['changes'])
    with pytest.raises(ValueError):validate_hanja_rules([dict(find='字',replace='於',mode='yu')])
