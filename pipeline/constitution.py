"""Constitution loader and snapshot for the Bench governance pipeline.

The constitution (bench.json) is loaded once per pipeline run as a frozen
snapshot. The SHA-256 hash is computed over the raw file string so any
byte-level change yields a distinct hash, recorded in every ledger entry.
"""

import hashlib
import json
import os
from pathlib import Path

from utils.project import governs_bench_itself, project_root


class ConstitutionError(Exception):
    """Base class for constitution loading and validation failures."""


class ConstitutionNotFoundError(ConstitutionError):
    """Raised when the constitution file cannot be located or read."""


class ConstitutionParseError(ConstitutionError):
    """Raised when the constitution file is not valid JSON."""


class ConstitutionSchemaError(ConstitutionError):
    """Raised when the constitution JSON is missing required fields or types."""


class ConstitutionFloorError(ConstitutionError):
    """Raised when a project layer tries to weaken Bench's core constraints.

    A governed project may extend the constitution, never erode it: it can add
    its own constraints and raise a core severity, but it cannot remove a core
    constraint, downgrade one, or reword a core rule. An attempt to do so is a
    hard failure rather than a silently ignored line, so an author is never
    left believing they changed a rule that is in fact still in force.
    """


_REQUIRED_TOP_LEVEL: tuple[str, ...] = ("constitution", "version", "constraints")
_REQUIRED_CONSTRAINT_FIELDS: tuple[str, ...] = ("id", "name", "rule", "severity")


# Bench's own constitution, resolved absolutely from this file's location.
#
# The default was the bare relative "bench.json", which resolves against the
# working directory. pipeline/runner.py always passes an absolute path, so the
# pipeline was unaffected — but cli/commands.py calls this with no argument, so
# `python -m cli constitution` read whatever bench.json happened to sit in the
# cwd. Inside the Bench repo the two coincide and the split is invisible; from
# any other project the auditor displayed a different constitution than the one
# the pipeline enforced. Anchoring the default to this file removes that split
# at the source, so every caller sees one constitution.
_BENCH_ROOT: Path = Path(__file__).resolve().parent.parent
_DEFAULT_CONSTITUTION_PATH: str = str(_BENCH_ROOT / "bench.json")

# Core constraints are Bench's own (C-001..C-008). A governed project's own
# constraints live in a reserved namespace so the two can never be confused and
# a project cannot shadow a core id by reusing it.
_CORE_ID_PREFIX: str = "C-"
_PROJECT_ID_PREFIX: str = "P-"

# Severity ordering, low to high. A project layer may move a core constraint up
# this scale, never down.
_SEVERITY_RANK: dict[str, int] = {"warning": 0, "veto": 1}


def resolve_constitution_path() -> str | None:
    """Resolve the project's own constitution layer, or None if there is none.

    Bench's core constitution always applies. This resolves the optional
    project layer stacked on top of it:

    1. ``BENCH_CONSTITUTION_PATH`` wins outright, for an explicit layer.
    2. A working directory inside the Bench repo is Bench governing itself,
       which has no separate layer - the core file already is its constitution.
    3. Anything else uses ``<project>/bench.json`` when that file exists.

    Returning None means "core only". That is a safe default here in a way it
    would not be under replace semantics: the core is the floor, so a project
    without a layer is governed by the full set of core constraints rather than
    by a weakened one. Anchored on the same ``utils.project.project_root`` that
    ledger routing uses, so a change cannot be judged against one project's
    constitution while being recorded in another project's ledger.
    """
    override: str = os.environ.get("BENCH_CONSTITUTION_PATH", "").strip()
    if override:
        return override

    if governs_bench_itself():
        return None

    candidate: Path = project_root() / "bench.json"
    return str(candidate) if candidate.is_file() else None


