"""Measurement tool: answers, document discovery and the summary, without OCR engines."""
import json
import sys
import zipfile
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import compare_local_options as tool   # noqa: E402

pymupdf = pytest.importorskip("pymupdf")


def test_page_list_spec_and_compact():
    assert tool.page_list(None, 3) == [1, 2, 3]
    assert tool.page_list("2-4,7,20", 10) == [2, 3, 4, 7]
    assert tool.page_list(None, 30, limit=5) == [1, 2, 3, 4, 5]
    assert tool.spec_of([1, 2, 3, 7, 9, 10]) == "1-3,7,9-10"
    assert tool.compact(" 天 地\n玄\t黃 ") == "天地玄黃"


def test_char_errors_and_crop():
    assert tool.char_errors("天地玄黃", "天地玄黄") == 1
    assert tool.char_errors("甲乙丙丁戊己", "丁戊己甲乙丙") == 6       # swapped halves: every character counts once
    whole = "天地玄黃宇宙洪荒日月盈昃辰宿列張寒來暑往秋收冬藏"
    assert tool.crop_reference(whole, "日月盈昃辰宿列張寒來暑往") == "日月盈昃辰宿列張寒來暑往"


def write_hwpx(path, paragraphs):
    body = "".join(f'<hp:p id="{i}"><hp:run><hp:t>{t}</hp:t></hp:run></hp:p>' for i, t in enumerate(paragraphs))
    memo = ('<hp:p id="9"><hp:run><hp:ctrl><hp:fieldBegin type="MEMO"><hp:subList><hp:p><hp:run><hp:t>메모</hp:t>'
            '</hp:run></hp:p></hp:subList></hp:fieldBegin></hp:ctrl><hp:t>本文</hp:t></hp:run></hp:p>')
    with zipfile.ZipFile(path, "w") as package:
        package.writestr("Contents/section0.xml", f'<hs:sec xmlns:hs="s" xmlns:hp="p">{body}{memo}</hs:sec>')


def test_answers_from_txt_hwpx_docx(tmp_path):
    txt = tmp_path / "a.txt"
    txt.write_text("天地玄黃", encoding="utf-8")
    assert tool.reference_text(txt) == "天地玄黃"
    hwpx = tmp_path / "a.hwpx"
    write_hwpx(hwpx, ["天地", "玄黃&amp;"])
    assert tool.compact(tool.reference_text(hwpx)) == "天地玄黃&本文"         # memo text left out
    docx = pytest.importorskip("docx")
    document = docx.Document()
    document.add_paragraph("宇宙洪荒")
    document.save(tmp_path / "a.docx")
    assert tool.compact(tool.reference_text(tmp_path / "a.docx")) == "宇宙洪荒"


def test_printed_detection_on_a_few_pages_of_a_long_answer():
    answer = "".join(chr(0x4E00 + i % 3000) for i in range(24000))
    assert tool.printed_from_answer(answer[5000:5800], answer) is True
    assert tool.printed_from_answer("".join(chr(0x9000 + i) for i in range(800)), answer) is False
    assert tool.printed_from_answer("短", answer) is False


def test_profile_guess():
    assert tool.guess_profile("天地玄黃宇宙洪荒" * 5) == "traditional"
    assert tool.guess_profile("药方集解说这个问题" * 5) == "simplified"
    assert tool.guess_profile("한자와 한글이 섞인 글 天地" * 5) == "mixed"


def make_pdf(path, pages, text=None):
    document = pymupdf.open()
    for _ in range(pages):
        page = document.new_page()
        if text:
            page.insert_text((72, 72), text)
    document.save(path)
    document.close()


