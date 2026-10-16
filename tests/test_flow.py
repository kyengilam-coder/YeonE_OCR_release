"""내용별 문서(--layout flow): 쪽 책갈피 조립, 本草 조문 짜임 규칙, 획 굵기(本經·別錄), 양식 HWPX 채우기. 모델 호출 없음."""
import contextlib
import io
import re
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pymupdf

from yeone_ocr.blocks import Block, assemble_blocks
from yeone_ocr.pdfio import images_to_pdf, page_frame, page_image
from yeone_ocr.schema import PageReading, frac_bbox
from yeone_ocr.structure import apply_structure, bold_spans, measure_bold

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _hwpx_fixtures import make_template, make_template2, rec, text_region   # noqa: E402  양식 흉내·판독 기록 도우미(골든 시험과 같이 씀)

HW = "{http://www.hancom.co.kr/hwpml/2011/head}"
HP = "{http://www.hancom.co.kr/hwpml/2011/paragraph}"


class FlowAssembly(unittest.TestCase):
    def test_pages_become_bookmarks_and_split_paragraph_is_joined(self):
        r1 = rec(5, [{"id": "t", "k": "제목", "title": "菜　下", "lv": 2, "bbox": frac_bbox((0.3, 0.05, 0.7, 0.1))},
                     text_region("a", ["香薷　味辛，微温。主霍乱腹痛"])], print_page="394")
        r2 = rec(6, [text_region("b", ["吐下，散水肿。"], cont=True), text_region("c", ["［陶隐居云］　处处有此。"])], print_page="395")
        page = assemble_blocks([r1, r2], grades=False)
        flow = assemble_blocks([r1, r2], grades=False, layout="flow")
        self.assertEqual([b.type for b in page].count("meta"), 2)
        self.assertNotIn("meta", [b.type for b in flow])
        page_para = next(b for b in page if b.type == "para")
        self.assertEqual(page_para.text.index("〔→PDF 6쪽/인쇄 395쪽〕"), len("香薷　味辛，微温。主霍乱腹痛"))   # 원본 쪽대로는 이은 자리에 쪽 경계 표시
        joined = next(b for b in flow if b.type == "para")
        self.assertEqual(joined.text, "香薷　味辛，微温。主霍乱腹痛吐下，散水肿。")                # 쪽을 넘은 문단을 표시 없이 잇는다
        self.assertEqual(flow[0].anchors, [(0, "PDF 5쪽 · 인쇄 394쪽")])
        self.assertEqual(joined.anchors, [(len("香薷　味辛，微温。主霍乱腹痛"), "PDF 6쪽 · 인쇄 395쪽")])
        with self.assertRaises(ValueError):
            assemble_blocks([r1], layout="pages")

    def test_legend_memo_goes_to_first_text_and_pages_without_text_carry_over(self):
        r1 = rec(1, [{"id": "f", "k": "삽화", "bbox": frac_bbox((0.1, 0.1, 0.9, 0.9))}])
        r2 = rec(2, [text_region("a", ["天地玄黃"])])
        blocks = assemble_blocks([r1, r2], grades=True, layout="flow")
        para = next(b for b in blocks if b.type == "para")
        self.assertEqual([a for _, a in para.anchors], ["PDF 1쪽", "PDF 2쪽"])         # 그림만 있는 쪽의 책갈피도 다음 글 첫머리에
        self.assertTrue(para.memos and para.memos[0][0] == 0 and "범례" in para.memos[0][1])


def title_region(rid: str, text: str, top: float = 0.03, level: int = 1) -> dict:
    return {"id": rid, "k": "제목", "bbox": frac_bbox((0.2, top, 0.8, top + 0.04)), "title": text, "lv": level}


