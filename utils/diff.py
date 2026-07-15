"""Hardened diff extraction for Bench's governance pipeline.

Turns the raw Write/Edit/MultiEdit tool_input dict into a sanitized
``diff_info`` dict ready for the Challenger -> Defender -> Oracle pipeline
to reason over. Three edge cases are handled inline:

* Binary content is replaced with a metadata-only shape so raw bytes
  never reach the LLM stages.
* Overlong diffs are truncated with governance-critical lines (first 50,
  last 20, function/class signatures, exception handlers) preserved and
  a structured notice appended.
* Write calls are labeled ``change_type: "create"`` with an addition-only
  formatted diff; Edit is ``"modify"``; MultiEdit is ``"multi_modify"``.

All extraction helpers in this module are pure: no filesystem access, no
network. ``build_diff_info`` wraps its dispatch in a top-level try/except
so unexpected exceptions do not propagate into the hook — they are logged
to stderr (not silently swallowed, per C-001) and surfaced as a
structured ``change_type: "error"`` dict so the pipeline can still record
an auditable ledger entry.
"""

import os
import os.path
import sys
import traceback
from typing import Any

MAX_DIFF_LINES: int = 300
BINARY_SNIFF_BYTES: int = 8192
BINARY_LABEL: str = "[BINARY FILE — content not evaluated]"

_PRESERVED_KINDS: str = "first50+signatures+exception_handlers+last20"
_FIRST_N: int = 50
_LAST_N: int = 20
_MAX_ERROR_MESSAGE_CHARS: int = 500
_PATH_TRAVERSAL_PLACEHOLDER: str = "[PATH_TRAVERSAL_BLOCKED]"

# Project root resolved from this file's location (utils/diff.py -> repo root),
# NOT os.getcwd(): the hook can run with a working directory below the repo
# root, and resolving against the CWD would wrongly reject in-repo edits that
# live outside it (e.g. editing utils/api.py while CWD is tests/).
_PROJECT_ROOT: str = os.path.realpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir)
)


def _normalize_relative_to_cwd(candidate: str) -> str:
    """Normalize path relative to CWD for files outside the Bench repo.

    Used by global governance to produce readable, project-relative paths
    for externally governed files. Returns CWD-relative if the file is
    inside the governed project, otherwise returns the absolute path for
    full transparency in the ledger.
    """
    try:
        cwd: str = os.path.realpath(os.getcwd())
        rel: str = os.path.relpath(candidate, cwd)
    except ValueError as exc:
        print(
            f"[bench diff] CWD-relative normalization failed for "
            f"{candidate!r}: {exc}",
            file=sys.stderr,
        )
        return candidate
    if rel == os.pardir or rel.startswith(os.pardir + os.sep):
        print(
            f"[bench diff] path escapes CWD, using absolute: {candidate!r}",
            file=sys.stderr,
        )
        return candidate
    return rel


def _normalize_path(raw_path: str) -> str:
    """Normalize a file path for governance.

    For files inside the Bench repo (_PROJECT_ROOT, derived from __file__):
    returns project-relative paths, preserving existing CWD-invariant behavior.
    _PROJECT_ROOT is NOT os.getcwd() because the hook can run with a working
    directory below the repo root, and resolving against CWD would wrongly
    reject in-repo edits that live outside it (e.g. editing utils/api.py
    while CWD is tests/).

    For files outside the Bench repo (global governance mode): normalizes
    relative to CWD, which Claude Code sets to the governed project's root.
    This path never blocks; the full absolute path is used when CWD-relative
    normalization is not possible.
    """
    if not raw_path:
        return raw_path
    root: str = _PROJECT_ROOT
    candidate: str = os.path.realpath(os.path.join(root, raw_path))
    try:
        rel: str = os.path.relpath(candidate, root)
    except ValueError as exc:
        print(
            f"[bench diff] path on different drive from Bench repo "
            f"{raw_path!r}: {exc}; normalizing against CWD",
            file=sys.stderr,
        )
        return _normalize_relative_to_cwd(candidate)
    if rel == os.pardir or rel.startswith(os.pardir + os.sep):
        print(
            f"[bench diff] path outside Bench repo {raw_path!r}; "
            f"normalizing against CWD (global governance)",
            file=sys.stderr,
        )
        return _normalize_relative_to_cwd(candidate)
    return rel


