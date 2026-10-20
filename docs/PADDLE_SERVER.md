# PaddleOCR 서버형 로컬 인식

`paddle_server`는 PP-OCRv5의 서버 크기 검출·인식 모델을 로컬에서 실행하는 별도 엔진이다. 외부 OCR 서버에 문서를 보내는 기능이 아니다. RapidAI가 제공하는 고정 ONNX 변환 모델을 RapidOCR 3.9.2와 ONNX Runtime 1.25.1로 실행한다. 기존 `rapidocr`의 모바일 인식 모델과 설치 폴더·엔진 식별자를 구분한다.

## 언어와 원문 보존

서버 인식 모델은 간체·번체·영어·일본어용이다. 한국어 서버 인식 모델은 제공하지 않으며, 한글·한자 혼용을 완전히 지원한다고 표시하지 않는다. `kor` 또는 `kor+eng`를 서버 프로필에 지정하면 오류로 중단한다. 한국어 모바일 모델이나 다른 엔진으로 자동 대체하지 않는다. 한글이 있는 이미지를 중국어 서버 모델로 시험한 결과는 한글 미지원이라는 조건과 함께 비교해야 한다.

`chi_sim`, `chi_tra`, `chi_sim+eng`, `chi_tra+eng`는 같은 서버 인식 모델을 사용한다. 언어 선택을 근거로 간체·번체·이체자·문장부호를 변환하지 않는다. 인식된 줄의 원시 문자열과 줄 좌표, 신뢰도, 실제 모델 해시·실행 제공자·시간을 보존한다. 원시 OCR, AI 응답·제안, 사람이 확정한 교정은 기존 편집기의 별도 이력 정책을 따른다.

## 설치

설치만 공개 패키지와 모델을 다운로드한다. 문서는 읽지 않는다. CPU 설치는 기존 모바일 환경을 변경하지 않는 `tools/paddle_server/venv`를 만든다.

```powershell
python scripts/install_paddle_server.py --root C:\Projects\YeonE_OCR
```

기존 공개 RapidOCR 모델을 재사용하려면 `--model-cache`로 모델 폴더를 지정한다. 파일명과 고정 SHA-256이 일치하는 모델만 새 폴더에 복사하며 원래 폴더를 수정하지 않는다.

NVIDIA CUDA 실행 환경은 선택적으로 별도 설치한다. CPU 환경과 시스템 드라이버를 변경하지 않는다.

```powershell
python scripts/install_paddle_server_gpu.py --root C:\Projects\YeonE_OCR --model-cache C:\Projects\YeonE_OCR\tools\paddle_server\models
```

GPU 환경은 `tools/paddle_server_gpu/venv`에 둔다. ONNX Runtime GPU 1.25.1과 NVIDIA 공식 CUDA 12.8 런타임·NVRTC·cuBLAS·cuFFT·cuDNN 9·nvJitLink를 정확한 버전으로 설치한다. 의존성은 `scripts/paddle-server-gpu-lock.json`, 다운로드한 실제 휠의 URL·해시는 해당 설치 폴더의 `pip-gpu-install-report.json`에 남긴다. CUDA 12.8 이상을 지원하는 NVIDIA 드라이버가 필요하며 설치기가 드라이버를 설치하거나 바꾸지는 않는다.

### 편집기에서 CUDA 사용·CPU 복원

먼저 CPU 설치가 존재하는지 확인한 뒤 다음 명령으로 CUDA 사용을 명시적으로 활성화한다.

```powershell
python scripts/install_paddle_server_gpu.py --root C:\Projects\YeonE_OCR --model-cache C:\Projects\YeonE_OCR\tools\paddle_server\models --activate
```

활성화는 CPU의 원래 `installation.json` 바이트를 `installation-cpu.json`에 보존한다. 기존 `venv`와 별도 GPU 원본 환경을 그대로 두고 GPU 환경을 `tools/paddle_server/venv_cuda`로 검증 복사한다. CPU·CUDA의 세 모델 해시가 모두 같아야 하며, Python 실행 파일·패키지 코드·ORT/NVIDIA DLL 등 고정 파일을 검증한 뒤에만 canonical `installation.json`을 원자적으로 바꾼다. 이 manifest의 `runtime_files`에 파일별 SHA-256을 기록해 설치 점검과 작업 재개에서도 검증한다. 실행 중 바뀌는 `.pyc`·캐시·로그·pip 보고서는 런타임 지문에서 제외한다. 앱의 엔진 폴더를 같은 루트로 지정하고 `PaddleOCR server`를 선택하면 명시된 CUDA 환경을 실행한다. CPU로 자동 대체하지 않는다.

