# 0.10.0 설치판 검증 · 2026-10-24

`dist/0.10.0/YeonE_OCR-0.10.0-Setup-x64.exe`에 PP-OCRv6 medium 검출·인식 모델과 독립 Python 3.13.15, RapidOCR 3.10.0, ONNX Runtime 1.25.1, Microsoft C++ Runtime 14.51.36247을 포함했다. 기존 앱의 AppId를 유지하고 버전을 0.9.0에서 0.10.0으로 올렸다. GitHub에는 게시하지 않았다.

이 PC의 한글·공백이 포함된 별도 경로에 실제 설치하고 아래 항목을 검증했다. 검증용 설치는 제거했으며 사용자 자료 보존을 확인했다. 새 PC의 깨끗한 VM 검증과는 구분한다.

- 전체 회귀 검사: 70개 모듈, 고유 테스트 1,175개 통과. 실패·오류·건너뜀·미완료 0개.
- 소스·설치용 실행 파일·포터블·실제 설치본의 오프라인 자체 검사: 각각 27개 통과.
- 설치·재설치·레지스트리 버전·제거·외부 합성 사용자 자료 보존 통과.
- 설치된 엔진 묶음 2,748개 파일의 목록·크기·SHA-256 검증 통과.
- 네이티브 DLL·확장 모듈 98개를 검사해 누락된 C++ 런타임 의존성 0개 확인.
- PATH를 Windows System32로 제한한 실행에서 Python 모듈 경로와 실제 로드된 MSVCP/VCRUNTIME DLL이 설치 폴더 안에 있음을 확인.
- 설치된 EXE의 실제 CLI와 내장 작업자로 기존 PDF 3쪽 및 추가 6개 조건을 실행. 706자에서 공백만 제외한 문자 오류 0개. 번체 가로쓰기는 `ppocrv6-traditional-original-crop-verified-v2` 적용 확인.
- 추가 6개 조건은 원래 Pillow 환경에서 재구성한 이미지와 픽셀 단위로 동일했다. 원시 모델의 기존 4개 오류와 개선 후 0개 결과도 동일했다.

기존 PDF SHA-256: `57863bf04cb406ad06e568974c5f57ec86f21da8fa8ddec885350756998a9db5`.

합성 테스트 결과는 모든 실제 스캔의 정확도를 보장하지 않는다. Windows 글꼴·사용자 문서·설정은 설치 파일에 포함하지 않았다. 기존에 저장한 엔진 폴더는 유지하므로 내장 엔진을 사용하려면 새 설치 폴더를 선택한다. 포터블 단일 EXE에는 PP-OCRv6 환경이 포함되지 않는다.

검증 결과와 실행 파일 해시는 `dist/0.10.0/verification.json`, `SHA256SUMS.txt`에 기록했다. 원본 보고서는 `work/windows-0.10/regression-final/summary.json`, `installation-check/result.json`, `installation-check/ocr/result.json`, `installation-check/ocr/conditions-audit.json`에 보관한다.
