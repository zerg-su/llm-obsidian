---
type: review
title: "Cross-model review — v2.1.1 final implementation review — 06aeda67d29d"
address: c-000017
created: 2026-07-20
updated: 2026-07-20
tags:
  - review
  - cross-model
status: resolved
sessions:
  - "019f6ddd-d07e-7a30-b018-f6358753fb91"
review_id: "0d292af0-b018-4263-929a-74bdc8037c96"
reviewer_runtime: "claude"
reviewer_model: "fable"
reviewer_effort: "high"
review_mode: "full"
rounds: 2
verdict: approve
---

# Cross-model review — v2.1.1 final implementation review — 06aeda67d29d

> [!abstract] Outcome
> **Task:** v2.1.1 final implementation review
> **Final verdict:** `approve`
> **Reviewer:** claude · fable · effort `high`
> **Executor:** codex
> **Mode:** `full` · **rounds:** 2
> **Started:** 2026-07-20T20:45:31Z
> **Updated:** 2026-07-20T21:18:11Z

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
- Run ID: `0d292af0-b018-4263-929a-74bdc8037c96`
- Received: 2026-07-20T20:56:57Z

### Findings

#### 1. blocking — The detached callback watcher executes agent-writable notify.py unsandboxed on the coordinator side, breaking the protected-research isolation boundary.

- File: `scripts/research-isolation.py:937`
- Evidence:
> cmd_watch_callback (added in fdf4bdc, after the last approved commit d27da21) runs `subprocess.run(\[str(notifier)\], cwd=workspace, env=callback_env, ...)` where `notifier` is `workspace / 'notify.py'` inside the stage scratch directory that is the sandboxed stage agent's writable cwd (Codex is launched with `--cd workspace --ask-for-approval never`). The watcher checks only is_symlink/is_file once, never the file's content. The feature's explicit threat model treats fetched content and the synthesis artifact as UNTRUSTED DATA and denies the synth agent all outbound network; a prompt-injected stage agent can rewrite notify.py, write a valid artifact.json/complete.json (callback_input_ready validates only those), skip its own callback, and within the 30s grace have the coordinator-side watcher execute attacker-chosen code with no sandbox, full network, and full user/vault access. This escalates from the deliberately networkless/contained stage sandbox to unrestricted execution, defeating the fetch/synth split.
- Recommendation:
> Do not execute the workspace file from the watcher. Either (a) have the watcher deliver the callback itself in-process (write the marker payload and run the exact cmux send commands it already knows from state), or (b) record the SHA-256 of the rendered notifier text in the coordinator-owned state file at write_notifier time and have the watcher verify the file hash immediately before each execution, invoking it via the pinned interpreter rather than the shebang. Add a regression test where notify.py content is tampered after launch and the watcher refuses to run it.

#### 2. warning — The compatible-orchestration-migration reuse path skips the fingerprint's environment/toolchain guarantees.

- File: `scripts/release-acceptance.py:304`
- Evidence:
> When a prior row's dependency list is a strict superset whose removed entries are all orchestration paths, load_resume_results reuses the row even though `raw_fingerprint != current\['cell_fingerprint'\]`. The acceptance criteria for that branch check only dependency-set relations, generation equality, and that no changed path intersects current dependencies. The fingerprint also encodes environment_contract() (OS, architecture, cmux/claude/codex CLI versions) and runner_contract_version; none of those are re-verified on the migration path. During the one-time migration window, evidence recorded under a different codex/claude/cmux version or OS release can be silently re-stamped with the current fingerprint (decorate_result overwrites cell_fingerprint with current metadata), permanently laundering the environment drift out of the evidence chain.
- Recommendation:
> Store the content-free environment snapshot (or its hash) in each row's provenance and require equality on the migration branch, or recompute and compare a prior-shape fingerprint using the current environment so only the dependency-list difference is tolerated. Add a negative test: orchestration-dependency removal plus a changed CLI version string must re-execute the cell.

#### 3. warning — A dead, behaviorally divergent copy of workspace\_trust\_prompt\_visible is retained solely to preserve the evidence fingerprint.

