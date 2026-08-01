# LLM Obsidian 2.6.0 skill quality baseline audit

Date: 2026-08-01

Status: pre-branch audit complete; skill and product behavior unchanged

## Scope and identity

This is the mandatory audit-only `improve-skills` meta-gate from section 3 of
the approved LLM Obsidian 2.6.0 plan. It covers exactly:

`clarify`, `design`, `prototype`, `save-plan`, `debug`, `tdd`, `review`,
`reap`, and `improve-skills`.

The audit baseline is the exact integrated technical-foundation commit
`21bdca927739c34a7ebb109f1e0393fca6230340`, at which `HEAD` and
`release/2.6.0` were identical before this report was added. The installed
inventory contained 32 skills; all nine in-scope skills were present exactly
once. The initial structural audit reported 32 audited, 0 errors, and 0
warnings.

This gate records evidence and future work only. It changes no `SKILL.md`,
script, schema, test, adapter, harness behavior, or vault page. A `fix` verdict
below means that the named workstream has a confirmed input; it does not mean
the fix was applied by this audit task.

## Method and evidence boundary

Each skill was checked through five passes:

1. invocation;
2. information hierarchy;
3. steering and completion criteria;
4. pruning and single-source relevance;
5. goal preservation.

The fifth pass names the approved overall outcome, the skill's permitted local
subgoal, any local completion proxy that could be mistaken for the outcome,
and the evidence needed before an outcome-level completion claim.

Repository contracts and the approved plan are authoritative. Third-party
material was used only as reference evidence for the audit method:

- Superpowers `writing-skills`, pinned at
  `44c9b2d6e889982ac18c27d05a19fefe335194e1`, supports observed-failure
  evidence, checkable output shapes, and matching the instruction form to the
  failure rather than importing a workflow.
- Matt Pocock `writing-great-skills`, pinned at
  `2ab958093e83e0ec752e6c1c5932da465bf23e0c`, supplies the invocation,
  hierarchy, steering, pruning, completion-criterion, and single-source
  vocabulary adapted by the local quality model.

The retained snapshot bytes verified against
`references/upstream-skills/manifest.json`: Matt 141 files / 397020 bytes /
tree SHA-256
`ee4511e5d2659c3ba9a4348828f65591f9be91c0de694f7790fc33827e94aa2d`;
Superpowers 95 files / 1174353 bytes / tree SHA-256
`1db2d4218fbbcaa660a56617ff1e6940c4638c1c6b6b721a57de379f5b5d54d5`.
No snapshot code was executed, no live upstream claim was made, and no
third-party orchestration, installer, issue-tracker, worktree, or lifecycle
mechanic is proposed.

## Protected behavior baseline

These behaviors are frozen for the future quality workstreams unless the
approved Outcome Contract amendment explicitly requires the recorded change.

| Skill | Protected behavior |
|---|---|
| `clarify` | Model-invoked clarify/grill-me route; repository facts are inspected read-only before questions; exactly one material question is asked at a time; material tradeoffs remain with the user; no code, plan, or implementation mutation occurs during interview; aligned facts hand off without redundant confirmation. |
| `design` | Read-only architecture shaping; facts, assumptions, and decisions remain distinct; alternatives, ownership, flows, recovery, rollout/rollback, acceptance, and ADR candidates remain visible; implementation, interface, migration, dependency, security, and external-effect boundaries require approval. |
| `prototype` | One falsifiable technical question; harness-owned disposable worktree and minimal packet; one bounded run command; production remains unchanged; exact owned cleanup only after durable capture; unknown ownership becomes `attention-required`; production promotion is separately authorized. |
| `save-plan` | Persist-only invocation, distinct from plan execution and general conversation save; `Read Bash Glob` tool boundary; real session/address metadata; a single `vault-write.py` page transaction; no direct page edit and no log/hot mutation; new plans remain pending. |
| `debug` | Reproduce, minimize, falsify hypotheses, and establish root cause before a fix; diagnosis-only stops without mutation; a fix requires authorization, the narrowest approved seam, regression evidence, and reruns of minimized and original loops; sensitive boundary changes escalate. |
| `tdd` | Approved observable behavior and authorization precede edits; each vertical slice proves red, applies the minimum production change, proves focused and affected green, refactors only while green, and remains runnable; test doubles stop at external adapters; bounded non-executable exemptions remain. |
| `review` | Harness owns routing, identity, provider sessions, budgets, callbacks, and archive evidence; reviewers are product-read-only; simple review is holistic and deep axes remain independent; only executors resolve findings; loops and restarts stay bounded; finalization binds exact HEAD/profile; lifecycle and external-effect boundaries remain fail-closed. |
| `reap` | Coordinator-owned, exact-session runner executes once; summary, plan, review, route, links, and clean product state validate before mutation; one optimistic `vault-write.py` transaction files the result; shared mode leaves the master plan pending; exact surface exit follows completion; compatibility remains conditional and no push/publish/deletion occurs. |
| `improve-skills` | Manual/explicit-only invocation and cross-runtime metadata parity; exact inventory before changes; quality-only edits preserve triggers, permissions, tools, schemas, writer paths, runtime routing, and lifecycle; one verdict per in-scope skill; only evidenced minimum edits; deterministic checks gate completion. |

