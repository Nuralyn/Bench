"""Chain retirement: the one bounded exception C-008 allows to immutability.

C-008 forbids editing, reordering, or removing an individual ledger entry under
all circumstances. It permits retiring a *whole* chain, and only when that chain
contains content which must not be published. Retirement is not deletion: every
entry is preserved in an archive that is verified *before* the live chain is
removed, and a successor chain opens with an anchor entry recording where the
predecessor went, so custody is documented rather than broken.

A storage-format change is NOT a permitted trigger. That case came up concretely
during the ledger-DAG work (PR #22), where retirement was considered as a
migration path and had to be rejected; freezing the legacy array was the answer
instead. The trigger is not machine-checkable, so this module does not pretend
to check it. It states the rule at the prompt, requires the operator to type a
substantive reason, and records that reason verbatim in the anchor.

Nothing in the pipeline calls this module. Retirement is operator-initiated,
never agent-initiated (C-008(a)), which is what the environment and TTY guards
in ``confirm_interactively`` exist to enforce. Those guards raise the bar; they
are not cryptographic proof, and the anchor's recorded ``human_decision`` is
what an auditor actually relies on.

The shape this leaves behind matters and is easy to get wrong. ``append_entry``
writes only to ``entries/<hash>.json``; ``bench-ledger.json`` is frozen and
``ledger-meta.json`` is frozen with it. A successor chain is therefore
``entries/`` *alone*, with neither of those files. Every segment is handled as
optional here for exactly that reason: the first retirement finds all three, and
every retirement after it finds only the entries directory.
"""

import os
import shutil
import sys
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ledger.chain import (
    ANCHOR_VERDICT,
    ENTRIES_DIRNAME,
    META_FILENAME,
    append_entry,
    resolve_ledger_path,
)
from ledger.verify import verify_chain
from pipeline.constitution import (
    ConstitutionError,
    load_governing_constitution,
)
from utils.project import project_root

ANCHOR_TOOL: str = "ChainRetirement"
"""``change.tool`` on an anchor entry, matching the 2026-07-24 reference."""

_GENESIS_MARKER: str = "GENESIS"
"""Re-declared locally rather than imported from ``chain``, as ``verify.py``
does with the same value and for the same reason: this module checks that the
anchor it just wrote actually opened the successor chain, and that check must
not inherit the writer's idea of what a root looks like."""

ANCHOR_EVENT: str = "chain_retirement"

ANCHOR_AUTHORITY: str = "C-008 as amended in constitution version 2"
"""Historical provenance: the constitution version in which C-008 gained the
retirement clause.

This is deliberately NOT the version in force at retirement time, and must not
be "corrected" to match ``bench.json``'s current version. The two legitimately
differ, and there is nothing to derive the value from: ``bench.json`` carries no
amendment history, only a single ``version`` field. The version actually in
force is recorded separately, as ``constitution_version`` on the summary and as
``constitution_hash`` / ``constitution_sources`` on the entry itself.
"""

ARCHIVE_RETENTION: str = "indefinite"

AUDITOR_CHECK: str = (
    "Run ledger.verify.verify_chain against the ledger file inside "
    "archive_path and confirm its tip hash equals predecessor_tip_hash and "
    "its entry count equals predecessor_entries. "
    "`python -m cli audit-retirement` performs exactly this check."
)

CONFIRMATION_PHRASE: str = "retire this chain and archive it indefinitely"

AGENT_ENV_MARKERS: tuple[str, ...] = ("BENCH_SUBPROCESS", "CLAUDECODE", "CI")
"""Environment variables whose presence means this is not a human at a terminal.

``BENCH_SUBPROCESS`` is set by Bench's own claude_code provider path.
``CLAUDECODE`` is set inside any Claude Code session, including for a command
the user types with a ``!`` prefix, which is intentional: C-008(a) requires the
decision not be agent-initiated, and a shell hosted by an agent does not clear
that bar. Retirement must be run from a plain terminal.
"""

MIN_REASON_CHARS: int = 80
"""Floor on ``--reason``.

Not a quality check, and it cannot be one. It exists so the reason is a stated
justification an auditor can weigh rather than a word typed to get past a
prompt.
"""

