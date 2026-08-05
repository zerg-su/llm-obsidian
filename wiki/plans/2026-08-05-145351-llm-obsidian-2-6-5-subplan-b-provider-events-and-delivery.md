---
type: plan
title: "LLM Obsidian 2.6.5 Subplan B — provider events and delivery"
address: c-000119
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

# LLM Obsidian 2.6.5 Subplan B — provider events and delivery

## Outcome Contract

```json
{
  "schema_version": 1,
  "purpose": "Свести provider/process facts к typed events и отделить delivery effect от liveness без screen/time authority.",
  "desired_outcome": "Interactive и ephemeral transports производят один validated ProviderEvent contract. Retry возможен только до irreversible input boundary; time/screen не дают positive transition; closure сходится durable и idempotent.",
  "success_evidence": [
    {
      "evidence_id": "B1-event-contract",
      "observable": "Closed vocabulary, exact identity and monotonic cursor reject duplicate/stale/gap/wrong-owner events fail closed."
    },
    {
      "evidence_id": "B2-equal-ephemeral",
      "observable": "Subscription-backed Claude print and ChatGPT-backed Codex exec pass one provider-neutral conformance matrix and cannot silently change auth/billing/provider."
    },
    {
      "evidence_id": "B3-zero-blind-replay",
      "observable": "Before accepted input reconciliation may retry once by idempotency key; after accepted/ambiguous effect no provider-facing replay occurs."
    },
    {
      "evidence_id": "B4-time-last-close",
      "observable": "Timer/screen only trigger recheck or terminal attention; process/workspace disappearance converges through one resource-closed receipt."
    }
  ],
  "non_goals": [
    "Не выполнять push, tag, publish, install или release.",
    "Не менять файлы, принадлежащие другим parallel subplans; пересечение ownership требует coordinator decision.",
    "Не расширять permissions, provider budgets или external effects.",
    "Не менять ReviewAttempt policy, FinalizationLedger, PipelineSpec, skills или Split.",
    "Не запускать real paid/API-key provider calls в hermetic tests."
  ]
}
```

## Parent and base

Parent: [[2026-08-05-113349-llm-obsidian-2-6-5-event-driven-lifecycle-bounded-finalizati|LLM Obsidian 2.6.5 — event-driven lifecycle, bounded finalization и Split pipeline]]. Exact base: `3e391fc9e6aa48e1344520dbffdebba704312540`. Covers parent E4, E4b, E5, E6 and Slices 3–4.

## Files/responsibility

Owned production: new `scripts/harness/provider_events.py` and provider-neutral ephemeral contracts; `scripts/harness/adapters/claude*.py`, `codex.py`, `process*.py`, `cmux.py`; `scripts/harness/runtime_provider.py`, `runtime_callback_io.py`, `runtime_session_delivery*`, `runtime_session_liveness*`, `runtime_session_cleanup.py`. Owned new tests: `tests/harness/test_provider_events.py`, `test_ephemeral_provider_conformance.py`, `test_delivery_boundary.py`. Do not edit review gate/finalization/PipelineSpec/Split or join-owned broad matrices.

## Consumes / produces

Consumes existing SessionIdentity, OwnedResources, OperationStore effect receipts, ContextPacket and output schemas. Produces `ProviderEvent`, `ProviderEventCursor`, `EphemeralRunSpec/Result`, provider-specific argv/auth compilers, `DeliveryDecision(send|wait|submit-callback|attention|close)` and durable close receipts. No public DSL contains CLI transport names.

## TDD slices

1. RED/GREEN closed event vocabulary and identity/cursor/gap validator with duplicate/out-of-order/generated cases.
2. RED/GREEN fake-process conformance shared by Claude print and Codex exec; exact argv, env sanitization, malformed/truncated output, nonzero exit and auth/billing ambiguity.
3. Bounded native-account preflight/probe contract for D-265-EPH-01; live effect remains a later join gate.
4. RED/GREEN irreversible delivery reducer: accepted or ambiguous effect is never blindly replayed; interactive Stop allows one same-HEAD submit-only action; ephemeral missing result is terminal attention.
5. RED/GREEN resource closure: disappeared PID/surface emits or reconciles exactly one durable `resource-closed`; no positive time/screen transition and no real sleep as proof.

## Verification and handoff

Run new suites plus runtime session/adapters/liveness/cleanup tests and `git diff --check`. Handoff includes exact HEAD, event subsets per profile, irreversible boundary table, auth dispositions and zero-leak proof. Review: explicit single-model Deep, runtime `codex`, model `sol`, effort `xhigh`.

Результат: [[LLM Obsidian 2.6.5 Subplan B provider events and delivery]] (reaped 2026-08-05)
