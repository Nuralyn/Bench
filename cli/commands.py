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

import json
import os
import stat
import subprocess
import sys
import tempfile
import webbrowser
from pathlib import Path
from typing import Any

from ledger.chain import (
    LedgerReadError,
    append_entry,
    load_ledger,
    resolve_ledger_path,
)
from ledger.attestation import AttestationError, build_attestation, render
from ledger.migrate import migrate_ledger
from ledger.sanitize import (
    SANITATION_VERDICT,
    SanitationError,
    audit_sanitation,
    INCONCLUSIVE,
    PRESENT,
    REMOVED,
    access_established,
    build_sanitation_record,
    classify_removal,
    confirm_sanitation_interactively,
    object_endpoint,
    verify_binding,
)
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


def cmd_record_sanitation(
    refs_file: str | None = None,
    backup_id: str | None = None,
    backup_digest: str | None = None,
    reason: str | None = None,
    retention_owner: str | None = None,
    retention_policy: str | None = None,
    repository: str | None = None,
) -> int:
    """Append a published-copy sanitation record to the live chain.

    Run this AFTER the rewrite and BEFORE the force-push. The record names
    post-image hashes, so it cannot be written until the rewrite exists; and
    C-008 makes an unrecorded removal a violation, so the rewritten history
    must not be published until the record does.

    Refuses outside a plain TTY and inside an agent session, refuses a record
    that does not conform, and refuses to report success if the chain does
    not still verify afterwards.
    """
    missing: list[str] = [
        name
        for name, value in (
            ("--refs-file", refs_file),
            ("--backup-id", backup_id),
            ("--backup-digest", backup_digest),
            ("--reason", reason),
            ("--retention-owner", retention_owner),
            ("--retention-policy", retention_policy),
            ("--repository", repository),
        )
        if not value
    ]
    if missing:
        print(
            f"[bench cli] record-sanitation requires {', '.join(missing)}. "
            f"C-008 enumerates every field; none of them has a default.",
            file=sys.stderr,
        )
        return 1

    try:
        payload: dict[str, Any] = json.loads(
            Path(str(refs_file)).read_text(encoding="utf-8")
        )
    except (OSError, ValueError) as exc:
        print(f"[bench cli] cannot read refs file: {exc}", file=sys.stderr)
        return 1

    refs: Any = payload.get("refs")
    if not isinstance(refs, list):
        print(
            "[bench cli] refs file must contain a 'refs' list of "
            "{ref, pre_image, post_image} objects.",
            file=sys.stderr,
        )
        return 1

    genesis_before: str = str(verify_chain().get("genesis_hash", ""))

    try:
        decision: str = confirm_sanitation_interactively(
            refs, str(backup_id)
        )
        record: dict[str, Any] = build_sanitation_record(
            refs=refs,
            backup_id=str(backup_id),
            backup_digest=str(backup_digest),
            reason=str(reason),
            human_decision=decision,
            retention_owner=str(retention_owner),
            retention_policy=str(retention_policy),
            repository=str(repository),
        )
    except SanitationError as exc:
        print(f"[bench cli] {exc}", file=sys.stderr)
        return 1

    appended: dict[str, Any] = append_entry(record)

    # C-008(d): the chain this operation promised not to touch must still
    # verify, with the same genesis it had before.
    after: dict[str, Any] = verify_chain()
    if not after.get("valid") or after.get("genesis_hash") != genesis_before:
        print(
            "[bench cli] the record was appended but the chain no longer "
            f"verifies as expected ({after.get('failure_type', 'genesis moved')}). "
            "Do NOT push the rewritten history; resolve this first.",
            file=sys.stderr,
        )
        return 1

    print("Sanitation: RECORDED")
    print(f"  entry        : {appended.get('entry_hash', '-')}")
    print(f"  refs recorded: {len(refs)}")
    print(f"  backup id    : {backup_id}")
    print(f"  chain        : VALID, genesis {genesis_before[:12]} unchanged")
    print("  next         : verify with 'python -m cli audit-sanitation "
          "<backup>', then push.")
    return 0


