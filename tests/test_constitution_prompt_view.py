"""Tests for the constitution's prompt view: what the models read.

Covers: prompt_view field selection at both levels, tolerance of malformed
entries, the optional `commentary` field's schema check, that all three stage
prompt builders send the view rather than the authored file, and a size
budget on Bench's own constitution as the models receive it.

Run: python -m unittest tests.test_constitution_prompt_view -v
"""

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_REPO_ROOT: Path = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from pipeline import challenger, defender, oracle  # noqa: E402
from pipeline.constitution import (  # noqa: E402
    ConstitutionSchemaError,
    build_cached_prefix,
    build_context_section,
    load_constitution_snapshot,
    prompt_view,
)

# The budget for the serialized constitution inside a stage prompt, in
# characters. Roadmap v2.1 item 2.1 sets the target at 1,500 tokens; no
# tokenizer is a declared dependency, so this uses the conventional four
# characters per token, which under-counts indented JSON if anything. Bench's
# own constitution is checked against it below, so a rule that grows past the
# budget fails here rather than silently costing every edit three times over.
_PROMPT_BUDGET_TOKENS: int = 1500
_CHARS_PER_TOKEN: int = 4
_PROMPT_BUDGET_CHARS: int = _PROMPT_BUDGET_TOKENS * _CHARS_PER_TOKEN

_RATIONALE: str = "RATIONALE-ONLY-TEXT"
_COMMENTARY: str = "COMMENTARY-ONLY-TEXT"


def _constitution() -> dict:
    return {
        "constitution": "bench-test",
        "version": 9,
        "author": "someone",
        "created": "2026-01-01T00:00:00Z",
        "description": "not for the models",
        "constraints": [
            {
                "id": "C-001",
                "name": "No Silent Errors",
                "scope": "error-handling",
                "rule": "Catch blocks must log, re-throw, or return.",
                "severity": "veto",
                "rationale": _RATIONALE,
                "commentary": _COMMENTARY,
            },
            {
                "id": "C-002",
                "name": "Scope",
                "rule": "One coherent change per edit.",
                "severity": "veto",
                "severity_raised_by_project": True,
            },
        ],
    }


class PromptViewTests(unittest.TestCase):
    def test_keeps_binding_fields_and_drops_prose(self) -> None:
        view: dict = prompt_view(_constitution())
        first: dict = view["constraints"][0]
        self.assertEqual(
            first,
            {
                "id": "C-001",
                "name": "No Silent Errors",
                "scope": "error-handling",
                "rule": "Catch blocks must log, re-throw, or return.",
                "severity": "veto",
            },
        )
        self.assertNotIn("rationale", first)
        self.assertNotIn("commentary", first)

    def test_rule_text_is_verbatim(self) -> None:
        source: dict = _constitution()
        view: dict = prompt_view(source)
        for original, rendered in zip(source["constraints"], view["constraints"]):
            self.assertEqual(rendered["rule"], original["rule"])
            self.assertEqual(rendered["severity"], original["severity"])
            self.assertEqual(rendered["id"], original["id"])

    def test_keeps_raised_severity_marker(self) -> None:
        view: dict = prompt_view(_constitution())
        self.assertTrue(view["constraints"][1]["severity_raised_by_project"])

    def test_top_level_keeps_identity_and_drops_metadata(self) -> None:
        source: dict = _constitution()
        source["project_version"] = 3
        view: dict = prompt_view(source)
        self.assertEqual(
            set(view), {"constitution", "version", "project_version", "constraints"}
        )
        self.assertEqual(view["version"], 9)

    def test_absent_optional_fields_are_not_invented(self) -> None:
        view: dict = prompt_view(_constitution())
        second: dict = view["constraints"][1]
        self.assertNotIn("scope", second)
        self.assertNotIn("project_version", view)

    def test_tolerates_non_dict_entries_and_missing_list(self) -> None:
        view: dict = prompt_view({"constraints": ["junk", {"id": "C-001"}]})
        self.assertEqual(view["constraints"], [{"id": "C-001"}])
        self.assertEqual(prompt_view({})["constraints"], [])
        self.assertEqual(prompt_view({"constraints": "no"})["constraints"], [])

    def test_does_not_mutate_the_source(self) -> None:
        source: dict = _constitution()
        before: str = json.dumps(source, sort_keys=True)
        prompt_view(source)
        self.assertEqual(json.dumps(source, sort_keys=True), before)


class CommentarySchemaTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp: str = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self._tmp)

    def _write(self, doc: dict) -> str:
        path: str = os.path.join(self._tmp, "bench.json")
        Path(path).write_text(json.dumps(doc), encoding="utf-8")
        return path

    def test_string_commentary_is_accepted(self) -> None:
        data, _ = load_constitution_snapshot(self._write(_constitution()))
        self.assertEqual(data["constraints"][0]["commentary"], _COMMENTARY)

    def test_absent_commentary_is_accepted(self) -> None:
        doc: dict = _constitution()
        del doc["constraints"][0]["commentary"]
        data, _ = load_constitution_snapshot(self._write(doc))
        self.assertNotIn("commentary", data["constraints"][0])

    def test_non_string_commentary_is_rejected(self) -> None:
        doc: dict = _constitution()
        doc["constraints"][0]["commentary"] = ["a", "list"]
        path: str = self._write(doc)
        with self.assertRaises(ConstitutionSchemaError):
            load_constitution_snapshot(path)


