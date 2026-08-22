"""Guard: the committed ledger must never carry another project's source.

Bench's hook can be registered globally, so it governs files belonging to
other projects. Between April and July 2026 those changes were written, with
full diff bodies, into Bench's own ledger, which is committed to a public
repository. The routing fix (ledger.chain.resolve_ledger_path) stops other
projects' sessions from landing here, and redaction strips the body of any
out-of-project file still governed from a Bench session. This module is the
regression test that keeps both honest.

The invariant: an out-of-project file may appear in the ledger by path and
verdict, but never with its contents.

In-project files are always recorded as repository-relative paths, because
the hook normalizes them against the repo root. So an absolute path, or one
that climbs out with '..', must carry no body.

This is deliberately stricter than the runtime redactor, which compares a
path against the real project root and so keeps the body of an absolute path
that happens to point inside the repo. Here, an absolute path is treated as a
defect either way: it is a foreign file whose body must be stripped, or it is
an in-repo file that escaped path normalization. The retired chain contained
both kinds. Both are worth failing on, so the message below names both.

Absoluteness is detected with a regex rather than os.path.isabs: CI runs on
Linux, where isabs(r"C:\\Users\\...") is False, and a Windows-shaped path in
the ledger is exactly what this test exists to catch.

Run: python -m unittest tests.test_ledger_hygiene -v
"""

import json
import re
import subprocess
import sys
import unittest
from pathlib import Path

_REPO_ROOT: Path = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from ledger.chain import _EXTERNAL_BODY_KEYS, resolve_ledger_path  # noqa: E402

# The redaction guard follows the chain rather than a fixed path: the
# operational ledger is private and project-local, so it exists on a working
# machine and not in a clean CI checkout, where these tests skip. The tracked
# file guards above are the enforced CI gate; this one keeps the runtime
# redactor honest wherever a chain actually exists.
_LEDGER: Path = Path(resolve_ledger_path())

# Drive-letter paths (C:\ or C:/), POSIX absolute paths, and UNC shares.
_ABSOLUTE: re.Pattern[str] = re.compile(r"^(?:[A-Za-z]:[\\/]|[\\/])")
# Paths that escape the project root.
_ESCAPES: re.Pattern[str] = re.compile(r"^\.\.[\\/]|^\.\.$")


def _is_out_of_project(file_ref: str) -> bool:
    """True when a ledger entry's file reference is not repo-relative."""
    if not file_ref or file_ref == "unknown":
        return False
    return bool(_ABSOLUTE.match(file_ref) or _ESCAPES.match(file_ref))


_SHA256: re.Pattern = re.compile(r"^[0-9a-f]{64}$")

_FORBIDDEN_TRACKED: tuple[str, ...] = (
    ".bench",
    "ledger/bench-ledger.json",
    "ledger/ledger-meta.json",
    "ledger/entries",
)


