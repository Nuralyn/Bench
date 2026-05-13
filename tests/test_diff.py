"""Tests for the hardened diff extractor in utils.diff.

Covers the four verify scenarios from the hardening task plus edge cases
flagged during governance review (C-005): binary-at-boundary sniffing,
malformed MultiEdit entries, fail-open on unexpected exceptions, and
non-truncation of short inputs. Uses stdlib unittest — no pytest in
requirements.txt.

Run: python -m unittest tests.test_diff -v
"""

import sys
import unittest
from pathlib import Path

_REPO_ROOT: Path = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from utils.diff import (  # noqa: E402
    BINARY_LABEL,
    MAX_DIFF_LINES,
    _is_binary,
    _normalize_path,
    _truncate_preserving,
    build_diff_info,
)


class BuildDiffInfoDispatchTests(unittest.TestCase):
    def test_write_marks_create_and_prefixes_every_line(self) -> None:
        result = build_diff_info(
            "Write",
            {"file_path": "foo.py", "content": "line1\nline2\nline3"},
        )
        self.assertEqual(result["change_type"], "create")
        self.assertEqual(result["file_path"], "foo.py")
        for line in result["formatted_diff"].splitlines():
            self.assertTrue(line.startswith("+"), f"line missing +: {line!r}")

    def test_edit_marks_modify_and_preserves_strings(self) -> None:
        result = build_diff_info(
            "Edit",
            {"file_path": "foo.py", "old_string": "a", "new_string": "b"},
        )
        self.assertEqual(result["change_type"], "modify")
        self.assertEqual(result["old_string"], "a")
        self.assertEqual(result["new_string"], "b")
        self.assertNotIn("truncation", result)

    def test_multi_edit_marks_multi_modify(self) -> None:
        result = build_diff_info(
            "MultiEdit",
            {
                "file_path": "foo.py",
                "edits": [{"old_string": "a", "new_string": "b"}],
            },
        )
        self.assertEqual(result["change_type"], "multi_modify")
        self.assertEqual(
            result["edits"], [{"old_string": "a", "new_string": "b"}]
        )

    def test_unknown_tool_returns_empty_dict(self) -> None:
        self.assertEqual(build_diff_info("Bash", {"command": "ls"}), {})

    def test_non_dict_tool_input_returns_empty_dict(self) -> None:
        self.assertEqual(build_diff_info("Write", None), {})  # type: ignore[arg-type]

    def test_fail_open_on_unexpected_exception(self) -> None:
        class Exploding:
            def __str__(self) -> str:
                raise RuntimeError("boom")

        result = build_diff_info(
            "Write", {"file_path": Exploding(), "content": "x"}
        )
        self.assertEqual(result.get("change_type"), "error")
        self.assertTrue(result.get("diff_error"))
        self.assertIn("error_type", result)
        self.assertIn("error_message", result)


class TruncationTests(unittest.TestCase):
    def _content(self) -> str:
        lines: list[str] = []
        for i in range(400):
            if i == 100:
                lines.append("    def middle_function():")
            elif i == 150:
                lines.append("    except ValueError:")
            elif i == 200:
                lines.append("class MiddleClass:")
            elif i == 250:
                lines.append("    } catch (e) {")
            else:
                lines.append(f"filler_line_{i}")
        return "\n".join(lines)

    def test_truncation_fires_and_preserves_critical_sections(self) -> None:
        content: str = self._content()
        self.assertGreater(len(content.splitlines()), MAX_DIFF_LINES)
        result = build_diff_info(
            "Write", {"file_path": "big.py", "content": content}
        )
        body: str = result["content"]
        self.assertIn("truncation", result)
        self.assertEqual(result["truncation"]["original_lines"], 400)
        self.assertLess(result["truncation"]["truncated_lines"], 400)
        self.assertIn("def middle_function():", body)
        self.assertIn("except ValueError:", body)
        self.assertIn("class MiddleClass:", body)
        self.assertIn("catch (e)", body)
        self.assertIn("filler_line_0", body)
        self.assertIn("filler_line_49", body)
        self.assertIn("filler_line_399", body)
        self.assertIn("filler_line_380", body)
        self.assertIn("original_lines=400", body)
        self.assertNotIn("filler_line_180", body)
        self.assertIn("lines omitted", body)

    def test_short_content_not_truncated(self) -> None:
        content: str = "\n".join(f"line_{i}" for i in range(50))
        result = build_diff_info(
            "Write", {"file_path": "small.py", "content": content}
        )
        self.assertNotIn("truncation", result)
        self.assertEqual(result["content"], content)

    def test_truncate_helper_short_returns_none(self) -> None:
        text, meta = _truncate_preserving("line1\nline2")
        self.assertIsNone(meta)
        self.assertEqual(text, "line1\nline2")

    def test_edit_truncates_per_side(self) -> None:
        long: str = "\n".join(f"line_{i}" for i in range(350))
        short: str = "hi"
        result = build_diff_info(
            "Edit",
            {"file_path": "foo.py", "old_string": long, "new_string": short},
        )
        self.assertIn("truncation", result)
        self.assertIn("old", result["truncation"])
        self.assertNotIn("new", result["truncation"])


