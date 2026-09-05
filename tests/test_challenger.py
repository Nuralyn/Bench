"""Tests for pipeline.challenger — response validation, content building, run_challenger.

All model calls are mocked. Covers: _validate_challenger_response schema
checks, _build_user_content assembly, and run_challenger end-to-end flow
including error wrapping.

Run: python -m unittest tests.test_challenger -v
"""

import sys
import unittest
from unittest.mock import MagicMock, patch

from pathlib import Path

_REPO_ROOT: Path = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from pipeline.constitution import (  # noqa: E402
    build_cached_prefix,
    build_context_section,
)
from pipeline.challenger import (  # noqa: E402
    _build_user_content,
    _normalize_challenger_response,
    _validate_challenger_response,
    run_challenger,
)


def _valid_finding() -> dict:
    return {
        "constraint_id": "C-001",
        "severity": "VIOLATION",
        "location": "test.py:10",
        "evidence": "empty except block",
        "reasoning": "violates C-001",
    }


def _valid_diff() -> dict:
    return {"file_path": "test.py", "change_type": "edit"}


def _valid_constitution() -> dict:
    return {
        "constraints": [
            {"id": "C-001", "name": "No Silent Errors", "rule": "...", "severity": "veto"}
        ]
    }


class ValidateChallengerResponseTests(unittest.TestCase):
    def test_clear_status_is_valid(self) -> None:
        self.assertTrue(
            _validate_challenger_response({"status": "CLEAR", "findings": []})
        )

    def test_clear_without_findings_key_is_valid(self) -> None:
        self.assertTrue(_validate_challenger_response({"status": "CLEAR"}))

    def test_findings_with_all_fields_is_valid(self) -> None:
        resp: dict = {"status": "FINDINGS", "findings": [_valid_finding()]}
        self.assertTrue(_validate_challenger_response(resp))

    def test_invalid_status_rejected(self) -> None:
        self.assertFalse(
            _validate_challenger_response({"status": "INVALID", "findings": []})
        )

    def test_missing_status_rejected(self) -> None:
        self.assertFalse(_validate_challenger_response({"findings": []}))

    def test_findings_not_list_rejected(self) -> None:
        self.assertFalse(
            _validate_challenger_response({"status": "FINDINGS", "findings": "string"})
        )

    def test_finding_missing_required_field_rejected(self) -> None:
        finding: dict = _valid_finding()
        del finding["constraint_id"]
        self.assertFalse(
            _validate_challenger_response({"status": "FINDINGS", "findings": [finding]})
        )

    def test_finding_empty_string_field_rejected(self) -> None:
        finding: dict = _valid_finding()
        finding["evidence"] = ""
        self.assertFalse(
            _validate_challenger_response({"status": "FINDINGS", "findings": [finding]})
        )

    def test_finding_invalid_severity_rejected(self) -> None:
        finding: dict = _valid_finding()
        finding["severity"] = "CRITICAL"
        self.assertFalse(
            _validate_challenger_response({"status": "FINDINGS", "findings": [finding]})
        )

    def test_finding_non_dict_rejected(self) -> None:
        self.assertFalse(
            _validate_challenger_response(
                {"status": "FINDINGS", "findings": ["not a dict"]}
            )
        )


