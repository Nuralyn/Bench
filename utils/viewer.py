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

from ledger.chain import load_ledger
from ledger.verify import verify_chain
from utils.stats import compute_ledger_stats, pct

_DEFAULT_LEDGER_PATH: str = "ledger/bench-ledger.json"
_HASH_SHORT_LEN: int = 12


def generate_viewer_html(ledger_path: str = _DEFAULT_LEDGER_PATH) -> str:
    """Return a complete self-contained HTML string rendering the ledger.

    On any unexpected error, logs to stderr with a full traceback and
    returns a minimal error HTML page — never raises.
    """
    try:
        entries: list[dict] = load_ledger(ledger_path)
        chain_status: dict = _compute_chain_status(ledger_path)
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

    return {
        "status": "BROKEN",
        "failure_index": idx,
        "message": str(result.get("message", "Chain broken.")),
    }


def _build_html(
    stats: dict,
    chain_status: dict,
    entries: list[dict],
) -> str:
    """Assemble the full HTML document as a single string."""
    entries_json: str = json.dumps(entries, default=str).replace("</", "<\\/")
    chain_json: str = json.dumps(chain_status).replace("</", "<\\/")

    total: int = int(stats.get("total", 0))
    passed: int = int(stats.get("passed", 0))
    vetoed: int = int(stats.get("vetoed", 0))
    passed_pct: str = pct(passed, total)
    vetoed_pct: str = pct(vetoed, total)

    most_cited: Any = stats.get("most_cited")
    if isinstance(most_cited, (list, tuple)) and len(most_cited) == 2:
        cited_label: str = html.escape(
            f"{most_cited[0]} ({most_cited[1]} veto(es))"
        )
    else:
        cited_label = "n/a"

    chain_status_str: str = str(chain_status.get("status", "EMPTY"))
    if chain_status_str == "VALID":
        chain_label: str = "VALID"
        chain_class: str = "ok"
    elif chain_status_str == "EMPTY":
        chain_label = "EMPTY"
        chain_class = "dim"
    else:
        idx_val: Any = chain_status.get("failure_index")
        suffix: str = (
            f" AT ENTRY #{idx_val + 1}" if isinstance(idx_val, int) else ""
        )
        chain_label = f"BROKEN{suffix}"
        chain_class = "err"
    chain_label_esc: str = html.escape(chain_label)

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
        f"  <div class=\"tile\"><div class=\"label\">Total changes</div><div class=\"value\">{total}</div></div>\n"
        f"  <div class=\"tile\"><div class=\"label\">Passed</div><div class=\"value ok\">{passed} <span class=\"pct\">({passed_pct})</span></div></div>\n"
        f"  <div class=\"tile\"><div class=\"label\">Vetoed</div><div class=\"value err\">{vetoed} <span class=\"pct\">({vetoed_pct})</span></div></div>\n"
        f"  <div class=\"tile\"><div class=\"label\">Most cited</div><div class=\"value mono small\">{cited_label}</div></div>\n"
        f"  <div class=\"tile\"><div class=\"label\">Chain status</div><div class=\"value {chain_class}\">{chain_label_esc}</div></div>\n"
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
.badge.fail-open { background: #94a3b8; color: #1a1a2e; }
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
#entries-root[data-filter="PASS"] .entry[data-verdict="VETO"],
#entries-root[data-filter="PASS"] .entry[data-verdict="NONE"] { display: none; }
#entries-root[data-filter="VETO"] .entry[data-verdict="PASS"],
#entries-root[data-filter="VETO"] .entry[data-verdict="NONE"] { display: none; }
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
    const o = entry && entry.oracle;
    if (o && typeof o === 'object' && typeof o.verdict === 'string') {
      return o.verdict;
    }
    return null;
  }

  function hasPipelineError(entry) {
    if (!entry) return false;
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
        } else if (k === 'onclick') node.addEventListener('click', v);
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
      container.appendChild(el('div', { class: 'field' },
        el('div', { class: 'k', text: 'diff' }),
        el('div', { class: 'v' }, el('pre', { text: ds }))
      ));
    } else if (typeof ds === 'object') {
      if (ds.file_path && ds.file_path !== ch.file) {
        container.appendChild(field('diff file', ds.file_path));
      }
      if (ds.change_type) container.appendChild(field('change type', ds.change_type));
      if (ds.content != null) {
        container.appendChild(el('div', { class: 'field' },
          el('div', { class: 'k', text: 'content' }),
          el('div', { class: 'v' }, el('pre', { text: String(ds.content) }))
        ));
      }
      if (ds.formatted_diff != null) {
        container.appendChild(el('div', { class: 'field' },
          el('div', { class: 'k', text: 'formatted diff' }),
          el('div', { class: 'v' }, el('pre', { text: String(ds.formatted_diff) }))
        ));
      }
      const extra = {};
      let extraCount = 0;
      const known = ['file_path', 'content', 'change_type', 'formatted_diff'];
      for (const k in ds) {
        if (!Object.prototype.hasOwnProperty.call(ds, k)) continue;
        if (known.indexOf(k) === -1) { extra[k] = ds[k]; extraCount++; }
      }
      if (extraCount > 0) {
        container.appendChild(el('div', { class: 'field' },
          el('div', { class: 'k', text: 'raw' }),
          el('div', { class: 'v' }, el('pre', { text: JSON.stringify(extra, null, 2) }))
        ));
      }
    }
    return container;
  }

  function renderChallenger(ch) {
    const container = el('div');
    if (!ch || typeof ch !== 'object' || Object.keys(ch).length === 0) {
      container.appendChild(el('p', { text: 'N/A' }));
      return container;
    }
    container.appendChild(field('status', ch.status));
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
      const cls = or.verdict === 'PASS' ? 'pass' : or.verdict === 'VETO' ? 'veto' : '';
      container.appendChild(el('div', { class: 'field' },
        el('div', { class: 'k', text: 'verdict' }),
        el('div', { class: 'v' }, el('span', { class: 'badge ' + cls, text: or.verdict }))
      ));
    }
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
    container.appendChild(el('div', { class: 'field' },
      el('div', { class: 'k', text: 'previous hash' }),
      el('div', { class: 'v' }, hashCopy(entry.previous_hash))
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
    const verdictLabel = verdict || (pipelineErr ? 'FAIL-OPEN' : '-');
    const verdictCssKey = verdict === 'PASS' ? 'pass'
      : verdict === 'VETO' ? 'veto'
      : (pipelineErr ? 'fail-open' : '');
    const dataVerdict = verdict || 'NONE';

    const change = (entry.change && typeof entry.change === 'object') ? entry.change : {};
    const file = change.file || 'unknown';
    const tool = change.tool || '-';

    const li = el('li', {
      class: 'entry',
      dataset: { verdict: dataVerdict }
    });

    const badge = (verdictLabel !== '-')
      ? el('span', { class: 'badge ' + verdictCssKey, text: verdictLabel })
      : el('span', { class: 'mono', text: '-' });

    const summary = el('div', {
      class: 'summary',
      onclick: function() { li.classList.toggle('expanded'); }
    },
      el('span', { class: 'idx', text: '#' + (index + 1) }),
      el('span', { class: 'ts', text: fmtTimestamp(entry.timestamp) }),
      el('span', { class: 'file', title: file, text: file }),
      el('span', { class: 'tool', text: tool }),
      badge,
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
