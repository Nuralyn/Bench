"""Independent chain validator for the Bench ledger.

Reads ``ledger/bench-ledger.json`` directly and walks every entry,
recomputing each hash and confirming the ``previous_hash`` link holds
from the GENESIS entry through to the latest. This module deliberately
does not call ``load_ledger`` from ``chain.py`` — independence from the
write path is the whole point of an auditor. Only ``compute_entry_hash``
and ``META_FILENAME`` are shared, because the hashing algorithm and the
meta-anchor filename must match the writer by construction.

The validator reports the first failure it encounters (one bad entry is
enough to invalidate the chain) along with enough context to pinpoint
the tampered or missing entry.
"""

import json
import sys
from pathlib import Path
from typing import Any

from ledger.chain import META_FILENAME as _META_FILENAME
from ledger.chain import compute_entry_hash, resolve_ledger_path

_GENESIS_MARKER: str = "GENESIS"
_ENTRIES_DIRNAME: str = "entries"
"""Per-entry directory name, re-declared rather than imported from chain.py.

Same reasoning as ``_GENESIS_MARKER`` above: the auditor keeps its own
definition of where entries live and what a link means, so a change to the
writer cannot silently redefine what verification looks at. A test pins the two
constants equal.
"""


def verify_chain(path: str | None = None) -> dict:
    """Walk the ledger at ``path`` and return a verification summary.

    ``path`` defaults to ``resolve_ledger_path()`` so the auditor inspects
    the same project-scoped chain the writer appends to. Sharing the
    resolver is not a break with this module's independence: the auditor
    still recomputes every hash itself and never calls ``load_ledger``.
    Agreeing on *which* file to audit is a precondition for auditing it.

    Returns a dict describing either a valid chain (with summary stats)
    or the first detected failure (with the index and the expected vs.
    found values). An empty or absent ledger is treated as trivially
    valid — there is nothing to tamper with.
    """
    file_path: Path = Path(path if path is not None else resolve_ledger_path())

    entries_dir: Path = file_path.parent / _ENTRIES_DIRNAME
    has_entry_files: bool = entries_dir.is_dir() and any(
        entries_dir.glob("*.json")
    )

    raw: str = ""
    if not file_path.exists():
        if not has_entry_files:
            return {
                "valid": True,
                "entries": 0,
                "ledger_path": str(file_path),
                "message": "No ledger found. Nothing to verify.",
            }
    else:
        try:
            raw = file_path.read_text(encoding="utf-8")
        except OSError as e:
            return {
                "valid": False,
                "entries_checked": 0,
                "failure_index": -1,
                "failure_type": "READ_ERROR",
                "expected": "readable ledger file",
                "found": str(e),
                "message": f"Could not read ledger at {file_path}: {e}",
            }

    if not raw.strip():
        if not has_entry_files:
            return {
                "valid": True,
                "entries": 0,
                "message": "No ledger found. Nothing to verify.",
            }
        data: object = []
    else:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            return {
                "valid": False,
                "entries_checked": 0,
                "failure_index": -1,
                "failure_type": "PARSE_ERROR",
                "expected": "valid JSON array",
                "found": f"JSONDecodeError: {e}",
                "message": f"Ledger at {file_path} is not valid JSON: {e}",
            }

    if not isinstance(data, list):
        return {
            "valid": False,
            "entries_checked": 0,
            "failure_index": -1,
            "failure_type": "PARSE_ERROR",
            "expected": "JSON array at ledger root",
            "found": type(data).__name__,
            "message": (
                f"Ledger at {file_path} root must be a JSON array, "
                f"got {type(data).__name__}"
            ),
        }

    if len(data) == 0 and not has_entry_files:
        return {
            "valid": True,
            "entries": 0,
            "message": "No ledger found. Nothing to verify.",
        }

    entries: list[dict] = data
    previous_entry_hash: str | None = None

    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            return _failure(
                entries_checked=index,
                failure_index=index,
                failure_type="SCHEMA_ERROR",
                expected="entry to be a JSON object",
                found=type(entry).__name__,
                message=(
                    f"Entry {index} is not a JSON object "
                    f"(got {type(entry).__name__})."
                ),
            )

        stored_hash: Any = entry.get("entry_hash")
        if not isinstance(stored_hash, str):
            return _failure(
                entries_checked=index,
                failure_index=index,
                failure_type="SCHEMA_ERROR",
                expected="entry_hash field (string)",
                found=repr(stored_hash),
                message=f"Entry {index} is missing a string entry_hash.",
            )

        recomputed: str = compute_entry_hash(entry)
        if recomputed != stored_hash:
            return _failure(
                entries_checked=index,
                failure_index=index,
                failure_type="HASH_MISMATCH",
                expected=recomputed,
                found=stored_hash,
                message=(
                    f"Entry {index} has been tampered with: stored "
                    f"entry_hash does not match recomputed hash."
                ),
            )

        stored_prev: Any = entry.get("previous_hash")
        if index == 0:
            if stored_prev != _GENESIS_MARKER:
                return _failure(
                    entries_checked=index,
                    failure_index=index,
                    failure_type="INVALID_GENESIS",
                    expected=_GENESIS_MARKER,
                    found=repr(stored_prev),
                    message=(
                        "First entry must have previous_hash "
                        f"'{_GENESIS_MARKER}'."
                    ),
                )
        else:
            if stored_prev != previous_entry_hash:
                return _failure(
                    entries_checked=index,
                    failure_index=index,
                    failure_type="CHAIN_BREAK",
                    expected=previous_entry_hash,
                    found=repr(stored_prev),
                    message=(
                        f"Entry {index} previous_hash does not match "
                        f"entry {index - 1} entry_hash — chain broken."
                    ),
                )

        previous_entry_hash = stored_hash

    # Scoped to the legacy array, which ledger-meta.json permanently pins. The
    # array is frozen — never appended to again — so the anchor stays a fixed
    # assertion about a fixed segment, and every later entry's ancestry roots
    # at a tip that is itself pinned by a committed count.
    meta_note: str = "meta anchor skipped: no legacy chain"
    if entries:
        meta_failure, meta_note = _check_meta_anchor(
            file_path.parent / _META_FILENAME, entries
        )
        if meta_failure is not None:
            return meta_failure

    seen: dict[str, dict] = {}
    for entry in entries:
        seen[str(entry.get("entry_hash", ""))] = entry

    files_failure, checked = _verify_entry_files(
        entries_dir, seen, len(entries)
    )
    if files_failure is not None:
        return files_failure

    dag_failure, tips = _verify_dag(seen, checked)
    if dag_failure is not None:
        return dag_failure

    # Endpoints come from the graph, never from iteration order. ``seen`` is
    # filled legacy-first and then by filename, which is hash order, so the
    # last inserted entry is not the tip and the first is not necessarily
    # genesis. Reporting those directly would name unrelated entries as the
    # chain's endpoints while still claiming the chain is valid.
    genesis_hash: str = next(
        (
            entry_hash
            for entry_hash, entry in seen.items()
            if entry.get("previous_hash") == _GENESIS_MARKER
        ),
        "",
    )
    genesis_entry: dict = seen.get(genesis_hash, {})
    tip_entries: list[dict] = [seen[tip] for tip in tips if tip in seen]
    latest_entry: dict = max(
        tip_entries,
        key=lambda entry: str(entry.get("timestamp", "")),
        default={},
    )
    return {
        "valid": True,
        "entries": checked,
        "ledger_path": str(file_path),
        "first_entry": genesis_entry.get("timestamp", ""),
        "last_entry": latest_entry.get("timestamp", ""),
        "genesis_hash": genesis_hash,
        "latest_hash": tips[0] if len(tips) == 1 else "",
        "tips": tips,
        "meta": meta_note,
    }


