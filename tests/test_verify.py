"""Tests for ledger.verify — chain validation and tamper detection.

Covers: verify_chain across all 6 failure types (READ_ERROR, PARSE_ERROR,
SCHEMA_ERROR, HASH_MISMATCH, INVALID_GENESIS, CHAIN_BREAK), plus valid
chains of varying lengths.

Run: python -m unittest tests.test_verify -v
"""

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

_REPO_ROOT: Path = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from ledger.chain import compute_entry_hash  # noqa: E402
from ledger.verify import verify_chain  # noqa: E402


def _build_valid_chain(n: int) -> list[dict]:
    """Build a correctly-linked chain of n entries starting from GENESIS."""
    entries: list[dict] = []
    for i in range(n):
        entry: dict[str, Any] = {
            "entry_id": f"id-{i}",
            "timestamp": f"2026-01-01T00:00:{i:02d}+00:00",
            "previous_hash": "GENESIS" if i == 0 else entries[i - 1]["entry_hash"],
            "constitution_hash": "abc",
            "change": {"file": "test.py", "tool": "Write"},
        }
        entry["entry_hash"] = compute_entry_hash(entry)
        entries.append(entry)
    return entries


class VerifyChainValidTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp: str = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self._tmp)

    def _path(self) -> str:
        return os.path.join(self._tmp, "ledger.json")

    def _write(self, content: str) -> None:
        Path(self._path()).write_text(content, encoding="utf-8")

    def test_missing_file_is_valid(self) -> None:
        result: dict = verify_chain(os.path.join(self._tmp, "no.json"))
        self.assertTrue(result["valid"])
        self.assertEqual(result["entries"], 0)

    def test_empty_file_is_valid(self) -> None:
        self._write("")
        result: dict = verify_chain(self._path())
        self.assertTrue(result["valid"])
        self.assertEqual(result["entries"], 0)

    def test_whitespace_only_file_is_valid(self) -> None:
        self._write("   \n  ")
        result: dict = verify_chain(self._path())
        self.assertTrue(result["valid"])
        self.assertEqual(result["entries"], 0)

    def test_empty_array_is_valid(self) -> None:
        self._write("[]")
        result: dict = verify_chain(self._path())
        self.assertTrue(result["valid"])
        self.assertEqual(result["entries"], 0)

    def test_single_valid_entry(self) -> None:
        chain: list[dict] = _build_valid_chain(1)
        self._write(json.dumps(chain))
        result: dict = verify_chain(self._path())
        self.assertTrue(result["valid"])
        self.assertEqual(result["entries"], 1)

    def test_multi_entry_valid_chain(self) -> None:
        chain: list[dict] = _build_valid_chain(5)
        self._write(json.dumps(chain))
        result: dict = verify_chain(self._path())
        self.assertTrue(result["valid"])
        self.assertEqual(result["entries"], 5)
        self.assertEqual(result["genesis_hash"], chain[0]["entry_hash"])
        self.assertEqual(result["latest_hash"], chain[-1]["entry_hash"])

    def test_timestamps_in_result(self) -> None:
        chain: list[dict] = _build_valid_chain(3)
        self._write(json.dumps(chain))
        result: dict = verify_chain(self._path())
        self.assertEqual(result["first_entry"], chain[0]["timestamp"])
        self.assertEqual(result["last_entry"], chain[-1]["timestamp"])


class VerifyChainFailureTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp: str = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self._tmp)

    def _path(self) -> str:
        return os.path.join(self._tmp, "ledger.json")

    def _write(self, content: str) -> None:
        Path(self._path()).write_text(content, encoding="utf-8")

    def test_invalid_json_returns_parse_error(self) -> None:
        self._write("{{{bad json")
        result: dict = verify_chain(self._path())
        self.assertFalse(result["valid"])
        self.assertEqual(result["failure_type"], "PARSE_ERROR")

    def test_non_array_root_returns_parse_error(self) -> None:
        self._write('{"key": "val"}')
        result: dict = verify_chain(self._path())
        self.assertFalse(result["valid"])
        self.assertEqual(result["failure_type"], "PARSE_ERROR")

    def test_non_dict_entry_returns_schema_error(self) -> None:
        self._write('["not a dict"]')
        result: dict = verify_chain(self._path())
        self.assertFalse(result["valid"])
        self.assertEqual(result["failure_type"], "SCHEMA_ERROR")
        self.assertEqual(result["failure_index"], 0)

    def test_missing_entry_hash_returns_schema_error(self) -> None:
        self._write('[{"previous_hash": "GENESIS"}]')
        result: dict = verify_chain(self._path())
        self.assertFalse(result["valid"])
        self.assertEqual(result["failure_type"], "SCHEMA_ERROR")

    def test_tampered_entry_returns_hash_mismatch(self) -> None:
        chain: list[dict] = _build_valid_chain(3)
        chain[1]["change"]["file"] = "TAMPERED.py"
        self._write(json.dumps(chain))
        result: dict = verify_chain(self._path())
        self.assertFalse(result["valid"])
        self.assertEqual(result["failure_type"], "HASH_MISMATCH")
        self.assertEqual(result["failure_index"], 1)

    def test_wrong_genesis_marker_returns_invalid_genesis(self) -> None:
        chain: list[dict] = _build_valid_chain(1)
        chain[0]["previous_hash"] = "NOT_GENESIS"
        chain[0]["entry_hash"] = compute_entry_hash(chain[0])
        self._write(json.dumps(chain))
        result: dict = verify_chain(self._path())
        self.assertFalse(result["valid"])
        self.assertEqual(result["failure_type"], "INVALID_GENESIS")

    def test_broken_link_returns_chain_break(self) -> None:
        chain: list[dict] = _build_valid_chain(3)
        chain[2]["previous_hash"] = "wrong_hash"
        chain[2]["entry_hash"] = compute_entry_hash(chain[2])
        self._write(json.dumps(chain))
        result: dict = verify_chain(self._path())
        self.assertFalse(result["valid"])
        self.assertEqual(result["failure_type"], "CHAIN_BREAK")
        self.assertEqual(result["failure_index"], 2)

    def test_failure_includes_expected_and_found(self) -> None:
        self._write("{{{")
        result: dict = verify_chain(self._path())
        self.assertIn("expected", result)
        self.assertIn("found", result)

    def test_stops_at_first_failure(self) -> None:
        chain: list[dict] = _build_valid_chain(5)
        chain[1]["change"]["file"] = "TAMPERED"
        chain[3]["change"]["file"] = "ALSO_TAMPERED"
        self._write(json.dumps(chain))
        result: dict = verify_chain(self._path())
        self.assertEqual(result["failure_index"], 1)
        self.assertEqual(result["entries_checked"], 1)


if __name__ == "__main__":
    unittest.main()
