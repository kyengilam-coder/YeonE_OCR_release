"""Windows 실행 파일 업데이트. 공개 릴리스의 해시를 확인한 뒤, 현재 프로세스 종료 후 교체한다.

소스 실행에서도 버전 확인·다운로드는 가능하다. 설치는 frozen Windows 실행 파일만 지원한다.
다운로드·보관 파일은 LOCALAPPDATA/YeonE_OCR/updates 에만 두며 사용자 설정은 건드리지 않는다.
"""
from __future__ import annotations

import ctypes
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
import uuid


REPOSITORY = "kyengilam-coder/YeonE_OCR_release"
API_URL = f"https://api.github.com/repos/{REPOSITORY}/releases/latest"
RELEASE_ROOT = f"https://github.com/{REPOSITORY}/releases"
ASSET_NAME = "YeonE_OCR-Windows-x64.exe"
MANIFEST_NAME = "update.json"
MAX_API_BYTES = 1024 * 1024
MAX_MANIFEST_BYTES = 64 * 1024
MAX_EXE_BYTES = 512 * 1024 * 1024
NETWORK_TIMEOUT = 30
HELPER_READY_TIMEOUT = 20
UPDATE_DIR_OVERRIDE: Path | None = None       # 테스트에서만 별도 폴더를 지정한다.
_VERSION = re.compile(r"v?(0|[1-9]\d{0,8})\.(0|[1-9]\d{0,8})\.(0|[1-9]\d{0,8})\Z")
_HASH = re.compile(r"[0-9a-f]{64}\Z")


class UpdateError(RuntimeError):
    """업데이트 확인·검증·설치를 안전하게 마치지 못했다."""


@dataclass(frozen=True)
class UpdateInfo:
    version: str
    url: str
    notes: str
    exe_url: str
    sha256: str
    size: int


def _version(value: str) -> tuple[int, int, int]:
    match = _VERSION.fullmatch(value) if isinstance(value, str) else None
    if not match:
        raise UpdateError("정식 버전 번호가 올바르지 않습니다(예: 0.7.0).")
    return tuple(int(part) for part in match.groups())


def _canonical_version(value: str) -> str:
    return ".".join(map(str, _version(value)))


def _valid_size(size, limit: int = MAX_EXE_BYTES) -> int:
    if type(size) is not int or not 0 < size <= limit:
        raise UpdateError("업데이트 파일 크기가 허용 범위를 벗어났습니다.")
    return size


def _safe_url(url: str, *, initial: str) -> None:
    """최초 주소는 고정 주소, 리디렉션은 GitHub 릴리스 CDN 에만 허용한다."""
    if not isinstance(url, str) or any(ord(c) < 33 or ord(c) == 127 for c in url):
        raise UpdateError("업데이트 다운로드 주소가 올바르지 않습니다.")
    parsed = urlsplit(url)
    try:
        valid_port = parsed.port in (None, 443)
    except ValueError:
        valid_port = False
    if parsed.scheme != "https" or parsed.username or parsed.password or parsed.fragment or not valid_port:
        raise UpdateError("업데이트는 신뢰한 HTTPS 주소에서만 받을 수 있습니다.")
    if url == initial:
        return
    if initial == API_URL or parsed.hostname not in ("release-assets.githubusercontent.com", "objects.githubusercontent.com"):
        raise UpdateError("다른 저장소·서버로 이동하는 업데이트 주소를 거부했습니다.")


class _PinnedRedirect(HTTPRedirectHandler):
    def __init__(self, initial: str):
        super().__init__()
        self.initial = initial

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _safe_url(newurl, initial=self.initial)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _open_url(url: str):
    if url != API_URL:
        prefix = RELEASE_ROOT + "/download/"
        remainder = url[len(prefix):] if url.startswith(prefix) else ""
        parts = remainder.split("/")
        if len(parts) != 2 or not _VERSION.fullmatch(parts[0]) or parts[1] not in (ASSET_NAME, MANIFEST_NAME):
            raise UpdateError("지정된 GitHub 저장소의 릴리스 파일만 받을 수 있습니다.")
    _safe_url(url, initial=url)
    request = Request(url, headers={"User-Agent": "YeonE-OCR-Updater", "Accept": "application/vnd.github+json" if url == API_URL else "application/octet-stream",
                                    "X-GitHub-Api-Version": "2022-11-28", "Accept-Encoding": "identity"})
    response = build_opener(_PinnedRedirect(url)).open(request, timeout=NETWORK_TIMEOUT)
    try:
        _safe_url(response.geturl(), initial=url)
    except Exception:
        response.close()
        raise
    return response


