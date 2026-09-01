"""Shared ledger statistics helpers for the CLI and the HTML viewer.

Single source of truth for how governance entries are counted, so the
terminal report (cli/commands.py cmd_stats) and the viewer banner
(utils/viewer.py) can never drift apart. Pure data transformation:
callers own all presentation.
"""

import datetime
import sys
from typing import Any

from ledger.chain import ANCHOR_VERDICT


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


_STAGES: tuple[str, ...] = ("challenger", "defender", "oracle")

# Directories whose files are the governance pipeline itself (constraint
# C-007). A change under any of them is scoped "governance"; the rest of a
# project is "other".
GOVERNANCE_SCOPE_DIRS: frozenset[str] = frozenset({"pipeline", "ledger", "hooks"})
# C-007 names the constitution alongside the pipeline stages and the ledger.
GOVERNANCE_SCOPE_FILES: frozenset[str] = frozenset({"bench.json"})
GOVERNANCE_SCOPE: str = "governance"
OTHER_SCOPE: str = "other"
UNKNOWN_WEEK: str = "unknown"


def scope_of_file(path: str) -> str:
    """GOVERNANCE_SCOPE for a C-007 file, else OTHER_SCOPE.

    A file is governance when it sits under a GOVERNANCE_SCOPE_DIRS directory
    or is named in GOVERNANCE_SCOPE_FILES. Accepts relative or absolute paths
    with either separator. Only directory components are matched against the
    directory set, so a file merely named hooks.md is not governance.
    """
    parts: list[str] = str(path).replace("\\", "/").split("/")
    if any(part in GOVERNANCE_SCOPE_DIRS for part in parts[:-1]):
        return GOVERNANCE_SCOPE
    if parts[-1] in GOVERNANCE_SCOPE_FILES:
        return GOVERNANCE_SCOPE
    return OTHER_SCOPE


def week_of(timestamp: Any) -> str:
    """ISO week label (YYYY-Www) of an ISO-8601 timestamp, else UNKNOWN_WEEK."""
    try:
        day: datetime.date = datetime.date.fromisoformat(str(timestamp)[:10])
    except ValueError:
        return UNKNOWN_WEEK
    year, week, _ = day.isocalendar()
    return f"{year}-W{week:02d}"


def _tally_verdicts(entries: list[dict]) -> dict[str, int]:
    """Count verdicts over ``entries``.

    Returns {"total", "passed", "vetoed", "pipeline_errors", "anchors",
    "adjudicated"}. Fail-closed entries carry verdict VETO and a pipeline
    error, so they are counted in both vetoed and pipeline_errors.
    """
    passed: int = 0
    vetoed: int = 0
    pipeline_errors: int = 0
    anchors: int = 0
    for entry in entries:
        verdict: str | None = entry_verdict(entry)
        if entry_has_pipeline_error(entry):
            pipeline_errors += 1
        if verdict == ANCHOR_VERDICT:
            # Chain-retirement markers are ledger bookkeeping, not governed
            # changes. Counting them as adjudications would skew pass rates.
            anchors += 1
        elif verdict == "PASS":
            passed += 1
        elif verdict == "VETO":
            vetoed += 1
    total: int = len(entries)
    return {
        "total": total,
        "passed": passed,
        "vetoed": vetoed,
        "pipeline_errors": pipeline_errors,
        "anchors": anchors,
        "adjudicated": total - anchors,
    }


def _group_tallies(groups: dict[str, list[dict]], key: str) -> list[dict]:
    """One _tally_verdicts row per group, labelled under ``key``, sorted."""
    rows: list[dict] = []
    for label in sorted(groups):
        row: dict = _tally_verdicts(groups[label])
        row[key] = label
        rows.append(row)
    return rows


def stats_by_week(entries: list[dict]) -> list[dict]:
    """Verdict tallies per ISO week of entry timestamp, oldest first.

    Each row is a _tally_verdicts dict plus "week". Entries whose timestamp
    does not parse land in the UNKNOWN_WEEK bucket rather than vanishing.
    """
    groups: dict[str, list[dict]] = {}
    for entry in entries:
        groups.setdefault(week_of(entry.get("timestamp")), []).append(entry)
    return _group_tallies(groups, "week")


def stats_by_scope(entries: list[dict]) -> list[dict]:
    """Verdict tallies for the governance scope and everything else.

    Both rows are always present, so a project with no pipeline changes
    reports zeros rather than a missing row.
    """
    groups: dict[str, list[dict]] = {GOVERNANCE_SCOPE: [], OTHER_SCOPE: []}
    for entry in entries:
        change: Any = entry.get("change")
        file: Any = change.get("file", "") if isinstance(change, dict) else ""
        groups[scope_of_file(str(file))].append(entry)
    return _group_tallies(groups, "scope")


