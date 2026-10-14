"""검색 가능한 PDF(textlayer): 모델 없이 그린 스캔 쪽에 판독 글을 숨은 글자층으로 넣고 다시 읽어 글·자리·화면을 확인한다."""
import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pymupdf

from yeone_ocr import textlayer as TL
from yeone_ocr.pdfio import images_to_pdf, layer_chars, page_frame, page_image, text_layer
from yeone_ocr.schema import PageReading, frac_bbox

VCOLS = ["開寶重定本草卷第一", "三皇之世神農氏嘗百草", "令民知所避就當此之時", "由是醫方興焉上藥一百", "以應天無毒多服"]
HLINES = ["香薷味辛微温主霍乱", "陶隐居云处处有此惟供", "薄荷味辛苦温无毒主贼"]


def scan(tmp: Path, lines: list[str], vertical: bool, name: str = "scan") -> tuple[Path, list[list[tuple[str, list[float]]]]]:
    """글자를 그린 쪽을 200dpi 회색 그림으로 바꿔 그림만 든 PDF 로(글자층 없음). 돌려주는 값: (PDF, 줄마다 [(글자, 정답 상자 pt)])."""
    W, H, fs = 360, 480, 20
    src = pymupdf.open(); page = src.new_page(width=W, height=H)
    truth = []
    for li, text in enumerate(lines):
        row = []
        for i, ch in enumerate(text):
            x, y = ((W - 50 - li * 36, 60 + i * fs * 1.1) if vertical else (40 + i * fs * 1.1, 70 + li * 40))
            page.insert_text((x, y + fs * 0.88), ch, fontname="china-t", fontsize=fs)
            row.append((ch, [x, y, x + fs, y + fs]))
        truth.append(row)
    png = tmp / f"{name}.png"
    page.get_pixmap(dpi=200, colorspace=pymupdf.csGRAY).save(png)
    src.close()
    pdf = tmp / f"{name}.pdf"; pdf.write_bytes(images_to_pdf([png], default_dpi=200))
    return pdf, truth


def reading(truth, vertical: bool, shift=(0.0, 0.0), W=360, H=480, label: str = "") -> PageReading:
    """정답 상자를 둘러싼 영역 하나(모델 좌표처럼 shift 만큼 밀어서)의 판독."""
    x0 = min(b[0] for row in truth for _, b in row) - 3; x1 = max(b[2] for row in truth for _, b in row) + 3
    y0 = min(b[1] for row in truth for _, b in row) - 3; y1 = max(b[3] for row in truth for _, b in row) + 3
    fr = (x0 / W + shift[0], y0 / H + shift[1], x1 / W + shift[0], y1 / H + shift[1])
    cols = [{"n": i + 1, "t": "".join(c for c, _ in row)} for i, row in enumerate(truth)]
    return PageReading.model_validate({"pdf_page": 1, "label": label, "regions": [
        {"id": "r1", "k": "본문", "bbox": frac_bbox(fr), "dir": "세로(우→좌)" if vertical else "가로(좌→우)", "paras": [{"cols": cols}]}]})


def inside(doc, truth, want: str) -> tuple[int, int]:
    """넣은 글자층의 글자 중심이 정답 상자 안에 든 수 / 전체."""
    lines = [l for l in layer_chars(doc, 1, own=True) if l["dir"] == want]
    hit = n = 0
    for row, ln in zip(truth, lines):
        for (_, tb), (_, bb) in zip(row, ln["chars"]):
            cx, cy = (bb[0] + bb[2]) / 2, (bb[1] + bb[3]) / 2
            hit += tb[0] <= cx <= tb[2] and tb[1] <= cy <= tb[3]; n += 1
    return hit, n


def render(doc) -> bytes:
    return doc[0].get_pixmap(dpi=72).samples


class Tokens(unittest.TestCase):
    def test_marks_fold_to_printed_glyph_slots(self):
        self.assertEqual(TL.tokens("上藥〔爲/為〕君 主"), list("上藥爲君主"))
        self.assertEqual(TL.tokens("甲〔疑: 龍〕乙"), list("甲龍乙"))
        self.assertEqual(TL.tokens("甲〔판독불명〕乙〔원본 결락〕丙"), ["甲", None, "乙", "丙"])   # 판독불명은 먹이 있는 한 자리, 결락은 자리 없음

    def test_out_path_and_half_width_advance(self):
        self.assertEqual(TL.out_path("w", "책.ocr.pdf"), Path("w") / "out" / "책.ocr_글자층.pdf")
        self.assertEqual((TL.adv("天"), TL.adv("，"), TL.adv("3"), TL.adv("a")), (1.0, 1.0, 0.5, 0.5))


