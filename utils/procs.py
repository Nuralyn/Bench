"""Subprocess calls whose timeout ends the child's descendants too.

``subprocess.run(timeout=...)`` kills only the process it started. A git
probe that hangs inside ``ssh`` or a credential helper would be killed
while the helper it launched lived on, holding network or authentication
state and leaking one process per timed-out probe. ``run_isolated`` starts
the child in a process group of its own (a session on POSIX, a new process
group on Windows) and, when the timeout fires, ends the whole group before
re-raising TimeoutExpired, so the caller's handling is unchanged and
nothing outlives it. Used by the git and gh probes in cli/commands.py and
by ledger/migrate.py; tests/test_procs.py proves a grandchild dies with
its parent.
"""

import contextlib
import os
import signal
import subprocess
import sys
import threading
from collections.abc import Iterator
from types import FrameType
from typing import Any

# How long to wait for the group to be gone once it has been told to go.
_REAP_SECONDS: float = 5.0


def run_isolated(
    args: list[str],
    *,
    timeout: float,
    cwd: str | None = None,
    encoding: str = "utf-8",
    errors: str = "strict",
) -> subprocess.CompletedProcess[str]:
    """Run ``args`` with captured text output, stdin detached, and a
    timeout that ends the child's whole process group.

    Raises subprocess.TimeoutExpired once the group has been ended, and
    OSError when the program cannot be started, exactly as subprocess.run
    would, so callers keep the handling they have.
    """
    isolate: dict[str, Any]
    if sys.platform == "win32":
        isolate = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    else:
        isolate = {"start_new_session": True}
    proc: subprocess.Popen[str] = subprocess.Popen(
        args,
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=False,
        text=True,
        encoding=encoding,
        errors=errors,
        **isolate,
    )
    try:
        with _ended_on_sigterm(proc):
            stdout, stderr = proc.communicate(timeout=timeout)
    except BaseException:
        # A timeout, or an interrupt (Ctrl-C) while waiting: either way the
        # group must not outlive the call. Ended, then re-raised unchanged.
        _end_group(proc)
        raise
    return subprocess.CompletedProcess(args, proc.returncode, stdout, stderr)


@contextlib.contextmanager
def _ended_on_sigterm(proc: subprocess.Popen[str]) -> Iterator[None]:
    """While active, a SIGTERM to this process ends the child's group first.

    The child leads its own session, so a signal sent to Bench's process
    group (a CI runner or a service manager cancelling the job) would not
    reach it, and SIGTERM ends Python without passing through the except
    clause above. POSIX only, and only on the main thread, where Python
    lets a handler be installed; elsewhere the timeout stays the bound.
    The previous handler is restored afterwards and, if it was a
    callable, invoked; the default disposition becomes SystemExit(143),
    which runs the finally clauses a plain termination would skip.
    """
    if sys.platform == "win32" or threading.current_thread() is not threading.main_thread():
        yield
        return
    previous = signal.getsignal(signal.SIGTERM)

    def end_then_terminate(signum: int, frame: FrameType | None) -> None:
        _end_group(proc)
        signal.signal(signal.SIGTERM, previous)
        if callable(previous):
            previous(signum, frame)
            return
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGTERM, end_then_terminate)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)


def _end_group(proc: subprocess.Popen[str]) -> None:
    """End the child's process group and reap the child; log what fails.

    A descendant killed with the group is reaped by whatever adopts it,
    normally PID 1. Where PID 1 does not reap adopted children (some
    minimal containers) it stays listed as a zombie, an entry with no
    running code, until that init does. Bench does not make itself a
    subreaper to cover that case: it would reparent every orphan of the
    process to it, and a waitpid(-1) sweep would swallow unrelated
    children's exit statuses.
    """
    try:
        if sys.platform == "win32":
            killed = subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=_REAP_SECONDS,
                check=False,
            )
            if killed.returncode != 0:
                # /T ends the tree; a nonzero status means some of it may
                # still be running, and that must be said, not assumed away.
                print(
                    f"[bench procs] taskkill /T on pid {proc.pid} returned "
                    f"{killed.returncode}: {(killed.stderr or killed.stdout).strip()}; "
                    "a descendant may still be running",
                    file=sys.stderr,
                )
        else:
            # The child leads its own session, so its pid is the group id.
            os.killpg(proc.pid, signal.SIGKILL)
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(
            f"[bench procs] could not end the process group of pid {proc.pid}: {exc}",
            file=sys.stderr,
        )
    proc.kill()
    try:
        proc.communicate(timeout=_REAP_SECONDS)
    except subprocess.TimeoutExpired:
        print(
            f"[bench procs] pid {proc.pid} did not exit within "
            f"{_REAP_SECONDS:g}s of being killed",
            file=sys.stderr,
        )