Baseline file identities:

| Skill | `SKILL.md` SHA-256 |
|---|---|
| `clarify` | `dd12faabaa93d03a0f3be77862aa6c9f2c2cd589b436ef4cec1376bfc51ebc9f` |
| `design` | `543c3dbd637d983304b20ee2b772e3145f318d98557f274ce4c98ae751c66426` |
| `prototype` | `88019939e52707104504d5b649e27cb6f651095bd47022482e71bd404f48a588` |
| `save-plan` | `c9542d484b52e1b87bf04943394523df615b790558d74af581438b5612972983` |
| `debug` | `d4cb29ba3a32fa46e25d6e604c52663ec29e75e7045ca2721ebd0a97941489bd` |
| `tdd` | `d94244eb2138ac321d20269999e0566ff59b000674650ce76224861b34ccc293` |
| `review` | `162083e9a3c86e40c84097dab29be46ea501c41eaff60a8be4c7e8753a714f69` |
| `reap` | `93f63f65d3b89d947ca88b35034b6951f27c78b44dc1d30f683572af1f196e63` |
| `improve-skills` | `9ee023a8874170d83d1cc510870615102837e0b88d14781f627f42f689354b75` |

## Behavioral and goal-preservation expectations

The approved overall outcome is: every new v4 task preserves the user's
approved outcome through independently verified result and typed reap
disposition, without adding a second orchestration or stop authority. The plan
page remains the source of truth; no skill may rewrite the contract.

| Skill | Permitted local subgoal | Completion proxy to resist | Required outcome evidence |
|---|---|---|---|
| `clarify` | Resolve material ambiguity and obtain user alignment. | “Shared understanding” or a summary exists. | `desired_outcome`, declared `success_evidence`, and `non_goals` are unambiguous and user-grounded; unresolved material ambiguity keeps the interview open. |
| `design` | Produce an implementable design with owned seams and explicit tradeoffs. | Acceptance criteria or a recommended architecture exists. | The design preserves the exact Outcome Contract, traces slices/checks to its evidence IDs, and introduces no scope beyond its non-goals. |
| `prototype` | Answer one bounded technical question with disposable evidence. | The run command succeeds or a local technical decision is reached. | Durable question, evidence, decision, limitations, and provenance show how the answer informs the approved outcome without claiming production completion. |
| `save-plan` | Persist the agreed plan safely. | `vault-write.py` succeeds and a plan page exists. | The same transaction contains one canonical Outcome Contract block preserving the user's terms; no second goal artifact is created. |
| `debug` | Establish the causal defect and, when authorized, repair the correct seam. | Reproduction, root-cause claim, regression green, or clean diff. | Repro and fix evidence establish the declared outcome evidence affected by the defect; an evidence gap remains explicit instead of becoming a speculative fix or completion claim. |
| `tdd` | Deliver one approved behavior slice through real red/green evidence. | Focused tests pass or a runnable slice is committed. | Red/green proves observable behavior at the right seam and the relevant declared success evidence is established; local green alone cannot close the task. |
| `review` | Independently judge implementation, plan compliance, and bounded findings. | Callback accepted, review approved, clean diff, or exact-HEAD checks pass. | Each declared success-evidence item is `established`, `missing`, or `contradicted`; non-goals are checked for scope creep; implementer claims remain unverified until reviewer evidence establishes them. |
| `reap` | File the approved, exact result and close only the authorized lifecycle scope. | Summary validates, vault write succeeds, or lifecycle reaches complete. | Wiki Summary v2 records `achieved`, `partially-achieved`, or `not-achieved`, bounded evidence IDs, and residual-gap pointers without changing the desired outcome; shared mode keeps this release plan pending. |
| `improve-skills` | Improve skill predictability while preserving protected behavior. | Structural audit/tests pass, every skill has a verdict, or the report is committed. | A fifth pass names overall input, local subgoal, completion proxies, and outcome evidence for every engineering skill; a verdict cannot treat local mechanics as proof of the approved outcome. |

