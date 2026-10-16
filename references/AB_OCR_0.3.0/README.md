# AB_OCR

한글·한자 문서 인식 정확도를 최우선으로 하는 Windows OCR 편집기 프로젝트입니다.
ABBYY FineReader OCR Editor와 유사한 문서 인식·대조·교정 작업 흐름을 목표로 새로 시작합니다.

사용자의 후속 지시에 따라 제품 기능은 [연어OCR 0.9.0](../YeonE_OCR/README.md)에 통합했습니다.
세로·책갈피·이미지 OCR을 유지하며 세 엔진 비교·원본 대조 편집과 AI 이미지·문맥 재검증을 제공합니다.
이 프로젝트의 0.3.0 소스와 정확도 시험 자료는 참조용으로 보존합니다.

## 현재 상태

**0.3.0 — 여러 로컬 엔진을 사용하는 원본 대조 OCR 교정 편집기**입니다. PDF·이미지·다중 쪽 TIFF를 열어
원본과 교정문을 나란히 보고, Tesseract·RapidOCR·EasyOCR 중 선택한 로컬 엔진으로 인식합니다.
원시 OCR 결과, 사람의 교정문, 이전 인식 결과를 구분하여 프로젝트로 저장·재개합니다.
쪽별 진행·취소와 완료된 쪽의 복구 저장을 지원하며 외부 API를 호출하지 않습니다.

기존 정확도 평가 작업대도 제공합니다. 정답/인식문 대조, 원문·정규화 CER,
한글·한자 오류 분류, 여러 엔진의 같은 문서 평가, HTML/JSON/CSV 보고서를 사용할 수 있습니다.

영역·읽기 순서 편집, AI 독립 재판독, 검색 가능한 PDF·DOCX 출력은 아직 구현하지 않았습니다.
ABBYY와의 실제 성능 비교도 수행하지 않았습니다.
YeonE_OCR와 독립된 소스 및 저장소를 사용합니다.

## 우선순위

1. 한글·한자 혼용, 세로쓰기, 이체자, 저화질 문서의 정답 자료와 정확도 측정 기반 마련.
2. OCR 엔진 비교 및 교체 가능한 판독 구조, 독립 재판독과 불확실 글자 검수.
3. 쪽 목록·원본 영역·교정문을 함께 보는 편집 화면.
4. 프로젝트 저장·재열기, 교정 결과를 검색 가능한 PDF·DOCX 등으로 출력.

## 개발 시작

Python 3.10 이상에서 **AB_OCR.bat** 또는 **AB_OCR.pyw**를 열면 교정 편집기가 실행됩니다.
이 개발 PC에는 Tesseract 5.5.3.20260724와 한글·번체·간체·영어 및 세로쓰기 모델,
RapidOCR와 EasyOCR의 한국어·중국어 모델이 설치되어 있습니다.
엔진은 프로젝트의 `tools/tesseract`, `tools/rapidocr`, `tools/easyocr` 폴더에 있으며
전역 Python 패키지나 PATH를 변경하지 않고 사용합니다.

```powershell
python -X utf8 -m ab_ocr --version
python -X utf8 -m ab_ocr
python -X utf8 -m ab_ocr gui --source "C:\문서\스캔.pdf"
python -X utf8 -m ab_ocr gui --project "C:\문서\교정.abocr.json"
```

1. **원본 열기**로 스캔 PDF 또는 이미지를 선택합니다.
2. 엔진과 언어를 고르고 현재 쪽 또는 전체 쪽의 OCR을 실행합니다. Tesseract에서는 페이지 분할 방식도 선택합니다.
3. 왼쪽 원본을 확대해 보면서 오른쪽 교정문을 수정합니다. 원시 OCR 결과는 별도로 보존됩니다.
4. 프로젝트를 `.abocr.json`으로 저장하면 쪽별 결과와 교정 상태를 이어서 작업할 수 있습니다.
5. 교정문을 TXT로 내보냅니다. 여러 쪽은 `줄바꿈 + form feed + 줄바꿈`으로 구분합니다.

취소한 문서는 완료된 쪽만 골라 내보낼 수 있습니다. **복구 사본 열기**에서는
`work/recovery`에 저장된 마지막 복구 사본을 선택합니다.

언어 목록에서 `kor+chi_tra`(한글·번체), `chi_tra`(번체 단독)도 선택할 수 있습니다.
영어가 없는 문서는 영어 모델을 추가한 조건과 별도로 비교하세요. 한자 전용 설정은 한글 문서용 설정과 구분합니다.
**PDF DPI**는 PDF를 인식용 이미지로 렌더링하는 해상도입니다. 150~600 중 선택하며 기본은 300입니다.
해상도를 높인 조건의 정확도도 실제 정답으로 확인해야 합니다. 인식에 사용한 언어·PSM·DPI는 기록에 남고 프로젝트를 열 때 복원됩니다.

RapidOCR·EasyOCR는 자체 영역 검출을 사용하므로 PSM을 적용하지 않습니다. 한국어(`kor`),
간체(`chi_sim`), 번체(`chi_tra`)를 각각 선택하며 한글+한자 혼용 단일 모델은 제공하지 않습니다.
혼용 자료를 한국어 전용 모델로 인식하면 한자가 빠지거나 바뀔 수 있으므로 원본 대조가 필요합니다.
엔진 선택이나 인식 실행은 모델 다운로드를 유발하지 않으며 설치되지 않은 엔진으로 자동 대체하지 않습니다.

