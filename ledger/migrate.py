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

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from ledger.chain import ENTRIES_DIRNAME, META_FILENAME, resolve_ledger_path
from ledger.verify import verify_chain

_LEGACY_DIRNAME: str = "ledger"
_LEDGER_FILENAME: str = "bench-ledger.json"
_JSON_GLOB: str = "*.json"

# One migration at a time per target. The lock is a file created with an
# exclusive create, so two runs cannot both hold it, and it is removed when
# the run ends. Under the lock, a staging directory found in the target can
# only belong to an attempt that died, so clearing it cannot pull a live
# attempt's files out from under it.
_LOCK_FILENAME: str = ".migrate.lock"

# A restore from git is fetched into a staging directory and published into
# the target only once every fetch has finished. Staging lives INSIDE the
# target directory, the one location the configured ledger path proves
# writable, and carries its own .gitignore of "*" so the debris of a failed
# attempt is ignored wherever the target sits (a BENCH_LEDGER_PATH may name
# a directory nothing else ignores). Each attempt gets its own mkdtemp
# directory carrying an ownership marker, and only a directory that carries
# the marker is ever removed, so a stale attempt is cleared and anything
# else with a similar name is left alone.
_STAGING_PREFIX: str = ".restoring-"
_STAGING_OWNER_MARKER: str = ".bench-restore"
# Written into the target before the first staged file is published and
# removed after the last. If it is found on a later run, a publish was
# interrupted and could not be rolled back, and the target must not be
# taken for a complete chain.
_INCOMPLETE_MARKER: str = "restore-incomplete"


# Ceiling on each git call. Migration reads local history, which is fast,
# but a repository on a stalled network mount or a hung credential helper
# must end as a failed step, not a command that never returns; every
# subprocess in this tree carries a timeout (tests/test_subprocess_timeouts.py
# scans for it).
_GIT_TIMEOUT_SECONDS: float = 60.0


class LockError(Exception):
    """The migration lock could not be taken for a reason other than "held".

    Raised after an attempt to remove any partly created lock file, so a
    refused run leaves no lock behind when it can help it. ``lock_left``
    says whether that removal failed, in which case later runs will refuse
    with MIGRATION_IN_PROGRESS until the file is removed by hand.
    """

    def __init__(self, message: str, lock_left: bool = False) -> None:
        super().__init__(message)
        self.lock_left: bool = lock_left


class RestoreIncomplete(Exception):
    """A publish ended with the incomplete marker still in the target.

    Either a failed publish could not be fully moved back, or a complete
    one could not remove its marker. In both the target holds files and
    the marker, every later run will refuse with INCOMPLETE_RESTORE, and
    this run must not report success over that state.
    """


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

    # Fetch into a staging directory inside the target and publish only
    # once every fetch has finished (see _STAGING_PREFIX). Nothing reaches
    # the target's chain files while git can still time out, so a timed-out
    # attempt cannot leave a half-restored chain for the already_started
    # guard to accept; the worst a failed cleanup can do is leave debris in
    # a staging directory Bench owns, inside the gitignored target.
    target_dir.mkdir(parents=True, exist_ok=True)
    _clear_stale_staging(target_dir)
    try:
        staging: Path = _new_staging(target_dir)
    except OSError as exc:
        print(
            f"[bench migrate] could not create a staging directory under "
            f"{target_dir}: {exc}; nothing was restored",
            file=sys.stderr,
        )
        return None
    try:
        written: int = _fetch_into(repo_root, ref, wanted, staging)
    except GitTimeout:
        _remove_staging(staging)
        raise
    except OSError as exc:
        # A blob that cannot be written to staging (disk full, a vanished
        # mount) fails the restore before anything reaches the target.
        print(
            f"[bench migrate] could not stage the chain at {ref[:12]}: {exc}; "
            f"nothing was restored",
            file=sys.stderr,
        )
        _remove_staging(staging)
        return None
    return _publish(staging, target_dir, written, len(wanted)), len(wanted)


def _new_staging(target_dir: Path) -> Path:
    """A fresh staging directory inside the target, marked as Bench's own.

    It carries its own ``.gitignore`` of ``*`` before any blob is written,
    so the debris of a failed attempt is ignored wherever the target sits:
    ``.bench/`` is ignored by the project's own file, but
    ``BENCH_LEDGER_PATH`` may name a directory nothing ignores, and a
    restored entry holds the full diff body of every change it recorded.
    """
    staging: Path = Path(tempfile.mkdtemp(prefix=_STAGING_PREFIX, dir=target_dir))
    (staging / ".gitignore").write_text("*\n", encoding="utf-8")
    (staging / _STAGING_OWNER_MARKER).write_text("", encoding="utf-8")
    (staging / ENTRIES_DIRNAME).mkdir()
    return staging


