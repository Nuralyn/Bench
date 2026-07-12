"""Shared synthetic-ledger fixtures for the test suite.

Single source of truth for building correctly-linked hash chains so the
entry shape lives in one place. Named with a leading underscore so
unittest discovery (pattern ``test*.py``) never collects it as a test
module; import it as ``from _ledger_fixtures import build_valid_chain``
(the tests directory is on sys.path both under discovery and when a
test file runs as a script).
"""

import sys
from pathlib import Path
from typing import Any

_REPO_ROOT: Path = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from ledger.chain import compute_entry_hash  # noqa: E402


def build_valid_chain(
    n: int, verdicts: list[str] | None = None
) -> list[dict]:
    """Build a correctly-linked chain of n entries starting from GENESIS.

    Each entry carries an oracle verdict ("PASS" unless overridden via
    ``verdicts``); VETO entries get a single C-001 VIOLATED citation so
    stats and viewer assertions have something to count.
    """
    entries: list[dict] = []
    for i in range(n):
        verdict: str = verdicts[i] if verdicts else "PASS"
        entry: dict[str, Any] = {
            "entry_id": f"id-{i}",
            "timestamp": f"2026-01-01T00:00:{i:02d}+00:00",
            "previous_hash": (
                "GENESIS" if i == 0 else entries[i - 1]["entry_hash"]
            ),
            "constitution_hash": "abc",
            "change": {"file": f"file_{i}.py", "tool": "Write"},
            "oracle": {
                "verdict": verdict,
                "constraint_citations": (
                    [{"constraint_id": "C-001", "disposition": "VIOLATED"}]
                    if verdict == "VETO"
                    else []
                ),
            },
        }
        entry["entry_hash"] = compute_entry_hash(entry)
        entries.append(entry)
    return entries
