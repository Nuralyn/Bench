"""Guard: the committed ledger must never carry another project's source.

Bench's hook can be registered globally, so it governs files belonging to
other projects. Between April and July 2026 those changes were written, with
full diff bodies, into Bench's own ledger, which is committed to a public
repository. The routing fix (ledger.chain.resolve_ledger_path) stops other
projects' sessions from landing here, and redaction strips the body of any
out-of-project file still governed from a Bench session. This module is the
regression test that keeps both honest.

The invariant: an out-of-project file may appear in the ledger by path and
verdict, but never with its contents.

In-project files are always recorded as repository-relative paths, because
the hook normalizes them against the repo root. So an absolute path, or one
that climbs out with '..', must carry no body.

This is deliberately stricter than the runtime redactor, which compares a
path against the real project root and so keeps the body of an absolute path
that happens to point inside the repo. Here, an absolute path is treated as a
defect either way: it is a foreign file whose body must be stripped, or it is
an in-repo file that escaped path normalization. The retired chain contained
both kinds. Both are worth failing on, so the message below names both.

Absoluteness is detected with a regex rather than os.path.isabs: CI runs on
Linux, where isabs(r"C:\\Users\\...") is False, and a Windows-shaped path in
the ledger is exactly what this test exists to catch.

Run: python -m unittest tests.test_ledger_hygiene -v
"""

import json
import re
import sys
import unittest
from pathlib import Path

_REPO_ROOT: Path = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from ledger.chain import _EXTERNAL_BODY_KEYS  # noqa: E402

_LEDGER: Path = _REPO_ROOT / "ledger" / "bench-ledger.json"

# Drive-letter paths (C:\ or C:/), POSIX absolute paths, and UNC shares.
_ABSOLUTE: re.Pattern[str] = re.compile(r"^(?:[A-Za-z]:[\\/]|[\\/])")
# Paths that escape the project root.
_ESCAPES: re.Pattern[str] = re.compile(r"^\.\.[\\/]|^\.\.$")


def _is_out_of_project(file_ref: str) -> bool:
    """True when a ledger entry's file reference is not repo-relative."""
    if not file_ref or file_ref == "unknown":
        return False
    return bool(_ABSOLUTE.match(file_ref) or _ESCAPES.match(file_ref))


class CommittedLedgerHygieneTests(unittest.TestCase):
    def setUp(self) -> None:
        if not _LEDGER.exists():
            self.skipTest("no ledger in this checkout")
        self.entries: list[dict] = json.loads(
            _LEDGER.read_text(encoding="utf-8")
        )

    def test_out_of_project_entries_are_redacted(self) -> None:
        """No foreign file body may sit in the ledger, in any entry."""
        offenders: list[str] = []
        for index, entry in enumerate(self.entries):
            change: dict = entry.get("change") or {}
            file_ref: str = str(change.get("file", ""))
            if not _is_out_of_project(file_ref):
                continue
            summary = change.get("diff_summary")
            if not isinstance(summary, dict) or not summary.get("redacted"):
                offenders.append(f"entry {index}: unredacted body for {file_ref}")
                continue
            leaked: list[str] = sorted(
                set(summary) & set(_EXTERNAL_BODY_KEYS)
            )
            if leaked:
                offenders.append(
                    f"entry {index}: {file_ref} retains {leaked}"
                )
        self.assertEqual(
            offenders,
            [],
            "Non-relative file reference carrying a diff body in the ledger. "
            "Either a foreign file's source is about to be published, or an "
            "in-repo path escaped normalization. Investigate before "
            "committing:\n  " + "\n  ".join(offenders[:20]),
        )

    def test_redaction_marker_implies_no_body_keys(self) -> None:
        """Anything flagged redacted must actually have been stripped."""
        for index, entry in enumerate(self.entries):
            summary = (entry.get("change") or {}).get("diff_summary")
            if not isinstance(summary, dict) or not summary.get("redacted"):
                continue
            for key in _EXTERNAL_BODY_KEYS:
                self.assertNotIn(
                    key,
                    summary,
                    f"entry {index} claims redacted but still carries {key!r}",
                )

    def test_detector_recognizes_foreign_path_shapes(self) -> None:
        """The detector itself must catch both platforms' absolute paths.

        Without this, the guard above could pass on Linux CI simply by
        failing to recognize a Windows path as absolute.
        """
        for path in (
            r"C:\Users\someone\project\src\billing.ts",
            "C:/Users/someone/project/src/billing.ts",
            "/home/someone/project/src/billing.ts",
            r"\\server\share\file.py",
            r"..\other-project\main.py",
            "../other-project/main.py",
        ):
            self.assertTrue(
                _is_out_of_project(path), f"failed to flag {path!r}"
            )

        for path in (
            "utils/api.py",
            r"utils\api.py",
            "bench.json",
            "unknown",
            "",
        ):
            self.assertFalse(
                _is_out_of_project(path), f"wrongly flagged {path!r}"
            )


if __name__ == "__main__":
    unittest.main()
