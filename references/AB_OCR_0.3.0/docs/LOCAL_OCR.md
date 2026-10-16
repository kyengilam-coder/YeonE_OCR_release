# 프로젝트 전용 로컬 OCR 설치

AB_OCR은 `tools/tesseract/tesseract.exe`를 사용하도록 설치할 수 있습니다.
현재 고정한 Windows x64 배포본은 **Tesseract 5.5.3.20260724**이며,
언어 모델은 공식 **tessdata_best** 저장소의
`e12c65a915945e4c28e237a9b52bc4a8f39a0cec` 버전입니다.
모델 이름의 best는 배포 계열 이름입니다. 사용자 문서나 ABBYY 대비 정확도를 보장하는 표시는 아닙니다.

## 설치와 검증

프로젝트 폴더에서 다음을 실행합니다. 기존 Bandizip 또는 7-Zip의 콘솔 실행 파일이 필요합니다.
설치 스크립트는 공식 배포 파일의 해시를 확인한 뒤 필요한 파일만 추출합니다.
Windows 설치 프로그램, 레지스트리 변경, PATH 변경은 실행하지 않습니다.

```powershell
python -X utf8 scripts/install_tesseract.py
python -X utf8 scripts/install_tesseract.py --verify-only
python -X utf8 -m ab_ocr doctor
```

압축 프로그램이 기본 위치에 없으면 `--extractor "C:\경로\7z.exe"`를 지정합니다.
Windows x64 외 환경에서는 해당 운영체제용 Tesseract를 별도로 설치합니다.
Python 이미지/PDF 의존성은 `python -m pip install -e ".[ocr]"`로 설치합니다.

설치 대상은 이 프로젝트의 `tools/tesseract` 한 곳입니다. 다른 설치본이 있거나 관계없는 파일이 든 폴더에는 덮어쓰지 않습니다.
이미 같은 버전이 설치되어 있으면 파일을 해시로 확인하고 필요한 모델만 받습니다.
바이너리와 언어 데이터는 Git에 포함하지 않습니다. 설치 출처, 고정 SHA-256, 모델 버전은
`scripts/tesseract-lock.json`, 실제 설치 기록은 `tools/tesseract/installation.json`에 남습니다.
다운로드 캐시는 `work/downloads`에 둡니다.

## 언어와 처리 조건

### 추가 로컬 엔진 (0.3.0)

이 개발 PC에는 RapidOCR 3.9.2/ONNX Runtime 1.25.1 및 EasyOCR 1.7.2/PyTorch 2.10.0 CPU를
각각 프로젝트 전용 Python 환경에 설치했다. 일반 앱 실행 환경과 무거운 엔진 의존성을 분리한다.
검증한 설치 Python은 Windows x64의 Python 3.14.5다.

```powershell
python -X utf8 scripts/install_rapidocr.py
python -X utf8 scripts/install_easyocr.py
python -X utf8 -m ab_ocr doctor --engine rapidocr
python -X utf8 -m ab_ocr doctor --engine easyocr
python -X utf8 -m ab_ocr recognize 문서.pdf --engine rapidocr --languages chi_tra --page 1 --output 결과-rapidocr.txt
python -X utf8 -m ab_ocr recognize 문서.pdf --engine easyocr --languages chi_sim --page 1 --output 결과-easyocr.txt
```

설치 명령만 공개 패키지·모델을 다운로드한다. 기존 전용 환경의 모델만 다시 확인하려면
각 설치 스크립트에 `--models-only`를 붙인다. 인식은 로컬 파일과 설치한 모델만 사용하며
다운로드나 유료 API 호출을 수행하지 않는다. 설치 출처·패키지 목록·모델 SHA-256은 각
`tools/<engine>/installation.json` 및 패키지 설치 보고서에 보관한다.

- RapidOCR 기본 구성은 PP-OCRv5 server 검출기와 mobile 인식기다. `chi_sim`·`chi_tra`는
  같은 중국어 인식 모델을 사용한다. 입력 문자를 간체나 번체로 강제 치환하지 않는다.
- RapidOCR의 `kor`는 별도의 한국어 인식 모델이다. `+eng` 별칭은 별도 모델 추가가 아니라
  영어가 포함된 해당 모델 경로를 선택한다.