def _tracked_files() -> list[str]:
    """Every path git tracks in this checkout."""
    result = subprocess.run(
        ["git", "ls-files"],
        cwd=str(_REPO_ROOT),
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git ls-files failed: {result.stderr.strip()}")
    return [line for line in result.stdout.splitlines() if line]


def _has_entry_shape(obj: object) -> bool:
    """True when one parsed object is an operational ledger entry.

    The 64-hex entry_hash is what separates a real chain from a synthetic
    fixture using a short placeholder digest such as "abc123hash".
    """
    if not isinstance(obj, dict):
        return False
    change = obj.get("change")
    return bool(
        _SHA256.match(str(obj.get("entry_hash", "")))
        and "previous_hash" in obj
        and isinstance(change, dict)
        and "diff_summary" in change
    )


def _is_ledger_shaped(raw: str) -> bool:
    """True when text is serialized operational ledger data.

    Keyed on parseability plus the real entry shape, so Bench's own source,
    tests, fixtures, docs, and this detector are excluded by construction
    rather than by an allowlist: none of them parse as JSON.

    Three carriers are recognized, because a chain can be renamed into any
    of them. Under DAG storage append_entry writes one standalone object per
    entry as entries/<hash>.json, so a relocated chain is most naturally a
    directory of root objects rather than an array. Checking only arrays
    would let exactly that shape through under a path the tracked-file guard
    does not name.

    attestation.json stays immune by design: it carries `commitment` and has
    no `change` object, so it matches none of the three.
    """
    try:
        data = json.loads(raw)
    except ValueError:
        return False
    if _has_entry_shape(data):
        return True
    if isinstance(data, dict):
        data = data.get("entries")
    if not isinstance(data, list):
        return False
    return any(_has_entry_shape(obj) for obj in data)


class TrackedLedgerGuardTests(unittest.TestCase):
    """CI gate: no operational ledger may be tracked, under any name.

    An entry embeds the full diff body of the change it governs, so a
    committed chain publishes every change it ever saw. Between April and
    July 2026 that is exactly what happened here, across three unrelated
    projects. The old guard allowed a committed chain and policed its
    contents; this one removes the category. Nothing committed means nothing
    to leak, which is strictly stronger than redacting what was committed.
    """

    def test_no_operational_ledger_is_tracked(self) -> None:
        tracked: list[str] = _tracked_files()
        offenders: list[str] = [
            path
            for path in tracked
            for prefix in _FORBIDDEN_TRACKED
            if path == prefix or path.startswith(prefix + "/")
        ]
        self.assertEqual(
            offenders,
            [],
            "Operational ledger files are tracked by git: "
            f"{offenders}. The chain is private and lives in .bench/. "
            "Untrack them with 'git rm --cached'; .gitignore alone does not "
            "untrack files already committed.",
        )

    def test_no_ledger_shaped_content_is_tracked(self) -> None:
        offenders: list[str] = []
        for path in _tracked_files():
            candidate = _REPO_ROOT / path
            try:
                raw: str = candidate.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            if _is_ledger_shaped(raw):
                offenders.append(path)
        self.assertEqual(
            offenders,
            [],
            f"Ledger-shaped content is tracked under: {offenders}. A chain "
            "renamed is still a chain; it carries diff bodies and must not "
            "be committed.",
        )


class LedgerShapeDetectorTests(unittest.TestCase):
    """The detector must catch a renamed chain without flagging Bench itself."""

    def _entry(self, entry_hash: str) -> dict:
        return {
            "entry_hash": entry_hash,
            "previous_hash": "GENESIS",
            "change": {"file": "a.py", "diff_summary": {"content": "x"}},
        }

    def test_flags_real_chain_array(self) -> None:
        self.assertTrue(_is_ledger_shaped(json.dumps([self._entry("a" * 64)])))

    def test_flags_entries_wrapped_object(self) -> None:
        payload = json.dumps({"entries": [self._entry("b" * 64)]})
        self.assertTrue(_is_ledger_shaped(payload))

    def test_flags_standalone_entry_object(self) -> None:
        """A renamed chain is a directory of root objects, not an array.

        append_entry writes one standalone object per entry under
        entries/<hash>.json. Checking only arrays let exactly that shape
        through under a path the tracked-file guard does not name.
        """
        self.assertTrue(_is_ledger_shaped(json.dumps(self._entry("e" * 64))))

    def test_ignores_standalone_object_with_short_digest(self) -> None:
        self.assertFalse(_is_ledger_shaped(json.dumps(self._entry("deadbeef"))))

    def test_ignores_standalone_object_without_change(self) -> None:
        payload = json.dumps({"entry_hash": "f" * 64, "previous_hash": "GENESIS"})
        self.assertFalse(_is_ledger_shaped(payload))

    def test_ignores_synthetic_fixture_digest(self) -> None:
        """Short placeholder hashes are how tests fake a chain."""
        self.assertFalse(_is_ledger_shaped(json.dumps([self._entry("abc123hash")])))

    def test_ignores_entry_hash_without_change(self) -> None:
        payload = json.dumps([{"entry_hash": "c" * 64, "previous_hash": "GENESIS"}])
        self.assertFalse(_is_ledger_shaped(payload))

    def test_ignores_attestation_shape(self) -> None:
        payload = json.dumps(
            {
                "schema_version": "1",
                "records": [
                    {"seq": 0, "commitment": "d" * 64, "verdict": "PASS"}
                ],
            }
        )
        self.assertFalse(_is_ledger_shaped(payload))

    def test_ignores_bench_json(self) -> None:
        raw: str = (_REPO_ROOT / "bench.json").read_text(encoding="utf-8")
        self.assertFalse(_is_ledger_shaped(raw))

    def test_ignores_python_source_including_this_detector(self) -> None:
        for source in (_REPO_ROOT / "ledger" / "chain.py", Path(__file__)):
            with self.subTest(source=source.name):
                raw: str = source.read_text(encoding="utf-8")
                self.assertFalse(_is_ledger_shaped(raw))

    def test_ignores_markdown_documentation(self) -> None:
        raw: str = (_REPO_ROOT / "CLAUDE.md").read_text(encoding="utf-8")
        self.assertFalse(_is_ledger_shaped(raw))


class CommittedLedgerHygieneTests(unittest.TestCase):
    def setUp(self) -> None:
        if not _LEDGER.exists():
            self.skipTest("no ledger in this checkout")
        self.entries: list[dict] = json.loads(
            _LEDGER.read_text(encoding="utf-8")
        )

    def test_out_of_project_entries_are_redacted(self) -> None:
        """No foreign file body may sit in the ledger, in any entry."""
        offenders: list[str] = []
        for index, entry in enumerate(self.entries):
            change: dict = entry.get("change") or {}
            file_ref: str = str(change.get("file", ""))
            if not _is_out_of_project(file_ref):
                continue
            summary = change.get("diff_summary")
            if not isinstance(summary, dict) or not summary.get("redacted"):
                offenders.append(f"entry {index}: unredacted body for {file_ref}")
                continue
            leaked: list[str] = sorted(
                set(summary) & set(_EXTERNAL_BODY_KEYS)
            )
            if leaked:
                offenders.append(
                    f"entry {index}: {file_ref} retains {leaked}"
                )
        self.assertEqual(
            offenders,
            [],
            "Non-relative file reference carrying a diff body in the ledger. "
            "Either a foreign file's source is about to be published, or an "
            "in-repo path escaped normalization. Investigate before "
            "committing:\n  " + "\n  ".join(offenders[:20]),
        )

    def test_redaction_marker_implies_no_body_keys(self) -> None:
        """Anything flagged redacted must actually have been stripped."""
        for index, entry in enumerate(self.entries):
            summary = (entry.get("change") or {}).get("diff_summary")
            if not isinstance(summary, dict) or not summary.get("redacted"):
                continue
            for key in _EXTERNAL_BODY_KEYS:
                self.assertNotIn(
                    key,
                    summary,
                    f"entry {index} claims redacted but still carries {key!r}",
                )

    def test_detector_recognizes_foreign_path_shapes(self) -> None:
        """The detector itself must catch both platforms' absolute paths.

        Without this, the guard above could pass on Linux CI simply by
        failing to recognize a Windows path as absolute.
        """
        for path in (
            r"C:\Users\someone\project\src\billing.ts",
            "C:/Users/someone/project/src/billing.ts",
            "/home/someone/project/src/billing.ts",
            r"\\server\share\file.py",
            r"..\other-project\main.py",
            "../other-project/main.py",
        ):
            self.assertTrue(
                _is_out_of_project(path), f"failed to flag {path!r}"
            )

        for path in (
            "utils/api.py",
            r"utils\api.py",
            "bench.json",
            "unknown",
            "",
        ):
            self.assertFalse(
                _is_out_of_project(path), f"wrongly flagged {path!r}"
            )


if __name__ == "__main__":
    unittest.main()
