---
type: plan
title: "LLM Obsidian 2.6.5 Subplan D — Split components"
address: c-000121
session_id: 019fab00-3160-7380-8920-4b20183afb76
sessions:
  - id: 019fab00-3160-7380-8920-4b20183afb76
    date: 2026-08-05
source_cwd: "/Users/zak/Projects/worktrees/llm-obsidian-2-6-5-coordinator"
status: pending
created: 2026-08-05
updated: 2026-08-05
tags:
  - plan
  - llm-obsidian
  - v2-6-5
  - parallel-subplan
---

# LLM Obsidian 2.6.5 Subplan D — Split components

## Outcome Contract

```json
{
  "schema_version": 1,
  "purpose": "Создать отключённые до Stability Gate Split components: governed skill, exact manifest, zero-effect validation, bounded waves, locality and deterministic join.",
  "desired_outcome": "Split preview выбирает естественное число independent subplans и создаёт exact manifest. Все восемь invalid classes отклоняются без effects. Pure scheduling/join components deterministic; activation existing dispatch остаётся join-owned.",
  "success_evidence": [
    {
      "evidence_id": "D1-governed-split-preview",
      "observable": "Новый split skill проходит raw baseline, fresh-context RED, skill-creator и improve-skills audit; preview имеет variable count, one-child fallback and zero effects."
    },
    {
      "evidence_id": "D2-eight-rejections",
      "observable": "Stale digest, uncovered evidence, overlap, cycle, missing join, weakened non-goal, unregistered pipeline and budget excess each reject before dispatch."
    },
    {
      "evidence_id": "D3-bounded-waves-locality",
      "observable": "Ready queue and waves deterministic with separate subplan_count/max_parallel; locality assignment is explicit data, not coordinator focus."
    },
    {
      "evidence_id": "D4-deterministic-join",
      "observable": "Join accepts exact approved child receipts in manifest order and stops on attention/failure/cancel/conflict/stale HEAD."
    }
  ],
  "non_goals": [
    "Не выполнять push, tag, publish, install или release.",
    "Не менять файлы, принадлежащие другим parallel subplans; пересечение ownership требует coordinator decision.",
    "Не расширять permissions, provider budgets или external effects.",
    "Не активировать split --dispatch, не create worktree/surface/provider and do not edit existing dispatch/session/cmux integration.",
    "Не bypass Stability Gate S."
  ]
}
```

## Parent and base

Parent: [[2026-08-05-113349-llm-obsidian-2-6-5-event-driven-lifecycle-bounded-finalizati|LLM Obsidian 2.6.5 — event-driven lifecycle, bounded finalization и Split pipeline]]. Exact base: `3e391fc9e6aa48e1344520dbffdebba704312540`. Covers parent E10–E13 and Slices 8–11 as disabled components.

## Files/responsibility

Owned new production only: `schemas/split-manifest-v1.schema.json`; `scripts/harness/split_contracts.py`, `split_validation.py`, `split_execution.py`, `split_join.py`; zero-effect preview facade; `skills/split/SKILL.md`; new split fixtures/tests and skill audit/baseline/verdict artifacts. Existing `dispatch-runner.py`, task session/cmux/workspace/cleanup, plugin manifests, broad matrices and release metadata are join-owned and must not be edited.

## Consumes / produces

Consumes parent plan/Outcome/non-goals/digests, registered pipelines, frozen budgets and abstract child terminal receipts. Produces exact SplitManifest, natural-count preview, validation result, deterministic ready waves, locality assignment and join decision. All execution adapters are injected test seams; this branch starts no child/provider/worktree.

## TDD slices

1. Skill governance: raw no-skill cases and fresh-context failure, system skill-creator initialization, improve-skills five-pass audit and schema-v1 verdict before claiming gain.
2. RED/GREEN manifest schema/preview: variable count, exact digests, complete evidence/non-goal inheritance, dependency DAG, declared join, registered child pipeline and one-child fallback.
3. RED/GREEN eight-class zero-effect validation matrix: stale digest, uncovered evidence, overlap, cycle, missing join, weakened non-goal, unregistered pipeline, exceeded budget.
4. RED/GREEN pure waves and locality: deterministic ready queue, subplan_count separate from max_parallel, bounded waves and workspace-local placement as data.
5. RED/GREEN pure join: manifest-order exact receipts, stale/attention/failure/cancel/conflict stop, complete parent evidence proof. No actual merge or dispatch activation.

## Verification and handoff

Run new split/schema/property suites, skill strict+verdict audits, instruction lint, skill budget, Codex adapter, release acceptance and `git diff --check`. Handoff includes exact HEAD, manifest/schema examples, all eight rejection receipts, deterministic wave/join fixtures and explicit statement of zero external effects. Review: explicit single-model Deep, runtime `codex`, model `sol`, effort `xhigh`.
