---
type: plan
title: "LLM Obsidian 2.6.7 — Bounded Harness Stabilization and Root-Scoped Observer"
address: c-000128
session_id: 019fab00-3160-7380-8920-4b20183afb76
sessions:
  - id: 019fab00-3160-7380-8920-4b20183afb76
    date: 2026-08-09
source_cwd: "/Users/zak/Projects/worktrees/llm-obsidian-2-6-7-stabilization"
status: pending
created: 2026-08-09
updated: 2026-08-09
tags:
  - plan
  - manual-save
---

# LLM Obsidian 2.6.7 — Bounded Harness Stabilization and Root-Scoped Observer

## Outcome Contract

```json
{
  "schema_version": 1,
  "purpose": "Turn the current v2.6.6 RC4-fix3 Harness into a narrow, evidence-backed bootstrap that can reliably execute one supported engineering lifecycle while exposing each run through its own read-only terminal observer.",
  "desired_outcome": "Starting from exact tag v2.6.6-rc4-fix3, release LLM Obsidian 2.6.7 as a stabilization-only version. One supported engineering/change corridor must progress from executor output through summary, scoped verification, one-lane review, at most five product correction cycles, refreshed summary, re-verification, approval, reap, and resource-free cleanup without coordinator prompting after supported durable artifacts appear. Product cycles 1–3 use a fresh-context Fable High executor/reviewer route; after the third material failure a read-only Sol X-High structural pivot analyzes the three failed attempts, cycles 4–5 apply and verify the resulting structural patch, and a sixth product cycle has zero model/session/ledger effect. Mechanism failures remain separately bounded and do not consume product cycles. RC1 stability is bound to a deterministic lifecycle behavioral digest rather than Git HEAD. RC2 opens one external, user-closed, root-scoped terminal observer per request_id before dispatch start, and RC3 proves the release candidate first sequentially and then with two concurrent opposite-runtime pipelines.",
  "success_evidence": [
    {
      "evidence_id": "E267.RC1.CORRIDOR",
      "observable": "A deterministic production-core scenario completes engineering/change through summary, scoped verify, Simple review, findings publication, one fix, refreshed summary, re-verify, approval, reap, and resource-free cleanup with one provider identity per authorized effect and no coordinator resume path."
    },
    {
      "evidence_id": "E267.RC1.CRASH_MATRIX",
      "observable": "A model-free crash/restart matrix uses the real OperationStore, worker/FSM, callback and finalization owners with fake provider/process/cmux/clock adapters at every critical durable boundary; every restart converges without duplicated provider, callback, review, cleanup, or ledger effect."
    },
    {
      "evidence_id": "E267.RC1.PRODUCT_BUDGET",
      "observable": "Only a complete fix-to-verify-to-review attempt ending in material findings consumes one of five product cycles. Mechanism failure, callback stall, provider transport failure, test flake, or safe restart consumes no product cycle. Cycles 1–3 stay on fresh-context Fable High, the exact third material failure publishes a bounded structural pivot packet for Sol X-High, cycles 4–5 use that pivot, and a sixth reservation has zero model, session, and ledger effect."
    },
    {
      "evidence_id": "E267.RC1.SUBJECT_DIGEST",
      "observable": "A deterministic lifecycle_subject_sha256 includes production Harness/dispatch/review code, behavioral runtime config, schemas, hooks, and behavior-changing skills while excluding wiki, ordinary docs, release metadata, evidence, tests, and user Obsidian state. Only a changed behavioral digest resets the live streak."
    },
    {
      "evidence_id": "E267.RC1.LIVE_STREAK",
      "observable": "Three consecutive fresh live runs on one lifecycle_subject_sha256 use new request/owner identities, stores, worktrees, and provider sessions; at least one run completes the real material-finding/fix/re-review path, all three terminate resource-free without coordinator recovery, and any intervening behavioral digest change resets the streak to zero."
    },
    {
      "evidence_id": "E267.RC1.RELEASE_STOP",
      "observable": "A typed RC1 defect ledger groups reproductions by root-cause class. Three independent new lifecycle defect classes outside the known callback/finalization seam stop further 2.6.7 recovery expansion, preserve reproducers, and yield a kernel-rewrite decision boundary instead of a fourth recovery mechanism."
    },
    {
      "evidence_id": "E267.RC2.ROOT_PROJECTION",
      "observable": "Normal dispatch opens dashboard projection for exactly request_id and its descendants before validate/start. Early validation/start failure is visible, no unrelated owner or historical attention record is rendered, and global all-owner projection remains available only through an explicit diagnostic mode."
    },
    {
      "evidence_id": "E267.RC2.SPLIT_IDENTITY",
      "observable": "Dashboard split identity is vault plus exact coordinator workspace plus request_id. Reopening one request reuses exactly one split; a second request creates a second split; ambiguous, stale, moved, or foreign markers fail closed without touching another surface."
    },
    {
      "evidence_id": "E267.RC2.EXTERNAL_OBSERVER",
      "observable": "The terminal observer remains read-only and absent from Harness OwnedResources. Observer creation failure cannot stop an already authorized pipeline, terminal output remains until the user closes it, and Harness cleanup never signals or closes the observer."
    },
    {
      "evidence_id": "E267.RC3.SEQUENTIAL",
      "observable": "One fresh sequential golden run on the final behavioral digest completes the full supported corridor and root-scoped observer lifecycle before concurrency is attempted."
    },
    {
      "evidence_id": "E267.RC3.PARALLEL",
      "observable": "Two fixed small engineering/change tasks run concurrently: Terra Medium executor with Opus High review and Sonnet Medium executor with Sol High review. Each has a distinct worktree, root identity, provider effects, callback lineage, and dashboard split; both reach an expected terminal state without cross-owner data or lifecycle effects."
    },
    {
      "evidence_id": "E267.RC3.CLEANUP",
      "observable": "After sequential and parallel acceptance, every Harness-owned provider, process, supervisor, review lane, worktree resource, and callback effect is terminal and resource-free; only the explicitly user-owned observer splits may remain."
    },
    {
      "evidence_id": "E267.RELEASE.GATE",
      "observable": "The final candidate binds the approved plan, Outcome Contract, lifecycle behavioral digest, RC1 streak, RC2 observer receipts, RC3 live receipts, full hermetic tests, coverage/quality checks, and one independent Fable High implementation review to one exact release HEAD without accepted unexplained deviation."
    }
  ],
  "non_goals": [
    "Migrating from Obsidian to Foam or another editor in 2.6.7.",
    "Implementing a new Harness or lifecycle kernel in 2.6.7.",
    "Implementing the deferred browser or HTTP dashboard.",
    "Cleaning, deleting, or migrating historical Harness owner records.",
    "Adding a new pipeline DSL, provider abstraction, lifecycle state, or positive authority source.",
    "Stabilizing every legacy, custom, Split, research, Deep, or Full lifecycle path.",
    "Broadly refactoring cli.py or the state machine outside a deterministic RC1 release blocker.",
    "Optimizing model cost, latency, or throughput when it is not required for the supported stability corridor."
  ]
}
```

