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

_BENCH_ROOT: Path = Path(__file__).resolve().parent.parent
_DEFAULT_LEDGER_PATH: str = str(_BENCH_ROOT / "ledger" / "bench-ledger.json")
_PROJECT_LEDGER_DIRNAME: str = ".bench"
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


def _project_root() -> Path:
    """The root of the project currently being governed.

    Ledger routing and out-of-project classification both anchor here, so a
    change can never be routed to one project's ledger while being judged
    against a different project's boundary. A working directory anywhere
    inside the Bench repo counts as Bench governing itself, which is why
    editing ``utils/api.py`` while sitting in ``tests/`` is still in-project.
    """
    try:
        cwd: Path = Path.cwd().resolve()
    except OSError as exc:
        # A deleted or unreadable CWD cannot be recovered here. Fall back to
        # Bench's own root so the verdict is still recorded somewhere rather
        # than lost (C-001: no silent swallowing).
        print(
            f"[bench ledger] cannot resolve working directory ({exc}); "
            f"treating Bench's own repo as the project root",
            file=sys.stderr,
        )
        return _BENCH_ROOT

    if cwd == _BENCH_ROOT or _BENCH_ROOT in cwd.parents:
        return _BENCH_ROOT
    return cwd


def resolve_ledger_path() -> str:
    """Resolve which ledger the current run's verdict belongs to.

    Bench's PreToolUse hook can be registered globally (in the user's
    ``~/.claude/settings.json``), in which case it governs every project on
    the machine. Routing all of those verdicts to Bench's own ledger mixes
    unrelated projects' diffs into one chain and, if that chain is committed
    to a public repository, publishes them. The ledger therefore follows the
    project being governed:

    1. ``BENCH_LEDGER_PATH`` wins outright, for an explicit central ledger.
    2. A working directory inside the Bench repo (Bench governing itself)
       uses Bench's own ``ledger/bench-ledger.json``, unchanged.
    3. Anything else writes to ``<project>/.bench/bench-ledger.json``.

    Claude Code invokes hooks with the governed project as the working
    directory. That is the same assumption ``utils.diff`` already relies on
    when normalizing paths that fall outside the Bench repo.
    """
    override: str = os.environ.get("BENCH_LEDGER_PATH", "").strip()
    if override:
        return override

    root: Path = _project_root()
    if root == _BENCH_ROOT:
        return _DEFAULT_LEDGER_PATH

    return str(root / _PROJECT_LEDGER_DIRNAME / "bench-ledger.json")


_EXTERNAL_BODY_KEYS: frozenset[str] = frozenset(
    {"content", "old_string", "new_string", "edits", "formatted_diff", "raw"}
)
_EXTERNAL_REDACTION_NOTE: str = (
    "Diff body omitted: file lies outside this ledger's project. "
    "Path and verdict are retained; the change itself was adjudicated in full."
)


def _is_external_change(file_ref: str) -> bool:
    """True when the governed file lies outside the project being governed.

    Anchored on ``_project_root()``, the same root ledger routing uses, so a
    change cannot be written to one project's ledger while being classified
    against another's boundary.

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


def load_ledger(path: str | None = None) -> list[dict]:
    """Load the ledger at ``path`` as a list of entries.

    ``path`` defaults to ``resolve_ledger_path()``, so readers follow the
    same project-scoped routing as writers.

    Returns an empty list when the file is absent. If the file exists but
    is unreadable, unparseable, or does not contain a JSON array, logs the
    problem to stderr and returns an empty list without touching the file
    on disk — preserving a corrupted ledger for forensic inspection rather
    than silently overwriting it.
    """
    file_path: Path = Path(path if path is not None else resolve_ledger_path())

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
    path: str | None = None,
) -> dict:
    """Append a governance verdict to the ledger and update ledger-meta.json.

    ``path`` defaults to ``resolve_ledger_path()``, which routes the verdict
    to the ledger of the project being governed rather than always to
    Bench's own. ``ledger-meta.json`` is written alongside whichever ledger
    is selected, so each chain carries its own anchor.

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

    existing: list[dict] = load_ledger(resolved)

    previous_hash: str = (
        existing[-1].get("entry_hash", _GENESIS_MARKER)
        if existing
        else _GENESIS_MARKER
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

    existing.append(entry)
    _atomic_write_json(file_path, existing)

    meta_path: Path = directory / META_FILENAME
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
