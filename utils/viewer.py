"""Self-contained HTML verdict viewer for the Bench ledger.

Turns the on-disk ``bench-ledger.json`` into a single HTML document with a
stats banner, filter bar, and expandable per-entry detail panels. All
styles, scripts, and ledger data are inlined into the returned string —
no external CDN, no fetch, no separate files. The caller (``cmd_viewer``
in ``cli/commands.py``) writes the returned string to a tempfile and
opens it in the browser.

``generate_viewer_html`` is the only public surface; everything else is
private. The module is fail-open per C-001: any unexpected error is
logged with a full traceback to stderr and surfaced as a minimal error
HTML page rather than raised into the CLI.

Chain integrity is computed in Python via ``verify_chain`` and embedded
as a JS constant. Replicating ``json.dumps(sort_keys=True, default=str)``
plus SHA-256 in the browser would be fragile and offers no auditability
benefit — the user can still run ``python -m cli verify`` for an
independent check.
"""

import html
import json
import sys
import traceback
from typing import Any

from ledger.chain import load_ledger, resolve_ledger_path
from ledger.verify import verify_chain
from utils.stats import compute_ledger_stats, pct

_HASH_SHORT_LEN: int = 12

# Characters that must never appear literally inside an inline <script>
# data block, mapped to their JSON \uXXXX escapes (the convention Django's
# json_script uses). Escaping only "</script" is not enough: an unclosed
# "<!--<script" in governed content drops the HTML parser into the
# script-data-double-escaped state, the page's own closing tag is swallowed
# into the script, and nothing renders. Escaping every "<" closes that
# whole class rather than one spelling of it.
_JSON_SCRIPT_ESCAPES: dict[str, str] = {
    "<": "\\u003C",
    ">": "\\u003E",
    "&": "\\u0026",
}


def _embed_json(value: Any) -> str:
    """Serialize ``value`` for safe inlining inside a <script> element."""
    text: str = json.dumps(value, default=str)
    for raw, escaped in _JSON_SCRIPT_ESCAPES.items():
        text = text.replace(raw, escaped)
    return text


def generate_viewer_html(ledger_path: str | None = None) -> str:
    """Return a complete self-contained HTML string rendering the ledger.

    ``ledger_path`` defaults to ``resolve_ledger_path()`` so the viewer
    renders the same project-scoped chain the writer appends to.

    On any unexpected error, logs to stderr with a full traceback and
    returns a minimal error HTML page — never raises.
    """
    try:
        resolved: str = (
            ledger_path if ledger_path is not None else resolve_ledger_path()
        )
        entries: list[dict] = load_ledger(resolved)
        chain_status: dict = _compute_chain_status(resolved)
        stats: dict = compute_ledger_stats(entries)
        return _build_html(stats, chain_status, entries)
    except Exception as e:
        print(
            f"[bench viewer] generate_viewer_html failed: "
            f"{type(e).__name__}: {e}",
            file=sys.stderr,
        )
        traceback.print_exc(file=sys.stderr)
        return _build_error_html(f"{type(e).__name__}: {e}")


def _compute_chain_status(ledger_path: str) -> dict:
    """Distill ``verify_chain`` output into a viewer-friendly status dict."""
    result: dict = verify_chain(ledger_path)

    if result.get("valid"):
        if int(result.get("entries", 0)) == 0:
            return {
                "status": "EMPTY",
                "failure_index": None,
                "message": "No entries yet.",
            }
        return {
            "status": "VALID",
            "failure_index": None,
            "message": "Chain intact.",
        }

    failure_index: Any = result.get("failure_index")
    try:
        idx: int | None = (
            int(failure_index) if failure_index is not None else None
        )
    except (TypeError, ValueError):
        idx = None
    # verify_chain reports entries-directory failures (MISSING_PARENT,
    # ORPHAN_ENTRY, DUPLICATE_ENTRY, FILENAME_MISMATCH) with index -1: no
    # position in the legacy array applies, so there is no entry number.
    if idx is not None and idx < 0:
        idx = None

    return {
        "status": "BROKEN",
        "failure_index": idx,
        "failure_type": str(result.get("failure_type", "")),
        "message": str(result.get("message", "Chain broken.")),
    }


