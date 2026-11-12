"""Local-first orchestration with actual projects/plans and fake API transport."""
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from PIL import Image

from yeone_ocr import cli
from yeone_ocr.local_first import command, POLICY
from yeone_ocr.comparison import ComparisonProject
from yeone_ocr.state import Work


class LocalFirstTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / "source.png"
        Image.new("RGB", (400, 200), "white").save(self.source)
        self.work = self.root / "work"
        self.confidence = 99
        self.omit_geometry = False
        self.calls = []
        self.local_calls = []
        self.api_failure = False
        self.transport_callback = None
        self.recognized_text = "天地玄黃"
        cli.CANCEL.clear()
        self.addCleanup(cli.CANCEL.clear)
        self.env = patch.dict(os.environ, {"OPENAI_API_KEY": "synthetic-test-key"})
        self.env.start()
        self.addCleanup(self.env.stop)
        for name in ("make_textlayer", "cmd_build", "cmd_review", "cmd_report"):
            mock = patch.object(cli, name, return_value=0)
            setattr(self, name, mock.start())
            self.addCleanup(mock.stop)
        p = patch("yeone_ocr.automatic_ocr.run_automatic_page", side_effect=self.recognize)
        p.start()
        self.addCleanup(p.stop)
        p = patch("yeone_ocr.comparison_ai.create_client", side_effect=self.client)
        p.start()
        self.addCleanup(p.stop)

    def args(self, *extra):
        return cli.build_parser().parse_args(["run", str(self.source), "--work", str(self.work),
            "--reader", "openai", "--model", "synthetic-vision", "--mode", "horizontal_zh",
            "--pdf-only", *extra])

    def recognize(self, project, number, **kwargs):
        self.local_calls.append(number)
        # Reuse completed readings just as the actual engine wrapper does.
        if not project.page(number)["runs"]:
            raw = {"kind": "recognition", "schema_version": 1, "engine": "ppocrv6",
                "coordinate_system": "rendered-image pixels, xyxy", "text": self.recognized_text,
                "words": ([{"text": self.recognized_text, "bbox": [40, 20, 160, 70], "confidence": self.confidence,
                           "block": 1, "paragraph": 1, "line": 1}] if self.recognized_text and not self.omit_geometry else []),
                "source_path": str(project.source_path), "source_sha256": project.source_sha256,
                "page_number": number, "image_width": 400, "image_height": 200,
                "engine_version": "synthetic", "languages": "chi_tra", "psm": None,
                "duration_seconds": 0, "preprocessing": {}, "engine_metadata": {}}
            project.add_result(raw, request={"profile": "traditional", "direction": "horizontal", "dpi": 300,
                                             "engine_root": None, "policy": "local-three-engine-v1"})
            project.choose_baseline(number, "ppocrv6", automatic=True)
        kwargs["on_result"](None)
        return {"cancelled": False, "completed": ["ppocrv6"], "skipped": [],
                "selection": {"selected_engine": "ppocrv6"}}

    def client(self, *_args):
        def create(**kwargs):
            self.calls.append(kwargs)
            if self.api_failure:
                raise OSError("synthetic network failure")
            data = json.loads(kwargs["input"][0]["content"][0]["text"].split("Source DATA (JSON):\n")[1].split("\nReturn JSON")[0])
            result = {"items": [{"target_id": t["id"], "uncertain": False, "note": "verified synthetic",
                "proposals": []} for t in data["targets"]]}
            if self.transport_callback is not None:
                self.transport_callback()
            return SimpleNamespace(output_text=json.dumps(result), status="completed", model="synthetic-vision", id="test",
                                   usage=SimpleNamespace(input_tokens=12, output_tokens=8))
        return SimpleNamespace(responses=SimpleNamespace(create=create), close=lambda: None)

    def record(self, number=1):
        return Work(str(self.source), str(self.work), initialize=False).load_record(number)

    def test_default_run_routes_local_first_and_clean_v6_uses_zero_api(self):
        args = self.args()
        self.assertEqual(args.ocr_strategy, "local-first")
        with patch.object(cli, "cmd_read", side_effect=AssertionError("full-page AI forbidden")):
            self.assertEqual(cli.cmd_run(args), 0)
        self.assertEqual(self.calls, [])
        rec = self.record()
        self.assertEqual(rec["pipeline"], POLICY)
        self.assertEqual(rec["image_frame"], "full-page")
        self.assertEqual(rec["local_first"]["review"]["status"], "no-api-needed")
        self.make_textlayer.assert_called_once()

    def test_low_confidence_single_v6_review_crops_and_resume_skips_paid_call(self):
        self.confidence = 20
        self.assertEqual(command(self.args()), 0)
        self.assertEqual(len(self.calls), 1)
        rec = self.record()
        self.assertEqual(rec["local_first"]["review"]["status"], "reviewed")
        project = ComparisonProject.load(rec["local_first"]["project_path"])
        image = project.page(1)["ai_reviews"][0]["request"]["images"][0]
        self.assertNotEqual(image["source_bbox"], [0, 0, 400, 200])
        self.assertEqual(command(self.args()), 0)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(ComparisonProject.load(project.project_path).page(1)["runs"][0]["raw"]["text"], "天地玄黃")

    def test_record_carries_per_glyph_grades_for_document_colours(self):
        self.confidence = 20
        with patch.dict(os.environ, {"OPENAI_API_KEY": ""}):
            command(self.args())
        self.assertEqual(self.record()["local_grades"],
                         [{"region_id": "local-line-1", "para": 0, "start": 0, "end": 4, "grade": "blue"}])

    def test_skip_blank_reads_no_engine_and_saves_an_empty_record(self):
        self.assertEqual(command(self.args("--local-skip-blank")), 0)
        self.assertEqual(self.local_calls, [])
        rec = self.record()
        self.assertEqual((rec["model"], rec["local_first"]["review"]["status"]), ("blank-page", "blank-page"))
        self.assertEqual(rec["merged"]["regions"], [])
        self.assertEqual(self.calls, [])

    def test_options_are_recorded_and_ai_batch_reaches_the_plan(self):
        self.confidence = 20
        self.assertEqual(command(self.args("--local-ai-batch", "3")), 0)
        rec = self.record()
        self.assertEqual(rec["local_first"]["options"], {"ai_batch": 3})          # columns is the default now
        project = ComparisonProject.load(rec["local_first"]["project_path"])
        self.assertEqual(project.page(1)["ai_reviews"][0]["policy"]["batch_size"], 3)

    def test_two_column_order_follows_the_switch(self):
        from yeone_ocr import local_first
        seen = []
        real = local_first._record

        def spy(project, number, args, work, started, review, extras=None):
            seen.append((extras or {}).get("columns"))
            return real(project, number, args, work, started, review, extras)
        with patch.object(local_first, "_record", side_effect=spy):
            command(self.args())
            command(self.args("--no-local-columns", "--force"))
        self.assertTrue(seen[0] is True and seen[-1] is False)
        self.assertEqual(self.record()["local_first"]["options"], {"columns": False})

    def test_easyocr_left_out_of_horizontal_pages_unless_asked(self):
        seen = []
        original = self.recognize

        def capture(project, number, **kwargs):
            seen.append(kwargs.get("exclude", ()))
            return original(project, number, **kwargs)
        with patch("yeone_ocr.automatic_ocr.run_automatic_page", side_effect=capture):
            command(self.args())
            command(self.args("--local-easyocr", "--force"))
            command(self.args("--local-exclude-engines", "tesseract", "--force"))
        self.assertEqual(seen, [("easyocr",), (), ("easyocr", "tesseract")])

    def test_subscription_review_runs_through_the_signed_in_cli(self):
        from yeone_ocr import claude_subscription as subscription
        self.confidence = 20
        seen = []
        harness = self

        class FakeCLI:
            subscription = True

            def request(self, *, model_name, system, prompt, images, schema):
                data = json.loads(prompt.split("Source DATA (JSON):\n")[1].split("\nReturn JSON")[0])
                harness.calls.append(model_name)
                return json.dumps({"items": [{"target_id": t["id"], "uncertain": False, "note": "ok", "proposals": []}
                                             for t in data["targets"]]}), True, "end_turn", {"input_tokens": 3}, model_name

        def factory(settings, key):
            seen.append((settings.provider, settings.model, key))
            return FakeCLI()
        with patch("yeone_ocr.comparison_ai.create_client", side_effect=factory), \
                patch.object(subscription, "status", return_value={"usable": True, "plan": "max", "reason": None}):
            self.assertEqual(command(self.args("--local-ai-auth", "subscription", "--local-ai-accept",
                                               "--local-ai-page", "off", "--local-ai-batch", "1", "--local-ai-detail-tile", "0")), 0)
        self.assertEqual(seen[0], ("anthropic", "opus", subscription.SUBSCRIPTION_KEY))
        self.assertEqual(self.calls, ["opus"])
        rec = self.record()
        self.assertEqual(rec["local_first"]["review"]["ai_accept"], {"accepted": 0, "held": 0})
        self.assertEqual(rec["local_first"]["options"], {"ai_accept": True})

    def test_subscription_mode_without_sign_in_keeps_the_review_pending(self):
        from yeone_ocr import claude_subscription as subscription
        self.confidence = 20
        with patch.object(subscription, "status", return_value={"usable": False, "reason": "로그인 필요"}):  # OpenAI key present
            self.assertEqual(command(self.args("--local-ai-auth", "subscription")), 4)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.record()["local_first"]["review"]["status"], "pending")

    def test_ai_page_reading_goes_into_the_document_not_the_engine_reading(self):
        from yeone_ocr import ai_page, claude_subscription as subscription
        self.recognized_text = "天地玄黄宇宙洪荒"
        sent = []

        class FakeCLI:
            subscription = True

            def request(self, **kwargs):
                sent.append(kwargs)
                return json.dumps({"lines": ["天地玄黃宇宙洪荒"], "uncertain": []}), True, "end_turn", {}, "opus"
        with patch.object(subscription, "status", return_value={"usable": True, "plan": "max", "reason": None}), \
                patch.object(subscription, "SubscriptionClient", return_value=FakeCLI()):
            self.assertEqual(command(self.args("--local-ai-page", "prefer", "--no-local-ai-review")), 0)
        rec = self.record()
        self.assertIn("天地玄黃宇宙洪荒", json.dumps(rec["merged"], ensure_ascii=False))
        self.assertEqual(rec["local_first"]["ai_page"]["taken"], 1)
        self.assertIn("green", {g["grade"] for g in rec["local_grades"]})
        project = ComparisonProject.load(rec["local_first"]["project_path"])
        self.assertEqual(project.text(1), "天地玄黄宇宙洪荒")                  # engine reading kept for the editor
        self.assertEqual(len(sent), 1)
        self.assertEqual(rec["local_first"]["options"], {"ai_page": "prefer"})

    def test_ai_page_without_sign_in_keeps_the_engine_reading(self):
        from yeone_ocr import claude_subscription as subscription
        with patch.object(subscription, "status", return_value={"usable": False, "reason": "로그인 필요"}):
            self.assertEqual(command(self.args("--local-ai-page", "prefer", "--no-local-ai-review")), 0)
        self.assertNotIn("ai_page", self.record()["local_first"])

    def test_signed_in_subscription_turns_on_page_reading_and_tiled_review_by_default(self):
        from yeone_ocr import claude_subscription as subscription, local_first
        args = self.args()
        with patch.dict(os.environ, {"YEONE_OCR_CLAUDE_AUTH": ""}), \
                patch.object(subscription, "status", return_value={"usable": True, "plan": "max", "reason": None}):
            resolved = local_first._resolve_ai_defaults(args, cli)
        self.assertEqual((args.local_ai_page, args.local_ai_accept, args.local_ai_detail_tile, args.local_ai_batch,
                          args.local_ai_auth), ("prefer", True, 1024, 4, "subscription"))
        self.assertEqual(resolved["ai_page"], "prefer")
        args = self.args("--local-ai-page", "off")
        with patch.dict(os.environ, {"YEONE_OCR_CLAUDE_AUTH": ""}), \
                patch.object(subscription, "status", return_value={"usable": True, "plan": "max", "reason": None}):
            local_first._resolve_ai_defaults(args, cli)
        self.assertEqual(args.local_ai_page, "off")                                   # explicit choice wins
        args = self.args()
        with patch.dict(os.environ, {"YEONE_OCR_CLAUDE_AUTH": ""}), \
                patch.object(subscription, "status", return_value={"usable": False, "reason": "로그인 필요"}):
            local_first._resolve_ai_defaults(args, cli)
        self.assertEqual((args.local_ai_page, args.local_ai_accept, args.local_ai_batch), ("off", False, 1))
        self.assertIsNone(args.local_ai_auth)

    def test_missing_key_saves_local_and_pending_review(self):
        self.confidence = 20
        with patch.dict(os.environ, {"OPENAI_API_KEY": ""}):
            self.assertEqual(command(self.args()), 4)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.record()["local_first"]["review"]["status"], "pending")
        self.make_textlayer.assert_called_once()

    def test_disabled_review_has_no_external_call(self):
        self.confidence = 20
        self.assertEqual(command(self.args("--no-local-ai-review")), 0)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.record()["local_first"]["review"]["reason"], "AI 검증 꺼짐")

    def test_api_failure_keeps_raw_and_builds_local_output(self):
        self.confidence = 20
        self.api_failure = True
        self.assertEqual(command(self.args()), 4)
        rec = self.record()
        self.assertEqual(rec["local_first"]["review"]["status"], "failed")
        self.assertEqual(rec["pass1"]["regions"][0]["paragraphs"][0]["columns"][0]["text"], "天地玄黃")
        self.make_textlayer.assert_called_once()

    def test_disabled_review_preserves_unlocalized_notice_without_api_error(self):
        self.omit_geometry = True
        self.assertEqual(command(self.args("--no-local-ai-review")), 0)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.record()["local_first"]["review"]["status"], "needs-local-review")

    def test_legacy_or_manually_edited_reading_is_preserved(self):
        self.assertEqual(command(self.args()), 0)
        old = self.record()
        old["merged"]["page_note"] = "human correction"
        work = Work(str(self.source), str(self.work), initialize=False)
        work.save_record(1, old)
        self.local_calls.clear()
        self.assertEqual(command(self.args()), 0)
        self.assertEqual(self.local_calls, [])
        self.assertEqual(self.record(), old)

    def test_document_build_disables_extra_context_model(self):
        args = self.args()
        args.pdf_only = False
        with patch.object(cli, "_check_document", return_value=None):
            self.assertEqual(command(args), 0)
        self.assertTrue(self.cmd_build.call_args.args[0].no_context_model)

    def test_total_call_cap_applies_across_pages(self):
        import pymupdf
        self.source = self.root / "two.pdf"
        with pymupdf.open() as document:
            document.new_page(width=96, height=48)
            document.new_page(width=96, height=48)
            document.save(self.source)
        self.confidence = 20
        self.assertEqual(command(self.args("--local-ai-max-calls", "1")), 4)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.record(2)["local_first"]["review"]["status"], "pending")

    def test_multiframe_review_resume_and_changed_cap_never_repeat_paid_calls(self):
        import pymupdf
        self.source = self.root / "two-resume.pdf"
        with pymupdf.open() as document:
            document.new_page(width=96, height=48)
            document.new_page(width=96, height=48)
            document.save(self.source)
        self.confidence = 20
        self.assertEqual(command(self.args("--local-ai-max-calls", "3")), 0)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(command(self.args("--local-ai-max-calls", "3")), 0)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(command(self.args("--local-ai-max-calls", "7")), 0)
        self.assertEqual(len(self.calls), 2)
        for number in (1, 2):
            rec = self.record(number)
            project = ComparisonProject.load(rec["local_first"]["project_path"])
            self.assertEqual(len(project.page(number)["ai_reviews"]), 1)

    def test_relative_source_and_work_paths_resume_without_duplicate_review(self):
        original_cwd = Path.cwd()
        original_source, original_work = self.source, self.work
        self.confidence = 20
        try:
            os.chdir(self.root)
            self.source, self.work = Path("source.png"), Path("work")
            self.assertEqual(command(self.args()), 0)
            self.assertEqual(len(self.calls), 1)
            rec = self.record()
            project = ComparisonProject.load(rec["local_first"]["project_path"])
            self.assertEqual(project.source_path, original_source.resolve())
            self.assertEqual(command(self.args()), 0)
            self.assertEqual(len(self.calls), 1)
            self.assertEqual(len(ComparisonProject.load(project.project_path).page(1)["ai_reviews"]), 1)
        finally:
            os.chdir(original_cwd)
            self.source, self.work = original_source, original_work

    def test_failed_forced_rerecognition_cannot_certify_stale_baseline(self):
        self.assertEqual(command(self.args()), 0)
        before = (self.work / "readings" / "p001.json").read_bytes()
        outcome = {"cancelled": False, "completed": [], "skipped": [],
                   "errors": {"ppocrv6": "synthetic engine failure"}, "selection": {}}
        with patch("yeone_ocr.automatic_ocr.run_automatic_page", return_value=outcome):
            self.assertNotEqual(command(self.args("--force")), 0)
        self.assertEqual((self.work / "readings" / "p001.json").read_bytes(), before)
        self.assertEqual(self.calls, [])

    def test_profile_change_failure_preserves_previous_reading(self):
        self.assertEqual(command(self.args()), 0)
        before = (self.work / "readings" / "p001.json").read_bytes()
        outcome = {"cancelled": False, "completed": [], "skipped": [],
                   "errors": {"ppocrv6": "synthetic unsupported model"}, "selection": {}}
        with patch("yeone_ocr.automatic_ocr.run_automatic_page", return_value=outcome):
            self.assertNotEqual(command(self.args("--force", "--local-profile", "simplified")), 0)
        self.assertEqual((self.work / "readings" / "p001.json").read_bytes(), before)
        self.assertEqual(self.calls, [])

    def test_cancel_after_paid_response_checkpoints_and_resumes_without_resend(self):
        self.confidence = 20
        self.transport_callback = cli.CANCEL.set
        self.assertEqual(command(self.args()), 130)
        self.assertEqual(len(self.calls), 1)
        self.make_textlayer.assert_not_called()
        rec = self.record()
        self.assertEqual(rec["local_first"]["review"]["status"], "cancelled")
        project = ComparisonProject.load(rec["local_first"]["project_path"])
        self.assertEqual(project.page(1)["ai_reviews"][0]["status"], "completed")
        self.assertEqual(project.page(1)["runs"][0]["raw"]["text"], "天地玄黃")
        cli.CANCEL.clear()
        self.transport_callback = None
        self.assertEqual(command(self.args()), 0)
        self.assertEqual(len(self.calls), 1)
        self.make_textlayer.assert_called_once()

    def test_true_blank_local_success_needs_no_api(self):
        self.recognized_text = ""
        self.assertEqual(command(self.args()), 0)
        self.assertEqual(self.calls, [])
        rec = self.record()
        self.assertEqual(rec["pass1"]["regions"], [])
        self.assertEqual(rec["merged"]["regions"], [])
        self.assertEqual(rec["local_first"]["review"]["status"], "no-api-needed")

    def test_source_change_during_transport_blocks_document_output(self):
        self.confidence = 20
        def replace_source():
            with Image.new("RGB", (400, 200), "black") as changed:
                changed.save(self.source)
        self.transport_callback = replace_source
        self.assertNotEqual(command(self.args()), 0)
        self.assertEqual(len(self.calls), 1)
        self.make_textlayer.assert_not_called()
        self.cmd_build.assert_not_called()
        self.assertEqual(self.record()["pass1"]["regions"][0]["paragraphs"][0]["columns"][0]["text"], "天地玄黃")

    def test_review_and_report_display_pending_without_injecting_html(self):
        from yeone_ocr.review_html import make_review
        self.confidence = 20
        command(self.args("--no-local-ai-review"))
        work = Work(str(self.source), str(self.work), initialize=False)
        rec = self.record()
        rec["local_first"]["ai_proposals"] = [{"before": "<script>", "after": "&", "reason": "<bad>", "decision": "pending"}]
        text = Path(make_review(work, [rec])).read_text(encoding="utf-8")
        self.assertIn("로컬 우선", text)
        self.assertNotIn("<script>", text)
        self.assertIn("&lt;script&gt;", text)


if __name__ == "__main__":
    unittest.main()