## Release Boundary

- Base tag and commit: `v2.6.6-rc4-fix3` at `5ac90ef94029371507320d900f32c4c0b254d790`.
- Product repository: canonical `LLM Obsidian`; Swarm is dogfood/integration only and receives the release after canonical evidence is terminal.
- Execution topology: one long Harness dispatch with a Fable High executor. RC1, RC2, and RC3 are durable checkpoints in that one task, not independent implementation dispatches.
- Plan review topology: one independent fresh Fable X-High intent review before any implementation effect.
- Ordinary product cycles 1–3: fresh-context Fable High executor and Fable High reviewer.
- Pivot boundary: after exactly three complete product cycles ending in material findings, one read-only Sol X-High analysis consumes the bounded pivot packet. It performs no product mutation.
- Product cycles 4–5: Fable High implements the structural recommendation; Sol X-High reviews it. The fifth failure ends `finalization-budget-exhausted`.
- Coordinator-only actions: live provider acceptance, exact Harness-store recovery requiring coordinator write authority, publishing/tagging, and any permission/security/external-effect decision. The executor must raise a typed escalation and remain on the same dispatch boundary.

## Supported Corridor and Invariants

The sole required RC1 corridor is:

`engineering/change → summary → scoped verify → Simple review → findings → fix → refreshed summary → scoped verify → Simple review/approve → reap → cleanup`

