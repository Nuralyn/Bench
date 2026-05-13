"""Defender stage of the Bench governance pipeline.

The Defender receives the same diff and constitution as the Challenger plus
the Challenger's findings. It argues for the soundness of the change by
rebutting, conceding, or mitigating each finding. It does not decide outcomes.

Invariants:
  * run_defender NEVER raises. Every code path returns a dict.
  * Every returned dict carries the "_tokens" field from call_model.
  * API / parse errors from utils.api are wrapped as PIPELINE_ERROR with the
    original error payload preserved under "error".
  * Structurally invalid model responses return PIPELINE_ERROR with the raw
    response preserved under "raw_response".
"""

import json
import sys
from typing import Any

from utils.api import DEFENDER_MODEL, call_model


_SYSTEM_PROMPT: str = """You are the Defender in the Bench constitutional governance pipeline. Your role
is to argue for the soundness of the proposed change.

You will receive:
1. A proposed code change (diff)
2. The current constitution (a set of binding constraints)
3. The Challenger's findings (their case against the change)

Your job:
- Evaluate each Challenger finding and provide a rebuttal, concession, or
  context that the Challenger may have missed
- If a finding is legitimate, CONCEDE. Do not defend indefensible code. A
  Defender who defends everything is as useless as a Challenger who challenges
  everything
- If a finding is based on a misreading of the diff, missing context, or a
  stretched interpretation of a constraint, make that case clearly
- You may also raise MITIGATIONS: reasons why a technical violation exists but
  the practical risk is low or the tradeoff is justified

You are an advocate, not a sycophant. Your credibility with the Oracle depends
on honest assessment. When the code is wrong, say so. When the Challenger is
wrong, prove it.

If the Challenger returned CLEAR, confirm or dispute with your own analysis.

Respond ONLY with valid JSON matching this schema:

{
  "status": "REBUTTAL" | "CONCEDE_ALL" | "CONFIRM_CLEAR",
  "rebuttals": [
    {
      "finding_index": 0,
      "position": "REBUT" | "CONCEDE" | "MITIGATE",
      "argument": "your detailed argument",
      "evidence": "supporting evidence from the diff or file context"
    }
  ],
  "summary": "one sentence overall assessment of the change's soundness"
}"""


_VALID_STATUSES: frozenset[str] = frozenset(
    {"REBUTTAL", "CONCEDE_ALL", "CONFIRM_CLEAR"}
)
_VALID_POSITIONS: frozenset[str] = frozenset({"REBUT", "CONCEDE", "MITIGATE"})
_REQUIRED_REBUTTAL_STRING_FIELDS: tuple[str, ...] = ("position", "argument")


def _validate_defender_inputs(
    diff_info: dict, constitution: dict, challenger_result: dict
) -> str | None:
    """Return an error message if inputs are malformed, else None."""
    if not isinstance(diff_info, dict) or not diff_info:
        return "diff_info is empty or not a dict"
    if not isinstance(constitution, dict) or not constitution:
        return "constitution is empty or not a dict"
    if not isinstance(challenger_result, dict) or not challenger_result:
        return "challenger_result is empty or not a dict"
    if "status" not in challenger_result:
        return "challenger_result missing status field"
    return None


def run_defender(
    diff_info: dict,
    constitution: dict,
    constitution_hash: str,
    challenger_result: dict,
    file_context: str = "",
) -> dict[str, Any]:
    """Run the Defender stage over a diff and the Challenger's findings.

    constitution_hash is accepted for signature uniformity with the rest of
    the pipeline (the runner records it per-stage) but is not injected into
    the prompt — the Defender reasons from the constitution body.
    """
    del constitution_hash  # unused in prompt; recorded by the runner

    input_error: str | None = _validate_defender_inputs(
        diff_info, constitution, challenger_result
    )
    if input_error is not None:
        print(
            f"[bench defender] input validation failed: {input_error}",
            file=sys.stderr,
        )
        return {
            "status": "PIPELINE_ERROR",
            "error": f"INVALID_DEFENDER_INPUT: {input_error}",
            "_tokens": {"input": 0, "output": 0},
        }

    user_content: str = _build_user_content(
        diff_info, constitution, challenger_result, file_context
    )

    response: dict[str, Any] = call_model(
        DEFENDER_MODEL, _SYSTEM_PROMPT, user_content
    )

    tokens: Any = response.get("_tokens", {"input": 0, "output": 0})

    if "error" in response:
        return {
            "status": "PIPELINE_ERROR",
            "error": response,
            "_tokens": tokens,
        }

    if not _validate_defender_response(response):
        return {
            "status": "PIPELINE_ERROR",
            "error": "INVALID_DEFENDER_RESPONSE",
            "raw_response": response,
            "_tokens": tokens,
        }

    return response


def _build_user_content(
    diff_info: dict,
    constitution: dict,
    challenger_result: dict,
    file_context: str,
) -> str:
    """Assemble the labeled user-content payload sent to the Defender."""
    sections: list[str] = [
        "PROPOSED CHANGE:",
        json.dumps(diff_info, indent=2),
        "",
        "CONSTITUTION:",
        json.dumps(constitution, indent=2),
        "",
        "CHALLENGER FINDINGS:",
        json.dumps(challenger_result, indent=2),
    ]
    if file_context:
        sections.extend(["", "FILE CONTEXT:", file_context])
    return "\n".join(sections)


def _validate_defender_response(response: dict[str, Any]) -> bool:
    """Return True if the response matches the Defender output schema."""
    status: Any = response.get("status")
    if status not in _VALID_STATUSES:
        return False

    summary: Any = response.get("summary")
    if not isinstance(summary, str) or not summary:
        return False

    if status != "REBUTTAL":
        return True

    rebuttals: Any = response.get("rebuttals")
    if not isinstance(rebuttals, list):
        return False

    for rebuttal in rebuttals:
        if not isinstance(rebuttal, dict):
            return False
        finding_index: Any = rebuttal.get("finding_index")
        if not isinstance(finding_index, int) or isinstance(finding_index, bool):
            return False
        for field in _REQUIRED_REBUTTAL_STRING_FIELDS:
            value: Any = rebuttal.get(field)
            if not isinstance(value, str) or not value:
                return False
        if rebuttal["position"] not in _VALID_POSITIONS:
            return False

    return True
