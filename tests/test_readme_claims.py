"""Tests that pin README claims about runtime behavior to the code they describe.

Two README statements drifted from the code without anything noticing: the
ledger path table still named ``ledger/bench-ledger.json`` for Bench governing
itself after every chain moved to ``.bench/``, and the diff-hardening section
said truncation preserved imports when the code preserves a fixed head and
tail plus signatures and exception handlers. Prose does not fail in CI, so
these tests make the specific claims fail instead.

Each test reads the README and compares a stated figure or path against the
constant or function that actually decides it.

Run: python -m unittest tests.test_readme_claims -v
"""

import os
import re
import sys
import unittest
from pathlib import Path
from unittest import mock

_REPO_ROOT: Path = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from ledger import chain  # noqa: E402
from utils import diff  # noqa: E402

_README: Path = _REPO_ROOT / "README.md"


def _readme_text() -> str:
    return _README.read_text(encoding="utf-8")


def _section(text: str, heading: str) -> str:
    """Return the body of the ``### heading`` section, up to the next heading."""
    match = re.search(
        rf"^### {re.escape(heading)}\n(.*?)(?=^#{{1,3}} |\Z)",
        text,
        flags=re.MULTILINE | re.DOTALL,
    )
    if match is None:
        raise AssertionError(f"README has no '### {heading}' section")
    return match.group(1)


class TestLedgerPathTable(unittest.TestCase):
    """The Project-Scoped Ledger table must name the path the resolver returns."""

    def test_default_row_matches_resolver(self) -> None:
        body = _section(_readme_text(), "Project-Scoped Ledger")
        rows = re.findall(r"^\| (.+?) \| (.+?) \|$", body, flags=re.MULTILINE)
        cells = {label.strip(): value.strip() for label, value in rows}
        self.assertIn("Any project, Bench included", cells)

        with mock.patch.dict(os.environ, {"BENCH_LEDGER_PATH": ""}):
            resolved = Path(chain.resolve_ledger_path())
        root = chain._project_root()
        expected_suffix = resolved.relative_to(root).as_posix()

        stated = cells["Any project, Bench included"]
        self.assertEqual(stated, f"`<project>/{expected_suffix}`")

    def test_table_no_longer_claims_an_in_repo_exemption(self) -> None:
        body = _section(_readme_text(), "Project-Scoped Ledger")
        self.assertNotIn("ledger/bench-ledger.json", body)

    def test_override_row_names_the_env_var_the_resolver_reads(self) -> None:
        body = _section(_readme_text(), "Project-Scoped Ledger")
        self.assertIn("`BENCH_LEDGER_PATH` set", body)
        with mock.patch.dict(os.environ, {"BENCH_LEDGER_PATH": "/elsewhere/ledger.json"}):
            self.assertEqual(chain.resolve_ledger_path(), "/elsewhere/ledger.json")


class TestTruncationClaim(unittest.TestCase):
    """The Diff Hardening bullet must state the thresholds and kept lines the code uses."""

    def _bullet(self) -> str:
        body = _section(_readme_text(), "Diff Hardening")
        for line in body.splitlines():
            if line.startswith("- **Large diffs**"):
                return line
        raise AssertionError("README Diff Hardening section has no Large diffs bullet")

    def test_thresholds_match_constants(self) -> None:
        bullet = self._bullet()
        self.assertIn(f"{diff.MAX_DIFF_LINES} lines", bullet)
        self.assertIn(f"{diff.MAX_DIFF_CHARS:,} characters", bullet)

    def test_kept_window_matches_constants(self) -> None:
        bullet = self._bullet()
        self.assertIn(f"first {diff._FIRST_N} and last {diff._LAST_N} lines", bullet)

    def test_does_not_claim_imports_are_preserved_unconditionally(self) -> None:
        bullet = self._bullet()
        self.assertNotRegex(bullet, r"lines: imports")

    def test_selection_keeps_what_the_bullet_says_and_not_imports(self) -> None:
        head = [f"line {i}" for i in range(diff._FIRST_N)]
        middle = ["import late_module", "def kept_signature():", "    pass", "    except ValueError:"]
        filler = [f"filler {i}" for i in range(diff._LAST_N + 5)]
        lines = head + middle + filler
        keep = diff._select_preserved_lines(lines, len(lines))

        import_index = len(head)
        self.assertNotIn(import_index, keep)
        self.assertIn(import_index + 1, keep)
        self.assertIn(import_index + 3, keep)
        self.assertTrue(set(range(diff._FIRST_N)) <= keep)
        self.assertTrue(set(range(len(lines) - diff._LAST_N, len(lines))) <= keep)


if __name__ == "__main__":
    unittest.main()
