---
type: review
title: "Cross-model review — v2.1.1 final implementation review — 43a447bc1b02"
address: c-000015
created: 2026-07-20
updated: 2026-07-20
tags:
  - review
  - cross-model
status: resolved
sessions:
  - "019f6ddd-d07e-7a30-b018-f6358753fb91"
review_id: "facfb77b-e345-46c9-83b5-4ef18fb740a4"
reviewer_runtime: "claude"
reviewer_model: "fable"
reviewer_effort: "high"
review_mode: "full"
rounds: 2
verdict: approve
---

# Cross-model review — v2.1.1 final implementation review — 43a447bc1b02

> [!abstract] Outcome
> **Task:** v2.1.1 final implementation review
> **Final verdict:** `approve`
> **Reviewer:** claude · fable · effort `high`
> **Executor:** codex
> **Mode:** `full` · **rounds:** 2
> **Started:** 2026-07-20T13:18:35Z
> **Updated:** 2026-07-20T13:40:22Z

## Review request

Review the implementation for **v2.1.1 final implementation review** in `task/v2.1.1-code-owned-optimizations` against `v2.1.0` using the `full` cross-model gate.

> [!quote] Original task request
> Perform a full, critical release-gate review of the committed implementation on
> `task/v2.1.1-code-owned-optimizations` against `v2.1.0`. The approved plan is
> `docs/plans/v2.1.1-code-owned-optimization-plan.md`; the current review target
> ends at commit `8ce7eb0`.
>
> The implementation moves deterministic orchestration into repo-owned code while
> preserving model-owned semantic decisions. Review the complete branch, including:
>
> - content-free per-turn and per-stage timing;
> - content-addressed, fail-closed acceptance evidence and selective reuse;
> - production-generation versus cheaper live-test-model provenance;
> - compact runner-first skill paths and their compatibility references;
> - exact cmux surface lifecycle and direct `/close` live acceptance;
> - deterministic dispatch/reap/review mechanics and recovery behavior;
> - the final exact non-behavioral release-file allowlist.
> - post-review hardening for task/reviewer surface closure, broker completion,
>   pending drive/reap transitions, classifier-safe callbacks, unique local
>   operation handoffs, and canonical acceptance checkpoint retention.
>
> Critically check correctness, security, privacy, stale-evidence risk, task/lane
> identity, surface cleanup, failure recovery, backward compatibility, missing
> tests, and any place where moving work into code could reduce result quality.
> Unknown changed paths must remain fail-closed. The release-file allowlist must
> stay exact, path-safe, and unable to hide behavioral changes outside its four
> reviewed files.
>
> The executor previously ran the full hermetic suite successfully. After the
> latest lifecycle/cache fixes, all directly affected review/lifecycle/reap suites,
> 159 review-dispatch checks, `tests/test_release_acceptance.py`, instruction lint,
> and `git diff --check` passed. A real two-direction dispatch canary passed with
> Claude Sonnet and Codex Terra, including review, drive, reap, and automatic exact
> surface cleanup. The expensive full matrix was intentionally stopped before
> continuation so review findings can be fixed first. Independently inspect the
> complete diff and run any relevant pre-approved focused tests. Return actionable
> file/line findings. Approve only if this branch is ready for final acceptance and
> release preparation without quality loss.

## Round 1 — changes-requested

- Phase: `initial-review`
- Run ID: `facfb77b-e345-46c9-83b5-4ef18fb740a4`
- Received: 2026-07-20T13:31:26Z

### Findings

#### 1. blocking — make test is red at HEAD: skill body baseline is stale for review-dispatch

- File: `config/skill-body-baseline.json:23`
- Evidence:
> Commit 84a4a05 (bind review commands to local handoffs) grew skills/review-dispatch/SKILL.md by 192 body bytes without updating the committed baseline. Running `python3 tests/test_skill_budget.py` at HEAD fails with SKILL_BODY_BASELINE_EXCEEDED: review-dispatch:body_bytes+192, body_lines+2, token_estimate+48, closure_bytes+192, closure_token_estimate+48. test-skill-budget is part of `make test` (Makefile:80), so the branch's own hermetic gate fails; the executor's later verification list did not include this suite. Workstream 3.1 explicitly requires increases to come with an explicit baseline update.
- Recommendation:
> Either trim the review-dispatch body back to baseline or, if the +192 bytes from the operation-handoff fix are justified, regenerate the baseline with `python3 scripts/check-skill-budget.py --update-baseline` in a commit that states the reason, then rerun tests/test_skill_budget.py.

#### 2. blocking — Turn-marker telemetry state is not gitignored and gets committed by the Stop hook

