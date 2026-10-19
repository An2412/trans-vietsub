import asyncio
import base64
import hashlib
import logging
import re
from pathlib import Path
from typing import Any

import edge_tts
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from api_providers import PROVIDERS, create_provider
from local_cache import LocalCache
from provider_settings import ProviderConfigurationError, ProviderSettings, SecureStoreUnavailable
from translator import FallbackTranslator, ProviderHTTPError, RateLimitedError, build_translator
from tts_cache import TTSCache

log = logging.getLogger("uvicorn.error")

VOICE = "vi-VN-HoaiMyNeural"  # hoặc vi-VN-NamMinhNeural
MAX_BATCH = 16
CACHE_DB = Path(__file__).resolve().parent / "data" / "vietdub.sqlite3"


def text_fingerprint(text: str) -> str:
    """Mã ngắn để nối các log cùng một câu mà không ghi nội dung câu."""
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:10]


def safe_error_detail(error: Exception) -> str:
    response = getattr(error, "response", None)
    status = getattr(error, "status_code", None) or getattr(response, "status_code", None)
    message = str(error).replace("\n", " ").strip()
    message = re.sub(r"https?://\S+", "[url-redacted]", message)
    message = re.sub(r"(?i)(api[_-]?key|token|authorization)=\S+", r"\1=[redacted]", message)
    if status:
        return f"{type(error).__name__} http_status={status} detail={message[:180]}"
    return f"{type(error).__name__} detail={message[:180]}"


app = FastAPI(title="viet-dub")
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"chrome-extension://.*",
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
    expose_headers=["Retry-After"],
)

_ALLOWED_HOSTS = {"127.0.0.1", "localhost"}


@app.middleware("http")
async def local_only_guard(request, call_next):
    """Chỉ nhận request từ extension: chặn trang web bất kỳ gọi POST tới server cục bộ
    (CSRF, ví dụ /provider-config/clear) và Host lạ (DNS rebinding)."""
    host = request.headers.get("host", "").rsplit(":", 1)[0].strip("[]")
    origin = request.headers.get("origin")
    if host not in _ALLOWED_HOSTS or (origin and not origin.startswith("chrome-extension://")):
        return JSONResponse({"detail": "forbidden_origin"}, status_code=403)
    return await call_next(request)


local_cache = LocalCache(CACHE_DB)
provider_settings = ProviderSettings(local_cache)
audio_cache = TTSCache(
    max_entries=256,
    max_bytes=32 * 1024 * 1024,
    persistent_store=local_cache,
)


class UnavailableTranslator:
    """Keeps the server responsive when an explicitly selected key cannot be read."""
    def __init__(self, message: str):
        self.message = message
        self._providers = []

    def translate(self, text: str, target: str = "vi") -> str:
        raise ProviderConfigurationError(self.message)

    def translate_batch(self, texts: list[str], target: str = "vi") -> list[str]:
        raise ProviderConfigurationError(self.message)


def _make_active_translator():
    active = provider_settings.active_config()
    if not active:
        return build_translator()
    provider, model, api_key = active
    return FallbackTranslator([(provider, create_provider(provider, api_key, model))])


try:
    translator = _make_active_translator()
except (SecureStoreUnavailable, ProviderConfigurationError) as error:
    log.error("provider_config_unavailable reason=%s", type(error).__name__)
    translator = UnavailableTranslator(str(error))


class TranslateReq(BaseModel):
    text: str = Field(min_length=1, max_length=500)
    target: str = "vi"


class TranslateBatchReq(BaseModel):
    texts: list[str] = Field(min_length=1, max_length=MAX_BATCH)
    target: str = "vi"


class ProviderConfigReq(BaseModel):
    provider: str = Field(min_length=1, max_length=32)
    model: str = Field(min_length=1, max_length=128)
    api_key: Any = None


class ProviderTestReq(BaseModel):
    provider: str = Field(min_length=1, max_length=32)
    model: str = Field(min_length=1, max_length=128)
    api_key: Any = None


def validated_api_key(value: Any) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise HTTPException(status_code=400, detail="API key phải là chuỗi ký tự; giá trị không được trả lại trong lỗi.")
    if len(value) > 4096:
        raise HTTPException(status_code=400, detail="API key vượt giới hạn độ dài 4096 ký tự.")
    return value


def translate_batch_with_cache(texts: list[str], target: str = "vi") -> list[tuple[str, str]]:
    """Tra cache từng câu, gom các câu chưa có vào ĐÚNG MỘT request tới provider."""
    selection = provider_settings.selection()
    cache_namespace = f"{selection[0]}:{selection[1]}" if selection else "fallback"
    results: list[tuple[str, str] | None] = [None] * len(texts)
    missing: dict[str, list[int]] = {}
    for i, text in enumerate(texts):
        try:
            cached = local_cache.get_translation(text, target, cache_namespace)
        except Exception as error:
            log.warning("translation_cache_read_failed text_id=%s error=%s", text_fingerprint(text), safe_error_detail(error))
            cached = None
        if cached is not None:
            results[i] = (cached, "hit")
        else:
            missing.setdefault(text, []).append(i)
    if missing:
        unique = list(missing)
        translated = translator.translate_batch(unique, target)
        for text, vi in zip(unique, translated):
            status = "miss"
            try:
                local_cache.set_translation(text, target, vi, cache_namespace)
            except Exception as error:
                status = "uncached"
                log.warning("translation_cache_write_failed text_id=%s error=%s", text_fingerprint(text), safe_error_detail(error))
            for i in missing[text]:
                results[i] = (vi, status)
        log.info("translate_batch requested=%d cache_hits=%d sent=%d", len(texts), len(texts) - sum(len(v) for v in missing.values()), len(unique))
    return [r for r in results if r is not None]