def _clear_stale_staging(target_dir: Path) -> None:
    """Remove staging directories left by earlier attempts, and only those.

    A directory with the staging prefix but no ownership marker was not
    created by Bench and is left alone, with a note. A removal that fails
    is logged and skipped: this attempt uses its own fresh directory, so a
    stale one it cannot clear is debris, not a blocker.
    """
    for candidate in sorted(target_dir.glob(f"{_STAGING_PREFIX}*")):
        if not (candidate / _STAGING_OWNER_MARKER).is_file():
            print(
                f"[bench migrate] {candidate} has no ownership marker and was "
                f"not created by Bench; left alone",
                file=sys.stderr,
            )
            continue
        _remove_staging(candidate)


def _remove_staging(staging: Path) -> bool:
    """Remove a staging directory Bench created. False, and a log, on failure."""
    try:
        shutil.rmtree(staging)
    except OSError as exc:
        print(
            f"[bench migrate] could not remove the staging directory "
            f"{staging}: {exc}",
            file=sys.stderr,
        )
        return False
    return True


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


def _publish(staging: Path, target_dir: Path, written: int, expected: int) -> int:
    """Move every staged file into the target; return how many are in place.

    The incomplete marker is written before the first move and removed
    after the last. Each move is a rename, so a file is either wholly in
    place or absent. If a move fails, every file already moved is moved
    back (C-001: logged, and reported as zero published); if any move-back
    fails, the marker stays, and migrate_ledger then refuses to treat the
    target as a chain rather than reporting already_migrated over a
    partial one. The marker also stays when fewer files were staged than
    the commit held, for the same reason. Staging is removed after a
    complete publish and left for inspection otherwise.
    """
    marker: Path = target_dir / _INCOMPLETE_MARKER
    try:
        (target_dir / ENTRIES_DIRNAME).mkdir(exist_ok=True)
        marker.write_text("", encoding="utf-8")
    except OSError as exc:
        print(
            f"[bench migrate] could not write {marker}: {exc}; nothing was "
            f"published, since an interrupted publish could not be flagged",
            file=sys.stderr,
        )
        return 0

    pending: list[tuple[Path, Path]] = [
        (source, target_dir / source.name)
        for source in sorted(staging.glob(_JSON_GLOB))
    ] + [
        (source, target_dir / ENTRIES_DIRNAME / source.name)
        for source in sorted((staging / ENTRIES_DIRNAME).glob(_JSON_GLOB))
    ]
    moved: list[tuple[Path, Path]] = []
    try:
        for source, destination in pending:
            source.replace(destination)
            moved.append((source, destination))
    except OSError as exc:
        print(
            f"[bench migrate] publish failed after {len(moved)} of {written} "
            f"files: {exc}; moving them back",
            file=sys.stderr,
        )
        if _move_back(moved) and _remove_marker(marker):
            return 0
        raise RestoreIncomplete("publish failed and could not be fully rolled back")

    if len(moved) < expected:
        print(
            f"[bench migrate] {len(moved)} of {expected} files published; the "
            f"incomplete marker stays so the next run does not take this for "
            f"a complete chain",
            file=sys.stderr,
        )
        return len(moved)
    if not _remove_marker(marker):
        # The files are all in place but every later run would refuse
        # them; that is not a success this run may report.
        raise RestoreIncomplete("published in full but the marker could not be removed")
    _remove_staging(staging)
    return len(moved)


def _move_back(moved: list[tuple[Path, Path]]) -> bool:
    """Return published files to staging, newest first. False if any stays."""
    all_back: bool = True
    for source, destination in reversed(moved):
        try:
            destination.replace(source)
        except OSError as exc:
            print(
                f"[bench migrate] could not move {destination} back to staging: "
                f"{exc}",
                file=sys.stderr,
            )
            all_back = False
    return all_back


def _remove_marker(marker: Path) -> bool:
    """Remove the incomplete marker. False, and a log, if it stays."""
    try:
        marker.unlink()
    except OSError as exc:
        print(
            f"[bench migrate] could not remove {marker}: {exc}; the next "
            f"migration will refuse until it is removed by hand, once the "
            f"target's files match the commit's listing and "
            f"`python -m cli verify` passes",
            file=sys.stderr,
        )
        return False
    return True


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
        for entry in sorted(entries_src.glob(_JSON_GLOB)):
            expected += 1
            shutil.copy2(entry, entries_dst / entry.name)
            written += 1
    return written, expected


