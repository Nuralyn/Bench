"""PreToolUse hook entry point for the Bench governance pipeline.

Claude Code invokes this script before any Write/Edit/MultiEdit tool call,
piping a JSON payload to stdin. The script returns a JSON response on stdout
that either allows or denies the tool call via permissionDecision.

Dispatches to the Challenger -> Defender -> Oracle runner in pipeline/runner.py.

Invariants:
  * Exit code is ALWAYS 0. Flow control is via JSON, not exit codes.
    (Exit-2 would cause Claude Code to stall.)
  * All structured output goes to stdout. All diagnostics go to stderr.
  * The hook fails closed: if governance cannot run (pipeline import
    failure) or the hook itself errors, the change is denied, not allowed,
    so a broken or exploited pipeline cannot wave changes through. Failures
    are logged to stderr. The sole exception is the reentrancy guard for a
    Bench-spawned governance subprocess (the claude_code provider), which is
    allowed so the pipeline does not recurse into itself and deadlock.
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
    structured diff info for governance. The hardening is forfeited but
    coverage is not (C-007 continuity). A stderr warning is emitted per
    call so degraded-mode operation is observable at the call site.

    Unknown tool names return an empty dict. The hook still runs
    governance on them so the pipeline can decide what to do, but we
    don't fabricate fields.

    Global governance behavior (differs from prior fallback): when a
    file path resolves outside the Bench repo root (_REPO_ROOT), the
    fallback no longer returns the sentinel ``[PATH_TRAVERSAL_BLOCKED]``.
    Instead it delegates to ``_fallback_normalize_to_cwd`` (defined above
    in this module) which normalizes the path relative to CWD (the
    governed project's root). This enables governance of edits in
    external projects without over-blocking. A ``_path_normalized_external``
    flag is set on the returned dict so pipeline stages (Challenger,
    Defender, Oracle) can see that the path originated outside the
    Bench repo and was normalized via CWD heuristics.

    C-005 test coverage: both CWD-normalization branches (cross-drive
    ValueError and escapes-repo-root) are covered by
    TestFallbackExternalNormalization in tests/test_hook.py.
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
    path_external: bool = False
    if raw_path:
        # Mirror utils.diff._normalize_path: resolve against the Bench repo
        # root (_REPO_ROOT, from __file__) for in-repo files. _REPO_ROOT is
        # NOT os.getcwd() because the hook can run with a working directory
        # below the repo root, and resolving against CWD would wrongly reject
        # in-repo edits (e.g. editing utils/api.py while CWD is tests/).
        # For files outside the Bench repo (global governance), fall back to
        # CWD-relative normalization via _fallback_normalize_to_cwd.
        root: str = os.path.realpath(str(_REPO_ROOT))
        candidate: str = os.path.realpath(os.path.join(root, raw_path))
        try:
            normalized = os.path.relpath(candidate, root)
        except ValueError as exc:
            print(
                f"[bench hook] path on different drive from Bench repo "
                f"(fallback) {raw_path!r}: {exc}; normalizing against CWD",
                file=sys.stderr,
            )
            normalized = _fallback_normalize_to_cwd(candidate)
            path_external = True
        else:
            if normalized == os.pardir or normalized.startswith(
                os.pardir + os.sep
            ):
                print(
                    f"[bench hook] path outside Bench repo (fallback) "
                    f"{raw_path!r}; normalizing against CWD",
                    file=sys.stderr,
                )
                normalized = _fallback_normalize_to_cwd(candidate)
                path_external = True
    result: dict[str, Any]
    if tool_name == "Write":
        result = {
            "file_path": normalized,
            "content": tool_input.get("content"),
        }
    elif tool_name == "Edit":
        result = {
            "file_path": normalized,
            "old_string": tool_input.get("old_string"),
            "new_string": tool_input.get("new_string"),
        }
    elif tool_name == "MultiEdit":
        result = {
            "file_path": normalized,
            "edits": tool_input.get("edits", []),
        }
    else:
        result = {}
    if path_external:
        result["_path_normalized_external"] = True
    return result


def _fallback_normalize_to_cwd(candidate: str) -> str:
    """Normalize path relative to CWD for files outside the Bench repo.

    Mirrors utils.diff._normalize_relative_to_cwd for the degraded fallback
    path. Returns CWD-relative if the file is inside the governed project,
    otherwise returns the absolute path for transparency in the ledger.
    """
    try:
        cwd: str = os.path.realpath(os.getcwd())
        rel: str = os.path.relpath(candidate, cwd)
    except ValueError as exc:
        print(
            f"[bench hook] CWD-relative normalization failed in fallback "
            f"for {candidate!r}: {exc}",
            file=sys.stderr,
        )
        return candidate
    if rel == os.pardir or rel.startswith(os.pardir + os.sep):
        print(
            f"[bench hook] path escapes CWD in fallback, using absolute: "
            f"{candidate!r}",
            file=sys.stderr,
        )
        return candidate
    return rel


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
        return warnings
    if tool_name not in _GOVERNED_TOOLS:
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
    """Translate a governance verdict into the Claude Code hook response shape.

    VETO: reason is 'BENCH VETO [C-XXX]: <reasoning>' (constraint tag
    omitted when the pipeline surfaced no violated constraints) and
    additionalContext carries a 'Remediation: ' prefixed message, per the
    documented hook response format. PASS: fixed sentence plus any Oracle
    advisories.
    """
    decision: str = verdict.get("verdict", "PASS")
    if decision == "VETO":
        reasoning: Any = verdict.get("reason")
        if not isinstance(reasoning, str) or not reasoning:
            reasoning = "Change rejected by governance pipeline."
        violated: Any = verdict.get("violated_constraints")
        if isinstance(violated, list) and violated:
            tag: str = ", ".join(str(cid) for cid in violated)
            reason: str = f"BENCH VETO [{tag}]: {reasoning}"
        else:
            reason = f"BENCH VETO: {reasoning}"
        remediation_raw: Any = verdict.get("remediation")
        if isinstance(remediation_raw, str) and remediation_raw:
            remediation: str = f"Remediation: {remediation_raw}"
        else:
            remediation = "Remediation: see ledger entry for details."
        return build_deny_response(reason, remediation)

    message: str = "Bench governance: PASS. All constraints satisfied."
    advisories: Any = verdict.get("advisories")
    if isinstance(advisories, list):
        advisory_texts: list[str] = [
            a for a in advisories if isinstance(a, str) and a
        ]
        if advisory_texts:
            message = f"{message} Advisories: " + " | ".join(advisory_texts)
    return build_allow_response(message)


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
                "verdict": "VETO",
                "reason": (
                    "Governance pipeline is unavailable (import failed); "
                    "cannot adjudicate. Failing closed."
                ),
                "remediation": (
                    "The pipeline failed to import (see stderr). Fix the import "
                    "error, then retry. Changes are blocked until governance can "
                    "run. Emergency recovery is a human editing files directly, "
                    "outside Claude Code's governed tools."
                ),
            }
        else:
            verdict = run_governance_pipeline(tool_name, tool_input, diff_info)
        response: dict[str, Any] = build_response_from_verdict(verdict)

    except Exception as e:  # fail-closed: an unadjudicated change must not pass
        print(
            f"[bench hook] internal error, failing closed: {type(e).__name__}: {e}",
            file=sys.stderr,
        )
        traceback.print_exc(file=sys.stderr)
        response = build_deny_response(
            "BENCH VETO: governance hook error; the change could not be "
            "adjudicated. Failing closed.",
            "Remediation: the hook raised an unexpected error (see stderr). Fix "
            "it, then retry. Emergency recovery is a human editing files "
            "directly, outside Claude Code's governed tools.",
        )

    json.dump(response, sys.stdout)
    sys.stdout.write("\n")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
