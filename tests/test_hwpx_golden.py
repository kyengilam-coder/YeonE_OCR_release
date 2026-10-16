"""HWPX 작성기의 지금 출력을 골든 파일로 고정한다(정규화한 XML 특징 + 통계). 작성기를 고쳐도 같은 블록이면 같은 문서가 나와야 한다.
골든은 tests/golden/<이름>.json. 일부러 출력을 바꿨거나 python-hwpx 를 올렸을 때만 YEONE_WRITE_GOLDEN=1 로 다시 쓰고, 바뀐 곳을 사람이 diff 로 확인한다.
통계는 골든에 있는 열쇠만 견준다(작성기가 새 열쇠를 더해도 된다). 모델 호출·네트워크 없음."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _hwpx_fixtures import hwpx_features, make_template, make_template2, make_template3, rec, text_region, tiny_png   # noqa: E402
from yeone_ocr.blocks import Block, assemble_blocks   # noqa: E402
from yeone_ocr.schema import frac_bbox   # noqa: E402
from yeone_ocr.structure import apply_structure   # noqa: E402
from yeone_ocr.verify import check_hwpx_file   # noqa: E402

GOLDEN = Path(__file__).resolve().parent / "golden"
B3 = "大黃　味苦，寒。日一兩又能〔入/人〕肝經主之"


def records(td: Path) -> tuple[list[dict], dict]:
    """두 쪽 판독 기록과 문맥 추정 기록: 제목·소주·삽화(잘라 낸 그림)·단순표·두 문단 본문(주석·약재 조문, 〔a/b〕·〔疑: 〕·〔판독불명〕)·확인 항목·
    구조 검사 오류, 둘째 쪽은 앞 쪽에서 이어지는 문단과 3차 확정 두 곳(초록·파랑)·열린 문맥 후보(빨강 안 실선)."""
    r1 = rec(1, [{"id": "t1", "k": "제목", "title": "草部上品之上　卷第六", "lv": 1, "bbox": frac_bbox((0.2, 0.03, 0.8, 0.07))},
                 text_region("s1", ["小注〔入/人〕之文"], kind="소주", box=(0.1, 0.1, 0.9, 0.15)),
                 {"id": "f1", "k": "삽화", "bbox": frac_bbox((0.1, 0.2, 0.5, 0.4)), "note": "손글씨 표시 있음"},
                 {"id": "tb", "k": "단순표", "bbox": frac_bbox((0.1, 0.45, 0.9, 0.5)), "rows": [{"cells": ["甲", "乙"]}, {"cells": ["丙"]}]},
                 {"id": "b1", "k": "본문", "bbox": frac_bbox((0.1, 0.55, 0.9, 0.95)), "paras": [
                     {"cols": [{"n": 1, "t": "［陶隐居云］　处处有此。　唐附"}]},
                     {"cols": [{"n": 1, "t": "  香薷　味辛，微温。主霍乱〔入/人〕腹痛，〔疑: 龍〕吐〔판독불명〕"}]}]}], print_page="3")
    r1["merged"]["uncertain"] = [{"region_id": "b1", "col_from_right": 1, "position": "중간", "context": "腹痛，〔疑: 龍〕吐"},
                                 {"region_id": "b1", "col_from_right": 1, "position": "아래", "context": "2차 판독이 이 구간을 읽지 못함"}]
    r1["verify"] = [{"level": "error", "rule": "열 순서", "where": "b1", "msg": "열 번호가 겹침"}]
    r1["crops"] = {"f1": {"path": tiny_png(td / "f1.png"), "frac_w": 0.4, "ratio": 0.75, "touching": False}}
    r2 = rec(2, [text_region("b2", ["下，散水腫。　今附"], cont=True, box=(0.1, 0.1, 0.9, 0.3)),
                 text_region("b3", [B3], box=(0.1, 0.35, 0.9, 0.9))], print_page="4")
    i, j = B3.index("日"), B3.index("兩")
    r2["adjudications"] = [
        {"region_id": "b3", "col_index": 0, "char_index": i, "n_cols": 1, "n_chars": 22, "a": "曰", "b": "日", "pre": B3[i - 2:i], "post": B3[i + 1:i + 3],
         "mark": "〔曰/日〕", "choice": "日", "window": B3[i - 1:i + 2], "evidence": "획"},
        {"region_id": "b3", "col_index": 0, "char_index": j, "n_cols": 1, "n_chars": 22, "a": "雨", "b": "兩", "pre": B3[j - 2:j], "post": B3[j + 1:j + 3],
         "mark": "〔雨/兩〕", "choice": "兩", "window": "", "evidence": ""}]
    k = B3.index("〔")
    ctx = {2: {"sites": [{"region_id": "b3", "para": 0, "start": k, "a": "入", "b": "人", "suggest": "入", "state": "open", "verdict": "suggest",
                          "reason": "이 판독의 다른 대목 入肝"}]}}
    return [r1, r2], ctx


def flow_blocks(td: Path, bold: dict | None = None) -> list[Block]:
    recs, ctx = records(td)
    blocks = assemble_blocks(recs, layout="flow", grades=True, check_list="all", legend=True, join_pages=True, whole_offset=10, context=ctx, bold=bold)
    out, _ = apply_structure(blocks, "bencao")
    return out + [Block(type="report", text="줄1\n줄2")]


def template_blocks(td: Path) -> list[Block]:
    """양식 작성기용 손으로 만든 블록: 쓰임마다 하나씩과 빈 제목·빈 문단·메모·책갈피(가운데·끝)·글자 스타일 범위·확인 목록·작업 기록·그림·표."""
    img = tiny_png(td / "그림.png")
    return [Block(type="heading", text="菜　下", level=2, role="section", anchors=[(0, "PDF 1쪽 · 인쇄 394쪽")]),
            Block(type="heading", text="香薷", level=3, role="entry"),
            Block(type="heading", text="", level=2, role="section"),
            Block(type="para", text="味苦寒有毒〔主/生〕大水", role="bonjing", spans=[(0, 2, "green"), (2, 4, "blue")], cstyles=[(4, 8, "bonjing")]),
            Block(type="para", text="味辛，微温。主霍乱〔腹/腸〕痛吐下，散水肿。", role="bielu", memos=[(3, "고지 하나")], anchors=[(9, "PDF 2쪽")]),
            Block(type="para", text="［唐本注云］　功用相似。　唐附", role="commentary", cstyles=[(13, 15, "source")]),
            Block(type="para", text="小注之文〔疑: 龍〕", kind="소주", role="small", cstyles=[(0, 2, "source")]),
            Block(type="heading", text="그림 1 香薷", kind="그림제목", level=3, role="caption"),
            Block(type="note", text="〔판독 메모〕 손글씨 표시", memos=[(0, "메모 고지")], anchors=[(2, "PDF 2쪽")]),
            Block(type="meta", text="[책.pdf / PDF 2쪽]", memos=[(5, "쪽 고지")]),
            Block(type="para", text="", role="body"),
            Block(type="uncertain", text="[판독 확인 필요]", items=["b1: 腹痛，〔疑: 龍〕吐", "구조 검사: 열 순서 (b1) — 겹침"]),
            Block(type="report", text="줄1\n줄2"),
            Block(type="image", path=img, kind="삽화", frac_w=0.4, ratio=0.75),
            Block(type="table", rows=[["甲", "乙"], ["丙", ""]]),
            Block(type="para", text="散水腫。", role="body", anchors=[(4, "PDF 3쪽")])]


def dump(got: dict) -> str:
    """골든 JSON: 글자 모양·문단 모양·문단 하나가 한 줄(바뀐 곳을 diff 로 보기 쉽게)."""
    f = got["features"]; j = lambda v: json.dumps(v, ensure_ascii=False)
    out = ['{"stats": ' + j(got["stats"]) + ",", '"features": {"bindata": ' + j(f["bindata"]) + ","]
    for key, end in (("char_shapes", "],"), ("para_shapes", "],"), ("paras", "]}}")):
        out.append(f'"{key}": [' + ",\n".join(j(r) for r in f[key]) + end)
    return "\n".join(out) + "\n"


class Golden(unittest.TestCase):
    def check(self, name: str, out: Path, st: dict) -> None:
        self.assertEqual(check_hwpx_file(str(out)), [])
        got = {"features": hwpx_features(out), "stats": json.loads(json.dumps({k: st[k] for k in sorted(st)}, ensure_ascii=False))}
        path = GOLDEN / f"{name}.json"
        if os.environ.get("YEONE_WRITE_GOLDEN") == "1":
            GOLDEN.mkdir(exist_ok=True)
            path.write_text(dump(got), encoding="utf-8")
            return
        want = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(got["features"], want["features"], f"{name}: 문서 특징이 골든과 다름")
        self.assertEqual({k: got["stats"].get(k) for k in want["stats"]}, want["stats"], f"{name}: 통계가 골든과 다름")

    def test_page_default(self):
        from yeone_ocr.hwpx_builder import build_hwpx
        with tempfile.TemporaryDirectory() as td:
            td = Path(td); recs, ctx = records(td)
            blocks = assemble_blocks(recs, layout="page", grades=True, check_list="all", legend=True, join_pages=True, whole_offset=10, context=ctx)
            blocks.append(Block(type="report", text="줄1\n줄2"))
            out = td / "page.hwpx"
            self.check("page_default", out, build_hwpx(blocks, str(out), highlight=True, page_breaks=True, memos=True))

    def test_page_default_plain(self):
        from yeone_ocr.hwpx_builder import build_hwpx
        with tempfile.TemporaryDirectory() as td:
            td = Path(td); recs, ctx = records(td)
            blocks = assemble_blocks(recs, layout="page", grades=True, check_list="all", legend=True, join_pages=True, whole_offset=10, context=ctx)
            blocks.append(Block(type="report", text="줄1\n줄2"))
            out = td / "plain.hwpx"
            self.check("page_default_plain", out, build_hwpx(blocks, str(out), highlight=False, page_breaks=False, memos=False))

    def test_flow_default(self):
        from yeone_ocr.hwpx_builder import build_hwpx
        with tempfile.TemporaryDirectory() as td:
            td = Path(td); out = td / "flow.hwpx"
            self.check("flow_default", out, build_hwpx(flow_blocks(td), str(out)))

    def test_flow_template(self):
        from yeone_ocr.hwpx_template import build_hwpx_template
        with tempfile.TemporaryDirectory() as td:
            td = Path(td); tpl = td / "양식.hwpx"; make_template(tpl); out = td / "t.hwpx"
            self.check("flow_template", out, build_hwpx_template(template_blocks(td), str(out), str(tpl), highlight=True, memos=True))

    def test_flow_template_plain(self):
        from yeone_ocr.hwpx_template import build_hwpx_template
        with tempfile.TemporaryDirectory() as td:
            td = Path(td); tpl = td / "양식.hwpx"; make_template(tpl); out = td / "t.hwpx"
            self.check("flow_template_plain", out, build_hwpx_template(template_blocks(td), str(out), str(tpl), highlight=False, memos=False))

    def test_flow_template2_pagenum(self):
        from yeone_ocr.hwpx_template import build_hwpx_template
        with tempfile.TemporaryDirectory() as td:
            td = Path(td); tpl = td / "양식2.hwpx"; make_template2(tpl, page_number=True); out = td / "t2.hwpx"
            blocks = [Block(type="heading", text="菜　下", level=2, role="section", anchors=[(0, "PDF 1쪽")]),
                      Block(type="para", text="味苦寒有毒〔主/生〕大水", role="bonjing", spans=[(0, 2, "green"), (2, 4, "blue")]),
                      Block(type="image", path=tiny_png(td / "g.png"), kind="원형나경도", frac_w=0.9, ratio=1.3),
                      Block(type="para", text="味辛", role="body", anchors=[(1, "PDF 2쪽"), (1, "PDF 2쪽")])]
            self.check("flow_template2_pagenum", out, build_hwpx_template(blocks, str(out), str(tpl)))

    def test_flow_records_template3(self):
        """내용별 실제 길(판독 기록 → 내용별 조립(굵기 포함) → 本草 짜임 → 본초학 이름의 양식)."""
        from yeone_ocr.hwpx_template import build_hwpx_template
        with tempfile.TemporaryDirectory() as td:
            td = Path(td); tpl = td / "양식3.hwpx"; make_template3(tpl); out = td / "t3.hwpx"
            lead = "  香薷　"
            bold = {(1, "b1", 1): [(len(lead), len(lead) + 6, "bold"), (len(lead) - 1, len(lead) - 1, "gap")],
                    (2, "b3", 0): [(B3.index("味"), B3.index("日"), "bold")]}
            self.check("flow_records_template3", out, build_hwpx_template(flow_blocks(td, bold), str(out), str(tpl)))


if __name__ == "__main__":
    unittest.main()
