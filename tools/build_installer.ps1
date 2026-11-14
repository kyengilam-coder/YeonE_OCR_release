# Build the Windows installer on one PC: prepare the build environment if needed, then run
# tools\build_windows.ps1 with the newest PP-OCRv6 bundle. Same environment as the release
# workflow (.github/workflows/windows-release.yml):
#   - Python 3.14 64-bit (python.org; the release workflow pins 3.14.5)  -> must already be installed
#   - Inno Setup 6 (ISCC.exe)                                           -> must already be installed
#   - work\windows-release\venv with packaging\requirements-windows.lock.txt (created/updated here)
# This script installs no system software. It downloads the pinned Python packages into the venv.
#
#   powershell -ExecutionPolicy Bypass -File tools\build_installer.ps1
#   ... -Bundle <folder>         PP-OCRv6 bundle (default: newest work\ppocrv6-bundle\bundle-*)
#   ... -Python314 <python.exe>  base Python 3.14 (default: py -3.14)
#   ... -InnoCompiler <ISCC.exe> (default: the usual Inno Setup 6 install folders)
# Output: dist\YeonE_OCR-<version>-Setup-x64.exe  -  log: work\windows-release\installer-build-<time>.log
param(
    [string]$Bundle = '',
    [string]$Python314 = '',
    [string]$InnoCompiler = ''
)
$ErrorActionPreference = 'Stop'
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$Work = Join-Path $RepoRoot 'work\windows-release'
New-Item -ItemType Directory -Force -Path $Work | Out-Null
$Stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$Log = Join-Path $Work "installer-build-$Stamp.log"
Start-Transcript -LiteralPath $Log | Out-Null
$env:PYTHONUTF8 = '1'; $env:PYTHONIOENCODING = 'utf-8'; $env:PYTHONNOUSERSITE = '1'
Remove-Item Env:PYTHONPATH, Env:PYTHONHOME -ErrorAction SilentlyContinue

function Invoke-Checked([string]$Program, [string[]]$Arguments, [string]$What) {
    Write-Host "`n== $What" -ForegroundColor Cyan
    # Windows PowerShell 5.1: stream native stdout/stderr as text (stderr would be fatal under 'Stop').
    $previous = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
    try {
        & $Program @Arguments 2>&1 | ForEach-Object { Write-Host "$_" }
        $code = $LASTEXITCODE
    } finally { $ErrorActionPreference = $previous }
    if ($code -ne 0) { throw "$What failed (exit $code)" }
}

function Get-Output([string]$Program, [string[]]$Arguments) {
    $previous = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
    try {
        $text = & $Program @Arguments 2>$null
        if ($LASTEXITCODE -ne 0) { return '' }
        return ("$text").Trim()
    } catch { return '' } finally { $ErrorActionPreference = $previous }
}

$VersionProbe = "import struct, sys; print('%d.%d.%d %d' % (*sys.version_info[:3], struct.calcsize('P') * 8))"

function Find-Python314 {
    if ($Python314) {
        $info = Get-Output $Python314 @('-c', $VersionProbe)
        if ($info -notmatch '^3\.14\.\d+ 64$') { throw "-Python314 is not 64-bit Python 3.14 ($info): $Python314" }
        return (Resolve-Path -LiteralPath $Python314).Path
    }
    if (Get-Command py -ErrorAction SilentlyContinue) {
        $found = Get-Output 'py' @('-3.14', '-c', "import struct, sys; print(sys.executable if struct.calcsize('P') == 8 else '')")
        if ($found) { return $found }
    }
    return $null
}

function Find-Inno {
    if ($InnoCompiler) { return (Resolve-Path -LiteralPath $InnoCompiler).Path }
    $candidates = @((Join-Path ${env:ProgramFiles(x86)} 'Inno Setup 6\ISCC.exe'),
                    (Join-Path $env:ProgramFiles 'Inno Setup 6\ISCC.exe'),
                    (Join-Path $env:LOCALAPPDATA 'Programs\Inno Setup 6\ISCC.exe'),
                    (Join-Path $Work 'toolchain\InnoSetup\ISCC.exe'))
    return ($candidates | Where-Object { $_ -and (Test-Path -LiteralPath $_ -PathType Leaf) } | Select-Object -First 1)
}