- File: `scripts/cmux\_agent\_support.py:94`
- Evidence:
> 8cf95f8 moved both runtime consumers (cmux_agent_supervisor.py, live-acceptance-runner.py) to the new cmux_trust_prompt.py, whose Codex markers are ('...', 'Yes, continue', 'No, quit', 'Press enter'), then reverted the support-module copy to the old markers ('Press enter to continue'); 2e35d61 adds a trailing newline explicitly to pin the file hash. Nothing imports the support copy, and only test_task_lifecycle.py:70 guards the supervisor binding (not the live runner's). The stale copy has no in-file marker saying it is dead, so a future editor fixing trust-prompt recognition can plausibly patch the wrong function and see no behavior change, while the real matcher drifts.
- Recommendation:
> Acceptable as a one-release fingerprint-preservation measure, but schedule removal of the dead function in the first post-release change that already invalidates cmux_agent_support.py-dependent evidence, and extend the module-binding regression guard to cover live-acceptance-runner's import as test_task_lifecycle.py:70 does for the supervisor.

#### 4. nit — A complete green sharded run never refreshes .vault-meta/acceptance/model-generations.json.

- File: `scripts/acceptance-workspace-supervisor.py:420`
- Evidence:
> release-acceptance.py writes the generation snapshot only on its own canonical complete green report (line 638). The workspace supervisor writes the merged canonical report through checkpoint()/write_json directly, so a fully sharded green matrix leaves the snapshot stale and session-preflight keeps proposing `make acceptance-live` for an already-retested generation until a (cheap, fully-reused) single-runner resume run rewrites it.
- Recommendation:
> After the final merge, when summary.complete and failed == 0 and the report is canonical, write generation_snapshot() the same way release-acceptance.py does.

#### 5. nit — Notifier crash between a successful cmux send and the marker write can produce a duplicate coordinator callback.

- File: `scripts/research-isolation.py:573`
- Evidence:
> The rewritten notifier now writes notify-complete.json only after both cmux commands succeed (correct for retryability), but if the process dies between the successful Enter send and the marker os.replace, the claim file is unlinked in finally and a later watcher retry re-sends the full callback, typing a second command into the coordinator surface. cmd_receive's state guards make this visible noise rather than a safety issue.
- Recommendation:
> Tolerable; if revisited, write a local 'sent' sentinel before the marker or have the retry path check surface delivery, and document that a duplicate typed callback is expected to fail closed in receive.

### Executor resolution

> [!note] Resolution snapshot
> # Review Resolution — v2.1.1 final implementation review
>
> Verdict received: `changes-requested` (1 blocking, 2 warnings, 2 nits; full
> review; run `0d292af0-b018-4263-929a-74bdc8037c96`). All actionable findings
> were resolved before verification.
>
> ## Finding 1 — applied
>
> The detached protected-research callback watcher no longer executes the
> stage-agent-writable `notify.py`. The coordinator now receives the exact
> callback, surface, run identity, and marker path as immutable watcher argv and
> delivers the callback in-process after validating the stage output. A tampered
> notifier regression proves attacker-controlled notifier content is never run.
> The receive boundary also waits briefly for the trusted completion marker so a
> successful callback cannot race the marker publication into a stuck lane.
>
> ## Finding 2 — applied
>
> Acceptance evidence provenance now records `environment_sha256`, derived from
> the complete environment/toolchain contract. Compatible orchestration
> migration requires exact equality of that hash. A negative regression changes
> the prior environment while removing only orchestration dependencies and
> proves that every affected cell executes instead of being reused.
>
> ## Finding 3 — applied
>
> The dead divergent `workspace_trust_prompt_visible` implementation was removed
> from `cmux_agent_support.py`. Both the supervisor and live acceptance runner are
> bound to the canonical matcher, and the live-runner binding now has an explicit
> regression guard.
>
> ## Finding 4 — applied
>
> A complete green sharded acceptance merge now refreshes the canonical model
> generation snapshot. Incomplete, failed, and non-canonical reports cannot
> refresh it; positive and negative tests cover the boundary.
>
> ## Finding 5 — accepted residual behavior
>
> The unavoidable process-crash interval between a successful cmux Enter and
> durable marker publication can still type a duplicate callback. The exact
> receive state machine rejects that duplicate fail-closed, so this remains
> visible noise rather than a safety or state-integrity issue. Moving the marker
> before delivery would instead permit a lost callback, which is the worse
> failure mode. The new delivery claim prevents concurrent duplicates; no unsafe
> workspace code executes.
>
> ## Additional failures found by live acceptance — applied
>
> - Exact surface cleanup accepts proof that the entire anchored workspace
>   disappeared after close, while ambiguous layouts remain fail-closed.
> - Acceptance-only synthetic session identity is honored only when both guarded
>   acceptance environment variables are present and valid.
> - The review fixture now contains a behavior-preserving maintainability warning
>   rather than a real logic defect.
> - Lifecycle cleanup accepts the v3-contract-valid absence of `target_repo`, but
>   still rejects an explicitly foreign repository.
>
> ## Executor verification
>
> - `make test` — passed.
> - `python3 tests/test_research_isolation.py` — passed.
> - `python3 tests/test_release_acceptance.py` — passed.
> - `python3 tests/test_live_acceptance_runner.py` — passed.
> - `python3 tests/test_task_sessions.py` — passed (59 tests).
> - `python3 tests/test_task_lifecycle.py` — passed.
> - `bash tests/test_review_dispatch.sh` — passed (160 tests).
> - Review operation namespacing, session preflight, pipeline events,
>   queue/session exit, reap-send, and instruction lint — passed.
> - `git diff --check` — passed.

### Verification gaps

- No test covers a stage agent tampering with notify.py after launch; the watcher tests only exercise cooperative retry (tests/test_research_isolation.py 'code-owned watcher retries validated callback').
- No test covers environment/CLI-version drift coinciding with the compatible-orchestration-migration reuse path; existing tests hold the environment fixed.
- The live matrix that was started early after the shared vault-write.py change was not treated as review evidence here; a fresh affected-cell live run is still required after the blocking fix, since research-isolation.py is a declared dependency of protected-web, unsafe-web, and ingest scenarios.

### Residual risks

- Silent provider drift behind an unchanged hosted model alias is deliberately accepted (no TTL); mitigation is only evidence-age visibility in schema 2 and --explain-selection, as the plan states.
- Evidence reuse trusts the local worktree's report file plus row_integrity_sha256, which is tamper-evident against accident, not against a deliberate local actor; that matches the single-user trust model but should not be treated as cryptographic provenance.
- The fingerprint-preservation pattern (freezing a shared file byte-for-byte while moving changed logic to a new path) is powerful and easy to misuse in future releases; it worked here only because the frozen copy is genuinely unreachable at runtime.

### Notes for executor

- Everything else in d27da21..3f08c5a held up well under adversarial reading: the canonical fragment-free URL identity is enforced at the shared vault-write boundary for both pages and manifest keys, first-ingest is permitted only against the exact empty-manifest baseline hash, local-file ingestion and journal roll-forward are untouched (vw-source-url-*, vw-manifest-url-fragment-*, vw-crash/roll-forward tests green).
- The reuse dependency graph is otherwise fail-closed: schema-1 and orchestration-version mismatches hard-fail, unknown changed paths (including staged/unstaged/untracked via dirty_paths) invalidate everything, non-behavioral exact paths and the tests/ prefix are code-restricted in acceptance_fingerprints.py, and .gitignore is a global fingerprint dependency.
- Shard evidence validation in the workspace supervisor pins source commit, matrix fingerprint, orchestration version, per-row integrity, and skill assignment; the LLM_OBSIDIAN_ACCEPTANCE_SOURCE_COMMIT pin is validated and cannot be inherited stale (tests cover both).
- All pre-approved suites pass locally: test_release_acceptance, test_live_acceptance_runner, test_research_isolation, test_task_lifecycle, test_task_sessions, test_vault_scripts, test_review_dispatch, test_runtime_hooks, test_journal_write, test_review_operation_namespacing, test_reap_send_runner, test_queue_session_exit, test_session_preflight, test_skill_budget, test_dispatch_resolver, test_pipeline_events, test_claude_subscription, test_codex_adapter, and lint-instructions.

## Round 2 — approve

- Phase: `verify-fixes`
- Run ID: `be8af50b-1176-4388-a9c3-d87df5d9cdd7`
- Received: 2026-07-20T21:18:11Z

### Findings

No findings.
### Executor resolution

No resolution was required or recorded for this round.

### Verification gaps

- None

### Residual risks

- The fix commit touches research-isolation.py, cmux_agent_support.py, cmux_surface_lifecycle.py, task_sessions.py, and live-acceptance-runner.py; the global-dependency changes invalidate the entire prior live evidence set by design. A fresh full live matrix (not the early pre-fix run) is required before final release acceptance — the fingerprint system will enforce this, but budget for a complete rerun rather than selective reuse.
- The watcher still writes resume-checkpoint.json into the agent-writable workspace and later resume paths read checkpoint id/cwd from it; contents are data-only, regex-validated, and cwd is re-validated by prepare_stage_workspace, so this is bounded, but it remains the one coordinator-consumed file inside the stage scratch.
- Previously noted accepted risks stand: no-TTL provider drift behind unchanged hosted aliases is surfaced only via evidence age, and row_integrity_sha256 is tamper-evident against accident, not a deliberate local actor.

### Notes for executor

- Finding 1 (blocking) resolved in 106e46b: cmd_watch_callback no longer executes any workspace file. The coordinator surface, callback text, marker path, and run id are pinned as immutable watcher argv at launch, and deliver_watched_callback performs the cmux send and marker publication in-process with symlink refusal, payload-equality idempotency, and a claim-file race guard. The regression test plants an attacker-controlled notify.py in the workspace and proves the watcher never runs it ('code-owned watcher never executes agent-writable notifier'), while the cooperative-retry and checkpoint-preservation tests still pass. The new wait_for_completion_marker gate in cmd_receive correctly closes the send-before-marker race introduced by delivering before publication, and the marker-race test covers it.
- Finding 2 (warning) resolved: cell_metadata now emits environment_sha256, decorate_result records it in each executed row's provenance (integrity-protected), and the compatible-orchestration-migration branch requires exact equality. Pre-fix rows lacking the field yield None and fail the equality check, so legacy evidence cannot migrate — fail-closed in the right direction. The negative test ('orchestration migration rejects environment drift') proves all four cells re-execute under a mismatched hash while the happy-path migration test still passes.
- Finding 3 (warning) resolved beyond the suggested scope: the dead divergent workspace_trust_prompt_visible was removed from cmux_agent_support.py entirely rather than deferred, and the live runner now has the same module-binding guard as the supervisor ('live runner uses the canonical trust prompt matcher'). Note this changes a global fingerprint dependency and therefore invalidates all prior live evidence — correct and conservative, since the watcher fix already invalidated the research-dependent scenarios.
- Finding 4 (nit) resolved: refresh_generation_snapshot writes the snapshot only for canonical reports under .vault-meta/acceptance with summary.complete true and failed == 0, called both on the already-complete early return and after the final green merge; positive and negative tests cover the boundary.
- Finding 5 (nit): the accepted-residual rationale is sound. Publishing the marker before delivery would trade a duplicate visible callback for a silently lost one, which is the worse failure mode; the receive state machine rejects the duplicate fail-closed and the claim file prevents concurrent duplicates. No further change requested.
- The four additional live-acceptance fixes in the same commit were inspected and are sound: guarded two-variable acceptance session identity in cmux_surface_lifecycle (regression-tested), behavior-preserving redundant-f-string review fixture replacing the previous real logic defect, v3-valid absent target_repo accepted while an explicitly foreign repo is still rejected, and close_surface_exact accepting disappearance of the whole anchored workspace while ambiguous layouts stay fail-closed.
- Suites re-run green in this verify pass: test_research_isolation, test_release_acceptance, test_live_acceptance_runner, test_task_lifecycle, test_task_sessions (59), and lint-instructions. The two ERROR lines in test_task_lifecycle output are expected stderr from negative fixtures; the suite exits green.

## Archive boundary

This page keeps validated review findings, executor resolutions, and final verification. Dedicated raw prompts, compressed callback payloads, command logs, sockets, and cmux identifiers are intentionally excluded. Validated findings and executor resolutions are retained as review evidence.
