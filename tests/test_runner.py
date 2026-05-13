"""Tests for pipeline.runner — orchestration, fail-open, CLEAR optimization, tokens.

All pipeline stages, constitution loading, and ledger append are mocked.
Covers: happy paths (PASS/VETO), fail-open on every error source,
CLEAR-skips-defender optimization, token accumulation, and finalize behavior.

Run: python -m unittest tests.test_runner -v
"""

import sys
import unittest
from typing import Any
from unittest.mock import MagicMock, call, patch

from pathlib import Path

_REPO_ROOT: Path = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from pipeline.constitution import ConstitutionError  # noqa: E402
from pipeline.runner import run_governance_pipeline  # noqa: E402


_MOCK_CONSTITUTION: tuple[dict, str] = (
    {"constraints": [{"id": "C-001", "name": "Test", "rule": "...", "severity": "veto"}]},
    "abc123hash",
)

_DIFF: dict = {"file_path": "test.py", "change_type": "edit"}
_TOOL_INPUT: dict = {"file_path": "test.py"}


def _clear_challenger() -> dict:
    return {"status": "CLEAR", "findings": [], "_tokens": {"input": 10, "output": 20}}


def _findings_challenger() -> dict:
    return {"status": "FINDINGS", "findings": [{"constraint_id": "C-001"}], "_tokens": {"input": 10, "output": 20}}


def _rebuttal_defender() -> dict:
    return {"status": "REBUTTAL", "summary": "Rebutted.", "rebuttals": [], "_tokens": {"input": 30, "output": 40}}


def _pass_oracle() -> dict:
    return {
        "verdict": "PASS",
        "reasoning": "All good.",
        "remediation": None,
        "status": "ok",
        "_tokens": {"input": 50, "output": 60},
    }


def _veto_oracle() -> dict:
    return {
        "verdict": "VETO",
        "reasoning": "Violation found.",
        "remediation": "Fix the error handling.",
        "status": "ok",
        "_tokens": {"input": 50, "output": 60},
    }


def _pipeline_error_stage() -> dict:
    return {"status": "PIPELINE_ERROR", "error": "something broke", "_tokens": {"input": 0, "output": 0}}


@patch("pipeline.runner.append_entry", return_value={})
@patch("pipeline.runner.run_oracle")
@patch("pipeline.runner.run_defender")
@patch("pipeline.runner.run_challenger")
@patch("pipeline.runner.load_constitution_snapshot")
class HappyPathTests(unittest.TestCase):
    def test_pass_with_clear_challenger(
        self, mock_const: MagicMock, mock_chall: MagicMock,
        mock_def: MagicMock, mock_oracle: MagicMock, mock_ledger: MagicMock,
    ) -> None:
        mock_const.return_value = _MOCK_CONSTITUTION
        mock_chall.return_value = _clear_challenger()
        mock_oracle.return_value = _pass_oracle()
        result: dict = run_governance_pipeline("Write", _TOOL_INPUT, _DIFF)
        self.assertEqual(result["verdict"], "PASS")
        mock_def.assert_not_called()

    def test_pass_with_findings_challenger(
        self, mock_const: MagicMock, mock_chall: MagicMock,
        mock_def: MagicMock, mock_oracle: MagicMock, mock_ledger: MagicMock,
    ) -> None:
        mock_const.return_value = _MOCK_CONSTITUTION
        mock_chall.return_value = _findings_challenger()
        mock_def.return_value = _rebuttal_defender()
        mock_oracle.return_value = _pass_oracle()
        result: dict = run_governance_pipeline("Write", _TOOL_INPUT, _DIFF)
        self.assertEqual(result["verdict"], "PASS")
        mock_def.assert_called_once()

    def test_veto_verdict_propagated(
        self, mock_const: MagicMock, mock_chall: MagicMock,
        mock_def: MagicMock, mock_oracle: MagicMock, mock_ledger: MagicMock,
    ) -> None:
        mock_const.return_value = _MOCK_CONSTITUTION
        mock_chall.return_value = _findings_challenger()
        mock_def.return_value = _rebuttal_defender()
        mock_oracle.return_value = _veto_oracle()
        result: dict = run_governance_pipeline("Write", _TOOL_INPUT, _DIFF)
        self.assertEqual(result["verdict"], "VETO")
        self.assertIsNotNone(result["remediation"])


