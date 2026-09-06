"""Entry point for ``python -m cli`` and the ``bench`` console script.

argparse owns the grammar. Every command and flag is declared once in
``build_parser``, so ``bench --help`` lists all of them (including
``verify-purge`` and ``verify-sanitation-binding``, which the earlier
hand-rolled parser accepted but never documented), ``bench <command> --help``
explains one command, and an unknown command or flag exits 2 with a usage
message instead of silence.

Required values are still checked by the command functions rather than by
``required=True``: their messages carry the reasoning (retire's
``--archive-dir`` is retained indefinitely under C-008(d), a checkpoint's
``--cutoff`` is a deliberate act), and that text would be lost behind a
generic "the following arguments are required". Logic lives in commands.py.
"""

import argparse
import sys
from collections.abc import Callable, Sequence
from typing import NoReturn

from cli.commands import (
    cmd_attest,
    cmd_audit_retirement,
    cmd_audit_sanitation,
    cmd_constitution,
    cmd_install,
    cmd_ledger,
    cmd_migrate_ledger,
    cmd_record_sanitation,
    cmd_retire,
    cmd_stats,
    cmd_uninstall,
    cmd_verify,
    cmd_verify_purge,
    cmd_verify_sanitation_binding,
    cmd_viewer,
)
from cli.install import PROVIDERS

USAGE: str = "bench <command> [options]\n       python -m cli <command> [options]"
_REPO_METAVAR: str = "OWNER/NAME"

_DESCRIPTION: str = (
    "Bench: constitutional governance for Claude Code. Every command reads the\n"
    "ledger of the project it is run from (or BENCH_LEDGER_PATH)."
)


class _ParserExit(Exception):
    """argparse wanted to end the process; ``main`` returns the code instead.

    ``code`` is 0 after help, 2 after a usage error. The message, if any,
    has already been written to stderr by ``_Parser.exit``.
    """

    def __init__(self, code: int) -> None:
        super().__init__(code)
        self.code: int = code


class _Parser(argparse.ArgumentParser):
    """An ArgumentParser that reports instead of calling ``sys.exit``.

    ``main`` returns an exit code so the console script and ``python -m cli``
    behave alike and tests can call it directly; letting argparse raise
    SystemExit and catching it would be swallowing a process-exit signal.
    Subparsers are created with this class too (argparse uses the parent's
    type), so every usage error takes the same path.
    """

    def exit(self, status: int = 0, message: str | None = None) -> NoReturn:
        if message:
            print(message, end="", file=sys.stderr)
        raise _ParserExit(status)

    def error(self, message: str) -> NoReturn:
        self.print_usage(sys.stderr)
        print(f"{self.prog}: error: {message}", file=sys.stderr)
        raise _ParserExit(2)


def _add(
    sub: "argparse._SubParsersAction[_Parser]",
    name: str,
    help_text: str,
    description: str | None = None,
) -> argparse.ArgumentParser:
    return sub.add_parser(
        name,
        # Set explicitly: argparse otherwise derives a subparser's prog from
        # the parent's usage string, which here is two lines, and the
        # per-command usage came out mangled on Python 3.11 to 3.13.
        prog=f"bench {name}",
        help=help_text,
        description=description or help_text,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        # A flag is spelled in full or it is an error: argparse would
        # otherwise accept --prov for --provider, and a command that appends
        # to an evidence chain should not guess.
        allow_abbrev=False,
    )


