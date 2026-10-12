"""업데이트 메타데이터·해시·교체 트랜잭션. 네트워크와 OCR API 를 실제로 호출하지 않는다."""
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
from urllib.error import HTTPError
from urllib.request import Request

from yeone_ocr import updater as U


PAYLOAD = b"MZ-test-release-executable"


def info(payload=PAYLOAD, version="0.8.0"):
    return U.UpdateInfo(version, f"{U.RELEASE_ROOT}/tag/v{version}", "release notes",
                        f"{U.RELEASE_ROOT}/download/v{version}/{U.ASSET_NAME}", hashlib.sha256(payload).hexdigest(), len(payload))


class Response(io.BytesIO):
    def __init__(self, data, url=U.API_URL, length=None):
        super().__init__(data)
        self.url = url
        self.headers = {"Content-Length": str(len(data) if length is None else length)}

    def geturl(self):
        return self.url


def release_data(version="0.8.0"):
    update = info(version=version)
    manifest = {"schema_version": 1, "version": version, "asset_name": U.ASSET_NAME,
                "sha256": update.sha256, "size": update.size, "repository": U.REPOSITORY}
    release = {"tag_name": "v" + version, "draft": False, "prerelease": False, "body": "changes", "assets": [
        {"name": U.ASSET_NAME, "browser_download_url": update.exe_url, "size": update.size, "digest": "sha256:" + update.sha256},
        {"name": U.MANIFEST_NAME, "browser_download_url": f"{U.RELEASE_ROOT}/download/v{version}/{U.MANIFEST_NAME}", "size": 500},
    ]}
    return release, manifest


def metadata(release, manifest):
    return mock.patch.object(U, "_open_url", side_effect=[Response(json.dumps(release).encode()), Response(json.dumps(manifest).encode())])


class ReleaseChecks(unittest.TestCase):
    def test_newer_stable_release(self):
        release, manifest = release_data()
        with metadata(release, manifest):
            result = U.check_update("0.7.0")
        self.assertEqual((result.version, result.sha256, result.size), ("0.8.0", info().sha256, len(PAYLOAD)))
        self.assertEqual(result.url, info().url)

    def test_equal_older_prerelease_draft_and_empty_repository_are_not_updates(self):
        for version, flags in (("0.7.0", {}), ("0.6.9", {}), ("0.8.0", {"prerelease": True}), ("0.8.0", {"draft": True})):
            with self.subTest(version=version, flags=flags):
                release, manifest = release_data(version); release.update(flags)
                with metadata(release, manifest) as get:
                    self.assertIsNone(U.check_update("0.7.0"))
                    self.assertEqual(get.call_count, 1)
        with mock.patch.object(U, "_open_url", side_effect=HTTPError(U.API_URL, 404, "not found", {}, None)):
            self.assertIsNone(U.check_update("0.7.0"))

    def test_version_comparison_is_numeric_and_rejects_nonstable_tag(self):
        release, manifest = release_data("0.10.0")
        with metadata(release, manifest):
            self.assertEqual(U.check_update("0.9.0").version, "0.10.0")
        release["tag_name"] = "v0.10.0-rc1"
        with metadata(release, manifest), self.assertRaises(U.UpdateError):
            U.check_update("0.9.0")

    def test_repository_asset_manifest_and_digest_must_agree(self):
        changes = [
            lambda r, m: r["assets"][0].update(browser_download_url="https://github.com/other/repo/releases/download/v0.8.0/evil.exe"),
            lambda r, m: r["assets"][1].update(browser_download_url="http://github.com/" + U.REPOSITORY + "/releases/download/v0.8.0/update.json"),
            lambda r, m: r["assets"].append(dict(r["assets"][0])),
            lambda r, m: m.update(repository="other/repo"),
            lambda r, m: m.update(version="0.9.0"),
            lambda r, m: m.update(asset_name="../another.exe"),
            lambda r, m: m.update(sha256="0" * 63),
            lambda r, m: m.update(schema_version=True),
            lambda r, m: m.update(size=True),
            lambda r, m: m.update(size=U.MAX_EXE_BYTES + 1),
            lambda r, m: r["assets"][0].update(size=len(PAYLOAD) + 1),
            lambda r, m: r["assets"][0].update(digest="sha256:" + "0" * 64),
        ]
        for number, change in enumerate(changes):
            with self.subTest(case=number):
                release, manifest = release_data(); change(release, manifest)
                with metadata(release, manifest), self.assertRaises(U.UpdateError):
                    U.check_update("0.7.0")

    def test_response_size_limit_applies_with_or_without_content_length(self):
        for has_header in (True, False):
            response = Response(b"x" * 101)
            if not has_header:
                response.headers.clear()
            with mock.patch.object(U, "_open_url", return_value=response), self.assertRaises(U.UpdateError):
                U._read_json(U.API_URL, 100)

    def test_redirects_allow_only_https_github_asset_cdn(self):
        request = Request(info().exe_url)
        redirects = U._PinnedRedirect(info().exe_url)
        for url in ("http://release-assets.githubusercontent.com/file", "https://evil.example/file",
                    "https://github.com/other/repo/file", "https://release-assets.githubusercontent.com.evil.example/file",
                    "https://user:secret@release-assets.githubusercontent.com/file", "https://release-assets.githubusercontent.com:444/file"):
            with self.subTest(url=url), self.assertRaises(U.UpdateError):
                redirects.redirect_request(request, None, 302, "redirect", {}, url)
        result = redirects.redirect_request(request, None, 302, "redirect", {}, "https://release-assets.githubusercontent.com/file?sig=test")
        self.assertEqual(result.host, "release-assets.githubusercontent.com")
        with self.assertRaises(U.UpdateError):
            U._PinnedRedirect(U.API_URL).redirect_request(Request(U.API_URL), None, 302, "redirect", {}, "https://release-assets.githubusercontent.com/file")
        with mock.patch.object(U, "build_opener") as opener, self.assertRaises(U.UpdateError):
            U._open_url("https://evil.example/update.json")
        opener.assert_not_called()


