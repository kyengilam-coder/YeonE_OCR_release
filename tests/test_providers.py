"""0.7.1 제공사 전환: SDK 가짜 응답만 사용(키·네트워크·과금 없음)."""
import contextlib
import io
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest import mock

from PIL import Image

from yeone_ocr import cli, readers, reader_gemini as rg
from yeone_ocr.schema import PageReading, Region, Paragraph, Column, RegionReread, GlyphAsk
from yeone_ocr.state import Work


def page():
    return PageReading(pdf_page=1, regions=[Region(id=rid, kind="본문", bbox=[0, 0, 1000, 1000],
        paragraphs=[Paragraph(columns=[Column(col_from_right=1, text=text)])]) for rid, text in (("b1", "天地玄黃"), ("b2", "宇宙洪荒"))])


class SDKReaders(unittest.TestCase):
    def setUp(self):
        rg.set_call_cap(0)
        rg.set_cancel_event(None)
        self.image = Image.new("RGB", (24, 24), "white")

    def tearDown(self):
        rg.set_call_cap(0)
        rg.set_cancel_event(None)

    def fake(self, provider, payload):
        if provider == "openai":
            response = NS(status="completed", output_text=json.dumps(payload, ensure_ascii=False),
                          usage=NS(input_tokens=100, output_tokens=40, input_tokens_details=NS(cached_tokens=20), output_tokens_details=NS(reasoning_tokens=10)))
            create = mock.Mock(return_value=response)
            client = NS(responses=NS(create=create))
        else:
            response = NS(stop_reason="end_turn", content=[NS(type="thinking", thinking="hidden"), NS(type="text", text=json.dumps(payload, ensure_ascii=False))],
                          usage=NS(input_tokens=80, output_tokens=40, cache_read_input_tokens=20))
            create = mock.Mock(return_value=response)
            client = NS(messages=NS(create=create))
        with mock.patch.object(readers, "_client", return_value=client):
            reader = readers.create_reader(provider, api_key="test-key", system="OCR guide", thinking_budget=123, media_resolution="ultra_high")
        return reader, create, response

    def test_page_image_request_and_usage(self):
        for provider in ("openai", "anthropic"):
            with self.subTest(provider=provider):
                reader, create, _ = self.fake(provider, page().model_dump(by_alias=True))
                reader.usage_begin()
                got = reader.read_page(self.image, "book.pdf", 7, 10)
                self.assertEqual(got.pdf_page, 7)
                self.assertEqual(len(got.regions), 2)
                sent = create.call_args.kwargs
                self.assertNotIn("thinking_budget", sent)
                self.assertNotIn("media_resolution", sent)
                images = (sent["input"] if provider == "openai" else sent["messages"])[0]["content"]
                if provider == "openai":
                    self.assertTrue(images[0]["image_url"].startswith("data:image/png;base64,"))
                    self.assertFalse(sent["store"])
                    self.assertEqual(sent["text"]["format"]["type"], "json_object")
                else:
                    self.assertEqual(images[0]["source"]["media_type"], "image/png")
                usage = reader.usage_end()
                self.assertEqual(usage["calls"], 1)
                self.assertEqual(usage["prompt_tokens"], 100)
                self.assertEqual(usage["output_tokens"] + usage["thinking_tokens"], 40)
                self.assertEqual(usage["cached_tokens"], 20)

    def test_region_batch_and_glyph_batch(self):
        for provider in ("openai", "anthropic"):
            reader, create, response = self.fake(provider, {"regions": [{"r": "b1", "cols": [{"n": 1, "t": "天地"}]}]})
            self.assertEqual(reader.reread_regions([("b1", "본문", self.image)], 1)["b1"].columns[0].text, "天地")
            payload = json.dumps({"items": [{"s": "s1", "w": "云", "e": "자형", "c": "云"}]})
            if provider == "openai": response.output_text = payload
            else: response.content = [NS(type="text", text=payload)]
            result = reader.adjudicate_many([("s1", self.image, GlyphAsk(candidates=("云", "雲")))], True, 1)
            self.assertEqual(result["s1"].choice, "云")
            self.assertEqual(create.call_count, 2)

    def test_text_context_only_accepts_candidates_and_preserves_ocr_system(self):
        for provider in ("openai", "anthropic"):
            reader, create, _ = self.fake(provider, {"items": [{"id": "a", "choice": "云"}, {"id": "b", "choice": "wrong"},
                                                               {"id": "c", "choice": "云"}, {"id": "c", "choice": "雲"}]})
            items = [{"id": key, "context": "詩雲：", "source": "雲", "candidates": ["云", "雲"]} for key in "abc"]
            self.assertEqual(reader.resolve_text_choices(items), {"a": "云", "b": ""})          # 후보 밖 답 = 못 고름, 중복 답 = 답 없음
            self.assertEqual(reader._system, "OCR guide")
            sent = create.call_args.kwargs
            content = (sent.get("input") or sent["messages"])[0]["content"]
            self.assertEqual(len(content), 1)
            self.assertIn("text", content[0])

    def test_openai_json_mode_requests_name_json_in_the_input(self):
        """OpenAI 는 json_object 형식을 쓸 때 입력 글에 'json' 낱말이 없으면 400 으로 거절한다(instructions 는 세지 않음)."""
        reader, create, response = self.fake("openai", {"items": [{"id": "a", "choice": "云"}]})
        real = create.side_effect

        def server(**kw):
            texts = [c.get("text", "") for m in kw["input"] for c in m["content"] if isinstance(c, dict)]
            if not any("json" in t.lower() for t in texts):
                e = RuntimeError("Response input messages must contain the word 'json'"); e.status_code = 400
                raise e
            return response if real is None else real(**kw)
        create.side_effect = server
        items = [{"id": "a", "context": "詩雲：", "source": "雲", "candidates": ["云", "雲"]}]
        self.assertEqual(reader.resolve_text_choices(items), {"a": "云"})       # 문맥 교정(0.7.4 까지 400)
        response.output_text = json.dumps(page().model_dump(by_alias=True), ensure_ascii=False)
        self.assertEqual(len(reader.read_page(self.image, "book.pdf", 1, 1).regions), 2)   # 판독도 같은 규칙을 지킨다

    def test_gemini_text_choices_uses_same_cap_and_dedicated_prompt(self):
        reader = rg.GeminiReader.__new__(rg.GeminiReader)
        reader._system = "OCR system"; reader.cache_name = "cached-OCR"
        observed = []
        def call(shadow, img, prompt, schema, extra_parts=None):
            rg.CALL_CAP.take()
            observed.append((shadow._system, shadow.cache_name, img, extra_parts))
            return readers.TextChoices(items=[readers.TextChoice(id="q", choice="云")])
        with mock.patch.object(rg.GeminiReader, "_call", call):
            result = reader.resolve_text_choices([{"id": "q", "source": "雲", "context": "書雲", "candidates": ["云", "雲"]}])
        self.assertEqual(result, {"q": "云"})
        self.assertEqual(reader._system, "OCR system")
        self.assertEqual(reader.cache_name, "cached-OCR")
        self.assertIsNone(observed[0][1]); self.assertIsNone(observed[0][2])
        self.assertEqual(rg.CALL_CAP.used, 1)

    def test_shared_call_limit_and_sdk_retry_disabled(self):
        fake_client = mock.Mock()
        with mock.patch.dict("sys.modules", {"openai": NS(OpenAI=fake_client), "anthropic": NS(Anthropic=fake_client)}):
            readers._client("openai", "test")
            self.assertEqual(fake_client.call_args.kwargs["max_retries"], 0)
            readers._client("anthropic", "test")
            self.assertEqual(fake_client.call_args.kwargs["max_retries"], 0)
        a, create_a, _ = self.fake("openai", page().model_dump())
        b, create_b, _ = self.fake("anthropic", page().model_dump())
        rg.set_call_cap(1)
        a.read_page(self.image, "x", 1, 1)
        with self.assertRaises(rg.ReaderError) as caught:
            b.read_page(self.image, "x", 1, 1)
        self.assertTrue(caught.exception.fatal)
        self.assertEqual(create_a.call_count, 1); self.assertEqual(create_b.call_count, 0)

    def test_invalid_or_truncated_json_never_saved_as_success(self):
        for provider in ("openai", "anthropic"):
            reader, create, response = self.fake(provider, page().model_dump())
            if provider == "openai": response.status = "incomplete"
            else: response.stop_reason = "max_tokens"
            with mock.patch.object(readers.time, "sleep"), self.assertRaises(rg.ReaderError) as caught:
                reader.read_page(self.image, "x", 1, 1)
            self.assertFalse(caught.exception.fatal)
            self.assertEqual(create.call_count, 3)
            self.assertEqual(reader.usage["calls"], 3)

    def test_auth_error_is_fatal_and_redacts_raw_secret(self):
        reader, create, _ = self.fake("openai", {})
        error = RuntimeError("secret-key should not leak"); error.status_code = 401
        create.side_effect = error
        with self.assertRaises(rg.ReaderError) as caught:
            reader.read_page(self.image, "x", 1, 1)
        self.assertTrue(caught.exception.fatal)
        self.assertNotIn("secret-key", str(caught.exception))
        self.assertEqual(create.call_count, 1)

    def test_simplified_source_cannot_be_confirmed_as_traditional(self):
        reader, _, _ = self.fake("openai", {"items": [{"id": "a", "choice": "发"}]})
        self.assertEqual(reader.resolve_text_choices([{"id": "a", "source": "发", "context": "头发", "candidates": ["发", "發", "髮"],
                                                       "source_is_valid_traditional": False}]), {"a": ""})

    def test_owned_document_closed_on_error_borrowed_document_kept_open(self):
        document = mock.Mock()
        @cli._document_command
        def operation(args, work=None, doc=None):
            self.assertIs(doc, document)
            raise RuntimeError("stop")
        with mock.patch.object(cli, "open_pdf", return_value=document), self.assertRaises(RuntimeError):
            operation(NS(pdf="source.pdf"))
        document.close.assert_called_once()
        document.close.reset_mock()
        with self.assertRaises(RuntimeError):
            operation(NS(pdf="source.pdf"), None, document)
        document.close.assert_not_called()

    def test_cancelled_provider_request_spends_no_call(self):
        for provider in ("openai", "anthropic"):
            reader, create, _ = self.fake(provider, page().model_dump())
            event = threading.Event(); event.set()
            rg.set_cancel_event(event)
            with self.assertRaises(rg.ReaderError) as caught:
                reader.read_page(self.image, "x", 1, 1)
            self.assertEqual(caught.exception.kind, "cancelled")
            create.assert_not_called()
        self.assertEqual(rg.CALL_CAP.used, 0)

    def test_server_outage_keeps_infrastructure_kind_for_batch_handling(self):
        for provider in ("openai", "anthropic"):
            reader, create, _ = self.fake(provider, {})
            failure = RuntimeError("temporary server outage"); failure.status_code = 503
            create.side_effect = failure
            with mock.patch.object(rg, "_wait_retry"), self.assertRaises(rg.ReaderError) as caught:
                reader.read_page(self.image, "x", 1, 1)
            self.assertEqual(caught.exception.kind, "infrastructure")
            self.assertFalse(caught.exception.fatal)
            self.assertEqual(create.call_count, 3)


