# Windows 1.1.0 설치판

1.1.0은 내장 PP-OCRv6 환경을 유지하고 일반·이미지 OCR의 기본 경로를 로컬 인식 후 선택 구간 AI 검증으로 연결한다. [사용법·검증 경계](LOCAL_FIRST.md)와 [릴리스 기록](release-notes.md)을 따른다. 기존 AppId와 사용자 자료 저장 위치를 유지하며 최신 배포판 1.0.6 설치 위에 갱신할 수 있도록 빌드한다. 기존 1.0.6의 저장·교정 동작을 통합한 소스를 기준으로 검증한다. 포터블 단일 파일은 엔진 환경을 별도로 준비해야 한다.

버전은 `pyproject.toml`, `yeone_ocr/__init__.py`, `packaging/YeonE_OCR.iss`에서 **1.1.0**으로 맞춘다. 아래 빌드 절차를 사용할 때 `WorkRoot`는 `work/windows-1.1/build`, `OutputDirectory`는 `dist/1.1.0`으로 지정한다. 검증한 고정 PP-OCRv6 묶음을 `-PPOCRv6Bundle`에 전달하고 빌드 환경에는 잠금 의존성을 설치한다. 소스·문서의 버전 변경은 설치 파일 생성이나 게시 완료를 뜻하지 않는다.

배포 전 설치본에서 버전 표시·엔진 탐색·로컬 인식·선택 구간 계획·키 없는 실행·명시적 로컬 전용·재개·HWPX/검색 PDF 출력·사용자 자료 보존을 확인한다. AI 연결은 모의 전송 검증과 실제 유료 호출을 구분해 기록한다. 소스 기준 75개 모듈·1,256개 회귀 검사의 통과 기록은 `work/local-first-final-regression/summary.json`이며 설치본 검증을 대체하지 않는다. 빌드 산출물 해시와 설치본 검증 결과·게시 완료는 실제 완료 후 기록한다.

