"""Tests for the recorded per-stage model override (roadmap 3.3).

``BENCH_CHALLENGER_MODEL``, ``BENCH_DEFENDER_MODEL``, and
``BENCH_ORACLE_MODEL`` replace the constants in utils/api.py for one stage
each. An override is never silent: the stage writes the model it ran on
and whether it was overridden into its result on every path after the
call, error or not, and a note goes to stderr when the override is read.
These tests pin the resolver, the stamp, and each stage's recording.

Run: python -m unittest tests.test_stage_models -v
"""

import io
import os
import sys
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import MagicMock, patch

_REPO_ROOT: Path = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from pipeline.challenger import run_challenger  # noqa: E402
from pipeline.defender import run_defender  # noqa: E402
from pipeline.oracle import run_oracle  # noqa: E402
from tests.test_challenger import _valid_constitution, _valid_diff  # noqa: E402
from tests.test_defender import _valid_challenger as _defender_input  # noqa: E402
from tests.test_oracle import (  # noqa: E402
    _valid_challenger as _oracle_challenger,
    _valid_defender as _oracle_defender,
    _valid_pass,
)
from utils.api import (  # noqa: E402
    CHALLENGER_MODEL,
    DEFENDER_MODEL,
    ORACLE_MODEL,
    STAGE_MODEL_ENV,
    stage_model,
    stamp_model,
)

_ALL_OVERRIDES: dict[str, str] = {name: "" for name in STAGE_MODEL_ENV.values()}
_API_ERROR: dict = {"error": "API_ERROR", "detail": "boom", "_tokens": {"input": 0, "output": 0}}


class StageModelResolverTests(unittest.TestCase):
    def test_defaults_are_the_constants_with_no_override(self) -> None:
        with patch.dict(os.environ, _ALL_OVERRIDES):
            self.assertEqual(stage_model("challenger"), (CHALLENGER_MODEL, False))
            self.assertEqual(stage_model("defender"), (DEFENDER_MODEL, False))
            self.assertEqual(stage_model("oracle"), (ORACLE_MODEL, False))

    def test_override_replaces_one_stage_and_says_so_on_stderr(self) -> None:
        err = io.StringIO()
        with patch.dict(os.environ, {**_ALL_OVERRIDES, "BENCH_ORACLE_MODEL": "claude-sonnet-5"}):
            with redirect_stderr(err):
                self.assertEqual(stage_model("oracle"), ("claude-sonnet-5", True))
                self.assertEqual(stage_model("challenger"), (CHALLENGER_MODEL, False))
        self.assertIn("BENCH_ORACLE_MODEL", err.getvalue())
        self.assertIn("claude-sonnet-5", err.getvalue())
        self.assertIn(ORACLE_MODEL, err.getvalue())

    def test_blank_override_means_the_constant(self) -> None:
        with patch.dict(os.environ, {**_ALL_OVERRIDES, "BENCH_DEFENDER_MODEL": "   "}):
            self.assertEqual(stage_model("defender"), (DEFENDER_MODEL, False))

    def test_override_is_used_as_given_after_trimming(self) -> None:
        with patch.dict(os.environ, {**_ALL_OVERRIDES, "BENCH_CHALLENGER_MODEL": " claude-x "}):
            with redirect_stderr(io.StringIO()):
                self.assertEqual(stage_model("challenger"), ("claude-x", True))

    def test_unknown_stage_is_a_programming_error(self) -> None:
        with self.assertRaises(ValueError):
            stage_model("utility")

    def test_stamp_model_adds_both_keys_and_returns_the_same_dict(self) -> None:
        result: dict = {"status": "CLEAR", "_tokens": {}}
        stamped: dict = stamp_model(result, "claude-x", True)
        self.assertIs(stamped, result)
        self.assertEqual(result["_model"], "claude-x")
        self.assertTrue(result["_model_override"])
        self.assertEqual(result["status"], "CLEAR")