## Five-pass matrix and one verdict per skill

`pass` means no confirmed issue in that pass. A finding ID means the pass has
the confirmed issue described in the next section. Each row has exactly one
overall verdict.

| Skill | Invocation | Hierarchy | Steering | Pruning | Goal preservation | Verdict | Owner |
|---|---|---|---|---|---|---|---|
| `clarify` | pass | pass | `A-CLA-01` | pass | `A-CLA-01` | `fix` | A |
| `design` | pass | pass | `A-DES-01` | pass | `A-DES-01` | `fix` | A |
| `prototype` | pass | pass | `A-PRO-01` | pass | `A-PRO-01` | `fix` | A |
| `save-plan` | pass | `A-SVP-01` | `A-SVP-01` | `A-SVP-01` | `A-SVP-01` | `fix` | A |
| `debug` | pass | pass | `B-DBG-01` | pass | `B-DBG-01` | `fix` | B |
| `tdd` | pass | pass | `B-TDD-01` | pass | `B-TDD-01` | `fix` | B |
| `review` | pass | pass | `C-REV-01` | `C-REV-01` | `C-REV-01` | `fix` | C |
| `reap` | pass | pass | `C-REA-01` | `C-REA-01` | `C-REA-01` | `fix` | C |
| `improve-skills` | pass | pass | `I-IMP-01` | pass | `I-IMP-01` | `fix` | integration |

Inventory/verdict proof: 9 in-scope skill names, 9 unique rows, 9 `fix`, 0
`no-change`, 0 `defer`, no omissions, and no duplicates. Invocation mode,
metadata, routing coverage, authorization boundaries, and current progressive
disclosure remain protected; none needs a quality-only change in this gate.

## Confirmed finding ledger

### A — clarify, design, prototype, save-plan

#### A-CLA-01 — alignment can close without a canonical outcome

Evidence: `skills/clarify/SKILL.md:9-24` resolves general requirements and ends
on “shared understanding”; `:35-43` summarizes requirements and hands off, but
does not require terms, invariants, contradictions, edge cases, or ADR
candidates to be retained, and does not form or validate the approved Outcome
Contract. The local summary is therefore a completion proxy.

Smallest future correction: retain the read-only, one-question loop; make
material ambiguity in `desired_outcome`, `success_evidence`, or `non_goals`
keep the interview open, and make the alignment output one user-grounded
Outcome Contract without inventing a goal. Brainstorming/domain modeling stays
conditional on real ambiguity.

Behavior proof: the trigger, user decision authority, one-question cadence,
read-only boundary, and handoff semantics remain unchanged.

#### A-DES-01 — design output is not bound to the approved outcome

Evidence: `skills/design/SKILL.md:8-25` covers current constraints,
alternatives, ownership, flows, acceptance, and approval boundaries, but it
does not require test seams, placeholder rejection, vertical slices,
expand-contract for wide migrations, fog/out-of-scope separation, or semantic
preservation of the incoming Outcome Contract. “Testable acceptance criteria”
can pass while the approved outcome drifts.