def build_parser() -> argparse.ArgumentParser:
    """The complete grammar: one subparser per command, every flag declared."""
    parser = _Parser(
        prog="bench",
        usage=USAGE,
        description=_DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        allow_abbrev=False,
    )
    sub = parser.add_subparsers(dest="command", metavar="<command>", title="commands")
    sub.required = True

    _add(sub, "verify", "Validate the ledger hash chain")
    ledger = _add(sub, "ledger", "Show ledger entries (default: the last 10)")
    ledger.add_argument("--all", action="store_true", help="show every entry")
    ledger.add_argument("--vetoes", action="store_true", help="show only VETO entries")
    _add(sub, "stats", "Governance summary statistics")
    _add(
        sub,
        "migrate-ledger",
        "One-time upgrade for a clone made before the ledger became private",
        "One-time upgrade for a clone made before the ledger became private.\n"
        "Copies the pre-migration chain into .bench/ from the working tree, or\n"
        "from git history if the checkout already removed it. Idempotent, refuses\n"
        "to touch a chain that already exists, and verifies before reporting\n"
        "success.",
    )
    _add(sub, "constitution", "Show the constitution enforced here and its sources")
    _add(sub, "viewer", "Open the HTML verdict viewer in the browser")

    attest = _add(
        sub,
        "attest",
        "Export a public attestation up to a declared cutoff",
        "Export a public attestation for entries up to the declared cutoff:\n"
        "commitments, verdicts, and constraint ids, with no diff, path, or stage\n"
        "prose. --cutoff is required; a checkpoint is a deliberate act, not a\n"
        "running view of the tip. Not a backup.",
    )
    attest.add_argument("--cutoff", metavar="HASH", help="entry hash the attestation runs up to (required)")
    attest.add_argument("--bench-version", metavar="X.Y.Z", help="Bench version to record (required)")
    attest.add_argument("--out", metavar="PATH", help="write the attestation here instead of stdout")

    record = _add(
        sub,
        "record-sanitation",
        "Append a published-copy sanitation record",
        "Append a published-copy sanitation record. Run AFTER the rewrite and\n"
        "BEFORE the push: the record names post-image hashes, and an unrecorded\n"
        "removal violates C-008. Refuses outside a plain TTY, inside an agent\n"
        "session, on a non-conforming record, and if the chain does not still\n"
        "verify after. Every flag is required: C-008 enumerates each field and\n"
        "none has a default.",
    )
    record.add_argument("--refs-file", metavar="PATH", help="pre- and post-rewrite refs")
    record.add_argument("--backup-id", metavar="ID", help="identifier of the encrypted backup")
    record.add_argument("--backup-digest", metavar="HEX", help="sha256 of the encrypted backup")
    record.add_argument("--reason", metavar="TEXT", help="why the published copy was sanitized")
    record.add_argument("--retention-owner", metavar="NAME", help="who retains the backup")
    record.add_argument("--retention-policy", metavar="TEXT", help="how long and under what terms")
    record.add_argument("--repository", metavar=_REPO_METAVAR, help="GitHub repository the copy lives in")

    purge = _add(
        sub,
        "verify-purge",
        "Check that a purge manifest's objects are gone from GitHub",
        "Check every object in a purge manifest against the GitHub repository\n"
        "and report which are gone, which remain, and which could not be probed.\n"
        "--manifest and --repository are required.",
    )
    purge.add_argument("--manifest", metavar="TSV", help="purge manifest to check")
    purge.add_argument("--repository", metavar=_REPO_METAVAR, help="GitHub repository to probe")

    binding = _add(
        sub,
        "verify-sanitation-binding",
        "Check a sanitation record against a mirror and the remote",
        "Check that a sanitation record's post-image refs match a local mirror\n"
        "and the remote repository. --record, --mirror, and --repository are\n"
        "required.",
    )
    binding.add_argument("--record", metavar="HASH", help="entry hash of the sanitation record")
    binding.add_argument("--mirror", metavar="PATH", help="local mirror clone to compare")
    binding.add_argument("--repository", metavar=_REPO_METAVAR, help="GitHub repository to compare")

    audit_san = _add(
        sub,
        "audit-sanitation",
        "Audit this chain's published-copy sanitation records",
        "Audit this chain's published-copy sanitation records: structure, the\n"
        "live chain's own state, and the encrypted backup's digest when PATH is\n"
        "given. Read-only; it never performs a sanitation.",
    )
    audit_san.add_argument("backup", nargs="?", metavar="PATH", help="encrypted backup to digest")
    audit_san.add_argument("--record", metavar="HASH", help="audit only this record")

    audit_ret = _add(
        sub,
        "audit-retirement",
        "Run C-008's auditor check on this chain's opening anchor",
        "Run C-008's auditor check on this chain's opening anchor. PATH defaults\n"
        "to the archive the anchor recorded.",
    )
    audit_ret.add_argument("archive", nargs="?", metavar="PATH", help="archived chain to check")

    retire = _add(
        sub,
        "retire",
        "Retire this chain under C-008's bounded exception",
        "Retire this chain under C-008's bounded exception. Requires a human at\n"
        "a plain terminal: it refuses when stdin is not a TTY and when\n"
        "BENCH_SUBPROCESS, CLAUDECODE, or CI are set, so it cannot be run from\n"
        "inside a Claude Code session. --archive-dir and --reason are required.",
    )
    retire.add_argument("--archive-dir", metavar="PATH", help="where the retired chain is kept, indefinitely")
    retire.add_argument("--reason", metavar="TEXT", help="the content which must not be published")
    retire.add_argument("--remediation", metavar="TEXT", help="what was done about it")

    install = _add(
        sub,
        "install",
        "Register Bench in a project",
        "Register Bench in a project: write the PreToolUse hook into its\n"
        ".claude/settings.json (pinned to this interpreter and the installed\n"
        "hook script), add /.bench/ to its .gitignore, install the ledger commit\n"
        "guard, and record what was written in .bench/install.json. Idempotent.\n"
        "--project is required.",
    )
    install.add_argument("--project", metavar="PATH", help="the project to govern")
    install.add_argument(
        "--provider", choices=PROVIDERS, help="set BENCH_PROVIDER in that project's settings"
    )

    uninstall = _add(
        sub,
        "uninstall",
        "Reverse install from its receipt",
        "Reverse install from its receipt: remove the hook, the env keys, the\n"
        "guard, and the ignore line install wrote, and only if unchanged since.\n"
        "Refuses inside an agent session, like retire. --project is required.",
    )
    uninstall.add_argument("--project", metavar="PATH", help="the project to release")

    _add(sub, "help", "Show this help")

    # The top-level help also lists every command with its flags, generated
    # from the subparsers themselves so the summary cannot drift from what
    # each command accepts.
    lines: list[str] = ["commands and their flags:"]
    for name, command in sub.choices.items():
        usage_lines: list[str] = command.format_usage().splitlines()
        first: str = usage_lines[0].replace("usage: ", "", 1).replace("bench ", "", 1)
        lines.append("  " + first.replace(" [-h]", "", 1).rstrip())
        lines.extend("      " + rest.strip() for rest in usage_lines[1:] if rest.strip())
    parser.epilog = "\n".join(lines)
    return parser


