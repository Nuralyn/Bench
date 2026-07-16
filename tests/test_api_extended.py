"""Extended tests for utils.api — call_model, strip_code_fences, _try_parse_dict.

Complements test_api.py (which covers _sanitize_error_detail). All provider
calls are mocked — no network traffic.

Run: python -m unittest tests.test_api_extended -v
"""

import os
import sys
import unittest
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

from pathlib import Path

_REPO_ROOT: Path = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from utils.api import (  # noqa: E402
    _ProviderError,
    _anthropic_call,
    _try_parse_dict,
    call_model,
    strip_code_fences,
)


class StripCodeFencesTests(unittest.TestCase):
    def test_removes_json_fence(self) -> None:
        text: str = '```json\n{"a": 1}\n```'
        self.assertEqual(strip_code_fences(text), '{"a": 1}')

    def test_removes_plain_fence(self) -> None:
        text: str = '```\n{"a": 1}\n```'
        self.assertEqual(strip_code_fences(text), '{"a": 1}')

    def test_case_insensitive_language_tag(self) -> None:
        text: str = '```JSON\n{"a": 1}\n```'
        self.assertEqual(strip_code_fences(text), '{"a": 1}')

    def test_no_fence_returns_unchanged(self) -> None:
        text: str = '{"a": 1}'
        self.assertEqual(strip_code_fences(text), '{"a": 1}')

    def test_short_string_returns_unchanged(self) -> None:
        self.assertEqual(strip_code_fences("hi"), "hi")

    def test_strips_surrounding_whitespace(self) -> None:
        text: str = '  \n```json\n{"a": 1}\n```\n  '
        self.assertEqual(strip_code_fences(text), '{"a": 1}')


class TryParseDictTests(unittest.TestCase):
    def test_valid_json_object_returns_dict(self) -> None:
        result: Any = _try_parse_dict('{"a": 1}')
        self.assertEqual(result, {"a": 1})

    def test_json_array_returns_none(self) -> None:
        self.assertIsNone(_try_parse_dict("[1, 2, 3]"))

    def test_json_string_returns_none(self) -> None:
        self.assertIsNone(_try_parse_dict('"hello"'))

    def test_invalid_json_returns_none(self) -> None:
        self.assertIsNone(_try_parse_dict("{{{malformed"))

    def test_strips_code_fences_before_parsing(self) -> None:
        text: str = '```json\n{"ok": true}\n```'
        result: Any = _try_parse_dict(text)
        self.assertEqual(result, {"ok": True})

    def test_json_integer_returns_none(self) -> None:
        self.assertIsNone(_try_parse_dict("42"))


class CallModelProviderDispatchTests(unittest.TestCase):
    @patch("utils.api._anthropic_call")
    def test_default_provider_is_anthropic(self, mock_call: MagicMock) -> None:
        mock_call.return_value = ('{"status":"ok"}', 10, 20)
        env = os.environ.copy()
        env.pop("BENCH_PROVIDER", None)
        with patch.dict("os.environ", env, clear=True):
            call_model("model", "sys", "user")
        mock_call.assert_called_once()

    @patch("utils.api._anthropic_call")
    def test_explicit_anthropic_provider(self, mock_call: MagicMock) -> None:
        mock_call.return_value = ('{"status":"ok"}', 10, 20)
        with patch.dict("os.environ", {"BENCH_PROVIDER": "anthropic"}):
            call_model("model", "sys", "user")
        mock_call.assert_called_once()

    @patch("utils.api._openrouter_call")
    def test_openrouter_provider(self, mock_call: MagicMock) -> None:
        mock_call.return_value = ('{"status":"ok"}', 10, 20)
        with patch.dict("os.environ", {"BENCH_PROVIDER": "openrouter"}):
            call_model("model", "sys", "user")
        mock_call.assert_called_once()

    def test_unknown_provider_returns_api_error(self) -> None:
        with patch.dict("os.environ", {"BENCH_PROVIDER": "unknown"}):
            result: dict = call_model("model", "sys", "user")
        self.assertEqual(result["error"], "API_ERROR")
        self.assertIn("unknown", result["detail"])


