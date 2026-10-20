"""Real SDK request serialization against in-memory HTTP; never a provider call."""
import base64
import io
import json
import socket
import unittest
from unittest.mock import patch

import httpx
from PIL import Image

from yeone_ocr.comparison_ai import AISettings, RESPONSE_SCHEMA, _request, _wire_schema


class SDKWireTests(unittest.TestCase):
    def setUp(self):
        stream = io.BytesIO()
        Image.new("RGB", (4, 4), "white").save(stream, "PNG")
        self.png = stream.getvalue()
        self.requests = []
        self.guard = patch.object(socket.socket, "connect", side_effect=AssertionError("Real network forbidden"))
        self.guard.start()
        self.addCleanup(self.guard.stop)

    def transport(self, response, wire=httpx, status=200):
        def handle(request):
            self.requests.append((request.url.path, json.loads(request.content)))
            return wire.Response(status, json=response, headers={"request-id": "offline-sdk-fixture"})
        return wire.MockTransport(handle)

    def test_openai_real_sdk_preserves_selected_model_images_schema_and_no_store(self):
        from openai import OpenAI
        from openai import _base_client
        wire = getattr(_base_client, "httpx2", None) or _base_client.httpx
        client = OpenAI(api_key="offline-fixture-key", base_url="https://offline.example.invalid/v1",
            max_retries=0, http_client=wire.Client(transport=self.transport({
                "id": "resp_offline", "object": "response", "created_at": 0, "status": "completed",
                "model": "selected-vision-model", "output": [{"type": "message", "id": "msg_offline",
                    "role": "assistant", "status": "completed", "content": [
                        {"type": "output_text", "text": '{"items":[]}', "annotations": []}]}],
                "usage": {"input_tokens": 7, "output_tokens": 5, "total_tokens": 12}}, wire)))
        self.addCleanup(client.close)
        text, complete, metadata = _request(client, AISettings("openai", "selected-vision-model"), "fixture-data", [self.png])
        self.assertEqual(text, '{"items":[]}')
        self.assertTrue(complete)
        self.assertEqual(metadata["actual_model"], "selected-vision-model")
        self.assertEqual(len(self.requests), 1)
        path, body = self.requests[0]
        self.assertEqual(path, "/v1/responses")
        self.assertEqual(body["model"], "selected-vision-model")
        self.assertIs(body["store"], False)
        self.assertEqual(body["text"]["format"]["schema"], RESPONSE_SCHEMA)
        picture = body["input"][0]["content"][1]
        self.assertEqual(base64.b64decode(picture["image_url"].split(",", 1)[1]), self.png)
        self.assertNotIn("tools", body)

    def test_anthropic_real_sdk_sends_image_with_text_and_returns_exact_response(self):
        from anthropic import Anthropic
        from anthropic import _base_client
        wire = getattr(_base_client, "httpx2", None) or _base_client.httpx
        client = Anthropic(api_key="offline-fixture-key", base_url="https://offline.example.invalid",
            max_retries=0, http_client=wire.Client(transport=self.transport({
                "id": "msg_offline", "type": "message", "role": "assistant", "model": "selected-vision-model",
                "content": [{"type": "text", "text": '{"items":[]}'}], "stop_reason": "end_turn",
                "stop_sequence": None, "usage": {"input_tokens": 7, "output_tokens": 5}}, wire)))
        self.addCleanup(client.close)
        text, complete, metadata = _request(client, AISettings("anthropic", "selected-vision-model"), "fixture-data", [self.png])
        self.assertEqual(text, '{"items":[]}')
        self.assertTrue(complete)
        self.assertEqual(metadata["actual_model"], "selected-vision-model")
        self.assertEqual(len(self.requests), 1)
        path, body = self.requests[0]
        self.assertEqual(path, "/v1/messages")
        self.assertEqual(body["model"], "selected-vision-model")
        self.assertEqual(base64.b64decode(body["messages"][0]["content"][1]["source"]["data"]), self.png)
        self.assertNotIn("tools", body)

    def test_openai_real_sdk_refusal_is_retained_and_cannot_be_a_completed_review(self):
        from openai import OpenAI, _base_client
        wire = getattr(_base_client, "httpx2", None) or _base_client.httpx
        refusal = "Offline fixture: cannot verify these characters."
        client = OpenAI(api_key="offline-fixture-key", base_url="https://offline.example.invalid/v1",
            max_retries=0, http_client=wire.Client(transport=self.transport({
                "id": "resp_refused", "object": "response", "created_at": 0, "status": "completed",
                "model": "selected-vision-model", "output": [{"type": "message", "id": "msg_refused",
                    "role": "assistant", "status": "completed", "content": [
                        {"type": "refusal", "refusal": refusal}]}], "usage": None}, wire)))
        self.addCleanup(client.close)
        text, complete, _metadata = _request(client, AISettings("openai", "selected-vision-model"), "fixture-data", [self.png])
        self.assertEqual(text, refusal)
        self.assertFalse(complete)
        self.assertEqual(len(self.requests), 1)

    def test_gemini_real_sdk_sends_schema_and_image_without_tools_or_retry(self):
        from google import genai
        from google.genai import types
        client = genai.Client(api_key="offline-fixture-key", http_options=types.HttpOptions(
            base_url="https://offline.example.invalid", api_version="v1beta", client_args={"transport": self.transport({
                "candidates": [{"finishReason": "STOP", "content": {"role": "model", "parts": [{"text": '{"items":[]}'}]}}],
                "usageMetadata": {"promptTokenCount": 7, "candidatesTokenCount": 5, "totalTokenCount": 12},
                "modelVersion": "selected-vision-model", "responseId": "offline-response"})}))
        self.addCleanup(client.close)
        text, complete, metadata = _request(client, AISettings("gemini", "selected-vision-model"), "fixture-data", [self.png])
        self.assertEqual(text, '{"items":[]}')
        self.assertTrue(complete)
        self.assertEqual(metadata["actual_model"], "selected-vision-model")
        self.assertEqual(len(self.requests), 1)
        path, body = self.requests[0]
        self.assertEqual(path, "/v1beta/models/selected-vision-model:generateContent")
        self.assertEqual(body["generationConfig"]["responseJsonSchema"], RESPONSE_SCHEMA)
        parts = [part for content in body["contents"] for part in content["parts"]]
        self.assertEqual(base64.urlsafe_b64decode(next(p["inlineData"]["data"] for p in parts if "inlineData" in p)), self.png)
        self.assertNotIn("tools", body)
        self.assertNotIn("thinkingConfig", body["generationConfig"])

    def test_gemini3_real_sdk_serializes_low_thinking_cap_and_target_schema(self):
        from google import genai
        from google.genai import types
        schema = {"type": "object", "properties": {"items": {"type": "array", "maxItems": 1,
                  "items": {"type": "string", "enum": ["approved-target"]}}}, "required": ["items"]}
        client = genai.Client(api_key="offline-fixture-key", http_options=types.HttpOptions(
            base_url="https://offline.example.invalid", api_version="v1beta", client_args={"transport": self.transport({
                "candidates": [{"finishReason": "MAX_TOKENS", "content": {"role": "model", "parts": [{"text": '{"items":[]}'}]}}],
                "usageMetadata": {"promptTokenCount": 7, "candidatesTokenCount": 5, "thoughtsTokenCount": 8,
                                  "totalTokenCount": 20}, "modelVersion": "gemini-3.1-pro-preview"})}))
        self.addCleanup(client.close)
        text, complete, metadata = _request(client, AISettings("gemini", "gemini-3.1-pro-preview"),
                                           "fixture-data", [self.png], response_schema=schema)
        self.assertEqual(text, '{"items":[]}')
        self.assertFalse(complete)
        self.assertFalse(metadata["response_complete"])
        self.assertEqual(metadata["finish_reason"], "MAX_TOKENS")
        self.assertEqual(metadata["usage"]["thoughts_token_count"], 8)
        self.assertEqual(len(self.requests), 1)
        config = self.requests[0][1]["generationConfig"]
        self.assertEqual(config["thinkingConfig"], {"thinkingLevel": "LOW"})
        self.assertEqual(config["maxOutputTokens"], 8192)
        self.assertEqual(config["responseJsonSchema"], _wire_schema(schema, "gemini"))
        self.assertNotIn("maxItems", config["responseJsonSchema"]["properties"]["items"])
        self.assertEqual(metadata["wire_schema_policy"], "gemini-basic-v1")

    def test_gemini_wire_subset_keeps_ids_kinds_and_does_not_mutate_other_providers(self):
        schema = {"type": "object", "additionalProperties": False, "required": ["items"], "properties": {
            "items": {"type": "array", "minItems": 1, "maxItems": 1, "items": {"type": "object",
                "additionalProperties": False, "required": ["target_id", "kind", "start", "reason"],
                "properties": {"target_id": {"type": "string", "enum": ["approved-target"]},
                    "kind": {"type": "string", "enum": ["recognition"]},
                    "start": {"type": "integer", "minimum": 0},
                    "reason": {"type": "string", "maxLength": 80}}}}}}
        original = json.dumps(schema, sort_keys=True)
        filtered = _wire_schema(schema, "gemini")
        self.assertEqual(json.dumps(schema, sort_keys=True), original)
        self.assertIs(_wire_schema(schema, "openai"), schema)
        self.assertIs(_wire_schema(schema, "anthropic"), schema)
        array = filtered["properties"]["items"]
        self.assertNotIn("minItems", array)
        self.assertNotIn("maxItems", array)
        fields = array["items"]["properties"]
        self.assertEqual(fields["target_id"]["enum"], ["approved-target"])
        self.assertEqual(fields["kind"]["enum"], ["recognition"])
        self.assertNotIn("minimum", fields["start"])
        self.assertNotIn("maxLength", fields["reason"])
        self.assertFalse(array["items"]["additionalProperties"])

    def test_gemini_real_sdk_invalid_argument_makes_one_request_without_retry(self):
        from google import genai
        from google.genai import types, errors
        client = genai.Client(api_key="offline-fixture-key", http_options=types.HttpOptions(
            base_url="https://offline.example.invalid", api_version="v1beta", client_args={"transport": self.transport({
                "error": {"code": 400, "status": "INVALID_ARGUMENT", "message": "Public synthetic rejection"}}, status=400)}))
        self.addCleanup(client.close)
        with self.assertRaises(errors.ClientError):
            _request(client, AISettings("gemini", "gemini-3.1-pro-preview"), "fixture-data", [self.png])
        self.assertEqual(len(self.requests), 1)


if __name__ == "__main__":
    unittest.main()
