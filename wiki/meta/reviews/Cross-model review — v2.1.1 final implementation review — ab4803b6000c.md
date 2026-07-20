---
type: review
title: "Cross-model review — v2.1.1 final implementation review — ab4803b6000c"
address: c-000014
created: 2026-07-20
updated: 2026-07-20
tags:
  - review
  - cross-model
status: resolved
sessions:
  - "019f6ddd-d07e-7a30-b018-f6358753fb91"
review_id: "29fd55d7-c10d-4df5-bbe7-a7dccd463f50"
reviewer_runtime: "claude"
reviewer_model: "fable"
reviewer_effort: "high"
review_mode: "full"
rounds: 2
verdict: approve
---

# Cross-model review — v2.1.1 final implementation review — ab4803b6000c

> [!abstract] Outcome
> **Task:** v2.1.1 final implementation review
> **Final verdict:** `approve`
> **Reviewer:** claude · fable · effort `high`
> **Executor:** codex
> **Mode:** `full` · **rounds:** 2
> **Started:** 2026-07-20T00:51:50Z
> **Updated:** 2026-07-20T01:25:34Z

## Review request

Review the implementation for **v2.1.1 final implementation review** in `task/v2.1.1-code-owned-optimizations` against `v2.1.0` using the `full` cross-model gate.

> [!quote] Original task request
> Perform a full, critical release-gate review of the committed implementation on
> `task/v2.1.1-code-owned-optimizations` against `v2.1.0`. The approved plan is
> `docs/plans/v2.1.1-code-owned-optimization-plan.md`; the current review target
> ends at commit `d16f371`.
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
>
> Critically check correctness, security, privacy, stale-evidence risk, task/lane
> identity, surface cleanup, failure recovery, backward compatibility, missing
> tests, and any place where moving work into code could reduce result quality.
> Unknown changed paths must remain fail-closed. The release-file allowlist must
> stay exact, path-safe, and unable to hide behavioral changes outside its four
> reviewed files.
>
> The executor previously ran the full hermetic suite successfully. After the
> final allowlist change, `tests/test_release_acceptance.py`,
> `tests/test_live_acceptance_runner.py`, instruction lint, and `git diff --check`
> all passed. Independently inspect the diff and run any relevant pre-approved
> focused tests. Return actionable file/line findings. Approve only if this branch
> is ready for final acceptance and release preparation without quality loss.

## Round 1 — changes-requested

- Phase: `initial-review`
- Run ID: `29fd55d7-c10d-4df5-bbe7-a7dccd463f50`
- Received: 2026-07-20T01:05:01Z

### Findings

#### 1. blocking — The final allowlist commit d16f371 broke test\_session\_preflight deterministically; the suite is red on the release candidate.

- File: `tests/test\_session\_preflight.py:63`
- Evidence:
> d16f371 added the file-existence check in acceptance_fingerprints.read_manifest (every non_behavioral_paths entry must be a file under --root). The preflight fixture root ('guessed') contains only config/model-routing.toml and a copied config/acceptance-cells.toml, not .claude-plugin/marketplace.json, .claude-plugin/plugin.json, .codex-plugin/plugin.json, or CHANGELOG.md, so read_manifest raises FingerprintError, session-preflight.py:88 swallows it as ValueError, no 'model-generation-changed' issue is emitted, and the check at tests/test_session_preflight.py:77 raises AssertionError. Reproduced locally (exit 1). The executor re-ran only test_release_acceptance, test_live_acceptance_runner, lint, and git diff --check after d16f371, so this regression was missed.
- Recommendation:
> Create the four allowlisted files in the fixture root (empty files suffice) or validate allowlist existence where the allowlist is consumed instead of in read_manifest; then re-run the complete hermetic suite, not a subset, before final acceptance.

#### 2. blocking — Origin-vault redirect applies to every hook route, so task/reviewer sessions now run the coordinator vault's full Stop pipeline and other hooks against it, crossing the documented read-only boundary.

