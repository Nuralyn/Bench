"""Tests for ledger.chain — hash computation, chain linking, append, truncation.

Covers: compute_entry_hash determinism and field exclusion, load_ledger
error handling, _cap_stage_fields truncation, append_entry chain linking
and metadata sync, _atomic_write_json atomicity.

Run: python -m unittest tests.test_chain -v
"""

import contextlib
import io
import json
import os
import shutil
import statistics
import sys
import tempfile
import time
import unittest
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch

_REPO_ROOT: Path = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from ledger.chain import (  # noqa: E402
    TIP_CACHE_FILENAME,
    _atomic_write_json,
    _cap_stage_fields,
    _entry_file_names,
    _is_external_change,
    _read_entry_file,
    _redact_external_diff,
    append_entry,
    compute_entry_hash,
    LedgerReadError,
    load_ledger,
    resolve_entries_dir,
    resolve_ledger_path,
)
from ledger.verify import verify_chain  # noqa: E402
from tests._ledger_fixtures import build_valid_chain  # noqa: E402
from utils.project import BENCH_ROOT  # noqa: E402

# Bench no longer has a special ledger location: it resolves to its own
# project-local .bench/ exactly as any governed project does. chain.py
# therefore exports no Bench-specific path constant, so the expected value is
# derived here from the same project root the resolver uses.
_BENCH_ROOT: Path = BENCH_ROOT
_BENCH_LEDGER_PATH: str = str(BENCH_ROOT / ".bench" / "bench-ledger.json")


class ComputeEntryHashTests(unittest.TestCase):
    def test_deterministic_for_identical_entries(self) -> None:
        entry: dict = {"a": 1, "b": "hello"}
        twin: dict = {"a": 1, "b": "hello"}
        self.assertEqual(compute_entry_hash(entry), compute_entry_hash(twin))

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
        self.assertEqual(resolve_ledger_path(), _BENCH_LEDGER_PATH)

    def test_bench_repo_root_uses_bench_ledger(self) -> None:
        os.chdir(str(_BENCH_ROOT))
        self.assertEqual(resolve_ledger_path(), _BENCH_LEDGER_PATH)

    def test_subdirectory_of_bench_repo_uses_bench_ledger(self) -> None:
        os.chdir(str(_BENCH_ROOT / "tests"))
        self.assertEqual(resolve_ledger_path(), _BENCH_LEDGER_PATH)

    def test_foreign_project_gets_its_own_ledger(self) -> None:
        foreign: str = tempfile.mkdtemp()
        try:
            os.chdir(foreign)
            resolved: Path = Path(resolve_ledger_path())
            self.assertNotEqual(str(resolved), _BENCH_LEDGER_PATH)
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
            written: Path = Path(foreign) / ".bench" / "entries"
            self.assertTrue(
                written.is_dir() and any(written.glob("*.json")),
                "verdict did not land in project",
            )

            entries: list[dict] = load_ledger()
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0]["change"]["file"], "app/main.py")

            # The entries directory is colocated with the ledger it belongs to.
            self.assertEqual(
                Path(resolve_entries_dir()).resolve(), written.resolve()
            )

            # Bench's own ledger must be untouched by a foreign project.
            bench_entries: list[dict] = load_ledger(_BENCH_LEDGER_PATH)
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
        # A list of every current tip, not a bare string: that is what lets a
        # fork left by a git merge be reconciled by the next append.
        self.assertEqual(second["previous_hash"], [first["entry_hash"]])
        self.assertEqual(first["previous_hash"], "GENESIS")

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

    def test_entry_file_created_on_first_append(self) -> None:
        """Entries are written one per file, named by their own hash."""
        entry: dict = append_entry(self._minimal_result(), path=self._ledger)
        entry_file: Path = (
            Path(resolve_entries_dir(self._ledger)) / f"{entry['entry_hash']}.json"
        )
        self.assertTrue(entry_file.is_file())
        written: dict = json.loads(entry_file.read_text(encoding="utf-8"))
        self.assertEqual(written["entry_hash"], entry["entry_hash"])

    def test_legacy_array_is_never_written(self) -> None:
        """The frozen segment must not be created or touched by an append.

        A file that is never written cannot conflict between branches, and
        freezing it is what keeps C-008 satisfied without moving any entry.
        """
        self.assertFalse(os.path.exists(self._ledger))
        append_entry(self._minimal_result(), path=self._ledger)
        self.assertFalse(os.path.exists(self._ledger))
        self.assertFalse(os.path.exists(self._meta))

    def test_append_leaves_an_existing_legacy_array_byte_identical(self) -> None:
        seeded: list[dict] = build_valid_chain(3)
        Path(self._ledger).write_text(
            json.dumps(seeded, indent=2), encoding="utf-8"
        )
        before: bytes = Path(self._ledger).read_bytes()

        entry: dict = append_entry(self._minimal_result(), path=self._ledger)

        self.assertEqual(Path(self._ledger).read_bytes(), before)
        self.assertEqual(entry["previous_hash"], [seeded[-1]["entry_hash"]])

    def test_refuses_to_overwrite_an_existing_entry_file(self) -> None:
        """C-008: an existing entry file is never overwritten.

        Real entries carry a uuid and timestamp so their hashes differ; the
        hash is pinned here so the collision is deterministic and the raising
        call is unambiguous.
        """
        with patch("ledger.chain.compute_entry_hash", return_value="deadbeef"):
            append_entry(self._minimal_result(), path=self._ledger)
            colliding: dict = self._minimal_result()
            with self.assertRaises(LedgerReadError):
                append_entry(colliding, path=self._ledger)

    def test_corrupt_legacy_array_raises_and_is_left_on_disk(self) -> None:
        """Fail closed, and preserve the evidence.

        Treating a corrupt array as an empty chain is what previously let the
        next append restart from GENESIS and overwrite the damaged file.
        """
        Path(self._ledger).write_text("{not json", encoding="utf-8")
        before: bytes = Path(self._ledger).read_bytes()
        result: dict = self._minimal_result()

        with self.assertRaises(LedgerReadError):
            append_entry(result, path=self._ledger)

        self.assertEqual(Path(self._ledger).read_bytes(), before)

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


