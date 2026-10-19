import tempfile
import unittest
from pathlib import Path

from local_cache import LocalCache


class LocalCacheTests(unittest.TestCase):
    def test_translation_and_audio_survive_reopen(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "vietdub.sqlite3"
            first = LocalCache(path)
            first.set_translation("  Hello   world! ", "vi", "Xin chào thế giới!")
            first.set_audio("Xin chào thế giới!", "vi-VN-HoaiMyNeural", b"mp3 bytes")
            first.close()

            second = LocalCache(path)
            self.assertEqual(second.get_translation("Hello world!", "vi"), "Xin chào thế giới!")
            self.assertEqual(second.get_audio("Xin chào thế giới!", "vi-VN-HoaiMyNeural"), b"mp3 bytes")
            second.close()

    def test_audio_entries_are_evicted_to_stay_within_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = LocalCache(Path(tmp) / "cache.sqlite3", max_audio_entries=1)
            cache.set_audio("first", "voice", b"111")
            cache.set_audio("second", "voice", b"2222")
            self.assertIsNone(cache.get_audio("first", "voice"))
            self.assertEqual(cache.get_audio("second", "voice"), b"2222")
            self.assertEqual(cache.stats()["audio_entries"], 1)
            cache.close()

    def test_translation_cache_is_isolated_by_provider_and_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = LocalCache(Path(tmp) / "cache.sqlite3")
            cache.set_translation("Hello", "vi", "Xin chào từ Gemini", "gemini:gemini-test")
            self.assertEqual(cache.get_translation("Hello", "vi", "gemini:gemini-test"), "Xin chào từ Gemini")
            self.assertIsNone(cache.get_translation("Hello", "vi", "openai:gpt-test"))
            cache.close()


if __name__ == "__main__":
    unittest.main()
