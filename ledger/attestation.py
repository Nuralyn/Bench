"""Public attestation export: commitments without content.

The operational ledger is private because it records content. Every entry
embeds the full diff body of the change it governs plus the three stages'
prose about it, so publishing the chain publishes every change it ever saw.
That is why Bench's chain is gitignored and why three unrelated projects
once ended up in a public repository.

A ledger that records *commitments* can be public safely, because a hash
proves something happened without revealing what. This module produces that
artifact. It is evidence of an immutable commitment, not proof that the
ruling behind it was correct, and it is emphatically not a backup: nothing
here can reconstruct a diff, a path, or a line of source.

**Checkpoint model.** An attestation is generated at a deliberate
checkpoint, never continuously, and declares a fixed ``cutoff_commitment``
covering entries up to and including it. Committing the artifact is itself a
governed edit that appends a new entry, which is necessarily after the
cutoff and therefore belongs to the *next* checkpoint. The recursion
terminates because the cutoff is fixed when the export runs and the document
never claims to represent the live tip.

**Every field is validated on emit.** The exporter builds each record field
by field from an allowlist and validates the assembled record before
writing; it never copies an input dict through. That matters because entry
data is model-authored: ``challenger.findings[].constraint_id`` contains
strings like ``"process (CLAUDE.md Rule 15, not a numbered C-XXX
constraint)"`` in this very chain. Constraint ids are therefore sourced only
from ``oracle.constraint_citations``, and validated even so.

**Two failure modes, deliberately different.** A structural failure aborts
the whole export with a typed error, because a partial attestation is worse
than none. An unmappable constraint id is excluded and counted in
``unmapped_citation_count``, because aborting there would make export
impossible on the real chain while dropping it silently would violate C-001.

Excluded by name, never emitted: everything under ``change`` (diffs, file
paths, tool names), ``oracle.reasoning``, ``oracle.advisories``,
``oracle.remediation``, ``oracle.confidence``, ``oracle.raw_response``,
``challenger.*``, ``defender.*``, ``_tokens``, ``entry_id``, and
``constitution_sources`` (which carries absolute machine paths).
"""

import json
import re
from typing import Any

SCHEMA_VERSION: str = "1"

_VERDICTS: frozenset[str] = frozenset(
    {"PASS", "VETO", "ANCHOR", "SANITATION"}
)
_SHA256_RE: re.Pattern = re.compile(r"^[0-9a-f]{64}$")
_CONSTRAINT_RE: re.Pattern = re.compile(r"^[CP]-\d{3}$")
_VERSION_RE: re.Pattern = re.compile(r"^\d+\.\d+\.\d+$")
_TIMESTAMP_RE: re.Pattern = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$"
)

_MAX_UNMAPPED: int = 999

_RECORD_FIELDS: tuple[str, ...] = (
    "seq",
    "timestamp",
    "verdict",
    "pipeline_error",
    "commitment",
    "previous_commitment",
    "constitution_commitment",
    "constraint_ids",
    "unmapped_citation_count",
)

_HEADER_FIELDS: tuple[str, ...] = (
    "schema_version",
    "bench_version",
    "cutoff_commitment",
    "cutoff_timestamp",
    "record_count",
    "records",
)


class AttestationError(Exception):
    """A structural defect that makes the export unsound.

    Raised rather than returned so a partial artifact cannot be written and
    then mistaken for a complete one.
    """


def _iso_seconds(raw: Any) -> str:
    """Normalize a stored timestamp to whole-second UTC.

    Stored timestamps carry microseconds and a ``+00:00`` offset. Sub-second
    precision is dropped rather than reformatted: it conveys nothing an
    auditor needs and is a needlessly fine side channel.
    """
    if not isinstance(raw, str) or len(raw) < 19:
        raise AttestationError(f"entry timestamp is unusable: {raw!r}")
    candidate: str = raw[:19] + "Z"
    if not _TIMESTAMP_RE.match(candidate):
        raise AttestationError(f"entry timestamp is unusable: {raw!r}")
    return candidate


