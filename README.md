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

Run `python -m bench verify` to confirm the ledger's integrity.
Run `python -m bench stats` to see the full governance history.

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
python -m bench verify
python -m bench stats
```

## Models

| Role       | Model      | Purpose                     |
|------------|------------|-----------------------------|
| Challenger | Sonnet 4.6 | Adversarial analysis        |
| Defender   | Sonnet 4.6 | Soundness argument          |
| Oracle     | Opus 4.7   | Binding verdict             |
| Utility    | Haiku 4.5  | Diff summaries, formatting  |

## Built With

- Python 3.11+
- Anthropic API (Claude model family)
- Claude Code hooks (PreToolUse)
- SHA-256 hash chaining

## Author

Dana Burks / Nuralyn LLC

## License

MIT License. See [LICENSE](LICENSE).