def load_constitution_snapshot(
    path: str = _DEFAULT_CONSTITUTION_PATH,
) -> tuple[dict, str]:
    """Load and validate the constitution, returning (parsed_data, sha256_hex).

    The hash is computed over the raw file content (UTF-8 bytes) before
    parsing, so it captures the exact authored bytes — not a re-serialized
    canonical form.
    """
    file_path: Path = Path(path)

    if not file_path.exists():
        raise ConstitutionNotFoundError(
            f"Constitution file not found: {file_path}"
        )

    try:
        raw: str = file_path.read_text(encoding="utf-8")
    except OSError as e:
        raise ConstitutionNotFoundError(
            f"Failed to read constitution file {file_path}: {e}"
        ) from e

    constitution_hash: str = hashlib.sha256(raw.encode("utf-8")).hexdigest()

    try:
        data: object = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ConstitutionParseError(
            f"Constitution file {file_path} is not valid JSON: {e}"
        ) from e

    if not isinstance(data, dict):
        raise ConstitutionSchemaError(
            f"Constitution root must be a JSON object, got {type(data).__name__}"
        )

    for field in _REQUIRED_TOP_LEVEL:
        if field not in data:
            raise ConstitutionSchemaError(
                f"Constitution missing required top-level field: '{field}'"
            )

    constraints: object = data["constraints"]
    if not isinstance(constraints, list):
        raise ConstitutionSchemaError(
            f"'constraints' must be a list, got {type(constraints).__name__}"
        )

    for index, constraint in enumerate(constraints):
        if not isinstance(constraint, dict):
            raise ConstitutionSchemaError(
                f"constraints[{index}] must be a JSON object, "
                f"got {type(constraint).__name__}"
            )
        for field in _REQUIRED_CONSTRAINT_FIELDS:
            if field not in constraint:
                raise ConstitutionSchemaError(
                    f"constraints[{index}] missing required field: '{field}'"
                )
            value: object = constraint[field]
            if not isinstance(value, str) or not value:
                raise ConstitutionSchemaError(
                    f"constraints[{index}].{field} must be a non-empty string"
                )

    return data, constitution_hash


