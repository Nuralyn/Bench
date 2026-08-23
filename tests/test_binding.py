"""Tests that a sanitation warrant is bound to the operation it authorizes.

A well-formed record is a warrant with no name on it. `validate_sanitation_record`
asks whether a record is shaped correctly and whether the chain is intact;
neither answers whether it authorizes *this* rewrite of *this* repository,
right now, and whether it has already been spent.

Every test here is a case where the record is perfectly valid and the
operation is still wrong. They exist because the first version of the push
gate checked none of them, and would have accepted a warrant for a different
repository, a mirror holding a different rewrite, or a second push under a
warrant already consumed by the first.

The purge-verification tests cover the other half: proving objects are gone
without mistaking an unanswered question for an answer.

Run: python -m unittest tests.test_binding -v
"""

import hashlib
import sys
import unittest
from pathlib import Path

_REPO_ROOT: Path = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from ledger.sanitize import (  # noqa: E402
    INCONCLUSIVE,
    PRESENT,
    REMOVED,
    SanitationError,
    classify_removal,
    object_endpoint,
    verify_binding,
    verify_copy,
    worktree_is_clean,
)

_REPO: str = "Nuralyn/Bench"
_PRE: str = "3226ee5a37b603c79b1c0b0cc2ff6d13d1185a1c"
_POST: str = "ca1d300c7bc3bb1f39021c244352a21c30723d28"
_OTHER: str = "9999999999999999999999999999999999999999"


def _summary(**overrides: object) -> dict:
    base: dict = {
        "repository": _REPO,
        "refs": [
            {
                "ref": "refs/heads/main",
                "pre_image": _PRE,
                "post_image": _POST,
            }
        ],
    }
    base.update(overrides)
    return base


class BindingHoldsTests(unittest.TestCase):
    def test_matching_remote_and_mirror_bind(self) -> None:
        defects = verify_binding(
            _summary(),
            _REPO,
            remote_refs={"refs/heads/main": _PRE},
            local_refs={"refs/heads/main": _POST},
        )
        self.assertEqual(defects, [])


class RepositoryBindingTests(unittest.TestCase):
    def test_record_without_a_repository_cannot_bind(self) -> None:
        """Records written before this check carry no repository."""
        summary = _summary()
        del summary["repository"]
        defects = verify_binding(
            summary,
            _REPO,
            remote_refs={"refs/heads/main": _PRE},
            local_refs={"refs/heads/main": _POST},
        )
        self.assertTrue(any("does not name a repository" in d for d in defects))

    def test_record_for_another_repository_is_refused(self) -> None:
        defects = verify_binding(
            _summary(repository="someone/else"),
            _REPO,
            remote_refs={"refs/heads/main": _PRE},
            local_refs={"refs/heads/main": _POST},
        )
        self.assertTrue(any("authorizes" in d for d in defects))


class RemoteDriftTests(unittest.TestCase):
    """The remote moved after the warrant was written."""

    def test_remote_pre_image_drift_is_refused(self) -> None:
        defects = verify_binding(
            _summary(),
            _REPO,
            remote_refs={"refs/heads/main": _OTHER},
            local_refs={"refs/heads/main": _POST},
        )
        self.assertTrue(any("remote moved" in d for d in defects))

    def test_ref_absent_from_the_remote_is_refused(self) -> None:
        defects = verify_binding(
            _summary(),
            _REPO,
            remote_refs={},
            local_refs={"refs/heads/main": _POST},
        )
        self.assertTrue(any("absent from the remote" in d for d in defects))


class LocalDriftTests(unittest.TestCase):
    """The mirror holds something other than what was authorized."""

    def test_local_post_image_drift_is_refused(self) -> None:
        defects = verify_binding(
            _summary(),
            _REPO,
            remote_refs={"refs/heads/main": _PRE},
            local_refs={"refs/heads/main": _OTHER},
        )
        self.assertTrue(
            any("is not the one that was authorized" in d for d in defects)
        )

    def test_ref_absent_from_the_mirror_is_refused(self) -> None:
        defects = verify_binding(
            _summary(),
            _REPO,
            remote_refs={"refs/heads/main": _PRE},
            local_refs={},
        )
        self.assertTrue(any("absent from the local mirror" in d for d in defects))


