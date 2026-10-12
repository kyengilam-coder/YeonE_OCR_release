# Windows 실행 파일 만들기

배포받는 사용자는 Python을 설치할 필요가 없습니다. `YeonE_OCR-Windows-x64.exe`를 실행하면 창이 열립니다. 빌드 내부 이름과 파일 속성의 원래 이름은 `YeonE_OCR.exe`입니다. 두 파일은 같은 내용입니다.

## 개발자 빌드

Windows x64와 64비트 Python이 필요합니다. 이 빌드는 Python 3.14, PyInstaller 6.22.3을 기준으로 확인합니다. 다른 운영체제에서 Windows 실행 파일을 교차 컴파일하지 않습니다.

```powershell
python -m venv .venv-build
.venv-build\Scripts\python.exe -m pip install -r requirements-windows.txt -r requirements-build.txt
.venv-build\Scripts\python.exe scripts\build_windows.py --dist-dir dist\windows --work-dir build\windows
```

`--prepare-only`를 덧붙이면 의존성·라이선스·버전 자료만 준비합니다. 빌드 스크립트 자체는 설치나 인터넷 요청을 하지 않습니다. `dist/windows`에는 실행 파일, SHA-256, 빌드 명세, `THIRD_PARTY_LICENSES`가 생성됩니다. 소스 수정 내용도 빌드 명세의 파일별 SHA-256에 기록됩니다.

`지침.md`, HWPX 기본 틀과 XML 스키마, 한자 독음 사전, OpenCC 사전, DOCX 기본 틀, Google SDK 메타데이터를 실행 파일에 포함합니다. PyInstaller의 표준 훅이 Tcl/Tk, NumPy, OpenCV, lxml 등 실행에 필요한 라이브러리를 수집합니다. UPX는 사용하지 않습니다.

## 다음 버전 배포

배포 저장소는 `kyengilam-coder/YeonE_OCR_release`입니다. 수정한 소스와 함께 `yeone_ocr/__init__.py`와 `pyproject.toml`의 버전을 같은 새 번호로 올리고, `docs/release-notes.md`에 변경 내용을 적어 `main`에 반영하십시오.

GitHub의 **Actions → Windows release → Run workflow**를 실행하면 Windows EXE 빌드, 오프라인 실행 점검, 업데이트 정보·소스·라이선스 ZIP 생성, 릴리스 게시를 이어서 수행합니다. 같은 버전의 기존 릴리스는 덮어쓰지 않습니다. 이 작업은 수동으로 시작하며, 코드를 push하는 것만으로 릴리스를 만들지는 않습니다. 정식 릴리스 게시 후 기존 앱이 시작할 때 새 버전을 감지합니다.

직접 의존성은 `requirements-windows.txt`, 빌드 도구는 `requirements-build.txt`에 고정되어 있습니다. 간접 의존성은 설치 시점에 결정되며 해당 빌드에서 사용한 실제 버전 전체는 `build-manifest.json`에 기록됩니다.

## 인터넷 없이 실행 파일 점검

```powershell
$process = Start-Process -FilePath .\dist\windows\YeonE_OCR-Windows-x64.exe -ArgumentList '--self-test', '--output', 'C:\검증\YeonE_OCR' -Wait -PassThru -WindowStyle Hidden
$process.ExitCode
Get-Content 'C:\검증\YeonE_OCR\self-test.json' -Encoding UTF8
```

공백이 있는 출력 경로는 `-ArgumentList` 안에서도 큰따옴표로 감싸십시오. `--self-test`와 `--output`은 함께 지정해야 합니다. 출력 경로 안에 실행별 하위 폴더와 최신 `self-test.json`을 만듭니다. 성공하면 종료 코드는 0이고 `passed`가 `true`입니다. 실패하면 종료 코드는 1이며 보고서와 로그에 실패 위치를 남깁니다.

이 점검은 숨긴 Tk 창, 사전·서식·SDK 가져오기, 합성 2쪽 PDF의 mock 판독, HWPX/DOCX 저장·재열기, 검수 HTML과 보고서를 확인합니다. 사용자의 설정 파일을 읽거나 쓰지 않고, API 키·업데이트·환율 요청을 사용하지 않습니다. DNS와 연결 호출도 차단합니다. 실제 스캔 문헌의 OCR 정확도나 유료 모델 품질을 측정하는 시험은 아닙니다.

`--version`은 버전만 출력합니다. 일반 창 실행 중 표준 출력이 없는 경우 진단 로그를 `%LOCALAPPDATA%\YeonE_OCR\logs\startup.log`에 남깁니다. API 키와 인증 헤더는 로그에서 가립니다. 일반 설정과 결과 파일의 위치는 앱의 기존 동작을 따릅니다.

## 배포 자료와 라이선스

실행 파일과 함께 `THIRD_PARTY_NOTICES.txt`, `THIRD_PARTY_LICENSES`, 빌드 명세를 보관하십시오. 실행 파일 안에도 같은 라이선스 자료를 넣습니다. 사용한 배포판에 별도 라이선스 파일이 없는 경우 `license-manifest.json`에 기록하므로 게시 전에 원본 소스의 조건을 확인해야 합니다.

이번 배포는 배포자가 보유한 PyMuPDF 상용 라이선스를 사용합니다. 설치된 패키지의 원래 `COPYING` 파일도 보존하지만, 이를 앱 소스의 AGPL 라이선스 지정으로 해석하지 않습니다. 비공개 계약서나 인증 자료는 번들에 넣지 않습니다. 재배포 범위는 해당 상용 계약과 다른 구성 요소의 각 라이선스를 따릅니다. [PyMuPDF 공식 라이선스 안내](https://pymupdf.io/licensing)에서 두 제공 방식을 확인할 수 있습니다.

수집 대상은 앱 소스, 명시한 정적 자료, 설치된 의존성의 배포 파일입니다. API 키, 사용자 설정, 홈 폴더의 `.yeone_ocr/config.json`, 실행 로그, 시험 산출물은 배포에 포함하지 않습니다. 빌드 명세에는 환경변수 값이나 인증 자료를 기록하지 않습니다.

현재 파일에는 코드 서명이 포함되지 않습니다. 코드 서명을 도입하면 서명 후 SHA-256을 다시 생성해야 합니다. 자동 업데이트는 게시된 실행 파일과 체크섬을 이용하므로, 최종 파일과 체크섬이 일치하는 상태로 릴리스해야 합니다.
