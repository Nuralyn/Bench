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

from pipeline.constitution import build_cached_prefix, build_context_section
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
}

The "position" field must be exactly one of REBUT, CONCEDE, or MITIGATE.
No other value is valid. When you simply agree with a finding — including
an observation the Challenger itself marked as context-only or
non-violating — use CONCEDE. Do not invent positions such as CONFIRM or
AGREE; an out-of-schema position invalidates the entire response and is
recorded as a pipeline error."""


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

    # The prompt is the constitution (cached prefix), the repository context
    # (cached where a provider can do so at user priority), then this
    # edit's content. All built fresh from this run's snapshot; the cache
    # holds bytes, not a stale snapshot, and a changed constitution renders
    # different bytes and misses it.
    cached_prefix: str = build_cached_prefix(constitution)
    cached_context: str = build_context_section(file_context)
    user_content: str = _build_user_content(diff_info, challenger_result)

    response: dict[str, Any] = call_model(
        DEFENDER_MODEL,
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

    # Repair cosmetic drift before validating and record what was repaired
    # on the result, so the ledger entry shows it. _normalize_defender_
    # response (below) makes two repairs and no others: a digit-string
    # finding_index becomes the integer, and a position that is an alias of
    # CONCEDE (CONFIRM, CONFIRM_CLEAR, AGREE) becomes CONCEDE. Every other
    # position, index, or missing field still fails closed in the validator.
    # "_normalized" is reserved for this stage's own record. The validator
    # tolerates unknown keys, so a model-authored one is dropped first;
    # otherwise it would reach the ledger and count as a repair.
    response.pop("_normalized", None)
    notes: list[str] = _normalize_defender_response(response)
    if notes:
        response["_normalized"] = notes

    if not _validate_defender_response(response):
        return {
            "status": "PIPELINE_ERROR",
            "error": "INVALID_DEFENDER_RESPONSE",
            "raw_response": response,
            "_tokens": tokens,
        }

    return response


def _build_user_content(diff_info: dict, challenger_result: dict) -> str:
    """Assemble the per-edit part of the Defender's prompt.

    The change and the Challenger's findings. The constitution and the
    repository context come first as the cached prefix
    (pipeline.constitution.build_cached_prefix), the same rendering the
    Challenger and Oracle receive, so all three stages argue from identical
    constitutional text.
    """
    return "\n".join(
        [
            "PROPOSED CHANGE:",
            json.dumps(diff_info, indent=2),
            "",
            "CHALLENGER FINDINGS:",
            json.dumps(challenger_result, indent=2),
        ]
    )


# Cosmetic drift the operational ledger has recorded in otherwise sound
# Defender responses (INVALID_DEFENDER_RESPONSE on 2026-07-31, twice, and
# 2026-08-04, twice), each a fail-closed VETO recorded as a pipeline error,
# not a ruling. The positions here are the ones the system prompt above
# already warns against, and every one of them is a way of agreeing with a
# finding, which is what CONCEDE means. No alias maps to REBUT or MITIGATE:
# a word that could mean disagreement is never guessed at.
_POSITION_ALIASES: dict[str, str] = {
    "CONFIRM": "CONCEDE",
    "CONFIRM_CLEAR": "CONCEDE",
    "AGREE": "CONCEDE",
}


def _normalize_defender_response(response: dict[str, Any]) -> list[str]:
    """Repair cosmetic drift in a Defender response in place; return notes.

    Two repairs, neither changing the argument made: a ``finding_index``
    given as a digit string becomes the integer, and a position in
    _POSITION_ALIASES becomes CONCEDE. Any other position, a non-numeric
    or negative index, and a missing argument or summary are left for
    _validate_defender_response to fail closed, exactly as before.

    This does not weaken enforcement. A rebuttal's position is an input to
    the Oracle, which reads the argument text and rules on the merits; a
    response the validator rejected for spelling CONCEDE as CONFIRM was
    never adjudicated at all, and the VETO it produced was a pipeline
    error. Mapping an agreement word to CONCEDE sends the Oracle the same
    argument with the position the schema meant, whether the response also
    carries a genuine REBUT or not.

    run_defender records the returned notes on the result as
    ``_normalized`` (the same pattern run_challenger and run_oracle use) so
    the ledger entry shows what was repaired. tests/test_defender.py
    NormalizeDefenderResponseTests covers each repair, the untouched clean
    response, and the fail-closed cases (REFUTE, REJECT, DISPUTE, PARTIAL,
    a non-numeric index, a missing argument) end to end through
    run_defender.
    """
    notes: list[str] = []
    rebuttals: Any = response.get("rebuttals")
    if not isinstance(rebuttals, list):
        return notes
    for index, rebuttal in enumerate(rebuttals):
        if not isinstance(rebuttal, dict):
            continue
        finding_index: Any = rebuttal.get("finding_index")
        # ASCII decimal digits only, and short: str.isdigit() is also true
        # of superscripts and circled digits that int() rejects, and int()
        # refuses very long digit strings, and an exception here would skip
        # the PIPELINE_ERROR receipt the validator writes.
        if (
            isinstance(finding_index, str)
            and finding_index.strip().isascii()
            and finding_index.strip().isdecimal()
            and len(finding_index.strip()) <= 9
        ):
            rebuttal["finding_index"] = int(finding_index.strip())
            notes.append(
                f"rebuttal {index}: finding_index {finding_index!r} recorded as "
                f"{rebuttal['finding_index']}"
            )
        position: Any = rebuttal.get("position")
        if isinstance(position, str) and position.upper() in _POSITION_ALIASES:
            rebuttal["position"] = _POSITION_ALIASES[position.upper()]
            notes.append(
                f"rebuttal {index}: position {position!r} recorded as "
                f"{rebuttal['position']!r}"
            )
    return notes


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
        if not _is_valid_rebuttal(rebuttal):
            return False

    return True


def _is_valid_rebuttal(rebuttal: Any) -> bool:
    """Return True if a single rebuttal entry matches the schema."""
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
