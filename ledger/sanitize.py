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
import os
import re
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

SANITATION_EVENT: str = "published_copy_sanitation"
SANITATION_VERDICT: str = "SANITATION"
DIGEST_ALGORITHM: str = "sha256"
# Typed verbatim to confirm. Distinct from retirement's phrase so muscle
# memory from one ceremony cannot carry a human through the other.
CONFIRMATION_PHRASE: str = "SANITIZE PUBLISHED COPIES"

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

# Fields that must carry an actual written value. The check is non-empty
# string, not a prose heuristic: backup_id is deliberately opaque and
# retention_owner may be a role rather than a sentence. It exists because
# presence alone is not evidence, and `x in ("", None)` admits False, 0, [],
# and {} without complaint.
_TEXT_FIELDS: tuple[str, ...] = (
    "human_decision",
    "reason",
    "backup_id",
    "retention_owner",
    "retention_policy",
)


def _ref_entry_defects(index: int, ref: Any) -> list[str]:
    """Defects in one rewritten-ref entry, empty when it conforms."""
    if not isinstance(ref, dict):
        return [f"refs[{index}] is not an object"]

    defects: list[str] = []
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


def _validate_refs(refs: Any) -> list[str]:
    """Defects in the rewritten-ref list, empty when it conforms."""
    if not isinstance(refs, list) or not refs:
        return ["refs must be a non-empty list of rewritten references"]

    defects: list[str] = []
    for index, ref in enumerate(refs):
        defects.extend(_ref_entry_defects(index, ref))
    return defects


def _required_field_defects(summary: dict) -> list[str]:
    """Defects for required fields that are missing or empty."""
    defects: list[str] = []
    for field in _REQUIRED_FIELDS:
        if field not in summary:
            defects.append(f"missing required field {field!r}")
        elif summary[field] in ("", None):
            defects.append(f"field {field!r} is empty")
    return defects


def _text_field_defects(summary: dict) -> list[str]:
    """Defects for fields that must carry an actual written value.

    Presence is not evidence. `x in ("", None)` admits False, 0, [], and {},
    so a record could carry human_decision=False or retention_owner={} and
    report no defects while containing no decision and naming nobody. Every
    field C-008 expects a human to have written must actually be prose.
    """
    defects: list[str] = []
    for field in _TEXT_FIELDS:
        value: Any = summary.get(field)
        if field in summary and (
            not isinstance(value, str) or not value.strip()
        ):
            defects.append(
                f"field {field!r} must be a non-empty string, got "
                f"{type(value).__name__}"
            )
    return defects


def _backup_field_defects(summary: dict) -> list[str]:
    """Defects in the backup digest, algorithm, and identifier fields."""
    defects: list[str] = []
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
    return defects


def _chain_claim_defects(summary: dict) -> list[str]:
    """Defects in the recorded claims about the authoritative chain.

    C-008 requires the authoritative chain to be untouched. A sanitation
    recorded while it does not verify would assert the one thing the
    operation is not allowed to change.
    """
    defects: list[str] = []
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
    return defects


