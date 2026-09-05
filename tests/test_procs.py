"""run_isolated ends a timed-out child's descendants, not only the child.

A child that spawns a grandchild and then hangs stands in for git
launching ssh or a credential helper. After the timeout the grandchild
must be gone too; with plain subprocess.run it would sleep on.

Run: python -m unittest tests.test_procs -v
"""

import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

_REPO_ROOT: Path = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from utils.procs import run_isolated  # noqa: E402

_GRANDCHILD: str = "import time; time.sleep(60)"
# Starts the grandchild, records its pid where the test can read it, hangs.
_CHILD: str = (
    "import subprocess, sys, time\n"
    "grandchild = subprocess.Popen([sys.executable, '-c', sys.argv[2]])\n"
    "open(sys.argv[1], 'w').write(str(grandchild.pid))\n"
    "time.sleep(60)\n"
)


def _alive(pid: int) -> bool:
    """Whether a process with this pid still exists, on either platform."""
    if sys.platform == "win32":
        listing: str = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        ).stdout
        return str(pid) in listing
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    stat: Path = Path(f"/proc/{pid}/stat")
    if stat.exists():
        # A zombie runs nothing. Where PID 1 does not reap adopted children
        # (some minimal containers) a killed grandchild stays listed as one.
        state: str = stat.read_text(encoding="utf-8").rsplit(")", 1)[-1].split()[0]
        return state != "Z"
    return True


class RunIsolatedTests(unittest.TestCase):
    def test_a_timed_out_child_takes_its_descendants_with_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pid_file: Path = Path(tmp) / "grandchild.pid"
            with self.assertRaises(subprocess.TimeoutExpired):
                run_isolated(
                    [sys.executable, "-c", _CHILD, str(pid_file), _GRANDCHILD],
                    timeout=3,
                )
            pid: int = int(pid_file.read_text(encoding="utf-8"))
        # A killed process may take a moment to be reaped; give it that.
        deadline: float = time.monotonic() + 10
        while _alive(pid) and time.monotonic() < deadline:
            time.sleep(0.2)
        self.assertFalse(_alive(pid), f"grandchild {pid} outlived the timeout")

    def test_a_finished_child_returns_a_completed_process(self) -> None:
        result = run_isolated(
            [
                sys.executable,
                "-c",
                "import sys; print('out'); print('err', file=sys.stderr); sys.exit(3)",
            ],
            timeout=60,
        )
        self.assertEqual(result.returncode, 3)
        self.assertEqual(result.stdout.strip(), "out")
        self.assertEqual(result.stderr.strip(), "err")

    def test_stdin_is_detached(self) -> None:
        result = run_isolated(
            [sys.executable, "-c", "import sys; print(repr(sys.stdin.read()))"],
            timeout=60,
        )
        self.assertEqual(result.stdout.strip(), "''")

    def test_a_missing_program_raises_oserror(self) -> None:
        with self.assertRaises(OSError):
            run_isolated(["bench-no-such-program-4f9c"], timeout=5)


if __name__ == "__main__":
    unittest.main()
