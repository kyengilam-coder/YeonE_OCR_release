import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ab_ocr.local_engines import RecognitionSession
from ab_ocr.ocr import OCRError


class SessionTests(unittest.TestCase):
    def setUp(self):
        folder = Path(__file__).resolve().parents[1] / "work" / "gpu-session-tests"
        folder.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=folder)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "synthetic.png"
        self.source.write_bytes(b"synthetic image only")
        self.worker = self.root / "rapidocr_worker.py"
        self.worker.write_text("# fixed worker")
        self.worker.with_name("worker_sessions.py").write_text("# fixed protocol")
        self.info = {"available": True, "engine": "paddle_server", "languages": ["chi_tra"],
            "executable": "unused", "worker": str(self.worker), "model_dir": str(self.root),
            "models": {"m": {"sha256": "a" * 64, "bytes": 1}}, "language_models": {"chi_tra": ["m"]},
            "installation": {"execution_provider": "cuda"}, "runtime_files": {},
            "worker_sha256": "b" * 64, "installation_sha256": "c" * 64, "worker_args": []}
        self.events = []
        self.processes = []
        self.behavior = None
        test = self
        class FakeLease:
            def __init__(self, *a, **kw):
                pass
            def __enter__(self):
                test.events.append("acquire")
                return self
            def close(self):
                test.events.append("release")
                if test.behavior in ("cleanup-failure", "request-and-cleanup"):
                    raise OSError("private-key-or-filename-must-never-leave-cleanup")
            def evidence(self):
                return {"schema": "synthetic-lease"}
        class FakeProcess:
            def __init__(self, argv, root, **kw):
                self.root = Path(root)
                self.calls = 0
                self.pid = 100 + len(test.processes)
                test.processes.append(self)
            def request(self, request):
                self.calls += 1
                if test.behavior == "request-and-cleanup":
                    raise OCRError("original request failure")
                text = "漢，汉。𠀀한글"
                if test.behavior == "surya-failure":
                    (self.root / (request["output"] + ".failure.json")).write_text(json.dumps({
                        "engine": "surya", "status": "failed", "raw_completions": [{"content": "<p>未完</p>"}]}), encoding="utf-8")
                    return {"status": "failed"}
                payload = {"text": text, "words": [], "engine_version": "synthetic", "engine_metadata": {
                    "worker_session": {"request_id": request["id"], "session_id": self.root.name,
                        "request_ordinal": self.calls, "worker_pid": self.pid, "server_pid": self.pid + 1000}}}
                if test.behavior == "incomplete":
                    payload["engine_metadata"]["generation_complete"] = False
                (self.root / request["output"]).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
                if test.behavior == "ack-lost":
                    raise OCRError("Synthetic worker exit after atomic result")
                return {"status": "ok"}
            def close(self):
                test.events.append("worker-close")
        def render(_source, destination, _page, _dpi, **_kwargs):
            destination.write_bytes(b"synthetic PNG")
            return 100, 100
        for target, replacement in (("ab_ocr.local_engines.inspect_engine", lambda *a, **k: dict(self.info)),
            ("ab_ocr.worker_sessions.WorkerProcess", FakeProcess), ("ab_ocr.gpu_queue.GPULease", FakeLease),
            ("ab_ocr.ocr._render", render)):
            mock = patch(target, replacement)
            mock.start()
            self.addCleanup(mock.stop)

    def test_reuses_one_worker_and_server_for_independent_requests(self):
        with RecognitionSession("paddle_server", "chi_tra") as session:
            first = session.recognize_file(self.source)
            second = session.recognize_file(self.source)
        self.assertEqual(len(self.processes), 1)
        self.assertEqual(self.processes[0].calls, 2)
        self.assertEqual(first.text, "漢，汉。𠀀한글")
        a, b = [r.engine_metadata["worker_session"] for r in (first, second)]
        self.assertEqual(a["worker_pid"], b["worker_pid"])
        self.assertEqual(a["server_pid"], b["server_pid"])
        self.assertNotEqual(a["request_id"], b["request_id"])
        self.assertEqual((a["request_ordinal"], b["request_ordinal"]), (1, 2))
        self.assertEqual(self.events, ["acquire", "worker-close", "release"])

    def test_condition_snapshot_is_detached_and_does_not_start_worker_or_gpu(self):
        session = RecognitionSession("paddle_server", "chi_tra", engine_root=self.root)
        self.info["engine_root"] = str(self.root)
        snapshot = session.condition_snapshot()
        self.assertEqual(snapshot["engine_root"], str(self.root.resolve()))
        self.assertEqual(snapshot["device"], "cuda")
        snapshot["pin"]["models"].clear()
        self.assertTrue(session.pin["models"])
        self.assertEqual(self.events, [])
        self.assertEqual(self.processes, [])

    def test_bounded_batch_releases_gpu_then_requeues_without_replay(self):
        with RecognitionSession("paddle_server", "chi_tra", max_requests=2) as session:
            results = [session.recognize_file(self.source) for _ in range(3)]
        self.assertEqual([p.calls for p in self.processes], [2, 1])
        self.assertNotEqual(results[0].engine_metadata["worker_session"]["session_id"],
                            results[2].engine_metadata["worker_session"]["session_id"])
        self.assertEqual(self.events, ["acquire", "worker-close", "release"] * 2)

    def test_cpu_request_does_not_acquire_gpu(self):
        with RecognitionSession("paddle_server", "chi_tra", device="cpu") as session:
            result = session.recognize_file(self.source)
        self.assertNotIn("acquire", self.events)
        self.assertEqual(result.engine_metadata["session_conditions"]["device"], "cpu")

    def test_changed_runtime_closes_owned_worker_and_releases_gpu(self):
        with RecognitionSession("paddle_server", "chi_tra") as session:
            session.recognize_file(self.source)
            self.info["installation_sha256"] = "d" * 64
            with self.assertRaises(OCRError):
                session.recognize_file(self.source)
        self.assertEqual(self.processes[0].calls, 1)
        self.assertEqual(self.events, ["acquire", "worker-close", "release"])

    def test_unavailable_device_never_silently_uses_another(self):
        self.info["installation"]["execution_provider"] = "cpu"
        with RecognitionSession("paddle_server", "chi_tra", device="cuda") as session:
            with self.assertRaises(OCRError):
                session.recognize_file(self.source)
        self.assertFalse(self.processes)

    def test_incomplete_raw_is_preserved_without_retry_and_session_is_closed(self):
        self.behavior = "incomplete"
        with RecognitionSession("paddle_server", "chi_tra") as session:
            result = session.recognize_file(self.source)
            self.assertIsNone(session.worker)
        self.assertEqual(result.text, "漢，汉。𠀀한글")
        self.assertFalse(result.engine_metadata["generation_complete"])
        self.assertEqual(self.processes[0].calls, 1)

    def test_atomic_result_after_lost_ack_is_recovered_without_replay(self):
        self.behavior = "ack-lost"
        with RecognitionSession("paddle_server", "chi_tra") as session:
            result = session.recognize_file(self.source)
        self.assertTrue(result.engine_metadata["session_ack_missing_recovered_from_atomic_output"])
        self.assertEqual(self.processes[0].calls, 1)

    def test_failed_surya_html_is_preserved_as_evidence_only(self):
        self.behavior = "surya-failure"
        self.info["engine"] = "surya"
        self.info["installation"]["backend_device"] = "cuda"
        with RecognitionSession("surya", "chi_tra") as session:
            result = session.recognize_file(self.source)
        self.assertEqual(result.text, "")
        self.assertFalse(result.engine_metadata["generation_complete"])
        self.assertEqual(result.engine_metadata["raw_failure"]["raw_completions"][0]["content"], "<p>未完</p>")
        self.assertEqual(self.processes[0].calls, 1)

    def test_cleanup_failure_returns_completed_raw_then_blocks_every_new_request(self):
        self.behavior = "cleanup-failure"
        session = RecognitionSession("paddle_server", "chi_tra", max_requests=1)
        session.__enter__()
        result = session.recognize_file(self.source)
        self.assertEqual(result.text, "漢，汉。𠀀한글")
        self.assertTrue(result.engine_metadata["session_halted_after_completed_raw"])
        self.assertEqual(result.engine_metadata["session_cleanup_failures"],
                         [{"stage": "gpu_lease", "error_type": "OSError"}])
        self.assertNotIn("private-key", json.dumps(result.to_dict()))
        # This is the checkpoint opportunity the caller receives before exit.
        checkpointed = result.to_dict()
        with self.assertRaises(OCRError):
            session.recognize_file(self.source)
        with self.assertRaises(OCRError):
            session.close()
        self.assertEqual(checkpointed["text"], result.text)
        self.assertEqual(self.processes[0].calls, 1)

    def test_cleanup_failure_does_not_mask_original_request_exception(self):
        self.behavior = "request-and-cleanup"
        with self.assertRaisesRegex(OCRError, "original request failure"):
            with RecognitionSession("paddle_server", "chi_tra") as session:
                session.recognize_file(self.source)
        self.assertEqual(self.processes[0].calls, 1)
        self.assertEqual(session.cleanup_failures, [{"stage": "gpu_lease", "error_type": "OSError"}])


if __name__ == "__main__":
    unittest.main()
