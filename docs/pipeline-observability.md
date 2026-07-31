# Pipeline observability

The unattended pipeline records enough local **content-free telemetry** to answer three practical
questions: did the handoff finish, where did it wait, and did automation reduce
or merely move human intervention? It deliberately does **not** record what the
task or review said. Separately, an explicitly completed cross-model review is
filed as a normal contentful wiki page under `wiki/meta/reviews/` so a human can
later reconstruct the technical reasoning. That page is not telemetry.

## Generate a report

From the vault root:

```bash
python3 scripts/pipeline-stats.py --days 30
python3 scripts/pipeline-stats.py --days 30 --report
```

The first command prints a report. The second also writes the dated page through
`scripts/vault-write.py`. The report has three separate evidence classes:

1. runtime-neutral operations emitted by shared scripts;
2. unattended task/review lifecycle measurements;
3. Claude-only skill telemetry from Claude history and transcripts.

Do not read the third class as Codex usage. The heading and report text make
that boundary explicit.

## Skill usage is bounded evidence, not a verdict

Only Claude history and transcripts record skill invocations. Codex is
skill-capable but leaves no invocation trace. A zero is therefore reported with
its evidence boundary attached, and the heading states which case applies:

| Heading | Meaning | Safe action |
|---|---|---|
| `Dead-weight candidates` | Skill usage was observed, and nothing that could have invoked a skill unobserved was active | Complete verdict for this window; still confirm before removing |
| `Claude-zero skills` | Codex, or orchestration under no recorded runtime, was active, so a zero only proves absent Claude evidence | Not a removal list; the report separates the skills the router matched inside that uncovered activity |
| `Skill usage evidence unavailable` | No invocation of any kind was observed | Make no claim; widen the window, and check source coverage when no Claude record exists either |

A router hint is a prompt-pattern match, not a recorded invocation — the report
labels those skills unverified rather than proven in use, but they must not be
offered for removal.

`Skill telemetry coverage` above them reports per runtime how many evidence
records were seen and whether its skill invocations are observable at all. Those
counts are per-source presence markers: Claude accrues from four sources and the
others from fewer, so they are not comparable across runtimes.

Runtime attribution comes from the field the existing seams already carry:
`runtime` on `pipeline-events.jsonl` and on `router-hits.jsonl`. No new telemetry
source, file, or emission point is involved. Both seams are clamped to the same
vocabulary, and `unknown` is rendered as **unattributed** — it means the emitting
process carried no runtime marker, not that no model was involved. Agent-driven
orchestration frequently lands there, so an unattributed record carrying an
`agent-run`, `review-round`, `surface-lifecycle`, `task-complete`,
`task-escalation` or `model-turn` op counts as uncovered and blocks the complete
verdict.

## What the lifecycle section measures

| Metric | Meaning |
|---|---|
| Task agent runs | Supervisor-wrapped task processes that returned |
| Validated task completions | Final reaps whose result, summary, plan, and originating session passed the contract |
| Reviewer agent runs | Supervisor-wrapped reviewer processes that returned |
| Review rounds started | Initial review or same-session verification handoffs actually sent to a live reviewer |
| Valid / invalid callbacks | Reviewer payloads accepted or rejected by the versioned JSON contract |
| Findings by severity | Numeric counts only; finding text and file evidence are not copied into telemetry |
| Escalations raised / resolved / delivery failures | Material decisions created, answered, or not delivered to the originating coordinator |
| Watchdog stages | Delivered warning, alert, degraded, and recovery notifications accumulated per agent run |
| Surface outcomes | Exact-surface lifecycle outcome: auto-closed, expected left open, or auto-close missed |

Durations are reported as sample count, p50, and nearest-rank p95:

- **Task end-to-end** — dispatch metadata `spawned_at` to validated final reap.
- **Task/reviewer process** — supervisor start to agent exit plus post-exit
  lifecycle handling.
- **Review round callback** — handoff sent to a schema-valid or rejected
  callback.
- **Human escalation wait** — escalation raised to coordinator decision sent.

Zero-duration synthetic checks are excluded. Percentiles are directional until
the row has at least 10–20 real samples. Before that threshold, inspect every
sample instead of treating p95 as a stable service objective.

## Privacy boundary

Events live in the gitignored `.vault-meta/pipeline-events.jsonl` (plus one
rotated file). The shared schema accepts only:

- runtime, session, actor, operation, and status identifiers;
- an optional map of at most 16 safe identifier keys to bounded tokens or
  one-way hashes;
- vault-relative page paths where an operation already needs them;
- non-negative numeric counters.

