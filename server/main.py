import asyncio
import base64
import hashlib
import logging
import re
from pathlib import Path
from typing import Any

import edge_tts
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, Field

from api_providers import PROVIDERS, create_provider
import google_auth as gauth
import notion_export
from local_cache import LocalCache
from provider_settings import ProviderConfigurationError, ProviderSettings, SecureStoreUnavailable
from summarizer import summarize_lesson
from translator import (
    FallbackTranslator,
    GenerationNotSupported,
    ProviderHTTPError,
    RateLimitedError,
    build_translator,
)
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
async def local_only_guard(request: Request, call_next):
    """Chỉ nhận request từ extension: chặn trang web bất kỳ gọi POST tới server cục bộ
    (CSRF, ví dụ /provider-config/clear) và Host lạ (DNS rebinding)."""
    host = request.headers.get("host", "").rsplit(":", 1)[0].strip("[]")
    origin = request.headers.get("origin")
    # Google OAuth callback comes from the browser directly (no origin header) – allow GET only
    if request.method == "GET" and request.url.path.startswith("/google-auth/callback"):
        return await call_next(request)
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


@app.get("/version")
def version():
    """Chẩn đoán: server đang chạy có đủ route OAuth không (tránh nhầm với bản cũ)."""
    paths = sorted({getattr(route, "path", "") for route in app.routes})
    oauth_paths = [path for path in paths if path.startswith("/google-auth")]
    return {
        "app": "viet-dub",
        "oauth_routes": oauth_paths,
        "oauth_complete": "/google-auth/url" in oauth_paths
        and "/google-auth/callback" in oauth_paths,
        "client_configured": gauth.is_client_configured(),
        "redirect_uri": gauth.REDIRECT_URI if hasattr(gauth, "REDIRECT_URI") else "",
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


# ══════════════════════════════════════════════════════════════════════
#  Google OAuth2 endpoints
# ══════════════════════════════════════════════════════════════════════

class GoogleClientConfigReq(BaseModel):
    client_id: str = Field(min_length=1, max_length=512)
    client_secret: str = Field(min_length=0, max_length=512)


@app.post("/google-auth/client-config")
def save_google_client_config(req: GoogleClientConfigReq):
    try:
        gauth.save_client_config(req.client_id, req.client_secret)
    except Exception as error:
        raise HTTPException(status_code=500, detail=str(error)) from None
    return {"ok": True, "message": "Đã lưu OAuth client ID vào kho bí mật."}


# Lỗi OAuth gần nhất — để popup hiển thị lý do thất bại mà không cần đọc log.
_last_oauth_error: str = ""


@app.get("/google-auth/status")
def google_auth_status():
    status = gauth.get_auth_status()
    # Popup dùng cờ này để biết đã sẵn sàng cho nút "Đăng nhập Google" hay chưa.
    status["client_configured"] = gauth.is_client_configured()
    status["last_error"] = _last_oauth_error
    return status


@app.get("/google-auth/url")
def google_auth_url():
    global _last_oauth_error
    try:
        url, _state = gauth.build_auth_url()
    except RuntimeError as error:
        raise HTTPException(status_code=400, detail=str(error)) from None
    # Bắt đầu lượt đăng nhập mới thì xoá lỗi cũ cho khỏi báo nhầm.
    _last_oauth_error = ""
    return {"url": url}


@app.get("/google-auth/callback")
async def google_auth_callback(
    code: str = "",
    state: str = "",
    error: str = "",
    error_description: str = "",
):
    """Browser redirect target after Google OAuth consent screen."""
    global _last_oauth_error
    from fastapi.responses import HTMLResponse
    if error:
        _last_oauth_error = f"{error}: {error_description}".strip(": ")
        log.warning("google_auth_denied error=%s desc=%s", error, (error_description or "")[:200])
        html = f"""<!doctype html><html><head><meta charset="utf-8"/>
<title>Viet Dub – Đăng nhập thất bại</title>
<style>body{{font-family:system-ui;display:flex;flex-direction:column;align-items:center;justify-content:center;min-height:100vh;margin:0;background:#f1f3f4;}}
.card{{background:#fff;border-radius:12px;padding:32px;box-shadow:0 2px 12px rgba(0,0,0,.1);max-width:520px;}}
h2{{color:#b3261e;margin:0 0 12px;}} p{{color:#444;line-height:1.5;}} code{{background:#f1f3f4;padding:2px 5px;border-radius:4px;}}
small{{color:#888;}}</style></head><body>
<div class="card">
<h2>&#10060; Google không cấp quyền</h2>
<p><strong>Mã lỗi:</strong> <code>{error}</code></p>
<p>{error_description or "Không có mô tả thêm."}</p>
<p>Viet Dub cần quyền <em>Google Docs</em> + <em>Drive</em> để lưu bản tóm tắt.<br>
Đóng tab này, kiểm tra lại <em>OAuth consent screen</em> (Test users) rồi bấm
<strong>Tiếp tục với Google</strong> lần nữa.</p>
</div></body></html>"""
        return HTMLResponse(html, status_code=400)
    if not code:
        _last_oauth_error = "Thiếu mã xác thực từ Google (code rỗng)."
        log.warning("google_auth_callback_without_code state=%s", (state or "")[:40])
        html = """<!doctype html><html><head><meta charset="utf-8"/>
<title>Viet Dub – Thiếu mã</title></head><body>
<h2>&#10060; Thiếu mã xác thực</h2>
<p>Địa chỉ này chỉ dùng được sau khi Google chuyển hướng về từ trang đăng nhập.</p>
<p>Đóng tab này, mở popup Viet Dub và bấm <strong>Tiếp tục với Google</strong>.</p>
</body></html>"""
        return HTMLResponse(html, status_code=400)
    try:
        token_data = await asyncio.to_thread(gauth.exchange_code, code, state)
    except Exception as exc:
        _last_oauth_error = str(exc)[:300]
        log.error("google_auth_callback_failed error=%s", str(exc)[:200])
        html = f"""<!doctype html><html><head><meta charset="utf-8"/>
<title>Viet Dub – Lỗi</title></head><body>
<h2>&#10060; Kết nối thất bại</h2><p>{exc}</p>
<p>Đóng tab này và thử lại.</p></body></html>"""
        return HTMLResponse(html, status_code=400)
    email = token_data.get("email", "")
    _last_oauth_error = ""
    html = f"""<!doctype html><html><head><meta charset="utf-8"/>
<title>Viet Dub – Đã kết nối</title>
<style>body{{font-family:system-ui;display:flex;flex-direction:column;align-items:center;justify-content:center;min-height:100vh;margin:0;background:#f1f3f4;}}
.card{{background:#fff;border-radius:12px;padding:32px;box-shadow:0 2px 12px rgba(0,0,0,.1);text-align:center;max-width:360px;}}
h2{{color:#137333;margin:0 0 12px;}} p{{color:#444;}} small{{color:#888;}}</style>
</head><body>
<div class="card">
<h2>&#10003; Đã kết nối Google</h2>
<p>Tài khoản: <strong>{email}</strong></p>
<p>Bây giờ bạn có thể đóng tab này.<br>Viet Dub sẵn sàng xuất ghi chú bài học lên Google Docs.</p>
<small>Tab này sẽ tự đóng sau 5 giây...</small>
</div>
<script>setTimeout(()=>window.close(),5000);</script>
</body></html>"""
    return HTMLResponse(html)


@app.post("/google-auth/disconnect")
def google_auth_disconnect():
    gauth.clear_tokens()
    return {"ok": True, "message": "Đã ngắt kết nối Google."}


# ══════════════════════════════════════════════════════════════════════
#  Notion endpoints
# ══════════════════════════════════════════════════════════════════════

class NotionConfigReq(BaseModel):
    token: str = Field(min_length=1, max_length=512)
    parent_page_id: str = Field(min_length=1, max_length=64)


@app.post("/notion/config")
def save_notion_config(req: NotionConfigReq):
    try:
        notion_export.save_config(req.token, req.parent_page_id)
    except Exception as error:
        raise HTTPException(status_code=400, detail=str(error)) from None
    return {"ok": True, "message": "Đã lưu Notion token và parent page ID vào kho bí mật."}


@app.get("/notion/status")
def notion_status():
    return notion_export.get_status()


@app.post("/notion/test")
def notion_test(req: NotionConfigReq):
    """Kiểm tra token và parent page trước khi lưu."""
    try:
        result = notion_export.test_connection(req.token, req.parent_page_id)
    except Exception as error:
        raise HTTPException(status_code=400, detail=str(error)[:300]) from None
    return {
        "ok": True,
        "message": f"Kết nối thành công với Notion ({result.get('bot_name', 'integration')}).",
    }


@app.post("/notion/disconnect")
def notion_disconnect():
    notion_export.clear_config()
    return {"ok": True, "message": "Đã xoá Notion token và parent page ID."}


# ══════════════════════════════════════════════════════════════════════
#  Lesson summarize + export (Google Docs / Notion)
# ══════════════════════════════════════════════════════════════════════

class SummarizeReq(BaseModel):
    title: str = Field(min_length=1, max_length=500)
    vi_cues: list[str] = Field(min_length=1, max_length=5000)
    export_to: str = "none"        # none | gdocs | notion
    target_id: str = ""            # gdocs: doc id để append | notion: page id để append
    export_to_docs: bool = False   # tương thích ngược: true => export_to = gdocs


class SummarizeResp(BaseModel):
    ok: bool
    summary: str = ""
    target: str = "none"
    target_id: str = ""
    target_url: str = ""
    message: str = ""


def _supports_generation(active: Any) -> bool:
    """True nếu provider đang chạy có thể sinh văn bản tự do (cần API key AI)."""
    providers = getattr(active, "_providers", None)
    if providers is not None:
        return any(hasattr(provider, "generate_text") for _name, provider in providers)
    return hasattr(active, "generate_text")


def _summary_cache_key(title: str, vi_cues: list[str]) -> str:
    """Cache theo tiêu đề + nội dung phụ đề để hai video trùng tên không dùng chung kết quả."""
    digest = hashlib.sha256()
    digest.update(title.encode("utf-8", errors="replace"))
    digest.update(b"\0")
    for cue in vi_cues:
        digest.update(cue.encode("utf-8", errors="replace"))
        digest.update(b"\x1e")
    return f"{title}\0{digest.hexdigest()[:16]}"


@app.post("/summarize", response_model=SummarizeResp)
async def summarize_endpoint(req: SummarizeReq):
    title_id = text_fingerprint(req.title)
    export_to = (req.export_to or "none").strip().lower()
    if req.export_to_docs and export_to == "none":
        export_to = "gdocs"
    if export_to not in {"none", "gdocs", "notion"}:
        raise HTTPException(status_code=400, detail="export_to phải là none, gdocs hoặc notion.")

    # Tóm tắt chỉ dùng sau khi đã có nơi lưu nội dung: đăng nhập Google hoặc
    # cấu hình Notion. Chặn ở server để không thể gọi vòng qua giao diện.
    if not (
        gauth.get_auth_status().get("connected")
        or notion_export.get_status().get("configured")
    ):
        raise HTTPException(
            status_code=403,
            detail="Cần đăng nhập Google (hoặc cấu hình Notion) để dùng tính năng tóm tắt.",
        )

    # 1. Tóm tắt (có cache theo tiêu đề + nội dung)
    cache_key = _summary_cache_key(req.title, req.vi_cues)
    summary = local_cache.get_summary(cache_key) or ""
    if not summary:
        if isinstance(translator, UnavailableTranslator):
            raise HTTPException(status_code=400, detail=translator.message)
        if not _supports_generation(translator):
            raise HTTPException(
                status_code=400,
                detail="Chưa cấu hình provider AI (Gemini/OpenAI/Claude/Ollama) để tóm tắt. Hãy lưu API key trong popup.",
            )
        try:
            summary = await asyncio.to_thread(summarize_lesson, req.title, req.vi_cues, translator)
        except GenerationNotSupported as error:
            raise HTTPException(status_code=400, detail=str(error)) from None
        except Exception as error:
            log.error("summarize_failed title_id=%s error=%s", title_id, safe_error_detail(error))
            raise HTTPException(status_code=502, detail=f"summarize_failed:{type(error).__name__}") from None
        local_cache.set_summary(cache_key, req.title, summary)

    # 2. Xuất ra Google Docs / Notion
    target_id = ""
    target_url = ""
    if export_to == "gdocs":
        status = gauth.get_auth_status()
        if not status.get("connected"):
            raise HTTPException(status_code=401, detail="Chưa đăng nhập Google. Vào popup → tab Google để kết nối.")
        try:
            if req.target_id:
                target_url = await asyncio.to_thread(gauth.append_to_doc, req.target_id, req.title, summary)
                target_id = req.target_id
            else:
                target_url = await asyncio.to_thread(gauth.create_or_get_lesson_doc, req.title, summary)
                match = re.search(r"/d/([a-zA-Z0-9_-]+)/", target_url)
                target_id = match.group(1) if match else ""
        except Exception as error:
            log.error("gdocs_export_failed title_id=%s error=%s", title_id, safe_error_detail(error))
            raise HTTPException(status_code=502, detail=f"gdocs_export_failed:{str(error)[:200]}") from None
    elif export_to == "notion":
        status = notion_export.get_status()
        if not status.get("configured"):
            raise HTTPException(status_code=401, detail="Chưa cấu hình Notion. Vào popup → tab Notion để thiết lập.")
        try:
            if req.target_id:
                result = await asyncio.to_thread(notion_export.append_to_page, req.target_id, req.title, summary)
            else:
                result = await asyncio.to_thread(notion_export.create_child_page, req.title, summary, "")
            target_id = result.get("page_id", "")
            target_url = result.get("page_url", "")
        except Exception as error:
            log.error("notion_export_failed title_id=%s error=%s", title_id, safe_error_detail(error))
            raise HTTPException(status_code=502, detail=f"notion_export_failed:{str(error)[:200]}") from None

    suffix = ""
    if target_url:
        suffix = " Đã lưu vào Google Docs." if export_to == "gdocs" else " Đã lưu vào Notion."
    return SummarizeResp(
        ok=True,
        summary=summary,
        target=export_to,
        target_id=target_id,
        target_url=target_url,
        message="Đã tóm tắt bài học." + suffix,
    )