def merge_constitutions(core: dict, project: dict) -> dict:
    """Stack a project layer on Bench's core constitution: floor plus extend.

    The core is a floor. A project layer may add its own constraints in the
    reserved ``P-`` namespace and may raise a core constraint's severity via
    ``severity_overrides``. It may not remove a core constraint, downgrade one,
    reword a core rule, or reuse a core id.

    Every rejected case raises rather than being dropped: a silently ignored
    line would leave an author believing they had changed a rule that is in
    fact still in force. Raises ConstitutionFloorError for erosion attempts and
    ConstitutionSchemaError for structurally invalid layers.
    """
    core_constraints: list[dict] = [
        c for c in core.get("constraints", []) if isinstance(c, dict)
    ]
    core_ids: set[str] = {str(c.get("id", "")) for c in core_constraints}

    raw_additions: object = project.get("constraints", [])
    if not isinstance(raw_additions, list):
        raise ConstitutionSchemaError(
            f"project 'constraints' must be a list, got "
            f"{type(raw_additions).__name__}"
        )

    additions: list[dict] = []
    seen: set[str] = set()
    for constraint in raw_additions:
        if not isinstance(constraint, dict):
            raise ConstitutionSchemaError(
                f"project constraint must be an object, got "
                f"{type(constraint).__name__}"
            )
        cid: str = str(constraint.get("id", ""))
        if not cid.startswith(_PROJECT_ID_PREFIX):
            raise ConstitutionFloorError(
                f"project constraint {cid!r} must use the reserved "
                f"{_PROJECT_ID_PREFIX}* namespace; {_CORE_ID_PREFIX}* ids are "
                f"Bench's core and cannot be redefined by a project"
            )
        if cid in seen:
            raise ConstitutionSchemaError(
                f"project defines constraint {cid!r} more than once"
            )
        missing: list[str] = [
            field
            for field in _REQUIRED_CONSTRAINT_FIELDS
            if field not in constraint
        ]
        if missing:
            raise ConstitutionSchemaError(
                f"project constraint {cid!r} is missing required field(s): "
                f"{', '.join(missing)}"
            )
        severity: str = str(constraint.get("severity", ""))
        if severity not in _SEVERITY_RANK:
            raise ConstitutionSchemaError(
                f"project constraint {cid!r} has unknown severity "
                f"{severity!r}; expected one of {sorted(_SEVERITY_RANK)}"
            )
        seen.add(cid)
        additions.append(constraint)

    raw_overrides: object = project.get("severity_overrides", {})
    if not isinstance(raw_overrides, dict):
        raise ConstitutionSchemaError(
            f"project 'severity_overrides' must be an object, got "
            f"{type(raw_overrides).__name__}"
        )

    unknown: set[str] = set(raw_overrides) - core_ids
    if unknown:
        raise ConstitutionFloorError(
            f"severity_overrides names unknown core constraint(s): "
            f"{', '.join(sorted(unknown))}"
        )

    merged_core: list[dict] = []
    for constraint in core_constraints:
        cid = str(constraint.get("id", ""))
        if cid not in raw_overrides:
            merged_core.append(constraint)
            continue

        requested: str = str(raw_overrides[cid])
        if requested not in _SEVERITY_RANK:
            raise ConstitutionSchemaError(
                f"severity_overrides[{cid!r}] has unknown severity "
                f"{requested!r}; expected one of {sorted(_SEVERITY_RANK)}"
            )
        current: str = str(constraint.get("severity", ""))
        if _SEVERITY_RANK[requested] <= _SEVERITY_RANK.get(current, 0):
            raise ConstitutionFloorError(
                f"severity_overrides[{cid!r}] would move {current!r} -> "
                f"{requested!r}; a project layer may only raise a core "
                f"severity, never lower or restate it"
            )
        raised: dict = dict(constraint)
        raised["severity"] = requested
        raised["severity_raised_by_project"] = True
        merged_core.append(raised)

    return {
        "constitution": (
            f"{core.get('constitution', 'bench')}"
            f"+{project.get('constitution', 'project')}"
        ),
        "version": core.get("version"),
        "project_version": project.get("version"),
        "constraints": merged_core + additions,
    }


def load_governing_constitution() -> tuple[dict, str, list[dict]]:
    """Load the constitution governing this run, with its project layer.

    Returns ``(constitution, hash, sources)``. Sources record the layer, path,
    and raw hash of each file that contributed, so an auditor reading a ledger
    entry can see *which files* ruled rather than only a digest.

    The returned hash is the SHA-256 of the contributing files' raw hashes. A
    merged document has no authored bytes of its own, so hashing a
    re-serialization would break the guarantee that the digest reflects exactly
    what someone wrote; chaining the raw per-file hashes preserves it.

    Failure modes differ deliberately. An absent project layer is fine - the
    core floor applies in full. A layer that is present but malformed, or that
    tries to erode the core, raises: the author intended additional
    constraints, and Bench cannot tell which, so the run fails closed.
    """
    core, core_hash = load_constitution_snapshot(_DEFAULT_CONSTITUTION_PATH)
    sources: list[dict] = [
        {
            "layer": "core",
            "path": _DEFAULT_CONSTITUTION_PATH,
            "sha256": core_hash,
        }
    ]

    layer_path: str | None = resolve_constitution_path()
    if layer_path is None:
        return core, core_hash, sources

    project, project_hash = load_constitution_snapshot(layer_path)
    merged: dict = merge_constitutions(core, project)
    sources.append(
        {"layer": "project", "path": layer_path, "sha256": project_hash}
    )
    merged_hash: str = hashlib.sha256(
        f"{core_hash}\n{project_hash}".encode("utf-8")
    ).hexdigest()
    return merged, merged_hash, sources


def get_constraint_by_id(constitution: dict, constraint_id: str) -> dict | None:
    """Return the constraint dict matching constraint_id, or None if absent."""
    for constraint in constitution.get("constraints", []):
        if isinstance(constraint, dict) and constraint.get("id") == constraint_id:
            return constraint
    return None