class CallModelSuccessTests(unittest.TestCase):
    @patch("utils.api._anthropic_call")
    def test_successful_parse_returns_dict_with_tokens(
        self, mock_call: MagicMock
    ) -> None:
        mock_call.return_value = ('{"status": "CLEAR"}', 10, 20)
        with patch.dict("os.environ", {"BENCH_PROVIDER": "anthropic"}):
            result: dict = call_model("model", "sys", "user")
        self.assertEqual(result["status"], "CLEAR")
        self.assertEqual(result["_tokens"], {"input": 10, "output": 20})


class CallModelRetryTests(unittest.TestCase):
    @patch("utils.api._anthropic_call")
    def test_retry_on_parse_failure_succeeds(
        self, mock_call: MagicMock
    ) -> None:
        mock_call.side_effect = [
            ("not json at all", 10, 20),
            ('{"ok": true}', 15, 25),
        ]
        with patch.dict("os.environ", {"BENCH_PROVIDER": "anthropic"}):
            result: dict = call_model("model", "sys", "user")
        self.assertTrue(result["ok"])

    @patch("utils.api._anthropic_call")
    def test_tokens_accumulated_across_retry(
        self, mock_call: MagicMock
    ) -> None:
        mock_call.side_effect = [
            ("not json", 10, 20),
            ('{"ok": true}', 15, 25),
        ]
        with patch.dict("os.environ", {"BENCH_PROVIDER": "anthropic"}):
            result: dict = call_model("model", "sys", "user")
        self.assertEqual(result["_tokens"], {"input": 25, "output": 45})

    @patch("utils.api._anthropic_call")
    def test_both_parses_fail_returns_parse_failure(
        self, mock_call: MagicMock
    ) -> None:
        mock_call.side_effect = [
            ("bad1", 10, 20),
            ("bad2", 15, 25),
        ]
        with patch.dict("os.environ", {"BENCH_PROVIDER": "anthropic"}):
            result: dict = call_model("model", "sys", "user")
        self.assertEqual(result["error"], "PARSE_FAILURE")
        self.assertEqual(result["raw_response"], "bad2")
        self.assertEqual(result["_tokens"], {"input": 25, "output": 45})


class CallModelApiErrorTests(unittest.TestCase):
    @patch("utils.api._anthropic_call")
    def test_provider_error_returns_api_error(
        self, mock_call: MagicMock
    ) -> None:
        mock_call.side_effect = _ProviderError("connection failed")
        with patch.dict("os.environ", {"BENCH_PROVIDER": "anthropic"}):
            result: dict = call_model("model", "sys", "user")
        self.assertEqual(result["error"], "API_ERROR")

    @patch("utils.api._anthropic_call")
    def test_retry_provider_error_returns_api_error(
        self, mock_call: MagicMock
    ) -> None:
        mock_call.side_effect = [
            ("not json", 10, 20),
            _ProviderError("retry failed"),
        ]
        with patch.dict("os.environ", {"BENCH_PROVIDER": "anthropic"}):
            result: dict = call_model("model", "sys", "user")
        self.assertEqual(result["error"], "API_ERROR")

    @patch("utils.api._anthropic_call")
    def test_error_detail_is_sanitized(self, mock_call: MagicMock) -> None:
        mock_call.side_effect = _ProviderError(
            "AuthenticationError: Invalid API key sk-ant-1234567890abcdef"
        )
        with patch.dict("os.environ", {"BENCH_PROVIDER": "anthropic"}):
            result: dict = call_model("model", "sys", "user")
        self.assertNotIn("sk-ant-1234567890abcdef", result["detail"])
        self.assertIn("[REDACTED]", result["detail"])


