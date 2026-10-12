"""0.7.1 문맥 정자·부호 정리: 원문 보존, 제한된 모델 선택, 실제 HWPX 출력."""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from zipfile import ZipFile

from yeone_ocr.blocks import Block
from yeone_ocr.normalization import extract_msr_rules, normalize_blocks, normalize_text, rule_data


def keep_valid(items):
    return {item["id"]: item["source"] for item in items if item["source"] in item["candidates"]}


class MsrData(unittest.TestCase):
    def test_full_ordered_macro_data_and_flags(self):
        data = rule_data()
        self.assertEqual(data["parameter_blocks"], 101)
        self.assertEqual(len(data["rules"]), 103)
        self.assertEqual(data["script_offset"], 196)
        self.assertEqual(data["sha256"], "ef5a067482dc042835763e9019e2dd8d121be1a0e1d112834b6418dfd2ba0ca1")
        rules = data["rules"]
        self.assertEqual([(r["find"], r["replace"]) for r in rules[:3]], [("，", ", "), ("、", "·"), ("。", ". ")])
        self.assertEqual(rules[-1]["find"], "餅")
        self.assertEqual([(a["id"], b["id"]) for a, b in zip(rules, rules[1:]) if a["source_block"] == b["source_block"]],
                         [(93, 94), (95, 96)])
        self.assertEqual((data["flags"]["MatchCase"], data["flags"]["FindRegExp"], data["flags"]["FindStyle"]), (0, 0, ""))

    def _fixture(self, **overrides):
        flags = dict(rule_data()["flags"], FindString="。", ReplaceString=". ", **overrides)
        statements = []
        for key, value in flags.items():
            literal = value if key == "Direction" else json.dumps(value, ensure_ascii=False)
            statements.append(f"{key} = {literal};")
        script = ('function Test() { HAction.GetDefault("AllReplace", HParameterSet.HFindReplace.HSet);'
                  'with (HParameterSet.HFindReplace) {' + "".join(statements) + '}'
                  'HAction.Execute("AllReplace", HParameterSet.HFindReplace.HSet);'
                  'HAction.Execute("AllReplace", HParameterSet.HFindReplace.HSet); }\x00')
        return script

    def test_extracts_data_never_executes_and_preserves_repeat(self):
        data = extract_msr_rules(b"binary header" + self._fixture().encode("utf-16le"), "fixture.msr")
        self.assertEqual([r["source_block"] for r in data["rules"]], [1, 1])
        with self.assertRaises(ValueError):
            extract_msr_rules(self._fixture(FindRegExp=1).encode("utf-16le"), "regex.msr")
        unsafe = self._fixture().replace("function Test() {", 'function Test() { malicious();')
        with self.assertRaises(ValueError):
            extract_msr_rules(unsafe.encode("utf-16le"), "not-executed.msr")
        with self.assertRaises(ValueError):
            extract_msr_rules(self._fixture(FindStyle="특정 서식").encode("utf-16le"), "styled.msr")