class ProviderResume(unittest.TestCase):
    class FakeReader:
        def __init__(self, name):
            self.name = name; self.model = {"gemini": "gemini-2.5-pro", "openai": "gpt-4.1", "anthropic": "claude-sonnet-4-6"}[name]
            self.calls = 0
        def read_page(self, *args):
            self.calls += 1
            return page()

    def setUp(self):
        cli.CANCEL.clear()
        self.temp = tempfile.TemporaryDirectory()
        self.work = Work(str(Path(self.temp.name) / "source.png"), str(Path(self.temp.name) / "work"))
        Image.new("RGB", (80, 80), "white").save(self.work.pdf_path)
        self.work.ensure_source()
        self.args = cli.build_parser().parse_args(["read", str(self.work.pdf_path), "--work", str(self.work.dir), "--second-mode", "each", "--workers", "1", "--mode", "vertical"])
        cli.apply_mode(self.args)
        self.stack = contextlib.ExitStack()
        self.stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        self.stack.enter_context(mock.patch.object(cli, "_load_page_image", return_value=(Image.new("RGB", (80, 80), "white"), "test")))
        self.stack.enter_context(mock.patch.object(cli, "_load_layer", return_value={"plain": ""}))
        self.stack.enter_context(mock.patch.object(cli, "refit_regions", return_value={"fixed": 0}))
        self.stack.enter_context(mock.patch.object(cli, "cover_check", return_value=[]))
        self.stack.enter_context(mock.patch.object(cli, "detect_figures", return_value=[]))

    def tearDown(self):
        self.stack.close()
        self.temp.cleanup()

    @staticmethod
    def rereads(*ids):
        return {r.id: RegionReread(columns=r.paragraphs[0].columns) for r in page().regions if r.id in ids}

    def test_old_gemini_partial_resumes_on_openai_with_original_provenance(self):
        a = self.FakeReader("gemini")
        with mock.patch.object(cli, "_second_pass", side_effect=rg.ReaderError("quota")), self.assertRaises(rg.ReaderError):
            cli.read_one(self.work, [None], a, a, 1, self.args)
        partial = cli.partial_path(self.work, 1)
        d = json.loads(partial.read_text(encoding="utf-8")); d["version"] = "0.6.8"; d.pop("reader")
        partial.write_text(json.dumps(d), encoding="utf-8")
        b = self.FakeReader("openai"); self.args.reader = "openai"
        with mock.patch.object(cli, "_second_pass", return_value=(self.rereads("b1", "b2"), {})):
            rec = cli.read_one(self.work, [None], b, b, 1, self.args)
        self.assertEqual(a.calls, 1); self.assertEqual(b.calls, 0)
        self.assertEqual(rec["reader"], "gemini")
        self.assertEqual(rec["model"], a.model)
        self.assertEqual(rec["provenance"]["pass2"]["b1"]["provider"], "openai")
        self.assertEqual(rec["second_model"], b.model)
        self.assertFalse(partial.exists())

    def test_successful_regions_and_manual_edits_survive_claude_switch(self):
        a = self.FakeReader("gemini")
        with mock.patch.object(cli, "_second_pass", return_value=(self.rereads("b1"), {"b2": {"pass2_failed": "server"}})):
            rec = cli.read_one(self.work, [None], a, a, 1, self.args)
        rec["merged"]["regions"][0]["paragraphs"][0]["columns"][0]["text"] = "手校保存"
        self.work.save_record(1, rec)
        b = self.FakeReader("anthropic"); self.args.reader = "anthropic"
        def remaining(reader, image, p1, n, args):
            self.assertEqual([r.id for r in p1.regions], ["b2"])
            return self.rereads("b2"), {}
        with mock.patch.object(cli, "_second_pass", side_effect=remaining):
            final = cli.read_one(self.work, [None], b, b, 1, self.args)
        self.assertEqual(b.calls, 0)
        self.assertEqual(final["merged"]["regions"][0]["paragraphs"][0]["columns"][0]["text"], "手校保存")
        self.assertEqual(final["provenance"]["pass2"]["b1"]["provider"], "gemini")
        self.assertEqual(final["provenance"]["pass2"]["b2"]["provider"], "anthropic")
        self.assertTrue((self.work.readings_dir / "prev" / "p001.json").exists())

    def test_finished_page_is_skipped_even_when_provider_changes(self):
        a = self.FakeReader("gemini")
        with mock.patch.object(cli, "_second_pass", return_value=(self.rereads("b1", "b2"), {})):
            cli.read_one(self.work, [None], a, a, 1, self.args)
        before = self.work.record_path(1).read_bytes()
        b = self.FakeReader("openai"); self.args.reader = "openai"; self.args.second_model = ""
        with mock.patch.object(cli, "make_reader", return_value=b), mock.patch.object(cli, "create_reader", return_value=b):
            self.assertEqual(cli.cmd_read(self.args, self.work, [None]), 0)
        self.assertEqual(b.calls, 0)
        self.assertEqual(before, self.work.record_path(1).read_bytes())

    def test_changed_reading_mode_does_not_reuse_pass1(self):
        a = self.FakeReader("gemini"); b = self.FakeReader("openai")
        d = {"reader": a.name, "model": a.model, "mode": "horizontal_zh"}
        self.assertFalse(cli._can_resume_pass1(d, b, self.args))


