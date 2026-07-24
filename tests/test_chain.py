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
    _BENCH_ROOT,
    _cap_stage_fields,
    _DEFAULT_LEDGER_PATH,
    _is_external_change,
    _redact_external_diff,
    append_entry,
    compute_entry_hash,
    load_ledger,
    resolve_ledger_path,
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


class ResolveLedgerPathTests(unittest.TestCase):
    """Project-scoped ledger routing.

    Bench's hook can be registered globally, so a verdict must land in the
    ledger of the project being governed rather than always in Bench's own.
    """

    def setUp(self) -> None:
        self._prev_env: str | None = os.environ.pop("BENCH_LEDGER_PATH", None)
        self._prev_cwd: str = os.getcwd()

    def tearDown(self) -> None:
        os.chdir(self._prev_cwd)
        os.environ.pop("BENCH_LEDGER_PATH", None)
        if self._prev_env is not None:
            os.environ["BENCH_LEDGER_PATH"] = self._prev_env

    def test_env_override_wins(self) -> None:
        os.environ["BENCH_LEDGER_PATH"] = "/custom/central.json"
        self.assertEqual(resolve_ledger_path(), "/custom/central.json")

    def test_env_override_ignored_when_blank(self) -> None:
        os.environ["BENCH_LEDGER_PATH"] = "   "
        os.chdir(str(_BENCH_ROOT))
        self.assertEqual(resolve_ledger_path(), _DEFAULT_LEDGER_PATH)

    def test_bench_repo_root_uses_bench_ledger(self) -> None:
        os.chdir(str(_BENCH_ROOT))
        self.assertEqual(resolve_ledger_path(), _DEFAULT_LEDGER_PATH)

    def test_subdirectory_of_bench_repo_uses_bench_ledger(self) -> None:
        os.chdir(str(_BENCH_ROOT / "tests"))
        self.assertEqual(resolve_ledger_path(), _DEFAULT_LEDGER_PATH)

    def test_foreign_project_gets_its_own_ledger(self) -> None:
        foreign: str = tempfile.mkdtemp()
        try:
            os.chdir(foreign)
            resolved: Path = Path(resolve_ledger_path())
            self.assertNotEqual(str(resolved), _DEFAULT_LEDGER_PATH)
            self.assertEqual(resolved.name, "bench-ledger.json")
            self.assertEqual(resolved.parent.name, ".bench")
            # The governed project's ledger must live under that project,
            # not under the Bench checkout.
            self.assertEqual(
                resolved.parent.parent.resolve(), Path(foreign).resolve()
            )
        finally:
            os.chdir(self._prev_cwd)
            shutil.rmtree(foreign, ignore_errors=True)

    def test_writer_and_reader_agree_on_foreign_project(self) -> None:
        """append_entry() and load_ledger() must target the same file.

        This is the invariant the auditor depends on: if the writer routes
        by project but a reader still resolves to Bench's own ledger, the
        chain would verify clean while verdicts accumulated elsewhere.
        """
        foreign: str = tempfile.mkdtemp()
        try:
            os.chdir(foreign)
            # Guard before writing: if routing were broken this would resolve
            # to Bench's real ledger, and an append there is irreversible
            # under C-008. Fail the test instead of contaminating the chain.
            target: Path = Path(resolve_ledger_path()).resolve()
            self.assertTrue(
                target.is_relative_to(Path(foreign).resolve()),
                f"refusing to append: {target} is outside the test fixture",
            )
            append_entry(
                {
                    "verdict": "PASS",
                    "constitution_hash": "abc123",
                    "change": {
                        "file": "app/main.py",
                        "tool": "Write",
                        "diff_summary": {},
                    },
                }
            )
            written: Path = Path(foreign) / ".bench" / "bench-ledger.json"
            self.assertTrue(written.exists(), "verdict did not land in project")

            entries: list[dict] = load_ledger()
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0]["change"]["file"], "app/main.py")

            # The meta anchor is colocated with the chain it describes.
            self.assertTrue((Path(foreign) / ".bench" / "ledger-meta.json").exists())

            # Bench's own ledger must be untouched by a foreign project.
            bench_entries: list[dict] = load_ledger(_DEFAULT_LEDGER_PATH)
            self.assertFalse(
                any(e["change"].get("file") == "app/main.py" for e in bench_entries),
                "foreign verdict contaminated Bench's own ledger",
            )
        finally:
            os.chdir(self._prev_cwd)
            shutil.rmtree(foreign, ignore_errors=True)