class ContextNormalization(unittest.TestCase):
    def test_clear_traditional_punctuation_and_yun_context(self):
        def resolve(items):
            return {item["id"]: choice for item, choice in zip(items, ["云", "雲", "雲", "云", "云"])}
        result = normalize_text("简体书，孔子雲：白云在天。浮云、雲云。", resolver=resolve)
        self.assertEqual(result.text, "簡體書, 孔子云 : 白雲在天. 浮雲·云云. ")
        self.assertFalse(result.review)
        self.assertTrue(any(c["stage"] == "model_context" for c in result.changes))

    def test_yun_cloud_phrase_across_attribution_boundary_needs_model(self):
        source = "陶雲中暑宜清熱。"
        out, pending = normalize_blocks([Block(type="para", text=source)])
        self.assertIn("陶雲中暑", out[0].text)
        self.assertFalse(pending["complete"])
        seen = []
        def resolve(items):
            seen.extend(items)
            return {i["id"]: "云" if i["source"] == "雲" else i["source"] for i in items}
        out, report = normalize_blocks([Block(type="para", text=source)], resolver=resolve)
        self.assertIn("陶云中暑", out[0].text)
        self.assertEqual(seen[0]["original_context"], source)
        self.assertTrue(report["complete"])

    def test_macro_hanzi_is_a_proposal_not_a_global_replacement(self):
        result = normalize_text("技術，白術；樹幹，干燥；贏得，羸弱；學校教育。", resolver=keep_valid)
        self.assertEqual(result.text, "技術, 白術; 樹幹, 干燥; 贏得, 羸弱; 學校教育. ")
        self.assertNotIn("朮", result.text)
        self.assertNotIn("樹乾", result.text)
        self.assertNotIn("羸得", result.text)

    def test_model_can_choose_different_characters_at_each_occurrence(self):
        requests = []
        def decide(items):
            requests.extend(items)
            decisions = keep_valid(items)
            for item in items:
                before = item["context"][:item["target_offset"]]
                if item["source"] == "術" and before.endswith("白"):
                    decisions[item["id"]] = "朮"
                elif item["source"] == "干" and before.endswith("樹"):
                    decisions[item["id"]] = "幹"
                elif item["source"] == "干" and item["context"][item["target_offset"] + 1:].startswith("燥"):
                    decisions[item["id"]] = "乾"
            return decisions
        out, report = normalize_blocks([Block(type="para", text="技術，白術；樹干，干燥。", page=12)], resolver=decide)
        self.assertEqual(out[0].text, "技術, 白朮; 樹幹, 乾燥. ")
        self.assertTrue(report["complete"])
        self.assertTrue(all(r["original_context"] == "技術，白術；樹干，干燥。" for r in requests))
        self.assertTrue(all(d["page"] == 12 and d["block"] == 0 for d in report["decisions"]))

    def test_simplified_only_source_is_never_accepted_as_completed(self):
        requests = []
        def keep_wrong(items):
            requests.extend(items)
            return {item["id"]: item["source"] for item in items}
        out, report = normalize_blocks([Block(type="para", text="白术发言。")], resolver=keep_wrong)
        self.assertEqual(out[0].text, "白术发言. ")
        for item in requests:
            self.assertFalse(item["source_is_valid_traditional"])
            self.assertNotIn(item["source"], item["candidates"])
        self.assertFalse(report["complete"])
        self.assertEqual(report["summary"]["context_unresolved"], 2)

    def test_valid_traditional_original_still_gets_context_choice(self):
        requests = []
        def choose(items):
            requests.extend(items)
            return keep_valid(items)
        out, report = normalize_blocks([Block(type="para", text="皇后台了。")], resolver=choose)
        self.assertTrue(any(item["source"] == "后" for item in requests))
        self.assertTrue(any(item["source"] == "了" for item in requests))
        self.assertTrue(report["complete"])
        self.assertEqual(out[0].text, "皇后台了. ")

    def test_missing_invalid_or_invented_choices_preserve_source(self):
        for reply in ({}, {"1": "任意插入"}, {"999": "朮"}, {"1": None}):
            with self.subTest(reply=reply):
                out, report = normalize_blocks([Block(type="para", text="白術")], resolver=lambda _: reply)
                self.assertEqual(out[0].text, "白術")
                self.assertFalse(report["complete"])
                self.assertEqual(report["summary"]["context_unresolved"], 1)

    def test_unresolved_yun_and_names_are_not_globally_changed(self):
        out, report = normalize_blocks([Block(type="para", text="張雲，揚子雲，雲氣，詩雲。")])
        self.assertEqual(out[0].text, "張雲, 揚子雲, 雲氣, 詩雲. ")
        self.assertEqual(report["summary"]["yun_ambiguous"], 4)
        self.assertFalse(report["complete"])
        out2, rep2 = normalize_blocks([Block(type="para", text="其雲於後。")],
                                     resolver=lambda xs: {x["id"]: "云" for x in xs if x["source"] == "雲"})
        self.assertEqual(out2[0].text, "其云於後. ")
        self.assertTrue(rep2["complete"])

    def test_english_and_korean_word_guards(self):
        requests = []
        def all_macro_changes(items):
            requests.extend(items)
            return {item["id"]: item["candidates"][-1] for item in items}
        out, report = normalize_blocks([Block(type="para", text="OCR logo하고 榮고 O o。")], resolver=all_macro_changes)
        self.assertEqual(out[0].text, "OCR logo하고 榮衛 . .. ")
        self.assertEqual(report["summary"]["guarded_rules"], 4)
        self.assertTrue(report["complete"])
        self.assertEqual([i["source"] for i in requests], ["榮고", "O", "o"])

    def test_originals_metadata_markers_and_checklists_never_mutated(self):
        blocks = [Block(type="meta", text="OCR고.pdf"),
                  Block(type="para", text="简体，〔疑: 云〕〔干/乾〕〔→PDF 3쪽〕。〔疑: 术"),
                  Block(type="note", text="〔고지: 简体〕"), Block(type="uncertain", items=["术，云"]),
                  Block(type="report", text="原文: 云。")]
        original = deepcopy(blocks)
        out, _ = normalize_blocks(blocks, resolver=keep_valid)
        self.assertEqual(blocks, original)
        self.assertEqual(out[1].text, "簡體, 〔疑: 云〕〔干/乾〕〔→PDF 3쪽〕. 〔疑: 术")
        for index in (0, 2, 3, 4):
            self.assertEqual(out[index], blocks[index])

    def test_offsets_and_memos_follow_inserted_spaces(self):
        block = Block(type="para", text="甲，白術。乙", spans=[(3, 4, "green"), (5, 6, "blue")],
                      memos=[(3, "원문 術"), (5, "乙 메모")])
        out, _ = normalize_blocks([block], resolver=lambda xs: {x["id"]: "朮" for x in xs})
        self.assertEqual(out[0].text, "甲, 白朮. 乙")
        self.assertEqual(out[0].spans, [(4, 5, "green"), (7, 8, "blue")])
        self.assertEqual(out[0].memos, [(4, "원문 術"), (7, "乙 메모")])
        self.assertEqual(block.memos[0][0], 3)

    def test_tables_are_independent_and_source_rebuild_does_not_add_spaces_twice(self):
        source = [Block(type="table", rows=[["简体；", "白術"], ["云雨", "詩雲"]], page=4)]
        def resolve(items):
            return {i["id"]: {"云雨": "雲", "詩雲": "云"}.get(i["original_context"], i["source"]) for i in items}
        first, report = normalize_blocks(source, resolver=resolve)
        second, _ = normalize_blocks(source, resolver=resolve)
        self.assertEqual(first, second)
        self.assertEqual(first[0].rows, [["簡體; ", "白術"], ["雲雨", "詩云"]])
        self.assertTrue(any(d.get("row") == 0 and d.get("column") == 1 for d in report["decisions"]))
        self.assertEqual(source[0].rows[0][0], "简体；")

    def test_resolver_failure_is_audited_without_secret_and_cancel_propagates(self):
        def fail(_):
            raise RuntimeError("api_key=DO-NOT-LEAK")
        out, report = normalize_blocks([Block(type="para", text="白術")], resolver=fail)
        self.assertEqual(out[0].text, "白術")
        self.assertFalse(report["complete"])
        self.assertEqual(report["resolver_errors"][0]["type"], "RuntimeError")
        self.assertNotIn("DO-NOT-LEAK", json.dumps(report))
        def cancel(_):
            raise KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            normalize_blocks([Block(type="para", text="白術")], resolver=cancel)

    def test_many_candidates_are_bounded_and_ids_unique(self):
        batches = []
        def decide(items):
            batches.append(items)
            return keep_valid(items)
        _, report = normalize_blocks([Block(type="para", text="術" * 65)], resolver=decide)
        self.assertEqual([len(b) for b in batches], [32, 32, 1])
        self.assertEqual(len({i["id"] for b in batches for i in b}), 65)
        self.assertTrue(report["complete"])

    def test_missing_opencc_is_explicit(self):
        with patch("yeone_ocr.collate.zh._cc", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "OpenCC"):
                normalize_blocks([Block(type="para", text="简体")])