def _reads_during(action: Callable[[], dict]) -> tuple[dict, list[str]]:
    """Run ``action`` and return its result with the entry files it read.

    Wraps the module's own reader, so both the cache path and the full scan
    are counted through the same seam.
    """
    reads: list[str] = []

    def counting(entry_file: Path, *, strict: bool) -> dict | None:
        reads.append(entry_file.name)
        return _read_entry_file(entry_file, strict=strict)

    with patch("ledger.chain._read_entry_file", side_effect=counting):
        return action(), reads


def _pass_result(name: str) -> dict:
    return {
        "verdict": "PASS",
        "constitution_hash": "abc123",
        "change": {"file": name, "tool": "Write", "diff_summary": {}},
    }


class TipCacheTests(unittest.TestCase):
    """The derived tip cache on the write path.

    The cache lets an append validate only the entries it links to. These
    tests pin what makes that safe: the cache is consulted but never trusted
    past what can be checked against the chain, every doubt falls back to
    the full scan, a refused append writes nothing, and neither the readers
    nor the auditor look at it.
    """

    def setUp(self) -> None:
        self._tmp: str = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self._tmp, True)
        self._ledger: str = os.path.join(self._tmp, "bench-ledger.json")
        self._entries: Path = Path(resolve_entries_dir(self._ledger))
        self._cache: Path = Path(self._tmp) / TIP_CACHE_FILENAME

    def _append(self, name: str = "file.py") -> dict:
        return append_entry(_pass_result(name), path=self._ledger)

    def _cache_data(self) -> dict:
        data: dict = json.loads(self._cache.read_text(encoding="utf-8"))
        return data

    def _rewrite_cache(self, **overrides: Any) -> None:
        """Rewrite the cache with fields replaced; the digest stays valid."""
        data: dict = self._cache_data()
        data.update(overrides)
        self._cache.write_text(json.dumps(data), encoding="utf-8")

    def _write_foreign_entry(
        self, parents: list[str], name: str = "foreign.py"
    ) -> dict:
        """An entry file that did not pass through this writer.

        Another process, a restore from an archive, a hand copy: anything
        that changes the listing without updating the cache.
        """
        entry: dict[str, Any] = {
            "entry_id": "foreign",
            "timestamp": "2026-01-01T00:00:00+00:00",
            "previous_hash": parents,
            "constitution_hash": "abc",
            "verdict": "PASS",
            "change": {"file": name, "tool": "Write", "diff_summary": {}},
        }
        entry["entry_hash"] = compute_entry_hash(entry)
        self._entries.mkdir(parents=True, exist_ok=True)
        (self._entries / f"{entry['entry_hash']}.json").write_text(
            json.dumps(entry), encoding="utf-8"
        )
        return entry

    def _seed_legacy(self, chain: list[dict]) -> None:
        Path(self._ledger).write_text(
            json.dumps(chain, indent=2), encoding="utf-8"
        )

    # --- the fast path ------------------------------------------------------

    def test_an_append_records_itself_as_the_only_tip(self) -> None:
        entry: dict = self._append()
        data: dict = self._cache_data()
        self.assertEqual(data["tips"], [entry["entry_hash"]])
        self.assertEqual(data["format"], 1)
        self.assertEqual(data["legacy_count"], 0)

    def test_a_cache_hit_reads_only_the_tip_it_links_to(self) -> None:
        tip: dict = {}
        for name in ("a.py", "b.py", "c.py"):
            tip = self._append(name)
        entry, reads = _reads_during(lambda: self._append("d.py"))
        self.assertEqual(reads, [f"{tip['entry_hash']}.json"])
        self.assertEqual(entry["previous_hash"], [tip["entry_hash"]])

    def test_the_cache_pins_the_frozen_array(self) -> None:
        seeded: list[dict] = build_valid_chain(3)
        self._seed_legacy(seeded)
        entry: dict = self._append()
        data: dict = self._cache_data()
        self.assertEqual(data["legacy_count"], 3)
        self.assertEqual(data["legacy_tip"], seeded[-1]["entry_hash"])
        self.assertEqual(entry["previous_hash"], [seeded[-1]["entry_hash"]])

    # --- every doubt falls back to the full scan ----------------------------

    def test_a_missing_cache_is_rebuilt_from_the_full_scan(self) -> None:
        self._append("a.py")
        tip: dict = self._append("b.py")
        self._cache.unlink()
        entry, reads = _reads_during(lambda: self._append("c.py"))
        # Every entry file was read, not just the tip: that is the scan.
        self.assertEqual(len(reads), 2)
        self.assertEqual(entry["previous_hash"], [tip["entry_hash"]])
        self.assertEqual(self._cache_data()["tips"], [entry["entry_hash"]])

    def test_an_entry_added_outside_the_writer_invalidates_the_cache(
        self,
    ) -> None:
        self._append("a.py")
        tip: dict = self._append("b.py")
        foreign: dict = self._write_foreign_entry([tip["entry_hash"]])

        entry: dict = self._append("c.py")

        self.assertEqual(entry["previous_hash"], [foreign["entry_hash"]])
        result: dict = verify_chain(self._ledger)
        self.assertTrue(result["valid"], result.get("message"))
        self.assertEqual(result["tips"], [entry["entry_hash"]])

    def test_a_fork_left_by_a_foreign_entry_is_reconciled(self) -> None:
        first: dict = self._append("a.py")
        second: dict = self._append("b.py")
        foreign: dict = self._write_foreign_entry([first["entry_hash"]])

        entry: dict = self._append("c.py")

        self.assertEqual(
            entry["previous_hash"],
            sorted([second["entry_hash"], foreign["entry_hash"]]),
        )
        result: dict = verify_chain(self._ledger)
        self.assertTrue(result["valid"], result.get("message"))
        self.assertEqual(result["tips"], [entry["entry_hash"]])

    def test_an_entry_removed_outside_the_writer_invalidates_the_cache(
        self,
    ) -> None:
        """Removal is forbidden by C-008; the point is that the cache notices."""
        first: dict = self._append("a.py")
        second: dict = self._append("b.py")
        (self._entries / f"{second['entry_hash']}.json").unlink()

        entry: dict = self._append("c.py")

        self.assertEqual(entry["previous_hash"], [first["entry_hash"]])

    def test_a_cache_naming_a_tip_that_does_not_resolve_is_rescanned(
        self,
    ) -> None:
        tip: dict = self._append("a.py")
        self._rewrite_cache(tips=["f" * 64])

        entry: dict = self._append("b.py")

        self.assertEqual(entry["previous_hash"], [tip["entry_hash"]])
        self.assertEqual(self._cache_data()["tips"], [entry["entry_hash"]])

    def test_a_cache_naming_the_frozen_arrays_tip_is_rescanned(self) -> None:
        """The writer records only the entry it just wrote, so a cache naming
        the frozen array's tip did not come from it. Accepting that tip
        without the file check would link to it with no proof that nothing
        already descends from it, which would fork the chain."""
        seeded: list[dict] = build_valid_chain(3)
        self._seed_legacy(seeded)
        tip: dict = self._append("a.py")
        self._rewrite_cache(tips=[seeded[-1]["entry_hash"]])

        entry: dict = self._append("b.py")

        self.assertEqual(entry["previous_hash"], [tip["entry_hash"]])
        result: dict = verify_chain(self._ledger)
        self.assertTrue(result["valid"], result.get("message"))
        self.assertEqual(result["tips"], [entry["entry_hash"]])

    def test_a_swapped_frozen_array_invalidates_the_cache(self) -> None:
        self._seed_legacy(build_valid_chain(3))
        tip: dict = self._append("a.py")
        other: list[dict] = build_valid_chain(3, ["VETO", "PASS", "PASS"])
        self._seed_legacy(other)

        entry: dict = self._append("b.py")

        # The scan's answer for the union as it now stands, not the cache's.
        self.assertEqual(
            entry["previous_hash"],
            sorted([other[-1]["entry_hash"], tip["entry_hash"]]),
        )

    def test_a_corrupt_cache_falls_back_and_is_replaced(self) -> None:
        tip: dict = self._append("a.py")
        self._cache.write_text("{not json", encoding="utf-8")

        entry: dict = self._append("b.py")

        self.assertEqual(entry["previous_hash"], [tip["entry_hash"]])
        self.assertEqual(self._cache_data()["tips"], [entry["entry_hash"]])

    def test_an_unknown_cache_format_falls_back(self) -> None:
        tip: dict = self._append("a.py")
        self._rewrite_cache(format=99)

        entry: dict = self._append("b.py")

        self.assertEqual(entry["previous_hash"], [tip["entry_hash"]])
        self.assertEqual(self._cache_data()["format"], 1)

    # --- the cache never changes what a corrupt ledger does -----------------

    def test_a_defective_tip_is_handed_to_the_scan_which_refuses(self) -> None:
        tip: dict = self._append("a.py")
        target: Path = self._entries / f"{tip['entry_hash']}.json"
        tampered: dict = json.loads(target.read_text(encoding="utf-8"))
        tampered["verdict"] = "VETO"
        target.write_text(json.dumps(tampered), encoding="utf-8")
        before: bytes = self._cache.read_bytes()

        with self.assertRaises(LedgerReadError):
            self._append("b.py")

        # A refused append writes nothing, the cache included.
        self.assertEqual(self._cache.read_bytes(), before)

    def test_a_cache_that_cannot_be_written_does_not_fail_the_append(
        self,
    ) -> None:
        tip: dict = self._append("a.py")
        self._cache.unlink()
        self._cache.mkdir()  # os.replace onto a directory fails
        stderr: io.StringIO = io.StringIO()

        with contextlib.redirect_stderr(stderr):
            entry: dict = self._append("b.py")

        self.assertEqual(entry["previous_hash"], [tip["entry_hash"]])
        self.assertTrue(
            (self._entries / f"{entry['entry_hash']}.json").is_file()
        )
        self.assertIn("tip cache not updated", stderr.getvalue())

    # --- the listing the cache is keyed on ----------------------------------

    def test_the_listing_is_sorted_names_with_the_entry_suffix(self) -> None:
        self.assertEqual(_entry_file_names(self._entries), [])
        self._append("a.py")
        self._append("b.py")
        (self._entries / "notes.txt").write_text("x", encoding="utf-8")
        (self._entries / "stray.json.tmp").write_text("x", encoding="utf-8")

        names: list[str] = _entry_file_names(self._entries)

        self.assertEqual(names, sorted(names))
        self.assertEqual(
            names, sorted(p.name for p in self._entries.glob("*.json"))
        )

    def test_the_scan_not_the_listing_decides_whether_the_ledger_is_empty(
        self,
    ) -> None:
        """An entry the listing misses must never be answered with GENESIS.

        The listing is an exact-suffix filter and the scan is a glob, which
        differ on a differently cased suffix on Windows. The listing feeds
        only the cache; emptiness is decided by the same enumeration the
        auditor uses.
        """
        tip: dict = self._append("a.py")

        with patch("ledger.chain._entry_file_names", return_value=[]):
            entry: dict = self._append("b.py")

        self.assertEqual(entry["previous_hash"], [tip["entry_hash"]])
        result: dict = verify_chain(self._ledger)
        self.assertTrue(result["valid"], result.get("message"))

    def test_a_stray_directory_with_the_suffix_is_refused_as_before(
        self,
    ) -> None:
        """The listing does not stat each name, so a directory named like an
        entry is listed. Its appearance changes the listing, the cache goes
        stale, and the scan refuses it as unreadable, which is what the
        write path did before the cache existed. The auditor reports it."""
        self._append("a.py")
        (self._entries / "stray.json").mkdir()
        before: bytes = self._cache.read_bytes()

        with self.assertRaises(LedgerReadError):
            self._append("b.py")

        self.assertEqual(self._cache.read_bytes(), before)
        result: dict = verify_chain(self._ledger)
        self.assertFalse(result["valid"])
        self.assertEqual(result["failure_type"], "READ_ERROR")

    # --- derived, never authoritative ---------------------------------------

    def test_readers_and_the_auditor_ignore_the_cache(self) -> None:
        first: dict = self._append("a.py")
        second: dict = self._append("b.py")
        self._rewrite_cache(tips=[first["entry_hash"]])  # wrong on purpose

        self.assertEqual(len(load_ledger(self._ledger)), 2)
        result: dict = verify_chain(self._ledger)
        self.assertTrue(result["valid"], result.get("message"))
        self.assertEqual(result["tips"], [second["entry_hash"]])

        self._cache.write_text("garbage", encoding="utf-8")
        self.assertTrue(verify_chain(self._ledger)["valid"])
        self.assertEqual(len(load_ledger(self._ledger)), 2)