class RunningHeads(unittest.TestCase):
    """쪽마다 찍힌 머리말(書眉)을 판독이 '제목' 으로 읽으면 내용별 문서에서 장 제목이 되어 글을 끊었다(개보본초 462쪽에서 제목-장 263개)."""
    def test_running_heads_are_dropped_and_the_paragraph_rejoins(self):
        def body(rid, t, cont=False, top=0.2):
            return text_region(rid, [t], cont=cont, box=(0.1, top, 0.9, top + 0.3))
        recs = [rec(1, [title_region("t", "開寶本草草部上品之上  卷第六", top=0.05), body("b", "青芝本經145 赤芝本經145")]),
                rec(2, [body("b", "黃芝本經145", cont=True), title_region("t", "芫花草草部上品之上卷第六")]),      # 영역 순서가 거꾸로·잘못 읽은 머리말
                rec(3, [title_region("t", "開寶本草草部上品之上卷第六"), body("b", "肉蓯蓉本經165", cont=True)]),
                rec(4, [title_region("t", "開寶本草草部上品之下  卷第七"), body("b", "地膚子本經165")]),          # 권이 바뀌면 새 장
                rec(5, [title_region("t", "雲母", top=0.5), body("b", "味甘，平。", top=0.6)])]
        st = {}
        blocks = assemble_blocks(recs, grades=False, layout="flow", stats=st)
        heads = [b.text for b in blocks if b.type == "heading"]
        self.assertEqual(heads, ["開寶本草草部上品之上  卷第六", "開寶本草草部上品之下  卷第七", "雲母"])
        self.assertEqual(st["running_heads"], 2)
        para = next(b for b in blocks if b.type == "para")
        self.assertEqual(para.text, "青芝本經145 赤芝本經145黃芝本經145肉蓯蓉本經165")                        # 머리말에 끊기지 않고 이어짐
        self.assertEqual([a for a in para.anchors], [(15, "PDF 2쪽"), (22, "PDF 3쪽")])                   # 쪽 책갈피는 그 쪽 첫 글자에
        recs[2]["merged"]["header"] = "開寶本草 卷第七"; recs[3]["merged"]["regions"][0]["title"] = "開寶本草卷第七"     # 앞 쪽 머리말 칸과 같으면 위치와 상관없이 되풀이 머리말
        self.assertNotIn("開寶本草卷第七", [b.text for b in assemble_blocks(recs, grades=False, layout="flow") if b.type == "heading"])
        self.assertIn("芫花草草部上品之上卷第六", [b.text for b in assemble_blocks(recs, grades=False) if b.type == "heading"])  # 원본 쪽대로는 그대로


    def test_similar_but_different_chapters_and_first_occurrences_are_kept(self):
        """검토에서 잡힌 것: 木部上品 → 木部中品(권 번호 없음), 木部 → 草部, 이 쪽 머리말 칸과 같은 첫 장 제목은 머리말이 아니다. 뺀 제목은 쪽과 함께 남는다."""
        def page(n, title, body, header=""):
            r = rec(n, [title_region("t", title, top=0.04), text_region("b", [body], box=(0.1, 0.2, 0.9, 0.9))])
            r["merged"]["header"] = header; return r
        recs = [page(1, "開寶本草木部上品", "龍眼 味甘，平。", header="開寶本草木部上品"),
                page(2, "開寶本草木部上品", "厚樸 味苦，溫。", header="開寶本草木部上品"),     # 되풀이 머리말
                page(3, "開寶本草木部中品", "桑根白皮 味甘，寒。", header="開寶本草木部中品"),  # 새 장(품급이 다름), 머리말 칸도 새 장
                page(4, "開寶本草草部中品", "當歸 味甘，溫。"),                            # 새 장(부가 다름)
                page(5, "開寳本草草部中品", "芎藭 味辛，溫。")]                             # 잘못 읽은 되풀이 머리말
        st = {}
        heads = [b.text for b in assemble_blocks(recs, grades=False, layout="flow", stats=st) if b.type == "heading"]
        self.assertEqual(heads, ["開寶本草木部上品", "開寶本草木部中品", "開寶本草草部中品"])
        self.assertEqual(st["running_head_texts"], [(2, "開寶本草木部上品"), (5, "開寳本草草部中品")])
        shang = [page(1, "辨太陽病脈證并治上", "太陽之為病"), page(2, "辨太陽病脈證并治上", "脈浮", header="辨太陽病脈證并治上"),
                 page(3, "辨太陽病脈證并治中", "太陽病，項背強"), page(4, "辨太陽病脈證并治下", "問曰"), page(5, "辨陽明病脈證并治", "陽明之為病"),
                 page(6, "婦人方第二", "求子"), page(7, "婦人方第三", "妊娠")]                                   # 끝 글자·第N 만 다른 형제 장(傷寒論·千金方)
        heads = [b.text for b in assemble_blocks(shang, grades=False, layout="flow") if b.type == "heading"]
        self.assertEqual(heads, ["辨太陽病脈證并治上", "辨太陽病脈證并治中", "辨太陽病脈證并治下", "辨陽明病脈證并治", "婦人方第二", "婦人方第三"])

    def test_join_rules_small_text_and_check_list(self):
        """cont 문단은 소주는 소주끼리·본문은 본문끼리만 잇고, 쪽 끝 확인 목록 블록은 건너뛰어 잇는다. 글 없는 제목 영역은 문단이 되지 않는다."""
        recs = [rec(1, [text_region("b", ["本文一"], box=(0.1, 0.1, 0.9, 0.5)), text_region("s", ["小注"], kind="소주", box=(0.1, 0.5, 0.9, 0.6)),
                        text_region("c", ["本文二"], cont=True, box=(0.1, 0.6, 0.9, 0.9))]),
                rec(2, [title_region("t", ""), text_region("b", ["本文三"], cont=True)])]
        recs[0]["merged"]["unc"] = [{"r": "b", "n": 1, "pos": "위", "ctx": "「本〔文/門〕」"}]
        out = assemble_blocks(recs, grades=False, layout="flow", check_list="all")
        paras = [(b.kind, b.text) for b in out if b.type == "para"]
        self.assertEqual(paras, [("본문", "本文一"), ("소주", "小注"), ("본문", "本文二本文三")])   # 소주에 붙지 않고, 확인 목록을 건너 2쪽 글을 이음
        self.assertEqual([b.type for b in out if b.type != "para"], ["uncertain"])                 # 빈 제목은 없음
        self.assertEqual([b for b in out if b.type == "para"][-1].anchors, [(3, "PDF 2쪽")])


