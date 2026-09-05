"""Tests for the one-time private-ledger upgrade path.

The hazard these cover is silence. A clone made before the ledger went
private loses its chain from the working tree when it checks out the switch,
because those files stop being tracked. Nothing in git can repopulate
``.bench/`` since it is ignored, so without a migration the clone resolves to
an empty chain and its next governed edit opens a fresh GENESIS without
saying anything. Every branch below exists to make that failure loud or
impossible.

Run: python -m unittest tests.test_migrate -v
"""

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

_REPO_ROOT: Path = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from ledger.migrate import _run_git, migrate_ledger  # noqa: E402
from tests._ledger_fixtures import build_valid_chain  # noqa: E402


def _write_chain(
    directory: Path, count: int = 3, dag_entries: int = 0
) -> list[dict]:
    """Lay down a verifiable chain under ``directory``.

    Mirrors the real two-segment layout: the first ``count`` entries form
    the frozen legacy array, and any ``dag_entries`` beyond them are written
    one per file as ``entries/<entry_hash>.json``. The filename must equal
    the hash it contains or verify_chain reports FILENAME_MISMATCH, so a
    placeholder file would make every restore look partial.
    """
    directory.mkdir(parents=True, exist_ok=True)
    entries: list[dict] = build_valid_chain(count + dag_entries)
    (directory / "bench-ledger.json").write_text(
        json.dumps(entries[:count], indent=2), encoding="utf-8"
    )
    entries_dir: Path = directory / "entries"
    entries_dir.mkdir(exist_ok=True)
    for entry in entries[count:]:
        (entries_dir / f"{entry['entry_hash']}.json").write_text(
            json.dumps(entry, indent=2), encoding="utf-8"
        )
    return entries


class _MigrateTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._prev_override: str | None = os.environ.get("BENCH_LEDGER_PATH")
        self._tmp: Path = Path(tempfile.mkdtemp())
        self.repo: Path = self._tmp / "repo"
        self.repo.mkdir()
        self.target: Path = self.repo / ".bench"
        os.environ["BENCH_LEDGER_PATH"] = str(self.target / "bench-ledger.json")
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        if self._prev_override is None:
            os.environ.pop("BENCH_LEDGER_PATH", None)
        else:
            os.environ["BENCH_LEDGER_PATH"] = self._prev_override
        shutil.rmtree(self._tmp, ignore_errors=True)


class WorkingTreeSourceTests(_MigrateTestCase):
    def test_copies_chain_still_on_disk(self) -> None:
        _write_chain(self.repo / "ledger")

        result = migrate_ledger(self.repo)

        self.assertEqual(result["status"], "migrated")
        self.assertEqual(result["source"], "working tree")
        self.assertTrue(result["verified"])
        self.assertEqual(result["entries"], 3)
        self.assertTrue((self.target / "bench-ledger.json").exists())

    def test_nothing_to_migrate_when_no_chain_anywhere(self) -> None:
        result = migrate_ledger(self.repo)
        self.assertEqual(result["status"], "nothing_to_migrate")
        self.assertEqual(result["files"], 0)


class AlreadyStartedGuardTests(_MigrateTestCase):
    """A running chain must never be spliced with a restored history."""

    def test_refuses_when_legacy_segment_present_at_target(self) -> None:
        _write_chain(self.repo / "ledger")
        _write_chain(self.target)

        result = migrate_ledger(self.repo)

        self.assertEqual(result["status"], "already_migrated")
        self.assertEqual(result["files"], 0)

    def test_refuses_when_only_entries_dir_is_populated(self) -> None:
        """A post-switch chain has no legacy segment, only entries/.

        Checking for bench-ledger.json alone would miss it and splice a
        restored history into a chain that is already running.
        """
        _write_chain(self.repo / "ledger")
        entries_dir: Path = self.target / "entries"
        entries_dir.mkdir(parents=True)
        (entries_dir / f"{'a' * 64}.json").write_text("{}", encoding="utf-8")

        result = migrate_ledger(self.repo)

        self.assertEqual(result["status"], "already_migrated")
        self.assertFalse((self.target / "bench-ledger.json").exists())


