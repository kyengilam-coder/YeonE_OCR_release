"""Gemini 판독기. google-genai SDK 로 이미지 + 지침을 보내 구조화 JSON 을 받는다.

환경 변수: GEMINI_API_KEY (또는 GOOGLE_API_KEY), GEMINI_MODEL (선택).
"""
from __future__ import annotations
import os, re, time, json, logging, threading
from PIL import Image
from .schema import PageReading, PageResponse, RegionReread, MultiRegionReread, GlyphChoice, MultiGlyphChoice, GlyphAsk
from .layout import to_png_bytes
from . import prompts

DEFAULT_MODEL = "gemini-2.5-pro"


class ReaderError(RuntimeError):
    """사용자에게 그대로 보여 줄 수 있는 한 줄 오류. fatal=True 면 다른 쪽도 같은 이유로 실패하므로 전체를 멈춘다(키·권한·모델·한도 0).
    fatal=False 면 이 쪽만 건너뛰고 계속한다(여러 번 시도해도 응답 형식이 맞지 않음 등)."""
    def __init__(self, msg: str, fatal: bool = True, kind: str = ""):
        super().__init__(msg); self.fatal = fatal; self.kind = kind


_CANCEL_EVENT: threading.Event | None = None


def set_cancel_event(event: threading.Event | None) -> None:
    """현재 명령의 중지 신호. 명령이 끝나면 None 으로 돌려 독립 키 확인에 영향을 주지 않는다."""
    global _CANCEL_EVENT
    _CANCEL_EVENT = event


def _check_cancelled() -> None:
    if _CANCEL_EVENT is not None and _CANCEL_EVENT.is_set():
        raise ReaderError("중지 요청으로 모델 호출을 멈췄습니다.", kind="cancelled")


def _wait_retry(seconds: float) -> None:
    event = _CANCEL_EVENT
    if event is None:
        time.sleep(seconds)
    elif event.wait(seconds):
        _check_cancelled()


def _failure_kind(e: Exception) -> str:
    """통신·서버 장애는 조각을 작게 나눠 다시 물어도 해결되지 않는다."""
    code = getattr(e, "code", None) or getattr(e, "status_code", None)
    try:
        code = int(code) if code is not None else None
    except (TypeError, ValueError):
        code = None
    msg = str(e).lower()
    if (code == 429 or (code is not None and 500 <= code < 600)
            or isinstance(e, (OSError, TimeoutError, ConnectionError))
            or any(s in msg for s in ("resource_exhausted", "timeout", "timed out"))
            or type(e).__module__.split(".")[0] in ("httpx", "httpcore", "requests", "urllib3")):
        return "infrastructure"
    return "format" if is_format_error(e) else ""


def normalize_key(key: str | None) -> str:
    """붙여 넣을 때 섞인 공백·줄바꿈·따옴표·'GEMINI_API_KEY=' 머리를 걷어 낸다."""
    k = (key or "").strip().strip('"').strip("'")
    if "=" in k and k.split("=", 1)[0].strip().upper().endswith("API_KEY"):
        k = k.split("=", 1)[1]
    return "".join(k.split())


def split_keys(raw: str | None) -> list[str]:
    """API 키 칸의 글 → 키 목록. 키를 여러 개(쉼표·줄바꿈 등으로) 넣으면 하루 한도에 걸릴 때 다음 키로 넘어간다.
    Google 키는 'AIza'(39자) 또는 'AQ.' 로 시작하므로 그 머리로 가른다(붙여 넣다 줄이 꺾인 키 하나는 그대로 하나)."""
    k = normalize_key(re.sub(r"[,;]", " ", raw or ""))
    out: list[str] = []
    for part in re.split(r"(?=AIza|AQ\.)", k):
        if part and part not in out:
            out.append(part)
    return out


def join_keys(keys: list[str]) -> str:
    return ", ".join(keys)


def key_shape_warning(key: str) -> str:
    """Google API 키는 'AIza…'(39자) 또는 새 형식 'AQ.…'(50자 안팎)이다. 그 밖의 모양이면 경고문을 돌려준다."""
    if not key:
        return "API 키가 비어 있습니다."
    if key.startswith("AIza") and len(key) == 39:
        return ""
    if key.startswith("AQ.") and 40 <= len(key) <= 80:
        return ""
    if not re.fullmatch(r"[A-Za-z0-9._\-]+", key):
        return f"키에 쓸 수 없는 글자가 섞여 있습니다 (지금 {len(key)}자). 키만 다시 붙여 넣으십시오."
    return f"키 모양이 낯섭니다 (지금 {len(key)}자, 시작 '{key[:4]}'). Google 키는 보통 AIza 39자 또는 AQ. 로 시작하는 50자 안팎입니다. [키 확인] 으로 쓸 수 있는지 확인하십시오."


def _raw(msg: str) -> str:
    m = re.search(r"'message':\s*'([^']{5,220})'", msg) or re.search(r'"message":\s*"([^"]{5,220})"', msg)
    return " (서버 메시지: " + (m.group(1) if m else re.sub(r"\s+", " ", msg)[:220]) + ")"


def is_daily_quota(msg: str) -> bool:
    """429 가 하루 한도(quotaId '...PerDay...')에 걸린 것인지. 분당 한도와 달리 기다려 다시 해도 그날은 풀리지 않는다."""
    return bool(re.search(r"PerDay|per[_ ]day", msg, re.I))


