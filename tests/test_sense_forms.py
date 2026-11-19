from yeone_ocr.hanja_rules import default_hanja_rules,rule_group,validate_hanja_rules
from yeone_ocr.normalization import normalize_text,NormalizationOptions
from yeone_ocr.sense_forms import sense_choice,EXAMPLES
import pytest


@pytest.mark.parametrize('rules',[None,default_hanja_rules()])
def test_herb_shu_uses_medical_context_but_keeps_skill_terms(rules):
    source = '陳嘉謨 術雖二種，補脾燥濕，功用皆同。白术、蒼術。醫術之精。古法之術。'
    result = normalize_text(source, options=NormalizationOptions(hanja_rules=rules, punctuation=False, yun=False))
    assert result.text == '陳嘉謨 朮雖二種，補脾燥濕，功用皆同。白朮、蒼朮。醫術之精。古法之術。'
    assert [p['source'] for p in result.proposals if p['stage']=='atractylodes_context'] == ['術']


@pytest.mark.parametrize('rules',[None,default_hanja_rules()])
def test_remainder_uses_context_for_both_change_and_preservation(rules):
    source='有余。十余年。余曰。余氏。'
    options=NormalizationOptions(hanja_rules=rules,punctuation=False,yun=False)
    pending=normalize_text(source,options=options)
    assert pending.text==source
    assert len(pending.review)==4
    requests=[]
    def choose(items):
        requests.extend(items)
        return {item['id']:choice for item,choice in zip(items,['餘','餘','余','余'])}
    result=normalize_text(source,resolver=choose,options=options)
    assert result.text=='有餘。十餘年。余曰。余氏。'
    assert len(requests)==4
    assert all(set(item['candidates'])=={'余','餘'} for item in requests)
    assert all(item['stage']=='remainder_context' for item in pending.proposals)


@pytest.mark.parametrize('text,expected',[
    ('根中虛松竅大者','根中虛鬆竅大者'),('其體极松','其體極鬆'),
    ('此藥質极黏膩','此藥質極黏膩'),
    ('土旺則能胜溼','土旺則能胜濕'),
    ('質地疏松','質地疏鬆'),('根中稍松','根中稍鬆'),
    ('松樹松柏松脂松子松香','松樹松柏松脂松子松香'),
    ('咽干口干，干涉干戈','咽乾口乾，干涉干戈'),
    ('成根荄枝干','成根荄枝幹'),('樹干莖干','樹幹莖幹'),
    ('着作，顯着，着衣着火附着','著作，顯著，着衣着火附着')])
def test_semantic_senses_without_external_calls(text,expected):
    for rules in (None,default_hanja_rules()):
        # Identity converter isolates sense selection from separate script forms.
        result=normalize_text(text,converter=str,options=NormalizationOptions(hanja_rules=rules,punctuation=False,yun=False))
        assert result.text==expected


@pytest.mark.parametrize('rules',[None,default_hanja_rules()])
def test_sheng_always_requires_context_and_accepts_either_character(rules):
    source='土旺則能胜溼。此處胜字須辨其義。'
    options=NormalizationOptions(hanja_rules=rules,punctuation=False,yun=False)
    pending=normalize_text(source,options=options)
    assert pending.text=='土旺則能胜濕。此處胜字須辨其義。'
    assert len(pending.review)==2
    requests=[]
    def choose(items):
        requests.extend(items)
        return {item['id']:choice for item,choice in zip(items,['勝','胜'])}
    result=normalize_text(source,resolver=choose,options=options)
    assert result.text=='土旺則能勝濕。此處胜字須辨其義。'
    assert len(requests)==2
    assert all(set(item['candidates'])=={'胜','勝'} for item in requests)
    assert all(item['original_context']==source for item in requests)
    invalid=normalize_text(source,resolver=lambda items:{i['id']:'陌' for i in items},options=options)
    assert invalid.text==pending.text and len(invalid.review)==2