@patch.dict(os.environ, _ALL_OVERRIDES)
class StageRecordingTests(unittest.TestCase):
    """Every stage records its model on success and on every error path."""

    @patch("pipeline.challenger.call_model")
    def test_challenger_success_and_error_paths(self, mock_call: MagicMock) -> None:
        mock_call.return_value = {"status": "CLEAR", "findings": [], "_tokens": {"input": 1, "output": 1}}
        result: dict = run_challenger(_valid_diff(), _valid_constitution(), "hash")
        self.assertEqual(result["status"], "CLEAR")
        self.assertEqual(result["_model"], CHALLENGER_MODEL)
        self.assertFalse(result["_model_override"])
        self.assertEqual(mock_call.call_args.args[0], CHALLENGER_MODEL)

        mock_call.return_value = dict(_API_ERROR)
        error: dict = run_challenger(_valid_diff(), _valid_constitution(), "hash")
        self.assertEqual(error["status"], "PIPELINE_ERROR")
        self.assertEqual(error["_model"], CHALLENGER_MODEL)

        mock_call.return_value = {"status": "NONSENSE", "_tokens": {}}
        invalid: dict = run_challenger(_valid_diff(), _valid_constitution(), "hash")
        self.assertEqual(invalid["error"], "INVALID_CHALLENGER_RESPONSE")
        self.assertEqual(invalid["_model"], CHALLENGER_MODEL)

    @patch("pipeline.challenger.call_model")
    def test_challenger_override_is_used_and_recorded(self, mock_call: MagicMock) -> None:
        mock_call.return_value = {"status": "CLEAR", "findings": [], "_tokens": {}}
        with patch.dict(os.environ, {"BENCH_CHALLENGER_MODEL": "claude-x"}):
            with redirect_stderr(io.StringIO()):
                result: dict = run_challenger(_valid_diff(), _valid_constitution(), "hash")
        self.assertEqual(mock_call.call_args.args[0], "claude-x")
        self.assertEqual(result["_model"], "claude-x")
        self.assertTrue(result["_model_override"])

    @patch("pipeline.defender.call_model")
    def test_defender_records_on_error_and_override(self, mock_call: MagicMock) -> None:
        mock_call.return_value = dict(_API_ERROR)
        error: dict = run_defender(_valid_diff(), _valid_constitution(), "hash", _defender_input())
        self.assertEqual(error["status"], "PIPELINE_ERROR")
        self.assertEqual(error["_model"], DEFENDER_MODEL)
        self.assertFalse(error["_model_override"])
        with patch.dict(os.environ, {"BENCH_DEFENDER_MODEL": "claude-y"}):
            with redirect_stderr(io.StringIO()):
                overridden: dict = run_defender(
                    _valid_diff(), _valid_constitution(), "hash", _defender_input()
                )
        self.assertEqual(mock_call.call_args.args[0], "claude-y")
        self.assertEqual(overridden["_model"], "claude-y")
        self.assertTrue(overridden["_model_override"])

    @patch("pipeline.oracle.call_model")
    def test_oracle_records_on_success_error_and_override(self, mock_call: MagicMock) -> None:
        mock_call.return_value = {**_valid_pass(), "_tokens": {"input": 1, "output": 1}}
        result: dict = run_oracle(
            _valid_diff(), _valid_constitution(), "hash", _oracle_challenger(), _oracle_defender()
        )
        self.assertEqual(result["verdict"], "PASS")
        self.assertEqual(result["_model"], ORACLE_MODEL)
        self.assertFalse(result["_model_override"])

        mock_call.return_value = dict(_API_ERROR)
        error: dict = run_oracle(
            _valid_diff(), _valid_constitution(), "hash", _oracle_challenger(), _oracle_defender()
        )
        self.assertEqual(error["status"], "PIPELINE_ERROR")
        self.assertEqual(error["_model"], ORACLE_MODEL)

        mock_call.return_value = {**_valid_pass(), "_tokens": {}}
        with patch.dict(os.environ, {"BENCH_ORACLE_MODEL": "claude-z"}):
            with redirect_stderr(io.StringIO()):
                overridden: dict = run_oracle(
                    _valid_diff(), _valid_constitution(), "hash",
                    _oracle_challenger(), _oracle_defender(),
                )
        self.assertEqual(mock_call.call_args.args[0], "claude-z")
        self.assertEqual(overridden["_model"], "claude-z")
        self.assertTrue(overridden["_model_override"])

    @patch("pipeline.challenger.call_model")
    @patch("pipeline.defender.call_model")
    @patch("pipeline.oracle.call_model")
    def test_input_rejected_before_the_call_records_no_call(
        self, mock_oracle: MagicMock, mock_defender: MagicMock, mock_challenger: MagicMock
    ) -> None:
        # No model ran, and the result says so with an explicit None: a
        # missing key would read as an entry from before models were
        # recorded, and the constant would claim a call that never happened.
        for stage, run in (
            ("challenger", lambda: run_challenger({}, _valid_constitution(), "hash")),
            ("defender", lambda: run_defender({}, _valid_constitution(), "hash", _defender_input())),
            (
                "oracle",
                lambda: run_oracle(
                    {}, _valid_constitution(), "hash", _oracle_challenger(), _oracle_defender()
                ),
            ),
        ):
            with self.subTest(stage=stage):
                result: dict = run()
                self.assertEqual(result["status"], "PIPELINE_ERROR")
                self.assertIn("_model", result)
                self.assertIsNone(result["_model"])
                self.assertFalse(result["_model_override"])
        mock_challenger.assert_not_called()
        mock_defender.assert_not_called()
        mock_oracle.assert_not_called()


if __name__ == "__main__":
    unittest.main()
