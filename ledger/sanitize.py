"""Validation and audit for published-copy sanitation.

This is **not** an exception to C-008, and it does not remove anything from
the ledger. C-008 governs the authoritative append-only chain at
``resolve_ledger_path()``. Sanitation never touches that chain, never reads
it for writing, and is invalid by definition if the chain fails
``verify_chain`` afterwards. What it concerns is a different object.

Before the ledger became private, Bench committed its chain to a public
repository, and git retained those commits. Those git objects are a
*publication* of ledger data, not the ledger: nothing reads them, nothing
appends to them, and `resolve_ledger_path()` has never pointed at them.
Deleting a published copy is therefore outside C-008's immutability scope
in the same way that deleting a printed copy of a document is not an edit
to the document.

Retirement cannot reach them. The chain retired on 2026-07-24 was retired
because it held unpublishable third-party content, and its anchor records
that 264 of its entries had already been published. Retirement archived the
chain and opened a successor; the published copies stayed exactly where
they were.

Being outside C-008's scope is not a licence to act unaccountably, which is
the whole reason this module exists. A sanitation is recorded in the live
chain and must satisfy every requirement below, so a reader can check what
was removed and confirm the chain it spared is intact.

Whole files only. Sanitation never edits, reorders, or partially rewrites an
entry in any copy. That operation is what C-008 forbids without exception,
and Bench correctly vetoed it when it was first attempted here.

Two deliberate differences from retirement's evidence requirements:

* The backup is identified by an **opaque id and a digest**, never by a
  filesystem path. A path is not evidence, since it can rot, move, or be
  fabricated, while a digest proves integrity wherever the artifact lives.
  A path recorded in the chain would also reintroduce the machine-path
  leak that motivated the privacy boundary in the first place.
* Retention is **owned by a named human** rather than mandated as
  indefinite, because the backup holds the very content being purged.
  Requiring it to live forever would defeat the purpose of the operation.

This module validates and audits. It does not perform the rewrite: that is
a human action at a plain TTY, and nothing here should make it reachable
from inside an agent session.
"""

import hashlib
import re
import sys
from pathlib import Path
from typing import Any

SANITATION_EVENT: str = "published_copy_sanitation"
SANITATION_VERDICT: str = "SANITATION"
DIGEST_ALGORITHM: str = "sha256"

_SHA256_RE: re.Pattern = re.compile(r"^[0-9a-f]{64}$")
_GIT_SHA_RE: re.Pattern = re.compile(r"^[0-9a-f]{40}$")
_BACKUP_ID_RE: re.Pattern = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{2,63}$")
# Matches a Windows drive path or a POSIX absolute path. Used to refuse a
# record that smuggles a machine path into a field meant to be opaque.
_PATH_RE: re.Pattern = re.compile(r"(^|[\s\"'])([A-Za-z]:[\\/]|/[A-Za-z0-9_.-]+/)")

_REQUIRED_FIELDS: tuple[str, ...] = (
    "event",
    "human_decision",
    "reason",
    "refs",
    "backup_id",
    "backup_digest_algorithm",
    "backup_digest",
    "retention_owner",
    "retention_policy",
    "chain_verified_valid",
    "chain_genesis_hash",
    "chain_entry_count",
)

_OPAQUE_FIELDS: tuple[str, ...] = (
    "backup_id",
    "retention_owner",
)


def _validate_refs(refs: Any) -> list[str]:
    """Defects in the rewritten-ref list, empty when it conforms."""
    if not isinstance(refs, list) or not refs:
        return ["refs must be a non-empty list of rewritten references"]

    defects: list[str] = []
    for index, ref in enumerate(refs):
        if not isinstance(ref, dict):
            defects.append(f"refs[{index}] is not an object")
            continue
        name: Any = ref.get("ref")
        if not isinstance(name, str) or not name.startswith("refs/"):
            defects.append(
                f"refs[{index}].ref must be a full refname like "
                f"'refs/heads/main', got {name!r}"
            )
        for side in ("pre_image", "post_image"):
            value: Any = ref.get(side)
            if not isinstance(value, str) or not _GIT_SHA_RE.match(value):
                defects.append(
                    f"refs[{index}].{side} must be a 40-character git sha, "
                    f"got {value!r}"
                )
        if (
            isinstance(ref.get("pre_image"), str)
            and ref.get("pre_image") == ref.get("post_image")
        ):
            defects.append(
                f"refs[{index}] pre_image equals post_image: a ref that did "
                f"not change is not evidence of a rewrite"
            )
    return defects


