"""Tests for pipeline.challenger — response validation, content building, run_challenger.

All model calls are mocked. Covers: _validate_challenger_response schema
checks, _build_user_content assembly, and run_challenger end-to-end flow
including error wrapping.

Run: python -m unittest tests.test_challenger -v
"""

import sys
import unittest
from typing import Any
from unittest.mock import MagicMock, patch

from pathlib import Path

_REPO_ROOT: Path = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from pipeline.challenger import (  # noqa: E402
    _build_user_content,
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


class BuildUserContentTests(unittest.TestCase):
    def test_contains_diff_and_constitution_sections(self) -> None:
        content: str = _build_user_content(_valid_diff(), _valid_constitution(), "")
        self.assertIn("PROPOSED CHANGE:", content)
        self.assertIn("CONSTITUTION:", content)

    def test_file_context_appended_when_present(self) -> None:
        content: str = _build_user_content(
            _valid_diff(), _valid_constitution(), "def foo(): pass"
        )
        self.assertIn("FILE CONTEXT:", content)
        self.assertIn("def foo(): pass", content)

    def test_file_context_omitted_when_empty(self) -> None:
        content: str = _build_user_content(_valid_diff(), _valid_constitution(), "")
        self.assertNotIn("FILE CONTEXT:", content)


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
