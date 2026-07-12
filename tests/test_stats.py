"""Tests for utils.stats: shared ledger statistics helpers.

Covers: entry_has_pipeline_error across stages, pct zero-guard and
formatting, and compute_ledger_stats aggregation (PASS/VETO counts,
string and dict citation shapes, unexpected citation types, most_cited
selection, pipeline-error tallying).

Run: python -m unittest discover -s tests -p test_stats.py -v
"""

import sys
import unittest
from pathlib import Path

_REPO_ROOT: Path = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from utils.stats import (  # noqa: E402
    compute_ledger_stats,
    entry_has_pipeline_error,
    pct,
)


def _pass_entry() -> dict:
    return {"oracle": {"verdict": "PASS"}}


def _veto_entry(citations: list) -> dict:
    return {"oracle": {"verdict": "VETO", "constraint_citations": citations}}


class EntryHasPipelineErrorTests(unittest.TestCase):
    def test_clean_entry_is_false(self) -> None:
        self.assertFalse(entry_has_pipeline_error(_pass_entry()))

    def test_error_in_each_stage_is_detected(self) -> None:
        for stage in ("challenger", "defender", "oracle"):
            entry: dict = {stage: {"status": "PIPELINE_ERROR"}}
            self.assertTrue(entry_has_pipeline_error(entry), stage)

    def test_non_dict_stage_is_ignored(self) -> None:
        self.assertFalse(
            entry_has_pipeline_error({"challenger": "PIPELINE_ERROR"})
        )

    def test_empty_entry_is_false(self) -> None:
        self.assertFalse(entry_has_pipeline_error({}))


class PctTests(unittest.TestCase):
    def test_zero_total_returns_zero_percent(self) -> None:
        self.assertEqual(pct(5, 0), "0.0%")

    def test_negative_total_returns_zero_percent(self) -> None:
        self.assertEqual(pct(1, -3), "0.0%")

    def test_formats_one_decimal(self) -> None:
        self.assertEqual(pct(1, 3), "33.3%")
        self.assertEqual(pct(2, 2), "100.0%")
        self.assertEqual(pct(0, 7), "0.0%")


class ComputeLedgerStatsTests(unittest.TestCase):
    def test_empty_ledger(self) -> None:
        stats: dict = compute_ledger_stats([])
        self.assertEqual(stats["total"], 0)
        self.assertEqual(stats["passed"], 0)
        self.assertEqual(stats["vetoed"], 0)
        self.assertEqual(stats["pipeline_errors"], 0)
        self.assertIsNone(stats["most_cited"])

    def test_pass_and_veto_counts(self) -> None:
        entries: list[dict] = [
            _pass_entry(),
            _pass_entry(),
            _veto_entry(["C-001"]),
        ]
        stats: dict = compute_ledger_stats(entries)
        self.assertEqual(stats["total"], 3)
        self.assertEqual(stats["passed"], 2)
        self.assertEqual(stats["vetoed"], 1)

    def test_string_citations_counted(self) -> None:
        entries: list[dict] = [
            _veto_entry(["C-001", "C-002"]),
            _veto_entry(["C-001"]),
        ]
        stats: dict = compute_ledger_stats(entries)
        self.assertEqual(stats["most_cited"], ("C-001", 2))

    def test_dict_citations_counted(self) -> None:
        entries: list[dict] = [
            _veto_entry([
                {"constraint_id": "C-007", "disposition": "VIOLATED"},
                {"constraint_id": "C-007", "disposition": "VIOLATED"},
            ]),
        ]
        stats: dict = compute_ledger_stats(entries)
        self.assertEqual(stats["most_cited"], ("C-007", 2))

    def test_unexpected_citation_types_skipped(self) -> None:
        entries: list[dict] = [_veto_entry([42, None, ["nested"]])]
        stats: dict = compute_ledger_stats(entries)
        self.assertEqual(stats["vetoed"], 1)
        self.assertIsNone(stats["most_cited"])

    def test_dict_citation_without_string_id_skipped(self) -> None:
        entries: list[dict] = [_veto_entry([{"constraint_id": 3}])]
        stats: dict = compute_ledger_stats(entries)
        self.assertIsNone(stats["most_cited"])

    def test_pipeline_errors_counted_independently(self) -> None:
        entries: list[dict] = [
            {
                "oracle": {"verdict": "PASS"},
                "challenger": {"status": "PIPELINE_ERROR"},
            },
            _pass_entry(),
        ]
        stats: dict = compute_ledger_stats(entries)
        self.assertEqual(stats["pipeline_errors"], 1)
        self.assertEqual(stats["passed"], 2)

    def test_non_dict_oracle_ignored(self) -> None:
        stats: dict = compute_ledger_stats([{"oracle": "corrupt"}])
        self.assertEqual(stats["total"], 1)
        self.assertEqual(stats["passed"], 0)
        self.assertEqual(stats["vetoed"], 0)


if __name__ == "__main__":
    unittest.main()
