---
type: plan
title: "LLM Obsidian 2.5 — 10 Real-Task Dogfood Window"
address: c-000039
session_id: 019fab00-3160-7380-8920-4b20183afb76
sessions:
  - id: 019fab00-3160-7380-8920-4b20183afb76
    date: 2026-07-31
source_cwd: "/Users/zak/Projects/llm-obsidian"
status: pending
created: 2026-07-31
updated: 2026-07-31
tags:
  - plan
  - dogfood
  - harness
  - skills
  - v2-5
related:
  - "[[2026-07-30-224926-llm-obsidian-2-5-model-authored-custom-pipelines]]"
  - "[[LLM Obsidian 2.5.0 implementation]]"
  - "[[Unattended Pipeline]]"
---

# LLM Obsidian 2.5 — 10 Real-Task Dogfood Window

> [!abstract] Approved contract
> Run ten completed, useful tasks against ordinary `/Users/zak/Projects/llm-obsidian`. Artificial utility-function fixtures do not count. The window exercises real engineering and knowledge workflows to discover defects in skills, dispatch, review, callbacks, reconciliation, cmux ownership and cleanup.

## Fixed decisions

- Target: ordinary LLM Obsidian v2.5.0, never Swarm.
- Integration branch: `dogfood/2.5-real-10`; `main` changes only once after the final gate.
- Waves: `3 + 3 + 3 + 1`; maximum concurrency is 3.
- Executors: five Codex/Sol default tasks and five Claude/Opus 5 default tasks.
- Reviews: eight simple and two deep; final aggregate review is independent Fable + Sol deep review.
- Only completed useful tasks count. `cannot-reproduce`, infrastructure stop or no-result tasks remain evidence and receive replacements until ten tasks complete.
- Eligible narrow repo-owned mechanism defects may be repaired autonomously with regression tests. Dependency, security, public-interface, migration, permission, external-effect and scope-expansion findings become typed parked findings and do not stop unrelated tasks.
- Do not push, publish, deploy, delete worktrees, broaden permissions or commit unrelated user changes.
- No version bump is promised. Decide on a possible 2.5.1 only after the dogfood report and final review.

## Phase 0 — safe baseline

1. Prove old lifecycle records have no live provider, supervisor or cmux ownership.
2. Reconcile or close only exact stale operations through repository-owned runtimes; never hand-edit derived state.
3. Create the integration branch/worktree from exact v2.5.0.
4. Freeze one task contract per dispatch, with exact model route, pipeline and review preset.

## Task matrix

| ID | Wave | Route | Pipeline / skills | Review | Useful result required |
|---|---:|---|---|---|---|
| RT01 | 1 | Codex/Sol | `debug` + `tdd`, engineering/fix | deep | Diagnose why `upgrade-preflight` reports proven-dead legacy operations as active; implement the narrowest safe classification/recovery improvement with regression coverage, or produce a durable evidence-backed decision if a public migration is required. |
| RT02 | 1 | Claude/Opus 5 | `debug` + `tdd`, engineering/fix | deep | Reproduce the backlog exact-cmux-surface cleanup miss across normal exit, timeout and interrupted acceptance; repair exact ownership/cleanup without closing unrelated surfaces and add regressions. |
| RT03 | 1 | Codex/Sol | `prototype` + `design`, lifecycle/default | simple | Test whether same-session review verification can use a machine-built delta packet without losing original context or review quality; deliver a bounded prototype and ADR-quality recommendation, implementing only a clearly proven narrow improvement. |
| RT04 | 2 | Claude/Opus 5 | `debug` + `tdd`, engineering/fix | simple | Forensically classify the four observed invalid review callbacks using durable content-free evidence; reproduce at least one repository-owned cause and repair it, otherwise return a useful bounded diagnostic/report and trigger a replacement task. |
| RT05 | 2 | Codex/Sol | `debug` + `tdd`, engineering/fix | simple | Trace the observed auto-close miss through exact operation/surface ownership; add a deterministic regression and narrow repair, distinct from RT02 if evidence shows a separate boundary. Replace if it is only duplicate evidence. |
| RT06 | 2 | Claude/Opus 5 | `design` + `tdd`, engineering/change | simple | Remove misleading Codex skill dead-weight reporting: design and implement runtime-neutral or explicitly bounded telemetry semantics so pipeline stats do not present absent Claude-only evidence as zero Codex usage. |
| RT07 | 3 | Codex/Sol | `design` + `tdd`, engineering/change | simple | Add a read-only, exact-identity stale worktree/operation diagnostic and actionable recovery guidance without automatic deletion or guessing ownership; integrate with the existing doctor/preflight seam rather than a second state system. |
| RT08 | 3 | Claude/Opus 5 | protected `research` + `wiki-ingest`, lifecycle/default | simple | Refresh the existing Superpowers/Matt-skills comparison against current upstream primary sources, retain minimal cited evidence and update the durable integration guidance without importing tool-specific GitHub workflow assumptions. |
| RT09 | 3 | Codex/Sol | `wiki-lint` + `obsidian-markdown`, lifecycle/default | simple | Run a real vault health audit, repair the highest-value bounded frontmatter/link/orphan issues transactionally, and leave a reproducible dashboard/report. Do not rewrite unrelated knowledge pages. |
| RT10 | 4 | Claude/Opus 5 | `distill-runbook`, lifecycle/default | simple | Distill the accepted dogfood recovery and release commands into a human-executable, AI-outage-safe runbook using actual command evidence from the completed window; verify copy-paste paths and failure stops.

## Wave controller

1. Dispatch at most three tasks from the current integration HEAD.
2. Await typed task summary and review callbacks; do not poll active models.
3. Count a task only after scoped verification, required review, exact cleanup and a useful durable result.
4. Merge approved task commits sequentially into the integration branch. Resolve conflicts from intent evidence; never silently drop a regression.
5. Run focused tests after each merge and the full suite after each wave.
6. If a task is duplicate, unreproducible or blocked by a material boundary, record it and dispatch a real replacement from the remaining evidence backlog.
7. Propagate accepted harness fixes into the next wave base so later tasks test the repaired mechanism.

## Acceptance

- Ten useful tasks are terminal complete, with five Codex and five Claude executors.
- Eight simple and two deep task reviews are approved or have every material finding resolved in the same lane.
- At least seven engineering and three knowledge/ops tasks complete.
- No unresolved callback, provider, surface, cleanup, duplicate-effect or replay failure remains.
- Every repo-owned repair has a regression test and the original failing loop rerun.
- Full `make test`, `make acceptance-check`, vault validation, adapter sync and `git diff --check` pass on the exact integration HEAD.
- Dogfood report separates product findings, harness defects, task replacements, model/runtime behavior, human interventions and remaining material decisions.
- Final independent Fable + Sol deep review approves the exact candidate.
- `main` remains unchanged until the user accepts the final aggregate result; no push or release occurs automatically.