def build_diff_info(tool_name: str, tool_input: dict) -> dict[str, Any]:
    """Produce a hardened, pipeline-ready diff_info dict.

    Dispatch:
      * Write      -> change_type="create", addition-only formatted_diff
      * Edit       -> change_type="modify", old/new strings (possibly truncated)
      * MultiEdit  -> change_type="multi_modify", edits list (possibly truncated)
      * anything else -> empty dict (preserves prior hook behavior)

    Any embedded binary content anywhere in the payload collapses the
    whole dict to a metadata-only representation — raw bytes never
    appear in the output.

    Unexpected exceptions are caught, logged to stderr with a full
    traceback, and surfaced as a ``change_type: "error"`` dict. A broken
    helper must not be the reason the hook fails (C-007 fail-open).
    """
    try:
        if not isinstance(tool_input, dict):
            return {}
        file_path: str = _normalize_path(_coerce_str(tool_input.get("file_path")))

        if tool_name == "Write":
            return _build_write(file_path, tool_input)
        if tool_name == "Edit":
            return _build_edit(file_path, tool_input)
        if tool_name == "MultiEdit":
            return _build_multi_edit(file_path, tool_input)
        return {}
    except Exception as e:
        print(
            f"[bench diff] build_diff_info failed, surfacing as error dict: "
            f"{type(e).__name__}: {e}",
            file=sys.stderr,
        )
        traceback.print_exc(file=sys.stderr)
        return {
            "file_path": _safe_file_path(tool_input),
            "change_type": "error",
            "diff_error": True,
            "error_type": type(e).__name__,
            "error_message": str(e)[:_MAX_ERROR_MESSAGE_CHARS],
        }


def _build_write(file_path: str, tool_input: dict) -> dict[str, Any]:
    content: str = _coerce_str(tool_input.get("content"))
    if _is_binary(content):
        return _binary_metadata(file_path, content, "create")
    truncated, meta = _truncate_preserving(content)
    result: dict[str, Any] = {
        "file_path": file_path,
        "change_type": "create",
        "content": truncated,
        "formatted_diff": _format_as_create_diff(truncated),
    }
    if meta is not None:
        result["truncation"] = meta
    return result


def _build_edit(file_path: str, tool_input: dict) -> dict[str, Any]:
    old: str = _coerce_str(tool_input.get("old_string"))
    new: str = _coerce_str(tool_input.get("new_string"))
    if _is_binary(old) or _is_binary(new):
        return _binary_metadata(file_path, old + new, "modify")
    old_trunc, old_meta = _truncate_preserving(old)
    new_trunc, new_meta = _truncate_preserving(new)
    result: dict[str, Any] = {
        "file_path": file_path,
        "change_type": "modify",
        "old_string": old_trunc,
        "new_string": new_trunc,
    }
    truncation: dict[str, Any] = {}
    if old_meta is not None:
        truncation["old"] = old_meta
    if new_meta is not None:
        truncation["new"] = new_meta
    if truncation:
        result["truncation"] = truncation
    return result


def _build_multi_edit(file_path: str, tool_input: dict) -> dict[str, Any]:
    edits_raw: Any = tool_input.get("edits", [])
    if not isinstance(edits_raw, list):
        edits_raw = []
    for edit in edits_raw:
        if not isinstance(edit, dict):
            continue
        old_leg: str = _coerce_str(edit.get("old_string"))
        new_leg: str = _coerce_str(edit.get("new_string"))
        if _is_binary(old_leg) or _is_binary(new_leg):
            return _binary_metadata(file_path, old_leg + new_leg, "multi_modify")
    out_edits: list[dict[str, Any]] = []
    out_trunc: list[dict[str, Any]] = []
    for index, edit in enumerate(edits_raw):
        if not isinstance(edit, dict):
            out_edits.append(
                {
                    "old_string": "",
                    "new_string": "",
                    "error": "malformed edit entry",
                }
            )
            continue
        old: str = _coerce_str(edit.get("old_string"))
        new: str = _coerce_str(edit.get("new_string"))
        old_trunc, old_meta = _truncate_preserving(old)
        new_trunc, new_meta = _truncate_preserving(new)
        out_edits.append({"old_string": old_trunc, "new_string": new_trunc})
        if old_meta is not None or new_meta is not None:
            leg: dict[str, Any] = {"index": index}
            if old_meta is not None:
                leg["old"] = old_meta
            if new_meta is not None:
                leg["new"] = new_meta
            out_trunc.append(leg)
    result: dict[str, Any] = {
        "file_path": file_path,
        "change_type": "multi_modify",
        "edits": out_edits,
    }
    if out_trunc:
        result["truncation"] = out_trunc
    return result


