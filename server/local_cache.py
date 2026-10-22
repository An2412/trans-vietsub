"""Persistent, bounded SQLite cache for translations and synthesized audio."""

from __future__ import annotations

import hashlib
import sqlite3
import threading
import time
import unicodedata
from pathlib import Path


def _normalize(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).split())


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()


class LocalCache:
    def __init__(
        self,
        path: str | Path = "data/vietdub.sqlite3",
        *,
        ttl_seconds: int = 30 * 24 * 60 * 60,
        max_audio_bytes: int = 256 * 1024 * 1024,
        max_audio_entries: int = 5000,
        max_translation_entries: int = 20000,
    ) -> None:
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.ttl_seconds = max(60, int(ttl_seconds))
        self.max_audio_bytes = max(0, int(max_audio_bytes))
        self.max_audio_entries = max(0, int(max_audio_entries))
        self.max_translation_entries = max(0, int(max_translation_entries))
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, timeout=15, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA busy_timeout=15000")
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS translations (
                cache_key TEXT PRIMARY KEY,
                translated TEXT NOT NULL,
                created_at REAL NOT NULL,
                accessed_at REAL NOT NULL,
                expires_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS audio (
                cache_key TEXT PRIMARY KEY,
                payload BLOB NOT NULL,
                created_at REAL NOT NULL,
                accessed_at REAL NOT NULL,
                expires_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS app_settings (
                setting_key TEXT PRIMARY KEY,
                setting_value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS summaries (
                cache_key TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                summary TEXT NOT NULL,
                created_at REAL NOT NULL,
                accessed_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_translations_expiry ON translations(expires_at);
            CREATE INDEX IF NOT EXISTS idx_translations_access ON translations(accessed_at);
            CREATE INDEX IF NOT EXISTS idx_audio_expiry ON audio(expires_at);
            CREATE INDEX IF NOT EXISTS idx_audio_access ON audio(accessed_at);
            CREATE INDEX IF NOT EXISTS idx_summaries_access ON summaries(accessed_at);
            """
        )
        self._conn.commit()

    def get_setting(self, key: str) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT setting_value FROM app_settings WHERE setting_key=?", (key,)
            ).fetchone()
            return str(row[0]) if row else None

    def set_setting(self, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO app_settings(setting_key, setting_value) VALUES(?,?) "
                "ON CONFLICT(setting_key) DO UPDATE SET setting_value=excluded.setting_value",
                (key, value),
            )
            self._conn.commit()

    def delete_setting(self, key: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM app_settings WHERE setting_key=?", (key,))
            self._conn.commit()

    @staticmethod
    def _translation_key(text: str, target: str, namespace: str = "default") -> str:
        return _digest(f"translation\0{namespace}\0{target.casefold()}\0{_normalize(text)}")

    @staticmethod
    def _audio_key(text: str, voice: str) -> str:
        return _digest(f"audio\0{voice}\0{_normalize(text)}")

    @staticmethod
    def _summary_key(key_text: str, namespace: str = "default") -> str:
        return _digest(f"summary\0{namespace}\0{_normalize(key_text)}")

    def get_summary(self, key_text: str, namespace: str = "default") -> str | None:
        key = self._summary_key(key_text, namespace)
        now = time.time()
        with self._lock:
            row = self._conn.execute(
                "SELECT summary FROM summaries WHERE cache_key=?", (key,)
            ).fetchone()
            if not row:
                return None
            self._conn.execute("UPDATE summaries SET accessed_at=? WHERE cache_key=?", (now, key))
            self._conn.commit()
            return str(row[0])

    def set_summary(self, key_text: str, title: str, summary: str, namespace: str = "default") -> None:
        if not summary:
            return
        key = self._summary_key(key_text, namespace)
        now = time.time()
        with self._lock:
            self._conn.execute(
                "INSERT INTO summaries(cache_key, title, summary, created_at, accessed_at) "
                "VALUES(?,?,?,?,?) ON CONFLICT(cache_key) DO UPDATE SET "
                "title=excluded.title, summary=excluded.summary, accessed_at=excluded.accessed_at",
                (key, title, summary, now, now),
            )
            self._conn.commit()

    def get_translation(self, source: str, target: str, namespace: str = "default") -> str | None:
        key = self._translation_key(source, target, namespace)
        now = time.time()
        with self._lock:
            row = self._conn.execute(
                "SELECT translated, expires_at FROM translations WHERE cache_key=?", (key,)
            ).fetchone()
            if not row:
                return None
            translated, expires_at = row
            if expires_at <= now:
                self._conn.execute("DELETE FROM translations WHERE cache_key=?", (key,))
                self._conn.commit()
                return None
            self._conn.execute(
                "UPDATE translations SET accessed_at=? WHERE cache_key=?", (now, key)
            )
            self._conn.commit()
            return str(translated)

    def set_translation(self, source: str, target: str, translated: str, namespace: str = "default") -> None:
        if not translated or self.max_translation_entries == 0:
            return
        key = self._translation_key(source, target, namespace)
        now = time.time()
        with self._lock:
            self._conn.execute(
                "INSERT INTO translations(cache_key, translated, created_at, accessed_at, expires_at) "
                "VALUES(?,?,?,?,?) ON CONFLICT(cache_key) DO UPDATE SET "
                "translated=excluded.translated, created_at=excluded.created_at, "
                "accessed_at=excluded.accessed_at, expires_at=excluded.expires_at",
                (key, translated, now, now, now + self.ttl_seconds),
            )
            self._conn.execute("DELETE FROM translations WHERE expires_at<=?", (now,))
            self._conn.execute(
                "DELETE FROM translations WHERE cache_key IN (SELECT cache_key FROM translations "
                "ORDER BY accessed_at DESC LIMIT -1 OFFSET ?)",
                (self.max_translation_entries,),
            )
            self._conn.commit()

    def get_audio(self, text: str, voice: str) -> bytes | None:
        key = self._audio_key(text, voice)
        now = time.time()
        with self._lock:
            row = self._conn.execute(
                "SELECT payload, expires_at FROM audio WHERE cache_key=?", (key,)
            ).fetchone()
            if not row:
                return None
            payload, expires_at = row
            if expires_at <= now:
                self._conn.execute("DELETE FROM audio WHERE cache_key=?", (key,))
                self._conn.commit()
                return None
            self._conn.execute("UPDATE audio SET accessed_at=? WHERE cache_key=?", (now, key))
            self._conn.commit()
            return bytes(payload)

    def set_audio(self, text: str, voice: str, payload: bytes) -> None:
        if not payload or self.max_audio_bytes == 0 or self.max_audio_entries == 0:
            return
        if len(payload) > self.max_audio_bytes:
            return
        key = self._audio_key(text, voice)
        now = time.time()
        with self._lock:
            self._conn.execute(
                "INSERT INTO audio(cache_key, payload, created_at, accessed_at, expires_at) "
                "VALUES(?,?,?,?,?) ON CONFLICT(cache_key) DO UPDATE SET "
                "payload=excluded.payload, created_at=excluded.created_at, "
                "accessed_at=excluded.accessed_at, expires_at=excluded.expires_at",
                (key, sqlite3.Binary(payload), now, now, now + self.ttl_seconds),
            )
            self._conn.execute("DELETE FROM audio WHERE expires_at<=?", (now,))
            while True:
                count, total_bytes = self._conn.execute(
                    "SELECT COUNT(*), COALESCE(SUM(LENGTH(payload)),0) FROM audio"
                ).fetchone()
                if count <= self.max_audio_entries and total_bytes <= self.max_audio_bytes:
                    break
                self._conn.execute(
                    "DELETE FROM audio WHERE cache_key=(SELECT cache_key FROM audio "
                    "ORDER BY accessed_at ASC LIMIT 1)"
                )
            self._conn.commit()

    def stats(self) -> dict[str, int | str]:
        now = time.time()
        with self._lock:
            self._conn.execute("DELETE FROM audio WHERE expires_at<=?", (now,))
            self._conn.execute("DELETE FROM translations WHERE expires_at<=?", (now,))
            audio_entries, audio_bytes = self._conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(LENGTH(payload)),0) FROM audio"
            ).fetchone()
            translation_entries = self._conn.execute("SELECT COUNT(*) FROM translations").fetchone()[0]
            self._conn.commit()
        return {
            "path": str(self.path),
            "translation_entries": int(translation_entries),
            "audio_entries": int(audio_entries),
            "audio_bytes": int(audio_bytes),
            "ttl_seconds": self.ttl_seconds,
            "max_audio_bytes": self.max_audio_bytes,
            "max_audio_entries": self.max_audio_entries,
            "max_translation_entries": self.max_translation_entries,
        }

    def close(self) -> None:
        with self._lock:
            self._conn.close()