def daily_reset_hint() -> str:
    """Gemini 하루 한도는 태평양 시간 자정에 다시 찬다. 그 시각을 이 PC 의 시각으로 알려 준다."""
    try:
        from datetime import datetime, timedelta
        from zoneinfo import ZoneInfo
        pt = datetime.now(ZoneInfo("America/Los_Angeles"))
        reset = (pt + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        return reset.astimezone().strftime("%m월 %d일 %H:%M") + "(이 PC 시각)"
    except Exception:                      # tzdata 가 없는 Windows 등
        return "태평양 시간 자정(한국 시간 오후 4시, 겨울철 오후 5시)"


def friendly_error(e: Exception, model: str = "") -> tuple[str, bool]:
    """(한 줄 설명, 재시도 가치 여부)."""
    code = getattr(e, "code", None) or getattr(e, "status_code", None)
    msg = str(e)
    if "API_KEY_INVALID" in msg or "API key not valid" in msg or code == 401:
        return "API 키가 올바르지 않습니다. https://aistudio.google.com/apikey 에서 키를 만들어 통째로 다시 붙여 넣고 [키 확인] 을 누르십시오.", False
    if "PERMISSION_DENIED" in msg or code == 403:
        return "이 키로는 Gemini API 를 쓸 수 없습니다(권한 거부). 키의 제한 설정(HTTP 리퍼러 제한 등)을 풀거나 Generative Language API 가 켜진 프로젝트의 키를 쓰십시오.", False
    if "NOT_FOUND" in msg or code == 404:
        rec = recommended_model(msg)
        return (f"모델 '{model}' 을 호출할 수 없습니다(404). " + (f"서버가 권하는 모델: '{rec}'. " if rec else "퇴역했거나 이 키 종류에서는 다른 이름을 써야 합니다. ")
                + "[키 확인·모델 고르기] 로 바꾸십시오." + _raw(msg)), False
    if "RESOURCE_EXHAUSTED" in msg or code == 429:
        if re.search(r"limit:\s*0\b", msg):
            return f"이 키(무료 등급)에서는 모델 '{model}' 의 요청 한도가 0 입니다. Google AI Studio 에서 결제를 켜거나, flash 계열 모델을 고르십시오." + _raw(msg), False
        if is_daily_quota(msg):
            return (f"모델 '{model}' 의 하루 요청 한도를 다 썼습니다. 다시 시도해도 오늘은 같으므로 멈춥니다. 끝난 쪽은 기록에 남아 있으니 "
                    f"{daily_reset_hint()} 이후 [▶] 를 다시 누르면 남은 쪽부터 이어서 합니다('기록 있는 쪽도 다시 판독' 은 끄십시오). "
                    "더 빨리 하려면 Google AI Studio 에서 결제를 켜거나 flash 계열 모델을 고르십시오." + _raw(msg)), False
        return "요청 한도(쿼터)를 넘었습니다. 서버가 알려 준 시간만큼 기다렸다 다시 시도합니다. 자주 나면 쪽 범위를 줄이거나 결제를 켜십시오." + _raw(msg), True
    if code and 500 <= int(code) < 600:
        return f"Gemini 서버 오류({code}). 잠시 뒤 다시 시도하십시오.", True
    if "timeout" in msg.lower() or "timed out" in msg.lower():
        return "Gemini 응답이 제한 시간 안에 오지 않았습니다. 인터넷 연결을 확인하고 다시 시도하십시오.", True
    if "validation error" in msg.lower() or "JSON" in msg:
        return FORMAT_MSG, True
    return f"{type(e).__name__}: {msg[:300]}", True


FORMAT_MSG = "모델 응답이 정해진 형식(JSON)에 맞지 않았습니다. 같은 쪽을 다시 판독하면 대개 해결됩니다."
FORMAT_ATTEMPTS = 3        # 응답 형식 오류는 세 번까지만(한도·서버 오류의 5번과 달리 기다려도 나아지지 않는 편)


def is_format_error(e: Exception, model: str = "") -> bool:
    return friendly_error(e, model)[0] == FORMAT_MSG


def make_client(api_key: str, vertexai: bool = False, timeout_ms: int = 180_000):
    """vertexai=False: AI Studio(Gemini Developer API). True: Vertex AI 익스프레스(API 키만으로 쓰는 Vertex)."""
    from google import genai
    from google.genai import types
    logging.getLogger("google_genai").setLevel(logging.ERROR)     # AFC 권고문 같은 잡음 숨김
    return genai.Client(vertexai=vertexai, api_key=normalize_key(api_key), http_options=types.HttpOptions(timeout=timeout_ms))


# 글·그림 판독에 쓸 수 없는 모델(음성 합성, 이미지·영상 생성, 임베딩, 실시간 음성 등)은 목록에서 뺀다
NON_TEXT_HINTS = ("tts", "image", "imagen", "veo", "embedding", "embed", "audio", "live", "robotics",
                  "computer-use", "deep-research", "aqa", "gemma", "learnlm", "lyria", "music", "speech")


def is_text_model(name: str) -> bool:
    n = name.lower()
    return "gemini" in n and not any(h in n for h in NON_TEXT_HINTS)


def recommended_model(msg: str) -> str:
    """404 메시지의 'Please update your code to use models/XXX' 에서 권장 모델 이름을 꺼낸다."""
    m = re.search(r"use models/([A-Za-z0-9._\-]+)", msg)
    return m.group(1) if m else ""


def retry_delay_seconds(msg: str, default: float = 20.0) -> float:
    """429 메시지의 'retry in 23.5s' 또는 'retryDelay': '23s' 를 읽는다."""
    m = re.search(r"[Rr]etry in ([\d.]+)\s*s", msg) or re.search(r"retryDelay'?:?\s*'?(\d+)s", msg)
    try:
        return min(120.0, max(3.0, float(m.group(1)))) if m else default
    except Exception:
        return default


def model_sort_key(name: str):
    """최신 버전·pro 계열이 앞에 오게. 'gemini-3.1-pro-preview' → (-3.1, 0, 1, name)."""
    m = re.search(r"gemini-(\d+(?:\.\d+)?)", name)
    ver = float(m.group(1)) if m else -1.0
    fam = 0 if "pro" in name else (1 if "flash" in name and "lite" not in name else (2 if "flash" in name else 3))
    preview = 1 if ("preview" in name or "exp" in name) else 0
    return (-ver, fam, preview, name)


def pick_second_model(models: list[str], primary: str) -> str:
    """2차 판독용 모델 자동 선택: 1차와 같은 세대의 flash(lite 제외, 정식판 우선). 없으면 더 새 세대 flash, 그것도 없으면 1차 모델."""
    m = re.search(r"gemini-(\d+(?:\.\d+)?)", primary or "")
    ver = float(m.group(1)) if m else None
    cands = [x for x in models if is_text_model(x) and "flash" in x and "lite" not in x]
    if ver is not None:
        same = [x for x in cands if f"gemini-{m.group(1)}-" in x]
        if same:
            return sorted(same, key=lambda x: (("preview" in x or "exp" in x), x))[0]
        newer = []
        for x in cands:
            mm = re.search(r"gemini-(\d+(?:\.\d+)?)", x)
            if mm and float(mm.group(1)) >= ver:
                newer.append(x)
        if newer:
            return sorted(newer, key=model_sort_key)[0]
    return primary


def list_models(api_key: str, vertexai: bool = False) -> list[str]:
    """generateContent 를 지원하는 모델 이름 목록(models/ 머리 제거), 최신 순. 키가 틀리면 예외."""
    client = make_client(api_key, vertexai, timeout_ms=30_000)
    names = []
    for m in client.models.list():
        acts = getattr(m, "supported_actions", None)
        if acts and "generateContent" not in acts:
            continue
        n = (getattr(m, "name", "") or "")
        n = n.split("/")[-1] if "/" in n else n          # models/… 또는 publishers/google/models/…
        if n and is_text_model(n):
            names.append(n)
    return sorted(set(names), key=model_sort_key)


def probe_model(api_key: str, model: str, vertexai: bool = False, *, count_call: bool = False) -> tuple[bool, str]:
    """모델을 짧게 호출한다. 명령 중 자동 진단(count_call)은 그 명령의 호출 상한·중지 신호를 따른다."""
    from google.genai import types
    try:
        client = make_client(api_key, vertexai, timeout_ms=40_000)
        if count_call:
            _check_cancelled()
            CALL_CAP.take()
        client.models.generate_content(model=model, contents="1", config=types.GenerateContentConfig(
            max_output_tokens=5, temperature=0.0, automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True)))
        return True, "호출 확인됨"
    except ReaderError:
        raise
    except Exception as e:
        return False, friendly_error(e, model)[0]


