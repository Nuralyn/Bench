"""Tests for utils.formatting rendering helpers.

Covers the branches flagged by the C-005 audit of utils/formatting.py:
format_diff_for_display across its four shape branches plus malformed
variants, _truncate across its three decision paths, and _indent across
its two. Uses stdlib unittest to match project conventions
(see tests/test_diff.py — no pytest in requirements.txt).

Run: python -m unittest tests.test_formatting -v
"""

import sys
import unittest
from pathlib import Path

_REPO_ROOT: Path = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from utils.formatting import (  # noqa: E402
    _indent,
    _truncate,
    format_diff_for_display,
)


class FormatDiffForDisplayTests(unittest.TestCase):
    def test_non_dict_input_returns_no_diff_info(self) -> None:
        self.assertEqual(
            format_diff_for_display("not a dict"),  # type: ignore[arg-type]
            "(no diff info)",
        )

    def test_empty_dict_returns_no_diff_info(self) -> None:
        self.assertEqual(format_diff_for_display({}), "(no diff info)")

    def test_write_shape_renders_file_and_body(self) -> None:
        result: str = format_diff_for_display(
            {"file_path": "foo.py", "content": "print('hi')"}
        )
        self.assertEqual(result, "Write: foo.py\n---\nprint('hi')")

    def test_edit_shape_renders_old_and_new(self) -> None:
        result: str = format_diff_for_display(
            {"file_path": "foo.py", "old_string": "a", "new_string": "b"}
        )
        self.assertEqual(
            result,
            "Edit: foo.py\n--- old ---\na\n--- new ---\nb",
        )

    def test_multi_edit_with_valid_edits_renders_each_entry(self) -> None:
        result: str = format_diff_for_display(
            {
                "file_path": "foo.py",
                "edits": [
                    {"old_string": "a", "new_string": "b"},
                    {"old_string": "c", "new_string": "d"},
                ],
            }
        )
        self.assertIn("MultiEdit: foo.py", result)
        self.assertIn("[1]", result)
        self.assertIn("[2]", result)

    def test_multi_edit_with_non_list_edits_hits_malformed_field_branch(
        self,
    ) -> None:
        result: str = format_diff_for_display(
            {"file_path": "foo.py", "edits": "not a list"}
        )
        self.assertIn("(edits field is not a list: got str)", result)

    def test_multi_edit_with_non_dict_entry_hits_malformed_entry_branch(
        self,
    ) -> None:
        result: str = format_diff_for_display(
            {"file_path": "foo.py", "edits": ["not a dict"]}
        )
        self.assertIn("(malformed edit entry)", result)

    def test_unknown_shape_hits_keys_fallback_branch(self) -> None:
        result: str = format_diff_for_display(
            {"file_path": "foo.py", "weird_key": "x"}
        )
        self.assertEqual(
            result,
            "Unknown diff shape for foo.py (keys: file_path, weird_key)",
        )


class TruncateTests(unittest.TestCase):
    def test_short_text_returns_unchanged(self) -> None:
        self.assertEqual(_truncate("hello", 10), "hello")

    def test_long_text_is_truncated_with_suffix(self) -> None:
        text: str = "x" * 100
        result: str = _truncate(text, 10)
        self.assertEqual(
            result, "x" * 10 + " ... [truncated, 100 chars total]"
        )

    def test_non_positive_max_length_disables_truncation(self) -> None:
        text: str = "x" * 100
        self.assertEqual(_truncate(text, 0), text)


class IndentTests(unittest.TestCase):
    def test_normal_string_gets_prefix_on_each_line(self) -> None:
        self.assertEqual(_indent("a\nb", prefix="  "), "  a\n  b")

    def test_empty_string_returns_bare_prefix(self) -> None:
        self.assertEqual(_indent("", prefix="  "), "  ")


if __name__ == "__main__":
    unittest.main()
