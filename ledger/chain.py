"""Hash-chained append-only ledger for Bench governance verdicts.

Every pipeline run (PASS or VETO) lands here as a JSON entry whose
``entry_hash`` is the SHA-256 of its own serialized fields and whose
``previous_hash`` links to the prior entry. The first entry uses the
sentinel ``"GENESIS"`` for ``previous_hash``. The chain is tamper-evident:
any modification to a historical entry invalidates every hash after it
(C-008 ledger immutability).

Writes are atomic via ``os.replace`` on a same-directory temp file, so a
crash mid-write cannot leave a half-written JSON array on disk. The
sibling ``ledger-meta.json`` is kept in sync on every append.

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

_DEFAULT_LEDGER_PATH: str = "ledger/bench-ledger.json"
_META_FILENAME: str = "ledger-meta.json"
_GENESIS_MARKER: str = "GENESIS"
_MAX_FIELD_CHARS: int = 10_000
_MAX_STAGE_CHARS: int = 50_000


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


def load_ledger(path: str = _DEFAULT_LEDGER_PATH) -> list[dict]:
    """Load the ledger at ``path`` as a list of entries.

    Returns an empty list when the file is absent. If the file exists but
    is unreadable, unparseable, or does not contain a JSON array, logs the
    problem to stderr and returns an empty list without touching the file
    on disk — preserving a corrupted ledger for forensic inspection rather
    than silently overwriting it.
    """
    file_path: Path = Path(path)

    if not file_path.exists():
        return []

    try:
        raw: str = file_path.read_text(encoding="utf-8")
    except OSError as e:
        print(f"[bench ledger] cannot read ledger {file_path}: {e}", file=sys.stderr)
        return []

    try:
        data: object = json.loads(raw)
    except json.JSONDecodeError as e:
        print(
            f"[bench ledger] corrupted ledger at {file_path}: {e}",
            file=sys.stderr,
        )
        return []

    if not isinstance(data, list):
        print(
            f"[bench ledger] ledger at {file_path} is not a JSON array "
            f"(got {type(data).__name__}); returning empty",
            file=sys.stderr,
        )
        return []

    return data


def append_entry(
    pipeline_result: dict,
    path: str = _DEFAULT_LEDGER_PATH,
) -> dict:
    """Append a governance verdict to the ledger and update ledger-meta.json.

    Expects ``pipeline_result`` to include the standard runner keys
    (``constitution_hash``, ``challenger``, ``defender``, ``oracle``) and a
    ``change`` dict with ``file``, ``tool``, ``diff_summary``. Missing
    fields fall back to safe defaults so the ledger never fails to record
    a verdict because of an upstream shape drift.

    Returns the full new entry (including its computed ``entry_hash``).
    """
    file_path: Path = Path(path)
    directory: Path = file_path.parent
    directory.mkdir(parents=True, exist_ok=True)

    existing: list[dict] = load_ledger(path)

    previous_hash: str = (
        existing[-1].get("entry_hash", _GENESIS_MARKER)
        if existing
        else _GENESIS_MARKER
    )

    change_in: dict = pipeline_result.get("change") or {}
    timestamp: str = datetime.now(timezone.utc).isoformat()

    entry: dict[str, Any] = {
        "entry_id": str(uuid.uuid4()),
        "timestamp": timestamp,
        "previous_hash": previous_hash,
        "constitution_hash": pipeline_result.get("constitution_hash", ""),
        "change": {
            "file": change_in.get("file", "unknown"),
            "tool": change_in.get("tool", "unknown"),
            "diff_summary": change_in.get(
                "diff_summary",
                change_in.get("raw", {}),
            ),
        },
        "challenger": _cap_stage_fields(pipeline_result.get("challenger", {})),
        "defender": _cap_stage_fields(pipeline_result.get("defender", {})),
        "oracle": _cap_stage_fields(pipeline_result.get("oracle", {})),
    }
    entry["entry_hash"] = compute_entry_hash(entry)

    existing.append(entry)
    _atomic_write_json(file_path, existing)

    meta_path: Path = directory / _META_FILENAME
    _update_meta(meta_path, entry, len(existing))

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


def _update_meta(meta_path: Path, entry: dict, entry_count: int) -> None:
    """Refresh ledger-meta.json with counts and the latest hash."""
    existing_meta: dict[str, Any] = {}
    if meta_path.exists():
        try:
            raw: str = meta_path.read_text(encoding="utf-8")
            parsed: object = json.loads(raw)
            if isinstance(parsed, dict):
                existing_meta = parsed
            else:
                print(
                    f"[bench ledger] meta file {meta_path} is not a JSON "
                    f"object (got {type(parsed).__name__}); rebuilding",
                    file=sys.stderr,
                )
        except (OSError, json.JSONDecodeError) as e:
            print(
                f"[bench ledger] meta file {meta_path} unreadable: {e}; "
                "rebuilding",
                file=sys.stderr,
            )

    meta: dict[str, Any] = {
        "entry_count": entry_count,
        "latest_hash": entry["entry_hash"],
        "created": existing_meta.get("created", entry["timestamp"]),
        "last_updated": entry["timestamp"],
    }
    _atomic_write_json(meta_path, meta)
