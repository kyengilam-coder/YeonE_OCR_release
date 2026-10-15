"""검색 가능한 PDF(textlayer): 모델 없이 그린 스캔 쪽에 판독 글을 숨은 글자층으로 넣고 다시 읽어 글·자리·화면을 확인한다."""
import contextlib
import io
import os
import tempfile
import threading
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
        done = pymupdf.open("pdf", out.read_bytes()); self.addCleanup(done.close)  # 다음 변형에서도 같은 출력 이름을 쓸 수 있게(Windows 파일 잠금)
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

    def test_nothing_inserted_saves_no_copy_and_progress_reaches_total(self):
        doc = pymupdf.open(); doc.new_page(width=200, height=200).insert_text((20, 40), "visible", fontsize=12)
        pdf = self.tmp / "born2.pdf"; doc.save(pdf); doc.close()
        pr = PageReading.model_validate({"pdf_page": 1, "regions": [{"id": "r1", "k": "본문", "bbox": [100, 100, 300, 600], "dir": "가로", "paras": [{"cols": [{"n": 1, "t": "天地"}]}]}]})
        steps = []
        with pymupdf.open(pdf) as d:
            img, _ = page_image(d, 1)
            st = TL.searchable_pdf(d, [1], lambda n: (pr, img, page_frame(d, 1), False), self.tmp / "none.pdf", log=lambda *a: None, step=lambda a, b: steps.append((a, b)))
        self.assertEqual(st["pages"], 0)
        self.assertFalse((self.tmp / "none.pdf").exists())                   # 넣은 쪽이 없으면 원본 사본을 만들지 않는다
        self.assertEqual(steps[-1], (2, 2))                                   # 진행 막대는 끝까지 간다

    def test_stop_between_pages_keeps_previous_output(self):
        pdf, truth = scan(self.tmp, HLINES[:1], vertical=False, name="stop")
        pr = reading(truth, False)
        out = self.tmp / "previous.pdf"
        for stop_at in (0, 1, 2, 3, 4):
            with self.subTest(stop_at=stop_at), pymupdf.open(pdf) as doc:
                doc.fullcopy_page(0)
                img, _ = page_image(doc, 1)
                data = mock.Mock(side_effect=lambda n: (pr, img, page_frame(doc, n), False))
                stop = threading.Event()
                if stop_at == 0:
                    stop.set()
                out.write_bytes(b"previous completed PDF")
                def step(done, total):
                    if done == stop_at:
                        stop.set()
                with self.assertRaises(TL.TextlayerCancelled):
                    TL.searchable_pdf(doc, [1, 2], data, out, step=step, should_stop=stop.is_set)
                self.assertEqual(out.read_bytes(), b"previous completed PDF")
                self.assertEqual(data.call_count, min(stop_at, 2))

    def test_stop_during_save_discards_temporary_pdf_and_can_resume(self):
        pdf, truth = scan(self.tmp, HLINES[:1], vertical=False, name="save_stop")
        pr = reading(truth, False)
        out = self.tmp / "finished.pdf"
        out.write_bytes(b"previous completed PDF")
        before = {p.name for p in self.tmp.iterdir()}
        stop = threading.Event()
        with pymupdf.open(pdf) as doc:
            img, _ = page_image(doc, 1)
            save = doc.save
            def save_then_stop(*args, **kwargs):
                save(*args, **kwargs)
                stop.set()
            with mock.patch.object(doc, "save", side_effect=save_then_stop):
                with self.assertRaises(TL.TextlayerCancelled):
                    TL.searchable_pdf(doc, [1], lambda n: (pr, img, page_frame(doc, n), False), out, should_stop=stop.is_set)
        self.assertEqual(out.read_bytes(), b"previous completed PDF")
        self.assertEqual({p.name for p in self.tmp.iterdir()}, before)
        stop.clear()
        with pymupdf.open(pdf) as doc:
            img, _ = page_image(doc, 1)
            st = TL.searchable_pdf(doc, [1], lambda n: (pr, img, page_frame(doc, n), False), out, should_stop=stop.is_set)
        self.assertEqual((st["pages"], st["mismatch"]), (1, []))
        with pymupdf.open(out) as done:
            self.assertIn(HLINES[0], "".join(TL.own_text(done, 1)).replace(" ", ""))

    def test_indirect_contents_array_keeps_the_scan_visible_and_font_is_embedded(self):
        """검토에서 재현된 것: /Contents 가 간접 배열(N 0 R → [a 0 R b 0 R])인 쪽에 글자층을 덧붙이면 배열 객체를 흐름으로 알고 이어 써
        MuPDF 가 'object is not a stream' 으로 쪽을 백지로 그렸다(숨은 글자층이 없어 redaction 을 거치지 않는 순수 스캔·--keep-old-layer).
        글꼴은 내장(FontFile2)이어야 Acrobat 이 '글꼴을 찾거나 만들 수 없습니다' 창을 띄우지 않는다."""
        pdf, truth = scan(self.tmp, HLINES[:1], vertical=False, name="indirect")
        doc = pymupdf.open(pdf); page = doc[0]
        cx = page.get_contents()
        arr = doc.get_new_xref(); doc.update_object(arr, "[" + " ".join(f"{x} 0 R" for x in cx) + "]")
        doc.xref_set_key(page.xref, "Contents", f"{arr} 0 R")
        src = self.tmp / "indirect2.pdf"; doc.save(src); doc.close()
        with pymupdf.open(src) as before:
            self.assertEqual(before.xref_get_key(before[0].xref, "Contents")[0], "xref"); shown = render(before)
            pymupdf.TOOLS.mupdf_warnings(reset=True)                                  # scan() 의 글꼴 경고는 여기 것이 아님
            for strip in (True, False):
                st, done = self.make(src, reading(truth, False), False, strip_old=strip)
                self.assertEqual((st["pages"], st["mismatch"]), (1, []))
                self.assertEqual(render(done), shown)                                  # 그림이 그대로 보인다(백지 아님)
                self.assertEqual(len(done[0].search_for(HLINES[0][:3])), 1)
                refs = TL.content_refs(done, done[0])
                self.assertTrue(refs and all(done.xref_is_stream(x) for x in refs), refs)  # 배열 안은 모두 흐름
                fonts = [f for f in done[0].get_fonts(full=True) if f[3] == TL.FONT_NAME]
                self.assertEqual(len(fonts), 1)
                name, ext, _, buf, *_ = done.extract_font(fonts[0][0])
                self.assertEqual((ext, buf[:4]), ("ttf", b"\x00\x01\x00\x00"))      # 글꼴 파일이 내장됨
                self.assertEqual(pymupdf.TOOLS.mupdf_warnings(), "")

    def test_strip_old_keeps_links(self):
        pdf, truth = scan(self.tmp, HLINES[:1], vertical=False, name="link")
        doc = pymupdf.open(pdf); doc[0].insert_text((40, 88), "香蕾", fontname="china-t", fontsize=20, render_mode=3)
        doc[0].insert_link({"kind": pymupdf.LINK_URI, "from": pymupdf.Rect(10, 10, 100, 40), "uri": "https://example.org/a"})
        doc[0].insert_link({"kind": pymupdf.LINK_GOTO, "from": pymupdf.Rect(10, 50, 100, 80), "page": 0, "to": pymupdf.Point(0, 0)})
        old = self.tmp / "link.ocr.pdf"; doc.save(old); doc.close()
        with pymupdf.open(old) as before:
            want = [(l["kind"], tuple(round(v, 1) for v in l["from"]), l.get("uri")) for l in before[0].get_links()]
        self.assertEqual(len(want), 2)
        st, done = self.make(old, reading(truth, False), False)
        self.assertEqual((st["stripped"], st["links_kept"]), (1, 2))
        self.assertEqual([(l["kind"], tuple(round(v, 1) for v in l["from"]), l.get("uri")) for l in done[0].get_links()], want)
        self.assertNotIn("香蕾", done[0].get_text())                           # 옛 층은 지워졌다
        for rot in (90, 270):                                                 # 돌린 쪽에서도 링크 자리가 그대로
            with self.subTest(rot=rot):
                d = pymupdf.open(); pg = d.new_page(width=300, height=400); pg.set_rotation(rot)
                pg.insert_text((40, 88), "hidden", fontsize=20, render_mode=3)
                pg.insert_link({"kind": pymupdf.LINK_URI, "from": pymupdf.Rect(10, 10, 100, 40), "uri": "https://example.org/r"})
                d = pymupdf.open("pdf", d.tobytes()); pg = d[0]
                before = [(l["kind"], tuple(round(v, 1) for v in l["from"])) for l in pg.get_links()]
                self.assertEqual(TL.strip_hidden_text(pg), (True, 1))
                d = pymupdf.open("pdf", d.tobytes(garbage=3))
                self.assertEqual([(l["kind"], tuple(round(v, 1) for v in l["from"])) for l in d[0].get_links()], before)
                self.assertEqual(d[0].get_text().strip(), "")

    def test_lone_surrogate_in_reading_is_dropped_and_counted(self):
        pdf, truth = scan(self.tmp, HLINES[:1], vertical=False, name="surr")
        pr = reading(truth, False)
        pr.regions[0].paragraphs[0].columns[0].text = HLINES[0][:4] + "\ud83d" + HLINES[0][4:]       # JSON 에서 반 쪽만 남은 이모지
        st, done = self.make(pdf, pr, False)
        self.assertEqual((st["pages"], st["bad_chars"], st["mismatch"]), (1, 1, []))
        self.assertEqual("".join(TL.own_text(done, 1)).replace(" ", ""), HLINES[0])

    def test_keep_old_layer_strips_only_our_previous_layer(self):
        pdf, truth = scan(self.tmp, HLINES[:1], vertical=False, name="own")
        doc = pymupdf.open(pdf); doc[0].insert_text((40, 88), "香蕾", fontname="china-t", fontsize=20, render_mode=3)
        old = self.tmp / "own.ocr.pdf"; doc.save(old); doc.close()
        st, first = self.make(old, reading(truth, False), False, strip_old=False)
        self.assertEqual((st["pages"], st["own_stripped"]), (1, 0))
        first_pdf = self.tmp / "own_first.pdf"; first.save(first_pdf)
        pr = reading(truth, False)
        pr.regions[0].paragraphs[0].columns[0].text = new = "香薷味辛微溫主霍亂"                # 글자가 다른 새 판독(같은 수): 옛 층이 남으면 그 글자는 깨진다
        st, done = self.make(first_pdf, pr, False, strip_old=False)
        self.assertEqual((st["pages"], st["stripped"], st["own_stripped"], st["mismatch"]), (1, 0, 1, []))
        self.assertEqual("".join(TL.own_text(done, 1)).replace(" ", ""), new)      # 이전의 이 글자층은 사라지고 새 판독만
        self.assertIn("香蕾", text_layer(done, 1).plain())                          # 다른 OCR 층은 그대로
        with pymupdf.open(old) as before:
            self.assertEqual(render(done), render(before))
        with pymupdf.open(first_pdf) as d:                                          # 흐름이 하나로 합쳐진 꼴은 가려낼 수 없어 건너뛴다(깨뜨리지 않는다)
            page = d[0]; refs = [int(x) for x in __import__("re").findall(r"(\d+) 0 R", d.xref_get_key(page.xref, "Contents")[1])]
            merged = b"".join(d.xref_stream(x) for x in refs); d.update_stream(refs[0], merged)
            d.xref_set_key(page.xref, "Contents", f"{refs[0]} 0 R")
            merged_pdf = self.tmp / "own_merged.pdf"; d.save(merged_pdf)
        with pymupdf.open(merged_pdf) as d:
            img, _ = page_image(d, 1)
            st = TL.searchable_pdf(d, [1], lambda n: (pr, img, page_frame(d, 1), False), self.tmp / "own_skip.pdf", strip_old=False, log=lambda *a: None)
        self.assertEqual(st["pages"], 0); self.assertIn("가려내", st["skipped"][1]); self.assertFalse((self.tmp / "own_skip.pdf").exists())


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
        pymupdf.TOOLS.mupdf_warnings(reset=True)
        with self.write(doc, [line]) as out:
            self.assertEqual(pymupdf.TOOLS.mupdf_warnings(), "")               # 자원을 쪽에 붙인 뒤 내용을 감싸므로 'cannot find XObject' 가 없다
            self.assertEqual(render(out), before)
            self.assertEqual("".join(TL.own_text(out, 1)), "天地玄")


