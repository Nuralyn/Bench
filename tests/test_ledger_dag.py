"""Tests for the parallel-branch ledger: forks, merges, and self-healing.

A single JSON array rewritten on every append gave two branches divergent
chains that could not be merged. Entries now live one per file named by their
hash, and ``previous_hash`` names every current tip, so a git merge is
conflict-free and the next governed edit reconciles the fork.

Run: python -m unittest tests.test_ledger_dag -v
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

import ledger.verify as verify_module  # noqa: E402
from ledger.chain import (  # noqa: E402
    ENTRIES_DIRNAME,
    LedgerReadError,
    append_entry,
    compute_entry_hash,
    compute_tips,
    load_ledger,
    resolve_entries_dir,
)
from ledger.verify import verify_chain  # noqa: E402
from tests._ledger_fixtures import build_valid_chain  # noqa: E402


def _result(file_ref: str = "app/main.py") -> dict:
    return {
        "verdict": "PASS",
        "constitution_hash": "abc123",
        "change": {"file": file_ref, "tool": "Write", "diff_summary": {}},
    }


class LedgerDagTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp: str = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self._tmp, True)
        self._ledger: str = os.path.join(self._tmp, "bench-ledger.json")
        self._entries: Path = Path(resolve_entries_dir(self._ledger))

    def _write_entry(self, entry: dict) -> Path:
        self._entries.mkdir(parents=True, exist_ok=True)
        target: Path = self._entries / f"{entry['entry_hash']}.json"
        target.write_text(json.dumps(entry, indent=2), encoding="utf-8")
        return target

    # --- the property this change exists for -------------------------------

    def test_two_branches_fork_then_the_next_append_heals_it(self) -> None:
        base: dict = append_entry(_result("base.py"), path=self._ledger)

        # Two branches, each appending from the same base. In git these are two
        # new files, so the merge is a union with no conflict.
        branch_a: dict = append_entry(_result("a.py"), path=self._ledger)
        os.remove(self._entries / f"{branch_a['entry_hash']}.json")
        branch_b: dict = append_entry(_result("b.py"), path=self._ledger)
        self._write_entry(branch_a)

        merged: list[dict] = load_ledger(self._ledger)
        tips: list[str] = compute_tips(merged)
        self.assertEqual(len(merged), 3)
        self.assertEqual(
            tips, sorted([branch_a["entry_hash"], branch_b["entry_hash"]])
        )

        result: dict = verify_chain(self._ledger)
        self.assertTrue(result["valid"], result.get("message"))
        self.assertEqual(sorted(result["tips"]), tips)

        # The next governed edit names both tips and the fork is gone.
        healed: dict = append_entry(_result("next.py"), path=self._ledger)
        self.assertEqual(healed["previous_hash"], tips)

        after: dict = verify_chain(self._ledger)
        self.assertTrue(after["valid"], after.get("message"))
        self.assertEqual(after["tips"], [healed["entry_hash"]])
        self.assertEqual(after["latest_hash"], healed["entry_hash"])
        self.assertEqual(base["previous_hash"], "GENESIS")

    # --- the union of a frozen array and per-entry files --------------------

    def test_legacy_array_and_entry_files_verify_together(self) -> None:
        seeded: list[dict] = build_valid_chain(3)
        Path(self._ledger).write_text(
            json.dumps(seeded, indent=2), encoding="utf-8"
        )
        appended: dict = append_entry(_result(), path=self._ledger)

        entries: list[dict] = load_ledger(self._ledger)
        self.assertEqual(len(entries), 4)
        # Legacy entries keep their stored order and lead.
        self.assertEqual(
            [e["entry_hash"] for e in entries[:3]],
            [e["entry_hash"] for e in seeded],
        )
        self.assertEqual(entries[3]["entry_hash"], appended["entry_hash"])

        result: dict = verify_chain(self._ledger)
        self.assertTrue(result["valid"], result.get("message"))
        self.assertEqual(result["entries"], 4)
        self.assertEqual(result["tips"], [appended["entry_hash"]])

    def test_entry_files_alone_verify_without_a_legacy_array(self) -> None:
        append_entry(_result("one.py"), path=self._ledger)
        append_entry(_result("two.py"), path=self._ledger)

        self.assertFalse(os.path.exists(self._ledger))
        result: dict = verify_chain(self._ledger)
        self.assertTrue(result["valid"], result.get("message"))
        self.assertEqual(result["entries"], 2)

    def test_load_ledger_order_is_deterministic(self) -> None:
        for name in ("a.py", "b.py", "c.py", "d.py"):
            append_entry(_result(name), path=self._ledger)

        first: list[str] = [e["entry_hash"] for e in load_ledger(self._ledger)]
        second: list[str] = [e["entry_hash"] for e in load_ledger(self._ledger)]
        self.assertEqual(first, second)
        self.assertEqual(len(first), 4)

    # --- fail-closed detection ---------------------------------------------

    def test_missing_parent_is_detected(self) -> None:
        append_entry(_result("one.py"), path=self._ledger)
        middle: dict = append_entry(_result("two.py"), path=self._ledger)
        append_entry(_result("three.py"), path=self._ledger)

        os.remove(self._entries / f"{middle['entry_hash']}.json")

        result: dict = verify_chain(self._ledger)
        self.assertFalse(result["valid"])
        self.assertEqual(result["failure_type"], "MISSING_PARENT")

    def test_duplicate_entry_is_detected(self) -> None:
        entry: dict = append_entry(_result(), path=self._ledger)
        copy: Path = self._entries / "copy.json"
        copy.write_text(json.dumps(entry, indent=2), encoding="utf-8")

        result: dict = verify_chain(self._ledger)
        self.assertFalse(result["valid"])
        # The renamed copy trips the filename check before the duplicate check.
        self.assertIn(
            result["failure_type"], ("FILENAME_MISMATCH", "DUPLICATE_ENTRY")
        )

    def test_filename_must_match_the_hash_it_contains(self) -> None:
        entry: dict = append_entry(_result(), path=self._ledger)
        original: Path = self._entries / f"{entry['entry_hash']}.json"
        original.rename(self._entries / "0000000000.json")

        result: dict = verify_chain(self._ledger)
        self.assertFalse(result["valid"])
        self.assertEqual(result["failure_type"], "FILENAME_MISMATCH")

    def test_tampered_entry_file_is_detected(self) -> None:
        entry: dict = append_entry(_result(), path=self._ledger)
        target: Path = self._entries / f"{entry['entry_hash']}.json"
        tampered: dict = json.loads(target.read_text(encoding="utf-8"))
        tampered["verdict"] = "VETO"
        target.write_text(json.dumps(tampered, indent=2), encoding="utf-8")

        result: dict = verify_chain(self._ledger)
        self.assertFalse(result["valid"])
        self.assertEqual(result["failure_type"], "HASH_MISMATCH")

    def test_multiple_genesis_is_rejected(self) -> None:
        append_entry(_result("one.py"), path=self._ledger)
        rogue: dict = build_valid_chain(1)[0]
        self._write_entry(rogue)

        result: dict = verify_chain(self._ledger)
        self.assertFalse(result["valid"])
        self.assertEqual(result["failure_type"], "MULTIPLE_GENESIS")

    def test_orphan_subtree_is_rejected(self) -> None:
        append_entry(_result("one.py"), path=self._ledger)
        orphan: dict = build_valid_chain(2)[1]  # parent is not present
        self._write_entry(orphan)

        result: dict = verify_chain(self._ledger)
        self.assertFalse(result["valid"])
        self.assertIn(
            result["failure_type"], ("MISSING_PARENT", "ORPHAN_ENTRY")
        )


class MalformedLinkTests(unittest.TestCase):
    """A link that names nothing, or names a non-string, is not a valid link."""

    def setUp(self) -> None:
        self._tmp: str = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self._tmp, True)
        self._ledger: str = os.path.join(self._tmp, "bench-ledger.json")
        self._entries: Path = Path(resolve_entries_dir(self._ledger))

    def _rewrite(self, entry: dict, previous_hash: object) -> None:
        """Replace an entry's link and re-file it under its new hash."""
        (self._entries / f"{entry['entry_hash']}.json").unlink()
        entry = dict(entry)
        entry["previous_hash"] = previous_hash
        entry["entry_hash"] = compute_entry_hash(entry)
        (self._entries / f"{entry['entry_hash']}.json").write_text(
            json.dumps(entry, indent=2), encoding="utf-8"
        )

    def test_empty_parent_list_is_not_a_genesis(self) -> None:
        entry: dict = append_entry(_result(), path=self._ledger)
        self._rewrite(entry, [])

        result: dict = verify_chain(self._ledger)
        self.assertFalse(result["valid"])
        self.assertEqual(result["failure_type"], "SCHEMA_ERROR")

    def test_non_string_parent_element_is_rejected(self) -> None:
        first: dict = append_entry(_result("one.py"), path=self._ledger)
        second: dict = append_entry(_result("two.py"), path=self._ledger)
        self._rewrite(second, [first["entry_hash"], 123])

        result: dict = verify_chain(self._ledger)
        self.assertFalse(result["valid"])
        self.assertEqual(result["failure_type"], "SCHEMA_ERROR")

    def test_empty_string_parent_is_rejected(self) -> None:
        entry: dict = append_entry(_result(), path=self._ledger)
        self._rewrite(entry, "")

        result: dict = verify_chain(self._ledger)
        self.assertFalse(result["valid"])
        self.assertEqual(result["failure_type"], "SCHEMA_ERROR")


