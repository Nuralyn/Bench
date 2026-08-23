"""Implementations of the Bench CLI commands (verify, ledger, stats,
constitution, viewer, retire, audit-retirement).

Each command returns an int exit code (0 on success, 1 on failure) and
prints a human-readable summary to stdout. Error diagnostics go to stderr
so the two streams can be separated in scripts.

All but one of these are read-only reports over the ledger and constitution,
so there is no risk of collision with the governance pipeline running in
parallel. The exception is ``cmd_retire``, which archives the active chain and
opens a successor under C-008's single bounded exception to ledger
immutability. It holds no logic of its own: every decision, guard, and write
lives in ``ledger/retire.py``, and this module only parses arguments, supplies
the real ``sys.stdin.isatty`` and ``input``, and renders the result.
"""

import os
import stat
import sys
import tempfile
import webbrowser
from pathlib import Path
from typing import Any

from ledger.chain import LedgerReadError, load_ledger, resolve_ledger_path
from ledger.attestation import AttestationError, build_attestation, render
from ledger.migrate import migrate_ledger
from ledger.sanitize import SANITATION_VERDICT, audit_sanitation
from ledger.retire import (
    ANCHOR_TOOL,
    RetirementError,
    audit_retirement,
    execute_retirement,
)
from ledger.verify import verify_chain
from pipeline.constitution import (
    ConstitutionError,
    load_governing_constitution,
)
from utils.stats import (
    compute_ledger_stats,
    entry_has_pipeline_error,
    entry_verdict,
    pct,
)
from utils.viewer import generate_viewer_html

_HASH_PREFIX_LEN: int = 12
_DEFAULT_LEDGER_TAIL: int = 10
_RULE_PREVIEW_LEN: int = 100


def _display_path(raw: str) -> str:
    """Render a ledger path for terminal output, preferring a relative form.

    Now that the ledger is project-scoped, operators need to see which chain
    a command actually read. Printing the absolute path would put the user's
    home directory (and username) into any output they paste into a bug
    report, so collapse it to a CWD-relative path when the ledger sits under
    the working directory, and fall back to the absolute path otherwise.

    Mirrors the normalization idiom in ``hooks/pre-tool-use.py``.
    """
    if not raw or raw == "-":
        return "-"
    try:
        rel: str = os.path.relpath(os.path.realpath(raw), os.getcwd())
    except ValueError:
        # Windows: path on a different drive from CWD, so no relative form
        # exists. Expected control flow, not a fault.
        return raw
    except OSError as exc:
        # An unresolvable path is a genuine fault; do not hide it (C-001).
        print(
            f"[bench cli] cannot resolve ledger path {raw!r}: {exc}",
            file=sys.stderr,
        )
        return raw
    if rel == os.pardir or rel.startswith(os.pardir + os.sep):
        return raw
    return rel


def cmd_verify() -> int:
    """Validate the ledger hash chain and print a pass/fail summary."""
    result: dict[str, Any] = verify_chain()

    if result.get("valid"):
        entries: int = int(result.get("entries", 0))
        if entries == 0:
            print("Ledger: EMPTY (nothing to verify)")
            return 0
        print("Ledger: VALID")
        print(f"  ledger       : {_display_path(result.get('ledger_path', '-'))}")
        print(f"  entries      : {entries}")
        print(f"  first entry  : {result.get('first_entry', '-')}")
        print(f"  last entry   : {result.get('last_entry', '-')}")
        print(f"  genesis hash : {result.get('genesis_hash', '-')}")
        tips: Any = result.get("tips", [])
        tip_list: list[str] = tips if isinstance(tips, list) else []
        if len(tip_list) > 1:
            # A fork is a legitimate state after a git merge, not a failure.
            # Name every tip so it is visible rather than implied, and say what
            # resolves it, since a reader seeing two heads should know the next
            # governed edit reconciles them.
            print(f"  tips         : {len(tip_list)} (merged branches)")
            for tip in tip_list:
                print(f"    - {tip}")
            print("  note         : the next governed edit will name both tips")
        else:
            print(f"  latest hash  : {result.get('latest_hash', '-')}")
        print(f"  meta anchor  : {result.get('meta', '-')}")
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