def check_key(api_key: str, model: str = "", probe: bool = True, max_probe: int = 8, *, count_calls: bool = False) -> dict:
    """키가 쓸 수 있는지, 어느 접속 경로(AI Studio / Vertex 익스프레스)에서 쓰이는지, 지정한 모델이 있는지 확인한다."""
    if count_calls:
        _check_cancelled()
    ks = split_keys(api_key)
    k = ks[0] if ks else ""                  # 키를 여러 개 넣었으면 첫 키(나머지는 창 프로그램이 따로 확인)
    out = {"ok": False, "key_len": len(k), "shape_warning": key_shape_warning(k), "models": [], "model_ok": None,
           "message": "", "api": "", "endpoints": {}, "probe": {}, "working_model": ""}
    if not k:
        out["message"] = "API 키가 비어 있습니다."; return out
    tries = [("studio", False), ("vertex", True)] if k.startswith("AQ.") else [("studio", False)]
    for name, vx in tries:
        try:
            out["endpoints"][name] = {"ok": True, "models": list_models(k, vx)}
        except Exception as e:
            out["endpoints"][name] = {"ok": False, "error": friendly_error(e, model)[0]}
    good = [n for n, r in out["endpoints"].items() if r["ok"]]
    if not good:
        out["message"] = out["endpoints"]["studio"].get("error", "확인 실패"); return out
    out["ok"] = True; out["api"] = good[0]
    seen = set(); models = []
    for n in good:
        for m in out["endpoints"][n]["models"]:
            if m not in seen: seen.add(m); models.append(m)
    out["models"] = sorted(models, key=model_sort_key)
    if model:
        out["model_ok"] = model in models
    where = " · ".join(("AI Studio" if n == "studio" else "Vertex 익스프레스") + f" {len(out['endpoints'][n]['models'])}개" for n in good)
    msg = f"키 확인됨 ({where})."
    vx = (out["api"] == "vertex")
    if probe:
        # 지정한 모델부터 실제로 불러 보고, 안 되면 최신 순으로 되는 모델을 찾는다
        order = ([model] if model else []) + [m for m in out["models"] if m != model]
        tried = 0
        while order and tried < max_probe:
            m = order.pop(0); tried += 1
            ok, pm = probe_model(k, m, vx, count_call=True) if count_calls else probe_model(k, m, vx)
            out["probe"][m] = pm
            if ok:
                out["working_model"] = m; break
            rec = recommended_model(pm)
            if rec and rec not in out["probe"] and rec not in order:
                order.insert(0, rec)                     # 서버가 권한 모델을 바로 다음에 시험
                if rec not in out["models"]: out["models"].insert(0, rec)
        if model:
            if out["probe"].get(model) == "호출 확인됨":
                msg += f" '{model}' 호출 확인됨."
            else:
                msg += f" '{model}' 은 {'목록에 있지만 ' if out['model_ok'] else '목록에 없고 '}호출이 거부됨 — {out['probe'].get(model, '')}"
        if out["working_model"] and out["working_model"] != model:
            msg += f" → 호출 확인된 모델: '{out['working_model']}'. 이 모델로 바꾸십시오."
        elif not out["working_model"]:
            msg += f" 위 {min(max_probe, len(order))}개 모델 모두 호출 거부 — 키의 API 제한이나 결제 상태를 확인하십시오."
    else:
        msg += "" if not model else (f" '{model}' 목록에 있음." if out["model_ok"] else f" 그러나 '{model}' 은 목록에 없음.")
    out["message"] = msg
    return out