def _check_length(response, maximum: int) -> None:
    value = response.headers.get("Content-Length")
    if value is not None:
        try:
            size = int(value)
        except (ValueError, TypeError) as exc:
            raise UpdateError("서버의 파일 크기 정보가 올바르지 않습니다.") from exc
        if size < 0 or size > maximum:
            raise UpdateError("서버가 허용 크기보다 큰 업데이트 자료를 보냈습니다.")


def _read_json(url: str, maximum: int) -> dict:
    try:
        with _open_url(url) as response:
            _check_length(response, maximum)
            raw = response.read(maximum + 1)
        if len(raw) > maximum:
            raise UpdateError("업데이트 자료가 허용 크기를 넘었습니다.")
        result = json.loads(raw.decode("utf-8-sig"))
        if not isinstance(result, dict):
            raise ValueError("JSON object required")
        return result
    except (UnicodeError, ValueError) as exc:
        raise UpdateError("업데이트 정보(JSON)를 읽을 수 없습니다.") from exc


def _asset(release: dict, name: str, tag: str) -> dict:
    assets = release.get("assets")
    if not isinstance(assets, list):
        raise UpdateError("릴리스에 업데이트 파일 목록이 없습니다.")
    matches = [a for a in assets if isinstance(a, dict) and a.get("name") == name]
    if len(matches) != 1:
        raise UpdateError(f"릴리스에 {name} 파일이 없거나 중복되었습니다.")
    asset = matches[0]
    expected = f"{RELEASE_ROOT}/download/{tag}/{name}"
    if asset.get("browser_download_url") != expected:
        raise UpdateError("다른 저장소의 업데이트 파일 주소를 거부했습니다.")
    if asset.get("state", "uploaded") != "uploaded":
        raise UpdateError("업데이트 파일 업로드가 아직 끝나지 않았습니다.")
    return asset


def _validate_info(info: UpdateInfo) -> str:
    version = _canonical_version(info.version)
    prefix = RELEASE_ROOT + "/tag/"
    if not isinstance(info.url, str) or not info.url.startswith(prefix):
        raise UpdateError("업데이트 릴리스 주소가 올바르지 않습니다.")
    tag = info.url[len(prefix):]
    if _canonical_version(tag) != version or info.exe_url != f"{RELEASE_ROOT}/download/{tag}/{ASSET_NAME}":
        raise UpdateError("업데이트 버전과 다운로드 주소가 맞지 않습니다.")
    if not isinstance(info.sha256, str) or not _HASH.fullmatch(info.sha256):
        raise UpdateError("업데이트 SHA-256 정보가 올바르지 않습니다.")
    _valid_size(info.size)
    return version


def check_update(current_version: str) -> UpdateInfo | None:
    """고정 공개 저장소의 최신 정식판만 확인한다. 같은·이전 버전과 미공개판은 설치하지 않는다."""
    current = _version(current_version)
    try:
        try:
            release = _read_json(API_URL, MAX_API_BYTES)
        except HTTPError as exc:
            if exc.code == 404:
                return None
            raise
        if release.get("draft") is not False or release.get("prerelease") is not False:
            return None
        tag = release.get("tag_name")
        if _version(tag) <= current:
            return None
        version = _canonical_version(tag)
        exe = _asset(release, ASSET_NAME, tag)
        manifest_asset = _asset(release, MANIFEST_NAME, tag)
        _valid_size(manifest_asset.get("size"), MAX_MANIFEST_BYTES)
        manifest = _read_json(manifest_asset["browser_download_url"], MAX_MANIFEST_BYTES)
        if type(manifest.get("schema_version")) is not int or manifest["schema_version"] != 1:
            raise UpdateError("지원하지 않는 업데이트 정보 형식입니다.")
        if manifest.get("version") != version or manifest.get("asset_name") != ASSET_NAME:
            raise UpdateError("릴리스와 업데이트 파일 정보가 일치하지 않습니다.")
        for key in ("repository", "repo"):
            if key in manifest and manifest[key] != REPOSITORY:
                raise UpdateError("업데이트 정보에 다른 저장소가 지정되어 있습니다.")
        info = UpdateInfo(version=version, url=f"{RELEASE_ROOT}/tag/{tag}", notes=str(release.get("body") or "")[:32000],
                          exe_url=exe["browser_download_url"], sha256=manifest.get("sha256"), size=manifest.get("size"))
        _validate_info(info)
        if _valid_size(exe.get("size")) != info.size:
            raise UpdateError("릴리스와 업데이트 파일 크기가 일치하지 않습니다.")
        if exe.get("digest") not in (None, "sha256:" + info.sha256):
            raise UpdateError("릴리스와 업데이트 파일 해시가 일치하지 않습니다.")
        return info
    except UpdateError:
        raise
    except (OSError, URLError, HTTPError) as exc:
        raise UpdateError("업데이트 서버에 연결하지 못했습니다. 인터넷 연결을 확인하고 다시 시도하십시오.") from exc


