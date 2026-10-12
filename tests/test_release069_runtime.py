"""0.6.9: offline cancellation, retry-budget and automatic diagnostic call-cap regressions."""
import contextlib
import io
import json
import sys
import tempfile
import threading
import types
import unittest
from argparse import Namespace
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PIL import Image
from pydantic import BaseModel
from yeone_ocr import reader_gemini as rg
from yeone_ocr.schema import PageReading, Region, Paragraph, Column, RegionReread


KEY = "AIza" + "k" * 35


class Result(BaseModel):
    value: int


class ApiError(Exception):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def response(text='{"value": 1}'):
    return types.SimpleNamespace(text=text, usage_metadata=None)


@contextlib.contextmanager
def fake_reader(generate):
    calls = []

    def send(**kwargs):
        calls.append(kwargs)
        return generate(len(calls), kwargs)

    client = types.SimpleNamespace(models=types.SimpleNamespace(generate_content=send))
    with mock.patch.object(rg, "make_client", return_value=client):
        yield rg.GeminiReader(model="gemini-3-pro", api_key=KEY, use_cache=False), calls


def call(reader):
    return reader._call(None, "prompt", Result, extra_parts=["prompt"])


class RuntimeRecovery(unittest.TestCase):
    def setUp(self):
        rg.set_call_cap(0)
        rg.set_cancel_event(None)

    def tearDown(self):
        rg.set_call_cap(0)
        rg.set_cancel_event(None)

    def test_cancel_before_request_does_not_spend_a_call(self):
        cancel = threading.Event()
        cancel.set()
        rg.set_cancel_event(cancel)
        with fake_reader(lambda *_: response()) as (reader, calls):
            with self.assertRaises(rg.ReaderError) as caught:
                call(reader)
        self.assertEqual(caught.exception.kind, "cancelled")
        self.assertTrue(caught.exception.fatal)
        self.assertEqual((len(calls), rg.CALL_CAP.used), (0, 0))

    def test_stop_during_failed_request_prevents_retry(self):
        cancel = threading.Event()
        rg.set_cancel_event(cancel)

        def fail(*_):
            cancel.set()
            raise ApiError(503, "server unavailable")

        with fake_reader(fail) as (reader, calls), mock.patch.object(rg, "_wait_retry") as wait:
            with self.assertRaises(rg.ReaderError) as caught:
                call(reader)
        self.assertEqual(caught.exception.kind, "cancelled")
        self.assertEqual(len(calls), 1)
        wait.assert_not_called()

    def test_stop_interrupts_retry_wait(self):
        class CancelDuringWait(threading.Event):
            def wait(self, timeout=None):
                self.timeout = timeout
                self.set()
                return True

        cancel = CancelDuringWait()
        rg.set_cancel_event(cancel)

        def fail(*_):
            raise ApiError(429, "RESOURCE_EXHAUSTED retry in 90s")

        with fake_reader(fail) as (reader, calls), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(rg.ReaderError) as caught:
                call(reader)
        self.assertEqual(caught.exception.kind, "cancelled")
        self.assertEqual(cancel.timeout, 90)
        self.assertEqual(len(calls), 1)

    def test_completed_response_survives_stop_but_next_request_is_blocked(self):
        cancel = threading.Event()
        rg.set_cancel_event(cancel)

        def finish(*_):
            cancel.set()
            return response()

        with fake_reader(finish) as (reader, calls):
            self.assertEqual(call(reader).value, 1)
            with self.assertRaises(rg.ReaderError) as caught:
                call(reader)
        self.assertEqual(caught.exception.kind, "cancelled")
        self.assertEqual(len(calls), 1)

    def test_infrastructure_failure_has_bounded_attempts_without_final_wait(self):
        def fail(*_):
            raise ApiError(503, "server unavailable")

        with fake_reader(fail) as (reader, calls), mock.patch.object(rg.time, "sleep") as sleep:
            with self.assertRaises(rg.ReaderError) as caught:
                call(reader)
        self.assertFalse(caught.exception.fatal)
        self.assertEqual(caught.exception.kind, "infrastructure")
        self.assertEqual(len(calls), 5)
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [1, 2, 4, 8])

    def test_format_failure_preserves_content_fallback_classification(self):
        with fake_reader(lambda *_: response("not JSON")) as (reader, calls), mock.patch.object(rg.time, "sleep"):
            with self.assertRaises(rg.ReaderError) as caught:
                call(reader)
        self.assertEqual(caught.exception.kind, "format")
        self.assertFalse(caught.exception.fatal)
        self.assertEqual(len(calls), 3)

    def test_page_reader_rejects_duplicate_ids_before_returning_a_page(self):
        region = Region(id="duplicate", kind="본문", bbox=[100, 100, 900, 900])
        legacy = PageReading(pdf_page=1, regions=[region, region.model_copy()])
        with fake_reader(lambda *_: response(legacy.model_dump_json())) as (reader, calls), mock.patch.object(rg.time, "sleep"):
            with self.assertRaises(rg.ReaderError) as caught:
                reader.read_page(Image.new("RGB", (10, 10), "white"), "sample.pdf", 1, 1)
        self.assertEqual(caught.exception.kind, "format")
        self.assertEqual(len(calls), 3)

    def test_failed_eight_crop_batch_does_not_multiply_infrastructure_retries(self):
        from yeone_ocr import cli

        def fail(*_):
            raise ApiError(503, "server unavailable")

        page = PageReading(pdf_page=1, regions=[
            Region(id=f"r{i}", kind="본문", bbox=[100, 100, 900, 900],
                   paragraphs=[Paragraph(columns=[Column(col_from_right=1, text="天地玄黃")])])
            for i in range(8)
        ])
        with fake_reader(fail) as (reader, calls), mock.patch.object(rg.time, "sleep"), mock.patch.object(cli, "log"):
            with self.assertRaises(rg.ReaderError) as caught:
                cli._second_pass(reader, Image.new("RGB", (100, 100), "white"), page, 1,
                                 Namespace(chunk_lines=0, second_mode="batch", batch_crops=8))
        self.assertEqual(caught.exception.kind, "infrastructure")
        self.assertEqual(len(calls), 5)

    def test_automatic_model_probe_cannot_exceed_command_cap(self):
        def fail_first(n, _):
            if n == 1:
                raise ApiError(404, "NOT_FOUND missing model")
            return response()

        rg.set_call_cap(1)
        with fake_reader(fail_first) as (reader, calls), mock.patch.object(rg, "list_models", return_value=["gemini-3-pro"]):
            with self.assertRaises(rg.ReaderError) as caught:
                call(reader)
        self.assertTrue(caught.exception.fatal)
        self.assertIn("호출 상한", str(caught.exception))
        self.assertEqual((len(calls), rg.CALL_CAP.used), (1, 1))

    def test_automatic_probe_is_counted_when_budget_allows_it(self):
        def fail_first(n, _):
            if n == 1:
                raise ApiError(404, "NOT_FOUND missing model")
            return response()

        rg.set_call_cap(2)
        with fake_reader(fail_first) as (reader, calls), mock.patch.object(rg, "list_models", return_value=["gemini-3-pro"]):
            with self.assertRaises(rg.ReaderError) as caught:
                call(reader)
        self.assertTrue(caught.exception.fatal)
        self.assertIn("호출 확인된 모델", str(caught.exception))
        self.assertEqual((len(calls), rg.CALL_CAP.used), (2, 2))

    def test_standalone_key_check_ignores_previous_command_cap_and_stop(self):
        rg.set_call_cap(1)
        rg.CALL_CAP.take()
        cancel = threading.Event()
        cancel.set()
        rg.set_cancel_event(cancel)
        with fake_reader(lambda *_: response()) as (_, calls), mock.patch.object(rg, "list_models", return_value=["gemini-3-pro"]):
            result = rg.check_key(KEY, "gemini-3-pro")
        self.assertEqual(result["working_model"], "gemini-3-pro")
        self.assertEqual((len(calls), rg.CALL_CAP.used), (1, 1))

    def test_page_stage_timings_are_saved_and_reused_pass1_is_identified(self):
        from yeone_ocr import cli
        from yeone_ocr.state import Work

        clock = [0.0]

        def advance(seconds, result):
            clock[0] += seconds
            return result

        page = PageReading(pdf_page=1, regions=[Region(id="r1", kind="본문", bbox=[100, 100, 900, 900],
                           paragraphs=[Paragraph(columns=[Column(col_from_right=1, text="天地玄黃")])])])
        reader = types.SimpleNamespace(name="fake", model="fake", read_page=mock.Mock(side_effect=lambda *a: advance(3.0, page)))
        args = Namespace(pdf="missing-fixture.pdf", dpi=72, upscale=1, no_second_pass=False, print_offset=None,
                         mode="vertical", force=True, chunk_lines=0, second_mode="each")
        rr = {"r1": RegionReread(columns=[Column(col_from_right=1, text="天地玄黃")])}
        with tempfile.TemporaryDirectory() as folder, contextlib.ExitStack() as stack:
            work = Work(args.pdf, folder)
            stack.enter_context(mock.patch.object(cli.time, "perf_counter", side_effect=lambda: clock[0]))
            stack.enter_context(mock.patch.object(cli, "_load_page_image", side_effect=lambda *a: advance(5.0, (Image.new("RGB", (100, 100), "white"), "fake"))))
            stack.enter_context(mock.patch.object(cli, "_load_layer", side_effect=lambda *a: advance(2.0, {"plain": ""})))
            stack.enter_context(mock.patch.object(cli, "refit_regions", return_value={"fixed": 0}))
            stack.enter_context(mock.patch.object(cli, "cover_check", return_value=[]))
            stack.enter_context(mock.patch.object(cli, "detect_figures", return_value=[]))
            second = stack.enter_context(mock.patch.object(cli, "_second_pass", side_effect=lambda *a: advance(7.0, (rr, {}))))
            stack.enter_context(mock.patch.object(cli, "log"))
            record = cli.read_one(work, [None], reader, reader, 1, args)
            self.assertEqual(record["timings"], {"image_load": 5.0, "layer_load": 2.0, "pass1": 3.0,
                                               "layout": 0.0, "pass2": 7.0, "postprocess": 0.0, "total": 17.0})
            self.assertFalse(record["pass1_reused"])
            self.assertEqual(record["seconds"], 17.0)
            self.assertEqual(work.load_record(1)["timings"], record["timings"])
            part = {"pass1": page.model_dump(), "saved_at": "previous attempt"}
            with mock.patch.object(cli, "_reusable_pass1", return_value=part):
                reused = cli.read_one(work, [None], reader, reader, 1, args)
            self.assertTrue(reused["pass1_reused"])
            self.assertEqual(reused["pass1_reused_from"], "previous attempt")
            self.assertEqual((reused["timings"]["pass1"], reused["timings"]["total"]), (0.0, 14.0))
            self.assertTrue(all(value >= 0 for value in reused["timings"].values()))
            self.assertEqual(reader.read_page.call_count, 1)
            self.assertEqual(work.load_record(1)["timings"], reused["timings"])

            def fail(*args):
                clock[0] += 4.0
                raise rg.ReaderError("server unavailable", fatal=False, kind="infrastructure")

            second.side_effect = fail
            with self.assertRaises(rg.ReaderError):
                cli.read_one(work, [None], reader, reader, 1, args)
            failed = json.loads(cli.partial_path(work, 1).read_text(encoding="utf-8"))
            self.assertEqual((failed["timings"]["pass2"], failed["timings"]["total"]), (4.0, 14.0))
            self.assertTrue(all(value >= 0 for value in failed["timings"].values()))


if __name__ == "__main__":
    unittest.main()