class HeadingEntries(unittest.TestCase):
    """판독이 약재명을 제목으로 읽은 블록: 다음 문단이 味·一名 으로 시작하면 약재명, 제목에 味… 가 붙었으면 떼어 다음 문단 앞에 잇는다."""
    def test_herb_headings_become_entries(self):
        blocks = [Block(type="heading", text="開寶本草玉石等部中品 卷第四", level=1, kind="제목"),
                  Block(type="heading", text="雄黃", level=1, kind="제목"),
                  Block(type="para", text="〔疑: 雄黄〕味苦，甘，平。主寒熱。", kind="본문"),
                  Block(type="heading", text="孔公孽　味辛，溫，無毒。", level=1, kind="제목"),
                  Block(type="para", text="主傷食不化。", kind="본문", spans=[(1, 2, "green")], anchors=[(0, "PDF 9쪽")]),
                  Block(type="heading", text="附子\u2003味辛·甘，溫。", level=2, kind="제목"),
                  Block(type="para", text="〔陶隱居云〕此物。", kind="본문"),
                  Block(type="heading", text="吐唾血", level=2, kind="제목"),
                  Block(type="para", text="生地黃『本經』寒。", kind="본문"),
                  Block(type="heading", text="菜　下", level=2, kind="제목"),
                  Block(type="para", text="香薷　味辛，微溫。", kind="본문")]
        out, st = apply_structure(blocks, "auto")
        got = [(b.type, b.role, b.text) for b in out]
        self.assertEqual(got, [("heading", "chapter", "開寶本草玉石等部中品 卷第四"),
                               ("heading", "entry", "雄黃"), ("para", "body", "〔疑: 雄黄〕味苦，甘，平。主寒熱。"),
                               ("heading", "entry", "孔公孽"), ("para", "body", "味辛，溫，無毒。主傷食不化。"),
                               ("heading", "entry", "附子"), ("para", "body", "味辛·甘，溫。"), ("para", "commentary", "〔陶隱居云〕此物。"),
                               ("heading", "section", "吐唾血"), ("para", "body", "生地黃『本經』寒。"),   # 序例 병증 제목 뒤 生… 은 약재명이 아님
                               ("heading", "section", "菜　下"), ("heading", "entry", "香薷"), ("para", "body", "味辛，微溫。")])  # 다음 문단이 제 이름으로 시작(굵은 글자 없는 문서 → 본문)
        joined = out[4]
        self.assertEqual((joined.spans, joined.anchors), ([(9, 10, "green")], [(8, "PDF 9쪽")]))      # 이은 문단의 색·책갈피 자리도 함께 밀림
        self.assertEqual(st["entry"], 4)


    def test_heading_entry_needs_matching_prefix_and_marks_do_not_fool_comment_rule(self):
        blocks = [Block(type="heading", text="菜　下", level=2, kind="제목"), Block(type="para", text="香薷味辛，微溫。", kind="본문"),   # 빈칸 빠진 다른 약재명
                  Block(type="heading", text="雄黃", level=1, kind="제목"), Block(type="para", text="〔疑: 雄黄〕味苦。", kind="본문"),  # 간체로 되읽은 같은 이름
                  Block(type="heading", text="點青", level=1, kind="제목"), Block(type="para", text="〔疑: 扁青〕味甘。", kind="본문"),  # 한 글자 달리 읽은 같은 이름
                  Block(type="heading", text="黃連", level=1, kind="제목"), Block(type="heading", text="黃芩", level=1, kind="제목"),
                  Block(type="para", text="味苦，平。", kind="본문"),                                                               # 약재명 둘이 잇달아 제목으로
                  Block(type="para", text="〔日/曰〕本經曰味甘。", kind="본문"), Block(type="para", text="〔疑: 注〕云味甘。", kind="본문"),
                  Block(type="para", text="〔陶隱居云〕此物。", kind="본문")]
        out, st = apply_structure(blocks, "bencao")
        self.assertEqual([(b.role, b.text) for b in out], [("section", "菜　下"), ("body", "香薷味辛，微溫。"),
                         ("entry", "雄黃"), ("body", "〔疑: 雄黄〕味苦。"), ("entry", "點青"), ("body", "〔疑: 扁青〕味甘。"),
                         ("entry", "黃連"), ("entry", "黃芩"), ("body", "味苦，平。"),
                         ("body", "〔日/曰〕本經曰味甘。"), ("body", "〔疑: 注〕云味甘。"), ("commentary", "〔陶隱居云〕此物。")])

    def test_entry_name_with_marks_function_words_unmeasured_bold_and_page_split(self):
        blocks = [Block(type="para", text="〔黄/黃〕連 味苦，寒。", kind="본문", spans=[(7, 8, "green")]),
                  Block(type="para", text="陶隱居云 味苦。", kind="본문"), Block(type="para", text="本經 味甘。", kind="본문"),
                  Block(type="para", text="主心痛。久服輕身。苦瓠 味苦，寒。主大水。〔唐本注云〕今處處有。", kind="본문",
                        anchors=[(9, "PDF 2쪽"), (21, "PDF 3쪽")])]
        out, st = apply_structure(blocks, "bencao", bold_measured=False)
        self.assertEqual([(b.type, b.role, b.text) for b in out],
                         [("heading", "entry", "〔黄/黃〕連"), ("para", "body", "味苦，寒。"),                    # 표시가 든 이름도 떼고, 굵기를 안 쟀으면 본문
                          ("para", "body", "陶隱居云 味苦。"), ("para", "body", "本經 味甘。"),                   # 云·經 으로 끝나는 토막은 약재명이 아님
                          ("para", "body", "主心痛。久服輕身。"), ("heading", "entry", "苦瓠"), ("para", "body", "味苦，寒。主大水。"),
                          ("para", "commentary", "〔唐本注云〕今處處有。")])                                      # 쪽 첫머리의 새 조문·주석은 가른다
        self.assertEqual((st["page_split"], out[1].spans, out[6].anchors), (2, [(0, 1, "green")], []))
        self.assertEqual(out[5].anchors, [(0, "PDF 2쪽")]); self.assertEqual(out[7].anchors, [(0, "PDF 3쪽")])
        out2, _ = apply_structure(blocks[:1] + [Block(type="para", text="本文。", kind="본문")] * 300, "auto")
        self.assertEqual(out2[0].role, "body")                                                                  # auto: 셋 미만이면 떼지 않음
        hit = [Block(type="para", text=f"藥{c} 味苦。", kind="본문") for c in "甲乙丙"]
        self.assertEqual(sum(1 for b in apply_structure(hit + [Block(type="para", text="本文。", kind="본문")] * 400, "auto")[0] if b.role == "entry"), 0)   # 1% 미만
        self.assertEqual(sum(1 for b in apply_structure(hit + [Block(type="para", text="本文。", kind="본문")] * 100, "auto")[0] if b.role == "entry"), 3)


