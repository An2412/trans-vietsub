import json
import logging
import hashlib
import random
import re
import threading
import time
import urllib.error
import urllib.request
from typing import Protocol

log = logging.getLogger("uvicorn.error")


def _safe_provider_error(error: Exception) -> str:
    response = getattr(error, "response", None)
    status = getattr(error, "status_code", None) or getattr(response, "status_code", None)
    message = re.sub(r"https?://\S+", "[url-redacted]", str(error).replace("\n", " ").strip())
    message = re.sub(r"(?i)(api[_-]?key|token|authorization)=\S+", r"\1=[redacted]", message)
    prefix = f"{type(error).__name__} http_status={status}" if status else type(error).__name__
    return f"{prefix} detail={message[:180]}"


def is_rate_limited(error: Exception) -> bool:
    response = getattr(error, "response", None)
    status = getattr(error, "status_code", None) or getattr(response, "status_code", None)
    message = f"{type(error).__name__} {error}".casefold()
    return status == 429 or any(token in message for token in (
        "toomanyrequests", "too many requests", "rate limit", "rate-limit", "quota exceeded", "http 429"
    ))


def is_auth_error(error: Exception) -> bool:
    """Key API bị từ chối (hết hạn/thu hồi) — lỗi lặp lại mọi câu, phải cooldown dài."""
    response = getattr(error, "response", None)
    status = getattr(error, "status_code", None) or getattr(response, "status_code", None)
    return status in (401, 403)


class RateLimitedError(RuntimeError):
    def __init__(self, retry_after: int, providers: list[str] | None = None) -> None:
        self.retry_after = max(1, int(retry_after))
        self.providers = providers or []
        super().__init__(f"rate_limited retry_after={self.retry_after}s")


class GenerationNotSupported(RuntimeError):
    """Provider đang chạy không sinh được văn bản tự do (chưa cấu hình AI có API key)."""


class Translator(Protocol):
    def translate(self, text: str, target: str = "vi") -> str: ...


class GoogleWebTranslator:
    """Cần Internet. Không chính thức, có thể bị chặn theo IP."""

    def __init__(self) -> None:
        from deep_translator import GoogleTranslator
        self._cls = GoogleTranslator

    def translate(self, text: str, target: str = "vi") -> str:
        return self._cls(source="auto", target=target).translate(text)


class MyMemoryTranslator:
    """Cần Internet; dịch vụ công khai có thể giới hạn request."""

    _CODES = {"vi": "vi-VN"}

    def __init__(self, source: str = "en-GB") -> None:
        from deep_translator import MyMemoryTranslator as MM
        self._cls = MM
        self._source = source

    def translate(self, text: str, target: str = "vi") -> str:
        tgt = self._CODES.get(target, target)
        return self._cls(source=self._source, target=tgt).translate(text)


class ProviderHTTPError(RuntimeError):
    def __init__(self, status_code: int, message: str, retry_after: int = 0) -> None:
        self.status_code = status_code
        self.retry_after = retry_after
        super().__init__(message)


_BATCH_SYSTEM = (
    "You are a professional subtitle translator. Translate every item into natural, "
    "concise spoken Vietnamese suitable for dubbing. Keep technical terms, product names, "
    "code identifiers and proper nouns unchanged when that is how Vietnamese speakers say them. "
    "Never add explanations. Items are consecutive subtitle lines of one video, so use "
    "neighbouring lines for context, but translate each line separately and do not merge or split them. "
    'Reply ONLY with JSON: {"items":[{"id":<same id>,"vi":"<translation>"}]} containing every id exactly once.'
)