_REQUIRED_SUMMARY_FIELDS: tuple[str, ...] = (
    "event",
    "authority",
    "human_decision",
    "reason",
    "predecessor_tip_hash",
    "predecessor_genesis_hash",
    "predecessor_entries",
    "predecessor_first_entry",
    "predecessor_last_entry",
    "archive_path",
    "archive_verified_valid",
    "archive_retention",
    "auditor_check",
    "retired_at",
)
"""Every element C-008(c) enumerates, plus the bookkeeping the reference anchor
established. ``remediation_landed`` and ``constitution_version`` are optional.
"""


class RetirementError(RuntimeError):
    """Raised when a retirement cannot proceed, for any reason.

    Every refusal path raises this with a message naming the specific failure
    (C-001: no silent swallowing). Retirement is fail-closed throughout: a
    condition that cannot be confirmed aborts rather than proceeding, and every
    abort before the deletion step leaves the live chain byte-identical.
    """


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _present_segments(ledger_path: str) -> list[str]:
    """Names of the ledger segments that actually exist on disk.

    All three are optional. A first retirement finds the frozen array, its meta
    pin, and the entries directory; every retirement after it finds only the
    entries directory, because that is all ``append_entry`` writes.
    """
    file_path: Path = Path(ledger_path)
    directory: Path = file_path.parent
    present: list[str] = []
    if file_path.is_file():
        present.append(file_path.name)
    if (directory / META_FILENAME).is_file():
        present.append(META_FILENAME)
    if (directory / ENTRIES_DIRNAME).is_dir():
        present.append(ENTRIES_DIRNAME)
    return present


def plan_retirement(ledger_path: str | None = None) -> dict:
    """Gather the facts a retirement needs, refusing if the chain is unfit.

    Read-only: nothing on disk is touched. Reuses ``verify_chain``'s returned
    values rather than recomputing them, so the facts recorded in the anchor are
    the auditor's own numbers.

    Refuses a chain that fails verification (archiving a broken chain would
    preserve the damage and call it evidence), an empty chain (nothing to
    retire), and a forked chain. The fork case is not a defect: a git merge of
    two branches that both appended leaves two tips, and C-008 records a single
    predecessor tip hash. One governed edit names both tips and reconciles it.
    """
    resolved: str = (
        ledger_path if ledger_path is not None else resolve_ledger_path()
    )
    result: dict = verify_chain(resolved)

    if not result.get("valid"):
        raise RetirementError(
            f"refusing to retire a chain that does not verify: "
            f"{result.get('failure_type', 'unknown')}: "
            f"{result.get('message', 'no detail')}"
        )

    entries: int = int(result.get("entries", 0))
    if entries == 0:
        raise RetirementError(
            f"refusing to retire an empty chain at {resolved}: there is "
            f"nothing to archive"
        )

    raw_tips: Any = result.get("tips", [])
    tips: list[str] = raw_tips if isinstance(raw_tips, list) else []
    if len(tips) != 1:
        listed: str = "\n".join(f"    - {tip}" for tip in tips)
        raise RetirementError(
            f"refusing to retire a chain with {len(tips)} tips:\n{listed}\n"
            f"C-008 records a single predecessor tip hash. Make one governed "
            f"edit (any Write/Edit) so the next entry names every tip and "
            f"reconciles the fork, then retire."
        )

    return {
        "ledger_path": resolved,
        "entries": entries,
        "tip_hash": tips[0],
        "genesis_hash": str(result.get("genesis_hash", "")),
        "first_entry": str(result.get("first_entry", "")),
        "last_entry": str(result.get("last_entry", "")),
        "segments": _present_segments(resolved),
    }


