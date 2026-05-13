"""Tests for input validation on hook payload and pipeline stages.

Run: python -m unittest tests.test_input_validation -v
"""

import importlib.util
import sys
import unittest
from pathlib import Path
from typing import Any

_REPO_ROOT: Path = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

_hook_spec = importlib.util.spec_from_file_location(
    "pre_tool_use", str(_REPO_ROOT / "hooks" / "pre-tool-use.py")
)
_hook_mod = importlib.util.module_from_spec(_hook_spec)  # type: ignore[arg-type]
_hook_spec.loader.exec_module(_hook_mod)  # type: ignore[union-attr]
_validate_hook_payload = _hook_mod._validate_hook_payload

from pipeline.challenger import _validate_challenger_inputs  # noqa: E402
from pipeline.defender import _validate_defender_inputs  # noqa: E402
from pipeline.oracle import _validate_oracle_inputs  # noqa: E402


class HookPayloadValidationTests(unittest.TestCase):
    def test_valid_write_payload(self) -> None:
        warnings = _validate_hook_payload(
            "Write", {"file_path": "foo.py", "content": "hello"}
        )
        self.assertEqual(warnings, [])

    def test_valid_edit_payload(self) -> None:
        warnings = _validate_hook_payload(
            "Edit", {"file_path": "foo.py", "old_string": "a", "new_string": "b"}
        )
        self.assertEqual(warnings, [])

    def test_valid_multi_edit_payload(self) -> None:
        warnings = _validate_hook_payload(
            "MultiEdit",
            {"file_path": "foo.py", "edits": [{"old_string": "a", "new_string": "b"}]},
        )
        self.assertEqual(warnings, [])

    def test_empty_tool_name_warns(self) -> None:
        warnings = _validate_hook_payload("", {"file_path": "foo.py"})
        self.assertTrue(any("tool_name is empty" in w for w in warnings))

    def test_missing_file_path_warns(self) -> None:
        warnings = _validate_hook_payload("Write", {"content": "hello"})
        self.assertTrue(any("file_path" in w for w in warnings))

    def test_write_missing_content_warns(self) -> None:
        warnings = _validate_hook_payload("Write", {"file_path": "foo.py"})
        self.assertTrue(any("content" in w for w in warnings))

    def test_edit_missing_strings_warns(self) -> None:
        warnings = _validate_hook_payload("Edit", {"file_path": "foo.py"})
        self.assertTrue(any("old_string" in w for w in warnings))
        self.assertTrue(any("new_string" in w for w in warnings))

    def test_multi_edit_non_list_edits_warns(self) -> None:
        warnings = _validate_hook_payload(
            "MultiEdit", {"file_path": "foo.py", "edits": "not a list"}
        )
        self.assertTrue(any("edits" in w for w in warnings))

    def test_unknown_tool_no_field_warnings(self) -> None:
        warnings = _validate_hook_payload("Bash", {"command": "ls"})
        self.assertEqual(warnings, [])


class ChallengerInputValidationTests(unittest.TestCase):
    def _valid_diff(self) -> dict[str, Any]:
        return {"file_path": "foo.py", "change_type": "create", "content": "x"}

    def _valid_constitution(self) -> dict[str, Any]:
        return {"constraints": [{"id": "C-001", "rule": "test"}]}

    def test_valid_inputs_pass(self) -> None:
        self.assertIsNone(
            _validate_challenger_inputs(self._valid_diff(), self._valid_constitution())
        )

    def test_empty_diff_info_fails(self) -> None:
        result = _validate_challenger_inputs({}, self._valid_constitution())
        self.assertIsNotNone(result)
        self.assertIn("diff_info", result)

    def test_empty_constitution_fails(self) -> None:
        result = _validate_challenger_inputs(self._valid_diff(), {})
        self.assertIsNotNone(result)
        self.assertIn("constitution", result)

    def test_constitution_without_constraints_list_fails(self) -> None:
        result = _validate_challenger_inputs(
            self._valid_diff(), {"constraints": "not a list"}
        )
        self.assertIsNotNone(result)
        self.assertIn("constraints", result)


class DefenderInputValidationTests(unittest.TestCase):
    def _valid_diff(self) -> dict[str, Any]:
        return {"file_path": "foo.py", "change_type": "create"}

    def _valid_constitution(self) -> dict[str, Any]:
        return {"constraints": []}

    def _valid_challenger(self) -> dict[str, Any]:
        return {"status": "CLEAR"}

    def test_valid_inputs_pass(self) -> None:
        self.assertIsNone(
            _validate_defender_inputs(
                self._valid_diff(), self._valid_constitution(), self._valid_challenger()
            )
        )

    def test_missing_challenger_status_fails(self) -> None:
        result = _validate_defender_inputs(
            self._valid_diff(), self._valid_constitution(), {"findings": []}
        )
        self.assertIsNotNone(result)
        self.assertIn("status", result)

    def test_empty_challenger_result_fails(self) -> None:
        result = _validate_defender_inputs(
            self._valid_diff(), self._valid_constitution(), {}
        )
        self.assertIsNotNone(result)


class OracleInputValidationTests(unittest.TestCase):
    def _valid_diff(self) -> dict[str, Any]:
        return {"file_path": "foo.py", "change_type": "create"}

    def _valid_constitution(self) -> dict[str, Any]:
        return {"constraints": []}

    def _valid_challenger(self) -> dict[str, Any]:
        return {"status": "CLEAR"}

    def _valid_defender(self) -> dict[str, Any]:
        return {"status": "CONFIRM_CLEAR", "summary": "All clear."}

    def test_valid_inputs_pass(self) -> None:
        self.assertIsNone(
            _validate_oracle_inputs(
                self._valid_diff(),
                self._valid_constitution(),
                self._valid_challenger(),
                self._valid_defender(),
            )
        )

    def test_missing_defender_status_fails(self) -> None:
        result = _validate_oracle_inputs(
            self._valid_diff(),
            self._valid_constitution(),
            self._valid_challenger(),
            {"summary": "No status"},
        )
        self.assertIsNotNone(result)
        self.assertIn("defender_result", result)

    def test_empty_diff_fails(self) -> None:
        result = _validate_oracle_inputs(
            {},
            self._valid_constitution(),
            self._valid_challenger(),
            self._valid_defender(),
        )
        self.assertIsNotNone(result)


if __name__ == "__main__":
    unittest.main()
