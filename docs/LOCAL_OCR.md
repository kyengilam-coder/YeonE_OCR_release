# 로컬 세 엔진 실행 환경

연어OCR 0.9.0은 AB_OCR 0.3.0의 인식·원본 대조 기반을 포함합니다. 사용자 문서와 모델 가중치는 소스나 배포물에 포함하지 않습니다. 재사용한 소스의 원래 SHA-256은 `AB_OCR_PROVENANCE.json`에 기록했습니다. 로컬 인식 뒤 실행하는 별도 AI 검증은 [AI_REVIEW.md](AI_REVIEW.md)를 따릅니다.

Tesseract, RapidOCR, EasyOCR는 서로 다른 원시 판독을 반환합니다. 로컬 인식 중 API를 호출하거나 모델을 다운로드하지 않습니다. RapidOCR·EasyOCR는 별도 Python 환경의 작업자를 실행하며, 작업자의 네트워크 연결을 차단합니다. 모델 파일이 없거나 해시가 달라지면 해당 엔진을 오류로 표시하며 다른 엔진·언어로 자동 대체하지 않습니다.

## 엔진 폴더 선택

엔진 폴더는 다음 구조입니다. 설정에서 고르는 경로는 `tools`를 포함하는 **상위 폴더**입니다.

```text
선택한 폴더/
  tools/tesseract/tesseract.exe
  tools/tesseract/tessdata/*.traineddata
  tools/tesseract/installation.json
  tools/rapidocr/venv/Scripts/python.exe
  tools/rapidocr/models/*.onnx
  tools/rapidocr/installation.json
  tools/easyocr/venv/Scripts/python.exe
  tools/easyocr/models/*.pth
  tools/easyocr/installation.json
```

탐색 순서는 화면에서 선택·저장한 폴더 또는 호출에 명시한 폴더 → `YEONE_OCR_ENGINE_ROOT` 환경 변수 → 호환용 `AB_OCR_ENGINE_ROOT` 환경 변수 → 연어OCR 프로그램 폴더의 설치 → 사용자 설치 폴더입니다. 소스 개발 환경에서는 마지막으로 같은 상위 폴더의 기존 `AB_OCR` 설치도 탐색합니다. 설치판은 이 개발용 탐색을 하지 않습니다. 화면에서 폴더를 선택하면 기존 환경 변수보다 우선하며, 명시한 폴더가 준비되지 않았을 때는 다른 폴더로 넘어가지 않습니다.

연어OCR에서 [엔진 폴더…]로 고른 위치는 공통 설정에 기억하며 다음 비교 창에도 적용합니다. 엔진 폴더를 선택하거나 설치 상태를 확인하는 동작은 모델 다운로드를 실행하지 않습니다.

기존 AB_OCR 설치를 선택해도 Python 코드는 연어OCR에 포함된 작업자를 실행합니다. 기존 설치의 실행 환경·공개 모델만 읽으며, 다른 프로젝트 소스나 사용자 문서를 가져오지 않습니다. 대용량 가중치를 복사할 필요가 없습니다. 실행마다 선택 경로를 함수에 전달하므로 병렬 작업이 전역 환경 변수를 바꾸지 않습니다.

기본 사용자 설치 폴더는 `%LOCALAPPDATA%\YeonE_OCR\local-engines`입니다. 새 PC에는 아래 설치 작업이 필요합니다. 일반 연어OCR의 AI 판독·책갈피·이미지 입력은 로컬 엔진 설치와 독립적으로 사용할 수 있습니다.

## 새 PC에 설치

설치 스크립트는 사용자가 실행할 때 공개 패키지·모델만 다운로드합니다. 모델의 SHA-256, 실제 패키지 버전, 내려받은 패키지의 URL·해시를 설치 폴더에 기록합니다. Windows x64 Python 3.14.5에서 검증했으며, 신경망 엔진의 고정 의존성은 별도 환경에 설치합니다. Tesseract 배포 압축을 풀 때는 기존 Bandizip 또는 7-Zip이 필요합니다.

