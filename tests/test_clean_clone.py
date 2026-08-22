"""Guard: Bench works from a clean clone, with no chain and no exemption.

The operational ledger is private and project-local. A fresh clone therefore
carries no chain at all, and nothing about that is an error state: `verify`
reports an empty ledger and exits 0, and the first governed edit opens a new
chain at GENESIS.

The self-exemption regression guard lives here too. Bench used to route its
own verdicts to a tracked `ledger/bench-ledger.json` while every other
governed project wrote to `<project>/.bench/`. That exemption is why three
unrelated projects' source ended up published in a public repository. These
tests assert Bench now resolves exactly like any other project, so the
exemption cannot return unnoticed.

Run: python -m unittest tests.test_clean_clone -v
"""

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

_REPO_ROOT: Path = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from cli.commands import cmd_verify  # noqa: E402
from ledger.chain import (  # noqa: E402
    append_entry,
    load_ledger,
    resolve_entries_dir,
    resolve_ledger_path,
)
from utils.project import BENCH_ROOT  # noqa: E402

_PASS_RESULT: dict = {
    "verdict": "PASS",
    "pipeline_error": False,
    "change": {"file": "app.py", "tool": "Write", "diff_summary": {}},
    "challenger": {"status": "CLEAR", "findings": []},
    "defender": {"status": "CONFIRM_CLEAR", "rebuttals": []},
    "oracle": {"verdict": "PASS", "constraint_citations": []},
    "constitution_hash": "f" * 64,
    "constitution_sources": [],
}


class _LedgerEnvTestCase(unittest.TestCase):
    """Isolates BENCH_LEDGER_PATH and cwd so no test touches the real chain."""

    def setUp(self) -> None:
        self._prev_cwd: str = os.getcwd()
        self._prev_override: str | None = os.environ.get("BENCH_LEDGER_PATH")
        self._tmp: str = tempfile.mkdtemp()
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        os.chdir(self._prev_cwd)
        if self._prev_override is None:
            os.environ.pop("BENCH_LEDGER_PATH", None)
        else:
            os.environ["BENCH_LEDGER_PATH"] = self._prev_override
        shutil.rmtree(self._tmp, ignore_errors=True)


class CleanCloneTests(_LedgerEnvTestCase):
    def test_verify_on_clean_clone_is_empty_not_an_error(self) -> None:
        """No chain is a legitimate starting state, not a failure."""
        os.environ["BENCH_LEDGER_PATH"] = str(
            Path(self._tmp) / ".bench" / "bench-ledger.json"
        )
        self.assertEqual(cmd_verify(), 0)

    def test_first_append_creates_genesis(self) -> None:
        target: Path = Path(self._tmp) / ".bench" / "bench-ledger.json"
        os.environ["BENCH_LEDGER_PATH"] = str(target)

        append_entry(dict(_PASS_RESULT))

        entries: list[dict] = load_ledger(str(target))
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["previous_hash"], "GENESIS")

    def test_entries_dir_is_colocated_with_a_fresh_ledger(self) -> None:
        target: Path = Path(self._tmp) / ".bench" / "bench-ledger.json"
        os.environ["BENCH_LEDGER_PATH"] = str(target)
        self.assertEqual(
            Path(resolve_entries_dir()).parent.resolve(),
            target.parent.resolve(),
        )


class NoBenchExemptionTests(_LedgerEnvTestCase):
    """Regression guard: Bench must resolve like any other governed project."""

    def setUp(self) -> None:
        super().setUp()
        os.environ.pop("BENCH_LEDGER_PATH", None)

    def test_bench_repo_root_uses_project_local_ledger(self) -> None:
        os.chdir(str(BENCH_ROOT))
        resolved: Path = Path(resolve_ledger_path())
        self.assertEqual(resolved.parent.name, ".bench")
        self.assertEqual(
            resolved.resolve(),
            (BENCH_ROOT / ".bench" / "bench-ledger.json").resolve(),
        )

    def test_bench_repo_never_resolves_to_tracked_ledger_dir(self) -> None:
        """The old exemption pointed here. It must not come back."""
        os.chdir(str(BENCH_ROOT))
        resolved: Path = Path(resolve_ledger_path()).resolve()
        self.assertNotEqual(
            resolved, (BENCH_ROOT / "ledger" / "bench-ledger.json").resolve()
        )
        self.assertNotEqual(resolved.parent.name, "ledger")

    def test_foreign_project_stays_in_its_own_tree(self) -> None:
        os.chdir(self._tmp)
        resolved: Path = Path(resolve_ledger_path())
        self.assertEqual(resolved.parent.name, ".bench")
        self.assertNotIn(
            str(BENCH_ROOT.resolve()), str(resolved.resolve()),
            "a governed project's verdicts must not land in Bench's tree",
        )

    def test_env_override_still_wins(self) -> None:
        os.chdir(str(BENCH_ROOT))
        os.environ["BENCH_LEDGER_PATH"] = str(Path(self._tmp) / "central.json")
        self.assertEqual(
            Path(resolve_ledger_path()).name, "central.json"
        )


if __name__ == "__main__":
    unittest.main()
