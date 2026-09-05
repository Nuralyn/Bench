"""Shared ledger statistics helpers for the CLI and the HTML viewer.

Single source of truth for how governance entries are counted, so the
terminal report (cli/commands.py cmd_stats) and the viewer banner
(utils/viewer.py) can never drift apart. Pure data transformation:
callers own all presentation.
"""

import datetime
import statistics
import sys
from pathlib import PurePosixPath
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


def scope_of_file(path: str, project_root: str) -> str:
    """GOVERNANCE_SCOPE for a C-007 file of the project, else OTHER_SCOPE.

    A file is governance when, relative to ``project_root``, its top-level
    directory is in GOVERNANCE_SCOPE_DIRS or it is a top-level file named in
    GOVERNANCE_SCOPE_FILES. Matching only the top level keeps a governed
    project's own ``src/pipeline/`` out of the governance scope; a file
    outside the project is never governance. Either separator is accepted.
    """
    normalized: str = str(path).replace("\\", "/")
    root: str = str(project_root).replace("\\", "/").rstrip("/")
    if PurePosixPath(normalized).is_absolute() or (
        len(normalized) > 1 and normalized[1] == ":"
    ):
        if root and normalized.startswith(root + "/"):
            normalized = normalized[len(root) + 1:]
        else:
            return OTHER_SCOPE
    parts: list[str] = normalized.split("/")
    if len(parts) > 1 and parts[0] in GOVERNANCE_SCOPE_DIRS:
        return GOVERNANCE_SCOPE
    if len(parts) == 1 and parts[0] in GOVERNANCE_SCOPE_FILES:
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
    Adjudicated is passed plus vetoed: chain-retirement anchors and
    published-copy sanitation records are bookkeeping, not rulings, and
    neither may inflate the denominator of a rate.
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
    return {
        "total": len(entries),
        "passed": passed,
        "vetoed": vetoed,
        "pipeline_errors": pipeline_errors,
        "anchors": anchors,
        "adjudicated": passed + vetoed,
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


def stats_by_scope(entries: list[dict], project_root: str) -> list[dict]:
    """Verdict tallies for the governance scope and everything else.

    ``project_root`` is the directory the ledger governs; see scope_of_file
    for how a change's file is classified against it. Both rows are always
    present, so a project with no pipeline changes reports zeros rather
    than a missing row.
    """
    groups: dict[str, list[dict]] = {GOVERNANCE_SCOPE: [], OTHER_SCOPE: []}
    for entry in entries:
        change: Any = entry.get("change")
        file: Any = change.get("file", "") if isinstance(change, dict) else ""
        groups[scope_of_file(str(file), project_root)].append(entry)
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
    """Per constraint over VETO entries: {"cited": n, "violated": n}.

    Both figures count vetoes, not mentions: a constraint an Oracle response
    cites twice is tallied once for that entry, and once as violated if any
    of its citations there found it so.
    """
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
        cited: set[str] = set()
        violated: set[str] = set()
        for citation in citations:
            parsed: tuple[str, bool] | None = _parse_citation(citation)
            if parsed is None:
                continue
            cid, is_violated = parsed
            cited.add(cid)
            if is_violated:
                violated.add(cid)
        for cid in cited:
            row: dict[str, int] = tallies.setdefault(
                cid, {"cited": 0, "violated": 0}
            )
            row["cited"] += 1
            if cid in violated:
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


# Prompt-cache pricing on the Anthropic API relative to the base input rate: a
# cache read bills at a tenth, a cache write (5-minute TTL) at 1.25x. A stage's
# usage record carries both counts; billed_input turns them into the number
# of uncached input tokens they cost the same as, so a cached edit and an
# uncached one are compared on price rather than on raw prompt size.
CACHE_READ_RATE: float = 0.1
CACHE_WRITE_RATE: float = 1.25

# Fields of a stage's usage record. "input" is the whole prompt, cache reads
# and writes included; the two cache fields break that out. Older entries
# carry only the first two.
_TOKEN_FIELDS: tuple[str, ...] = ("input", "output", "cache_read", "cache_creation")


def _usable_usage(tokens: Any) -> dict[str, int] | None:
    """The integer fields of a stage's usage record, or None if unusable.

    A record counts when it carries an integer "input" or "output"; bools,
    floats, strings, and None are ignored field by field, so a malformed
    value never inflates a total and never zeroes a good one beside it.
    """
    if not isinstance(tokens, dict):
        return None
    usable: dict[str, int] = {}
    for field in _TOKEN_FIELDS:
        value: Any = tokens.get(field)
        if isinstance(value, int) and not isinstance(value, bool):
            usable[field] = value
    if "input" not in usable and "output" not in usable:
        return None
    return usable


def _stage_usage(entry: dict) -> dict[str, dict[str, int]]:
    """Usable usage records for each stage of an entry, keyed by stage."""
    found: dict[str, dict[str, int]] = {}
    for stage in _STAGES:
        result: Any = entry.get(stage)
        if not isinstance(result, dict):
            continue
        usage: dict[str, int] | None = _usable_usage(
            result.get("_tokens", result.get("tokens_used"))
        )
        if usage is not None:
            found[stage] = usage
    return found


def billed_input(figures: dict[str, int]) -> int:
    """Input tokens priced at cached rates, as an uncached-token equivalent.

    The uncached part costs 1 each, a cache write CACHE_WRITE_RATE, a cache
    read CACHE_READ_RATE. An older record with no cache fields prices as
    fully uncached, which is what it was.
    """
    total: int = int(figures.get("input", 0))
    read: int = int(figures.get("cache_read", 0))
    written: int = int(figures.get("cache_creation", 0))
    uncached: int = max(total - read - written, 0)
    return round(uncached + written * CACHE_WRITE_RATE + read * CACHE_READ_RATE)


def normalized_entries(entries: list[dict]) -> int:
    """Entries in which at least one stage repaired cosmetic schema drift.

    A stage records what it repaired as ``_normalized`` (the
    _normalize_*_response functions in pipeline/). Each such response would
    otherwise have been a pipeline error and a fail-closed VETO, so this
    count beside the pipeline-error count shows how much of the judges'
    schema drift is being absorbed instead of blocking edits.
    """
    count: int = 0
    for entry in entries:
        for stage in _STAGES:
            result: Any = entry.get(stage)
            if isinstance(result, dict) and result.get("_normalized"):
                count += 1
                break
    return count


def tokens_by_stage(entries: list[dict]) -> dict[str, dict[str, int]]:
    """Token totals per stage and overall.

    Returns {stage: {"input", "output", "cache_read", "cache_creation",
    "billed_input", "entries"}} for each of _STAGES plus "total". A stage's
    ``_tokens`` (or the older ``tokens_used``) record is read and only
    integer fields are counted; "entries" is how many entries recorded a
    usable figure, so an average is total / entries. "billed_input" is the
    stage's input priced at cached rates (see billed_input).
    """
    totals: dict[str, dict[str, int]] = {
        stage: dict.fromkeys(_TOKEN_FIELDS, 0) | {"entries": 0}
        for stage in _STAGES
    }
    entries_with_tokens: int = 0
    for entry in entries:
        usage_by_stage: dict[str, dict[str, int]] = _stage_usage(entry)
        for stage, usage in usage_by_stage.items():
            for field, value in usage.items():
                totals[stage][field] += value
            totals[stage]["entries"] += 1
        if usage_by_stage:
            entries_with_tokens += 1
    totals["total"] = {
        field: sum(totals[stage][field] for stage in _STAGES)
        for field in _TOKEN_FIELDS
    } | {"entries": entries_with_tokens}
    for figures in totals.values():
        figures["billed_input"] = billed_input(figures)
    return totals


def tokens_per_entry(entries: list[dict]) -> dict[str, float | int]:
    """Distribution of total tokens (input plus output, all stages) per entry.

    Returns {"entries", "median", "p90"} over entries that recorded any
    usable token figure, read the same way tokens_by_stage reads them, so
    the two cannot disagree about what counts. This is the figure the
    README quotes as the median cost of a governed edit; cli stats and the
    viewer print it so the quoted number can be reproduced.
    """
    totals: list[float] = []
    for entry in entries:
        usage_by_stage: dict[str, dict[str, int]] = _stage_usage(entry)
        if usage_by_stage:
            totals.append(
                float(
                    sum(
                        usage.get("input", 0) + usage.get("output", 0)
                        for usage in usage_by_stage.values()
                    )
                )
            )
    return _distribution_summary(totals)


def billed_tokens_per_entry(entries: list[dict]) -> dict[str, float | int]:
    """tokens_per_entry with each stage's input priced at cached rates.

    Same entries, same shape; the only difference is that cache reads and
    writes count at CACHE_READ_RATE and CACHE_WRITE_RATE instead of 1. On a
    ledger with no cache fields it equals tokens_per_entry exactly.
    """
    totals: list[float] = []
    for entry in entries:
        usage_by_stage: dict[str, dict[str, int]] = _stage_usage(entry)
        if usage_by_stage:
            totals.append(
                float(
                    sum(
                        billed_input(usage) + usage.get("output", 0)
                        for usage in usage_by_stage.values()
                    )
                )
            )
    return _distribution_summary(totals)


def _stage_seconds(entry: dict) -> dict[str, float]:
    """Per-stage wall time recorded on ``entry``, stage name to seconds.

    Reads each stage's ``_seconds`` (written by pipeline/runner.py beside
    ``_tokens``). Only a non-negative int or float counts; a missing,
    negative, boolean, or non-numeric figure is left out rather than
    treated as zero, so entries older than the timing field do not drag a
    median toward nothing. A skipped Defender records an explicit 0.0 and
    is counted, since zero really is what it cost.
    """
    seconds: dict[str, float] = {}
    for stage in _STAGES:
        result: Any = entry.get(stage)
        if not isinstance(result, dict):
            continue
        value: Any = result.get("_seconds")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        if value < 0:
            continue
        seconds[stage] = float(value)
    return seconds


def _percentile(values: list[float], percent: int) -> float:
    """Nearest-rank percentile of ``values`` (already sorted, non-empty).

    ``percent`` is an integer such as 90. The rank is ceil(n * percent / 100)
    in integer arithmetic, so no float rounding can move it by one.
    """
    rank: int = max(1, -(-len(values) * percent // 100))
    return values[min(rank, len(values)) - 1]


def _distribution_summary(values: list[float]) -> dict[str, float | int]:
    """{"entries", "median", "p90"} over ``values``; zeros when empty."""
    if not values:
        return {"entries": 0, "median": 0.0, "p90": 0.0}
    ordered: list[float] = sorted(values)
    return {
        "entries": len(ordered),
        "median": round(statistics.median(ordered), 3),
        "p90": round(_percentile(ordered, 90), 3),
    }


def seconds_by_stage(entries: list[dict]) -> dict[str, dict[str, float | int]]:
    """Wall-time distribution per stage and per entry.

    Returns {stage: {"entries", "median", "p90"}} for each of _STAGES plus
    "total", where an entry's total is the sum of every stage figure it
    recorded. "entries" is how many entries carried a usable figure for
    that row, so the medians are over recorded timings only, never over
    the whole ledger.
    """
    per_stage: dict[str, list[float]] = {stage: [] for stage in _STAGES}
    totals: list[float] = []
    for entry in entries:
        seconds: dict[str, float] = _stage_seconds(entry)
        for stage, value in seconds.items():
            per_stage[stage].append(value)
        if seconds:
            totals.append(sum(seconds.values()))
    summary: dict[str, dict[str, float | int]] = {
        stage: _distribution_summary(per_stage[stage]) for stage in _STAGES
    }
    summary["total"] = _distribution_summary(totals)
    return summary


def latency_by_week(entries: list[dict]) -> list[dict]:
    """Per-entry total wall time per ISO week, oldest first.

    Each row is a _distribution_summary dict plus "week". Only entries that
    recorded at least one stage timing contribute, and weeks with none are
    omitted rather than shown as zero, so the table cannot imply a verdict
    was instant when it was merely unmeasured.
    """
    groups: dict[str, list[float]] = {}
    for entry in entries:
        seconds: dict[str, float] = _stage_seconds(entry)
        if not seconds:
            continue
        groups.setdefault(week_of(entry.get("timestamp")), []).append(
            sum(seconds.values())
        )
    rows: list[dict] = []
    for label in sorted(groups):
        row: dict = _distribution_summary(groups[label])
        row["week"] = label
        rows.append(row)
    return rows


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