def _check_meta_anchor(
    meta_path: Path, entries: list[dict]
) -> tuple[dict | None, str]:
    """Cross-check ledger-meta.json against the verified chain.

    Returns ``(failure, note)``. ``failure`` is a ``_failure(...)`` dict
    when the meta anchor contradicts the chain (a rewritten but internally
    consistent chain would otherwise pass), else None. ``note`` records the
    anchor status for the summary. A missing or unreadable meta file does
    not invalidate the chain, which is self-contained; the skip is
    surfaced in the note rather than silently ignored.

    Relies on ``json``, ``sys``, ``Path``, and ``_META_FILENAME`` already
    imported/defined at module scope. Called only after the chain walk has
    validated every entry, so ``entries`` is non-empty and each entry_hash
    is a string.
    """
    if not meta_path.exists():
        return None, "meta anchor skipped: ledger-meta.json not found"

    try:
        parsed: object = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        print(
            f"[bench verify] meta anchor unreadable: "
            f"{type(e).__name__}: {e}",
            file=sys.stderr,
        )
        return None, f"meta anchor skipped: unreadable ({type(e).__name__})"

    if not isinstance(parsed, dict):
        return None, (
            "meta anchor skipped: ledger-meta.json is not a JSON object"
        )

    last_hash: str = entries[-1]["entry_hash"]
    meta_hash: object = parsed.get("latest_hash")
    if meta_hash != last_hash:
        return _failure(
            entries_checked=len(entries),
            failure_index=len(entries) - 1,
            failure_type="META_MISMATCH",
            expected=meta_hash,
            found=last_hash,
            message=(
                "ledger-meta.json latest_hash does not match the final "
                "entry's hash: the chain may have been rewritten."
            ),
        ), ""

    meta_count: object = parsed.get("entry_count")
    if meta_count != len(entries):
        return _failure(
            entries_checked=len(entries),
            failure_index=len(entries) - 1,
            failure_type="META_MISMATCH",
            expected=meta_count,
            found=len(entries),
            message=(
                "ledger-meta.json entry_count does not match the number "
                "of chain entries: entries may have been added or removed."
            ),
        ), ""

    return None, "meta anchor verified"


