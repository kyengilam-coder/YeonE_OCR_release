from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from yeone_ocr.hanja_rules import default_hanja_rules, validate_hanja_rules, save_hanja_rules, load_hanja_rules
from yeone_ocr.normalization import normalize_text, normalize_blocks, NormalizationOptions
from yeone_ocr.blocks import Block


def test_book_title_zhengyao_is_a_specific_editable_rule():
    text='『本草征要』，征戰與徵候。'
    for rules in (None,default_hanja_rules()):
        out=normalize_text(text,options=NormalizationOptions(hanja_rules=rules))
        assert out.text.startswith('『本草徵要』') and '征戰' in out.text
    rules=[r for r in default_hanja_rules() if r['find']!='本草征要']
    assert normalize_text(text,options=NormalizationOptions(hanja_rules=rules)).text.startswith('『本草征要』')


def test_paozhi_is_an_editable_phrase_not_global_zhi_replacement():
    for rules in (None,default_hanja_rules()):
        result=normalize_text('藥材炮制，制度。',options=NormalizationOptions(hanja_rules=rules))
        assert result.text=='藥材炮製, 制度. '
    rules=[r for r in default_hanja_rules() if r['find']!='炮制']
    assert normalize_text('炮制',options=NormalizationOptions(hanja_rules=rules)).text=='炮制'


def test_dried_ginger_phrase_covers_partial_forms_and_preserves_surname():
    for rules in (None,default_hanja_rules()):
        out=normalize_text('干姜，干薑，姜先生。',options=NormalizationOptions(hanja_rules=rules))
        assert out.text=='乾薑, 乾薑, 姜先生. '


def test_zang_is_an_editable_unconditional_user_rule():
    for rules in (None,default_hanja_rules()):
        out=normalize_text('五髒，髒腑，心髒。',options=NormalizationOptions(hanja_rules=rules))
        assert out.text=='五臟, 臟腑, 心臟. '
    rule=next(r for r in default_hanja_rules() if r['find']=='髒')
    assert rule['replace']=='臟' and rule['mode']=='auto'


def test_yangan_is_contextual_and_preserves_non_dry_gan():
    for rules in (None,default_hanja_rules()):
        assert normalize_text('咽干，干戈，干涉。',options=NormalizationOptions(hanja_rules=rules)).text=='咽乾, 干戈, 干涉. '
    rules=[r for r in default_hanja_rules() if r['mode']!='gan']
    assert normalize_text('咽干',options=NormalizationOptions(hanja_rules=rules)).text=='咽干'


def test_root_looseness_is_contextual_and_preserves_pine():
    for rules in (None,default_hanja_rules()):
        result=normalize_text('根中虛松竅大者，松樹，松柏。',options=NormalizationOptions(hanja_rules=rules))
        assert result.text=='根中虛鬆竅大者, 松樹, 松柏. '
    rules=[r for r in default_hanja_rules() if r['mode']!='song']
    assert normalize_text('根中虛松竅大者',options=NormalizationOptions(hanja_rules=rules)).text=='根中虛松竅大者'


def test_default_rules_are_organized_and_cover_user_examples():
    rules = default_hanja_rules()
    assert len({r['find'] for r in rules}) == len(rules)
    assert not any(r['find'] == r['replace'] for r in rules)
    by_source = {r['find']: r for r in rules}
    assert by_source['產']['replace'] == '産' and by_source['產']['mode'] == 'auto'
    assert by_source['叶']['mode'] == 'leaf'
    assert by_source['術']['mode'] == 'review'
    out = normalize_text('牛膝春夏用叶，經候胎產諸病，醫術。', options=NormalizationOptions(editorial_simplified=True, hanja_rules=rules))
    assert out.text == '牛膝春夏用葉, 經候胎産諸病, 醫術. '


def test_delete_modify_add_and_empty_rules_affect_actual_output():
    rules = [r for r in default_hanja_rules() if r['find'] != '產']
    assert normalize_text('胎產', options=NormalizationOptions(hanja_rules=rules)).text == '胎產'
    rules.append(dict(id=999_999, find='產', replace='産', mode='auto'))
    assert normalize_text('胎產', options=NormalizationOptions(hanja_rules=rules)).text == '胎産'
    rules[-1]['replace'] = '産業'
    source = Block(type='para', text='甲產乙', spans=[(2,3,'red')])
    out, _ = normalize_blocks([source], options=NormalizationOptions(hanja_rules=rules))
    assert out[0].text == '甲産業乙' and (3,4,'red') in out[0].spans
    assert source.text == '甲產乙' and source.spans == [(2,3,'red')]
    assert normalize_text('為', options=NormalizationOptions(hanja_rules=[])).text == '為'


def test_explicit_review_mode_is_not_overridden_by_builtin_glyph_table():
    rules = [dict(find='產', replace='産', mode='review')]
    result = normalize_text('胎產', options=NormalizationOptions(hanja_rules=rules))
    assert result.text == '胎產' and result.review
    result = normalize_text('胎產', options=NormalizationOptions(hanja_rules=rules), resolver=lambda items: {i['id']:'産' for i in items})
    assert result.text == '胎産'


def test_rule_file_roundtrip_and_original_project_protection(tmp_path):
    path = tmp_path/'rules.json'
    saved = save_hanja_rules(path, [dict(find='產', replace='産', mode='auto')])
    assert load_hanja_rules(saved)[0]['replace'] == '産'
    save_hanja_rules(path, [])
    assert load_hanja_rules(path) == []
    project = tmp_path/'existing.yeone-compare.json'; project.write_bytes(b'SQLite format 3\0'+b'x'*50)
    before = project.read_bytes()
    with pytest.raises(ValueError):save_hanja_rules(project, [])
    assert project.read_bytes() == before
    with pytest.raises(ValueError):save_hanja_rules(tmp_path/'original.msr', [])


@pytest.mark.parametrize('bad', [
    [{'find':'','replace':'字','mode':'auto'}],
    [{'find':'產','replace':'産','mode':'execute'}],
    [{'find':'產','replace':'産','script':'run()'}],
    [{'find':'產','replace':'産'},{'find':'產','replace':'生'}],
    [{'find':'，','replace':', ','mode':'auto'}],
    [{'find':'術','replace':'朮','mode':'leaf'}],
])
def test_invalid_rules_rejected(bad):
    with pytest.raises(ValueError):validate_hanja_rules(bad)


def test_cli_gui_and_disabled_options_use_the_selected_file(tmp_path):
    from yeone_ocr import cli, gui
    path = save_hanja_rules(tmp_path/'my.json',[dict(find='產',replace='産',mode='auto')])
    app = SimpleNamespace(v_hanja_rules=SimpleNamespace(get=lambda:str(path)))
    flags = gui.App._normalization_opts(app)
    assert flags == ['--hanja-rules',str(path)]
    args = cli.build_parser().parse_args(['build','x.pdf',*flags])
    assert cli.normalization_options(args).hanja_rules == load_hanja_rules(path)
    args = cli.build_parser().parse_args(['build','x.pdf','--no-normalize','--hanja-rules','missing.json'])
    assert cli.normalization_options(args).hanja_rules is None


def test_save_and_restore_is_literal_not_regex(tmp_path):
    path = save_hanja_rules(tmp_path/'literal.json',[dict(find='字.*',replace='新字',mode='auto')])
    options = NormalizationOptions(hanja_rules=load_hanja_rules(path),traditional=False,punctuation=False,yun=False)
    assert normalize_text('字ABC 字.*',options=options).text == '字ABC 新字'
