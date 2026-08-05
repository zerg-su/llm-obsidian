---
type: plan
title: "LLM Obsidian 2.6.5 Subplan A — exact-HEAD review attempt"
address: c-000118
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

# LLM Obsidian 2.6.5 Subplan A — exact-HEAD review attempt

## Outcome Contract

```json
{
  "schema_version": 1,
  "purpose": "Создать одну immutable exact-HEAD review truth и вывести cross-HEAD continuation из нового critical path.",
  "desired_outcome": "ReviewAttempt фиксирует plan/Outcome/policy/exact HEAD, терминализируется один раз и не rearm/rebind. Новый flow не создаёт verification child или cross-HEAD resolution chain; historical state остаётся read-only.",
  "success_evidence": [
    {
      "evidence_id": "A1-causal-fixtures",
      "observable": "D-264-73 и stale-exiting interleavings воспроизводятся отдельными deterministic fixtures и связаны с root classes, а не timeout symptoms."
    },
    {
      "evidence_id": "A2-immutable-attempt",
      "observable": "Changed HEAD, terminal rearm и verification_iteration больше нуля отклоняются до нового provider/session effect."
    },
    {
      "evidence_id": "A3-legacy-zero-callgraph",
      "observable": "Новый review path не вызывает continuation/rebind/recovery modules; historical resume возвращает typed disabled result и допускает только inspect/archive/cleanup."
    }
  ],
  "non_goals": [
    "Не выполнять push, tag, publish, install или release.",
    "Не менять файлы, принадлежащие другим parallel subplans; пересечение ownership требует coordinator decision.",
    "Не расширять permissions, provider budgets или external effects.",
    "Не менять provider adapters, delivery semantics, FinalizationLedger, PipelineSpec или Split."
  ]
}
```

## Parent and base

Parent: [[2026-08-05-113349-llm-obsidian-2-6-5-event-driven-lifecycle-bounded-finalizati|LLM Obsidian 2.6.5 — event-driven lifecycle, bounded finalization и Split pipeline]]. Exact base: `3e391fc9e6aa48e1344520dbffdebba704312540`. Covers parent E1–E3 and Slices 0–2.

## Files/responsibility

Owned production: new `scripts/harness/review_attempt.py`; `scripts/harness/review_program*.py`; `scripts/harness/workflows/review_gate*.py`; `scripts/task_review_resolution_flow.py`; `scripts/task_review_verification*.py`; `scripts/task_review_*rebind*` and exact legacy recovery callers. Owned evidence/tests: `docs/acceptance/v2.6.5-causal-ledger.md`, new fixtures under `tests/fixtures/v2.6.5/`, new `tests/harness/test_exact_head_review_attempt.py` and `tests/harness/test_review_attempt.py`. Existing broad test files are join-owned: add new focused tests instead of editing them.

## Consumes / produces

Consumes existing ReviewProgram, CallbackEnvelope, exact task/plan/Outcome digests and OperationStore identities. Produces frozen `ReviewAttemptIdentity`, terminal result enum, one-shot terminal write and typed `legacy-cross-head-resume-disabled`. Exported values must be provider-neutral and usable by Subplans B/C after join.

## TDD slices

1. RED causal fixtures: reproduce child-on-terminal-parent, mixed reviewed/resolved HEAD and resource-gone/stale-exiting. Production unchanged.
2. RED/GREEN immutable domain module: reject changed HEAD, rearm, second terminal write and verification iteration before effects.
3. RED/GREEN initial review wiring: one attempt → one exact HEAD → one terminal verdict, no child round.
4. RED/GREEN legacy exclusion: prove new call graph cannot invoke continuation/rebind/recovery; retain read-only parse/inspect/archive/cleanup.
5. Refactor while green: delete only proven unreachable active branches; do not create compatibility execution adapter.

## Verification and handoff

Run focused new tests, review-program/gate/resolution suites, contract matrix and `git diff --check`. Handoff must list exact HEAD, new public internal interfaces, deleted active calls, untouched historical read path, tests and any join-owned adaptations. Review: explicit single-model Deep, runtime `codex`, model `sol`, effort `xhigh`.