def _parse_citation(citation: Any) -> tuple[str, bool] | None:
    """(constraint_id, violated) for one citation, or None to skip it.

    Citations are plain strings (legacy) or {"constraint_id", "disposition"}
    dicts. A string, or a dict with no disposition, names the constraint as
    the reason for the veto and counts as violated. Anything else is logged
    to stderr and skipped.
    """
    if isinstance(citation, str):
        return citation, True
    if isinstance(citation, dict):
        raw: Any = citation.get("constraint_id")
        if not isinstance(raw, str):
            return None
        disposition: Any = citation.get("disposition")
        return raw, disposition is None or disposition == "VIOLATED"
    print(
        f"[bench stats] unexpected citation type: {type(citation).__name__}",
        file=sys.stderr,
    )
    return None


def _citation_tallies(entries: list[dict]) -> dict[str, dict[str, int]]:
    """Per constraint over VETO entries: {"cited": n, "violated": n}."""
    tallies: dict[str, dict[str, int]] = {}
    for entry in entries:
        if entry_verdict(entry) != "VETO":
            continue
        oracle: Any = entry.get("oracle")
        citations: Any = (
            oracle.get("constraint_citations") if isinstance(oracle, dict) else None
        )
        if not isinstance(citations, list):
            continue
        for citation in citations:
            parsed: tuple[str, bool] | None = _parse_citation(citation)
            if parsed is None:
                continue
            cid, violated = parsed
            row: dict[str, int] = tallies.setdefault(
                cid, {"cited": 0, "violated": 0}
            )
            row["cited"] += 1
            if violated:
                row["violated"] += 1
    return tallies


def citations_by_constraint(entries: list[dict]) -> list[dict]:
    """Constraint citation table over VETO entries, sorted by constraint id.

    Each row is {"constraint_id", "cited", "violated"}: how many vetoes
    mentioned the constraint at all, and how many found it violated. The
    gap between the two is the Oracle clearing a constraint while vetoing
    on another.
    """
    return [
        {"constraint_id": cid, **counts}
        for cid, counts in sorted(_citation_tallies(entries).items())
    ]


def tokens_by_stage(entries: list[dict]) -> dict[str, dict[str, int]]:
    """Token totals per stage and overall.

    Returns {stage: {"input", "output", "entries"}} for each of _STAGES plus
    "total". A stage's ``_tokens`` (or the older ``tokens_used``) record is
    read and only integer fields are counted; "entries" is how many entries
    recorded a usable figure, so an average is total / entries.
    """
    totals: dict[str, dict[str, int]] = {
        stage: {"input": 0, "output": 0, "entries": 0} for stage in _STAGES
    }
    entries_with_tokens: int = 0
    for entry in entries:
        counted_entry: bool = False
        for stage in _STAGES:
            result: Any = entry.get(stage)
            if not isinstance(result, dict):
                continue
            tokens: Any = result.get("_tokens", result.get("tokens_used"))
            if not isinstance(tokens, dict):
                continue
            counted_stage: bool = False
            for field in ("input", "output"):
                value: Any = tokens.get(field)
                if isinstance(value, int) and not isinstance(value, bool):
                    totals[stage][field] += value
                    counted_stage = True
            if counted_stage:
                totals[stage]["entries"] += 1
                counted_entry = True
        if counted_entry:
            entries_with_tokens += 1
    totals["total"] = {
        "input": sum(totals[stage]["input"] for stage in _STAGES),
        "output": sum(totals[stage]["output"] for stage in _STAGES),
        "entries": entries_with_tokens,
    }
    return totals


def compute_ledger_stats(entries: list[dict]) -> dict:
    """Aggregate verdict counts and the headline constraint over the ledger.

    Returns _tally_verdicts plus "most_cited": a (constraint_id, count)
    tuple for the constraint most often found VIOLATED in a veto, or None
    when no veto found one. Citations the Oracle recorded as SATISFIED or
    NOT_APPLICABLE while vetoing on another constraint do not count; they
    are mentions, not reasons.
    """
    stats: dict = _tally_verdicts(entries)
    violated: dict[str, int] = {
        cid: counts["violated"]
        for cid, counts in _citation_tallies(entries).items()
        if counts["violated"]
    }
    most_cited: tuple[str, int] | None = None
    if violated:
        most_cited = max(violated.items(), key=lambda kv: kv[1])
    stats["most_cited"] = most_cited
    return stats