def _coerce_str(value: Any) -> str:
    """Return value as str; empty string for None. May raise if str(value) raises —
    callers must be within the top-level fail-open guard in build_diff_info."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return str(value)


def _safe_file_path(tool_input: Any) -> str:
    """Extract file_path for the error-dict payload without re-raising.

    Used only from the top-level except in build_diff_info: the normal
    path already coerced file_path, so reaching here means that coercion
    itself raised. Fall back to repr if str() fails."""
    if not isinstance(tool_input, dict):
        return ""
    raw: Any = tool_input.get("file_path")
    if raw is None:
        return ""
    try:
        return str(raw)
    except Exception as e:
        print(
            f"[bench diff] _safe_file_path: str() failed: {type(e).__name__}",
            file=sys.stderr,
        )
        try:
            return repr(raw)
        except Exception as e2:
            print(
                f"[bench diff] _safe_file_path: repr() also failed: "
                f"{type(e2).__name__}",
                file=sys.stderr,
            )
            return "<unrepresentable>"


def _is_binary(text: str) -> bool:
    """True if the first BINARY_SNIFF_BYTES chars contain a null byte."""
    if not text:
        return False
    return "\x00" in text[:BINARY_SNIFF_BYTES]


def _binary_metadata(
    file_path: str, text: str, change_type: str
) -> dict[str, Any]:
    """Metadata-only representation used in place of raw binary content."""
    _, ext = os.path.splitext(file_path)
    return {
        "file_path": file_path,
        "change_type": change_type,
        "binary": True,
        "extension": ext,
        "content_length_bytes": len(text),
        "label": BINARY_LABEL,
    }


def _truncate_preserving(
    text: str,
) -> tuple[str, dict[str, Any] | None]:
    """Truncate text > MAX_DIFF_LINES while preserving governance-critical lines.

    Returns (possibly-truncated-text, meta-dict-or-None). If the input has
    at most MAX_DIFF_LINES lines, returns the input unchanged and None.
    Otherwise preserves the first 50 lines, the last 20 lines, every line
    whose stripped form starts with ``def `` or ``class ``, and every line
    containing ``except`` or ``catch`` (substring match). If preservation
    would keep every original line (nothing actually cut), returns the
    input unchanged and None.
    """
    lines: list[str] = text.splitlines()
    original: int = len(lines)
    if original <= MAX_DIFF_LINES:
        return text, None

    keep: set[int] = set(range(0, min(_FIRST_N, original)))
    keep.update(range(max(0, original - _LAST_N), original))
    for i, line in enumerate(lines):
        stripped: str = line.lstrip()
        if stripped.startswith("def ") or stripped.startswith("class "):
            keep.add(i)
            continue
        if "except" in line or "catch" in line:
            keep.add(i)

    if len(keep) >= original:
        return text, None

    sorted_keep: list[int] = sorted(keep)
    out_lines: list[str] = []
    prev: int = -1
    for idx in sorted_keep:
        if prev != -1 and idx != prev + 1:
            gap: int = idx - prev - 1
            out_lines.append(f"[BENCH TRUNCATION: {gap} lines omitted]")
        out_lines.append(lines[idx])
        prev = idx
    kept: int = len(sorted_keep)
    footer: str = (
        f"[BENCH TRUNCATION: original_lines={original}, "
        f"truncated_lines={kept}, preserved={_PRESERVED_KINDS}]"
    )
    out_lines.append(footer)

    tail: str = "\n" if text.endswith("\n") else ""
    meta: dict[str, Any] = {
        "original_lines": original,
        "truncated_lines": kept,
        "preserved": _PRESERVED_KINDS,
    }
    return "\n".join(out_lines) + tail, meta


def _format_as_create_diff(text: str) -> str:
    """Prefix every source line with '+' to mark as an addition-only diff."""
    if not text:
        return ""
    lines: list[str] = text.splitlines()
    prefixed: list[str] = [f"+{line}" for line in lines]
    tail: str = "\n" if text.endswith("\n") else ""
    return "\n".join(prefixed) + tail
