---
type: plan
title: "LLM Obsidian 2.6.5 Join — stability, activation and release"
address: c-000122
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

# LLM Obsidian 2.6.5 Join — stability, activation and release

## Outcome Contract

```json
{
  "schema_version": 1,
  "purpose": "Интегрировать четыре terminal component branches, доказать Stability Gate, активировать bounded Split и собрать release evidence 2.6.5.",
  "desired_outcome": "A–D объединены в deterministic order на одном clean HEAD; central wiring использует одну exact-HEAD attempt truth and typed events, five-cycle finalization and additive DSL. Split activates only after Stability Gate and passes live bounded dogfood plus release gates.",
  "success_evidence": [
    {
      "evidence_id": "J1-component-adoption",
      "observable": "Exact A–D handoff HEADs and contracts adopted in manifest order; conflicts produce attention, never silent semantic choice."
    },
    {
      "evidence_id": "J2-stability-gate",
      "observable": "D-264-73/stale-exiting, zero replay, time-zero-authority, full suite, coverage, acceptance and resource cleanup green before Split activation."
    },
    {
      "evidence_id": "J3-integrated-split",
      "observable": "Existing dispatch/session/cmux wiring executes validated waves workspace-locally and joins exact receipts without helper/reviewer leaks."
    },
    {
      "evidence_id": "J4-release-proof",
      "observable": "All parent E1–E14 mapped and green on one exact clean HEAD with bounded single-model dogfood and typed second-provider fallback/availability receipt."
    }
  ],
  "non_goals": [
    "Не выполнять push, tag, publish, install или release.",
    "Не менять файлы, принадлежащие другим parallel subplans; пересечение ownership требует coordinator decision.",
    "Не расширять permissions, provider budgets или external effects.",
    "Не начинать до terminal reviewed handoff A–D.",
    "Не redesign component interfaces during join without a bounded amendment and affected-task review.",
    "Не publish release without separate user command."
  ]
}
```

## Parent and entry gate

Parent: [[2026-08-05-113349-llm-obsidian-2-6-5-event-driven-lifecycle-bounded-finalizati|LLM Obsidian 2.6.5 — event-driven lifecycle, bounded finalization и Split pipeline]]. Base lineage begins at `3e391fc9e6aa48e1344520dbffdebba704312540`. This plan remains pending until A–D each return terminal summary and approved single-model Codex review.

## Integration order and ownership

Adopt exact branches in order A → B → C → D. The join owns shared integration only: existing broad harness matrices, central dispatch/review/task-session/cmux wiring, plugin/version metadata, README/changelog, release readiness/notes and evidence map. It may adapt component interfaces but must not reimplement them in parallel. Preserve component commits and record every conflict ruling.

## TDD/integration slices

1. Verify four handoff digests, clean branches, scoped ownership and focused gates. Reject stale or overlapping unapproved bytes.
2. Integrate ReviewAttempt + ProviderEvent + FinalizationLedger through one path; remove old active continuation call graph only after equivalence/regressions.
3. Run Stability Gate S: exact D-264-73 and stale-exiting repro, generated event/retry interleavings, max one provider-facing effect, deadline zero authority, no helper/reviewer/eventual-file leaks, complexity/writable-authority reduction, full/coverage/acceptance/clean HEAD. If not green within bounded budget, finish lifecycle-only partial outcome and leave Split disabled.
4. If S green, wire Split preview/validation/waves/locality/join into existing dispatch/task-session adapters; test all eight zero-effect rejections and terminal cleanup.
5. Provider-neutral hermetic conformance for Claude print/Codex exec, one allowed live single-model dogfood, optional second smoke only after typed availability.
6. Update evidence map, deviations, docs, versions and release notes; run full final gate and bounded review. No push/tag/publish.

## Verification

Focused component suites; generated transition/interleaving matrix; full `make test`; honest harness coverage; acceptance; vault validation; Codex/MCP sync checks; skill audits; diff/secret/quality gates; clean exact HEAD; resource inventory. Final review uses explicit single-model Deep Codex/Sol while Opus is constrained, unless the user changes the route.