def validate_sanitation_summary(summary: Any) -> list[str]:
    """Defects in a sanitation summary, empty when it conforms.

    Callable before the record is appended, which is the only point where a
    defect can still prevent a non-conforming sanitation from being recorded
    as though it satisfied the constraint.
    """
    if not isinstance(summary, dict):
        return [f"summary is not an object (got {type(summary).__name__})"]

    defects: list[str] = []
    for field in _REQUIRED_FIELDS:
        if field not in summary:
            defects.append(f"missing required field {field!r}")
        elif summary[field] in ("", None):
            defects.append(f"field {field!r} is empty")

    if summary.get("event") != SANITATION_EVENT:
        defects.append(
            f"event must be {SANITATION_EVENT!r}, got {summary.get('event')!r}"
        )

    digest: Any = summary.get("backup_digest")
    if not isinstance(digest, str) or not _SHA256_RE.match(digest):
        defects.append(
            f"backup_digest must be a 64-character sha256 hex digest, "
            f"got {digest!r}"
        )
    if summary.get("backup_digest_algorithm") != DIGEST_ALGORITHM:
        defects.append(
            f"backup_digest_algorithm must be {DIGEST_ALGORITHM!r}"
        )

    backup_id: Any = summary.get("backup_id")
    if isinstance(backup_id, str) and not _BACKUP_ID_RE.match(backup_id):
        defects.append(
            f"backup_id must be an opaque identifier, not a path or free "
            f"text, got {backup_id!r}"
        )

    # C-008 requires the authoritative chain to be untouched. A sanitation
    # recorded while it does not verify would assert the one thing the
    # operation is not allowed to change.
    if summary.get("chain_verified_valid") is not True:
        defects.append(
            "chain_verified_valid must be true: sanitation may not leave the "
            "authoritative chain failing verify_chain"
        )
    genesis: Any = summary.get("chain_genesis_hash")
    if not isinstance(genesis, str) or not _SHA256_RE.match(genesis):
        defects.append(
            f"chain_genesis_hash must be a 64-character hash, got {genesis!r}"
        )
    count: Any = summary.get("chain_entry_count")
    if not isinstance(count, int) or isinstance(count, bool) or count < 1:
        defects.append(
            f"chain_entry_count must be a positive integer, got {count!r}"
        )

    for field in _OPAQUE_FIELDS:
        value: Any = summary.get(field)
        if isinstance(value, str) and _PATH_RE.search(value):
            defects.append(
                f"field {field!r} looks like a filesystem path; sanitation "
                f"records carry an opaque identifier so the chain does not "
                f"republish machine paths"
            )

    defects.extend(_validate_refs(summary.get("refs")))
    return defects


def validate_sanitation_record(entry: Any) -> list[str]:
    """Defects in a whole sanitation record, empty when it conforms.

    Wraps ``validate_sanitation_summary`` and adds the entry-level
    requirements. Deliberately does not require ``entry_hash``, for the same
    reason ``validate_anchor`` does not: ``append_entry`` computes it after
    assembly, so demanding it would make the check unusable at the only
    point where it can still prevent harm.
    """
    if not isinstance(entry, dict):
        return [f"entry is not an object (got {type(entry).__name__})"]

    defects: list[str] = []
    if entry.get("verdict") != SANITATION_VERDICT:
        defects.append(
            f"verdict must be {SANITATION_VERDICT!r}, got "
            f"{entry.get('verdict')!r}"
        )

    change: Any = entry.get("change")
    if not isinstance(change, dict):
        defects.append("entry.change must be an object")
        return defects

    defects.extend(validate_sanitation_summary(change.get("diff_summary")))
    return defects


