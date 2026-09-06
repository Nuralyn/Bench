"""Tests for the argparse grammar in cli/__main__.py.

The hand-rolled parser it replaced accepted seven flags by string
membership, left two working commands out of its usage text, and treated a
mistyped flag as absent. These tests pin the contract the replacement makes:
``--help`` names every command, each command's help names every flag it
takes, an unknown command or flag exits 2 with a usage message on stderr,
and every command routes to its function with the flags passed by name.

Run: python -m unittest tests.test_cli_parser -v
"""

import io
import subprocess
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

_REPO_ROOT: Path = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from cli import __main__ as cli_main  # noqa: E402

# Every command and the flags (or positional metavar) its help must name.
_GRAMMAR: dict[str, list[str]] = {
    "verify": [],
    "ledger": ["--all", "--vetoes"],
    "stats": [],
    "migrate-ledger": [],
    "constitution": [],
    "viewer": [],
    "attest": ["--cutoff", "--bench-version", "--out"],
    "record-sanitation": [
        "--refs-file",
        "--backup-id",
        "--backup-digest",
        "--reason",
        "--retention-owner",
        "--retention-policy",
        "--repository",
    ],
    "verify-purge": ["--manifest", "--repository"],
    "verify-sanitation-binding": ["--record", "--mirror", "--repository"],
    "audit-sanitation": ["PATH", "--record"],
    "audit-retirement": ["PATH"],
    "retire": ["--archive-dir", "--reason", "--remediation"],
    "install": ["--project", "--provider"],
    "uninstall": ["--project"],
    "help": [],
}


def _run(argv: list[str]) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code: int = cli_main.main(["bench", *argv])
    return code, out.getvalue(), err.getvalue()


class TestHelp(unittest.TestCase):
    def test_top_level_help_names_every_command_with_its_flags(self) -> None:
        # The roadmap's bar for 3.2: `python -m cli --help` lists every command
        # with its flags. The flag summary is generated from the subparsers.
        code, out, _ = _run(["--help"])
        self.assertEqual(code, 0)
        self.assertIn("commands and their flags:", out)
        for command, flags in _GRAMMAR.items():
            with self.subTest(command=command):
                self.assertIn(command, out)
                for flag in flags:
                    self.assertIn(flag, out)

    def test_the_two_commands_the_old_usage_omitted_are_listed(self) -> None:
        _, out, _ = _run(["--help"])
        self.assertIn("verify-purge", out)
        self.assertIn("verify-sanitation-binding", out)

    def test_help_names_both_spellings(self) -> None:
        _, out, _ = _run(["help"])
        self.assertIn("bench <command>", out)
        self.assertIn("python -m cli <command>", out)

    def test_each_command_help_names_its_flags(self) -> None:
        for command, flags in _GRAMMAR.items():
            with self.subTest(command=command):
                code, out, _ = _run([command, "--help"])
                self.assertEqual(code, 0)
                for flag in flags:
                    self.assertIn(flag, out)

    def test_grammar_table_matches_the_parser(self) -> None:
        # The table above must not drift from build_parser: every subparser
        # is in the table and the table names nothing the parser lacks.
        parser = cli_main.build_parser()
        actions = [a for a in parser._actions if hasattr(a, "choices") and a.choices]
        subparsers = actions[0].choices
        self.assertEqual(sorted(subparsers), sorted(_GRAMMAR))


class TestUsageErrors(unittest.TestCase):
    def test_unknown_command_is_a_usage_error(self) -> None:
        code, out, err = _run(["frobnicate"])
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("usage:", err)
        self.assertIn("invalid choice", err)

    def test_unknown_flag_is_a_usage_error_not_silence(self) -> None:
        with patch("cli.__main__.cmd_verify") as fake:
            code, _, err = _run(["verify", "--nope"])
        self.assertEqual(code, 2)
        self.assertIn("usage:", err)
        self.assertIn("unrecognized arguments", err)
        fake.assert_not_called()

    def test_no_command_is_a_usage_error(self) -> None:
        code, _, err = _run([])
        self.assertEqual(code, 2)
        self.assertIn("usage:", err)

    def test_abbreviated_flags_are_rejected_not_guessed(self) -> None:
        # argparse accepts --prov for --provider unless told not to. A tool
        # that appends to an evidence chain must not guess a flag.
        with patch("cli.__main__.cmd_install") as fake:
            code, out, err = _run(["install", "--project", "p", "--prov", "claude_code"])
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("usage:", err)
        self.assertIn("unrecognized arguments: --prov", err)
        fake.assert_not_called()
        with patch("cli.__main__.cmd_retire") as fake_retire:
            code, _, err = _run(["retire", "--archive", "d", "--reason", "r"])
        self.assertEqual(code, 2)
        self.assertIn("--archive", err)
        fake_retire.assert_not_called()

    def test_abbreviation_is_disabled_on_every_parser(self) -> None:
        parser = cli_main.build_parser()
        self.assertFalse(parser.allow_abbrev)
        actions = [a for a in parser._actions if hasattr(a, "choices") and a.choices]
        for name, command in actions[0].choices.items():
            with self.subTest(command=name):
                self.assertFalse(command.allow_abbrev)

    def test_provider_outside_the_choices_is_a_usage_error(self) -> None:
        with patch("cli.__main__.cmd_install") as fake:
            code, _, err = _run(["install", "--project", "p", "--provider", "bedrock"])
        self.assertEqual(code, 2)
        self.assertIn("invalid choice", err)
        fake.assert_not_called()


