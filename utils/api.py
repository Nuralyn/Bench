"""LLM API client wrapper for the Bench pipeline.

Single point of contact with the model API. Used by Challenger, Defender,
and Oracle to issue structured JSON prompts and receive structured JSON
responses.

Provider is selected via the BENCH_PROVIDER env var:
  * "anthropic" (default) — anthropic SDK, ANTHROPIC_API_KEY
  * "openrouter"          — openai SDK + OpenRouter base URL,
                            OPENROUTER_API_KEY; model auto-prefixed with
                            "anthropic/"
  * "claude_code"         — local `claude` CLI in headless mode (`claude -p`),
                            riding the logged-in Claude Code subscription; no
                            API key. Child runs with BENCH_SUBPROCESS set to a
                            per-call random nonce, recorded in an owner-only
                            file under the Bench checkout for the duration of
                            the call, so the Bench hook can recognise its own
                            child without recursing and a guessed value earns
                            no bypass. Per-stage timeout via
                            BENCH_CLAUDE_TIMEOUT seconds (default 120).

Invariants:
  * call_model NEVER raises. Every code path returns a dict.
  * Every returned dict carries an "_tokens" field for accounting: the
    usage record {"input", "output", "cache_read", "cache_creation"}, where
    "input" is the whole prompt and the cache fields break out the part
    served from or written to the prompt cache.
  * JSON parse failure triggers exactly one retry, then returns PARSE_FAILURE.
  * API errors return API_ERROR; the pipeline decides how to react.
  * The call_model signature is identical regardless of provider. Its
    cached_prefix is text every provider sends ahead of the content; only
    the anthropic provider marks it as a prompt-cache breakpoint.
  * Every provider returns (text, usage) with that same usage record.
"""

import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    # Type-only imports. Both SDKs are imported lazily inside their provider
    # functions at runtime, since openai is a soft dependency and anthropic
    # is absent on the claude_code provider.
    from anthropic.types import MessageParam
    from openai.types.chat import ChatCompletionMessageParam

# Soft dependency, mirroring the openai treatment in requirements.txt. Only the
# BENCH_PROVIDER=anthropic path (the default) needs the SDK; claude_code and
# openrouter do not. A bare top-level import made a missing SDK an ImportError
# at module load, which crashes the hook before it can emit JSON — and a hook
# that emits no JSON fails closed, locking out every Write/Edit/MultiEdit with
# no stated cause. Tolerating it here keeps the module importable; _anthropic_call
# re-imports lazily and raises a typed _ProviderError naming the fix, so the
# anthropic path still fails closed, but legibly.
try:
    import anthropic  # noqa: F401
except ImportError:
    pass


# Single source of truth for pipeline model IDs (CLAUDE.md and README reference
# these constants by name, not by value, so the docs cannot drift). Each is the
# exact first-party Anthropic model ID: current-generation aliases are complete
# as-is, so no dated suffix is used except for models that publish dated
# snapshots (see UTILITY_MODEL). Verify each ID resolves on the target
# provider before shipping a change.
CHALLENGER_MODEL: str = "claude-sonnet-5"
DEFENDER_MODEL: str = "claude-sonnet-5"
ORACLE_MODEL: str = "claude-opus-4-8"
UTILITY_MODEL: str = "claude-haiku-4-5-20251001"

# OpenRouter publishes Anthropic slugs with a dotted version (for example
# "anthropic/claude-opus-4.8"), while the constants above use the first-party
# hyphenated IDs. Map the models the pipeline dispatches through call_model to
# their exact OpenRouter slugs. Anything unlisted (including the reserved,
# currently-uninvoked UTILITY_MODEL) falls back to the bare "anthropic/"
# prefix, which is only correct when the first-party ID and the OpenRouter slug
# coincide (as they do for claude-sonnet-5). A wrong slug would make the stage
# return API_ERROR, which the stage reports as PIPELINE_ERROR and the runner
# fails CLOSED on, returning a VETO.
_OPENROUTER_SLUGS: dict[str, str] = {
    "claude-sonnet-5": "anthropic/claude-sonnet-5",
    "claude-opus-4-8": "anthropic/claude-opus-4.8",
}

