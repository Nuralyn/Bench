"""Tests for cli.commands: exit codes and key output of each command.

The data loaders (load_ledger, verify_chain, load_constitution_snapshot,
generate_viewer_html) are patched at the cli.commands import site so the
tests are independent of the working directory and the real ledger.

Run: python -m unittest discover -s tests -p test_commands.py -v
"""

import io
import subprocess
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

_REPO_ROOT: Path = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from cli.commands import (  # noqa: E402
    _gh_status,
    _git_refs,
    cmd_constitution,
    cmd_ledger,
    cmd_migrate_ledger,
    cmd_stats,
    cmd_verify,
    cmd_viewer,
)
from ledger.chain import LedgerReadError  # noqa: E402
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

    def test_forked_chain_lists_every_tip(self) -> None:
        """Two tips is a legitimate post-merge state, not a failure.

        It must be visible rather than implied: a single collapsed 'latest
        hash' would hide that the chain has two heads awaiting reconciliation.
        """
        forked: dict = _valid_verify()
        forked["tips"] = ["a" * 64, "b" * 64]
        forked["latest_hash"] = ""

        out = io.StringIO()
        with patch("cli.commands.verify_chain", return_value=forked):
            with redirect_stdout(out):
                code: int = cmd_verify()

        text: str = out.getvalue()
        self.assertEqual(code, 0)
        self.assertIn("Ledger: VALID", text)
        self.assertIn("tips         : 2 (merged branches)", text)
        self.assertIn("a" * 64, text)
        self.assertIn("b" * 64, text)
        self.assertNotIn("latest hash", text)

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

    def test_unreadable_ledger_exits_one(self) -> None:
        err = io.StringIO()
        with patch(
            "cli.commands.load_ledger",
            side_effect=LedgerReadError("corrupted ledger at x"),
        ):
            with redirect_stderr(err):
                code: int = cmd_ledger()
        self.assertEqual(code, 1)
        self.assertIn("cannot read ledger", err.getvalue())


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
        # Repaired responses print beside pipeline errors, since each would
        # otherwise have been one.
        self.assertIn("Normalized responses   : 0", text)
        # The cost lines the README quotes are always printed, as a figure
        # or as an explicit n/a, so a reader can reproduce or rule out each.
        self.assertIn("Tokens per edit        : ", text)
        self.assertIn("Seconds per edit       : ", text)

    def test_stats_prints_token_and_timing_distributions(self) -> None:
        entries: list[dict] = _entries()
        entries[0]["challenger"] = {"_tokens": {"input": 100, "output": 20}, "_seconds": 4.0}
        entries[0]["oracle"] = {"_tokens": {"input": 300, "output": 80}, "_seconds": 6.0}
        entries[1]["oracle"] = {"_tokens": {"input": 1000, "output": 0}, "_seconds": 30.0}
        out = io.StringIO()
        with patch("cli.commands.load_ledger", return_value=entries):
            with patch("cli.commands.verify_chain", return_value=_valid_verify()):
                with redirect_stdout(out):
                    cmd_stats()
        text: str = out.getvalue()
        # Per-entry totals 500 and 1,000 tokens; 10.0 and 30.0 seconds.
        self.assertIn("Tokens per edit        : median 750, p90 1,000 (2 with usage)", text)
        self.assertIn("Seconds per edit       : median 20.0, p90 30.0 (2 timed)", text)
        self.assertIn(
            "Seconds by stage       : challenger 4.0/4.0, defender 0.0/0.0, "
            "oracle 18.0/30.0 (median/p90)",
            text,
        )

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