class AnthropicCallTests(unittest.TestCase):
    @patch("utils.api.anthropic.Anthropic")
    def test_successful_call_extracts_text_and_tokens(
        self, mock_cls: MagicMock
    ) -> None:
        mock_response: MagicMock = MagicMock()
        mock_response.content = [MagicMock(type="text", text='{"result": true}')]
        mock_response.usage.input_tokens = 50
        mock_response.usage.output_tokens = 100
        mock_cls.return_value.messages.create.return_value = mock_response

        text, in_tok, out_tok = _anthropic_call(
            "model", "system", [{"role": "user", "content": "hi"}], 4096
        )
        self.assertEqual(text, '{"result": true}')
        self.assertEqual(in_tok, 50)
        self.assertEqual(out_tok, 100)

    @patch("utils.api.anthropic.Anthropic")
    def test_anthropic_error_raises_provider_error(
        self, mock_cls: MagicMock
    ) -> None:
        import anthropic

        mock_cls.return_value.messages.create.side_effect = (
            anthropic.APIConnectionError(request=MagicMock())
        )
        with self.assertRaises(_ProviderError):
            _anthropic_call(
                "model", "system", [{"role": "user", "content": "hi"}], 4096
            )

    @patch("utils.api.anthropic.Anthropic")
    def test_type_error_raises_provider_error(
        self, mock_cls: MagicMock
    ) -> None:
        mock_cls.side_effect = TypeError("bad config")
        with self.assertRaises(_ProviderError):
            _anthropic_call(
                "model", "system", [{"role": "user", "content": "hi"}], 4096
            )


    @patch("utils.api.anthropic.Anthropic")
    def test_skips_thinking_block_and_extracts_text(
        self, mock_cls: MagicMock
    ) -> None:
        # Sonnet 5 runs adaptive thinking by default, so a thinking block can
        # precede the text block; the reply body must still be extracted.
        mock_response: MagicMock = MagicMock()
        mock_response.content = [
            SimpleNamespace(type="thinking", thinking="deliberating"),
            SimpleNamespace(type="text", text='{"status": "CLEAR"}'),
        ]
        mock_response.usage.input_tokens = 5
        mock_response.usage.output_tokens = 7
        mock_cls.return_value.messages.create.return_value = mock_response

        text, in_tok, out_tok = _anthropic_call(
            "model", "system", [{"role": "user", "content": "hi"}], 4096
        )
        self.assertEqual(text, '{"status": "CLEAR"}')
        self.assertEqual(in_tok, 5)
        self.assertEqual(out_tok, 7)

    @patch("utils.api.anthropic.Anthropic")
    def test_concatenates_multiple_text_blocks(
        self, mock_cls: MagicMock
    ) -> None:
        mock_response: MagicMock = MagicMock()
        mock_response.content = [
            SimpleNamespace(type="text", text='{"sta'),
            SimpleNamespace(type="text", text='tus": "CLEAR"}'),
        ]
        mock_response.usage.input_tokens = 1
        mock_response.usage.output_tokens = 1
        mock_cls.return_value.messages.create.return_value = mock_response

        text, _in_tok, _out_tok = _anthropic_call(
            "model", "system", [{"role": "user", "content": "hi"}], 4096
        )
        self.assertEqual(text, '{"status": "CLEAR"}')

    @patch("utils.api.anthropic.Anthropic")
    def test_non_text_block_with_text_attr_is_ignored(
        self, mock_cls: MagicMock
    ) -> None:
        # A non-"text" block that happens to carry a .text field must not leak
        # into the governed reply body.
        mock_response: MagicMock = MagicMock()
        mock_response.content = [
            SimpleNamespace(type="citation", text="LEAK"),
            SimpleNamespace(type="text", text='{"status": "CLEAR"}'),
        ]
        mock_response.usage.input_tokens = 1
        mock_response.usage.output_tokens = 1
        mock_cls.return_value.messages.create.return_value = mock_response

        text, _in_tok, _out_tok = _anthropic_call(
            "model", "system", [{"role": "user", "content": "hi"}], 4096
        )
        self.assertEqual(text, '{"status": "CLEAR"}')
        self.assertNotIn("LEAK", text)


if __name__ == "__main__":
    unittest.main()
