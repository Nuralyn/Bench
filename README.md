# Bench

Constitutional governance for Claude Code.

Every code change Claude proposes is challenged, defended, ruled on, and
recorded before it touches your files. Every verdict is hash-chained into
an auditable ledger. Governance of AI reasoning is a primitive, not a feature.

## The Problem

AI coding tools ship unchallenged, unaudited, untraceable code. When Claude
Code writes a function, nothing stops it from swallowing errors silently,
leaking credentials, or creeping beyond the scope of the task. Self-verification
is a step forward, but without adversarial challenge, binding authority, and
cryptographic evidence, it is just an opinion.

## How Bench Works

```
Proposed Change -> Challenger -> Defender -> Oracle -> Ledger
                   (Sonnet)     (Sonnet)    (Opus)    (SHA-256)
```

1. **Challenge.** A Challenger model examines the proposed change against a
   declared constitution of binding constraints. It surfaces evidence.
2. **Defend.** A Defender model argues for the soundness of the change,
   rebutting or conceding each finding.
3. **Rule.** An Oracle model weighs both sides and issues a binding verdict:
   PASS or VETO. A veto blocks the change and provides remediation guidance.
4. **Record.** Every verdict is hash-chained into an append-only ledger.
   The evidence is permanent, traceable, and tamper-evident.

## The Constitution

Bench enforces a declared set of constraints (bench.json). Each constraint
has a severity level (veto or warning) and a rationale. Users can add their
own constraints. The constitution is law. The Oracle enforces it.

See [bench.json](bench.json) for the current constraints.

## Self-Governance

This tool was built under its own governance. Every change authored through
Claude Code's file tools was challenged, defended, ruled on, and recorded by
Bench itself.

One category is outside that boundary and is named here rather than glossed
over: bot-authored dependency PRs. Dependabot edits `requirements.txt` on
GitHub, which never reaches the PreToolUse hook, so those changes merge on a
human decision and carry no ledger entry.

A second, wider boundary deserves the same candor: Bench governs the `Write`,
`Edit`, and `MultiEdit` tools, and nothing else. Any file written through `Bash`
— shell redirection, `tee`, `sed -i`, a `python - <<EOF` heredoc — never reaches
the PreToolUse hook. No challenge, no verdict, no ledger entry. This is not a
narrow gap: it is a complete bypass of the governance layer, available to any
model or human with shell access, and it leaves no trace in the chain to show it
was used.

Bench does not close this hole, and adding `Bash` to the matcher yourself does
not close it either — it breaks the tool instead. `utils.diff.build_diff_info`
produces a payload only for `Write`, `Edit`, and `MultiEdit`; every other tool
yields an empty dict, which the Challenger rejects as a malformed input and the
runner fail-closes into a VETO. The practical result is that every `git status`,
test run, and build is denied without ever reaching a model. Bash matching is
unsupported today, not merely slow, and it should not be enabled.

Closing the hole properly needs a meaningful diff representation for a shell
command — deciding which files `sed -i`, a heredoc, or a redirect will touch
before it runs. That is a real piece of work and is not done.

MCP servers are the same gap, and a less visible one. Any server registered in a
project's `.mcp.json` is spawned by Claude Code with its own tool surface, and
none of those tools pass through the PreToolUse hook. A server exposing
file-write or shell capability bypasses Bench exactly as `Bash` does — but a
tool named `write_file` reads as a governed primitive, where a shell command at
least looks like a shell command.

Bench already accounts for MCP in exactly one place, and it is worth being
precise about which. `utils/api.py` spawns the judge with `--strict-mcp-config`
and no `--mcp-config`, dropping every MCP server so a prompt-injected diff cannot
drive the judge into running MCP tools. That hardens the judge's own sandbox. It
does nothing for the governed project's tool surface, which is where this gap
lives.

The mitigation available today is to audit a server's package before registering
it in a governed project — read what it can reach, not what it advertises. Be
honest about what that buys: it is a point-in-time check of one version, it does
not survive an upgrade, and a source grep is necessary rather than sufficient. A
server can reach files through `os.popen`, `pty`, an asyncio subprocess,
`Path.open`, a dynamic import, or a remote API, and many MCP servers are Node
rather than Python. An audit raises confidence; it does not produce a guarantee,
and it is not governance.

So read the boundary plainly: Bench governs the file-editing tools it hooks.
Shell commands and MCP server tools are outside that boundary. What Bench
guarantees is that changes made through the governed file tools were adjudicated
and recorded — not that every change to the repository was.

