import asyncio
import tempfile
import unittest
from pathlib import Path

from local_cache import LocalCache
from tts_cache import TTSCache


class TTSCacheTests(unittest.IsolatedAsyncioTestCase):
    async def test_normalized_duplicate_is_memory_hit(self):
        cache = TTSCache()
        calls = 0

        async def synth(text):
            nonlocal calls
            calls += 1
            return b"audio-data"

        first, first_status = await cache.get_or_create("Xin chào bạn!", "vi-VN-A", synth)
        second, second_status = await cache.get_or_create("  Xin   chào bạn!  ", "vi-VN-A", synth)

        self.assertEqual(first, b"audio-data")
        self.assertEqual(second, first)
        self.assertEqual(first_status, "miss")
        self.assertEqual(second_status, "memory_hit")
        self.assertEqual(calls, 1)

    async def test_concurrent_duplicate_is_single_flight(self):
        cache = TTSCache()
        calls = 0

        async def synth(_text):
            nonlocal calls
            calls += 1
            await asyncio.sleep(0.03)
            return b"one-audio"

        results = await asyncio.gather(
            cache.get_or_create("same phrase", "voice", synth),
            cache.get_or_create("same phrase", "voice", synth),
        )

        self.assertEqual(calls, 1)
        self.assertEqual({status for _audio, status in results}, {"miss", "coalesced"})
        self.assertEqual(results[0][0], results[1][0])

    async def test_voice_is_part_of_cache_key(self):
        cache = TTSCache()
        calls = 0

        async def synth(_text):
            nonlocal calls
            calls += 1
            return f"audio-{calls}".encode()

        await cache.get_or_create("hello", "voice-a", synth)
        await cache.get_or_create("hello", "voice-b", synth)
        self.assertEqual(calls, 2)

    async def test_oversized_audio_is_returned_but_not_cached_in_memory(self):
        cache = TTSCache(max_bytes=2)

        async def synth(_text):
            return b"123"

        audio, status = await cache.get_or_create("phrase", "voice", synth)
        stats = await cache.stats()
        self.assertEqual(audio, b"123")
        self.assertEqual(status, "miss")
        self.assertEqual(stats["memory_entries"], 0)
        self.assertEqual(stats["memory_bytes"], 0)

    async def test_lru_entry_limit_evicts_oldest_clip(self):
        cache = TTSCache(max_entries=1)
        calls = 0

        async def synth(text):
            nonlocal calls
            calls += 1
            return text.encode()

        await cache.get_or_create("first", "voice", synth)
        await cache.get_or_create("second", "voice", synth)
        await cache.get_or_create("first", "voice", synth)

        self.assertEqual(calls, 3)
        self.assertEqual((await cache.stats())["memory_entries"], 1)

    async def test_persistent_audio_hit_skips_synthesis(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cache.sqlite3"
            store = LocalCache(path)
            store.set_audio("Xin chào", "voice", b"persistent-audio")
            cache = TTSCache(persistent_store=store)
            calls = 0

            async def synth(_text):
                nonlocal calls
                calls += 1
                return b"unexpected"

            audio, status = await cache.get_or_create("Xin chào", "voice", synth)
            self.assertEqual(audio, b"persistent-audio")
            self.assertEqual(status, "disk_hit")
            self.assertEqual(calls, 0)
            store.close()


if __name__ == "__main__":
    unittest.main()
