"""Formatting helpers for rendering diff_info dicts as human-readable text.

The PreToolUse hook extracts a tool-specific ``diff_info`` dict in one of
three shapes — Write, Edit, or MultiEdit. This module turns any of those
shapes into a multi-line string suitable for CLI output or log lines,
truncating overlong content with a visible marker so the output cannot
blow past a reasonable terminal width. Unrecognized or malformed shapes
fall back to a descriptive one-liner rather than raising, because a
formatting helper must never be the reason a larger operation fails.
"""

from typing import Any

_TRUNCATION_SUFFIX_TEMPLATE: str = " ... [truncated, {n} chars total]"


def format_diff_for_display(diff_info: dict, max_length: int = 2000) -> str:
    """Render ``diff_info`` as a multi-line, human-readable string.

    Recognizes three tool shapes:

    * ``Write``     — ``{file_path, content}``
    * ``Edit``      — ``{file_path, old_string, new_string}``
    * ``MultiEdit`` — ``{file_path, edits: [{old_string, new_string}, ...]}``

    Any individual string longer than ``max_length`` is truncated and
    suffixed with ``... [truncated, N chars total]`` showing the
    pre-truncation length. A non-positive ``max_length`` disables
    truncation.
    """
    if not isinstance(diff_info, dict) or not diff_info:
        return "(no diff info)"

    file_path: str = str(diff_info.get("file_path", "<unknown>"))

    if "content" in diff_info:
        body: str = _truncate(str(diff_info.get("content", "")), max_length)
        return f"Write: {file_path}\n---\n{body}"

    if "old_string" in diff_info or "new_string" in diff_info:
        old: str = _truncate(str(diff_info.get("old_string", "")), max_length)
        new: str = _truncate(str(diff_info.get("new_string", "")), max_length)
        return (
            f"Edit: {file_path}\n"
            f"--- old ---\n{old}\n"
            f"--- new ---\n{new}"
        )

    if "edits" in diff_info:
        edits_raw: Any = diff_info.get("edits", [])
        if not isinstance(edits_raw, list):
            return (
                f"MultiEdit: {file_path}\n"
                f"(edits field is not a list: got "
                f"{type(edits_raw).__name__})"
            )
        pieces: list[str] = [f"MultiEdit: {file_path}"]
        for index, edit in enumerate(edits_raw, start=1):
            if not isinstance(edit, dict):
                pieces.append(f"  [{index}] (malformed edit entry)")
                continue
            old = _truncate(str(edit.get("old_string", "")), max_length)
            new = _truncate(str(edit.get("new_string", "")), max_length)
            pieces.append(
                f"  [{index}] old:\n{_indent(old)}\n"
                f"      new:\n{_indent(new)}"
            )
        return "\n".join(pieces)

    keys: str = ", ".join(sorted(str(k) for k in diff_info.keys())) or "(empty)"
    return f"Unknown diff shape for {file_path} (keys: {keys})"


def _truncate(text: str, max_length: int) -> str:
    if max_length <= 0 or len(text) <= max_length:
        return text
    suffix: str = _TRUNCATION_SUFFIX_TEMPLATE.format(n=len(text))
    return text[:max_length] + suffix


def _indent(text: str, prefix: str = "        ") -> str:
    return "\n".join(prefix + line for line in text.splitlines()) or prefix
