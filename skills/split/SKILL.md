---
name: split
description: Fan.
disable-model-invocation: true
allowed-tools: Read Bash
---

# Split

Produce a governed SplitManifest preview for an already approved plan and,
only when the invocation includes `--dispatch`, activate its bounded child
workflow through the harness. Invoke this skill only through explicit `$split`;
ordinary requests to divide prose, files, or data do not select it.

## 1. Freeze the parent contract

Read the approved plan and capture its exact plan and Outcome Contract SHA-256,
ordered evidence IDs, ordered non-goals, registered child pipelines, and frozen
budget. Treat those values as inputs, never as text to weaken or reinterpret.

Complete this step only when every parent evidence item and non-goal has one
exact manifest carrier and no requested authority exceeds the approved plan.

## 2. Propose the natural split

List one candidate per independently owned change reason. Give each candidate
exact owned paths, evidence coverage, dependencies, a transport-neutral route
alias, registered pipeline, and bounded token/time budget. Prefer the natural
count; do not target a fixed number. Use one child when independence is not
proved or coordination cost is not lower than parallel benefit.

Complete this step only when ownership is disjoint, dependencies form a DAG,
and `subplan_count` remains distinct from `max_parallel`.

## 3. Run the zero-effect preview

Pass the request JSON to `python3 scripts/split-preview.py -`. The facade may
read stdin and write stdout only. It has no dispatch option and cannot create a
provider call, worktree, surface, session, child task, or merge.

Accept only `validation.accepted: true`, four zero effect counters, and a
lowercase `manifest_sha256` that round-trips through the exact schema. On any
typed rejection, report its code and stop before child effects.

## 4. Stop at preview or compile activation

Return the manifest SHA-256, selection mode/reason, ordered child IDs, waves
implied by the DAG, and the validation receipt. Without an explicit
`$split --dispatch`, stop here and state that preview success does not authorize
dispatch, execution, merge, release, or parent completion.

For `$split --dispatch`, require the approved plan to authorize child effects.
Bind every child to one existing dispatch request with `placement: workspace`,
the exact manifest slice, registered pipeline and route alias, disjoint owned
files, assigned evidence, dependencies, and a frozen token/time ceiling. Run
`python3 scripts/split-runner.py validate --spec <activation.json>` and continue
only from `status: valid` with four zero effect counters. Validation failure or
policy/budget drift stops before any child effect.

Complete this step only when every child request is exact, the activation waves
equal the preview DAG, and the existing dispatch contract carries the same
manifest SHA-256.

## 5. Drive waves and join exact receipts

Run `split-runner.py start` with the complete prior launch and terminal receipt
sets. Let the runner call the existing dispatch adapter; use `harness-cli.py`
for status, inspect, resume, reconcile, cancel, or close. Never orchestrate
cmux or a provider directly. A current wave may launch at most `max_parallel`;
later waves require exact approved, resource-closed dependency receipts.

After every child is terminal, run `split-runner.py join` with the immutable
launch receipts, terminal receipts, and current child HEAD map. Accept only
`disposition: ready` and use its manifest-order integration list. Attention,
failure, cancellation, conflict, stale HEAD, receipt drift, incomplete evidence,
or open resources stops the join without another child/provider effect.

Preview completion requires the exact manifest plus zero-effect validation.
Dispatch completion additionally requires all exact approved resource-free
child receipts and a ready deterministic join. A clean diff, plausible
decomposition, local schema pass, or launched wave is only a proxy; the parent
outcome still requires its declared evidence and does not authorize merge,
release, push, tag, or publish.