try {
    Write-Host "YeonE_OCR installer build - $Stamp"

    # 1. Check what must already be installed, before any long step.
    $BasePython = Find-Python314
    $Inno = Find-Inno
    $missing = @()
    if (-not $BasePython) {
        $missing += ("Python 3.14 (64-bit) - https://www.python.org/downloads/windows/  " +
                     "(choose 'Windows installer (64-bit)' of 3.14.x; keep 'py launcher' checked)")
    }
    if (-not $Inno) {
        $missing += "Inno Setup 6 - https://jrsoftware.org/isdl.php  (innosetup-6.x.x.exe, default options)"
    }
    if ($missing) {
        Write-Host "`nInstall these first, then run this script again:" -ForegroundColor Yellow
        $missing | ForEach-Object { Write-Host "  - $_" -ForegroundColor Yellow }
        throw 'Build tools are missing (nothing was changed).'
    }
    Write-Host "Base Python: $BasePython ($(Get-Output $BasePython @('-c', $VersionProbe)))"
    Write-Host "Inno Setup:  $Inno"

    if (-not $Bundle) {
        $latest = Get-ChildItem -LiteralPath (Join-Path $RepoRoot 'work\ppocrv6-bundle') -Directory -Filter 'bundle-*' -ErrorAction SilentlyContinue |
            Where-Object { Test-Path -LiteralPath (Join-Path $_.FullName 'bundle-manifest.json') } |
            Sort-Object Name -Descending | Select-Object -First 1
        if (-not $latest) { throw 'No PP-OCRv6 bundle found. Run tools\build_ppocrv6_bundle.bat first, or pass -Bundle <folder>.' }
        $Bundle = $latest.FullName
    }
    $Bundle = (Resolve-Path -LiteralPath $Bundle).Path
    Write-Host "PP-OCRv6 bundle: $Bundle"

    # 2. Build venv (created once; re-runs only re-check the pinned packages).
    $Venv = Join-Path $Work 'venv'
    $VenvPython = Join-Path $Venv 'Scripts\python.exe'
    if (Test-Path -LiteralPath $VenvPython) {
        $info = Get-Output $VenvPython @('-c', $VersionProbe)
        if ($info -notmatch '^3\.14\.\d+ 64$') {
            throw "Existing $Venv is not a 64-bit Python 3.14 venv ($info). Delete that folder and run again."
        }
    } else {
        Invoke-Checked $BasePython @('-m', 'venv', $Venv) 'Create build venv'
    }
    $Wheels = Join-Path $Work 'wheels'
    $env:PYYAML_FORCE_LIBYAML = '0'
    Invoke-Checked $VenvPython @('-m', 'pip', 'wheel', 'PyYAML==6.0.1', '--wheel-dir', $Wheels, '--disable-pip-version-check') 'Build PyYAML wheel (pure Python, as in the release workflow)'
    Invoke-Checked $VenvPython @('-m', 'pip', 'install', '--disable-pip-version-check', '--find-links', $Wheels,
                                 '-r', (Join-Path $RepoRoot 'packaging\requirements-windows.lock.txt')) 'Install pinned build packages'

    # 3. Build, smoke-test and package (tools\build_windows.ps1 checks everything it builds).
    Write-Host "`n== Build application and installer (PyInstaller + smoke tests + Inno Setup; several minutes)" -ForegroundColor Cyan
    & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $RepoRoot 'tools\build_windows.ps1') `
        -Python $VenvPython -InnoCompiler $Inno -PPOCRv6Bundle $Bundle
    if ($LASTEXITCODE -ne 0) { throw "tools\build_windows.ps1 failed (exit $LASTEXITCODE)" }

    $result = Get-Content -LiteralPath (Join-Path $Work 'build-result.json') -Raw -Encoding UTF8 | ConvertFrom-Json
    $installer = Join-Path $RepoRoot ("dist\YeonE_OCR-{0}-Setup-x64.exe" -f $result.version)
    $hash = (Get-FileHash -LiteralPath $installer -Algorithm SHA256).Hash.ToLowerInvariant()
    Write-Host ("`nDone: {0}`n  {1:N0} MB - SHA-256 {2}`n  PP-OCRv6 bundle manifest {3}" -f $installer,
                ((Get-Item -LiteralPath $installer).Length / 1MB), $hash, $result.ppocrv6_bundle_sha256) -ForegroundColor Green
    Write-Host 'Next: install it and follow the checklist at the top of docs\windows-release.md.'
    $exit = 0
} catch {
    Write-Host "`nFAILED: $($_.Exception.Message)" -ForegroundColor Red
    $exit = 1
} finally {
    Write-Host "Log: $Log"
    Stop-Transcript | Out-Null
}
exit $exit
