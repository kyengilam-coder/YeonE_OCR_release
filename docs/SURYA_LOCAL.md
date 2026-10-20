# Surya 로컬 엔진

이 어댑터는 공식 `surya-ocr==0.22.1`의 **Surya OCR 2 (650M)**와 공식 GGUF를 사용한다. 이전 Surya 1의 행 인식 모델과 구분한다. 모델 리비전은 `6a3a4c30e5e74446d4f8b6afd05b2f2da970f470`, llama.cpp는 `b11684`에 고정한다. 공개 가중치·설치 환경은 Git에서 제외한 `tools/surya`에 설치한다.

```powershell
python scripts/install_surya.py
# NVIDIA GPU의 CUDA 12.4 런타임을 추가 설치하되 아직 선택하지 않음
python scripts/install_surya_cuda.py
# CUDA 백엔드를 명시적으로 선택할 때
python scripts/install_surya_cuda.py --activate
```

앱의 Python 환경과 분리한 CPython 3.12.10을 사용한다. 이는 Surya의 `Pillow<11` 요구를 바이너리 패키지로 만족시키기 위한 선택이며, 앱이나 다른 엔진의 패키지를 변경하지 않는다. `scripts/surya-lock.json`은 모델·Python·llama.cpp 공개 배포 파일의 SHA-256을, `scripts/surya-requirements.txt`는 실제 설치한 Python 패키지 버전을 고정한다. 설치 영수증에는 모델, 실행 바이너리, Surya 패키지 소스의 해시를 기록한다. 모델과 런타임을 인식 중 자동으로 내려받지 않는다.

작업자는 검증한 자기 `llama-server`를 새 `127.0.0.1` 포트로 실행하고 종료 때 자기 프로세스만 닫는다. 다른 서버를 탐색하거나 재사용하지 않는다. Python 연결은 이 포트로 제한하며 외부 API·API 키를 사용하지 않는다. CPU와 CUDA 모델 파일은 같다. CUDA를 선택할 때는 설치된 NVIDIA 드라이버와 가용 VRAM도 필요하다.

공식 `RecognitionPredictor`의 전체 이미지 판독과 기본 파서·레이아웃 대체 처리를 사용한다. 재생성은 끄고 HTTP·클라이언트 재시도는 0회로 고정한다. 원래 모델 응답의 HTML과 공식 파싱 결과, 블록 순서·좌표를 보존한다. 편집용 텍스트는 HTML 엔티티와 문단·행·표 구분만 해석한 파생 보기이며, 변환 정책과 출력 해시를 기록한다. 간체·번체·이체자·문장부호를 정규화하지 않는다.

큰 토큰별 확률 벡터를 프로젝트에 쌓지 않도록 `logprobs`는 요청하지 않는다. 사용자에게 노출하는 confidence는 `null`이다. 공식 파서가 넣는 기본 `confidence=1.0`은 보존한 원형 파싱 기록에 남지만, 측정된 확률이나 정확도로 사용하지 않는다. 모델의 좌표는 글자 단위가 아닌 **블록 단위**이다. 모델이 길이 제한으로 끝난 결과는 정상 OCR로 채택하지 않고 실패 원문 증거를 별도로 보존한다.

현재 모델 가중치는 공식 Modified AI Pubs Open Rail-M 라이선스를 따른다. 소스 코드의 Apache-2.0과 구분하며, 가중치를 앱 배포물에 포함하지 않는다. 실제 문헌 정확도는 같은 입력 이미지와 고정 정답으로 별도 평가해야 하며 공개 벤치마크 수치를 사용자 문헌의 정확도로 사용하지 않는다.

공식 자료: [Surya 소스·사용법](https://github.com/datalab-to/surya), [공식 GGUF 모델](https://huggingface.co/datalab-to/surya-ocr-2-gguf), [llama.cpp 배포](https://github.com/ggml-org/llama.cpp/releases/tag/b11684).
