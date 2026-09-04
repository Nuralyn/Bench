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
    citations_by_constraint,
    compute_ledger_stats,
    entry_has_pipeline_error,
    entry_verdict,
    latency_by_week,
    pct,
    scope_of_file,
    seconds_by_stage,
    stats_by_scope,
    stats_by_week,
    tokens_by_stage,
    tokens_per_entry,
    week_of,
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
        # Two vetoes, one citation each. (This test once put both citations
        # in a single veto and expected 2, which pinned the double count
        # that CitationTableTests.test_a_constraint_counts_once_per_veto
        # now forbids.)
        entries: list[dict] = [
            _veto_entry([{"constraint_id": "C-007", "disposition": "VIOLATED"}]),
            _veto_entry([{"constraint_id": "C-007", "disposition": "VIOLATED"}]),
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


class AnchorVerdictStatsTests(unittest.TestCase):
    """Chain-retirement anchors are bookkeeping, not adjudicated changes.

    Counting an anchor as a governed verdict would skew the pass rate the
    project publishes, so it is tallied separately (C-008, constitution v2).
    """

    def _entries(self) -> list[dict]:
        return [
            {"verdict": "ANCHOR", "change": {"file": "ledger/x.json"}},
            {"verdict": "PASS", "change": {"file": "a.py"}},
            {"verdict": "PASS", "change": {"file": "b.py"}},
            {"verdict": "VETO", "change": {"file": "c.py"}},
        ]

    def test_anchor_counted_separately(self) -> None:
        stats: dict = compute_ledger_stats(self._entries())
        self.assertEqual(stats["anchors"], 1)
        self.assertEqual(stats["passed"], 2)
        self.assertEqual(stats["vetoed"], 1)

    def test_adjudicated_excludes_anchors(self) -> None:
        stats: dict = compute_ledger_stats(self._entries())
        self.assertEqual(stats["total"], 4)
        self.assertEqual(stats["adjudicated"], 3)
        self.assertEqual(stats["anchors"] + stats["adjudicated"], stats["total"])

    def test_pass_rate_uses_adjudicated_denominator(self) -> None:
        stats: dict = compute_ledger_stats(self._entries())
        # 2 of 3 adjudicated, not 2 of 4 entries.
        self.assertEqual(pct(stats["passed"], stats["adjudicated"]), "66.7%")

    def test_ledger_without_anchors_is_unchanged(self) -> None:
        stats: dict = compute_ledger_stats(
            [{"verdict": "PASS", "change": {"file": "a.py"}}]
        )
        self.assertEqual(stats["anchors"], 0)
        self.assertEqual(stats["adjudicated"], stats["total"])

    def test_empty_ledger_has_zero_anchors(self) -> None:
        stats: dict = compute_ledger_stats([])
        self.assertEqual(stats["anchors"], 0)
        self.assertEqual(stats["adjudicated"], 0)


class EntryVerdictTests(unittest.TestCase):
    def test_top_level_verdict_wins(self) -> None:
        entry: dict = {"verdict": "VETO", "oracle": {"verdict": "PASS"}}
        self.assertEqual(entry_verdict(entry), "VETO")

    def test_falls_back_to_oracle_verdict(self) -> None:
        self.assertEqual(entry_verdict({"oracle": {"verdict": "PASS"}}), "PASS")

    def test_none_when_no_verdict(self) -> None:
        self.assertIsNone(entry_verdict({}))
        self.assertIsNone(entry_verdict({"oracle": "corrupt"}))
        self.assertIsNone(entry_verdict({"verdict": ""}))


class TopLevelPipelineErrorTests(unittest.TestCase):
    def test_top_level_flag_detected(self) -> None:
        # A constitution-load fail-closed VETO runs no stage but sets the flag.
        self.assertTrue(entry_has_pipeline_error({"pipeline_error": True}))

    def test_false_flag_and_no_stage_is_false(self) -> None:
        self.assertFalse(entry_has_pipeline_error({"pipeline_error": False}))


class FailClosedStatsTests(unittest.TestCase):
    def test_fail_closed_veto_counted_as_veto_and_pipeline_error(self) -> None:
        # Fail-closed VETOs carry a top-level verdict and pipeline_error and
        # no oracle stage; they must appear in both tallies, not vanish.
        entries: list[dict] = [
            {"verdict": "VETO", "pipeline_error": True,
             "challenger": {"status": "PIPELINE_ERROR"}},
            {"verdict": "VETO", "pipeline_error": True},  # constitution failure
        ]
        stats: dict = compute_ledger_stats(entries)
        self.assertEqual(stats["vetoed"], 2)
        self.assertEqual(stats["pipeline_errors"], 2)
        self.assertEqual(stats["passed"], 0)


def _change(
    file: str,
    verdict: str,
    timestamp: str = "2026-07-24T18:44:07+00:00",
    pipeline_error: bool = False,
) -> dict:
    return {
        "timestamp": timestamp,
        "verdict": verdict,
        "pipeline_error": pipeline_error,
        "change": {"file": file, "tool": "Edit"},
        "oracle": {} if pipeline_error else {"verdict": verdict},
    }


def _counts(row: dict) -> tuple[int, int, int, int]:
    return (
        row["adjudicated"], row["passed"], row["vetoed"], row["pipeline_errors"]
    )


_ROOT: str = "C:/Users/x/Bench"


class ScopeTests(unittest.TestCase):
    def test_top_level_governance_paths_in_any_form(self) -> None:
        for path in (
            "pipeline/oracle.py",
            "ledger/chain.py",
            "hooks/pre-tool-use.py",
            "ledger/entries/deadbeef.json",  # nested below a top-level dir
            "C:/Users/x/Bench/ledger/verify.py",
            "C:\\Users\\x\\Bench\\hooks\\pre-tool-use.py",
            "bench.json",  # the constitution, named by C-007
            "C:/Users/x/Bench/bench.json",
        ):
            self.assertEqual(scope_of_file(path, _ROOT), "governance", path)

    def test_posix_root_is_handled_too(self) -> None:
        self.assertEqual(
            scope_of_file("/home/x/proj/pipeline/a.py", "/home/x/proj"),
            "governance",
        )
        self.assertEqual(
            scope_of_file("/home/x/other/pipeline/a.py", "/home/x/proj"),
            "other",
        )

    def test_everything_else_is_other(self) -> None:
        for path in (
            "utils/viewer.py",
            "tests/test_ledger_dag.py",
            "src/pipeline/etl.py",  # a governed project's own pipeline dir
            "docs/bench.json",  # not the top-level constitution
            "hooks.md",  # a file, not a directory
            "C:/Users/x/Elsewhere/pipeline/a.py",  # outside the project
            "C:/Users/x/Bench-fork/pipeline/a.py",  # prefix, not the root
            "",
        ):
            self.assertEqual(scope_of_file(path, _ROOT), "other", path)

    def test_stats_by_scope_tallies_each_group(self) -> None:
        entries: list[dict] = [
            _change("pipeline/oracle.py", "PASS"),
            _change("pipeline/runner.py", "VETO"),
            _change("README.md", "PASS"),
            _change("utils/x.py", "VETO", pipeline_error=True),
        ]
        rows: list[dict] = stats_by_scope(entries, _ROOT)
        self.assertEqual([r["scope"] for r in rows], ["governance", "other"])
        self.assertEqual(_counts(rows[0]), (2, 1, 1, 0))
        self.assertEqual(_counts(rows[1]), (2, 1, 1, 1))

    def test_both_scopes_present_for_an_empty_ledger(self) -> None:
        rows: list[dict] = stats_by_scope([], _ROOT)
        self.assertEqual([r["scope"] for r in rows], ["governance", "other"])
        self.assertEqual([_counts(r) for r in rows], [(0, 0, 0, 0)] * 2)


class WeekTests(unittest.TestCase):
    def test_week_of_is_the_iso_week(self) -> None:
        self.assertEqual(week_of("2026-07-24T18:44:07+00:00"), "2026-W30")
        self.assertEqual(week_of("2026-01-01T00:00:00+00:00"), "2026-W01")

    def test_week_of_unparseable_is_unknown(self) -> None:
        for bad in ("", "not a date", "2026-13-40T00:00:00", None):
            self.assertEqual(week_of(bad), "unknown", repr(bad))

    def test_stats_by_week_orders_buckets_and_tallies(self) -> None:
        anchor: dict = _change("ledger/bench-ledger.json", "ANCHOR",
                               timestamp="2026-07-25T00:00:00+00:00")
        entries: list[dict] = [
            _change("a.py", "PASS", timestamp="2026-08-10T00:00:00+00:00"),
            _change("b.py", "PASS"),
            _change("c.py", "VETO", pipeline_error=True),
            anchor,
        ]
        rows: list[dict] = stats_by_week(entries)
        self.assertEqual([r["week"] for r in rows], ["2026-W30", "2026-W33"])
        self.assertEqual(_counts(rows[0]), (2, 1, 1, 1))
        self.assertEqual(rows[0]["anchors"], 1)
        self.assertEqual(_counts(rows[1]), (1, 1, 0, 0))


class SanitationRecordTests(unittest.TestCase):
    def test_sanitation_records_are_not_adjudicated(self) -> None:
        # ledger/sanitize.py records a published-copy sanitation with verdict
        # SANITATION. It is bookkeeping like an anchor: neither passed nor
        # vetoed, so it must not inflate the denominator of any rate.
        entries: list[dict] = [
            _change("a.py", "PASS"),
            _change("b.py", "VETO"),
            _change("published copies", "SANITATION"),
        ]
        stats: dict = compute_ledger_stats(entries)
        self.assertEqual(stats["total"], 3)
        self.assertEqual(stats["adjudicated"], 2)
        self.assertEqual(pct(stats["vetoed"], stats["adjudicated"]), "50.0%")
        week: dict = stats_by_week(entries)[0]
        self.assertEqual(_counts(week), (2, 1, 1, 0))


class CitationTableTests(unittest.TestCase):
    def test_a_constraint_counts_once_per_veto(self) -> None:
        # One Oracle response may cite a constraint more than once; the
        # columns count vetoes, so the second mention must not add another.
        entries: list[dict] = [
            _veto_entry([
                {"constraint_id": "C-007", "disposition": "VIOLATED"},
                {"constraint_id": "C-007", "disposition": "VIOLATED"},
                {"constraint_id": "C-007", "disposition": "SATISFIED"},
            ]),
            _veto_entry(["C-007", "C-007"]),
        ]
        self.assertEqual(
            citations_by_constraint(entries),
            [{"constraint_id": "C-007", "cited": 2, "violated": 2}],
        )
        self.assertEqual(
            compute_ledger_stats(entries)["most_cited"], ("C-007", 2)
        )

    def test_cited_and_violated_are_counted_separately(self) -> None:
        entries: list[dict] = [
            _veto_entry([
                {"constraint_id": "C-007", "disposition": "VIOLATED"},
                {"constraint_id": "C-001", "disposition": "SATISFIED"},
                {"constraint_id": "C-003", "disposition": "NOT_APPLICABLE"},
            ]),
            _veto_entry(["C-007"]),  # legacy string: the veto named it
            _veto_entry([{"constraint_id": "C-007"}]),  # no disposition
            {"oracle": {"verdict": "PASS", "constraint_citations": [
                {"constraint_id": "C-001", "disposition": "SATISFIED"},
            ]}},
        ]
        rows: list[dict] = citations_by_constraint(entries)
        self.assertEqual(
            rows,
            [
                {"constraint_id": "C-001", "cited": 1, "violated": 0},
                {"constraint_id": "C-003", "cited": 1, "violated": 0},
                {"constraint_id": "C-007", "cited": 3, "violated": 3},
            ],
        )

    def test_most_cited_counts_only_violations(self) -> None:
        # Every veto cites C-001 as SATISFIED; only one found C-002 VIOLATED.
        # The headline is the constraint that drove a veto, not the one the
        # Oracle mentioned most while clearing it.
        entries: list[dict] = [
            _veto_entry([
                {"constraint_id": "C-001", "disposition": "SATISFIED"},
                {"constraint_id": "C-002", "disposition": "VIOLATED"},
            ]),
            _veto_entry([
                {"constraint_id": "C-001", "disposition": "SATISFIED"},
            ]),
        ]
        stats: dict = compute_ledger_stats(entries)
        self.assertEqual(stats["most_cited"], ("C-002", 1))

    def test_no_violations_means_no_headline(self) -> None:
        entries: list[dict] = [
            _veto_entry([{"constraint_id": "C-001", "disposition": "SATISFIED"}]),
        ]
        self.assertIsNone(compute_ledger_stats(entries)["most_cited"])


class TokensByStageTests(unittest.TestCase):
    def test_sums_per_stage_and_overall(self) -> None:
        entries: list[dict] = [
            {
                "challenger": {"_tokens": {"input": 10, "output": 2}},
                "defender": {"_tokens": {"input": 5, "output": 1}},
                "oracle": {"_tokens": {"input": 7, "output": 3}},
            },
            {
                "challenger": {"tokens_used": {"input": 1, "output": 1}},
                "oracle": {"_tokens": {}},
            },
            {"verdict": "VETO", "pipeline_error": True},
        ]
        totals: dict = tokens_by_stage(entries)
        self.assertEqual(
            totals["challenger"], {"input": 11, "output": 3, "entries": 2}
        )
        self.assertEqual(
            totals["defender"], {"input": 5, "output": 1, "entries": 1}
        )
        self.assertEqual(
            totals["oracle"], {"input": 7, "output": 3, "entries": 1}
        )
        self.assertEqual(
            totals["total"], {"input": 23, "output": 7, "entries": 2}
        )

    def test_non_numeric_figures_are_ignored(self) -> None:
        entries: list[dict] = [
            {"oracle": {"_tokens": {"input": "many", "output": None}}},
            {"oracle": {"_tokens": {"input": True, "output": 4.5}}},
            {"oracle": {"_tokens": "n/a"}},
        ]
        totals: dict = tokens_by_stage(entries)
        self.assertEqual(
            totals["oracle"], {"input": 0, "output": 0, "entries": 0}
        )
        self.assertEqual(
            totals["total"], {"input": 0, "output": 0, "entries": 0}
        )


class TokensPerEntryTests(unittest.TestCase):
    """The per-edit token distribution the README quotes."""

    def test_sums_input_and_output_across_stages_per_entry(self) -> None:
        entries: list[dict] = [
            {
                "challenger": {"_tokens": {"input": 100, "output": 10}},
                "defender": {"_tokens": {"input": 200, "output": 20}},
                "oracle": {"_tokens": {"input": 300, "output": 30}},
            },
            {"oracle": {"tokens_used": {"input": 1000, "output": 0}}},
            {"challenger": {"_tokens": {"input": 5000, "output": 500}}},
        ]
        # Totals 660, 1000, 5500.
        self.assertEqual(
            tokens_per_entry(entries), {"entries": 3, "median": 1000.0, "p90": 5500.0}
        )

    def test_entries_without_usable_usage_are_skipped_not_zero(self) -> None:
        entries: list[dict] = [
            {"verdict": "PASS"},
            {"oracle": {"_tokens": {"input": True, "output": "many"}}},
            {"oracle": {"_tokens": "n/a"}},
            {"oracle": {"_tokens": {"input": 40, "output": 2}}},
        ]
        self.assertEqual(
            tokens_per_entry(entries), {"entries": 1, "median": 42.0, "p90": 42.0}
        )

    def test_agrees_with_tokens_by_stage_on_what_counts(self) -> None:
        entries: list[dict] = [
            {"oracle": {"_tokens": {"input": 7, "output": 3}}},
            {"challenger": {"_tokens": {"input": 1.5, "output": 1}}},
        ]
        self.assertEqual(
            tokens_per_entry(entries)["entries"], tokens_by_stage(entries)["total"]["entries"]
        )

    def test_empty_ledger(self) -> None:
        self.assertEqual(tokens_per_entry([]), {"entries": 0, "median": 0.0, "p90": 0.0})


class SecondsByStageTests(unittest.TestCase):
    """Latency figures come only from entries that recorded a timing."""

    def test_medians_and_p90_over_recorded_stages_only(self) -> None:
        entries: list[dict] = [
            {
                "challenger": {"_seconds": 10.0},
                "defender": {"_seconds": 0.0},
                "oracle": {"_seconds": 20.0},
            },
            {
                "challenger": {"_seconds": 30.0},
                "oracle": {"_seconds": 40.0},
            },
            {"challenger": {"_seconds": 50}},
            {"verdict": "VETO", "pipeline_error": True},
        ]
        summary: dict = seconds_by_stage(entries)
        self.assertEqual(
            summary["challenger"], {"entries": 3, "median": 30.0, "p90": 50.0}
        )
        self.assertEqual(
            summary["defender"], {"entries": 1, "median": 0.0, "p90": 0.0}
        )
        self.assertEqual(
            summary["oracle"], {"entries": 2, "median": 30.0, "p90": 40.0}
        )
        # Totals: 30, 70, 50 -> sorted 30, 50, 70.
        self.assertEqual(
            summary["total"], {"entries": 3, "median": 50.0, "p90": 70.0}
        )

    def test_untimed_and_malformed_figures_are_left_out(self) -> None:
        entries: list[dict] = [
            {"oracle": {"_tokens": {"input": 1, "output": 1}}},
            {"oracle": {"_seconds": "fast"}},
            {"oracle": {"_seconds": True}},
            {"oracle": {"_seconds": -1.0}},
            {"oracle": {"_seconds": None}},
            {"oracle": "n/a"},
        ]
        summary: dict = seconds_by_stage(entries)
        self.assertEqual(summary["oracle"], {"entries": 0, "median": 0.0, "p90": 0.0})
        self.assertEqual(summary["total"], {"entries": 0, "median": 0.0, "p90": 0.0})

    def test_p90_is_nearest_rank(self) -> None:
        entries: list[dict] = [{"oracle": {"_seconds": float(i)}} for i in range(1, 11)]
        summary: dict = seconds_by_stage(entries)
        self.assertEqual(summary["oracle"]["p90"], 9.0)
        self.assertEqual(summary["oracle"]["median"], 5.5)

    def test_p90_rank_at_sample_size_boundaries(self) -> None:
        # Nearest rank is ceil(n * 90 / 100), in integer arithmetic so no
        # float product can move it. n=1 -> 1st, n=3 -> 3rd, n=9 -> 9th,
        # n=11 -> 10th, n=20 -> 18th.
        for n, expected_rank in ((1, 1), (3, 3), (9, 9), (11, 10), (20, 18)):
            with self.subTest(n=n):
                entries: list[dict] = [
                    {"oracle": {"_seconds": float(i)}} for i in range(1, n + 1)
                ]
                self.assertEqual(
                    seconds_by_stage(entries)["oracle"]["p90"], float(expected_rank)
                )

    def test_empty_ledger(self) -> None:
        summary: dict = seconds_by_stage([])
        for stage in ("challenger", "defender", "oracle", "total"):
            self.assertEqual(summary[stage], {"entries": 0, "median": 0.0, "p90": 0.0})


class LatencyByWeekTests(unittest.TestCase):
    def test_weeks_without_a_timing_are_omitted(self) -> None:
        entries: list[dict] = [
            {"timestamp": "2026-01-05T00:00:00+00:00", "oracle": {"_seconds": 4.0}},
            {"timestamp": "2026-01-06T00:00:00+00:00", "challenger": {"_seconds": 1.0}, "oracle": {"_seconds": 1.0}},
            {"timestamp": "2026-01-12T00:00:00+00:00", "verdict": "PASS"},
            {"timestamp": "2026-01-19T00:00:00+00:00", "oracle": {"_seconds": 8.0}},
            {"timestamp": "not a date", "oracle": {"_seconds": 3.0}},
        ]
        rows: list[dict] = latency_by_week(entries)
        self.assertEqual(
            rows,
            [
                {"week": "2026-W02", "entries": 2, "median": 3.0, "p90": 4.0},
                {"week": "2026-W04", "entries": 1, "median": 8.0, "p90": 8.0},
                {"week": week_of("not a date"), "entries": 1, "median": 3.0, "p90": 3.0},
            ],
        )

    def test_empty_ledger(self) -> None:
        self.assertEqual(latency_by_week([]), [])


if __name__ == "__main__":
    unittest.main()
