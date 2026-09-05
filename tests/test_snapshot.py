"""Tests for pipeline.snapshot: the copy that survives a stage's repairs.

Covers the copy's shape (top level and the named lists' dicts copied,
everything else shared), the edge cases (missing list, non-dict items),
that a deeply nested unknown field neither fails the copy nor escapes the
ledger's serializer at the depths the ledger can record, and the contract
the snapshot depends on: each stage's normalizer, run on every repairable
shape the ledger has recorded, leaves the snapshot byte-identical to what
the model wrote.

Run: python -m unittest tests.test_snapshot -v
"""

import json
import sys
import unittest
from pathlib import Path

_REPO_ROOT: Path = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from pipeline.challenger import _normalize_challenger_response  # noqa: E402
from pipeline.defender import _normalize_defender_response  # noqa: E402
from pipeline.oracle import _normalize_oracle_response  # noqa: E402
from pipeline.snapshot import snapshot_response  # noqa: E402


def _nested(depth: int) -> dict:
    nested: dict = {"leaf": True}
    for _ in range(depth):
        nested = {"n": nested}
    return nested


class SnapshotShapeTests(unittest.TestCase):
    def test_top_level_and_named_list_dicts_are_copied(self) -> None:
        response: dict = {
            "status": "FINDINGS",
            "findings": [{"severity": "WARNING", "evidence": ["a", "b"]}],
        }
        snap: dict = snapshot_response(response, "findings")
        self.assertIsNot(snap, response)
        self.assertIsNot(snap["findings"], response["findings"])
        self.assertIsNot(snap["findings"][0], response["findings"][0])
        # Below the copied dict, values are shared, not copied.
        self.assertIs(snap["findings"][0]["evidence"], response["findings"][0]["evidence"])

    def test_edits_to_the_response_do_not_reach_the_snapshot(self) -> None:
        response: dict = {"status": "FINDINGS", "findings": [{"severity": "WARNING"}]}
        snap: dict = snapshot_response(response, "findings")
        response["findings"][0]["severity"] = "CONCERN"
        response["_normalized"] = ["note"]
        response["findings"].append({"severity": "OBSERVATION"})
        self.assertEqual(snap, {"status": "FINDINGS", "findings": [{"severity": "WARNING"}]})

    def test_unnamed_values_are_shared_by_reference(self) -> None:
        extra: dict = _nested(3)
        response: dict = {"status": "CLEAR", "extra": extra}
        snap: dict = snapshot_response(response, "findings")
        self.assertIs(snap["extra"], extra)

    def test_missing_list_and_non_dict_items_pass_through(self) -> None:
        response: dict = {"status": "REBUTTAL", "rebuttals": ["text", 3, None, {"position": "REBUT"}]}
        snap: dict = snapshot_response(response, "rebuttals", "absent")
        self.assertEqual(snap["rebuttals"][:3], ["text", 3, None])
        self.assertIsNot(snap["rebuttals"][3], response["rebuttals"][3])
        self.assertNotIn("absent", snap)

    def test_list_field_that_is_not_a_list_is_shared(self) -> None:
        response: dict = {"status": "FINDINGS", "findings": "none"}
        self.assertEqual(snapshot_response(response, "findings"), response)


class SnapshotDepthTests(unittest.TestCase):
    def test_deep_unknown_field_never_fails_the_copy(self) -> None:
        response: dict = {"status": "CLEAR", "findings": [], "extra": _nested(5000)}
        snap: dict = snapshot_response(response, "findings")
        self.assertIs(snap["extra"], response["extra"])

    def test_a_field_the_ledger_can_record_survives_to_serialization(self) -> None:
        # A deep copy failed a few hundred levels down; json goes past
        # that. What the ledger can serialize, the snapshot must not lose.
        response: dict = {"status": "CLEAR", "findings": [], "extra": _nested(600)}
        snap: dict = snapshot_response(response, "findings")
        self.assertEqual(json.dumps(snap), json.dumps(response))


class SnapshotMatchesNormalizerContractTests(unittest.TestCase):
    """Every repair a normalizer makes lands inside what the snapshot copied.

    Each case is a repairable shape the ledger has recorded. If a future
    repair edited a structure the snapshot shares by reference, the
    snapshot would change under it and this test would fail.
    """

    def _assert_isolated(self, response: dict, snap: dict, before: str) -> None:
        self.assertEqual(json.dumps(snap, sort_keys=True), before)
        self.assertNotEqual(json.dumps(response, sort_keys=True), before)

    def test_challenger_repairs_stay_inside_the_snapshot(self) -> None:
        response: dict = {
            "status": "FINDINGS",
            "findings": [
                {
                    "constraint_id": "C-005",
                    "severity": "WARNING",
                    "location": "x",
                    "evidence": "y",
                    "reasoning": "z",
                }
            ],
        }
        before: str = json.dumps(response, sort_keys=True)
        snap: dict = snapshot_response(response, "findings")
        self.assertTrue(_normalize_challenger_response(response))
        self._assert_isolated(response, snap, before)

    def test_defender_repairs_stay_inside_the_snapshot(self) -> None:
        response: dict = {
            "status": "REBUTTAL",
            "summary": "Sound.",
            "rebuttals": [{"finding_index": "0", "position": "CONFIRM", "argument": "a"}],
        }
        challenger: dict = {"status": "FINDINGS", "findings": [{"constraint_id": "C-001"}]}
        before: str = json.dumps(response, sort_keys=True)
        snap: dict = snapshot_response(response, "rebuttals")
        self.assertTrue(_normalize_defender_response(response, challenger))
        self._assert_isolated(response, snap, before)

    def test_oracle_repairs_stay_inside_the_snapshot(self) -> None:
        response: dict = {
            "verdict": "PASS",
            "reasoning": "fine",
            "constraint_citations": [],
            "advisories": ["", "keep"],
            "remediation": "null",
        }
        before: str = json.dumps(response, sort_keys=True)
        snap: dict = snapshot_response(response)
        self.assertTrue(_normalize_oracle_response(response))
        self._assert_isolated(response, snap, before)


if __name__ == "__main__":
    unittest.main()