def _build_html(
    stats: dict,
    chain_status: dict,
    entries: list[dict],
) -> str:
    """Assemble the full HTML document as a single string."""
    entries_json: str = _embed_json(entries)
    chain_json: str = _embed_json(chain_status)

    # Rates are computed over adjudicated entries, excluding chain-retirement
    # anchors, exactly as cmd_stats does: the shared stats helper exists so
    # the terminal report and this banner cannot drift apart.
    adjudicated: int = int(stats.get("adjudicated", 0))
    passed: int = int(stats.get("passed", 0))
    vetoed: int = int(stats.get("vetoed", 0))
    passed_pct: str = pct(passed, adjudicated)
    vetoed_pct: str = pct(vetoed, adjudicated)

    most_cited: Any = stats.get("most_cited")
    if isinstance(most_cited, (list, tuple)) and len(most_cited) == 2:
        cited_label: str = html.escape(
            f"{most_cited[0]} ({most_cited[1]} veto(es))"
        )
    else:
        cited_label = "n/a"

    chain_status_str: str = str(chain_status.get("status", "EMPTY"))
    chain_note_html: str = ""
    if chain_status_str == "VALID":
        chain_label: str = "VALID"
        chain_class: str = "ok"
    elif chain_status_str == "EMPTY":
        chain_label = "EMPTY"
        chain_class = "dim"
    else:
        idx_val: Any = chain_status.get("failure_index")
        failure_type: str = str(chain_status.get("failure_type") or "")
        if isinstance(idx_val, int):
            suffix: str = f" AT ENTRY #{idx_val + 1}"
        elif failure_type:
            suffix = f" ({failure_type})"
        else:
            suffix = ""
        chain_label = f"BROKEN{suffix}"
        chain_class = "err"
        # The verifier's message names the offending hash; a bare BROKEN
        # label would leave the auditor to rerun verify to learn which.
        note: str = html.escape(str(chain_status.get("message") or ""))
        if note:
            chain_note_html = f'<div class="note">{note}</div>'
    chain_label_esc: str = html.escape(chain_label)

    # Fail-closed entries carry verdict VETO with pipeline_error true, so
    # the vetoed count above includes them. cmd_stats prints this count
    # separately; without it here a run of timeouts reads as a strict judge.
    pipeline_errors: int = int(stats.get("pipeline_errors", 0))
    pipeline_class: str = "err" if pipeline_errors else "dim"

    return (
        "<!doctype html>\n"
        "<html lang=\"en\">\n"
        "<head>\n"
        "<meta charset=\"utf-8\">\n"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
        "<title>Bench Verdict Viewer</title>\n"
        "<style>\n"
        f"{_CSS}"
        "</style>\n"
        "</head>\n"
        "<body>\n"
        "<header>\n"
        "  <h1>Bench Verdict Viewer</h1>\n"
        "  <p class=\"subtitle\">Constitutional governance ledger &mdash; read-only</p>\n"
        "</header>\n"
        "<section class=\"banner\" aria-label=\"Statistics\">\n"
        f"  <div class=\"tile\"><div class=\"label\">Governed changes</div><div class=\"value\">{adjudicated}</div></div>\n"
        f"  <div class=\"tile\"><div class=\"label\">Passed</div><div class=\"value ok\">{passed} <span class=\"pct\">({passed_pct})</span></div></div>\n"
        f"  <div class=\"tile\"><div class=\"label\">Vetoed</div><div class=\"value err\">{vetoed} <span class=\"pct\">({vetoed_pct})</span></div></div>\n"
        f"  <div class=\"tile\"><div class=\"label\">Pipeline errors</div><div class=\"value {pipeline_class}\">{pipeline_errors}</div></div>\n"
        f"  <div class=\"tile\"><div class=\"label\">Most cited</div><div class=\"value mono small\">{cited_label}</div></div>\n"
        f"  <div class=\"tile\"><div class=\"label\">Chain status</div><div class=\"value {chain_class}\">{chain_label_esc}</div>{chain_note_html}</div>\n"
        "</section>\n"
        "<section class=\"filter-bar\" role=\"tablist\" aria-label=\"Verdict filter\">\n"
        "  <button type=\"button\" class=\"filter active\" data-filter-value=\"all\" role=\"tab\" aria-selected=\"true\">All</button>\n"
        "  <button type=\"button\" class=\"filter\" data-filter-value=\"PASS\" role=\"tab\" aria-selected=\"false\">PASS</button>\n"
        "  <button type=\"button\" class=\"filter\" data-filter-value=\"VETO\" role=\"tab\" aria-selected=\"false\">VETO</button>\n"
        "</section>\n"
        "<main id=\"entries-root\" data-filter=\"all\">\n"
        "  <p id=\"empty-msg\" class=\"empty-msg\" hidden>No governed changes recorded yet.</p>\n"
        "  <p id=\"filter-empty-msg\" class=\"empty-msg\" hidden></p>\n"
        "  <ol id=\"entries\"></ol>\n"
        "</main>\n"
        "<footer>\n"
        "  <p>Generated by <code>python -m cli viewer</code></p>\n"
        "</footer>\n"
        "<script>\n"
        f"const LEDGER_DATA = {entries_json};\n"
        f"const CHAIN_STATUS = {chain_json};\n"
        f"{_JS}"
        "</script>\n"
        "</body>\n"
        "</html>\n"
    )


