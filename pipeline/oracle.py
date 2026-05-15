"""Oracle stage of the Bench governance pipeline.

The Oracle is the final authority. It receives the diff, the constitution,
the Challenger's findings, and the Defender's rebuttals, then issues a
binding verdict (PASS or VETO) with reasoning, constraint citations,
advisories, remediation (on VETO), and a confidence level.

Invariants:
  * run_oracle NEVER raises. Every code path returns a dict.
  * Every returned dict carries the "_tokens" field from call_model.
  * API / parse errors from utils.api are wrapped as PIPELINE_ERROR with the
    original error payload preserved under "error".
  * Structurally invalid model responses return PIPELINE_ERROR with the raw
    response preserved under "raw_response".
  * A VETO without a non-empty remediation string is rejected as invalid: a
    veto that cannot be remediated is a governance dead-end.
"""

import json
import sys
from typing import Any

from utils.api import ORACLE_MODEL, call_model


_SYSTEM_PROMPT: str = """You are the Oracle in the Bench constitutional governance pipeline. You are the
final authority. Your verdict is binding.

You will receive:
1. A proposed code change (diff)
2. The current constitution (a set of binding constraints with severities)
3. The Challenger's findings
4. The Defender's rebuttals

Your job:
- Weigh the Challenger's evidence against the Defender's arguments
- Evaluate the change against the constitution as a whole, not just the
  specific constraints the Challenger cited. You may identify violations
  they missed
- Issue a binding verdict: PASS or VETO

VETO rules:
- You MUST veto if any constraint with severity "veto" is clearly violated
  and the Defender's rebuttal does not adequately address it
- You MUST explain which constraint(s) triggered the veto
- You MUST provide actionable feedback: what would the change need to look
  like to pass governance?

PASS rules:
- A PASS means the change meets constitutional standards
- You may issue a PASS with ADVISORIES: things that aren't violations but
  the developer should be aware of
- A PASS is not an endorsement of quality. It is a certification that the
  change does not violate the declared constitution

You are not here to have opinions about code style, architecture preferences,
or "better ways" to do things unless those preferences are encoded in the
constitution. You enforce declared law, not personal taste.

Your reasoning must be traceable. Every claim in your verdict must reference
either a constitutional constraint, a Challenger finding, or a Defender
rebuttal. Unsupported assertions are a governance failure.

Respond ONLY with valid JSON matching this schema:

{
  "verdict": "PASS" | "VETO",
  "reasoning": "detailed reasoning that references specific constraints, findings, and rebuttals",
  "constraint_citations": [
    {
      "constraint_id": "C-XXX",
      "disposition": "SATISFIED" | "VIOLATED" | "NOT_APPLICABLE",
      "note": "brief explanation"
    }
  ],
  "advisories": [
    "optional warnings that don't trigger a veto but should be noted"
  ],
  "remediation": "if VETO: specific guidance on what the change needs to pass. if PASS: null",
  "confidence": "HIGH" | "MODERATE" | "LOW"
}"""


_VALID_VERDICTS: frozenset[str] = frozenset({"PASS", "VETO"})
_VALID_DISPOSITIONS: frozenset[str] = frozenset(
    {"SATISFIED", "VIOLATED", "NOT_APPLICABLE"}
)
_VALID_CONFIDENCES: frozenset[str] = frozenset({"HIGH", "MODERATE", "LOW"})
_REQUIRED_CITATION_FIELDS: tuple[str, ...] = (
    "constraint_id",
    "disposition",
    "note",
)


def _validate_oracle_inputs(
    diff_info: dict,
    constitution: dict,
    challenger_result: dict,
    defender_result: dict,
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
    if not isinstance(defender_result, dict) or not defender_result:
        return "defender_result is empty or not a dict"
    if "status" not in defender_result:
        return "defender_result missing status field"
    return None


def run_oracle(
    diff_info: dict,
    constitution: dict,
    constitution_hash: str,
    challenger_result: dict,
    defender_result: dict,
    file_context: str = "",
) -> dict[str, Any]:
    """Run the Oracle stage and return a binding verdict.

    constitution_hash is accepted for signature uniformity with the rest of
    the pipeline (the runner records it per-stage) but is not injected into
    the prompt — the Oracle reasons from the constitution body.
    """
    del constitution_hash  # unused in prompt; recorded by the runner

    input_error: str | None = _validate_oracle_inputs(
        diff_info, constitution, challenger_result, defender_result
    )
    if input_error is not None:
        print(
            f"[bench oracle] input validation failed: {input_error}",
            file=sys.stderr,
        )
        return {
            "status": "PIPELINE_ERROR",
            "error": f"INVALID_ORACLE_INPUT: {input_error}",
            "_tokens": {"input": 0, "output": 0},
        }

    user_content: str = _build_user_content(
        diff_info,
        constitution,
        challenger_result,
        defender_result,
        file_context,
    )

    response: dict[str, Any] = call_model(
        ORACLE_MODEL, _SYSTEM_PROMPT, user_content
    )

    tokens: Any = response.get("_tokens", {"input": 0, "output": 0})

    if "error" in response:
        return {
            "status": "PIPELINE_ERROR",
            "error": response,
            "_tokens": tokens,
        }

    if not _validate_oracle_response(response):
        return {
            "status": "PIPELINE_ERROR",
            "error": "INVALID_ORACLE_RESPONSE",
            "raw_response": response,
            "_tokens": tokens,
        }

    return response


def _build_user_content(
    diff_info: dict,
    constitution: dict,
    challenger_result: dict,
    defender_result: dict,
    file_context: str,
) -> str:
    """Assemble the labeled user-content payload sent to the Oracle."""
    sections: list[str] = [
        "PROPOSED CHANGE:",
        json.dumps(diff_info, indent=2),
        "",
        "CONSTITUTION:",
        json.dumps(constitution, indent=2),
        "",
        "CHALLENGER FINDINGS:",
        json.dumps(challenger_result, indent=2),
        "",
        "DEFENDER REBUTTALS:",
        json.dumps(defender_result, indent=2),
    ]
    if file_context:
        sections.extend(["", "FILE CONTEXT:", file_context])
    return "\n".join(sections)


def _validate_oracle_response(response: dict[str, Any]) -> bool:
    """Return True if the response matches the Oracle output schema."""
    verdict: Any = response.get("verdict")
    if verdict not in _VALID_VERDICTS:
        return False

    reasoning: Any = response.get("reasoning")
    if not isinstance(reasoning, str) or not reasoning:
        return False

    confidence: Any = response.get("confidence")
    if confidence not in _VALID_CONFIDENCES:
        return False

    citations: Any = response.get("constraint_citations")
    if not isinstance(citations, list):
        return False
    if verdict == "VETO" and len(citations) == 0:
        return False
    for citation in citations:
        if not isinstance(citation, dict):
            return False
        for field in _REQUIRED_CITATION_FIELDS:
            value: Any = citation.get(field)
            if not isinstance(value, str) or not value:
                return False
        if citation["disposition"] not in _VALID_DISPOSITIONS:
            return False

    advisories: Any = response.get("advisories")
    if not isinstance(advisories, list):
        return False
    for advisory in advisories:
        if not isinstance(advisory, str) or not advisory:
            return False

    if "remediation" not in response:
        return False
    remediation: Any = response["remediation"]
    if verdict == "VETO":
        if not isinstance(remediation, str) or not remediation:
            return False
    else:  # PASS
        if remediation is not None:
            return False

    return True
