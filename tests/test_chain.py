"""Tests for ledger.chain — hash computation, chain linking, append, truncation.

Covers: compute_entry_hash determinism and field exclusion, load_ledger
error handling, _cap_stage_fields truncation, append_entry chain linking
and metadata sync, _atomic_write_json atomicity.

Run: python -m unittest tests.test_chain -v
"""

import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from typing import Any

_REPO_ROOT: Path = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from ledger.chain import (  # noqa: E402
    _atomic_write_json,
    _cap_stage_fields,
    append_entry,
    compute_entry_hash,
    load_ledger,
)


class ComputeEntryHashTests(unittest.TestCase):
    def test_deterministic_for_identical_entries(self) -> None:
        entry: dict = {"a": 1, "b": "hello"}
        self.assertEqual(compute_entry_hash(entry), compute_entry_hash(entry))

    def test_excludes_entry_hash_field(self) -> None:
        base: dict = {"a": 1, "b": 2}
        with_hash: dict = {"a": 1, "b": 2, "entry_hash": "should_be_ignored"}
        self.assertEqual(compute_entry_hash(base), compute_entry_hash(with_hash))

    def test_different_entries_produce_different_hashes(self) -> None:
        e1: dict = {"a": 1}
        e2: dict = {"a": 2}
        self.assertNotEqual(compute_entry_hash(e1), compute_entry_hash(e2))

    def test_hash_is_64_char_hex_string(self) -> None:
        result: str = compute_entry_hash({"x": "y"})
        self.assertRegex(result, r"^[0-9a-f]{64}$")

    def test_sort_keys_ensures_key_order_independence(self) -> None:
        e1: dict = {"a": 1, "b": 2}
        e2: dict = {"b": 2, "a": 1}
        self.assertEqual(compute_entry_hash(e1), compute_entry_hash(e2))

    def test_handles_non_json_native_values(self) -> None:
        entry: dict = {"ts": datetime(2026, 1, 1)}
        result: str = compute_entry_hash(entry)
        self.assertRegex(result, r"^[0-9a-f]{64}$")


class LoadLedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp: str = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self._tmp)

    def _path(self, name: str = "ledger.json") -> str:
        return os.path.join(self._tmp, name)

    def test_missing_file_returns_empty_list(self) -> None:
        self.assertEqual(load_ledger(self._path("nonexistent.json")), [])

    def test_valid_json_array_loaded(self) -> None:
        p: str = self._path()
        data: list = [{"entry_hash": "abc", "x": 1}]
        Path(p).write_text(json.dumps(data), encoding="utf-8")
        self.assertEqual(load_ledger(p), data)

    def test_corrupt_json_returns_empty_list(self) -> None:
        p: str = self._path()
        Path(p).write_text("{{{bad", encoding="utf-8")
        self.assertEqual(load_ledger(p), [])

    def test_non_array_json_returns_empty_list(self) -> None:
        p: str = self._path()
        Path(p).write_text('{"key": "val"}', encoding="utf-8")
        self.assertEqual(load_ledger(p), [])


class CapStageFieldsTests(unittest.TestCase):
    def test_non_dict_passes_through(self) -> None:
        self.assertEqual(_cap_stage_fields("hello"), "hello")

    def test_short_fields_unchanged(self) -> None:
        stage: dict = {"status": "CLEAR", "summary": "ok"}
        self.assertEqual(_cap_stage_fields(stage), stage)

    def test_long_string_field_truncated(self) -> None:
        stage: dict = {"big": "x" * 15_000}
        result: dict = _cap_stage_fields(stage)
        self.assertTrue(result["big"].endswith("[TRUNCATED]"))
        self.assertLessEqual(len(result["big"]), 10_000 + 20)

    def test_nested_list_items_truncated(self) -> None:
        stage: dict = {"findings": [{"evidence": "y" * 15_000}]}
        result: dict = _cap_stage_fields(stage)
        self.assertTrue(result["findings"][0]["evidence"].endswith("[TRUNCATED]"))

    def test_total_serialized_over_50k_collapses(self) -> None:
        stage: dict = {f"f{i}": "z" * 9_999 for i in range(6)}
        stage["status"] = "FINDINGS"
        stage["verdict"] = "PASS"
        result: dict = _cap_stage_fields(stage)
        self.assertTrue(result.get("_capped"))
        self.assertEqual(result["status"], "FINDINGS")


class AppendEntryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp: str = tempfile.mkdtemp()
        self._ledger: str = os.path.join(self._tmp, "ledger.json")
        self._meta: str = os.path.join(self._tmp, "ledger-meta.json")
        self.addCleanup(shutil.rmtree, self._tmp)

    def _minimal_result(self) -> dict:
        return {
            "verdict": "PASS",
            "reason": "test",
            "constitution_hash": "abc123",
            "change": {"file": "test.py", "tool": "Write", "diff_summary": {}},
            "challenger": {"status": "CLEAR"},
            "defender": {"status": "CONFIRM_CLEAR"},
            "oracle": {"verdict": "PASS"},
        }

    def test_first_entry_uses_genesis_marker(self) -> None:
        entry: dict = append_entry(self._minimal_result(), path=self._ledger)
        self.assertEqual(entry["previous_hash"], "GENESIS")

    def test_second_entry_links_to_first(self) -> None:
        first: dict = append_entry(self._minimal_result(), path=self._ledger)
        second: dict = append_entry(self._minimal_result(), path=self._ledger)
        self.assertEqual(second["previous_hash"], first["entry_hash"])

    def test_entry_hash_is_valid(self) -> None:
        entry: dict = append_entry(self._minimal_result(), path=self._ledger)
        recomputed: str = compute_entry_hash(entry)
        self.assertEqual(entry["entry_hash"], recomputed)

    def test_entry_has_uuid_entry_id(self) -> None:
        entry: dict = append_entry(self._minimal_result(), path=self._ledger)
        uuid_pattern: str = r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
        self.assertRegex(entry["entry_id"], uuid_pattern)

    def test_entry_has_utc_iso_timestamp(self) -> None:
        entry: dict = append_entry(self._minimal_result(), path=self._ledger)
        ts: str = entry["timestamp"]
        parsed: datetime = datetime.fromisoformat(ts)
        self.assertIn("+00:00", ts)
        self.assertIsNotNone(parsed)

    def test_missing_change_fields_fallback(self) -> None:
        result: dict = {"verdict": "PASS"}
        entry: dict = append_entry(result, path=self._ledger)
        self.assertEqual(entry["change"]["file"], "unknown")
        self.assertEqual(entry["change"]["tool"], "unknown")

    def test_ledger_file_created_on_first_append(self) -> None:
        self.assertFalse(os.path.exists(self._ledger))
        append_entry(self._minimal_result(), path=self._ledger)
        self.assertTrue(os.path.exists(self._ledger))

    def test_meta_file_created_on_first_append(self) -> None:
        append_entry(self._minimal_result(), path=self._ledger)
        self.assertTrue(os.path.exists(self._meta))

    def test_meta_entry_count_incremented(self) -> None:
        append_entry(self._minimal_result(), path=self._ledger)
        append_entry(self._minimal_result(), path=self._ledger)
        meta: dict = json.loads(Path(self._meta).read_text(encoding="utf-8"))
        self.assertEqual(meta["entry_count"], 2)

    def test_meta_latest_hash_matches(self) -> None:
        entry: dict = append_entry(self._minimal_result(), path=self._ledger)
        meta: dict = json.loads(Path(self._meta).read_text(encoding="utf-8"))
        self.assertEqual(meta["latest_hash"], entry["entry_hash"])

    def test_stages_are_cap_truncated(self) -> None:
        result: dict = self._minimal_result()
        result["challenger"] = {"status": "FINDINGS", "big": "a" * 15_000}
        entry: dict = append_entry(result, path=self._ledger)
        self.assertTrue(
            entry["challenger"]["big"].endswith("[TRUNCATED]")
        )


class AtomicWriteJsonTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp: str = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self._tmp)

    def test_writes_valid_json(self) -> None:
        target: Path = Path(self._tmp) / "out.json"
        _atomic_write_json(target, {"key": "value"})
        result: Any = json.loads(target.read_text(encoding="utf-8"))
        self.assertEqual(result, {"key": "value"})

    def test_replaces_existing_file(self) -> None:
        target: Path = Path(self._tmp) / "out.json"
        _atomic_write_json(target, {"v": 1})
        _atomic_write_json(target, {"v": 2})
        result: Any = json.loads(target.read_text(encoding="utf-8"))
        self.assertEqual(result["v"], 2)


if __name__ == "__main__":
    unittest.main()