def _restore_from_disk(legacy_dir: Path, target_dir: Path) -> tuple[int, int] | None:
    """Copy the working-tree chain into the target through staging.

    The same shape as _restore_from_git: files are copied into a staging
    directory and published only once every copy has finished, so a copy
    that fails halfway (disk full, a vanished mount) leaves no chain files
    in the target for the started guard to accept. None means nothing was
    restored; RestoreIncomplete propagates from a publish that ended with
    the marker in place.
    """
    target_dir.mkdir(parents=True, exist_ok=True)
    _clear_stale_staging(target_dir)
    try:
        staging: Path = _new_staging(target_dir)
    except OSError as exc:
        print(
            f"[bench migrate] could not create a staging directory under "
            f"{target_dir}: {exc}; nothing was restored",
            file=sys.stderr,
        )
        return None
    try:
        written, expected = _copy_from_disk(legacy_dir, staging)
    except OSError as exc:
        print(
            f"[bench migrate] could not copy the chain from {legacy_dir}: "
            f"{exc}; nothing was restored",
            file=sys.stderr,
        )
        _remove_staging(staging)
        return None
    return _publish(staging, target_dir, written, expected), expected


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


def _failed(
    target: Path, source: str, failure_type: str, detail: str
) -> dict[str, Any]:
    """A failed-migration result in which nothing was restored.

    One shape for every failure so callers and the CLI render them the
    same way; failure_type names the cause and detail says what to do,
    which is always a retry or an inspection, never a fresh genesis.
    """
    return {
        "status": "failed",
        "source": source,
        "target": str(target),
        "files": 0,
        "expected": 0,
        "verified": False,
        "entries": 0,
        "genesis_hash": "",
        "failure_type": failure_type,
        "detail": detail,
    }


def _incomplete(target: Path) -> dict[str, Any]:
    """The failed result for a target carrying the incomplete marker.

    Returned both when a run finds the marker left by an earlier attempt
    and when this run's own publish ends with the marker in place (see
    RestoreIncomplete), so the two read the same and neither is mistaken
    for a chain.
    """
    return _failed(
        target,
        "git history",
        "INCOMPLETE_RESTORE",
        f"{target / _INCOMPLETE_MARKER} exists: a restore was interrupted "
        "while publishing, or published fewer files than the commit held, "
        "or could not remove its marker after publishing, and the target "
        "must not be taken for a complete chain. Remove what the "
        "interrupted restore left and retry: a retry restores the full "
        "set from history. A passing `python -m cli verify` is not enough "
        "on its own to clear the marker by hand, because a prefix of the "
        "commit verifies; the target's files must first match the commit's "
        "listing (`git ls-tree -r --name-only <commit> -- ledger/`).",
    )


def _timed_out(target: Path, exc: GitTimeout) -> dict[str, Any]:
    """The failed result for a git call that did not finish."""
    return _failed(
        target,
        "git history",
        "GIT_TIMEOUT",
        f"{exc} did not finish within {_GIT_TIMEOUT_SECONDS:g}s, so "
        "whether history holds a chain is unknown. Nothing was restored "
        "and nothing was opened: appending to a chain that only looks "
        "absent would fork it. Retry, or inspect the repository.",
    )


# The runtime files a migration may leave in the target: the lock, the
# incomplete marker, and staging directories. Each must stay out of git
# even when BENCH_LEDGER_PATH names a directory nothing else ignores, or a
# lock left by an interrupted run could be committed and make every other
# clone of that repository refuse to migrate. Written into the target's
# own .gitignore before the first of them is created. The ignore file names
# itself as well: git still reads an ignored .gitignore, and without that
# entry the file Bench wrote would be the one untracked path left behind
# (tests.test_migrate.GitHistorySourceTests).
# Each pattern is anchored with a leading slash: a slashless pattern in a
# .gitignore matches at every level beneath it, so under a ledger path that
# shares its directory with project files (the repository root included)
# Bench would otherwise hide `sub/.gitignore` or `sub/.restoring-x/` too.
_RUNTIME_IGNORES: tuple[str, ...] = (
    f"/{_LOCK_FILENAME}",
    f"/{_INCOMPLETE_MARKER}",
    f"/{_STAGING_PREFIX}*/",
    "/.gitignore",
)
# The block _ensure_runtime_ignore keeps at the tail of that file.
_RUNTIME_IGNORE_BLOCK: bytes = b"\n".join(p.encode("ascii") for p in _RUNTIME_IGNORES) + b"\n"


