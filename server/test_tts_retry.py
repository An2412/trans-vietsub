"""Test synth() tự thử lại khi Edge TTS rớt thoáng (NoAudioReceived).

Đã tái hiện trên máy thật: cùng câu văn/cùng giọng, lần đầu FAIL, lần sau OK —
nên synth() phải tự thử lại và lần cuối đổi giọng dự phòng (NamMinh).
"""

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import main


def make_flaky(fail_calls: int = 0, empty_calls: int = 0, audio: bytes = b"ok"):
    """Tạo fake edge_tts.Communicate: N lần stream đầu raise/ra rỗng, sau đó OK.

    - raise → mô phỏng exception NoAudioReceived của edge_tts.
    - empty → stream kết thúc không có chunk audio (edge_tts đôi khi im lặng).
    """
    state = {"raise_left": fail_calls, "empty_left": empty_calls}
    calls: list[str] = []

    class _Stream:
        def __init__(self) -> None:
            self.used = False

        def __aiter__(self):
            return self

        async def __anext__(self):
            if self.used:
                raise StopAsyncIteration
            self.used = True
            if state["raise_left"] > 0:
                state["raise_left"] -= 1
                raise RuntimeError("NoAudioReceived: No audio was received.")
            if state["empty_left"] > 0:
                state["empty_left"] -= 1
                raise StopAsyncIteration
            return {"type": "audio", "data": audio}

    class _Comm:
        def __init__(self, text: str, voice: str) -> None:
            calls.append(voice)

        def stream(self):
            return _Stream()

    _Comm.calls = calls
    return _Comm


class SynthRetryTests(unittest.TestCase):
    def setUp(self):
        # Tắt delay giữa các lần thử để test chạy nhanh.
        patcher = patch.object(main, "_TTS_RETRY_DELAYS_S", (0.0, 0.0, 0.0))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_raise_then_recover_on_second_attempt(self):
        stub = make_flaky(fail_calls=1)
        with patch.object(main, "edge_tts", SimpleNamespace(Communicate=stub)):
            audio = asyncio.run(main.synth("câu thử"))
        self.assertEqual(audio, b"ok")
        self.assertEqual(stub.calls, [main.VOICE, main.VOICE])

    def test_empty_stream_counts_as_failure_then_retries(self):
        stub = make_flaky(empty_calls=1)
        with patch.object(main, "edge_tts", SimpleNamespace(Communicate=stub)):
            audio = asyncio.run(main.synth("câu rỗng"))
        self.assertEqual(audio, b"ok")
        self.assertEqual(stub.calls, [main.VOICE, main.VOICE])

    def test_recovers_with_fallback_voice_on_third_attempt(self):
        stub = make_flaky(fail_calls=2)
        with patch.object(main, "edge_tts", SimpleNamespace(Communicate=stub)):
            audio = asyncio.run(main.synth("câu bám"))
        self.assertEqual(audio, b"ok")
        # Hai lần đầu HoaiMy, lần cuối NamMinh dự phòng.
        self.assertEqual(stub.calls, [main.VOICE, main.VOICE, main._TTS_FALLBACK_VOICE])

    def test_all_attempts_fail_raises_last_error(self):
        stub = make_flaky(fail_calls=10)
        with patch.object(main, "edge_tts", SimpleNamespace(Communicate=stub)):
            with self.assertRaises(RuntimeError) as ctx:
                asyncio.run(main.synth("câu hỏng"))
        self.assertIn("NoAudioReceived", str(ctx.exception))
        self.assertEqual(len(stub.calls), 3)
        self.assertEqual(stub.calls[2], main._TTS_FALLBACK_VOICE)

    def test_custom_voice_is_used_and_falls_back_to_default(self):
        stub = make_flaky(fail_calls=2)
        with patch.object(main, "edge_tts", SimpleNamespace(Communicate=stub)):
            audio = asyncio.run(main.synth("câu thử", "vi-VN-NamMinhNeural"))
        self.assertEqual(audio, b"ok")
        # Giọng nam được chọn: hai lần đầu giữ nguyên, lần cuối rơi về giọng mặc định.
        self.assertEqual(stub.calls, ["vi-VN-NamMinhNeural", "vi-VN-NamMinhNeural", main.VOICE])

    def test_invalid_voice_is_replaced_by_default(self):
        stub = make_flaky(fail_calls=0)
        with patch.object(main, "edge_tts", SimpleNamespace(Communicate=stub)):
            asyncio.run(main.synth("câu thử", "../../etc/passwd; rm -rf"))
        self.assertEqual(stub.calls, [main.VOICE])

    def test_concurrent_synthesis_is_capped(self):
        """Nhiều /speak cùng lúc không được mở quá _TTS_CONCURRENCY phiên Edge TTS."""
        state = {"active": 0, "peak": 0}

        class _Stream:
            def __init__(self) -> None:
                self.sent = False

            def __aiter__(self):
                return self

            async def __anext__(self):
                if self.sent:
                    raise StopAsyncIteration
                self.sent = True
                state["active"] += 1
                state["peak"] = max(state["peak"], state["active"])
                try:
                    await asyncio.sleep(0.05)
                finally:
                    state["active"] -= 1
                return {"type": "audio", "data": b"ok"}

        class _Comm:
            def __init__(self, text: str, voice: str) -> None:
                self.text = text

            def stream(self):
                return _Stream()

        async def run_all():
            return await asyncio.gather(*(main.synth(f"câu {i}") for i in range(8)))

        with patch.object(main, "edge_tts", SimpleNamespace(Communicate=_Comm)):
            results = asyncio.run(run_all())

        self.assertEqual(results, [b"ok"] * 8)
        self.assertLessEqual(state["peak"], main._TTS_CONCURRENCY)
        self.assertEqual(state["peak"], main._TTS_CONCURRENCY)

    def test_attempt_timeout_is_retried_then_raised(self):
        calls = []

        class _Stream:
            def __aiter__(self):
                return self

            async def __anext__(self):
                calls.append(1)
                await asyncio.sleep(5)
                raise StopAsyncIteration

        class _Comm:
            def __init__(self, text: str, voice: str) -> None:
                pass

            def stream(self):
                return _Stream()

        with patch.object(main, "edge_tts", SimpleNamespace(Communicate=_Comm)), \
                patch.object(main, "_TTS_ATTEMPT_TIMEOUT_S", 0.01):
            with self.assertRaises(TimeoutError):
                asyncio.run(main.synth("câu treo"))

        self.assertEqual(len(calls), 3)


