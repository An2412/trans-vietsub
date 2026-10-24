import unittest
from unittest.mock import patch

import json

from translator import FallbackTranslator, OllamaCloudTranslator, ProviderHTTPError, RateLimitedError, build_translator


class TooManyRequests(Exception):
    pass


class AlwaysLimited:
    def __init__(self):
        self.calls = 0

    def translate(self, _text, _target="vi"):
        self.calls += 1
        raise TooManyRequests("too many requests")


class Works:
    def translate(self, _text, _target="vi"):
        return "translated"


class AuthFail:
    """Provider trả 401 (key hết hạn/thu hồi)."""

    def __init__(self):
        self.calls = 0

    def translate(self, _text, _target="vi"):
        self.calls += 1
        raise ProviderHTTPError(401, "ollama http_status=401")

    def translate_batch(self, texts, _target="vi"):
        self.calls += 1
        raise ProviderHTTPError(401, "ollama http_status=401")


class TranslatorRetryTests(unittest.TestCase):
    @patch("translator.time.sleep")
    @patch("translator.random.uniform", return_value=0)
    def test_rate_limited_provider_gets_exponential_retry_then_fallback(self, _jitter, sleep):
        limited = AlwaysLimited()
        translator = FallbackTranslator([("limited", limited), ("fallback", Works())])
        self.assertEqual(translator.translate("hello"), "translated")
        self.assertEqual(limited.calls, 3)
        delays = [call.args[0] for call in sleep.call_args_list]
        self.assertIn(2.0, delays)
        self.assertIn(4.0, delays)

    def test_auth_error_is_not_retried_and_next_provider_serves(self):
        dead = AuthFail()
        translator = FallbackTranslator([("ollama", dead), ("free", Works())])
        self.assertEqual(translator.translate("hello"), "translated")
        self.assertEqual(dead.calls, 1)
        # Câu tiếp theo: provider chết đang cooldown nên không bị gọi lại.
        self.assertEqual(translator.translate("another line"), "translated")
        self.assertEqual(dead.calls, 1)

    def test_batch_auth_error_falls_back_to_free_provider(self):
        dead = AuthFail()
        translator = FallbackTranslator([("ollama", dead), ("free", Works())])
        self.assertEqual(translator.translate_batch(["a", "b"]), ["translated", "translated"])
        self.assertEqual(dead.calls, 1)
        self.assertEqual(translator.translate_batch(["c"]), ["translated"])
        self.assertEqual(dead.calls, 1)

    @patch("translator.time.sleep")
    @patch("translator.random.uniform", return_value=0)
    def test_all_limited_providers_return_cooldown_without_repeating_calls(self, _jitter, _sleep):
        first, second = AlwaysLimited(), AlwaysLimited()
        translator = FallbackTranslator([("first", first), ("second", second)])
        with self.assertRaises(RateLimitedError) as caught:
            translator.translate("hello")
        self.assertEqual(first.calls, 3)
        self.assertEqual(second.calls, 3)
        self.assertGreater(caught.exception.retry_after, 0)

        with self.assertRaises(RateLimitedError):
            translator.translate("different text")
        self.assertEqual(first.calls, 3)
        self.assertEqual(second.calls, 3)


def chat_reply(pairs):
    return {"message": {"content": json.dumps({"items": [{"id": i, "vi": v} for i, v in pairs]})}}


class OllamaBatchTests(unittest.TestCase):
    def make(self, replies):
        t = OllamaCloudTranslator(api_key="test-key", model="m")
        calls = []

        def fake(payload):
            calls.append(payload)
            reply = replies.pop(0)
            if isinstance(reply, Exception):
                raise reply
            return reply

        t._post_chat = fake
        return t, calls

    def test_batch_is_one_request_and_keeps_order(self):
        t, calls = self.make([chat_reply([(1, "hai"), (0, "một"), (2, "ba")])])
        self.assertEqual(t.translate_batch(["one", "two", "three"]), ["một", "hai", "ba"])
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["options"]["temperature"], 0)

    def test_missing_ids_are_split_into_smaller_batches(self):
        t, calls = self.make([chat_reply([(0, "một")]), chat_reply([(0, "một"), (1, "hai")]), chat_reply([(0, "ba"), (1, "bốn")])])
        fb = FallbackTranslator([("ollama", t)])
        self.assertEqual(fb.translate_batch(["1", "2", "3", "4"]), ["một", "hai", "ba", "bốn"])
        self.assertEqual(len(calls), 3)

    @patch("translator.time.sleep")
    @patch("translator.random.uniform", return_value=0)
    def test_429_retries_then_cools_down_without_leaking_key(self, _j, _s):
        err = ProviderHTTPError(429, "ollama http 429 too many requests", retry_after=1)
        t, calls = self.make([err, err, err])
        fb = FallbackTranslator([("ollama", t)])
        with self.assertRaises(RateLimitedError) as caught:
            fb.translate_batch(["a", "b"])
        self.assertEqual(len(calls), 3)
        self.assertNotIn("test-key", str(caught.exception))
        with self.assertRaises(RateLimitedError):
            fb.translate_batch(["c"])
        self.assertEqual(len(calls), 3)  # đang cooldown, không gọi thêm

    def test_missing_key_is_rejected(self):
        import os
        old = os.environ.pop("OLLAMA_API_KEY", None)
        try:
            with self.assertRaises(RuntimeError):
                OllamaCloudTranslator()
        finally:
            if old is not None:
                os.environ["OLLAMA_API_KEY"] = old


class ConfigSourceTests(unittest.TestCase):
    @patch.dict("os.environ", {"OLLAMA_API_KEY": "must-not-be-loaded"})
    def test_default_builder_does_not_read_api_key_from_environment(self):
        translator = build_translator()
        names = [name for name, _ in translator._providers]
        self.assertNotIn("ollama", names)


if __name__ == "__main__":
    unittest.main()