- File: `.gitignore:151`
- Evidence:
> turn_telemetry.start_turn writes per-session markers to .vault-meta/turn-markers/<token>.json (scripts/turn_telemetry.py:48), but .gitignore has no entry for that directory (every other new runtime artifact, e.g. .vault-meta/acceptance/ and pipeline-events.jsonl, is ignored). The Stop hook scoped commit runs `git add -A -- wiki .raw .vault-meta` (scripts/stop-hook.py:25 and :286), so any concurrent session's in-flight marker, or a stale marker after a crash, is committed into vault history and later deleted again. This violates the approved plan's rollback contract ('Runtime telemetry files remain derived, gitignored state') and additionally makes release-acceptance's canonical dirty-worktree check (scripts/release-acceptance.py dirty_paths) treat another session's live marker as an unknown behavioral dirty path, spuriously blocking a canonical acceptance run on a multi-session vault.
- Recommendation:
> Add `.vault-meta/turn-markers/` to .gitignore next to the other derived .vault-meta entries, and add a regression assertion (like the existing 'acceptance runtime is gitignored' checks) that the marker path is ignored.

#### 3. warning — Claude graceful exit ports the documented two-character \\n escape as a raw LF, which may not submit

- File: `scripts/queue-session-exit.py:41`
- Evidence:
> The v2.1.0 close skill sent `cmux send --surface "$CMUX_SURFACE_ID" "/exit\n"` from shell, where the argument contains the literal two characters backslash-n, and documented that cmux itself interprets that escape as Enter. The Python port passes "/exit\n" with a real 0x0A byte instead. If cmux only translates the escape sequence, the raw LF reaches the Claude composer as a newline rather than a submit, leaving `/exit` typed but not executed. Every other exit path in the repo submits with a separate keypress (scripts/cmux_surface_lifecycle.py:239-250 uses send "/exit" then send-key Enter; the research notifiers do the same). tests/test_queue_session_exit.py only asserts the argv shape, and the close live-acceptance cell has not been re-run since the matrix was intentionally stopped.
- Recommendation:
> Use the repo-proven sequence (send "/exit" then send-key Enter, optionally after the same composer-clear used by request_exit) or pass the literal backslash-n escape exactly as v2.1.0 did; then prove it with the close live cell before final acceptance.

#### 4. warning — Compacted reap-send normal path uses a cwd-relative runner path that does not exist in most task worktrees

- File: `skills/reap-send/SKILL.md:30`
- Evidence:
> Step 4 instructs `python3 skills/reap-send/scripts/send_reap.py --worktree "$PWD"` to be run from the dispatched task worktree. Task worktrees of arbitrary product repos do not contain the plugin's skills/ tree; only self-hosted llm-obsidian worktrees do. The prior skill text and the dispatch task-prompt template (skills/dispatch/references/task-prompt-template.md:264) both pin `<vault-root>/skills/reap-send/scripts/send_reap.py`. A task agent following the skill body instead of the rendered prompt gets a file-not-found on the critical final handoff.
- Recommendation:
> Restore the `<vault-root>` prefix in the skill body command (matching the task-prompt template), and update config/skill-body-baseline.json if the byte count changes.

### Executor resolution

> [!note] Resolution snapshot
> # Executor resolution
>
> Review run: `facfb77b-e345-46c9-83b5-4ef18fb740a4`
>
> 1. **Skill body baseline — applied.** Compacted `skills/review-dispatch/SKILL.md` below the existing committed budget instead of expanding the baseline. `scripts/check-skill-budget.py` and `tests/test_skill_budget.py` pass.
> 2. **Turn-marker runtime state — applied.** Added `.vault-meta/turn-markers/` to `.gitignore` and added a regression that invokes `git check-ignore` on a representative marker.
> 3. **Claude exit submission — applied.** Replaced the raw-newline send with the repository-proven exact-surface sequence: clear composer, send `/exit`, press Enter. Added an exact argv unit regression. A real close-only live canary will run after this verification gate and before full acceptance.
> 4. **Reap runner location — applied.** Restored the `<vault-root>/skills/reap-send/scripts/send_reap.py` path while keeping the skill within its existing budget.
>
> Focused verification passed: skill-budget checker and test, queue-session-exit, live-acceptance-runner, runtime-hooks, reap-send-runner, review-dispatch (159/159), instruction lint, and `git diff --check`. The complete hermetic suite and invalidated live acceptance cells intentionally follow cross-model approval so they are not rerun before possible review-driven changes.

### Verification gaps

- Could not execute a live cmux probe of `cmux send` raw-newline semantics from this read-only reviewer; the queue-session-exit warning needs the close live cell (or a direct cmux check) to confirm or dismiss.
- Did not run the full `make test` aggregate (only per-suite entrypoints are pre-approved); I ran release-acceptance, live-acceptance-runner, runtime-hooks, task-sessions, task-lifecycle, research-isolation, review-dispatch (159 checks), review-operation-namespacing, dispatch-runner, dispatch-resolver, reap-runner, reap-send-runner, queue-session-exit, pipeline-events, session-preflight, skill-budget, and instruction lint. Only tests/test_skill_budget.py failed.
- The expensive full live matrix was intentionally stopped before completion, so the reworked close and autoresearch live cells and the new exact-surface replacement-shell collapse have not yet been proven live on this exact HEAD.