def digest_file(path: Path) -> str | None:
    """SHA-256 of a file, or None when it cannot be read.

    Returns None rather than raising so an auditor reports a missing backup
    as a failed audit instead of a traceback (C-001).
    """
    try:
        digest = hashlib.sha256()
        with open(path, "rb") as handle:
            for block in iter(lambda: handle.read(65536), b""):
                digest.update(block)
        return digest.hexdigest()
    except OSError as exc:
        print(f"[bench sanitize] cannot read backup: {exc}", file=sys.stderr)
        return None


def _audit_live_chain(summary: dict) -> list[str]:
    """Check the recorded chain claims against the chain itself.

    A record asserts that the authoritative chain still verifies and that its
    genesis and entry count are unchanged. Those are the claims a bad actor
    would most want to state falsely, and they are checkable, so the auditor
    checks them rather than trusting the record that makes them.
    """
    from ledger.verify import verify_chain

    defects: list[str] = []
    live: dict = verify_chain()
    if not live.get("valid"):
        defects.append(
            f"the authoritative chain does not verify: "
            f"{live.get('failure_type', 'unknown')}. A sanitation cannot be "
            f"sound while the chain it promised not to touch is broken."
        )
        return defects

    recorded_genesis: Any = summary.get("chain_genesis_hash")
    if live.get("genesis_hash") != recorded_genesis:
        defects.append(
            f"genesis mismatch: record says {recorded_genesis!r}, live chain "
            f"has {live.get('genesis_hash')!r}"
        )
    # Shrinkage only. Growth is expected: the chain is append-only and keeps
    # taking entries after the sanitation is recorded. A record that omits
    # chain_entry_count or gives a non-integer cannot slip past this check by
    # failing the isinstance guard, because validate_sanitation_summary
    # already reports that as a defect before the audit reaches here.
    recorded_count: Any = summary.get("chain_entry_count")
    live_count: Any = live.get("entries")
    if isinstance(recorded_count, int) and isinstance(live_count, int):
        if live_count < recorded_count:
            defects.append(
                f"the live chain has {live_count} entries, fewer than the "
                f"{recorded_count} recorded at sanitation time: entries "
                f"appear to have been removed"
            )
    return defects


def audit_sanitation(entry: Any, backup: Path | None = None) -> dict[str, Any]:
    """Run C-008's auditor check against a sanitation record.

    Three independent checks, because each can pass while another fails:
    the record's structure, the backup's digest, and the live chain's own
    state. Only the first can be satisfied by writing a well-formed record.
    """
    defects: list[str] = validate_sanitation_record(entry)
    result: dict[str, Any] = {
        "valid": False,
        "defects": defects,
        "digest_checked": False,
        "digest_matches": False,
    }

    summary_for_chain: dict = {}
    if isinstance(entry, dict) and isinstance(entry.get("change"), dict):
        candidate_summary = entry["change"].get("diff_summary")
        if isinstance(candidate_summary, dict):
            summary_for_chain = candidate_summary
    if summary_for_chain:
        defects.extend(_audit_live_chain(summary_for_chain))

    if backup is None:
        result["defects"] = defects
        result["valid"] = not defects
        result["detail"] = (
            "Structure and live chain only. Supply the backup artifact to "
            "verify its digest; a well-formed record proves nothing about "
            "the backup itself."
        )
        return result

    summary: dict = {}
    if isinstance(entry, dict) and isinstance(entry.get("change"), dict):
        candidate = entry["change"].get("diff_summary")
        if isinstance(candidate, dict):
            summary = candidate

    recorded: Any = summary.get("backup_digest")
    actual: str | None = digest_file(backup)
    result["digest_checked"] = True
    if actual is None:
        defects.append("backup could not be read at the supplied location")
    elif actual != recorded:
        defects.append(
            f"backup digest mismatch: record says {recorded!r}, artifact "
            f"hashes to {actual!r}"
        )
    else:
        result["digest_matches"] = True

    result["defects"] = defects
    result["valid"] = not defects
    return result
