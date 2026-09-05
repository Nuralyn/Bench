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
    _first_user_turn,
    _openrouter_call,
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


def _usage(
    input_tokens: int,
    output_tokens: int,
    cache_read: int = 0,
    cache_creation: int = 0,
) -> dict[str, int]:
    """The usage record every provider returns beside its text."""
    return {
        "input": input_tokens,
        "output": output_tokens,
        "cache_read": cache_read,
        "cache_creation": cache_creation,
    }


class CallModelProviderDispatchTests(unittest.TestCase):
    @patch("utils.api._anthropic_call")
    def test_default_provider_is_anthropic(self, mock_call: MagicMock) -> None:
        mock_call.return_value = ('{"status":"ok"}', _usage(10, 20))
        env = os.environ.copy()
        env.pop("BENCH_PROVIDER", None)
        with patch.dict("os.environ", env, clear=True):
            call_model("model", "sys", "user")
        mock_call.assert_called_once()

    @patch("utils.api._anthropic_call")
    def test_explicit_anthropic_provider(self, mock_call: MagicMock) -> None:
        mock_call.return_value = ('{"status":"ok"}', _usage(10, 20))
        with patch.dict("os.environ", {"BENCH_PROVIDER": "anthropic"}):
            call_model("model", "sys", "user")
        mock_call.assert_called_once()

    @patch("utils.api._openrouter_call")
    def test_openrouter_provider(self, mock_call: MagicMock) -> None:
        mock_call.return_value = ('{"status":"ok"}', _usage(10, 20))
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
        mock_call.return_value = ('{"status": "CLEAR"}', _usage(10, 20))
        with patch.dict("os.environ", {"BENCH_PROVIDER": "anthropic"}):
            result: dict = call_model("model", "sys", "user")
        self.assertEqual(result["status"], "CLEAR")
        self.assertEqual(result["_tokens"], _usage(10, 20))

    @patch("utils.api._anthropic_call")
    def test_cache_fields_pass_through_to_tokens(
        self, mock_call: MagicMock
    ) -> None:
        mock_call.return_value = ('{"status": "CLEAR"}', _usage(1000, 20, 900, 0))
        with patch.dict("os.environ", {"BENCH_PROVIDER": "anthropic"}):
            result: dict = call_model("model", "sys", "user")
        self.assertEqual(result["_tokens"], _usage(1000, 20, 900, 0))


