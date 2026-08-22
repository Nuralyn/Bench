"""Hash-chained append-only ledger for Bench governance verdicts.

Every pipeline run (PASS or VETO) lands here as a JSON entry whose
``entry_hash`` is the SHA-256 of its own serialized fields and whose
``previous_hash`` names its parents. The first entry uses the sentinel
``"GENESIS"``. The chain is tamper-evident: any modification to a historical
entry invalidates every hash that descends from it (C-008 ledger immutability).

Storage has two segments. ``bench-ledger.json`` is the frozen legacy array,
read on every append and never written again, with ``ledger-meta.json`` frozen
beside it as a permanent pin on that segment's tip and count. New entries are
written one per file to ``entries/<entry_hash>.json``.

That split is what lets two branches record verdicts independently. A single
array rewritten in full on every append gave two branches divergent chains that
could not be merged — interleaving breaks the links, rebasing rewrites hashes —
so the array was frozen rather than migrated, because C-008 forbids moving or
rewriting an existing entry and a file that is never written cannot conflict.
``previous_hash`` accordingly holds a string (legacy, one parent) or a sorted
list, and an append names every current tip, so a fork left by a merge is
reconciled by the next governed edit.

Writes are atomic via ``os.replace`` on a same-directory temp file, so a crash
mid-write cannot leave a half-written file on disk.

This module only records; independent validation lives in ``verify.py``.
"""

import hashlib
import json
import os
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from utils.project import project_root

# Ledger routing, out-of-project classification, and constitution resolution
# must agree on which project a run belongs to; if they could disagree, a
# change could be judged against one project's constitution while being
# recorded in another project's ledger. utils.project is the single definition
# all three resolve through.
_PROJECT_LEDGER_DIRNAME: str = ".bench"
# Frozen pin on the legacy segment; never written here. verify.py imports
# this name to check the pin.
META_FILENAME: str = "ledger-meta.json"
_GENESIS_MARKER: str = "GENESIS"
ANCHOR_VERDICT: str = "ANCHOR"
"""Verdict on a chain-retirement anchor entry (C-008, constitution v2).

An anchor opens a successor chain and records the retired chain's tip hash,
genesis hash, entry count, and archive path. It is ledger bookkeeping rather
than an adjudicated change, so consumers must not count it as a governed
verdict.
"""
_MAX_FIELD_CHARS: int = 10_000
_MAX_STAGE_CHARS: int = 50_000

ENTRIES_DIRNAME: str = "entries"
"""Directory holding one JSON file per entry, named ``<entry_hash>.json``.

One file per entry makes branch merges conflict-free (the module docstring
covers why the single array could not be), and because the filename is the
content hash a merge cannot yield two files claiming the same identity.

``verify.py`` re-declares this name locally rather than importing it, keeping
the auditor independent of the write path.
"""


class LedgerReadError(RuntimeError):
    """Raised when existing ledger content cannot be read or would be lost.

    Fail-closed on the write path (C-001, C-008). A corrupt ledger previously
    read as an empty chain, which made the next append restart from GENESIS and
    overwrite the damaged file. Refusing to proceed preserves the evidence
    instead of destroying it; the caller in ``pipeline.runner`` already logs and
    returns the verdict without a receipt rather than blocking the developer.
    """


def _project_root() -> Path:
    """The root of the project currently being governed.

    A working directory anywhere inside the Bench repo counts as Bench
    governing itself, which is why editing ``utils/api.py`` while sitting in
    ``tests/`` is still in-project. Delegates to
    ``utils.project.project_root`` (see the module-level note on why ledger
    routing, external-change classification, and constitution resolution
    share one root definition).
    """
    return project_root()


