"""Tests for per-project constitution layering (floor + extend).

Bench's core constitution is a floor. A governed project may add constraints in
the reserved ``P-`` namespace and raise a core severity; it may not remove,
downgrade, reword, or shadow a core constraint. Every rejection must raise
rather than silently drop the line, so an author is never left believing they
changed a rule that is still in force.

Run: python -m unittest tests.test_constitution_layers -v
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

from pipeline.constitution import (  # noqa: E402
    ConstitutionFloorError,
    ConstitutionSchemaError,
    load_governing_constitution,
    merge_constitutions,
    resolve_constitution_path,
)
from utils.project import BENCH_ROOT  # noqa: E402

_ENV_VAR: str = "BENCH_CONSTITUTION_PATH"


def _core() -> dict:
    return {
        "constitution": "bench-v1",
        "version": 4,
        "constraints": [
            {
                "id": "C-001",
                "name": "No Silent Error Swallowing",
                "rule": "Catch blocks must log, re-throw, or return.",
                "severity": "veto",
            },
            {
                "id": "C-005",
                "name": "Test Coverage",
                "rule": "New logic carries tests.",
                "severity": "warning",
            },
        ],
    }


def _project(**overrides: object) -> dict:
    layer: dict = {
        "constitution": "myproject-v1",
        "version": 1,
        "constraints": [
            {
                "id": "P-001",
                "name": "No Raw SQL",
                "rule": "Use the query builder.",
                "severity": "veto",
            }
        ],
    }
    layer.update(overrides)
    return layer


def _severities(doc: dict) -> dict:
    return {c["id"]: c["severity"] for c in doc["constraints"]}


class MergeExtendTests(unittest.TestCase):
    def test_project_constraints_are_appended(self) -> None:
        merged: dict = merge_constitutions(_core(), _project())
        ids: list[str] = [c["id"] for c in merged["constraints"]]
        self.assertEqual(ids, ["C-001", "C-005", "P-001"])

    def test_core_constraints_survive_unchanged(self) -> None:
        merged: dict = merge_constitutions(_core(), _project())
        self.assertEqual(_severities(merged)["C-001"], "veto")
        self.assertEqual(_severities(merged)["C-005"], "warning")

    def test_severity_may_be_raised(self) -> None:
        merged: dict = merge_constitutions(
            _core(), _project(severity_overrides={"C-005": "veto"})
        )
        self.assertEqual(_severities(merged)["C-005"], "veto")
        raised = next(
            c for c in merged["constraints"] if c["id"] == "C-005"
        )
        self.assertTrue(raised["severity_raised_by_project"])

    def test_both_versions_are_recorded(self) -> None:
        merged: dict = merge_constitutions(_core(), _project())
        self.assertEqual(merged["version"], 4)
        self.assertEqual(merged["project_version"], 1)
        self.assertIn("bench-v1", merged["constitution"])
        self.assertIn("myproject-v1", merged["constitution"])


class MergeFloorTests(unittest.TestCase):
    """The floor: a project layer must not be able to erode the core."""

    def test_rejects_reusing_a_core_id(self) -> None:
        hostile: dict = _project(constraints=[{
            "id": "C-007",
            "name": "Governance Pipeline Integrity",
            "rule": "Anything goes.",
            "severity": "warning",
        }])
        with self.assertRaises(ConstitutionFloorError) as ctx:
            merge_constitutions(_core(), hostile)
        self.assertIn("C-007", str(ctx.exception))

    def test_rejects_downgrading_a_core_severity(self) -> None:
        with self.assertRaises(ConstitutionFloorError) as ctx:
            merge_constitutions(
                _core(), _project(severity_overrides={"C-001": "warning"})
            )
        self.assertIn("only raise", str(ctx.exception))

    def test_rejects_restating_the_same_severity(self) -> None:
        """Equal is not a raise; restating is how a reword would sneak in."""
        with self.assertRaises(ConstitutionFloorError):
            merge_constitutions(
                _core(), _project(severity_overrides={"C-001": "veto"})
            )

    def test_rejects_override_of_unknown_constraint(self) -> None:
        with self.assertRaises(ConstitutionFloorError) as ctx:
            merge_constitutions(
                _core(), _project(severity_overrides={"C-999": "veto"})
            )
        self.assertIn("C-999", str(ctx.exception))

    def test_omitting_a_core_constraint_does_not_remove_it(self) -> None:
        """A layer listing nothing still inherits the full core floor."""
        merged: dict = merge_constitutions(
            _core(), _project(constraints=[])
        )
        ids: list[str] = [c["id"] for c in merged["constraints"]]
        self.assertIn("C-001", ids)
        self.assertIn("C-005", ids)


class MergeSchemaTests(unittest.TestCase):
    def test_rejects_non_list_constraints(self) -> None:
        with self.assertRaises(ConstitutionSchemaError):
            merge_constitutions(_core(), _project(constraints={"id": "P-001"}))

    def test_rejects_duplicate_project_ids(self) -> None:
        dup: dict = {
            "id": "P-001",
            "name": "n",
            "rule": "r",
            "severity": "veto",
        }
        with self.assertRaises(ConstitutionSchemaError):
            merge_constitutions(_core(), _project(constraints=[dup, dict(dup)]))

    def test_rejects_missing_required_fields(self) -> None:
        with self.assertRaises(ConstitutionSchemaError) as ctx:
            merge_constitutions(
                _core(), _project(constraints=[{"id": "P-002"}])
            )
        self.assertIn("missing required field", str(ctx.exception))

    def test_rejects_unknown_severity(self) -> None:
        with self.assertRaises(ConstitutionSchemaError):
            merge_constitutions(_core(), _project(constraints=[{
                "id": "P-003",
                "name": "n",
                "rule": "r",
                "severity": "advisory",
            }]))

    def test_rejects_non_dict_severity_overrides(self) -> None:
        with self.assertRaises(ConstitutionSchemaError):
            merge_constitutions(_core(), _project(severity_overrides=["C-001"]))


class ResolveTests(unittest.TestCase):
    def setUp(self) -> None:
        self._cwd: str = os.getcwd()
        self.addCleanup(os.chdir, self._cwd)
        self._saved: str | None = os.environ.get(_ENV_VAR)
        self.addCleanup(self._restore_env)

    def _restore_env(self) -> None:
        if self._saved is None:
            os.environ.pop(_ENV_VAR, None)
        else:
            os.environ[_ENV_VAR] = self._saved

    def test_bench_repo_has_no_project_layer(self) -> None:
        os.environ.pop(_ENV_VAR, None)
        os.chdir(BENCH_ROOT)
        self.assertIsNone(resolve_constitution_path())

    def test_foreign_project_without_a_file_has_no_layer(self) -> None:
        os.environ.pop(_ENV_VAR, None)
        tmp: str = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        os.chdir(tmp)
        self.assertIsNone(resolve_constitution_path())

    def test_foreign_project_with_a_file_uses_it(self) -> None:
        os.environ.pop(_ENV_VAR, None)
        tmp: str = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        with open(
            os.path.join(tmp, "bench.json"), "w", encoding="utf-8"
        ) as handle:
            json.dump(_project(), handle)
        os.chdir(tmp)
        self.assertEqual(
            resolve_constitution_path(),
            str(Path(tmp).resolve() / "bench.json"),
        )

    def test_env_override_wins(self) -> None:
        os.environ[_ENV_VAR] = "/custom/layer.json"
        os.chdir(BENCH_ROOT)
        self.assertEqual(resolve_constitution_path(), "/custom/layer.json")


class LoadGoverningTests(unittest.TestCase):
    def setUp(self) -> None:
        self._saved: str | None = os.environ.get(_ENV_VAR)
        self.addCleanup(self._restore_env)
        self._tmp: str = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self._tmp, True)

    def _restore_env(self) -> None:
        if self._saved is None:
            os.environ.pop(_ENV_VAR, None)
        else:
            os.environ[_ENV_VAR] = self._saved

    def _write_layer(self, layer: dict) -> str:
        path: str = os.path.join(self._tmp, "bench.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(layer, handle)
        return path

    def test_core_only_when_no_layer(self) -> None:
        os.environ.pop(_ENV_VAR, None)
        original: str = os.getcwd()
        os.chdir(BENCH_ROOT)
        try:
            doc, digest, sources = load_governing_constitution()
        finally:
            os.chdir(original)

        self.assertEqual(len(sources), 1)
        self.assertEqual(sources[0]["layer"], "core")
        self.assertEqual(digest, sources[0]["sha256"])
        self.assertTrue(
            all(c["id"].startswith("C-") for c in doc["constraints"])
        )

    def test_layer_is_merged_and_both_sources_recorded(self) -> None:
        os.environ[_ENV_VAR] = self._write_layer(_project())
        doc, digest, sources = load_governing_constitution()

        self.assertEqual([s["layer"] for s in sources], ["core", "project"])
        ids: list[str] = [c["id"] for c in doc["constraints"]]
        self.assertIn("P-001", ids)
        self.assertIn("C-007", ids)  # real core constraint still in force
        self.assertNotEqual(digest, sources[0]["sha256"])
        self.assertEqual(len(digest), 64)

    def test_hash_changes_when_the_layer_changes(self) -> None:
        os.environ[_ENV_VAR] = self._write_layer(_project())
        _, first, _ = load_governing_constitution()

        altered: dict = _project()
        altered["constraints"][0]["rule"] = "Use the query builder, always."
        os.environ[_ENV_VAR] = self._write_layer(altered)
        _, second, _ = load_governing_constitution()

        self.assertNotEqual(first, second)

    def test_malformed_layer_fails_closed(self) -> None:
        path: str = os.path.join(self._tmp, "bench.json")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("{not json")
        os.environ[_ENV_VAR] = path

        from pipeline.constitution import ConstitutionError

        with self.assertRaises(ConstitutionError):
            load_governing_constitution()

    def test_hostile_layer_fails_closed(self) -> None:
        os.environ[_ENV_VAR] = self._write_layer(
            _project(severity_overrides={"C-007": "warning"})
        )
        with self.assertRaises(ConstitutionFloorError):
            load_governing_constitution()


if __name__ == "__main__":
    unittest.main()
