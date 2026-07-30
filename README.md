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

So read the boundary plainly: what Bench guarantees is that changes made through
the governed file tools were adjudicated and recorded — not that every change to
the repository was.

Bench governs what a model proposes through the tools it hooks, not everything
that can reach a branch.

During the build, Bench vetoed a change to its own governance pipeline code
under constraint C-007 (governance pipeline integrity). The change would have
reduced fallback coverage in the hook entry point. It was corrected and
re-submitted. Ledger entry #13 is the receipt.

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

# Customize your constitution
# The pipeline resolves the constitution to an absolute path inside THIS
# checkout (pipeline/runner.py `_CONSTITUTION_PATH`), so it reads the same
# bench.json no matter which project it is governing. Edit it here; copying a
# bench.json into the governed project has no effect today.
# Edit bench.json to add your own rules.

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

The corollary is worth stating: keeping a ledger out of git also removes git as its backup path. An ignored `.bench/` chain exists on one machine and nowhere else, so losing that working copy loses the audit trail with it, and a project governed from two machines accumulates two independent chains that cannot be merged — the hash chain admits no interleaving after the fact. Decide deliberately which you want: a private chain you back up by other means, or a committed one that publishes the diffs it records.

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
