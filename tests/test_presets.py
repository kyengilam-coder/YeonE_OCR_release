"""짜임 규칙(preset): 내장 regions·kaibao 와 예전 --structure 이름, kaibao 가 짜는 정규식 = 0.7.x structure.py 상수(말뭉치 비교),
apply_structure 가 규칙 객체·이름으로 같은 답, 규칙 파일 검사 오류 글, 굵기 해석 없는 규칙·늘 떼기 규칙·스타일 없는 규칙, 고르기 목록. 모델 호출 없음."""
import json
import random
import re
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from yeone_ocr.blocks import Block
from yeone_ocr.presets import (ALIASES, BUILTIN, DEFAULT_PRESET, ROLES, Preset, PresetError, load_preset, preset_choices, preset_label,
                               resolve_preset)
from yeone_ocr.structure import apply_structure

sys.path.insert(0, str(Path(__file__).resolve().parent))

# ---- 0.7.x structure.py 의 상수 그대로(옮기기 전). 한자 범위는 이스케이프로 적는다(호환 한자 U+F900 이 NFC 로 바뀌지 않게) ----
OLD_HAN = "\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\U00020000-\U0003134f"
OLD_OPENERS = "味|一名|主|性|氣|气"
OLD_ENTRY_RE = re.compile(rf"^([{OLD_HAN}]{{1,6}})[ \t\n\u00a0\u2002\u2003\u3000]+(?=(?:{OLD_OPENERS})[{OLD_HAN}])")
OLD_ENTRY_START = re.compile(rf"(?:{OLD_OPENERS})[{OLD_HAN}]")
OLD_COMMENT_RE = re.compile(r"^[［\[〔【（(]\s*[^］\]〕】）)\s/:：]{0,16}?(?:云|曰|按|注|謹按|谨按|又按|今注|今按|今詳|今详|今據|今据|說|说)\s*[］\]〕】）)]")
OLD_SOURCE_RE = re.compile(r"(?:(?<=[。，、；：\s　])|^)((?:唐|今|宋|新|唐本)(?:附|增|退|移)|(?:陳藏器|陈藏器|海藥|海药|唐本)(?:餘|余)|新補|新补|新定)[ 　]*$")
OLD_HEADING_NEXT = re.compile(rf"([{OLD_HAN}]{{0,6}})[ 　,，、]*(?:味|一名)[{OLD_HAN}]")   # _heading_entry 안의 정규식
OLD_FUNC_END = "云曰按注經錄等謹所"
OLD_UNIT_END = "兩两錢钱分枚升斤個箇合字銖铢丸片把束尺寸撮粒顆颗"
OLD_FUNC_START = "凡又其此是以故若則则乃每諸诸"


def old_is_name(name: str) -> bool:
    return bool(re.fullmatch(rf"[{OLD_HAN}]{{1,6}}", name)) and name[-1] not in OLD_FUNC_END and name[-1] not in OLD_UNIT_END and name[0] not in OLD_FUNC_START


def new_is_name(name: str, p: Preset) -> bool:
    from yeone_ocr.structure import _is_name
    return _is_name(name, p)


TAGS = [a + b for a in ("唐", "今", "宋", "新", "唐本") for b in ("附", "增", "退", "移")] + \
       [a + b for a in ("陳藏器", "陈藏器", "海藥", "海药", "唐本") for b in ("餘", "余")] + ["新補", "新补", "新定"]
MARKERS = ["云", "曰", "按", "注", "謹按", "谨按", "又按", "今注", "今按", "今詳", "今详", "今據", "今据", "說", "说"]
OPENS, CLOSES = "［[〔【（(", "］]〕】）)"
HAN = "香薷味辛主性氣气一名陶隱居唐本今附新補陳藏器餘余兩錢凡又其云按經所臣禹錫等謹草處有物生地黃\uf900\U00020000"   # 호환·확장 B 한자도
OTHER = "가a1 　。，、；：/:：·\t\u00a0\u2002\u2003\n"