class StagePromptTests(unittest.TestCase):
    """Each stage sends the view, not the file, and the rule survives whole.

    Since prompt caching, the constitution travels in the cached prefix
    every stage hands to call_model, so the assertions run against what
    each stage actually passes as ``cached_prefix``.
    """

    def _diff(self) -> dict:
        return {"file_path": "x.py", "diff": "+pass", "tool_name": "Edit"}

    def _assert_view_sent(self, content: str) -> None:
        self.assertIn("CONSTITUTION:", content)
        self.assertIn("Catch blocks must log, re-throw, or return.", content)
        self.assertIn('"severity": "veto"', content)
        self.assertNotIn(_RATIONALE, content)
        self.assertNotIn(_COMMENTARY, content)
        self.assertNotIn("not for the models", content)

    def test_cached_prefix_is_the_prompt_view(self) -> None:
        self._assert_view_sent(build_cached_prefix(_constitution()))

    def test_prefix_is_the_constitution_and_context_is_separate(self) -> None:
        source: dict = _constitution()
        rendered: str = json.dumps(prompt_view(source), indent=2)
        prefix: str = build_cached_prefix(source)
        self.assertEqual(prefix, f"CONSTITUTION:\n{rendered}")
        self.assertEqual(build_context_section("CTX"), "FILE CONTEXT:\nCTX")
        self.assertEqual(build_context_section(""), "")

    def _stage_prefixes(self) -> list[str]:
        """The cached_prefix and cached_context each stage passes, joined."""
        source: dict = _constitution()
        prefixes: list[str] = []

        def capture(*_args: object, **kwargs: object) -> dict:
            prefixes.append(
                f"{kwargs.get('cached_prefix', '')}\n\n"
                f"{kwargs.get('cached_context', '')}"
            )
            return {"error": "API_ERROR", "detail": "captured", "_tokens": {}}

        with patch("pipeline.challenger.call_model", side_effect=capture):
            challenger.run_challenger(self._diff(), source, "h", "CTX")
        with patch("pipeline.defender.call_model", side_effect=capture):
            defender.run_defender(
                self._diff(), source, "h", {"status": "FINDINGS", "findings": []}, "CTX"
            )
        with patch("pipeline.oracle.call_model", side_effect=capture):
            oracle.run_oracle(
                self._diff(),
                source,
                "h",
                {"status": "FINDINGS", "findings": []},
                {"status": "REBUTTAL", "rebuttals": []},
                "CTX",
            )
        return prefixes

    def test_all_three_stages_pass_the_same_cached_prefix(self) -> None:
        prefixes: list[str] = self._stage_prefixes()
        self.assertEqual(len(prefixes), 3)
        self.assertEqual(len(set(prefixes)), 1)
        self._assert_view_sent(prefixes[0])
        self.assertIn("FILE CONTEXT:\nCTX", prefixes[0])

    def test_changed_constitution_changes_the_prefix_bytes(self) -> None:
        source: dict = _constitution()
        before: str = build_cached_prefix(source)
        source["constraints"][0]["rule"] += " Also log the traceback."
        self.assertNotEqual(build_cached_prefix(source), before)


class BenchConstitutionBudgetTests(unittest.TestCase):
    """Bench's own constitution, as the models receive it, fits the budget."""

    def test_prompt_view_of_core_constitution_is_within_budget(self) -> None:
        core, _ = load_constitution_snapshot(str(_REPO_ROOT / "bench.json"))
        rendered: str = json.dumps(prompt_view(core), indent=2)
        self.assertLessEqual(
            len(rendered),
            _PROMPT_BUDGET_CHARS,
            f"constitution prompt view is {len(rendered)} chars, over the "
            f"{_PROMPT_BUDGET_TOKENS}-token budget "
            f"({_PROMPT_BUDGET_CHARS} chars at {_CHARS_PER_TOKEN}/token); "
            "move procedure and history from `rule` into `commentary`",
        )

    def test_core_constitution_rules_reach_the_models_whole(self) -> None:
        core, _ = load_constitution_snapshot(str(_REPO_ROOT / "bench.json"))
        view: dict = prompt_view(core)
        self.assertEqual(len(view["constraints"]), len(core["constraints"]))
        for original, rendered in zip(core["constraints"], view["constraints"]):
            self.assertEqual(rendered["id"], original["id"])
            self.assertEqual(rendered["rule"], original["rule"])
            self.assertEqual(rendered["severity"], original["severity"])


if __name__ == "__main__":
    unittest.main()