def _parents_of(entry: dict) -> list[str]:
    """Parent hashes of an entry: ``GENESIS`` -> none, str -> one, list -> many.

    Deliberately duplicated from ``chain.py`` rather than imported, like
    ``_GENESIS_MARKER`` above. The auditor must not inherit the writer's idea
    of what a link means; if the two ever disagree, verification should fail
    rather than silently agree with a bug.
    """
    raw: Any = entry.get("previous_hash")
    if raw == _GENESIS_MARKER:
        return []
    if isinstance(raw, str):
        return [raw]
    if isinstance(raw, list):
        return [parent for parent in raw if isinstance(parent, str)]
    return []


def _verify_entry_files(
    entries_dir: Path, seen: dict[str, dict], checked: int
) -> tuple[dict | None, int]:
    """Recompute the hash of every per-entry file and register it in ``seen``.

    Each file is validated exactly as strictly as an array element: readable,
    an object, carrying a string ``entry_hash`` that matches its recomputed
    value. Two extra checks exist only in this storage form — the filename must
    equal the hash it contains, and no hash may appear twice across the whole
    union, so a renamed or duplicated file cannot smuggle in a second copy.
    """
    if not entries_dir.is_dir():
        return None, checked

    for entry_file in sorted(entries_dir.glob("*.json")):
        try:
            parsed: object = json.loads(entry_file.read_text(encoding="utf-8"))
        except OSError as e:
            return _failure(
                entries_checked=checked,
                failure_index=-1,
                failure_type="READ_ERROR",
                expected="readable ledger entry file",
                found=str(e),
                message=f"Could not read ledger entry {entry_file}: {e}",
            ), checked
        except json.JSONDecodeError as e:
            return _failure(
                entries_checked=checked,
                failure_index=-1,
                failure_type="PARSE_ERROR",
                expected="valid JSON object",
                found=f"JSONDecodeError: {e}",
                message=f"Ledger entry {entry_file} is not valid JSON: {e}",
            ), checked

        if not isinstance(parsed, dict):
            return _failure(
                entries_checked=checked,
                failure_index=-1,
                failure_type="SCHEMA_ERROR",
                expected="entry to be a JSON object",
                found=type(parsed).__name__,
                message=(
                    f"Ledger entry {entry_file} is not a JSON object "
                    f"(got {type(parsed).__name__})."
                ),
            ), checked

        stored_hash: Any = parsed.get("entry_hash")
        if not isinstance(stored_hash, str):
            return _failure(
                entries_checked=checked,
                failure_index=-1,
                failure_type="SCHEMA_ERROR",
                expected="entry_hash field (string)",
                found=repr(stored_hash),
                message=f"Ledger entry {entry_file} is missing a string entry_hash.",
            ), checked

        recomputed: str = compute_entry_hash(parsed)
        if recomputed != stored_hash:
            return _failure(
                entries_checked=checked,
                failure_index=-1,
                failure_type="HASH_MISMATCH",
                expected=recomputed,
                found=stored_hash,
                message=(
                    f"Ledger entry {entry_file} has been tampered with: "
                    f"stored entry_hash does not match recomputed hash."
                ),
            ), checked

        if entry_file.stem != stored_hash:
            return _failure(
                entries_checked=checked,
                failure_index=-1,
                failure_type="FILENAME_MISMATCH",
                expected=f"{stored_hash}.json",
                found=entry_file.name,
                message=(
                    f"Ledger entry {entry_file.name} does not match the "
                    f"entry_hash it contains."
                ),
            ), checked

        if stored_hash in seen:
            return _failure(
                entries_checked=checked,
                failure_index=-1,
                failure_type="DUPLICATE_ENTRY",
                expected="each entry_hash to appear once",
                found=stored_hash,
                message=(
                    f"Entry {stored_hash} appears more than once across the "
                    f"ledger."
                ),
            ), checked

        seen[stored_hash] = parsed
        checked += 1

    return None, checked


