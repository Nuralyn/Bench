"""Constitution loader and snapshot for the Bench governance pipeline.

The constitution (bench.json) is loaded once per pipeline run as a frozen
snapshot. The SHA-256 hash is computed over the raw file string so any
byte-level change yields a distinct hash, recorded in every ledger entry.
"""

import hashlib
import json
from pathlib import Path


class ConstitutionError(Exception):
    """Base class for constitution loading and validation failures."""


class ConstitutionNotFoundError(ConstitutionError):
    """Raised when the constitution file cannot be located or read."""


class ConstitutionParseError(ConstitutionError):
    """Raised when the constitution file is not valid JSON."""


class ConstitutionSchemaError(ConstitutionError):
    """Raised when the constitution JSON is missing required fields or types."""


_REQUIRED_TOP_LEVEL: tuple[str, ...] = ("constitution", "version", "constraints")
_REQUIRED_CONSTRAINT_FIELDS: tuple[str, ...] = ("id", "name", "rule", "severity")


def load_constitution_snapshot(path: str = "bench.json") -> tuple[dict, str]:
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


def get_constraint_by_id(constitution: dict, constraint_id: str) -> dict | None:
    """Return the constraint dict matching constraint_id, or None if absent."""
    for constraint in constitution.get("constraints", []):
        if isinstance(constraint, dict) and constraint.get("id") == constraint_id:
            return constraint
    return None
