"""Pinned CRT and complete archived license checks without network or install."""
import hashlib
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from tools.prepare_msvc_runtime import archived_license, fetch_verified

ROOT = Path(__file__).resolve().parents[1]


class Response(io.BytesIO):
    def __init__(self, content, url="https://download.visualstudio.microsoft.com/pinned.exe"):
        super().__init__(content)
        self.url = url

    def geturl(self):
        return self.url


class MSVCRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)

    def test_official_https_redirect_keeps_exact_pinned_payload(self):
        data = b"synthetic pinned executable bytes"
        path = self.folder / "payload.exe"
        with patch("urllib.request.urlopen", return_value=Response(data)):
            fetch_verified("https://aka.ms/synthetic", path, hashlib.sha256(data).hexdigest())
        self.assertEqual(path.read_bytes(), data)
        with patch("urllib.request.urlopen", side_effect=AssertionError("valid cache must stay offline")):
            fetch_verified("https://aka.ms/synthetic", path, hashlib.sha256(data).hexdigest())

    def test_html_wrapper_or_changed_binary_cannot_replace_cache(self):
        path = self.folder / "payload.exe"
        path.write_bytes(b"existing corrupt cache retained for inspection")
        before = path.read_bytes()
        with patch("urllib.request.urlopen", return_value=Response(b"<html>dynamic cookie wrapper</html>")):
            with self.assertRaisesRegex(ValueError, "pinned hash"):
                fetch_verified("https://aka.ms/synthetic", path, "a" * 64)
        self.assertEqual(path.read_bytes(), before)
        self.assertFalse(path.with_name(path.name + ".download").exists())

    def test_insecure_redirect_is_rejected(self):
        with patch("urllib.request.urlopen", return_value=Response(b"bytes", "http://example.invalid/payload")):
            with self.assertRaisesRegex(ValueError, "non-HTTPS"):
                fetch_verified("https://aka.ms/synthetic", self.folder / "payload.exe", "a" * 64)

    def test_complete_archive_is_pinned_and_never_downloads_page_wrapper(self):
        lock = json.loads((ROOT / "packaging/msvc-runtime-lock.json").read_text(encoding="utf-8"))
        with patch("urllib.request.urlopen", side_effect=AssertionError("license must use committed archive")):
            text = archived_license(lock, ROOT)
        self.assertIn("EULA ID: Cpp_v14_ENU.1033", text)
        self.assertTrue(text.startswith("MICROSOFT SOFTWARE LICENSE TERMS"))
        self.assertEqual(hashlib.sha256(text.encode("utf-8")).hexdigest(), lock["license_archive_sha256"])
        bad = {**lock, "license_archive_sha256": "0" * 64}
        with self.assertRaisesRegex(ValueError, "pinned hash"):
            archived_license(bad, ROOT)

    def test_even_hash_matching_html_is_not_accepted_as_license(self):
        data = b"<html>MICROSOFT SOFTWARE LICENSE TERMS dynamic WordPress wrapper</html>"
        (self.folder / "LICENSE.txt").write_bytes(data)
        lock = {"license_archive": "LICENSE.txt", "license_archive_sha256": hashlib.sha256(data).hexdigest()}
        with self.assertRaisesRegex(ValueError, "reviewed V14"):
            archived_license(lock, self.folder)

    def test_archive_bytes_are_stable_under_windows_checkout_filters(self):
        lock = json.loads((ROOT / "packaging/msvc-runtime-lock.json").read_text(encoding="utf-8"))
        relative = lock["license_archive"]
        before = subprocess.check_output(["git", "hash-object", "--no-filters", relative], cwd=ROOT)
        windows = subprocess.check_output(["git", "-c", "core.autocrlf=true", "hash-object", "--path=" + relative, relative], cwd=ROOT)
        self.assertEqual(before, windows)


if __name__ == "__main__":
    unittest.main()
