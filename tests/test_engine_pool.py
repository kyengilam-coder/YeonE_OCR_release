"""Resident engine workers and parallel engines: same results, fewer processes."""
import io
import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import pytest

from ab_ocr import ocr
from yeone_ocr import engine_pool
from yeone_ocr.engine_pool import EnginePool, engine_parallelism


class Parallelism(unittest.TestCase):
    def test_cores_and_memory_bound_the_engine_count(self):
        engines = ["ppocrv6", "rapidocr", "easyocr", "tesseract"]
        with mock.patch.dict("os.environ", {engine_pool.PARALLEL_ENV: ""}):
            self.assertEqual(engine_parallelism(engines, cpu_count=20), 4)
            self.assertEqual(engine_parallelism(engines, cpu_count=8), 2)
            self.assertEqual(engine_parallelism(engines, cpu_count=4), 1)
            tight = {"available_commit_mb": 3000, "available_physical_mb": 3000}
            self.assertEqual(engine_parallelism(engines, cpu_count=20, memory=tight), 1)   # easyocr 2048 + 640 > 2400
            self.assertEqual(engine_parallelism(engines, cpu_count=20, memory=tight, resident={"easyocr"}), 4)
            self.assertEqual(engine_parallelism(engines, 3, cpu_count=2), 3)
            self.assertEqual(engine_parallelism(engines, 9, cpu_count=2), 4)
        with mock.patch.dict("os.environ", {engine_pool.PARALLEL_ENV: "1"}):
            self.assertEqual(engine_parallelism(engines, cpu_count=20), 1)


class FakeSession:
    instances = []

    def __init__(self, engine, languages, **kwargs):
        self.engine, self.languages, self.kwargs = engine, languages, kwargs
        self.calls, self.worker, self.cleanup_failures, self.closed = 0, None, [], False
        FakeSession.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.closed = True

    def recognize_file(self, path, *, page_number=1, dpi=300, psm=None):
        self.calls += 1
        self.worker = object()
        return {"engine": self.engine, "page": page_number}

    def close(self):
        self.closed = True


class Pool(unittest.TestCase):
    def setUp(self):
        FakeSession.instances = []
        patcher = mock.patch("ab_ocr.local_engines.RecognitionSession", FakeSession)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_one_session_per_engine_across_pages_and_one_shot_otherwise(self):
        one_shot = mock.Mock(return_value="one-shot")
        with mock.patch.object(ocr, "recognize_file", one_shot):
            with EnginePool(enabled=True) as pool:
                for page in (1, 2, 3):
                    self.assertEqual(pool.recognize("a.pdf", engine="ppocrv6", page_number=page, languages="chi_tra",
                                                    direction="horizontal")["page"], page)
                self.assertEqual(pool.resident(), {"ppocrv6"})
                self.assertEqual(pool.recognize("a.pdf", engine="ppocrv6", direction="vertical"), "one-shot")
                self.assertEqual(pool.recognize("a.pdf", engine="tesseract", psm=6, verify_chinese_glyphs=True), "one-shot")
        self.assertEqual(len(FakeSession.instances), 1)
        session = FakeSession.instances[0]
        self.assertEqual((session.calls, session.kwargs["device"], session.closed), (3, "cpu", True))
        self.assertEqual(one_shot.call_args_list[0].kwargs["direction"], "vertical")
        self.assertEqual(one_shot.call_args_list[1].kwargs["verify_chinese_glyphs"], True)

    def test_disabled_pool_never_starts_sessions(self):
        with mock.patch.object(ocr, "recognize_file", return_value="one-shot"):
            with EnginePool(enabled=False) as pool:
                self.assertEqual(pool.recognize("a.pdf", engine="ppocrv6", direction="horizontal"), "one-shot")
        self.assertEqual(FakeSession.instances, [])

    def test_cleanup_failure_falls_back_to_one_shot_for_that_engine(self):
        logs = []
        with mock.patch.object(ocr, "recognize_file", return_value="one-shot"):
            with EnginePool(enabled=True, log=logs.append) as pool:
                pool.recognize("a.pdf", engine="easyocr", direction="horizontal")
                FakeSession.instances[0].cleanup_failures.append({"stage": "temporary_files"})
                self.assertEqual(pool.recognize("a.pdf", engine="easyocr", direction="horizontal"), "one-shot")
                self.assertEqual(pool.recognize("a.pdf", engine="easyocr", direction="horizontal"), "one-shot")
        self.assertEqual(len(FakeSession.instances), 1)
        self.assertTrue(logs)


@pytest.fixture
def project(tmp_path):
    from PIL import Image
    from yeone_ocr.comparison import ComparisonProject
    source = tmp_path / "page.png"
    Image.new("RGB", (400, 200), "white").save(source)
    return ComparisonProject.open(source)


