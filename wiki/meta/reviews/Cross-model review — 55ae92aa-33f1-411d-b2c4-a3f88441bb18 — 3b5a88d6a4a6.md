---
type: review
status: active
created: 2026-08-04
updated: 2026-08-04
tags: [review, harness]
sessions: []
review_id: "55ae92aa-33f1-411d-b2c4-a3f88441bb18"
address: "c-000104"
---

# Cross-model review — 55ae92aa-33f1-411d-b2c4-a3f88441bb18 — 3b5a88d6a4a6

Final verdict: `approve`.

## Bound evidence

- Operation: `55ae92aa-33f1-411d-b2c4-a3f88441bb18`
- Run: `2c494525617bcabce2542027aa6fff27`
- Mode: `deep`
- HEAD: `99c4658562e868c9659c6722631f21d1228fa37a`
- Verification profile: `scoped` (`7724ff1e1e5dd664008315264d34a55bd53d35d9d10b66d797c540841108df82`)

## Axis: anthropic-intent

- Verdict: `approve`
- Verification iteration: 1

### Findings

- **anthropic-intent:intent-v1-exact-head-receipt-stale · minor · No typed content-free receipt exists for reviewed HEAD 99c4658; the newest .json receipt is for 1d437d7f, which the ledger's own rule invalidates. Only three untyped .log files cover the current HEAD.**
  - File: `docs/acceptance/v2.6.3-release-readiness.md:8`
  - Evidence: Ledger line 8 states the receipt '.vault-meta/release-evidence/v2.6.3-<short-head>.json' records the full 40-character HEAD, every gate command and exit code, and lines 9-11 state it is generated only after HEAD freezes and that any later commit invalidates it and requires a new exact-HEAD run. Plan Slice 11 requires the same receipt and a readiness ledger that references it. Directory listing at reviewed HEAD: v2.6.3-1d437d7f4804.json (two commits stale, superseded by 9abbe6c and 99c4658), v2.6.3-c880c1d1f950.json, v2.6.3-f431e0959c52.json, plus v2.6.3-99c4658562e8-make-test.log, -harness-coverage.log and -codex-sync.log. No v2.6.3-99c4658562e8.json. Ledger line 133-137 says the logs sit 'beside the content-free receipt', which does not currently exist for this HEAD, so the 13-command exit-code packet has no typed exact-HEAD carrier and the only machine receipt on disk points at superseded bytes. I inspected the three logs rather than trusting them: make-test.log (3,785 lines) ends 'All tests passed.' with no 'make: ***' line; harness-coverage.log reports 76.34% across 123 modules with each listed module above its floor and 4,370 transition matrix cases; codex-sync.log reads 'codex-sync: no changes'. They are consistent, but they remain implementer artifacts with no HEAD stamp inside the file body.
  - Recommendation: Regenerate the content-free receipt for the exact frozen HEAD (v2.6.3-99c4658562e8.json) with the full 40-character SHA, all 13 gate commands and their exit codes before terminal release approval, and either delete or explicitly mark the superseded 1d437d7f receipt so no reader treats it as current release evidence.
- **anthropic-intent:intent-v1-handbook-silent-on-cancel-semantics · minor · This release changes the terminal result of harness-cli cancel/close for operations holding an accepted callback, and adds callback-complete to reconcile output, but the Russian handbook still teaches cancel only as stopping the operation.**
  - File: `docs/ru/sessions-and-tasks.md:28`
  - Evidence: scripts/harness/cli.py:329-337 sets terminal_state to 'complete' when record.state == 'exiting' and accepted_callback_id/kind/sha256 are all present, otherwise 'cancelled'; lines 584 and 616 emit action 'callback-complete' from reconcile. tests/harness/test_store.py now pins this for the explicit verb ('CLI cancel preserves an accepted callback as completion'), which I ran green at this HEAD. The readiness ledger (lines 162-168 at HEAD) and both changelogs now disclose this exact CLI behavior change, satisfying the engineering-lane disclosure. The user-facing handbook does not: docs/ru/sessions-and-tasks.md:28-29 says 'cancel останавливает operation, close завершает разрешённую lifecycle surface'; docs/ru/reference/commands.md:36 says 'Cancel exact operation'; docs/ru/documents-and-research.md:52 says only 'обычный cancel остаётся cancelled', which is true but does not tell a reader that cancel on an accepted-callback operation ends as complete. E2 and E4 promise the handbook teaches sessions, dispatch, review and recovery for this release, and the plan forbids documenting a command whose behavior is not confirmed by help/test/authoritative source; here the confirmed behavior is broader than the documented behavior. No factual statement in the handbook is wrong, so this is a completeness gap, not an inaccuracy.
  - Recommendation: Add one sentence to docs/ru/sessions-and-tasks.md (and a note in the reference/commands.md cancel row) stating that an operation whose durable callback was already accepted terminates as complete rather than cancelled during owned cleanup, and that reconcile reports callback-complete for that case, linking the release notes as authority.
