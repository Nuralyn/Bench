"""Tests for the parallel-branch ledger: forks, merges, and self-healing.

A single JSON array rewritten on every append gave two branches divergent
chains that could not be merged. Entries now live one per file named by their
hash, and ``previous_hash`` names every current tip, so a git merge is
conflict-free and the next governed edit reconciles the fork.

Run: python -m unittest tests.test_ledger_dag -v
"""

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import traceback
import unittest
from pathlib import Path
from unittest.mock import patch

_REPO_ROOT: Path = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import ledger.verify as verify_module  # noqa: E402
from ledger.chain import (  # noqa: E402
    ENTRIES_DIRNAME,
    LOCK_FILENAME,
    LedgerReadError,
    _append_lock,
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


# A real second process, not a thread: the lock is a file lock, and the race
# it closes is between two Claude Code sessions on one machine.
_APPENDER: str = textwrap.dedent(
    """
    import sys
    sys.path.insert(0, sys.argv[1])
    from ledger.chain import append_entry
    for index in range(int(sys.argv[4])):
        append_entry(
            {
                "verdict": "PASS",
                "constitution_hash": "abc123",
                "change": {
                    "file": f"{sys.argv[3]}-{index}.py",
                    "tool": "Write",
                    "diff_summary": {},
                },
            },
            path=sys.argv[2],
        )
    """
)


# Holds the chain's lock from a second process until told to let go.
_HOLDER: str = textwrap.dedent(
    """
    import sys
    import time
    from pathlib import Path
    sys.path.insert(0, sys.argv[1])
    from ledger.chain import _append_lock
    held, release = Path(sys.argv[3]), Path(sys.argv[4])
    with _append_lock(Path(sys.argv[2])):
        held.write_text("x", encoding="utf-8")
        deadline = time.monotonic() + 30
        while not release.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
    """
)


class AppendLockTests(unittest.TestCase):
    """Two sessions appending to one chain at once must not fork it.

    Without the lock, both read the same tips and both write, and the chain
    ends with two tips that the next append has to reconcile. With it, the
    appends serialise: one tip, every entry present, and every append after
    the first finds the cache the one before it wrote.
    """

    def setUp(self) -> None:
        self._tmp: str = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self._tmp, True)
        self._ledger: str = os.path.join(self._tmp, "bench-ledger.json")
        self._dir: Path = Path(self._tmp)

    def test_two_processes_appending_fifty_each_end_with_one_tip(
        self,
    ) -> None:
        procs: list[subprocess.Popen[str]] = [
            subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    _APPENDER,
                    str(_REPO_ROOT),
                    self._ledger,
                    tag,
                    "50",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for tag in ("a", "b")
        ]
        outputs: list[tuple[str, str]] = [
            proc.communicate(timeout=180) for proc in procs
        ]
        for proc, (_, err) in zip(procs, outputs, strict=True):
            self.assertEqual(proc.returncode, 0, err)

        result: dict = verify_chain(self._ledger)
        self.assertTrue(result["valid"], result.get("message"))
        self.assertEqual(result["entries"], 100)
        self.assertEqual(len(result["tips"]), 1)
        # Serialised appends never see a stale cache: the listing and the
        # cache are always read together under the lock.
        for _, err in outputs:
            self.assertNotIn("rescanning", err)
        self.assertTrue((self._dir / LOCK_FILENAME).is_file())

    def _start_holder(self) -> tuple[subprocess.Popen[str], Path]:
        """Start a process holding the chain's lock; return it and the file
        whose creation tells it to let go. Blocks until the lock is held."""
        held: Path = self._dir / "held"
        release: Path = self._dir / "release"
        holder: subprocess.Popen[str] = subprocess.Popen(
            [
                sys.executable,
                "-c",
                _HOLDER,
                str(_REPO_ROOT),
                str(self._dir),
                str(held),
                str(release),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        deadline: float = time.monotonic() + 30
        while not held.exists():
            if holder.poll() is not None or time.monotonic() >= deadline:
                release.write_text("x", encoding="utf-8")
                _, err = holder.communicate(timeout=60)
                self.fail(f"holder never locked: {err}")
            time.sleep(0.01)
        return holder, release

    def test_an_append_waits_for_a_lock_held_by_another_process(self) -> None:
        """A governed edit's receipt is delayed, not lost. While it waits it
        says so on stderr at the heartbeat interval, naming the lock."""
        holder, release = self._start_holder()
        try:
            def release_soon() -> None:
                time.sleep(0.6)
                release.write_text("x", encoding="utf-8")

            releaser: threading.Thread = threading.Thread(target=release_soon)
            releaser.start()
            stderr: io.StringIO = io.StringIO()
            started: float = time.monotonic()
            with (
                patch("ledger.chain._LOCK_HEARTBEAT_SECONDS", 0.1),
                contextlib.redirect_stderr(stderr),
            ):
                entry: dict = append_entry(_result(), path=self._ledger)
            waited: float = time.monotonic() - started
            releaser.join(timeout=30)
        finally:
            release.write_text("x", encoding="utf-8")
            _, err = holder.communicate(timeout=60)
        self.assertEqual(holder.returncode, 0, err)

        self.assertGreaterEqual(waited, 0.4, "the append did not wait")
        self.assertEqual(entry["previous_hash"], "GENESIS")
        self.assertIn("waiting for the append lock", stderr.getvalue())
        self.assertIn(LOCK_FILENAME, stderr.getvalue())

    def test_a_bounded_acquisition_is_refused_while_another_process_holds_it(
        self,
    ) -> None:
        """The bounded form retirement uses: fail closed, nothing written."""
        holder, release = self._start_holder()
        try:
            with self.assertRaises(LedgerReadError) as caught:
                with _append_lock(self._dir, timeout=0.2):
                    self.fail("acquired a lock another process holds")
        finally:
            release.write_text("x", encoding="utf-8")
            _, err = holder.communicate(timeout=60)
        self.assertEqual(holder.returncode, 0, err)
        self.assertIn("not acquired within 0.2s", str(caught.exception))

    def test_another_thread_waits_rather_than_re_entering(self) -> None:
        """Re-entry is per thread, so a second thread waits like a process."""
        outcome: dict[str, object] = {}

        def try_append() -> None:
            started: float = time.monotonic()
            outcome["entry"] = append_entry(_result(), path=self._ledger)
            outcome["waited"] = time.monotonic() - started

        with _append_lock(self._dir):
            worker: threading.Thread = threading.Thread(target=try_append)
            worker.start()
            time.sleep(0.5)
            # Still held here, so the worker must not have got through.
            self.assertNotIn("entry", outcome)
        worker.join(timeout=30)
        self.assertFalse(worker.is_alive())

        self.assertIn("entry", outcome)
        self.assertGreaterEqual(float(str(outcome["waited"])), 0.4)

    @unittest.skipUnless(hasattr(os, "fork"), "fork is POSIX only")
    def test_a_forked_child_does_not_inherit_the_right_to_re_enter(
        self,
    ) -> None:
        """The child shares the holder's descriptor, and with it the OS lock,
        but it must not share the holder's re-entrancy: its first append has
        to contend for the file like any other process, and be refused while
        the parent holds it."""
        with _append_lock(self._dir):
            pid: int = os.fork()
            if pid == 0:
                code: int = 0
                try:
                    with _append_lock(self._dir, timeout=0.2):
                        code = 5  # re-entered: the defect this test exists for
                except LedgerReadError:
                    code = 3
                except BaseException:
                    traceback.print_exc()
                    code = 4
                os._exit(code)
            _, status = os.waitpid(pid, 0)

        self.assertEqual(os.waitstatus_to_exitcode(status), 3)

    def test_the_lock_is_re_entrant_within_a_thread(self) -> None:
        """Retirement holds the lock and appends the anchor inside it."""
        with _append_lock(self._dir):
            entry: dict = append_entry(_result(), path=self._ledger)
        self.assertEqual(entry["previous_hash"], "GENESIS")

        # And the outermost exit released it: a bounded acquisition, which
        # would be refused if the lock were still held, goes straight in.
        with _append_lock(self._dir, timeout=0.2):
            second: dict = append_entry(_result("two.py"), path=self._ledger)
        self.assertEqual(second["previous_hash"], [entry["entry_hash"]])

    def test_the_lock_is_released_after_a_refused_append(self) -> None:
        Path(self._ledger).write_text("{not json", encoding="utf-8")
        with self.assertRaises(LedgerReadError):
            append_entry(_result(), path=self._ledger)
        os.remove(self._ledger)

        # A lock left held by the refusal would make this bounded
        # acquisition raise instead of going straight in.
        with _append_lock(self._dir, timeout=0.2):
            entry: dict = append_entry(_result(), path=self._ledger)

        self.assertEqual(entry["previous_hash"], "GENESIS")

    def test_the_lock_file_is_empty_and_survives_appends(self) -> None:
        append_entry(_result("one.py"), path=self._ledger)
        append_entry(_result("two.py"), path=self._ledger)

        lock: Path = self._dir / LOCK_FILENAME
        self.assertTrue(lock.is_file())
        self.assertEqual(lock.stat().st_size, 0)


if __name__ == "__main__":
    unittest.main()
