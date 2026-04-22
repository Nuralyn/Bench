"""Entry point for ``python -m cli``.

Parses the first positional argument as a command name and dispatches
into ``cli.commands``. Keeps parsing minimal — flags are simple string
membership checks against the remaining argv. Logic lives in commands.py.
"""

import sys

from cli.commands import (
    cmd_constitution,
    cmd_ledger,
    cmd_stats,
    cmd_verify,
)


_USAGE: str = (
    "Usage: python -m cli <command> [options]\n"
    "\n"
    "Commands:\n"
    "  verify                     Validate the ledger hash chain\n"
    "  ledger [--all] [--vetoes]  Show ledger entries (default: last 10)\n"
    "  stats                      Governance summary statistics\n"
    "  constitution               Show current constitutional constraints\n"
)


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
    if command in ("-h", "--help", "help"):
        print(_USAGE, end="")
        return 0

    print(f"[bench cli] unknown command: {command}", file=sys.stderr)
    print(_USAGE, end="", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
