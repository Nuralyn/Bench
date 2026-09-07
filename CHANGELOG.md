# Changelog

All notable changes to Bench. The format follows Keep a Changelog, and the
project follows semantic versioning. Dates are the merge dates of the release
tags. Every change listed here landed through Bench's own governance pipeline
and carries a receipt in the operational ledger.

## [2.1.0] - unreleased

The theme is making the claim survive a skeptic: cheaper verdicts, a real
installer, honest numbers, and tests for the properties that used to live in
comments.

### Added

- `pip install` gives a `bench` command; `bench install --project PATH` writes
  the hook settings with an absolute path, appends `/.bench/` to the project's
  gitignore, and sets the commit guard; `bench uninstall` reverses exactly what
  install wrote, from a receipt it keeps at `.bench/install.json`.
- Per-stage wall time recorded beside token counts in every entry; the
  dashboard shows median and p90 latency per week and the README quotes the
  ledger's own median tokens and seconds per edit.
- Prompt caching of the constitution and repository context on the anthropic
  provider; the claude_code provider lifts the constitution into its system
  prompt file so the CLI reads it back from cache. Cache reads are priced at
  the cached rate in `cli stats` and the viewer.
- A recorded model override per stage: `BENCH_CHALLENGER_MODEL`,
  `BENCH_DEFENDER_MODEL`, and `BENCH_ORACLE_MODEL`, with the model actually
  used written into each stage's record and filterable on the dashboard.
- A derived tip cache beside `entries/` so an append reads and validates only
  the entries it links to. Never authoritative: the auditor ignores it, and a
  stale, missing, or doubtful cache falls back to the full scan and rebuilds.
- An append lock so two sessions on one machine cannot fork the chain, held by
  retirement across its whole mutation window. Appends wait as long as the
  holder lives; retirement's own acquisition is bounded and refuses loudly.
- A ruff and strict mypy gate in CI over every source package and the hook.
- Tests that run the hook as a real subprocess and pin its host contract:
  exact allow and deny JSON on stdout, exit code zero always.
- Tests that a prompt-injected diff cannot move the verdict: offline placement
  proofs for every stage and provider, and an opt-in live run that expects a
  VETO.
- A per-project constitution layer: a governed project's `bench.json` may add
  `P-` constraints and raise core severities, never remove or redefine a core
  constraint; `cli constitution` prints the merged result and its sources.
- Chain retirement under C-008's single bounded exception, with archive
  verification before removal, an anchor entry opening the successor, and
  `cli audit-retirement`.
- Published-copy sanitation records and their audit, for the case where a
  chain's content must be removed from copies that retirement cannot reach.
- A dashboard in the viewer: weekly veto and pipeline-error rates, verdicts by
  C-007 scope, constraint citations, token cost, and seconds per stage, all
  computed by the same helpers `cli stats` uses.
- A migration command for clones that predate the private ledger location.

### Changed

- The operational ledger lives at `<project>/.bench/` for every governed
  project, Bench included, and is never a tracked artifact: an entry records
  the full diff body of the change it governs. The legacy array segment is
  frozen and new entries are written one per file, so two branches that both
  appended merge as a union and the next governed edit reconciles the fork.
- Constitution v7 splits each constraint into the rule the models read and
  commentary they do not; prompts carry the rule only.
- Cosmetic drift in judge output (an empty remediation on PASS, a blank
  advisory, a severity written as WARNING, a finding index written as a
  string) is repaired and recorded on the entry instead of failing closed. A
  missing or unknown verdict, or a citation outside the constitution, still
  fails closed.
- The subprocess bypass is bound to a per-call nonce the provider records on
  disk; a bare `BENCH_SUBPROCESS=1` or a guessed token is governed normally.
- Every subprocess call carries a timeout, enforced by a test that scans the
  source tree.
- The CLI parses arguments with argparse; `--help` lists every command and its
  flags, and a bad flag is a usage error rather than silence.
- Diffs are bounded by a character budget as well as a line budget, so a few
  very long lines cannot carry an unbounded payload to the models.
- The Oracle's per-stage timeout in this repo's settings is 300 seconds, so a
  slow judge on a constitutionally heavy diff is not mistaken for a strict one.
- Path normalization has one implementation in `utils.diff`, one deliberate
  fallback copy in the hook pinned equal to it by a test, and the dead
  path-traversal placeholder is gone.
- The viewer file is created owner-only from its first byte on Windows as well
  as POSIX.
- README statements that had drifted from the code were corrected and pinned
  by tests.
- Docstrings and comments describe what the code does now. Release history
  lives in this file; the one date that remains in source names the frozen
  anchor entry whose shape the retirement auditor must keep reading.

## [2.0.0] - 2026-07-24

### Added

- Global governance: the hook can be registered in the user's settings and
  govern every project on the machine, with verdicts routed to the ledger of
  the project being governed.
- `SECURITY.md` and a documented governance boundary: files written through
  Bash or MCP tools never reach the hook and carry no verdict.

### Changed

- Governance fails closed. A pipeline error, a judge that cannot be reached, or
  a response that cannot be validated denies the change instead of allowing
  it, and the ledger entry records the error as a pipeline error rather than
  a ruling.
- Stage models moved to Sonnet 5 for the Challenger and Defender and Opus 4.8
  for the Oracle, with `utils/api.py` the single source of truth for model ids.
- C-004 type safety tightened to the precision the constitution states.

## [1.1.0] - 2026-07-12

### Added

- The claude_code provider: stages run through the local `claude` CLI in
  headless mode on the user's subscription, with the stage system prompt kept
  at system priority over the untrusted diff and a reentrancy guard for the
  judge subprocess.
- A test suite and CI workflow across supported Python versions.
- Centralized governance logging.
- `utils/stats.py` as the single source of truth for ledger statistics, used
  by `cli stats` and the viewer alike.

### Changed

- The hook emits the documented `BENCH VETO [C-XXX]` reason format and surfaces
  Oracle advisories on PASS.
- Paths resolve against the repository root derived from the source file
  location, not the working directory, so in-repo edits from a subdirectory
  are governed as in-repo.
- The openai SDK is a soft dependency, installed only for the OpenRouter
  provider; the anthropic SDK has a pinned lower bound.

### Fixed

- Response parsing is guarded, silent catches log, and verification is
  anchored to the ledger meta pin.
- The viewer's temporary file is no longer removed before the browser loads it.
- Hardening against the findings of a security audit of the pipeline.

## [1.0.0] - 2026-04-21

### Added

- The governance pipeline: a PreToolUse hook intercepts Write, Edit, and
  MultiEdit, and a Challenger, a Defender, and an Oracle rule on the change
  before it lands. The Oracle's PASS or VETO is binding.
- The constitution, `bench.json`, with constraints C-001 through C-008 and a
  version field.
- The hash-chained, append-only ledger: every verdict is an entry whose hash
  covers its fields and links to the previous entry.
- The CLI: `verify`, `ledger`, `stats`, `constitution`, and `viewer`, a
  self-contained HTML browser for the ledger.