def render_confirmation(facts: dict, archive_root: str) -> str:
    """The text shown before a retirement, stating the rule and the stakes.

    The trigger cannot be checked in code, so it is stated here instead, with
    the format-change non-example named explicitly. A test asserts both appear,
    so #22's lesson is machine-checked rather than left to prose.
    """
    segments: str = ", ".join(facts.get("segments", [])) or "none"
    return (
        "\n"
        "RETIREMENT OF THE ACTIVE CHAIN (C-008)\n"
        f"  ledger        : {facts['ledger_path']}\n"
        f"  entries       : {facts['entries']}\n"
        f"  segments      : {segments}\n"
        f"  genesis       : {facts['genesis_hash']}\n"
        f"  tip           : {facts['tip_hash']}\n"
        f"  first entry   : {facts['first_entry']}\n"
        f"  last entry    : {facts['last_entry']}\n"
        f"  archive       : {archive_root}\n"
        "\n"
        "C-008 permits retirement ONLY when the chain contains content which\n"
        "must not be published. A storage-format change is NOT such a trigger\n"
        "(see PR #22, where retirement was considered for the ledger-DAG\n"
        "migration and rejected). Retirement is not deletion: the archive is\n"
        "verified before anything is removed and retained indefinitely.\n"
        "\n"
        "Type the phrase to confirm, or anything else to abort:\n"
        f"  {CONFIRMATION_PHRASE}\n"
    )


def confirm_interactively(
    facts: dict,
    archive_root: str,
    *,
    stdin_isatty: Callable[[], bool] | None = None,
    prompt: Callable[[str], str] | None = None,
    env: Mapping[str, str] | None = None,
) -> str:
    """Enforce C-008(a) and return the recorded human decision.

    Raises ``RetirementError`` on every refusal rather than returning a bool, so
    the operator is told which gate stopped them instead of receiving a bare
    "no". Returns the attestation string written into the anchor.

    The callables are injectable so tests never need a real TTY. They default to
    the live ones, resolved at call time rather than bound at import, so a
    replaced ``sys.stdin`` is honoured.
    """
    environment: Mapping[str, str] = env if env is not None else os.environ
    tripped: list[str] = [
        marker
        for marker in AGENT_ENV_MARKERS
        if str(environment.get(marker, "")).strip()
    ]
    if tripped:
        raise RetirementError(
            f"refusing to retire: {', '.join(tripped)} set in the environment, "
            f"so this is not a human at a plain terminal. C-008(a) requires an "
            f"explicit human decision that is never automated or "
            f"agent-initiated. Run this from a terminal outside any agent "
            f"session."
        )

    isatty: Callable[[], bool] = (
        stdin_isatty if stdin_isatty is not None else sys.stdin.isatty
    )
    if not isatty():
        raise RetirementError(
            "refusing to retire: stdin is not a TTY, so no human confirmation "
            "is possible. C-008(a) requires an explicit human decision."
        )

    ask: Callable[[str], str] = prompt if prompt is not None else input
    answer: str = ask(render_confirmation(facts, archive_root))
    if answer.strip() != CONFIRMATION_PHRASE:
        raise RetirementError(
            "aborted: confirmation phrase not matched. Nothing was changed."
        )

    confirmed_at: str = _utc_now().isoformat()
    return (
        f"Confirmed interactively at a TTY on {confirmed_at} by typing the "
        f"required phrase verbatim. Not agent-initiated: none of "
        f"{', '.join(AGENT_ENV_MARKERS)} were set in the environment."
    )


def build_anchor_summary(
    facts: dict,
    *,
    archive_path: str,
    reason: str,
    human_decision: str,
    constitution_version: Any = None,
    remediation: str | None = None,
    retired_at: str | None = None,
) -> dict:
    """Assemble the anchor's ``change.diff_summary``.

    The field set is the one the conforming 2026-07-24 anchor established, which
    is in turn C-008(c)'s enumeration plus the bookkeeping an auditor needs.
    ``predecessor_first_entry`` and ``predecessor_last_entry`` hold timestamp
    *values*, not hashes or indices, because that is what C-008(c) names.
    """
    summary: dict[str, Any] = {
        "event": ANCHOR_EVENT,
        "authority": ANCHOR_AUTHORITY,
        "human_decision": human_decision,
        "reason": reason,
        "predecessor_tip_hash": facts["tip_hash"],
        "predecessor_genesis_hash": facts["genesis_hash"],
        "predecessor_entries": facts["entries"],
        "predecessor_first_entry": facts["first_entry"],
        "predecessor_last_entry": facts["last_entry"],
        "archive_path": archive_path,
        "archive_verified_valid": True,
        "archive_retention": ARCHIVE_RETENTION,
        "auditor_check": AUDITOR_CHECK,
        "retired_at": retired_at or _utc_now().isoformat(),
    }
    if constitution_version is not None:
        # The version in force at retirement time, which is not what
        # ``authority`` names. See ANCHOR_AUTHORITY for why the two differ and
        # why neither can be derived from the other.
        summary["constitution_version"] = constitution_version
    if remediation:
        summary["remediation_landed"] = remediation
    return summary


