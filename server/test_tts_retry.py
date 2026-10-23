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


if __name__ == "__main__":
    unittest.main()
