# 구독 계정으로 AI 판독·검증: Claude · ChatGPT · Gemini

로컬 우선 인식에서 AI가 하는 두 가지 일 — **쪽 판독**(`--local-ai-page`)과 **의심 구간 검증** — 을 API 키 없이
자기 구독 계정으로 할 수 있다. 세 회사 모두 **공식 명령줄 프로그램에 자기 계정으로 로그인**하는 방식이다.

| 구독 | 실행하는 공식 프로그램 | 설치 (PowerShell) | 로그인 |
| --- | --- | --- | --- |
| Claude (Pro/Max) | Claude Code `claude -p` | `irm https://claude.ai/install.ps1 \| iex` | `claude auth login` |
| ChatGPT (Plus/Pro/Business/Edu/Enterprise) | OpenAI Codex CLI `codex exec` | `npm install -g @openai/codex` | `codex login` → **Sign in with ChatGPT** |
| Gemini (Google 계정: 무료, Google AI Pro/Ultra, Code Assist) | Gemini CLI `gemini -p` | `npm install -g @google/gemini-cli` | `gemini` 실행 → **Login with Google** → `/quit` |

Codex CLI와 Gemini CLI는 Node.js(20 이상)가 필요하다. 로그인 상태는 `yeone-ocr plans`로 확인한다
(상태 명령만 실행하며 모델은 부르지 않는다).

## 고르기

- 설정 탭 「구독 계정 판독」: 체크(기본 켬)와 계정 선택 — 자동 / Claude / ChatGPT / Gemini.
- 명령줄: `--local-ai-plan auto|claude|chatgpt|gemini`, 환경 변수 `YEONE_OCR_AI_PLAN`.
- **자동**은 로그인된 것 중 Claude → ChatGPT → Gemini 순으로 고른다. Claude가 먼저인 까닭은 2026-11의 기본 설정
  (쪽 판독 prefer, 4구간씩 1024화소 조각, 확신한 제안 반영)을 Claude로 쟀기 때문이다. ChatGPT·Gemini에서도 같은
  설정을 쓰지만 정확도는 아직 재지 않았다.
- 계정을 고르면 그 계정부터 쓰고, 쓸 수 없으면 나머지를 위 순서대로 쓴다.
- 끄기: 체크 해제, 또는 `--local-ai-page off --local-ai-auth api`.

## 사용량 한도에 닿으면: 다른 구독 → API 키

실행 중에 쓰던 구독 계정이 사용량 한도(5시간·주간·일일)에 닿거나 로그인이 풀리거나 프로그램을 실행할 수 없으면,
**같은 요청을 로그인된 다음 구독 계정으로 다시 보낸다**(Claude → ChatGPT → Gemini 순, 이번 실행에서 실패한 계정은 건너뜀).
쪽 판독과 의심 구간 검증이 한 연결을 같이 쓰므로, 한쪽에서 넘어가면 다른 쪽도 넘어간 계정을 쓴다.

**쓸 수 있는 구독 계정이 하나도 남지 않으면 바로 API 키로 넘어간다.** 설정 탭에서 고른 제공사와 모델, 그 API 키를 쓰며,
이번 실행의 API 호출은 「로컬 뒤 AI 검증」의 호출 수(`--local-ai-max-calls`, 기본 20)까지다 — 쪽 판독과 검증을 합해 센다.
처음부터 로그인된 구독 계정이 없을 때도 API 키가 있으면 그것으로 검증한다(쪽 판독은 이때 기본으로 끈다).
API 키도 없거나 호출 상한에 닿으면 AI 판독·검증을 멈추고 결과와 검증 대상을 저장한다. 넘어갈 때마다 진행 기록에 적는다.

API 요금을 쓰지 않으려면 `--no-local-ai-api-fallback`. 한도가 풀렸는지는 다음 실행에서 다시 확인한다.
- 검증만 자동(`--local-ai-auth auto`)에 두고 그 제공사의 API 키가 없으면, 그 제공사의 구독을 먼저 찾는다.

모델: Claude는 `opus`(`YEONE_OCR_CLAUDE_MODEL`). ChatGPT와 Gemini는 각 프로그램이 요금제에 맞춰 고르는 기본 모델을
쓰고(기록에는 `codex-default`, `gemini-cli-auto`로 남는다), 바꾸려면 `YEONE_OCR_CHATGPT_MODEL`,
`YEONE_OCR_GEMINI_MODEL`. 그 밖의 환경 변수는 각 모듈 머리글(`yeone_ocr/chatgpt_subscription.py`,
`yeone_ocr/gemini_subscription.py`, `yeone_ocr/claude_subscription.py`)에 있다.

## 이 프로그램이 하는 일과 하지 않는 일

- 하는 일: 요청마다 공식 프로그램을 한 번 실행한다. 새 빈 폴더에 그 요청의 그림·지시문·검증 스키마만 두고,
  도구·MCP 서버·확장·사용자 설정·세션 기록을 끈다(Codex: `--ephemeral --ignore-user-config --ignore-rules`,
  읽기 전용 샌드박스, 승인 없음, 웹 검색 끔 / Gemini: `--approval-mode plan -e none --allowed-mcp-server-names none`,
  `GEMINI_SYSTEM_MD`로 지시문 교체). 환경 변수의 API 키(`OPENAI_API_KEY`, `CODEX_API_KEY`, `GEMINI_API_KEY`,
  `GOOGLE_API_KEY`, `ANTHROPIC_API_KEY` 등)는 자식 프로그램에 넘기지 않는다 — 넘기면 구독 대신 API 요금이 나간다.
- 하지 않는 일: 로그인 토큰(`~/.codex/auth.json`, `~/.gemini/oauth_creds.json`, Claude 자격 증명)을 읽거나
  저장하거나 다른 곳으로 보내지 않는다. Gemini는 어느 로그인 방식을 골랐는지 보려고 `~/.gemini/settings.json`만 읽는다.
- 사용량은 각 요금제의 한도(5시간·주간·일일)에서 차감된다. 한도에 닿으면 그 실행의 검증을 멈추고 결과와 검증 대상을
  저장하며, 메시지로 알린다.
- **개인 사용 전용.** 각 회사는 공식 프로그램을 자기 계정으로 쓰는 것은 허용하지만, 제3자 프로그램이 구독 로그인을
  가져가 쓰거나 다른 사람에게 나눠 주는 것은 허용하지 않는다(특히 Google은 Gemini CLI 로그인(OAuth)을 다른
  프로그램이 직접 쓰는 것을 약관으로 금한다). 다른 사람에게 줄 설치본이나 여러 사람이 쓰는 작업에는 API 키를 쓴다.

## 다른 점

- Claude Code는 검증 스키마를 직접 받는다(`--json-schema`). Codex CLI도 받는다(`--output-schema`).
  Gemini CLI는 스키마 옵션이 없어 지시문에 스키마를 넣고, 받은 답을 JSON으로 검사한다(``` 울타리는 벗긴다).
  JSON이 아니면 그 호출은 "완료되지 않음"으로 남고 엔진 판독을 쓴다.
- 그림: Claude는 stream-json 입력에 base64로, Codex는 `-i 파일`, Gemini는 지시문의 `@image1.png` 참조로 보낸다.
- 지시문은 셋 다 표준 입력으로 넘겨 Windows 명령줄 길이 제한(cmd.exe 8191자)에 걸리지 않는다.
  npm으로 설치한 `codex.cmd`, `gemini.cmd`도 그래서 쓸 수 있다.