class ReplayTests(unittest.TestCase):
    def test_a_spent_warrant_cannot_be_reused(self) -> None:
        """The remote already sits at the post-image: the push happened."""
        defects = verify_binding(
            _summary(),
            _REPO,
            remote_refs={"refs/heads/main": _POST},
            local_refs={"refs/heads/main": _POST},
        )
        self.assertTrue(any("already used" in d for d in defects))

    def test_empty_ref_list_cannot_bind(self) -> None:
        defects = verify_binding(
            _summary(refs=[]),
            _REPO,
            remote_refs={"refs/heads/main": _PRE},
            local_refs={"refs/heads/main": _POST},
        )
        self.assertTrue(any("no refs to bind" in d for d in defects))


class ObjectEndpointTests(unittest.TestCase):
    """Probing a blob at the commit endpoint is a false all-clear."""

    def test_each_type_gets_its_own_endpoint(self) -> None:
        cases = {
            "blob": "git/blobs",
            "tree": "git/trees",
            "commit": "git/commits",
            "tag": "git/tags",
        }
        for object_type, path in cases.items():
            with self.subTest(object_type=object_type):
                self.assertEqual(
                    object_endpoint(object_type, _REPO, "abc"),
                    f"repos/{_REPO}/{path}/abc",
                )

    def test_a_blob_is_never_probed_as_a_commit(self) -> None:
        """The exact bug: /commit/<blob sha> 404s whatever the truth is."""
        endpoint = object_endpoint("blob", _REPO, "deadbeef")
        self.assertIn("git/blobs", endpoint)
        self.assertNotIn("commit", endpoint)

    def test_unknown_type_raises_rather_than_reporting_removed(self) -> None:
        with self.assertRaises(SanitationError):
            object_endpoint("submodule", _REPO, "abc")


class RemovalClassificationTests(unittest.TestCase):
    """Only 404 proves removal; an unanswered question is not a pass."""

    def test_404_is_removed(self) -> None:
        self.assertEqual(classify_removal(404), REMOVED)

    def test_200_is_present(self) -> None:
        self.assertEqual(classify_removal(200), PRESENT)

    def test_auth_rate_limit_and_server_errors_are_inconclusive(self) -> None:
        for status in (0, 301, 401, 403, 409, 429, 500, 502, 503):
            with self.subTest(status=status):
                self.assertEqual(classify_removal(status), INCONCLUSIVE)

    def test_unparseable_response_is_not_removed(self) -> None:
        """Status 0 is what the caller uses when nothing parsed."""
        self.assertNotEqual(classify_removal(0), REMOVED)


class BackupCopyTests(unittest.TestCase):
    """Copy, hash the destination, then delete. Never move-and-hope."""

    def setUp(self) -> None:
        import shutil
        import tempfile

        self._tmp: Path = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, str(self._tmp), True)
        self.source: Path = self._tmp / "backup.gpg"
        self.source.write_bytes(b"ciphertext")
        self.digest: str = hashlib.sha256(b"ciphertext").hexdigest()

    def test_faithful_copy_has_no_defects(self) -> None:
        destination = self._tmp / "offline.gpg"
        destination.write_bytes(b"ciphertext")
        self.assertEqual(
            verify_copy(self.source, destination, self.digest), []
        )

    def test_truncated_copy_is_caught(self) -> None:
        """The case that makes a move destroy the only good copy."""
        destination = self._tmp / "offline.gpg"
        destination.write_bytes(b"cipher")
        defects = verify_copy(self.source, destination, self.digest)
        self.assertTrue(any("does not match the recorded" in d for d in defects))
        self.assertTrue(any("Do not delete the source" in d for d in defects))

    def test_missing_destination_is_caught(self) -> None:
        defects = verify_copy(
            self.source, self._tmp / "absent.gpg", self.digest
        )
        self.assertTrue(any("does not exist" in d for d in defects))

    def test_non_sha256_expected_digest_is_refused(self) -> None:
        destination = self._tmp / "offline.gpg"
        destination.write_bytes(b"ciphertext")
        defects = verify_copy(self.source, destination, "not-a-digest")
        self.assertTrue(any("not a sha256" in d for d in defects))


class WorktreeGateTests(unittest.TestCase):
    """Retiring a clone must not silently destroy uncommitted work."""

    def test_clean_worktree_passes(self) -> None:
        self.assertTrue(worktree_is_clean(""))
        self.assertTrue(worktree_is_clean("   \n  "))

    def test_dirty_worktree_is_refused(self) -> None:
        for status in (
            " M cli/commands.py",
            "?? notes.txt",
            "A  ledger/sanitize.py",
            " D README.md",
        ):
            with self.subTest(status=status):
                self.assertFalse(worktree_is_clean(status))


if __name__ == "__main__":
    unittest.main()
