"""Tests for utils.owneronly: the viewer file is owner-only from creation.

The POSIX check reads the mode back. The Windows check reads the file's
ACL back through ``icacls`` (a test may spawn a process; the source tree
may not) and asserts exactly one entry, full control, for the user running
the test, with nothing inherited, on a file that has not been closed yet:
the restriction is part of creation, not applied after a write. It also
shows that a file created the ordinary way in the same directory inherits
entries, so the check is known to be able to fail. Each check runs only on
its own platform and is skipped on the other.

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

from utils.owneronly import open_owner_only  # noqa: E402


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


class OpenOwnerOnlyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp: Path = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.target: Path = self.tmp / "viewer.html"

    def _write(self, text: str) -> None:
        fd: int = open_owner_only(self.target)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)

    @unittest.skipUnless(os.name == "posix", "POSIX modes")
    def test_posix_file_is_created_0600(self) -> None:
        self._write("<!doctype html>")
        self.assertEqual(self.target.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.target.read_text(encoding="utf-8"), "<!doctype html>")

    @unittest.skipUnless(os.name == "nt", "Windows ACLs")
    def test_windows_file_is_owner_only_before_the_first_write(self) -> None:
        # Proof the check can fail: an ordinary file in the same directory
        # inherits entries for SYSTEM and Administrators.
        ordinary: Path = self.tmp / "ordinary.html"
        ordinary.write_text("x", encoding="utf-8")
        inherited: list[str] = _icacls(ordinary)
        self.assertTrue(any("(I)" in entry for entry in inherited), inherited)
        self.assertGreater(len(inherited), 1, inherited)

        fd: int = open_owner_only(self.target)
        try:
            # Nothing has been written yet and the handle is still open:
            # the DACL was applied at creation, not afterwards.
            after: list[str] = _icacls(self.target)
        finally:
            os.close(fd)
        self.assertEqual(len(after), 1, after)
        entry: str = after[0]
        self.assertTrue(entry.endswith(":(F)"), entry)
        self.assertNotIn("(I)", entry)
        principal: str = entry[: entry.index(":(")].lower()
        self.assertEqual(principal, _whoami())

    @unittest.skipUnless(os.name == "nt", "Windows ACLs")
    def test_windows_owner_can_write_and_read_the_file(self) -> None:
        self._write("<!doctype html>")
        self.assertEqual(self.target.read_text(encoding="utf-8"), "<!doctype html>")
        self.assertEqual(len(_icacls(self.target)), 1)

    def test_creation_is_exclusive(self) -> None:
        # A file already at the path is never written into: the caller
        # removes the old page first, and anything that reappears is an
        # error rather than a target.
        self.target.write_text("planted", encoding="utf-8")
        with self.assertRaises(OSError):
            open_owner_only(self.target)
        self.assertEqual(self.target.read_text(encoding="utf-8"), "planted")

    def test_missing_directory_is_an_oserror(self) -> None:
        with self.assertRaises(OSError):
            open_owner_only(self.tmp / "absent" / "viewer.html")


if __name__ == "__main__":
    unittest.main()