def cmd_attest(
    cutoff: str | None = None,
    bench_version: str | None = None,
    out: str | None = None,
) -> int:
    """Export a public attestation for entries up to a declared cutoff.

    The operational chain stays private because it records content. This
    emits commitments instead: hashes, verdicts, and constraint citations,
    with no diff, no path, and no stage prose. It is evidence that a ruling
    happened, not proof the ruling was right, and it is not a backup, since
    nothing in it can reconstruct what was governed.

    ``--cutoff`` is required rather than defaulting to the tip. A checkpoint
    is a deliberate act, and committing the artifact appends a new entry
    that necessarily falls after the cutoff, which is what stops the export
    from chasing its own tail.
    """
    if not cutoff:
        print(
            "[bench cli] attest requires --cutoff <commitment>. A checkpoint "
            "declares a fixed boundary; it does not track the live tip.",
            file=sys.stderr,
        )
        return 1
    if not bench_version:
        print(
            "[bench cli] attest requires --bench-version <x.y.z>.",
            file=sys.stderr,
        )
        return 1

    try:
        entries: list[dict] = load_ledger()
    except LedgerReadError as exc:
        print(f"[bench cli] cannot read ledger: {exc}", file=sys.stderr)
        return 1

    try:
        document: dict[str, Any] = build_attestation(
            entries, cutoff, bench_version
        )
    except AttestationError as exc:
        print(f"[bench cli] attestation refused: {exc}", file=sys.stderr)
        return 1

    target: Path = Path(out) if out else Path("attestation.json")
    try:
        target.write_text(render(document), encoding="utf-8")
    except OSError as exc:
        print(f"[bench cli] cannot write attestation: {exc}", file=sys.stderr)
        return 1

    print("Attestation: WRITTEN")
    print(f"  file        : {_display_path(str(target))}")
    print(f"  bench version: {document['bench_version']}")
    print(f"  cutoff      : {document['cutoff_commitment']}")
    print(f"  records     : {document['record_count']}")
    unmapped: int = sum(
        r["unmapped_citation_count"] for r in document["records"]
    )
    if unmapped:
        print(f"  unmapped    : {unmapped} citation(s) excluded as non-ids")
    return 0


def cmd_audit_sanitation(backup: str | None = None) -> int:
    """Audit this chain's published-copy sanitation records.

    Read-only. It audits records; it never performs a sanitation, which is a
    human action at a plain TTY. A sanitation removes published copies of
    ledger data from a version control remote and never touches the
    authoritative chain, so this reports three things that can each fail
    alone: the record's structure, the live chain's own state, and, when the
    artifact is supplied, the encrypted backup's digest.

    Checking the live chain matters most. The record asserts that the chain
    still verifies with an unchanged genesis; an auditor that trusted that
    claim would be checking the record against itself.
    """
    try:
        entries: list[dict] = load_ledger()
    except LedgerReadError as exc:
        print(f"[bench cli] cannot read ledger: {exc}", file=sys.stderr)
        return 1

    records: list[dict] = [
        e for e in entries if e.get("verdict") == SANITATION_VERDICT
    ]
    if not records:
        print("Sanitation: NONE RECORDED")
        print("  This chain has never had published copies sanitized.")
        return 0

    artifact: Path | None = Path(backup) if backup else None
    failures: int = 0

    for record in records:
        summary: dict = (record.get("change") or {}).get("diff_summary") or {}
        result: dict[str, Any] = audit_sanitation(record, artifact)
        print(f"Sanitation: {'VALID' if result['valid'] else 'INVALID'}")
        print(f"  recorded at   : {record.get('timestamp', '-')}")
        print(f"  backup id     : {summary.get('backup_id', '-')}")
        print(f"  digest        : {summary.get('backup_digest', '-')}")
        print(f"  refs rewritten: {len(summary.get('refs') or [])}")
        print(f"  retention     : {summary.get('retention_owner', '-')}")
        if result["digest_checked"]:
            print(
                f"  backup digest : "
                f"{'matches' if result['digest_matches'] else 'DOES NOT MATCH'}"
            )
        else:
            print("  backup digest : not checked (supply the artifact path)")

        if not result["valid"]:
            failures += 1
            for defect in result["defects"]:
                print(f"  defect        : {defect}", file=sys.stderr)

    return 1 if failures else 0


