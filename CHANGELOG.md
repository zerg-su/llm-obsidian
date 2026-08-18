# Changelog

Language: **English** · [Русский](CHANGELOG.ru.md)

All notable changes to llm-obsidian. Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Versioning: [SemVer](https://semver.org/).

> llm-obsidian descends from [AgriciDaniel/claude-obsidian](https://github.com/AgriciDaniel/claude-obsidian) (see [ATTRIBUTION.md](ATTRIBUTION.md)); its mechanics were incubated and battle-tested in a private DevOps vault through 2026 before this generic public release. This changelog starts fresh at 1.0.0.

Only public releases are listed. Versions 2.0.5, 2.1.1, and 2.4.0 were internal
checkpoints folded into the following public releases; no public tags or
packages were published for them.

## [2.8.1] - 2026-08-17

### Fixed

- Lifecycle continuation now ingests an exact accepted nested review callback
  across the observed post-accept `callback-invalid` boundary without replay,
  refreshes the canonical task summary after exact-HEAD verification and before
  review, proves superseded verification through an immutable successful receipt,
  preserves failed baseline-gap evidence through typed restart and provider
  admission, and publishes only decisions executable for the exact escalation
  kind. The live scripts ratchet moves to the measured 294-file / 111,456-line
  candidate with zero headroom. By explicit coordinator decision, the separately
  measured 26-line scripts drift predates this repair and is admitted exactly,
  with no speculative allowance. By prior explicit user decision, the 26-line
  active-authority increase preserves only the bounded ancestor-resolution and
  verification continuation recovery; the unchanged 27-file contour is pinned
  to its measured 15,796 LOC with zero writable authorities, zero incident
  literals, and no growth allowance.
- A coordinator-authorized `task-review-runner.py authorized-continuation`
  primitive binds one exact resolved escalation and amended Outcome Contract to
  a clean successor HEAD, one immutable full-profile verification receipt, and
  one fresh Deep review continuation without rewriting task metadata or prior
  records. Its single production owner and registered entry point move the live
  scripts ratchet by the exact measured 676 lines to 295 files / 112,132 lines
  with zero speculative headroom. The unchanged 27-file active-authority
  contour is remeasured exactly at 15,802 LOC with zero writable authorities,
  zero incident literals, and zero headroom.
- A durable failed authorized-continuation receipt now hands off instead of
  raising an untyped error: it derives the standard identity-bound
  `.task-verification.json` attention packet from the immutable receipt and
  publishes it idempotently, and repeated consumption of that same failed
  identity is served from the durable receipt without executing any profile
  command or binding a review. Because the bounded authorization allows at most
  one receipt per exact clean HEAD, the packet offers only `escalate`. The
  registered continuation owner grows by the exact measured 114 lines to
  295 files / 112,246 lines with zero speculative headroom; the 27-file
  active-authority contour is untouched at 15,802 LOC.
- The installed `architecture` and `decompose` skills are registered in
  `wiki/meta/daily-pipeline-guide.md`, closing the catalog drift that failed
  `validate-vault.py --summary` inside the full verification profile.
- That handoff's Deep review findings are applied in the same owner: the
  attention packet is no longer written by the directory-hardening writer, so
  publishing the attention packet no longer tightens the caller-owned checkout
  root from 0755 to 0700 (the success-path binding written by `_write_once` is a
  checkout-root artifact that still does; narrowing it is parent-owned); the size guard measures the exact bytes that writer emits, so the bound
  the standard consumer applies to the file is the bound enforced here; an
  attention packet this identity did not derive is refused rather than replaced,
  and a published packet is cleared only by the coordinator decision that
  resolves it; and the terminal-`failed` choice plus the published packet path
  are stated where the refusal is raised. Net exact measured 18 lines to
  295 files / 112,264 lines with zero headroom.
- The identity-bound verification resubmission corridors publish
  `.task-verification-response.json` atomically. The fixture published this
  shared control artifact with a non-atomic writer while the worker concurrently
  polls it, so a reader could observe the zero-byte truncation window; the real
  consumer rejects an empty read as an invalid response and the dispatch records
  `callback-invalid` with no second verification attempt. Measured directly: the
  non-atomic writer exposed an invalid state in 10,212 of 16,725 concurrent
  observations, the atomic writer in 0 of 44,761. Both corridors now use the
  existing `write_json_atomic` publisher, the 0.12s sleep that narrowed but never
  closed the window is removed, and a deterministic regression pins that an
  atomic publication is only ever observed complete while a zero-byte one is
  still detectable as invalid. No production code changed.
- The second review round of that handoff is applied in the same owner: the
  deterministic race regression is rebound to the corridor's own publication
  through the `publication_barrier` idiom this file already owns, so reverting
  the publisher now fails a check instead of regressing to an intermittent
  end-to-end flake; the observer runs through `start_checked_thread` so a probe
  failure is main-thread evidence; the response-state classifier is collapsed to
  the three states the assertions actually use; and the attention packet is
  published create-exclusively (`O_WRONLY|O_CREAT|O_EXCL`) so the
  refuse-rather-than-replace ownership decision and the write are a single step
  rather than a check-then-write. Exact measured 37 lines to
  295 files / 112,301 lines with zero headroom.

## [2.8.0] - 2026-08-17

Architecture Workflow v1: architectural intent becomes accepted project
knowledge and then an executable Work Item DAG. Details: [docs/releases/v2.8.0.md](docs/releases/v2.8.0.md).

### Added

- `architecture` carrier: owns the Design Frontier and whole-system project
  knowledge (vision, architecture, bounded design, spec, contract) under one
  shared artifact contract, with one explicit handoff to a carrier and the
  returned artifact collected into project context.
- `decompose` carrier: builds the `<Project> Work Graph` and its Work Items from
  accepted knowledge through MAP, ACCEPT and authorized MATERIALIZE, proves
  bidirectional traceability, and derives waves and the Planning Frontier from
  `depends_on` plus real concurrency constraints. File paths and TDD stay below
  the Work Item boundary.
- `docs/skill-references/architecture-artifacts.md`: the single artifact contract
  both carriers read completely — roles, placement under
  `wiki/projects/<project>/`, title-plus-address identity, revisions, report-only
  freshness, lifecycle, and the durable-consumption invariant.
- `scripts/architecture_paths.py`: bounded path validator owning the grammar, the
  `NFC(title).casefold()` collision key checked against the whole link namespace
  including aliases, role-to-folder mapping, symlink rejection on every component
  from the wiki root through the selected role directory, and canonical
  destination equality.
- `implementation-plan` accepts a Work Item input, verifying per-class upstream
  authority, total current pins and recovery/projection consistency before any
  file or TDD planning, and emitting the canonical Upstream Gap otherwise.
- `scripts/architecture_workflow_pressure.py` and
  `scripts/architecture_workflow_audit.py`: release-owned behavioral evidence
  machinery with per-case fresh-session provenance and fail-closed credential
  sanitization of the complete record.

### Fixed

- The frozen RC4 engineering-discipline denominator pins the exact bytes of
  `AGENTS.md` and `CLAUDE.md`, which made the skill catalogue inside
  `CLAUDE.md` frozen too: any release shipping a new skill broke a historical
  denominator, and the only alternatives were rewriting recorded acceptance
  evidence or leaving the manual stale. `normalized_governing_source` now
  canonicalizes exactly that catalogue line, so historical pins and prompt
  digests stay byte-identical while the manual stays current; a regression
  proves a shipped catalogue change is accepted and any other `CLAUDE.md`
  drift still fails closed. Scripts ratchet moves to 290 files / 109,677 lines.

- The frozen RC4 engineering-discipline denominator no longer breaks when a
  release ships a new skill. It pins the exact bytes of `AGENTS.md` and
  `CLAUDE.md` because the recorded v2.6.6 prompts must stay reproducible, but
  the skill catalogue inside `CLAUDE.md` is an index rather than one of the
  principles under test. `normalized_governing_source` now canonicalizes only
  that catalogue line, so the historical pins and prompt digests stay
  byte-identical while the manual stays current; a regression proves a shipped
  catalogue change is accepted and that any other `CLAUDE.md` drift still fails
  closed. Historical evidence is untouched. Scripts ratchet moves to the measured candidate 293 files / 110,718 lines.
- The bounded review resolution fix delta now uses one registered git command
  (`review_resolution.fix_delta_command`) that excludes committed
  `docs/acceptance/evidence/` bytes, so acceptance evidence can never crowd a
  small product fix out of the 1 MiB transport cap; the cap still applies to
  product paths and an evidence-only resolution stays an empty, rejected
  delta. Both resolution bundle computations and the review program's
  independent digest verification bind the same command. The scripts scope ratchet is stated once for this release below.

### Changed

- Architecture Workflow v1 advances the scope ratchet by its exact measured
  production cost to 293 files / 110,718 lines with zero headroom:
  the 252-line `scripts/architecture_paths.py` validator, 276-line
  `scripts/architecture_workflow_audit.py` composition, and 780-line
  `scripts/architecture_workflow_pressure.py` adapter are release-owned while
  the shared engineering-eval runner remains byte-identical to the reconciled
  main base after its separately owned 29-line catalogue projection.

## [2.7.8] - 2026-08-17

Bounded stability candidate; not tagged, published, installed, or live-tested.
It closes the protected-research and custom-pipeline follow-ups exposed by the
2.7.7 runs, the verification-control cleanliness defect, and the
structural-pivot cleanup gap exposed while qualifying this candidate, without
adding a lifecycle owner or replay path.

### Fixed

- Argv-delivered one-shot protected research now records input acceptance and
  the accepted result on the existing provider-event stream. Input acceptance
  now requires the prompt pointer bytes to match the exact prompt persisted in
  the research argv; drift enters existing attention containment without an
  `input-accepted` event. Cleanup reaches `resource-closed`, and synthesis
  starts without a second prompt, provider send, or network fetch.
- Untyped executor raises always retain the ordinary fail-closed escalation
  path. State-only stale post-self-heal suppression was removed because the
  raise carries no predecessor/successor operation, run, or visit identity;
  `v278-suppress-stale-post-self-heal-escalation` is deferred until a separate
  exact incoming-raise identity contract is approved.
- Dispatch now installs exact local Git exclusions for the three
  `.task-verification*.json` control artifacts. An authorized same-HEAD retry
  can therefore enforce strict tracked-and-untracked product cleanliness
  without classifying its own packet, response, or decision contract as
  product drift; real untracked product files remain visible and fail closed.
- Structural-pivot cleanup now admits its delegated `review-round` result only
  through the existing exact child kind, parent, lane, run, generation,
  callback-digest, provider, and resource checks. The already accepted pivot
  can reach `resource-closed` without replaying a provider, review, or
  callback; every identity mismatch remains fail-closed.

### Governance

- The target repository's unchanged 27-file active-authority contour remains
  exactly 15,603 lines with zero writable authorities and zero incident
  literals. The previously diagnosed 15,663 contour belongs to the separate
  `llm-obsidian-swarm` integration commit `71f0b27c`; its 60-line liveness
  repair is absent from target base `379e772c` and is neither ported nor used
  as target headroom.
- `runtime_session_launch.py` is 1,155 lines, within its existing 1,157-line
  hotspot ceiling. Removing the ambiguous suppression machinery while adding
  the argv binding and structural-pivot cleanup leave the exact candidate at
  290 files / 109,373 lines, 275 below the unchanged 109,648-line ceiling and
  without a ceiling increase.

## [2.7.7] - 2026-08-16

Stability release for long-running built-in and custom Harness pipelines. It
consolidates the bounded 2.7.1–2.7.7 verification, cleanup, admission, and
provider-generation repairs and is qualified by five lightweight live smoke
corridors across OpenAI and Claude routes.

### Fixed

- Successful initial and replacement ready handshakes now carry the positive
  provider generation read before that exact provider process starts. Cleanup
  validates the complete ready/process/supervisor identity and closes only the
  named generation, so a reviewer retargeted from placeholder generation 1 to
  generation 2 before initial input can finalize with only generation-2
  provider events. Missing, malformed, symlinked, stale, identity-drifted, or
  attention-latched authority remains fail-closed without a fabricated close
  receipt.

### Governance

- The live scripts scope ratchet moves by the exact measured 59-line
  production cost to 290 files / 109,339 lines with no speculative headroom.
- Final dogfood completed three built-in corridors and two custom corridors:
  OpenAI-local, OpenAI-to-Claude, Claude-to-OpenAI, a four-step custom pipeline
  with verification, and a three-step custom pipeline without verification.
  Every corridor reached terminal reap. One custom callback self-healed before
  a stale executor escalation was classified; suppressing that redundant
  escalation is accepted as a minor follow-up rather than a release blocker.

## [2.7.6] - 2026-08-16

Bounded provider-start-admission candidate; not tagged, published, installed,
or live-accepted. Builds on the preserved 2.7.5 negative candidate and closes
only its two terminal Sol findings.

### Fixed

- The exact durable verification receipt/HEAD admission is now passed through
  the existing review/runtime start seam and consumed by the real
  provider-start effect owner after launch preparation and immediately before
  `start-provider` registration. Candidate drift closes the prepared surface
  with zero provider command or process effect; an unchanged exact candidate
  starts once (`F275.PROVIDER_EFFECT_TOCTOU_REMAINS`).
- Dispatch contract resolution now distinguishes recordless and legacy-empty,
  resolved no-verify, resolved verify-owning, and unresolved nonempty
  contracts. A present nonempty contract that neither canonical resolver can
  compile fails closed before reservation or provider effect instead of being
  treated as no-verify (`F275.UNRESOLVABLE_CONTRACT_FAILS_OPEN`)
  (`docs/acceptance/v2.7.6-provider-start-admission.md`).

## [2.7.5] - 2026-08-16

Bounded durable-exact-consumer-admission candidate; not tagged, published, or
live-accepted. Builds on the preserved 2.7.4 negative-evidence candidate and
closes exactly the frozen F274.POST_CHECK_LAUNCH_RACE class.

### Fixed

- Verification consumption is bound to the exact durable receipt/HEAD
  identity instead of a prior boolean observation, so a clean commit landing
  strictly after the closing candidate read can no longer publish stale
  controller authority or launch review for an unverified candidate. The
  durable controller link admits a completed receipt only while its exact
  HEAD is the clean current candidate — re-observed immediately before the
  atomic write and once more after it, retracting on a post-write mismatch —
  while failed receipts stay linkable failure evidence for the existing
  attention/resubmit machinery. The review drive re-reads the durable
  verification receipt at the launch boundary and publishes the exact
  receipt/HEAD pair as the launch admission; the exact-HEAD review flow
  verifies that admission against the actual review context before
  reservation or provider effect, failing closed on any HEAD, receipt
  identity, clean-state, or launch-input mismatch, with malformed or
  symlinked admissions never treated as absent
  (F274.POST_CHECK_LAUNCH_RACE)
  (`docs/acceptance/v2.7.5-durable-exact-consumer-admission.md`).
- After its Sol implementation review (both findings applied in one bounded
  patch): the flow consumes the durable receipt named by the admission's
  published receipt pointer through the real VerificationAuthority ingress —
  bound to the discovered dispatch record, its compiled contract, and the
  frozen verification profile — and requires the admission for verify-owning
  dispatch contracts, so deleting the admission or the receipt refuses the
  launch (F275.RECEIPT_ADMISSION_NOT_CONSUMED); and the same admission
  closure re-runs inside the gate's launch transaction as the last
  observation immediately before each fresh provider start, so drift after
  the flow's earlier reads leaves at most an effect-free attention
  reservation with zero provider starts (F275.POST_CHECK_PROVIDER_RACE).

### Governance

- The RC1 active-authority contour is rebaselined at the exact measured
  15599 LOC (unchanged 27-file manifest, zero writable authorities, zero
  incident literals), the verification owner, review bridge, review flow,
  and gate attempt hotspots move to exactly their measured
  1276/1146/1438/1051 lines, and the live scripts scope ratchet moves by
  the measured 149-line closure cost plus the measured 199-line
  findings-patch cost to the exact 290-file / 109,263-line candidate with
  no speculative headroom.

## [2.7.4] - 2026-08-16

Bounded exact-verification-closure candidate; not tagged, published, or
live-accepted. Builds on the preserved combined 2.7.1 + 2.7.2 +
rejected-but-useful 2.7.3 candidate and closes the two material findings of
the terminal 2.7.3 review.

### Fixed

- The verification owner now runs one read-only census of the deterministic
  attempt-0, attempt-1, receipt, and invalidation identities before
  classifying a missing attempt-0 predecessor record as a fresh run. Any
  surviving successor record or receipt/response/invalidation trace proves
  the predecessor was lost rather than never created: the orphaned lineage
  latches one typed `pipeline-verification-orphaned-lineage` attention with
  zero minting, probe effect, store mutation, receipt linking, attention
  clearance, or review effect, and repeated wakes stay idempotent. A truly
  empty attempt identity space remains the only fresh-run classification
  (F273.MISSING_PREDECESSOR_FAIL_OPEN).
- Verification authority is consumed only against a fresh exact-current-HEAD
  observation with a fully clean tracked-and-untracked tree, re-checked
  immediately before controller receipt linking (including link recovery),
  summary consumption/attention clearance, and review drive. A receipt stays
  immutable evidence for its own exact HEAD and is never rewritten; when a
  clean commit races past any of those boundaries — or the tree is dirty or
  unobservable — stale authority is never linked or consumed, no review or
  provider effect launches, and the continuation halts to the existing
  rebind path or one typed `pipeline-verification-stale-authority`
  attention. Drift owned by a durable review-resolution notification keeps
  its existing resolution machinery; an exact clean same-HEAD receipt keeps
  the ordinary exactly-once path (F273.EXACT_HEAD_ACCEPTANCE_RACE)
  (`docs/acceptance/v2.7.4-exact-verification-closure.md`).
- After its Sol implementation review (both findings applied in one bounded
  patch): the current-candidate predicate brackets its tree observation
  with two exact HEAD observations so a clean commit between any of its
  reads invalidates the whole observation
  (F274.CANDIDATE_PREDICATE_TOCTOU); and the review-resolution notification
  is identity-exact and strictly wait-only — it suppresses one attention
  latch during an active resolution but never authorizes linking,
  consuming, or review-releasing stale or dirty authority
  (F274.RESOLUTION_DRIFT_BYPASS).

### Governance

- The RC1 active-authority contour is rebaselined at the exact measured
  15333 LOC (unchanged 27-file manifest, zero writable authorities, zero
  incident literals), the verification owner hotspot moves to exactly its
  measured 1261 lines, and the live scripts scope ratchet moves by the
  measured 234-line closure cost to the exact 290-file / 108,915-line
  candidate with no speculative headroom. 2.7.3 wording that implied review
  approval is corrected: its first review round's findings were applied, and
  its refreshed terminal review remains immutable problem evidence.
- The refreshed 2.7.4 Sol review reported one further material finding in
  the same exact-HEAD consumption class (F274.POST_CHECK_LAUNCH_RACE: the
  candidate predicate is checked before, but not bound to, the durable
  controller-link and review-launch effects). Per the approved stop rule
  the slice is frozen with that finding as immutable problem evidence for
  the next narrow slice; 2.7.4 is not review-approved.

## [2.7.3] - 2026-08-16

Bounded invalidated-verification handoff candidate; not tagged, published, or
live-accepted. Builds on the preserved combined 2.7.1 + 2.7.2 candidate.

### Fixed

- The verification owner now hands an invalidated verification attempt to
  exactly one predecessor-bound fresh attempt at the current clean HEAD. A
  settled succeeded own-identity verification effect without a persisted
  receipt — probes that finished at a HEAD the product left before callback
  acceptance, or whose receipt was lost to a crash — is classified as a
  successful effect with invalid verification authority, never as a
  verification receipt. The stale attempt is durably terminalized and linked
  through one immutable invalidation record to its attempt-1 successor, built
  from the existing identity constructors; repeated wakes and crash re-entry
  converge on the same successor, exhaustion of the bounded successor identity
  space stays typed retry-exhausted attention with no replacement and no probe
  replay, and root callback-invalid attention clears only through the ordinary
  summary path after an exact-current-HEAD successful receipt
  (`docs/acceptance/v2.7.3-invalidated-verification-handoff.md`).
- The changed-HEAD review-resolution gate adopts the same invalidated-attempt
  handoff before launching verification, so its rebound attempt identity can
  never re-enter the recovery dead end.
- After its first Sol review round (findings applied; the refreshed
  terminal review later reported two further material findings, closed by
  2.7.4): the receiptless invalidation
  classifier is identity-exact and symlink-safe — the derived spec, lane,
  run, released resources, and own settled succeeded effect must all match,
  and a dangling receipt symlink is tamper evidence, not absence; the handoff
  is gated on an exact tracked-and-untracked clean-tree observation (typed
  `pipeline-verification-dirty-tree` attention with zero mutation when
  dirty); a replacement attempt rechecks cleanliness at receipt issuance so
  bytes mutated during its probes can never be attested as the clean HEAD;
  and the immutable invalidation record binds the predecessor and successor
  attempt/effect digests.

### Governance

- The RC1 active-authority contour is rebaselined at the exact measured
  15168 LOC (unchanged 27-file manifest, zero writable authorities, zero
  incident literals), the verification owner is an owned file-lines hotspot at
  exactly its measured 1096 lines, and the live scripts scope ratchet moves by
  the measured 123-line repair cost to the exact 290-file / 108,681-line
  candidate with no speculative headroom. Exact-HEAD complete-gate evidence is
  recorded owner-externally so recording it cannot move the product HEAD.

## [2.7.2] - 2026-08-15

Bounded root-generation cleanup authority candidate; not tagged, published, or
live-accepted. Builds on the preserved 2.7.1 candidate.

### Fixed

- Runtime session cleanup closes owned resources only through the immutable
  provider root generation minted at session start; a later review-callback
  generation still carries the accepted result but can no longer substitute as
  resource ownership. A missing, duplicate, malformed, symlinked, or
  identity-drifted root generation — and a conflicting result digest — fails
  closed to typed attention before any close receipt or resource clearing,
  while a valid same-session later-generation review callback cleanup still
  completes exactly once (`docs/acceptance/v2.7.2-root-generation-cleanup-authority.md`).
- A delegated review callback now binds result authority to its exact
  callback-target generation: when the target generation stream is missing or
  mismatched, an earlier-generation result cannot substitute, and cleanup fails
  closed to typed attention. A callback already accepted by the parent record
  itself keeps its own durable earlier-generation contract.
- A root stream already latched in typed attention (beyond the tolerated
  `result-missing` state) can never receive a durable close receipt, and every
  path component from the trusted store root through the closure authority
  directories — `owners/<owner>/runtime/<operation>/provider-events/generation-1`
  with its `delivery` and `events` leaves — is validated without following
  symlinks (including symlinked ancestors), so the closure ledger can never
  write outside the operation's owned state tree. Task pipeline transport
  (`.task-pipeline/`, `.task-pipeline-step-*.json`) is repository-ignored so
  candidates stay clean.
- The live scripts scope ratchet (`rc4_scope_ratchet`) is rebased to the exact
  measured 2.7.2 reviewed candidate — 290 files, 108,558 lines, no speculative
  headroom — absorbing the 2.7.1-base `review_cleanup_recovery.py` module that
  landed without a ceiling raise plus the measured cleanup-authority repair and
  its review corrections.

## [2.7.1] - 2026-08-15

Bounded verification stabilization candidate; not tagged, published, or
live-accepted. 2.6.7 remains an unpublished historical base.

### Fixed

- `retry-mechanism-flake` is now the single public decision for a same-HEAD
  verification retry, from the typed raise through the durable resolution
  record and the coordinator wake. The private `authorize-one-same-head-retry`
  alias is retired; only the typed constructor derives the private action, and
  near-match decisions fail closed.
- After the exact originating coordinator resolves `retry-mechanism-flake`,
  the identity-bound same-HEAD response is published automatically by the
  registered code path before the task wakes; the manual
  `--same-head-mechanism-flake` resubmit command no longer exists. Attempt-0
  evidence, the one-retry ceiling, and changed-HEAD fix-and-resubmit are
  unchanged.
- The active review authority baseline is bound to the exact measured final
  contour (27 files, 15,051 LOC, zero writable authorities, zero incident
  literals) and the RC1 integration trace now asserts exact LOC equality, so
  authority growth and stale headroom both fail immediately.

## [2.6.7-rc6.11] - 2026-08-14

Observer-safe custom terminal cleanup candidate.

### Fixed

- A terminal custom-pipeline Wiki Summary now publishes its accepted result
  against the immutable initial root provider generation, even after child
  callback retargeting advances the active target generation.
- Terminal cleanup now derives close authority only from the exact task
  process, supervisor, and surface. It closes an alive task surface exactly,
  preserves a live external observer workspace, and completes after the exact
  surface is proven missing.
- Cleanup publishes one resource-closed receipt and clears ownership only
  after an accepted matching result, succeeded exit request, dead
  identity-matched process and supervisor, and missing exact surface. Missing
  or mismatched results, changed generations, unresolved identities or
  surfaces, and conflicting or foreign receipts remain fail-closed.
- Review startup cleanup now retires an exact dead reviewer only when durable
  provider events prove `provider-started` without `input-accepted`; its event
  identity is derived from the canonical `ProviderEventIdentity` contract.
- The live scripts ratchet now covers 289 Python files / 108,051 lines.

### Authorized mechanism repair

Outside the approved RC6.11 Outcome Contract and recorded as such: one
coordinator-authorized repair of the engineering/fix transport, not RC6.11
candidate scope.

- A bounded engineering/fix retry that completes with an empty change set at
  the verified HEAD now publishes one typed `pipeline-decision` continuation
  (`stop` or `retry-with-scope`) and parks in attention on every path instead
  of returning silently. It creates no commit and never completes the fix
  transport on the failed HEAD. Each retry iteration carries its own decision
  identity, so a later null-change retry at the same HEAD still publishes an
  actionable continuation while replay stays idempotent. Including this repair,
  the live scripts ratchet stands at 289 Python files / 108,157 lines.

### Candidate boundary

- Public schemas, provider/callback replay authority, the stable RC6.10 outer
  Harness, dashboard markers, and unrelated lifecycle behavior are unchanged.
  This candidate does not merge, push, tag, publish, update plugins, or run
  live dogfood.

## [2.6.7-rc6.10] - 2026-08-14

Durable reviewer terminal-duration candidate.

### Fixed

- Each new identity-bound reviewer callback now publishes one owner-only,
  attempt-scoped display interval from its exact review-input start through
  callback observation. Identical replay preserves the same bytes; malformed,
  symlinked, or conflicting evidence is never overwritten and cannot change
  callback processing.
- Active reviewer rows retain live elapsed time. Terminal reviewer rows freeze
  their own accepted callback-bound duration, and the aggregate review phase
  uses those same validated child intervals. Legacy, missing, malformed,
  mismatched, reversed, negative, non-finite, or future evidence remains `—`.
- The live scripts ratchet now covers 289 Python files / 108,021 lines, with
  the receipt validator ceiling bound to the RC6.10 negative matrix.

### Candidate boundary

- Timing remains read-only display evidence. OperationStore, Harness FSM,
  callback acceptance, review verdicts, provider behavior, cleanup, palette,
  layout, ordering, and root scope are unchanged. This candidate does not
  merge, push, tag, publish, update plugins, or run live dogfood.

## [2.6.7-rc6.9] - 2026-08-14

Review-evidence boundary and bounded terminal-cancellation candidate.

### Fixed

- Planning, saving, and review authoring now keep task success evidence
  reviewer-observable at verdict time. Callback, reap, release, and terminal
  cleanup evidence remains in a parent-owned post-review gate outside the task
  Outcome Contract; strict missing-evidence review behavior is unchanged.
- One supported live-provider cancel requests exit once, performs bounded
  exact-identity cleanup, clears owned resources, and terminates as `cancelled`.
  Only the matching durable `exiting` plus succeeded `request-exit` branch is
  treated as expected; sibling identity/effect states remain fail-closed.

### Changed

- `harness cancel` returns exit status `3` with typed `partial` output when the
  requested root remains nonterminal. Exact surface close is re-probed; failed
  proof retains ownership as `cleanup-incomplete` instead of guessing success.
- The live scripts ratchet is pinned to the exact RC6.9 denominator of 288
  Python files / 107,656 lines.

### Candidate boundary

- Public schemas, provider replay authority, review verdict policy, and
  ordinary successful reap semantics remain unchanged. Merge, plugin refresh,
  dogfood, reap, and promotion remain coordinator-owned post-review gates.

## [2.6.7-rc6.8] - 2026-08-14

Policy-valid custom snapshot actor candidate.

### Fixed

- The custom-pipeline freeze boundary accepts the existing code-owned
  `policy-valid-snapshot` actor only for an exact `approve` decision. Existing
  definition and approval-card bindings remain mandatory; unknown/model
  actors and reject or revise decisions remain fail-closed.
- A production-shaped regression now crosses validation, immutable snapshot
  consumption, authorization, and freeze preparation without a host token or
  pre-start worktree, provider, or model effect. Missing/reused snapshots and
  request, plan, spec, definition, card, route, review, session, permission,
  effect, budget, or actor drift are rejected before start.

### Candidate boundary

- Approval schemas, lifecycle/recovery behavior, and the retained host-dialog
  path are unchanged. Post-install custom dogfood remains coordinator-owned
  after a separate merge and plugin refresh; this candidate does not merge,
  push, tag, publish, update plugins, or run that dogfood.

## [2.6.7-rc6.7] - 2026-08-14

Durable engineering/fix phase timing and policy-valid custom-start candidate.

### Fixed

- Engineering/fix writes identity-bound timing sidecars under
  `pipeline-fix/timing/pass-N/<step>/`. They are display-only: missing,
  malformed, tampered, reversed, or future evidence renders timing unavailable
  and never blocks request publication, callback acceptance, cleanup, or the
  existing lifecycle transition.
- The dashboard selects the newest active retry interval over a completed prior
  pass and otherwise freezes the exact accepted receipt-bound duration.
- A policy-valid owner-only custom validation snapshot may start without a host
  dialog or approval token after atomic revalidation of request, coordinator,
  plan, definition, card, prompt, route, review, and session. Legacy explicit
  host approval tokens remain one-shot.
- The live scripts ratchet is raised to 107,600 lines for the bounded RC6.7
  timing implementation and its strict validation paths.

## [2.6.7-rc6.6] - 2026-08-14

Canonical engineering/fix phase-request candidate. Initial dispatch and
runtime continuation now share the same immutable request grammar and pass
numbering.

### Fixed

- Replaced the duplicated dispatch/worker phase mappings with one
  workflow-owned constructor. Initial reproduce now publishes
  `.task-pipeline/results/pass-0/reproduce.json` and
  `.task-pipeline/outputs/pass-0/reproduce.md`, matching runtime continuation
  and the existing immutable contract-template authority.
- A valid non-template result present before worker observation is submitted
  and accepted through the existing two-read code-owned path without a
  duplicate phase notification. Missing and untouched templates keep the
  ordinary provider prompt; invalid pointer, identity, digest, template, and
  symlink states remain fail-closed.
- Production-shaped traversal proves one initial reproduce receipt, one
  root-cause advance, and provider restart without reproduce prompt, provider
  processing, callback, child, or receipt replay.

### Candidate boundary

- Phase schemas and order, retry and verification budgets, review, dashboard,
  event wake, and lifecycle authority are unchanged. No compatibility reader,
  provider retry, callback replay, or old-run migration was added.
- The post-install engineering/fix smoke remains coordinator-owned after merge
  and plugin refresh. This candidate does not merge, push, tag, publish, update
  plugins, run post-install smoke, or promote the final release.

## [2.6.7-rc6.5] - 2026-08-13

Event-first runtime wake and dashboard step-timing candidate. Existing durable
state and callback artifacts remain the only lifecycle authority.

### Added

- Each interactive runtime worker may use one strict identity-bound
  `cmux events.v1` subscription as a lossy wake hint. Closed routing covers
  exact session, notification, surface, workspace, reconnect, and cursor-gap
  observations without persisting raw event content.
- Bounded atomic diagnostics retain the latest full-reconcile wake and latest
  progressing wake with content-free identity, source, sequence, timing, and
  outcome fields.

### Changed

- Full transport reconciliation is event-first with a 30-second maximum
  fallback. Existing two-read stability confirmation and the prompt,
  checkpoint, provider-exit, callback, liveness, and guardian-control
  deadlines remain independently effective.
- After a task-summary provider exits, its parent reconciles child-session
  review, fix, custom, and verification handoffs within one second because
  those events cannot route through the parent's session-bound subscription.
  The ordinary eventless idle fallback remains 30 seconds.
- The root-owned `TDD slices` dashboard step freezes as a completed duration
  when exact later-step liveness begins. In-process verification children use
  their accepted durable receipt start because they do not own interactive
  liveness state; invalid or contradictory timing remains unavailable.
  Dashboard layout and non-timing labels are unchanged.
- Runtime polling requires an explicit wake source. The lifecycle simulator,
  crash matrix, and production-path oracle now inject an eventless source and
  execute the same event-first branch that ships instead of a legacy test-only
  polling branch.
- The live scripts ratchet is pinned to the measured final RC6.5 candidate at
  287 Python files / 107,239 lines: one new wake adapter module, its bounded
  partial-frame read repair, cross-session reconcile deadline, and final
  timing/oracle corrections, with no blanket headroom.

### Candidate boundary

- Event-source absence, malformed frames, EOF, and identity ambiguity degrade
  to bounded retry or fallback polling without pipeline attention. Wake hints
  never authorize transitions, callbacks, provider effects, recovery, cleanup,
  or completion.
- Post-install built-in and custom dogfood remain separate coordinator-owned
  release evidence after merge and plugin update. This candidate does not
  merge, push, tag, publish, update plugins, or promote the final release.

## [2.6.7-rc6.4] - 2026-08-13

Bounded autonomous-review continuation candidate. It adds two exact
code-owned recovery classes to the existing worker poll and does not add a
scheduler, provider retry, callback replay, or generic attention self-heal.

### Fixed

- A terminal `changes-requested` review can rearm exactly once after an
  identity-bound resolution changes product HEAD and exact-HEAD verification
  succeeds, provided no effect is pending and a review iteration remains.
- An immutable review callback already accepted by the operation store but
  not yet consumed by its exact gate is ingested from the dispatch root's
  durable `review-drive-failed` boundary through the registered gate workflow,
  without accepting or delivering the callback again.
- A terminal approval is reused only while both its exact product HEAD and
  reviewed implementer-summary digest still match. A summary-only refresh at
  the same HEAD reserves one ordinary predecessor-bound follow-up review;
  any additional context drift remains fail-closed.
- If that follow-up fails before creating a lane, its next bounded invocation
  may roll over callback scratch only when the existing content-addressed
  authority proves it belongs uniquely to the archived approved predecessor.
  The zero-effect attempt is superseded without callback or provider replay
  and without consuming another product cycle.

### Changed

- Both paths share one pure classifier and one prepared/finalized,
  revision-bound recovery receipt. Crash, restart, and concurrent ticks
  converge without duplicate transitions or finalization-cycle reservations.
- Diagnostics expose receipt-bound `review-drive-recovery-*` and
  `review-callback-ingestion-*` outcomes; ambiguous identities, uncertain
  effects, exhausted ceilings, and unrelated attention classes remain
  fail-closed.
- Coordinator-owned reap now resolves its session from the task's frozen
  runtime before consulting the generic cross-runtime helper, so an inherited
  foreign runtime variable cannot reject the exact registered reap boundary.
- The active review/recovery authority contour is pinned to its exact RC6.4
  denominator of 27 files / 14,909 lines, with zero writable authorities and
  zero incident literals.

### Candidate boundary

- This work does not publish a release or update the installed RC6.3 plugin.
  A disposable custom dogfood corridor covered the pre-review candidate; the
  post-review HEAD still requires the staged release-gate corridor after merge,
  RC6.4 plugin update, and a fresh host session.
- The reap session-binding change is the narrow, coordinator-authorized repair
  of a repository-owned cross-runtime adapter failure encountered by that
  corridor; it does not relax task identity or finalization checks.

## [2.6.7-rc5] - 2026-08-12

Bounded structural-pivot orchestration candidate. The existing finalization
routes and five-cycle product ledger remain authoritative.

### Added

- After the third material product failure, one deterministic store-backed
  pivot operation launches the registered Sol X-High read-only review, accepts
  the ordinary review-input callback, publishes one bounded identity-bound
  receipt, and cleans its owned resources before cycle 4.

### Changed

- Finalization cycles 1–3 remain on Fable High. Cycles 4–5 add Sol X-High only
  after the accepted structural-pivot receipt; provider availability alone no
  longer authorizes the independent route.
- The root-scoped terminal dashboard projects the pivot descendant and its
  route, callback, terminal, timing, and cleanup evidence without acquiring
  lifecycle authority.
- The live scripts ratchet is pinned to 281 Python files / 103,731 lines for
  the inherited RC5 tree, the single authorized workflow module, the frozen
  historical-denominator repair, and the directly tested pending-pivot
  receipt seam, with no blanket headroom.
- The separately owned active review/recovery authority contour is pinned to
  its exact post-resolution RC5.5 denominator of 27 files / 14,583 lines, with
  zero writable authorities and zero incident literals.
- The code-quality gate records the two exact file-size blockers inherited at
  the approved RC5 base; the new finalization bridge remains below the existing
  function hard limit.
- Structural-pivot child finalization now resumes from its durable transition
  suffix after a crash between `exiting` and `complete`.
- A vault-shaped production wiring test now drives the real pivot constructor,
  callback, cleanup, and cycle-4 continuation; a focused task-flow test binds
  pending pivot work to the ordinary `reviewing` receipt.

### Candidate boundary

- Standalone review presets, the five-cycle ceiling, provider transports, and
  historical roots are unchanged. This work does not publish a release.

## [2.6.7-rc4] - 2026-08-11

Terminal-only Harness dashboard candidate. RC4 refines the existing read-only,
root-scoped observer without changing dispatch, review, verification, recovery,
callback, cleanup, or reap authority.

### Added

- Durable display timing for roots, verification steps, and exact active
  children. Missing current evidence renders `time unavailable`; compact
  terminal rows use a dash and pending rows omit timing.
- Validated terminal review cycle/limit/finding counts without reading review
  prose or inferring severity, bound to the exact gate, reviewed HEAD, axes,
  lane, run, and attempt.

### Changed

- A dedicated human-readable, task-name-first root composition with compact
  completed/future steps, exactly current work expanded, bounded recent roots
  and issues, and the approved semantic truecolor foreground palette.
- Rejected both leaf and ancestor symlink evidence paths; owner-wide diagnostic
  facts no longer leak into the ordinary root view.
- One display-only frame-clock sample per root or owner-wide diagnostic frame;
  `--no-color` remains byte-equivalent after ANSI stripping.

### Candidate boundary

- Observer splits remain external and user-owned, and the owner-wide view is
  diagnostic only. The dashboard has no lifecycle authority.
- This local candidate requires independent review. It is not tagged or
  published by this work.

## [2.6.7-rc3] - 2026-08-11

Final stabilization candidate for the supported `engineering/change`
corridor. RC3 packages the lifecycle repairs found by live acceptance and
binds three consecutive sequential cells plus sequential and parallel
opposite-runtime dogfood to one behavioral digest.

### Fixed

- Executors publish process ownership before semantic startup, while prompt
  delivery and one bounded submit recovery remain identity- and digest-bound.
- Review drive recovery recognizes every live bound round state and scopes
  failure receipts by exact cycle, avoiding false `attention-required` latches.
- A product HEAD changed by findings cannot start review until exact-HEAD
  verification succeeds; malformed review resolutions get at most two
  same-executor correction attempts instead of terminating the corridor.
- Live success authority accepts the terminal cleanup effect, and the RC1 gate
  can close a reserved never-launched claim from durable negative evidence.
- Wikilink-shaped reviewer prose is escaped in archives and archive replay can
  recover after a renderer repair.
- Reap log rendering truncates long summaries without splitting or orphaning
  a wikilink, and the registered one-shot `reap-log-repair` through
  `scripts/vault-write.py` replaces exactly one malformed top reap block
  under optimistic task/log-SHA-256 binding, failing closed on missing,
  duplicate, drifted, or unrelated damage.
- Malformed review-resolution JSON or schema now restores the exact frozen
  template and asks the same live executor for at most two corrections. The
  crash-safe wake never relaunches review or repeats completed product work.

### Changed

- Review prompts carry the exact round iteration and Claude prompt bodies are
  delivered through digest-bound pointers.
- `tests/rc4_scope_ratchet.py` ceilings raised to exactly 273 files / 95,891
  lines for the accepted RC3 review-corridor repairs, bounded resolution
  correction, and the reap-log-repair planner; no blanket headroom.
- `config/code-quality-baseline.json` owns the
  `runtime_worker_review_bridge.py` file-lines hotspot with RC3 regression
  evidence; the resolution correction logic is kept in callback I/O instead.

### Verified

- Three consecutive cheap-model RC1 cells completed on lifecycle digest
  `e0b419fb...`, including two real findings/fix/re-verify/re-review cycles.
- A fresh sequential Terra/Opus High golden run completed before two concurrent
  Terra/Opus High and Sonnet/Sol High roots; all three roots reaped with zero
  Harness-owned resources and distinct root-scoped observer splits.

## [2.6.7-rc2] - 2026-08-10

Root-scoped Harness observability: each dispatch opens or reuses one external,
read-only terminal observer for its exact root operation instead of mixing all
historical owners in one global dashboard.

### Added

- Root-scoped dashboard projection with compact parent/child lineage, route,
  model, effort, step, cycle, terminal outcome, and bounded issue context.
- A per-root cmux marker: the same root reuses its observer split while a
  different root receives a separate split.

### Fixed

- Dispatch binds the observer to its pre-known request/root identity before
  provider launch without making observer availability lifecycle authority.
- Fable tasks use dedicated workspaces by default, matching other executors.
- Late reviewer readiness can recover from exact durable process, supervisor,
  surface, provider, and callback identity without replaying provider input.
- An exact callback from a prior terminal review attempt is archived before a
  fresh zero-effect review cycle; foreign or ambiguous callbacks fail closed.

## [2.6.7-rc1] - 2026-08-09

Bounded harness stabilization: the supported engineering/change corridor
converges across worker restart at every named durable boundary, and the
finalization ledger separates product cycles from mechanism recovery.

### Fixed

- An interrupted own-identity pipeline verification effect resumes on worker
  restart instead of latching `pipeline-verification-effect-uncertain`.
- The runtime worker re-drives the review flow once per distinct durable
  drive input when the review gate is at `attention-required`, restoring the
  code-owned recovery removed in the RC4 refactor.
- A torn callback wake (findings, refresh, or reap notification) resumes once
  per restarted worker generation; live same-generation retries stay
  fail-closed.
- A restarted worker generation consumes a durable resumable mechanism
  attention latch exactly once instead of requiring coordinator `resume`.
- 2.6.6 release evidence validators (`rc3_release_disposition`,
  `rc4_gate_bundle`) now bind to their own era's frozen routes and to the
  receipt's recorded commit tree instead of the moving candidate tree.
- An approved dispatch whose provider cleanup already released every exact
  resource can resume from `attention-required(resume_state=exiting)` and
  terminalize without replaying review or provider effects.

### Added

- `scripts/v267_stabilization.py` and `config/v267-stabilization-subject.json`:
  deterministic `lifecycle_subject_sha256`, RC1 streak validation, and the
  typed three-class release stop rule.
- `scripts/harness/finalization_pivot.py`: the exact third material failure
  freezes a read-only structural pivot packet; product cycle four requires
  the accepted `finalization-independent` receipt.
- Mechanism outcomes (`attention-required`, `blocked`) release their
  finalization reservation into bounded immutable attempt receipts; only
  material outcomes consume the five product cycles.
- Corridor crash matrix over the golden engineering/change scenario
  (`tests/harness/test_lifecycle_crash_matrix.py`,
  `tests/harness/lifecycle_simulator_world.py`).
- `tests/rc4_scope_ratchet.py` ceilings raised to 272 files / 93,750 lines for
  the accepted 2.6.7 RC1 stabilization corridor, including the final
  architecture-stop and cleanup-recovery repairs; the packaged candidate
  retains 15 lines of explicit headroom.

## [2.6.6-rc4-fix3] - 2026-08-09

This bounded reconciliation patch keeps Codex dispatch profile synchronization
inside a target repository when that repository owns its own dispatch profile.
It adds no dashboard or Harness lifecycle behavior.

### Fixed

- Run both MCP configuration sync and Codex profile sync through the target
  repository gateway and working directory when `.codex/dispatch-env.toml` is
  present, while preserving the existing vault-local fallback.
- Normalize only the registered Swarm branding aliases in the frozen RC4
  engineering-eval source projection, preserving byte-identical prompts while
  every non-branding contract change still fails closed.

## [2.6.6-rc4-fix2] - 2026-08-09

This bounded RC4 patch adds an external read-only Harness dashboard and closes
the exact lifecycle and evidence gaps found while dogfooding it. It does not
change pipeline DSL authority, provider routing, review topology, or durable
Harness ownership.

### Added

- Added an idempotent companion dashboard that projects real compiled
  pipelines, routes, steps, loops, review lanes, terminal history, and bounded
  recent issues without owning lifecycle state.
- Added restrained semantic terminal colors with byte-stable `--no-color`,
  non-TTY, JSON, and one-shot plain output.

### Fixed

- Keep the newest genuinely running pipeline visible inside continuously
  redrawn terminal panes while compacting old attention-only programs and
  reporting truthful hidden counts.
- Bind missing exact-HEAD verification to the current durable attempt, prevent
  stale children from hiding missing evidence, and make caller-alias marker
  recovery signal-safe.
- Restore the dashboard's leaf dependency boundary, exact tracked-tree gate
  provenance, and declared code-quality release gate.

## [2.6.6-rc4-fix1] - 2026-08-08

This bounded RC4 dogfood patch repairs seven observed dispatch, review,
diagnostic, cancellation, and vault-log failures without changing the RC4
pipeline DSL, review topology, provider routing, or security boundary.

### Fixed

- Start reviews from exact offscreen cmux surfaces instead of requiring the
  coordinator surface to be visible.
- Bind task diagnostics to the durable Harness owner.
- Resume the exact live executor after a recoverable review-drive failure.
- Keep folded log extracts wikilink-safe.
- Compile Claude callback permissions through edit-only path rules.
- Cancel the exact owned lifecycle subtree child-first and report a blocked
  cascade as partial instead of claiming success.
- Resume an exact terminal review-resolution handoff from its durable accepted
  findings without replaying reviewer or provider effects.

## [2.6.6-rc4] - 2026-08-08

This control-plane candidate makes review routing and callback continuation
deterministic without adding another orchestrator or provider route.

### Added

- Added one canonical effective-review-topology digest shared by validation,
  finalization, and runtime launch.
- Added a machine-checked lifecycle transition certificate and a six-part
  engineering review denominator covering quality, implementation, testing,
  simplification, documentation, and security.
- Added bounded skill-quality evidence and exact release-boundary fixtures.
- Added a committed exact-HEAD gate bundle and a distinct RC4
  accepted-deviations artifact.
- Added `harness-cli.py dashboard`: a read-only English terminal view that
  projects the real compiled pipeline, parallel lanes, loop visits, one bounded
  cmux surface probe, and bounded recent issues. It holds no lifecycle
  authority and classifies anything it cannot resolve exactly as
  `request-coordinator-classification` instead of rendering it as progress.

### Fixed

- The live Harness dashboard now prioritizes newest running work, fits each TTY
  redraw to the current terminal height, and compacts older attention-only
  programs without truncating the underlying read-only projection. A restrained
  semantic ANSI palette distinguishes complete, running, waiting/review, retry,
  attention, and model tokens; `--no-color`, non-TTY, and `--once` output remain
  plain. Missing exact-HEAD verification is attention unless a running child is
  durably bound to the current attempt, and persisted dashboard markers can no
  longer reuse or close the caller surface.
- The harness dashboard now renders one dispatch as one tree. Verification
  children, review parents, and review rounds nest under the compiled step that
  executes them instead of appearing as unrelated top-level programs, each step
  shows the frozen runtime/model/effort and preset of the record that runs it
  (`unknown` when the metadata is absent), an active verification no longer
  leaves the finished implementation highlighted, and a nonterminal operation
  owning no runtime resource is reported as unresolved rather than live.
- Restored the live `scripts/` scope ratchet: it measures the working tree again
  instead of a frozen historical commit, under explicit RC4 ceilings declared in
  `tests/rc4_scope_ratchet.py` (268 files, 91,300 lines). The dashboard surface
  now includes the extracted read-only receipt validator; its review corrections
  bind fix visits to accepted callbacks, serialize marker recovery with atomic
  writes, resolve exact frozen custom pipelines, recover bounded stale startup
  states, and distinguish failed/cancelled roots from successful completion.
  Exact protected amendments are now validated from the authoritative
  escalation chain and bound by record identity and digest into review
  ContextPackets. Accumulated history now retains the active review lineage and
  axes under every display cap, reports dropped child/lane counts, and cannot
  let a failed verification from an old HEAD poison current accepted evidence.
  Missing current-HEAD evidence no longer falls back to historical success, and
  a split response can never alias, receive input through, or close the caller.
  Tight panes reserve the newest live identity before terminal history and
  issues, verification input identity has one production owner, ANSI tokens are
  matched once without substring collisions, and stale exact-HEAD gate evidence
  fails closed when the current candidate tree differs.
  The RC2 numbers are
  retained in `tests/test_v266_rc2_scope.py` as historical evidence only and no
  longer stand in for the ratchet.
- Resume accepted review callbacks incrementally and exactly once across crash
  prefixes, stale surfaces, changed-HEAD verification, and zero-lane preflight.
- Bind release evidence to its exact plan, Outcome Contract, artifact root,
  reviewed bytes, and candidate HEAD.
- Require semantic provider activity before recording initial input accepted,
  including the current Claude spaced activity display.
- Cancel the exact owned lifecycle subtree. `harness-cli cancel|close` now walks
  the `parent_operation_id` lineage under the same durable owner and
  terminalizes every exact descendant child-first before the root, so a root
  cancellation no longer leaves a review parent and review-round
  `awaiting-callback`. A cascade blocked by a nonterminal descendant reports
  `"status": "partial"` with the requested root and the blocking descendant, and
  exits `3` rather than reporting success.

## [2.6.6-rc3] - 2026-08-07

This final evidence-polish candidate closes the residual RC2 review findings
without expanding runtime capability or lifecycle authority.

### Added

- Added post-commit exact-tree inventory sidecars, prospective per-slice
  receipts, an append-only attempt ledger, and a typed compiler over actual
  gate/review/finding bytes.
- Added reproducible coverage observations and a portable shell-test scratch
  allocator for constrained macOS environments.
- Added an executable RC3 evidence map plus focused documentation and release
  contracts.

### Changed

- Made normative implementation plans English by default unless the user
  explicitly requests another language; user-facing conversation remains in
  the user's language.
- Aligned root, runtime, task, testing, review, and Russian release guidance
  with the Harness/runtime-worker lifecycle that remains after RC2 deletion.

### Fixed

- Count every full-profile candidate execution mechanically, including
  unpublished and test-only attempts, with an operator-authorized hard
  eight-attempt ceiling and a digest-bound authorization record.
- Reject candidate, gate, review, finding, waiver, output, or profile drift
  from machine-readable RC3 evidence.

## [2.6.6-rc2] - 2026-08-07

This polishing candidate repairs exact callback/evidence ownership and removes
the proven-unreachable classic cmux contour without adding orchestration.

### Added

- Added one bounded `vault-repair` skill and exact blocked-Stop handoff for
  Codex and Claude Code, reusing the existing recovery, validation, and scoped
  commit pipeline.
- Added an exact-candidate secret check for added content and newly tracked
  secret-container paths to the immutable release profile.

### Fixed

- Bound release receipts to the exact subject HEAD and profile, rejecting
  parent/descendant drift.
- Made callback acceptance one public atomic Store transaction and completed
  already-accepted resource-free review dispatches exactly once.
- Made durable terminal approval resumable after a crash without replaying
  provider, callback, verification, process, or cmux effects.
- Required implementation review authority to validate the complete immutable
  evidence bundle, including every output sidecar, before reviewer launch.

### Removed

- Removed seven zero-caller classic supervisor, watchdog, trust-prompt, and
  review-archive compatibility files: 2,217 physical lines in the frozen tree.

## [2.6.6-rc1-fix2] - 2026-08-07

This interim RC1 patch fixes exact cmux cleanup reconciliation after a provider
has already exited successfully.

### Fixed

- When `cmux identify` no longer returns a caller for a closed surface, the
  adapter now confirms absence through the exact cmux tree instead of treating
  the missing caller object as an adapter error. Completed `request-exit`
  effects can therefore clear their exact owned resources and let protected
  research continue to synthesis without replaying fetch or provider effects.

## [2.6.6-rc1-fix1] - 2026-08-07

This interim RC1 patch fixes four dispatch failures found while dogfooding the
packaged release. It does not broaden the RC1 lifecycle or orchestration scope.

### Fixed

- Pinned Codex resume launches to the task worktree so a native current-folder
  chooser cannot redirect an unattended continuation.
- Preserved bounded provider-start diagnostics and transport-stage receipts
  when a launch fails before input delivery.
- Waited for a fresh cmux terminal to become readable before sending the task
  command, tolerating transient empty and malformed surface reads.
- Made exact-HEAD `review.mode=skip` terminalize through the code-owned review
  path without creating reviewer or provider effects.

## [2.6.6-rc1] - 2026-08-07

This release candidate is a deletion-first lifecycle simplification. It removes
legacy cross-HEAD recovery authority, makes reviewer liveness observational,
and seals Split dispatch, replay, and Join to one immutable base commit.

### Changed

- Removed the incident-bound compatibility lifecycle and reduced the active
  authority contour while preserving exact-attempt evidence.
- Made callback recovery attention-only until a supported runtime publishes an
  authenticated turn-complete event; time and screen stability cannot submit
  input or restart a provider.
- Bound every Split child request, task contract, launch receipt, terminal
  receipt, replay, and Join result to the manifest's sealed base SHA.
- Added exact replay validation against durable child metadata and fail-closed
  handling for missing, conflicting, duplicate, or unrelated ancestry.

### Release boundary

- Publication is gated by the immutable 15-command release profile and a final
  Fable release review on the packaged exact HEAD. The sole minor
  configuration-cleanup finding from the implementation candidate is deferred
  to RC2.

## [2.6.5] - 2026-08-05

This is a minimum-stabilization release. Its full technical gate is green;
terminal exact-HEAD review is explicitly waived, and all accepted residual
findings are retained in the 2.6.6 lifecycle-debt plan.

### Added

- Added immutable exact-HEAD `ReviewAttempt` and bounded
  `VerificationAttempt` records. One coordinator-authorized mechanism-flake
  decision may create same-HEAD attempt 1 without a model call; attempt 0 stays
  durable and a second retry stops with typed attention.
- Added a closed, cursor-bound `ProviderEvent` stream for delivery, progress,
  exit and resource closure, with equal Claude print and Codex exec ephemeral
  adapter contracts.
- Added atomic five-cycle finalization, freshness-bounded independent-provider
  availability and additive PipelineSpec finalization metadata.
- Added governed Split manifests. `$split` remains a zero-effect preview;
  explicit `$split --dispatch` drives bounded workspace-local waves through the
  existing dispatch adapter and joins exact approved resource-free receipts in
  manifest order.

### Changed

- Interactive Codex tasks and continuations now receive one content-addressed
  pointer to the complete prompt artifact instead of a multiline editor paste;
  Claude delivery remains unchanged. The native update dialog selects `Skip`
  for the current launch and leaves installation and future reminders to the
  user.
- The `tdd` skill now requires an unknown adapter/runtime mechanism to be
  proven first by one disposable live `prototype`, then promoted into a
  real-seam RED regression and focused GREEN before the broad gate.
- The manual transport prototype can opt into a model-free CMUX layout smoke:
  one isolated workspace, extra tab, right/left splits, exact tree checks and
  exact cleanup with no workspace or surface tail.
- Added the exact-HEAD attempt path. Legacy V3/pre-activation continuation and
  bounded review-drive rearm remain accepted compatibility debt for 2.6.6.
- Added typed provider-event and delivery paths. Legacy screen/time recovery
  authority remains accepted debt; Stop remains the callback-submit owner on
  the new path.
- Task metadata v4 optionally carries an immutable Split child policy without
  changing v1-v3 read compatibility or ordinary dispatch behavior.

### Fixed

- Current-checkout callback notifications no longer append the synthetic
  review-scope file as legacy `current --plan`; their exact command now reuses
  the original purpose/boundary identity and can ingest the ready callback.
- Made the synthetic task-summary publisher used by runtime tests atomic, and
  proved the stable-read watcher cannot observe partial JSON.
- Kept immutable escalation records durable while ignoring their runtime
  directory, so raised and resolved evidence survives with a clean Git status.
- Made Codex subscription detection require a zero exit and the exact supported
  logged-in marker across normalized stdout/stderr; warnings alone fail closed.
- Bound dispatch context aliases to exact wiki path stems and added strict typed
  exact-binding repair for the one case inference cannot safely represent.
- Reconciled an already durable reap completion only against its exact pending
  effect and receipt identities, without repeating the vault or provider effect.
- Bound Split activation to a green pre-activation Stability Gate and stopped
  every invalid manifest, budget, receipt, dependency, HEAD or resource state
  before replaying a child or provider effect.
- Recorded that ephemeral adapters are conformance-ready but not yet selected
  by production review, late-cycle independent availability is not wired, and
  Split ownership still needs an exact sealed base commit.

## [2.6.4] - 2026-08-04

### Added

- Added harness-owned recovery for a missing reviewer submit. Recovery is
  bound to the exact operation, run, lane, generation, callback target,
  deadline, process/surface ownership, and the existing shared one-nudge
  ceiling; stable typed input, callback, or receipt wins without a model call.
- Added append-only escalation, resolution, and plan-amendment records. The
  latest attention marker is now a bounded pointer to immutable decision
  history, with deterministic legacy-marker backfill.
- Added `task-review-runner.py plan`, a purpose-safe plan-review facade with
  protected Outcome/disposition/evidence regions, exact base/head OIDs, and
  ready-to-run bounded inspection commands.
- Added one-shot Wiki self-healing for uniquely resolvable title/H1 wikilinks
  through the canonical transactional writer, followed by index rebuild and
  strict validation.

### Changed

- Harness now publishes accepted resource-free reviewer callbacks, rearms an
  exact timed-out parent only under durable identity checks, and cleans up only
  exact-owned superseded reviewer resources. Active or unknown ownership still
  fails closed.
- Same-session continuation distinguishes transport acceptance from provider
  acknowledgement. A prompt is never marked successful merely because paste
  and Enter RPCs returned zero; one identity-bound Enter retry may consume the
  shared liveness budget, otherwise typed attention is recorded.
- Callback wake delivery is serialized per exact operation and uses
  write-ahead paste/submit phases. Partial or ambiguous effects fail closed,
  while concurrent reconciliation cannot send a second provider-facing wake.
- Continuation Enter delivery now persists an exact generation reservation
  before the external key effect. Crash replay sends no second Enter, and exact
  accepted-generation retirement is idempotent under concurrent workers.
- Missing screen evidence after the first continuation Enter stops at a typed
  unconfirmed result instead of consuming retry budget for another Enter.
- Dispatch context preserves the exact resolved wiki path instead of treating
  a display title as a file identity. Review inspection requires canonical
  full object IDs.
- All harness test suites and production entrypoints are checked against the
  standing Makefile and coverage denominator.

### Fixed

- Reconciled the exact `sent` state plus `reserved` callback-submit receipt
  before the accepted-callback fast return, so acceptance-before-restart cannot
  strand the next reviewer generation.
- Replaced the release-bound E6/E14 fixture tail with production review-gate
  and task-summary entrypoints, and added a public Deep-facade iteration test;
  direct lifecycle-helper calls are no longer counted as unattended evidence.
- Bound E14 to an effect-recorded two-pass `engineering/fix` traversal with
  seven typed step receipts, fail-to-pass verification, a real typed Sol
  approval, checkpoint, accepted summary callback, reap effect, and production
  cleanup to a resource-free `complete` parent.
- Ignored stale prior-generation callback receipts during current-generation
  recovery and settled typed artifacts that win after reservation without a
  duplicate provider prompt or Enter.
- Made callback liveness state and receipt publications directory-durable
  before provider-facing effects.
- Healed the exact callback-submit crash phase where durable state was already
  `sent` but its separate receipt remained `reserved`; restart advances only
  the matching receipt, never repeats provider input, and fails closed on
  malformed bytes.
- Prevented silent pipeline stalls after completed reviewer output, accepted
  callback races, callback-rearm crashes, and retained prompt text that was not
  actually submitted.
- Prevented unattended Codex reviews from stalling at the native rate-limit
  model-switch dialog. The exact prompt keeps the route-bound model without
  disabling future reminders; unknown choices still receive no input.
- Restored the frozen plan's exact UTF-8 Outcome bytes after an intermediate
  documentation transaction introduced replacement characters and correctly
  triggered a pre-provider digest rejection.
- Bound the user-requested 2.7 TaskGraph/project-task backlog entries to the
  exact planning-only accepted-deviation ledger.
- Closed the release-evidence gap between callback acceptance and lifecycle
  completion: the dogfood trace reaches resource-free terminal parent/child
  states and an actual `reap-ready` pipeline boundary, with a separate final
  harness-authority trace.
- Replaced fixture-owned terminal transitions and fixed manual-effect counters
  in the E6/E14 dogfood with production review acceptance, exit/cleanup and
  pipeline advancement plus an effect-derived zero-manual-action assertion.
- Bound both accepted and duplicate callback receipts to the exact broker
  callback and payload identity, and made continuation paste replay
  write-ahead/fail-closed so a crash cannot paste the same prompt twice.
- Prevented plan review from silently defaulting to implementation review or
  reusing a retained lane after protected plan-contract drift.
- Prevented newer coordinator decisions from overwriting older escalation and
  amendment evidence.
- Removed a full-suite-only verification recovery test race by waiting for the
  exact packet with a bounded deadline and joining its responder before cleanup.
- Removed two additional task-summary fixture races by synchronizing helper
  readiness/completion and waiting for atomic recovery receipts before cleanup.
- Removed a trace-only summary refresh race by giving both bounded responder
  threads the documented eventual window and joining them before assertions.
- Removed a full-suite-only pipeline-fix retry fixture race by applying the
  same bounded atomic-publication window to its retry-intent receipt.
- Removed a traced full-suite fake-provider race with a bounded fixture-only
  polling ceiling; production provider and callback deadlines are unchanged.
- Retired a sent callback-recovery generation only after exact durable broker
  acceptance, so the next retained-session generation remains observable while
  active without gaining another prompt, Enter, nudge, or restart budget.
- Made that retirement replayable across a crash between the accepted receipt
  write and liveness-state clear; replay retires only the exact matching sent
  binding and leaves all shared recovery budgets consumed.
- Bound continuation Enter retry to the same callback-target digest and full
  generation identity used by worker liveness. Pending-effect replay now
  confirms activity and marks the exact reservation sent without another
  attempt, prompt, or key.
- Made append-only escalation publication directory-durable: first-use records
  directory creation and every immutable record entry are fsynced before the
  latest pointer can be replaced.
- Added a direct fail-closed liveness assertion for an uncertain callback
  submit without an exact reservation; the standing coverage floor was not
  lowered.
- Bound resolution-time plan and Outcome semantics to the exact reviewed Git
  object, allowing append-only fix amendments without weakening fresh-plan
  digest validation.
- Preserved ignored release evidence during resolution only when its confined
  file bytes match the original frozen boundary digest; tracked evidence stays
  Git-backed, and only a positively absent reviewed-tree entry may use the
  fallback.
- Made continuation retry stage publication crash-safe: the exact generation
  binding reaches durable `sent` before the `submit-retried` receipt, so replay
  cannot inherit a more advanced receipt with a reserved liveness effect.
- Kept same-axis verification callbacks visible across Deep/Full lane barriers:
  an older recorded iteration no longer hides a newly accepted iteration, while
  exact recorded iterations remain idempotently filtered.
- Restored atomic pointer materialization for resolution-bound review inputs
  larger than the inline packet limit after the lifecycle-module extraction.
- Made equal attention-marker replay retry an earlier failed authoritative
  state transition instead of silently leaving the operation stranded.
- Allowed the canonical one-shot wikilink repair to update a writer-owned
  log/hot surface only when its entire optimistic payload exactly matches the
  planner's current derivation; forged and ordinary direct updates stay blocked.

## [2.6.3] - 2026-08-04

### Added

- Added a versioned 24-page Russian technical handbook with explicit routes
  for first-time users, operators, PipelineSpec authors, and maintainers; a
  complete 34-skill invocation catalog; runnable examples with expected,
  verification, failure, recovery, and authority fields; and release-candidate
  documentation.
- Added a practical manual fan-out/join guide for splitting one large outcome
  into independently owned plans, dispatching multiple task worktrees, and
  integrating accepted exact HEADs without claiming an automatic task graph.
- Added `make test-docs`, mutation-sensitive handbook checks, a source/coverage
  matrix, three disposable-checkout dogfood walkthroughs, and a strict
  documentation PipelineSpec example compiled entirely from the existing
  primitive, `tdd`/`review` skill, and named-check registries.
- Added protected-source claim rulings for eight fetched primary sources and
  preserved the invalid nine-citation synthesis rejection as negative evidence.

### Fixed

- Preserved any exact accepted callback as terminal completion during owned
  cleanup; protected research supplies the release regression. An exact-identity
  cancelled fetch receipt may recover
  only while its research parent is nonterminal; a digest, run, request, or
  artifact mismatch starts no synthesis child or provider. A terminal
  composition is never resurrected, and cancelled-synthesis recovery remains
  unsupported. Cleanup or explicit cancellation after an exact callback was
  accepted ends as `complete`; cancellation without one remains `cancelled`.
- Repaired fresh-worktree MCP config bootstrap: only the default
  `sync-config --apply` path may atomically initialize missing `runtime.env`
  from its strictly validated committed sibling example with owner-only mode.
  Check/print, custom paths, direct calls, invalid examples, and symlinks remain
  write-free and fail closed.
- Repaired coordinator-authorized review recovery for a mixed-HEAD
  `awaiting-resolution` gate. It can enter the existing fresh boundary only
  after every retained parent and current round is terminal, resource-free,
  and free of pending effects; verification is clamped to zero iterations.
- Added recovery-only compatibility for terminal pre-schema review rounds whose
  sole specification difference is a missing historical `parent_operation_id`.
  Stored records are not rewritten, and live or otherwise drifting identities
  remain fail-closed.

### Changed

- Evaluated but did not ship a `document-project` skill. A fresh no-skill
  control already satisfied all four required documentation behaviors, so the
  approved no-improvement stop condition removed the candidate, its router and
  compiler registrations, and its temporary 8,000-byte registry cap. The
  reusable quality contracts and typed rejection evidence remain in docs.
- Split the newly added review-recovery facade into cohesive authorization,
  legacy-round, and resolution-evidence modules, then centralized the durable
  research callback payload/identity shared by producer and recovery. Focused
  tests pin the refactor and the accepted-callback result of explicit `cancel`.

### Compatibility

- This is a documentation-first patch release. Existing runtime, permissions,
  providers, fallbacks, built-in pipeline descriptors, and custom PipelineSpec
  ceilings are unchanged outside the exact regression-covered research
  callback, default MCP runtime-config bootstrap, and review-recovery repairs.
- The bootstrap is a separately authorized compatibility exception: the default
  `sync-config --apply` path gains only the bounded filesystem authority to
  create missing canonical `runtime.env` from its validated committed example.

## [2.6.2] - 2026-08-03

### Fixed

- Made cmux workspace progress show only exact live dispatch, review, and
  research programs from the coordinator origin workspace and clear
  immediately when the workspace is idle. Terminal controllers now suppress
  stale descendants, and controllers with known missing exact surfaces no
  longer look active, including stale launch attention.
- Added one bounded live-tree inventory per publish. An unknown probe preserves
  the existing UI and never mutates harness lifecycle state; Claude and Codex
  retain identical content-free labels and cleanup behavior.
- Coordinator SessionStart now refreshes stale progress without granting task
  worktrees coordinator authority.
- Preserved the pre-integrated turn-end save-and-close repair and pinned the
  trusted env-shebang interpreter and exact executor product root for ordinary
  provider launches.
- Preserved bounded hot-thread cache eviction: a full Active Threads cache now
  evicts its oldest entry instead of rejecting a new current thread.
- Restored reviewer-local Claude usage visibility with a code-owned standard
  status line for model, effort, context, 5-hour, and 7-day limits through a
  subscription-compatible profile. User/project/local setting sources and
  ordinary Claude memory are excluded; skills, marketplace autoinstall, MCP,
  network, product writes, and arbitrary status-line commands stay disabled.
- Documented that foreign stale-controller uncertainty preserves the current
  bar and must be recovered through exact harness diagnose/reconcile/cancel.

### Changed

- Moved the sole historical `docs/plans` v2.1.1 plan into canonical
  `wiki/plans` with executed-plan provenance, a DragonScale address, preserved
  body, and its validated final result link.

## [2.6.1] - 2026-08-03

### Added

- Added explicit `--full` review as a four-lane provider/responsibility grid;
  Full is never selected automatically. Deep review now gives each default
  provider an independent holistic view, while an explicit single-model Deep
  review separates intent and engineering responsibilities.
- Added provider-stable public lane identities (`anthropic-*`, `openai-*`) and
  retained model-family names only as centralized routing aliases.

### Fixed

- Repaired fresh current-review selection, exact same-session continuation,
  bounded delta transport, callback/checkpoint races, and exact reviewer cleanup
  discovered by Full-topology dogfood.
- Accepted terminal callbacks can finish when cmux did not materialize a resume
  checkpoint; missing evidence is typed and never authorizes another effect.
- Prevented independent review lanes from colliding on a shared local finding
  ID without rewriting trusted callback bytes, and kept outcome review active
  when no implementer summary exists.

### Security

- Claude and Codex reviewers can write only their current lane's callback/test
  scratch. They cannot forge sibling-lane callbacks or write the product tree.
- Codex reviewers exclude ambient temporary write roots, disable reviewer
  network access, and filter credential-like variables from shell subprocesses;
  persisted reviewer commands are revalidated fail-closed before execution.

### Compatibility

- Review lane IDs and the review-v1 axis vocabulary intentionally changed.
  Finish or cancel active 2.6.0 reviews before upgrading; they are not migrated.

## [2.6.0] - 2026-08-02

### Added

- Added a canonical Outcome Contract v1 embedded in approved plans, stable
  contract digests in task metadata v4, reserved ContextPacket delivery, and
  Wiki Summary v2 outcome dispositions with bounded evidence and residual-gap
  pointers.
- Added bounded `review-inspect`, identity-bound per-finding review resolution,
  content-free review telemetry, deterministic paired evaluations, and a
  five-pass `improve-skills` audit with explicit goal-preservation checks.
- Added provenance-aware command evidence and RT10 runbook distillation for
  both agent-executed commands and strictly typed user-attested results.

### Changed

- Refined `clarify`, `design`, `prototype`, `save-plan`, `debug`, `tdd`,
  `review`, and `reap` using the applicable general practices from pinned
  Superpowers and Matt Pocock Skills snapshots while retaining the existing
  harness-first lifecycle.
- Review now treats implementer summaries as unverified claims, checks the
  approved outcome before mechanics, classifies every declared evidence item,
  and preserves independent Fable/spec and Sol/engineering axes in deep mode.
- Fresh tasks use the clean v4 metadata and severity vocabulary; historical
  v1-v3 operations and summaries remain readable but are never rewritten or
  silently migrated.

### Fixed

- Repaired accepted-callback cleanup, stalled review verification and
  finalization recovery, stale callback liveness, fresh-review resolution
  identity, protected-research cleanup/error normalization, and dispatch
  worktree Git-status leakage.
- Restored the compact debug skill's explicit invocation, diagnosis-only,
  feedback-loop, and residual-uncertainty governance markers without changing
  its three-failure architecture stop.

### Security

- Outcome fields cannot authorize effects, widen permissions, continue a typed
  stop, or create another scheduler, pipeline engine, review lane, or model
  call in deterministic transitions.
- Reviewer inspection remains bounded and read-only; callback, resolution,
  verification, cleanup, and reap evidence stay bound to exact operation,
  receipt, callback, plan, contract, and Git identities.

## [2.5.1] - 2026-08-01

### Added

- Added an exact stale-operation diagnostic with identity-bound recovery
  guidance, a same-session review delta prototype, and a pinned comparison of
  the Superpowers and Matt Pocock skill libraries.
- Added durable real-task dogfood evidence for nine independent tasks spanning
  change, fix, prototype, research, and vault-health workflows.

### Changed

- Tightened task/review lifecycle contracts around exact surface ownership,
  canonical summaries, prior-phase evidence, bounded telemetry verdicts, and
  explicit review callback validity.
- Preserved shared plans during reap, bounded skill documentation, and repaired
  vault navigation found by the real-task audit.

### Fixed

- Fixed abnormal acceptance cleanup, stale operation release proof, ambiguous
  reap modes, callback races, and review finalization ordering.
- Protected research now classifies a dead provider immediately, persists its
  resolved runtime interpreter, and bypasses the cmux wrapper only for isolated
  fetch/synthesis. Dispatch and review keep their exact-surface wrappers.
- Fixed task summary rendering and stale identity diagnostics so repair and
  resume remain bound to the exact operation and accepted phase evidence.

## [2.5.0] - 2026-07-31

### Added

- Added bounded model-authored `PipelineSpec` data, strict compilation,
  immutable approval snapshots, typed branching/loops, and registered checks
  on the existing harness lifecycle.
- Added code-owned liveness recovery and content-free promotion reporting for
  repeated successful custom definitions.

### Security

- Kept provider routes, commands, permissions, effects, dependencies, and
  approval outside model authority; custom definitions cannot widen the
  selected built-in baseline.

## [2.4.1] - 2026-07-31

### Added

- Completed the executable `engineering/fix` profile with persistent
  reproduce, root-cause, regression-test, minimal-fix, verification, review,
  and reap-ready phases.
- Added immutable phase receipts, bounded retry/restart policy, typed
  `cannot-reproduce` decisions, and restart-safe resume from accepted evidence.

### Changed

- Kept compiled pipelines on the single 2.3 operation store, supervisor,
  provider session, callback seam, and coordinator-owned finalization path.

## [2.3.0] - 2026-07-30

### Added

- Added a restartable owner-scoped harness kernel with typed operation specs,
  atomic state, write-ahead effects, exact callbacks, reconciliation, and
  `status`/`inspect`/`resume`/`cancel`/`close`/`doctor` commands.
- Added the public `review` and protected `research` workflows plus the
  `debug`, `tdd`, `design`, `prototype`, and `resolve-conflict` engineering
  skills.
- Added unified simple/deep review contracts and a four-cell, exact-SHA live
  acceptance driver for Claude, Codex, cross-runtime composition, and deep
  review.
- Added one state-driven review facade for both dispatched tasks and the
  current checkout; ad-hoc review no longer requires hand-built harness
  pointers or `.task-meta.json`.

### Changed

- Moved cmux, provider process, worktree, context, callback, verification,
  retry, and cleanup mechanics behind code-owned harness modules.
- Centralized Sol, Terra, Opus, and Fable aliases and review profiles in
  `config/model-routing.toml`; the Opus alias pins `claude-opus-5` rather than
  relying on a host-moving default.
- Protected research now uses vaultless fetch, networkless synthesis, hashed
  pointer artifacts, and coordinator-owned vault writes.
- Version 2.3.0 is a clean runtime baseline; pre-harness operation state is not
  migrated.

### Removed

- Removed `dispatch-workspace`, `review-dispatch`, `review-send`, `reap-send`,
  and `autoresearch` without compatibility aliases. Use `dispatch`, `review`,
  `reap`, and `research`.
- Removed the legacy skill-by-runtime acceptance implementation and prompt
  baselines in favor of hermetic replay coverage and four bounded live cells.

### Security

- Wrong-run, late, terminal, mutated, and duplicate callbacks now fail closed
  or become an idempotent no-op according to exact operation state.
- Review approval is published only after provider exit and exact owned-resource
  cleanup.

## [2.1.3] - 2026-07-29

### Fixed

- Fixed primary coordinator-review cleanup so root-scoped review state is no
  longer mistaken for a v3 broker operation merely because `--state-dir`
  points at the canonical checkout. An approved coordinator reviewer now closes
  its exact cmux surface after process exit without requiring nonexistent
  `project_id`, `task_id`, or `lane_id` values.
- Preserved fail-closed broker behavior for real v3 operation directories:
  missing or corrupt broker identity still leaves the exact reviewer surface
  visible and retryable.
- Added a hermetic regression covering the complete root-state
  `request-exit` → `after-exit` path and exact-surface close.
- Made the protected-research test harness discard ambient coordinator session
  routing before launching subprocess fixtures, so `make test` remains
  hermetic inside active Claude and Codex sessions without changing product
  routing.
- Kept acceptance coordinator identity stable when Codex filters inherited
  environment variables from tool commands: disposable acceptance clones now
  persist their synthetic session ID locally, while normal Claude/Codex
  sessions keep their existing identity precedence.
- Restored opposite-model Claude review callbacks by allowing both native
  editors on the same exact `.review-outbox.json` target. The reviewer remains
  `dontAsk`, product-read-only, and unable to write any other checkout path.
- Kept the exact native workspace-trust bootstrap active for a bounded
  30-minute window instead of abandoning slow cold starts after 120 seconds;
  the lower polling rate avoids lifetime subprocess churn on long tasks.
- Requested both UUIDs and short refs explicitly from current cmux tree output,
  while retaining compatibility with older CLIs, so review/dispatch can resolve
  the caller's exact workspace instead of failing after cmux changed its default
  ID format.
- Live primary coordinator reviews now anchor to the current exact
  `CMUX_SURFACE_ID` instead of reusing a stale root-level task handoff from an
  older review cycle.
- Root-scoped coordinator reviews no longer capture or validate an unused
  resume checkpoint; broker-scoped review rounds retain their existing
  checkpoint behavior.
- Explicit primary-checkout reviews now use their own bounded approve/resolve/
  escalate policy instead of inheriting stale legacy task metadata. A verified
  approval is mechanically applicable, and finish arms exact-surface cleanup
  even when no unattended dispatch contract exists.
- Replaced model-authored `reap`/`reap-send` acceptance setup with a
  runner-prepared v3 task, canonical summary, exact coordinator/task surfaces,
  and a readiness handshake. The live cells still exercise real opposite-model
  review, duplicate-safe reap-send, final reap, graceful task exit, and
  independent durable proof, without failing on invented plan addresses.
- Pinned unsafe-research acceptance to one stable official PEP page with a
  bounded same-URL GET fallback, so the cell tests its single-context route
  rather than an unreliable documentation endpoint choice. Only that exact
  Codex acceptance row receives outbound proxy access to `peps.python.org`;
  every other acceptance and product task policy is unchanged.

## [2.1.2] - 2026-07-21

### Added

- Added a generated fail-closed acceptance dependency lock covering static
  Python imports, constant code/data paths, runtime registrations, and explicit
  dynamic repo-path declarations without executing product or historical code.
- Added a committed minimal seed vault and deterministic synthetic seed commit,
  keeping live fixtures independent of the working wiki and `.vault-meta/` data.

- Added content-free per-turn and runner-stage timing to the existing bounded
  pipeline event stream, including incomplete-turn accounting and p50/p95
  reporting without prompts, responses, commands, or error text.
- Added content-addressed acceptance evidence with exact per-cell dependencies,
  production model-generation tracking, row integrity hashes, evidence age,
  atomic checkpoints, and fail-closed selective reuse.

### Changed

- Split the monolithic live-acceptance runner into contracts, sandbox,
  launchers, prompting, scenario adapters, and skill adapters behind the same
  CLI. The refactor preserved all 58 v2.1.1 prompts byte-identically on pinned
  inputs before the reviewed fixture corrections below.
- Revised six live fixtures (backlog, daily, distill-runbook, learn, reap, and
  wiki-query) under the v2.1.2 prompt baseline to clarify operational setup
  without changing their expected behavior contracts; the other 46 rendered
  prompts remain byte-identical to v2.1.1.
- Replaced unknown-path global invalidation and historical evidence migration
  with evidence epoch 3 and semantic per-cell fingerprints. Data-only,
  packaging-only, orchestration-only, and same-generation model-alias changes
  reuse evidence; unregistered runtime edges stop the model-free check.
- Acceptance now records the exact launched model while fingerprinting its
  registered major generation, runs by default in two owned cmux workspaces
  with five cells each, checkpoints every completed cell, and resumes only
  unfinished fingerprints.

- Compacted the normal dispatch, review, reap, reap-send, and close skill paths
  around deterministic repo-owned runners and conditional compatibility
  references, reducing normal-path orchestration context by about 30% while
  retaining semantic decisions and safety gates in the model.
- Live acceptance can select bounded skills, hashes only the exact fixture and
  scenario registry fragments for each cell, and records the actual cheaper
  Sonnet/Terra test model separately from the production generation.
- Exact reviewed release-packaging paths are classified as non-behavioral, so
  metadata-only changes reuse valid acceptance evidence. Unregistered runtime
  edges stop the model-free dependency-lock check; unknown non-runtime paths
  neither invalidate evidence nor bypass the runtime graph.
- Pipeline timing reports completed and incomplete model turns by runtime and
  coordinator/task/reviewer role; incomplete turns carry no fabricated latency.

### Fixed

- Made operation-scoped review drive callbacks fully independent of the
  executor's current directory by carrying absolute script, worktree, and
  action-handoff paths.
- Kept resumed scratch reviewers on their existing owner-only working
  directory instead of letting a model reconstruct a stale nested path.
- Run one code-owned Claude subscription preflight before allocating live
  acceptance workspaces, avoiding model-side credential-status probes while
  retaining the normal-session fail-closed check.
- Pin the Codex daily subagent against the observed legacy summary shape and
  retry one invalid response in the same agent thread with the exact validator
  error and schema, without spawning a fallback model.
- Bound automatic retries to at most three attempts for explicit cmux
  allocation and agent-capacity transients. Product, permission, contract, and
  unknown failures are never retried.
- Replaced wall-clock cell termination with content-free screen/lifecycle
  heartbeats, a 15-minute status probe, and an inactivity boundary; exact owned
  surfaces and workspaces are reconciled after every run so empty shells and
  orphan tabs fail the release gate.

- Confirm Claude's exact native background-work exit dialog automatically only
  after an unattended task or reviewer close has been lifecycle-authorized,
  preventing completed task splits from lingering on an interactive prompt.
- Deliver v3 review transitions through operation-bound task-local handoffs,
  so executors run a short deterministic command instead of recopying long
  registry paths that can silently lose a path segment.
- Block final reap while an operation-bound review transition is still pending,
  require its drive command to run standalone, and clear Claude's composer
  before lifecycle `/exit` so suggested text cannot swallow the command.
- Keep duplicate reap-send callbacks idempotent after the coordinator's exact
  prepared plan close, and bind the Codex daily summarizer to the complete
  object schema instead of relying on prose-only shape hints.
- Make daily acceptance account for the canonical writer-owned `wiki/log.md`
  evidence update and one exact address allocation, while backlog acceptance
  restores the inbox byte-for-byte through the canonical writer instead of
  weakening product-residue checks.
- Treat either a real reviewer approval or a reviewed warning/fix/verification
  round as valid live review evidence. Acceptance no longer fabricates a
  finding—or fails a healthy review—merely to force a nondeterministic model
  through the optional verification branch.
- Use the standard `python3` entrypoint and a neutral authoritative-result
  contract for task-local review drive callbacks, avoiding Claude auto-mode
  classifier denials on Homebrew interpreter paths.
- Reviewer completion is now persisted in the exact broker lane before cmux
  closes the supervisor's own surface, preventing an approved review from
  remaining `callback-ready` after its tab disappears. Successful unattended
  reap-send output also omits the already-delivered coordinator command, so a
  task model cannot execute the coordinator-only reap a second time.
- Disposable live-acceptance clones now carry the repository's standard
  auto-commit opt-out marker, preventing host Codex Stop hooks from advancing
  the coordinator HEAD even when the CLI hook-disable switch is ineffective.
- Exact cmux cleanup now resolves and supplies the surface's window/workspace
  anchors, verifies disappearance in the cmux tree, and retries once instead of
  treating a misleading `not_found` response as success. The `/close` live
  fixture reuses its runner-created surface and delegates proof/page cleanup to
  code. When cmux replaces the last surface in an auxiliary split with an empty
  shell, cleanup identifies that one layout delta and exits it so the pane
  collapses instead of leaving an orphan tab.
- Task and reviewer hooks now write only content-free turn telemetry to the
  origin vault; coordinator context injection, command capture, plan capture,
  and the full Stop pipeline remain disabled across the read-only boundary.
- Canonical acceptance evidence refuses dirty behavioral worktrees before
  execution, including same-commit staged, unstaged, and untracked paths, while
  retaining the exact release-metadata exception. Review startup also removes
  stale outboxes before granting the reviewer its single-file write surface.
- Claude live acceptance now loads the exact repo-local plugin, disables
  interactive question UI in unattended cells, carries explicit promotion
  choices in fixtures, and refreshes derived address indexes before validating
  the close fixture.
- Interrupted live acceptance now force-closes only its exact coordinator and
  registered child surfaces instead of waiting through the normal interactive
  shutdown grace and leaving orphan tabs when the outer matrix exits.
- Persistent protected-research callbacks resolve an exact run-to-operation
  locator inside the current vault, keeping callbacks short and eliminating
  model-reconstructed task-session paths while rejecting locator escapes
  fail-closed.
- Protected-research workspaces ship one exact executable notifier that writes
  a typed Codex checkpoint sidecar before callback, while synthesis reindexes
  before validation; neither path depends on model-copied paths or cmux resume
  API calls.
- Autoresearch acceptance now leaves validated product outputs to the runner,
  which resolves the one bound operation, deletes new pages and restores
  deduplicated pages/indexes through one optimistic vault transaction, then
  proves the clone clean without model-written cleanup shell.

- Approved-plan dispatch now uses a typed, idempotent post-approval runner for
  route capture, worktree/task identity, prompt/meta rendering, anchored spawn,
  supervisor launch, and log filing. Coordinators no longer reproduce the
  lifecycle as dozens of shell/tool steps, and a repeated preparing/failed
  request cannot open a duplicate surface.
- Dispatch Phase 1 now has a read-only candidate resolver, first final v3 reap
  has one contract-bound runner, and both paths emit content-free stage timing
  so repeated model turns are reserved for semantic choices rather than
  mechanical orchestration.
- Unattended `reap-send` now validates and sends the exact `reap-runner.py`
  callback instead of asking the coordinator to rediscover the task and replay
  the finalization phases. Reap log/hot entries reuse the result page address,
  and structured writer failures retain their actionable reason.

- Codex task sessions now receive write access only to their exact v3 task
  registry subtree, allowing operation-scoped review callbacks without exposing
  the broader coordinator registry.
- Review archival now resolves the coordinator from the reviewed worktree
  instead of the caller's current directory, so linked-task reviews defer
  correctly even when invoked from the coordinator checkout.
- Live acceptance now contains runner-owned nested worktrees, waits for slow
  interactive agent shutdown, uses bounded scenario-specific timeouts, and
  distinguishes disposable append-only bookkeeping from product residue.
- Defuddle's no-CLI fallback now performs and verifies bounded boilerplate
  removal instead of treating raw fetched Markdown as cleaned output.
- V3 Codex task launch now validates both exact writable roots, avoids parent
  mutations for an already secure registry directory, and recognizes wrapped
  native Claude trust dialogs in narrow panes.
- Dispatch anchors callbacks to the caller's explicit cmux surface instead of
  the globally selected tab, and generated semantic-tiling reports now include
  required session provenance.
- Dispatch records only verified vault context links and stays within the
  enforced skill-size budget; schema validation ignores illustrative links in
  lint reports, log archives, and folder-index templates so reports cannot
  amplify their own findings.
- Dispatch resolves its exact cmux caller through a parsed caller identity,
  bootstraps fresh-clone MCP JSON before scoped Codex sync, and prevents task
  Stop hooks from committing coordinator-owned derived indexes. Vault writes
  now support optimistic, journaled page deletion for canonical cleanup.
- Dispatch acceptance now prepares its exact approved fixture in runner code
  and proves the one-commit/review/reap lifecycle from durable artifacts before
  accepting an agent pass. Claude reviewers use an explicit empty MCP config;
  if the host still presents its exact native project-MCP prompt, the trusted
  supervisor selects “continue without” instead of granting trust. V3 task
  launch also pins the coordinator's canonical DCG profile across linked
  supervisor copies.

## [2.1.0] - 2026-07-18

### Added

- Added owner-only task/session registry state keyed by opaque project, task,
  permission-domain, runtime, and pinned-model identities. Exact session
  bindings support multiple coordinators without guessing by name or recency.
- Added persistent product-read-only review, protected fetch, and protected
  synthesis lanes with typed cmux checkpoint capture, visible fresh-session
  fallback, per-lane FIFO, and task-scoped reap cleanup.
- Added task-meta v3, namespaced review operations, multi-review archive links,
  active-broker upgrade blocking, and macOS cmux capability preflight.
- Added a repo-shipped interactive live-acceptance runner with an exact
  skill/runtime scenario registry, one required real fixture per installed
  skill, disposable committed-HEAD clones, typed evidence, exact-surface
  cleanup, and a single `make acceptance-live` gate.

### Changed

- Every cmux workflow anchors a new split explicitly to the caller's captured
  surface and opens it to the right. Initial/verify review rounds stay in one
  surface; later rounds of the same task/model/domain resume its checkpoint.
- Same-model bounded work defaults to an internal subagent. A visible
  same-model review requires an explicit separate-window request.
- Protected research retains context only inside the exact task and isolation
  domain. Each operation still receives fresh scratch; runtime homes are
  removed only after the task is archived by final reap.
- Trusted review submission now receives and renders callbacks before notifying
  the executor. `review-dispatch drive --apply-action` owns safe approve/verify
  transitions while semantic fixes and escalations remain agent decisions.

### Fixed

- Concurrent reviews in one project no longer overwrite singleton
  `.review-*` metadata, baselines, callbacks, results, watchdog state, or close
  sentinels.
- Reviewer exit now closes only the exact armed surface after process return;
  crash/checkpoint loss is visible and releases the lane instead of leaving a
  permanent busy state.
- Resumed reviewers no longer depend on a newly operation-scoped callback
  permission, and a failed UI notification no longer retries an already durable
  callback transition.
- Live acceptance waits for a stable bounded regular-file outbox, tolerating a
  short non-atomic agent write without accepting symlinks or oversized output.
- Repo-spawned Codex task, review, research, and acceptance sessions explicitly
  use default service; Fast/priority service remains a user-only session choice.
- Live acceptance reports checkpoint atomically after every cell and resume
  only against the same source commit and matrix fingerprint.
- Live acceptance now scopes nested temporary files to the exact operation,
  leaves disposable clone/bookkeeping cleanup to the runner, rejects residual
  product outputs, and gives an interrupted cell time to close its exact
  surface before the matrix process exits.
- Protected fetch and synthesis send cmux a bounded operation-owned launcher
  path instead of an inline command containing long task/runtime paths, so
  persistent lanes cannot be truncated in the terminal composer.

## [2.0.9] - 2026-07-18

### Added

- Added a dynamic cross-runtime release acceptance contract covering every
  installed skill, sanitized evidence ledgers, fault visibility, and explicit
  baseline/final phases.
- Added a once-per-session readiness preflight for runtime routing, generated
  config drift, CLI dependencies, and hybrid retrieval. Missing Ollama or
  `bge-m3` produces exact repair commands while sparse retrieval stays usable.
- Added explicit `unsafe-research` as a separate single-context escape hatch.
  It requires direct user authorization, warns once, inherits the current
  session, and never weakens protected research as a fallback.

### Changed

- Concrete Claude/Codex defaults now live only in
  `config/model-routing.toml`. Dispatch inherits the exact current route, daily
  inherits its model at medium effort, review defaults to the opposite runtime,
  and protected research keeps its Codex isolation.
- Task and review metadata now record resolved model, effort, source, and config
  fingerprint. Same-model review is explicit and can override effort without
  changing model.
- Overlay upgrades refuse active task/reviewer/research sessions, ignore stock
  v2.0.8 reviewer defaults, and migrate only customized legacy routes after
  pre-install validation and explicit confirmation into a gitignored override.

### Fixed

- Unknown models, provider mismatches, invalid effort, generated-config drift,
  and incomplete session routing now fail visibly instead of silently selecting
  another route.
- Daily model defaults are no longer duplicated in runtime-specific agent
  definitions, and a model-literal lint prevents new active-code hardcoding.

## [2.0.8] - 2026-07-17

### Added

- Added `/clarify`, a one-question-at-a-time alignment workflow for explicit
  pre-code interviews. It inspects local facts first, keeps material decisions
  with the user, supports interactive-question tools or plain text, and avoids
  redundant confirmation after the user already authorizes the next step.
- Added RU/EN router hints and false-positive regression coverage for explicit
  clarification and `grill me` requests.

### Changed

- Explicit Codex reviewer reasoning effort is preserved after `--model` through
  a validated argv override.
- Dispatch and review defaults are Claude `fable` high and Codex
  `gpt-5.6-sol` high across runtime configs, generated commands, skills, and
  documentation. Explicit task/CLI overrides remain authoritative; the deep
  Codex profile intentionally remains `max` and the daily summarizer remains a
  bounded Terra/low or Claude Sonnet/low exception, depending on runtime.
- The plugin and both Claude/Codex marketplace surfaces now report v2.0.8.

### Fixed

- Read-only Codex reviewers no longer inherit the executor's full-MCP profile.
  They use an explicit reviewer profile, an installed generated readonly
  profile, or no profile, preventing tool-schema overflow from blocking startup.
- Canonical-vault coordinator reviews accept only their exact generated,
  owner-only, empty, gitignored scratch hierarchy while every other
  in-worktree reviewer runtime remains rejected.
- Reviewer task metadata now takes precedence over repository defaults, and
  supervisor validation includes the resolved model and effort.
- The DCG smoke suite clears an inherited task `DCG_CONFIG` before base-profile
  cases, so base/task policy differences are exercised against the intended
  files.

### Security

- Reviewer profile isolation fails narrow instead of falling back to executor
  capabilities. The DCG base profile explicitly blocks rebase and destructive
  history/lifecycle operations while allowing amend; isolated task worktrees
  retain their existing rebase and amend allowances.

## [2.0.7] - 2026-07-14

### Added

- Cross-model review cycles now keep a stable, idempotent history page under `wiki/meta/reviews/`: the original task request, every validated round, executor resolution, verification gap, residual risk, reviewer/model/mode, and final verdict are retained and linked from the reaped task result. Unattended finalization hashes the marker and blocks close if the approved archive is missing, changed, or unlinked.
- Coordinator reviews use the same durable archive contract, while task worktrees defer archive writes to the canonical coordinator vault.

### Changed

- Unattended task splits use a practical workspace-write profile constrained to the task worktree, exact cmux callback socket, and validated supervisor command; the coordinator owns any bounded mechanism repair.
- Review callbacks use an atomic relay file instead of pasting large encoded payloads into the terminal composer.
- Monthly agenda reports identify themselves as unfinished plans and reminders, improving both human navigation and sparse retrieval.
- Bookkeeping mutations to the writer-owned `log.md` and `hot.md` no longer append every runtime session to their frontmatter; durable content pages, plans, and review archives retain explicit session provenance.

### Fixed

- Dense retrieval now catches up after sparse self-heal on an already-clean Git tree, respects retry backoff for the same corpus fingerprint, and immediately retries when a newer fingerprint supersedes an old marker.
- Failed escalation delivery is recoverable, unattended executors may commit inside their isolated worktree, and exact-socket callbacks work without broadening filesystem access.
- Review archives remain bound to the coordinator vault, reap result-name collisions reroute deterministically, and coordinator reviews archive automatically after approval.

### Security

- Review archives are coordinator-owned `vault-write.py` transactions. Task worktrees can only request archival; only the bounded human task-description section is retained, while raw orchestration/reviewer prompts, compressed callback payloads, command logs, sockets, and cmux identifiers stay outside the durable page.
- Read-only Codex reviewers allow loopback client/server tests while external networking and web search remain disabled.
- Auto-repair remains limited to local, reproducible, reversible repository mechanisms inside approved scope; permission, dependency, public-interface, migration, destructive, and external effects still require user authority.

## [2.0.6] - 2026-07-13

### Added

- Added content-free unattended lifecycle telemetry for task and reviewer process latency, review callback validity and findings counts, escalations, watchdog stages, validated reap completion, and exact-surface outcomes.
- Added a `pipeline-stats.py` dogfood section with p50/p95 durations, completion and intervention counters, privacy boundaries, and explicit small-sample guidance.
- Added macOS GitHub Actions CI for the full hermetic suite and generated Codex marketplace drift checks.

### Fixed

- Preserved the close guard for tracked `.vault-meta/` state while keeping gitignored lifecycle events outside Git status.
- Preserved the `v2.0.5` agenda spacing repair in the consolidated release.

### Security

- Lifecycle events accept only safe identifiers and non-negative numeric counters; task text, review prose, decisions, commands, queries, errors, and page bodies remain outside telemetry.

## [2.0.4] - 2026-07-13

### Added

- Added the runtime-neutral `/agenda` workflow: read-only preview of unfinished plans and reminders, atomic carry-over into one target occurrence, and declarative monthly Obsidian Tasks reports.
- Added an optional pinned Obsidian Tasks 8.2.2 UI layer with checksum-verified assets, preserved user settings, explicit backup-and-repair mode, and a small status snippet.

### Changed

- Journal plans and reminders now use Tasks-compatible checkboxes, stable block IDs, canonical completion dates, and exact-text deduplication while retaining legacy plain reminders as readable input.

### Fixed

- Agenda collection skips ambiguous legacy chains and nested subtrees, guards terminal, duplicate, or conflicting target identities, tolerates missing source sections, and restores required target headings in canonical template order.
- Partial Tasks installations restore only missing verified assets; clean reruns and carry-over reruns remain idempotent.

### Security

- Source pages, the target day, and all affected monthly reports are committed through one optimistic `vault-write.py` transaction; plugin downloads are version-pinned and SHA-256 verified before installation.

## [2.0.3] - 2026-07-13

### Added

- Added local document normalization: Markdown/text use a stdlib fast path, while PDF, Office, EPUB, and scans use a pinned isolated Docling runtime with explicit `ru,en` OCR, content-addressed caching, confidence signals, and fail-closed size/page/time limits.
- Added a cross-runtime failure-to-repair contract: repository-owned mechanism defects are contained and diagnosed read-only, then require explicit user consent before a narrow fix, regression test, failed-stage retry, and resume of the original task.

### Changed

- Claude reviewers in locked-down `dontAsk` can run clean cwd-relative `python3 tests/test_*.py` and `bash tests/test_*.sh` entrypoints, while composed pipe/redirect/wrapper forms remain outside the allowlist.
- Fresh-machine setup provisions the isolated Docling runtime and OCR/layout/table artifacts by default; `--skip-docling` keeps an explicit lightweight path.

### Security

- Docling conversion disables remote services and external plugins, uses offline model flags, preserves immutable source files, and returns typed user-action escalation instead of silently falling back to native model parsing.
- Reviewer permission documentation now states the wildcard boundary accurately: the allowlist is not an argv parser, and executing newly added or modified repository tests is an explicit unattended-review trade-off.

## [2.0.2] - 2026-07-12

### Fixed

- Restored the macOS bootstrap, pinned-Python, protected-research callback, and restart fixes described in 2.0.1 but accidentally omitted from that release tag.
- Completed fetch and synthesis splits now close automatically only after their durable completion marker (and, for synthesis, a valid final output) proves the work finished. `--keep-surfaces` remains an explicit debugging opt-in.
- Claude reviewers now receive cwd-relative read-only Git commands that match their locked-down allowlist; Codex reviewers retain explicit worktree-qualified commands from their isolated scratch directory.

### Security

- Protected research profiles now make the Codex deny-by-default network contract explicit: no external-domain allowlist, no upstream-proxy chaining, no broad local binding or non-loopback listeners, no arbitrary Unix sockets, and no SOCKS5/UDP. The exact cmux callback socket remains the sole exception.
- Surface cleanup is marker-gated, exact-UUID, idempotent, coordinator-safe, and retryable when cmux is temporarily unavailable.

## [2.0.1] - 2026-07-12

### Fixed

- The macOS clean-machine bootstrap now verifies Xcode Command Line Tools before mutating the vault, rejects the inert system Python placeholder, and consistently uses a runnable Python 3.9+ interpreter.
- Protected research sessions pin that exact interpreter and expose only the read-only Homebrew and Command Line Tools roots required by Python and its framework libraries inside the sandbox.
- Protected fetch/synthesis callbacks now receive one explicit cmux Unix-socket exception, write durable completion markers, and can restart networkless synthesis from the already validated artifact when callback delivery fails.

### Security

- Research command networking stays in limited mode with no external-domain allowlist; the new access is restricted to the exact cmux socket, its readable parent, and read-only local toolchain roots.
- Callback failure is recoverable rather than silently losing task progress, without granting the isolated reviewer/fetcher general vault or network access.

## [2.0.0] - 2026-07-11

### Added

- First-class Claude Code and Codex plugin packaging with generated Codex marketplace metadata, shared safe Stop processing, runtime capability documentation, and portable setup helpers.
- Contract-bound unattended orchestration for cmux task worktrees: executor supervision, observer-only stall watchdogs, typed escalation, cross-model review, bounded verification, reap gating, and surface auto-close after a verified handoff.
- Evidence-grounded daily summaries, journal/backlog workflows, research isolation, instruction linting, schema validation, operation telemetry, and crash-safe transactional page writes.
- Section-level sparse retrieval with optional local `bge-m3`, quality gates, dense refresh workers, experiment tooling, and expanded hermetic regression coverage.

### Changed

- Cross-model review defaults now use subscription-backed Claude `opus` (currently Opus 4.8) for Codex work and Codex `gpt-5.6-sol` for Claude work; Fable remains an explicit opt-in.
- Hook execution, MCP profile generation, memory backup, sanitization, and clean-machine bootstrap are hardened for repeatable multi-agent use without committing machine-local state.

### Security

- Reviewer commands, task metadata, callback payloads, lifecycle transitions, and external-effect escalation are validated against strict schemas and pinned permission boundaries.
- Personal wiki pages, session records, workspace state, credentials, runtime metadata, and private memory are intentionally excluded; committed template indexes were regenerated solely from the public seed vault.

## [1.0.0] - 2026-07-05

Initial public release.

### Retrieval

- Local dense retrieval on ollama `bge-m3` (`scripts/semantic-search.py`, `scripts/tiling-check.py`): RU-capable embeddings, zero cloud calls. On the calibration vault the dense channel scored hit@1 0.85 / MRR@10 0.904 vs 0.27 / 0.405 for the previous English-centric model.
- Scope-aware hybrid fusion (`--hybrid`): dense ranks the pages it embeds; BM25 (`scripts/bm25-index.py`, whole-page Okapi with a Unicode tokenizer and RU stopwords) injects only pages outside the dense tiling scope (meta/plans/folds). Design validated on a goldset with a held-out half after plain weighted RRF measurably destroyed BM25's coverage role.
- Tag prefilter (`scripts/tag-search.py`) over the reverse tag index.
- Permanent benchmark harness: `scripts/retrieval-bench.py` + `.vault-meta/retrieval-goldset.jsonl` (seed template included) reporting hit@1 / hit@5 / MRR@10 per channel with automatic degradation handling. House rule: no ranking change ships without moving these numbers; add new goldset queries only after tuning (held-out discipline).
- Automatic degradations: ollama down → BM25-only; BM25 index missing → dense-only.

### Write path & hooks

- `scripts/vault-write.py`: single-payload dispatcher for `wiki/log.md` + `wiki/hot.md` with deterministic caps (hot ≤800 words, Recent Changes ≤15 × 160 chars, Active Threads ≤8, narrative ≤120 words) and `plan_close` lifecycle support. `scripts/validate-vault.py` enforces the caps, frontmatter schema and plans lifecycle.
- `stop.sh` turn-end hook: reindex → sanitized memory backup → BM25 rebuild → incremental dense refresh → auto-commit, serialized under `flock` (parallel sessions cannot corrupt each other), atomic index writes, per-phase latency telemetry in `.vault-meta/stop-hook-latency.jsonl` with a `STOP_HOOK_SLOW` warning at ≥30s.
- `skill-router` (UserPromptSubmit): data-driven soft skill hints from `.claude/skill-rules.json` (12 rules shipped). `session-nudge` (SessionStart): maintenance hints — lint age, fold due, tiling age, stale memory backup, skill-of-the-day, retrieval-assist discipline (`pipeline-stats.py --nudge`).
- `command-capture` (PostToolUse[Bash]): sanitized command log (`scripts/lib_sanitize.py` masks credential-looking values) feeding `/distill-runbook`. `plan-capture` (PostToolUse[ExitPlanMode]): every approved plan auto-filed to `wiki/plans/`.

### MCP HTTP gateway

- `scripts/mcp-gateway/`: one launchd-managed [TBXark/mcp-proxy](https://github.com/TBXark/mcp-proxy) service per machine fronting all MCP children; sessions connect over HTTP (`.mcp.json.example`). Secrets via env indirection (`~/.config/mcp-gateway/secrets.env`); `doctor` derives required keys, child binaries and AWS profiles from `config.json`; `smoke`/`health` do real MCP handshakes; `update`/`sync-tools` manage version pins (`tools.json`).
- Flagship example: context7 (hosted) — setup is a single `CONTEXT7_API_KEY=` line.
- `.mcp-profiles/` pattern for heavy servers (schema-budget escape hatch), documented gotchas in `docs/mcp-gateway.md`.

### Skills (23)

- Wiki core: `wiki`, `wiki-ingest`, `wiki-query` (quick/standard/deep), `wiki-lint`, `wiki-fold`, `save`, `close`, `autoresearch`, `canvas`, `defuddle`, `obsidian-markdown`, `obsidian-bases`.
- Productivity: `journal` (date-keyed planner with carry-over), `daily` (end-of-day status log), `backlog` (append-only capture inbox), `find-session`, `draft` (external-communication advisor with redaction pass), `distill-runbook`, `learn`, `save-plan`.
- Orchestration (optional, requires cmux): `dispatch` (worktree + split with approved-plan handoff, configurable `LLM_OBSIDIAN_PROJECTS_ROOT` / `LLM_OBSIDIAN_WORKTREES`), `reap` (interim/final filing with plan close), `reap-send`.

### DragonScale Memory (inherited from upstream, recalibrated)

- Fold operator, deterministic `c-NNNNNN` addresses, semantic tiling duplicate lint, boundary-first autoresearch. Tiling thresholds ship as bge-m3 defaults (error 0.92 / review 0.85, `calibrated: false`) with a documented per-vault recalibration procedure.

### Vault template

- Seeded `wiki/` skeleton: demo concept/entity/source pages, getting-started, full folder set (concepts, entities, sources, comparisons, questions, runbooks, decisions, goals, routines, daily, plans, folds, meta) with auto-generated `_index.md`, fresh `hot.md`/`log.md` matching the vault-write contract.
- `CLAUDE.md` template (RU) + `AGENTS.md` agent-agnostic contract.

### Testing

- 9 hermetic suites, no network or ollama required: address allocator, tiling, boundary, vault scripts, stop-hook (flock/opt-out/latency), BM25 + fusion, bench harness, skill router, MCP gateway management layer.

### Known limitations / roadmap

- Claude Code is the only wired agent (hooks layer); Codex adapter planned — scripts are agent-agnostic.
- Skill bodies are English (RU triggers work); RU localization planned.
- launchd autostart is macOS-only; on Linux run the gateway under systemd manually.