1. Durable artifact/event identity is the only positive authority. Screen contents, elapsed time, missing processes, focused surfaces, or model prose may cause diagnosis or `attention-required`, never a repeated provider effect.
2. A product cycle is counted only after a complete product correction reaches a material `changes-requested` review result. Mechanism/transport/runtime failures are retained as evidence under a separate bounded recovery budget and do not advance the product cycle number.
3. `FinalizationLedger` remains the sole atomic reservation owner for product cycles 1–5. The lineage key binds origin task, approved plan digest, Outcome Contract digest, and product-cycle identity. Attempt receipts may record mechanism outcomes without consuming another product-cycle reservation.
4. RC1 live streak identity is `lifecycle_subject_sha256`, not Git HEAD. Documentation, wiki, release/evidence, test-only, and user-editor changes do not reset it.
5. The observer is a projection only. It has no transition, cleanup, provider, review, or authority role.
6. No slice may add a fallback recovery branch merely to make one incident green. A production mutation needs a deterministic pre-fix RED reproducer and one named transition owner.

## Work Strategy

- Use TDD slices and run focused tests before any broad suite.
- Keep fake adapters only at slow, privileged, nondeterministic, or external boundaries; use real policy, store, FSM, worker, callbacks, ledgers, and projections.
- Commit each green slice separately inside the single long execution dispatch.
- Do not start RC2 until RC1 deterministic evidence and the three-run live streak are terminal. Do not start RC3 until RC2 root-scoped observer acceptance is terminal.
- After each RC checkpoint, run one fresh Fable High implementation review against that checkpoint and resolve findings inside the same bounded product-cycle lineage.
- If RC1 reaches the three-class release stop rule, stop the dispatch with preserved evidence; do not continue to RC2.

## Slice 0 — Freeze the Stabilization Denominator

**files/responsibility**

- `config/v267-stabilization-subject.json`: exact behavioral path denominator and exclusions for the 2.6.7 lifecycle subject.
- `scripts/v267_stabilization.py`: deterministic subject digest, streak, defect-class, and release-evidence validation; no runtime transition authority.
- `tests/test_v267_stabilization.py`: independent expectations for inclusion/exclusion, reset behavior, streak ordering, and release stop classification.
- `docs/acceptance/v2.6.7-stabilization-contract.md`: human-readable evidence map and operator commands generated from the approved contract, without duplicating authority.

**consumes**

- Exact Fix3 base, this Outcome Contract, existing live-acceptance behavioral dependency rules, and current release evidence conventions.

**produces**

- One deterministic `lifecycle_subject_sha256` and a typed RC1 streak/defect ledger format that is independent of ordinary Git HEAD changes.

**failing evidence**

- A fixture where wiki/docs/evidence/test-only edits incorrectly reset the streak and a fixture where runtime/config/schema/hook/skill changes incorrectly preserve it.

**minimal green**

- Reuse existing tracked-tree hashing and live-acceptance dependency classification; add no general hashing framework or public API.

**refactor seam**

- Deduplicate only one proven behavioral-path classifier shared with `scripts/live-acceptance-runner.py`; keep release-specific policy in the versioned config.

**focused verification**

- `python3 tests/test_v267_stabilization.py`
- Existing live-acceptance contract tests covering behavioral dependency classification.

**covers**

- `E267.RC1.SUBJECT_DIGEST`, `E267.RC1.RELEASE_STOP`.

## Slice 1 — Complete the Production-Core Golden Corridor

**files/responsibility**

- `tests/harness/lifecycle_simulator.py`: model-free world actions for summary, scoped verification, review callback, findings, refreshed summary, approval, reap, and cleanup.
- `tests/harness/lifecycle_simulator_world.py`: production constructors with fake provider/process/cmux/clock ports only.
- `tests/harness/lifecycle_simulator_oracle.py`: singular invariants for callback identity, effect counts, lineage, terminal resources, and exact owner ancestry.
- `tests/harness/test_harness_control_plane.py`: one full supported engineering/change scenario through real policy owners.

**consumes**

- Existing simulator, RC4 transition certificate, OperationStore, runtime worker, review gate, callback, finalization, and reap contracts.

**produces**

