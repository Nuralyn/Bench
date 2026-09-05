"""One-time upgrade path for clones created before the ledger went private.

Bench used to keep its own chain as tracked files under ``ledger/``. That
exemption is gone: every governed project, Bench included, now keeps its
chain in a gitignored ``.bench/``. The commit that made the switch untracks
the old paths, so pulling it makes git delete ``ledger/bench-ledger.json``,
``ledger/ledger-meta.json``, and ``ledger/entries/`` from the working tree.

Nothing in git can repopulate ``.bench/``, because ``.bench/`` is ignored by
design. Without this module an existing clone would resolve to an empty
chain and its next governed edit would silently open a fresh GENESIS, losing
continuity with everything before it. That silence is the hazard; the chain
itself stays recoverable either from the working tree (a clone that has not
pulled yet) or from git history (one that has).

Two sources, tried in that order. Whichever is used, the restored chain is
handed to ``verify_chain`` before the migration reports success, so a
truncated or corrupted restore is surfaced rather than mistaken for a clean
one. A restore that writes fewer files than the source held is reported as
``partial``, never as ``migrated``.

The operation only ever copies whole files into a location that has no
chain. It never modifies, reorders, or removes an entry, and it refuses to
overwrite an existing ``.bench/`` chain, so it cannot destroy a chain a
clone has already started.
"""

import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from ledger.chain import ENTRIES_DIRNAME, META_FILENAME, resolve_ledger_path
from ledger.verify import verify_chain

_LEGACY_DIRNAME: str = "ledger"
_LEDGER_FILENAME: str = "bench-ledger.json"


# Ceiling on each git call. Migration reads local history, which is fast,
# but a repository on a stalled network mount or a hung credential helper
# must end as a failed step, not a command that never returns; every
# subprocess in this tree carries a timeout (tests/test_subprocess_timeouts.py
# scans for it).
_GIT_TIMEOUT_SECONDS: float = 60.0


class GitTimeout(Exception):
    """A git call did not finish within _GIT_TIMEOUT_SECONDS.

    Distinct from a non-zero exit on purpose. The history probes read a
    non-zero code as a negative answer ("this commit does not hold the
    chain"), and a timeout read that way would let migrate_ledger report
    nothing_to_migrate over a chain that is in fact in history, after which
    the next governed edit would open a fresh genesis and fork it. A timeout
    is an unanswered question, so it propagates as a failed migration.
    """


def _run_git(args: list[str], cwd: Path) -> tuple[int, str]:
    """Run a git command, returning (returncode, stdout).

    Failures are logged and returned rather than raised, so callers degrade
    with a typed result instead of a traceback (C-001). The one exception
    is a timeout, which raises GitTimeout: it is not a negative answer and
    must not be mistaken for one (see the class).
    """
    try:
        # stdin is detached so a prompt of any kind fails at once instead
        # of waiting on a terminal until the timeout fires.
        result = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdin=subprocess.DEVNULL,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        print(
            f"[bench migrate] git {' '.join(args)} did not finish within "
            f"{_GIT_TIMEOUT_SECONDS:g}s",
            file=sys.stderr,
        )
        raise GitTimeout(f"git {' '.join(args)}") from exc
    except OSError as exc:
        print(f"[bench migrate] git unavailable: {exc}", file=sys.stderr)
        return 1, ""
    if result.returncode != 0:
        print(
            f"[bench migrate] git {' '.join(args)} failed: "
            f"{result.stderr.strip()}",
            file=sys.stderr,
        )
    return result.returncode, result.stdout


def _last_ref_with_chain(repo_root: Path) -> str | None:
    """Newest commit whose tree still *contains* the tracked chain.

    Not simply the newest commit that touched the path. ``git log -- path``
    includes the commit that deleted it, and that commit's tree is exactly
    the one without the chain, so restoring from it would find nothing and
    report a clean migration of zero files. Each candidate is therefore
    probed with ``cat-file -e`` and the first that still holds the blob
    wins.
    """
    legacy: str = f"{_LEGACY_DIRNAME}/{_LEDGER_FILENAME}"
    code, out = _run_git(["log", "--format=%H", "--", legacy], repo_root)
    if code != 0:
        return None
    for ref in out.split():
        probe, _ = _run_git(["cat-file", "-e", f"{ref}:{legacy}"], repo_root)
        if probe == 0:
            return ref
    return None


