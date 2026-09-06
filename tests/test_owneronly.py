"""Tests for utils.owneronly: the viewer file is readable by its owner only.

The POSIX check reads the mode back. The Windows check reads the file's
ACL back through ``icacls`` (a test may spawn a process; the source tree
may not) and asserts exactly one entry, full control, for the user running
the test, with nothing inherited. It also asserts the file was NOT that way
before the call, so the check is known to be able to fail. Each check runs
only on its own platform and is skipped on the other.

Run: python -m unittest tests.test_owneronly -v
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

from utils.owneronly import restrict_to_owner  # noqa: E402


def _icacls(path: Path) -> list[str]:
    """The file's ACL entries as icacls prints them, one principal per item."""
    proc = subprocess.run(
        ["icacls", str(path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdin=subprocess.DEVNULL,
        timeout=60,
    )
    if proc.returncode != 0:
        raise AssertionError(f"icacls failed: {proc.stderr.strip()}")
    entries: list[str] = []
    for line in proc.stdout.splitlines():
        text: str = line.strip()
        if text.startswith(str(path)):
            text = text[len(str(path)):].strip()
        if ":(" in text:
            entries.append(text)
    return entries


def _whoami() -> str:
    proc = subprocess.run(
        ["whoami"], capture_output=True, text=True, encoding="utf-8",
        stdin=subprocess.DEVNULL, timeout=60,
    )
    return proc.stdout.strip().lower()


class RestrictToOwnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp: Path = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.target: Path = self.tmp / "viewer.html"
        self.target.write_text("<!doctype html>", encoding="utf-8")

    @unittest.skipUnless(os.name == "posix", "POSIX modes")
    def test_posix_mode_is_owner_only(self) -> None:
        self.target.chmod(0o644)
        restrict_to_owner(self.target)
        self.assertEqual(self.target.stat().st_mode & 0o777, 0o600)

    @unittest.skipUnless(os.name == "nt", "Windows ACLs")
    def test_windows_acl_grants_the_current_user_only(self) -> None:
        # Proof the check can fail: a fresh file inherits entries for SYSTEM
        # and Administrators from its directory.
        before: list[str] = _icacls(self.target)
        self.assertTrue(any("(I)" in entry for entry in before), before)
        self.assertGreater(len(before), 1, before)

        restrict_to_owner(self.target)

        after: list[str] = _icacls(self.target)
        self.assertEqual(len(after), 1, after)
        entry: str = after[0]
        self.assertTrue(entry.endswith(":(F)"), entry)
        self.assertNotIn("(I)", entry)
        principal: str = entry[: entry.index(":(")].lower()
        self.assertEqual(principal, _whoami())
        # The owner can still read what was restricted to them.
        self.assertEqual(self.target.read_text(encoding="utf-8"), "<!doctype html>")

    @unittest.skipUnless(os.name == "nt", "Windows ACLs")
    def test_windows_restriction_is_idempotent_and_survives_a_rewrite(self) -> None:
        restrict_to_owner(self.target)
        # A rewrite through O_TRUNC keeps the ACL; the command still calls
        # restrict_to_owner again, which must not duplicate the entry.
        self.target.write_text("<!doctype html>v2", encoding="utf-8")
        restrict_to_owner(self.target)
        after: list[str] = _icacls(self.target)
        self.assertEqual(len(after), 1, after)
        self.assertEqual(self.target.read_text(encoding="utf-8"), "<!doctype html>v2")

    @unittest.skipUnless(os.name == "nt", "Windows ACLs")
    def test_windows_missing_file_is_an_oserror(self) -> None:
        with self.assertRaises(OSError):
            restrict_to_owner(self.tmp / "absent.html")


if __name__ == "__main__":
    unittest.main()
