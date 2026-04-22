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

This tool was built under its own governance. Every change in this codebase
was challenged, defended, ruled on, and recorded by Bench itself.

During the build, Bench vetoed a change to its own governance pipeline code
under constraint C-007 (governance pipeline integrity). The change would have
reduced fallback coverage in the hook entry point. It was corrected and
re-submitted. Ledger entry #8 is the receipt.

Run `python -m cli verify` to confirm the ledger's integrity.
Run `python -m cli stats` to see the full governance history.

## Quick Start

```bash
# Clone
git clone https://github.com/Nuralyn/bench.git
cd bench

# Install
pip install -r requirements.txt

# Set your API key
export ANTHROPIC_API_KEY=your-key-here

# Add Bench hooks to your Claude Code project
cp settings.json /your-project/.claude/settings.json

# Add your constitution
cp bench.json /your-project/bench.json

# Customize your constraints
# Edit bench.json to add your own rules

# Verify governance
python -m cli verify
python -m cli stats
```

## Provider Configuration

Bench defaults to the Anthropic API. To route through OpenRouter instead, set the `BENCH_PROVIDER` environment variable:

```bash
# Default (Anthropic direct)
export BENCH_PROVIDER=anthropic

# OpenRouter
export BENCH_PROVIDER=openrouter
export OPENROUTER_API_KEY=your-key-here
```

When using OpenRouter, the same model roles apply (Challenger, Defender, Oracle). Only the routing changes.

## Design Decisions

### Fail-Open by Design

Bench always exits with code 0. Flow control uses JSON `permissionDecision` fields (`"allow"` or `"deny"`), never exit codes. If the governance pipeline encounters an error (API timeout, malformed response, import failure), the change is allowed through with a stderr warning. This prevents Bench from becoming a blocker that stalls Claude Code on infrastructure failures. Governance should be a gate, not a wall.

### Diff Hardening

Not all tool inputs are simple text edits. Bench handles three edge cases:

- **Binary files** (images, compiled output) are detected via null-byte sniffing and passed through with metadata only. The pipeline does not attempt to reason about binary content.
- **Large diffs** exceeding 300 lines are truncated while preserving governance-critical lines: imports, function/class signatures, and exception handlers.
- **New file creation** is typed as `change_type: "create"` so the pipeline knows it is reviewing a creation, not a modification.

## Models

| Role       | Model      | Purpose                     |
|------------|------------|-----------------------------|
| Challenger | Sonnet 4.6 | Adversarial analysis        |
| Defender   | Sonnet 4.6 | Soundness argument          |
| Oracle     | Opus 4.7   | Binding verdict             |
| Utility    | Haiku 4.5  | Reserved for future summarization (formatting is currently stdlib-only) |

## Built With

- Python 3.11+
- Anthropic API (Claude model family)
- Claude Code hooks (PreToolUse)
- SHA-256 hash chaining

## Author

Dana Burks / Nuralyn LLC

## License

MIT License. See [LICENSE](LICENSE).