def _seed_linear_chain(entries_dir: Path, size: int) -> str:
    """Write ``size`` correctly linked entry files directly; return the tip."""
    entries_dir.mkdir(parents=True, exist_ok=True)
    previous: str | list[str] = "GENESIS"
    tip: str = ""
    for index in range(size):
        entry: dict[str, Any] = {
            "entry_id": f"seed-{index}",
            "timestamp": "2026-01-01T00:00:00+00:00",
            "previous_hash": previous,
            "constitution_hash": "abc",
            "verdict": "PASS",
            "change": {
                "file": f"seed_{index}.py",
                "tool": "Write",
                "diff_summary": {},
            },
        }
        tip = compute_entry_hash(entry)
        entry["entry_hash"] = tip
        (entries_dir / f"{tip}.json").write_text(
            json.dumps(entry), encoding="utf-8"
        )
        previous = [tip]
    return tip


class AppendScalingBenchmark(unittest.TestCase):
    """Roadmap 4.1 acceptance: append time is flat from 1,000 to 20,000 entries.

    Seeds two chains directly on disk (several seconds for the larger one),
    lets the first append rebuild the cache with a full scan, then times the
    steady state and, separately, the directory listing the cache is keyed
    on. The listing is the acknowledged floor: it is how an append notices
    a file added by anything other than itself, it is the only cost that
    still grows with the chain, and it is two orders of magnitude cheaper
    than reading the chain (about 20 ms against 2 s at 20,000 entries on
    NTFS). Three things are asserted. The deterministic one: a steady state
    append reads exactly one entry file, the tip, at either size. Then, net
    of the listing, the median append at 20,000 entries stays within
    ``FLATNESS_BOUND`` of the median at 1,000. And the whole append at
    20,000 stays at least ``SCAN_MARGIN`` times cheaper than the scan it
    replaced. The raw figures, listing included, are printed so a CI log
    carries them.
    """

    SIZES: tuple[int, int] = (1_000, 20_000)
    REPS: int = 7
    # Measured 2.1x on NTFS (8.1 ms to 35.5 ms, of which 0.9 ms and 20.1 ms
    # of listing). The bound leaves room for a runner where the fixed costs
    # (two fsyncs) are cheaper and the ratio therefore sits higher; a
    # reintroduced per-entry read would put it past 50x.
    FLATNESS_BOUND: float = 4.0
    SCAN_MARGIN: float = 10.0

    def setUp(self) -> None:
        self._tmp: str = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self._tmp, True)

    @staticmethod
    def _median_ms(action: Callable[[], object], reps: int) -> float:
        samples: list[float] = []
        for _ in range(reps):
            started: float = time.perf_counter()
            action()
            samples.append((time.perf_counter() - started) * 1000)
        return statistics.median(samples)

    def test_append_time_is_flat_from_one_thousand_to_twenty_thousand(
        self,
    ) -> None:
        measured: dict[int, dict[str, float]] = {}
        for size in self.SIZES:
            ledger: str = os.path.join(
                self._tmp, f"chain-{size}", "bench-ledger.json"
            )
            entries_dir: Path = Path(resolve_entries_dir(ledger))
            tip: str = _seed_linear_chain(entries_dir, size)

            started: float = time.perf_counter()
            rebuilt, reads = _reads_during(
                lambda: append_entry(_pass_result("rebuild.py"), path=ledger)
            )
            scan_ms: float = (time.perf_counter() - started) * 1000
            self.assertEqual(rebuilt["previous_hash"], [tip])
            self.assertEqual(len(reads), size, "the rebuild is the full scan")
            tip = rebuilt["entry_hash"]

            samples: list[float] = []
            for _ in range(self.REPS):
                started = time.perf_counter()
                entry, reads = _reads_during(
                    lambda: append_entry(_pass_result("steady.py"), path=ledger)
                )
                samples.append((time.perf_counter() - started) * 1000)
                self.assertEqual(
                    reads,
                    [f"{tip}.json"],
                    "a steady-state append reads only the tip it links to",
                )
                self.assertEqual(entry["previous_hash"], [tip])
                tip = entry["entry_hash"]
            measured[size] = {
                "append": statistics.median(samples),
                "listing": self._median_ms(
                    lambda: _entry_file_names(entries_dir), self.REPS
                ),
                "scan": scan_ms,
            }

        for size, row in measured.items():
            print(
                f"[bench benchmark] {size:>6,d} entries: append "
                f"{row['append']:7.1f} ms (median of {self.REPS}), of which "
                f"listing {row['listing']:6.1f} ms; full scan "
                f"{row['scan']:8.1f} ms",
                file=sys.stderr,
            )

        small: dict[str, float] = measured[self.SIZES[0]]
        large: dict[str, float] = measured[self.SIZES[1]]
        small_net: float = small["append"] - small["listing"]
        large_net: float = large["append"] - large["listing"]
        self.assertLessEqual(
            large_net,
            small_net * self.FLATNESS_BOUND,
            f"append net of the listing grew {large_net / small_net:.1f}x "
            f"from {self.SIZES[0]:,} to {self.SIZES[1]:,} entries",
        )
        self.assertLess(
            large["append"] * self.SCAN_MARGIN,
            large["scan"],
            "a cached append must be far cheaper than the scan it replaced",
        )


if __name__ == "__main__":
    unittest.main()