소스 폴더에서 PowerShell로 실행합니다. 설치판에도 `scripts`와 이 문서를 허용 목록으로 포함하며, 설치 스크립트 실행에는 별도 Python이 필요합니다.

```powershell
$ocrRoot = Join-Path $env:LOCALAPPDATA 'YeonE_OCR\local-engines'
py -3.14 scripts/install_tesseract.py --root $ocrRoot
py -3.14 scripts/install_rapidocr.py --root $ocrRoot
py -3.14 scripts/install_easyocr.py --root $ocrRoot
```

`--root`는 소스 프로젝트일 필요가 없습니다. Tesseract는 레지스트리·PATH를 변경하거나 관리자 설치 프로그램을 실행하지 않고, 검증한 배포 파일을 전용 폴더에 풉니다. RapidOCR와 EasyOCR는 `venv`에 설치합니다. 설치 중 다운로드에는 인터넷이 필요합니다. 인식 중에는 인터넷이 필요하지 않습니다.

런타임 점검 예:

```python
from ab_ocr.ocr import doctor
from ab_ocr.runtime import default_engine_root

root = default_engine_root()
for engine in ('tesseract', 'rapidocr', 'easyocr'):
    status = doctor(engine=engine, engine_root=root)
    print(engine, status['available'], status['languages'])
```

## 언어·판독 결과 보존

- Tesseract: 한글·중문·영문 조합을 지원합니다. 언어팩 목록을 실제 설치에서 확인합니다. PSM은 이 엔진 전용입니다.
- RapidOCR 3.9.2 / ONNX Runtime 1.25.1: PP-OCRv5 검출 모델과 mobile 인식 모델을 기본으로 사용합니다. `chi_sim`과 `chi_tra`는 같은 중국어 모델을 쓰며, 간체·번체 변환은 적용하지 않습니다. 한국어 모델은 별도입니다.
- EasyOCR 1.7.2 / PyTorch 2.10.0+cpu: `chi_sim`, `chi_tra`, `kor`를 각각 지원합니다. CPU로 실행하고 다운로드 옵션을 끕니다.

두 신경망 엔진의 한국어 모델은 한자를 지원하지 않습니다. 한글·한자 혼용에서 두 모델의 같은 누락을 정답 근거로 삼으면 안 됩니다. 세로 문헌도 가로 문헌과 별도로 품질을 검증해야 합니다. 엔진의 confidence 수치는 실측 정확도가 아니며 서로 직접 비교할 수 없습니다.

원시 문자열은 엔진의 Unicode와 부호를 보존합니다. 결과에는 엔진·모델 해시, 언어, 렌더링 크기·DPI, 좌표와 전처리 정책을 기록합니다. 교정·번체 변환·부호 편집은 별도 기록에서 수행해야 합니다. 원본이 실행 중 바뀌거나 좌표가 유효하지 않으면 적용을 중단합니다. 취소·시간 초과 시 작업자 프로세스를 종료합니다.

세 엔진은 한 쪽 안에서 순서대로 실행합니다. Tesseract의 OpenMP 스레드는 자식 프로세스에서 최대 8개로 제한하고, 사용자가 더 낮게 설정했으면 그 값을 따릅니다. RapidOCR는 내부 실행 스레드 최대 8개, EasyOCR는 2개를 사용합니다. 프로그램 전체 환경 변수는 바꾸지 않습니다.

공식 모델·라이선스 정보:

- Tesseract: https://tesseract-ocr.github.io/tessdoc/Downloads.html
- RapidOCR: https://github.com/RapidAI/RapidOCR
- EasyOCR: https://github.com/JaidedAI/EasyOCR
- PyTorch CPU 배포: https://download.pytorch.org/whl/cpu

새 배포물은 위 엔진의 실행 환경·모델 자체를 묶지 않습니다. 설치 스크립트·잠금 기록을 포함하며, 설치한 구성요소의 라이선스는 각 엔진 설치 폴더와 공식 배포 자료를 따릅니다.
