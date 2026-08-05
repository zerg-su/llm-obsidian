---
type: plan
title: "LLM Obsidian 2.6.5 Subplan C — bounded finalization and DSL"
address: c-000120
session_id: 019fab00-3160-7380-8920-4b20183afb76
sessions:
  - id: 019fab00-3160-7380-8920-4b20183afb76
    date: 2026-08-05
  - id: 019fab00-3160-7380-8920-4b20183afb76
    date: 2026-08-05
source_cwd: "/Users/zak/Projects/worktrees/llm-obsidian-2-6-5-coordinator"
status: executed
created: 2026-08-05
updated: 2026-08-05
tags:
  - plan
  - llm-obsidian
  - v2-6-5
  - parallel-subplan
---

# LLM Obsidian 2.6.5 Subplan C — bounded finalization and DSL

## Outcome Contract

```json
{
  "schema_version": 1,
  "purpose": "Ограничить finalization пятью immutable cycles и выразить policy в обратно совместимом DSL без изменения standalone Deep.",
  "desired_outcome": "FinalizationLedger атомарно резервирует до пяти exact-HEAD attempts. Циклы 1–3 используют один registered finalization-primary route; 4–5 могут добавить independent route по typed availability. PipelineSpec v1 получает только additive optional policy, а skill/docs parity проходит governance.",
  "success_evidence": [
    {
      "evidence_id": "C1-five-cycle-ledger",
      "observable": "Concurrent reservation linearizable, lineage не сбрасывается HEAD/worktree/provider, шестая попытка zero-effect."
    },
    {
      "evidence_id": "C2-adaptive-finalization-route",
      "observable": "Standalone Deep остаётся dual-provider; finalization cycles 1–3 single primary, cycles 4–5 expand only when permitted/available, explicit single-model wins."
    },
    {
      "evidence_id": "C3-additive-dsl",
      "observable": "Existing PipelineSpec v1 bytes parse unchanged; optional finalization_policy is bounded and rejects unknown aliases/ceilings before effects."
    },
    {
      "evidence_id": "C4-governed-parity",
      "observable": "Implementation-plan, dispatch and review changes have baseline, skill-creator/improve-skills verdicts, lint/budget/adapter/acceptance evidence."
    }
  ],
  "non_goals": [
    "Не выполнять push, tag, publish, install или release.",
    "Не менять файлы, принадлежащие другим parallel subplans; пересечение ownership требует coordinator decision.",
    "Не расширять permissions, provider budgets или external effects.",
    "Не менять standalone review --deep topology.",
    "Не внедрять schema migration, provider event transport или Split skill."
  ]
}
```

## Parent and base

Parent: [[2026-08-05-113349-llm-obsidian-2-6-5-event-driven-lifecycle-bounded-finalizati|LLM Obsidian 2.6.5 — event-driven lifecycle, bounded finalization и Split pipeline]]. Exact base: `3e391fc9e6aa48e1344520dbffdebba704312540`. Covers parent E7–E9 and Slices 5–7.

## Files/responsibility

Owned production: new `scripts/harness/finalization_ledger.py` and policy compiler; `scripts/harness/review_finalization.py`; additive task metadata/schema; `scripts/model_routing_config.py`, `config/model-routing.toml`; `scripts/harness/custom_pipeline_contracts.py`, `schemas/pipeline-spec-v1.schema.json`; related narrow finalization/DSL tests. Owned governed docs/skills: `.claude-memory/feedback_no_claude_p_headless.md`, `AGENTS.md`, `CLAUDE.md`, `docs/runtime-capabilities.md`, `skills/implementation-plan/SKILL.md`, `skills/dispatch/SKILL.md`, `skills/review/SKILL.md`, audit/baseline/verdict artifacts. Do not edit ProviderEvent/runtime adapters, ReviewAttempt/gate or Split modules.

## Frozen policy contract

This task must not reinterpret standalone `review --deep`: it remains Anthropic+OpenAI holistic by default. Add registered `finalization-primary` and `finalization-independent` logical routes for the finalization compiler. PipelineSpec v1 compatibility is additive: `finalization_policy` optional, required set unchanged, persisted v1 specs parse without rewrite. If parser design cannot express that safely, stop for schema-version ADR rather than mutate old bytes.

## TDD slices

1. RED/GREEN FinalizationLedger: atomic cycles 1–5, immutable terminal entry, lineage persistence, zero-effect sixth attempt.
2. RED/GREEN adaptive matrix: explicit single-model precedence, policy denial, unavailable/unknown/stale availability and allowed expansion after cycle 3 without test model call.
3. RED/GREEN additive DSL parser/compiler and registered aliases; regression over every committed example plus `examples/pipelines/document-project-v1.json`.
4. Capture protected skill baselines and use improve-skills `defer`; initialize/update through system skill-creator, then record verdicts and full audit. Reconcile D-265-EPH-01 across memory/AGENTS/CLAUDE/docs while preserving arbitrary direct print prohibition.

## Verification and handoff

Run ledger/routing/custom-pipeline/schema suites, all existing example specs, `audit_skills.py --strict` plain+verdict, `make test-instruction-lint test-skill-budget test-codex-adapter`, `release-acceptance.py check`, and `git diff --check`. Handoff states exact HEAD, schema compatibility proof, route matrix, skill verdicts and join wiring points. Review: explicit single-model Deep, runtime `codex`, model `sol`, effort `xhigh`.

Результат: [[LLM Obsidian 2.6.5 Subplan C bounded finalization and DSL]] (reaped 2026-08-05)
