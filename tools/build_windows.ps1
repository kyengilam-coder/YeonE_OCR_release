# Requires 64-bit Windows, a prepared Python venv with packaging dependencies,
# and Inno Setup 6 when producing the installer. This script installs nothing.
# Example:
# .\tools\build_windows.ps1 -Python .\work\windows-release\venv\Scripts\python.exe `
#   -InnoCompiler .\work\windows-release\toolchain\InnoSetup\ISCC.exe
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Python,
    [string]$InnoCompiler = '',
    [string]$WorkRoot = '',
    [string]$OutputDirectory = '',
    [switch]$SkipInstaller
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$RepoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
if (-not $WorkRoot) { $WorkRoot = Join-Path $RepoRoot 'work\windows-release' }
if (-not $OutputDirectory) { $OutputDirectory = Join-Path $RepoRoot 'dist' }
$WorkRoot = [IO.Path]::GetFullPath($WorkRoot)
$OutputDirectory = [IO.Path]::GetFullPath($OutputDirectory)
$Python = (Resolve-Path -LiteralPath $Python).Path
if ($WorkRoot.TrimEnd('\') -eq $RepoRoot.TrimEnd('\') -or
    $WorkRoot.TrimEnd('\') -eq [IO.Path]::GetPathRoot($WorkRoot).TrimEnd('\')) {
    throw 'WorkRoot must be a dedicated build directory, not the repository or drive root.'
}

function Assert-BuildChild([string]$Path) {
    $Resolved = [IO.Path]::GetFullPath($Path)
    $Prefix = $WorkRoot.TrimEnd('\') + '\'
    if (-not $Resolved.StartsWith($Prefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Build path is outside WorkRoot: $Resolved"
    }
    return $Resolved
}

$DistRoot = Assert-BuildChild (Join-Path $WorkRoot 'dist')
$PyInstallerWork = Assert-BuildChild (Join-Path $WorkRoot 'pyinstaller')
$AppSource = Assert-BuildChild (Join-Path $DistRoot 'YeonE_OCR')
$SmokeRoot = Assert-BuildChild (Join-Path $WorkRoot 'smoke')
# PyInstaller may replace these generated directories; all resolved targets were
# checked above. No source, user document or configuration directory is removed.
New-Item -ItemType Directory -Force -Path $WorkRoot, $OutputDirectory, $SmokeRoot | Out-Null

if (-not $SkipInstaller) {
    if (-not $InnoCompiler) {
        $Candidates = @(
            (Join-Path ${env:ProgramFiles(x86)} 'Inno Setup 6\ISCC.exe'),
            (Join-Path $env:LOCALAPPDATA 'Programs\Inno Setup 6\ISCC.exe'),
            (Join-Path $WorkRoot 'toolchain\InnoSetup\ISCC.exe')
        )
        $InnoCompiler = $Candidates | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } | Select-Object -First 1
    }
    if (-not $InnoCompiler) { throw 'Specify -InnoCompiler <ISCC.exe>, or use -SkipInstaller for application binaries only.' }
    $InnoCompiler = (Resolve-Path -LiteralPath $InnoCompiler).Path
}

function Invoke-Python([string[]]$Arguments) {
    & $Python @Arguments
    if ($LASTEXITCODE -ne 0) { throw "Python build step failed with exit code $LASTEXITCODE." }
}

function Test-FrozenApp([string]$Executable, [string]$Report) {
    $Report = [IO.Path]::GetFullPath($Report)
    if (Test-Path -LiteralPath $Report) { Remove-Item -LiteralPath $Report -Force }
    $Process = Start-Process -FilePath $Executable -ArgumentList @('--smoke-test', ('"{0}"' -f $Report)) `
        -WorkingDirectory $SmokeRoot -WindowStyle Hidden -PassThru
    try {
        if (-not $Process.WaitForExit(180000)) {
            Stop-Process -Id $Process.Id -Force
            throw "Smoke test timed out: $Executable"
        }
        $Process.Refresh()
        if (-not (Test-Path -LiteralPath $Report -PathType Leaf)) {
            throw "Smoke test did not produce its report (exit $($Process.ExitCode)): $Executable"
        }
        $Result = Get-Content -LiteralPath $Report -Raw -Encoding UTF8 | ConvertFrom-Json
        if ($Process.ExitCode -ne 0 -or -not $Result.ok -or -not $Result.frozen -or $Result.bits -ne 64) {
            throw "Frozen smoke test failed. Read $Report"
        }
        Write-Host "Verified $Executable ($($Result.checks.Count) checks)."
    } finally {
        $Process.Dispose()
    }
}

$PriorUtf8 = $env:PYTHONUTF8
$PriorNoUserSite = $env:PYTHONNOUSERSITE
$PriorPythonPath = $env:PYTHONPATH
try {
    $env:PYTHONUTF8 = '1'
    $env:PYTHONNOUSERSITE = '1'
    Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue
    Push-Location $RepoRoot
    try {
        $Preflight = & $Python -c "import json,site,struct,sys,PyInstaller; from yeone_ocr import __version__; assert sys.platform=='win32' and struct.calcsize('P')==8; assert sys.prefix!=sys.base_prefix and not site.ENABLE_USER_SITE, 'Use a prepared isolated venv'; print(json.dumps({'version':__version__,'python':sys.version.split()[0],'pyinstaller':PyInstaller.__version__}))"
        if ($LASTEXITCODE -ne 0) { throw 'Windows x64 build preflight failed.' }
        $BuildInfo = $Preflight | ConvertFrom-Json
        $Version = $BuildInfo.version
        Write-Host "Building YeonE_OCR $Version with Python $($BuildInfo.python) / PyInstaller $($BuildInfo.pyinstaller)."
        Invoke-Python @('-m', 'yeone_ocr.frozen_smoke', (Join-Path $SmokeRoot 'source.json'))
        Invoke-Python @('-m', 'PyInstaller', '--noconfirm', '--clean', '--distpath', $DistRoot,
                        '--workpath', $PyInstallerWork, (Join-Path $RepoRoot 'packaging\YeonE_OCR.spec'))
        $Executable = Join-Path $AppSource 'YeonE_OCR.exe'
        $Portable = Join-Path $DistRoot 'YeonE_OCR-Windows-x64.exe'
        if (-not (Test-Path -LiteralPath $Executable -PathType Leaf) -or -not (Test-Path -LiteralPath $Portable -PathType Leaf)) {
            throw 'PyInstaller did not produce both onedir and portable executables.'
        }
        Test-FrozenApp $Executable (Join-Path $SmokeRoot 'onedir.json')
        Test-FrozenApp $Portable (Join-Path $SmokeRoot 'portable.json')
        $PublishedPortable = Join-Path $OutputDirectory 'YeonE_OCR-Windows-x64.exe'
        Copy-Item -LiteralPath $Portable -Destination $PublishedPortable -Force
        $Artifacts = @($PublishedPortable)
        if (-not $SkipInstaller) {
            $InstallerScript = Join-Path $RepoRoot 'packaging\YeonE_OCR.iss'
            & $InnoCompiler "/DAppVersion=$Version" "/DSourceDir=$AppSource" "/DOutputDir=$OutputDirectory" $InstallerScript
            if ($LASTEXITCODE -ne 0) { throw "Inno Setup failed with exit code $LASTEXITCODE." }
            $Installer = Join-Path $OutputDirectory "YeonE_OCR-$Version-Setup-x64.exe"
            if (-not (Test-Path -LiteralPath $Installer -PathType Leaf)) { throw "Expected installer missing: $Installer" }
            $Artifacts += $Installer
        }
        $Lines = foreach ($Artifact in $Artifacts) {
            $Hash = (Get-FileHash -LiteralPath $Artifact -Algorithm SHA256).Hash.ToLowerInvariant()
            "$Hash  $([IO.Path]::GetFileName($Artifact))"
        }
        [IO.File]::WriteAllText((Join-Path $OutputDirectory 'SHA256SUMS.txt'), (($Lines -join "`n") + "`n"), [Text.UTF8Encoding]::new($false))
        $BuildInfo | Add-Member -NotePropertyName artifacts -NotePropertyValue @($Artifacts | ForEach-Object { [IO.Path]::GetFileName($_) })
        $BuildInfo | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath (Join-Path $WorkRoot 'build-result.json') -Encoding UTF8
        Write-Host "Build complete. Release files: $OutputDirectory"
    } finally {
        Pop-Location
    }
} finally {
    $env:PYTHONUTF8 = $PriorUtf8
    $env:PYTHONNOUSERSITE = $PriorNoUserSite
    $env:PYTHONPATH = $PriorPythonPath
}
