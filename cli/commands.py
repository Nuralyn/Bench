"""Implementations of the four Bench CLI commands.

Each command returns an int exit code (0 on success, 1 on failure) and
prints a human-readable summary to stdout. Error diagnostics go to stderr
so the two streams can be separated in scripts.

These commands are read-only reports over the ledger and constitution —
nothing here mutates state, so there is no risk of collision with the
governance pipeline running in parallel.
"""

import sys
from typing import Any

from ledger.chain import load_ledger
from ledger.verify import verify_chain
from pipeline.constitution import (
    ConstitutionError,
    load_constitution_snapshot,
)

_HASH_PREFIX_LEN: int = 12
_DEFAULT_LEDGER_TAIL: int = 10
_RULE_PREVIEW_LEN: int = 100


def cmd_verify() -> int:
    """Validate the ledger hash chain and print a pass/fail summary."""
    result: dict[str, Any] = verify_chain()

    if result.get("valid"):
        entries: int = int(result.get("entries", 0))
        if entries == 0:
            print("Ledger: EMPTY (nothing to verify)")
            return 0
        print("Ledger: VALID")
        print(f"  entries      : {entries}")
        print(f"  first entry  : {result.get('first_entry', '-')}")
        print(f"  last entry   : {result.get('last_entry', '-')}")
        print(f"  genesis hash : {result.get('genesis_hash', '-')}")
        print(f"  latest hash  : {result.get('latest_hash', '-')}")
        return 0

    print("Ledger: INVALID", file=sys.stderr)
    print(
        f"  failure type    : {result.get('failure_type', '-')}",
        file=sys.stderr,
    )
    print(
        f"  failure index   : {result.get('failure_index', '-')}",
        file=sys.stderr,
    )
    print(
        f"  entries checked : {result.get('entries_checked', 0)}",
        file=sys.stderr,
    )
    print(f"  expected        : {result.get('expected', '-')}", file=sys.stderr)
    print(f"  found           : {result.get('found', '-')}", file=sys.stderr)
    print(f"  message         : {result.get('message', '-')}", file=sys.stderr)
    return 1


def cmd_ledger(show_all: bool = False, vetoes_only: bool = False) -> int:
    """Print ledger entries (default: last 10)."""
    entries: list[dict] = load_ledger()

    if not entries:
        print("Ledger is empty.")
        return 0

    filtered: list[dict] = entries
    if vetoes_only:
        filtered = [
            e for e in filtered
            if isinstance(e.get("oracle"), dict)
            and e["oracle"].get("verdict") == "VETO"
        ]

    if not filtered:
        print("No entries match the filter.")
        return 0

    if not show_all:
        filtered = filtered[-_DEFAULT_LEDGER_TAIL:]

    shown: int = len(filtered)
    total: int = len(entries)
    scope: str = "all" if show_all else f"last {shown}"
    if vetoes_only:
        scope = f"{scope}, vetoes only"
    print(f"Ledger entries ({scope} of {total} total):")
    print()

    for entry in filtered:
        _print_entry_line(entry)

    return 0


