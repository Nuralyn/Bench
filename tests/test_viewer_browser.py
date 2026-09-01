"""Browser-level tests for the HTML ledger viewer.

tests/test_viewer.py asserts on the generated string. The failures the
viewer audit found (a blank page under a healthy banner, a fail-closed VETO
indistinguishable from a ruling, filters leaking anchors) only exist once a
browser has parsed and run the page, so this module loads the generated
HTML in headless Chromium through Playwright and asserts on the DOM.

Playwright is a development-only dependency (requirements-dev.txt). The
module skips itself, with the reason printed, when the package or its
browser is unavailable, so the default test run needs neither. CI's
browser-tests job installs both and runs it for real.

Run: python -m unittest tests.test_viewer_browser -v
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

_REPO_ROOT: Path = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from tests._ledger_fixtures import build_valid_chain  # noqa: E402
from ledger.chain import ANCHOR_VERDICT, compute_entry_hash  # noqa: E402
from ledger.retire import ANCHOR_TOOL  # noqa: E402
from utils.viewer import generate_viewer_html  # noqa: E402

try:
    from playwright.sync_api import Browser, Locator, Page, Playwright
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover - exercised only without the dev deps
    sync_playwright = None  # type: ignore[assignment]
    PlaywrightError = Exception  # type: ignore[assignment,misc]


_SCRIPT_SNIPPET: str = '<!--<script src="a.js">'


def _relink(chain: list[dict[str, Any]]) -> None:
    """Recompute every link after the fixture entries have been edited."""
    previous: str = "GENESIS"
    for entry in chain:
        entry["previous_hash"] = previous
        entry["entry_hash"] = compute_entry_hash(entry)
        previous = entry["entry_hash"]


def _build_fixture_chain() -> list[dict[str, Any]]:
    """Five entries covering every rendering path the audit found broken."""
    chain: list[dict[str, Any]] = build_valid_chain(
        4, verdicts=["PASS", "VETO", "VETO", "PASS"]
    )
    # 1: an Edit whose old_string would have blanked the page.
    chain[0]["change"]["tool"] = "Edit"
    chain[0]["change"]["diff_summary"] = {
        "file_path": chain[0]["change"]["file"],
        "change_type": "modify",
        "old_string": _SCRIPT_SNIPPET,
        "new_string": '<script src="b.js">',
        # utils.diff._build_edit nests one truncation record per side.
        "truncation": {
            "old": {"original_lines": 362, "truncated_lines": 73},
        },
    }
    # 3: a fail-closed VETO. The Oracle never ruled; the stage timed out.
    chain[2]["verdict"] = "VETO"
    chain[2]["pipeline_error"] = True
    chain[2]["oracle"] = {
        "status": "PIPELINE_ERROR",
        "error": "ORACLE_TIMEOUT",
        "raw_response": "not json",
    }
    # 4: a change outside the project, body redacted at write time.
    chain[3]["change"]["diff_summary"] = {
        "file_path": chain[3]["change"]["file"],
        "change_type": "create",
        "redacted": True,
        "note": "Diff body omitted: file lies outside this ledger's project.",
    }
    # 5: a chain-retirement anchor, which is neither PASS nor VETO.
    chain.append(
        {
            "entry_id": "id-anchor",
            "timestamp": "2026-01-01T00:01:00+00:00",
            "previous_hash": "",
            "constitution_hash": "abc",
            "verdict": ANCHOR_VERDICT,
            "change": {"file": "ledger/bench-ledger.json", "tool": ANCHOR_TOOL},
            "oracle": {},
        }
    )
    _relink(chain)
    return chain


def _unavailable(reason: str) -> Exception:
    """Skip locally; fail where the browser is required.

    CI's browser-tests job sets BENCH_REQUIRE_BROWSER so a broken install
    surfaces as a failure instead of a green run that tested nothing.
    """
    if os.environ.get("BENCH_REQUIRE_BROWSER"):
        return AssertionError(f"browser required but unavailable: {reason}")
    return unittest.SkipTest(reason)


class ViewerBrowserTests(unittest.TestCase):
    _pw: Playwright
    _browser: Browser

    @classmethod
    def setUpClass(cls) -> None:
        if sync_playwright is None:
            raise _unavailable(
                "playwright is not installed "
                "(pip install -r requirements-dev.txt)"
            )
        cls._pw = sync_playwright().start()
        try:
            cls._browser = cls._pw.chromium.launch()
        except PlaywrightError as exc:
            cls._pw.stop()
            raise _unavailable(
                "chromium is not installed "
                f"(python -m playwright install chromium): {exc}"
            )

    @classmethod
    def tearDownClass(cls) -> None:
        if getattr(cls, "_browser", None) is not None:
            cls._browser.close()
        if getattr(cls, "_pw", None) is not None:
            cls._pw.stop()

    def setUp(self) -> None:
        self._tmp: str = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self._tmp)
        self.chain: list[dict[str, Any]] = _build_fixture_chain()
        ledger_path: str = os.path.join(self._tmp, "ledger.json")
        Path(ledger_path).write_text(json.dumps(self.chain), encoding="utf-8")
        html_path: Path = Path(self._tmp) / "viewer.html"
        html_path.write_text(generate_viewer_html(ledger_path), encoding="utf-8")

        self.errors: list[str] = []
        self.page: Page = self._browser.new_page()
        self.addCleanup(self.page.close)
        self.page.on("pageerror", lambda exc: self.errors.append(str(exc)))
        self.page.on(
            "console",
            lambda msg: self.errors.append(msg.text)
            if msg.type == "error"
            else None,
        )
        self.page.goto(html_path.as_uri())

    def _entry(self, number: int) -> Locator:
        """Locate the row for ledger entry ``number`` (1-based, load order)."""
        return self.page.locator(".entry").filter(
            has=self.page.locator(".idx", has_text=f"#{number}")
        )

    def _visible_verdicts(self) -> list[str]:
        return self.page.locator(".entry:visible").evaluate_all(
            "rows => rows.map(r => r.dataset.verdict)"
        )

    def test_every_entry_renders_and_nothing_errors(self) -> None:
        self.assertEqual(self.page.locator(".entry").count(), len(self.chain))
        self.assertEqual(self.errors, [])
        # The embedded data survived the HTML parser byte for byte.
        embedded: list[dict[str, Any]] = self.page.evaluate("LEDGER_DATA")
        self.assertEqual(embedded, self.chain)

    def test_edit_diff_renders_old_and_new_not_a_raw_dump(self) -> None:
        row: Locator = self._entry(1)
        row.locator(".summary").click()
        keys: list[str] = row.locator(".detail .field .k").all_text_contents()
        self.assertIn("old", keys)
        self.assertIn("new", keys)
        self.assertNotIn("raw", keys)
        self.assertIn(
            _SCRIPT_SNIPPET, row.locator(".detail pre").first.text_content()
        )
        # Nested truncation records keep their counts.
        detail: str = row.locator(".detail").inner_text()
        self.assertIn('"original_lines":362', detail)
        self.assertNotIn("[object Object]", detail)

    def test_fail_closed_veto_is_labelled_a_pipeline_error(self) -> None:
        rows: Locator = self.page.locator('.entry[data-pipeline-error="true"]')
        self.assertEqual(rows.count(), 1)
        row: Locator = rows.first
        self.assertEqual(
            row.locator(".summary .badge.veto").text_content(), "VETO"
        )
        self.assertEqual(
            row.locator(".summary .badge.pipeline-error").text_content(),
            "PIPELINE ERROR",
        )
        row.locator(".summary").click()
        detail: str = row.locator(".detail").inner_text()
        self.assertIn("ORACLE_TIMEOUT", detail)
        self.assertIn("not json", detail)
        tile: Locator = self.page.locator(".tile", has_text="Pipeline errors")
        self.assertEqual(tile.locator(".value").text_content(), "1")

    def test_redacted_change_shows_its_note(self) -> None:
        row: Locator = self._entry(4)
        row.locator(".summary").click()
        self.assertIn("Diff body omitted", row.locator(".detail").inner_text())
        self.assertNotIn(
            "raw", row.locator(".detail .field .k").all_text_contents()
        )

    def test_filters_show_only_their_own_verdict(self) -> None:
        anchor: Locator = self._entry(5)
        self.assertTrue(anchor.is_visible())

        self.page.locator('.filter[data-filter-value="PASS"]').click()
        self.assertEqual(self._visible_verdicts(), ["PASS", "PASS"])
        self.assertFalse(anchor.is_visible())

        self.page.locator('.filter[data-filter-value="VETO"]').click()
        self.assertEqual(self._visible_verdicts(), ["VETO", "VETO"])
        self.assertFalse(anchor.is_visible())

    def test_dashboard_restates_the_ledger_tallies(self) -> None:
        self.assertEqual(self.page.locator(".dashboard .card").count(), 4)
        # The fixture spans one ISO week, so each of the two rate charts
        # draws one column, and every column carries a hover title.
        columns: Locator = self.page.locator("#dash-weeks svg path")
        self.assertEqual(columns.count(), 2)
        self.assertEqual(
            columns.first.locator("title").text_content(),
            "2026-W01: 2 of 4 (50.0%)",
        )
        week_cells: list[str] = self.page.locator(
            "#dash-weeks tbody td"
        ).all_text_contents()
        # Five entries: the anchor is not adjudicated; two passed, two
        # vetoed, one of those a pipeline error.
        self.assertEqual(
            week_cells, ["2026-W01", "4", "2", "2", "1", "50.0%", "25.0%"]
        )
        self.assertEqual(
            self.page.locator("#dash-constraints tbody td").all_text_contents(),
            ["C-001", "1", "1"],
        )
        self.assertIn(
            "n/a",
            self.page.locator("#dash-tokens tbody td").all_text_contents(),
        )

    def test_dashboard_tables_stay_inside_their_cards(self) -> None:
        # On a wide screen the grid once opened an empty fourth column and
        # squeezed the lower cards until their tables spilled past the card
        # edge. Every table must fit its card with no internal scrolling,
        # and the page must never scroll horizontally.
        # 1701 is the narrowest three-column layout; 1400 the widest
        # two-column one; 1000 is single column.
        for width in (1920, 1701, 1400, 1000):
            self.page.set_viewport_size({"width": width, "height": 1080})
            cards: list[dict[str, Any]] = self.page.evaluate(
                "[...document.querySelectorAll('.dashboard .card')].map(c => {"
                "  const t = c.querySelector('table');"
                "  return {id: c.id, width: c.clientWidth,"
                "          fits: t.getBoundingClientRect().right"
                "                <= c.getBoundingClientRect().right,"
                "          scrolls: c.scrollWidth > c.clientWidth};"
                "})"
            )
            for card in cards:
                self.assertTrue(card["fits"], (width, card))
                self.assertFalse(card["scrolls"], (width, card))
            self.assertLessEqual(
                self.page.evaluate("document.documentElement.scrollWidth"),
                width,
            )

    def test_summary_row_is_keyboard_operable(self) -> None:
        row: Locator = self._entry(1)
        summary: Locator = row.locator(".summary")
        self.assertEqual(summary.get_attribute("role"), "button")
        summary.focus()
        self.page.keyboard.press("Enter")
        self.assertTrue(row.locator(".detail").is_visible())
        self.assertEqual(summary.get_attribute("aria-expanded"), "true")
        self.page.keyboard.press("Space")
        self.assertFalse(row.locator(".detail").is_visible())
        self.assertEqual(summary.get_attribute("aria-expanded"), "false")


if __name__ == "__main__":
    unittest.main()
