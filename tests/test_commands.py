"""Tests for cli.commands: exit codes and key output of each command.

The data loaders (load_ledger, verify_chain, load_constitution_snapshot,
generate_viewer_html) are patched at the cli.commands import site so the
tests are independent of the working directory and the real ledger.

Run: python -m unittest discover -s tests -p test_commands.py -v
"""

import io
import os
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

_REPO_ROOT: Path = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from cli.commands import (  # noqa: E402
    cmd_constitution,
    cmd_ledger,
    cmd_stats,
    cmd_verify,
    cmd_viewer,
)
from pipeline.constitution import ConstitutionError  # noqa: E402


def _valid_verify() -> dict:
    return {
        "valid": True,
        "entries": 2,
        "first_entry": "2026-01-01T00:00:00+00:00",
        "last_entry": "2026-01-01T00:00:01+00:00",
        "genesis_hash": "aaa",
        "latest_hash": "bbb",
        "meta": "meta anchor verified",
    }


def _entries() -> list[dict]:
    return [
        {
            "timestamp": "2026-01-01T00:00:00+00:00",
            "change": {"file": "a.py"},
            "oracle": {"verdict": "PASS"},
            "entry_hash": "a" * 64,
            "constitution_hash": "c" * 64,
        },
        {
            "timestamp": "2026-01-01T00:00:01+00:00",
            "change": {"file": "b.py"},
            "oracle": {
                "verdict": "VETO",
                "constraint_citations": [
                    {"constraint_id": "C-001", "disposition": "VIOLATED"}
                ],
            },
            "entry_hash": "b" * 64,
            "constitution_hash": "c" * 64,
        },
    ]


class CmdVerifyTests(unittest.TestCase):
    def test_valid_chain_exits_zero_and_prints_meta(self) -> None:
        out = io.StringIO()
        with patch("cli.commands.verify_chain", return_value=_valid_verify()):
            with redirect_stdout(out):
                code: int = cmd_verify()
        self.assertEqual(code, 0)
        self.assertIn("Ledger: VALID", out.getvalue())
        self.assertIn("meta anchor verified", out.getvalue())

    def test_empty_chain_exits_zero(self) -> None:
        out = io.StringIO()
        result: dict = {"valid": True, "entries": 0, "message": "empty"}
        with patch("cli.commands.verify_chain", return_value=result):
            with redirect_stdout(out):
                code: int = cmd_verify()
        self.assertEqual(code, 0)
        self.assertIn("EMPTY", out.getvalue())

    def test_invalid_chain_exits_one_and_reports_failure(self) -> None:
        err = io.StringIO()
        result: dict = {
            "valid": False,
            "entries_checked": 1,
            "failure_index": 1,
            "failure_type": "HASH_MISMATCH",
            "expected": "x",
            "found": "y",
            "message": "tampered",
        }
        with patch("cli.commands.verify_chain", return_value=result):
            with redirect_stderr(err):
                code: int = cmd_verify()
        self.assertEqual(code, 1)
        self.assertIn("HASH_MISMATCH", err.getvalue())


class CmdLedgerTests(unittest.TestCase):
    def test_empty_ledger_exits_zero(self) -> None:
        out = io.StringIO()
        with patch("cli.commands.load_ledger", return_value=[]):
            with redirect_stdout(out):
                code: int = cmd_ledger()
        self.assertEqual(code, 0)
        self.assertIn("empty", out.getvalue().lower())

    def test_prints_entries_with_verdicts(self) -> None:
        out = io.StringIO()
        with patch("cli.commands.load_ledger", return_value=_entries()):
            with redirect_stdout(out):
                code: int = cmd_ledger()
        self.assertEqual(code, 0)
        text: str = out.getvalue()
        self.assertIn("a.py", text)
        self.assertIn("VETO", text)
        self.assertIn("citations: C-001", text)

    def test_vetoes_only_filter(self) -> None:
        out = io.StringIO()
        with patch("cli.commands.load_ledger", return_value=_entries()):
            with redirect_stdout(out):
                code: int = cmd_ledger(vetoes_only=True)
        self.assertEqual(code, 0)
        text: str = out.getvalue()
        self.assertIn("b.py", text)
        self.assertNotIn("a.py", text)


class CmdStatsTests(unittest.TestCase):
    def test_stats_summary_and_exit_zero_on_valid_chain(self) -> None:
        out = io.StringIO()
        with patch("cli.commands.load_ledger", return_value=_entries()):
            with patch(
                "cli.commands.verify_chain", return_value=_valid_verify()
            ):
                with redirect_stdout(out):
                    code: int = cmd_stats()
        self.assertEqual(code, 0)
        text: str = out.getvalue()
        self.assertIn("Total governed changes : 2", text)
        self.assertIn("Passed                 : 1 (50.0%)", text)
        self.assertIn("Vetoed                 : 1 (50.0%)", text)
        self.assertIn("C-001", text)
        self.assertIn("Ledger integrity       : VALID", text)

    def test_exit_one_when_chain_invalid(self) -> None:
        out = io.StringIO()
        invalid: dict = {"valid": False, "failure_type": "CHAIN_BREAK"}
        with patch("cli.commands.load_ledger", return_value=_entries()):
            with patch("cli.commands.verify_chain", return_value=invalid):
                with redirect_stdout(out):
                    code: int = cmd_stats()
        self.assertEqual(code, 1)
        self.assertIn("INVALID (CHAIN_BREAK)", out.getvalue())

    def test_empty_ledger_exits_zero(self) -> None:
        out = io.StringIO()
        with patch("cli.commands.load_ledger", return_value=[]):
            with redirect_stdout(out):
                code: int = cmd_stats()
        self.assertEqual(code, 0)


