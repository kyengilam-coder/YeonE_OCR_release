# GLM-OCR 로컬 전사 엔진

`ab_ocr/glmocr_worker.py`는 공개 GLM-OCR 모델을 별도 Ollama 자식 서버에서 실행한다. 문서는 작업자 내부에서 만든 `127.0.0.1` 서버로만 전달한다. 외부 OCR API, 계정, API 키를 사용하지 않는다. 설치 과정에서만 공개 실행 파일과 모델을 다운로드한다.

공식 [GLM-OCR 배포 안내](https://github.com/zai-org/GLM-OCR/blob/main/examples/ollama-deploy/README.md)에 따라 Ollama native `/api/generate`와 `Text Recognition:` 프롬프트를 사용한다. 이 연결은 모델의 영역 전사 기능이다. PP-DocLayoutV3를 포함한 전체 문서 분석 SDK를 실행한 것으로 해석하지 않는다.

## 설치

Windows x64에서 다음 명령으로 새 폴더에 설치한다. 현재 잠금 파일은 Ollama **0.40.5**, Pillow **12.3.0**, 공식 Ollama library **`glm-ocr:bf16`**를 고정한다. 모델 이름의 bf16 표기와 달리 이 배포판의 실제 GGUF 메타데이터는 **F16**이며 결과에도 이 정보를 보존한다. 원래 Hugging Face BF16 모델과 실행 조건이 같다고 주장하지 않는다.

```powershell
python scripts/install_glmocr.py --target tools/glmocr
```

설치기는 공식 배포 ZIP과 추출 실행 파일·라이브러리, 모델 blob·manifest의 SHA-256을 확인한다. 원격 태그가 잠금 파일의 모델 digest와 달라졌다면 설치를 중지한다. 다른 모델로 자동 대체하지 않는다. 실행 시에도 이 해시를 다시 확인한다. 다운로드와 설치 파일은 Git 및 앱 배포물에서 제외한다.

## 직접 실행

```powershell
tools/glmocr/venv/Scripts/python.exe -I -X utf8 ab_ocr/glmocr_worker.py --model-dir tools/glmocr/models --input input.png --output reading.json --languages kor+chi_tra --device cuda
```

CPU는 `--device cpu`로 명시한다. 기본값은 CUDA, 스레드는 8개, 출력 상한은 4,096 token, 컨텍스트는 8,192 token이다. `temperature=0`, `seed=0`이며 원래 문자·간체·번체·이체자·문장부호·공백을 후처리로 바꾸지 않는다. 모델 자체의 오독·누락이나 생성된 서식은 원시 결과에 그대로 남는다.

작업자는 기존 Ollama 서버를 재사용하지 않는다. 비어 있는 임의 loopback 포트에서 검증한 실행 파일을 직접 시작하고, 자기 모델 캐시만 사용하며, cloud 기능과 서버 출력 로그를 끈다. 완료·예외 처리에서 자신이 만든 서버를 종료한다. 프로그램의 작업 취소·시간 초과 처리는 작업자 자식 프로세스 전체의 정리와 함께 사용해야 한다.

`--batch request.json`은 `{"items": [{"input": "input.png", "output": "reading.json", "languages": "chi_tra"}]}` 형식을 받는다. 완료한 결과를 원자적으로 별도 저장하고 기존 결과 파일을 덮어쓰지 않는다. 진행 출력에는 문서 내용이나 경로가 들어가지 않는다.

## 결과와 보존 정책

`text`는 모델이 생성한 원문 그대로다. 모델 digest, 파일 해시, 입력 이미지 해시, 실제 보고 GPU 메모리, 생성·입력 token 수, 완료 사유와 실행 조건은 `engine_metadata`에 보관한다. 출력 상한으로 끝난 응답은 `generation_complete=false`로 표시하고 원시 증거로 남긴다. 정상 판독이나 교정 근거로 자동 승격하지 않는다.

이 전사 모델은 문자·행 좌표나 confidence를 반환하지 않는다. `words=[]`, `bbox_granularity=none`, `confidence_granularity=none`으로 보관하고, 입력 영역 경계는 별도 메타데이터에 둔다. 균등 분할한 가짜 글자 좌표나 만들어 낸 신뢰도를 제공하지 않는다. 원본 이미지와 문장 대조는 가능하지만 해당 엔진만으로 정밀 글자 위치를 표시할 수는 없다.

원시 OCR은 AI 제안이나 사람의 확정 이력과 별개다. 엔진 교체·새 판독은 기존 원문, 제안 또는 사람의 결정을 덮어쓰지 않는다. 모델 카드의 공개 벤치마크와 실제 사용자 문헌의 정확도는 구분한다. 대표 정답과 같은 범위로 측정한 결과를 보고 주력 엔진을 선정한다.

## 공식 자료와 라이선스

- [GLM-OCR 모델 카드](https://huggingface.co/zai-org/GLM-OCR): 모델 MIT, 언어 목록과 정해진 전사 프롬프트.
- [GLM-OCR SDK](https://github.com/zai-org/GLM-OCR): 별도 SDK·레이아웃 분석 코드 Apache-2.0. 현재 작업자는 SDK 코드를 가져오지 않는다.
- [Ollama GLM-OCR library](https://ollama.com/library/glm-ocr): 실제 모델 배포 조건.
- [Ollama MIT 라이선스](https://github.com/ollama/ollama/blob/v0.40.5/LICENSE): 별도 설치하는 실행 환경.
