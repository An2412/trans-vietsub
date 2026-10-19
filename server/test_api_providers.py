import io
import json
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from api_providers import AnthropicTranslator, GeminiTranslator, OpenAITranslator, _translations_from_json


class FakeResponse:
    def __init__(self, payload):
        self.payload = json.dumps(payload).encode()
    def __enter__(self):
        return self
    def __exit__(self, *args):
        return False
    def read(self):
        return self.payload


class ProviderAdapterTests(unittest.TestCase):
    def test_parses_exact_batch_and_rejects_mismatched_ids(self):
        self.assertEqual(
            _translations_from_json('{"items":[{"id":0,"vi":"Xin chào"},{"id":1,"vi":"Tạm biệt"}]}', 2),
            ["Xin chào", "Tạm biệt"],
        )
        with self.assertRaisesRegex(ValueError, "provider_batch_mismatch"):
            _translations_from_json('{"items":[{"id":1,"vi":"Lệch"}]}', 1)

    def test_gemini_interactions_request_and_response(self):
        response = {"steps": [{"type": "model_output", "content": [{"type": "text", "text": '{"items":[{"id":0,"vi":"Xin chào"}]}'}]}]}
        with patch("urllib.request.urlopen", return_value=FakeResponse(response)) as call:
            result = GeminiTranslator("gem-secret", "gemini-test").translate_batch(["Hello"])
        self.assertEqual(result, ["Xin chào"])
        req = call.call_args.args[0]
        self.assertEqual(req.full_url, "https://generativelanguage.googleapis.com/v1beta/interactions")
        self.assertEqual(req.get_header("X-goog-api-key"), "gem-secret")
        self.assertFalse(json.loads(req.data)["store"])

    def test_openai_responses_request_and_response(self):
        response = {"output": [{"type": "message", "content": [{"type": "output_text", "text": '{"items":[{"id":0,"vi":"Xin chào"}]}'}]}]}
        with patch("urllib.request.urlopen", return_value=FakeResponse(response)) as call:
            result = OpenAITranslator("openai-secret", "gpt-test").translate_batch(["Hello"])
        self.assertEqual(result, ["Xin chào"])
        req = call.call_args.args[0]
        self.assertEqual(req.full_url, "https://api.openai.com/v1/responses")
        self.assertEqual(req.get_header("Authorization"), "Bearer openai-secret")
        self.assertFalse(json.loads(req.data)["store"])

    def test_anthropic_messages_request_and_response(self):
        response = {"content": [{"type": "text", "text": '{"items":[{"id":0,"vi":"Xin chào"}]}'}]}
        with patch("urllib.request.urlopen", return_value=FakeResponse(response)) as call:
            result = AnthropicTranslator("anthropic-secret", "claude-test").translate_batch(["Hello"])
        self.assertEqual(result, ["Xin chào"])
        req = call.call_args.args[0]
        self.assertEqual(req.full_url, "https://api.anthropic.com/v1/messages")
        self.assertEqual(req.get_header("X-api-key"), "anthropic-secret")
        self.assertEqual(req.get_header("Anthropic-version"), "2023-06-01")

    def test_provider_http_error_does_not_echo_secret(self):
        error = HTTPError("https://api.openai.com/v1/responses", 401, "Unauthorized", {"Retry-After": "3"}, io.BytesIO(b'bad key'))
        with patch("urllib.request.urlopen", side_effect=error):
            with self.assertRaises(Exception) as caught:
                OpenAITranslator("must-not-leak-this", "gpt-test").translate_batch(["Hello"])
        self.assertNotIn("must-not-leak-this", str(caught.exception))
        self.assertEqual(caught.exception.status_code, 401)
        self.assertEqual(caught.exception.retry_after, 3)


if __name__ == "__main__":
    unittest.main()
