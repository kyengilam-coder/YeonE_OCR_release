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


class GeminiReader:
    name = "gemini"

    def __init__(self, model: str | None = None, api_key: str | None = None, thinking_budget: int | None = None, retries: int = 3):
        from google import genai           # 지연 임포트: mock 만 쓸 때는 SDK 없이도 돌아가게
        from google.genai import types
        key = api_key or os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        if not key:
            raise SystemExit("GEMINI_API_KEY 환경 변수가 없습니다. (PowerShell: $env:GEMINI_API_KEY='...', bash: export GEMINI_API_KEY=...)")
        self.types = types
        self.client = genai.Client(api_key=key)
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
        last = None
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
            except Exception as e:      # 네트워크·형식 오류는 잠시 뒤 다시
                last = e
                time.sleep(2 ** i)
        raise RuntimeError(f"Gemini 호출 실패({self.retries}회): {last}")

    def read_page(self, img: Image.Image, file_name: str, pdf_page: int, total: int, extra: str = "") -> PageReading:
        r = self._call(img, prompts.page_prompt(file_name, pdf_page, total, extra), PageReading)
        r.pdf_page = pdf_page
        return r

    def reread_region(self, crop: Image.Image, kind: str, region_id: str, pdf_page: int) -> RegionReread:
        return self._call(crop, prompts.region_prompt(kind, region_id, pdf_page), RegionReread)
