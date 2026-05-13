"""Tests for error sanitization in utils.api.

Run: python -m unittest tests.test_api -v
"""

import sys
import unittest
from pathlib import Path

_REPO_ROOT: Path = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from utils.api import _sanitize_error_detail  # noqa: E402


class SanitizeErrorDetailTests(unittest.TestCase):
    def test_strips_sk_api_key(self) -> None:
        msg = "AuthenticationError: Invalid API key sk-ant-1234567890abcdef in request"
        result = _sanitize_error_detail(msg)
        self.assertNotIn("sk-ant-1234567890abcdef", result)
        self.assertIn("[REDACTED]", result)

    def test_strips_bearer_token(self) -> None:
        msg = "Header: Bearer eyJhbGciOiJIUzI1NiJ9.secretdata"
        result = _sanitize_error_detail(msg)
        self.assertNotIn("eyJhbGciOiJIUzI1NiJ9", result)
        self.assertIn("[REDACTED]", result)

    def test_strips_api_key_assignment(self) -> None:
        msg = 'Config error: api_key="sk-secret-key-value" is invalid'
        result = _sanitize_error_detail(msg)
        self.assertIn("[REDACTED]", result)

    def test_truncates_long_messages(self) -> None:
        msg = "x" * 1000
        result = _sanitize_error_detail(msg)
        self.assertLessEqual(len(result), 600)
        self.assertTrue(result.endswith("... [truncated]"))

    def test_passes_through_clean_messages(self) -> None:
        msg = "Connection timed out after 30s"
        result = _sanitize_error_detail(msg)
        self.assertEqual(result, msg)

    def test_empty_string_passes_through(self) -> None:
        self.assertEqual(_sanitize_error_detail(""), "")


if __name__ == "__main__":
    unittest.main()
