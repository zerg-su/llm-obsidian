# LLM Obsidian 2.6 engineering-skill semantic gap audit

## Purpose

The 2.6 skill audit originally proved structural predictability and preservation
of the semantics already present in each local skill. It did not prove that the
local inventory retained the important transferable engineering capabilities in
the pinned Matt Pocock Skills and Superpowers snapshots. This audit supplies
that missing denominator.

The release outcome is not merely a green harness. A coding task should move
from an agreed outcome to a maintainable design, an implementation plan with
explicit ownership, honest red-green tests, focused implementation, independent
standards/spec review, and evidence-backed completion. Harness code owns the
deterministic lifecycle; skills own engineering judgment.

## Reference boundary

Adopt or adapt technology-agnostic practices only. Reject issue trackers,
GitHub/GitLab workflow, installers, foreign worktree mechanics, HTML reporting,
and upstream subagent orchestration. The pinned sources are:

- Matt: `codebase-design`, `improve-codebase-architecture`, `tdd`,
  `diagnosing-bugs`, `implement`, `code-review`, `to-spec`, `domain-modeling`,
  and `writing-great-skills`.
- Superpowers: `brainstorming`, `writing-plans`, `test-driven-development`,
  `writing-good-tests`, `systematic-debugging`, `requesting-code-review`,
  `receiving-code-review`, `verification-before-completion`, and
  `writing-skills`.

## Capability coverage matrix

| Capability | Upstream evidence | Local carrier at `9ff18a9` | Transfer / verdict |
|---|---|---|---|
| Clarify purpose, constraints, evidence, and non-goals before code | Superpowers `brainstorming`; Matt `to-spec` | `clarify` plus Outcome Contract | **adapt** — stronger outcome identity and minimal user loop |
| Present alternatives and obtain approval before material architecture changes | Superpowers `brainstorming`; Matt `codebase-design` | `design` | **adapt / partial** — module-shape vocabulary is missing |
| YAGNI/scope minimality: remove unrequired features and speculative extension points | Superpowers `brainstorming`, `writing-plans`, and `receiving-code-review` | minimal production change exists in `tdd`, but no design/plan carrier rejects unnecessary scope | **adapt / missing / important** |
| Domain modeling: shared language, identity/lifecycle, values, invariants, services, and domain errors/events only where the domain needs them | Matt `domain-modeling` | `design` names domain boundaries and invariants but supplies no modeling decision vocabulary | **adapt / partial / important** |
| Deep modules: small interface, hidden complexity, locality, leverage, explicit seam and adapter | Matt `codebase-design` and `DEEPENING.md` | no independently invokable local skill; `design` only says ownership/test seams | **adapt / missing / important** |
| Detect and safely deepen shallow, tangled, or oversized existing code | Matt `improve-codebase-architecture`; Superpowers design isolation guidance | no local architecture-improvement mode or measurable signal | **adapt / missing / important** |
| Map files to one clear responsibility before task decomposition | Superpowers `writing-plans` | no implementation-plan skill; `save-plan` only persists already-discussed prose | **adapt / missing / important** |
| Declare task interfaces (`consumes`/`produces`) and vertical independently reviewable slices | Superpowers `writing-plans`; Matt `implement` | partial in `design`; not required in executable plans | **adapt / missing / important** |
| Red before green, minimal implementation, refactor only while green | both upstream TDD skills | `tdd`; restored and guarded by `9ff18a9` | **adapted**, subject to the compression findings below |
| Stateful work inventories states/transitions and exhausts a cheap matrix | local 2.6 extension consistent with upstream feedback-loop discipline | B-TDD-01 and the workstream test claim an exhaustive matrix, but the skill never stated `exhaust` | **adapt / missing / important** |
| Exempt artifacts receive proportional checks without weakening gates | Superpowers TDD exemptions and verification discipline | `tdd` lists exemptions but omits `never weaken gates` | **adapt / weakened / important** |
| Tests assert behavior through agreed interfaces and survive refactors | Matt `tdd`/`codebase-design`; Superpowers `writing-good-tests` | `tdd` says observable seam/source-text is not evidence | **partial** — no interface-selection or refactor-survival contract |
| Expected values are independent; tests reject tautologies/change detectors | Matt TDD; Superpowers `writing-good-tests` | absent | **missing / important** |
| Mocks isolate slow/external adapters, preserve real side effects, and earn no assertions | Matt `DEEPENING.md`/TDD; Superpowers `writing-good-tests` | only `Mock provider/transport; keep state transitions real` | **partial / important** |
| Mutation sensitivity: every realistic wrong branch/side effect is caught | Superpowers `writing-good-tests` | absent | **missing / important** |
| Tight red-capable debug loop, minimization, root-cause proof, regression, cleanup, architecture stop | Matt `diagnosing-bugs`; Superpowers `systematic-debugging` | `debug` | **mostly adapted** |
| Rank falsifiable hypotheses before probes and preserve one-variable experiments | Matt `diagnosing-bugs`; Superpowers `systematic-debugging` | `debug` says `Falsify` but does not require a ranked hypothesis set or one-variable probe | **weakened / important** |
| Independent spec/outcome and standards review axes | Matt `code-review`; Superpowers task review | deep `review` lanes | **adapted** |
| Standards review has a fixed maintainability-smell baseline when repo standards are absent | Matt `code-review`; Superpowers task-reviewer structure checks | standards lane name only; no authoritative local baseline is loaded | **missing / important** |
| Review verifies feedback against code reality and supports reasoned rejection | Superpowers `receiving-code-review` | typed `applied`/`rejected`/`out-of-scope` resolution | **adapted**, but skill should point to technical-evidence criteria |
| Evidence before completion claims | Superpowers `verification-before-completion` | Outcome Contract, verification profiles, review and reap | **adapted and stronger** |
| Skill edits use behavioral RED/GREEN/REFACTOR pressure scenarios | Superpowers `writing-skills`; system skill creator | mostly source-text/structural tests plus two paired workflows | **partial / important** |
| Skill audit detects missing upstream capability, not only drift in installed semantics | Matt `writing-great-skills`; release integration goal | `improve-skills` explicitly defers every semantic expansion | **adapt / missing / critical to this integration** |
| Mechanical quality signals are automated; judgment remains in skills | Superpowers `writing-skills`; Matt deep-module model | coverage ratchet exists, but no file/function responsibility or complexity ratchet | **adapt / missing / important** |

