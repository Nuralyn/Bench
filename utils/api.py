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
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Any

import anthropic


CHALLENGER_MODEL: str = "claude-sonnet-4-6"
DEFENDER_MODEL: str = "claude-sonnet-4-6"
ORACLE_MODEL: str = "claude-opus-4-7"
UTILITY_MODEL: str = "claude-haiku-4-5-20251001"

_PROVIDER_ANTHROPIC: str = "anthropic"
_PROVIDER_OPENROUTER: str = "openrouter"
_PROVIDER_CLAUDE_CLI: str = "claude_code"
_OPENROUTER_BASE_URL: str = "https://openrouter.ai/api/v1"
_DEFAULT_CLAUDE_CLI_TIMEOUT: float = 120.0
_RETRY_NUDGE: str = (
    "Your previous response was not valid JSON. Respond ONLY with valid JSON."
)


_MAX_ERROR_DETAIL_CHARS: int = 500
_SENSITIVE_PATTERN: re.Pattern[str] = re.compile(
    r"(sk-[A-Za-z0-9_-]{10,}|Bearer\s+\S+|api[_-]?key[\"']?\s*[:=]\s*\S+)",
    re.IGNORECASE,
)


def _sanitize_error_detail(text: str) -> str:
    """Strip potential API keys and truncate error details."""
    scrubbed: str = _SENSITIVE_PATTERN.sub("[REDACTED]", text)
    if len(scrubbed) > _MAX_ERROR_DETAIL_CHARS:
        return scrubbed[:_MAX_ERROR_DETAIL_CHARS] + "... [truncated]"
    return scrubbed


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
    elif provider == _PROVIDER_CLAUDE_CLI:
        provider_call = _claude_cli_call
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
        print(f"[bench api] {_sanitize_error_detail(str(e))}", file=sys.stderr)
        return {
            "error": "API_ERROR",
            "detail": _sanitize_error_detail(str(e)),
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
        print(f"[bench api] {_sanitize_error_detail(str(e))}", file=sys.stderr)
        return {
            "error": "API_ERROR",
            "detail": _sanitize_error_detail(str(e)),
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
    construction failures and all API-call exceptions) and on any
    unexpected response shape, so callers never see a raw exception.
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
        raise _ProviderError(
            f"anthropic: {type(e).__name__}: {_sanitize_error_detail(str(e))}"
        ) from e
    except (TypeError, ValueError) as e:
        raise _ProviderError(
            f"anthropic config: {type(e).__name__}: {_sanitize_error_detail(str(e))}"
        ) from e

    try:
        text: str = ""
        content = getattr(response, "content", None)
        if content:
            first = content[0]
            text = getattr(first, "text", "") or ""

        usage = getattr(response, "usage", None)
        input_tokens: int = (
            getattr(usage, "input_tokens", 0) if usage is not None else 0
        )
        output_tokens: int = (
            getattr(usage, "output_tokens", 0) if usage is not None else 0
        )
    except Exception as e:
        raise _ProviderError(
            f"anthropic response: {type(e).__name__}: "
            f"{_sanitize_error_detail(str(e))}"
        ) from e

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

    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        raise _ProviderError(
            "openrouter: OPENROUTER_API_KEY environment variable is not set"
        )

    try:
        client = openai.OpenAI(
            base_url=_OPENROUTER_BASE_URL,
            api_key=api_key,
        )
        response = client.chat.completions.create(
            model=routed_model,
            max_tokens=max_tokens,
            messages=full_messages,
        )
    except openai.OpenAIError as e:
        raise _ProviderError(
            f"openrouter: {type(e).__name__}: {_sanitize_error_detail(str(e))}"
        ) from e
    except (TypeError, ValueError) as e:
        raise _ProviderError(
            f"openrouter config: {type(e).__name__}: {_sanitize_error_detail(str(e))}"
        ) from e

    try:
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
    except Exception as e:
        raise _ProviderError(
            f"openrouter response: {type(e).__name__}: "
            f"{_sanitize_error_detail(str(e))}"
        ) from e

    return text, input_tokens, output_tokens


def _coerce_int(value: Any) -> int:
    """Best-effort int coercion for token counts; returns 0 on bad input."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _claude_cli_call(
    model: str,
    system_prompt: str,
    messages: list[dict[str, str]],
    max_tokens: int,
) -> tuple[str, int, int]:
    """One call via the local `claude` CLI in headless print mode.

    Routes the stage through `claude -p` so it rides the user's Claude Code
    subscription instead of an ANTHROPIC_API_KEY. The stage system prompt is
    written to a temp file and loaded via --system-prompt-file so it keeps
    SYSTEM priority over the untrusted diff (which goes on stdin) and avoids the
    multi-line-argv truncation cmd.exe inflicts on --system-prompt for a .cmd/.bat
    shim. The reply returns as a single JSON envelope whose "result" field is the
    assistant text. Returns (text, in_tok, out_tok).

    Hardening: the call runs tool-less -- --tools "" drops the built-in tools
    and --strict-mcp-config (no --mcp-config) drops every MCP server -- so a
    prompt-injected diff cannot drive the judge to run Bash/Edit/MCP/etc. This
    matters because the child runs with BENCH_SUBPROCESS=1, which makes Bench's
    own PreToolUse hook fail open (see hooks/pre-tool-use.py); the env guard
    still prevents recursion.

    max_tokens is accepted for signature parity with the other providers; the
    CLI manages its own output cap.

    Raises _ProviderError if the binary is missing, the call exits non-zero or
    times out, or the JSON envelope is malformed or reports an error.
    """
    binary = shutil.which("claude")
    if binary is None:
        raise _ProviderError("claude_code: `claude` binary not found on PATH")

    # Flatten messages into a single text body for stdin. Single-turn calls pass
    # the user content as-is; the parse-retry path (user/assistant/user) is
    # rendered with role labels so the prior reply and the JSON nudge survive.
    # The system prompt is NOT folded in here — it goes to --system-prompt-file
    # below so it keeps system priority over this (untrusted) payload.
    if len(messages) == 1:
        body: str = messages[0].get("content", "")
    else:
        body = "\n\n".join(
            f"{m.get('role', 'user').upper()}: {m.get('content', '')}"
            for m in messages
        )

    timeout: float = _DEFAULT_CLAUDE_CLI_TIMEOUT
    timeout_raw: str = os.environ.get("BENCH_CLAUDE_TIMEOUT", "")
    if timeout_raw:
        try:
            parsed_timeout: float = float(timeout_raw)
        except ValueError:
            print(
                f"[bench api] invalid BENCH_CLAUDE_TIMEOUT={timeout_raw!r}; "
                f"using {_DEFAULT_CLAUDE_CLI_TIMEOUT}s",
                file=sys.stderr,
            )
        else:
            if parsed_timeout > 0:
                timeout = parsed_timeout
            else:
                print(
                    f"[bench api] BENCH_CLAUDE_TIMEOUT={timeout_raw!r} must be "
                    f"> 0; using {_DEFAULT_CLAUDE_CLI_TIMEOUT}s",
                    file=sys.stderr,
                )

    child_env: dict[str, str] = dict(os.environ)
    child_env["BENCH_SUBPROCESS"] = "1"

    # Write the system prompt to a temp file loaded via --system-prompt-file so
    # the stage's role/schema instructions keep SYSTEM priority over the
    # untrusted diff on stdin (a prompt-injection diff cannot override a
    # system-priority prompt), without the multi-line-argv truncation cmd.exe
    # inflicts on --system-prompt for a .cmd/.bat shim. The file is ephemeral
    # (model input only, never a governed project file) and removed in finally.
    sys_prompt_path: str | None = None
    if system_prompt:
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".txt", delete=False, encoding="utf-8"
            ) as f:
                # Record the path before writing: the file already exists on disk
                # once NamedTemporaryFile is opened, so if the write (or the
                # close-time flush) raises, the cleanup below still finds it.
                sys_prompt_path = f.name
                f.write(system_prompt)
        except OSError as e:
            if sys_prompt_path is not None:
                try:
                    os.unlink(sys_prompt_path)
                except OSError as cleanup_err:
                    print(
                        "[bench api] failed to remove temp system-prompt file "
                        f"after write error: {cleanup_err}",
                        file=sys.stderr,
                    )
            raise _ProviderError(
                "claude_code: failed to write system prompt file: "
                f"{type(e).__name__}: {_sanitize_error_detail(str(e))}"
            ) from e

    # Give the judge NO tools at all: --tools "" removes the built-in tools and
    # --strict-mcp-config (with no --mcp-config) removes every MCP server, so an
    # injected diff cannot make the agent run Bash/Edit/MCP/etc. This matters
    # because the child runs with BENCH_SUBPROCESS=1 (Bench's own hook is
    # bypassed). Note --tools "" alone drops only built-ins, not MCP tools, and
    # --bare would isolate further but strips the subscription auth (unusable).
    cmd: list[str] = [
        binary,
        "-p",
        "--output-format",
        "json",
        "--model",
        model,
        "--tools",
        "",
        "--strict-mcp-config",
    ]
    if sys_prompt_path is not None:
        cmd += ["--system-prompt-file", sys_prompt_path]

    try:
        completed = subprocess.run(
            cmd,
            input=body,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=child_env,
            shell=False,
        )
    except subprocess.TimeoutExpired as e:
        raise _ProviderError(
            f"claude_code: `claude` timed out after {timeout}s"
        ) from e
    except (OSError, ValueError) as e:
        # OSError: spawn failure. ValueError (includes UnicodeError): an encoding
        # edge the explicit utf-8 setting did not absorb. Both become a
        # _ProviderError so call_model's never-raises contract holds.
        raise _ProviderError(
            f"claude_code: failed to run `claude`: {type(e).__name__}: "
            f"{_sanitize_error_detail(str(e))}"
        ) from e
    finally:
        if sys_prompt_path is not None:
            try:
                os.unlink(sys_prompt_path)
            except OSError as cleanup_err:
                print(
                    "[bench api] failed to remove temp system-prompt file "
                    f"{sys_prompt_path!r}: {cleanup_err}",
                    file=sys.stderr,
                )

    if completed.returncode != 0:
        detail: str = _sanitize_error_detail(
            completed.stderr or completed.stdout or ""
        )
        raise _ProviderError(
            f"claude_code: `claude` exited {completed.returncode}: {detail}"
        )

    try:
        envelope: Any = json.loads(completed.stdout)
    except json.JSONDecodeError as e:
        raise _ProviderError(
            "claude_code: response was not valid JSON: "
            f"{_sanitize_error_detail(completed.stdout)}"
        ) from e

    if not isinstance(envelope, dict):
        raise _ProviderError(
            "claude_code: response envelope was not a JSON object"
        )

    if envelope.get("is_error") or envelope.get("subtype") != "success":
        detail = _sanitize_error_detail(str(envelope.get("result", envelope)))
        raise _ProviderError(f"claude_code: CLI reported error: {detail}")

    text: str = envelope.get("result", "") or ""

    usage = envelope.get("usage")
    if isinstance(usage, dict):
        # Claude Code applies prompt caching automatically, so most real input
        # lands in the cache fields; sum all three so the ledger reflects true
        # input consumption. Coercion is defensive: a malformed token value must
        # not break call_model's never-raises contract.
        input_tokens: int = (
            _coerce_int(usage.get("input_tokens"))
            + _coerce_int(usage.get("cache_creation_input_tokens"))
            + _coerce_int(usage.get("cache_read_input_tokens"))
        )
        output_tokens: int = _coerce_int(usage.get("output_tokens"))
    else:
        input_tokens = 0
        output_tokens = 0

    return text, input_tokens, output_tokens