def _build_error_html(message: str) -> str:
    """Minimal fallback page rendered when generation itself fails."""
    safe: str = html.escape(message)
    return (
        "<!doctype html>\n"
        "<html><head><meta charset=\"utf-8\"><title>Bench Viewer Error</title>\n"
        "<style>body{background:#1a1a2e;color:#e8e8f0;font-family:system-ui;"
        "padding:2rem;}pre{background:#22223a;padding:1rem;"
        "border-left:3px solid #f87171;white-space:pre-wrap;}</style>\n"
        "</head><body>\n"
        "<h1>Bench Viewer &mdash; generation failed</h1>\n"
        "<p>The viewer could not be built. See stderr for a full traceback.</p>\n"
        f"<pre>{safe}</pre>\n"
        "</body></html>\n"
    )


_CSS: str = """
* { box-sizing: border-box; }
html, body {
  margin: 0; padding: 0;
  background: #1a1a2e; color: #e8e8f0;
  font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
  font-size: 14px; line-height: 1.5;
}
code, pre, .mono { font-family: ui-monospace, "Cascadia Code", Menlo, Consolas, monospace; }
header { padding: 1.5rem 2rem 1rem; border-bottom: 1px solid #3a3a55; }
header h1 { margin: 0; font-size: 1.5rem; font-weight: 500; letter-spacing: 0.02em; }
.subtitle { margin: 0.25rem 0 0; color: #94a3b8; font-size: 0.875rem; }
.banner {
  display: flex; flex-wrap: wrap; gap: 0.75rem;
  padding: 1rem 2rem; background: #22223a;
  border-bottom: 1px solid #3a3a55;
}
.tile {
  flex: 1 1 160px; min-width: 160px;
  padding: 0.75rem 1rem;
  background: #2a2a44; border: 1px solid #3a3a55; border-radius: 4px;
}
.tile .label {
  text-transform: uppercase; letter-spacing: 0.08em;
  font-size: 0.72rem; color: #94a3b8;
}
.tile .value {
  margin-top: 0.25rem; font-size: 1.5rem; font-weight: 500;
}
.tile .value.ok { color: #4ade80; }
.tile .value.err { color: #f87171; }
.tile .value.dim { color: #94a3b8; }
.tile .value.small { font-size: 0.95rem; }
.tile .value.mono { font-family: ui-monospace, Menlo, Consolas, monospace; word-break: break-word; }
.tile .pct { font-size: 0.9rem; color: #94a3b8; font-weight: 400; }
.tile .note {
  margin-top: 0.35rem; font-size: 0.78rem; color: #94a3b8;
  font-family: ui-monospace, Menlo, Consolas, monospace; word-break: break-word;
}
.filter-bar {
  display: flex; gap: 0.5rem;
  padding: 1rem 2rem 0.75rem;
  background: #1a1a2e;
  border-bottom: 1px solid #3a3a55;
}
.filter {
  background: #22223a; color: #e8e8f0;
  border: 1px solid #3a3a55;
  padding: 0.4rem 1rem; border-radius: 4px;
  font: inherit; cursor: pointer;
  transition: background 120ms ease, border-color 120ms ease, color 120ms ease;
}
.filter:hover { background: #2a2a44; }
.filter.active {
  background: #2a2a44; border-color: #a78bfa; color: #a78bfa;
}
main { padding: 1rem 2rem 3rem; }
.empty-msg {
  color: #94a3b8; font-style: italic;
  padding: 2rem 0; text-align: center;
}
ol#entries {
  list-style: none; padding: 0; margin: 0;
  display: flex; flex-direction: column; gap: 0.5rem;
}
.entry {
  background: #22223a;
  border: 1px solid #3a3a55;
  border-left: 3px solid #94a3b8;
  border-radius: 4px;
  overflow: hidden;
}
.entry[data-verdict="PASS"] { border-left-color: #4ade80; }
.entry[data-verdict="VETO"] { border-left-color: #f87171; }
.entry .summary {
  display: flex; align-items: center; gap: 1rem;
  padding: 0.75rem 1rem; cursor: pointer; user-select: none;
}
.entry .summary:hover { background: #2a2a44; }
.entry .summary:focus-visible { outline: 2px solid #a78bfa; outline-offset: -2px; }
.entry .summary .idx {
  flex: 0 0 3.5rem; color: #94a3b8;
  font-family: ui-monospace, Menlo, Consolas, monospace;
  font-size: 0.85rem;
}
.entry .summary .ts {
  flex: 0 0 auto; color: #94a3b8;
  font-family: ui-monospace, Menlo, Consolas, monospace;
  font-size: 0.85rem; white-space: nowrap;
}
.entry .summary .file {
  flex: 1 1 auto;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  font-family: ui-monospace, Menlo, Consolas, monospace;
  font-size: 0.85rem;
}
.entry .summary .tool {
  flex: 0 0 auto; color: #94a3b8;
  font-size: 0.72rem; text-transform: uppercase; letter-spacing: 0.06em;
}
.entry .summary .hash {
  flex: 0 0 auto; color: #94a3b8;
  font-family: ui-monospace, Menlo, Consolas, monospace;
  font-size: 0.8rem;
}
.badge {
  display: inline-block;
  padding: 0.15rem 0.5rem;
  border-radius: 3px;
  font-size: 0.7rem;
  font-weight: 600;
  letter-spacing: 0.04em;
  text-transform: uppercase;
  font-family: ui-monospace, Menlo, Consolas, monospace;
  white-space: nowrap;
}
.badge.pass { background: #4ade80; color: #0a0a14; }
.badge.veto { background: #f87171; color: #1a1a2e; }
.badge.other { background: #3a3a55; color: #e8e8f0; }
.badge.pipeline-error { background: #f59e0b; color: #1a1a2e; }
.badge.concern { background: #f59e0b; color: #1a1a2e; }
.badge.observation { background: #60a5fa; color: #0a0a14; }
.badge.violated { background: #f87171; color: #1a1a2e; }
.badge.not-applicable { background: #3a3a55; color: #94a3b8; }
.badge.concede,
.badge.concede-all { background: #60a5fa; color: #0a0a14; }
.badge.rebuttal { background: #a78bfa; color: #0a0a14; }
.badge.confirm-clear,
.badge.clear { background: #4ade80; color: #0a0a14; }
.entry .detail {
  display: none;
  padding: 1rem 1.25rem;
  border-top: 1px solid #3a3a55;
  background: #1a1a2e;
}
.entry.expanded .detail { display: block; }
.detail h3 {
  margin: 1.25rem 0 0.5rem;
  font-size: 0.8rem;
  text-transform: uppercase;
  letter-spacing: 0.08em;
  color: #a78bfa;
  border-bottom: 1px solid #3a3a55;
  padding-bottom: 0.25rem;
}
.detail h3:first-child { margin-top: 0; }
.detail .field {
  display: flex; gap: 0.75rem;
  margin: 0.3rem 0;
  font-size: 0.88rem;
}
.detail .field .k {
  flex: 0 0 140px;
  color: #94a3b8;
  font-size: 0.78rem;
  text-transform: lowercase;
  padding-top: 0.15rem;
}
.detail .field .v { flex: 1 1 auto; word-break: break-word; }
.detail pre {
  background: #22223a;
  padding: 0.75rem;
  border-radius: 3px;
  max-height: 400px;
  overflow: auto;
  margin: 0.25rem 0;
  font-size: 0.8rem;
  white-space: pre-wrap;
  word-break: break-word;
  border: 1px solid #3a3a55;
}
.detail .card {
  background: #22223a;
  border: 1px solid #3a3a55;
  border-radius: 3px;
  padding: 0.75rem 1rem;
  margin: 0.5rem 0;
}
.detail .card-head {
  display: flex; gap: 0.5rem; align-items: center; flex-wrap: wrap;
  margin-bottom: 0.5rem;
}
.detail .card-body p { margin: 0.35rem 0; font-size: 0.88rem; }
.detail .card-body .k {
  color: #94a3b8;
  font-size: 0.72rem;
  text-transform: uppercase;
  letter-spacing: 0.04em;
  margin-right: 0.35rem;
}
.detail table.citations {
  width: 100%;
  border-collapse: collapse;
  margin: 0.5rem 0;
  font-size: 0.85rem;
}
.detail table.citations th,
.detail table.citations td {
  padding: 0.4rem 0.6rem;
  border: 1px solid #3a3a55;
  text-align: left;
  vertical-align: top;
}
.detail table.citations th {
  background: #22223a;
  color: #94a3b8;
  font-weight: 500;
  font-size: 0.72rem;
  text-transform: uppercase;
  letter-spacing: 0.04em;
}
.detail table.citations td.cid {
  font-family: ui-monospace, Menlo, Consolas, monospace;
  white-space: nowrap;
}
.detail .hashcopy {
  font-family: ui-monospace, Menlo, Consolas, monospace;
  background: #22223a;
  color: #e8e8f0;
  padding: 0.15rem 0.4rem;
  border-radius: 3px;
  cursor: pointer;
  border: 1px solid #3a3a55;
  font-size: 0.8rem;
}
.detail .hashcopy:hover { border-color: #a78bfa; color: #a78bfa; }
.detail .hashcopy.copied { border-color: #4ade80; color: #4ade80; }
.detail ul {
  margin: 0.25rem 0;
  padding-left: 1.25rem;
}
.detail ul li { margin: 0.2rem 0; font-size: 0.88rem; }
/* A filter shows only its own verdict. Listing the verdicts to hide let
   SANITATION and ANCHOR entries through both filters. */
#entries-root[data-filter="PASS"] .entry:not([data-verdict="PASS"]),
#entries-root[data-filter="VETO"] .entry:not([data-verdict="VETO"]) { display: none; }
footer {
  padding: 1rem 2rem;
  color: #94a3b8;
  font-size: 0.8rem;
  border-top: 1px solid #3a3a55;
}
footer code { background: #22223a; padding: 0.1rem 0.35rem; border-radius: 3px; }
"""


