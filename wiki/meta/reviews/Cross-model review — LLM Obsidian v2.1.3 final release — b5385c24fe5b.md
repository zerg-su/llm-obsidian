---
type: review
title: "Cross-model review — LLM Obsidian v2.1.3 final release — b5385c24fe5b"
address: c-000033
created: 2026-07-29
updated: 2026-07-29
tags:
  - review
  - cross-model
status: resolved
sessions:
  - "019fab00-3160-7380-8920-4b20183afb76"
review_id: "9c203d65-9ebc-415a-95fc-f0c6d80c5103"
reviewer_runtime: "claude"
reviewer_model: "fable"
reviewer_effort: "high"
review_mode: "full"
rounds: 2
verdict: approve
---

# Cross-model review — LLM Obsidian v2.1.3 final release — b5385c24fe5b

> [!abstract] Outcome
> **Task:** LLM Obsidian v2.1.3 final release
> **Final verdict:** `approve`
> **Reviewer:** claude · fable · effort `high`
> **Executor:** codex
> **Mode:** `full` · **rounds:** 2
> **Started:** 2026-07-29T12:34:40Z
> **Updated:** 2026-07-29T12:47:00Z

## Review request

Review the implementation for **LLM Obsidian v2.1.3 final release** in `main` against `v2.1.2` using the `full` cross-model gate.

> [!quote] Original task request
> Perform a final full, read-only cross-model code review of the public LLM Obsidian v2.1.3 release candidate on `main` against tag `v2.1.2`.
>
> Review the complete committed diff `v2.1.2..HEAD`, with particular attention to exact coordinator-review surface cleanup, callback routing, cmux ID compatibility, bounded trust bootstrap, acceptance session identity, runner-owned reap/reap-send fixtures, and the exact unsafe-research acceptance network scope. Verify release metadata, bilingual changelog parity, hermetic regression coverage, and the recorded 58/58 live acceptance evidence. Do not review private vault content or edit product files.
>
> Evidence already available: the full hermetic `make test` suite passed, `scripts/release-acceptance.py check` reports 29 skills × 2 runtimes, `git diff --check` passed, and the fresh live matrix completed 58/58 with Sonnet and `gpt-5.6-terra` at medium effort.
>
> Return only actionable findings grounded in file/line evidence. Distinguish blocking correctness/security defects from warnings or nits, state any verification gaps and residual risks, and approve when no changes are required.

## Round 1 — approve

- Phase: `initial-review`
- Run ID: `9c203d65-9ebc-415a-95fc-f0c6d80c5103`
- Received: 2026-07-29T12:41:00Z

### Findings

#### 1. nit — File-based acceptance identity now outranks real session env, but the .acceptance-sandbox.json gate is not gitignored

- File: `scripts/current-session-id.sh:12`
- Evidence:
> The new branch reads .vault-meta/acceptance/session-id and prefers it over CLAUDE_CODE_SESSION_ID/CODEX_THREAD_ID whenever <repo-root>/.acceptance-sandbox.json exists, without requiring LLM_OBSIDIAN_ACCEPTANCE=1. The session-id file lives under gitignored .vault-meta/acceptance/, but .acceptance-sandbox.json itself has no .gitignore entry, so a marker accidentally copied or committed into a real checkout would silently redirect provenance for every session in that checkout. Today both markers are only created inside disposable sandbox clones (scripts/acceptance/sandbox.py:141, scripts/acceptance/runner.py persist_acceptance_session), so the practical risk is low.
- Recommendation:
> Add .acceptance-sandbox.json to .gitignore so the identity-switching marker can never be committed into a real checkout; optionally also validate its schema (e.g. schema_version plus run_dir existence) before honoring the session-id file.

#### 2. nit — Durable review archive quotes a stale v2.0.7 task request for the v2.1.3 lifecycle review

- File: `wiki/meta/reviews/Cross-model review — LLM Obsidian 2.1.3 lifecycle fixes — 1ace771c7718.md:34`
- Evidence:
> The archived page's 'Original task request' block reproduces .task-prompt.md verbatim, which still describes the old 'llm-obsidian-v207-opus-final-review' task (release/v2.0.7 vs v2.0.6). The same stale prompt plus a .task-meta.json task_name of 'v2.1.2 bilingual documentation' also mislabel this dispatch itself. The round 1 reviewer already noted the staleness; it has now been baked into the permanent wiki archive, where a future reader will misidentify what the 2.1.3 review actually covered.
- Recommendation:
> Refresh .task-prompt.md and .task-meta.json task_name when dispatching a new coordinator review, and correct (or annotate) the archived page's quoted request so the durable record describes the 2.1.3 lifecycle scope it actually reviewed.

### Executor resolution

> [!note] Resolution snapshot
> # Executor Resolution
>
> ## Finding 1 — acceptance marker tracking
>
> Classification: fixed.
>
> Added the root-only `/.acceptance-sandbox.json` marker to `.gitignore`. The
> acceptance clone still creates and reads the marker normally, while an ordinary
> checkout can no longer accidentally commit the switch that enables synthetic
> session provenance.
>
> ## Finding 2 — stale coordinator review metadata
>
> Classification: fixed.
>
> Annotated the existing durable v2.1.3 lifecycle review so its quoted v2.0.7
> request is explicitly identified as stale legacy handoff metadata and the
> actual v2.1.3 scope is unambiguous. Refreshed the current coordinator handoff,
> review metadata, and review history to `LLM Obsidian v2.1.3 final release`
> against `v2.1.2`, so this verification round and its durable archive no longer
> inherit the old task label or request.
>
> ## Verification
>
> - `git check-ignore .acceptance-sandbox.json` resolves to the new root-only
>   ignore rule.
> - The archived page retains valid Obsidian Markdown/frontmatter and is updated
>   through one optimistic `vault-write.py` transaction.
> - The full hermetic suite and fresh 58/58 live matrix were already green at
>   commit `cbdc889`; these metadata-only fixes do not change the acceptance
>   execution path.

