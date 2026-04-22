"""PreToolUse hook entry point for the Bench governance pipeline.

Claude Code invokes this script before any Write/Edit/MultiEdit tool call,
piping a JSON payload to stdin. The script returns a JSON response on stdout
that either allows or denies the tool call via permissionDecision.

Day 1 stub: governance always returns PASS. Day 2 will wire this to the real
challenger -> defender -> oracle pipeline.

Invariants:
  * Exit code is ALWAYS 0. Flow control is via JSON, not exit codes.
    (Exit-2 would cause Claude Code to stall.)
  * All structured output goes to stdout. All diagnostics go to stderr.
  * The hook fails open: any internal error returns 'allow' so a broken
    governance layer never blocks the developer. Failures are logged to
    stderr for the developer to notice.
"""

import json
import sys
import traceback
from typing import Any


def run_governance_stub(
    tool_name: str,
    diff_info: dict[str, Any],
) -> dict[str, Any]:
    """Day 1 placeholder for the real pipeline. Always returns PASS.

    On Day 2 this is replaced by the challenger/defender/oracle runner.
    The signature is shaped to match what the real runner will need:
    the tool name and the extracted diff fields.
    """
    del tool_name, diff_info  # unused in stub; real pipeline will consume
    return {"verdict": "PASS"}


def extract_diff_info(tool_name: str, tool_input: dict[str, Any]) -> dict[str, Any]:
    """Pull the change-relevant fields out of tool_input by tool kind.

    Unknown tool names return an empty dict — the hook still runs governance
    on them so the stub can decide what to do, but we don't fabricate fields.
    """
    if tool_name == "Write":
        return {
            "file_path": tool_input.get("file_path"),
            "content": tool_input.get("content"),
        }
    if tool_name == "Edit":
        return {
            "file_path": tool_input.get("file_path"),
            "old_string": tool_input.get("old_string"),
            "new_string": tool_input.get("new_string"),
        }
    if tool_name == "MultiEdit":
        return {
            "file_path": tool_input.get("file_path"),
            "edits": tool_input.get("edits", []),
        }
    return {}


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

        diff_info: dict[str, Any] = extract_diff_info(tool_name, tool_input)
        verdict: dict[str, Any] = run_governance_stub(tool_name, diff_info)
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
