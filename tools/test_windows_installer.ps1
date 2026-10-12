[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$Installer,
    [string]$TestRoot = '',
    [string]$ExpectedVersion = ''
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
$AppPath = Join-Path $TestRoot 'installed-app'
$RegistryKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\{CDCDCE06-51D4-4AB5-B44D-B48133508FD9}_is1'
if (Test-Path -LiteralPath $RegistryKey) {
    throw 'An installation with this AppId already exists. The test will not touch it.'
}
if (Test-Path -LiteralPath $AppPath) { throw 'Use a new installer test directory.' }
New-Item -ItemType Directory -Force -Path $TestRoot | Out-Null
$UserData = Join-Path $TestRoot 'preserved-user-data.txt'
[IO.File]::WriteAllText($UserData, 'synthetic OCR record - preserve through install and uninstall')
$DataHash = (Get-FileHash -LiteralPath $UserData -Algorithm SHA256).Hash
$Checks = [ordered]@{}
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
    Invoke-TestProcess $Installer ($InstallArgs + ('/LOG="' + (Join-Path $TestRoot 'install.log') + '"'))
    $Executable = Join-Path $AppPath 'YeonE_OCR.exe'
    $Registered = Get-ItemProperty -LiteralPath $RegistryKey
    if ([IO.Path]::GetFullPath($Registered.InstallLocation).TrimEnd('\') -ne $AppPath) { throw 'Unexpected registered installation directory.' }
    if ($Registered.DisplayVersion -ne $ExpectedVersion) { throw 'Installed version mismatch.' }
    $Checks.install = $true
    $InstalledHash = (Get-FileHash -LiteralPath $Executable -Algorithm SHA256).Hash
    $SmokeReport = Join-Path $TestRoot 'installed-smoke.json'
    Invoke-TestProcess $Executable @('--smoke-test', ('"' + $SmokeReport + '"'))
    $Smoke = Get-Content -LiteralPath $SmokeReport -Raw -Encoding UTF8 | ConvertFrom-Json
    if (-not $Smoke.ok -or -not $Smoke.frozen) { throw 'Installed application smoke test failed.' }
    $Checks.installed_smoke = $Smoke.checks.Count
    Invoke-TestProcess $Installer ($InstallArgs + ('/LOG="' + (Join-Path $TestRoot 'reinstall.log') + '"'))
    if ((Get-FileHash -LiteralPath $Executable -Algorithm SHA256).Hash -ne $InstalledHash) { throw 'Reinstall changed the executable unexpectedly.' }
    $Checks.reinstall = $true
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