class NormalizeChallengerResponseTests(unittest.TestCase):
    """One test per repair the ledger has recorded, and the fail-closed line.

    The operational ledger recorded these exact shapes as
    INVALID_CHALLENGER_RESPONSE on 2026-08-06, 2026-08-22, 2026-08-23, and
    twice on 2026-09-05: each a fail-closed VETO on an edit the Challenger
    had examined and reported findings for.
    """

    def _resp(self, finding: dict) -> dict:
        return {"status": "FINDINGS", "findings": [finding]}

    def test_warning_severity_becomes_concern_with_a_note(self) -> None:
        finding: dict = _valid_finding()
        finding["severity"] = "WARNING"
        resp: dict = self._resp(finding)
        notes: list[str] = _normalize_challenger_response(resp)
        self.assertEqual(resp["findings"][0]["severity"], "CONCERN")
        self.assertEqual(len(notes), 1)
        self.assertIn("WARNING", notes[0])
        self.assertTrue(_validate_challenger_response(resp))

    def test_missing_or_misspelled_evidence_still_fails_closed(self) -> None:
        # The ledger recorded findings with no evidence string and one under
        # the key "eviduence". Neither is repaired: evidence is what the
        # Challenger quotes from the diff, and the normalizer never
        # synthesizes a field the schema requires.
        for mutate in (
            lambda f: f.pop("evidence"),
            lambda f: f.__setitem__("eviduence", f.pop("evidence")),
            lambda f: f.__setitem__("evidence", ""),
        ):
            finding: dict = _valid_finding()
            mutate(finding)
            resp: dict = self._resp(finding)
            self.assertEqual(_normalize_challenger_response(resp), [])
            self.assertFalse(_validate_challenger_response(resp), finding)

    def test_clean_response_is_untouched_and_unnoted(self) -> None:
        resp: dict = self._resp(_valid_finding())
        self.assertEqual(_normalize_challenger_response(resp), [])
        self.assertEqual(resp["findings"][0], _valid_finding())

    def test_stray_findings_on_a_clear_response_are_not_a_repair(self) -> None:
        # The validator accepts CLEAR on its status alone and never reads
        # the list, so rewriting it would only inflate the repair count.
        finding: dict = _valid_finding()
        finding["severity"] = "WARNING"
        resp: dict = {"status": "CLEAR", "findings": [finding]}
        self.assertEqual(_normalize_challenger_response(resp), [])
        self.assertEqual(resp["findings"][0]["severity"], "WARNING")
        self.assertTrue(_validate_challenger_response(resp))

    def test_unknown_severity_still_fails_closed(self) -> None:
        finding: dict = _valid_finding()
        finding["severity"] = "CRITICAL"
        resp: dict = self._resp(finding)
        self.assertEqual(_normalize_challenger_response(resp), [])
        self.assertFalse(_validate_challenger_response(resp))

    def test_missing_constraint_location_or_reasoning_still_fails_closed(
        self,
    ) -> None:
        for field in ("constraint_id", "location", "reasoning"):
            finding: dict = _valid_finding()
            del finding[field]
            resp: dict = self._resp(finding)
            _normalize_challenger_response(resp)
            self.assertFalse(_validate_challenger_response(resp), field)

    def test_unknown_status_and_non_list_findings_still_fail_closed(self) -> None:
        for resp in (
            {"status": "MAYBE", "findings": []},
            {"status": "FINDINGS", "findings": "none"},
        ):
            _normalize_challenger_response(resp)
            self.assertFalse(_validate_challenger_response(resp), resp)

    @patch("pipeline.challenger.call_model")
    def test_run_challenger_records_repairs_on_the_result(
        self, mock_call: MagicMock
    ) -> None:
        finding: dict = _valid_finding()
        finding["severity"] = "WARNING"
        mock_call.return_value = {
            "status": "FINDINGS",
            "findings": [finding],
            "_tokens": {"input": 10, "output": 20},
        }
        result: dict = run_challenger(_valid_diff(), _valid_constitution(), "hash")
        self.assertEqual(result["status"], "FINDINGS")
        self.assertEqual(result["findings"][0]["severity"], "CONCERN")
        self.assertEqual(len(result["_normalized"]), 1)

    @patch("pipeline.challenger.call_model")
    def test_run_challenger_leaves_no_note_when_nothing_repaired(
        self, mock_call: MagicMock
    ) -> None:
        mock_call.return_value = {
            "status": "FINDINGS",
            "findings": [_valid_finding()],
            "_tokens": {"input": 10, "output": 20},
        }
        result: dict = run_challenger(_valid_diff(), _valid_constitution(), "hash")
        self.assertNotIn("_normalized", result)

    @patch("pipeline.challenger.call_model")
    def test_raw_response_on_failure_is_the_model_output_not_the_repair(
        self, mock_call: MagicMock
    ) -> None:
        # Repairable drift beside a fatal defect: the ledger must show what
        # the model actually wrote, so the defect can be diagnosed.
        finding: dict = _valid_finding()
        finding["severity"] = "WARNING"
        del finding["constraint_id"]
        mock_call.return_value = {
            "status": "FINDINGS",
            "findings": [finding],
            "_tokens": {"input": 10, "output": 20},
        }
        result: dict = run_challenger(_valid_diff(), _valid_constitution(), "hash")
        self.assertEqual(result["status"], "PIPELINE_ERROR")
        raw: dict = result["raw_response"]
        self.assertEqual(raw["findings"][0]["severity"], "WARNING")
        self.assertNotIn("_normalized", raw)

    @patch("pipeline.challenger.call_model")
    def test_deeply_nested_unknown_field_is_tolerated(
        self, mock_call: MagicMock
    ) -> None:
        # The validator ignores unknown fields, so a valid CLEAR with one
        # nested far beyond what a deep copy could walk must still be a
        # CLEAR: the original is kept by targeted shallow copy, not by
        # recursion, and the extra field rides through by reference.
        nested: dict = {}
        for _ in range(5000):
            nested = {"n": nested}
        mock_call.return_value = {
            "status": "CLEAR",
            "findings": [],
            "extra": nested,
            "_tokens": {"input": 10, "output": 20},
        }
        result: dict = run_challenger(_valid_diff(), _valid_constitution(), "hash")
        self.assertEqual(result["status"], "CLEAR")
        self.assertIs(result["extra"], nested)

    @patch("pipeline.challenger.call_model")
    def test_model_authored_normalized_key_does_not_survive(
        self, mock_call: MagicMock
    ) -> None:
        # The key is reserved for the stage's own record; a model cannot
        # inflate the repair count by writing it.
        mock_call.return_value = {
            "status": "FINDINGS",
            "findings": [_valid_finding()],
            "_normalized": ["fake"],
            "_tokens": {"input": 10, "output": 20},
        }
        result: dict = run_challenger(_valid_diff(), _valid_constitution(), "hash")
        self.assertEqual(result["status"], "FINDINGS")
        self.assertNotIn("_normalized", result)

    @patch("pipeline.challenger.call_model")
    def test_run_challenger_still_fails_closed_on_unknown_severity(
        self, mock_call: MagicMock
    ) -> None:
        finding: dict = _valid_finding()
        finding["severity"] = "CRITICAL"
        mock_call.return_value = {
            "status": "FINDINGS",
            "findings": [finding],
            "_tokens": {"input": 10, "output": 20},
        }
        result: dict = run_challenger(_valid_diff(), _valid_constitution(), "hash")
        self.assertEqual(result["status"], "PIPELINE_ERROR")
        self.assertEqual(result["error"], "INVALID_CHALLENGER_RESPONSE")