- One readable deterministic scenario that is RED at the first actual unsupported handoff and proves the complete supported corridor after the owning fix.

**failing evidence**

- Pre-fix golden scenario must stop at the exact durable state currently requiring coordinator resume; a green scenario that skips findings/fix or uses a test-only transition is invalid.

**minimal green**

- Extend the existing simulator vocabulary and oracle only; do not build a second scheduler or duplicate production transition policy.

**refactor seam**

- Extract test helpers only when two corridor stages need the same fixture and the helper does not encode the expected production decision.

**focused verification**

- `python3 tests/harness/test_harness_control_plane.py`
- `python3 tests/harness/test_lifecycle_simulator_world.py`
- Existing simulator oracle checks.

**covers**

- `E267.RC1.CORRIDOR`.

## Slice 2 — Expand the Model-Free Crash and Historical Matrix

**files/responsibility**

- `tests/harness/test_lifecycle_crash_matrix.py`: before/after crash points at summary publication, verification receipt, callback acceptance, findings publication, resolution handoff, refreshed summary, approval, reap-finalize, and cleanup receipt.
- `tests/harness/test_lifecycle_regression_corpus.py`: frozen real failure fixtures and deterministic schedule replay.
- `tests/harness/lifecycle_scenarios/*.json`: only newly captured canonical failure cases, content-addressed and immutable.
- `tests/harness/lifecycle_scheduler.py` and `tests/harness/lifecycle_historical.py`: schedule support only if a required interleaving cannot be represented today.

**consumes**

- Slice 1 golden scenario and existing historical cases.

**produces**

- A complete, named durable-boundary matrix with zero real model/provider/cmux/network effects and replay-stable traces.

**failing evidence**

- Each new crash point must fail because restart cannot converge or duplicates an exact effect before production repair; merely arming a failpoint is not evidence.

**minimal green**

- Repair the transition owner, not the test scheduler; each crash resumes from the last durable prefix.

**refactor seam**

- Keep fault injection test-only and absent from production entrypoint signatures.

**focused verification**

- `python3 tests/harness/test_lifecycle_crash_matrix.py`
- `python3 tests/harness/test_lifecycle_regression_corpus.py`

**covers**

- `E267.RC1.CRASH_MATRIX`.

## Slice 3 — Repair Only the Proven Callback/Finalization Owner Seam

**files/responsibility**

- `scripts/harness/runtime_callback_io.py`: stable accepted-callback observation and identity validation only.
- `scripts/harness/runtime_worker_review_bridge.py`: code-owned ingestion, findings publication, resolution handoff, and same-worker continuation.
- `scripts/harness/review_finalization.py`: exact lineage reservation and terminal review handoff.
- `scripts/harness/runtime_worker_summary.py`: refreshed-summary continuation only where the golden corridor proves a gap.
- `scripts/harness/cli.py`: no new recovery authority; delete or narrow a branch only if the production worker makes it provably unreachable.
- Focused existing tests under `tests/harness/test_callback_submit_recovery*.py`, `test_runtime_task_summary.py`, `test_review_gate.py`, and `test_store.py`.

**consumes**

- The first RED state from Slices 1–2 and exact accepted callback/receipt identities.

**produces**

- One production owner that deterministically consumes the accepted callback and progresses the corridor without manual `resume`, provider replay, cross-HEAD reinterpretation, or duplicated findings.

**failing evidence**

- A stable accepted callback plus unchanged behavioral subject remains pending ingestion/finalizing after worker restart, or two owners can both publish the next handoff.

**minimal green**

- Reorder or consolidate the smallest owning transition; no incident-specific authorization compiler, magic identity, polling heuristic, or new state.

**refactor seam**

- Private cross-module imports in `cli.py` may be removed only when focused tests prove the runtime owner covers the exact path; no broad CLI rewrite.

**focused verification**

- Callback submit/recovery, runtime task summary, review gate, store/CLI and golden corridor tests.

**covers**

- `E267.RC1.CORRIDOR`, `E267.RC1.CRASH_MATRIX`.

## Slice 4 — Separate Product Cycles from Mechanism Recovery and Add the Structural Pivot

**files/responsibility**