def cmd_migrate_ledger() -> int:
    """Populate this clone's private chain from the pre-migration location.

    Only needed once, and only by a clone that existed before the ledger
    became private. Checking out that switch makes git delete the formerly
    tracked chain under ``ledger/``, and nothing can repopulate ``.bench/``
    from git because it is ignored by design. Without this the clone would
    resolve to an empty chain and silently open a fresh GENESIS.
    """
    result: dict[str, Any] = migrate_ledger()
    status: str = str(result.get("status", ""))

    if status in ("already_migrated", "nothing_to_migrate"):
        print(f"Migration: {status.upper()}")
        print(f"  ledger : {_display_path(str(result.get('target', '-')))}")
        print(f"  detail : {result.get('detail', '')}")
        return 0

    print(f"Migration: {status.upper()}")
    print(f"  source   : {result.get('source', '-')}")
    print(f"  ledger   : {_display_path(str(result.get('target', '-')))}")
    print(f"  files    : {result.get('files', 0)} of {result.get('expected', 0)}")
    print(f"  entries  : {result.get('entries', 0)}")
    print(f"  genesis  : {result.get('genesis_hash', '-')}")

    if status == "migrated":
        print("  verified : chain verifies at the new location")
        return 0

    print(
        "  verified : NO",
        file=sys.stderr,
    )
    print(
        f"  failure  : {result.get('failure_type') or 'incomplete restore'}",
        file=sys.stderr,
    )
    print(
        "The restored chain is incomplete or does not verify. It has been "
        "left in place for inspection rather than deleted; resolve the "
        "shortfall before making a governed edit, because appending to a "
        "partial chain compounds the break.",
        file=sys.stderr,
    )
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
            if entry_verdict(e) == "VETO"
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

    stats: dict = compute_ledger_stats(entries)
    passed: int = stats["passed"]
    vetoed: int = stats["vetoed"]
    pipeline_errors: int = stats["pipeline_errors"]
    most_cited: tuple[str, int] | None = stats["most_cited"]

    latest_cons_hash: str = str(entries[-1].get("constitution_hash", "-"))
    verify: dict = verify_chain()
    if verify.get("valid"):
        integrity: str = "VALID"
    else:
        integrity = f"INVALID ({verify.get('failure_type', 'unknown')})"

    anchors: int = stats["anchors"]
    adjudicated: int = stats["adjudicated"]

    print("Bench Governance Statistics")
    print("=" * 40)
    print(f"Total governed changes : {adjudicated}")
    print(f"Passed                 : {passed} ({pct(passed, adjudicated)})")
    print(f"Vetoed                 : {vetoed} ({pct(vetoed, adjudicated)})")
    print(f"Pipeline errors        : {pipeline_errors}")
    if anchors:
        # Chain-retirement markers (C-008). Not adjudicated changes, so they
        # sit outside the pass/veto rates above.
        print(f"Chain anchors          : {anchors}")
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
    """Print the constitution governing this project: hash, constraints, rules.

    Resolves through load_governing_constitution, the same loader the pipeline
    uses, so the auditor never displays a different constitution than the one
    enforced. Reading the core file alone would omit a project's own layer and
    any severity it raised, which is the divergence this command exists to
    prevent.
    """
    try:
        (
            constitution,
            constitution_hash,
            sources,
        ) = load_governing_constitution()
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
    for source in sources:
        if not isinstance(source, dict):
            continue
        layer: str = str(source.get("layer", "-"))
        print(
            f"  {layer:8} {source.get('path', '-')}  "
            f"{_short_hash(str(source.get('sha256', '')), 12)}"
        )
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
        origin: str = ""
        if constraint.get("severity_raised_by_project"):
            origin = "  (severity raised by project layer)"
        elif cid.startswith("P-"):
            origin = "  (project layer)"
        print()
        print(f"  {cid}  [{severity:7}]  {cname}{origin}")
        print(f"           {rule}")

    return 0


