---
type: review
title: "Cross-model review — LLM Obsidian 2.1.3 lifecycle fixes — 1ace771c7718"
address: c-000032
created: 2026-07-29
updated: 2026-07-29
tags:
  - review
  - cross-model
status: resolved
sessions:
  - "019fab00-3160-7380-8920-4b20183afb76"
review_id: "519fb4ec-4cf9-4a79-8686-22e627bd454b"
reviewer_runtime: "claude"
reviewer_model: "fable"
reviewer_effort: "high"
review_mode: "full"
rounds: 3
verdict: approve
---

# Cross-model review — LLM Obsidian 2.1.3 lifecycle fixes — 1ace771c7718

> [!abstract] Outcome
> **Task:** LLM Obsidian 2.1.3 lifecycle fixes
> **Final verdict:** `approve`
> **Reviewer:** claude · fable · effort `high`
> **Executor:** codex
> **Mode:** `full` · **rounds:** 3
> **Started:** 2026-07-29T10:12:12Z
> **Updated:** 2026-07-29T10:36:29Z

## Review request

Review the implementation for **LLM Obsidian 2.1.3 lifecycle fixes** in `main` against `origin/main` using the `full` cross-model gate.

> [!quote] Original task request
> Perform a final full, read-only cross-model code review of the public LLM Obsidian release candidate on branch `release/v2.0.7` against tag `v2.0.6`.
>
> Review the complete committed diff `v2.0.6..HEAD`, with particular attention to unattended task/reviewer permissions, exact cmux callback routing, DCG policy, escalation recovery, durable review archival, dense retrieval catch-up, agenda report naming, release metadata, portability, and regressions in clean public installations. Do not review private vault content or edit product files.
>
> Evidence already available: the full hermetic `make test` suite passed, `make bench-retrieval` passed with sparse hit@5 = 1.0, `git diff --check` passed, and the release branch was scanned for private repository names, user paths, runtime state, and credential-like leakage.
>
> Return only actionable findings grounded in file/line evidence. Distinguish blocking correctness/security defects from warnings or nits, state any verification gaps and residual risks, and approve when no changes are required.

## Round 1 — approve

- Phase: `initial-review`
- Run ID: `519fb4ec-4cf9-4a79-8686-22e627bd454b`
- Received: 2026-07-29T10:17:45Z

### Findings

#### 1. warning — Unbounded workspace-trust watcher widens the false-positive key-injection window to the whole agent lifetime