공개 게시 대상은 [v1.1.0 릴리스](https://github.com/kyengilam-coder/YeonE_OCR_release/releases/tag/v1.1.0)다. 이 안내는 빌드·게시 준비 절차이며 해당 주소의 게시 완료를 주장하지 않는다.

# Windows 0.11.0 설치판

0.11.0은 같은 고정 PP-OCRv6 환경을 포함하고 내장 엔진의 독립 탐색과 쪽별 자동 선택을 추가한다. 기존 0.10.0 설치를 같은 AppId로 갱신할 수 있다. 새 빌드는 아래 명령에서 `WorkRoot`를 `work/windows-0.11/build`, `OutputDirectory`를 `dist/0.11.0`으로 지정한다. 현재 버전은 앱 소스에서 읽는다. 검증 스크립트의 `-AutomaticOCR` 옵션으로 구형 엔진 폴더 설정이 있는 상태에서 설치된 EXE의 기본 자동 OCR을 확인한다.

## 0.10.0부터 포함한 독립 실행 환경

0.10.0 설치 파일은 PP-OCRv6 medium 모델 두 개와 Python 3.13.15·RapidOCR 3.10.0·ONNX Runtime 1.25.1 실행 환경을 포함한다. 인식할 때 인터넷이나 별도 Python 설치가 필요 없다. 앱과 함께 설치되는 `tools/ppocrv6`를 자동 탐색하며 새 설치에서는 해당 엔진만 기본 선택한다. 기존에 저장한 엔진 폴더가 있으면 그 선택이 우선한다.

```powershell
# 먼저 scripts/install_ppocrv6.py로 고정 모델과 엔진 venv를 준비한다.
python tools/prepare_msvc_runtime.py --output work/windows-0.10/msvc-runtime --cache work/windows-0.10/msvc-download
& .\tools\ppocrv6\venv\Scripts\python.exe tools/prepare_ppocrv6_bundle.py `
  --source tools/ppocrv6 --output 'work/windows-0.10/engine pack/tools/ppocrv6' --msvc-runtime work/windows-0.10/msvc-runtime
& .\tools\build_windows.ps1 `
  -Python .\work\windows-0.10\build-venv\Scripts\python.exe `
  -InnoCompiler .\work\windows-0.10\toolchain\InnoSetup\ISCC.exe `
  -WorkRoot .\work\windows-0.10\build -OutputDirectory .\dist\0.10.0 `
  -PPOCRv6Bundle 'work/windows-0.10/engine pack/tools/ppocrv6'
```

빌드 환경은 `packaging/requirements-windows.lock.txt`를 설치하고 같은 Python으로 `tools/build_notices.py`를 실행한다. Python 3.13에서 hanja의 고정 의존성 PyYAML 6.0.1은 환경 변수 `PYYAML_FORCE_LIBYAML=0`을 지정하고 `pip wheel PyYAML==6.0.1 --wheel-dir work/windows-0.10/wheels`로 순수 Python wheel을 만든 뒤 `pip install --find-links work/windows-0.10/wheels -r packaging/requirements-windows.lock.txt`로 설치한다.

설치용 엔진 묶음은 venv의 외부 Python 경로를 복사하지 않는다. 독립 Python과 표준 라이브러리, 잠금 목록의 배포 RECORD 파일·네이티브 DLL·라이선스만 수집하고 `_pth`로 포함 경로를 제한한다. 모델은 잠금 SHA-256으로 확인하며, `bundle-manifest.json`으로 전체 파일 목록·크기·SHA-256을 설치 파일 생성 전에 검증한다. Windows 글꼴과 사용자 자료는 포함하지 않는다.

포터블 단일 실행 파일은 종전 업데이트 형식을 유지하며 모델·엔진 환경을 별도로 설치한다. 완전한 오프라인 PP-OCRv6 환경이 필요하면 `YeonE_OCR-0.10.0-Setup-x64.exe`를 사용한다. 이번 로컬 빌드는 공개 게시와 별개다.

## 이전 0.9.0 배포 기록

0.9.0 다운로드 주소는 [v0.9.0 릴리스](https://github.com/kyengilam-coder/YeonE_OCR_release/releases/tag/v0.9.0)다. 배포 완료 후 아래 자산을 이 주소에서 받는다. 공개 완료 여부와 실제 파일은 GitHub 릴리스 화면에서 확인한다.

| 자산 | 용도 |
|---|---|
| `YeonE_OCR-0.9.0-Setup-x64.exe` | Windows 10/11 x64 사용자별 설치 |
| `YeonE_OCR-Windows-x64.exe` | 설치 없이 실행하는 포터블·기존 자동 업데이트 자산 |
| `YeonE_OCR-0.9.0-source.zip` | 배포 커밋의 소스·테스트·빌드 자료·AB 원본 소스 보존 사본 |
| `YeonE_OCR-0.9.0-licenses.zip` | 제3자 고지와 라이선스 원문 |
| `update.json` | 기존 포터블 업데이트 형식의 버전·파일 크기·SHA-256 |
| `SHA256SUMS.txt` | 배포 자산의 SHA-256 목록 |

앱 실행에 필요한 Python과 패키지는 설치판·포터블에 포함한다. 로컬 세 엔진의 별도 실행 환경과 공개 모델은 [로컬 엔진 안내](LOCAL_OCR.md)에 따라 설치한다. 모델 가중치와 큰 엔진 환경을 앱 설치 파일에 넣지 않는다.

## 설치와 사용자 자료

설치 파일은 `%LOCALAPPDATA%\Programs\YeonE_OCR`에 관리자 권한 없이 설치하고 시작 메뉴 바로가기를 만든다. 바탕 화면 바로가기는 선택 사항이다. 기존 설치를 갱신할 때는 OCR을 끝내거나 중지하여 완료 기록을 저장한 뒤 프로그램을 닫고 새 설치 파일을 같은 위치에 실행한다. 포터블은 기존 자동 업데이트 절차를 사용할 수 있다.

API 키·설정은 기존 `사용자 폴더\.yeone_ocr\config.json`, OCR·교정 기록과 출력 문서는 원본 옆 작업 폴더를 사용한다. 붙여넣은 원본 이미지는 `%LOCALAPPDATA%\YeonE_OCR\screenshots`에 보관한다. 이 사용자 자료를 업데이트·제거 대상으로 등록하지 않는다. API 키와 이용 권한은 사용자가 설정하며 API 이용료는 별도다.

기존 일반 OCR·책갈피 OCR·이미지 OCR·설정 탭과 세로 읽기를 유지한다. **3엔진 비교·교정**은 현재 원본으로 원본 대조 편집 창을 연다. 로컬 인식은 외부에 전송하지 않는다. 별도 AI 검증은 제공사·모델·문서 범위·이미지와 호출 상한을 확인한 뒤 실행하고, 결과를 교정 제안으로 보존한다. [통합 기능](INTEGRATION.md)과 [AI 검증 정책](AI_REVIEW.md)에 사용·저장 범위를 설명한다.

## 빌드

Windows x64, 64비트 Python, 빌드 가상환경과 Inno Setup 6의 `ISCC.exe`가 필요하다. `tools/build_windows.ps1`은 빌드 도구나 의존성을 자동 설치하지 않는다. 의존성은 `packaging/requirements-windows.lock.txt`를 사용한다.

```powershell
python -m venv work\windows-release\venv
& .\work\windows-release\venv\Scripts\python.exe -m pip install -r .\packaging\requirements-windows.lock.txt
& .\work\windows-release\venv\Scripts\python.exe .\tools\build_notices.py
& .\tools\build_windows.ps1 `
  -Python .\work\windows-release\venv\Scripts\python.exe `
  -InnoCompiler .\work\windows-release\toolchain\InnoSetup\ISCC.exe `
  -WorkRoot .\work\windows-ai-0.9 `
  -OutputDirectory .\dist\0.9.0
```

이미 설치한 Inno Setup의 경로를 지정해도 된다. `-SkipInstaller`는 실행 파일까지만 만들고 설치 파일은 생략한다. `-WorkRoot`·`-OutputDirectory`를 생략하는 스크립트 기본 경로는 각각 `work/windows-release`·`dist`이므로, 위 예제는 0.9.0 결과를 이전 빌드와 분리해 둔다.

`packaging/YeonE_OCR.spec`은 설치용 폴더 묶음과 포터블을 만들고, `packaging/YeonE_OCR.iss`는 폴더 묶음으로 설치 파일을 만든다. 앱 버전은 `0.9.0`, 설치 파일·태그·업데이트 manifest도 같은 버전이어야 한다. Inno Setup 파일을 직접 사용할 때의 버전 기본값도 `0.9.0`이다.

실행 파일에는 앱과 명시적으로 허용한 사전·문서 서식·아이콘·규칙·안내 문서를 포함한다. 저장소 전체나 사용자 홈, 설정, 실제 문서·캡처·판독 기록·작업 폴더를 묶지 않는다. 통합 전 AB 소스 보존 사본은 **소스 ZIP**의 `references/AB_OCR_0.3.0`에 포함하며 앱 런타임으로 불러오지 않는다. [보존 안내](../references/README.md)

## 검증

빌드 스크립트는 소스에서 오프라인 자체 검사를 한 뒤 설치용 실행 파일과 포터블에서 같은 검사를 실행한다. 결과는 `work/windows-ai-0.9/smoke/{source,onedir,portable}.json`에 남긴다. 실패하면 후속 설치 파일 생성으로 진행하지 않는다.

0.9.0 검증 기준 결과는 다음과 같다. 문서·소스·의존성을 바꾸면 다시 빌드하고 동일성을 재검사한다.

- 기능 통합 기준: **61개 모듈·고유 테스트 1,080개 통과**, 실패·오류·건너뜀·미완료 0개. 공개 소스 정리와 추가 검사 뒤의 결과는 [릴리스 기록](release-notes.md)에 이어 기록한다.
- 소스·설치용 실행 파일·포터블: **오프라인 자체 검사 각각 26개 통과**. 로컬 비교와 편집·복구, AI의 모의 이미지 재검증과 번체·부호 제안, 사람의 채택·되돌리기·재열기를 포함한다.
- 두 실행 파일: **앱 모듈 각각 75개**, 명시적 리소스 **17개**, 라이선스 참조 **162개**가 소스와 일치한다. 기준 보고서는 `work/windows-ai-0.9/verify-build.json`이다.
- 실제 연어OCR 창: 공개 합성 원본·모의 HTTP로 두 단계 AI 검증, 제안 채택·기각·되돌리기, 저장·재열기와 TXT·HWPX·DOCX 출력을 확인했다. 원본·원시 OCR·AI 응답은 유지됐으며 1440×930·1080×720 화면을 확인했다.
- Gemini·OpenAI·Anthropic의 실제 SDK 직렬화와 거절 응답 보존은 외부 연결을 막은 메모리 HTTP로 확인했다. **실제 유료 API의 인증·문헌 인식 정확도는 검증하지 않았다.**

```powershell
$check = Start-Process -FilePath .\dist\0.9.0\YeonE_OCR-Windows-x64.exe `
  -ArgumentList '--smoke-test', '"C:\검증\연어OCR-smoke.json"' `
  -WindowStyle Hidden -Wait -PassThru
$check.ExitCode
Get-Content -LiteralPath 'C:\검증\연어OCR-smoke.json' -Encoding UTF8
Get-FileHash -LiteralPath .\dist\0.9.0\YeonE_OCR-0.9.0-Setup-x64.exe -Algorithm SHA256
```

이 자체 검사는 사용자 설정·키와 유료 모델을 사용하지 않는다. 실제 스캔의 글자 정확도, 각 제공사의 온라인 인증, Windows 설치·재설치·제거 검증은 별도이며 실제 수행 여부를 릴리스에 구분해서 기록한다. 이번 개발 검증에서는 기존 사용자 설치를 변경하지 않았다.

## 소스와 공개 배포

공개 저장소는 [kyengilam-coder/YeonE_OCR_release](https://github.com/kyengilam-coder/YeonE_OCR_release), 0.9.0 배포 태그는 `v0.9.0`이다. 소스를 커밋한 뒤 다음 명령으로 **그 커밋**의 소스 ZIP·라이선스 ZIP·업데이트 manifest와 전체 SHA-256 목록을 만든다.

```powershell
python tools/prepare_windows_release.py dist/0.9.0 --revision HEAD
```

별도 체크아웃은 `--repo <경로>`를 지정한다. 소스 ZIP은 커밋의 허용 목록만 담고 `SOURCE_REVISION.txt`에 버전과 커밋을 기록한다. 공개 테스트의 골든 JSON·라이선스 보완 자료·AB 소스 보존 사본도 포함하되 개인 자료와 모델은 제외한다. `.github/workflows/windows-release.yml`은 수동 실행 시 검사·빌드·설치 검증과 배포 자료 생성을 거쳐 공개 릴리스를 만든다. 문서에 다운로드 주소가 있다고 게시가 완료된 것은 아니므로, 게시 후 실제 자산과 SHA-256을 대조한다.

`update.json`은 최종 포터블의 정확한 바이트 크기와 SHA-256을 사용한다. 기존 자산 이름·형식을 유지하고 설치 파일을 포터블 자동 업데이트 자산으로 등록하지 않는다. 실행 파일과 함께 그 소스·빌드 스크립트·라이선스 자료를 같은 버전으로 보관한다.

## 제3자 고지와 서명

빌드 가상환경의 Python으로 `tools/build_notices.py`를 실행하면 설치된 패키지의 이름·버전·선언된 라이선스·프로젝트 URL과 원문 자료를 `packaging/THIRD_PARTY_NOTICES.txt`·`packaging/third_party_licenses`에 모은다. Python·Tcl/Tk·PyInstaller 부트로더 고지도 포함한다. 이 고지 목록은 최종 실행 파일의 모듈 목록과 같다는 뜻은 아니다.

`hanja` wheel에서 빠진 원문은 `packaging/license_supplements/hanja`에 출처와 함께 보존한다. 라이선스 이름을 임의로 추정하거나 앱의 새 라이선스를 지정하지 않으며, 기존 `DISTRIBUTION_NOTICE.txt`의 선택과 각 원문 고지를 유지한다.

설치 파일과 실행 파일은 코드 서명되지 않았다. 서명을 추가하면 서명한 최종 파일로 해시와 업데이트 manifest를 다시 만든다. SHA-256은 게시 파일과 내려받은 파일의 바이트 일치를 확인하며 게시자의 신원을 인증하는 코드 서명을 대신하지 않는다.

## 이전 버전 기록

- **0.8.0 로컬 통합판:** 설치·포터블 자체 검사 각각 25개, 앱 모듈 각각 74개·리소스 15개·라이선스 참조 162개 일치를 확인했다. 원본 설치를 교체하거나 당시 인터넷에 게시하지 않았다. 상세 기록은 [0.8.0 릴리스 기록](release-notes.md)에 보존한다.
- **0.7.4 Windows 공개판:** 자동 테스트 661개와 하위 테스트 154개, 소스·두 실행 파일 자체 검사 각각 21개를 통과했다. 앱 모듈 각각 49개와 라이선스 참조 162개 일치를 확인했고 공개 커밋 `0b59439`의 CI가 통과했다. 이 수치는 0.9.0 검증 결과와 구분한다. [0.7.4 릴리스](https://github.com/kyengilam-coder/YeonE_OCR_release/releases/tag/v0.7.4)