def _ensure_runtime_ignore(target: Path) -> None:
    """Make sure the target's .gitignore covers Bench's runtime files.

    Git applies the last matching pattern, so a pattern that is merely
    present somewhere in the file proves nothing: `.migrate.lock` followed
    by `!.migrate.lock` leaves the lock committable. Bench's block is
    therefore appended whenever it is not already the tail of the file,
    which makes it the last word on its own paths and keeps a rerun from
    growing the file. A .gitignore inside an already-ignored .bench/ is
    harmless. The file is read and appended as bytes, so an existing file
    in any encoding is kept as it was and cannot raise a decode error; the
    patterns Bench adds are ASCII. Raises OSError to the caller, which is
    about to create the lock in the same directory and reports both
    failures the same way (tests.test_migrate.GitHistorySourceTests).
    """
    ignore: Path = target / ".gitignore"
    raw: bytes = ignore.read_bytes() if ignore.exists() else b""
    if raw == _RUNTIME_IGNORE_BLOCK or raw.endswith(b"\n" + _RUNTIME_IGNORE_BLOCK):
        return
    with ignore.open("ab") as handle:
        if raw and not raw.endswith(b"\n"):
            handle.write(b"\n")
        handle.write(_RUNTIME_IGNORE_BLOCK)


def _acquire_lock(target: Path) -> Path | None:
    """Take the target's migration lock, or None if it is held or cannot be.

    An exclusive create is atomic, so of two runs that race for it exactly
    one gets a file descriptor. The holder's pid is written for the note a
    refused run prints; a failure other than "exists" is logged and also
    refuses (C-001), since a migration that cannot prove it is alone must
    not clear another attempt's staging.
    """
    lock: Path = target / _LOCK_FILENAME
    try:
        target.mkdir(parents=True, exist_ok=True)
        _ensure_runtime_ignore(target)
        descriptor: int = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return None
    except OSError as exc:
        raise LockError(f"could not take {lock}: {exc}") from exc
    # From here the lock file exists and is ours. If writing the pid fails
    # the file is removed before the error propagates, so a run refused for
    # a filesystem fault does not leave a lock every retry would trip on.
    handle = None
    try:
        handle = os.fdopen(descriptor, "w", encoding="utf-8")
        with handle:
            handle.write(str(os.getpid()))
    except OSError as exc:
        if handle is None:
            # fdopen never took the descriptor, so it is still open, and an
            # open file cannot be unlinked on Windows.
            try:
                os.close(descriptor)
            except OSError as close_exc:
                print(
                    f"[bench migrate] could not close {lock}: {close_exc}",
                    file=sys.stderr,
                )
        removed: bool = _release_lock(lock)
        raise LockError(
            f"could not initialise {lock}: {exc}", lock_left=not removed
        ) from exc
    return lock


def _release_lock(lock: Path) -> bool:
    """Remove the lock. False, and a log, if it stays."""
    try:
        lock.unlink()
    except OSError as exc:
        print(
            f"[bench migrate] could not remove {lock}: {exc}; remove it by "
            f"hand once no migration is running",
            file=sys.stderr,
        )
        return False
    return True


