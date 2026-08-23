"""Tests for the public attestation export.

The artifact is published; the chain it derives from is not. So the tests
that matter most are the ones proving content cannot cross that boundary.
Entry data is model-authored, and this chain genuinely contains strings like
"process (CLAUDE.md Rule 15, not a numbered C-XXX constraint)" sitting in a
field named constraint_id. Every string is therefore treated as a possible
exfiltration channel and must match its pattern to be emitted.

Run: python -m unittest tests.test_attestation -v
"""

import json
import sys
import unittest
from pathlib import Path

_REPO_ROOT: Path = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from ledger.attestation import (  # noqa: E402
    SCHEMA_VERSION,
    AttestationError,
    build_attestation,
    render,
    validate_document,
)

_H1: str = "1" * 64
_H2: str = "2" * 64
_H3: str = "3" * 64
_CONST: str = "c" * 64
_VERSION: str = "2.0.0"


def _entry(
    entry_hash: str,
    previous: object,
    verdict: str = "PASS",
    **extra: object,
) -> dict:
    """An entry shaped like the real thing, including the parts we exclude."""
    base: dict = {
        "entry_id": "9d1f-uuid-that-must-not-be-emitted",
        "timestamp": "2026-08-22T21:07:18.425488+00:00",
        "previous_hash": previous,
        "constitution_hash": _CONST,
        "verdict": verdict,
        "pipeline_error": False,
        "change": {
            "file": "C:\\Users\\someone\\secret\\app.py",
            "tool": "Write",
            "diff_summary": {"content": "SECRET SOURCE CODE"},
        },
        "challenger": {"status": "CLEAR", "findings": [], "_tokens": {"input": 9}},
        "defender": {"status": "CONFIRM_CLEAR", "summary": "no issues"},
        "oracle": {
            "verdict": verdict,
            "reasoning": "long prose about the change",
            "advisories": ["an advisory"],
            "raw_response": "RAW MODEL OUTPUT",
            "constraint_citations": [{"constraint_id": "C-001"}],
            "_tokens": {"input": 100, "output": 200},
        },
        "constitution_sources": [
            {"layer": "core", "path": "C:\\Users\\mstar\\Bench\\bench.json"}
        ],
        "entry_hash": entry_hash,
    }
    base.update(extra)
    return base


def _chain() -> list[dict]:
    return [
        _entry(_H1, "GENESIS"),
        _entry(_H2, [_H1]),
        _entry(_H3, [_H2]),
    ]


class SchemaTests(unittest.TestCase):
    def test_emits_only_schema_fields(self) -> None:
        doc = build_attestation(_chain(), _H3, _VERSION)
        self.assertEqual(
            set(doc),
            {
                "schema_version",
                "bench_version",
                "cutoff_commitment",
                "cutoff_timestamp",
                "record_count",
                "records",
            },
        )
        self.assertEqual(
            set(doc["records"][0]),
            {
                "seq",
                "timestamp",
                "verdict",
                "pipeline_error",
                "commitment",
                "previous_commitment",
                "constitution_commitment",
                "constraint_ids",
                "unmapped_citation_count",
            },
        )
        self.assertEqual(doc["schema_version"], SCHEMA_VERSION)

    def test_built_document_validates(self) -> None:
        self.assertEqual(
            validate_document(build_attestation(_chain(), _H3, _VERSION)), []
        )

    def test_timestamp_is_whole_second_utc(self) -> None:
        doc = build_attestation(_chain(), _H3, _VERSION)
        self.assertEqual(doc["records"][0]["timestamp"], "2026-08-22T21:07:18Z")


