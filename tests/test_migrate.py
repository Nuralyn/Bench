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

from ledger.migrate import GitTimeout, _run_git, migrate_ledger  # noqa: E402
from tests._ledger_fixtures import build_valid_chain  # noqa: E402


def _hang_on_show(nth: int):  # type: ignore[no-untyped-def]
    """A subprocess.run stand-in whose ``nth`` `git show` times out.

    Every other call goes to the real subprocess.run, so the probe and the
    enumeration answer and only the fetch hangs.
    """
    real = subprocess.run
    shows: list[int] = [0]

    def run(args, **kwargs):  # type: ignore[no-untyped-def]
        if "show" in args:
            shows[0] += 1
            if shows[0] == nth:
                raise subprocess.TimeoutExpired(args, 60)
        return real(args, **kwargs)

    return run


def _refuse(*_args, **_kwargs):  # type: ignore[no-untyped-def]
    """A filesystem call that fails, standing in for rmtree or unlink."""
    raise OSError("filesystem went away")


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

    def test_git_timeout_raises_a_typed_error_not_a_negative_answer(self) -> None:
        """A git call that never returns raises GitTimeout with a stderr line."""
        err = io.StringIO()
        with patch(
            "ledger.migrate.subprocess.run",
            side_effect=subprocess.TimeoutExpired(["git", "log"], 60),
        ):
            with redirect_stderr(err):
                with self.assertRaises(GitTimeout):
                    _run_git(["log", "-1"], self.repo)
        self.assertIn("did not finish within", err.getvalue())

    def test_timeout_during_history_probe_is_a_failed_migration(self) -> None:
        """Not nothing_to_migrate: that would let the next edit fork the chain."""
        self._commit_then_untrack()
        with patch(
            "ledger.migrate.subprocess.run",
            side_effect=subprocess.TimeoutExpired(["git", "log"], 60),
        ):
            with redirect_stderr(io.StringIO()):
                result = migrate_ledger(self.repo)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["failure_type"], "GIT_TIMEOUT")
        self.assertFalse((self.target / "bench-ledger.json").exists())

    def test_timeout_during_restore_is_a_failed_migration(self) -> None:
        """The probe answered; a cat-file or show that hangs still fails."""
        self._commit_then_untrack()
        real = subprocess.run

        def hang_on_show(args, **kwargs):  # type: ignore[no-untyped-def]
            if "show" in args:
                raise subprocess.TimeoutExpired(args, 60)
            return real(args, **kwargs)

        with patch("ledger.migrate.subprocess.run", side_effect=hang_on_show):
            with redirect_stderr(io.StringIO()):
                result = migrate_ledger(self.repo)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["failure_type"], "GIT_TIMEOUT")

    def test_timeout_mid_restore_leaves_no_partial_chain_behind(self) -> None:
        """Files written before the hang are removed, so a retry can succeed.

        Left in place, they would satisfy the already_started guard and the
        retry would report already_migrated over a truncated chain. A
        fully valid chain is committed here (the shared fixture carries a
        deliberately broken entry so partial restores stay partial), so
        the retry can be asserted as a complete, verified migration.
        """
        self._commit_valid_chain_then_untrack()
        hang_on_second_show = _hang_on_show(2)

        with patch("ledger.migrate.subprocess.run", side_effect=hang_on_second_show):
            with redirect_stderr(io.StringIO()):
                first = migrate_ledger(self.repo)
        self.assertEqual(first["status"], "failed")
        self.assertEqual(first["failure_type"], "GIT_TIMEOUT")
        # Nothing reached the target, and the staging directory is gone.
        self._assert_no_chain_in_target()
        self.assertEqual(self._staging_dirs(), [])

        # git is answering again: the retry restores the whole chain.
        with redirect_stderr(io.StringIO()):
            second = migrate_ledger(self.repo)
        self.assertEqual(second["status"], "migrated")
        self.assertTrue((self.target / "bench-ledger.json").exists())
        self.assertEqual(self._staging_dirs(), [])

    def _commit_valid_chain_then_untrack(self) -> None:
        legacy: Path = self.repo / "ledger"
        _write_chain(legacy, count=3, dag_entries=2)
        self._git("add", "-A")
        self._git("commit", "-q", "-m", "chain")
        self._git("rm", "-r", "-q", "--cached", "ledger")
        shutil.rmtree(legacy)
        self._git("commit", "-q", "-m", "untrack")

    def _staging_dirs(self) -> list[Path]:
        return sorted(self.target.glob(".restoring-*"))

    def _assert_no_chain_in_target(self) -> None:
        self.assertFalse((self.target / "bench-ledger.json").exists())
        self.assertEqual(list((self.target / "entries").glob("*.json")), [])
        self.assertFalse((self.target / "restore-incomplete").exists())

    def test_failed_cleanup_after_timeout_still_leaves_the_target_empty(self) -> None:
        """Debris from a cleanup that fails stays in staging, inside the
        gitignored target, never in the target's chain files.

        The next attempt still sees no chain, clears the stale staging
        directory it owns, and restores in full.
        """
        self._commit_valid_chain_then_untrack()
        hang_on_second_show = _hang_on_show(2)

        refuse = _refuse

        with patch("ledger.migrate.subprocess.run", side_effect=hang_on_second_show):
            with patch("ledger.migrate.shutil.rmtree", side_effect=refuse):
                with redirect_stderr(io.StringIO()):
                    first = migrate_ledger(self.repo)
        self.assertEqual(first["status"], "failed")
        self.assertEqual(first["failure_type"], "GIT_TIMEOUT")
        self._assert_no_chain_in_target()
        stale: list[Path] = self._staging_dirs()
        self.assertEqual(len(stale), 1)
        self.assertTrue((stale[0] / ".bench-restore").is_file())

        with redirect_stderr(io.StringIO()):
            second = migrate_ledger(self.repo)
        self.assertEqual(second["status"], "migrated")
        self.assertTrue(second["verified"])
        self.assertEqual(self._staging_dirs(), [])

    def test_staging_debris_is_ignored_even_under_an_unignored_ledger_path(
        self,
    ) -> None:
        """A custom BENCH_LEDGER_PATH may sit where nothing ignores it.

        The staging directory ignores itself, so the debris of a failed
        attempt never shows as untracked, and a `git add -A` cannot commit
        the diff bodies a restored entry carries.
        """
        custom: Path = self.repo / "custom-ledger"
        os.environ["BENCH_LEDGER_PATH"] = str(custom / "bench-ledger.json")
        self._commit_valid_chain_then_untrack()
        hang_on_second_show = _hang_on_show(2)

        refuse = _refuse

        with patch("ledger.migrate.subprocess.run", side_effect=hang_on_second_show):
            with patch("ledger.migrate.shutil.rmtree", side_effect=refuse):
                with redirect_stderr(io.StringIO()):
                    result = migrate_ledger(self.repo)
        self.assertEqual(result["status"], "failed")
        stale: list[Path] = sorted(custom.glob(".restoring-*"))
        self.assertEqual(len(stale), 1)
        self.assertEqual((stale[0] / ".gitignore").read_text(encoding="utf-8"), "*\n")
        self.assertTrue(any(stale[0].glob("*.json")) or any((stale[0] / "entries").glob("*.json")))

        status = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=all"],
            cwd=str(self.repo),
            capture_output=True,
            text=True,
            check=True,
            timeout=60,
        ).stdout
        self.assertNotIn(".restoring-", status)
        self.assertNotIn("custom-ledger", status)

    def test_a_directory_bench_did_not_create_is_never_removed(self) -> None:
        """Only a staging directory carrying the ownership marker is cleared."""
        self._commit_valid_chain_then_untrack()
        foreign: Path = self.target / ".restoring-user-data"
        foreign.mkdir(parents=True)
        (foreign / "keep.txt").write_text("mine", encoding="utf-8")

        with redirect_stderr(io.StringIO()):
            result = migrate_ledger(self.repo)
        self.assertEqual(result["status"], "migrated")
        self.assertEqual((foreign / "keep.txt").read_text(encoding="utf-8"), "mine")

    def test_failed_publish_is_rolled_back_and_the_retry_completes(self) -> None:
        """A rename that fails after others succeeded moves them back.

        Nothing stays in the target, so the retry sees no chain, and the
        staged files are still there for it to publish.
        """
        self._commit_valid_chain_then_untrack()
        real_replace = Path.replace
        moves: list[int] = [0]

        def fail_second_move(self_path, target):  # type: ignore[no-untyped-def]
            if ".restoring-" in str(self_path):
                moves[0] += 1
                if moves[0] == 2:
                    raise OSError("disk full")
            return real_replace(self_path, target)

        with patch("ledger.migrate.Path.replace", new=fail_second_move):
            with redirect_stderr(io.StringIO()):
                first = migrate_ledger(self.repo)
        self.assertEqual(first["status"], "partial")
        self.assertEqual(first["files"], 0)
        self._assert_no_chain_in_target()

        with redirect_stderr(io.StringIO()):
            second = migrate_ledger(self.repo)
        self.assertEqual(second["status"], "migrated")
        self.assertTrue(second["verified"])
        self.assertEqual(self._staging_dirs(), [])

    def test_marker_that_cannot_be_removed_is_not_a_successful_migration(
        self,
    ) -> None:
        """Every rename succeeded, the marker stays: that is a failure.

        Reporting migrated here would leave the CLI exiting 0 while every
        later run refuses with INCOMPLETE_RESTORE.
        """
        self._commit_valid_chain_then_untrack()
        real_unlink = Path.unlink

        def refuse_marker(self_path, *args, **kwargs):  # type: ignore[no-untyped-def]
            if self_path.name == "restore-incomplete":
                raise OSError("filesystem went away")
            return real_unlink(self_path, *args, **kwargs)

        with patch("ledger.migrate.Path.unlink", new=refuse_marker):
            with redirect_stderr(io.StringIO()):
                result = migrate_ledger(self.repo)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["failure_type"], "INCOMPLETE_RESTORE")
        self.assertTrue((self.target / "restore-incomplete").exists())
        # The files did arrive; only the marker is wrong, and the detail
        # tells the operator how to clear it after verifying.
        self.assertTrue((self.target / "bench-ledger.json").exists())
        self.assertIn("verify", result["detail"])

    def test_a_held_lock_refuses_a_second_migration(self) -> None:
        """Two runs cannot overlap: the second reports, it does not delete."""
        self._commit_valid_chain_then_untrack()
        self.target.mkdir(parents=True, exist_ok=True)
        lock: Path = self.target / ".migrate.lock"
        lock.write_text("12345", encoding="utf-8")
        live: Path = self.target / ".restoring-live"
        live.mkdir()
        (live / ".bench-restore").write_text("", encoding="utf-8")
        (live / "bench-ledger.json").write_text("[]", encoding="utf-8")

        with redirect_stderr(io.StringIO()):
            result = migrate_ledger(self.repo)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["failure_type"], "MIGRATION_IN_PROGRESS")
        self.assertIn(".migrate.lock", result["detail"])
        # The other attempt's staging and the lock are untouched.
        self.assertTrue((live / "bench-ledger.json").exists())
        self.assertEqual(lock.read_text(encoding="utf-8"), "12345")

        lock.unlink()
        with redirect_stderr(io.StringIO()):
            second = migrate_ledger(self.repo)
        self.assertEqual(second["status"], "migrated")
        self.assertFalse(lock.exists())
        self.assertFalse(live.exists())

    def test_existing_chain_is_reported_without_needing_a_lock(self) -> None:
        """A read-only directory that already holds a chain is a no-op.

        Idempotence does not depend on being able to write; the lock is
        only needed when a restore may happen.
        """
        _write_chain(self.target)
        with patch("ledger.migrate.os.open", side_effect=PermissionError("read-only")):
            with redirect_stderr(io.StringIO()):
                result = migrate_ledger(self.repo)
        self.assertEqual(result["status"], "already_migrated")
        self.assertFalse((self.target / ".migrate.lock").exists())

    def test_incomplete_marker_is_reported_without_needing_a_lock(self) -> None:
        self.target.mkdir(parents=True, exist_ok=True)
        (self.target / "restore-incomplete").write_text("", encoding="utf-8")
        with patch("ledger.migrate.os.open", side_effect=PermissionError("read-only")):
            with redirect_stderr(io.StringIO()):
                result = migrate_ledger(self.repo)
        self.assertEqual(result["failure_type"], "INCOMPLETE_RESTORE")

    def test_unwritable_target_without_a_chain_is_lock_failed(self) -> None:
        self._commit_valid_chain_then_untrack()
        with patch("ledger.migrate.os.open", side_effect=PermissionError("read-only")):
            with redirect_stderr(io.StringIO()):
                result = migrate_ledger(self.repo)
        self.assertEqual(result["failure_type"], "LOCK_FAILED")
        self.assertIn("writable", result["detail"])

    def test_lock_that_cannot_be_initialised_is_removed_not_left(self) -> None:
        """A pid write that fails must not leave a lock every retry trips on."""
        self._commit_valid_chain_then_untrack()
        with patch("ledger.migrate.os.fdopen", side_effect=OSError("quota")):
            with redirect_stderr(io.StringIO()):
                result = migrate_ledger(self.repo)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["failure_type"], "LOCK_FAILED")
        self.assertFalse((self.target / ".migrate.lock").exists())
        # And the next run, with the filesystem behaving, completes.
        with redirect_stderr(io.StringIO()):
            second = migrate_ledger(self.repo)
        self.assertEqual(second["status"], "migrated")

    def test_lock_that_cannot_be_released_turns_success_into_failure(self) -> None:
        """A migrated chain behind a stuck lock is reported as a failure.

        Otherwise the CLI exits 0 while every later run refuses with
        MIGRATION_IN_PROGRESS.
        """
        self._commit_valid_chain_then_untrack()
        real_unlink = Path.unlink

        def refuse_lock(self_path, *args, **kwargs):  # type: ignore[no-untyped-def]
            if self_path.name == ".migrate.lock":
                raise OSError("filesystem went away")
            return real_unlink(self_path, *args, **kwargs)

        with patch("ledger.migrate.Path.unlink", new=refuse_lock):
            with redirect_stderr(io.StringIO()):
                result = migrate_ledger(self.repo)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["failure_type"], "LOCK_NOT_RELEASED")
        self.assertIn("migrated", result["detail"])
        self.assertTrue((self.target / ".migrate.lock").exists())
        self.assertTrue((self.target / "bench-ledger.json").exists())

    def test_stuck_lock_after_a_failed_run_is_the_headline(self) -> None:
        """A retry note is useless if every retry is refused by the lock."""
        self._commit_valid_chain_then_untrack()
        real_unlink = Path.unlink

        def refuse_lock(self_path, *args, **kwargs):  # type: ignore[no-untyped-def]
            if self_path.name == ".migrate.lock":
                raise OSError("filesystem went away")
            return real_unlink(self_path, *args, **kwargs)

        with patch("ledger.migrate.subprocess.run", side_effect=_hang_on_show(1)):
            with patch("ledger.migrate.Path.unlink", new=refuse_lock):
                with redirect_stderr(io.StringIO()):
                    result = migrate_ledger(self.repo)
        self.assertEqual(result["failure_type"], "LOCK_NOT_RELEASED")
        self.assertIn("GIT_TIMEOUT", result["detail"])
        self.assertIn("removed by hand", result["detail"])

    def test_lock_is_released_after_a_failed_run(self) -> None:
        self._commit_valid_chain_then_untrack()
        with patch("ledger.migrate.subprocess.run", side_effect=_hang_on_show(1)):
            with redirect_stderr(io.StringIO()):
                result = migrate_ledger(self.repo)
        self.assertEqual(result["failure_type"], "GIT_TIMEOUT")
        self.assertFalse((self.target / ".migrate.lock").exists())

    def test_incomplete_marker_blocks_already_migrated(self) -> None:
        """A publish that could not be rolled back is a failure, not a chain."""
        self._commit_valid_chain_then_untrack()
        self.target.mkdir(parents=True, exist_ok=True)
        (self.target / "restore-incomplete").write_text("", encoding="utf-8")
        (self.target / "bench-ledger.json").write_text("[]", encoding="utf-8")

        with redirect_stderr(io.StringIO()):
            result = migrate_ledger(self.repo)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["failure_type"], "INCOMPLETE_RESTORE")

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
