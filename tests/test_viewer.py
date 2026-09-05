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
from ledger.chain import (  # noqa: E402
    ANCHOR_VERDICT,
    compute_entry_hash,
    resolve_entries_dir,
)
from ledger.retire import ANCHOR_TOOL  # noqa: E402
from utils.stats import (  # noqa: E402
    citations_by_constraint,
    compute_ledger_stats,
    pct,
    stats_by_scope,
    stats_by_week,
)
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

    def test_entries_dir_failure_reports_type_not_entry_zero(self) -> None:
        """A failure with no array position names its type, not "#0".

        verify_chain reports entries-directory failures (MISSING_PARENT,
        ORPHAN_ENTRY, DUPLICATE_ENTRY, FILENAME_MISMATCH) with failure_index
        -1 because no position in the legacy array applies. The banner used
        to format that as "BROKEN AT ENTRY #0" and dropped the message that
        names the offending hash (audit finding 4).
        """
        chain: list[dict] = _build_valid_chain(2)
        self._write_chain(chain)
        entries_dir: Path = Path(resolve_entries_dir(self._path()))
        entries_dir.mkdir()
        orphan: dict = dict(chain[1])
        orphan["entry_id"] = "id-orphan"
        orphan["previous_hash"] = ["f" * 64]
        orphan["entry_hash"] = compute_entry_hash(orphan)
        (entries_dir / f"{orphan['entry_hash']}.json").write_text(
            json.dumps(orphan), encoding="utf-8"
        )
        html_out: str = generate_viewer_html(self._path())
        self.assertIn("BROKEN (MISSING_PARENT)", html_out)
        self.assertNotIn("ENTRY #0", html_out)
        self.assertIn('<div class="note">Entry ', html_out)

    def test_markup_in_data_is_unicode_escaped(self) -> None:
        """Every ``<``, ``>``, and ``&`` in embedded data is a \\uXXXX escape.

        Escaping only ``</script`` is not enough. An Edit whose old_string
        holds ``<!--<script`` with no closing ``-->`` in the same snippet
        puts the HTML parser into the script-data-double-escaped state, the
        page's own closing ``</script>`` is swallowed into the script, and
        the viewer renders a banner over zero entries with no console error
        (audit finding 1, reproduced in Chrome).
        """
        chain: list[dict] = _build_valid_chain(2)
        chain[0]["change"]["file"] = "evil</script><script>alert(1)"
        chain[0]["entry_hash"] = compute_entry_hash(chain[0])
        chain[1]["previous_hash"] = chain[0]["entry_hash"]
        chain[1]["change"]["diff_summary"] = {
            "file_path": "index.html",
            "change_type": "modify",
            "old_string": '<!--<script src="a.js">',
            "new_string": "a & b",
        }
        chain[1]["entry_hash"] = compute_entry_hash(chain[1])
        self._write_chain(chain)
        html_out: str = generate_viewer_html(self._path())
        script: str = html_out.split("<script>", 1)[1]
        data: str = script.split("const CHAIN_STATUS", 1)[0]
        self.assertNotIn("<", data)
        self.assertNotIn(">", data)
        self.assertNotIn("&", data)
        self.assertIn("evil\\u003C/script\\u003E", html_out)
        self.assertIn("\\u003C!--\\u003Cscript", html_out)
        # The escapes are plain JSON, so the browser parses back exactly
        # what was written to disk.
        payload: str = data.split("=", 1)[1].strip().rstrip(";")
        self.assertEqual(json.loads(payload), chain)

    def test_generation_failure_returns_error_page(self) -> None:
        with patch(
            "utils.viewer.load_ledger", side_effect=RuntimeError("boom")
        ):
            html_out: str = generate_viewer_html(self._path())
        self.assertIn("generation failed", html_out)
        self.assertIn("RuntimeError: boom", html_out)