_PROVIDER_ANTHROPIC: str = "anthropic"
_PROVIDER_OPENROUTER: str = "openrouter"
_PROVIDER_CLAUDE_CLI: str = "claude_code"
_OPENROUTER_BASE_URL: str = "https://openrouter.ai/api/v1"
_DEFAULT_CLAUDE_CLI_TIMEOUT: float = 120.0
_RETRY_NUDGE: str = (
    "Your previous response was not valid JSON. Respond ONLY with valid JSON."
)


_MAX_ERROR_DETAIL_CHARS: int = 500
_SENSITIVE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"sk-[a-z0-9_-]{10,}", re.IGNORECASE),
    re.compile(r"Bearer\s+\S+", re.IGNORECASE),
    re.compile(r"api[_-]?key[\"']?\s*[:=]\s*\S+", re.IGNORECASE),
)


def _sanitize_error_detail(text: str) -> str:
    """Strip potential API keys and truncate error details."""
    scrubbed: str = text
    for pattern in _SENSITIVE_PATTERNS:
        scrubbed = pattern.sub("[REDACTED]", scrubbed)
    if len(scrubbed) > _MAX_ERROR_DETAIL_CHARS:
        return scrubbed[:_MAX_ERROR_DETAIL_CHARS] + "... [truncated]"
    return scrubbed


class _ProviderError(Exception):
    """Internal: a provider helper failed (SDK exception or missing dep).

    Raised by _anthropic_call / _openrouter_call so call_model has one
    exception type to catch regardless of which backend is active.
    """


# The per-call usage record every provider reports and every "_tokens" field
# carries. "input" is the whole prompt (uncached tokens plus cache writes plus
# cache reads) so a reader that knows only "input" and "output" still sees the
# full figure; "cache_read" and "cache_creation" break out the cached part so
# the ledger can price it at the cached rate.
_USAGE_FIELDS: tuple[str, ...] = ("input", "output", "cache_read", "cache_creation")


def _zero_usage() -> dict[str, int]:
    """A usage record with every field at zero."""
    return {field: 0 for field in _USAGE_FIELDS}


def _add_usage(totals: dict[str, int], usage: dict[str, int]) -> None:
    """Fold one call's usage into a running total, field by field."""
    for field in _USAGE_FIELDS:
        totals[field] += _coerce_int(usage.get(field, 0))


def _provider_result(
    result: tuple[str, dict[str, int]],
) -> tuple[str, dict[str, int]]:
    """Normalise a provider's (text, usage) to exactly the _USAGE_FIELDS.

    Every field is coerced to an int and a missing one reads as zero, so a
    provider that reports less than the full record cannot break the
    accounting; a usage that is not a dict at all is logged and counted as
    zero rather than raising into the verdict path.
    """
    text: str = str(result[0])
    raw: Any = result[1]
    if not isinstance(raw, dict):
        print(
            f"[bench api] provider returned usage of type "
            f"{type(raw).__name__}, not a dict; counting zero tokens",
            file=sys.stderr,
        )
        return text, _zero_usage()
    return text, {field: _coerce_int(raw.get(field, 0)) for field in _USAGE_FIELDS}


def _first_user_turn(
    provider: str, cached_prefix: str, user_content: str
) -> dict[str, Any]:
    """Build the opening user message, with a cache breakpoint where supported.

    On the anthropic provider the prefix is its own text block marked
    ``cache_control: ephemeral``. Render order is system, then messages, so
    the breakpoint caches the system prompt and the prefix together; the
    content after it (the diff, the earlier stages' findings) is never
    cached. Every other provider receives the same text as one string, in
    the same order, so no provider's judge reads a different prompt.
    """
    if not cached_prefix:
        return {"role": "user", "content": user_content}
    if provider == _PROVIDER_ANTHROPIC:
        # The second block opens with the same separator the single-string
        # form uses, so the two renderings are byte-identical text.
        return {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": cached_prefix,
                    "cache_control": {"type": "ephemeral"},
                },
                {"type": "text", "text": f"\n\n{user_content}"},
            ],
        }
    return {"role": "user", "content": f"{cached_prefix}\n\n{user_content}"}


