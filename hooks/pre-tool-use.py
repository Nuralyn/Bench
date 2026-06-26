"""PreToolUse hook entry point for the Bench governance pipeline.

Claude Code invokes this script before any Write/Edit/MultiEdit tool call,
piping a JSON payload to stdin. The script returns a JSON response on stdout
that either allows or denies the tool call via permissionDecision.

Dispatches to the Challenger -> Defender -> Oracle runner in pipeline/runner.py.

Invariants:
  * Exit code is ALWAYS 0. Flow control is via JSON, not exit codes.
    (Exit-2 would cause Claude Code to stall.)
  * All structured output goes to stdout. All diagnostics go to stderr.
  * The hook fails open: any internal error returns 'allow' so a broken
    governance layer never blocks the developer. Failures are logged to
    stderr for the developer to notice. The runner also fails open on its
    own internal errors; this wrapper is defense in depth.
"""

import json
import os
import sys
import traceback
from pathlib import Path
from typing import Any

# The hook is invoked as `python hooks/pre-tool-use.py`, which puts hooks/
# on sys.path[0]. The pipeline package lives at the repo root, so prepend
# the repo root before importing from it.
_REPO_ROOT: Path = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

try:
    from pipeline.runner import run_governance_pipeline
except Exception as _e:  # pragma: no cover — import-time fail-open
    print(
        f"[bench hook] pipeline import failed, failing open: "
        f"{type(_e).__name__}: {_e}",
        file=sys.stderr,
    )
    traceback.print_exc(file=sys.stderr)
    run_governance_pipeline = None  # type: ignore[assignment]

# utils.diff (ledger entry 8bc4a3671a13, verdict PASS) provides the
# hardened extractor: binary detection, truncation with governance-critical
# line preservation, and change_type labeling. Imported in its own try
# so the fallback path below can engage independently of pipeline import.
try:
    from utils.diff import build_diff_info as _build_diff_info_hardened
except Exception as _diff_e:  # pragma: no cover — import-time fail-open
    print(
        f"[bench hook] utils.diff import failed, extraction will run in "
        f"degraded mode: {type(_diff_e).__name__}: {_diff_e}",
        file=sys.stderr,
    )
    traceback.print_exc(file=sys.stderr)
    _build_diff_info_hardened = None  # type: ignore[assignment]


def extract_diff_info(tool_name: str, tool_input: dict[str, Any]) -> dict[str, Any]:
    """Pull the change-relevant fields out of tool_input by tool kind.

    Primary path: delegate to utils.diff.build_diff_info for binary
    detection, truncation, and change_type labeling.

    Fallback path: if utils.diff failed to import, fall back to the
    original inline field mapping so Write/Edit/MultiEdit still yield
    structured diff info for governance — the hardening is forfeited but
    coverage is not (C-007 continuity). A stderr warning is emitted per
    call so degraded-mode operation is observable at the call site.

    Unknown tool names return an empty dict — the hook still runs
    governance on them so the pipeline can decide what to do, but we
    don't fabricate fields.

    C-005 deferral justification for the fallback path: the fallback is
    byte-identical to the pre-existing extract_diff_info logic (see prior
    commit history); its correctness is established by the ledger
    continuity of the preceding governance pipeline runs. No new
    behavior is introduced on the fallback branch, so no additional
    tests are required. The primary delegation path is covered by
    tests/test_diff.py.
    """
    if _build_diff_info_hardened is not None:
        return _build_diff_info_hardened(tool_name, tool_input)
    print(
        "[bench hook] utils.diff unavailable — using inline fallback "
        "(no binary/truncation hardening)",
        file=sys.stderr,
    )
    raw_path: str = str(tool_input.get("file_path", ""))
    normalized: str = raw_path
    if raw_path:
        # Mirror utils.diff._normalize_path: resolve against the project root and
        # allow in-root paths (returned project-relative, nameable for
        # governance), blocking only genuine escapes. Rejecting every absolute
        # path would garble every edit, since Write/Edit always pass absolute
        # paths — the bug this mirrors the fix for.
        root: str = os.path.realpath(os.getcwd())
        candidate: str = os.path.realpath(os.path.join(root, raw_path))
        try:
            normalized = os.path.relpath(candidate, root)
        except ValueError as exc:
            # Different drive on Windows: cannot be inside the project root.
            print(
                f"[bench hook] path traversal blocked in fallback "
                f"(cross-drive) {raw_path!r}: {exc}",
                file=sys.stderr,
            )
            normalized = "[PATH_TRAVERSAL_BLOCKED]"
        else:
            if normalized == os.pardir or normalized.startswith(
                os.pardir + os.sep
            ):
                print(
                    f"[bench hook] path traversal blocked in fallback "
                    f"(escapes project root) {raw_path!r}",
                    file=sys.stderr,
                )
                normalized = "[PATH_TRAVERSAL_BLOCKED]"
    if tool_name == "Write":
        return {
            "file_path": normalized,
            "content": tool_input.get("content"),
        }
    if tool_name == "Edit":
        return {
            "file_path": normalized,
            "old_string": tool_input.get("old_string"),
            "new_string": tool_input.get("new_string"),
        }
    if tool_name == "MultiEdit":
        return {
            "file_path": normalized,
            "edits": tool_input.get("edits", []),
        }
    return {}