Smallest future correction: add the approved design output fields and trace
them to the unchanged contract; preserve vertical slices as the default and
expand-contract only for a wide migration.

Behavior proof: design stays read-only and keeps the same ownership,
alternatives, recovery, rollout/rollback, ADR, and approval boundaries.

#### A-PRO-01 — a technical answer can overclaim outcome completion

Evidence: `skills/prototype/SKILL.md:8-18` correctly bounds one question, one
command, disposable ownership, and promotion, but the durable record requires
only decision and limitations. It does not structurally require the question,
evidence, or provenance, nor distinguish a successful spike from progress on
the approved outcome.

Smallest future correction: require the durable result to contain question,
bounded evidence, decision, limitations, and provenance, and explicitly bind
the local answer to the incoming outcome without turning disposable code into
production implementation.

Behavior proof: the one-question scope, production immutability, harness
ownership, cleanup safety, and separate promotion authorization remain
unchanged.

#### A-SVP-01 — plan capture has no canonical outcome slot and cites a stale schema source

Evidence: `skills/save-plan/SKILL.md:88-110` composes the page from verbatim plan
content without a canonical Outcome Contract slot. The successful writer call
at `:113-119` can therefore persist a plan whose local steps have lost the
approved outcome. Separately, `:152-154` says the canonical frontmatter schema
lives in `wiki/plans/_index.md`, while that generated index contains only an
auto-index listing; this is confirmed sediment and a misleading hierarchy
pointer.

Smallest future correction: capture the canonical Outcome Contract in the same
single `vault-write.py` transaction as the plan, reject missing or ambiguous
required fields, create no second goal artifact, and point schema authority to
the actual code-owned schema/validator source.

Behavior proof: invocation, persist-only scope, tools, address/session
metadata, single writer transaction, collision behavior, pending status, and
no-log/no-hot boundaries remain unchanged.

### B — debug and tdd

#### B-DBG-01 — the feedback loop lacks entry/attempt stops and outcome evidence

Evidence: `skills/debug/SKILL.md:8-17` begins with reproduction and ends with
rerun/reporting, but it does not distinguish a defect already observed by one
deterministic command from a case that still needs a red-capable loop. It has
no explicit evidence-gap exit, no definition of a failed fix attempt, no
unconditional three-attempt architecture stop, and no requirement that the
fix establish the relevant outcome evidence. A root-cause or green regression
claim can become a completion proxy.

Smallest future correction: add the approved direct-repro/red-capable entry
gate, explicit evidence-gap result, failed-attempt definition, three-attempt
architecture stop, and outcome-evidence trace.

Behavior proof: diagnosis-only remains non-mutating; root cause still precedes
authorized narrow mutation; regression and both repro loops remain mandatory;
all escalation boundaries remain.

#### B-TDD-01 — local red/green can substitute for desired-outcome evidence

Evidence: `skills/tdd/SKILL.md:8-23` requires red/green vertical slices and
affected checks, but it does not name the production change that should break
the test, reject source-text assertions, prove regression red on a preserved
pre-fix state without destructive reset, define proportional checks for its
exemptions, or bind green to declared success evidence. A passing focused test
or committed slice can therefore be mistaken for outcome completion.

Smallest future correction: add the approved name-the-break, observable seam,
non-destructive regression red/green, proportional verification, and
Outcome-Contract evidence rules.

Behavior proof: implementation authorization, vertical slicing, minimum
production change, integration checks, green refactor, doubles boundary,
bounded exemptions, and existing-gate protection remain unchanged.

### C — review and reap

#### C-REV-01 — review can approve mechanics without judging the approved outcome

Evidence: `skills/review/SKILL.md:12-15` binds approval to exact HEAD/profile,
and `:48-62` correctly protects independent axes and bounded resolution.
However, it does not treat the implementer report as an unverified claim,
classify each declared success-evidence item, or check non-goals for scope
creep. It also calls the dispatched path v3 at `:30` and `:67` although the
foundation's task metadata schema and this task are v4. An accepted callback
or exact-HEAD approval can become the outcome proxy.

