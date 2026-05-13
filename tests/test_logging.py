"""Tests for util.logging — centralized Bench governance logging.

Covers: get_logger factory (level, format, duplicate-handler guard),
generate_correlation_id uniqueness and format, and all four governance
helpers (log_evaluation, log_veto, log_chain, log_ledger_entry).

Run: python -m unittest tests.test_logging -v
"""

import logging
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

_REPO_ROOT: Path = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from util.logging import (  # noqa: E402
    generate_correlation_id,
    get_logger,
    log_chain,
    log_evaluation,
    log_ledger_entry,
    log_veto,
)


class GetLoggerTests(unittest.TestCase):
    def _cleanup_logger(self, name: str) -> None:
        logger: logging.Logger = logging.getLogger(name)
        logger.handlers.clear()

    def test_returns_logger_with_requested_name(self) -> None:
        name: str = "bench.test.name"
        self.addCleanup(self._cleanup_logger, name)
        logger: logging.Logger = get_logger(name)
        self.assertEqual(logger.name, name)

    def test_default_level_is_info(self) -> None:
        name: str = "bench.test.defaultlevel"
        self.addCleanup(self._cleanup_logger, name)
        logger: logging.Logger = get_logger(name)
        self.assertEqual(logger.level, logging.INFO)

    @patch.dict("os.environ", {"LOG_LEVEL": "DEBUG"})
    def test_respects_log_level_env_var(self) -> None:
        name: str = "bench.test.envlevel"
        self.addCleanup(self._cleanup_logger, name)
        logger: logging.Logger = get_logger(name)
        self.assertEqual(logger.level, logging.DEBUG)

    @patch.dict("os.environ", {"LOG_LEVEL": "nonsense"})
    def test_invalid_log_level_falls_back_to_info(self) -> None:
        name: str = "bench.test.badlevel"
        self.addCleanup(self._cleanup_logger, name)
        logger: logging.Logger = get_logger(name)
        self.assertEqual(logger.level, logging.INFO)

    def test_handler_is_stream_handler_to_stdout(self) -> None:
        name: str = "bench.test.stdout"
        self.addCleanup(self._cleanup_logger, name)
        logger: logging.Logger = get_logger(name)
        self.assertEqual(len(logger.handlers), 1)
        handler: logging.Handler = logger.handlers[0]
        self.assertIsInstance(handler, logging.StreamHandler)
        self.assertIs(handler.stream, sys.stdout)  # type: ignore[attr-defined]

    def test_no_duplicate_handlers_on_repeated_calls(self) -> None:
        name: str = "bench.test.nodup"
        self.addCleanup(self._cleanup_logger, name)
        get_logger(name)
        get_logger(name)
        get_logger(name)
        logger: logging.Logger = logging.getLogger(name)
        self.assertEqual(len(logger.handlers), 1)

    def test_propagate_is_false(self) -> None:
        name: str = "bench.test.noprop"
        self.addCleanup(self._cleanup_logger, name)
        logger: logging.Logger = get_logger(name)
        self.assertFalse(logger.propagate)

    def test_format_contains_structured_fields(self) -> None:
        name: str = "bench.test.fmt"
        self.addCleanup(self._cleanup_logger, name)
        logger: logging.Logger = get_logger(name)
        fmt: str = logger.handlers[0].formatter._fmt  # type: ignore[union-attr]
        self.assertIn("%(asctime)s", fmt)
        self.assertIn("%(name)s", fmt)
        self.assertIn("%(levelname)s", fmt)
        self.assertIn("%(message)s", fmt)


class GenerateCorrelationIdTests(unittest.TestCase):
    def test_returns_8_char_hex_string(self) -> None:
        cid: str = generate_correlation_id()
        self.assertEqual(len(cid), 8)
        int(cid, 16)  # raises ValueError if not valid hex

    def test_successive_ids_are_unique(self) -> None:
        ids: set[str] = {generate_correlation_id() for _ in range(100)}
        self.assertEqual(len(ids), 100)


class LogEvaluationTests(unittest.TestCase):
    def test_logs_at_info_level(self) -> None:
        with self.assertLogs("bench.governance", level="INFO") as cm:
            log_evaluation("C-001", "Write", "challenger", "FLAGGED", "found issue")
        self.assertEqual(len(cm.output), 1)
        self.assertIn("C-001", cm.output[0])
        self.assertIn("Write", cm.output[0])
        self.assertIn("challenger", cm.output[0])
        self.assertIn("FLAGGED", cm.output[0])

    def test_includes_correlation_id_when_provided(self) -> None:
        with self.assertLogs("bench.governance", level="INFO") as cm:
            log_evaluation(
                "C-002", "Edit", "oracle", "PASS", "ok",
                correlation_id="abcd1234",
            )
        self.assertIn("abcd1234", cm.output[0])

    def test_works_without_correlation_id(self) -> None:
        with self.assertLogs("bench.governance", level="INFO") as cm:
            log_evaluation("C-003", "MultiEdit", "defender", "CLEAR", "clean")
        self.assertNotIn("[None]", cm.output[0])


class LogVetoTests(unittest.TestCase):
    def test_logs_at_warning_level(self) -> None:
        with self.assertLogs("bench.governance", level="WARNING") as cm:
            log_veto("C-007", "Write", "oracle", "weakens enforcement")
        self.assertEqual(len(cm.output), 1)
        self.assertIn("WARNING", cm.output[0])

    def test_contains_veto_keyword_and_constraint(self) -> None:
        with self.assertLogs("bench.governance", level="WARNING") as cm:
            log_veto("C-008", "Edit", "oracle", "ledger tampering")
        self.assertIn("VETO", cm.output[0])
        self.assertIn("C-008", cm.output[0])

    def test_includes_correlation_id_when_provided(self) -> None:
        with self.assertLogs("bench.governance", level="WARNING") as cm:
            log_veto("C-001", "Write", "oracle", "swallowed error", correlation_id="ff00ff00")
        self.assertIn("ff00ff00", cm.output[0])


class LogChainTests(unittest.TestCase):
    def test_logs_at_info_with_correlation_id(self) -> None:
        with self.assertLogs("bench.governance", level="INFO") as cm:
            log_chain("aabb0011", "Write", "challenger=FLAGGED defender=REBUTTED oracle=PASS")
        self.assertIn("aabb0011", cm.output[0])
        self.assertIn("chain complete", cm.output[0])
        self.assertIn("Write", cm.output[0])


class LogLedgerEntryTests(unittest.TestCase):
    def test_logs_hash_and_verdict(self) -> None:
        fake_hash: str = "abc123def456"
        with self.assertLogs("bench.governance", level="INFO") as cm:
            log_ledger_entry(fake_hash, "C-001", "PASS")
        self.assertIn(fake_hash, cm.output[0])
        self.assertIn("ledger write", cm.output[0])
        self.assertIn("PASS", cm.output[0])

    def test_includes_correlation_id_when_provided(self) -> None:
        with self.assertLogs("bench.governance", level="INFO") as cm:
            log_ledger_entry("deadbeef", "C-002", "VETO", correlation_id="11223344")
        self.assertIn("11223344", cm.output[0])


if __name__ == "__main__":
    unittest.main()
