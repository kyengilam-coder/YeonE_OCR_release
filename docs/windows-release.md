# Windows 0.12.2 배포

이 문서는 **0.12.2의 배포 구성과 확인 절차**를 설명한다. 게시 여부·최종 검증 결과·실제 배포 파일은 [v0.12.2 릴리스](https://github.com/kyengilam-coder/YeonE_OCR_release/releases/tag/v0.12.2)에서 확인한다. 문서와 다운로드 링크의 존재만으로 검증 완료나 공개 게시를 판단하지 않는다.

0.12.2의 배포 대상 자산은 다음과 같다. 게시 후 공개 릴리스의 실제 파일·버전·SHA-256을 대조한다.

| 자산 | 용도 |
|---|---|
| `YeonE_OCR-0.12.2-Setup-x64.exe` | Windows 10/11 x64 사용자별 설치 |
| `YeonE_OCR-Windows-x64.exe` | 설치 없이 실행하는 포터블·기존 자동 업데이트 자산 |
| `YeonE_OCR-0.12.2-source.zip` | 배포 커밋의 소스·테스트·빌드 자료·AB 원본 소스 보존 사본 |
| `YeonE_OCR-0.12.2-licenses.zip` | 제3자 고지와 라이선스 원문 |
| `update.json` | 기존 포터블 업데이트 형식의 버전·파일 크기·SHA-256 |
| `SHA256SUMS.txt` | 배포 자산의 SHA-256 목록 |

앱 실행에 필요한 Python과 패키지는 설치판·포터블에 포함한다. 로컬 OCR은 **Tesseract·RapidOCR·EasyOCR·PaddleOCR 서버형·GLM-OCR·Surya의 6개 엔진** 중 선택해 사용한다. 각 엔진의 별도 실행 환경과 공개 모델은 [로컬 엔진 안내](LOCAL_OCR.md)에 따라 설치한다. 모델 가중치와 큰 엔진 환경을 앱 설치 파일에 넣지 않는다. 앱 설치·자체 검사 통과는 여섯 엔진의 실모델 설치·실제 문헌 정확도 검증을 뜻하지 않는다.

## 설치와 사용자 자료

설치 파일은 `%LOCALAPPDATA%\Programs\YeonE_OCR`에 관리자 권한 없이 설치하고 시작 메뉴 바로가기를 만든다. 바탕 화면 바로가기는 선택 사항이다. 기존 설치를 갱신할 때는 OCR을 끝내거나 중지하여 완료 기록을 저장한 뒤 프로그램을 닫고 새 설치 파일을 같은 위치에 실행한다. 포터블은 기존 자동 업데이트 절차를 사용할 수 있다.

API 키·설정은 기존 `사용자 폴더\.yeone_ocr\config.json`, OCR·교정 기록과 출력 문서는 원본 옆 작업 폴더를 사용한다. 붙여넣은 원본 이미지는 `%LOCALAPPDATA%\YeonE_OCR\screenshots`에 보관한다. 이 사용자 자료를 업데이트·제거 대상으로 등록하지 않는다. API 키와 이용 권한은 사용자가 설정하며 API 이용료는 별도다.

0.12.2는 책갈피 OCR에서 원본 PDF와 교정문을 나란히 표시하고, 직접 고친 문단을 기존 교정본에 저장한다. 저장 전 사본·외부 변경 확인과 사람 수정 문단의 재교정 보호를 포함한다. PDF 본문의 첫 언급만으로 구획 범위를 정하지 않으며, 확인되지 않는 범위는 사용자가 지정해야 한다. 결과 보기와 교정 실행을 구분한다.

기존 일반 OCR·책갈피 OCR·이미지 OCR·설정 탭과 세로 읽기를 유지한다. **OCR 모델 비교·교정**은 현재 원본으로 원본 대조 편집 창을 열고, 선택한 로컬 엔진의 원시 판독·사람의 교정·채택 이력을 구분해 보관한다. 로컬 인식은 외부에 전송하지 않는다. 별도 AI 검증은 제공사·모델·문서 범위·이미지와 호출 상한을 확인한 뒤 실행하고, 결과를 교정 제안으로 보존한다. [통합 기능](INTEGRATION.md)과 [AI 검증 정책](AI_REVIEW.md)에 사용·저장 범위를 설명한다.

새 판독은 원문의 호환 한자·이체자 선택자 등을 보존하며, 기존 기록은 당시의 문자 정책과 사람이 확정한 교정을 유지한다. 문자 위치를 확인하지 못한 판별은 보류한다. **소주 검수**는 사용자가 선택해 실행하는 영역 검출·재판독 도구다. 검출·재판독만으로 기존 본문을 바꾸지 않고, 원본과 삽입 위치를 사람이 확인한 뒤 반영하며 변경 전 사본을 남긴다. 제공된 실제 표본에서는 소주 재판독의 정확도 개선을 입증하지 못했으므로 자동 누락 복구나 정확도 향상 기능으로 안내하지 않는다. Document AI 연동은 보류 상태다.

## 빌드

Windows x64, 64비트 Python, 빌드 가상환경과 Inno Setup 6의 `ISCC.exe`가 필요하다. `tools/build_windows.ps1`은 빌드 도구나 의존성을 자동 설치하지 않는다. 의존성은 `packaging/requirements-windows.lock.txt`를 사용한다.

```powershell
python -m venv work\windows-release\venv
& .\work\windows-release\venv\Scripts\python.exe -m pip install -r .\packaging\requirements-windows.lock.txt
& .\work\windows-release\venv\Scripts\python.exe .\tools\build_notices.py
& .\tools\build_windows.ps1 `
  -Python .\work\windows-release\venv\Scripts\python.exe `
  -InnoCompiler .\work\windows-release\toolchain\InnoSetup\ISCC.exe `
  -WorkRoot .\work\release-0.12.2\build `
  -OutputDirectory .\dist\0.12.2
```

이미 설치한 Inno Setup의 경로를 지정해도 된다. `-SkipInstaller`는 실행 파일까지만 만들고 설치 파일은 생략한다. `-WorkRoot`·`-OutputDirectory`를 생략하는 스크립트 기본 경로는 각각 `work/windows-release`·`dist`이므로, 이번 빌드는 위와 같이 `work/release-0.12.2/build`·`dist/0.12.2`을 명시해 이전 빌드와 분리한다.

`packaging/YeonE_OCR.spec`은 설치용 폴더 묶음과 포터블을 만들고, `packaging/YeonE_OCR.iss`는 폴더 묶음으로 설치 파일을 만든다. 빌드 스크립트는 소스의 앱 버전을 읽어 Inno Setup에 전달한다. 이번 배포에서는 앱 버전·설치 파일·태그 `v0.12.2`·업데이트 manifest·소스 ZIP의 버전이 모두 `0.12.2`인지 확인한다. Inno Setup 파일을 직접 사용할 때에도 버전을 명시하고 결과를 검사한다.

실행 파일에는 앱과 명시적으로 허용한 사전·문서 서식·아이콘·규칙·안내 문서를 포함한다. 저장소 전체나 사용자 홈, 설정, 실제 문서·캡처·판독 기록·작업 폴더를 묶지 않는다. 통합 전 AB 소스 보존 사본은 **소스 ZIP**의 `references/AB_OCR_0.3.0`에 포함하며 앱 런타임으로 불러오지 않는다. [보존 안내](../references/README.md)

## 검증

아래 단계의 실제 결과를 모두 수집한 뒤 해당 공개 릴리스에 소스 커밋·검사 범위·성공 여부·제한을 함께 남긴다. 이 문서는 검증 절차를 설명하며 통과 수치를 대신하지 않는다. 이전 버전의 테스트 수나 과거 개발 소스의 통과 기록을 이번 실행 파일의 검증 결과로 대신 쓰지 않는다.

빌드 스크립트는 소스에서 오프라인 자체 검사를 한 뒤 설치용 실행 파일과 포터블에서 같은 검사를 실행한다. 결과는 `work/release-0.12.2/build/smoke/{source,onedir,portable}.json`, 빌드 정보는 같은 폴더의 `build-result.json`에 남긴다. 실패하면 후속 설치 파일 생성으로 진행하지 않는다.

- 전체 기능 회귀 검사와 기록·사람 교정 보존 검사는 최종 소스에 대해 실행한다.
- 소스·설치용 실행 파일·포터블의 오프라인 자체 검사, 포함 모듈·리소스·고지 자료의 일치를 확인한다.
- 실제 설치·동일 버전 재설치·제거와 설치된 실행 파일의 자체 검사는 기존 설치가 없는 GitHub Windows CI에서 수행하는 배포 절차로 둔다. 완료 여부와 결과는 해당 실행 보고서·공개 릴리스에서 확인한다.
- 실제 문헌 정확도, 온라인 API 인증, 여섯 로컬 엔진의 실모델 검증은 기능 회귀 검사와 구분해 수행 범위를 기록한다.

```powershell
$check = Start-Process -FilePath .\dist\0.12.2\YeonE_OCR-Windows-x64.exe `
  -ArgumentList '--smoke-test', '"C:\검증\연어OCR-smoke.json"' `
  -WindowStyle Hidden -Wait -PassThru
$check.ExitCode
Get-Content -LiteralPath 'C:\검증\연어OCR-smoke.json' -Encoding UTF8
Get-FileHash -LiteralPath .\dist\0.12.2\YeonE_OCR-0.12.2-Setup-x64.exe -Algorithm SHA256
```

이 자체 검사는 사용자 설정·키와 유료 모델을 사용하지 않는다. 실제 스캔의 글자 정확도, 각 제공사의 온라인 인증, Windows 설치·재설치·제거 검증은 별도이며 실제 수행 여부를 릴리스에 구분해서 기록한다.

`tools/test_windows_installer.ps1`은 같은 AppId의 기존 사용자 설치를 발견하면 중단한다. 이 보호를 해제하거나 설치 경로만 바꿔 우회하지 않는다. 설치 검증은 폐기 가능한 Windows 가상 머신·Windows Sandbox 또는 기존 설치가 없는 CI 환경에서 최종 배포 설치 파일로 수행한다. 검사에서 만드는 외부 파일의 보존 확인은 해당 합성 파일에 대한 검사이며, 실제 사용자 설정·원본·교정 기록 전체를 검증했다는 뜻은 아니다.

## 소스와 공개 배포

개발 소스 저장소는 `kyengilam-coder/YeonE_OCR`, 공개 배포 저장소는 [kyengilam-coder/YeonE_OCR_release](https://github.com/kyengilam-coder/YeonE_OCR_release)다. 0.12.2의 배포 태그는 `v0.12.2`이다. 소스를 커밋한 뒤 다음 명령으로 **그 커밋**의 소스 ZIP·라이선스 ZIP·업데이트 manifest와 전체 SHA-256 목록을 만든다.

```powershell
python tools/prepare_windows_release.py dist/0.12.2 --revision HEAD
```

별도 체크아웃은 `--repo <경로>`를 지정한다. 소스 ZIP은 커밋의 허용 목록만 담고 `SOURCE_REVISION.txt`에 버전과 커밋을 기록한다. 공개 테스트의 골든 JSON·라이선스 보완 자료·AB 소스 보존 사본도 포함하되 개인 자료와 모델은 제외한다. `.github/workflows/windows-release.yml`은 수동 실행 시 검사·빌드·설치 검증과 배포 자료 생성을 거쳐 공개 릴리스를 만든다. 문서에 다운로드 주소가 있다고 게시가 완료된 것은 아니므로, 게시 후 실제 자산과 SHA-256을 대조한다.

`update.json`은 최종 포터블의 정확한 바이트 크기와 SHA-256을 사용한다. 기존 자산 이름·형식을 유지하고 설치 파일을 포터블 자동 업데이트 자산으로 등록하지 않는다. 실행 파일과 함께 그 소스·빌드 스크립트·라이선스 자료를 같은 버전으로 보관한다.

## 제3자 고지와 서명

빌드 가상환경의 Python으로 `tools/build_notices.py`를 실행하면 설치된 패키지의 이름·버전·선언된 라이선스·프로젝트 URL과 원문 자료를 `packaging/THIRD_PARTY_NOTICES.txt`·`packaging/third_party_licenses`에 모은다. Python·Tcl/Tk·PyInstaller 부트로더 고지도 포함한다. 이 고지 목록은 최종 실행 파일의 모듈 목록과 같다는 뜻은 아니다.

`hanja` wheel에서 빠진 원문은 `packaging/license_supplements/hanja`에 출처와 함께 보존한다. 라이선스 이름을 임의로 추정하거나 앱의 새 라이선스를 지정하지 않으며, 기존 `DISTRIBUTION_NOTICE.txt`의 선택과 각 원문 고지를 유지한다.

설치 파일과 실행 파일은 코드 서명되지 않았다. 서명을 추가하면 서명한 최종 파일로 해시와 업데이트 manifest를 다시 만든다. SHA-256은 게시 파일과 내려받은 파일의 바이트 일치를 확인하며 게시자의 신원을 인증하는 코드 서명을 대신하지 않는다.

## 이전 버전 기록

- **0.9.0 Windows 공개판:** 아래 수치는 당시 검증 기록이며 0.12.2의 결과가 아니다. 세부 공개 소스 정리와 추가 검사 결과는 [릴리스 기록](release-notes.md), 당시 자산은 [v0.9.0 릴리스](https://github.com/kyengilam-coder/YeonE_OCR_release/releases/tag/v0.9.0)에 보존한다.
  - 기능 통합 기준으로 61개 모듈·고유 테스트 1,080개가 통과했고 실패·오류·건너뜀·미완료는 0개였다.
  - 소스·설치용 실행 파일·포터블의 오프라인 자체 검사는 각각 26개를 통과했다. 로컬 비교와 편집·복구, AI의 모의 이미지 재검증과 번체·부호 제안, 사람의 채택·되돌리기·재열기를 포함했다.
  - 두 실행 파일의 앱 모듈 각각 75개·명시적 리소스 17개·라이선스 참조 162개가 소스와 일치했다. 당시 보고서는 `work/windows-ai-0.9/verify-build.json`, 자체 검사 보고서는 `work/windows-ai-0.9/smoke/{source,onedir,portable}.json`이다.
  - 실제 연어OCR 창에서 공개 합성 원본·모의 HTTP로 두 단계 AI 검증, 제안 채택·기각·되돌리기, 저장·재열기와 TXT·HWPX·DOCX 출력을 확인했다. 원본·원시 OCR·AI 응답은 유지됐으며 1440×930·1080×720 화면을 확인했다.
  - Gemini·OpenAI·Anthropic의 실제 SDK 직렬화와 거절 응답 보존은 외부 연결을 막은 메모리 HTTP로 확인했다. 실제 유료 API의 인증·문헌 인식 정확도는 이 검사에 포함하지 않았고 당시 개발 검증에서 기존 사용자 설치를 변경하지 않았다.
- **0.8.0 로컬 통합판:** 설치·포터블 자체 검사 각각 25개, 앱 모듈 각각 74개·리소스 15개·라이선스 참조 162개 일치를 확인했다. 원본 설치를 교체하거나 당시 인터넷에 게시하지 않았다. 상세 기록은 [0.8.0 릴리스 기록](release-notes.md)에 보존한다.
- **0.7.4 Windows 공개판:** 자동 테스트 661개와 하위 테스트 154개, 소스·두 실행 파일 자체 검사 각각 21개를 통과했다. 앱 모듈 각각 49개와 라이선스 참조 162개 일치를 확인했고 공개 커밋 `0b59439`의 CI가 통과했다. 이 수치는 이후 버전의 검증 결과와 구분한다. [0.7.4 릴리스](https://github.com/kyengilam-coder/YeonE_OCR_release/releases/tag/v0.7.4)
