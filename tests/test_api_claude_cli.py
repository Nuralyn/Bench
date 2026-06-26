"""Tests for the claude_code (`claude -p`) provider in utils.api.

Run: python -m unittest tests.test_api_claude_cli -v
"""

import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

_REPO_ROOT: Path = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from utils.api import (  # noqa: E402
    _ProviderError,
    _claude_cli_call,
    call_model,
)


def _completed(
    stdout: str = "", stderr: str = "", returncode: int = 0
) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=["claude"], returncode=returncode, stdout=stdout, stderr=stderr
    )


_OK_ENVELOPE: str = json.dumps(
    {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": '{"verdict": "PASS"}',
        "usage": {"input_tokens": 12, "output_tokens": 5},
    }
)


def _msgs() -> list[dict[str, str]]:
    return [{"role": "user", "content": "review this diff"}]


class ClaudeCliCallTests(unittest.TestCase):
    @mock.patch("utils.api.subprocess.run")
    @mock.patch("utils.api.shutil.which", return_value="/usr/bin/claude")
    def test_success_returns_text_and_tokens(self, _which, run) -> None:
        run.return_value = _completed(stdout=_OK_ENVELOPE)
        text, in_tok, out_tok = _claude_cli_call(
            "claude-sonnet-4-6", "sys", _msgs(), 4096
        )
        self.assertEqual(text, '{"verdict": "PASS"}')
        self.assertEqual(in_tok, 12)
        self.assertEqual(out_tok, 5)

    @mock.patch("utils.api.shutil.which", return_value=None)
    def test_binary_missing_raises(self, _which) -> None:
        with self.assertRaises(_ProviderError):
            _claude_cli_call("claude-sonnet-4-6", "sys", _msgs(), 4096)

    @mock.patch("utils.api.subprocess.run")
    @mock.patch("utils.api.shutil.which", return_value="/usr/bin/claude")
    def test_nonzero_exit_raises_and_sanitizes(self, _which, run) -> None:
        run.return_value = _completed(
            stderr="boom sk-ant-secret1234567890", returncode=1
        )
        with self.assertRaises(_ProviderError) as ctx:
            _claude_cli_call("claude-sonnet-4-6", "sys", _msgs(), 4096)
        self.assertNotIn("sk-ant-secret1234567890", str(ctx.exception))
        self.assertIn("[REDACTED]", str(ctx.exception))

    @mock.patch(
        "utils.api.subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd="claude", timeout=120),
    )
    @mock.patch("utils.api.shutil.which", return_value="/usr/bin/claude")
    def test_timeout_raises(self, _which, _run) -> None:
        with self.assertRaises(_ProviderError) as ctx:
            _claude_cli_call("claude-sonnet-4-6", "sys", _msgs(), 4096)
        self.assertIn("timed out", str(ctx.exception))

    @mock.patch("utils.api.subprocess.run")
    @mock.patch("utils.api.shutil.which", return_value="/usr/bin/claude")
    def test_malformed_json_raises(self, _which, run) -> None:
        run.return_value = _completed(stdout="not json at all")
        with self.assertRaises(_ProviderError):
            _claude_cli_call("claude-sonnet-4-6", "sys", _msgs(), 4096)

    @mock.patch("utils.api.subprocess.run")
    @mock.patch("utils.api.shutil.which", return_value="/usr/bin/claude")
    def test_envelope_is_error_raises(self, _which, run) -> None:
        run.return_value = _completed(
            stdout=json.dumps(
                {
                    "subtype": "error_during_execution",
                    "is_error": True,
                    "result": "nope",
                }
            )
        )
        with self.assertRaises(_ProviderError):
            _claude_cli_call("claude-sonnet-4-6", "sys", _msgs(), 4096)

    @mock.patch("utils.api.subprocess.run")
    @mock.patch("utils.api.shutil.which", return_value="/usr/bin/claude")
    def test_envelope_not_object_raises(self, _which, run) -> None:
        run.return_value = _completed(stdout="[1, 2, 3]")
        with self.assertRaises(_ProviderError):
            _claude_cli_call("claude-sonnet-4-6", "sys", _msgs(), 4096)

    @mock.patch("utils.api.subprocess.run")
    @mock.patch("utils.api.shutil.which", return_value="/usr/bin/claude")
    def test_sets_subprocess_env_and_no_tools(self, _which, run) -> None:
        run.return_value = _completed(stdout=_OK_ENVELOPE)
        _claude_cli_call("claude-opus-4-7", "SYSPROMPT_SENTINEL", _msgs(), 4096)
        args, kwargs = run.call_args
        cmd = args[0] if args else kwargs["args"]
        # Security: the judge gets NO tools (--tools ""), not just a Write/Edit
        # deny list, since the child bypasses Bench's own hook.
        self.assertEqual(cmd[cmd.index("--tools") + 1], "")
        self.assertIn("--strict-mcp-config", cmd)
        self.assertNotIn("--disallowedTools", cmd)
        self.assertIn("--model", cmd)
        self.assertIn("claude-opus-4-7", cmd)
        self.assertEqual(kwargs["env"].get("BENCH_SUBPROCESS"), "1")
        self.assertFalse(kwargs.get("shell", False))
        self.assertEqual(kwargs.get("encoding"), "utf-8")
        # System prompt goes to --system-prompt-file, never onto the stdin
        # payload or the argv (which would lose system priority / get truncated).
        self.assertIn("--system-prompt-file", cmd)
        self.assertNotIn("SYSPROMPT_SENTINEL", kwargs["input"])
        self.assertNotIn("SYSPROMPT_SENTINEL", cmd)

    @mock.patch("utils.api.subprocess.run")
    @mock.patch("utils.api.shutil.which", return_value="/usr/bin/claude")
    def test_multi_turn_flattening(self, _which, run) -> None:
        run.return_value = _completed(stdout=_OK_ENVELOPE)
        msgs = [
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "bad output"},
            {"role": "user", "content": "respond with JSON only"},
        ]
        _claude_cli_call("claude-sonnet-4-6", "SYS_SENTINEL", msgs, 4096)
        _args, kwargs = run.call_args
        sent = kwargs["input"]
        self.assertIn("ASSISTANT: bad output", sent)
        self.assertIn("USER: respond with JSON only", sent)
        # The system prompt must not leak onto stdin in the multi-turn path
        # either — it goes to --system-prompt-file.
        self.assertNotIn("SYS_SENTINEL", sent)

    @mock.patch("utils.api.shutil.which", return_value="/usr/bin/claude")
    def test_system_prompt_written_to_file_not_stdin(self, _which) -> None:
        captured: dict[str, str] = {}

        def fake_run(cmd, **kwargs):
            # Read the system-prompt-file content while it still exists, before
            # _claude_cli_call removes it in its finally block.
            path = cmd[cmd.index("--system-prompt-file") + 1]
            captured["file"] = Path(path).read_text(encoding="utf-8")
            captured["input"] = kwargs.get("input", "")
            captured["path"] = path
            return _completed(stdout=_OK_ENVELOPE)

        with mock.patch("utils.api.subprocess.run", side_effect=fake_run):
            _claude_cli_call("claude-sonnet-4-6", "STRICT_JUDGE_RULES", _msgs(), 4096)

        # System prompt reaches the file (system priority), not the stdin payload.
        self.assertEqual(captured["file"], "STRICT_JUDGE_RULES")
        self.assertNotIn("STRICT_JUDGE_RULES", captured["input"])
        # The temp file is cleaned up after the call.
        self.assertFalse(Path(captured["path"]).exists())

    @mock.patch("utils.api.shutil.which", return_value="/usr/bin/claude")
    def test_temp_file_write_failure_raises_provider_error(self, _which) -> None:
        # If the system-prompt temp file cannot be written, the helper must
        # raise the typed _ProviderError, never a bare OSError that would break
        # call_model's never-raises contract.
        with mock.patch(
            "utils.api.tempfile.NamedTemporaryFile",
            side_effect=OSError("disk full"),
        ):
            with self.assertRaises(_ProviderError):
                _claude_cli_call("claude-sonnet-4-6", "sys", _msgs(), 4096)

    @mock.patch("utils.api.subprocess.run")
    @mock.patch("utils.api.shutil.which", return_value="/usr/bin/claude")
    def test_timeout_env_valid_is_passed(self, _which, run) -> None:
        run.return_value = _completed(stdout=_OK_ENVELOPE)
        with mock.patch.dict("os.environ", {"BENCH_CLAUDE_TIMEOUT": "5"}):
            _claude_cli_call("claude-sonnet-4-6", "sys", _msgs(), 4096)
        _args, kwargs = run.call_args
        self.assertEqual(kwargs["timeout"], 5.0)

    @mock.patch("utils.api.subprocess.run")
    @mock.patch("utils.api.shutil.which", return_value="/usr/bin/claude")
    def test_timeout_env_invalid_falls_back(self, _which, run) -> None:
        run.return_value = _completed(stdout=_OK_ENVELOPE)
        with mock.patch.dict("os.environ", {"BENCH_CLAUDE_TIMEOUT": "abc"}):
            _claude_cli_call("claude-sonnet-4-6", "sys", _msgs(), 4096)
        _args, kwargs = run.call_args
        self.assertEqual(kwargs["timeout"], 120.0)

    @mock.patch("utils.api.subprocess.run")
    @mock.patch("utils.api.shutil.which", return_value="/usr/bin/claude")
    def test_timeout_env_nonpositive_falls_back(self, _which, run) -> None:
        run.return_value = _completed(stdout=_OK_ENVELOPE)
        for bad in ("0", "-30"):
            with mock.patch.dict("os.environ", {"BENCH_CLAUDE_TIMEOUT": bad}):
                _claude_cli_call("claude-sonnet-4-6", "sys", _msgs(), 4096)
            _args, kwargs = run.call_args
            self.assertEqual(kwargs["timeout"], 120.0)

    @mock.patch("utils.api.subprocess.run")
    @mock.patch("utils.api.shutil.which", return_value="/usr/bin/claude")
    def test_subtype_error_without_is_error_raises(self, _which, run) -> None:
        run.return_value = _completed(
            stdout=json.dumps({"subtype": "error_max_turns", "result": "partial"})
        )
        with self.assertRaises(_ProviderError):
            _claude_cli_call("claude-sonnet-4-6", "sys", _msgs(), 4096)

    @mock.patch("utils.api.subprocess.run")
    @mock.patch("utils.api.shutil.which", return_value="/usr/bin/claude")
    def test_sums_cache_tokens_into_input(self, _which, run) -> None:
        run.return_value = _completed(
            stdout=json.dumps(
                {
                    "subtype": "success",
                    "is_error": False,
                    "result": "{}",
                    "usage": {
                        "input_tokens": 2,
                        "cache_creation_input_tokens": 100,
                        "cache_read_input_tokens": 50,
                        "output_tokens": 7,
                    },
                }
            )
        )
        _text, in_tok, out_tok = _claude_cli_call(
            "claude-sonnet-4-6", "sys", _msgs(), 4096
        )
        self.assertEqual(in_tok, 152)
        self.assertEqual(out_tok, 7)

    @mock.patch("utils.api.subprocess.run")
    @mock.patch("utils.api.shutil.which", return_value="/usr/bin/claude")
    def test_malformed_usage_does_not_raise(self, _which, run) -> None:
        run.return_value = _completed(
            stdout=json.dumps(
                {
                    "subtype": "success",
                    "is_error": False,
                    "result": "{}",
                    "usage": {"input_tokens": "oops", "output_tokens": None},
                }
            )
        )
        text, in_tok, out_tok = _claude_cli_call(
            "claude-sonnet-4-6", "sys", _msgs(), 4096
        )
        self.assertEqual((in_tok, out_tok), (0, 0))
        self.assertEqual(text, "{}")


class CallModelClaudeCliIntegrationTests(unittest.TestCase):
    @mock.patch("utils.api.subprocess.run")
    @mock.patch("utils.api.shutil.which", return_value="/usr/bin/claude")
    def test_call_model_routes_to_cli_and_appends_tokens(
        self, _which, run
    ) -> None:
        run.return_value = _completed(stdout=_OK_ENVELOPE)
        with mock.patch.dict("os.environ", {"BENCH_PROVIDER": "claude_code"}):
            result = call_model("claude-sonnet-4-6", "sys", "content")
        self.assertEqual(result.get("verdict"), "PASS")
        self.assertEqual(result["_tokens"], {"input": 12, "output": 5})


if __name__ == "__main__":
    unittest.main()
