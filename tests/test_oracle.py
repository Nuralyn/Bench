"""Tests for pipeline.oracle — response validation, content building, run_oracle.

All model calls are mocked. Covers: _validate_oracle_response including the
critical VETO-requires-remediation and PASS-requires-null-remediation
invariants, citation/advisory schema, confidence enum.

Run: python -m unittest tests.test_oracle -v
"""

import sys
import unittest
from unittest.mock import MagicMock, patch

from pathlib import Path

_REPO_ROOT: Path = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from pipeline.constitution import (  # noqa: E402
    build_cached_prefix,
    build_context_section,
)
from pipeline.oracle import (  # noqa: E402
    _build_user_content,
    _normalize_oracle_response,
    _validate_oracle_response,
    run_oracle,
)


def _valid_pass() -> dict:
    return {
        "verdict": "PASS",
        "reasoning": "Change satisfies all constraints.",
        "confidence": "HIGH",
        "constraint_citations": [
            {
                "constraint_id": "C-001",
                "disposition": "SATISFIED",
                "note": "Error handling present.",
            }
        ],
        "advisories": [],
        "remediation": None,
    }


def _valid_veto() -> dict:
    return {
        "verdict": "VETO",
        "reasoning": "Silent error swallowing detected.",
        "confidence": "HIGH",
        "constraint_citations": [
            {
                "constraint_id": "C-001",
                "disposition": "VIOLATED",
                "note": "Empty except block.",
            }
        ],
        "advisories": [],
        "remediation": "Add logging or re-raise in the except block.",
    }


def _valid_diff() -> dict:
    return {"file_path": "test.py", "change_type": "edit"}


def _valid_constitution() -> dict:
    return {
        "constraints": [
            {"id": "C-001", "name": "No Silent Errors", "rule": "...", "severity": "veto"}
        ]
    }


def _valid_challenger() -> dict:
    return {"status": "FINDINGS"}


def _valid_defender() -> dict:
    return {"status": "REBUTTAL"}


class ValidateOracleResponseTests(unittest.TestCase):
    def test_valid_pass_response(self) -> None:
        self.assertTrue(_validate_oracle_response(_valid_pass()))

    def test_valid_veto_response(self) -> None:
        self.assertTrue(_validate_oracle_response(_valid_veto()))

    def test_invalid_verdict_rejected(self) -> None:
        resp: dict = _valid_pass()
        resp["verdict"] = "ALLOW"
        self.assertFalse(_validate_oracle_response(resp))

    def test_missing_verdict_rejected(self) -> None:
        resp: dict = _valid_pass()
        del resp["verdict"]
        self.assertFalse(_validate_oracle_response(resp))

    def test_missing_reasoning_rejected(self) -> None:
        resp: dict = _valid_pass()
        del resp["reasoning"]
        self.assertFalse(_validate_oracle_response(resp))

    def test_empty_reasoning_rejected(self) -> None:
        resp: dict = _valid_pass()
        resp["reasoning"] = ""
        self.assertFalse(_validate_oracle_response(resp))

    def test_invalid_confidence_rejected(self) -> None:
        resp: dict = _valid_pass()
        resp["confidence"] = "VERY_HIGH"
        self.assertFalse(_validate_oracle_response(resp))

    def test_citations_not_list_rejected(self) -> None:
        resp: dict = _valid_pass()
        resp["constraint_citations"] = "string"
        self.assertFalse(_validate_oracle_response(resp))

    def test_citation_missing_field_rejected(self) -> None:
        resp: dict = _valid_pass()
        del resp["constraint_citations"][0]["constraint_id"]
        self.assertFalse(_validate_oracle_response(resp))

    def test_citation_invalid_disposition_rejected(self) -> None:
        resp: dict = _valid_pass()
        resp["constraint_citations"][0]["disposition"] = "MAYBE"
        self.assertFalse(_validate_oracle_response(resp))

    def test_advisories_not_list_rejected(self) -> None:
        resp: dict = _valid_pass()
        resp["advisories"] = "string"
        self.assertFalse(_validate_oracle_response(resp))

    def test_advisory_empty_string_rejected(self) -> None:
        resp: dict = _valid_pass()
        resp["advisories"] = [""]
        self.assertFalse(_validate_oracle_response(resp))

    def test_veto_without_remediation_rejected(self) -> None:
        resp: dict = _valid_veto()
        resp["remediation"] = None
        self.assertFalse(_validate_oracle_response(resp))

    def test_veto_with_empty_remediation_rejected(self) -> None:
        resp: dict = _valid_veto()
        resp["remediation"] = ""
        self.assertFalse(_validate_oracle_response(resp))

    def test_pass_with_non_null_remediation_rejected(self) -> None:
        resp: dict = _valid_pass()
        resp["remediation"] = "some text"
        self.assertFalse(_validate_oracle_response(resp))

    def test_missing_remediation_key_rejected(self) -> None:
        resp: dict = _valid_pass()
        del resp["remediation"]
        self.assertFalse(_validate_oracle_response(resp))