- `scripts/harness/finalization_ledger.py`: immutable attempt evidence, product-cycle accounting, five-cycle ceiling, and zero-effect sixth attempt.
- `scripts/harness/finalization_policy.py`: Fable High primary route for cycles 1–3 and Sol X-High structural route for cycles 4–5 without an availability probe effect.
- `scripts/harness/review_finalization.py`: reservation integration and exact third-failure pivot boundary.
- `scripts/harness/finalization_pivot.py` (new only if no existing deep module owns it): bounded read-only packet compiled from the first three typed material findings/resolutions and exact HEADs.
- `config/model-routing.toml`: registered Fable High primary and Sol X-High pivot routes; no provider CLI literal in public DSL.
- `tests/harness/test_finalization_ledger.py`, `test_finalization_policy.py`, `test_exact_head_review_attempt.py`: product/mechanism accounting, route decisions, crash/idempotency, and sixth-attempt zero effects.
- `skills/review/SKILL.md`, `skills/dispatch/SKILL.md`, and the smallest directly relevant reference file: preserve existing triggers and authority while documenting product-cycle counting and the third-cycle pivot.
- `skills/improve-skills/*` verdict artifact in ignored task evidence only; production skill edits must pass both `skill-creator` guidance and `improve-skills` audit.

**consumes**

- Existing `FinalizationLedger`, optional `finalization_policy`, registered logical aliases, the agreed five-cycle policy, and typed mechanism classification.

**produces**

- Product cycles advance only on material review failure; mechanism failures remain immutable but budget-neutral. The third material failure deterministically produces one pivot packet; cycle 4 cannot start before one Sol X-High pivot receipt is accepted.

**failing evidence**

- A mechanism failure currently consumes a cycle, a fourth cycle can start without the pivot receipt, route effort is below the approved level, or a sixth attempt mutates ledger/session/provider state.

**minimal green**

- Extend the existing ledger/policy rather than add another lifecycle store. Keep the pivot packet bounded, typed, read-only, and derived only from accepted attempt artifacts.

**refactor seam**

- One authoritative rule source for cycle accounting and one for skill prose. Do not duplicate the route matrix across skills.

**focused verification**

- Finalization ledger/policy/exact-head attempt tests.
- `python3 skills/improve-skills/scripts/audit_skills.py --strict`
- Scoped verdict audit for every changed skill.
- `make test-instruction-lint test-skill-budget test-codex-adapter`

**covers**

- `E267.RC1.PRODUCT_BUDGET`.

## RC1 Gate — Three Fresh Live Runs

**files/responsibility**

- `scripts/live-acceptance-runner.py`, `scripts/live_acceptance_contracts.py`, and `scripts/live_acceptance_driver.py`: reuse the existing bounded provider-backed runner for the one supported corridor and streak identity.
- `config/acceptance-cells.toml`: fixed RC1 cell identities and routes only.
- `tests/test_live_acceptance_runner.py` and `tests/test_live_acceptance_surface_cleanup.py`: preflight, restart, streak reset/preservation, exact effect and resource evidence.
- `docs/acceptance/evidence/v2.6.7/rc1-*`: immutable sanitized receipts for the three-run streak; no promoted diagnostic result.

**consumes**

- Green Slices 0–4, one exact `lifecycle_subject_sha256`, healthy registered Fable/Sol routes, and coordinator authorization for each provider-backed cell.

**produces**

- Three consecutive fresh successful runs, with at least one real finding/fix/re-review path, and a terminal RC1 acceptance receipt.

**failing evidence**

- Any coordinator lifecycle repair, duplicate effect, mixed identity, non-resource-free terminal state, missing real fix loop, or behavioral digest drift invalidates the affected run; a code/config/schema/hook/behavioral-skill mutation resets the streak to zero.

**minimal green**

- Repair only a deterministic reproducer in an existing owner. Documentation/evidence/test-only changes preserve the streak.

**refactor seam**

- None during the live streak. Any production cleanup is a behavioral change and restarts it.

**focused verification**

- Runner dry-run/preflight, then three coordinator-owned live cells one at a time with immutable receipts.

**covers**

- `E267.RC1.LIVE_STREAK`, `E267.RC1.RELEASE_STOP`.

## Slice 5 — Root-Scoped Dashboard Projection

**files/responsibility**

