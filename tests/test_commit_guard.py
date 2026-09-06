"""Tests for the ledger commit guard (hooks/pre-commit) and its shim.

The guard is a shell script, so it is exercised through git itself: a scratch
repository gets the guard as its pre-commit hook, a ledger-shaped file is
force-staged, and a commit is attempted. The regression this file exists for
is the closed-pipe case. When whatever reads git's stderr has stopped
reading, the guard's refusal message fails to write; without ``trap '' PIPE``
SIGPIPE kills the shell before ``exit 1`` and the commit lands, with git
reporting success (Windows git 2.51, three of three). Here stderr is a pipe
whose read end is closed before git starts, so the first write fails
deterministically.

Every git call pins GIT_CONFIG_GLOBAL to an empty file and sets
GIT_CONFIG_NOSYSTEM, so a developer's global hooks configuration cannot
redirect anything, and identity comes from ``-c`` flags.

Run: python -m unittest tests.test_commit_guard -v
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

_REPO_ROOT: Path = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

_GUARD: Path = _REPO_ROOT / "hooks" / "pre-commit"
_SHIM_DIR: Path = _REPO_ROOT / "scripts" / "githooks"
_TRAP_LINE: str = "trap '' PIPE"
_HAVE_GIT: bool = shutil.which("git") is not None


class _GuardCase(unittest.TestCase):
    """A scratch repository with one ledger-shaped file staged."""

    def setUp(self) -> None:
        if not _HAVE_GIT:
            self.skipTest("git is not installed")
        self.tmp: Path = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        empty_config: Path = self.tmp / "gitconfig"
        empty_config.write_text("", encoding="utf-8")
        self.env: dict[str, str] = dict(
            os.environ, GIT_CONFIG_GLOBAL=str(empty_config), GIT_CONFIG_NOSYSTEM="1"
        )
        self.repo: Path = self.tmp / "repo"
        self.repo.mkdir()
        self._git("init", "-q", check=True)
        entry: Path = self.repo / ".bench" / "entries" / "e.json"
        entry.parent.mkdir(parents=True)
        entry.write_text("{}\n", encoding="utf-8")
        self._git("add", "-f", ".bench/entries/e.json", check=True)

    def _git(self, *args: str, **kwargs: object) -> subprocess.CompletedProcess[str]:
        kwargs.setdefault("capture_output", True)
        return subprocess.run(  # type: ignore[call-overload,no-any-return]
            ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
            cwd=str(self.repo),
            env=self.env,
            text=True,
            encoding="utf-8",
            stdin=subprocess.DEVNULL,
            timeout=60,
            **kwargs,
        )

    def _install_hook(self, text: str) -> None:
        hook: Path = self.repo / ".git" / "hooks" / "pre-commit"
        hook.parent.mkdir(parents=True, exist_ok=True)
        hook.write_text(text, encoding="utf-8", newline="\n")
        hook.chmod(0o755)

    def _commit_with_closed_stderr(self) -> int:
        """Attempt the commit with stderr already unreadable. Returns git's
        exit code; ``_commits()`` tells whether the commit landed."""
        read_end, write_end = os.pipe()
        os.close(read_end)
        try:
            proc = self._git(
                "commit", "-q", "-m", "leak", capture_output=False,
                stdout=subprocess.DEVNULL, stderr=write_end,
            )
        finally:
            os.close(write_end)
        return proc.returncode

    def _commits(self) -> int:
        proc = self._git("rev-list", "--count", "HEAD")
        return int(proc.stdout.strip()) if proc.returncode == 0 else 0


class TestGuardBlocksWithClosedStderr(_GuardCase):
    def test_shipped_guard_refuses_when_nobody_reads_stderr(self) -> None:
        self._install_hook(_GUARD.read_text(encoding="utf-8"))
        code: int = self._commit_with_closed_stderr()
        self.assertNotEqual(code, 0, "git reported success with ledger data staged")
        self.assertEqual(self._commits(), 0, "the ledger entry was committed")

    def test_shipped_guard_refuses_with_a_terminal_reading(self) -> None:
        self._install_hook(_GUARD.read_text(encoding="utf-8"))
        proc = self._git("commit", "-q", "-m", "leak")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("stages operational ledger data", proc.stderr)
        self.assertEqual(self._commits(), 0)

    def test_the_guard_can_fail_without_the_trap(self) -> None:
        # Proof the closed-pipe test is not vacuous: the same script with the
        # trap removed is what leaked, three of three, on Windows git. Other
        # platforms deliver SIGPIPE differently and may block anyway, so the
        # leak is asserted only where it was reproduced.
        text: str = _GUARD.read_text(encoding="utf-8")
        self.assertIn(_TRAP_LINE, text)
        self._install_hook(text.replace(_TRAP_LINE + "\n", "", 1))
        self._commit_with_closed_stderr()
        leaked: bool = self._commits() == 1
        if os.name == "nt":
            self.assertTrue(leaked, "expected the un-hardened guard to leak on Windows")
        elif not leaked:
            self.skipTest("the un-hardened guard blocks on this platform; the leak reproduces on Windows")


class TestGuardText(unittest.TestCase):
    def test_trap_precedes_the_first_write_to_stderr(self) -> None:
        lines: list[str] = _GUARD.read_text(encoding="utf-8").splitlines()
        trap_at: int = next(i for i, line in enumerate(lines) if line.strip() == _TRAP_LINE)
        first_write: int = next(i for i, line in enumerate(lines) if ">&2" in line)
        self.assertLess(trap_at, first_write)
        self.assertEqual(lines[0], "#!/bin/sh")

    def test_shim_is_executable_in_git_and_forwards(self) -> None:
        shim: Path = _SHIM_DIR / "pre-commit"
        self.assertIn("hooks/pre-commit", shim.read_text(encoding="utf-8"))
        proc = subprocess.run(
            ["git", "ls-files", "-s", "scripts/githooks/pre-commit", "hooks/pre-commit"],
            cwd=str(_REPO_ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            stdin=subprocess.DEVNULL,
            timeout=60,
        )
        if proc.returncode != 0:
            self.skipTest(f"git ls-files unavailable: {proc.stderr.strip()}")
        modes: list[str] = [line.split()[0] for line in proc.stdout.splitlines()]
        self.assertEqual(modes, ["100755", "100755"], proc.stdout)


class TestShimForwards(_GuardCase):
    def test_hooks_path_at_the_shim_directory_still_guards(self) -> None:
        # A clone that enabled `git config core.hooksPath scripts/githooks`
        # before the guard moved must keep refusing through the shim.
        self._git("config", "core.hooksPath", _SHIM_DIR.as_posix(), check=True)
        proc = self._git("commit", "-q", "-m", "leak")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("stages operational ledger data", proc.stderr)
        self.assertEqual(self._commits(), 0)


if __name__ == "__main__":
    unittest.main()