class DownloadAndInstall(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.override = mock.patch.object(U, "UPDATE_DIR_OVERRIDE", self.root / "updates")
        self.override.start(); self.addCleanup(self.override.stop)

    def download(self, payload=PAYLOAD):
        with mock.patch.object(U, "_open_url", return_value=Response(payload, info(payload).exe_url)):
            return U.download_update(info(payload))

    def test_download_verifies_size_hash_and_reuses_verified_cache(self):
        path = self.download()
        self.assertEqual(path.read_bytes(), PAYLOAD)
        receipt = json.loads(path.with_name("verified.json").read_text(encoding="utf-8"))
        self.assertEqual((receipt["sha256"], receipt["repository"]), (info().sha256, U.REPOSITORY))
        with mock.patch.object(U, "_open_url") as get:
            self.assertEqual(U.download_update(info()), path)
            get.assert_not_called()

    def test_bad_download_never_commits_or_leaves_partial_file(self):
        for body, length in ((b"x" * len(PAYLOAD), len(PAYLOAD)), (PAYLOAD[:-1], len(PAYLOAD)), (PAYLOAD + b"extra", len(PAYLOAD))):
            with self.subTest(length=len(body)), mock.patch.object(U, "_open_url", return_value=Response(body, info().exe_url, length)), self.assertRaises(U.UpdateError):
                U.download_update(info())
            self.assertFalse(any(self.root.rglob("*.exe")))
            self.assertFalse(any(self.root.rglob("*.part")))
            self.assertFalse(any(self.root.rglob("verified.json")))

    def test_arbitrary_download_url_and_version_path_are_rejected_before_write(self):
        for update in (U.UpdateInfo("../outside", info().url, "", info().exe_url, info().sha256, info().size),
                       U.UpdateInfo(info().version, info().url, "", "https://evil.example/file.exe", info().sha256, info().size)):
            with mock.patch.object(U, "_open_url") as get, self.assertRaises(U.UpdateError):
                U.download_update(update)
            get.assert_not_called()
        self.assertFalse((self.root / "updates").exists())

    def frozen(self, target):
        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch.object(sys, "frozen", True, create=True))
        stack.enter_context(mock.patch.object(sys, "platform", "win32"))
        stack.enter_context(mock.patch.object(sys, "executable", str(target)))
        stack.enter_context(mock.patch.object(U, "_process_started", return_value="123456"))
        stack.enter_context(mock.patch.object(subprocess, "CREATE_NO_WINDOW", 0x08000000, create=True))
        stack.enter_context(mock.patch.object(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200, create=True))
        return stack

    def test_source_install_and_modified_verified_download_are_rejected(self):
        source = self.download(); target = self.root / "YeonE_OCR.exe"; target.write_bytes(b"old app")
        with mock.patch.object(sys, "frozen", False, create=True), self.assertRaises(U.UpdateError):
            U.install_update(source, target, os.getpid())
        source.write_bytes(b"tampered")
        with self.frozen(target), mock.patch.object(U.subprocess, "Popen") as launch, self.assertRaises(U.UpdateError):
            U.install_update(source, target, os.getpid())
        launch.assert_not_called()
        self.assertEqual(target.read_bytes(), b"old app")

    def test_helper_launch_uses_json_paths_hidden_process_and_clean_environment(self):
        source = self.download(); folder = self.root / "app [test] ' $special"; folder.mkdir()
        target = folder / "YeonE_OCR.exe"; target.write_bytes(b"old app")
        seen = {}
        helper = mock.Mock(); helper.poll.return_value = None

        def launch(command, **kwargs):
            config = Path(command[command.index("-ConfigPath") + 1])
            seen.update(command=command, kwargs=kwargs, data=json.loads(config.read_text(encoding="utf-8")), script=config.with_name("install.ps1").read_text(encoding="utf-8-sig"))
            config.with_name("ready").write_text("ready", encoding="ascii")
            return helper

        with self.frozen(target), mock.patch.object(U.subprocess, "Popen", side_effect=launch), mock.patch.dict(os.environ, {"GEMINI_API_KEY": "secret", "GOOGLE_API_KEY": "secret"}):
            U.install_update(source, target, os.getpid())
        self.assertEqual(target.read_bytes(), b"old app")
        self.assertEqual(seen["data"]["target"], str(target.resolve()))
        self.assertNotIn(str(target), seen["script"])
        self.assertEqual(seen["command"][seen["command"].index("-WindowStyle") + 1], "Hidden")
        self.assertTrue(seen["kwargs"]["creationflags"] & 0x08000000)
        self.assertEqual(Path(seen["kwargs"]["cwd"]), source.parent.parent,
                         "The helper must not lock its own job directory as its working directory")
        self.assertEqual(seen["kwargs"]["env"]["PYINSTALLER_RESET_ENVIRONMENT"], "1")
        self.assertNotIn("GEMINI_API_KEY", seen["kwargs"]["env"])
        self.assertNotIn("GOOGLE_API_KEY", seen["kwargs"]["env"])
        helper.terminate.assert_not_called()

    def test_failed_helper_handshake_leaves_original_and_cleans_staging(self):
        source = self.download(); target = self.root / "YeonE_OCR.exe"; target.write_bytes(b"old app")
        helper = mock.Mock(); helper.poll.return_value = None
        with self.frozen(target), mock.patch.object(U.subprocess, "Popen", return_value=helper), mock.patch.object(U, "HELPER_READY_TIMEOUT", 0), self.assertRaises(U.UpdateError):
            U.install_update(source, target, os.getpid())
        helper.terminate.assert_called_once()
        self.assertEqual(target.read_bytes(), b"old app")
        self.assertFalse(list(self.root.glob(".YeonE_OCR-update-*.exe")))


