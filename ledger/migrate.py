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
import tempfile
from pathlib import Path
from typing import Any

from ledger.chain import ENTRIES_DIRNAME, META_FILENAME, resolve_ledger_path
from ledger.verify import verify_chain

_LEGACY_DIRNAME: str = "ledger"
_LEDGER_FILENAME: str = "bench-ledger.json"
_JSON_GLOB: str = "*.json"

# A restore from git is fetched into a staging directory and published into
# the target only once every fetch has finished. Staging lives INSIDE the
# target directory: that is the one location the configured ledger path
# proves writable, and it is the gitignored one, so debris from a failed
# attempt can never be committed. Each attempt gets its own mkdtemp
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
    return _publish(staging, target_dir, written, len(wanted)), len(wanted)


def _new_staging(target_dir: Path) -> Path:
    """A fresh staging directory inside the target, marked as Bench's own."""
    staging: Path = Path(tempfile.mkdtemp(prefix=_STAGING_PREFIX, dir=target_dir))
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
    (target_dir / ENTRIES_DIRNAME).mkdir(exist_ok=True)
    marker: Path = target_dir / _INCOMPLETE_MARKER
    try:
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
        if _move_back(moved):
            _remove_marker(marker)
        return 0

    if len(moved) < expected:
        print(
            f"[bench migrate] {len(moved)} of {expected} files published; the "
            f"incomplete marker stays so the next run does not take this for "
            f"a complete chain",
            file=sys.stderr,
        )
    else:
        _remove_marker(marker)
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


def _remove_marker(marker: Path) -> None:
    """Remove the incomplete marker; a failure is logged and leaves it."""
    try:
        marker.unlink()
    except OSError as exc:
        print(
            f"[bench migrate] could not remove {marker}: {exc}; the next "
            f"migration will refuse until it is removed by hand after "
            f"`python -m cli verify` passes",
            file=sys.stderr,
        )


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
    # A marker left by an interrupted publish (see _publish) means the
    # target may hold part of a chain. That is checked before the
    # already_started guard, which would otherwise take the part for the
    # whole and report already_migrated.
    if (target / _INCOMPLETE_MARKER).exists():
        return {
            "status": "failed",
            "source": "git history",
            "target": str(target),
            "files": 0,
            "expected": 0,
            "verified": False,
            "entries": 0,
            "genesis_hash": "",
            "failure_type": "INCOMPLETE_RESTORE",
            "detail": (
                f"{target / _INCOMPLETE_MARKER} exists: an earlier restore was "
                "interrupted while publishing, or published fewer files than "
                "the commit held, and the target must not be taken for a "
                "complete chain. Inspect the target, remove what the "
                "interrupted restore left, and retry; or, if `python -m cli "
                "verify` passes on it, remove the marker by hand."
            ),
        }

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
