# Build the relocatable PP-OCRv6 bundle that the Windows installer copies to tools\ppocrv6.
# Same steps as the release workflow (.github/workflows/windows-release.yml), for one PC:
#   1. engine source: reuse tools\ppocrv6 if its venv is Python 3.13, otherwise install it
#      fresh with Python 3.13 (scripts\install_ppocrv6.py - downloads pinned packages/models)
#   2. Microsoft C++ runtime DLLs (tools\prepare_msvc_runtime.py - signed vc_redist, pinned hash)
#   3. tools\prepare_ppocrv6_bundle.py  4. tools\verify_ppocrv6_bundle.py
#   5. tools\check_ppocrv6_base_python.py (the engine manager can create venvs from it)
# If the reused source does not match the pinned lock, it falls back to a fresh install.
#
#   powershell -ExecutionPolicy Bypass -File tools\build_ppocrv6_bundle.ps1
#   ... -Python313 C:\Python313\python.exe   (base Python 3.13 x64 for a fresh install)
#   ... -Fresh                               (always install a new engine source)
# Output: work\ppocrv6-bundle\bundle-<time>  -  log: work\ppocrv6-bundle\build-<time>.log
param(
    [string]$Source = '',
    [string]$Python313 = '',
    [string]$Output = '',
    [switch]$Fresh
)
$ErrorActionPreference = 'Stop'
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$Work = Join-Path $RepoRoot 'work\ppocrv6-bundle'
New-Item -ItemType Directory -Force -Path $Work | Out-Null
$Stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
if (-not $Output) { $Output = Join-Path $Work "bundle-$Stamp" }
$Log = Join-Path $Work "build-$Stamp.log"
Start-Transcript -LiteralPath $Log | Out-Null
$env:PYTHONUTF8 = '1'; $env:PYTHONIOENCODING = 'utf-8'; $env:PYTHONNOUSERSITE = '1'
Remove-Item Env:PYTHONPATH, Env:PYTHONHOME -ErrorAction SilentlyContinue

function Invoke-Checked([string]$Python, [string[]]$Arguments, [string]$What) {
    Write-Host "`n== $What" -ForegroundColor Cyan
    # Windows PowerShell 5.1: native stderr becomes error records (fatal under 'Stop') and native
    # output is not transcribed unless it goes through the pipeline. Stream both as text instead.
    $previous = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
    try {
        & $Python -X utf8 @Arguments 2>&1 | ForEach-Object { Write-Host "$_" }
        $code = $LASTEXITCODE
    } finally { $ErrorActionPreference = $previous }
    if ($code -ne 0) { throw "$What failed (exit $code)" }
}

function Get-Output([string]$Program, [string[]]$Arguments) {
    # One line of stdout, or '' when the program is missing or fails.
    $previous = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
    try {
        $text = & $Program @Arguments 2>$null
        if ($LASTEXITCODE -ne 0) { return '' }
        return ("$text").Trim()
    } catch { return '' } finally { $ErrorActionPreference = $previous }
}

function Get-PythonVersion([string]$Python) {
    return Get-Output $Python @('-c', "import sys; print('%d.%d' % sys.version_info[:2])")
}

function Get-BasePython313 {
    if ($Python313) {
        if ((Get-PythonVersion $Python313) -ne '3.13') { throw "-Python313 is not Python 3.13: $Python313" }
        return (Resolve-Path -LiteralPath $Python313).Path
    }
    if (Get-Command py -ErrorAction SilentlyContinue) {
        $found = Get-Output 'py' @('-3.13', '-c', "import sys, struct; print(sys.executable if struct.calcsize('P') == 8 else '')")
        if ($found) { return $found }
    }
    throw ("Python 3.13 (64-bit) is needed to install the PP-OCRv6 engine source.`n" +
           "Install it from https://www.python.org/downloads/windows/ (not the Microsoft Store build), " +
           "or pass -Python313 <path to python.exe>.")
}

function New-EngineSource {
    $base = Get-BasePython313
    $root = Join-Path $Work 'engine-source'
    Invoke-Checked $base @((Join-Path $RepoRoot 'scripts\install_ppocrv6.py'), '--root', $root) 'Install PP-OCRv6 engine source (Python 3.13)'
    return (Join-Path $root 'tools\ppocrv6')
}