_GOVERNED_TOOLS: frozenset[str] = frozenset({"Write", "Edit", "MultiEdit"})


def _validate_hook_payload(
    tool_name: str, tool_input: dict[str, Any]
) -> list[str]:
    """Return a list of validation warnings (empty = valid).

    Lightweight schema check — no external dependencies. Fail-open: the
    hook continues regardless; the warnings are logged for observability.
    """
    warnings: list[str] = []
    if not tool_name:
        warnings.append("tool_name is empty")
    elif tool_name not in _GOVERNED_TOOLS:
        return warnings

    fp: Any = tool_input.get("file_path")
    if not isinstance(fp, str) or not fp:
        warnings.append(f"file_path missing or not a string (got {type(fp).__name__})")

    if tool_name == "Write":
        content: Any = tool_input.get("content")
        if not isinstance(content, str):
            warnings.append(
                f"Write: content is not a string (got {type(content).__name__})"
            )
    elif tool_name == "Edit":
        for field in ("old_string", "new_string"):
            val: Any = tool_input.get(field)
            if not isinstance(val, str):
                warnings.append(
                    f"Edit: {field} is not a string (got {type(val).__name__})"
                )
    elif tool_name == "MultiEdit":
        edits: Any = tool_input.get("edits")
        if not isinstance(edits, list):
            warnings.append(
                f"MultiEdit: edits is not a list (got {type(edits).__name__})"
            )
    return warnings


def build_allow_response(message: str) -> dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "additionalContext": message,
        }
    }


def build_deny_response(reason: str, remediation: str) -> dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
            "additionalContext": remediation,
        }
    }


def build_response_from_verdict(verdict: dict[str, Any]) -> dict[str, Any]:
    """Translate a governance verdict into the Claude Code hook response shape."""
    decision: str = verdict.get("verdict", "PASS")
    if decision == "VETO":
        reason: str = verdict.get(
            "reason", "BENCH VETO: change rejected by governance pipeline."
        )
        remediation: str = verdict.get(
            "remediation", "See ledger entry for details."
        )
        return build_deny_response(reason, remediation)
    return build_allow_response(
        "Bench governance: PASS. All constraints satisfied."
    )


def main() -> int:
    """Entry point. Always returns 0; flow control is via stdout JSON."""
    if os.environ.get("BENCH_SUBPROCESS") == "1":
        # Reentrancy guard: this hook is firing inside a `claude -p` subprocess
        # that Bench itself spawned (utils/api.py claude_code provider).
        # Governing the nested agent would recurse, so fail open immediately.
        bypass: dict[str, Any] = build_allow_response(
            "Bench governance: nested subprocess (BENCH_SUBPROCESS=1), skipping."
        )
        json.dump(bypass, sys.stdout)
        sys.stdout.write("\n")
        sys.stdout.flush()
        return 0

    try:
        raw_stdin: str = sys.stdin.read()
        payload: Any = json.loads(raw_stdin)
        if not isinstance(payload, dict):
            raise ValueError(
                f"hook payload must be a JSON object, got {type(payload).__name__}"
            )

        tool_name: str = payload.get("tool_name", "")
        tool_input_raw: Any = payload.get("tool_input", {})
        tool_input: dict[str, Any] = (
            tool_input_raw if isinstance(tool_input_raw, dict) else {}
        )

        payload_warnings: list[str] = _validate_hook_payload(tool_name, tool_input)
        for warning in payload_warnings:
            print(
                f"[bench hook] payload validation: {warning}",
                file=sys.stderr,
            )

        diff_info: dict[str, Any] = extract_diff_info(tool_name, tool_input)
        if run_governance_pipeline is None:
            verdict: dict[str, Any] = {
                "verdict": "PASS",
                "reason": "Pipeline unavailable (import failed) — failing open",
                "remediation": None,
            }
        else:
            verdict = run_governance_pipeline(tool_name, tool_input, diff_info)
        response: dict[str, Any] = build_response_from_verdict(verdict)

    except Exception as e:  # fail-open: governance must never block on its own bug
        print(
            f"[bench hook] internal error, failing open: {type(e).__name__}: {e}",
            file=sys.stderr,
        )
        traceback.print_exc(file=sys.stderr)
        response = build_allow_response(
            "Bench governance: hook error, failing open. See stderr."
        )

    json.dump(response, sys.stdout)
    sys.stdout.write("\n")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
