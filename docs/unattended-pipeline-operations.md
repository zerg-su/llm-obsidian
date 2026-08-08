# Unattended harness operations

The public chain is `dispatch` → `review` → `reap`. Skills define triggers,
reasoning discipline, authorization boundaries, and completion criteria.
Deterministic lifecycle mechanics live in `scripts/harness/`.

## Operation lifecycle

Each operation has an immutable typed spec, owner-scoped ledger record, exact
surface/process handles, bounded callback and retry state, verification
evidence, and a compact handoff. `scripts/harness-cli.py` exposes:

```text
status
inspect <operation-id>
resume <operation-id>
reconcile
cancel <operation-id>
close <operation-id>
doctor
```

The cmux adapter is the only production perimeter for open/send/read/status/
close. Runtime drivers pin model, effort, permission profile, cwd, and callback
transport. Unknown prompts, ownership, callbacks, or resource state become
`attention-required`; uncertain effects reconcile before any retry.

The workspace progress bar is derived from this ledger but has no lifecycle
authority. One bounded exact cmux tree snapshot scopes current top-level
controllers to the coordinator origin workspace and verifies owned surfaces.
A controller with a recorded exact surface that is known missing is not current
workspace work, including stale launch attention. A terminal controller closes
its whole program even if a derived child is stale. With no proven current program the publisher clears progress; when the
tree probe is unknown it leaves the existing UI untouched. Coordinator
SessionStart refreshes this projection silently, while task-worktree hooks do
not receive coordinator status authority. The label contains counts only:
`completed/total · active▶`, plus waiting `⌛` and attention `!` when nonzero.

## Review and reap

Simple review uses one holistic session. Deep review keeps independent spec and
correctness/architecture/security axes. Reviewer sessions are product
read-only; the executor resolves findings. Verification reuses the exact
reviewer lane and surface within the configured round bound.

The task writes canonical `.task-summary.json`. The provider worker validates and delivers
that value through the internal callback broker. The coordinator owns
the one `reap-runner.py` vault transaction, review archival, plan close,
validation, task archival, armed exit, and exact terminal cleanup.

## Recovery and upgrade

The operation ledger and write-ahead effect marker are authoritative after an
interruption. Read-only probes may retry within their small budget; model start,
send, callback, or close does not repeat until reconciliation proves the prior
effect.

`scripts/upgrade-preflight.py` rejects every nonterminal harness kind, every
terminal harness record that retains a pending effect or exact owned resource,
and unmatched live or uncertain legacy task/review/research state before
mutation. A canonical same-ID terminal dispatch with settled effects and no
provider-process or cmux ownership proves only its matching v3 worktree
and active lane-free broker mirrors stale; those mirrors do not block.

Finish or cancel live operations with the installed runtime. Inspect and
reconcile exact ownership for a terminal record that still retains an effect or
resource, then rerun the preflight. Per-class actionable doctor guidance remains
available from the same read-only preflight seam:

```text
python3 scripts/upgrade-preflight.py --diagnose-identities
```

The JSON result classifies exact operation/worktree pairs as `active`,
`proven-stale`, `ambiguous`, or `mismatched`. Only a self-owned terminal
dispatch whose path identity matches and whose effects and owned resources are
settled can prove its same-ID, same-path v3 worktree mirror stale. Active rows
point to exact harness inspection before normal finish/cancel. Ambiguous rows
require exact ownership reconciliation. Mismatched rows show both recorded and
path-bound identities and deliberately choose neither owner. Proven-stale
worktree rows provide separate inspect and Git removal command arrays; the
diagnostic never runs them, edits state, deletes a worktree, or removes an
operation record. Active rows also carry an exact `cancel_command`; worktree
rows mirror their one matched operation under `identity.operation_identity`.
It exits `4` when any active, ambiguous, or mismatched row requires attention
and `0` for healthy or proven-stale-only results.

