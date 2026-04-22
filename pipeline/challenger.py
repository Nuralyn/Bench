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
from typing import Any

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

    user_content: str = _build_user_content(diff_info, constitution, file_context)

    response: dict[str, Any] = call_model(
        CHALLENGER_MODEL, _SYSTEM_PROMPT, user_content
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


def _build_user_content(
    diff_info: dict,
    constitution: dict,
    file_context: str,
) -> str:
    """Assemble the labeled user-content payload sent to the Challenger."""
    sections: list[str] = [
        "PROPOSED CHANGE:",
        json.dumps(diff_info, indent=2),
        "",
        "CONSTITUTION:",
        json.dumps(constitution, indent=2),
    ]
    if file_context:
        sections.extend(["", "FILE CONTEXT:", file_context])
    return "\n".join(sections)


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
