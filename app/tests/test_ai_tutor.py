"""Provider and configuration tests: every request uses a fake transport."""
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import urllib.error

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ai_tutor


class TutorClientTests(unittest.TestCase):
    def setUp(self):
        self.config = ai_tutor.TutorConfig(api_key="fixture-secret-only", model="fixture-model")

    def response(self, content="先看复合函数的导数。", usage=None):
        return io.BytesIO(json.dumps({"choices": [{"message": {"content": content}}],
                                     "usage": usage or {}}).encode())

    def test_request_is_bounded_nonstreaming_and_secret_is_server_only(self):
        messages = [{"role": "user", "content": "这里为什么要乘二？"}]
        with patch.object(ai_tutor.urllib.request, "urlopen", return_value=self.response(
                usage={"prompt_tokens": 12, "completion_tokens": 7, "total_tokens": 19})) as transport:
            reply, usage = ai_tutor.complete(self.config, messages)
        request = transport.call_args.args[0]
        body = json.loads(request.data)
        self.assertEqual(request.full_url, ai_tutor.ENDPOINT)
        self.assertEqual(body["messages"], messages)
        self.assertEqual(body["thinking"], {"type": "disabled"})
        self.assertFalse(body["stream"])
        self.assertEqual(body["max_tokens"], 1000)
        self.assertEqual(transport.call_args.kwargs["timeout"], 30)
        self.assertEqual(usage["total_tokens"], 19)
        self.assertNotIn(self.config.api_key, str((body, reply, usage, repr(self.config))))

    def test_bad_empty_and_overlong_responses_are_rejected(self):
        cases = [b"not-json", b"[]", b"{}", b'{"choices":null}',
                 json.dumps({"choices": [{"message": {"content": " "}}]}).encode(),
                 json.dumps({"choices": [{"message": {"content": "x" * 6001}}]}).encode(),
                 b"x" * (ai_tutor.MAX_RESPONSE_BYTES + 1)]
        for payload in cases:
            with self.subTest(size=len(payload)), patch.object(
                    ai_tutor.urllib.request, "urlopen", return_value=io.BytesIO(payload)):
                with self.assertRaises(ai_tutor.TutorError):
                    ai_tutor.complete(self.config, [])

    def test_provider_errors_never_include_body_or_credentials(self):
        errors = [TimeoutError(self.config.api_key),
                  urllib.error.URLError(self.config.api_key)]
        errors.extend(urllib.error.HTTPError(ai_tutor.ENDPOINT, code, self.config.api_key, {},
                                             io.BytesIO(self.config.api_key.encode()))
                      for code in (401, 429, 500, 503))
        for error in errors:
            with self.subTest(error=type(error).__name__), patch.object(
                    ai_tutor.urllib.request, "urlopen", side_effect=error):
                with self.assertRaises(ai_tutor.TutorError) as result:
                    ai_tutor.complete(self.config, [])
                self.assertNotIn(self.config.api_key, str(result.exception))

    def test_usage_accepts_only_nonnegative_integer_fields(self):
        with patch.object(ai_tutor.urllib.request, "urlopen", return_value=self.response(
                usage={"prompt_tokens": True, "completion_tokens": -1, "total_tokens": "7", "secret": "x"})):
            self.assertEqual(ai_tutor.complete(self.config, [])[1], {})

    def test_token_limited_reply_is_identified_as_incomplete(self):
        data = {"choices": [{"message": {"content": "现在代入上限，"}, "finish_reason": "length"}]}
        with patch.object(ai_tutor.urllib.request, "urlopen", return_value=io.BytesIO(json.dumps(data).encode())):
            reply, _ = ai_tutor.complete(self.config, [])
        self.assertIn("回复达到长度上限，可继续追问", reply)

    def test_disabled_client_never_calls_network(self):
        with patch.object(ai_tutor.urllib.request, "urlopen") as transport:
            with self.assertRaises(ai_tutor.TutorError):
                ai_tutor.complete(ai_tutor.TutorConfig(), [])
        transport.assert_not_called()

    def test_local_config_whitelist_and_environment_precedence(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / ".env").write_text(
                "DEEPSEEK_API_KEY=fixture-local\nDEEPSEEK_MODEL=local-model\n"
                "LINGKAO_AI_DAILY_LIMIT=8\nLINGKAO_DB_PATH=should-not-be-loaded\n",
                encoding="utf-8")
            with patch.dict("os.environ", {}, clear=True):
                config = ai_tutor.load_config(path, {"DEEPSEEK_API_KEY": "fixture-environment"})
                self.assertNotIn("LINGKAO_DB_PATH", ai_tutor.os.environ)
            self.assertEqual(config.api_key, "fixture-environment")
            self.assertEqual(config.model, "local-model")
            self.assertEqual(config.daily_limit, 8)
            self.assertNotIn(config.api_key, repr(config))
            for value in ("0", "-1", "not-a-number", "17"):
                with self.subTest(value=value), self.assertRaises(ValueError):
                    ai_tutor.load_config(path, {"LINGKAO_AI_CONCURRENCY": value})


if __name__ == "__main__":
    unittest.main()