class ProbeTimeoutTests(unittest.TestCase):
    """A git or gh probe that hangs ends as a failed probe, not a hung CLI.

    Both helpers feed a gate that must not mistake "no answer" for an
    answer: _git_refs returns None (not an empty map) and _gh_status returns
    0 (the value classify_removal reads as inconclusive), each with a
    stderr line naming the timeout.
    """

    def test_git_refs_timeout_returns_none_and_logs(self) -> None:
        err = io.StringIO()
        with patch(
            "cli.commands.subprocess.run",
            side_effect=subprocess.TimeoutExpired(["git", "ls-remote"], 60),
        ):
            with redirect_stderr(err):
                result = _git_refs(["git", "ls-remote", "origin"])
        self.assertIsNone(result)
        self.assertIn("did not answer within", err.getvalue())

    def test_gh_status_timeout_returns_zero_and_logs(self) -> None:
        err = io.StringIO()
        with patch(
            "cli.commands.subprocess.run",
            side_effect=subprocess.TimeoutExpired(["gh", "api"], 60),
        ):
            with redirect_stderr(err):
                status: int = _gh_status("repos/x/y/commits/abc")
        self.assertEqual(status, 0)
        self.assertIn("did not answer within", err.getvalue())

    def test_probes_pass_a_timeout_and_detach_stdin(self) -> None:
        # Detached stdin makes a credential or auth prompt fail at once
        # instead of holding the probe open until the timeout.
        with patch("cli.commands.subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, "", "")
            _git_refs(["git", "ls-remote", "origin"])
            _gh_status("repos/x/y/commits/abc")
        self.assertEqual(len(run.call_args_list), 2)
        for call in run.call_args_list:
            self.assertGreater(call.kwargs.get("timeout", 0), 0)
            self.assertEqual(call.kwargs.get("stdin"), subprocess.DEVNULL)


class CmdMigrateLedgerTests(unittest.TestCase):
    """A failed migration prints the result's own recovery note."""

    def _failed(self, failure_type: str, detail: str) -> dict:
        return {
            "status": "failed",
            "source": "git history",
            "target": "/tmp/x/.bench",
            "files": 0,
            "expected": 0,
            "verified": False,
            "entries": 0,
            "genesis_hash": "",
            "failure_type": failure_type,
            "detail": detail,
        }

    def test_failure_prints_type_and_detail_and_exits_one(self) -> None:
        err = io.StringIO()
        with patch(
            "cli.commands.migrate_ledger",
            return_value=self._failed("GIT_TIMEOUT", "git log did not finish. Retry."),
        ):
            with redirect_stdout(io.StringIO()), redirect_stderr(err):
                code: int = cmd_migrate_ledger()
        self.assertEqual(code, 1)
        self.assertIn("failure  : GIT_TIMEOUT", err.getvalue())
        self.assertIn("detail   : git log did not finish. Retry.", err.getvalue())

    def test_failure_without_detail_prints_no_detail_line(self) -> None:
        err = io.StringIO()
        with patch(
            "cli.commands.migrate_ledger",
            return_value=dict(self._failed("ENUMERATION_FAILED", ""), detail=""),
        ):
            with redirect_stdout(io.StringIO()), redirect_stderr(err):
                cmd_migrate_ledger()
        self.assertNotIn("detail   :", err.getvalue())


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

    def test_constitution_shows_rationale_and_commentary_apart_from_rule(
        self,
    ) -> None:
        """The auditor sees which text binds and which the models never read."""
        out = io.StringIO()
        constitution: dict = {
            "constitution": "Bench",
            "version": "7",
            "constraints": [
                {
                    "id": "C-008",
                    "name": "Ledger Immutability",
                    "severity": "veto",
                    "rule": "Append only.",
                    "rationale": "Evidence must not be tampered with.",
                    "commentary": "An auditor runs verify_chain. " * 10,
                },
                {
                    "id": "C-001",
                    "name": "No silent errors",
                    "severity": "veto",
                    "rule": "Catch blocks must log, re-throw, or return.",
                },
            ],
        }
        with patch(
            "cli.commands.load_governing_constitution",
            return_value=(constitution, "deadbeef", []),
        ):
            with redirect_stdout(out):
                code: int = cmd_constitution()
        self.assertEqual(code, 0)
        text: str = out.getvalue()
        self.assertIn("rule: Append only.", text)
        self.assertIn(
            "rationale (not sent to models): Evidence must not be tampered with.",
            text,
        )
        self.assertIn(
            "commentary (not sent to models): An auditor runs verify_chain.", text
        )
        self.assertIn("...", text)  # long commentary is cut to a preview
        # The constraint without either field prints neither label.
        self.assertEqual(text.count("(not sent to models)"), 2)

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
    def test_writes_html_beside_the_ledger_and_exits_zero(self) -> None:
        """The page lands in the ledger directory, created if needed.

        It embeds every diff body the chain holds, so it belongs beside the
        gitignored chain rather than in the system temp directory, where the
        old implementation left it with no cleanup (audit finding 8).
        """
        tmp: str = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        ledger_path: str = os.path.join(tmp, ".bench", "bench-ledger.json")
        target: Path = Path(tmp) / ".bench" / "viewer.html"
        out = io.StringIO()
        with patch(
            "cli.commands.generate_viewer_html",
            return_value="<!doctype html><title>t</title>",
        ), patch(
            "cli.commands.resolve_ledger_path", return_value=ledger_path
        ), patch(
            "cli.commands.webbrowser.open", return_value=True
        ) as opened:
            with redirect_stdout(out):
                code: int = cmd_viewer()
        self.assertEqual(code, 0)
        self.assertIn(f"Bench viewer written to: {target}", out.getvalue())
        self.assertEqual(
            target.read_text(encoding="utf-8"),
            "<!doctype html><title>t</title>",
        )
        if os.name == "posix":
            # Owner-only, like the ledger's entry files. Windows keeps only
            # the read-only bit, so there is nothing to assert there.
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)
        opened.assert_called_once_with(target.resolve().as_uri())

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
