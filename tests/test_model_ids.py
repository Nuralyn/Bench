"""Smoke tests for the pipeline model IDs.

Two layers:
  * structural (always runs): each model constant is a non-empty, well-formed
    Claude model id, so a typo'd or empty constant fails here instead of
    silently degrading a pipeline stage to API_ERROR / PIPELINE_ERROR at
    runtime.
  * live (opt-in via BENCH_LIVE_SMOKE=1): each stage model actually resolves on
    the configured provider. Skipped by default because it makes real API calls
    (network, credentials, cost).

Run: python -m unittest tests.test_model_ids -v
"""

import os
import re
import sys
import unittest
from pathlib import Path

_REPO_ROOT: Path = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from utils.api import (  # noqa: E402
    CHALLENGER_MODEL,
    DEFENDER_MODEL,
    ORACLE_MODEL,
    UTILITY_MODEL,
    call_model,
)

# Anthropic first-party ids: "claude-" followed by hyphen-separated lowercase
# alphanumeric segments (e.g. claude-sonnet-5, claude-opus-4-8,
# claude-haiku-4-5-20251001). Deliberately loose: it catches empties,
# whitespace, and obvious typos without pinning version strings, which live in
# utils/api.py and change over time.
_MODEL_ID_RE: re.Pattern[str] = re.compile(r"^claude-[a-z0-9]+(-[a-z0-9]+)*$")

_STAGE_MODELS: dict[str, str] = {
    "CHALLENGER_MODEL": CHALLENGER_MODEL,
    "DEFENDER_MODEL": DEFENDER_MODEL,
    "ORACLE_MODEL": ORACLE_MODEL,
}
_ALL_MODELS: dict[str, str] = {**_STAGE_MODELS, "UTILITY_MODEL": UTILITY_MODEL}


class ModelIdStructureTest(unittest.TestCase):
    """Offline structural checks. Always run."""

    def test_constants_are_wellformed_ids(self) -> None:
        for name, value in _ALL_MODELS.items():
            with self.subTest(constant=name):
                self.assertIsInstance(value, str)
                self.assertTrue(value, f"{name} is empty")
                self.assertEqual(
                    value, value.strip(), f"{name} has surrounding whitespace"
                )
                self.assertRegex(
                    value, _MODEL_ID_RE, f"{name}={value!r} is not a claude-* id"
                )


@unittest.skipUnless(
    os.environ.get("BENCH_LIVE_SMOKE") == "1",
    "live model smoke test; set BENCH_LIVE_SMOKE=1 to run (real API calls)",
)
class ModelResolutionSmokeTest(unittest.TestCase):
    """Live checks: each stage model resolves on the configured provider.

    Exercises the same call_model path the pipeline uses (honoring
    BENCH_PROVIDER). An unavailable or misspelled id surfaces as API_ERROR,
    which fails here instead of silently degrading every governance run to
    PIPELINE_ERROR.
    """

    def test_stage_models_resolve(self) -> None:
        for name, model in _STAGE_MODELS.items():
            with self.subTest(constant=name, model=model):
                result = call_model(
                    model,
                    "You are a connectivity probe. Respond only with JSON.",
                    'Respond with exactly: {"ok": true}',
                )
                self.assertNotEqual(
                    result.get("error"),
                    "API_ERROR",
                    f"{name} ({model}) did not resolve: {result.get('detail')}",
                )


if __name__ == "__main__":
    unittest.main()