def call_model(
    model: str,
    system_prompt: str,
    user_content: str,
    max_tokens: int = 8192,  # Sonnet 5 stages spend part of this on adaptive thinking
    cached_prefix: str = "",
) -> dict[str, Any]:
    """Call the configured LLM provider expecting a JSON-object response.

    ``cached_prefix`` is text that opens the user turn ahead of
    ``user_content`` and is the same from one governed edit to the next: the
    constitution and the repository context. See _first_user_turn for how
    each provider carries it. It is part of the prompt on every provider;
    only the anthropic provider caches it.

    Returns a dict on every code path. Successful calls return the parsed
    JSON object with an "_tokens" key appended, a usage record with the
    fields in _USAGE_FIELDS. Failure modes:
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
            "_tokens": _zero_usage(),
        }

    first_turn: dict[str, Any] = _first_user_turn(
        provider, cached_prefix, user_content
    )
    messages: list[dict[str, Any]] = [first_turn]
    totals: dict[str, int] = _zero_usage()

    try:
        first_text, usage = _provider_result(
            provider_call(model, system_prompt, messages, max_tokens)
        )
    except _ProviderError as e:
        print(f"[bench api] {_sanitize_error_detail(str(e))}", file=sys.stderr)
        return {
            "error": "API_ERROR",
            "detail": _sanitize_error_detail(str(e)),
            "_tokens": totals,
        }

    _add_usage(totals, usage)

    parsed = _try_parse_dict(first_text)
    if parsed is not None:
        parsed["_tokens"] = totals
        return parsed

    # The retry repeats the opening turn byte for byte, so on the anthropic
    # provider its prefix is a cache read rather than a second write.
    retry_messages: list[dict[str, Any]] = [
        first_turn,
        {"role": "assistant", "content": first_text},
        {"role": "user", "content": _RETRY_NUDGE},
    ]

    try:
        retry_text, usage = _provider_result(
            provider_call(model, system_prompt, retry_messages, max_tokens)
        )
    except _ProviderError as e:
        print(f"[bench api] {_sanitize_error_detail(str(e))}", file=sys.stderr)
        return {
            "error": "API_ERROR",
            "detail": _sanitize_error_detail(str(e)),
            "_tokens": totals,
        }

    _add_usage(totals, usage)

    parsed = _try_parse_dict(retry_text)
    if parsed is not None:
        parsed["_tokens"] = totals
        return parsed

    return {
        "error": "PARSE_FAILURE",
        "raw_response": retry_text,
        "_tokens": totals,
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


def _anthropic_text_blocks(content: Any) -> str:
    """Concatenate the text blocks of an Anthropic response content list.

    Adaptive-thinking models (Sonnet 5 runs adaptive thinking by default
    when `thinking` is unset) can return a thinking block as content[0];
    reading content[0].text would then yield "" and force a spurious
    PARSE_FAILURE, which the stage reports as PIPELINE_ERROR and the runner
    fails closed on, denying a legitimate change. Anchor selection to the
    documented "text" block type (and require non-empty text) so thinking,
    tool_use, or any future non-text block cannot leak into the governed
    reply body.
    """
    texts: list[str] = []
    for block in content:
        if getattr(block, "type", None) != "text":
            continue
        block_text = getattr(block, "text", "")
        if isinstance(block_text, str) and block_text:
            texts.append(block_text)
    return "".join(texts)


def _usage_int(usage: Any, name: str) -> int:
    """An integer usage field from an SDK usage object, or 0 if absent.

    The SDK reports a missing cache field as None; anything that is not a
    plain int (a bool, a mock, a string) also counts as 0 rather than
    raising, so a surprising usage object cannot break a verdict.
    """
    value: Any = getattr(usage, name, 0) if usage is not None else 0
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return 0


def _anthropic_call(
    model: str,
    system_prompt: str,
    messages: list[dict[str, Any]],
    max_tokens: int,
) -> tuple[str, dict[str, int]]:
    """One Anthropic call. Returns (text, usage).

    ``usage`` has the fields in _USAGE_FIELDS. The SDK reports the uncached
    prompt as input_tokens and the cached part separately; "input" here is
    their sum, the whole prompt, with "cache_read" and "cache_creation"
    carrying the cached part. A message whose content is a list of text
    blocks (see _first_user_turn) passes through unchanged; the SDK renders
    the blocks in order and honours the cache_control marker on the first.

    Raises _ProviderError on any anthropic.AnthropicError (covers SDK
    construction failures and all API-call exceptions) and on any
    unexpected response shape, so callers never see a raw exception.
    """
    # Imported lazily so the SDK stays a soft dependency (see the module-level
    # note above the top-level import): a missing SDK becomes a typed
    # _ProviderError that fails closed legibly instead of an ImportError at
    # module load.
    try:
        import anthropic
    except ImportError as e:
        raise _ProviderError(
            "anthropic: SDK not installed. BENCH_PROVIDER is 'anthropic' "
            "(the default), which requires it: pip install -r requirements.txt. "
            "Set BENCH_PROVIDER=claude_code to use the Claude Code CLI instead."
        ) from e

    try:
        client = anthropic.Anthropic()
        # Every message is built in call_model as {"role": "user" | "assistant",
        # "content": str}, which is the SDK's MessageParam shape. The cast
        # states that at the boundary; nothing is widened.
        response = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system_prompt,
            messages=cast("list[MessageParam]", messages),
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
            text = _anthropic_text_blocks(content)

        usage = getattr(response, "usage", None)
        uncached: int = _usage_int(usage, "input_tokens")
        cache_read: int = _usage_int(usage, "cache_read_input_tokens")
        cache_creation: int = _usage_int(usage, "cache_creation_input_tokens")
        output_tokens: int = _usage_int(usage, "output_tokens")
    except Exception as e:
        raise _ProviderError(
            f"anthropic response: {type(e).__name__}: "
            f"{_sanitize_error_detail(str(e))}"
        ) from e

    return text, {
        "input": uncached + cache_read + cache_creation,
        "output": output_tokens,
        "cache_read": cache_read,
        "cache_creation": cache_creation,
    }


def _openrouter_call(
    model: str,
    system_prompt: str,
    messages: list[dict[str, Any]],
    max_tokens: int,
) -> tuple[str, dict[str, int]]:
    """One OpenRouter call via the openai SDK. Model is auto-prefixed
    with "anthropic/". Returns (text, usage) with the _USAGE_FIELDS record;
    this path sends no cache breakpoint, so its cache fields are zero.

    Raises _ProviderError if the openai SDK is not installed (it is a
    soft dependency — not in requirements.txt) or on any openai.OpenAIError.
    """
    try:
        import openai
    except ImportError as e:
        raise _ProviderError(
            "openrouter: openai SDK not installed; pip install openai"
        ) from e

    routed_model: str = _OPENROUTER_SLUGS.get(model, f"anthropic/{model}")
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
        # full_messages is the system prompt plus call_model's user and
        # assistant turns, each {"role": ..., "content": str}: the shape the
        # SDK's message params describe. The cast states that at the boundary.
        response = client.chat.completions.create(
            model=routed_model,
            max_tokens=max_tokens,
            messages=cast("list[ChatCompletionMessageParam]", full_messages),
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
        input_tokens: int = _usage_int(usage, "prompt_tokens")
        output_tokens: int = _usage_int(usage, "completion_tokens")
    except Exception as e:
        raise _ProviderError(
            f"openrouter response: {type(e).__name__}: "
            f"{_sanitize_error_detail(str(e))}"
        ) from e

    # This path sends no cache breakpoint (see _first_user_turn), so the
    # cache fields are zero by construction rather than unreported.
    return text, {
        "input": input_tokens,
        "output": output_tokens,
        "cache_read": 0,
        "cache_creation": 0,
    }


def _coerce_int(value: Any) -> int:
    """Best-effort int coercion for token counts; returns 0 on bad input."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _flatten_cli_messages(messages: list[dict[str, Any]]) -> str:
    """Flatten messages into a single text body for stdin.

    Single-turn calls pass the user content as-is; the parse-retry path
    (user/assistant/user) is rendered with role labels so the prior reply
    and the JSON nudge survive. The system prompt is NOT folded in here:
    it goes to --system-prompt-file so it keeps system priority over this
    (untrusted) payload.
    """
    if len(messages) == 1:
        # This provider only ever receives string content (call_model builds
        # block lists for the anthropic provider alone), so str() is a
        # no-op that states the type at the boundary.
        return str(messages[0].get("content", ""))
    return "\n\n".join(
        f"{m.get('role', 'user').upper()}: {m.get('content', '')}"
        for m in messages
    )


