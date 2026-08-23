"""Entry point for ``python -m cli``.

Parses the first positional argument as a command name and dispatches
into ``cli.commands``. Keeps parsing minimal — flags are simple string
membership checks against the remaining argv. Logic lives in commands.py.
"""

import sys

from cli.commands import (
    cmd_attest,
    cmd_audit_retirement,
    cmd_audit_sanitation,
    cmd_constitution,
    cmd_ledger,
    cmd_migrate_ledger,
    cmd_record_sanitation,
    cmd_retire,
    cmd_stats,
    cmd_verify,
    cmd_viewer,
)


_USAGE: str = (
    "Usage: python -m cli <command> [options]\n"
    "\n"
    "Commands:\n"
    "  verify                     Validate the ledger hash chain\n"
    "  ledger [--all] [--vetoes]  Show ledger entries (default: last 10)\n"
    "  stats                      Governance summary statistics\n"
    "  migrate-ledger             One-time upgrade for a clone made before\n"
    "                             the ledger became private. Copies the\n"
    "                             pre-migration chain into .bench/ from the\n"
    "                             working tree, or from git history if the\n"
    "                             checkout already removed it. Idempotent,\n"
    "                             refuses to touch a chain that already\n"
    "                             exists, and verifies before reporting\n"
    "                             success.\n"
    "  constitution               Show current constitutional constraints\n"
    "  viewer                     Open an HTML verdict viewer in the browser\n"
    "  attest --cutoff HASH --bench-version X.Y.Z [--out PATH]\n"
    "                             Export a public attestation for entries up\n"
    "                             to the declared cutoff: commitments,\n"
    "                             verdicts, and constraint ids, with no diff,\n"
    "                             path, or stage prose. --cutoff is required;\n"
    "                             a checkpoint is a deliberate act, not a\n"
    "                             running view of the tip. Not a backup.\n"
    "  record-sanitation --refs-file PATH --backup-id ID\n"
    "                    --backup-digest HEX --reason TEXT\n"
    "                    --retention-owner NAME --retention-policy TEXT\n"
    "                             Append a published-copy sanitation record.\n"
    "                             Run AFTER the rewrite and BEFORE the push:\n"
    "                             the record names post-image hashes, and an\n"
    "                             unrecorded removal violates C-008. Refuses\n"
    "                             outside a plain TTY, inside an agent\n"
    "                             session, on a non-conforming record, and if\n"
    "                             the chain does not still verify after.\n"
    "  audit-sanitation [PATH]    Audit this chain's published-copy\n"
    "                             sanitation records: structure, the live\n"
    "                             chain's own state, and the encrypted\n"
    "                             backup's digest when PATH is given.\n"
    "                             Read-only; it never performs a sanitation.\n"
    "  audit-retirement [PATH]    Run C-008's auditor check on this chain's\n"
    "                             opening anchor (PATH defaults to the archive\n"
    "                             the anchor recorded)\n"
    "  retire --archive-dir PATH --reason TEXT [--remediation TEXT]\n"
    "                             Retire this chain under C-008's bounded\n"
    "                             exception. Requires a human at a plain\n"
    "                             terminal: it refuses when stdin is not a TTY\n"
    "                             and when BENCH_SUBPROCESS, CLAUDECODE, or CI\n"
    "                             are set, so it cannot be run from inside a\n"
    "                             Claude Code session.\n"
)


def _flag_value(rest: list[str], flag: str) -> str | None:
    """Value following ``flag`` in ``rest``, or None when absent or last.

    Kept in the same spirit as the rest of this parser: minimal, positional,
    no argparse. Returns None rather than raising when the flag is present with
    nothing after it, and the command reports the missing value; a parser that
    raised here would turn a typo into a stack trace.
    """
    if flag not in rest:
        return None
    index: int = rest.index(flag)
    if index + 1 >= len(rest):
        return None
    return rest[index + 1]


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(_USAGE, end="")
        return 1

    command: str = argv[1]
    rest: list[str] = argv[2:]

    if command == "verify":
        return cmd_verify()
    if command == "ledger":
        return cmd_ledger(
            show_all="--all" in rest,
            vetoes_only="--vetoes" in rest,
        )
    if command == "stats":
        return cmd_stats()
    if command == "migrate-ledger":
        # No TTY gate, unlike retire. Retirement is destructive and moves a
        # chain out of the way, so it is human-only. Migration only copies
        # whole files into a location holding no chain, refuses to touch one
        # that already exists, and verifies the result before reporting
        # success, so it cannot destroy or alter evidence.
        return cmd_migrate_ledger()
    if command == "constitution":
        return cmd_constitution()
    if command == "viewer":
        return cmd_viewer()
    if command == "retire":
        return cmd_retire(
            archive_dir=_flag_value(rest, "--archive-dir"),
            reason=_flag_value(rest, "--reason"),
            remediation=_flag_value(rest, "--remediation"),
        )
    if command == "attest":
        return cmd_attest(
            cutoff=_flag_value(rest, "--cutoff"),
            bench_version=_flag_value(rest, "--bench-version"),
            out=_flag_value(rest, "--out"),
        )
    if command == "record-sanitation":
        return cmd_record_sanitation(
            refs_file=_flag_value(rest, "--refs-file"),
            backup_id=_flag_value(rest, "--backup-id"),
            backup_digest=_flag_value(rest, "--backup-digest"),
            reason=_flag_value(rest, "--reason"),
            retention_owner=_flag_value(rest, "--retention-owner"),
            retention_policy=_flag_value(rest, "--retention-policy"),
        )
    if command == "audit-sanitation":
        positional_backup: list[str] = [
            arg for arg in rest if not arg.startswith("-")
        ]
        return cmd_audit_sanitation(
            positional_backup[0] if positional_backup else None
        )
    if command == "audit-retirement":
        positional: list[str] = [arg for arg in rest if not arg.startswith("-")]
        return cmd_audit_retirement(positional[0] if positional else None)
    if command in ("-h", "--help", "help"):
        print(_USAGE, end="")
        return 0

    print(f"[bench cli] unknown command: {command}", file=sys.stderr)
    print(_USAGE, end="", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