## Why the previous audit gave a false complete result

`improve-skills` enumerated installed skills and applied invocation, hierarchy,
steering, pruning, and goal-preservation passes. Its preservation gate required
every semantic expansion to be marked `defer`. The audit therefore had no row
for a capability that was absent from the installed inventory. A locally tidy
`design` skill could pass even though `codebase-design` was missing entirely.

The deterministic workstream tests also over-relied on source-text presence.
At `9ff18a9`, the test labelled “exhausts a cheap release matrix” did not match
the word or behavior `exhaust`; the TDD exemption check did not protect the
`never weaken gates` clause. A green structural test was treated as semantic
evidence it did not establish.

## Required 2.6 remedies

### 1. Add `codebase-design`

Create one model-invoked skill for designing or improving module shape. Keep
Matt's essential vocabulary: module, interface, depth, seam, adapter, leverage,
and locality. Require:

- one coherent responsibility/change reason per file or internal module;
- a small caller/test interface that hides implementation decisions;
- the interface as the durable behavior-test surface;
- dependencies accepted at real seams, with adapters only where variation is
  real (production plus test counts);
- the deletion test for pass-through abstractions;
- focused deepening when touched code has mixed responsibilities, poor
  locality, or cannot be tested without reaching through internals.

Approximately 200 physical lines is a review signal, not a universal law. A
file beyond that point must remain demonstrably cohesive. New or growing files
above a higher code-owned threshold require an explicit extraction decision;
multi-thousand-line functions are release-blocking architecture debt.

### 2. Add `implementation-plan`

Separate engineering planning from `save-plan` persistence. Before code, map:

- files/modules and their single responsibility;
- interfaces and dependency direction;
- each task's `consumes` and `produces` contracts;
- vertical red-green slices and focused verification;
- spec/outcome evidence coverage and non-goals;
- rollout/recovery where relevant.

Self-review must detect uncovered requirements, placeholders, contradictory
interfaces, and tasks that concentrate unrelated responsibilities in one file.

### 3. Restore TDD test quality through progressive disclosure

Keep the normal-path TDD loop concise, but load one authoritative test-quality
reference whenever tests or mocks are written. It must cover independent
expectations, behavior through interfaces, mutation sensitivity, complete fake
data, preservation of real side effects, and mock-only assertions. Restore
`exhaust` and `never weaken gates` and protect their semantics with mutation-
sensitive tests rather than optimistic labels.

### 4. Give review an enforceable standards baseline