def resolve_ledger_path() -> str:
    """Resolve which ledger the current run's verdict belongs to.

    Bench's PreToolUse hook can be registered globally (in the user's
    ``~/.claude/settings.json``), in which case it governs every project on
    the machine. Routing all of those verdicts to Bench's own ledger mixes
    unrelated projects' diffs into one chain and, if that chain is committed
    to a public repository, publishes them. The ledger therefore follows the
    project being governed:

    1. ``BENCH_LEDGER_PATH`` wins outright, for an explicit central ledger.
    2. Anything else writes to ``<project>/.bench/bench-ledger.json``.

    Bench governs itself through those same two rules, with no exemption: a
    run inside the Bench repo resolves to
    ``<bench repo>/.bench/bench-ledger.json`` exactly as any other project
    does. An operational ledger records the full diff body of every change it
    governs, so publishing one publishes every change it ever saw. It is
    therefore never a tracked artifact of the repository it governs, and
    dogfooding does not earn an exception to that.

    Bench's existing chain was relocated to that path before this branch
    removed the special case: the legacy segment, ``ledger-meta.json``, and
    every entry file were copied unchanged, and ``verify_chain`` reports VALID
    at the new location with genesis hash 4e98fb41 unchanged. No entry was
    modified, reordered, or removed, so continuity is preserved and no
    retirement was triggered, which matters because a storage-location change
    is not a permitted C-008 retirement trigger.

    Claude Code invokes hooks with the governed project as the working
    directory. That is the same assumption ``utils.diff`` already relies on
    when normalizing paths that fall outside the Bench repo.
    """
    override: str = os.environ.get("BENCH_LEDGER_PATH", "").strip()
    if override:
        return override

    return str(_project_root() / _PROJECT_LEDGER_DIRNAME / "bench-ledger.json")


def resolve_entries_dir(path: str | None = None) -> str:
    """Resolve the per-entry directory that sits beside the ledger file.

    Derived from ``resolve_ledger_path`` rather than resolved independently, so
    the entries directory always belongs to the same project as the ledger it
    accompanies. Bench's own is ``ledger/entries/``; a governed project's is
    ``<project>/.bench/entries/``.
    """
    resolved: str = path if path is not None else resolve_ledger_path()
    return str(Path(resolved).parent / ENTRIES_DIRNAME)


_EXTERNAL_BODY_KEYS: frozenset[str] = frozenset(
    {"content", "old_string", "new_string", "edits", "formatted_diff", "raw"}
)
_EXTERNAL_REDACTION_NOTE: str = (
    "Diff body omitted: file lies outside this ledger's project. "
    "Path and verdict are retained; the change itself was adjudicated in full."
)


def _is_external_change(file_ref: str) -> bool:
    """True when the governed file lies outside the project being governed.

    Anchored on ``_project_root()``, the same root ledger routing uses.

    Relative paths are normalized against the project by the hook, so only
    absolute paths can escape. A path that cannot be compared to the project
    root (different drive, unresolvable) is treated as external: the safe
    default is to redact rather than to publish.
    """
    if not file_ref or file_ref == "unknown" or not os.path.isabs(file_ref):
        return False
    try:
        rel: str = os.path.relpath(
            os.path.realpath(file_ref), str(_project_root())
        )
    except (OSError, ValueError) as exc:
        # Cannot prove the file is in-project, so redact. Log it: an
        # unresolvable path should be distinguishable from a genuinely
        # external one when auditing (C-001).
        print(
            f"[bench ledger] cannot locate {file_ref!r} against project root "
            f"({exc}); treating as external and redacting its diff body",
            file=sys.stderr,
        )
        return True
    return rel == os.pardir or rel.startswith(os.pardir + os.sep)


