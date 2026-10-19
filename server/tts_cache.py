"""Bounded memory + SQLite cache with single-flight audio synthesis."""

import asyncio
import logging
import time
import unicodedata
from collections import OrderedDict
from typing import Awaitable, Callable, Protocol

SynthFn = Callable[[str], Awaitable[bytes]]
log = logging.getLogger("uvicorn.error")


class PersistentAudioStore(Protocol):
    def get_audio(self, text: str, voice: str) -> bytes | None: ...
    def set_audio(self, text: str, voice: str, payload: bytes) -> None: ...


class TTSCache:
    def __init__(
        self,
        max_entries: int = 256,
        max_bytes: int = 32 * 1024 * 1024,
        persistent_store: PersistentAudioStore | None = None,
    ) -> None:
        self.max_entries = max(0, int(max_entries))
        self.max_bytes = max(0, int(max_bytes))
        self.persistent_store = persistent_store
        self._items: OrderedDict[str, tuple[str, str, bytes]] = OrderedDict()
        self._inflight: dict[str, asyncio.Task[tuple[bytes, str]]] = {}
        self._bytes = 0
        self._hits = 0
        self._disk_hits = 0
        self._misses = 0
        self._coalesced = 0
        self._lock = asyncio.Lock()

    @staticmethod
    def _normalize(text: str) -> str:
        return " ".join(unicodedata.normalize("NFKC", text).split())

    @classmethod
    def _key(cls, text: str, voice: str) -> str:
        return f"{voice}\0{cls._normalize(text)}"

    def _remember_locked(self, key: str, text: str, voice: str, audio: bytes) -> None:
        if not audio or not self.max_entries or not self.max_bytes or len(audio) > self.max_bytes:
            return
        old = self._items.pop(key, None)
        if old:
            self._bytes -= len(old[2])
        self._items[key] = (text, voice, audio)
        self._bytes += len(audio)
        while self._items and (len(self._items) > self.max_entries or self._bytes > self.max_bytes):
            _old_key, (_old_text, _old_voice, old_audio) = self._items.popitem(last=False)
            self._bytes -= len(old_audio)

    async def _create_and_store(
        self, key: str, text: str, voice: str, synth: SynthFn
    ) -> tuple[bytes, str]:
        try:
            if self.persistent_store is not None:
                try:
                    disk_audio = await asyncio.to_thread(self.persistent_store.get_audio, text, voice)
                except Exception as error:
                    log.warning("audio_cache_read_failed error=%s", type(error).__name__)
                    disk_audio = None
                if disk_audio:
                    async with self._lock:
                        self._remember_locked(key, text, voice, disk_audio)
                        self._disk_hits += 1
                    return disk_audio, "disk_hit"

            audio = await synth(text)
            if not audio:
                return b"", "miss"
            if self.persistent_store is not None:
                try:
                    await asyncio.to_thread(self.persistent_store.set_audio, text, voice, audio)
                except Exception as error:
                    log.warning("audio_cache_write_failed error=%s", type(error).__name__)
            async with self._lock:
                self._remember_locked(key, text, voice, audio)
            return audio, "miss"
        finally:
            async with self._lock:
                task = asyncio.current_task()
                if self._inflight.get(key) is task:
                    self._inflight.pop(key, None)

    async def get_or_create(self, text: str, voice: str, synth: SynthFn) -> tuple[bytes, str]:
        key = self._key(text, voice)
        async with self._lock:
            cached = self._items.get(key)
            if cached:
                self._items.move_to_end(key)
                self._hits += 1
                return cached[2], "memory_hit"
            task = self._inflight.get(key)
            if task is None:
                self._misses += 1
                task = asyncio.create_task(self._create_and_store(key, text, voice, synth))
                self._inflight[key] = task
                leader = True
            else:
                self._coalesced += 1
                leader = False

        # Client cancellation must not cancel audio work shared with other requests.
        audio, status = await asyncio.shield(task)
        return audio, status if leader else "coalesced"

    async def stats(self) -> dict[str, int]:
        async with self._lock:
            return {
                "memory_entries": len(self._items),
                "memory_bytes": self._bytes,
                "memory_max_entries": self.max_entries,
                "memory_max_bytes": self.max_bytes,
                "memory_hits": self._hits,
                "disk_hits": self._disk_hits,
                "misses": self._misses,
                "coalesced": self._coalesced,
                "inflight": len(self._inflight),
            }
