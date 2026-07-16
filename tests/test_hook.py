"""Tests for hooks/pre-tool-use.py — response builders, verdict translation, main flow.

The hook module uses a hyphen in its filename, so it is imported via
importlib (same pattern as test_input_validation.py). Pipeline execution
is mocked to prevent real API calls.

Run: python -m unittest tests.test_hook -v
"""

import importlib.util
import io
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

_REPO_ROOT: Path = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

_HOOK_PATH: Path = _REPO_ROOT / "hooks" / "pre-tool-use.py"
_spec = importlib.util.spec_from_file_location("pre_tool_use", str(_HOOK_PATH))
_hook_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_hook_module)

build_allow_response = _hook_module.build_allow_response
build_deny_response = _hook_module.build_deny_response
build_response_from_verdict = _hook_module.build_response_from_verdict
main = _hook_module.main


class BuildAllowResponseTests(unittest.TestCase):
    def test_structure_matches_schema(self) -> None:
        resp: dict = build_allow_response("test message")
        hook_out: dict = resp["hookSpecificOutput"]
        self.assertEqual(hook_out["hookEventName"], "PreToolUse")
        self.assertEqual(hook_out["permissionDecision"], "allow")

    def test_message_in_additional_context(self) -> None:
        resp: dict = build_allow_response("governance passed")
        self.assertEqual(
            resp["hookSpecificOutput"]["additionalContext"], "governance passed"
        )


class BuildDenyResponseTests(unittest.TestCase):
    def test_structure_matches_schema(self) -> None:
        resp: dict = build_deny_response("VETO C-001", "fix the error")
        hook_out: dict = resp["hookSpecificOutput"]
        self.assertEqual(hook_out["permissionDecision"], "deny")

    def test_reason_and_remediation_placed_correctly(self) -> None:
        resp: dict = build_deny_response("VETO C-001", "fix the error")
        hook_out: dict = resp["hookSpecificOutput"]
        self.assertEqual(hook_out["permissionDecisionReason"], "VETO C-001")
        self.assertEqual(hook_out["additionalContext"], "fix the error")


