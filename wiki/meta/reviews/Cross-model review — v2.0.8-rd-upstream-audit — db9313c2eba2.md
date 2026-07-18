---
type: review
title: "Cross-model review — v2.0.8-rd-upstream-audit — db9313c2eba2"
address: c-000009
created: 2026-07-18
updated: 2026-07-18
tags:
  - review
  - cross-model
status: resolved
sessions:
  - "019f6ddd-d07e-7a30-b018-f6358753fb91"
review_id: "156defb8-518e-4d32-9347-e9d14c39aedf"
reviewer_runtime: "claude"
reviewer_model: "fable"
reviewer_effort: "high"
review_mode: "full"
rounds: 2
verdict: approve
related:
  - "[[2026-07-17-052426-prepare-v2-0-8-from-test-and-upstream-rd-fixes]]"
---

# Cross-model review — v2.0.8-rd-upstream-audit — db9313c2eba2

> [!abstract] Outcome
> **Task:** v2.0.8-rd-upstream-audit
> **Final verdict:** `approve`
> **Reviewer:** claude · fable · effort `high`
> **Executor:** codex
> **Mode:** `full` · **rounds:** 2
> **Started:** 2026-07-18T01:31:45Z
> **Updated:** 2026-07-18T01:43:27Z
> **Plan:** [[2026-07-17-052426-prepare-v2-0-8-from-test-and-upstream-rd-fixes]]

## Review request

Review the implementation for **v2.0.8-rd-upstream-audit** in `task/v2.0.8-rd-upstream-audit` against `main` using the `full` cross-model gate.

> [!quote] Original task request
> Critically audit the remote branches `origin/test` and `origin/upstream-sync/rd-fixes` as patch sources for the current v2.0.7 pipeline. Accept, adapt, or reject each candidate with evidence, prepare a coherent local v2.0.8 release candidate, run full verification, and require a full Claude Fable high cross-model review before final acceptance.
Approved scope and rationale: [[2026-07-17-052426-prepare-v2-0-8-from-test-and-upstream-rd-fixes]].

## Round 1 — approve

- Phase: `initial-review`
- Run ID: `156defb8-518e-4d32-9347-e9d14c39aedf`
- Received: 2026-07-18T01:39:52Z

### Findings

#### 1. nit — Confirm the corrected clarify attribution URL actually exists

- File: `ATTRIBUTION.md:22`
- Evidence:
> Upstream f201232 cited .../skills/productivity/grill-me; the adapted entry cites .../skills/productivity/grilling. Exactly one of the two is the real path, and the reviewer runtime cannot verify it offline. A wrong public attribution link would ship in the v2.0.8 release surface.
- Recommendation:
> Verify the mattpocock/skills path once (browser or coordinator) and keep whichever URL resolves; note the check in the review resolution.

#### 2. nit — Exception list omits the Claude-side Sonnet/low daily summarizer

- File: `docs/model-routing.md:36`
- Evidence:
> The new 'Intentional repository exceptions are narrow' paragraph (and CHANGELOG.md:28) names only the Codex deep profile and the gpt-5.6-terra/low daily route. agents/daily-summarizer.md pins `model: sonnet` at low effort for the Claude runtime — an equally intentional non-fable default that the extended model-defaults audit scope asked to document rather than leave implicit.
- Recommendation:
> Add the Claude Sonnet/low daily-summarizer agent to the documented exceptions in docs/model-routing.md (and optionally the CHANGELOG Changed bullet), mirroring the existing Terra/low wording.

#### 3. nit — Leftover untracked patch artifact duplicates committed changes

- File: `.task-codex-defaults.patch`
- Evidence:
> git status shows untracked .task-codex-defaults.patch whose hunks are identical to the .codex/ model-default changes already committed in 1305003. It is untracked so it cannot ship, but it leaves the worktree dirty for reap and could confuse recovery.
- Recommendation:
> Delete the stale patch file before the final reap handoff.

### Executor resolution

> [!note] Resolution snapshot
> # Review Resolution
>
> Review run: `156defb8-518e-4d32-9347-e9d14c39aedf`
> Verdict received: `approve` (full Claude Fable, high effort)
>
> ## Findings
>
> 1. **Applied — clarify attribution URL.** Current upstream evidence identifies
>    the skill as `grill-me`: the mattpocock/skills repository README lists
>    `/grill-me`, and its productivity README lists the `grill-me` directory.
>    `ATTRIBUTION.md` now links to
>    `skills/productivity/grill-me` and names the skill consistently.
>
> 2. **Applied — Claude daily-summarizer exception.**
>    `docs/model-routing.md` now documents both bounded daily routes:
>    Codex `gpt-5.6-terra` low and Claude Code `sonnet` low. The v2.0.8
>    changelog uses the same runtime-specific wording.
>
> 3. **Applied — stale coordinator patch artifact.** The applied and committed
>    `.codex` changes remain in `1305003`; the untracked
>    `.task-codex-defaults.patch` handoff artifact was removed and is not part of
>    the release candidate.
>
> ## Verification gaps and residual risks
>
> The executor had already run full `make test` and
> `scripts/validate-vault.py --summary` successfully before review. Fable
> independently reran all touched-area suites and reported them passing. The
> reviewer's two residual risks are accepted and documented behavior: the
> approved base rebase block also catches rebase-shaped invocations such as
> `git pull --rebase`, and dispatched Codex tasks now receive explicit repository
> model/effort defaults unless task metadata overrides them.
>
> No finding requires a verify iteration because the typed verdict is
> `approve`; the review contract requires committing the remaining product
> documentation cleanup and finishing this review run.