class Structure(unittest.TestCase):
    def para(self, text: str, **kw) -> Block:
        return Block(type="para", text=text, kind=kw.pop("kind", "본문"), **kw)

    def test_bencao_entry_commentary_and_source(self):
        blocks = [self.para("香薷　味辛，微温。主霍乱腹痛吐下，散水肿。", anchors=[(0, "PDF 1쪽")], memos=[(0, "고지")],
                            spans=[(5, 6, "green")]),
                  self.para("［陶隐居云］　处处有此，惟供生食。"),
                  self.para("秦荻梨　味辛，温，无毒。所在有之。　唐附"),
                  self.para("中，今人食之，录在菜部也。"),
                  Block(type="heading", text="菜　下", level=2)]
        out, stats = apply_structure(blocks, "bencao")
        self.assertEqual([(b.type, b.role, b.text) for b in out[:2]],
                         [("heading", "entry", "香薷"), ("para", "body", "味辛，微温。主霍乱腹痛吐下，散水肿。")])    # 굵은 범위가 하나도 없는 문서는 本經·別錄을 가르지 않는다
        self.assertEqual(out[0].anchors, [(0, "PDF 1쪽")]); self.assertEqual(out[0].memos, [(0, "고지")])
        self.assertEqual(out[1].spans, [(2, 3, "green")])                        # 떼어 낸 만큼 등급 범위도 옮긴다(微)
        self.assertEqual(out[2].role, "commentary")
        src = out[4]
        self.assertEqual([src.text[a:e] for a, e, k in src.cstyles if k == "source"], ["唐附"])
        self.assertEqual(out[5].role, "body"); self.assertEqual(out[6].role, "section")
        self.assertEqual(stats["entry"], 2)
        self.assertEqual("".join(b.text for b in out).replace("　", ""), "".join(b.text for b in blocks).replace("　", ""))  # 글자는 그대로

    def test_other_books_do_not_get_herb_entries_and_entry_paths_agree(self):
        """범용성 검토에서 잡힌 것: 방제서의 약재 목록(수량사)·허사 첫머리·序例 목록은 약재명이 아니고, 굵은 글자가 없는 책은 모두 본문,
        제목으로 읽힌 약재명 뒤 조문은 떼어 낸 조문과 같은 규칙, 긴 주석 표지와 開寶의 今詳·今據, 출처 新補·陳藏器余(간체 今余 는 아님)."""
        from yeone_ocr.presets import resolve_preset
        p = resolve_preset("kaibao"); COMMENT_RE, SOURCE_RE = p.comment_re, p.source_re                     # 예전 structure 상수 → 짜임 규칙
        recipe = [self.para("桂枝三兩 生薑三兩 大棗十二枚"), self.para("桂枝三兩 味辛"), self.para("麻黃三錢 性溫"),
                  self.para("凡藥 性有宜丸者"), self.para("吐唾血　生地黃　大黃")]
        out, st = apply_structure(recipe, "bencao")
        self.assertEqual([b.role for b in out], ["body"] * 5); self.assertNotIn("entry", st)
        plain = [self.para(f"{n}　味甘，平。") for n in ("水斳", "落葵", "繁蔞")]                         # 本經을 굵게 찍지 않은 책(神農本草經 輯本)
        self.assertEqual([b.role for b in apply_structure(plain, "bencao", bold_measured=True)[0] if b.type == "para"], ["body"] * 3)
        bold = [self.para("苦瓠　味苦寒有毒", cstyles=[(3, 8, "bold")]), Block(type="heading", text="水斳", level=1, kind="제목"),
                self.para("味甘，平。"), self.para("落葵　味酸，寒。")]
        self.assertEqual([b.role for b in apply_structure(bold, "bencao")[0]], ["entry", "bonjing", "entry", "bielu", "entry", "bielu"])
        self.assertTrue(COMMENT_RE.match("［臣禹錫等謹按陳藏器本草云］處處有")); self.assertTrue(COMMENT_RE.match("［今詳］此物")); self.assertTrue(COMMENT_RE.match("［今據］此物"))
        self.assertEqual(SOURCE_RE.search("主治。　新補").group(1), "新補"); self.assertEqual(SOURCE_RE.search("录在菜部。陈藏器余").group(1), "陈藏器余")
        self.assertIsNone(SOURCE_RE.search("其事，今余")); self.assertEqual(SOURCE_RE.search("所在有之。　唐附").group(1), "唐附")

    def test_measure_bold_without_opencv_measures_nothing_and_entries_stay_body(self):
        import importlib.util
        from unittest import mock
        real = importlib.util.find_spec; logs = []
        with mock.patch.object(importlib.util, "find_spec", lambda name, *a, **k: None if name == "cv2" else real(name, *a, **k)):
            bold, info = measure_bold(iter(()), log=logs.append)
        self.assertEqual((bold, info["chars"]), ({}, 0)); self.assertTrue(logs and "opencv" in logs[0])
        out, _ = apply_structure([self.para("水斳　味甘，平。")], "bencao", bold_measured=bool(bold) and info["chars"] > 0)
        self.assertEqual([x.role for x in out], ["entry", "body"])                       # 재지 못했으면 별록 꼬리표가 아니라 본문

    def test_entry_without_space_uses_measured_gap_and_auto_needs_three(self):
        b = self.para("苦瓠味苦，寒，有毒。", cstyles=[(2, 2, "gap")])
        out, _ = apply_structure([b], "bencao")
        self.assertEqual([x.text for x in out], ["苦瓠", "味苦，寒，有毒。"])
        out, _ = apply_structure([b], "auto")                                     # 한 번뿐이면 우연한 꼴일 수 있어 떼지 않는다
        self.assertEqual([x.text for x in out], ["苦瓠味苦，寒，有毒。"])
        three = [self.para(f"{n}　味甘，平。") for n in ("水斳", "落葵", "繁蔞")]
        out, _ = apply_structure(three, "auto")
        self.assertEqual([x.role for x in out].count("entry"), 3)
        out, _ = apply_structure(three, "none")
        self.assertEqual([x.role for x in out], ["body"] * 3)

    def test_bold_ratio_decides_bonjing_and_keeps_mixed_ranges(self):
        t = "苦瓠　味苦寒有毒主大水面目四肢浮腫下水"
        full = self.para(t, cstyles=[(3, len(t), "bold")])
        half = self.para(t, cstyles=[(3, 9, "bold")])
        out_full, _ = apply_structure([full], "bencao")
        out_half, _ = apply_structure([half], "bencao")
        self.assertEqual(out_full[1].role, "bonjing"); self.assertEqual(out_full[1].cstyles, [])
        self.assertEqual(out_half[1].role, "bielu")
        self.assertEqual([(a, e, k) for a, e, k in out_half[1].cstyles], [(0, 6, "bonjing")])

    def test_bold_spans_smooth_noise_and_merge_short_runs(self):
        thin, thick = 3.0, 4.6
        ws = [thick] * 6 + [3.1, 2.6] + [thick] * 6 + [thin] * 10 + [4.2] + [thin] * 5
        spans = bold_spans({("r", 0): [(i, w) for i, w in enumerate(ws)]}, median=3.1)
        self.assertEqual(spans, {("r", 0): [(0, 14)]})                            # 가운데 두 글자의 흔들림·뒤의 낱글자는 무시


def bold_scan(tmp: Path) -> tuple[Path, PageReading]:
    """보통 글씨 두 문단 + 굵은 글씨(획을 덧그림) 한 문단의 가로쓰기 쪽."""
    W, H, fs = 420, 300, 18
    lines = [("香薷　味辛微温主霍乱腹痛吐下散水肿", False), ("苦瓠　味苦寒有毒主大水面目四肢浮肿", True), ("秦荻梨　味辛温无毒主心腹冷胀下气消食", False)]
    d = pymupdf.open(); p = d.new_page(width=W, height=H)
    regions = []
    for i, (t, bold) in enumerate(lines):
        y = 60 + i * 60
        kw = dict(render_mode=2, border_width=0.9 / fs, color=(0, 0, 0), fill=(0, 0, 0)) if bold else {}   # 획 테두리 0.9pt(글자 크기에 대한 비)
        p.insert_text((30, y + fs * 0.88), t, fontname="china-s", fontsize=fs, **kw)
        regions.append(text_region(f"r{i}", [t], box=((30 - 4) / W, (y - 4) / H, (30 + len(t) * fs + 4) / W, (y + fs + 4) / H)))
    png = tmp / "bold.png"; p.get_pixmap(dpi=200, colorspace=pymupdf.csGRAY).save(png); d.close()
    pdf = tmp / "bold.pdf"; pdf.write_bytes(images_to_pdf([png], default_dpi=200))
    return pdf, PageReading.model_validate({"pdf_page": 1, "regions": regions})


