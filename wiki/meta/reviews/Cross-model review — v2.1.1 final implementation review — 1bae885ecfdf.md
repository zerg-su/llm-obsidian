---
type: review
title: "Cross-model review — v2.1.1 final implementation review — 1bae885ecfdf"
address: c-000018
created: 2026-07-20
updated: 2026-07-21
tags:
  - review
  - cross-model
status: resolved
sessions:
  - "019f6ddd-d07e-7a30-b018-f6358753fb91"
review_id: "abc057a6-1acf-431e-86bf-cda80cdd2cff"
reviewer_runtime: "claude"
reviewer_model: "fable"
reviewer_effort: "high"
review_mode: "full"
rounds: 2
verdict: approve
---

# Cross-model review — v2.1.1 final implementation review — 1bae885ecfdf

> [!abstract] Outcome
> **Task:** v2.1.1 final implementation review
> **Final verdict:** `approve`
> **Reviewer:** claude · fable · effort `high`
> **Executor:** codex
> **Mode:** `full` · **rounds:** 2
> **Started:** 2026-07-20T23:03:11Z
> **Updated:** 2026-07-21T04:37:57Z

## Review request

Review the implementation for **v2.1.1 final implementation review** in `task/v2.1.1-code-owned-optimizations` against `v2.1.0` using the `full` cross-model gate.

> [!quote] Original task request
> Perform a full, critical release-gate review of the committed implementation on
> `task/v2.1.1-code-owned-optimizations` against `v2.1.0`. The approved plan is
> `docs/plans/v2.1.1-code-owned-optimization-plan.md`; the current review target
> ends at commit `3f08c5a`.
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
> - the post-approval acceptance invalidation refactor in `cd14a20`: code-owned
>   `tests/` classification, explicit `.gitignore` behavior dependency, separate
>   orchestration contract version, shared cmux support primitives, and
>   scenario-scoped supervisor/lifecycle dependencies.
> - the complete post-`cd14a20` delta through `3f08c5a`: bounded live-acceptance
>   concurrency/workspace sharding, evidence migration and effort provenance,
>   protected-research and lifecycle recovery, code-owned review fixtures,
>   operation-scoped review resolutions, and canonical fragment-free URL source
>   identity enforced at the shared vault-write boundary.
>
> Critically check correctness, security, privacy, stale-evidence risk, task/lane
> identity, surface cleanup, failure recovery, backward compatibility, missing
> tests, and any place where moving work into code could reduce result quality.
> Unknown changed paths must remain fail-closed. Both exact and prefix
> non-behavioral classifications must be code-restricted and unable to hide
> product changes. Confirm that compatible orchestration-only changes reuse live
> evidence while an orchestration version mismatch fails closed, and that moving
> shared cmux functions does not alter runtime behavior.
>
> Fable previously approved the complete branch only through `d27da21`; no later
> commit should be treated as reviewed. The executor ran the complete hermetic
> suite and focused regressions on the committed deltas. A live matrix was
> started too early after the shared `vault-write.py` change and must not be
> treated as a substitute for this review. Independently inspect the complete
> branch, with special attention to `d27da21..3f08c5a`, and run relevant
> pre-approved tests. Check that URL re-ingestion updates exactly one canonical
> source page/address, rejects duplicate owners and fragment-bearing manifest
> keys without breaking valid local-file ingestion or transactional recovery.
> Return actionable file/line findings. Approve only if the dependency graph
> cannot reuse stale behavior and the complete branch is ready for final
> acceptance without quality loss.

## Round 1 — changes-requested

- Phase: `initial-review`
- Run ID: `abc057a6-1acf-431e-86bf-cda80cdd2cff`
- Received: 2026-07-20T23:16:04Z

### Findings

#### 1. blocking — dispatch-review-reap cells can reuse stale evidence across changes to subprocess dependencies such as vault-write.py

