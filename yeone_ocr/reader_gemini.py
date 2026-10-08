"""Gemini 판독기. google-genai SDK 로 이미지 + 지침을 보내 구조화 JSON 을 받는다.

환경 변수: GEMINI_API_KEY (또는 GOOGLE_API_KEY), GEMINI_MODEL (선택).
"""
from __future__ import annotations
import os, re, time, json, logging, threading
from PIL import Image
from .schema import PageReading, RegionReread, MultiRegionReread, GlyphChoice
from .layout import to_png_bytes
from . import prompts

DEFAULT_MODEL = "gemini-2.5-pro"


class ReaderError(RuntimeError):
    """사용자에게 그대로 보여 줄 수 있는 한 줄 오류. fatal=True 면 다른 쪽도 같은 이유로 실패하므로 전체를 멈춘다(키·권한·모델·한도 0).
    fatal=False 면 이 쪽만 건너뛰고 계속한다(여러 번 시도해도 응답 형식이 맞지 않음 등)."""
    def __init__(self, msg: str, fatal: bool = True):
        super().__init__(msg); self.fatal = fatal


def normalize_key(key: str | None) -> str:
    """붙여 넣을 때 섞인 공백·줄바꿈·따옴표·'GEMINI_API_KEY=' 머리를 걷어 낸다."""
    k = (key or "").strip().strip('"').strip("'")
    if "=" in k and k.split("=", 1)[0].strip().upper().endswith("API_KEY"):
        k = k.split("=", 1)[1]
    return "".join(k.split())


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
        return "요청 한도(쿼터)를 넘었습니다. 서버가 알려 준 시간만큼 기다렸다 다시 시도합니다. 자주 나면 쪽 범위를 줄이거나 결제를 켜십시오." + _raw(msg), True
    if code and 500 <= int(code) < 600:
        return f"Gemini 서버 오류({code}). 잠시 뒤 다시 시도하십시오.", True
    if "timeout" in msg.lower() or "timed out" in msg.lower():
        return "Gemini 응답이 제한 시간 안에 오지 않았습니다. 인터넷 연결을 확인하고 다시 시도하십시오.", True
    if "validation error" in msg.lower() or "JSON" in msg:
        return "모델 응답이 정해진 형식(JSON)에 맞지 않았습니다. 같은 쪽을 다시 판독하면 대개 해결됩니다.", True
    return f"{type(e).__name__}: {msg[:300]}", True


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


def probe_model(api_key: str, model: str, vertexai: bool = False) -> tuple[bool, str]:
    """모델을 아주 짧게 실제로 호출해 본다(토큰 몇 개). 목록에 있어도 호출이 거부되는 퇴역 모델을 가려낸다."""
    from google.genai import types
    try:
        client = make_client(api_key, vertexai, timeout_ms=40_000)
        client.models.generate_content(model=model, contents="1", config=types.GenerateContentConfig(
            max_output_tokens=5, temperature=0.0, automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True)))
        return True, "호출 확인됨"
    except Exception as e:
        return False, friendly_error(e, model)[0]


def check_key(api_key: str, model: str = "", probe: bool = True, max_probe: int = 8) -> dict:
    """키가 쓸 수 있는지, 어느 접속 경로(AI Studio / Vertex 익스프레스)에서 쓰이는지, 지정한 모델이 있는지 확인한다."""
    k = normalize_key(api_key)
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
            ok, pm = probe_model(k, m, vx)
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


