"""LLM API client wrapper for the Bench pipeline.

Single point of contact with the model API. Used by Challenger, Defender,
and Oracle to issue structured JSON prompts and receive structured JSON
responses.

Provider is selected via the BENCH_PROVIDER env var:
  * "anthropic" (default) — anthropic SDK, ANTHROPIC_API_KEY
  * "openrouter"          — openai SDK + OpenRouter base URL,
                            OPENROUTER_API_KEY; model auto-prefixed with
                            "anthropic/"

Invariants:
  * call_model NEVER raises. Every code path returns a dict.
  * Every returned dict carries an "_tokens" field for accounting.
  * JSON parse failure triggers exactly one retry, then returns PARSE_FAILURE.
  * API errors return API_ERROR; the pipeline decides how to react.
  * The call_model signature is identical regardless of provider.
"""

import json
import os
import sys
import traceback
from typing import Any

import anthropic


CHALLENGER_MODEL: str = "claude-sonnet-4-6"
DEFENDER_MODEL: str = "claude-sonnet-4-6"
ORACLE_MODEL: str = "claude-opus-4-7"
UTILITY_MODEL: str = "claude-haiku-4-5-20251001"

_PROVIDER_ANTHROPIC: str = "anthropic"
_PROVIDER_OPENROUTER: str = "openrouter"
_OPENROUTER_BASE_URL: str = "https://openrouter.ai/api/v1"
_RETRY_NUDGE: str = (
    "Your previous response was not valid JSON. Respond ONLY with valid JSON."
)


class _ProviderError(Exception):
    """Internal: a provider helper failed (SDK exception or missing dep).

    Raised by _anthropic_call / _openrouter_call so call_model has one
    exception type to catch regardless of which backend is active.
    """


def call_model(
    model: str,
    system_prompt: str,
    user_content: str,
    max_tokens: int = 4096,
) -> dict[str, Any]:
    """Call the configured LLM provider expecting a JSON-object response.

    Returns a dict on every code path. Successful calls return the parsed
    JSON object with an "_tokens" key appended. Failure modes:
      * {"error": "API_ERROR",      "detail": ..., "_tokens": {...}}
      * {"error": "PARSE_FAILURE",  "raw_response": ..., "_tokens": {...}}

    Tokens accumulate across the initial call and the parse-retry call.
    """
    provider: str = os.environ.get("BENCH_PROVIDER", _PROVIDER_ANTHROPIC)

    if provider == _PROVIDER_ANTHROPIC:
        provider_call = _anthropic_call
    elif provider == _PROVIDER_OPENROUTER:
        provider_call = _openrouter_call
    else:
        return {
            "error": "API_ERROR",
            "detail": f"Unknown BENCH_PROVIDER: {provider!r}",
            "_tokens": {"input": 0, "output": 0},
        }

    messages: list[dict[str, str]] = [
        {"role": "user", "content": user_content},
    ]

    total_input: int = 0
    total_output: int = 0

    try:
        first_text, in_tok, out_tok = provider_call(
            model, system_prompt, messages, max_tokens
        )
    except _ProviderError as e:
        print(f"[bench api] {e}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        return {
            "error": "API_ERROR",
            "detail": str(e),
            "_tokens": {"input": total_input, "output": total_output},
        }

    total_input += in_tok
    total_output += out_tok

    parsed = _try_parse_dict(first_text)
    if parsed is not None:
        parsed["_tokens"] = {"input": total_input, "output": total_output}
        return parsed

    retry_messages: list[dict[str, str]] = [
        {"role": "user", "content": user_content},
        {"role": "assistant", "content": first_text},
        {"role": "user", "content": _RETRY_NUDGE},
    ]

    try:
        retry_text, in_tok, out_tok = provider_call(
            model, system_prompt, retry_messages, max_tokens
        )
    except _ProviderError as e:
        print(f"[bench api] {e}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        return {
            "error": "API_ERROR",
            "detail": str(e),
            "_tokens": {"input": total_input, "output": total_output},
        }

    total_input += in_tok
    total_output += out_tok

    parsed = _try_parse_dict(retry_text)
    if parsed is not None:
        parsed["_tokens"] = {"input": total_input, "output": total_output}
        return parsed

    return {
        "error": "PARSE_FAILURE",
        "raw_response": retry_text,
        "_tokens": {"input": total_input, "output": total_output},
    }


def _try_parse_dict(text: str) -> dict[str, Any] | None:
    """Parse text as a JSON object. Returns None on JSON error or non-dict.

    Runs strip_code_fences first so a common LLM response shape — a JSON
    object wrapped in a ```json ... ``` Markdown fence — parses cleanly
    without burning a retry round-trip on the model.
    """
    cleaned: str = strip_code_fences(text)
    try:
        result: Any = json.loads(cleaned)
    except json.JSONDecodeError:
        return None
    if isinstance(result, dict):
        return result
    return None


