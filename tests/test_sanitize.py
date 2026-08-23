"""Tests for C-008 published-copy sanitation validation and audit.

Sanitation authorizes removing ledger data that was already distributed
through a VCS remote, which retirement cannot reach. That is a powerful
operation, so the point of these tests is the refusals: a record that looks
well-formed while asserting something false must not pass, because the
whole value of the record is that an auditor can check it independently.

Run: python -m unittest tests.test_sanitize -v
"""

import hashlib
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_REPO_ROOT: Path = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from ledger.sanitize import (  # noqa: E402
    DIGEST_ALGORITHM,
    SANITATION_EVENT,
    SANITATION_VERDICT,
    audit_sanitation,
    digest_file,
    validate_sanitation_record,
    validate_sanitation_summary,
)

_GENESIS: str = "a" * 64


def _summary(**overrides: object) -> dict:
    base: dict = {
        "event": SANITATION_EVENT,
        "human_decision": "Authorized at a TTY on 2026-08-22 by the owner.",
        "reason": "Published copies carried unrelated projects' source.",
        "refs": [
            {
                "ref": "refs/heads/main",
                "pre_image": "1" * 40,
                "post_image": "2" * 40,
            }
        ],
        "backup_id": "BKP-A7F3",
        "backup_digest_algorithm": DIGEST_ALGORITHM,
        "backup_digest": "b" * 64,
        "retention_owner": "repository owner",
        "retention_policy": "Destroyed once the purge is confirmed complete.",
        "chain_verified_valid": True,
        "chain_genesis_hash": _GENESIS,
        "chain_entry_count": 448,
    }
    base.update(overrides)
    return base


def _record(**overrides: object) -> dict:
    return {
        "verdict": SANITATION_VERDICT,
        "change": {
            "file": "published copies",
            "tool": "PublishedCopySanitation",
            "diff_summary": _summary(**overrides),
        },
    }


class WellFormedRecordTests(unittest.TestCase):
    def test_conforming_summary_has_no_defects(self) -> None:
        self.assertEqual(validate_sanitation_summary(_summary()), [])

    def test_conforming_record_has_no_defects(self) -> None:
        self.assertEqual(validate_sanitation_record(_record()), [])

    def test_entry_hash_is_not_required(self) -> None:
        """append_entry computes it after assembly, as with validate_anchor."""
        record: dict = _record()
        self.assertNotIn("entry_hash", record)
        self.assertEqual(validate_sanitation_record(record), [])


class RefusalTests(unittest.TestCase):
    """Each case is a record that would otherwise look legitimate."""

    def _has_defect(self, defects: list[str], needle: str) -> None:
        self.assertTrue(
            any(needle in d for d in defects),
            f"expected a defect mentioning {needle!r}, got {defects}",
        )

    def test_chain_not_verified_is_refused(self) -> None:
        """The one thing sanitation promises not to change."""
        defects = validate_sanitation_summary(
            _summary(chain_verified_valid=False)
        )
        self._has_defect(defects, "chain_verified_valid must be true")

    def test_missing_human_decision_is_refused(self) -> None:
        summary = _summary()
        del summary["human_decision"]
        self._has_defect(
            validate_sanitation_summary(summary), "human_decision"
        )

    def test_backup_path_instead_of_opaque_id_is_refused(self) -> None:
        """A path in the record reintroduces the leak this boundary closed."""
        defects = validate_sanitation_summary(
            _summary(backup_id="C:/Users/someone/backups/bundle.gpg")
        )
        self._has_defect(defects, "opaque identifier")

    def test_path_smuggled_into_retention_owner_is_refused(self) -> None:
        defects = validate_sanitation_summary(
            _summary(retention_owner="/home/someone/keys")
        )
        self._has_defect(defects, "filesystem path")

    def test_short_digest_is_refused(self) -> None:
        defects = validate_sanitation_summary(_summary(backup_digest="abc123"))
        self._has_defect(defects, "sha256 hex digest")

    def test_wrong_digest_algorithm_is_refused(self) -> None:
        defects = validate_sanitation_summary(
            _summary(backup_digest_algorithm="md5")
        )
        self._has_defect(defects, "backup_digest_algorithm")

    def test_empty_ref_list_is_refused(self) -> None:
        defects = validate_sanitation_summary(_summary(refs=[]))
        self._has_defect(defects, "non-empty list")

    def test_unchanged_ref_is_refused(self) -> None:
        """A ref whose images match is not evidence of a rewrite."""
        defects = validate_sanitation_summary(
            _summary(
                refs=[
                    {
                        "ref": "refs/heads/main",
                        "pre_image": "1" * 40,
                        "post_image": "1" * 40,
                    }
                ]
            )
        )
        self._has_defect(defects, "pre_image equals post_image")

    def test_short_ref_name_is_refused(self) -> None:
        defects = validate_sanitation_summary(
            _summary(
                refs=[
                    {
                        "ref": "main",
                        "pre_image": "1" * 40,
                        "post_image": "2" * 40,
                    }
                ]
            )
        )
        self._has_defect(defects, "full refname")

    def test_non_integer_entry_count_is_refused(self) -> None:
        defects = validate_sanitation_summary(_summary(chain_entry_count="448"))
        self._has_defect(defects, "positive integer")

    def test_wrong_verdict_is_refused(self) -> None:
        record = _record()
        record["verdict"] = "PASS"
        self._has_defect(validate_sanitation_record(record), "verdict must be")

    def test_non_object_inputs_are_refused(self) -> None:
        self.assertTrue(validate_sanitation_summary("nope"))
        self.assertTrue(validate_sanitation_record(["nope"]))


class DigestTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp: Path = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, str(self._tmp), True)

    def test_digest_matches_hashlib(self) -> None:
        target: Path = self._tmp / "backup.gpg"
        target.write_bytes(b"ciphertext")
        self.assertEqual(
            digest_file(target), hashlib.sha256(b"ciphertext").hexdigest()
        )

    def test_missing_file_returns_none_not_raises(self) -> None:
        self.assertIsNone(digest_file(self._tmp / "absent.gpg"))


class AuditTests(unittest.TestCase):
    """The audit checks the record, the backup, and the live chain."""

    def setUp(self) -> None:
        self._tmp: Path = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, str(self._tmp), True)
        self.backup: Path = self._tmp / "backup.gpg"
        self.backup.write_bytes(b"ciphertext")
        self.digest: str = hashlib.sha256(b"ciphertext").hexdigest()

    def _healthy_chain(self) -> dict:
        return {"valid": True, "genesis_hash": _GENESIS, "entries": 448}

    def test_matching_digest_and_chain_passes(self) -> None:
        record = _record(backup_digest=self.digest)
        with patch("ledger.verify.verify_chain", return_value=self._healthy_chain()):
            result = audit_sanitation(record, self.backup)
        self.assertEqual(result["defects"], [])
        self.assertTrue(result["valid"])
        self.assertTrue(result["digest_matches"])

    def test_digest_mismatch_fails(self) -> None:
        record = _record(backup_digest="c" * 64)
        with patch("ledger.verify.verify_chain", return_value=self._healthy_chain()):
            result = audit_sanitation(record, self.backup)
        self.assertFalse(result["valid"])
        self.assertFalse(result["digest_matches"])
        self.assertTrue(any("digest mismatch" in d for d in result["defects"]))

    def test_structure_only_audit_says_so(self) -> None:
        record = _record(backup_digest=self.digest)
        with patch("ledger.verify.verify_chain", return_value=self._healthy_chain()):
            result = audit_sanitation(record)
        self.assertFalse(result["digest_checked"])
        self.assertIn("proves nothing about the backup", result["detail"])

    def test_broken_live_chain_fails_the_audit(self) -> None:
        """A sanitation cannot be sound if the chain it spared is broken."""
        record = _record(backup_digest=self.digest)
        broken = {"valid": False, "failure_type": "MISSING_PARENT"}
        with patch("ledger.verify.verify_chain", return_value=broken):
            result = audit_sanitation(record, self.backup)
        self.assertFalse(result["valid"])
        self.assertTrue(
            any("does not verify" in d for d in result["defects"])
        )

    def test_genesis_mismatch_fails_the_audit(self) -> None:
        """The record's claim is checked against the chain, not trusted."""
        record = _record(backup_digest=self.digest)
        moved = {"valid": True, "genesis_hash": "f" * 64, "entries": 448}
        with patch("ledger.verify.verify_chain", return_value=moved):
            result = audit_sanitation(record, self.backup)
        self.assertFalse(result["valid"])
        self.assertTrue(any("genesis mismatch" in d for d in result["defects"]))

    def test_shrunk_chain_fails_the_audit(self) -> None:
        record = _record(backup_digest=self.digest)
        shrunk = {"valid": True, "genesis_hash": _GENESIS, "entries": 400}
        with patch("ledger.verify.verify_chain", return_value=shrunk):
            result = audit_sanitation(record, self.backup)
        self.assertFalse(result["valid"])
        self.assertTrue(
            any("appear to have been removed" in d for d in result["defects"])
        )

    def test_chain_growth_is_accepted(self) -> None:
        """Append-only: more entries later is the expected state."""
        record = _record(backup_digest=self.digest)
        grown = {"valid": True, "genesis_hash": _GENESIS, "entries": 999}
        with patch("ledger.verify.verify_chain", return_value=grown):
            result = audit_sanitation(record, self.backup)
        self.assertTrue(result["valid"])


class CliAuditTests(unittest.TestCase):
    """The auditor a person actually runs."""

    def _run(self, entries: list[dict], backup: str | None = None) -> tuple[int, str]:
        import io
        from contextlib import redirect_stdout

        from cli.commands import cmd_audit_sanitation

        out = io.StringIO()
        with patch("cli.commands.load_ledger", return_value=entries):
            with redirect_stdout(out):
                code = cmd_audit_sanitation(backup)
        return code, out.getvalue()

    def test_no_records_is_not_a_failure(self) -> None:
        code, out = self._run([{"verdict": "PASS"}])
        self.assertEqual(code, 0)
        self.assertIn("NONE RECORDED", out)

    def test_valid_record_reports_valid(self) -> None:
        record = _record()
        healthy = {"valid": True, "genesis_hash": _GENESIS, "entries": 448}
        with patch("ledger.verify.verify_chain", return_value=healthy):
            code, out = self._run([record])
        self.assertEqual(code, 0)
        self.assertIn("Sanitation: VALID", out)
        self.assertIn("BKP-A7F3", out)
        self.assertIn("not checked", out)

    def test_invalid_record_exits_nonzero(self) -> None:
        record = _record(chain_verified_valid=False)
        healthy = {"valid": True, "genesis_hash": _GENESIS, "entries": 448}
        with patch("ledger.verify.verify_chain", return_value=healthy):
            code, out = self._run([record])
        self.assertEqual(code, 1)
        self.assertIn("Sanitation: INVALID", out)

    def test_unreadable_ledger_is_reported_not_raised(self) -> None:
        from cli.commands import cmd_audit_sanitation
        from ledger.chain import LedgerReadError

        with patch(
            "cli.commands.load_ledger", side_effect=LedgerReadError("corrupt")
        ):
            self.assertEqual(cmd_audit_sanitation(), 1)


if __name__ == "__main__":
    unittest.main()
