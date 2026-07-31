"""Tests for chain retirement, the sole bounded exception C-008 allows.

Retirement is the most safety-critical operation the constitution permits, so
the properties under test are mostly *refusals*. Nearly every case here asserts
that the live chain is byte-identical after the call, because the guarantee
being bought is not that retirement works but that a retirement which should not
happen cannot happen, and that one which fails midway loses nothing.

Run: python -m unittest tests.test_retire -v
"""

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

_REPO_ROOT: Path = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from cli.__main__ import _flag_value  # noqa: E402
from ledger import retire  # noqa: E402
from ledger.chain import (  # noqa: E402
    ANCHOR_VERDICT,
    META_FILENAME,
    append_entry,
    resolve_entries_dir,
)
from ledger.retire import (  # noqa: E402
    ANCHOR_TOOL,
    CONFIRMATION_PHRASE,
    MIN_REASON_CHARS,
    RetirementError,
    audit_retirement,
    build_anchor_summary,
    execute_retirement,
    plan_retirement,
    validate_anchor,
    validate_anchor_summary,
)
from ledger.verify import verify_chain  # noqa: E402
from tests._ledger_fixtures import build_valid_chain  # noqa: E402

REASON: str = (
    "The chain accumulated governance receipts from unrelated projects and "
    "contains third-party source that must not be published."
)


def _always_tty() -> bool:
    return True


def _types_phrase(_prompt: str) -> str:
    return CONFIRMATION_PHRASE