def validate_anchor_summary(summary: Any) -> list[str]:
    """Defects in an anchor summary, empty when it conforms.

    Callable before an entry exists, which is what lets the caller validate
    while the live chain is still on disk. A malformed anchor caught here is a
    refusal; caught after deletion it would be a chain opened on a record that
    does not satisfy the constraint permitting it.
    """
    if not isinstance(summary, dict):
        return [f"summary is not an object (got {type(summary).__name__})"]

    defects: list[str] = []
    for field in _REQUIRED_SUMMARY_FIELDS:
        if field not in summary:
            defects.append(f"missing required field {field!r}")
        elif summary[field] in ("", None):
            defects.append(f"field {field!r} is empty")

    if summary.get("event") != ANCHOR_EVENT:
        defects.append(
            f"event must be {ANCHOR_EVENT!r}, got {summary.get('event')!r}"
        )
    if summary.get("archive_verified_valid") is not True:
        defects.append(
            "archive_verified_valid must be true: C-008(b) requires the "
            "archive to verify before the original is removed"
        )
    if summary.get("archive_retention") != ARCHIVE_RETENTION:
        defects.append(
            f"archive_retention must be {ARCHIVE_RETENTION!r}: C-008(d) "
            f"requires indefinite retention"
        )
    entries: Any = summary.get("predecessor_entries")
    if not isinstance(entries, int) or isinstance(entries, bool) or entries < 1:
        defects.append(
            f"predecessor_entries must be a positive integer, got {entries!r}"
        )
    reason: Any = summary.get("reason")
    if isinstance(reason, str) and len(reason.strip()) < MIN_REASON_CHARS:
        defects.append(
            f"reason must be at least {MIN_REASON_CHARS} characters, got "
            f"{len(reason.strip())}"
        )
    return defects


def validate_anchor(entry: Any) -> list[str]:
    """Defects in a whole anchor entry, empty when it conforms.

    Wraps ``validate_anchor_summary`` and adds the entry-level requirements.
    Deliberately does not require ``entry_hash``: ``append_entry`` computes that
    after the entry is assembled, so demanding it would make the check
    unusable at the only point where it can still prevent harm.
    """
    if not isinstance(entry, dict):
        return [f"entry is not an object (got {type(entry).__name__})"]

    defects: list[str] = []
    if entry.get("verdict") != ANCHOR_VERDICT:
        defects.append(
            f"verdict must be {ANCHOR_VERDICT!r}, got {entry.get('verdict')!r}"
        )
    change: Any = entry.get("change")
    if not isinstance(change, dict):
        defects.append("entry has no change object")
        return defects
    if change.get("tool") != ANCHOR_TOOL:
        defects.append(
            f"change.tool must be {ANCHOR_TOOL!r}, got {change.get('tool')!r}"
        )
    defects.extend(validate_anchor_summary(change.get("diff_summary")))
    return defects


def _archive_root(archive_dir: str, now: datetime) -> Path:
    """The per-retirement directory inside ``archive_dir``.

    Stamped to the second rather than the day so a second retirement on the
    same date does not collide with the first and strand the operator against
    the never-overwrite rule.
    """
    stamp: str = now.strftime("%Y-%m-%dT%H%M%SZ")
    return Path(archive_dir).expanduser().resolve() / f"bench-ledger-{stamp}"