class ExternalChangeRedactionTests(unittest.TestCase):
    """Out-of-project file bodies must never reach the ledger.

    A globally registered hook governs files belonging to other projects.
    Their diffs are adjudicated in full but recorded as metadata only, so a
    published ledger cannot become a mirror of someone else's source.
    """

    def setUp(self) -> None:
        self._prev_env: str | None = os.environ.pop("BENCH_LEDGER_PATH", None)
        self._prev_cwd: str = os.getcwd()

    def tearDown(self) -> None:
        os.chdir(self._prev_cwd)
        os.environ.pop("BENCH_LEDGER_PATH", None)
        if self._prev_env is not None:
            os.environ["BENCH_LEDGER_PATH"] = self._prev_env

    def test_relative_path_is_never_external(self) -> None:
        os.chdir(str(_BENCH_ROOT))
        self.assertFalse(_is_external_change(os.path.join("utils", "api.py")))

    def test_absolute_path_inside_project_is_not_external(self) -> None:
        os.chdir(str(_BENCH_ROOT))
        self.assertFalse(
            _is_external_change(str(_BENCH_ROOT / "utils" / "api.py"))
        )

    def test_bench_file_from_subdirectory_is_not_external(self) -> None:
        """Classification anchors on the project root, not the raw CWD.

        Editing utils/api.py while sitting in tests/ is still in-project. If
        this used os.getcwd() directly it would misclassify and strip
        evidence from Bench's own self-governance record.
        """
        os.chdir(str(_BENCH_ROOT / "tests"))
        self.assertFalse(
            _is_external_change(str(_BENCH_ROOT / "utils" / "api.py"))
        )

    def test_path_outside_project_is_external(self) -> None:
        foreign: str = tempfile.mkdtemp()
        try:
            os.chdir(str(_BENCH_ROOT))
            self.assertTrue(
                _is_external_change(os.path.join(foreign, "private.py"))
            )
        finally:
            shutil.rmtree(foreign, ignore_errors=True)

    def test_sentinel_values_are_not_external(self) -> None:
        os.chdir(str(_BENCH_ROOT))
        self.assertFalse(_is_external_change(""))
        self.assertFalse(_is_external_change("unknown"))

    def test_redaction_drops_bodies_and_keeps_metadata(self) -> None:
        redacted: dict = _redact_external_diff(
            {
                "file_path": "secret.py",
                "change_type": "modify",
                "old_string": "API_TOKEN = 'live'",
                "new_string": "API_TOKEN = os.environ['T']",
                "content": "whole file body",
                "truncation": {"old": "truncated"},
            }
        )
        self.assertNotIn("old_string", redacted)
        self.assertNotIn("new_string", redacted)
        self.assertNotIn("content", redacted)
        self.assertEqual(redacted["file_path"], "secret.py")
        self.assertEqual(redacted["change_type"], "modify")
        self.assertEqual(redacted["truncation"], {"old": "truncated"})
        self.assertTrue(redacted["redacted"])
        serialized: str = json.dumps(redacted)
        self.assertNotIn("API_TOKEN", serialized)
        self.assertNotIn("whole file body", serialized)

    def test_redaction_handles_non_dict_summary(self) -> None:
        redacted: dict = _redact_external_diff("raw diff text")
        self.assertTrue(redacted["redacted"])
        self.assertNotIn("raw diff text", json.dumps(redacted))

    def test_append_redacts_external_file_body(self) -> None:
        tmp: str = tempfile.mkdtemp()
        foreign: str = tempfile.mkdtemp()
        try:
            os.chdir(str(_BENCH_ROOT))
            ledger: str = os.path.join(tmp, "bench-ledger.json")
            entry: dict = append_entry(
                {
                    "verdict": "PASS",
                    "change": {
                        "file": os.path.join(foreign, "billing.ts"),
                        "tool": "Edit",
                        "diff_summary": {
                            "file_path": "billing.ts",
                            "old_string": "const SECRET_RATE = 0.3",
                            "new_string": "const SECRET_RATE = 0.4",
                        },
                    },
                },
                path=ledger,
            )
            body: str = json.dumps(entry)
            self.assertNotIn("SECRET_RATE", body)
            self.assertTrue(entry["change"]["diff_summary"]["redacted"])
            # The path and verdict survive: the audit trail still shows that
            # this file was governed and how it was ruled on.
            self.assertIn("billing.ts", entry["change"]["file"])
            self.assertEqual(entry["verdict"], "PASS")
        finally:
            os.chdir(self._prev_cwd)
            shutil.rmtree(tmp, ignore_errors=True)
            shutil.rmtree(foreign, ignore_errors=True)

    def test_append_keeps_in_project_body_intact(self) -> None:
        tmp: str = tempfile.mkdtemp()
        try:
            os.chdir(str(_BENCH_ROOT))
            ledger: str = os.path.join(tmp, "bench-ledger.json")
            entry: dict = append_entry(
                {
                    "verdict": "PASS",
                    "change": {
                        "file": os.path.join("utils", "api.py"),
                        "tool": "Edit",
                        "diff_summary": {
                            "file_path": "utils/api.py",
                            "old_string": "CHALLENGER_MODEL = 'a'",
                            "new_string": "CHALLENGER_MODEL = 'b'",
                        },
                    },
                },
                path=ledger,
            )
            summary: dict = entry["change"]["diff_summary"]
            self.assertNotIn("redacted", summary)
            self.assertEqual(summary["old_string"], "CHALLENGER_MODEL = 'a'")
        finally:
            os.chdir(self._prev_cwd)
            shutil.rmtree(tmp, ignore_errors=True)


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