Smallest future correction: update the skill together with its harness-owned
contract so the existing simple/Fable lane checks the Outcome Contract first,
classifies each evidence item as established/missing/contradicted, checks
non-goals, and retains typed finding rulings; replace stale v3 normal-path
wording without adding a lane, model call, or surface.

Behavior proof: harness ownership, reviewer read-only state, simple/deep
routing, independent axes, severity freedom, typed resolution, exact identity,
bounded loops, and executor-only mutation remain unchanged.

#### C-REA-01 — successful reap has no typed outcome disposition

Evidence: `skills/reap/SKILL.md:17-30` names v3 as the normal path and
`:66-72` describes only v1/v2 task-metadata compatibility, while foundation
dispatches v4 tasks. The summary and transaction rules at `:32-49` validate
mechanical readiness but do not require Wiki Summary v2 disposition, bounded
outcome evidence IDs, or residual gaps. A validated summary plus successful
vault transaction can therefore overclaim completion.

Smallest future correction: update skill and harness together so new v4 tasks
accept only approved review evidence and write Wiki Summary v2 with
`achieved|partially-achieved|not-achieved`, declared evidence IDs, and residual
gap pointers; retain legacy summary readability and shared-plan behavior.

Behavior proof: exact-session runner ownership, pre-mutation validation,
single optimistic writer transaction, review archive, recovery, shared/final
plan semantics, exact exit, and destructive/external-effect prohibitions remain
unchanged.

### Integration — improve-skills

#### I-IMP-01 — the meta-skill has four passes and no goal-preservation gate

Evidence: `skills/improve-skills/SKILL.md:33-55` explicitly runs only four
passes and defines verdicts without naming overall outcome, permitted local
subgoal, completion proxies, or outcome evidence. Its final proof at `:70-86`
can therefore treat green audit/lint/budget/adapter checks and a complete
verdict set as proof that the audited skill preserves the user's goal.

Smallest future correction: add goal preservation as the fifth required pass
and extend the audit record with overall input/outcome, local subgoal,
completion proxies, and required outcome evidence. Keep the same
`fix|no-change|defer` discipline and do not add an automatic invocation or
model call.

Behavior proof: explicit-only invocation, quality-only boundary, protected
behavior inventory, minimum-edit rule, deterministic checks, and one-verdict
closure remain unchanged.

## Workstream handoff

| Workstream | Confirmed finding IDs | Skill files permitted in that future workstream |
|---|---|---|
| A | `A-CLA-01`, `A-DES-01`, `A-PRO-01`, `A-SVP-01` | `clarify`, `design`, `prototype`, `save-plan` |
| B | `B-DBG-01`, `B-TDD-01` | `debug`, `tdd` |
| C | `C-REV-01`, `C-REA-01` | `review`, `reap` plus their already-approved harness coupling |
| integration | `I-IMP-01` | `improve-skills` and shared goal-preservation audit wiring |

Every confirmed finding has exactly one owner. `dispatch` and schema/harness
transport outside the explicit C coupling remain technical-foundation/product
work, not quality-only edits. Unproven wording polish, foreign orchestration,
new schedulers, extra review lanes/model calls, automatic `improve-skills`
invocation, and a second goal artifact remain out of scope.

## Gate evidence

The report-only worktree passed the required gate:

| Check | Result |
|---|---|
| `python3 skills/improve-skills/scripts/audit_skills.py --strict` | PASS — 32 audited, 0 errors, 0 warnings |
| `make test-instruction-lint` | PASS — all instruction-lint tests |
| `make test-skill-budget` | PASS — repository and negative budget fixtures |
| `make test-codex-adapter` | PASS — 22 passed, 0 failed |
| `python3 scripts/codex-adapter.py --check` | PASS — no changes |
| `python3 scripts/release-acceptance.py check` | PASS — 4 harness cells valid |
| `git diff --check` | PASS |

The 2.6 release plan remains pending because this is a shared-plan pre-branch
audit, not completion of the release.
