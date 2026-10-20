"""Offline regression scheduling must retain isolation and truthful receipts."""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tools import check_regression


class RegressionReceipts(unittest.TestCase):
    def test_transient_share_lock_preserves_atomic_complete_receipt(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "receipt.json"
            target.write_text('{"old": true}', encoding="utf-8")
            replace = os.replace
            sharing = PermissionError("synthetic sharing violation")
            sharing.winerror = 32
            calls = []

            def sometimes_locked(source, destination):
                calls.append((source, destination))
                if len(calls) == 1:
                    self.assertEqual(json.loads(target.read_text()), {"old": True})
                    raise sharing
                return replace(source, destination)

            with patch.object(check_regression.os, "name", "nt"), \
                    patch.object(check_regression.os, "replace", side_effect=sometimes_locked), \
                    patch.object(check_regression.time, "sleep"):
                check_regression._write_json(target, {"status": "complete", "passed": 5})
            self.assertEqual(json.loads(target.read_text()), {"status": "complete", "passed": 5})
            self.assertEqual(len(calls), 2)
            self.assertFalse(target.with_suffix(".json.tmp").exists())

    def test_nonsharing_permission_failure_is_not_hidden_or_retried(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "receipt.json"
            target.write_text('{"old": true}', encoding="utf-8")
            failure = PermissionError("synthetic permanent failure")
            failure.winerror = 1314
            with patch.object(check_regression.os, "name", "nt"), \
                    patch.object(check_regression.os, "replace", side_effect=failure) as replace, \
                    patch.object(check_regression.time, "sleep") as sleep:
                with self.assertRaises(PermissionError):
                    check_regression._write_json(target, {"status": "complete"})
            replace.assert_called_once()
            sleep.assert_not_called()
            self.assertEqual(json.loads(target.read_text()), {"old": True})


class RegressionScheduling(unittest.TestCase):
    SAFE_A = "tests/test_normalization.py"
    SAFE_B = "tests/test_record_preservation.py"
    SAFE_C = "tests/test_text_preservation.py"
    GUI = "tests/test_bookmark_section_editor.py"
    UNKNOWN = "tests/test_unreviewed_future_module.py"

    def entry(self, module, *, outcome="passed", complete=True):
        counts = dict.fromkeys(check_regression.COUNT_KEYS, 0)
        counts[outcome] = 1
        return {"module": module, "exit_code": 0 if outcome == "passed" else 1,
                "timed_out": False, "collected": 1, "counts": counts,
                "complete": complete, "duration_seconds": 0.01,
                "log": Path(module).stem + ".log", "result": Path(module).stem + ".json"}

    def test_priority_is_exclusive_safe_pool_overlaps_and_unknown_waits(self):
        """Two child slots must overlap, while priority/GUI/unknown never do."""
        modules = [self.UNKNOWN, self.SAFE_A, self.GUI, self.SAFE_B]
        barrier = threading.Barrier(2, timeout=5)
        lock = threading.Lock()
        active, started, overlaps = set(), [], []

        def child(_output, _index, module, _timeout):
            with lock:
                if module in (self.GUI, self.UNKNOWN):
                    self.assertFalse(active)
                else:
                    self.assertFalse(active & {self.GUI, self.UNKNOWN})
                active.add(module)
                started.append(module)
                overlaps.append(set(active))
            if module in (self.SAFE_A, self.SAFE_B):
                barrier.wait()
            with lock:
                active.remove(module)
            return self.entry(module)

        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "parallel"
            with patch.object(check_regression, "_run_module", side_effect=child), contextlib.redirect_stdout(io.StringIO()):
                code = check_regression.run(output, modules, 20, workers=2, prioritize=[self.GUI])
            summary = json.loads((output / "summary.json").read_text())
        self.assertEqual(code, 0)
        self.assertEqual(started[0], self.GUI)
        self.assertEqual(started[-1], self.UNKNOWN)
        self.assertIn({self.SAFE_A, self.SAFE_B}, overlaps)
        self.assertEqual(summary["scheduled_modules"], [self.GUI, self.SAFE_A, self.SAFE_B, self.UNKNOWN])
        self.assertEqual(summary["requested_modules"], modules)
        self.assertEqual(summary["parallel_modules"], [self.SAFE_A, self.SAFE_B])
        self.assertEqual(summary["module_count"], summary["modules_complete"])
        self.assertEqual(summary["counts"]["passed"], 4)
        self.assertEqual(summary["unrun_modules"], [])

    def test_default_one_worker_preserves_requested_order(self):
        modules = [self.UNKNOWN, self.SAFE_A, self.GUI, self.SAFE_B]
        launched = []

        def child(_output, _index, module, _timeout):
            launched.append(module)
            return self.entry(module)

        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "serial"
            with patch.object(check_regression, "_run_module", side_effect=child), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(check_regression.run(output, modules, 20), 0)
            summary = json.loads((output / "summary.json").read_text())
        self.assertEqual(launched, modules)
        self.assertEqual(summary["parallel_modules"], [])

    def test_fail_fast_drains_started_children_and_never_claims_unrun_passed(self):
        modules = [self.SAFE_A, self.SAFE_B, self.SAFE_C, self.GUI]
        barrier = threading.Barrier(2, timeout=5)
        failure_recorded = threading.Event()
        launched = []
        real_write = check_regression._write_json

        def checkpoint(path, payload):
            real_write(path, payload)
            if payload.get("stopped_early"):
                failure_recorded.set()

        def child(_output, _index, module, _timeout):
            launched.append(module)
            barrier.wait()
            if module == self.SAFE_A:
                return self.entry(module, outcome="failed", complete=False)
            self.assertTrue(failure_recorded.wait(5))
            return self.entry(module)

        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "failed"
            with patch.object(check_regression, "_run_module", side_effect=child), \
                    patch.object(check_regression, "_write_json", side_effect=checkpoint), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(check_regression.run(output, modules, 20, workers=2, fail_fast=True), 1)
            summary = json.loads((output / "summary.json").read_text())
            with self.assertRaisesRegex(ValueError, "finished run"):
                check_regression.consolidate([output], Path(folder) / "false-success.json")
        self.assertEqual(set(launched), {self.SAFE_A, self.SAFE_B})
        self.assertFalse(summary["success"])
        self.assertTrue(summary["finished_at"])
        self.assertEqual(summary["module_count"], 4)
        self.assertEqual(summary["modules_started"], 2)
        self.assertEqual(summary["modules_complete"], 1)
        self.assertEqual(summary["counts"]["passed"], 1)
        self.assertEqual(summary["counts"]["failed"], 1)
        self.assertEqual(summary["unrun_modules"], [self.SAFE_C, self.GUI])

    def test_priority_failure_stops_before_parallel_pool(self):
        modules = [self.SAFE_A, self.GUI, self.SAFE_B]
        with tempfile.TemporaryDirectory() as folder:
            with patch.object(check_regression, "_run_module",
                              return_value=self.entry(self.GUI, outcome="error", complete=False)) as child, \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(check_regression.run(Path(folder) / "failed", modules, 20,
                                 workers=2, fail_fast=True, prioritize=[self.GUI]), 1)
            child.assert_called_once()
            self.assertEqual(child.call_args.args[2], self.GUI)

    def test_skip_or_missing_teardown_blocks_success_and_triggers_fail_fast(self):
        for outcome in ("skipped", "incomplete", "error"):
            with self.subTest(outcome=outcome), tempfile.TemporaryDirectory() as folder:
                output = Path(folder) / "report"
                with patch.object(check_regression, "_run_module",
                                  return_value=self.entry(self.GUI, outcome=outcome)) as child, \
                        contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(check_regression.run(output, [self.GUI, self.UNKNOWN], 20, fail_fast=True), 1)
                child.assert_called_once()
                summary = json.loads((output / "summary.json").read_text())
                self.assertEqual(summary["unrun_modules"], [self.UNKNOWN])
                self.assertFalse(summary["success"])

    def test_without_fail_fast_later_modules_still_run_after_failure(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "report"
            with patch.object(check_regression, "_run_module", side_effect=[
                    self.entry(self.GUI, outcome="failed", complete=False), self.entry(self.UNKNOWN)]), \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(check_regression.run(output, [self.GUI, self.UNKNOWN], 20), 1)
            summary = json.loads((output / "summary.json").read_text())
        self.assertEqual(summary["unrun_modules"], [])
        self.assertEqual(len(summary["modules"]), 2)
        self.assertFalse(summary["success"])

    def test_invalid_schedule_does_not_create_output(self):
        for kwargs in ({"workers": 0}, {"prioritize": [self.UNKNOWN]},
                       {"prioritize": [self.GUI, self.GUI]}):
            with self.subTest(kwargs=kwargs), tempfile.TemporaryDirectory() as folder:
                output = Path(folder) / "invalid"
                with self.assertRaises(ValueError):
                    check_regression.run(output, [self.GUI, self.SAFE_A], 20, **kwargs)
                self.assertFalse(output.exists())

    def test_cli_priorities_reorder_full_selection_without_reducing_coverage(self):
        argv = ["check_regression.py", "--workers", "2", "--fail-fast",
                "--module", self.SAFE_A, "--module", self.GUI,
                "--prioritize", self.GUI]
        with patch.object(check_regression.sys, "argv", argv), \
                patch.object(check_regression, "run", return_value=0) as run:
            self.assertEqual(check_regression.main(), 0)
        self.assertEqual(run.call_args.args[1], [self.SAFE_A, self.GUI])
        self.assertEqual(run.call_args.kwargs, {"workers": 2, "fail_fast": True, "prioritize": [self.GUI]})

    def test_child_keeps_process_temp_network_flags_and_fingerprint_receipt(self):
        """Exercise the real launcher and receipt reader with a synthetic child."""
        contexts = []

        def process(argv, **kwargs):
            module = argv[argv.index("--_child") + 1]
            result = Path(argv[argv.index("--_child-result") + 1])
            child_temp = Path(argv[argv.index("--_child-temp") + 1])
            contexts.append((argv, kwargs, child_temp))
            node = module + "::synthetic"
            payload = {"module": module, "status": "complete", "exit_code": 0,
                       "collected": [node], "reports": {node: {
                           phase: {"outcome": "passed"} for phase in ("setup", "call", "teardown")}},
                       "collection_errors": [], "source_and_test_sha256": check_regression._fingerprint(module)}
            check_regression._write_json(result, payload)
            return SimpleNamespace(returncode=0)

        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "report"
            with patch.object(check_regression.subprocess, "run", side_effect=process), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(check_regression.run(output, [self.SAFE_A, self.SAFE_B], 20, workers=2), 0)
                target = Path(folder) / "consolidated.json"
                self.assertEqual(check_regression.consolidate([output], target), 0)
            consolidated = json.loads(target.read_text())
            self.assertEqual(consolidated["collected_unique"], 2)
            for module in consolidated["modules"]:
                self.assertEqual(module["source_and_test_sha256"], check_regression._fingerprint(module["module"]))
        self.assertEqual(len({row[2] for row in contexts}), 2)
        for argv, kwargs, child_temp in contexts:
            self.assertIn("-B", argv)
            self.assertIn("--_child", argv)
            self.assertEqual(kwargs["cwd"], check_regression.ROOT)
            self.assertEqual(kwargs["env"]["TEMP"], str(child_temp))
            self.assertEqual(kwargs["env"]["TMP"], str(child_temp))
            self.assertEqual(kwargs["env"]["PYTEST_DISABLE_PLUGIN_AUTOLOAD"], "1")
            self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)

    def test_timeout_launch_error_and_empty_child_never_pass(self):
        failures = [subprocess.TimeoutExpired("synthetic", 1), OSError("synthetic launch error"),
                    SimpleNamespace(returncode=0)]
        for failure in failures:
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as folder:
                output = Path(folder) / "report"
                kwargs = {"side_effect": failure} if isinstance(failure, Exception) else {"return_value": failure}
                with patch.object(check_regression.subprocess, "run", **kwargs), contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(check_regression.run(output, [self.SAFE_A], 20, fail_fast=True), 1)
                summary = json.loads((output / "summary.json").read_text())
                self.assertEqual(summary["modules_complete"], 0)
                self.assertEqual(summary["counts"]["passed"], 0)
                self.assertFalse(summary["success"])

    def test_timeout_preserves_partial_case_as_incomplete(self):
        def process(argv, **_kwargs):
            result = Path(argv[argv.index("--_child-result") + 1])
            node = self.SAFE_A + "::interrupted"
            check_regression._write_json(result, {
                "module": self.SAFE_A, "status": "running", "collected": [node],
                "reports": {node: {"call": {"outcome": "passed"}}}})
            raise subprocess.TimeoutExpired("synthetic", 1)

        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "report"
            with patch.object(check_regression.subprocess, "run", side_effect=process), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(check_regression.run(output, [self.SAFE_A, self.GUI], 1, fail_fast=True), 1)
            summary = json.loads((output / "summary.json").read_text())
            self.assertTrue(summary["modules"][0]["timed_out"])
            self.assertEqual(summary["counts"]["incomplete"], 1)
            self.assertEqual(summary["counts"]["passed"], 0)
            self.assertEqual(summary["unrun_modules"], [self.GUI])

    def test_retry_cannot_hide_changed_fingerprint_or_coverage(self):
        def process(argv, **_kwargs):
            module = argv[argv.index("--_child") + 1]
            result = Path(argv[argv.index("--_child-result") + 1])
            node = module + "::synthetic"
            check_regression._write_json(result, {
                "module": module, "status": "complete", "exit_code": 0,
                "collected": [node], "reports": {node: {
                    phase: {"outcome": "passed"} for phase in ("setup", "call", "teardown")}},
                "source_and_test_sha256": "a" * 64})
            return SimpleNamespace(returncode=0)

        with tempfile.TemporaryDirectory() as folder:
            initial, retry = (Path(folder) / name for name in ("initial", "retry"))
            with patch.object(check_regression.subprocess, "run", side_effect=process), contextlib.redirect_stdout(io.StringIO()):
                for output in (initial, retry):
                    self.assertEqual(check_regression.run(output, [self.SAFE_A], 20, workers=2), 0)
            path = retry / (Path(self.SAFE_A).stem + ".json")
            original = json.loads(path.read_text())
            changed = {**original, "source_and_test_sha256": "b" * 64}
            check_regression._write_json(path, changed)
            with self.assertRaisesRegex(ValueError, "source changed"):
                check_regression.consolidate([initial, retry], Path(folder) / "false-success.json")
            changed = {**original, "collected": [self.SAFE_A + "::different"],
                       "reports": {self.SAFE_A + "::different": next(iter(original["reports"].values()))}}
            check_regression._write_json(path, changed)
            with self.assertRaisesRegex(ValueError, "different test IDs"):
                check_regression.consolidate([initial, retry], Path(folder) / "false-success.json")
            self.assertFalse((Path(folder) / "false-success.json").exists())
