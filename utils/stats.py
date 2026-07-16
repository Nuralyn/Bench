"""Shared ledger statistics helpers for the CLI and the HTML viewer.

Single source of truth for how governance entries are counted, so the
terminal report (cli/commands.py cmd_stats) and the viewer banner
(utils/viewer.py) can never drift apart. Pure data transformation:
callers own all presentation.
"""

import sys
from typing import Any


def entry_has_pipeline_error(entry: dict) -> bool:
    """True if the entry recorded a pipeline error.

    Checks the top-level ``pipeline_error`` flag (set on fail-closed error
    VETOs, including a constitution-load failure that runs no stage) and,
    for older entries and stage-level errors, any stage with a
    PIPELINE_ERROR status.
    """
    if entry.get("pipeline_error"):
        return True
    for stage in ("challenger", "defender", "oracle"):
        stage_result: Any = entry.get(stage)
        if (
            isinstance(stage_result, dict)
            and stage_result.get("status") == "PIPELINE_ERROR"
        ):
            return True
    return False


def entry_verdict(entry: dict) -> str | None:
    """The authoritative verdict for a ledger entry.

    Prefers the top-level ``verdict`` recorded by append_entry (present on
    fail-closed error VETOs, which never produce an oracle stage), then the
    oracle stage verdict (older entries written before the top-level field
    existed), else None.
    """
    top: Any = entry.get("verdict")
    if isinstance(top, str) and top:
        return top
    oracle: Any = entry.get("oracle")
    if isinstance(oracle, dict):
        v: Any = oracle.get("verdict")
        if isinstance(v, str) and v:
            return v
    return None


def pct(part: int, total: int) -> str:
    """Format part/total as a one-decimal percentage string."""
    if total <= 0:
        return "0.0%"
    return f"{part / total * 100:.1f}%"


def compute_ledger_stats(entries: list[dict]) -> dict:
    """Aggregate verdict counts and constraint citations over the ledger.

    Returns {"total", "passed", "vetoed", "pipeline_errors", "most_cited"}
    where most_cited is a (constraint_id, count) tuple over VETO citations,
    or None when no veto cited a constraint. Citation entries may be plain
    strings or {"constraint_id": ...} dicts; anything else is logged to
    stderr and skipped.
    """
    total: int = len(entries)
    passed: int = 0
    vetoed: int = 0
    pipeline_errors: int = 0
    citation_counts: dict[str, int] = {}

    for entry in entries:
        verdict: Any = entry_verdict(entry)
        oracle: Any = entry.get("oracle")
        oracle_dict: dict = oracle if isinstance(oracle, dict) else {}

        if entry_has_pipeline_error(entry):
            pipeline_errors += 1

        if verdict == "PASS":
            passed += 1
        elif verdict == "VETO":
            vetoed += 1
            citations: Any = oracle_dict.get("constraint_citations")
            if isinstance(citations, list):
                for cid in citations:
                    if isinstance(cid, str):
                        citation_counts[cid] = citation_counts.get(cid, 0) + 1
                    elif isinstance(cid, dict):
                        raw: Any = cid.get("constraint_id")
                        if isinstance(raw, str):
                            citation_counts[raw] = (
                                citation_counts.get(raw, 0) + 1
                            )
                    else:
                        print(
                            f"[bench stats] unexpected citation type: "
                            f"{type(cid).__name__}",
                            file=sys.stderr,
                        )

    most_cited: tuple[str, int] | None = None
    if citation_counts:
        most_cited = max(citation_counts.items(), key=lambda kv: kv[1])

    return {
        "total": total,
        "passed": passed,
        "vetoed": vetoed,
        "pipeline_errors": pipeline_errors,
        "most_cited": most_cited,
    }
