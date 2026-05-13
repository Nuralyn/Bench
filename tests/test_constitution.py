"""Tests for pipeline.constitution — loading, hashing, schema validation.

Covers: load_constitution_snapshot with valid/invalid files and all three
exception types, get_constraint_by_id lookup, hash determinism.

Run: python -m unittest tests.test_constitution -v
"""

import hashlib
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

from pipeline.constitution import (  # noqa: E402
    ConstitutionNotFoundError,
    ConstitutionParseError,
    ConstitutionSchemaError,
    get_constraint_by_id,
    load_constitution_snapshot,
)


def _valid_constitution() -> dict:
    return {
        "constitution": "bench-v1",
        "version": 1,
        "constraints": [
            {
                "id": "C-001",
                "name": "Test Constraint",
                "rule": "No silent error swallowing",
                "severity": "veto",
            }
        ],
    }


class LoadConstitutionSnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp: str = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self._tmp)

    def _write(self, content: str, name: str = "bench.json") -> str:
        p: str = os.path.join(self._tmp, name)
        Path(p).write_text(content, encoding="utf-8")
        return p

    def test_valid_constitution_returns_data_and_hash(self) -> None:
        raw: str = json.dumps(_valid_constitution())
        path: str = self._write(raw)
        data, h = load_constitution_snapshot(path)
        self.assertIsInstance(data, dict)
        self.assertRegex(h, r"^[0-9a-f]{64}$")
        self.assertEqual(data["constitution"], "bench-v1")

    def test_hash_matches_sha256_of_raw_bytes(self) -> None:
        raw: str = json.dumps(_valid_constitution())
        path: str = self._write(raw)
        _, h = load_constitution_snapshot(path)
        expected: str = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        self.assertEqual(h, expected)

    def test_file_not_found_raises_not_found_error(self) -> None:
        with self.assertRaises(ConstitutionNotFoundError):
            load_constitution_snapshot(os.path.join(self._tmp, "nope.json"))

    def test_invalid_json_raises_parse_error(self) -> None:
        path: str = self._write("{{{bad json")
        with self.assertRaises(ConstitutionParseError):
            load_constitution_snapshot(path)

    def test_non_dict_root_raises_schema_error(self) -> None:
        path: str = self._write("[1, 2, 3]")
        with self.assertRaises(ConstitutionSchemaError):
            load_constitution_snapshot(path)

    def test_missing_top_level_field_raises_schema_error(self) -> None:
        path: str = self._write(json.dumps({"constitution": "v1", "version": 1}))
        with self.assertRaises(ConstitutionSchemaError):
            load_constitution_snapshot(path)

    def test_constraints_not_list_raises_schema_error(self) -> None:
        doc: dict = _valid_constitution()
        doc["constraints"] = "not a list"
        path: str = self._write(json.dumps(doc))
        with self.assertRaises(ConstitutionSchemaError):
            load_constitution_snapshot(path)

    def test_constraint_not_dict_raises_schema_error(self) -> None:
        doc: dict = _valid_constitution()
        doc["constraints"] = ["not a dict"]
        path: str = self._write(json.dumps(doc))
        with self.assertRaises(ConstitutionSchemaError):
            load_constitution_snapshot(path)

    def test_constraint_missing_required_field_raises_schema_error(self) -> None:
        doc: dict = _valid_constitution()
        del doc["constraints"][0]["id"]
        path: str = self._write(json.dumps(doc))
        with self.assertRaises(ConstitutionSchemaError):
            load_constitution_snapshot(path)

    def test_constraint_empty_string_field_raises_schema_error(self) -> None:
        doc: dict = _valid_constitution()
        doc["constraints"][0]["id"] = ""
        path: str = self._write(json.dumps(doc))
        with self.assertRaises(ConstitutionSchemaError):
            load_constitution_snapshot(path)

    def test_constraint_non_string_field_raises_schema_error(self) -> None:
        doc: dict = _valid_constitution()
        doc["constraints"][0]["severity"] = 42
        path: str = self._write(json.dumps(doc))
        with self.assertRaises(ConstitutionSchemaError):
            load_constitution_snapshot(path)

    def test_multiple_valid_constraints_accepted(self) -> None:
        doc: dict = _valid_constitution()
        doc["constraints"].append({
            "id": "C-002",
            "name": "Second",
            "rule": "Scope boundary",
            "severity": "veto",
        })
        path: str = self._write(json.dumps(doc))
        data, _ = load_constitution_snapshot(path)
        self.assertEqual(len(data["constraints"]), 2)


class GetConstraintByIdTests(unittest.TestCase):
    def test_finds_existing_constraint(self) -> None:
        doc: dict = _valid_constitution()
        result = get_constraint_by_id(doc, "C-001")
        self.assertIsNotNone(result)
        self.assertEqual(result["id"], "C-001")

    def test_returns_none_for_missing_id(self) -> None:
        doc: dict = _valid_constitution()
        self.assertIsNone(get_constraint_by_id(doc, "C-999"))

    def test_handles_missing_constraints_key(self) -> None:
        self.assertIsNone(get_constraint_by_id({}, "C-001"))

    def test_handles_non_dict_entries_in_list(self) -> None:
        doc: dict = {"constraints": ["not a dict", {"id": "C-001"}]}
        result = get_constraint_by_id(doc, "C-001")
        self.assertIsNotNone(result)


if __name__ == "__main__":
    unittest.main()