def translate_with_cache(text: str, target: str = "vi") -> tuple[str, str]:
    selection = provider_settings.selection()
    cache_namespace = f"{selection[0]}:{selection[1]}" if selection else "fallback"
    try:
        cached = local_cache.get_translation(text, target, cache_namespace)
    except Exception as error:
        log.warning("translation_cache_read_failed text_id=%s error=%s", text_fingerprint(text), safe_error_detail(error))
        cached = None
    if cached is not None:
        log.info("translation_cache status=hit text_id=%s target=%s", text_fingerprint(text), target)
        return cached, "hit"
    result = translator.translate(text, target)
    try:
        local_cache.set_translation(text, target, result, cache_namespace)
        log.info("translation_cache status=miss_stored text_id=%s target=%s", text_fingerprint(text), target)
        return result, "miss"
    except Exception as error:
        log.warning("translation_cache_write_failed text_id=%s error=%s", text_fingerprint(text), safe_error_detail(error))
        return result, "uncached"


async def synth(text: str) -> bytes:
    buf = bytearray()
    async for chunk in edge_tts.Communicate(text, VOICE).stream():
        if chunk["type"] == "audio":
            buf += chunk["data"]
    return bytes(buf)


def rate_limit_http_error(error: RateLimitedError, text_id: str) -> HTTPException:
    wait = max(1, int(error.retry_after))
    providers = ",".join(error.providers) or "unknown"
    log.warning("request_rate_limited text_id=%s retry_after=%ds providers=%s", text_id, wait, providers)
    return HTTPException(
        status_code=429,
        detail=f"rate_limited:retry_after={wait}:id={text_id}",
        headers={"Retry-After": str(wait)},
    )


@app.get("/health")
def health():
    names = [name for name, _ in getattr(translator, "_providers", [])]
    config = provider_settings.status()
    return {
        "status": "ok",
        "translation_providers": names,
        "active_provider": config["provider"],
        "active_model": config["model"],
        "provider_key_configured": config["has_api_key"],
    }


def provider_error_message(error: Exception) -> tuple[str, str]:
    status = getattr(error, "status_code", None)
    if status == 401:
        return "invalid_key", "API key bị từ chối (401). Kiểm tra key và đúng dịch vụ/provider."
    if status == 403:
        return "permission_denied", "Key hợp lệ nhưng chưa có quyền, API chưa bật hoặc tài khoản chưa được cấp quyền."
    if status == 404:
        return "model_not_found", "Không tìm thấy model. Kiểm tra model ID và quyền truy cập model."
    if status == 429:
        wait = max(0, int(getattr(error, "retry_after", 0) or 0))
        suffix = f" Thử lại sau khoảng {wait} giây." if wait else " Kiểm tra quota, billing hoặc giới hạn tốc độ."
        return "rate_limited", "Provider giới hạn request/quota (429)." + suffix
    if status == 400:
        return "bad_request", "Provider từ chối request (400). Kiểm tra API key, model ID, API đã bật và quyền model."
    if status and status >= 500:
        return "provider_unavailable", f"Dịch vụ provider đang lỗi (HTTP {status}); hãy thử lại sau."
    if "network_error" in str(error) or "timeout" in str(error):
        return "network_error", "Không tới được API provider. Kiểm tra Internet, DNS hoặc firewall."
    if "invalid_json" in str(error) or "batch_" in str(error):
        return "response_format", "Model trả kết quả không đúng định dạng subtitle; thử model khác hoặc thử lại."
    return "provider_error", "Không kiểm tra được API. Xem server log theo provider và mã HTTP; khóa không được ghi vào log."


@app.get("/provider-config")
def get_provider_config():
    return {"providers": PROVIDERS, **provider_settings.status()}


@app.post("/provider-config/test")
def test_provider_config(req: ProviderTestReq):
    if req.provider not in PROVIDERS:
        raise HTTPException(status_code=400, detail="Provider không được hỗ trợ.")
    model_name = req.model.strip()
    if not model_name:
        return {"ok": False, "code": "missing_model", "message": "Hãy nhập Model ID trước khi kiểm tra."}
    api_key = validated_api_key(req.api_key).strip()
    if not api_key:
        try:
            api_key = provider_settings.get_secret(req.provider) or ""
        except SecureStoreUnavailable as error:
            return {"ok": False, "code": "secure_store_unavailable", "message": str(error)}
    if not api_key:
        return {"ok": False, "code": "missing_key", "message": "Chưa có API key. Nhập key rồi thử lại."}
    try:
        # An explicit button click sends one minimal sample and may consume provider quota.
        provider = create_provider(req.provider, api_key, model_name)
        provider.translate_batch(["Hello, how are you?"], "vi")
        return {"ok": True, "provider": req.provider, "model": model_name, "message": "Kết nối API thành công; khóa chưa được lưu bởi thao tác kiểm tra."}
    except Exception as error:
        code, message = provider_error_message(error)
        log.warning("provider_test_failed provider=%s code=%s", req.provider, code)
        return {"ok": False, "provider": req.provider, "model": model_name, "code": code, "message": message}