class ContentCannotCrossTests(unittest.TestCase):
    """The boundary the artifact exists to hold."""

    def _serialized(self) -> str:
        return render(build_attestation(_chain(), _H3, _VERSION))

    def test_no_diff_body_survives(self) -> None:
        self.assertNotIn("SECRET SOURCE CODE", self._serialized())

    def test_no_file_path_survives(self) -> None:
        text = self._serialized()
        self.assertNotIn("app.py", text)
        self.assertNotIn("Users", text)

    def test_no_stage_prose_survives(self) -> None:
        text = self._serialized()
        for fragment in (
            "long prose about the change",
            "an advisory",
            "RAW MODEL OUTPUT",
            "no issues",
        ):
            self.assertNotIn(fragment, text)

    def test_no_token_counts_or_entry_id_survive(self) -> None:
        text = self._serialized()
        self.assertNotIn("_tokens", text)
        self.assertNotIn("uuid-that-must-not-be-emitted", text)

    def test_no_constitution_source_paths_survive(self) -> None:
        self.assertNotIn("bench.json", self._serialized())

    def test_no_drive_letter_or_separator_survives(self) -> None:
        """A blunt sweep, since any of them would mean a leak."""
        text = self._serialized()
        self.assertNotIn("C:\\", text)
        self.assertNotIn("\\\\", text)


class FreeTextRejectionTests(unittest.TestCase):
    """Model-authored strings must not become published values."""

    def _with_citations(self, citations: list) -> list[dict]:
        chain = _chain()
        chain[0]["oracle"]["constraint_citations"] = citations
        return chain

    def test_real_malformed_ids_are_excluded_and_counted(self) -> None:
        """These three strings are in the actual chain."""
        chain = self._with_citations(
            [
                {"constraint_id": "C-002"},
                {"constraint_id": "N/A"},
                {"constraint_id": "N/A (process/metadata)"},
                {
                    "constraint_id": (
                        "process (CLAUDE.md Rule 15, not a numbered "
                        "C-XXX constraint)"
                    )
                },
            ]
        )
        doc = build_attestation(chain, _H3, _VERSION)
        record = doc["records"][0]
        self.assertEqual(record["constraint_ids"], ["C-002"])
        self.assertEqual(record["unmapped_citation_count"], 3)
        self.assertNotIn("N/A", render(doc))
        self.assertNotIn("CLAUDE.md", render(doc))

    def test_oversized_string_cannot_survive(self) -> None:
        chain = self._with_citations([{"constraint_id": "X" * 10000}])
        doc = build_attestation(chain, _H3, _VERSION)
        self.assertEqual(doc["records"][0]["constraint_ids"], [])
        self.assertEqual(doc["records"][0]["unmapped_citation_count"], 1)
        self.assertNotIn("XXXX", render(doc))

    def test_path_shaped_citation_cannot_survive(self) -> None:
        chain = self._with_citations([{"constraint_id": "C:/secrets/key.pem"}])
        doc = build_attestation(chain, _H3, _VERSION)
        self.assertEqual(doc["records"][0]["constraint_ids"], [])
        self.assertNotIn("secrets", render(doc))

    def test_challenger_findings_are_never_a_source(self) -> None:
        """The field where the malformed ids actually live."""
        chain = _chain()
        chain[0]["challenger"]["findings"] = [
            {"constraint_id": "C-999-FROM-CHALLENGER"}
        ]
        chain[0]["oracle"]["constraint_citations"] = []
        doc = build_attestation(chain, _H3, _VERSION)
        self.assertEqual(doc["records"][0]["constraint_ids"], [])
        self.assertNotIn("FROM-CHALLENGER", render(doc))


class StructuralFailureTests(unittest.TestCase):
    """A partial attestation is worse than none."""

    def test_bad_commitment_aborts(self) -> None:
        chain = _chain()
        chain[1]["entry_hash"] = "not-a-hash"
        with self.assertRaises(AttestationError):
            build_attestation(chain, _H3, _VERSION)

    def test_unknown_verdict_aborts(self) -> None:
        chain = _chain()
        chain[1]["verdict"] = "MAYBE"
        with self.assertRaises(AttestationError):
            build_attestation(chain, _H3, _VERSION)

    def test_unusable_timestamp_aborts(self) -> None:
        chain = _chain()
        chain[1]["timestamp"] = "yesterday"
        with self.assertRaises(AttestationError):
            build_attestation(chain, _H3, _VERSION)

    def test_bad_version_aborts(self) -> None:
        with self.assertRaises(AttestationError):
            build_attestation(_chain(), _H3, "v2")

    def test_unknown_cutoff_aborts(self) -> None:
        """A checkpoint must declare a boundary that exists."""
        with self.assertRaises(AttestationError):
            build_attestation(_chain(), "f" * 64, _VERSION)