_JS: str = """
(function() {
  'use strict';

  const root = document.getElementById('entries-root');
  const list = document.getElementById('entries');
  const emptyMsg = document.getElementById('empty-msg');
  const filterEmptyMsg = document.getElementById('filter-empty-msg');
  const filterButtons = document.querySelectorAll('.filter');

  function shortHash(h, n) {
    if (!h || typeof h !== 'string') return 'N/A';
    n = n || 12;
    if (h.length <= n) return h;
    return h.slice(0, n) + '...';
  }

  function fmtTimestamp(ts) {
    if (!ts) return 'N/A';
    try {
      const d = new Date(ts);
      if (isNaN(d.getTime())) return String(ts);
      return d.toLocaleString();
    } catch (e) {
      return String(ts);
    }
  }

  function verdictOf(entry) {
    if (entry && typeof entry.verdict === 'string' && entry.verdict) {
      return entry.verdict;
    }
    const o = entry && entry.oracle;
    if (o && typeof o === 'object' && typeof o.verdict === 'string') {
      return o.verdict;
    }
    return null;
  }

  function hasPipelineError(entry) {
    if (!entry) return false;
    if (entry.pipeline_error) return true;
    const stages = ['challenger', 'defender', 'oracle'];
    for (let i = 0; i < stages.length; i++) {
      const s = entry[stages[i]];
      if (s && typeof s === 'object' && s.status === 'PIPELINE_ERROR') return true;
    }
    return false;
  }

  function el(tag, props) {
    const node = document.createElement(tag);
    if (props) {
      for (const k in props) {
        if (!Object.prototype.hasOwnProperty.call(props, k)) continue;
        const v = props[k];
        if (k === 'class') node.className = v;
        else if (k === 'dataset') {
          for (const dk in v) {
            if (Object.prototype.hasOwnProperty.call(v, dk)) node.dataset[dk] = v[dk];
          }
        } else if (k.indexOf('on') === 0 && typeof v === 'function') {
          node.addEventListener(k.slice(2), v);
        }
        else if (k === 'text') node.textContent = v;
        else node.setAttribute(k, v);
      }
    }
    for (let i = 2; i < arguments.length; i++) {
      const c = arguments[i];
      if (c == null) continue;
      if (typeof c === 'string') node.appendChild(document.createTextNode(c));
      else node.appendChild(c);
    }
    return node;
  }

  function field(k, v) {
    const vs = (v == null || v === '') ? 'N/A' : String(v);
    return el('div', { class: 'field' },
      el('div', { class: 'k', text: k }),
      el('div', { class: 'v', text: vs })
    );
  }

  function severityClass(s) {
    if (!s) return '';
    return String(s).toLowerCase().replace(/_/g, '-');
  }

  function hashCopy(full) {
    if (!full || typeof full !== 'string') {
      return el('span', { class: 'mono', text: 'N/A' });
    }
    const short = shortHash(full, 16);
    const node = el('button', {
      type: 'button',
      class: 'hashcopy',
      title: 'Click to copy full hash',
      text: short,
      onclick: function(e) {
        e.stopPropagation();
        const writeFn = (navigator.clipboard && navigator.clipboard.writeText)
          ? navigator.clipboard.writeText.bind(navigator.clipboard)
          : null;
        const done = function() {
          node.classList.add('copied');
          node.textContent = 'COPIED';
          setTimeout(function() {
            node.classList.remove('copied');
            node.textContent = short;
          }, 1200);
        };
        if (writeFn) {
          writeFn(full).then(done).catch(function(err) { console.warn('Clipboard copy failed:', err); });
        } else {
          console.warn('Clipboard API not available.');
        }
      }
    });
    return node;
  }

  function preField(k, text) {
    return el('div', { class: 'field' },
      el('div', { class: 'k', text: k }),
      el('div', { class: 'v' }, el('pre', { text: String(text) }))
    );
  }

  // Every diff_summary shape utils.diff.build_diff_info writes gets its own
  // rendering. Anything else falls through to the raw dump, which is a
  // signal that this list needs a new branch, not a display mode.
  const DIFF_SUMMARY_KEYS = [
    'file_path', 'change_type', 'content', 'formatted_diff',
    'old_string', 'new_string', 'redacted', 'note', 'truncation'
  ];

  function renderChange(ch) {
    const container = el('div');
    if (!ch || typeof ch !== 'object') {
      container.appendChild(el('p', { text: 'N/A' }));
      return container;
    }
    if (ch.file) container.appendChild(field('file', ch.file));
    if (ch.tool) container.appendChild(field('tool', ch.tool));
    if (ch.task_description) container.appendChild(field('task', ch.task_description));
    const ds = ch.diff_summary;
    if (ds == null) {
      // nothing
    } else if (typeof ds === 'string') {
      container.appendChild(preField('diff', ds));
    } else if (typeof ds === 'object') {
      if (ds.file_path && ds.file_path !== ch.file) {
        container.appendChild(field('diff file', ds.file_path));
      }
      if (ds.change_type) container.appendChild(field('change type', ds.change_type));
      if (ds.redacted) {
        container.appendChild(field('diff', ds.note || 'Diff body omitted.'));
      }
      if (ds.content != null) container.appendChild(preField('content', ds.content));
      if (ds.formatted_diff != null) container.appendChild(preField('formatted diff', ds.formatted_diff));
      if (ds.old_string != null) container.appendChild(preField('old', ds.old_string));
      if (ds.new_string != null) container.appendChild(preField('new', ds.new_string));
      if (ds.truncation != null) {
        const t = ds.truncation;
        const parts = (t && typeof t === 'object')
          ? Object.keys(t).map(function(k) { return k + ' ' + String(t[k]); })
          : [String(t)];
        container.appendChild(field('truncation', parts.join(', ')));
      }
      const extra = {};
      let extraCount = 0;
      for (const k in ds) {
        if (!Object.prototype.hasOwnProperty.call(ds, k)) continue;
        if (DIFF_SUMMARY_KEYS.indexOf(k) === -1) { extra[k] = ds[k]; extraCount++; }
      }
      if (extraCount > 0) {
        container.appendChild(preField('raw', JSON.stringify(extra, null, 2)));
      }
    }
    return container;
  }

  // A stage that failed records status PIPELINE_ERROR with an error code
  // and, when the model answered at all, the raw response that did not
  // validate. Dropping those left the auditor with a bare status line.
  function appendStageError(container, stage) {
    if (stage.error != null) container.appendChild(field('error', stage.error));
    if (stage.raw_response != null) {
      const raw = (typeof stage.raw_response === 'string')
        ? stage.raw_response
        : JSON.stringify(stage.raw_response, null, 2);
      container.appendChild(preField('raw response', raw));
    }
  }

  function renderChallenger(ch) {
    const container = el('div');
    if (!ch || typeof ch !== 'object' || Object.keys(ch).length === 0) {
      container.appendChild(el('p', { text: 'N/A' }));
      return container;
    }
    container.appendChild(field('status', ch.status));
    appendStageError(container, ch);
    const findings = Array.isArray(ch.findings) ? ch.findings : [];
    if (findings.length === 0) {
      container.appendChild(el('p', { text: ch.status === 'CLEAR' ? 'No findings reported.' : 'No findings in entry.' }));
      return container;
    }
    findings.forEach(function(f) {
      const card = el('div', { class: 'card' });
      const head = el('div', { class: 'card-head' });
      if (f && typeof f === 'object') {
        if (f.constraint_id) head.appendChild(el('span', { class: 'mono', text: f.constraint_id }));
        if (f.severity) head.appendChild(el('span', { class: 'badge ' + severityClass(f.severity), text: f.severity }));
      }
      card.appendChild(head);
      const body = el('div', { class: 'card-body' });
      if (f && typeof f === 'object') {
        if (f.location) body.appendChild(el('p', {}, el('span', { class: 'k', text: 'location' }), el('span', { text: String(f.location) })));
        if (f.evidence) body.appendChild(el('p', {}, el('span', { class: 'k', text: 'evidence' }), el('span', { text: String(f.evidence) })));
        if (f.reasoning) body.appendChild(el('p', {}, el('span', { class: 'k', text: 'reasoning' }), el('span', { text: String(f.reasoning) })));
      } else {
        body.appendChild(el('p', { text: String(f) }));
      }
      card.appendChild(body);
      container.appendChild(card);
    });
    return container;
  }

  function renderDefender(df) {
    const container = el('div');
    if (!df || typeof df !== 'object' || Object.keys(df).length === 0) {
      container.appendChild(el('p', { text: 'N/A' }));
      return container;
    }
    container.appendChild(field('status', df.status));
    appendStageError(container, df);
    if (df.summary) container.appendChild(field('summary', df.summary));
    const rebuttals = Array.isArray(df.rebuttals) ? df.rebuttals : [];
    rebuttals.forEach(function(r) {
      const card = el('div', { class: 'card' });
      const head = el('div', { class: 'card-head' });
      if (r && typeof r === 'object') {
        if (typeof r.finding_index !== 'undefined') head.appendChild(el('span', { class: 'mono', text: 'finding #' + r.finding_index }));
        if (r.position) head.appendChild(el('span', { class: 'badge ' + severityClass(r.position), text: r.position }));
      }
      card.appendChild(head);
      const body = el('div', { class: 'card-body' });
      if (r && typeof r === 'object') {
        if (r.argument) body.appendChild(el('p', {}, el('span', { class: 'k', text: 'argument' }), el('span', { text: String(r.argument) })));
        if (r.evidence) body.appendChild(el('p', {}, el('span', { class: 'k', text: 'evidence' }), el('span', { text: String(r.evidence) })));
      } else {
        body.appendChild(el('p', { text: String(r) }));
      }
      card.appendChild(body);
      container.appendChild(card);
    });
    return container;
  }

  function renderCitations(list) {
    const anyDict = list.some(function(c) { return c && typeof c === 'object'; });
    if (!anyDict) {
      const ul = el('ul');
      list.forEach(function(c) { ul.appendChild(el('li', { class: 'mono', text: String(c) })); });
      return ul;
    }
    const table = el('table', { class: 'citations' });
    const thead = el('thead', {}, el('tr', {},
      el('th', { text: 'Constraint' }),
      el('th', { text: 'Disposition' }),
      el('th', { text: 'Note' })
    ));
    const tbody = el('tbody');
    list.forEach(function(c) {
      if (c && typeof c === 'object') {
        const disp = c.disposition || '';
        const dispCell = el('td');
        if (disp) dispCell.appendChild(el('span', { class: 'badge ' + severityClass(disp), text: disp }));
        else dispCell.textContent = '-';
        tbody.appendChild(el('tr', {},
          el('td', { class: 'cid', text: c.constraint_id || '-' }),
          dispCell,
          el('td', { text: c.note || '-' })
        ));
      } else {
        tbody.appendChild(el('tr', {},
          el('td', { class: 'cid', text: String(c) }),
          el('td', { text: '-' }),
          el('td', { text: '-' })
        ));
      }
    });
    table.appendChild(thead);
    table.appendChild(tbody);
    return table;
  }

  function renderOracle(or) {
    const container = el('div');
    if (!or || typeof or !== 'object' || Object.keys(or).length === 0) {
      container.appendChild(el('p', { text: 'N/A' }));
      return container;
    }
    if (or.verdict) {
      const cls = or.verdict === 'PASS' ? 'pass' : or.verdict === 'VETO' ? 'veto' : 'other';
      container.appendChild(el('div', { class: 'field' },
        el('div', { class: 'k', text: 'verdict' }),
        el('div', { class: 'v' }, el('span', { class: 'badge ' + cls, text: or.verdict }))
      ));
    }
    if (or.status) container.appendChild(field('status', or.status));
    appendStageError(container, or);
    if (or.confidence) container.appendChild(field('confidence', or.confidence));
    if (or.reasoning) {
      container.appendChild(el('div', { class: 'field' },
        el('div', { class: 'k', text: 'reasoning' }),
        el('div', { class: 'v' }, el('pre', { text: String(or.reasoning) }))
      ));
    }
    if (or.remediation) {
      container.appendChild(el('div', { class: 'field' },
        el('div', { class: 'k', text: 'remediation' }),
        el('div', { class: 'v' }, el('pre', { text: String(or.remediation) }))
      ));
    }
    const citations = Array.isArray(or.constraint_citations) ? or.constraint_citations : [];
    if (citations.length > 0) {
      container.appendChild(el('h3', { text: 'Constraint citations' }));
      container.appendChild(renderCitations(citations));
    }
    const advisories = Array.isArray(or.advisories) ? or.advisories : [];
    if (advisories.length > 0) {
      container.appendChild(el('h3', { text: 'Advisories' }));
      const ul = el('ul');
      advisories.forEach(function(a) { ul.appendChild(el('li', { text: String(a) })); });
      container.appendChild(ul);
    }
    return container;
  }

  function sumTokens(entry) {
    let input = 0, output = 0;
    const stages = ['challenger', 'defender', 'oracle'];
    for (let i = 0; i < stages.length; i++) {
      const s = entry[stages[i]];
      if (!s || typeof s !== 'object') continue;
      const tk = s._tokens || s.tokens_used;
      if (tk && typeof tk === 'object') {
        if (typeof tk.input === 'number') input += tk.input;
        if (typeof tk.output === 'number') output += tk.output;
      }
    }
    return { input: input, output: output };
  }

  function renderMetadata(entry, index) {
    const container = el('div');
    container.appendChild(field('entry #', String(index + 1)));
    container.appendChild(field('entry id', entry.entry_id));
    container.appendChild(el('div', { class: 'field' },
      el('div', { class: 'k', text: 'entry hash' }),
      el('div', { class: 'v' }, hashCopy(entry.entry_hash))
    ));
    // previous_hash is a string on legacy entries and a list of parent hashes
    // on entries written after the DAG format; a merge reconciliation names
    // two. Render every parent rather than passing an array to hashCopy, which
    // takes a string and would fall through to 'N/A'.
    const parents = Array.isArray(entry.previous_hash)
      ? entry.previous_hash
      : [entry.previous_hash];
    container.appendChild(el('div', { class: 'field' },
      el('div', {
        class: 'k',
        text: parents.length > 1 ? 'previous hashes' : 'previous hash'
      }),
      el('div', { class: 'v' }, ...parents.map(function (p) { return hashCopy(p); }))
    ));
    if (entry.constitution_hash) {
      container.appendChild(el('div', { class: 'field' },
        el('div', { class: 'k', text: 'constitution hash' }),
        el('div', { class: 'v' }, hashCopy(entry.constitution_hash))
      ));
    }
    const tok = sumTokens(entry);
    container.appendChild(field('tokens (all stages)', 'input ' + tok.input + ' / output ' + tok.output));
    return container;
  }

  function renderEntry(entry, index) {
    const verdict = verdictOf(entry);
    const pipelineErr = hasPipelineError(entry);
    const verdictCssKey = verdict === 'PASS' ? 'pass'
      : verdict === 'VETO' ? 'veto'
      : (verdict ? 'other' : '');
    const dataVerdict = verdict || 'NONE';

    const change = (entry.change && typeof entry.change === 'object') ? entry.change : {};
    const file = change.file || 'unknown';
    const tool = change.tool || '-';

    const li = el('li', {
      class: 'entry',
      dataset: { verdict: dataVerdict, pipelineError: pipelineErr ? 'true' : 'false' }
    });

    const badge = verdict
      ? el('span', { class: 'badge ' + verdictCssKey, text: verdict })
      : el('span', { class: 'mono', text: '-' });
    // Bench fails closed: a stage that timed out or returned an unparseable
    // response records VETO with pipeline_error. That VETO is a pipeline
    // failure, not a ruling, and the row must say so.
    const errorBadge = pipelineErr
      ? el('span', { class: 'badge pipeline-error', text: 'PIPELINE ERROR' })
      : null;

    function toggle() {
      const expanded = li.classList.toggle('expanded');
      summary.setAttribute('aria-expanded', String(expanded));
    }
    const summary = el('div', {
      class: 'summary',
      role: 'button',
      tabindex: '0',
      'aria-expanded': 'false',
      onclick: toggle,
      onkeydown: function(e) {
        if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggle(); }
      }
    },
      el('span', { class: 'idx', text: '#' + (index + 1) }),
      el('span', { class: 'ts', text: fmtTimestamp(entry.timestamp) }),
      el('span', { class: 'file', title: file, text: file }),
      el('span', { class: 'tool', text: tool }),
      badge,
      errorBadge,
      el('span', { class: 'hash', text: shortHash(entry.entry_hash) })
    );
    li.appendChild(summary);

    const detail = el('div', { class: 'detail' });
    detail.appendChild(el('h3', { text: 'Change' }));
    detail.appendChild(renderChange(entry.change));
    detail.appendChild(el('h3', { text: 'Challenger' }));
    detail.appendChild(renderChallenger(entry.challenger));
    detail.appendChild(el('h3', { text: 'Defender' }));
    detail.appendChild(renderDefender(entry.defender));
    detail.appendChild(el('h3', { text: 'Oracle' }));
    detail.appendChild(renderOracle(entry.oracle));
    detail.appendChild(el('h3', { text: 'Metadata' }));
    detail.appendChild(renderMetadata(entry, index));
    li.appendChild(detail);

    return li;
  }

  function updateFilterMessage() {
    const filter = root.dataset.filter;
    if (!LEDGER_DATA || LEDGER_DATA.length === 0) {
      emptyMsg.hidden = false;
      filterEmptyMsg.hidden = true;
      return;
    }
    emptyMsg.hidden = true;
    if (filter === 'all') {
      filterEmptyMsg.hidden = true;
      return;
    }
    const children = list.children;
    let matchFound = false;
    for (let i = 0; i < children.length; i++) {
      if (children[i].dataset.verdict === filter) { matchFound = true; break; }
    }
    if (matchFound) {
      filterEmptyMsg.hidden = true;
    } else {
      filterEmptyMsg.hidden = false;
      filterEmptyMsg.textContent = 'No ' + filter + ' entries to show.';
    }
  }

  function init() {
    const entries = Array.isArray(LEDGER_DATA) ? LEDGER_DATA : [];
    entries.forEach(function(entry, i) {
      try {
        const node = renderEntry(entry, i);
        list.insertBefore(node, list.firstChild);
      } catch (err) {
        console.error('Failed to render entry', i, err);
      }
    });
    updateFilterMessage();

    filterButtons.forEach(function(btn) {
      btn.addEventListener('click', function() {
        const v = btn.dataset.filterValue;
        root.dataset.filter = v;
        filterButtons.forEach(function(b) {
          const active = b.dataset.filterValue === v;
          b.classList.toggle('active', active);
          b.setAttribute('aria-selected', String(active));
        });
        updateFilterMessage();
      });
    });

    if (CHAIN_STATUS && CHAIN_STATUS.message) {
      console.info('Bench chain status:', CHAIN_STATUS.status, '-', CHAIN_STATUS.message);
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
"""
