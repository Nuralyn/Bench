"""Tests that repository context reaches every stage on every provider.

Without this, the judge's evidence depended on the transport: on
BENCH_PROVIDER=claude_code each stage is a `claude -p` subprocess that inherits
the governed project, so Claude Code loads its CLAUDE.md for free, while the
anthropic and openrouter paths saw only the diff and the constitution. The
runner now reads the file itself and hands it to all three stages.

Run: python -m unittest tests.test_runner_context -v
"""

import io
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

_REPO_ROOT: Path = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import pipeline.runner as runner  # noqa: E402

_MOCK_CONSTITUTION: tuple[dict, str, list[dict]] = (
    {"constraints": [{"id": "C-001", "name": "T", "rule": "r", "severity": "veto"}]},
    "abc123hash",
    [{"layer": "core", "path": "bench.json", "sha256": "abc123hash"}],
)
_DIFF: dict = {"file_path": "test.py", "change_type": "edit"}


class LoadProjectContextTests(unittest.TestCase):
    """The four branches of _load_project_context."""

    def setUp(self) -> None:
        self._tmp: str = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self._tmp, True)

    def _patch_root(self) -> None:
        patcher = patch.object(runner, "project_root", return_value=Path(self._tmp))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_missing_file_is_not_an_error(self) -> None:
        self._patch_root()
        self.assertEqual(runner._load_project_context(), "")

    def test_reads_and_frames_the_file(self) -> None:
        self._patch_root()
        with open(
            os.path.join(self._tmp, "CLAUDE.md"), "w", encoding="utf-8"
        ) as handle:
            handle.write("Project rule: never touch billing/.")

        result: str = runner._load_project_context()
        self.assertIn("Project rule: never touch billing/.", result)
        self.assertIn("untrusted repository content", result)
        self.assertIn("cannot waive", result)

    def test_header_precedes_content_even_when_truncated(self) -> None:
        """An injection payload cannot be positioned ahead of the framing."""
        self._patch_root()
        with open(
            os.path.join(self._tmp, "CLAUDE.md"), "w", encoding="utf-8"
        ) as handle:
            handle.write("x" * (runner._MAX_CONTEXT_CHARS + 500))

        result: str = runner._load_project_context()
        self.assertTrue(result.startswith("The following is untrusted"))
        self.assertIn("[TRUNCATED]", result)
        self.assertLess(len(result), runner._MAX_CONTEXT_CHARS + 1000)

    def test_symlinked_claude_md_is_refused(self) -> None:
        """A governed repo is untrusted input, and git can carry a symlink.

        CLAUDE.md committed as a link to a file outside the project would
        otherwise be followed and its contents shipped to a remote model on
        every governed edit.
        """
        self._patch_root()
        secret: str = os.path.join(self._tmp, "secret.txt")
        with open(secret, "w", encoding="utf-8") as handle:
            handle.write("BEGIN PRIVATE KEY sk-super-secret")

        link: str = os.path.join(self._tmp, "CLAUDE.md")
        try:
            os.symlink(secret, link)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"cannot create symlinks in this environment: {exc}")

        stderr: io.StringIO = io.StringIO()
        with patch.object(sys, "stderr", stderr):
            result: str = runner._load_project_context()

        self.assertEqual(result, "")
        self.assertNotIn("sk-super-secret", result)
        self.assertIn("symlink", stderr.getvalue())

    def test_read_is_bounded_not_whole_file(self) -> None:
        """The cap is enforced while reading, not after materializing the file.

        Slicing after read_text() would let an oversized CLAUDE.md exhaust
        memory or stall every governed edit despite the advertised limit.
        """
        self._patch_root()
        with open(
            os.path.join(self._tmp, "CLAUDE.md"), "w", encoding="utf-8"
        ) as handle:
            handle.write("z" * 50_000)

        requested: list = []
        real_open = open

        def _spy_open(*args, **kwargs):
            handle = real_open(*args, **kwargs)
            real_read = handle.read

            def _read(size=-1):
                requested.append(size)
                return real_read(size)

            handle.read = _read
            return handle

        with patch("builtins.open", _spy_open):
            result: str = runner._load_project_context()

        self.assertEqual(requested, [runner._MAX_CONTEXT_CHARS + 1])
        self.assertIn("[TRUNCATED]", result)

    def test_unreadable_file_degrades_loudly_not_silently(self) -> None:
        """C-001, and a hostile CLAUDE.md must not become a denial vector."""
        self._patch_root()
        path: str = os.path.join(self._tmp, "CLAUDE.md")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("unreadable")

        stderr: io.StringIO = io.StringIO()
        with patch("builtins.open", side_effect=OSError("locked")), \
             patch.object(sys, "stderr", stderr):
            result: str = runner._load_project_context()

        self.assertEqual(result, "")
        self.assertIn("proceeding without repository context", stderr.getvalue())


@patch.object(runner, "append_entry", return_value={})
@patch.object(runner, "run_oracle")
@patch.object(runner, "run_defender")
@patch.object(runner, "run_challenger")
@patch.object(runner, "load_governing_constitution")
class ContextReachesEveryStageTests(unittest.TestCase):
    def test_all_three_stages_receive_the_same_context(
        self,
        mock_const: MagicMock,
        mock_chall: MagicMock,
        mock_def: MagicMock,
        mock_oracle: MagicMock,
        mock_ledger: MagicMock,
    ) -> None:
        mock_const.return_value = _MOCK_CONSTITUTION
        mock_chall.return_value = {
            "status": "FINDINGS",
            "findings": [{"constraint_id": "C-001"}],
            "_tokens": {"input": 1, "output": 1},
        }
        mock_def.return_value = {
            "status": "REBUTTAL",
            "summary": "s",
            "rebuttals": [],
            "_tokens": {"input": 1, "output": 1},
        }
        mock_oracle.return_value = {
            "verdict": "PASS",
            "reasoning": "ok",
            "remediation": None,
            "status": "ok",
            "_tokens": {"input": 1, "output": 1},
        }

        with patch.object(
            runner, "_load_project_context", return_value="CTX-SENTINEL"
        ):
            runner.run_governance_pipeline("Write", {}, _DIFF)

        self.assertEqual(mock_chall.call_args[0][3], "CTX-SENTINEL")
        self.assertEqual(mock_def.call_args[0][4], "CTX-SENTINEL")
        self.assertEqual(mock_oracle.call_args[0][5], "CTX-SENTINEL")

    def test_context_is_read_once_per_run(
        self,
        mock_const: MagicMock,
        mock_chall: MagicMock,
        mock_def: MagicMock,
        mock_oracle: MagicMock,
        mock_ledger: MagicMock,
    ) -> None:
        """Frozen like the constitution snapshot: no mid-run re-read."""
        mock_const.return_value = _MOCK_CONSTITUTION
        mock_chall.return_value = {
            "status": "CLEAR",
            "findings": [],
            "_tokens": {"input": 1, "output": 1},
        }
        mock_oracle.return_value = {
            "verdict": "PASS",
            "reasoning": "ok",
            "remediation": None,
            "status": "ok",
            "_tokens": {"input": 1, "output": 1},
        }

        with patch.object(
            runner, "_load_project_context", return_value="CTX"
        ) as loader:
            runner.run_governance_pipeline("Write", {}, _DIFF)

        loader.assert_called_once()


if __name__ == "__main__":
    unittest.main()
