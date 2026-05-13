"""Tests for pipeline.defender — response validation, content building, run_defender.

All model calls are mocked. Covers: _validate_defender_response schema checks
including rebuttal field validation (bool finding_index, position enum),
_build_user_content assembly, and run_defender end-to-end flow.

Run: python -m unittest tests.test_defender -v
"""

import sys
import unittest
from typing import Any
from unittest.mock import MagicMock, patch

from pathlib import Path

_REPO_ROOT: Path = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from pipeline.defender import (  # noqa: E402
    _build_user_content,
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


class BuildUserContentTests(unittest.TestCase):
    def test_contains_all_sections(self) -> None:
        content: str = _build_user_content(
            _valid_diff(), _valid_constitution(), _valid_challenger(), ""
        )
        self.assertIn("PROPOSED CHANGE:", content)
        self.assertIn("CONSTITUTION:", content)
        self.assertIn("CHALLENGER FINDINGS:", content)

    def test_file_context_appended_when_present(self) -> None:
        content: str = _build_user_content(
            _valid_diff(), _valid_constitution(), _valid_challenger(), "source code"
        )
        self.assertIn("FILE CONTEXT:", content)

    def test_file_context_omitted_when_empty(self) -> None:
        content: str = _build_user_content(
            _valid_diff(), _valid_constitution(), _valid_challenger(), ""
        )
        self.assertNotIn("FILE CONTEXT:", content)


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
