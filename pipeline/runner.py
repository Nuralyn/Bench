"""Orchestrator for the Bench governance pipeline.

Loads the constitution snapshot, drives Challenger -> Defender -> Oracle in
sequence, appends a hash-chained receipt to the ledger, and returns a
consolidated result dict for the hook to translate into a permissionDecision.

Fail-open policy:
  * Any stage returning PIPELINE_ERROR short-circuits to a PASS verdict.
  * A missing or malformed constitution short-circuits to a PASS verdict.
  * The returned dict carries pipeline_error=True whenever this happens so
    the hook and ledger can flag the incident for the developer. A broken
    governance layer must not block work.

Ledger policy:
  * Every exit path records a ledger entry via append_entry before
    returning — PASS, VETO, and fail-open alike. Fail-open entries carry
    pipeline_error=True so the evidence chain distinguishes them from
    adjudicated verdicts.
  * If the ledger write itself raises, the exception is logged to stderr
    and the verdict is returned anyway. A broken ledger must not block
    the developer (same motivation as the fail-open verdict policy).

Optimization:
  * Challenger CLEAR skips the Defender (saves one model call). A synthetic
    CONFIRM_CLEAR defender result is fabricated so the Oracle sees a
    consistent three-input payload.
"""

import sys
import traceback
from pathlib import Path
from typing import Any

from ledger.chain import append_entry
from pipeline.challenger import run_challenger
from pipeline.constitution import (
    ConstitutionError,
    load_constitution_snapshot,
)
from pipeline.defender import run_defender
from pipeline.oracle import run_oracle

_BENCH_ROOT: Path = Path(__file__).resolve().parent.parent
_CONSTITUTION_PATH: str = str(_BENCH_ROOT / "bench.json")


def run_governance_pipeline(
    tool_name: str,
    tool_input: dict,
    diff_info: dict,
) -> dict[str, Any]:
    """Run the full Challenger -> Defender -> Oracle pipeline.

    tool_name and diff_info are used to tag the ledger entry. tool_input
    is accepted for signature uniformity with the hook but is not recorded
    separately — diff_info already carries the extracted change payload.
    """
    del tool_input  # accepted for signature uniformity; not recorded

    accumulated: dict[str, int] = {"input": 0, "output": 0}

    try:
        constitution, constitution_hash = load_constitution_snapshot(
            _CONSTITUTION_PATH
        )
    except ConstitutionError as e:
        return _finalize(
            {
                "verdict": "PASS",
                "reason": f"Constitution load failure — failing open: {e}",
                "remediation": None,
                "pipeline_error": True,
                "_tokens": accumulated,
            },
            tool_name,
            diff_info,
        )

    challenger_result: dict[str, Any] = run_challenger(
        diff_info, constitution, constitution_hash
    )
    _accumulate_tokens(accumulated, challenger_result.get("_tokens"))
    if challenger_result.get("status") == "PIPELINE_ERROR":
        return _finalize(
            {
                "verdict": "PASS",
                "reason": "Challenger pipeline error — failing open",
                "remediation": None,
                "challenger": challenger_result,
                "constitution_hash": constitution_hash,
                "pipeline_error": True,
                "_tokens": accumulated,
            },
            tool_name,
            diff_info,
        )

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
    if challenger_result.get("status") != "CLEAR":
        if defender_result.get("status") == "PIPELINE_ERROR":
            return _finalize(
                {
                    "verdict": "PASS",
                    "reason": "Defender pipeline error — failing open",
                    "remediation": None,
                    "challenger": challenger_result,
                    "defender": defender_result,
                    "constitution_hash": constitution_hash,
                    "pipeline_error": True,
                    "_tokens": accumulated,
                },
                tool_name,
                diff_info,
            )

    oracle_result: dict[str, Any] = run_oracle(
        diff_info,
        constitution,
        constitution_hash,
        challenger_result,
        defender_result,
    )
    _accumulate_tokens(accumulated, oracle_result.get("_tokens"))
    if oracle_result.get("status") == "PIPELINE_ERROR":
        return _finalize(
            {
                "verdict": "PASS",
                "reason": "Oracle pipeline error — failing open",
                "remediation": None,
                "challenger": challenger_result,
                "defender": defender_result,
                "oracle": oracle_result,
                "constitution_hash": constitution_hash,
                "pipeline_error": True,
                "_tokens": accumulated,
            },
            tool_name,
            diff_info,
        )

    return _finalize(
        {
            "verdict": oracle_result["verdict"],
            "reason": oracle_result["reasoning"],
            "remediation": oracle_result["remediation"],
            "violated_constraints": _violated_constraint_ids(oracle_result),
            "advisories": oracle_result.get("advisories", []),
            "challenger": challenger_result,
            "defender": defender_result,
            "oracle": oracle_result,
            "constitution_hash": constitution_hash,
            "_tokens": accumulated,
        },
        tool_name,
        diff_info,
    )


def _violated_constraint_ids(oracle_result: dict[str, Any]) -> list[str]:
    """Constraint IDs the Oracle cited as VIOLATED, in citation order.

    Feeds the hook's documented 'BENCH VETO [C-XXX]: ...' reason format.
    Defensive against malformed citations: anything that is not a dict
    with a non-empty string constraint_id is skipped (the Oracle schema
    validation should prevent that, but the hook must not depend on it).
    """
    ids: list[str] = []
    citations: Any = oracle_result.get("constraint_citations")
    if not isinstance(citations, list):
        return ids
    for citation in citations:
        if (
            isinstance(citation, dict)
            and citation.get("disposition") == "VIOLATED"
        ):
            cid: Any = citation.get("constraint_id")
            if isinstance(cid, str) and cid and cid not in ids:
                ids.append(cid)
    return ids


def _finalize(
    result: dict[str, Any],
    tool_name: str,
    diff_info: dict,
) -> dict[str, Any]:
    """Attach change context, record to the ledger, and return the result.

    The ledger append is wrapped in a best-effort guard: any exception is
    logged with a full traceback to stderr but is swallowed so the verdict
    still reaches the hook. A broken ledger must not block the developer
    (constitutional fail-open policy).
    """
    result["change"] = {
        "file": diff_info.get("file_path", "unknown"),
        "tool": tool_name,
        "diff_summary": diff_info,
    }
    try:
        append_entry(result)
    except Exception as e:
        print(
            f"[bench runner] ledger append failed; returning verdict "
            f"without receipt: {e}",
            file=sys.stderr,
        )
        traceback.print_exc(file=sys.stderr)
    return result


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