class StrictWritePathTests(unittest.TestCase):
    """An append must not build on entry files it has not validated."""

    def setUp(self) -> None:
        self._tmp: str = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self._tmp, True)
        self._ledger: str = os.path.join(self._tmp, "bench-ledger.json")
        self._entries: Path = Path(resolve_entries_dir(self._ledger))
        self._entries.mkdir(parents=True, exist_ok=True)

    def test_empty_object_entry_file_blocks_the_append(self) -> None:
        """The corruption path: `{}` yields no tips, so the next entry would
        have been written with an empty parent list and become a second root."""
        (self._entries / "junk.json").write_text("{}", encoding="utf-8")
        result: dict = _result()

        with self.assertRaises(LedgerReadError):
            append_entry(result, path=self._ledger)

    def test_hash_mismatch_in_an_existing_entry_blocks_the_append(self) -> None:
        entry: dict = append_entry(_result(), path=self._ledger)
        target: Path = self._entries / f"{entry['entry_hash']}.json"
        tampered: dict = json.loads(target.read_text(encoding="utf-8"))
        tampered["verdict"] = "VETO"
        target.write_text(json.dumps(tampered, indent=2), encoding="utf-8")
        result: dict = _result()

        with self.assertRaises(LedgerReadError):
            append_entry(result, path=self._ledger)

    def test_misnamed_entry_file_blocks_the_append(self) -> None:
        entry: dict = append_entry(_result(), path=self._ledger)
        (self._entries / f"{entry['entry_hash']}.json").rename(
            self._entries / "wrong-name.json"
        )
        result: dict = _result()

        with self.assertRaises(LedgerReadError):
            append_entry(result, path=self._ledger)