class BinaryDetectionTests(unittest.TestCase):
    def test_write_with_null_bytes_collapses_to_metadata(self) -> None:
        content: str = "PNG header\x00\x00\x01\x02lots of bytes"
        result = build_diff_info(
            "Write", {"file_path": "logo.png", "content": content}
        )
        self.assertTrue(result.get("binary"))
        self.assertEqual(result.get("label"), BINARY_LABEL)
        self.assertEqual(result.get("extension"), ".png")
        self.assertEqual(result.get("change_type"), "create")
        self.assertEqual(result.get("content_length_bytes"), len(content))
        for value in result.values():
            if isinstance(value, str):
                self.assertNotIn("\x00", value)
        self.assertNotIn("content", result)
        self.assertNotIn("formatted_diff", result)

    def test_edit_with_binary_old_string(self) -> None:
        result = build_diff_info(
            "Edit",
            {
                "file_path": "icon.ico",
                "old_string": "\x00\x00binary",
                "new_string": "safe",
            },
        )
        self.assertTrue(result.get("binary"))
        self.assertEqual(result.get("label"), BINARY_LABEL)
        self.assertNotIn("old_string", result)

    def test_edit_with_binary_new_string(self) -> None:
        result = build_diff_info(
            "Edit",
            {
                "file_path": "icon.ico",
                "old_string": "safe",
                "new_string": "new\x00bytes",
            },
        )
        self.assertTrue(result.get("binary"))

    def test_multi_edit_with_binary_leg(self) -> None:
        result = build_diff_info(
            "MultiEdit",
            {
                "file_path": "icon.ico",
                "edits": [{"old_string": "safe", "new_string": "bad\x00"}],
            },
        )
        self.assertTrue(result.get("binary"))

    def test_is_binary_at_sniff_boundary(self) -> None:
        self.assertTrue(_is_binary("x" * 8191 + "\x00"))
        self.assertFalse(_is_binary("x" * 8192 + "\x00"))
        self.assertFalse(_is_binary(""))
        self.assertFalse(_is_binary("def foo(): pass"))

    def test_plain_text_not_marked_binary(self) -> None:
        result = build_diff_info(
            "Write",
            {"file_path": "foo.py", "content": "def foo():\n    pass\n"},
        )
        self.assertNotIn("binary", result)


class PathTraversalTests(unittest.TestCase):
    def test_relative_traversal_blocked(self) -> None:
        self.assertEqual(_normalize_path("../../../etc/passwd"), "[PATH_TRAVERSAL_BLOCKED]")

    def test_dotdot_single_level_blocked(self) -> None:
        self.assertEqual(_normalize_path("../sibling.py"), "[PATH_TRAVERSAL_BLOCKED]")

    def test_absolute_path_blocked(self) -> None:
        result = _normalize_path("/etc/passwd")
        self.assertEqual(result, "[PATH_TRAVERSAL_BLOCKED]")

    def test_windows_absolute_path_blocked(self) -> None:
        result = _normalize_path("C:\\Windows\\System32\\config")
        self.assertEqual(result, "[PATH_TRAVERSAL_BLOCKED]")

    def test_normal_relative_path_passes(self) -> None:
        import os
        expected: str = "src\\main.py" if os.sep == "\\" else "src/main.py"
        self.assertEqual(_normalize_path("src/main.py"), expected)

    def test_embedded_dotdot_that_stays_in_project_passes(self) -> None:
        result = _normalize_path("src/../utils/diff.py")
        self.assertNotEqual(result, "[PATH_TRAVERSAL_BLOCKED]")
        self.assertIn("utils", result)

    def test_empty_path_passes_through(self) -> None:
        self.assertEqual(_normalize_path(""), "")

    def test_build_diff_info_normalizes_traversal(self) -> None:
        result = build_diff_info(
            "Write",
            {"file_path": "../../../etc/passwd", "content": "pwned"},
        )
        self.assertEqual(result["file_path"], "[PATH_TRAVERSAL_BLOCKED]")

    def test_build_diff_info_normalizes_absolute(self) -> None:
        result = build_diff_info(
            "Edit",
            {
                "file_path": "/etc/shadow",
                "old_string": "old",
                "new_string": "new",
            },
        )
        self.assertEqual(result["file_path"], "[PATH_TRAVERSAL_BLOCKED]")


class MalformedInputTests(unittest.TestCase):
    def test_multi_edit_with_non_list_edits_field(self) -> None:
        result = build_diff_info(
            "MultiEdit", {"file_path": "foo.py", "edits": "not a list"}
        )
        self.assertEqual(result["change_type"], "multi_modify")
        self.assertEqual(result["edits"], [])

    def test_multi_edit_with_malformed_entry(self) -> None:
        result = build_diff_info(
            "MultiEdit",
            {
                "file_path": "foo.py",
                "edits": [
                    "not a dict",
                    {"old_string": "a", "new_string": "b"},
                ],
            },
        )
        self.assertEqual(len(result["edits"]), 2)
        self.assertIn("error", result["edits"][0])
        self.assertEqual(
            result["edits"][1], {"old_string": "a", "new_string": "b"}
        )


if __name__ == "__main__":
    unittest.main()
