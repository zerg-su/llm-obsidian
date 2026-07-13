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
- vault-relative page paths where an operation already needs them;
- non-negative numeric counters.

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