- File: `scripts/cmux\_agent\_supervisor.py:186`
- Evidence:
> WORKSPACE_TRUST_TIMEOUT_SECONDS is now None, so auto_accept_workspace_trust polls `cmux read-screen` every 0.1s until the agent exits (stop is set only in run_agent's finally block). The matchers are substring checks over a whitespace-compacted screen, and every marker string appears verbatim in this repository's own sources: scripts/cmux_trust_prompt.py:12-24 contains all four claude and all four codex workspace markers, and cmux_agent_supervisor.py:970-977 plus tests/test_task_lifecycle.py fixtures contain the full Claude MCP dialog markers. An unattended agent working on this repo (the dominant dogfooding task type) that cats, greps, or diffs those files can render all markers within the 80-line screen window and receive an injected Enter (or down/down/Enter for the MCP path) mid-task. Under the previous 120s bound this was practically unreachable; now the window is the entire run. Secondary cost: ~10 read-screen subprocess spawns per second for the full agent lifetime once the workspace prompt is handled (claude runtime keeps looping for the MCP prompt).
- Recommendation:
> Keep the watcher unbounded only until the runtime's first real interactive screen is observed (a cold start that has painted a non-trust interactive screen can no longer show the first-run dialog), or replace None with a generous finite bound (e.g. 10-15 minutes) that still fixes the slow-cold-start miss. Alternatively require the markers to persist across two consecutive polls and anchor them to the dialog's structure rather than bare substrings.

#### 2. nit — Checkpoint-capture branch still gates on \_STATE\_DIR instead of reviewer\_uses\_broker\_state

- File: `scripts/cmux\_surface\_lifecycle.py:295`
- Evidence:
> after_exit's checkpoint block at line 295 (`kind == "reviewer" and _STATE_DIR is not None`) now also runs for root-state coordinator reviews (--state-dir == worktree), but its only consumer, transition_broker_review at line 318, is correctly skipped by the new reviewer_uses_broker_state guard. In the root-state path this does dead capture_resume work against cmux and, on failure, prints the misleading "the next round will start fresh" degradation warning even though coordinator reviews have no broker-driven next round.
- Recommendation:
> Use reviewer_uses_broker_state(worktree) at line 295 as well, so root-state coordinator reviews skip checkpoint capture entirely.

### Executor resolution

> [!note] Resolution snapshot
> # Executor Resolution
>
> ## Finding 1 — workspace-trust watcher
>
> Classification: fixed.
>
> Replaced the unbounded watcher with a 30-minute startup window and reduced
> polling from 10 to 2 screen reads per second. This keeps the slow cold-start
> repair effective while removing lifetime polling and bounding the
> false-positive key-injection window. The lifecycle regression now asserts the
> bounded production values and still proves delayed prompt acceptance.
>
> ## Finding 2 — coordinator checkpoint capture
>
> Classification: fixed.
>
> Root-scoped coordinator reviews now skip resume checkpoint capture and the
> broker-only after-exit checkpoint branch. Legacy and real v3 broker review
> rounds retain checkpoint behavior. The root-state integration regression
> asserts that no `surface resume get` call occurs before the exact reviewer
> surface closes.
>
> ## Validation
>
> - `make test` — passed.
> - `tests/test_review_dispatch.sh` — 170 passed, 0 failed.
> - `tests/test_task_sessions.py` — 65 passed.
> - `scripts/lint-instructions.py` — passed.
> - Resolution commit: `aa11a79`.

### Verification gaps

- cmux `--id-format both` compatibility was verified only against test doubles (tests/test_task_sessions.py fixture); no live check that the currently installed cmux accepts the flag and that older CLIs fail with a non-zero exit rather than rc 0 with unexpected output, which the fallback in scripts/task_sessions.py:330-349 relies on.
- The delayed-startup trust regression in tests/test_task_lifecycle.py exercises timeout_seconds=0.2, not the shipped timeout_seconds=None loop-exit-via-stop-event path; that path is only covered indirectly by the constant assertion.

### Residual risks

- Unattended agents displaying this repo's trust-prompt marker strings on screen can receive an injected Enter at any point in their run (see warning finding); the failure is recoverable but can confuse or derail an unattended session.
- review_executor_surface now hard-requires a UUID-form CMUX_SURFACE_ID for spawned coordinator reviews (skills/review-dispatch/scripts/spawn_review.py:954-380); if a future cmux emits non-UUID surface IDs in that env var, coordinator reviews fail closed with a clear message rather than degrading.

### Notes for executor

- Verified locally: python3 tests/test_task_lifecycle.py, tests/test_task_sessions.py, tests/test_research_isolation.py, tests/test_live_acceptance_runner.py all pass; bash tests/test_review_dispatch.sh passes 170/170; python3 scripts/lint-instructions.py OK.
- .task-prompt.md (v2.0.7 release review) and .task-meta.json (task_name "v2.1.2 bilingual documentation") are stale relative to this 2.1.3 lifecycle dispatch; treated as orchestration noise, but refreshing them would avoid ambiguity for later reviewers and archives.
- Version bump to 2.1.3 is consistent across .claude-plugin/marketplace.json, .claude-plugin/plugin.json, and .codex-plugin/plugin.json, and both CHANGELOG entries accurately describe the shipped changes.

## Round 2 — approve

- Phase: `verify-fixes`
- Run ID: `d63e4643-b116-4caa-8274-f03a9da38ff7`
- Received: 2026-07-29T10:26:15Z

### Findings

No findings.
### Executor resolution

> [!note] Resolution snapshot
> # Executor Resolution
>
> ## Review Gate mechanism failure
>
> Classification: applied.
>
> The first green verification callback still produced `interactive` because an
> explicit primary-checkout review inherited stale legacy `.task-meta.json`.
> Consequently the authoritative `drive --apply-action` could not finish, and a
> legacy task surface policy would not arm reviewer cleanup.
>
> Commit `7a8ef48` gives explicit primary-checkout reviews a bounded local
> decision policy: clean approval finishes, warning/nit findings reuse the same
> review lane for at most two verification rounds, and blocking findings
> escalate. Drive revalidates the exact received callback when recovering an
> in-flight legacy `interactive` action. Finish now arms root coordinator
> exact-surface cleanup independently of an unrelated dispatch task contract;
> ordinary task and broker review policy remains unchanged.
>
> ## Validation
>
> - `make test` — passed.
> - `tests/test_review_dispatch.sh` — 175 passed, 0 failed.
> - `tests/test_task_lifecycle.py` — passed with a legacy-metadata root-review
>   close regression.
> - `scripts/lint-instructions.py` — passed.

### Verification gaps

- None

### Residual risks

- The false-positive key-injection scenario from the initial review is now bounded to the first 30 minutes of an unattended run rather than eliminated; an agent that renders the trust-prompt marker files on screen inside that window could still receive an injected Enter. This matches the accepted recommendation and needs no further action for this release.

### Notes for executor

- Finding 1 (unbounded trust watcher) verified fixed in aa11a79: WORKSPACE_TRUST_TIMEOUT_SECONDS is now 30*60 with WORKSPACE_TRUST_POLL_SECONDS raised to 0.5 (scripts/cmux_agent_supervisor.py:61-66), matching the recommended generous finite bound; tests/test_task_lifecycle.py:737-740 asserts both production values and the delayed-startup acceptance regression still passes.
- Finding 2 (root-state checkpoint capture) verified fixed in aa11a79: after_exit now gates on reviewer_uses_broker_state (scripts/cmux_surface_lifecycle.py:301) and arm() skips capture via the new reviewer_captures_checkpoint helper (lines 56-60, 230), which correctly preserves capture for legacy reviewers (_STATE_DIR is None) and real broker rounds; the new regression proves no `surface resume get` call occurs for the root-state path and the exact surface still closes.
- Re-verified locally on aa11a79: python3 tests/test_task_lifecycle.py passes including the two new assertions, bash tests/test_review_dispatch.sh passes 170/170, python3 scripts/lint-instructions.py OK. Both CHANGELOG entries were updated to describe the bounded window and skipped checkpoint accurately.
- No stale references remain: the only occurrences of WORKSPACE_TRUST_TIMEOUT_SECONDS and reviewer_captures_checkpoint are the updated definition, the parameter default, and the new test assertions.

## Round 3 — approve

- Phase: `verify-fixes`
- Run ID: `c0da99c3-ba03-475e-a70b-506a3e66d659`
- Received: 2026-07-29T10:36:29Z

### Findings

No findings.
### Executor resolution

No resolution was required or recorded for this round.

### Verification gaps

- None

### Residual risks

- root_coordinator_reviewer in cmux_surface_lifecycle.py is a weaker recognizer than is_primary_coordinator_review in spawn_review.py (no .git-directory or repo-root check); the surface-close contract bypass therefore relies on the supervisor only passing --state-dir equal to the worktree for genuine primary-checkout reviews. Not independently exploitable, since any actor able to invoke the lifecycle script with forged arguments and an armed sentinel could already close its own surface directly, but the two recognizers could drift apart in future changes.
- The trust-prompt key-injection window remains bounded to the first 30 minutes of an unattended run, as accepted in the previous verification round.

### Notes for executor

- The review-gate mechanism fix in 7a8ef48 verified correct. coordinator_review_action implements the stated bounded policy exactly: blocked verdict or any blocking finding escalates, a clean approve applies, warning/nit findings resolve within COORDINATOR_MAX_VERIFY_ITERATIONS=2, and empty-findings non-approve callbacks or exhausted iterations escalate (skills/review-dispatch/scripts/spawn_review.py:1262-1286). An approve carrying warning/nit findings maps to resolve rather than approve, which is the conservative choice and matches this task's own observed lifecycle.
- Drive recovery is tightly bounded: only a recorded legacy 'interactive' action on a primary coordinator review is recomputed, the callback is reloaded and revalidated against the recorded run_id and review mode via parse_review_json (current_received_review, lines 1288-1305), and the recomputed action is persisted only on --apply-action after the payload is printed.
- The lifecycle contract bypass is correctly scoped: root_coordinator_reviewer (scripts/cmux_surface_lifecycle.py:62-79) requires kind=reviewer, --state-dir resolving to the worktree, and archive_mode=coordinator in .review-meta.json, and fails closed to the strict unattended-contract path when review meta is missing or unreadable. cmd_finish still works for legacy metadata because task_contract.normalize maps version-1 metadata to an interactive policy instead of raising, and review_auto_close then arms close via is_primary_coordinator_review, which retains the worktree==vault, real .git directory, and repo-root checks.
- Re-verified locally on 7a8ef48: bash tests/test_review_dispatch.sh passes 175/175 including the five new gate checks (bounded action policy, finish-dry auto-close, drive recovery to approve), python3 tests/test_task_lifecycle.py passes including the legacy-metadata root-review close regression, python3 scripts/lint-instructions.py OK. Both CHANGELOG entries describe the new policy accurately.

## Archive boundary

This page keeps validated review findings, executor resolutions, and final verification. Dedicated raw prompts, compressed callback payloads, command logs, sockets, and cmux identifiers are intentionally excluded. Validated findings and executor resolutions are retained as review evidence.