class DashboardTests(unittest.TestCase):
    """The dashboard restates utils.stats; it never counts on its own."""

    def setUp(self) -> None:
        self._tmp: str = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self._tmp)

    def _path(self) -> str:
        return os.path.join(self._tmp, "ledger.json")

    def _render(self, chain: list[dict]) -> str:
        Path(self._path()).write_text(json.dumps(chain), encoding="utf-8")
        return generate_viewer_html(self._path())

    @staticmethod
    def _row(cells: list[str]) -> str:
        return "<tr>" + "".join(f"<td>{c}</td>" for c in cells) + "</tr>"

    def test_weekly_and_scope_tables_match_the_helpers(self) -> None:
        chain: list[dict] = _build_valid_chain(
            3, verdicts=["PASS", "VETO", "PASS"]
        )
        chain[1]["change"]["file"] = "pipeline/oracle.py"
        chain[1]["entry_hash"] = compute_entry_hash(chain[1])
        chain[2]["timestamp"] = "2026-01-12T00:00:00+00:00"  # next ISO week
        chain[2]["previous_hash"] = chain[1]["entry_hash"]
        chain[2]["entry_hash"] = compute_entry_hash(chain[2])
        html_out: str = self._render(chain)

        for row in stats_by_week(chain):
            self.assertIn(self._row([
                row["week"], str(row["adjudicated"]), str(row["passed"]),
                str(row["vetoed"]), str(row["pipeline_errors"]),
                pct(row["vetoed"], row["adjudicated"]),
                pct(row["pipeline_errors"], row["adjudicated"]),
            ]), html_out)
        self.assertEqual(html_out.count("<path d="), 4)  # 2 weeks x 2 charts

        # generate_viewer_html anchors scope at the ledger's grandparent.
        scopes: list[dict] = stats_by_scope(
            chain, str(Path(self._path()).resolve().parent.parent)
        )
        self.assertEqual(
            [(r["scope"], r["adjudicated"], r["vetoed"]) for r in scopes],
            [("governance", 1, 1), ("other", 2, 0)],
        )
        for row in scopes:
            self.assertIn(
                f"<tr><td>{row['scope']}</td><td>{row['adjudicated']}</td>",
                html_out,
            )

    def test_constraint_table_separates_violated_from_cited(self) -> None:
        chain: list[dict] = _build_valid_chain(2, verdicts=["VETO", "VETO"])
        chain[1]["oracle"]["constraint_citations"] = [
            {"constraint_id": "C-001", "disposition": "SATISFIED"},
            {"constraint_id": "C-007", "disposition": "VIOLATED"},
        ]
        chain[1]["entry_hash"] = compute_entry_hash(chain[1])
        html_out: str = self._render(chain)
        rows: list[dict] = citations_by_constraint(chain)
        self.assertEqual(
            [(r["constraint_id"], r["violated"], r["cited"]) for r in rows],
            [("C-001", 1, 2), ("C-007", 1, 1)],
        )
        self.assertIn(self._row(["C-001", "1", "2"]), html_out)
        self.assertIn(self._row(["C-007", "1", "1"]), html_out)
        self.assertIn("Most violated", html_out)

    def test_constraint_ids_are_escaped(self) -> None:
        chain: list[dict] = _build_valid_chain(1, verdicts=["VETO"])
        chain[0]["oracle"]["constraint_citations"] = [
            {"constraint_id": "<b>C-9</b>", "disposition": "VIOLATED"},
        ]
        chain[0]["entry_hash"] = compute_entry_hash(chain[0])
        html_out: str = self._render(chain)
        self.assertIn("<td>&lt;b&gt;C-9&lt;/b&gt;</td>", html_out)
        self.assertNotIn("<b>C-9</b>", html_out)

    def test_token_table_averages_per_recorded_entry(self) -> None:
        chain: list[dict] = _build_valid_chain(2)
        chain[0]["oracle"]["_tokens"] = {"input": 1000, "output": 10}
        chain[0]["entry_hash"] = compute_entry_hash(chain[0])
        chain[1]["previous_hash"] = chain[0]["entry_hash"]
        chain[1]["oracle"]["_tokens"] = {"input": 3000, "output": 20}
        chain[1]["entry_hash"] = compute_entry_hash(chain[1])
        html_out: str = self._render(chain)
        # Stage, entries, input, of which cache reads, output, input per
        # entry, billed input per entry. No cache fields, so billed equals
        # input.
        self.assertIn(
            self._row(["oracle", "2", "4,000", "0", "30", "2,000", "2,000"]),
            html_out,
        )
        self.assertIn(
            self._row(["challenger", "0", "0", "0", "0", "n/a", "n/a"]), html_out
        )
        # Per-entry totals 1,010 and 3,020: the line the README quotes.
        self.assertIn(
            "Per entry: median 2,015, p90 3,020 tokens over 2 entries with usage. "
            "At cached rates (reads 0.1x, writes 1.25x): median 2,015, p90 3,020.",
            html_out,
        )

    def test_token_table_prices_cache_reads_at_the_cached_rate(self) -> None:
        chain: list[dict] = _build_valid_chain(1)
        chain[0]["oracle"]["_tokens"] = {
            "input": 1000,
            "output": 10,
            "cache_read": 900,
            "cache_creation": 0,
        }
        chain[0]["entry_hash"] = compute_entry_hash(chain[0])
        html_out: str = self._render(chain)
        # 100 uncached + 900 * 0.1 = 190 billed input for the one entry.
        self.assertIn(
            self._row(["oracle", "1", "1,000", "900", "10", "1,000", "190"]),
            html_out,
        )
        self.assertIn(
            "At cached rates (reads 0.1x, writes 1.25x): median 200, p90 200.",
            html_out,
        )

    def test_latency_table_restates_seconds_by_stage(self) -> None:
        chain: list[dict] = _build_valid_chain(3)
        chain[0]["oracle"]["_seconds"] = 10.0
        chain[0]["entry_hash"] = compute_entry_hash(chain[0])
        chain[1]["previous_hash"] = chain[0]["entry_hash"]
        chain[1]["oracle"]["_seconds"] = 30.0
        chain[1]["challenger"] = {"status": "CLEAR", "_seconds": 5.0}
        chain[1]["entry_hash"] = compute_entry_hash(chain[1])
        chain[2]["previous_hash"] = chain[1]["entry_hash"]
        chain[2]["entry_hash"] = compute_entry_hash(chain[2])
        html_out: str = self._render(chain)
        self.assertIn(self._row(["oracle", "2", "20.0", "30.0"]), html_out)
        self.assertIn(self._row(["challenger", "1", "5.0", "5.0"]), html_out)
        self.assertIn(self._row(["defender", "0", "n/a", "n/a"]), html_out)
        # Entry totals 10 and 35: the untimed third entry is not counted.
        self.assertIn(self._row(["total", "2", "22.5", "35.0"]), html_out)
        self.assertNotIn("No timings recorded yet.", html_out)

    def test_empty_ledger_renders_an_empty_dashboard(self) -> None:
        html_out: str = generate_viewer_html(self._path())
        self.assertNotIn("generation failed", html_out)
        self.assertEqual(html_out.count('class="card" id="dash-'), 5)
        self.assertEqual(html_out.count("<path d="), 0)
        self.assertIn("No governed changes yet.", html_out)
        self.assertIn("No veto has cited a constraint.", html_out)
        self.assertIn("No timings recorded yet.", html_out)


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

    def test_banner_reports_pipeline_errors_like_cmd_stats(self) -> None:
        """A fail-closed VETO is counted as a pipeline error on both surfaces.

        Bench fails closed: a stage that times out or returns an unparseable
        response records verdict VETO with pipeline_error true. cmd_stats has
        always printed that count separately; the banner did not, so a spike
        of timeouts read as a strict judge (audit finding 2).
        """
        chain: list[dict] = _build_valid_chain(
            3, verdicts=["PASS", "VETO", "PASS"]
        )
        failed: dict = dict(chain[1])
        failed["pipeline_error"] = True
        failed["oracle"] = {"status": "PIPELINE_ERROR", "error": "TIMEOUT"}
        failed["entry_hash"] = compute_entry_hash(failed)
        chain[1] = failed
        chain[2]["previous_hash"] = failed["entry_hash"]
        chain[2]["entry_hash"] = compute_entry_hash(chain[2])
        self._write_chain(chain)
        html_out: str = generate_viewer_html(self._path())

        stats: dict = compute_ledger_stats(chain)
        self.assertEqual(stats["pipeline_errors"], 1)
        self.assertIn(
            '<div class="label">Pipeline errors</div>'
            '<div class="value err">1</div>',
            html_out,
        )

        buf: io.StringIO = io.StringIO()
        with patch("cli.commands.load_ledger", return_value=chain), patch(
            "cli.commands.verify_chain", return_value={"valid": True}
        ), contextlib.redirect_stdout(buf):
            cmd_stats()
        self.assertIn("Pipeline errors        : 1", buf.getvalue())

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
