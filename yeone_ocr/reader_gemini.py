"""Gemini 판독기. google-genai SDK 로 이미지 + 지침을 보내 구조화 JSON 을 받는다.

환경 변수: GEMINI_API_KEY (또는 GOOGLE_API_KEY), GEMINI_MODEL (선택).
"""
from __future__ import annotations
import os, time, json
from PIL import Image
from .schema import PageReading, RegionReread
from .layout import to_png_bytes
from . import prompts

DEFAULT_MODEL = "gemini-2.5-pro"


class ReaderError(RuntimeError):
    """사용자에게 그대로 보여 줄 수 있는 한 줄 오류."""


def normalize_key(key: str | None) -> str:
    """붙여 넣을 때 섞인 공백·줄바꿈·따옴표·'GEMINI_API_KEY=' 머리를 걷어 낸다."""
    k = (key or "").strip().strip('"').strip("'")
    if "=" in k and k.split("=", 1)[0].strip().upper().endswith("API_KEY"):
        k = k.split("=", 1)[1]
    return "".join(k.split())


def key_shape_warning(key: str) -> str:
    """Gemini(AI Studio) 키는 보통 AIza 로 시작하는 39자다. 모양이 다르면 경고문을 돌려준다."""
    if not key:
        return "API 키가 비어 있습니다."
    if not key.startswith("AIza") or len(key) != 39:
        return f"키 모양이 이상합니다 (지금 {len(key)}자, 시작 '{key[:4]}'). AI Studio 키는 보통 AIza 로 시작하는 39자입니다. 키를 두 번 붙여 넣었거나 다른 값을 넣지 않았는지 확인하십시오."
    return ""


def friendly_error(e: Exception, model: str = "") -> tuple[str, bool]:
    """(한 줄 설명, 재시도 가치 여부)."""
    code = getattr(e, "code", None) or getattr(e, "status_code", None)
    msg = str(e)
    if "API_KEY_INVALID" in msg or "API key not valid" in msg or code == 401:
        return "API 키가 올바르지 않습니다. https://aistudio.google.com/apikey 에서 키를 만들어 통째로 다시 붙여 넣고 [키 확인] 을 누르십시오.", False
    if "PERMISSION_DENIED" in msg or code == 403:
        return "이 키로는 Gemini API 를 쓸 수 없습니다(권한 거부). 키의 제한 설정(HTTP 리퍼러 제한 등)을 풀거나 Generative Language API 가 켜진 프로젝트의 키를 쓰십시오.", False
    if "NOT_FOUND" in msg or code == 404:
        return f"모델 '{model}' 을 찾을 수 없습니다. [키 확인·모델 고르기] 로 쓸 수 있는 모델 이름을 고르십시오.", False
    if "RESOURCE_EXHAUSTED" in msg or code == 429:
        return "요청 한도(쿼터)를 넘었습니다. 잠시 뒤 다시 하거나 쪽 범위를 줄이십시오. 무료 등급이면 분당 요청 수 제한이 있습니다.", True
    if code and 500 <= int(code) < 600:
        return f"Gemini 서버 오류({code}). 잠시 뒤 다시 시도하십시오.", True
    if "timeout" in msg.lower() or "timed out" in msg.lower():
        return "Gemini 응답이 제한 시간 안에 오지 않았습니다. 인터넷 연결을 확인하고 다시 시도하십시오.", True
    if "validation error" in msg.lower() or "JSON" in msg:
        return "모델 응답이 정해진 형식(JSON)에 맞지 않았습니다. 같은 쪽을 다시 판독하면 대개 해결됩니다.", True
    return f"{type(e).__name__}: {msg[:300]}", True


def list_models(api_key: str) -> list[str]:
    """generateContent 를 지원하는 모델 이름 목록(models/ 머리 제거). 키가 틀리면 예외."""
    from google import genai
    from google.genai import types
    client = genai.Client(api_key=normalize_key(api_key), http_options=types.HttpOptions(timeout=30_000))
    names = []
    for m in client.models.list():
        acts = getattr(m, "supported_actions", None)
        if acts and "generateContent" not in acts:
            continue
        n = (getattr(m, "name", "") or "").removeprefix("models/")
        if n:
            names.append(n)
    return sorted(set(names))