Identifier values such as pipeline IDs, profile names, versions, outcome
categories, and definition hashes remain readable only when they match the
bounded token grammar. Arbitrary text and absolute paths are replaced with a
short SHA-256 token; unsafe keys are omitted. The identifier map cannot contain
prompts, commands, prose, or unbounded metadata.

It rejects prompt text, task descriptions, search queries, commands, snippets,
page bodies, review prose, decisions, and error messages. Lifecycle emission is
best-effort: a missing or corrupt telemetry destination never changes the task,
review, escalation, reap, or close result.

The durable review archive has the opposite purpose and a different trust
boundary: it retains the bounded human task-description section plus
schema-validated findings, executor resolutions, verification gaps, residual
risks, and final verdict through `vault-write.py`. Task worktrees cannot write
it directly. Raw orchestration/reviewer prompts, compressed payloads, command
logs, sockets, and cmux IDs are excluded. The result page gets one validated
wikilink to the archive during coordinator `/reap`.

## Reading the numbers

Useful release questions are ratios and trends, not one attractive latency:

- **Completion:** validated completions compared with task runs.
- **Review transport:** valid callbacks divided by all callback attempts.
- **Autonomy:** escalations and auto-close misses per completed task; expected
  attended surfaces left open are reported separately.
- **Reliability:** watchdog alerts, degraded sampling, relay failures, and
  escalation delivery failures or non-zero agent exits.
- **Cost in time:** task end-to-end and review-round p50/p95, compared with the
  same class of work without dispatch.

A review finding is not automatically a pipeline failure; catching a real defect
is the review gate doing useful work. Repeated transport-invalid callbacks,
auto-close misses, unresolved escalations, or delivery failures are mechanism failures and should be
fixed before adding more orchestration.

## Compiled pipeline catalog

LLM Obsidian 2.4 adds a catalog/compiler for lifecycle and engineering semantic
contracts. The existing harness remains the sole executor: it derives the next
action from the compiled step order plus existing `OperationRecord`, typed child
receipts, and review-gate evidence. Each model or verification phase may receive
a derived child `OperationSpec` and immutable typed receipt. Those children live
in the same `OperationStore` and advance through the same FSM under the same
code-owned controller; there is no second progress database, orchestration FSM,
or parallel telemetry path.

The existing `dispatch-runner.py validate` preview renders the lifecycle
contract before approval. Rendering the summary is not execution and creates no
worktree, provider session, or external effect. The summary labels both the
sandbox-enforced write/socket classes and the code-policy-enforced sequencing
boundary, plus exact model/review limits, side-effect classes, and typed return
categories.

Existing operation records and typed receipts remain authoritative; compiled
progress is derived from them and is never a second durable truth. The original
rejected controller design and the later single-controller execution decision
are recorded in
[the original boundary ADR](decisions/v2.4-pipeline-composition-boundary.md)
and its
[superseding ADR](decisions/v2.4-state-free-executable-lifecycle.md).

LLM Obsidian 2.5 adds approved custom definitions without changing that
ownership. Raw `PipelineSpec` data stays in owner-only runtime scratch. The
shared event stream sees only the normalized definition hash, custom/built-in
compiler outcome, bounded primitive/loop counters, terminal or attention
category, and idempotent liveness stages. `custom-pipeline-report.py` groups
those fingerprints and marks a promotion candidate only after repeated
successful runs with zero observed attention; it never promotes a definition
or copies task content. See the
[2.5 boundary ADR](decisions/v2.5-model-authored-pipeline-boundary.md).

### Compiled event call contract

`lifecycle_telemetry.emit_compiled_pipeline_event()` is the best-effort seam for
compiled execution telemetry. Callers provide:

- `event`: a short phase token such as `compile`, `primitive`, `loop`,
  `attention`, or `terminal`;
- identifiers: pipeline ID, pipeline version, profile, compiler outcome,
  definition SHA, and terminal/attention category;
- counters: primitive count and the current bounded-loop iteration.

The helper emits the fixed `compiled-pipeline` operation, applies the shared
identifier masking and caps, and normalizes the two counters. It returns
`False` if telemetry cannot be written and never changes the compiled operation
or its receipt. Task descriptions, primitive inputs/outputs, finding text,
commands, and absolute worktree paths are not valid arguments.

## Dogfood acceptance window

For a release candidate, collect at least 10 completed real tasks across both
executor directions when possible. Include full and light review modes, one
bounded verification loop, and at least one deliberate escalation exercise.
Record the report date and sample counts; do not copy task content into a release
issue. The CI suite proves deterministic contracts, while this window measures
the human and model behavior that hermetic tests cannot simulate.

See also [runtime capabilities](runtime-capabilities.md) for host parity and the
unattended pipeline operations guide for the supervisor, watchdog, review, and
surface state machines.