@patch("pipeline.runner.append_entry", return_value={})
@patch("pipeline.runner.run_oracle")
@patch("pipeline.runner.run_defender")
@patch("pipeline.runner.run_challenger")
@patch("pipeline.runner.load_constitution_snapshot")
class FailOpenTests(unittest.TestCase):
    def test_constitution_load_failure_fails_open(
        self, mock_const: MagicMock, mock_chall: MagicMock,
        mock_def: MagicMock, mock_oracle: MagicMock, mock_ledger: MagicMock,
    ) -> None:
        mock_const.side_effect = ConstitutionError("file missing")
        result: dict = run_governance_pipeline("Write", _TOOL_INPUT, _DIFF)
        self.assertEqual(result["verdict"], "PASS")
        self.assertTrue(result.get("pipeline_error"))

    def test_challenger_pipeline_error_fails_open(
        self, mock_const: MagicMock, mock_chall: MagicMock,
        mock_def: MagicMock, mock_oracle: MagicMock, mock_ledger: MagicMock,
    ) -> None:
        mock_const.return_value = _MOCK_CONSTITUTION
        mock_chall.return_value = _pipeline_error_stage()
        result: dict = run_governance_pipeline("Write", _TOOL_INPUT, _DIFF)
        self.assertEqual(result["verdict"], "PASS")
        self.assertTrue(result.get("pipeline_error"))

    def test_defender_pipeline_error_fails_open(
        self, mock_const: MagicMock, mock_chall: MagicMock,
        mock_def: MagicMock, mock_oracle: MagicMock, mock_ledger: MagicMock,
    ) -> None:
        mock_const.return_value = _MOCK_CONSTITUTION
        mock_chall.return_value = _findings_challenger()
        mock_def.return_value = _pipeline_error_stage()
        result: dict = run_governance_pipeline("Write", _TOOL_INPUT, _DIFF)
        self.assertEqual(result["verdict"], "PASS")
        self.assertTrue(result.get("pipeline_error"))

    def test_oracle_pipeline_error_fails_open(
        self, mock_const: MagicMock, mock_chall: MagicMock,
        mock_def: MagicMock, mock_oracle: MagicMock, mock_ledger: MagicMock,
    ) -> None:
        mock_const.return_value = _MOCK_CONSTITUTION
        mock_chall.return_value = _findings_challenger()
        mock_def.return_value = _rebuttal_defender()
        mock_oracle.return_value = _pipeline_error_stage()
        result: dict = run_governance_pipeline("Write", _TOOL_INPUT, _DIFF)
        self.assertEqual(result["verdict"], "PASS")
        self.assertTrue(result.get("pipeline_error"))

    def test_ledger_failure_does_not_block_verdict(
        self, mock_const: MagicMock, mock_chall: MagicMock,
        mock_def: MagicMock, mock_oracle: MagicMock, mock_ledger: MagicMock,
    ) -> None:
        mock_const.return_value = _MOCK_CONSTITUTION
        mock_chall.return_value = _clear_challenger()
        mock_oracle.return_value = _pass_oracle()
        mock_ledger.side_effect = Exception("disk full")
        result: dict = run_governance_pipeline("Write", _TOOL_INPUT, _DIFF)
        self.assertEqual(result["verdict"], "PASS")


@patch("pipeline.runner.append_entry", return_value={})
@patch("pipeline.runner.run_oracle")
@patch("pipeline.runner.run_defender")
@patch("pipeline.runner.run_challenger")
@patch("pipeline.runner.load_constitution_snapshot")
class ClearOptimizationTests(unittest.TestCase):
    def test_clear_challenger_skips_defender_call(
        self, mock_const: MagicMock, mock_chall: MagicMock,
        mock_def: MagicMock, mock_oracle: MagicMock, mock_ledger: MagicMock,
    ) -> None:
        mock_const.return_value = _MOCK_CONSTITUTION
        mock_chall.return_value = _clear_challenger()
        mock_oracle.return_value = _pass_oracle()
        run_governance_pipeline("Write", _TOOL_INPUT, _DIFF)
        mock_def.assert_not_called()

    def test_synthetic_defender_result_has_confirm_clear(
        self, mock_const: MagicMock, mock_chall: MagicMock,
        mock_def: MagicMock, mock_oracle: MagicMock, mock_ledger: MagicMock,
    ) -> None:
        mock_const.return_value = _MOCK_CONSTITUTION
        mock_chall.return_value = _clear_challenger()
        mock_oracle.return_value = _pass_oracle()
        result: dict = run_governance_pipeline("Write", _TOOL_INPUT, _DIFF)
        self.assertEqual(result["defender"]["status"], "CONFIRM_CLEAR")