class CheckpointTests(unittest.TestCase):
    def test_cutoff_bounds_the_document(self) -> None:
        doc = build_attestation(_chain(), _H2, _VERSION)
        self.assertEqual(doc["record_count"], 2)
        self.assertEqual(doc["cutoff_commitment"], _H2)
        self.assertNotIn(_H3, render(doc))

    def test_entries_after_the_cutoff_are_absent(self) -> None:
        """Committing the artifact appends an entry; it is the next
        checkpoint's problem, not this document's."""
        doc = build_attestation(_chain(), _H1, _VERSION)
        self.assertEqual([r["commitment"] for r in doc["records"]], [_H1])

    def test_seq_is_monotonic_from_zero(self) -> None:
        doc = build_attestation(_chain(), _H3, _VERSION)
        self.assertEqual([r["seq"] for r in doc["records"]], [0, 1, 2])


class ParentNormalizationTests(unittest.TestCase):
    """Storage has four shapes; the export has one."""

    def _first(self, previous: object) -> list[str]:
        chain = [_entry(_H1, previous)]
        return build_attestation(chain, _H1, _VERSION)["records"][0][
            "previous_commitment"
        ]

    def test_genesis_becomes_empty_array(self) -> None:
        self.assertEqual(self._first("GENESIS"), [])

    def test_bare_string_becomes_one_element_array(self) -> None:
        self.assertEqual(self._first(_H2), [_H2])

    def test_single_element_list_is_preserved(self) -> None:
        self.assertEqual(self._first([_H2]), [_H2])

    def test_two_parents_are_preserved(self) -> None:
        """One real entry has two, from a git-merge fork reconciliation."""
        self.assertEqual(sorted(self._first([_H3, _H2])), sorted([_H2, _H3]))

    def test_unusable_parent_aborts(self) -> None:
        with self.assertRaises(AttestationError):
            self._first(["nope"])


class RealEntryShapeTests(unittest.TestCase):
    def test_anchor_with_empty_oracle_exports(self) -> None:
        """The ANCHOR entry's oracle is {} in the real chain."""
        chain = [_entry(_H1, "GENESIS", verdict="ANCHOR", oracle={})]
        doc = build_attestation(chain, _H1, _VERSION)
        self.assertEqual(doc["records"][0]["constraint_ids"], [])
        self.assertEqual(doc["records"][0]["verdict"], "ANCHOR")

    def test_pipeline_error_entry_exports(self) -> None:
        chain = [_entry(_H1, "GENESIS", verdict="VETO", pipeline_error=True)]
        doc = build_attestation(chain, _H1, _VERSION)
        self.assertTrue(doc["records"][0]["pipeline_error"])


class DeterminismTests(unittest.TestCase):
    def test_two_runs_are_byte_identical(self) -> None:
        first = render(build_attestation(_chain(), _H3, _VERSION))
        second = render(build_attestation(_chain(), _H3, _VERSION))
        self.assertEqual(first, second)

    def test_output_is_parseable_json(self) -> None:
        json.loads(render(build_attestation(_chain(), _H3, _VERSION)))


class DocumentValidationTests(unittest.TestCase):
    def test_extra_field_is_a_defect(self) -> None:
        doc = build_attestation(_chain(), _H3, _VERSION)
        doc["leaked"] = "some content"
        self.assertTrue(
            any("unexpected header" in d for d in validate_document(doc))
        )

    def test_extra_record_field_is_a_defect(self) -> None:
        doc = build_attestation(_chain(), _H3, _VERSION)
        doc["records"][0]["diff"] = "leaked body"
        self.assertTrue(
            any("unexpected fields" in d for d in validate_document(doc))
        )

    def test_count_mismatch_is_a_defect(self) -> None:
        doc = build_attestation(_chain(), _H3, _VERSION)
        doc["record_count"] = 99
        self.assertTrue(
            any("record_count" in d for d in validate_document(doc))
        )


if __name__ == "__main__":
    unittest.main()