def corpus(seed: int = 20261015) -> list[str]:
    rnd = random.Random(seed)
    out = ["今余", "其事，今余", "唐本", "陳藏器", "唐余", "本附", "x今附", "所在有之。　唐附", "录在菜部。陈藏器余", "主治。　新補", ""]
    for t in TAGS + ["今余", "唐余", "本附", "新"]:                                      # 출처: 앞 글 × 뒤 빈칸
        for pre in ("", "。", "，", "主治。　", "其事，", "其事", "x "):
            for suf in ("", " ", "　", "　 ", "x"):
                out.append(pre + t + suf)
    word = lambda n, pool=HAN + OTHER: "".join(rnd.choice(pool) for _ in range(n))
    for _ in range(4000):                                                             # 주석: 괄호 + 표지(0~18자, / : ： 빈칸 섞임) + 표지어 + 닫는 괄호
        label = word(rnd.randrange(0, 19), HAN + "/:： 　" + "".join(MARKERS))
        out.append(rnd.choice(OPENS + "〈") + rnd.choice(["", " ", "　"]) + label + rnd.choice(MARKERS + ["", "曰", "也"])
                   + rnd.choice(["", " ", "\t"]) + rnd.choice(CLOSES + "〉") + word(rnd.randrange(0, 5)))
    seps = [" ", "　", "\t", "\u00a0", "", "\u2002", "\u2003", "\n", "  ", "，"]
    openers = ["味", "一名", "主", "性", "氣", "气", "生", "名", "一", "云"]
    for _ in range(4000):                                                             # 약재명: 한자 1~8 + 빈칸 + 조문 첫 낱말
        out.append(word(rnd.randrange(1, 9), HAN) + rnd.choice(seps) + rnd.choice(openers) + word(rnd.randrange(0, 4)))
        out.append(word(rnd.randrange(0, 8), HAN + "，、 　,") + rnd.choice(["味", "一名", "主"]) + word(rnd.randrange(0, 3)))
    for _ in range(3000):
        out.append(word(rnd.randrange(0, 12)))
    return out


def roles(blocks):
    return [(b.type, b.role, b.level, b.text, sorted(b.cstyles), b.spans, b.anchors, b.memos) for b in blocks]


def fixtures() -> list[list[Block]]:
    """tests/test_flow 의 짜임 시험 블록(약재명 제목·주석·출처·굵기·틈·쪽 첫머리 가르기·다른 책)."""
    P = lambda t, **kw: Block(type="para", text=t, kind=kw.pop("kind", "본문"), **kw)
    H = lambda t, lv=1: Block(type="heading", text=t, level=lv, kind="제목")
    return [
        [H("開寶本草玉石等部中品 卷第四"), H("雄黃"), P("〔疑: 雄黄〕味苦，甘，平。主寒熱。"), H("孔公孽　味辛，溫，無毒。"),
         P("主傷食不化。", spans=[(1, 2, "green")], anchors=[(0, "PDF 9쪽")]), H("附子\u2003味辛·甘，溫。", 2), P("〔陶隱居云〕此物。"),
         H("吐唾血", 2), P("生地黃『本經』寒。"), H("菜　下", 2), P("香薷　味辛，微溫。")],
        [H("菜　下", 2), P("香薷味辛，微溫。"), H("雄黃"), P("〔疑: 雄黄〕味苦。"), H("點青"), P("〔疑: 扁青〕味甘。"), H("黃連"), H("黃芩"),
         P("味苦，平。"), P("〔日/曰〕本經曰味甘。"), P("〔疑: 注〕云味甘。"), P("〔陶隱居云〕此物。")],
        [P("〔黄/黃〕連 味苦，寒。", spans=[(7, 8, "green")]), P("陶隱居云 味苦。"), P("本經 味甘。"),
         P("主心痛。久服輕身。苦瓠 味苦，寒。主大水。〔唐本注云〕今處處有。", anchors=[(9, "PDF 2쪽"), (21, "PDF 3쪽")])],
        [P("香薷　味辛，微温。主霍乱腹痛吐下，散水肿。", anchors=[(0, "PDF 1쪽")], memos=[(0, "고지")], spans=[(5, 6, "green")]),
         P("［陶隐居云］　处处有此，惟供生食。"), P("秦荻梨　味辛，温，无毒。所在有之。　唐附"), P("中，今人食之，录在菜部也。"), H("菜　下", 2)],
        [P("桂枝三兩 生薑三兩 大棗十二枚"), P("桂枝三兩 味辛"), P("麻黃三錢 性溫"), P("凡藥 性有宜丸者"), P("吐唾血　生地黃　大黃")],
        [P("苦瓠　味苦寒有毒", cstyles=[(3, 8, "bold")]), H("水斳"), P("味甘，平。"), P("落葵　味酸，寒。")],
        [P("苦瓠味苦，寒，有毒。", cstyles=[(2, 2, "gap")])],
        [P(f"{n}　味甘，平。") for n in ("水斳", "落葵", "繁蔞")],
        [P("苦瓠　味苦寒有毒主大水面目四肢浮腫下水", cstyles=[(3, 21, "bold")]), P("苦瓠　味苦寒有毒主大水面目四肢浮腫下水", cstyles=[(3, 9, "bold")]),
         P("小注", kind="소주", cstyles=[(0, 2, "bold")]), Block(type="note", text="〔작업 메모〕"), H("圖", 3)],
    ]