- File: `hooks/run-hook.py:30`
- Evidence:
> vault_root() now short-circuits to origin_vault() whenever .task-meta.json is found above cwd, and this branch also exports LLM_OBSIDIAN_PROJECT_ROOT (the coordinator vault) into task/reviewer sessions (cmux_agent_supervisor.py ALLOWED_ENV/prepare_task, spawn_review.py launch_command). .task-meta.json's vault_root is the coordinator vault by contract (dispatch-runner.py:224 requires it to be an llm-obsidian vault). Result: a dispatched task or reviewer session in a product worktree, where v2.1.0 hooks were a complete no-op, now injects the coordinator's hot.md at SessionStart, emits skill-router hints into task prompts, captures task commands into the coordinator's command log, and on every Stop runs stop_pipeline(coordinator vault) - reindex, validation, and scoped git auto-commit of the coordinator repo from an unattended background session (run-hook.py:152-154). The plan (WS1.3) authorized origin-vault resolution for telemetry only; the dispatch contract states the coordinator vault is read-only to the task. Tests only cover the router route for this case, not Stop.
- Recommendation:
> Scope the origin-vault resolution to the telemetry calls (start_turn/finish_turn/clear_stale) and keep stop_pipeline, session_context, skill-router, and command-capture on the previous root resolution (or explicitly no-op them when the resolved root is not the session's own project). Add a regression test that a Stop in a worktree carrying .task-meta.json does not invoke stop.sh against the declared coordinator vault.

#### 3. blocking — Acceptance evidence reuse fails open for dirty worktrees: same-commit resume skips the dirty-path scan, and fingerprints hash worktree bytes while cells execute the committed HEAD clone.

- File: `scripts/release-acceptance.py:246`
- Evidence:
> Two fail-open holes against the plan's core invariant ('unknown changed paths invalidate cached evidence fail-closed'; WS2.2 requires staged/unstaged/non-ignored-untracked paths in the changed set relative to the provenance commit). (a) load_resume_results sets changed = set() when prior_commit == commit, so `git status` is never consulted: a dirty modification to an undeclared behavioral path (e.g. scripts/vault_schema.py or lib_sanitize.py, both imported by the worktree copy of live-acceptance-runner.py that actually executes, yet absent from global_dependencies) invalidates nothing at the same commit. The plan's promised staged/unstaged/untracked negative tests do not exist; test_release_acceptance.py commits before every rerun. (b) cell_metadata hashes dependency bytes from the worktree, but create_sandbox executes `git clone` + `checkout --detach HEAD`, i.e. committed content. Sequence: edit a declared dependency (e.g. skills/close/SKILL.md) uncommitted, run acceptance at commit X - the sandbox tests X's version while the recorded row carries the fingerprint of the edited bytes; commit the edit (HEAD=Y); resume - the changed path is declared, the fingerprint matches, and the row is reused as 'reused-identical' although the committed content was never live-tested.
- Recommendation:
> Always union the `git status --porcelain=v1 --untracked-files=all` path set into `changed`, including when prior_commit == commit (changed_paths already handles the same-commit diff). Close the hash/execution asymmetry: hash declared dependencies from HEAD blobs (git show) to match what the sandbox executes, or refuse to record/decorate/reuse evidence whenever any declared dependency or unknown path is dirty. Add regression tests for the same-commit dirty unknown path and for the dirty-then-commit reuse sequence.

#### 4. warning — The task-origin telemetry test inherits os.environ and breaks (or passes vacuously) inside this repo's own supervised sessions.

- File: `tests/test\_runtime\_hooks.py:179`
- Evidence:
> task_env = dict(os.environ), and turn_telemetry.role_for gives LLM_OBSIDIAN_SESSION_ROLE precedence over the .task-meta.json context probe. This branch itself exports that variable into reviewer ('reviewer') and task ('task') sessions via spawn_review.py/cmux_agent_supervisor.py. Running the suite in a reviewer session (as this review did) fails 'task origin routes to coordinator vault' because actor == 'reviewer'; running it in a task session passes for the wrong reason without exercising the context-probe branch the test claims to verify.
- Recommendation:
> Strip LLM_OBSIDIAN_SESSION_ROLE (and other role/identity variables the adapter reads) from every child env this test constructs, so the suite is hermetic in exactly the supervised sessions this repo's own dispatch pipeline uses to run `make test`.

#### 5. warning — Plan workstream 1.6 is not delivered: no per-session-role turn/incomplete breakdown in pipeline-stats.

- File: `scripts/pipeline-stats.py:315`
- Evidence:
> The plan requires 'turn counts, incomplete counts, and p50/p95 duration by runtime/session role and lifecycle stage'. pipeline-stats.py is untouched on this branch; it groups by runtime|op|status only and ignores the `actor` field the new events record, so coordinator/task/reviewer turns merge into one row. Relatedly, WS1.4 says stale turns emit 'without a duration', but turn_telemetry.emit_incomplete (scripts/turn_telemetry.py:70) attaches duration_ms - harmless for model-turn percentiles since incomplete events form a separate op row, but a plan deviation.
- Recommendation:
> Either add the actor/role dimension (and an explicit turn/incomplete counter section) to pipeline-stats.py, or record the reduced scope as an approved deviation from the plan before release.

#### 6. nit — --update-baseline regenerates the baseline with a stale 'v2.1.0' basis label.

- File: `scripts/check-skill-budget.py:71`
- Evidence:
> baseline_payload hardcodes basis 'v2.1.0 skill bodies and directly referenced Markdown', while the committed config/skill-body-baseline.json says 'v2.1.1 optimized skill bodies...'. The next legitimate baseline update silently regresses the provenance label.
- Recommendation:
> Derive the basis string from an argument or drop the version from it.

#### 7. nit — The 'legacy-same-commit' schema-1 reuse branch is unreachable dead code.

- File: `scripts/release-acceptance.py:268`
- Evidence:
> Schema-1 resume requires the stored matrix_fingerprint to equal the new computation, which now includes per-cell fingerprints; a v2.1.0-written report can never match, so every schema-1 resume raises 'use --restart' before reaching the reuse branch. Behavior is fail-closed and matches the rollback note, but the branch and its reason string are dead.
- Recommendation:
> Remove the unreachable branch or add a comment stating it is defensive only, so future readers do not assume schema-1 same-commit reuse works.

### Executor resolution

> [!note] Resolution snapshot
> # Executor Resolution: v2.1.1 final implementation review
>
> Fix commit: `8c4a42e` (`fix: close final review safety gaps`)
>
> ## Findings
>
> 1. **Applied — preflight fixture follows the exact allowlist.** The fixture parses
>    `config/acceptance-cells.toml` and copies every declared
>    `non_behavioral_paths` file into the synthetic root. This keeps the test tied
>    to the manifest rather than duplicating four literals.
>
> 2. **Applied — task/reviewer hooks preserve the read-only boundary.** Origin-vault
>    resolution is used only for content-free turn telemetry. Task/reviewer
>    `SessionStart`, skill routing, command capture, plan capture, `PostCompact`,
>    and the full Stop pipeline are disabled. Invalid task origin metadata fails
>    closed before coordinator behavior. Regression coverage proves no hot-context
>    injection, no command capture, no misleading compact hint, and no Stop
>    invocation against the coordinator vault.
>
> 3. **Applied — canonical acceptance evidence is fail-closed on dirty behavioral
>    state.** Same-commit and cross-commit changed-path inspection now includes
>    staged, unstaged, and non-ignored untracked files. Canonical reports refuse
>    to execute or record evidence while any path outside the exact
>    `non_behavioral_paths` set is dirty, eliminating the worktree-hash/HEAD-run
>    asymmetry. Tests cover dirty unknown paths, dirty declared dependencies,
>    dirty-then-commit reruns, exact metadata-only reuse, and committed unknown
>    path invalidation. Explicit external reports remain diagnostic only and do
>    not update the canonical model-generation snapshot.
>
> 4. **Applied — runtime hook tests are hermetic.** Role, project, and session
>    identity variables are removed before constructing child environments, so
>    the task-context probe is tested rather than inherited coordinator/task or
>    reviewer state.
>
> 5. **Applied — role-aware turn telemetry is reported.** Pipeline stats now show
>    completed and incomplete turns by runtime and coordinator/task/reviewer role,
>    with p50/p95 computed only from completed turns. Incomplete events carry no
>    fabricated duration.
>
> 6. **Applied — skill budget provenance is version-neutral.** Both the generator
>    and committed baseline use `optimized skill bodies and normal-path referenced
>    Markdown`, with regression coverage for regenerated baselines.
>
> 7. **Applied — schema-1 resume is explicit.** Schema-1 evidence always requires
>    `--restart`; the unreachable legacy same-commit reuse branch was removed.
>
> ## Additional mechanism repair
>
> Review startup now removes a stale `.review-outbox.json` with the other
> round-scoped artifacts. This repairs the reproducible initial reviewer write
> failure observed in this round and has a regression test.
>
> ## Verification
>
> - `make test` — passed, exit 0.
> - `python3 tests/test_runtime_hooks.py` — passed again after the final
>   `PostCompact` boundary refinement.
> - `python3 -m py_compile hooks/run-hook.py tests/test_runtime_hooks.py` — passed.
> - `git diff --check` — passed.
> - Focused suites passed before the full run: session preflight, release
>   acceptance, pipeline events, skill budget, and review dispatch (159/159).
>
> No live acceptance cells were rerun in this resolution; the changes are covered
> by the hermetic integration and regression suites above. The live matrix remains
> a separate release-evidence gate.

### Verification gaps

- Ran the focused hermetic suites individually (release_acceptance, live_acceptance_runner, queue_session_exit, task_sessions, dispatch_resolver, skill_budget, session_preflight, runtime_hooks, task_lifecycle, research_isolation, lint-instructions); did not run `make test` as one entrypoint. test_session_preflight fails everywhere (finding 1); test_runtime_hooks fails only under supervised-session env (finding 4); all others pass.
- Did not execute any live acceptance cells or verify Codex Tab-queued /exit and exact cmux close semantics against a real cmux daemon; those paths were validated only through the repo's fakes and unit tests.
- Did not independently verify that production routing still selects Opus/Fable and Sol (plan step 7's live proof).

### Residual risks

- Accepted by the plan: silent provider drift behind an unchanged hosted alias is not detected; only evidence age makes it visible at the gate.
- Reuse locality to one worktree is by declaration only: nothing in the schema-2 report binds it to the worktree, so a manually copied .vault-meta/acceptance/latest-live.json with intact integrity hashes would be honored in a sibling worktree at the same commit ancestry.
- Turn telemetry markers and events from task/reviewer sessions require write access to the coordinator vault's .vault-meta; inside the unattended Codex sandbox those writes may silently fail, so task-turn latency coverage will be partial.

### Notes for executor

- The skill compaction itself looks contract-preserving: canonical runner commands, fail-closed boundaries, same-session verification, process-exit surface closing, and compatibility routes survive in the compacted SKILL.md files plus conditional references, and the body-baseline gate enforces the reduction.
- close_surface_exact (task_sessions.py) with tree-proof and one bounded retry is a real improvement over trusting close-surface exit codes, and its adoption across live runner, lifecycle, dispatch-runner, and research-isolation is consistent.
- The non-behavioral allowlist is exact and path-safe as required: no globs, traversal rejected (tested), existence enforced, and limited to the four release-metadata files.

## Round 2 — approve

- Phase: `verify-fixes`
- Run ID: `214a207e-1789-4fc5-8b30-58523183cfa1`
- Received: 2026-07-20T01:25:34Z

### Findings

No findings.
### Executor resolution

No resolution was required or recorded for this round.

### Verification gaps

- Did not run `make test` as one entrypoint (executor reports exit 0); independently re-ran the affected suites: test_session_preflight, test_runtime_hooks, test_release_acceptance, test_pipeline_events, test_skill_budget, test_review_dispatch.sh (159/159), and lint-instructions - all pass, including test_runtime_hooks inside this supervised reviewer session where it previously failed.
- No live acceptance cells were rerun in this resolution; plan step 7 (changed-cell live matrix with Sonnet/Terra plus the Opus/Fable-and-Sol production-routing proof) remains an outstanding separate release-evidence gate, as the executor states.
- Codex Tab-queued /exit and exact cmux close semantics were still validated only against the repo's fakes, not a live cmux daemon.

### Residual risks

- Behavior change accepted as the conservative direction: task sessions in vault self-worktrees no longer run any Stop pipeline against their own worktree vault (v2.1.0 did), so vault-write mutations made inside a task worktree stay uncommitted until an explicit commit.
- External --report runs outside .vault-meta/acceptance/ remain diagnostic-only by design: they skip the dirty gate and dirty-path invalidation. The release gate must continue to accept only the canonical worktree-local report as evidence.
- Unchanged accepted risks from the initial review: silent provider drift behind an unchanged hosted alias is visible only through evidence age, and reuse locality to one worktree is by declaration rather than binding.

### Notes for executor

- Finding 1 (preflight fixture) verified: the fixture now materializes every manifest-declared non_behavioral path, staying tied to the manifest instead of duplicating literals; suite passes.
- Finding 2 (hook boundary) verified: task_context() scopes origin-vault resolution to start_turn/finish_turn/clear_stale only; SessionStart context, skill-router emission, command capture, plan capture, PostCompact output, and stop_pipeline are all gated on non-task contexts, and an invalid .task-meta.json origin fails closed before any coordinator behavior. The stop-probe regression test proves stop.sh is not invoked from a task worktree.
- Finding 3 (acceptance fail-open) verified: changed_paths always unions staged/unstaged/non-ignored-untracked paths (same-commit shortcut removed, quoted paths unquoted), and canonical reports refuse to execute or resume with any dirty path outside the exact non-behavioral allowlist - which also closes the worktree-hash/HEAD-execution asymmetry, since canonical evidence can only be recorded on a clean behavioral tree. The four new tests cover dirty non-behavioral reuse, same-commit dirty unknown block, dirty declared-dependency block, and the dirty-then-commit sequence now re-executing its cells. conversation-readonly dependencies were extended with the telemetry modules.
- Findings 4-7 verified: hook tests strip role/identity env vars in both invoke() and the task fixture; pipeline-stats adds the per-role completed/incomplete turn table with p50/p95 from completed turns only and emit_incomplete no longer fabricates a duration; the baseline basis label is version-neutral in generator and committed file with a regression assertion; the unreachable schema-1 reuse branch is gone and schema-1 evidence always requires --restart.
- The additional mechanism repair (spawn_review clearing a stale .review-outbox.json with other round artifacts) matches the write failure observed at the start of the initial review round and is covered by the updated start-clears-stale-round-artifacts test.

## Archive boundary

This page keeps validated review findings, executor resolutions, and final verification. Dedicated raw prompts, compressed callback payloads, command logs, sockets, and cmux identifiers are intentionally excluded. Validated findings and executor resolutions are retained as review evidence.