def _copy_segments(ledger_path: str, destination: Path, segments: list[str]) -> None:
    """Copy the segments that exist into ``destination``.

    Copy, never move. C-008(b) requires the archive to verify before the
    original goes anywhere, and a copy that fails verification must leave the
    live chain untouched.
    """
    source_dir: Path = Path(ledger_path).parent
    destination.mkdir(parents=True, exist_ok=False)
    for name in segments:
        source: Path = source_dir / name
        if source.is_dir():
            shutil.copytree(source, destination / name)
        else:
            shutil.copy2(source, destination / name)


def _restore_segments(staging: Path, ledger_path: str, moved: list[str]) -> None:
    """Move staged segments back, undoing a retirement that could not finish."""
    destination: Path = Path(ledger_path).parent
    destination.mkdir(parents=True, exist_ok=True)
    for name in moved:
        source: Path = staging / name
        if source.exists():
            shutil.move(str(source), str(destination / name))


def _stage_segments(
    ledger_path: str, segments: list[str], staging: Path
) -> list[str]:
    """Move the live segments aside rather than deleting them.

    Deleting outright is not recoverable. An OSError partway through the old
    sequential delete left a half-dismantled chain: the frozen array gone but
    the entry files still present, their parents now missing. ``plan_retirement``
    refuses such a chain, so the operator could not simply retry, and the
    recovery was undocumented manual surgery against the archive.

    Moving is one rename per segment on the same filesystem, and it is
    reversible. A failure here restores what it already moved and leaves the
    chain exactly as it was, so "retire again" is honest advice.

    Returns the names actually moved, so the caller can undo them later.
    """
    staging.mkdir(parents=True, exist_ok=False)
    source_dir: Path = Path(ledger_path).parent
    moved: list[str] = []
    try:
        for name in segments:
            source: Path = source_dir / name
            if not source.exists():
                continue
            shutil.move(str(source), str(staging / name))
            moved.append(name)
    except OSError as exc:
        _restore_segments(staging, ledger_path, moved)
        raise RetirementError(
            f"could not move the live chain aside ({exc}). It was restored "
            f"and nothing was retired."
        ) from exc
    return moved


def _discard_staging(staging: Path) -> None:
    """Delete the staged copy once the retirement has fully succeeded.

    Best effort by design. At this point the archive is verified and the anchor
    is written, so a leftover staging directory is a redundant copy rather than
    a risk, and failing the whole retirement over it would be worse than saying
    so. Readers ignore it: nothing resolves a dot-prefixed directory as a ledger
    segment.
    """
    try:
        shutil.rmtree(staging)
    except OSError as exc:
        print(
            f"[bench retire] retirement succeeded but the staged copy at "
            f"{staging} could not be removed ({exc}); it is redundant with the "
            f"archive and can be deleted by hand",
            file=sys.stderr,
        )


def _project_relative(ledger_path: str) -> str:
    """The ledger path as recorded in ``change.file``, always relative.

    An absolute path outside the project root makes ``chain._is_external_change``
    treat the anchor as foreign and stamp ``redacted: true`` onto its summary.
    The required fields would survive that, so the entry would still validate
    while misrepresenting itself as a redacted record of someone else's code. A
    relative path is never classified as external, so this is the safe form
    regardless of where the ledger lives.
    """
    try:
        relative: str = os.path.relpath(
            os.path.realpath(ledger_path), str(project_root())
        )
        # Forward slashes, matching the 2026-07-24 reference anchor's
        # "ledger/bench-ledger.json". The ledger is read on whatever platform
        # audits it, so the recorded evidence should not carry the separator of
        # the machine that happened to write it.
        return relative.replace(os.sep, "/")
    except (OSError, ValueError) as exc:
        # Windows: a different drive from the project has no relative form.
        # Fall back to the bare filename, which is still relative and so still
        # avoids redaction, and say why (C-001).
        print(
            f"[bench retire] cannot express {ledger_path!r} relative to the "
            f"project root ({exc}); recording its filename instead",
            file=sys.stderr,
        )
        return Path(ledger_path).name


