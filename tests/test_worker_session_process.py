"""Owned persistent worker/descendant cleanup, using synthetic stdlib workers."""
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest

from ab_ocr.gpu_queue import _identity
from ab_ocr.ocr import OCRCancelled, OCRError
from ab_ocr.worker_sessions import WorkerProcess


@unittest.skipUnless(os.name == "nt", "Windows job tree contract")
class WorkerProcessTests(unittest.TestCase):
    def setUp(self):
        folder = Path(__file__).resolve().parents[1] / "work" / "gpu-session-tests"
        folder.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=folder)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        helper = Path(__file__).resolve().parents[1] / "ab_ocr" / "worker_sessions.py"
        script = self.root / "synthetic_worker.py"
        script.write_text('''import importlib.util,json,os,subprocess,sys,time
from pathlib import Path
spec=importlib.util.spec_from_file_location('protocol',sys.argv[2])
protocol=importlib.util.module_from_spec(spec);spec.loader.exec_module(protocol)
root=Path(sys.argv[1])
def write(path,value):
 if path.exists():raise FileExistsError('exclusive output')
 temporary=path.with_suffix('.tmp')
 with temporary.open('w',encoding='utf-8') as f:
  json.dump(value,f,ensure_ascii=False);f.flush();os.fsync(f.fileno())
 os.replace(temporary,path)
class Worker:
 def __init__(self):
  self.process=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
  (root/'owned-server-pid').write_text(str(self.process.pid))
 def recognize(self,path,languages):
  if path.read_text()=='slow':time.sleep(20)
  return {'text':'漢，한글。','words':[],'engine_version':'synthetic','engine_metadata':{}}
raise SystemExit(protocol.serve_requests(Worker(),root,'chi_tra',write))
''', encoding="utf-8")
        self.argv = [sys.executable, "-I", str(script), str(self.root), str(helper)]

    def request(self, token="a", content="normal"):
        token *= 32
        source = self.root / (token + ".input")
        source.write_text(content)
        return {"id": token, "input": source.name, "output": token + ".json",
                "ack": token + ".ack", "languages": "chi_tra"}

    def assert_server_gone(self):
        pid = int((self.root / "owned-server-pid").read_text())
        deadline = time.monotonic() + 5
        while _identity(pid) is not False and time.monotonic() < deadline:
            time.sleep(.02)
        self.assertIs(_identity(pid), False)

    def test_real_protocol_reuses_owned_server_and_closes_descendant(self):
        instance = WorkerProcess(self.argv, self.root, timeout=10)
        try:
            records = []
            for token in ("a", "b"):
                request = self.request(token)
                self.assertEqual(instance.request(request)["status"], "ok")
                records.append(json.loads((self.root / request["output"]).read_text(encoding="utf-8")))
            first, second = [r["engine_metadata"]["worker_session"] for r in records]
            self.assertEqual(first["worker_pid"], second["worker_pid"])
            self.assertEqual(first["server_pid"], second["server_pid"])
            self.assertEqual((first["request_ordinal"], second["request_ordinal"]), (1, 2))
            self.assertEqual(records[0]["text"], "漢，한글。")
        finally:
            instance.close()
        self.assert_server_gone()

    def test_cancel_during_request_cleans_only_owned_tree(self):
        stop = threading.Event()
        instance = WorkerProcess(self.argv, self.root, timeout=10, should_stop=stop.is_set)
        timer = threading.Timer(.3, stop.set)
        timer.start()
        try:
            with self.assertRaises(OCRCancelled):
                instance.request(self.request(content="slow"))
        finally:
            timer.cancel()
            instance.close()
        self.assert_server_gone()

    def test_timeout_has_no_automatic_replay_and_cleans_owned_tree(self):
        instance = WorkerProcess(self.argv, self.root, timeout=.3)
        try:
            with self.assertRaises(OCRError):
                instance.request(self.request(content="slow"))
        finally:
            instance.close()
        self.assertEqual(len(list(self.root.glob("*.input"))), 1)
        self.assertFalse(list(self.root.glob("*.json")))
        self.assert_server_gone()


if __name__ == "__main__":
    unittest.main()