- **anthropic-intent:intent-v1-nongoal4-standing-runtime-exceptions · minor · Non-goal 4 allows exactly one runtime exception; the release still carries four disclosed repairs plus this round's harness refactor. All are owner-authorized and now fully disclosed, so the crossing stands as a recorded amendment rather than hidden drift.**
  - File: `scripts/harness/workflows/research_contracts.py:131`
  - Evidence: New at this HEAD: research_contracts.py:131 fetch_callback_payload() and :148 research_callback_identity() centralize the durable research callback payload and identity, now consumed by scripts/harness/runtime_worker_custom.py (producer), workflows/research.py (recovery matcher) and tests/harness/test_research_vertical.py; the helpers add fail-closed validation of the artifact digest, source count and stage. This is de-duplication inside the already-authorized research callback repair, and I verified it behaviour-safe at the outcome level by running test_research_vertical.py, test_runtime_research.py, test_callbacks.py, test_store.py, test_workflows.py, test_custom_pipelines.py, test_task_review_mechanism_recovery.py, test_task_review_flow_units.py, test_review_gate.py, test_runtime_sessions.py and 29 further harness suites (0 failing), plus make acceptance-check ('release-acceptance: 4 harness cells valid at 99c4658562e868c9659c6722631f21d1228fa37a'), make test-docs, test-model-routing, test-code-quality, test-instruction-lint, test-skill-budget, test-outcome-contract, test-release-acceptance, test-task-sessions, test-research-isolation, test-contract-schemas, test-task-lifecycle, codex-adapter --check, validate-vault --summary, verify_snapshots, and a clean git diff --check v2.6.2..HEAD. Standing crossings against non-goal 4 and E7's 'кроме одного узкого regression-covered исправления': accepted-callback cleanup generalization (in-plan per the Slice 0.5 refactor seam), MCP default runtime.env bootstrap (coordinator record c-000103, page SHA 872e7a80… verified byte-exact), quiescent mixed-HEAD review recovery plus the legacy-round adapter (user decision recorded as c-000101), and now the two-step recovery/callback module restructuring, disclosed at readiness lines 98-110 and release notes lines 115-122. Ceilings and registries remain untouched: pipeline_builtins.py and custom_pipeline_contracts.py appear nowhere in the 62-path v2.6.2..HEAD range, and skill inventory stays 34 at 7,496/7,500 bytes.
  - Recommendation: No product change requested. Keep the amendment records (c-000101, c-000103) and the ledger disclosure attached to the final release evidence so the four-exception scope is read as an authorized boundary amendment, and hold any further runtime work for 2.6.4 as c-000101 requires.

## Axis: anthropic-engineering

- Verdict: `approve`
- Verification iteration: 1

### Findings

- None

## Verification gaps

- None

## Residual risks

- None

## Notes for executor

- None

## Executor resolutions

### anthropic-engineering · `1d437d7f48046acf82aa5cec16187ef83b259617` → `99c4658562e868c9659c6722631f21d1228fa37a`

- Fix delta SHA-256: `6185fbd7b2a35979b0db7faf6456119edce0bca2add8e765788ef8b8e7bb7c25`
- **anthropic-engineering:ENG-RELEASE-LEDGER-RUNTIME-DISCLOSURE · applied**
  - Rationale: The readiness ledger, release notes, and both changelogs now disclose commits 24189b44 and 1d437d7 as behavior-preserving review-recovery module restructuring instead of documentation-only work. They also disclose the accepted-callback terminal-state and callback-complete reconcile vocabulary change. Follow-up commit 9abbe6c adds the requested cancel characterization test, centralizes research callback identity, and binds the documentation context-pointer digest; commit 99c4658 records the exact release evidence. Full exact-HEAD tests and coverage are green with executor stdout/stderr retained under .vault-meta/release-evidence.
### anthropic-intent · `1d437d7f48046acf82aa5cec16187ef83b259617` → `99c4658562e868c9659c6722631f21d1228fa37a`

- Fix delta SHA-256: `6185fbd7b2a35979b0db7faf6456119edce0bca2add8e765788ef8b8e7bb7c25`
- No material findings on this independent axis

## Archive boundary

Raw prompts, transcripts, commands, sockets, and cmux/process identifiers are excluded.