class RetirementTestCase(unittest.TestCase):
    """Shared scaffolding. Every chain lives in a tempdir, never the real one."""

    def setUp(self) -> None:
        self._tmp: str = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self._tmp, True)
        self.ledger_dir: Path = Path(self._tmp) / "chain"
        self.ledger: str = str(self.ledger_dir / "bench-ledger.json")
        self.archive_dir: str = str(Path(self._tmp) / "archive")

    def make_legacy_chain(self, legacy: int = 4, new: int = 1) -> None:
        """A first-retirement shape: frozen array, meta pin, and entry files."""
        self.ledger_dir.mkdir(parents=True, exist_ok=True)
        entries: list[dict] = build_valid_chain(legacy)
        Path(self.ledger).write_text(
            json.dumps(entries, indent=2), encoding="utf-8"
        )
        (self.ledger_dir / META_FILENAME).write_text(
            json.dumps(
                {
                    "entry_count": len(entries),
                    "latest_hash": entries[-1]["entry_hash"],
                    "created": "2026-01-01T00:00:00+00:00",
                    "last_updated": "2026-01-01T00:00:00+00:00",
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        for index in range(new):
            self.append(f"new_{index}.py")

    def make_entries_only_chain(self, count: int = 3) -> None:
        """The shape every chain has after a retirement: entry files alone."""
        for index in range(count):
            self.append(f"file_{index}.py")

    def append(self, file_ref: str = "app/main.py") -> dict:
        return append_entry(
            {
                "verdict": "PASS",
                "constitution_hash": "abc123",
                "change": {
                    "file": file_ref,
                    "tool": "Write",
                    "diff_summary": {},
                },
            },
            path=self.ledger,
        )

    def snapshot(self) -> dict[str, bytes]:
        """Every byte of the live chain, for proving a refusal changed nothing."""
        out: dict[str, bytes] = {}
        if not self.ledger_dir.is_dir():
            return out
        for path in sorted(self.ledger_dir.rglob("*")):
            if path.is_file():
                out[str(path.relative_to(self.ledger_dir))] = path.read_bytes()
        return out

    def retire(self, **overrides: Any) -> dict:
        kwargs: dict[str, Any] = {
            "archive_dir": self.archive_dir,
            "reason": REASON,
            "ledger_path": self.ledger,
            "stdin_isatty": _always_tty,
            "prompt": _types_phrase,
            "env": {},
        }
        kwargs.update(overrides)
        return execute_retirement(**kwargs)

    def assert_refused(self, expected: str, **overrides: Any) -> str:
        """Assert retirement refuses and the live chain is byte-identical."""
        before: dict[str, bytes] = self.snapshot()
        with self.assertRaises(RetirementError) as caught:
            self.retire(**overrides)
        self.assertEqual(
            self.snapshot(), before, "a refused retirement modified the chain"
        )
        message: str = str(caught.exception)
        self.assertIn(expected, message)
        return message


class HappyPathTests(RetirementTestCase):
    def test_retiring_a_legacy_shaped_chain_archives_and_opens_a_successor(
        self,
    ) -> None:
        self.make_legacy_chain(legacy=4, new=1)
        before: dict = verify_chain(self.ledger)
        self.assertTrue(before["valid"])
        self.assertEqual(before["entries"], 5)

        result: dict = self.retire()

        # The archive holds every segment that existed, and verifies.
        archive: Path = Path(result["archive_path"])
        self.assertEqual(
            sorted(p.name for p in archive.iterdir()),
            ["bench-ledger.json", "entries", META_FILENAME],
        )
        self.assertEqual(result["archive_entries"], 5)
        self.assertTrue(verify_chain(str(archive / "bench-ledger.json"))["valid"])

        # The successor chain is entries-only and opens at GENESIS.
        self.assertEqual(
            sorted(p.name for p in self.ledger_dir.iterdir()), ["entries"]
        )
        anchor: dict = result["anchor"]
        self.assertEqual(anchor["previous_hash"], "GENESIS")
        self.assertEqual(anchor["verdict"], ANCHOR_VERDICT)
        self.assertEqual(anchor["change"]["tool"], ANCHOR_TOOL)
        self.assertEqual(validate_anchor(anchor), [])
        self.assertTrue(result["successor"]["valid"])
        self.assertEqual(result["successor"]["entries"], 1)

    def test_the_anchor_records_every_element_c008_enumerates(self) -> None:
        self.make_legacy_chain(legacy=3, new=0)
        before: dict = verify_chain(self.ledger)

        result: dict = self.retire()
        summary: dict = result["anchor"]["change"]["diff_summary"]

        self.assertEqual(summary["predecessor_tip_hash"], before["tips"][0])
        self.assertEqual(summary["predecessor_genesis_hash"], before["genesis_hash"])
        self.assertEqual(summary["predecessor_entries"], before["entries"])
        # Timestamp VALUES, not hashes or indices, as C-008(c) names.
        self.assertEqual(summary["predecessor_first_entry"], before["first_entry"])
        self.assertEqual(summary["predecessor_last_entry"], before["last_entry"])
        self.assertEqual(summary["archive_path"], result["archive_path"])
        self.assertTrue(summary["archive_verified_valid"])
        self.assertEqual(summary["archive_retention"], "indefinite")
        self.assertEqual(summary["reason"], REASON)
        self.assertIn("TTY", summary["human_decision"])
        self.assertTrue(summary["retired_at"])

    def test_a_second_retirement_works_on_an_entries_only_chain(self) -> None:
        """The case that breaks a naive implementation.

        After one retirement there is no frozen array and no meta pin, so a
        retirement that copies those unconditionally raises FileNotFoundError.
        """
        self.make_legacy_chain(legacy=3, new=1)
        self.retire()
        self.assertEqual(
            sorted(p.name for p in self.ledger_dir.iterdir()), ["entries"]
        )
        self.append("after.py")

        second: dict = self.retire(
            archive_dir=str(Path(self._tmp) / "archive-two")
        )

        archive: Path = Path(second["archive_path"])
        self.assertEqual([p.name for p in archive.iterdir()], ["entries"])
        self.assertEqual(second["archive_entries"], 2)
        self.assertTrue(second["successor"]["valid"])
        self.assertEqual(validate_anchor(second["anchor"]), [])

    def test_change_file_is_relative_so_the_anchor_is_never_redacted(self) -> None:
        self.make_entries_only_chain(2)
        anchor: dict = self.retire()["anchor"]
        recorded: str = anchor["change"]["file"]
        self.assertFalse(os.path.isabs(recorded))
        self.assertNotIn("\\", recorded)
        # chain._redact_external_diff stamps this key; a relative path must
        # never trigger it, or the anchor would misrepresent itself.
        self.assertNotIn("redacted", anchor["change"]["diff_summary"])

    def test_remediation_is_optional_and_recorded_when_given(self) -> None:
        self.make_entries_only_chain(2)
        summary: dict = self.retire(remediation="PR #13")["anchor"]["change"][
            "diff_summary"
        ]
        self.assertEqual(summary["remediation_landed"], "PR #13")


class HumanGateTests(RetirementTestCase):
    """C-008(a): an explicit human decision, never agent-initiated."""

    def test_refuses_when_stdin_is_not_a_tty(self) -> None:
        self.make_entries_only_chain()
        self.assert_refused("not a TTY", stdin_isatty=lambda: False)

    def test_refuses_when_bench_subprocess_is_set(self) -> None:
        self.make_entries_only_chain()
        self.assert_refused("BENCH_SUBPROCESS", env={"BENCH_SUBPROCESS": "1"})

    def test_refuses_when_claudecode_is_set(self) -> None:
        self.make_entries_only_chain()
        self.assert_refused("CLAUDECODE", env={"CLAUDECODE": "1"})

    def test_refuses_when_ci_is_set(self) -> None:
        self.make_entries_only_chain()
        self.assert_refused("CI", env={"CI": "1"})

    def test_refuses_when_the_phrase_is_not_matched(self) -> None:
        self.make_entries_only_chain()
        self.assert_refused("confirmation phrase", prompt=lambda _p: "yes")

    def test_the_prompt_states_the_trigger_and_the_format_change_non_example(
        self,
    ) -> None:
        """#22's lesson, machine-checked rather than left to prose."""
        self.make_entries_only_chain()
        facts: dict = plan_retirement(self.ledger)
        text: str = retire.render_confirmation(facts, self.archive_dir)
        self.assertIn("must not be published", text)
        self.assertIn("storage-format change is NOT such a trigger", text)
        self.assertIn("#22", text)
        self.assertIn(CONFIRMATION_PHRASE, text)

    def test_no_env_marker_can_be_dropped_silently(self) -> None:
        self.assertEqual(
            set(retire.AGENT_ENV_MARKERS),
            {"BENCH_SUBPROCESS", "CLAUDECODE", "CI"},
        )


class RefusalTests(RetirementTestCase):
    def test_refuses_a_reason_under_the_floor(self) -> None:
        self.make_entries_only_chain()
        self.assert_refused(str(MIN_REASON_CHARS), reason="too short")

    def test_refuses_an_empty_chain(self) -> None:
        self.ledger_dir.mkdir(parents=True, exist_ok=True)
        with self.assertRaises(RetirementError) as caught:
            self.retire()
        self.assertIn("empty chain", str(caught.exception))

    def test_refuses_a_chain_that_does_not_verify(self) -> None:
        self.make_entries_only_chain(2)
        entries_dir: Path = Path(resolve_entries_dir(self.ledger))
        victim: Path = sorted(entries_dir.glob("*.json"))[0]
        tampered: dict = json.loads(victim.read_text(encoding="utf-8"))
        tampered["constitution_hash"] = "tampered"
        victim.write_text(json.dumps(tampered, indent=2), encoding="utf-8")
        self.assert_refused("does not verify")

    def test_refuses_a_forked_chain(self) -> None:
        self.make_entries_only_chain(1)
        entries_dir: Path = Path(resolve_entries_dir(self.ledger))
        branch_a: dict = self.append("a.py")
        os.remove(entries_dir / f"{branch_a['entry_hash']}.json")
        self.append("b.py")
        (entries_dir / f"{branch_a['entry_hash']}.json").write_text(
            json.dumps(branch_a, indent=2), encoding="utf-8"
        )
        self.assertEqual(len(verify_chain(self.ledger)["tips"]), 2)

        message: str = self.assert_refused("2 tips")
        self.assertIn("reconciles the fork", message)

    def test_refuses_an_archive_destination_that_already_exists(self) -> None:
        self.make_entries_only_chain()
        with mock.patch.object(retire, "_archive_root") as fake:
            existing: Path = Path(self._tmp) / "already-there"
            existing.mkdir()
            fake.return_value = existing
            self.assert_refused("already")

    def test_refuses_archiving_inside_the_ledger_directory(self) -> None:
        self.make_entries_only_chain()
        self.assert_refused(
            "inside the ledger directory",
            archive_dir=str(self.ledger_dir / "self-archive"),
        )

    def test_a_failed_archive_verification_leaves_the_originals_intact(
        self,
    ) -> None:
        """The critical ordering property of C-008(b).

        The archive is verified before anything is removed, so an archive that
        does not verify must abort with the live chain byte-identical.
        """
        self.make_legacy_chain(legacy=3, new=1)
        before: dict[str, bytes] = self.snapshot()
        real_verify = retire.verify_chain

        def verify_live_but_not_archive(path: str | None = None) -> dict:
            if path and Path(path).parent.parent == Path(self.archive_dir):
                return {
                    "valid": False,
                    "failure_type": "HASH_MISMATCH",
                    "message": "synthetic archive corruption",
                }
            return real_verify(path)

        with mock.patch.object(
            retire, "verify_chain", side_effect=verify_live_but_not_archive
        ):
            with self.assertRaises(RetirementError) as caught:
                self.retire()

        self.assertIn("does not verify", str(caught.exception))
        self.assertIn("was not touched", str(caught.exception))
        self.assertEqual(self.snapshot(), before)

    def test_a_count_mismatch_between_archive_and_live_chain_aborts(self) -> None:
        self.make_entries_only_chain(3)
        before: dict[str, bytes] = self.snapshot()
        real_verify = retire.verify_chain

        def shrink_the_archive(path: str | None = None) -> dict:
            result: dict = real_verify(path)
            if path and Path(path).parent.parent == Path(self.archive_dir):
                result = dict(result)
                result["entries"] = 1
            return result

        with mock.patch.object(
            retire, "verify_chain", side_effect=shrink_the_archive
        ):
            with self.assertRaises(RetirementError) as caught:
                self.retire()

        self.assertIn("entries but the live chain has", str(caught.exception))
        self.assertEqual(self.snapshot(), before)


class AnchorSchemaTests(unittest.TestCase):
    def _facts(self) -> dict:
        return {
            "ledger_path": "ledger/bench-ledger.json",
            "entries": 12,
            "tip_hash": "t" * 64,
            "genesis_hash": "g" * 64,
            "first_entry": "2026-01-01T00:00:00+00:00",
            "last_entry": "2026-02-01T00:00:00+00:00",
            "segments": ["entries"],
        }

    def _summary(self, **overrides: Any) -> dict:
        summary: dict = build_anchor_summary(
            self._facts(),
            archive_path="/archives/bench-ledger-2026",
            reason=REASON,
            human_decision="Confirmed at a TTY.",
            constitution_version=5,
        )
        summary.update(overrides)
        return summary

    def test_a_well_formed_summary_has_no_defects(self) -> None:
        self.assertEqual(validate_anchor_summary(self._summary()), [])

    def test_every_required_field_is_actually_required(self) -> None:
        for field in retire._REQUIRED_SUMMARY_FIELDS:
            with self.subTest(field=field):
                summary: dict = self._summary()
                del summary[field]
                defects: list[str] = validate_anchor_summary(summary)
                self.assertTrue(
                    any(field in defect for defect in defects),
                    f"removing {field} produced no defect naming it",
                )

    def test_rejects_an_unverified_archive(self) -> None:
        defects: list[str] = validate_anchor_summary(
            self._summary(archive_verified_valid=False)
        )
        self.assertTrue(any("C-008(b)" in d for d in defects))

    def test_rejects_retention_other_than_indefinite(self) -> None:
        defects: list[str] = validate_anchor_summary(
            self._summary(archive_retention="90 days")
        )
        self.assertTrue(any("C-008(d)" in d for d in defects))

    def test_rejects_a_non_positive_entry_count(self) -> None:
        self.assertTrue(
            validate_anchor_summary(self._summary(predecessor_entries=0))
        )
        self.assertTrue(
            validate_anchor_summary(self._summary(predecessor_entries="12"))
        )

    def test_rejects_a_reason_under_the_floor(self) -> None:
        self.assertTrue(validate_anchor_summary(self._summary(reason="nope")))

    def test_authority_records_history_not_the_version_in_force(self) -> None:
        """The two legitimately differ and must not be reconciled.

        ``authority`` names the version in which C-008 gained its retirement
        clause. The version in force at retirement time is recorded separately.
        """
        summary: dict = self._summary()
        self.assertIn("version 2", summary["authority"])
        self.assertEqual(summary["constitution_version"], 5)

    def test_validate_anchor_checks_the_entry_level_requirements(self) -> None:
        entry: dict = {
            "verdict": ANCHOR_VERDICT,
            "change": {"tool": ANCHOR_TOOL, "diff_summary": self._summary()},
        }
        self.assertEqual(validate_anchor(entry), [])

        entry["verdict"] = "PASS"
        self.assertTrue(any("verdict" in d for d in validate_anchor(entry)))

        entry["verdict"] = ANCHOR_VERDICT
        entry["change"]["tool"] = "Write"
        self.assertTrue(any("change.tool" in d for d in validate_anchor(entry)))

    def test_validate_anchor_does_not_require_an_entry_hash(self) -> None:
        """It must be callable before append_entry computes the hash."""
        entry: dict = {
            "verdict": ANCHOR_VERDICT,
            "change": {"tool": ANCHOR_TOOL, "diff_summary": self._summary()},
        }
        self.assertNotIn("entry_hash", entry)
        self.assertEqual(validate_anchor(entry), [])


class AuditTests(RetirementTestCase):
    def test_audit_confirms_a_real_retirement(self) -> None:
        self.make_legacy_chain(legacy=3, new=1)
        result: dict = self.retire()
        report: dict = audit_retirement(result["anchor"])
        self.assertTrue(report["ok"])
        self.assertTrue(report["tip_matches"])
        self.assertTrue(report["count_matches"])
        self.assertEqual(report["found_entries"], 4)

    def test_audit_detects_a_tip_mismatch(self) -> None:
        self.make_entries_only_chain(2)
        anchor: dict = self.retire()["anchor"]
        anchor["change"]["diff_summary"]["predecessor_tip_hash"] = "z" * 64
        report: dict = audit_retirement(anchor)
        self.assertFalse(report["ok"])
        self.assertFalse(report["tip_matches"])
        self.assertTrue(report["count_matches"])

    def test_audit_detects_a_count_mismatch(self) -> None:
        self.make_entries_only_chain(2)
        anchor: dict = self.retire()["anchor"]
        anchor["change"]["diff_summary"]["predecessor_entries"] = 999
        report: dict = audit_retirement(anchor)
        self.assertFalse(report["ok"])
        self.assertFalse(report["count_matches"])

    def test_audit_rejects_a_malformed_anchor(self) -> None:
        report: dict = audit_retirement({"verdict": "PASS", "change": {}})
        self.assertFalse(report["ok"])
        self.assertTrue(report["defects"])

    def test_audit_fails_closed_on_a_missing_archive(self) -> None:
        self.make_entries_only_chain(2)
        anchor: dict = self.retire()["anchor"]
        shutil.rmtree(anchor["change"]["diff_summary"]["archive_path"])
        report: dict = audit_retirement(anchor)
        self.assertFalse(report["ok"])

    def test_a_file_shaped_archive_path_resolves_to_itself(self) -> None:
        """The 2026-07-24 retirement recorded a file; new ones record a
        directory. The auditor must read either."""
        self.make_entries_only_chain(2)
        anchor: dict = self.retire()["anchor"]
        summary: dict = anchor["change"]["diff_summary"]
        archived_file: str = str(
            Path(summary["archive_path"]) / "bench-ledger.json"
        )
        summary["archive_path"] = archived_file

        report: dict = audit_retirement(anchor)
        self.assertEqual(report["archive_ledger"], archived_file)


class CliWiringTests(RetirementTestCase):
    def test_cmd_retire_uses_the_real_stdin_isatty(self) -> None:
        """The injectable seams must not hide a broken production path."""
        from cli.commands import cmd_retire

        self.make_entries_only_chain()
        fake_stdin = mock.Mock()
        fake_stdin.isatty.return_value = False

        with mock.patch.dict(os.environ, {}, clear=True):
            with mock.patch.object(sys, "stdin", fake_stdin):
                with mock.patch.dict(
                    os.environ, {"BENCH_LEDGER_PATH": self.ledger}
                ):
                    exit_code: int = cmd_retire(
                        archive_dir=self.archive_dir, reason=REASON
                    )

        self.assertEqual(exit_code, 1)
        fake_stdin.isatty.assert_called()

    def test_cmd_retire_requires_both_flags(self) -> None:
        from cli.commands import cmd_retire

        self.assertEqual(cmd_retire(archive_dir=None, reason=REASON), 1)
        self.assertEqual(
            cmd_retire(archive_dir=self.archive_dir, reason=None), 1
        )

    def test_flag_value_handles_absent_present_and_trailing(self) -> None:
        self.assertIsNone(_flag_value([], "--reason"))
        self.assertIsNone(_flag_value(["--reason"], "--reason"))
        self.assertEqual(
            _flag_value(["--reason", "because"], "--reason"), "because"
        )


class ProjectRelativeTests(unittest.TestCase):
    def test_recorded_path_uses_forward_slashes(self) -> None:
        recorded: str = retire._project_relative(
            str(_REPO_ROOT / "ledger" / "bench-ledger.json")
        )
        self.assertEqual(recorded, "ledger/bench-ledger.json")
        self.assertNotIn("\\", recorded)


class ConcurrencyTests(RetirementTestCase):
    """A governed edit landing mid-retirement must never cost an entry.

    Retirement reads the chain, archives it, and removes it, and those are not
    one atomic step. A receipt appended in the gap was previously deleted
    without ever reaching the archive or the anchor's count, which is exactly
    the removal of an entry C-008 forbids without exception.
    """

    def _staging_dirs(self) -> list[str]:
        return [
            p.name
            for p in self.ledger_dir.iterdir()
            if p.name.startswith(".retiring-")
        ]

    def test_an_append_after_archiving_is_refused_and_the_entry_survives(
        self,
    ) -> None:
        self.make_entries_only_chain(2)
        real_stage = retire._stage_segments

        def append_then_stage(
            ledger_path: str, segments: list[str], staging: Path
        ) -> list[str]:
            # A governance run in another session commits its receipt after the
            # archive was verified but before the chain is moved aside.
            self.append("raced.py")
            return real_stage(ledger_path, segments, staging)

        with mock.patch.object(
            retire, "_stage_segments", side_effect=append_then_stage
        ):
            with self.assertRaises(RetirementError) as caught:
                self.retire()

        self.assertIn("changed between being", str(caught.exception))
        after: dict = verify_chain(self.ledger)
        self.assertTrue(after["valid"])
        # The raced receipt is still here. That is the whole point.
        self.assertEqual(after["entries"], 3)
        self.assertEqual(self._staging_dirs(), [])

    def test_an_entry_appended_before_the_anchor_is_detected(self) -> None:
        """The successor's genesis must be the anchor, not a racing receipt.

        verify_chain passes on such a chain and audit-retirement reads the first
        entry, so without this check the retirement would silently produce a
        successor whose opening record is not the retirement.
        """
        self.make_entries_only_chain(2)
        real_append = retire.append_entry

        def racer_first(payload: dict, path: str | None = None) -> dict:
            if payload.get("verdict") == ANCHOR_VERDICT:
                real_append(
                    {
                        "verdict": "PASS",
                        "constitution_hash": "abc123",
                        "change": {
                            "file": "raced.py",
                            "tool": "Write",
                            "diff_summary": {},
                        },
                    },
                    path=path,
                )
            return real_append(payload, path=path)

        with mock.patch.object(
            retire, "append_entry", side_effect=racer_first
        ):
            with self.assertRaises(RetirementError) as caught:
                self.retire()

        self.assertIn("did not open the successor chain", str(caught.exception))

    def test_a_failed_move_restores_what_it_already_moved(self) -> None:
        """The old sequential delete could not do this.

        An OSError partway through left the array gone and the entry files
        behind with their parents missing, a chain plan_retirement then refuses,
        so "retire again" was impossible and recovery was manual surgery.
        """
        self.make_legacy_chain(legacy=2, new=1)
        before: dict[str, bytes] = self.snapshot()
        real_move = shutil.move
        calls: dict[str, int] = {"n": 0}

        def flaky_move(source: str, destination: str) -> Any:
            calls["n"] += 1
            if calls["n"] == 2:
                raise OSError("no space left on device")
            return real_move(source, destination)

        with mock.patch.object(shutil, "move", side_effect=flaky_move):
            with self.assertRaises(RetirementError) as caught:
                self.retire()

        self.assertIn("restored", str(caught.exception))
        self.assertEqual(self.snapshot(), before)

    def test_an_error_while_staged_restores_the_chain_and_reraises(self) -> None:
        self.make_entries_only_chain(2)
        before: dict[str, bytes] = self.snapshot()
        real_verify = retire.verify_chain

        def boom(path: str | None = None) -> dict:
            if path and ".retiring-" in str(path):
                raise RuntimeError("filesystem exploded")
            return real_verify(path)

        with mock.patch.object(retire, "verify_chain", side_effect=boom):
            with self.assertRaises(RuntimeError) as caught:
                self.retire()

        # The original error surfaces rather than being masked by cleanup.
        self.assertIn("exploded", str(caught.exception))
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self._staging_dirs(), [])

    def test_staging_is_discarded_only_after_the_retirement_succeeds(
        self,
    ) -> None:
        self.make_legacy_chain(legacy=2, new=1)
        result: dict = self.retire()

        self.assertEqual(self._staging_dirs(), [])
        self.assertEqual(
            sorted(p.name for p in self.ledger_dir.iterdir()), ["entries"]
        )
        archive_ledger: str = str(
            Path(result["archive_path"]) / "bench-ledger.json"
        )
        self.assertTrue(verify_chain(archive_ledger)["valid"])

    def test_the_local_genesis_marker_matches_the_writers(self) -> None:
        """Re-declared for independence, so assert the two have not drifted."""
        import ledger.chain as chain_module

        self.assertEqual(retire._GENESIS_MARKER, chain_module._GENESIS_MARKER)


if __name__ == "__main__":
    unittest.main()
