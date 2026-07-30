"""Tests that the hook decodes its stdin payload as UTF-8, not the locale codec.

Claude Code pipes the hook payload as UTF-8. On Windows sys.stdin defaults to
cp1252, which silently mangles every non-ASCII character in a governed diff
before the Challenger sees it — so the pipeline adjudicates corrupted text and
the ledger records that corruption as the receipt.

The hook module uses a hyphen in its filename, so it is imported via importlib
(same pattern as test_hook.py). Pipeline execution is mocked to prevent real
API calls.

Run: python -m unittest tests.test_hook_encoding -v
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

main = _hook_module.main

# An em dash: 3 bytes in UTF-8, which cp1252 would decode as three characters.
_EM_DASH: str = "—"
_MOJIBAKE: str = _EM_DASH.encode("utf-8").decode("cp1252")


def _cp1252_stdin(payload: str) -> io.TextIOWrapper:
    """A text stdin carrying UTF-8 bytes but defaulting to the cp1252 codec.

    This reproduces the Windows console the hook actually runs under.
    """
    return io.TextIOWrapper(
        io.BytesIO(payload.encode("utf-8")), encoding="cp1252"
    )


class StdinEncodingTests(unittest.TestCase):
    def _run(self, stdin_obj: object) -> tuple[int, str, MagicMock]:
        mock_stdout: io.StringIO = io.StringIO()
        with patch.object(_hook_module, "run_governance_pipeline") as pipeline:
            pipeline.return_value = {"verdict": "PASS"}
            with patch.object(sys, "stdin", stdin_obj), \
                 patch.object(sys, "stdout", mock_stdout):
                code: int = main()
        return code, mock_stdout.getvalue(), pipeline

    def test_utf8_survives_a_cp1252_stdin(self) -> None:
        """The em dash must reach the pipeline intact, not as mojibake.

        ensure_ascii=False is load-bearing. Python's json.dumps escapes
        non-ASCII to \\uXXXX by default, which makes the payload pure ASCII and
        the decoding bug unreachable — this test passes with or without the fix
        if you leave the default on. Node's JSON.stringify, which is what
        actually feeds this hook, emits raw UTF-8. Send raw UTF-8 here so the
        test genuinely fails against the locale-decoded stdin it guards.
        """
        payload: str = json.dumps({
            "tool_name": "Write",
            "tool_input": {
                "file_path": "test.py",
                "content": f"# fails closed {_EM_DASH} blocking every edit",
            },
        }, ensure_ascii=False)
        code, _, pipeline = self._run(_cp1252_stdin(payload))

        self.assertEqual(code, 0)
        pipeline.assert_called_once()
        tool_input: dict = pipeline.call_args[0][1]
        content: str = tool_input["content"]
        self.assertIn(_EM_DASH, content)
        self.assertNotIn(_MOJIBAKE, content)

    def test_stringio_stdin_still_works(self) -> None:
        """The no-op fallback: io.StringIO has no reconfigure and is already text."""
        payload: str = json.dumps({
            "tool_name": "Write",
            "tool_input": {
                "file_path": "test.py",
                "content": f"plain {_EM_DASH} text",
            },
        })
        code, _, pipeline = self._run(io.StringIO(payload))

        self.assertEqual(code, 0)
        pipeline.assert_called_once()
        self.assertIn(_EM_DASH, pipeline.call_args[0][1]["content"])

    def test_undecodable_bytes_fail_closed(self) -> None:
        """Strict decoding: a payload Bench cannot read is blocked, not adjudicated."""
        bad: io.TextIOWrapper = io.TextIOWrapper(
            io.BytesIO(b'{"tool_name": "Write", "tool_input": {"content": "\xff\xfe"}}'),
            encoding="cp1252",
        )
        code, output, pipeline = self._run(bad)

        self.assertEqual(code, 0)
        pipeline.assert_not_called()
        resp: dict = json.loads(output)
        self.assertEqual(
            resp["hookSpecificOutput"]["permissionDecision"], "deny"
        )


if __name__ == "__main__":
    unittest.main()