class Bold(unittest.TestCase):
    def test_measure_bold_finds_only_the_thick_paragraph_and_entry_gaps(self):
        with tempfile.TemporaryDirectory() as td:
            pdf, pr = bold_scan(Path(td))
            with pymupdf.open(pdf) as d:
                img, _ = page_image(d, 1); frame = page_frame(d, 1)
            bold, info = measure_bold([(1, pr, img, frame, False)])
        kinds = {key: {k for _, _, k in v} for key, v in bold.items()}
        self.assertEqual([key for key, ks in kinds.items() if "bold" in ks], [(1, "r1", 0)])
        self.assertTrue(all("gap" in kinds.get((1, f"r{i}", 0), set()) for i in range(3)), kinds)   # 약재명 뒤 빈칸
        self.assertEqual(info["bold_paragraphs"], 1)
        from yeone_ocr.structure import _box_gap, _ink, _ink_gap
        from yeone_ocr.textlayer import plan_page
        lines = plan_page(pr, img, frame, None, False)                              # 상자를 못 맞춘 줄(ink~)에 쓰는 먹 빈칸 재기도 같은 답
        self.assertEqual([_ink_gap(_ink(img), ln) for ln in lines], [_box_gap(ln) for ln in lines])
        self.assertEqual([_box_gap(ln) for ln in lines], [2, 2, 3])
        blocks = assemble_blocks([rec(1, [r.model_dump() for r in pr.regions])], grades=False, layout="flow", bold=bold)
        out, _ = apply_structure(blocks, "bencao")
        self.assertEqual([(b.role, b.text[:3]) for b in out if b.type == "para"], [("bielu", "味辛微"), ("bonjing", "味苦寒"), ("bielu", "味辛温")])


