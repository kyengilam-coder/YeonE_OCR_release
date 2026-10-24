[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$Installer,
    [string]$TestRoot = '',
    [string]$ExpectedVersion = '',
    [string]$PPOCRv6Fixture = '',
    [string]$TestPython = '',
    [switch]$AutomaticOCR,
    [string]$PreviousInstaller = ''
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$RepoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
if (-not $TestRoot) { $TestRoot = Join-Path $RepoRoot 'work\windows-release\installation-check' }
$TestRoot = [IO.Path]::GetFullPath($TestRoot)
if (-not $TestRoot.StartsWith(($RepoRoot + '\'), [StringComparison]::OrdinalIgnoreCase)) {
    throw 'Installer test directory must stay inside this workspace.'
}
$Installer = (Resolve-Path -LiteralPath $Installer).Path
if (-not $ExpectedVersion) {
    $VersionSource = Get-Content -LiteralPath (Join-Path $RepoRoot 'yeone_ocr\__init__.py') -Raw -Encoding UTF8
    $ExpectedVersion = [regex]::Match($VersionSource, '__version__\s*=\s*"([0-9]+\.[0-9]+\.[0-9]+)"').Groups[1].Value
    if (-not $ExpectedVersion) { throw 'Application version not found.' }
}
$AppPath = Join-Path $TestRoot 'installed app 한글'
$RegistryKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\{CDCDCE06-51D4-4AB5-B44D-B48133508FD9}_is1'
if (Test-Path -LiteralPath $RegistryKey) {
    throw 'An installation with this AppId already exists. The test will not touch it.'
}
if (Test-Path -LiteralPath $AppPath) { throw 'Use a new installer test directory.' }
$ExpectedPreviousVersion = ''
if ($PreviousInstaller) {
    $PreviousInstaller = (Resolve-Path -LiteralPath $PreviousInstaller).Path
    $PreviousFileVersion = (Get-Item -LiteralPath $PreviousInstaller).VersionInfo.FileVersionRaw
    if ($null -eq $PreviousFileVersion -or $PreviousFileVersion -eq [version]'0.0.0.0') {
        throw 'Previous installer has no usable Windows file version.'
    }
    $ExpectedPreviousVersion = $PreviousFileVersion.ToString(3)
    if ([version]$ExpectedPreviousVersion -ge [version]$ExpectedVersion) {
        throw 'Previous installer must have an older version than the target installer.'
    }
}
New-Item -ItemType Directory -Force -Path $TestRoot | Out-Null
$UserData = Join-Path $TestRoot 'preserved-user-data.txt'
[IO.File]::WriteAllText($UserData, 'synthetic OCR record - preserve through install and uninstall')
$DataHash = (Get-FileHash -LiteralPath $UserData -Algorithm SHA256).Hash
$Checks = [ordered]@{ previous_version = $null; upgrade = $false }
function Invoke-TestProcess([string]$File, [string[]]$ProcessArguments) {
    $Process = Start-Process -FilePath $File -ArgumentList $ProcessArguments -WindowStyle Hidden -PassThru
    try {
        if (-not $Process.WaitForExit(180000)) {
            Stop-Process -Id $Process.Id -Force
            throw "Test process timed out: $File"
        }
        $Process.Refresh()
        if ($Process.ExitCode -ne 0) { throw "Test process failed ($($Process.ExitCode)): $File" }
    } finally { $Process.Dispose() }
}
$InstallArgs = @('/VERYSILENT','/SUPPRESSMSGBOXES','/NORESTART','/SP-','/CURRENTUSER','/NOICONS','/TASKS=',('/DIR="' + $AppPath + '"'))
try {
    if ($PreviousInstaller) {
        $PreviousInstaller = (Resolve-Path -LiteralPath $PreviousInstaller).Path
        Invoke-TestProcess $PreviousInstaller ($InstallArgs + ('/LOG="' + (Join-Path $TestRoot 'previous-install.log') + '"'))
        $PreviousRegistered = Get-ItemProperty -LiteralPath $RegistryKey
        if ([IO.Path]::GetFullPath($PreviousRegistered.InstallLocation).TrimEnd('\') -ne $AppPath) {
            throw 'Unexpected previous installation directory.'
        }
        if ($PreviousRegistered.DisplayVersion -ne $ExpectedPreviousVersion) {
            throw 'Previous installed version does not match the installer file version.'
        }
        if (-not (Test-Path -LiteralPath (Join-Path $AppPath 'YeonE_OCR.exe') -PathType Leaf)) {
            throw 'Previous installation did not produce the application executable.'
        }
        $Checks.previous_version = $PreviousRegistered.DisplayVersion
    }
    Invoke-TestProcess $Installer ($InstallArgs + ('/LOG="' + (Join-Path $TestRoot 'install.log') + '"'))
    $Executable = Join-Path $AppPath 'YeonE_OCR.exe'
    $Registered = Get-ItemProperty -LiteralPath $RegistryKey
    if ([IO.Path]::GetFullPath($Registered.InstallLocation).TrimEnd('\') -ne $AppPath) { throw 'Unexpected registered installation directory.' }
    if ($Registered.DisplayVersion -ne $ExpectedVersion) { throw 'Installed version mismatch.' }
    $Checks.install = $true
    $Checks.upgrade = [bool]$PreviousInstaller
    $InstalledHash = (Get-FileHash -LiteralPath $Executable -Algorithm SHA256).Hash
    $SmokeReport = Join-Path $TestRoot 'installed-smoke.json'
    Invoke-TestProcess $Executable @('--smoke-test', ('"' + $SmokeReport + '"'))
    $Smoke = Get-Content -LiteralPath $SmokeReport -Raw -Encoding UTF8 | ConvertFrom-Json
    if (-not $Smoke.ok -or -not $Smoke.frozen) { throw 'Installed application smoke test failed.' }
    $Checks.installed_smoke = $Smoke.checks.Count
    Invoke-TestProcess $Installer ($InstallArgs + ('/LOG="' + (Join-Path $TestRoot 'reinstall.log') + '"'))
    $Reinstalled = Get-ItemProperty -LiteralPath $RegistryKey
    if ([IO.Path]::GetFullPath($Reinstalled.InstallLocation).TrimEnd('\') -ne $AppPath -or
        $Reinstalled.DisplayVersion -ne $ExpectedVersion) { throw 'Reinstalled registration mismatch.' }
    if ((Get-FileHash -LiteralPath $Executable -Algorithm SHA256).Hash -ne $InstalledHash) { throw 'Reinstall changed the executable unexpectedly.' }
    $Checks.reinstall = $true
    if ($TestPython) {
        & $TestPython (Join-Path $RepoRoot 'tools\verify_installed_local_first.py') --app $AppPath --output (Join-Path $TestRoot 'local-first')
        if ($LASTEXITCODE -ne 0) { throw 'Installed local-first real inference failed.' }
        $Checks.installed_local_first = $true
    }
    if ($PPOCRv6Fixture) {
        if (-not $TestPython) { throw 'Specify -TestPython for the installed OCR verifier.' }
        & $TestPython (Join-Path $RepoRoot 'tools\verify_ppocrv6_bundle.py') (Join-Path $AppPath 'tools\ppocrv6')
        if ($LASTEXITCODE -ne 0) { throw 'Installed engine inventory verification failed.' }
        $AutomaticArgs = @()
        if ($AutomaticOCR) { $AutomaticArgs = @('--automatic') }
        & $TestPython (Join-Path $RepoRoot 'tools\verify_installed_ppocrv6.py') --app $AppPath --fixture $PPOCRv6Fixture --output (Join-Path $TestRoot 'ocr') @AutomaticArgs
        if ($LASTEXITCODE -ne 0) { throw 'Installed PP-OCRv6 inference failed.' }
        $Checks.installed_ppocrv6 = $true
    }
} finally {
    $Uninstaller = Join-Path $AppPath 'unins000.exe'
    if (Test-Path -LiteralPath $Uninstaller) {
        $Registered = Get-ItemProperty -LiteralPath $RegistryKey
        if ([IO.Path]::GetFullPath($Registered.InstallLocation).TrimEnd('\') -ne $AppPath) { throw 'Refusing to uninstall a different installation.' }
        Invoke-TestProcess $Uninstaller @('/VERYSILENT','/SUPPRESSMSGBOXES','/NORESTART',('/LOG="' + (Join-Path $TestRoot 'uninstall.log') + '"'))
    }
}
if ((Test-Path -LiteralPath (Join-Path $AppPath 'YeonE_OCR.exe')) -or (Test-Path -LiteralPath $RegistryKey)) { throw 'Test installation was not removed.' }
if ((Get-FileHash -LiteralPath $UserData -Algorithm SHA256).Hash -ne $DataHash) { throw 'External user data changed.' }
$Checks.uninstall = $true
$Checks.external_data_preserved = $true
$Checks | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $TestRoot 'result.json') -Encoding UTF8
$Checks | ConvertTo-Json