def _update_dir() -> Path:
    if UPDATE_DIR_OVERRIDE is not None:
        return Path(UPDATE_DIR_OVERRIDE).resolve()
    local = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
    return (local / "YeonE_OCR" / "updates").resolve()


def _hash_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            size += len(chunk)
            if size > MAX_EXE_BYTES:
                raise UpdateError("실행 파일이 허용 크기를 넘었습니다.")
            digest.update(chunk)
    return digest.hexdigest(), size


def _verify_file(path: Path, sha256: str, size: int) -> bool:
    return path.is_file() and not path.is_symlink() and _hash_file(path) == (sha256, size)


def _write_json(path: Path, data: dict) -> None:
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        temporary.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def download_update(info: UpdateInfo) -> Path:
    """정확한 크기와 SHA-256 을 확인한 파일만 실행 파일 이름으로 저장한다."""
    version = _validate_info(info)
    folder = _update_dir() / version
    path = folder / ASSET_NAME
    temporary = None
    try:
        folder.mkdir(parents=True, exist_ok=True)
        if not _verify_file(path, info.sha256, info.size):
            digest = hashlib.sha256(); size = 0
            with _open_url(info.exe_url) as response:
                _check_length(response, info.size)
                with tempfile.NamedTemporaryFile(prefix="download-", suffix=".part", dir=folder, delete=False) as stream:
                    temporary = Path(stream.name)
                    while chunk := response.read(min(1024 * 1024, info.size - size + 1)):
                        size += len(chunk)
                        if size > info.size:
                            raise UpdateError("받은 실행 파일이 예고한 크기를 넘었습니다.")
                        stream.write(chunk); digest.update(chunk)
                    stream.flush(); os.fsync(stream.fileno())
            if size != info.size or digest.hexdigest() != info.sha256:
                raise UpdateError("받은 파일의 크기·SHA-256 이 맞지 않아 업데이트를 취소했습니다.")
            os.replace(temporary, path)
            temporary = None
        _write_json(folder / "verified.json", {"schema_version": 1, "repository": REPOSITORY, "version": info.version,
                                               "url": info.url, "exe_url": info.exe_url, "sha256": info.sha256, "size": info.size})
        return path
    except UpdateError:
        raise
    except (OSError, URLError, HTTPError) as exc:
        raise UpdateError("업데이트 파일을 내려받거나 저장하지 못했습니다. 기존 실행 파일은 그대로입니다.") from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