class WritingMore(unittest.TestCase):
    """글꼴 자원 이름 겹침(자원을 같이 쓰는 쪽)과 전각 간격으로 찍힌 반각 글자."""

    def test_shared_resources_get_a_fresh_font_key_so_other_pages_stay_readable(self):
        with tempfile.TemporaryDirectory() as td:
            d = pymupdf.open(); p = d.new_page(width=120, height=80); p.draw_rect(pymupdf.Rect(10, 10, 60, 40), color=(0, 0, 0), fill=(0, 0, 0))
            png = Path(td) / "a.png"; p.get_pixmap(dpi=100).save(png); d.close()
            doc = pymupdf.open("pdf", images_to_pdf([png, png], default_dpi=100))
        res = doc.xref_get_key(doc[0].xref, "Resources")
        pages_xref = int(doc.xref_get_key(doc.pdf_catalog(), "Pages")[1].split()[0])
        doc.xref_set_key(pages_xref, "Resources", res[1])                        # 두 쪽이 자원 하나를 물려받는다
        for pg in doc: doc.xref_set_key(pg.xref, "Resources", "null")
        doc = pymupdf.open("pdf", doc.tobytes())
        plan = lambda txt: TL.Line("h", [(c, (0.1 + i * 0.1, 0.4, 0.2 + i * 0.1, 0.5)) for i, c in enumerate(txt)], "ink")
        font, cids = TL.add_font(doc, "天地玄黃")
        for pno, txt in ((0, "天地玄"), (1, "黃地天")):
            TL.write_lines(doc, doc[pno], [plan(txt)], (0, 0, 120, 80), font, cids)
        doc = pymupdf.open("pdf", doc.tobytes(garbage=3, deflate=True))
        self.assertEqual((TL.own_text(doc, 1), TL.own_text(doc, 2)), (["天地玄"], ["黃地天"]))
        self.assertTrue(TL.has_own_layer(doc[0]))
        self.assertTrue(TL.strip_own_layer(doc, doc[0]))                           # 1쪽의 이 글자층만 빼고 다른 글꼴(다른 CID)로 다시 넣는다
        font, cids = TL.add_font(doc, "宇宙")
        TL.write_lines(doc, doc[0], [plan("宇宙")], (0, 0, 120, 80), font, cids)
        doc = pymupdf.open("pdf", doc.tobytes(garbage=3, deflate=True))
        self.assertEqual((TL.own_text(doc, 1), TL.own_text(doc, 2)), (["宇宙"], ["黃地天"]))   # 2쪽(건드리지 않은 쪽)의 글자가 깨지지 않는다
        self.assertEqual(sorted(f[4] for f in doc[0].get_fonts()), [TL.FONT_KEY, TL.FONT_KEY + "2"])

    def test_half_width_digits_at_full_width_pitch_stay_joined_and_in_place(self):
        for d_, mids in (("h", [0.15, 0.25, 0.352, 0.455]), ("v", [0.15, 0.25, 0.352, 0.455]), ("h", [0.15, 0.25, 0.325, 0.375])):
            with self.subTest(dir=d_, mids=mids):                                   # 숫자가 전각 간격(1.03칸)으로 — 또는 반각 둘이 한 칸에
                boxes = [((m - 0.05, 0.4, m + 0.05, 0.5) if d_ == "h" else (0.4, m - 0.05, 0.5, m + 0.05)) for m in mids]
                doc = pymupdf.open(); doc.new_page(width=300, height=300)
                font, cids = TL.add_font(doc, "卷第12")
                TL.write_lines(doc, doc[0], [TL.Line(d_, list(zip("卷第12", boxes)), "ink")], (0, 0, 300, 300), font, cids)
                out = pymupdf.open("pdf", doc.tobytes())
                self.assertEqual(TL.own_text(out, 1), ["卷第12"])                      # '卷第1 2' 가 아니다
                self.assertEqual(len(out[0].search_for("卷第12")), 1)
                got = [l for l in layer_chars(out, 1, own=True) if l["chars"]][0]["chars"]
                for (c, bb), m in zip(got, mids):
                    self.assertAlmostEqual(((bb[0] + bb[2]) if d_ == "h" else (bb[1] + bb[3])) / 2 / 300, m, delta=0.02, msg=c)


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

    def test_pdf_run_progress_tracks_pdf_output_after_ocr(self):
        from yeone_ocr.progress import Tracker
        tracker = Tracker("run-pdf")
        tracker.update("read", 2, 2)
        tracker.update("adjudicate", 2, 2)
        start = tracker.frac
        tracker.update("textlayer", 1, 2)
        self.assertGreater(tracker.frac, start)
        self.assertIn("글자층 넣기", tracker.text())
        tracker.update("textlayer", 2, 2)
        tracker.update("review", 1, 1)
        tracker.update("report", 1, 1)
        self.assertAlmostEqual(tracker.frac, 1.0)


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