def _opaque_field_defects(summary: dict) -> list[str]:
    """Defects for opaque fields that carry a filesystem path."""
    defects: list[str] = []
    for field in _OPAQUE_FIELDS:
        value: Any = summary.get(field)
        if isinstance(value, str) and _PATH_RE.search(value):
            defects.append(
                f"field {field!r} looks like a filesystem path; sanitation "
                f"records carry an opaque identifier so the chain does not "
                f"republish machine paths"
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

    defects: list[str] = _required_field_defects(summary)
    defects.extend(_text_field_defects(summary))

    if summary.get("event") != SANITATION_EVENT:
        defects.append(
            f"event must be {SANITATION_EVENT!r}, got {summary.get('event')!r}"
        )

    defects.extend(_backup_field_defects(summary))
    defects.extend(_chain_claim_defects(summary))
    defects.extend(_opaque_field_defects(summary))

    # Shape-checked when present, not required. Records written before
    # verify_binding existed carry no repository, and C-008 forbids editing
    # them. Making it required here would retroactively invalidate a record
    # that was well formed under the schema it was written against. The
    # binding gate requires it instead, so an unnamed warrant cannot
    # authorize a rewrite while remaining honestly described as it was.
    repository: Any = summary.get("repository")
    if repository is not None and (
        not isinstance(repository, str)
        or not _REPOSITORY_RE.match(repository)
    ):
        defects.append(
            f"repository must look like 'owner/name', got {repository!r}"
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


_REPOSITORY_RE: re.Pattern = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")

# What a mirror push of 'refs/heads/*' and 'refs/tags/*' can reach. A ref
# under either prefix can be created, updated, or deleted by the push, so the
# warrant has to name it. refs/pull/* is GitHub-managed and unpushable.
PUSHABLE_PREFIXES: tuple[str, ...] = ("refs/heads/", "refs/tags/")


def access_established(status: int) -> bool:
    """True when a repository probe proved the repository is reachable.

    Object-level 404s only mean "removed" if the repository itself answered.
    GitHub returns 404 for a repository that is private to the token, renamed,
    or simply misspelled, and every object probe under it then returns 404
    too. Without this gate a single transposed letter turns a failed purge
    into a confirmed one, which is the most dangerous outcome available here:
    silent, total, and final.
    """
    return status == 200

# One endpoint per git object type. A purged set is mixed: the baseline for
# this repository is 298 blobs, 177 trees, 79 commits, and 2 tags.
_OBJECT_ENDPOINTS: Mapping[str, str] = {
    "blob": "git/blobs",
    "tree": "git/trees",
    "commit": "git/commits",
    "tag": "git/tags",
}

REMOVED: str = "removed"
PRESENT: str = "present"
INCONCLUSIVE: str = "inconclusive"


def object_endpoint(object_type: str, repository: str, sha: str) -> str:
    """API path for probing one object, chosen by its type.

    Probing everything at ``/commit/{sha}`` is the trap this exists to avoid.
    A blob SHA is not a commit SHA, so that path returns 404 for every blob
    whether or not the blob is still served, which reads as proof of removal
    while proving nothing. Most of a purged set is blobs and trees.
    """
    path: str | None = _OBJECT_ENDPOINTS.get(object_type)
    if path is None:
        raise SanitationError(
            f"no endpoint for object type {object_type!r}; refusing to probe "
            f"a type this cannot check rather than reporting it removed"
        )
    return f"repos/{repository}/{path}/{sha}"


def classify_removal(status: int) -> str:
    """Turn an HTTP status into a removal verdict.

    Only 404 proves removal. 200 proves the object is still served. Anything
    else, including 401, 403, 429, and every 5xx, means the question was not
    answered: a rate limit or an expired token would otherwise be recorded as
    a successful purge, which is the failure mode that matters most here
    because it is silent and final.
    """
    if status == 404:
        return REMOVED
    if status == 200:
        return PRESENT
    return INCONCLUSIVE


def _bound_ref_defects(
    entry: Any,
    remote_refs: Mapping[str, str],
    local_refs: Mapping[str, str],
) -> list[str]:
    """Defects binding one recorded ref to the remote and the local mirror."""
    if not isinstance(entry, dict):
        return ["a ref entry is not an object"]
    name: Any = entry.get("ref")
    pre: Any = entry.get("pre_image")
    post: Any = entry.get("post_image")
    if not isinstance(name, str):
        return ["a ref entry has no name"]

    defects: list[str] = []
    actual_remote: str | None = remote_refs.get(name)
    actual_local: str | None = local_refs.get(name)

    if actual_remote is None:
        defects.append(f"{name}: recorded but absent from the remote")
    elif actual_remote == post:
        defects.append(
            f"{name}: the remote is already at the recorded post-image "
            f"{str(post)[:12]}. This warrant was already used; a second "
            f"push under it would be an unrecorded rewrite."
        )
    elif actual_remote != pre:
        defects.append(
            f"{name}: remote is {actual_remote[:12]}, but the record was "
            f"issued against {str(pre)[:12]}. The remote moved after the "
            f"warrant was written."
        )

    if actual_local is None:
        defects.append(f"{name}: recorded but absent from the local mirror")
    elif actual_local != post:
        defects.append(
            f"{name}: mirror is {actual_local[:12]}, but the record "
            f"authorizes {str(post)[:12]}. The rewrite about to be pushed "
            f"is not the one that was authorized."
        )
    return defects


def _unrecorded_ref_defects(
    refs: list,
    remote_refs: Mapping[str, str],
    local_refs: Mapping[str, str],
) -> list[str]:
    """Defects for pushable refs the record never mentions.

    Checking only the recorded refs leaves the push wider than the warrant.
    The push is 'refs/heads/*' and 'refs/tags/*', so a ref in either the
    remote or the mirror that the record never mentions would still be
    created, updated, or deleted by it, under an authorization that never
    named it. Bind the union of what the push can reach, not the subset the
    record happens to list. refs/pull/* is excluded because it is
    GitHub-managed and no push can touch it.
    """
    recorded_names: set[str] = {
        entry.get("ref")
        for entry in refs
        if isinstance(entry, dict) and isinstance(entry.get("ref"), str)
    }
    reachable: set[str] = {
        name
        for name in set(remote_refs) | set(local_refs)
        if name.startswith(PUSHABLE_PREFIXES)
    }
    defects: list[str] = []
    for name in sorted(reachable - recorded_names):
        on_remote: bool = name in remote_refs
        in_mirror: bool = name in local_refs
        if on_remote and not in_mirror:
            # The push cannot delete this, but that is the smaller problem:
            # the mirror was made without it, so the rewrite never examined
            # it and any contaminated objects it carries survive the purge.
            defects.append(
                f"{name}: on the remote but absent from the mirror and "
                f"unrecorded. The rewrite never covered it, so anything it "
                f"carries survives the purge. Re-clone the mirror."
            )
        elif in_mirror and not on_remote:
            defects.append(
                f"{name}: in the mirror but not on the remote and "
                f"unrecorded. The push would create it under a warrant that "
                f"never mentioned it."
            )
        else:
            # In both. A name absent from both cannot reach here: `reachable`
            # is built from the union of the two mappings, so every name in
            # it belongs to at least one.
            defects.append(
                f"{name}: present on both sides but unrecorded. The push "
                f"would update it under a warrant that never mentioned it."
            )
    return defects


def verify_binding(
    summary: Mapping[str, Any],
    repository: str,
    remote_refs: Mapping[str, str],
    local_refs: Mapping[str, str],
) -> list[str]:
    """Bind a sanitation warrant to the operation about to be performed.

    ``validate_sanitation_record`` checks that a record is well formed and
    that the chain it describes is intact. Neither says the record authorizes
    *this* rewrite of *this* repository right now. A well-formed record is a
    warrant with no name on it: without these checks the same record would
    satisfy the gate for a different repository, for a mirror holding a
    different rewrite, or a second time after the first push already consumed
    it.

    Four bindings, each of which can hold while another fails:

    * **Repository.** The record names the repository it authorizes.
    * **Pre-image.** Every recorded ref still points where the record says it
      did, so the remote has not moved since the warrant was issued.
    * **Post-image.** The local mirror holds exactly the rewrite the record
      describes, so the thing about to be pushed is the thing authorized.
    * **Not consumed.** A remote already sitting at the post-image means this
      warrant was spent; re-using it would authorize a second, unrecorded
      rewrite.

    Pure and injectable: refs come in as mappings so this is testable without
    a network or a repository. Returns defects, empty when the binding holds.
    """
    defects: list[str] = []

    recorded_repo: Any = summary.get("repository")
    if not isinstance(recorded_repo, str) or not _REPOSITORY_RE.match(
        recorded_repo
    ):
        defects.append(
            "record does not name a repository, so it cannot authorize an "
            "operation against one. Records created before this check "
            "existed carry no repository and must be superseded."
        )
    elif recorded_repo != repository:
        defects.append(
            f"record authorizes {recorded_repo!r}, not {repository!r}"
        )

    refs: Any = summary.get("refs")
    if not isinstance(refs, list) or not refs:
        defects.append("record carries no refs to bind against")
        return defects

    for entry in refs:
        defects.extend(_bound_ref_defects(entry, remote_refs, local_refs))

    defects.extend(_unrecorded_ref_defects(refs, remote_refs, local_refs))
    return defects


def confirm_sanitation_interactively(
    refs: list[dict[str, Any]],
    backup_id: str,
    *,
    stdin_isatty: Callable[[], bool] | None = None,
    prompt: Callable[[str], str] | None = None,
    env: Mapping[str, str] | None = None,
) -> str:
    """Enforce C-008(a) and return the recorded human decision.

    Deliberately reuses retirement's markers rather than defining a parallel
    set: two gates that drift apart would leave the weaker one as the real
    boundary.

    The callables are injectable so tests never need a real TTY, and are
    resolved at call time rather than bound at import so a replaced
    ``sys.stdin`` is honoured.
    """
    from ledger.retire import AGENT_ENV_MARKERS

    environment: Mapping[str, str] = env if env is not None else os.environ
    tripped: list[str] = [
        marker
        for marker in AGENT_ENV_MARKERS
        if str(environment.get(marker, "")).strip()
    ]
    if tripped:
        raise SanitationError(
            f"refusing to record a sanitation: {', '.join(tripped)} set in "
            f"the environment, so this is not a human at a plain terminal. "
            f"C-008(a) requires an explicit human decision that is never "
            f"automated or agent-initiated."
        )

    isatty: Callable[[], bool] = (
        stdin_isatty if stdin_isatty is not None else sys.stdin.isatty
    )
    if not isatty():
        raise SanitationError(
            "refusing to record a sanitation: stdin is not a TTY, so no "
            "human confirmation is possible. C-008(a) requires an explicit "
            "human decision."
        )

    ask: Callable[[str], str] = prompt if prompt is not None else input
    banner: str = (
        f"\nAbout to record a published-copy sanitation.\n"
        f"  refs rewritten : {len(refs)}\n"
        f"  backup id      : {backup_id}\n"
        f"This asserts that a human authorized removing published copies and\n"
        f"that the backup exists and verifies.\n"
        f"Type {CONFIRMATION_PHRASE} to confirm: "
    )
    if ask(banner).strip() != CONFIRMATION_PHRASE:
        raise SanitationError(
            "aborted: confirmation phrase not matched. Nothing was recorded."
        )

    return (
        f"Confirmed interactively at a TTY by typing the required phrase "
        f"verbatim, authorizing removal of published copies across "
        f"{len(refs)} reference(s). Not agent-initiated."
    )


def build_sanitation_record(
    refs: list[dict],
    backup_id: str,
    backup_digest: str,
    reason: str,
    human_decision: str,
    retention_owner: str,
    retention_policy: str,
    repository: str,
) -> dict[str, Any]:
    """Assemble a sanitation record, filling the chain fields from the chain.

    ``chain_verified_valid``, ``chain_genesis_hash`` and
    ``chain_entry_count`` are read here rather than supplied by the caller.
    They are assertions about the chain's state at the moment of sanitation,
    so taking them from a human's typing would let a stale or invented value
    into the one place an auditor checks against reality. Reading them inline
    also keeps them contemporaneous: a governed edit landing between the
    rewrite and the record would otherwise shift the count.

    Raises ``SanitationError`` when the assembled record does not conform, so
    a non-conforming record cannot reach the chain and be mistaken later for
    one that satisfied the constraint.
    """
    from ledger.verify import verify_chain

    live: dict = verify_chain()
    summary: dict[str, Any] = {
        "event": SANITATION_EVENT,
        # Names the repository the warrant authorizes. Without it a record is
        # a warrant with no name on it, and verify_binding refuses to bind.
        "repository": repository,
        "human_decision": human_decision,
        "reason": reason,
        "refs": refs,
        "backup_id": backup_id,
        "backup_digest_algorithm": DIGEST_ALGORITHM,
        "backup_digest": backup_digest,
        "retention_owner": retention_owner,
        "retention_policy": retention_policy,
        "chain_verified_valid": bool(live.get("valid")),
        "chain_genesis_hash": live.get("genesis_hash", ""),
        "chain_entry_count": live.get("entries", 0),
    }
    record: dict[str, Any] = {
        "verdict": SANITATION_VERDICT,
        "pipeline_error": False,
        "change": {
            "file": "published copies",
            "tool": "PublishedCopySanitation",
            "diff_summary": summary,
        },
        "challenger": {},
        "defender": {},
        "oracle": {},
    }

    defects: list[str] = validate_sanitation_record(record)
    if defects:
        raise SanitationError(
            "refusing to record a non-conforming sanitation: "
            + "; ".join(defects)
        )
    return record


class SanitationError(Exception):
    """A defect that must stop a sanitation from being recorded.

    Raised rather than returned so a caller cannot append a record it failed
    to inspect.
    """


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


def verify_copy(source: Path, destination: Path, expected: str) -> list[str]:
    """Confirm a copied backup is byte-identical before the source is removed.

    Moving the only encrypted recovery bundle is not safe. A cross-volume
    move is a copy plus a delete, and if the copy is short, truncated, or
    silently corrupted the delete destroys the only remaining good copy. The
    move also reports success from the filesystem, which is not the same
    claim as "the destination hashes to what the sanitation record says".

    So: copy, hash the destination, require it to equal the recorded digest,
    and only then delete the source deliberately. Returns defects, empty when
    the destination is provably identical.
    """
    defects: list[str] = []
    if not _SHA256_RE.match(expected or ""):
        defects.append(f"expected digest is not a sha256: {expected!r}")

    if not destination.exists():
        defects.append(f"destination does not exist: {destination}")
        return defects

    actual: str | None = digest_file(destination)
    if actual is None:
        defects.append(f"destination could not be read: {destination}")
        return defects
    if actual != expected:
        defects.append(
            f"destination digest {actual} does not match the recorded "
            f"{expected}. Do not delete the source."
        )

    if source.exists():
        source_digest: str | None = digest_file(source)
        if source_digest is not None and source_digest != actual:
            defects.append(
                "source and destination differ; the copy is not faithful"
            )
    return defects


def worktree_is_clean(porcelain_status: str) -> bool:
    """True when ``git status --porcelain`` reported nothing.

    Gate for retiring an old clone. After a history rewrite the old clone
    holds commits that no longer exist upstream, and the reflex fix,
    ``git reset --hard origin/main``, destroys uncommitted work without
    asking. It also does not sanitize anything: the purged objects stay in
    that clone's ``.git`` afterwards. A fresh clone is the honest move, and
    this refuses to retire a clone that still holds unsaved work.
    """
    return porcelain_status.strip() == ""


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
        result["structure_valid"] = not defects
        # Not valid, only incomplete. C-008 defines three checks, and the one
        # that is missing here is the only one a forged record cannot satisfy
        # by being well-formed. Reporting VALID on two of three would let the
        # cheapest audit look like the strongest.
        result["valid"] = False
        # Only incomplete when nothing else is wrong. A record with real
        # defects that also lacks a digest check is INVALID, not merely
        # unfinished, and reporting the softer word would bury the defect.
        result["incomplete"] = not defects
        result["detail"] = (
            "Structure and live chain only, so this is incomplete rather "
            "than valid. Supply the backup artifact to verify its digest; a "
            "well-formed record proves nothing about the backup itself."
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