class HwpxNormalizedOutput(unittest.TestCase):
    def test_hwpx_preserves_format_runs_table_and_original_audit(self):
        from lxml import etree
        from hwpx import HwpxDocument
        from yeone_ocr.hwpx_builder import build_hwpx, HP
        source = [Block(type="para", text="甲，白術。乙", spans=[(3, 4, "green")], memos=[(3, "原文 術")]),
                  Block(type="table", rows=[["简体", "云雨"]])]
        blocks, report = normalize_blocks(source, resolver=lambda xs: {x["id"]: "朮" if x["source"] == "術" else "雲" for x in xs})
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "normalized.hwpx"
            build_hwpx(blocks, str(output))
            reopened = HwpxDocument.open(str(output))
            try:
                self.assertTrue(reopened.paragraphs)
            finally:
                reopened.close()
            with ZipFile(output) as archive:
                xml = etree.fromstring(archive.read("Contents/section0.xml"))
                runs = [(r.get("charPrIDRef"), "".join(t.text or "" for t in r.findall(HP + "t"))) for r in xml.iter(HP + "run")]
                self.assertTrue(any(text == "朮" for _, text in runs))
                self.assertIn("簡體", "".join(xml.itertext()))
                self.assertIn("雲雨", "".join(xml.itertext()))
                self.assertIn("原文 術", "".join(xml.itertext()))
                self.assertEqual(len(list(xml.iter(HP + "tbl"))), 1)
        self.assertEqual(source[0].text, "甲，白術。乙")
        self.assertEqual(report["blocks"][0]["before"], "甲，白術。乙")


if __name__ == "__main__":
    unittest.main()