- `scripts/harness-dashboard.py`: require `--root` for normal open/live mode, project one exact root/owner, retain explicit `--all` diagnostic mode, and include root identity in the marker.
- `scripts/harness/dashboard_projection.py`: one root plus exact descendants; no scan of unrelated owners in normal mode.
- `scripts/harness/dashboard_view.py`: compact current/past/future steps, route/model/effort/review/cycle metadata, and issues only for the selected root.
- `tests/harness/test_harness_dashboard.py`: projection isolation, pre-start empty observer, early start failure, marker identity, idempotent reuse, concurrent requests, stale/moved/foreign markers, color/row behavior, and external ownership.

**consumes**

- RC1 terminal evidence and the existing invariant `request_id == owner_id == root operation_id` before provider launch.

**produces**

- One request-scoped terminal observer and a separately invoked global diagnostic view.

**failing evidence**

- Two request IDs in one workspace reuse one split, normal mode renders another owner, or an observer appears in Harness OwnedResources/cleanup.

**minimal green**

- Add exact root filtering and marker identity to the current terminal implementation; no HTTP server, browser, history migration, or new cmux lifecycle.

**refactor seam**

- Separate global snapshot selection from root projection only if it reduces conditional policy in the entrypoint; renderer stays shared.

**focused verification**

- `python3 tests/harness/test_harness_dashboard.py`
- Existing status-segment and cmux adapter tests affected by projection inputs.

**covers**

- `E267.RC2.ROOT_PROJECTION`, `E267.RC2.SPLIT_IDENTITY`, `E267.RC2.EXTERNAL_OBSERVER`.

## Slice 6 — Bind Dispatch to the Pre-Known Root Observer

**files/responsibility**

- `skills/dispatch/SKILL.md`: pass the approved request ID to dashboard open before validate/start and keep observer failure contained.
- `scripts/dispatch_workspace.py` and `scripts/dispatch-runner.py`: expose the already-approved request identity to the exact observer command without changing operation identity or launch ordering.
- `tests/test_dispatch_runner.py` and dashboard integration fixtures: command identity, no provider effect from observer, early failure visibility, and unchanged start authorization.

**consumes**

- Slice 5 root-scoped dashboard contract and the existing request/owner/root identity mapping.

**produces**

- Normal dispatch opens or reuses exactly the request-scoped observer before `validate/start`; a second request opens a second split.

**failing evidence**

- Dashboard opens without exact request identity, start must be reordered to discover identity, or observer failure blocks/rolls back an approved pipeline.

**minimal green**

- Thread the existing request ID; do not create another identifier or move provider start.

**refactor seam**

- Keep observer invocation in one dispatch boundary and out of Harness OwnedResources.

**focused verification**

- Dispatch runner, dashboard, instruction lint, skill budget, and Codex adapter tests.

**covers**

- `E267.RC2.ROOT_PROJECTION`, `E267.RC2.SPLIT_IDENTITY`, `E267.RC2.EXTERNAL_OBSERVER`.

## RC2 Gate — Sequential Observer Dogfood

- Run one fixed small Fable High `engineering/change` task from a fresh request/worktree/store boundary.
- Confirm the split exists before start, shows only that root, records model/runtime/effort/review/cycle details, survives early/terminal states, and remains user-owned after Harness cleanup.
- Any lifecycle failure returns to the RC1 defect classifier. Any projection-only failure is repaired under Slices 5–6 without invalidating the already-bound RC1 behavioral streak unless `lifecycle_subject_sha256` changes.

**covers**

- `E267.RC2.ROOT_PROJECTION`, `E267.RC2.SPLIT_IDENTITY`, `E267.RC2.EXTERNAL_OBSERVER`.

## RC3 Gate — Final Sequential and Parallel Acceptance

**files/responsibility**

- `config/acceptance-cells.toml`: fixed sequential and two parallel 2.6.7 release cells.
- `scripts/live-acceptance-runner.py` and existing driver/contracts: bounded scheduling, identity/effect evidence, cleanup, and final aggregation only.
- `tests/test_live_acceptance_runner.py`, `tests/test_live_acceptance_surface_cleanup.py`: deterministic coordinator preflight, independent route/worktree/root identity, partial failure, and terminal cleanup.
- `docs/acceptance/evidence/v2.6.7/rc3-*`: sanitized immutable sequential/parallel receipts.
- Version manifests, changelog/release notes, README release index, and final exact-HEAD gate receipt: packaging only after all behavioral evidence is terminal.