def _load_constitution() -> tuple[str, list, Any]:
    """Hash, sources, and version of the constitution authorizing this act.

    Fails closed: a constitution that cannot be loaded means the authority for
    the retirement cannot be recorded, and an anchor that cannot name its
    authority is not worth writing.
    """
    try:
        constitution, constitution_hash, sources = load_governing_constitution()
    except ConstitutionError as exc:
        raise RetirementError(
            f"refusing to retire: the governing constitution could not be "
            f"loaded, so the anchor cannot record its authority: {exc}"
        ) from exc
    return constitution_hash, sources, constitution.get("version")


def _check_archive_matches(
    archive_result: dict, facts: dict, archive_root: Path
) -> None:
    """Refuse unless the archive verifies and equals the live chain.

    C-008(b): this runs BEFORE anything is removed, so every refusal here
    leaves the live chain byte-identical.
    """
    if not archive_result.get("valid"):
        raise RetirementError(
            f"refusing to retire: the archive at {archive_root} does not "
            f"verify ({archive_result.get('failure_type', 'unknown')}: "
            f"{archive_result.get('message', 'no detail')}). The live chain "
            f"was not touched."
        )
    if int(archive_result.get("entries", -1)) != facts["entries"]:
        raise RetirementError(
            f"refusing to retire: the archive holds "
            f"{archive_result.get('entries')} entries but the live chain has "
            f"{facts['entries']}. The live chain was not touched."
        )
    if archive_result.get("tips") != [facts["tip_hash"]]:
        raise RetirementError(
            f"refusing to retire: the archive's tips "
            f"{archive_result.get('tips')} do not match the live chain's tip "
            f"{facts['tip_hash']}. The live chain was not touched."
        )


def _check_staged_matches(
    staging: Path,
    resolved: str,
    moved: list[str],
    facts: dict,
    archive_root: Path,
) -> None:
    """Re-check the staged chain against the archive, restoring on drift.

    A governance run in another session can append between the archive
    verification and the segments being moved aside. Under the old sequential
    delete that receipt was destroyed without ever appearing in the archive or
    in the anchor's count, which is precisely the removal of an entry C-008
    forbids without exception. Comparing the staged chain against the archive
    turns that race into a refusal, and because the segments were moved rather
    than deleted, the refusal restores the chain exactly as it was.
    """
    try:
        staged: dict = verify_chain(str(staging / Path(resolved).name))
        drifted: bool = (
            not staged.get("valid")
            or int(staged.get("entries", -1)) != facts["entries"]
            or staged.get("tips") != [facts["tip_hash"]]
        )
    except Exception:
        # Any failure raised while the chain is set aside has to put it back
        # before it surfaces, or an unrelated fault leaves the operator with a
        # ledger directory that looks empty. Restore, then re-raise unchanged
        # so the real error is never masked (C-001).
        _restore_segments(staging, resolved, moved)
        _discard_staging(staging)
        raise

    if drifted:
        _restore_segments(staging, resolved, moved)
        _discard_staging(staging)
        raise RetirementError(
            f"refusing to retire: the live chain changed between being "
            f"archived and being moved aside, so the archive at {archive_root} "
            f"is already stale and retiring would destroy the entry that "
            f"landed in between. The chain has been restored exactly as it "
            f"was. This happens when a governed edit runs mid-retirement; "
            f"retry with no other session writing to this ledger."
        )