Bench governs what a model proposes through the tools it hooks, not everything
that can reach a branch.

Bench has repeatedly vetoed changes to its own governance pipeline under
constraint C-007 (governance pipeline integrity). Ledger entry #64 is a clean
example: a change to `pipeline/constitution.py` left a literal placeholder token
in the file, which would have been a `SyntaxError` breaking every pipeline
import and disabling enforcement outright. The Oracle caught it, named it, and
blocked it. It was corrected and re-submitted.

Other C-007 vetoes in this chain: #22–#25 (`bench.json`), #28
(`ledger/chain.py`), #54 and #56 (`utils/api.py`), #77 (`pipeline/runner.py`).
Read any of them with `python -m cli ledger`.

One note on the numbering, so it is not mysterious. This chain opens with an
`ANCHOR` entry rather than an ordinary verdict. A predecessor chain was retired
on 2026-07-24 and archived without being published, because a globally
registered hook had written diffs from unrelated projects into Bench's own
ledger and that chain contained third-party source. Retiring it whole was chosen
over editing entries, which C-008 forbids without exception. Entry numbers in
this README refer to the current published chain. See
[Chain Retirement](#chain-retirement) for when this is permitted, and run
`python -m cli audit-retirement` to confirm that retirement yourself.

Run `python -m cli verify` to confirm the ledger's integrity.
Run `python -m cli stats` to see the full governance history.

## Quick Start

```bash
# Clone
git clone https://github.com/Nuralyn/bench.git
cd bench

# Install
pip install -r requirements.txt

# Pick how Bench reaches the models (see "Provider Configuration" below):
#   Option A: use your own Anthropic API key
export ANTHROPIC_API_KEY=your-key-here
#   Option B: use your existing Claude Code subscription instead (no API key)
# export BENCH_PROVIDER=claude_code

# Add Bench hooks to your Claude Code project
# Copy the TEMPLATE, not Bench's own .claude/settings.json: that file registers
# the hook by a Bench-relative path, which only resolves when the working
# directory is the Bench repo itself.
cp .claude/settings.template.json /your-project/.claude/settings.json
# Then edit the copied file and replace /absolute/path/to/bench with the
# absolute path to this checkout. Claude Code runs the hook with your project
# as the working directory, so a relative path leaves python unable to find the
# script. The hook then emits no JSON, and Bench fails closed — blocking every
# Write/Edit/MultiEdit in that project until the path is corrected.

# Add your project's own rules as a layer
# Bench's core constitution always applies. Put rules specific to THIS project
# in <project>/bench.json, which is stacked on top of the core: use the
# reserved P- namespace for new constraints, and severity_overrides to make a
# core constraint stricter. A layer can add and tighten, never weaken.
# See "Per-Project Constitutions" below for the full schema and rules.
cat > /your-project/bench.json <<'JSON'
{
  "constitution": "myproject-v1",
  "version": 1,
  "constraints": [
    {
      "id": "P-001",
      "name": "No Raw SQL",
      "rule": "Use the query builder.",
      "severity": "veto"
    }
  ],
  "severity_overrides": { "C-005": "veto" }
}
JSON

# Edit this checkout's bench.json only for rules that should bind EVERY project
# you govern. It is the shared floor, not the place for one project's rules.

# Keep the ledger out of git BEFORE your first governed edit
# The ledger stores the full diff of every change it governs, so committing it
# publishes them (see "Project-Scoped Ledger" below). Anchor the pattern with a
# leading slash: bare `.bench/` would match any directory of that name at any
# depth in the project.
echo '/.bench/' >> /your-project/.gitignore

# Verify governance
python -m cli verify
python -m cli stats
```

## Provider Configuration

Bench defaults to the Anthropic API (`ANTHROPIC_API_KEY`). Two alternative backends are selectable via the `BENCH_PROVIDER` environment variable: OpenRouter, and `claude_code`, which routes every stage through your existing Claude Code subscription so no separate API key is needed.

```bash
# Default (Anthropic direct, uses ANTHROPIC_API_KEY)
export BENCH_PROVIDER=anthropic

# OpenRouter
export BENCH_PROVIDER=openrouter
export OPENROUTER_API_KEY=your-key-here

# Claude Code subscription (no API key — uses your logged-in `claude` CLI)
export BENCH_PROVIDER=claude_code
```

When using OpenRouter, the same model roles apply (Challenger, Defender, Oracle). Only the routing changes.

### Using your Claude Code subscription (`claude_code`)

Set `BENCH_PROVIDER=claude_code` to run the pipeline on the subscription that already powers your Claude Code session, with no `ANTHROPIC_API_KEY`. Each stage is dispatched through `claude -p` (headless mode), which inherits your logged-in session's auth. Requirements and tradeoffs:

- The `claude` CLI must be installed and logged in (it is, if you run Claude Code).
- Higher per-edit latency: every stage cold-starts a `claude` invocation, so a governed edit is noticeably slower than the direct-API path. Tune the per-stage timeout with `BENCH_CLAUDE_TIMEOUT` (seconds, default 120).
- This is the sanctioned subprocess route, not raw token reuse. Bench sets `BENCH_SUBPROCESS=1` on the child so its own hook does not recurse.

## Design Decisions

### Fail-Closed by Design

Bench always exits with code 0. Flow control uses JSON `permissionDecision` fields (`"allow"` or `"deny"`), never exit codes. If the governance pipeline cannot adjudicate a change (API timeout, malformed response, unimportable pipeline, unreadable constitution), the change is **denied**, with a stderr warning and a `pipeline_error` VETO recorded in the ledger, rather than allowed through. A broken or exploited judge must not be able to wave changes past governance, so governance is a wall when it cannot render a verdict, not a gate that swings open on failure. Recovery from a genuinely broken pipeline is an out-of-band human action (editing files directly, outside the governed tools), never an automatic pass. The lone exception is the reentrancy guard that lets a Bench-spawned governance subprocess through, so the pipeline does not recurse into itself and deadlock.

### Diff Hardening

Not all tool inputs are simple text edits. Bench handles three edge cases:

- **Binary files** (images, compiled output) are detected via null-byte sniffing and passed through with metadata only. The pipeline does not attempt to reason about binary content.
- **Large diffs** exceeding 300 lines are truncated while preserving governance-critical lines: imports, function/class signatures, and exception handlers.
- **New file creation** is typed as `change_type: "create"` so the pipeline knows it is reviewing a creation, not a modification.

### Project-Scoped Ledger

Bench's hook can be registered globally in `~/.claude/settings.json`, which governs every project on the machine. Each project's verdicts land in that project's own ledger, not in Bench's:

| Working directory | Ledger |
|---|---|
| Inside the Bench repo | `ledger/bench-ledger.json` (Bench governing itself) |
| Any other project | `<project>/.bench/bench-ledger.json` |
| `BENCH_LEDGER_PATH` set | That path, overriding both |

`ledger-meta.json` is written alongside whichever ledger is selected, so every chain carries its own anchor and verifies independently. `python -m cli verify` prints which ledger it read, and validates that chain only: per-project chains under `.bench/` are verified by running the command from that project.

This matters because a ledger records the full diff of every change it governs. Routing all projects into one chain mixes unrelated codebases together, and if that chain is committed to a public repository, it publishes them. Set `BENCH_LEDGER_PATH` if you deliberately want one central ledger across projects.

The corollary is worth stating: keeping a ledger out of git also removes git as its backup path. An ignored `.bench/` chain exists on one machine and nowhere else, so losing that working copy loses the audit trail with it. Decide deliberately which you want: a private chain you back up by other means, or a committed one that publishes the diffs it records.

### Ledger Format

The ledger is stored in two segments, and the split is what lets two branches record verdicts independently.

`bench-ledger.json` is the **frozen** legacy segment: a hash-chained JSON array that every reader still reads and nothing writes. `ledger-meta.json` is frozen with it, as a permanent pin on that segment's tip hash and entry count. Every new entry is written to its own file at `<ledger_dir>/entries/<entry_hash>.json`.

That shape exists because the original single array was rewritten in full on every append. Two branches appending produced divergent chains with no valid resolution: interleaving breaks the hash links, and rebasing rewrites hashes, which C-008 forbids. The array was frozen rather than migrated for the same reason — C-008 permits no moving or rewriting of an existing entry, and a file that is never written can never conflict.

`previous_hash` therefore holds either a string (legacy entries, one parent) or a sorted list of parent hashes. An append names **every current tip**, so after a `git merge` leaves two heads, the next governed edit names both and the fork reconciles itself. There is no merge command: the reconciliation is an ordinary governed, hash-linked entry.

Verification walks the union. The legacy array keeps its original positional check at full strength, and the entries directory is enumerated separately, failing closed on a parent that resolves to nothing (`MISSING_PARENT`), an entry unreachable from genesis (`ORPHAN_ENTRY`), a repeated hash (`DUPLICATE_ENTRY`), a filename that disagrees with the hash inside it (`FILENAME_MISMATCH`), and more than one genesis (`MULTIPLE_GENESIS`). `python -m cli verify` reports every tip when a chain is forked.

One tradeoff is worth naming rather than burying. The new segment has no in-band global entry count — a per-append count cannot be both conflict-free and append-only across branches. Deleting any non-tip entry file is a hard verification failure, because its children reference a parent that no longer resolves. Deleting a *tip* file is visible in `git diff` but not in-band, which is the same exposure the array always had: truncating it was only caught if the attacker forgot to also edit the committed meta file.

### Chain Retirement

C-008 forbids editing, reordering, or removing an individual ledger entry under
all circumstances. It permits exactly one escape, and only for one trigger:

> the sole permitted trigger is that the chain contains content which must not
> be published

That is narrower than people expect. A storage-format change is **not** such a
trigger. When the ledger moved to the per-entry DAG format above, retirement was
considered as a migration path and rejected for exactly this reason; freezing the
array was the correct answer instead. Reaching for retirement as a
general-purpose "start fresh" tool is a C-008 violation.

Retirement is not deletion. Every entry is preserved in an archive that is
verified **before** anything is removed, and a successor chain opens with an
`ANCHOR` entry recording where the predecessor went, so custody is documented
rather than broken.

```bash
python -m cli retire --archive-dir /path/outside/the/repo \
                     --reason "why this chain contains unpublishable content"
```

The command refuses unless every C-008 element is satisfied. It refuses a chain
that does not verify, an empty chain, and a forked chain (C-008 records a single
predecessor tip hash, so reconcile the fork with one governed edit first). It
refuses an archive destination that already exists, because an archive is never
overwritten, and one inside the chain being retired.

**It cannot be run from inside a Claude Code session**, including via a `!`
command. C-008(a) requires a decision that is never automated or agent-initiated,
so the gate refuses when stdin is not a TTY and when `BENCH_SUBPROCESS`,
`CLAUDECODE`, or `CI` are set, and it requires a confirmation phrase typed
verbatim. Use a plain terminal. Be honest about what this buys: it raises the
bar, it is not cryptographic proof, and what an auditor actually relies on is the
`human_decision` attestation recorded in the anchor.

Retire when nothing else is writing to the ledger. Reading, archiving, and
removing a chain are not one atomic step, so a governed edit that lands mid
retirement would otherwise have its receipt deleted without ever reaching the
archive. Retirement moves the chain aside rather than deleting it and re-checks
it against the archive before committing, so that case is refused and the chain
is restored exactly as it was, but the retry is yours to run.

The archive is a directory holding whichever segments existed. A first
retirement archives the frozen array, its meta pin, and the entries directory; a
later one archives only `entries/`, because a chain that retirement created has
nothing else. That is also the shape retirement leaves behind: the successor
chain is `entries/` alone, with no `bench-ledger.json` and no `ledger-meta.json`,
which verifies clean.

Confirm any retirement independently. This is C-008's auditor check, run against
the archive rather than described in prose:

```bash
python -m cli audit-retirement
```

It re-verifies the archived chain and confirms its tip hash and entry count equal
the values the anchor recorded. Run against this repository it confirms the
2026-07-24 retirement: 2,471 entries, tip `2176516f`.

### Per-Project Constitutions

Bench's own constitution is a floor, not a default to be replaced. A governed
project can add constraints of its own on top of it, and can make an existing
constraint stricter, but it cannot weaken what the core already requires.

| Working directory | Constitution |
|---|---|
| Inside the Bench repo | `bench.json` (core only — it *is* Bench's constitution) |
| Any other project | core + `<project>/bench.json` when that file exists |
| `BENCH_CONSTITUTION_PATH` set | core + that file |

A project layer looks like this:

```json
{
  "constitution": "myproject-v1",
  "version": 1,
  "constraints": [
    {
      "id": "P-001",
      "name": "No Raw SQL",
      "rule": "Use the query builder.",
      "severity": "veto"
    }
  ],
  "severity_overrides": { "C-005": "veto" }
}
```

Project constraints live in the reserved `P-` namespace; `C-` belongs to the
core and cannot be redefined. `severity_overrides` may only move a core
constraint *up* the scale. Removing a core constraint, downgrading one,
restating a severity unchanged, reusing a `C-` id, or overriding an id the core
does not define are all errors, and they raise rather than being ignored — a
silently dropped line would leave you believing you had changed a rule that is
still in force.

The two failure modes differ on purpose. **No layer** is safe: the core floor
applies in full, which is why this design is preferable to one where a project
file replaces the constitution and an absent file silently downgrades it. **A
malformed or hostile layer** fails closed, because the author clearly intended
additional constraints and Bench cannot tell which.

Each ledger entry records `constitution_sources` — the layer, path, and raw hash
of every file that ruled. The recorded hash chains those raw hashes rather than
digesting a re-serialization, so it still reflects authored bytes. Run
`python -m cli constitution` to see the merged result, its sources, and which
constraints the project added or raised.

One consequence worth stating plainly: if you already run Bench's hook globally,
a `bench.json` sitting in an unrelated project root is now a constitution layer
where it was previously inert. The floor makes that safe — a layer can only add
— but it is not a no-op, and it will show up in that project's ledger entries.

### Declaring Scope Where the Pipeline Can See It

C-002 judges whether a change stays inside its intended boundary. It is worth
knowing what the pipeline actually has to judge that against.

The PreToolUse payload carries the tool name and the tool's input. It does not
carry your prompt. Intent that exists only in the conversation is invisible to
the Challenger, the Defender, and the Oracle — they see a diff and a
constitution, and nothing about why you asked for it. On ordinary code changes
that is usually enough, because the reason is legible in the change itself. On
config files, dotfiles, and comment-less JSON there is often nothing to read.

The remedy is to put the justification in the repository: declare provenance,
the task boundary, and — for anything that adds a tool surface — what you
audited and what you found. Your project's `CLAUDE.md` is the natural home.

`pipeline/runner.py` reads that file once per run — alongside the constitution
snapshot, so every stage judges against the same evidence — and passes it to the
Challenger, the Defender, and the Oracle alike, on every provider.

This used to depend on the backend. On `BENCH_PROVIDER=claude_code` each stage
is a `claude -p` subprocess that inherits the governed project as its working
directory, so Claude Code loaded that project's `CLAUDE.md` for free, while the
`anthropic` and `openrouter` paths had no subprocess and saw only the diff and
the constitution. Identical changes could be judged against different evidence
because of a transport choice. The runner now supplies it explicitly, so
governance does not vary by provider.

Two properties worth knowing. The content is passed to the judge framed as
untrusted repository input that cannot waive, weaken, or add constraints — your
`CLAUDE.md` informs how scope is judged, it does not amend the constitution.
And it is capped, with the framing always ahead of the content, so an oversized
or hostile file is truncated rather than allowed to crowd out its own framing.
If the file exists but cannot be read, the failure is reported to stderr and
adjudication continues without it, so an unreadable `CLAUDE.md` cannot become a
denial vector.

This is not a workaround so much as the better outcome. In a project governed by
Bench, an `.mcp.json` that had been vetoed passed on resubmission after exactly
this, with no change to the config file itself. The justification stopped being
ephemeral prompt text and became something a reviewer can read six months later,
which is what a governance record is for.

## Models

| Role       | Constant (in `utils/api.py`) | Current model    | Purpose                     |
|------------|------------------------------|------------------|-----------------------------|
| Challenger | `CHALLENGER_MODEL`           | Claude Sonnet 5  | Adversarial analysis        |
| Defender   | `DEFENDER_MODEL`             | Claude Sonnet 5  | Soundness argument          |
| Oracle     | `ORACLE_MODEL`               | Claude Opus 4.8  | Binding verdict             |
| Utility    | `UTILITY_MODEL`              | Claude Haiku 4.5 | Reserved for future summarization (formatting is currently stdlib-only) |

`utils/api.py` is the single source of truth for model IDs. The "Current model"
column is an illustrative snapshot for readers, not an authoritative record;
refresh it when you change a model. The exact IDs live in the constants named
above.

With `BENCH_PROVIDER=claude_code` (set by the repo's `.claude/settings.json`),
the local Claude Code CLI must be recent enough to recognize these IDs: Claude
Sonnet 5 needs Claude Code v2.1.197+ and Claude Opus 4.8 needs v2.1.154+ (per
Claude Code's model-config docs; run `claude update` to upgrade). An older CLI
fails the stage, and under the runner's fail-closed policy that blocks the
change (a flagged `pipeline_error` VETO) until the pipeline can run, so keep the
CLI current.

## Built With

- Python 3.11+
- Anthropic API (Claude model family)
- Claude Code hooks (PreToolUse)
- SHA-256 hash chaining

## Author

Dana Burks / Nuralyn LLC

## License

MIT License. See [LICENSE](LICENSE).
