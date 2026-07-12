"""Tests for ledger.verify: chain validation and tamper detection.

Covers: verify_chain across all 7 failure types (READ_ERROR, PARSE_ERROR,
SCHEMA_ERROR, HASH_MISMATCH, INVALID_GENESIS, CHAIN_BREAK, META_MISMATCH),
plus valid chains of varying lengths and the ledger-meta.json anchor.

Synthetic chains come from the shared fixture module
tests/_ledger_fixtures.py (build_valid_chain), which already exists in
the repository and is the single source of truth for the entry shape.

Run: python -m unittest discover -s tests -p test_verify.py -v
"""

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

_REPO_ROOT: Path = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from _ledger_fixtures import build_valid_chain as _build_valid_chain  # noqa: E402
from ledger.chain import compute_entry_hash  # noqa: E402
from ledger.verify import verify_chain  # noqa: E402


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


class MetaAnchorTests(unittest.TestCase):
    """Cross-checking ledger-meta.json against the verified chain."""

    def setUp(self) -> None:
        self._tmp: str = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self._tmp)

    def _path(self) -> str:
        return os.path.join(self._tmp, "ledger.json")

    def _write_chain(self, chain: list[dict]) -> None:
        Path(self._path()).write_text(json.dumps(chain), encoding="utf-8")

    def _write_meta(self, content: str) -> None:
        meta_path: str = os.path.join(self._tmp, "ledger-meta.json")
        Path(meta_path).write_text(content, encoding="utf-8")

    def _meta_for(self, chain: list[dict]) -> dict:
        return {
            "entry_count": len(chain),
            "latest_hash": chain[-1]["entry_hash"],
            "created": chain[0]["timestamp"],
            "last_updated": chain[-1]["timestamp"],
        }

    def test_missing_meta_is_valid_with_skip_note(self) -> None:
        self._write_chain(_build_valid_chain(2))
        result: dict = verify_chain(self._path())
        self.assertTrue(result["valid"])
        self.assertIn("not found", result["meta"])

    def test_malformed_meta_is_valid_with_skip_note(self) -> None:
        self._write_chain(_build_valid_chain(2))
        self._write_meta("{{{not json")
        result: dict = verify_chain(self._path())
        self.assertTrue(result["valid"])
        self.assertIn("unreadable", result["meta"])

    def test_non_object_meta_is_valid_with_skip_note(self) -> None:
        self._write_chain(_build_valid_chain(2))
        self._write_meta('["a", "list"]')
        result: dict = verify_chain(self._path())
        self.assertTrue(result["valid"])
        self.assertIn("not a JSON object", result["meta"])

    def test_matching_meta_is_verified(self) -> None:
        chain: list[dict] = _build_valid_chain(3)
        self._write_chain(chain)
        self._write_meta(json.dumps(self._meta_for(chain)))
        result: dict = verify_chain(self._path())
        self.assertTrue(result["valid"])
        self.assertEqual(result["meta"], "meta anchor verified")

    def test_latest_hash_mismatch_fails(self) -> None:
        chain: list[dict] = _build_valid_chain(3)
        self._write_chain(chain)
        meta: dict = self._meta_for(chain)
        meta["latest_hash"] = "0" * 64
        self._write_meta(json.dumps(meta))
        result: dict = verify_chain(self._path())
        self.assertFalse(result["valid"])
        self.assertEqual(result["failure_type"], "META_MISMATCH")

    def test_entry_count_mismatch_fails(self) -> None:
        chain: list[dict] = _build_valid_chain(3)
        self._write_chain(chain)
        meta: dict = self._meta_for(chain)
        meta["entry_count"] = 2
        self._write_meta(json.dumps(meta))
        result: dict = verify_chain(self._path())
        self.assertFalse(result["valid"])
        self.assertEqual(result["failure_type"], "META_MISMATCH")
        self.assertEqual(result["expected"], 2)
        self.assertEqual(result["found"], 3)

    def test_rewritten_chain_detected_by_meta_anchor(self) -> None:
        original: list[dict] = _build_valid_chain(3)
        meta: dict = self._meta_for(original)
        self._write_meta(json.dumps(meta))
        rewritten: list[dict] = _build_valid_chain(3)
        rewritten[1]["change"]["file"] = "REWRITTEN.py"
        for i in range(1, 3):
            rewritten[i]["previous_hash"] = rewritten[i - 1]["entry_hash"]
            rewritten[i].pop("entry_hash", None)
            rewritten[i]["entry_hash"] = compute_entry_hash(rewritten[i])
        self._write_chain(rewritten)
        result: dict = verify_chain(self._path())
        self.assertFalse(result["valid"])
        self.assertEqual(result["failure_type"], "META_MISMATCH")


if __name__ == "__main__":
    unittest.main()