class VoiceSelectionTests(unittest.TestCase):
    def test_normalize_voice(self):
        self.assertEqual(main.normalize_voice("vi-VN-NamMinhNeural"), "vi-VN-NamMinhNeural")
        self.assertEqual(main.normalize_voice(" en-US-AvaMultilingualNeural "), "en-US-AvaMultilingualNeural")
        self.assertEqual(main.normalize_voice(""), main.VOICE)
        self.assertEqual(main.normalize_voice(None), main.VOICE)
        self.assertEqual(main.normalize_voice("vi-VN-NamMinhNeural; rm"), main.VOICE)
        self.assertEqual(main.normalize_voice("x" * 100), main.VOICE)

    def test_voice_list_contains_both_vietnamese_voices(self):
        ids = [voice["id"] for voice in main.VOICE_PRESETS]
        self.assertIn("vi-VN-HoaiMyNeural", ids)
        self.assertIn("vi-VN-NamMinhNeural", ids)
        self.assertEqual(main.voices()["default"], main.VOICE)

    def test_speak_uses_requested_voice(self):
        captured = {}

        async def fake_get_or_create(text, voice, synth):
            captured["text"] = text
            captured["voice"] = voice
            return b"mp3", "miss"

        with patch.object(main, "translate_with_cache", return_value=("xin chào", "hit")), \
                patch.object(main.audio_cache, "get_or_create", side_effect=fake_get_or_create):
            result = asyncio.run(main.speak(main.SpeakReq(
                text="hello", voice="vi-VN-NamMinhNeural")))

        self.assertEqual(captured["voice"], "vi-VN-NamMinhNeural")
        self.assertEqual(captured["text"], "xin chào")
        self.assertEqual(result["voice"], "vi-VN-NamMinhNeural")
        self.assertTrue(result["audio"])

    def test_speak_falls_back_to_default_for_invalid_voice(self):
        captured = {}

        async def fake_get_or_create(text, voice, synth):
            captured["voice"] = voice
            return b"mp3", "miss"

        with patch.object(main, "translate_with_cache", return_value=("xin chào", "hit")), \
                patch.object(main.audio_cache, "get_or_create", side_effect=fake_get_or_create):
            asyncio.run(main.speak(main.SpeakReq(text="hello", voice="bad voice")))

        self.assertEqual(captured["voice"], main.VOICE)


if __name__ == "__main__":
    unittest.main()