class GeminiReader:
    name = "gemini"

    def __init__(self, model: str | None = None, api_key: str | None = None, thinking_budget: int | None = None, retries: int = 3, api: str = "auto",
                 use_cache: bool = True, thinking: str | None = None, media_resolution: str | None = None):
        from google.genai import types     # 지연 임포트: mock 만 쓸 때는 SDK 없이도 돌아가게
        key = normalize_key(api_key or os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"))
        if not key:
            raise ReaderError("GEMINI_API_KEY 가 없습니다. 창 프로그램의 'Gemini API 키' 칸에 넣거나 (PowerShell) $env:GEMINI_API_KEY='...' 로 설정하십시오.")
        warn = key_shape_warning(key)
        if warn:
            print("주의: " + warn, flush=True)
        self.types = types
        self.key = key
        self.api = api                                   # auto | studio | vertex
        self.vertexai = (api == "vertex")
        self._switched = False
        self.client = make_client(key, self.vertexai)    # 응답 제한 시간 180초. 없으면 네트워크 문제 때 영영 기다릴 수 있다
        self.model = model or os.environ.get("GEMINI_MODEL") or DEFAULT_MODEL
        self.thinking_budget = thinking_budget
        self.thinking = thinking          # None=모델 기본, "low"=생각 줄임(2차 전사용). 거부되면 자동 해제
        self.media_resolution = (media_resolution or "").lower() if media_resolution in ("low", "medium", "high") else None   # 이미지 토큰 예산(2차 조각용 high). 거부되면 자동 해제
        self.n_429 = 0                    # 한도 초과 횟수(동시 쪽 수 조절에 씀)
        self.retries = retries
        self._system = prompts.system_prompt()
        self.usage = {"calls": 0, "prompt_tokens": 0, "output_tokens": 0, "thinking_tokens": 0, "cached_tokens": 0}
        self._lock = threading.Lock()
        self._tls = threading.local()
        self.cache_name: str | None = None
        self.cache_note = ""
        if use_cache:
            self._make_cache()

    def _make_cache(self) -> None:
        """지침 전문을 컨텍스트 캐시에 올린다. 호출마다 지침을 다시 보내지 않아 빠르고 싸다. 모델이 지원하지 않으면 그냥 넘어간다."""
        t = self.types
        try:
            c = self.client.caches.create(model=self.model, config=t.CreateCachedContentConfig(
                system_instruction=self._system, ttl="7200s", display_name="yeone-ocr-guide"))
            self.cache_name = getattr(c, "name", None)
            self.cache_note = f"지침 캐시 사용({self.cache_name})" if self.cache_name else ""
        except Exception as e:
            self.cache_name = None
            self.cache_note = f"지침 캐시 미사용({friendly_error(e, self.model)[0][:80]})"

    def _config(self, schema):
        t = self.types
        kw = dict(response_mime_type="application/json", response_schema=schema, temperature=0.0,
                  automatic_function_calling=t.AutomaticFunctionCallingConfig(disable=True))
        if self.cache_name:
            kw["cached_content"] = self.cache_name          # 캐시를 쓰면 system_instruction 을 따로 보내지 않는다
        else:
            kw["system_instruction"] = self._system
        if self.media_resolution:
            try:
                kw["media_resolution"] = getattr(t.MediaResolution, f"MEDIA_RESOLUTION_{self.media_resolution.upper()}")
            except AttributeError:                       # 옛 SDK — 설정 없이 간다
                self.media_resolution = None
        if self.thinking_budget is not None:
            kw["thinking_config"] = t.ThinkingConfig(thinking_budget=self.thinking_budget)
        elif self.thinking == "low":
            # 3.x 는 thinking_level, 2.5 는 thinking_budget 으로 생각을 줄인다(전사 작업이라 깊은 생각이 필요 없음)
            kw["thinking_config"] = t.ThinkingConfig(thinking_level="LOW") if re.search(r"gemini-[3-9]", self.model) else t.ThinkingConfig(thinking_budget=512)
        return t.GenerateContentConfig(**kw)

    def _call(self, img: Image.Image, prompt: str, schema, extra_parts: list | None = None):
        """이미지 한 장(또는 extra_parts 로 여러 장)과 지시문을 보내 schema 대로 받는다."""
        t = self.types
        contents = extra_parts if extra_parts is not None else [t.Part.from_bytes(data=to_png_bytes(img), mime_type="image/png"), prompt]
        last_msg = ""; tried_msgs: list[str] = []
        attempts = max(self.retries, 5)
        for i in range(attempts):
            try:
                resp = self.client.models.generate_content(model=self.model, contents=contents, config=self._config(schema))
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
                last_msg, retry = friendly_error(e, self.model)
                code = getattr(e, "code", None) or getattr(e, "status_code", None)
                if code == 429 or "RESOURCE_EXHAUSTED" in str(e):
                    with self._lock:
                        self.n_429 += 1
                if (self.thinking or self.thinking_budget is not None) and code == 400 and "think" in str(e).lower():
                    print(f"주의: {self.model} 이 생각 설정을 받지 않아 모델 기본값으로 계속합니다.", flush=True)
                    self.thinking = None; self.thinking_budget = None
                    continue
                if self.media_resolution and code == 400 and "resolution" in str(e).lower():
                    print(f"주의: {self.model} 이 이미지 해상도 설정(media_resolution)을 받지 않아 기본값으로 계속합니다.", flush=True)
                    self.media_resolution = None
                    continue
                if self.cache_name and ("cachedContent" in str(e) or "cached content" in str(e).lower() or (code == 403 and "cache" in str(e).lower())):
                    self._make_cache()                       # 캐시가 만료·소실되면 다시 만들고 재시도
                    if not self.cache_name:
                        print("주의: 지침 캐시를 다시 만들지 못해 일반 호출로 계속합니다.", flush=True)
                    continue
                if code in (403, 404) and self.api == "auto" and not self._switched and self.key.startswith("AQ."):
                    tried_msgs.append(("AI Studio 경로: " if not self.vertexai else "Vertex 경로: ") + last_msg)
                    self._switched = True; self.vertexai = not self.vertexai
                    self.client = make_client(self.key, self.vertexai)
                    print(f"주의: {'Vertex 익스프레스' if self.vertexai else 'AI Studio'} 접속 경로로 바꿔 다시 시도합니다.", flush=True)
                    continue
                if not retry:                      # 키·권한·모델 이름 오류는 다시 해도 같다
                    tried_msgs.append(("Vertex 경로: " if self.vertexai else "AI Studio 경로: ") + last_msg) if tried_msgs else None
                    final = " / ".join(tried_msgs) if tried_msgs else last_msg
                    if code in (403, 404):
                        try:                       # 되는 모델을 찾아 바로 제안한다
                            res = check_key(self.key, self.model, probe=True, max_probe=6)
                            if res.get("working_model"):
                                final += f" → 호출 확인된 모델: '{res['working_model']}'. [키 확인·모델 고르기] 에서 이 모델로 바꾸십시오."
                        except Exception:
                            pass
                    raise ReaderError(final) from e
                if code == 429 or "RESOURCE_EXHAUSTED" in str(e):
                    d = retry_delay_seconds(str(e))
                    print(f"주의: 요청 한도 초과 — {d:.0f}초 뒤 다시 시도 ({i + 1}/{attempts})", flush=True)
                    time.sleep(d)
                else:
                    time.sleep(2 ** i)             # 서버·형식 오류는 잠시 뒤 다시
        raise ReaderError(f"{attempts}회 시도 뒤에도 실패: {last_msg}", fatal=False)

    def usage_begin(self) -> None:
        """이 스레드에서 이어지는 호출들의 토큰을 쪽 단위로 따로 센다(여러 쪽을 동시에 읽어도 섞이지 않게)."""
        self._tls.page = {}

    def usage_end(self) -> dict:
        d = getattr(self._tls, "page", None) or {}
        self._tls.page = None
        return d

    def read_page(self, img: Image.Image, file_name: str, pdf_page: int, total: int, extra: str = "") -> PageReading:
        r = self._call(img, prompts.page_prompt(file_name, pdf_page, total, extra), PageReading)
        r.pdf_page = pdf_page
        return r

    def reread_region(self, crop: Image.Image, kind: str, region_id: str, pdf_page: int) -> RegionReread:
        return self._call(crop, prompts.region_prompt(kind, region_id, pdf_page), RegionReread)

    def adjudicate(self, crop: Image.Image, candidates: list[str], pre: str, post: str, horizontal: bool, pdf_page: int) -> GlyphChoice:
        """3차 글자 판별: 글자 하나를 크게 잘라 보내 후보 가운데 어느 것인지 자형만으로 묻는다."""
        return self._call(crop, prompts.adjudicate_prompt(candidates, pre, post, horizontal, pdf_page), GlyphChoice)

    def reread_regions(self, crops: list[tuple[str, str, Image.Image]], pdf_page: int) -> dict[str, RegionReread]:
        """여러 문자 영역 조각을 한 호출로 다시 판독한다. 돌려주는 사전에 빠진 영역은 호출자가 낱개로 다시 묻는다."""
        t = self.types
        parts = [prompts.regions_prompt([(rid, kind) for rid, kind, _ in crops], pdf_page)]
        for rid, kind, im in crops:
            parts.append(f"【{rid}】"); parts.append(t.Part.from_bytes(data=to_png_bytes(im), mime_type="image/png"))
        res = self._call(None, "", MultiRegionReread, extra_parts=parts)
        return {it.region_id: RegionReread(columns=it.columns, uncertain=it.uncertain) for it in res.regions if it.columns}