def cap_message(limit: int) -> str:
    return (f"모델 호출 상한 {limit}회에 닿아 멈춤 — 끝난 쪽은 기록에 남음. [세부 설정] 의 호출 상한을 늘리거나 0(상한 없음)으로 바꾸십시오"
            "(명령줄 --max-calls). 다시 실행하면 남은 쪽부터 이어서 합니다.")


class CallCap:
    """한 명령(cli.main 한 번) 동안 이 프로세스의 모든 판독기가 함께 세는 모델 호출 상한(비용 안전장치). 0 = 상한 없음.
    성공·실패를 가리지 않고 호출 시도마다(재시도 포함) 하나씩 센다. 창 프로그램은 한 프로세스에서 명령을 여럿 돌리므로 명령마다 reset 한다."""
    def __init__(self):
        self.limit = 0; self.used = 0; self._lock = threading.Lock()

    def reset(self, limit: int = 0) -> None:
        with self._lock:
            self.limit = max(0, int(limit or 0)); self.used = 0

    def take(self) -> None:
        """호출 하나를 센다. 상한에 닿았으면 부르지 않고 멈춘다(fatal — 다른 쪽도 같은 이유로 못 부른다)."""
        with self._lock:
            if self.limit and self.used >= self.limit:
                raise ReaderError(cap_message(self.limit), fatal=True)
            self.used += 1


CALL_CAP = CallCap()


def set_call_cap(limit: int = 0) -> None:
    """상한을 정하고 센 수를 0 으로(cli.main 이 명령마다 부른다)."""
    CALL_CAP.reset(limit)


THINKING_LEVELS = ("low", "medium", "high")
THINKING_BUDGET_25 = {"low": 512, "medium": 4096, "high": 24576}     # 2.5 이하는 수준 대신 예산. high = 2.5 Flash·Flash-Lite 의 최대
THINKING_HIGH_25_PRO = 32768                                          # 2.5 Pro 의 최대(범위 128~32768)


def thinking_kw(model: str, level: str | None) -> dict:
    """생각 수준 → ThinkingConfig 인자. 어느 계열에서나 low ≤ medium ≤ high(높을수록 더 생각) — 어느 수준도 '모델 기본'(설정 없음)으로 두지 않는다.
    - Gemini 3 이상: thinking_level LOW · MEDIUM · HIGH(모델이 받지 않으면 _call 이 모델 기본 = 동적 생각으로 계속).
    - 그 밖(2.5 등): thinking_budget low 512 · medium 4096 · high 24576(Flash·Flash-Lite 최대), 2.5 Pro 의 high 는 32768(최대).
      high 를 모델 기본으로 두면 생각 끔이 기본인 2.5 Flash-Lite 에서 high 가 low 보다 덜 생각한다(측정에서 high 를 재면 생각 없음을 잰다).
    수준이 없거나 모르는 값(auto·full)이면 {} = 모델 기본."""
    lv = (level or "").lower()
    if lv not in THINKING_LEVELS:
        return {}
    if re.search(r"gemini-[3-9]", model or ""):
        return {"thinking_level": lv.upper()}
    return {"thinking_budget": THINKING_HIGH_25_PRO if lv == "high" and re.search(r"gemini-2\.5-pro", model or "") else THINKING_BUDGET_25[lv]}


_CACHE_REGISTRY: dict[tuple, str] = {}        # (모델, 접속 경로, 지침 해시) → 캐시 이름. 한 실행의 1차·2차 판독기가 같은 캐시를 쓴다
CACHE_MIN_CHARS = 6000                         # 이보다 짧은 시스템 지시(3차 판별용)는 캐시 최소 토큰(모델별 1~4천)에 못 미쳐 캐시를 만들지 않는다
_CACHE_LOCK = threading.Lock()