def test_folder_discovery_pairs_answers_and_marks_printed_pdfs(tmp_path):
    folder = tmp_path / "TEST"
    folder.mkdir()
    make_pdf(folder / "인쇄본 - test.pdf", 2, "abcdefghij klmnopqrst")
    (folder / "인쇄본 - test.txt").write_text("abcdefghijklmnopqrst" * 2, encoding="utf-8")
    make_pdf(folder / "스캔본.pdf", 3)
    write_hwpx(folder / "스캔본.hwpx", ["天地玄黃"])
    (folder / "스캔본.BAK").write_text("x", encoding="utf-8")
    make_pdf(folder / "정답없음.pdf", 1)
    args = tool.argparse.Namespace(source=folder, out=tmp_path / "out", pages=None, max_pages=0, profile=None,
                                   mode="auto", reference=None)
    docs = {d["name"]: d for d in tool.discover(args)}
    assert docs["인쇄본 - test"]["printed_from_answer"] is True
    assert docs["스캔본"]["answer"].endswith("스캔본.hwpx") and docs["스캔본"]["printed_from_answer"] is False
    assert docs["스캔본"]["profile"] == "traditional" and docs["스캔본"]["pages"] == [1, 2, 3]
    assert docs["정답없음"]["answer"] is None and docs["정답없음"]["profile_source"] == "default"
    assert docs["스캔본"]["profile_source"] == "answer"
    manifest = json.loads((tmp_path / "out" / "manifest.json").read_text(encoding="utf-8"))
    manifest["documents"][0].update(profile="mixed", profile_confirmed=True)   # edits are kept on the next run
    (tmp_path / "out" / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    again = tool.discover(args)[0]
    assert (again["profile"], again["profile_source"]) == ("mixed", "manifest")
    args.max_pages = 1                                               # the command, not the last run, picks pages
    assert tool.discover(args)[0]["pages"] == [1]
    args.max_pages = 0
    assert tool.discover(args)[0]["pages"] == [1, 2, 3]


def test_main_runs_sets_in_order_skips_witness_on_printed_and_resumes(tmp_path):
    folder = tmp_path / "TEST"
    folder.mkdir()
    make_pdf(folder / "doc.pdf", 2, "abcdefghij")
    (folder / "doc.txt").write_text("abcdefghij" * 2, encoding="utf-8")
    calls = []

    def fake_run(name, doc, args, out_dir):
        calls.append(name)
        (out_dir / name).mkdir(parents=True, exist_ok=True)
        return out_dir / name, 0, 4.0

    out = tmp_path / "out"
    with mock.patch.object(tool, "run_set", side_effect=fake_run), \
            mock.patch.object(tool, "recognized", return_value=("abcdefghij" * 2, {"ppocrv6": 2})):
        assert tool.main([str(folder), "--out", str(out), "--sets", "vote,witness,sequential"]) == 0
        assert calls == ["sequential", "default", "vote"]              # witness skipped: printed from the answer
        assert tool.main([str(folder), "--out", str(out), "--sets", "vote,sequential", "--resume"]) == 0
        assert calls == ["sequential", "default", "vote"]
        summary = json.loads((out / "summary.json").read_text(encoding="utf-8"))
        assert [r["cer_percent"] for r in summary["rows"]] == [0.0, 0.0, 0.0]
        assert "| doc (인쇄본) |" in (out / "summary.md").read_text(encoding="utf-8")
        assert tool.main([str(folder), "--out", str(out), "--sets", "default", "--resume", "--max-pages", "1"]) == 0
        assert calls == ["sequential", "default", "vote", "default"]   # other pages: measured again


def test_run_set_streams_page_progress_and_keeps_full_log(tmp_path, capsys):
    script = tmp_path / "fake.py"
    script.write_text('print("  1쪽: no-api-needed · 로컬 ppocrv6")\nprint("MuPDF error: font")\n'
                      'print("  glmocr: GPU 실행을 시작하지 못해")\nprint("detail line")\n', encoding="utf-8")
    doc = {"pdf": "x.pdf", "profile": "traditional", "pages": [1, 2]}
    args = tool.argparse.Namespace(engine_root=None, ai=False)
    real_popen = tool.subprocess.Popen

    def fake(argv, **kwargs):
        return real_popen([tool.sys.executable, str(script)], **{k: v for k, v in kwargs.items() if k != "cwd"})
    with mock.patch.object(tool, "SETS", {"default": ([], {}, False)}), \
            mock.patch.object(tool.subprocess, "Popen", side_effect=fake):
        _work, code, _seconds = tool.run_set("default", doc, args, tmp_path)
    shown = capsys.readouterr().out
    assert code == 0
    assert "[1/2쪽" in shown and "GPU" in shown
    assert "MuPDF" not in shown and "detail line" not in shown
    assert "detail line" in (tmp_path / "default.log").read_text(encoding="utf-8")