def _restore_from_git(
    repo_root: Path, ref: str, target_dir: Path
) -> tuple[int, int] | None:
    """Write the chain from ``ref`` into ``target_dir``.

    Returns (written, expected) so a partial restore is visible to the
    caller rather than being reported as a complete one, or ``None`` when
    the tree could not be enumerated at all. None is distinct from (0, 0)
    on purpose: the latter is what an empty tree looks like, and a total
    failure must not be mistaken for a clean restore of nothing.
    """
    # Enumerate the tree first rather than probing each name with `git show`.
    # A failed `show` cannot be told apart from an absent file, so probing
    # would either count a chain that legitimately has no ledger-meta.json as
    # permanently incomplete, or let a genuine read failure pass as complete.
    # Listing what the ref actually holds makes expected exact, so any
    # shortfall afterwards is a real failure.
    code, listing = _run_git(
        ["ls-tree", "-r", "--name-only", ref, f"{_LEGACY_DIRNAME}/"],
        repo_root,
    )
    if code != 0:
        # None, not (0, 0). A zero-of-zero restore is what a legitimately
        # empty tree looks like, so returning counts here would let total
        # enumeration failure read as a clean, complete migration.
        print(
            f"[bench migrate] could not enumerate the chain at {ref[:12]}; "
            f"nothing was restored",
            file=sys.stderr,
        )
        return None

    wanted: list[str] = _wanted_paths(listing)

    # Fetch into a staging directory beside the target and move into place
    # only once every fetch has finished. Nothing reaches the target while
    # git can still time out, so a timed-out attempt cannot leave a
    # half-restored chain that the already_started guard would accept on
    # the next run; the worst a failed cleanup can do is leave debris in
    # the staging directory, which the next attempt clears first.
    staging: Path = target_dir.parent / f"{target_dir.name}.restoring"
    if not _clear_staging(staging):
        return None
    staging.mkdir(parents=True)
    (staging / ENTRIES_DIRNAME).mkdir()
    try:
        written: int = _fetch_into(repo_root, ref, wanted, staging)
    except GitTimeout:
        _clear_staging(staging)
        raise
    return _move_into_place(staging, target_dir, written), len(wanted)


def _wanted_paths(listing: str) -> list[str]:
    """The chain files in an ls-tree listing: the two segments and entries."""
    wanted: list[str] = []
    for path in listing.split():
        parent: str = Path(path).parent.name
        name: str = Path(path).name
        if parent == _LEGACY_DIRNAME and name in (_LEDGER_FILENAME, META_FILENAME):
            wanted.append(path)
        elif parent == ENTRIES_DIRNAME and name.endswith(".json"):
            wanted.append(path)
        elif parent == ENTRIES_DIRNAME:
            print(
                f"[bench migrate] skipping non-JSON file in entries: {path}",
                file=sys.stderr,
            )
    return wanted


def _clear_staging(staging: Path) -> bool:
    """Remove a staging directory, stale or just abandoned. False on failure.

    A failure is logged and reported rather than raised (C-001). The
    caller treats it as a failed restore, since fetching into a directory
    that still holds another attempt's files would mix two restores.
    """
    if not staging.exists():
        return True
    try:
        shutil.rmtree(staging)
    except OSError as exc:
        print(
            f"[bench migrate] could not clear the staging directory "
            f"{staging}: {exc}",
            file=sys.stderr,
        )
        return False
    return True


def _fetch_into(repo_root: Path, ref: str, wanted: list[str], staging: Path) -> int:
    """Write each wanted blob at ``ref`` into ``staging``; return the count.

    A blob git cannot read is logged and skipped, which the caller reports
    as a partial restore. A timeout propagates as GitTimeout.
    """
    written: int = 0
    for path in wanted:
        code, blob = _run_git(["show", f"{ref}:{path}"], repo_root)
        if code != 0:
            print(
                f"[bench migrate] could not read {path} at {ref[:12]}; "
                f"the restore will be reported as partial",
                file=sys.stderr,
            )
            continue
        name: str = Path(path).name
        destination: Path = (
            staging / ENTRIES_DIRNAME / name
            if Path(path).parent.name == ENTRIES_DIRNAME
            else staging / name
        )
        destination.write_text(blob, encoding="utf-8")
        written += 1
    return written


def _move_into_place(staging: Path, target_dir: Path, written: int) -> int:
    """Move every staged file into the target; return how many arrived.

    Each move is a rename, so a file is either wholly in place or not there
    at all. A move that fails is logged and stops the loop (C-001); what
    arrived is counted, the caller verifies it and reports the shortfall as
    partial, and the staging directory is left for inspection.
    """
    target_dir.mkdir(parents=True, exist_ok=True)
    (target_dir / ENTRIES_DIRNAME).mkdir(exist_ok=True)
    moved: int = 0
    try:
        for source in sorted(staging.glob("*.json")):
            source.replace(target_dir / source.name)
            moved += 1
        for source in sorted((staging / ENTRIES_DIRNAME).glob("*.json")):
            source.replace(target_dir / ENTRIES_DIRNAME / source.name)
            moved += 1
    except OSError as exc:
        print(
            f"[bench migrate] could not move a restored file into place: "
            f"{exc}; {moved} of {written} arrived",
            file=sys.stderr,
        )
        return moved
    _clear_staging(staging)
    return moved