def _previous_commitments(raw: Any) -> list[str]:
    """Normalize every stored parent shape to one array type.

    Storage has four: the literal ``GENESIS``, a bare string from the legacy
    segment, a one-element list, and, for one real entry, a two-element list
    left by a git-merge fork reconciliation. Consumers should handle one
    shape, and collapsing to a scalar would be lossy on that two-parent
    entry, so genesis becomes an empty array and everything else an array of
    hashes.
    """
    if raw == "GENESIS":
        return []
    values: list[Any] = raw if isinstance(raw, list) else [raw]
    out: list[str] = []
    for value in values:
        if not isinstance(value, str) or not _SHA256_RE.match(value):
            raise AttestationError(f"unusable previous_hash element: {value!r}")
        out.append(value)
    return sorted(out)


def _constraint_ids(entry: dict) -> tuple[list[str], int]:
    """Validated constraint ids and the count that could not be mapped."""
    oracle: Any = entry.get("oracle")
    citations: Any = (oracle or {}).get("constraint_citations")
    if not isinstance(citations, list):
        return [], 0

    ids: set[str] = set()
    unmapped: int = 0
    for citation in citations:
        value: Any = (
            citation.get("constraint_id")
            if isinstance(citation, dict)
            else citation
        )
        if isinstance(value, str) and _CONSTRAINT_RE.match(value):
            ids.add(value)
        else:
            unmapped += 1
    return sorted(ids), min(unmapped, _MAX_UNMAPPED)


def _build_record(seq: int, entry: dict) -> dict[str, Any]:
    """One attestation record, assembled field by field from an allowlist."""
    commitment: Any = entry.get("entry_hash")
    if not isinstance(commitment, str) or not _SHA256_RE.match(commitment):
        raise AttestationError(f"entry {seq} has an unusable entry_hash")

    constitution: Any = entry.get("constitution_hash")
    if not isinstance(constitution, str) or not _SHA256_RE.match(constitution):
        raise AttestationError(
            f"entry {seq} has an unusable constitution_hash"
        )

    verdict: Any = entry.get("verdict")
    if verdict not in _VERDICTS:
        raise AttestationError(
            f"entry {seq} has an unrecognized verdict {verdict!r}; refusing "
            f"rather than emitting an unvalidated value"
        )

    ids, unmapped = _constraint_ids(entry)
    return {
        "seq": seq,
        "timestamp": _iso_seconds(entry.get("timestamp")),
        "verdict": verdict,
        "pipeline_error": bool(entry.get("pipeline_error")),
        "commitment": commitment,
        "previous_commitment": _previous_commitments(
            entry.get("previous_hash")
        ),
        "constitution_commitment": constitution,
        "constraint_ids": ids,
        "unmapped_citation_count": unmapped,
    }


def validate_document(document: Any) -> list[str]:
    """Defects in a built attestation, empty when it conforms.

    Run against the assembled document before it is written, so a field that
    slipped past construction cannot reach the artifact. Treats every string
    as a possible exfiltration channel: nothing is accepted because it looks
    reasonable, only because it matches its pattern.
    """
    if not isinstance(document, dict):
        return [f"document is not an object (got {type(document).__name__})"]

    defects: list[str] = []
    extra: set[str] = set(document) - set(_HEADER_FIELDS)
    if extra:
        defects.append(f"unexpected header fields: {sorted(extra)}")
    for field in _HEADER_FIELDS:
        if field not in document:
            defects.append(f"missing header field {field!r}")

    if document.get("schema_version") != SCHEMA_VERSION:
        defects.append(f"schema_version must be {SCHEMA_VERSION!r}")
    version: Any = document.get("bench_version")
    if not isinstance(version, str) or not _VERSION_RE.match(version):
        defects.append(f"bench_version must be x.y.z, got {version!r}")
    cutoff: Any = document.get("cutoff_commitment")
    if not isinstance(cutoff, str) or not _SHA256_RE.match(cutoff):
        defects.append("cutoff_commitment must be a 64-hex hash")
    stamp: Any = document.get("cutoff_timestamp")
    if not isinstance(stamp, str) or not _TIMESTAMP_RE.match(stamp):
        defects.append("cutoff_timestamp must be YYYY-MM-DDTHH:MM:SSZ")

    records: Any = document.get("records")
    if not isinstance(records, list):
        defects.append("records must be a list")
        return defects
    if document.get("record_count") != len(records):
        defects.append("record_count does not match the number of records")

    for index, record in enumerate(records):
        defects.extend(_validate_record(index, record))
    return defects