### Verification gaps

- cmux '--id-format both' compatibility (scripts/task_sessions.py cmux_tree) is still proven only against test doubles; no live cmux invocation was possible in this read-only review, so the assumption that older CLIs fail with non-zero rc (rather than rc 0 with non-JSON) remains unverified.
- The reworked reap/reap-send acceptance cells and the pinned unsafe-research network path (peps.python.org allowlist, curl fallback) were validated only through the hermetic suites (test_live_acceptance_runner.py, prompt baseline); no live acceptance run was executed here.
- I ran the five suites relevant to this diff (lint-instructions, test_task_sessions, test_task_lifecycle, test_research_isolation, test_review_dispatch.sh, test_live_acceptance_runner) — all pass; the full 'make test' and bench-retrieval were not rerun in this session and rest on the executor's recorded evidence.

### Residual risks

- The exact-trust-prompt key-injection window remains the first 30 minutes of an unattended run (scripts/cmux_agent_supervisor.py WORKSPACE_TRUST_TIMEOUT_SECONDS), as accepted in the prior review rounds; wait_for_reap_coordinator_ready adds a similar bounded auto-accept in the acceptance runner only.
- root_coordinator_reviewer (scripts/cmux_surface_lifecycle.py) remains a weaker recognizer than is_primary_coordinator_review (spawn_review.py) — no .git/repo-root check — so the auto-close contract bypass relies on the supervisor passing --state-dir == worktree only for genuine primary-checkout reviews; the two recognizers could drift in future changes.
- Any checkout that ever gains both .acceptance-sandbox.json and .vault-meta/acceptance/session-id will silently attribute all provenance to the synthetic acceptance identity (see nit finding).

### Notes for executor

- This branch was already approved through a 3-round coordinator review archived at commit a925e08; the new unreviewed content is commits 3082aaf (runner-prepared reap acceptance), 77990a6 (pinned unsafe-research source), and cbdc889 (scoped peps.python.org acceptance network allow), plus the archive bookkeeping. All of it checks out.
- The acceptance network scoping is well fail-closed: domains are code-owned constants keyed on (skill, runtime, scenario), re-validated in run_agent_process against the spec, and Claude-runtime overrides are rejected outright (scripts/acceptance/launchers.py).
- CHANGELOG.md and CHANGELOG.ru.md are in accurate parity, the 2.1.3 version bump is consistent across all three plugin manifests, and the renamed prompt baseline updates exactly the six hashes whose fixtures changed (reap, reap-send, unsafe-research × two runtimes).

## Round 2 — approve

- Phase: `verify-fixes`
- Run ID: `7824f95e-538f-4d80-bc75-f3c6288cac84`
- Received: 2026-07-29T12:47:00Z

### Findings

No findings.
### Executor resolution

No resolution was required or recorded for this round.

### Verification gaps

- The executor's 'git check-ignore' evidence was not rerun here (command not pre-approved), but the added rule is a correct root-anchored '/.acceptance-sandbox.json' entry in .gitignore:152 and git status confirms no marker is tracked or listed in this checkout.
- Both fixes are currently unstaged working-tree changes (.gitignore and the annotated wiki review page); they still need the normal scoped commit to become durable.

### Residual risks

- The previously accepted residual risks are unchanged: the 30-minute exact-trust-prompt auto-accept window, and the weaker root_coordinator_reviewer recognizer in cmux_surface_lifecycle.py relative to is_primary_coordinator_review in spawn_review.py.
- The acceptance-identity precedence in scripts/current-session-id.sh still honors the marker pair if both ever appear in a checkout; the new ignore rule removes the accidental-commit path, which was the accepted scope of the nit.

### Notes for executor

- Finding 1 verified fixed: .gitignore:152 adds the root-only '/.acceptance-sandbox.json' rule exactly as recommended. Sandbox behavior is unaffected — the marker was always untracked inside disposable clones, safe_cleanup and run_agent_process check the file directly rather than git state, and the existing git-status special-cases in scripts/acceptance/scenario_adapters.py and skill_adapters.py become dead-but-harmless. python3 tests/test_live_acceptance_runner.py passes in full (including 'runner-owned marker is cleanup-neutral' and the 58/58 v2.1.3 prompt baseline) and scripts/lint-instructions.py is OK on the fixed tree.
- Finding 2 verified fixed: the archived v2.1.3 lifecycle review page now carries a well-formed '> \[!warning\] Historical metadata mismatch' callout directly above the stale quoted request, explicitly stating the actual v2.1.3 scope; the page frontmatter remains valid and 'updated: 2026-07-29' was already correct. The live coordinator handoff is also refreshed: .task-prompt.md and .task-meta.json now describe 'LLM Obsidian v2.1.3 final release' against base v2.1.2, matching the real diff, so this verification round and its archive no longer inherit the v2.0.7/v2.1.2-bilingual labels.
- No new code, test, or prompt-baseline changes were introduced by these metadata-only fixes; the committed diff v2.1.2...HEAD is unchanged since the prior approve.

## Archive boundary

This page keeps validated review findings, executor resolutions, and final verification. Dedicated raw prompts, compressed callback payloads, command logs, sockets, and cmux identifiers are intentionally excluded. Validated findings and executor resolutions are retained as review evidence.
