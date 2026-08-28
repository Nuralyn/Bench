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
# Line count alone does not bound payload size: a generated JSON, minified
# asset, or CSV can sit far under MAX_DIFF_LINES while carrying tens of
# thousands of characters on a handful of very long lines. Those two caps
# bound the per-line and total character budget so an unbounded payload
# cannot reach the LLM stages or the ledger.
MAX_DIFF_CHARS: int = 20000
MAX_LINE_CHARS: int = 500
# Floor on the share of the budget allocated to each side of a MultiEdit
# leg. This is an allocation minimum, never a threshold for omission: a leg
# shorter than this is shown in full, and a longer one is trimmed to at
# least this much rather than to nothing. A small edit can be the whole
# point of a change (a flipped boolean, a removed credential), so no leg is
# ever dropped for being short.
_MIN_LEG_CHARS: int = 200
# Charged against MAX_DIFF_CHARS before any line is kept, so the clamp
# marker cannot push the result past the bound it enforces. Comfortably
# larger than the marker's longest form, which is fixed text plus a line
# count.
_CLAMP_MARKER_RESERVE: int = 96
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
    helper must not crash the hook: the error dict keeps the failure
    visible so the pipeline can still adjudicate and record it (C-001).
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


def _find_binary_multi_edit_leg(edits_raw: list) -> str | None:
    """Concatenated old+new text of the first binary leg, or None."""
    for edit in edits_raw:
        if not isinstance(edit, dict):
            continue
        old_leg: str = _coerce_str(edit.get("old_string"))
        new_leg: str = _coerce_str(edit.get("new_string"))
        if _is_binary(old_leg) or _is_binary(new_leg):
            return old_leg + new_leg
    return None