class TestDispatch(unittest.TestCase):
    def _dispatch(self, name: str, argv: list[str]) -> tuple[int, object]:
        with patch(f"cli.__main__.{name}", return_value=7) as fake:
            code, _, _ = _run(argv)
        return code, fake

    def test_zero_argument_commands(self) -> None:
        for command, name in (
            ("verify", "cmd_verify"),
            ("stats", "cmd_stats"),
            ("migrate-ledger", "cmd_migrate_ledger"),
            ("constitution", "cmd_constitution"),
            ("viewer", "cmd_viewer"),
        ):
            with self.subTest(command=command):
                code, fake = self._dispatch(name, [command])
                self.assertEqual(code, 7)
                fake.assert_called_once_with()  # type: ignore[attr-defined]

    def test_ledger_flags(self) -> None:
        code, fake = self._dispatch("cmd_ledger", ["ledger", "--all", "--vetoes"])
        self.assertEqual(code, 7)
        fake.assert_called_once_with(show_all=True, vetoes_only=True)  # type: ignore[attr-defined]
        _, fake = self._dispatch("cmd_ledger", ["ledger"])
        fake.assert_called_once_with(show_all=False, vetoes_only=False)  # type: ignore[attr-defined]

    def test_attest_flags(self) -> None:
        _, fake = self._dispatch(
            "cmd_attest", ["attest", "--cutoff", "abc", "--bench-version", "2.1.0", "--out", "o.json"]
        )
        fake.assert_called_once_with(cutoff="abc", bench_version="2.1.0", out="o.json")  # type: ignore[attr-defined]

    def test_missing_values_reach_the_command_as_none(self) -> None:
        # Required values are checked by the command, whose message carries
        # the reasoning; the parser passes None rather than refusing itself.
        _, fake = self._dispatch("cmd_retire", ["retire"])
        fake.assert_called_once_with(archive_dir=None, reason=None, remediation=None)  # type: ignore[attr-defined]

    def test_record_sanitation_flags(self) -> None:
        _, fake = self._dispatch(
            "cmd_record_sanitation",
            [
                "record-sanitation",
                "--refs-file", "refs.tsv",
                "--backup-id", "b1",
                "--backup-digest", "ff",
                "--reason", "why",
                "--retention-owner", "owner",
                "--retention-policy", "policy",
                "--repository", "o/n",
            ],
        )
        fake.assert_called_once_with(  # type: ignore[attr-defined]
            refs_file="refs.tsv",
            backup_id="b1",
            backup_digest="ff",
            reason="why",
            retention_owner="owner",
            retention_policy="policy",
            repository="o/n",
        )

    def test_verification_commands(self) -> None:
        _, fake = self._dispatch(
            "cmd_verify_purge", ["verify-purge", "--manifest", "m.tsv", "--repository", "o/n"]
        )
        fake.assert_called_once_with(manifest="m.tsv", repository="o/n")  # type: ignore[attr-defined]
        _, fake = self._dispatch(
            "cmd_verify_sanitation_binding",
            ["verify-sanitation-binding", "--record", "h", "--mirror", "m", "--repository", "o/n"],
        )
        fake.assert_called_once_with(record_hash="h", mirror="m", repository="o/n")  # type: ignore[attr-defined]

    def test_positionals_survive_flags_in_either_order(self) -> None:
        _, fake = self._dispatch(
            "cmd_audit_sanitation", ["audit-sanitation", "--record", "h", "backup.bin"]
        )
        fake.assert_called_once_with(backup="backup.bin", record_hash="h")  # type: ignore[attr-defined]
        _, fake = self._dispatch(
            "cmd_audit_sanitation", ["audit-sanitation", "backup.bin", "--record", "h"]
        )
        fake.assert_called_once_with(backup="backup.bin", record_hash="h")  # type: ignore[attr-defined]
        _, fake = self._dispatch("cmd_audit_retirement", ["audit-retirement"])
        fake.assert_called_once_with(None)  # type: ignore[attr-defined]
        _, fake = self._dispatch("cmd_audit_retirement", ["audit-retirement", "arch"])
        fake.assert_called_once_with("arch")  # type: ignore[attr-defined]

    def test_retire_and_install_flags(self) -> None:
        _, fake = self._dispatch(
            "cmd_retire", ["retire", "--archive-dir", "d", "--reason", "r", "--remediation", "m"]
        )
        fake.assert_called_once_with(archive_dir="d", reason="r", remediation="m")  # type: ignore[attr-defined]
        _, fake = self._dispatch(
            "cmd_install", ["install", "--project", "p", "--provider", "claude_code"]
        )
        fake.assert_called_once_with(project="p", provider="claude_code")  # type: ignore[attr-defined]
        _, fake = self._dispatch("cmd_uninstall", ["uninstall", "--project", "p"])
        fake.assert_called_once_with(project="p")  # type: ignore[attr-defined]


class TestRealInterface(unittest.TestCase):
    def test_python_m_cli_help_lists_the_omitted_commands(self) -> None:
        # The interface a user actually types, through a real process.
        proc = subprocess.run(
            [sys.executable, "-m", "cli", "--help"],
            cwd=str(_REPO_ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            stdin=subprocess.DEVNULL,
            timeout=60,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("verify-purge", proc.stdout)
        self.assertIn("verify-sanitation-binding", proc.stdout)

    def test_python_m_cli_bad_flag_exits_two_with_usage(self) -> None:
        proc = subprocess.run(
            [sys.executable, "-m", "cli", "stats", "--verbose"],
            cwd=str(_REPO_ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            stdin=subprocess.DEVNULL,
            timeout=60,
        )
        self.assertEqual(proc.returncode, 2)
        self.assertIn("usage:", proc.stderr)
        self.assertEqual(proc.stdout, "")


if __name__ == "__main__":
    unittest.main()
