"""Tests for pipeline.defender — response validation, content building, run_defender.

All model calls are mocked. Covers: _validate_defender_response schema checks
including rebuttal field validation (bool finding_index, position enum),
_build_user_content assembly, and run_defender end-to-end flow.

Run: python -m unittest tests.test_defender -v
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
from pipeline.defender import (  # noqa: E402
    _build_user_content,
    _normalize_defender_response,
    _validate_defender_response,
    run_defender,
)


def _valid_rebuttal() -> dict:
    return {
        "finding_index": 0,
        "position": "REBUT",
        "argument": "The error is logged on the next line",
        "evidence": "see line 12",
    }


def _valid_diff() -> dict:
    return {"file_path": "test.py", "change_type": "edit"}


def _valid_constitution() -> dict:
    return {
        "constraints": [
            {"id": "C-001", "name": "No Silent Errors", "rule": "...", "severity": "veto"}
        ]
    }


def _valid_challenger() -> dict:
    return {"status": "FINDINGS", "findings": []}


class ValidateDefenderResponseTests(unittest.TestCase):
    def test_confirm_clear_with_summary_is_valid(self) -> None:
        self.assertTrue(
            _validate_defender_response(
                {"status": "CONFIRM_CLEAR", "summary": "All clear."}
            )
        )

    def test_concede_all_with_summary_is_valid(self) -> None:
        self.assertTrue(
            _validate_defender_response(
                {"status": "CONCEDE_ALL", "summary": "Conceded."}
            )
        )

    def test_rebuttal_with_valid_rebuttals_is_valid(self) -> None:
        resp: dict = {
            "status": "REBUTTAL",
            "summary": "Rebutted one finding.",
            "rebuttals": [_valid_rebuttal()],
        }
        self.assertTrue(_validate_defender_response(resp))

    def test_invalid_status_rejected(self) -> None:
        self.assertFalse(
            _validate_defender_response({"status": "UNKNOWN", "summary": "x"})
        )

    def test_missing_summary_rejected(self) -> None:
        self.assertFalse(
            _validate_defender_response({"status": "CONFIRM_CLEAR"})
        )

    def test_empty_summary_rejected(self) -> None:
        self.assertFalse(
            _validate_defender_response({"status": "CONFIRM_CLEAR", "summary": ""})
        )

    def test_rebuttal_without_rebuttals_list_rejected(self) -> None:
        self.assertFalse(
            _validate_defender_response({"status": "REBUTTAL", "summary": "x"})
        )

    def test_rebuttal_non_dict_entry_rejected(self) -> None:
        self.assertFalse(
            _validate_defender_response(
                {"status": "REBUTTAL", "summary": "x", "rebuttals": ["not a dict"]}
            )
        )

    def test_rebuttal_non_int_finding_index_rejected(self) -> None:
        r: dict = _valid_rebuttal()
        r["finding_index"] = "zero"
        self.assertFalse(
            _validate_defender_response(
                {"status": "REBUTTAL", "summary": "x", "rebuttals": [r]}
            )
        )

    def test_rebuttal_bool_finding_index_rejected(self) -> None:
        r: dict = _valid_rebuttal()
        r["finding_index"] = True
        self.assertFalse(
            _validate_defender_response(
                {"status": "REBUTTAL", "summary": "x", "rebuttals": [r]}
            )
        )

    def test_rebuttal_missing_argument_rejected(self) -> None:
        r: dict = _valid_rebuttal()
        del r["argument"]
        self.assertFalse(
            _validate_defender_response(
                {"status": "REBUTTAL", "summary": "x", "rebuttals": [r]}
            )
        )

    def test_rebuttal_invalid_position_rejected(self) -> None:
        r: dict = _valid_rebuttal()
        r["position"] = "ARGUE"
        self.assertFalse(
            _validate_defender_response(
                {"status": "REBUTTAL", "summary": "x", "rebuttals": [r]}
            )
        )


class NormalizeDefenderResponseTests(unittest.TestCase):
    """One test per repair the ledger has recorded, and the fail-closed line.

    The operational ledger recorded these exact shapes as
    INVALID_DEFENDER_RESPONSE on 2026-07-31 (twice) and 2026-08-04 (twice):
    finding_index as a digit string, and the positions CONFIRM and
    CONFIRM_CLEAR, each a fail-closed VETO on an edit the Defender had
    argued for. The system prompt already names CONFIRM and AGREE as the
    mistakes to avoid and says they mean CONCEDE.
    """

    def _resp(self, rebuttal: dict) -> dict:
        return {"status": "REBUTTAL", "summary": "Sound.", "rebuttals": [rebuttal]}

    def test_digit_string_finding_index_becomes_int_with_a_note(self) -> None:
        rebuttal: dict = _valid_rebuttal()
        rebuttal["finding_index"] = "0"
        resp: dict = self._resp(rebuttal)
        notes: list[str] = _normalize_defender_response(resp)
        self.assertEqual(resp["rebuttals"][0]["finding_index"], 0)
        self.assertEqual(len(notes), 1)
        self.assertTrue(_validate_defender_response(resp))

    def test_concede_aliases_become_concede_with_a_note(self) -> None:
        for alias in ("CONFIRM", "CONFIRM_CLEAR", "AGREE", "confirm_clear"):
            rebuttal: dict = _valid_rebuttal()
            rebuttal["position"] = alias
            resp: dict = self._resp(rebuttal)
            notes: list[str] = _normalize_defender_response(resp)
            self.assertEqual(resp["rebuttals"][0]["position"], "CONCEDE", alias)
            self.assertEqual(len(notes), 1, alias)
            self.assertTrue(_validate_defender_response(resp), alias)

    def test_clean_response_is_untouched_and_unnoted(self) -> None:
        resp: dict = self._resp(_valid_rebuttal())
        self.assertEqual(_normalize_defender_response(resp), [])
        self.assertEqual(resp["rebuttals"][0], _valid_rebuttal())

    def test_unknown_position_still_fails_closed(self) -> None:
        # Only agreement synonyms are aliased. A position that could mean
        # disagreement is not guessed at.
        for position in ("REFUTE", "REJECT", "DISPUTE", "PARTIAL"):
            rebuttal: dict = _valid_rebuttal()
            rebuttal["position"] = position
            resp: dict = self._resp(rebuttal)
            self.assertEqual(_normalize_defender_response(resp), [], position)
            self.assertFalse(_validate_defender_response(resp), position)

    def test_non_numeric_or_negative_index_still_fails_closed(self) -> None:
        # "²" and "①" satisfy str.isdigit() but int() rejects them, and a
        # 5,000-digit string exceeds int()'s conversion limit: each must
        # reach the validator's fail-closed path, never raise.
        for index in ("first", "-1", "1.5", None, "²", "①", "9" * 5000):
            rebuttal: dict = _valid_rebuttal()
            rebuttal["finding_index"] = index
            resp: dict = self._resp(rebuttal)
            _normalize_defender_response(resp)
            self.assertFalse(_validate_defender_response(resp), repr(index))

    def test_missing_argument_or_summary_still_fails_closed(self) -> None:
        rebuttal: dict = _valid_rebuttal()
        del rebuttal["argument"]
        resp: dict = self._resp(rebuttal)
        _normalize_defender_response(resp)
        self.assertFalse(_validate_defender_response(resp))
        resp = self._resp(_valid_rebuttal())
        del resp["summary"]
        _normalize_defender_response(resp)
        self.assertFalse(_validate_defender_response(resp))

    @patch("pipeline.defender.call_model")
    def test_run_defender_records_repairs_on_the_result(
        self, mock_call: MagicMock
    ) -> None:
        rebuttal: dict = _valid_rebuttal()
        rebuttal["finding_index"] = "0"
        rebuttal["position"] = "CONFIRM_CLEAR"
        mock_call.return_value = {
            "status": "REBUTTAL",
            "summary": "Sound.",
            "rebuttals": [rebuttal],
            "_tokens": {"input": 10, "output": 20},
        }
        result: dict = run_defender(
            _valid_diff(), _valid_constitution(), "hash", _valid_challenger()
        )
        self.assertEqual(result["status"], "REBUTTAL")
        self.assertEqual(result["rebuttals"][0]["finding_index"], 0)
        self.assertEqual(result["rebuttals"][0]["position"], "CONCEDE")
        self.assertEqual(len(result["_normalized"]), 2)

    @patch("pipeline.defender.call_model")
    def test_run_defender_leaves_no_note_when_nothing_repaired(
        self, mock_call: MagicMock
    ) -> None:
        mock_call.return_value = {
            "status": "REBUTTAL",
            "summary": "Sound.",
            "rebuttals": [_valid_rebuttal()],
            "_tokens": {"input": 10, "output": 20},
        }
        result: dict = run_defender(
            _valid_diff(), _valid_constitution(), "hash", _valid_challenger()
        )
        self.assertNotIn("_normalized", result)

    @patch("pipeline.defender.call_model")
    def test_run_defender_still_fails_closed_on_unknown_position(
        self, mock_call: MagicMock
    ) -> None:
        rebuttal: dict = _valid_rebuttal()
        rebuttal["position"] = "REFUTE"
        mock_call.return_value = {
            "status": "REBUTTAL",
            "summary": "Sound.",
            "rebuttals": [rebuttal],
            "_tokens": {"input": 10, "output": 20},
        }
        result: dict = run_defender(
            _valid_diff(), _valid_constitution(), "hash", _valid_challenger()
        )
        self.assertEqual(result["status"], "PIPELINE_ERROR")
        self.assertEqual(result["error"], "INVALID_DEFENDER_RESPONSE")


class BuildUserContentTests(unittest.TestCase):
    """The per-edit body carries the change and findings; the prefix the rest."""

    def test_body_contains_change_and_findings_and_nothing_cached(self) -> None:
        content: str = _build_user_content(_valid_diff(), _valid_challenger())
        self.assertIn("PROPOSED CHANGE:", content)
        self.assertIn("CHALLENGER FINDINGS:", content)
        self.assertNotIn("CONSTITUTION:", content)
        self.assertNotIn("FILE CONTEXT:", content)

    def test_prefix_carries_the_constitution_only(self) -> None:
        prefix: str = build_cached_prefix(_valid_constitution())
        self.assertIn("CONSTITUTION:", prefix)
        self.assertNotIn("FILE CONTEXT:", prefix)

    def test_context_section_carries_file_context(self) -> None:
        section: str = build_context_section("source code")
        self.assertIn("FILE CONTEXT:", section)
        self.assertIn("source code", section)
        self.assertEqual(build_context_section(""), "")


class RunDefenderTests(unittest.TestCase):
    @patch("pipeline.defender.call_model")
    def test_valid_response_passed_through(self, mock_call: MagicMock) -> None:
        mock_call.return_value = {
            "status": "REBUTTAL",
            "summary": "Rebutted.",
            "rebuttals": [_valid_rebuttal()],
            "_tokens": {"input": 10, "output": 20},
        }
        result: dict = run_defender(
            _valid_diff(), _valid_constitution(), "hash", _valid_challenger()
        )
        self.assertEqual(result["status"], "REBUTTAL")

    @patch("pipeline.defender.call_model")
    def test_api_error_returns_pipeline_error(
        self, mock_call: MagicMock
    ) -> None:
        mock_call.return_value = {
            "error": "API_ERROR",
            "_tokens": {"input": 0, "output": 0},
        }
        result: dict = run_defender(
            _valid_diff(), _valid_constitution(), "hash", _valid_challenger()
        )
        self.assertEqual(result["status"], "PIPELINE_ERROR")

    @patch("pipeline.defender.call_model")
    def test_invalid_response_returns_pipeline_error(
        self, mock_call: MagicMock
    ) -> None:
        mock_call.return_value = {
            "garbage": True,
            "_tokens": {"input": 10, "output": 20},
        }
        result: dict = run_defender(
            _valid_diff(), _valid_constitution(), "hash", _valid_challenger()
        )
        self.assertEqual(result["status"], "PIPELINE_ERROR")
        self.assertIn("raw_response", result)

    def test_input_validation_failure_returns_pipeline_error(self) -> None:
        result: dict = run_defender(
            _valid_diff(), _valid_constitution(), "hash", {}
        )
        self.assertEqual(result["status"], "PIPELINE_ERROR")
        self.assertIn("INVALID_DEFENDER_INPUT", result["error"])

    @patch("pipeline.defender.call_model")
    def test_tokens_preserved_on_all_paths(self, mock_call: MagicMock) -> None:
        mock_call.return_value = {
            "status": "CONFIRM_CLEAR",
            "summary": "ok",
            "_tokens": {"input": 5, "output": 15},
        }
        result: dict = run_defender(
            _valid_diff(), _valid_constitution(), "hash", _valid_challenger()
        )
        self.assertIn("_tokens", result)


if __name__ == "__main__":
    unittest.main()