def _copy_from_disk(legacy_dir: Path, target_dir: Path) -> tuple[int, int]:
    """Copy an untouched on-disk chain into ``target_dir``."""
    target_dir.mkdir(parents=True, exist_ok=True)
    written: int = 0
    expected: int = 0
    for name in (_LEDGER_FILENAME, META_FILENAME):
        source: Path = legacy_dir / name
        if not source.exists():
            continue
        expected += 1
        shutil.copy2(source, target_dir / name)
        written += 1

    entries_src: Path = legacy_dir / ENTRIES_DIRNAME
    if entries_src.is_dir():
        entries_dst: Path = target_dir / ENTRIES_DIRNAME
        entries_dst.mkdir(exist_ok=True)
        for entry in sorted(entries_src.glob("*.json")):
            expected += 1
            shutil.copy2(entry, entries_dst / entry.name)
            written += 1
    return written, expected


def _finish(
    target: Path, source: str, written: int, expected: int
) -> dict[str, Any]:
    """Verify the restored chain and classify the outcome."""
    result: dict[str, Any] = verify_chain(str(target / _LEDGER_FILENAME))
    valid: bool = bool(result.get("valid"))
    complete: bool = written == expected
    return {
        "status": "migrated" if (valid and complete) else "partial",
        "source": source,
        "target": str(target),
        "files": written,
        "expected": expected,
        "verified": valid,
        "entries": result.get("entries", 0),
        "genesis_hash": result.get("genesis_hash", ""),
        "failure_type": result.get("failure_type", ""),
    }


def _timed_out(target: Path, exc: GitTimeout) -> dict[str, Any]:
    """The failed-migration result for a git call that did not finish.

    Same shape as the other failure results so callers and the CLI render
    it the same way; failure_type names the cause so a retry is the obvious
    next step rather than a fresh genesis.
    """
    return {
        "status": "failed",
        "source": "git history",
        "target": str(target),
        "files": 0,
        "expected": 0,
        "verified": False,
        "entries": 0,
        "genesis_hash": "",
        "failure_type": "GIT_TIMEOUT",
        "detail": (
            f"{exc} did not finish within {_GIT_TIMEOUT_SECONDS:g}s, so "
            "whether history holds a chain is unknown. Nothing was restored "
            "and nothing was opened: appending to a chain that only looks "
            "absent would fork it. Retry, or inspect the repository."
        ),
    }


def migrate_ledger(repo_root: Path | None = None) -> dict[str, Any]:
    """Populate this clone's private chain from the pre-migration location.

    Idempotent and non-destructive: an existing ``.bench/`` chain is left
    alone and reported as ``already_migrated`` rather than overwritten.
    """
    root: Path = repo_root or Path.cwd()
    target: Path = Path(resolve_ledger_path()).parent
    legacy_dir: Path = root / _LEGACY_DIRNAME

    # A chain opened after the switch has no legacy segment at all: its
    # entries live only in entries/. Checking for bench-ledger.json alone
    # would miss it and splice a restored history into a running chain, so
    # a non-empty entries/ counts as started too.
    entries_dir: Path = target / ENTRIES_DIRNAME
    already_started: bool = (target / _LEDGER_FILENAME).exists() or (
        entries_dir.is_dir() and any(entries_dir.glob("*.json"))
    )
    if already_started:
        return {
            "status": "already_migrated",
            "target": str(target),
            "files": 0,
            "detail": "This clone already has a private chain; nothing to do.",
        }

    if (legacy_dir / _LEDGER_FILENAME).exists():
        written, expected = _copy_from_disk(legacy_dir, target)
        return _finish(target, "working tree", written, expected)

    # A git call that times out is an unanswered question, not a negative
    # answer. It is reported as a failed migration so the caller retries or
    # looks into the repository, never as nothing_to_migrate, which would
    # let the next governed edit open a fresh genesis over a chain that is
    # in history after all.
    try:
        ref: str | None = _last_ref_with_chain(root)
    except GitTimeout as exc:
        return _timed_out(target, exc)
    if ref is None:
        return {
            "status": "nothing_to_migrate",
            "target": str(target),
            "files": 0,
            "detail": (
                "No chain found on disk or in git history. A clone that never "
                "carried one starts empty, which is the expected state."
            ),
        }

    try:
        restored: tuple[int, int] | None = _restore_from_git(root, ref, target)
    except GitTimeout as exc:
        return _timed_out(target, exc)
    if restored is None:
        return {
            "status": "failed",
            "source": f"git history at {ref[:12]}",
            "target": str(target),
            "files": 0,
            "expected": 0,
            "verified": False,
            "entries": 0,
            "genesis_hash": "",
            "failure_type": "ENUMERATION_FAILED",
            "detail": (
                "Could not list the chain at that commit, so nothing was "
                "restored. This is reported rather than treated as an empty "
                "chain, because appending to a chain that only looks empty "
                "would fork it."
            ),
        }
    written, expected = restored
    return _finish(target, f"git history at {ref[:12]}", written, expected)