def migrate_ledger(repo_root: Path | None = None) -> dict[str, Any]:
    """Populate this clone's private chain from the pre-migration location.

    Idempotent and non-destructive: an existing ``.bench/`` chain is left
    alone and reported as ``already_migrated`` rather than overwritten.
    One run at a time per target: a second run while the lock is held is
    refused with MIGRATION_IN_PROGRESS and touches nothing. A run whose
    lock cannot be released afterwards is reported as a failure even when
    the chain migrated, because every later run would refuse.
    """
    root: Path = repo_root or Path.cwd()
    target: Path = Path(resolve_ledger_path()).parent
    legacy_dir: Path = root / _LEGACY_DIRNAME

    # A target that already holds a chain, or the marker of an interrupted
    # restore, is answered without taking the lock: neither answer writes
    # anything, and a read-only directory provisioned with a chain must
    # still report already_migrated rather than fail to create a lock.
    # The same check runs again under the lock before anything is written,
    # so a chain that appears in between is not restored over.
    existing: dict[str, Any] | None = _existing_chain(target)
    if existing is not None:
        return existing

    try:
        lock: Path | None = _acquire_lock(target)
    except LockError as exc:
        print(f"[bench migrate] {exc}", file=sys.stderr)
        if exc.lock_left:
            return _failed(
                target,
                "none",
                "LOCK_NOT_RELEASED",
                f"{exc}, and the partly created {target / _LOCK_FILENAME} could "
                "not be removed, so every later run would refuse with "
                "MIGRATION_IN_PROGRESS until it is removed by hand once no "
                "migration is running. Nothing else was touched.",
            )
        return _failed(
            target,
            "none",
            "LOCK_FAILED",
            f"{exc}. Nothing was touched. Check that {target} is writable, "
            "then retry.",
        )
    if lock is None:
        return _failed(
            target,
            "none",
            "MIGRATION_IN_PROGRESS",
            f"{target / _LOCK_FILENAME} is held: another migration is running, "
            "or one ended without releasing it. Nothing was touched. Wait for "
            "it, or if no migration is running, remove the lock file and retry.",
        )
    try:
        result: dict[str, Any] = _migrate_locked(root, target, legacy_dir)
    finally:
        released: bool = _release_lock(lock)
    if released:
        return result
    # Whatever the run's own outcome, a lock that stays would make every
    # retry refuse with MIGRATION_IN_PROGRESS and no explanation, so the
    # stuck lock is the headline and the run's own result rides in the
    # detail, where a retry note or a chain status is still readable.
    own: str = str(result.get("status", ""))
    if result.get("failure_type"):
        own += f" ({result.get('failure_type')}): {result.get('detail', '')}"
    return _failed(
        target,
        str(result.get("source", "none")),
        "LOCK_NOT_RELEASED",
        f"{lock} could not be removed, so every later run would refuse with "
        "MIGRATION_IN_PROGRESS until it is removed by hand once no migration "
        f"is running. This run's own result: {own}. If a chain is in place, "
        "`python -m cli verify` checks it.",
    )


def _existing_chain(target: Path) -> dict[str, Any] | None:
    """The result for a target that must not be restored into, or None.

    Reads only, so it can answer before the lock is taken. A marker left by
    an interrupted publish (see _publish) means the target may hold part of
    a chain, and is checked first: the started check below would take the
    part for the whole and report already_migrated. A chain opened after
    the switch has no legacy segment at all, its entries live only in
    entries/, so a non-empty entries/ counts as started too; checking for
    bench-ledger.json alone would splice a restored history into a running
    chain.
    """
    if (target / _INCOMPLETE_MARKER).exists():
        return _incomplete(target)
    entries_dir: Path = target / ENTRIES_DIRNAME
    already_started: bool = (target / _LEDGER_FILENAME).exists() or (
        entries_dir.is_dir() and any(entries_dir.glob(_JSON_GLOB))
    )
    if already_started:
        return {
            "status": "already_migrated",
            "target": str(target),
            "files": 0,
            "detail": "This clone already has a private chain; nothing to do.",
        }
    return None


def _migrate_locked(root: Path, target: Path, legacy_dir: Path) -> dict[str, Any]:
    """The migration proper, run while the target's lock is held."""
    # Re-checked under the lock: a chain that appeared between the
    # lock-free check and here must not be restored over.
    existing: dict[str, Any] | None = _existing_chain(target)
    if existing is not None:
        return existing

    if (legacy_dir / _LEDGER_FILENAME).exists():
        try:
            copied: tuple[int, int] | None = _restore_from_disk(legacy_dir, target)
        except RestoreIncomplete as exc:
            print(f"[bench migrate] {exc}", file=sys.stderr)
            return _incomplete(target)
        if copied is None:
            return _failed(
                target,
                "working tree",
                "COPY_FAILED",
                f"Could not copy the chain from {legacy_dir} into place, so "
                "nothing was restored. The working-tree chain is untouched; "
                "check that the target is writable and has room, then retry.",
            )
        written, expected = copied
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
    except RestoreIncomplete as exc:
        print(f"[bench migrate] {exc}", file=sys.stderr)
        return _incomplete(target)
    if restored is None:
        return _failed(
            target,
            f"git history at {ref[:12]}",
            "ENUMERATION_FAILED",
            "Could not list the chain at that commit, or could not stage a "
            "restore, so nothing was restored. This is reported rather than "
            "treated as an empty chain, because appending to a chain that "
            "only looks empty would fork it.",
        )
    written, expected = restored
    return _finish(target, f"git history at {ref[:12]}", written, expected)