Completed worktrees already carrying the reap completion marker retain their
existing finalization treatment. Live or uncertain 2.2.x state is never silently
adopted.

### The one same-HEAD review retry

A terminated review attempt at an unchanged HEAD normally replays its stored
receipt rather than starting again — that is what stops a review from being
re-run until it returns a preferred verdict. There is exactly one exception, and
it is deliberately narrow: an attempt that terminated *before* the provider was
launched owns no durable effect, so it may be superseded at the same HEAD.

The predicate lives in one place, `scripts/review_zero_effect.py`, and is used by
every consumer (the current-review identity check, the harness recovery
classifier, and the attempt retry). A gate is zero-effect only when all of the
following hold:

- `status` is `attention-required`;
- `lanes` is empty — no reviewer lane was ever bound;
- `round_results`, `final_results`, and `evidence` are all empty;
- the attempt is `terminal` with result `attention-required` and no
  `lane_results`.

An absent mapping and an empty mapping count the same; any non-empty value is an
effect. Owning no durable effect is necessary but not sufficient — the caller
must additionally prove no operation row exists before superseding the lineage,
and a zero-lane retry that finds any callback artifact fails closed. Because the
predicate has one owner, no weaker same-HEAD admission path can be added beside
it without changing that module.

### Unconfirmed initial provider input

Delivering the first prompt is two separate facts: the text was pasted, and the
provider actually started a turn. A returned keystroke is transport, so the
worker holds its reserved send until the provider crosses a semantic boundary —
native Claude/Codex activity on screen, a typed artifact appearing at the
callback pointer, or an exact session checkpoint. That wait is bounded by an
observation budget (`initial_start_observation_limit`, threaded into
`await_initial_start_acknowledged`); the worker polls a fixed number of times
rather than waiting indefinitely, so a wedged surface fails fast instead of
holding the lane.

When the budget is exhausted without a semantic start, the worker contains the
provider, marks the operation `attention-required`, and writes the exit status
`input-unconfirmed` with exit code `2`. That status means exactly one thing: the
prompt may or may not have been delivered, and the harness refuses to guess.
Nothing is retried automatically, because a blind resend can double-deliver a
prompt that did start a turn.

Operator recovery, in order:

1. Inspect the surface with `scripts/harness-cli.py inspect` and read the exit
   record. `input-unconfirmed` never means the model produced output.
2. Look at the provider surface itself. If a turn is visibly running, let it
   finish and reconcile — do not resend.
3. If no turn started, reconcile the operation
   (`scripts/harness-cli.py reconcile`) so the reserved send is released, then
   resume. Reconciliation is what makes the resend safe.
4. If the surface is unusable, cancel the operation and dispatch again. The
   callback pointer is idempotent, so a genuinely undelivered prompt loses
   nothing.

`input-unconfirmed` is a containment outcome, not a failure of the reviewed
work; it never advances the review gate and never consumes a finalization cycle.

## Protected research shadow parity

Protected research is the shadow-validation example for staged compiled
operations; it is not a second pipeline language or controller. Its parent,
isolated fetch, and networkless synthesis are immutable `OperationSpec`
instances with deterministic derived operation/lane/run identities under one
owner. All three records live in the same `OperationStore` and use the same
state machine and `OperationSupervisor` as dispatch and review.

Fetch and synthesis enter the generic provider runtime through
`RuntimeSessionRequest`. Their exact `research-fetch` and `research-synth`
callback modes deliver schema-validated typed artifacts (`artifact.json` and
`complete.json`) through the harness-owned callback boundary. The accepted
fetch digest and content-free synthesis provenance bind the second stage; source
or artifact drift fails closed.

Restart derives progress from those same records and typed artifacts. Once the
fetch is complete, replay validates the pinned receipt and reuses the exact
synthesis identity without opening another fetch or provider session. There is
no research-specific progress database, orchestration FSM, dynamic DSL, or
second durable truth. The public-seam evidence is
`tests/harness/test_research_vertical.py`.
