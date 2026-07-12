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
from ledger.chain import compute_entry_hash

_DEFAULT_LEDGER_PATH: str = "ledger/bench-ledger.json"
_GENESIS_MARKER: str = "GENESIS"


def verify_chain(path: str = _DEFAULT_LEDGER_PATH) -> dict:
    """Walk the ledger at ``path`` and return a verification summary.

    Returns a dict describing either a valid chain (with summary stats)
    or the first detected failure (with the index and the expected vs.
    found values). An empty or absent ledger is treated as trivially
    valid — there is nothing to tamper with.
    """
    file_path: Path = Path(path)

    if not file_path.exists():
        return {
            "valid": True,
            "entries": 0,
            "message": "No ledger found. Nothing to verify.",
        }

    try:
        raw: str = file_path.read_text(encoding="utf-8")
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
        return {
            "valid": True,
            "entries": 0,
            "message": "No ledger found. Nothing to verify.",
        }

    try:
        data: object = json.loads(raw)
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

    if len(data) == 0:
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

    meta_failure, meta_note = _check_meta_anchor(
        file_path.parent / _META_FILENAME, entries
    )
    if meta_failure is not None:
        return meta_failure

    first_entry: dict = entries[0]
    last_entry: dict = entries[-1]
    return {
        "valid": True,
        "entries": len(entries),
        "first_entry": first_entry.get("timestamp", ""),
        "last_entry": last_entry.get("timestamp", ""),
        "genesis_hash": first_entry.get("entry_hash", ""),
        "latest_hash": last_entry.get("entry_hash", ""),
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
