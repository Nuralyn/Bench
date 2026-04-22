"""Orchestrator for the Bench governance pipeline.

Loads the constitution snapshot, drives Challenger -> Defender -> Oracle in
sequence, and returns a consolidated result dict for the hook to translate
into a permissionDecision and for the ledger to append.

Fail-open policy:
  * Any stage returning PIPELINE_ERROR short-circuits to a PASS verdict.
  * A missing or malformed constitution short-circuits to a PASS verdict.
  * The returned dict carries pipeline_error=True whenever this happens so
    the hook and ledger can flag the incident for the developer. A broken
    governance layer must not block work.

Optimization:
  * Challenger CLEAR skips the Defender (saves one model call). A synthetic
    CONFIRM_CLEAR defender result is fabricated so the Oracle sees a
    consistent three-input payload.
"""

from typing import Any

from pipeline.challenger import run_challenger
from pipeline.constitution import (
    ConstitutionError,
    load_constitution_snapshot,
)
from pipeline.defender import run_defender
from pipeline.oracle import run_oracle


_CONSTITUTION_PATH: str = "bench.json"


def run_governance_pipeline(
    tool_name: str,
    tool_input: dict,
    diff_info: dict,
) -> dict[str, Any]:
    """Run the full Challenger -> Defender -> Oracle pipeline.

    tool_name and tool_input are accepted for signature uniformity with the
    hook and future ledger recording; this function operates on diff_info.
    """
    del tool_name, tool_input  # reserved for ledger context; not used here

    accumulated: dict[str, int] = {"input": 0, "output": 0}

    try:
        constitution, constitution_hash = load_constitution_snapshot(
            _CONSTITUTION_PATH
        )
    except ConstitutionError as e:
        return {
            "verdict": "PASS",
            "reason": f"Constitution load failure — failing open: {e}",
            "remediation": None,
            "pipeline_error": True,
            "_tokens": accumulated,
        }

    challenger_result: dict[str, Any] = run_challenger(
        diff_info, constitution, constitution_hash
    )
    _accumulate_tokens(accumulated, challenger_result.get("_tokens"))
    if challenger_result.get("status") == "PIPELINE_ERROR":
        return {
            "verdict": "PASS",
            "reason": "Challenger pipeline error — failing open",
            "remediation": None,
            "challenger": challenger_result,
            "constitution_hash": constitution_hash,
            "pipeline_error": True,
            "_tokens": accumulated,
        }

    if challenger_result.get("status") == "CLEAR":
        defender_result: dict[str, Any] = {
            "status": "CONFIRM_CLEAR",
            "rebuttals": [],
            "summary": "Challenger found no issues.",
            "_tokens": {"input": 0, "output": 0},
        }
    else:
        defender_result = run_defender(
            diff_info, constitution, constitution_hash, challenger_result
        )
        _accumulate_tokens(accumulated, defender_result.get("_tokens"))
        if defender_result.get("status") == "PIPELINE_ERROR":
            return {
                "verdict": "PASS",
                "reason": "Defender pipeline error — failing open",
                "remediation": None,
                "challenger": challenger_result,
                "defender": defender_result,
                "constitution_hash": constitution_hash,
                "pipeline_error": True,
                "_tokens": accumulated,
            }

    oracle_result: dict[str, Any] = run_oracle(
        diff_info,
        constitution,
        constitution_hash,
        challenger_result,
        defender_result,
    )
    _accumulate_tokens(accumulated, oracle_result.get("_tokens"))
    if oracle_result.get("status") == "PIPELINE_ERROR":
        return {
            "verdict": "PASS",
            "reason": "Oracle pipeline error — failing open",
            "remediation": None,
            "challenger": challenger_result,
            "defender": defender_result,
            "oracle": oracle_result,
            "constitution_hash": constitution_hash,
            "pipeline_error": True,
            "_tokens": accumulated,
        }

    return {
        "verdict": oracle_result["verdict"],
        "reason": oracle_result["reasoning"],
        "remediation": oracle_result["remediation"],
        "challenger": challenger_result,
        "defender": defender_result,
        "oracle": oracle_result,
        "constitution_hash": constitution_hash,
        "_tokens": accumulated,
    }


def _accumulate_tokens(
    accumulated: dict[str, int],
    stage_tokens: Any,
) -> None:
    """Fold a stage's {input, output} token counts into the accumulator.

    A malformed or missing _tokens field is treated as zero rather than
    raising: token accounting is observational, never a reason to block
    the verdict path."""
    if not isinstance(stage_tokens, dict):
        return
    inp: Any = stage_tokens.get("input", 0)
    out: Any = stage_tokens.get("output", 0)
    if isinstance(inp, int) and not isinstance(inp, bool):
        accumulated["input"] += inp
    if isinstance(out, int) and not isinstance(out, bool):
        accumulated["output"] += out
