"""A Windows sharing lock must not invalidate successful product tests."""
import json
import os
from pathlib import Path
import tempfile
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