def _redact_external_diff(diff_summary: Any) -> Any:
    """Strip file contents from a diff summary for an out-of-project file.

    The pipeline still sees and adjudicates the full diff; only the recorded
    evidence is minimized, so a published ledger cannot become a mirror of
    source code belonging to another project. Metadata that carries no file
    content (path, change_type, truncation info) is preserved.

    Note the residual: challenger/defender/oracle prose may quote a few lines
    of the change it is reasoning about. This removes the systematic copy of
    file bodies, not every possible quotation.
    """
    if not isinstance(diff_summary, dict):
        return {"redacted": True, "note": _EXTERNAL_REDACTION_NOTE}
    kept: dict[str, Any] = {
        key: value
        for key, value in diff_summary.items()
        if key not in _EXTERNAL_BODY_KEYS
    }
    kept["redacted"] = True
    kept["note"] = _EXTERNAL_REDACTION_NOTE
    return kept


def _cap_stage_fields(stage: Any) -> Any:
    """Truncate oversized string fields in a pipeline stage dict.

    Caps individual strings at _MAX_FIELD_CHARS and the total serialized
    stage at _MAX_STAGE_CHARS. Returns the (possibly modified) stage.
    Non-dict stages pass through unchanged.
    """
    if not isinstance(stage, dict):
        return stage
    capped: dict[str, Any] = {}
    for key, value in stage.items():
        if isinstance(value, str) and len(value) > _MAX_FIELD_CHARS:
            capped[key] = value[:_MAX_FIELD_CHARS] + " [TRUNCATED]"
        elif isinstance(value, list):
            new_list: list[Any] = []
            for item in value:
                if isinstance(item, dict):
                    new_item: dict[str, Any] = {}
                    for k, v in item.items():
                        if isinstance(v, str) and len(v) > _MAX_FIELD_CHARS:
                            new_item[k] = v[:_MAX_FIELD_CHARS] + " [TRUNCATED]"
                        else:
                            new_item[k] = v
                    new_list.append(new_item)
                else:
                    new_list.append(item)
            capped[key] = new_list
        else:
            capped[key] = value
    serialized: str = json.dumps(capped, default=str)
    if len(serialized) > _MAX_STAGE_CHARS:
        return {
            "_capped": True,
            "_original_size": len(serialized),
            "status": stage.get("status", "UNKNOWN"),
            "verdict": stage.get("verdict"),
        }
    return capped


def compute_entry_hash(entry: dict) -> str:
    """Return the SHA-256 hex digest of ``entry`` with ``entry_hash`` excluded.

    Determinism is guaranteed by ``json.dumps(..., sort_keys=True)`` over a
    shallow copy that strips any existing ``entry_hash`` field. ``default=str``
    lets non-JSON-native values (e.g. ``datetime``) serialize without raising.
    """
    payload: dict[str, Any] = {k: v for k, v in entry.items() if k != "entry_hash"}
    serialized: str = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _parents_of(entry: dict) -> list[str]:
    """Parent hashes of an entry, tolerant of both link forms.

    Legacy entries carry a single string; entries written under the DAG format
    carry a sorted list. Readers stay lenient and return ``[]`` for anything
    else — rejecting a malformed link is ``verify.py``'s job, and a reader that
    raised would let one bad entry hide the whole history from a human trying
    to inspect it.
    """
    raw: Any = entry.get("previous_hash")
    if raw == _GENESIS_MARKER:
        return []
    if isinstance(raw, str):
        return [raw]
    if isinstance(raw, list):
        return [parent for parent in raw if isinstance(parent, str)]
    return []


def _load_legacy_strict(file_path: Path) -> list[dict]:
    """Read the frozen legacy array, raising rather than degrading.

    An absent file is normal and returns ``[]``. Every other failure raises
    ``LedgerReadError`` (see that class for why degrading to an empty chain
    is forbidden).
    """
    if not file_path.exists():
        return []

    try:
        raw: str = file_path.read_text(encoding="utf-8")
    except OSError as e:
        raise LedgerReadError(f"cannot read ledger {file_path}: {e}") from e

    try:
        data: object = json.loads(raw)
    except json.JSONDecodeError as e:
        raise LedgerReadError(f"corrupted ledger at {file_path}: {e}") from e

    if not isinstance(data, list):
        raise LedgerReadError(
            f"ledger at {file_path} is not a JSON array "
            f"(got {type(data).__name__})"
        )
    for index, item in enumerate(data):
        if not isinstance(item, dict):
            raise LedgerReadError(
                f"ledger at {file_path} entry {index} is not an object "
                f"(got {type(item).__name__})"
            )
    return data


