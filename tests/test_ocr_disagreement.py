from yeone_ocr.local_reading import reading_from_run
from yeone_ocr.ocr_disagreement import narrow_saved_grades, unsupported_disagreement


def test_displaced_dissent_keeps_real_substitution_and_small_omission_marked():
    base = '而清虛熱，補而不滯'
    agree = {'glmocr': base, 'rapidocr': '而清虚热 补而不滞'}
    assert unsupported_disagreement(base, dict(agree, tesseract='RAY NA EN RANDOM TEXT'))
    assert not unsupported_disagreement(base, dict(agree, tesseract='而清虛熱，補而不泄'))
    assert not unsupported_disagreement(base, dict(agree, tesseract='而清虛熱，補不滯'))
    assert not unsupported_disagreement(base, {'glmocr': base, 'tesseract': 'other'})


def test_saved_record_replay_preserves_other_highlights_and_ai_objections():
    text = '而清虛熱，補而不滯\n次句'
    raw = {'text': text, 'image_width': 100, 'image_height': 100, 'words': []}
    record = {'merged': reading_from_run(raw, 1, 'horizontal').model_dump(), 'local_grades': [
        {'region_id':'local-line-1', 'para':0, 'start':0, 'end':9, 'grade':'red'},
        {'region_id':'local-line-2', 'para':0, 'start':0, 'end':2, 'grade':'red'},
    ]}
    page = {'baseline_run_id':'base', 'runs':[{'id':'base', 'raw':raw}], 'suggestions':[
        {'decision':'pending', 'kind':'difference', 'start':0, 'end':9,
         'candidates':{'glmocr':text[:9], 'rapidocr':'而清虚热 补而不滞', 'tesseract':'RANDOM SHIFTED LINE'}},
    ]}
    updated = narrow_saved_grades(record, page)
    assert updated['local_grades'] == record['local_grades'][1:]
    assert updated['_narrowed_local_disagreements'] == 8
    assert len(record['local_grades']) == 2
    assert narrow_saved_grades(record, dict(page, ai_reviews=[{'proposals':[{}]}])) is record
