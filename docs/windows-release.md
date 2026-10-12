# Windows 0.7.2 배포

Windows 10/11 x64 사용자는 [공개 릴리스](https://github.com/kyengilam-coder/YeonE_OCR_release/releases/tag/v0.7.2)의 `YeonE_OCR-0.7.2-Setup-x64.exe`로 설치합니다. Python과 실행에 필요한 패키지를 포함합니다. 기본 설치 경로는 `%LOCALAPPDATA%\Programs\YeonE_OCR`이며 관리자 권한을 요구하지 않는 사용자별 설치입니다. 시작 메뉴 바로가기를 만들고 바탕 화면 바로가기는 선택 사항으로 둡니다.

설치 프로그램은 프로그램 파일을 설치합니다. API 키·설정은 기존 `사용자 폴더\.yeone_ocr\config.json`을 사용하고, 원본과 OCR 기록·HWPX는 기존 문서 옆 결과 폴더에 남습니다. 붙여넣은 스크린샷은 `%LOCALAPPDATA%\YeonE_OCR\screenshots`에 원래 해상도의 RGB PNG로 보관하며, 각 원본 옆에 결과 폴더를 만듭니다. 업데이트와 제거 대상으로 이 사용자 자료를 등록하지 않습니다. API 키는 각 사용자가 입력하며 API 이용료는 별도입니다.

0.7.2에서는 `Win+Shift+S` 캡처 후 [스크린샷 붙여넣기] 또는 `Ctrl+Shift+V` → [원본 보기] → [▶ 한 번에 인식 (PDF·이미지 → HWPX)]으로 사용할 수 있습니다. 붙여넣기는 원본만 저장·선택하며 모델 호출은 인식을 실행할 때 시작합니다. 같은 픽셀의 캡처는 같은 원본과 기록을 재사용하므로 제공사를 바꿔 이어 읽을 수 있습니다. 다른 캡처는 별도로 저장하고, 기존 저장 파일이 수정되었으면 보존하고 새 파일을 만듭니다. PNG·JPG 등 기존 파일 선택도 지원합니다.

새 붙여넣기는 쪽 범위·인쇄 쪽수 차이·강제 재판독을 초기화하고 선택한 읽기 방향은 유지합니다. 이미지의 자동 방향 판정은 세로쓰기 기본이므로 가로쓰기 캡처는 [읽기 방향]을 직접 고릅니다.

## 빌드

Windows x64, 64비트 Python, 준비된 빌드 가상환경과 Inno Setup 6의 `ISCC.exe`가 필요합니다. `tools/build_windows.ps1` 자체는 패키지나 빌드 도구를 설치하지 않습니다. 실제 배포에 사용한 Python 의존성 버전은 `packaging/requirements-windows.lock.txt`로 고정합니다.

```powershell
python -m venv work\windows-release\venv
& .\work\windows-release\venv\Scripts\python.exe -m pip install -r .\packaging\requirements-windows.lock.txt
& .\work\windows-release\venv\Scripts\python.exe .\tools\build_notices.py
& .\tools\build_windows.ps1 `
  -Python .\work\windows-release\venv\Scripts\python.exe `
  -InnoCompiler .\work\windows-release\toolchain\InnoSetup\ISCC.exe
```

이미 설치된 Inno Setup의 `ISCC.exe` 경로를 지정해도 됩니다. `-SkipInstaller`를 사용하면 앱 실행 파일까지만 만듭니다. `-WorkRoot`와 `-OutputDirectory`로 빌드 중간 경로와 최종 출력 폴더를 바꿀 수 있습니다.

`packaging/YeonE_OCR.spec`은 설치판용 폴더 묶음과 기존 자동 업데이트용 단일 실행 파일을 만듭니다. `packaging/YeonE_OCR.iss`가 폴더 묶음으로 설치 파일을 만듭니다. 기본 출력은 `dist/YeonE_OCR-0.7.2-Setup-x64.exe`, `dist/YeonE_OCR-Windows-x64.exe`, `dist/SHA256SUMS.txt`입니다.

자료 수집은 앱 패키지, 명시한 사전·문서 서식·아이콘·규칙 데이터 및 설치된 의존성으로 제한합니다. 저장소 전체나 사용자 홈·설정·실제 원본 문서·저장한 스크린샷·OCR 작업 폴더를 데이터 트리로 묶지 않습니다. 고대비 문서 아이콘은 실행 파일과 설치 파일, 시작 메뉴 바로가기에 적용됩니다.

## 검증

빌드 스크립트는 소스의 오프라인 점검을 먼저 실행하고, 두 종류의 생성된 실행 파일에 같은 점검을 실행합니다. 결과 JSON은 기본 `work/windows-release/smoke`에 저장합니다. 점검이 실패하면 이후 설치 파일 생성 단계를 진행하지 않습니다.

```powershell
$check = Start-Process -FilePath .\dist\YeonE_OCR-Windows-x64.exe `
  -ArgumentList '--smoke-test', '"C:\검증\연어OCR-smoke.json"' `
  -WindowStyle Hidden -Wait -PassThru
$check.ExitCode
Get-Content -LiteralPath 'C:\검증\연어OCR-smoke.json' -Encoding UTF8
Get-FileHash -LiteralPath .\dist\YeonE_OCR-0.7.2-Setup-x64.exe -Algorithm SHA256
```

숨긴 Tk 창과 아이콘, 제공사 SDK와 내장 자료, 합성 문서의 PDF/HWPX/DOCX 처리를 검사합니다. 실제 사용자 설정과 API 키를 쓰지 않고 유료 모델 호출도 하지 않습니다. 이 점검은 실제 스캔의 글자 정확도나 각 제공사의 온라인 인증을 보장하지 않습니다. 설치·바로가기·제거 검증은 생성된 설치 파일로 별도 수행하고, 릴리스에 실제 실행한 검증과 미실행 검증을 구분해 적습니다.

스크린샷 기능은 원래 픽셀 크기와 RGB 저장, 같은 픽셀의 재사용, 다른 캡처 분리, 변경된 기존 파일 보존, 붙여넣기 뒤 설정 초기화와 API 미호출을 확인합니다. 0.7.2는 전체 자동 테스트와 소스·설치용 실행파일·단일 실행파일의 각 20개 오프라인 검사를 통과했습니다. 실제 검사 범위와 제한은 `docs/release-notes.md`에 기록했습니다.

## 기존 배포와 업데이트

공개 배포 저장소는 [kyengilam-coder/YeonE_OCR_release](https://github.com/kyengilam-coder/YeonE_OCR_release)이며 태그는 `v0.7.2`입니다. 기존 `YeonE_OCR-Windows-x64.exe`와 `update.json`의 자산 이름·형식을 유지하여 기존 단일 실행 파일이 새 버전을 찾을 수 있게 합니다. 설치판용 파일을 단일 실행 파일의 자동 업데이트 자산으로 등록하지 않습니다.

설치판은 새 설치 파일을 같은 위치에 실행해 갱신합니다. 단일 실행 파일은 기존 자동 업데이트 절차를 이용할 수 있습니다. 어느 방식이든 작업 중인 OCR을 먼저 마치거나 중지하여 기록을 저장합니다. 사용자의 판독 기록을 새 버전 소스와 함께 배포하지 않습니다.

실행 파일과 함께 해당 소스·빌드 스크립트, 라이선스 자료와 SHA-256을 보관합니다. `update.json`은 최종 단일 실행 파일의 정확한 바이트 크기·SHA-256을 사용해야 합니다. 수정된 앱 버전, 설치 파일 버전, 릴리스 태그와 manifest 버전이 모두 `0.7.2`로 맞는지 확인합니다. 비공개 개발 저장소의 가시성을 바꾸지 않고 공개 배포 저장소에 배포용 자료만 반영합니다.

배포할 소스를 커밋한 뒤 `python tools/prepare_windows_release.py dist --revision HEAD`를 실행하면 자동 업데이트 manifest, 소스 ZIP, 라이선스 ZIP과 전체 SHA-256 목록을 만듭니다. 소스 ZIP은 커밋의 배포 대상 경로만 담고 사용자 설정·작업 폴더·문서를 제외합니다. 별도 체크아웃을 배포한다면 `--repo <체크아웃 경로>`로 지정합니다. `.github/workflows/windows-release.yml`은 수동 실행 시 테스트·빌드·설치 검사 후 같은 자료를 만들어 릴리스를 게시합니다.

## 제3자 고지와 서명

`tools/build_notices.py`를 빌드에 사용하는 가상환경의 Python으로 실행하면 설치된 패키지 메타데이터를 기준으로 이름·버전·선언된 라이선스·프로젝트 URL을 수집합니다. `packaging/THIRD_PARTY_NOTICES.txt`와 `packaging/third_party_licenses`에 원문 라이선스·고지 및 메타데이터를 저장합니다. Python의 `LICENSE.txt`, 설치된 Tcl/Tk 고지와 PyInstaller 부트로더 고지도 포함합니다. 메타데이터가 요구한 간접 의존성 중 일부는 실행 파일에서 제외될 수 있으므로 고지 목록은 최종 실행 파일의 모듈 목록과 동일하다는 뜻은 아닙니다.

`hanja` wheel에 빠진 라이선스 원문은 기존 배포의 보완 자료를 `packaging/license_supplements/hanja`에 출처와 함께 보존합니다. 메타데이터에 라이선스 이름이 없으면 이를 임의로 추정하지 않습니다. 기존 배포의 `DISTRIBUTION_NOTICE.txt`가 명시한 선택과 각 구성 요소의 원문 고지는 유지하며, 앱에 새로운 라이선스를 지정하는 문서로 사용하지 않습니다.

현재 설치 파일과 실행 파일은 코드 서명되지 않았습니다. 코드 서명을 추가하면 서명된 최종 파일로 SHA-256과 업데이트 manifest를 다시 생성해야 합니다. `SHA256SUMS.txt`는 게시된 파일과 내려받은 파일이 같은지 비교하는 자료이며 코드 서명 대신 게시자의 신원을 인증하지 않습니다.