The standards lane must load one local, technology-agnostic quality contract:
module responsibility, locality, dependency direction, error handling,
duplication, divergent change, shotgun surgery, speculative generality,
middle-man/pass-through abstractions, and test quality. Repo-specific standards
override heuristic smells, but absence of a repo standard does not mean absence
of architectural judgment.

### 5. Add capability-gap mode to `improve-skills`

Preservation mode remains the safe default. An explicitly authorized
integration audit additionally requires a reference-capability matrix with one
row per relevant upstream capability:

- `adopted` with local carrier/evidence;
- `equivalent` with evidence;
- `missing` with a bounded proposed carrier;
- `rejected` with technology/scope rationale;
- `deferred` with owner and evidence gap.

No inventory can be declared complete while a relevant reference row is
unclassified. Pressure-scenario evidence is required for changed discipline
skills; structural grep alone cannot establish agent behavior.

### 6. Automate measurable signals

Add a standard-library quality audit for Python files/functions. Use size and
AST complexity as review triggers and ratchets, never as proof of good design.
It should fail on new multi-responsibility growth, report exact hotspots, and
allow only explicit evidence-backed exceptions. It must not reward splitting a
deep module into shallow pass-through files.

The coverage gate should report two honest denominators:

- 100% of the declared deterministic policy/state/validation core and all
  supported state/decision combinations;
- whole-harness statement coverage including adapters and entrypoints, with
  explicit uncovered lines and no exclusions added solely to improve the
  number.

Provider/OS wiring remains a small integration/live suite. It is not multiplied
to compensate for missing unit seams.

### 7. Refactor the current harness before release

The 5,541-line `runtime_worker.py` contains a roughly 4,460-line `run()` with
callback, fix transport, custom pipeline, research, summary, and liveness
responsibilities. Extract pure policy/validation/receipt/transition logic first
under characterization tests, then thin provider/process/callback adapters.
Apply the same cohesion audit to
`scripts/harness/workflows/review_gate.py`,
`scripts/harness/runtime_sessions.py`,
`scripts/harness/workflows/review.py`,
`scripts/harness/custom_pipelines.py`,
`scripts/harness/workflows/research.py`, and
`scripts/harness/workflows/engineering_fix.py`.

Stop conditions:

- do not change user-visible workflow or durable identity merely to split code;
- do not create pass-through modules or expose internal seams to tests;
- after three failed behavior-preserving extraction attempts at one seam, stop
  and revise the architecture;
- do not claim success from line count alone; public contracts, deterministic
  matrices, coverage, and final integration evidence must remain green.

## Behavioral evidence required before release

1. Pressure case: design/plan a feature against an already-large orchestrator;
   the agent must propose a deep extraction seam and explicit file ownership,
   not append another branch to the monolith.
2. Pressure case: implement under time pressure with a tempting tautological or
   mock-only test; the agent must preserve red-green, independent expectation,
   real state logic, and refactor-only-while-green.
3. Review case: a spec-correct diff contains a giant mixed-responsibility
   function and weak tests; spec may pass while standards returns material
   findings.
4. Negative cases: small cohesive files must not be split mechanically, and a
   deep module with a compact interface must not be penalized merely for having
   substantial private implementation across focused internal files.
5. Run deterministic skill/router/quality/coverage suites first; use only a
   small bounded live sample to confirm model behavior and cross-runtime wiring.

## Audit disposition

The earlier conclusions “Every retained edit maps to one frozen finding” and
“no further evidenced skill edit is required at this boundary” in
`docs/skill-quality-post-audit-v2.6.0.md` are superseded. They remain true only
for the old behavior-preserving audit boundary. Relative to the release's
clarified engineering-quality outcome, the important and critical gaps above
are in scope for 2.6 and must be resolved before final deep review.

## Closure evidence

- Five bounded model pressure scenarios are registered in
  `evals/cases/engineering-quality.jsonl` and exercised by the read-only
  code-owned `scripts/engineering-eval-runner.py`.
- `docs/acceptance/v2.6-engineering-skill-pressure.md` binds exact skill,
  reference, runner, and corpus hashes and records the honest 4/5 aggregate
  plus corrected TDD-orientation rerun, yielding five established cases.
- `config/code-quality-baseline.json` and `make test-code-quality` reject every
  new, stale, or growing hard blocker. `make acceptance-check` remains strict
  and intentionally fails until all thirteen owned release blockers are
  decomposed; the ratchet is not a waiver.
- The shared engineering quality contract now treats an unnameable
  responsibility as a design signal, and debug records a missing correct
  regression seam as architecture evidence instead of accepting a shallow
  proxy.
