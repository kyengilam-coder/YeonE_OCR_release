from yeone_ocr.korean_forms import KOREAN_FORMS, apply_to_reading, korean_forms
from yeone_ocr.schema import Column, PageReading, Paragraph, Region


def _ks(char):
    try:
        char.encode("euc_kr")
        return True
    except UnicodeEncodeError:
        return False


def test_every_pair_maps_a_non_ks_form_to_a_ks_form():
    for source, target in KOREAN_FORMS.items():
        assert not _ks(source) and _ks(target), source + target


def test_sample_errors_and_korean_characters():
    assert korean_forms("説明 真實 絕對 緒論 離脱") == "說明 眞實 絶對 緖論 離脫"
    keep = "欠缺 余餘 山川 氣 한글 ABC"
    assert korean_forms(keep) == keep                       # forms a Korean book can carry are never touched


def test_reading_columns_are_rewritten_with_the_same_length():
    page = PageReading(pdf_page=1, regions=[Region(id="a", kind="본문", bbox=[0, 0, 10, 10],
        paragraphs=[Paragraph(columns=[Column(col_from_right=1, text="그 説明이 真實")])])])
    assert apply_to_reading(page) == 2
    assert page.regions[0].paragraphs[0].columns[0].text == "그 說明이 眞實"