@patch("pipeline.runner.append_entry", return_value={})
@patch("pipeline.runner.run_oracle")
@patch("pipeline.runner.run_defender")
@patch("pipeline.runner.run_challenger")
@patch("pipeline.runner.load_constitution_snapshot")
class TokenAccumulationTests(unittest.TestCase):
    def test_tokens_accumulated_across_all_stages(
        self, mock_const: MagicMock, mock_chall: MagicMock,
        mock_def: MagicMock, mock_oracle: MagicMock, mock_ledger: MagicMock,
    ) -> None:
        mock_const.return_value = _MOCK_CONSTITUTION
        mock_chall.return_value = _findings_challenger()
        mock_def.return_value = _rebuttal_defender()
        mock_oracle.return_value = _pass_oracle()
        result: dict = run_governance_pipeline("Write", _TOOL_INPUT, _DIFF)
        self.assertEqual(result["_tokens"]["input"], 90)
        self.assertEqual(result["_tokens"]["output"], 120)

    def test_malformed_tokens_treated_as_zero(
        self, mock_const: MagicMock, mock_chall: MagicMock,
        mock_def: MagicMock, mock_oracle: MagicMock, mock_ledger: MagicMock,
    ) -> None:
        mock_const.return_value = _MOCK_CONSTITUTION
        chall: dict = _clear_challenger()
        chall["_tokens"] = "bad"
        mock_chall.return_value = chall
        oracle: dict = _pass_oracle()
        oracle["_tokens"] = {"input": 10, "output": 20}
        mock_oracle.return_value = oracle
        result: dict = run_governance_pipeline("Write", _TOOL_INPUT, _DIFF)
        self.assertEqual(result["_tokens"]["input"], 10)

    def test_bool_tokens_ignored(
        self, mock_const: MagicMock, mock_chall: MagicMock,
        mock_def: MagicMock, mock_oracle: MagicMock, mock_ledger: MagicMock,
    ) -> None:
        mock_const.return_value = _MOCK_CONSTITUTION
        chall: dict = _clear_challenger()
        chall["_tokens"] = {"input": True, "output": False}
        mock_chall.return_value = chall
        oracle: dict = _pass_oracle()
        oracle["_tokens"] = {"input": 10, "output": 20}
        mock_oracle.return_value = oracle
        result: dict = run_governance_pipeline("Write", _TOOL_INPUT, _DIFF)
        self.assertEqual(result["_tokens"]["input"], 10)


@patch("pipeline.runner.append_entry", return_value={})
@patch("pipeline.runner.run_oracle")
@patch("pipeline.runner.run_defender")
@patch("pipeline.runner.run_challenger")
@patch("pipeline.runner.load_constitution_snapshot")
class FinalizeTests(unittest.TestCase):
    def test_change_context_attached_to_result(
        self, mock_const: MagicMock, mock_chall: MagicMock,
        mock_def: MagicMock, mock_oracle: MagicMock, mock_ledger: MagicMock,
    ) -> None:
        mock_const.return_value = _MOCK_CONSTITUTION
        mock_chall.return_value = _clear_challenger()
        mock_oracle.return_value = _pass_oracle()
        result: dict = run_governance_pipeline("Write", _TOOL_INPUT, _DIFF)
        self.assertIn("change", result)
        self.assertEqual(result["change"]["tool"], "Write")

    def test_append_entry_called_with_result(
        self, mock_const: MagicMock, mock_chall: MagicMock,
        mock_def: MagicMock, mock_oracle: MagicMock, mock_ledger: MagicMock,
    ) -> None:
        mock_const.return_value = _MOCK_CONSTITUTION
        mock_chall.return_value = _clear_challenger()
        mock_oracle.return_value = _pass_oracle()
        run_governance_pipeline("Write", _TOOL_INPUT, _DIFF)
        mock_ledger.assert_called_once()
        appended: dict = mock_ledger.call_args[0][0]
        self.assertIn("verdict", appended)


if __name__ == "__main__":
    unittest.main()