class SummaryEndpointTests(unittest.TestCase):
    """Endpoints must come from the graph, not from iteration order."""

    def setUp(self) -> None:
        self._tmp: str = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self._tmp, True)
        self._ledger: str = os.path.join(self._tmp, "bench-ledger.json")

    def test_genesis_and_latest_are_the_real_endpoints(self) -> None:
        first: dict = append_entry(_result("one.py"), path=self._ledger)
        append_entry(_result("two.py"), path=self._ledger)
        last: dict = append_entry(_result("three.py"), path=self._ledger)

        result: dict = verify_chain(self._ledger)
        self.assertTrue(result["valid"], result.get("message"))
        # Filename order is hash order, so these only agree if the endpoints
        # are derived from the DAG rather than from insertion order.
        self.assertEqual(result["genesis_hash"], first["entry_hash"])
        self.assertEqual(result["latest_hash"], last["entry_hash"])
        self.assertEqual(result["first_entry"], first["timestamp"])
        self.assertEqual(result["last_entry"], last["timestamp"])


class ConstantAgreementTests(unittest.TestCase):
    def test_writer_and_auditor_agree_on_the_entries_dirname(self) -> None:
        """verify.py re-declares the name rather than importing it.

        The duplication is deliberate — the auditor must not inherit the
        writer's definitions — so this test is what keeps the two in step.
        """
        self.assertEqual(ENTRIES_DIRNAME, verify_module._ENTRIES_DIRNAME)


if __name__ == "__main__":
    unittest.main()