### Verification gaps

- Full `make test` and `scripts/validate-vault.py --summary` are outside the reviewer's pre-approved command surface and were not re-run here; the final summary should carry the executor's evidence for both. The reviewer independently re-ran every touched-area suite: test_review_dispatch.sh 138/138, test_task_lifecycle.py all pass, test_dcg_assets.sh 21/21, test_codex_adapter.sh 20/20 (incl. new A6 model-defaults alignment), test_skill_router.sh 44/44, test_skill_budget.py (27 skills), test_research_isolation.py all pass, scripts/lint-instructions.py OK, and bash scripts/dcg-test-suite.sh 150/150 including the new base amend-allowed/rebase-blocked matrix.
- The ATTRIBUTION.md grilling URL could not be verified from the offline reviewer runtime (see nit).

### Residual risks

- The base-profile rebase block pattern `git\b.*?\brebase\b` also blocks `git pull --rebase` and rebase-related config invocations in coordinator sessions; this is consistent with the approved decision but slightly broader than 'git rebase' literally.
- Interactive (non-unattended) Codex dispatches now always receive a pinned --model/--effort instead of delegating to the user's active Codex config; this is documented in docs/model-routing.md but changes behavior for users who relied on profile-level model selection in dispatched tasks.

### Notes for executor

- Audit dispositions verified against sources: a4d9516 (origin/test) is correctly not ported — main already ships scripts/with-timeout, its test, and richer Makefile wiring since v2.0.0 (f371c50), so the patch is superseded; the final summary table should record it as rejected/superseded with that evidence.
- 534f977 adaptation is strictly narrower than upstream and matches the approved security decision: base profile gains an explicit rebase block that upstream omitted, amend/cherry-pick/staging stay allowed, task worktrees keep rebase; test_dcg_assets.sh now asserts the base/task delta is exactly the rebase pattern.
- d08d84c adaptation is also narrower than upstream: the executor-profile last-resort fallback in resolve_review_env was removed (fail narrow, no profile), matching the CHANGELOG Security wording; the coordinator-vault scratch exception is gated to the exact canonical checkout with the empty/owner-only/gitignored checks intact.
- b769ed7 adaptation is consistent end-to-end: spawn_review appends the effort -c after --model, imports reviewer_codex_config_values from the supervisor so generation and validation cannot drift, and the supervisor fails closed on unknown efforts or reviewer_model/effort mismatches.
- Model-defaults sweep is complete for current-default surfaces: remaining `opus` hits are historical CHANGELOG entries and explicit opt-in test fixtures, which the mandate says to preserve; .codex-plugin version derives from .claude-plugin via codex_version(), so the 2.0.8 bump is consistent by construction.

## Round 2 — approve

- Phase: `verify-fixes`
- Run ID: `a95a0066-ce78-4c1f-aba0-6e1acfbb2dee`
- Received: 2026-07-18T01:43:27Z

### Findings

No findings.
### Executor resolution

No resolution was required or recorded for this round.

### Verification gaps

- The grill-me URL restoration relies on the executor's stated upstream README evidence; the reviewer runtime remains offline and cannot resolve the link itself. The restored value matches upstream f201232's original citation, which is the lower-risk choice.

### Residual risks

- Unchanged from the initial review: the base-profile rebase block also catches rebase-shaped invocations like `git pull --rebase`, and dispatched Codex tasks now receive pinned repository model/effort defaults unless task metadata overrides them; both are accepted, documented behavior.

### Notes for executor

- All three prior nits verified as resolved in commit 4b49f50: ATTRIBUTION.md:22 restores the grill-me source URL and skill name consistently in both the link and the prose; docs/model-routing.md:36-39 and CHANGELOG.md:28 now document both bounded daily-summarizer routes (Codex gpt-5.6-terra low, Claude sonnet low); git status is completely clean — the stale .task-codex-defaults.patch is gone.
- The fix commit is docs-only (ATTRIBUTION.md, CHANGELOG.md, docs/model-routing.md); python3 scripts/lint-instructions.py passes after the change. No code paths were touched, so the previously verified suites remain valid.
- Final commit hash for the release candidate at verification time: 4b49f50.

## Archive boundary

This page keeps validated review findings, executor resolutions, and final verification. Dedicated raw prompts, compressed callback payloads, command logs, sockets, and cmux identifiers are intentionally excluded. Validated findings and executor resolutions are retained as review evidence.
