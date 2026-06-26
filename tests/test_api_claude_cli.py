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
    def test_sets_subprocess_env_and_disallowed_tools(self, _which, run) -> None:
        run.return_value = _completed(stdout=_OK_ENVELOPE)
        _claude_cli_call("claude-opus-4-7", "sys", _msgs(), 4096)
        args, kwargs = run.call_args
        cmd = args[0] if args else kwargs["args"]
        self.assertIn("--disallowedTools", cmd)
        self.assertIn("Write", cmd)
        self.assertIn("Edit", cmd)
        self.assertIn("--model", cmd)
        self.assertIn("claude-opus-4-7", cmd)
        self.assertEqual(kwargs["env"].get("BENCH_SUBPROCESS"), "1")
        self.assertFalse(kwargs.get("shell", False))

    @mock.patch("utils.api.subprocess.run")
    @mock.patch("utils.api.shutil.which", return_value="/usr/bin/claude")
    def test_multi_turn_flattening(self, _which, run) -> None:
        run.return_value = _completed(stdout=_OK_ENVELOPE)
        msgs = [
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "bad output"},
            {"role": "user", "content": "respond with JSON only"},
        ]
        _claude_cli_call("claude-sonnet-4-6", "sys", msgs, 4096)
        _args, kwargs = run.call_args
        sent = kwargs["input"]
        self.assertIn("ASSISTANT: bad output", sent)
        self.assertIn("USER: respond with JSON only", sent)


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