def strip_code_fences(text: str) -> str:
    """Strip a surrounding Markdown code fence from ``text``, if present.

    Recognizes an opening ``````` or `````<lang>``
    (language tag in any casing — ``json``, ``JSON``, ``Json``, etc.) and a
    matching trailing ```````, tolerating leading and trailing
    whitespace or newlines around the block. If no surrounding fence is
    detected, ``text`` is returned unchanged. Not a general Markdown
    parser — just a cleanup pass before :func:`json.loads`.
    """
    stripped: str = text.strip()
    if len(stripped) < 6:
        return text
    if not (stripped.startswith("```") and stripped.endswith("```")):
        return text

    after_open: str = stripped[3:]
    newline_idx: int = after_open.find("\n")
    if newline_idx == -1:
        inner: str = after_open[:-3]
    else:
        inner = after_open[newline_idx + 1 :]
        if inner.endswith("```"):
            inner = inner[:-3]
    return inner.strip()


def _anthropic_call(
    model: str,
    system_prompt: str,
    messages: list[dict[str, str]],
    max_tokens: int,
) -> tuple[str, int, int]:
    """One Anthropic call. Returns (text, input_tokens, output_tokens).

    Raises _ProviderError on any anthropic.AnthropicError (covers SDK
    construction failures and all API-call exceptions).
    """
    try:
        client = anthropic.Anthropic()
        response = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system_prompt,
            messages=messages,
        )
    except anthropic.AnthropicError as e:
        raise _ProviderError(f"anthropic: {type(e).__name__}: {e}") from e
    except (TypeError, ValueError) as e:
        # Config-level failures from the SDK (missing/invalid ANTHROPIC_API_KEY,
        # malformed kwargs) are raised as stdlib exceptions, not AnthropicError.
        # Treat them as API_ERROR so call_model's non-raising contract holds.
        raise _ProviderError(f"anthropic config: {type(e).__name__}: {e}") from e

    text: str = ""
    content = getattr(response, "content", None)
    if content:
        first = content[0]
        text = getattr(first, "text", "") or ""

    usage = getattr(response, "usage", None)
    input_tokens: int = getattr(usage, "input_tokens", 0) if usage is not None else 0
    output_tokens: int = getattr(usage, "output_tokens", 0) if usage is not None else 0

    return text, input_tokens, output_tokens


def _openrouter_call(
    model: str,
    system_prompt: str,
    messages: list[dict[str, str]],
    max_tokens: int,
) -> tuple[str, int, int]:
    """One OpenRouter call via the openai SDK. Model is auto-prefixed
    with "anthropic/". Returns (text, input_tokens, output_tokens).

    Raises _ProviderError if the openai SDK is not installed (it is a
    soft dependency — not in requirements.txt) or on any openai.OpenAIError.
    """
    try:
        import openai
    except ImportError as e:
        raise _ProviderError(
            "openrouter: openai SDK not installed; pip install openai"
        ) from e

    routed_model: str = f"anthropic/{model}"
    full_messages: list[dict[str, str]] = [
        {"role": "system", "content": system_prompt},
        *messages,
    ]

    try:
        client = openai.OpenAI(
            base_url=_OPENROUTER_BASE_URL,
            api_key=os.environ.get("OPENROUTER_API_KEY"),
        )
        response = client.chat.completions.create(
            model=routed_model,
            max_tokens=max_tokens,
            messages=full_messages,
        )
    except openai.OpenAIError as e:
        raise _ProviderError(f"openrouter: {type(e).__name__}: {e}") from e
    except (TypeError, ValueError) as e:
        # Config-level failures (missing/invalid OPENROUTER_API_KEY, malformed
        # kwargs) surface as stdlib exceptions, not OpenAIError. Treat them
        # as API_ERROR so call_model's non-raising contract holds.
        raise _ProviderError(
            f"openrouter config: {type(e).__name__}: {e}"
        ) from e

    text: str = ""
    choices = getattr(response, "choices", None)
    if choices:
        message = getattr(choices[0], "message", None)
        if message is not None:
            text = getattr(message, "content", "") or ""

    usage = getattr(response, "usage", None)
    input_tokens: int = (
        getattr(usage, "prompt_tokens", 0) if usage is not None else 0
    )
    output_tokens: int = (
        getattr(usage, "completion_tokens", 0) if usage is not None else 0
    )

    return text, input_tokens, output_tokens
