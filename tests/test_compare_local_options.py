"""Measurement tool helpers: reference extraction, page lists, error counts."""
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import compare_local_options as tool   # noqa: E402


def test_page_list_and_compact():
    assert tool.page_list(None, 3) == [1, 2, 3]
    assert tool.page_list("2-4,7,20", 10) == [2, 3, 4, 7]
    assert tool.compact(" 天 地\n玄\t黃 ") == "天地玄黃"


def test_char_errors_counts_substitutions_and_moves():
    assert tool.char_errors("天地玄黃", "天地玄黃") == 0
    assert tool.char_errors("天地玄黃", "天地玄黄") == 1
    assert tool.char_errors("甲乙丙丁戊己", "丁戊己甲乙丙") == 6       # swapped halves: every character counts once


def test_reference_from_txt_and_hwpx(tmp_path):
    txt = tmp_path / "ref.txt"
    txt.write_text("天地玄黃\n宇宙洪荒", encoding="utf-8")
    assert tool.reference_text(txt) == "天地玄黃\n宇宙洪荒"
    hwpx = tmp_path / "ref.hwpx"
    section = ('<hs:sec xmlns:hs="s" xmlns:hp="p"><hp:p id="1"><hp:run><hp:t>天地</hp:t></hp:run>'
               '<hp:run><hp:t>玄黃&amp;</hp:t></hp:run></hp:p><hp:p id="2"><hp:run><hp:t>宇宙</hp:t></hp:run></hp:p></hs:sec>')
    with zipfile.ZipFile(hwpx, "w") as package:
        package.writestr("Contents/section0.xml", section)
    assert tool.reference_text(hwpx) == "天地玄黃&\n宇宙"