class NormalizeOracleResponseTests(unittest.TestCase):
    """One test per repair, and the fail-closed line it never crosses.

    A missing confidence is the shape the operational ledger recorded as
    INVALID_ORACLE_RESPONSE, four times on 2026-08-22 and 2026-08-23, each a
    fail-closed VETO on a change the Oracle had in fact ruled on. The
    remediation and advisory cases are the drift the schema invites.
    """

    def test_missing_confidence_is_recorded_as_low_with_a_note(self) -> None:
        for resp in (_valid_pass(), _valid_veto()):
            del resp["confidence"]
            notes: list[str] = _normalize_oracle_response(resp)
            self.assertEqual(resp["confidence"], "LOW")
            self.assertEqual(len(notes), 1)
            self.assertTrue(_validate_oracle_response(resp), resp["verdict"])

    def test_null_confidence_is_recorded_as_low(self) -> None:
        resp: dict = _valid_pass()
        resp["confidence"] = None
        _normalize_oracle_response(resp)
        self.assertEqual(resp["confidence"], "LOW")
        self.assertTrue(_validate_oracle_response(resp))

    def test_placeholder_remediation_on_pass_becomes_null(self) -> None:
        for placeholder in ("", "  ", "null", "None", "N/A", "not applicable"):
            resp: dict = _valid_pass()
            resp["remediation"] = placeholder
            notes: list[str] = _normalize_oracle_response(resp)
            self.assertIsNone(resp["remediation"], repr(placeholder))
            self.assertEqual(len(notes), 1, repr(placeholder))
            self.assertTrue(_validate_oracle_response(resp), repr(placeholder))

    def test_blank_advisory_strings_are_dropped(self) -> None:
        resp: dict = _valid_pass()
        resp["advisories"] = ["", "Keep an eye on the retry path.", "   "]
        notes: list[str] = _normalize_oracle_response(resp)
        self.assertEqual(resp["advisories"], ["Keep an eye on the retry path."])
        self.assertEqual(len(notes), 1)
        self.assertIn("2", notes[0])
        self.assertTrue(_validate_oracle_response(resp))

    def test_missing_advisories_becomes_empty_list(self) -> None:
        resp: dict = _valid_pass()
        del resp["advisories"]
        _normalize_oracle_response(resp)
        self.assertEqual(resp["advisories"], [])
        self.assertTrue(_validate_oracle_response(resp))

    def test_clean_response_is_untouched_and_unnoted(self) -> None:
        for resp in (_valid_pass(), _valid_veto()):
            expected: dict = _valid_pass() if resp["verdict"] == "PASS" else _valid_veto()
            self.assertEqual(_normalize_oracle_response(resp), [])
            self.assertEqual(resp, expected)

    def test_veto_remediation_is_never_touched(self) -> None:
        # A VETO must say what would pass. The normalizer only nulls a
        # placeholder on a PASS; on a VETO it leaves the field exactly as
        # the Oracle wrote it, and an empty one still fails closed.
        for placeholder in ("", "null", "N/A"):
            resp: dict = _valid_veto()
            resp["remediation"] = placeholder
            self.assertEqual(_normalize_oracle_response(resp), [], repr(placeholder))
            self.assertEqual(resp["remediation"], placeholder)
        resp = _valid_veto()
        resp["remediation"] = ""
        self.assertFalse(_validate_oracle_response(resp))

    def test_pass_with_real_remediation_text_still_fails_closed(self) -> None:
        resp: dict = _valid_pass()
        resp["remediation"] = "Rename the helper."
        self.assertEqual(_normalize_oracle_response(resp), [])
        self.assertFalse(_validate_oracle_response(resp))

    def test_verdict_reasoning_and_citations_still_fail_closed(self) -> None:
        broken: list[dict] = []
        resp: dict = _valid_pass()
        resp["verdict"] = "ALLOW"
        broken.append(resp)
        resp = _valid_pass()
        del resp["verdict"]
        broken.append(resp)
        resp = _valid_pass()
        resp["reasoning"] = ""
        broken.append(resp)
        resp = _valid_veto()
        resp["constraint_citations"] = []
        broken.append(resp)
        resp = _valid_veto()
        resp["constraint_citations"][0]["disposition"] = "BREACHED"
        broken.append(resp)
        resp = _valid_veto()
        del resp["constraint_citations"][0]["note"]
        broken.append(resp)
        resp = _valid_pass()
        resp["confidence"] = "VERY_HIGH"
        broken.append(resp)
        for candidate in broken:
            _normalize_oracle_response(candidate)
            self.assertFalse(_validate_oracle_response(candidate), candidate)

    def test_non_string_advisory_entries_are_not_dropped(self) -> None:
        # Dropping a non-string entry would hide a malformed advisory; the
        # validator still rejects it.
        resp: dict = _valid_pass()
        resp["advisories"] = [{"text": "structured"}]
        _normalize_oracle_response(resp)
        self.assertEqual(resp["advisories"], [{"text": "structured"}])
        self.assertFalse(_validate_oracle_response(resp))

    @patch("pipeline.oracle.call_model")
    def test_run_oracle_records_repairs_on_the_result(
        self, mock_call: MagicMock
    ) -> None:
        resp: dict = _valid_veto()
        del resp["confidence"]
        resp["_tokens"] = {"input": 10, "output": 20}
        mock_call.return_value = resp
        result: dict = run_oracle(
            _valid_diff(), _valid_constitution(), "hash",
            _valid_challenger(), _valid_defender(),
        )
        self.assertEqual(result["verdict"], "VETO")
        self.assertEqual(result["confidence"], "LOW")
        self.assertEqual(len(result["_normalized"]), 1)

    @patch("pipeline.oracle.call_model")
    def test_run_oracle_leaves_no_note_when_nothing_repaired(
        self, mock_call: MagicMock
    ) -> None:
        resp: dict = _valid_pass()
        resp["_tokens"] = {"input": 10, "output": 20}
        mock_call.return_value = resp
        result: dict = run_oracle(
            _valid_diff(), _valid_constitution(), "hash",
            _valid_challenger(), _valid_defender(),
        )
        self.assertNotIn("_normalized", result)

    @patch("pipeline.oracle.call_model")
    def test_raw_response_on_failure_is_the_model_output_not_the_repair(
        self, mock_call: MagicMock
    ) -> None:
        resp: dict = _valid_pass()
        del resp["confidence"]
        resp["constraint_citations"][0]["disposition"] = "BREACHED"
        resp["_tokens"] = {"input": 10, "output": 20}
        mock_call.return_value = resp
        result: dict = run_oracle(
            _valid_diff(), _valid_constitution(), "hash",
            _valid_challenger(), _valid_defender(),
        )
        self.assertEqual(result["status"], "PIPELINE_ERROR")
        raw: dict = result["raw_response"]
        self.assertNotIn("confidence", raw)
        self.assertNotIn("_normalized", raw)

    @patch("pipeline.oracle.call_model")
    def test_deeply_nested_unknown_field_is_tolerated(
        self, mock_call: MagicMock
    ) -> None:
        # The validator ignores unknown fields, so a valid PASS with one
        # nested far beyond what a deep copy could walk must still be a PASS.
        nested: dict = {}
        for _ in range(5000):
            nested = {"n": nested}
        resp: dict = _valid_pass()
        resp["extra"] = nested
        resp["_tokens"] = {"input": 10, "output": 20}
        mock_call.return_value = resp
        result: dict = run_oracle(
            _valid_diff(), _valid_constitution(), "hash",
            _valid_challenger(), _valid_defender(),
        )
        self.assertEqual(result["verdict"], "PASS")
        self.assertIs(result["extra"], nested)

    @patch("pipeline.oracle.call_model")
    def test_model_authored_normalized_key_does_not_survive(
        self, mock_call: MagicMock
    ) -> None:
        resp: dict = _valid_pass()
        resp["_normalized"] = ["fake"]
        resp["_tokens"] = {"input": 10, "output": 20}
        mock_call.return_value = resp
        result: dict = run_oracle(
            _valid_diff(), _valid_constitution(), "hash",
            _valid_challenger(), _valid_defender(),
        )
        self.assertEqual(result["verdict"], "PASS")
        self.assertNotIn("_normalized", result)

    @patch("pipeline.oracle.call_model")
    def test_run_oracle_still_fails_closed_on_unknown_verdict(
        self, mock_call: MagicMock
    ) -> None:
        resp: dict = _valid_pass()
        resp["verdict"] = "ALLOW"
        del resp["confidence"]
        resp["_tokens"] = {"input": 10, "output": 20}
        mock_call.return_value = resp
        result: dict = run_oracle(
            _valid_diff(), _valid_constitution(), "hash",
            _valid_challenger(), _valid_defender(),
        )
        self.assertEqual(result["status"], "PIPELINE_ERROR")
        self.assertEqual(result["error"], "INVALID_ORACLE_RESPONSE")