- File: `config/acceptance-cells.toml:88`
- Evidence:
> The evidence-reuse invariant (stated in tests/test_release_acceptance.py:373-376) is: a path declared by ANY cell loses unknown-path protection, so every cell must declare its complete transitive dependency closure. The import-closure test only walks Python `import` statements, but reap-runner.py (a declared dispatch-review-reap dependency) shells out to scripts/vault-write.py (reap-runner.py:257), scripts/validate-vault.py (:268), scripts/current-session-id.sh (:75), scripts/parse-wiki-summary.py (:107), scripts/archive_task_reviews.py (:92), and scripts/reindex.py (:267). Of these, vault-write.py, validate-vault.py, and current-session-id.sh are declared by OTHER scenarios (agenda-carry, vault-capture, vault-maintenance, unsafe-web), so a change to them is not an unknown path, does not enter the dispatch-review-reap cell fingerprints, and lets dispatch/reap/review live evidence be reused as 'reused-identical'. The same class applies to skills/review-send/scripts/send_review.py (invoked by cmux_agent_supervisor.py:903, a declared dep) and skills/reap-send/scripts/send_reap.py (exercised by the dispatch cell's integrated lifecycle) which are fingerprinted only for their own skill's cells. Concretely, dispatch-review-reap evidence recorded before the 3f08c5a vault-write.py change would survive it, which is exactly the too-early-live-matrix scenario this review was asked to rule out.
- Recommendation:
> Add the missing subprocess-edge dependencies to scenarios.dispatch-review-reap (at minimum scripts/vault-write.py, scripts/validate-vault.py, scripts/current-session-id.sh; also the cross-skill send_reap.py/send_review.py scripts for the cells whose lifecycle executes them, plus reap-runner's other helper scripts so they stop relying on unknown-path over-invalidation). Then extend the closure audit in test_release_acceptance.py beyond `import` statements to also flag repo-script references in subprocess argv (or assert per-scenario subprocess edges explicitly), so the next subprocess-composed dependency cannot silently escape both unknown-path protection and the cell fingerprint. Rerun or restart the affected live cells afterwards.

#### 2. warning — The live runner defines test behavior (fixtures, prompts, cleanup proofs) yet is fingerprint-exempt as orchestration

- File: `scripts/live-acceptance-runner.py:453`
- Evidence:
> config/acceptance-cells.toml classifies scripts/live-acceptance-runner.py as an orchestration dependency ('evidence selection/checkpointing, not live cell behavior'), and the test suite pins 'acceptance orchestration is absent from cell behavior fingerprints'. But since aec5991 this file also contains behavior-defining content: review_acceptance_fixture/dispatch_acceptance_fixture fixture text and provisioning, prompt_text/review_fixture_prompt acceptance prompts, and the lifecycle/daily cleanup-proof validators. Editing any of these changes what a live cell actually verifies, yet reuses all prior evidence as 'reused-identical' unless a human remembers to bump runner_contract_version or orchestration_contract_version. The fail-closed property is convention-enforced for this one file while being code-enforced everywhere else.
- Recommendation:
> Split the behavior-defining parts (fixture provisioners, prompt templates, cleanup-proof validators) into a module that is declared as a scenario/global dependency, or include a code-owned digest of those exact functions in the cell fingerprint, leaving only selection/checkpoint/surface mechanics fingerprint-exempt. If deferred, at minimum extend the TOML comment and reviewer checklist to require a runner_contract_version bump for any prompt/fixture/proof change in this file.

#### 3. nit — A single legacy source page with an unparseable source\_url blocks every future source-page write

- File: `scripts/vault-write.py:518`
- Evidence:
> validate_unique_source_urls scans all wiki pages and only tolerates OSError/UnicodeError; canonical_source_url raises PayloadError for a pre-v2.1.1 source page whose source_url has a non-HTTP scheme, embedded credentials, or malformed syntax, and that error aborts every subsequent transaction that writes any source page, even for unrelated URLs. The message names the offending legacy page, so it is actionable, but the failure surfaces on an unrelated ingest rather than on the broken page itself.
- Recommendation:
> Acceptable as deliberate fail-closed behavior; consider adding the repair hint (fix or retype the named legacy page) to the error text, or reporting all invalid legacy source URLs in one pass instead of failing on the first.

### Executor resolution

> [!note] Resolution snapshot
> # Executor resolution
>
> Review run: `abc057a6-1acf-431e-86bf-cda80cdd2cff`
> Resolved commit: `3eacb48`
>
> ## Finding 1 — applied
>
> The acceptance dependency closure now discovers repo-owned subprocess script
> edges recursively in `scripts/acceptance_fingerprints.py`. The
> `dispatch-review-reap` manifest also declares the reviewed `send_reap.py` edge.
> Regression tests cover the reap helper chain plus `send_review.py` and
> `send_reap.py`, including unchanged-dependency migration and changed-dependency
> invalidation.
>
> ## Finding 2 — applied
>
> Behavior inside `scripts/live-acceptance-runner.py` now contributes a code-owned
> per-row digest: stable common behavior, exact rendered prompt, and only the
> relevant scenario/skill fixture and proof functions. Historical reuse renders
> the same digest from the prior commit's runner source and fails closed on
> classification drift. Tests prove scenario-scoped and common invalidation.
>
> ## Finding 3 — rejected as unnecessary
>
> The legacy invalid `source_url` behavior remains deliberately fail-closed and
> already names the offending page. Changing shared `vault-write.py` error
> aggregation is not required for correctness or this release and would expand
> the acceptance scope without improving the verified result.
>
> ## Additional user-approved delta
>
> `dispatch-workspace` is an additive thin skill over the existing dispatch
> contract. Classic dispatch still defaults to `split`; explicit workspace
> placement creates one unfocused workspace in the coordinator's exact window and
> reuses the existing lifecycle/review/reap machinery. Unit and acceptance fixture
> tests cover exact anchoring, placement validation, metadata, and no focus steal.
>
> ## Verification
>
> - `make test` — passed (`All tests passed`).
> - `python3 tests/test_release_acceptance.py` — passed.
> - `python3 tests/test_live_acceptance_runner.py` — passed.
> - `python3 tests/test_task_sessions.py` — passed.
> - `python3 tests/test_dispatch_runner.py` — passed.
> - skill-creator `quick_validate.py` — passed for `dispatch-workspace`.
> - Worktree is clean at `3eacb48` apart from review handoff files.

### Verification gaps

- The task prompt binds this review to 3f08c5a, but the branch HEAD is 65caf90 with four additional commits (106e46b, ee0d851, 9ced64e, 65caf90, ~970 lines of acceptance/research-isolation hardening). I reviewed through HEAD 65caf90; final acceptance must bind that exact SHA in validated review history, not 3f08c5a.
- This is a hermetic review: I ran the full pre-approved test surface but did not execute any live acceptance cells. Because of the blocking dependency-graph finding, the early-started live matrix cannot be trusted for dispatch-review-reap rows even where fingerprints match; the final gate needs those cells re-run (or --restart) after the dependency fix at the accepted SHA.

### Residual risks

- Silent provider drift behind an unchanged hosted model alias is accepted by design (no TTL); mitigated only by visible evidence age in schema-2 rows and --explain-selection.
- Both contract-version knobs (runner_contract_version, orchestration_contract_version) are manual; the fail-closed guarantee for the five orchestration files depends on reviewer discipline rather than content hashing.
- Evidence reuse trusts the local worktree's report file; row integrity hashing detects accidental edits but is not cryptographic protection against a deliberate local actor, which is consistent with the single-user threat model.

### Notes for executor

- All pre-approved suites pass on HEAD 65caf90: test_release_acceptance, test_live_acceptance_runner, test_research_isolation, test_task_sessions, test_task_lifecycle, test_journal_write, test_vault_scripts.sh, test_runtime_hooks, test_review_operation_namespacing, test_queue_session_exit, test_reap_send_runner, test_review_dispatch.sh, test_dispatch_resolver, test_session_preflight, test_skill_budget, test_pipeline_events, test_claude_subscription, test_codex_adapter.sh, and lint-instructions. The two ERROR: lines in test_task_lifecycle output are expected stderr from negative tests (archive tamper / failed-review accounting).
- Verified sound as implemented: canonical fragment-free URL identity and duplicate-owner rejection at the vault-write boundary (including the deterministic empty-manifest first-ingest baseline); environment-scope-v2 migration is tamper-resistant because both the legacy and rescoped fingerprints must match integrity-protected row data; orchestration migration rejects environment drift and changed current dependencies; non-behavioral exact paths and the tests/ prefix are hard-restricted in code; the workspace supervisor is bounded (max 5x5), seeds/merges shard reports atomically, and fails closed on conflicting shard evidence; the post-target research-isolation watcher no longer executes the agent-writable notify.py and delivers callbacks code-owned with an exclusive claim; reap/review callbacks are idempotent via O_EXCL delivery claims; drive resolutions are operation-scoped and ambiguity fails closed.
- Minor observation, no action needed: the cmux_trust_prompt.py move slightly changed the Codex trust-dialog marker set (added 'No, quit', relaxed to 'Press enter'). That is a small intentional behavior delta relative to the 'pure function move' description, and it is covered by updated tests.

## Round 2 — approve

- Phase: `verify-fixes`
- Run ID: `7eec3d1d-dac0-4cf9-aeb2-0b36dc3f913f`
- Received: 2026-07-21T04:37:57Z

### Findings

#### 1. warning — Undeclared evidence-policy loosening: claude/codex CLI versions are collapsed to major.minor in cell fingerprints

- File: `scripts/acceptance\_fingerprints.py:474`
- Evidence:
> The resolution commit 3eacb48 adds compatible_runtime_version and applies it to the claude/codex keys inside scoped_environment_contract, so an agent CLI patch release (e.g. Claude Code 2.1.205 -> 2.1.206) no longer invalidates any live acceptance evidence. This is deliberate and test-covered ('runtime patch releases share one acceptance compatibility line'), bounded to the two CLI keys (cmux and OS stay exact, minor/major bumps still invalidate), and arguably necessary to keep precise reuse usable given frequent CLI patch releases. But it is a substantive weakening of the plan's 'runtime and cmux versions' fingerprint input, and the executor resolution does not mention it: it declares only the two finding fixes and the user-approved dispatch-workspace delta. CLI patch releases can change agent behavior, so this compatibility line is a risk-acceptance decision the coordinator/user should make knowingly, not discover later.
- Recommendation:
> No code change required if the policy is intended. Surface it explicitly: acknowledge this loosening in the resolution/record for the final acceptance decision, and document the major.minor compatibility line (and its rationale) next to the environment-scope comment in config/acceptance-cells.toml or the plan's residual-risk list alongside the accepted provider-alias drift.

#### 2. nit — New dispatch-workspace skill is missing from the CLAUDE.md skill inventory

- File: `CLAUDE.md:7`
- Evidence:
> 3eacb48 adds skills/dispatch-workspace (registered in the acceptance registry, skill-body baseline, and live-runner classification), but the '**Skills:**' inventory line in CLAUDE.md still ends at the previous 28-skill set, so the operating manual no longer matches installed skills.
- Recommendation:
> Add `/dispatch-workspace` to the CLAUDE.md skills line (and the getting-started catalog if it enumerates skills) in the next docs touch.

### Executor resolution

No resolution was required or recorded for this round.

### Verification gaps

- Hermetic verification only: I re-ran the pre-approved suites at 3eacb48 (release_acceptance, live_acceptance_runner, task_sessions, dispatch_runner, skill_budget, task_lifecycle, lint-instructions — all pass, matching the executor's make test claim) but executed no live acceptance cells. The release gate still needs the complete live matrix at 3eacb48; because every dispatch-review-reap fingerprint changed (new subprocess dependencies plus the behavior digest), prior evidence now either migrates only when provably unchanged or reruns, which structurally retires the earlier too-early-matrix concern.
- Final acceptance must bind exactly 3eacb48; both earlier target SHAs (3f08c5a, 65caf90) are superseded.

### Residual risks

- The runtime-script-edge detector is a heuristic: it scans top-level functions that call run/run_checked/runner/subprocess.* for suffix-bearing string literals, so a repo script invoked from a class method, module level, or via a name built without a .py/.sh literal would escape detection. Undeclared scripts remain protected by unknown-path invalidation; the residual gap needs a script declared by another cell AND an undetectable invocation pattern, and the known edges are pinned by explicit regression assertions.
- The live-runner behavior classification tables (common/scenario/skill function maps) live in orchestration code outside the fingerprint; a deliberately wrong classification of an existing function could under-invalidate, though any new or renamed function fails closed and prompt-level drift is caught row-exactly by the rendered-prompt digest.
- CLI patch releases within one major.minor line and provider-side drift behind unchanged hosted model aliases both reuse evidence by design; evidence age remains the only visibility for both.
- Row integrity hashing remains tamper-evidence against accidental edits, not cryptographic protection against a deliberate local actor — unchanged, consistent with the single-user threat model.

### Notes for executor

- Finding 1 (blocking) — verified resolved. expanded_dependencies now recursively folds in runtime_script_references (AST scan of process-invoking functions for repo-script literals, with unique-basename resolution), the manifest declares the send_reap.py edge for dispatch-review-reap, and the closure test now walks runtime edges and explicitly asserts the full reap helper chain (vault-write.py, validate-vault.py, current-session-id.sh, parse-wiki-summary.py, archive_task_reviews.py, allocate-address.sh, reindex.py) plus the supervisor->send_review.py edge. The rebuilt resume migration correctly reuses prior evidence only when newly discovered dependencies are unchanged since the provenance commit and reruns when they changed — both directions test-pinned.
- Finding 2 (warning) — verified resolved. live_runner_behavior_sha256 enters every cell fingerprint and covers module-level constants, common functions, row-scoped scenario/skill functions, and the exact rendered per-row prompt; set-equality classification fails closed on any new or renamed runner function; historical migration renders the digest from the prior commit's runner source via git show. Tests prove scoped invalidation (review prompt edit invalidates only review cells), common invalidation, and that a committed runner behavior change reruns every cell. The VAULT_REINDEX_SCENARIOS prompt scoping is captured row-exactly by the rendered-prompt digest.
- Finding 3 (nit) rejection accepted: the fail-closed legacy source_url behavior names the offending page and stands as designed.
- dispatch-workspace (declared user-approved delta) — reviewed clean: placement is validated fail-closed to split|workspace at request, contract, and fixture layers with a backward-compatible split default; spawn_workspace anchors to the origin surface's exact window with --focus false and requires exactly one child surface; the skill reuses the dispatch contract and forbids manual cmux workspace creation; acceptance registry, live-runner classification, and skill-body baseline all updated; Claude/Codex plugin discovery is directory-based, so no manifest regeneration was needed.

## Archive boundary

This page keeps validated review findings, executor resolutions, and final verification. Dedicated raw prompts, compressed callback payloads, command logs, sockets, and cmux identifiers are intentionally excluded. Validated findings and executor resolutions are retained as review evidence.
