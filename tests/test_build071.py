"""최종 문서에서만 변환하고, 원본 기록·문맥 판단 캐시·미확정 상태를 보존한다."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from zipfile import ZipFile

import fitz

from yeone_ocr import cli
from yeone_ocr.schema import Column, PageReading, Paragraph, Region
from yeone_ocr.state import Work


class BuildNormalization(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.pdf = self.root / "source.pdf"
        with fitz.open() as document:
            document.new_page()
            document.save(self.pdf)
        self.work = Work(str(self.pdf), str(self.root / "work"))
        reading = PageReading(pdf_page=1, regions=[Region(id="r1", kind="본문", bbox=[0, 0, 1000, 1000],
                              paragraphs=[Paragraph(columns=[Column(col_from_right=1, text="髒，汉书。")])])])
        self.record = {"file": self.pdf.name, "pdf_page": 1, "pass1": reading.model_dump(), "merged": reading.model_dump(), "crops": {}}
        self.work.save_record(1, self.record)
        self.original = self.work.record_path(1).read_text(encoding="utf-8")
        cli.CANCEL.clear()

    def args(self, *flags):
        return cli.build_parser().parse_args(["build", str(self.pdf), "--work", str(self.work.dir),
                    "--reader", "openai", "--model", "gpt-test", "--no-context", "--no-highlight", *flags])

    def run_build(self, args):
        with fitz.open(self.pdf) as document:
            return cli.cmd_build(args, self.work, document)

    def test_final_hwpx_docx_normalized_raw_preserved_cached_and_review_linked(self):
        calls = []
        class Reader:
            model = "gpt-test"
            usage = {"calls": 0}
            def resolve_text_choices(self, items):
                calls.append(items)
                self.usage = {"calls": len(calls), "prompt_tokens": 100, "output_tokens": 10}
                return {item["id"]: "臟" if "臟" in item["candidates"] else item["candidates"][-1] for item in items}

        with patch.object(cli, "create_reader", return_value=Reader()) as factory:
            self.assertEqual(self.run_build(self.args("--docx")), 0)
            self.assertEqual(factory.call_count, 1)
            first_calls = len(calls)
            self.assertEqual(self.run_build(self.args("--docx")), 0)
            self.assertEqual(len(calls), first_calls)
            self.assertEqual(factory.call_count, 1)
        self.assertEqual(self.work.record_path(1).read_text(encoding="utf-8"), self.original)
        for name, xml_name in (("source.hwpx", "Contents/section0.xml"), ("source.docx", "word/document.xml")):
            with ZipFile(self.work.out_dir / name) as archive:
                xml = archive.read(xml_name).decode("utf-8")
            self.assertIn("漢書", xml)
            self.assertIn("臟", xml)
            self.assertNotIn("汉书", xml)
        audit = json.loads((self.work.out_dir / "source_normalization.json").read_text(encoding="utf-8"))
        self.assertTrue(audit["complete"])
        self.assertGreater(audit["reader"]["cache_hits"], 0)
        from yeone_ocr.review_html import make_review
        review = Path(make_review(self.work, [self.record])).read_text(encoding="utf-8")
        self.assertIn("source_normalization.html", review)
        self.assertTrue((self.work.out_dir / "source_normalization.html").exists())

    def test_unavailable_model_retains_candidates_and_returns_review_code(self):
        with patch.object(cli, "create_reader", side_effect=cli.ReaderError("설정한 API 키가 없습니다.")):
            self.assertEqual(self.run_build(self.args()), 5)
        audit = json.loads((self.work.out_dir / "source_normalization.json").read_text(encoding="utf-8"))
        self.assertFalse(audit["complete"])
        self.assertGreater(audit["summary"]["context_unresolved"], 0)
        self.assertTrue((self.work.out_dir / "source.hwpx").exists())
        self.assertEqual(self.work.record_path(1).read_text(encoding="utf-8"), self.original)

    def test_disabled_normalization_has_no_model_calls_and_replaces_old_audit(self):
        (self.work.out_dir / "source_normalization.json").write_text('{"enabled":true}', encoding="utf-8")
        with patch.object(cli, "create_reader") as factory:
            self.assertEqual(self.run_build(self.args("--no-normalize")), 0)
            factory.assert_not_called()
        audit = json.loads((self.work.out_dir / "source_normalization.json").read_text(encoding="utf-8"))
        self.assertFalse(audit["enabled"])
        with ZipFile(self.work.out_dir / "source.hwpx") as archive:
            self.assertIn("髒，汉书。", archive.read("Contents/section0.xml").decode("utf-8"))

    def test_punctuation_only_custom_rules_reach_hwpx_docx_and_audit_without_models(self):
        path = self.root / "punctuation.json"
        path.write_text(json.dumps([{ "find": "，", "replace": " / "}], ensure_ascii=False), encoding="utf-8")
        with patch.object(cli, "create_reader") as factory:
            self.assertEqual(self.run_build(self.args("--no-traditional", "--no-yun", "--punctuation-rules", str(path), "--docx")), 0)
            factory.assert_not_called()
        for name, xml_name in (("source.hwpx", "Contents/section0.xml"), ("source.docx", "word/document.xml")):
            with ZipFile(self.work.out_dir / name) as archive:
                self.assertIn("髒 / 汉书。", archive.read(xml_name).decode("utf-8"))
        audit = json.loads((self.work.out_dir / "source_normalization.json").read_text(encoding="utf-8"))
        self.assertEqual(audit["options"], {"traditional": False, "yun": False, "punctuation": True})
        self.assertTrue(audit["custom_punctuation"])
        self.assertTrue(audit["complete"])
        self.assertEqual(self.work.record_path(1).read_text(encoding="utf-8"), self.original)

    def test_disabling_all_three_stages_is_equivalent_to_no_normalize(self):
        with patch.object(cli, "create_reader") as factory:
            self.assertEqual(self.run_build(self.args("--no-traditional", "--no-yun", "--no-punctuation")), 0)
            factory.assert_not_called()
        audit = json.loads((self.work.out_dir / "source_normalization.json").read_text(encoding="utf-8"))
        self.assertFalse(audit["enabled"])

    def test_invalid_rules_stop_run_before_ocr_but_disabled_punctuation_ignores_them(self):
        args = cli.build_parser().parse_args(["run", str(self.pdf), "--work", str(self.work.dir), "--punctuation-rules", str(self.root / "missing.json")])
        with patch.object(cli, "cmd_read") as read:
            with fitz.open(self.pdf) as doc:
                self.assertEqual(cli.cmd_run(args, self.work, doc), 2)
            read.assert_not_called()
        args.no_punctuation = True
        self.assertIsNone(cli._check_document(args))

    def test_cache_does_not_confuse_repeated_target_positions(self):
        inputs = [dict(id="1", context="甲云乙云", source="云", candidates=["云", "雲"], target_offset=1),
                  dict(id="2", context="甲云乙云", source="云", candidates=["云", "雲"], target_offset=3)]
        class Reader:
            model = "gpt-test"
            usage = {}
            def resolve_text_choices(self, items):
                return {item["id"]: "云" if item["target_offset"] == 1 else "雲" for item in items}
        with patch.object(cli, "create_reader", return_value=Reader()):
            resolver, _ = cli._normalization_resolver(self.work, self.args())
            self.assertEqual(resolver(inputs), {"1": "云", "2": "雲"})
        with patch.object(cli, "create_reader") as factory:
            resolver, metadata = cli._normalization_resolver(self.work, self.args())
            self.assertEqual(resolver(inputs), {"1": "云", "2": "雲"})
            self.assertEqual(metadata["cache_hits"], 2)
            factory.assert_not_called()

    def test_provider_change_cannot_reuse_another_models_decision(self):
        item = dict(id="1", context="髒", source="髒", candidates=["髒", "臟"])
        class Reader:
            usage = {}
            def resolve_text_choices(self, items):
                return {"1": "臟"}
        with patch.object(cli, "create_reader", return_value=Reader()) as factory:
            first, _ = cli._normalization_resolver(self.work, self.args())
            self.assertEqual(first([item]), {"1": "臟"})
            other_args = self.args("--skip-answered", "same-model"); other_args.reader = "anthropic"; other_args.model = "claude-test"   # 이번 모델로 다시 묻기
            second, metadata = cli._normalization_resolver(self.work, other_args)
            self.assertEqual(second([item]), {"1": "臟"})
            self.assertEqual(factory.call_count, 2)
            self.assertEqual(metadata["cache_hits"], 0)

    def other_args(self, value):
        args = self.args("--skip-answered", value); args.reader = "anthropic"; args.model = "claude-test"
        return args

    def cache(self) -> dict:
        return json.loads((self.work.dir / "normalization_cache.json").read_text(encoding="utf-8"))

    def test_other_models_answers_are_reused_when_any_model(self):
        """--skip-answered any-model(기본): 다른 모델이 고른 답은 그대로 쓰고 못 고른 후보는 묻지 않는다 — 판독기를 바꿔도 1만여 건을 다시 사지 않게.
        다른 모델의 답은 이 모델의 열쇠로 옮겨 적지 않으므로 same-model 이면 이번 모델로 정말 다시 묻는다."""
        items = [dict(id="1", context="甲云乙", source="云", candidates=["云", "雲"], target_offset=1),
                 dict(id="2", context="丙云丁", source="云", candidates=["云", "雲"], target_offset=1)]
        class Gpt:
            model = "gpt-test"; usage = {}
            def resolve_text_choices(self, xs):
                return {x["id"]: ("雲" if x["id"] == "1" else "") for x in xs}         # 1 은 고름, 2 는 못 고름
        with patch.object(cli, "create_reader", return_value=Gpt()):
            resolver, _ = cli._normalization_resolver(self.work, self.args())
            self.assertEqual(resolver(items), {"1": "雲"})
        before = self.cache()
        self.assertEqual(before["askers"], [["openai", "gpt-test"]])
        with patch.object(cli, "create_reader") as factory:
            resolver, meta = cli._normalization_resolver(self.work, self.other_args("any-model"))
            self.assertEqual(resolver(items), {"1": "雲"})
            factory.assert_not_called()
        self.assertEqual((meta["cache_hits"], meta["other_hits"], meta["remembered"], meta["other_remembered"], meta["asked"]), (1, 1, 1, 1, 0))
        after = self.cache()
        self.assertEqual(after["schema_version"], 1)
        self.assertEqual((set(after["choices"]), set(after["undecided"])), (set(before["choices"]), set(before["undecided"])))   # 남의 답을 내 열쇠로 적지 않음
        self.assertIn(["openai", "gpt-test"], after["askers"])
        asked = []
        class Claude:
            model = "claude-test"; usage = {}
            def resolve_text_choices(self, xs):
                asked.extend(x["id"] for x in xs); return {x["id"]: "云" for x in xs}
        with patch.object(cli, "create_reader", return_value=Claude()) as factory:
            resolver, meta = cli._normalization_resolver(self.work, self.other_args("same-model"))
            self.assertEqual(resolver(items), {"1": "云", "2": "云"})
            self.assertEqual(factory.call_count, 1)
        self.assertEqual((sorted(asked), meta["other_hits"], meta["cache_hits"]), (["1", "2"], 0, 0))
        self.assertEqual(self.cache()["askers"], [["anthropic", "claude-test"], ["openai", "gpt-test"]])

    def test_old_cache_without_askers_finds_other_model_from_report_and_remembers_it(self):
        """0.7.4 까지의 재사용 기록에는 askers 가 없다: out/*_normalization.json 의 reader 로 찾고(깨진·모델을 끈 보고는 건너뜀), 찾으면 askers 에 적어 둔다
        — 보고는 다음 문서 만들기에서 이번 모델로 덮이므로."""
        item = dict(id="1", context="甲云乙", source="云", candidates=["云", "雲"], target_offset=1)
        class Gpt:
            model = "gpt-test"; usage = {}
            def resolve_text_choices(self, xs):
                return {"1": "雲"}
        with patch.object(cli, "create_reader", return_value=Gpt()):
            resolver, _ = cli._normalization_resolver(self.work, self.args())
            self.assertEqual(resolver([item]), {"1": "雲"})
        old = self.cache(); del old["askers"]
        (self.work.dir / "normalization_cache.json").write_text(json.dumps(old), encoding="utf-8")
        out = self.work.out_dir
        (out / "a_normalization.json").write_text("{깨짐", encoding="utf-8")
        (out / "b_normalization.json").write_text("[1, 2]", encoding="utf-8")
        (out / "c_normalization.json").write_text(json.dumps({"reader": "gpt"}), encoding="utf-8")
        (out / "d_normalization.json").write_text(json.dumps({"reader": {"provider": "gemini", "model": "gemini-x", "disabled": True}}), encoding="utf-8")
        class Claude:
            model = "claude-test"; usage = {}
            def resolve_text_choices(self, xs):
                return {x["id"]: "云" for x in xs}
        self.assertEqual(cli._normalization_askers(self.work, "anthropic", "claude-test", {}, []), [])   # 깨진 보고·모델을 끈 보고는 건너뜀
        with patch.object(cli, "create_reader", return_value=Claude()) as factory:   # 찾을 길이 없으면 이번 모델로 묻는다(호출이 들 뿐 틀리지 않음)
            resolver, _ = cli._normalization_resolver(self.work, self.other_args("any-model"))
            self.assertEqual(resolver([item]), {"1": "云"})
            self.assertEqual(factory.call_count, 1)
        (self.work.dir / "normalization_cache.json").write_text(json.dumps(old), encoding="utf-8")   # 그 답은 지우고 옛 기록으로 되돌림
        (out / "source_normalization.json").write_text(json.dumps({"reader": {"provider": "openai", "model": "gpt-test"}}), encoding="utf-8")
        self.assertEqual(cli._normalization_askers(self.work, "anthropic", "claude-test", {}, [["x"], "y", None]), [("openai", "gpt-test")])
        with patch.object(cli, "create_reader") as factory:
            resolver, meta = cli._normalization_resolver(self.work, self.other_args("any-model"))
            self.assertEqual(resolver([item]), {"1": "雲"})
            factory.assert_not_called()
        self.assertEqual(meta["other_hits"], 1)
        self.assertIn(["openai", "gpt-test"], self.cache()["askers"])                    # 물을 것이 없어도 적어 둔다
        (out / "source_normalization.json").unlink()                                     # 보고가 덮이거나 지워져도
        with patch.object(cli, "create_reader") as factory:
            resolver, meta = cli._normalization_resolver(self.work, self.other_args("any-model"))
            self.assertEqual(resolver([item]), {"1": "雲"})
            factory.assert_not_called()
        undecided_only = {"k": "gpt-4.1", "j": "알 수 없는 모델", "i": 3}               # 못 고른 기록의 모델 이름으로도 찾는다(제공사는 이름으로 짐작)
        self.assertEqual(cli._normalization_askers(self.work, "openai", "gpt-test", undecided_only, []), [("openai", "gpt-4.1")])

    def test_plan_counts_other_models_and_matches_the_build(self):
        """실행 전 어림(normalization_plan)도 같은 규칙으로 센다: any-model 이면 다른 모델이 고른 답·못 고른 후보를 빼고, same-model 이면 모두 새로 묻는다.
        어림의 '새로 물을 것' = 실제 문서 만들기에서 물은 수."""
        paras = [Paragraph(columns=[Column(col_from_right=1, text=f"甲{i:03d}云乙。")]) for i in range(1, 4)]
        reading = PageReading(pdf_page=1, regions=[Region(id="r1", kind="본문", bbox=[0, 0, 1000, 1000], paragraphs=paras)])
        self.work.save_record(1, {"file": self.pdf.name, "pdf_page": 1, "pass1": reading.model_dump(), "merged": reading.model_dump(), "crops": {}})
        recs = self.work.records([1])
        class Gpt:
            model = "gpt-test"; usage = {}
            def resolve_text_choices(self, xs):                                          # 001 은 고름, 002 는 못 고름, 003 은 답에서 빠짐(다시 물을 것)
                return {x["id"]: ("雲" if "001" in x["context"] else "") for x in xs if "003" not in x["context"]}
        with patch.object(cli, "create_reader", return_value=Gpt()):
            self.run_build(self.args())
        plan = cli.normalization_plan(self.work, recs, "anthropic", "claude-test")
        self.assertEqual({k: plan[k] for k in ("total", "reused", "remembered", "ask", "calls", "other_reused", "other_remembered")},
                         {"total": 3, "reused": 1, "remembered": 1, "ask": 1, "calls": 1, "other_reused": 1, "other_remembered": 1})
        self.assertGreater(plan["ask_chars"], len("甲003云乙"))
        same = cli.normalization_plan(self.work, recs, "anthropic", "claude-test", any_model=False)
        self.assertEqual((same["ask"], same["reused"], same["other_reused"], same["other_remembered"]), (3, 0, 0, 0))
        self.assertGreater(same["ask_chars"], plan["ask_chars"])
        calls = []
        class Claude:
            model = "claude-test"; usage = {}
            def resolve_text_choices(self, xs):
                calls.append(len(xs)); return {x["id"]: "云" for x in xs}
        with patch.object(cli, "create_reader", return_value=Claude()):
            self.assertEqual(self.run_build(self.other_args("any-model")), 5)                # 못 고른 후보는 원문 유지 → 확인 필요(같은 모델이 못 고른 것과 같게)
        audit = json.loads((self.work.out_dir / "source_normalization.json").read_text(encoding="utf-8"))
        self.assertEqual((audit["reader"]["asked"], calls), (plan["ask"], [1]))
        self.assertEqual((audit["reader"]["other_hits"], audit["reader"]["other_remembered"]), (1, 1))
        html = (self.work.out_dir / "source_normalization.html").read_text(encoding="utf-8")
        self.assertIn("다른 모델이 앞서 고른 답을 그대로 씀 1건", html); self.assertIn("--skip-answered same-model", html)

    def test_simplified_original_cannot_be_accepted_as_final_traditional(self):
        item = dict(id="1", context="发展", source="发", candidates=["发", "發", "髮"],
                    source_is_valid_traditional=False)
        class Reader:
            usage = {}
            def resolve_text_choices(self, items):
                return {"1": "发"}
        with patch.object(cli, "create_reader", return_value=Reader()):
            resolver, _ = cli._normalization_resolver(self.work, self.args())
            self.assertEqual(resolver([item]), {})
        saved = json.loads((self.work.dir / "normalization_cache.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["choices"], {})                                     # 간체 원문은 확정 답으로 남기지 않는다
        self.assertEqual(list(saved["undecided"].values()), ["gpt-test"])          # 못 고른 후보로만 기억
        with patch.object(cli, "create_reader") as factory:                        # 같은 모델이면 다시 묻지 않는다
            resolver, metadata = cli._normalization_resolver(self.work, self.args())
            self.assertEqual(resolver([item]), {})
            factory.assert_not_called(); self.assertEqual(metadata["remembered"], 1)

    def test_model_sees_a_short_window_once_per_distinct_context_and_progress_is_logged(self):
        """모델에는 후보마다 앞뒤 60자만 보내고, 앞뒤 글이 같은 후보는 한 번만 묻는다. 문서 쪽 묶음은 80건, 진행은 기록에 남는다."""
        long = "甲" * 700
        items = [dict(id=str(i), context=long + "云" + long, source="云", candidates=["云", "雲"], target_offset=700,
                      original_context=long + "云" + long, original_source="云", original_target_offset=700) for i in range(1, 4)]
        sent = []
        class Reader:
            model = "gpt-test"; usage = {"calls": 1}
            def resolve_text_choices(self, xs):
                sent.append(xs); return {x["id"]: "云" for x in xs}
        with patch.object(cli, "create_reader", return_value=Reader()):
            resolver, meta = cli._normalization_resolver(self.work, self.args())
            self.assertEqual(resolver(items), {"1": "云", "2": "云", "3": "云"})
        self.assertEqual(len(sent), 1); self.assertEqual(len(sent[0]), 1)              # 셋이 같은 글 → 하나만
        x = sent[0][0]
        self.assertEqual((len(x["context"]), x["target_offset"], x["context"][x["target_offset"]]), (121, 60, "云"))
        self.assertEqual((len(x["original_context"]), x["original_target_offset"]), (61, 30))
        self.assertEqual((meta["asked"], meta["deduped"]), (1, 2)); self.assertGreater(resolver.batch_size, 10 ** 6)   # 문서 쪽은 후보 전체를 한 번에
        other = [dict(id="9", context="乙云丙", source="云", candidates=["云", "雲"], target_offset=1),
                 dict(id="10", context="丁云戊", source="云", candidates=["云", "雲"], target_offset=1)]
        class Partial:                                                             # 하나는 '못 고름', 하나는 답에서 빠짐
            model = "gpt-test"; usage = {"calls": 1}
            def resolve_text_choices(self, xs):
                return {"9": ""}
        with patch.object(cli, "create_reader", return_value=Partial()):
            resolver, meta = cli._normalization_resolver(self.work, self.args())
            self.assertEqual(resolver(other), {})
        with patch.object(cli, "create_reader", return_value=Reader()):            # 빠졌던 것만 다시 묻는다
            sent.clear(); resolver, meta = cli._normalization_resolver(self.work, self.args())
            self.assertEqual(resolver(other), {"10": "云"})
        self.assertEqual([[x["id"] for x in b] for b in sent], [["10"]]); self.assertEqual(meta["remembered"], 1)
        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            resolver.progress(0, 9000); resolver.progress(9000, 9000)
        self.assertIn("후보 9,000건", buf.getvalue()); self.assertIn("새로 물을 것만 80건씩", buf.getvalue()); self.assertIn("문맥 교정 9,000/9,000건", buf.getvalue())

    def test_only_new_candidates_fill_each_80_item_call(self):
        """재사용이 많아도 새로 물을 것만 80건씩 꽉 채워 묻는다(전에는 문서 쪽 묶음마다 몇 건씩 보내 772건에 98회 호출)."""
        item = lambda i: dict(id=str(i), context=f"{i:04d}云", source="云", candidates=["云", "雲"], target_offset=4)
        calls = []
        class Reader:
            model = "gpt-test"; usage = {}
            def resolve_text_choices(self, xs):
                calls.append(len(xs)); return {x["id"]: "云" for x in xs}
        with patch.object(cli, "create_reader", return_value=Reader()):
            resolver, _ = cli._normalization_resolver(self.work, self.args())
            self.assertEqual(len(resolver([item(i) for i in range(170)])), 170)
        self.assertEqual(calls, [80, 80, 10])
        calls.clear()
        mixed = [item(i) for i in range(0, 170, 2)] + [item(i) for i in range(1000, 1085)]   # 85건은 앞서 고른 답, 85건은 새로
        mixed.sort(key=lambda x: int(x["id"]) % 2)
        with patch.object(cli, "create_reader", return_value=Reader()):
            resolver, meta = cli._normalization_resolver(self.work, self.args())
            self.assertEqual(len(resolver(mixed)), 170)
        self.assertEqual((calls, meta["cache_hits"], meta["asked"]), ([80, 5], 85, 85))

    def test_document_path_keeps_80_item_batches_estimate_matches_and_stop_keeps_answers(self):
        """시험 검토에서 잡힌 것: normalize_blocks(문서 조립 경로)를 거쳐도 80건 묶음을 지키는지(resolver.batch_size 를 무시하면 32건씩 — 772건에 98회 호출의 재발),
        실행 전 어림(normalization_plan)의 '새로 물을 것' 이 실제 문서 만들기의 asked 와 같은지, [중지] 를 눌러도 받은 답은 기억하고 끝 코드 5 로 끝나는지."""
        from yeone_ocr.blocks import Block
        from yeone_ocr.normalization import normalize_blocks
        calls = []
        class Reader:
            model = "gpt-test"; usage = {}
            def resolve_text_choices(self, xs):
                calls.append(len(xs)); return {x["id"]: "云" for x in xs}
        blocks = [Block(type="para", text=f"甲{i:03d}云乙。", kind="본문") for i in range(100)]            # 앞뒤 글이 모두 다른 云 후보 100건
        with patch.object(cli, "create_reader", return_value=Reader()):
            resolver, meta = cli._normalization_resolver(self.work, self.args())
            normalize_blocks(blocks, resolver=resolver)
        self.assertEqual((calls, meta["asked"]), ([80, 20], 100))
        # 어림 = 실제: 한 쪽에 云 후보 60건이 든 기록으로 내용별 문서를 만든다
        paras = [Paragraph(columns=[Column(col_from_right=1, text=f"丙{i:03d}云丁。")]) for i in range(60)]
        reading = PageReading(pdf_page=1, regions=[Region(id="r1", kind="본문", bbox=[0, 0, 1000, 1000], paragraphs=paras)])
        self.work.save_record(1, {"file": self.pdf.name, "pdf_page": 1, "pass1": reading.model_dump(), "merged": reading.model_dump(), "crops": {}})
        recs = self.work.records([1])
        plan = cli.normalization_plan(self.work, recs, "openai", "gpt-test", layout="flow")
        self.assertEqual((plan["ask"], plan["calls"]), (60, 1))
        calls.clear()
        with patch.object(cli, "create_reader", return_value=Reader()):
            self.assertEqual(self.run_build(self.args("--layout", "flow")), 0)
        audit = json.loads((self.work.out_dir / "source_normalization.json").read_text(encoding="utf-8"))
        self.assertEqual((audit["reader"]["asked"], calls), (plan["ask"], [60]))
        self.assertEqual(cli.normalization_plan(self.work, recs, "openai", "gpt-test", layout="flow")["ask"], 0)     # 이제 모두 재사용
        # [중지]: 첫 묶음 뒤 멈춤 — 받은 답은 기록에 남고, 문서는 남은 후보를 원문대로 두고 만들어지며 끝 코드는 130(멈춤)
        paras = [Paragraph(columns=[Column(col_from_right=1, text=f"戊{i:03d}云己。")]) for i in range(100)]
        reading = PageReading(pdf_page=1, regions=[Region(id="r1", kind="본문", bbox=[0, 0, 1000, 1000], paragraphs=paras)])
        self.work.save_record(1, {"file": self.pdf.name, "pdf_page": 1, "pass1": reading.model_dump(), "merged": reading.model_dump(), "crops": {}})
        class Stopper(Reader):
            def resolve_text_choices(self, xs):
                cli.CANCEL.set(); return super().resolve_text_choices(xs)
        calls.clear()
        try:
            with patch.object(cli, "create_reader", return_value=Stopper()):
                self.assertEqual(self.run_build(self.args()), 130)
        finally:
            cli.CANCEL.clear()
        audit = json.loads((self.work.out_dir / "source_normalization.json").read_text(encoding="utf-8"))
        cache = json.loads((self.work.dir / "normalization_cache.json").read_text(encoding="utf-8"))
        self.assertEqual(calls, [80]); self.assertFalse(audit["complete"]); self.assertTrue((self.work.out_dir / "source.hwpx").exists())
        self.assertGreaterEqual(len(cache["choices"]), 80 + 60)
        calls.clear()
        with patch.object(cli, "create_reader", return_value=Reader()):                                       # 다시 만들면 남은 20건만 묻는다
            self.assertEqual(self.run_build(self.args()), 0)
        self.assertEqual(calls, [20])

    def test_context_model_off_keeps_rules_and_does_not_flag_incomplete(self):
        with patch.object(cli, "create_reader") as factory:
            self.assertEqual(self.run_build(self.args("--no-context-model")), 0)
            factory.assert_not_called()
        audit = json.loads((self.work.out_dir / "source_normalization.json").read_text(encoding="utf-8"))
        self.assertTrue(audit["reader"]["disabled"]); self.assertGreater(audit["summary"]["context_unresolved"], 0)
        with ZipFile(self.work.out_dir / "source.hwpx") as archive:
            xml = archive.read("Contents/section0.xml").decode("utf-8")
        self.assertIn("漢書", xml); self.assertIn("髒", xml)                            # 규칙 변환은 하고, 갈리는 후보는 원문

    def test_version_change_keeps_choices_and_undecided_but_prompt_change_reasks(self):
        """판 번호는 재사용 열쇠에 들어가지 않는다(판이 바뀔 때마다 고른 답·못 고른 기억을 버려 수천 건을 다시 샀다). 모델 지시문이 바뀌면 다시 묻는다."""
        items = [dict(id="1", context="甲云乙", source="云", candidates=["云", "雲"], target_offset=1),
                 dict(id="2", context="丙云丁", source="云", candidates=["云", "雲"], target_offset=1)]
        class Reader:
            model = "gpt-test"; usage = {}
            def resolve_text_choices(self, xs):
                return {x["id"]: ("云" if x["id"] == "1" else "") for x in xs}         # 하나는 고름, 하나는 못 고름
        with patch.object(cli, "create_reader", return_value=Reader()):
            resolver, _ = cli._normalization_resolver(self.work, self.args())
            self.assertEqual(resolver(items), {"1": "云"})
        with patch.object(cli, "__version__", "99.0.0-시험"), patch.object(cli, "create_reader") as factory:
            resolver, meta = cli._normalization_resolver(self.work, self.args())
            self.assertEqual(resolver(items), {"1": "云"})
            factory.assert_not_called()
            self.assertEqual((meta["cache_hits"], meta["remembered"]), (1, 1))
        from yeone_ocr import readers
        with patch.object(readers, "TEXT_CHOICE_SYSTEM", readers.TEXT_CHOICE_SYSTEM + "\n(바뀐 지시문)"), \
             patch.object(cli, "create_reader", return_value=Reader()) as factory:
            resolver, meta = cli._normalization_resolver(self.work, self.args())
            self.assertEqual(resolver(items), {"1": "云"})
            self.assertEqual((factory.call_count, meta["asked"], meta["cache_hits"]), (1, 2, 0))

    def test_cache_write_failure_warns_once_and_stops_buying(self):
        """재사용 기록을 쓰지 못하면 한 번만 알리고 남은 묶음은 묻지 않는다(기억 못 하는 답은 다음 실행에서 또 산다). 받은 답은 반영하고 보고는 미완료."""
        import io, contextlib
        item = lambda i: dict(id=str(i), context=f"{i:04d}云", source="云", candidates=["云", "雲"], target_offset=4)
        calls = []
        class Reader:
            model = "gpt-test"; usage = {}
            def resolve_text_choices(self, xs):
                calls.append(len(xs)); return {x["id"]: "云" for x in xs}
        buf = io.StringIO()
        with patch.object(cli, "create_reader", return_value=Reader()), patch.object(cli, "write_atomic", side_effect=OSError("디스크 가득")), \
             contextlib.redirect_stdout(buf):
            resolver, meta = cli._normalization_resolver(self.work, self.args())
            answers = resolver([item(i) for i in range(170)])
        self.assertEqual(calls, [80])                                                   # 첫 묶음 뒤 멈춤(전에는 묶음마다 알리며 계속 샀다)
        self.assertEqual(len(answers), 80)
        self.assertIn("재사용 기록을 저장하지 못했습니다", meta["error"]); self.assertEqual(meta["cache_error"], meta["error"])
        self.assertEqual(buf.getvalue().count("재사용 기록을 저장하지 못했습니다"), 1)
        self.assertFalse((self.work.dir / "normalization_cache.json").exists())

    def test_missing_or_misplaced_template_stops_before_any_model_work(self):
        """양식 HWPX 가 없거나 원본 쪽대로(--layout page)와 함께 주면 모델을 부르기 전에 끝 코드 2 — 문서도 만들지 않는다. run 도 판독 전에 막는다."""
        import io, contextlib
        missing = str(self.root / "없는_양식.hwpx")
        for flags, want in ((("--layout", "flow", "--template", missing), "양식 HWPX 가 없습니다"),
                            (("--template", str(self.pdf)), "내용별 문서(--layout flow)에만")):
            buf = io.StringIO()
            with patch.object(cli, "create_reader") as factory, contextlib.redirect_stdout(buf):
                self.assertEqual(self.run_build(self.args(*flags)), 2)
            factory.assert_not_called(); self.assertIn(want, buf.getvalue())
            self.assertFalse((self.work.out_dir / "source.hwpx").exists())
        run_args = cli.build_parser().parse_args(["run", str(self.pdf), "--work", str(self.work.dir), "--reader", "mock", "--layout", "flow", "--template", missing])
        buf = io.StringIO()
        with patch.object(cli, "cmd_read", side_effect=AssertionError("판독이 시작되면 안 된다")), contextlib.redirect_stdout(buf), fitz.open(self.pdf) as document:
            self.assertEqual(cli.cmd_run(run_args, self.work, document), 2)
        self.assertIn("양식 HWPX 가 없습니다", buf.getvalue())
        self.assertFalse(any(self.work.pages_dir.iterdir()))                            # 준비(쪽 꺼내기)도 하지 않았다
        self.assertIsNone(cli._check_document(self.args("--layout", "flow", "--template", str(self.pdf))))   # 있는 파일 + 내용별이면 통과

    def test_unusable_preset_stops_before_any_model_work(self):
        """내용별 문서의 짜임 규칙 파일이 없거나 --preset 과 예전 --structure 를 함께 주면 모델을 부르기 전에 끝 코드 2(run 도 판독 전에).
        원본 쪽대로에서는 짜임 규칙을 보지 않는다(쓰지 않는 규칙 파일이 없어도 막지 않음)."""
        import io, contextlib
        missing = str(self.root / "missing.json")
        for flags, want in ((("--layout", "flow", "--preset", "missing.json"), "짜임 규칙 파일이 없습니다: missing.json"),
                            (("--layout", "flow", "--preset", missing), f"짜임 규칙 파일이 없습니다: {missing}"),
                            (("--layout", "flow", "--preset", "honzo"), "짜임 규칙 'honzo' 를 모릅니다"),
                            (("--layout", "flow", "--preset", "kaibao", "--structure", "none"), "--preset 과 예전 이름 --structure 를 함께")):
            buf = io.StringIO()
            with patch.object(cli, "create_reader", side_effect=AssertionError("모델을 만들면 안 된다")) as factory, contextlib.redirect_stdout(buf):
                self.assertEqual(self.run_build(self.args(*flags)), 2)
            factory.assert_not_called(); self.assertIn(want, buf.getvalue())
            self.assertFalse((self.work.out_dir / "source.hwpx").exists())
        run_args = cli.build_parser().parse_args(["run", str(self.pdf), "--work", str(self.work.dir), "--reader", "mock", "--layout", "flow", "--preset", missing])
        buf = io.StringIO()
        with patch.object(cli, "cmd_read", side_effect=AssertionError("판독이 시작되면 안 된다")), contextlib.redirect_stdout(buf), fitz.open(self.pdf) as document:
            self.assertEqual(cli.cmd_run(run_args, self.work, document), 2)
        self.assertIn("짜임 규칙 파일이 없습니다", buf.getvalue())
        self.assertFalse(any(self.work.pages_dir.iterdir()))
        self.assertIsNone(cli._check_document(self.args("--preset", missing)))                  # 원본 쪽대로는 규칙을 보지 않는다
        for ref in ("kaibao", "regions"):
            self.assertIsNone(cli._check_document(self.args("--layout", "flow", "--preset", ref)))
        self.assertIsNone(cli._check_document(self.args("--layout", "flow", "--structure", "bencao")))   # 예전 이름만 주면 별칭
        buf = io.StringIO()                                                               # 시작 검사 뒤 규칙 파일이 없어졌으면: 다른 규칙으로 만들지 않고 모델도 부르지 않는다
        with patch.object(cli, "create_reader", side_effect=AssertionError("모델을 만들면 안 된다")), contextlib.redirect_stdout(buf), fitz.open(self.pdf) as document:
            self.assertFalse(cli.build_outputs(self.work, document, self.work.records([1]), self.args("--layout", "flow", "--preset", missing)))
        self.assertIn("문서를 만들지 않았습니다(판독 기록은 그대로)", buf.getvalue()); self.assertFalse(any(self.work.out_dir.glob("*.hwpx")))

    def test_textlayer_refuses_changed_source_without_archiving_records(self):
        """검색 가능한 PDF 는 결과 폴더를 읽기만 한다: 원본 PDF 의 바이트가 바뀌었으면(다시 저장·만든 PDF 를 원본 이름으로 둠) 기록을 보관함으로 옮기지 않고 끝 코드 2."""
        import io, contextlib
        self.assertTrue(self.work.source_matches())
        self.pdf.unlink()
        with fitz.open() as document:                                                   # 같은 이름으로 다시 저장 → 바이트가 다름
            document.new_page(); document.new_page()
            document.save(self.pdf)
        self.assertFalse(self.work.source_matches())
        args = cli.build_parser().parse_args(["textlayer", str(self.pdf), "--work", str(self.work.dir)])
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(cli.cmd_textlayer(args), 2)
        self.assertIn("판독 기록의 원본과 다릅니다", buf.getvalue())
        self.assertEqual(self.work.record_path(1).read_text(encoding="utf-8"), self.original)
        self.assertFalse((self.work.dir / "archives").exists())
        self.assertFalse(any(self.work.out_dir.glob("*.pdf")))


if __name__ == "__main__":
    unittest.main()