class CmdConstitutionTests(unittest.TestCase):
    def test_prints_constraints_and_exits_zero(self) -> None:
        out = io.StringIO()
        constitution: dict = {
            "constitution": "Bench",
            "version": "1.0",
            "constraints": [
                {
                    "id": "C-001",
                    "name": "No silent errors",
                    "severity": "veto",
                    "rule": "Catch blocks must log, re-throw, or return.",
                }
            ],
        }
        with patch(
            "cli.commands.load_governing_constitution",
            return_value=(
                constitution,
                "deadbeef",
                [{"layer": "core", "path": "bench.json", "sha256": "deadbeef"}],
            ),
        ):
            with redirect_stdout(out):
                code: int = cmd_constitution()
        self.assertEqual(code, 0)
        text: str = out.getvalue()
        self.assertIn("Bench v1.0", text)
        self.assertIn("deadbeef", text)
        self.assertIn("C-001", text)
        self.assertIn("[VETO", text)
        self.assertIn("core", text)

    def test_constitution_shows_project_layer_and_raised_severity(self) -> None:
        """The auditor must surface what a project layer added or raised.

        Displaying the core alone would show a different constitution than the
        pipeline enforces, which is the divergence this command guards against.
        """
        out = io.StringIO()
        constitution: dict = {
            "constitution": "Bench+demo",
            "version": "1.0",
            "constraints": [
                {
                    "id": "C-005",
                    "name": "Test Coverage",
                    "severity": "veto",
                    "rule": "New logic carries tests.",
                    "severity_raised_by_project": True,
                },
                {
                    "id": "P-001",
                    "name": "No Raw SQL",
                    "severity": "veto",
                    "rule": "Use the query builder.",
                },
            ],
        }
        sources: list[dict] = [
            {"layer": "core", "path": "bench.json", "sha256": "aaaa1111"},
            {"layer": "project", "path": "/proj/bench.json", "sha256": "bbbb2222"},
        ]
        with patch(
            "cli.commands.load_governing_constitution",
            return_value=(constitution, "merged99", sources),
        ):
            with redirect_stdout(out):
                code: int = cmd_constitution()

        self.assertEqual(code, 0)
        text: str = out.getvalue()
        self.assertIn("project", text)
        self.assertIn("/proj/bench.json", text)
        self.assertIn("bbbb2222", text)
        self.assertIn("severity raised by project layer", text)
        self.assertIn("(project layer)", text)

    def test_constitution_tolerates_malformed_sources(self) -> None:
        """A non-dict source entry must not crash the auditor."""
        out = io.StringIO()
        constitution: dict = {
            "constitution": "Bench",
            "version": "1.0",
            "constraints": [],
        }
        with patch(
            "cli.commands.load_governing_constitution",
            return_value=(constitution, "deadbeef", ["not-a-dict", None]),
        ):
            with redirect_stdout(out):
                code: int = cmd_constitution()
        self.assertEqual(code, 0)

    def test_load_failure_exits_one(self) -> None:
        err = io.StringIO()
        with patch(
            "cli.commands.load_governing_constitution",
            side_effect=ConstitutionError("missing"),
        ):
            with redirect_stderr(err):
                code: int = cmd_constitution()
        self.assertEqual(code, 1)
        self.assertIn("constitution load failed", err.getvalue())


class CmdViewerTests(unittest.TestCase):
    def test_writes_html_and_exits_zero(self) -> None:
        out = io.StringIO()
        with patch(
            "cli.commands.generate_viewer_html",
            return_value="<!doctype html><title>t</title>",
        ):
            with patch("cli.commands.webbrowser.open", return_value=True):
                with redirect_stdout(out):
                    code: int = cmd_viewer()
        self.assertEqual(code, 0)
        text: str = out.getvalue()
        self.assertIn("Bench viewer written to:", text)
        tmp_path: str = text.split("Bench viewer written to:")[1].strip()
        self.addCleanup(
            lambda: os.path.exists(tmp_path) and os.remove(tmp_path)
        )
        written: str = Path(tmp_path).read_text(encoding="utf-8")
        self.assertIn("<!doctype html>", written)

    def test_generation_failure_exits_one(self) -> None:
        err = io.StringIO()
        with patch(
            "cli.commands.generate_viewer_html",
            side_effect=RuntimeError("boom"),
        ):
            with redirect_stderr(err):
                code: int = cmd_viewer()
        self.assertEqual(code, 1)
        self.assertIn("viewer generation failed", err.getvalue())


if __name__ == "__main__":
    unittest.main()