def write(td: str, data, name: str = "내책.json") -> Path:
    p = Path(td) / name
    p.write_text(data if isinstance(data, str) else json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    return p


class Builtins(unittest.TestCase):
    def test_builtins_aliases_and_default(self):
        self.assertEqual((DEFAULT_PRESET, BUILTIN), ("basic", ("basic", "regions", "kaibao")))
        for ref in (None, "", "  ", "basic"):
            self.assertEqual(resolve_preset(ref).id, "basic", ref)
        self.assertEqual(resolve_preset("regions").id, "regions")
        r, k = resolve_preset("regions"), resolve_preset("kaibao")
        self.assertIs(resolve_preset("kaibao"), k)                                    # 내장은 한 번만 읽는다
        self.assertEqual((r.entry, r.comment_re, r.source_re, r.emphasis), (None, None, None, None))
        self.assertEqual((k.entry.when, k.entry.min_hits, k.entry.min_ratio, k.entry.max_name, k.emphasis), ("auto", 3, 0.01, 6, ("bonjing", "bielu")))
        self.assertEqual(set(ALIASES), {"none", "auto", "bencao"})
        a, b, n = resolve_preset("auto"), resolve_preset("bencao"), resolve_preset("none")
        self.assertEqual((a.id, a.ref, a.entry.when), ("kaibao", "auto", "auto"))
        self.assertEqual((b.id, b.ref, b.entry.when), ("kaibao", "bencao", "always"))  # 예전 bencao = 약재명 늘 떼기(kaibao 로 바꾸지 않는다)
        self.assertEqual((n.id, n.ref, n.entry), ("regions", "none", None))
        self.assertEqual(k.entry.when, "auto")                                       # 별칭이 내장 객체를 바꾸지 않는다
        self.assertEqual(set(k.para_styles), set(ROLES))
        self.assertEqual(k.char_styles, {"small": ("소주",), "source": ("출처",), "bonjing": ("본경 조문",)})
        self.assertEqual({r_: k.para_styles[r_] for r_ in r.para_styles}, r.para_styles)   # regions 의 이름은 kaibao 와 같다

    def test_kaibao_regexes_equal_old_literals_on_corpus(self):
        k = resolve_preset("kaibao")
        pairs = [("entry_re", OLD_ENTRY_RE, k.entry_re, "match"), ("entry_start_re", OLD_ENTRY_START, k.entry_start_re, "match"),
                 ("comment_re", OLD_COMMENT_RE, k.comment_re, "match"), ("source_re", OLD_SOURCE_RE, k.source_re, "search"),
                 ("heading_next_re", OLD_HEADING_NEXT, k.heading_next_re, "match")]
        texts = corpus()
        hits = {name: 0 for name, *_ in pairs}
        for s in texts:
            for name, old, new, how in pairs:
                mo, mn = getattr(old, how)(s), getattr(new, how)(s)
                self.assertEqual(bool(mo), bool(mn), (name, s))
                if mo:
                    hits[name] += 1
                    self.assertEqual(mo.end(), mn.end(), (name, s))
                    if old.groups:
                        self.assertEqual((mo.group(1), mo.start(1)), (mn.group(1), mn.start(1)), (name, s))
            name = re.sub(r"[\s　·・.,，。]", "", s)[:8]
            for cand in {s[:8], name, s.strip()[:3], s[:1]}:
                self.assertEqual(old_is_name(cand), new_is_name(cand, k), cand)
        self.assertTrue(all(v > 100 for v in hits.values()), hits)                   # 말뭉치가 규칙마다 맞는 글을 넉넉히 담는다
        self.assertEqual(k.source_re.search("所在有之。　唐附").group(1), "唐附"); self.assertIsNone(k.source_re.search("其事，今余"))
        self.assertIsNone(k.entry_re.match("가나　味甘"))                              # 한글은 한자가 아니다(호환 한자 범위가 NFC 로 바뀌면 여기서 걸린다)

    def test_apply_structure_same_with_preset_object_name_and_old_names(self):
        k, r = resolve_preset("kaibao"), resolve_preset("regions")
        always = replace(k, entry=replace(k.entry, when="always"))
        for i, blocks in enumerate(fixtures()):
            for bm in (True, False):
                with self.subTest(fixture=i, bold_measured=bm):
                    self.assertEqual(roles(apply_structure(blocks, k, bm)[0]), roles(apply_structure(blocks, "auto", bm)[0]))
                    self.assertEqual(roles(apply_structure(blocks, k, bm)[0]), roles(apply_structure(blocks, "kaibao", bm)[0]))
                    self.assertEqual(roles(apply_structure(blocks, always, bm)[0]), roles(apply_structure(blocks, "bencao", bm)[0]))
                    self.assertEqual(roles(apply_structure(blocks, r, bm)[0]), roles(apply_structure(blocks, "none", bm)[0]))
                    self.assertEqual(roles(apply_structure(blocks, bold_measured=bm)[0]), roles(apply_structure(blocks, "none", bm)[0]))   # 기본 = regions
        out, st = apply_structure(fixtures()[3], "regions")
        self.assertEqual([b.role for b in out], ["body", "body", "body", "body", "section"])   # 영역 종류만: 약재명·주석·출처 없음
        self.assertTrue(all(not b.cstyles for b in out)); self.assertNotIn("entry", st)


class PresetFiles(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.mkdtemp(); self.addCleanup(__import__("shutil").rmtree, self.td, True)

    def bad(self, data, *want):
        path = write(self.td, data)
        with self.assertRaises(PresetError) as cm:
            resolve_preset(str(path))
        msg = str(cm.exception)
        self.assertTrue(msg.startswith(f"짜임 규칙 파일 {path}: "), msg)
        for w in want:
            self.assertIn(w, msg)
        return msg

    def test_errors_name_the_file_and_the_field(self):
        self.bad('{"schema": 1,\n  "entry": {"openers": ["味"],}\n}', "JSON 꼴이 틀렸습니다 — 2째 줄", "째 글자")
        self.bad('{"schema": 1\n "entry": null}', "2째 줄", "쉼표(,)가 빠졌습니다")                   # json 의 영어 오류는 고칠 길을 말하는 글로
        self.bad('{"schema": 1, entry: null}', '칸 이름은 큰따옴표 "…" 로 감싸야 합니다')
        self.bad('{"schema": 1, "entry": [1,]}', "값이 없거나 꼴이 틀렸습니다")
        self.bad([1, 2], "맨 위가 {…} 가 아닙니다")
        self.bad({"schema": 2}, "schema 는 1 이어야 합니다(지금 2)")
        self.bad({"label": "x"}, "schema 는 1 이어야 합니다(지금 None)")
        self.bad({"schema": True}, "schema 는 1 이어야 합니다")
        self.bad({"schema": 1, "entry": {"opener": ["味"]}}, "모르는 칸 'entry.opener' — 쓸 수 있는 칸: when, min_hits, min_ratio, max_name, openers")
        self.bad({"schema": 1, "entri": None}, "모르는 칸 'entri'")
        self.bad({"schema": 1, "entry": {"when": "sometimes", "openers": ["味"]}}, 'entry.when 은 "auto" 또는 "always" 입니다')
        self.bad({"schema": 1, "entry": {"openers": []}}, "entry.openers 는 비지 않은 글자열 목록")
        self.bad({"schema": 1, "entry": {"openers": ["味"], "max_name": 13}}, "entry.max_name 은 1~12 정수")
        self.bad({"schema": 1, "entry": {"openers": ["味"], "min_ratio": 2}}, "entry.min_ratio 는 0~1 사이 수")
        self.bad({"schema": 1, "entry": {"openers": ["味"], "min_hits": True}}, "entry.min_hits 는 1 이상 정수")
        self.bad({"schema": 1, "commentary": {"open": "［", "markers": ["云"]}}, "commentary.open·close 는 글자열")
        self.bad({"schema": 1, "source": {"tags": []}}, "source.tags 는 비지 않은 글자열 목록")
        self.bad({"schema": 1, "entry": {"openers": ["味"]}, "emphasis": {"bold": "chapter", "plain": "bielu"}},
                 "emphasis.bold 'chapter' 는 쓸 수 없는 쓰임입니다 — 가능: body, bonjing, bielu, commentary")
        self.bad({"schema": 1, "entry": {"openers": ["味"]}, "emphasis": {"bold": "bielu", "plain": "bielu"}}, "emphasis 의 bold 와 plain 이 같습니다")
        self.bad({"schema": 1, "emphasis": {"bold": "bonjing", "plain": "bielu"}}, "emphasis 는 entry(약재명 떼기)가 있을 때만")
        self.bad({"schema": 1, "styles": {"paragraph": {"xx": ["본문"]}}}, "styles.paragraph 의 'xx' 는 쓰임이 아닙니다 — 가능: chapter, section,")
        self.bad({"schema": 1, "styles": {"paragraph": {"body": "본문"}}}, "styles.paragraph.body 는 스타일 이름 목록(비지 않은 글자열)")
        self.bad({"schema": 1, "styles": {"character": {"bonjing": ["본경 조문"]}}}, "styles.character 의 'bonjing' 는 글자 범위 종류가 아닙니다 — 가능: small, source")
        with self.assertRaises(PresetError) as cm:
            resolve_preset(str(Path(self.td) / "없는.json"))
        self.assertEqual(str(cm.exception), f"짜임 규칙 파일이 없습니다: {Path(self.td) / '없는.json'}")
        with self.assertRaises(PresetError) as cm:
            resolve_preset("honzo")
        self.assertIn("짜임 규칙 'honzo' 를 모릅니다 — 내장: basic(기본 규칙)·regions(영역 종류만)·kaibao(本草 조문)", str(cm.exception))
        (Path(self.td) / "글자.json").write_bytes("{}".encode("utf-16"))
        with self.assertRaises(PresetError) as cm:
            load_preset(Path(self.td) / "글자.json")
        self.assertIn("UTF-8 로 저장된 파일이 아닙니다", str(cm.exception)); self.assertNotIn("codec", str(cm.exception))
        (Path(self.td) / "ansi.json").write_bytes('{"schema": 1, "label": "本草 규칙"}'.encode("cp949"))   # 한국어 Windows 편집기의 ANSI 저장
        with self.assertRaises(PresetError) as cm:
            load_preset(Path(self.td) / "ansi.json")
        self.assertIn("'다른 이름으로 저장 → 인코딩 UTF-8' 로 다시 저장하십시오", str(cm.exception))
        self.assertTrue(issubclass(PresetError, ValueError))

    def test_copy_of_kaibao_with_bom_loads_as_file_preset(self):
        src = Path(resolve_preset.__globals__["DATA"]) / "preset_kaibao.json"
        path = Path(self.td) / "내 本草.json"; path.write_bytes(b"\xef\xbb\xbf" + src.read_bytes())   # 메모장의 BOM
        p = resolve_preset(str(path))
        self.assertEqual((p.ref, p.id, p.emphasis), (str(path), "kaibao", ("bonjing", "bielu")))
        self.assertEqual(p.comment_re.pattern, resolve_preset("kaibao").comment_re.pattern)
        q = load_preset(write(self.td, {"schema": 1}, "빈규칙.json"))
        self.assertEqual((q.id, q.label, q.entry, q.para_styles, q.char_styles), ("빈규칙", "빈규칙", None, {}, {}))   # id·label 은 파일 이름

    def test_no_emphasis_keeps_bold_paragraphs_body_and_always_splits_one_entry(self):
        rule = {"schema": 1, "entry": {"when": "always", "openers": ["味"]}, "commentary": {"open": "［（", "close": "］）", "markers": ["注"]}}
        p = resolve_preset(str(write(self.td, rule)))
        blocks = [Block(type="para", text="苦瓠　味苦寒有毒", kind="본문", cstyles=[(3, 8, "bold")]), Block(type="para", text="（注）此物。", kind="본문"),
                  Block(type="para", text="所在有之。　唐附", kind="본문", cstyles=[(0, 4, "bold")])]
        out, st = apply_structure(blocks, p, bold_measured=True)
        self.assertEqual([(b.type, b.role, b.text) for b in out], [("heading", "entry", "苦瓠"), ("para", "body", "味苦寒有毒"),
                                                                   ("para", "commentary", "（注）此物。"), ("para", "body", "所在有之。　唐附")])
        self.assertTrue(all(not b.cstyles for b in out))                             # 굵기 해석이 없으면 굵은 범위를 남기지 않고, 출처 규칙도 없다
        self.assertEqual(st.get("entry"), 1)                                          # always: 하나뿐이어도 뗀다
        auto = replace(p, entry=replace(p.entry, when="auto"))
        self.assertNotIn("entry", apply_structure(blocks, auto)[1])                   # auto: 셋 미만이면 떼지 않는다
        swap = {"schema": 1, "entry": {"openers": ["味"], "when": "always"}, "emphasis": {"bold": "commentary", "plain": "body"},
                "styles": {"character": {"commentary": ["굵은 주"]}}}
        sw = resolve_preset(str(write(self.td, swap, "b.json")))
        out, _ = apply_structure([Block(type="para", text="甲乙　味苦寒有毒主大水", kind="본문", cstyles=[(3, 6, "bold")]),
                                  Block(type="para", text="本文。又本文", kind="본문", cstyles=[(0, 3, "bold")])], sw)
        self.assertEqual([(b.role, b.cstyles) for b in out[1:]], [("body", [(0, 3, "commentary")]), ("body", [])])   # 굵은 글자 반 미만 → 보통 글씨 쓰임, 굵은 범위는 굵은 글씨 쓰임으로
        out, _ = apply_structure([Block(type="para", text="甲乙　味苦寒有毒主大水", kind="본문", cstyles=[(3, 7, "bold")])], sw)
        self.assertEqual((out[1].role, out[1].cstyles), ("commentary", [(0, 4, "commentary")]))                   # 반 이상 → 굵은 글씨 쓰임

    def test_preset_without_styles_uses_regions_names_then_default(self):
        from _hwpx_fixtures import make_template3
        from yeone_ocr.hwpx_template import build_hwpx_template
        from yeone_ocr.hwpx_writer import TemplateLook
        p = resolve_preset(str(write(self.td, {"schema": 1, "entry": {"openers": ["味"]}})))
        look = TemplateLook("x.hwpx", preset=p)
        self.assertEqual((look.para_styles["section"], look.para_styles.get("entry"), look.char_styles), (("제목-절",), None, {"small": ("소주",)}))
        self.assertEqual(TemplateLook("x.hwpx").para_styles, resolve_preset("kaibao").para_styles)   # 규칙 없이 부르면 예전 이름(kaibao)
        own = resolve_preset(str(write(self.td, {"schema": 1, "styles": {"paragraph": {"section": ["본문"]}}}, "own.json")))
        tpl = Path(self.td) / "양식.hwpx"; make_template3(tpl)
        blocks = [Block(type="heading", text="菜部", level=2, role="section"), Block(type="heading", text="香薷", level=3, role="entry"),
                  Block(type="para", text="味辛。", role="body")]
        st = build_hwpx_template(blocks, str(Path(self.td) / "a.hwpx"), str(tpl), preset=p)
        self.assertEqual((st["styles"]["section"], st["styles"]["entry"]), ("제목-절", "바탕글"))   # 규칙·regions 에 없는 쓰임은 바탕글(로그의 쓰임 → 스타일에 보인다)
        st = build_hwpx_template(blocks, str(Path(self.td) / "b.hwpx"), str(tpl), preset=own)
        self.assertEqual(st["styles"]["section"], "본문")                            # 규칙의 이름이 regions 보다 먼저

    def test_readme_example_preset_loads_and_marks_commentary(self):
        text = (Path(__file__).resolve().parents[1] / "README.md").read_text(encoding="utf-8")
        part = text.split("### 짜임 규칙 파일 만들기 (다른 책)", 1)[1]
        example = re.search(r"```json\n(.*?)```", part, re.S).group(1)
        p = load_preset(write(self.td, example, "醫經.json"))
        self.assertEqual((p.label, p.entry, p.source_re, p.emphasis), ("醫經 注本", None, None, None))
        blocks = [Block(type="heading", text="上古天眞論篇第一", level=1, kind="제목"), Block(type="para", text="昔在黃帝，生而神靈。", kind="본문"),
                  Block(type="para", text="［王冰注］有熊國君少典之子。", kind="본문"), Block(type="para", text="（新校正云）按全元起注本。", kind="본문"),
                  Block(type="para", text="〔疑: 注〕云味甘。", kind="본문")]
        self.assertEqual([b.role for b in apply_structure(blocks, p)[0]], ["chapter", "body", "commentary", "commentary", "body"])
        from yeone_ocr.hwpx_writer import TemplateLook
        look = TemplateLook("x.hwpx", preset=p)
        self.assertEqual((look.para_styles["chapter"], look.para_styles["subsection"], look.char_styles["small"]), (("편명",), ("제목-본초명(소)", "제목-절"), ("협주",)))

    def test_labels_and_choices(self):
        self.assertEqual(preset_choices("kaibao"), [("basic", "기본 규칙"), ("regions", "영역 종류만 (제목·문단·소주)"), ("kaibao", "本草 조문 (開寶本草·證類 계열)")])
        self.assertEqual(preset_choices(None), preset_choices("regions"))
        self.assertEqual(preset_choices("D:/x.json")[3], ("D:/x.json", "파일: x.json (읽지 못함)"))
        self.assertEqual(preset_choices("bencao")[3], ("bencao", "本草 조문 · 약재명 늘 떼기(예전 설정 bencao)"))
        self.assertEqual(len({lab for _, lab in preset_choices("auto")}), 4)                  # 예전 이름도 내장과 다른 이름으로 보인다
        good = write(self.td, {"schema": 1}, "책.json")
        self.assertEqual(preset_label(str(good)), "파일: 책.json")
        self.assertEqual(preset_label("kaibao"), "本草 조문 (開寶本草·證類 계열)"); self.assertEqual(preset_label(""), preset_label("basic"))
        self.assertEqual(preset_label("honzo"), "파일: honzo (읽지 못함)")


if __name__ == "__main__":
    unittest.main()