class OllamaCloudTranslator:
    """Ollama hosted API. The key is supplied by the OS credential store."""

    def __init__(self, api_key: str | None = None, model: str | None = None,
                 host: str | None = None, timeout: float = 60.0) -> None:
        self._key = (api_key or "").strip()
        if not self._key:
            raise RuntimeError("API key chưa được cấu hình")
        self._model = model or "gpt-oss:120b"
        self._host = (host or "https://ollama.com").rstrip("/")
        self._timeout = timeout
        self._think = ""

    def _post_chat(self, payload: dict) -> dict:
        req = urllib.request.Request(
            f"{self._host}/api/chat",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Authorization": f"Bearer {self._key}", "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as res:
                return json.loads(res.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            try:
                retry_after = int(float(error.headers.get("Retry-After", "0")))
            except (TypeError, ValueError):
                retry_after = 0
            raise ProviderHTTPError(error.code, f"ollama http_status={error.code}", retry_after) from None
        except urllib.error.URLError as error:
            raise RuntimeError(f"ollama unreachable: {type(error.reason).__name__}") from None

    def translate_batch(self, texts: list[str], target: str = "vi") -> list[str]:
        if target != "vi":
            raise ValueError("OllamaCloudTranslator hiện chỉ hỗ trợ target=vi")
        items = [{"id": i, "text": t} for i, t in enumerate(texts)]
        payload = {
            "model": self._model,
            "stream": False,
            "messages": [
                {"role": "system", "content": _BATCH_SYSTEM},
                {"role": "user", "content": json.dumps(items, ensure_ascii=False)},
            ],
            "format": {
                "type": "object",
                "properties": {"items": {"type": "array", "items": {
                    "type": "object",
                    "properties": {"id": {"type": "integer"}, "vi": {"type": "string"}},
                    "required": ["id", "vi"],
                }}},
                "required": ["items"],
            },
            "options": {"temperature": 0},
        }
        if self._think:
            payload["think"] = {"true": True, "false": False}.get(self._think.lower(), self._think)
        data = self._post_chat(payload)
        content = (data.get("message") or {}).get("content", "")
        try:
            parsed = json.loads(content)
            by_id = {int(it["id"]): str(it["vi"]).strip() for it in parsed["items"]}
        except (ValueError, KeyError, TypeError) as error:
            raise ValueError(f"ollama_bad_json:{type(error).__name__}") from None
        if set(by_id) != set(range(len(texts))) or not all(by_id.values()):
            raise ValueError("ollama_batch_mismatch")
        return [by_id[i] for i in range(len(texts))]

    def translate(self, text: str, target: str = "vi") -> str:
        return self.translate_batch([text], target)[0]

    def generate_text(self, prompt: str, system: str = "") -> str:
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        res = self._post_chat({
            "model": self._model,
            "stream": False,
            "messages": messages,
        })
        msg = res.get("message", {}).get("content", "").strip()
        if not msg:
            raise RuntimeError("ollama_empty_output")
        return msg


class FallbackTranslator:
    """Serial provider access, exponential backoff and temporary provider cooldown."""

    MIN_INTERVAL = 0.8
    RATE_RETRIES = 2
    RATE_BACKOFF_BASE_S = 2.0
    RATE_BACKOFF_CAP_S = 8.0
    RATE_COOLDOWN_S = 60
    AUTH_COOLDOWN_S = 300

    def __init__(self, providers: list[tuple[str, Translator]]) -> None:
        self._providers = providers
        self._cache: dict[tuple[str, str], str] = {}
        self._cooldowns: dict[str, float] = {}
        self._lock = threading.Lock()
        self._last = 0.0

    def translate(self, text: str, target: str = "vi") -> str:
        key = (target, text)
        text_id = hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:10]
        with self._lock:
            if key in self._cache:
                return self._cache[key]

            last_err: Exception | None = None
            limited_until: dict[str, float] = {}
            now = time.monotonic()
            available = [(name, provider) for name, provider in self._providers
                         if self._cooldowns.get(name, 0.0) <= now]

            if not available:
                retry_after = max(1, int(max(self._cooldowns.values(), default=now) - now))
                names = [name for name, _ in self._providers]
                log.warning("all_translation_providers_cooling_down retry_after=%ds providers=%s", retry_after, ",".join(names))
                raise RateLimitedError(retry_after, names)

            for name, provider in available:
                for attempt in range(self.RATE_RETRIES + 1):
                    wait = self.MIN_INTERVAL - (time.monotonic() - self._last)
                    if wait > 0:
                        time.sleep(wait)
                    try:
                        out = provider.translate(text, target)
                        self._last = time.monotonic()
                        if out and out.strip():
                            self._cache[key] = out
                            if len(self._cache) > 5000:
                                self._cache.clear()
                                self._cache[key] = out
                            self._cooldowns.pop(name, None)
                            return out
                        last_err = RuntimeError("empty translation")
                        break
                    except Exception as error:
                        self._last = time.monotonic()
                        last_err = error
                        if is_rate_limited(error):
                            delay = min(self.RATE_BACKOFF_CAP_S, self.RATE_BACKOFF_BASE_S * (2 ** attempt))
                            if attempt < self.RATE_RETRIES:
                                delay += random.uniform(0, 0.25)
                                log.warning(
                                    "provider_rate_limited text_id=%s retry_in=%.1fs attempt=%d/%d provider=%s error=%s",
                                    text_id, delay, attempt + 1, self.RATE_RETRIES, name, _safe_provider_error(error),
                                )
                                time.sleep(delay)
                                continue
                            until = time.monotonic() + self.RATE_COOLDOWN_S
                            self._cooldowns[name] = until
                            limited_until[name] = until
                            log.warning(
                                "provider_cooldown text_id=%s seconds=%d provider=%s error=%s",
                                text_id, self.RATE_COOLDOWN_S, name, _safe_provider_error(error),
                            )
                        else:
                            if is_auth_error(error):
                                self._cooldowns[name] = time.monotonic() + self.AUTH_COOLDOWN_S
                                log.warning(
                                    "provider_auth_failed text_id=%s provider=%s error=%s cooldown_seconds=%d",
                                    text_id, name, _safe_provider_error(error), self.AUTH_COOLDOWN_S,
                                )
                            else:
                                log.warning("provider_failed text_id=%s provider=%s error=%s", text_id, name, _safe_provider_error(error))
                        break

            if limited_until:
                retry_after = max(1, int(max(limited_until.values()) - time.monotonic()))
                raise RateLimitedError(retry_after, list(limited_until)) from last_err
            raise last_err or RuntimeError("no translation provider")

    def generate_text(self, prompt: str, system: str = "") -> str:
        """Gọi provider để sinh văn bản tự do (dùng cho tóm tắt bài học)."""
        key = ("__generate__", prompt, system)
        with self._lock:
            if key in self._cache:
                return self._cache[key]

        last_err: Exception | None = None
        now = time.monotonic()
        available = [(name, provider) for name, provider in self._providers
                     if hasattr(provider, "generate_text") and self._cooldowns.get(name, 0.0) <= now]
        if not available:
            raise GenerationNotSupported(
                "Chưa cấu hình provider AI (Gemini/OpenAI/Claude/Ollama) để tóm tắt. "
                "Hãy lưu API key trong popup Viet Dub."
            )

        for name, provider in available:
            for attempt in range(self.RATE_RETRIES + 1):
                wait = self.MIN_INTERVAL - (time.monotonic() - self._last)
                if wait > 0:
                    time.sleep(wait)
                try:
                    out = provider.generate_text(prompt, system)
                    self._last = time.monotonic()
                    if out and out.strip():
                        with self._lock:
                            self._cache[key] = out
                            if len(self._cache) > 5000:
                                self._cache.clear()
                                self._cache[key] = out
                        self._cooldowns.pop(name, None)
                        return out
                    last_err = RuntimeError("empty generated output")
                    break
                except Exception as error:
                    self._last = time.monotonic()
                    last_err = error
                    if is_rate_limited(error):
                        delay = min(self.RATE_BACKOFF_CAP_S, self.RATE_BACKOFF_BASE_S * (2 ** attempt))
                        if attempt < self.RATE_RETRIES:
                            delay += random.uniform(0, 0.25)
                            log.warning(
                                "generate_rate_limited retry_in=%.1fs attempt=%d/%d provider=%s error=%s",
                                delay, attempt + 1, self.RATE_RETRIES, name, _safe_provider_error(error),
                            )
                            time.sleep(delay)
                            continue
                        until = time.monotonic() + self.RATE_COOLDOWN_S
                        self._cooldowns[name] = until
                        log.warning(
                            "generate_provider_cooldown seconds=%d provider=%s error=%s",
                            self.RATE_COOLDOWN_S, name, _safe_provider_error(error),
                        )
                    else:
                        if is_auth_error(error):
                            self._cooldowns[name] = time.monotonic() + self.AUTH_COOLDOWN_S
                            log.warning(
                                "generate_provider_auth_failed provider=%s error=%s cooldown_seconds=%d",
                                name, _safe_provider_error(error), self.AUTH_COOLDOWN_S,
                            )
                        else:
                            log.warning("generate_provider_failed provider=%s error=%s", name, _safe_provider_error(error))
                    break

        raise last_err or RuntimeError("no generation provider")


def _translate_batch_split(provider, texts: list[str], target: str, depth: int = 0) -> list[str]:
    """Gọi provider một lần; nếu model trả sai số lượng thì chia đôi batch (tối đa 3 cấp)."""
    try:
        return provider.translate_batch(texts, target)
    except ValueError:
        if len(texts) == 1 or depth >= 3:
            raise
        mid = len(texts) // 2
        return (_translate_batch_split(provider, texts[:mid], target, depth + 1)
                + _translate_batch_split(provider, texts[mid:], target, depth + 1))


def _fallback_translate_batch(self: "FallbackTranslator", texts: list[str], target: str = "vi") -> list[str]:
    """Một request cho cả batch. Provider có translate_batch được dùng trước (kèm retry/cooldown);
    nếu thất bại (không phải 429 dài hạn) thì dịch từng câu bằng các provider còn lại."""
    if not texts:
        return []
    with self._lock:
        now = time.monotonic()
        batch_providers = [(n, p) for n, p in self._providers
                           if hasattr(p, "translate_batch") and self._cooldowns.get(n, 0.0) <= now]
        last_err: Exception | None = None
        limited_until: dict[str, float] = {}
        for name, provider in batch_providers:
            for attempt in range(self.RATE_RETRIES + 1):
                wait = self.MIN_INTERVAL - (time.monotonic() - self._last)
                if wait > 0:
                    time.sleep(wait)
                try:
                    out = _translate_batch_split(provider, texts, target)
                    self._last = time.monotonic()
                    self._cooldowns.pop(name, None)
                    return out
                except Exception as error:
                    self._last = time.monotonic()
                    last_err = error
                    if is_rate_limited(error):
                        hinted = int(getattr(error, "retry_after", 0) or 0)
                        delay = max(hinted, min(self.RATE_BACKOFF_CAP_S, self.RATE_BACKOFF_BASE_S * (2 ** attempt)))
                        if attempt < self.RATE_RETRIES and delay <= self.RATE_BACKOFF_CAP_S:
                            log.warning("batch_rate_limited provider=%s retry_in=%.1fs attempt=%d/%d error=%s",
                                        name, delay, attempt + 1, self.RATE_RETRIES, _safe_provider_error(error))
                            time.sleep(delay + random.uniform(0, 0.25))
                            continue
                        cooldown = max(self.RATE_COOLDOWN_S, hinted)
                        self._cooldowns[name] = time.monotonic() + cooldown
                        limited_until[name] = self._cooldowns[name]
                        log.warning("batch_provider_cooldown provider=%s seconds=%d error=%s",
                                    name, cooldown, _safe_provider_error(error))
                    else:
                        if is_auth_error(error):
                            cooldown = self.AUTH_COOLDOWN_S
                            self._cooldowns[name] = time.monotonic() + cooldown
                            log.warning("batch_provider_auth_failed provider=%s error=%s cooldown_seconds=%d",
                                        name, _safe_provider_error(error), cooldown)
                        else:
                            log.warning("batch_provider_failed provider=%s error=%s", name, _safe_provider_error(error))
                    break
    # Fallback từng câu (translate() tự khóa/cooldown). Chỉ chạy nếu còn provider khác.
    try:
        return [self.translate(t, target) for t in texts]
    except RateLimitedError:
        if limited_until:
            retry_after = max(1, int(max(limited_until.values()) - time.monotonic()))
            raise RateLimitedError(retry_after, list(limited_until)) from last_err
        raise


FallbackTranslator.translate_batch = _fallback_translate_batch


def free_providers() -> list[tuple[str, Translator]]:
    """Chuỗi dịch miễn phí, dùng làm lưới an toàn khi provider API lỗi."""
    return [
        ("mymemory", MyMemoryTranslator()),
        ("google", GoogleWebTranslator()),
    ]


def build_translator() -> Translator:
    # Premium/API providers are selected explicitly in the extension popup and
    # receive their key from the OS credential store. No .env key is loaded here.
    providers = free_providers()
    log.warning("translation_providers=%s", ",".join(name for name, _ in providers))
    return FallbackTranslator(providers)
