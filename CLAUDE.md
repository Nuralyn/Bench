# CLAUDE.md - Bench

## What This Is

Bench is a constitutional governance layer for Claude Code. Every code change
Claude proposes passes through an adversarial brigade of models that challenge,
defend, and rule on it before a single line commits. Every verdict is
hash-chained into an auditable ledger. This is not a code review tool. This is
a judicial system for AI-generated code.

Bench governs itself. Every change to this codebase is subject to the same
governance pipeline. This is non-negotiable.

## Architecture

```
PreToolUse Hook -> Challenger (Sonnet 4.6) -> Defender (Sonnet 4.6) -> Oracle (Opus 4.7) -> Ledger
```

- Hook intercepts Write/Edit/MultiEdit tool calls
- Constitution snapshot loaded once per pipeline run (frozen within, hot-reload between)
- Oracle verdict is PASS or VETO. VETO is binding.
- Every verdict hashed and chained into bench-ledger.json
- On VETO: JSON permissionDecision "deny" with remediation feedback
- On PASS: JSON permissionDecision "allow"
- Exit code is ALWAYS 0. Flow control is via JSON, not exit codes.

## Project Structure

```
bench/
  bench.json              # Constitution file. User-editable. Versioned.
  settings.json           # Claude Code hook config
  hooks/
    pre-tool-use.py       # Hook entry point
  pipeline/
    challenger.py         # Adversarial analysis (Sonnet 4.6)
    defender.py           # Soundness argument (Sonnet 4.6)
    oracle.py             # Binding verdict (Opus 4.7)
    constitution.py       # Load, snapshot, hash
    runner.py             # Sequential orchestration
  ledger/
    chain.py              # Hash-chaining, append
    verify.py             # Independent chain validation
    bench-ledger.json     # Append-only ledger
    ledger-meta.json      # Metadata
  cli/
    __main__.py           # python -m bench
    commands.py           # verify, ledger, stats, constitution
  utils/
    diff.py               # Diff extraction and formatting
    api.py                # Anthropic API client
    formatting.py         # Haiku utility calls
  tests/
```

## Models

| Role       | Model      | Purpose                          |
|------------|------------|----------------------------------|
| Challenger | Sonnet 4.6 | Find problems in proposed change |
| Defender   | Sonnet 4.6 | Argue soundness of the change    |
| Oracle     | Opus 4.7   | Issue binding PASS or VETO       |
| Utility    | Haiku 4.5  | Diff summaries, formatting       |

Models are Anthropic by default. The wrapper in utils/api.py also supports
OpenRouter as a routing backend (selected via the BENCH_PROVIDER env var) so
the same Anthropic models can be reached through either path. Direct calls to
non-Anthropic model families remain out of scope.

## Rules

### Absolute

1. Every file change is governed. No exceptions. No bypasses.
2. The ledger is append-only. Never modify, delete, or overwrite entries.
3. The hash chain must remain intact. Every entry references the previous.
4. Constitution is loaded as a snapshot per pipeline run. All three stages
   see the same version. No mid-run constitution changes.
5. Oracle verdicts are binding. VETO means the change does not land.
6. Exit code from the hook is ALWAYS 0. Use JSON permissionDecision for
   flow control. Exit-2 causes Claude to stall.

### Code Standards

7. Python 3.11+. Type hints on all function signatures.
8. All API calls wrapped in try/except with typed error returns.
9. No silent error swallowing. Catch blocks must log, re-throw, or return
   a typed error. This is also constitutional constraint C-001.
10. No undeclared dependencies. Every import has a corresponding entry in
    requirements.txt.
11. JSON output from all pipeline stages. No free-form text responses.
12. All structured output validated before use. Parse failures retry once,
    then record as PIPELINE_ERROR in the ledger.

### Workflow

13. Do not run tests with `npm test` or `pytest` in bulk. Test specific
    files or functions only.
14. One change per tool call. Do not batch unrelated changes into a single
    Write/Edit operation.
15. If you modify bench.json (the constitution), increment the version field.
16. If you modify any file in pipeline/, ledger/, or hooks/, you are
    modifying the governance pipeline itself. Constraint C-007 applies.
    Be aware that Bench will scrutinize these changes.
17. Commit messages follow: `[bench] <component>: <what changed>`
    Examples: `[bench] oracle: add confidence scoring`
              `[bench] ledger: implement chain verification`
              `[bench] constitution: add C-009 logging constraint`

## Constitution Reference

The constitution lives in bench.json. Current constraints:

- **C-001**: No silent error swallowing (veto)
- **C-002**: Scope boundary enforcement (veto)
- **C-003**: Dependency declaration (veto)
- **C-004**: Type safety preservation (veto)
- **C-005**: Test coverage for new logic (warning)
- **C-006**: No hardcoded secrets (veto)
- **C-007**: Governance pipeline integrity (veto)
- **C-008**: Ledger immutability (veto)

## API Configuration

The LLM wrapper lives at `utils/api.py` and exposes a single
`call_model(model, system_prompt, user_content, max_tokens=4096) -> dict`
function. The provider is selected at call time by the `BENCH_PROVIDER`
environment variable; the function signature is identical for both backends.

```python
# Provider: anthropic (default if BENCH_PROVIDER is unset)
import anthropic
client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY

# Provider: openrouter (BENCH_PROVIDER=openrouter)
import openai
client = openai.OpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=os.environ["OPENROUTER_API_KEY"],
)
# Model strings are auto-prefixed with "anthropic/" on this path,
# e.g. "claude-sonnet-4-6" -> "anthropic/claude-sonnet-4-6".
# The openai SDK is a soft dependency — install it only if you set
# BENCH_PROVIDER=openrouter.

# Model strings (same on both providers; routing handles the prefix)
CHALLENGER_MODEL = "claude-sonnet-4-6"
DEFENDER_MODEL = "claude-sonnet-4-6"
ORACLE_MODEL = "claude-opus-4-7"
UTILITY_MODEL = "claude-haiku-4-5-20251001"
```

## Hook Response Format

### On PASS:
```json
{
  "hookSpecificOutput": {
    "hookEventName": "PreToolUse",
    "permissionDecision": "allow",
    "additionalContext": "Bench governance: PASS. All constraints satisfied."
  }
}
```

### On VETO:
```json
{
  "hookSpecificOutput": {
    "hookEventName": "PreToolUse",
    "permissionDecision": "deny",
    "permissionDecisionReason": "BENCH VETO [C-XXX]: ...",
    "additionalContext": "Remediation: ..."
  }
}
```

## What Success Looks Like

Bench builds Bench. Every change in this repo was challenged, defended, ruled
on, and recorded. The ledger is the proof. `python -m bench verify` confirms
the chain is intact. `python -m bench stats` shows the full governance history.

The thesis: governance of AI reasoning is a primitive, not a feature.