def execute_retirement(
    *,
    archive_dir: str,
    reason: str,
    ledger_path: str | None = None,
    remediation: str | None = None,
    stdin_isatty: Callable[[], bool] | None = None,
    prompt: Callable[[str], str] | None = None,
    env: Mapping[str, str] | None = None,
    now: Callable[[], datetime] | None = None,
) -> dict:
    """Retire the active chain and open its successor. Raises on any refusal.

    The ordering is the whole safety argument, so it is spelled out:

    1. Verify the live chain and refuse if it is unfit (``plan_retirement``).
    2. Enforce the human gate.
    3. Copy the segments that exist into a fresh archive directory.
    4. Verify the archive, and require its entry count and tip to equal the
       live chain's. C-008(b): this happens BEFORE anything is removed.
    5. Build and validate the anchor summary, while the originals still exist.
    6. Only now remove the live segments.
    7. Append the anchor through ``chain.append_entry``. The location is empty,
       so ``previous_hash`` becomes GENESIS and the successor chain opens
       through the audited write path with no second writer.
    8. Validate the written entry and verify the successor chain.

    Any failure in steps 1 through 5 leaves the live chain byte-identical. A
    failure after step 6 cannot lose data, because the archive is already
    verified on disk, which is why the error names it.
    """
    clock: Callable[[], datetime] = now if now is not None else _utc_now
    stripped_reason: str = reason.strip() if isinstance(reason, str) else ""
    if len(stripped_reason) < MIN_REASON_CHARS:
        raise RetirementError(
            f"refusing to retire: --reason must be at least "
            f"{MIN_REASON_CHARS} characters describing the unpublishable "
            f"content that triggers C-008's exception, got "
            f"{len(stripped_reason)}"
        )

    facts: dict = plan_retirement(ledger_path)
    resolved: str = facts["ledger_path"]
    segments: list[str] = facts["segments"]
    if not segments:
        raise RetirementError(
            f"refusing to retire: no ledger segments found beside {resolved}"
        )

    started: datetime = clock()
    archive_root: Path = _archive_root(archive_dir, started)
    if archive_root.exists():
        raise RetirementError(
            f"refusing to retire: archive destination {archive_root} already "
            f"exists. An archive is never overwritten (C-008(d))."
        )

    ledger_dir: Path = Path(resolved).parent.resolve()
    if archive_root == ledger_dir or ledger_dir in archive_root.parents:
        raise RetirementError(
            f"refusing to retire: archive destination {archive_root} is inside "
            f"the ledger directory {ledger_dir}. Archive outside the chain "
            f"being retired."
        )

    human_decision: str = confirm_interactively(
        facts,
        str(archive_root),
        stdin_isatty=stdin_isatty,
        prompt=prompt,
        env=env,
    )

    constitution_hash, sources, version = _load_constitution()

    try:
        _copy_segments(resolved, archive_root, segments)
    except OSError as exc:
        raise RetirementError(
            f"refusing to retire: archiving to {archive_root} failed ({exc}). "
            f"The live chain was not touched."
        ) from exc

    archive_ledger: str = str(archive_root / Path(resolved).name)
    archive_result: dict = verify_chain(archive_ledger)
    _check_archive_matches(archive_result, facts, archive_root)

    summary: dict = build_anchor_summary(
        facts,
        archive_path=str(archive_root),
        reason=stripped_reason,
        human_decision=human_decision,
        constitution_version=version,
        remediation=remediation,
        retired_at=clock().isoformat(),
    )
    defects: list[str] = validate_anchor_summary(summary)
    if defects:
        raise RetirementError(
            "refusing to retire: the anchor would not conform to C-008(c): "
            + "; ".join(defects)
            + ". The live chain was not touched."
        )

    # Move the live chain aside rather than deleting it, then re-check it
    # against the archive now that nothing can be appended to it.
    # _check_staged_matches restores the moved segments and refuses if an
    # entry landed in between, so the race is a refusal rather than a loss.
    staging: Path = ledger_dir / f".retiring-{started.strftime('%Y-%m-%dT%H%M%SZ')}"
    moved: list[str] = _stage_segments(resolved, segments, staging)
    _check_staged_matches(staging, resolved, moved, facts, archive_root)

    anchor: dict = append_entry(
        {
            "verdict": ANCHOR_VERDICT,
            "pipeline_error": False,
            "constitution_hash": constitution_hash,
            "constitution_sources": sources,
            "change": {
                "file": _project_relative(resolved),
                "tool": ANCHOR_TOOL,
                "diff_summary": summary,
            },
            "challenger": {},
            "defender": {},
            "oracle": {},
        },
        path=resolved,
    )

    if anchor.get("previous_hash") != _GENESIS_MARKER:
        # An entry landed between the chain being moved aside and the anchor
        # being written, so that entry is the successor's genesis and the anchor
        # merely links to it. verify_chain still passes on such a chain, and
        # audit-retirement reads the first entry, so this would silently produce
        # a successor whose opening record is not the retirement. Nothing is
        # lost, but it must not pass unnoticed.
        raise RetirementError(
            f"the anchor did not open the successor chain: its previous_hash "
            f"is {anchor.get('previous_hash')!r} rather than "
            f"{_GENESIS_MARKER}, so another entry was appended first. The "
            f"verified archive is at {archive_root} and the retired chain is "
            f"staged at {staging}. Stop the concurrent writer before "
            f"continuing."
        )

    entry_defects: list[str] = validate_anchor(anchor)
    if entry_defects:
        raise RetirementError(
            f"the anchor was written but does not conform: "
            f"{'; '.join(entry_defects)}. The verified archive is at "
            f"{archive_root}."
        )

    successor: dict = verify_chain(resolved)
    if not successor.get("valid"):
        raise RetirementError(
            f"the anchor was written but the successor chain does not verify "
            f"({successor.get('failure_type', 'unknown')}: "
            f"{successor.get('message', 'no detail')}). The verified archive "
            f"is at {archive_root}."
        )

    # Everything is verified and the anchor is in place, so the staged copy is
    # now redundant with the archive and can go.
    _discard_staging(staging)

    return {
        "archive_path": str(archive_root),
        "archive_entries": int(archive_result.get("entries", 0)),
        "anchor": anchor,
        "successor": successor,
    }