class BuildResponseFromVerdictTests(unittest.TestCase):
    def test_pass_verdict_returns_allow(self) -> None:
        resp: dict = build_response_from_verdict({"verdict": "PASS"})
        self.assertEqual(
            resp["hookSpecificOutput"]["permissionDecision"], "allow"
        )

    def test_veto_verdict_returns_deny(self) -> None:
        resp: dict = build_response_from_verdict({
            "verdict": "VETO",
            "reason": "C-001 violated",
            "remediation": "Add error handling",
        })
        self.assertEqual(
            resp["hookSpecificOutput"]["permissionDecision"], "deny"
        )

    def test_veto_uses_default_reason_when_missing(self) -> None:
        resp: dict = build_response_from_verdict({"verdict": "VETO"})
        reason: str = resp["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertTrue(len(reason) > 0)

    def test_veto_uses_default_remediation_when_missing(self) -> None:
        resp: dict = build_response_from_verdict({"verdict": "VETO"})
        ctx: str = resp["hookSpecificOutput"]["additionalContext"]
        self.assertTrue(len(ctx) > 0)

    def test_missing_verdict_treated_as_pass(self) -> None:
        resp: dict = build_response_from_verdict({})
        self.assertEqual(
            resp["hookSpecificOutput"]["permissionDecision"], "allow"
        )

    def test_veto_reason_carries_constraint_tag(self) -> None:
        resp: dict = build_response_from_verdict({
            "verdict": "VETO",
            "reason": "Silent exception swallowing detected.",
            "remediation": "Log or re-throw in the catch block.",
            "violated_constraints": ["C-001", "C-004"],
        })
        hook_out: dict = resp["hookSpecificOutput"]
        self.assertEqual(
            hook_out["permissionDecisionReason"],
            "BENCH VETO [C-001, C-004]: Silent exception swallowing detected.",
        )
        self.assertEqual(
            hook_out["additionalContext"],
            "Remediation: Log or re-throw in the catch block.",
        )

    def test_veto_reason_without_citations_omits_tag(self) -> None:
        resp: dict = build_response_from_verdict({
            "verdict": "VETO",
            "reason": "Rejected.",
            "remediation": "Fix it.",
        })
        reason: str = resp["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertEqual(reason, "BENCH VETO: Rejected.")

    def test_veto_default_remediation_is_prefixed(self) -> None:
        resp: dict = build_response_from_verdict({"verdict": "VETO"})
        ctx: str = resp["hookSpecificOutput"]["additionalContext"]
        self.assertTrue(ctx.startswith("Remediation: "))

    def test_pass_appends_advisories(self) -> None:
        resp: dict = build_response_from_verdict({
            "verdict": "PASS",
            "advisories": ["Consider a test for the new branch."],
        })
        ctx: str = resp["hookSpecificOutput"]["additionalContext"]
        self.assertEqual(
            ctx,
            "Bench governance: PASS. All constraints satisfied. "
            "Advisories: Consider a test for the new branch.",
        )

    def test_pass_without_advisories_keeps_fixed_message(self) -> None:
        resp: dict = build_response_from_verdict({
            "verdict": "PASS",
            "advisories": [],
        })
        self.assertEqual(
            resp["hookSpecificOutput"]["additionalContext"],
            "Bench governance: PASS. All constraints satisfied.",
        )

    def test_pass_ignores_non_string_advisories(self) -> None:
        resp: dict = build_response_from_verdict({
            "verdict": "PASS",
            "advisories": [None, 42, "", "Real advisory."],
        })
        ctx: str = resp["hookSpecificOutput"]["additionalContext"]
        self.assertEqual(
            ctx,
            "Bench governance: PASS. All constraints satisfied. "
            "Advisories: Real advisory.",
        )


class MainFlowTests(unittest.TestCase):
    def _run_main_with_stdin(self, stdin_content: str) -> tuple[int, str]:
        """Run main() with mocked stdin/stdout, return (exit_code, stdout_text)."""
        mock_stdin: io.StringIO = io.StringIO(stdin_content)
        mock_stdout: io.StringIO = io.StringIO()
        with patch.object(sys, "stdin", mock_stdin), \
             patch.object(sys, "stdout", mock_stdout):
            exit_code: int = main()
        return exit_code, mock_stdout.getvalue()

    @patch.object(_hook_module, "run_governance_pipeline")
    def test_governed_tool_invokes_pipeline(
        self, mock_pipeline: MagicMock
    ) -> None:
        mock_pipeline.return_value = {"verdict": "PASS"}
        payload: str = json.dumps({
            "tool_name": "Write",
            "tool_input": {"file_path": "test.py", "content": "hello"},
        })
        code, output = self._run_main_with_stdin(payload)
        self.assertEqual(code, 0)
        mock_pipeline.assert_called_once()

    def test_pipeline_import_failure_fails_closed(self) -> None:
        original = _hook_module.run_governance_pipeline
        try:
            _hook_module.run_governance_pipeline = None
            payload: str = json.dumps({
                "tool_name": "Write",
                "tool_input": {"file_path": "test.py", "content": "hello"},
            })
            code, output = self._run_main_with_stdin(payload)
            self.assertEqual(code, 0)
            resp: dict = json.loads(output)
            self.assertEqual(
                resp["hookSpecificOutput"]["permissionDecision"], "deny"
            )
        finally:
            _hook_module.run_governance_pipeline = original

    def test_invalid_json_stdin_fails_closed(self) -> None:
        code, output = self._run_main_with_stdin("{{{bad json")
        self.assertEqual(code, 0)
        resp: dict = json.loads(output)
        self.assertEqual(
            resp["hookSpecificOutput"]["permissionDecision"], "deny"
        )

    def test_non_dict_payload_fails_closed(self) -> None:
        code, output = self._run_main_with_stdin("[1, 2, 3]")
        self.assertEqual(code, 0)
        resp: dict = json.loads(output)
        self.assertEqual(
            resp["hookSpecificOutput"]["permissionDecision"], "deny"
        )

    @patch.object(_hook_module, "run_governance_pipeline")
    def test_pipeline_exception_fails_closed(
        self, mock_pipeline: MagicMock
    ) -> None:
        # An unexpected exception from the pipeline hits the outer handler,
        # which now denies (fail closed) instead of allowing, while still
        # returning exit 0 per Absolute Rule 6.
        mock_pipeline.side_effect = RuntimeError("boom")
        payload: str = json.dumps({
            "tool_name": "Write",
            "tool_input": {"file_path": "test.py", "content": "hello"},
        })
        code, output = self._run_main_with_stdin(payload)
        self.assertEqual(code, 0)
        resp: dict = json.loads(output)
        self.assertEqual(
            resp["hookSpecificOutput"]["permissionDecision"], "deny"
        )

    @patch.object(_hook_module, "run_governance_pipeline")
    def test_always_returns_zero(self, mock_pipeline: MagicMock) -> None:
        mock_pipeline.return_value = {"verdict": "VETO", "reason": "x", "remediation": "y"}
        payload: str = json.dumps({
            "tool_name": "Write",
            "tool_input": {"file_path": "test.py", "content": "hello"},
        })
        code, _ = self._run_main_with_stdin(payload)
        self.assertEqual(code, 0)

    @patch.object(_hook_module, "run_governance_pipeline")
    def test_stdout_is_valid_json(self, mock_pipeline: MagicMock) -> None:
        mock_pipeline.return_value = {"verdict": "PASS"}
        payload: str = json.dumps({
            "tool_name": "Write",
            "tool_input": {"file_path": "test.py", "content": "hello"},
        })
        _, output = self._run_main_with_stdin(payload)
        parsed: dict = json.loads(output)
        self.assertIn("hookSpecificOutput", parsed)

    @patch.object(_hook_module, "run_governance_pipeline")
    def test_bench_subprocess_env_bypasses_pipeline(
        self, mock_pipeline: MagicMock
    ) -> None:
        # With BENCH_SUBPROCESS=1 the hook must fail open WITHOUT governing,
        # even for a payload the pipeline would VETO. A bypass returns 'allow'
        # and never calls the pipeline; the normal path would deny.
        mock_pipeline.return_value = {
            "verdict": "VETO", "reason": "x", "remediation": "y",
        }
        payload: str = json.dumps({
            "tool_name": "Write",
            "tool_input": {"file_path": "test.py", "content": "hello"},
        })
        with patch.dict("os.environ", {"BENCH_SUBPROCESS": "1"}):
            code, output = self._run_main_with_stdin(payload)
        self.assertEqual(code, 0)
        resp: dict = json.loads(output)
        self.assertEqual(
            resp["hookSpecificOutput"]["permissionDecision"], "allow"
        )
        mock_pipeline.assert_not_called()

    @patch.object(_hook_module, "run_governance_pipeline")
    def test_bench_subprocess_short_circuits_before_parsing_stdin(
        self, mock_pipeline: MagicMock
    ) -> None:
        # Malformed stdin: the bypass returns allow with its distinctive message
        # before any parse, proving it short-circuits ahead of pipeline work.
        with patch.dict("os.environ", {"BENCH_SUBPROCESS": "1"}):
            code, output = self._run_main_with_stdin("{{{ not json")
        self.assertEqual(code, 0)
        resp: dict = json.loads(output)
        self.assertEqual(
            resp["hookSpecificOutput"]["permissionDecision"], "allow"
        )
        self.assertIn(
            "nested subprocess",
            resp["hookSpecificOutput"]["additionalContext"],
        )
        mock_pipeline.assert_not_called()


class ExtractDiffInfoFallbackTests(unittest.TestCase):
    """The inline fallback (used when utils.diff fails to import) must use the
    same project-root containment as utils.diff._normalize_path, not block every
    absolute path (Write/Edit always supply absolute paths)."""

    def test_fallback_allows_in_root_absolute_path(self) -> None:
        import os

        abs_in_root: str = os.path.join(os.getcwd(), "utils", "api.py")
        original = _hook_module._build_diff_info_hardened
        try:
            _hook_module._build_diff_info_hardened = None  # force fallback
            info: dict = _hook_module.extract_diff_info(
                "Edit",
                {"file_path": abs_in_root, "old_string": "a", "new_string": "b"},
            )
        finally:
            _hook_module._build_diff_info_hardened = original
        self.assertNotEqual(info["file_path"], "[PATH_TRAVERSAL_BLOCKED]")
        self.assertEqual(info["file_path"], os.path.join("utils", "api.py"))
        self.assertNotIn("_path_normalized_external", info)

    def test_fallback_escape_produces_absolute_with_external_flag(self) -> None:
        import os

        original = _hook_module._build_diff_info_hardened
        try:
            _hook_module._build_diff_info_hardened = None
            info: dict = _hook_module.extract_diff_info(
                "Edit",
                {
                    "file_path": "../../../etc/passwd",
                    "old_string": "a",
                    "new_string": "b",
                },
            )
        finally:
            _hook_module._build_diff_info_hardened = original
        self.assertNotEqual(info["file_path"], "[PATH_TRAVERSAL_BLOCKED]")
        self.assertTrue(os.path.isabs(info["file_path"]))
        self.assertTrue(info.get("_path_normalized_external"))


class TestFallbackExternalNormalization(unittest.TestCase):
    """Exercises both CWD-normalization branches in extract_diff_info's
    fallback path: the escapes-repo-root branch and a simulated cross-drive
    ValueError branch. Covers C-005 requirement cited in the docstring."""

    def test_escape_repo_root_normalizes_relative_to_cwd(self) -> None:
        import os
        import tempfile

        original = _hook_module._build_diff_info_hardened
        original_cwd: str = os.getcwd()
        with tempfile.TemporaryDirectory() as tmpdir:
            external_file: str = os.path.join(tmpdir, "src", "app.py")
            try:
                _hook_module._build_diff_info_hardened = None
                os.chdir(tmpdir)
                info: dict = _hook_module.extract_diff_info(
                    "Write",
                    {"file_path": external_file, "content": "x = 1"},
                )
            finally:
                os.chdir(original_cwd)
                _hook_module._build_diff_info_hardened = original
        self.assertEqual(info["file_path"], os.path.join("src", "app.py"))
        self.assertTrue(info.get("_path_normalized_external"))

    def test_cross_drive_valueerror_normalizes_to_cwd(self) -> None:
        import os

        original = _hook_module._build_diff_info_hardened
        original_cwd: str = os.getcwd()
        try:
            _hook_module._build_diff_info_hardened = None
            original_relpath = os.path.relpath
            call_count: list[int] = [0]

            def mock_relpath(path: str, start: str) -> str:
                call_count[0] += 1
                if call_count[0] == 1:
                    raise ValueError("path is on mount 'D:', start on mount 'C:'")
                return original_relpath(path, start)

            with patch("os.path.relpath", side_effect=mock_relpath):
                info: dict = _hook_module.extract_diff_info(
                    "Edit",
                    {
                        "file_path": os.path.join(os.getcwd(), "utils", "api.py"),
                        "old_string": "a",
                        "new_string": "b",
                    },
                )
        finally:
            _hook_module._build_diff_info_hardened = original
        self.assertTrue(info.get("_path_normalized_external"))
        self.assertNotEqual(info["file_path"], "[PATH_TRAVERSAL_BLOCKED]")

    def test_escape_both_roots_returns_absolute(self) -> None:
        import os

        original = _hook_module._build_diff_info_hardened
        try:
            _hook_module._build_diff_info_hardened = None
            info: dict = _hook_module.extract_diff_info(
                "Write",
                {"file_path": "../../../etc/passwd", "content": "x"},
            )
        finally:
            _hook_module._build_diff_info_hardened = original
        self.assertTrue(os.path.isabs(info["file_path"]))
        self.assertTrue(info.get("_path_normalized_external"))


if __name__ == "__main__":
    unittest.main()