def _load_entry_files(entries_dir: Path, *, strict: bool) -> list[dict]:
    """Read every ``<entry_hash>.json`` in the entries directory.

    ``strict=True`` is the write path: a bad file raises, because appending on
    top of a ledger that cannot be fully read risks a second genesis or a lost
    parent. ``strict=False`` is the read path: log and skip, so one damaged
    file does not conceal the rest of the history.
    """
    if not entries_dir.is_dir():
        return []

    entries: list[dict] = []
    for entry_file in sorted(entries_dir.glob("*.json")):
        try:
            data: object = json.loads(entry_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            if strict:
                raise LedgerReadError(
                    f"cannot read ledger entry {entry_file}: {e}"
                ) from e
            print(
                f"[bench ledger] skipping unreadable entry {entry_file}: {e}",
                file=sys.stderr,
            )
            continue

        if not isinstance(data, dict):
            if strict:
                raise LedgerReadError(
                    f"ledger entry {entry_file} is not a JSON object "
                    f"(got {type(data).__name__})"
                )
            print(
                f"[bench ledger] skipping malformed entry {entry_file} "
                f"(got {type(data).__name__})",
                file=sys.stderr,
            )
            continue

        # On the write path, being parseable is not enough. An entry with a
        # missing or mismatched hash contributes nothing to compute_tips, so a
        # single `{}` file would leave no tips and the next receipt would be
        # written with an empty parent list — extending a corrupt ledger with
        # another malformed root instead of refusing. Validate before building
        # on it. The read path stays lenient so a human can still inspect a
        # damaged ledger; verify.py is what rejects it authoritatively.
        if strict:
            problem: str = _entry_defect(data, entry_file)
            if problem:
                raise LedgerReadError(
                    f"refusing to append onto an invalid ledger: {problem}"
                )

        entries.append(data)
    return entries


def _entry_defect(entry: dict, entry_file: Path) -> str:
    """Describe why ``entry`` is unusable as a parent, or "" when it is sound.

    Deliberately narrow: it checks the properties an append depends on — that
    the entry has an identity, that the identity is authentic, and that the
    file claims the same identity it contains. Full chain validation is
    ``verify.py``'s job and is not duplicated here.
    """
    stored: Any = entry.get("entry_hash")
    if not isinstance(stored, str) or not stored:
        return f"{entry_file} has no string entry_hash"
    if compute_entry_hash(entry) != stored:
        return f"{entry_file} entry_hash does not match its contents"
    if entry_file.stem != stored:
        return f"{entry_file} filename does not match its entry_hash"
    return ""


def compute_tips(entries: list[dict]) -> list[str]:
    """Entry hashes that no other entry claims as a parent, sorted.

    A linear chain has exactly one tip. A git merge of two branches that both
    appended leaves two, and the next append names both as parents, so the fork
    reconciles itself as an ordinary governed entry rather than needing a
    separate command.
    """
    known: set[str] = {
        str(entry["entry_hash"])
        for entry in entries
        if isinstance(entry.get("entry_hash"), str)
    }
    referenced: set[str] = set()
    for entry in entries:
        referenced.update(_parents_of(entry))
    return sorted(known - referenced)


def _order_entries(legacy: list[dict], new: list[dict]) -> list[dict]:
    """Legacy entries in stored order, then new entries topologically.

    The legacy array's order is frozen and authoritative, so it is emitted
    verbatim — nothing is reordered. New entries are sequenced by Kahn's
    algorithm with the ready set ordered by ``(timestamp, entry_hash)``, so two
    runs over the same data always agree. Entries whose parents never resolve
    are appended last rather than dropped: a reader must never silently lose an
    entry it was able to read.
    """
    resolved: set[str] = {
        str(entry.get("entry_hash", "")) for entry in legacy
    }
    pending: dict[str, dict] = {
        str(entry["entry_hash"]): entry
        for entry in new
        if isinstance(entry.get("entry_hash"), str)
    }

    def _sort_key(entry_hash: str) -> tuple[str, str]:
        return (str(pending[entry_hash].get("timestamp", "")), entry_hash)

    ordered: list[dict] = list(legacy)
    while pending:
        ready: list[str] = [
            entry_hash
            for entry_hash, entry in pending.items()
            if all(parent in resolved for parent in _parents_of(entry))
        ]
        if not ready:
            break
        for entry_hash in sorted(ready, key=_sort_key):
            ordered.append(pending.pop(entry_hash))
            resolved.add(entry_hash)

    for entry_hash in sorted(pending, key=_sort_key):
        ordered.append(pending[entry_hash])
    return ordered


def load_ledger(path: str | None = None) -> list[dict]:
    """Load the ledger at ``path`` as a list of entries.

    ``path`` defaults to ``resolve_ledger_path()``, so readers follow the
    same project-scoped routing as writers.

    Returns the union of the frozen legacy array and the per-entry files
    beside it, deduplicated by ``entry_hash`` (the array wins a collision), in
    deterministic order.

    Read failures are logged to stderr and the readable remainder is returned,
    without touching anything on disk — a damaged ledger is preserved for
    forensic inspection rather than hidden or overwritten. The write path is
    strict instead: ``append_entry`` refuses to append onto a ledger it cannot
    fully read.
    """
    resolved: str = path if path is not None else resolve_ledger_path()
    file_path: Path = Path(resolved)

    try:
        legacy: list[dict] = _load_legacy_strict(file_path)
    except LedgerReadError as e:
        print(f"[bench ledger] {e}", file=sys.stderr)
        legacy = []

    new: list[dict] = _load_entry_files(
        Path(resolve_entries_dir(resolved)), strict=False
    )

    seen: set[str] = {str(entry.get("entry_hash", "")) for entry in legacy}
    deduped: list[dict] = [
        entry
        for entry in new
        if str(entry.get("entry_hash", "")) not in seen
    ]
    return _order_entries(legacy, deduped)


def append_entry(
    pipeline_result: dict,
    path: str | None = None,
) -> dict:
    """Append a governance verdict to the ledger.

    ``path`` defaults to ``resolve_ledger_path()``, which routes the verdict
    to the ledger of the project being governed rather than always to
    Bench's own.

    Expects ``pipeline_result`` to include the standard runner keys
    (``verdict``, ``pipeline_error``, ``constitution_hash``, ``challenger``,
    ``defender``, ``oracle``) and a ``change`` dict with ``file``, ``tool``,
    ``diff_summary``. The top-level ``verdict`` and ``pipeline_error`` are
    recorded so fail-closed error VETOs (which carry no oracle stage) stay
    legible in the audit trail. Missing fields fall back to safe defaults so
    the ledger never fails to record a verdict because of an upstream shape
    drift.

    Returns the full new entry (including its computed ``entry_hash``).
    """
    # Resolve once and reuse, so the entry is read from and appended to the
    # same chain even if resolution inputs were to change mid-run.
    resolved: str = path if path is not None else resolve_ledger_path()
    file_path: Path = Path(resolved)
    directory: Path = file_path.parent
    directory.mkdir(parents=True, exist_ok=True)

    # Strict on the write path: appending onto a ledger that cannot be fully
    # read risks a second genesis or a lost parent.
    entries_dir: Path = Path(resolve_entries_dir(resolved))
    legacy: list[dict] = _load_legacy_strict(file_path)
    existing_new: list[dict] = _load_entry_files(entries_dir, strict=True)
    existing: list[dict] = legacy + existing_new

    # A list of every current tip, so a fork left by a git merge is reconciled
    # by the next governed edit instead of needing a separate command. Sorted,
    # so the hash does not depend on filesystem iteration order.
    previous_hash: str | list[str] = _GENESIS_MARKER
    if existing:
        previous_hash = compute_tips(existing)
        if not previous_hash:
            # A non-empty ledger always has at least one tip; a cycle is
            # impossible because a parent hash must exist before a child can
            # commit to it. Reaching here means the ledger is incoherent, and
            # writing an empty parent list would create a second root.
            raise LedgerReadError(
                "refusing to append: ledger has entries but no tip, so the "
                "new entry would have no parent"
            )

    change_in: dict = pipeline_result.get("change") or {}
    timestamp: str = datetime.now(timezone.utc).isoformat()

    file_ref: str = change_in.get("file", "unknown")
    diff_summary: dict[str, object] | str = change_in.get(
        "diff_summary", change_in.get("raw", {})
    )
    if _is_external_change(file_ref):
        # Strip the file body before hashing, so the recorded evidence never
        # contains source belonging to another project. The pipeline already
        # adjudicated the full diff upstream; only the record is minimized.
        diff_summary = _redact_external_diff(diff_summary)

    entry: dict[str, Any] = {
        "entry_id": str(uuid.uuid4()),
        "timestamp": timestamp,
        "previous_hash": previous_hash,
        "constitution_hash": pipeline_result.get("constitution_hash", ""),
        # Which files produced that hash. When a project layer is stacked on
        # Bench's core, the hash chains two raw file hashes and is not itself
        # any file's digest, so a hash alone would not tell an auditor which
        # constitutions actually ruled. Absent (empty) on entries written
        # before layering existed, and on core-only runs it simply names the
        # single core file.
        "constitution_sources": pipeline_result.get("constitution_sources", []),
        "verdict": pipeline_result.get("verdict"),
        "pipeline_error": bool(pipeline_result.get("pipeline_error", False)),
        "change": {
            "file": file_ref,
            "tool": change_in.get("tool", "unknown"),
            "diff_summary": diff_summary,
        },
        "challenger": _cap_stage_fields(pipeline_result.get("challenger", {})),
        "defender": _cap_stage_fields(pipeline_result.get("defender", {})),
        "oracle": _cap_stage_fields(pipeline_result.get("oracle", {})),
    }
    entry["entry_hash"] = compute_entry_hash(entry)

    # Two-segment storage (see module docstring): the entry gets its own file
    # named by its hash; bench-ledger.json and ledger-meta.json are read above
    # but never rewritten, so the frozen segment stays byte-identical.
    entries_dir.mkdir(parents=True, exist_ok=True)
    entry_file: Path = entries_dir / f"{entry['entry_hash']}.json"
    if entry_file.exists():
        raise LedgerReadError(
            f"refusing to overwrite existing ledger entry {entry_file}"
        )
    _atomic_write_json(entry_file, entry)

    return entry


def _atomic_write_json(target: Path, data: Any) -> None:
    """Serialize ``data`` to ``target`` atomically via tempfile + os.replace."""
    directory: Path = target.parent
    tmp_fd, tmp_name = tempfile.mkstemp(
        prefix=target.name + ".",
        suffix=".tmp",
        dir=str(directory),
    )
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, default=str)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, target)
    except OSError as e:
        print(
            f"[bench ledger] atomic write to {target} failed: {e}",
            file=sys.stderr,
        )
        if os.path.exists(tmp_name):
            try:
                os.remove(tmp_name)
            except OSError as cleanup_err:
                print(
                    f"[bench ledger] failed to clean up temp file "
                    f"{tmp_name}: {cleanup_err}",
                    file=sys.stderr,
                )
        raise
