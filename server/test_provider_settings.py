import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from local_cache import LocalCache
from provider_settings import ProviderSettings


class FakeKeyring:
    def __init__(self):
        self.values = {}
    def get_password(self, service, username):
        return self.values.get((service, username))
    def set_password(self, service, username, password):
        self.values[(service, username)] = password
    def delete_password(self, service, username):
        if (service, username) not in self.values:
            raise RuntimeError("Password not found")
        del self.values[(service, username)]


class ProviderSettingsTests(unittest.TestCase):
    def test_secret_is_not_written_to_sqlite_and_is_not_returned(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "settings.sqlite3"
            cache = LocalCache(db_path)
            settings = ProviderSettings(cache)
            keyring = FakeKeyring()
            with patch("provider_settings._secure_keyring", return_value=keyring):
                settings.save("gemini", "gemini-test", "SUPER-SECRET-KEY")
                status = settings.status()
                active = settings.active_config()
            self.assertTrue(status["has_api_key"])
            self.assertNotIn("SUPER-SECRET-KEY", repr(status))
            self.assertEqual(active, ("gemini", "gemini-test", "SUPER-SECRET-KEY"))
            cache.close()
            with sqlite3.connect(db_path) as conn:
                db_dump = repr(conn.execute("SELECT * FROM app_settings").fetchall())
            self.assertNotIn("SUPER-SECRET-KEY", db_dump)

    def test_empty_key_cannot_overwrite_without_existing_credential(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = LocalCache(Path(tmp) / "settings.sqlite3")
            settings = ProviderSettings(cache)
            with patch("provider_settings._secure_keyring", return_value=FakeKeyring()):
                with self.assertRaisesRegex(RuntimeError, "API key"):
                    settings.save("openai", "gpt-test", "")
            cache.close()

    def test_clear_removes_secret_and_nonsecret_selection(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = LocalCache(Path(tmp) / "settings.sqlite3")
            settings = ProviderSettings(cache)
            keyring = FakeKeyring()
            with patch("provider_settings._secure_keyring", return_value=keyring):
                settings.save("anthropic", "claude-test", "test-secret")
                settings.save("gemini", "gemini-test", "older-secret")
                settings.clear()
                self.assertIsNone(settings.active_config())
                self.assertFalse(keyring.values)
            cache.close()


if __name__ == "__main__":
    unittest.main()