function Get-EnginePython([string]$Folder) {
    $python = Join-Path $Folder 'venv\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $python)) { return $null }
    if ((Get-PythonVersion $python) -ne '3.13') { return $null }
    $base = Get-Output $python @('-c', 'import sys; print(sys.base_prefix)')
    if ($base -like '*WindowsApps*') {
        Write-Host "Skipping ${Folder}: its venv uses the Microsoft Store Python, which cannot be copied." -ForegroundColor Yellow
        return $null
    }
    return $python
}

try {
    Write-Host "PP-OCRv6 bundle build - $Stamp - output $Output"
    if (Test-Path -LiteralPath $Output) { throw "Output already exists: $Output" }
    $reused = $false
    if (-not $Fresh) {
        if (-not $Source) { $Source = Join-Path $RepoRoot 'tools\ppocrv6' }
        if (Test-Path -LiteralPath (Join-Path $Source 'installation.json')) {
            if (Get-EnginePython $Source) { $reused = $true } else {
                Write-Host "Not reusing $Source (no Python 3.13 venv); installing a fresh engine source." -ForegroundColor Yellow
            }
        }
    }
    if (-not $reused) { $Source = New-EngineSource }
    $Source = (Resolve-Path -LiteralPath $Source).Path
    $EnginePython = Get-EnginePython $Source
    if (-not $EnginePython) { throw "Engine source has no usable Python 3.13 venv: $Source" }
    Write-Host "Engine source: $Source" ; Write-Host "Engine Python: $EnginePython"

    $Msvc = Join-Path $Work "msvc-runtime-$Stamp"
    Invoke-Checked $EnginePython @((Join-Path $RepoRoot 'tools\prepare_msvc_runtime.py'), '--output', $Msvc,
                                    '--cache', (Join-Path $Work 'msvc-download')) 'Extract Microsoft C++ runtime'

    $bundleArgs = @((Join-Path $RepoRoot 'tools\prepare_ppocrv6_bundle.py'), '--source', $Source, '--output', $Output, '--msvc-runtime', $Msvc)
    try {
        Invoke-Checked $EnginePython $bundleArgs 'Prepare PP-OCRv6 bundle'
    } catch {
        if (-not $reused) { throw }
        Write-Host "The existing source did not match the pinned lock ($($_.Exception.Message)); retrying with a fresh install." -ForegroundColor Yellow
        if (Test-Path -LiteralPath $Output) { Remove-Item -LiteralPath $Output -Recurse -Force }
        $Source = (Resolve-Path -LiteralPath (New-EngineSource)).Path
        $EnginePython = Get-EnginePython $Source
        if (-not $EnginePython) { throw "Fresh engine source has no usable Python 3.13 venv: $Source" }
        $bundleArgs[2] = $Source
        Invoke-Checked $EnginePython $bundleArgs 'Prepare PP-OCRv6 bundle (fresh source)'
    }
    Invoke-Checked $EnginePython @((Join-Path $RepoRoot 'tools\verify_ppocrv6_bundle.py'), $Output) 'Verify bundle inventory and hashes'
    Invoke-Checked $EnginePython @((Join-Path $RepoRoot 'tools\check_ppocrv6_base_python.py'), $Output) 'Check engine-manager base Python (venv + pip)'

    $hash = (Get-FileHash -LiteralPath (Join-Path $Output 'bundle-manifest.json') -Algorithm SHA256).Hash.ToLowerInvariant()
    $files = (Get-ChildItem -LiteralPath $Output -Recurse -File | Measure-Object -Property Length -Sum)
    Write-Host ("`nDone: {0}`n  {1} files - {2:N0} MB - bundle-manifest.json SHA-256 {3}" -f $Output, $files.Count, ($files.Sum / 1MB), $hash) -ForegroundColor Green
    Write-Host "Build the installer with: .\tools\build_windows.ps1 ... -PPOCRv6Bundle `"$Output`""
    $exit = 0
} catch {
    Write-Host "`nFAILED: $($_.Exception.Message)" -ForegroundColor Red
    $exit = 1
} finally {
    Write-Host "Log: $Log"
    Stop-Transcript | Out-Null
}
exit $exit