class GeminiReader:
    name = "gemini"

    def __init__(self, model: str | None = None, api_key: str | None = None, thinking_budget: int | None = None, retries: int = 3, api: str = "auto",
                 use_cache: bool = True, thinking: str | None = None, media_resolution: str | None = None, system: str | None = None):
        from google.genai import types     # 지연 임포트: mock 만 쓸 때는 SDK 없이도 돌아가게
        keys = split_keys(api_key or os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"))
        if not keys:
            raise ReaderError("GEMINI_API_KEY 가 없습니다. 창 프로그램의 'Gemini API 키' 칸에 넣거나 (PowerShell) $env:GEMINI_API_KEY='...' 로 설정하십시오.")
        for i, k in enumerate(keys, 1):
            warn = key_shape_warning(k)
            if warn:
                print(f"주의: {'키 ' + str(i) + ': ' if len(keys) > 1 else ''}{warn}", flush=True)
        key = keys[0]
        self.types = types
        self.keys = keys                                 # 하루 한도에 걸리면 다음 키로(한도는 프로젝트마다 따로다)
        self.key_idx = 0
        self.key = key
        self.api = api                                   # auto | studio | vertex
        self.vertexai = (api == "vertex")
        self._switched = False
        self.client = make_client(key, self.vertexai)    # 응답 제한 시간 180초. 없으면 네트워크 문제 때 영영 기다릴 수 있다
        self.model = model or os.environ.get("GEMINI_MODEL") or DEFAULT_MODEL
        self.thinking_budget = thinking_budget
        # None=모델 기본, low·medium·high=생각 수준(2차 전사는 low, 1차는 --page-thinking). 거부되면 자동 해제
        self.thinking = (thinking or "").lower() if (thinking or "").lower() in THINKING_LEVELS else None
        # 이미지 토큰 예산. low/medium/high 는 호출 설정으로, ultra_high(Gemini 3: 2,240 토큰)는 이미지 조각(Part)마다 붙인다(SDK 전역 열거형에 없음).
        # 모델이 거부하면 ultra_high → high → 기본값 순으로 자동으로 내린다.
        self.media_resolution = (media_resolution or "").lower() if (media_resolution or "").lower() in ("low", "medium", "high", "ultra_high") else None
        self.n_429 = 0                    # 한도 초과 횟수(동시 쪽 수 조절에 씀)
        self.retries = retries
        self._system = system or prompts.system_prompt()      # 3차 판별기는 자형 전용 지시(prompts.glyph_system_prompt)를 받는다
        self.usage = {"calls": 0, "prompt_tokens": 0, "output_tokens": 0, "thinking_tokens": 0, "cached_tokens": 0}
        self._lock = threading.Lock()
        self._tls = threading.local()
        self.cache_name: str | None = None
        self.cache_note = ""
        if use_cache and len(self._system) >= CACHE_MIN_CHARS:
            self._make_cache()
        elif use_cache:
            self.cache_note = "지시가 짧아 캐시 안 씀"        # 캐시 최소 토큰에 못 미친다(만들기 호출이 매번 거절되기만 한다)

    def _cache_key(self) -> tuple:
        import hashlib
        return (self.model, self.vertexai, hashlib.sha1(getattr(self, "key", "").encode("utf-8")).hexdigest()[:12], hashlib.sha1(self._system.encode("utf-8")).hexdigest())

    def _make_cache(self, stale: str | None = None) -> None:
        """지침 전문을 컨텍스트 캐시에 올린다. 호출마다 지침을 다시 보내지 않아 빠르고 싸다. 모델이 지원하지 않으면 그냥 넘어간다.
        같은 모델·지침으로 이미 만든 캐시가 이 프로세스에 있으면 그것을 쓴다(한 실행에서 1차·2차 판독기가 각각 만들지 않게).
        stale 을 주면 그 이름은 만료된 것이라 보고 새로 만든다."""
        t = self.types
        key = self._cache_key()
        with _CACHE_LOCK:
            have = _CACHE_REGISTRY.get(key)
            if have and have != stale:
                self.cache_name = have; self.cache_note = f"지침 캐시 사용({have}, 공유)"; return
            try:
                c = self.client.caches.create(model=self.model, config=t.CreateCachedContentConfig(
                    system_instruction=self._system, ttl="7200s", display_name="yeone-ocr-guide"))
                self.cache_name = getattr(c, "name", None)
                self.cache_note = f"지침 캐시 사용({self.cache_name})" if self.cache_name else ""
                if self.cache_name:
                    _CACHE_REGISTRY[key] = self.cache_name
            except Exception as e:
                self.cache_name = None
                self.cache_note = f"지침 캐시 미사용({friendly_error(e, self.model)[0][:80]})"
                _CACHE_REGISTRY.pop(key, None)

    def _switch_route(self, used_vx: bool, used_key: int) -> bool:
        """이 호출이 쓴 접속 경로(AI Studio ↔ Vertex 익스프레스)가 403·404 로 거절됐다. 경로를 한 번만 바꾼다(잠금 안에서 — 두 스레드가 함께 거절을 보고
        경로를 두 번 뒤집거나, 늦은 스레드가 바뀐 줄 모르고 멈추던 경쟁). 다른 스레드가 이미 바꿨거나 키가 바뀌었으면 True(새 경로로 다시),
        바꾼 경로에서 또 거절됐으면 False."""
        with self._lock:
            if self.key_idx != used_key or self.vertexai != used_vx:
                return True
            if self._switched:
                return False
            self._switched = True; self.vertexai = not used_vx
            self.client = make_client(self.key, self.vertexai)
        print(f"주의: {'Vertex 익스프레스' if self.vertexai else 'AI Studio'} 접속 경로로 바꿔 다시 시도합니다.", flush=True)
        return True

    def _rotate_key(self, used_idx: int) -> bool:
        """used_idx 키가 하루 한도에 걸렸다. 다음 키로 바꾸면 True(다른 스레드가 이미 바꿨어도 True), 남은 키가 없으면 False."""
        with self._lock:
            if self.key_idx != used_idx:
                return True
            if used_idx + 1 >= len(self.keys):
                return False
            self.key_idx = used_idx + 1
            self.key = self.keys[self.key_idx]
            self.vertexai = (self.api == "vertex"); self._switched = False
            self.client = make_client(self.key, self.vertexai)
            had_cache = self.cache_name is not None
            self.cache_name = None                       # 캐시는 키의 프로젝트에 속한다 — 새 키로 다시 만든다
            print(f"주의: 키 {used_idx + 1}/{len(self.keys)} 이(가) 모델 '{self.model}' 의 하루 한도에 걸려 키 {self.key_idx + 1}(…{self.key[-4:]})로 바꿔 계속합니다.", flush=True)
        if had_cache:
            self._make_cache()
        return True

    def _config(self, schema):
        t = self.types
        kw = dict(response_mime_type="application/json", response_schema=schema, temperature=0.0,
                  automatic_function_calling=t.AutomaticFunctionCallingConfig(disable=True))
        if self.cache_name:
            kw["cached_content"] = self.cache_name          # 캐시를 쓰면 system_instruction 을 따로 보내지 않는다
        else:
            kw["system_instruction"] = self._system
        if self.media_resolution and self.media_resolution != "ultra_high":
            try:
                kw["media_resolution"] = getattr(t.MediaResolution, f"MEDIA_RESOLUTION_{self.media_resolution.upper()}")
            except AttributeError:                       # 옛 SDK — 설정 없이 간다
                self.media_resolution = None
        if self.thinking_budget is not None:
            kw["thinking_config"] = t.ThinkingConfig(thinking_budget=self.thinking_budget)
        elif self.thinking and (tk := thinking_kw(self.model, self.thinking)):
            kw["thinking_config"] = t.ThinkingConfig(**tk)         # 3.x 는 thinking_level, 2.5 는 thinking_budget
        return t.GenerateContentConfig(**kw)

    def settings_note(self) -> str:
        """생각·해상도 설정 한 줄(측정 실행의 진행 기록이 스스로 설명되게 머리줄에 적는다)."""
        if self.thinking_budget is not None:
            th = f"생각 예산 {self.thinking_budget}"
        elif self.thinking:
            tk = thinking_kw(self.model, self.thinking)
            th = f"생각 {self.thinking}" + (f"(예산 {tk['thinking_budget']})" if "thinking_budget" in tk else ("" if tk else "(모델 기본)"))
        else:
            th = "생각 모델 기본"
        mr = self.media_resolution
        return f"{th} · 해상도 " + ((mr + ("(조각별)" if mr == "ultra_high" else "")) if mr else "기본")

    def image_part(self, img: Image.Image):
        """이미지 조각(Part). ultra_high 면 조각마다 해상도 수준을 붙인다(조각별 media_resolution 은 Gemini 3 계열만 받는다).
        3 계열이 아니거나 옛 SDK 면 high(전역 설정)로 내려 쓴다 — 서버가 조각 설정을 무시해 기본 해상도로 떨어지는 일을 막는다."""
        t = self.types
        p = t.Part.from_bytes(data=to_png_bytes(img), mime_type="image/png")
        if self.media_resolution == "ultra_high":
            if not re.search(r"gemini-[3-9]", self.model):
                self._step_down("high", f"주의: {self.model} 은 조각별 최고 해상도(ultra_high)를 받지 않는 세대라 high 로 계속합니다.")
                return p
            try:
                p.media_resolution = t.PartMediaResolution(level=t.PartMediaResolutionLevel.MEDIA_RESOLUTION_ULTRA_HIGH)
            except Exception:                            # 옛 SDK — 조각별 설정이 없으면 high 로
                self._step_down("high", "주의: 설치된 SDK 가 조각별 해상도를 지원하지 않아 high 로 계속합니다.")
        return p

    def _step_down(self, level: str | None, note: str) -> None:
        """해상도 설정을 내린다(여러 스레드가 같은 거부를 봐도 한 번만 알린다)."""
        with self._lock:
            if self.media_resolution == level or (level == "high" and self.media_resolution != "ultra_high"):
                return
            self.media_resolution = level
        print(note, flush=True)

    @staticmethod
    def _has_part_resolution(contents: list) -> bool:
        return any(getattr(p, "media_resolution", None) is not None for p in (contents or []))

    @staticmethod
    def _strip_part_resolution(contents: list) -> None:
        for p in contents:
            if getattr(p, "media_resolution", None) is not None:
                try:
                    p.media_resolution = None
                except Exception:
                    pass

    def _call(self, img: Image.Image, prompt: str, schema, extra_parts: list | None = None):
        """이미지 한 장(또는 extra_parts 로 여러 장)과 지시문을 보내 schema 대로 받는다."""
        _check_cancelled()
        t = self.types
        contents = extra_parts if extra_parts is not None else [self.image_part(img), prompt]
        last_msg = ""; last_kind = ""; tried_msgs: list[str] = []; fmt_fails = 0
        attempts = max(self.retries, 5)
        for i in range(attempts):
            _check_cancelled()
            used_cache = self.cache_name                                  # 이 호출이 쓰는 캐시(실패하면 이 이름이 만료된 것)
            with self._lock:                                              # 이 호출이 쓰는 키(하루 한도면 다음 키로)·접속 경로·클라이언트 — 한꺼번에(다른 스레드가 바꾸는 중일 수 있다)
                used_key, used_vx, client = self.key_idx, self.vertexai, self.client
            try:
                config = self._config(schema)                             # 재시도 try 밖에서: 설정을 못 만드는 것은 응답 오류가 아니다(옛 SDK 가 thinking_level 을 모름 등)
            except Exception as e:
                raise ReaderError(f"호출 설정을 만들지 못했습니다 — 설치된 google-genai SDK 가 이 설정(생각 수준·해상도 등)을 모르는 듯합니다: {type(e).__name__}: {str(e)[:160]} "
                                  "→ pip install -U google-genai 로 SDK 를 올리거나 [세부 설정] 의 생각 줄이기·조각 해상도를 끄십시오(1차 생각 수준·쪽 해상도는 auto 로).", fatal=True) from e
            _check_cancelled()
            CALL_CAP.take()                                               # 호출 상한(--max-calls) — 재시도 try 밖에서: 닿으면 부르지 않고 바로 멈춘다
            try:
                resp = client.models.generate_content(model=self.model, contents=contents, config=config)
                um = getattr(resp, "usage_metadata", None)
                if um is not None and not getattr(self, "_usage_fields_logged", False):
                    # 생각 토큰이 0 으로만 기록되던 문제를 가리기 위해 SDK 가 주는 사용량 필드를 한 번 그대로 적어 둔다
                    self._usage_fields_logged = True
                    try:
                        raw = {k: v for k, v in (um.model_dump() if hasattr(um, "model_dump") else vars(um)).items() if v not in (None, 0, [], {})}
                        print(f"  사용량 필드({self.model}): {raw}", flush=True)
                    except Exception:
                        pass
                delta = {"calls": 1,
                         "prompt_tokens": (getattr(um, "prompt_token_count", 0) or 0) if um else 0,
                         "output_tokens": (getattr(um, "candidates_token_count", 0) or 0) if um else 0,
                         "thinking_tokens": (getattr(um, "thoughts_token_count", 0) or 0) if um else 0,      # 출력 요금으로 과금
                         "cached_tokens": (getattr(um, "cached_content_token_count", 0) or 0) if um else 0}
                with self._lock:
                    for k, v in delta.items():
                        self.usage[k] += v
                page = getattr(self._tls, "page", None)
                if page is not None:
                    for k, v in delta.items():
                        page[k] = page.get(k, 0) + v
                text = resp.text or ""
                return schema.model_validate_json(text)
            except Exception as e:
                _check_cancelled()
                last_msg, retry = friendly_error(e, self.model)
                last_kind = _failure_kind(e)
                code = getattr(e, "code", None) or getattr(e, "status_code", None)
                if code == 429 or "RESOURCE_EXHAUSTED" in str(e):
                    if is_daily_quota(str(e)):
                        if self._rotate_key(used_key):
                            continue                         # 다음 키로 같은 호출을 다시(속도 조절 대상 아님)
                        if len(self.keys) > 1:
                            last_msg = f"넣은 키 {len(self.keys)}개가 모두 하루 한도에 걸렸습니다. " + last_msg
                    with self._lock:
                        self.n_429 += 1
                if (self.thinking or self.thinking_budget is not None) and code == 400 and "think" in str(e).lower():
                    print(f"주의: {self.model} 이 생각 설정을 받지 않아 모델 기본값으로 계속합니다.", flush=True)
                    self.thinking = None; self.thinking_budget = None
                    continue
                if used_cache and ("cachedContent" in str(e) or "cached content" in str(e).lower() or (code == 403 and "cache" in str(e).lower())):
                    if self.cache_name == used_cache:        # 다른 스레드가 먼저 새로 만들었으면 그 이름으로 다시 시도. 만드는 일은 _make_cache 의 등록부 잠금이 하나로 묶는다
                        self._make_cache(stale=used_cache)   # 캐시가 만료·소실되면 다시 만들고 재시도
                    if not self.cache_name:
                        print("주의: 지침 캐시를 다시 만들지 못해 일반 호출로 계속합니다.", flush=True)
                    continue
                if code == 400 and self._has_part_resolution(contents) and not re.search(r"payload|size|too large|cache|token", str(e).lower()):
                    # 이 호출의 조각에 ultra_high 가 붙어 있었다(다른 스레드가 이미 내렸어도). 다른 원인이 분명한 400(크기·캐시·토큰)이 아니면 조각 설정을 떼고 high 로
                    self._strip_part_resolution(contents)
                    self._step_down("high", f"주의: {self.model} 이 조각별 최고 해상도(ultra_high)를 받지 않아 high 로 계속합니다.")
                    continue
                if code == 400 and "resolution" in str(e).lower() and self.media_resolution:
                    self._step_down(None, f"주의: {self.model} 이 이미지 해상도 설정(media_resolution)을 받지 않아 기본값으로 계속합니다.")
                    continue
                if code in (403, 404) and self.api == "auto" and self.key.startswith("AQ.") and self._switch_route(used_vx, used_key):
                    tried_msgs.append(("Vertex 경로: " if used_vx else "AI Studio 경로: ") + last_msg)
                    continue
                if not retry:                      # 키·권한·모델 이름 오류는 다시 해도 같다
                    tried_msgs.append(("Vertex 경로: " if used_vx else "AI Studio 경로: ") + last_msg) if tried_msgs else None
                    final = " / ".join(tried_msgs) if tried_msgs else last_msg
                    if code in (403, 404):
                        try:                       # 되는 모델을 찾아 바로 제안한다
                            res = check_key(self.key, self.model, probe=True, max_probe=6, count_calls=True)
                            if res.get("working_model"):
                                final += f" → 호출 확인된 모델: '{res['working_model']}'. [키 확인·모델 고르기] 에서 이 모델로 바꾸십시오."
                        except ReaderError:
                            raise
                        except Exception:
                            pass
                    raise ReaderError(final) from e
                if last_msg == FORMAT_MSG:
                    fmt_fails += 1
                    if fmt_fails >= FORMAT_ATTEMPTS:   # 응답 형식 오류는 세 번까지만 — 기다려도 나아지지 않는 편이라 쪽마다 30초를 버리지 않게
                        raise ReaderError(f"{fmt_fails}회 시도 뒤에도 실패: {last_msg}", fatal=False, kind="format") from e
                if i + 1 >= attempts:
                    break                              # 마지막 실패 뒤에는 더 시도하지 않으므로 기다리지 않는다
                if code == 429 or "RESOURCE_EXHAUSTED" in str(e):
                    d = retry_delay_seconds(str(e))
                    print(f"주의: 요청 한도 초과 — {d:.0f}초 뒤 다시 시도 ({i + 1}/{attempts})", flush=True)
                    _wait_retry(d)
                else:
                    _wait_retry(2 ** i)             # 서버·형식 오류는 잠시 뒤 다시
        raise ReaderError(f"{attempts}회 시도 뒤에도 실패: {last_msg}", fatal=False, kind=last_kind)

    def usage_begin(self) -> None:
        """이 스레드에서 이어지는 호출들의 토큰을 쪽 단위로 따로 센다(여러 쪽을 동시에 읽어도 섞이지 않게)."""
        self._tls.page = {}

    def resolve_text_choices(self, items: list[dict]) -> dict[str, str]:
        """간체→번체·MSR 교정의 문맥 후보만 텍스트로 판별한다(이미지 호출 없음)."""
        from .readers import resolve_text_choices
        return resolve_text_choices(self, items)

    def usage_end(self) -> dict:
        d = getattr(self._tls, "page", None) or {}
        self._tls.page = None
        return d

    def read_page(self, img: Image.Image, file_name: str, pdf_page: int, total: int, extra: str = "") -> PageReading:
        r = self._call(img, prompts.page_prompt(file_name, pdf_page, total, extra), PageResponse)
        r.pdf_page = pdf_page
        return r

    def reread_region(self, crop: Image.Image, kind: str, region_id: str, pdf_page: int) -> RegionReread:
        return self._call(crop, prompts.region_prompt(kind, region_id, pdf_page), RegionReread)

    def adjudicate(self, crop: Image.Image, ask: GlyphAsk, horizontal: bool, pdf_page: int) -> GlyphChoice:
        """3차 글자 판별: 글자 몇 개를 크게 잘라 보내, 조각 안 순번으로 가리킨 글자가 후보 가운데 어느 것인지 자형만으로 묻는다.
        앞뒤 글자는 받지 않는다(눈가림 — GlyphAsk 참고)."""
        return self._call(crop, prompts.adjudicate_prompt(ask, horizontal, pdf_page), GlyphChoice)

    def identify(self, crop: Image.Image, ask: GlyphAsk, horizontal: bool, pdf_page: int) -> GlyphChoice:
        """눈가림 확인: 후보 없이 가리킨 글자를 읽게 한다(객관식 답과 맞춰 본다)."""
        return self._call(crop, prompts.identify_prompt(ask, horizontal, pdf_page), GlyphChoice)

    def reread_regions(self, crops: list[tuple[str, str, Image.Image]], pdf_page: int) -> dict[str, RegionReread]:
        """여러 문자 영역 조각을 한 호출로 다시 판독한다. 돌려주는 사전에 빠진 영역은 호출자가 낱개로 다시 묻는다."""
        t = self.types
        parts = [prompts.regions_prompt([(rid, kind) for rid, kind, _ in crops], pdf_page)]
        for rid, kind, im in crops:
            parts.append(f"【{rid}】"); parts.append(self.image_part(im))
        res = self._call(None, "", MultiRegionReread, extra_parts=parts)
        return {it.region_id: RegionReread(columns=it.columns, uncertain=it.uncertain) for it in res.regions if it.columns}

    def adjudicate_many(self, crops: list[tuple[str, Image.Image, GlyphAsk]], horizontal: bool, pdf_page: int) -> dict[str, GlyphChoice]:
        """3차 글자 판별을 조각 여러 장(자리 번호, 조각, 질문)으로 한 호출에 묻는다. 빠진 자리는 호출자가 낱개로 다시 묻는다."""
        return self._glyph_many(prompts.adjudicate_many_prompt([(sid, ask) for sid, _, ask in crops], horizontal, pdf_page), crops)

    def identify_many(self, crops: list[tuple[str, Image.Image, GlyphAsk]], horizontal: bool, pdf_page: int) -> dict[str, GlyphChoice]:
        """눈가림 확인을 조각 여러 장으로 한 호출에 묻는다."""
        return self._glyph_many(prompts.identify_many_prompt([(sid, ask) for sid, _, ask in crops], horizontal, pdf_page), crops)

    def _glyph_many(self, prompt: str, crops) -> dict[str, GlyphChoice]:
        parts = [prompt]
        for sid, im, _ in crops:
            parts.append(f"【{sid}】"); parts.append(self.image_part(im))
        res = self._call(None, "", MultiGlyphChoice, extra_parts=parts)
        out: dict[str, GlyphChoice] = {}
        for it in res.items:
            sid = (it.site_id or "").strip().strip("【】")
            if sid and sid not in out:
                out[sid] = GlyphChoice(window=it.window, evidence=it.evidence, choice=it.choice)
        return out