def _verify_dag(seen: dict[str, dict], checked: int) -> tuple[dict | None, list[str]]:
    """Check the union forms one connected, fully-linked history.

    Returns ``(failure, tips)``. Every parent must resolve to a real entry, so
    a deleted non-tip file is caught; exactly one entry may claim GENESIS; and
    every entry must be reachable from it, so a subtree grafted on without a
    path back to the root cannot hide. Cycles cannot occur — a parent hash has
    to exist before a child can commit to it — and would be caught here anyway,
    since a cycle is unreachable from genesis.
    """
    genesis: list[str] = []
    for entry_hash, entry in seen.items():
        raw: Any = entry.get("previous_hash")

        # Only the exact sentinel is genesis. An empty list is not "no
        # parents", it is a link that names nothing, and accepting it would let
        # a malformed entry pose as a second root. Likewise every element must
        # be a string: _parents_of is lenient for readers and would quietly
        # drop a non-string, so a child could validate against only the
        # parents that happened to be well-formed.
        is_genesis: bool = raw == _GENESIS_MARKER
        malformed: bool = not is_genesis and not (
            (isinstance(raw, str) and raw)
            or (
                isinstance(raw, list)
                and raw
                and all(isinstance(p, str) and p for p in raw)
            )
        )
        if malformed:
            return _failure(
                entries_checked=checked,
                failure_index=-1,
                failure_type="SCHEMA_ERROR",
                expected=(
                    "previous_hash as GENESIS, a non-empty string, or a "
                    "non-empty list of strings"
                ),
                found=repr(raw),
                message=f"Entry {entry_hash} has a malformed previous_hash.",
            ), []

        parents: list[str] = _parents_of(entry)
        if is_genesis:
            genesis.append(entry_hash)
        for parent in parents:
            if parent not in seen:
                return _failure(
                    entries_checked=checked,
                    failure_index=-1,
                    failure_type="MISSING_PARENT",
                    expected=f"an entry with hash {parent}",
                    found="no such entry",
                    message=(
                        f"Entry {entry_hash} references parent {parent}, "
                        f"which is not present in the ledger."
                    ),
                ), []

    if len(genesis) != 1:
        return _failure(
            entries_checked=checked,
            failure_index=-1,
            failure_type=(
                "INVALID_GENESIS" if not genesis else "MULTIPLE_GENESIS"
            ),
            expected="exactly one genesis entry",
            found=f"{len(genesis)} genesis entries",
            message=(
                f"The ledger must have exactly one genesis entry, "
                f"found {len(genesis)}."
            ),
        ), []

    children: dict[str, list[str]] = {entry_hash: [] for entry_hash in seen}
    for entry_hash, entry in seen.items():
        for parent in _parents_of(entry):
            children[parent].append(entry_hash)

    reachable: set[str] = set()
    queue: list[str] = [genesis[0]]
    while queue:
        current: str = queue.pop()
        if current in reachable:
            continue
        reachable.add(current)
        queue.extend(children[current])

    if len(reachable) != len(seen):
        orphans: list[str] = sorted(set(seen) - reachable)
        return _failure(
            entries_checked=checked,
            failure_index=-1,
            failure_type="ORPHAN_ENTRY",
            expected="every entry reachable from genesis",
            found=f"{len(orphans)} unreachable entr(ies)",
            message=(
                f"Entries are not connected to the genesis entry: "
                f"{', '.join(orphans[:5])}"
            ),
        ), []

    referenced: set[str] = set()
    for entry in seen.values():
        referenced.update(_parents_of(entry))
    return None, sorted(set(seen) - referenced)


def _failure(
    *,
    entries_checked: int,
    failure_index: int,
    failure_type: str,
    expected: Any,
    found: Any,
    message: str,
) -> dict:
    """Emit a structured failure dict and log a one-line diagnostic."""
    print(
        f"[bench verify] FAIL entry={failure_index} type={failure_type}: "
        f"{message}",
        file=sys.stderr,
    )
    return {
        "valid": False,
        "entries_checked": entries_checked,
        "failure_index": failure_index,
        "failure_type": failure_type,
        "expected": expected,
        "found": found,
        "message": message,
    }
