"""Provider adapters for subtitle translation. API keys are passed in memory only."""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from typing import Any

from translator import OllamaCloudTranslator, ProviderHTTPError

PROVIDERS = {
    "gemini": {"label": "Google Gemini", "model": "gemini-3.8-flash"},
    "openai": {"label": "OpenAI GPT", "model": "gpt-6-luna"},
    "anthropic": {"label": "Anthropic Claude", "model": "claude-haiku-5-5"},
    "ollama": {"label": "Ollama Cloud", "model": "gpt-oss:120b"},
}

_SYSTEM = (
    "You translate subtitle lines into concise, natural spoken Vietnamese for dubbing. "
    "Preserve meaning, names, technical terms, numbers, and the original sequence. "
    "Use neighboring lines only as context; do not merge or omit lines. "
    'Return only valid JSON in this exact shape: '
    '{"items":[{"id":0,"vi":"Vietnamese translation"}]}. '
    "Include every input id exactly once and no extra text."
)


def _prompt(texts: list[str]) -> str:
    items = [{"id": i, "text": text} for i, text in enumerate(texts)]
    return json.dumps(items, ensure_ascii=False)


def _extract_json_object(text: str) -> dict[str, Any]:
    value = text.strip()
    value = re.sub(r"^```(?:json)?\s*", "", value, flags=re.IGNORECASE)
    value = re.sub(r"\s*```$", "", value)
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        start, end = value.find("{"), value.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("provider_invalid_json") from None
        try:
            parsed = json.loads(value[start:end + 1])
        except json.JSONDecodeError:
            raise ValueError("provider_invalid_json") from None
    if not isinstance(parsed, dict):
        raise ValueError("provider_invalid_json")
    return parsed


def _translations_from_json(text: str, count: int) -> list[str]:
    parsed = _extract_json_object(text)
    items = parsed.get("items")
    if not isinstance(items, list):
        raise ValueError("provider_batch_missing_items")
    by_id: dict[int, str] = {}
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("provider_batch_invalid_item")
        try:
            item_id = int(item["id"])
            translated = str(item["vi"]).strip()
        except (KeyError, TypeError, ValueError):
            raise ValueError("provider_batch_invalid_item") from None
        if item_id in by_id or not translated:
            raise ValueError("provider_batch_duplicate_or_empty")
        by_id[item_id] = translated
    if set(by_id) != set(range(count)):
        raise ValueError("provider_batch_mismatch")
    return [by_id[i] for i in range(count)]


def _retry_after(headers) -> int:
    try:
        return max(0, int(float(headers.get("Retry-After", "0"))))
    except (TypeError, ValueError):
        return 0


class APITranslator:
    provider = ""
    url = ""

    def __init__(self, api_key: str, model: str, timeout: float = 60.0):
        if not api_key or not api_key.strip():
            raise ValueError("API key chưa được nhập")
        self._key = api_key.strip()
        self.model = model.strip()
        if not self.model:
            raise ValueError("Tên model không được để trống")
        self.timeout = timeout

    def _post(self, payload: dict, headers: dict[str, str]) -> dict[str, Any]:
        request = urllib.request.Request(
            self.url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json", **headers},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                result = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            # Do not include response bodies or request headers in exceptions/logs; those
            # can contain provider diagnostics or credentials echoed by a proxy.
            raise ProviderHTTPError(
                error.code,
                f"{self.provider} http_status={error.code}",
                _retry_after(error.headers),
            ) from None
        except urllib.error.URLError as error:
            reason = getattr(error, "reason", None)
            raise RuntimeError(f"{self.provider}_network_error:{type(reason).__name__}") from None
        except TimeoutError:
            raise RuntimeError(f"{self.provider}_timeout") from None
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise RuntimeError(f"{self.provider}_invalid_http_json") from None
        if not isinstance(result, dict):
            raise RuntimeError(f"{self.provider}_invalid_response")
        return result

    def translate_batch(self, texts: list[str], target: str = "vi") -> list[str]:
        if target != "vi":
            raise ValueError("Chỉ hỗ trợ dịch sang tiếng Việt")
        if not texts:
            return []
        return _translations_from_json(self._generate(texts), len(texts))

    def translate(self, text: str, target: str = "vi") -> str:
        return self.translate_batch([text], target)[0]

    def _generate(self, texts: list[str]) -> str:
        raise NotImplementedError


class GeminiTranslator(APITranslator):
    provider = "gemini"
    url = "https://generativelanguage.googleapis.com/v1beta/interactions"

    def _generate(self, texts: list[str]) -> str:
        data = self._post(
            {"model": self.model, "input": f"{_SYSTEM}\n\nInput items:\n{_prompt(texts)}", "store": False},
            {"x-goog-api-key": self._key},
        )
        chunks: list[str] = []
        for step in data.get("steps", []):
            if not isinstance(step, dict) or step.get("type") != "model_output":
                continue
            for block in step.get("content", []):
                if isinstance(block, dict) and block.get("type") == "text":
                    chunks.append(str(block.get("text", "")))
        output = "\n".join(part for part in chunks if part).strip()
        if not output:
            raise RuntimeError("gemini_empty_output")
        return output


class OpenAITranslator(APITranslator):
    provider = "openai"
    url = "https://api.openai.com/v1/responses"

    def _generate(self, texts: list[str]) -> str:
        data = self._post(
            {
                "model": self.model,
                "instructions": _SYSTEM,
                "input": _prompt(texts),
                "store": False,
            },
            {"Authorization": f"Bearer {self._key}"},
        )
        chunks: list[str] = []
        for item in data.get("output", []):
            if not isinstance(item, dict) or item.get("type") != "message":
                continue
            for block in item.get("content", []):
                if isinstance(block, dict) and block.get("type") in {"output_text", "text"}:
                    chunks.append(str(block.get("text", "")))
        output = "\n".join(part for part in chunks if part).strip()
        if not output:
            raise RuntimeError("openai_empty_output")
        return output


class AnthropicTranslator(APITranslator):
    provider = "anthropic"
    url = "https://api.anthropic.com/v1/messages"

    def _generate(self, texts: list[str]) -> str:
        data = self._post(
            {
                "model": self.model,
                "max_tokens": 4096,
                "system": _SYSTEM,
                "messages": [{"role": "user", "content": _prompt(texts)}],
            },
            {"x-api-key": self._key, "anthropic-version": "2023-06-01"},
        )
        chunks = [
            str(block.get("text", ""))
            for block in data.get("content", [])
            if isinstance(block, dict) and block.get("type") == "text"
        ]
        output = "\n".join(part for part in chunks if part).strip()
        if not output:
            raise RuntimeError("anthropic_empty_output")
        return output


def create_provider(provider: str, api_key: str, model: str):
    if provider == "gemini":
        return GeminiTranslator(api_key, model)
    if provider == "openai":
        return OpenAITranslator(api_key, model)
    if provider == "anthropic":
        return AnthropicTranslator(api_key, model)
    if provider == "ollama":
        return OllamaCloudTranslator(api_key=api_key, model=model)
    raise ValueError("Nhà cung cấp API không được hỗ trợ")