class BuildUserContentTests(unittest.TestCase):
    """The per-edit body carries the change and both arguments; the prefix the rest."""

    def test_body_contains_change_findings_rebuttals_and_nothing_cached(
        self,
    ) -> None:
        content: str = _build_user_content(
            _valid_diff(), _valid_challenger(), _valid_defender()
        )
        self.assertIn("PROPOSED CHANGE:", content)
        self.assertIn("CHALLENGER FINDINGS:", content)
        self.assertIn("DEFENDER REBUTTALS:", content)
        self.assertNotIn("CONSTITUTION:", content)
        self.assertNotIn("FILE CONTEXT:", content)

    def test_prefix_carries_the_constitution_only(self) -> None:
        prefix: str = build_cached_prefix(_valid_constitution())
        self.assertIn("CONSTITUTION:", prefix)
        self.assertNotIn("FILE CONTEXT:", prefix)

    def test_context_section_carries_file_context(self) -> None:
        section: str = build_context_section("source code here")
        self.assertIn("FILE CONTEXT:", section)
        self.assertIn("source code here", section)
        self.assertEqual(build_context_section(""), "")


class RunOracleTests(unittest.TestCase):
    @patch("pipeline.oracle.call_model")
    def test_valid_pass_response_passed_through(
        self, mock_call: MagicMock
    ) -> None:
        resp: dict = _valid_pass()
        resp["_tokens"] = {"input": 10, "output": 20}
        mock_call.return_value = resp
        result: dict = run_oracle(
            _valid_diff(), _valid_constitution(), "hash",
            _valid_challenger(), _valid_defender(),
        )
        self.assertEqual(result["verdict"], "PASS")

    @patch("pipeline.oracle.call_model")
    def test_valid_veto_response_passed_through(
        self, mock_call: MagicMock
    ) -> None:
        resp: dict = _valid_veto()
        resp["_tokens"] = {"input": 10, "output": 20}
        mock_call.return_value = resp
        result: dict = run_oracle(
            _valid_diff(), _valid_constitution(), "hash",
            _valid_challenger(), _valid_defender(),
        )
        self.assertEqual(result["verdict"], "VETO")
        self.assertIsNotNone(result["remediation"])

    @patch("pipeline.oracle.call_model")
    def test_api_error_returns_pipeline_error(
        self, mock_call: MagicMock
    ) -> None:
        mock_call.return_value = {
            "error": "API_ERROR",
            "_tokens": {"input": 0, "output": 0},
        }
        result: dict = run_oracle(
            _valid_diff(), _valid_constitution(), "hash",
            _valid_challenger(), _valid_defender(),
        )
        self.assertEqual(result["status"], "PIPELINE_ERROR")

    @patch("pipeline.oracle.call_model")
    def test_invalid_response_returns_pipeline_error(
        self, mock_call: MagicMock
    ) -> None:
        mock_call.return_value = {
            "garbage": True,
            "_tokens": {"input": 10, "output": 20},
        }
        result: dict = run_oracle(
            _valid_diff(), _valid_constitution(), "hash",
            _valid_challenger(), _valid_defender(),
        )
        self.assertEqual(result["status"], "PIPELINE_ERROR")
        self.assertIn("raw_response", result)

    def test_input_validation_failure_returns_pipeline_error(self) -> None:
        result: dict = run_oracle(
            _valid_diff(), _valid_constitution(), "hash",
            _valid_challenger(), {},
        )
        self.assertEqual(result["status"], "PIPELINE_ERROR")
        self.assertIn("INVALID_ORACLE_INPUT", result["error"])

    @patch("pipeline.oracle.call_model")
    def test_tokens_preserved_on_all_paths(self, mock_call: MagicMock) -> None:
        resp: dict = _valid_pass()
        resp["_tokens"] = {"input": 5, "output": 15}
        mock_call.return_value = resp
        result: dict = run_oracle(
            _valid_diff(), _valid_constitution(), "hash",
            _valid_challenger(), _valid_defender(),
        )
        self.assertIn("_tokens", result)


if __name__ == "__main__":
    unittest.main()