class WindowsAtomicSave(unittest.TestCase):
    def test_short_sharing_conflict_retries_rename_without_rewriting(self):
        from yeone_ocr import state
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "state.json"; target.write_text("old", encoding="utf-8")
            original_replace = os.replace
            conflict = PermissionError("sharing conflict"); conflict.winerror = 5
            calls = []
            def replace(source, destination):
                calls.append((source, destination))
                if len(calls) == 1:
                    self.assertEqual(target.read_text(), "old")
                    raise conflict
                return original_replace(source, destination)
            writer = mock.Mock(side_effect=lambda path: path.write_text("new", encoding="utf-8"))
            with mock.patch.object(state.os, "replace", side_effect=replace), mock.patch.object(state.time, "sleep"):
                state.write_atomic_with(target, writer)
            self.assertEqual(target.read_text(), "new")
            writer.assert_called_once()
            self.assertEqual(len(calls), 2)

    def test_permanent_denial_keeps_previous_file_and_cleans_temp(self):
        from yeone_ocr import state
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "state.json"; target.write_text("old", encoding="utf-8")
            conflict = PermissionError("denied"); conflict.winerror = 5
            with mock.patch.object(state.os, "replace", side_effect=conflict) as replace, mock.patch.object(state.time, "sleep"), self.assertRaises(PermissionError):
                state.write_atomic(target, "new")
            self.assertEqual(replace.call_count, 5)
            self.assertEqual(target.read_text(), "old")
            self.assertEqual(list(Path(td).iterdir()), [target])


if __name__ == "__main__":
    unittest.main()