def _resolve_cli_timeout() -> float:
    """Per-stage timeout in seconds, from BENCH_CLAUDE_TIMEOUT when valid.

    Falls back to _DEFAULT_CLAUDE_CLI_TIMEOUT (with a stderr note) when the
    variable is unparseable or not positive.
    """
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
    return timeout


def _write_system_prompt_file(system_prompt: str) -> str | None:
    """Write the stage system prompt to a temp file for --system-prompt-file.

    Returns the file path, or None when system_prompt is empty. Raises
    _ProviderError when the write fails, after removing the partially
    created file. The caller owns removal of a returned path.
    """
    if not system_prompt:
        return None
    sys_prompt_path: str | None = None
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
    return sys_prompt_path


def _parse_cli_result(
    completed: subprocess.CompletedProcess,
) -> tuple[str, dict[str, int]]:
    """Extract (text, usage) from a finished `claude` run.

    ``usage`` has the fields in _USAGE_FIELDS. Raises _ProviderError when
    the call exited non-zero or the JSON envelope is malformed or reports
    an error.
    """
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
    if not isinstance(usage, dict):
        return text, _zero_usage()

    # Claude Code applies prompt caching automatically, so most real input
    # lands in the cache fields. "input" sums all three so the ledger reflects
    # true input consumption; the two cache fields are kept apart so it can
    # be priced. Coercion is defensive: a malformed token value must not break
    # call_model's never-raises contract.
    cache_read: int = _coerce_int(usage.get("cache_read_input_tokens"))
    cache_creation: int = _coerce_int(usage.get("cache_creation_input_tokens"))
    return text, {
        "input": _coerce_int(usage.get("input_tokens")) + cache_read + cache_creation,
        "output": _coerce_int(usage.get("output_tokens")),
        "cache_read": cache_read,
        "cache_creation": cache_creation,
    }


