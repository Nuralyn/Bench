"""Tests that pin the packaging metadata to the code and files it describes.

pyproject.toml is prose to the test suite unless something reads it. These
tests make its claims fail instead: the dependency list must equal
requirements.txt, every listed package must import, the console script must
resolve to a callable, and the README's Quick Start must document the install
path the metadata provides.

Run: python -m unittest tests.test_packaging -v
"""

import importlib
import re
import sys
import tomllib
import unittest
from pathlib import Path

_REPO_ROOT: Path = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def _pyproject() -> dict:
    return tomllib.loads((_REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def _requirement_lines(path: Path) -> list[str]:
    lines: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line: str = raw.strip()
        if line and not line.startswith("#"):
            lines.append(line)
    return lines


class TestProjectMetadata(unittest.TestCase):
    def setUp(self) -> None:
        self.data: dict = _pyproject()

    def test_dependencies_mirror_requirements_txt(self) -> None:
        self.assertEqual(
            self.data["project"]["dependencies"],
            _requirement_lines(_REPO_ROOT / "requirements.txt"),
        )

    def test_requires_python_matches_the_documented_floor(self) -> None:
        self.assertEqual(self.data["project"]["requires-python"], ">=3.11")

    def test_every_declared_package_imports(self) -> None:
        packages: list[str] = self.data["tool"]["setuptools"]["packages"]
        self.assertEqual(sorted(packages), ["cli", "hooks", "ledger", "pipeline", "utils"])
        for name in packages:
            with self.subTest(package=name):
                self.assertTrue((_REPO_ROOT / name).is_dir())
                importlib.import_module(name)

    def test_console_script_resolves_to_a_callable(self) -> None:
        target: str = self.data["project"]["scripts"]["bench"]
        module_name, _, attribute = target.partition(":")
        module = importlib.import_module(module_name)
        self.assertTrue(callable(getattr(module, attribute)))

    def test_runtime_resources_are_package_data_that_exists(self) -> None:
        # A wheel is complete only if the constitution, the hook script, and
        # the guard travel inside packages. The hook script ships as a module
        # file of hooks/; the other two must be declared as package data.
        package_data: dict = self.data["tool"]["setuptools"]["package-data"]
        self.assertEqual(package_data, {"pipeline": ["bench.json"], "hooks": ["pre-commit"]})
        for relpath in ("pipeline/bench.json", "hooks/pre-commit", "hooks/pre-tool-use.py", "hooks/__init__.py"):
            with self.subTest(file=relpath):
                self.assertTrue((_REPO_ROOT / relpath).is_file())

    def test_the_constitution_has_exactly_one_home(self) -> None:
        # A stale copy at the old root would be loaded by nothing and edited
        # by mistake.
        self.assertFalse((_REPO_ROOT / "bench.json").exists())

    def test_the_guard_shim_forwards_to_the_packaged_guard(self) -> None:
        shim: str = (_REPO_ROOT / "scripts" / "githooks" / "pre-commit").read_text(encoding="utf-8")
        self.assertIn("hooks/pre-commit", shim)
        self.assertTrue(shim.startswith("#!/bin/sh"))


class TestReadmeQuickStart(unittest.TestCase):
    def setUp(self) -> None:
        text: str = (_REPO_ROOT / "README.md").read_text(encoding="utf-8")
        match = re.search(
            r"^## Quick Start\n(.*?)(?=^## |\Z)", text, flags=re.MULTILINE | re.DOTALL
        )
        if match is None:
            raise AssertionError("README has no '## Quick Start' section")
        self.section: str = match.group(1)

    def test_documents_the_editable_install(self) -> None:
        self.assertIn("pip install -e .", self.section)
        self.assertNotIn("pip install -r requirements.txt", self.section)

    def test_documents_install_and_uninstall(self) -> None:
        self.assertIn("bench install --project", self.section)
        self.assertIn("bench uninstall --project", self.section)

    def test_template_placeholders_are_both_named(self) -> None:
        template: str = (_REPO_ROOT / ".claude" / "settings.template.json").read_text(
            encoding="utf-8"
        )
        self.assertIn("/absolute/path/to/python", template)
        self.assertIn("/absolute/path/to/bench/hooks/pre-tool-use.py", template)
        self.assertIn("both placeholders", self.section)


if __name__ == "__main__":
    unittest.main()