def cmd_stats() -> int:
    """Print a governance summary: counts, top citation, integrity."""
    entries: list[dict] = load_ledger()
    total: int = len(entries)

    if total == 0:
        print("Ledger is empty. No governance statistics to report.")
        return 0

    passed: int = 0
    vetoed: int = 0
    pipeline_errors: int = 0
    citation_counts: dict[str, int] = {}

    for entry in entries:
        oracle: Any = entry.get("oracle")
        oracle_dict: dict = oracle if isinstance(oracle, dict) else {}
        verdict: Any = oracle_dict.get("verdict")

        if _entry_has_pipeline_error(entry):
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

    most_cited: tuple[str, int] | None = None
    if citation_counts:
        most_cited = max(citation_counts.items(), key=lambda kv: kv[1])

    latest_cons_hash: str = str(entries[-1].get("constitution_hash", "-"))
    verify: dict = verify_chain()
    if verify.get("valid"):
        integrity: str = "VALID"
    else:
        integrity = f"INVALID ({verify.get('failure_type', 'unknown')})"

    print("Bench Governance Statistics")
    print("=" * 40)
    print(f"Total governed changes : {total}")
    print(f"Passed                 : {passed} ({_pct(passed, total)})")
    print(f"Vetoed                 : {vetoed} ({_pct(vetoed, total)})")
    print(f"Pipeline errors        : {pipeline_errors}")
    if most_cited is not None:
        print(
            f"Most cited constraint  : {most_cited[0]} "
            f"({most_cited[1]} veto(es))"
        )
    else:
        print("Most cited constraint  : n/a")
    print(f"Constitution hash      : {_short_hash(latest_cons_hash, 16)}")
    print(f"Ledger integrity       : {integrity}")

    return 0 if verify.get("valid") else 1


def cmd_constitution() -> int:
    """Print the current constitution: hash, constraint list, rules."""
    try:
        constitution, constitution_hash = load_constitution_snapshot()
    except ConstitutionError as e:
        print(f"[bench cli] constitution load failed: {e}", file=sys.stderr)
        return 1

    name: str = str(constitution.get("constitution", "-"))
    version: Any = constitution.get("version", "-")
    constraints: Any = constitution.get("constraints", [])
    constraint_list: list[dict] = (
        constraints if isinstance(constraints, list) else []
    )

    print(f"Constitution : {name} v{version}")
    print(f"Hash         : {constitution_hash}")
    print(f"Constraints  : {len(constraint_list)}")
    print("=" * 40)

    for constraint in constraint_list:
        if not isinstance(constraint, dict):
            continue
        cid: str = str(constraint.get("id", "-"))
        cname: str = str(constraint.get("name", "-"))
        severity: str = str(constraint.get("severity", "-")).upper()
        rule: str = str(constraint.get("rule", ""))
        if len(rule) > _RULE_PREVIEW_LEN:
            rule = rule[: _RULE_PREVIEW_LEN - 3] + "..."
        print()
        print(f"  {cid}  [{severity:7}]  {cname}")
        print(f"           {rule}")

    return 0


def _print_entry_line(entry: dict) -> None:
    timestamp: str = str(entry.get("timestamp", "-"))
    change: Any = entry.get("change")
    file: str = "-"
    if isinstance(change, dict):
        file = str(change.get("file", "-"))

    oracle: Any = entry.get("oracle")
    oracle_dict: dict = oracle if isinstance(oracle, dict) else {}
    verdict: str = str(oracle_dict.get("verdict") or "").strip()
    if not verdict:
        if _entry_has_pipeline_error(entry):
            verdict = "FAIL-OPEN"
        else:
            verdict = "-"

    entry_hash: str = str(entry.get("entry_hash", "-"))
    short: str = _short_hash(entry_hash, _HASH_PREFIX_LEN)

    print(f"  {timestamp}  {verdict:10}  {file}  [{short}]")

    if verdict == "VETO":
        citations: Any = oracle_dict.get("constraint_citations")
        if isinstance(citations, list) and citations:
            cite_str: str = ", ".join(str(c) for c in citations if c)
            if cite_str:
                print(f"      citations: {cite_str}")


def _entry_has_pipeline_error(entry: dict) -> bool:
    for stage in ("challenger", "defender", "oracle"):
        stage_result: Any = entry.get(stage)
        if (
            isinstance(stage_result, dict)
            and stage_result.get("status") == "PIPELINE_ERROR"
        ):
            return True
    return False


def _short_hash(value: str, length: int) -> str:
    if not value or value == "-":
        return "-"
    if len(value) <= length:
        return value
    return value[:length] + "..."


def _pct(part: int, total: int) -> str:
    if total <= 0:
        return "0.0%"
    return f"{part / total * 100:.1f}%"