def cmd_viewer() -> int:
    """Generate the HTML ledger viewer, write it to a tempfile, open browser."""
    try:
        html_content: str = generate_viewer_html()
    except Exception as e:
        print(
            f"[bench cli] viewer generation failed: {type(e).__name__}: {e}",
            file=sys.stderr,
        )
        return 1

    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".html",
            prefix="bench-viewer-",
            delete=False,
            encoding="utf-8",
        ) as fh:
            fh.write(html_content)
            tmp_path: str = fh.name
    except OSError as e:
        print(f"[bench cli] viewer write failed: {e}", file=sys.stderr)
        return 1

    try:
        os.chmod(tmp_path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError as e:
        print(
            f"[bench cli] could not restrict temp file permissions: {e}",
            file=sys.stderr,
        )

    print(f"Bench viewer written to: {tmp_path}")
    if not webbrowser.open(f"file://{tmp_path}"):
        print(
            "[bench cli] could not auto-open browser; open the path above manually.",
            file=sys.stderr,
        )
    return 0


def cmd_retire(
    archive_dir: str | None,
    reason: str | None,
    remediation: str | None = None,
) -> int:
    """Retire the active chain under C-008's bounded exception.

    Argument handling and rendering only. Every guard, ordering guarantee, and
    write lives in ``ledger.retire.execute_retirement``; this passes it the real
    ``sys.stdin.isatty`` and ``input`` so the human gate is the live one and not
    a test seam.

    Both flags are required and neither has a default. ``--archive-dir`` in
    particular must be stated deliberately, because C-008(d) commits the
    operator to retaining what lands there indefinitely.
    """
    if not archive_dir:
        print(
            "[bench cli] retire requires --archive-dir <path>; the archive is "
            "retained indefinitely (C-008(d)), so name it deliberately",
            file=sys.stderr,
        )
        return 1
    if not reason:
        print(
            "[bench cli] retire requires --reason <text> describing the "
            "content which must not be published",
            file=sys.stderr,
        )
        return 1

    try:
        result: dict[str, Any] = execute_retirement(
            archive_dir=archive_dir,
            reason=reason,
            remediation=remediation,
            stdin_isatty=sys.stdin.isatty,
            prompt=input,
        )
    except RetirementError as e:
        print(f"[bench cli] {e}", file=sys.stderr)
        return 1
    except LedgerReadError as e:
        print(f"[bench cli] ledger unreadable, nothing retired: {e}", file=sys.stderr)
        return 1
    except OSError as e:
        print(f"[bench cli] retirement failed: {e}", file=sys.stderr)
        return 1

    anchor: dict = result.get("anchor", {})
    successor: dict = result.get("successor", {})
    print("Chain retired.")
    print(f"  archive      : {result.get('archive_path', '-')}")
    print(f"  archived     : {result.get('archive_entries', 0)} entries, verified")
    print(f"  anchor       : {anchor.get('entry_hash', '-')}")
    print(f"  successor    : {successor.get('entries', 0)} entry (the anchor)")
    print()
    print("Confirm the retirement independently with:")
    print("  python -m cli audit-retirement")
    return 0


def cmd_audit_retirement(archive: str | None = None) -> int:
    """Run C-008's auditor check against the current chain's opening anchor.

    C-008 says an auditor confirms a retirement by running ``verify_chain``
    against the archive and checking that its tip hash and entry count match the
    anchor. This is that check, so it is a command rather than a paragraph
    someone has to reimplement by hand.

    With no argument it reads the anchor from the current chain's first entry
    and the archive location from the path that anchor recorded.
    """
    entries: list[dict] = load_ledger()
    if not entries:
        print("Ledger is empty. No retirement to audit.", file=sys.stderr)
        return 1

    anchor: dict = entries[0]
    change: Any = anchor.get("change")
    tool: str = (
        str(change.get("tool", "")) if isinstance(change, dict) else ""
    )
    if tool != ANCHOR_TOOL:
        print(
            f"This chain does not open with a retirement anchor "
            f"(first entry tool is {tool or 'unset'!r}), so there is nothing "
            f"to audit.",
            file=sys.stderr,
        )
        return 1

    resolved: str | None = archive
    if resolved and os.path.isdir(resolved):
        # Accept either the archive directory or the ledger file inside it.
        resolved = os.path.join(resolved, os.path.basename(resolve_ledger_path()))

    report: dict[str, Any] = audit_retirement(anchor, resolved)

    if report.get("defects"):
        print("Retirement audit: ANCHOR MALFORMED", file=sys.stderr)
        for defect in report["defects"]:
            print(f"  - {defect}", file=sys.stderr)
        return 1

    print(f"  archive      : {_display_path(str(report.get('archive_path', '-')))}")
    print(f"  verified file: {_display_path(str(report.get('archive_ledger', '-')))}")
    print(f"  archive tip  : {report.get('found_tips', [])}")
    print(f"  anchor tip   : {report.get('expected_tip', '-')}")
    print(f"  archive count: {report.get('found_entries', '-')}")
    print(f"  anchor count : {report.get('expected_entries', '-')}")

    if report.get("ok"):
        print("Retirement audit: CONFIRMED")
        return 0

    print("Retirement audit: FAILED", file=sys.stderr)
    if reason := report.get("reason"):
        print(f"  reason       : {reason}", file=sys.stderr)
    if message := report.get("message"):
        print(f"  message      : {message}", file=sys.stderr)
    return 1


def _print_entry_line(entry: dict) -> None:
    timestamp: str = str(entry.get("timestamp", "-"))
    change: Any = entry.get("change")
    file: str = "-"
    if isinstance(change, dict):
        file = str(change.get("file", "-"))

    oracle: Any = entry.get("oracle")
    oracle_dict: dict = oracle if isinstance(oracle, dict) else {}
    verdict: str = str(entry_verdict(entry) or "").strip()
    if not verdict:
        # No recorded verdict: an older fail-open entry (pipeline error with
        # no adjudicated verdict) reads as FAIL-OPEN for historical accuracy.
        if entry_has_pipeline_error(entry):
            verdict = "FAIL-OPEN"
        else:
            verdict = "-"

    entry_hash: str = str(entry.get("entry_hash", "-"))
    short: str = _short_hash(entry_hash, _HASH_PREFIX_LEN)

    print(f"  {timestamp}  {verdict:10}  {file}  [{short}]")

    if verdict == "VETO":
        citations: Any = oracle_dict.get("constraint_citations")
        if isinstance(citations, list) and citations:
            cite_str: str = ", ".join(
                c.get("constraint_id", str(c)) if isinstance(c, dict) else str(c)
                for c in citations if c
            )
            if cite_str:
                print(f"      citations: {cite_str}")


def _short_hash(value: str, length: int) -> str:
    if not value or value == "-":
        return "-"
    if len(value) <= length:
        return value
    return value[:length] + "..."
