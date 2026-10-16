# 같은 문서로 여러 OCR 결과 평가하기

이 폴더의 manifest와 텍스트는 **평가기 동작 확인용 합성 자료**입니다.
실제 OCR 엔진의 출력, 실제 스캔, ABBYY 성능 측정 자료가 아닙니다.

명령: python -X utf8 -m ab_ocr benchmark examples/benchmark/manifest.json --output 평가.html
또는 화면의 [여러 엔진 일괄 평가]에서 manifest.json을 선택합니다.

## 실제 자료 준비

별도 폴더에 정답 UTF-8 TXT와 같은 쪽·영역·읽기 순서의 엔진별 TXT를 넣고 manifest.json을 만듭니다.
정답은 원본과 대조하여 작성하고 엔진 결과를 정답으로 복사하지 않습니다.
개발용(dev)·최종 비교용(test) 자료를 구분하고, 각 엔진의 버전과 설정을 기록합니다.
태그로 가로/세로, 혼용, 작은 주석, 표, 저화질 등을 표시할 수 있습니다.

```json
{
  "schema_version": 1,
  "name": "한글·한자 검증 문서",
  "synthetic": false,
  "engines": [
    {"id": "engine-a", "name": "선택한 엔진", "version": "실제 버전", "settings": {"language": "실제 언어"}}
  ],
  "pages": [
    {"id": "book-001", "reference": "truth/page001.txt",
     "outputs": {"engine-a": "outputs/page001.txt"},
     "tags": ["mixed", "vertical"], "split": "test"}
  ]
}
```

필드 제약은 같은 폴더의 manifest.schema.json을 참조합니다.
파일 경로는 manifest 폴더 안의 상대 경로여야 합니다. 외부 경로와 심볼릭 링크를 통한 폴더 이탈은 거부합니다.
--split test로 고정 비교 자료만 평가할 수 있습니다.

## 점수 해석

- 원문 CER와 선택한 정규화 CER를 함께 보고합니다. 한자 이체자·간체·번체는 합치지 않습니다.
- 총 CER는 오류 합계 / 정답 글자 합계입니다. 쪽별 CER의 단순 평균이 아닙니다.
- 빈 정답 CER는 null입니다. 빈 쪽에 생성된 글자는 삽입 오류로 따로 기록하고 전체 오류 합계에도 넣습니다.
- 누락 파일, 손상된 UTF-8, 계산 한도 초과 등을 빈 결과나 성공으로 처리하지 않습니다.
- 누락·실패가 있으면 엔진 순위를 제시하지 않습니다. 완료한 쪽만의 부분 점수와 완료 범위를 표시합니다.
- SHA-256은 실제 파일 바이트를 대상으로 합니다. 선두 UTF-8 BOM 제거 여부와 줄바꿈 정책을 기록합니다.
- 현재 CER는 Unicode 코드 포인트 단위입니다. 표 셀 정확도·읽기 순서의 별도 구조 평가는 아직 없습니다.
- 긴 문서는 쪽·영역 단위로 나누십시오. 정렬 계산 한도를 넘으면 근사값 없이 실패로 보고합니다.

실제 사용자 자료는 data/private/ 등 Git 제외 경로에서 관리하십시오.