# 고정 코드와 JSON 자료를 분리한다. 경로·릴리스 내용은 PowerShell 코드에 삽입하지 않는다.
_HELPER_SCRIPT = r'''param([Parameter(Mandatory=$true)][string]$ConfigPath)
$ErrorActionPreference = 'Stop'
$configuration = $null
$guard = $null
$replaced = $false
$parentExited = $false
$success = $false
$target = $null
$backup = $null
$staged = $null
$application = $null
$preserveBackup = $false
$ready = Join-Path $PSScriptRoot 'ready'
$applicationReady = Join-Path $PSScriptRoot 'application-ready'

function File-Hash([string]$Path) {
    $algorithm = [System.Security.Cryptography.SHA256]::Create()
    $stream = $null
    try {
        $stream = [System.IO.File]::OpenRead($Path)
        return ([System.BitConverter]::ToString($algorithm.ComputeHash($stream))).Replace('-', '').ToLowerInvariant()
    } finally {
        if ($null -ne $stream) { $stream.Dispose() }
        $algorithm.Dispose()
    }
}
function Start-Application([string]$Path, [bool]$RequireReady = $false) {
    $start = New-Object System.Diagnostics.ProcessStartInfo
    $start.FileName = $Path
    $start.WorkingDirectory = [System.IO.Path]::GetDirectoryName($Path)
    $start.UseShellExecute = $false
    $start.CreateNoWindow = $true
    $start.EnvironmentVariables['PYINSTALLER_RESET_ENVIRONMENT'] = '1'
    if ($RequireReady) {
        $start.EnvironmentVariables['YEONE_UPDATE_READY_FILE'] = $applicationReady
        $start.EnvironmentVariables['YEONE_UPDATE_READY_TOKEN'] = [string]$configuration.ready_token
    } else {
        $start.EnvironmentVariables.Remove('YEONE_UPDATE_READY_FILE')
        $start.EnvironmentVariables.Remove('YEONE_UPDATE_READY_TOKEN')
    }
    return [System.Diagnostics.Process]::Start($start)
}
try {
    $configFull = [System.IO.Path]::GetFullPath($ConfigPath)
    if ([System.IO.Path]::GetDirectoryName($configFull) -ne $PSScriptRoot) { throw 'Configuration path mismatch' }
    $configuration = Get-Content -LiteralPath $configFull -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($configuration.schema_version -ne 1) { throw 'Configuration version mismatch' }
    if ([string]$configuration.ready_token -notmatch '^[0-9a-f]{64}$') { throw 'Readiness token mismatch' }
    $target = [System.IO.Path]::GetFullPath([string]$configuration.target)
    $staged = [System.IO.Path]::GetFullPath([string]$configuration.staged)
    $backup = [System.IO.Path]::GetFullPath([string]$configuration.backup)
    $targetDir = [System.IO.Path]::GetDirectoryName($target)
    if ([System.IO.Path]::GetDirectoryName($staged) -ne $targetDir -or [System.IO.Path]::GetDirectoryName($backup) -ne $targetDir) { throw 'Staging path mismatch' }
    if ([System.IO.Path]::GetFileName($staged) -notmatch '^\.YeonE_OCR-update-[0-9a-f]{32}\.exe$' -or [System.IO.Path]::GetFileName($backup) -notmatch '^\.YeonE_OCR-backup-[0-9a-f]{32}\.exe$') { throw 'Staging name mismatch' }
    if ((Get-Item -LiteralPath $target).Attributes -band [System.IO.FileAttributes]::ReparsePoint) { throw 'Target is a link' }
    if ((Get-Item -LiteralPath $staged).Attributes -band [System.IO.FileAttributes]::ReparsePoint) { throw 'Staging is a link' }
    if ((Get-Item -LiteralPath $staged).Length -ne [long]$configuration.size -or (File-Hash $staged) -ne $configuration.sha256) { throw 'Staging integrity check failed' }
    if (Test-Path -LiteralPath $backup) { throw 'Backup already exists' }
    $guardPath = $target + '.update.lock'
    $guard = [System.IO.File]::Open($guardPath, [System.IO.FileMode]::OpenOrCreate, [System.IO.FileAccess]::ReadWrite, [System.IO.FileShare]::None)
    $parent = $null
    try { $parent = [System.Diagnostics.Process]::GetProcessById([int]$configuration.parent_pid) } catch [System.ArgumentException] { }
    if ($null -ne $parent -and $parent.StartTime.ToUniversalTime().ToFileTimeUtc().ToString() -ne [string]$configuration.parent_started) {
        $parent.Dispose(); $parent = $null
    }
    Set-Content -LiteralPath $ready -Value 'ready' -Encoding ASCII
    if ($null -ne $parent) { $parent.WaitForExit(); $parent.Dispose() }
    $parentExited = $true
    if ((File-Hash $target) -ne $configuration.original_sha256) { throw 'Installed file changed while waiting' }
    for ($attempt = 0; $attempt -lt 150; $attempt++) {
        try {
            [System.IO.File]::Replace($staged, $target, $backup, $true)
            $replaced = $true
            break
        } catch [System.IO.IOException] {
            if ($attempt -eq 149 -or (Test-Path -LiteralPath $backup) -or -not (Test-Path -LiteralPath $staged)) { throw }
            Start-Sleep -Milliseconds 200
        }
    }
    $application = Start-Application $target $true
    if ($null -eq $application) { throw 'Restart did not create a process' }
    $deadline = [DateTime]::UtcNow.AddSeconds(120)
    $confirmed = $false
    while ([DateTime]::UtcNow -lt $deadline) {
        if (Test-Path -LiteralPath $applicationReady) {
            try {
                $acknowledgment = Get-Content -LiteralPath $applicationReady -Raw -Encoding UTF8 | ConvertFrom-Json
                if ([string]$acknowledgment.token -eq [string]$configuration.ready_token) { $confirmed = $true; break }
            } catch { }  # 준비 파일을 쓰는 순간과 겹치면 다음 회차에 다시 확인한다.
        }
        if ($application.HasExited) { throw 'Updated program exited before the interface was ready' }
        Start-Sleep -Milliseconds 100
    }
    if (-not $confirmed) {
        $preserveBackup = $true
        throw 'Startup readiness timed out; previous executable retained at the backup path'
    }
    $success = $true
    try { Remove-Item -LiteralPath $backup -Force } catch { }  # 종료 직후 bootloader 가 붙든 백업은 그대로 둔다.
} catch {
    $failure = $_.Exception.Message
    if ($replaced -and -not $preserveBackup -and (Test-Path -LiteralPath $backup)) {
        try {
            for ($attempt = 0; $attempt -lt 150; $attempt++) {
                try {
                    if (Test-Path -LiteralPath $target) { [System.IO.File]::Replace($backup, $target, $staged, $true) }
                    else { Move-Item -LiteralPath $backup -Destination $target }
                    break
                } catch [System.IO.IOException] {
                    if ($attempt -eq 149 -or -not (Test-Path -LiteralPath $backup)) { throw }
                    Start-Sleep -Milliseconds 200
                }
            }
            $replaced = $false
        } catch { $failure += '; rollback failed: ' + $_.Exception.Message }
    }
    if ($parentExited -and -not $replaced -and (Test-Path -LiteralPath $target)) {
        try {
            if ((File-Hash $target) -eq $configuration.original_sha256) {
                $oldApplication = Start-Application $target
                if ($null -ne $oldApplication) { $oldApplication.Dispose() }
            }
        } catch { $failure += '; old program restart failed: ' + $_.Exception.Message }
    }
    try { Set-Content -LiteralPath (Join-Path $PSScriptRoot 'error.txt') -Value $failure -Encoding UTF8 } catch { }
} finally {
    if ($null -ne $application) { $application.Dispose() }
    if ($null -ne $guard) {
        $guard.Dispose()
        try { Remove-Item -LiteralPath $guardPath -Force } catch { }
    }
    if ($null -ne $staged -and (Test-Path -LiteralPath $staged)) {
        try { Remove-Item -LiteralPath $staged -Force } catch { }
    }
    if ($success) {
        foreach ($file in @($ready, $applicationReady, $ConfigPath, $PSCommandPath)) {
            try { Remove-Item -LiteralPath $file -Force } catch { }
        }
        try { Remove-Item -LiteralPath $PSScriptRoot } catch { }
    }
}
if ($success) { exit 0 } else { exit 1 }
'''


