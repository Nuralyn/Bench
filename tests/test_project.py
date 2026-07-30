"""Tests for utils.project — which project a governed run belongs to.

Ledger routing, out-of-project classification, and constitution resolution all
resolve through project_root(). If they could disagree, a change could be
judged against one project's constitution while being recorded in another
project's ledger, so every branch here is covered deliberately.

Run: python -m unittest tests.test_project -v
"""

import io
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_REPO_ROOT: Path = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from utils.project import (  # noqa: E402
    BENCH_ROOT,
    governs_bench_itself,
    project_root,
)


class ProjectRootTests(unittest.TestCase):
    def setUp(self) -> None:
        self._original_cwd: str = os.getcwd()
        self.addCleanup(os.chdir, self._original_cwd)

    def test_bench_root_itself_is_in_project(self) -> None:
        os.chdir(BENCH_ROOT)
        self.assertEqual(project_root(), BENCH_ROOT)
        self.assertTrue(governs_bench_itself())

    def test_subdirectory_of_bench_is_still_bench(self) -> None:
        """Editing utils/api.py while sitting in tests/ is still in-project."""
        os.chdir(BENCH_ROOT / "tests")
        self.assertEqual(project_root(), BENCH_ROOT)
        self.assertTrue(governs_bench_itself())

    def test_unrelated_directory_is_its_own_project(self) -> None:
        tmp: str = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        os.chdir(tmp)

        root: Path = project_root()
        self.assertEqual(root, Path(tmp).resolve())
        self.assertNotEqual(root, BENCH_ROOT)
        self.assertFalse(governs_bench_itself())

    def test_unreadable_cwd_falls_back_to_bench_root_loudly(self) -> None:
        """C-001: the failure is reported, not swallowed."""
        stderr: io.StringIO = io.StringIO()
        with patch("utils.project.Path.cwd", side_effect=OSError("gone")), \
             patch.object(sys, "stderr", stderr):
            root: Path = project_root()

        self.assertEqual(root, BENCH_ROOT)
        self.assertIn("cannot resolve working directory", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
