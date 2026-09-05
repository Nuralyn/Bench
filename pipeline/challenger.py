"""Challenger stage of the Bench governance pipeline.

The Challenger is the adversarial first pass. It receives a proposed code
change and the constitution snapshot, examines the change against every
constraint, and emits structured findings for the Oracle to rule on. It does
not decide outcomes.

Invariants:
  * run_challenger NEVER raises. Every code path returns a dict.
  * Every returned dict carries the "_tokens" field from call_model.
  * API / parse errors from utils.api are wrapped as PIPELINE_ERROR with the
    original error payload preserved under "error".
  * Structurally invalid model responses return PIPELINE_ERROR with the raw
    response preserved under "raw_response".
"""

import json
import sys
from typing import Any

from pipeline.constitution import build_cached_prefix, build_context_section
from utils.api import CHALLENGER_MODEL, call_model


_SYSTEM_PROMPT: str = """You are the Challenger in the Bench constitutional governance pipeline. Your role
is adversarial. You exist to find problems that would otherwise ship silently.

You will receive:
1. A proposed code change (diff)
2. The current constitution (a set of binding constraints)
3. Relevant file context (the file being modified)

Your job:
- Examine the proposed change against EVERY constraint in the constitution
- Identify any violations, potential violations, or areas of concern
- Be thorough but honest. Do not fabricate issues. Do not stretch interpretations
  to manufacture violations that don't exist
- If the change is clean, say so. A Challenger who cries wolf on every change
  destroys the integrity of the governance pipeline

For each finding, you must specify:
- Which constitutional constraint (by ID) is implicated
- What specifically in the diff triggers the concern
- The severity: VIOLATION (clear breach), CONCERN (potential issue worth examining),
  or OBSERVATION (notable but not actionable)

If you find no issues, return status: CLEAR. Do not invent problems to justify
your existence.

You are not the judge. You do not decide outcomes. You surface evidence for the
Oracle to evaluate. Your integrity depends on accuracy, not volume.

Respond ONLY with valid JSON matching this schema:

{
  "status": "FINDINGS" | "CLEAR",
  "findings": [
    {
      "constraint_id": "C-XXX",
      "severity": "VIOLATION" | "CONCERN" | "OBSERVATION",
      "location": "file:line or description of location in diff",
      "evidence": "exact code or pattern from the diff",
      "reasoning": "why this implicates the constraint"
    }
  ]
}"""


_VALID_STATUSES: frozenset[str] = frozenset({"FINDINGS", "CLEAR"})
_VALID_SEVERITIES: frozenset[str] = frozenset(
    {"VIOLATION", "CONCERN", "OBSERVATION"}
)
_REQUIRED_FINDING_FIELDS: tuple[str, ...] = (
    "constraint_id",
    "severity",
    "location",
    "evidence",
    "reasoning",
)


def _validate_challenger_inputs(
    diff_info: dict, constitution: dict
) -> str | None:
    """Return an error message if inputs are malformed, else None."""
    if not isinstance(diff_info, dict) or not diff_info:
        return "diff_info is empty or not a dict"
    if "file_path" not in diff_info and "change_type" not in diff_info:
        return "diff_info missing both file_path and change_type"
    if not isinstance(constitution, dict) or not constitution:
        return "constitution is empty or not a dict"
    constraints: Any = constitution.get("constraints")
    if not isinstance(constraints, list):
        return f"constitution.constraints is not a list (got {type(constraints).__name__})"
    return None


def run_challenger(
    diff_info: dict,
    constitution: dict,
    constitution_hash: str,
    file_context: str = "",
) -> dict[str, Any]:
    """Run the Challenger stage over a proposed diff.

    constitution_hash is accepted for signature uniformity with the rest of
    the pipeline (the runner records it per-stage) but is not injected into
    the prompt — the Challenger reasons from the constitution body.
    """
    del constitution_hash  # unused in prompt; recorded by the runner

    input_error: str | None = _validate_challenger_inputs(diff_info, constitution)
    if input_error is not None:
        print(
            f"[bench challenger] input validation failed: {input_error}",
            file=sys.stderr,
        )
        return {
            "status": "PIPELINE_ERROR",
            "error": f"INVALID_CHALLENGER_INPUT: {input_error}",
            "_tokens": {"input": 0, "output": 0},
        }

    # The prompt is the constitution (cached prefix), the repository context
    # (cached where a provider can do so at user priority), then this
    # edit's content. All built fresh from this run's snapshot; the cache
    # holds bytes, not a stale snapshot, and a changed constitution renders
    # different bytes and misses it.
    cached_prefix: str = build_cached_prefix(constitution)
    cached_context: str = build_context_section(file_context)
    user_content: str = _build_user_content(diff_info)

    response: dict[str, Any] = call_model(
        CHALLENGER_MODEL,
        _SYSTEM_PROMPT,
        user_content,
        cached_prefix=cached_prefix,
        cached_context=cached_context,
    )

    tokens: Any = response.get("_tokens", {"input": 0, "output": 0})

    if "error" in response:
        return {
            "status": "PIPELINE_ERROR",
            "error": response,
            "_tokens": tokens,
        }

    if not _validate_challenger_response(response):
        return {
            "status": "PIPELINE_ERROR",
            "error": "INVALID_CHALLENGER_RESPONSE",
            "raw_response": response,
            "_tokens": tokens,
        }

    return response


def _build_user_content(diff_info: dict) -> str:
    """Assemble the per-edit part of the Challenger's prompt: the change.

    The constitution and the repository context come first as the cached
    prefix (pipeline.constitution.build_cached_prefix), the rendering every
    stage receives: each constraint's id, name, scope, rule, and severity,
    without the rationale and commentary written for a human reader. The
    rule is sent whole; nothing a constraint forbids is dropped.
    """
    return "\n".join(["PROPOSED CHANGE:", json.dumps(diff_info, indent=2)])


def _validate_challenger_response(response: dict[str, Any]) -> bool:
    """Return True if the response matches the Challenger output schema."""
    status: Any = response.get("status")
    if status not in _VALID_STATUSES:
        return False

    if status == "CLEAR":
        return True

    findings: Any = response.get("findings")
    if not isinstance(findings, list):
        return False

    for finding in findings:
        if not isinstance(finding, dict):
            return False
        for field in _REQUIRED_FINDING_FIELDS:
            value: Any = finding.get(field)
            if not isinstance(value, str) or not value:
                return False
        if finding["severity"] not in _VALID_SEVERITIES:
            return False

    return True