def _git_refs(args: list[str]) -> dict[str, str] | None:
    """Run a git ref-listing command into a {refname: sha} map.

    Returns None on failure rather than an empty map, because "no refs" and
    "could not read refs" must not look alike to a gate that refuses on
    mismatch.
    """
    try:
        proc = subprocess.run(
            args, capture_output=True, text=True, encoding="utf-8"
        )
    except OSError as exc:
        print(f"[bench cli] cannot run git: {exc}", file=sys.stderr)
        return None
    if proc.returncode != 0:
        print(
            f"[bench cli] git failed: {proc.stderr.strip()}", file=sys.stderr
        )
        return None

    refs: dict[str, str] = {}
    for line in proc.stdout.splitlines():
        parts = line.split()
        if len(parts) == 2:
            sha, name = parts[0], parts[1]
            if not name.endswith("^{}"):
                refs[name] = sha
    return refs


def _gh_status(endpoint: str) -> int:
    """HTTP status from ``gh api -i <endpoint>``, or 0 when unknown.

    Returns 0 rather than raising or guessing when gh is missing, the call
    fails, or the status cannot be parsed. classify_removal maps 0 to
    inconclusive, so an unanswered probe can never be read as a removal
    (C-001: the failure is surfaced, not swallowed into a pass).
    """
    try:
        proc = subprocess.run(
            ["gh", "api", "-i", endpoint],
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
    except OSError as exc:
        print(f"[bench cli] cannot run gh: {exc}", file=sys.stderr)
        return 0

    first: list[str] = (proc.stdout or proc.stderr or "").splitlines()[0:1] or [""]
    for token in first[0].split():
        if token.isdigit() and len(token) == 3:
            return int(token)
    # Logged for the same reason the OSError branch is: an unparseable
    # response is a question that went unanswered, and it should be as
    # diagnosable as a missing binary rather than a silent 0.
    print(
        f"[bench cli] no HTTP status in the response for {endpoint}: "
        f"{first[0][:120]!r}",
        file=sys.stderr,
    )
    return 0


def _probe_manifest_objects(
    lines: list[str], repository: str
) -> tuple[dict[str, int], list[str]]:
    """Probe every manifest object at its own endpoint.

    Returns (tally, problems): verdict counts keyed by REMOVED, PRESENT, and
    INCONCLUSIVE, and one problem line per object that did not prove removed.
    """
    tally: dict[str, int] = {REMOVED: 0, PRESENT: 0, INCONCLUSIVE: 0}
    problems: list[str] = []

    for line in lines:
        if not line.strip():
            continue
        parts: list[str] = line.split("\t")
        if len(parts) != 2:
            problems.append(f"malformed manifest line: {line!r}")
            tally[INCONCLUSIVE] += 1
            continue
        sha, object_type = parts[0].strip(), parts[1].strip()
        try:
            endpoint: str = object_endpoint(object_type, repository, sha)
        except SanitationError as exc:
            problems.append(str(exc))
            tally[INCONCLUSIVE] += 1
            continue

        # Defaults to 0 when nothing parses, and classify_removal maps 0 to
        # inconclusive. An unparseable response must never read as removed.
        status: int = _gh_status(endpoint)
        verdict: str = classify_removal(status)
        tally[verdict] += 1
        if verdict != REMOVED:
            problems.append(f"{sha} ({object_type}): HTTP {status} -> {verdict}")

    return tally, problems


def cmd_verify_purge(
    manifest: str | None = None, repository: str | None = None
) -> int:
    """Prove purged objects are actually gone from a repository.

    Probes each object at the endpoint for its own type. Reports success only
    when every object answered 404. A 403, a rate limit, an expired token, or
    any 5xx is counted as inconclusive and fails the check, because the
    dangerous outcome here is a silent false all-clear: believing a purge
    finished when the objects are still served.
    """
    if not manifest or not repository:
        print(
            "[bench cli] verify-purge requires --manifest <tsv> and "
            "--repository <owner/name>.",
            file=sys.stderr,
        )
        return 1

    try:
        lines: list[str] = (
            Path(manifest).read_text(encoding="utf-8").splitlines()
        )
    except OSError as exc:
        print(f"[bench cli] cannot read manifest: {exc}", file=sys.stderr)
        return 1

    # Prove the repository answers before interpreting any 404 under it.
    # GitHub returns 404 for a repository that is private to this token,
    # renamed, or misspelled, and then every object probe returns 404 too. A
    # single transposed letter would otherwise read as a completed purge.
    access_status: int = _gh_status(f"repos/{repository}")
    if not access_established(access_status):
        print(
            f"[bench cli] cannot establish access to {repository} "
            f"(HTTP {access_status}). Refusing to probe objects: every "
            f"object under an unreachable repository returns 404, which "
            f"would read as a completed purge.",
            file=sys.stderr,
        )
        return 1

    tally, problems = _probe_manifest_objects(lines, repository)

    total: int = sum(tally.values())
    print(f"Purge check: {repository}")
    print(f"  objects probed : {total}")
    print(f"  removed (404)  : {tally[REMOVED]}")
    print(f"  present (200)  : {tally[PRESENT]}")
    print(f"  inconclusive   : {tally[INCONCLUSIVE]}")

    # A manifest that parsed to nothing must not read as a clean sweep. With
    # every tally at zero the checks below all pass and the command would
    # report success having made no request at all, turning a failed
    # manifest-generation step into a purge confirmation.
    if total == 0:
        print(
            "[bench cli] the manifest yielded no objects to probe. Refusing "
            "to report a purge that was never checked; regenerate the "
            "manifest and re-run.",
            file=sys.stderr,
        )
        return 1

    if tally[PRESENT] or tally[INCONCLUSIVE]:
        print("", file=sys.stderr)
        for problem in problems[:20]:
            print(f"  {problem}", file=sys.stderr)
        if len(problems) > 20:
            print(f"  ... and {len(problems) - 20} more", file=sys.stderr)
        print(
            "\nNOT PROVEN REMOVED. An inconclusive answer is not a pass: "
            "re-run when the API answers cleanly.",
            file=sys.stderr,
        )
        return 1

    print("  all objects answered 404 at their own endpoint")
    return 0


def cmd_verify_sanitation_binding(
    record_hash: str | None = None,
    mirror: str | None = None,
    repository: str | None = None,
) -> int:
    """Bind a sanitation warrant to the rewrite about to be pushed.

    audit-sanitation answers "is this record well formed and is the chain
    intact". That is not the same question as "does this record authorize
    THIS rewrite of THIS repository, right now, and has it not already been
    used". A well-formed record is a warrant with no name on it.

    Reads the live remote and the local mirror and compares both against the
    record, so the gate refuses on a moved remote, a mirror holding a
    different rewrite, a record for another repository, or a warrant already
    spent by an earlier push.
    """
    missing: list[str] = [
        name
        for name, value in (
            ("--record", record_hash),
            ("--mirror", mirror),
            ("--repository", repository),
        )
        if not value
    ]
    if missing:
        print(
            f"[bench cli] verify-sanitation-binding requires "
            f"{', '.join(missing)}.",
            file=sys.stderr,
        )
        return 1

    chain: dict[str, Any] = verify_chain()
    if not chain.get("valid"):
        print(
            f"[bench cli] the chain does not verify "
            f"({chain.get('failure_type', 'unknown')}); a record read from it "
            f"cannot be trusted.",
            file=sys.stderr,
        )
        return 1

    try:
        entries: list[dict] = load_ledger()
    except LedgerReadError as exc:
        print(f"[bench cli] cannot read ledger: {exc}", file=sys.stderr)
        return 1

    matches: list[dict] = [
        e
        for e in entries
        if e.get("verdict") == SANITATION_VERDICT
        and e.get("entry_hash") == record_hash
    ]
    if not matches:
        print(
            f"[bench cli] no sanitation record with entry hash {record_hash}",
            file=sys.stderr,
        )
        return 1

    raw_change: Any = matches[0].get("change")
    change: dict[str, Any] = raw_change if isinstance(raw_change, dict) else {}
    raw_summary: Any = change.get("diff_summary")
    summary: dict[str, Any] = (
        raw_summary if isinstance(raw_summary, dict) else {}
    )

    remote_refs: dict[str, str] | None = _git_refs(
        ["git", "ls-remote", f"https://github.com/{repository}.git"]
    )
    local_refs: dict[str, str] | None = _git_refs(
        ["git", "-C", str(mirror), "for-each-ref", "--format=%(objectname) %(refname)"]
    )
    if remote_refs is None or local_refs is None:
        print(
            "[bench cli] could not read refs; refusing rather than assuming.",
            file=sys.stderr,
        )
        return 1

    defects: list[str] = verify_binding(
        summary, str(repository), remote_refs, local_refs
    )

    print(f"Binding: {'BOUND' if not defects else 'NOT BOUND'}")
    print(f"  record     : {record_hash}")
    print(f"  repository : {repository}")
    print(f"  remote refs: {len(remote_refs)}")
    print(f"  mirror refs: {len(local_refs)}")
    if defects:
        for defect in defects:
            print(f"  defect     : {defect}", file=sys.stderr)
        return 1
    print("  every recorded ref matches the remote pre-image and the mirror "
          "post-image, and the warrant is unspent")
    return 0


def cmd_audit_sanitation(
    backup: str | None = None, record_hash: str | None = None
) -> int:
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
    # load_ledger is deliberately tolerant: it logs an unreadable segment and
    # returns what it could parse. That is right for reporting, and wrong
    # here, because a corrupt segment holding the sanitation record would
    # produce "NONE RECORDED" and exit 0. Verify first, so absence means
    # absence rather than unreadability.
    chain: dict[str, Any] = verify_chain()
    if not chain.get("valid"):
        print(
            f"[bench cli] refusing to audit: the chain does not verify "
            f"({chain.get('failure_type', 'unknown')}). A record could be "
            f"present but unreadable, so absence cannot be reported as "
            f"absence.",
            file=sys.stderr,
        )
        return 1

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

    selected: tuple[list[dict], Path | None] | None = _select_audit_targets(
        records, record_hash, backup
    )
    if selected is None:
        return 1
    records, artifact = selected

    failures: int = 0

    for record in records:
        if not _report_sanitation_record(record, artifact):
            failures += 1

    return 1 if failures else 0


def _select_audit_targets(
    records: list[dict], record_hash: str | None, backup: str | None
) -> tuple[list[dict], Path | None] | None:
    """Narrow records to the named entry and pair them with the artifact.

    Returns None (after printing the defect) when the named record does not
    exist or when one backup is offered against several records.
    """
    if record_hash:
        records = [r for r in records if r.get("entry_hash") == record_hash]
        if not records:
            print(
                f"[bench cli] no sanitation record with entry hash "
                f"{record_hash}",
                file=sys.stderr,
            )
            return None

    artifact: Path | None = Path(backup) if backup else None
    # One artifact cannot belong to several records. Two legitimate
    # sanitations have different backups, so applying one path to both would
    # fail the other on a digest mismatch that is not a defect.
    if artifact is not None and len(records) > 1:
        print(
            f"[bench cli] {len(records)} sanitation records exist and a "
            f"backup was supplied. Name the one it belongs to with "
            f"--record <entry_hash>; a backup verifies one record, not all.",
            file=sys.stderr,
        )
        return None
    return records, artifact


def _report_sanitation_record(record: dict, artifact: Path | None) -> bool:
    """Print one sanitation record's audit report. True when it is valid."""
    # A malformed record is exactly what the auditor exists to diagnose,
    # so reading it must not raise before the defects can be printed.
    raw_change: Any = record.get("change")
    change: dict[str, Any] = raw_change if isinstance(raw_change, dict) else {}
    raw_summary: Any = change.get("diff_summary")
    summary: dict[str, Any] = (
        raw_summary if isinstance(raw_summary, dict) else {}
    )
    raw_refs: Any = summary.get("refs")
    ref_count: int = len(raw_refs) if isinstance(raw_refs, list) else 0

    result: dict[str, Any] = audit_sanitation(record, artifact)
    if result["valid"]:
        status: str = "VALID"
    elif result.get("incomplete"):
        status = "INCOMPLETE"
    else:
        status = "INVALID"

    print(f"Sanitation: {status}")
    print(f"  entry         : {record.get('entry_hash', '-')}")
    print(f"  recorded at   : {record.get('timestamp', '-')}")
    print(f"  backup id     : {summary.get('backup_id', '-')}")
    print(f"  digest        : {summary.get('backup_digest', '-')}")
    print(f"  refs rewritten: {ref_count}")
    print(f"  retention     : {summary.get('retention_owner', '-')}")
    if result["digest_checked"]:
        print(
            f"  backup digest : "
            f"{'matches' if result['digest_matches'] else 'DOES NOT MATCH'}"
        )
    else:
        print("  backup digest : NOT CHECKED (supply the artifact path)")

    if not result["valid"]:
        for defect in result["defects"]:
            print(f"  defect        : {defect}", file=sys.stderr)
        return False
    return True


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
    try:
        entries: list[dict] = load_ledger()
    except LedgerReadError as exc:
        print(f"[bench cli] cannot read ledger: {exc}", file=sys.stderr)
        return 1

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
        _print_veto_citations(oracle_dict)


def _print_veto_citations(oracle_dict: dict) -> None:
    """Print a VETO entry's constraint citations line, when it has any."""
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