def check_key(api_key: str, model: str = "") -> dict:
    """키가 쓸 수 있는지, 지정한 모델이 있는지 확인한다. 네트워크 한 번."""
    k = normalize_key(api_key)
    out = {"ok": False, "key_len": len(k), "shape_warning": key_shape_warning(k), "models": [], "model_ok": None, "message": ""}
    if not k:
        out["message"] = "API 키가 비어 있습니다."; return out
    try:
        models = list_models(k)
    except Exception as e:
        out["message"] = friendly_error(e, model)[0]; return out
    out["ok"] = True; out["models"] = models
    if model:
        out["model_ok"] = model in models
    out["message"] = f"키 확인됨. 쓸 수 있는 모델 {len(models)}개." + ("" if not model else (f" '{model}' 사용 가능." if out["model_ok"] else f" 그러나 '{model}' 은 목록에 없음 — 다른 이름을 고르십시오."))
    return out


class GeminiReader:
    name = "gemini"

    def __init__(self, model: str | None = None, api_key: str | None = None, thinking_budget: int | None = None, retries: int = 3):
        from google import genai           # 지연 임포트: mock 만 쓸 때는 SDK 없이도 돌아가게
        from google.genai import types
        key = normalize_key(api_key or os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"))
        if not key:
            raise ReaderError("GEMINI_API_KEY 가 없습니다. 창 프로그램의 'Gemini API 키' 칸에 넣거나 (PowerShell) $env:GEMINI_API_KEY='...' 로 설정하십시오.")
        warn = key_shape_warning(key)
        if warn:
            print("주의: " + warn, flush=True)
        self.types = types
        # 응답 제한 시간(ms). 없으면 네트워크 문제 때 창이 영영 기다릴 수 있다.
        self.client = genai.Client(api_key=key, http_options=types.HttpOptions(timeout=180_000))
        self.model = model or os.environ.get("GEMINI_MODEL") or DEFAULT_MODEL
        self.thinking_budget = thinking_budget
        self.retries = retries
        self._system = prompts.system_prompt()
        self.usage = {"calls": 0, "prompt_tokens": 0, "output_tokens": 0}

    def _config(self, schema):
        t = self.types
        kw = dict(system_instruction=self._system, response_mime_type="application/json",
                  response_schema=schema, temperature=0.0)
        if self.thinking_budget is not None:
            kw["thinking_config"] = t.ThinkingConfig(thinking_budget=self.thinking_budget)
        return t.GenerateContentConfig(**kw)

    def _call(self, img: Image.Image, prompt: str, schema):
        t = self.types
        part = t.Part.from_bytes(data=to_png_bytes(img), mime_type="image/png")
        last_msg = ""
        for i in range(self.retries):
            try:
                resp = self.client.models.generate_content(model=self.model, contents=[part, prompt], config=self._config(schema))
                self.usage["calls"] += 1
                um = getattr(resp, "usage_metadata", None)
                if um:
                    self.usage["prompt_tokens"] += getattr(um, "prompt_token_count", 0) or 0
                    self.usage["output_tokens"] += getattr(um, "candidates_token_count", 0) or 0
                text = resp.text or ""
                return schema.model_validate_json(text)
            except Exception as e:
                last_msg, retry = friendly_error(e, self.model)
                if not retry:                      # 키·권한·모델 이름 오류는 다시 해도 같다
                    raise ReaderError(last_msg) from e
                time.sleep(2 ** i)                 # 한도·서버·형식 오류는 잠시 뒤 다시
        raise ReaderError(f"{self.retries}회 시도 뒤에도 실패: {last_msg}")

    def read_page(self, img: Image.Image, file_name: str, pdf_page: int, total: int, extra: str = "") -> PageReading:
        r = self._call(img, prompts.page_prompt(file_name, pdf_page, total, extra), PageReading)
        r.pdf_page = pdf_page
        return r

    def reread_region(self, crop: Image.Image, kind: str, region_id: str, pdf_page: int) -> RegionReread:
        return self._call(crop, prompts.region_prompt(kind, region_id, pdf_page), RegionReread)