### Residual risks

- Evidence reuse deliberately accepts silent provider drift behind an unchanged hosted model alias (no TTL); mitigated by visible evidence age and --explain-selection per the approved plan.
- The last-surface replacement-shell collapse types `exit` into the surface cmux substituted into the closed pane; it is guarded by an exact one-delta layout comparison and fails closed on ambiguity, but it depends on current cmux replacement semantics and should be watched across cmux upgrades.
- Claude background-work exit auto-confirmation sends a single Enter to the native dialog only after an armed lifecycle close; if a future Claude build changes the dialog's default selection, the confirmation could pick a different option. The marker gate bounds the blast radius to already-authorized closures.

### Notes for executor

- Fixing either blocker (baseline update, .gitignore line) changes paths that are neither declared acceptance dependencies nor in the four-file non-behavioral allowlist, so changed_paths will classify them as unknown and correctly invalidate all existing live evidence; plan to run the full matrix after the fixes, which matches the already-planned continuation.
- The .task-review-drive-*.json pending marker is created on every supervised callback and removed only by a successful `drive --action-file --apply-action`; an escalated or abandoned review leaves it in place and send_reap fails closed. That is correct fail-closed behavior, but consider documenting the coordinator-side cleanup for an abandoned review cycle so a stuck marker is diagnosable.
- The rest of the branch reviewed well: acceptance reuse is genuinely fail-closed (integrity hash, provenance, unknown-path invalidation, schema-1 rejection, --restart/--skill exclusivity), the four-file non-behavioral allowlist is exact and path-safe, telemetry stays content-free with fail-closed origin resolution and no coordinator behavior granted to task hooks, reviewer env additions (LLM_OBSIDIAN_PROJECT_ROOT/SESSION_ROLE) are validated in the supervisor spec, and the exact-surface close/replacement-shell logic raises on any ambiguous layout delta instead of guessing.

## Round 2 — approve

- Phase: `verify-fixes`
- Run ID: `83c4d47b-7fe8-4db3-b26f-71ca23c82125`
- Received: 2026-07-20T13:40:22Z

### Findings

No findings.
### Executor resolution

No resolution was required or recorded for this round.

### Verification gaps

- The queue-session-exit fix is proven hermetically (exact argv regression) but not yet live; the executor's planned close-only live canary before full acceptance is the remaining proof for real cmux submit behavior.
- I re-ran the focused suites covering everything commit d27da21 touches (skill-budget, queue-session-exit, live-acceptance-runner including the new turn-marker gitignore regression, runtime-hooks, reap-send-runner, the 159-check review-dispatch suite, instruction lint); the full `make test` aggregate and the invalidated live matrix intentionally follow this approval per the executor's stated ordering.

### Residual risks

- Evidence reuse deliberately accepts silent provider drift behind an unchanged hosted model alias (no TTL); mitigated by visible evidence age and --explain-selection per the approved plan.
- The last-surface replacement-shell collapse and the Claude background-work exit auto-confirmation depend on current cmux/Claude UI semantics; both are gated (exact one-delta layout comparison, armed-close marker) and fail closed on ambiguity, but should be watched across cmux/Claude upgrades.
- The reworked close and autoresearch live cells have not yet been proven live on this exact HEAD; the planned post-approval live matrix and close canary cover this before release.

### Notes for executor

- Finding 1 (stale skill body baseline) — resolved as verified: skills/review-dispatch/SKILL.md was compacted back under the committed baseline instead of expanding it; tests/test_skill_budget.py passes and the 159-check contract suite confirms no required phrase was lost in the compaction.
- Finding 2 (turn markers not gitignored) — resolved as verified: `.vault-meta/turn-markers/` added at .gitignore:152 next to the other derived entries, with a `git check-ignore` regression in tests/test_live_acceptance_runner.py that passes.
- Finding 3 (raw-LF Claude exit) — resolved as verified: scripts/queue-session-exit.py now uses the repository-proven sequence (ctrl+u composer clear, send `/exit`, send-key Enter), matching cmux_surface_lifecycle.request_exit, with an exact three-call argv regression. Run the promised close live canary before final acceptance.
- Finding 4 (cwd-relative send_reap path) — resolved as verified: the `<vault-root>/skills/reap-send/scripts/send_reap.py` prefix is restored in skills/reap-send/SKILL.md within the existing budget.
- Commit d27da21 contains nothing beyond the four fixes and their regressions; the working tree is clean at HEAD. As noted in the prior round, these changed paths correctly invalidate all existing live acceptance evidence, so the full matrix must run before the release decision — which matches the already-approved plan ordering.

## Archive boundary

This page keeps validated review findings, executor resolutions, and final verification. Dedicated raw prompts, compressed callback payloads, command logs, sockets, and cmux identifiers are intentionally excluded. Validated findings and executor resolutions are retained as review evidence.