**consumes**

- One final `lifecycle_subject_sha256`, terminal RC1/RC2 receipts, healthy Terra/Opus/Sonnet/Sol capabilities, and clean candidate HEAD.

**produces**

- One final sequential golden receipt, one parallel two-root receipt, cleanup proof, exact-HEAD full gate, and independent Fable High implementation approval.

**failing evidence**

- Start concurrency before sequential success, conflate weak-model content quality with lifecycle failure, share owner/worktree/surface/effect identity, leave Harness-owned resources, or promote diagnostic-only output.

**minimal green**

- Fix only release-blocking defects in the supported corridor or scoped observer. New independent defect classes count toward the release stop rule.

**refactor seam**

- None after the final behavioral digest is frozen; packaging/evidence commits may change HEAD without invalidating the bound digest.

**focused verification**

1. One sequential fresh release-candidate run.
2. Terra Medium executor → Opus High review and Sonnet Medium executor → Sol High review concurrently.
3. Resource and effect audit.
4. Focused suites, then full hermetic `make test`, configured coverage/quality gates, skill audits, adapter sync checks, and exact-HEAD release gate.
5. One fresh Fable High implementation review on the exact candidate.

**covers**

- `E267.RC3.SEQUENTIAL`, `E267.RC3.PARALLEL`, `E267.RC3.CLEANUP`, `E267.RELEASE.GATE`.

## Capability Dispositions and Defect Ledger

The following dispositions are release constraints, not optional design suggestions:

| Capability or defect class | 2.6.7 disposition | Required evidence | Stop boundary |
|---|---|---|---|
| Known callback-ingestion and review-finalization seam | Repair only a deterministic RED reproducer inside the named transition owner; preserve accepted callback and provider receipts; do not add a CLI recovery authority. | Golden corridor, crash matrix, focused owner tests, and fresh live streak. | Any ambiguous provider/callback effect or need for a second lifecycle owner stops the attempt. |
| Product correction loop | Retain five atomic product-cycle reservations. Cycles 1–3 use fresh-context Fable High; the exact third material failure publishes one immutable structural pivot packet; cycles 4–5 use Sol X-High review of the structural solution. | Ledger tests prove material-finding accounting, mechanism neutrality, pivot identity, fifth-cycle terminal state, and sixth-cycle zero effect. | Fifth material failure is `finalization-budget-exhausted`; no sixth provider, session, reservation, or callback effect. |
| Mechanism, transport, callback-stall, provider, test-flake, and safe-restart failures | Record under a separate bounded recovery lineage and resume from the last durable boundary without consuming a product cycle or replaying effects. | Typed attempt receipts and crash/restart schedules distinguish recovery attempts from product corrections. | Unknown or ambiguous authority becomes exact `attention-required`; it never authorizes replay. |
| Stability streak | Bind three fresh live runs to `lifecycle_subject_sha256`, not raw Git HEAD. | Versioned denominator, digest fixtures, three ordered run receipts, and reset tests. | Any behavioral subject change resets the streak; excluded documentation/evidence/test-only changes do not. |
| Root-scoped observer | Treat the pre-known request ID as the sole root identity and keep the observer outside Harness ownership. | Projection, marker, duplicate-open, two-root, early-failure, and cleanup tests plus RC2 live receipt. | Stale, ambiguous, moved, or foreign marker fails closed and cannot touch another surface. |
| Legacy/custom pipeline surface | Preserve compatibility only where existing tests require it; it is outside the supported release corridor. | Existing hermetic regression suite remains green. | A defect found only in a non-goal path is recorded for a later release unless it corrupts the supported corridor. |
| Independent new lifecycle defect classes | Group by demonstrated root cause, not symptom wording or surface. | Typed RC1 defect ledger with reproducer, owner, transition, and disposition. | Three independent new classes outside the known callback/finalization seam stop 2.6.7 expansion and create a kernel-rewrite decision boundary. |
| Browser/Foam/new kernel work | Deferred and excluded. | Non-goal check in final release review. | Any implementation in this release is scope drift. |