class Template(unittest.TestCase):
    @staticmethod
    def body_runs(p) -> list:
        """문단의 본문 런(쪽 설정·머리말·꼬리말·단 컨트롤만 든 런은 빼고)."""
        return [r for r in p.findall(f"{HP}run") if r.find(f"{HP}secPr") is None
                and all(r.find(f"{HP}ctrl/{HP}{k}") is None for k in ("header", "footer", "colPr"))]

    @classmethod
    def body_text(cls, p) -> str:
        return "".join(t.text or "" for r in cls.body_runs(p) for t in r.iter(f"{HP}t"))

    def test_template_styles_bookmarks_break_word_and_clean_body(self):
        from yeone_ocr.hwpx_template import build_hwpx_template
        from yeone_ocr.verify import check_hwpx_file
        with tempfile.TemporaryDirectory() as td:
            tpl = Path(td) / "양식.hwpx"; make_template(tpl)
            blocks = [Block(type="heading", text="菜　下", level=2, role="section", anchors=[(0, "PDF 1쪽 · 인쇄 394쪽")]),
                      Block(type="heading", text="香薷", level=3, role="entry"),
                      Block(type="para", text="味辛，微温。主霍乱〔腹/腸〕痛吐下，散水肿。", role="bielu", anchors=[(9, "PDF 2쪽")]),
                      Block(type="para", text="［唐本注云］　功用相似。　唐附", role="commentary", cstyles=[(13, 15, "source")])]
            out = Path(td) / "out.hwpx"
            st = build_hwpx_template(blocks, str(out), str(tpl))
            self.assertEqual(check_hwpx_file(str(out)), [])
            self.assertEqual((st["bookmarks"], st["images_in_file"], st["marks"]), (2, 0, 1))
            self.assertEqual(st["styles"]["section"], "제목-절"); self.assertEqual(st["styles"]["commentary"], "주석")
            self.assertIn("entry", st["missing"]); self.assertNotIn("bielu", st["missing"])  # 양식에 없는 것은 바탕글로
            self.assertEqual(st["styles"]["bielu"], "본문")                                 # '별록원문'이 없으면 다음 후보 '본문'으로
            with zipfile.ZipFile(out) as z:
                sec = z.read("Contents/section0.xml").decode("utf-8"); head = z.read("Contents/header.xml").decode("utf-8")
        names = dict(re.findall(r'<hh:style id="(\d+)" type="\w+" name="([^"]*)"', head))
        paras = ET.fromstring(sec).findall(f"{HP}p")                                # 맨 위 문단만(꼬리말 안 문단은 빼고)
        self.assertNotIn("양식 본문", sec)
        self.assertIsNotNone(paras[0].findall(f"{HP}run")[0].find(f"{HP}secPr")); self.assertIsNotNone(paras[0].find(f".//{HP}pageNum"))
        self.assertEqual(self.body_text(paras[0]), "菜　下")                          # 쪽 설정·쪽 번호는 첫 제목 문단 첫 런에 — 맨 앞 빈 줄이 없다
        body = paras
        self.assertEqual([names[q.get("styleIDRef")] for q in body], ["제목-절", "바탕글", "본문", "주석"])
        marks = lambda q: [("bm", e.get("name")) if e.tag == f"{HP}bookmark" else ("t", e.text) for r in self.body_runs(q) for e in r.iter() if e.tag in (f"{HP}bookmark", f"{HP}t")]
        self.assertEqual(marks(body[0])[0], ("bm", "PDF 1쪽 · 인쇄 394쪽"))
        m3 = marks(body[2])
        self.assertLess(m3.index(("bm", "PDF 2쪽")), m3.index(("t", "〔腹/腸〕")))       # 책갈피는 그 글자 자리(9번째) 앞에
        ppr = body[3].get("paraPrIDRef")                                            # 주석(본문 바탕 KEEP_WORD) → 글자 단위 사본
        self.assertRegex(head, rf'<hh:paraPr id="{ppr}"(?:(?!</hh:paraPr>).)*breakLatinWord="BREAK_WORD"(?:(?!</hh:paraPr>).)*breakNonLatinWord="BREAK_WORD"')   # 반각 ,. 이 든 한자 덩어리도 글자 단위로
        src_run = next(r for r in body[3].findall(f"{HP}run") if (r.findtext(f"{HP}t") or "") == "唐附")
        char_of_source = re.search(r'<hh:style id="\d+" type="CHAR" name="출처"[^>]*charPrIDRef="(\d+)"', head).group(1)
        self.assertEqual(src_run.get("charPrIDRef"), char_of_source)

    def test_numbered_outline_paragraphs_are_still_flagged(self):
        from hwpx import HwpxDocument
        from yeone_ocr.verify import check_hwpx_file
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "outline.hwpx"
            doc = HwpxDocument.new(); doc.add_heading("제목", level=1); doc.save_to_path(str(out)); doc.close()
            self.assertTrue(any(i["rule"] == "개요 문단" for i in check_hwpx_file(str(out))))

    def test_numbered_paragraph_shape_with_id_written_last_is_flagged(self):
        from hwpx import HwpxDocument
        from yeone_ocr.verify import check_hwpx_file
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "number.hwpx"
            doc = HwpxDocument.new(); h = doc.oxml.headers[0]
            pid = h.ensure_paragraph_format(base_para_pr_id=0, heading={"type": "NUMBER", "idRef": "1", "level": "0"})   # python-hwpx 는 id 를 맨 뒤에 쓴다
            doc.add_paragraph("가나", para_pr_id_ref=pid); doc.save_to_path(str(out)); doc.close()
            with zipfile.ZipFile(out) as z:
                head = z.read("Contents/header.xml").decode("utf-8")
            self.assertNotRegex(head, rf'<hh:paraPr id="{pid}"'); self.assertRegex(head, rf'<hh:paraPr [^>]*id="{pid}"')
            self.assertEqual([i["rule"] for i in check_hwpx_file(str(out))], ["개요 문단"])

    def test_two_section_template_with_image_keeps_added_pictures_in_manifest(self):
        from hwpx import HwpxDocument
        from yeone_ocr.hwpx_template import build_hwpx_template
        from yeone_ocr.verify import check_hwpx_file
        with tempfile.TemporaryDirectory() as td:
            tpl = Path(td) / "양식2.hwpx"; make_template2(tpl)
            img = Path(td) / "그림.png"
            d = pymupdf.open(); pg = d.new_page(width=40, height=40); pg.draw_rect(pymupdf.Rect(5, 5, 35, 35), fill=(0, 0, 0))
            img.write_bytes(pg.get_pixmap().tobytes("png")); d.close()
            blocks = [Block(type="heading", text="菜　下", level=2, role="section"),
                      Block(type="image", path=str(img), kind="그림", frac_w=0.5, ratio=1.0),
                      Block(type="para", text="味辛", role="body")]
            out = Path(td) / "out.hwpx"
            st = build_hwpx_template(blocks, str(out), str(tpl))
            HwpxDocument.open(str(out)).close()                                            # 열린다
            self.assertEqual(check_hwpx_file(str(out)), [])
            self.assertEqual((st["validate_issues"], st["validate_messages"], st["images"], st["images_in_file"]), (0, [], 1, 1))
            self.assertEqual({k: st["template_dropped"][k] for k in ("sections", "headers", "footers")}, {"sections": 1, "headers": 1, "footers": 1})
            self.assertFalse(st["page_number_from_template"])
            with zipfile.ZipFile(out) as z:
                names = set(z.namelist()); sec = ET.fromstring(z.read("Contents/section0.xml")); head = ET.fromstring(z.read("Contents/header.xml"))
                listed = {it.get("id"): it.get("href") for it in ET.fromstring(z.read("Contents/content.hpf")).iter("{http://www.idpf.org/2007/opf/}item")}
        refs = [im.get("binaryItemIDRef") for im in sec.iter("{http://www.hancom.co.kr/hwpml/2011/core}img")]
        self.assertEqual(len(refs), 1)
        for r in refs:                                                                      # 본문이 가리키는 그림마다 꾸러미 목록(content.hpf)에 있고 파일도 있다
            self.assertIn(r, listed); self.assertIn(listed[r], names)
        xml = ET.tostring(sec, encoding="unicode")
        self.assertNotIn("양식 본문", xml); self.assertNotIn("둘째 구역", xml); self.assertNotIn("양식 꼬리말", xml); self.assertNotIn("본초학 양식", xml)
        paras = sec.findall(f"{HP}p")                                                       # 그림 문단의 문단 모양: 제목(제목-절)의 것이 아니라 바탕글을 가운데 정렬한 것
        pic_p = next(p for p in paras if p.find(f".//{HP}pic") is not None)
        styles = {s.get("name"): s for s in head.iter(f"{HW}style")}
        self.assertNotEqual(pic_p.get("paraPrIDRef"), styles["제목-절"].get("paraPrIDRef"))
        self.assertEqual(pic_p.get("styleIDRef"), styles["바탕글"].get("id"))
        pic_ppr = next(p for p in head.iter(f"{HW}paraPr") if p.get("id") == pic_p.get("paraPrIDRef"))
        self.assertEqual(pic_ppr.find(f"{HW}align").get("horizontal"), "CENTER")

    def test_empty_heading_is_dropped_and_empty_para_keeps_a_run(self):
        from yeone_ocr.hwpx_template import build_hwpx_template
        with tempfile.TemporaryDirectory() as td:
            tpl = Path(td) / "양식.hwpx"; make_template(tpl)
            blocks = [Block(type="heading", text="", level=2, role="section"), Block(type="para", text="", role="body"), Block(type="para", text="味辛", role="body")]
            out = Path(td) / "out.hwpx"
            st = build_hwpx_template(blocks, str(out), str(tpl))
            with zipfile.ZipFile(out) as z:
                sec = ET.fromstring(z.read("Contents/section0.xml"))
        paras = sec.findall(f"{HP}p")
        self.assertEqual((st["headings"], st["paragraphs"], len(paras)), (0, 2, 2))          # 빈 제목은 문단이 되지 않는다
        self.assertTrue(all(p.findall(f"{HP}run") for p in paras))                           # 런 없는 <hp:p/> 는 없다
        self.assertEqual([self.body_text(p) for p in paras], ["", "味辛"])
        self.assertEqual(len(self.body_runs(paras[0])), 1)                                   # 빈 문단에도 런 하나

    def test_template_page_number_is_kept_and_not_doubled(self):
        from yeone_ocr.hwpx_template import build_hwpx_template
        with tempfile.TemporaryDirectory() as td:
            tpl = Path(td) / "양식2.hwpx"; make_template2(tpl, page_number=True)
            out = Path(td) / "out.hwpx"
            st = build_hwpx_template([Block(type="heading", text="菜　下", level=2, role="section")], str(out), str(tpl))
            with zipfile.ZipFile(tpl) as z:
                tsec = ET.fromstring(z.read("Contents/section0.xml"))
            with zipfile.ZipFile(out) as z:
                sec = ET.fromstring(z.read("Contents/section0.xml"))
        self.assertTrue(st["page_number_from_template"]); self.assertEqual(st["template_dropped"]["footers"], 0)
        count = lambda s: (len(s.findall(f".//{HP}ctrl/{HP}footer")), len(s.findall(f".//{HP}ctrl/{HP}footer//{HP}autoNum")) + len(s.findall(f".//{HP}ctrl/{HP}footer//{HP}pageNum")))
        self.assertEqual(count(sec), count(tsec))                                             # 양식의 쪽 번호 꼬리말 그대로, 하나만
        self.assertEqual(count(sec)[0], 1); self.assertGreater(count(sec)[1], 0)

    def test_green_and_blue_spans_keep_template_bold(self):
        from yeone_ocr.hwpx_template import build_hwpx_template
        with tempfile.TemporaryDirectory() as td:
            tpl = Path(td) / "양식2.hwpx"; make_template2(tpl)
            out = Path(td) / "out.hwpx"
            st = build_hwpx_template([Block(type="para", text="味苦寒有毒〔主/生〕大水", role="bonjing", spans=[(0, 2, "green"), (2, 4, "blue")])], str(out), str(tpl))
            with zipfile.ZipFile(out) as z:
                sec = ET.fromstring(z.read("Contents/section0.xml")); head = ET.fromstring(z.read("Contents/header.xml"))
        self.assertEqual((st["styles"]["bonjing"], st["green"], st["blue"], st["marks"]), ("본경 원문", 1, 1, 1))
        chars = {c.get("id"): c for c in head.iter(f"{HW}charPr")}
        base_id = next(s for s in head.iter(f"{HW}style") if s.get("name") == "본경 원문").get("charPrIDRef")
        self.assertIsNotNone(chars[base_id].find(f"{HW}bold"))
        runs = {(r.findtext(f"{HP}t") or ""): r.get("charPrIDRef") for p in sec.findall(f"{HP}p") for r in self.body_runs(p)}
        for piece in ("味苦", "寒有", "毒", "〔主/生〕", "大水"):
            self.assertIsNotNone(chars[runs[piece]].find(f"{HW}bold"), piece)               # 초록·파랑 등급도 양식 글자 모양의 굵기를 잃지 않는다
        self.assertNotEqual(runs["味苦"], base_id); self.assertEqual(runs["毒"], base_id)      # 색만 덧입힌 사본 / 본문은 양식 그대로


