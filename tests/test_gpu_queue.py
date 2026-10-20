"""Real interprocess admission/owner-death tests; no model or GPU is loaded."""
import json
from contextlib import closing
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import uuid

from ab_ocr.gpu_queue import GPULease, _identity
from ab_ocr.ocr import OCRCancelled, OCRError


@unittest.skipUnless(os.name == "nt", "Windows global mutex contract")
class GPUQueueTests(unittest.TestCase):
    def setUp(self):
        folder = Path(__file__).resolve().parents[1] / "work" / "gpu-session-tests"
        folder.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=folder)
        self.root = Path(self.temporary.name)
        self.name = "YeonE_test_" + uuid.uuid4().hex
        self.children = []

    def tearDown(self):
        for child in self.children:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=10)
        self.temporary.cleanup()

    def lease(self, **kwargs):
        return GPULease("synthetic", queue_dir=self.root, name=self.name, **kwargs)

    def count(self):
        with closing(sqlite3.connect(self.root / "gpu-queue.sqlite3")) as db:
            return db.execute("SELECT count(*) FROM tickets").fetchone()[0]

    def wait(self, predicate):
        deadline = time.monotonic() + 10
        while not predicate():
            self.assertLess(time.monotonic(), deadline, "Child progress timed out")
            time.sleep(0.02)

    def child(self, label, hold=False):
        code = ("from ab_ocr.gpu_queue import GPULease;from pathlib import Path;import sys,time,json;"
            "root=Path(sys.argv[1]);name=sys.argv[2];label=sys.argv[3];hold=sys.argv[4]=='1'\n"
            "with GPULease('synthetic',queue_dir=root,name=name) as lease:\n"
            " (root/(label+'.acquired')).write_text(json.dumps(lease.evidence()))\n"
            " with (root/'order').open('a') as f:f.write(label+'\\n')\n"
            " while hold and not (root/(label+'.release')).exists():time.sleep(.02)\n")
        child = subprocess.Popen([sys.executable, "-c", code, str(self.root), self.name, label, "1" if hold else "0"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)
        self.children.append(child)
        return child

    def test_fifo_between_real_processes(self):
        with self.lease():
            first = self.child("first")
            self.wait(lambda: self.count() == 2)
            second = self.child("second")
            self.wait(lambda: self.count() == 3)
            self.assertFalse((self.root / "first.acquired").exists())
        self.assertEqual(first.wait(timeout=10), 0)
        self.assertEqual(second.wait(timeout=10), 0)
        self.assertEqual((self.root / "order").read_text().splitlines(), ["first", "second"])
        self.assertEqual(self.count(), 0)

    def test_wait_cancel_removes_only_own_ticket_and_reports_position(self):
        stop = threading.Event()
        events, errors = [], []
        def waiting():
            try:
                with self.lease(should_stop=stop.is_set, on_wait=events.append):
                    self.fail("Cancelled waiter acquired GPU")
            except OCRCancelled:
                errors.append("cancelled")
        with self.lease():
            thread = threading.Thread(target=waiting)
            thread.start()
            self.wait(lambda: bool(events))
            self.assertEqual(events[0]["position"], 2)
            self.assertEqual(events[0]["phase"], "gpu_wait")
            stop.set()
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, ["cancelled"])
            self.assertEqual(self.count(), 1)
        self.assertEqual(self.count(), 0)

    def test_dead_owner_is_recovered_without_touching_other_processes(self):
        owner = self.child("owner", hold=True)
        self.wait(lambda: (self.root / "owner.acquired").exists())
        waiter = self.child("waiter")
        self.wait(lambda: self.count() == 2)
        owner.kill()  # Exclusively the disposable process this test created.
        owner.wait(timeout=10)
        self.assertEqual(waiter.wait(timeout=10), 0)
        evidence = json.loads((self.root / "waiter.acquired").read_text())
        self.assertTrue(evidence["dead_owner_recovered"])
        self.assertEqual(self.count(), 0)

    def test_pid_reuse_identity_is_not_treated_as_live_owner(self):
        with self.lease():
            pass
        with closing(sqlite3.connect(self.root / "gpu-queue.sqlite3")) as db:
            db.execute("INSERT INTO tickets(token,pid,created,engine) VALUES('stale',?,?,?)",
                       (os.getpid(), "different-creation-time", "synthetic"))
            db.commit()
        with self.lease() as lease:
            self.assertTrue(lease.recovered)
        self.assertEqual(self.count(), 0)

    def test_unverifiable_owner_is_never_stolen(self):
        with self.lease():
            pass
        with closing(sqlite3.connect(self.root / "gpu-queue.sqlite3")) as db:
            db.execute("INSERT INTO tickets(token,pid,created,engine) VALUES('unknown',999999,'unknown','synthetic')")
            db.commit()
        actual = _identity
        with patch("ab_ocr.gpu_queue._identity", side_effect=lambda pid: None if pid == 999999 else actual(pid)):
            with self.assertRaises(OCRError):
                with self.lease(timeout=.15):
                    self.fail("An unknown owner was stolen")
        self.assertEqual(self.count(), 1)

    def test_corrupt_database_fails_closed(self):
        (self.root / "gpu-queue.sqlite3").write_bytes(b"not a sqlite database")
        with self.assertRaises(OCRError) as raised:
            with self.lease():
                self.fail("An independent fallback queue must not be created")
        self.assertIn("DatabaseError", str(raised.exception))
        self.assertNotIn(str(self.root), str(raised.exception))
        # Explicit repair of this disposable fixture, never an automatic product
        # DB reset, proves the failed acquisition leaked no native mutex/handle.
        (self.root / "gpu-queue.sqlite3").unlink()
        with self.lease():
            self.assertEqual(self.count(), 1)
        self.assertEqual(self.count(), 0)


if __name__ == "__main__":
    unittest.main()
