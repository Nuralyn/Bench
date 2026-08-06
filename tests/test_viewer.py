"""Tests for utils.viewer: self-contained HTML ledger viewer.

Smoke-level coverage of generate_viewer_html against synthetic ledgers
on disk: document structure, stats banner values, chain status labels,
JSON embedding safety, and the never-raises error page fallback.

Synthetic chains come from the shared fixture module
tests/_ledger_fixtures.py (build_valid_chain), the single source of
truth for the entry shape.

Run: python -m unittest tests.test_viewer -v
"""

import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_REPO_ROOT: Path = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from tests._ledger_fixtures import build_valid_chain as _build_valid_chain  # noqa: E402
from cli.commands import cmd_stats  # noqa: E402
from ledger.chain import ANCHOR_VERDICT, compute_entry_hash  # noqa: E402
from ledger.retire import ANCHOR_TOOL  # noqa: E402
from utils.stats import compute_ledger_stats, pct  # noqa: E402
from utils.viewer import generate_viewer_html  # noqa: E402


class GenerateViewerHtmlTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp: str = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self._tmp)

    def _path(self) -> str:
        return os.path.join(self._tmp, "ledger.json")

    def _write_chain(self, chain: list[dict]) -> None:
        Path(self._path()).write_text(json.dumps(chain), encoding="utf-8")

    def test_missing_ledger_renders_empty_viewer(self) -> None:
        html_out: str = generate_viewer_html(self._path())
        self.assertIn("<!doctype html>", html_out)
        self.assertIn("Bench Verdict Viewer", html_out)
        self.assertIn("EMPTY", html_out)
        self.assertIn("const LEDGER_DATA = [];", html_out)

    def test_valid_ledger_renders_stats_and_chain_status(self) -> None:
        chain: list[dict] = _build_valid_chain(
            3, verdicts=["PASS", "VETO", "PASS"]
        )
        self._write_chain(chain)
        html_out: str = generate_viewer_html(self._path())
        self.assertIn('"status": "VALID"', html_out)
        self.assertIn("(66.7%)", html_out)
        self.assertIn("(33.3%)", html_out)
        self.assertIn("C-001 (1 veto(es))", html_out)
        self.assertIn("file_1.py", html_out)

    def test_multi_parent_entry_is_embedded_for_rendering(self) -> None:
        """An entry reconciling a merge carries a list of parent hashes.

        The renderer passes each parent to hashCopy individually; handing it
        the array would fall through to 'N/A' and silently hide the linkage
        that makes a merge auditable.
        """
        chain: list[dict] = _build_valid_chain(2)
        merge_entry: dict = dict(chain[-1])
        merge_entry["previous_hash"] = [
            chain[0]["entry_hash"],
            "b" * 64,
        ]
        merge_entry["entry_hash"] = compute_entry_hash(merge_entry)
        chain[-1] = merge_entry
        self._write_chain(chain)

        html_out: str = generate_viewer_html(self._path())
        self.assertIn("previous hashes", html_out)
        self.assertIn("Array.isArray(entry.previous_hash)", html_out)
        self.assertIn("b" * 64, html_out)

    def test_tampered_ledger_reports_broken_chain(self) -> None:
        chain: list[dict] = _build_valid_chain(3)
        chain[1]["change"]["file"] = "TAMPERED.py"
        self._write_chain(chain)
        html_out: str = generate_viewer_html(self._path())
        self.assertIn("BROKEN AT ENTRY #2", html_out)

    def test_script_close_tags_in_data_are_escaped(self) -> None:
        chain: list[dict] = _build_valid_chain(1)
        chain[0]["change"]["file"] = "evil</script><script>alert(1)"
        chain[0]["entry_hash"] = compute_entry_hash(chain[0])
        self._write_chain(chain)
        html_out: str = generate_viewer_html(self._path())
        self.assertNotIn("evil</script>", html_out)
        self.assertIn("evil<\\/script>", html_out)

    def test_generation_failure_returns_error_page(self) -> None:
        with patch(
            "utils.viewer.load_ledger", side_effect=RuntimeError("boom")
        ):
            html_out: str = generate_viewer_html(self._path())
        self.assertIn("generation failed", html_out)
        self.assertIn("RuntimeError: boom", html_out)


class ViewerStatsParityTests(unittest.TestCase):
    """The viewer banner and cmd_stats must report identical rates.

    Both surfaces consume utils.stats.compute_ledger_stats, which exists so
    they cannot drift apart; these tests lock the contract that both compute
    pass/veto percentages over adjudicated entries (excluding chain-retirement
    anchors), and pin pct() behavior when a chain holds only an anchor.
    """

    def setUp(self) -> None:
        self._tmp: str = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self._tmp)

    def _path(self) -> str:
        return os.path.join(self._tmp, "ledger.json")

    def _write_chain(self, chain: list[dict]) -> None:
        Path(self._path()).write_text(json.dumps(chain), encoding="utf-8")

    @staticmethod
    def _append_anchor(chain: list[dict]) -> None:
        """Append a chain-retirement anchor entry, correctly linked."""
        anchor: dict = {
            "entry_id": "id-anchor",
            "timestamp": "2026-01-01T00:01:00+00:00",
            "previous_hash": (
                chain[-1]["entry_hash"] if chain else "GENESIS"
            ),
            "constitution_hash": "abc",
            "verdict": ANCHOR_VERDICT,
            "change": {
                "file": "ledger/bench-ledger.json",
                "tool": ANCHOR_TOOL,
            },
            "oracle": {},
        }
        anchor["entry_hash"] = compute_entry_hash(anchor)
        chain.append(anchor)

    def test_banner_rates_match_cmd_stats_with_anchor_present(self) -> None:
        chain: list[dict] = _build_valid_chain(
            3, verdicts=["PASS", "VETO", "PASS"]
        )
        self._append_anchor(chain)
        self._write_chain(chain)
        html_out: str = generate_viewer_html(self._path())

        stats: dict = compute_ledger_stats(chain)
        self.assertEqual(stats["adjudicated"], 3)
        expected_passed: str = f"({pct(stats['passed'], stats['adjudicated'])})"
        expected_vetoed: str = f"({pct(stats['vetoed'], stats['adjudicated'])})"
        self.assertIn(expected_passed, html_out)  # (66.7%)
        self.assertIn(expected_vetoed, html_out)  # (33.3%)
        # Rates over total (4, anchor included) would be 50.0% and 25.0%;
        # their absence proves the anchor is excluded from the denominator.
        self.assertNotIn("(50.0%)", html_out)
        self.assertNotIn("(25.0%)", html_out)
        self.assertIn(
            '<div class="label">Governed changes</div>'
            '<div class="value">3</div>',
            html_out,
        )

        buf: io.StringIO = io.StringIO()
        with patch("cli.commands.load_ledger", return_value=chain), patch(
            "cli.commands.verify_chain", return_value={"valid": True}
        ), contextlib.redirect_stdout(buf):
            cmd_stats()
        cli_out: str = buf.getvalue()
        self.assertIn(expected_passed, cli_out)
        self.assertIn(expected_vetoed, cli_out)

    def test_anchor_only_ledger_renders_zero_rates(self) -> None:
        chain: list[dict] = []
        self._append_anchor(chain)
        self._write_chain(chain)
        html_out: str = generate_viewer_html(self._path())
        self.assertNotIn("generation failed", html_out)
        self.assertIn("(0.0%)", html_out)
        self.assertIn(
            '<div class="label">Governed changes</div>'
            '<div class="value">0</div>',
            html_out,
        )


if __name__ == "__main__":
    unittest.main()