def _process_started(pid: int) -> str:
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    kernel.GetProcessTimes.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x1000, False, pid)   # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        raise UpdateError("현재 프로그램의 시작 시각을 확인하지 못했습니다.")
    try:
        created, exited, kernel_time, user_time = (wintypes.FILETIME() for _ in range(4))
        if not kernel.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited), ctypes.byref(kernel_time), ctypes.byref(user_time)):
            raise UpdateError("현재 프로그램의 실행 정보를 확인하지 못했습니다.")
        return str((created.dwHighDateTime << 32) | created.dwLowDateTime)
    finally:
        kernel.CloseHandle(handle)


def _powershell() -> Path:
    return Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"


def install_update(downloaded: Path, target_exe: Path, current_pid: int) -> None:
    """검증된 실행 파일의 교체 도우미를 시작한다. 성공 반환 후에만 GUI 를 정상 종료해야 한다."""
    if sys.platform != "win32" or not getattr(sys, "frozen", False):
        raise UpdateError("자동 설치는 Windows 실행 파일 배포판에서만 지원합니다.")
    target = Path(target_exe).resolve()
    if target != Path(sys.executable).resolve() or target.suffix.lower() != ".exe" or current_pid != os.getpid():
        raise UpdateError("현재 실행 중인 프로그램만 업데이트할 수 있습니다.")
    if Path(downloaded).is_symlink() or Path(target_exe).is_symlink():
        raise UpdateError("바로가기·링크 파일은 자동 교체하지 않습니다.")
    source = Path(downloaded).resolve()
    root = _update_dir()
    if source.name != ASSET_NAME or source.parent.parent != root or source.is_symlink():
        raise UpdateError("검증된 업데이트 폴더의 실행 파일이 아닙니다.")
    job = None; staged = None; helper = None; handed_off = False
    try:
        receipt_path = source.parent / "verified.json"
        if receipt_path.stat().st_size > MAX_MANIFEST_BYTES:
            raise UpdateError("업데이트 검증 기록이 너무 큽니다.")
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        if not isinstance(receipt, dict) or type(receipt.get("schema_version")) is not int or receipt["schema_version"] != 1 or receipt.get("repository") != REPOSITORY:
            raise UpdateError("업데이트 검증 기록이 올바르지 않습니다.")
        info = UpdateInfo(version=receipt.get("version"), url=receipt.get("url"), notes="", exe_url=receipt.get("exe_url"),
                          sha256=receipt.get("sha256"), size=receipt.get("size"))
        if _validate_info(info) != source.parent.name or not _verify_file(source, info.sha256, info.size):
            raise UpdateError("다운로드 이후 실행 파일이 바뀌었습니다. 업데이트를 다시 받아 주십시오.")
        identifier = uuid.uuid4().hex
        staged = target.with_name(f".YeonE_OCR-update-{identifier}.exe")
        backup = target.with_name(f".YeonE_OCR-backup-{identifier}.exe")
        with source.open("rb") as inp, staged.open("xb") as out:
            shutil.copyfileobj(inp, out, length=1024 * 1024)
            out.flush(); os.fsync(out.fileno())
        if not _verify_file(staged, info.sha256, info.size):
            raise UpdateError("설치 준비 파일의 해시가 맞지 않습니다.")
        original_hash, _ = _hash_file(target)
        job = root / ("install-" + identifier); job.mkdir()
        script = job / "install.ps1"; config = job / "install.json"
        script.write_text(_HELPER_SCRIPT, encoding="utf-8-sig")
        _write_json(config, {"schema_version": 1, "target": str(target), "staged": str(staged), "backup": str(backup),
                             "parent_pid": current_pid, "parent_started": _process_started(current_pid),
                             "sha256": info.sha256, "size": info.size, "original_sha256": original_hash,
                             "ready_token": uuid.uuid4().hex + uuid.uuid4().hex})
        environment = os.environ.copy()
        for key in ("GEMINI_API_KEY", "GOOGLE_API_KEY"):
            environment.pop(key, None)
        environment["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
        helper = subprocess.Popen([str(_powershell()), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden",
                                   "-File", str(script), "-ConfigPath", str(config)],
                                  stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                  creationflags=subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP,
                                  close_fds=True, cwd=str(root), env=environment)
        deadline = time.monotonic() + HELPER_READY_TIMEOUT
        while time.monotonic() < deadline:
            if helper.poll() is not None:
                raise UpdateError("업데이트 도우미가 시작되지 않았습니다. 기존 프로그램은 계속 사용할 수 있습니다.")
            if (job / "ready").exists():
                handed_off = True
                return
            time.sleep(0.05)
        raise UpdateError("업데이트 도우미의 준비 확인 시간이 지났습니다. 프로그램을 종료하지 않고 다시 시도하십시오.")
    except UpdateError:
        raise
    except (OSError, ValueError) as exc:
        raise UpdateError("업데이트를 설치할 준비가 되지 않았습니다. 실행 파일 폴더의 쓰기 권한을 확인하십시오.") from exc
    finally:
        # 준비 확인에 성공한 도우미는 부모가 끝날 때까지 기다리며 자기 파일을 정리한다.
        if not handed_off:
            if helper is not None and helper.poll() is None:
                try:
                    helper.terminate()
                    helper.wait(timeout=5)
                except (OSError, subprocess.TimeoutExpired):
                    pass
            if staged is not None:
                staged.unlink(missing_ok=True)
            if job is not None:
                for name in ("install.ps1", "install.json", "ready"):
                    (job / name).unlink(missing_ok=True)
                try:
                    job.rmdir()
                except OSError:
                    pass                                # 실패 원인 error.txt 는 남겨 진단할 수 있게 한다.
