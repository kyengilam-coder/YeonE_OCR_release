#ifndef AppVersion
  #define AppVersion "0.7.1"
#endif
#ifndef SourceDir
  #define SourceDir "..\work\windows-release\dist\YeonE_OCR"
#endif
#ifndef OutputDir
  #define OutputDir "..\dist"
#endif

[Setup]
AppId={{CDCDCE06-51D4-4AB5-B44D-B48133508FD9}
AppName=YeonE OCR · 연어OCR
AppVersion={#AppVersion}
AppVerName=연어OCR {#AppVersion}
AppPublisher=YeonE OCR
AppPublisherURL=https://github.com/kyengilam-coder/YeonE_OCR_release
AppSupportURL=https://github.com/kyengilam-coder/YeonE_OCR_release/issues
AppUpdatesURL=https://github.com/kyengilam-coder/YeonE_OCR_release/releases/latest
DefaultDirName={localappdata}\Programs\YeonE_OCR
DefaultGroupName=연어OCR
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
OutputDir={#OutputDir}
OutputBaseFilename=YeonE_OCR-{#AppVersion}-Setup-x64
SetupIconFile=..\yeone_ocr\assets\YeonE_OCR.ico
UninstallDisplayIcon={app}\YeonE_OCR.exe
UninstallDisplayName=연어OCR {#AppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
DisableProgramGroupPage=yes
CloseApplications=yes
CloseApplicationsFilter=YeonE_OCR.exe
RestartApplications=no
SetupMutex=YeonE.OCR.Setup
VersionInfoVersion={#AppVersion}.0
VersionInfoProductName=YeonE OCR
VersionInfoDescription=YeonE OCR Windows Installer

[Languages]
Name: "korean"; MessagesFile: "compiler:Languages\Korean.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "바탕 화면에 바로가기 만들기"; GroupDescription: "바로가기:"; Flags: unchecked

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\연어OCR"; Filename: "{app}\YeonE_OCR.exe"; WorkingDir: "{userdocs}"; AppUserModelID: "YeonE.OCR.Desktop"
Name: "{autodesktop}\연어OCR"; Filename: "{app}\YeonE_OCR.exe"; WorkingDir: "{userdocs}"; Tasks: desktopicon; AppUserModelID: "YeonE.OCR.Desktop"

[Run]
Filename: "{app}\YeonE_OCR.exe"; Description: "연어OCR 실행"; Flags: nowait postinstall skipifsilent