def test_parallel_engines_record_in_engine_order(project):
    from yeone_ocr.comparison import run_page
    order, running, peak = [], [0], [0]
    lock = threading.Lock()
    delays = {"ppocrv6": .15, "rapidocr": .05, "easyocr": .0}

    def recognize(source, *, engine, **kwargs):
        with lock:
            running[0] += 1
            peak[0] = max(peak[0], running[0])
        time.sleep(delays[engine])
        with lock:
            running[0] -= 1
        return ocr.RecognitionResult(text=engine, engine=engine, source_path=str(project.source_path),
            source_sha256=project.source_sha256, page_number=1, image_width=400, image_height=200,
            engine_version="synthetic", languages="chi_tra", psm=None, duration_seconds=.01,
            words=[{"text": engine, "bbox": [10, 10, 390, 100], "confidence": 90}])

    outcome = run_page(project, 1, engines=["ppocrv6", "rapidocr", "easyocr"], recognizer=recognize,
                       on_result=lambda result: order.append(result.engine), parallel=3)
    assert outcome["completed"] == ["ppocrv6", "rapidocr", "easyocr"]
    assert order == ["ppocrv6", "rapidocr", "easyocr"]
    assert [r["engine"] for r in project.page(1)["runs"]] == ["ppocrv6", "rapidocr", "easyocr"]
    assert peak[0] > 1


def test_parallel_failure_of_one_engine_keeps_the_others(project):
    from yeone_ocr.comparison import run_page

    def recognize(source, *, engine, **kwargs):
        if engine == "rapidocr":
            raise ocr.OCRError("synthetic")
        return ocr.RecognitionResult(text=engine, engine=engine, source_path=str(project.source_path),
            source_sha256=project.source_sha256, page_number=1, image_width=400, image_height=200,
            engine_version="synthetic", languages="chi_tra", psm=None, duration_seconds=.01, words=[])

    outcome = run_page(project, 1, engines=["ppocrv6", "rapidocr", "easyocr"], recognizer=recognize, parallel=2)
    assert outcome["completed"] == ["ppocrv6", "easyocr"]
    assert set(outcome["errors"]) == {"rapidocr"}


def _serve(module, argv, tmp_path, worker_cls):
    """Drive a worker's --session-root mode over the real file-ack protocol."""
    root = tmp_path / "session"
    root.mkdir()
    (root / "in.png").write_bytes(b"png")
    token = "a" * 32
    request = json.dumps({"id": token, "input": "in.png", "output": "out.json", "ack": "ack.json", "languages": "chi_tra"})
    stdin = io.TextIOWrapper(io.BytesIO((request + "\n").encode("utf-8")))
    with mock.patch.object(sys, "stdin", stdin), mock.patch.object(sys, "argv", ["worker", *argv, "--session-root", str(root), "--languages", "chi_tra"]):
        code = module.main([*argv, "--session-root", str(root), "--languages", "chi_tra"]) \
            if module.__name__.endswith("easyocr_worker") else module.main()
    return code, root


def test_easyocr_session_mode_pins_direction_and_loads_once(tmp_path):
    easy = pytest.importorskip("ab_ocr.easyocr_worker")
    created = []

    class Worker:
        def __init__(self, model_dir, threads, verify_lines=True, batch_size=1):
            created.append(threads)

        def recognize(self, source, languages, direction):
            return {"text": direction, "engine_metadata": {}}

    with mock.patch.object(easy, "EasyOCRWorker", Worker):
        code, root = _serve(easy, ["--model-dir", str(tmp_path), "--direction", "horizontal"], tmp_path, Worker)
    assert code == 0 and created == [2]
    assert json.loads((root / "out.json").read_text(encoding="utf-8"))["text"] == "horizontal"
    assert json.loads((root / "ack.json").read_text(encoding="utf-8"))["status"] == "ok"


def test_ppocrv6_session_mode_pins_direction_and_loads_once(tmp_path):
    v6 = pytest.importorskip("ab_ocr.ppocrv6_worker")
    created = []

    class Worker:
        def __init__(self, model_dir, verify_lines=True):
            created.append(model_dir)

        def recognize(self, source, languages, direction):
            return {"text": direction, "engine_metadata": {}}

    with mock.patch.object(v6, "Worker", Worker), mock.patch.object(v6._shared, "block_network"):
        code, root = _serve(v6, ["--model-dir", str(tmp_path), "--direction", "horizontal"], tmp_path, Worker)
    assert code == 0 and len(created) == 1
    assert json.loads((root / "out.json").read_text(encoding="utf-8"))["text"] == "horizontal"


def _install(root, engine):
    folder = root / "tools" / engine
    folder.mkdir(parents=True)
    (folder / "installation.json").write_text("{}", encoding="utf-8")


