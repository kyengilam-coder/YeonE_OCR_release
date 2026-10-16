"""기본 규칙: 원문 구조 보존, 실제 한글 스타일 연결, 기존 양식 모양 보존."""
import tempfile
import unittest
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

from yeone_ocr.blocks import Block
from yeone_ocr.hwpx_builder import build_hwpx
from yeone_ocr.hwpx_template import build_hwpx_template
from yeone_ocr.hwpx_writer import HH, HP
from yeone_ocr.presets import resolve_preset
from yeone_ocr.structure import apply_structure


def read_parts(path):
    with zipfile.ZipFile(path) as archive:
        header = ET.fromstring(archive.read("Contents/header.xml"))
        section = ET.fromstring(archive.read("Contents/section0.xml"))
    styles = {s.get("id"): s for s in header.iter(f"{HH}style")}
    paragraphs = {"".join(t.text or "" for t in p.findall(f"{HP}run/{HP}t")): p for p in section.findall(f"{HP}p")}
    return header, styles, paragraphs


class BasicStyles(unittest.TestCase):
    def test_basic_uses_source_structure_without_splitting_or_inventing_headings(self):
        source = [Block(type="heading", text="원문 제목", level=1),
                  Block(type="para", text="苦瓠　味苦寒有毒。", cstyles=[(3, 7, "bold")]),
                  Block(type="para", text="［唐本注云］正文。"),
                  Block(type="para", text="小注", kind="소주"),
                  Block(type="heading", text="그림 제목", kind="그림제목", level=3)]
        blocks, _ = apply_structure(source, "basic")
        self.assertEqual([b.text for b in blocks], [b.text for b in source])
        self.assertEqual([b.type for b in blocks], [b.type for b in source])
        self.assertEqual([b.role for b in blocks], ["chapter", "body", "body", "small", "caption"])
        self.assertFalse(any(b.cstyles for b in blocks))

    def test_standalone_hwpx_has_all_eight_styles_and_paragraph_references(self):
        roles = ["chapter", "section", "subsection", "body", "commentary", "small", "caption", "note"]
        names = ["대제목", "중제목", "소제목", "문단-1", "문단-2", "문단-3", "문단-4", "문단-5"]
        blocks = [Block(type="heading" if i < 3 else "para", text=f"원문 {i}", role=role, level=i + 1 if i < 3 else 0)
                  for i, role in enumerate(roles)]
        blocks[3].anchors = [(1, "PDF_1")]
        blocks[3].memos = [(1, "검토 메모")]
        blocks[3].spans = [(0, 2, "green")]
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "기본.hwpx"
            stats = build_hwpx(blocks, str(path), preset="basic")
            header, styles, paragraphs = read_parts(path)
            self.assertEqual(stats["validate_issues"], 0)
            self.assertEqual((stats["bookmarks"], stats["memos"]), (1, 1))
            self.assertTrue(set(names).issubset({s.get("name") for s in styles.values()}))
            for i, name in enumerate(names):
                paragraph = paragraphs[f"원문 {i}"]
                style = styles[paragraph.get("styleIDRef")]
                self.assertEqual(style.get("name"), name)
                self.assertEqual(paragraph.get("paraPrIDRef"), style.get("paraPrIDRef"))
                ppr = next(p for p in header.iter(f"{HH}paraPr") if p.get("id") == paragraph.get("paraPrIDRef"))
                heading = ppr.find(f"{HH}heading")
                self.assertTrue(heading is None or heading.get("type") == "NONE")

    def test_template_keeps_existing_names_and_inherits_legacy_style_shapes(self):
        from hwpx import HwpxDocument
        with tempfile.TemporaryDirectory() as td:
            template, output = Path(td) / "양식.hwpx", Path(td) / "결과.hwpx"
            doc = HwpxDocument.new()
            header = doc.oxml.headers[0]
            custom_char = doc.ensure_run_style(size=19, bold=True)
            custom_para = header.ensure_paragraph_format(base_para_pr_id=0, alignment="RIGHT")
            exact = header.ensure_style("대제목", style_type="PARA", para_pr_id_ref=custom_para, char_pr_id_ref=custom_char)
            header.ensure_style("제목-절", style_type="PARA", para_pr_id_ref=custom_para, char_pr_id_ref=custom_char)
            doc.save_to_path(str(template)); doc.close()
            blocks = [Block(type="heading", text="장 제목", role="chapter", level=1),
                      Block(type="heading", text="절 제목", role="section", level=2)]
            stats = build_hwpx_template(blocks, str(output), str(template), preset=resolve_preset("basic"))
            _, styles, paragraphs = read_parts(output)
            self.assertEqual((stats["validate_issues"], stats["missing"]), (0, []))
            self.assertEqual(paragraphs["장 제목"].get("styleIDRef"), exact)
            for text in ("장 제목", "절 제목"):
                style = styles[paragraphs[text].get("styleIDRef")]
                self.assertEqual((style.get("paraPrIDRef"), style.get("charPrIDRef")), (custom_para, custom_char))
            self.assertEqual(stats["styles"], {"chapter": "대제목", "section": "중제목"})


if __name__ == "__main__":
    unittest.main()