An item enters the defect ledger only with a deterministic reproducer or a preserved live receipt. Each record must identify the durable pre-state, expected owner and transition, observed post-state, effect ambiguity, root-cause class, focused regression, and final disposition. Symptom-only aliases share one root-cause record and do not inflate the release stop count.

## Success Evidence Map

| Evidence ID | Owning slice or gate | Required durable artifact or command family |
|---|---|---|
| `E267.RC1.CORRIDOR` | Slice 1 | Golden production-core trace plus focused Harness control-plane tests. |
| `E267.RC1.CRASH_MATRIX` | Slice 2 | Named before/after crash schedule receipts, replay counters, and historical regression corpus. |
| `E267.RC1.PRODUCT_BUDGET` | Slice 4 | Finalization ledger/policy tests for cycles 1–5, pivot packet identity, mechanism-neutral attempts, and sixth zero effect. |
| `E267.RC1.SUBJECT_DIGEST` | Slice 0 | `lifecycle_subject_sha256`, versioned denominator, inclusion/exclusion fixtures, and reset proof. |
| `E267.RC1.LIVE_STREAK` | RC1 gate | Three ordered fresh-run receipts on one behavioral digest, including one findings/fix/re-review run. |
| `E267.RC1.RELEASE_STOP` | Slice 0 and RC1 gate | Typed defect-class ledger and deterministic three-class stop-rule tests. |
| `E267.RC2.ROOT_PROJECTION` | Slice 5 | Root-only projection snapshots and explicit global-diagnostic regression. |
| `E267.RC2.SPLIT_IDENTITY` | Slice 6 | Marker identity tests for reuse, two roots, and stale/foreign fail-closed behavior. |
| `E267.RC2.EXTERNAL_OBSERVER` | Slice 6 and RC2 gate | OwnedResources exclusion, contained open failure, user-closed terminal observer receipt. |
| `E267.RC3.SEQUENTIAL` | RC3 gate | One fresh final-digest sequential golden live receipt. |
| `E267.RC3.PARALLEL` | RC3 gate | Terra/Opus and Sonnet/Sol concurrent root, callback, worktree, and dashboard receipts. |
| `E267.RC3.CLEANUP` | RC3 gate | Terminal resource inventory proving zero Harness-owned provider/process/supervisor/review/worktree resources. |
| `E267.RELEASE.GATE` | Final gate | Exact release-HEAD manifest binding plan, Outcome Contract, behavioral digest, RC receipts, full tests, quality/coverage, packaging, and independent Fable High implementation review. |

Every success claim must cite the exact artifact or command output at the reviewed HEAD. A passing command from another HEAD, an accepted callback without ingestion, a visible terminal screen, or an unexplained deviation is not release evidence.


## Recovery and Stop Rules

1. A repository-owned deterministic mechanism failure follows the existing failure-repair contract: contain, diagnose read-only, make one narrow reversible TDD repair, and resume from the last safe boundary without replaying provider effects.
2. Mechanism repair receipts are separate from product-cycle receipts. They neither advance nor reset the product cycle unless their behavioral code mutation changes `lifecycle_subject_sha256`.
3. After the third material product failure, ordinary patching is prohibited until the Sol X-High pivot receipt is accepted.
4. After the fifth material failure, the product lineage is terminal `finalization-budget-exhausted`; no sixth model/session/ledger effect.
5. Three independent new lifecycle root-cause classes during RC1 terminate the release effort before RC2. Preserve tests and evidence for a future kernel; do not broaden the old Harness.
6. Permission, security, dependency, public-interface, destructive, migration, external-effect, and ambiguous-state boundaries remain coordinator/user decisions.

## Plan Review and Execution Handoff

1. Validate this plan and Outcome Contract from the exact Fix3 base.
2. Run one independent Fable X-High intent review through the code-owned plan-review facade.
3. Amend design artifacts only through the protected plan amendment/review boundary until Fable approves; do not start implementation on a material unresolved finding.
4. Start one long Fable High Harness execution dispatch for the approved plan.
5. Keep RC1, RC2, and RC3 as checkpoints in that dispatch. Use typed escalations for coordinator-owned live/provider/store actions and return to the same task after resolution.
6. Do not push, tag, publish, or update Swarm until the canonical release evidence and final approval are terminal.
