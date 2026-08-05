---
name: split
description: Fan.
disable-model-invocation: true
allowed-tools: Read Bash
---

# Split

Produce a governed SplitManifest preview for an already approved plan. Invoke
this skill only through explicit `$split`; ordinary requests to divide prose,
files, or data do not select it.

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

## 4. Report bounded evidence

Return the manifest SHA-256, selection mode/reason, ordered child IDs, waves
implied by the DAG, and the validation receipt. State that preview success does
not authorize dispatch, execution, merge, release, or parent completion.

Completion requires the exact manifest plus zero-effect validation. A clean
diff, plausible decomposition, or local schema pass is only a proxy; the parent
outcome still requires approved child receipts and deterministic join evidence.
