"""Store provider metadata in SQLite and API secrets in the OS credential store only."""

from __future__ import annotations

import sys

from api_providers import PROVIDERS
from local_cache import LocalCache

SERVICE_NAME = "viet-dub-api"


class SecureStoreUnavailable(RuntimeError):
    pass


class ProviderConfigurationError(RuntimeError):
    pass


def _secure_keyring():
    try:
        import keyring
    except ImportError:
        raise SecureStoreUnavailable(
            "Thiếu thư viện keyring. Cài requirements và bật kho bí mật của hệ điều hành."
        ) from None

    try:
        backend = keyring.get_keyring()
        module = backend.__class__.__module__.lower()
        priority = float(getattr(backend, "priority", 0))
    except Exception:
        raise SecureStoreUnavailable(
            "Không truy cập được kho bí mật của hệ điều hành."
        ) from None

    if priority <= 0:
        raise SecureStoreUnavailable(
            "Không tìm thấy kho bí mật bảo mật; không lưu khóa thành file thường."
        )

    if sys.platform.startswith("linux"):
        acceptable = ("secretservice", "kwallet")
    elif sys.platform == "darwin":
        acceptable = ("macos", "keychain")
    elif sys.platform.startswith("win"):
        acceptable = ("win32", "windows")
    else:
        acceptable = ("secretservice", "kwallet", "macos", "keychain", "win32", "windows")

    if not any(name in module for name in acceptable):
        raise SecureStoreUnavailable(
            "Keyring hiện chọn backend không được hỗ trợ an toàn; không lưu API key."
        )
    return keyring


class ProviderSettings:
    def __init__(self, cache: LocalCache):
        self.cache = cache

    def _provider(self) -> str | None:
        value = self.cache.get_setting("provider")
        return value if value in PROVIDERS else None

    def _model(self, provider: str) -> str:
        return self.cache.get_setting(f"model:{provider}") or PROVIDERS[provider]["model"]

    @staticmethod
    def _secret_for(keyring, provider: str) -> str | None:
        try:
            return keyring.get_password(SERVICE_NAME, provider)
        except Exception:
            raise SecureStoreUnavailable(
                "Không đọc được khóa từ kho bí mật. Hãy kiểm tra Windows Credential Manager / Keychain / Secret Service (KWallet) rồi thử lại."
            ) from None

    def status(self) -> dict:
        provider = self._provider()
        model = self._model(provider) if provider else ""
        try:
            keyring = _secure_keyring()
            ready = True
            has_key = bool(self._secret_for(keyring, provider)) if provider else False
            store_message = "Khóa được lưu trong kho bí mật của hệ điều hành."
        except SecureStoreUnavailable as error:
            ready = False
            has_key = False
            store_message = str(error)
        return {
            "provider": provider or "",
            "model": model,
            "has_api_key": has_key,
            "secure_store_ready": ready,
            "store_message": store_message,
        }

    def get_secret(self, provider: str) -> str | None:
        if provider not in PROVIDERS:
            raise ValueError("Nhà cung cấp không được hỗ trợ")
        keyring = _secure_keyring()
        return self._secret_for(keyring, provider)

    def save(self, provider: str, model: str, api_key: str | None) -> None:
        if provider not in PROVIDERS:
            raise ValueError("Nhà cung cấp không được hỗ trợ")
        model = model.strip()
        if not model or len(model) > 128:
            raise ValueError("Tên model phải từ 1 đến 128 ký tự")

        keyring = _secure_keyring()
        existing = self._secret_for(keyring, provider)
        secret = (api_key or "").strip()
        if secret:
            try:
                keyring.set_password(SERVICE_NAME, provider, secret)
            except Exception:
                raise SecureStoreUnavailable(
                    "Không ghi được khóa vào kho bí mật. Hãy kiểm tra Windows Credential Manager / Keychain / Secret Service (KWallet) rồi thử lại."
                ) from None
        elif not existing:
            raise ProviderConfigurationError("Hãy nhập API key trước khi lưu cấu hình.")

        # Only non-secret metadata is stored in SQLite.
        self.cache.set_setting("provider", provider)
        self.cache.set_setting(f"model:{provider}", model)

    def clear(self) -> str | None:
        provider = self._provider()
        keyring = _secure_keyring()
        for provider_name in PROVIDERS:
            if self._secret_for(keyring, provider_name):
                try:
                    keyring.delete_password(SERVICE_NAME, provider_name)
                except Exception:
                    raise SecureStoreUnavailable(
                        "Không xóa được khóa khỏi kho bí mật; hãy kiểm tra trạng thái keyring."
                    ) from None
        self.cache.delete_setting("provider")
        if provider:
            self.cache.delete_setting(f"model:{provider}")
        return provider

    def selection(self) -> tuple[str, str] | None:
        """Non-secret provider/model identity used to namespace translation cache."""
        provider = self._provider()
        if not provider:
            return None
        return provider, self._model(provider)

    def active_config(self) -> tuple[str, str, str] | None:
        provider = self._provider()
        if not provider:
            return None
        model = self._model(provider)
        secret = self.get_secret(provider)
        if not secret:
            raise ProviderConfigurationError(
                "Provider đã chọn nhưng chưa tìm thấy API key trong kho bí mật. Nhập lại khóa trong popup."
            )
        return provider, model, secret