class GitHistorySourceTests(_MigrateTestCase):
    """The case that matters: the clone already pulled and git deleted it."""

    def _git(self, *args: str) -> None:
        subprocess.run(
            ["git", *args],
            cwd=str(self.repo),
            capture_output=True,
            text=True,
            check=True,
        )

    def setUp(self) -> None:
        super().setUp()
        try:
            self._git("init", "-q")
            self._git("config", "user.email", "t@example.com")
            self._git("config", "user.name", "t")
        except (subprocess.CalledProcessError, FileNotFoundError):
            self.skipTest("git unavailable")

    def test_restores_from_the_last_commit_that_carried_the_chain(self) -> None:
        """The deleting commit's tree no longer holds the chain.

        git log -- <path> names that commit first, so restoring from it
        would find nothing and report a clean migration of zero files.
        """
        legacy: Path = self.repo / "ledger"
        entries: list[dict] = _write_chain(legacy, count=3, dag_entries=1)
        dag_name: str = f"{entries[3]['entry_hash']}.json"
        self._git("add", "-A")
        self._git("commit", "-q", "-m", "chain")

        # The switch: untrack and delete, exactly what checkout does.
        self._git("rm", "-r", "-q", "--cached", "ledger")
        shutil.rmtree(legacy)
        self._git("commit", "-q", "-m", "untrack")

        result = migrate_ledger(self.repo)

        self.assertEqual(result["status"], "migrated")
        self.assertTrue(result["source"].startswith("git history at"))
        self.assertTrue(result["verified"])
        self.assertEqual(result["entries"], 4)
        self.assertTrue((self.target / "bench-ledger.json").exists())
        self.assertTrue((self.target / "entries" / dag_name).exists())

    def _commit_then_untrack(self) -> None:
        legacy: Path = self.repo / "ledger"
        _write_chain(legacy)
        (legacy / "entries" / f"{'c' * 64}.json").write_text(
            "{}", encoding="utf-8"
        )
        self._git("add", "-A")
        self._git("commit", "-q", "-m", "chain")
        self._git("rm", "-r", "-q", "--cached", "ledger")
        shutil.rmtree(legacy)
        self._git("commit", "-q", "-m", "untrack")

    def test_git_timeout_is_a_failed_step_not_a_hang(self) -> None:
        """A git call that never returns ends as (1, "") with a stderr line."""
        err = io.StringIO()
        with patch(
            "ledger.migrate.subprocess.run",
            side_effect=subprocess.TimeoutExpired(["git", "log"], 60),
        ):
            with redirect_stderr(err):
                code, out = _run_git(["log", "-1"], self.repo)
        self.assertEqual((code, out), (1, ""))
        self.assertIn("did not finish within", err.getvalue())

    def test_git_calls_carry_a_timeout_and_detach_stdin(self) -> None:
        with patch("ledger.migrate.subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, "", "")
            _run_git(["status"], self.repo)
        self.assertGreater(run.call_args.kwargs.get("timeout", 0), 0)
        self.assertEqual(run.call_args.kwargs.get("stdin"), subprocess.DEVNULL)

    def test_partial_restore_is_not_reported_as_migrated(self) -> None:
        """A read failure must not satisfy written == expected."""
        self._commit_then_untrack()
        real = subprocess.run

        def flaky(args, **kwargs):
            joined: str = " ".join(str(a) for a in args)
            if "show" in args and "c" * 64 in joined:
                return subprocess.CompletedProcess(args, 1, "", "boom")
            return real(args, **kwargs)

        with patch("ledger.migrate.subprocess.run", side_effect=flaky):
            result = migrate_ledger(self.repo)

        self.assertEqual(result["status"], "partial")
        self.assertLess(result["files"], result["expected"])

    def test_enumeration_failure_is_not_treated_as_an_empty_chain(self) -> None:
        """Total failure must be distinguishable from a clean empty restore.

        Returning (0, 0) here would satisfy written == expected and report a
        complete migration of nothing, after which the next governed edit
        would fork the chain.
        """
        self._commit_then_untrack()
        real = subprocess.run

        def no_ls_tree(args, **kwargs):
            if "ls-tree" in args:
                return subprocess.CompletedProcess(args, 1, "", "boom")
            return real(args, **kwargs)

        with patch("ledger.migrate.subprocess.run", side_effect=no_ls_tree):
            result = migrate_ledger(self.repo)

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["failure_type"], "ENUMERATION_FAILED")
        self.assertFalse(result["verified"])
        self.assertNotEqual(result["status"], "migrated")


if __name__ == "__main__":
    unittest.main()
