"""The hook as Claude Code runs it: a real process, real bytes, exact JSON.

Every other hook test patches stdin and captures stdout inside the test
process. The one interface Claude Code actually consumes is a child process
that reads a JSON payload from a pipe and writes exactly one JSON line to
stdout, with exit code zero, always. These tests exercise that interface for
a PASS, a VETO, malformed input, a pipeline that raises, and a pipeline that
fails to import.

The governance pipeline is replaced through ``sitecustomize``: the child gets
a PYTHONPATH entry holding a sitecustomize.py that pre-installs a fake
``pipeline.runner`` in ``sys.modules`` before the hook runs, or poisons it
for the import-failure case. Nothing in the hook is patched; it imports the
name it always imports and finds what the interpreter already holds. The fake
records what it was handed, so the test can also prove the payload crossed
the pipe intact, non-ASCII included. It never touches a ledger.

Run: python -m unittest tests.test_hook_subprocess -v
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

_REPO_ROOT: Path = Path(__file__).resolve().parent.parent
_HOOK: Path = _REPO_ROOT / "hooks" / "pre-tool-use.py"

# Installed into the child's sys.modules before the hook's first import.
_SITECUSTOMIZE: str = textwrap.dedent(
    """
    import json
    import os
    import sys
    import types

    mode = os.environ.get("BENCH_TEST_RUNNER_MODE", "")
    if mode == "import-failure":
        # None in sys.modules makes `from pipeline.runner import ...` raise
        # ModuleNotFoundError, which is the failure the hook must fail closed on.
        sys.modules["pipeline.runner"] = None
    elif mode:
        runner = types.ModuleType("pipeline.runner")

        def run_governance_pipeline(tool_name, tool_input, diff_info):
            capture = os.environ.get("BENCH_TEST_CAPTURE")
            if capture:
                with open(capture, "w", encoding="utf-8") as handle:
                    json.dump(
                        {
                            "tool_name": tool_name,
                            "tool_input": tool_input,
                            "diff_info": diff_info,
                        },
                        handle,
                    )
            if mode == "raise":
                raise RuntimeError("pipeline exploded")
            return json.loads(os.environ["BENCH_TEST_VERDICT"])

        runner.run_governance_pipeline = run_governance_pipeline
        sys.modules["pipeline.runner"] = runner
    """
)

_PASS_RESPONSE: dict = {
    "hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "allow",
        "additionalContext": "Bench governance: PASS. All constraints satisfied.",
    }
}

_IMPORT_FAILURE_RESPONSE: dict = {
    "hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": (
            "BENCH VETO: Governance pipeline is unavailable (import failed); "
            "cannot adjudicate. Failing closed."
        ),
        "additionalContext": (
            "Remediation: The pipeline failed to import (see stderr). Fix the "
            "import error, then retry. Changes are blocked until governance "
            "can run. Emergency recovery is a human editing files directly, "
            "outside Claude Code's governed tools."
        ),
    }
}

_HOOK_ERROR_RESPONSE: dict = {
    "hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": (
            "BENCH VETO: governance hook error; the change could not be "
            "adjudicated. Failing closed."
        ),
        "additionalContext": (
            "Remediation: the hook raised an unexpected error (see stderr). "
            "Fix it, then retry. Emergency recovery is a human editing files "
            "directly, outside Claude Code's governed tools."
        ),
    }
}


def _payload(content: str = "x = 1") -> bytes:
    return json.dumps(
        {
            "tool_name": "Write",
            "tool_input": {"file_path": "utils/api.py", "content": content},
        },
        ensure_ascii=False,
    ).encode("utf-8")


class HookSubprocessTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp: str = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self._tmp, True)
        (Path(self._tmp) / "sitecustomize.py").write_text(
            _SITECUSTOMIZE, encoding="utf-8"
        )
        self._capture: Path = Path(self._tmp) / "captured.json"

    def _run(
        self, stdin: bytes, *, mode: str, verdict: dict | None = None
    ) -> tuple[int, list[str], str]:
        """Spawn the hook on real bytes; return (exit code, stdout lines, stderr).

        The child's environment carries only what the test intends. Any
        BENCH_SUBPROCESS from the surrounding session is dropped so the
        reentrancy guard cannot short-circuit the run, and the provider is
        pointed at nothing so that, if the fake failed to install, a real
        pipeline would fail closed fast instead of calling a model.
        """
        env: dict[str, str] = {
            key: value
            for key, value in os.environ.items()
            if key not in ("BENCH_SUBPROCESS", "PYTHONPATH")
            and not key.startswith("BENCH_TEST_")
        }
        env["PYTHONPATH"] = self._tmp
        env["BENCH_PROVIDER"] = "nonexistent"
        env["BENCH_TEST_RUNNER_MODE"] = mode
        env["BENCH_TEST_CAPTURE"] = str(self._capture)
        if verdict is not None:
            env["BENCH_TEST_VERDICT"] = json.dumps(verdict)
        proc = subprocess.run(
            [sys.executable, str(_HOOK)],
            input=stdin,
            capture_output=True,
            timeout=120,
            env=env,
            cwd=str(_REPO_ROOT),
        )
        stdout_lines: list[str] = [
            line for line in proc.stdout.decode("utf-8").splitlines() if line
        ]
        return proc.returncode, stdout_lines, proc.stderr.decode("utf-8", "replace")

    def _captured(self) -> dict:
        self.assertTrue(
            self._capture.is_file(),
            "the fake pipeline never ran: sitecustomize did not install it",
        )
        data: dict = json.loads(self._capture.read_text(encoding="utf-8"))
        return data

    # --- the happy path, and the payload crossing the pipe intact -----------

    def test_pass_is_exit_zero_and_the_exact_allow_payload(self) -> None:
        code, lines, _ = self._run(
            _payload(), mode="verdict", verdict={"verdict": "PASS"}
        )
        self.assertEqual(code, 0)
        self.assertEqual(len(lines), 1, "stdout must carry exactly one JSON line")
        self.assertEqual(json.loads(lines[0]), _PASS_RESPONSE)

    def test_non_ascii_content_reaches_the_pipeline_intact(self) -> None:
        """Claude Code pipes UTF-8; a Windows child decodes stdin as cp1252
        unless the hook reconfigures it, and a mangled diff would be what
        the pipeline adjudicated and the ledger recorded."""
        content: str = "greeting = 'héllo, wörld: 日本語 ✓'"
        code, lines, _ = self._run(
            _payload(content), mode="verdict", verdict={"verdict": "PASS"}
        )
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(lines[0]), _PASS_RESPONSE)
        seen: dict = self._captured()
        self.assertEqual(seen["tool_name"], "Write")
        self.assertEqual(seen["tool_input"]["content"], content)
        self.assertEqual(seen["diff_info"]["file_path"], os.path.join("utils", "api.py"))

    def test_advisories_ride_along_on_a_pass(self) -> None:
        code, lines, _ = self._run(
            _payload(),
            mode="verdict",
            verdict={"verdict": "PASS", "advisories": ["Consider a test.", ""]},
        )
        self.assertEqual(code, 0)
        self.assertEqual(
            json.loads(lines[0])["hookSpecificOutput"]["additionalContext"],
            "Bench governance: PASS. All constraints satisfied. Advisories: "
            "Consider a test.",
        )

    # --- deny paths, each still exit zero -----------------------------------

    def test_veto_is_exit_zero_and_the_exact_deny_payload(self) -> None:
        code, lines, _ = self._run(
            _payload(),
            mode="verdict",
            verdict={
                "verdict": "VETO",
                "reason": "Silent except swallows the error.",
                "violated_constraints": ["C-001"],
                "remediation": "Log or re-raise in the except block.",
            },
        )
        self.assertEqual(code, 0)
        self.assertEqual(len(lines), 1)
        self.assertEqual(
            json.loads(lines[0]),
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": (
                        "BENCH VETO [C-001]: Silent except swallows the error."
                    ),
                    "additionalContext": (
                        "Remediation: Log or re-raise in the except block."
                    ),
                }
            },
        )

    def test_malformed_input_is_exit_zero_and_a_deny(self) -> None:
        code, lines, err = self._run(b"{{{not json", mode="verdict")
        self.assertEqual(code, 0)
        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0]), _HOOK_ERROR_RESPONSE)
        self.assertIn("failing closed", err)
        self.assertFalse(self._capture.exists(), "nothing was adjudicated")

    def test_undecodable_bytes_are_exit_zero_and_a_deny(self) -> None:
        """Strict UTF-8 on purpose: bytes Bench cannot read faithfully must
        not be adjudicated."""
        code, lines, _ = self._run(b'{"tool_name": "Write\xff"}', mode="verdict")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(lines[0]), _HOOK_ERROR_RESPONSE)
        self.assertFalse(self._capture.exists())

    def test_a_pipeline_that_raises_is_exit_zero_and_a_deny(self) -> None:
        code, lines, err = self._run(_payload(), mode="raise")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(lines[0]), _HOOK_ERROR_RESPONSE)
        self.assertIn("RuntimeError: pipeline exploded", err)
        self._captured()  # the fake ran and then raised

    def test_import_failure_is_exit_zero_and_the_exact_deny_payload(self) -> None:
        code, lines, err = self._run(_payload(), mode="import-failure")
        self.assertEqual(code, 0)
        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0]), _IMPORT_FAILURE_RESPONSE)
        self.assertIn("pipeline import failed", err)

    # --- the contract itself ------------------------------------------------

    def test_stdout_carries_nothing_but_the_response(self) -> None:
        """Claude Code parses stdout as the hook's answer. Every diagnostic
        the hook prints goes to stderr, and the payload-validation warnings
        this input provokes are the proof."""
        odd: bytes = json.dumps(
            {"tool_name": "Write", "tool_input": {"file_path": 7}}
        ).encode("utf-8")
        code, lines, err = self._run(
            odd, mode="verdict", verdict={"verdict": "PASS"}
        )
        self.assertEqual(code, 0)
        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0]), _PASS_RESPONSE)
        self.assertIn("payload validation", err)


if __name__ == "__main__":
    unittest.main()
