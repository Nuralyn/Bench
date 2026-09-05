"""Guard: every subprocess call in the source tree carries a timeout.

Bench's pitch is that governance never stalls. The provider call has had a
timeout since the claude_code path landed, but the git and gh probes in the
CLI and the migration helper did not, so a hung remote or credential helper
could hang the command forever. This test walks every source module with
the ast module and fails on a ``subprocess.run`` (or ``check_output``,
``check_call``, ``call``) that has no ``timeout`` keyword, and on any
``subprocess.Popen`` at all, since a Popen's timeout lives on a later
``communicate`` call this scan cannot pair with it.

Proof that the gate can fail: ``_calls_without_timeout`` is also run in a
test against the three call sites as they stood before roadmap item 2.4,
reconstructed inline, and must report all three.

Run: python -m unittest tests.test_subprocess_timeouts -v
"""

import ast
import sys
import unittest
from pathlib import Path

_REPO_ROOT: Path = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# Every package the pipeline, the CLI, and the hook are made of. tests/ is
# excluded on purpose: a test may spawn a process to prove a timeout fires.
_SOURCE_DIRS: tuple[str, ...] = ("cli", "hooks", "ledger", "pipeline", "utils")
_TIMED_CALLS: frozenset[str] = frozenset({"run", "check_output", "check_call", "call"})


def _subprocess_names(tree: ast.AST) -> tuple[set[str], dict[str, str]]:
    """Every name the module binds to subprocess, or to one of its calls.

    ``import subprocess as sp`` and ``from subprocess import run as go`` are
    both ways a call could dodge a scan that only knows the literal spelling,
    so aliases are resolved from the module's own import statements.
    Returns (module aliases, {local name: subprocess function name}).
    """
    modules: set[str] = set()
    functions: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "subprocess":
                    modules.add(alias.asname or alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module == "subprocess":
            for alias in node.names:
                functions[alias.asname or alias.name] = alias.name
    return modules, functions


def _called_name(
    func: ast.AST, modules: set[str], functions: dict[str, str]
) -> str | None:
    """The subprocess function a call targets, or None if it is not one."""
    if (
        isinstance(func, ast.Attribute)
        and isinstance(func.value, ast.Name)
        and func.value.id in modules
    ):
        return func.attr
    if isinstance(func, ast.Name) and func.id in functions:
        return functions[func.id]
    return None


def _calls_without_timeout(source: str, label: str) -> list[str]:
    """Names each subprocess call in ``source`` that carries no timeout."""
    tree: ast.AST = ast.parse(source, filename=label)
    modules, functions = _subprocess_names(tree)
    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name: str | None = _called_name(node.func, modules, functions)
        if name == "Popen":
            offenders.append(f"{label}:{node.lineno} subprocess.Popen")
        elif name in _TIMED_CALLS and not any(
            kw.arg == "timeout" for kw in node.keywords
        ):
            offenders.append(f"{label}:{node.lineno} subprocess.{name}")
    return offenders


def _source_files() -> list[Path]:
    files: list[Path] = []
    for directory in _SOURCE_DIRS:
        files.extend(sorted((_REPO_ROOT / directory).rglob("*.py")))
    return files


class SubprocessTimeoutTests(unittest.TestCase):
    def test_every_subprocess_call_in_the_tree_has_a_timeout(self) -> None:
        offenders: list[str] = []
        for path in _source_files():
            label: str = path.relative_to(_REPO_ROOT).as_posix()
            offenders.extend(
                _calls_without_timeout(path.read_text(encoding="utf-8"), label)
            )
        self.assertEqual(
            offenders,
            [],
            "subprocess calls without a timeout (a hung child would hang "
            "Bench): " + ", ".join(offenders),
        )

    def test_the_scan_covers_every_source_package(self) -> None:
        # The provider call in utils/api.py is the one subprocess that has
        # always had a timeout; if the scan cannot see it, it sees nothing.
        api: Path = _REPO_ROOT / "utils" / "api.py"
        self.assertIn(api, _source_files())
        tree = ast.parse(api.read_text(encoding="utf-8"))
        modules, functions = _subprocess_names(tree)
        timed: int = sum(
            1
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and _called_name(node.func, modules, functions) == "run"
        )
        self.assertGreaterEqual(timed, 1)

    def test_the_gate_reports_the_three_sites_it_was_written_for(self) -> None:
        before: str = (
            "import subprocess\n"
            "def _git_refs(args):\n"
            "    proc = subprocess.run(args, capture_output=True, text=True)\n"
            "def _gh_status(endpoint):\n"
            "    proc = subprocess.run(['gh', 'api', '-i', endpoint], text=True)\n"
            "def _run_git(args, cwd):\n"
            "    result = subprocess.run(['git', *args], cwd=str(cwd), text=True)\n"
        )
        self.assertEqual(
            _calls_without_timeout(before, "before.py"),
            [
                "before.py:3 subprocess.run",
                "before.py:5 subprocess.run",
                "before.py:7 subprocess.run",
            ],
        )

    def test_the_gate_accepts_a_timeout_and_rejects_popen(self) -> None:
        after: str = (
            "import subprocess\n"
            "subprocess.run(['git'], timeout=60)\n"
            "subprocess.check_output(['gh'], timeout=5.0)\n"
            "subprocess.Popen(['claude'])\n"
        )
        self.assertEqual(
            _calls_without_timeout(after, "after.py"), ["after.py:4 subprocess.Popen"]
        )

    def test_the_gate_sees_through_import_aliases(self) -> None:
        aliased: str = (
            "import subprocess as sp\n"
            "from subprocess import run as go, check_output, Popen as P\n"
            "sp.run(['git'])\n"
            "go(['git'])\n"
            "check_output(['git'], timeout=1)\n"
            "P(['git'])\n"
            "sp.run(['git'], timeout=2)\n"
        )
        self.assertEqual(
            _calls_without_timeout(aliased, "aliased.py"),
            [
                "aliased.py:3 subprocess.run",
                "aliased.py:4 subprocess.run",
                "aliased.py:6 subprocess.Popen",
            ],
        )

    def test_the_gate_ignores_unrelated_names(self) -> None:
        # A local function called run, or another module's Popen, is not a
        # subprocess call and must not trip the scan.
        unrelated: str = (
            "import multiprocessing as mp\n"
            "def run(x):\n"
            "    return x\n"
            "run(1)\n"
            "mp.Pool(2)\n"
        )
        self.assertEqual(_calls_without_timeout(unrelated, "unrelated.py"), [])


if __name__ == "__main__":
    unittest.main()