_DISPATCH: dict[str, Callable[[argparse.Namespace], int]] = {
    "verify": lambda a: cmd_verify(),
    "ledger": lambda a: cmd_ledger(show_all=a.all, vetoes_only=a.vetoes),
    "stats": lambda a: cmd_stats(),
    # No TTY gate, unlike retire. Retirement is destructive and moves a chain
    # out of the way, so it is human-only. Migration only copies whole files
    # into a location holding no chain, refuses to touch one that already
    # exists, and verifies the result before reporting success.
    "migrate-ledger": lambda a: cmd_migrate_ledger(),
    "constitution": lambda a: cmd_constitution(),
    "viewer": lambda a: cmd_viewer(),
    "attest": lambda a: cmd_attest(cutoff=a.cutoff, bench_version=a.bench_version, out=a.out),
    "record-sanitation": lambda a: cmd_record_sanitation(
        refs_file=a.refs_file,
        backup_id=a.backup_id,
        backup_digest=a.backup_digest,
        reason=a.reason,
        retention_owner=a.retention_owner,
        retention_policy=a.retention_policy,
        repository=a.repository,
    ),
    "verify-purge": lambda a: cmd_verify_purge(manifest=a.manifest, repository=a.repository),
    "verify-sanitation-binding": lambda a: cmd_verify_sanitation_binding(
        record_hash=a.record, mirror=a.mirror, repository=a.repository
    ),
    "audit-sanitation": lambda a: cmd_audit_sanitation(backup=a.backup, record_hash=a.record),
    "audit-retirement": lambda a: cmd_audit_retirement(a.archive),
    "retire": lambda a: cmd_retire(
        archive_dir=a.archive_dir, reason=a.reason, remediation=a.remediation
    ),
    "install": lambda a: cmd_install(project=a.project, provider=a.provider),
    "uninstall": lambda a: cmd_uninstall(project=a.project),
}


def main(argv: Sequence[str]) -> int:
    """Parse ``argv`` (``argv[0]`` is the program) and run one command.

    Usage errors exit 2, help exits 0, commands return their own code. The
    parser reports and raises ``_ParserExit`` instead of calling
    ``sys.exit``, so the code is returned here rather than a process-exit
    signal being caught.
    """
    parser: argparse.ArgumentParser = build_parser()
    try:
        args: argparse.Namespace = parser.parse_args(list(argv[1:]))
    except _ParserExit as exc:
        return exc.code
    if args.command == "help":
        parser.print_help()
        return 0
    return _DISPATCH[args.command](args)


def run() -> None:
    """Console-script entry point for the ``bench`` command (pyproject.toml).

    ``python -m cli`` reaches ``main`` through the block below. Both spellings
    hand ``main`` the same argv shape, so the two cannot drift.
    """
    sys.exit(main(sys.argv))


if __name__ == "__main__":
    run()
