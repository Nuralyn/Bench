"""Live prompt-cache check on the anthropic provider (opt-in, real API calls).

Roadmap v2.1 item 2.2 is done when cache read tokens appear in the usage
record for the second of two consecutive calls that share a prefix. That is
a property of the real API, not of any mock, so this test makes two real
calls and reads the usage back. It is gated the same way the live model-id
check is: set BENCH_LIVE_SMOKE=1 and an ANTHROPIC_API_KEY to run it.

Run: BENCH_LIVE_SMOKE=1 python -m unittest tests.test_prompt_cache_live -v
"""

import json
import os
import sys
import unittest
import unittest.mock
from pathlib import Path

_REPO_ROOT: Path = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from pipeline.constitution import (  # noqa: E402
    build_cached_prefix,
    load_constitution_snapshot,
)
from utils.api import CHALLENGER_MODEL, call_model  # noqa: E402

_SYSTEM: str = (
    "You are a JSON echo service. Reply with exactly one JSON object of the "
    'form {"ok": true, "seen": "<the word after TOKEN:>"} and nothing else.'
)


@unittest.skipUnless(
    os.environ.get("BENCH_LIVE_SMOKE") == "1",
    "live prompt-cache test; set BENCH_LIVE_SMOKE=1 to run (real API calls)",
)
class PromptCacheLiveTests(unittest.TestCase):
    def test_second_call_reads_the_prefix_from_cache(self) -> None:
        core, _ = load_constitution_snapshot(str(_REPO_ROOT / "bench.json"))
        # Bench's real prefix: the constitution plus a repository context of
        # the size the runner sends, well over the model's cacheable minimum.
        prefix: str = build_cached_prefix(core, "# CONTEXT\n" + ("lorem ipsum " * 900))

        with unittest.mock.patch.dict("os.environ", {"BENCH_PROVIDER": "anthropic"}):
            first: dict = call_model(
                CHALLENGER_MODEL, _SYSTEM, "TOKEN: alpha", cached_prefix=prefix
            )
            second: dict = call_model(
                CHALLENGER_MODEL, _SYSTEM, "TOKEN: beta", cached_prefix=prefix
            )

        for result in (first, second):
            self.assertNotIn("error", result, json.dumps(result)[:500])

        self.assertGreater(
            first["_tokens"]["cache_creation"] + first["_tokens"]["cache_read"],
            0,
            f"first call neither wrote nor read the cache: {first['_tokens']}",
        )
        self.assertGreater(
            second["_tokens"]["cache_read"],
            0,
            f"second call read nothing from cache: {second['_tokens']}",
        )
        # "input" is the whole prompt, so it is never smaller than the
        # cached part it contains.
        self.assertGreaterEqual(
            second["_tokens"]["input"], second["_tokens"]["cache_read"]
        )


if __name__ == "__main__":
    unittest.main()
