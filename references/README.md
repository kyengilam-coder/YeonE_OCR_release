# 보존된 AB_OCR 소스

`AB_OCR_0.3.0/`은 통합 전 AB_OCR 0.3.0의 공개 소스·문서·합성 예제·시험 52개 파일을 원래 바이트 그대로 보관한 참조 스냅샷입니다. 현재 제품은 상위 폴더의 YeonE_OCR이며, 이 참조 폴더는 제품의 실행 코드나 Windows 실행 파일에 포함되지 않습니다.

원본 AB_OCR에는 Git 커밋이 없었습니다. `SOURCE_MANIFEST.json`은 각 원본 파일의 SHA-256을 기록하며, 배포 소스 ZIP을 만들 때 커밋에 들어 있는 스냅샷의 목록과 해시를 검증합니다. 개인 원본 문서, 정답·인식 결과, 작업 기록, 설치된 엔진 환경 및 모델 가중치는 포함하지 않습니다. 예제와 시험 입력은 기존 합성 자료입니다.

원래 AB_OCR 시험은 현재 제품의 시험과 분리해 이 스냅샷을 작업 폴더로 실행합니다. 외부 API는 사용하지 않습니다. GUI와 PDF·이미지 시험에는 Python의 tkinter, Pillow, PyMuPDF가 필요합니다.

```powershell
Set-Location references/AB_OCR_0.3.0
python -B -X utf8 -m unittest discover -s tests -v
```

현재 제품의 시험은 저장소 최상위에서 `python -B -X utf8 tools/check_regression.py`로 실행합니다. 참조 폴더의 원래 문서에 적힌 계획·실험·버전은 당시 기록이며, 현재 제품 상태는 상위 `README.md`와 `docs/`를 기준으로 확인하세요.