class SearchablePdf(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.tmp = Path(self.temp.name)

    def make(self, pdf: Path, pr: PageReading, vertical: bool, strip_old: bool = True):
        doc = pymupdf.open(pdf); self.addCleanup(doc.close)
        img, _ = page_image(doc, 1); frame = page_frame(doc, 1)
        out = self.tmp / f"{pdf.stem}_out.pdf"
        st = TL.searchable_pdf(doc, [1], lambda n: (pr, img, frame, vertical), out, strip_old=strip_old, log=lambda *a: None)
        done = pymupdf.open(out); self.addCleanup(done.close)
        return st, done

    def test_vertical_scan_gets_invisible_columns_at_the_glyphs_even_with_shifted_model_box(self):
        pdf, truth = scan(self.tmp, VCOLS, vertical=True)
        for shift in ((0.0, 0.0), (0.01, 0.025)):                     # 모델 좌표가 오른쪽 1%·아래 2.5% 밀려도
            with self.subTest(shift=shift):
                st, done = self.make(pdf, reading(truth, True, shift), True)
                self.assertEqual([c["text"] for c in text_layer(done, 1, own=True).vertical_columns], VCOLS)
                hit, n = inside(done, truth, "v")
                self.assertGreaterEqual(hit, n - 1, (hit, n))
                self.assertEqual(st["mismatch"], []); self.assertEqual(st["pages"], 1)
                self.assertTrue(set(st["how"]) <= {"ink", "ink/group", "ink/split"}, st["how"])
                with pymupdf.open(pdf) as before:
                    self.assertEqual(render(done), render(before))           # 화면은 그대로
                self.assertEqual(text_layer(done, 1).char_count, 0)            # 자기 판독은 기존 OCR(증인)로 읽지 않는다
                self.assertEqual(len(done[0].search_for("三皇之世")), 1)

    def test_horizontal_scan_replaces_wrong_hidden_layer_of_another_engine(self):
        pdf, truth = scan(self.tmp, HLINES, vertical=False)
        doc = pymupdf.open(pdf)
        wrong = {"薷": "蕾", "隐": "隱", "温": "溫"}                        # 다른 엔진: 오독·번체로 읽음
        for row in truth:
            for ch, b in row:
                doc[0].insert_text((b[0], b[1] + 17.6), wrong.get(ch, ch), fontname="china-t", fontsize=20, render_mode=3)
        old = self.tmp / "other.ocr.pdf"; doc.save(old); doc.close()
        with pymupdf.open(old) as d:
            self.assertIn("香蕾", d[0].get_text())
        st, done = self.make(old, reading(truth, False, (0.01, 0.01)), False)
        self.assertEqual(st["stripped"], 1)
        text = done[0].get_text()
        self.assertNotIn("香蕾", text); self.assertNotIn("隱", text)          # 옛 층은 지워짐
        got = ["".join(l["text"].split()) for l in sorted(text_layer(done, 1, own=True).horizontal_lines, key=lambda l: (l["bbox"][1], l["bbox"][0]))]
        self.assertEqual(got, HLINES)
        hit, n = inside(done, truth, "h"); self.assertGreaterEqual(hit, n - 1, (hit, n))
        with pymupdf.open(old) as before:
            self.assertEqual(render(done), render(before))

    def test_keep_old_layer_leaves_other_engine_text(self):
        pdf, truth = scan(self.tmp, HLINES[:1], vertical=False)
        doc = pymupdf.open(pdf); doc[0].insert_text((40, 88), "香蕾", fontname="china-t", fontsize=20, render_mode=3)
        old = self.tmp / "keep.pdf"; doc.save(old); doc.close()
        st, done = self.make(old, reading(truth, False), False, strip_old=False)
        self.assertEqual(st["stripped"], 0)
        self.assertIn("香蕾", text_layer(done, 1).plain())                   # 남겨 둔 다른 OCR 은 증인으로 계속 읽힌다
        self.assertIn(HLINES[0], "".join(text_layer(done, 1, own=True).plain().split()))

    def test_page_with_visible_text_is_skipped(self):
        doc = pymupdf.open(); doc.new_page(width=200, height=200).insert_text((20, 40), "visible", fontsize=12)
        pdf = self.tmp / "born.pdf"; doc.save(pdf); doc.close()
        pr = PageReading.model_validate({"pdf_page": 1, "regions": [{"id": "r1", "k": "본문", "bbox": [100, 100, 300, 600], "dir": "가로", "paras": [{"cols": [{"n": 1, "t": "天地"}]}]}]})
        with pymupdf.open(pdf) as d:
            img, _ = page_image(d, 1)
            st = TL.searchable_pdf(d, [1], lambda n: (pr, img, page_frame(d, 1), False), self.tmp / "o.pdf", log=lambda *a: None)
        self.assertEqual(st["pages"], 0); self.assertIn(1, st["skipped"])


class Writing(unittest.TestCase):
    """자리 계획과 무관하게 쓰기만: 희귀 한자 보존, /Rotate·CropBox 좌표, 위에서 물려받은 자원."""

    def write(self, doc, lines, frame=None):
        font, cids = TL.add_font(doc, [c for ln in lines for c, _ in ln.chars])
        page = doc[0]
        TL.write_lines(doc, page, lines, frame or (0, 0, page.rect.width, page.rect.height), font, cids)
        data = doc.tobytes(garbage=3, deflate=True)
        return pymupdf.open("pdf", data)

    def test_rare_hanja_hangul_and_digits_survive_extraction(self):
        text = "天地㐀䗪𠮷𧊅﨑硏가〓12"
        doc = pymupdf.open(); doc.new_page(width=300, height=200)
        line = TL.Line("h", [(c, (0.05 + i * 0.07, 0.3, 0.05 + (i + 1) * 0.07, 0.4)) for i, c in enumerate(text)], "uniform")
        with self.write(doc, [line]) as out:
            self.assertEqual("".join(TL.own_text(out, 1)).replace(" ", ""), text)
            self.assertEqual(len(out[0].search_for("𠮷𧊅")), 1)
            self.assertEqual(out[0].get_fonts()[0][3], TL.FONT_NAME)
            self.assertEqual(text_layer(out, 1).char_count, 0)
            self.assertEqual("".join(text_layer(out, 1, own=True).plain().split()), text)

    def test_coordinates_follow_rotate_and_cropbox(self):
        boxes = [(0.70, 0.10 + i * 0.08, 0.78, 0.18 + i * 0.08) for i in range(5)]   # 보이는 쪽에서 위→아래 세로 열
        for rot in (0, 90, 180, 270):
            for crop in (False, True):
                with self.subTest(rot=rot, crop=crop):
                    doc = pymupdf.open(); page = doc.new_page(width=400, height=500)
                    if crop:
                        page.set_cropbox(pymupdf.Rect(30, 40, 370, 480))
                    page.set_rotation(rot)
                    W, H = page.rect.width, page.rect.height                 # 보이는 쪽 크기
                    line = TL.Line("v", [(c, b) for c, b in zip("天地玄黃宇", boxes)], "ink")
                    with self.write(doc, [line]) as out:
                        got = [l for l in layer_chars(out, 1, own=True) if l["chars"]]
                        self.assertEqual(len(got), 1); self.assertEqual(got[0]["dir"], "v")
                        self.assertEqual("".join(c for c, _ in got[0]["chars"]), "天地玄黃宇")
                        for (c, bb), want in zip(got[0]["chars"], boxes):
                            cx, cy = (bb[0] + bb[2]) / 2 / W, (bb[1] + bb[3]) / 2 / H
                            self.assertAlmostEqual(cx, (want[0] + want[2]) / 2, delta=0.02)
                            self.assertAlmostEqual(cy, (want[1] + want[3]) / 2, delta=0.02)

    def test_inherited_resources_keep_the_scan_image(self):
        with tempfile.TemporaryDirectory() as td:
            d = pymupdf.open(); p = d.new_page(width=120, height=80); p.draw_rect(pymupdf.Rect(10, 10, 60, 40), color=(0, 0, 0), fill=(0, 0, 0))
            png = Path(td) / "a.png"; p.get_pixmap(dpi=100).save(png); d.close()
            doc = pymupdf.open("pdf", images_to_pdf([png], default_dpi=100))
        page = doc[0]
        res = doc.xref_get_key(page.xref, "Resources")
        pages_xref = int(doc.xref_get_key(doc.pdf_catalog(), "Pages")[1].split()[0])
        doc.xref_set_key(pages_xref, "Resources", res[1])                     # 자원을 Pages 로 올리고 쪽에서는 지움(물려받는 꼴)
        doc.xref_set_key(page.xref, "Resources", "null")
        doc = pymupdf.open("pdf", doc.tobytes())
        self.assertEqual(doc.xref_get_key(doc[0].xref, "Resources")[0], "null")
        before = render(doc)
        line = TL.Line("h", [(c, (0.1 + i * 0.1, 0.4, 0.2 + i * 0.1, 0.5)) for i, c in enumerate("天地玄")], "ink")
        with self.write(doc, [line]) as out:
            self.assertEqual(render(out), before)
            self.assertEqual("".join(TL.own_text(out, 1)), "天地玄")


class Command(unittest.TestCase):
    def test_run_with_mock_reader_makes_searchable_pdf_and_textlayer_command(self):
        from yeone_ocr import cli
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            pdf, truth = scan(tmp, VCOLS, vertical=True, name="책")
            doc = pymupdf.open(pdf)                                         # mock 판독기가 옮겨 쓸 다른 OCR 글자층(돌려 쓴 세로 열 — 흔한 OCR PDF 의 꼴)
            for row in truth:
                x0, y0 = row[0][1][0], row[0][1][1]
                doc[0].insert_text((x0 + 4, y0), "".join(c for c, _ in row), fontname="china-t", fontsize=22, rotate=270, render_mode=3)
            src = tmp / "책.ocr.pdf"; doc.save(src); doc.close()
            w = str(tmp / "w")
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(cli.main(["textlayer", str(src), "--work", w]), 2)          # 판독 기록이 없으면 만들지 않는다
                cli.main(["run", str(src), "--work", w, "--reader", "mock", "--checkpoint", "0", "--mode", "vertical", "--textlayer-pdf"])
            out = tmp / "w" / "out" / "책.ocr_글자층.pdf"
            self.assertTrue(out.exists())
            with pymupdf.open(out) as d:
                self.assertEqual([c["text"] for c in text_layer(d, 1, own=True).vertical_columns], VCOLS)
                self.assertEqual(text_layer(d, 1).char_count, 0)                 # 옛 층은 지우고 자기 층은 증인에서 뺌
            os.remove(out)
            log = io.StringIO()
            with contextlib.redirect_stdout(log):
                rc = cli.main(["textlayer", str(src), "--work", w, "--out", str(tmp / "다른.pdf")])
            self.assertEqual(rc, 0, log.getvalue())
            self.assertTrue((tmp / "다른.pdf").exists()); self.assertFalse(out.exists())
            self.assertIn("모든 쪽 같음", log.getvalue())

    def test_build_option_and_progress_stage(self):
        from yeone_ocr.cli import build_parser
        from yeone_ocr.progress import PLANS, STAGE_NAMES
        for cmd in ("build", "run"):
            self.assertTrue(build_parser().parse_args([cmd, "x.pdf", "--textlayer-pdf"]).textlayer_pdf)
            self.assertFalse(build_parser().parse_args([cmd, "x.pdf"]).textlayer_pdf)
        a = build_parser().parse_args(["textlayer", "x.pdf", "--keep-old-layer", "--pages", "2-3"])
        self.assertTrue(a.keep_old_layer); self.assertEqual(a.pages, "2-3")
        self.assertIn("textlayer", PLANS); self.assertIn("textlayer", STAGE_NAMES)


@unittest.skipUnless(__import__("importlib").util.find_spec("tkinter"), "tkinter 없음")
class Window(unittest.TestCase):
    def test_tool_button_and_document_option_build_commands(self):
        from yeone_ocr import gui
        from yeone_ocr.cli import build_parser
        app = SimpleNamespace(_need_records=lambda what: True, nb=SimpleNamespace(select=lambda i: None),
                              work=lambda: Path("C:/책_YeonE_OCR"), pdf=lambda: "C:/책.ocr.pdf", _pages=lambda: ["--pages", "1-3"], start=mock.Mock())
        gui.App.do_textlayer(app)
        argv, label = app.start.call_args.args
        self.assertEqual(argv, ["textlayer", "C:/책.ocr.pdf", "--work", str(Path("C:/책_YeonE_OCR")), "--pages", "1-3"])
        self.assertEqual(app.start.call_args.kwargs["open_after"], TL.out_path(Path("C:/책_YeonE_OCR"), "C:/책.ocr.pdf"))
        build_parser().parse_args(argv)
        V = lambda v: SimpleNamespace(get=lambda: v)
        opts = SimpleNamespace(v_docx=V(False), v_join=V(True), v_hl=V(True), v_ctx=V(True), v_pb=V(True), v_tlpdf=V(True),
                               check_list_mode=lambda: "none", memo_mode=lambda: "notice")
        o = gui.App._build_opts(opts)
        self.assertIn("--textlayer-pdf", o)
        self.assertTrue(build_parser().parse_args(["run", "x.pdf"] + o).textlayer_pdf)
        opts.v_tlpdf = V(False)
        self.assertNotIn("--textlayer-pdf", gui.App._build_opts(opts))
        self.assertFalse(gui.DEFAULTS["textlayer_pdf"])


if __name__ == "__main__":
    unittest.main()