class Command(unittest.TestCase):
    def test_build_flow_with_template_from_mock_reading(self):
        from yeone_ocr import cli
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td); tpl = tmp / "양식.hwpx"; make_template(tpl)
            pdf, pr = bold_scan(tmp)
            doc = pymupdf.open(pdf)                                                  # mock 판독기는 세로 열 글자층을 옮긴다
            doc[0].insert_text((380, 40), "香薷　味辛微温", fontname="china-s", fontsize=18, rotate=270, render_mode=3)
            src = tmp / "책.pdf"; doc.save(src); doc.close()
            w = str(tmp / "w"); log = io.StringIO()
            with contextlib.redirect_stdout(log):
                cli.main(["run", str(src), "--work", w, "--reader", "mock", "--checkpoint", "0", "--mode", "vertical", "--no-normalize"])
                rc = cli.main(["build", str(src), "--work", w, "--layout", "flow", "--template", str(tpl), "--no-normalize", "--out", "내용별"])
            self.assertEqual(rc, 0, log.getvalue()[-800:])
            self.assertIn("원본 쪽 책갈피 1개", log.getvalue()); self.assertIn("내용별 짜임", log.getvalue())
            self.assertTrue((tmp / "w" / "out" / "내용별.hwpx").exists())
            self.assertIn("짜임 규칙: 기본 규칙 (basic)", log.getvalue())

    def test_bold_is_measured_only_when_the_preset_interprets_it(self):
        """획 굵기(쪽마다 그려 재는 몇 분)는 내용별 + 양식 + 짜임 규칙에 약재명 떼기(entry — 굵기 해석이 없어도 빠뜨린 빈칸을 잼)가 있을 때만 잰다 — 아니면 까닭을 로그에."""
        from unittest import mock
        from yeone_ocr import cli, structure
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td); tpl = tmp / "양식.hwpx"; make_template(tpl)
            pdf, _ = bold_scan(tmp); w = str(tmp / "w")
            import json
            from yeone_ocr.presets import DATA
            only = json.loads((DATA / "preset_kaibao.json").read_text(encoding="utf-8"))   # 굵기 해석 없이 약재명 떼기만 — 빠뜨린 빈칸(gap)을 재려고 잰다
            only.update(emphasis=None, label="약재명만"); only["styles"]["character"].pop("bonjing")
            (tmp / "약재명만.json").write_text(json.dumps(only, ensure_ascii=False), encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()):
                cli.main(["run", str(pdf), "--work", w, "--reader", "mock", "--checkpoint", "0", "--mode", "horizontal_zh", "--no-normalize"])
            for flags, called, why in ((["--preset", "regions"], False, "짜임 규칙 '영역 종류만 (제목·문단·소주)' 에는 굵은 글씨 해석이 없어 재지 않습니다"),
                                       (["--preset", str(tmp / "약재명만.json")], True, "판독이 빠뜨린 약재명 뒤 빈칸을 쪽 이미지에서 재려고 잽니다"),
                                       (["--preset", str(tmp / "약재명만.json"), "--no-bold"], False, ""),
                                       ([], False, "굵은 글씨 해석이 없어"),                                                       # 기본 basic
                                       (["--preset", "kaibao", "--no-bold"], False, ""),
                                       (["--structure", "none"], False, "굵은 글씨 해석이 없어"),
                                       (["--preset", "kaibao"], True, "짜임 규칙: 本草 조문 (開寶本草·證類 계열) (kaibao)"),
                                       (["--structure", "auto"], True, "(auto)")):
                log = io.StringIO()
                with self.subTest(flags=flags), mock.patch.object(structure, "measure_bold", side_effect=RuntimeError("재면 안 됨") if not called else
                                                                  (lambda pages, log=None: ({}, {"pages": 0, "chars": 0, "median": 0.0, "bold_paragraphs": 0}))) as mb, \
                        contextlib.redirect_stdout(log):
                    rc = cli.main(["build", str(pdf), "--work", w, "--layout", "flow", "--template", str(tpl), "--no-normalize", "--out", "x"] + flags)
                    self.assertEqual((rc, mb.called), (0, called), log.getvalue()[-600:]); self.assertIn(why, log.getvalue())
            log = io.StringIO()
            with mock.patch.object(structure, "measure_bold", side_effect=RuntimeError("재면 안 됨")), contextlib.redirect_stdout(log):
                self.assertEqual(cli.main(["build", str(pdf), "--work", w, "--layout", "flow", "--preset", "kaibao", "--no-normalize", "--out", "y"]), 0)
            self.assertIn("양식 없이 만드는 문서에는 굵은 글씨 구분이 보이지 않아", log.getvalue())

    def test_cli_options_and_window_command(self):
        from yeone_ocr import cli
        from yeone_ocr.cli import build_parser
        a = build_parser().parse_args(["build", "x.pdf", "--layout", "flow", "--template", "t.hwpx", "--preset", "kaibao", "--no-bold"])
        self.assertEqual((a.layout, a.template, a.preset, a.no_bold, cli.preset_ref(a)), ("flow", "t.hwpx", "kaibao", True, "kaibao"))
        a = build_parser().parse_args(["build", "x.pdf", "--layout", "flow", "--structure", "bencao"])        # 예전 이름도 받는다(도움말에는 숨김)
        self.assertEqual((a.preset, a.structure, cli.preset_ref(a)), (None, "bencao", "bencao"))
        a = build_parser().parse_args(["run", "x.pdf"])
        self.assertEqual((a.layout, a.template, a.preset, a.structure, a.no_bold, cli.preset_ref(a)), ("page", None, None, None, False, "basic"))
        self.assertNotIn("--structure", build_parser()._subparsers._group_actions[0].choices["build"].format_help())
        try:
            from yeone_ocr import gui
        except ImportError:
            self.skipTest("tkinter 없음")
        V = lambda v: SimpleNamespace(get=lambda: v)
        app = SimpleNamespace(v_docx=V(False), v_join=V(True), v_hl=V(True), v_ctx=V(True), v_pb=V(True), v_tlpdf=V(False),
                              v_layout=V(gui.LAYOUT_LABELS["flow"]), v_template=V(" C:/양식.hwpx "), v_bold=V(False), v_ctxmodel=V(False),
                              check_list_mode=lambda: "none", memo_mode=lambda: "notice", _preset_ref="kaibao")
        app.layout_key = lambda: gui.App.layout_key(app)
        o = gui.App._build_opts(app)
        a = build_parser().parse_args(["build", "x.pdf"] + o)
        self.assertEqual((a.layout, a.template, a.no_bold, a.no_context_model, a.preset, a.structure), ("flow", "C:/양식.hwpx", True, True, "kaibao", None))
        app._preset_ref = "D:/책/규칙.json"
        self.assertEqual(build_parser().parse_args(["build", "x.pdf"] + gui.App._build_opts(app)).preset, "D:/책/규칙.json")   # 창의 짜임 규칙 선택(늘 넘긴다)
        app.v_layout = V(gui.LAYOUT_LABELS["page"])
        o = gui.App._build_opts(app)
        self.assertEqual(o[:2], ["--layout", "page"]); self.assertNotIn("--preset", o); self.assertNotIn("--template", o)
        self.assertEqual((gui.DEFAULTS["layout"], gui.DEFAULTS["template"], gui.DEFAULTS["bold_split"], gui.DEFAULTS["preset"]), ("page", "", True, "basic"))
        self.assertNotIn("structure", gui.DEFAULTS)

    def test_window_greys_out_options_the_layout_does_not_use(self):
        try:
            from yeone_ocr import gui
        except ImportError:
            self.skipTest("tkinter 없음")
        from unittest import mock
        V = lambda v: SimpleNamespace(get=lambda: v)
        app = SimpleNamespace(v_layout=V(gui.LAYOUT_LABELS["flow"]), cb_pb=mock.Mock(), te_template=mock.Mock(), cb_bold=mock.Mock(), cb_preset=mock.Mock(),
                              _preset_ref="kaibao", v_template=V("C:/양식.hwpx"))
        app.layout_key = lambda: gui.App.layout_key(app)
        ws = (app.cb_pb, app.te_template, app.cb_bold, app.cb_preset)
        gui.App._layout_changed(app)                                                # 내용별: 쪽 나누기만 흐리게
        self.assertEqual([w.state.call_args.args[0] for w in ws], [["disabled"], ["!disabled"], ["!disabled"], ["!disabled"]])
        for ref in ("regions", "없는/규칙.json"):                                     # 약재명 떼기·굵은 글씨 해석이 없는(·쓸 수 없는) 규칙이면 획 굵기도 흐리게
            app._preset_ref = ref
            gui.App._layout_changed(app)
            self.assertEqual([w.state.call_args.args[0] for w in ws], [["disabled"], ["!disabled"], ["disabled"], ["!disabled"]], ref)
        app._preset_ref = "kaibao"; app.v_template = V("  ")                       # 양식이 없으면(기본 모양) 재지 않으므로 흐리게(README 대로)
        gui.App._layout_changed(app)
        self.assertEqual(app.cb_bold.state.call_args.args[0], ["disabled"])
        import json, tempfile
        from yeone_ocr.presets import DATA
        with tempfile.TemporaryDirectory() as td:                                   # 굵기 해석 없이 약재명 떼기만 있는 규칙: 빠뜨린 빈칸을 재므로 켠다
            only = json.loads((DATA / "preset_kaibao.json").read_text(encoding="utf-8")); only["emphasis"] = None; only["styles"]["character"].pop("bonjing")
            path = Path(td) / "약재명만.json"; path.write_text(json.dumps(only, ensure_ascii=False), encoding="utf-8")
            app._preset_ref = str(path); app.v_template = V("C:/양식.hwpx")
            gui.App._layout_changed(app)
        self.assertEqual(app.cb_bold.state.call_args.args[0], ["!disabled"])
        app.v_layout = V(gui.LAYOUT_LABELS["page"]); app._preset_ref = "kaibao"
        gui.App._layout_changed(app)                                                # 원본 쪽대로: 양식·획 굵기·짜임 규칙을 흐리게
        self.assertEqual([w.state.call_args.args[0] for w in ws], [["!disabled"], ["disabled"], ["disabled"], ["disabled"]])


if __name__ == "__main__":
    unittest.main()