# --- claude_code subprocess nonce ---------------------------------------------
# The child `claude -p` inherits Bench's PreToolUse hook. The hook used to
# skip governance whenever BENCH_SUBPROCESS was "1", a value anyone could
# set. Now the provider mints a random token per call, records it in an
# owner-only file under the Bench checkout, and passes only the token in the
# child's environment. The hook honours a token only while its file exists
# and is fresh, and the provider removes the file when the call returns.
SUBPROCESS_NONCE_DIRNAME: str = "subprocess"
_SUBPROCESS_NONCE_MAX_AGE_S: float = 3600.0
_SUBPROCESS_NONCE_RE: re.Pattern[str] = re.compile(r"^[0-9a-f]{32}$")
_BENCH_ROOT: Path = Path(__file__).resolve().parent.parent


def _subprocess_nonce_dir() -> Path:
    """Directory holding live subprocess nonces, one file per call.

    It sits under Bench's own gitignored ``.bench/`` so the hook can locate
    it from its install path alone; nothing about the location comes from
    the environment, so a forged variable cannot point the hook elsewhere.
    """
    return _BENCH_ROOT / ".bench" / SUBPROCESS_NONCE_DIRNAME


def issue_subprocess_nonce() -> tuple[str, Path]:
    """Mint a nonce and record it. Returns (token, file path).

    The file is created owner-only and exclusively, so a token can be
    issued once; a collision on 128 random bits is treated as an error,
    not retried. Raises OSError on any filesystem failure.
    """
    token: str = secrets.token_hex(16)
    nonce_dir: Path = _subprocess_nonce_dir()
    nonce_dir.mkdir(parents=True, exist_ok=True)
    path: Path = nonce_dir / token
    fd: int = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump({"pid": os.getpid(), "created": time.time()}, fh)
    return token, path


def revoke_subprocess_nonce(path: Path) -> None:
    """Remove a nonce file once the call it covered has returned.

    Never raises: the provider call is already finishing, and a leftover
    file only widens the bypass window until its age check expires it, so
    the failure is logged rather than escalated.
    """
    try:
        path.unlink()
    except OSError as e:
        print(
            f"[bench api] failed to remove subprocess nonce {path.name[:8]}: "
            f"{type(e).__name__}: {e}",
            file=sys.stderr,
        )