@unittest.skipUnless(sys.platform == "win32", "Windows PowerShell transaction")
class WindowsTransaction(unittest.TestCase):
    def run_helper(self, rollback=False, delayed_failure=False):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); app = root / "app [literal] 'quoted' $text"; app.mkdir()
            job = root / "install-test"; job.mkdir()
            target = app / "YeonE_OCR.exe"
            system = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32"
            shutil.copyfile(system / ("whoami.exe" if rollback else "cmd.exe"), target)
            original = target.read_bytes()
            staged = app / (".YeonE_OCR-update-" + "1" * 32 + ".exe")
            if rollback and not delayed_failure:
                staged.write_bytes(b"MZ-invalid-program")
            else:
                # Actual harmless native executable, including startup readiness; no Python shell tricks.
                compiler = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "Microsoft.NET" / "Framework64" / "v4.0.30319" / "csc.exe"
                if not compiler.exists():
                    self.skipTest(".NET Framework C# fixture compiler unavailable")
                fixture = root / "fixture.cs"
                fixture.write_text('''using System;
using System.IO;
using System.Threading;
class Fixture { static int Main() {
''' + ('''Thread.Sleep(3500); return 1;
''' if delayed_failure else '''var path = Environment.GetEnvironmentVariable("YEONE_UPDATE_READY_FILE");
var token = Environment.GetEnvironmentVariable("YEONE_UPDATE_READY_TOKEN");
if (String.IsNullOrEmpty(path) || String.IsNullOrEmpty(token)) return 2;
File.WriteAllText(path + ".tmp", "{\\"token\\":\\"" + token + "\\"}");
File.Move(path + ".tmp", path); return 0;
''') + "} }", encoding="utf-8")
                compiled = subprocess.run([str(compiler), "/nologo", "/target:winexe", "/out:" + str(staged), str(fixture)],
                                          capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
                self.assertEqual(compiled.returncode, 0, compiled.stdout.decode(errors="replace") + compiled.stderr.decode(errors="replace"))
            expected = staged.read_bytes()
            script = job / "install.ps1"; script.write_text(U._HELPER_SCRIPT, encoding="utf-8-sig")
            config = job / "install.json"
            parent = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                      stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)
            helper = None
            try:
                config.write_text(json.dumps({"schema_version": 1, "target": str(target), "staged": str(staged),
                    "backup": str(app / (".YeonE_OCR-backup-" + "1" * 32 + ".exe")), "parent_pid": parent.pid,
                    "parent_started": U._process_started(parent.pid), "sha256": hashlib.sha256(expected).hexdigest(), "size": len(expected),
                    "original_sha256": hashlib.sha256(original).hexdigest(), "ready_token": "c" * 64}), encoding="utf-8")
                helper = subprocess.Popen([str(U._powershell()), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden", "-File", str(script), "-ConfigPath", str(config)],
                                          stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)
                deadline = time.monotonic() + 15
                while not (job / "ready").exists() and helper.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.05)
                error = (job / "error.txt").read_text(encoding="utf-8-sig") if (job / "error.txt").exists() else ""
                self.assertTrue((job / "ready").exists(), error)
                self.assertIsNone(parent.poll())
                self.assertEqual(target.read_bytes(), original, "Never replace an executable before its process exits")
                parent.terminate(); parent.wait(timeout=10)
                code = helper.wait(timeout=20)
                error = (job / "error.txt").read_text(encoding="utf-8-sig") if (job / "error.txt").exists() else ""
                self.assertEqual(code, 1 if rollback else 0, error)
                self.assertEqual(hashlib.sha256(target.read_bytes()).hexdigest(), hashlib.sha256(original if rollback else expected).hexdigest(), error)
                self.assertFalse(staged.exists())
                self.assertFalse(list(app.glob(".YeonE_OCR-backup-*.exe")))
                if not rollback:
                    self.assertFalse(job.exists())
            finally:
                for process in (parent, helper):
                    if process is not None and process.poll() is None:
                        process.terminate(); process.wait(timeout=10)
                # The rollback intentionally restarts the old whoami fixture asynchronously.
                # Wait for that exact temporary executable's image lock to disappear before cleanup.
                deadline = time.monotonic() + 5
                while target.exists():
                    try:
                        target.unlink()
                    except PermissionError:
                        if time.monotonic() >= deadline:
                            raise
                        time.sleep(0.05)

    def test_helper_waits_for_actual_process_then_replaces_and_restarts(self):
        self.run_helper()

    def test_failed_new_executable_rolls_back_to_original(self):
        self.run_helper(rollback=True)

    def test_exit_after_three_seconds_without_ready_rolls_back(self):
        self.run_helper(rollback=True, delayed_failure=True)


if __name__ == "__main__":
    unittest.main()