@app.post("/provider-config")
def save_provider_config(req: ProviderConfigReq):
    global translator
    api_key = validated_api_key(req.api_key)
    try:
        provider_settings.save(req.provider, req.model, api_key)
        active = provider_settings.active_config()
        if not active:
            raise ProviderConfigurationError("Cấu hình provider chưa được lưu.")
        translator = FallbackTranslator([(active[0], create_provider(*active))])
    except SecureStoreUnavailable as error:
        log.warning("provider_config_save_failed provider=%s reason=secure_store_unavailable", req.provider)
        raise HTTPException(status_code=503, detail=str(error)) from None
    except (ProviderConfigurationError, ValueError) as error:
        raise HTTPException(status_code=400, detail=str(error)) from None
    except Exception as error:
        log.error("provider_config_save_failed provider=%s error_type=%s", req.provider, type(error).__name__)
        raise HTTPException(status_code=500, detail="Không lưu được cấu hình provider; API key không được ghi vào file.") from None
    return {
        "ok": True,
        "provider": active[0],
        "model": active[1],
        "has_api_key": True,
        "message": "Đã lưu cấu hình; API key nằm trong kho bí mật của hệ điều hành.",
    }


@app.post("/provider-config/clear")
def clear_provider_config():
    global translator
    try:
        removed = provider_settings.clear()
        translator = build_translator()
    except SecureStoreUnavailable as error:
        raise HTTPException(status_code=503, detail=str(error)) from None
    return {"ok": True, "removed_provider": removed or "", "message": "Đã xóa cấu hình và khóa khỏi kho bí mật."}


@app.get("/cache/stats")
async def cache_stats():
    return {"runtime": await audio_cache.stats(), "local_storage": local_cache.stats()}


@app.post("/translate")
def translate(req: TranslateReq):
    text_id = text_fingerprint(req.text)
    try:
        out, cache_status = translate_with_cache(req.text, req.target)
    except RateLimitedError as error:
        raise rate_limit_http_error(error, text_id)
    except Exception as error:
        log.error("request_failed stage=translate text_id=%s chars=%d target=%s error=%s", text_id, len(req.text), req.target, safe_error_detail(error))
        raise HTTPException(status_code=502, detail=f"translate_failed:{type(error).__name__}:id={text_id}")
    return {"text": out, "translation_cache": cache_status}


@app.post("/translate-batch")
def translate_batch(req: TranslateBatchReq):
    batch_id = text_fingerprint("\n".join(req.texts))
    try:
        out = translate_batch_with_cache(req.texts, req.target)
    except RateLimitedError as error:
        raise rate_limit_http_error(error, batch_id)
    except Exception as error:
        log.error("request_failed stage=translate_batch items=%d error=%s", len(req.texts), safe_error_detail(error))
        raise HTTPException(status_code=502, detail=f"translate_failed:{type(error).__name__}:id={batch_id}")
    return {"items": [{"text": t, "translation_cache": st} for t, st in out]}


@app.post("/speak")
async def speak(req: TranslateReq):
    text_id = text_fingerprint(req.text)
    try:
        vi, translation_status = await asyncio.to_thread(translate_with_cache, req.text, req.target)
    except RateLimitedError as error:
        raise rate_limit_http_error(error, text_id)
    except Exception as error:
        log.error("request_failed stage=translate text_id=%s chars=%d target=%s error=%s", text_id, len(req.text), req.target, safe_error_detail(error))
        raise HTTPException(status_code=502, detail=f"translate_failed:{type(error).__name__}:id={text_id}")

    try:
        audio, audio_status = await audio_cache.get_or_create(vi, VOICE, synth)
    except Exception as error:
        log.error("request_failed stage=tts text_id=%s chars=%d voice=%s error=%s", text_id, len(vi), VOICE, safe_error_detail(error))
        raise HTTPException(status_code=502, detail=f"tts_failed:{type(error).__name__}:id={text_id}")
    if not audio:
        log.error("request_failed stage=tts text_id=%s chars=%d voice=%s error=NoAudioReceived", text_id, len(vi), VOICE)
        raise HTTPException(status_code=502, detail=f"tts_empty:NoAudioReceived:id={text_id}")
    if audio_status != "miss":
        log.info("tts_cache_reuse status=%s text_id=%s voice=%s audio_bytes=%d", audio_status, text_fingerprint(vi), VOICE, len(audio))
    return {
        "text": vi,
        "audio": base64.b64encode(audio).decode(),
        "translation_cache": translation_status,
        "audio_cache": audio_status,
    }
