"""Tests for pipeline.oracle — response validation, content building, run_oracle.

All model calls are mocked. Covers: _validate_oracle_response including the
critical VETO-requires-remediation and PASS-requires-null-remediation
invariants, citation/advisory schema, confidence enum.

Run: python -m unittest tests.test_oracle -v
"""

import sys
import unittest
from typing import Any
from unittest.mock import MagicMock, patch

from pathlib import Path

_REPO_ROOT: Path = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from pipeline.oracle import (  # noqa: E402
    _build_user_content,
    _validate_oracle_response,
    run_oracle,
)


def _valid_pass() -> dict:
    return {
        "verdict": "PASS",
        "reasoning": "Change satisfies all constraints.",
        "confidence": "HIGH",
        "constraint_citations": [
            {
                "constraint_id": "C-001",
                "disposition": "SATISFIED",
                "note": "Error handling present.",
            }
        ],
        "advisories": [],
        "remediation": None,
    }


def _valid_veto() -> dict:
    return {
        "verdict": "VETO",
        "reasoning": "Silent error swallowing detected.",
        "confidence": "HIGH",
        "constraint_citations": [
            {
                "constraint_id": "C-001",
                "disposition": "VIOLATED",
                "note": "Empty except block.",
            }
        ],
        "advisories": [],
        "remediation": "Add logging or re-raise in the except block.",
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
    return {"status": "FINDINGS"}


def _valid_defender() -> dict:
    return {"status": "REBUTTAL"}


class ValidateOracleResponseTests(unittest.TestCase):
    def test_valid_pass_response(self) -> None:
        self.assertTrue(_validate_oracle_response(_valid_pass()))

    def test_valid_veto_response(self) -> None:
        self.assertTrue(_validate_oracle_response(_valid_veto()))

    def test_invalid_verdict_rejected(self) -> None:
        resp: dict = _valid_pass()
        resp["verdict"] = "ALLOW"
        self.assertFalse(_validate_oracle_response(resp))

    def test_missing_verdict_rejected(self) -> None:
        resp: dict = _valid_pass()
        del resp["verdict"]
        self.assertFalse(_validate_oracle_response(resp))

    def test_missing_reasoning_rejected(self) -> None:
        resp: dict = _valid_pass()
        del resp["reasoning"]
        self.assertFalse(_validate_oracle_response(resp))

    def test_empty_reasoning_rejected(self) -> None:
        resp: dict = _valid_pass()
        resp["reasoning"] = ""
        self.assertFalse(_validate_oracle_response(resp))

    def test_invalid_confidence_rejected(self) -> None:
        resp: dict = _valid_pass()
        resp["confidence"] = "VERY_HIGH"
        self.assertFalse(_validate_oracle_response(resp))

    def test_citations_not_list_rejected(self) -> None:
        resp: dict = _valid_pass()
        resp["constraint_citations"] = "string"
        self.assertFalse(_validate_oracle_response(resp))

    def test_citation_missing_field_rejected(self) -> None:
        resp: dict = _valid_pass()
        del resp["constraint_citations"][0]["constraint_id"]
        self.assertFalse(_validate_oracle_response(resp))

    def test_citation_invalid_disposition_rejected(self) -> None:
        resp: dict = _valid_pass()
        resp["constraint_citations"][0]["disposition"] = "MAYBE"
        self.assertFalse(_validate_oracle_response(resp))

    def test_advisories_not_list_rejected(self) -> None:
        resp: dict = _valid_pass()
        resp["advisories"] = "string"
        self.assertFalse(_validate_oracle_response(resp))

    def test_advisory_empty_string_rejected(self) -> None:
        resp: dict = _valid_pass()
        resp["advisories"] = [""]
        self.assertFalse(_validate_oracle_response(resp))

    def test_veto_without_remediation_rejected(self) -> None:
        resp: dict = _valid_veto()
        resp["remediation"] = None
        self.assertFalse(_validate_oracle_response(resp))

    def test_veto_with_empty_remediation_rejected(self) -> None:
        resp: dict = _valid_veto()
        resp["remediation"] = ""
        self.assertFalse(_validate_oracle_response(resp))

    def test_pass_with_non_null_remediation_rejected(self) -> None:
        resp: dict = _valid_pass()
        resp["remediation"] = "some text"
        self.assertFalse(_validate_oracle_response(resp))

    def test_missing_remediation_key_rejected(self) -> None:
        resp: dict = _valid_pass()
        del resp["remediation"]
        self.assertFalse(_validate_oracle_response(resp))


class BuildUserContentTests(unittest.TestCase):
    def test_contains_all_sections(self) -> None:
        content: str = _build_user_content(
            _valid_diff(),
            _valid_constitution(),
            _valid_challenger(),
            _valid_defender(),
            "",
        )
        self.assertIn("PROPOSED CHANGE:", content)
        self.assertIn("CONSTITUTION:", content)
        self.assertIn("CHALLENGER FINDINGS:", content)
        self.assertIn("DEFENDER REBUTTALS:", content)

    def test_file_context_appended_when_present(self) -> None:
        content: str = _build_user_content(
            _valid_diff(),
            _valid_constitution(),
            _valid_challenger(),
            _valid_defender(),
            "source code here",
        )
        self.assertIn("FILE CONTEXT:", content)

    def test_file_context_omitted_when_empty(self) -> None:
        content: str = _build_user_content(
            _valid_diff(),
            _valid_constitution(),
            _valid_challenger(),
            _valid_defender(),
            "",
        )
        self.assertNotIn("FILE CONTEXT:", content)


class RunOracleTests(unittest.TestCase):
    @patch("pipeline.oracle.call_model")
    def test_valid_pass_response_passed_through(
        self, mock_call: MagicMock
    ) -> None:
        resp: dict = _valid_pass()
        resp["_tokens"] = {"input": 10, "output": 20}
        mock_call.return_value = resp
        result: dict = run_oracle(
            _valid_diff(), _valid_constitution(), "hash",
            _valid_challenger(), _valid_defender(),
        )
        self.assertEqual(result["verdict"], "PASS")

    @patch("pipeline.oracle.call_model")
    def test_valid_veto_response_passed_through(
        self, mock_call: MagicMock
    ) -> None:
        resp: dict = _valid_veto()
        resp["_tokens"] = {"input": 10, "output": 20}
        mock_call.return_value = resp
        result: dict = run_oracle(
            _valid_diff(), _valid_constitution(), "hash",
            _valid_challenger(), _valid_defender(),
        )
        self.assertEqual(result["verdict"], "VETO")
        self.assertIsNotNone(result["remediation"])

    @patch("pipeline.oracle.call_model")
    def test_api_error_returns_pipeline_error(
        self, mock_call: MagicMock
    ) -> None:
        mock_call.return_value = {
            "error": "API_ERROR",
            "_tokens": {"input": 0, "output": 0},
        }
        result: dict = run_oracle(
            _valid_diff(), _valid_constitution(), "hash",
            _valid_challenger(), _valid_defender(),
        )
        self.assertEqual(result["status"], "PIPELINE_ERROR")

    @patch("pipeline.oracle.call_model")
    def test_invalid_response_returns_pipeline_error(
        self, mock_call: MagicMock
    ) -> None:
        mock_call.return_value = {
            "garbage": True,
            "_tokens": {"input": 10, "output": 20},
        }
        result: dict = run_oracle(
            _valid_diff(), _valid_constitution(), "hash",
            _valid_challenger(), _valid_defender(),
        )
        self.assertEqual(result["status"], "PIPELINE_ERROR")
        self.assertIn("raw_response", result)

    def test_input_validation_failure_returns_pipeline_error(self) -> None:
        result: dict = run_oracle(
            _valid_diff(), _valid_constitution(), "hash",
            _valid_challenger(), {},
        )
        self.assertEqual(result["status"], "PIPELINE_ERROR")
        self.assertIn("INVALID_ORACLE_INPUT", result["error"])

    @patch("pipeline.oracle.call_model")
    def test_tokens_preserved_on_all_paths(self, mock_call: MagicMock) -> None:
        resp: dict = _valid_pass()
        resp["_tokens"] = {"input": 5, "output": 15}
        mock_call.return_value = resp
        result: dict = run_oracle(
            _valid_diff(), _valid_constitution(), "hash",
            _valid_challenger(), _valid_defender(),
        )
        self.assertIn("_tokens", result)


if __name__ == "__main__":
    unittest.main()
