"""Entry point for ``python -m cli``.

Parses the first positional argument as a command name and dispatches
into ``cli.commands``. Keeps parsing minimal — flags are simple string
membership checks against the remaining argv. Logic lives in commands.py.
"""

import sys

from cli.commands import (
    cmd_audit_retirement,
    cmd_constitution,
    cmd_ledger,
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
    "  constitution               Show current constitutional constraints\n"
    "  viewer                     Open an HTML verdict viewer in the browser\n"
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
