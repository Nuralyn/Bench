"""Tests for utils.viewer: self-contained HTML ledger viewer.

Smoke-level coverage of generate_viewer_html against synthetic ledgers
on disk: document structure, stats banner values, chain status labels,
JSON embedding safety, and the never-raises error page fallback.

Run: python -m unittest discover -s tests -p test_viewer.py -v
"""

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

_REPO_ROOT: Path = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from ledger.chain import compute_entry_hash  # noqa: E402
from utils.viewer import generate_viewer_html  # noqa: E402


def _build_valid_chain(n: int, verdicts: list[str] | None = None) -> list[dict]:
    """Build a correctly-linked chain of n entries with oracle verdicts."""
    entries: list[dict] = []
    for i in range(n):
        verdict: str = verdicts[i] if verdicts else "PASS"
        entry: dict[str, Any] = {
            "entry_id": f"id-{i}",
            "timestamp": f"2026-01-01T00:00:{i:02d}+00:00",
            "previous_hash": "GENESIS" if i == 0 else entries[i - 1]["entry_hash"],
            "constitution_hash": "abc",
            "change": {"file": f"file_{i}.py", "tool": "Write"},
            "oracle": {
                "verdict": verdict,
                "constraint_citations": (
                    [{"constraint_id": "C-001", "disposition": "VIOLATED"}]
                    if verdict == "VETO"
                    else []
                ),
            },
        }
        entry["entry_hash"] = compute_entry_hash(entry)
        entries.append(entry)
    return entries


class GenerateViewerHtmlTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp: str = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self._tmp)

    def _path(self) -> str:
        return os.path.join(self._tmp, "ledger.json")

    def _write_chain(self, chain: list[dict]) -> None:
        Path(self._path()).write_text(json.dumps(chain), encoding="utf-8")

    def test_missing_ledger_renders_empty_viewer(self) -> None:
        html_out: str = generate_viewer_html(self._path())
        self.assertIn("<!doctype html>", html_out)
        self.assertIn("Bench Verdict Viewer", html_out)
        self.assertIn("EMPTY", html_out)
        self.assertIn("const LEDGER_DATA = [];", html_out)

    def test_valid_ledger_renders_stats_and_chain_status(self) -> None:
        chain: list[dict] = _build_valid_chain(
            3, verdicts=["PASS", "VETO", "PASS"]
        )
        self._write_chain(chain)
        html_out: str = generate_viewer_html(self._path())
        self.assertIn(">VALID<", html_out)
        self.assertIn("2 <span class=\"pct\">(66.7%)</span>", html_out)
        self.assertIn("1 <span class=\"pct\">(33.3%)</span>", html_out)
        self.assertIn("C-001 (1 veto(es))", html_out)
        self.assertIn("file_1.py", html_out)

    def test_tampered_ledger_reports_broken_chain(self) -> None:
        chain: list[dict] = _build_valid_chain(3)
        chain[1]["change"]["file"] = "TAMPERED.py"
        self._write_chain(chain)
        html_out: str = generate_viewer_html(self._path())
        self.assertIn("BROKEN AT ENTRY #2", html_out)

    def test_script_close_tags_in_data_are_escaped(self) -> None:
        chain: list[dict] = _build_valid_chain(1)
        chain[0]["change"]["file"] = "evil</script><script>alert(1)"
        chain[0]["entry_hash"] = compute_entry_hash(chain[0])
        self._write_chain(chain)
        html_out: str = generate_viewer_html(self._path())
        self.assertNotIn("evil</script>", html_out)
        self.assertIn("evil<\\/script>", html_out)

    def test_generation_failure_returns_error_page(self) -> None:
        with patch(
            "utils.viewer.load_ledger", side_effect=RuntimeError("boom")
        ):
            html_out: str = generate_viewer_html(self._path())
        self.assertIn("generation failed", html_out)
        self.assertIn("RuntimeError: boom", html_out)


if __name__ == "__main__":
    unittest.main()
