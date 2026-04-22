"""Independent chain validator for the Bench ledger.

Reads ``ledger/bench-ledger.json`` directly and walks every entry,
recomputing each hash and confirming the ``previous_hash`` link holds
from the GENESIS entry through to the latest. This module deliberately
does not call ``load_ledger`` from ``chain.py`` — independence from the
write path is the whole point of an auditor. Only ``compute_entry_hash``
is shared, because the hashing algorithm must match by construction.

The validator reports the first failure it encounters (one bad entry is
enough to invalidate the chain) along with enough context to pinpoint
the tampered or missing entry.
"""

import json
import sys
from pathlib import Path
from typing import Any

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

    first_entry: dict = entries[0]
    last_entry: dict = entries[-1]
    return {
        "valid": True,
        "entries": len(entries),
        "first_entry": first_entry.get("timestamp", ""),
        "last_entry": last_entry.get("timestamp", ""),
        "genesis_hash": first_entry.get("entry_hash", ""),
        "latest_hash": last_entry.get("entry_hash", ""),
    }


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