def _validate_record(index: int, record: Any) -> list[str]:
    """Defects in one record, empty when it conforms."""
    if not isinstance(record, dict):
        return [f"records[{index}] is not an object"]

    defects: list[str] = []
    extra: set[str] = set(record) - set(_RECORD_FIELDS)
    if extra:
        defects.append(f"records[{index}] has unexpected fields {sorted(extra)}")
    for field in _RECORD_FIELDS:
        if field not in record:
            defects.append(f"records[{index}] missing {field!r}")
    if defects:
        return defects

    if record["seq"] != index:
        defects.append(f"records[{index}].seq is {record['seq']}, expected {index}")
    if not _TIMESTAMP_RE.match(str(record["timestamp"])):
        defects.append(f"records[{index}].timestamp is not whole-second UTC")
    if record["verdict"] not in _VERDICTS:
        defects.append(f"records[{index}].verdict is not a known verdict")
    if not isinstance(record["pipeline_error"], bool):
        defects.append(f"records[{index}].pipeline_error is not a boolean")
    for field in ("commitment", "constitution_commitment"):
        if not _SHA256_RE.match(str(record[field])):
            defects.append(f"records[{index}].{field} is not a 64-hex hash")
    parents: Any = record["previous_commitment"]
    if not isinstance(parents, list) or any(
        not isinstance(p, str) or not _SHA256_RE.match(p) for p in parents
    ):
        defects.append(f"records[{index}].previous_commitment is not hashes")
    ids: Any = record["constraint_ids"]
    if not isinstance(ids, list) or any(
        not isinstance(i, str) or not _CONSTRAINT_RE.match(i) for i in ids
    ):
        defects.append(f"records[{index}].constraint_ids has a non-id value")
    count: Any = record["unmapped_citation_count"]
    if (
        not isinstance(count, int)
        or isinstance(count, bool)
        or not 0 <= count <= _MAX_UNMAPPED
    ):
        defects.append(f"records[{index}].unmapped_citation_count out of range")
    return defects


def build_attestation(
    entries: list[dict], cutoff: str, bench_version: str
) -> dict[str, Any]:
    """Build the attestation for entries up to and including ``cutoff``.

    Raises ``AttestationError`` on any structural defect, including a cutoff
    that names no entry, so a checkpoint cannot silently cover less than the
    caller asked for.
    """
    if not _SHA256_RE.match(cutoff or ""):
        raise AttestationError(f"cutoff must be a 64-hex commitment: {cutoff!r}")
    if not _VERSION_RE.match(bench_version or ""):
        raise AttestationError(
            f"bench_version must be x.y.z, got {bench_version!r}"
        )

    covered: list[dict] = []
    found: bool = False
    for entry in entries:
        covered.append(entry)
        if entry.get("entry_hash") == cutoff:
            found = True
            break
    if not found:
        raise AttestationError(
            f"cutoff {cutoff[:12]} names no entry in this chain; a checkpoint "
            f"must declare a boundary that exists"
        )

    records: list[dict[str, Any]] = [
        _build_record(seq, entry) for seq, entry in enumerate(covered)
    ]
    document: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "bench_version": bench_version,
        "cutoff_commitment": cutoff,
        "cutoff_timestamp": records[-1]["timestamp"],
        "record_count": len(records),
        "records": records,
    }

    defects: list[str] = validate_document(document)
    if defects:
        raise AttestationError(
            "attestation failed its own validation: " + "; ".join(defects)
        )
    return document


def render(document: dict[str, Any]) -> str:
    """Serialize deterministically, so two runs are byte-identical."""
    return json.dumps(document, indent=2, sort_keys=True) + "\n"
