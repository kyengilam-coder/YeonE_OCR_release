"""AI page reading with synthetic images and a fake Claude client."""
import json

from PIL import Image, ImageDraw

from yeone_ocr import ai_page


def test_cuts_land_in_the_gaps():
    values = [255] * 1000
    for x in range(480, 520):
        values[x] = 0                        # an empty gap between two columns
    cuts = ai_page._cuts(values, 1000, target=600, search=140)
    assert cuts[0] == 0 and cuts[-1] == 1000 and 480 <= cuts[1] < 520


def test_vertical_strips_run_right_to_left_and_cut_between_columns():
    image = Image.new("RGB", (2000, 1200), "white")
    draw = ImageDraw.Draw(image)
    for x in range(100, 1900, 150):          # printed columns 100 px wide, 50 px gaps
        draw.rectangle((x, 100, x + 100, 1100), fill="black")
    overview, pictures, boxes = ai_page.strips(image, "vertical")
    assert len(pictures) >= 2 and overview
    assert boxes[0][0] > boxes[-1][0]                                   # first strip is the rightmost
    for left, _top, right, _bottom in boxes:
        for edge in (left, right):
            assert not any(x < edge < x + 100 for x in range(100, 1900, 150))   # never through a column


def test_merge_takes_short_ai_differences_and_keeps_long_ones():
    base = "그렇다면어찌서\n一小一多하다"
    text, red, blue, summary = ai_page.merge(base, "그렇다면 어째서 一小一多하다")
    assert text == "그렇다면어째서\n一小一多하다" and summary["taken"] == 1 and summary["used"]
    text, red, blue, summary = ai_page.merge(base, "그렇다면어째서 一小一多하다", mode="witness")
    assert text == base and blue == [(5, 6)] and summary["witness"] == 1
    long_base = "가나다라마바사아자차카타파하" * 2
    long_ai = "가나다라마바사아자차카타파하" + "ㄱㄴㄷㄹㅁㅂㅅㅇ" + "아자차카타파하"
    text, red, _blue, summary = ai_page.merge(long_base, long_ai)
    assert text == long_base and red and summary["kept_long"] == 1
    assert ai_page.merge(base, "完全히 다른 쪽의 글")[3]["used"] is False


def test_read_page_sends_overview_and_strips_and_caches(tmp_path):
    image = Image.new("RGB", (1600, 1000), "white")
    ImageDraw.Draw(image).rectangle((200, 100, 1400, 900), fill="black")
    calls = []

    class Fake:
        def request(self, **kwargs):
            calls.append(kwargs)
            return json.dumps({"lines": ["一行", "二行"], "uncertain": ["二"]}), True, "end_turn", {"input_tokens": 9}, "opus-x"

    first = ai_page.read_page(image, direction="vertical", profile="mixed", client=Fake(), model="opus", cache_dir=tmp_path)
    again = ai_page.read_page(image, direction="vertical", profile="mixed", client=Fake(), model="opus", cache_dir=tmp_path)
    assert first["text"] == "一行\n二行" and first["uncertain"] == ["二"] and not first["cached"]
    assert again["cached"] and len(calls) == 1                         # no second paid call for the same page
    request = calls[0]
    assert len(request["images"]) == len(first["strips"]) + 1 and "RIGHT side" in request["prompt"]


def test_punctuation_and_uncertain_words_keep_the_engine_reading():
    base = "것이다.分合을 取하여 用穿하고"
    text, red, _blue, summary = ai_page.merge(base, "것이다·分合을 取하여 用夯하고", uncertain=["夯"])
    assert text == base                                            # · for . not taken; uncertain 夯 not taken
    assert summary["punctuation_kept"] == 1 and summary["uncertain_kept"] == 1
    assert [text[a:b] for a, b in red] == ["穿"]                    # the doubted glyph is painted red
    text, red, _blue, _summary = ai_page.merge("自薦之癖을", "自薦之癖을", uncertain=["自薦之癖"])
    assert text == "自薦之癖을" and [text[a:b] for a, b in red] == ["自", "薦", "之", "癖"]   # agreed but doubted


def test_script_differences_align_and_take_the_ai_form():
    """Engine (PP-OCRv6) wrote Simplified forms in a Traditional book, the AI page reading Traditional.
    Before: one long difference, engine line kept and painted red. Now the glyphs align, the AI's form
    is taken (it settles one-to-many characters: 叶→葉, 须→須), and nothing is marked."""
    from yeone_ocr.ai_page import merge
    engine = "药有陰陽配合，子母兄弟，根叶華實，草石骨肉。有單行者，有相须者，有相使者，有相恶者"
    ai = "藥有陰陽配合，子母兄弟，根葉華實，草石骨肉。有單行者，有相須者，有相使者，有相惡者"
    text, red, blue, summary = merge(engine, ai, "prefer")
    assert text == ai and red == [] and blue == []
    assert summary["script_taken"] == 4 and summary["kept_long"] == 0 and summary["ratio"] == 1.0
    kept, red, blue, summary = merge(engine, ai, "witness")
    assert kept == engine and red == [] and blue == [] and summary["script_taken"] == 0   # witness never edits


def test_variant_forms_still_count_as_differences():
    """裏/裡 fold to the same Simplified 里 but neither is it, and both are Korean forms: a real variant."""
    from yeone_ocr.ai_page import merge
    text, red, blue, summary = merge("裏之", "裡之", "prefer")
    assert text == "裡之" and summary["taken"] == 1 and summary["script_taken"] == 0
    text, red, blue, summary = merge("裏之", "裡之", "witness")
    assert text == "裏之" and blue == [(0, 1)]


def test_korean_standard_forms_are_the_same_glyph():
    """Documents write 爲/眞/說 (KS X 1001) for 為/真/説, so an engine or AI reading either is no difference."""
    from yeone_ocr.ai_page import merge
    text, red, blue, summary = merge("為之真", "爲之眞", "prefer")
    assert (text, red, blue, summary["script_taken"]) == ("爲之眞", [], [], 2)


def test_script_pairs_are_one_character_in_two_scripts_only():
    from yeone_ocr.collate.zh import script_only, script_pair
    assert script_pair("会", "會") and script_pair("復", "复") and script_pair("复", "複")
    assert script_pair("爲", "為") and script_pair("真", "眞") and script_pair("説", "說")      # Korean standard forms
    assert not script_pair("裏", "裡") and not script_pair("會", "會") and not script_pair("甲", "乙")
    assert script_only(["会议", "會議"]) and script_only(["会 ", "會"]) and script_only(["爲", "為"])
    assert not script_only(["裏", "裡"]) and not script_only(["，", ","]) and not script_only(["會", "会x"])
    assert not script_only(["會", "會"])
