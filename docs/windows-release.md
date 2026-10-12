# Windows 0.7.4 배포

Windows 10/11 x64 사용자는 [공개 릴리스](https://github.com/kyengilam-coder/YeonE_OCR_release/releases/tag/v0.7.4)의 `YeonE_OCR-0.7.4-Setup-x64.exe`로 설치합니다. Python과 실행에 필요한 패키지를 포함합니다. 기본 설치 경로는 `%LOCALAPPDATA%\Programs\YeonE_OCR`이며 관리자 권한을 요구하지 않는 사용자별 설치입니다. 시작 메뉴 바로가기를 만들고 바탕 화면 바로가기는 선택 사항으로 둡니다.

설치 프로그램은 프로그램 파일을 설치합니다. API 키·설정은 기존 `사용자 폴더\.yeone_ocr\config.json`을 사용하고, 원본과 OCR 기록·HWPX는 기존 문서 옆 결과 폴더에 남습니다. 붙여넣은 스크린샷은 `%LOCALAPPDATA%\YeonE_OCR\screenshots`에 원래 해상도의 RGB PNG로 보관하며, 각 원본 옆에 결과 폴더를 만듭니다. 업데이트와 제거 대상으로 이 사용자 자료를 등록하지 않습니다. API 키는 각 사용자가 입력하며 API 이용료는 별도입니다.

0.7.4은 **[일반 OCR]·[책갈피 OCR]·[이미지(스크린샷) OCR]·[설정]**의 네 탭을 제공합니다. 일반 탭의 [PDF 고르기…]는 PDF를, 이미지 탭의 [이미지 고르기…]는 PNG·JPG 등을 선택합니다. `Win+Shift+S` 캡처는 이미지 탭의 [스크린샷 붙여넣기] 또는 `Ctrl+Shift+V`로 불러오고, 인접한 [원본 보기]로 확인한 뒤 [한 번에 인식]을 누릅니다. 세 작업 탭의 원본 선택은 각각 유지합니다. 붙여넣기는 원본만 저장·선택하며 모델 호출은 인식을 실행할 때 시작합니다.

판독기·모델·API 키·읽기 방향·세부 옵션·문서 설정, 책갈피 교정 정책·메모·재판독과 고급 도구는 네 번째 [설정]에 모았습니다. 각 작업 탭에는 현재 판독기·모델·읽기 방향 요약과 [설정 바꾸기]를 표시하며, 설정 아래의 [설정 저장]·[OCR로 돌아가기]로 저장하고 작업 탭에 복귀합니다. 쪽 범위와 인쇄 쪽수 보정은 원본을 고르는 작업 탭에 둡니다.

이미지 탭의 **[입력 비우기]**는 선택 원본·쪽 범위·인쇄 쪽수 보정·강제 재판독과 저장 위치·진행 표시를 초기화하여 새 이미지 입력을 기다립니다. 디스크의 원본·판독 기록·HWPX와 진행 기록 이력은 보존합니다. 다른 작업 탭의 원본, API·모델·문서 설정도 유지하며 모델을 호출하지 않습니다.

이미지 탭의 **HWPX 저장** 줄에 저장 폴더의 전체 경로를 표시하고 **[HWPX 바로 열기]·[저장 폴더]**를 제공합니다. 캡처의 기본 HWPX 경로는 `%LOCALAPPDATA%\YeonE_OCR\screenshots\screenshot-<hash>_png_YeonE_OCR\out\screenshot-<hash>.hwpx`입니다. 0.7.2의 저장 위치를 유지하며 같은 픽셀은 같은 원본과 판독 기록을 재사용합니다. 다른 캡처는 별도로 저장하고, 기존 저장 파일이 수정되었으면 보존하고 새 파일을 만듭니다.

이미지용 **[읽기 방향]**은 일반·책갈피 OCR과 별도로 기억하며 기본은 **자동 판정**입니다. 글자층을 먼저 활용하고 없거나 부족하면 내 PC에서 실제 이미지의 글줄 간격·배치로 가로·세로 방향을 판정합니다. 추가 API 호출이나 비용은 없습니다. 빈 화면·그림 위주·가로와 세로 근거가 비슷해 확정하기 어려운 경우에는 이유를 알리고 세로쓰기 기본값을 사용합니다. 필요하면 이미지 탭의 [설정 바꾸기]에서 방향을 직접 고르십시오. 새 붙여넣기는 쪽 범위·인쇄 쪽수 보정·강제 재판독을 초기화하고 직접 고른 이미지 방향은 유지합니다.

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

`packaging/YeonE_OCR.spec`은 설치판용 폴더 묶음과 기존 자동 업데이트용 단일 실행 파일을 만듭니다. `packaging/YeonE_OCR.iss`가 폴더 묶음으로 설치 파일을 만듭니다. 기본 출력은 `dist/YeonE_OCR-0.7.4-Setup-x64.exe`, `dist/YeonE_OCR-Windows-x64.exe`, `dist/SHA256SUMS.txt`입니다.

자료 수집은 앱 패키지, 명시한 사전·문서 서식·아이콘·규칙 데이터 및 설치된 의존성으로 제한합니다. 저장소 전체나 사용자 홈·설정·실제 원본 문서·저장한 스크린샷·OCR 작업 폴더를 데이터 트리로 묶지 않습니다. 고대비 문서 아이콘은 실행 파일과 설치 파일, 시작 메뉴 바로가기에 적용됩니다.

## 검증

빌드 스크립트는 소스의 오프라인 점검을 먼저 실행하고, 두 종류의 생성된 실행 파일에 같은 점검을 실행합니다. 결과 JSON은 기본 `work/windows-release/smoke`에 저장합니다. 점검이 실패하면 이후 설치 파일 생성 단계를 진행하지 않습니다.

```powershell
$check = Start-Process -FilePath .\dist\YeonE_OCR-Windows-x64.exe `
  -ArgumentList '--smoke-test', '"C:\검증\연어OCR-smoke.json"' `
  -WindowStyle Hidden -Wait -PassThru
$check.ExitCode
Get-Content -LiteralPath 'C:\검증\연어OCR-smoke.json' -Encoding UTF8
Get-FileHash -LiteralPath .\dist\YeonE_OCR-0.7.4-Setup-x64.exe -Algorithm SHA256
```

숨긴 Tk 창과 아이콘, 제공사 SDK와 내장 자료, 합성 문서의 PDF/HWPX/DOCX 처리를 검사합니다. 실제 사용자 설정과 API 키를 쓰지 않고 유료 모델 호출도 하지 않습니다. 이 점검은 실제 스캔의 글자 정확도나 각 제공사의 온라인 인증을 보장하지 않습니다. 설치·바로가기·제거 검증은 생성된 설치 파일로 별도 수행하고, 릴리스에 실제 실행한 검증과 미실행 검증을 구분해 적습니다.

0.7.4에서는 이미지 입력 비우기의 화면 초기화와 파일 보존, 작업별 읽기 방향 설정의 독립성, 실제 픽셀 기반 가로·세로 판정, 저장 경로와 결과 열기를 검사 대상으로 둡니다. 최종 실행파일의 오프라인 검사·소스 일치·동봉 자료 검사 결과는 확인 후 `docs/release-notes.md`에 기록합니다. 현재 최종 빌드 검증의 완료 여부나 검사 수는 확정하지 않았습니다. 실제 유료 OCR API와 실제 문서의 인식 정확도는 오프라인 검사와 구분하여 보고합니다.

이번 로컬 배포 준비에서는 사용자의 기존 설치를 보존하기 위해 설치·재설치·제거 검증을 다시 실행하지 않습니다. 이전 버전의 설치 검증과 이번 버전의 오프라인 검사를 구분하여 보고합니다.

## 기존 배포와 업데이트

공개 배포 저장소는 [kyengilam-coder/YeonE_OCR_release](https://github.com/kyengilam-coder/YeonE_OCR_release)이며 태그는 `v0.7.4`입니다. 기존 `YeonE_OCR-Windows-x64.exe`와 `update.json`의 자산 이름·형식을 유지하여 기존 단일 실행 파일이 새 버전을 찾을 수 있게 합니다. 설치판용 파일을 단일 실행 파일의 자동 업데이트 자산으로 등록하지 않습니다.

설치판은 새 설치 파일을 같은 위치에 실행해 갱신합니다. 단일 실행 파일은 기존 자동 업데이트 절차를 이용할 수 있습니다. 어느 방식이든 작업 중인 OCR을 먼저 마치거나 중지하여 기록을 저장합니다. 사용자의 판독 기록을 새 버전 소스와 함께 배포하지 않습니다.

실행 파일과 함께 해당 소스·빌드 스크립트, 라이선스 자료와 SHA-256을 보관합니다. `update.json`은 최종 단일 실행 파일의 정확한 바이트 크기·SHA-256을 사용해야 합니다. 수정된 앱 버전, 설치 파일 버전, 릴리스 태그와 manifest 버전이 모두 `0.7.4`로 맞는지 확인합니다. 비공개 개발 저장소의 가시성을 바꾸지 않고 공개 배포 저장소에 배포용 자료만 반영합니다.

배포할 소스를 커밋한 뒤 `python tools/prepare_windows_release.py dist --revision HEAD`를 실행하면 자동 업데이트 manifest, 소스 ZIP, 라이선스 ZIP과 전체 SHA-256 목록을 만듭니다. 소스 ZIP은 커밋의 배포 대상 경로만 담고 사용자 설정·작업 폴더·문서를 제외합니다. 별도 체크아웃을 배포한다면 `--repo <체크아웃 경로>`로 지정합니다. `.github/workflows/windows-release.yml`은 수동 실행 시 테스트·빌드·설치 검사 후 같은 자료를 만들어 릴리스를 게시합니다.

## 제3자 고지와 서명

`tools/build_notices.py`를 빌드에 사용하는 가상환경의 Python으로 실행하면 설치된 패키지 메타데이터를 기준으로 이름·버전·선언된 라이선스·프로젝트 URL을 수집합니다. `packaging/THIRD_PARTY_NOTICES.txt`와 `packaging/third_party_licenses`에 원문 라이선스·고지 및 메타데이터를 저장합니다. Python의 `LICENSE.txt`, 설치된 Tcl/Tk 고지와 PyInstaller 부트로더 고지도 포함합니다. 메타데이터가 요구한 간접 의존성 중 일부는 실행 파일에서 제외될 수 있으므로 고지 목록은 최종 실행 파일의 모듈 목록과 동일하다는 뜻은 아닙니다.

`hanja` wheel에 빠진 라이선스 원문은 기존 배포의 보완 자료를 `packaging/license_supplements/hanja`에 출처와 함께 보존합니다. 메타데이터에 라이선스 이름이 없으면 이를 임의로 추정하지 않습니다. 기존 배포의 `DISTRIBUTION_NOTICE.txt`가 명시한 선택과 각 구성 요소의 원문 고지는 유지하며, 앱에 새로운 라이선스를 지정하는 문서로 사용하지 않습니다.

현재 설치 파일과 실행 파일은 코드 서명되지 않았습니다. 코드 서명을 추가하면 서명된 최종 파일로 SHA-256과 업데이트 manifest를 다시 생성해야 합니다. `SHA256SUMS.txt`는 게시된 파일과 내려받은 파일이 같은지 비교하는 자료이며 코드 서명 대신 게시자의 신원을 인증하지 않습니다.