def test_unknown_senses_use_full_context_and_do_not_blanket_replace():
    source='此物松。此物干。此物着。'
    pending=normalize_text(source,converter=str,options=NormalizationOptions(punctuation=False,yun=False))
    assert pending.text==source and len(pending.review)==3
    requests=[]
    def choose(items):
        requests.extend(items)
        return {i['id']:{'松':'鬆','干':'幹','着':'著'}[i['source']] for i in items}
    result=normalize_text(source,converter=str,resolver=choose,options=NormalizationOptions(punctuation=False,yun=False))
    assert result.text=='此物鬆。此物幹。此物著。'
    assert len(requests)==3
    assert all(i['original_context']==source for i in requests)
    assert set(requests[1]['candidates'])=={'干','乾','幹'}


def test_conflicting_compounds_are_held_and_invalid_model_choice_is_rejected():
    # 枝干燥 could be a branch followed by dry, or a trunk; do not force 幹 or 乾.
    assert sense_choice('枝干燥',1,'gan')[0] is None
    result=normalize_text('枝干燥',converter=str,resolver=lambda rows:{i['id']:'陌' for i in rows},
                          options=NormalizationOptions(yun=False,punctuation=False))
    assert result.text=='枝干燥' and result.review


def test_rules_all_have_one_group_and_saved_user_modes_can_be_disabled():
    rows=default_hanja_rules()
    assert all(rule_group(r) in ('문맥 판별 규칙','자동 치환 규칙') for r in rows)
    modes={r['mode'] for r in rows if rule_group(r)=='문맥 판별 규칙'}
    assert {'yun','song','gan','zhu','yu','xian','leaf','review'}<=modes
    assert set(EXAMPLES)>={'song','gan','zhu'}
    for mode,source in [('song','根中虛松'),('gan','咽干'),('zhu','着作'),('yun','古人云')]:
        rules=[r for r in rows if r['mode']!=mode]
        result=normalize_text(source,converter=str,options=NormalizationOptions(hanja_rules=rules,punctuation=False))
        assert result.text==source
        assert not any(p['stage']==mode+'_context' for p in result.proposals)
    with pytest.raises(ValueError):validate_hanja_rules([dict(find='松',replace='幹',mode='song')])


def test_shi_is_editable_unconditional_glyph_rule():
    rule=next(r for r in default_hanja_rules() if r['find']=='溼')
    assert rule['replace']=='濕' and rule['mode']=='auto'
    assert normalize_text('風溼，溼熱，溼氣').text=='風濕, 濕熱, 濕氣'


@pytest.mark.parametrize('policy',['post1900','vernacular'])
def test_modern_book_policy_preserves_yu_even_with_auto_rule_or_model(policy):
    source='用于治病。于姓。'
    for rules in (None,default_hanja_rules(),[dict(find='于',replace='於',mode='auto')]):
        requests=[]
        def model(items):
            requests.extend(items)
            return {i['id']:'於' for i in items}
        out=normalize_text(source,resolver=model,options=NormalizationOptions(hanja_rules=rules,yu_policy=policy,punctuation=False,yun=False))
        assert out.text==source
        assert not any(i['source']=='于' for i in requests)


def test_book_yu_policy_roundtrip_and_cli_gui_forwarding(tmp_path):
    from types import SimpleNamespace
    from yeone_ocr.state import save_book_settings,load_book_settings
    from yeone_ocr import cli,gui
    save_book_settings(tmp_path,{'yu_policy':'post1900'},create=True)
    assert load_book_settings(tmp_path)['yu_policy']=='post1900'
    for policy in ('post1900','vernacular'):
        app=SimpleNamespace(v_yu_policy=SimpleNamespace(get=lambda:gui.YU_POLICY_LABELS[policy]))
        flags=gui.App._normalization_opts(app)
        assert flags==['--yu-policy',policy]
        args=cli.build_parser().parse_args(['build','book.pdf',*flags])
        assert cli.normalization_options(args).yu_policy==policy