`cuda-activation.json`에 원래 CPU·활성 CUDA의 manifest 해시, 복사 환경의 해시와 활성화·복원 이력을 기록한다. 설치 기록이나 이미 존재하는 CUDA 환경이 예상 내용과 다르면 덮어쓰지 않고 중단한다. manifest 전환 직후 이력 파일 쓰기가 중단된 경우에도 원래 CPU 백업·활성화 identity·고정 런타임의 해시가 모두 일치할 때 CPU 복원을 지원한다. CPU 선택으로 되돌릴 때는 다음 명령을 사용한다. 다운로드 없이 원래 CPU manifest 바이트를 복원하고 GPU 파일은 보존한다.

```powershell
python scripts/install_paddle_server_gpu.py --root C:\Projects\YeonE_OCR --restore-cpu
```

## 명시적 CPU·CUDA 실행

아래 명령은 설치 점검용 작업자 실행이다. 입력은 로컬 이미지 파일이어야 하며, 이미 존재하는 결과 JSON은 덮어쓰지 않는다.

```powershell
.\tools\paddle_server\venv\Scripts\python.exe -I -X utf8 ab_ocr\rapidocr_worker.py --model-dir tools\paddle_server\models --profile server --provider cpu --input C:\문서\시험.png --output C:\문서\새결과-cpu.json --languages chi_tra
```

```powershell
.\tools\paddle_server_gpu\venv\Scripts\python.exe -I -X utf8 ab_ocr\rapidocr_worker.py --model-dir tools\paddle_server_gpu\models --profile server --provider cuda --input C:\문서\시험.png --output C:\문서\새결과-cuda.json --languages chi_tra
```

CUDA를 선택하면 검출·방향·인식 세 세션 모두 실제 `CUDAExecutionProvider`를 사용하는지 확인한다. NVRTC와 builtins DLL도 시작할 때 설치된 고정 NVIDIA 패키지의 절대 경로로 로드해, 긴 글줄에서 필요한 런타임이 뒤늦게 빠지는 경우를 막는다. CUDA가 준비되지 않으면 CPU로 자동 대체하지 않고 오류로 중단한다. 모델은 동일한 float32 ONNX 파일이며 TF32는 끈다. 결과 메타데이터에 실제 제공자와 설정을 기록하므로 CPU·CUDA 실행 결과와 속도를 구별할 수 있다. 작업자 추론 중에는 네트워크 연결과 ONNX Runtime 원격 측정을 차단한다.

## 모델 고정

서버 인식에 필요한 세 모델은 다음과 같다. 모델 파일은 소스·배포물에 넣지 않고 명시적 설치로 준비한다.

| 역할 | 파일 | SHA-256 |
|---|---|---|
| 검출 | `ch_PP-OCRv5_det_server.onnx` | `0f8846b1d4bba223a2a2f9d9b44022fbc22cc019051a602b41a7fda9667e4cad` |
| 줄 방향 | `ch_PP-LCNet_x0_25_textline_ori_cls_mobile.onnx` | `54379ae5174d026780215fc748a7f31910dee36818e63d49e17dc598ecc82df7` |
| 서버 인식 | `ch_PP-OCRv5_rec_server.onnx` | `e09385400eaaaef34ceff54aeb7c4f0f1fe014c27fa8b9905d4709b65746562a` |

줄 방향 분류기는 모바일 크기 모델이며 문자 인식기는 서버 크기 모델이다. 기존 RapidOCR와 비교할 때 검출·방향 모델과 이미지 조건을 유지하고 문자 인식 모델의 차이를 기록한다. 검출 줄의 순서는 문서 전체의 표·단·세로 읽기 순서를 복원했다는 뜻이 아니다. 다른 엔진보다 높은 한글·한자 정확도를 주장하려면 같은 이미지와 별도 정답으로 측정한 결과가 필요하다.

공식 근거: [PaddleOCR 인식 모델과 언어 범위](https://www.paddleocr.ai/latest/en/version3.x/module_usage/text_recognition.html), [RapidOCR 3.9.2 모델·해시](https://github.com/RapidAI/RapidOCR/blob/v3.9.2/python/rapidocr/default_models.yaml), [ONNX Runtime CUDA 요구 사항](https://onnxruntime.ai/docs/execution-providers/CUDA-ExecutionProvider.html).