class CachedPrefixTests(unittest.TestCase):
    """How the cached prefix reaches each provider."""

    def test_no_prefix_sends_plain_string_content(self) -> None:
        turn: dict = _first_user_turn("anthropic", "", "BODY")
        self.assertEqual(turn, {"role": "user", "content": "BODY"})

    def test_anthropic_gets_a_breakpoint_block_then_the_body(self) -> None:
        turn: dict = _first_user_turn("anthropic", "PREFIX", "BODY")
        blocks: list[dict] = turn["content"]
        self.assertEqual(len(blocks), 2)
        self.assertEqual(blocks[0]["text"], "PREFIX")
        self.assertEqual(blocks[0]["cache_control"], {"type": "ephemeral"})
        self.assertNotIn("cache_control", blocks[1])
        # The two renderings are the same bytes: block text joined equals
        # the single string every other provider receives.
        joined: str = "".join(block["text"] for block in blocks)
        self.assertEqual(joined, "PREFIX\n\nBODY")

    def test_claude_code_keeps_context_out_of_the_lifted_block(self) -> None:
        # The CLI path lifts only the first block into its system prompt
        # file. The repository context is untrusted and rides with the
        # body, at user priority.
        turn: dict = _first_user_turn("claude_code", "PREFIX", "BODY", "CONTEXT")
        blocks: list[dict] = turn["content"]
        self.assertEqual(len(blocks), 2)
        self.assertEqual(blocks[0]["text"], "PREFIX")
        self.assertEqual(blocks[1]["text"], "\n\nCONTEXT\n\nBODY")
        self.assertNotIn("CONTEXT", blocks[0]["text"])

    def test_anthropic_breakpoint_lands_on_the_last_stable_block(self) -> None:
        turn: dict = _first_user_turn("anthropic", "PREFIX", "BODY", "CONTEXT")
        blocks: list[dict] = turn["content"]
        self.assertEqual([b["text"] for b in blocks], ["PREFIX", "\n\nCONTEXT", "\n\nBODY"])
        self.assertNotIn("cache_control", blocks[0])
        self.assertEqual(blocks[1]["cache_control"], {"type": "ephemeral"})
        self.assertNotIn("cache_control", blocks[2])

    def test_openrouter_gets_one_string_in_the_same_order(self) -> None:
        turn: dict = _first_user_turn("openrouter", "PREFIX", "BODY", "CONTEXT")
        self.assertEqual(turn["content"], "PREFIX\n\nCONTEXT\n\nBODY")

    def test_every_provider_renders_identical_text(self) -> None:
        cases: list[tuple[str, str, str]] = [
            ("PREFIX", "BODY", "CONTEXT"),
            ("PREFIX", "BODY", ""),
            ("", "BODY", "CONTEXT"),
            ("", "BODY", ""),
        ]
        for prefix, body, context in cases:
            expected: str = "\n\n".join(p for p in (prefix, context, body) if p)
            for provider in ("anthropic", "claude_code", "openrouter"):
                content = _first_user_turn(provider, prefix, body, context)["content"]
                rendered: str = (
                    content
                    if isinstance(content, str)
                    else "".join(b["text"] for b in content)
                )
                self.assertEqual(rendered, expected, (provider, prefix, context))

    @patch("utils.api._anthropic_call")
    def test_call_model_passes_the_breakpoint_turn_to_the_provider(
        self, mock_call: MagicMock
    ) -> None:
        mock_call.return_value = ('{"ok": true}', _usage(1, 1))
        with patch.dict("os.environ", {"BENCH_PROVIDER": "anthropic"}):
            call_model("model", "sys", "BODY", cached_prefix="PREFIX")
        messages: list[dict] = mock_call.call_args[0][2]
        self.assertEqual(messages[0]["content"][0]["text"], "PREFIX")
        self.assertIn("cache_control", messages[0]["content"][0])

    @patch("utils.api._anthropic_call")
    def test_retry_repeats_the_same_opening_turn(
        self, mock_call: MagicMock
    ) -> None:
        mock_call.side_effect = [
            ("not json", _usage(1, 1)),
            ('{"ok": true}', _usage(1, 1)),
        ]
        with patch.dict("os.environ", {"BENCH_PROVIDER": "anthropic"}):
            call_model("model", "sys", "BODY", cached_prefix="PREFIX")
        first: list[dict] = mock_call.call_args_list[0][0][2]
        retry: list[dict] = mock_call.call_args_list[1][0][2]
        self.assertEqual(retry[0], first[0])
        self.assertEqual(len(retry), 3)