- EasyOCR는 `chi_sim`, `chi_tra`, `kor`를 각각 제공한다. 두 새 엔진 모두 한국어 모델의
  문자 목록에 한자가 없어 한글·한자 혼용을 하나의 한국어 모델로 처리한다고 보장하지 않는다.
- 두 엔진은 검출한 줄/영역 단위 좌표와 인식 신뢰도를 반환한다. 이를 글자별 신뢰도나
  측정한 정확도로 해석하지 않는다. PSM·Tesseract 실행 파일/언어 데이터 옵션은 적용하지 않는다.
- 엔진·언어 변경은 원시 출력과 재인식 이력에 기록된다. 사람의 기존 교정문은 보존한다.
- 여러 엔진을 동시에 실행하면 CPU 처리 시간이 달라지므로 정확도 시험 중 기록한 시간은
  공정한 속도 비교 결과가 아니다.

공식 안내: [RapidOCR 모델](https://rapidai.github.io/RapidOCRDocs/main/en/model_list/),
[PP-OCRv5 문자 인식](https://www.paddleocr.ai/main/en/version3.x/module_usage/text_recognition.html),
[EasyOCR 사용법](https://github.com/JaidedAI/EasyOCR), [EasyOCR 모델](https://www.jaided.ai/easyocr/modelhub/).

### Tesseract 언어 코드

| 코드 | 용도 |
| --- | --- |
| kor | 한국어 |
| chi_tra | 번체 한자 |
| chi_sim | 간체 한자 |
| eng | 영어 |
| kor_vert | 한국어 세로쓰기 |
| chi_tra_vert | 번체 한자 세로쓰기 |
| chi_sim_vert | 간체 한자 세로쓰기 |
| osd | 방향·문자 체계 탐지 |

가로쓰기 혼용문은 `kor+chi_tra+eng`, 세로쓰기 자료는 `kor_vert+chi_tra_vert`와 PSM 5 등
문서에 맞는 조건으로 비교할 수 있습니다. 모든 언어를 한 번에 넣는 것이 항상 더 정확하지는 않습니다.
이 모델들은 LSTM 엔진(OEM 1)을 사용합니다. 원문 한자와 이체자는 인식 후 자동 치환하지 않습니다.
요청한 언어 데이터가 없으면 실행을 중단합니다.

실행 파일 선택 순서는 명시한 경로/`AB_OCR_TESSERACT` → 프로젝트 전용 설치 → PATH → 일반 설치 위치입니다.
언어 데이터는 명시한 경로/`AB_OCR_TESSDATA` → 선택한 실행 파일 옆 `tessdata` 순서로 찾습니다.
각 OCR 결과에는 실제 엔진 버전, 실행 파일 해시, 사용한 언어 모델의 SHA-256, 렌더링 조건을 기록합니다.
미리보기와 인식은 동일한 EXIF 방향·PDF 렌더·다중 TIFF 쪽 선택 규칙을 사용합니다.

## 출처와 라이선스

- [UB Mannheim Windows 배포 안내](https://github.com/UB-Mannheim/tesseract/wiki)
- [Tesseract 5.5.3 공식 릴리스](https://github.com/tesseract-ocr/tesseract/releases/tag/5.5.3)
- [공식 tessdata_best 모델](https://github.com/tesseract-ocr/tessdata_best/tree/e12c65a915945e4c28e237a9b52bc4a8f39a0cec)
- [공식 모델 종류와 OEM 설명](https://tesseract-ocr.github.io/tessdoc/Data-Files.html)

모델은 Apache-2.0으로 제공되며 설치 폴더에 `LICENSE.tessdata_best`를 보관합니다.
엔진 배포물의 라이선스 파일도 `doc` 아래에 함께 추출합니다. 재배포 시 함께 포함된 라이브러리의 라이선스도 확인해야 합니다.

실행 성공이나 합성 문장 OCR 검사는 실제 스캔 자료의 정확도 검증과 구분합니다.
한글·한자 혼용, 작은 글씨, 이체자, 저화질·세로쓰기 문서는 사람이 확인한 정답과 함께 평가해야 합니다.