def test_cuda_engines_join_automatic_selection_only_when_installed_with_cuda(tmp_path):
    from yeone_ocr import automatic_ocr as auto
    for engine in ("surya", "glmocr", "paddle_server"):
        _install(tmp_path, engine)
    devices = {"surya": "cpu", "glmocr": "cuda", "paddle_server": "cuda"}
    broken = set()

    def doctor(*, engine, engine_root=None, should_stop=None):
        if engine in auto.PREFERENCE or engine in broken:
            return {"available": False, "message": "not installed"}
        return {"available": True, "engine": engine, "languages": ["chi_tra", "kor+chi_tra"],
                "installation": {"backend_device": devices[engine], "execution_provider": devices[engine]}}

    with mock.patch.object(auto.ocr, "doctor", side_effect=doctor), mock.patch.object(auto, "memory_status", return_value=None), \
            mock.patch("yeone_ocr.automatic_ocr.engine_root_for", return_value=tmp_path):
        ready, excluded = auto.available_engines("traditional", "horizontal", gpu=True)
        assert ready == ["glmocr"]                                   # one GPU engine per run
        assert "surya" not in excluded                               # not even inspected once one is chosen
        assert auto.available_engines("traditional", "horizontal")[0] == []          # off unless the caller can run GPU sessions
        assert auto.available_engines("traditional", "vertical", gpu=True)[0] == []  # vertical not measured
        devices["surya"] = "cuda"
        ready, _ = auto.available_engines("mixed", "horizontal", gpu=True)
        assert ready == ["surya"]                                                    # mixed: Surya first
        devices["surya"] = "cpu"
        broken.add("glmocr")                                                         # GLM-OCR is CUDA-only; make it unavailable
        ready, _ = auto.available_engines("traditional", "horizontal", gpu=True)
        assert ready == ["paddle_server"]                                            # next usable CUDA engine


def test_gpu_engines_take_no_cpu_slot():
    with mock.patch.dict("os.environ", {engine_pool.PARALLEL_ENV: ""}):
        assert engine_parallelism(["ppocrv6", "tesseract", "glmocr"], cpu_count=4) == 2
        assert engine_parallelism(["glmocr"], cpu_count=1) == 1
        assert engine_parallelism(["ppocrv6", "rapidocr", "glmocr"], cpu_count=8) == 3


def test_gpu_session_failure_is_reported_once_then_refused(tmp_path):
    starts = []

    class Failing(FakeSession):
        def recognize_file(self, *a, **k):
            starts.append(self.kwargs["device"])
            raise OSError("no GPU queue")

    logs = []
    with mock.patch("ab_ocr.local_engines.RecognitionSession", Failing):
        with EnginePool(enabled=True, log=logs.append) as pool:
            with pytest.raises(OSError):
                pool.recognize("a.pdf", engine="glmocr", languages="chi_tra")
            with pytest.raises(ocr.OCRError):
                pool.recognize("a.pdf", engine="glmocr", languages="chi_tra")
    assert starts == ["cuda"]
    assert len(logs) == 1


def test_gpu_queue_wait_is_reported_every_30_seconds():
    seen = []

    class Waiting(FakeSession):
        def recognize_file(self, *a, **k):
            for elapsed in (0.1, 5, 31, 40, 62):
                self.kwargs["on_wait"]({"phase": "gpu_wait", "position": 1, "elapsed_seconds": elapsed})
            return {"engine": self.engine}

    with mock.patch("ab_ocr.local_engines.RecognitionSession", Waiting):
        with EnginePool(enabled=True, log=seen.append) as pool:
            pool.recognize("a.pdf", engine="surya", languages="kor+chi_tra")
    assert len(seen) == 3 and all("GPU 대기 중" in line for line in seen)


def test_a_second_gpu_engine_is_refused_instead_of_waiting_forever():
    FakeSession.instances = []
    with mock.patch("ab_ocr.local_engines.RecognitionSession", FakeSession):
        with EnginePool(enabled=True) as pool:
            pool.recognize("a.pdf", engine="glmocr", languages="chi_tra")
            with pytest.raises(ocr.OCRError):
                pool.recognize("a.pdf", engine="surya", languages="chi_tra")
            pool.recognize("a.pdf", engine="glmocr", languages="chi_tra", page_number=2)
    assert [s.engine for s in FakeSession.instances] == ["glmocr"]


def test_gpu_session_work_and_close_stay_on_one_thread_across_page_threads():
    from concurrent.futures import ThreadPoolExecutor
    threads = []

    class Owned(FakeSession):
        def recognize_file(self, *a, **k):
            threads.append(threading.get_ident())
            return {"engine": self.engine}

        def close(self):
            threads.append(threading.get_ident())
            self.closed = True

    with mock.patch("ab_ocr.local_engines.RecognitionSession", Owned):
        with EnginePool(enabled=True) as pool:
            for page in (1, 2, 3):                                     # run_page: a new thread pool per page
                with ThreadPoolExecutor(1) as page_threads:
                    page_threads.submit(pool.recognize, "a.pdf", engine="surya", languages="kor+chi_tra",
                                        page_number=page).result()
    assert len(threads) == 4 and len(set(threads)) == 1             # 3 requests + close, one owner thread
    assert threads[0] != threading.get_ident()