class CallModelRetryTests(unittest.TestCase):
    @patch("utils.api._anthropic_call")
    def test_retry_on_parse_failure_succeeds(
        self, mock_call: MagicMock
    ) -> None:
        mock_call.side_effect = [
            ("not json at all", _usage(10, 20)),
            ('{"ok": true}', _usage(15, 25)),
        ]
        with patch.dict("os.environ", {"BENCH_PROVIDER": "anthropic"}):
            result: dict = call_model("model", "sys", "user")
        self.assertTrue(result["ok"])

    @patch("utils.api._anthropic_call")
    def test_tokens_accumulated_across_retry(
        self, mock_call: MagicMock
    ) -> None:
        mock_call.side_effect = [
            ("not json", _usage(10, 20, 5, 0)),
            ('{"ok": true}', _usage(15, 25, 0, 7)),
        ]
        with patch.dict("os.environ", {"BENCH_PROVIDER": "anthropic"}):
            result: dict = call_model("model", "sys", "user")
        self.assertEqual(result["_tokens"], _usage(25, 45, 5, 7))

    @patch("utils.api._anthropic_call")
    def test_both_parses_fail_returns_parse_failure(
        self, mock_call: MagicMock
    ) -> None:
        mock_call.side_effect = [
            ("bad1", _usage(10, 20)),
            ("bad2", _usage(15, 25)),
        ]
        with patch.dict("os.environ", {"BENCH_PROVIDER": "anthropic"}):
            result: dict = call_model("model", "sys", "user")
        self.assertEqual(result["error"], "PARSE_FAILURE")
        self.assertEqual(result["raw_response"], "bad2")
        self.assertEqual(result["_tokens"], _usage(25, 45))


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
            ("not json", _usage(10, 20)),
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
        # A MagicMock attribute is not an int, so an unset cache field reads
        # as zero, the same as the SDK's None for an uncached request.
        mock_cls.return_value.messages.create.return_value = mock_response

        text, usage = _anthropic_call(
            "model", "system", [{"role": "user", "content": "hi"}], 4096
        )
        self.assertEqual(text, '{"result": true}')
        self.assertEqual(usage, _usage(50, 100))

    @patch("utils.api.anthropic.Anthropic")
    def test_cache_fields_are_split_out_and_input_is_the_whole_prompt(
        self, mock_cls: MagicMock
    ) -> None:
        mock_response: MagicMock = MagicMock()
        mock_response.content = [MagicMock(type="text", text='{"ok": true}')]
        mock_response.usage.input_tokens = 40
        mock_response.usage.cache_read_input_tokens = 900
        mock_response.usage.cache_creation_input_tokens = 60
        mock_response.usage.output_tokens = 12
        mock_cls.return_value.messages.create.return_value = mock_response

        _text, usage = _anthropic_call(
            "model", "system", [{"role": "user", "content": "hi"}], 4096
        )
        self.assertEqual(usage, _usage(1000, 12, 900, 60))

    @patch("utils.api.anthropic.Anthropic")
    def test_block_content_with_breakpoint_reaches_the_sdk_unchanged(
        self, mock_cls: MagicMock
    ) -> None:
        mock_response: MagicMock = MagicMock()
        mock_response.content = [MagicMock(type="text", text="{}")]
        mock_response.usage.input_tokens = 1
        mock_response.usage.output_tokens = 1
        mock_cls.return_value.messages.create.return_value = mock_response

        turn: dict = _first_user_turn("anthropic", "PREFIX", "BODY")
        _anthropic_call("model", "system", [turn], 4096)
        sent: list[dict] = mock_cls.return_value.messages.create.call_args.kwargs[
            "messages"
        ]
        self.assertEqual(sent, [turn])
        self.assertEqual(sent[0]["content"][0]["cache_control"], {"type": "ephemeral"})

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

        text, usage = _anthropic_call(
            "model", "system", [{"role": "user", "content": "hi"}], 4096
        )
        self.assertEqual(text, '{"status": "CLEAR"}')
        self.assertEqual(usage, _usage(5, 7))

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

        text, _usage_record = _anthropic_call(
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

        text, _usage_record = _anthropic_call(
            "model", "system", [{"role": "user", "content": "hi"}], 4096
        )
        self.assertEqual(text, '{"status": "CLEAR"}')
        self.assertNotIn("LEAK", text)


class OpenRouterSlugTests(unittest.TestCase):
    """The openrouter path must send OpenRouter's published slug (dotted
    version), not the first-party hyphenated id, or the stage returns an
    API_ERROR that the runner fails open into a PASS."""

    def _routed_model(self, model: str) -> str:
        fake_openai: MagicMock = MagicMock()
        fake_openai.OpenAIError = Exception
        response: MagicMock = MagicMock()
        response.choices = [
            MagicMock(message=MagicMock(content='{"ok": true}'))
        ]
        response.usage.prompt_tokens = 1
        response.usage.completion_tokens = 1
        create = fake_openai.OpenAI.return_value.chat.completions.create
        create.return_value = response
        with patch.dict(sys.modules, {"openai": fake_openai}), patch.dict(
            os.environ, {"OPENROUTER_API_KEY": "test-key"}
        ):
            _openrouter_call(
                model, "sys", [{"role": "user", "content": "hi"}], 4096
            )
        _args, kwargs = create.call_args
        return kwargs["model"]

    def test_opus_maps_to_dotted_openrouter_slug(self) -> None:
        self.assertEqual(
            self._routed_model("claude-opus-4-8"),
            "anthropic/claude-opus-4.8",
        )

    def test_sonnet_5_maps_to_its_slug(self) -> None:
        self.assertEqual(
            self._routed_model("claude-sonnet-5"),
            "anthropic/claude-sonnet-5",
        )

    def test_unmapped_model_falls_back_to_prefix(self) -> None:
        self.assertEqual(
            self._routed_model("claude-future-9"),
            "anthropic/claude-future-9",
        )


if __name__ == "__main__":
    unittest.main()