def verify_subprocess_nonce(token: str) -> bool:
    """True only for a token whose nonce file exists and is fresh.

    A malformed token never touches the filesystem. A missing or unreadable
    file is the expected outcome for a stale, guessed, or forged token and
    is logged as such; it is never an error that widens the bypass.
    """
    if not _SUBPROCESS_NONCE_RE.fullmatch(token):
        print(
            "[bench api] BENCH_SUBPROCESS is set but is not a nonce token; "
            "treating as no bypass",
            file=sys.stderr,
        )
        return False
    path: Path = _subprocess_nonce_dir() / token
    try:
        record: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        created: float = float(record["created"])
    except (OSError, ValueError, KeyError, TypeError) as e:
        print(
            f"[bench api] subprocess nonce {token[:8]} does not verify: "
            f"{type(e).__name__}; treating as no bypass",
            file=sys.stderr,
        )
        return False
    age: float = time.time() - created
    if age > _SUBPROCESS_NONCE_MAX_AGE_S:
        print(
            f"[bench api] subprocess nonce {token[:8]} is {age:.0f}s old, past "
            f"the {_SUBPROCESS_NONCE_MAX_AGE_S:.0f}s window; treating as no bypass",
            file=sys.stderr,
        )
        return False
    return True


def _claude_cli_call(
    model: str,
    system_prompt: str,
    messages: list[dict[str, Any]],
    max_tokens: int,
) -> tuple[str, dict[str, int]]:
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

    body: str = _flatten_cli_messages(messages)
    timeout: float = _resolve_cli_timeout()

    child_env: dict[str, str] = dict(os.environ)

    # Write the system prompt to a temp file loaded via --system-prompt-file so
    # the stage's role/schema instructions keep SYSTEM priority over the
    # untrusted diff on stdin (a prompt-injection diff cannot override a
    # system-priority prompt), without the multi-line-argv truncation cmd.exe
    # inflicts on --system-prompt for a .cmd/.bat shim. The file is ephemeral
    # (model input only, never a governed project file) and removed in finally.
    sys_prompt_path: str | None = _write_system_prompt_file(system_prompt)

    # Give the judge NO tools at all: --tools "" removes the built-in tools and
    # --strict-mcp-config (with no --mcp-config) removes every MCP server, so an
    # injected diff cannot make the agent run Bash/Edit/MCP/etc. This matters
    # because the child carries a live BENCH_SUBPROCESS nonce (Bench's own hook
    # is bypassed for it). Note --tools "" alone drops only built-ins, not MCP
    # tools, and --bare would isolate further but strips the subscription auth
    # (unusable).
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

    # The nonce is issued inside the try so the finally below always revokes
    # it, including when the spawn itself fails. Until it is assigned, the
    # child environment carries no BENCH_SUBPROCESS at all, which the hook
    # treats as an ordinary governed process.
    nonce_path: Path | None = None
    try:
        nonce_token, nonce_path = issue_subprocess_nonce()
        child_env["BENCH_SUBPROCESS"] = nonce_token
        # Run from an isolated, empty directory. Claude Code loads project
        # memory (CLAUDE.md) and project settings from its working directory,
        # and this child would otherwise inherit the governed project's. That
        # would hand the claude_code provider an unframed, untruncated copy of
        # the very file pipeline/runner.py passes in framed and capped, while
        # the API providers see only the framed copy - so content past the cap,
        # or instructions the framing disclaims, could move a verdict on one
        # backend and not another. The judge's evidence must not depend on the
        # transport, so the implicit load is removed and the explicit one kept.
        with tempfile.TemporaryDirectory() as work_dir:
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
                cwd=work_dir,
            )
    except subprocess.TimeoutExpired as e:
        raise _ProviderError(
            f"claude_code: `claude` timed out after {timeout}s"
        ) from e
    except (OSError, ValueError) as e:
        # OSError: spawn failure, or a nonce file that could not be created.
        # ValueError (includes UnicodeError): an encoding edge the explicit
        # utf-8 setting did not absorb. Both become a _ProviderError so
        # call_model's never-raises contract holds.
        raise _ProviderError(
            f"claude_code: failed to run `claude`: {type(e).__name__}: "
            f"{_sanitize_error_detail(str(e))}"
        ) from e
    finally:
        if nonce_path is not None:
            revoke_subprocess_nonce(nonce_path)
        if sys_prompt_path is not None:
            try:
                os.unlink(sys_prompt_path)
            except OSError as cleanup_err:
                print(
                    "[bench api] failed to remove temp system-prompt file "
                    f"{sys_prompt_path!r}: {cleanup_err}",
                    file=sys.stderr,
                )

    return _parse_cli_result(completed)