원본 문서는 프로젝트에 내장하지 않습니다. 원본 파일도 함께 보관하세요.
다시 열 때 원본 SHA-256을 확인하며, 내용이 달라진 파일에 기존 결과를 적용하지 않습니다.
개인 문서와 프로젝트는 Git에 추가하지 마세요. `work/`와 `*.abocr.json`은 기본 제외 대상입니다.

## 정확도 평가 화면

```powershell
python -X utf8 -m ab_ocr evaluate-gui
```

화면의 **정답 TXT 열기 → OCR 결과 TXT 열기 → 정확도 비교** 순서로 사용합니다.
오류 행을 누르면 해당 글자 위치가 강조됩니다. [예제]는 평가기 확인용 합성 텍스트이며 실제 엔진 성능이 아닙니다.
UTF-8 입력의 전송용 BOM만 제거하며 줄바꿈·한자·문장부호를 그대로 비교합니다.
한글 자모 결합(NFC)과 공백 처리는 선택한 정규화 점수에만 적용되고 원문 점수는 함께 남습니다.

```powershell
python -X utf8 -m ab_ocr compare 정답.txt 인식문.txt --unicode-form NFC --whitespace remove --output 평가.html
python -X utf8 -m ab_ocr benchmark examples/benchmark/manifest.json --output 예제평가.html
python -X utf8 -m ab_ocr doctor
```

## 로컬 OCR

다른 PC에서는 이미지/PDF 처리용 Python 패키지와 Tesseract 실행 파일·언어 데이터를 준비해야 합니다.
고정 버전과 해시를 확인하는 설치 방법은 [로컬 엔진 설치 안내](docs/LOCAL_OCR.md)를 참고하세요.
다음 첫 명령은 Python 패키지만 설치합니다.

```powershell
python -m pip install -e ".[ocr]"
python -X utf8 -m ab_ocr recognize 스캔.pdf --page 1 --languages kor+chi_tra+eng --output 판독.txt
python -X utf8 -m ab_ocr recognize 스캔.pdf --engine rapidocr --languages chi_sim --page 1 --output 판독-rapidocr.txt
python -X utf8 -m ab_ocr recognize 스캔.pdf --engine easyocr --languages kor --page 1 --output 판독-easyocr.txt
python -X utf8 -m ab_ocr doctor --engine rapidocr
```

- [Tesseract 공식 설치 안내](https://tesseract-ocr.github.io/tessdoc/Downloads.html)
- [언어 데이터 안내](https://tesseract-ocr.github.io/tessdoc/Data-Files.html): 언어·문서 방향에 맞는 모델을 준비합니다.
- 실행 파일은 AB_OCR_TESSERACT 환경 변수 또는 --tesseract로 지정할 수 있습니다.
- 언어 데이터 폴더는 AB_OCR_TESSDATA 또는 --tessdata로 지정할 수 있습니다.
- 세로쓰기는 kor_vert+chi_tra_vert와 --psm 5 등 적절한 조건을 별도로 비교합니다. 해당 언어 데이터가 먼저 필요합니다.
- 요청한 언어가 없으면 중단하며 영어로 자동 대체하지 않습니다.
- 한자 보조 모델에는 기본적으로 번체 `chi_tra`를 사용합니다. 중국어용 모델이므로 한국 한자 문헌 전체의 정확도를 보장하지 않으며, 문서별 검증이 필요합니다.
- 원시 TXT와 좌표·엔진·처리 조건을 담은 .ocr.json을 새 파일로 저장합니다. 기존 출력은 덮어쓰지 않습니다.
- 고비트 회색조는 파생 이미지에서 8비트 대비로 변환하고 조건을 기록합니다. 원본 파일은 바꾸지 않습니다.
- 원시 OCR 텍스트의 교정·간체/번체 치환은 자동으로 수행하지 않습니다.

## 개발 검증

```powershell
python -X utf8 -m unittest discover -s tests -v
```

GUI 검증은 실제 tkinter 화면이 필요하며, 화면이 없는 환경에서는 해당 검증을 건너뜁니다.
실제 엔진이 없는 환경에서는 요청·결과 처리와 실패 경로를 시험하고 실제 인식 정확도와 구분합니다.
2026-10-16 개발 환경에서는 자동 테스트 **134개**, 세 로컬 엔진 설치 검증,
공개 스캔의 OCR·교정·프로젝트 재열기·TXT 내보내기를 확인했습니다.
같은 사용자 자료 65쪽/영역으로 세 엔진을 비교했습니다. 입력·원시 결과·상세 보고서와
교정용 프로젝트는 Git 제외 경로 `work/private-validation/engine-comparison/report.html`에서 확인합니다.
중국어 본문과 한글·한자 혼용의 결과가 달랐으며, 자체 교정은 후보 생성 실험 단계입니다.

- [요구사항](docs/REQUIREMENTS.md)
- [설계 초안](docs/ARCHITECTURE.md)
- [이전 대화에서 이어받은 내용](docs/HANDOFF.md)
- [평가 자료 형식](examples/benchmark/README.md)
- [현재 검증 범위](docs/VALIDATION.md)

## 품질 기준

정답과 OCR 결과의 글자 오류율(CER), 한자 혼동·누락, 읽기 순서, 처리 시간을 함께 측정합니다.
사람이 교정한 결과와 엔진의 원시 결과를 구분하고, 같은 자료·조건으로 비교합니다.
인식 신뢰도 점수를 실제 정확도로 표시하지 않습니다.
