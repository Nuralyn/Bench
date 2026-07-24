# Security Policy

Bench is a constitutional governance layer for Claude Code. It sits between a
model's proposed file change and your filesystem, and it keeps a hash-chained
record of every verdict. A vulnerability in Bench is therefore usually one of
two things: a way to get a change past governance without a verdict, or a way
to make the ledger lie about what happened.

## Supported Versions

| Version | Supported                                  |
|---------|--------------------------------------------|
| 1.1.x   | Yes. Security fixes land here.             |
| 1.0.x   | No. Upgrade to 1.1.x.                      |
| `main`  | Yes, best effort. Fixes land here first.   |

## Reporting a Vulnerability

Do not open a public issue for a suspected vulnerability.

1. **Preferred:** open a private report at
   https://github.com/Nuralyn/Bench/security/advisories/new
2. **Alternative:** email dburks@nuralyn.com with `[bench-security]` in the
   subject line.

Please include:

- The version or commit SHA you tested.
- Which provider was configured (`BENCH_PROVIDER`: `anthropic`, `openrouter`,
  or `claude_code`).
- Reproduction steps, ideally a minimal diff or tool input that triggers the
  behavior.
- What you believe the impact is (governance bypass, ledger forgery, secret
  exposure, and so on).
- A redacted ledger entry or stderr excerpt if one is relevant. Scrub API keys
  before sending.

What to expect:

- Acknowledgement within 3 business days.
- An initial assessment (accepted, needs more information, or out of scope)
  within 10 business days.
- A fix and a public advisory for accepted reports, with credit unless you ask
  otherwise.

Please allow 90 days before public disclosure, or less by mutual agreement if a
fix ships sooner. Bench is maintained by a single author, so timelines are best
effort rather than contractual.

## In Scope

These are the classes that matter most for this project:

- **Governance bypass.** Any tool input, path, or encoding that causes a
  Write/Edit/MultiEdit to reach disk without an adjudicated verdict, including
  abuse of the `BENCH_SUBPROCESS` reentrancy guard to mark arbitrary edits as
  pipeline-internal.
- **Fail-open regressions.** Any path where an API timeout, malformed judge
  response, unimportable pipeline module, or unreadable constitution yields
  `permissionDecision: "allow"` instead of `"deny"`. Bench is fail-closed by
  design; a fail-open path is a bug, not a convenience.
- **Ledger integrity.** Forging, reordering, rewriting, or deleting entries in
  a way that still passes `python -m cli verify`. Also: entries that record a
  constitution hash they were not actually judged under.
- **Prompt injection through governed content.** Text inside a diff or a file
  under review that steers the Challenger, Defender, or Oracle into a PASS, that
  suppresses findings, or that exfiltrates the constitution or surrounding
  context into a model response or ledger entry.
- **Constitution tampering.** Anything that lets the three stages of one
  pipeline run see different constitution versions, breaking the per-run
  snapshot guarantee.
- **Secret exposure.** `ANTHROPIC_API_KEY`, `OPENROUTER_API_KEY`, or session
  credentials leaking into ledger entries, stderr output, subprocess argument
  vectors, or the generated HTML viewer.
- **Local code execution** triggered by a crafted `bench.json`, a crafted
  ledger file, or a crafted hook payload.

## Out of Scope

- **Model judgment quality.** An Oracle that passes a change you consider bad,
  or vetoes one you consider fine, is not a vulnerability. Bench is adversarial
  probabilistic review with a binding verdict, not a proof system. Report it as
  a normal issue so the constitution or prompts can improve.
- **Attacks that require existing write access to your machine.** Someone who
  can already run a shell in your working tree can edit files outside the
  governed tool path. That is documented and intentional: it is the recovery
  route when the pipeline itself is broken.
- **Upstream vulnerabilities** in the Anthropic API, OpenRouter, the Claude Code
  CLI, or Python itself. Report those to their maintainers. Do report cases
  where Bench's specific use of them creates exposure that would not otherwise
  exist.
- **Dependency CVEs with no reachable call path** in Bench, and missing
  hardening with no demonstrated exploit. Both are welcome as normal issues.
- **Resource exhaustion against your own pipeline** by feeding it very large
  diffs. Bench truncates at 300 lines and is a local single-user tool.

## Security Model and Assumptions

Understanding these will save you time deciding whether a finding is in scope.

- **Local tool, local trust boundary.** Bench runs on a developer machine as a
  Claude Code hook. There is no server, no multi-tenant surface, and no network
  listener. The trust boundary is your machine and your API credentials.
- **Fail-closed, always exit 0.** The hook always exits with code 0 and controls
  flow through the JSON `permissionDecision` field. When the pipeline cannot
  render a verdict, the change is denied and a `pipeline_error` VETO is written
  to the ledger. The sole exception is the reentrancy guard for a
  Bench-spawned governance subprocess.
- **The ledger is tamper-evident, not tamper-proof.** Entries are not signed.
  Anyone with write access to `ledger/bench-ledger.json` can rewrite it; the
  SHA-256 chain makes that detectable through `python -m cli verify`, and
  committing the ledger to git gives you a second copy to compare against.
  There is no cryptographic proof of authorship today.
- **Governed content is sent to a model provider.** Every diff Bench adjudicates
  is transmitted to whichever backend `BENCH_PROVIDER` selects. Treat the
  contents of governed files accordingly.
- **Only the PreToolUse path is governed.** Direct shell commands, editors, and
  anything that does not route through Claude Code's Write/Edit/MultiEdit tools
  is outside Bench's reach by design. The recurring case worth stating plainly
  is a bot-authored dependency PR: Dependabot edits `requirements.txt` on
  GitHub, so it never touches the hook and merges without a verdict or a ledger
  entry. Read those diffs before merging and do not enable auto-merge. A report
  that Bench "failed to govern" such a change is not a vulnerability, because
  it was never in scope; a way to make an ordinary tool-path change *look* like
  one in order to skip adjudication would be.

## Credentials

Bench reads API keys from the environment only. No key is ever committed to this
repository, and constitutional constraint C-006 vetoes hardcoded secrets in
governed changes. If you find a credential in this repository or its history,
report it through the private channels above rather than opening an issue.