def audit_retirement(
    anchor_entry: dict,
    archive_ledger_path: str | None = None,
) -> dict:
    """Perform C-008's auditor check against a retirement's archive.

    Verifies the archived chain independently and confirms its tip hash and
    entry count equal the values the anchor recorded. Returns a report with an
    ``ok`` flag and the compared values, rather than raising, so a caller can
    print a full picture of a mismatch instead of the first difference.

    ``archive_ledger_path`` defaults to the ledger file inside the anchor's
    recorded ``archive_path``, so an auditor who has the chain has everything.
    """
    defects: list[str] = validate_anchor(anchor_entry)
    if defects:
        return {
            "ok": False,
            "reason": "anchor does not conform to C-008(c)",
            "defects": defects,
        }

    summary: dict = anchor_entry["change"]["diff_summary"]
    recorded_path: str = str(summary["archive_path"])
    recorded: Path = Path(recorded_path)
    if archive_ledger_path:
        resolved: str = archive_ledger_path
    elif recorded.suffix == ".json":
        # The 2026-07-24 retirement archived a single JSON array and recorded
        # that file directly. Retirements written by execute_retirement record
        # the archive *directory*, because a chain is now up to three segments.
        # Both are legitimate values for C-008(c)'s "verbatim archive path", so
        # the auditor reads either rather than only the shape it writes.
        # Keyed on the suffix, not on is_file, so a missing archive is reported
        # as a failed verification rather than as a confusing path.
        resolved = str(recorded)
    else:
        resolved = str(recorded / Path(resolve_ledger_path()).name)

    result: dict = verify_chain(resolved)
    if not result.get("valid"):
        return {
            "ok": False,
            "reason": "the archive does not verify",
            "archive_path": recorded_path,
            "archive_ledger": resolved,
            "failure_type": result.get("failure_type", "unknown"),
            "message": result.get("message", ""),
        }

    expected_tip: str = str(summary["predecessor_tip_hash"])
    expected_entries: int = int(summary["predecessor_entries"])
    raw_tips: Any = result.get("tips", [])
    found_tips: list[str] = raw_tips if isinstance(raw_tips, list) else []
    found_entries: int = int(result.get("entries", -1))

    tip_ok: bool = found_tips == [expected_tip]
    count_ok: bool = found_entries == expected_entries

    return {
        "ok": tip_ok and count_ok,
        "archive_path": recorded_path,
        "archive_ledger": resolved,
        "expected_tip": expected_tip,
        "found_tips": found_tips,
        "tip_matches": tip_ok,
        "expected_entries": expected_entries,
        "found_entries": found_entries,
        "count_matches": count_ok,
    }