class BuildUserContentTests(unittest.TestCase):
    """The per-edit body carries the change; the prefix carries the rest."""

    def test_body_contains_the_change_and_nothing_cached(self) -> None:
        content: str = _build_user_content(_valid_diff())
        self.assertIn("PROPOSED CHANGE:", content)
        self.assertNotIn("CONSTITUTION:", content)
        self.assertNotIn("FILE CONTEXT:", content)

    def test_prefix_carries_the_constitution_only(self) -> None:
        prefix: str = build_cached_prefix(_valid_constitution())
        self.assertIn("CONSTITUTION:", prefix)
        self.assertNotIn("FILE CONTEXT:", prefix)

    def test_context_section_carries_file_context(self) -> None:
        section: str = build_context_section("def foo(): pass")
        self.assertIn("FILE CONTEXT:", section)
        self.assertIn("def foo(): pass", section)

    def test_context_section_empty_when_no_file_context(self) -> None:
        self.assertEqual(build_context_section(""), "")


class RunChallengerTests(unittest.TestCase):
    @patch("pipeline.challenger.call_model")
    def test_valid_clear_response_passed_through(
        self, mock_call: MagicMock
    ) -> None:
        mock_call.return_value = {
            "status": "CLEAR",
            "findings": [],
            "_tokens": {"input": 10, "output": 20},
        }
        result: dict = run_challenger(_valid_diff(), _valid_constitution(), "hash")
        self.assertEqual(result["status"], "CLEAR")

    @patch("pipeline.challenger.call_model")
    def test_valid_findings_response_passed_through(
        self, mock_call: MagicMock
    ) -> None:
        mock_call.return_value = {
            "status": "FINDINGS",
            "findings": [_valid_finding()],
            "_tokens": {"input": 10, "output": 20},
        }
        result: dict = run_challenger(_valid_diff(), _valid_constitution(), "hash")
        self.assertEqual(result["status"], "FINDINGS")
        self.assertEqual(len(result["findings"]), 1)

    @patch("pipeline.challenger.call_model")
    def test_api_error_returns_pipeline_error(
        self, mock_call: MagicMock
    ) -> None:
        mock_call.return_value = {
            "error": "API_ERROR",
            "detail": "timeout",
            "_tokens": {"input": 0, "output": 0},
        }
        result: dict = run_challenger(_valid_diff(), _valid_constitution(), "hash")
        self.assertEqual(result["status"], "PIPELINE_ERROR")

    @patch("pipeline.challenger.call_model")
    def test_invalid_response_returns_pipeline_error_with_raw(
        self, mock_call: MagicMock
    ) -> None:
        mock_call.return_value = {
            "garbage": True,
            "_tokens": {"input": 10, "output": 20},
        }
        result: dict = run_challenger(_valid_diff(), _valid_constitution(), "hash")
        self.assertEqual(result["status"], "PIPELINE_ERROR")
        self.assertIn("raw_response", result)

    def test_input_validation_failure_returns_pipeline_error(self) -> None:
        result: dict = run_challenger({}, _valid_constitution(), "hash")
        self.assertEqual(result["status"], "PIPELINE_ERROR")
        self.assertIn("INVALID_CHALLENGER_INPUT", result["error"])

    @patch("pipeline.challenger.call_model")
    def test_tokens_preserved_on_all_paths(self, mock_call: MagicMock) -> None:
        mock_call.return_value = {
            "status": "CLEAR",
            "_tokens": {"input": 5, "output": 15},
        }
        result: dict = run_challenger(_valid_diff(), _valid_constitution(), "hash")
        self.assertIn("_tokens", result)


if __name__ == "__main__":
    unittest.main()