def _leg_truncation_meta(
    index: int,
    old_meta: dict[str, Any] | None,
    new_meta: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Truncation record for one leg, or None when neither side truncated."""
    if old_meta is None and new_meta is None:
        return None
    leg: dict[str, Any] = {"index": index}
    if old_meta is not None:
        leg["old"] = old_meta
    if new_meta is not None:
        leg["new"] = new_meta
    return leg


def _collect_multi_edit_legs(
    edits_raw: list,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int, int]:
    """Truncate and budget each MultiEdit leg.

    Returns (out_edits, out_trunc, omitted, fitted).
    """
    out_edits: list[dict[str, Any]] = []
    out_trunc: list[dict[str, Any]] = []
    budget_used: int = 0
    omitted: int = 0
    fitted: int = 0

    # Every leg gets a fair share of the budget rather than the first legs
    # spending it all. Dropping a leg is worse than shrinking one: the tool
    # still applies the whole edit list on PASS, so a dropped leg lands on
    # disk with no stage having seen it, while a shrunk leg is at least
    # represented. The drop below stays only as a backstop for a leg count
    # so large that even the floor cannot fit.
    leg_count: int = max(len(edits_raw), 1)
    per_side: int = max(MAX_DIFF_CHARS // (2 * leg_count), _MIN_LEG_CHARS)

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

        # The per-string caps bound each leg but not their sum. Without an
        # aggregate budget a MultiEdit stays unbounded: 100 legs each just
        # under MAX_DIFF_CHARS serialize to megabytes and reach the stages
        # and the ledger in full, which is the growth this cap exists to
        # stop. Each leg is trimmed to its fair share first, so shrinking
        # is what absorbs the pressure and dropping stays a last resort.
        old_trunc, old_fit = _fit_to(old_trunc, per_side)
        new_trunc, new_fit = _fit_to(new_trunc, per_side)
        if old_fit or new_fit:
            fitted += 1

        cost: int = len(old_trunc) + len(new_trunc)
        if out_edits and budget_used + cost > MAX_DIFF_CHARS:
            omitted = len(edits_raw) - index
            break
        budget_used += cost

        out_edits.append({"old_string": old_trunc, "new_string": new_trunc})
        leg: dict[str, Any] | None = _leg_truncation_meta(
            index, old_meta, new_meta
        )
        if leg is not None:
            out_trunc.append(leg)
    return out_edits, out_trunc, omitted, fitted


def _build_multi_edit(file_path: str, tool_input: dict) -> dict[str, Any]:
    edits_raw: Any = tool_input.get("edits", [])
    if not isinstance(edits_raw, list):
        edits_raw = []
    binary_text: str | None = _find_binary_multi_edit_leg(edits_raw)
    if binary_text is not None:
        return _binary_metadata(file_path, binary_text, "multi_modify")
    out_edits, out_trunc, omitted, fitted = _collect_multi_edit_legs(edits_raw)
    result: dict[str, Any] = {
        "file_path": file_path,
        "change_type": "multi_modify",
        "edits": out_edits,
    }
    if fitted:
        result["fitted_edits"] = fitted
    if omitted:
        # Named as reduced coverage rather than as a size note. The tool
        # still applies every edit on PASS, so an omitted leg reaches disk
        # with no stage having read it. That is weaker evidence, and the
        # Oracle should weigh it as such rather than reading a short list
        # as a complete one.
        result["omitted_edits"] = omitted
        result["label"] = (
            f"[BENCH TRUNCATION: {omitted} of {len(edits_raw)} edits were "
            f"not shown to the pipeline, though all of them will be applied. "
            f"Evidence for this change is incomplete.]"
        )
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


def _cap_line(line: str) -> tuple[str, bool]:
    """Cap a single line at MAX_LINE_CHARS, marking what was dropped.

    Returns (possibly-capped-line, was_capped). The marker is visible in
    the body for the same reason BINARY_LABEL is: evidence loss reaching
    the Challenger or the ledger must be signaled, never silent (C-001).
    """
    if len(line) <= MAX_LINE_CHARS:
        return line, False
    omitted: int = len(line) - MAX_LINE_CHARS
    return (
        f"{line[:MAX_LINE_CHARS]}"
        f"[BENCH TRUNCATION: {omitted} chars omitted from line]",
        True,
    )


def _fit_to(text: str, budget: int) -> tuple[str, int]:
    """Trim ``text`` to ``budget`` chars, keeping both ends.

    Returns (text, dropped). Head and tail both survive for the same reason
    they do in ``_clamp_to_budget``: the end of an edit is often where the
    substance is, and a head-only slice would hide it. Returns the input
    untouched when it already fits, so a short leg is never altered.
    """
    if len(text) <= budget:
        return text, 0
    keep: int = max(budget - _CLAMP_MARKER_RESERVE, 0)
    dropped: int = len(text) - keep
    marker: str = f"[BENCH TRUNCATION: {dropped} chars omitted]"
    if keep == 0:
        return marker, dropped
    head_len: int = keep // 2
    tail_len: int = keep - head_len
    if tail_len == 0:
        return text[:head_len] + marker, dropped
    return text[:head_len] + marker + text[-tail_len:], dropped


def _clamp_to_budget(lines: list[str]) -> tuple[list[str], int]:
    """Fit ``lines`` into MAX_DIFF_CHARS while keeping both ends.

    Returns (lines, dropped). A prefix-only slice would satisfy the budget
    while discarding everything the selection just worked to keep: the last
    20 lines, and every signature or exception handler past the cut. The
    footer would still claim ``last20`` was preserved, so the metadata would
    be wrong in the one direction that matters, hiding the end of a change
    from all three stages while asserting it was shown.

    Lines are therefore taken alternately from the front and the back until
    the budget is spent, and what could not fit is named in the middle.
    """
    total: int = sum(len(line) + 1 for line in lines)
    if total <= MAX_DIFF_CHARS:
        return lines, 0

    # The marker is part of the output, so it is charged against the budget
    # before any line is taken. Reserving it afterwards would let the result
    # exceed MAX_DIFF_CHARS by the marker's own length, breaking the bound
    # this function exists to enforce.
    budget: int = MAX_DIFF_CHARS - _CLAMP_MARKER_RESERVE
    head: list[str] = []
    tail: list[str] = []
    used: int = 0
    low: int = 0
    high: int = len(lines) - 1
    from_head: bool = True

    while low <= high:
        candidate: str = lines[low] if from_head else lines[high]
        cost: int = len(candidate) + 1
        if used + cost > budget:
            break
        if from_head:
            head.append(candidate)
            low += 1
        else:
            tail.append(candidate)
            high -= 1
        used += cost
        from_head = not from_head

    dropped: int = high - low + 1
    marker: str = (
        f"[BENCH TRUNCATION: {dropped} lines dropped to fit "
        f"{MAX_DIFF_CHARS} chars]"
    )
    return head + [marker] + list(reversed(tail)), dropped


def _select_preserved_lines(lines: list[str], original: int) -> set[int]:
    """Indices of governance-critical lines to keep during truncation.

    Keeps the first 50 lines, the last 20 lines, every line whose stripped
    form starts with ``def `` or ``class ``, and every line containing
    ``except`` or ``catch`` (substring match).
    """
    keep: set[int] = set(range(0, min(_FIRST_N, original)))
    keep.update(range(max(0, original - _LAST_N), original))
    for i, line in enumerate(lines):
        stripped: str = line.lstrip()
        if stripped.startswith(("def ", "class ")):
            keep.add(i)
            continue
        if "except" in line or "catch" in line:
            keep.add(i)
    return keep


def _assemble_selection(
    lines: list[str], sorted_keep: list[int]
) -> tuple[list[str], bool]:
    """Render the kept lines with gap markers and per-line caps.

    Returns (out_lines, capped_any).
    """
    out_lines: list[str] = []
    prev: int = -1
    capped_any: bool = False
    for idx in sorted_keep:
        if prev != -1 and idx != prev + 1:
            gap: int = idx - prev - 1
            out_lines.append(f"[BENCH TRUNCATION: {gap} lines omitted]")
        capped_line, was_capped = _cap_line(lines[idx])
        capped_any = capped_any or was_capped
        out_lines.append(capped_line)
        prev = idx
    return out_lines, capped_any


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
    original_chars: int = len(text)
    if original <= MAX_DIFF_LINES and original_chars <= MAX_DIFF_CHARS:
        return text, None

    keep: set[int] = _select_preserved_lines(lines, original)

    sorted_keep: list[int] = sorted(keep)
    out_lines, capped_any = _assemble_selection(lines, sorted_keep)
    kept: int = len(sorted_keep)

    # Nothing was actually cut: every line survived, none needed capping, and
    # the payload is inside the char budget so the clamp below would not fire
    # either. Checked after assembly rather than before, because a short-but-
    # wide payload keeps all its lines yet still needs per-line capping. The
    # char-budget term is load-bearing: without it a payload whose lines are
    # each under MAX_LINE_CHARS but whose total exceeds MAX_DIFF_CHARS (say 50
    # lines of 400 chars) would return here unbounded, before the clamp ran.
    if kept >= original and not capped_any and original_chars <= MAX_DIFF_CHARS:
        return text, None

    # Per-line caps bound each line but not their sum, so the assembled
    # selection still needs a total budget. It is applied across the
    # selection rather than as a prefix slice: cutting from the front alone
    # would drop the last 20 lines and every signature past the cut, while
    # the footer went on claiming they were preserved.
    out_lines, dropped = _clamp_to_budget(out_lines)
    clamped: bool = dropped > 0
    body: str = "\n".join(out_lines)

    footer: str = (
        f"[BENCH TRUNCATION: original_lines={original}, "
        f"truncated_lines={kept}, original_chars={original_chars}, "
        f"preserved={_PRESERVED_KINDS}]"
    )
    body = body + "\n" + footer

    tail: str = "\n" if text.endswith("\n") else ""
    meta: dict[str, Any] = {
        "original_lines": original,
        "truncated_lines": kept,
        "original_chars": original_chars,
        "truncated_chars": len(body) + len(tail),
        "line_capped": capped_any,
        "body_clamped": clamped,
        "preserved": _PRESERVED_KINDS,
    }
    return body + tail, meta


def _format_as_create_diff(text: str) -> str:
    """Prefix every source line with '+' to mark as an addition-only diff."""
    if not text:
        return ""
    lines: list[str] = text.splitlines()
    prefixed: list[str] = [f"+{line}" for line in lines]
    tail: str = "\n" if text.endswith("\n") else ""
    return "\n".join(prefixed) + tail
